"""Parse tests for the itch.io adapter against fixtures in tests/fixtures/itchio/.

Fixtures were built from the research samples saved on 2026-11-10 (research/itchio/: html5.html,
newpop.html, toprated.html, topsellers.html, ai_assisted.html, cnt_ai-code_html5.html, noai.html,
newest.html, html5_p2.json, html5_429_try1.html), trimmed with itchio.trim_html to the results header and
the game cells — the same shape record_fixtures() writes — rather than re-fetched, to spare the rate limit."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from tracker.http import Blocked
from tracker.models import GameRef
from tracker.sources.itchio import (
    LIST_PATHS,
    ItchIo,
    catalogue_totals,
    list_url,
    parse_cell,
    parse_cells,
    parse_json_page,
    parse_rating_tooltip,
    parse_results_count,
    split_cells,
    trim_html,
)

FIX = Path(__file__).parent / "fixtures" / "itchio"


def load(name: str) -> str:
    return (FIX / name).read_text()


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def raise_429(url: str):
    req = httpx.Request("GET", url)
    raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req, text=load("429.html")))


class FakeHttp:
    """Serves page 1 of every list from fixtures and popular page 2 from popular_p2.json; anything else is a
    test failure (in particular no game pages, embeds or other hosts)."""

    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get(self, url: str, **kwargs):
        assert url.startswith("https://itch.io/games/"), f"unexpected URL {url}"
        assert "/embed" not in url
        self.calls.append(url)
        self.requests_made += 1
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                return payload(url) if callable(payload) else payload
        for name, path in LIST_PATHS.items():
            if url == "https://itch.io" + path:
                return FakeResponse(load(f"{name}.html"))
        if url == "https://itch.io/games/html5?page=2&format=json":
            return FakeResponse(load("popular_p2.json"))
        if re.search(r"\?page=\d+&format=json$", url):
            return FakeResponse(json.dumps({"num_items": 0, "content": "", "page": 9}))
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None, lists=None, top_n=36):
    http = FakeHttp(overrides)
    sleeps: list[float] = []
    adapter = ItchIo(http, {"name": "itchio", "lists": lists or ["popular"], "top_n": top_n}, sleep=sleeps.append)
    return adapter, http, sleeps


# ---------------------------------------------------------------- cell parsing
def test_list_fixtures_have_36_cells_each():
    for name in LIST_PATHS:
        cells = parse_cells(load(f"{name}.html"))
        assert len(cells) == 36, name
        assert len({c["game_id"] for c in cells}) == 36
        for c in cells:
            assert re.fullmatch(r"\d+", c["game_id"])
            assert re.fullmatch(r"https://[a-z0-9-]+\.itch\.io/[^/?#]+", c["url"]), c["url"]
            assert c["title"]
            assert c["author_id"] and c["author_id"].isdigit()
            assert re.fullmatch(r"https://[a-z0-9-]+\.itch\.io", c["author_url"])


def test_top_rated_first_cell_fields():
    c = parse_cells(load("top_rated.html"))[0]
    assert c["game_id"] == "3631685"
    assert c["url"] == "https://garula.itch.io/the-freak-circus"
    assert c["title"] == "The Freak Circus"
    assert c["author_id"] == "12967091" and c["author_name"] == "Garula"
    assert c["author_url"] == "https://garula.itch.io"
    assert c["genre"] == "Visual Novel"
    assert c["thumbnail"].startswith("https://img.itch.zone/")
    assert (c["rating_avg"], c["rating_count"]) == (4.89, 11663)


def test_price_parsed_on_paid_games():
    prices = [c["price"] for c in parse_cells(load("top_sellers.html")) if c.get("price")]
    assert prices and all(re.search(r"\d", p) for p in prices)


CELL_A = (
    '<div dir="auto" class="game_cell has_cover lazy_images" data-game_id="42">'
    '<div class="game_thumb"><a data-label="game:42:thumb" href="https://foo-bar.itch.io/my-game/" class="thumb_link game_link">'
    '<img height="250" data-lazy_src="https://img.itch.zone/abc/315x250.png" width="315"/></a></div>'
    '<div class="game_cell_data"><div class="game_title"><a href="https://foo-bar.itch.io/my-game/" data-label="game:42:title" class="title game_link">My &amp; Game</a>'
    '<div class="price_tag meta_tag"><div class="price_value">$2.50</div></div></div>'
    '<div class="game_author"><a data-label="user:777" href="https://foo-bar.itch.io">Foo Bar</a></div>'
    '<div><div tabindex="0" data-tooltip="3.5 average rating from 1,234 total ratings" class="game_rating"></div></div>'
    '<div class="game_genre">Puzzle</div></div></div>'
)
# same cell, every attribute list reordered, img src instead of data-lazy_src
CELL_B = (
    '<div data-game_id="42" class="game_cell" dir="auto">'
    '<div class="game_thumb"><a class="thumb_link game_link" href="https://foo-bar.itch.io/my-game" data-label="game:42:thumb">'
    '<img src="https://img.itch.zone/abc/315x250.png" width="315" height="250"></a></div>'
    '<div class="game_cell_data"><div class="game_title"><a class="title game_link" data-label="game:42:title" href="https://foo-bar.itch.io/my-game">My &amp; Game</a>'
    '<div class="price_tag meta_tag"><div class="price_value">$2.50</div></div></div>'
    '<div class="game_author"><a href="https://foo-bar.itch.io" data-action="game_grid" data-label="user:777">Foo Bar</a></div>'
    '<div><div class="game_rating" data-tooltip="3.5 average rating from 1,234 total ratings" tabindex="0"></div></div>'
    '<div class="game_genre">Puzzle</div></div></div>'
)


def test_cell_attribute_order_does_not_matter():
    a, b = parse_cell(CELL_A), parse_cell(CELL_B)
    assert a == b
    assert a["game_id"] == "42"
    assert a["url"] == "https://foo-bar.itch.io/my-game"  # canonical, trailing slash stripped
    assert a["title"] == "My & Game"
    assert a["author_id"] == "777" and a["author_name"] == "Foo Bar"
    assert a["thumbnail"] == "https://img.itch.zone/abc/315x250.png"
    assert a["price"] == "$2.50" and a["genre"] == "Puzzle"
    assert a["rating_count"] == 1234


def test_split_cells_balances_nested_divs():
    html = "<div class='x'>" + CELL_A + CELL_B + "</div><div>after</div>"
    cells = split_cells(html)
    assert len(cells) == 2 and cells[0] == CELL_A and cells[1] == CELL_B


def test_cell_without_id_or_link_is_dropped():
    assert parse_cell('<div class="game_cell"><a class="title game_link" href="https://a.itch.io/b">x</a></div>') is None
    assert parse_cell('<div class="game_cell" data-game_id="5"><div class="game_title">x</div></div>') is None


def test_rating_tooltip():
    assert parse_rating_tooltip("4.89 average rating from 11,663 total ratings") == (4.89, 11663)
    assert parse_rating_tooltip("5.00 average rating from 1 total rating") == (5.0, 1)
    assert parse_rating_tooltip("no ratings") == (None, None)
    assert parse_rating_tooltip(None) == (None, None)


def test_results_count():
    assert parse_results_count(load("popular.html")) == 857715
    assert parse_results_count(load("ai_popular.html")) == 90742
    assert parse_results_count(load("no_ai.html")) == 217240
    assert parse_results_count('<nobr class="game_count"> (90,742 results)</nobr>') == 90742
    assert parse_results_count('<nobr class="game_count">(1 result)</nobr>') == 1
    assert parse_results_count("<html>nothing</html>") is None
    assert parse_results_count("") is None


def test_json_page():
    cells, n = parse_json_page(load("popular_p2.json"))
    assert n == 36 and len(cells) == 36
    assert cells[0]["game_id"] == "5196561" and cells[0]["url"] == "https://traumalab.itch.io/trauma-lab"


def test_trim_html_keeps_what_parser_reads():
    html = load("top_rated.html")
    assert parse_cells(trim_html(html)) == parse_cells(html)
    assert parse_results_count(trim_html(html)) == 176954
    assert len(parse_cells(trim_html(html, 5))) == 5


# ---------------------------------------------------------------- discover / pagination
def test_discover_pages_via_format_json():
    adapter, http, _ = make(lists=["popular"], top_n=100)
    rows = list(adapter.discover(["popular"], 100))
    assert http.calls == [
        "https://itch.io/games/html5",
        "https://itch.io/games/html5?page=2&format=json",
        "https://itch.io/games/html5?page=3&format=json",
    ]
    # page 3 is empty in the fake. The real page 2 repeats one page-1 game (lists shift between requests),
    # so 36 + 35 unique games, positions 1..71
    assert [p.position for _, p in rows] == list(range(1, 72))
    assert len({r.external_id for r, _ in rows}) == 71
    assert rows[36][0].external_id == "1399740"  # page-2 cell 1 (5196561) was already on page 1
    assert all(p.list_name == "popular" for _, p in rows)


def test_discover_respects_top_n_and_page_count():
    adapter, http, _ = make(top_n=10)
    rows = list(adapter.discover(["popular"], 10))
    assert len(rows) == 10 and http.calls == ["https://itch.io/games/html5"]
    adapter, http, _ = make(top_n=40)
    rows = list(adapter.discover(["popular"], 40))
    assert len(rows) == 40 and len(http.calls) == 2


def test_discover_dedupes_across_pages_and_stops_on_short_page():
    p2 = json.dumps({"num_items": 2, "page": 2, "content": split_cells(load("popular.html"))[0] + CELL_A})
    adapter, http, _ = make({r"html5\?page=2": FakeResponse(p2)}, top_n=100)
    rows = list(adapter.discover(["popular"], 100))
    assert len(rows) == 37  # the repeated cell is skipped; CELL_A gets position 37
    assert rows[-1][1].position == 37 and rows[-1][0].external_id == "42"
    assert len(http.calls) == 2  # num_items < 36 → no page 3


def test_all_lists_one_request_each_at_36():
    lists = list(LIST_PATHS)
    adapter, http, _ = make(lists=lists)
    rows = list(adapter.discover(lists, 36))
    assert http.calls == [list_url(n) for n in lists]
    assert {p.list_name for _, p in rows} == set(lists)


def test_unknown_list_and_404_are_skipped():
    adapter, http, _ = make({r"top-sellers": FakeResponse("not found", 404)})
    rows = list(adapter.discover(["nope", "top_sellers", "popular"], 36))
    assert {p.list_name for _, p in rows} == {"popular"}


def test_blocked_propagates():
    def challenge(url):
        raise Blocked(f"403 on {url}")

    adapter, _, _ = make({r"ai-code": challenge})
    with pytest.raises(Blocked):
        list(adapter.discover(["ai_code"], 36))


# ---------------------------------------------------------------- fetch / mapping
def _run(lists, top_n=36, overrides=None):
    adapter, http, sleeps = make(overrides, lists=lists, top_n=top_n)
    refs: dict[str, GameRef] = {}
    for ref, _ in adapter.discover(lists, top_n):
        refs.setdefault(ref.external_id, ref)
    return adapter, http, refs


def test_fetch_from_cache_only_and_skips_unseen():
    adapter, http, refs = _run(["popular"])
    n_calls = len(http.calls)
    extra = GameRef(source="itchio", external_id="999999999", url="https://x.itch.io/y")
    snaps = list(adapter.fetch(list(refs.values()) + [extra]))
    assert len(http.calls) == n_calls  # fetch never requests
    assert len(snaps) == 36 and all(s.external_id != "999999999" for s in snaps)
    s = snaps[0]
    assert s.status == "ok"
    assert s.plays_total is None and s.likes is None and s.players_now is None and s.players_total is None
    assert s.title and s.thumbnail_url and s.creator_external_id and s.creator_name
    assert s.creator_url.endswith(".itch.io")
    assert s.tags == [parse_cells(load("popular.html"))[0]["genre"]]


def test_votes_only_from_top_rated():
    adapter, _, refs = _run(["popular", "top_rated"])
    snaps = {s.external_id: s for s in adapter.fetch(list(refs.values()))}
    assert snaps["3631685"].votes == 11663 and snaps["3631685"].rating == 4.89
    assert all(s.rating is None for s in snaps.values() if s.votes is None)
    rated = {c["game_id"] for c in parse_cells(load("top_rated.html"))}
    assert all(s.votes is None for gid, s in snaps.items() if gid not in rated)
    assert all(snaps[g].votes for g in rated)
    # a rating seen on another list (synthetic) is ignored
    page = '<nobr class="game_count">(1 result)</nobr>' + CELL_A
    adapter, _, refs = _run(["popular"], overrides={r"games/html5$": FakeResponse(page)})
    (snap,) = adapter.fetch(list(refs.values()))
    assert snap.votes is None


def test_ai_disclosure_from_list_membership():
    adapter, _, refs = _run(["popular", "ai_popular", "ai_code", "no_ai"])
    snaps = {s.external_id: s for s in adapter.fetch(list(refs.values()))}
    ai = {c["game_id"] for n in ("ai_popular", "ai_code") for c in parse_cells(load(f"{n}.html"))}
    no_ai = {c["game_id"] for c in parse_cells(load("no_ai.html"))}
    popular = {c["game_id"] for c in parse_cells(load("popular.html"))}
    for gid, s in snaps.items():
        expected = "ai" if gid in ai else "no_ai" if gid in no_ai else None
        assert s.ai_disclosure == expected, gid
    assert popular & ai and popular & no_ai and popular - ai - no_ai  # all three cases exercised
    # popular alone says nothing about AI
    adapter, _, refs = _run(["popular"])
    assert all(s.ai_disclosure is None for s in adapter.fetch(list(refs.values())))


def test_ai_wins_over_no_ai_tag():
    page = '<nobr class="game_count">(1 result)</nobr>' + CELL_A
    adapter, _, refs = _run(["no_ai", "ai_code"], overrides={r"tag-no-ai$|ai-code/html5$": FakeResponse(page)})
    (snap,) = adapter.fetch(list(refs.values()))
    assert snap.ai_disclosure == "ai"


# ---------------------------------------------------------------- catalogue totals
def test_catalogue_totals_fetches_three_pages():
    http = FakeHttp()
    assert catalogue_totals(http, sleep=lambda s: None) == {"total": 857715, "ai_total": 90742, "no_ai_total": 217240}
    assert http.calls == [list_url("popular"), list_url("ai_popular"), list_url("no_ai")]


def test_catalogue_totals_reuses_discover_pages():
    adapter, http, _ = _run(["popular", "ai_popular"])
    n = len(http.calls)
    assert adapter.catalogue_totals() == {"total": 857715, "ai_total": 90742, "no_ai_total": 217240}
    assert http.calls[n:] == [list_url("no_ai")]


def test_catalogue_totals_missing_count_is_none():
    http = FakeHttp({r"tag-no-ai": FakeResponse("<html>changed layout</html>"), r"ai-assisted": FakeResponse("gone", 404)})
    assert catalogue_totals(http, sleep=lambda s: None) == {"total": 857715, "ai_total": None, "no_ai_total": None}


# ---------------------------------------------------------------- 429 handling
def test_429_backs_off_once_then_succeeds():
    attempts = {"n": 0}

    def flaky(url):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise_429(url)
        return FakeResponse(load("popular.html"))

    adapter, http, sleeps = make({r"games/html5$": flaky})
    rows = list(adapter.discover(["popular"], 36))
    assert len(rows) == 36 and sleeps == [90.0] and len(http.calls) == 2


def test_429_twice_gives_up_on_page_and_continues():
    adapter, http, sleeps = make({r"games/html5$": raise_429})
    rows = list(adapter.discover(["popular", "top_rated"], 36))
    assert {p.list_name for _, p in rows} == {"top_rated"}
    assert sleeps == [90.0]
    assert http.calls.count("https://itch.io/games/html5") == 2


def test_429_returned_status_is_handled_like_raised():
    adapter, http, sleeps = make({r"games/html5$": FakeResponse(load("429.html"), 429)})
    assert list(adapter.discover(["popular"], 36)) == []
    assert sleeps == [90.0]


def test_repeated_429_giveups_stop_the_run():
    adapter, http, sleeps = make({r".*": raise_429})
    lists = list(LIST_PATHS)
    assert list(adapter.discover(lists, 100)) == []
    assert len(http.calls) == 4  # two lists × (try + one retry), then no more requests
    assert sleeps == [90.0, 90.0]


def test_429_on_later_page_keeps_earlier_rows():
    adapter, http, _ = make({r"page=2": raise_429}, top_n=100)
    rows = list(adapter.discover(["popular"], 100))
    assert len(rows) == 36


def test_other_http_errors_propagate():
    def boom(url):
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))

    adapter, _, sleeps = make({r"games/html5$": boom})
    with pytest.raises(httpx.HTTPStatusError):
        list(adapter.discover(["popular"], 36))
    assert sleeps == []


# ---------------------------------------------------------------- record_fixtures
def test_record_fixtures(tmp_path):
    adapter, http, _ = make()
    written = adapter.record_fixtures(tmp_path, 5)
    assert {p.name for p in written} == {f"{n}.html" for n in LIST_PATHS} | {"popular_p2.json"}
    assert len(http.calls) == 9
    assert len(parse_cells((tmp_path / "top_rated.html").read_text())) == 5
    assert parse_results_count((tmp_path / "ai_popular.html").read_text()) == 90742
    cells, n = parse_json_page((tmp_path / "popular_p2.json").read_text())
    assert len(cells) == 5 and n == 36


def test_robots_fetched_then_one_interval_before_first_page():
    class RobotsHttp(FakeHttp):
        min_interval = 25.0

        def __init__(self):
            super().__init__()
            self.robots_checks = 0

        def allowed(self, url):
            self.robots_checks += 1
            return True

    http = RobotsHttp()
    sleeps: list[float] = []
    adapter = ItchIo(http, {"name": "itchio"}, sleep=sleeps.append)
    list(adapter.discover(["popular", "top_rated"], 36))
    assert http.robots_checks == 1 and sleeps == [25.0] and len(http.calls) == 2


def test_platform_totals_carry_ai_split():
    adapter, http, _ = _run(["popular", "ai_popular", "no_ai"])
    n = len(http.calls)
    assert adapter.platform_totals() == {"games": 857715, "extra": {"ai_total": 90742, "no_ai_total": 217240}}
    assert http.calls[n:] == []  # all three pages came from this run's discover
