# Data dictionary

All tables are in `data/processed/` as gzip CSV (UTF-8, `\n` line endings, deterministic bytes). Read them with
`pandas.read_csv(path)`; **read `alt_id` / `alt_raw` / `perf_key` as strings** (`dtype=str`).

Keys at a glance

```
tournaments (tournament_id) 1──* events (event_id) 1──* entries (entry_id) *──1 archers (archer_id)
                                      │                   ├──* entry_groups   (entry_id, group)
                                      │                   └──1 entry_arrows   (entry_id)
                                      ├──* match_rows     (match_row_id)  ─ archer_id
                                      └──* custom_points  (event_id, ranking_name, aid) ─ archer_id
legacy_tournaments (tournament_key) 1──* legacy_entries (entry_id) ─ archer_id    (2016-17, usarchery.org PDFs)
```

Conventions: booleans are `True/False`; scores are **regulation** scores unless a column says otherwise;
`avg_arrow` is points per arrow and is only comparable **within a `format_family`**.

---

## tournaments.csv.gz — one row per tournament in the Between Ends index (1,572)

| column | meaning |
|---|---|
| `tournament_id` | Between Ends tournament id (the number in `/tournament/<id>`) |
| `name`, `location` | as entered by the organiser |
| `start_date`, `end_date`, `year` | ISO dates; `year` = year of `start_date` |
| `updated_at` | last update at the source |
| `category` | derived from the name — see below |
| `tags` | pipe-separated name tags (`joad`, `collegiate`, `combined_results`, `final`, `trials` …) |
| `is_national_championship` | USA Archery national championship family (indoor/target/field/3D/collegiate/JOAD/OAS) |
| `is_indoor_national`, `is_outdoor_national` | the "(indoor and outdoor) nationals" split |
| `is_usat` | USAT = the **USA Archery Team** series (USAT #1–#4 + the Series Final) |
| `is_team_trials` | Olympic / Paralympic / World / Pan-Am / Youth team trials |
| `is_marquee` | national championship ∪ USAT ∪ team trials ∪ S3DA nationals |
| `championship_year` | the *season* (the 50th Indoor Nationals starts 28-Dec-2018 but is the 2019 championship) |
| `championship_key` | `<category>:<championship_year>` — groups the site events + Combined-results tournaments of one championship |
| `edition` | ordinal parsed from the name (49th Indoor, 134th Target …) |
| `usat_leg` | `1`–`4`, or `F` for the Series Final |
| `is_combined_results` | the tournament re-publishes other tournaments' results (see *canonical* below) |
| `status` | `completed` / `in_progress` / `upcoming` (as of the build date) |
| `in_scope`, `scope_reasons` | collected? why (`last_5_years`, `decade_marquee`) |
| `details_collected`, `n_events`, `event_types` | collection bookkeeping |
| `n_entries`, `n_archers`, `n_canonical_entries` | result counts (0 when no results) |
| `organizer_message` | the tournament banner text, if any |

`category` values: `indoor_nationals`, `joad_indoor_nationals`, `target_nationals` (incl. the U.S. Open),
`joad_target_nationals`, `collegiate_target_nationals`, `collegiate_3d_nationals`, `field_nationals`,
`3d_nationals`, `oas_nationals`, `usat_series`, `team_trials`, `s3da_nationals`, `s3da_other`, `nast`
(National Archery Scholarship Tour — local shoots), `virtual_tournament`, `collegiate_regional`,
`state_championship`, `selection_event`, `other_national`, `other`.

## events.csv.gz — one row per event of every in-scope tournament (2,763)

An *event* is one result sheet inside a tournament (a ranking round, a combined ranking, an elimination bracket,
a team-points table).

| column | meaning |
|---|---|
| `event_id`, `tournament_id`, `display_order`, `event_name` | identity (use the URL id, **not** `event_json_id`) |
| `event_type` | `RankingEvent`, `CombinedRankingEvent`, `MatchEvent`, `CustomPointsEvent`, `SpecialEvent` |
| `is_aggregate` | `CombinedRankingEvent` — re-publishes performances shot in other events |
| `rounds`, `ends_per_round`, `arrows_per_end`, `regulation_arrows`, `format_key` | the declared format, e.g. `4x10x3` = 120 arrows |
| `format_family` | `indoor_wa`, `outdoor_wa`, `field_wa`, `field_nfaa`, `3d`, `indoor_nfaa`, `scholarship_round`, `oas`, `lancaster`, `other` |
| `scoring_rule`, `scoring_rule_name`, `tiebreak_labels` | Between Ends' scoring-rule number and what it breaks ties on |
| `declared_max_arrow_value`, `max_arrow_value`, `max_score_possible`, `rule_mismatch` | `max_arrow_value` = declared rule's maximum, raised to the highest value actually scored if the organiser picked a wrong rule (`rule_mismatch`) |
| `n_sessions`, `n_archers`, `n_groups`, `n_scored`, `n_complete`, `has_scores` | size / completeness |

## entries.csv.gz — the fact table: one row per archer per ranking/combined event (304,023)

| column | meaning |
|---|---|
| `entry_id` | `<event_id>-<aid>` |
| `event_id`, `tournament_id`, `event_type` | links |
| `aid` | event-local registration row (**not** a person id) |
| `archer_id`, `identity_basis` | the resolved person — see *Identity* below |
| `alt_id`, `alt_raw` | the source's person-level number (cleaned / raw) |
| `display_name`, `name_key`, `first_name_raw`, `last_name_raw` | cleaned display name; normalised key; raw |
| `club`, `sessions`, `targets`, `cnd_code` | club/team as typed; shooting sessions; target letters; compact class code |
| `primary_group`, `groups`, `n_groups` | class/division groups the archer is ranked in |
| `division`, `age_class`, `gender` | from the primary group (`Recurve`, `Compound`, `Barebow`, `Fixed Pins`, `Para …` …; `Senior`, `U21`/`U18`/`U15`/`U13`, `50+`/`60+`/`70+`, `Collegiate` …; `M`/`W`/`X`) |
| `is_guest`, `is_para`, `is_collegiate` | guests are not eligible for titles; collegiate is an overlay group |
| `regulation_arrows`, `arrows_shot`, `reg_arrows_shot` | declared arrows; arrows shot (incl. shoot-off); regulation arrows shot |
| `complete_card` | every regulation arrow shot **or** every round total entered, and the score is possible under the format |
| `totals_only` | results typed in as round totals (no arrows) |
| `score_exceeds_format` | the entered score is impossible under the event's own format (source inconsistency; excluded from stats) |
| `total_score` | the score the website ranks by (**includes** shoot-off arrows) |
| `regulation_score`, `shootoff_arrows` | score over the regulation arrows only; number of shoot-off arrows |
| `max_score_possible` | `regulation_arrows × max_arrow_value` |
| `round_totals`, `r1`–`r4` | per-round totals (pipe-separated / first four rounds) |
| `tie1`, `tie2`, `tie3` | tie-break counts for the event's rule (see `events.tiebreak_labels`) |
| `x_count`, `ten_count`, `nine_count`, `miss_count` | arrow counts (`ten_count` = X + 10) |
| `avg_arrow` | points per arrow (regulation) |
| `pct_of_max` | `regulation_score / max_score_possible` for complete cards |
| `rank_primary`, `group_size_primary`, `percentile_primary` | rank in the primary group (ties share a rank, as on the website); percentile 1.0 = best |
| `used_round_total_override` | a typed round total replaced the arrow-derived value |
| `perf_key` | hash of the arrow string (≥24 arrows) — identifies a physical performance |
| `is_canonical`, `duplicate_of_entry_id` | **use `is_canonical == True` for any statistic.** The same performance published in a site event *and* a Combined-results event is kept once |

## entry_groups.csv.gz — entry × class/division group (273,085)

`entry_id, event_id, archer_id, group_name, group_order, division, age_class, gender, is_guest, is_collegiate, is_para,
rank, group_size, cut, advanced`. An archer can be in two groups (e.g. *Recurve U21 Women* and *Recurve Collegiate Women*).
`rank` is computed exactly as the website does (total, then tie-breaks; ties share a rank). `advanced` = within the group's cut.

## entry_arrows.csv.gz — `entry_id, arrows`

The arrow-by-arrow string for each entry: `X`/`T`=10, digits, `M`=miss, `E`=not shot, `a`/`b`/`d`=3D 11/12/14,
`W`/`Y`/`Z` = NFAA X variants. Rounds/ends are not delimited — split by `events.rounds/ends_per_round/arrows_per_end`.

## match_rows.csv.gz — elimination / round-robin: one row per archer per match (152,313)

`match_row_id, event_id, tournament_id, match_group, group_order, division, age_class, gender, match_event_type,
start_round, scoring_rule, ends_per_match, arrows_per_end, mrid, match_number, round_index, round_name, pair_index,
aid, is_bye, first_name_raw, last_name_raw, club, target, seed, won, final_score, arrows, shootoff, opponent_aid,
opponent_final_score, final_place, archer_id, archer_match_basis, display_name`

* `seed` = qualification rank; `won` = won this match; `final_score` = set points (recurve) or total (compound);
* `pair_index` pairs the two rows of one match (`match_event_type` ≠ 8); `opponent_*` are the other row's values;
* `final_place` (1,2,3,4,5,9,17,33,65) is **inferred** for single-elimination brackets and only when the gold match is decided;
* `archer_match_basis`: `tournament_aid` (exact), `unique_name`, or `unresolved`.

## custom_points.csv.gz — team / points tables (59,695)

`event_id, tournament_id, ranking_name, column_label, sub_group, aid, first_name_raw, last_name_raw, home_state, points,
score, rank, sig_digits, archer_id, archer_match_basis, display_name`. Includes club-team championships, S3DA team points,
USAT Series Final points. For teams the "name" is the team name. `home_state` is only present here.

## archers.csv.gz — one row per resolved archer (41.6k)

| column | meaning |
|---|---|
| `archer_id`, `display_name`, `name_variants` | `A<alt>` (member-number based) or `N<hash>` (name only) |
| `alt_id`, `alt_ids`, `identity_basis` | modal alt; all alts merged into this archer; weakest basis among their records |
| `gender`, `primary_division`, `divisions`, `latest_division`, `latest_age_class`, `age_classes` | profile |
| `first_date`, `last_date`, `active_years`, `n_active_years`, `active_last_12_months` | activity |
| `n_entries`, `n_tournaments` | canonical performances / tournaments |
| `n_marquee_tournaments`, `n_national_championships`, `n_usat_events`, `n_team_trials` | marquee participation |
| `top_clubs`, `latest_club` | clubs |
| `sources` | `betweenends`, `usarchery_pdf` or both (2016-17 history linked) |

## archer_seasons.csv.gz — archer × year × division × format_family

`n_performances, mean_avg_arrow, best_avg_arrow, mean_pct_of_max, best_regulation_score, mean_percentile, best_rank,
total_x, total_tens, total_arrows, ten_rate, x_rate` (complete, canonical, non-guest cards only).

## archer_trends.csv.gz — archer × division × format_family with ≥1 comparable performance

`n_performances, first_date, last_date, span_years, first/last/best/mean_avg_arrow, sd_avg_arrow, slope_per_year,
slope_se, r2, recent3_mean, prior_mean, recent_vs_prior, trend, best_pct_of_max, last_pct_of_max`.
`slope_per_year` is the OLS slope of points-per-arrow against time (years). `trend` ∈ `improving` / `declining`
(|slope/SE| ≥ 2), `stable`, or `insufficient_data` (< 3 performances). Slope/SE/r² need ≥ 3 performances.

## marquee_results.csv.gz — every canonical result at a nationals / USAT / trials event (106k)

Both eras. `source` = `betweenends` | `usarchery_pdf`; `date_approx` flags legacy documents with only a year;
`qualification_rank` / `group_size` / `percentile`; `final_place` is joined from `match_rows` where the archer is in an
inferred bracket result (otherwise blank).

## yearly_summary.csv — year × category: tournaments, entries, unique & new archers, mean % of max

## legacy_tournaments.csv / legacy_entries.csv.gz — 2016-17 results parsed from USA Archery PDFs

`legacy_tournaments`: one row per catalogued PDF with `source_url`, `sha256`, `parsed`, `unparsed_reason`, `n_rows`,
`n_rows_rejected`, `n_sum_check_failed`. `legacy_entries`: rank, name, club, country, total, round scores, tie-breaks,
the same `division/age_class/gender` vocabulary, and `sum_check_ok` / `round_scores_reliable` / `score_plausible` integrity flags.
**No arrow data and no member number** in these documents.

## identity_review.csv.gz — cases where identity resolution made (or declined) a judgement call

`kind` ∈ `bridged`, `name_bridged`, `bridge_ambiguous`, `alt_name_conflict`, `junk_alt`, `possible_duplicate_person`;
`alt` (the ids involved), `names`, `detail`. Use it to audit or override identity decisions.

## normalize_issues.csv — events the normaliser could not process (empty on a clean build)
