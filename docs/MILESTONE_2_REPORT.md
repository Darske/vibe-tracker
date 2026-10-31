# Milestone 2 report — Breadth + Health page (Oct 31, 2026)

Decisions received Oct 31: public repo ✅ · Astrocade API approved ✅ · playgama.ai added as a ninth source ✅.

## What works

- **Nine adapters**, all JSON or server-rendered HTML, no browser anywhere: Tesana, Rosebud, Bitmagic (M1) + Astrocade, Spawn, Remix, Wavedash, Playgama, Playgama AI (M2). Each has recorded fixtures and parse tests; 175 tests green, including the SQL derive tests against Postgres.
- **Full nine-source run verified** end-to-end against a local Postgres 16: 2,414 games, 1,425 creators, 2,295 `game_daily` rows derived, `platform_daily` populated for all nine, rollup and idempotency intact. Concurrent players are already comparable across platforms at one `run_ts` (e.g. Wavedash 139 · Spawn 110 · Tesana 10 · Bitmagic 1).
- **Health page** (`dashboard/app.py` → Health): headline tiles (days of history, games, snapshot rows, DB size, Actions minutes), 7-day average run time with the 6-minute warning, last run per platform with ✅/⚠️/🔴 states (failed or zero-row run in the last 6 h → red; rows dropped >50 % vs the 7-day median → warning), rows-per-run and seconds-per-run charts, metric_map labels. Phone layout shows 6 columns with an "All columns" toggle. Screenshots: `docs/health_desktop.png`, `docs/health_mobile.png`. The page caught a real failure on its first render (Tesana 410, below).
- Not yet live: the dashboard URL and the scheduled runs still need the repo + secrets (the setup checklist). Nothing in milestone 2 depended on them.

## Per platform (first full run, Oct 31, 1 req/s)

| Platform | Metrics available (meaning) | Rows / run | Seconds · requests | Blocked / notes |
|---|---|---|---|---|
| Tesana | plays (sessions), players 7 d (unique), CCU, likes, remixes, votes, comments | 147 discovered / 174 snapshotted | 43 s · 43 | a delisted game returned **410 Gone** and killed the fetch in the first run → 410 now = gone (fixed, re-run ok) |
| Rosebud | plays (page loads), likes, remixes, comments | 200 / 302 | 161 s · 105 | watchlist carry-overs cost 1 request each (play-detail); lists themselves are 2 requests |
| Bitmagic | plays (Play presses), CCU, likes, comments | 122 / 124 (+1 gone) | 18 s · 18 | — |
| Astrocade | plays (session starts), likes, remixes, comments, creator followers | 789 / 785 (+4 gone) | 11 s · 10 | guest-id API, one request per list; richest/cheapest of the new five |
| Spawn | plays (sessions), CCU (multiplayer list), likes, comments, votes | 95 / 95 | 10 s · 10 | lists are first-page only (~24 cards; ~60 multiplayer) — HTML paging is ignored, real paging is the disallowed `/api` |
| Wavedash | CCU for the trending set (4 games today), comments, placements; **no plays** | 49 / 49 | 102 s · 102 | 2 Convex calls per game; no `new` list (feed has no dates) |
| Remix | plays (sessions), **unique players**, likes, comments | 492 / 492 | 185 s · 185 | one `/g/` page per game for ~170 shelf/chip-only games; ISR cache can lag up to ~3 h |
| Playgama | votes (= likes), 7-day leaderboard players, placements; **no plays** | 243 / 243 | 251 s · 251 | one game page per game (votes are not in the list tuples after all) |
| Playgama AI | plays all-time (exact), **plays 24 h (platform's own)**, likes, **AI tool per game** | 145 / 145 | 146 s · 146 | one `/play/<id>` page per game for the exact count (cards round above 1k); no creators shown |

**Total: ~15.4 minutes per full run, ~870 requests.** Inside the time budget today; steady-state watchlists (Remix 400, Playgama 250, Rosebud carry-overs) will push it toward 20 minutes, so I raised `RUN_TIME_BUDGET_SECONDS` to 1500 (job timeout stays 30 min). If the budget is hit, the last platforms in `sources.yaml` order are skipped and the run is marked partial — the Health page shows it.

## Decisions I made that you did not specify

1. **Time budget 20 → 25 min** (see above). Alternative levers, your call: `rate_limit_rps: 2` for the three HTML sites (still polite; none showed rate limiting), or drop Playgama's `new` list (100 of its 243 page fetches).
2. **Spawn `players_now = 0`** for a game on the multiplayer list without a `playing` key (that list is sorted by CCU and omits zeros); `None` on other lists.
3. **Remix tracks two lists per category**: the 48-game grid (trending/staff-pick order, with counters) and the "most played first" chip list (true most-played order, no counters). The grid is *not* most-played as SOURCES.md first said.
4. **Wavedash has no `new` list**; an optional `catalog` list (53 calls) exists in the yaml comment for first-seen dates.
5. **Astrocade `ai_tool = "astrocade"`** on every game (all made with its builder); Playgama AI sets `ai_tool` from the card's agent badge (claude-code, codex, cursor, …). Other platforms leave it NULL.
6. **Tesana `players_7d` → `players_total`** column, labelled "unique players, trailing 7 days" in metric_map (no 7-day-players column in the schema; keeps a players figure out of the plays column).
7. **HTTP 410 = gone** everywhere (http.py + the four HTML adapters).
8. Dashboard colours are assigned to platforms in a fixed order from the `sources` table and never re-cycled when filters hide a platform (dataviz rule); numbers are abbreviated (12.4k, 3.5M).
9. `dashboard/requirements.txt` pins the Streamlit deps separately so Streamlit Cloud installs only what the app needs.

## Open questions (with my recommended answer)

1. Run length ~15–20 min at 1 req/s. Keep 1 req/s and the 25-min budget (recommended — public repo makes minutes free), or allow 2 req/s on Playgama/Remix/Playgama AI to halve it?
2. Spawn coverage is first-page only (~95 games). Enough for v1? The alternative is one game page per sitemap entry (3.8k games ≈ 65 min) — not recommended.
3. Rosebud: the top-100-by-plays list barely moves; should `top` sweep deeper (e.g. top 300, 3 requests) so the watchlist carries more of the long tail? Recommended: yes, `top_n: 300` for Rosebud only.
4. Wavedash: enable the `catalog` list (53 calls/run) to get first-seen dates for all 1,270 games → "new games/day" for Wavedash? Recommended: yes, but at `poll_every_hours: 6`-style cadence — needs a per-list cadence feature; defer to milestone 4.

## Cost

- Expected monthly cost: **$0**. Nothing paid added.
- GitHub Actions minutes this month: **0** (not deployed). Projection: ~15 min/run × 12 = ~3 h/day ≈ 5,500 min/month — fine on a public repo (unlimited), would not fit a private one.
- Supabase: the first full run wrote 2,414 snapshot rows; ~29k rows/day at full resolution, rolled up after 30 days → ~100 MB/year.

## Next

Milestone 3 (dashboard pages 1–5 with the vibez.club-style creator page) can start now; it needs no new data. But the clock on history only starts when the poller runs, so the highest-value 20 minutes are still the setup checklist: public repo `vibe-tracker` ← push the zip (git history included), Supabase session-pooler `DATABASE_URL` + `CONTACT_EMAIL` as repo secrets, run `scrape` once from the Actions tab, then deploy `dashboard/app.py` on Streamlit Community Cloud (private, `DATABASE_URL` in app secrets).
