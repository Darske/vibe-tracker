"""Remix adapter — https://remix.gg (creation platform).

Remix is a Next.js app on Vercel. `/api/*` (tRPC) and `/games/*` are robots-disallowed, so the adapter
reads the server-rendered HTML of allowed pages only — `/`, `/category/<name>`, `/g/<slug>`, `/u/<id>` —
and never a browser (see docs/SOURCES.md, research/remix/). All data sits in the React Server Component
flight payload: `<script>self.__next_f.push([1,"<id>:<json>\\n..."])</script>` chunks. Scalars carry RSC
type prefixes — `"$D2026-08-03T…"` is a date, `"$n155636"` a bigint, `"$undefined"` undefined, `"$L2f"` a
reference to row 2f — which `rsc_scalar` strips.

Lists (sources.yaml `lists`):
  picks | latest | multiplayer | mobile | portals → the home page `/` (one request per run); shelf
                                                    `resolved.catalog.shelves.<id>.games` (48/24 each).
                                                    Shelf objects carry only `playCount`, so `fetch`
                                                    completes them from `/g/<slug>`.
  <category>  (action, casual, puzzle, …)         → `/category/<name>` page 1: the 48-game grid, whose
                                                    objects carry every counter (playCount, usersCount,
                                                    thumbsUpCount, commentCount, createdAt, creator, …).
  most_played_<category>                          → the "N free <category> games, most played first" chip
                                                    list on the same page: 120 `/g/<slug>` links, names
                                                    only; `fetch` hits `/g/<slug>` for each.

Game pages `/g/<slug>` embed the same object (merged from `gameSeo` + `game` props) plus a JSON-LD
`VideoGame` block whose `interactionStatistic` (PlayAction, LikeAction) and `datePublished` serve as a
fallback. A missing game answers 404 (or redirects to `/`) → status "gone".

Metric semantics (Remix's own definitions, see metric_map in sources.yaml):
  plays_total   = playCount       play sessions started, cumulative
  players_total = usersCount      unique users who have played the game, all time
  likes         = thumbsUpCount
  comments      = commentCount    game page / category grid only (home shelves omit it)
  `totalSecondsPlayed` exists but has no column — ignored. No CCU, no remixes, no votes.

Pages are Vercel ISR: `/category/*` and `/g/*` answer from a cache that can be hours old (`age` header);
`/u/*` is rendered per request.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

BASE = "https://remix.gg"
HOME_SHELVES = ("picks", "latest", "multiplayer", "mobile", "portals")
MOST_PLAYED_PREFIX = "most_played_"
CHIP_LIST_SIZE = 120  # "most played first" chip list per category page

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', re.S)
_LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_ROW_ID_RE = re.compile(rb"([0-9a-fA-F]+):")
_TEXT_ROW_RE = re.compile(rb"T([0-9a-fA-F]+),")
_REF_RE = re.compile(r"^\$L?([0-9a-fA-F]+)$")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_GAME_HREF_RE = re.compile(r"^/g/([^/?#]+)")
_PLAYED_TIMES_RE = re.compile(r"played\s+([\d.,]+)\s*([KkMmBb])?\s+times")
_GAMES_COUNT_RE = re.compile(r"^\s*([\d,]+)\s*$")


# ---------------------------------------------------------------------------- RSC flight parsing
def _unescape_js_string(body: str) -> str:
    """The push argument is a JS string literal; JSON's escape rules cover what Next.js emits."""
    try:
        return json.loads('"' + body + '"')
    except ValueError:
        return body.encode("utf-8").decode("unicode_escape", errors="replace")


def flight_text(html: str) -> str:
    """Concatenate every `self.__next_f.push([1, "..."])` payload in document order."""
    return "".join(_unescape_js_string(m.group(1)) for m in _PUSH_RE.finditer(html))


def parse_flight(html: str) -> dict[str, object]:
    """Return the flight rows as {row_id: value}.

    Rows are `<id>:<json>\\n`; text rows are `<id>:T<hex byte length>,<raw text>` (no newline terminator,
    and the text may contain newlines — hence the byte-length walk). JSON rows are json-loaded; text rows
    and non-JSON rows (`I[...]` module imports, `HL[...]` hints) are kept as strings.
    """
    data = flight_text(html).encode("utf-8")
    rows: dict[str, object] = {}
    pos = 0
    n = len(data)
    while pos < n:
        m = _ROW_ID_RE.match(data, pos)
        if not m:
            nl = data.find(b"\n", pos)
            pos = n if nl < 0 else nl + 1
            continue
        row_id = m.group(1).decode()
        pos = m.end()
        t = _TEXT_ROW_RE.match(data, pos)
        if t:
            length = int(t.group(1), 16)
            start = t.end()
            rows[row_id] = data[start : start + length].decode("utf-8", errors="replace")
            pos = start + length
            if pos < n and data[pos : pos + 1] == b"\n":
                pos += 1
            continue
        nl = data.find(b"\n", pos)
        end = n if nl < 0 else nl
        body = data[pos:end]
        pos = end + 1
        try:
            rows[row_id] = json.loads(body)
        except ValueError:
            rows[row_id] = body.decode("utf-8", errors="replace")
    return rows


def rsc_scalar(v):
    """Strip RSC type prefixes: `$D<iso>` → iso string, `$n<digits>` → int, `$undefined`/refs → None."""
    if not isinstance(v, str) or not v.startswith("$"):
        return v
    if v.startswith("$D"):
        return v[2:]
    if v.startswith("$n"):
        s = v[2:]
        try:
            return int(s)
        except ValueError:
            return None
    return None  # "$undefined", "$48:props:…", "$L2f", "$@48" — references or placeholders


def _walk(o, pred) -> Iterator:
    """Depth-first, document-order walk yielding every dict for which pred(dict) is true."""
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if pred(cur):
                yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _is_game(d: dict) -> bool:
    return isinstance(d.get("slug"), str) and bool(d.get("slug")) and "playCount" in d


def find_game_objects(payload) -> list[dict]:
    """Every dict with a `slug` and a `playCount`, in document order (payload: flight rows or any subtree)."""
    return list(_walk(payload, _is_game))


def _game_lists(payload) -> list[list[dict]]:
    """Every `games: [...]` array whose items are game objects, in document order."""
    out: list[list[dict]] = []
    for d in _walk(payload, lambda d: isinstance(d.get("games"), list)):
        games = [g for g in d["games"] if isinstance(g, dict) and _is_game(g)]
        if games:
            out.append(games)
    return out


def find_home_shelves(rows: dict) -> dict[str, list[dict]]:
    """Home page: `resolved.catalog.shelves` → {shelf key: [game, …]}; the shelf's own `id` is an alias."""
    out: dict[str, list[dict]] = {}
    for d in _walk(rows, lambda d: isinstance(d.get("shelves"), dict)):
        for key, shelf in d["shelves"].items():
            if not isinstance(shelf, dict) or not isinstance(shelf.get("games"), list):
                continue
            games = [g for g in shelf["games"] if isinstance(g, dict) and _is_game(g)]
            if not games:
                continue
            out.setdefault(str(key), games)
            sid = shelf.get("id")
            if isinstance(sid, str) and sid and sid != key:
                out.setdefault(sid, games)
        if out:
            break
    return out


def find_category_grid(rows: dict) -> list[dict]:
    """Category page 1: the 48-game grid — the `games` array whose objects carry the full counter set."""
    lists = _game_lists(rows)
    with_counters = [lst for lst in lists if any("usersCount" in g for g in lst)]
    if with_counters:
        return max(with_counters, key=len)
    if lists:
        return max(lists, key=len)
    return find_game_objects(rows)


def _resolve(node, rows: dict, depth: int = 0):
    """Replace `$L<id>` / `$<id>` reference strings with the row they point to (one level per hop)."""
    if depth > 8:
        return node
    if isinstance(node, str):
        m = _REF_RE.match(node)
        if m and m.group(1) in rows:
            return _resolve(rows[m.group(1)], rows, depth + 1)
        return node
    return node


def _is_element(node, tag: str | None = None) -> bool:
    return (
        isinstance(node, list)
        and len(node) == 4
        and node[0] == "$"
        and isinstance(node[1], str)
        and (tag is None or node[1] == tag)
        and isinstance(node[3], dict)
    )


def _game_links(node, rows: dict, out: list[tuple[str, str | None]], depth: int = 0) -> None:
    """Collect (slug, label) from every element with an href under /g/, resolving row references in order."""
    if depth > 64:
        return
    node = _resolve(node, rows)
    if isinstance(node, dict):
        href = node.get("href")
        if isinstance(href, str):
            m = _GAME_HREF_RE.match(href)
            if m:
                label = node.get("children")
                out.append((m.group(1), label if isinstance(label, str) else None))
        for v in node.values():
            _game_links(v, rows, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _game_links(v, rows, out, depth + 1)


def find_chip_list(rows: dict) -> list[tuple[str, str | None]]:
    """Category page: the "All <Category> games" nav (`aria-label`), 120 `/g/<slug>` chips, most played first."""
    best: list[tuple[str, str | None]] = []
    for d in _walk(rows, lambda d: isinstance(d.get("aria-label"), str) and re.match(r"(?i)^all .+ games$", d["aria-label"]) is not None):
        links: list[tuple[str, str | None]] = []
        _game_links(d.get("children"), rows, links)
        if len(links) > len(best):
            best = links
    if best:
        return _dedupe(best)
    # fallback: the largest <ul> made of /g/ links
    candidates: list[list[tuple[str, str | None]]] = []
    stack = [rows]
    while stack:
        cur = stack.pop()
        if _is_element(cur, "ul"):
            links = []
            _game_links(cur[3].get("children"), rows, links)
            if len(links) >= 20:
                candidates.append(links)
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return _dedupe(max(candidates, key=len)) if candidates else []


def _dedupe(links: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    seen: set[str] = set()
    out = []
    for slug, label in links:
        if slug not in seen:
            seen.add(slug)
            out.append((slug, label))
    return out


def parse_ld_json(html: str) -> list[dict]:
    """Every JSON-LD object on the page (top-level arrays flattened). Looks at the raw HTML `<script>`
    tags first, then at flight text rows (the page embeds the same block via `dangerouslySetInnerHTML`)."""
    out: list[dict] = []

    def add(text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        items = data if isinstance(data, list) else [data]
        out.extend(i for i in items if isinstance(i, dict))

    for m in _LDJSON_RE.finditer(html):
        add(m.group(1).strip())
    if not out:
        for v in parse_flight(html).values():
            if isinstance(v, str) and '"@context"' in v:
                add(v.strip())
    return out


def ld_video_game(html: str) -> dict | None:
    for item in parse_ld_json(html):
        if item.get("@type") == "VideoGame":
            return item
    return None


def ld_game_counters(ld: dict) -> dict:
    """VideoGame JSON-LD → {playCount, thumbsUpCount, createdAt, name, appImageUrl, categories, creator}."""
    out: dict = {}
    for stat in ld.get("interactionStatistic") or []:
        if not isinstance(stat, dict):
            continue
        itype = stat.get("interactionType")
        kind = itype.get("@type") if isinstance(itype, dict) else itype
        count = stat.get("userInteractionCount")
        if kind == "PlayAction":
            out["playCount"] = count
        elif kind == "LikeAction":
            out["thumbsUpCount"] = count
    if ld.get("datePublished"):
        out["createdAt"] = ld["datePublished"]
    if ld.get("name"):
        out["name"] = ld["name"]
    if isinstance(ld.get("image"), str):
        out["appImageUrl"] = ld["image"]
    if isinstance(ld.get("genre"), list):
        out["categories"] = [g for g in ld["genre"] if isinstance(g, str)]
    author = ld.get("author")
    if isinstance(author, dict):
        url = author.get("url") or ""
        m = re.search(r"/u/([^/?#]+)", url if isinstance(url, str) else "")
        name = author.get("name")
        creator = {}
        if m:
            creator["id"] = m.group(1)
        if isinstance(name, str) and name:
            creator["username"] = name.lstrip("@")
        if creator:
            out["creator"] = creator
    return out


# ---------------------------------------------------------------------------- scalar helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value) -> datetime | None:
    value = rsc_scalar(value)
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
    value = rsc_scalar(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        return int(s) if s.isdigit() else None
    return None


def _approx_count(num: str, suffix: str | None) -> int | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((suffix or "").lower(), 1)
    return int(round(v * mult))


def _tags(raw) -> list[str]:
    """`categories` is `["ACTION", …]` on game pages and home shelves, `[{"name": "ACTION"}, …]` in grids."""
    out: list[str] = []
    for c in raw if isinstance(raw, list) else []:
        v = c.get("name") or c.get("slug") if isinstance(c, dict) else c
        if isinstance(v, str) and v and v.lower() not in out:
            out.append(v.lower())
    return out


def _has_counters(game: dict) -> bool:
    """Grid / game-page objects carry usersCount + thumbsUpCount; home-shelf objects carry only playCount."""
    return "usersCount" in game or "thumbsUpCount" in game


def _merge(objs: list[dict]) -> dict:
    """First object wins per key; later objects fill keys that are missing or None/placeholder."""
    out: dict = {}
    for o in objs:
        for k, v in o.items():
            if k not in out or out[k] is None or (isinstance(out[k], str) and out[k].startswith("$")):
                out[k] = v
    return out


# ---------------------------------------------------------------------------- creator page
def parse_creator_page(html: str) -> dict | None:
    """`/u/<id>` → {id, username, avatar_url, games, plays_total} or None when nothing identifies a user."""
    rows = parse_flight(html)
    out: dict = {}
    for d in _walk(rows, lambda d: isinstance(d.get("user"), dict) and isinstance(d["user"].get("username"), str)):
        u = d["user"]
        out.setdefault("id", u.get("id"))
        out.setdefault("username", u.get("username"))
        if isinstance(u.get("pfp"), str):
            out.setdefault("avatar_url", u["pfp"])
        break
    for item in parse_ld_json(html):
        if item.get("@type") != "ProfilePage":
            continue
        main = item.get("mainEntity") if isinstance(item.get("mainEntity"), dict) else {}
        name = main.get("name")
        if isinstance(name, str) and name:
            out.setdefault("username", name.lstrip("@"))
        if isinstance(main.get("image"), str):
            out.setdefault("avatar_url", main["image"])
        url = item.get("url") or main.get("url")
        if isinstance(url, str):
            m = re.search(r"/u/([^/?#]+)", url)
            if m:
                out.setdefault("id", m.group(1))
        part = item.get("hasPart")
        if isinstance(part, dict) and _int_or_none(part.get("numberOfItems")) is not None:
            out.setdefault("games", _int_or_none(part.get("numberOfItems")))
        break
    # "<N> free games on Remix, played 2.9M times." — the paragraph is a children array of strings
    text = flight_text(html)
    m = _PLAYED_TIMES_RE.search(text)
    if m:
        out["plays_total"] = _approx_count(m.group(1), m.group(2))
    if "games" not in out:
        m2 = re.search(r'\["([\d,]+)"," free ","games"', text)
        if m2:
            out["games"] = _int_or_none(m2.group(1))
    return out if out.get("username") or out.get("id") else None


# ---------------------------------------------------------------------------- adapter
class Remix(SourceAdapter):
    name = "remix"
    display_name = "Remix"
    base_url = BASE
    segment = "creation"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        self._games: dict[str, dict] = {}  # slug → best object seen this run
        self._home_rows: dict | None = None  # parsed home page, once per run
        self._category_rows: dict[str, dict] = {}  # category → parsed /category/<name> page 1

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def game_url(slug: str) -> str:
        return f"{BASE}/g/{quote(slug, safe='')}"

    @staticmethod
    def creator_url(creator_id: str) -> str:
        return f"{BASE}/u/{quote(creator_id, safe='')}"

    def _ref(self, slug: str) -> GameRef:
        return GameRef(source=self.name, external_id=slug, url=self.game_url(slug))

    def _remember(self, game: dict) -> str | None:
        slug = game.get("slug")
        if not isinstance(slug, str) or not slug:
            return None
        prev = self._games.get(slug)
        if prev is None:
            self._games[slug] = dict(game)
        elif _has_counters(game) and not _has_counters(prev):
            self._games[slug] = _merge([game, prev])
        else:
            self._games[slug] = _merge([prev, game])
        return slug

    def _get_html(self, path: str):
        return self.http.get(BASE + path)

    def _home(self) -> dict:
        if self._home_rows is None:
            self._home_rows = parse_flight(self._get_html("/").text)
        return self._home_rows

    def _category(self, category: str) -> dict:
        if category not in self._category_rows:
            resp = self._get_html(f"/category/{quote(category, safe='')}")
            self._category_rows[category] = {} if resp.status_code in (404, 410) else parse_flight(resp.text)
        return self._category_rows[category]

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            captured = _utcnow()
            if list_name in HOME_SHELVES:
                shelves = find_home_shelves(self._home())
                games = shelves.get(list_name) or []
                if not games:
                    log.warning("remix: home shelf %r not found (have %s)", list_name, sorted(shelves))
                items: Iterable[tuple[int, str, dict | None]] = ((i, g["slug"], g) for i, g in enumerate(games, start=1))
            elif list_name.startswith(MOST_PLAYED_PREFIX):
                category = list_name[len(MOST_PLAYED_PREFIX) :]
                chips = find_chip_list(self._category(category))
                if not chips:
                    log.warning("remix: no chip list on /category/%s", category)
                items = ((i, slug, None) for i, (slug, _label) in enumerate(chips, start=1))
            else:
                grid = find_category_grid(self._category(list_name))
                if not grid:
                    log.warning("remix: no game grid on /category/%s", list_name)
                items = ((i, g["slug"], g) for i, g in enumerate(grid, start=1))

            seen: set[str] = set()
            for pos, slug, game in items:
                if pos > top_n:
                    break
                if not isinstance(slug, str) or not slug or slug in seen:
                    continue
                seen.add(slug)
                if game is not None:
                    self._remember(game)
                yield self._ref(slug), ListPlacement(
                    source=self.name, list_name=list_name, position=pos, external_id=slug, captured_at=captured
                )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        for ref in refs:
            slug = ref.external_id
            game = self._games.get(slug)
            if game is not None and _has_counters(game):
                yield self._snapshot(slug, ref.url, game)
                continue
            fetched = self._fetch_game(slug)
            if fetched is None:
                yield GameSnapshot(source=self.name, external_id=slug, url=ref.url, status="gone", captured_at=_utcnow())
                continue
            if not fetched:
                log.warning("remix: /g/%s returned 200 but no game object or JSON-LD could be parsed; skipping", slug)
                continue
            self._remember(fetched)
            yield self._snapshot(slug, ref.url, self._games.get(slug) or fetched)

    def _fetch_game(self, slug: str) -> dict | None:
        """GET /g/<slug>. None → gone (404 / redirected away), {} → unparseable, else the merged object."""
        resp = self._get_html(f"/g/{quote(slug, safe='')}")
        if resp.status_code in (404, 410):
            return None
        final = getattr(resp, "url", None)
        final_path = urlsplit(str(final)).path if final else f"/g/{slug}"
        if not final_path.startswith("/g/"):
            return None  # redirected to / (or elsewhere): the game is not public any more
        html = resp.text
        rows = parse_flight(html)
        objs = [g for g in find_game_objects(rows) if g.get("slug") == slug]
        if not objs and final_path != f"/g/{slug}":
            # redirected to a renamed slug: take that page's own game object
            new_slug = final_path[len("/g/") :].split("/")[0]
            objs = [g for g in find_game_objects(rows) if g.get("slug") == new_slug]
        ld = ld_video_game(html)
        if not objs and ld is None:
            title = _TITLE_RE.search(html)
            if title and "404" in title.group(1):
                return None
            return {}
        merged = _merge(objs) if objs else {}
        if ld is not None:
            merged = _merge([merged, ld_game_counters(ld)])
        merged.setdefault("slug", slug)
        return merged

    def _snapshot(self, slug: str, url: str, g: dict) -> GameSnapshot:
        creator = g.get("creator") if isinstance(g.get("creator"), dict) else {}
        creator_id = creator.get("id") if isinstance(creator.get("id"), str) and creator.get("id") else None
        creator_name = creator.get("username") if isinstance(creator.get("username"), str) else None
        live = g.get("liveVersion") if isinstance(g.get("liveVersion"), dict) else {}
        published = _parse_dt(g.get("createdAt")) or _parse_dt(live.get("launchedAt"))
        return GameSnapshot(
            source=self.name,
            external_id=slug,
            url=url or self.game_url(slug),
            title=g.get("name") or None,
            thumbnail_url=g.get("appImageUrl") or g.get("coverImageUrl") or g.get("image") or None,
            creator_external_id=creator_id,
            creator_name=creator_name,
            creator_url=self.creator_url(creator_id) if creator_id else None,
            plays_total=_int_or_none(g.get("playCount")),
            # players_total = usersCount: Remix's count of unique users who have played the game (all time).
            players_total=_int_or_none(g.get("usersCount")),
            players_now=None,  # Remix shows no concurrent-player figure
            likes=_int_or_none(g.get("thumbsUpCount")),
            comments=_int_or_none(g.get("commentCount")),
            remixes=None,
            votes=None,
            engine=None,
            tags=_tags(g.get("categories")),
            published_at=published,
            status="ok",
            captured_at=_utcnow(),
        )

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        resp = self._get_html(f"/u/{quote(creator_ref, safe='')}")
        if resp.status_code in (404, 410):
            return None
        final = getattr(resp, "url", None)
        if final and not urlsplit(str(final)).path.startswith("/u/"):
            return None
        info = parse_creator_page(resp.text)
        if not info:
            return None
        username = info.get("username") or creator_ref
        log.debug("remix: creator %s (%s): %s games, ~%s plays", creator_ref, username, info.get("games"), info.get("plays_total"))
        return CreatorProfile(
            source=self.name,
            external_id=creator_ref,
            name=username,
            url=self.creator_url(creator_ref),
            avatar_url=info.get("avatar_url") or None,
            followers=None,  # Remix profiles show level, streak and games count — no follower figure, no links
            bio=None,
            links={},
        )

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, html: str) -> None:
            p = out_dir / name
            p.write_text(trim_html(html), encoding="utf-8")
            written.append(p)

        home = self._get_html("/").text
        save("home.html", home)
        action = self._get_html("/category/action").text
        save("category_action.html", action)
        puzzle = self._get_html("/category/puzzle").text
        save("category_puzzle.html", puzzle)

        grid = find_category_grid(parse_flight(action))
        game = grid[0] if grid else None
        if game is None:
            shelves = find_home_shelves(parse_flight(home))
            picks = shelves.get("picks") or next(iter(shelves.values()), [])
            game = picks[0] if picks else None
        if game is not None:
            slug = game["slug"]
            save(f"game_{slug}.html", self._get_html(f"/g/{quote(slug, safe='')}").text)
            creator = game.get("creator") if isinstance(game.get("creator"), dict) else {}
            cid = creator.get("id")
            if isinstance(cid, str) and cid:
                save(f"creator_{cid}.html", self._get_html(f"/u/{quote(cid, safe='')}").text)
        return written


# ---------------------------------------------------------------------------- fixture trimming
_KEEP_MARKERS = ('"playCount"', '"href":"/g/', '"shelves"', '"@context"', '"aria-label":"All ', '"user":{"id"', '" free ",')


def trim_html(html: str) -> str:
    """Reduce a captured page to what the parser reads: the flight rows that carry game objects, shelf data,
    `/g/` links (chip list), JSON-LD and the creator block, re-emitted as one `__next_f.push` chunk, plus the
    page `<title>` and the raw JSON-LD script tags. Everything else (markup, CSS, module manifests) is dropped
    so each fixture stays well under 200 KB; `parse_flight` reads the trimmed file exactly like the original.
    """
    rows = parse_flight(html)
    kept: list[str] = []
    for row_id, value in rows.items():
        if isinstance(value, str):
            text = value
            serialised = None
        else:
            serialised = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            text = serialised
        if not any(marker in text for marker in _KEEP_MARKERS):
            continue
        if serialised is None:
            if value.startswith(("I[", "HL[")):
                continue
            b = value.encode("utf-8")
            kept.append(f"{row_id}:T{len(b):x},{value}")
        else:
            kept.append(f"{row_id}:{serialised}\n")
    flight = "".join(kept)
    js = json.dumps(flight, ensure_ascii=False).replace("<", "\\u003c")
    title = _TITLE_RE.search(html)
    ld_scripts = "\n".join(f'<script type="application/ld+json">{m.group(1).strip()}</script>' for m in _LDJSON_RE.finditer(html))
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title.group(1) if title else ''}</title>\n{ld_scripts}\n</head><body>\n"
        f"<script>self.__next_f.push([1,{js}])</script>\n</body></html>\n"
    )
