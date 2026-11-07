# Methodology

How the data was collected, cleaned and organised — and the reasoning behind each judgement call. Numbers that change
with each build live in the generated documents ([COVERAGE](COVERAGE.md), [TRENDS](TRENDS.md), [VALIDATION](VALIDATION.md)).

## 1. Sources

| source | what | how it was accessed |
|---|---|---|
| **Between Ends** (`betweenends.com`) | tournament results, arrow by arrow, Dec 2017 → today | The public site is a static AngularJS shell; every page loads its data from a JSON API at `resultsapi.herokuapp.com`. The endpoints were discovered by reading the site's own page scripts (nothing was guessed or reverse-engineered beyond that): `GET /tournaments/`, `/tournaments/<id>`, `/events/<id>`, `/events/<id>/scores`. |
| **USA Archery results archive** (`usarchery.org/resource-center/events/national-event-results`) | 2016-17 results books (PDF) for the nationals, USAT-series precursors and team trials | Catalogued from the archive page; PDFs downloaded by their public `/resource/<slug>` URL; parsed by `pipeline/legacy.py`. |

Collection is done with the Between Ends owner's permission and politely: a single request in flight, ≥ 0.5 s between
requests, retry with exponential back-off, an honest `User-Agent`, and a persistent cache so nothing is ever fetched twice.
(Measured during the original run: the service handles requests essentially serially — 2 workers gave ~0.46 req/s with ~6 % 503
time-outs, 1 worker gave ~1.3 req/s with none — so the polite setting is also the fastest.) Every network call is logged to
`data/raw/_fetch_log.jsonl`.

The raw layer (`data/raw/`) stores the **exact bytes returned**, gzip-compressed, so every derived table can be rebuilt offline
(`python -m pipeline.run --skip-collect`) and any number can be traced back to a response.

## 2. Scope

* **Last 5 years, everything:** every tournament overlapping `[as_of − 5 y, as_of]` that has already started — no filtering by type.
* **Past decade, marquee only:** USA Archery nationals (indoor and outdoor: Indoor, Target Nationals + U.S. Open, Field, 3D,
  Collegiate, JOAD, OAS), the **USAT series** (USAT = *USA Archery Team*: USAT #1–#4 + the Series Final each year) and team/Olympic
  trials, over `2016-01-01 … as_of` ("the past decade" is read as the 2016-2026 seasons so no calendar year is cut in half).
* Tournaments scheduled after `as_of` are catalogued (`status = upcoming`) but not fetched — they have no results.
* Between Ends itself only exists from December 2017; the rest of the decade comes from USA Archery's PDFs (§8).

Tournament **categories** are derived from the name (the API has no type field): independent name tags first, then a
precedence-ordered rule (`pipeline/classify.py`). The rules were reviewed against every name in the index; e.g. the *National
Archery Scholarship Tour* (≈130 local shoots whose names contain "National") is deliberately **not** a national championship.

## 3. Reproducing the website's numbers

The API returns arrow strings, not totals or ranks — the website computes both in the browser. `pipeline/scoring.py` is a
line-by-line port of the site's `scoring.js`, and ranking mirrors the event page:

* arrow values (`X`/`T` = 10, `M` = 0, `a`/`b`/`d` = 3D 11/12/14, `W`/`Y`/`Z` NFAA variants, `E` blank);
* the site adds **shoot-off arrows** into the total — we keep both `total_score` (as ranked) and `regulation_score`;
* typed round totals (`rtl`) and manual tie-break counts (`tbs`) override arrow-derived values;
* ranking within each group: total, then tie-breakers of the event's scoring rule, ties share a rank (`1,1,3`).

One deliberate deviation: where the site recomputes a missing round total from a partially filled `rtl` it slices the arrow
string by the wrong index (a round number where an end number is expected). We slice by round correctly.

**Independent confirmation.** The validator compares our recomputed scores with results USA Archery published separately
(e.g. 2020 Indoor Nationals: Gellenthien 1196, Ellison 1191; 2021: Douglas 598, Kaufhold 583) — all match exactly. See
[VALIDATION](VALIDATION.md).

## 4. One performance, counted once

Results are published more than once. Indoor Nationals runs at many sites (each its own tournament with a `RankingEvent`) and
is *also* re-published in "Combined Results" tournaments (`CombinedRankingEvent`); Target Nationals publishes a combined
qualification and a class/division sheet in the same tournament.

A **performance key** (`perf_key`) = hash of the arrow string (≥ 24 arrows); entries with the same `archer_id` + `perf_key` are the
same physical performance. One is marked `is_canonical` (preferring a site `RankingEvent` over a combined event, then the lowest
event id); the rest keep `duplicate_of_entry_id`. **Nothing is deleted** — filter on `is_canonical` for statistics. Entries with
fewer than 24 recorded arrows (typed totals, partial cards) are never deduplicated, so they can only be over-counted, never lost.

## 5. Archer identity (the hard part)

Goal: follow one person across events, years, divisions and spellings **without merging different people** (there are many
homonyms: three different "Justin Kim"s in one Indoor Nationals).

**Evidence from the data.** Each archer record carries `alt`, a numeric person-level id (≈ 99 % populated). One person entered in two
divisions, or as "Archie Nixon - LB", shares one `alt`; homonyms do not. `aid` is only an event-local row. But `alt` is *the id in
whichever registration system the event used*, not one universal key:

* 5–6-digit values recur across tournaments and years (≈ 74 % of 6-digit values appear in ≥ 2 tournaments) — consistent with stable
  USA Archery member numbers (this is an inference; the source does not say so);
* other lengths behave like per-season / per-organisation numbers: a single international guest appears under **14 different
  4-digit alts** over 2018-26 (monotonically increasing), and under two alts within one tournament; 7-digit values appear almost
  only in 2026 events run through other organisations; 3-digit values are shared by unrelated people.

**Algorithm** (`pipeline/identity.py`; every decision that is not a plain `alt` match is recorded in `identity_review.csv.gz`):

1. **alt → archer** `A<alt>`. Names under one alt are clustered with a similarity-aware test (typos, nicknames, middle names, suffixes,
   swapped order). Compatible names stay together (`mackenzie`/`makenzie`, `bella`/`isabella`); clearly different names are split into
   separate archers (`A<alt>~2`) — two people on one number. A 3-4-digit alt with conflicting names, or any alt with > 4 incompatible
   names, is treated as *not a person id*.
2. **Strong vs weak ids.** "Strong" = 5-6 digits **and** corroborated (seen in a marquee USA Archery event — those require a membership —
   or persisting across ≥ 2 calendar years). Everything else is "weak".
3. **Bridge weak ids** into a person only on positive evidence: same normalised name, compatible gender, overlapping club (or, if a
   club is missing, an unambiguous shared division with no tournament overlap). Two strong ids are **never** merged. Ambiguity → stays split.
4. **Blank-id records** join an existing archer only if that exact name belongs to exactly one *final* archer; otherwise they become a
   name-only archer `N<hash>`.
5. **Name bridge** (labelled `name_bridged`, medium confidence): a leftover fragment joins the single strong-id archer with the same
   exact name if gender is compatible, it never appears in the same event under a different id, and there is no youth/adult conflict in
   overlapping years.

`archers.identity_basis` and `entries.identity_basis` expose the weakest evidence used (`alt` > `alt_bridged` > `name_matched_to_alt` >
`name_bridged` > `name_only(_ambiguous)`). **For rigorous per-archer trends, filter to `alt`/`alt_bridged`.**

Known limits: a person who changed membership number, or who shoots under unrelated ids in several systems with no club overlap, will
appear as more than one archer; two people with the same name and no distinguishing evidence in a local series may be merged by the
name bridge only when exactly one strong archer carries the name. The review table lists every such call.

## 6. Classes and divisions

Group names are free text and changed over time (2018 "Recurve Cadet Men" → 2026 "Recurve U18 Men"). `pipeline/groups.py` maps them to a
canonical vocabulary: divisions (`Recurve`, `Compound`, `Barebow`, `Fixed Pins`, `Traditional Recurve`, `Longbow`, `Compound Fingers`,
`Crossbow`, `Para …`), age classes on WA-style codes (`Junior → U21`, `Cadet → U18`, `Cub → U15`, `Bowman → U13`, `Master → 50+` — the
mapping is confirmed by the data, where 2025-26 names read "U21 (Junior)", "U13 (Bowman)"), and `(Guest)`/collegiate/para flags. S3DA
("Pins Eagle Men", "Jr. Eagle") and school-league ("Middle School Girls") vocabularies have their own classes so they are never confused with
WA age classes. Unrecognised names stay `Other`/`Unknown` and are listed by the validator.

## 7. Trends

Comparisons are made **within a `format_family`** on `avg_arrow` (points per arrow), using complete, canonical, non-guest cards only. Archer
trends are an OLS slope of points-per-arrow on time; a series is `improving`/`declining` only when |slope / SE| ≥ 2 (≥ 3 performances),
otherwise `stable`. Caveats: different distances (60 m vs 70 m) within `outdoor_wa`, and the move between age classes, shift averages
independent of skill — compare within a division and age class for sharp conclusions.

## 8. The pre-2018 gap

2016-17 marquee results come from USA Archery's PDF results books, most of which are exports from the *Archerz* system. The text layer
has spurious spaces inside words ("Alfre d"), so the parser reads **word positions** (pdfplumber), assigns words to rows by vertical band
(handling names that wrap onto neighbouring lines), and separates columns by x-position. Quality gates:

* the round scores of every row must sum to the printed total (99 %+ pass; the failures are a documented quirk of the combined 2017 Final
  report, which prints only the first round cell — the total is correct and is used);
* rows without the exact numeric shape (the books also contain match-card pages) are **rejected and counted**, not loaded;
* documents in other layouts (ianseo books, spreadsheet exports, garbled text, selection-point tables) are listed with a reason and not guessed at.

These rows have **no arrow data and no member number**; archers are linked to their Between Ends histories only when the exact name is
unambiguous (otherwise a name-only id), so 2016-17 appearances of a person using a very common name may not join their later history.
Source PDFs are not committed (≈ 60 MB, third-party); `data/raw/legacy_usarchery/manifest.json` records each one's URL and SHA-256.

## 9. Validation

`python -m pipeline.validate` runs structural (unique keys, referential integrity), coverage (every in-scope event collected), scoring
(possible scores, rank sanity, round-sum consistency), dedupe, identity, legacy-parser and **external** checks, and writes
[VALIDATION.md](VALIDATION.md). It exits non-zero on any FAIL. Anything the pipeline cannot reconcile at the source (impossible typed totals,
events whose declared scoring rule contradicts their arrows) is **flagged, kept, and excluded from statistics** — never silently dropped or "fixed".
