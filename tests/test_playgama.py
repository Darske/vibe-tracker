"""Parse tests for the Playgama adapter against recorded fixtures (tests/fixtures/playgama/)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tracker.models import CreatorProfile, GameRef, GameSnapshot
from tracker.sources.playgama import (
    Playgama,
    extract_state,
    parse_category_page,
    parse_devalue,
    parse_game_page,
    trim_html,
)

FIX = Path(__file__).parent / "fixtures" / "playgama"
GAME_FIXTURE = next(FIX.glob("game_*.html"))
GAME_HRU = GAME_FIXTURE.name[len("game_"):-len(".html")]
DEV_FIXTURE = next(FIX.glob("developer_*.html"))
DEV_ID = DEV_FIXTURE.name[len("developer_"):-len(".html")]
CFG = {"name": "playgama", "lists": ["top_playgama", "trending_now", "new"], "top_n": 100, "rate_limit_rps": 1}


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def state_page(queries: list[tuple[list, object]]) -> str:
    """Build a minimal page whose window.__query holds the given (queryKey, data) pairs (plain literal form)."""
    import json

    payload = {"mutations": [], "queries": [{"queryKey": k, "state": {"data": d}} for k, d in queries]}
    return f"<html><body><script>window.__query={json.dumps(payload)};window.__apiRequests=[]</script></body></html>"


class FakeResponse:
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.text = body
        self.url = url


class FakeHttp:
    """Serves fixture HTML by URL; `overrides` maps a regex to a body (str), a status code (int) or a callable."""

    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        self.requests_made += 1
        assert "/api/" not in url, f"adapter must never request /api/ on playgama.com: {url}"
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                if callable(payload):
                    payload = payload()
                if isinstance(payload, int):
                    return FakeResponse(payload, "", url)
                return FakeResponse(200, payload, url)
        m = re.match(r"https://playgama\.com/category/([^?]+)(?:\?page=(\d+))?$", url)
        if m:
            cat, page = m.group(1), int(m.group(2) or 1)
            if cat == "top_playgama" and page == 1:
                return FakeResponse(200, load("category_top_playgama_p1.html"), url)
            if cat == "trending_now" and page == 1:
                return FakeResponse(200, load("category_trending_now_p1.html"), url)
            if cat in ("top_playgama", "trending_now") and page == 2:
                # a short second page: the page-1 fixture with 5 games, page meta from the real fixture
                _, games = parse_category_page(load(f"category_{cat}_p1.html"))
                tuples = [[g["hru"] + "-p2", g["title"], g["preview"], g["icon"], "", g["id"], False, None, g["badges"], "playgama", g["platforms"], None] for g in games[:5]]
                return FakeResponse(200, state_page([(["category-games-data", cat, "category", 2], {"games": tuples, "category": {"id": cat, "totalPages": 2, "total": 50}})]), url)
            return FakeResponse(404, "", url)
        if url == f"https://playgama.com/game/{GAME_HRU}":
            return FakeResponse(200, load(GAME_FIXTURE.name), url)
        if url.startswith("https://playgama.com/game/"):
            return FakeResponse(404, "<html>not found</html>", url)
        if url == f"https://playgama.com/developer/{DEV_ID}":
            return FakeResponse(200, load(DEV_FIXTURE.name), url)
        if url.startswith("https://playgama.com/developer/"):
            return FakeResponse(404, "", url)
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None) -> tuple[Playgama, FakeHttp]:
    http = FakeHttp(overrides)
    return Playgama(http, CFG), http


# ---------------------------------------------------------------- devalue parser
def test_parse_devalue_hoisted_function_form():
    src = '(function(a,b,c){return {x:a,y:[a,b,c,void 0,-0,1.5e3,"q\\u002Fz\\"s"],z:{k:b,"quoted key":null,n:true}}}("hru",42,[a_ref_free]))'
    src = src.replace("[a_ref_free]", "[1,2]")
    out = parse_devalue(src)
    assert out == {"x": "hru", "y": ["hru", 42, [1, 2], None, 0, 1500.0, 'q/z"s'], "z": {"k": 42, "quoted key": None, "n": True}}
    # args can reference other params and nest
    out2 = parse_devalue('(function(a,b){return {g:b}}("deadly",{hru:a,list:[a,a]}))')
    assert out2 == {"g": {"hru": "deadly", "list": ["deadly", "deadly"]}}
    # plain literal form and the `})(` call variant
    assert parse_devalue('{"a":[1,{"b":"c"}]}') == {"a": [1, {"b": "c"}]}
    assert parse_devalue("(function(a){return [a,a]})(7)") == [7, 7]


def test_extract_state_from_real_page_and_trimmed_copy():
    html = load("category_top_playgama_p1.html")
    state = extract_state(html)
    assert isinstance(state, dict) and state.get("queries")
    keys = [q["queryKey"][0] for q in state["queries"]]
    assert "category-games-data" in keys
    assert extract_state(trim_html(html)) == state
    assert extract_state("<html>no state here</html>") is None


# ---------------------------------------------------------------- discover
def test_discover_top_playgama_positions_start_at_one():
    adapter, http = make()
    out = list(adapter.discover(["top_playgama"], 45))
    assert len(out) >= 5
    placements = [p for _, p in out]
    assert placements[0].position == 1
    assert [p.position for p in placements] == list(range(1, len(placements) + 1))
    for ref, p in out:
        assert ref.source == "playgama" and p.source == "playgama"
        assert ref.external_id and ref.url == f"https://playgama.com/game/{ref.external_id}"
        assert p.list_name == "top_playgama" and p.external_id == ref.external_id
        assert p.captured_at.tzinfo is not None
    assert len({r.external_id for r, _ in out}) == len(out)
    assert http.calls == ["https://playgama.com/category/top_playgama"]  # page 1 has no ?page=
    assert adapter._games[out[0][0].external_id]["title"]


def test_discover_pages_until_top_n_and_positions_continue_across_pages():
    adapter, http = make()
    out = list(adapter.discover(["top_playgama", "trending_now"], 48))
    assert any(u.endswith("/category/top_playgama?page=2") for u in http.calls)
    top = [p for _, p in out if p.list_name == "top_playgama"]
    assert len(top) == 48 and [p.position for p in top] == list(range(1, 49))
    assert top[45].external_id.endswith("-p2")  # position 46 comes from page 2
    trending = [p for _, p in out if p.list_name == "trending_now"]
    assert trending and trending[0].position == 1 and len(trending) == 48


def test_discover_respects_top_n_and_skips_missing_list(caplog):
    adapter, http = make()
    out = list(adapter.discover(["top_playgama", "nope"], 3))
    assert [p.position for _, p in out] == [1, 2, 3]
    assert not any("page=2" in u for u in http.calls)
    assert "not found" in caplog.text


# ---------------------------------------------------------------- fetch
def test_fetch_maps_game_page_fields():
    adapter, http = make()
    ref = GameRef(source="playgama", external_id=GAME_HRU, url=f"https://playgama.com/game/{GAME_HRU}")
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1
    s = snaps[0]
    assert http.calls == [ref.url]  # exactly one request per game
    page = parse_game_page(load(GAME_FIXTURE.name))
    g = page["game"]
    assert isinstance(s, GameSnapshot) and s.status == "ok"
    assert s.title == g["title"] and s.url == ref.url
    assert s.votes == g["rating"]["count"] and s.likes == s.votes  # the rating widget is the like icon
    assert s.players_total == page["leaderboard"]["totalPlayers"]
    assert s.plays_total is None and s.players_now is None and s.plays_24h is None
    assert s.creator_external_id == str(g["developer_id"]) and s.creator_name == g["developer"]
    assert s.creator_url == f"https://playgama.com/developer/{g['developer_id']}"
    assert s.thumbnail_url == (g.get("icon") or g.get("preview")) and s.thumbnail_url.startswith("https://")
    assert s.engine == g["engine"][0].lower()
    assert s.tags == g["tags"] and all(isinstance(t, str) for t in s.tags)
    assert s.published_at is not None and s.published_at.tzinfo is not None
    assert s.published_at.date().isoformat() == g["createdAt"][:10]


def test_fetch_404_is_gone():
    adapter, _ = make()
    ref = GameRef(source="playgama", external_id="does-not-exist", url="https://playgama.com/game/does-not-exist")
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "gone" and s.external_id == "does-not-exist" and s.url == ref.url
    assert s.votes is None and s.players_total is None and s.likes is None


def test_missing_counters_stay_none_not_zero():
    bare = state_page([(["game-data", "bare", "en-US"], {"status": "ok", "data": {"game": {"hru": "bare", "title": "Bare"}, "leaderboard": {}}})])
    adapter, _ = make({r"/game/bare$": bare})
    s = list(adapter.fetch([GameRef(source="playgama", external_id="bare", url="https://playgama.com/game/bare")]))[0]
    assert s.status == "ok" and s.title == "Bare"
    for field in ("plays_total", "plays_24h", "players_total", "players_now", "likes", "remixes", "comments", "votes", "followers"):
        assert getattr(s, field) is None, field
    assert s.creator_external_id is None and s.creator_url is None and s.engine is None
    assert s.tags == [] and s.published_at is None and s.thumbnail_url is None

    # explicit zero / null counters: 0 stays 0, null stays None
    zero = state_page([(["game-data", "zero", "en-US"], {"status": "ok", "data": {
        "game": {"hru": "zero", "title": "Zero", "rating": {"count": 0, "value": None}, "developer_id": 5, "developer": "D", "engine": []},
        "leaderboard": {"totalPlayers": None}}})])
    adapter2, _ = make({r"/game/zero$": zero})
    s2 = list(adapter2.fetch([GameRef(source="playgama", external_id="zero", url="https://playgama.com/game/zero")]))[0]
    assert s2.votes == 0 and s2.likes == 0 and s2.players_total is None and s2.engine is None
    assert s2.creator_external_id == "5" and s2.creator_url == "https://playgama.com/developer/5"


def test_fetch_page_without_state_keeps_cached_title():
    adapter, _ = make({r"/game/odd$": "<html><body>rendered without window.__query</body></html>"})
    adapter._games["odd"] = {"hru": "odd", "title": "Odd", "icon": "https://static.playgama.com/i.png", "preview": None}
    s = list(adapter.fetch([GameRef(source="playgama", external_id="odd", url="https://playgama.com/game/odd")]))[0]
    assert s.status == "ok" and s.title == "Odd" and s.thumbnail_url == "https://static.playgama.com/i.png"
    assert s.votes is None and s.players_total is None


# ---------------------------------------------------------------- creators
def test_fetch_creator_profile():
    adapter, http = make()
    prof = adapter.fetch_creator(DEV_ID)
    assert isinstance(prof, CreatorProfile)
    assert prof.source == "playgama" and prof.external_id == DEV_ID
    assert prof.url == f"https://playgama.com/developer/{DEV_ID}"
    assert prof.name and prof.name != DEV_ID
    assert prof.followers is None and prof.links == {}
    assert prof.bio and re.search(r"\d+ games on Playgama", prof.bio)
    assert http.calls == [prof.url]


def test_fetch_creator_missing_returns_none():
    adapter, _ = make()
    assert adapter.fetch_creator("999999999") is None


# ---------------------------------------------------------------- fixture hygiene
@pytest.mark.parametrize("path", sorted(FIX.glob("*.html")), ids=lambda p: p.name)
def test_fixtures_are_small_and_parse(path: Path):
    assert path.stat().st_size <= 200 * 1024
    assert extract_state(path.read_text(encoding="utf-8")) is not None
