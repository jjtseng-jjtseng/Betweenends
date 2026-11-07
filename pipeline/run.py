"""One-command rebuild of the whole dataset.

    python -m pipeline.run                      # everything, as of today
    python -m pipeline.run --as-of 2026-11-07   # reproduce a given snapshot window
    python -m pipeline.run --skip-collect       # re-derive tables from the cached raw data (offline)

Stages, in order:
    1. collect   index -> tournaments -> events -> scores        (network, resumable, polite)
    2. legacy    fetch the 2016-17 USA Archery PDFs               (network, ~25 files)
    3. normalize raw JSON -> tidy tables                          (offline)
    4. legacy    parse the PDFs, link archers                     (offline)
    5. archers   profiles, seasons, trends, marquee results       (offline)
    6. validate  integrity + external spot checks                 (offline; non-zero exit on FAIL)
    7. report    docs/COVERAGE.md + docs/TRENDS.md                (offline, computed from the tables)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import archers, collect, config, legacy, normalize, report, validate


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    p.add_argument("--skip-collect", action="store_true", help="do not touch the network for Between Ends data")
    p.add_argument("--skip-legacy-fetch", action="store_true", help="do not download the USA Archery PDFs")
    p.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS)
    p.add_argument("--min-interval", type=float, default=config.DEFAULT_MIN_INTERVAL_S)
    a = p.parse_args(argv)
    as_of = a.as_of.isoformat()

    if not a.skip_collect:
        rc = collect.main(["all", "--as-of", as_of, "--workers", str(a.workers), "--min-interval", str(a.min_interval)])
        if rc:
            print("collect finished with errors (see data/raw/_errors.json); continuing with what was collected")
    if not a.skip_legacy_fetch:
        legacy.fetch()
    normalize.run(a.as_of)
    legacy.build_tables()
    archers.build(config.PROCESSED_DIR, a.as_of)
    rc = validate.main(["--as-of", as_of])
    report.build(config.PROCESSED_DIR, config.REPO_ROOT / "docs", a.as_of)
    return rc


if __name__ == "__main__":
    sys.exit(main())
