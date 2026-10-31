"""All SQL used by the dashboard, in one place, cached. The dashboard only reads (plus notes and
creator_merges, written from the creator page). No platform-specific code anywhere: platforms,
segments and metric labels come from the sources table."""

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


@st.cache_data(ttl=TTL)
def sources() -> pd.DataFrame:
    return _read("select name, display_name, base_url, segment, metric_map, poll_every_hours, enabled, added_at from sources order by added_at, name")


# ---------------------------------------------------------------- health
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
                  (select avg(total) from (select run_ts, sum(seconds) as total from runs where run_ts > now() - interval '7 days' group by run_ts) t) as avg_run_seconds_7d"""
    ).iloc[0]
    return row.to_dict()


@st.cache_data(ttl=120)
def row_drop_warnings() -> pd.DataFrame:
    """Sources whose latest row count is <50% of their trailing-7-day median."""
    return _read(
        """with med as (
             select source, percentile_cont(0.5) within group (order by rows_snapshotted) as median
               from runs where run_ts > now() - interval '7 days' and status in ('ok','partial') group by source),
           last as (select distinct on (source) source, rows_snapshotted, run_ts from runs order by source, run_ts desc)
           select l.source, l.rows_snapshotted, m.median, l.run_ts
             from last l join med m on m.source = l.source
            where m.median > 0 and l.rows_snapshotted < 0.5 * m.median"""
    )


# ---------------------------------------------------------------- platforms / games / creators (milestone 3)
@st.cache_data(ttl=TTL)
def platform_daily(start: date) -> pd.DataFrame:
    return _read(
        """select p.*, s.display_name, s.segment, s.metric_map from platform_daily p join sources s on s.name = p.source
            where p.day >= %s order by p.day""",
        (start,),
    )


@st.cache_data(ttl=TTL)
def games_window(start: date, sources_sel: tuple[str, ...]) -> pd.DataFrame:
    return _read(
        """with w as (
             select gd.game_id, sum(gd.plays_delta) as plays_window, max(gd.ccu_peak) as ccu_peak_window
               from game_daily gd where gd.day >= %s group by gd.game_id),
           latest as (
             select distinct on (gd.game_id) gd.game_id, gd.growth_7d, gd.acceleration, gd.rank_in_source, gd.plays_7d
               from game_daily gd order by gd.game_id, gd.day desc),
           snap as (
             select distinct on (s.game_id) s.game_id, s.players_now, s.likes, s.remixes, s.plays_total
               from snapshots s order by s.game_id, s.captured_at desc)
           select g.id, g.source, s.display_name, s.metric_map, g.title, g.url, g.thumbnail_url, g.published_at, g.first_seen, g.status,
                  c.id as creator_id, c.name as creator_name, c.url as creator_url,
                  w.plays_window, w.ccu_peak_window, l.growth_7d, l.acceleration, l.rank_in_source, l.plays_7d,
                  sn.players_now, sn.likes, sn.remixes, sn.plays_total
             from games g
             join sources s on s.name = g.source
             left join creators c on c.id = g.creator_id
             left join w on w.game_id = g.id
             left join latest l on l.game_id = g.id
             left join snap sn on sn.game_id = g.id
            where g.source = any(%s)""",
        (start, list(sources_sel)),
    )


@st.cache_data(ttl=TTL)
def notes() -> pd.DataFrame:
    return _read("select canonical_key, status, note, updated_at from notes")


def upsert_note(canonical_key: str, status: str, note: str) -> None:
    _write(
        """insert into notes (canonical_key, status, note, updated_at) values (%s, %s, %s, now())
           on conflict (canonical_key) do update set status = excluded.status, note = excluded.note, updated_at = now()""",
        (canonical_key, status, note),
    )
    notes.clear()


def merge_creator(creator_id: int, canonical_key: str, note: str | None) -> None:
    _write(
        """insert into creator_merges (canonical_key, creator_id, note, updated_at) values (%s, %s, %s, now())
           on conflict (creator_id) do update set canonical_key = excluded.canonical_key, note = excluded.note, updated_at = now();
           update creators set canonical_key = %s where id = %s""",
        (canonical_key, creator_id, note, canonical_key, creator_id),
    )
