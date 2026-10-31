
# Vibe-Tracker — Cowork build prompt
2026-10-29 · @Giorgio

## How to use this doc
Do the setup checklist (section 12) first, then paste sections 1–11 into a new Cowork chat as the opening message, or share this doc's link and say "build what's in this doc". Section 11 is a separate prompt you reuse later each time you want a new site tracked.
Everything below section 0 is written to the builder, in the second person.

## 1. Mission
Build **vibe-tracker**: a personal system that polls the public usage counters of AI-made ("vibe-coded") browser games on the eight platforms in section 2, every 2 hours, and shows the results by platform, by creator, by game and over time. It follows the top 100 games on each platform.
Two references for the output:

- https://vibez.club — open any creator page from the home page. That page is the model for every creator, game and platform page here: a big headline number, a "+35M (+9.0%) since Oct 5" delta with a line chart from a chosen date to today, a share-by-platform donut, then a grid of the items with per-item stats. I want exactly that, for plays and for players.

- https://howmanyareplaying.com — the feel of a cross-platform leaderboard. We do not scrape it; every number in this system comes from the platform itself.
Why it exists: this space is about to grow fast. I want to know which platform is winning (top platform by daily plays, top platform by concurrent players), which games are top on each platform, and which creators are breaking out so I can reach out to them and potentially fund them — from trends I measured myself.
Key design ideas:

- Platforms display cumulative totals; we store a snapshot every poll and compute **daily deltas** (plays gained per day). Velocity and acceleration are the primary metrics everywhere.

- Concurrent players is a point-in-time number, so it is captured every poll and summarised per day as peak and average. That is the reason for the 2-hour cadence.

- History only exists from the day the poller first runs, so the poller ships before the dashboard.
Hard constraints:

- Running cost target: $0/month on free tiers. Tell me before anything that would cost money. If 2-hour polling does not fit the free tiers, fall back to every 3 hours, then to daily, in that order, and tell me.

- Private use only. Nothing is published; the dashboard sits behind Streamlit's viewer login.

- Adding a new platform later must be a one-file job (section 3) and must not require touching the dashboard.

- Must work on a phone: I will read the dashboard on mobile most of the time.
Non-goals: no logins, no bypassing bot protection or CAPTCHAs, no paid data APIs, no scraping of private pages, no aggregator sites as sources, no one-off scripts outside the pipeline.

## 2. Sources for v1
Eight platforms in two segments. Follow the top 100 games on each; if a platform's top list is shorter, take what it has. Domains below were verified on Oct 29, 2026; the "expected metrics" column is what each platform advertises, not what I have confirmed — the research step decides.
**Segment A — AI UGC aggregators** (portals that distribute games built elsewhere)
Source
URL
What it is
Expected public metrics
Wavedash
https://wavedash.com
Browser-first PC game platform (YC; $4.3M seed led by Floodgate). Store pages at /games/<slug>, multiplayer lobbies, leaderboards, achievements
plays or players per game, players online, ratings
Playgama
https://playgama.com
HTML5 game portal plus the Bridge SDK distribution network; claims 300M+ monthly players across partner portals
plays per game, ratings, category rank
**Segment B — AI UGC creation + distribution** (make the game on the platform and publish there)
Source
URL
What it is
Expected public metrics
Spawn
https://www.spawn.co
Multiplayer games made by talking to its AI builder "Savi"; every game is multiplayer; has "new and rising" lists and an agent API under /api/agent/v1
players online, plays, likes, creator
Tesana
https://tesana.com
AI game engine with a marketplace and store; claims 200,000+ games built; game pages show plays; hosted multiplayer for 2–4 players
plays, players, likes, creator, price
Rosebud AI
https://rosebud.ai
Largest prompt-to-game host (millions of community games); creator profiles, remixing
plays, likes, remixes, creator, published date
Bitmagic
https://bitmagic.ai
Prompt-generated multiplayer 3D worlds in the browser; access has been waitlist-gated, so public listings may be thin
players online, plays, likes
Astrocade
https://astrocade.com
"TikTok for games"; claims ~5M monthly users and ~140M plays a month across 75,000+ games; one-tap remix
plays, likes, remixes, creator
Remix
https://remix.gg
AI game maker with web, desktop and iOS/Android apps; every game gets a remix.gg page; leaderboards; ~1.5M players, ~100,000 games
plays, likes, creator, published date
**Research step (do this for all eight before writing any adapter):**

- Open the platform in a browser. Find the list pages worth sweeping: top, trending, most played, new, by tool.

- Open DevTools → Network. Find the JSON endpoints behind those pages (/api/..., _next/data/..., GraphQL, Supabase or Firebase REST). Prefer these over HTML parsing every time; record the exact URL pattern, pagination parameter and any required headers.

- Open one game page and one creator profile. Record which counters are public (plays, players, players online, likes, remixes, votes, comments, followers), whether the creator's external links (X, YouTube, TikTok, Discord, website) are visible, and whether the page needs JavaScript to render.

- Read /robots.txt. If the paths you need are disallowed, do not scrape them — record it and move on.

- Record what each platform's "play" and "player" numbers mean (a session start, a unique user, players in the room right now) as a metric_map; the dashboard labels every figure with it.

- Write the findings into docs/SOURCES.md, one row per platform: segment, list pages, endpoint pattern, metrics available, metric_map, JS required, rate limit observed, robots status, open questions.
Source-specific notes:

- **Spawn** and **Bitmagic** are multiplayer-first: players online is the number to find. Spawn has an agent API; check for read endpoints before parsing HTML.

- **Rosebud**: capture remixes (a creator-influence signal) and creator profile links. If a batch or list endpoint returns counters for many games at once, use it for the 2-hourly re-poll.

- **Astrocade** and **Remix** also have mobile apps; only their web pages are in scope.

- **Playgama** lists games that also live on other portals; we count only what Playgama itself shows.

- **Wavedash** announced developer analytics in Oct 2026; check whether any player numbers are public on store pages.

- If a platform exposes no usage counter at all, still record list placements (rank per list per poll); that is a signal on its own.

## 3. Architecture and repo layout
Three parts, all free: a Python poller on a GitHub Actions cron, a Supabase Postgres database, and a Streamlit dashboard on Streamlit Community Cloud. The poller writes; the dashboard only reads (plus two small tables it writes: notes and creator merges).

```
vibe-tracker/
  README.md                 # setup, how to run locally, how to add a platform
  pyproject.toml            # Python 3.11+, uv or pip; pinned deps
  sources.yaml              # registry of platforms (below)
  sql/schema.sql            # idempotent DDL, run by `python -m tracker.db init`
  tracker/
    models.py               # pydantic: GameRef, GameSnapshot, ListPlacement, CreatorProfile
    base.py                 # SourceAdapter ABC
    registry.py             # loads sources.yaml, imports adapters by module path
    http.py                 # shared httpx client: per-source rate limit, retries w/ backoff, UA, ETag cache
    browser.py              # Playwright helper, used only by adapters flagged needs_browser
    db.py                   # connection, upserts, init, rollup
    pipeline.py             # CLI: run [--source X] [--dry-run] [--limit N] [--force]
    metrics.py              # daily deltas, CCU peak/avg, ranks, growth, acceleration, aggregates
    external.py             # Cloudflare Radar / Tranco / Google Trends signals (optional)
    sources/
      wavedash.py
      playgama.py
      spawn.py
      tesana.py
      rosebud.py
      bitmagic.py
      astrocade.py
      remix.py
  tests/
    fixtures/<source>/      # recorded JSON/HTML responses
    test_adapters.py        # contract + parse tests per adapter
    test_metrics.py         # delta, reset, gap, CCU logic
  dashboard/
    app.py                  # Streamlit entry; pages in dashboard/pages/
    components.py           # metric_card, share_donut, game_grid (see section 7)
    queries.py              # all SQL in one place, cached
  docs/
    SOURCES.md              # research findings per platform
    ADDING_A_SOURCE.md      # the playbook in section 11
  .github/workflows/scrape.yml
```
**Adapter interface** (tracker/base.py). Every platform implements exactly this; nothing else in the system knows platform specifics.

```python
class SourceAdapter(ABC):
    name: str            # slug, e.g. "rosebud"
    display_name: str
    base_url: str
    segment: str         # "aggregator" | "creation"
    needs_browser: bool = False

    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        """Walk each list page (top, trending, most_played, new, ...) up to top_n games
        per list and yield the games seen with their position on that list."""

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        """Return current counters for the given games. Batch where the platform allows it.
        Missing games (404, private) are returned with status='gone'."""

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        """Optional. Profile name, URL, followers, external links (x, youtube, tiktok, discord, website)."""
```
Models (tracker/models.py):

- GameRef: source, external_id, url

- GameSnapshot: source, external_id, url, title, thumbnail_url, creator_external_id, creator_name, creator_url, plays_total, plays_24h, players_now, players_total, likes, remixes, comments, votes, followers, ai_tool, engine, tags (list), published_at, status (ok | gone), captured_at. Every counter is int | None — never coerce a missing value to 0.

- ListPlacement: source, list_name, position, external_id, captured_at

- CreatorProfile: source, external_id, name, url, avatar_url, followers, bio, links (dict)
**sources.yaml** — one entry per platform; the pipeline iterates over enabled entries only:

```yaml
rosebud:
  module: tracker.sources.rosebud
  display_name: Rosebud AI
  segment: creation            # aggregator | creation
  enabled: true
  lists: [top, trending, new]
  top_n: 100                   # games followed per list
  poll_every_hours: 2          # 6 for needs_browser sources
  rate_limit_rps: 1
  needs_browser: false
  stale_after_days: 30
  max_watchlist: 400
  metric_map:
    plays: "plays = game sessions started (from docs/SOURCES.md)"
    players: "players_now = users in a live session"
```
Adding a platform later means: one adapter file, one yaml entry, one fixture folder. The dashboard reads sources (including segment and metric_map) from the database and needs no change.

## 4. Data model and metric definitions
Postgres on Supabase. Raw snapshots are append-only; everything derived is recomputed idempotently after each run.
Table
Grain
Key columns
sources
one row per platform
name (pk), display_name, base_url, segment, metric_map jsonb, poll_every_hours, enabled, added_at
creators
one row per creator per platform
id, source, external_id, name, url, avatar_url, links jsonb, followers, canonical_key, first_seen, last_seen; unique (source, external_id)
creator_merges
manual cross-platform identity
canonical_key, creator_id, note — edited via dashboard or CSV; default canonical_key = normalized handle
games
one row per game per platform
id, source, external_id, url, title, thumbnail_url, creator_id, ai_tool, engine, tags jsonb, published_at, first_seen, last_seen, status (live, gone, stale); unique (source, external_id)
snapshots
one row per game per poll
game_id, run_ts, captured_at, plays_total, plays_24h, players_now, players_total, likes, remixes, comments, votes; index (game_id, captured_at). No raw HTML or JSON stored here.
list_placements
one row per game per list per poll
source, list_name, position, game_id, run_ts
game_daily
one row per game per day (derived)
game_id, day, captures, plays_delta, likes_delta, remixes_delta, votes_delta, plays_total_eod, ccu_peak, ccu_avg, gap_days, reset_flag, rank_in_source, plays_7d, plays_prev_7d, growth_7d, acceleration
creator_daily
one row per creator per day (derived)
creator_id, day, plays_delta, ccu_peak, games_live, games_new, hit_games, share_of_source, plays_7d, growth_7d, acceleration
platform_daily
one row per platform per day (derived)
source, day, plays_delta_top100, ccu_peak_top100, ccu_avg_top100, games_tracked, games_new, creators_active, top10_share, cloudflare_rank_bucket, tranco_rank, trends_index
notes
one row per canonical creator
canonical_key, status (new, watching, contacted, replied, passed, funded), note, updated_at — written by the dashboard
runs
one row per platform per run
source, run_ts, started_at, finished_at, status, rows_discovered, rows_snapshotted, rows_gone, seconds, errors jsonb
**Metric definitions (implement exactly these; put them in ****metrics.py**** with docstrings):**

- run_ts is one timestamp per pipeline run, shared by every platform polled in that run, so numbers at one run_ts are comparable across platforms.

- plays_delta for day D = last plays_total captured on D minus last plays_total captured on the previous capture day. Several polls in a day collapse to one daily delta. First-ever capture → NULL, not 0. If the previous capture is k > 1 days old, write delta ÷ k to each intervening day and set gap_days = k.

- A negative delta means the platform reset or corrected its counter: write NULL and set reset_flag = true. Never write negative plays.

- plays_24h from a platform is stored as-is and shown as the platform's own figure; our measured plays_delta is the primary number everywhere.

- ccu_peak / ccu_avg for a game on day D = max / mean of players_now over that day's polls (NULL if the platform exposes no players-online number). The raw per-poll series is kept for 30 days for the game page.

- platform_daily.plays_delta_top100 = sum of plays_delta over the games in that platform's top 100 that day. ccu_peak_top100 = the maximum, across the day's runs, of the sum of players_now over the top 100 at that run_ts; ccu_avg_top100 = the mean of those per-run sums. Label all three as "tracked top 100" on the dashboard.

- The two cross-platform measures are **daily plays** (plays_delta_top100) and **concurrent players** (ccu_peak_top100, ccu_avg_top100). A platform's metric_map says what its play and player counters actually mean; the dashboard shows that label next to every platform figure and never mixes a "players" counter into a "plays" column.

- plays_7d = sum of plays_delta over the trailing 7 days; plays_prev_7d = the 7 days before that; growth_7d = plays_7d ÷ plays_prev_7d − 1, NULL when plays_prev_7d < 100 (noise floor; make it a config value).

- acceleration = growth_7d − growth_7d one week earlier.

- rank_in_source = rank of the game within its platform by plays_7d, computed daily. Cross-platform game comparisons use rank and growth, never raw plays.

- Creator hit_games = games with plays_7d ≥ the platform's 90th percentile that day. share_of_source = creator plays_7d ÷ platform plays_7d (tracked set).

- top10_share = plays_7d of the top 10 games ÷ plays_7d of all tracked games in that platform.

- A game not seen in any list and not fetchable for stale_after_days → status stale; a 404/private → gone. Both keep their history.

## 5. Pipeline behavior
One command, python -m tracker.pipeline run, executes every 2 hours. It stamps one run_ts, then handles each enabled platform in order, each wrapped in its own try/except so one broken platform never blocks the others. A platform whose last successful run is more recent than its poll_every_hours is skipped (so Playwright platforms can poll every 6 hours while the rest poll every 2); --force overrides this.

- **Discover.** Walk every list in sources.yaml up to top_n games per list. Upsert games and creators (set first_seen on insert, last_seen always). Insert a list_placements row per game per list for this run_ts.

- **Build the watchlist.** Every game that appeared in any of that platform's lists in the last stale_after_days (so a game that drops out of the top 100 is still followed for a month), capped at max_watchlist, prioritising most recent last_seen then highest plays_7d. Union with this run's discovered games, dedupe.

- **Fetch.** Call adapter.fetch() in batches. Insert one snapshots row per game with this run_ts. Mark gone on 404/private. Fetch creator profiles only on the first run after 00:00 UTC, and only for creators first seen that day or not refreshed in 14 days.

- **Derive.** Recompute game_daily for today and yesterday (late captures), then creator_daily and platform_daily. All upserts are keyed on (id, day), so re-running is safe.

- **External signals** (external.py, once a day, off until the token exists): Cloudflare Radar domain rank bucket for each platform's domain (free API token), the Tranco list rank (free daily download), and Google Trends interest via pytrends weekly (best-effort; skip on failure). Write to platform_daily.

- **Rollup** (first run after 00:00 UTC): snapshots older than 30 days are reduced to one row per game per day (the day's last capture); game_daily already holds that day's CCU peak and average, so nothing is lost that the dashboard shows.

- **Log a ****runs**** row** per platform with counts, seconds and any errors. Exit non-zero at the end if any platform failed or returned zero rows, so GitHub emails me.
Politeness and robustness rules, enforced in http.py so adapters cannot skip them:

- Default 1 request/second per platform, configurable; exponential backoff on 429/5xx with a maximum of 3 retries; honour Retry-After.

- A fixed User-Agent that identifies the project and includes a contact email I will provide.

- Respect robots.txt at request time (urllib.robotparser, cached per run); a disallowed path is logged and skipped.

- ETag / If-Modified-Since caching where the platform sends them.

- Hard time budget: 20 minutes per run (configurable). If the budget is hit, finish the current platform, skip the rest, and mark the run partial.

- Playwright only for platforms flagged needs_browser; headless Chromium, no screenshots, block images/fonts; those platforms default to poll_every_hours: 6.

- --dry-run prints what would be written and writes nothing; --source X runs one platform; --limit N caps the watchlist for local testing.

## 6. Scheduling, secrets and cost guardrails
**GitHub Actions** (.github/workflows/scrape.yml): one cron at 23 */2 * * * UTC (12 runs a day; an off-minute avoids the congested top of the hour) plus workflow_dispatch with optional source and force inputs. Steps: checkout, set up Python 3.11 with dependency cache, install, install Playwright Chromium only if any enabled platform has needs_browser: true, run the pipeline, upload the log as an artifact on failure. Set timeout-minutes: 30 and a concurrency group so two runs never overlap.
Minutes budget: a private repo gets 2,000 free minutes a month. Eight JSON-only platforms at ~300 games each should take 4–6 minutes a run, so 12 runs a day is 50–70 minutes a day, or 1,500–2,100 a month — right at the limit. Two levers, and I will choose in section 12: make the repo public (unlimited minutes; the code is not secret and all data lives in Supabase), or poll every 3 hours (23 */3 * * *, 8 runs a day, ~1,400 minutes a month). The Health page shows minutes used this month and the 7-day average run time; if the average passes 6 minutes, warn me there.
Keep-alive: GitHub disables scheduled workflows after 60 days without a commit. Add a weekly job that commits a heartbeat file so the schedule never silently stops.
**Supabase** (free tier, 500 MB): connect from GitHub Actions and Streamlit using the **session pooler** connection string, not the direct one — GitHub runners have no IPv6 and the direct connection is IPv6-only on the free tier. Frequent writes keep the project from being paused for inactivity. Storage: ~8 platforms × ~300 games × 12 polls a day ≈ 30,000 snapshot rows a day ≈ 3 MB a day at full resolution, so the rollup in section 5 is not optional: after 30 days each day collapses to ~2,500 rows, roughly 90–100 MB a year.
**Streamlit Community Cloud** (free): deploy dashboard/app.py from the repo; set the app to private (viewer login) and put the read connection string in the app's secrets as DATABASE_URL. The app sleeps after inactivity and takes about 30 seconds to wake; that is acceptable.
Secrets (never in code, never in logs):
Secret
Where
Required
DATABASE_URL (pooler, read-write)
GitHub Actions repository secret
yes
DATABASE_URL (pooler, can be the same string)
Streamlit app secrets
yes
CONTACT_EMAIL (for the User-Agent)
GitHub Actions secret
yes
CLOUDFLARE_API_TOKEN (Radar read scope)
GitHub Actions secret
optional, enables external signals
Cost rules: if something would exceed a free tier or need a paid service (a scraping proxy, Supabase Pro, a paid API), stop and tell me the option and its monthly price. Do not add it on your own. If polling has to slow down to stay free, the order is: 2 hours → 3 hours → daily, and tell me each time. The expected bill is $0/month.

## 7. Dashboard (Streamlit)
The model is a vibez.club creator page (https://vibez.club → open any creator). Reproduce its structure, not its styling: a header with the name and handle buttons, a stats line ("444 videos · 3.5M followers · 424M views"), a row of three cards, then a grid of items with per-item stats. Build three reusable components in dashboard/components.py and use them on every page:

- metric_card(title, series, anchor_date, kind): headline value, then "+Δ (+x%) since <anchor date>" computed from the series value at the anchor, then a Plotly line chart from the anchor to today. kind is cumulative (plays, followers) or daily-peak (concurrent players). The anchor date is a sidebar date picker, default 7 days back, applied to every card on every page.

- share_donut(title, breakdown): the "Views by platform" card — a donut with a legend showing each slice's share and absolute value, for a selected window.

- game_grid(games): cards with thumbnail (if the platform provides one), title, platform badge, creator, published date, plays in window, players now; on a phone one card per row.
All pages read the derived tables via dashboard/queries.py with st.cache_data(ttl=600). Global sidebar filters: platforms (multi-select, default all), segment (A / B / both), anchor date, window for tables (24h / 7d / 30d). Numbers abbreviated (12.4k, 3.5M). Tables show at most 6 columns on a phone with an "all columns" toggle.

- **Platforms (home).** A ranked table of platforms with a metric selector: daily plays (top 100), peak concurrent players (top 100), average concurrent players, new games/day, active creators, 7-day growth. Each row shows its segment and the metric_map label (e.g. "plays = sessions started"). Below: two line charts over time — daily plays per platform and peak concurrent players per platform — with an indexed (=100 at anchor) toggle because raw counts are not comparable across platforms. Then a share_donut of tracked daily plays by platform, grouped by segment. Every chart carries a one-line note that figures cover the tracked top 100, not the whole platform.

- **Platform page** (tap a platform). Header: name, segment, link. Stats line: games tracked · creators · plays in window · peak players today. Cards: Plays (cumulative tracked, delta since anchor), Concurrent players (daily peak, with average as a second line), New games per day. Then the platform's top 100 as a sortable table: rank, rank change vs the prior window, title (link), creator (link), plays in window, growth_7d, players now, likes, remixes, first seen; plus a Movers toggle and a New entrants toggle (first seen within 7 days).

- **Games (cross-platform).** Top games across all platforms by plays in window or by peak concurrent players: rank, Δrank, title, creator, platform with its metric label, plays in window, growth_7d, players now, likes, remixes, first seen. Same toggles as the platform page. Tap a row → Game page.

- **Creators.** The page I will use most. Table of creators ranked by portfolio plays in window: creator (link), platform(s), games live, plays in window, share of platform, growth_7d, acceleration, hit games, first seen, outreach status, external links. A Rising toggle shows acceleration above a threshold with at least 2 games. Tap a creator → **Creator page**, the vibez.club layout: name; handle buttons for X, YouTube, TikTok, Discord, website where known; stats line "N games · X plays · Y followers"; three cards — Plays (cumulative, delta since anchor), Followers (if the platform exposes them, else Concurrent players), Plays by platform (share_donut, when the creator is on more than one platform; otherwise Top game share); then game_grid of their games. Below the grid: a status dropdown and a free-text note that write to notes, and a merge control that links this creator to a canonical_key (writes creator_merges).

- **Game page.** Header: title, platform badge, creator link, published date, link to play. Cards: Plays (cumulative, delta since anchor), Concurrent players (daily peak and average, plus the raw per-poll line for the last 7 days), Rank in platform over time. Below: placements history (which lists featured it, when, at what position).

- **Health.** Last run per platform with status, rows discovered and snapshotted, seconds, errors, days of history, estimated database size, GitHub Actions minutes used this month, and a chart of rows per run over time. A platform with a failed or zero-row run in the last 6 hours is highlighted red. This page is how I notice a broken adapter.
The dashboard must not contain any platform-specific code. It reads the platform list, segments and metric_map from the sources table so a newly added platform appears automatically.

## 8. Testing and health
Tests run with pytest and in a CI workflow on every push (separate from the scrape workflow, no database needed).

- **Contract test**: every adapter in sources.yaml loads, subclasses SourceAdapter, and declares name, display_name, base_url.

- **Parse tests**: each adapter has recorded fixtures in tests/fixtures/<source>/ (the real JSON or HTML saved during research). The test parses them and asserts at least 5 games with external_id, url, title and at least one counter populated, and at least one creator id. A fixture-recording helper (python -m tracker.pipeline record --source X) saves fresh fixtures so they can be refreshed when a site changes.

- **Metrics tests**: deltas across normal days, a 3-day gap (delta split evenly), a counter reset (NULL + flag), first capture (NULL), several polls in one day collapsing to one daily delta, CCU peak and average from those polls, and the growth noise floor. Use a throwaway SQLite or a local Postgres via testcontainers; keep the SQL portable enough for that, or test the pure-Python parts of metrics.py directly.

- **Live smoke test** (marked @pytest.mark.live, run only from workflow_dispatch or weekly): hits each enabled source once with --limit 5 and asserts at least one snapshot.
Health in production: the pipeline exits non-zero on any failed or zero-row source so GitHub Actions emails me; the Health page shows the same per source. A dropped row count of more than 50% versus the trailing 7-day median for a source is also flagged as a warning in the run log and on the Health page.

## 9. Milestones and what to report back
Work in this order and stop for my review at the end of each milestone. Data collection starts at milestone 1, so get there fast.

- **Research and collect (first session).** Research all eight platforms and fill docs/SOURCES.md — this is the most valuable hour of the project, because it tells us what is measurable where. Then: repo scaffold, schema, db init against Supabase, the pipeline with the politeness rules, adapters for the three platforms with the cleanest JSON endpoints, tests, and the 2-hourly workflow running green. Report: rows written per platform, seconds per run, and the metrics each platform actually exposes versus the "expected" column in section 2.

- **Breadth.** The remaining five adapters, full metrics.py with the derived tables, the rollup, and the Health page deployed on Streamlit so I can watch runs. Any platform that blocks or exposes nothing gets a one-line verdict in docs/SOURCES.md rather than hours of workarounds.

- **Dashboard.** The three components and pages 1–5, including the vibez.club-style creator page with notes and merges.

- **Finish.** External signals wired up if I have provided the Cloudflare token, keep-alive job, README, docs/ADDING_A_SOURCE.md.
At each stop, report in this format:

- What works (with the dashboard URL and a screenshot of the Health page)

- Per platform: metrics available and what they mean, rows per run, seconds per run, anything blocked and why

- Decisions you made that I did not specify

- Open questions, each with your recommended answer

- Current monthly cost (expected: $0) and GitHub Actions minutes used so far this month

## 10. Rules for the builder

- Research before code: no adapter is written until its docs/SOURCES.md row exists.

- Prefer a platform's JSON endpoints to HTML parsing; prefer HTML parsing to Playwright. Use Playwright only when the data does not exist without JavaScript, and give those platforms poll_every_hours: 6.

- Every number comes from the platform itself. Never add an aggregator or leaderboard site as a source.

- All platforms are polled in the same run under one run_ts, so concurrent-player totals are comparable across platforms at the same moment.

- If a platform returns 403s, a CAPTCHA, or a Cloudflare challenge, stop working on that platform, record it, and move on. Do not add proxies, rotate user agents, or spoof headers.

- Never scrape anything behind a login, and never use my accounts.

- Never store raw HTML or full JSON responses in the database; store the parsed counters only.

- Missing counters are NULL, never 0. Negative deltas are NULL plus a flag. A "players" counter never lands in a "plays" column. No silent coercion anywhere.

- Secrets come from environment variables only. If a secret is missing, fail with a clear message naming it.

- Keep every run idempotent: re-running the same run or day must not duplicate rows or change derived numbers.

- Pin dependencies. No dependency that pulls in a service with a paid tier by default.

- Ask me before: adding any paid service, changing the schema after milestone 2, changing the cron frequency, or polling a platform not in sources.yaml.

- Small, descriptive commits with a short message each. Push after every milestone at minimum.

- When you make a judgment call I did not specify, note it in the milestone report rather than silently choosing.

## 11. Prompt template: adding a new platform later
Keep this in docs/ADDING_A_SOURCE.md as well. Paste it into any future Cowork or Claude Code chat with the repo open, filling in the three placeholders.

```markdown
Add a new platform to vibe-tracker: {PLATFORM NAME} at {URL}, segment {aggregator | creation}.

Follow docs/ADDING_A_SOURCE.md and the existing adapters in tracker/sources/ as the pattern. Do not change anything outside the new adapter, sources.yaml, tests/fixtures/{slug}/, tests/test_adapters.py and docs/SOURCES.md.

1. Research first. Find the list pages (top, trending, most played, new, by tool), the JSON endpoints behind them (DevTools → Network), which counters a game page exposes (plays, players, players online, likes, remixes, votes, comments), whether creator profiles show external links, whether JavaScript is required, what robots.txt allows, and what the platform's play and player numbers actually mean. Add one row to docs/SOURCES.md before writing code.
2. Implement tracker/sources/{slug}.py as a SourceAdapter: discover() over the lists found with top_n 100, fetch() returning GameSnapshot rows with NULL for any counter the platform does not expose, fetch_creator() if profiles exist. Map players-online to players_now and the platform's own 24h figure to plays_24h if it has one. Use http.py for all requests; set needs_browser only if there is no alternative.
3. Add the sources.yaml entry with enabled: false, segment, lists, top_n: 100, poll_every_hours (2, or 6 if needs_browser), rate_limit_rps, stale_after_days, max_watchlist and metric_map.
4. Record fixtures with `python -m tracker.pipeline record --source {slug}` and add parse tests. Run pytest.
5. Run `python -m tracker.pipeline run --source {slug} --dry-run --limit 20`, then a real run with --limit 20, and check the rows in Supabase.
6. Flip enabled: true, commit, push, and trigger the scrape workflow once with workflow_dispatch. Confirm the Health page shows the new platform with a successful run and that it appears on the Platforms page without any dashboard change.

Report: metrics available and what they mean, rows discovered and snapshotted, seconds per run, robots.txt status, and anything the platform blocks. If the platform needs a login, shows a CAPTCHA, or disallows the paths in robots.txt, stop and tell me instead of working around it.
```

## 12. Setup checklist (do these before handing off)
The builder cannot create accounts for you. About 20 minutes total, all free tiers.

- Decide: public or private GitHub repo. Public = unlimited Actions minutes, so 2-hour polling is safe; private = 2,000 minutes a month, which 2-hour polling can exceed. The code is not secret and all data lives in Supabase, so public is my recommendation; if you want private, tell the builder to use the 3-hour cron.

- Create the repo named vibe-tracker (empty) and have it open in Cowork, or give the builder push access.

- Create a Supabase project (free tier). From Project Settings → Database, copy the **session pooler** connection string (the one that mentions pooler, port 5432). Keep it somewhere safe; this is the only secret that matters.

- Add that string as a repository secret named DATABASE_URL in GitHub (Settings → Secrets and variables → Actions). Add CONTACT_EMAIL with an email you are happy to have in a User-Agent string.

- Create a Streamlit Community Cloud account and connect it to the GitHub account that owns the repo. The builder will tell you when to click "deploy" and what to paste into the app's secrets (the same DATABASE_URL).

- Optional: create a Cloudflare account, make an API token with Radar read access, and add it as the CLOUDFLARE_API_TOKEN secret. This turns on the outside-in domain-rank signal; skip it for now if you want.

- Have an answer ready for the one question the prompt tells the builder to ask: if a platform blocks polling, are you willing to pay roughly $10–30 a month for a proxy service for that platform?
