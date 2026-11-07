"""Normalise the raw API cache (data/raw) into tidy, analysis-ready tables.

    python -m pipeline.normalize [--as-of YYYY-MM-DD]

Outputs (data/processed/, gzip CSV, deterministic ordering and bytes):

    tournaments.csv.gz      every tournament in the index (in-scope flag + classification)
    events.csv.gz           every event of every in-scope tournament
    entries.csv.gz          one row per archer per ranking/combined event  (the fact table)
    entry_groups.csv.gz     entry x class/division group membership, with rank in group
    entry_arrows.csv.gz     the raw arrow string for each entry
    match_rows.csv.gz       elimination / round-robin match rows (one per archer per match)
    custom_points.csv.gz    team / points-based rankings (CustomPointsEvent)
    identity_review.csv.gz  identity-resolution cases a human should look at
    normalize_issues.csv    events that could not be parsed (should be empty)

The archer dimension and per-archer trend tables are built from these by archers.py.

Everything is derived from raw JSON only - re-running is offline and idempotent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from . import config
from .classify import classify_tournament
from .client import ApiClient, NotFound
from .groups import parse_group
from .identity import clean_display_name, name_key, normalize_alt, resolve
from .scope import decide
from .scoring import (
    ARROW_VALUE,
    SCORING_RULE_NAMES,
    max_arrow_value,
    rank_group,
    score_entry,
    tb_labels,
)

# Minimum arrows for a performance to be deduplicated by its arrow string.
MIN_ARROWS_FOR_DEDUPE = 24

_DIV_FROM_CND = {"R": "Recurve", "C": "Compound", "B": "Barebow", "L": "Longbow", "T": "Traditional Recurve",
                 "F": "Fixed Pins", "X": "Crossbow", "O": "Para Compound", "H": "Bowhunter"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _bool(x) -> bool:
    if isinstance(x, str):
        return x.strip().lower() not in ("", "false", "0", "none", "null")
    return bool(x)


def _rtl_complete(rtl, rounds: int) -> bool:
    """True when `rtl` (pipe-separated manual round totals) supplies a number for every round."""
    if not rtl or rounds <= 0:
        return False
    parts = str(rtl).split("|")
    # a typed 0 means "round not entered" (a real round total of 0 does not occur)
    return len(parts) >= rounds and all(p.strip().isdigit() and int(p) > 0 for p in parts[:rounds])


def _parse_edition(name: str) -> int | None:
    m = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\b", name or "", re.I)
    return int(m.group(1)) if m else None


def _championship_year(name: str, start_year: int | None, category: str, edition: int | None) -> int | None:
    """The season a championship belongs to, which can differ from its start date's year:
    the 50th Indoor Nationals starts 28-Dec-2018 but is the 2019 championship.
    Order: an explicit year in the name (within +-1 of the start year) -> edition arithmetic
    (49th Indoor = 2018; 134th Target = 2018) -> the start year."""
    if start_year is None:
        return None
    m = re.search(r"\b(20\d{2})\b", name or "")
    if m and abs(int(m.group(1)) - start_year) <= 1:
        return int(m.group(1))
    if edition:
        if category in ("indoor_nationals", "joad_indoor_nationals"):
            y = edition + 1969
        elif category in ("target_nationals",):
            y = edition + 1884
        else:
            return start_year
        if abs(y - start_year) <= 1:
            return y
    return start_year


def _usat_leg(name: str) -> str | None:
    if not re.search(r"\bUSAT\b", name or ""):
        return None
    m = re.search(r"#\s*(\d)", name)
    if m:
        return m.group(1)
    return "F" if re.search(r"series final", name, re.I) else None


def _format_family(srl, ape: int) -> str:
    s = _int(srl, -1)
    if s in (0, 8):
        return "indoor_wa"
    if s in (1, 17):
        return "outdoor_wa"
    if s == 13:
        return "scholarship_round"
    if s in (2, 10):
        return "oas"
    if s in (3,):
        return "field_wa"
    if s in (12, 7):
        return "field_nfaa" if s == 12 else "field_3d_combined"
    if s in (6, 14):
        return "indoor_nfaa"
    if s in (4, 5, 11, 15, 16, 18):
        return "3d"
    if s == 9:
        return "lancaster"
    return "other"


def _perf_hash(arrows: str) -> str:
    return hashlib.sha1(arrows.encode()).hexdigest()[:16]


def _load(client: ApiClient, kind: str, key: str):
    try:
        if client.is_cached(kind, key):
            return client.read_cached(kind, key)
    except NotFound:
        return None
    return None


# --------------------------------------------------------------------------- #
# tournaments
# --------------------------------------------------------------------------- #
def build_tournaments(index: list[dict], details: dict[int, dict], as_of: dt.date) -> pd.DataFrame:
    rows = []
    for t in index:
        d = decide(t, as_of)
        c = classify_tournament(t)
        det = details.get(t["id"]) or {}
        evs = det.get("events") or []
        name = t.get("tournament_name") or ""
        start = (t.get("start_date") or "")[:10]
        year = int(start[:4]) if start else None
        edition = _parse_edition(name)
        usat_leg = _usat_leg(name)
        champ_year = _championship_year(name, year, c.category, edition)
        champ_key = None
        if c.category in ("indoor_nationals", "joad_indoor_nationals", "target_nationals", "joad_target_nationals",
                          "collegiate_target_nationals", "collegiate_3d_nationals", "field_nationals",
                          "3d_nationals", "oas_nationals"):
            champ_key = f"{c.category}:{champ_year}"
        elif c.category == "usat_series":
            champ_key = f"usat_series:{champ_year}"
        rows.append(
            {
                "tournament_id": t["id"],
                "name": name.strip(),
                "location": (t.get("location") or "").strip(),
                "start_date": start,
                "end_date": (t.get("end_date") or "")[:10],
                "year": year,
                "updated_at": t.get("updated_at"),
                "category": c.category,
                "tags": "|".join(c.tags),
                "is_national_championship": c.is_national_championship,
                "is_indoor_national": c.is_indoor_national,
                "is_outdoor_national": c.is_outdoor_national,
                "is_usat": c.category == "usat_series",
                "is_team_trials": c.category == "team_trials",
                "is_marquee": c.is_marquee,
                "championship_year": champ_year, "championship_key": champ_key,
                "edition": edition,
                "usat_leg": usat_leg,
                "is_combined_results": "combined_results" in c.tags,
                "status": d.status,
                "in_scope": d.in_scope,
                "scope_reasons": "|".join(d.reasons),
                "details_collected": bool(det),
                "n_events": len(evs),
                "event_types": "|".join(sorted({e.get("event_type") or "" for e in evs})),
                "organizer_message": (det.get("msg") or "").strip(),
            }
        )
    df = pd.DataFrame(rows).sort_values(["start_date", "tournament_id"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# ranking / combined events -> entries
# --------------------------------------------------------------------------- #
def process_ranking_event(tid: int, eref: dict, ev: dict, sc: dict | None):
    eid = eref["id"]
    rds, epr, ape = _int(ev.get("rds")), _int(ev.get("epr")), _int(ev.get("ape"))
    srl = ev.get("srl")
    reg = rds * epr * ape
    special_tb = _bool(ev.get("stb"))
    rps: dict = ev.get("rps") or {}
    arrows_by_aid: dict = (sc or {}).get("ars") or {}
    tnl: dict = ev.get("tnl") or {}
    rule_max = max_arrow_value(srl)
    # Organisers sometimes pick the wrong scoring rule (e.g. NFAA, max 5, for a 10-ring round). The arrows
    # actually shot are the evidence: never let the declared rule cap below a value that was really scored.
    observed_chars = set("".join(arrows_by_aid.values())) if arrows_by_aid else set()
    observed_max = max((ARROW_VALUE.get(c, 0) for c in observed_chars), default=0)
    maxpts = max(rule_max, observed_max)
    max_score = reg * maxpts

    groups = [g for g in (ev.get("cgs") or []) if g]
    # --- score every registered archer once
    scored = {}
    for aid_s, a in rps.items():
        scored[aid_s] = score_entry(
            arrows_by_aid.get(aid_s), rds, epr, ape, srl, a.get("rtl"), a.get("tbs"),
            special_tb1=_int(a.get("tb1")), special_tb=special_tb,
        )

    # --- rank within each group exactly as the website does
    membership: dict[str, list[dict]] = defaultdict(list)   # aid -> [group info]
    group_rows = []
    for gi, g in enumerate(groups):
        members = [m for m in (g.get("ars") or []) if m and str(m.get("aid")) in rps]
        sortable = []
        for m in members:
            aid_s = str(m["aid"])
            r = scored[aid_s]
            a = rps[aid_s]
            sortable.append((r.total, r.tie[0], r.tie[1], r.tie[2], (a.get("fnm") or "").lower(), (a.get("lnm") or "").lower(), aid_s))
        sortable.sort(key=lambda t: (-t[0], -t[1], -t[2], -t[3], t[4], t[5]))
        ranks = rank_group(sortable)
        pg = parse_group(g.get("nm"))
        cut = _int(g.get("cut"))
        for rk, row in zip(ranks, sortable):
            aid_s = row[6]
            info = {
                "group_name": g.get("nm") or "", "group_order": _int(g.get("dor"), gi + 1), "division": pg.division,
                "age_class": pg.age_class, "gender": pg.gender, "is_guest": pg.is_guest,
                "is_collegiate": pg.is_collegiate, "is_para": pg.is_para, "rank": rk, "group_size": len(sortable),
                "cut": cut, "advanced": bool(cut and rk <= cut),
            }
            membership[aid_s].append(info)
            group_rows.append({"entry_id": f"{eid}-{aid_s}", "event_id": eid, **info})

    # --- entries
    entries, arrow_rows = [], []
    for aid_s, a in rps.items():
        r = scored[aid_s]
        mem = sorted(membership.get(aid_s, []), key=lambda x: x["group_order"])
        non_coll = [m for m in mem if not m["is_collegiate"]]
        primary = (non_coll or mem or [None])[0]
        cnd = (a.get("cnd") or "").strip()
        if primary:
            division, age_class, gender = primary["division"], primary["age_class"], primary["gender"]
            is_guest, is_para = primary["is_guest"], primary["is_para"]
        else:  # not in any group: fall back to the compact class code
            division = _DIV_FROM_CND.get(cnd[:1], "Other") if cnd else "Other"
            age_class = "Unknown"
            gender = cnd[-1] if cnd[-1:] in ("M", "W") else "?"
            is_guest = is_para = False
        arrows = arrows_by_aid.get(aid_s) or ""
        rtl_full = _rtl_complete(a.get("rtl"), rds)
        exceeds = bool(max_score and r.regulation_score > max_score)      # impossible under the event's own format
        totals_only = bool(rtl_full and r.reg_arrows_shot == 0)           # results typed in as round totals
        # points-per-arrow denominator: all regulation arrows if every round total was entered; the arrows
        # actually shot if the card is arrow-scored; none (blank) for a typed/partial mix we cannot interpret.
        arrows_complete = bool(reg and r.reg_arrows_shot >= reg)
        if rtl_full or arrows_complete:
            basis_arrows = reg
        elif r.used_round_totals_override:
            basis_arrows = 0
        else:
            basis_arrows = r.reg_arrows_shot
        complete = bool(reg and (r.reg_arrows_shot >= reg or (rtl_full and reg > 0))) and not exceeds
        sess_ids = a.get("tnl") or []
        sess_names = [(tnl.get(str(s)) or tnl.get(s) or {}).get("nm", "") for s in sess_ids]
        rt = r.round_totals
        entries.append(
            {
                "entry_id": f"{eid}-{aid_s}",
                "event_id": eid, "tournament_id": tid, "aid": _int(aid_s),
                "first_name_raw": (a.get("fnm") or "").strip(), "last_name_raw": (a.get("lnm") or "").strip(),
                "alt_raw": (a.get("alt") or "").strip() if isinstance(a.get("alt"), str) else (a.get("alt") or ""),
                "cnd_code": cnd, "club": (a.get("tm") or "").strip(),
                "sessions": "|".join(s for s in sess_names if s),
                "targets": "|".join(str(t) for t in (a.get("tgt") or [])),
                "primary_group": primary["group_name"] if primary else "",
                "division": division, "age_class": age_class, "gender": gender,
                "is_guest": is_guest, "is_para": is_para,
                "is_collegiate": any(m["is_collegiate"] for m in mem),
                "groups": "|".join(m["group_name"] for m in mem),
                "n_groups": len(mem),
                "regulation_arrows": reg, "arrows_shot": r.arrows_shot, "reg_arrows_shot": r.reg_arrows_shot,
                "complete_card": complete, "totals_only": totals_only, "score_exceeds_format": exceeds,
                "total_score": r.total, "regulation_score": r.regulation_score, "shootoff_arrows": r.shootoff_arrows,
                "max_score_possible": max_score,
                "round_totals": "|".join(str(x) for x in rt),
                **{f"r{i + 1}": (rt[i] if i < len(rt) else None) for i in range(4)},
                "tie1": r.tie[0], "tie2": r.tie[1], "tie3": r.tie[2],
                "x_count": r.x_count, "ten_count": r.ten_count, "nine_count": r.nine_count, "miss_count": r.miss_count,
                "avg_arrow": round(r.regulation_score / basis_arrows, 4) if (basis_arrows and not exceeds) else None,
                "pct_of_max": round(r.regulation_score / max_score, 4) if (max_score and complete) else None,
                "rank_primary": primary["rank"] if primary else None,
                "group_size_primary": primary["group_size"] if primary else None,
                "percentile_primary": (round(1 - (primary["rank"] - 1) / primary["group_size"], 4)
                                       if primary and primary["group_size"] else None),
                "used_round_total_override": r.used_round_totals_override,
                "perf_key": (_perf_hash(arrows) if len(arrows.replace("E", "")) >= MIN_ARROWS_FOR_DEDUPE else ""),
            }
        )
        if arrows:
            arrow_rows.append({"entry_id": f"{eid}-{aid_s}", "arrows": arrows})

    event_row = {
        "event_id": eid, "tournament_id": tid, "display_order": eref.get("display_order"),
        "event_type": eref.get("event_type"), "event_name": (eref.get("event_name") or "").strip(),
        "is_aggregate": eref.get("event_type") == "CombinedRankingEvent",
        "rounds": rds, "ends_per_round": epr, "arrows_per_end": ape, "regulation_arrows": reg,
        "format_key": f"{rds}x{epr}x{ape}", "format_family": _format_family(srl, ape),
        "declared_max_arrow_value": rule_max, "rule_mismatch": observed_max > rule_max,
        "scoring_rule": _int(srl, -1), "scoring_rule_name": SCORING_RULE_NAMES.get(_int(srl, -1), ""),
        "tiebreak_labels": "|".join(tb_labels(srl)), "max_arrow_value": maxpts, "max_score_possible": max_score,
        "n_sessions": _int(ev.get("nos")), "n_archers": len(rps), "n_groups": len(groups),
        "n_scored": sum(1 for r in scored.values() if r.reg_arrows_shot > 0),
        "n_complete": sum(1 for r in scored.values() if reg and r.reg_arrows_shot >= reg),
        "has_scores": bool(arrows_by_aid), "event_json_id": ev.get("id"),
    }
    return event_row, entries, group_rows, arrow_rows


# --------------------------------------------------------------------------- #
# match events
# --------------------------------------------------------------------------- #
_ROUND_NAMES = {0: "Finals", 1: "Semifinals", 2: "Quarterfinals", 3: "1/8 Elimination", 4: "1/16 Elimination",
                5: "1/32 Elimination", 6: "1/64 Elimination"}
_EXIT_PLACE = {2: 5, 3: 9, 4: 17, 5: 33, 6: 65}


def _std_round(mnm: int) -> int:
    for lim, r in ((4, 0), (8, 1), (16, 2), (32, 3), (64, 4), (128, 5)):
        if mnm < lim:
            return r
    return 6


def process_match_event(tid: int, eref: dict, ev: dict, sc: dict | None):
    eid = eref["id"]
    ars = (sc or {}).get("ars") or {}
    rows = []
    mgs = ev.get("mgs") or []
    for gi, g in enumerate(mgs):
        met = _int(g.get("met"))
        mrs = sorted(g.get("mrs") or [], key=lambda m: _int(m.get("mnm")))
        pg = parse_group(g.get("nm"))
        # standard single-elimination: derive final placings from deepest round reached
        deepest: dict[str, int] = {}
        in_gold, in_bronze, won_last = {}, {}, {}
        std = met not in (3, 4, 7, 8)
        for m in mrs:
            aid = m.get("aid")
            if aid in ("", None):
                continue
            rnd = _std_round(_int(m.get("mnm"))) if std else None
            if std:
                deepest[aid] = max(deepest.get(aid, 0), rnd)
                if rnd == 0:
                    (in_gold if _int(m.get("mnm")) < 2 else in_bronze)[aid] = _int(m.get("sts")) == 1
        gold_decided = any(in_gold.values())
        place: dict = {}
        if std and gold_decided:
            for aid, won in in_gold.items():
                place[aid] = 1 if won else 2
            if any(in_bronze.values()):
                for aid, won in in_bronze.items():
                    place[aid] = 3 if won else 4
            for aid, d in deepest.items():
                if aid not in place and d in _EXIT_PLACE:
                    place[aid] = _EXIT_PLACE[d]
        # pair partners (consecutive rows form a match) for non-"met 8" formats
        for j, m in enumerate(mrs):
            mrid = m.get("mrid")
            mnm = _int(m.get("mnm"))
            partner = None
            if met != 8 and len(mrs) > 1:
                pj = j + 1 if j % 2 == 0 else j - 1
                if 0 <= pj < len(mrs):
                    partner = mrs[pj]
            sc_row = ars.get(str(mrid)) or {}
            aid = m.get("aid")
            rnd = _std_round(mnm) if std else (mnm // max(_int(g.get("str")) + (_int(g.get("str")) % 2), 1) if met in (3, 4) else None)
            rows.append(
                {
                    "match_row_id": f"{eid}-{mrid}", "event_id": eid, "tournament_id": tid,
                    "match_group": g.get("nm") or "", "group_order": _int(g.get("dor"), gi + 1),
                    "division": pg.division, "age_class": pg.age_class, "gender": pg.gender,
                    "match_event_type": met, "start_round": _int(g.get("str")), "scoring_rule": _int(g.get("srl"), -1),
                    "ends_per_match": _int(g.get("epm")), "arrows_per_end": _int(g.get("ape")),
                    "mrid": mrid, "match_number": mnm, "round_index": rnd,
                    "round_name": _ROUND_NAMES.get(rnd, "") if std else "",
                    "pair_index": (mnm // 2) if met != 8 else None,
                    "aid": aid if aid not in ("", None) else None, "is_bye": aid in ("", None),
                    "first_name_raw": (m.get("fnm") or "").strip(), "last_name_raw": (m.get("lnm") or "").strip(),
                    "club": (m.get("tm") or "").strip(), "target": m.get("tgt") or "",
                    "seed": _int(m.get("rnk"), None) if m.get("rnk") not in ("", None) else None,
                    "won": _int(m.get("sts")) == 1, "final_score": m.get("fsc"),
                    "arrows": sc_row.get("avs") or "", "shootoff": sc_row.get("tb") or "",
                    "opponent_aid": (partner.get("aid") if partner and partner.get("aid") not in ("", None) else None),
                    "opponent_final_score": (partner.get("fsc") if partner else None),
                    "final_place": (place.get(aid) if aid not in ("", None) else None),
                }
            )
    n_arch = len({r["aid"] for r in rows if r["aid"] is not None})
    event_row = {
        "event_id": eid, "tournament_id": tid, "display_order": eref.get("display_order"),
        "event_type": eref.get("event_type"), "event_name": (eref.get("event_name") or "").strip(),
        "is_aggregate": False, "n_archers": n_arch, "n_groups": len(mgs), "n_scored": sum(1 for r in rows if r["arrows"]),
        "has_scores": bool(ars), "event_json_id": ev.get("id"),
    }
    return event_row, rows


# --------------------------------------------------------------------------- #
# custom points events
# --------------------------------------------------------------------------- #
def process_custom_event(tid: int, eref: dict, ev: dict):
    eid = eref["id"]
    archers = ev.get("archers") or {}
    if isinstance(archers, list):                      # tolerate a list-of-dicts layout
        archers = {str(a.get("aid")): a for a in archers if isinstance(a, dict)}
    rows = []
    for blk in ev.get("data") or []:
        # newer events nest columns as data[i][j]; older (2018) ones are a flat list of column dicts
        cols = blk if isinstance(blk, list) else [blk]
        for col in cols:
            if not isinstance(col, dict):
                continue
            for aid_s, sc in (col.get("scores") or {}).items():
                a = archers.get(aid_s) or archers.get(str(sc.get("aid"))) or {}
                rows.append(
                    {
                        "event_id": eid, "tournament_id": tid,
                        "ranking_name": col.get("cnd_name") or eref.get("event_name") or "",
                        "column_label": col.get("column_label") or "", "sub_group": col.get("group") or "",
                        "aid": _int(aid_s), "first_name_raw": (a.get("fname") or "").strip(),
                        "last_name_raw": (a.get("lname") or "").strip(), "home_state": (a.get("home_state") or "").strip(),
                        "points": sc.get("points"), "score": sc.get("score"), "rank": sc.get("rank"),
                        "sig_digits": sc.get("sig_digits"),
                    }
                )
    event_row = {
        "event_id": eid, "tournament_id": tid, "display_order": eref.get("display_order"),
        "event_type": eref.get("event_type"), "event_name": (eref.get("event_name") or "").strip(),
        "is_aggregate": False, "n_archers": len({r["aid"] for r in rows}), "n_groups": len({r["ranking_name"] for r in rows}),
        "has_scores": bool(rows), "event_json_id": None,
    }
    return event_row, rows


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression={"method": "gzip", "mtime": 0, "compresslevel": 6}, lineterminator="\n")
    try:
        shown = path.relative_to(config.REPO_ROOT)
    except ValueError:  # output directory outside the repo (e.g. a scratch dir)
        shown = path
    print(f"  wrote {shown}  rows={len(df):,}  {path.stat().st_size / 1e6:6.2f} MB")


def run(as_of: dt.date, out_dir: Path = config.PROCESSED_DIR) -> dict:
    client = ApiClient()
    index = client.tournaments_index().data
    decisions = {t["id"]: decide(t, as_of) for t in index}
    scoped = [t for t in index if decisions[t["id"]].in_scope]
    details = {t["id"]: _load(client, "tournaments", str(t["id"])) for t in scoped}
    details = {k: v for k, v in details.items() if v}

    tournaments = build_tournaments(index, details, as_of)

    events, entries, grp_rows, arrow_rows, match_rows, custom_rows, issues = [], [], [], [], [], [], []
    for t in sorted(scoped, key=lambda x: (x["start_date"], x["id"])):
        det = details.get(t["id"])
        if not det:
            issues.append({"tournament_id": t["id"], "event_id": None, "issue": "tournament detail not collected"})
            continue
        for eref in det.get("events") or []:
            eid, etype = eref["id"], eref.get("event_type")
            try:
                ev = _load(client, "events", str(eid))
                if ev is None:
                    # SpecialEvent etc. legitimately return null; record as an empty event row
                    sc_probe = _load(client, "scores", str(eid))
                    events.append({"event_id": eid, "tournament_id": t["id"], "display_order": eref.get("display_order"),
                                   "event_type": etype, "event_name": (eref.get("event_name") or "").strip(),
                                   "is_aggregate": False, "n_archers": 0, "has_scores": False})
                    if not client.is_cached("events", str(eid)):
                        issues.append({"tournament_id": t["id"], "event_id": eid, "issue": f"{etype}: event payload not collected"})
                    elif sc_probe not in (None, {}):
                        issues.append({"tournament_id": t["id"], "event_id": eid, "issue": f"{etype}: event null but scores present"})
                    continue
                sc = _load(client, "scores", str(eid))
                if etype in ("RankingEvent", "CombinedRankingEvent"):
                    if sc is None and not client.is_cached("scores", str(eid)):
                        issues.append({"tournament_id": t["id"], "event_id": eid, "issue": "scores not collected"})
                    er, en, gr, ar = process_ranking_event(t["id"], eref, ev, sc)
                    events.append(er); entries.extend(en); grp_rows.extend(gr); arrow_rows.extend(ar)
                elif etype == "MatchEvent":
                    er, mr = process_match_event(t["id"], eref, ev, sc)
                    events.append(er); match_rows.extend(mr)
                elif etype == "CustomPointsEvent":
                    er, cr = process_custom_event(t["id"], eref, ev)
                    events.append(er); custom_rows.extend(cr)
                else:
                    events.append({"event_id": eid, "tournament_id": t["id"], "display_order": eref.get("display_order"),
                                   "event_type": etype, "event_name": (eref.get("event_name") or "").strip(),
                                   "is_aggregate": False, "n_archers": 0, "has_scores": False})
                    issues.append({"tournament_id": t["id"], "event_id": eid, "issue": f"unhandled event type {etype}"})
            except Exception as e:  # noqa: BLE001 - one bad event must not sink the run
                issues.append({"tournament_id": t["id"], "event_id": eid, "issue": f"{etype}: {type(e).__name__}: {e}"[:300]})

    events_df = pd.DataFrame(events).sort_values(["tournament_id", "display_order", "event_id"]).reset_index(drop=True)
    entries_df = pd.DataFrame(entries)
    groups_df = pd.DataFrame(grp_rows)
    arrows_df = pd.DataFrame(arrow_rows)
    match_df = pd.DataFrame(match_rows)
    custom_df = pd.DataFrame(custom_rows)

    # ---- identity ---------------------------------------------------------
    print("resolving archer identities ...")
    tyear = dict(zip(tournaments.tournament_id, tournaments.year))
    tmarq = dict(zip(tournaments.tournament_id, tournaments.is_marquee))
    if len(entries_df):
        res = resolve(entries_df.first_name_raw.tolist(), entries_df.last_name_raw.tolist(), entries_df.alt_raw.tolist(),
                      clubs=entries_df.club.tolist(), years=[tyear.get(t) for t in entries_df.tournament_id],
                      genders=entries_df.gender.tolist(), divisions=entries_df.division.tolist(),
                      tournaments=entries_df.tournament_id.tolist(), events=entries_df.event_id.tolist(),
                      age_classes=entries_df.age_class.tolist(),
                      marquee=[bool(tmarq.get(t)) for t in entries_df.tournament_id])
        entries_df["archer_id"] = res.archer_id
        entries_df["identity_basis"] = res.basis
        entries_df["alt_id"] = [normalize_alt(a) or "" for a in entries_df.alt_raw]
        entries_df["display_name"] = [clean_display_name(f, l) for f, l in zip(entries_df.first_name_raw, entries_df.last_name_raw)]
        entries_df["name_key"] = [name_key(f, l) for f, l in zip(entries_df.first_name_raw, entries_df.last_name_raw)]
        review_df = pd.DataFrame(res.review)

        # ---- canonical performance (dedupe Combined vs site events) ----------
        etype = dict(zip(events_df.event_id, events_df.event_type))
        entries_df["event_type"] = entries_df.event_id.map(etype)
        entries_df["_agg"] = (entries_df.event_type == "CombinedRankingEvent").astype(int)
        entries_df["is_canonical"] = True
        has_key = entries_df.perf_key != ""
        sub = entries_df[has_key].copy()
        sub["_dk"] = sub.archer_id + "|" + sub.perf_key
        sub = sub.sort_values(["_dk", "_agg", "event_id"])
        dup = sub.duplicated("_dk", keep="first")
        entries_df.loc[dup[dup].index, "is_canonical"] = False
        entries_df["duplicate_of_entry_id"] = ""
        first_of = sub.drop_duplicates("_dk", keep="first").set_index("_dk").entry_id
        entries_df.loc[dup[dup].index, "duplicate_of_entry_id"] = sub.loc[dup[dup].index, "_dk"].map(first_of).values
        entries_df = entries_df.drop(columns=["_agg"])

        # ---- map match rows / custom rows to archers via the same tournament --
        aid_to_archer = {(r.tournament_id, r.aid): r.archer_id for r in entries_df[["tournament_id", "aid", "archer_id"]].itertuples()}
        nk_to_archers = entries_df.groupby("name_key").archer_id.nunique()
        unique_nk = entries_df[entries_df.name_key.isin(nk_to_archers[nk_to_archers == 1].index)].drop_duplicates("name_key").set_index("name_key").archer_id
        team_rx = re.compile(r"^(?P<team>.*?)\s*\[(?P<members>[^\]]+)\]\s*$")
        for df_ in (match_df, custom_df):
            if len(df_):
                ids, how = [], []
                is_team, team_name, team_members = [], [], []
                for r in df_.itertuples():
                    tm = team_rx.match(f"{r.first_name_raw} {r.last_name_raw}".strip())
                    is_team.append(bool(tm))
                    team_name.append(tm.group("team").strip() if tm else "")
                    team_members.append("|".join(x.strip() for x in tm.group("members").split("&")) if tm else "")
                    if tm:                       # a team entry ("Name [A & B & C]"), not an individual archer
                        ids.append(""); how.append("team")
                        continue
                    a = aid_to_archer.get((r.tournament_id, r.aid)) if r.aid == r.aid and r.aid is not None else None
                    if a:
                        ids.append(a); how.append("tournament_aid")
                        continue
                    k = name_key(r.first_name_raw, r.last_name_raw)
                    if k and k in unique_nk.index:
                        ids.append(unique_nk[k]); how.append("unique_name")
                    else:
                        ids.append(""); how.append("unresolved")
                df_["archer_id"] = ids
                df_["archer_match_basis"] = how
                df_["is_team"] = is_team
                df_["team_name"] = team_name
                df_["team_members"] = team_members
                df_["display_name"] = [clean_display_name(f, l) for f, l in zip(df_.first_name_raw, df_.last_name_raw)]
        # entry-group table gets archer_id too
        groups_df = groups_df.merge(entries_df[["entry_id", "archer_id"]], on="entry_id", how="left")
    else:
        review_df = pd.DataFrame()

    # ---- derived counts back on tournaments / events ----------------------
    if len(entries_df):
        can = entries_df[entries_df.is_canonical]
        t_counts = entries_df.groupby("tournament_id").agg(n_entries=("entry_id", "size"), n_archers=("archer_id", "nunique"))
        t_counts["n_canonical_entries"] = can.groupby("tournament_id").size()
        tournaments = tournaments.merge(t_counts, how="left", left_on="tournament_id", right_index=True)
        for c in ("n_entries", "n_archers", "n_canonical_entries"):
            tournaments[c] = tournaments[c].fillna(0).astype(int)

    # ---- write -------------------------------------------------------------
    print(f"writing tables to {out_dir} ...")
    _write(tournaments, out_dir / "tournaments.csv.gz")
    _write(events_df, out_dir / "events.csv.gz")
    if len(entries_df):
        entries_df = entries_df.sort_values(["tournament_id", "event_id", "aid"]).reset_index(drop=True)
        _write(entries_df, out_dir / "entries.csv.gz")
        _write(groups_df.sort_values(["event_id", "entry_id", "group_order"]).reset_index(drop=True), out_dir / "entry_groups.csv.gz")
        _write(arrows_df.sort_values("entry_id").reset_index(drop=True), out_dir / "entry_arrows.csv.gz")
        if len(review_df):
            _write(review_df, out_dir / "identity_review.csv.gz")
    if len(match_df):
        _write(match_df.sort_values(["event_id", "group_order", "match_number"]).reset_index(drop=True), out_dir / "match_rows.csv.gz")
    if len(custom_df):
        _write(custom_df.sort_values(["event_id", "ranking_name", "aid"]).reset_index(drop=True), out_dir / "custom_points.csv.gz")
    iss = pd.DataFrame(issues, columns=["tournament_id", "event_id", "issue"])
    iss.to_csv(out_dir / "normalize_issues.csv", index=False)
    print(f"  normalize_issues.csv  rows={len(iss)}")
    return {"tournaments": len(tournaments), "events": len(events_df), "entries": len(entries_df),
            "match_rows": len(match_df), "custom_rows": len(custom_df), "issues": len(iss)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.normalize", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    p.add_argument("--out", type=Path, default=config.PROCESSED_DIR)
    args = p.parse_args(argv)
    summary = run(args.as_of, args.out)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
