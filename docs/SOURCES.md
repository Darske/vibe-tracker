# Sources — research findings (Oct 29, 2026)

Research done from a cloud sandbox with `curl` and headless Chromium at ≤1 req/s, User-Agent `vibe-tracker-research/0.1`. No logins, no CAPTCHA/Cloudflare bypass. Raw evidence (captured responses, network logs, extraction scripts) is in `research/<slug>/`.

**Headline:** every platform is readable without a browser. Six expose JSON (Tesana, Rosebud, Bitmagic, Astrocade, Wavedash, and Playgama's AI index); Spawn and Remix embed full counters in server-rendered HTML. **Playwright is needed nowhere** — and on Rosebud, Wavedash and Bitmagic a browser visit to a game page would *inflate the counters we measure*, so adapters never load game pages in a browser.

## Summary table

| Platform | Segment | Best path | Robots | Plays | Players (unique) | Players now (CCU) | Likes | Remixes | Creator links | JS needed | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Tesana | creation | public JSON `api.tesana.ai` | allowed | ✅ `play_count` | ✅ `players_7d`, `players_today` | ✅ `playing_now` | ✅ `favorite_count` | ✅ `remix_count` | per-game `social_links` (mostly empty) | no | **Adapter #1** |
| Rosebud | creation | public JSON (Cloud Run gateway), batch list of top-10k by plays | allowed (profile pages disallowed) | ✅ `plays_count` | ✗ | ✗ | ✅ | ✅ `clones_count` | ✗ (profile blocked) | no | **Adapter #2** |
| Bitmagic | creation | JSON `/api/catalog/*` with public token from site JS | no robots.txt | ✅ `playCount` (+`viewCount`) | ✗ | ✅ `/api/catalog/live` | ✅ | ✗ | `homePageUrl` only | no | **Adapter #3** (small catalog: 150 games) |
| Astrocade | creation | JSON `astrocade-api-prod.com/v3` with self-minted `guest-<uuid>` header | no robots on API host; www allows /games /profile | ✅ `plays` | ✗ | ✗ | ✅ | ✅ `remixCount` | ✗ (bio only) | no | **Adapter #4** (approved Oct 31) — plays, likes, remixes, follower counts |
| Spawn | creation | HTML/RSC of allowed pages (`/?filter=…`, `/@user/slug`) | `/api` disallowed (but `llms.txt` invites `/api/feed`) | ✅ `play_count` | ✗ | ✅ `playing` (68-game multiplayer list) | ✅ | ✗ | ✗ | no | **Adapter #5** — HTML/RSC; CCU from the multiplayer list |
| Remix | creation | HTML/RSC of `/`, `/category/*`, `/g/<slug>` | `/api`, `/games/` disallowed; `/g/` allowed | ✅ `playCount` | ✅ `usersCount` | ✗ | ✅ `thumbsUpCount` | ✗ | ✗ | no | **Adapter #6** — HTML/RSC; grids + most-played chip lists |
| Wavedash | aggregator | Convex HTTP API (no auth) | allowed | ✗ | ✗ | ✅ but only top ~12 "trending" | ✗ | ✗ | website only | no | **Adapter #7** — placements + CCU (trending set) + comments; no `new` list |
| Playgama | aggregator | HTML with embedded `window.__query` state | `/api/` disallowed; pages allowed | ✗ (only aggregated AI index) | weekly leaderboard `totalPlayers` | ✗ | ✅ `rating.count` | ✗ | store links only | no | **Adapter #8** — placements, votes, 7-day leaderboard players (1 page fetch per game) |
| Playgama AI | aggregator | static Astro HTML of `playgama.ai/play` (+ `/play/<id>`) | no Disallow on playgama.ai; `developer.playgama.com/api` disallowed | ✅ all-time (exact on game page) + **own 24h** `data-top-plays24h` | ✗ | ✗ | ✅ Most Liked cards only (rounded ≥1k) | ✗ | ✗ (no creator shown) | no | **Adapter #9** — plays, plays 24h, likes, AI agent per game |

Coverage of the "expected metrics" column in the brief: plays are public on 6 of 8 (not Wavedash, not Playgama); concurrent players on 4 (Tesana, Spawn, Bitmagic, Wavedash-top-12); unique players on 2 (Tesana, Remix); remixes on 3 (Rosebud, Tesana, Astrocade). **No platform exposes creator X/YouTube/TikTok/Discord links as structured fields** — only Tesana has the fields (empty so far), Bitmagic/Wavedash have a website field. Outreach links will have to come from bios or manual entry.

---

## Tesana — https://tesana.com (creation)

- **List pages:** `/play`, `/play/charts`, `/play/section/{new,popular,top,free}`, `/play/category/<slug>` (22), `/play/tag/<slug>` (87). Game `/game/<slug>` (3,422 in sitemap), creator `/u/<username>`.
- **Endpoints** (base `https://api.tesana.ai`, unauthenticated JSON):
  - `GET /api/marketplace/charts/?metric=played|trending|new|rated|rising&limit=100` → `{results:[{rank, previous_rank, change, weeks_on_chart, game{…}}]}` — weekly charts, hard cap ≈104 rows. `played` ranks by `players_7d`.
  - `GET /api/marketplace/section/{popular|new|top|free}/?page=N` → 200 games, 24/page.
  - `GET /api/marketplace/search/?q=&sort=plays|popular|recent&page_size=60&page=N` → full catalog (3,508), 59 pages.
  - `GET /api/play/playing-now/top/?limit=50` → games with players now; `GET /api/play/playing-now/?ids=<public_id,…>` → `{id: n}` batch CCU.
  - `GET /api/games/store/<slug>/` → full record incl. `remix_count`, `verdict`, `social_links{twitter,youtube,tiktok,twitch,discord,instagram,reddit}`, `seals`.
  - `GET /api/games/users/<username>/` → `{username, display_name, bio, links[], games_count, total_plays, follower_count, stats{players_total,…}, games[60]}`.
  - Side effect to avoid: the SPA posts `/api/games/<id>/view/` on page load — use JSON, never a browser.
- **Game object fields:** `id, public_id, name, slug, user, user_id, tagline, classified_category{slug,name}, tags[], engine_kind, is_mobile, published_at, created_at, play_count, players_7d, players_today, players_7d_prev, favorite_count, playing_now, badges[], is_featured, pricing_type, price, allow_remixing, cover_url`.
- **metric_map:** `plays` = `play_count`, cumulative play sessions. `players` = `players_7d`, unique players in the trailing week (platform's own figure). `players_now` = `playing_now`, concurrent players right now.
- **JS required:** no. **Robots:** site paths allowed except `/play/search`; `api.tesana.ai` has no robots.txt.
- **Rate limit observed:** none at 1 r/s (~30 calls).
- **Open questions:** `players_total` (creator) vs `play_count` definitions; charts cap at 104; no ToS statement about API use found.

## Rosebud AI — https://rosebud.ai (creation)

- **List pages:** `/play` (SSR; "New games" 100, "Featured" 35, "Popular" 12, 24 genre rows), `/play/c/<slug>` (24 categories × 100 games, SSR, no pagination). Game `/play/<slug|uuid>`. Creator `/profile/<uuid>` — **robots-disallowed**, not fetched.
- **Endpoints** (host `https://playground-gateway-v2-snduoq54tq-uc.a.run.app`, DRF, no auth):
  - `GET /api/published-projects/?ordering=-plays_count&page_size=100&page=1..100` → `{count, next, results:[{id, name, published_name, plays_count, likes_count, clones_count, comments_count, creator_username, creator_id, tags[], source_project, is_remixable, preview_image, mode}]}`. **This is the batch endpoint**: top 10,000 games by plays in 100 requests. `ordering=-created` gives newest. `tags=Play%20Page` = curated set (344). `count` is capped at 100×page_size, not a true total.
  - `GET /api/published-projects/<id>/play-detail/` → same fields + revision ids (1.2 KB). Avoid `/api/published-projects/<id>/` (2 MB, embeds game source; only source of a date: `deployed_revision.created`).
  - `GET /api/published-projects/<id>/comments/`; `GET /api/users/<creator_id>/` → `{username}` only.
  - Side effects to avoid: game page load fires `POST /api/plays/` — **a browser visit increments plays_count** (verified: 94,351 → 94,352).
- **metric_map:** `plays` = `plays_count`, game-page loads / session starts (not unique). `remixes` = `clones_count` (includes unpublished remixes). No players or CCU.
- **JS required:** no. **Robots:** `/play*` allowed, `/profile/` disallowed; API host has no robots.txt.
- **Rate limit observed:** none at 1 r/s (~60 calls, Google Frontend).
- **Open questions:** no published date in list rows (use first_seen); creator followers/links unobtainable (profile blocked); list ordering is all-time plays only.

## Bitmagic — https://bitmagic.ai (creation)

- **List pages:** `/`, `/games/`, `/trending/` (+ category tabs), `/feed/`. Game `/play/<ID>/`, creator `/u/<brandId>/`. Browsing is not waitlist-gated; only creation is. **Catalog is small: 150 public games, ~87 creators.**
- **Endpoints** (same origin; header `X-API-Token: <CATALOG_API_TOKEN>` read from `/assets/config-*.js`; without it 401):
  - `GET /api/catalog/browse?sort=trending|new|popular|random&limit=50&offset=0|50|100` → `{games:[{gameId, name, publishedAt, access, primaryPlatform, viewCount, playCount, likeCount, commentCount, creator{canonicalId, publicId, brandId, displayName}}], total:150, nextOffset}`.
  - `GET /api/trending?offset&limit=50` → `{items:[{gameId, globalRank, playCount, likeCount, creator}], total}`; `GET /api/trending/<category>`.
  - `GET /api/catalog/games?ids=A,B,…` → batch counters; `GET /api/catalog/live?ids=…` → `{counts:{id:n}}` players in room now.
  - `GET /api/catalog/stats` → `{totalGames, totalPlays, totalCreators, newGamesThisWeek, playingNow}`.
  - `GET /api/players/<brandId>/profile` → `{followerCount, memberSince, homePageUrl, totalPlays, totalViews, gamesTotal, games[]}`; `GET /api/categories`.
  - Side effects to avoid: `POST /api/games/<id>/view` and `/play` fire from the page.
- **metric_map:** `plays` = `playCount`, Play-button presses (per-session dedup likely; not unique users). `views` = `viewCount`, page loads deduped per viewer session. `players_now` = `/api/catalog/live` counts.
- **JS required:** no (API). HTML shells are empty. **Robots:** none (GCS NoSuchKey) → nothing disallowed.
- **Rate limit observed:** none (~35 calls).
- **Open questions:** token rotates per deploy → adapter re-reads it on 401; max `ids` per batch call; two `publishedAt` values (catalog vs `game.json`).

## Astrocade — https://www.astrocade.com (creation)

- **List pages:** `/`, `/category/top_picks` ("Players' Choice" = most played, 29), `/category/trending` (30), `/category/beta` (50), 20 genre categories capped at 100 each, `/category/halloween_2026`. Game `/games/<slug>/<ULID>` (5,883 in sitemap), creator `/profile/<username>` (2,064).
- **Endpoints** (base `https://astrocade-api-prod.com/v3`, AWS API Gateway; requires `authorization: guest-<uuid>` — the site mints one client-side; any uuid works):
  - `GET /v3/feed?filter=categorized_feed&category=<key>&categoryPageSize=100` → `{category, label, games[], hasMore, paginationToken}`; `&numCategories=60` returns all 24 rows at once.
  - `GET /v3/games?gameId=<id>` → `{game}`; `GET /v3/games?username=<u>&sortBy=popular|newest&pageSize=100&view=profile`.
  - `GET /v3/users?username=<u>` → `{userId, username, totalPlays, followerCount, bio}`; `GET /v3/analytics?filter=stats&username=<u>` → `{playCount, commentsCount, likeCount}`.
  - Game page HTML also carries the full game object in RSC (5-min ISR cache), so a curl-HTML fallback exists for per-game fetches.
- **Game fields:** `gameId, title, username, ownerId, plays, likeCount, commentsCount, shares, remixCount, isRemix, baseGameId, firstPublishedTimestamp, lastPublishedTimestamp, createdAt, ownerTotalPlays, ownerFollowerCount, isPublic`.
- **metric_map:** `plays` = session starts ("each instance in which a user starts a playable", contest rules). `remixes` = `remixCount`. No unique or concurrent players.
- **JS required:** no (API). **Robots:** www allows `/games/`, `/profile/`, `/category/`; API host has no robots.txt.
- **Rate limit observed:** none (~60 calls).
- **Policy question for Giorgio:** this is the app's internal API with a self-minted guest id, not a documented public API. Robots does not forbid it and nothing is behind a login, but it is less clearly "public" than Tesana/Rosebud. Recommendation: use it with a fixed guest id and the project UA; fall back to curl of `/games/<slug>/<id>` HTML (allowed, same object) if they object or it stops accepting unknown ids.

## Spawn — https://www.spawn.co (creation)

- **List pages (all SSR, robots-allowed):** `/?filter=top` (64 games total; 24 in HTML), `/?filter=new` (24), **`/?filter=multiplayer` (68 cards, each with `playing` — the CCU snapshot)**, `/?genre=<slug>` (27 genres × 24, "hottest first"). Game `/@user/slug` (3,831 in sitemap, 950 creators). Creator `/@user`.
- **Data location:** Next.js RSC flight chunks (`self.__next_f.push`) in the HTML — curl is enough. Card: `{game:{id, app_id, creator_id, play_count, like_count, comment_count, play_shape, pass_price_cents, posted_at, last_published_at, genre_tags, ai_genre_tags, games{name, adventure_name}, profiles{username}}, playing, votes{loves,hates}}`. Also a static `<section data-signs-of-life>` block with "188 likes · 13,952 plays · 90 playing right now" per game (regex fallback). Game page HTML: `summary:{playCount, likeCount, commentCount, loves, hates}`.
- **Endpoints seen but robots-disallowed (`/api`):** `/api/discovery/tab`, `/api/games/live?ids=` (CCU, polled every 5 s by the site), `/api/feed`. Note `llms.txt`/`openapi.json` explicitly invite agents to `GET /api/feed` with no credential — **conflict; we follow robots and use HTML**. `Content-Signal: ai-input=yes`.
- **Creator:** den page embeds `{handle, likeCount, gameCount, follower_count, partner, games[]}`; no external links.
- **metric_map:** `plays` = `play_count`, cumulative play sessions. `players_now` = `playing`, concurrent players in rooms. `peopleNow.people` = people on Spawn in the last hour (site-wide, not CCU).
- **JS required:** no. Playwright flags webdriver and posts impressions — never use it here.
- **Rate limit observed:** none; pages are 1–2 MB.
- **Open questions:** whether to use `/api/feed` given the llms.txt invitation; coverage beyond first page needs per-game fetches (3.8k games ≈ 65 min at 1 r/s) — v1 tracks the lists' first pages + multiplayer list.

## Remix — https://remix.gg (creation)

- **List pages:** `/` (shelves: Picks 48, Just Launched 24, Multiplayer 24, Mobile 24, Portals 24), `/category` (31), `/category/<name>` page 1 = 48-game grid **with counters** + "most played first" chip list (120 slugs/page, no counters; e.g. action 95 pages). Game `/g/<slug>` (24,003 in sitemap). Creator `/u/<id>` (9,237).
- **Data location:** SSR RSC chunks; curl is enough. Category grid object: `{id, slug, name, isMultiplayer, isLive, playCount, totalSecondsPlayed ($n bigint), thumbsUpCount, commentCount, usersCount, createdAt, liveVersion{launchedAt}, categories[], creator{id, username}, trendingPlays, isStaffPick, homeBadge}`. Game page: same + `description`, JSON-LD `VideoGame` with `datePublished`, `PlayAction`, `LikeAction`.
- **Endpoints:** none allowed — site uses `/api/trpc/*` (disallowed). Official HTTP API needs a Studio key and only lists your own games.
- **Creator:** `/u/<id>` shows games count and total plays; no followers, no links.
- **metric_map:** `plays` = `playCount`, sessions started. `players` = `usersCount`, unique users who played. `likes` = `thumbsUpCount`; `rating` = % positive. No CCU.
- **JS required:** no. Pages are Vercel ISR — `/g/` pages can be stale (check `age` header).
- **Rate limit observed:** none (~45 calls).
- **Open questions:** does the chip order truly equal playCount desc; ISR staleness of `/g/` pages; v1 plan: 31 category grids + home (~32 req) daily, then `/g/<slug>` for the top ~240 per category over the 2-hourly polls.

## Wavedash — https://wavedash.com (aggregator)

- **List pages:** `/` (Trending rail of ~12 games with "N playing" + Recommended feed of all 1,250 games), `/category/<slug>` (97), `/games/tags/<tag>` (329). No top/most-played/new list. Sitemaps: 1,250 games, 480 teams, 24k users.
- **Endpoints** (Convex HTTP API, `POST https://convex-api.wavedash.com/api/query|action`, body `{"path":fn,"args":{},"format":"json"}`, no auth):
  - `gamePlayerCounts:listTrendingGames` → `[{gameId, count}]` — **the only public usage number: concurrent players, top ~12 games only**.
  - `gameFeed/feed:pageV2` (action) `{cursor:{offset:0,phase:0}, limit:24, seed}` → full catalog in 53 pages.
  - `games:getPublicGameBySlug`, `gameComments/queries:getCountForGameCloud` (comment count), `gameAchievements:getAchievementsForGame` (`unlockRate` %), `leaderboards:*`, `organizations/queries:get` (`gameCount, memberCount, website, location`).
  - Side effect to avoid: loading `/games/<slug>` in a browser auto-launches the game (`POST /api/game-launch`) — a Playwright visit inflates play sessions. curl of the HTML does not.
- **Counters:** no plays, no total players, no likes count, no ratings. Comments count and achievement unlock rates only.
- **metric_map:** `players_now` = trending `count`, concurrent players in-game (top 12 only). `plays` = not public.
- **JS required:** no. **Robots:** `Allow: /`.
- **Verdict:** track list placements (trending rank), CCU for the trending set, and catalog growth (new games/day from the feed). No "vibe-coded" flag exists; `gameBuild.engine` is the only tool signal.

## Playgama — https://playgama.com (aggregator)

- **List pages (SSR, allowed):** `/category/top_playgama` ("Playgama Top", 62, with votes), `/category/trending_now` (392, 45/page), `/category/new` (1,943), 197 categories, 21 `/collections/top-*`. `?page=N` works without JS. Game `/game/<hru>` (5,904 in sitemap), developer `/developer/<id>` (1,736).
- **Data location:** `window.__query` dehydrated cache in the HTML (devalue form; extractor in `research/scripts/extract_playgama_state.js`). Game page: `{game:{id, hru, title, rating{value, count}, developer, developer_id, engine[], links[], createdAt, updatedAt, position, badges}, leaderboard{totalPlayers, entries}}`.
- **Endpoints:** `/api/v1/categories/games?…` exists but `/api/` is **robots-disallowed** → not used. Partner `export-list` API needs a `clid`.
- **AI index:** `https://playgama.com/ai-game-index/data.json` (public, 20 KB): `totals{games:5491, launches}`, per-agent share (claude-code 51%), play-time medians — **aggregate only**, sourced from the separate sandbox **`https://playgama.ai/play`**, which does show per-game `plays24h`, all-time plays, likes and avg playtime for vibe-coded games (47 cards per tab in static HTML).
- **Counters:** `rating.count` (votes = likes), `rating.value`, `leaderboard.totalPlayers` (players ranked in the last 7 days — a weekly-active proxy), category `position`. No plays, no CCU.
- **metric_map:** `likes` = `rating.count`. `players` (proxy) = leaderboard `totalPlayers`, players with ≥1 ranked session in the trailing 7 days. `plays` = not public on playgama.com (Playgama defines a play as a ≥30 s Bridge session, index only).
- **JS required:** no. **Robots:** pages allowed, `/api/` disallowed.
- **Verdict:** placements (top/trending/new rank), votes and weekly leaderboard players. **Question for Giorgio:** add `playgama.ai/play` as a ninth source? It is the actual vibe-coded catalog with per-game plays and the by-tool breakdown you want; playgama.com itself is mostly conventional HTML5 games.

## Playgama AI — https://playgama.ai/play (aggregator)

- **List pages (static Astro HTML, one request):** `/play` renders every tab server-side as `<ol class="top-tabs__panel" data-top-panel="<tab>">` with 47 `<li class="top-tabs__item">` cards each (plus a trailing "Top 100 games" button that is not a card). Tabs seen 2026-10-31: `plays24h` ("Gameplays 24h"), `freshDrops`, `likesAllTime` ("Most Liked", 29 cards), `playsAllTime` ("All-Time"), `playtime`, `challenge`, and per-agent `claude24h`, `codex24h`, `cursor24h` (17). A second copy of the panels (`top-sidebar-*`) contains only `data-top-stub` placeholders. `<meta name="robots" content="noindex, nofollow">` is set on every page. Evidence: `research/playgama_ai/`.
- **Data location:** per card `<a class="top-snippet" href="/play/<id>" data-top-site-id="<id>" data-top-plays24h="960">`, `top-snippet__rank`, `img.top-snippet__cover` (static.playgama.com icon), `top-snippet__title`, `top-snippet__measure` (the tab's figure as text: `960 played`, `2.5k played`, `2k likes`, `33m 18s avg`, `1k played, 227 likes` on challenge), `top-snippet__agent` (AI agent chip: Claude Code, Codex, Cursor, Claude app, ChatGPT, Google Antigravity, Gemini CLI, OpenCode). `data-top-plays24h` is the exact trailing-24h play count on every tab except `challenge`, where the same attribute holds the all-time count. Text figures are rounded above 1k.
- **Game page `/play/<id>`:** static, 200 for a live game, 404 otherwise (no redirect observed with curl). `<span class="header__played">1,007 played</span>` is the exact all-time play count; `data-game-id`, `data-game-title`, `og:image`. The like counter (`data-game-likes`) is empty until JS calls the API; no creator, agent or publish date is rendered. Caveat seen live: the page count can lag the catalog by a few minutes and read `0 played` for a game with 2k likes (apparently re-published), so the adapter floors `plays_total` at the card's 24h count.
- **Endpoints:** the page lazy-loads "Top 100" pages from `data-top-api-host="https://developer.playgama.com/api"`, whose robots.txt has `Disallow: /api/` → **not used**. `https://playgama.com/ai-game-index/data.json` (robots-allowed, 20 KB) is aggregate only: `totals{games, launches}`, per-agent share by day (`claude-code`, `codex`, `cursor`, `antigravity`, `opencode`, `gemini-cli`, `grok-build`, `workbuddy`, `zcode`, `claude-app`, `chatgpt`, `other`), per-model share, play-time percentiles, likes per 1,000 plays. Saved as a fixture for reference, not polled.
- **Counters:** all-time plays (exact from the game page, rounded on the card), own 24h plays (exact), likes (Most Liked / Challenge cards only, rounded ≥1k), average playtime (playtime tab). No CCU, no unique players, no remixes, no comments.
- **metric_map:** `plays` = Bridge sessions of ≥30 s, all time (Playgama's own definition on `/ai-game-index`). `plays_24h` = Playgama's trailing-24h play count. `likes` = all-time likes where shown. `ai_tool` = agent chip slugged to the AI-index keys (`claude-code`, `codex`, …).
- **JS required:** no. **Robots:** `playgama.ai/robots.txt` has no `Disallow` (only Cloudflare content-signal comments); `developer.playgama.com` disallows `/api/`.
- **Verdict:** adapter #9 (`playgama_ai`): placements on the four main tabs, exact all-time plays via one `GET /play/<id>` per tracked game (≈200 requests ≈ 3.5 min per run at 1 r/s; `game_pages: false` in sources.yaml falls back to the rounded card figure), 24h plays and AI agent from the cards. No creators exist on the site (`fetch_creator` → None). Per-agent tabs (`claude24h`, `codex24h`, `cursor24h`) and `playtime` can be added to `lists` without code changes.

---

## Implementation notes added at milestone 2 (Oct 31)

- **Astrocade**: approved. Guest id is minted once per adapter instance (`ASTROCADE_GUEST_ID` pins it). `top_picks`/`trending` are ~30 games each; genre rows fill to 100. Every game gets `ai_tool = "astrocade"` (all games are built with Astrocade's own builder).
- **Spawn**: `?offset=` paging on HTML is ignored, so each list is its first page (23–24 cards; ~60 on multiplayer). `follower_count` disappeared from the creator den page between Oct 29 and Oct 31; it is still on game pages, so the adapter caches it from there. `players_now` is 0 for a game on the multiplayer list without a `playing` key (that list omits zeros), None elsewhere.
- **Remix**: the 48-game category grid is a trending/staff-pick ranking, not "most played first" — the chip list (120 slugs, no counters) is the true most-played order; both are tracked as separate lists (`<category>` and `most_played_<category>`). `/g/` pages are ISR-cached for up to ~3 h, so two consecutive polls can return identical numbers. Home-shelf objects carry only `playCount`.
- **Wavedash**: no `new` list — feed objects have no dates and resolving them costs one call per game (~1,270). Optional `catalog` list walks the whole feed (53 calls) so new games get a first-seen date. Unknown slugs come back as `{"status":"error","code":"not_found"}` → gone. Thumbnails at `https://uploads.wavedash.com/<r2Key>`. Trending set was 4 games on Oct 31 (varies with traffic).
- **Playgama**: list tuples carry no votes after all; `rating.count` is only on the game page, so fetch costs one GET per game (~250 games ≈ 4 min). The `window.__query` devalue payload is parsed with a small recursive-descent parser (`tracker/sources/playgama.py`), validated against the research captures.
- **Playgama AI**: added as the ninth source (see its section). Card figures are rounded above 1k, so the adapter fetches `/play/<id>` (exact "N played") for each game; `developer.playgama.com/api` is robots-disallowed and never called.
- **HTTP 410 Gone** is treated like 404 (→ `status='gone'`) after Tesana returned one for a delisted game and killed the whole platform's fetch in the first full run.

## Decisions taken in research

1. Follow robots.txt strictly even where a platform's `llms.txt` invites API use (Spawn). HTML of allowed pages carries the same numbers.
2. Never load a game page in a browser on any platform (inflates plays/views on Rosebud, Bitmagic, Wavedash; flags webdriver on Spawn). No adapter sets `needs_browser`.
3. Milestone-1 adapters: **Tesana, Rosebud, Bitmagic** — documented-shape public JSON, no tokens or robots ambiguity. Astrocade is next (best data of the rest) pending the policy answer.
4. Separate API hosts (`api.tesana.ai`, Rosebud's Cloud Run gateway, `astrocade-api-prod.com`, `convex-api.wavedash.com`) have no robots.txt; treated as allowed, polled at 1 r/s with the project UA.

## Legacy web game platforms (cohort: legacy)

Researched Nov 10, 2026; full write-up in `docs/SOURCES_LEGACY.md`.

| Platform | Data | Counters | AI segmentation | Terms |
|---|---|---|---|---|
| CrazyGames | keyless JSON `api.crazygames.com/v4` | plays, likes (whole catalogue) | none | ToS 6.3.G forbids automated collection — tracked at owner's decision, daily |
| Poki | keyless JSON `api.poki.com` | thumbs up/down (no plays) | none | ToS forbids bots without consent — tracked at owner's decision, ≤1 req/3 s |
| itch.io | HTML browse pages + `format=json` | ratings count (top-rated list), placements | creator AI disclosure (AI-assisted / AI code / No AI) | no scraping clause; heavy 429 rate limiting |
| Kongregate | HTML lists + `metrics.json` | plays, favourites, ratings | description declarations (regex) | broad anti-bot wording aimed at cheating |
