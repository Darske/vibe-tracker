"""Metric definitions. The pure-Python part (daily deltas, CCU, growth) is tested directly;
the SQL part (ranks, creator/platform aggregates) runs against Postgres in `derive()`.

Definitions (from the brief, implemented exactly):

- run_ts: one timestamp per pipeline run, shared by every platform polled in that run.
- plays_delta for day D = last plays_total captured on D minus last plays_total captured on the
  previous capture day. Several polls in a day collapse to one daily delta. First-ever capture →
  NULL, not 0. If the previous capture is k > 1 days old, write delta ÷ k to each intervening day
  and set gap_days = k.
- A negative delta means the platform reset or corrected its counter: write NULL and set
  reset_flag = true. Never write negative plays.
- ccu_peak / ccu_avg for a game on day D = max / mean of players_now over that day's polls
  (NULL if the platform exposes no players-online number).
- plays_7d = sum of plays_delta over the trailing 7 days; plays_prev_7d = the 7 days before that;
  growth_7d = plays_7d ÷ plays_prev_7d − 1, NULL when plays_prev_7d < NOISE_FLOOR.
- acceleration = growth_7d − growth_7d one week earlier.
- rank_in_source = rank of the game within its platform by plays_7d, computed daily.
- Creator hit_games = games with plays_7d ≥ the platform's 90th percentile that day.
  share_of_source = creator plays_7d ÷ platform plays_7d (tracked set).
- top10_share = plays_7d of the top 10 games ÷ plays_7d of all tracked games in that platform.
- platform_daily.plays_delta_top100 = sum of plays_delta over the games in that platform's top
  100 that day. ccu_peak_top100 = max over the day's runs of the sum of players_now over the top
  100 at that run_ts; ccu_avg_top100 = mean of those per-run sums.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime

NOISE_FLOOR = int(os.environ.get("GROWTH_NOISE_FLOOR", "100"))
COUNTER_COLS = ("plays_total", "likes", "remixes", "votes")
DELTA_COLS = {"plays_total": "plays_delta", "likes": "likes_delta", "remixes": "remixes_delta", "votes": "votes_delta"}


@dataclass
class Capture:
    captured_at: datetime
    plays_total: int | None = None
    likes: int | None = None
    remixes: int | None = None
    votes: int | None = None
    players_now: int | None = None

    @property
    def day(self) -> date:
        return self.captured_at.date()


@dataclass
class DailyRow:
    day: date
    captures: int = 0
    plays_delta: int | None = None
    likes_delta: int | None = None
    remixes_delta: int | None = None
    votes_delta: int | None = None
    plays_total_eod: int | None = None
    ccu_peak: int | None = None
    ccu_avg: float | None = None
    gap_days: int | None = None
    reset_flag: bool = False
    extra: dict = field(default_factory=dict)


def _split_delta(delta: int, k: int) -> list[int]:
    """Spread `delta` over k days as integers that sum to delta (remainder on the last day)."""
    base, rem = divmod(delta, k)
    out = [base] * k
    out[-1] += rem
    return out


def daily_rows(captures: list[Capture]) -> dict[date, DailyRow]:
    """Collapse per-poll captures into per-day rows with deltas, gaps, resets and CCU stats.

    Returns rows for every day that has a capture, plus intervening gap days (which carry a split
    delta, gap_days=k, captures=0).
    """
    if not captures:
        return {}
    caps = sorted(captures, key=lambda c: c.captured_at)
    by_day: dict[date, list[Capture]] = {}
    for c in caps:
        by_day.setdefault(c.day, []).append(c)

    rows: dict[date, DailyRow] = {}
    prev_day: date | None = None
    prev_last: Capture | None = None
    for day in sorted(by_day):
        todays = by_day[day]
        last = todays[-1]
        row = DailyRow(day=day, captures=len(todays), plays_total_eod=last.plays_total)
        ccu = [c.players_now for c in todays if c.players_now is not None]
        if ccu:
            row.ccu_peak = max(ccu)
            row.ccu_avg = round(sum(ccu) / len(ccu), 2)

        if prev_last is not None:
            k = (day - prev_day).days
            for col, dcol in DELTA_COLS.items():
                cur, before = getattr(last, col), getattr(prev_last, col)
                if cur is None or before is None:
                    continue  # NULL delta: counter missing on one side
                delta = cur - before
                if delta < 0:
                    if col == "plays_total":
                        row.reset_flag = True
                    continue  # NULL, never negative
                if k == 1:
                    setattr(row, dcol, delta)
                else:
                    parts = _split_delta(delta, k)
                    for i in range(k - 1):
                        gap_day = date.fromordinal(prev_day.toordinal() + i + 1)
                        gap_row = rows.setdefault(gap_day, DailyRow(day=gap_day, captures=0, gap_days=k))
                        setattr(gap_row, dcol, parts[i])
                    setattr(row, dcol, parts[-1])
                    row.gap_days = k
        rows[day] = row
        prev_day, prev_last = day, last
    return rows


def rolling_7d(deltas_by_day: dict[date, int | None], day: date) -> tuple[int | None, int | None]:
    """(plays_7d, plays_prev_7d) for `day`, treating missing days as 0 but returning None when there
    is no data at all in the window."""

    def window(start_offset: int) -> int | None:
        vals = []
        for i in range(start_offset, start_offset + 7):
            d = date.fromordinal(day.toordinal() - i)
            v = deltas_by_day.get(d)
            if v is not None:
                vals.append(v)
        return sum(vals) if vals else None

    return window(0), window(7)


def growth(plays_7d: int | None, plays_prev_7d: int | None, noise_floor: int = NOISE_FLOOR) -> float | None:
    if plays_7d is None or plays_prev_7d is None or plays_prev_7d < noise_floor:
        return None
    return plays_7d / plays_prev_7d - 1


def acceleration(growth_now: float | None, growth_week_ago: float | None) -> float | None:
    if growth_now is None or growth_week_ago is None:
        return None
    return growth_now - growth_week_ago


# --------------------------------------------------------------------------- SQL derive step
# These run in Postgres after each run and are idempotent (upserts keyed on (id, day)).

SQL_ROLLING = """
with d as (
  select gd.game_id, gd.day,
         sum(gd2.plays_delta) filter (where gd2.day >  gd.day - 7  and gd2.day <= gd.day)       as plays_7d,
         sum(gd2.plays_delta) filter (where gd2.day >  gd.day - 14 and gd2.day <= gd.day - 7)   as plays_prev_7d
  from game_daily gd
  join game_daily gd2 on gd2.game_id = gd.game_id and gd2.day > gd.day - 14 and gd2.day <= gd.day
  where gd.day = any(%(days)s)
  group by gd.game_id, gd.day
)
update game_daily g
   set plays_7d = d.plays_7d,
       plays_prev_7d = d.plays_prev_7d,
       growth_7d = case when d.plays_prev_7d is null or d.plays_prev_7d < %(noise)s then null
                        else d.plays_7d::numeric / d.plays_prev_7d - 1 end
  from d
 where g.game_id = d.game_id and g.day = d.day;
"""

SQL_ACCEL = """
update game_daily g
   set acceleration = g.growth_7d - w.growth_7d
  from game_daily w
 where w.game_id = g.game_id and w.day = g.day - 7
   and g.day = any(%(days)s) and g.growth_7d is not null and w.growth_7d is not null;
"""

SQL_RANK = """
with r as (
  select gd.game_id, gd.day,
         rank() over (partition by g.source, gd.day order by gd.plays_7d desc nulls last, gd.plays_delta desc nulls last, gd.plays_total_eod desc nulls last, gd.game_id) as rk
  from game_daily gd join games g on g.id = gd.game_id
  where gd.day = any(%(days)s)
)
update game_daily gd set rank_in_source = r.rk from r where r.game_id = gd.game_id and r.day = gd.day;
"""

SQL_CREATOR_DAILY = """
insert into creator_daily (creator_id, day, plays_delta, ccu_peak, games_live, games_new, hit_games, share_of_source, plays_7d, growth_7d, acceleration)
with p90 as (
  select g.source, gd.day, percentile_cont(0.9) within group (order by gd.plays_7d) as p90, sum(gd.plays_7d) as src_7d
  from game_daily gd join games g on g.id = gd.game_id
  where gd.day = any(%(days)s) and gd.plays_7d is not null
  group by g.source, gd.day
),
agg as (
  select g.creator_id, gd.day, g.source,
         sum(gd.plays_delta) as plays_delta,
         max(gd.ccu_peak) as ccu_peak,
         count(*) filter (where g.status = 'live') as games_live,
         count(*) filter (where g.first_seen::date = gd.day) as games_new,
         count(*) filter (where gd.plays_7d is not null and gd.plays_7d >= p.p90 and p.p90 > 0) as hit_games,
         sum(gd.plays_7d) as plays_7d,
         max(p.src_7d) as src_7d
  from game_daily gd
  join games g on g.id = gd.game_id
  left join p90 p on p.source = g.source and p.day = gd.day
  where gd.day = any(%(days)s) and g.creator_id is not null
  group by g.creator_id, gd.day, g.source
),
prev as (
  select g.creator_id, (gd.day + 7) as day, sum(gd.plays_7d) as plays_prev_7d
  from game_daily gd join games g on g.id = gd.game_id
  where (gd.day + 7) = any(%(days)s) and g.creator_id is not null
  group by g.creator_id, gd.day
)
select a.creator_id, a.day, a.plays_delta, a.ccu_peak, a.games_live, a.games_new, a.hit_games,
       case when a.src_7d > 0 then a.plays_7d::numeric / a.src_7d end,
       a.plays_7d,
       case when pr.plays_prev_7d is null or pr.plays_prev_7d < %(noise)s then null else a.plays_7d::numeric / pr.plays_prev_7d - 1 end,
       null
from agg a left join prev pr on pr.creator_id = a.creator_id and pr.day = a.day
on conflict (creator_id, day) do update set
  plays_delta = excluded.plays_delta, ccu_peak = excluded.ccu_peak, games_live = excluded.games_live,
  games_new = excluded.games_new, hit_games = excluded.hit_games, share_of_source = excluded.share_of_source,
  plays_7d = excluded.plays_7d, growth_7d = excluded.growth_7d;
"""

SQL_CREATOR_ACCEL = """
update creator_daily c set acceleration = c.growth_7d - w.growth_7d
  from creator_daily w
 where w.creator_id = c.creator_id and w.day = c.day - 7 and c.day = any(%(days)s)
   and c.growth_7d is not null and w.growth_7d is not null;
"""

SQL_PLATFORM_DAILY = """
insert into platform_daily (source, day, plays_delta_top100, ccu_peak_top100, ccu_avg_top100, games_tracked, games_new, creators_active, top10_share)
with top100 as (
  select gd.game_id, g.source, gd.day, gd.plays_delta, gd.plays_7d, gd.rank_in_source
  from game_daily gd join games g on g.id = gd.game_id
  where gd.day = any(%(days)s) and gd.rank_in_source <= 100
),
ccu as (
  select g.source, s.run_ts::date as day, sum(s.players_now) as ccu_sum
  from snapshots s
  join games g on g.id = s.game_id
  join top100 t on t.game_id = s.game_id and t.day = s.run_ts::date
  where s.players_now is not null and s.run_ts::date = any(%(days)s)
  group by g.source, s.run_ts::date, s.run_ts
),
ccu_day as (select source, day, max(ccu_sum) as ccu_peak, avg(ccu_sum) as ccu_avg from ccu group by source, day),
all_tracked as (
  select g.source, gd.day, count(*) as games_tracked,
         count(*) filter (where g.first_seen::date = gd.day) as games_new,
         count(distinct g.creator_id) filter (where gd.plays_delta > 0) as creators_active,
         sum(gd.plays_7d) as all_7d,
         sum(gd.plays_7d) filter (where gd.rank_in_source <= 10) as top10_7d
  from game_daily gd join games g on g.id = gd.game_id
  where gd.day = any(%(days)s)
  group by g.source, gd.day
)
select a.source, a.day,
       (select sum(plays_delta) from top100 t where t.source = a.source and t.day = a.day),
       c.ccu_peak, c.ccu_avg,
       a.games_tracked, a.games_new, a.creators_active,
       case when a.all_7d > 0 then a.top10_7d::numeric / a.all_7d end
from all_tracked a left join ccu_day c on c.source = a.source and c.day = a.day
on conflict (source, day) do update set
  plays_delta_top100 = excluded.plays_delta_top100, ccu_peak_top100 = excluded.ccu_peak_top100,
  ccu_avg_top100 = excluded.ccu_avg_top100, games_tracked = excluded.games_tracked, games_new = excluded.games_new,
  creators_active = excluded.creators_active, top10_share = excluded.top10_share;
"""
