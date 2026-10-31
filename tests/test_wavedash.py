"""Wavedash adapter tests against recorded fixtures (tests/fixtures/wavedash/, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracker.models import GameRef
from tracker.sources.wavedash import API, ConvexError, Wavedash, _link_kind

FIX = Path(__file__).parent / "fixtures" / "wavedash"
CFG = {"name": "wavedash", "lists": ["trending", "featured"], "top_n": 100, "rate_limit_rps": 1}


def _load(name: str):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    """Dispatches Convex calls on body["path"]. Records every (url, body)."""

    def __init__(self, *, trending=None, missing_slugs: set[str] | None = None, fail_fn: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.requests_made = 0
        self.trending = trending if trending is not None else _load("trending.json")["value"]
        self.cards = _load("trending_cards.json")["value"]
        self.feed = [_load("feed_p1.json")["value"], _load("feed_p2.json")["value"]]
        self.games = {p.name[len("game_"):-len(".json")]: _load(p.name)["value"] for p in FIX.glob("game_*.json")}
        self.comments = _load("comments_count.json")["value"]
        self.orgs = {p.name[len("org_"):-len(".json")]: _load(p.name)["value"] for p in FIX.glob("org_*.json")}
        self.missing_slugs = missing_slugs or set()
        self.fail_fn = fail_fn

    @staticmethod
    def _ok(value):
        return {"status": "success", "value": value}

    @staticmethod
    def _not_found(msg):
        return {"status": "error", "errorMessage": "[Request ID: x] Server Error", "errorData": {"code": "not_found", "message": msg}}

    def post_json(self, url: str, body, **kwargs):
        self.requests_made += 1
        self.calls.append((url, body))
        assert url.startswith(API + "/api/"), url
        assert body["format"] == "json"
        fn, args = body["path"], body["args"]
        if fn == self.fail_fn:
            return {"status": "error", "errorMessage": "boom", "errorData": None}
        if fn == "gamePlayerCounts:listTrendingGames":
            assert url.endswith("/api/query")
            return self._ok(self.trending)
        if fn == "gamePlayerCounts:listTrendingGameCards":
            ids = set(args["gameIds"])
            return self._ok([c for c in self.cards if c["_id"] in ids])
        if fn == "gameFeed/feed:pageV2":
            assert url.endswith("/api/action")
            assert args["limit"] == 24 and args["includeFeatured"] is True and isinstance(args["seed"], int)
            cur = args["cursor"]
            if cur == {"offset": 0, "phase": 0}:
                return self._ok(self.feed[0])
            if cur == {"offset": 24, "phase": 0}:
                return self._ok(self.feed[1])
            # beyond the recorded pages: pretend the catalog ends here, in phase 1
            return self._ok({"page": [], "cursor": {"offset": 0, "phase": 2}, "isDone": True, "seed": 1})
        if fn == "games:getPublicGameBySlug":
            slug = args["slug"]
            if slug in self.missing_slugs or slug not in self.games:
                return self._not_found(f"Game with slug {slug} not found")
            return self._ok(self.games[slug])
        if fn == "gameComments/queries:getCountForGameCloud":
            n = self.comments.get(args["gameCloudId"])
            return self._ok(float(n)) if n is not None else self._ok(0.0)
        if fn == "organizations/queries:get":
            org = self.orgs.get(args["orgSlug"])
            return self._ok(org) if org else self._not_found("Team not found")
        raise AssertionError(f"unexpected function {fn}")


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def adapter(http):
    return Wavedash(http, CFG)


def _refs(out):
    return [ref for ref, _ in out]


# ---------------------------------------------------------------- discover
def test_trending_positions_follow_count_desc(adapter):
    out = list(adapter.discover(["trending"], 100))
    raw = _load("trending.json")["value"]
    assert len(out) == len(raw) >= 3
    by_id = {r["gameId"]: r["count"] for r in raw}
    counts = [by_id[ref.external_id] for ref, _ in out]
    assert counts == sorted(counts, reverse=True)
    assert [pl.position for _, pl in out] == list(range(1, len(out) + 1))
    for ref, pl in out:
        assert pl.list_name == "trending" and pl.source == "wavedash" and pl.external_id == ref.external_id
        assert ref.url.startswith("https://wavedash.com/games/") and not ref.url.endswith("/games/")
    # one list call + one cards call, nothing else
    assert [b["path"] for _, b in adapter.http.calls] == ["gamePlayerCounts:listTrendingGames", "gamePlayerCounts:listTrendingGameCards"]


def test_trending_sorts_even_if_api_order_is_shuffled():
    raw = _load("trending.json")["value"]
    shuffled = list(reversed(raw))
    ad = Wavedash(FakeHttp(trending=shuffled), CFG)
    out = list(ad.discover(["trending"], 100))
    assert out[0][0].external_id == max(raw, key=lambda r: r["count"])["gameId"]


def test_featured_is_feed_phase_0_only(adapter):
    out = list(adapter.discover(["featured"], 100))
    p1, p2 = _load("feed_p1.json")["value"], _load("feed_p2.json")["value"]
    expected = [it["game"]["_id"] for it in p1["page"] + p2["page"]]
    assert [ref.external_id for ref, _ in out] == expected
    assert [pl.position for _, pl in out] == list(range(1, len(expected) + 1))
    assert len(out) == 48
    feed_calls = [b for _, b in adapter.http.calls if b["path"] == "gameFeed/feed:pageV2"]
    assert [c["args"]["cursor"] for c in feed_calls] == [{"offset": 0, "phase": 0}, {"offset": 24, "phase": 0}]
    # the third page would be phase 1 → never requested for `featured`
    assert len(feed_calls) == 2
    slug = p1["page"][0]["game"]["slug"]
    assert out[0][0].url == f"https://wavedash.com/games/{slug}"


def test_featured_respects_top_n(adapter):
    out = list(adapter.discover(["featured"], 10))
    assert len(out) == 10 and [pl.position for _, pl in out] == list(range(1, 11))
    assert sum(1 for _, b in adapter.http.calls if b["path"] == "gameFeed/feed:pageV2") == 1


def test_catalog_walks_until_done(adapter):
    out = list(adapter.discover(["catalog"], 1000))
    assert len(out) == 48  # the fake ends the feed after the recorded pages
    feed_calls = [b for _, b in adapter.http.calls if b["path"] == "gameFeed/feed:pageV2"]
    assert len(feed_calls) == 3  # p1, p2, then the terminating page


def test_new_and_unknown_lists_yield_nothing(adapter, caplog):
    assert list(adapter.discover(["new", "bogus"], 100)) == []
    assert adapter.http.requests_made == 0
    assert "not derivable" in caplog.text


# ---------------------------------------------------------------- fetch
def test_fetch_trending_refs_have_players_now_and_comments(adapter):
    out = list(adapter.discover(["trending"], 100))
    raw = {r["gameId"]: int(r["count"]) for r in _load("trending.json")["value"]}
    snaps = list(adapter.fetch(_refs(out)))
    assert len(snaps) == len(out)
    for s in snaps:
        assert s.status == "ok"
        assert s.source == "wavedash"
        assert s.players_now == raw[s.external_id]
        assert s.plays_total is None and s.likes is None and s.players_total is None
        assert isinstance(s.comments, int)
        assert s.title
        assert s.thumbnail_url and s.thumbnail_url.startswith("https://uploads.wavedash.com/") and s.thumbnail_url.endswith(".webp")
        assert s.creator_external_id and s.creator_name
        assert s.creator_url == f"https://wavedash.com/teams/{s.creator_external_id}"
        assert s.published_at is not None and s.published_at.tzinfo is not None
        assert s.ai_tool is None
        assert isinstance(s.tags, list)
    # a trending card lacks organization/firstPublishedAt → exactly one getPublicGameBySlug per ref
    paths = [b["path"] for _, b in adapter.http.calls]
    assert paths.count("games:getPublicGameBySlug") == len(out)
    assert paths.count("gameComments/queries:getCountForGameCloud") == len(out)


def test_fetch_featured_only_refs_have_players_now_none(adapter):
    trending_ids = {r["gameId"] for r in _load("trending.json")["value"]}
    out = list(adapter.discover(["trending", "featured"], 100))
    refs = _refs(out)
    snaps = {s.external_id: s for s in adapter.fetch(refs)}
    featured_only = [ref.external_id for ref in refs if ref.external_id not in trending_ids]
    assert featured_only
    for gid in featured_only:
        assert snaps[gid].players_now is None  # CCU is unknown outside the trending set, not 0
        assert snaps[gid].plays_total is None
    for gid in trending_ids:
        assert isinstance(snaps[gid].players_now, int)
    assert all(s.plays_total is None and s.likes is None for s in snaps.values())


def test_fetch_uses_feed_cache_but_still_needs_full_object_for_dates(adapter):
    out = list(adapter.discover(["featured"], 5))
    snaps = list(adapter.fetch(_refs(out)))
    assert len(snaps) == 5
    # feed items carry organization but not firstPublishedAt → the full object is fetched once per ref
    paths = [b["path"] for _, b in adapter.http.calls]
    assert paths.count("games:getPublicGameBySlug") == 5
    for s in snaps:
        assert s.creator_external_id and s.published_at is not None


def test_comments_parsed_from_count(adapter):
    out = list(adapter.discover(["trending"], 1))
    snap = next(iter(adapter.fetch(_refs(out))))
    game = _load(f"game_{snap.url.rsplit('/', 1)[1]}.json")["value"]
    expected = _load("comments_count.json")["value"][game["productionGameCloudId"]]
    assert snap.comments == expected
    assert snap.title == game["title"]
    assert snap.creator_external_id == game["organization"]["slug"]
    assert snap.thumbnail_url == "https://uploads.wavedash.com/" + game["wideCover"]["r2Key"]
    assert int(snap.published_at.timestamp() * 1000) // 1000 == int(game["firstPublishedAt"]) // 1000


def test_gone_on_not_found():
    http = FakeHttp()
    ad = Wavedash(http, CFG)
    refs = [GameRef(source="wavedash", external_id="j97doesnotexist", url="https://wavedash.com/games/no-such-game")]
    snaps = list(ad.fetch(refs))
    assert len(snaps) == 1 and snaps[0].status == "gone"
    assert snaps[0].plays_total is None and snaps[0].players_now is None and snaps[0].comments is None
    assert "gameComments/queries:getCountForGameCloud" not in [b["path"] for _, b in http.calls]


def test_watchlist_ref_resolved_from_url_slug():
    """A ref that comes from the DB watchlist (no cached object) is resolved via its slug."""
    http = FakeHttp()
    ad = Wavedash(http, CFG)
    game = _load("game_sumbasurf.json")["value"]
    refs = [GameRef(source="wavedash", external_id=game["_id"], url="https://wavedash.com/games/sumbasurf")]
    snaps = list(ad.fetch(refs))
    assert snaps[0].status == "ok" and snaps[0].title == game["title"]
    # fetch() loads trending itself so a trending game on the watchlist still gets CCU
    assert isinstance(snaps[0].players_now, int)


def test_convex_error_propagates():
    ad = Wavedash(FakeHttp(fail_fn="gamePlayerCounts:listTrendingGames"), CFG)
    with pytest.raises(ConvexError):
        list(ad.discover(["trending"], 10))


# ---------------------------------------------------------------- creators
def test_fetch_creator(adapter):
    slug = next(FIX.glob("org_*.json")).name[len("org_"):-len(".json")]
    raw = _load(f"org_{slug}.json")["value"]
    prof = adapter.fetch_creator(slug)
    assert prof is not None
    assert prof.source == "wavedash" and prof.external_id == slug
    assert prof.name == raw["name"]
    assert prof.url == f"https://wavedash.com/teams/{slug}"
    assert prof.followers is None
    assert prof.bio == (raw.get("description") or None)
    assert bool(prof.links) == bool(raw.get("website"))
    assert adapter.fetch_creator("no-such-team-xyz") is None


def test_link_kind():
    assert _link_kind("https://x.com/portner") == "x"
    assert _link_kind("https://www.twitter.com/a") == "x"
    assert _link_kind("https://discord.gg/abc") == "discord"
    assert _link_kind("https://whitethorngames.com/") == "website"
