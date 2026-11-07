"""Collect raw Between Ends API responses into data/raw/.

Usage (all stages are resumable; anything already cached is skipped):

    python -m pipeline.collect index                  # tournament list
    python -m pipeline.collect tournaments            # tournament -> events lists
    python -m pipeline.collect events                 # event metadata + archers
    python -m pipeline.collect scores                 # arrow-by-arrow scores
    python -m pipeline.collect all                    # all of the above
    python -m pipeline.collect status                 # what is cached vs expected

Common options:
    --as-of YYYY-MM-DD   "today" for the scope windows (default: today)
    --workers N          concurrent requests (default 3; the global rate limit
                         still applies, so more workers will not hammer the API)
    --min-interval S     minimum seconds between request starts (default 0.30)
    --limit N            only process the first N items of a stage (smoke tests)
    --refresh            ignore the cache and re-download
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
from typing import Callable, Iterable

from . import config
from .classify import classify_tournament
from .client import ApiClient, NotFound
from .scope import decide, select


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _priority(t: dict) -> tuple:
    """Marquee first (most valuable if a run is cut short), then most recent."""
    c = classify_tournament(t)
    return (0 if c.is_marquee else 1, _neg_date(t["start_date"]), t["id"])


def _neg_date(s: str) -> int:
    return -dt.date.fromisoformat(s[:10]).toordinal()


def run_pool(items: list, fn: Callable, workers: int, label: str, errors: list) -> dict:
    """Run fn(item) over items with progress output; collect errors."""
    total = len(items)
    done = fetched = cached = 0
    t0 = time.monotonic()
    if not total:
        print(f"[{label}] nothing to do")
        return {"total": 0, "network": 0, "cache": 0, "errors": 0}

    def progress(force: bool = False) -> None:
        if force or done % 50 == 0:
            el = time.monotonic() - t0
            rate = fetched / el if el > 0 and fetched else 0.0
            remaining = total - done
            eta = f"{remaining / rate / 60:5.1f}min" if rate else "   ?   "
            print(
                f"[{label}] {done}/{total}  net={fetched} cache={cached} err={len(errors)}"
                f"  {rate:4.1f} req/s  eta {eta}",
                flush=True,
            )

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn, it): it for it in items}
        try:
            for fut in cf.as_completed(futs):
                it = futs[fut]
                try:
                    res = fut.result()
                    if res is True:
                        cached += 1
                    else:
                        fetched += 1
                except NotFound:
                    errors.append({"label": label, "item": it, "error": "404"})
                except Exception as e:  # noqa: BLE001 - recorded, run continues
                    errors.append({"label": label, "item": it, "error": repr(e)[:300]})
                done += 1
                progress()
        except KeyboardInterrupt:
            print("\ninterrupted - cancelling pending work (cache is consistent)", file=sys.stderr)
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    progress(force=True)
    return {"total": total, "network": fetched, "cache": cached, "errors": len(errors)}


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def load_index(client: ApiClient) -> list[dict]:
    return client.tournaments_index().data


def stage_index(client: ApiClient, args) -> list[dict]:
    res = client.tournaments_index(refresh=args.refresh)
    print(f"[index] {len(res.data)} tournaments ({'cache' if res.from_cache else 'network'})")
    return res.data


def scoped_tournaments(client: ApiClient, as_of: dt.date) -> list[dict]:
    rows = select(load_index(client), as_of)
    return sorted(rows, key=_priority)


def stage_tournaments(client: ApiClient, args, errors: list) -> None:
    rows = scoped_tournaments(client, args.as_of)
    if args.limit:
        rows = rows[: args.limit]
    print(f"[tournaments] {len(rows)} in-scope tournaments (as of {args.as_of})")

    def work(t: dict):
        return client.tournament(t["id"], refresh=args.refresh).from_cache

    run_pool(rows, work, args.workers, "tournaments", errors)


def event_refs(client: ApiClient, as_of: dt.date, limit: int | None = None) -> list[dict]:
    """[{event_id, event_type, tournament_id}] for every in-scope tournament that is cached."""
    refs: list[dict] = []
    rows = scoped_tournaments(client, as_of)
    if limit:
        rows = rows[:limit]
    for t in rows:
        if not client.is_cached("tournaments", str(t["id"])):
            continue
        try:
            td = client.read_cached("tournaments", str(t["id"]))
        except NotFound:
            continue
        for e in td.get("events") or []:
            refs.append({"event_id": e["id"], "event_type": e.get("event_type"), "tournament_id": t["id"]})
    return refs


def stage_events(client: ApiClient, args, errors: list) -> None:
    refs = event_refs(client, args.as_of, args.limit)
    print(f"[events] {len(refs)} events across in-scope tournaments")

    def work(r: dict):
        return client.event(r["event_id"], refresh=args.refresh).from_cache

    run_pool(refs, work, args.workers, "events", errors)


def stage_scores(client: ApiClient, args, errors: list) -> None:
    refs = event_refs(client, args.as_of, args.limit)
    skip = set(filter(None, (args.skip_score_types or "").split(",")))
    if skip:
        refs = [r for r in refs if r["event_type"] not in skip]
    print(f"[scores] {len(refs)} events (skipping types: {sorted(skip) or 'none'})")

    def work(r: dict):
        return client.scores(r["event_id"], refresh=args.refresh).from_cache

    run_pool(refs, work, args.workers, "scores", errors)


def stage_status(client: ApiClient, args) -> None:
    rows = select(load_index(client), args.as_of)
    n_t = sum(client.is_cached("tournaments", str(t["id"])) for t in rows)
    refs = event_refs(client, args.as_of)
    n_e = sum(client.is_cached("events", str(r["event_id"])) for r in refs)
    n_s = sum(client.is_cached("scores", str(r["event_id"])) for r in refs)
    raw = sum(p.stat().st_size for p in client.raw_dir.rglob("*") if p.is_file())
    print(f"as of {args.as_of}")
    print(f"  in-scope tournaments : {len(rows):>6}   cached {n_t}")
    print(f"  events (known so far): {len(refs):>6}   cached events {n_e}, scores {n_s}")
    print(f"  raw cache on disk    : {raw / 1e6:,.1f} MB")


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline.collect", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=["index", "tournaments", "events", "scores", "all", "status"])
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    p.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS)
    p.add_argument("--min-interval", type=float, default=config.DEFAULT_MIN_INTERVAL_S)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh", action="store_true")
    p.add_argument(
        "--skip-score-types",
        default="CustomPointsEvent",
        help="comma list of event_type values to skip in the scores stage. CustomPointsEvent is skipped "
        "by default because /events/<id>/scores returns a payload identical to /events/<id> for it "
        "(verified), so fetching it twice is wasted load on a small service.",
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = ApiClient(min_interval=args.min_interval)
    errors: list = []
    t0 = time.monotonic()

    if args.stage == "status":
        stage_status(client, args)
        return 0

    stages = ["index", "tournaments", "events", "scores"] if args.stage == "all" else [args.stage]
    for s in stages:
        if s == "index":
            stage_index(client, args)
        elif s == "tournaments":
            stage_tournaments(client, args, errors)
        elif s == "events":
            stage_events(client, args, errors)
        elif s == "scores":
            stage_scores(client, args, errors)

    # Persist failures so they are visible and retried (they are never cached).
    err_path = config.RAW_DIR / "_errors.json"
    if errors:
        err_path.write_text(json.dumps(errors, indent=1))
    elif err_path.exists():
        err_path.unlink()

    print(
        f"done in {(time.monotonic() - t0) / 60:.1f} min | network={client.stats['network']} "
        f"cache={client.stats['cache']} retries={client.stats['retries']} 404s={client.stats['not_found']} "
        f"downloaded={client.stats['bytes'] / 1e6:,.1f} MB | errors={len(errors)}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
