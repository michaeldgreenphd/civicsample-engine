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
| Period 1 participant flow retention | `feasibility/metrics.py` | Extracts `resultsSection.participantFlowModule` (`periods[0]`, `groups`, `dropWithdraws`, `preAssignmentDetails`), excluding any duplicate `"Total"` column when multiple flow groups are posted. Computes `period1_started`, `period1_completed`, `period1_completion_rate` (withheld as `None` when `STARTED == 0`, missing/non-numeric, or `COMPLETED > STARTED`), `n_periods`, `has_pre_assignment_details`, and verbatim `dropWithdraws` reason tallies. |
| Early-termination signals & results shells | `feasibility/termination.py` | Versioned deterministic classifier (`TERMINATION_RULES_VERSION = "1.0.0"`) over `TERMINATED`, `WITHDRAWN`, and `SUSPENDED` studies with negation guards (`"no safety concerns"`, `"not due to COVID-19"`, `"Recruitment was fully completed"`). Assigns one of the 7 single-signal categories, `multiple_signals_detected` (preserving the full `matched_signals` list rather than picking whichever regex ran first), `other_unclassified_text`, or `missing_why_stopped`. Merges optional human overrides from `feasibility/termination_reviews.csv` and classifies `stopped_study_results_state` across `{"results_with_analysed_primary", "empty_results_shell_zero_analysed", "results_posted_no_primary_denom", "results_not_posted"}`. |
| Stratified & sponsor benchmarks | `feasibility/benchmarks.py` | Stratifies by indication (`condition_ontology.json` category + optional `src/condition_mesh.py` MeSH descriptors) x phase x footprint tier (`single_site`, `2_to_10_sites`, `11_to_50_sites`, `51_plus_sites`, `no_listed_sites`) x sponsor class / curated canonical company (`sponsors/` read-only attribution keeping `attributed`, `reviewed_excluded`, and `unreviewed` strictly separate, `nct_id`-deduplicated). Withholds percentiles below `min_cell_trials` while reporting exact `n_trials`. |

## Curated human review file

`feasibility/termination_reviews.csv` ships header-only (`nct_id,reviewer,reviewed_at,override_category,notes`) and is never written by code. When a human reviewer records an override row, `classification_source` becomes `"human_review_override"` while `deterministic_category` and `matched_signals` stay alongside it. Private columns (`reviewer`, `notes`) remain in the CSV and are blocked from reaching published artifacts by the producer contract.

## Artifacts (schema version 1)

Written by `scripts/build_feasibility_benchmarks.py`:

- `run.json` — manifest carrying `run_id` (`feasibility-YYYY-MM-DD-<run_identity>`), `generated_at`, `producer` (`commit`, `uncommitted_changes`, `source_sha256`), pinned input SHA-256s, `parameters`, `vocabularies`, `reviews`, and `files` SHA-256 map.
- `summary.json` — named `denominators`, headline `metrics`, breakdown `tables`, and `overall_benchmarks`. Every count/metric object names `value`, `numerator`, `denominator`, and `definition`.
- `strata.json` — overall, marginal, MeSH-indexed, and multi-way cross-classified operational benchmark cells.
- `sponsors.json` — curated sponsor attribution summary (`attributed`, `reviewed_excluded`, `unreviewed` kept disjoint) and company-level (`lead` and `any` role) benchmark cells.
- `trials.jsonl.gz` — deterministic (`mtime=0`) gzipped JSONL of per-trial operational, retention, sponsor, and termination records sorted by `nct_id`.

## Running & verifying

```bash
# Build from weekly full records (never pass site demographics.part*.json.gz)
python3 scripts/build_feasibility_benchmarks.py --demographics data/demographics.json.gz \
    --condition-mesh data/condition_mesh.jsonl.gz --out build/feasibility

# Or build from a directory or frozen archive of full CT.gov API v2 records
python3 scripts/build_feasibility_benchmarks.py \
    --archive docs/audits/outcomes_results_structure/first_pass/cache/raw_responses.jsonl.gz \
    --out build/feasibility

# Verify producer contract
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
