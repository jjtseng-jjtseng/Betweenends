"""Which tournaments are in scope for collection.

The request, restated precisely:

1. **Every competition in the last 5 years** that has already happened
   (anything overlapping ``[as_of - 5y, as_of]``). Nothing is filtered by type.
2. **Marquee competitions over the past decade** - USA Archery nationals (indoor
   and outdoor), the USAT series, and team/Olympic trials - over
   ``[as_of - 10y, as_of]``. Between Ends only has data from Dec-2017, so for
   marquee events older than that, see ``docs/COVERAGE.md``.

Tournaments scheduled after ``as_of`` are catalogued but never fetched: they have
no results yet.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import config
from .classify import classify_tournament


def _d(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s[:10]) if s else None


def decade_start(as_of: dt.date) -> dt.date:
    """Jan 1 of (as_of.year - 10): "the past decade" read as the 2016-2026 seasons,
    so a whole calendar year is never cut in half. A superset of the strict
    10-years-to-the-day reading; filter on dates if you need the strict one."""
    return dt.date(as_of.year - config.DECADE_YEARS, 1, 1)


@dataclass(frozen=True)
class ScopeDecision:
    tournament_id: int
    status: str            # completed | in_progress | upcoming
    in_recent_window: bool
    in_decade_marquee: bool

    @property
    def in_scope(self) -> bool:
        return self.status != "upcoming" and (self.in_recent_window or self.in_decade_marquee)

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.in_recent_window:
            out.append("last_5_years")
        if self.in_decade_marquee:
            out.append("decade_marquee")
        return out


def decide(t: dict, as_of: dt.date) -> ScopeDecision:
    start = _d(t.get("start_date"))
    end = _d(t.get("end_date")) or start
    if start is None:
        return ScopeDecision(t["id"], "upcoming", False, False)

    if start > as_of:
        status = "upcoming"
    elif end >= as_of:
        status = "in_progress"
    else:
        status = "completed"

    recent_cutoff = config.add_years(as_of, -config.RECENT_YEARS)
    decade_cutoff = decade_start(as_of)

    in_recent = start <= as_of and end >= recent_cutoff
    marquee = classify_tournament(t).is_marquee
    in_decade = marquee and start <= as_of and end >= decade_cutoff
    return ScopeDecision(t["id"], status, in_recent, in_decade)


def select(index: list[dict], as_of: dt.date) -> list[dict]:
    """Return the in-scope tournaments (index rows), oldest first."""
    rows = [t for t in index if decide(t, as_of).in_scope]
    return sorted(rows, key=lambda t: (t["start_date"], t["id"]))
