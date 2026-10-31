"""Parse tests for the Playgama AI adapter against recorded fixtures (tests/fixtures/playgama_ai/)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracker.models import GameRef, GameSnapshot
from tracker.sources.playgama_ai import (
    PlaygamaAI,
    agent_slug,
    parse_catalog,
    parse_game_page,
    parse_measure,
    parse_number,
    trim_catalog_html,
    trim_game_html,
)

FIX = Path(__file__).parent / "fixtures" / "playgama_ai"
GAME_FIXTURE = next(FIX.glob("play_*.html"))
GAME_ID = GAME_FIXTURE.name[len("play_"):-len(".html")]
LISTS = ["plays24h", "playsAllTime", "likesAllTime", "freshDrops"]
CFG = {"name": "playgama_ai", "lists": LISTS, "top_n": 100, "rate_limit_rps": 1}

CARD = (
    '<li class="top-tabs__item"><a class="top-snippet" href="/play/{id}" data-top-site-id="{id}"{attr}>'
    '<span class="top-snippet__rank">{rank}</span><img class="top-snippet__cover" src="https://static.playgama.com/x/{id}.png">'
    '<span class="top-snippet__title">{title}</span><span class="top-snippet__stats"><span class="top-snippet__measure">{measure}</span>'
    '{agent}</span></a></li>'
)


def card(id_, rank, title, measure, *, plays24h=None, agent=None) -> str:
    return CARD.format(
        id=id_, rank=rank, title=title, measure=measure,
        attr=f' data-top-plays24h="{plays24h}"' if plays24h is not None else "",
        agent=f'<span class="top-snippet__agent"><svg></svg>{agent}</span>' if agent else "",
    )


def panel(tab: str, cards: list[str]) -> str:
    return f'<ol class="top-tabs__panel" role="tabpanel" data-top-panel="{tab}">{"".join(cards)}</ol>'


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.text = body
        self.url = url

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        self.requests_made += 1
        assert "developer.playgama.com" not in url, "developer.playgama.com/api is robots-disallowed"
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                if callable(payload):
                    payload = payload()
                if isinstance(payload, int):
                    return FakeResponse(payload, "", url)
                return FakeResponse(200, payload, url)
        if url == "https://playgama.ai/play":
            return FakeResponse(200, load("play.html"), url)
        if url == f"https://playgama.ai/play/{GAME_ID}":
            return FakeResponse(200, load(GAME_FIXTURE.name), url)
        if url.startswith("https://playgama.ai/play/"):
            return FakeResponse(404, "<html>not found</html>", url)
        if url == "https://playgama.ai/robots.txt":
            return FakeResponse(200, load("robots.txt"), url)
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None, **cfg) -> tuple[PlaygamaAI, FakeHttp]:
    http = FakeHttp(overrides)
    return PlaygamaAI(http, {**CFG, **cfg}), http


# ---------------------------------------------------------------- parsers
def test_number_and_measure_parsing():
    assert parse_number("960") == 960 and parse_number("1,007") == 1007
    assert parse_number("2.5k") == 2500 and parse_number("2k") == 2000 and parse_number("1.2m") == 1_200_000
    assert parse_number("") is None and parse_number(None) is None and parse_number("n/a") is None
    assert parse_measure("960 played") == {"plays": 960}
    assert parse_measure("2k likes") == {"likes": 2000}
    assert parse_measure("1k played, 227 likes") == {"plays": 1000, "likes": 227}
    assert parse_measure("33m 18s avg") == {"playtime_sec": 33 * 60 + 18}
    assert parse_measure(None) == {}
    assert agent_slug("Claude Code") == "claude-code" and agent_slug("Google Antigravity") == "antigravity"
    assert agent_slug("Claude app") == "claude-app" and agent_slug("Some New Tool") == "some-new-tool" and agent_slug(None) is None


def test_parse_catalog_real_fixture():
    tabs = parse_catalog(load("play.html"))
    for tab in LISTS:
        assert tab in tabs and len(tabs[tab]) >= 5, tab
    first = tabs["plays24h"][0]
    assert first["rank"] == 1 and first["id"] and first["title"]
    assert isinstance(first["plays24h"], int) and first["plays24h"] >= 0
    assert first["thumbnail_url"].startswith("https://")
    assert first["agent"]  # the 24h chart shows the agent chip
    assert all(c["plays24h"] is None for c in tabs["likesAllTime"])  # the Most Liked cards carry no 24h attribute
    assert all(c.get("likes") is not None for c in tabs["likesAllTime"])
    assert all(c.get("plays") is not None for c in tabs["playsAllTime"])
    # ranks are 1..n in document order
    assert [c["rank"] for c in tabs["plays24h"]] == list(range(1, len(tabs["plays24h"]) + 1))


def test_parse_catalog_prefers_populated_panel_and_ignores_buttons():
    html = (
        panel("plays24h", [card("a1", 1, "A", "10 played", plays24h=10, agent="Codex"), '<li class="top-tabs__item"><button>Top 100 games</button></li>'])
        + panel("plays24h", ['<li class="top-tabs__item" data-top-stub><span class="top-snippet__rank"></span></li>'])
        + panel("challenge", [card("a1", 1, "A", "1k played, 5 likes", plays24h=1006)])
    )
    tabs = parse_catalog(html)
    assert [c["id"] for c in tabs["plays24h"]] == ["a1"]
    assert tabs["plays24h"][0]["agent"] == "codex"
    assert tabs["challenge"][0]["plays24h"] is None and tabs["challenge"][0]["likes"] == 5  # challenge attr is all-time


def test_parse_game_page_real_fixture_and_trim():
    html = load(GAME_FIXTURE.name)
    page = parse_game_page(html)
    assert page["id"] == GAME_ID and page["title"]
    assert isinstance(page["plays_total"], int) and page["plays_total"] > 0
    assert page["thumbnail_url"].startswith("https://")
    assert parse_game_page(trim_game_html(html)) == page
    assert parse_game_page("<html><body>nothing</body></html>") is None
    trimmed = trim_catalog_html(load("play.html"), LISTS)
    assert len(trimmed) <= 200 * 1024
    assert {k: len(v) for k, v in parse_catalog(trimmed).items() if k in LISTS} == {k: len(v) for k, v in parse_catalog(load("play.html")).items() if k in LISTS}


# ---------------------------------------------------------------- discover
def test_discover_positions_start_at_one_and_catalog_fetched_once():
    adapter, http = make()
    out = list(adapter.discover(LISTS, 100))
    assert http.calls == ["https://playgama.ai/play"]
    for name in LISTS:
        rows = [p for _, p in out if p.list_name == name]
        assert len(rows) >= 5, name
        assert [p.position for p in rows] == list(range(1, len(rows) + 1))
    for ref, p in out:
        assert ref.source == "playgama_ai" and p.source == "playgama_ai"
        assert ref.url == f"https://playgama.ai/play/{ref.external_id}" and p.external_id == ref.external_id
        assert p.captured_at.tzinfo is not None
    assert len(adapter._cards) >= 5


def test_discover_respects_top_n_and_unknown_list(caplog):
    adapter, _ = make()
    out = list(adapter.discover(["plays24h", "bogus"], 3))
    assert [p.position for _, p in out] == [1, 2, 3]
    assert "unknown list 'bogus'" in caplog.text


# ---------------------------------------------------------------- fetch
def test_fetch_merges_card_and_game_page():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(LISTS, 100)]
    # record_fixtures saves the game page of the first card of the first tab, so it is on a tab
    ref = next(r for r in refs if r.external_id == GAME_ID)
    n = len(http.calls)
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1 and http.calls[n:] == [ref.url]  # one GET /play/<id> per ref
    s = snaps[0]
    c = adapter._cards[ref.external_id]
    assert isinstance(s, GameSnapshot) and s.status == "ok"
    assert s.title == c["title"] and s.thumbnail_url == c["thumbnail_url"]
    assert s.plays_24h == c["plays24h"] and isinstance(s.plays_24h, int)
    assert s.ai_tool == c["agent"] and s.ai_tool
    assert s.plays_total == parse_game_page(load(GAME_FIXTURE.name))["plays_total"]  # exact count from the page
    assert s.likes == c.get("likes")  # only set when the game is on the Most Liked / Challenge tab
    assert s.creator_external_id is None and s.creator_name is None and s.creator_url is None
    assert s.players_now is None and s.players_total is None and s.engine is None and s.published_at is None


def test_fetch_uncached_ref_uses_game_page():
    adapter, http = make()
    ref = GameRef(source="playgama_ai", external_id=GAME_ID, url=f"https://playgama.ai/play/{GAME_ID}")
    s = list(adapter.fetch([ref]))[0]
    assert http.calls == [ref.url]
    page = parse_game_page(load(GAME_FIXTURE.name))
    assert s.status == "ok" and s.title == page["title"] and s.plays_total == page["plays_total"]
    assert s.thumbnail_url == page["thumbnail_url"]
    assert s.plays_24h is None and s.likes is None and s.ai_tool is None  # not on any tab in this run


def test_fetch_404_is_gone():
    adapter, _ = make()
    ref = GameRef(source="playgama_ai", external_id="zzz-not-there", url="https://playgama.ai/play/zzz-not-there")
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "gone" and s.external_id == "zzz-not-there" and s.url == ref.url
    assert s.plays_total is None and s.plays_24h is None and s.likes is None


def test_missing_counters_stay_none_not_zero():
    html = panel("plays24h", [card("bare", 1, "Bare", "")]) + panel("playsAllTime", [card("zero", 1, "Zero", "0 played", plays24h=0)])
    adapter, _ = make({r"/play$": html, r"/play/(bare|zero)$": "<html><div data-game-id='x'></div></html>"})
    refs = [r for r, _ in adapter.discover(["plays24h", "playsAllTime"], 10)]
    snaps = {s.external_id: s for s in adapter.fetch(refs)}
    bare = snaps["bare"]
    assert bare.status == "ok" and bare.title == "Bare"
    for field in ("plays_total", "plays_24h", "players_now", "players_total", "likes", "remixes", "comments", "votes", "followers"):
        assert getattr(bare, field) is None, field
    assert bare.ai_tool is None
    zero = snaps["zero"]
    assert zero.plays_24h == 0 and zero.plays_total == 0  # real zeros stay zeros (card fallback when the page has no count)


def test_fetch_without_game_pages_uses_card_figures_only():
    html = panel("playsAllTime", [card("g1", 1, "G1", "2.5k played", plays24h=3, agent="Claude Code")]) + panel("likesAllTime", [card("g1", 1, "G1", "2k likes")])
    adapter, http = make({r"/play$": html}, game_pages=False)
    refs = [r for r, _ in adapter.discover(["playsAllTime", "likesAllTime"], 10)]
    s = list(adapter.fetch(refs))[0]
    assert http.calls == ["https://playgama.ai/play"]  # no per-game requests
    assert s.plays_total == 2500 and s.plays_24h == 3 and s.likes == 2000 and s.ai_tool == "claude-code"


def test_page_count_below_24h_is_floored_to_24h():
    html = panel("plays24h", [card("lag", 1, "Lag", "695 played", plays24h=695)])
    adapter, _ = make({r"/play$": html, r"/play/lag$": '<div data-game-id="lag" data-game-title="Lag"></div><span class="header__played">675 played</span>'})
    refs = [r for r, _ in adapter.discover(["plays24h"], 10)]
    s = list(adapter.fetch(refs))[0]
    assert s.plays_24h == 695 and s.plays_total == 695  # all-time can never be below the 24h count


# ---------------------------------------------------------------- creators / fixtures
def test_fetch_creator_is_none():
    adapter, http = make()
    assert adapter.fetch_creator("anyone") is None and http.calls == []


def test_robots_fixture_has_no_disallow():
    assert "disallow" not in load("robots.txt").lower()


def test_ai_index_fixture_is_aggregate_only():
    d = json.loads(load("ai_game_index.json"))
    assert d["totals"]["games"] > 0 and any(c["key"] == "claude-code" for c in d["agent"]["categories"])


@pytest.mark.parametrize("name", sorted(p.name for p in FIX.iterdir()))
def test_fixtures_are_small(name):
    assert (FIX / name).stat().st_size <= 200 * 1024
