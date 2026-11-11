"""Kongregate adapter — https://www.kongregate.com (legacy aggregator).

Kongregate reopened to uploads in 2026; most new games are tiny, the big numbers are old catalogue games.
Everything here is plain HTML/JSON over `self.http` (1 req/s, robots.txt enforced). No browser, and never the
embed/play iframe: robots.txt disallows `/accounts/`, `/developer/`, `/games/*/comments`, `/games/*/embed`,
`/games/*/rating?` and `*.rss`, and `_guard` refuses those paths before `Http` is even asked.

Lists (sources.yaml `lists`, 50 cards per page, `?page=N`):
  hot                       → /en/hot-new-games                         (49 cards, one page)
  new                       → /en/new-and-updated-games?sort=newest     (50 cards ≈ 7 days, one page)
  <category>_<sort>         → /en/<category>-games?sort=<sort>&page=N   (sort = top | plays | newest)
  (<sort>_<category> is accepted too: `top_action` == `action_top`)
  categories: action, puzzle, adventure-rpg, sports-racing, strategy-defense, more, multiplayer, shooter, idle

Every list page carries two copies of the same 50 games, in the same order:
  1. the `game-tile` cards: href `/en/games/<Dev>/<slug>`, badge (New / Updated / Hot / Top), thumbnail,
     title, rounded plays ("7.7M plays") and rating ("★ 4.6", absent while a game is "under judgement");
  2. a JSON-LD `ItemList` whose items are full `VideoGame` objects: name, url, description (full text),
     datePublished, genre, image, author.name, aggregateRating.ratingCount.
So the AI declaration (which lives in the description) is read for every listed game at no extra cost.

fetch(refs):
  GET /games/<dev>/<slug>/metrics.json (lowercase; not cached by the CDN; a plain GET adds no play) →
      gameplays_count (exact), favorites_count, rating, average_rating_with_count
      "(Avg: 4.61 of 12,151 ratings)" → 12151, "(under judgement)" → None. 404/410 → status "gone".
  Game page /en/games/<dev>/<slug> (JSON-LD VideoGame) only for refs with no list JSON-LD this run (watchlist
  games that dropped off every list), rotated so each such game is re-read about once per
  `game_page_every_hours` (default 168), and at most `max_game_pages_per_run` (default 60) per run.

Metric semantics (metric_map in sources.yaml):
  plays_total = gameplays_count   all-time game starts as Kongregate counts them (exact)
  likes       = favorites_count   users who favourited the game
  votes       = number of star ratings (None while the game is "under judgement")
  ai_disclosure = classify_ai(description): the game's own AI declaration ("ai" / "no_ai" / None)
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import zlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from tracker.base import SourceAdapter
from tracker.models import GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

BASE = "https://www.kongregate.com"
PAGE_SIZE = 50
CATEGORIES = (
    "action", "puzzle", "adventure-rpg", "sports-racing", "strategy-defense",
    "more", "multiplayer", "shooter", "idle",
)
SORTS = ("top", "plays", "newest")
SPECIAL_LISTS = {
    "hot": "/en/hot-new-games",
    "new": "/en/new-and-updated-games?sort=newest",
}

# robots.txt-disallowed paths (Http enforces robots.txt too; this is a second, adapter-local fence)
_FORBIDDEN = re.compile(
    r"^(?:/[a-z]{2})?/(?:accounts|developer)(?:/|$)|^/games/[^?]*/(?:comments|embed|sticker_drop|unpublish|flags)"
    r"|^/games/[^?]*/rating\?|\.rss$|^/forums",
    re.I,
)

_LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_GAME_PATH_RE = re.compile(r"^/(?:en/)?games/([^/?#\"]+)/([^/?#\"]+)/?$", re.I)
_TILE_SPLIT = 'class="game-tile '
_TILE_HREF = re.compile(r'<a\s+href="(/en/games/[^"/]+/[^"/?#]+)"', re.I)
_TILE_BADGE = re.compile(r"<span>[^<]*</span>\s*<span>\s*([A-Za-z][A-Za-z ]*?)\s*</span>")
_TILE_IMG = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.S | re.I)
_TILE_ALT = re.compile(r'<img\b[^>]*?\balt="([^"]*)"', re.S | re.I)
_TILE_TITLE = re.compile(r'font-semibold truncate[^"]*">\s*(.*?)\s*</div>', re.S)
_TILE_RATING = re.compile(r"★</span>\s*<span>\s*([\d.]+)\s*</span>")
_TILE_PLAYS = re.compile(r"([\d][\d.,]*)\s*([KMB])?\s+plays?\b", re.I)
_RATING_COUNT = re.compile(r"of\s+([\d,]+)\s+ratings?", re.I)


# ---------------------------------------------------------------------------- small helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int_or_none(value) -> int | None:
    """Counters: None stays None; never coerce a missing value to 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        return int(s) if s.isdigit() else None
    return None


def approx_count(num: str, suffix: str | None) -> int | None:
    """'7.7', 'M' → 7_700_000 (card play counts are rounded; used only for logging/tests, never stored)."""
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((suffix or "").lower(), 1)
    return int(round(v * mult))


def _clean(text: str | None, unescape: bool = True) -> str | None:
    """Unescape HTML entities and collapse whitespace ("snake  predator " → "snake predator")."""
    if not isinstance(text, str):
        return None
    text = html_lib.unescape(text) if unescape else text
    return re.sub(r"\s+", " ", text).strip() or None


def slugify(text: str) -> str:
    """'Adventure & RPG' → 'adventure-rpg' (matches Kongregate's category slugs)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("&", " ")).strip("-")
    return re.sub(r"-{2,}", "-", s)


def game_id_from_path(path: str) -> tuple[str, str, str] | None:
    """'/en/games/JamesG466/incremancer' or a full URL → ('jamesg466/incremancer', 'JamesG466', 'incremancer')."""
    if path.startswith("http"):
        path = urlsplit(path).path
    m = _GAME_PATH_RE.match(path)
    if not m:
        return None
    dev, slug = html_lib.unescape(m.group(1)), html_lib.unescape(m.group(2))
    return f"{dev}/{slug}".lower(), dev, slug.lower()


def game_url(external_id: str) -> str:
    return f"{BASE}/en/games/{external_id.lower()}"


def metrics_url(external_id: str) -> str:
    return f"{BASE}/games/{external_id.lower()}/metrics.json"


def list_path(list_name: str, page: int = 1) -> str | None:
    """List name → path for that page, or None if the name is not understood."""
    if list_name in SPECIAL_LISTS:
        return SPECIAL_LISTS[list_name] if page == 1 else None
    parsed = parse_list_name(list_name)
    if parsed is None:
        return None
    category, sort = parsed
    path = f"/en/{category}-games?sort={sort}"
    return path if page == 1 else f"{path}&page={page}"


def parse_list_name(list_name: str) -> tuple[str, str] | None:
    """'action_top' / 'top_action' → ('action', 'top'). Unknown categories are allowed (logged) if slug-like."""
    for sort in SORTS:
        for category in (list_name.removesuffix(f"_{sort}") if list_name.endswith(f"_{sort}") else None,
                         list_name.removeprefix(f"{sort}_") if list_name.startswith(f"{sort}_") else None):
            if category and re.fullmatch(r"[a-z0-9][a-z0-9-]*", category):
                if category not in CATEGORIES:
                    log.warning("kongregate: list %r uses unknown category %r; trying /en/%s-games", list_name, category, category)
                return category, sort
    return None


# ---------------------------------------------------------------------------- page parsers
def parse_ld_json(html: str) -> list[dict]:
    """All JSON-LD nodes on a page (top-level objects and `@graph` members)."""
    out: list[dict] = []
    for m in _LDJSON_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                out.extend(n for n in graph if isinstance(n, dict))
            else:
                out.append(node)
    return out


def _is_video_game(node: dict) -> bool:
    t = node.get("@type")
    return "VideoGame" in (t if isinstance(t, list) else [t])


def ld_item_list(html: str) -> dict[str, dict]:
    """List page JSON-LD ItemList → {external_id: VideoGame object}."""
    out: dict[str, dict] = {}
    for node in parse_ld_json(html):
        if node.get("@type") != "ItemList":
            continue
        for el in node.get("itemListElement") or []:
            item = el.get("item") if isinstance(el, dict) else None
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                ids = game_id_from_path(item["url"])
                if ids:
                    out.setdefault(ids[0], item)
    return out


def ld_video_game(html: str) -> dict | None:
    """Game page JSON-LD → the VideoGame node (None if absent)."""
    for node in parse_ld_json(html):
        if _is_video_game(node):
            return node
    return None


def ld_fields(ld: dict | None) -> dict:
    """VideoGame JSON-LD → the fields we map: title, description, published_at, author, genre, image, rating_count."""
    if not isinstance(ld, dict):
        return {}
    author = ld.get("author") if isinstance(ld.get("author"), dict) else {}
    rating = ld.get("aggregateRating") if isinstance(ld.get("aggregateRating"), dict) else {}
    image = ld.get("image")
    if isinstance(image, dict):
        image = image.get("url")
    return {
        "title": _clean(ld.get("name"), unescape=False) if isinstance(ld.get("name"), str) else None,
        "description": ld.get("description") if isinstance(ld.get("description"), str) else None,
        "published_at": _parse_dt(ld.get("datePublished")),
        "author": author.get("name") if isinstance(author.get("name"), str) and author.get("name") else None,
        "genre": ld.get("genre") if isinstance(ld.get("genre"), str) and ld.get("genre") else None,
        "image": image if isinstance(image, str) and image else None,
        "rating_count": _int_or_none(rating.get("ratingCount")),
    }


def parse_cards(html: str) -> list[dict]:
    """List page → cards in page order: {external_id, dev, slug, title, thumbnail, badge, plays_approx, rating, ld}."""
    ld = ld_item_list(html)
    cards: list[dict] = []
    seen: set[str] = set()
    for tile in html.split(_TILE_SPLIT)[1:]:
        href = _TILE_HREF.search(tile)
        if not href:
            continue
        ids = game_id_from_path(href.group(1))
        if not ids or ids[0] in seen:
            continue
        seen.add(ids[0])
        external_id, dev, slug = ids
        head = tile[: href.start()]  # the badge div sits before the link
        badge = _TILE_BADGE.search(head)
        body = tile[href.start():]
        img = _TILE_IMG.search(body)
        alt = _TILE_ALT.search(body)
        title = _TILE_TITLE.search(body)
        rating = _TILE_RATING.search(body)
        plays = _TILE_PLAYS.search(body)
        title_text = _clean(title.group(1)) if title else None
        if not title_text and alt:
            title_text = _clean(alt.group(1))
        cards.append({
            "external_id": external_id,
            "dev": dev,
            "slug": slug,
            "title": title_text,
            "thumbnail": html_lib.unescape(img.group(1)) if img else None,
            "badge": badge.group(1) if badge else None,
            "plays_approx": approx_count(plays.group(1), plays.group(2)) if plays else None,
            "rating": float(rating.group(1)) if rating else None,
            "ld": ld.get(external_id),
        })
    return cards


def parse_metrics(data) -> dict | None:
    """metrics.json → {plays, favorites, rating, rating_count}. None if the payload is not a metrics object."""
    if not isinstance(data, dict) or "gameplays_count" not in data:
        return None
    text = data.get("average_rating_with_count")
    text = re.sub(r"<[^>]+>", "", html_lib.unescape(text)) if isinstance(text, str) else ""
    m = _RATING_COUNT.search(text)
    try:
        rating = float(data["rating"]) if data.get("rating") not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "plays": _int_or_none(data.get("gameplays_count")),
        "favorites": _int_or_none(data.get("favorites_count")),
        "rating": rating,
        # "(under judgement)" (too few ratings for a public average) carries no count → None
        "rating_count": _int_or_none(m.group(1)) if m else None,
    }


# ---------------------------------------------------------------------------- AI declaration classifier
# The game's own words, from the description. Clause by clause: a clause that says "no AI" counts as a
# No and its Yes-looking words are ignored ("AI Usage: 0% Generative AI tools were used"); statements about
# what happens during gameplay ("No generative AI is used during gameplay") and game-AI phrases ("enemy AI",
# "play against the AI") are ignored. Any Yes clause → "ai" (a partial declaration such as "code written with
# Claude; no AI-generated artwork" is still AI-assisted); else any No clause → "no_ai"; else None.
_AI = r"(?:generative\s+)?(?:ai|artificial\s+intelligence|llms?)"
_GENAI_TOOL = (
    r"(?:chat\s?gpt|gpt-?\d[\w.]*|openai|anthropic'?s?\s+claude|claude\s+(?:by\s+anthropic|code|opus|sonnet|\d)"
    r"|gemini(?:\s+\d[\w.]*)?|github\s+copilot|copilot|midjourney|stable\s+diffusion|dall-?e|suno|cursor\s+ai|veltosai)"
)

_GAMEPLAY_SCOPE = re.compile(
    r"\b(?:during|in)\s+(?:the\s+)?(?:gameplay|game\s?play|runtime|run-time)\b|\bat\s+run-?time\b"
    r"|\bai\s+(?:systems?|content)\s+(?:runs?|is\s+generated)\b|kongregate'?s\s+ai|\bfaq\b|moderation\s+api",
)
_GAME_AI = re.compile(
    r"\b(?:enemy|enemies|smart|smarter|tough|clever|adaptive|advanced|challenging|computer|cpu|bot|rival|opponent)\s+ai\b"
    r"|\bai\s+(?:opponents?|enemies|enemy|players?|bots?|cars?|drivers?|racers?|commanders?|difficulty|teams?|"
    r"controlled|characters?|units?|rivals?|squads?|troops?|soldiers?|behaviou?r|pathfinding|mode|partners?|"
    r"companions?|allies|ally|powered|opponent)\b"
    r"|\b(?:against|versus|vs\.?|beat|outsmart|outwit|fight)\s+(?:the\s+|an?\s+)?(?:computer\s+|cpu\s+)?ai\b(?![- ]*(?:tools?|assist|help|generat))",
)

_NO_PATTERNS = [re.compile(p) for p in (
    rf"\b(?:does|did|do)\s*(?:not|n't)\s+(?:use|contain|include|utili[sz]e|feature)\s+(?:any\s+)?(?:input\s+of\s+|use\s+of\s+)?(?:any\s+)?{_AI}",
    rf"\bno\s+{_AI}(?:\s+(?:tools?|assets?|art(?:work)?|content|assistance|generation))?\s+(?:was|were|has\s+been|have\s+been|is|are)\s+(?:used|utili[sz]ed|involved|employed)",
    rf"\bno\s+(?:use\s+of|usage\s+of)\s+{_AI}",
    rf"\b{_AI}\s+(?:tools?\s+)?(?:was|were|has|have|is|are)\s+(?:not|never)\s+(?:been\s+)?(?:used|utili[sz]ed|involved)",
    r"\b(?:ai|generative\s+ai)\s+(?:usage|use|used)\s*:?\s*(?:0\s*%|(?:none|no)\b)",
    r"^\s*(?:generative\s+)?ai\s*:\s*(?:none|no)\s*$",
    r"\b0\s*%\s*(?:generative\s+)?ai\b",
    r"\bwithout\s+(?:the\s+)?(?:use\s+of\s+|help\s+of\s+|any\s+)?(?:generative\s+)?(?:ai|artificial\s+intelligence)\b",
    r"\b100\s*%\s*human(?:[- ](?:made|created|crafted|written|drawn))?\b",
    r"\bno\s+(?:generative\s+)?ai(?:[- ](?:generated|made|created))?\s+(?:assets?|art(?:work)?|graphics|content|code|music|tools?|involved)\b",
    rf"\bnot\s+(?:made|created|developed|built|generated|written)\s+(?:with|using|by)\s+(?:the\s+)?(?:help\s+|assistance\s+|use\s+)?(?:of\s+|from\s+)?(?:any\s+)?{_AI}",
    r"\b(?:ai|a\.i\.)[- ]free\b",
    r"\bzero\s+(?:generative\s+)?ai\b",
)]

_YES_PATTERNS = [re.compile(p) for p in (
    # "made with (some) (assistance from) AI", "created with the help of AI", "written by an AI coding assistant"
    rf"\b(?:made|created|developed|built|written|generated|produced|designed|coded|programmed|drawn|composed|"
    rf"assisted|enhanced|updated|edited|crafted|done|translated|localized)\s+(?:\w+\s+){{0,3}}?(?:with|by|using|via|through|from)\s+"
    rf"(?:the\s+)?(?:some\s+|partial\s+|limited\s+|heavy\s+)?(?:help|assistance|aid|support|use)?\s*(?:of|from)?\s*(?:an?\s+|some\s+)?{_AI}\b",
    # "AI tools", "AI-assisted", "AI generated", "AI image generation", "AI code"
    r"\bai[- ](?:tools?|assistance|assisted|generated|generation|coding|code|art(?:work)?|images?|image[- ]generation|"
    r"assets?|music|voices?|helped|workflows?|upscaled|updated|enhanced)\b",
    # "AI was used ...", "Generative AI has been used for its code", "AI is used to ..."
    rf"\b{_AI}\s+(?:tools?\s+)?(?:was|were|has\s+been|have\s+been|is|are|got)\s+(?:only\s+|partially\s+|also\s+|solely\s+|"
    rf"mainly\s+|heavily\s+|extensively\s+|primarily\s+|lightly\s+)?(?:used|utili[sz]ed|employed|involved|leveraged)\b",
    # "using AI tools", "used AI in the creation", "use AI to create", "used Artificial Intelligence as a development assistant"
    rf"\b(?:used|using|use|uses|utili[sz]ed|utili[sz]ing|leveraged)\s+(?:of\s+)?(?:some\s+)?{_AI}\s+"
    rf"(?:tools?|to\s+(?:create|generate|make|help|assist|write|code|design|produce|build|develop|draw)|for\b|"
    rf"in\s+(?:the\s+)?(?:creation|development|making|production|code)|as\s+(?:an?\s+)?(?:\w+\s+)?(?:assistant|tool|helper))",
    # "AI used for the artwork", "AI used in code"
    rf"\b{_AI}\s+(?:used|utili[sz]ed)\s+(?:for|in|to|as)\b",
    # "AI tools assisted with the code", "AI helped", "Google AI assisted"
    rf"\b{_AI}\s+(?:tools?\s+|assistants?\s+)?(?:assisted|helped|wrote|generated|created|composed|drew)\b",
    # "with the help of AI", "with AI assistance", "assisted by AI", "AI-assisted"
    rf"\bwith\s+(?:the\s+)?(?:help|assistance|aid|support)\s+(?:of|from)\s+(?:an?\s+)?{_AI}",
    rf"\bwith\s+(?:\w+\s+)?{_AI}\s+(?:assistance|help|support)\b",
    rf"\bassisted\s+by\s+(?:an?\s+)?{_AI}",
    rf"\bassistance\s+(?:from|of|by)\s+(?:an?\s+)?{_AI}",
    rf"\b(?:built|made|created|developed)\s+with\s+{_AI}\b",
    # "include(s) AI code" / "AI usage: yes"
    rf"\b{_AI}\s+(?:usage|use|used|disclosure)\s*:?\s*(?:yes|partial|some)\b",
    # named generative tools next to a creation verb
    rf"\b{_GENAI_TOOL}\b.{{0,60}}\b(?:used|generated?|created|made|written|wrote|assisted|helped|coded)\b",
    rf"\b(?:made|created|generated|written|coded|built|developed|produced|drawn|assisted|composed)\s+(?:\w+\s+){{0,3}}?(?:with|by|using|in|via)\s+(?:the\s+help\s+of\s+)?{_GENAI_TOOL}\b",
)]
# clause-level combination: an AI mention plus "was used to generate/create ..." in the same clause
_USED_TO_CREATE = re.compile(
    r"\b(?:was|were|has\s+been|have\s+been|is|are)\s+(?:\w+\s+)?used\s+(?:to|for)\s+(?:generate|create|make|write|code|"
    r"assist|help|design|produce|build|develop|draw|compose)"
)
_AI_TOKEN = re.compile(r"\b(?:ai|artificial\s+intelligence|llms?)\b")
_TOOL_TOKEN = re.compile(rf"\b{_GENAI_TOOL}\b")
_CLAUSE_SPLIT = re.compile(r"(?<!\d)[.!?](?!\d)|[;\n•]|\s[-–—]\s")
_AI_ONLY_DESCRIPTION = re.compile(r"\bai\s+(?:is|was)\s+only\s+used\s+(?:in|for)\s+(?:the\s+)?description")


def _normalise(text: str) -> str:
    text = html_lib.unescape(text)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("’", "'").replace("‘", "'").replace(" ", " ")
    text = text.lower()
    text = re.sub(r"\ba\.i\.?(?=\W|$)", "ai", text)
    text = re.sub(r"[*_#`>|]+", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def classify_ai(description: str | None) -> str | None:
    """The game's own AI declaration: "ai", "no_ai", or None (no declaration / no description)."""
    if not description or not isinstance(description, str):
        return None
    text = _normalise(description)
    if not _AI_TOKEN.search(text) and not _TOOL_TOKEN.search(text) and "100%" not in text:
        return None
    yes = no = False
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause or _GAMEPLAY_SCOPE.search(clause):
            continue
        clause = _AI_ONLY_DESCRIPTION.sub(" ", clause)
        if any(p.search(clause) for p in _NO_PATTERNS):
            no = True
            continue  # No wins inside its own clause
        clause = _GAME_AI.sub(" ", clause)
        if any(p.search(clause) for p in _YES_PATTERNS) or (_AI_TOKEN.search(clause) and _USED_TO_CREATE.search(clause)):
            yes = True
    if yes:
        return "ai"
    if no:
        return "no_ai"
    return None


# ---------------------------------------------------------------------------- adapter
class Kongregate(SourceAdapter):
    name = "kongregate"
    display_name = "Kongregate"
    base_url = BASE
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        self._cards: dict[str, dict] = {}  # external_id → merged card (+ list JSON-LD) seen this run
        self._categories: dict[str, set[str]] = {}  # external_id → category slugs of the lists it was seen on
        self.max_game_pages = int(config.get("max_game_pages_per_run", 60))
        self.game_page_every_hours = float(config.get("game_page_every_hours", 168))
        self.poll_every_hours = float(config.get("poll_every_hours", 2)) or 2.0
        self.game_pages_fetched = 0

    # ------------------------------------------------------------------ helpers
    def _get(self, path_or_url: str):
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        _guard(url)
        return self.http.get(url)

    def _ref(self, external_id: str) -> GameRef:
        return GameRef(source=self.name, external_id=external_id, url=game_url(external_id))

    def _remember(self, card: dict, category: str | None) -> None:
        eid = card["external_id"]
        prev = self._cards.get(eid)
        if prev is None:
            self._cards[eid] = dict(card)
        else:
            for k, v in card.items():
                if prev.get(k) is None and v is not None:
                    prev[k] = v
            # keep the strongest badge for game-page decisions: any list that says "New" wins
            if card.get("badge") == "New":
                prev["badge"] = "New"
        if category:
            self._categories.setdefault(eid, set()).add(category)

    def _rotation_due(self, external_id: str, now: datetime) -> bool:
        """True when this game's slot comes up: about once per `game_page_every_hours` per game."""
        if self.game_page_every_hours <= 0:
            return True
        slots = max(1, int(round(self.game_page_every_hours / self.poll_every_hours)))
        bucket = int(now.timestamp() // 3600 // self.poll_every_hours)
        return zlib.crc32(external_id.encode()) % slots == bucket % slots

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            if list_path(list_name) is None:
                log.warning("kongregate: unknown list %r (use hot, new, <category>_top|plays|newest)", list_name)
                continue
            parsed = parse_list_name(list_name)
            category = parsed[0] if parsed else None
            captured = _utcnow()
            pos = 0
            seen: set[str] = set()
            page = 1
            while pos < top_n:
                path = list_path(list_name, page)
                if path is None:
                    break
                resp = self._get(path)
                if resp.status_code in (404, 410):
                    log.warning("kongregate: list %r page %d answered %s", list_name, page, resp.status_code)
                    break
                cards = parse_cards(resp.text)
                if page == 1 and not cards:
                    log.warning("kongregate: no game cards on %s (markup change?)", path)
                fresh = [c for c in cards if c["external_id"] not in seen]
                for card in fresh:
                    if pos >= top_n:
                        break
                    pos += 1
                    seen.add(card["external_id"])
                    self._remember(card, category)
                    yield self._ref(card["external_id"]), ListPlacement(
                        source=self.name, list_name=list_name, position=pos,
                        external_id=card["external_id"], captured_at=captured,
                    )
                if len(cards) < PAGE_SIZE or not fresh:
                    break  # last page
                page += 1

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        now = _utcnow()
        for ref in refs:
            eid = ref.external_id.lower()
            resp = self._get(metrics_url(eid))
            if resp.status_code in (404, 410) or _redirected_away(resp, "/metrics.json"):
                yield GameSnapshot(source=self.name, external_id=ref.external_id, url=ref.url or game_url(eid),
                                   status="gone", captured_at=_utcnow())
                continue
            try:
                metrics = parse_metrics(json.loads(resp.text))
            except ValueError:
                metrics = None
            if metrics is None:
                log.warning("kongregate: %s answered %s but is not a metrics object; skipping", metrics_url(eid), resp.status_code)
                continue

            card = self._cards.get(eid) or {}
            ld = card.get("ld")
            if ld is None and self.game_pages_fetched < self.max_game_pages and self._rotation_due(eid, now):
                ld, gone = self._fetch_game_page(eid)
                if gone:
                    yield GameSnapshot(source=self.name, external_id=ref.external_id, url=ref.url or game_url(eid),
                                       status="gone", captured_at=_utcnow())
                    continue
            yield self._snapshot(ref, eid, card, ld, metrics)

    def _fetch_game_page(self, eid: str) -> tuple[dict | None, bool]:
        """GET /en/games/<dev>/<slug> → (VideoGame JSON-LD or None, gone?)."""
        self.game_pages_fetched += 1
        resp = self._get(game_url(eid))
        if resp.status_code in (404, 410):
            return None, True
        final = getattr(resp, "url", None)
        if final and not game_id_from_path(str(final)):
            return None, True  # redirected to a listing / home page: the game is not public any more
        ld = ld_video_game(resp.text)
        if ld is None:
            log.warning("kongregate: no VideoGame JSON-LD on %s", game_url(eid))
        return ld, False

    def _snapshot(self, ref: GameRef, eid: str, card: dict, ld: dict | None, metrics: dict) -> GameSnapshot:
        f = ld_fields(ld)
        dev = card.get("dev") or eid.split("/", 1)[0]
        tags = sorted(self._categories.get(eid, set()))
        if f.get("genre"):
            genre = slugify(f["genre"])
            if genre and genre not in tags:
                tags.append(genre)
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=ref.url or game_url(eid),
            title=card.get("title") or f.get("title"),
            thumbnail_url=card.get("thumbnail") or f.get("image"),
            creator_external_id=eid.split("/", 1)[0],
            creator_name=f.get("author") or dev,
            creator_url=None,  # /accounts/ is robots-disallowed
            plays_total=metrics["plays"],
            likes=metrics["favorites"],
            votes=metrics["rating_count"],
            rating=metrics["rating"] if metrics["rating_count"] else None,  # no public average while "under judgement"
            plays_24h=None,
            players_now=None,
            players_total=None,
            comments=None,  # /games/*/comments is robots-disallowed
            remixes=None,
            tags=tags,
            published_at=f.get("published_at"),
            ai_disclosure=classify_ai(f.get("description")) if ld is not None else None,
            status="ok",
            captured_at=_utcnow(),
        )

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        """Save the responses the parsers read: hot + new + one category list page (trimmed to the cards
        and JSON-LD), metrics.json for up to `limit` games, and one game page (trimmed)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, text: str) -> None:
            p = out_dir / name
            p.write_text(text, encoding="utf-8")
            written.append(p)

        pages = {"hot": "list_hot.html", "new": "list_new.html", "action_top": "list_action_top.html"}
        cards: list[dict] = []
        for list_name, fname in pages.items():
            html = self._get(list_path(list_name)).text
            save(fname, trim_list_html(html))
            cards.extend(parse_cards(html))
        ids = list(dict.fromkeys(c["external_id"] for c in cards))
        for eid in ids[: max(1, limit)]:
            resp = self._get(metrics_url(eid))
            if resp.status_code == 200:
                save(f"metrics_{eid.replace('/', '__')}.json", resp.text)
        new_game = next((c for c in cards if c.get("badge") == "New"), cards[0] if cards else None)
        if new_game is not None:
            eid = new_game["external_id"]
            save(f"game_{eid.replace('/', '__')}.html", trim_game_html(self._get(game_url(eid)).text))
        return written


def _guard(url: str) -> None:
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    if _FORBIDDEN.search(path):
        raise ValueError(f"kongregate: refusing robots-disallowed path {url}")


def _redirected_away(resp, suffix: str) -> bool:
    final = getattr(resp, "url", None)
    return bool(final) and not urlsplit(str(final)).path.endswith(suffix)


# ---------------------------------------------------------------------------- fixture trimming
def _strip_ld(node):
    """Drop what no parser reads (screenshots, offers, platform lists) so fixtures stay small."""
    if isinstance(node, list):
        return [_strip_ld(n) for n in node]
    if isinstance(node, dict):
        return {k: _strip_ld(v) for k, v in node.items()
                if k not in ("screenshot", "offers", "potentialAction", "gamePlatform", "operatingSystem", "sameAs", "logo")}
    return node


def _ld_scripts(html: str) -> str:
    out = []
    for m in _LDJSON_RE.finditer(html):
        try:
            data = _strip_ld(json.loads(m.group(1).strip()))
        except ValueError:
            continue
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        out.append(f'<script type="application/ld+json">{text}</script>')
    return "\n".join(out)


def trim_list_html(html: str) -> str:
    """Keep `<title>`, the JSON-LD scripts and the game-tile markup (what parse_cards reads)."""
    title = _TITLE_RE.search(html)
    tiles = html.split(_TILE_SPLIT)[1:]
    body = "".join(
        "<div " + _TILE_SPLIT + re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", t.split("</a>", 1)[0], flags=re.S)) + "</a></div>\n"
        for t in tiles
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title.group(1).strip() if title else ""}</title>\n'
        f"{_ld_scripts(html)}\n</head><body>\n{body}</body></html>\n"
    )


def trim_game_html(html: str) -> str:
    """Keep `<title>` and the JSON-LD scripts (what ld_video_game reads)."""
    title = _TITLE_RE.search(html)
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title.group(1).strip() if title else ""}</title>\n'
        f"{_ld_scripts(html)}\n</head><body></body></html>\n"
    )
