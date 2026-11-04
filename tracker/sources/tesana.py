"""Tesana adapter — https://tesana.com (creation platform).

Everything is public, unauthenticated JSON on `https://api.tesana.ai` (see docs/SOURCES.md,
research/tesana/). No browser, no tokens. We never call `/api/games/<id>/view/` (side effect).

Lists (sources.yaml `lists`):
  played | trending | new | rising → GET /api/marketplace/charts/?metric=<m>&limit=<n>
                                     {metric, period, results:[{rank, game{…}}]}; position = rank
  popular                          → GET /api/marketplace/section/popular/?page=N
                                     {results:[game…], count, next, previous}; 24 per page
  playing_now                      → GET /api/play/playing-now/top/?limit=<n>
                                     {total, results:[game…]}; position = index + 1

Game objects in every list already carry all counters, so `fetch` reuses what `discover` saw
in the same run and only hits `/api/games/store/<slug>/` for refs that came from the watchlist.
Concurrent players for the whole batch are refreshed with `/api/play/playing-now/?ids=…`
(`{public_id: n}`), 50 ids per request.

Metric semantics (platform's own definitions, see metric_map in sources.yaml):
  plays_total   = play_count      cumulative play sessions started
  players_total = players_7d      unique players in the trailing 7 days — NOT an all-time figure;
                                  Tesana exposes no all-time unique-player count per game
  players_now   = playing_now     concurrent players right now
  likes         = favorite_count
  remixes       = remix_count     store record only
  comments      = message_count   store record only
  votes         = verdict.total   store record only (loved + not_for_me)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API = "https://api.tesana.ai"
CHART_METRICS = {"played", "trending", "new", "rising"}
SECTION_PAGE_SIZE = 24
PLAYING_NOW_BATCH = 50
CHART_HARD_CAP = 104  # weekly charts never return more than ~104 rows regardless of limit

# host → link key used in CreatorProfile.links
_LINK_HOSTS = {
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "discord.gg": "discord",
    "discord.com": "discord",
    "twitch.tv": "twitch",
    "instagram.com": "instagram",
    "reddit.com": "reddit",
    "github.com": "github",
    "itch.io": "itch",
    "bsky.app": "bluesky",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _int_or_none(value) -> int | None:
    """Counters: None stays None; never coerce a missing value to 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        return int(s) if s.isdigit() else None
    return None


def _tags(raw) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        if isinstance(t, dict):
            v = t.get("slug") or t.get("name")
        else:
            v = t
        if isinstance(v, str) and v:
            out.append(v)
    return out


def _link_key(url: str, fallback: str | None = None) -> str:
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for h, key in _LINK_HOSTS.items():
        if host == h or host.endswith("." + h):
            return key
    return fallback or "website"


class Tesana(SourceAdapter):
    name = "tesana"
    display_name = "Tesana"
    base_url = "https://tesana.com"
    segment = "creation"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        # per-run caches, filled by discover(); keyed by slug
        self._games: dict[str, dict] = {}
        self._public_ids: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def game_url(slug: str) -> str:
        return f"https://tesana.com/game/{slug}"

    @staticmethod
    def creator_url(username: str) -> str:
        return f"https://tesana.com/u/{quote(username, safe='')}"

    def _remember(self, game: dict) -> str | None:
        slug = game.get("slug")
        if not isinstance(slug, str) or not slug:
            return None
        self._games[slug] = game
        pid = game.get("public_id")
        if isinstance(pid, str) and pid:
            self._public_ids[slug] = pid
        return slug

    def _ref(self, slug: str) -> GameRef:
        return GameRef(source=self.name, external_id=slug, url=self.game_url(slug))

    # ------------------------------------------------------------------ list walkers
    def _charts(self, metric: str, top_n: int) -> Iterable[tuple[int, dict]]:
        limit = min(int(top_n), CHART_HARD_CAP)
        data = self.http.get_json(f"{API}/api/marketplace/charts/?metric={metric}&limit={limit}")
        rows = (data or {}).get("results") or []
        for i, row in enumerate(rows[:top_n], start=1):
            game = row.get("game") if isinstance(row, dict) else None
            if not isinstance(game, dict):
                continue
            pos = _int_or_none(row.get("rank")) or i
            yield pos, game

    def _catalog(self, top_n: int) -> Iterable[tuple[int, dict]]:
        """The whole marketplace, most-played first: /api/marketplace/search/?q=&sort=plays (60 per page,
        ~60 pages for ~3,600 games). Every row carries the full counter set, so fetch() needs no extra requests."""
        pos = 0
        page = 1
        while pos < top_n:
            data = self.http.get_json(f"{API}/api/marketplace/search/?q=&sort=plays&page_size=60&page={page}")
            if not data:
                break
            rows = data.get("results") or []
            if not rows:
                break
            for game in rows:
                if pos >= top_n:
                    break
                if isinstance(game, dict):
                    pos += 1
                    yield pos, game
            if not data.get("next"):
                break
            page += 1

    def _section(self, key: str, top_n: int) -> Iterable[tuple[int, dict]]:
        pos = 0
        page = 1
        url: str | None = f"{API}/api/marketplace/section/{key}/?page={page}"
        while url and pos < top_n:
            data = self.http.get_json(url)
            if not data:
                break
            rows = data.get("results") or []
            if not rows:
                break
            for game in rows:
                if pos >= top_n:
                    break
                if isinstance(game, dict):
                    pos += 1
                    yield pos, game
            if not data.get("next"):
                break
            page += 1
            # rebuild the URL ourselves instead of following `next` (same host/path; keeps us on the API host)
            url = f"{API}/api/marketplace/section/{key}/?page={page}"

    def _playing_now_top(self, top_n: int) -> Iterable[tuple[int, dict]]:
        data = self.http.get_json(f"{API}/api/play/playing-now/top/?limit={int(top_n)}")
        rows = (data or {}).get("results") or []
        for i, game in enumerate(rows[:top_n], start=1):
            if isinstance(game, dict):
                yield i, game

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            if list_name in CHART_METRICS:
                walker = self._charts(list_name, top_n)
            elif list_name == "popular":
                walker = self._section("popular", top_n)
            elif list_name == "playing_now":
                walker = self._playing_now_top(top_n)
            elif list_name == "catalog":
                walker = self._catalog(top_n)
            else:
                log.warning("tesana: unknown list %r, skipping", list_name)
                continue
            captured = _utcnow()
            seen: set[str] = set()
            for pos, game in walker:
                slug = self._remember(game)
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                yield self._ref(slug), ListPlacement(
                    source=self.name, list_name=list_name, position=pos, external_id=slug, captured_at=captured
                )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        snaps: list[GameSnapshot] = []
        for ref in refs:
            slug = ref.external_id
            game = self._games.get(slug)
            store = False
            if game is None:
                game = self.http.get_json(f"{API}/api/games/store/{quote(slug, safe='')}/")
                store = True
                if game is None:
                    snaps.append(GameSnapshot(source=self.name, external_id=slug, url=ref.url, status="gone", captured_at=_utcnow()))
                    continue
                self._remember(game)
            snaps.append(self._snapshot(slug, ref.url, game, store=store))

        # refresh concurrent players for everything we can map to a public_id
        live = self._playing_now_ids([self._public_ids[s.external_id] for s in snaps if s.external_id in self._public_ids])
        for snap in snaps:
            pid = self._public_ids.get(snap.external_id)
            if pid is not None and pid in live and snap.status == "ok":
                n = _int_or_none(live[pid])
                if n is not None:
                    snap.players_now = n
            yield snap

    def _playing_now_ids(self, public_ids: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        ids = list(dict.fromkeys(public_ids))
        for i in range(0, len(ids), PLAYING_NOW_BATCH):
            batch = ids[i : i + PLAYING_NOW_BATCH]
            try:
                data = self.http.get_json(f"{API}/api/play/playing-now/?ids={','.join(batch)}")
            except Exception as e:  # noqa: BLE001 — CCU refresh is best-effort; counters from the list stand
                log.warning("tesana: playing-now batch failed (%s)", e)
                continue
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if isinstance(k, str)})
        return out

    def _snapshot(self, slug: str, url: str, g: dict, *, store: bool) -> GameSnapshot:
        username = g.get("user")
        if not isinstance(username, str) or not username:
            # store records carry the author under `creator.username` and have `user: null`
            creator = g.get("creator")
            username = creator.get("username") if isinstance(creator, dict) else None
            if not isinstance(username, str) or not username:
                username = None
        verdict = g.get("verdict") if isinstance(g.get("verdict"), dict) else {}
        return GameSnapshot(
            source=self.name,
            external_id=slug,
            url=url or self.game_url(slug),
            title=g.get("name") or None,
            thumbnail_url=g.get("cover_url") or None,
            creator_external_id=username,
            creator_name=username,
            creator_url=self.creator_url(username) if username else None,
            plays_total=_int_or_none(g.get("play_count")),
            # players_total = players_7d: Tesana's own trailing-7-day unique players figure. There is no
            # all-time unique-player counter per game; this is the closest the platform exposes.
            players_total=_int_or_none(g.get("players_7d")),
            players_now=_int_or_none(g.get("playing_now")),
            likes=_int_or_none(g.get("favorite_count")),
            remixes=_int_or_none(g.get("remix_count")) if store else None,
            comments=_int_or_none(g.get("message_count")) if store else None,
            votes=_int_or_none(verdict.get("total")) if store else None,
            engine=g.get("engine_kind") or None,
            tags=_tags(g.get("tags")),
            published_at=_parse_dt(g.get("published_at")),
            status="ok",
            captured_at=_utcnow(),
        )

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        data = self.http.get_json(f"{API}/api/games/users/{quote(creator_ref, safe='')}/")
        if not isinstance(data, dict):
            return None
        username = data.get("username") or creator_ref
        followers = _int_or_none(data.get("follower_count"))
        if followers is None:
            stats = data.get("stats")
            if isinstance(stats, dict):
                followers = _int_or_none(stats.get("followers"))
        bio = data.get("bio") or data.get("biography") or None
        return CreatorProfile(
            source=self.name,
            external_id=username,
            name=data.get("display_name") or username,
            url=self.creator_url(username),
            avatar_url=data.get("profile_picture") or data.get("avatar_url") or None,
            followers=followers,
            bio=bio if isinstance(bio, str) and bio.strip() else None,
            links=self._links(data.get("links")),
        )

    @staticmethod
    def _links(raw) -> dict[str, str]:
        """`links[]` was empty on every profile captured so far; accept strings or dicts
        ({url, label|type|platform|kind}) and key them by platform where identifiable."""
        out: dict[str, str] = {}
        if isinstance(raw, dict):
            raw = [{"type": k, "url": v} for k, v in raw.items()]
        for item in raw or []:
            url = None
            hint = None
            if isinstance(item, str):
                url = item
            elif isinstance(item, dict):
                url = item.get("url") or item.get("href") or item.get("link")
                hint = item.get("type") or item.get("platform") or item.get("kind") or item.get("label") or item.get("name")
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            if "://" not in url:
                url = "https://" + url
            hint = hint.strip().lower() if isinstance(hint, str) and hint.strip() else None
            if hint == "twitter":
                hint = "x"
            key = _link_key(url, hint)
            if key in out:  # keep first, suffix duplicates
                n = 2
                while f"{key}_{n}" in out:
                    n += 1
                key = f"{key}_{n}"
            out[key] = url
        return out

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, data) -> None:
            p = out_dir / name
            p.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            written.append(p)

        charts = self.http.get_json(f"{API}/api/marketplace/charts/?metric=played&limit={limit}")
        save("charts_played.json", charts)
        popular = self.http.get_json(f"{API}/api/marketplace/section/popular/?page=1")
        save("section_popular_p1.json", popular)
        top = self.http.get_json(f"{API}/api/play/playing-now/top/?limit={limit}")
        save("playing_now_top.json", top)

        games = [r.get("game") for r in (charts or {}).get("results") or [] if isinstance(r, dict)]
        games = [g for g in games if isinstance(g, dict) and g.get("slug")]
        if not games:
            games = [g for g in (popular or {}).get("results") or [] if isinstance(g, dict) and g.get("slug")]
        if games:
            slug = games[0]["slug"]
            store = self.http.get_json(f"{API}/api/games/store/{quote(slug, safe='')}/")
            save(f"store_{slug}.json", store)
            ids = [g["public_id"] for g in games if isinstance(g.get("public_id"), str)][:PLAYING_NOW_BATCH]
            if ids:
                save("playing_now_ids.json", self.http.get_json(f"{API}/api/play/playing-now/?ids={','.join(ids)}"))
            username = games[0].get("user") or ((store or {}).get("creator") or {}).get("username")
            if username:
                save(f"user_{username}.json", self.http.get_json(f"{API}/api/games/users/{quote(username, safe='')}/"))
        return written
