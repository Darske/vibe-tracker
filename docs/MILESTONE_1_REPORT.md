# Milestone 1 report — Research and collect (Oct 29, 2026)

## What works

- Research for all eight platforms → `docs/SOURCES.md` (list pages, endpoints with real field names, counters, metric_map, robots, blocks, open questions).
- Repo scaffold exactly per the brief: models, `SourceAdapter`, `http.py` with the politeness rules, `registry`, `db` (init/upserts/derive/rollup), `pipeline` CLI (`run --source --dry-run --limit --force`, `record`), `metrics.py` with every definition from section 4, idempotent `sql/schema.sql`.
- Adapters: **Tesana, Rosebud, Bitmagic** with recorded fixtures and parse tests.
- Tests: 54 passing — contract, parse (3 adapters), metrics (pure Python), derive (full SQL spec against a real Postgres 16: deltas, gap split, reset flag, CCU, rolling 7d, noise floor, acceleration, ranks, creator_daily, platform_daily, rollup, idempotency), plus a `live` smoke test.
- Workflows: `scrape.yml` (cron `23 */2 * * *`, workflow_dispatch with source/force/limit, 30-min timeout, concurrency group, Playwright installed only if a browser source is enabled, log artifact on failure), `ci.yml` (tests with a Postgres service; weekly live smoke), `keepalive.yml` (weekly heartbeat commit).
- End-to-end verified locally: schema init → 3 full runs → derive → rollup, against a local Postgres. Re-running the same run or day changed nothing (idempotent); the freshness skip works.

**Not yet running on the schedule** — blocked on the setup checklist (below). No dashboard URL or Health screenshot yet; the Health page is milestone 2.

## Per platform (milestone-1 adapters)

| Platform | Metrics available (what they mean) | Rows per run | Seconds / requests per run |
|---|---|---|---|
| Tesana | plays_total = `play_count` (sessions); players_total = `players_7d` (unique, trailing 7 d); players_now = `playing_now` (CCU); likes = favorites; remixes, comments, votes from store records | 147 discovered across 6 lists (461 placements); watchlist up to 400 | ~10 s, 11 requests for 30 games (lists carry counters; +1 request per 50 games for CCU, +1 per watchlist-only game) |
| Rosebud | plays_total = `plays_count` (page loads, not unique); likes; remixes = `clones_count` (incl. unpublished); comments. No players, no CCU, no dates | 200 discovered (top 100 by plays + 100 newest) | ~2 s, 2 requests for 30 games — the list endpoint is the batch endpoint; watchlist-only games cost 1 request each |
| Bitmagic | plays_total = `playCount` (Play presses, session-deduped); players_now = live room counts (0 when absent from the live response — documented); likes; comments; publishedAt | 124 discovered (3 lists; whole catalog is 150 games) | ~11 s, 12 requests for 30 games (batch of 25; API accepts up to 100) |

Full-watchlist estimate: Tesana ≈ 400 games ≈ 2–4 min (store fetch for carry-overs), Rosebud ≈ 2–6 s, Bitmagic ≈ 15 s. Three platforms ≈ 3–5 min per run worst case; all eight should still fit the 20-minute budget.

### Expected vs actual (section 2 table)

- Tesana: expected plays, players, likes, creator, price — **all present**, plus CCU, unique players (7 d), remixes, verdict votes. Best data of the eight.
- Rosebud: expected plays, likes, remixes, creator, published date — all but **published date** (only in a 2 MB detail call) and creator profile links (**`/profile/` is robots-disallowed**).
- Bitmagic: expected players online, plays, likes — **all present**; listings are not thin (150 games public), only creation is waitlist-gated.
- Astrocade: expected plays, likes, remixes, creator — all present via an **app-internal API** (policy question below).
- Spawn: expected players online, plays, likes, creator — all present in **HTML** (its `/api` is robots-disallowed).
- Remix: expected plays, likes, creator, published date — all present in HTML, plus **unique players**.
- Wavedash: expected plays/players per game — **not public**; only concurrent players for the top ~12 trending games. Placements + CCU only.
- Playgama: expected plays, ratings, rank — **plays not public** on playgama.com (only votes, weekly leaderboard players, placements). The vibe-coded catalog with per-game plays is on **playgama.ai/play**.

## Decisions I made that you did not specify

1. Followed robots.txt even where a platform's `llms.txt` invites API use (Spawn). HTML of allowed pages has the same numbers.
2. No adapter uses a browser; on Rosebud/Bitmagic/Wavedash a browser visit inflates the counter we measure, on Spawn it flags webdriver. `needs_browser` stays available but unused.
3. Separate API hosts with no robots.txt (api.tesana.ai, Rosebud's gateway, Bitmagic same-origin) are treated as allowed, 1 r/s, project UA.
4. Tesana's `players_7d` is stored in `players_total` and labelled "unique players, trailing 7 days" in metric_map (the schema has no 7-day-players column; this keeps a players figure out of any plays column).
5. Bitmagic `players_now = 0` for a game absent from the live response, because that endpoint omits zeros — the one deliberate exception to "missing → NULL", documented in the adapter.
6. Rank tiebreak: `plays_7d`, then `plays_delta`, then `plays_total_eod` (so day-1 ranks are by cumulative plays rather than arbitrary).
7. Derive recomputes today and yesterday from a 40-day lookback so gap-splitting works after outages; `creators_active` = creators with >0 plays_delta that day.
8. Creator profiles are refreshed on the first run after 00:00 UTC (`run_ts.hour < poll_every_hours`), as specified; `mark_stale` runs then too.
9. CI runs the derive tests against a Postgres service so the SQL half of metrics.py is covered on every push.
10. Research evidence (`research/`, ~2 MB of raw captures) is git-ignored; the trimmed fixtures in `tests/fixtures/` are what the repo keeps.

## Open questions (with my recommended answer)

1. **Public or private repo?** → Public. Code isn't secret, data is in Supabase, unlimited minutes keeps the 2-hour cadence safe.
2. **Astrocade's app-internal API** (`astrocade-api-prod.com/v3` with a self-minted `guest-<uuid>` header, no robots.txt, nothing behind login). Use it? → Yes, with a fixed guest id and our UA; fall back to curl of the allowed `/games/<slug>/<id>` HTML (same object) if it ever rejects us. Your call — it's the greyest of the eight.
3. **Add `playgama.ai/play` as a ninth source?** It's Playgama's actual vibe-coded catalog with per-game 24 h/all-time plays, likes, avg playtime and a by-AI-tool breakdown (claude-code 51 % of games). playgama.com itself is mostly conventional HTML5 games with no play counts. → Yes, as a separate source `playgama_ai`, segment aggregator; keep `playgama` for placements.
4. **Spawn: use `/api/feed` or not?** robots says no, llms.txt says yes. → No; HTML is enough for the lists; full-catalog coverage would need it.
5. **Rosebud creator links** are only on robots-blocked `/profile/` pages. → Accept no links from Rosebud; the outreach note field covers it.
6. **Bitmagic batch size** 25 vs measured max 100. → Raise to 50 in milestone 2 (halves requests).
7. **Proxy for a blocked platform (~$10–30/mo)?** Not needed: no platform blocked us.

## Cost

- Expected monthly cost: **$0**. Nothing paid was added.
- GitHub Actions minutes used this month: **0** (not deployed yet). Projection at full breadth: 3–5 min/run × 12 runs/day ≈ 40–60 min/day ≈ 1,200–1,800 min/month — under the 2,000-minute private allowance but close; public repo removes the ceiling.
- Supabase: ~30 k snapshot rows/day at full breadth, rolled up after 30 days → well under 500 MB in year 1.

## To start collecting (your part, ~20 min)

1. Create the GitHub repo `vibe-tracker` and push the contents of `vibe-tracker.zip` (git history included: 2 commits).
2. Supabase project → Project Settings → Database → **session pooler** connection string (port 5432).
3. Repo secrets: `DATABASE_URL`, `CONTACT_EMAIL`.
4. Actions tab → run `scrape` once with workflow_dispatch; `ci` runs on the push. Then tell me and I'll check the `runs` table and start milestone 2.
