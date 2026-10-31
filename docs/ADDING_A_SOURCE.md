# Adding a platform

Adding a platform is a one-file job plus a yaml entry and a fixture folder. The dashboard reads the platform list, segments and `metric_map` from the `sources` table, so it never changes.

Files you touch: `tracker/sources/<slug>.py`, `sources.yaml`, `tests/fixtures/<slug>/`, `tests/test_<slug>.py`, `docs/SOURCES.md`. Nothing else.

## Prompt template (paste into a Cowork or Claude Code chat with the repo open)

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

## Adapter checklist

- One class per module, subclassing `tracker.base.SourceAdapter`, with `name` equal to the `sources.yaml` key.
- All requests through `self.http` (`get`, `get_json`, `post_json`). It enforces the rate limit, retries, the User-Agent, robots.txt and the time budget; adapters cannot bypass it.
- `discover(lists, top_n)` yields `(GameRef, ListPlacement)`; positions start at 1 per list. Cache the list objects you see so `fetch` can reuse them.
- `fetch(refs)` yields one `GameSnapshot` per ref. Missing counters stay `None`. A 404 or private game yields `status="gone"`.
- A `players`-type counter never lands in a `plays` field. `players_now` = concurrent players right now; `players_total` = the platform's own unique-players figure (document the window in `metric_map`).
- `record_fixtures(out_dir, limit)` saves the real responses your parser reads; `tests/test_<slug>.py` parses them with a fake `Http`.
- Never load a game page in a browser: on several platforms that increments the counter we measure. `needs_browser` is a last resort and forces `poll_every_hours: 6`.
- If the platform returns 403s, a CAPTCHA or a Cloudflare challenge, `Http` raises `Blocked` and the pipeline records it. Do not add proxies, rotate agents or spoof headers.
