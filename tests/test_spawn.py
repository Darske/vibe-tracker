"""Parse tests for the Spawn adapter against recorded fixtures (tests/fixtures/spawn/).

Fixtures are the trimmed form of the real pages (see Spawn._trim_html): the RSC flight rows that carry
data plus the `<section data-signs-of-life>` block."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tracker.models import CreatorProfile, GameRef, GameSnapshot
from tracker.sources.spawn import (
    Spawn,
    _trim_html,
    find_all_cards,
    find_cards,
    find_live_rooms,
    find_summary,
    parse_counter_text,
    parse_flight,
    parse_signs_of_life,
)

FIX = Path(__file__).parent / "fixtures" / "spawn"
GAME_FIXTURE = next(FIX.glob("game_*.html"))
GAME_SLUG = GAME_FIXTURE.stem[len("game_") :]
CREATOR_FIXTURE = next(FIX.glob("creator_*.html"))
CREATOR = CREATOR_FIXTURE.stem[len("creator_") :]
LISTS = {"top": "list_top.html", "multiplayer": "list_multiplayer.html", "new": "list_new.html", "action": "genre_action.html"}


def load(name: str) -> str:
    return (FIX / name).read_text()


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeHttp:
    """Serves fixture HTML by URL and records every URL asked for. Never serves /api."""

    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        assert "/api/" not in url and not url.startswith("https://www.spawn.co/api"), f"robots-disallowed URL requested: {url}"
        self.calls.append(url)
        self.requests_made += 1
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                return payload() if callable(payload) else payload
        m = re.fullmatch(r"https://www\.spawn\.co/\?(filter|genre)=([a-z0-9_-]+)", url)
        if m:
            name = m.group(2)
            if name in LISTS:
                return FakeResponse(load(LISTS[name]))
            return FakeResponse("<html><body>no such list</body></html>", 404)
        if url == f"https://www.spawn.co/@{CREATOR}/{GAME_SLUG}":
            return FakeResponse(load(GAME_FIXTURE.name))
        if url == f"https://www.spawn.co/@{CREATOR}":
            return FakeResponse(load(CREATOR_FIXTURE.name))
        if re.fullmatch(r"https://www\.spawn\.co/@[^/]+(/[^/]+)?", url):
            return FakeResponse("<html><body>404: This page could not be found.</body></html>", 404)
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None) -> tuple[Spawn, FakeHttp]:
    http = FakeHttp(overrides)
    return Spawn(http, {"name": "spawn", "lists": ["top"], "top_n": 100}), http


# ---------------------------------------------------------------- RSC parsing
def test_parse_flight_finds_list_container_cards():
    payloads = parse_flight(load("list_multiplayer.html"))
    assert payloads
    cards = find_all_cards(payloads)
    assert len(cards) >= 5
    for c in cards:
        g = c["game"]
        assert isinstance(g["id"], str) and g["games"]["adventure_name"] and g["profiles"]["username"]
        assert isinstance(g.get("play_count"), int)
    # the page also carries liveRooms with a concurrent-player count per room
    live = find_live_rooms(payloads)
    assert live and all(isinstance(v, int) for v in live.values())
    # find_cards prefers the `cards` container: liveRooms entries (joinHref/playingNow) are not mixed in
    assert not any("playingNow" in c for c in cards)


def test_find_cards_without_container_walks_everything():
    payload = {"a": [{"game": {"id": "x", "play_count": 1}}, {"other": {"game": {"id": "y", "games": {}}, "playing": 2}}, {"game": {"id": "x", "play_count": 1}}]}
    cards = find_cards(payload)
    assert [c["game"]["id"] for c in cards] == ["x", "y"]
    assert find_cards({"cards": [{"game": {"id": "z", "play_count": 0}}, "not a card"]}) == [{"game": {"id": "z", "play_count": 0}}]
    assert find_cards({"nothing": [1, 2, "3"]}) == []


def test_parse_flight_handles_text_rows_and_garbage():
    # a T row whose text contains newlines and a fake "id:" prefix must not break the JSON row after it
    txt = "hello\nworld\n1:{\"not\":1}"
    flight = f'0:["$","div"]\n2:I[1,[],"x"]\n:HL["/a.css","style"]\n5:T{len(txt.encode()):x},{txt}\n6:{{"game":{{"id":"g1","play_count":7}}}}\n7:{{broken\n'
    import json

    html = "<script>self.__next_f.push([1," + json.dumps(flight) + "])</script>"
    payloads = parse_flight(html)
    assert ["$", "div"] in payloads
    assert {"game": {"id": "g1", "play_count": 7}} in payloads
    assert not any(isinstance(p, dict) and p.get("not") == 1 for p in payloads)
    assert parse_flight("<html>no flight here</html>") == []


def test_signs_of_life_fallback_matches_flight_cards():
    html = load("list_top.html")
    flight_cards = find_all_cards(parse_flight(html))
    sol = parse_signs_of_life(html)
    assert len(sol) >= 5
    by_slug = {c["game"]["games"]["adventure_name"]: c for c in flight_cards}
    first = sol[0]
    assert first["game"]["games"]["adventure_name"] in by_slug
    twin = by_slug[first["game"]["games"]["adventure_name"]]
    assert first["game"]["profiles"]["username"] == twin["game"]["profiles"]["username"]
    assert first["game"]["games"]["name"] == twin["game"]["games"]["name"]
    assert isinstance(first["game"]["play_count"], int) and isinstance(first["game"]["like_count"], int)
    assert first["game"]["genre_tags"]
    assert parse_signs_of_life("<html></html>") == []


def test_counter_sentence_regex():
    c = parse_counter_text("Reviews: Mostly Positive · 70% loved it · 17 votes · 27 reviews and replies · 317 likes · 34,224 plays · 55 playing right now")
    assert c == {"like_count": 317, "play_count": 34224, "playing": 55, "votes_total": 17, "comment_count": 27}
    assert parse_counter_text("0 reviews and replies · 0 likes · 0 plays") == {"like_count": 0, "play_count": 0, "comment_count": 0}
    assert parse_counter_text("1 like · 1 play · 1 vote · 1 review and reply") == {"like_count": 1, "play_count": 1, "votes_total": 1, "comment_count": 1}
    assert parse_counter_text("nothing here") == {}


def test_trim_html_keeps_what_the_parsers_need():
    html = load("list_multiplayer.html")
    trimmed = _trim_html(html)
    assert len(trimmed) <= len(html)
    assert [c["game"]["id"] for c in find_all_cards(parse_flight(trimmed))] == [c["game"]["id"] for c in find_all_cards(parse_flight(html))]
    assert len(parse_signs_of_life(trimmed)) == len(parse_signs_of_life(html))
    # a full page shaped like the live one: markup around the scripts is dropped, the scripts survive
    page = "<html><head><style>x</style></head><body><div>junk</div>" + html + "<footer/></body></html>"
    assert find_all_cards(parse_flight(_trim_html(page)))


# ---------------------------------------------------------------- discover
@pytest.mark.parametrize("list_name", sorted(LISTS))
def test_discover_each_list_positions_from_one(list_name):
    adapter, http = make()
    out = list(adapter.discover([list_name], 100))
    assert len(out) >= 5
    placements = [p for _, p in out]
    assert [p.position for p in placements] == list(range(1, len(out) + 1))
    for ref, p in out:
        assert ref.source == "spawn" and p.source == "spawn"
        assert p.list_name == list_name and p.external_id == ref.external_id
        assert re.fullmatch(r"https://www\.spawn\.co/@[^/]+/[^/]+", ref.url), ref.url
        assert p.captured_at.tzinfo is not None
    assert len({r.external_id for r, _ in out}) == len(out)
    expected = f"https://www.spawn.co/?{'filter' if list_name in ('top', 'new', 'multiplayer') else 'genre'}={list_name}"
    assert http.calls == [expected]


def test_discover_urls_ids_and_cache():
    adapter, http = make()
    out = list(adapter.discover(["top"], 100))
    cards = find_all_cards(parse_flight(load("list_top.html")))
    first_card = cards[0]
    ref, placement = out[0]
    assert ref.external_id == first_card["game"]["id"]
    g = first_card["game"]
    assert ref.url == f"https://www.spawn.co/@{g['profiles']['username']}/{g['games']['adventure_name']}"
    assert adapter._cards[ref.external_id] is first_card or adapter._cards[ref.external_id] == first_card


def test_discover_respects_top_n_and_skips_404_lists(caplog):
    adapter, http = make()
    out = list(adapter.discover(["multiplayer", "nope", "top"], 3))
    assert [p.list_name for _, p in out] == ["multiplayer"] * 3 + ["top"] * 3
    assert "https://www.spawn.co/?genre=nope" in http.calls
    assert "404" in caplog.text


# ---------------------------------------------------------------- fetch
def test_fetch_from_cache_makes_no_requests():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(["top", "multiplayer"], 100)]
    n_before = http.requests_made
    snaps = list(adapter.fetch(refs))
    assert http.requests_made == n_before
    assert len(snaps) == len(refs) >= 5
    for s in snaps:
        assert isinstance(s, GameSnapshot) and s.status == "ok" and s.source == "spawn"
        assert s.title and s.url.startswith("https://www.spawn.co/@")
        assert s.plays_total is not None and s.likes is not None and s.comments is not None
        assert s.creator_external_id and s.creator_name == s.creator_external_id
        assert s.creator_url == f"https://www.spawn.co/@{s.creator_external_id}"
        assert s.thumbnail_url and s.thumbnail_url.startswith("https://")
        assert s.published_at is not None and s.published_at.tzinfo is not None
        assert s.engine is None and s.remixes is None and s.players_total is None and s.plays_24h is None
        assert isinstance(s.tags, list)
    cards = {c["game"]["id"]: c for c in find_all_cards(parse_flight(load("list_top.html")))}
    for s in snaps:
        c = cards.get(s.external_id)
        if c is None:
            continue
        g = c["game"]
        assert s.plays_total == g["play_count"] and s.likes == g["like_count"] and s.comments == g["comment_count"]
        assert s.tags == list(dict.fromkeys(g["genre_tags"]))
        if "votes" in c:
            assert s.votes == c["votes"]["loves"] + c["votes"]["hates"]
        else:
            assert s.votes is None


def test_multiplayer_cards_carry_players_now():
    adapter, _ = make()
    refs = [r for r, _ in adapter.discover(["multiplayer"], 100)]
    snaps = list(adapter.fetch(refs))
    cards = {c["game"]["id"]: c for c in find_all_cards(parse_flight(load("list_multiplayer.html")))}
    with_key = [s for s in snaps if "playing" in cards[s.external_id]]
    without_key = [s for s in snaps if "playing" not in cards[s.external_id]]
    assert len(with_key) >= 5
    for s in with_key:
        assert isinstance(s.players_now, int) and s.players_now == cards[s.external_id]["playing"]
    # the multiplayer list is sorted "most players first" and omits `playing` for games with no live room
    for s in without_key:
        assert s.players_now == 0
    assert all(isinstance(s.players_now, int) for s in snaps)
    assert max(s.players_now for s in snaps) >= 1


def test_players_now_is_none_when_unknown_outside_multiplayer_list():
    adapter, _ = make()
    refs = [r for r, _ in adapter.discover(["top"], 100)]
    snaps = list(adapter.fetch(refs))
    cards = {c["game"]["id"]: c for c in find_all_cards(parse_flight(load("list_top.html")))}
    live = find_live_rooms(parse_flight(load("list_top.html")))
    for s in snaps:
        c = cards[s.external_id]
        if "playing" in c:
            assert s.players_now == c["playing"]
        elif s.external_id in live:
            assert s.players_now == live[s.external_id]
        else:
            assert s.players_now is None


def test_fetch_uncached_ref_parses_game_page():
    adapter, http = make()
    url = f"https://www.spawn.co/@{CREATOR}/{GAME_SLUG}"
    ref = GameRef(source="spawn", external_id="some-uuid", url=url)
    snaps = list(adapter.fetch([ref]))
    assert http.calls == [url]
    assert len(snaps) == 1
    s = snaps[0]
    summary = find_summary(parse_flight(load(GAME_FIXTURE.name)))
    assert s.status == "ok" and s.external_id == "some-uuid" and s.url == url
    assert s.title and s.creator_external_id == CREATOR
    # the dehydrated summary is the freshest figure and wins over the card's own counters
    assert s.plays_total == summary["playCount"] and s.likes == summary["likeCount"] and s.comments == summary["commentCount"]
    assert s.votes == summary["loves"] + summary["hates"]
    sentence = parse_counter_text(load(GAME_FIXTURE.name).replace("\\u003c", "<"))
    assert s.players_now == sentence.get("playing")  # "N playing right now" or None
    assert s.tags and s.published_at is not None
    # the creator's follower_count on the game page is remembered for fetch_creator
    assert adapter._followers.get(CREATOR) is not None


def test_fetch_404_is_gone():
    adapter, _ = make()
    ref = GameRef(source="spawn", external_id="missing-id", url="https://www.spawn.co/@someone/does-not-exist")
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "gone" and s.external_id == "missing-id" and s.url == ref.url
    assert s.plays_total is None and s.players_now is None and s.likes is None


def test_missing_counters_stay_none_not_zero():
    import json

    bare = {"game": {"id": "bare-id", "games": {"name": "Bare", "adventure_name": "bare"}, "profiles": {"username": "u"}}}
    flight = "0:" + json.dumps({"filterView": {"view": "top", "cards": [bare], "nextOffset": 24}}) + "\n"
    html = "<script>self.__next_f.push([1," + json.dumps(flight) + "])</script>"
    adapter, _ = make({r"\?filter=top$": FakeResponse(html)})
    refs = [r for r, _ in adapter.discover(["top"], 10)]
    assert len(refs) == 1 and refs[0].external_id == "bare-id" and refs[0].url == "https://www.spawn.co/@u/bare"
    s = list(adapter.fetch(refs))[0]
    assert s.status == "ok" and s.title == "Bare"
    for field in ("plays_total", "plays_24h", "players_now", "players_total", "likes", "remixes", "comments", "votes", "followers"):
        assert getattr(s, field) is None, field
    assert s.tags == [] and s.published_at is None and s.engine is None and s.thumbnail_url is None

    # a stripped game page: no card, no summary, no sentence → ok with None counters, one request
    adapter2, http2 = make({r"/@u/bare$": FakeResponse("<html><body><script>self.__next_f.push([1,\"0:[]\\n\"])</script></body></html>")})
    s2 = list(adapter2.fetch([GameRef(source="spawn", external_id="bare-id", url="https://www.spawn.co/@u/bare")]))[0]
    assert s2.status == "ok" and s2.plays_total is None and s2.players_now is None and s2.likes is None and s2.votes is None
    assert len(http2.calls) == 1

    # explicit 0 in a card is a real zero and stays 0
    zero = {"game": {"id": "z", "play_count": 0, "like_count": 0, "comment_count": None, "games": {"name": "Z", "adventure_name": "z"}, "profiles": {"username": "u"}}, "playing": 0}
    flight = "0:" + json.dumps({"filterView": {"view": "top", "cards": [zero]}}) + "\n"
    html = "<script>self.__next_f.push([1," + json.dumps(flight) + "])</script>"
    adapter3, _ = make({r"\?filter=top$": FakeResponse(html)})
    s3 = list(adapter3.fetch([r for r, _ in adapter3.discover(["top"], 10)]))[0]
    assert s3.plays_total == 0 and s3.likes == 0 and s3.players_now == 0 and s3.comments is None and s3.votes is None


def test_discover_falls_back_to_signs_of_life(caplog):
    html = load("list_top.html")
    section = re.search(r"<section data-signs-of-life.*?</section>", html, re.S).group(0)
    adapter, _ = make({r"\?filter=top$": FakeResponse("<html><body>" + section + "</body></html>")})
    out = list(adapter.discover(["top"], 100))
    assert len(out) >= 5 and "signs-of-life fallback" in caplog.text
    ref, p = out[0]
    assert p.position == 1 and "/" in ref.external_id and ref.url == f"https://www.spawn.co/@{ref.external_id}"
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "ok" and s.title and s.plays_total is not None and s.likes is not None
    assert s.creator_external_id == ref.external_id.split("/")[0]


# ---------------------------------------------------------------- creators
def test_fetch_creator_parses_den_card_and_followers():
    adapter, http = make()
    prof = adapter.fetch_creator(CREATOR)
    assert isinstance(prof, CreatorProfile)
    assert http.calls == [f"https://www.spawn.co/@{CREATOR}"]
    assert prof.source == "spawn" and prof.external_id == CREATOR and prof.name == CREATOR
    assert prof.url == f"https://www.spawn.co/@{CREATOR}"
    assert prof.avatar_url and prof.avatar_url.startswith("https://")
    assert prof.links == {}
    assert prof.followers is None or isinstance(prof.followers, int)
    assert prof.bio is None or isinstance(prof.bio, str)

    # follower_count comes from the den page when it carries it, else from a game page seen in the run
    import json

    row = {"user_id": "x", "username": CREATOR, "follower_count": 42, "bio": "hi"}
    card = {"handle": CREATOR, "likeCount": 1, "gameCount": 2, "bio": None, "avatarUrl": None}
    flight = "0:" + json.dumps([card, {"state": {"data": row}}]) + "\n"
    html = "<script>self.__next_f.push([1," + json.dumps(flight) + "])</script>"
    adapter2, _ = make({rf"/@{CREATOR}$": FakeResponse(html)})
    p2 = adapter2.fetch_creator(CREATOR)
    assert p2.followers == 42 and p2.bio == "hi"

    adapter3, _ = make()
    list(adapter3.fetch([GameRef(source="spawn", external_id="id", url=f"https://www.spawn.co/@{CREATOR}/{GAME_SLUG}")]))
    p3 = adapter3.fetch_creator(CREATOR)
    assert p3.followers == adapter3._followers[CREATOR] and isinstance(p3.followers, int)


def test_fetch_creator_missing_returns_none():
    adapter, _ = make()
    assert adapter.fetch_creator("nobody-here-xyz") is None
    adapter2, _ = make({r"/@empty$": FakeResponse("<html><body>nothing embedded</body></html>")})
    assert adapter2.fetch_creator("empty") is None


# ---------------------------------------------------------------- fixture hygiene
@pytest.mark.parametrize("name", sorted(p.name for p in FIX.glob("*.html")))
def test_fixtures_are_trimmed_and_robots_clean(name):
    text = load(name)
    # the multiplayer list alone is ~60 cards × 3 KB in one unmodified flight row; everything else < 200 KB
    assert (FIX / name).stat().st_size <= (320 if name == "list_multiplayer.html" else 200) * 1024
    assert "__next_f.push" in text or "data-signs-of-life" in text
    assert "spawn.co/api/" not in text
