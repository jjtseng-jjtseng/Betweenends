"""Central configuration for the Between Ends data pipeline."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
# The public site (www.betweenends.com) is a static AngularJS shell. All of the
# results data is served from this JSON API, discovered by reading the site's
# own page scripts:
#   GET /tournaments/            -> list of every tournament
#   GET /tournaments/<id>        -> tournament + its list of events
#   GET /events/<id>             -> event metadata, archers ("rps"), groups ("cgs")
#   GET /events/<id>/scores      -> {"ars": {<archer_id>: "<arrow string>"}}
API_BASE = "https://resultsapi.herokuapp.com"

# Identify ourselves honestly. Collection is done with the site owner's permission.
USER_AGENT = "BetweenEnds-data-collection/1.0 (owner-authorized research; github.com/jjtseng-jjtseng/betweenends)"

# Be gentle: this is a small independently-run service on a Heroku dyno.
# Measured during the original collection: the server effectively serialises requests.
# 2 workers gave ~0.46 req/s with ~6% HTTP 503 (30 s router timeouts, which also leave the
# dyno computing abandoned responses); 1 worker gave ~1.3 req/s with zero errors. So the
# polite default is also the fastest one: a single worker.
DEFAULT_MIN_INTERVAL_S = 0.50   # global minimum gap between request *starts*
DEFAULT_WORKERS = 1
REQUEST_TIMEOUT_S = 180
MAX_RETRIES = 6
BACKOFF_BASE_S = 2.0            # 2, 4, 8, 16, 32, 64 ...

# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
# "Last N years" window = every competition that has already happened.
RECENT_YEARS = 5
# "Decade" window for marquee events (nationals / USAT / trials). Between Ends
# itself only starts in Dec-2017, so earlier marquee events need other sources.
DECADE_YEARS = 10


def add_years(d: dt.date, years: int) -> dt.date:
    """Date `years` before/after d, clamping Feb-29."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year + years, day=28)
