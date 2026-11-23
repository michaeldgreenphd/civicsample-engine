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
| Early-termination signals & results shells | `feasibility/termination.py` | Versioned deterministic classifier (`TERMINATION_RULES_VERSION = "1.1.0"`) over `TERMINATED`, `WITHDRAWN`, and `SUSPENDED` studies with negation guards (`"no safety concerns"`, `"not due to COVID-19"`, `"Recruitment was fully completed"`). A negation masks a signal only when it governs it: determiners such as "no" within a short span that does not cross a causal connective, and "not" only through a link such as "not due to / related to / based on / for (a)"; so "Could not continue due to COVID-19" is a COVID signal (rules 1.0.0 masked any signal within 35 characters of "not"). Assigns one of the 7 single-signal categories, `multiple_signals_detected` (preserving the full `matched_signals` list rather than picking whichever regex ran first), `other_unclassified_text`, or `missing_why_stopped`. Merges optional human overrides from `feasibility/termination_reviews.csv` and classifies `stopped_study_results_state` across `{"results_with_analysed_primary", "empty_results_shell_zero_analysed", "results_posted_no_primary_denom", "results_not_posted"}`. |
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
without a sidecar: `extracted_at` a whole UTC ISO-8601 timestamp and `pipeline_commit` a 40-hex
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

`--records-dir` and `--archive` need complete API v2 study records: every record must carry the protocol modules CT.gov requires on each registration (identification, status, sponsor, conditions, design) with the fields CT.gov holds on every registration inside them (`briefTitle`, `overallStatus`, `statusVerifiedDate`, `studyType`), and a boolean top-level `hasResults`, with a non-empty `resultsSection` whenever `hasResults` is true, and a response envelope requested with a `fields=` projection is refused. The outcomes audit caches under `docs/audits/outcomes_results_structure/` are field-projected and are not valid inputs here: built from them, every trial would read as Other / no sites / no sponsor class.

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
# condition_mesh.state against the known states)
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
