"""Release-count SQL and per-platform coverage notes, shared by the releases job and the dashboard.
No imports on purpose: the Streamlit app loads this without the scraper's dependencies."""

COVERAGE = {
    "tesana": ("complete", "every game on the marketplace, dated by Tesana's publish date (full history)"),
    "bitmagic": ("complete", "every game in the catalogue, dated by Bitmagic's publish date (full history)"),
    "spawn": ("complete", "every game in Spawn's sitemap; new ones counted from the first sweep, older ones dated as their publish dates are filled in"),
    "remix": ("complete", "every game in Remix's sitemap; new ones counted from the first sweep, older ones dated as their publish dates are filled in"),
    "wavedash": ("complete", "every game in Wavedash's sitemap; new ones counted from the first sweep, older ones dated as their publish dates are filled in"),
    "rosebud": ("complete", "every new game in Rosebud's newest feed from the first sweep; Rosebud publishes no dates, so no history"),
    "playgama_ai": ("net", "day-over-day change in the catalogue size Playgama publishes (net of removals); no per-game list"),
    "playgama": ("partial", "only games that appear in Playgama's New list"),
    "astrocade": ("partial", "only games that reached one of the tracked Astrocade lists (no public list of all new games)"),
}

DATE_LOOKUP = ("spawn", "remix", "wavedash")  # publish dates looked up per game (backfill), the rest come with the feed or never
FROM_GAMES = ("astrocade",)   # partial: no public list of all new games, so use the games the scrape tracks
NET_FROM_TOTALS = ("playgama_ai",)  # only a catalogue total is published: releases = day-over-day change

# Release counts per platform per period ('week' or 'month'). Release date = the platform's publish date when
# known, else the day our sweep first saw the game, except baseline rows (already there on the first sweep).
RELEASE_COUNTS_SQL = """
with r as (
  select source, coalesce(published_at, case when not baseline then first_seen end) as released
    from releases where source = any(%(sources)s)
  union all
  select g.source, coalesce(g.published_at, g.first_seen) from games g
   where g.source = any(%(from_games)s) and g.source = any(%(sources)s)
),
counted as (
  select source, date_trunc(%(period)s, released at time zone 'UTC')::date as period, count(*)::int as releases
    from r where released is not null and released >= %(since)s group by 1, 2
),
net as (
  select source, day, games_total - lag(games_total) over (partition by source order by day) as added
    from catalog_daily where source = any(%(net)s) and source = any(%(sources)s)
)
select source, period, releases from counted
union all
select source, date_trunc(%(period)s, day)::date, sum(added)::int from net
 where added is not null and day >= %(since)s::date group by 1, 2
order by 2, 1
"""

# What each platform's numbers rest on: games known, how many have a date, when counting of new games began.
RELEASE_COVERAGE_SQL = """
select source, count(*)::int as games, count(published_at)::int as dated,
       count(*) filter (where not baseline)::int as seen_new, min(first_seen) as since,
       count(*) filter (where published_at is null and not date_checked)::int as dates_pending
  from releases group by 1
"""


