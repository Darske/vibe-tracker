"""Storage changes (Nov 10): identical readings folded into the stored row, list positions kept to the top 100,
watchlist membership from games.last_listed. Daily figures must come out exactly as before."""

import os
from datetime import date, datetime, timedelta, timezone

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
    assert db.migrate(conn) == ["thin_history_v1", "placements_top100_v1"]
    assert conn.execute("select count(*) n from list_placements").fetchone()["n"] == 1
    listed = {r["external_id"]: r["last_listed"] for r in conn.execute("select external_id, last_listed from games").fetchall()}
    assert listed["deep"] is not None and listed["top"] is not None
    assert {r["external_id"] for r in db.watchlist(conn, "t", 7, 100)} == {"deep", "top"}  # the deep game is still polled
    assert db.migrate(conn) == []  # runs once


def test_live_players_are_never_folded():
    later = T + timedelta(hours=2)
    ins, fold = db.split_repeats({1: prev_row(T, ccu=5)}, [(1, snap(later, ccu=5))], later)
    assert len(ins) == 1 and fold == []          # 5 players again: kept, so per-run sums stay exact
    ins, fold = db.split_repeats({1: prev_row(T, ccu=0)}, [(1, snap(later, ccu=0))], later)
    assert ins == [] and len(fold) == 1          # zero again: folded (a missing zero does not change a sum)


@needs_db
def test_platform_totals_count_every_game_after_folding(conn):
    from dashboard.queries import PLATFORM_NOW_SQL
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = now - timedelta(hours=3) if (now - timedelta(hours=3)).date() == now.date() else now - timedelta(minutes=30)
    t1 = t0 + timedelta(minutes=20)
    ids = []
    for name in ("a", "b"):
        ids.append(conn.execute("insert into games (source, external_id, url, first_seen) values ('t',%s,%s,%s) returning id",
                                (name, f"https://t/{name}", t0)).fetchone()["id"])
    # run 1: both games; run 2: game a unchanged (folded, no row), game b moved
    db.insert_snapshots_bulk(conn, [(ids[0], snap(t0, plays=100)), (ids[1], snap(t0, plays=50))], t0)
    db.insert_snapshots_bulk(conn, [(ids[0], snap(t1, plays=100)), (ids[1], snap(t1, plays=60))], t1)
    conn.commit()
    assert conn.execute("select count(*) n from snapshots where run_ts = %s", (t1,)).fetchone()["n"] == 1
    row = conn.execute(PLATFORM_NOW_SQL).fetchone()
    assert (row["games_tracked"], row["plays_lifetime"]) == (2, 160)  # not (1, 60) as 'last run only' would give


@needs_db
def test_thin_history_keeps_last_reading_per_day_and_today_yesterday_in_full(conn):
    gid = conn.execute("insert into games (source, external_id, url, first_seen) values ('t','a','https://t/a',%s) returning id", (T,)).fetchone()["id"]
    today = date(2026, 11, 12)
    for day_off, hours in ((-3, (2, 10, 20)), (-2, (1, 23)), (-1, (5, 15)), (0, (3, 4))):
        for h in hours:
            ts = datetime(2026, 11, 12, h, tzinfo=timezone.utc) + timedelta(days=day_off)
            conn.execute("insert into snapshots (game_id, run_ts, captured_at, plays_total, players_now) values (%s,%s,%s,%s,%s)",
                         (gid, ts, ts, 100 + h, h))
    conn.commit()
    assert db.thin_history(conn, today) == 3  # Nov 9: 2 of 3 gone, Nov 10: 1 of 2 gone; Nov 11 (yesterday) and today kept
    left = [(r["captured_at"].day, r["captured_at"].hour) for r in
            conn.execute("select captured_at from snapshots order by captured_at").fetchall()]
    assert left == [(9, 20), (10, 23), (11, 5), (11, 15), (12, 3), (12, 4)]


@needs_db
def test_gap_fill_does_not_overwrite_a_stored_players_peak(conn):
    """A gap day (no readings left) that already has a stored row keeps its poll count and players peak/average;
    only its share of the plays is rewritten."""
    gid = conn.execute("insert into games (source, external_id, url, first_seen) values ('t','a','https://t/a',%s) returning id", (T,)).fetchone()["id"]
    d_gap = date(2026, 11, 2)
    conn.execute("insert into game_daily (game_id, day, captures, ccu_peak, ccu_avg, plays_total_eod) values (%s,%s,12,40,9.5,120)", (gid, d_gap))
    for off, plays, ccu in ((0, 100, 2), (3, 160, 1)):  # readings Nov 1 and Nov 4: Nov 2-3 are gap days
        ts = datetime(2026, 11, 1, 23, tzinfo=timezone.utc) + timedelta(days=off)
        conn.execute("insert into snapshots (game_id, run_ts, captured_at, plays_total, players_now) values (%s,%s,%s,%s,%s)", (gid, ts, ts, plays, ccu))
    conn.commit()
    db.derive(conn, [date(2026, 11, 4)], ["t"])
    r = conn.execute("select captures, ccu_peak, ccu_avg, gap_days from game_daily where game_id=%s and day=%s", (gid, d_gap)).fetchone()
    assert r["gap_days"] == 3                                      # the gap fill did run on this day
    assert (r["captures"], r["ccu_peak"], float(r["ccu_avg"])) == (12, 40, 9.5)


def test_rating_read_back_as_float4_still_folds():
    from datetime import datetime, timezone

    from tracker.db import split_repeats
    from tracker.models import GameSnapshot
    t0 = datetime(2026, 11, 11, 1, tzinfo=timezone.utc); t1 = datetime(2026, 11, 11, 7, tzinfo=timezone.utc)
    prev = {1: {"run_ts": t0, "captured_at": t0, "plays_total": 10, "plays_24h": None, "players_now": None, "players_total": None,
                "likes": 2, "remixes": None, "comments": None, "votes": 5, "rating": 4.610000133514404}}
    s = GameSnapshot(source="kongregate", external_id="a/b", url="u", plays_total=10, likes=2, votes=5, rating=4.61, captured_at=t1)
    ins, fold = split_repeats(prev, [(1, s)], t1)
    assert not ins and len(fold) == 1
    s2 = s.model_copy(update={"rating": 4.58})
    ins, fold = split_repeats(prev, [(1, s2)], t1)
    assert len(ins) == 1
