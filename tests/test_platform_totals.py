"""Whole-platform figures (platform_totals): adapter hooks and the daily upsert."""

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from tracker import db
from tracker.sources.spawn import parse_people_now
from tracker.sources.tesana import Tesana


class PagedHttp:
    """Serves Tesana marketplace search pages from a list of row lists."""

    def __init__(self, pages):
        self.pages, self.calls, self.requests_made = pages, [], 0

    def get_json(self, url, **kw):
        self.calls.append(url)
        page = int(url.rsplit("page=", 1)[1])
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return {"count": sum(len(p) for p in self.pages), "results": rows, "next": page < len(self.pages)}


def game(slug, plays, live=0, user="u"):
    return {"slug": slug, "name": slug, "play_count": plays, "playing_now": live, "players_today": 2, "user": user}


def test_tesana_totals_only_after_full_catalogue_walk():
    pages = [[game("a", 100, 1, "x"), game("b", 50, 0, "y")], [game("c", 10, 2, "x")]]
    t = Tesana(PagedHttp(pages), {"name": "tesana", "lists": ["catalog"], "top_n": 100})
    assert t.platform_totals() is None                     # nothing walked yet
    list(t.discover(["catalog"], 5000))
    tot = t.platform_totals()
    assert (tot["plays_total"], tot["games"], tot["creators"], tot["players_now"]) == (160, 3, 2, 3)
    t2 = Tesana(PagedHttp(pages), {"name": "tesana", "lists": ["catalog"], "top_n": 100})
    list(t2.discover(["catalog"], 2))                      # cut short by top_n: not the whole platform
    assert t2.platform_totals() is None


def test_spawn_people_now_parsing():
    assert parse_people_now('...\\"peopleNow\\":{\\"people\\":1181,\\"minute\\":29905784}...') == 1181
    assert parse_people_now('"peopleNow":{"people":42,"minute":1}') == 42
    assert parse_people_now("no counter here") is None


def test_spawn_site_online_from_list_pages_read_this_run():
    from tracker.sources.spawn import Spawn
    sp = Spawn.__new__(Spawn)
    sp._people_now = None
    assert sp.site_online() is None
    sp._people_now = 974
    assert sp.site_online() == {"people_recent": 974} == sp.platform_totals()


URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@needs_db
def test_upsert_keeps_latest_and_day_peak():
    c = psycopg.connect(URL, row_factory=dict_row)
    c.execute("drop schema public cascade; create schema public;")
    c.commit()
    db.init(c)
    c.execute("insert into sources (name, display_name, base_url, segment) values ('t','T','https://t','creation')")
    t0 = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
    db.upsert_platform_totals(c, "t", t0, {"plays_total": 1000, "players_now": 30, "people_recent": 500, "extra": {"a": 1}})
    db.upsert_platform_totals(c, "t", t0 + timedelta(hours=6), {"plays_total": 1200, "players_now": 10, "people_recent": 700})
    db.upsert_platform_totals(c, "t", t0 + timedelta(days=1), {"plays_total": 1500})
    c.commit()
    rows = c.execute("select * from platform_totals order by day").fetchall()
    assert [(r["plays_total"], r["players_now"], r["players_now_peak"], r["people_recent_peak"]) for r in rows] == \
        [(1200, 10, 30, 700), (1500, None, None, None)]
    assert rows[0]["extra"] == {"a": 1}
    c.close()


def test_crazygames_totals_after_catalog_walk_ignore_masked_plays():
    from tests.test_crazygames import make
    cg, _ = make()
    assert cg.platform_totals() is None
    list(cg.discover(["catalog"], 6000))
    tot = cg.platform_totals()
    assert tot is not None and tot["games"] == cg._totals[("new", None)]
    expected = sum(int(r["totalPlays"]) for r in cg._rows.values() if r.get("totalPlays") not in (None, 1))
    assert tot["plays_total"] == expected
