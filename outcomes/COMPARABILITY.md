# Trial Comparability, Estimand, Attrition & Outcome-Fidelity Inspector

Deterministic, purely additive analytical layer under `outcomes/` inspecting ClinicalTrials.gov API v2
study records (`protocolSection`, `resultsSection`, `documentSection`, `hasResults`) for trial
comparability, participant-denominator funnels, conservative arm alignment, estimand / missing-data
signals, statistical contrast non-independence, and registration-to-results primary outcome fidelity.

It runs manually via `scripts/build_outcomes_comparability.py`; no scheduled workflow calls it, and
it modifies no weekly dataset or existing `outcomes/` cohort.

---

## 1. Core Methodological Invariants

1. **Zero RNG & deterministic bytes**: No random number generator or seed exists anywhere in
   `outcomes/comparability.py`, `outcomes/outcome_history.py`, or `outcomes/comparability_build.py`.
   Output files are canonical UTF-8 sorted-key JSON (`dump`), and `run_id` ends in a 10-hex
   content-sensitive `run_identity` SHA-256 digest.
2. **No silent coercion & no hardcoded pass/fail cutoffs**:
   - Missing, `"NA"`, empty, or non-numeric registry values are stored as `None` alongside an
     explicit state/reason string — never coerced to `0`, `0.0`, `""`, or `False`.
   - Non-participant denominator units (e.g. `Eyes`, `Lesions`, `Teeth`, `Implants`) are flagged as
     `unit_not_participants` and excluded from participant-count differences and ratios.
   - Exact signed differences (`baseline_minus_period1_started`, `analysed_minus_baseline_total`,
     `analysed_minus_period1_started`), ratios (`analysed_over_period1_started`,
     `analysed_over_baseline_total`), and arm-specific attrition rates are reported continuously
     without arbitrary pass/fail thresholds.
3. **Conservative `OG -> BG` & `OG -> FG` arm matching**:
   - Outcome groups (`OG`, excluding any group titled `"Total"`) are matched to non-Total
     `baselineCharacteristicsModule.groups` (`BG`) and `participantFlowModule.groups` (`FG`)
     **strictly by normalized non-empty title/description equivalence**.
   - A group titled `"Total"` is never an arm, including when it is a measure's only group: it is
     never aligned and never counted in `primary_measure_ogs` (see `arm_groups_state` below).
   - Array position (`OG000 == BG000`) and participant count equality (`n == n`) are **never** used
     to align arms or resolve duplicate titles.
   - Arm-specific attrition and `abs_differential_attrition` are computed **only** when both `OG`s
     in a 2-arm contrast achieve `exact_unique_title_match` to two distinct groups in `FG` and/or
     `BG` with positive numeric denominators.

---

## 2. Per-Trial Inspection Specification (`outcomes/comparability.py`)

### 2.1 Trial & Primary-Measure Denominator Funnel (`denominator_funnel`)

- **`registered_enrollment`**: `protocolSection.designModule.enrollmentInfo` (`count` + `type` in
  `ACTUAL` / `ESTIMATED`, with `state` in `ok`, `not_registered`, `non_numeric`).
- **`period1_started`**: Sum of `numSubjects` across non-Total `participantFlowModule.groups` in
  `periods[0].milestones[type=STARTED]`.
  - States (`FLOW_STARTED_STATES`): `ok`, `flow_absent`, `unit_not_participants`, `no_periods`,
    `no_started_milestone`, `no_flow_groups`, `non_numeric_or_missing_num_subjects`,
    `no_posted_results`.
  - When `typeUnitsAnalyzed` names a non-participant second unit alongside numeric `numSubjects`,
    `second_unit_flag = "unit_not_participants"` records its presence while keeping `numSubjects`
    as participants; when `numSubjects` is missing/non-numeric and non-participant units are given,
    `state = "unit_not_participants"` and `value = None`.
- **`baseline_total`**: `baselineCharacteristicsModule.denoms[units=Participants]` via Rule T:
  - Methods when `state == "ok"`: `single_group`, `titled_total`,
    `last_equals_sum_untitled_total` (3+ groups where the trailing untitled group equals the sum of
    earlier groups), or `non_total_group_sum` when no Total column exists. Never sums Total + arms.
  - States (`BASELINE_TOTAL_STATES`): `ok`, `baseline_absent`, `no_denoms`,
    `unit_not_participants`, `no_groups`, `non_numeric_count`, `no_posted_results`.
- **Primary outcome measures (`primary_measures[]`)**:
  - Preserves `title`, `type` (`"PRIMARY"`), `unitOfMeasure`, `paramType`, `dispersionType`,
    `timeFrame`, and `populationDescription` verbatim.
  - Extracts measure-level `Participants` denominator per `OG` (`og_denominators`, with
    `is_total_group` true for every group titled `"Total"`) and `analysed_total`
    (`MEASURE_DENOM_STATES`: `ok`, `denoms_absent`, `unit_not_participants`, `no_groups`,
    `incomplete_or_non_numeric`, `multiple_total_groups`). `analysed_total` sums the non-Total
    `OG`s; when every posted `OG` is titled `"Total"` it is that single Total group's own count, and
    with two or more Total-titled groups and no arm it is `None` with `multiple_total_groups` (never
    a sum of Totals, never a pick by position).
  - **`arm_groups_state`** (`MEASURE_ARM_GROUP_STATES`): `arm_groups_present` (at least one
    non-Total `OG`), `no_arm_groups` (every posted `OG` is titled `"Total"`), `no_groups` (no `OG`
    posted). Only `arm_groups_present` measures are aligned; the other two carry
    `og_to_bg_alignment = None` and `og_to_fg_alignment = None` rather than an empty or
    `no_title_match` alignment. Counted per measure in `summary.json`
    `state_breakdowns.primary_measure_arm_groups`.
  - **`class_denom_deviations`**: Compares every `classes[].denoms[]` row against measure-level
    `denoms`, recording exact JSON path, unit mismatches (`unit_differs`), and signed per-`OG`
    differences (`class_minus_measure`).
  - **Funnel arithmetic**: `baseline_minus_period1_started`, `baseline_over_period1_started`,
    `analysed_minus_baseline_total`, `analysed_minus_period1_started`,
    `analysed_over_period1_started`, and `analysed_over_baseline_total`.

### 2.2 Arm Alignment & Differential Attrition (`arm_alignment`)

Each non-Total `OG` on a `PRIMARY` measure is classified into one of five mutually exclusive states
(`ARM_ALIGNMENT_STATES`) against `BG` and against `FG`:

| State | Rule |
|---|---|
| `exact_unique_title_match` | Unique 1-to-1 match on normalized non-empty title, `(title, description)` when descriptions uniquely disambiguate duplicate titles, unique cross title-description equivalence, or unique description equivalence when at least one title is a generic placeholder (`Arm 1`, `Group A`). |
| `ambiguous_duplicate_titles` | Duplicate normalized group titles/descriptions prevent unique 1-to-1 matching; ties are never broken by index or count. |
| `pooled_or_subset_suspected` | No exact unique match, and an `OG` or target label carries pooling/subset cues (`pooled`, `combined`, `completers`, `per-protocol`, `subgroup`, etc.) or embeds another arm's normalized title. |
| `no_title_match` | Target module has non-Total groups, with no title/description equivalence or pooling/subset cue. |
| `module_groups_absent` | Target module (`BG` or `FG`) is absent or has zero non-Total groups. |

A measure with no non-Total `OG` (`arm_groups_state` `no_arm_groups` or `no_groups`) is not aligned:
it adds no `OG` to these states, to `n_primary_measure_ogs`, to the extra-cohort flags, or to any
attrition contrast.

- **`extra_baseline_groups_outside_primary`**: Flags non-Total `BG` cohorts (such as open-label
  run-in cohorts, non-randomized cohorts, or Part 1 lead-in arms) that have no aligned or pooled
  counterpart in any posted `PRIMARY` measure.
- **2-arm contrast differential attrition (`differential_attrition`)**: For every `analyses[]`
  contrast with `len(groupIds) == 2` naming two distinct resolved `OG`s where both `OG`s achieve
  `exact_unique_title_match` to two distinct `FG` (`STARTED`) and/or `BG` groups with positive
  denominators, computes arm-specific `1 - n_analysed / n_started`, `1 - n_analysed / n_baseline`,
  `signed_differential_attrition`, and `abs_differential_attrition`.

### 2.3 Estimand, Missing-Data, Document & Contrast-Structure Signals

- **Posted study documents (`posted_documents`)**: Reads `documentSection.largeDocumentModule.largeDocs`
  for `has_posted_protocol`, `has_posted_sap`, `has_posted_icf`, `filenames`, `upload_dates`, and
  per-document metadata.
- **Missing-data method signal (`MISSING_DATA_STATES`)**: Scans primary-measure
  `populationDescription`, `analyses[].statisticalMethod`, `analyses[].statisticalComment`, and
  `analyses[].otherAnalysisDescription`, recording exact `json_path` and verbatim
  `matched_substring`:
  - `mmrm`, `multiple_imputation`, `locf`, `bocf`, `wocf`, `observed_cases_only`,
    `multiple_methods_named` (>= 2 distinct atomic methods), `not_stated`.
- **Intercurrent-event / rescue-medication signal (`INTERCURRENT_EVENT_STATES`)**: Scans primary
  `title`, `description`, `populationDescription`, `analyses[].groupDescription`,
  `analyses[].statisticalComment`, and `analyses[].otherAnalysisDescription`, recording `json_path`,
  verbatim `matched_substring`, and surrounding `snippet`:
  - `rescue_excluded_or_censored`, `treatment_policy_regardless_of_rescue_or_discontinuation`,
    `both_co_reported`, `not_stated`.
- **Hypothesis & margin clarity per analysis (`HYPOTHESIS_CLARITY_STATES`)**:
  - `superiority_explicit`, `non_inferiority_with_margin`, `non_inferiority_missing_margin`,
    `equivalence_with_margin`, `equivalence_missing_margin`, `ambiguous_legacy_enum`
    (undisambiguated `SUPERIORITY_OR_OTHER(_LEGACY)` or `NON_INFERIORITY_OR_EQUIVALENCE(_LEGACY)`),
    `other_explicit`, `not_stated`.
- **Contrast structure & shared-arm non-independence (`contrast_structure`)**:
  - `group_ids_cardinality_state` in `empty`, `single_arm`, `pairwise_two_arm`, `multi_arm_joint`
    (plus `has_duplicate_group_ids` and `unresolved_group_ids`).
  - Within-measure `shared_arm_contrast_groups` flags pairwise contrasts that reuse a common
    comparator or experimental `OG` (`non_independent: true`, `statistically_independent_within_measure: false`),
    preventing downstream meta-analyses from treating shared-control contrasts as independent.

---

## 3. Primary Outcome Fidelity & Historical Diffs (`outcomes/outcome_history.py`)

### 3.1 Current Registration vs Posted Results (`protocol_to_results`)

Compares `protocolSection.outcomesModule.primaryOutcomes` against posted `PRIMARY` measures in
`resultsSection.outcomeMeasuresModule.outcomeMeasures` (`PROTOCOL_TO_RESULTS_STATES`):

- `verbatim_measure_and_timeframe_match`
- `timeframe_differing`
- `measure_title_differing` (shared substantive clinical-construct tokens without opposing-pole
  conflict; summary framing words such as `"change"` and `"baseline"` alone never pair distinct
  endpoints)
- `registered_primary_missing_in_results`
- `results_primary_unlisted_in_protocol`

Trials without posted results receive `state = "not_applicable_no_posted_results"` (`alignments = None`).

### 3.2 Historical Study-Version Snapshots (`version_history`)

Supports an optional frozen `jsonl.gz` archive keyed by `nct_id` carrying initial or pre-completion
`primaryOutcomes` (and optional `secondaryOutcomes`). Every row must record its primary list: a row
whose `primaryOutcomes` is absent or null (flat, under `protocolSection.outcomesModule`, or under a
top-level `outcomesModule`, including a nested row with no `outcomesModule`) is refused with
`HistoryArchiveError` rather than read as an empty registration; `[]` records a version that
registered none.

- Record states (`VERSION_HISTORY_STATES`): `history_archive_not_provided` (cleanly recorded with
  `diffs = None` when no archive is supplied), `trial_not_in_history_archive`, `evaluated`,
  `parse_failed`.
- Diff states when `evaluated` (`HISTORY_DIFF_STATES`): `unchanged`, `primary_added`,
  `primary_dropped`, `primary_demoted_to_secondary`, `secondary_promoted_to_primary`,
  `timeframe_changed`, `measure_reworded`.

---

## 4. Schema-Version-1 Artifact Contract & CLI

### Artifact Family (`outcomes/comparability_build.py`)

- `run.json`: Manifest with `schema_version: 1`, `run_id` (`outcomes-comparability-YYYY-MM-DD-<10hex>`),
  `producer` (`commit`, `uncommitted_changes`, `source_sha256`), `method` (`spec_sha256`),
  `acquisition` (`sha256_uncompressed`), `history_archive`, `scope`, `vocabularies`, and `files`
  SHA-256 map.
- `summary.json`: Every count object carries `value`, `numerator`, `denominator` (a named key in
  `summary.json["denominators"]`), and `definition`. `comparability_build.check()` recounts
  `summary.json` from `records.json` and refuses any discrepancy.
- `records.json`: Key-sorted map `{nct_id: record}` for every included `NCT` ID.

### CLI Entry Points

```bash
# Build from stored per-trial .json.gz / .json records directory:
python3 scripts/build_outcomes_comparability.py --records-dir PATH --out build/outcomes_comparability

# Build from a frozen sha256-verified .jsonl.gz archive (+ optional historical snapshots):
python3 scripts/build_outcomes_comparability.py --archive PATH \
    [--history-archive hist.jsonl.gz] --out build/outcomes_comparability

# Build from a frozen CT.gov HTTP responses cache:
python3 scripts/build_outcomes_comparability.py --responses-cache PATH --out build/outcomes_comparability

# Fetch full CT.gov API v2 records (<= 2 req/s) and build:
python3 scripts/build_outcomes_comparability.py --fetch --nct-list ids.txt --out build/outcomes_comparability

# Producer contract verification:
python3 scripts/check_outcomes_comparability_contract.py build/outcomes_comparability

# Atomic site export (verifies build sha256s + contract before staging):
python3 scripts/export_outcomes_comparability_site.py build/outcomes_comparability /tmp/site_comparability
```
