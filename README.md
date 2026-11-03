# vibe-tracker

Polls the public usage counters of AI-made ("vibe-coded") browser games across platforms every two hours and shows the results by platform, creator, game and over time. Every number comes from the platform itself; no aggregator sites, no logins, no bot-protection bypass.

Three free parts: a Python poller on a GitHub Actions cron → Supabase Postgres → a Streamlit dashboard (milestone 3).

## Status

| Milestone | State |
|---|---|
| 1. Research + collect (Tesana, Rosebud, Bitmagic) | ✅ |
| 2. Breadth (Astrocade, Spawn, Remix, Wavedash, Playgama, Playgama AI) + Health page | ✅ code done, 175 tests green, full 9-source run verified against a local Postgres (~15 min); needs secrets + repo to run on the schedule |
| 3. Dashboard (Platforms, Platform, Games, Creators, Creator with notes + merges, Game) | ✅ code done; deployed at the Streamlit app once pushed |
| 4. External signals, keep-alive, docs | keep-alive done; rest ⏳ |

Research findings per platform: [docs/SOURCES.md](docs/SOURCES.md). The build brief: [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md).

## Run locally

```bash
pip install -e .[dev]
export DATABASE_URL='postgresql://...pooler.supabase.com:5432/postgres'   # Supabase *session pooler* string
export CONTACT_EMAIL='you@example.com'                                     # goes in the User-Agent

python -m tracker.db init                       # idempotent schema
python -m tracker.pipeline run --dry-run --limit 20   # writes nothing
python -m tracker.pipeline run --source tesana --limit 20
python -m tracker.pipeline run                  # what the cron runs
python -m tracker.pipeline record --source tesana     # refresh test fixtures
pytest                                          # unit + parse tests; set TEST_DATABASE_URL to also run the derive tests
pytest -m live --override-ini="addopts="        # hits each enabled platform once
```

Flags: `--source X` one platform · `--dry-run` print, write nothing · `--limit N` cap the watchlist · `--force` ignore the `poll_every_hours` freshness skip.

Environment: `DATABASE_URL`, `CONTACT_EMAIL` (required); `CLOUDFLARE_API_TOKEN` (optional, external signals); `RUN_TIME_BUDGET_SECONDS` (default 1200); `GROWTH_NOISE_FLOOR` (default 100); `LOG_LEVEL`.

## How it works

`python -m tracker.pipeline run` stamps one `run_ts`, then for each enabled platform in `sources.yaml` (each in its own try/except):

1. **Discover** — walk each list up to `top_n`, upsert games, insert `list_placements`.
2. **Watchlist** — everything seen in a list in the last `stale_after_days`, capped at `max_watchlist`, plus this run's discoveries.
3. **Fetch** — counters for the watchlist (batched where the platform allows), one `snapshots` row per game. 404/private → `gone`.
4. **Derive** — recompute `game_daily` for today and yesterday, then `creator_daily` and `platform_daily`. Idempotent.
5. **Rollup** (first run after 00:00 UTC) — snapshots older than 30 days collapse to one row per game per day.
6. A `runs` row per platform; exit non-zero if any platform failed or returned zero rows so GitHub emails you.

Politeness lives in `tracker/http.py` and adapters cannot skip it: 1 req/s per platform, backoff on 429/5xx (max 3 retries, honours `Retry-After`), project User-Agent with contact email, robots.txt checked at request time, ETag caching, hard time budget.

Metric definitions are in the docstring of `tracker/metrics.py` and tested in `tests/test_metrics.py` (pure Python) and `tests/test_derive_db.py` (SQL, against Postgres).

## Layout

```
sources.yaml          registry of platforms (enabled, lists, top_n, poll_every_hours, metric_map)
sql/schema.sql        idempotent DDL
tracker/
  models.py           GameRef, GameSnapshot, ListPlacement, CreatorProfile (every counter int | None)
  base.py             SourceAdapter ABC
  http.py             shared client: rate limit, retries, UA, robots, ETag, time budget
  registry.py         loads sources.yaml, imports adapters
  db.py               connect, init, upserts, derive, rollup
  pipeline.py         CLI
  metrics.py          daily deltas, CCU, growth, acceleration, ranks, aggregates
  sources/            one adapter per platform
tests/                contract, parse (recorded fixtures), metrics, derive (Postgres), live
dashboard/            Streamlit app (milestone 3)
docs/                 SOURCES.md, ADDING_A_SOURCE.md, BUILD_PROMPT.md
.github/workflows/    scrape (2-hourly), ci (tests), keepalive (weekly heartbeat commit)
```

## Deploy

1. Supabase: create a free project; copy the **session pooler** connection string (port 5432, host contains `pooler`). The direct connection is IPv6-only, and GitHub runners have no IPv6.
2. GitHub: repository secrets `DATABASE_URL`, `CONTACT_EMAIL` (and optionally `CLOUDFLARE_API_TOKEN`).
3. Push. `ci` runs the tests; trigger `scrape` once from the Actions tab (workflow_dispatch) and check the `runs` table.
4. Streamlit Community Cloud (milestone 3): deploy `dashboard/app.py`, set the app private, put `DATABASE_URL` in the app secrets.

Minutes: a full nine-source run measured ~15 minutes (the HTML platforms — Playgama, Remix, Playgama AI — need one page per game at 1 req/s). 12 runs/day ≈ 3 h/day ≈ 5,500 min/month, far over the 2,000-minute private allowance; the repo is public, so minutes are unlimited. `RUN_TIME_BUDGET_SECONDS` is 1500 with a 30-minute job timeout.

## Adding a platform

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md): one adapter file, one yaml entry, one fixture folder. The dashboard needs no change.
