"""itch.io adapter — https://itch.io (HTML5 / browser games; aggregator, legacy cohort).

itch.io publishes no plays, views, downloads or likes for a game (those are owner-only; `data.json` has no
counters either), so this adapter tracks *placements* on the public browse lists plus the creator's
generative-AI disclosure (filter launched 2024-11-20) and, on the top-rated list only, the rating count.
See docs/SOURCES_LEGACY.md (itch.io) and research/itchio/.

Lists (sources.yaml `lists`), 36 games per page:
  popular      /games/html5
  new_popular  /games/new-and-popular/html5
  top_rated    /games/top-rated/html5          cells carry `.game_rating[data-tooltip="4.89 average rating
                                               from 11,663 total ratings"]` → votes
  top_sellers  /games/top-sellers/html5
  ai_popular   /games/ai-assisted/html5        creator disclosed generative-AI use (any kind)
  ai_code      /games/ai-code/html5            creator disclosed AI-generated code
  no_ai        /games/html5/tag-no-ai          creator tagged the game "No AI"
  newest       /games/newest/html5
Page 1 is the HTML browse page; pages 2+ are `<path>?page=N&format=json` → `{"content": "<cells html>",
"num_items": 36, "page": N}`. Pages per list = ceil(top_n / 36).

fetch() never requests anything: it answers only from the cells cached by discover() in this run (no game
pages, no embeds — robots.txt disallows /embed/ and a game page view is what we would be measuring
anyway). A watchlist ref that was not on any list this run yields **nothing** (it is skipped, not marked
"gone"): itch.io has no batch endpoint and per-game pages would cost 25 s each. Its last snapshot stays the
latest one; staleness is handled by `stale_after_days`.

ai_disclosure: "ai" if the game was on ai_popular or ai_code this run, else "no_ai" if it was on no_ai, else
None (unknown — the pipeline keeps an earlier known value). A game on both an AI list and no_ai (a creator
tag contradicting the disclosure) counts as "ai": the disclosure filter is the authoritative field.

Politeness. itch.io answers HTTP 429 (openresty, no Retry-After) after a few quick requests and needed
20–75 s spacing in research; some 3-/4-segment browse URLs (e.g. /games/newest/ai-assisted/html5) get a
Cloudflare challenge (403), which `Http` raises as `Blocked` — we let it propagate and never retry it.
Run with rate_limit_rps 0.04 (one request per 25 s). `Http` itself retries a 429 three times on its
throttle (≈25 s apart at this rate) and then raises `httpx.HTTPStatusError`; on that the adapter sleeps
`BACKOFF_429_S` (90 s) once, retries the page once, and otherwise gives up on that page (the rest of that
list is skipped). After `MAX_429_GIVEUPS` such give-ups in one run it stops requesting more pages.
Because Http fetches robots.txt unthrottled right before its first request (a pair itch.io answered with
429 in research), the adapter triggers the robots.txt fetch itself and waits one interval before page 1.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from tracker.base import SourceAdapter
from tracker.models import GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

BASE = "https://itch.io"
PAGE_SIZE = 36
BACKOFF_429_S = 90.0
MAX_429_GIVEUPS = 2

LIST_PATHS: dict[str, str] = {
    "popular": "/games/html5",
    "new_popular": "/games/new-and-popular/html5",
    "top_rated": "/games/top-rated/html5",
    "top_sellers": "/games/top-sellers/html5",
    "ai_popular": "/games/ai-assisted/html5",
    "ai_code": "/games/ai-code/html5",
    "no_ai": "/games/html5/tag-no-ai",
    "newest": "/games/newest/html5",
}
AI_LISTS = frozenset({"ai_popular", "ai_code"})
NO_AI_LISTS = frozenset({"no_ai"})
RATED_LISTS = frozenset({"top_rated"})

# catalogue_totals() keys → list whose page-1 header carries "(N results)"
TOTALS: dict[str, str] = {"total": "popular", "ai_total": "ai_popular", "no_ai_total": "no_ai"}

_CELL_START = re.compile(r"<div\b[^>]*?\bclass=\"game_cell\b[^\"]*\"[^>]*>", re.S)
_DIV_TOKEN = re.compile(r"<div\b|</div\s*>", re.I)
_RESULTS = re.compile(r"\(\s*([\d,.\s]+?)\s+results?\s*\)", re.I)
_GAME_COUNT = re.compile(r"<[a-z]+\b[^>]*\bclass=\"game_count\"[^>]*>(.*?)</", re.S | re.I)
_TOOLTIP = re.compile(r"([\d.]+)\s+average rating from\s+([\d,]+)\s+total ratings?", re.I)
_USER_LABEL = re.compile(r"user:(\d+)")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(url: str | None) -> str | None:
    """https://user.itch.io/slug/?x → https://user.itch.io/slug (no query, fragment or trailing slash)."""
    if not url:
        return None
    url = html_lib.unescape(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if not parts.netloc:
        return None
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


# ------------------------------------------------------------------ parsing
def split_cells(html: str) -> list[str]:
    """Raw HTML of every `div.game_cell` (attribute order varies; matched by class), balanced on <div>."""
    out: list[str] = []
    for m in _CELL_START.finditer(html):
        depth, end = 1, len(html)
        for t in _DIV_TOKEN.finditer(html, m.end()):
            depth += -1 if t.group(0).startswith("</") else 1
            if depth == 0:
                end = t.end()
                break
        out.append(html[m.start():end])
    return out


class _CellParser(HTMLParser):
    """Reads one game cell. Elements are recognised by class / data attributes, never by position."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.d: dict = {}
        self._stack: list[str | None] = []  # per open element: the field whose text it captures
        self._text: dict[str, list[str]] = {}
        self._in_author = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "").split()
        field = None
        if tag == "div" and "game_cell" in cls and "game_id" not in self.d:
            self.d["game_id"] = a.get("data-game_id", "").strip() or None
        if tag == "a" and "title" in cls and "game_link" in cls:
            self.d.setdefault("url", a.get("href"))
            field = "title"
        elif tag == "a" and "thumb_link" in cls:
            self.d.setdefault("thumb_href", a.get("href"))
        elif tag == "img" and "thumbnail" not in self.d:
            src = a.get("data-lazy_src") or a.get("src")
            if src and not src.startswith("data:"):
                self.d["thumbnail"] = src
        elif "game_author" in cls:
            self._in_author += 1
            field = "_author_block"
        elif tag == "a" and self._in_author and "author_name" not in self.d:
            m = _USER_LABEL.search(a.get("data-label", ""))
            self.d["author_id"] = m.group(1) if m else None
            self.d["author_url"] = a.get("href")
            field = "author_name"
        elif "game_genre" in cls:
            field = "genre"
        elif "price_value" in cls:
            field = "price"
        elif "game_rating" in cls:
            self.d["rating_tooltip"] = a.get("data-tooltip")
        elif "game_text" in cls:
            field = "text"
        if tag in ("img", "br", "hr", "input", "source", "meta", "link"):
            return  # void elements never get an end tag
        self._stack.append(field)

    def handle_endtag(self, tag):
        if tag in ("img", "br", "hr", "input", "source", "meta", "link") or not self._stack:
            return
        field = self._stack.pop()
        if field == "_author_block":
            self._in_author -= 1

    def handle_data(self, data):
        for field in self._stack:
            if field and not field.startswith("_"):
                self._text.setdefault(field, []).append(data)

    def result(self) -> dict:
        for k, parts in self._text.items():
            v = " ".join("".join(parts).split())
            if v:
                self.d.setdefault(k, v)
        return self.d


def parse_rating_tooltip(text: str | None) -> tuple[float | None, int | None]:
    """"4.89 average rating from 11,663 total ratings" → (4.89, 11663)."""
    if not text:
        return None, None
    m = _TOOLTIP.search(html_lib.unescape(text))
    if not m:
        return None, None
    return float(m.group(1)), int(m.group(2).replace(",", ""))


def parse_cell(cell_html: str) -> dict | None:
    """One game cell → {game_id, url, title, thumbnail, author_id, author_name, author_url, genre, price,
    rating_avg, rating_count}. None when the cell has no id or no game link."""
    p = _CellParser()
    p.feed(cell_html)
    p.close()
    d = p.result()
    d["url"] = _canonical(d.get("url") or d.pop("thumb_href", None))
    d.pop("thumb_href", None)
    d["author_url"] = _canonical(d.get("author_url"))
    d["rating_avg"], d["rating_count"] = parse_rating_tooltip(d.pop("rating_tooltip", None))
    if not d.get("game_id") or not d["url"]:
        return None
    return d


def parse_cells(html: str) -> list[dict]:
    return [c for c in (parse_cell(raw) for raw in split_cells(html)) if c]


def parse_results_count(html: str) -> int | None:
    """The browse header's "(90,742 results)" → 90742. Prefers the `.game_count` element."""
    if not html:
        return None
    for text in [m.group(1) for m in _GAME_COUNT.finditer(html)] + [html]:
        m = _RESULTS.search(html_lib.unescape(text))
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                return int(digits)
    return None


def parse_json_page(text: str) -> tuple[list[dict], int | None]:
    """`?page=N&format=json` body → (cells, num_items)."""
    data = json.loads(text)
    if not isinstance(data, dict):
        return [], None
    n = data.get("num_items")
    return parse_cells(data.get("content") or ""), (int(n) if isinstance(n, int) else None)


def list_url(list_name: str, page: int = 1) -> str:
    path = LIST_PATHS[list_name]
    return BASE + path if page <= 1 else f"{BASE}{path}?page={page}&format=json"


# ------------------------------------------------------------------ polite GET
class _Pager:
    """Wraps Http.get with the adapter's own 429 policy (see module docstring)."""

    def __init__(self, http, sleep: Callable[[float], None] = time.sleep, backoff: float = BACKOFF_429_S):
        self.http = http
        self.sleep = sleep
        self.backoff = backoff
        self.giveups = 0
        self._warmed = False

    def _warm(self) -> None:
        """Http fetches robots.txt outside its throttle, right before the first request, and itch.io answered
        429 to exactly that pair in research. Fetch robots.txt first, then wait one interval."""
        if self._warmed:
            return
        self._warmed = True
        allowed = getattr(self.http, "allowed", None)
        interval = float(getattr(self.http, "min_interval", 0) or 0)
        if callable(allowed) and interval > 0:
            allowed(BASE + "/games/html5")
            self.sleep(interval)

    @property
    def exhausted(self) -> bool:
        return self.giveups >= MAX_429_GIVEUPS

    def get_text(self, url: str) -> str | None:
        """Body text, or None on 404/410 or after giving up on 429. `Blocked` / budget errors propagate."""
        if self.exhausted:
            return None
        self._warm()
        for attempt in (1, 2):
            try:
                resp = self.http.get(url)
            except httpx.HTTPStatusError as e:
                if e.response is None or e.response.status_code != 429:
                    raise
                if attempt == 1:
                    log.warning("itchio: still 429 on %s after Http retries; backing off %.0fs, then one retry", url, self.backoff)
                    self.sleep(self.backoff)
                    continue
                self.giveups += 1
                log.warning("itchio: 429 again on %s; giving up on this page (%d/%d)", url, self.giveups, MAX_429_GIVEUPS)
                return None
            if resp.status_code == 429:  # an Http double that returns instead of raising
                if attempt == 1:
                    log.warning("itchio: 429 on %s; backing off %.0fs, then one retry", url, self.backoff)
                    self.sleep(self.backoff)
                    continue
                self.giveups += 1
                log.warning("itchio: 429 again on %s; giving up on this page (%d/%d)", url, self.giveups, MAX_429_GIVEUPS)
                return None
            if resp.status_code in (404, 410):
                log.warning("itchio: %s → %d", url, resp.status_code)
                return None
            return resp.text
        return None


def catalogue_totals(http, pages: dict[str, str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> dict[str, int | None]:
    """{"total", "ai_total", "no_ai_total"} from the "(N results)" header on page 1 of /games/html5,
    /games/ai-assisted/html5 and /games/html5/tag-no-ai. `pages` maps a page-1 URL to HTML already fetched
    this run (ItchIo.page1_html) so those are not requested again. A value is None when the page could not
    be read (404, 429 give-up) or carried no count. `Blocked` propagates (stop, never bypass)."""
    pages = pages or {}
    pager = _Pager(http, sleep=sleep)
    out: dict[str, int | None] = {}
    for key, list_name in TOTALS.items():
        url = list_url(list_name)
        text = pages.get(url)
        if text is None:
            text = pager.get_text(url)
        out[key] = parse_results_count(text) if text else None
    return out


# ------------------------------------------------------------------ adapter
class ItchIo(SourceAdapter):
    name = "itchio"
    display_name = "itch.io"
    base_url = BASE
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config: dict, *, sleep: Callable[[float], None] = time.sleep):
        super().__init__(http, config)
        self._pager = _Pager(http, sleep=sleep)
        self._cells: dict[str, dict] = {}     # game_id → merged cell (first sighting wins, rating merged in)
        self._seen_in: dict[str, set[str]] = {}
        self.page1_html: dict[str, str] = {}  # page-1 URL → HTML, reused by catalogue_totals()

    # --------------------------------------------------------------- discover
    def _page(self, list_name: str, page: int) -> tuple[list[dict], int | None] | None:
        url = list_url(list_name, page)
        text = self._pager.get_text(url)
        if text is None:
            return None
        if page == 1:
            self.page1_html[url] = text
            cells = parse_cells(text)
            return cells, len(cells)
        try:
            return parse_json_page(text)
        except ValueError:
            log.warning("itchio: %s did not return JSON; stopping list %s", url, list_name)
            return None

    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            if list_name not in LIST_PATHS:
                log.warning("itchio: unknown list %r, skipping", list_name)
                continue
            pages = max(1, math.ceil(top_n / PAGE_SIZE))
            captured = _utcnow()
            seen: set[str] = set()
            pos = 0
            for page in range(1, pages + 1):
                if pos >= top_n:
                    break
                got = self._page(list_name, page)
                if got is None:
                    break
                cells, num_items = got
                new = 0
                for cell in cells:
                    if pos >= top_n:
                        break
                    gid = cell["game_id"]
                    if gid in seen:  # pages shift between requests; keep the first position
                        continue
                    seen.add(gid)
                    new += 1
                    pos += 1
                    self._remember(cell, list_name)
                    yield (
                        GameRef(source=self.name, external_id=gid, url=cell["url"]),
                        ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=captured),
                    )
                if not new or (num_items is not None and num_items < PAGE_SIZE):
                    break  # last page
            if not pos:
                log.warning("itchio: list %r produced no games", list_name)

    def _remember(self, cell: dict, list_name: str) -> None:
        gid = cell["game_id"]
        self._seen_in.setdefault(gid, set()).add(list_name)
        known = self._cells.get(gid)
        if known is None:
            self._cells[gid] = dict(cell)
            known = self._cells[gid]
        else:
            for k, v in cell.items():
                if known.get(k) in (None, "") and v not in (None, ""):
                    known[k] = v
        if list_name in RATED_LISTS and cell.get("rating_count") is not None:
            known["votes"] = cell["rating_count"]
            known["rating_avg"] = cell.get("rating_avg")

    def ai_disclosure(self, game_id: str) -> str | None:
        lists = self._seen_in.get(game_id, set())
        if lists & AI_LISTS:
            return "ai"
        if lists & NO_AI_LISTS:
            return "no_ai"
        return None

    # --------------------------------------------------------------- fetch
    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        """Snapshots from this run's cached list cells only; refs not seen this run are skipped (no request)."""
        now = _utcnow()
        skipped = 0
        for ref in refs:
            cell = self._cells.get(ref.external_id)
            if cell is None:
                skipped += 1
                continue
            yield self._snapshot(ref, cell, now)
        if skipped:
            log.info("itchio: %d watchlist games not on any list this run; skipped (no per-game requests)", skipped)

    def _snapshot(self, ref: GameRef, cell: dict, now: datetime) -> GameSnapshot:
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=cell.get("url") or ref.url,
            title=cell.get("title"),
            thumbnail_url=cell.get("thumbnail"),
            creator_external_id=cell.get("author_id"),
            creator_name=cell.get("author_name"),
            creator_url=cell.get("author_url"),
            votes=cell.get("votes"),  # rating count, top_rated list only
            rating=cell.get("rating_avg") if cell.get("votes") is not None else None,
            tags=[cell["genre"]] if cell.get("genre") else [],
            ai_disclosure=self.ai_disclosure(ref.external_id),
            captured_at=now,
        )

    # --------------------------------------------------------------- totals
    def catalogue_totals(self) -> dict[str, int | None]:
        """Module-level catalogue_totals() reusing this run's page-1 HTML."""
        return catalogue_totals(self.http, self.page1_html, sleep=self._pager.sleep)

    def platform_totals(self) -> dict | None:
        """HTML5 catalogue size and its AI-disclosure split (stored in platform_totals: games + extra)."""
        t = self.catalogue_totals()
        if not any(v is not None for v in t.values()):
            return None
        return {"games": t.get("total"), "extra": {k: v for k, v in t.items() if k != "total" and v is not None}}

    # --------------------------------------------------------------- fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        """Page 1 of every list (trimmed to the results header + cells) and page 2 of `popular` as JSON.
        9 requests ≈ 4 minutes at 0.04 rps. `limit` caps the cells kept per page."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        keep = max(1, int(limit or PAGE_SIZE))
        for list_name in LIST_PATHS:
            text = self._pager.get_text(list_url(list_name))
            if text is None:
                continue
            p = out_dir / f"{list_name}.html"
            p.write_text(trim_html(text, keep))
            written.append(p)
        text = self._pager.get_text(list_url("popular", 2))
        if text:
            data = json.loads(text)
            data["content"] = "".join(split_cells(data.get("content") or "")[:keep])
            p = out_dir / "popular_p2.json"
            p.write_text(json.dumps(data))
            written.append(p)
        return written


def trim_html(html: str, keep: int = PAGE_SIZE) -> str:
    """Keep only what the parser reads: the `.game_count` header and the first `keep` game cells."""
    m = _GAME_COUNT.search(html)
    count = f'<h2><nobr class="game_count">{m.group(1)}</nobr></h2>' if m else ""
    cells = "".join(split_cells(html)[:keep])
    return f'<html><body>{count}<div class="game_grid_widget browse_game_grid">{cells}</div></body></html>\n'
