"""Arrow-string scoring: a faithful Python port of Between Ends' ``scoring.js``.

Each archer's score is stored by the API as one string of arrow symbols, e.g.
``"899T99998TT9..."`` (rounds/ends are NOT delimited; they are implied by the
event's rounds / ends-per-round / arrows-per-end). This module turns that string
into totals, round totals, and the tie-breaker counts used for ranking.

Fidelity notes (behaviour copied from the website so our numbers match theirs):
  * ``X`` and ``T`` are both worth 10. ``W``/``Z`` are NFAA "X" variants worth
    5/6; ``Y`` is a worth-11 X; ``a``/``b``/``d`` are 3D 11/12/14.
  * ``E`` is a blank (end not shot) and ``!`` is a zero-point bonus marker.
  * Arrows beyond rounds*ends*arrows (shoot-offs) ARE added into the site's
    total and tie-breaker counts. We keep both the regulation score and the
    with-shoot-off total.
  * ``rtl`` (pipe-delimited round totals) and ``tbs`` (pipe-delimited manual
    tie-breaker counts) override the arrow-derived values when present.

One deliberate deviation: where the site computes a missing round total from a
partially filled ``rtl`` it slices the arrow string incorrectly (it passes a
round index where an end index is expected). We slice by round properly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# char -> (points, display text)
ARROW_VALUE: dict[str, int] = {
    "M": 0, "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "X": 10,
    "W": 5, "Y": 11, "Z": 6,       # alternative X's (NFAA etc.)
    "a": 11, "b": 12, "d": 14,     # 3D
    "E": 0,                        # blank
    "!": 0,                        # bonus marker
    # Lowercase data-entry variants seen in the wild (6 entries). The website's own scorer gives
    # them 0; we also treat them as the obvious intent: m = miss, e = blank (not shot).
    "m": 0,
    "e": 0,
}
BLANK = "E"

# index = the event's `srl` ("scoring rules") value.
# Each entry is a list of (arrow chars counted, label) tie-breakers in priority order.
TIE_BREAKERS: list[list[tuple[str, str]]] = [
    [("T", "10s"), ("9", "9s")],                      # 0  WA indoor
    [("TX", "10s"), ("X", "Xs")],                     # 1  WA outdoor (10s then Xs)
    [("T", "10s")],                                   # 2  OAS ranking round
    [("6", "6s"), ("5", "5s")],                       # 3  WA field
    [("a", "11s")],                                   # 4  3D IBO
    [("bd!", "12s")],                                 # 5  3D ASA
    [("W", "Xs")],                                    # 6  NFAA indoor
    [],                                               # 7  combined field & 3D
    [],                                               # 8  WA indoor, no tie-breakers
    [("a", "11s"), ("T", "10s")],                     # 9  Lancaster
    [],                                               # 10 OAS league matches
    [("bd!", "Bonus")],                               # 11 3D ASA (14 ring)
    [("W", "Xs")],                                    # 12 NFAA field
    [("X", "Xs"), ("TX", "10s"), ("9", "9s")],        # 13 Easton scholarship round
    [("Z", "Xs")],                                    # 14 irregular NFAA indoor
    [("T", "10s"), ("8", "8s")],                      # 15 ESDF 3D
    [("a", "11s"), ("T", "10s")],                     # 16 3D WA
    [("X", "Xs"), ("T", "10s")],                      # 17 WA outdoor (Xs then 10s)
    [("a", "11s")],                                   # 18 3D IBO (4-H ethical pass)
]

# Arrow symbols that are legal under each rule (from scoring.js `valid_arrows`).
VALID_ARROWS: list[str] = [
    "M123456789T",    # 0  WA indoor
    "M123456789TX",   # 1  WA outdoor
    "M123456789T",    # 2  OAS ranking
    "M123456",        # 3  WA field
    "M58Ta",          # 4  3D IBO
    "M58Tb",          # 5  3D ASA
    "M12345W",        # 6  NFAA indoor
    "M1234568Ta",     # 7  combined field & 3D
    "M123456789T",    # 8  WA indoor, no tie-breakers
    "M123456789Ta",   # 9  Lancaster
    "01234567",       # 10 OAS league matches
    "M58Tbd",         # 11 3D ASA (14 ring)
    "M345W",          # 12 NFAA field
    "M123456789TX",   # 13 Easton scholarship round
    "M12345Z",        # 14 irregular NFAA indoor
    "M58T",           # 15 ESDF 3D
    "M58Ta",          # 16 3D WA
    "M123456789TX",   # 17 WA outdoor (Xs then 10s)
    "M158Ta",         # 18 3D IBO (4-H ethical pass)
]


def max_arrow_value(srl) -> int:
    """Highest points a single arrow can score under scoring rule `srl` (default 10)."""
    try:
        i = int(srl)
    except (TypeError, ValueError):
        return 10
    if not 0 <= i < len(VALID_ARROWS):
        return 10
    vals = [ARROW_VALUE.get(c, 0) for c in VALID_ARROWS[i]]
    return max(vals) if vals else 10


SCORING_RULE_NAMES = {
    0: "WA indoor (10s, 9s)", 1: "WA outdoor (10s, Xs)", 2: "OAS ranking round (10s)",
    3: "WA field (6s, 5s)", 4: "3D IBO", 5: "3D ASA", 6: "NFAA indoor",
    7: "Combined field & 3D", 8: "WA indoor, no tie-breakers", 9: "Lancaster",
    10: "OAS league matches", 11: "3D ASA (14 ring)", 12: "NFAA field",
    13: "Easton scholarship round", 14: "Irregular NFAA indoor (X=6)", 15: "ESDF 3D",
    16: "3D WA", 17: "WA outdoor (Xs, 10s)", 18: "3D IBO (4-H ethical pass)",
}


def arrow_points(arrows: str | None) -> int:
    if not arrows:
        return 0
    return sum(ARROW_VALUE.get(c, 0) for c in arrows)


def arrows_shot(arrows: str | None) -> int:
    """Arrows actually shot (blank 'E' placeholders excluded), as the site counts."""
    return len(arrows.replace(BLANK, "").replace("e", "")) if arrows else 0


def tb_labels(srl: int | str | None) -> list[str]:
    rules = _rule(srl)
    return [lab for _, lab in rules]


def _rule(srl) -> list[tuple[str, str]]:
    try:
        i = int(srl)
    except (TypeError, ValueError):
        return []
    return TIE_BREAKERS[i] if 0 <= i < len(TIE_BREAKERS) else []


def count_chars(arrows: str | None, chars: str) -> int:
    if not arrows:
        return 0
    return sum(1 for c in arrows if c in chars)


def _pipe_int(s: str | None, idx: int) -> int | None:
    if not s:
        return None
    parts = s.split("|")
    if idx >= len(parts) or parts[idx].strip() == "":
        return None
    try:
        return int(parts[idx])
    except ValueError:
        return None


@dataclass
class EntryScore:
    arrows_shot: int = 0              # all arrows shot, incl. shoot-off
    reg_arrows_shot: int = 0          # arrows shot within the regulation count
    total: int = 0                    # as the website ranks (includes shoot-off arrows)
    regulation_score: int = 0         # first rounds*epr*ape arrows only
    shootoff_arrows: int = 0
    round_totals: list[int] = field(default_factory=list)
    tie: tuple[int, int, int] = (0, 0, 0)
    x_count: int = 0                  # arrows scored 'X'
    ten_count: int = 0                # 'X' or 'T' (every 10)
    nine_count: int = 0
    miss_count: int = 0               # 'M'
    used_round_totals_override: bool = False


def score_entry(
    arrows: str | None,
    rounds: int,
    ends_per_round: int,
    arrows_per_end: int,
    srl: int | str | None = 0,
    rtl: str | None = None,
    tbs: str | None = None,
    special_tb1: int | None = None,
    special_tb: bool = False,
) -> EntryScore:
    arrows = arrows or ""
    per_round = ends_per_round * arrows_per_end
    regulation = rounds * per_round

    reg_arrows = arrows[:regulation] if regulation else arrows
    extra = arrows[regulation:] if regulation else ""

    # ---- round totals (override from `rtl` where given, else from arrows) ----
    round_totals: list[int] = []
    for r in range(max(rounds, 0)):
        override = _pipe_int(rtl, r)
        if override is not None:
            round_totals.append(override)
        else:
            round_totals.append(arrow_points(arrows[r * per_round:(r + 1) * per_round]))

    if rtl and any(_pipe_int(rtl, r) is not None for r in range(max(rounds, 0))):
        reg_total = sum(round_totals)
        used_override = True
    else:
        reg_total = arrow_points(reg_arrows) if regulation else arrow_points(arrows)
        used_override = False
    total = reg_total + arrow_points(extra)

    # ---- tie-breakers (manual `tbs` value is added to any shoot-off arrows) ----
    rule = _rule(srl)
    ties = [0, 0, 0]
    for i in range(min(len(rule), 3)):
        manual = _pipe_int(tbs, i)
        if manual is not None:
            ties[i] = manual + (count_chars(extra, rule[i][0]) if extra else 0)
        else:
            ties[i] = count_chars(arrows, rule[i][0])
    # site: tie_3 = archer.tb1 if the event uses a special tie-breaker.
    if special_tb:
        ties[2] = special_tb1 or 0
    elif len(rule) != 3:
        ties[2] = 0

    return EntryScore(
        arrows_shot=arrows_shot(arrows),
        reg_arrows_shot=arrows_shot(reg_arrows),
        total=total,
        regulation_score=reg_total,
        shootoff_arrows=len(extra),
        round_totals=round_totals,
        tie=(ties[0], ties[1], ties[2]),
        x_count=count_chars(arrows, "X"),
        ten_count=count_chars(arrows, "XT"),
        nine_count=count_chars(arrows, "9"),
        miss_count=count_chars(arrows, "Mm"),
        used_round_totals_override=used_override,
    )


def rank_group(rows: list[tuple]) -> list[int]:
    """Competition ranking (1,1,3,...) over rows = [(total, t1, t2, t3, first, last)].

    Rows must already be sorted best-first by (-total, -t1, -t2, -t3, first, last)
    as the website does. Entries identical on total and all three tie-breakers
    share a rank; the next distinct entry takes its positional rank.
    """
    ranks: list[int] = []
    prev_key = None
    prev_rank = 0
    for pos, r in enumerate(rows, start=1):
        key = r[:4]
        if prev_key is not None and key == prev_key:
            ranks.append(prev_rank)
        else:
            ranks.append(pos)
            prev_rank = pos
            prev_key = key
    return ranks
