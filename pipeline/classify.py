"""Classify Between Ends tournaments from their names.

The API has no "type" field for tournaments, so the category is derived from the
name. We first compute independent boolean *tags* (joad, collegiate, combined...),
then pick one *primary category* by explicit precedence. Categories are the unit
the user's scope ("indoor + outdoor nationals, USAT") is expressed in.

Primary categories
------------------
USA Archery national championship family (``is_national_championship``):
    indoor_nationals, joad_indoor_nationals,
    target_nationals (incl. the U.S. Open), joad_target_nationals,
    collegiate_target_nationals, collegiate_3d_nationals,
    field_nationals, 3d_nationals, oas_nationals
Other marquee:
    usat_series        USA Archery Team series (USAT #1-#4 + Series Final)
    team_trials        Olympic / Paralympic / World / Pan-Am / Youth team trials
    s3da_nationals     S3DA (Scholastic 3D Archery) national championships
Everything else:
    nast, s3da_other, virtual_tournament, collegiate_regional,
    state_championship, selection_event, other_national, other
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NATIONAL_CHAMPIONSHIP_CATEGORIES = frozenset(
    {
        "indoor_nationals",
        "joad_indoor_nationals",
        "target_nationals",
        "joad_target_nationals",
        "collegiate_target_nationals",
        "collegiate_3d_nationals",
        "field_nationals",
        "3d_nationals",
        "oas_nationals",
    }
)
MARQUEE_CATEGORIES = NATIONAL_CHAMPIONSHIP_CATEGORIES | {"usat_series", "team_trials", "s3da_nationals"}

# Indoor / outdoor grouping used by the "(indoor and outdoor) nationals" request.
INDOOR_CATEGORIES = frozenset({"indoor_nationals", "joad_indoor_nationals"})
OUTDOOR_CATEGORIES = frozenset(
    {
        "target_nationals",
        "joad_target_nationals",
        "collegiate_target_nationals",
        "collegiate_3d_nationals",
        "field_nationals",
        "3d_nationals",
        "oas_nationals",
    }
)


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


_TAGS = {
    "joad": _rx(r"\bjoad\b"),
    "collegiate": _rx(r"collegiate"),
    "indoor": _rx(r"\bindoor\b"),
    "combined_results": _rx(r"combined results"),
    "collegiate_results": _rx(r"collegiate results"),
    "final": _rx(r"(indoor nationals|national indoor championships|nationals) final\b|championships final\b|series final\b"),
    "trials": _rx(r"\btrials?\b"),
    "us_open": _rx(r"\bu\.?\s?s\.?\s+open\b"),
    "state": _rx(r"\bstate\b"),
    "s3da": _rx(r"\bs3da\b"),
    "nast": _rx(r"national archery scholarship tour|\bnast\b"),
    "oas": _rx(r"\boas\b"),
    "three_d": _rx(r"\b3-?d\b"),
    "field": _rx(r"\bfield\b"),
    "target": _rx(r"\btarget\b"),
    "selection": _rx(r"\bselection\b"),
    "national_word": _rx(r"\bnational(s)?\b"),
    "para": _rx(r"\bpara(lympic)?\b"),
    "virtual": _rx(r"\bvirtual\b"),
    "regional": _rx(r"\bregional(s)?\b"),
}

# National-championship phrases, by sub-discipline.
_INDOOR_NAT = _rx(r"indoor nationals|national indoor championships?")
_TARGET_NAT = _rx(r"target nationals|national target championships?")
_COLL_TARGET_NAT = _rx(r"collegiate target nationals|national outdoor collegiate championships?")
_COLL_3D_NAT = _rx(r"3d collegiate nationals|collegiate 3d nationals")
_FIELD_NAT = _rx(r"field nationals|national field championships?")
_3D_NAT = _rx(r"\b3d nationals")
_OAS_NAT = _rx(r"\boas\b.*national")


@dataclass
class Classification:
    category: str
    tags: list[str] = field(default_factory=list)

    @property
    def is_national_championship(self) -> bool:
        return self.category in NATIONAL_CHAMPIONSHIP_CATEGORIES

    @property
    def is_marquee(self) -> bool:
        return self.category in MARQUEE_CATEGORIES

    @property
    def is_indoor_national(self) -> bool:
        return self.category in INDOOR_CATEGORIES

    @property
    def is_outdoor_national(self) -> bool:
        return self.category in OUTDOOR_CATEGORIES


def classify_name(name: str | None) -> Classification:
    n = (name or "").strip()
    tags = [t for t, rx in _TAGS.items() if rx.search(n)]
    has = set(tags).__contains__

    # Order matters: most specific / least ambiguous first.
    if re.search(r"\bUSAT\b", n):
        cat = "usat_series"
    elif has("nast"):
        cat = "nast"  # contains the word "National" but is a local-shoot tour
    elif has("s3da"):
        cat = "s3da_nationals" if has("national_word") and not re.search(r"youth tour", n, re.I) else "s3da_other"
    elif _OAS_NAT.search(n):
        cat = "oas_nationals"
    elif _COLL_3D_NAT.search(n):
        cat = "collegiate_3d_nationals"
    elif _COLL_TARGET_NAT.search(n):
        cat = "collegiate_target_nationals"
    elif _INDOOR_NAT.search(n):
        cat = "joad_indoor_nationals" if has("joad") else "indoor_nationals"
    elif _TARGET_NAT.search(n):
        cat = "joad_target_nationals" if has("joad") else "target_nationals"
    elif _FIELD_NAT.search(n):
        cat = "field_nationals"
    elif _3D_NAT.search(n):
        cat = "3d_nationals"
    elif has("selection"):
        cat = "selection_event"
    elif has("trials"):
        cat = "team_trials"
    elif has("virtual"):
        cat = "virtual_tournament"  # USA Archery quarterly virtual tournaments
    elif has("collegiate") and has("regional"):
        cat = "collegiate_regional"
    elif has("state"):
        cat = "state_championship"
    elif has("national_word"):
        cat = "other_national"
    else:
        cat = "other"

    # Secondary descriptors worth surfacing alongside the category.
    if cat == "usat_series" and has("trials"):
        tags.append("also_trials")
    return Classification(cat, sorted(set(tags)))


# Convenience wrapper used by collect/normalize ------------------------------
def classify_tournament(t: dict) -> Classification:
    return classify_name(t.get("tournament_name"))
