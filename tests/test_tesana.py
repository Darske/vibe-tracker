"""Parse tests for the Tesana adapter against recorded fixtures (tests/fixtures/tesana/)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracker.models import CreatorProfile, GameRef, GameSnapshot
from tracker.sources.tesana import Tesana

FIX = Path(__file__).parent / "fixtures" / "tesana"


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
        if "/api/marketplace/charts/" in url:
            return load("charts_played.json")
        if "/api/marketplace/section/popular/" in url:
            data = load("section_popular_p1.json")
            if "page=1" in url:
                return data
            data["results"] = data["results"][:5]
            data["next"] = None
            return data
        if "/api/play/playing-now/top/" in url:
            return load("playing_now_top.json")
        if "/api/play/playing-now/?ids=" in url:
            return load("playing_now_ids.json")
        if "/api/games/store/body-cam/" in url:
            return load("store_body-cam.json")
        if "/api/games/store/" in url:
            return None  # 404
        if "/api/games/users/Juicer/" in url:
            return load("user_Juicer.json")
        if "/api/games/users/" in url:
            return None
        raise AssertionError(f"unexpected URL {url}")


def make(overrides=None) -> tuple[Tesana, FakeHttp]:
    http = FakeHttp(overrides)
    return Tesana(http, {"name": "tesana", "lists": ["played"], "top_n": 20}), http


# ---------------------------------------------------------------- discover
def test_discover_played_positions_start_at_one():
    adapter, http = make()
    out = list(adapter.discover(["played"], 20))
    assert len(out) >= 5
    refs = [r for r, _ in out]
    placements = [p for _, p in out]
    assert placements[0].position == 1
    assert [p.position for p in placements] == sorted(p.position for p in placements)
    for ref, p in out:
        assert ref.source == "tesana" and p.source == "tesana"
        assert ref.external_id and ref.url == f"https://tesana.com/game/{ref.external_id}"
        assert p.list_name == "played" and p.external_id == ref.external_id
        assert p.captured_at.tzinfo is not None
    assert len({r.external_id for r in refs}) == len(refs)
    assert any("metric=played" in u and "limit=20" in u for u in http.calls)


def test_discover_all_configured_lists_and_skips_unknown(caplog):
    adapter, http = make()
    lists = ["played", "trending", "new", "rising", "popular", "playing_now", "bogus"]
    out = list(adapter.discover(lists, 30))
    names = {p.list_name for _, p in out}
    assert names == set(lists) - {"bogus"}
    assert "unknown list 'bogus'" in caplog.text
    # popular pages until top_n reached: page 1 has 24, so page 2 is requested for top_n=30
    assert any("section/popular/?page=2" in u for u in http.calls)
    popular = [p for _, p in out if p.list_name == "popular"]
    assert [p.position for p in popular] == list(range(1, len(popular) + 1))
    assert len(popular) <= 30
    now = [p for _, p in out if p.list_name == "playing_now"]
    assert now and now[0].position == 1


def test_discover_respects_top_n():
    adapter, _ = make()
    out = list(adapter.discover(["played", "popular"], 3))
    for name in ("played", "popular"):
        assert len([p for _, p in out if p.list_name == name]) == 3


# ---------------------------------------------------------------- fetch
def test_fetch_uses_discover_cache_and_refreshes_players_now():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(["played"], 20)]
    n_before = len(http.calls)
    snaps = list(adapter.fetch(refs))
    assert len(snaps) >= 5 and len(snaps) == len(refs)
    # no per-game store requests for games seen during discover; one playing-now batch
    new_calls = http.calls[n_before:]
    assert not any("/api/games/store/" in u for u in new_calls)
    assert len([u for u in new_calls if "/api/play/playing-now/?ids=" in u]) == 1
    for s in snaps:
        assert isinstance(s, GameSnapshot) and s.status == "ok"
        assert s.title and s.url.startswith("https://tesana.com/game/")
        assert s.plays_total is not None or s.players_now is not None
        assert s.creator_external_id and s.creator_url == f"https://tesana.com/u/{s.creator_external_id}"
        assert s.remixes is None and s.comments is None and s.votes is None  # list objects have no such fields
    first = snaps[0]
    assert first.plays_total > 0
    assert first.players_total is not None  # players_7d
    assert first.likes is not None
    assert first.engine == "web"
    assert first.tags and all(isinstance(t, str) for t in first.tags)
    assert first.published_at is not None and first.published_at.tzinfo is not None
    # players_now came from the ids batch for the game present in it
    ids = load("playing_now_ids.json")
    for s in snaps:
        pid = adapter._public_ids.get(s.external_id)
        if pid in ids:
            assert s.players_now == ids[pid]


def test_fetch_store_record_for_uncached_ref():
    adapter, http = make()
    ref = GameRef(source="tesana", external_id="body-cam", url="https://tesana.com/game/body-cam")
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1
    s = snaps[0]
    assert any("/api/games/store/body-cam/" in u for u in http.calls)
    assert s.status == "ok" and s.title == "BODY-CAM"
    assert s.plays_total is not None and s.plays_total > 0
    assert s.creator_external_id == "Juicer"  # from creator.username (store has user: null)
    assert s.creator_url == "https://tesana.com/u/Juicer"
    store = load("store_body-cam.json")
    assert s.comments == store["message_count"]
    assert s.votes == store["verdict"]["total"]
    assert s.remixes == store.get("remix_count")  # None in the fixture, must stay None
    assert s.likes == store["favorite_count"]
    assert s.thumbnail_url == store["cover_url"]


def test_fetch_404_is_gone():
    adapter, _ = make()
    ref = GameRef(source="tesana", external_id="does-not-exist", url="https://tesana.com/game/does-not-exist")
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "gone" and s.external_id == "does-not-exist" and s.url == ref.url
    assert s.plays_total is None and s.players_now is None


def test_missing_counters_stay_none_not_zero():
    stripped = {"slug": "bare", "name": "Bare", "public_id": "pid-bare"}
    adapter, _ = make({r"/api/games/store/bare/": stripped, r"playing-now/\?ids=": {}})
    ref = GameRef(source="tesana", external_id="bare", url="https://tesana.com/game/bare")
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "ok" and s.title == "Bare"
    for field in ("plays_total", "players_total", "players_now", "likes", "remixes", "comments", "votes", "followers"):
        assert getattr(s, field) is None, field
    assert s.creator_external_id is None and s.creator_url is None
    assert s.tags == [] and s.published_at is None and s.engine is None

    # explicit nulls in a list object are also None, and 0 is kept as 0 (a real zero)
    adapter2, _ = make({
        r"metric=played": {"metric": "played", "period": "week", "results": [
            {"rank": 1, "game": {"slug": "zero", "name": "Zero", "public_id": "z", "play_count": 0, "players_7d": None, "playing_now": None, "favorite_count": None}}
        ]},
        r"playing-now/\?ids=": {},
    })
    refs = [r for r, _ in adapter2.discover(["played"], 5)]
    s2 = list(adapter2.fetch(refs))[0]
    assert s2.plays_total == 0 and s2.players_total is None and s2.players_now is None and s2.likes is None


# ---------------------------------------------------------------- creators
def test_fetch_creator_profile():
    adapter, http = make()
    prof = adapter.fetch_creator("Juicer")
    assert isinstance(prof, CreatorProfile)
    assert prof.source == "tesana" and prof.external_id == "Juicer"
    assert prof.url == "https://tesana.com/u/Juicer"
    assert prof.followers is None or isinstance(prof.followers, int)
    assert prof.followers == load("user_Juicer.json")["follower_count"]
    assert prof.avatar_url and prof.avatar_url.startswith("https://")
    assert isinstance(prof.links, dict)
    assert any("/api/games/users/Juicer/" in u for u in http.calls)


def test_fetch_creator_missing_returns_none():
    adapter, _ = make()
    assert adapter.fetch_creator("nobody-here") is None


def test_creator_links_are_keyed_by_platform():
    adapter, _ = make()
    links = adapter._links([
        {"url": "https://x.com/someone", "label": "Twitter"},
        {"type": "youtube", "url": "https://youtube.com/@someone"},
        "https://discord.gg/abc",
        {"url": "https://someone.dev"},
        {"url": ""},
        None,
    ])
    assert links == {
        "x": "https://x.com/someone",
        "youtube": "https://youtube.com/@someone",
        "discord": "https://discord.gg/abc",
        "website": "https://someone.dev",
    }
    assert adapter._links([]) == {}
    assert adapter._links(None) == {}


# ---------------------------------------------------------------- fixture hygiene
@pytest.mark.parametrize("name", ["charts_played.json", "section_popular_p1.json", "playing_now_top.json", "playing_now_ids.json", "user_Juicer.json"])
def test_fixtures_are_small(name):
    assert (FIX / name).stat().st_size <= 200 * 1024
    assert (FIX / "store_body-cam.json").stat().st_size <= 200 * 1024
