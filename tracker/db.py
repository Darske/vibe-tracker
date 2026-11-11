"""Postgres access: connection, schema init, upserts, derive step, rollup.

Secrets come from environment variables only (DATABASE_URL = Supabase *session pooler* string).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tracker import metrics
from tracker.models import CreatorProfile, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)
SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (use the Supabase session-pooler connection string)")
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=False, connect_timeout=20)
    conn.execute("set time zone 'UTC'")  # every ::date cast and every Python .date() must agree on UTC days
    conn.commit()
    return conn


# One-off data migrations, each run once (recorded in `maintenance`). Order matters within a migration.
MIGRATIONS: list[tuple[str, list[str]]] = [
    ("thin_history_v1", [
        "SELECT 'thin'",            # placeholder: the thinning itself runs in migrate() via thin_history()
        "VACUUM FULL snapshots",    # give the freed space back so the database size actually drops
    ]),
    ("placements_top100_v1", [
        # 1. remember when each game was last in a list, before the rows that prove it are deleted
        """update games g set last_listed = x.m
             from (select game_id, max(run_ts) as m from list_placements group by game_id) x
            where g.id = x.game_id and (g.last_listed is null or g.last_listed < x.m)""",
        # 2. positions beyond 100 only came from the deep discovery lists (Tesana catalog, Rosebud top 1000)
        "delete from list_placements where position > 100",
    ]),
]


def init(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()
    migrate(conn)


def migrate(conn) -> list[str]:
    done = []
    for key, statements in MIGRATIONS:
        if get_maintenance(conn, f"migration:{key}") is not None:
            continue
        for sql in statements:
            if sql == "SELECT 'thin'":
                thin_history(conn)
            elif sql.upper().startswith("VACUUM"):
                conn.commit()
                conn.autocommit = True  # VACUUM cannot run inside a transaction
                try:
                    conn.execute(sql)
                finally:
                    conn.autocommit = False
            else:
                conn.execute(sql)
        set_maintenance(conn, f"migration:{key}", datetime.now(timezone.utc).isoformat())
        conn.commit()
        log.info("migration %s applied", key)
        done.append(key)
    return done


# ------------------------------------------------------------------ sources
def upsert_source(conn, cfg: dict, display_name: str, base_url: str, segment: str) -> None:
    conn.execute(
        """insert into sources (name, display_name, base_url, segment, metric_map, poll_every_hours, enabled, cohort)
           values (%s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (name) do update set display_name = excluded.display_name, base_url = excluded.base_url,
             segment = excluded.segment, metric_map = excluded.metric_map, cohort = excluded.cohort,
             poll_every_hours = excluded.poll_every_hours, enabled = excluded.enabled""",
        (cfg["name"], display_name, base_url, segment, Jsonb(cfg.get("metric_map") or {}), cfg["poll_every_hours"], cfg["enabled"],
         cfg.get("cohort", "new_entrant")),
    )


def last_successful_run(conn, source: str) -> datetime | None:
    row = conn.execute(
        "select max(run_ts) as ts from runs where source = %s and status in ('ok','partial')", (source,)
    ).fetchone()
    return row["ts"] if row else None


# ------------------------------------------------------------------ creators / games
def upsert_creator(conn, source: str, external_id: str, name: str | None, url: str | None, now: datetime) -> int:
    row = conn.execute(
        """insert into creators (source, external_id, name, url, canonical_key, first_seen, last_seen)
           values (%s, %s, %s, %s, %s, %s, %s)
           on conflict (source, external_id) do update set
             name = coalesce(excluded.name, creators.name), url = coalesce(excluded.url, creators.url),
             last_seen = excluded.last_seen
           returning id""",
        (source, external_id, name, url, canonical_key(name or external_id), now, now),
    ).fetchone()
    return row["id"]


def update_creator_profile(conn, creator_id: int, p: CreatorProfile, now: datetime) -> None:
    conn.execute(
        """update creators set name = coalesce(%s, name), url = coalesce(%s, url), avatar_url = coalesce(%s, avatar_url),
             followers = %s, bio = coalesce(%s, bio), links = %s, profile_refreshed_at = %s where id = %s""",
        (p.name, p.url, p.avatar_url, p.followers, p.bio, Jsonb(p.links or {}), now, creator_id),
    )


def canonical_key(handle: str) -> str:
    return "".join(ch for ch in handle.lower().strip() if ch.isalnum() or ch in "-_")[:64] or handle[:64]


def upsert_game(conn, snap: GameSnapshot, creator_id: int | None, now: datetime) -> int:
    row = conn.execute(
        """insert into games (source, external_id, url, title, thumbnail_url, creator_id, ai_tool, engine, tags, published_at, first_seen, last_seen, status)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'live')
           on conflict (source, external_id) do update set
             url = excluded.url,
             title = coalesce(excluded.title, games.title),
             thumbnail_url = coalesce(excluded.thumbnail_url, games.thumbnail_url),
             creator_id = coalesce(excluded.creator_id, games.creator_id),
             ai_tool = coalesce(excluded.ai_tool, games.ai_tool),
             engine = coalesce(excluded.engine, games.engine),
             tags = case when excluded.tags = '[]'::jsonb then games.tags else excluded.tags end,
             published_at = coalesce(excluded.published_at, games.published_at),
             last_seen = excluded.last_seen,
             status = case when games.status = 'gone' then 'gone' else 'live' end
           returning id""",
        (snap.source, snap.external_id, snap.url, snap.title, snap.thumbnail_url, creator_id, snap.ai_tool, snap.engine,
         Jsonb(snap.tags or []), snap.published_at, now, now),
    ).fetchone()
    return row["id"]


def upsert_game_ref(conn, source: str, external_id: str, url: str, now: datetime) -> int:
    row = conn.execute(
        """insert into games (source, external_id, url, first_seen, last_seen) values (%s, %s, %s, %s, %s)
           on conflict (source, external_id) do update set last_seen = excluded.last_seen,
             status = case when games.status = 'gone' then 'live' else games.status end
           returning id""",
        (source, external_id, url, now, now),
    ).fetchone()
    return row["id"]


# ------------------------------------------------------------------ bulk variants (one round trip per batch)
# psycopg's executemany runs in pipeline mode, so a batch of N upserts costs ~1 round trip instead of N.
# From a GitHub runner to Supabase a round trip is ~100 ms, which made per-row writes the slowest part of a run.

def upsert_game_refs_bulk(conn, source: str, refs: list[tuple[str, str]], now: datetime) -> dict[str, int]:
    """refs = [(external_id, url)]. Returns {external_id: game_id}."""
    if not refs:
        return {}
    with conn.cursor() as cur:
        cur.executemany(
            """insert into games (source, external_id, url, first_seen, last_seen, last_listed) values (%s, %s, %s, %s, %s, %s)
               on conflict (source, external_id) do update set last_seen = excluded.last_seen, last_listed = excluded.last_listed,
                 status = case when games.status = 'gone' then 'live' else games.status end""",
            [(source, eid, url, now, now, now) for eid, url in refs],
        )
    return _ids(conn, "games", source, [eid for eid, _ in refs])


def upsert_creators_bulk(conn, source: str, creators: dict[str, tuple[str | None, str | None]], now: datetime) -> dict[str, int]:
    """creators = {external_id: (name, url)}. Returns {external_id: creator_id}."""
    if not creators:
        return {}
    with conn.cursor() as cur:
        cur.executemany(
            """insert into creators (source, external_id, name, url, canonical_key, first_seen, last_seen)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (source, external_id) do update set
                 name = coalesce(excluded.name, creators.name), url = coalesce(excluded.url, creators.url),
                 last_seen = excluded.last_seen""",
            [(source, eid, name, url, canonical_key(name or eid), now, now) for eid, (name, url) in creators.items()],
        )
    return _ids(conn, "creators", source, list(creators))


def upsert_games_bulk(conn, snaps: list[GameSnapshot], creator_ids: dict[str, int], now: datetime) -> dict[str, int]:
    if not snaps:
        return {}
    source = snaps[0].source
    with conn.cursor() as cur:
        cur.executemany(
            """insert into games (source, external_id, url, title, thumbnail_url, creator_id, ai_tool, engine, tags, published_at, first_seen, last_seen, status, ai_disclosure)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (source, external_id) do update set
                 ai_disclosure = coalesce(excluded.ai_disclosure, games.ai_disclosure),
                 url = excluded.url,
                 title = coalesce(excluded.title, games.title),
                 thumbnail_url = coalesce(excluded.thumbnail_url, games.thumbnail_url),
                 creator_id = coalesce(excluded.creator_id, games.creator_id),
                 ai_tool = coalesce(excluded.ai_tool, games.ai_tool),
                 engine = coalesce(excluded.engine, games.engine),
                 tags = case when excluded.tags = '[]'::jsonb then games.tags else excluded.tags end,
                 published_at = coalesce(excluded.published_at, games.published_at),
                 last_seen = excluded.last_seen,
                 status = case when excluded.status = 'gone' then 'gone'
                               when games.status = 'gone' then 'gone' else 'live' end""",
            [(s.source, s.external_id, s.url, s.title, s.thumbnail_url,
              creator_ids.get(s.creator_external_id) if s.creator_external_id else None,
              s.ai_tool, s.engine, Jsonb(s.tags or []), s.published_at, now, now,
              "gone" if s.status == "gone" else "live", s.ai_disclosure) for s in snaps],
        )
    return _ids(conn, "games", source, [s.external_id for s in snaps])


COUNTERS = ("plays_total", "plays_24h", "players_now", "players_total", "likes", "remixes", "comments", "votes", "rating")


def _utc_day(ts: datetime):
    return ts.astimezone(timezone.utc).date()


def _same(a, b) -> bool:
    """Counter equality; floats (rating, stored as real) compared at 2 decimals so 4.61 == 4.6100001."""
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and round(float(a), 2) == round(float(b), 2)
    return a == b


def split_repeats(prev: dict[int, dict], rows: list[tuple[int, GameSnapshot]], run_ts: datetime):
    """-> (to_insert, to_fold). A reading is folded into the game's last stored row when every counter is equal and
    both are from the same UTC day (the first reading of each day is always stored, so daily end-of-day values,
    gaps and rollups keep working), and nobody is playing (players_now > 0 is always stored, because the dashboard
    sums live players per run; a missing zero does not change a sum). Pure, so it is unit-tested."""
    insert, fold = [], []
    for gid, s in rows:
        p = prev.get(gid)
        if (p is not None and p["run_ts"] != run_ts and _utc_day(p["captured_at"]) == _utc_day(s.captured_at)
                and not (s.players_now or 0) > 0  # live players are summed per run on the dashboard: never fold them
                and all(_same(p.get(c), getattr(s, c)) for c in COUNTERS)):
            fold.append((s.captured_at, gid, p["run_ts"]))
        else:
            insert.append((gid, s))
    return insert, fold


def insert_snapshots_bulk(conn, rows: list[tuple[int, GameSnapshot]], run_ts: datetime) -> dict:
    if not rows:
        return {"inserted": 0, "folded": 0}
    prev = {r["game_id"]: r for r in conn.execute(
        f"""select distinct on (game_id) game_id, run_ts, captured_at, {", ".join(COUNTERS)}
              from snapshots where game_id = any(%s) order by game_id, run_ts desc""",
        ([gid for gid, _ in rows],)).fetchall()}
    insert, fold = split_repeats(prev, rows, run_ts)
    with conn.cursor() as cur:
        if insert:
            cur.executemany(
                """insert into snapshots (game_id, run_ts, captured_at, plays_total, plays_24h, players_now, players_total, likes, remixes, comments, votes, rating)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (game_id, run_ts) do update set captured_at = excluded.captured_at, plays_total = excluded.plays_total,
                     plays_24h = excluded.plays_24h, players_now = excluded.players_now, players_total = excluded.players_total,
                     likes = excluded.likes, remixes = excluded.remixes, comments = excluded.comments, votes = excluded.votes, rating = excluded.rating""",
                [(gid, run_ts, s.captured_at, s.plays_total, s.plays_24h, s.players_now, s.players_total, s.likes, s.remixes, s.comments, s.votes, s.rating)
                 for gid, s in insert],
            )
        if fold:
            cur.executemany("update snapshots set repeats = repeats + 1, last_seen_at = %s where game_id = %s and run_ts = %s", fold)
    return {"inserted": len(insert), "folded": len(fold)}


def _ids(conn, table: str, source: str, external_ids: list[str]) -> dict[str, int]:
    rows = conn.execute(
        f"select id, external_id from {table} where source = %s and external_id = any(%s)", (source, external_ids)
    ).fetchall()
    return {r["external_id"]: r["id"] for r in rows}


def mark_status(conn, game_id: int, status: str) -> None:
    conn.execute("update games set status = %s where id = %s", (status, game_id))


def insert_placements(conn, placements: list[tuple[ListPlacement, int]], run_ts: datetime) -> None:
    if not placements:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into list_placements (source, list_name, position, game_id, run_ts) values (%s, %s, %s, %s, %s)
               on conflict do nothing""",
            [(p.source, p.list_name, p.position, gid, run_ts) for p, gid in placements],
        )


def insert_snapshot(conn, game_id: int, s: GameSnapshot, run_ts: datetime) -> None:
    conn.execute(
        """insert into snapshots (game_id, run_ts, captured_at, plays_total, plays_24h, players_now, players_total, likes, remixes, comments, votes)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (game_id, run_ts) do update set captured_at = excluded.captured_at, plays_total = excluded.plays_total,
             plays_24h = excluded.plays_24h, players_now = excluded.players_now, players_total = excluded.players_total,
             likes = excluded.likes, remixes = excluded.remixes, comments = excluded.comments, votes = excluded.votes""",
        (game_id, run_ts, s.captured_at, s.plays_total, s.plays_24h, s.players_now, s.players_total, s.likes, s.remixes, s.comments, s.votes),
    )


def watchlist(conn, source: str, stale_after_days: int, max_watchlist: int) -> list[dict]:
    """Games seen in any of this platform's lists (games.last_listed) in the last stale_after_days, prioritised by
    most recent last_seen then highest plays_7d, capped at max_watchlist."""
    return conn.execute(
        """select g.id, g.external_id, g.url
             from games g
             left join lateral (select plays_7d from game_daily gd where gd.game_id = g.id order by day desc limit 1) d on true
            where g.source = %s and g.status <> 'gone'
              and g.last_listed > now() - make_interval(days => %s)
            order by g.last_seen desc, d.plays_7d desc nulls last
            limit %s""",
        (source, stale_after_days, max_watchlist),
    ).fetchall()


def mark_stale(conn, source: str, stale_after_days: int) -> int:
    cur = conn.execute(
        """update games set status = 'stale' where source = %s and status = 'live'
              and last_seen < now() - make_interval(days => %s)""",
        (source, stale_after_days),
    )
    return cur.rowcount


def creators_needing_profile(conn, source: str, today: date) -> list[dict]:
    return conn.execute(
        """select id, external_id from creators where source = %s
              and (profile_refreshed_at is null or profile_refreshed_at < now() - interval '14 days' or first_seen::date = %s)
            limit 200""",
        (source, today),
    ).fetchall()


# ------------------------------------------------------------------ runs
def log_run(conn, source: str, run_ts: datetime, started_at: datetime, status: str, counts: dict, errors: list, seconds: float, requests: int | None) -> None:
    conn.execute(
        """insert into runs (source, run_ts, started_at, finished_at, status, rows_discovered, rows_snapshotted, rows_gone, seconds, requests, errors)
           values (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s)
           on conflict (source, run_ts) do update set finished_at = now(), status = excluded.status,
             rows_discovered = excluded.rows_discovered, rows_snapshotted = excluded.rows_snapshotted, rows_gone = excluded.rows_gone,
             seconds = excluded.seconds, requests = excluded.requests, errors = excluded.errors""",
        (source, run_ts, started_at, status, counts.get("discovered", 0), counts.get("snapshotted", 0), counts.get("gone", 0),
         round(seconds, 2), requests, Jsonb([str(e)[:500] for e in errors])),
    )


# ------------------------------------------------------------------ derive
def derive(conn, days: list[date], sources: list[str] | None = None) -> None:
    """Recompute game_daily for `days` (today and yesterday), then rolling/rank, creator_daily, platform_daily."""
    lookback_start = min(days) - timedelta(days=40)
    where_src = "and g.source = any(%(sources)s)" if sources else ""
    params = {"start": lookback_start, "end": max(days) + timedelta(days=1), "sources": sources}

    # last capture per game per day for the lookback window (small), plus all captures on target days (for CCU)
    rows = conn.execute(
        f"""with win as (
               select s.game_id, s.captured_at, s.plays_total, s.likes, s.remixes, s.votes, s.players_now, s.repeats
                 from snapshots s join games g on g.id = s.game_id
                where s.captured_at >= %(start)s and s.captured_at < %(end)s {where_src}
             ),
             last_per_day as (
               select distinct on (game_id, (captured_at at time zone 'UTC')::date) *
                 from win order by game_id, (captured_at at time zone 'UTC')::date, captured_at desc
             )
             select * from last_per_day
             union
             select * from win where (captured_at at time zone 'UTC')::date = any(%(days)s)
             order by game_id, captured_at""",
        {**params, "days": days},
    ).fetchall()

    by_game: dict[int, list[metrics.Capture]] = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(
            metrics.Capture(r["captured_at"], r["plays_total"], r["likes"], r["remixes"], r["votes"], r["players_now"], 1 + (r["repeats"] or 0))
        )

    upserts, gap_fills = [], []
    target = set(days)
    for game_id, caps in by_game.items():
        for day, row in metrics.daily_rows(caps).items():
            vals = (game_id, day, row.captures, row.plays_delta, row.likes_delta, row.remixes_delta, row.votes_delta,
                    row.plays_total_eod, row.ccu_peak, row.ccu_avg, row.gap_days, row.reset_flag)
            if day in target:
                upserts.append(vals)
            elif row.gap_days and day >= lookback_start:
                gap_fills.append(vals)
    with conn.cursor() as cur:
        # Older days re-touched only to spread a gap's plays: their intraday readings may already be thinned
        # (thin_history), so keep the stored poll count and players-online peak/average and update the plays only.
        cur.executemany(
            """insert into game_daily (game_id, day, captures, plays_delta, likes_delta, remixes_delta, votes_delta, plays_total_eod, ccu_peak, ccu_avg, gap_days, reset_flag)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (game_id, day) do update set plays_delta = excluded.plays_delta,
                 likes_delta = excluded.likes_delta, remixes_delta = excluded.remixes_delta, votes_delta = excluded.votes_delta,
                 plays_total_eod = excluded.plays_total_eod, gap_days = excluded.gap_days, reset_flag = excluded.reset_flag""",
            gap_fills,
        )
        cur.executemany(
            """insert into game_daily (game_id, day, captures, plays_delta, likes_delta, remixes_delta, votes_delta, plays_total_eod, ccu_peak, ccu_avg, gap_days, reset_flag)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (game_id, day) do update set captures = excluded.captures, plays_delta = excluded.plays_delta,
                 likes_delta = excluded.likes_delta, remixes_delta = excluded.remixes_delta, votes_delta = excluded.votes_delta,
                 plays_total_eod = excluded.plays_total_eod, ccu_peak = excluded.ccu_peak, ccu_avg = excluded.ccu_avg,
                 gap_days = excluded.gap_days, reset_flag = excluded.reset_flag""",
            upserts,
        )
    p = {"days": days, "noise": metrics.NOISE_FLOOR}
    conn.execute(metrics.SQL_ROLLING, p)
    conn.execute(metrics.SQL_ACCEL, p)
    conn.execute(metrics.SQL_RANK, p)
    conn.execute(metrics.SQL_CREATOR_DAILY, p)
    conn.execute(metrics.SQL_CREATOR_ACCEL, p)
    conn.execute(metrics.SQL_PLATFORM_DAILY, p)
    conn.commit()
    log.info("derive: %d game_daily rows for %s", len(upserts), ", ".join(str(d) for d in days))


# ------------------------------------------------------------------ rollup
def upsert_platform_totals(conn, source: str, now: datetime, t: dict) -> None:
    """One row per platform per UTC day: latest reading wins, peaks keep the day's maximum."""
    conn.execute(
        """insert into platform_totals (source, day, captured_at, plays_total, games, creators, players_now, players_now_peak,
                                        people_recent, people_recent_peak, extra)
           values (%(s)s, %(d)s, %(t)s, %(plays)s, %(games)s, %(creators)s, %(pn)s, %(pn)s, %(pr)s, %(pr)s, %(extra)s)
           on conflict (source, day) do update set
             captured_at = excluded.captured_at,
             plays_total = coalesce(excluded.plays_total, platform_totals.plays_total),
             games = coalesce(excluded.games, platform_totals.games),
             creators = coalesce(excluded.creators, platform_totals.creators),
             players_now = coalesce(excluded.players_now, platform_totals.players_now),
             players_now_peak = greatest(excluded.players_now_peak, platform_totals.players_now_peak),
             people_recent = coalesce(excluded.people_recent, platform_totals.people_recent),
             people_recent_peak = greatest(excluded.people_recent_peak, platform_totals.people_recent_peak),
             extra = platform_totals.extra || excluded.extra""",
        {"s": source, "d": now.astimezone(timezone.utc).date(), "t": now, "plays": t.get("plays_total"), "games": t.get("games"),
         "creators": t.get("creators"), "pn": t.get("players_now"), "pr": t.get("people_recent"), "extra": Jsonb(t.get("extra") or {})},
    )


def thin_history(conn, today: date | None = None) -> int:
    """Keep only one reading per game per day for every day before yesterday: the day's last reading, which carries
    the end-of-day counters daily plays are computed from. Intraday readings (every 30 min / 2 h) are deleted; the
    day's players-online peak and average are already stored in game_daily / platform_daily. Today and yesterday stay
    in full because derive() still recomputes yesterday."""
    today = today or datetime.now(timezone.utc).date()
    cutoff = datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    cur = conn.execute(
        """delete from snapshots s using (
             select game_id, (captured_at at time zone 'UTC')::date as d, max(captured_at) as keep
               from snapshots where captured_at < %s group by 1, 2) k
           where s.game_id = k.game_id and (s.captured_at at time zone 'UTC')::date = k.d and s.captured_at < k.keep""",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


rollup = thin_history  # old name


def set_maintenance(conn, key: str, value) -> None:
    conn.execute(
        "insert into maintenance (key, value, updated_at) values (%s, %s, now()) on conflict (key) do update set value = excluded.value, updated_at = now()",
        (key, Jsonb(value)),
    )


def get_maintenance(conn, key: str):
    row = conn.execute("select value from maintenance where key = %s", (key,)).fetchone()
    return row["value"] if row else None


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m tracker.db")
    ap.add_argument("cmd", choices=["init", "rollup"])
    a = ap.parse_args()
    with connect() as conn:
        if a.cmd == "init":
            init(conn)
            print("schema applied")
        elif a.cmd == "rollup":
            print(f"rolled up {rollup(conn)} snapshot rows")
    sys.exit(0)
