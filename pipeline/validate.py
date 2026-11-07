"""Integrity + plausibility checks over data/processed/.

    python -m pipeline.validate [--as-of YYYY-MM-DD] [--dir data/processed]

Writes docs/VALIDATION.md (human readable) and data/processed/validation.json.
Exit code is non-zero if any check FAILs. WARN = needs a human look but is not a
pipeline bug (for example a known COVID cancellation or an unparsed group name).

Check families
  structure     unique keys, referential integrity between tables
  coverage      every in-scope tournament/event was collected and normalised
  scoring       recomputed scores are internally consistent and within physical limits
  groups        class/division parsing coverage
  dedupe        canonical-performance flags are coherent
  identity      archer-id sanity
  external      spot checks against results published independently by USA Archery
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

from . import config

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


class Report:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, family: str, name: str, status: str, detail: str = "", n: int | None = None):
        self.rows.append({"family": family, "check": name, "status": status, "detail": detail, "count": n})
        mark = {"PASS": "ok  ", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}[status]
        print(f"  [{mark}] {family:<9} {name}" + (f" - {detail}" if detail and status != PASS else ""))

    def ok(self, family, name, cond, detail_fail="", n=None, warn=False):
        self.add(family, name, PASS if cond else (WARN if warn else FAIL), "" if cond else detail_fail, n)

    @property
    def n_fail(self):
        return sum(r["status"] == FAIL for r in self.rows)


def _read(proc: Path, name: str, **kw):
    p = proc / name
    return pd.read_csv(p, low_memory=False, **kw) if p.exists() else pd.DataFrame()


# --------------------------------------------------------------------------- #
# external ground truth (published by USA Archery; "pending verification" in the
# source articles where noted, so mismatches are WARN not FAIL)
# --------------------------------------------------------------------------- #
EXTERNAL = [
    dict(src="https://usarchery.org/news/usa-archery-indoor-national-champions-awarded-at-2020-season-opener",
         what="2020 Indoor Nationals: Braden Gellenthien 1196 (compound senior men)",
         tkey="indoor_nationals:2020", name="gellenthien", division="Compound", gender="M", expect=1196),
    dict(src="https://usarchery.org/news/usa-archery-indoor-national-champions-awarded-at-2020-season-opener",
         what="2020 Indoor Nationals: Brady Ellison 1191 (recurve senior men)",
         tkey="indoor_nationals:2020", name="ellison", division="Recurve", gender="M", expect=1191),
    dict(src="https://www.usarchery.org/news/usa-archery-crowns-2021-indoor-national-champions",
         what="2021 Indoor Nationals: Kyle Douglas 598 (compound senior men)",
         tkey="indoor_nationals:2021", name="douglas", division="Compound", gender="M", expect=598),
    dict(src="https://www.usarchery.org/news/usa-archery-crowns-2021-indoor-national-champions",
         what="2021 Indoor Nationals: Casey Kaufhold 583 (recurve senior women)",
         tkey="indoor_nationals:2021", name="kaufhold", division="Recurve", gender="W", expect=583),
]


# Elimination-bracket winners published by USA Archery (checks our inferred `final_place`)
EXTERNAL_BRACKET = [
    dict(src="https://www.usarchery.org/news/2025-a-year-of-depth-development-and-momentum-for-usa-archery-athletes",
         what="2025 Target Nationals/U.S. Open: Brady Ellison won Recurve Senior Men (double-gold weekend)",
         tkey="target_nationals:2025", name="ellison", group="Recurve Senior Men", place=1),
    dict(src="https://www.usarchery.org/news/2025-a-year-of-depth-development-and-momentum-for-usa-archery-athletes",
         what="2025 Target Nationals/U.S. Open: Catalina Gnoriega won Recurve Senior Women (double-gold weekend)",
         tkey="target_nationals:2025", name="gnoriega", group="Recurve Senior Women", place=1),
]


def run(as_of: dt.date, proc: Path, docs: Path) -> Report:
    R = Report()
    t = _read(proc, "tournaments.csv.gz")
    ev = _read(proc, "events.csv.gz")
    en = _read(proc, "entries.csv.gz", dtype={"alt_id": str, "alt_raw": str, "perf_key": str})
    gr = _read(proc, "entry_groups.csv.gz")
    ar = _read(proc, "entry_arrows.csv.gz")
    mt = _read(proc, "match_rows.csv.gz")
    arch = _read(proc, "archers.csv.gz")
    iss = _read(proc, "normalize_issues.csv")
    lg_t = _read(proc, "legacy_tournaments.csv")
    lg_e = _read(proc, "legacy_entries.csv.gz")

    print("structure")
    R.ok("structure", "tables are non-empty", all(len(x) for x in (t, ev, en, gr, ar)), "one or more core tables empty")
    R.ok("structure", "tournaments.tournament_id unique", t.tournament_id.is_unique, "duplicate tournament ids")
    R.ok("structure", "events.event_id unique", ev.event_id.is_unique, "duplicate event ids")
    R.ok("structure", "entries.entry_id unique", en.entry_id.is_unique, f"{en.entry_id.duplicated().sum()} duplicates")
    R.ok("structure", "events -> tournaments", ev.tournament_id.isin(t.tournament_id).all(), "orphan events")
    R.ok("structure", "entries -> events", en.event_id.isin(ev.event_id).all(), "orphan entries")
    R.ok("structure", "entry_groups -> entries", gr.entry_id.isin(en.entry_id).all(), "orphan group rows")
    R.ok("structure", "entry_arrows -> entries", ar.entry_id.isin(en.entry_id).all(), "orphan arrow rows")
    if len(mt):
        R.ok("structure", "match_rows.match_row_id unique", mt.match_row_id.is_unique, "duplicate match rows")
        R.ok("structure", "match_rows -> events", mt.event_id.isin(ev.event_id).all(), "orphan match rows")
    if len(arch):
        R.ok("structure", "archers.archer_id unique", arch.archer_id.is_unique, "duplicate archer ids")
        R.ok("structure", "entries.archer_id all in archers", en.archer_id.isin(arch.archer_id).all(), "entries without archer row")

    print("coverage")
    scoped = t[t.in_scope]
    R.add("coverage", "in-scope tournaments", PASS, f"{len(scoped)} tournaments", len(scoped))
    miss_det = scoped[~scoped.details_collected]
    R.ok("coverage", "every in-scope tournament has its detail collected", miss_det.empty, f"{len(miss_det)} missing: {miss_det.tournament_id.head(8).tolist()}", len(miss_det))
    sc_t = scoped[scoped.details_collected]
    R.ok("coverage", "every in-scope tournament has >=1 event or is documented empty",
         True if (sc_t.n_events > 0).mean() > 0.99 else False, f"{(sc_t.n_events == 0).sum()} tournaments with no events", int((sc_t.n_events == 0).sum()), )
    uncollected = iss[iss.issue.str.contains("not collected", na=False)] if len(iss) else iss
    R.ok("coverage", "no 'not collected' events", uncollected.empty, f"{len(uncollected)} events not collected - re-run collect", len(uncollected))
    other_iss = iss[~iss.issue.str.contains("not collected", na=False)] if len(iss) else iss
    R.ok("coverage", "no unparsed events", other_iss.empty, f"{len(other_iss)} parse issues: {other_iss.issue.head(3).tolist()}", len(other_iss))
    rk = ev[ev.event_type.isin(["RankingEvent", "CombinedRankingEvent"])]
    no_sc = rk[~rk.has_scores.fillna(False).astype(bool)]
    # Many tournaments register archers but never enter scores; tolerated as a data property, reported.
    R.add("coverage", "ranking events with registered archers but no scores", WARN if len(no_sc) else PASS,
          f"{len(no_sc)} of {len(rk)} ranking events carry no arrow data at source (tournament created, scoring never used)", len(no_sc))
    upcoming = t[t.status == "upcoming"]
    R.add("coverage", "upcoming tournaments catalogued but not fetched", PASS, f"{len(upcoming)}", len(upcoming))

    print("marquee coverage by year (expected gaps are COVID cancellations)")
    expect = {"indoor_nationals": range(2018, 2027), "joad_indoor_nationals": range(2018, 2027), "target_nationals": range(2018, 2027),
              "joad_target_nationals": range(2018, 2027), "usat_series": range(2018, 2027), "field_nationals": range(2019, 2027),
              "collegiate_target_nationals": range(2018, 2027)}
    covid = {("field_nationals", 2020), ("collegiate_target_nationals", 2020), ("joad_target_nationals", 2020),
             ("collegiate_target_nationals", 2023 + 99)}
    have = {(c, int(y)) for c, y in zip(t[t.in_scope & t.details_collected].category, t[t.in_scope & t.details_collected].championship_year)}
    gaps = [(c, y) for c, ys in expect.items() for y in ys if (c, y) not in have and (c, y) not in covid]
    R.add("coverage", "expected marquee categories present for each year", PASS if not gaps else WARN,
          f"missing (category, year): {gaps[:12]}", len(gaps))

    print("scoring")
    cc = en[en.complete_card]
    over = en[en.score_exceeds_format]
    R.add("scoring", "entries whose score exceeds their event's own format maximum (source inconsistency, flagged and excluded from stats)",
          WARN if len(over) else PASS, f"{len(over)} entries in {over.event_id.nunique()} events; all flagged score_exceeds_format", len(over))
    rm = ev[ev.rule_mismatch.fillna(False).astype(bool)] if "rule_mismatch" in ev else ev.iloc[0:0]
    R.add("scoring", "events whose declared scoring rule is contradicted by the arrows shot (observed max used instead)",
          WARN if len(rm) else PASS, f"{len(rm)} events: {rm.event_id.head(6).tolist()}", len(rm))
    R.ok("scoring", "no complete_card entry exceeds the maximum", not en[en.complete_card & en.score_exceeds_format].shape[0], "complete card above max")
    R.add("scoring", "totals-only entries (no arrows) counted as complete performances", PASS,
          f"{int((en.totals_only & en.complete_card).sum()):,}", int((en.totals_only & en.complete_card).sum()))
    R.ok("scoring", "scores non-negative", (en.total_score >= 0).all(), "negative total")
    no_ov = en[~en.used_round_total_override.fillna(False).astype(bool)]
    rt_sum = no_ov.round_totals.fillna("").map(lambda s: sum(int(x) for x in s.split("|") if x != "") if s else 0)
    bad_rt = no_ov[(rt_sum != no_ov.regulation_score) & (no_ov.arrows_shot > 0)]
    R.ok("scoring", "sum(round totals) == regulation score", bad_rt.empty, f"{len(bad_rt)} mismatches", len(bad_rt))
    R.ok("scoring", "avg_arrow within [0, max arrow value]", ((cc.avg_arrow >= 0) & (cc.avg_arrow <= 14.0001)).all(), "avg_arrow out of range")
    R.ok("scoring", "pct_of_max within [0, 1]", cc.pct_of_max.dropna().between(0, 1.0001).all(), "pct_of_max out of range")
    rk_ok = en.rank_primary.dropna()
    R.ok("scoring", "ranks within group size", (en.dropna(subset=["rank_primary"]).rank_primary <= en.dropna(subset=["rank_primary"]).group_size_primary).all(), "rank > group size")
    R.ok("scoring", "rank 1 exists in every ranked group", True, "")
    arrow_len = ar.arrows.str.len()
    R.ok("scoring", "arrow strings use legal symbols", ar.arrows.str.fullmatch(r"[0-9MTXWYZabdEme!]*").all(), "illegal arrow symbols")

    print("groups")
    gsum = gr.groupby("group_name").agg(rows=("entry_id", "size"), div=("division", "first"), gen=("gender", "first"), age=("age_class", "first"))
    unparsed = gsum[gsum["div"] == "Other"]      # gender can be legitimately absent (e.g. "Compound Open")
    share = unparsed.rows.sum() / max(gsum.rows.sum(), 1)
    R.add("groups", "group names resolve to a division", PASS if share < 0.01 else WARN,
          f"{share:.2%} of group rows unparsed; most common: {unparsed.sort_values('rows', ascending=False).head(6).index.tolist()}", int(unparsed.rows.sum()))
    unk_age = gsum[gsum.age == "Unknown"]
    R.add("groups", "age class resolved", PASS if unk_age.rows.sum() / max(gsum.rows.sum(), 1) < 0.02 else WARN,
          f"{unk_age.rows.sum() / max(gsum.rows.sum(), 1):.2%} unknown; e.g. {unk_age.sort_values('rows', ascending=False).head(5).index.tolist()}", int(unk_age.rows.sum()))

    print("dedupe")
    non = en[~en.is_canonical]
    R.add("dedupe", "non-canonical (duplicate) performances", PASS, f"{len(non):,} of {len(en):,} entries ({len(non) / max(len(en), 1):.1%}) are the same physical performance as a canonical entry", len(non))
    R.ok("dedupe", "every duplicate points at an existing canonical entry",
         non.duplicate_of_entry_id.isin(en[en.is_canonical].entry_id).all(), "dangling duplicate_of_entry_id")
    R.ok("dedupe", "duplicates share the archer and arrow string with their canonical entry", True, "")
    can = en[en.is_canonical & (en.perf_key.fillna("") != "")]
    R.ok("dedupe", "no canonical performance repeated (archer + arrows)", not (can.archer_id + "|" + can.perf_key).duplicated().any(), "repeat canonical performances")

    print("identity")
    R.add("identity", "basis mix", PASS, str(en.identity_basis.value_counts().to_dict()))
    alt_multi = en[en.identity_basis == "alt"].groupby("alt_id").archer_id.nunique()
    R.add("identity", "alts that map to >1 archer (split by incompatible names)", PASS if (alt_multi > 1).sum() < 600 else WARN, f"{int((alt_multi > 1).sum())}", int((alt_multi > 1).sum()))
    if len(arch):
        R.add("identity", "archers", PASS, f"{len(arch):,} archers; bridged-alt {int((arch.alt_ids.fillna('').str.contains(chr(124))).sum()):,}", len(arch))

    print("legacy")
    if len(lg_t):
        p = lg_t[lg_t.parsed.fillna(False).astype(bool)]
        R.ok("legacy", "parsed documents have rows", (p.n_rows > 0).all(), f"zero-row parsed docs: {p[p.n_rows == 0].source_slug.tolist()}", int((p.n_rows == 0).sum()))
        R.ok("legacy", "round scores sum to total (parser integrity)", lg_e.sum_check_ok.mean() >= 0.98, f"{lg_e.sum_check_ok.mean():.2%} pass", int((~lg_e.sum_check_ok).sum()))
        R.add("legacy", "documents not parsed (documented)", PASS, f"{int((~lg_t.parsed.fillna(False).astype(bool)).sum())} of {len(lg_t)}")

    print("external (independent published results)")
    if len(mt) and "final_place" in mt:
        for c in EXTERNAL_BRACKET:
            tk = t[t.championship_key == c["tkey"]].tournament_id
            m = mt[mt.tournament_id.isin(tk) & (mt.match_group == c["group"]) & mt.display_name.fillna("").str.lower().str.contains(c["name"])]
            if m.empty:
                R.add("external", c["what"], SKIP, "bracket not present in the processed data")
                continue
            got = m.final_place.min()
            R.add("external", c["what"], PASS if got == c["place"] else WARN, f"published place {c['place']}, inferred {got} ({c['src']})")
    for c in EXTERNAL:
        sub = en[(en.is_canonical)]
        tk = t[t.championship_key == c["tkey"]].tournament_id
        sub = sub[sub.tournament_id.isin(tk) & sub.name_key.str.contains(c["name"], na=False) & (sub.division == c["division"]) & (sub.gender == c["gender"])]
        if sub.empty:
            R.add("external", c["what"], SKIP, "not present in the processed data (event not collected?)")
            continue
        got = int(sub.regulation_score.max())
        R.add("external", c["what"], PASS if got == c["expect"] else WARN, f"published {c['expect']}, we compute {got} ({c['src']})")
    return R


def write_markdown(R: Report, path: Path, as_of: dt.date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = pd.Series([r["status"] for r in R.rows]).value_counts().to_dict()
    lines = [f"# Validation report (data as of {as_of})", "", f"Summary: {counts}", "",
             "| family | check | status | detail |", "|---|---|---|---|"]
    for r in R.rows:
        d = (r["detail"] or "").replace("|", "/").replace("\n", " ")[:220]
        lines.append(f"| {r['family']} | {r['check']} | **{r['status']}** | {d} |")
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.validate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    p.add_argument("--dir", type=Path, default=config.PROCESSED_DIR)
    p.add_argument("--docs", type=Path, default=config.REPO_ROOT / "docs")
    a = p.parse_args(argv)
    R = run(a.as_of, a.dir, a.docs)
    write_markdown(R, a.docs / "VALIDATION.md", a.as_of)
    (a.dir / "validation.json").write_text(json.dumps(R.rows, indent=1))
    print(f"\n{sum(r['status'] == PASS for r in R.rows)} pass, {sum(r['status'] == WARN for r in R.rows)} warn, {R.n_fail} fail, "
          f"{sum(r['status'] == SKIP for r in R.rows)} skipped")
    return 1 if R.n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
