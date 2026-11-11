"""Poki adapter — https://poki.com (legacy aggregator, ~2,100 games).

Poki's ToS forbids automated access without written consent; the owner decided to track it anyway, so
this adapter is built to be as low-impact as possible:

- only the site's own keyless JSON host `https://api.poki.com` (it has no robots.txt; poki.com itself
  disallows `/*?`, so no query URL on poki.com is ever requested). No HTML, no browser, no game/play pages.
- `rate_limit_rps: 0.33` (≤ 1 request / 3 s, Poki's own ToS rate rule), enforced by the shared `Http`
  throttle; polled every 6 h.
- `realtime.poki.com` is never called unless the platform config sets `live_players: true`.

Query params are pinned to `site=3&device=desktop&country=US` (lists differ by country and device).

Lists (sources.yaml `lists`), one request each:
  home    → GET /list/game/HOME?…&limit=<top_n>  {id, content:[row…]}   site home grid (Poki's own ranking)
  popular → GET /category/popular?…&limit=<top_n> {…, games:[row…]}     99 rows max
  new     → GET /category/new?…&limit=<top_n>     {…, games:[row…]}     130 rows max, release-date order
  row = {id, pokifordevs_game_id, slug, title, developer, image_url, …} — no counters.

fetch(): one GET /game/<slug>?… per game (list rows carry no counters, so there is nothing to batch):
  rating{up_count, down_count, rating}, trending_rank, release_date, developer(s), pokifordevs_team_id,
  categories[], image_url. 404 → status="gone".

Metric semantics (see metric_map in sources.yaml):
  likes       = rating.up_count                 thumbs-up votes, all-time, exact
  votes       = up_count + down_count           total thumbs votes, all-time; Poki publishes no play
                                                counts, so the change in votes between polls is the usage proxy
  players_now = realtime estimate `users`       only with live_players: true; a coarse lower-bound bucket
                                                (the site shows it as "Live N+"); otherwise None
  plays_total / players_total                   None (not public)
  ai_disclosure                                 None (Poki has no AI label or field)
`trending_rank` (top ~50 only) and the 0-5 `rating.rating` have no GameSnapshot column and are not mapped.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from tracker.base import SourceAdapter
from tracker.http import Blocked, BudgetExceeded
from tracker.models import GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API = "https://api.poki.com"
REALTIME = "https://realtime.poki.com"
PARAMS = "site=3&device=desktop&country=US"
IMG = "https://img.poki-cdn.com/cdn-cgi/image/q=78,scq=50,width=314,height=314,fit=cover,f=auto/"
LISTS = {
    "home": "/list/game/HOME",
    "popular": "/category/popular",
    "new": "/category/new",
}
# category slugs that mirror our own lists (and churn with them) rather than describe the game
_LIST_CATEGORIES = {"popular", "new"}


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


def slugify(text) -> str | None:
    if not isinstance(text, str):
        return None
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or None


class Poki(SourceAdapter):
    name = "poki"
    display_name = "Poki"
    base_url = "https://poki.com"
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        self.live_players = bool((config or {}).get("live_players", False))
        # per-run caches keyed by slug: list rows from discover(), game records from fetch()
        self._rows: dict[str, dict] = {}
        self._games: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def game_url(slug: str) -> str:
        return f"https://poki.com/en/g/{slug}"

    @staticmethod
    def list_url(list_name: str, top_n: int) -> str:
        return f"{API}{LISTS[list_name]}?{PARAMS}&limit={int(top_n)}"

    @staticmethod
    def detail_url(slug: str) -> str:
        return f"{API}/game/{quote(slug, safe='')}?{PARAMS}"

    @staticmethod
    def estimate_url(p4d_game_id: str) -> str:
        return f"{REALTIME}/estimate/metrics?id={quote(p4d_game_id, safe='')}"

    def _ref(self, slug: str) -> GameRef:
        return GameRef(source=self.name, external_id=slug, url=self.game_url(slug))

    def p4d_id(self, slug: str) -> str | None:
        for rec in (self._games.get(slug), self._rows.get(slug)):
            pid = (rec or {}).get("pokifordevs_game_id")
            if isinstance(pid, str) and pid:
                return pid
        return None

    @staticmethod
    def _rows_of(list_name: str, data) -> list[dict]:
        if not isinstance(data, dict):
            return []
        rows = data.get("content") if list_name == "home" else data.get("games")
        return [r for r in rows or [] if isinstance(r, dict)]

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            if list_name not in LISTS:
                log.warning("poki: unknown list %r, skipping", list_name)
                continue
            data = self.http.get_json(self.list_url(list_name, top_n))
            captured = _utcnow()
            seen: set[str] = set()
            pos = 0
            for row in self._rows_of(list_name, data):
                if pos >= top_n:
                    break
                slug = row.get("slug")
                if not isinstance(slug, str) or not slug or slug in seen:
                    continue
                seen.add(slug)
                pos += 1
                self._rows[slug] = row
                yield self._ref(slug), ListPlacement(
                    source=self.name, list_name=list_name, position=pos, external_id=slug, captured_at=captured
                )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        for ref in refs:
            slug = ref.external_id
            game = self.http.get_json(self.detail_url(slug))
            if not isinstance(game, dict) or not game:
                yield GameSnapshot(source=self.name, external_id=slug, url=ref.url or self.game_url(slug),
                                   status="gone", captured_at=_utcnow())
                continue
            self._games[slug] = game
            snap = self._snapshot(slug, game)
            if self.live_players:
                snap.players_now = self._live_users(slug)
            yield snap

    def _live_users(self, slug: str) -> int | None:
        """Optional (live_players: true). `users` is a coarse lower-bound bucket (1000, 3000, 5000, …);
        0 / missing means "below the smallest bucket", not zero players, so it stays None."""
        pid = self.p4d_id(slug)
        if not pid:
            return None
        try:
            data = self.http.get_json(self.estimate_url(pid))
        except (Blocked, BudgetExceeded):
            raise
        except Exception as e:  # noqa: BLE001 — best-effort; the vote counters stand
            log.warning("poki: realtime estimate for %s failed (%s)", slug, e)
            return None
        n = _int_or_none(data.get("users")) if isinstance(data, dict) else None
        return n if n else None

    def _snapshot(self, slug: str, g: dict) -> GameSnapshot:
        rating = g.get("rating") if isinstance(g.get("rating"), dict) else {}
        up = _int_or_none(rating.get("up_count"))
        down = _int_or_none(rating.get("down_count"))
        votes = up + down if up is not None and down is not None else None

        developer = g.get("developer") if isinstance(g.get("developer"), str) and g.get("developer").strip() else None
        if developer is None:
            devs = [d for d in g.get("developers") or [] if isinstance(d, str) and d.strip()]
            developer = devs[0] if devs else None
        team = g.get("pokifordevs_team_id")
        creator_id = team if isinstance(team, str) and team else slugify(developer)

        image = g.get("image_url")
        if not isinstance(image, str) or not image:
            image = (g.get("image") or {}).get("path") if isinstance(g.get("image"), dict) else None
        thumb = (image if image.startswith("http") else IMG + image.lstrip("/")) if isinstance(image, str) and image else None

        tags: list[str] = []
        for c in g.get("categories") or []:
            s = c.get("slug") if isinstance(c, dict) else None
            if isinstance(s, str) and s and s not in _LIST_CATEGORIES and s not in tags:
                tags.append(s)

        return GameSnapshot(
            source=self.name,
            external_id=slug,  # keep the requested slug even if the API answers for a renamed one
            url=self.game_url(slug),
            title=g.get("title") or g.get("english_title") or None,
            thumbnail_url=thumb,
            creator_external_id=creator_id,
            creator_name=developer.strip() if developer else None,
            creator_url=None,
            likes=up,
            votes=votes,
            players_now=None,
            plays_total=None,
            players_total=None,
            tags=tags,
            published_at=_parse_dt(g.get("release_date")),
            ai_disclosure=None,
            status="ok",
            captured_at=_utcnow(),
        )

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        """3 list requests + 2 game records (+1 realtime estimate only when live_players is on)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, data) -> None:
            p = out_dir / name
            p.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            written.append(p)

        slugs: list[str] = []
        for list_name in LISTS:
            data = self.http.get_json(self.list_url(list_name, limit))
            save(f"list_{list_name}.json", data)
            for row in self._rows_of(list_name, data):
                if isinstance(row.get("slug"), str) and row["slug"] not in slugs:
                    slugs.append(row["slug"])
                    self._rows[row["slug"]] = row
        # the top popular game (long-lived, big counters) and the newest release (fresh, small counters)
        new = self._rows_of("new", json.loads((out_dir / "list_new.json").read_text()))
        pop = self._rows_of("popular", json.loads((out_dir / "list_popular.json").read_text()))
        picks = [r["slug"] for r in (pop[:1] + new[:1]) if isinstance(r.get("slug"), str)]
        for slug in dict.fromkeys(picks):
            save(f"game_{slug}.json", self.http.get_json(self.detail_url(slug)))
        if self.live_players and picks:
            pid = self.p4d_id(picks[0])
            if pid:
                save("rt_estimate.json", self.http.get_json(self.estimate_url(pid)))
        return written
