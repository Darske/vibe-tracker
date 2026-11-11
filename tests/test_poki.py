"""Parse tests for the Poki adapter against recorded fixtures (tests/fixtures/poki/). No network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracker.models import GameRef, GameSnapshot
from tracker.sources.poki import Poki

FIX = Path(__file__).parent / "fixtures" / "poki"
PARAMS = "site=3&device=desktop&country=US"


def load(name: str):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    """Serves fixture JSON by URL pattern and records every URL asked for."""

    def __init__(self, overrides: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.requests_made = 0

    def get_json(self, url: str, **kwargs):
        self.calls.append(url)
        self.requests_made += 1
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                return payload() if callable(payload) else payload
        assert url.startswith(("https://api.poki.com/", "https://realtime.poki.com/")), url
        if "/list/game/HOME?" in url:
            return load("list_home.json")
        if "/category/popular?" in url:
            return load("list_popular.json")
        if "/category/new?" in url:
            return load("list_new.json")
        m = re.match(r"https://api\.poki\.com/game/([^?]+)\?", url)
        if m:
            f = FIX / f"game_{m.group(1)}.json"
            return json.loads(f.read_text()) if f.exists() else None  # None = 404
        if "realtime.poki.com/estimate/metrics?id=" in url:
            return load("rt_estimate.json")
        raise AssertionError(f"unexpected URL {url}")


def make(config: dict | None = None, overrides=None) -> tuple[Poki, FakeHttp]:
    http = FakeHttp(overrides)
    cfg = {"name": "poki", "lists": ["home", "popular", "new"], "top_n": 100, "rate_limit_rps": 0.33}
    cfg.update(config or {})
    return Poki(http, cfg), http


def ref(slug: str) -> GameRef:
    return GameRef(source="poki", external_id=slug, url=f"https://poki.com/en/g/{slug}")


# ---------------------------------------------------------------- discover
@pytest.mark.parametrize("list_name,fixture,key", [
    ("home", "list_home.json", "content"),
    ("popular", "list_popular.json", "games"),
    ("new", "list_new.json", "games"),
])
def test_discover_list_positions(list_name, fixture, key):
    adapter, http = make()
    out = list(adapter.discover([list_name], 100))
    rows = load(fixture)[key]
    assert len(out) == len(rows) >= 10
    assert [p.position for _, p in out] == list(range(1, len(out) + 1))
    assert [r.external_id for r, _ in out] == [row["slug"] for row in rows]
    for r, p in out:
        assert r.source == p.source == "poki"
        assert r.url == f"https://poki.com/en/g/{r.external_id}"
        assert p.list_name == list_name and p.external_id == r.external_id
        assert p.captured_at.tzinfo is not None
    # one request, pinned params, limit = top_n, API host only
    assert len(http.calls) == 1
    url = http.calls[0]
    assert url.startswith("https://api.poki.com/") and PARAMS in url and url.endswith("&limit=100")
    # list rows are cached with their numeric id and pokifordevs ids
    first = rows[0]
    assert adapter._rows[first["slug"]]["id"] == first["id"]
    assert adapter.p4d_id(first["slug"]) == first["pokifordevs_game_id"]


def test_discover_urls_per_list():
    adapter, http = make()
    list(adapter.discover(["home", "popular", "new"], 100))
    assert http.calls == [
        f"https://api.poki.com/list/game/HOME?{PARAMS}&limit=100",
        f"https://api.poki.com/category/popular?{PARAMS}&limit=100",
        f"https://api.poki.com/category/new?{PARAMS}&limit=100",
    ]


def test_discover_respects_top_n_and_skips_unknown(caplog):
    adapter, http = make()
    out = list(adapter.discover(["popular", "bogus", "new"], 5))
    assert [p.position for _, p in out if p.list_name == "popular"] == [1, 2, 3, 4, 5]
    assert [p.position for _, p in out if p.list_name == "new"] == [1, 2, 3, 4, 5]
    assert "unknown list 'bogus'" in caplog.text
    assert len(http.calls) == 2


def test_discover_dedupes_and_skips_rows_without_slug():
    payload = {"games": [{"slug": "a"}, {"slug": ""}, {"title": "no slug"}, {"slug": "a"}, {"slug": "b"}]}
    adapter, _ = make(overrides={r"/category/new\?": payload})
    out = list(adapter.discover(["new"], 100))
    assert [(r.external_id, p.position) for r, p in out] == [("a", 1), ("b", 2)]


def test_discover_empty_or_missing_list():
    adapter, _ = make(overrides={r"/list/game/HOME\?": None})
    assert list(adapter.discover(["home"], 100)) == []


# ---------------------------------------------------------------- fetch
def test_fetch_maps_game_record():
    adapter, http = make()
    snaps = list(adapter.fetch([ref("retro-bowl")]))
    assert len(snaps) == 1
    s = snaps[0]
    g = load("game_retro-bowl.json")
    assert isinstance(s, GameSnapshot) and s.status == "ok"
    assert http.calls == [f"https://api.poki.com/game/retro-bowl?{PARAMS}"]
    assert s.external_id == "retro-bowl" and s.url == "https://poki.com/en/g/retro-bowl"
    assert s.title == g["title"] == "Retro Bowl"
    assert s.likes == g["rating"]["up_count"]
    assert s.votes == g["rating"]["up_count"] + g["rating"]["down_count"]
    assert s.votes > s.likes > 0
    assert s.published_at is not None and s.published_at.isoformat() == "2020-05-01T00:00:00+00:00"
    assert s.creator_name == g["developer"] == "New Star Games"
    assert s.creator_external_id == g["pokifordevs_team_id"]
    assert s.creator_url is None
    assert s.thumbnail_url == "https://img.poki-cdn.com/cdn-cgi/image/q=78,scq=50,width=314,height=314,fit=cover,f=auto/" + g["image_url"]
    assert s.tags and "popular" not in s.tags and "new" not in s.tags
    assert set(s.tags) <= {c["slug"] for c in g["categories"]}
    for field in ("plays_total", "plays_24h", "players_total", "players_now", "remixes", "comments", "followers"):
        assert getattr(s, field) is None, field
    assert s.ai_disclosure is None


def test_fetch_new_release_and_cache():
    adapter, _ = make()
    list(adapter.discover(["new"], 100))
    s = list(adapter.fetch([ref("country-balls-world-war")]))[0]
    g = load("game_country-balls-world-war.json")
    assert s.likes == g["rating"]["up_count"]
    assert s.votes == g["rating"]["up_count"] + g["rating"]["down_count"]
    assert s.published_at.year == 2026 and s.published_at.tzinfo is not None
    assert adapter._games["country-balls-world-war"]["id"] == g["id"]
    assert adapter.p4d_id("country-balls-world-war") == g["pokifordevs_game_id"]


def test_fetch_one_request_per_game_no_realtime_by_default():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(["popular"], 3)]
    n = len(http.calls)
    snaps = list(adapter.fetch(refs + [ref("retro-bowl")]))
    calls = http.calls[n:]
    assert len(calls) == len(snaps) == 4
    assert all(c.startswith("https://api.poki.com/game/") for c in calls)
    assert not any("realtime.poki.com" in c for c in http.calls)
    assert all(s.players_now is None for s in snaps)


def test_fetch_404_is_gone():
    adapter, _ = make()
    s = list(adapter.fetch([ref("does-not-exist")]))[0]
    assert s.status == "gone" and s.external_id == "does-not-exist"
    assert s.url == "https://poki.com/en/g/does-not-exist"
    assert s.likes is None and s.votes is None


def test_missing_rating_and_release_date_stay_none():
    bare = {"slug": "bare", "title": "Bare", "developers": ["Some Studio Ltd."], "release_date": None}
    adapter, _ = make(overrides={r"/game/bare\?": bare})
    s = list(adapter.fetch([ref("bare")]))[0]
    assert s.status == "ok" and s.title == "Bare"
    assert s.likes is None and s.votes is None and s.published_at is None
    assert s.creator_name == "Some Studio Ltd."
    assert s.creator_external_id == "some-studio-ltd"  # slugified developer when no pokifordevs_team_id
    assert s.thumbnail_url is None and s.tags == []

    # a partial rating: likes kept, votes unknown (never a half sum)
    half = {"slug": "half", "title": "Half", "rating": {"up_count": 7}}
    adapter2, _ = make(overrides={r"/game/half\?": half})
    s2 = list(adapter2.fetch([ref("half")]))[0]
    assert s2.likes == 7 and s2.votes is None

    # zeros are real zeros
    zero = {"slug": "zero", "title": "Zero", "rating": {"up_count": 0, "down_count": 0, "rating": 0}}
    adapter3, _ = make(overrides={r"/game/zero\?": zero})
    s3 = list(adapter3.fetch([ref("zero")]))[0]
    assert s3.likes == 0 and s3.votes == 0


# ---------------------------------------------------------------- optional live players
def test_live_players_opt_in():
    adapter, http = make({"live_players": True})
    s = list(adapter.fetch([ref("retro-bowl")]))[0]
    g = load("game_retro-bowl.json")
    assert http.calls[-1] == f"https://realtime.poki.com/estimate/metrics?id={g['pokifordevs_game_id']}"
    assert s.players_now == load("rt_estimate.json")["users"]
    assert s.plays_total is None and s.players_total is None


def test_live_players_zero_or_error_is_none():
    adapter, _ = make({"live_players": True}, overrides={r"estimate/metrics": {"users": 0}})
    assert list(adapter.fetch([ref("retro-bowl")]))[0].players_now is None

    def boom():
        raise RuntimeError("realtime down")

    adapter2, _ = make({"live_players": True}, overrides={r"estimate/metrics": boom})
    s = list(adapter2.fetch([ref("retro-bowl")]))[0]
    assert s.status == "ok" and s.players_now is None and s.likes is not None


def test_live_players_off_by_default():
    adapter, _ = make()
    assert adapter.live_players is False


# ---------------------------------------------------------------- record_fixtures
def test_record_fixtures_writes_what_the_parser_reads(tmp_path):
    adapter, http = make()
    written = adapter.record_fixtures(tmp_path, 20)
    names = {p.name for p in written}
    assert {"list_home.json", "list_popular.json", "list_new.json"} <= names
    assert any(n.startswith("game_") for n in names)
    assert "rt_estimate.json" not in names  # no realtime call unless live_players is on
    assert all("limit=20" in u for u in http.calls if "/game/" not in u)


@pytest.mark.parametrize("name", ["list_home.json", "list_popular.json", "list_new.json",
                                  "game_retro-bowl.json", "game_country-balls-world-war.json", "rt_estimate.json"])
def test_fixtures_are_small(name):
    assert (FIX / name).stat().st_size <= 200 * 1024
