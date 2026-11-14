"""Bitmagic adapter tests against recorded fixtures (tests/fixtures/bitmagic/, no network)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tracker.http import Blocked
from tracker.models import GameRef
from tracker.sources.bitmagic import Bitmagic, extract_config_script, extract_token

FIX = Path(__file__).parent / "fixtures" / "bitmagic"
CFG = {"name": "bitmagic", "lists": ["trending", "popular", "new"], "top_n": 100, "rate_limit_rps": 1}

HOME_HTML = '<html><head><script type="module" crossorigin src="./assets/config-BDJXXKMg.js"></script></head></html>'


class FakeResponse:
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.text = body
        self.url = url

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Routes URLs to fixture bodies. Records every request (url, headers)."""

    def __init__(self, token: str, *, missing_ids: set[str] | None = None, live_counts: dict | None = None,
                 stripped_ids: set[str] | None = None, reject_token_once: bool = False, max_ids: int | None = None):
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.params: list[dict] = []
        self.missing_ids = missing_ids or set()
        self.stripped_ids = stripped_ids or set()
        self.live_counts = live_counts
        self.reject_token_once = reject_token_once
        self.max_ids = max_ids
        self.requests_made = 0
        self._games = json.loads((FIX / "games_batch.json").read_text())["games"]
        self._profiles = {p.name[len("profile_"):-len(".json")]: p.read_text() for p in FIX.glob("profile_*.json")}

    # -- routing -----------------------------------------------------------
    def _route(self, url: str, params: dict | None, headers: dict | None) -> FakeResponse:
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        q.update({k: str(v) for k, v in (params or {}).items()})
        path = parts.path
        if path == "/":
            return FakeResponse(200, HOME_HTML, url)
        if path.startswith("/assets/config-"):
            return FakeResponse(200, (FIX / "config.js").read_text(), url)

        assert path.startswith("/api/"), url
        if (headers or {}).get("X-API-Token") != self.token:
            raise Blocked(f"401 on {url}")
        if self.reject_token_once:
            self.reject_token_once = False
            raise Blocked(f"401 on {url}")

        if path == "/api/trending":
            body = (FIX / "trending_p1.json").read_text() if q.get("offset", "0") == "0" else '{"success":true,"items":[],"total":0}'
            return FakeResponse(200, body, url)
        if path == "/api/catalog/browse":
            sort = q["sort"]
            if q.get("offset", "0") != "0":
                return FakeResponse(200, '{"success":true,"games":[],"total":0,"nextOffset":null}', url)
            return FakeResponse(200, (FIX / f"browse_{sort}_p1.json").read_text(), url)
        if path in ("/api/catalog/games", "/api/catalog/live"):
            ids = q["ids"].split(",")
            if self.max_ids and len(ids) > self.max_ids:
                req = httpx.Request("GET", url)
                raise httpx.HTTPStatusError("414", request=req, response=httpx.Response(414, request=req))
            if path == "/api/catalog/games":
                games = {}
                for gid in ids:
                    if gid in self.missing_ids or gid not in self._games:
                        continue
                    obj = dict(self._games[gid])
                    if gid in self.stripped_ids:
                        for k in ("playCount", "likeCount", "commentCount", "viewCount", "publishedAt", "creator"):
                            obj.pop(k, None)
                    games[gid] = obj
                return FakeResponse(200, json.dumps({"success": True, "games": games}), url)
            counts = self.live_counts if self.live_counts is not None else json.loads((FIX / "live_batch.json").read_text())["counts"]
            return FakeResponse(200, json.dumps({"success": True, "counts": {k: v for k, v in counts.items() if k in ids}}), url)
        if path == "/api/catalog/stats":
            return FakeResponse(200, json.dumps({"success": True, "stats": {"totalPlays": 143264, "totalGames": 157,
                                                                             "totalCreators": 86, "playingNow": 9}}), url)
        m = re.fullmatch(r"/api/players/([^/]+)/profile", path)
        if m:
            body = self._profiles.get(m.group(1))
            return FakeResponse(200, body, url) if body else FakeResponse(404, "", url)
        raise AssertionError(f"unexpected URL {url}")

    # -- Http surface used by the adapter ----------------------------------
    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests_made += 1
        self.calls.append((url, dict(kwargs.get("headers") or {})))
        self.params.append(dict(kwargs.get("params") or {}))
        return self._route(url, kwargs.get("params"), kwargs.get("headers"))

    def get_json(self, url: str, **kwargs):
        r = self.get(url, **kwargs)
        if r.status_code == 404:
            return None
        return r.json()


@pytest.fixture
def token() -> str:
    tok = extract_token((FIX / "config.js").read_text())
    assert tok
    return tok


@pytest.fixture
def adapter(token):
    return Bitmagic(FakeHttp(token), CFG)


# ---------------------------------------------------------------- token
def test_token_extraction_from_fixture_config(token):
    assert len(token) >= 32
    assert token != "localhost-token"
    assert "CATALOG_API_TOKEN" in (FIX / "config.js").read_text()


def test_config_script_discovery():
    assert extract_config_script(HOME_HTML, "https://bitmagic.ai") == "https://bitmagic.ai/assets/config-BDJXXKMg.js"
    assert extract_config_script('<script src="/assets/config-XyZ_1.js">', "https://bitmagic.ai") == "https://bitmagic.ai/assets/config-XyZ_1.js"
    assert extract_config_script("<html></html>", "https://bitmagic.ai") is None


def test_token_fetched_before_first_api_call_and_cached(adapter):
    list(adapter.discover(["popular"], 5))
    urls = [u for u, _ in adapter.http.calls]
    assert urls[0] == "https://bitmagic.ai/"
    assert "/assets/config-" in urls[1]
    assert all(h.get("X-API-Token") == adapter.http.token for u, h in adapter.http.calls if "/api/" in u)
    list(adapter.discover(["popular"], 5))
    assert sum(1 for u, _ in adapter.http.calls if "/assets/config-" in u) == 1


def test_blocked_once_refreshes_token_and_retries(token):
    http = FakeHttp(token, reject_token_once=True)
    ad = Bitmagic(http, CFG)
    games = list(ad.discover(["popular"], 5))
    assert len(games) == 5
    assert sum(1 for u, _ in http.calls if "/assets/config-" in u) == 2


def test_blocked_twice_propagates(token):
    http = FakeHttp("different-token")  # fixture token never matches → every API call is Blocked
    ad = Bitmagic(http, CFG)
    with pytest.raises(Blocked):
        list(ad.discover(["popular"], 5))


# ---------------------------------------------------------------- discover
def test_discover_positions_and_refs(adapter):
    out = list(adapter.discover(["trending", "popular", "new"], 100))
    assert len(out) >= 15
    by_list: dict[str, list] = {}
    for ref, pl in out:
        assert ref.source == "bitmagic" and pl.source == "bitmagic"
        assert ref.url == f"https://bitmagic.ai/play/{ref.external_id}/"
        assert pl.external_id == ref.external_id
        by_list.setdefault(pl.list_name, []).append(pl.position)
    assert set(by_list) == {"trending", "popular", "new"}
    for name, positions in by_list.items():
        assert len(positions) >= 5, name
        assert positions[0] == 1, name
        assert positions == sorted(positions) and len(set(positions)) == len(positions), name


def test_discover_respects_top_n(adapter):
    out = list(adapter.discover(["popular"], 7))
    assert len(out) == 7
    assert [pl.position for _, pl in out] == list(range(1, 8))


def test_trending_uses_global_rank(adapter):
    items = json.loads((FIX / "trending_p1.json").read_text())["items"]
    out = list(adapter.discover(["trending"], 10))
    assert [pl.position for _, pl in out] == [it["globalRank"] for it in items[:10]]


# ---------------------------------------------------------------- fetch
def _refs(ids):
    return [GameRef(source="bitmagic", external_id=i, url=f"https://bitmagic.ai/play/{i}/") for i in ids]


def test_fetch_snapshots_from_batch(adapter):
    games = json.loads((FIX / "games_batch.json").read_text())["games"]
    ids = list(games)
    snaps = list(adapter.fetch(_refs(ids)))
    assert len(snaps) == len(ids) >= 5
    for s in snaps:
        assert s.status == "ok"
        assert s.title
        assert isinstance(s.plays_total, int)
        assert isinstance(s.likes, int)
        assert isinstance(s.comments, int)
        assert s.creator_external_id and s.creator_url == f"https://bitmagic.ai/u/{s.creator_external_id}/"
        assert s.published_at is not None
        assert s.ai_tool is None
        assert s.thumbnail_url
    # batching: one games call + one live call for ≤25 ids
    api = [u for u, _ in adapter.http.calls if "/api/catalog/" in u]
    assert len(api) == 2


def test_players_now_zero_when_absent_from_live_and_n_when_present(token):
    games = json.loads((FIX / "games_batch.json").read_text())["games"]
    ids = list(games)
    http = FakeHttp(token, live_counts={ids[0]: 7})
    snaps = {s.external_id: s for s in Bitmagic(http, CFG).fetch(_refs(ids))}
    assert snaps[ids[0]].players_now == 7
    assert snaps[ids[1]].players_now == 0  # live omits zeros → 0, not None
    assert all(s.players_now == 0 for i, s in snaps.items() if i != ids[0])


def test_missing_id_is_gone(token):
    games = json.loads((FIX / "games_batch.json").read_text())["games"]
    ids = list(games)[:4] + ["DOESNOTEXIST"]
    snaps = {s.external_id: s for s in Bitmagic(FakeHttp(token), CFG).fetch(_refs(ids))}
    assert snaps["DOESNOTEXIST"].status == "gone"
    assert snaps["DOESNOTEXIST"].plays_total is None
    assert all(snaps[i].status == "ok" for i in ids[:4])


def test_stripped_object_yields_none_counters(token):
    games = json.loads((FIX / "games_batch.json").read_text())["games"]
    ids = list(games)[:3]
    http = FakeHttp(token, stripped_ids={ids[0]}, live_counts={})
    snaps = {s.external_id: s for s in Bitmagic(http, CFG).fetch(_refs(ids))}
    s = snaps[ids[0]]
    assert s.status == "ok"
    assert s.plays_total is None and s.likes is None and s.comments is None
    assert s.published_at is None and s.creator_external_id is None and s.creator_url is None
    assert s.players_now == 0  # still in the live universe
    assert snaps[ids[1]].plays_total is not None


def test_batch_halves_on_4xx(token):
    games = json.loads((FIX / "games_batch.json").read_text())["games"]
    ids = list(games)
    assert len(ids) >= 10
    http = FakeHttp(token, max_ids=6)
    snaps = list(Bitmagic(http, CFG).fetch(_refs(ids)))
    assert len(snaps) == len(ids) and all(s.status == "ok" for s in snaps)
    sizes = [len(p["ids"].split(",")) for (u, _), p in zip(http.calls, http.params) if "/api/catalog/games" in u]
    assert max(sizes) > 6 and all(s <= 6 for s in sizes[-2:])


# ---------------------------------------------------------------- creators
def test_fetch_creator(adapter):
    brand = next(FIX.glob("profile_*.json")).name[len("profile_"):-len(".json")]
    prof = adapter.fetch_creator(brand)
    assert prof is not None
    assert prof.source == "bitmagic" and prof.external_id == brand
    assert prof.name
    assert prof.url == f"https://bitmagic.ai/u/{brand}/"
    assert isinstance(prof.followers, int)
    raw = json.loads((FIX / f"profile_{brand}.json").read_text())["profile"]
    assert prof.followers == raw["followerCount"]
    assert ("website" in prof.links) == bool(raw.get("homePageUrl"))
    assert adapter.fetch_creator("no-such-brand-xyz") is None


def test_site_online_and_totals_share_one_stats_request(token):
    http = FakeHttp(token)
    ad = Bitmagic(http, CFG)
    assert ad.site_online() == {"players_now": 9}
    tot = ad.platform_totals()
    assert tot["players_now"] == 9 and tot["plays_total"] == 143264
    assert sum(1 for u, _ in http.calls if u.endswith("/api/catalog/stats")) == 1
