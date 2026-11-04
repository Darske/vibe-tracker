"""All SQL used by the dashboard, in one place, cached (st.cache_data, ttl 600 s).

The dashboard only reads, plus two small tables it writes: notes and creator_merges.
No platform-specific code anywhere: platforms, segments and metric labels come from the sources table.
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import psycopg
import streamlit as st

TTL = 600


def _url() -> str:
    url = os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL", None)
    if not url:
        raise RuntimeError("DATABASE_URL is missing: set it in Streamlit secrets")
    return url


def _read(sql: str, params=None) -> pd.DataFrame:
    with psycopg.connect(_url(), connect_timeout=15) as conn:
        conn.execute("set time zone 'UTC'")
        return pd.read_sql_query(sql, conn, params=params)  # type: ignore[arg-type]


def _write(sql: str, params=None) -> None:
    with psycopg.connect(_url(), connect_timeout=15) as conn:
        conn.execute(sql, params)
        conn.commit()


# ====================================================================== sources
@st.cache_data(ttl=TTL)
def sources() -> pd.DataFrame:
    return _read("select name, display_name, base_url, segment, metric_map, poll_every_hours, enabled, added_at from sources order by added_at, name")


# ====================================================================== health
@st.cache_data(ttl=120)
def last_runs() -> pd.DataFrame:
    return _read(
        """select distinct on (r.source) r.source, s.display_name, r.run_ts, r.status, r.rows_discovered, r.rows_snapshotted,
                  r.rows_gone, r.seconds, r.requests, r.errors
             from runs r join sources s on s.name = r.source
            order by r.source, r.run_ts desc"""
    )


@st.cache_data(ttl=120)
def runs_history(days: int = 14) -> pd.DataFrame:
    return _read(
        """select r.source, s.display_name, r.run_ts, r.status, r.rows_snapshotted, r.seconds, r.requests
             from runs r join sources s on s.name = r.source
            where r.run_ts > now() - make_interval(days => %s) order by r.run_ts""",
        (days,),
    )


@st.cache_data(ttl=TTL)
def health_stats() -> dict:
    row = _read(
        """select (select min(captured_at)::date from snapshots) as first_day,
                  (select count(*) from snapshots) as snapshot_rows,
                  (select count(*) from games) as games,
                  (select count(*) from creators) as creators,
                  (select pg_database_size(current_database())) as db_bytes,
                  (select count(distinct run_ts) from runs where run_ts >= date_trunc('month', now())) as runs_this_month,
                  (select coalesce(sum(seconds), 0) from runs where run_ts >= date_trunc('month', now())) as seconds_this_month,
                  (select avg(total) from (select run_ts, sum(seconds) as total from runs where run_ts > now() - interval '7 days' group by run_ts) t) as avg_run_seconds_7d,
                  (select max(captured_at) from snapshots where players_now is not null) as last_ccu_reading,
                  (select count(distinct run_ts) from snapshots where players_now is not null and captured_at > now() - interval '24 hours') as ccu_polls_24h"""
    ).iloc[0]
    return row.to_dict()


@st.cache_data(ttl=120)
def row_drop_warnings() -> pd.DataFrame:
    return _read(
        """with med as (
             select source, percentile_cont(0.5) within group (order by rows_snapshotted) as median
               from runs where run_ts > now() - interval '7 days' and status in ('ok','partial') group by source),
           last as (select distinct on (source) source, rows_snapshotted, run_ts from runs order by source, run_ts desc)
           select l.source, l.rows_snapshotted, m.median, l.run_ts
             from last l join med m on m.source = l.source
            where m.median > 0 and l.rows_snapshotted < 0.5 * m.median"""
    )


# ====================================================================== platforms
@st.cache_data(ttl=TTL)
def platform_daily() -> pd.DataFrame:
    """Full platform_daily history joined with source metadata (small table: one row per platform per day)."""
    return _read(
        """select p.*, s.display_name, s.segment, s.metric_map
             from platform_daily p join sources s on s.name = p.source order by p.day"""
    )


@st.cache_data(ttl=TTL)
def platform_now() -> pd.DataFrame:
    """Latest poll per platform: games tracked, creators, players now (sum over tracked games)."""
    return _read(
        """with last as (select distinct on (g.source) g.source, s.run_ts
                          from snapshots s join games g on g.id = s.game_id order by g.source, s.run_ts desc)
           select g.source, count(distinct g.id) as games_tracked, count(distinct g.creator_id) as creators,
                  sum(s.players_now) as players_now, sum(s.plays_total) as plays_lifetime, max(s.run_ts) as run_ts,
                  sum(s.likes) filter (where s.plays_total is not null) as likes_lifetime,
                  sum(s.plays_total) filter (where s.likes is not null) as plays_with_likes,
                  percentile_cont(0.5) within group (order by s.likes * 1000.0 / s.plays_total)
                    filter (where s.likes is not null and s.plays_total >= 1000) as median_likes_per_k,
                  count(*) filter (where s.likes is not null and s.plays_total >= 1000) as games_with_ratio
             from snapshots s join games g on g.id = s.game_id join last l on l.source = g.source and l.run_ts = s.run_ts
            group by g.source"""
    )


# ====================================================================== concurrent players (rotrends-style hero)
@st.cache_data(ttl=120)
def ccu_total_series(days: int, sources_sel: tuple[str, ...]) -> pd.DataFrame:
    """Sum of players_now over all tracked games per poll (run_ts) — 'players right now' over time."""
    return _read(
        """select s.run_ts, sum(s.players_now) as players
             from snapshots s join games g on g.id = s.game_id
            where s.players_now is not null and g.source = any(%s) and s.run_ts > now() - make_interval(days => %s)
            group by s.run_ts order by s.run_ts""",
        (list(sources_sel), days),
    )


@st.cache_data(ttl=120)
def ccu_platform_series(days: int, sources_sel: tuple[str, ...]) -> pd.DataFrame:
    return _read(
        """select s.run_ts, g.source, sum(s.players_now) as players
             from snapshots s join games g on g.id = s.game_id
            where s.players_now is not null and g.source = any(%s) and s.run_ts > now() - make_interval(days => %s)
            group by s.run_ts, g.source order by s.run_ts""",
        (list(sources_sel), days),
    )


@st.cache_data(ttl=120)
def ccu_now(sources_sel: tuple[str, ...] | None = None) -> dict:
    """Latest total + all-time peak of players_now summed per poll (run_ts) over the given platforms —
    the same per-run sums the CCU charts draw, so hero number, peak and chart agree."""
    df = _read(
        """with per_run as (
             select s.run_ts, sum(s.players_now) as players
               from snapshots s join games g on g.id = s.game_id
              where s.players_now is not null and (%s::text[] is null or g.source = any(%s::text[]))
              group by s.run_ts)
           select (select players from per_run order by run_ts desc limit 1) as players_now,
                  (select run_ts from per_run order by run_ts desc limit 1) as updated_at,
                  (select players from per_run order by players desc limit 1) as peak,
                  (select run_ts from per_run order by players desc limit 1) as peak_at""",
        (list(sources_sel) if sources_sel is not None else None,) * 2,
    )
    return df.iloc[0].to_dict() if not df.empty else {}


@st.cache_data(ttl=120)
def top_games_now(sources_sel: tuple[str, ...], n: int = 5) -> pd.DataFrame:
    """Games with the most players online at their latest poll (platforms that publish live counts only)."""
    return _read(
        """with last as (select distinct on (game_id) game_id, players_now, captured_at from snapshots
                          where players_now is not null order by game_id, captured_at desc),
                peak as (select game_id, max(players_now) as peak from snapshots group by game_id)
           select g.id, g.source, s.display_name, g.title, g.url, g.thumbnail_url, c.name as creator_name,
                  l.players_now, p.peak, l.captured_at
             from last l join games g on g.id = l.game_id join sources s on s.name = g.source
             left join creators c on c.id = g.creator_id left join peak p on p.game_id = g.id
            where g.source = any(%s) and l.captured_at > now() - interval '6 hours'
            order by l.players_now desc nulls last limit %s""",
        (list(sources_sel), n),
    )


@st.cache_data(ttl=120)
def game_ccu_series(game_ids: tuple[int, ...], days: int) -> pd.DataFrame:
    return _read(
        """select game_id, captured_at, players_now from snapshots
            where game_id = any(%s) and players_now is not null and captured_at > now() - make_interval(days => %s)
            order by captured_at""",
        (list(game_ids), days),
    )


# ====================================================================== games
@st.cache_data(ttl=TTL)
def games_window(start: date, sources_sel: tuple[str, ...]) -> pd.DataFrame:
    """One row per game with window aggregates, latest derived metrics and latest raw counters."""
    return _read(
        """with w as (
             select gd.game_id, sum(gd.plays_delta) as plays_window, max(gd.ccu_peak) as ccu_peak_window,
                    sum(gd.likes_delta) as likes_window, sum(gd.remixes_delta) as remixes_window
               from game_daily gd where gd.day >= %s group by gd.game_id),
           latest as (
             select distinct on (gd.game_id) gd.game_id, gd.day, gd.growth_7d, gd.acceleration, gd.rank_in_source, gd.plays_7d
               from game_daily gd order by gd.game_id, gd.day desc),
           prior as (
             select distinct on (gd.game_id) gd.game_id, gd.rank_in_source as rank_prior
               from game_daily gd where gd.day < %s order by gd.game_id, gd.day desc),
           snap as (
             select distinct on (s.game_id) s.game_id, s.captured_at, s.players_now, s.likes, s.remixes, s.plays_total, s.players_total, s.votes, s.comments
               from snapshots s order by s.game_id, s.captured_at desc)
           select g.id, g.source, s.display_name, s.segment, s.metric_map, g.title, g.url, g.thumbnail_url, g.published_at, g.first_seen, g.status, g.ai_tool, g.engine, g.tags,
                  c.id as creator_id, c.name as creator_name, c.url as creator_url, c.canonical_key,
                  w.plays_window, w.ccu_peak_window, w.likes_window, w.remixes_window,
                  l.growth_7d, l.acceleration, l.rank_in_source, l.plays_7d, pr.rank_prior,
                  sn.players_now, sn.likes, sn.remixes, sn.plays_total, sn.players_total, sn.votes, sn.comments, sn.captured_at
             from games g
             join sources s on s.name = g.source
             left join creators c on c.id = g.creator_id
             left join w on w.game_id = g.id
             left join latest l on l.game_id = g.id
             left join prior pr on pr.game_id = g.id
             left join snap sn on sn.game_id = g.id
            where g.source = any(%s) and g.status <> 'gone'""",
        (start, start, list(sources_sel)),
    )


@st.cache_data(ttl=TTL)
def game(game_id: int) -> dict | None:
    df = _read(
        """select g.*, s.display_name, s.segment, s.metric_map, c.name as creator_name, c.url as creator_url, c.canonical_key
             from games g join sources s on s.name = g.source left join creators c on c.id = g.creator_id where g.id = %s""",
        (game_id,),
    )
    return None if df.empty else df.iloc[0].to_dict()


@st.cache_data(ttl=TTL)
def game_daily(game_id: int) -> pd.DataFrame:
    return _read("select * from game_daily where game_id = %s order by day", (game_id,))


@st.cache_data(ttl=TTL)
def game_polls(game_id: int, days: int = 7) -> pd.DataFrame:
    return _read(
        "select captured_at, players_now, plays_total from snapshots where game_id = %s and captured_at > now() - make_interval(days => %s) order by captured_at",
        (game_id, days),
    )


@st.cache_data(ttl=TTL)
def game_latest(game_id: int) -> dict | None:
    df = _read("select * from snapshots where game_id = %s order by captured_at desc limit 1", (game_id,))
    return None if df.empty else df.iloc[0].to_dict()


@st.cache_data(ttl=TTL)
def game_placements(game_id: int) -> pd.DataFrame:
    return _read(
        """select list_name, min(run_ts) as first_seen, max(run_ts) as last_seen, min(position) as best_position,
                  count(*) as polls, (array_agg(position order by run_ts desc))[1] as latest_position
             from list_placements where game_id = %s group by list_name order by best_position""",
        (game_id,),
    )


# ====================================================================== creators
@st.cache_data(ttl=TTL)
def creators_window(start: date, sources_sel: tuple[str, ...]) -> pd.DataFrame:
    """One row per creator (per platform) with window plays, latest creator_daily metrics, notes status."""
    return _read(
        """with w as (
             select g.creator_id, sum(gd.plays_delta) as plays_window, max(gd.ccu_peak) as ccu_peak_window
               from game_daily gd join games g on g.id = gd.game_id where gd.day >= %s and g.creator_id is not null group by g.creator_id),
           latest as (
             select distinct on (cd.creator_id) cd.creator_id, cd.day, cd.games_live, cd.hit_games, cd.share_of_source, cd.plays_7d, cd.growth_7d, cd.acceleration
               from creator_daily cd order by cd.creator_id, cd.day desc),
           gl as (
             select g.creator_id, count(*) filter (where g.status <> 'gone') as games_live, min(g.first_seen) as first_game_seen,
                    sum(sn.plays_total) as plays_lifetime
               from games g left join lateral (select plays_total from snapshots s where s.game_id = g.id order by captured_at desc limit 1) sn on true
              group by g.creator_id)
           select c.id, c.source, s.display_name, s.segment, c.external_id, c.name, c.url, c.avatar_url, c.links, c.followers, c.bio, c.canonical_key, c.first_seen,
                  gl.games_live, gl.plays_lifetime, w.plays_window, w.ccu_peak_window,
                  l.hit_games, l.share_of_source, l.plays_7d, l.growth_7d, l.acceleration,
                  n.status as outreach_status, n.note
             from creators c
             join sources s on s.name = c.source
             left join gl on gl.creator_id = c.id
             left join w on w.creator_id = c.id
             left join latest l on l.creator_id = c.id
             left join notes n on n.canonical_key = c.canonical_key
            where c.source = any(%s)""",
        (start, list(sources_sel)),
    )


@st.cache_data(ttl=TTL)
def creator(creator_id: int) -> dict | None:
    df = _read(
        """select c.*, s.display_name, s.segment, s.metric_map, n.status as outreach_status, n.note
             from creators c join sources s on s.name = c.source left join notes n on n.canonical_key = c.canonical_key
            where c.id = %s""",
        (creator_id,),
    )
    return None if df.empty else df.iloc[0].to_dict()


@st.cache_data(ttl=TTL)
def creator_family(canonical_key: str) -> pd.DataFrame:
    """All creator rows (across platforms) sharing a canonical_key — the manual cross-platform identity."""
    return _read(
        "select c.id, c.source, s.display_name, c.name, c.url, c.followers, c.links from creators c join sources s on s.name = c.source where c.canonical_key = %s",
        (canonical_key,),
    )


@st.cache_data(ttl=TTL)
def creator_daily(creator_ids: tuple[int, ...]) -> pd.DataFrame:
    return _read(
        """select cd.*, c.source from creator_daily cd join creators c on c.id = cd.creator_id
            where cd.creator_id = any(%s) order by cd.day""",
        (list(creator_ids),),
    )


@st.cache_data(ttl=TTL)
def creator_games(creator_ids: tuple[int, ...], start: date) -> pd.DataFrame:
    return _read(
        """with w as (select game_id, sum(plays_delta) as plays_window from game_daily where day >= %s group by game_id),
                snap as (select distinct on (game_id) game_id, players_now, plays_total, likes from snapshots order by game_id, captured_at desc)
           select g.id, g.source, s.display_name, g.title, g.url, g.thumbnail_url, g.published_at, g.first_seen, g.status,
                  c.name as creator_name, w.plays_window, sn.players_now, sn.plays_total, sn.likes
             from games g join sources s on s.name = g.source left join creators c on c.id = g.creator_id
             left join w on w.game_id = g.id left join snap sn on sn.game_id = g.id
            where g.creator_id = any(%s) and g.status <> 'gone' order by w.plays_window desc nulls last, sn.plays_total desc nulls last""",
        (start, list(creator_ids)),
    )


# ====================================================================== writes (notes, merges)
def upsert_note(canonical_key: str, status: str, note: str) -> None:
    _write(
        """insert into notes (canonical_key, status, note, updated_at) values (%s, %s, %s, now())
           on conflict (canonical_key) do update set status = excluded.status, note = excluded.note, updated_at = now()""",
        (canonical_key, status, note),
    )
    creators_window.clear()
    creator.clear()


def merge_creator(creator_id: int, canonical_key: str, note: str | None) -> None:
    _write(
        """insert into creator_merges (canonical_key, creator_id, note, updated_at) values (%s, %s, %s, now())
           on conflict (creator_id) do update set canonical_key = excluded.canonical_key, note = excluded.note, updated_at = now()""",
        (canonical_key, creator_id, note),
    )
    _write("update creators set canonical_key = %s where id = %s", (canonical_key, creator_id))
    creators_window.clear()
    creator.clear()
    creator_family.clear()
