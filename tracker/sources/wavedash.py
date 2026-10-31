"""Wavedash adapter — https://wavedash.com (aggregator, ~1,270 published games).

Everything comes from the public Convex HTTP API (no auth, no cookies):
  POST https://convex-api.wavedash.com/api/query   body {"path": fn, "args": {...}, "format": "json"}
  POST https://convex-api.wavedash.com/api/action  (same body; `gameFeed/feed:pageV2` is an action)
  → {"status": "success", "value": ...} | {"status": "error", "errorMessage": ..., "errorData": {"code": ...}}

Functions used (see docs/SOURCES.md and research/wavedash/):
  gamePlayerCounts:listTrendingGames            {}                → [{gameId, count}]  — concurrent players, the
                                                                    ONLY public usage number, ~4–12 games only
  gamePlayerCounts:listTrendingGameCards        {gameIds}         → [{_id, slug, title, wideCover{r2Key}}]
  gameFeed/feed:pageV2 (action)                 {cursor{offset,phase}, includeFeatured, limit, seed}
                                                                  → {page:[{game{_id,slug,title,tags,wideCover,…},
                                                                    organization{name,slug}}], cursor, isDone}
                                                                    phase 0 = the featured set (48), then 3 more
                                                                    phases; the whole catalog is 53 pages of 24.
  games:getPublicGameBySlug                     {slug}            → full game (organization, firstPublishedAt,
                                                                    productionGameCloudId, tags, wideCover{r2Key})
                                                                    or error code "not_found"
  gameComments/queries:getCountForGameCloud     {gameCloudId}     → number
  organizations/queries:get                     {orgSlug}         → {name, slug, description, website, …} or "not_found"

Wavedash publishes no play counts, no total players, no likes. This adapter records list placements,
concurrent players for the trending set, and comment counts. Nothing else is inferred.

Never load /games/<slug> in a browser: the page auto-launches the game (POST /api/game-launch) and
inflates play sessions. Nothing here touches wavedash.com itself except for building URLs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API = "https://convex-api.wavedash.com"
UPLOADS = "https://uploads.wavedash.com"  # serves r2Keys directly (also behind /cdn-cgi/image/… in the sitemap)
FEED_LIMIT = 24
FEED_SEED = 1  # fixed so that the feed order is stable between runs
FEED_MAX_PAGES = 120  # the catalog is 53 pages today; a hard stop in case the cursor ever loops

FN_TRENDING = "gamePlayerCounts:listTrendingGames"
FN_TRENDING_CARDS = "gamePlayerCounts:listTrendingGameCards"
FN_FEED = "gameFeed/feed:pageV2"
FN_GAME = "games:getPublicGameBySlug"
FN_COMMENTS = "gameComments/queries:getCountForGameCloud"
FN_ORG = "organizations/queries:get"


class ConvexError(RuntimeError):
    """The API answered with status != success (and not a plain not_found)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ms_to_dt(value) -> datetime | None:
    """Convex stores timestamps as float ms since the epoch."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _as_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _link_kind(url: str) -> str:
    """Teams have one `website` field; some point it at a social profile. Key it by host."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if host in ("x.com", "twitter.com"):
        return "x"
    if host in ("youtube.com", "youtu.be"):
        return "youtube"
    if host == "tiktok.com":
        return "tiktok"
    if host in ("discord.gg", "discord.com"):
        return "discord"
    return "website"


def _r2key(cover) -> str | None:
    """`wideCover` is a bare r2Key string in the feed and {agentGenerated, r2Key} elsewhere."""
    if isinstance(cover, str) and cover:
        return cover
    if isinstance(cover, dict) and isinstance(cover.get("r2Key"), str) and cover["r2Key"]:
        return cover["r2Key"]
    return None


class Wavedash(SourceAdapter):
    name = "wavedash"
    display_name = "Wavedash"
    base_url = "https://wavedash.com"
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        self._games: dict[str, dict] = {}  # _id -> best game object seen this run (feed / card / full)
        self._players_now: dict[str, int] = {}  # _id -> concurrent players, trending set of this run only
        self._trending_loaded = False
        self._feed_pages: list[dict] | None = None  # full walk, cached per run (shared by featured/catalog)

    # -------------------------------------------------------------- transport
    def _call(self, kind: str, fn: str, args: dict):
        data = self.http.post_json(f"{API}/api/{kind}", {"path": fn, "args": args, "format": "json"})
        if data is None:  # HTTP 404 on the API host itself
            raise ConvexError(f"wavedash: {fn} → HTTP 404")
        if not isinstance(data, dict):
            raise ConvexError(f"wavedash: {fn} → unexpected body {type(data).__name__}")
        status = data.get("status")
        if status == "success":
            return data.get("value")
        err = data.get("errorData")
        code = err.get("code") if isinstance(err, dict) else None
        if code == "not_found":
            return None
        raise ConvexError(f"wavedash: {fn} → {status}: {data.get('errorMessage')!r} {err!r}")

    def _q(self, fn: str, args: dict | None = None):
        return self._call("query", fn, args or {})

    def _a(self, fn: str, args: dict | None = None):
        return self._call("action", fn, args or {})

    # ---------------------------------------------------------------- caching
    def _remember(self, obj: dict) -> None:
        gid = obj.get("_id")
        if not isinstance(gid, str) or not gid:
            return
        cur = self._games.get(gid)
        if cur is None:
            self._games[gid] = dict(obj)
            return
        # merge: a fuller object (full game > feed item > trending card) never loses fields
        merged = dict(cur)
        for k, v in obj.items():
            if v is not None or k not in merged:
                merged[k] = v
        self._games[gid] = merged

    def _remember_feed_item(self, item: dict) -> dict | None:
        game = item.get("game") if isinstance(item, dict) else None
        if not isinstance(game, dict) or not game.get("_id"):
            return None
        obj = dict(game)
        org = item.get("organization")
        if isinstance(org, dict) and org.get("slug") and "organization" not in obj:
            obj["organization"] = org
        self._remember(obj)
        return self._games[obj["_id"]]

    def _url_for(self, slug: str) -> str:
        return f"{self.base_url}/games/{slug}"

    # --------------------------------------------------------------- trending
    def _load_trending(self) -> list[tuple[str, int]]:
        """[(gameId, count)] sorted by count desc; caches cards and players_now for the run."""
        raw = self._q(FN_TRENDING) or []
        rows: list[tuple[str, int]] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            gid, n = r.get("gameId"), _as_int(r.get("count"))
            if isinstance(gid, str) and gid and n is not None:
                rows.append((gid, n))
        rows.sort(key=lambda t: -t[1])
        self._players_now = dict(rows)
        self._trending_loaded = True
        if rows:
            cards = self._q(FN_TRENDING_CARDS, {"gameIds": [g for g, _ in rows]}) or []
            for c in cards:
                if isinstance(c, dict):
                    self._remember(c)
        return rows

    # ------------------------------------------------------------------- feed
    def _feed_page(self, cursor: dict) -> dict:
        v = self._a(FN_FEED, {"cursor": cursor, "includeFeatured": True, "limit": FEED_LIMIT, "seed": FEED_SEED})
        return v if isinstance(v, dict) else {"page": [], "isDone": True, "cursor": cursor}

    def _iter_feed(self, *, phase0_only: bool, max_items: int | None) -> Iterable[dict]:
        """Yield cached game objects in feed order. phase0_only stops when the cursor leaves phase 0
        (the featured set). A full walk is cached for the run so `featured` + `catalog` share it."""
        if self._feed_pages is not None:
            pages = self._feed_pages
        else:
            pages = []
        n = 0
        cursor = {"offset": 0, "phase": 0}
        i = 0
        while i < FEED_MAX_PAGES:
            if i < len(pages):
                page = pages[i]
            else:
                page = self._feed_page(cursor)
                pages.append(page)
            i += 1
            for item in page.get("page") or []:
                obj = self._remember_feed_item(item)
                if obj is None:
                    continue
                n += 1
                yield obj
                if max_items is not None and n >= max_items:
                    return
            nxt = page.get("cursor") or {}
            next_phase = _as_int(nxt.get("phase"))
            if page.get("isDone") or next_phase is None:
                self._feed_pages = pages  # complete walk
                return
            if phase0_only and next_phase != 0:
                return
            cursor = {"offset": _as_int(nxt.get("offset")) or 0, "phase": next_phase}
        log.warning("wavedash: feed did not finish within %d pages; stopping", FEED_MAX_PAGES)

    # --------------------------------------------------------------- discover
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        now = _utcnow()
        for list_name in lists:
            seen: set[str] = set()
            pos = 0
            if list_name == "trending":
                rows = self._load_trending()
                for gid, _count in rows:
                    obj = self._games.get(gid)
                    slug = obj.get("slug") if obj else None
                    if gid in seen or not slug:
                        if not slug:
                            log.warning("wavedash: trending game %s has no card; skipping", gid)
                        continue
                    seen.add(gid)
                    pos += 1
                    yield self._placement(list_name, pos, gid, slug, now)
                    if pos >= top_n:
                        break
            elif list_name in ("featured", "catalog"):
                # featured = feed phase 0 (48 games, 2 requests); catalog = the whole feed (53 requests),
                # position = feed order under the fixed seed. The catalog list exists so that every game
                # gets a first-seen date in the DB (new games/day); the feed itself carries no dates.
                walker = self._iter_feed(phase0_only=(list_name == "featured"), max_items=top_n)
                for obj in walker:
                    gid, slug = obj.get("_id"), obj.get("slug")
                    if not gid or not slug or gid in seen:
                        continue
                    seen.add(gid)
                    pos += 1
                    yield self._placement(list_name, pos, gid, slug, now)
                    if pos >= top_n:
                        break
            elif list_name == "new":
                # The feed exposes no publish date and there is no newest-first list; firstPublishedAt
                # only comes from games:getPublicGameBySlug, i.e. one request per game (~1,270). We do not
                # fabricate an order from the feed. Use `catalog` and the DB's first-seen dates instead.
                log.warning("wavedash: list 'new' is not derivable from the public API; use 'catalog' (feed order) and first-seen dates instead; skipping")
            else:
                log.warning("wavedash: unknown list %r; skipping", list_name)

    def _placement(self, list_name: str, pos: int, gid: str, slug: str, now: datetime):
        ref = GameRef(source=self.name, external_id=gid, url=self._url_for(slug))
        return ref, ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=now)

    # ------------------------------------------------------------------ fetch
    @staticmethod
    def _slug_from_url(url: str) -> str | None:
        marker = "/games/"
        i = url.find(marker)
        if i < 0:
            return None
        slug = url[i + len(marker):].strip("/").split("/")[0].split("?")[0]
        return slug or None

    def _needs_full(self, obj: dict | None) -> bool:
        if obj is None:
            return True
        org = obj.get("organization")
        return not (isinstance(org, dict) and org.get("slug")) or "firstPublishedAt" not in obj

    def _snapshot(self, ref: GameRef, obj: dict, now: datetime) -> GameSnapshot:
        org = obj.get("organization") if isinstance(obj.get("organization"), dict) else {}
        org_slug = org.get("slug") if isinstance(org.get("slug"), str) else None
        build = obj.get("gameBuild") if isinstance(obj.get("gameBuild"), dict) else {}
        engine = build.get("engine")
        key = _r2key(obj.get("wideCover"))
        tags = [t for t in (obj.get("tags") or []) if isinstance(t, str)]

        comments = None
        cloud_id = obj.get("productionGameCloudId")
        if isinstance(cloud_id, str) and cloud_id:
            comments = _as_int(self._q(FN_COMMENTS, {"gameCloudId": cloud_id}))

        # CCU is published for the trending set only. Absent from it = unknown, never 0.
        players_now = self._players_now.get(ref.external_id) if self._trending_loaded else None

        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=self._url_for(obj["slug"]) if obj.get("slug") else ref.url,
            title=obj.get("title"),
            thumbnail_url=f"{UPLOADS}/{key}" if key else None,
            creator_external_id=org_slug,
            creator_name=org.get("name") if org_slug else None,
            creator_url=f"{self.base_url}/teams/{org_slug}" if org_slug else None,
            plays_total=None,  # not public on Wavedash
            likes=None,  # not public on Wavedash
            players_now=players_now,
            comments=comments,
            ai_tool=None,
            engine=str(engine) if engine else None,
            tags=tags,
            published_at=_ms_to_dt(obj.get("firstPublishedAt")),
            status="ok",
            captured_at=now,
        )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        now = _utcnow()
        if not self._trending_loaded:
            # watchlist-only runs (no discover) still need CCU for whatever is trending right now
            self._load_trending()
        for ref in refs:
            gid = ref.external_id
            obj = self._games.get(gid)
            if self._needs_full(obj):
                slug = (obj or {}).get("slug") or self._slug_from_url(ref.url)
                if not slug:
                    log.warning("wavedash: %s has no slug (url %s); skipping", gid, ref.url)
                    continue
                full = self._q(FN_GAME, {"slug": slug})
                if full is None:
                    yield GameSnapshot(source=self.name, external_id=gid, url=ref.url, status="gone", captured_at=now)
                    continue
                if isinstance(full, dict) and full.get("_id") and full["_id"] != gid:
                    # slug reassigned to another game: the id we track is gone
                    self._remember(full)
                    yield GameSnapshot(source=self.name, external_id=gid, url=ref.url, status="gone", captured_at=now)
                    continue
                self._remember(full)
                obj = self._games.get(gid) or full
            yield self._snapshot(ref, obj, now)

    # --------------------------------------------------------------- creators
    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        org = self._q(FN_ORG, {"orgSlug": creator_ref})
        if not isinstance(org, dict):
            return None
        slug = org.get("slug") or creator_ref
        links: dict[str, str] = {}
        site = org.get("website")
        if isinstance(site, str) and site.strip():
            links[_link_kind(site.strip())] = site.strip()
        avatar = org.get("avatarUrl")
        return CreatorProfile(
            source=self.name,
            external_id=str(slug),
            name=org.get("name"),
            url=f"{self.base_url}/teams/{slug}",
            avatar_url=f"{UPLOADS}/{avatar}" if isinstance(avatar, str) and avatar else None,
            followers=None,  # not public
            bio=org.get("description") or None,
            links=links,
        )

    # --------------------------------------------------------------- fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def dump(name: str, payload) -> None:
            p = out_dir / name
            p.write_text(json.dumps({"status": "success", "value": payload}, indent=1, ensure_ascii=False))
            written.append(p)

        trending = self._q(FN_TRENDING) or []
        dump("trending.json", trending)
        ids = [r["gameId"] for r in trending if isinstance(r, dict) and r.get("gameId")]
        cards = self._q(FN_TRENDING_CARDS, {"gameIds": ids}) if ids else []
        dump("trending_cards.json", cards or [])
        for c in cards or []:
            if isinstance(c, dict):
                self._remember(c)

        p1 = self._feed_page({"offset": 0, "phase": 0})
        dump("feed_p1.json", p1)
        for item in p1.get("page") or []:
            self._remember_feed_item(item)
        nxt = p1.get("cursor") or {}
        p2 = self._feed_page({"offset": _as_int(nxt.get("offset")) or 0, "phase": _as_int(nxt.get("phase")) or 0})
        dump("feed_p2.json", p2)
        for item in p2.get("page") or []:
            self._remember_feed_item(item)

        # full game objects: trending first (they are the ones fetch() needs to resolve), then feed
        slugs: list[str] = []
        for gid in ids:
            s = (self._games.get(gid) or {}).get("slug")
            if s and s not in slugs:
                slugs.append(s)
        for obj in self._games.values():
            s = obj.get("slug")
            if s and s not in slugs:
                slugs.append(s)
        slugs = slugs[:limit]
        comments: dict[str, int | None] = {}
        org_slug = None
        for s in slugs:
            full = self._q(FN_GAME, {"slug": s})
            if full is None:
                continue
            dump(f"game_{s}.json", full)
            cloud = full.get("productionGameCloudId")
            if cloud:
                comments[cloud] = _as_int(self._q(FN_COMMENTS, {"gameCloudId": cloud}))
            org = full.get("organization") or {}
            if not org_slug and org.get("slug"):
                org_slug = org["slug"]
        dump("comments_count.json", comments)
        if org_slug:
            org = self._q(FN_ORG, {"orgSlug": org_slug})
            if org:
                dump(f"org_{org_slug}.json", org)
        return written
