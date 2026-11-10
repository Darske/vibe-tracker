"""Releases: sitemap parsing (pure) and the baseline / new / dated logic against a real Postgres."""

import os
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from tracker import db
from tracker.releases import (REMIX_GAME, SPAWN_GAME, WAVEDASH_GAME, Sweep, fill_dates_from_games, game_urls, known_urls,
                              release_counts, sitemap_locs, store_sweep)

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.spawn.co/</loc></url>
<url><loc>https://www.spawn.co/@tiger</loc></url>
<url><loc>https://www.spawn.co/@tiger/dust-2-dm</loc><lastmod>2026-11-10</lastmod></url>
<url><loc>https://www.spawn.co/@tiger/dust-2-dm/</loc></url>
<url><loc>https://www.spawn.co/@ann/snow</loc></url>
</urlset>"""
INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://remix.gg/sitemaps/misc.xml</loc></sitemap>
<sitemap><loc>https://remix.gg/sitemaps/games-1.xml</loc></sitemap>
</sitemapindex>"""


def test_sitemap_locs_reads_urlset_and_index():
    assert len(sitemap_locs(URLSET)) == 5
    assert sitemap_locs(INDEX)[1].endswith("games-1.xml")


def test_game_urls_keeps_only_game_pages_deduped():
    assert game_urls(sitemap_locs(URLSET), SPAWN_GAME) == ["https://www.spawn.co/@ann/snow", "https://www.spawn.co/@tiger/dust-2-dm"]
    assert game_urls(["https://remix.gg/g/a", "https://remix.gg/category/x", "https://remix.gg/g/a/"], REMIX_GAME) == ["https://remix.gg/g/a"]
    assert game_urls(["https://wavedash.com/games/sumbasurf", "https://wavedash.com/teams/x"], WAVEDASH_GAME) == ["https://wavedash.com/games/sumbasurf"]


URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
T0 = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = psycopg.connect(URL, row_factory=dict_row)
    c.execute("drop schema public cascade; create schema public;")
    c.commit()
    db.init(c)
    for n in ("spawn", "tesana", "astrocade", "playgama_ai"):
        c.execute("insert into sources (name, display_name, base_url, segment) values (%s, %s, 'https://x', 'creation')", (n, n))
    c.commit()
    yield c
    c.close()


@needs_db
def test_first_sweep_is_baseline_then_only_new_urls_count(conn):
    r1 = store_sweep(conn, Sweep("spawn", {"https://s/a": None, "https://s/b": None}, total=2), set(), T0)
    assert r1["baseline"] == 2 and r1["new"] == 0
    day2 = T0 + timedelta(days=1)
    r2 = store_sweep(conn, Sweep("spawn", {"https://s/a": None, "https://s/b": None, "https://s/c": None}, total=3),
                     known_urls(conn, "spawn"), day2)
    assert r2["new"] == 1
    rows = release_counts(conn, "week", date(2026, 1, 1), ["spawn"])
    assert sum(r["releases"] for r in rows) == 1  # a and b existed before we looked: no release date yet
    totals = conn.execute("select day, games_total from catalog_daily where source='spawn' order by day").fetchall()
    assert [t["games_total"] for t in totals] == [2, 3]


@needs_db
def test_publish_dates_give_history_even_for_baseline_rows(conn):
    pub = datetime(2026, 9, 3, tzinfo=timezone.utc)
    store_sweep(conn, Sweep("tesana", {"https://t/x": pub, "https://t/y": None}), set(), T0)
    rows = release_counts(conn, "month", date(2026, 1, 1), ["tesana"])
    assert [(r["period"], r["releases"]) for r in rows] == [(date(2026, 9, 1), 1)]
    # a later sweep that carries the date fills it in
    store_sweep(conn, Sweep("tesana", {"https://t/y": datetime(2026, 10, 5, tzinfo=timezone.utc)}), known_urls(conn, "tesana"), T0)
    assert sum(r["releases"] for r in release_counts(conn, "month", date(2026, 1, 1), ["tesana"])) == 2


@needs_db
def test_dates_borrowed_from_tracked_games(conn):
    store_sweep(conn, Sweep("spawn", {"https://www.spawn.co/@a/g": None}), set(), T0)
    conn.execute("insert into games (source, external_id, url, published_at, first_seen) values ('spawn','u1','https://www.spawn.co/@a/g/', %s, %s)",
                 (datetime(2026, 8, 1, tzinfo=timezone.utc), T0))
    conn.commit()
    assert fill_dates_from_games(conn) == 1


@needs_db
def test_partial_platform_from_games_and_net_platform_from_totals(conn):
    conn.execute("insert into games (source, external_id, url, published_at, first_seen) values ('astrocade','g1','https://a/g1', %s, %s)",
                 (datetime(2026, 10, 20, tzinfo=timezone.utc), T0))
    for d, n in ((date(2026, 11, 8), 8000), (date(2026, 11, 9), 8044), (date(2026, 11, 10), 8100)):
        conn.execute("insert into catalog_daily (source, day, games_total) values ('playgama_ai', %s, %s)", (d, n))
    conn.commit()
    rows = {(r["source"], r["period"]): r["releases"] for r in release_counts(conn, "month", date(2026, 1, 1), ["astrocade", "playgama_ai"])}
    assert rows[("astrocade", date(2026, 10, 1))] == 1
    assert rows[("playgama_ai", date(2026, 11, 1))] == 100  # 44 + 56; the first day has no previous total
