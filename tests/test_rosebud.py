"""Rosebud adapter tests against recorded fixtures (tests/fixtures/rosebud/). No network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracker.models import CreatorProfile, GameRef, GameSnapshot
from tracker.sources.rosebud import API, Rosebud

FIX = Path(__file__).parent / "fixtures" / "rosebud"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CFG = {"name": "rosebud", "lists": ["top", "new"], "top_n": 100, "rate_limit_rps": 1}


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeHttp:
    """Stands in for tracker.http.Http: answers get_json by URL pattern and counts calls."""

    def __init__(self):
        self.calls: list[str] = []
        self.requests_made = 0
        self.top = _load("list_top_p1.json")
        self.new = _load("list_new_p1.json")
        self.details = {p.name[len("play_detail_"):-len(".json")]: _load(p.name) for p in FIX.glob("play_detail_*.json")}
        self.users = {p.name[len("user_"):-len(".json")]: _load(p.name) for p in FIX.glob("user_*.json")}

    def get_json(self, url: str, **kwargs):
        self.calls.append(url)
        self.requests_made += 1
        assert url.startswith(API + "/api/"), url
        assert "/api/plays" not in url, "must never touch /api/plays/"
        m = re.match(rf"{re.escape(API)}/api/published-projects/\?ordering=(-\w+)&page_size=(\d+)&page=(\d+)$", url)
        if m:
            ordering, _, page = m.group(1), int(m.group(2)), int(m.group(3))
            if page > 1:
                return {"page": "Published projects can only be browsed through page 100."}
            return {**(self.top if ordering == "-plays_count" else self.new), "next": None}
        m = re.match(rf"{re.escape(API)}/api/published-projects/([0-9a-f-]+)/play-detail/$", url)
        if m:
            return self.details.get(m.group(1))  # None → 404
        m = re.match(rf"{re.escape(API)}/api/users/([0-9a-f-]+)/$", url)
        if m:
            return self.users.get(m.group(1))
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def adapter(http):
    return Rosebud(http, CFG)


def test_discover_yields_games_with_positions(adapter, http):
    out = list(adapter.discover(["top", "new"], 100))
    assert len(out) >= 5
    by_list: dict[str, list] = {}
    for ref, pl in out:
        assert ref.source == "rosebud"
        assert UUID_RE.match(ref.external_id), ref.external_id
        assert ref.url.startswith("https://rosebud.ai/play/")
        assert pl.external_id == ref.external_id
        by_list.setdefault(pl.list_name, []).append(pl.position)
    assert set(by_list) == {"top", "new"}
    for positions in by_list.values():
        assert positions == list(range(1, len(positions) + 1))
    # only list requests were made: one page per list (fixtures end at page 1)
    assert all("ordering=" in u for u in http.calls)
    assert len(http.calls) == 2


def test_discover_respects_top_n(adapter):
    out = list(adapter.discover(["top"], 5))
    assert [pl.position for _, pl in out] == [1, 2, 3, 4, 5]


def test_fetch_uses_discover_cache_with_no_extra_requests(adapter, http):
    refs = [ref for ref, _ in adapter.discover(["top"], 100)]
    before = len(http.calls)
    snaps = list(adapter.fetch(refs))
    assert len(http.calls) == before, "fetch must not issue requests for cached refs"
    assert len(snaps) >= 5
    assert len(snaps) == len(refs)
    for s in snaps:
        assert isinstance(s, GameSnapshot)
        assert s.status == "ok"
        assert s.title
        assert s.plays_total is not None and s.plays_total >= 0
        assert s.players_now is None and s.players_total is None
        assert s.creator_url is None
        assert s.published_at is None
    first = snaps[0]
    raw = http.top["results"][0]
    assert first.external_id == raw["id"]
    assert first.url == f"https://rosebud.ai/play/{raw['published_name']}"
    assert first.plays_total == raw["plays_count"]
    assert first.likes == raw["likes_count"]
    assert first.remixes == raw["clones_count"]
    assert first.comments == raw["comments_count"]
    assert first.thumbnail_url == raw["preview_image"]
    assert first.creator_external_id == raw["creator_id"]
    assert first.creator_name == raw["creator_username"]
    assert first.engine == raw["mode"]
    assert first.tags == raw["tags"]


def test_fetch_uncached_ref_hits_play_detail(adapter, http):
    gid, detail = next(iter(http.details.items()))
    ref = GameRef(source="rosebud", external_id=gid, url=f"https://rosebud.ai/play/{gid}")
    snaps = list(adapter.fetch([ref]))
    assert http.calls == [f"{API}/api/published-projects/{gid}/play-detail/"]
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "ok"
    assert s.title == detail["name"]
    assert s.plays_total == detail["plays_count"]
    assert s.remixes == detail["clones_count"]


def test_fetch_404_marks_gone(adapter, http):
    gid = "00000000-0000-4000-8000-000000000000"
    ref = GameRef(source="rosebud", external_id=gid, url=f"https://rosebud.ai/play/{gid}")
    snaps = list(adapter.fetch([ref]))
    assert len(snaps) == 1
    assert snaps[0].status == "gone"
    assert snaps[0].external_id == gid
    assert snaps[0].plays_total is None
    assert http.calls == [f"{API}/api/published-projects/{gid}/play-detail/"]


def test_missing_counters_stay_none(adapter, http):
    gid = "11111111-1111-4111-8111-111111111111"
    http.details[gid] = {"id": gid, "name": "Bare", "published_name": "bare"}
    ref = GameRef(source="rosebud", external_id=gid, url=f"https://rosebud.ai/play/{gid}")
    s = list(adapter.fetch([ref]))[0]
    assert s.status == "ok"
    assert s.title == "Bare"
    assert s.plays_total is None
    assert s.likes is None
    assert s.remixes is None
    assert s.comments is None
    assert s.players_now is None
    assert s.creator_external_id is None
    assert s.tags == []


def test_fetch_creator(adapter, http):
    cid, user = next(iter(http.users.items()))
    prof = adapter.fetch_creator(cid)
    assert isinstance(prof, CreatorProfile)
    assert prof.source == "rosebud"
    assert prof.external_id == cid
    assert prof.name == user["username"]
    assert prof.url is None
    assert prof.followers is None
    assert prof.links == {}
    assert http.calls == [f"{API}/api/users/{cid}/"]


def test_fetch_creator_404_returns_none(adapter):
    assert adapter.fetch_creator("22222222-2222-4222-8222-222222222222") is None
