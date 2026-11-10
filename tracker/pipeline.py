"""CLI: python -m tracker.pipeline run [--source X] [--dry-run] [--limit N] [--force]
          python -m tracker.pipeline record --source X [--limit N]

One run = one run_ts shared by every platform; each platform in its own try/except.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from tracker import db
from tracker.http import Blocked, BudgetExceeded, Http, RobotsDisallowed, TimeBudget
from tracker.models import GameRef, GameSnapshot, ListPlacement
from tracker.registry import ROOT, adapter_class, load_sources

# List positions are stored for the top 100 only. Deeper lists (Tesana's whole catalogue, Rosebud's top 1,000)
# exist to find games to poll; a game's position there is just its plays rank, and storing it was ~45% of the
# list_placements growth. Membership for the watchlist is kept in games.last_listed instead.
PLACEMENT_MAX_POSITION = 100

log = logging.getLogger("tracker")
FIXTURES = ROOT / "tests" / "fixtures"
BATCH_SIZE = 250  # snapshots per DB write batch


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class DryConn:
    """Stands in for a DB connection under --dry-run: counts what would be written."""

    def __init__(self):
        self.writes = {"games": 0, "creators": 0, "placements": 0, "snapshots": 0}
        self._ids = {}

    def _id(self, key):
        return self._ids.setdefault(key, len(self._ids) + 1)


def run_source(conn, name: str, cfg: dict, run_ts: datetime, budget: TimeBudget, *, limit: int | None, dry: bool, ccu: bool = False) -> dict:
    """One platform. ccu=True is the lightweight concurrent-players poll: only the lists in cfg["ccu_lists"],
    no watchlist carry-over, no creator profiles, no runs row (so Health's row-count medians stay meaningful)."""
    cls = adapter_class(cfg)
    started = utcnow()
    t0 = time.monotonic()
    counts = {"discovered": 0, "snapshotted": 0, "gone": 0}
    errors: list[str] = []
    status = "ok"
    http = Http(name, rate_limit_rps=float(cfg["rate_limit_rps"]), budget=budget)
    adapter = cls(http, cfg)
    try:
        if not dry:
            db.upsert_source(conn, cfg, cls.display_name, cls.base_url, cls.segment)

        # 1. discover ------------------------------------------------------
        discovered: dict[str, GameRef] = {}
        pending: list[ListPlacement] = []
        overrides = {k: int(v) for k, v in (cfg.get("list_top_n") or {}).items()}  # per-list depth, e.g. rosebud top: 1000
        groups = [([l for l in cfg["lists"] if l not in overrides], int(cfg["top_n"]))]
        groups += [([l], n) for l, n in overrides.items() if l in cfg["lists"]]
        for lists, depth in groups:
            if not lists:
                continue
            for ref, placement in adapter.discover(list(lists), depth):
                discovered.setdefault(ref.external_id, ref)
                pending.append(placement)
        counts["discovered"] = len(discovered)
        if not dry:
            ids = db.upsert_game_refs_bulk(conn, name, [(r.external_id, r.url) for r in discovered.values()], run_ts)
            db.insert_placements(conn, [(p, ids[p.external_id]) for p in pending
                                        if p.external_id in ids and p.position <= PLACEMENT_MAX_POSITION], run_ts)
            conn.commit()
        else:
            conn.writes["placements"] += len(pending)
        placements = pending
        log.info("%s: discovered %d games across %d placements", name, len(discovered), len(placements))

        # 2. watchlist ----------------------------------------------------
        refs = dict(discovered)
        if not dry and not ccu:
            for row in db.watchlist(conn, name, int(cfg["stale_after_days"]), int(cfg["max_watchlist"])):
                refs.setdefault(row["external_id"], GameRef(source=name, external_id=row["external_id"], url=row["url"]))
        ref_list = list(refs.values())
        if limit:
            ref_list = ref_list[:limit]

        # 3. fetch --------------------------------------------------------
        batch: list[GameSnapshot] = []

        def flush() -> None:
            """Write a batch of snapshots in ~4 round trips (creators, games, ids, snapshots)."""
            if not batch:
                return
            if not dry:
                creators = {s.creator_external_id: (s.creator_name, s.creator_url) for s in batch if s.creator_external_id}
                cids = db.upsert_creators_bulk(conn, name, creators, run_ts)
                gids = db.upsert_games_bulk(conn, batch, cids, run_ts)
                w = db.insert_snapshots_bulk(conn, [(gids[s.external_id], s) for s in batch if s.status == "ok" and s.external_id in gids], run_ts)
                conn.commit()
                counts["stored"] = counts.get("stored", 0) + w["inserted"]
                counts["unchanged"] = counts.get("unchanged", 0) + w["folded"]
            else:
                conn.writes["snapshots"] += len(batch)
            counts["gone"] += sum(1 for s in batch if s.status == "gone")
            counts["snapshotted"] += sum(1 for s in batch if s.status == "ok")
            batch.clear()

        try:
            for snap in adapter.fetch(ref_list):
                batch.append(snap)
                if len(batch) >= BATCH_SIZE:
                    flush()
        finally:
            flush()  # keep whatever was fetched if the time budget or the platform cut the loop short

        # 4. creator profiles: first run after 00:00 UTC only --------------
        if not dry and not ccu and run_ts.hour < int(cfg["poll_every_hours"]):
            todo = db.creators_needing_profile(conn, name, run_ts.date())
            for row in todo:
                try:
                    prof = adapter.fetch_creator(row["external_id"])
                except (Blocked, RobotsDisallowed) as e:
                    log.info("%s: creator profiles unavailable (%s)", name, e)
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"creator {row['external_id']}: {e}")
                    continue
                if prof:
                    db.update_creator_profile(conn, row["id"], prof, run_ts)
            conn.commit()
            stale = db.mark_stale(conn, name, int(cfg["stale_after_days"]))
            if stale:
                log.info("%s: marked %d games stale", name, stale)
            conn.commit()

        if counts["snapshotted"] == 0:
            status = "zero_rows"
    except BudgetExceeded as e:
        errors.append(str(e))
        status = "partial"
    except (Blocked, RobotsDisallowed) as e:
        errors.append(f"blocked: {e}")
        status = "failed"
        log.error("%s: stopped — %s. Not retrying, not working around it.", name, e)
    except Exception as e:  # noqa: BLE001
        errors.append(repr(e))
        status = "failed"
        log.exception("%s: failed", name)
        if not dry:
            conn.rollback()
    finally:
        seconds = time.monotonic() - t0
        http.close()
        if not dry and not ccu:
            try:
                db.log_run(conn, name, run_ts, started, status, counts, errors, seconds, http.requests_made)
                conn.commit()
            except Exception:  # noqa: BLE001
                log.exception("%s: could not log run", name)
                conn.rollback()
    log.info("%s: %s in %.1fs — %s (%d requests)", name, status, seconds, counts, http.requests_made)
    return {"source": name, "status": status, "seconds": round(seconds, 1), "requests": http.requests_made, **counts, "errors": errors}


def order_by_staleness(names: list[str], last_ok: dict[str, datetime | None]) -> list[str]:
    """Platforms whose last successful run is oldest go first (never-run first of all), so when the shared
    time budget runs out it is always the most recently covered platforms that wait, never the same ones."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(names, key=lambda n: (last_ok.get(n) or floor, names.index(n)))


def cmd_run(args) -> int:
    sources = load_sources()
    ccu = getattr(args, "mode", "full") == "ccu"
    if ccu:  # lightweight concurrent-players poll: only platforms that publish live player counts
        sources = {
            k: {**v, "lists": list(v["ccu_lists"]), "list_top_n": {}, "top_n": int(v.get("ccu_top_n", v["top_n"]))}
            for k, v in sources.items() if v.get("ccu_lists")
        }
    selected = {k: v for k, v in sources.items() if v["enabled"] or args.source}
    if getattr(args, "only", None):  # one platform, but unlike --source keep the enabled + freshness checks (matrix jobs)
        if args.only not in sources:
            print(f"unknown source {args.only}; known: {', '.join(sources)}", file=sys.stderr)
            return 2
        selected = {k: v for k, v in selected.items() if k == args.only}
        if not selected:
            print(f"{args.only} is disabled; nothing to do")
            return 0
    if args.source:
        if args.source not in sources:
            print(f"unknown source {args.source}; known: {', '.join(sources)}", file=sys.stderr)
            return 2
        selected = {args.source: sources[args.source]}
    if not selected:
        print("no enabled sources", file=sys.stderr)
        return 2

    run_ts = utcnow()
    budget = TimeBudget(float(os.environ.get("RUN_TIME_BUDGET_SECONDS", "1200")))
    dry = args.dry_run
    conn = DryConn() if dry else db.connect()
    results = []
    try:
        if not dry and not ccu and len(selected) > 1:
            order = order_by_staleness(list(selected), {n: db.last_successful_run(conn, n) for n in selected})
            selected = {n: selected[n] for n in order}
            log.info("run order (stalest first): %s", ", ".join(order))
        for name, cfg in selected.items():
            if cfg.get("needs_browser") and not args.allow_browser:
                log.warning("%s: needs_browser is set but browser adapters are not installed in this run; skipping", name)
                continue
            if not dry and not args.force and not args.source and not ccu:
                last = db.last_successful_run(conn, name)
                if last and run_ts - last < timedelta(hours=float(cfg["poll_every_hours"])) - timedelta(minutes=10):
                    log.info("%s: last run %s is fresher than poll_every_hours=%s; skipping", name, last, cfg["poll_every_hours"])
                    continue
            if budget.remaining() <= 0:
                log.warning("time budget exhausted before %s; skipping remaining platforms", name)
                results.append({"source": name, "status": "skipped", "errors": ["budget"]})
                continue
            results.append(run_source(conn, name, cfg, run_ts, budget, limit=args.limit, dry=dry, ccu=ccu))

        if not dry:
            today = run_ts.date()
            db.derive(conn, [today - timedelta(days=1), today], list(selected))
            if not ccu and (run_ts.hour < 2 or args.force):
                last = db.get_maintenance(conn, "rollup_day")
                if last != str(today):
                    n = db.rollup(conn)
                    db.set_maintenance(conn, "rollup_day", str(today))
                    log.info("rollup removed %d snapshot rows", n)
        else:
            print(json.dumps({"dry_run": True, "run_ts": run_ts.isoformat(), "would_write": conn.writes, "results": results}, indent=2, default=str))
    finally:
        if not dry:
            conn.close()

    bad = [r for r in results if r["status"] in ("failed", "zero_rows")]
    partial = [r for r in results if r["status"] == "partial"]
    print(json.dumps({"run_ts": run_ts.isoformat(), "results": results}, indent=2, default=str))
    if bad:
        print(f"FAILED sources: {', '.join(r['source'] for r in bad)}", file=sys.stderr)
        return 1
    if partial:
        print(f"PARTIAL (time budget): {', '.join(r['source'] for r in partial)}", file=sys.stderr)
    return 0


def cmd_record(args) -> int:
    """Save fresh fixtures for one adapter: the raw responses its discover()/fetch() use."""
    sources = load_sources()
    cfg = sources[args.source]
    cls = adapter_class(cfg)
    http = Http(args.source, rate_limit_rps=float(cfg["rate_limit_rps"]))
    out = FIXTURES / args.source
    out.mkdir(parents=True, exist_ok=True)
    adapter = cls(http, cfg)
    if not hasattr(adapter, "record_fixtures"):
        print(f"{args.source}: adapter has no record_fixtures(out_dir, limit) helper", file=sys.stderr)
        return 2
    written = adapter.record_fixtures(out, args.limit or 20)
    http.close()
    for p in written:
        print(p)
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise; counts are in the run summary
    ap = argparse.ArgumentParser(prog="python -m tracker.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--source", help="run exactly this platform, ignoring enabled and freshness checks")
    r.add_argument("--only", help="run only this platform, keeping the enabled and freshness checks (used by the per-platform jobs)")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--limit", type=int)
    r.add_argument("--force", action="store_true")
    r.add_argument("--mode", choices=["full", "ccu"], default="full", help="ccu = players-online-only poll of the ccu_lists (fast; runs every 30 min)")
    r.add_argument("--allow-browser", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run)
    rec = sub.add_parser("record")
    rec.add_argument("--source", required=True)
    rec.add_argument("--limit", type=int)
    rec.set_defaults(fn=cmd_record)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
