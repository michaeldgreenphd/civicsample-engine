# Operational Feasibility, Accrual Velocity & Early-Termination Benchmarks (manual)

Deterministic operational feasibility benchmarks computed over full ClinicalTrials.gov API v2
study records or weekly full-record snapshots (`src/full_records.py`). This package runs by hand;
no scheduled workflow calls it, and the weekly demographics job, retention rules, and existing
published site contracts remain untouched.

## What it computes

| Component | Module | Method & invariants |
|---|---|---|
| Date-precision-aware duration | `feasibility/metrics.py` | Evaluates `startDateStruct -> primaryCompletionDateStruct` and `startDateStruct -> completionDateStruct`. Exact integer `days` when both dates are `YYYY-MM-DD`; exact `[min_days, max_days]` and `[min_months, max_months]` calendar bounds when either is `YYYY-MM` or `YYYY` (never fabricating `-01` day defaults). Partial intervals that overlap or touch at a single day are flagged `overlapping_or_zero_at_precision`; `end < start` is `inverted_dates_anomaly`; `ESTIMATED`/`ANTICIPATED` dates are excluded with `excluded_estimated_or_anticipated_date`. Weekly full records (`--demographics`) carry the three date strings but no date types (the `src/utils.py` projection drops each struct's `type`), so a projected completion date cannot be told from an ACTUAL one: both windows are excluded with `date_type_unavailable_in_weekly_projection` (after the missing/malformed/estimated checks), and that status also becomes the trial's accrual `rate_status`. Realized durations and accrual velocity therefore come only from API v2 inputs (`--records-dir`, `--archive`); the summary's duration and accrual state tables count the new status, and the contract checker refuses a weekly build whose untyped window is marked realized. |
| Enrollment & accrual velocity | `feasibility/metrics.py` | Normalizes `enrollmentInfo.type` strictly into `{"ACTUAL", "ESTIMATED", "MISSING_TYPE"}` (`ANTICIPATED` maps to `ESTIMATED` with `raw_type` preserved). Monthly accrual velocity (`actual_participants_per_month` + `[min_rate, max_rate]` bounds) and `actual_participants_per_listed_facility` are computed **only** on `ACTUAL` enrollment (`count >= 0`), never pooling `ESTIMATED` target enrollment. Stamps `rate_basis = "trial_total_actual_over_calendar_window"` and `facility_basis = "listed_contacts_locations_not_site_level_enrollment"`. |
| Period 1 participant flow retention | `feasibility/metrics.py` | Extracts `resultsSection.participantFlowModule` (`periods[0]`, `groups`, `dropWithdraws`, `preAssignmentDetails`), excluding any duplicate `"Total"` column when multiple flow groups are posted. Computes `period1_started`, `period1_completed`, `period1_completion_rate` (withheld as `None` when `STARTED == 0`, missing/non-numeric, or `COMPLETED > STARTED`), `n_periods`, `has_pre_assignment_details`, and verbatim `dropWithdraws` reason tallies (a reason's tally is `null` unless every retained arm gives exactly one valid count for it). One malformed flow group, a repeated group id, a repeated STARTED or COMPLETED milestone in Period 1, or a retained arm counted twice in either, withholds the whole flow (`malformed_participant_flow`). |
| Early-termination signals & results shells | `feasibility/termination.py` | Versioned deterministic classifier (`TERMINATION_RULES_VERSION = "1.4.3"`) over `TERMINATED`, `WITHDRAWN`, and `SUSPENDED` studies with negation guards (`"no safety concerns"`, `"not due to COVID-19"`, `"Recruitment was fully completed"`). A negation masks a signal only when it governs it: determiners such as "no" within a short span that does not cross a causal connective, and "not" only through a link such as "not due to / related to / based on / for (a)"; so "Could not continue due to COVID-19" is a COVID signal (rules 1.0.0 masked any signal within 35 characters of "not"). For COVID, "no" masks only the noun phrase it heads, with at most filler words between (rules 1.2.0): "No COVID-19 impact" and "there were no COVID-19 related issues" are negations, while "No further enrollment due to COVID-19" and "No participants enrolled during the COVID-19 pandemic" keep the COVID signal, because there "no" governs the enrolment and COVID is its cause or its time (rules 1.1.0 had no "no" guard for COVID; "No impact of COVID-19" is still read as a COVID signal). For business, "no" masks only a phrase naming the business word as a reason (rules 1.3.0): "No business reason" and "there were no strategic reasons" are negations, while "No financial support", "no longer in business" and "No interest in continuing trial due to business changes" stay business signals (in the weekly full records, 16 of the 17 `whyStopped` texts with a negator near a business word state a business cause and none denies one; 1.3.0 moves none of the 11,291 weekly trials with a `whyStopped`). For regulatory holds and supply, a linked negation ("not due to", "unrelated to", "non-") covers every phrase the detector matches, so "Not due to FDA feedback" and "not related to manufacturing issues" are excluded causes, while "no" and "without" deny only a supply problem noun ("no shortage", "no supply issues"): "no drug supply" states a supply problem (rules 1.4.0, which move 3 of the 11,291 weekly trials, each to the cause its text states). Rules 1.4.1 add the detector's unavailable-first form ("Not due to an unavailable study drug") to the linked negation, so each of the supply detector's four alternatives can be excluded. Assigns one of the 7 single-signal categories, `multiple_signals_detected` (preserving the full `matched_signals` list rather than picking whichever regex ran first), `other_unclassified_text`, or `missing_why_stopped`. Merges optional human overrides from `feasibility/termination_reviews.csv` and classifies `stopped_study_results_state` across `{"results_with_analysed_primary", "empty_results_shell_zero_analysed", "results_posted_primary_denom_incomplete", "results_posted_no_primary_denom", "results_not_posted"}`. An outcome measure that is not an object, or whose `type` is not one of CT.gov's four (`OUTCOME_MEASURE_TYPES`), could be a primary one, so it rules out an empty shell (rules 1.4.2; the 2,200 cached records hold only the four). So does a primary measure's denominator entry that is not an object or names no unit (`units` missing, blank or not a string), since it could be another Participants row with unknown or positive counts; on rules 1.4.2 an all-zero Participants row beside `null` read as an empty shell (rules 1.4.3; all 4,803 denominator entries of the 4,670 cached primary measures are objects naming a unit). An empty shell needs every primary measure to carry exactly one complete denominator row (a second Participants row makes the measure incomplete, and any positive count in either still shows analysed participants; rules 1.4.0), with one count for each of the measure's groups and every count 0; when no count is above zero but a primary measure lacks a denominator, a group count is missing, non-numeric or negative, or the counts do not match the measure's groups one to one, the state is `results_posted_primary_denom_incomplete` (rules 1.3.0; 1.2.0 dropped such counts and read the rest as an empty shell). On the 2,200 cached records with posted outcome measures under `docs/audits/outcomes_results_structure/`, no state changes (89 empty shells either way), and every one of the 4,670 primary denominator rows lists exactly its measure's groups. |
| Stratified & sponsor benchmarks | `feasibility/benchmarks.py` | Stratifies by indication (`condition_ontology.json` category via `src/condition_classifier.py`, or, without rapidfuzz, a stdlib keyword fallback where keywords of <= 4 characters match only as whole words with an optional `s`/`es` and longer ones as substrings; + optional `src/condition_mesh.py` MeSH descriptors) x phase x footprint tier (`single_site`, `2_to_10_sites`, `11_to_50_sites`, `51_plus_sites`, `no_listed_sites`) x sponsor class / curated canonical company (`sponsors/` read-only attribution keeping `attributed`, `reviewed_excluded`, and `unreviewed` strictly separate, `nct_id`-deduplicated). Withholds percentiles below `min_cell_trials` while reporting exact `n_trials`. |

## Curated human review file

`feasibility/termination_reviews.csv` ships header-only (`nct_id,reviewer,reviewed_at,override_category,notes`) and is never written by code. When a human reviewer records an override row, `classification_source` becomes `"human_review_override"` while `deterministic_category` and `matched_signals` stay alongside it. Private columns (`reviewer`, `notes`) remain in the CSV and are blocked from reaching published artifacts by the producer contract.

## Artifacts (schema version 1)

Written by `scripts/build_feasibility_benchmarks.py`:

- `run.json` — manifest carrying `run_id` (`feasibility-YYYY-MM-DD-<run_identity>`), `generated_at`, `producer` (`commit`, `uncommitted_changes`, `source_sha256`; the digest and the dirty check cover every file under `feasibility/`, the three feasibility scripts, `condition_ontology.json`, and every repository module the build imports, including `src/full_records.py`, `src/condition_mesh.py`, `src/condition_classifier.py` and the `sponsors/` attribution modules), pinned input SHA-256s, `parameters`, `vocabularies`, `reviews`, and `files` SHA-256 map.
- `summary.json` — named `denominators`, headline `metrics`, breakdown `tables`, and `overall_benchmarks`. Every count/metric object names `value`, `numerator`, `denominator`, and `definition`.
- `strata.json` — overall, marginal, MeSH-indexed, and multi-way cross-classified operational benchmark cells.
- `sponsors.json` — curated sponsor attribution summary (`attributed`, `reviewed_excluded`, `unreviewed` kept disjoint) and company-level (`lead` and `any` role) benchmark cells.
- `trials.jsonl.gz` — deterministic (`mtime=0`) gzipped JSONL of per-trial operational, retention, sponsor, and termination records sorted by `nct_id`.

## Running & verifying

A `--demographics` container must carry both run stamps in the producer's format, with or
without a sidecar: `extracted_at` a whole UTC ISO-8601 timestamp (`Z` or `+00:00`; `-00:00`,
RFC 3339's unknown offset, is refused, as in `--generated-at`) and `pipeline_commit` a 40-hex
git revision (as `src/extract_all.py` writes them). A missing, blank or malformed stamp refuses
the build, and the contract re-checks `inputs.records.source_extracted_at` /
`source_pipeline_commit` on weekly builds, so the site export never republishes a stamp that
names no run.

`--condition-mesh` must come from the same run as the records it is joined to. With
`--demographics`, every sidecar line's `extracted_at` and `pipeline_commit` must equal the
full-record container's (both must be present), so the matching week's
`condition_mesh.jsonl.gz` from the same `data-YYYY-MM-DD` release is the only one accepted.
With `--records-dir` or `--archive` there are no run stamps to compare, so only coverage is
checked. Either way the sidecar must hold a line for every cohort trial; extra lines are counted,
never joined. Every line must also keep the `src/condition_mesh.py` line contract: `schema_version`
`1.0`, a known `state`, `meshes`/`ancestors` null or lists of `{id, term}`, and a state that agrees
with them (`indexed` has descriptors, `not_indexed` none, `no_module`/`no_derived` no lists, only
`malformed` a `problem`). A mismatch refuses the build and writes nothing. `run.json`
`inputs.condition_mesh` records `provenance_check`, the matched `extracted_at` /
`pipeline_commit` (`null` when no stamps were compared), `n_cohort_trials_covered` and
`n_rows_outside_cohort`.

`--records-dir` and `--archive` need complete API v2 study records: every record must carry the protocol modules CT.gov requires on each registration (identification, status, sponsor, conditions, design) with the fields CT.gov holds on every registration inside them (`briefTitle`, `overallStatus`, `statusVerifiedDate`, `studyType`), its conditions (`conditionsModule.conditions`, a non-empty list of non-blank strings) and its lead sponsor (`sponsorCollaboratorsModule.leadSponsor`, an object with a non-blank `name` and `class`), which would otherwise read as indication Other, an "Unknown" sponsor and `MISSING_CLASS`, and a boolean top-level `hasResults`, with a non-empty `resultsSection` whenever `hasResults` is true; the optional parts the build reads (the three date structs, `enrollmentInfo`, `collaborators`, `contactsLocationsModule` and its `locations`) must have their API v2 type when present, with every `locations` and `collaborators` entry an object (a skipped `null` location would lower the facility count) and its `country`, `name` and `class` strings when present; phases (`designModule.phases`) must be CT.gov phase values (`NA`, `EARLY_PHASE1`, `PHASE1` to `PHASE4`), since any other string would be kept as a phase stratum of its own; and a response envelope requested with a `fields=` projection is refused. A weekly full record's `phase` string is held to the same values, token by token (`"PHASE1, PHASE2"`); `""` means no phase stated, and the key must be present. Its `study_sites`, `countries`, `collaborators` and `conditions` must be present (`src/utils.py` always writes them; a missing one read as an empty list) and be lists (an explicit `null` included) of objects, or of non-blank strings for `conditions`, which must also be non-empty as on the raw path, with `country`, `name` and `class` strings when present, and every site and country entry carrying a non-blank `country`, since `src/utils.py` keeps only locations with one (`WEEKLY_LIST_FIELDS`, `WEEKLY_REQUIRED_ENTRY_FIELDS`). The scalar fields the build reads must be present with the producer's type, and non-blank where it never writes them blank (`WEEKLY_SCALAR_FIELDS`: `phase` present; `nct_id`, `study_type`, `status`, `lead_sponsor_name` and `sponsor_class` non-blank; `brief_title`, the three dates, `results_date`, `why_stopped` and `enrollment_type` strings; `enrollment` an integer); a missing or mistyped one was read as absent, an "Unknown" sponsor or no enrollment. `results_date` must be exactly `""` or a registry date (`metrics.parse_date_bounds`): the weekly projection carries no results section, so any other non-empty value (`"N/A"`, `" "`, `"2024-02-30"`) read as posted results and moved a stopped trial from `results_not_posted` to `results_posted_no_primary_denom`. Every `results_date` in the 2026-11-08 extraction is a full date. `primary_condition` and `secondary_condition` may be missing or blank, because the build then classifies `conditions` itself. Before these checks the build kept only well-formed entries, so a `null` site lowered the facility count, moved the footprint tier and raised participants per facility while the contract passed. All 80,890 records of the 2026-11-08 weekly extraction pass. The weekly projection (`src/utils.py`) also drops a location without a country before writing `study_sites`, so a weekly facility count counts only sites with a country; in the 3,371 distinct stored unprojected trials, 1 of 73,762 locations (one trial) has none. NCT IDs are `NCT` followed by eight ASCII digits. A malformed participant flow (a group title that is not a string, or `milestones`, `achievements` or `dropWithdraws` that are not lists) is `malformed_participant_flow`, and a withdrawal reason whose `reasons` or group ids are malformed has a `null` tally. The lead sponsor class is required because every stored unprojected record carries one; in the 2026-06-19 AACT sponsors fixture all 976 lead rows without a class are named "[Redacted]" (the fixture holds no study status, so that they are withheld trials is inferred, not checked), and the one such record cached from the API (NCT04060524, `WITHHELD`) has no study type either. The outcomes audit caches under `docs/audits/outcomes_results_structure/` are field-projected and are not valid inputs here: built from them, every trial would read as Other / no sites / no sponsor class.

`--archive` also reads cached HTTP response envelopes (one JSON line per request with `status`,
`body_sha256` and the unparsed `body`, as the outcomes audit's fetch scripts write them). Each must
have `status` 200, a body matching `body_sha256`, at least one study, and exactly the NCT ids it
requested (its `ids` field, or `filter.ids` in its `url`): a failed, altered or short response
refuses the build rather than silently dropping trials from the cohort. An envelope that names no
requested ids is refused too, because a short answer could not be detected. `nextPageToken` is not
consulted, since CT.gov returns one on full pages even when the requested ids were all answered.

```bash
# Build from weekly full records (never pass site demographics.part*.json.gz)
python3 scripts/build_feasibility_benchmarks.py --demographics data/demographics.json.gz \
    --condition-mesh data/condition_mesh.jsonl.gz --out build/feasibility

# Or build from a directory or frozen archive of full CT.gov API v2 records
python3 scripts/build_feasibility_benchmarks.py --archive /path/to/full_records.jsonl.gz \
    --out build/feasibility

# Verify producer contract (also re-parses run.json generated_at and checks every trial's
# condition_mesh.state against the known states). A JSON file, cell list, cell or trial row of the
# wrong type is a reported problem; an exception the checker did not anticipate is reported as
# FAIL with exit 1, not a traceback, and the site exporter refuses the build the same way.
python3 scripts/check_feasibility_contract.py build/feasibility

# Export compact site files (stages atomically after verifying build SHA-256s)
python3 scripts/export_feasibility_site.py build/feasibility build/feasibility_site

# Offline unit & contract test suite
python3 -m pytest tests/feasibility -q
```

## Compatibility

New files only: `feasibility/`, `scripts/build_feasibility_benchmarks.py`,
`scripts/check_feasibility_contract.py`, `scripts/export_feasibility_site.py`, and
`tests/feasibility/`. No existing module, workflow, sponsor rule table, or site record contract is
modified.
