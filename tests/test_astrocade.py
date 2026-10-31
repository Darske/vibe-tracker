"""Astrocade adapter tests against recorded fixtures (tests/fixtures/astrocade/, no network)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tracker.models import GameRef
from tracker.sources.astrocade import Astrocade, slugify

FIX = Path(__file__).parent / "fixtures" / "astrocade"
CFG = {"name": "astrocade", "lists": ["top_picks", "trending", "action"], "top_n": 100, "rate_limit_rps": 1}
UUID_RE = re.compile(r"^guest-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class FakeResponse:
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.text = body
        self.url = url

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Routes astrocade-api-prod.com URLs to fixture bodies. Asserts the guest header on every call."""

    def __init__(self, *, missing_ids: set[str] | None = None, private_ids: set[str] | None = None,
                 stripped_ids: set[str] | None = None):
        self.calls: list[tuple[str, dict, dict]] = []  # (url, params, headers)
        self.requests_made = 0
        self.missing_ids = missing_ids or set()
        self.private_ids = private_ids or set()
        self.stripped_ids = stripped_ids or set()
        self._games: dict[str, dict] = {}
        for p in FIX.glob("game_*.json"):
            g = json.loads(p.read_text())["game"]
            self._games[g["gameId"]] = g
        for p in FIX.glob("feed_*.json"):
            for g in json.loads(p.read_text())["games"]:
                self._games.setdefault(g["gameId"], g)
        self._users = {p.name[len("user_"):-len(".json")]: p.read_text() for p in FIX.glob("user_*.json")}

    def _route(self, url: str, params: dict | None, headers: dict | None) -> FakeResponse:
        parts = urlsplit(url)
        assert parts.netloc == "astrocade-api-prod.com", url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        q.update({k: str(v) for k, v in (params or {}).items()})
        auth = (headers or {}).get("authorization", "")
        assert auth.startswith("guest-"), f"missing guest header on {url}"
        assert (headers or {}).get("x-app-platform") == "web"

        if parts.path == "/v3/feed":
            assert q.get("filter") == "categorized_feed"
            p = FIX / f"feed_{q['category']}.json"
            if not p.exists():
                return FakeResponse(200, json.dumps({"category": q["category"], "label": q["category"], "games": [], "hasMore": False}), url)
            data = json.loads(p.read_text())
            data["games"] = data["games"][: int(q.get("categoryPageSize", 100))]
            return FakeResponse(200, json.dumps(data), url)
        if parts.path == "/v3/games":
            gid = q["gameId"]
            if gid in self.missing_ids or gid not in self._games:
                return FakeResponse(404, '{"message":"not found"}', url)
            obj = dict(self._games[gid])
            if gid in self.private_ids:
                obj["isPublic"] = False
            if gid in self.stripped_ids:
                for k in ("plays", "likeCount", "commentsCount", "remixCount", "ownerFollowerCount",
                          "firstPublishedTimestamp", "createdAt", "username", "ownerId", "thumbnailUrl"):
                    obj.pop(k, None)
            return FakeResponse(200, json.dumps({"game": obj}), url)
        if parts.path == "/v3/users":
            body = self._users.get(q["username"])
            return FakeResponse(200, body, url) if body else FakeResponse(404, '{"message":"not found"}', url)
        raise AssertionError(f"unexpected URL {url}")

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests_made += 1
        self.calls.append((url, dict(kwargs.get("params") or {}), dict(kwargs.get("headers") or {})))
        return self._route(url, kwargs.get("params"), kwargs.get("headers"))

    def get_json(self, url: str, **kwargs):
        r = self.get(url, **kwargs)
        if r.status_code == 404:
            return None
        return r.json()


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def adapter(http) -> Astrocade:
    return Astrocade(http, CFG)


def _feed_ids(name: str) -> list[str]:
    return [g["gameId"] for g in json.loads((FIX / f"feed_{name}.json").read_text())["games"]]


def _refs(ids):
    return [GameRef(source="astrocade", external_id=i, url=f"https://www.astrocade.com/games/x/{i}") for i in ids]


# ---------------------------------------------------------------- headers / ids
def test_guest_header_is_uuid4_and_stable_per_instance(adapter):
    list(adapter.discover(["trending"], 5))
    list(adapter.discover(["top_picks"], 5))
    auths = {h["authorization"] for _, _, h in adapter.http.calls}
    assert len(auths) == 1
    assert UUID_RE.match(auths.pop())
    assert all(h["x-app-platform"] == "web" for _, _, h in adapter.http.calls)


def test_guest_id_pinned_by_env(monkeypatch, http):
    monkeypatch.setenv("ASTROCADE_GUEST_ID", "11111111-2222-4333-8444-555555555555")
    ad = Astrocade(http, CFG)
    list(ad.discover(["trending"], 3))
    assert http.calls[0][2]["authorization"] == "guest-11111111-2222-4333-8444-555555555555"


def test_slugify():
    assert slugify("Era.io") == "era-io"
    assert slugify("Anime World Cup Squad") == "anime-world-cup-squad"
    assert slugify("+1 Clone for Millions") == "1-clone-for-millions"
    assert slugify("  BRICK  RUSH!! ") == "brick-rush"
    assert slugify(None) == "game"


# ---------------------------------------------------------------- discover
def test_discover_positions_and_refs(adapter):
    out = list(adapter.discover(["top_picks", "trending", "action"], 100))
    assert len(out) >= 15
    by_list: dict[str, list] = {}
    for ref, pl in out:
        assert ref.source == "astrocade" and pl.source == "astrocade"
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", ref.external_id), ref.external_id
        assert re.fullmatch(rf"https://www\.astrocade\.com/games/[a-z0-9-]+/{ref.external_id}", ref.url), ref.url
        assert pl.external_id == ref.external_id
        by_list.setdefault(pl.list_name, []).append(pl.position)
    assert set(by_list) == {"top_picks", "trending", "action"}
    for name, positions in by_list.items():
        assert len(positions) >= 5, name
        assert positions == list(range(1, len(positions) + 1)), name
    # one feed request per list, categoryPageSize capped at 100
    feed_calls = [(u, p) for u, p, _ in adapter.http.calls if u.endswith("/v3/feed")]
    assert len(feed_calls) == 3
    assert all(p["categoryPageSize"] == 100 and p["filter"] == "categorized_feed" for _, p in feed_calls)


def test_discover_respects_top_n(adapter):
    out = list(adapter.discover(["trending"], 7))
    assert len(out) == 7
    assert [pl.position for _, pl in out] == list(range(1, 8))
    assert adapter.http.calls[0][1]["categoryPageSize"] == 7


def test_discover_dedupes_within_list(http):
    ad = Astrocade(http, CFG)
    ids = _feed_ids("trending")
    # patch the fake to return the first game twice
    orig = http._route

    def dup_route(url, params, headers):
        r = orig(url, params, headers)
        if url.endswith("/v3/feed"):
            d = json.loads(r.text)
            d["games"] = [d["games"][0], d["games"][0]] + d["games"][1:]
            return FakeResponse(200, json.dumps(d), url)
        return r

    http._route = dup_route
    out = list(ad.discover(["trending"], 100))
    assert [r.external_id for r, _ in out] == ids
    assert [pl.position for _, pl in out] == list(range(1, len(ids) + 1))


def test_discover_unknown_category_is_skipped(adapter):
    out = list(adapter.discover(["no_such_category", "trending"], 5))
    assert {pl.list_name for _, pl in out} == {"trending"}


# ---------------------------------------------------------------- fetch
def test_fetch_from_cache_makes_no_requests(adapter):
    discovered = list(adapter.discover(["trending"], 100))
    n = adapter.http.requests_made
    refs = [ref for ref, _ in discovered]
    snaps = list(adapter.fetch(refs))
    assert adapter.http.requests_made == n  # zero new requests
    assert len(snaps) == len(refs) >= 5
    raw = {g["gameId"]: g for g in json.loads((FIX / "feed_trending.json").read_text())["games"]}
    for s in snaps:
        g = raw[s.external_id]
        assert s.status == "ok"
        assert s.title == g["title"]
        assert s.plays_total == g["plays"]
        assert s.likes == g["likeCount"]
        assert s.comments == g["commentsCount"]
        assert s.remixes == g["remixCount"]
        assert s.followers == g["ownerFollowerCount"]
        assert s.votes is None and s.players_now is None and s.players_total is None and s.plays_24h is None
        assert s.creator_external_id == g["username"] and s.creator_name == g["username"]
        assert s.creator_url == f"https://www.astrocade.com/profile/{g['username']}"
        assert s.thumbnail_url == g["thumbnailUrl"]
        assert s.ai_tool == "astrocade"
        assert s.tags == []
        assert s.published_at is not None and s.published_at.tzinfo is not None


def test_fetch_uncached_makes_one_request_per_game(adapter):
    gid = next(FIX.glob("game_*.json")).name[len("game_"):-len(".json")]
    raw = json.loads((FIX / f"game_{gid}.json").read_text())["game"]
    snaps = list(adapter.fetch(_refs([gid])))
    assert adapter.http.requests_made == 1
    url, params, _ = adapter.http.calls[0]
    assert url.endswith("/v3/games") and params == {"gameId": gid}
    s = snaps[0]
    assert s.status == "ok" and s.title == raw["title"]
    assert s.plays_total == raw["plays"] and s.likes == raw["likeCount"]
    assert s.comments == raw["commentsCount"] and s.remixes == raw["remixCount"]
    assert s.creator_external_id == raw["username"]
    # second fetch of the same id is served from the cache
    list(adapter.fetch(_refs([gid])))
    assert adapter.http.requests_made == 1


def test_missing_game_is_gone(adapter):
    ids = _feed_ids("action")[:2] + ["01ZZZZZZZZZZZZZZZZZZZZZZZZ"]
    snaps = {s.external_id: s for s in adapter.fetch(_refs(ids))}
    assert snaps["01ZZZZZZZZZZZZZZZZZZZZZZZZ"].status == "gone"
    assert snaps["01ZZZZZZZZZZZZZZZZZZZZZZZZ"].plays_total is None
    assert all(snaps[i].status == "ok" for i in ids[:2])
    assert adapter.http.requests_made == 3


def test_private_game_is_gone():
    ids = _feed_ids("action")[:2]
    http = FakeHttp(private_ids={ids[0]})
    snaps = {s.external_id: s for s in Astrocade(http, CFG).fetch(_refs(ids))}
    assert snaps[ids[0]].status == "gone"
    assert snaps[ids[1]].status == "ok"


def test_stripped_object_yields_none_counters():
    ids = _feed_ids("action")[:2]
    http = FakeHttp(stripped_ids={ids[0]})
    snaps = {s.external_id: s for s in Astrocade(http, CFG).fetch(_refs(ids))}
    s = snaps[ids[0]]
    assert s.status == "ok"
    assert s.title
    assert s.plays_total is None and s.likes is None and s.comments is None and s.remixes is None
    assert s.followers is None and s.published_at is None
    assert s.creator_external_id is None and s.creator_name is None and s.creator_url is None
    assert s.thumbnail_url is None
    assert snaps[ids[1]].plays_total is not None


def test_published_at_conversion(adapter):
    gid = _feed_ids("action")[0]
    base = adapter.http._games[gid]
    now = datetime.now(timezone.utc)

    def snap_for(obj):
        adapter._objects.clear()
        adapter._remember(obj)
        return next(iter(adapter.fetch(_refs([gid]))))

    # unix seconds → aware UTC datetime
    obj = dict(base, firstPublishedTimestamp=1793203485, createdAt=1793189563)
    s = snap_for(obj)
    assert s.published_at == datetime(2026, 10, 28, 16, 4, 45, tzinfo=timezone.utc)
    # fallback to createdAt
    obj = dict(base, createdAt=1793189563)
    obj.pop("firstPublishedTimestamp", None)
    assert snap_for(obj).published_at == datetime(2026, 10, 28, 12, 12, 43, tzinfo=timezone.utc)
    # neither → None
    obj = dict(base)
    obj.pop("firstPublishedTimestamp", None)
    obj.pop("createdAt", None)
    assert snap_for(obj).published_at is None
    # real fixture values are sane
    real = snap_for(base)
    assert datetime(2024, 1, 1, tzinfo=timezone.utc) < real.published_at <= now


def test_owner_id_fallback_for_creator_id(adapter):
    gid = _feed_ids("action")[0]
    obj = dict(adapter.http._games[gid])
    obj.pop("username", None)
    adapter._remember(obj)
    s = next(iter(adapter.fetch(_refs([gid]))))
    assert s.creator_external_id == obj["ownerId"]
    assert s.creator_name is None and s.creator_url is None


# ---------------------------------------------------------------- creators
def test_fetch_creator(adapter):
    username = next(FIX.glob("user_*.json")).name[len("user_"):-len(".json")]
    raw = json.loads((FIX / f"user_{username}.json").read_text())
    prof = adapter.fetch_creator(username)
    assert prof is not None
    assert prof.source == "astrocade" and prof.external_id == username
    assert prof.name == username
    assert prof.url == f"https://www.astrocade.com/profile/{username}"
    assert prof.avatar_url == raw["profilePicture"]
    assert prof.followers == raw["followerCount"]
    assert prof.bio == (raw["bio"] or None)
    assert prof.links == {}
    url, params, _ = adapter.http.calls[-1]
    assert url.endswith("/v3/users") and params == {"username": username}
    assert adapter.fetch_creator("no-such-user-xyz-123") is None
