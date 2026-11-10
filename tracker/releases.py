"""Game releases per platform: every game a platform publishes, not only the tracked top lists.

    python -m tracker.releases run [--source X] [--dry-run]      # daily (releases workflow)

Why a separate job: the scrape follows top lists, so counting release dates of tracked games would count only the
hits. Here each platform's newest-first feed or sitemap is swept once a day and every game URL is kept in
`releases`, so new URLs = new releases. Where the platform exposes a publish date it is stored (and backfilled a few
hundred games per run), which also gives history from before the tracker existed.

Per platform (see COVERAGE for the wording the dashboard shows):
  tesana       marketplace search sort=recent (all ~3.8k games, with published_at)      complete, full history
  bitmagic     catalog browse sort=new (all games, with publishedAt)                     complete, full history
  spawn        sitemap.xml game URLs (diff day to day); posted_at backfilled per game    complete from first sweep
  remix        sitemaps/games-*.xml (diff); createdAt backfilled per game                complete from first sweep
  wavedash     sitemap/games.xml (diff); firstPublishedAt backfilled per game            complete from first sweep
  rosebud      published-projects ordering=-created, newest pages until known games      complete from first sweep, no dates
  playgama_ai  ai-game-index totals.games (catalogue size only)                          net daily change, no per-game list
  playgama     its "new" list (first 100)                                                partial
  astrocade    no public newest feed or full sitemap -> dashboard uses tracked games     partial
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree

from tracker import db
from tracker.http import BudgetExceeded, Http, TimeBudget
from tracker.models import GameRef
from tracker.registry import adapter_class, load_sources
from tracker.release_queries import FROM_GAMES, NET_FROM_TOTALS, RELEASE_COUNTS_SQL  # noqa: F401 (COVERAGE etc. re-exported)
from tracker.release_queries import COVERAGE, RELEASE_COVERAGE_SQL  # noqa: F401

log = logging.getLogger("tracker.releases")

# Publish-date lookups per daily run (1 request each, at the platform's rate limit). Each platform runs in its own
# job, so these fit one job's budget: Remix ~24k backlog -> ~3 weeks, Spawn ~3.5k -> ~5 days, Wavedash ~1.1k -> ~3 days.
DATE_LOOKUP_CAP = {"remix": 1200, "spawn": 700, "wavedash": 500}
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass
class Sweep:
    source: str
    items: dict[str, datetime | None] = field(default_factory=dict)  # canonical url -> published_at (or None)
    total: int | None = None  # catalogue size when the platform states it (or the sweep is the whole catalogue)


def canon(url: str) -> str:
    return url.strip().rstrip("/")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


# ------------------------------------------------------------------ sitemap parsing (pure, tested)
def sitemap_locs(xml_text: str) -> list[str]:
    """All <loc> values of a urlset or sitemapindex."""
    root = ElementTree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    return [el.text.strip() for el in root.iter(f"{SITEMAP_NS}loc") if el.text]


SPAWN_GAME = re.compile(r"^https://www\.spawn\.co/@[^/]+/[^/?#]+$")
REMIX_GAME = re.compile(r"^https://remix\.gg/g/[^/?#]+$")
WAVEDASH_GAME = re.compile(r"^https://wavedash\.com/games/[^/?#]+$")


def game_urls(locs: list[str], pattern: re.Pattern) -> list[str]:
    return sorted({canon(u) for u in locs if pattern.match(canon(u))})


# ------------------------------------------------------------------ sweeps
def sweep_tesana(http: Http, adapter, known: set[str]) -> Sweep:
    api = "https://api.tesana.ai"
    out = Sweep("tesana")
    page = 1
    while True:
        data = http.get_json(f"{api}/api/marketplace/search/?q=&sort=recent&page_size=60&page={page}") or {}
        out.total = data.get("count", out.total)
        rows = data.get("results") or []
        fresh = 0
        for g in rows:
            if not g.get("slug"):
                continue
            url = canon(adapter.game_url(g["slug"]))
            fresh += url not in known
            out.items[url] = _parse_dt(g.get("published_at"))
        if not data.get("next") or not rows or (known and fresh == 0):
            break  # first sweep walks everything; later sweeps stop at the first page with nothing new
        page += 1
    return out


def sweep_bitmagic(http: Http, adapter, known: set[str]) -> Sweep:
    out = Sweep("bitmagic")
    for _, obj in adapter._iter_browse("new", 100_000):
        gid = obj.get("gameId")
        if gid:
            out.items[canon(adapter._url_for(gid))] = _parse_dt(obj.get("publishedAt"))
    out.total = len(out.items)
    return out


def _sitemap_sweep(http: Http, source: str, url: str, pattern: re.Pattern, index_filter: str | None = None) -> Sweep:
    out = Sweep(source)
    locs = sitemap_locs(http.get(url).text)
    if index_filter:  # sitemap index -> the game sitemaps
        child = [u for u in locs if index_filter in u]
        locs = []
        for u in child:
            locs += sitemap_locs(http.get(u).text)
    for u in game_urls(locs, pattern):
        out.items[u] = None
    out.total = len(out.items)
    return out


def sweep_spawn(http: Http, adapter, known: set[str]) -> Sweep:
    return _sitemap_sweep(http, "spawn", "https://www.spawn.co/sitemap.xml", SPAWN_GAME)


def sweep_remix(http: Http, adapter, known: set[str]) -> Sweep:
    return _sitemap_sweep(http, "remix", "https://remix.gg/sitemap.xml", REMIX_GAME, index_filter="/sitemaps/games-")


def sweep_wavedash(http: Http, adapter, known: set[str]) -> Sweep:
    return _sitemap_sweep(http, "wavedash", "https://wavedash.com/sitemap/games.xml", WAVEDASH_GAME)


def sweep_rosebud(http: Http, adapter, known: set[str]) -> Sweep:
    out = Sweep("rosebud")
    for page in range(1, 101):  # the API serves at most 100 pages of 100
        data = http.get_json(adapter._list_url("-created", page, 100)) or {}
        rows = data.get("results") or []
        fresh = 0
        for obj in rows:
            url = canon(adapter._game_url(obj))
            fresh += url not in known
            out.items[url] = None
        if not data.get("next") or not rows or (known and fresh == 0):
            break
    return out


def sweep_playgama(http: Http, adapter, known: set[str]) -> Sweep:
    out = Sweep("playgama")
    for ref, _ in adapter.discover(["new"], 100):
        out.items[canon(ref.url)] = None
    return out


def sweep_playgama_ai(http: Http, adapter, known: set[str]) -> Sweep:
    data = http.get_json("https://playgama.com/ai-game-index/data.json") or {}
    return Sweep("playgama_ai", total=(data.get("totals") or {}).get("games"))


SWEEPS = {
    "tesana": sweep_tesana, "bitmagic": sweep_bitmagic, "spawn": sweep_spawn, "remix": sweep_remix,
    "wavedash": sweep_wavedash, "rosebud": sweep_rosebud, "playgama": sweep_playgama, "playgama_ai": sweep_playgama_ai,
}


# ------------------------------------------------------------------ publish-date lookups (backfill)
def lookup_date(source: str, adapter, url: str) -> datetime | None:
    if source == "wavedash":
        from tracker.sources.wavedash import FN_GAME, _ms_to_dt
        slug = url.rsplit("/", 1)[-1]
        full = adapter._q(FN_GAME, {"slug": slug})
        return _ms_to_dt(full.get("firstPublishedAt")) if isinstance(full, dict) else None
    ext = url.rsplit("/", 1)[-1] if source == "remix" else url
    for snap in adapter.fetch([GameRef(source=source, external_id=ext, url=url)]):
        return snap.published_at
    return None


# ------------------------------------------------------------------ database
def known_urls(conn, source: str) -> set[str]:
    return {r["url"] for r in conn.execute("select url from releases where source = %s", (source,)).fetchall()}


def store_sweep(conn, sweep: Sweep, known: set[str], now: datetime) -> dict:
    """Insert new URLs (baseline on a platform's first sweep), fill missing publish dates, record catalogue size."""
    first = not known
    new = [(sweep.source, u, pub, now, first) for u, pub in sweep.items.items() if u not in known]
    dated = [(pub, sweep.source, u) for u, pub in sweep.items.items() if u in known and pub is not None]
    with conn.cursor() as cur:
        if new:
            cur.executemany(
                "insert into releases (source, url, published_at, first_seen, baseline) values (%s, %s, %s, %s, %s) on conflict do nothing", new)
        if dated:
            cur.executemany("update releases set published_at = %s where source = %s and url = %s and published_at is null", dated)
        if sweep.total is not None:
            cur.execute(
                "insert into catalog_daily (source, day, games_total) values (%s, %s, %s) "
                "on conflict (source, day) do update set games_total = excluded.games_total",
                (sweep.source, now.date(), int(sweep.total)))
    conn.commit()
    return {"seen": len(sweep.items), "new": 0 if first else len(new), "baseline": len(new) if first else 0, "total": sweep.total}


def fill_dates_from_games(conn) -> int:
    """Free backfill: games the scrape already tracks carry the platform's publish date."""
    cur = conn.execute(
        """update releases r set published_at = g.published_at
             from games g
            where r.published_at is null and g.source = r.source and g.published_at is not null
              and rtrim(g.url, '/') = r.url""")
    conn.commit()
    return cur.rowcount


def backfill_dates(conn, source: str, adapter, cap: int) -> int:
    rows = conn.execute(
        """select url from releases where source = %s and published_at is null and not date_checked
            order by baseline, first_seen desc limit %s""", (source, cap)).fetchall()
    done = 0
    for r in rows:
        try:
            pub = lookup_date(source, adapter, r["url"])
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — one bad page must not stop the backfill
            log.warning("%s: date lookup failed for %s: %s", source, r["url"], e)
            pub = None
        conn.execute("update releases set published_at = coalesce(%s, published_at), date_checked = true where source = %s and url = %s",
                     (pub, source, r["url"]))
        done += 1
        if done % 25 == 0:
            conn.commit()
    conn.commit()
    return done


def release_counts(conn, period: str, since, sources: list[str]) -> list[dict]:
    assert period in ("week", "month")
    return conn.execute(RELEASE_COUNTS_SQL, {"period": period, "since": since, "sources": list(sources),
                                             "from_games": list(FROM_GAMES), "net": list(NET_FROM_TOTALS)}).fetchall()


# ------------------------------------------------------------------ run
def cmd_run(args) -> int:
    sources = load_sources()
    names = [args.source] if args.source else [n for n in SWEEPS if sources.get(n, {}).get("enabled")]
    budget = TimeBudget(float(os.environ.get("RUN_TIME_BUDGET_SECONDS", "1500")))
    conn = None if args.dry_run else db.connect()
    now = _utcnow()
    report, adapters = {}, {}
    try:
        for name in names:
            cfg = sources[name]
            http = Http(name, rate_limit_rps=float(cfg["rate_limit_rps"]), budget=budget)
            adapter = adapter_class(cfg)(http, cfg)
            adapters[name] = (http, adapter)
            known = set() if conn is None else known_urls(conn, name)
            try:
                sweep = SWEEPS[name](http, adapter, known)
            except BudgetExceeded:
                report[name] = {"status": "budget"}
                continue
            except Exception as e:  # noqa: BLE001 — one platform failing must not stop the others
                log.exception("%s: sweep failed", name)
                report[name] = {"status": "failed", "error": str(e)[:200]}
                continue
            report[name] = {"status": "ok", **(store_sweep(conn, sweep, known, now) if conn else {"seen": len(sweep.items), "total": sweep.total})}
            log.info("%s: %s", name, report[name])
        if conn:
            report["dated_from_tracked_games"] = fill_dates_from_games(conn)
            for name, cap in DATE_LOOKUP_CAP.items():
                if name not in adapters or report.get(name, {}).get("status") != "ok":
                    continue
                try:
                    report[name]["dates_looked_up"] = backfill_dates(conn, name, adapters[name][1], cap)
                except BudgetExceeded:
                    report[name]["dates_looked_up"] = "budget"
    finally:
        for http, _ in adapters.values():
            http.close()
        if conn:
            conn.close()
    print(json.dumps({"run_ts": now.isoformat(), "report": report}, indent=2, default=str))
    return 1 if any(v.get("status") == "failed" for v in report.values() if isinstance(v, dict)) else 0


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="python -m tracker.releases")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--source")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
