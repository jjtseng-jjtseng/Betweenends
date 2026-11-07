# Validation report (data as of 2026-11-07)

Summary: {'PASS': 47, 'WARN': 3}

| family | check | status | detail |
|---|---|---|---|
| structure | tables are non-empty | **PASS** |  |
| structure | tournaments.tournament_id unique | **PASS** |  |
| structure | events.event_id unique | **PASS** |  |
| structure | entries.entry_id unique | **PASS** |  |
| structure | events -> tournaments | **PASS** |  |
| structure | entries -> events | **PASS** |  |
| structure | entry_groups -> entries | **PASS** |  |
| structure | entry_arrows -> entries | **PASS** |  |
| structure | match_rows.match_row_id unique | **PASS** |  |
| structure | match_rows -> events | **PASS** |  |
| structure | archers.archer_id unique | **PASS** |  |
| structure | entries.archer_id all in archers | **PASS** |  |
| coverage | in-scope tournaments | **PASS** | 1473 tournaments |
| coverage | every in-scope tournament has its detail collected | **PASS** |  |
| coverage | every in-scope tournament has >=1 event or is documented empty | **PASS** |  |
| coverage | no 'not collected' events | **PASS** |  |
| coverage | no unparsed events | **PASS** |  |
| coverage | ranking events with registered archers but no scores | **PASS** | 0 of 1815 ranking events carry no arrow data at source (tournament created, scoring never used) |
| coverage | upcoming tournaments catalogued but not fetched | **PASS** | 40 |
| coverage | expected marquee categories present for each year | **PASS** | missing (category, year): [] |
| scoring | entries whose score exceeds their event's own format maximum (source inconsistency, flagged and excluded from stats) | **WARN** | 648 entries in 14 events; all flagged score_exceeds_format |
| scoring | events whose declared scoring rule is contradicted by the arrows shot (observed max used instead) | **WARN** | 7 events: [1692, 2458, 4754, 5215, 5481, 6501] |
| scoring | no complete_card entry exceeds the maximum | **PASS** |  |
| scoring | totals-only entries (no arrows) counted as complete performances | **PASS** | 13,351 |
| scoring | scores non-negative | **PASS** |  |
| scoring | sum(round totals) == regulation score | **PASS** |  |
| scoring | avg_arrow within [0, max arrow value] | **PASS** |  |
| scoring | every complete card has an avg_arrow | **PASS** |  |
| scoring | pct_of_max within [0, 1] | **PASS** |  |
| scoring | ranks within group size | **PASS** |  |
| scoring | rank 1 exists in every ranked group | **PASS** |  |
| scoring | arrow strings use legal symbols | **PASS** |  |
| groups | group names resolve to a division | **WARN** | 1.66% of group rows unparsed; most common: ['Scoring Member', 'League Unisex', 'Novice Cloverleaf', 'Learning Division Learning Class', 'Combined Ranking', 'Division 6 – Intermediate Novice'] |
| groups | age class resolved | **PASS** | 1.07% unknown; e.g. ['Scoring Member', 'League Unisex', 'Novice Cloverleaf', 'Learning Division Learning Class', 'Combined Ranking'] |
| dedupe | non-canonical (duplicate) performances | **PASS** | 95,087 of 304,023 entries (31.3%) are the same physical performance as a canonical entry |
| dedupe | every duplicate points at an existing canonical entry | **PASS** |  |
| dedupe | duplicates share the archer and arrow string with their canonical entry | **PASS** |  |
| dedupe | no canonical performance repeated (archer + arrows) | **PASS** |  |
| identity | basis mix | **PASS** | {'alt': 237316, 'name_matched_to_alt': 25083, 'name_only': 17844, 'name_bridged': 11730, 'alt_bridged': 6332, 'name_only_ambiguous': 5718} |
| identity | alts that map to >1 archer (split by incompatible names) | **PASS** | 244 |
| identity | archers | **PASS** | 41,668 archers; bridged-alt 41,668 |
| legacy | parsed documents have rows | **PASS** |  |
| legacy | round scores sum to total (parser integrity) | **PASS** |  |
| legacy | documents not parsed (documented) | **PASS** | 7 of 25 |
| external | 2025 Target Nationals/U.S. Open: Brady Ellison won Recurve Senior Men (double-gold weekend) | **PASS** | published place 1, inferred 1.0 (https://www.usarchery.org/news/2025-a-year-of-depth-development-and-momentum-for-usa-archery-athletes) |
| external | 2025 Target Nationals/U.S. Open: Catalina Gnoriega won Recurve Senior Women (double-gold weekend) | **PASS** | published place 1, inferred 1.0 (https://www.usarchery.org/news/2025-a-year-of-depth-development-and-momentum-for-usa-archery-athletes) |
| external | 2020 Indoor Nationals: Braden Gellenthien 1196 (compound senior men) | **PASS** | published 1196, we compute 1196 (https://usarchery.org/news/usa-archery-indoor-national-champions-awarded-at-2020-season-opener) |
| external | 2020 Indoor Nationals: Brady Ellison 1191 (recurve senior men) | **PASS** | published 1191, we compute 1191 (https://usarchery.org/news/usa-archery-indoor-national-champions-awarded-at-2020-season-opener) |
| external | 2021 Indoor Nationals: Kyle Douglas 598 (compound senior men) | **PASS** | published 598, we compute 598 (https://www.usarchery.org/news/usa-archery-crowns-2021-indoor-national-champions) |
| external | 2021 Indoor Nationals: Casey Kaufhold 583 (recurve senior women) | **PASS** | published 583, we compute 583 (https://www.usarchery.org/news/usa-archery-crowns-2021-indoor-national-champions) |
