"""Storage changes (Nov 10): identical readings folded into the stored row, list positions kept to the top 100,
watchlist membership from games.last_listed. Daily figures must come out exactly as before."""

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from tracker import db, metrics
from tracker.models import GameSnapshot

T = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)


def snap(ts, plays=100, ccu=None, likes=5):
    return GameSnapshot(source="t", external_id="a", url="https://t/a", plays_total=plays, players_now=ccu, likes=likes, captured_at=ts)


def prev_row(ts, plays=100, ccu=None, likes=5, run_ts=None):
    return {"run_ts": run_ts or ts, "captured_at": ts, "plays_total": plays, "plays_24h": None, "players_now": ccu,
            "players_total": None, "likes": likes, "remixes": None, "comments": None, "votes": None}


def test_identical_same_day_reading_is_folded():
    ins, fold = db.split_repeats({1: prev_row(T)}, [(1, snap(T + timedelta(hours=2)))], T + timedelta(hours=2))
    assert ins == [] and fold == [(T + timedelta(hours=2), 1, T)]


def test_changed_counter_new_day_or_same_run_is_stored():
    later = T + timedelta(hours=2)
    assert len(db.split_repeats({1: prev_row(T)}, [(1, snap(later, plays=101))], later)[0]) == 1          # counter moved
    nextday = datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)
    assert len(db.split_repeats({1: prev_row(T)}, [(1, snap(nextday))], nextday)[0]) == 1                  # first of the day
    assert len(db.split_repeats({1: prev_row(T)}, [(1, snap(T))], T)[0]) == 1                               # same run re-written
    assert len(db.split_repeats({}, [(1, snap(T))], T)[0]) == 1                                             # first ever
    assert len(db.split_repeats({1: prev_row(T, ccu=None)}, [(1, snap(later, ccu=0))], later)[0]) == 1      # None != 0


def test_weighted_daily_rows_match_unfolded():
    # four polls: ccu 10, 0, 0, 0 -> average 2.5 over 4 polls; folded form: 10 (w1), 0 (w3)
    raw = [metrics.Capture(T + timedelta(hours=h), 100, players_now=v) for h, v in ((0, 10), (2, 0), (4, 0), (6, 0))]
    folded = [metrics.Capture(T, 100, players_now=10), metrics.Capture(T + timedelta(hours=2), 100, players_now=0, weight=3)]
    a, b = metrics.daily_rows(raw)[T.date()], metrics.daily_rows(folded)[T.date()]
    assert (a.captures, a.ccu_peak, a.ccu_avg) == (b.captures, b.ccu_peak, b.ccu_avg) == (4, 10, 2.5)


URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def conn():
    c = psycopg.connect(URL, row_factory=dict_row)
    c.execute("drop schema public cascade; create schema public;")
    c.commit()
    db.init(c)
    c.execute("insert into sources (name, display_name, base_url, segment) values ('t','T','https://t','creation')")
    c.commit()
    yield c
    c.close()


@needs_db
def test_folding_end_to_end_keeps_daily_figures(conn):
    gid = conn.execute("insert into games (source, external_id, url, first_seen) values ('t','a','https://t/a',%s) returning id", (T,)).fetchone()["id"]
    for h, plays, ccu in ((0, 100, 4), (2, 100, 0), (4, 100, 0), (6, 130, 0), (8, 130, 0)):
        ts = T + timedelta(hours=h)
        db.insert_snapshots_bulk(conn, [(gid, snap(ts, plays=plays, ccu=ccu))], ts)
    conn.commit()
    rows = conn.execute("select run_ts, plays_total, players_now, repeats from snapshots order by run_ts").fetchall()
    assert [(r["plays_total"], r["players_now"], r["repeats"]) for r in rows] == [(100, 4, 0), (100, 0, 1), (130, 0, 1)]
    db.derive(conn, [T.date()], ["t"])
    d = conn.execute("select captures, ccu_peak, ccu_avg, plays_total_eod from game_daily where game_id = %s", (gid,)).fetchone()
    assert (d["captures"], d["ccu_peak"], float(d["ccu_avg"]), d["plays_total_eod"]) == (5, 4, 0.8, 130)


@needs_db
def test_migration_backfills_last_listed_then_trims_and_watchlist_uses_it(conn):
    now = datetime.now(timezone.utc)
    g_deep = conn.execute("insert into games (source, external_id, url, first_seen) values ('t','deep','https://t/deep',%s) returning id", (now,)).fetchone()["id"]
    g_top = conn.execute("insert into games (source, external_id, url, first_seen) values ('t','top','https://t/top',%s) returning id", (now,)).fetchone()["id"]
    conn.execute("insert into list_placements (source, list_name, position, game_id, run_ts) values ('t','catalog',2345,%s,%s), ('t','top',3,%s,%s)",
                 (g_deep, now - timedelta(hours=3), g_top, now - timedelta(hours=1)))
    conn.execute("delete from maintenance where key like 'migration:%%'")
    conn.commit()
    assert db.migrate(conn) == ["placements_top100_v1"]
    assert conn.execute("select count(*) n from list_placements").fetchone()["n"] == 1
    listed = {r["external_id"]: r["last_listed"] for r in conn.execute("select external_id, last_listed from games").fetchall()}
    assert listed["deep"] is not None and listed["top"] is not None
    assert {r["external_id"] for r in db.watchlist(conn, "t", 7, 100)} == {"deep", "top"}  # the deep game is still polled
    assert db.migrate(conn) == []  # runs once
