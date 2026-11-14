# Results-structure run for the 2020 industry Phase 3 cohort, retrieved 2026-11-14

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Run | `results-structure-2026-11-14-3037691319`, generated 2026-11-14T02:59:54+00:00 by `scripts/build_results_structure.py` at engine commit `27515b167f65` (no uncommitted changes; producing-code digest `395a81013952…`) |
| Method | `results-structure-1`: usable-content rules B1, F1, O1, A1; comparative classes C1, C2; analysis rule S1; subgroup rule SG2 |
| Scope | Frame 648 registry records fetched; **598 trials in this build** (the eligible cohort of the Industry Outcomes beta, the rows of the site's `data/outcomes/index.json`, outcomes run `outcomes-2026-11-09-d41f32ec4d`); 50 frame records left out: excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded). The lists are `frame_ids.txt` (sha256 `637bb40370bedcb8…`), `eligible_ids.txt` (`dcfc1e3fc8829b23…`) and `excluded_ids.txt` (`e7f7925eacf48b28…`) |
| Registry records | 648 of 648 frame records retrieved from the ClinicalTrials.gov API v2, 2026-11-14T00:41:41+00:00 to 2026-11-14T00:41:57+00:00; API version 2.0.5, registry `dataTimestamp` 2026-11-13T10:00:06 |
| Requests | 27 (26 for study records in batches of 25 ids, 1 for the API version), one at a time and at least 0.5 s apart; 0 retries, 0 failed |
| Archive | `records-2026-11-14-cc8e86b9b06d-dcec4894.jsonl.gz`, sha256 of the uncompressed bytes `cc8e86b9b06ddc0b5cf229a5d2c6af1430ecc5d0fc1554fd090a8085c1ede39e` |
| Human reviews | `outcomes/subgroup_reviews.csv`, 0 rows (sha256 `173e1437b0922e3e…`) |

The registry was asked once, for the whole frame. This run is built from the frozen archive of
that fetch, restricted to the eligible cohort; the archive and its sha256 are unchanged. The
first build from the archive covered all 648 frame records (run
`results-structure-2026-11-14-0505340298`, engine commit `e673136`): 443 of 648 with posted
results; B1 443, F1 443, O1 431, A1 443, all four 431; C1 and C2 433; among C2, between-arm
analysis 308 and estimate with both limits 269; 449 signals in 67 trials. It was replaced because
its denominators were the frame, not the cohort the site's pane lists.

## Counts

Every count is in `summary.json` with its definition, over the 598 trials in this build.
Denominators: requested 598; retrieved 598; posted (retrieved, with posted
results) 416; C1 406; C2 406.

| Count | Trials | Out of | Denominator |
|---|---:|---:|---|
| Results posted | 416 | 598 | retrieved |
| Baseline module present | 416 | 416 | posted |
| Participant flow module present | 416 | 416 | posted |
| Outcome measures module present | 416 | 416 | posted |
| Adverse events module present | 416 | 416 | posted |
| Baseline table usable (rule B1) | 416 | 416 | posted |
| Participant flow usable (rule F1) | 416 | 416 | posted |
| Primary outcome usable (rule O1) | 404 | 416 | posted |
| Adverse events usable (rule A1) | 416 | 416 | posted |
| All four tables usable | 404 | 416 | posted |
| Two or more groups on a primary measure (C1) | 406 | 416 | posted |
| C1 and registered as randomised (C2) | 406 | 416 | posted |
| Between-arm analysis posted on a primary measure | 291 | 406 | C2 |
| Between-arm analysis with a p-value | 260 | 406 | C2 |
| Between-arm estimate with both confidence limits | 258 | 406 | C2 |
| Between-arm p-value posted as a bound | 100 | 406 | C2 |
| Subgroup reporting detected by the current method: at least one signal | 64 | 416 | posted |

Posted results, 416 of 598, is the count the Industry Outcomes beta reports for the same
cohort (its `results_posted` metric, as of 2026-11-09), and the two builds agree trial by trial.
C1 and C2 are equal here: every C1 trial in this cohort is registered as randomised, which the
cohort's rules require.

### Subgroup state

Each of the 598 trials is in exactly one state at study level and one per dimension.

| State | Study | Age | Sex | Race | Ethnicity |
|---|---:|---:|---:|---:|---:|
| `not_yet_processed` | 0 | 0 | 0 | 0 | 0 |
| `processing_failed_or_indeterminate` | 64 | 63 | 53 | 52 | 52 |
| `no_explicit_subgroup_results_detected_in_reviewed_structured_fields` | 352 | 353 | 363 | 364 | 364 |
| `explicit_subgroup_results_detected` | 0 | 0 | 0 | 0 | 0 |
| `not_applicable_no_posted_results` | 182 | 182 | 182 | 182 | 182 |

Signals: 440 in 64 of 416 trials with posted results; 0 have a review record.
By dimension: age 91, ethnicity 0, other 345, race 0, sex 4.
By field: analysis_text:estimateComment 10, analysis_text:groupDescription 40, analysis_text:statisticalComment 4, class_title_cue 1, class_titles 14, group_descriptions 58, group_titles 4, measure_description 63, measure_title 28, population_description 218.

### Failures, missing data and negative findings

- Retrieval failures: 0 of 598 requested.
- Parsing failures: 0 of 598 retrieved.

Missing data (rows with a count above zero):

| What is missing | Trials | Out of | Denominator |
|---|---:|---:|---|
| No posted results | 182 | 598 | retrieved |
| O1 not usable: no_data_table | 4 | 416 | posted |
| O1 not usable: no_numeric_value_for_an_analysed_group | 8 | 416 | posted |
| No baseline population description | 171 | 416 | posted |

Negative findings (each is a search that found nothing, with its scope in `summary.json`):

| Finding | Trials | Out of | Denominator |
|---|---:|---:|---|
| No subgroup signal found | 352 | 416 | posted |
| No between-arm analysis posted on a primary measure | 115 | 406 | C2 |
| No statistical analysis of any kind on a primary measure | 114 | 416 | posted |
| No serious adverse event rows posted | 31 | 416 | posted |

## Files here

- `run.json`: the manifest. Parameters, scope, acquisition, the fields the subgroup rule
  inspected and those it did not, limitations, and the sha256 of `summary.json` and `records.json`.
- `summary.json`: the scope and every count with its numerator, denominator and definition.
- `frame_ids.txt`: the 648 NCT ids the archive was fetched for.
- `eligible_ids.txt`, `excluded_ids.txt`: the 598 trials in this build and the 50 frame records left out.
- `archive.meta.json`: the retrieval record written beside the archive, with one entry per
  request (time, HTTP status, bytes, sha256 of the response).

## Files not here

`records.json` (sha256 `830dc7792925b418…`, 4.1 MB) is written to the site
repository at `data/results_structure/`. The archive of registry records (7.9 MB compressed) is
under the gitignored `build/results_structure_cache/`; its sha256 is in `run.json`. Two
consecutive builds of this run from the archive gave the same bytes for `summary.json` and
`records.json`; `run.json` differs between builds only in `generated_at`.

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
  run 345 of the 440 signals have no named dimension (the words "subgroup", "subset" and similar;
  218 of the 440 signals are in a population description), and each of those leaves all four
  dimensions indeterminate for its trial. In the audit, one automated reader labelled 71 of 136
  detected trials as false positives on a different sample; those labels are not human reviews,
  and nothing here says which of these signals are false.
- The 50 frame records left out were fetched and are in the archive; they have no record in
  `records.json` and are in no count here.
- Registry records were read as retrieved on 2026-11-14. The Industry Outcomes beta read the
  same registry on 2026-11-09; the two builds are separate and carry their own dates.

## What this record does not establish

That the build is reproducible and matches the audit scripts on the audit's cached records
(`docs/audits/outcomes_results_structure/port_check/`) is engineering validation. Whether the
rules measure what a study of subgroup reporting needs, and how many subgroup results the text
rule misses, are questions this run does not answer.

## Rebuilt on the pull-request branch

The build was first made on a working branch at commit `e097d22`. Those commits were replayed onto `main` for the pull request, which gave them new hashes, so the run was rebuilt from the same frozen archive at `5cc5e7d`. `summary.json` and `records.json` are identical to the earlier build apart from the run id, which includes the commit.

It was rebuilt once more after the first review round, at `7e431f8`. No record changed: the review's three fixes guard the build's inputs and make signal ids independent of measure order, and no signal in this cohort depended on that order. Only the run id differs, because it includes the commit.

It was rebuilt again after the third review round, at `0a4a27d` (run `results-structure-2026-11-14-b8d6e968c6`). Every signal id changed, because an id is now derived from the text around the match as well as the matched text; no review rows exist, so nothing had to follow. No other field of any record changed, and `summary.json` is identical apart from the run id. The run id now also reflects a digest of the producing code. The second round's two fixes (the archive encoder in the list of producing files; an unmatched review reported once) change no output of this run.

It was rebuilt a fourth time after the fourth review round, at `5b0ae8c` (run `results-structure-2026-11-14-ede13e7b74`). The run id is now a digest of what the family publishes (the manifest apart from the id, the build time and the file checksums, and the content of `summary.json` and `records.json`) instead of a digest of a list of inputs. `summary.json` and `records.json` are identical to the previous build apart from the run id. The checksums quoted in this file are checked against `run.json` by `tests/outcomes/test_results_structure_build.py`.

It was rebuilt a fifth time after the fifth review round, at `0887892` (run `results-structure-2026-11-14-9a3fbcfe15`). The ids of the 76 level signals (those found in sibling class, category or group titles) changed, because such an id is now taken from the sorted set of titles carrying the dimension's levels and no longer from their posted order. The ids of the 364 cue signals are unchanged, no other field of any record changed, and `summary.json` is identical apart from the run id. No review rows exist, so nothing had to follow. The round's other fix (the archive metadata's list of requested ids is checked against its recorded digest and count) changes no output: this archive's metadata passes the check.

It was rebuilt a sixth time after the sixth review round, at `095c911` (run `results-structure-2026-11-14-3599e78b61`). That round changed how a signal from an analysis's compared groups is labelled (with the dimensions those groups differ on, not every dimension the measure's groups carry). This cohort has no such signal, so `summary.json` and `records.json` are identical to the previous build apart from the run id, which changes because the producing code did.

It was rebuilt a seventh time after the seventh review round, at `bf20e09` (run `results-structure-2026-11-14-a5a8dda9cb`). That round changed only how a fetched archive is named (every fetch is now frozen under a digest of its own record). This run was built from the existing archive with `--archive` and fetched nothing, so `summary.json` and `records.json` are identical to the previous build apart from the run id, which changes because the producing code did.

It was rebuilt an eighth time after the eighth review round, at `b3c9986` (run `results-structure-2026-11-14-9a083dc09a`). The archive file was renamed from `records-2026-11-14-cc8e86b9b06d.jsonl.gz` to the name its own fetch record gives under the current naming, because `--archive` now refuses an archive whose metadata is not the record it is named by. Its bytes are unchanged (the same sha256), and the only change to `archive.meta.json` is the `archive` field. `summary.json` and `records.json` are identical to the previous build apart from the run id. The round's other fix (a review date must be a calendar date) changes no output: there are no review rows.

It was rebuilt a ninth time after the ninth review round, at `3eebfdd` (run `results-structure-2026-11-14-225412bce9`). That round made the number started missing, not zero, for a record with a STARTED milestone and no flow group to sum over. This cohort has no such record, so `records.json` is identical to the previous build apart from the run id, and `summary.json` differs only in the run id and in the definition sentence of "Number started cannot be formed", which now names that case.

It was rebuilt a tenth time after the tenth review round, at `13c9ee1` (run `results-structure-2026-11-14-e5480bd529`). That round changed what an analysis signal shows and is keyed on (the compared groups' descriptions as well as their titles). This cohort has no such signal, so `summary.json` and `records.json` are identical to the previous build apart from the run id.

It was rebuilt an eleventh time after the eleventh review round, at the commit named above. That round refused an empty id list for a fetch and counted an analysis that repeats one group id as naming one group. Neither case occurs in this cohort, so `summary.json` and `records.json` are identical to the previous build apart from the run id.
