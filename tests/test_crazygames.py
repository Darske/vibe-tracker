"""CrazyGames adapter tests against recorded fixtures (tests/fixtures/crazygames/, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from tracker.models import GameRef, GameSnapshot
from tracker.sources.crazygames import API, CrazyGames, plays_from, thumbnail_from

FIX = Path(__file__).parent / "fixtures" / "crazygames"
CFG = {"name": "crazygames", "lists": ["hot", "top", "new", "catalog"], "top_n": 100, "rate_limit_rps": 1}


def load(name: str):
    return json.loads((FIX / name).read_text())


def items(name: str) -> list[dict]:
    return load(name)["games"]["items"]


class FakeResponse:
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.text = body
        self.url = url

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Serves fixtures. `/games?sorting=new` page N → games_new_p1/p2 for 1-2, games_new_last for the
    last page (by `total`), and synthetic full pages of fresh rows in between."""

    def __init__(self, *, max_ids: int | None = None, data_override: dict | None = None):
        self.calls: list[str] = []
        self.requests_made = 0
        self.max_ids = max_ids
        self.data_override = data_override  # id -> row served by gamesData
        self._data = {str(r["id"]): r for r in load("games_data.json")["games"]}
        for f in ("games_hot_p1.json", "games_default_p1.json", "games_new_p1.json"):
            for r in items(f):
                self._data.setdefault(str(r["id"]), r)
        self.total = load("games_new_p1.json")["games"]["total"]
        self.last_page = -(-self.total // 100)

    def _page(self, q: dict) -> dict:
        sorting, label, page = q["sorting"], q.get("label"), int(q["paginationPage"])
        assert q["paginationSize"] == "100" and q["device"] == "desktop"
        if label == "hot":
            return load("games_hot_p1.json") if page == 1 else {"games": {"total": 50, "items": []}}
        if sorting == "default":
            if page == 1:
                return load("games_default_p1.json")
            src = items("games_default_p1.json")
        else:
            if page == 1:
                return load("games_new_p1.json")
            if page == 2:
                return load("games_new_p2.json")
            if page == self.last_page:
                return load("games_new_last.json")
            if page > self.last_page:
                return {"games": {"pagination": {"page": page, "size": 100}, "total": self.total, "items": []}}
            src = items("games_new_p2.json")
        rows = [dict(r, id=f"{sorting}-{page}-{i}", slug=f"{r['slug']}-{sorting}-{page}") for i, r in enumerate(src)]
        return {"games": {"pagination": {"page": page, "size": 100}, "total": self.total, "items": rows}}

    def get_json(self, url: str, **kwargs):
        self.requests_made += 1
        self.calls.append(url)
        parts = urlsplit(url)
        assert url.startswith(API), url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path.split("/v4/en_US", 1)[1]
        if path == "/games":
            return self._page(q)
        if path == "/gamesData":
            ids = json.loads(q["gameIds"])
            assert isinstance(ids, list) and all(isinstance(i, str) for i in ids)
            if self.max_ids and len(ids) > self.max_ids:
                req = httpx.Request("GET", url)
                raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))
            src = self.data_override if self.data_override is not None else self._data
            return {"games": [src[i] for i in ids if i in src]}
        raise AssertionError(f"unexpected URL {url}")


def make(**kw) -> tuple[CrazyGames, FakeHttp]:
    http = FakeHttp(**kw)
    return CrazyGames(http, CFG), http


# ---------------------------------------------------------------- discover
def test_hot_list_positions_and_refs():
    adapter, http = make()
    out = list(adapter.discover(["hot"], 100))
    hot = items("games_hot_p1.json")
    assert len(out) == len(hot) == 50
    assert [p.position for _, p in out] == list(range(1, 51))
    ref, p = out[0]
    assert ref.external_id == str(hot[0]["id"]) and isinstance(ref.external_id, str)
    assert ref.url == f"https://www.crazygames.com/game/{hot[0]['slug']}"
    assert p.list_name == "hot" and p.source == "crazygames" and p.captured_at.tzinfo is not None
    assert len(http.calls) == 1
    assert "sorting=default" in http.calls[0] and "label=hot" in http.calls[0] and "paginationPage=1" in http.calls[0]


def test_top_and_new_stop_at_top_n_and_positions_restart():
    adapter, http = make()
    out = list(adapter.discover(["top", "new"], 100))
    top = [p for _, p in out if p.list_name == "top"]
    new = [p for _, p in out if p.list_name == "new"]
    assert [p.position for p in top] == list(range(1, 101))
    assert [p.position for p in new] == list(range(1, 101))
    assert top[0].external_id == str(items("games_default_p1.json")[0]["id"])
    assert new[0].external_id == str(items("games_new_p1.json")[0]["id"])
    assert len(http.calls) == 2  # one page each, page 2 never requested for top_n=100
    assert not any("paginationPage=2" in u for u in http.calls)


def test_top_n_spanning_two_pages():
    adapter, http = make()
    out = list(adapter.discover(["new"], 150))
    assert len(out) == 150 and out[-1][1].position == 150
    assert [u for u in http.calls if "paginationPage=2" in u]
    assert not any("paginationPage=3" in u for u in http.calls)


def test_catalog_walks_all_pages_until_total_and_shares_pages_with_new():
    adapter, http = make()
    total = http.total
    out = list(adapter.discover(["new", "catalog"], 6000))
    cat = [p for _, p in out if p.list_name == "catalog"]
    assert len(cat) == total  # every row of every page, stops at total
    assert cat[-1].position == total
    # one request per page; `new` and `catalog` read the same pages (memoised)
    assert len(http.calls) == http.last_page
    assert len(set(http.calls)) == len(http.calls)
    assert any(f"paginationPage={http.last_page}" in u for u in http.calls)


def test_unknown_list_skipped(caplog):
    adapter, http = make()
    assert list(adapter.discover(["bogus"], 10)) == []
    assert "unknown list 'bogus'" in caplog.text and http.calls == []


# ---------------------------------------------------------------- mapping
def test_plays_masked_one_is_none_and_likes_mapped():
    assert plays_from({"totalPlays": 1}) is None
    assert plays_from({"totalPlays": None}) is None
    assert plays_from({}) is None
    assert plays_from({"totalPlays": 0}) == 0
    assert plays_from({"totalPlays": 48636056}) == 48636056

    adapter, _ = make()
    refs = [r for r, _ in adapter.discover(["new"], 100)]
    snaps = {s.external_id: s for s in adapter.fetch(refs)}
    rows = {str(r["id"]): r for r in items("games_new_p1.json")}
    masked = [gid for gid, r in rows.items() if r.get("totalPlays") == 1]
    assert masked, "fixture should contain at least one masked (totalPlays == 1) game"
    for gid in masked:
        assert snaps[gid].plays_total is None
        assert snaps[gid].likes == rows[gid]["totalLikes"]  # likes are not masked
    for gid, r in rows.items():
        if r.get("totalPlays", 1) != 1:
            assert snaps[gid].plays_total == r["totalPlays"]
        assert snaps[gid].likes == r.get("totalLikes")


def test_snapshot_fields():
    adapter, _ = make()
    refs = [r for r, _ in adapter.discover(["hot"], 100)]
    s = next(iter(adapter.fetch(refs[:1])))
    row = items("games_hot_p1.json")[0]
    assert isinstance(s, GameSnapshot) and s.status == "ok"
    assert s.title == row["name"]
    assert s.url == f"https://www.crazygames.com/game/{row['slug']}"
    assert s.thumbnail_url.startswith(f"https://imgs.crazygames.com/{row['cover']}?")
    assert s.tags == ([row["categoryName"]] if row.get("categoryName") else [])
    assert s.creator_external_id is None and s.creator_name is None and s.creator_url is None
    assert s.published_at is None and s.ai_disclosure is None
    for f in ("plays_24h", "players_now", "players_total", "remixes", "comments", "votes", "followers"):
        assert getattr(s, f) is None, f


def test_thumbnail_variants():
    assert thumbnail_from({"cover": "a_16x9/1/a_16x9-cover.png"}).startswith("https://imgs.crazygames.com/a_16x9/1/a_16x9-cover.png?")
    assert thumbnail_from({"cover": "https://x.test/c.png"}) == "https://x.test/c.png"
    assert thumbnail_from({"covers": {"16x9": "b/c"}}).startswith("https://imgs.crazygames.com/b/c?")
    assert thumbnail_from({}) is None


# ---------------------------------------------------------------- fetch
def test_fetch_from_cache_makes_no_requests():
    adapter, http = make()
    refs = [r for r, _ in adapter.discover(["hot", "top"], 100)]
    n = len(http.calls)
    snaps = list(adapter.fetch(refs))
    assert len(snaps) == len(refs) and len(http.calls) == n


def test_fetch_uncached_via_games_data_batches():
    adapter, http = make()
    data = load("games_data.json")["games"]
    assert len(data) >= 20
    refs = [GameRef(source="crazygames", external_id=str(r["id"]), url="https://www.crazygames.com/game/old-slug") for r in data]
    snaps = {s.external_id: s for s in adapter.fetch(refs)}
    calls = [u for u in http.calls if "/gamesData" in u]
    assert len(calls) == -(-len(refs) // 50)
    assert "gameIds=%5B%22" in calls[0]  # url-encoded JSON array of string ids
    assert len(snaps) == len(refs)
    for r in data:
        s = snaps[str(r["id"])]
        assert s.status == "ok" and s.title == r["name"]
        assert s.url == f"https://www.crazygames.com/game/{r['slug']}"  # canonical slug from the API
        assert s.likes == r["totalLikes"]
        assert s.plays_total == (None if r["totalPlays"] == 1 else r["totalPlays"])
    # second fetch is answered from the cache
    n = len(http.calls)
    list(adapter.fetch(refs))
    assert len(http.calls) == n


def test_fetch_mixed_cached_and_uncached_only_requests_missing():
    adapter, http = make()
    cached = [r for r, _ in adapter.discover(["hot"], 5)]
    extra_row = next(r for r in load("games_data.json")["games"] if str(r["id"]) not in {c.external_id for c in cached})
    extra = GameRef(source="crazygames", external_id=str(extra_row["id"]), url="u")
    list(adapter.fetch(cached + [extra]))
    gd = [u for u in http.calls if "/gamesData" in u]
    assert len(gd) == 1
    assert json.loads(parse_qs(urlsplit(gd[0]).query)["gameIds"][0]) == [extra.external_id]


def test_batch_halves_on_4xx():
    adapter, http = make(max_ids=20)
    data = load("games_data.json")["games"]
    refs = [GameRef(source="crazygames", external_id=str(r["id"]), url="u") for r in data[:50]]
    snaps = list(adapter.fetch(refs))
    assert len(snaps) == len(refs)


def test_absent_id_skipped_and_removed_status_is_gone():
    gone_row = dict(load("games_data.json")["games"][0], id="777", status="UNPUBLISHED")
    adapter, _ = make(data_override={"777": gone_row})
    refs = [
        GameRef(source="crazygames", external_id="777", url="https://www.crazygames.com/game/x"),
        GameRef(source="crazygames", external_id="999999999", url="https://www.crazygames.com/game/nope"),
    ]
    snaps = list(adapter.fetch(refs))
    assert [(s.external_id, s.status) for s in snaps] == [("777", "gone")]  # absent id: skipped, not "gone"
    assert snaps[0].plays_total is None and snaps[0].url == refs[0].url


# ---------------------------------------------------------------- fixtures
def test_record_fixtures_writes_parser_inputs(tmp_path):
    adapter, http = make()
    written = adapter.record_fixtures(tmp_path, 20)
    names = {p.name for p in written}
    assert {"games_hot_p1.json", "games_default_p1.json", "games_new_p1.json", "games_new_p2.json",
            "games_new_last.json", "games_data.json"} <= names
    assert len(json.loads((tmp_path / "games_data.json").read_text())["games"]) == 20
    assert all("videos" not in r for r in json.loads((tmp_path / "games_new_p1.json").read_text())["games"]["items"])
