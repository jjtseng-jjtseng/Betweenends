"""Pre-Dec-2017 marquee results from USA Archery's own results archive.

Between Ends only starts in December 2017, so the 2016-2017 seasons of the
nationals / USAT-series / trials come from the PDF results books that USA Archery
publishes at https://www.usarchery.org/resource-center/events/national-event-results

    python -m pipeline.legacy fetch      # download the source PDFs (polite, cached)
    python -m pipeline.legacy parse      # PDFs -> data/processed/legacy_*.csv

Most of those PDFs are exports from the "Archerz" results system (rcherz.com):
a section header such as ``Indoor - Recurve - Masters 70+ - Male - 18m double round``
followed by a table ``Rank | Name | Club | Country | Score | round scores... | tie-breaks``.
This module parses that layout with pdfplumber using word *positions* (the plain
text layer has spurious spaces inside words, e.g. "Alfre d").

Integrity check: for every parsed row the round scores must sum to the total score.
Rows that fail are kept but flagged (``sum_check_ok = False``) and counted in the
legacy tournament summary, so parser problems are visible and not silent.

Documents in other layouts (ianseo books, spreadsheet exports, scanned/garbled
text, selection-procedure point tables) are listed in the manifest with
``parsed = False`` and a reason, and are not guessed at.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import config
from .classify import classify_name
from .client import RateLimiter
from .groups import parse_group
from .identity import clean_display_name, name_key

BASE = "https://www.usarchery.org/resource/"
LEGACY_RAW = config.RAW_DIR / "legacy_usarchery"
USER_AGENT = "BetweenEnds-data-collection/1.0 (owner-authorized research; github.com/jjtseng-jjtseng/betweenends)"

# slug -> (expected category, parser, note). parser None => catalogued, not parsed.
SOURCES: dict[str, tuple[str, str | None, str]] = {
    # --- nationals ----------------------------------------------------------
    "2016-47th-u-s-national-indoor-results": ("indoor_nationals", "archerz", "47th U.S. National Indoor Championships"),
    "2017-48th-us-national-indoor-final": ("indoor_nationals", "archerz", "48th U.S. National Indoor Championships - Final"),
    "2017-48th-us-national-indoor-collegiate-championships": ("indoor_nationals", "archerz", "48th U.S. National Indoor - Collegiate"),
    "2017-joad-national-indoor-results": ("joad_indoor_nationals", "archerz", "2017 JOAD National Indoor Championships"),
    "2016-joad-national-indoor-results": ("joad_indoor_nationals", None, "text layer is garbled (font encoding); would need OCR"),
    "2017-ntc-jnc-results-book": ("target_nationals", "archerz", "133rd U.S. National Target Championships, U.S. Open and 2017 JOAD Nationals - qualifications"),
    "2016-u-s-national-outdoor-collegiate-championships-results": ("collegiate_target_nationals", "archerz", "2016 U.S. National Outdoor Collegiate Championships"),
    "2017-nocc-results-book": ("collegiate_target_nationals", "archerz", "2017 U.S. National Outdoor Collegiate Championships"),
    "2016-u-s-national-field-championships-results": ("field_nationals", "archerz", "2016 U.S. National Field Championships"),
    "2017-national-field-results": ("field_nationals", "archerz", "2017 U.S. National Field Championships"),
    "2016-collegiate-results": ("indoor_nationals", "collegiate_sheet", "47th U.S. National Indoor Championships - Collegiate (spreadsheet export)"),
    "2017-national-3-d-collegiate-championships-individual-results": ("collegiate_3d_nationals", "asa_3d", "2017 Collegiate 3D Championship, individual (ASA event software)"),
    # --- USAT-series precursors (called USAT from 2017) ---------------------------
    "2016-easton-foundations-gator-cup-results": ("usat_series", "archerz", "2016 Gator Cup (USAT #2 2016)"),
    "2016-buckeye-classic-results": ("usat_series", "archerz", "2016 Buckeye Classic"),
    "2016-aae-arizona-cup-results": ("usat_series", None, "ianseo results book layout"),
    "2017-arizona-cup-results-book": ("usat_series", "archerz", "USAT #1 2017 AAE Arizona Cup"),
    "2017-gator-results-book": ("usat_series", "archerz", "USAT #2 2017 Easton Foundations Gator Cup"),
    "2017-socal-results-book": ("usat_series", "archerz", "USAT #3 2017 SoCal Showdown"),
    "2017-mathews-texas-shootout-shootout-results-book": ("usat_series", "archerz", "USAT #4 2017 Mathews TX Shootout"),
    # --- team trials ---------------------------------------------------------
    "2017-wayc---u-s-team-trials-qualification-results": ("team_trials", None, "per-archer score-card layout (not a ranked table)"),
    "2017-world-archery-para-championships-ndash-u-s-team-trials-qualification-results": ("team_trials", "archerz", "2017 World Archery Para Championships U.S. Team Trials - qualification"),
    "2016-u-s-olympic-team-trials": ("team_trials", None, "selection-procedure points table, different layout"),
    "2016-paralympic-trials---final-points": ("team_trials", None, "selection-procedure points table, different layout"),
    "2017-world-games-trial-recurve-and-barebow": ("team_trials", None, "small spreadsheet export"),
    "2017-ntc-jnc-joad-team-results": ("joad_target_nationals", None, "team seeding sheet (no scores)"),
}

# Only used when the PDF's own title block lacks a full date. Each hint cites where it comes from.
DATE_HINTS = {
    # same championship as the 47th Indoor Nationals results book (Feb 12th - Mar 13th, 2016)
    "2016-collegiate-results": ("2016-02-12", "2016-03-13", "title block of the 47th U.S. National Indoor results book (same championship)"),
    # the ASA report prints "Event Date 10/28/2017"
    "2017-national-3-d-collegiate-championships-individual-results": ("2017-10-28", "2017-10-28", "'Event Date 10/28/2017' printed in the document"),
    # USA Archery news: "National Target Championships and JOAD Nationals will take place August 2-6 in Westfield, Indiana"
    "2017-ntc-jnc-results-book": ("2017-08-02", "2017-08-06", "usarchery.org news: USA Archery Kicks Off 133rd National Target Championships"),
}

_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_SECTION_RX = re.compile(r"^(Indoor|Outdoor|Field|3D)\s*-\s*(.+)$")
_DATE_RX = re.compile(r"([A-Z][a-z]{2})\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*-\s*([A-Z][a-z]{2})\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?)?,?\s+(\d{4})")


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def fetch(slugs=None, min_interval: float = 1.5) -> list[dict]:
    LEGACY_RAW.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(min_interval)
    manifest_path = LEGACY_RAW / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for slug in slugs or SOURCES:
        dest = LEGACY_RAW / f"{slug}.pdf"
        if slug in manifest and (dest.exists() or has_parsed_cache(slug, manifest[slug]["sha256"])):
            continue          # PDF present, or its parsed rows are cached - nothing to download
        limiter.wait()
        req = urllib.request.Request(BASE + slug, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read()
            final_url = r.geturl()
            ctype = r.headers.get("Content-Type", "")
        if not body.startswith(b"%PDF"):
            print(f"  !! {slug}: not a PDF ({ctype}, {len(body)} bytes)")
            continue
        dest.write_bytes(body)
        manifest[slug] = {"source_url": BASE + slug, "resolved_url": final_url, "bytes": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print(f"  fetched {slug} ({len(body) / 1e6:.1f} MB)")
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return list(manifest.values())


# --------------------------------------------------------------------------- #
# Archerz parser
# --------------------------------------------------------------------------- #
@dataclass
class _Cols:
    rank_x: float
    name_x: float
    club_x: float
    country_x: float
    score_x: float
    n_after_score: int          # header labels after "Score" (round columns + 2 tie-breaks)
    labels: list[str]


def _lines(words, tol=2.5):
    """Group words into visual lines (by `top`)."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    for ln in out:
        ln.sort(key=lambda w: w["x0"])
    return out


def _parse_dates(text: str):
    m = _DATE_RX.search(text or "")
    if not m:
        return None, None
    m1, d1, m2, d2, y = m.groups()
    try:
        y = int(y)
        start = f"{y}-{_MONTHS[m1[:3]]:02d}-{int(d1):02d}"
        end = f"{y}-{_MONTHS[(m2 or m1)[:3]]:02d}-{int(d2 or d1):02d}"
        return start, end
    except KeyError:
        return None, None


def parse_archerz(path: Path) -> dict:
    import pdfplumber

    rows: list[dict] = []
    title_parts: list[str] = []
    section = None
    cols: _Cols | None = None
    n_pages = 0
    skipped = 0
    with pdfplumber.open(str(path)) as pdf:
        for pi, page in enumerate(pdf.pages):
            n_pages += 1
            words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
            lines = _lines(words)

            # locate section headers / column headers on this page
            events = []  # (top, kind, payload)
            for ln in lines:
                text = " ".join(w["text"] for w in ln)
                if ln[0]["x0"] < 120 and _SECTION_RX.match(text) and text.count(" - ") >= 3:
                    events.append((ln[0]["top"], "section", text))
                elif any(w["text"] == "Rank" for w in ln) and any(w["text"] == "Name" for w in ln):
                    events.append((ln[0]["top"], "header", ln))
            if pi == 0:
                first_top = events[0][0] if events else 9999
                for ln in lines:
                    if ln[0]["top"] < first_top - 2:
                        t = " ".join(w["text"] for w in ln if w["x0"] < 470)
                        if t and not re.search(r"Created By|Sponsored By|www\.|^USA Archery$", t):
                            title_parts.append(t)

            # walk the page top-to-bottom, switching section/header state
            bounds = sorted(events, key=lambda e: e[0]) + [(page.height + 1, "end", None)]
            # rows before the first event on a continuation page belong to the carried-over section
            seg_start = 0.0
            seg_state = (section, cols)
            for ev_i, (top, kind, payload) in enumerate(bounds):
                seg_rows = _extract_rows(words, seg_start, top, seg_state[1])
                for r in seg_rows:
                    if seg_state[0] is not None:
                        rows.append({**r, **seg_state[0]})
                seg_start = top
                if kind == "header":
                    # start the next segment *below* the header line so its words never join row 1
                    seg_start = max(w["bottom"] for w in payload) + 0.5
                if kind == "section":
                    parts = [p.strip() for p in payload.split(" - ")]
                    if len(parts) >= 4:
                        seg_state = ({"season": parts[0], "division_raw": parts[1], "class_raw": parts[2],
                                      "gender_raw": parts[3], "round_desc": " - ".join(parts[4:])}, seg_state[1])
                    section = seg_state[0]
                elif kind == "header":
                    cols = _read_cols(payload)
                    seg_state = (seg_state[0], cols)
    return {"rows": rows, "title": re.sub(r"\s+", " ", " ".join(title_parts)).strip(), "pages": n_pages}


def _read_cols(ln) -> _Cols | None:
    pos = {}
    labels_after: list[str] = []
    score_x = None
    for w in ln:
        if w["text"] in ("Rank", "Name", "Club", "Country") and w["text"] not in pos:
            pos[w["text"]] = w["x0"]
        if w["text"] == "Score" and score_x is None:
            score_x = w["x0"]
        elif score_x is not None:
            labels_after.append(w["text"])
    if not {"Rank", "Name", "Club", "Country"} <= pos.keys() or score_x is None:
        return None
    return _Cols(pos["Rank"], pos["Name"], pos["Club"], pos["Country"], score_x, len(labels_after), labels_after)


_NUM = re.compile(r"^\d+$")


def _extract_rows(words, y0: float, y1: float, cols: _Cols | None) -> list[dict]:
    if cols is None:
        return []
    seg = [w for w in words if y0 <= w["top"] < y1 - 0.1]
    # rank anchors: integers well left of the name column
    anchors = [w for w in seg if _NUM.match(w["text"]) and w["x0"] < cols.name_x - 20]
    if not anchors:
        return []
    anchors.sort(key=lambda w: w["top"])
    tops = [a["top"] for a in anchors]
    # band edges: midpoints between consecutive anchors; first/last extend to the segment edges
    edges = [y0] + [(tops[i] + tops[i + 1]) / 2 for i in range(len(tops) - 1)] + [y1]
    b_name_club = (cols.name_x + cols.club_x) / 2
    country_candidates = [w["x0"] for w in seg if re.fullmatch(r"[A-Z]{3}", w["text"]) and cols.country_x - 8 <= w["x0"] <= cols.country_x + 70]
    country_left = min(country_candidates) if country_candidates else cols.country_x
    b_club_country = country_left - 4
    out = []
    for i, a in enumerate(anchors):
        band = [w for w in seg if edges[i] <= w["top"] < edges[i + 1]]
        name_w = sorted([w for w in band if a["x0"] + 8 < w["x0"] < b_name_club], key=lambda w: (round(w["top"]), w["x0"]))
        club_w = sorted([w for w in band if b_name_club <= w["x0"] < b_club_country], key=lambda w: (round(w["top"]), w["x0"]))
        ctry_w = [w for w in band if b_club_country <= w["x0"] < cols.score_x - 12]
        # numeric columns come from the anchor's own line only: wrapped tie-break digits can
        # sit on neighbouring lines (e.g. Field results) and must not shift the columns.
        nums = [w["text"] for w in sorted(band, key=lambda w: w["x0"])
                if w["x0"] >= cols.score_x - 12 and abs(w["top"] - a["top"]) <= 1.5]
        out.append(
            {
                "rank": int(a["text"]),
                "name_raw": " ".join(w["text"] for w in name_w).strip(),
                "club_raw": " ".join(w["text"] for w in club_w).strip(),
                "country": " ".join(w["text"] for w in ctry_w).strip(),
                "nums": nums,
                "labels": cols.labels,
            }
        )
    return out



# --------------------------------------------------------------------------- #
# 2016 Indoor Nationals Collegiate: spreadsheet (Excel) export
#   Rank | Bow type | Gender | First Name | Last Name | Club | Location | (18m,10,9) x4 | Final score | 10 | 9
# Excel bottom-aligns wrapped cells, so a row owns the lines between the previous
# rank anchor and its own.
# --------------------------------------------------------------------------- #
def parse_collegiate_sheet(path: Path) -> dict:
    import pdfplumber

    rows: list[dict] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            # Excel only prints the header row on some pages; continuation pages have none, so every
            # page is parsed (rows are anchored on the rank number, not on the header).
            # A new class section can start mid-page, so a header row may sit in the MIDDLE of a page:
            # exclude only the header lines themselves (and the title above the first header on page 1).
            hdr_tops = [w["top"] for w in words if w["text"] == "Rank"]
            first_page = page.page_number == 1
            body = [w for w in words
                    if not any(abs(w["top"] - h) <= 5 for h in hdr_tops)
                    and not (first_page and hdr_tops and w["top"] < min(hdr_tops))]
            anchors = sorted([w for w in body if _NUM.match(w["text"]) and w["x0"] < 42], key=lambda w: w["top"])
            prev_top = -1.0
            for a in anchors:
                band = [w for w in body if prev_top + 1.5 < w["top"] <= a["top"] + 1.5]
                prev_top = a["top"]

                def col(lo, hi):
                    return " ".join(w["text"] for w in sorted(band, key=lambda w: (round(w["top"]), w["x0"])) if lo <= w["x0"] < hi).strip()

                nums = [w["text"] for w in sorted(band, key=lambda w: w["x0"]) if w["x0"] >= 430 and abs(w["top"] - a["top"]) <= 1.5]
                rows.append({
                    "season": "Indoor", "division_raw": col(42, 88), "class_raw": "Collegiate", "gender_raw": col(88, 120),
                    "round_desc": "18m double round", "rank": int(a["text"]),
                    "name_raw": f'{col(118, 185)} {col(185, 248)}'.strip(), "club_raw": col(248, 370), "country": "USA",
                    # (r,10,9) x4 then final,10,9  ->  [final, r1..r4, 10s, 9s] to match the Archerz row shape
                    # full card: 4 x (score,10s,9s) + final,10s,9s = 15 numbers.  Partial card (2 rounds shot): the source
                    # prints only "0 0" for each missing round -> 13 numbers; rounds 3-4 are zero (card flagged incomplete).
                    "nums": (([nums[12]] + [nums[0], nums[3], nums[6], nums[9]] + nums[13:15]) if len(nums) == 15 else
                             ([nums[10], nums[0], nums[3], "0", "0", nums[11], nums[12]]) if len(nums) == 13 else nums),
                    "labels": ["18m", "18m", "18m", "18m", "10", "9"],
                })
    return {"rows": rows, "title": "Final Results - 2016 U.S. National Indoor - Collegiate", "pages": len(rows)}


# --------------------------------------------------------------------------- #
# 2017 Collegiate 3D Championship (ASA event software): one class per page
#   Rank | ASA No | First | Last | Club | Day 1 | 12s | Day 2 | 12s | Shootdown | 12s | Total | 12s
# --------------------------------------------------------------------------- #
def parse_asa_3d(path: Path) -> dict:
    import subprocess

    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout
    rows: list[dict] = []
    cur = None
    for raw in out.splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+-\s+Collegiate Championship \d{4}\s+-\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)                       # e.g. "Female Barebow"
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if cur and len(parts) == 13 and parts[0].isdigit() and parts[1].isdigit():
            gender, _, bow = cur.partition(" ")
            d1, c1, d2, c2, sd, sdc, tot, tc = parts[5:13]
            nums = [tot, d1, d2, tc, sdc if sdc.isdigit() else "0"]
            rows.append({
                "season": "3D", "division_raw": bow, "class_raw": "Collegiate", "gender_raw": gender,
                "round_desc": "3D 2-day", "rank": int(parts[0]), "name_raw": f"{parts[2]} {parts[3]}".strip(),
                "club_raw": parts[4], "country": "USA", "nums": nums, "labels": ["Day 1", "Day 2", "12s", "Shootdown 12s"],
            })
    return {"rows": rows, "title": "2017 USA Archery Collegiate 3D Championship - Individual", "pages": 0}


PARSERS = {"archerz": None, "collegiate_sheet": None, "asa_3d": None}   # filled below (after parse_archerz exists)

# --------------------------------------------------------------------------- #
# parse cache: the PDFs are not committed (third-party, ~60 MB) but their parsed rows are,
# keyed by the PDF's SHA-256, so the whole dataset rebuilds from the repo alone.
# --------------------------------------------------------------------------- #
PARSED_DIR = LEGACY_RAW / "parsed"


def _parsed_path(slug: str) -> Path:
    return PARSED_DIR / f"{slug}.json.gz"


def has_parsed_cache(slug: str, sha256: str) -> bool:
    p = _parsed_path(slug)
    if not p.exists():
        return False
    try:
        return json.loads(gzip.open(p).read()).get("sha256") == sha256
    except Exception:  # noqa: BLE001 - corrupt cache => reparse
        return False


def _parser_for(slug: str):
    return {"archerz": parse_archerz, "collegiate_sheet": parse_collegiate_sheet, "asa_3d": parse_asa_3d}[SOURCES[slug][1]]


def parse_cached(slug: str, sha256: str) -> dict:
    p = _parsed_path(slug)
    if has_parsed_cache(slug, sha256):
        return json.loads(gzip.open(p).read())["result"]
    pdf = LEGACY_RAW / f"{slug}.pdf"
    if not pdf.exists():
        raise FileNotFoundError(f"{pdf} missing and no parsed cache for it - run `python -m pipeline.legacy fetch`")
    res = _parser_for(slug)(pdf)
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(gzip.compress(json.dumps({"sha256": sha256, "result": res}, separators=(",", ":")).encode(), compresslevel=6, mtime=0))
    tmp.replace(p)
    return res


# --------------------------------------------------------------------------- #
# build tables
# --------------------------------------------------------------------------- #
_ARROWS_PER_COL = {"Indoor": 30, "Outdoor": 36}


def build_tables(proc: Path = config.PROCESSED_DIR) -> dict:
    manifest = json.loads((LEGACY_RAW / "manifest.json").read_text())
    # BE archer lookup for conservative name-based linking
    be_unique: dict[str, str] = {}
    ent_path = proc / "entries.csv.gz"
    if ent_path.exists():
        e = pd.read_csv(ent_path, usecols=["name_key", "archer_id"], low_memory=False)
        per = e.groupby("name_key").archer_id.nunique()
        be_unique = e[e.name_key.isin(per[per == 1].index)].drop_duplicates("name_key").set_index("name_key").archer_id.to_dict()

    tourn_rows, entry_rows = [], []
    for slug, (cat_expected, parser, note) in SOURCES.items():
        meta = manifest.get(slug)
        if not meta:
            continue
        base = {"tournament_key": slug, "source_slug": slug, "source_url": meta["source_url"], "sha256": meta["sha256"],
                "expected_category": cat_expected, "note": note}
        if parser is None:
            tourn_rows.append({**base, "title": "", "category": cat_expected, "start_date": None, "end_date": None, "year": None,
                               "parsed": False, "unparsed_reason": note, "n_rows": 0, "n_rows_rejected": 0, "n_sections": 0, "n_sum_check_failed": 0})
            continue
        res = parse_cached(slug, meta["sha256"])
        title = re.sub(r"^(Sponsored\s+)+|\bUSA Archery\b|\bwww\.\S+", "", res["title"])
        title = re.sub(r"\s+", " ", title).strip()
        start, end = _parse_dates(title)
        date_source = "pdf_title" if start else ""
        if not start and slug in DATE_HINTS:
            start, end, date_source = DATE_HINTS[slug][0], DATE_HINTS[slug][1], DATE_HINTS[slug][2]
        slug_year = int(slug[:4]) if slug[:4].isdigit() else None
        year = int(start[:4]) if start else slug_year
        if not start:
            date_source = "slug_year_only"
        title_cat = classify_name(title).category
        n_fail = n_rejected = 0
        sections = set()
        kept = 0
        for r in res["rows"]:
            nums = [int(x) for x in r["nums"] if _NUM.match(x)]
            labels = r["labels"]
            n_cols = len(labels)
            ok_full = len(nums) == 1 + n_cols and n_cols >= 3
            # "short" rows: tie-break cells wrapped onto another line (seen in the 2017 Field book).
            # Accepted only when the round columns sum exactly to the score - garbage rows won't.
            ok_short = (not ok_full and n_cols >= 3 and len(nums) == n_cols - 1 and len(nums) >= 3
                        and sum(nums[1:]) == nums[0])
            ok_shape = ok_full or ok_short
            # Result books also contain match-card / bracket pages that reuse the same
            # "Rank | Name" header. Anything without the exact numeric shape is rejected
            # (and counted) rather than loaded as a result.
            if not ok_shape or not r["name_raw"] or re.search(r"\bName\b|Country|Quarterfinals|Semifinals|Finals", r["name_raw"] + " " + r["club_raw"]):
                n_rejected += 1
                continue
            kept += 1
            k = kept
            score = nums[0] if nums else None
            rounds = (nums[1:-2] if ok_full else nums[1:]) if ok_shape else []
            tb = nums[-2:] if ok_full else [None, None]
            sum_ok = bool(ok_shape and rounds and sum(rounds) == score)
            n_fail += 0 if sum_ok else 1
            apc0 = _ARROWS_PER_COL.get(r["season"])
            max0 = apc0 * len(rounds) * 10 if (apc0 and rounds) else None
            plausible = bool(score is not None and score > 0 and (max0 is None or score <= max0))
            pg = parse_group(f"{r['division_raw']} {r['class_raw']} {r['gender_raw']} {r.get('round_desc', '')}")
            season = r["season"]
            apc = _ARROWS_PER_COL.get(season)
            reg = apc * len(rounds) if apc and rounds else None
            maxs = reg * 10 if reg else None
            nk = name_key(*(r["name_raw"].split(" ", 1) + [""])[:2]) if r["name_raw"] else ""
            full = r["name_raw"]
            sections.add((r["season"], r["division_raw"], r["class_raw"], r["gender_raw"], r.get("round_desc")))
            archer_id = be_unique.get(_nk_full(full)) or f"N{hashlib.sha1(_nk_full(full).encode()).hexdigest()[:10]}"
            basis = "name_matched_to_alt" if be_unique.get(_nk_full(full)) else "name_only"
            entry_rows.append(
                {
                    "entry_id": f"L:{slug}:{k}", "tournament_key": slug, "season": season,
                    "division": pg.division, "age_class": pg.age_class, "gender": pg.gender, "is_collegiate": pg.is_collegiate or "Collegiate" in r["class_raw"] or "Collegiate" in (r.get("round_desc") or ""),
                    "is_para": pg.is_para, "division_raw": r["division_raw"], "class_raw": r["class_raw"], "gender_raw": r["gender_raw"],
                    "round_desc": r.get("round_desc"), "rank": r["rank"],
                    "display_name": clean_display_name(full, ""), "name_key": _nk_full(full), "club": r["club_raw"].strip(" -"),
                    "country": r["country"], "total_score": score, "round_scores": "|".join(map(str, rounds)),
                    "n_rounds": len(rounds), "regulation_arrows": reg, "max_score_possible": maxs,
                    "tb1": tb[0], "tb2": tb[1], "tb_labels": "|".join(labels[-2:]) if len(labels) >= 2 else "",
                    # Metrics are computed from the TOTAL when it is plausible. In some combined reports the
                    # per-round cells are blank/zero although the total is right, so the split is flagged
                    # (round_scores_reliable) but the total is still used.
                    "avg_arrow": round(score / reg, 4) if (reg and plausible) else None,
                    "pct_of_max": round(score / maxs, 4) if (maxs and plausible) else None,
                    "complete_card": bool(plausible and ((sum_ok and all(x > 0 for x in rounds)) or (maxs and score >= 0.5 * maxs))),
                    "round_scores_reliable": sum_ok, "score_plausible": plausible,
                    "sum_check_ok": sum_ok, "archer_id": archer_id, "identity_basis": basis,
                }
            )
        tourn_rows.append({**base, "title": title, "category": cat_expected, "title_category": title_cat, "date_source": date_source,
                           "start_date": start, "end_date": end, "year": year,
                           "parsed": True, "unparsed_reason": "", "n_rows": kept, "n_rows_rejected": n_rejected,
                           "n_sections": len(sections), "n_sum_check_failed": n_fail})
    tdf = pd.DataFrame(tourn_rows)
    edf = pd.DataFrame(entry_rows)
    proc.mkdir(parents=True, exist_ok=True)
    tdf.to_csv(proc / "legacy_tournaments.csv", index=False, lineterminator="\n")
    edf.to_csv(proc / "legacy_entries.csv.gz", index=False, compression={"method": "gzip", "mtime": 0, "compresslevel": 6}, lineterminator="\n")
    print(f"legacy: {len(tdf)} documents ({int(tdf.parsed.sum())} parsed) -> {len(edf):,} result rows; "
          f"sum-check failures: {int(edf.sum_check_ok.eq(False).sum()) if len(edf) else 0}")
    return {"documents": len(tdf), "parsed": int(tdf.parsed.sum()), "rows": len(edf)}


def _nk_full(full_name: str) -> str:
    """name_key for a single 'First Last' string (same normalisation as identity.name_key)."""
    return name_key(full_name, "")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.legacy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=["fetch", "parse", "all"])
    a = p.parse_args(argv)
    if a.stage in ("fetch", "all"):
        fetch()
    if a.stage in ("parse", "all"):
        build_tables()
    return 0


if __name__ == "__main__":
    sys.exit(main())
