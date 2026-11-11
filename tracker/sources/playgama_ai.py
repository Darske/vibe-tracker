"""Playgama AI adapter — https://playgama.ai/play (the vibe-coded sandbox catalog; aggregator segment).

Static Astro HTML, no JavaScript needed, robots.txt has no Disallow (research/playgama_ai/). The
sandbox's JSON API lives on `developer.playgama.com/api`, which that host's robots.txt disallows, so
it is never called: everything comes from the server-rendered markup.

/play (catalog, fetched once per run and cached) — one `<ol class="top-tabs__panel" data-top-panel="<tab>">`
per tab, each with 47 `<li class="top-tabs__item">` cards plus a trailing "Top 100 games" button:

    <a class="top-snippet" href="/play/<id>" data-top-site-id="<id>" data-top-plays24h="960">
      <span class="top-snippet__rank">1</span>
      <img class="top-snippet__cover" src="https://static.playgama.com/p-img/pg/icon/…?width=320">
      <span class="top-snippet__title">Rest your Mind</span>
      <span class="top-snippet__measure">960 played</span>        ← per-tab figure, abbreviated ≥1k ("2.5k played",
      <span class="top-snippet__agent"><svg/>Claude Code</span>      "2k likes", "33m 18s avg", "1k played, 227 likes")

Tabs seen (2026-10-31): plays24h, freshDrops, likesAllTime, playsAllTime, playtime, challenge, claude24h,
codex24h, cursor24h. `data-top-plays24h` is the exact trailing-24h play count on every tab except
`challenge`, where the same attribute carries the all-time count — so it is only read as plays_24h off
the other tabs. The second copy of the panels (`top-sidebar-*`) only holds empty stubs.

/play/<id> (game page, static, 200 for a live game, 404 otherwise) — `<span class="header__played">1,007 played</span>`
is the exact all-time play count (it can lag the catalog by minutes and was seen at 0 for a re-published game
with 2k likes, so the card's 24h count is used as a floor); `data-game-title`, `og:image`. Likes (`data-game-likes`) are empty until
JS calls the API, and no creator or agent is rendered on the game page.

fetch() = cached card figures + one GET /play/<id> per ref for the exact all-time plays (1 r/s; configurable
with `game_pages: false` in sources.yaml, in which case plays_total falls back to the abbreviated
playsAllTime figure where the game is on that tab).

Metric semantics (sources.yaml metric_map):
  plays_total  = all-time plays   sessions of ≥30 s counted by Playgama Bridge (exact from /play/<id>)
  plays_24h    = data-top-plays24h   Playgama's own trailing-24h play count (exact)
  likes        = "N likes" on the likesAllTime/challenge cards; abbreviated ≥1k (2k → 2000); None elsewhere
  ai_tool      = agent chip on the card, slugged like the AI index keys ("Claude Code" → claude-code)
  players_*    = None (not exposed); creators are not exposed anywhere (fetch_creator → None)
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

BASE = "https://playgama.ai"
CATALOG_URL = f"{BASE}/play"
AI_INDEX_URL = "https://playgama.com/ai-game-index/data.json"
KNOWN_TABS = {"plays24h", "freshDrops", "likesAllTime", "playsAllTime", "playtime", "challenge", "claude24h", "codex24h", "cursor24h"}

_PANEL_RE = re.compile(r'<ol\b[^>]*\bdata-top-panel="([^"]+)"[^>]*>(.*?)</ol>', re.S)
_ITEM_RE = re.compile(r'<li\b[^>]*class="top-tabs__item"[^>]*>(.*?)</li>', re.S)
_HREF_RE = re.compile(r'<a\b[^>]*class="top-snippet"[^>]*\bhref="/play/([^"/?#]+)"')
_SITE_ID_RE = re.compile(r'data-top-site-id="([^"]+)"')
_PLAYS24H_RE = re.compile(r'data-top-plays24h="(-?\d+)"')
_RANK_RE = re.compile(r'class="top-snippet__rank">\s*(\d+)\s*<')
_COVER_RE = re.compile(r'<img\b[^>]*class="top-snippet__cover"[^>]*\bsrc="([^"]+)"')
_TITLE_RE = re.compile(r'class="top-snippet__title">(.*?)</span>', re.S)
_MEASURE_RE = re.compile(r'class="top-snippet__measure">(.*?)</span>', re.S)
_AGENT_RE = re.compile(r'class="top-snippet__agent">(.*?)</span>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM]?)")
_HEADER_PLAYED_RE = re.compile(r'class="header__played"[^>]*>\s*([\d,.]+\s*[kKmM]?)\s*played', re.S)
_GAME_TITLE_RE = re.compile(r'data-game-title="([^"]*)"')
_GAME_ID_RE = re.compile(r'data-game-id="([^"]+)"')
_OG_IMAGE_RE = re.compile(r'<meta\b[^>]*property="og:image"[^>]*content="([^"]+)"')
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.S)

# agent chip label → slug, aligned with https://playgama.com/ai-game-index/data.json `agent.categories[].key`
AGENT_SLUGS = {
    "claude code": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "google antigravity": "antigravity",
    "antigravity": "antigravity",
    "opencode": "opencode",
    "gemini cli": "gemini-cli",
    "grok build": "grok-build",
    "workbuddy": "workbuddy",
    "zcode": "zcode",
    "claude app": "claude-app",
    "claude app (chat / desktop)": "claude-app",
    "chatgpt": "chatgpt",
    "chatgpt (chat)": "chatgpt",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _text(fragment: str) -> str:
    return htmllib.unescape(_TAG_RE.sub("", fragment)).strip()


def parse_number(text: str | None) -> int | None:
    """'960' → 960, '1,007' → 1007, '2.5k' → 2500, '1m' → 1000000. None for anything else.
    Abbreviated values are the platform's rounding (≈2 significant digits), not exact."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw, suffix = m.group(1).replace(",", ""), m.group(2).lower()
    try:
        val = float(raw)
    except ValueError:
        return None
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return int(round(val))


def parse_duration(text: str) -> int | None:
    """'33m 18s' → 1998 seconds; '1h 2m' → 3720. None if no unit matched."""
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+)\s*([hms])", text):
        found = True
        total += int(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


def parse_measure(text: str | None) -> dict:
    """Split a card's measure text into {plays, likes, playtime_sec}. '1k played, 227 likes' → both."""
    out: dict = {}
    if not text:
        return out
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if "like" in part:
            out["likes"] = parse_number(part)
        elif "play" in part:
            out["plays"] = parse_number(part)
        elif part.endswith("avg") or re.search(r"\d\s*[hms]\b", part):
            d = parse_duration(part)
            if d is not None:
                out["playtime_sec"] = d
    return out


def agent_slug(label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().lower()
    if key in AGENT_SLUGS:
        return AGENT_SLUGS[key]
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or None


def parse_catalog(page_html: str) -> dict[str, list[dict]]:
    """tab → ordered list of card dicts
    {id, rank, title, thumbnail_url, plays24h, measure, agent_label, agent, plays, likes, playtime_sec}."""
    tabs: dict[str, list[dict]] = {}
    for m in _PANEL_RE.finditer(page_html):
        tab, body = m.group(1), m.group(2)
        cards: list[dict] = []
        for item in _ITEM_RE.findall(body):
            href = _HREF_RE.search(item)
            sid = _SITE_ID_RE.search(item)
            gid = (href.group(1) if href else None) or (sid.group(1) if sid else None)
            if not gid:
                continue  # the "Top 100 games" button / loading stubs
            rank = _RANK_RE.search(item)
            cover = _COVER_RE.search(item)
            title = _TITLE_RE.search(item)
            measure = _MEASURE_RE.search(item)
            agent = _AGENT_RE.search(item)
            p24 = _PLAYS24H_RE.search(item)
            card = {
                "id": gid,
                "rank": int(rank.group(1)) if rank else len(cards) + 1,
                "title": (_text(title.group(1)) or None) if title else None,
                "thumbnail_url": htmllib.unescape(cover.group(1)) if cover else None,
                # on the `challenge` tab the attribute holds the all-time count, not a 24h figure
                "plays24h": int(p24.group(1)) if p24 and tab != "challenge" else None,
                "measure": _text(measure.group(1)) if measure else None,
                "agent_label": (_text(agent.group(1)) or None) if agent else None,
            }
            card["agent"] = agent_slug(card["agent_label"])
            card.update(parse_measure(card["measure"]))
            cards.append(card)
        if cards and (tab not in tabs or len(cards) > len(tabs[tab])):
            tabs[tab] = cards  # the sidebar copy of a panel holds only stubs; keep the populated one
    return tabs


def parse_game_page(page_html: str) -> dict | None:
    """{id, title, thumbnail_url, plays_total} from a /play/<id> page, or None if it is not a game page."""
    gid = _GAME_ID_RE.search(page_html)
    played = _HEADER_PLAYED_RE.search(page_html)
    title = _GAME_TITLE_RE.search(page_html)
    if not gid and not played and not title:
        return None
    if not title:
        t = _TITLE_TAG_RE.search(page_html)
        title_txt = re.sub(r"\s+—\s+Playgama\s*$", "", _text(t.group(1))) if t else None
    else:
        title_txt = htmllib.unescape(title.group(1)).strip() or None
    og = _OG_IMAGE_RE.search(page_html)
    return {
        "id": gid.group(1) if gid else None,
        "title": title_txt,
        "thumbnail_url": htmllib.unescape(og.group(1)) if og else None,
        "plays_total": parse_number(played.group(1)) if played else None,
    }


def trim_catalog_html(page_html: str, keep_tabs: Iterable[str] | None = None) -> str:
    """Fixture trim: keep <title> and the populated panels of `keep_tabs` (all tabs when None), with the
    SVG icons, Play buttons and presentational attributes removed. The parser reads the result unchanged."""
    title = _TITLE_TAG_RE.search(page_html)
    keep = set(keep_tabs) if keep_tabs is not None else None
    panels = []
    for m in _PANEL_RE.finditer(page_html):
        if "top-snippet__measure" not in m.group(2) or (keep is not None and m.group(1) not in keep):
            continue
        s = re.sub(r"<svg\b.*?</svg>", "", m.group(0), flags=re.S)
        s = re.sub(r'<span class="btn[^"]*top-snippet__play">.*?</span>', "", s, flags=re.S)
        s = re.sub(r'\s(?:target|rel|width|height|alt|loading|decoding|data-cover-tilt|data-top-cover|data-top-copy|data-top-stats)(?:="[^"]*")?(?=[\s>])', "", s)
        panels.append(s)
    head = f'<head><meta charset="utf-8"><title>{title.group(1) if title else "Playgama.ai"}</title></head>'
    return f"<!DOCTYPE html><html>{head}<body><section data-top-games><div data-top-tabs>{''.join(panels)}</div></section></body></html>"


def trim_game_html(page_html: str) -> str:
    """Fixture trim for /play/<id>: <title>, og:image, the data-game-id / data-game-title tags and header__played."""
    head = [m.group(0) + (">" if not m.group(0).endswith(">") else "") for rx in (_TITLE_TAG_RE, _OG_IMAGE_RE) if (m := rx.search(page_html))]
    body = []
    for rx in (r'<[^>]*\bdata-game-id="[^"]+"[^>]*>', r'<[^>]*\bdata-game-title="[^"]*"[^>]*>'):
        m = re.search(rx, page_html)
        if m:
            tag = m.group(0)
            name = re.match(r"<(\w+)", tag).group(1)
            body.append(tag + f"</{name}>")
    m = re.search(r'<span class="header__played"[^>]*>.*?</span>', page_html, re.S)
    if m:
        body.append(m.group(0))
    return "<!DOCTYPE html><html><head>" + "".join(head) + "</head><body>" + "".join(body) + "</body></html>"


class PlaygamaAI(SourceAdapter):
    name = "playgama_ai"
    display_name = "Playgama AI"
    base_url = BASE
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        self._tabs: dict[str, list[dict]] | None = None  # parsed /play, once per run
        self._cards: dict[str, dict] = {}  # id → merged card figures across tabs
        self.game_pages = bool((config or {}).get("game_pages", True))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def game_url(gid: str) -> str:
        return f"{BASE}/play/{quote(gid, safe='')}"

    def _ref(self, gid: str) -> GameRef:
        return GameRef(source=self.name, external_id=gid, url=self.game_url(gid))

    def _catalog(self) -> dict[str, list[dict]]:
        if self._tabs is None:
            r = self.http.get(CATALOG_URL)
            self._tabs = parse_catalog(r.text) if r.status_code != 404 else {}
            for tab, cards in self._tabs.items():
                for c in cards:
                    self._remember(tab, c)
        return self._tabs

    def _remember(self, tab: str, card: dict) -> None:
        cur = self._cards.setdefault(card["id"], {"id": card["id"], "tabs": []})
        cur["tabs"].append(tab)
        for k in ("title", "thumbnail_url", "agent", "agent_label"):
            if card.get(k) and not cur.get(k):
                cur[k] = card[k]
        if card.get("plays24h") is not None and cur.get("plays24h") is None:
            cur["plays24h"] = card["plays24h"]
        if tab == "playsAllTime" and card.get("plays") is not None:
            cur["plays_all_time_approx"] = card["plays"]
        if card.get("likes") is not None and cur.get("likes") is None:
            cur["likes"] = card["likes"]
        if card.get("playtime_sec") is not None and cur.get("playtime_sec") is None:
            cur["playtime_sec"] = card["playtime_sec"]

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        tabs = self._catalog()
        for list_name in lists:
            cards = tabs.get(list_name)
            if not cards:
                if list_name not in KNOWN_TABS:
                    log.warning("playgama_ai: unknown list %r, skipping", list_name)
                else:
                    log.warning("playgama_ai: tab %r has no cards in the catalog HTML", list_name)
                continue
            captured = _utcnow()
            seen: set[str] = set()
            pos = 0
            for card in cards:
                if pos >= top_n:
                    break
                gid = card["id"]
                if gid in seen:
                    continue
                seen.add(gid)
                pos += 1
                yield self._ref(gid), ListPlacement(
                    source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=captured
                )

    def platform_totals(self) -> dict | None:
        """playgama.com/ai-game-index/data.json (public, robots-allowed): all-time launches and games across the
        whole vibe-coded catalogue, plus each AI tool's share of games (the index's own breakdown)."""
        data = self.http.get_json(AI_INDEX_URL) or {}
        tot = data.get("totals") or {}
        if tot.get("launches") is None and tot.get("games") is None:
            return None
        agents = {c.get("key"): c.get("share") for c in ((data.get("agent") or {}).get("categories") or []) if c.get("key")}
        return {"plays_total": tot.get("launches"), "games": tot.get("games"),
                "extra": {"agent_share_pct": agents, "as_of": tot.get("asOf")}}

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        for ref in refs:
            gid = ref.external_id
            card = self._cards.get(gid)
            page = None
            if self.game_pages or card is None:
                r = self.http.get(self.game_url(gid))
                if r.status_code in (404, 410):
                    yield GameSnapshot(source=self.name, external_id=gid, url=ref.url, status="gone", captured_at=_utcnow())
                    continue
                page = parse_game_page(r.text)
                if page is None:
                    log.warning("playgama_ai: %s returned a page without game markup; counters from the catalog only", gid)
            yield self._snapshot(gid, ref.url, card or {}, page or {})

    def _snapshot(self, gid: str, url: str, card: dict, page: dict) -> GameSnapshot:
        plays_total = page.get("plays_total")
        if plays_total is None:
            plays_total = card.get("plays_all_time_approx")  # abbreviated card figure, only if on playsAllTime
        plays_24h = card.get("plays24h")
        if plays_total is not None and plays_24h is not None and plays_total < plays_24h:
            # Seen live: the page counter lags the catalog by minutes, or resets to 0 when a sandbox game is
            # re-published while the card still shows the old total. All-time can never be below the 24h
            # figure, so use the 24h count as the floor instead of recording an impossible value.
            log.debug("playgama_ai: %s page count %s < 24h count %s; using the 24h count as floor", gid, plays_total, plays_24h)
            plays_total = plays_24h
        return GameSnapshot(
            source=self.name,
            external_id=gid,
            url=url or self.game_url(gid),
            title=page.get("title") or card.get("title"),
            thumbnail_url=card.get("thumbnail_url") or page.get("thumbnail_url"),
            creator_external_id=None,  # the sandbox shows no creator anywhere
            creator_name=None,
            creator_url=None,
            plays_total=plays_total,
            plays_24h=plays_24h,  # Playgama's own trailing-24h play count
            players_now=None,
            players_total=None,
            likes=card.get("likes"),
            ai_tool=card.get("agent"),
            engine=None,
            tags=[],
            published_at=None,  # not rendered; the freshDrops tab order is the only recency signal
            status="ok",
            captured_at=_utcnow(),
        )

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        return None  # playgama.ai renders no creator profiles

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, text: str) -> None:
            p = out_dir / name
            p.write_text(text, encoding="utf-8")
            written.append(p)

        r = self.http.get(f"{BASE}/robots.txt")
        save("robots.txt", r.text if r.status_code != 404 else "")
        r = self.http.get(CATALOG_URL)
        page_html = r.text
        keep = set(self.config.get("lists") or []) | {"playtime"}
        save("play.html", trim_catalog_html(page_html, keep) if len(page_html) > 200 * 1024 else page_html)
        tabs = parse_catalog(page_html)
        first = next((c for cards in tabs.values() for c in cards), None)
        if first:
            gr = self.http.get(self.game_url(first["id"]))
            if gr.status_code != 404:
                save(f"play_{first['id']}.html", trim_game_html(gr.text) if len(gr.text) > 200 * 1024 else gr.text)
        idx = self.http.get(AI_INDEX_URL)
        if idx.status_code != 404:
            try:
                save("ai_game_index.json", json.dumps(idx.json(), indent=1))
            except ValueError:
                save("ai_game_index.json", idx.text)
        return written
