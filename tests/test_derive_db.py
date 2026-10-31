"""Derive step against a real Postgres. Skipped unless TEST_DATABASE_URL is set (CI provides one).

Covers the SQL half of metrics.py: rolling 7d sums, growth with noise floor, acceleration, ranks,
creator_daily and platform_daily aggregates, gap splitting through derive(), and idempotency.
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from tracker import db  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

D0 = date(2026, 3, 1)


@pytest.fixture
def conn():
    c = psycopg.connect(URL, row_factory=dict_row)
    c.execute("drop schema public cascade; create schema public;")
    c.commit()
    db.init(c)
    yield c
    c.close()


def seed(conn):
    conn.execute("insert into sources (name, display_name, base_url, segment) values ('t','T','https://t','creation')")
    c1 = conn.execute("insert into creators (source, external_id, name) values ('t','alice','alice') returning id").fetchone()["id"]
    c2 = conn.execute("insert into creators (source, external_id, name) values ('t','bob','bob') returning id").fetchone()["id"]
    g = {}
    for name, cid in (("a1", c1), ("a2", c1), ("b1", c2)):
        g[name] = conn.execute(
            "insert into games (source, external_id, url, title, creator_id, first_seen) values ('t',%s,%s,%s,%s,%s) returning id",
            (name, f"https://t/{name}", name, cid, datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ).fetchone()["id"]
    return g


def snap(conn, gid, day_offset, hour, plays, ccu=None):
    ts = datetime(D0.year, D0.month, D0.day, hour, tzinfo=timezone.utc) + timedelta(days=day_offset)
    conn.execute(
        "insert into snapshots (game_id, run_ts, captured_at, plays_total, players_now) values (%s,%s,%s,%s,%s)",
        (gid, ts, ts, plays, ccu),
    )


def test_full_derive(conn):
    g = seed(conn)
    # a1: 1000 plays/day for 21 days, 2 polls a day with CCU 10 and 30 ; one 3-day gap at days 10..13
    total = 0
    for d in range(21):
        if d in (11, 12):
            continue  # gap: no captures on day 11 and 12
        total += 1000 if d not in (13,) else 3000
        snap(conn, g["a1"], d, 2, total - 500, ccu=10)
        snap(conn, g["a1"], d, 14, total, ccu=30)
    # a2: slow game, 10/day
    for d in range(21):
        snap(conn, g["a2"], d, 14, 10 * (d + 1))
    # b1: accelerating: 100/day first week, 200/day second, 400/day third
    t = 0
    for d in range(21):
        t += 100 if d < 7 else 200 if d < 14 else 400
        snap(conn, g["b1"], d, 14, t)
    conn.commit()

    days = [D0 + timedelta(days=i) for i in range(21)]
    db.derive(conn, days, ["t"])
    db.derive(conn, days, ["t"])  # idempotent

    rows = {(r["game_id"], r["day"]): r for r in conn.execute("select * from game_daily").fetchall()}
    a1 = g["a1"]
    assert rows[(a1, D0)]["plays_delta"] is None  # first capture
    assert rows[(a1, D0 + timedelta(days=1))]["plays_delta"] == 1000  # collapses two polls to one delta
    assert rows[(a1, D0 + timedelta(days=1))]["ccu_peak"] == 30 and float(rows[(a1, D0 + timedelta(days=1))]["ccu_avg"]) == 20.0
    # gap: day 10 → day 13 is 3 days, delta 3000 split 1000/1000/1000
    for off in (11, 12, 13):
        r = rows[(a1, D0 + timedelta(days=off))]
        assert r["plays_delta"] == 1000 and r["gap_days"] == 3
    assert rows[(a1, D0 + timedelta(days=11))]["captures"] == 0

    d20 = D0 + timedelta(days=20)
    assert rows[(a1, d20)]["plays_7d"] == 7000 and rows[(a1, d20)]["plays_prev_7d"] == 7000
    assert float(rows[(a1, d20)]["growth_7d"]) == 0.0
    assert rows[(g["a2"], d20)]["growth_7d"] is None  # prev_7d = 70 < noise floor 100
    b1 = rows[(g["b1"], d20)]
    assert b1["plays_7d"] == 2800 and b1["plays_prev_7d"] == 1400 and float(b1["growth_7d"]) == 1.0
    # day 13: 7×200 = 1400 vs prev window 6×100 = 600 (day 0 is the NULL first capture) → 1.3333
    assert abs(float(rows[(g["b1"], D0 + timedelta(days=13))]["growth_7d"]) - 4 / 3) < 1e-3
    assert abs(float(b1["acceleration"]) - (1.0 - 4 / 3)) < 1e-3

    # ranks within source on day 20: a1 (7000) > b1 (2800) > a2 (70)
    assert rows[(a1, d20)]["rank_in_source"] == 1
    assert rows[(g["b1"], d20)]["rank_in_source"] == 2
    assert rows[(g["a2"], d20)]["rank_in_source"] == 3

    cd = {r["creator_id"]: r for r in conn.execute("select * from creator_daily where day = %s", (d20,)).fetchall()}
    alice = conn.execute("select id from creators where external_id='alice'").fetchone()["id"]
    assert cd[alice]["plays_7d"] == 7070 and cd[alice]["games_live"] == 2
    assert abs(float(cd[alice]["share_of_source"]) - 7070 / 9870) < 1e-6
    assert cd[alice]["hit_games"] == 1  # a1 >= p90 of (7000, 2800, 70)

    pd = conn.execute("select * from platform_daily where day = %s", (d20,)).fetchone()
    assert pd["plays_delta_top100"] == 1000 + 10 + 400
    assert pd["ccu_peak_top100"] == 30 and float(pd["ccu_avg_top100"]) == 20.0
    assert pd["games_tracked"] == 3 and pd["creators_active"] == 2
    assert abs(float(pd["top10_share"]) - 1.0) < 1e-9


def test_reset_and_rollup(conn):
    g = seed(conn)
    snap(conn, g["a1"], 0, 14, 5000)
    snap(conn, g["a1"], 1, 14, 100)  # reset
    conn.commit()
    db.derive(conn, [D0, D0 + timedelta(days=1)], ["t"])
    r = conn.execute("select * from game_daily where day = %s", (D0 + timedelta(days=1),)).fetchone()
    assert r["plays_delta"] is None and r["reset_flag"] is True

    # rollup keeps one row per game per day for old data
    for h in (2, 8, 14, 20):
        snap(conn, g["a2"], -60, h, h)
    conn.commit()
    removed = db.rollup(conn, keep_days=30)
    assert removed == 3
    left = conn.execute("select count(*) as n from snapshots where game_id = %s", (g["a2"],)).fetchone()["n"]
    assert left == 1
