# Legacy web game platforms — source research (Nov 10, 2026)

Candidates for a second cohort, **legacy web game platforms**, next to the current **new entrants** (Tesana, Rosebud,
Bitmagic, Astrocade, Spawn, Remix, Wavedash, Playgama, Playgama AI). Researched with plain HTTP GETs only (≤1 req/s,
UA `vibe-tracker research (+https://github.com/Darske/vibe-tracker)`, no login, robots respected, no game/play pages).
Raw samples were kept in the session scratchpad (`research/<site>/`). Key claims below (ToS quotes, itch.io counts,
CrazyGames totals) were re-checked against the saved pages.

## Summary

| | CrazyGames | Poki | itch.io (HTML5) | Kongregate |
|---|---|---|---|---|
| Catalogue | 4,681 launched + 1,247 soft-launch | 2,082 (1,512 indexed) | 857,715 HTML5 games | ~47k in sitemap |
| New games | ~60/week (`addedOn`) | ~7/week (`release_date`) | ~7,900/week (last-7-days) | ~25–30/week (reopened 2026) |
| Plays | ✅ `totalPlays` (all-time; reads 1 for new games; seems refreshed ~daily) | ✗ not public | ✗ owner-only | ✅ `gameplays_count` (exact, `metrics.json`) |
| Likes / votes | ✅ `totalLikes`, up/down, rating | ✅ thumbs up/down (live, exact) | ratings avg+count on top-rated list only | ✅ favourites, rating + count |
| Players now | ✗ | coarse "Live N+" buckets | ✗ | ✗ |
| **AI segmentation** | **none** | **none** (policy: allowed, no label) | **yes**: creator disclosure filter, 90,742 AI-assisted (10.6%), 70,809 AI-code, 217,240 explicit "No AI", ~64% undisclosed | **free text**: 2026 uploads declare AI use in the description (7 of 13 sampled) |
| Data access | keyless JSON `api.crazygames.com/v4` (site's own) | keyless JSON `api.poki.com` (site's own) | HTML browse pages, `?format=json`, `.xml` feeds | HTML lists + legacy `/games/<dev>/<slug>/metrics.json` |
| robots.txt | allows what we'd use | `Disallow: /*?` on poki.com; API hosts none | allows `/games/*` | allows lists, game pages, metrics.json |
| **ToS on automated access** | **forbidden** — 6.3.G "You will not conduct any systematic or automated data collection activities (including scraping, …)" | **forbidden without written consent** — "you will not (a) use any robot, spider or other automatic device … to access the Website … without the prior written consent of Poki" | no scraping clause; staff say humans come before bots | broad anti-"bots/scripts/extraction tools" wording under "Don't cheat!" (aimed at players/games) |
| Bot protection | Cloudflare, no challenge seen (51 req) | Cloudflare + client-side bot score, no challenge (88 req) | **aggressive 429s** (needed 20–75 s spacing) + Cloudflare challenge on 3-segment browse URLs | CloudFront, no challenge (95 req) |

## CrazyGames — https://www.crazygames.com
- Lists: `/hot` (50), `/new` (4,678, 70/page), `/updated`, `/c/<cat>` (16), `/t/<tag>` (200). Every page is Next.js with `__NEXT_DATA__`.
- API `https://api.crazygames.com/v4/en_US` (no auth, CORS *, edge-cached 4 h): `/games?sorting=new|default[&label=hot]&paginationPage=N&paginationSize≤100` → `{games{total, items[]}}` with `totalPlays, totalLikes, releaseYear, …`; `/games/basicLaunch` (1,247 soft-launch games, plays masked to 1); `/gamesData?gameIds=[…]` batch; `/page/game/<slug>` full record (`upvotes, downvotes, rating, developer, addedOn, basicLaunchOn, loaderType`); `/sitemap` (all slugs).
- AI: no tag/category/badge/field; 200 tags checked, `/t/ai*` 404; developer docs silent. Only engine signals (`loaderType`, tags `phaser`/`scratch`).
- Adapter if allowed: `/games?sorting=new` paged = whole catalogue in 47 requests (plays + likes for every game); daily cadence is enough (plays refresh ~daily). Releases: new ids + `addedOn`.

## Poki — https://poki.com
- Lists: home grid (`/list/game/HOME?limit=500`, country-dependent), `/en/popular` (99), `/en/new` (130, release-date order), 141 categories. `window.INITIAL_STATE` in HTML or `api.poki.com` JSON (`/game/<slug>`, `/category/<slug>`, `/allgames` 21 pages = full catalogue).
- Per game: `rating{up_count, down_count, rating}` (live, exact), `trending_rank` (top ~50 only), `release_date`, `developers`; `realtime.poki.com/estimate/metrics?id=` → coarse "Live N+" players. **No plays, no players.**
- AI: none in categories, tags or fields. Poki's developer page ("Working with AI") allows AI if it adds value, no public label.
- Adapter if allowed: lists + per-game vote counts (~300 req/poll); usage proxy = change in votes. Releases: `/en/new` + `release_date`.

## itch.io — https://itch.io (HTML5)
- Lists (36/page, `?page=N&format=json`, `.xml` RSS): `/games/html5` (popular), `/games/new-and-popular/html5`, `/games/top-rated/html5` (cells carry "4.89 average rating from 11,663 total ratings"), `/games/top-sellers/html5`, `/games/newest/html5`, date filters `/games/html5/last-day|last-7-days|last-30-days`.
- **AI disclosure** (since 2024-11-20, optional for games, mandatory for assets): browse filter `/games/ai-assisted/html5` 90,742; sub-types `ai-code` 70,809, `ai-graphics` 51,448, `ai-text` 33,860, `ai-audio` 28,577; `/games/html5/tag-no-ai` 217,240. Nov 3, 2026: AI spam purge + disclosure filters in search.
- No views/plays/downloads/likes public anywhere. `data.json` per game has no counters.
- Adapter: placements (popular / new-and-popular / top-rated / top-sellers, AI vs all), ratings from top-rated cells, catalogue + AI-share totals, releases per day (last-day totals, AI total day-over-day). ~14 requests per poll at 20–30 s spacing with back-off on 429; never bypass Cloudflare challenges.

## Kongregate — https://www.kongregate.com
- Reopened to uploads in 2026 ("Welcome Back Revenue Share Program"), after closing submissions in July 2020; sold to Monumental in 2024. ~25–30 new games/week, mostly tiny; big numbers are old catalogue games (Bloons TD 5 35.5M plays).
- Lists (HTML, 50/page): `/en/hot-new-games` (49), `/en/new-and-updated-games?sort=newest` (50 ≈ 7 days), main categories `?page=N&sort=top|plays|newest`.
- `GET /games/<dev>/<slug>/metrics.json` → exact `gameplays_count`, `favorites_count`, `rating`, "of N ratings" (not cached; curl of a game page does not add a play — tested). Game page JSON-LD: `datePublished`, `aggregateRating`, description.
- AI: no tag/category; submission checklist asks for an AI declaration, which developers put in the description ("made with some assistance from AI tools…" / "does not use generative AI"). Sub-segment = regex on description: declared yes / declared no / undeclared.
- Adapter: lists (~10–18 req) + `metrics.json` per game (~150–250) per poll, game page once per new game.

## Recommendation
- **Build now:** itch.io (best AI segmentation, no ToS prohibition, must be slow and polite) and Kongregate (exact plays/favourites/ratings, robots-clean; ToS wording is broad but aimed at cheating — same kind of policy call as Astrocade).
- **Ask first:** CrazyGames and Poki explicitly prohibit automated collection in their terms. Email them for permission or a partner feed; build only with consent (CrazyGames would be the most valuable legacy source: plays + likes for the whole catalogue in ~47 requests).
- New cohort field on sources: `new_entrant` | `legacy`; keep `aggregator` / `creation` as the type. Legacy AI sub-segment from itch.io disclosure and Kongregate declarations.
