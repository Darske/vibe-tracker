"""Plain SQL used by dashboard/queries.py, kept free of streamlit/pandas imports so tests (CI installs only
.[dev]) can run it against a database."""

PLATFORM_NOW_SQL = """
with latest as (
  select distinct on (s.game_id) s.game_id, s.run_ts, s.players_now, s.plays_total, s.likes,
         greatest(s.captured_at, coalesce(s.last_seen_at, s.captured_at)) as seen
    from snapshots s
   where s.captured_at > now() - interval '3 days'
   order by s.game_id, s.run_ts desc
),
per as (select g.source, l.*, g.creator_id, max(l.seen) over (partition by g.source) as source_seen
          from latest l join games g on g.id = l.game_id)
select source, count(*) as games_tracked, count(distinct creator_id) as creators,
       sum(players_now) filter (where seen > source_seen - interval '90 minutes') as players_now,
       sum(plays_total) as plays_lifetime, max(run_ts) as run_ts,
       sum(likes) filter (where plays_total is not null) as likes_lifetime,
       sum(plays_total) filter (where likes is not null) as plays_with_likes,
       percentile_cont(0.5) within group (order by likes * 1000.0 / plays_total)
         filter (where likes is not null and plays_total >= 1000) as median_likes_per_k,
       count(*) filter (where likes is not null and plays_total >= 1000) as games_with_ratio
  from per group by source
"""
