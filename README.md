# Between Ends — archery results dataset

A complete, analysis-ready snapshot of archery results from [Between Ends](https://www.betweenends.com/) (collected with the
owner's permission), organised **per archer** so you can see who competed, where, and how they are trending — plus a
rebuildable pipeline.

**Snapshot date: 2026-11-07.**

| | |
|---|---|
| **Every competition in the last 5 years** (Nov 2021 → Nov 2026) | 1,422 tournaments, complete as published: every event, every archer, every arrow that was entered |
| **Nationals + USAT + team trials, past decade** (2016 → 2026) | 174 marquee tournaments from Between Ends (Dec 2017 →) + **12,942 result rows** from USA Archery's 2016-17 PDF results books |
| Events / result entries | 2,763 events; 304,023 entries → **208,936 canonical performances** (the rest are duplicates, see below) |
| Arrow-by-arrow records | 274,152 entries |
| Elimination matches | 152,313 match rows |
| Archers | 41,668 resolved people (38,840 in the Between Ends era; 2,849 linked across both eras) |

"Nationals" = USA Archery's national championships: **Indoor** (incl. JOAD & Collegiate), **Target Nationals / U.S. Open**
(incl. JOAD), **Field**, **3D**, **Collegiate Target & 3D**, **OAS**. **USAT** = the *USA Archery Team* series (USAT #1–#4 + the
Series Final each year). Team/Olympic **trials** are included as marquee events. See [COVERAGE](docs/COVERAGE.md) for the per-year
breakdown and every known gap.

## Start here

```python
import pandas as pd
P = "data/processed/"
rd = lambda f, **kw: pd.read_csv(P + f, low_memory=False, **kw)
archers = rd("archers.csv.gz", dtype={"alt_id": str, "alt_ids": str})
trends  = rd("archer_trends.csv.gz")
marquee = rd("marquee_results.csv.gz")                           # every result at nationals / USAT / trials
entries = rd("entries.csv.gz", dtype={"alt_id": str, "alt_raw": str, "perf_key": str})
entries = entries[entries.is_canonical]                          # <- always: counts each performance once

# Who is Brady Ellison and how is he doing?
a = archers[archers.display_name == "Brady Ellison"].iloc[0]
print(a[["archer_id", "first_date", "last_date", "n_national_championships", "n_usat_events"]])
print(trends[(trends.archer_id == a.archer_id) & (trends.n_performances >= 3)]
      [["division", "format_family", "n_performances", "mean_avg_arrow", "slope_per_year", "trend"]])

# Who competed at the 2025 Target Nationals (Senior Recurve Men), and how did they place?
m = marquee[(marquee.championship_key == "target_nationals:2025") & ~marquee.is_guest
            & (marquee.division == "Recurve") & (marquee.age_class == "Senior") & (marquee.gender == "M")]
print(m.sort_values("qualification_rank")[["display_name", "total_score", "qualification_rank", "final_place"]].head(10))
```

More: [what the data says](docs/TRENDS.md) (participation, scoring level, top competitors, improvers),
[data dictionary](docs/DATA_DICTIONARY.md), [methodology](docs/METHODOLOGY.md), [validation report](docs/VALIDATION.md).

**Open in Excel / Sheets:** the tables are gzip CSV (smaller, and pandas/R/DuckDB read them directly). Excel can't open `.gz`; either
gunzip (`gunzip -k data/processed/archers.csv.gz`) or convert one table:
`python -c "import pandas as pd; pd.read_csv('data/processed/archers.csv.gz').to_excel('archers.xlsx', index=False)"`.
Start with `archers`, `archer_trends`, `marquee_results`, `yearly_summary` — the other tables are the detail behind them.

## What's in `data/`

```
data/processed/            tidy tables (gzip CSV) — see docs/DATA_DICTIONARY.md
  tournaments, events      catalogue (all 1,572 indexed tournaments; 1,473 in scope & collected)
  entries                  fact table: one row per archer per ranking event (+ entry_groups, entry_arrows)
  match_rows               elimination brackets / round robins          custom_points   team & points tables
  archers                  one row per person            archer_seasons / archer_trends   history & trajectory
  marquee_results          every result at nationals / USAT / trials    yearly_summary   participation by year
  legacy_*                 2016-17 results parsed from USA Archery PDFs
  identity_review          every identity judgement call, for audit
data/raw/                  exact API responses (gzip JSON) + fetch log — rebuild everything offline from these
  legacy_usarchery/        manifest.json (URL + SHA-256) of the 25 source PDFs; the PDFs themselves are not committed
pipeline/                  the code (collect → normalize → archers → validate → report)
docs/                      methodology, data dictionary, coverage, trends, validation
```

## Three things to know before analysing

1. **Use `is_canonical == True`.** The same performance is published several times (each Indoor Nationals site *and* a Combined-results
   tournament; Target Nationals' combined qualification *and* class sheet). Raw `entries` over-counts by ≈ 45 %; for the 2023 Indoor
   Nationals 15,471 raw entries are 3,495 real performances. Duplicates are flagged, never deleted.
2. **Identity has confidence levels.** `archer_id` is built from the source's person-level number (`alt`) with careful bridging; see
   [METHODOLOGY §5](docs/METHODOLOGY.md#5-archer-identity-the-hard-part). About 78 % of entries rest on a direct `alt` match. For rigorous
   per-archer trends filter `identity_basis` to `alt` / `alt_bridged`; `identity_review` lists every judgement call.
3. **Compare like with like.** `avg_arrow` (points per arrow) is comparable within a `format_family`; scores over different arrow counts are
   not. `complete_card` / `pct_of_max` already restrict to full cards.

## Rebuild / refresh

```bash
python -m pipeline.run                         # collect (polite, resumable) → normalize → archers → validate → report
python -m pipeline.run --skip-collect          # re-derive every table offline from data/raw (≈ 9 min)
python -m pipeline.collect status              # what is cached vs expected
python -m pipeline.validate                    # integrity + external spot checks (exit 1 on FAIL)
```

Requires Python ≥ 3.10, `pandas`, `numpy`, `pdfplumber` (legacy PDFs only). The collector is a single polite client (≈ 1 request/s,
retry/back-off, honest User-Agent, nothing is fetched twice); a full first-time collection took roughly 1½ hours of wall-clock time. The Between Ends API is a small
independently-run service — please keep the defaults.

Network access needed to *collect* (not to rebuild): `www.betweenends.com`, `resultsapi.herokuapp.com`, `www.usarchery.org`.

## Verified against independent results

The pipeline recomputes every score from raw arrows. The validator compares those with results USA Archery published separately — e.g. the
2020 Indoor Nationals (Gellenthien **1196**, Ellison **1191**) and 2021 (Douglas **598**, Kaufhold **583**) — and they match exactly
([validation report](docs/VALIDATION.md)).

## Honest limitations

* **2016 Target Nationals** are not listed on USA Archery's results-archive page (only the 2017 NTC/JNC book is), and 7 of the 25 catalogued 2016-17 PDFs could not be parsed
  (garbled font needing OCR, an ianseo book, selection-point tables, per-archer cards, a team-seeding sheet) — listed with reasons in [COVERAGE](docs/COVERAGE.md). 2016-17 rows have no arrow
  data and no member number.
* **COVID**: no Field / Collegiate Nationals in 2020 — real absences, not gaps.
* Some organisers never entered scores, entered impossible totals, or picked the wrong scoring rule; these are **flagged and excluded from
  statistics, not altered** (`has_scores`, `score_exceeds_format`, `rule_mismatch`).
* Elimination `final_place` is inferred for single-elimination brackets and is reliable only for finished events.
* Between Ends is a live service; results can be corrected after this snapshot — refresh with `python -m pipeline.run`.
