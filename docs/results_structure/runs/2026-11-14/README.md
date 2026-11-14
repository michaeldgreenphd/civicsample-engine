# Results-structure run for the 2020 industry Phase 3 frame, retrieved 2026-11-14

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Run | `results-structure-2026-11-14-0505340298`, generated 2026-11-14T00:51:37+00:00 by `scripts/build_results_structure.py` at engine commit `e6731363aeaa` (no uncommitted changes) |
| Method | `results-structure-1`: usable-content rules B1, F1, O1, A1; comparative classes C1, C2; analysis rule S1; subgroup rule SG2 |
| Frame | 648 NCT ids: every record of the Industry Outcomes beta frame, taken from the site's `data/outcomes/index.json` (598 eligible rows and 50 excluded records) of outcomes run `outcomes-2026-11-09-d41f32ec4d`. The list is `frame_ids.txt` (sha256 `637bb40370bedcb8…`) |
| Registry records | 648 of 648 retrieved from the ClinicalTrials.gov API v2, 2026-11-14T00:41:41+00:00 to 2026-11-14T00:41:57+00:00; API version 2.0.5, registry `dataTimestamp` 2026-11-13T10:00:06 |
| Requests | 27 (26 for study records in batches of 25 ids, 1 for the API version), one at a time and at least 0.5 s apart; 0 retries, 0 failed |
| Archive | `records-2026-11-14-cc8e86b9b06d.jsonl.gz`, sha256 of the uncompressed bytes `cc8e86b9b06ddc0b5cf229a5d2c6af1430ecc5d0fc1554fd090a8085c1ede39e` |
| Human reviews | `outcomes/subgroup_reviews.csv`, 0 rows (sha256 `173e1437b0922e3e…`) |

The build first ran with a live fetch at engine commit `ca41872`; this run was rebuilt from the
frozen archive of that fetch at the commit above, after `summary.json` gained a count on each of
its two failure tables. No rule changed between the two, and the registry was not asked again.

## Counts

Every count is in `summary.json` with its definition. Denominators: requested
648; retrieved 648; posted (retrieved, with posted
results) 443; C1 433; C2 433.

| Count | Trials | Out of | Denominator |
|---|---:|---:|---|
| Results posted | 443 | 648 | retrieved |
| Baseline module present | 443 | 443 | posted |
| Participant flow module present | 443 | 443 | posted |
| Outcome measures module present | 443 | 443 | posted |
| Adverse events module present | 443 | 443 | posted |
| Baseline table usable (rule B1) | 443 | 443 | posted |
| Participant flow usable (rule F1) | 443 | 443 | posted |
| Primary outcome usable (rule O1) | 431 | 443 | posted |
| Adverse events usable (rule A1) | 443 | 443 | posted |
| All four tables usable | 431 | 443 | posted |
| Two or more groups on a primary measure (C1) | 433 | 443 | posted |
| C1 and registered as randomised (C2) | 433 | 443 | posted |
| Between-arm analysis posted on a primary measure | 308 | 433 | C2 |
| Between-arm analysis with a p-value | 275 | 433 | C2 |
| Between-arm estimate with both confidence limits | 269 | 433 | C2 |
| Between-arm p-value posted as a bound | 108 | 433 | C2 |
| Subgroup reporting detected by the current method: at least one signal | 67 | 443 | posted |

C1 and C2 are equal here: every C1 trial in this frame is registered as randomised, which the
frame query asks for.

### Subgroup state

Each of the 648 requested records is in exactly one state at study level
and one per dimension.

| State | Study | Age | Sex | Race | Ethnicity |
|---|---:|---:|---:|---:|---:|
| `not_yet_processed` | 0 | 0 | 0 | 0 | 0 |
| `processing_failed_or_indeterminate` | 67 | 66 | 56 | 55 | 55 |
| `no_explicit_subgroup_results_detected_in_reviewed_structured_fields` | 376 | 377 | 387 | 388 | 388 |
| `explicit_subgroup_results_detected` | 0 | 0 | 0 | 0 | 0 |
| `not_applicable_no_posted_results` | 205 | 205 | 205 | 205 | 205 |

Signals: 449 in 67 of 443 trials with posted results; 0 have a review record.
By dimension: age 91, ethnicity 0, other 354, race 0, sex 4.
By field: analysis_text:estimateComment 10, analysis_text:groupDescription 40, analysis_text:statisticalComment 4, class_title_cue 7, class_titles 14, group_descriptions 58, group_titles 4, measure_description 66, measure_title 28, population_description 218.

### Failures, missing data and negative findings

- Retrieval failures: 0 of 648 requested.
- Parsing failures: 0 of 648 retrieved.

Missing data (rows with a count above zero):

| What is missing | Trials | Out of | Denominator |
|---|---:|---:|---|
| No posted results | 205 | 648 | retrieved |
| O1 not usable: no_data_table | 4 | 443 | posted |
| O1 not usable: no_numeric_value_for_an_analysed_group | 8 | 443 | posted |
| No baseline population description | 187 | 443 | posted |

Negative findings (each is a search that found nothing, with its scope in `summary.json`):

| Finding | Trials | Out of | Denominator |
|---|---:|---:|---|
| No subgroup signal found | 376 | 443 | posted |
| No between-arm analysis posted on a primary measure | 125 | 433 | C2 |
| No statistical analysis of any kind on a primary measure | 123 | 443 | posted |
| No serious adverse event rows posted | 35 | 443 | posted |

## Files here

- `run.json`: the manifest. Parameters, acquisition, the fields the subgroup rule inspected and
  those it did not, limitations, and the sha256 of `summary.json` and `records.json`.
- `summary.json`: every count with its numerator, denominator and definition.
- `frame_ids.txt`: the 648 requested NCT ids.
- `archive.meta.json`: the retrieval record written beside the archive, with one entry per
  request (time, HTTP status, bytes, sha256 of the response).

## Files not here

`records.json` (sha256 `bff217ac4088d724…`, 4.4 MB) is written to the site
repository at `data/results_structure/`. The archive of registry records (7.9 MB compressed) is
under the gitignored `build/results_structure_cache/`; its sha256 is in `run.json`. Rebuilding
from the archive at the same commit gave the same bytes for `summary.json` and `records.json` in
two consecutive builds; `run.json` differs only in `generated_at`.

## Limitations

- Subgroup figures are subgroup reporting detected by the current method: a text rule over 18 structured fields of posted outcome measures. They are not a prevalence.
- A signal is wording that looks like subgroup reporting. Only a human review record can accept or reject it; with no review records, no trial is in the detected state.
- Posted protocols, statistical analysis plans, study reports and publications are not read. Absence in the structured fields is not absence everywhere.
- No result is inferred for a subgroup from a trial's overall result and its baseline proportions.
- Outcome switching is not assessed: a current record does not show how the registered outcomes changed over time.
- Usable-content rules test whether a table can be read, not whether its content is correct, complete or analysed appropriately.
- Two or more groups on a primary measure (C1, C2) is the structure of the record; dose cohorts, periods and disease cohorts also appear as groups.
- Participant flow counts are for the first period only.
- The registry record is read as retrieved on the stated date; registry records change.
- With no review rows, every trial with a signal is `processing_failed_or_indeterminate`. In this
  run 354 of the 449 signals have no named dimension (the words "subgroup", "subset" and similar;
  218 of the 449 signals are in a population description), and each of those leaves all four
  dimensions indeterminate for its trial. In the audit, one automated reader labelled 71 of 136
  detected trials as false positives on a different sample; those labels are not human reviews,
  and nothing here says which of these signals are false.
- The frame is the Industry Outcomes beta's registry query, not its eligible cohort: the counts
  above include the 50 records that cohort excludes.
- Registry records were read as retrieved on 2026-11-14. The Industry Outcomes beta read the
  same registry on 2026-11-09; the two builds are separate and carry their own dates.

## What this record does not establish

That the build is reproducible and matches the audit scripts on the audit's cached records
(`docs/audits/outcomes_results_structure/port_check/`) is engineering validation. Whether the
rules measure what a study of subgroup reporting needs, and how many subgroup results the text
rule misses, are questions this run does not answer.
