"""Playgama adapter — https://playgama.com (aggregator of HTML5 games; placements + votes).

`/api/` is robots-disallowed (Http enforces it), so everything comes from the server-rendered HTML
of robots-allowed pages. Each page embeds a dehydrated TanStack Query cache as

    window.__query=(function(a,b,c,...){return {mutations:[],queries:[...]}}(val_a,val_b,...))

i.e. devalue's `uneval` form: repeated values are hoisted into function parameters and the body
is a JS literal expression. `parse_devalue()` below is a small recursive-descent parser for exactly
that grammar (objects with bare keys, arrays, JSON strings, numbers, `void 0`, null/true/false,
`-0`, `Infinity`, `NaN`, `new Date(...)`, parameter references). It is a port of
research/scripts/extract_playgama_state.js without a JS VM.

What the parsers key on (see research/playgama/*_state.json):
  list page /category/<hru>?page=N
      query ["category-games-data", <hru>, "category", <page>, ...] →
      {games: [tuple…], category: {id, categoryType, total, totalPages}}
      tuple = [hru, title, preview_url, icon_url, _, game_id, _, _, badges[], source, platforms[], _]
      (45 per page; votes are NOT in the tuples — only the game page has rating.count)
  game page /game/<hru>
      query ["game-data", <hru>, ...] → {status, data:{game:{...}, leaderboard:{totalPlayers}, gameCategoriesInfo}}
      game: id, hru, title, rating{value,count}, developer, developer_id, engine[], tags[], icon, preview,
            createdAt, position, badges, links[]
  developer page /developer/<id>
      query ["category-seo-info", <id>, "developer", ...] → {categoryInfo:{title, description, ...}}
      query ["category-games-data", <id>, "developer", ...] → {category:{total}}

Metric semantics (sources.yaml metric_map):
  votes = likes   = rating.count         Playgama's rating widget IS the like/heart icon; one number
  players_total   = leaderboard.totalPlayers  players ranked on the game's leaderboard in the trailing
                                              7 days (weekly-active proxy, not all-time unique players)
  plays_total     = None                 not public on playgama.com (only the aggregate AI index)
  players_now     = None
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

BASE = "https://playgama.com"
PAGE_SIZE = 45
STATE_MARKER = "window.__query="


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


# ====================================================================== devalue parser
class DevalueError(ValueError):
    pass


class _Ref:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


_UNDEFINED = object()  # JS `void 0`; collapses to None when resolved
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUM_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_STR_ESC = {'"': '"', "'": "'", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}


class _Parser:
    """Parses one JS literal expression (devalue output) into Python data with `_Ref` placeholders."""

    def __init__(self, src: str, pos: int = 0):
        self.s = src
        self.i = pos
        self.n = len(src)

    # -- lexing helpers
    def ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < self.n else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise DevalueError(f"expected {ch!r} at {self.i}: {self.s[self.i:self.i + 40]!r}")
        self.i += 1

    def startswith(self, lit: str) -> bool:
        self.ws()
        return self.s.startswith(lit, self.i)

    # -- grammar
    def value(self):
        c = self.peek()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "\"'":
            return self.string()
        if c == "-" or c.isdigit() or c == ".":
            if self.startswith("-Infinity"):
                self.i += 9
                return float("-inf")
            return self.number()
        if self.startswith("void 0"):
            self.i += 6
            return _UNDEFINED
        if self.startswith("new Date("):
            self.i += 9
            inner = self.value()
            self.expect(")")
            return inner  # ISO string (or epoch) — callers parse with _parse_dt
        if self.startswith("new Map(") or self.startswith("new Set("):
            self.i += 8
            inner = self.value()
            self.expect(")")
            return inner
        if self.startswith("Object.create(null)"):
            self.i += len("Object.create(null)")
            return {}
        m = _IDENT_RE.match(self.s, self.i)
        if not m:
            raise DevalueError(f"unexpected token at {self.i}: {self.s[self.i:self.i + 40]!r}")
        word = m.group(0)
        self.i = m.end()
        if word == "null":
            return None
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "undefined":
            return _UNDEFINED
        if word == "Infinity":
            return float("inf")
        if word == "NaN":
            return float("nan")
        return _Ref(word)

    def number(self):
        m = _NUM_RE.match(self.s, self.i)
        if not m:
            raise DevalueError(f"bad number at {self.i}")
        self.i = m.end()
        txt = m.group(0)
        if any(ch in txt for ch in ".eE"):
            return float(txt)
        return int(txt)

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out: list[str] = []
        s = self.s
        while True:
            if self.i >= self.n:
                raise DevalueError("unterminated string")
            c = s[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = s[self.i + 1]
                if e == "u":
                    code = int(s[self.i + 2:self.i + 6], 16)
                    self.i += 6
                    # surrogate pairs
                    if 0xD800 <= code <= 0xDBFF and s.startswith("\\u", self.i):
                        low = int(s[self.i + 2:self.i + 6], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            self.i += 6
                    out.append(chr(code))
                elif e == "x":
                    out.append(chr(int(s[self.i + 2:self.i + 4], 16)))
                    self.i += 4
                elif e == "\n":
                    self.i += 2
                else:
                    out.append(_STR_ESC.get(e, e))
                    self.i += 2
                continue
            out.append(c)
            self.i += 1

    def arr(self) -> list:
        self.expect("[")
        out: list = []
        while True:
            c = self.peek()
            if c == "]":
                self.i += 1
                return out
            if c == ",":  # hole
                self.i += 1
                out.append(None)
                continue
            out.append(self.value())
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "]":
                raise DevalueError(f"expected , or ] at {self.i}")

    def obj(self) -> dict:
        self.expect("{")
        out: dict = {}
        while True:
            c = self.peek()
            if c == "}":
                self.i += 1
                return out
            if c in "\"'":
                key = self.string()
            else:
                m = _IDENT_RE.match(self.s, self.i) or _NUM_RE.match(self.s, self.i)
                if not m:
                    raise DevalueError(f"bad object key at {self.i}: {self.s[self.i:self.i + 40]!r}")
                key = m.group(0)
                self.i = m.end()
            self.expect(":")
            out[key] = self.value()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "}":
                raise DevalueError(f"expected , or }} at {self.i}")


def _resolve(node, env: dict, memo: dict, stack: set):
    """Replace `_Ref` placeholders with the parsed parameter values (recursively)."""
    if isinstance(node, _Ref):
        name = node.name
        if name in memo:
            return memo[name]
        if name not in env:
            raise DevalueError(f"unknown identifier {name}")
        if name in stack:  # cyclic — devalue would patch it via statements; not expected here
            return None
        stack.add(name)
        memo[name] = val = _resolve(env[name], env, memo, stack)
        stack.discard(name)
        return val
    if node is _UNDEFINED:
        return None
    if isinstance(node, list):
        return [_resolve(x, env, memo, stack) for x in node]
    if isinstance(node, dict):
        return {k: _resolve(v, env, memo, stack) for k, v in node.items()}
    return node


def parse_devalue(expr: str):
    """Parse a devalue `uneval` expression: either a plain literal or
    `(function(a,b,…){[statements;]return EXPR}(args…))` / `(function(…){…})(args…)`."""
    src = expr.strip().rstrip(";").strip()
    p = _Parser(src)
    if not p.startswith("(function("):
        return _resolve(p.value(), {}, {}, set())
    p.i += len("(function(")
    close = src.index(")", p.i)
    params = [x.strip() for x in src[p.i:close].split(",") if x.strip()]
    p.i = close + 1
    p.expect("{")
    statements: list[tuple[str, object]] = []
    while True:
        if p.startswith("return"):
            p.i += len("return")
            break
        # cyclic patch statement: `a.b=c;` or `a[0]=c;`
        m = re.compile(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[\d+\]|\[\"[^\"]*\"\])+)\s*=").match(src, p.i)
        if not m:
            raise DevalueError(f"unexpected statement at {p.i}: {src[p.i:p.i + 40]!r}")
        p.i = m.end()
        statements.append((m.group(1), p.value()))
        p.ws()
        if p.peek() == ";":
            p.i += 1
    body = p.value()
    p.expect("}")
    # either `}(args))` or `})(args)`
    if p.peek() == ")":
        p.i += 1
    p.expect("(")
    args: list = []
    while p.peek() != ")":
        args.append(p.value())
        if p.peek() == ",":
            p.i += 1
    p.i += 1
    if len(args) != len(params):
        raise DevalueError(f"{len(params)} params but {len(args)} args")
    env = dict(zip(params, args))
    memo: dict = {}
    result = _resolve(body, env, memo, set())
    for target, val in statements:  # best effort: apply `a.b=c` patches
        try:
            _apply_patch(target, _resolve(val, env, memo, set()), memo, env)
        except Exception:  # noqa: BLE001
            log.debug("playgama: could not apply devalue patch %s", target)
    return result


def _apply_patch(target: str, value, memo: dict, env: dict) -> None:
    parts = re.findall(r"^[A-Za-z_$][\w$]*|\.([A-Za-z_$][\w$]*)|\[(\d+)\]|\[\"([^\"]*)\"\]", target)
    root = target.split(".")[0].split("[")[0]
    obj = memo.get(root)
    if obj is None:
        obj = memo[root] = _resolve(env[root], env, memo, set())
    keys = []
    for name, idx, skey in parts:
        if name:
            keys.append(name)
        elif idx:
            keys.append(int(idx))
        elif skey:
            keys.append(skey)
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value


def extract_state(html: str, name: str = "__query"):
    """Locate `window.<name>=…` in a Playgama page and return the parsed value (None if absent)."""
    marker = f"window.{name}="
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("</script>", i)
    if j < 0:
        j = len(html)
    seg = html[i + len(marker):j]
    k = seg.find(";window.")
    if k >= 0:
        seg = seg[:k]
    return parse_devalue(seg)


def dehydrated_queries(state) -> list[tuple[list, object]]:
    out: list[tuple[list, object]] = []
    for q in (state or {}).get("queries") or []:
        if not isinstance(q, dict):
            continue
        key = q.get("queryKey")
        st = q.get("state") if isinstance(q.get("state"), dict) else {}
        out.append((key if isinstance(key, list) else [key], st.get("data")))
    return out


def find_query(state, prefix: str, *, kind: str | None = None):
    """First dehydrated query whose key starts with `prefix` (and whose 3rd key element == kind, if given)."""
    for key, data in dehydrated_queries(state):
        if key and key[0] == prefix and (kind is None or (len(key) > 2 and key[2] == kind)):
            return data
    return None


# ====================================================================== page parsers
def parse_category_page(html: str) -> tuple[dict, list[dict]]:
    """Return ({total, totalPages}, [game dict…]) from a /category or /developer list page.

    Game tuples are positional: [hru, title, preview, icon, _, id, _, _, badges, source, platforms, _]."""
    state = extract_state(html)
    data = find_query(state, "category-games-data")
    if not isinstance(data, dict):
        return {}, []
    meta = data.get("category") if isinstance(data.get("category"), dict) else {}
    games: list[dict] = []
    for t in data.get("games") or []:
        g = tuple_to_game(t)
        if g:
            games.append(g)
    return meta, games


def tuple_to_game(t) -> dict | None:
    if isinstance(t, dict):
        return t if isinstance(t.get("hru"), str) else None
    if not isinstance(t, list) or not t or not isinstance(t[0], str) or not t[0]:
        return None

    def at(i, typ):
        v = t[i] if len(t) > i else None
        return v if isinstance(v, typ) else None

    return {
        "hru": t[0],
        "title": at(1, str),
        "preview": at(2, str),
        "icon": at(3, str),
        "id": at(5, str),
        "badges": at(8, list) or [],
        "source": at(9, str),
        "platforms": at(10, list) or [],
    }


def parse_game_page(html: str) -> dict | None:
    """Return {game: {...}, leaderboard: {...}} from /game/<hru>, or None if the page has no game data."""
    state = extract_state(html)
    data = find_query(state, "game-data")
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    game = inner.get("game")
    if not isinstance(game, dict):
        return None
    lb = inner.get("leaderboard")
    return {"game": game, "leaderboard": lb if isinstance(lb, dict) else {}, "status": data.get("status")}


def parse_developer_page(html: str) -> dict | None:
    """Return {id, name, description, total_games} from /developer/<id>, or None."""
    state = extract_state(html)
    seo = find_query(state, "category-seo-info", kind="developer")
    info = (seo or {}).get("categoryInfo") if isinstance(seo, dict) else None
    games = find_query(state, "category-games-data", kind="developer")
    meta = (games or {}).get("category") if isinstance(games, dict) else None
    if not isinstance(info, dict) and not isinstance(meta, dict):
        return None
    info = info if isinstance(info, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    name = info.get("title")
    if not isinstance(name, str) or not name.strip():
        m = re.search(r"<title>(.*?)(?:\s+—\s+\[.*?)?</title>", html, re.S)
        name = m.group(1).strip() if m else None
    return {
        "id": str(info.get("id") or meta.get("id") or ""),
        "name": name or None,
        "description": info.get("description") if isinstance(info.get("description"), str) else None,
        "total_games": _int_or_none(meta.get("total")),
    }


def trim_html(html: str) -> str:
    """Keep only what the parsers need: <title> and the <script> holding window.__query (+ __apiRequests)."""
    title = re.search(r"<title>.*?</title>", html, re.S)
    i = html.find(STATE_MARKER)
    if i < 0:
        return html[:200_000]
    start = html.rfind("<script", 0, i)
    end = html.find("</script>", i)
    script = html[start:end + len("</script>")] if start >= 0 and end >= 0 else html[i:i + 190_000]
    return f"<!doctype html><html><head>{title.group(0) if title else ''}</head><body>{script}</body></html>"


# ====================================================================== adapter
class Playgama(SourceAdapter):
    name = "playgama"
    display_name = "Playgama"
    base_url = BASE
    segment = "aggregator"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        self._games: dict[str, dict] = {}  # hru → list tuple dict seen during discover

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def game_url(hru: str) -> str:
        return f"{BASE}/game/{quote(hru, safe='')}"

    @staticmethod
    def developer_url(dev_id: str) -> str:
        return f"{BASE}/developer/{quote(str(dev_id), safe='')}"

    @staticmethod
    def category_url(name: str, page: int) -> str:
        url = f"{BASE}/category/{quote(name, safe='')}"
        return url if page <= 1 else f"{url}?page={page}"

    def _ref(self, hru: str) -> GameRef:
        return GameRef(source=self.name, external_id=hru, url=self.game_url(hru))

    def _get_html(self, url: str) -> str | None:
        r = self.http.get(url)
        if r.status_code in (404, 410):
            return None
        return r.text

    # ------------------------------------------------------------------ SourceAdapter
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            captured = _utcnow()
            seen: set[str] = set()
            pos = 0
            page = 1
            while pos < top_n:
                html = self._get_html(self.category_url(list_name, page))
                if html is None:
                    if page == 1:
                        log.warning("playgama: list %r not found (404), skipping", list_name)
                    break
                meta, games = parse_category_page(html)
                if not games:
                    if page == 1:
                        log.warning("playgama: list %r has no embedded games, skipping", list_name)
                    break
                for g in games:
                    if pos >= top_n:
                        break
                    hru = g["hru"]
                    self._games.setdefault(hru, g)
                    if hru in seen:
                        continue
                    seen.add(hru)
                    pos += 1
                    yield self._ref(hru), ListPlacement(
                        source=self.name, list_name=list_name, position=pos, external_id=hru, captured_at=captured
                    )
                total_pages = _int_or_none(meta.get("totalPages"))
                if (total_pages is not None and page >= total_pages) or len(games) < PAGE_SIZE:
                    break
                page += 1

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        # One request per game: the game page is the only robots-allowed place with rating.count,
        # leaderboard.totalPlayers, developer_id, engine and createdAt.
        for ref in refs:
            hru = ref.external_id
            html = self._get_html(self.game_url(hru))
            if html is None:
                yield GameSnapshot(source=self.name, external_id=hru, url=ref.url, status="gone", captured_at=_utcnow())
                continue
            page = parse_game_page(html)
            if page is None:
                log.warning("playgama: %s rendered without game-data; counters left None", hru)
                cached = self._games.get(hru) or {}
                yield GameSnapshot(
                    source=self.name, external_id=hru, url=ref.url or self.game_url(hru),
                    title=cached.get("title"), thumbnail_url=cached.get("icon") or cached.get("preview"),
                    captured_at=_utcnow(),
                )
                continue
            yield self._snapshot(hru, ref.url, page)

    def _snapshot(self, hru: str, url: str, page: dict) -> GameSnapshot:
        g = page["game"]
        lb = page.get("leaderboard") or {}
        rating = g.get("rating") if isinstance(g.get("rating"), dict) else {}
        dev_id = g.get("developer_id")
        dev_id = str(dev_id) if dev_id not in (None, "") else None
        dev_name = g.get("developer") if isinstance(g.get("developer"), str) and g.get("developer").strip() else None
        engine = g.get("engine")
        if isinstance(engine, list):
            engine = engine[0] if engine and isinstance(engine[0], str) else None
        elif not isinstance(engine, str):
            engine = None
        votes = _int_or_none(rating.get("count"))
        return GameSnapshot(
            source=self.name,
            external_id=hru,
            url=url or self.game_url(hru),
            title=g.get("title") or None,
            thumbnail_url=g.get("icon") or g.get("preview") or None,
            creator_external_id=dev_id,
            creator_name=dev_name,
            creator_url=self.developer_url(dev_id) if dev_id else None,
            plays_total=None,  # not public on playgama.com
            players_now=None,
            # leaderboard.totalPlayers = players ranked on the game's leaderboard in the trailing 7 days
            # (Playgama leaderboards are weekly). A weekly-active proxy, not an all-time unique-player count.
            players_total=_int_or_none(lb.get("totalPlayers")),
            # Playgama's "rating" widget is the heart/like icon on the game page: rating.count is both the
            # vote count and the like count, so the same number lands in votes and likes.
            votes=votes,
            likes=votes,
            engine=engine.lower() if isinstance(engine, str) else None,
            tags=[t for t in (g.get("tags") or []) if isinstance(t, str) and t],
            published_at=_parse_dt(g.get("createdAt")),
            status="ok",
            captured_at=_utcnow(),
        )

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        html = self._get_html(self.developer_url(creator_ref))
        if html is None:
            return None
        info = parse_developer_page(html)
        if not info:
            return None
        total = info.get("total_games")
        bio = info.get("description")
        if total is not None:
            bio = f"{total} games on Playgama" + (f" — {bio}" if bio else "")
        return CreatorProfile(
            source=self.name,
            external_id=str(creator_ref),
            name=info.get("name") or str(creator_ref),
            url=self.developer_url(creator_ref),
            followers=None,  # developer pages show no follower count
            bio=bio or None,
            links={},  # developer pages expose no external links; game pages only list app-store links
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

        first_games: list[dict] = []
        for cat in ("top_playgama", "trending_now"):
            html = self._get_html(self.category_url(cat, 1))
            if html:
                save(f"category_{cat}_p1.html", html)
                _, games = parse_category_page(html)
                if not first_games:
                    first_games = games
        if first_games:
            hru = first_games[0]["hru"]
            html = self._get_html(self.game_url(hru))
            if html:
                save(f"game_{hru}.html", html)
                page = parse_game_page(html)
                dev = (page or {}).get("game", {}).get("developer_id")
                if dev not in (None, ""):
                    dh = self._get_html(self.developer_url(str(dev)))
                    if dh:
                        save(f"developer_{dev}.html", dh)
        return written
