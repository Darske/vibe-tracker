# Storage (Supabase free tier: 500 MB)

Measured Nov 10, 2026 (`measure` workflow, read-only): 132.6 MB used, growing ~11.8 MB/day — limit in ~31 days.
Growth: snapshots 4.8 MB/day, list_placements 4.8 MB/day, game_daily 2.3 MB/day.

## Changes shipped Nov 10

1. **List positions kept to the top 100.** Tesana's `catalog` list (whole marketplace, ~3,700 positions per run)
   and Rosebud's `top` (1,000) exist to find games to poll; a position there is just the plays rank. They were
   ~45% of `list_placements` rows. New rows beyond position 100 are not stored (`pipeline.PLACEMENT_MAX_POSITION`)
   and a one-off migration (`db.MIGRATIONS`, `placements_top100_v1`) deletes the old ones.
   The watchlist (games polled after leaving a list) used to require a recent placement row, so it now uses
   `games.last_listed`, set only when discovery sees the game in a list and backfilled from placements before the
   delete. Verified: the watchlist is unchanged for every platform.
2. **Identical readings folded.** 55% of full-scrape and 71% of players-online readings repeated the previous one
   exactly. A reading equal to the game's last stored reading *from the same UTC day* is not inserted; that row's
   `repeats` is incremented (and `last_seen_at` set). Each row weighs `1 + repeats` in the derive step, so
   `captures` (polls per day), CCU peak and CCU average are exactly what they were (tests/test_storage.py).
   The first reading of each day is always stored, so end-of-day totals, gaps and the 30-day rollup are unchanged.
   `runs.rows_snapshotted` still counts games fetched, so the Health page is unaffected.
3. **Unused index dropped:** `list_placements_run_idx` (no query used it).

Expected: ~11.8 → ~6 MB/day; from ~Nov 30 `db.rollup` also thins snapshots older than 30 days to one per game per day.

## Not done (options if needed later)

- Daily (instead of 2-hourly) polling of games with no counter change for 7 days (Tesana 33%, Rosebud 41% dormant).
- 14-day retention of sub-daily readings, only with end-of-day rows kept forever and the rest archived to
  Supabase Storage (separate free 1 GB) so nothing is lost.
