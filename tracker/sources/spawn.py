"""Spawn adapter — https://www.spawn.co (creation platform).

Spawn's `/api` is disallowed by robots.txt (`Http` raises RobotsDisallowed on it), so this adapter reads
only the server-rendered HTML of allowed pages (see docs/SOURCES.md, research/spawn/). No browser: Spawn
flags webdriver and posts impressions. `?offset=` paging on the list pages returns the same first page
(real paging goes through `/api/discovery/tab`), so every list is its first page only.

Where the numbers live in the HTML:
  1. Next.js RSC flight chunks  `<script>self.__next_f.push([1,"<escaped flight text>"])</script>`.
     The flight text is a sequence of rows `<hex id>:<payload>`; payloads are JSON (`{`, `[`, `"`),
     module refs (`I[...]`), hints (`HL[...]`), or text blobs `T<hex byte length>,<raw text>` that may
     contain newlines (the parser skips them by length).
     - list pages  (`/?filter=top|new|multiplayer`, `/?genre=<slug>`): one container
       `{view|lens, headerLabel, headerMeta, cards:[card…], nextOffset}` where a card is
       `{game:{id, app_id, play_count, like_count, comment_count, play_shape, posted_at, last_published_at,
                genre_tags, custom_thumbnail_url, og_image_url, games:{name, adventure_name}, profiles:{username}},
         playing?, votes?:{loves, hates}}`
       plus `liveRooms:[{game, playingNow, joinHref}]` (top ~12 rooms by concurrent players).
     - game page (`/@user/slug`): the same card-shaped object and a dehydrated react-query page
       `summary:{playCount, likeCount, commentCount, loves, hates}`, which is fresher than that page's own
       card (list pages can be fresher still); also the creator's profile row `{username, follower_count, bio, …}`.
     - creator page (`/@user`): a den card `{handle, likeCount, gameCount, bio, avatarUrl, partner, games[]}`.
       `follower_count` appeared on it in the research capture but not on later fetches; when it is absent
       we fall back to the value seen on that creator's game pages in the same run, else None.
  2. A static `<section data-signs-of-life>` block with one `<article>` per game:
     "Reviews: … · 17 votes · 27 reviews and replies · 317 likes · 34,224 plays · 55 playing right now".
     Used only when the flight parse yields nothing (format change) — those pseudo-cards have no uuid, so
     their external_id is "<user>/<slug>" and a warning is logged.

Metric semantics (metric_map in sources.yaml):
  plays_total = play_count    cumulative play sessions
  players_now = playing       concurrent players in rooms right now. The multiplayer list is sorted
                              "most players first" and simply omits `playing` for games with no live room,
                              so a multiplayer-list card without the key is 0; a card without the key seen
                              anywhere else (solo games, other lists) is None (unknown).
  likes       = like_count
  comments    = comment_count ("reviews and replies")
  votes       = loves + hates (box verdicts), only when the card carries `votes`
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

SITE = "https://www.spawn.co"
FILTER_LISTS = {"top", "new", "multiplayer"}

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_SIGNS_RE = re.compile(r"<section data-signs-of-life[^>]*>.*?</section>", re.S)
_ARTICLE_RE = re.compile(r"<article>.*?</article>", re.S)
_ROW_ID_RE = re.compile(rb"([0-9a-fA-F]+):")
_JSON_START = b'{["0123456789tfn-'


# ---------------------------------------------------------------------------- small helpers
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


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _tags(raw) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        v = t.get("slug") or t.get("name") if isinstance(t, dict) else t
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


# ---------------------------------------------------------------------------- RSC flight parsing
def flight_text(html: str) -> str:
    """Concatenate the JS string literals of every `self.__next_f.push([1,"…"])` call."""
    parts: list[str] = []
    for lit in _PUSH_RE.findall(html):
        try:
            parts.append(json.loads('"' + lit + '"'))
        except ValueError:  # a literal with an escape JSON does not accept; skip that chunk
            continue
    return "".join(parts)


def _iter_rows(data: bytes) -> Iterable[tuple[bytes, bytes]]:
    """Split flight bytes into rows → (kind, raw row bytes incl. id and trailing newline).

    Rows are `<hex id>:<payload>\\n`. `T<hexlen>,` rows are raw text of exactly that many UTF-8 bytes
    and may contain newlines, so they are cut by length rather than by line. kind is the payload's first
    byte: `{`/`[`/`"`/digit… for JSON, `I` module refs, `T` text, `H` hints, `X`/`C` misc, `?` unparsable.
    """
    pos, n = 0, len(data)
    while pos < n:
        m = _ROW_ID_RE.match(data, pos)
        if not m:  # e.g. ":HL[…" rows with an empty id, or stray text
            nl = data.find(b"\n", pos)
            end = n if nl < 0 else nl + 1
            yield b"?", data[pos:end]
            pos = end
            continue
        body_start = m.end()
        kind = data[body_start : body_start + 1]
        if kind == b"T":
            comma = data.find(b",", body_start)
            if comma < 0:
                yield b"?", data[pos:]
                return
            try:
                length = int(data[body_start + 1 : comma], 16)
            except ValueError:
                length = 0
            end = comma + 1 + length
            if data[end : end + 1] == b"\n":
                end += 1
            yield kind, data[pos:end]
            pos = end
            continue
        nl = data.find(b"\n", body_start)
        end = n if nl < 0 else nl + 1
        yield (kind or b"?"), data[pos:end]
        pos = end


def _row_body(row: bytes) -> bytes:
    return row[row.index(b":") + 1 :].rstrip(b"\n")


PEOPLE_NOW = re.compile(r'peopleNow\\?"\s*:\s*\{\s*\\?"people\\?"\s*:\s*(\d+)')


def parse_people_now(html: str) -> int | None:
    """Site-wide people on Spawn in the last hour, from the RSC payload (`peopleNow:{people:N, minute:M}`)."""
    m = PEOPLE_NOW.search(html)
    return int(m.group(1)) if m else None


def parse_flight(html: str) -> list[Any]:
    """Return every JSON payload in the page's RSC flight stream (see `_iter_rows`).
    Non-JSON rows and anything that fails to json-load are ignored."""
    out: list[Any] = []
    for kind, row in _iter_rows(flight_text(html).encode("utf-8")):
        if kind not in _JSON_START:
            continue
        try:
            out.append(json.loads(_row_body(row)))
        except ValueError:
            continue
    return out


def _walk(obj, pred, found: list) -> list:
    if isinstance(obj, dict):
        if pred(obj):
            found.append(obj)
        for v in obj.values():
            _walk(v, pred, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, pred, found)
    return found


def _is_card(obj) -> bool:
    """A card is `{"game": {...}}` whose game has an id and at least one of the counter/name blocks."""
    if not isinstance(obj, dict):
        return False
    g = obj.get("game")
    return (
        isinstance(g, dict)
        and isinstance(g.get("id"), str)
        and ("play_count" in g or "games" in g or "profiles" in g)
    )


def find_cards(payload) -> list[dict]:
    """Card objects in page order.

    Prefers the list container (`{…, "cards": [card, …], "nextOffset": …}` — `filterView` on `/?filter=`
    pages, `lensView` on `/?genre=` pages) so positions follow the list and `liveRooms` entries are not
    mixed in. Without such a container (game page, format change) every card-shaped object is returned,
    deduplicated by game id in walk order.
    """
    containers = _walk(payload, lambda d: isinstance(d.get("cards"), list) and any(_is_card(c) for c in d["cards"]), [])
    if containers:
        return [c for c in containers[0]["cards"] if _is_card(c)]
    out: list[dict] = []
    seen: set[str] = set()
    for c in _walk(payload, _is_card, []):
        gid = c["game"]["id"]
        if gid not in seen:
            seen.add(gid)
            out.append(c)
    return out


def find_all_cards(payloads: list[Any]) -> list[dict]:
    for p in payloads:
        cards = find_cards(p)
        if cards:
            return cards
    return []


def find_live_rooms(payloads: list[Any]) -> dict[str, int]:
    """`liveRooms:[{game:{id}, playingNow}]` → {game id: concurrent players}."""
    out: dict[str, int] = {}
    for p in payloads:
        for holder in _walk(p, lambda d: isinstance(d.get("liveRooms"), list), []):
            for room in holder["liveRooms"]:
                if _is_card(room):
                    n = _int_or_none(room.get("playingNow"))
                    if n is not None:
                        out[room["game"]["id"]] = n
    return out


def find_summary(payloads: list[Any]) -> dict | None:
    """Game page: `summary:{playCount, likeCount, commentCount, loves, hates}`."""
    for p in payloads:
        hits = _walk(p, lambda d: isinstance(d.get("summary"), dict) and "playCount" in d["summary"], [])
        if hits:
            return hits[0]["summary"]
    return None


def find_profile_rows(payloads: list[Any]) -> dict[str, dict]:
    """Rows shaped `{username, follower_count, …}` → {username: row}. Present on game pages (the creator)
    and sometimes on creator pages."""
    out: dict[str, dict] = {}
    for p in payloads:
        for row in _walk(p, lambda d: isinstance(d.get("username"), str) and "follower_count" in d, []):
            out.setdefault(row["username"], row)
    return out


def find_den_card(payloads: list[Any]) -> dict | None:
    """Creator page: `{handle, likeCount, gameCount, bio, avatarUrl, …}`."""
    for p in payloads:
        hits = _walk(p, lambda d: isinstance(d.get("handle"), str) and ("gameCount" in d or "likeCount" in d), [])
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------- text fallbacks
_NUM = r"([\d,]+)"
_COUNTER_RES = {
    "like_count": re.compile(_NUM + r"\s+likes?\b"),
    "play_count": re.compile(_NUM + r"\s+plays?\b"),
    "playing": re.compile(_NUM + r"\s+playing right now"),
    "votes_total": re.compile(_NUM + r"\s+votes?\b"),
    "comment_count": re.compile(_NUM + r"\s+reviews? and repl(?:y|ies)"),
}


def parse_counter_text(text: str) -> dict[str, int]:
    """'17 votes · 27 reviews and replies · 317 likes · 34,224 plays · 55 playing right now' → ints."""
    out: dict[str, int] = {}
    for key, rx in _COUNTER_RES.items():
        m = rx.search(text)
        if m:
            n = _int_or_none(m.group(1))
            if n is not None:
                out[key] = n
    return out


def game_page_counter_text(flight: str) -> str:
    """The game page's own SEO sentence ('Reviews: … · 317 likes · 34,224 plays · 55 playing right now'):
    the JSON string in the flight text that contains the first 'N plays'. Empty when there is none."""
    m = re.search(r"[\d,]+ plays\b", flight)
    if not m:
        return ""
    start = flight.rfind('"', 0, m.start()) + 1
    end = flight.find('"', m.end())
    return flight[start : end if end >= 0 else None]


def parse_signs_of_life(html: str) -> list[dict]:
    """Fallback: pseudo-cards from the static `<section data-signs-of-life>` articles, in page order.
    Shaped like flight cards but with `id` None (no uuid in the markup)."""
    m = _SIGNS_RE.search(html)
    if not m:
        return []
    cards: list[dict] = []
    for art in _ARTICLE_RE.findall(m.group(0)):
        art = art.replace("<!-- -->", "")
        head = re.search(r'<h4><a href="/@([^/"]+)/([^"?#]+)">(.*?)</a></h4>', art, re.S)
        if not head:
            continue
        username, slug, title = head.group(1), head.group(2), html_lib.unescape(re.sub(r"<[^>]+>", "", head.group(3))).strip()
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", art))
        counters = parse_counter_text(text)
        pub = re.search(r"Published (\d{4}-\d{2}-\d{2})", text)
        cats = re.search(r"Categories:\s*([^.]*?)\s*\.", text)
        tags = [t.strip().lower() for t in cats.group(1).split(",")] if cats else []
        game = {
            "id": None,
            "play_count": counters.get("play_count"),
            "like_count": counters.get("like_count"),
            "comment_count": counters.get("comment_count"),
            "posted_at": pub.group(1) + "T00:00:00+00:00" if pub else None,
            "genre_tags": [t for t in tags if t],
            "games": {"name": title, "adventure_name": slug},
            "profiles": {"username": username},
        }
        card: dict[str, Any] = {"game": game}
        if "playing" in counters:
            card["playing"] = counters["playing"]
        if "votes_total" in counters:
            card["votes"] = {"loves": counters["votes_total"], "hates": 0}
        cards.append(card)
    return cards


_CREATOR_MARKERS = (b'"handle"', b'"follower_count"')
_TRIM_MARKERS = (b'"play_count"', b'"cards"', b'"liveRooms"', b'"summary"', b"playing right now") + _CREATOR_MARKERS


def _trim_html(html: str, markers: tuple[bytes, ...] = _TRIM_MARKERS) -> str:
    """Fixture form of a 1–2 MB page: the RSC flight reduced to the JSON rows that carry data (any row
    mentioning one of `markers` — a card, list container, live rooms, summary, den card or profile row —
    kept whole and unmodified), re-emitted as one `self.__next_f.push` script, plus the signs-of-life
    section with the per-game changelog `<div>`s removed. Module manifests, CSS/text blobs and UI-tree
    rows are dropped. Every parser above works on this form as well as on the full page."""
    kept: list[bytes] = []
    for kind, row in _iter_rows(flight_text(html).encode("utf-8")):
        if kind in _JSON_START and any(mk in row for mk in markers):
            kept.append(row if row.endswith(b"\n") else row + b"\n")
    literal = json.dumps(b"".join(kept).decode("utf-8"), ensure_ascii=False).replace("<", "\\u003c")
    parts = [f"<script>self.__next_f.push([1,{literal}])</script>"]
    m = _SIGNS_RE.search(html)
    if m:
        parts.append(re.sub(r"<div>.*?</div>", "", m.group(0), flags=re.S))
    return "<!doctype html><html><body>\n" + "\n".join(parts) + "\n</body></html>\n"


# ---------------------------------------------------------------------------- adapter
class Spawn(SourceAdapter):
    name = "spawn"
    display_name = "Spawn"
    base_url = SITE
    segment = "creation"
    needs_browser = False

    def __init__(self, http, config):
        super().__init__(http, config)
        # per-run caches filled by discover()
        self._cards: dict[str, dict] = {}  # external_id → card
        self._on_multiplayer: set[str] = set()  # external_ids seen on /?filter=multiplayer
        self._live: dict[str, int] = {}  # game id → playingNow from liveRooms
        self._followers: dict[str, int] = {}  # username → follower_count seen on game pages
        self._people_now: int | None = None  # site-wide "people on Spawn in the last hour" (peopleNow.people)

    # ------------------------------------------------------------------ urls
    @staticmethod
    def list_url(list_name: str) -> str:
        if list_name in FILTER_LISTS:
            return f"{SITE}/?filter={quote(list_name, safe='')}"
        return f"{SITE}/?genre={quote(list_name, safe='')}"

    @staticmethod
    def game_url(username: str, slug: str) -> str:
        return f"{SITE}/@{quote(username, safe='')}/{quote(slug, safe='')}"

    @staticmethod
    def creator_url(username: str) -> str:
        return f"{SITE}/@{quote(username, safe='')}"

    # ------------------------------------------------------------------ card access
    @staticmethod
    def _ident(card: dict) -> tuple[str | None, str | None, str | None]:
        """(external_id, username, slug) of a card; any may be None."""
        g = card.get("game") or {}
        ext = _str_or_none(g.get("id")) or _str_or_none(g.get("app_id"))
        username = _str_or_none((g.get("profiles") or {}).get("username")) if isinstance(g.get("profiles"), dict) else None
        slug = _str_or_none((g.get("games") or {}).get("adventure_name")) if isinstance(g.get("games"), dict) else None
        if ext is None and username and slug:
            ext = f"{username}/{slug}"  # signs-of-life fallback: no uuid in the markup
        return ext, username, slug

    def _cards_from_page(self, html: str, *, what: str) -> list[dict]:
        payloads = parse_flight(html)
        cards = find_all_cards(payloads)
        if cards:
            self._live.update(find_live_rooms(payloads))
            return cards
        cards = parse_signs_of_life(html)
        if cards:
            log.warning("spawn: %s: no cards in the RSC flight, using the signs-of-life fallback (%d articles)", what, len(cards))
        return cards

    # ------------------------------------------------------------------ SourceAdapter
    def platform_totals(self) -> dict | None:
        return {"people_recent": self._people_now} if self._people_now is not None else None

    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            url = self.list_url(list_name)
            resp = self.http.get(url)
            if resp.status_code in (404, 410):
                log.warning("spawn: list %r → 404 at %s, skipping", list_name, url)
                continue
            people = parse_people_now(resp.text)
            if people is not None:
                self._people_now = max(people, self._people_now or 0)
            cards = self._cards_from_page(resp.text, what=f"list {list_name}")
            if not cards:
                log.warning("spawn: list %r produced no cards (%s)", list_name, url)
                continue
            captured = _utcnow()
            seen: set[str] = set()
            pos = 0
            for card in cards:
                if pos >= top_n:
                    break
                ext, username, slug = self._ident(card)
                if not ext or not username or not slug or ext in seen:
                    continue
                seen.add(ext)
                pos += 1
                self._cards[ext] = card
                if list_name == "multiplayer":
                    self._on_multiplayer.add(ext)
                yield (
                    GameRef(source=self.name, external_id=ext, url=self.game_url(username, slug)),
                    ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=ext, captured_at=captured),
                )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        for ref in refs:
            card = self._cards.get(ref.external_id)
            if card is not None:
                yield self._snapshot(ref, card, summary=None, from_multiplayer=ref.external_id in self._on_multiplayer)
                continue
            resp = self.http.get(ref.url)
            if resp.status_code in (404, 410):
                yield GameSnapshot(source=self.name, external_id=ref.external_id, url=ref.url, status="gone", captured_at=_utcnow())
                continue
            payloads = parse_flight(resp.text)
            for username, row in find_profile_rows(payloads).items():
                n = _int_or_none(row.get("follower_count"))
                if n is not None:
                    self._followers[username] = n
            cards = find_all_cards(payloads)
            summary = find_summary(payloads)
            text_counters = parse_counter_text(game_page_counter_text(flight_text(resp.text)))
            if cards:
                card = cards[0]
                self._cards[ref.external_id] = card
            else:
                card = {"game": {"id": ref.external_id}}
                if not summary and not text_counters:
                    log.warning("spawn: %s returned no game data; counters left None", ref.url)
            if "playing" not in card and "playing" in text_counters:
                # the game page card has no `playing`; the SEO sentence carries "N playing right now"
                card = {**card, "playing": text_counters["playing"]}
            if summary is None and text_counters:
                summary = {
                    "playCount": text_counters.get("play_count"),
                    "likeCount": text_counters.get("like_count"),
                    "commentCount": text_counters.get("comment_count"),
                }
                if "votes_total" in text_counters:  # the sentence gives the total only
                    summary["loves"], summary["hates"] = text_counters["votes_total"], 0
            yield self._snapshot(ref, card, summary=summary, from_multiplayer=False)

    def _snapshot(self, ref: GameRef, card: dict, *, summary: dict | None, from_multiplayer: bool) -> GameSnapshot:
        g = card.get("game") or {}
        games = g.get("games") if isinstance(g.get("games"), dict) else {}
        profiles = g.get("profiles") if isinstance(g.get("profiles"), dict) else {}
        username = _str_or_none(profiles.get("username"))
        summary = summary or {}

        plays = _int_or_none(summary.get("playCount"))
        if plays is None:
            plays = _int_or_none(g.get("play_count"))
        likes = _int_or_none(summary.get("likeCount"))
        if likes is None:
            likes = _int_or_none(g.get("like_count"))
        comments = _int_or_none(summary.get("commentCount"))
        if comments is None:
            comments = _int_or_none(g.get("comment_count"))

        votes = None
        loves, hates = _int_or_none(summary.get("loves")), _int_or_none(summary.get("hates"))
        if loves is not None or hates is not None:
            votes = (loves or 0) + (hates or 0)
        elif isinstance(card.get("votes"), dict):
            v = card["votes"]
            loves, hates = _int_or_none(v.get("loves")), _int_or_none(v.get("hates"))
            if loves is not None or hates is not None:
                votes = (loves or 0) + (hates or 0)

        players_now: int | None
        if "playing" in card:
            players_now = _int_or_none(card.get("playing"))
        else:
            players_now = self._live.get(ref.external_id)
            if players_now is None and from_multiplayer:
                players_now = 0  # multiplayer list omits `playing` when the game has no live room

        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=ref.url,
            title=_str_or_none(games.get("name")),
            thumbnail_url=_str_or_none(g.get("custom_thumbnail_url")) or _str_or_none(g.get("og_image_url")),
            creator_external_id=username,
            creator_name=username,
            creator_url=self.creator_url(username) if username else None,
            plays_total=plays,
            players_now=players_now,
            likes=likes,
            comments=comments,
            votes=votes,
            engine=None,
            tags=_tags(g.get("genre_tags")),
            published_at=_parse_dt(g.get("posted_at")) or _parse_dt(g.get("last_published_at")),
            status="ok",
            captured_at=_utcnow(),
        )

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        resp = self.http.get(self.creator_url(creator_ref))
        if resp.status_code in (404, 410):
            return None
        payloads = parse_flight(resp.text)
        card = find_den_card(payloads)
        rows = find_profile_rows(payloads)
        if card is None and not rows:
            return None
        card = card or {}
        handle = _str_or_none(card.get("handle")) or creator_ref
        row = rows.get(handle) or rows.get(creator_ref) or {}
        followers = _int_or_none(row.get("follower_count"))
        if followers is None:
            followers = self._followers.get(handle)
        bio = _str_or_none(card.get("bio")) or _str_or_none(row.get("bio"))
        return CreatorProfile(
            source=self.name,
            external_id=handle,
            name=_str_or_none(card.get("nickname")) or handle,
            url=self.creator_url(handle),
            avatar_url=_str_or_none(card.get("avatarUrl")) or _str_or_none(row.get("avatar_url")),
            followers=followers,
            bio=bio,
            links={},  # the den card exposes no external links
        )

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def save(name: str, html: str, markers: tuple[bytes, ...] = _TRIM_MARKERS) -> None:
            p = out_dir / name
            p.write_text(_trim_html(html, markers))
            written.append(p)

        first: tuple[str, str] | None = None
        for fname, list_name in (("list_top.html", "top"), ("list_multiplayer.html", "multiplayer"), ("list_new.html", "new"), ("genre_action.html", "action")):
            html = self.http.get(self.list_url(list_name)).text
            save(fname, html)
            if first is None:
                for card in find_all_cards(parse_flight(html))[: max(1, limit)]:
                    _, username, slug = self._ident(card)
                    if username and slug:
                        first = (username, slug)
                        break
        if first:
            username, slug = first
            r = self.http.get(self.game_url(username, slug))
            if r.status_code == 200:
                save(f"game_{slug}.html", r.text)
            r = self.http.get(self.creator_url(username))
            if r.status_code == 200:
                # the den page embeds the whole home-page browse state (~300 KB of cards); keep only the
                # rows fetch_creator() reads
                save(f"creator_{username}.html", r.text, _CREATOR_MARKERS)
        return written
