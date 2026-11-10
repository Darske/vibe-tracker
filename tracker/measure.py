"""Storage measurement (read-only): which tables grow, how fast, and how much of the growth is repeats.

    python -m tracker.measure            # prints a markdown report; also appended to $GITHUB_STEP_SUMMARY if set

Answers four questions before any retention / dedup change:
  1. Which tables take the space (table vs index bytes)?
  2. How many rows/day each history table gains, split by full scrape vs 30-min players-online (ccu) poll?
  3. What share of snapshot and placement rows repeat the previous reading exactly (removable without losing information)?
  4. How many games are dormant (no counter change in 7 days) and how much of the polling they account for?
Ends with a projection of days until the 500 MB free-tier limit at the current rate.
"""

from __future__ import annotations

import os
import sys

import psycopg

LIMIT_MB = 500
DAYS = 7  # measurement window for rates

# Full scrapes write a `runs` row per platform; the ccu poll does not. That is how the two are told apart.
Q_SIZES = """
select c.relname as table, s.n_live_tup as rows,
       pg_relation_size(c.oid) as table_bytes, pg_indexes_size(c.oid) as index_bytes, pg_total_relation_size(c.oid) as total_bytes
  from pg_class c join pg_stat_user_tables s on s.relid = c.oid
 order by pg_total_relation_size(c.oid) desc
"""

Q_DB = "select pg_database_size(current_database())"

Q_GROWTH = """
with full_runs as (select distinct run_ts from runs where run_ts > now() - make_interval(days => %(d)s))
select 'snapshots' as tbl, (s.run_ts in (select run_ts from full_runs)) as full_run, count(*) as rows
  from snapshots s where s.run_ts > now() - make_interval(days => %(d)s) group by 1, 2
union all
select 'list_placements', (p.run_ts in (select run_ts from full_runs)), count(*)
  from list_placements p where p.run_ts > now() - make_interval(days => %(d)s) group by 1, 2
union all
select 'game_daily', true, count(*) from game_daily where day > current_date - %(d)s
union all
select 'runs', true, count(*) from runs where run_ts > now() - make_interval(days => %(d)s)
"""

# A snapshot "repeats" when every counter equals the game's previous reading (NULLs compared as equal).
Q_SNAP_REPEATS = """
with full_runs as (select distinct run_ts from runs),
s as (
  select s.run_ts, s.captured_at::date as day, s.game_id,
         (s.run_ts in (select run_ts from full_runs)) as full_run,
         row(s.plays_total, s.plays_24h, s.players_now, s.players_total, s.likes, s.remixes, s.comments, s.votes)::text as cur,
         lag(row(s.plays_total, s.plays_24h, s.players_now, s.players_total, s.likes, s.remixes, s.comments, s.votes)::text)
           over (partition by s.game_id order by s.run_ts) as prev,
         row_number() over (partition by s.game_id, s.captured_at::date order by s.run_ts desc) as rn_day_last
    from snapshots s
   where s.run_ts > now() - make_interval(days => %(d)s + 1)
)
select full_run, count(*) as rows,
       count(*) filter (where prev = cur) as repeats,
       count(*) filter (where prev = cur and rn_day_last > 1) as removable_keep_eod
  from s where run_ts > now() - make_interval(days => %(d)s)
 group by full_run
"""

# A placement "repeats" when the game held the same position in the same list on that list's previous run.
Q_PLACE_REPEATS = """
with p as (
  select p.run_ts, position,
         lag(position) over (partition by source, list_name, game_id order by run_ts) as prev_pos
    from list_placements p
   where p.run_ts > now() - make_interval(days => %(d)s + 1)
)
select count(*) as rows, count(*) filter (where prev_pos = position) as same_position
  from p where run_ts > now() - make_interval(days => %(d)s)
"""

Q_PLACE_BY_LIST = """
select source, list_name, count(*) as rows, count(distinct run_ts) as runs, round(count(*)::numeric / greatest(count(distinct run_ts), 1)) as rows_per_run
  from list_placements where run_ts > now() - make_interval(days => %(d)s)
 group by 1, 2 order by 3 desc limit 12
"""

# Dormant = at least 2 readings in the window and no change in plays/likes/remixes/players_total/votes.
Q_DORMANT = """
with w as (
  select g.source, s.game_id, count(*) as readings,
         count(distinct (s.plays_total, s.likes, s.remixes, s.players_total, s.votes, s.comments)) as distinct_values
    from snapshots s join games g on g.id = s.game_id
   where s.run_ts > now() - make_interval(days => %(d)s)
   group by 1, 2
)
select source, count(*) as games, count(*) filter (where readings >= 2 and distinct_values = 1) as dormant,
       sum(readings) as readings, sum(readings) filter (where readings >= 2 and distinct_values = 1) as dormant_readings
  from w group by 1 order by 4 desc
"""


def mb(b) -> str:
    return f"{float(b or 0) / 1e6:,.1f} MB"


def pct(a, b) -> str:
    return "—" if not b else f"{100.0 * float(a or 0) / float(b):.0f}%"


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def measure(conn) -> str:
    cur = conn.cursor()
    cur.execute("set transaction read only")
    cur.execute("set statement_timeout = '300s'")
    p = {"d": DAYS}

    cur.execute(Q_DB)
    db_bytes = cur.fetchone()[0]
    cur.execute(Q_SIZES)
    sizes = cur.fetchall()
    size_of = {r[0]: r for r in sizes}

    cur.execute(Q_GROWTH, p)
    growth: dict[str, dict[bool, int]] = {}
    for tbl, full, n in cur.fetchall():
        growth.setdefault(tbl, {})[bool(full)] = n

    cur.execute(Q_SNAP_REPEATS, p)
    snap = {bool(r[0]): r[1:] for r in cur.fetchall()}
    cur.execute(Q_PLACE_REPEATS, p)
    place_rows, place_same = cur.fetchone()
    cur.execute(Q_PLACE_BY_LIST, p)
    by_list = cur.fetchall()
    cur.execute(Q_DORMANT, p)
    dormant = cur.fetchall()

    # bytes/day per table = (rows gained per day) x (current bytes per row, indexes included)
    rate_rows, total_rate = [], 0.0
    for tbl in ("snapshots", "list_placements", "game_daily", "runs"):
        r = size_of.get(tbl)
        if not r:
            continue
        per_row = float(r[4]) / r[1] if r[1] else 0
        g = growth.get(tbl, {})
        rows_day = (g.get(True, 0) + g.get(False, 0)) / DAYS
        bytes_day = rows_day * per_row
        total_rate += bytes_day
        rate_rows.append([tbl, f"{g.get(True, 0) / DAYS:,.0f}", f"{g.get(False, 0) / DAYS:,.0f}", f"{per_row:,.0f} B", mb(bytes_day)])

    lines = [f"# Storage measurement (last {DAYS} days)", "",
             f"**Database size:** {mb(db_bytes)} of {LIMIT_MB} MB ({pct(db_bytes, LIMIT_MB * 1e6)})", ""]
    lines += ["## Space by table", "", table(["table", "rows", "table", "indexes", "total"],
              [[r[0], f"{r[1]:,}", mb(r[2]), mb(r[3]), mb(r[4])] for r in sizes[:10]]), ""]
    lines += ["## Growth per day", "", table(["table", "rows/day full scrape", "rows/day ccu poll", "bytes/row", "growth/day"], rate_rows), "",
              f"**Total growth:** {mb(total_rate)}/day"]
    if total_rate > 0:
        days_left = (LIMIT_MB * 1e6 - db_bytes) / total_rate
        lines += [f"**At this rate the {LIMIT_MB} MB limit is reached in ~{days_left:,.0f} days.**"]
    lines += [""]

    lines += ["## Repeated readings (removable without losing information)", ""]
    srows = []
    for full, label in ((True, "full scrape"), (False, "ccu poll")):
        if full in snap:
            n, rep, rem = snap[full]
            srows.append([f"snapshots, {label}", f"{n:,}", f"{rep:,} ({pct(rep, n)})", f"{rem:,} ({pct(rem, n)})"])
    srows.append(["list_placements (same position as last run)", f"{place_rows:,}", f"{place_same:,} ({pct(place_same, place_rows)})", "—"])
    lines += [table(["rows", "in window", "exact repeats", "removable keeping 1 reading/game/day"], srows), ""]

    lines += ["## Biggest placement lists", "", table(["platform", "list", "rows", "runs", "rows/run"], [list(r) for r in by_list]), ""]
    lines += ["## Dormant games (no counter change in the window)", "", table(
        ["platform", "games read", "dormant", "readings", "readings on dormant games"],
        [[r[0], f"{r[1]:,}", f"{r[2]:,} ({pct(r[2], r[1])})", f"{r[3]:,}", f"{r[4] or 0:,} ({pct(r[4], r[3])})"] for r in dormant]), ""]
    return "\n".join(lines)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=20) as conn:
        report = measure(conn)
        conn.rollback()  # read-only: nothing to keep
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
