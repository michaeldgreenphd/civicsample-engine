# Baseline chronological age parsing and representation benchmarks

Deterministic extraction of participant chronological baseline age from ClinicalTrials.gov API v2
records, exact non-interpolated age-cutoff bounds, protocol-eligibility versus enrolled-age gap
auditing, and reference-population comparisons against American Community Survey (ACS) 5-year table
`B01001`.

## What this package computes, and what it refuses to do

ClinicalTrials.gov baseline characteristics report participant age in heterogeneous registrant
layouts: categorical age brackets (`"< 18 years"`, `"18 to 64 years"`, `">= 65 years"`, neonatal
multi-unit brackets such as `"0 to 27 days"` and `"28 days to 23 months"`, or custom cutpoints such
as `"50 to 70 years"`), continuous summary measures (`MEAN`, `MEDIAN`, `GEOMETRIC_MEAN`,
`LEAST_SQUARES_MEAN` with `STANDARD_DEVIATION`, `STANDARD_ERROR`, `INTER_QUARTILE_RANGE`,
`FULL_RANGE`, or `CONFIDENCE_INTERVAL_95`), or both.

Three analytical rules govern every figure in `age/`:

1. **No denominator balancing.** Registered protocol enrollment (`enrollmentInfo.count`) minus
   `n_total_parsed` is recorded separately as `enrollment_minus_parsed` when a categorical
   participant count exists. It is **never** added to `n_unknown` and **never** enters
   `age_report_status`.
2. **No uniform interpolation inside a trial's straddling age bracket.** When a trial reports a
   bracket such as `"50 to 70 years"` (`[50.0, 71.0)`), the number of participants aged `>= 65`
   inside that bracket can lie anywhere between `0` and the bracket's entire count. Rather than
   imposing a uniform within-bracket assumption on trial participants, `representation.age_range_bounds`
   computes exact `[min_count, max_count]` and `[floor_pct, ceiling_pct]` bounds on both
   Denominator A (all participants in the table) and Denominator B (`of_classified`).
3. **Reference-population framing.** Comparisons with ACS 5-year `B01001` (`compare_age_to_acs`)
   benchmark enrolled baseline age against **residents of a selected geographic area who meet the
   trial's nominal sex and age eligibility** (`[eligible_lo, eligible_hi)`). As in `census/`, an
   area's resident age distribution is a demographic context benchmark, **not** the disease-prevalent
   population (many chronic conditions skew substantially older than general area residents).

## Architecture

| Module | Dependencies | Responsibility |
|---|---|---|
| `age/parser.py` | Python stdlib only | Measure selection & false-positive/non-participant exclusion, whole-population `BG` group selection, categorical interval grammar & pairwise overlap/gap audit, continuous stat normalization, seven-state `age_report_status`, and import-pinned `VOCABULARY_SHA256`. |
| `age/table.py` | `age.parser` + stdlib | Versioned raw measure envelope (`select_raw_age_measures`, `age_raw_measures/1.0`), per-trial flat row (`build_age_row`, `build_age_row_from_raw`), `lean_age_row`, `parse_error_row` trial-preservation guarantee, cohort `structural_checks`, and JSONL/CSV round-trip helpers. |
| `age/representation.py` | `age.parser`, `census.acs`, `census.benchmark`, `census.trials` | Exact `[min_count, max_count]` / `[floor_pct, ceiling_pct]` cutoff bounds (`age_range_bounds`), nominal eligibility versus observed enrollment gap auditing (`audit_eligibility_vs_enrolled`), and ACS 5-year `B01001` comparison with 90% margins of error (`compare_age_to_acs`). |

## Measure selection and false-positive / non-baseline guards

`parser.measure_kind(title)` classifies every baseline measure title deterministically:

- **`"participant_baseline_age"`** — chronological baseline age of study participants (`"Age"`,
  `"Age, Continuous"`, `"Age, Categorical"`, `"Age, Customized"`, `"Age Group"`, `"Baseline Age"`,
  `"Participant Age"`, `"Age at Enrollment"`, `"Age at Randomization"`, etc.). Only these measures
  are returned by `select_age_measures(measures)`.
- **Rejected as `"not_age"`** — English words containing `"age"` as a substring (`"Dosage"`,
  `"Disease Stage"`, `"Coverage"`, `"Percentage"`, `"Hemorrhage"`, `"Cartilage"`, `"Drainage"`,
  `"Language"`, `"Storage"`, `"Average"`, `"Damage"`, `"Leakage"`) and anthropometric or
  age-adjusted indices (`"Weight-for-Age"`, `"Height-for-Age"`, `"Length-for-Age"`, `"BMI-for-Age"`,
  `"Age-Adjusted Charlson Comorbidity Index"`).
- **Explicit excluded age kinds** — retained in `excluded_age_measure_titles` and
  `excluded_age_measure_kinds` for audit transparency and **never** mixed into whole-cohort
  participant baseline age:
  - `"gestational_or_developmental_age"` — Gestational Age, Postmenstrual Age, Postconceptional Age,
    Corrected Age, Bone Age, Dental Age, Developmental Age, Biological/Epigenetic Age.
  - `"disease_history_age"` — Age at Diagnosis, Age at Onset, Age at Screening, Age at Menarche,
    Age at Menopause, Age at Transplant/Surgery/Injury.
  - `"non_participant_role_age"` — Maternal Age, Paternal Age, Parental Age, Caregiver Age, Donor
    Age, Partner/Spouse Age, Clinician/Provider Age.
  - `"subgroup_stratified_age"` — Sex-, race-, or subgroup-stratified age tables (`"Age, Female"`,
    `"Age, Male"`, `"Age by Sex"`).

## Whole-population group selection, units, and percentages

- **`select_overall_group`** follows `src/utils.py`: when multiple `BG` groups exist, it selects the
  last group if its title starts with `"Total"` or the unique group matching whole-population titles
  (`"Total"`, `"Overall"`, `"All Participants"`, `"All Subjects"`, `"All Patients"`). When only one
  group exists, it reads that single group. When multiple groups exist with no `Total`/`Overall`
  group, it sums across groups (`flag_summed_arms_no_total = True`) **only** when all group titles
  are distinct, non-empty study arms free of pooling, analysis-subset (`ITT`, `Safety`,
  `Per-Protocol`, `Completers`), or rollover/extension markers; otherwise it refuses and sets
  `flag_ambiguous_groups = True`.
- **Non-participant units:** Measures with `paramType == "COUNT_OF_UNITS"` or anatomical/procedural
  units (eyes, lesions, cycles, biopsies, infusions, teeth, knees, sites, clusters) set
  `is_participant_count = False` and `flag_nonparticipant_units = True` and **never** enter
  participant counts or representation shares.
- **Percentage measures:** Categorical measures reported as percentages (`paramType == "PERCENTAGE"`
  or `%` unit) are converted via `round((pct / 100) * denom)` (`flag_percentage_converted = True`)
  **only** when an explicit positive participant-unit denominator exists for the selected group(s);
  otherwise they classify as `uninformative` (`"percentage_without_denominator"`).

## Categorical interval grammar and pairwise bracket auditing

`parse_interval_label` maps category strings onto half-open chronological year intervals
`[lo_years, hi_years)` (`hi_years = None` for open upper bands) using exact `census/trials.py` unit
factors (`year = 1`, `month = 1/12`, `week = 1/52`, `day = 1/365`, `hour = 1/8760`,
`minute = 1/525600`):

- Completed-integer upper endpoints include that whole unit of age: `"< 18 years"`,
  `"Under 18 years"`, and `"<= 17 years"` map to `[0.0, 18.0)`, whereas `"<= 18 years"` maps to
  `[0.0, 19.0)` (`bound_basis = "inclusive_integer_upper_plus_one_year"`).
- Closed integer ranges (`"18 to 64 years"`, `"18-64 years"`, `"Between 18 and 64 years"`) and
  explicit half-open ranges (`"18 - <65 years"`) both map to `[18.0, 65.0)`. Spelled-out
  comparators with optional parenthetical symbols (`"Less than (<) 18 years"`,
  `"Greater than or equal to (>=) 18 and <= 45 years"`, `"Greater than or equal to 85 years"`) are
  normalized prior to interval parsing.
- **NIH PRS standard `Between` flanking rule:** ClinicalTrials.gov's built-in `Age, Categorical`
  template posts three mutually exclusive partition rows: `"<=18 years"` (`[0.0, 19.0)`),
  `"Between 18 and 65 years"`, and `">=65 years"` (`[65.0, None)`). When a `"Between A and B"`
  bracket is flanked by `"<=A"` and/or `">=B"`, its endpoints resolve to `[A+1, B)` (`[19.0, 65.0)`),
  tiling `[0, 19) + [19, 65) + [65, None)` with an exact `65.0` cutpoint while keeping `"<=18 years"`
  conservatively straddling `18.0`. Hyphen/to ranges (`"18-65"` + `"65-80"`) remain flagged as
  overlapping.
- Neonatal and pediatric multi-unit brackets tile seamlessly: `"0 to 27 days"` -> `[0, 28/365)`,
  `"28 days to 23 months"` -> `[28/365, 2.0)`, `"2 to 11 years"` -> `[2.0, 12.0)`,
  `"12 to 17 years"` -> `[12.0, 18.0)`.
- Open upper bands: `">= 65 years"`, `"65 years and older"`, `"65 years and over"`, `"65+"`, and
  `"> 64 years"` map to `[65.0, None)`. Strict integer `"> 65 years"` maps to `[66.0, None)`, which
  tiles cleanly at `66.0` alongside `"<= 65 years"` (`[0.0, 66.0)`) while leaving `"<= 65 years"`
  properly straddling the `65.0` cutoff because 65-year-olds sit inside `"<= 65 years"`.
- **Pairwise overlap guard:** `audit_bracket_intervals` inspects every pair of parsed brackets. If
  two overlapping brackets (for example `"18-65"` and `"65-80"`) both carry positive counts, the
  categorical measure is rejected as `uninformative` (`"overlapping_age_brackets"`) rather than
  summing double-counted participants.

## Seven non-collapsing reporting states

Every trial record (for example synthetic fixture `NCT99000101`) receives exactly one
`age_report_status`:

| `age_report_status` | Meaning |
|---|---|
| `reported_both` | Valid participant categorical breakdown (`n_classified > 0`) **and** valid chronological-unit continuous age summary. |
| `reported_categorical_only` | Valid participant categorical breakdown (`n_classified > 0`); no valid continuous age summary. |
| `reported_continuous_only` | Valid chronological-unit continuous age summary; no valid categorical breakdown. |
| `explicit_unknown_only` | Categorical participant table posted whose only positive count is an explicit Unknown/Missing/Not Reported row (`n_unknown > 0`, `n_classified == 0`). |
| `uninformative` | Primary age measure present, but carries no usable participant breakdown or continuous stat (reason recorded in `uninformative_reason`: `overlapping_age_brackets`, `incomplete_categorical_brackets`, `inconsistent_categorical_total`, `negative_category_count`, `percentage_exceeds_100`, `percentage_sum_exceeds_100`, `percentage_without_denominator`, `non_participant_units`, `unrecognized_or_mixed_unit`, `impossible_continuous_value`, `ambiguous_groups`, `ambiguous_multiclass_table`, `ambiguous_multicategory_class`, `ambiguous_multicategory_continuous`, `arms_not_poolable_for_continuous`, `partial_unmapped_categories`, `no_mapped_age_brackets`, `no_usable_continuous_stat`, `all_values_zero`, `all_values_na`, `no_measurements`, `other`). |
| `not_reported` | No primary chronological participant baseline age measure present in `baselineCharacteristicsModule`. |
| `parse_error` | Malformed input or unexpected parser failure; boolean outcomes are `None` so failed parses are never miscounted as non-reporting. |

## Denominators, representation gaps, and ACS `B01001` comparison

| View | Denominator | Interpretation |
|---|---|---|
| Denominator A | `n_classified + n_unknown` | Primary comparison denominator. Wholly inside brackets add to `min_count` and `max_count`; straddling brackets and `n_unknown` add `0` to `min_count` and their full count to `max_count`. |
| Denominator B (`of_classified`) | `n_classified` | Secondary view restricted to classified participants. Carries `classified_bounds` (`min_count`, `max_count`, `floor_pct`, `ceiling_pct`) whenever a positive-count bracket straddles the target cutpoint (`point_pct` is `None` when `cutpoint_exact` is `False`). |

`audit_eligibility_vs_enrolled` combines `census.trials.eligible_ages` with enrolled age bounds to
identify trials nominally open to older adults (`>= 65`, `>= 75`) or pediatric participants (`< 18`)
that enrolled **zero** participants in that group (`older_adult_representation_gap`,
`older_adult_75_plus_representation_gap`, `pediatric_representation_gap`), recording whether zero
enrollment was established by `"categorical_zero_in_65_plus"` or `"continuous_max_below_65"` (and
analogously for `>= 75` and `< 18`), plus `enrolled_outside_eligibility_bounds` violations.

`compare_age_to_acs` restricts ACS 5-year `B01001` (`"total"`) cells to the trial's eligible sexes
and `[eligible_lo, eligible_hi)` via `census.benchmark.band_weights`, `_sum`, and `_share`,
evaluating `[0, 18)`, `[18, 65)`, `[65, None)`, and `[75, None)` against `[floor_pct, ceiling_pct]`.
`direction_settled` is `True` if and only if `ceiling_pct < expected_pct` or
`floor_pct > expected_pct`.

## Verification

```bash
python -m pytest tests/age -q
```
