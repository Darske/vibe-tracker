-- vibe-tracker schema. Idempotent: safe to run on every `python -m tracker.db init`.
-- Raw snapshots are append-only; everything derived is recomputed idempotently after each run.

create table if not exists sources (
  name              text primary key,
  display_name      text not null,
  base_url          text not null,
  segment           text not null check (segment in ('aggregator', 'creation')),
  metric_map        jsonb not null default '{}'::jsonb,
  poll_every_hours  int  not null default 2,
  enabled           boolean not null default true,
  added_at          timestamptz not null default now()
);

create table if not exists creators (
  id             bigserial primary key,
  source         text not null references sources(name),
  external_id    text not null,
  name           text,
  url            text,
  avatar_url     text,
  links          jsonb not null default '{}'::jsonb,
  followers      int,
  bio            text,
  canonical_key  text,
  first_seen     timestamptz not null default now(),
  last_seen      timestamptz not null default now(),
  profile_refreshed_at timestamptz,
  unique (source, external_id)
);
create index if not exists creators_canonical_idx on creators (canonical_key);

create table if not exists creator_merges (
  canonical_key  text not null,
  creator_id     bigint not null references creators(id) on delete cascade,
  note           text,
  updated_at     timestamptz not null default now(),
  primary key (creator_id)
);

create table if not exists games (
  id             bigserial primary key,
  source         text not null references sources(name),
  external_id    text not null,
  url            text not null,
  title          text,
  thumbnail_url  text,
  creator_id     bigint references creators(id),
  ai_tool        text,
  engine         text,
  tags           jsonb not null default '[]'::jsonb,
  published_at   timestamptz,
  first_seen     timestamptz not null default now(),
  last_seen      timestamptz not null default now(),
  status         text not null default 'live' check (status in ('live', 'gone', 'stale')),
  unique (source, external_id)
);
create index if not exists games_source_status_idx on games (source, status);
create index if not exists games_creator_idx on games (creator_id);

create table if not exists snapshots (
  game_id        bigint not null references games(id) on delete cascade,
  run_ts         timestamptz not null,
  captured_at    timestamptz not null,
  plays_total    bigint,
  plays_24h      bigint,
  players_now    int,
  players_total  bigint,
  likes          int,
  remixes        int,
  comments       int,
  votes          int,
  primary key (game_id, run_ts)
);
create index if not exists snapshots_game_captured_idx on snapshots (game_id, captured_at);
create index if not exists snapshots_run_idx on snapshots (run_ts);

create table if not exists list_placements (
  source       text not null references sources(name),
  list_name    text not null,
  position     int  not null,
  game_id      bigint not null references games(id) on delete cascade,
  run_ts       timestamptz not null,
  primary key (source, list_name, game_id, run_ts)
);
create index if not exists list_placements_game_idx on list_placements (game_id, run_ts);
drop index if exists list_placements_run_idx;  -- no query used it (Nov 10 storage review)

create table if not exists game_daily (
  game_id         bigint not null references games(id) on delete cascade,
  day             date not null,
  captures        int not null default 0,
  plays_delta     bigint,
  likes_delta     int,
  remixes_delta   int,
  votes_delta     int,
  plays_total_eod bigint,
  ccu_peak        int,
  ccu_avg         numeric(10,2),
  gap_days        int,
  reset_flag      boolean not null default false,
  rank_in_source  int,
  plays_7d        bigint,
  plays_prev_7d   bigint,
  growth_7d       numeric(12,4),
  acceleration    numeric(12,4),
  primary key (game_id, day)
);
create index if not exists game_daily_day_idx on game_daily (day);

create table if not exists creator_daily (
  creator_id      bigint not null references creators(id) on delete cascade,
  day             date not null,
  plays_delta     bigint,
  ccu_peak        int,
  games_live      int,
  games_new       int,
  hit_games       int,
  share_of_source numeric(12,6),
  plays_7d        bigint,
  growth_7d       numeric(12,4),
  acceleration    numeric(12,4),
  primary key (creator_id, day)
);

create table if not exists platform_daily (
  source                text not null references sources(name),
  day                   date not null,
  plays_delta_top100    bigint,
  ccu_peak_top100       int,
  ccu_avg_top100        numeric(12,2),
  games_tracked         int,
  games_new             int,
  creators_active       int,
  top10_share           numeric(12,6),
  cloudflare_rank_bucket text,
  tranco_rank           int,
  trends_index          numeric(8,2),
  primary key (source, day)
);

create table if not exists notes (
  canonical_key  text primary key,
  status         text not null default 'new' check (status in ('new','watching','contacted','replied','passed','funded')),
  note           text,
  updated_at     timestamptz not null default now()
);

create table if not exists runs (
  id                bigserial primary key,
  source            text not null references sources(name),
  run_ts            timestamptz not null,
  started_at        timestamptz not null,
  finished_at       timestamptz,
  status            text not null check (status in ('ok','failed','partial','skipped','zero_rows')),
  rows_discovered   int not null default 0,
  rows_snapshotted  int not null default 0,
  rows_gone         int not null default 0,
  seconds           numeric(10,2),
  requests          int,
  errors            jsonb not null default '[]'::jsonb,
  unique (source, run_ts)
);
create index if not exists runs_source_ts_idx on runs (source, run_ts desc);

-- bookkeeping for rollups / external signals
create table if not exists maintenance (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- Game releases: every game a platform has published, from its newest-first feed or sitemap (not just the
-- tracked top lists). Keyed by canonical game URL, which every platform exposes and which matches games.url.
-- Release date = published_at (the platform's own publish date) when known, otherwise first_seen, except for
-- rows from a platform's first sweep (baseline), whose first_seen is only "already existed when we started".
create table if not exists releases (
  source        text not null references sources(name),
  url           text not null,
  published_at  timestamptz,
  first_seen    timestamptz not null,
  baseline      boolean not null default false,
  date_checked  boolean not null default false,  -- publish-date lookup attempted (backfill does each game once)
  primary key (source, url)
);
create index if not exists releases_source_pub_idx on releases (source, published_at);
create index if not exists releases_undated_idx on releases (source) where published_at is null and not date_checked;

-- Catalogue size per platform per day (sitemap / API totals). Its day-over-day change is the release count
-- for platforms that publish a total but no per-game list (Playgama AI), and a cross-check for the others.
create table if not exists catalog_daily (
  source       text not null references sources(name),
  day          date not null,
  games_total  int not null,
  primary key (source, day)
);

-- Storage changes (Nov 10, 2026; see docs/STORAGE.md):
-- A reading identical to the game's last stored reading from the same UTC day is not inserted; the stored row
-- counts it in `repeats` instead, so polls per day and the CCU average are unchanged (each row weighs 1 + repeats).
alter table snapshots add column if not exists repeats int not null default 0;
alter table snapshots add column if not exists last_seen_at timestamptz;  -- last poll that confirmed this reading
-- When the game last appeared in one of the platform's lists (discovery only; last_seen also moves on watchlist
-- polls). The watchlist uses this instead of list_placements, which now keeps positions 1-100 only.
alter table games add column if not exists last_listed timestamptz;
create index if not exists games_source_listed_idx on games (source, last_listed);
