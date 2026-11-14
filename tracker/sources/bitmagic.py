"""Bitmagic adapter — https://bitmagic.ai (creation platform, ~150 public games).

Everything comes from the same-origin JSON catalog API; no HTML parsing of game pages, no browser
(a page load POSTs /api/games/<id>/view and inflates viewCount, which we never do).

Token: `/api/catalog/*`, `/api/trending` and `/api/players/*` require `X-API-Token`. The value is a
public constant shipped in the site's `/assets/config-<hash>.js` bundle (exported as
`CATALOG_API_TOKEN`); it rotates per deploy, so it is discovered at runtime and refreshed once when
an API call comes back 401/403 (`Http` raises `Blocked` for those).

Endpoints used (see docs/SOURCES.md and research/bitmagic/):
  GET /api/trending?offset&limit=50                → {items:[{gameId, globalRank, playCount, likeCount, creator}], total}
  GET /api/catalog/browse?sort=popular|new&limit=50&offset → {games:[{gameId, name, publishedAt, viewCount, playCount,
                                                      likeCount, commentCount, creator{brandId,…}}], total, nextOffset}
  GET /api/catalog/games?ids=A,B,…                 → {games:{id: {…same object…}}}  (missing ids are simply absent)
  GET /api/catalog/live?ids=A,B,…                  → {counts:{id: n}}  (omits zeros)
  GET /api/players/<brandId>/profile               → {profile:{displayName, brandId, followerCount, bio, homePageUrl, avatarUrl}}

viewCount is deliberately not mapped (no column for it).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from tracker.base import SourceAdapter
from tracker.http import Blocked
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

PAGE_SIZE = 50
BATCH_SIZE = 25  # /api/catalog/games?ids= and /live?ids=; halved on a 4xx. Measured 2026-10-29: 100 ids OK, 101 → HTTP 400.

# <script type="module" crossorigin src="./assets/config-BDJXXKMg.js">
_CONFIG_SCRIPT_RE = re.compile(r"""(?:src|href)=["']([^"']*?/assets/config-[A-Za-z0-9_-]+\.js)["']""")
# function s(){const r="t/ijpMV5…XM=";return r.trim()!==""?r.trim():"localhost-token"}  … A=l … export{… A as CATALOG_API_TOKEN …}
_TOKEN_FN_RE = re.compile(r"""const\s+(\w+)\s*=\s*["']([^"']{16,})["']\s*;\s*return\s+\1\.trim\(\)""")
_TOKEN_LITERAL_RE = re.compile(r"""["']([A-Za-z0-9+/_-]{32,}={0,2})["']""")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_token(config_js: str) -> str | None:
    """Pull CATALOG_API_TOKEN out of the config bundle.

    Preferred: the `const X="…";return X.trim()` getter the bundle wraps the token in.
    Fallback: the first long base64-looking string literal in the file.
    """
    m = _TOKEN_FN_RE.search(config_js)
    if m:
        return m.group(2).strip()
    m = _TOKEN_LITERAL_RE.search(config_js)
    return m.group(1) if m else None


def extract_config_script(html: str, base_url: str) -> str | None:
    m = _CONFIG_SCRIPT_RE.search(html)
    if not m:
        return None
    return urljoin(base_url.rstrip("/") + "/", m.group(1))


class Bitmagic(SourceAdapter):
    name = "bitmagic"
    display_name = "Bitmagic"
    base_url = "https://bitmagic.ai"
    segment = "creation"
    needs_browser = False

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        self._token: str | None = None
        self._config_js: str | None = None  # kept for record_fixtures
        self._objects: dict[str, dict] = {}  # gameId -> last full catalog object seen (browse/batch)

    # ------------------------------------------------------------------ token
    def _discover_token(self) -> str:
        home = self.http.get(self.base_url + "/")
        script_url = extract_config_script(home.text, self.base_url)
        if not script_url:
            raise RuntimeError("bitmagic: no /assets/config-*.js reference on the home page")
        js = self.http.get(script_url).text
        token = extract_token(js)
        if not token:
            raise RuntimeError(f"bitmagic: CATALOG_API_TOKEN not found in {script_url}")
        self._config_js = js
        return token

    def token(self, refresh: bool = False) -> str:
        if refresh or not self._token:
            self._token = self._discover_token()
        return self._token

    def _api(self, path: str, **params):
        """GET a JSON API path with the token; on a single Blocked, refresh the token and retry once."""
        url = self.base_url + path
        headers = {"X-API-Token": self.token()}
        try:
            return self.http.get_json(url, params=params or None, headers=headers)
        except Blocked:
            log.info("bitmagic: API rejected token on %s; re-reading config bundle once", path)
            headers = {"X-API-Token": self.token(refresh=True)}
            return self.http.get_json(url, params=params or None, headers=headers)

    def _stats(self):
        """/api/catalog/stats, fetched at most once per run (platform_totals and site_online both use it)."""
        if not hasattr(self, "_stats_cache"):
            self._stats_cache = self._api("/api/catalog/stats")
        return self._stats_cache

    def site_online(self) -> dict | None:
        """Bitmagic's own site-wide "playing now" (one request, shared with platform_totals)."""
        data = self._stats() or {}
        st = data.get("stats") if isinstance(data.get("stats"), dict) else data
        v = st.get("playingNow") if isinstance(st, dict) else None
        return {"players_now": int(v)} if isinstance(v, (int, float)) else None

    def platform_totals(self) -> dict | None:
        """GET /api/catalog/stats: platform-wide plays, games, creators and players online (one request)."""
        data = self._stats() or {}
        st = data.get("stats") if isinstance(data.get("stats"), dict) else data
        if not isinstance(st, dict) or st.get("totalPlays") is None:
            return None
        def i(k):
            v = st.get(k)
            return int(v) if isinstance(v, (int, float)) else None
        return {"plays_total": i("totalPlays"), "games": i("totalGames"), "creators": i("totalCreators"),
                "players_now": i("playingNow"),
                "extra": {k: st[k] for k in ("newGamesThisWeek", "creatorsShippingThisWeek") if k in st}}

    # --------------------------------------------------------------- discover
    def _url_for(self, game_id: str) -> str:
        return f"{self.base_url}/play/{game_id}/"

    def _remember(self, obj: dict) -> None:
        gid = obj.get("gameId")
        if gid:
            # browse/batch objects are a superset of trending items; never downgrade a cached object
            if gid not in self._objects or "publishedAt" in obj or "commentCount" in obj:
                self._objects[gid] = obj

    def _iter_trending(self, top_n: int) -> Iterable[tuple[int, dict]]:
        offset, idx = 0, 0
        while idx < top_n:
            data = self._api("/api/trending", offset=offset, limit=PAGE_SIZE) or {}
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                idx += 1
                pos = item.get("globalRank")
                yield (int(pos) if isinstance(pos, int) and pos > 0 else idx), item
                if idx >= top_n:
                    break
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    def _iter_browse(self, sort: str, top_n: int) -> Iterable[tuple[int, dict]]:
        offset, idx = 0, 0
        while idx < top_n:
            data = self._api("/api/catalog/browse", sort=sort, limit=PAGE_SIZE, offset=offset) or {}
            items = data.get("games") or []
            if not items:
                break
            for item in items:
                idx += 1
                yield idx, item
                if idx >= top_n:
                    break
            nxt = data.get("nextOffset")
            if len(items) < PAGE_SIZE or nxt is None:
                break
            offset = int(nxt)

    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        now = _utcnow()
        for list_name in lists:
            if list_name == "trending":
                walker = self._iter_trending(top_n)
            elif list_name in ("popular", "new"):
                walker = self._iter_browse(list_name, top_n)
            else:
                log.warning("bitmagic: unknown list %r; skipping", list_name)
                continue
            seen: set[str] = set()
            for pos, item in walker:
                gid = item.get("gameId")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                self._remember(item)
                ref = GameRef(source=self.name, external_id=gid, url=self._url_for(gid))
                yield ref, ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=now)

    # ------------------------------------------------------------------ fetch
    def _batched(self, path: str, key: str, ids: list[str]) -> tuple[dict, set[str]]:
        """GET <path>?ids=… in chunks; returns (merged dict under `key`, set of ids actually asked).

        A 4xx on a chunk (URL too long, limit exceeded) halves the chunk and retries; a chunk of one
        that still fails is skipped and logged, never faked.
        """
        import httpx  # local import keeps the module importable in tests without touching the network

        merged: dict = {}
        asked: set[str] = set()
        queue: list[list[str]] = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
        while queue:
            chunk = queue.pop(0)
            try:
                data = self._api(path, ids=",".join(chunk)) or {}
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500 and len(chunk) > 1:
                    half = len(chunk) // 2
                    log.warning("bitmagic: %s rejected %d ids (%s); retrying as %d+%d", path, len(chunk), e.response.status_code, half, len(chunk) - half)
                    queue[:0] = [chunk[:half], chunk[half:]]
                    continue
                log.warning("bitmagic: %s failed for %s: %s", path, chunk, e)
                continue
            asked.update(chunk)
            part = data.get(key)
            if isinstance(part, dict):
                merged.update(part)
            elif isinstance(part, list):  # defensive: tolerate a list form
                for obj in part:
                    if isinstance(obj, dict) and obj.get("gameId"):
                        merged[obj["gameId"]] = obj
        return merged, asked

    @staticmethod
    def _creator_fields(obj: dict) -> tuple[str | None, str | None]:
        creator = obj.get("creator") or {}
        cid = creator.get("brandId") or creator.get("canonicalId") or creator.get("publicId")
        return (str(cid) if cid else None), creator.get("displayName")

    def _snapshot(self, ref: GameRef, obj: dict, players_now: int | None, now: datetime) -> GameSnapshot:
        creator_id, creator_name = self._creator_fields(obj)
        engine = obj.get("engine") or obj.get("builtWith")
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=ref.url,
            title=obj.get("name"),
            thumbnail_url=obj.get("thumbnailUrl"),
            creator_external_id=creator_id,
            creator_name=creator_name,
            creator_url=f"{self.base_url}/u/{creator_id}/" if creator_id else None,
            plays_total=obj.get("playCount"),
            likes=obj.get("likeCount"),
            comments=obj.get("commentCount"),
            players_now=players_now,
            ai_tool=None,  # Bitmagic is the platform, not the AI tool used to make the game
            engine=str(engine) if engine else None,
            published_at=_parse_dt(obj.get("publishedAt")),
            status="ok",
            captured_at=now,
        )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        now = _utcnow()
        ids = [r.external_id for r in refs]
        games, asked = self._batched("/api/catalog/games", "games", ids)
        present = [gid for gid in ids if gid in games]
        for obj in games.values():
            if isinstance(obj, dict):
                self._remember(obj)

        # /api/catalog/live omits ids with zero players in a room, so for every game that the catalog
        # DID return, "absent from counts" means 0 — not unknown. Games not covered by a successful
        # live request (chunk failed) keep players_now=None.
        counts, live_asked = self._batched("/api/catalog/live", "counts", present) if present else ({}, set())

        for ref in refs:
            gid = ref.external_id
            obj = games.get(gid)
            if not isinstance(obj, dict):
                if gid in asked:
                    yield GameSnapshot(source=self.name, external_id=gid, url=ref.url, status="gone", captured_at=now)
                else:
                    log.warning("bitmagic: no catalog answer for %s (batch failed); skipping", gid)
                continue
            if gid in live_asked:
                raw = counts.get(gid, 0)
                players_now = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
            else:
                players_now = None
            yield self._snapshot(ref, obj, players_now, now)

    # --------------------------------------------------------------- creators
    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        data = self._api(f"/api/players/{creator_ref}/profile")
        if not data:
            return None
        prof = data.get("profile") if isinstance(data, dict) else None
        if not isinstance(prof, dict):
            return None
        brand = prof.get("brandId") or creator_ref
        links: dict[str, str] = {}
        home = prof.get("homePageUrl")
        if isinstance(home, str) and home.strip():
            links["website"] = home.strip()
        return CreatorProfile(
            source=self.name,
            external_id=str(brand),
            name=prof.get("displayName"),
            url=f"{self.base_url}/u/{brand}/",
            avatar_url=prof.get("avatarUrl"),
            followers=prof.get("followerCount"),
            bio=prof.get("bio") or None,
            links=links,
        )

    # --------------------------------------------------------------- fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def dump(name: str, payload) -> None:
            p = out_dir / name
            if isinstance(payload, str):
                p.write_text(payload)
            else:
                p.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
            written.append(p)

        self.token(refresh=True)
        js = self._config_js or ""
        i = js.find("CATALOG_API_TOKEN")
        tok = self._token or ""
        j = js.find(tok) if tok else -1
        anchor = j if j >= 0 else max(i, 0)
        start, end = max(0, anchor - 1024), min(len(js), anchor + 1024)
        # make sure the export list naming CATALOG_API_TOKEN is kept even if far from the literal
        snippet = js[start:end]
        if i >= 0 and not (start <= i < end):
            snippet += "\n/* … */\n" + js[max(0, i - 200): i + 200]
        dump("config.js", snippet)

        trending = self._api("/api/trending", offset=0, limit=PAGE_SIZE) or {}
        dump("trending_p1.json", trending)
        popular = self._api("/api/catalog/browse", sort="popular", limit=PAGE_SIZE, offset=0) or {}
        dump("browse_popular_p1.json", popular)
        new = self._api("/api/catalog/browse", sort="new", limit=PAGE_SIZE, offset=0) or {}
        dump("browse_new_p1.json", new)

        ids: list[str] = []
        for coll in (trending.get("items") or [], popular.get("games") or []):
            for item in coll:
                gid = item.get("gameId")
                if gid and gid not in ids:
                    ids.append(gid)
        ids = ids[:limit]
        games, _ = self._batched("/api/catalog/games", "games", ids)
        dump("games_batch.json", {"success": True, "games": games})
        counts, _ = self._batched("/api/catalog/live", "counts", ids)
        dump("live_batch.json", {"success": True, "counts": counts})

        brand = None
        for obj in games.values():
            brand, _ = self._creator_fields(obj)
            if brand:
                break
        if brand:
            prof = self._api(f"/api/players/{brand}/profile")
            if prof:
                dump(f"profile_{brand}.json", prof)
        return written
