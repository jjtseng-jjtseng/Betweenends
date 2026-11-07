"""Parse class/division group names ("Recurve Master 60+ Women") into structured fields.

Group names are free text typed by tournament organisers and their conventions
changed over time (2018 "Recurve Cadet Men" -> 2026 "Recurve U18 Men"). This is
a tolerant parser with explicit fallbacks. Anything it cannot place is reported
as ``division="Other"`` / ``age_class="Unknown"`` / ``gender="?"`` rather than
guessed; ``validate`` lists the most common unparsed names so the rules can be
extended.

Canonical vocabulary
--------------------
division   Recurve, Compound, Barebow, Fixed Pins, Traditional Recurve, Longbow,
           Compound Fingers, Crossbow, Bowhunter, Para Recurve, Para Compound,
           Para W1, Para V.I., Unlimited, Open, Other
age_class  Senior, U21, U18, U15, U13 (and other Uxx), 50+, 60+, 70+,
           Collegiate, Youth, Young Adult, Jr Eagle / Eagle (S3DA), Elementary / Middle School /
           High School (school leagues), Unknown
           Legacy names are mapped onto the WA-style codes. The mapping is
           confirmed by the data itself, where 2025/26 names read
           "U21 (Junior)", "U18 (Cadet)", "U15 (Cub)", "U13 (Bowman)":
               Junior->U21  Cadet->U18  Cub->U15  Bowman(men)->U13  Master(s)->50+
gender     M, W, X (mixed / combined), ? (unknown)
is_guest   "(Guest)" groups: archers not eligible for titles/records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: most specific first.
_DIVISIONS: list[tuple[str, str]] = [
    (r"\bw1\b", "Para W1"),
    (r"\bv\.?\s?i\.?(?=\s|$)|visually", "Para V.I."),
    (r"para\s+compound|compound[^()]*\(para\)", "Para Compound"),      # "Compound Open (Para)" (2018) / "Para Compound" (2026)
    (r"para\s+recurve|recurve[^()]*\(para\)", "Para Recurve"),         # a plain "Compound Open" is NOT para
    (r"traditional\s+recurve", "Traditional Recurve"),
    (r"compound\s+fingers?", "Compound Fingers"),
    (r"fixed\s+pins?|^pins?\b|\bpins?\s+(youth|eagle|jr|young)", "Fixed Pins"),   # S3DA "Pins Youth Men"
    (r"compound", "Compound"),
    (r"recurve|olympic", "Recurve"),
    (r"bare\s?bow", "Barebow"),
    (r"long\s?bow", "Longbow"),
    (r"traditional|instinctive", "Traditional Recurve"),
    (r"crossbow", "Crossbow"),
    (r"bowhunter|hunter", "Bowhunter"),
    (r"unlimited", "Unlimited"),
    (r"high school|middle school|elementary", "Unspecified"),               # school-league groups carry no bow type
    (r"\bopen\b", "Open"),
]

_LEGACY_AGE = {"junior": "U21", "jr": "U21", "cadet": "U18", "cub": "U15", "bowman": "U13", "bowmen": "U13"}

_GENDER = [
    (r"men\s*/\s*women|women\s*/\s*men|\bmixed\b|\bcombined\b", "X"),
    (r"\b(women|woman|female|ladies|girls?)\b", "W"),
    (r"\b(men|man|male|boys?)\b", "M"),
]


@dataclass(frozen=True)
class Group:
    raw: str
    division: str
    age_class: str
    gender: str          # M | W | X | ?
    is_collegiate: bool
    is_para: bool
    is_guest: bool
    is_team: bool

    @property
    def is_parsed(self) -> bool:
        return self.division != "Other" and self.gender != "?"


def _age_class(s: str) -> str:
    if re.search(r"collegiate|\bncaa\b|\bnaia\b|\bcollege\b|\buniversity\b", s):
        return "Collegiate"
    m = re.search(r"\b(70|60|50)\s?\+", s) or re.search(r"master\s*(70|60|50)", s)
    if m:
        return f"{m.group(1)}+"
    if re.search(r"\bmasters?\b", s):
        return "50+"
    if re.search(r"jr\.?\s*eagle|junior\s+eagle", s):
        return "Jr Eagle"                                  # S3DA, not the WA "Junior" (U21)
    if re.search(r"\beagle\b", s):
        return "Eagle"
    if "young adult" in s:
        return "Young Adult"
    if "high school" in s:
        return "High School"
    if "middle school" in s:
        return "Middle School"
    if "elementary" in s:
        return "Elementary"
    m = re.search(r"\bu\s?-?(\d{1,2})\b|\bunder\s?(\d{1,2})\b", s)
    if m:
        return f"U{m.group(1) or m.group(2)}"
    for word, code in _LEGACY_AGE.items():
        if re.search(rf"\b{word}s?\b\.?", s):
            return code
    if re.search(r"\bseniors?\b|\badult\b|\bopen\b", s):
        return "Senior"
    if re.search(r"\byouth\b", s):
        return "Youth"
    return "Unknown"


def parse_group(name: str | None) -> Group:
    raw = (name or "").strip()
    s = raw.lower()

    division = "Other"
    for rx, label in _DIVISIONS:
        if re.search(rx, s):
            division = label
            break

    age = _age_class(s)
    # A plain "Recurve Men" (no class word) is the open/senior class.
    if age == "Unknown" and division != "Other":
        age = "Senior"

    gender = "?"
    for rx, label in _GENDER:
        if re.search(rx, s):
            gender = label
            break

    return Group(
        raw=raw,
        division=division,
        age_class=age,
        gender=gender,
        is_collegiate=age == "Collegiate" or "collegiate" in s,
        is_para=division.startswith("Para"),
        is_guest=bool(re.search(r"\(guest\)|\bguest\b", s)),
        is_team=bool(re.search(r"\bteams?\b|relay", s)),
    )
