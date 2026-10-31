"""Parse tests for the Remix adapter against recorded, trimmed HTML fixtures (tests/fixtures/remix/)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from tracker.models import CreatorProfile, GameRef, GameSnapshot
from tracker.sources.remix import (
    Remix,
    find_category_grid,
    find_chip_list,
    find_game_objects,
    find_home_shelves,
    parse_creator_page,
    parse_flight,
    rsc_scalar,
    trim_html,
)

FIX = Path(__file__).parent / "fixtures" / "remix"
GAME_FIXTURE = sorted(FIX.glob("game_*.html"))[0]
GAME_SLUG = GAME_FIXTURE.name[len("game_") : -len(".html")]
CREATOR_FIXTURE = sorted(FIX.glob("creator_*.html"))[0]
CREATOR_ID = CREATOR_FIXTURE.name[len("creator_") : -len(".html")]


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = ""):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeHttp:
    """Serves fixture HTML by URL and records every URL asked for."""

    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        self.requests_made += 1
        assert not re.search(r"remix\.gg/(api|games)/", url), f"robots-disallowed path requested: {url}"
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                return payload(url) if callable(payload) else payload
        if url == "https://remix.gg/":
            return FakeResponse(load("home.html"), url=url)
        if url == "https://remix.gg/category/action":
            return FakeResponse(load("category_action.html"), url=url)
        if url == "https://remix.gg/category/puzzle":
            return FakeResponse(load("category_puzzle.html"), url=url)
        if url == f"https://remix.gg/g/{GAME_SLUG}":
            return FakeResponse(GAME_FIXTURE.read_text(encoding="utf-8"), url=url)
        if url.startswith("https://remix.gg/g/"):
            return FakeResponse("<html><head><title>404 Not Found</title></head><body></body></html>", status_code=404, url=url)
        if url == f"https://remix.gg/u/{CREATOR_ID}":
            return FakeResponse(CREATOR_FIXTURE.read_text(encoding="utf-8"), url=url)
        if url.startswith("https://remix.gg/u/"):
            return FakeResponse("<html><head><title>404 Not Found</title></head><body></body></html>", status_code=404, url=url)
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None) -> tuple[Remix, FakeHttp]:
    http = FakeHttp(overrides)
    return Remix(http, {"name": "remix", "lists": ["picks", "action"], "top_n": 100}), http


# ---------------------------------------------------------------- pure parsers
def test_rsc_scalar_strips_prefixes():
    assert rsc_scalar("$D2026-08-03T10:00:00.000Z") == "2026-08-03T10:00:00.000Z"
    assert rsc_scalar("$n155636") == 155636
    assert rsc_scalar("$undefined") is None
    assert rsc_scalar("$48:props:game:compatibility") is None
    assert rsc_scalar("plain") == "plain"
    assert rsc_scalar(42) == 42
    assert rsc_scalar(None) is None


def test_parse_flight_rows_and_text_chunks():
    html = (
        '<script>self.__next_f.push([1,"1:{\\"a\\":1}\\n"])</script>'
        '<script>self.__next_f.push([1,"2:T5,héll3:[\\"x\\"]\\n4:I[1,[],\\"m\\"]\\n"])</script>'
    )
    rows = parse_flight(html)
    assert rows["1"] == {"a": 1}
    assert rows["2"] == "héll"  # T length is in UTF-8 bytes: "héll" is 5 bytes
    assert rows["3"] == ["x"]
    assert rows["4"].startswith("I[")


def test_home_shelves_parse_from_fixture():
    shelves = find_home_shelves(parse_flight(load("home.html")))
    for key in ("picks", "latest", "multiplayer", "mobile", "portals"):
        assert len(shelves.get(key, [])) >= 5, key
    assert len(shelves["picks"]) >= 24
    for g in shelves["picks"]:
        assert g["slug"] and "playCount" in g


def test_category_grid_and_chip_list_parse_from_fixture():
    for name in ("category_action.html", "category_puzzle.html"):
        rows = parse_flight(load(name))
        grid = find_category_grid(rows)
        assert len(grid) >= 5
        assert all("usersCount" in g and "thumbsUpCount" in g for g in grid)
        chips = find_chip_list(rows)
        assert len(chips) >= 50
        assert len({s for s, _ in chips}) == len(chips)
        assert all(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s) for s, _ in chips), chips[:5]
        assert all(isinstance(label, str) and label for _, label in chips)


def test_find_game_objects_on_game_page():
    objs = [o for o in find_game_objects(parse_flight(GAME_FIXTURE.read_text())) if o["slug"] == GAME_SLUG]
    assert objs
    merged_keys = set().union(*(o.keys() for o in objs))
    assert {"playCount", "usersCount", "thumbsUpCount", "createdAt", "creator"} <= merged_keys


# ---------------------------------------------------------------- discover
def test_discover_home_shelf_positions_start_at_one():
    adapter, http = make()
    out = list(adapter.discover(["picks"], 100))
    assert len(out) >= 5
    placements = [p for _, p in out]
    assert [p.position for p in placements] == list(range(1, len(placements) + 1))
    for ref, p in out:
        assert ref.source == "remix" and p.source == "remix" and p.list_name == "picks"
        assert ref.external_id and ref.url == f"https://remix.gg/g/{ref.external_id}"
        assert p.external_id == ref.external_id and p.captured_at.tzinfo is not None
    assert http.calls == ["https://remix.gg/"]


def test_discover_home_page_fetched_once_for_all_shelves():
    adapter, http = make()
    out = list(adapter.discover(["picks", "latest", "multiplayer", "mobile", "portals"], 100))
    assert http.calls == ["https://remix.gg/"]
    names = {p.list_name for _, p in out}
    assert names == {"picks", "latest", "multiplayer", "mobile", "portals"}
    for name in names:
        ps = [p.position for _, p in out if p.list_name == name]
        assert ps[0] == 1 and ps == list(range(1, len(ps) + 1)) and len(ps) >= 5


def test_discover_category_grid():
    adapter, http = make()
    out = list(adapter.discover(["action", "puzzle"], 100))
    assert http.calls == ["https://remix.gg/category/action", "https://remix.gg/category/puzzle"]
    for name in ("action", "puzzle"):
        ps = [p.position for _, p in out if p.list_name == name]
        assert len(ps) >= 5 and ps == list(range(1, len(ps) + 1))
    assert len({r.external_id for r, p in out if p.list_name == "action"}) == len([p for _, p in out if p.list_name == "action"])


def test_discover_most_played_chip_list_shares_category_request():
    adapter, http = make()
    out = list(adapter.discover(["action", "most_played_action"], 100))
    assert http.calls == ["https://remix.gg/category/action"]  # same page, parsed once
    chips = [(r, p) for r, p in out if p.list_name == "most_played_action"]
    assert len(chips) == 100
    assert [p.position for _, p in chips] == list(range(1, 101))
    assert all(r.url == f"https://remix.gg/g/{r.external_id}" for r, _ in chips)
    # chip entries carry no counters, so nothing with counters is cached for chip-only slugs
    grid_slugs = {r.external_id for r, p in out if p.list_name == "action"}
    chip_only = [r.external_id for r, _ in chips if r.external_id not in grid_slugs]
    assert chip_only
    assert all(adapter._games.get(s) is None for s in chip_only)


def test_discover_respects_top_n():
    adapter, _ = make()
    out = list(adapter.discover(["picks", "action", "most_played_action"], 7))
    for name in ("picks", "action", "most_played_action"):
        assert [p.position for _, p in out if p.list_name == name] == list(range(1, 8))


# ---------------------------------------------------------------- fetch
def test_fetch_from_category_cache_makes_zero_requests():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(["action"], 100)]
    n_before = len(http.calls)
    snaps = list(adapter.fetch(refs))
    assert len(http.calls) == n_before  # every grid object carries its counters
    assert len(snaps) == len(refs) >= 5
    for s in snaps:
        assert isinstance(s, GameSnapshot) and s.status == "ok"
        assert s.title and s.url.startswith("https://remix.gg/g/")
        assert isinstance(s.plays_total, int) and isinstance(s.players_total, int) and isinstance(s.likes, int)
        assert s.comments is None or isinstance(s.comments, int)
        assert s.creator_external_id and s.creator_name
        assert s.creator_url == f"https://remix.gg/u/{s.creator_external_id}"
        assert s.players_now is None and s.remixes is None and s.votes is None and s.engine is None
        assert isinstance(s.published_at, datetime) and s.published_at.tzinfo is not None  # "$D…" stripped
        assert s.tags and all(t == t.lower() for t in s.tags)
        assert s.thumbnail_url and s.thumbnail_url.startswith("https://")
    assert any(s.plays_total > 0 for s in snaps)


def test_fetch_home_shelf_game_completes_from_game_page():
    """Shelf objects carry only playCount, so fetch hits /g/<slug> unless a grid already supplied counters."""
    def game_page(url: str) -> FakeResponse:  # the recorded game page, re-slugged to whatever was asked for
        slug = url.rsplit("/", 1)[1]
        return FakeResponse(GAME_FIXTURE.read_text(encoding="utf-8").replace(GAME_SLUG, slug), url=url)

    adapter, http = make({r"/g/": game_page})
    refs = [r for r, _ in adapter.discover(["picks"], 3)]
    n_before = len(http.calls)
    snaps = list(adapter.fetch(refs))
    new = http.calls[n_before:]
    assert len(new) == len(refs) and all(u.startswith("https://remix.gg/g/") for u in new)
    assert len(snaps) == len(refs)
    assert all(isinstance(s.players_total, int) and isinstance(s.likes, int) for s in snaps)


def test_fetch_uncached_ref_parses_game_page():
    adapter, http = make()
    ref = GameRef(source="remix", external_id=GAME_SLUG, url=f"https://remix.gg/g/{GAME_SLUG}")
    snaps = list(adapter.fetch([ref]))
    assert http.calls == [f"https://remix.gg/g/{GAME_SLUG}"]
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "ok" and s.external_id == GAME_SLUG and s.title
    assert isinstance(s.plays_total, int) and isinstance(s.players_total, int) and isinstance(s.likes, int)
    assert isinstance(s.comments, int)  # the `game` prop carries commentCount
    assert isinstance(s.published_at, datetime)  # createdAt "$D…" from the `gameSeo` prop
    assert s.creator_external_id and s.creator_url == f"https://remix.gg/u/{s.creator_external_id}"
    assert s.tags
    # second fetch of the same ref is served from the cache
    snaps2 = list(adapter.fetch([ref]))
    assert len(http.calls) == 1 and snaps2[0].plays_total == s.plays_total


def test_fetch_404_is_gone():
    adapter, http = make()
    ref = GameRef(source="remix", external_id="does-not-exist", url="https://remix.gg/g/does-not-exist")
    snaps = list(adapter.fetch([ref]))
    assert http.calls == ["https://remix.gg/g/does-not-exist"]
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "gone" and s.external_id == "does-not-exist" and s.url == ref.url
    assert s.plays_total is None and s.players_total is None and s.likes is None


def test_fetch_redirect_to_home_is_gone():
    adapter, _ = make({r"/g/private-one": FakeResponse(load("home.html"), url="https://remix.gg/")})
    ref = GameRef(source="remix", external_id="private-one", url="https://remix.gg/g/private-one")
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "gone"


def test_json_ld_fallback_when_game_object_missing():
    html = (
        '<html><head><title>Bare</title><script type="application/ld+json">'
        '[{"@context":"https://schema.org","@type":"VideoGame","name":"Bare","url":"https://remix.gg/g/bare",'
        '"datePublished":"2026-01-02T03:04:05.000Z","genre":["Puzzle"],'
        '"author":{"@type":"Person","name":"@someone","url":"https://remix.gg/u/abc123"},'
        '"interactionStatistic":[{"@type":"InteractionCounter","interactionType":{"@type":"PlayAction"},"userInteractionCount":77},'
        '{"@type":"InteractionCounter","interactionType":{"@type":"LikeAction"},"userInteractionCount":5}]}]'
        "</script></head><body></body></html>"
    )
    adapter, _ = make({r"/g/bare": FakeResponse(html, url="https://remix.gg/g/bare")})
    s = list(adapter.fetch([GameRef(source="remix", external_id="bare", url="https://remix.gg/g/bare")]))[0]
    assert s.status == "ok" and s.title == "Bare"
    assert s.plays_total == 77 and s.likes == 5
    assert s.players_total is None and s.comments is None  # JSON-LD has no usersCount / commentCount
    assert s.published_at == datetime.fromisoformat("2026-01-02T03:04:05+00:00")
    assert s.creator_external_id == "abc123" and s.creator_name == "someone"
    assert s.tags == ["puzzle"]


def test_missing_counters_stay_none_not_zero():
    stripped = '<script>self.__next_f.push([1,"5:[\\"$\\",\\"div\\",null,{\\"game\\":{\\"slug\\":\\"bare\\",\\"name\\":\\"Bare\\",\\"playCount\\":null,\\"usersCount\\":null}}]\\n"])</script>'
    adapter, _ = make({r"/g/bare": FakeResponse(stripped, url="https://remix.gg/g/bare")})
    s = list(adapter.fetch([GameRef(source="remix", external_id="bare", url="https://remix.gg/g/bare")]))[0]
    assert s.status == "ok" and s.title == "Bare"
    for field in ("plays_total", "players_total", "players_now", "likes", "remixes", "comments", "votes", "followers"):
        assert getattr(s, field) is None, field
    assert s.creator_external_id is None and s.creator_url is None
    assert s.tags == [] and s.published_at is None and s.engine is None

    # a real zero stays zero; a "$n" bigint and "$D" date are stripped
    zero = '<script>self.__next_f.push([1,"5:{\\"games\\":[{\\"slug\\":\\"z\\",\\"name\\":\\"Z\\",\\"playCount\\":0,\\"usersCount\\":\\"$n12\\",\\"thumbsUpCount\\":null,\\"createdAt\\":\\"$D2026-03-04T05:06:07.000Z\\",\\"liveVersion\\":{\\"launchedAt\\":\\"$undefined\\"}}]}\\n"])</script>'
    adapter2, _ = make({r"/category/zero": FakeResponse(zero, url="https://remix.gg/category/zero")})
    refs = [r for r, _ in adapter2.discover(["zero"], 5)]
    s2 = list(adapter2.fetch(refs))[0]
    assert s2.plays_total == 0 and s2.players_total == 12 and s2.likes is None
    assert s2.published_at == datetime.fromisoformat("2026-03-04T05:06:07+00:00")


def test_unparseable_game_page_is_skipped_not_gone(caplog):
    adapter, _ = make({r"/g/weird": FakeResponse("<html><head><title>Weird</title></head><body>no flight</body></html>", url="https://remix.gg/g/weird")})
    snaps = list(adapter.fetch([GameRef(source="remix", external_id="weird", url="https://remix.gg/g/weird")]))
    assert snaps == []
    assert "no game object" in caplog.text


# ---------------------------------------------------------------- creators
def test_parse_creator_page():
    info = parse_creator_page(CREATOR_FIXTURE.read_text(encoding="utf-8"))
    assert info["id"] == CREATOR_ID
    assert info["username"]
    assert isinstance(info.get("games"), int) and info["games"] >= 1
    assert isinstance(info.get("plays_total"), int) and info["plays_total"] >= 0


def test_fetch_creator_profile():
    adapter, http = make()
    prof = adapter.fetch_creator(CREATOR_ID)
    assert isinstance(prof, CreatorProfile)
    assert http.calls == [f"https://remix.gg/u/{CREATOR_ID}"]
    assert prof.source == "remix" and prof.external_id == CREATOR_ID
    assert prof.name and not prof.name.startswith("@")
    assert prof.url == f"https://remix.gg/u/{CREATOR_ID}"
    assert prof.followers is None and prof.links == {}
    assert prof.avatar_url is None or prof.avatar_url.startswith("https://")


def test_fetch_creator_missing_returns_none():
    adapter, _ = make()
    assert adapter.fetch_creator("nobody-here") is None


# ---------------------------------------------------------------- fixture hygiene
@pytest.mark.parametrize("path", sorted(FIX.glob("*.html")), ids=lambda p: p.name)
def test_fixtures_are_small_and_trim_is_idempotent(path):
    html = path.read_text(encoding="utf-8")
    assert path.stat().st_size <= 200 * 1024
    again = trim_html(html)
    assert parse_flight(again).keys() == parse_flight(html).keys()
