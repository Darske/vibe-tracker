"""CrazyGames adapter — https://www.crazygames.com (legacy aggregator, ~4,700 launched games).

Everything comes from the site's own keyless JSON API on `https://api.crazygames.com/v4/en_US`
(CORS *, Cloudflare edge-cached for 4 h). No browser, no HTML, and never a game/play page
(`/game/<slug>` is only used as the canonical URL we store). Low-impact by design: 1 request/second,
polled once a day, robots.txt checked by `Http` (api.crazygames.com has none → nothing disallowed;
www.crazygames.com only disallows /u/, /uid/, /search?q, /preview/).

Note: CrazyGames' terms (6.3.G) forbid systematic/automated data collection. The owner decided to
track it anyway; keep the footprint minimal (see docs/SOURCES_LEGACY.md).

Endpoints used (see docs/SOURCES_LEGACY.md and research/crazygames/api/):
  GET /games?sorting=default&label=hot&device=desktop&paginationPage=1&paginationSize=100   → hot (~50)
  GET /games?sorting=default&device=desktop&paginationPage=N&paginationSize=100             → top (site ranking)
  GET /games?sorting=new&device=desktop&paginationPage=N&paginationSize=100                 → new / catalog
      all → {games:{pagination:{page,size}, total, items:[{id, name, slug, cover, status, categoryName,
             releaseYear, totalLikes, totalPlays, gameThumbLabels, …}]}}
      paginationSize > 100 → HTTP 400 "Bad request".
  GET /gamesData?gameIds=<url-encoded JSON array of string ids>                              → {games:[…same row…]}

`new` and `catalog` read the same pages, so pages are memoised per adapter instance: a daily run
is ≈ 1 (hot) + 1 (top) + ⌈total/100⌉ (catalog, shared with new) ≈ 49 requests.

Metric semantics:
  plays_total = totalPlays   all-time game starts, refreshed about daily. New / soft-launch games are
                             masked as exactly 1 by the API → stored as None (unknown), never 1.
  likes       = totalLikes
No players, players-now, comments or creator fields in list rows; `published_at` stays None (rows only
carry `releaseYear`). CrazyGames has no AI tag/field → `ai_disclosure` = None.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from tracker.base import SourceAdapter
from tracker.models import GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API = "https://api.crazygames.com/v4/en_US"
IMG = "https://imgs.crazygames.com"
THUMB_QUERY = "format=auto&quality=85&metadata=none&width=480"
PAGE_SIZE = 100  # API maximum; 101+ → 400
MAX_PAGES = 120  # safety cap (catalogue is ~47 pages)
BATCH_SIZE = 50  # /gamesData ids per request; halved on a 4xx
MASKED_PLAYS = 1  # API value for new / soft-launch games whose count is hidden
GONE_STATUSES = {"UNPUBLISHED", "DELETED", "REMOVED", "DISABLED", "ARCHIVED", "OFFLINE"}

# list name → (sorting, label or None, single page only)
LISTS: dict[str, tuple[str, str | None, bool]] = {
    "hot": ("default", "hot", True),
    "top": ("default", None, False),
    "new": ("new", None, False),
    "catalog": ("new", None, False),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _int_or_none(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def plays_from(row: dict) -> int | None:
    n = _int_or_none(row.get("totalPlays"))
    if n is None or n == MASKED_PLAYS or n < 0:
        return None
    return n


def thumbnail_from(row: dict) -> str | None:
    cover = row.get("cover")
    if not cover:
        covers = row.get("covers")
        if isinstance(covers, dict):
            cover = covers.get("16x9") or covers.get("1x1") or next((v for v in covers.values() if isinstance(v, str)), None)
        elif isinstance(covers, list):
            cover = next((v for v in covers if isinstance(v, str)), None)
    if not isinstance(cover, str) or not cover.strip():
        return None
    cover = cover.strip()
    if cover.startswith(("http://", "https://")):
        return cover
    return f"{IMG}/{cover.lstrip('/')}?{THUMB_QUERY}"


class CrazyGames(SourceAdapter):
    name = "crazygames"
    display_name = "CrazyGames"
    base_url = "https://www.crazygames.com"
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        self._rows: dict[str, dict] = {}  # id -> last row object seen
        self._pages: dict[tuple, dict] = {}  # (sorting, label, page) -> response, memoised per run
        self._totals: dict[tuple, int] = {}

    # --------------------------------------------------------------- helpers
    def url_for(self, slug: str) -> str:
        return f"{self.base_url}/game/{slug}"

    @staticmethod
    def _games_url(sorting: str, label: str | None, page: int) -> str:
        params: list[tuple[str, str]] = [("sorting", sorting)]
        if label:
            params.append(("label", label))
        params += [("device", "desktop"), ("paginationPage", str(page)), ("paginationSize", str(PAGE_SIZE))]
        return f"{API}/games?{urlencode(params)}"

    def _page(self, sorting: str, label: str | None, page: int) -> dict:
        key = (sorting, label, page)
        if key not in self._pages:
            self._pages[key] = self.http.get_json(self._games_url(sorting, label, page)) or {}
        return self._pages[key]

    def _remember(self, row: dict) -> str | None:
        gid = row.get("id")
        if gid is None or gid == "":
            return None
        gid = str(gid)
        if row.get("slug"):
            self._rows[gid] = row
        return gid

    def _iter_list(self, sorting: str, label: str | None, single: bool, top_n: int) -> Iterable[dict]:
        idx = 0
        for page in range(1, MAX_PAGES + 1):
            block = (self._page(sorting, label, page).get("games") or {})
            items = block.get("items") or []
            total = _int_or_none(block.get("total"))
            if total is not None:
                self._totals[(sorting, label)] = total
            for item in items:
                if not isinstance(item, dict):
                    continue
                idx += 1
                yield item
                if idx >= top_n:
                    return
            if single or len(items) < PAGE_SIZE:
                return
            if total is not None and page * PAGE_SIZE >= total:
                return

    def platform_totals(self) -> dict | None:
        """All-time plays summed over the whole catalogue, when this run walked it (the `catalog` list)."""
        total = self._totals.get(("new", None))
        if not total or len(self._rows) < 0.98 * total:
            return None
        plays = [_int_or_none(r.get("totalPlays")) for r in self._rows.values()]
        plays = [p for p in plays if p is not None and p > 1]  # 1 = masked (new / soft-launch games)
        return {"plays_total": sum(plays) if plays else None, "games": total}

    # --------------------------------------------------------------- discover
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        now = _utcnow()
        for list_name in lists:
            spec = LISTS.get(list_name)
            if spec is None:
                log.warning("crazygames: unknown list %r; skipping", list_name)
                continue
            sorting, label, single = spec
            seen: set[str] = set()
            pos = 0
            for row in self._iter_list(sorting, label, single, top_n):
                gid = self._remember(row)
                slug = row.get("slug")
                if not gid or not slug or gid in seen:  # pages can shift while we walk them
                    continue
                seen.add(gid)
                pos += 1
                ref = GameRef(source=self.name, external_id=gid, url=self.url_for(slug))
                yield ref, ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=now)

    # ------------------------------------------------------------------ fetch
    @staticmethod
    def _games_data_url(ids: list[str]) -> str:
        return f"{API}/gamesData?gameIds={quote(json.dumps(ids, separators=(',', ':')), safe='')}"

    def _games_data(self, ids: list[str]) -> tuple[dict[str, dict], set[str]]:
        """Batch-read rows for ids not seen in lists. Returns (rows by id, ids actually answered for)."""
        import httpx

        rows: dict[str, dict] = {}
        asked: set[str] = set()
        queue = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
        while queue:
            chunk = queue.pop(0)
            try:
                data = self.http.get_json(self._games_data_url(chunk))
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500 and len(chunk) > 1:
                    half = len(chunk) // 2
                    log.warning("crazygames: gamesData rejected %d ids (%s); retrying as %d+%d",
                                len(chunk), e.response.status_code, half, len(chunk) - half)
                    queue[:0] = [chunk[:half], chunk[half:]]
                    continue
                log.warning("crazygames: gamesData failed for %s: %s", chunk, e)
                continue
            if data is None:  # 404 for the whole batch: says nothing about individual games
                log.warning("crazygames: gamesData 404 for %d ids; skipping", len(chunk))
                continue
            asked.update(chunk)
            items = data.get("games") if isinstance(data, dict) else None
            if isinstance(items, dict):
                items = items.get("items") or list(items.values())
            for row in items or []:
                if isinstance(row, dict) and row.get("id") is not None:
                    rows[str(row["id"])] = row
        return rows, asked

    def _snapshot(self, ref: GameRef, row: dict, now: datetime) -> GameSnapshot:
        slug = row.get("slug")
        cat = row.get("categoryName")
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=self.url_for(slug) if slug else ref.url,
            title=row.get("name"),
            thumbnail_url=thumbnail_from(row),
            creator_external_id=None,
            creator_name=None,
            creator_url=None,
            plays_total=plays_from(row),
            likes=_int_or_none(row.get("totalLikes")),
            tags=[cat] if isinstance(cat, str) and cat.strip() else [],
            published_at=None,  # rows only carry releaseYear; we don't invent a date
            ai_disclosure=None,  # CrazyGames has no AI signal
            status="ok",
            captured_at=now,
        )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        now = _utcnow()
        missing = [r.external_id for r in refs if r.external_id not in self._rows]
        fetched, asked = self._games_data(list(dict.fromkeys(missing))) if missing else ({}, set())
        for row in fetched.values():
            self._remember(row)
        for ref in refs:
            gid = ref.external_id
            row = self._rows.get(gid) or fetched.get(gid)
            if row is None:
                # An id absent from a successful gamesData answer is not proof of removal; skip it.
                log.info("crazygames: no data for %s (%s); skipping", gid, "absent from gamesData" if gid in asked else "batch failed")
                continue
            status = str(row.get("status") or "").upper()
            if status in GONE_STATUSES:
                yield GameSnapshot(source=self.name, external_id=gid, url=ref.url, status="gone", captured_at=now)
                continue
            yield self._snapshot(ref, row, now)

    # --------------------------------------------------------------- fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def slim(data):
            # Rows carry a `videos` array (~2.4 KB each) the parser never reads; drop it to keep the
            # fixtures small. Everything else is saved exactly as served.
            data = json.loads(json.dumps(data))
            block = data.get("games") if isinstance(data, dict) else None
            items = block.get("items") if isinstance(block, dict) else block
            for row in items or []:
                if isinstance(row, dict):
                    row.pop("videos", None)
            return data

        def save(name: str, data) -> None:
            p = out_dir / name
            p.write_text(json.dumps(slim(data), indent=1, ensure_ascii=False))
            written.append(p)

        hot = self._page("default", "hot", 1)
        save("games_hot_p1.json", hot)
        save("games_default_p1.json", self._page("default", None, 1))
        new1 = self._page("new", None, 1)
        save("games_new_p1.json", new1)
        save("games_new_p2.json", self._page("new", None, 2))
        total = _int_or_none((new1.get("games") or {}).get("total")) or 0
        last = max(1, -(-total // PAGE_SIZE))
        if last > 2:
            save("games_new_last.json", self._page("new", None, last))

        ids = [str(r["id"]) for r in ((hot.get("games") or {}).get("items") or []) if isinstance(r, dict) and r.get("id")]
        ids = ids[: max(1, min(limit, BATCH_SIZE))]
        if ids:
            save("games_data.json", self.http.get_json(self._games_data_url(ids)) or {})
        return written
