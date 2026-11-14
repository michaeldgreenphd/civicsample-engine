# Industry Outcomes beta (manual)

Evidence availability for a fixed cohort of industry-sponsored Phase 3 trials, and a small set of
traceable assessments of whether each trial met its prespecified primary objective. It runs by
hand; no workflow calls it, and the weekly demographics job, its cohort and its files are
unchanged.

## What it computes

| Step | Module | Notes |
|---|---|---|
| Acquire the frame | `acquire.py` | CT.gov API v2, query in `evidence.FRAME_QUERY` (interventional, Phase 3, randomized, INDUSTRY lead class, primary completion in 2020). Fetches `protocolSection,documentSection,hasResults` and freezes the result as a sorted, byte-stable `jsonl.gz` with its sha256 under `build/outcomes_cache/`. `--archive` reuses one. |
| Eligibility | `evidence.eligibility` | Codebook E1-E5 with exclusion reasons; Phase 2/3 is excluded as its own stratum; month- or year-precision completion dates must lie wholly inside 2020. 648 frame records gave 598 eligible on 2026-11-09. |
| Results availability | `evidence.results_state` | POSTED, or the absence state recomputed at the cutoff on every build: PENDING (within the one-year window, certified delay, or completion not confirmed), UNREPORTED (past the standard window), NOT_APPLICABLE (withdrawn, or terminated with zero enrolment). Not a finding of non-compliance: applicable-trial status and extensions are not established. |
| Sponsor | `attribution.py` | Curated lead-sponsor rules (`sponsors/company_aliases.csv`, by sha256) with the three review states kept; unreviewed literals shown as registered. The Industry tab's regex recode is computed alongside and every difference is counted, by review state. The Industry tab is not changed. |
| Demographics link | `link.py` | Which eligible trials are in the site's participant-demographics dataset (read through record-contract fields only), and whether they are in the Industry tab's cohort. |
| Assessments | read, not computed | From the private repository's allowlisted export (`--assessments`), read at run time and never copied here. Its eligibility must equal this module's for every audit trial, or the build stops. |

## Artifacts (schema version 1)

`run.json`, `summary.json`, `index.json`, `records/rNN.json`; see the docstring of
`artifacts.py`. Every count in `summary.json` names its denominator and definition; the browser
computes no aggregate. The run manifest records the cutoff, the acquisition's sha256, the
producer commit, the method version and spec sha, the sponsor rules sha, the demographics
snapshot used, the display mode, and the sha256 of every other file. Outcomes, assessments and
demographics each carry their own date.

## Display boundary

The export's mode decides which assessments exist:

- `evidence_only`: evidence facts plus human-reviewed assessments. The only mode a public
  release may use. `scripts/check_outcomes_contract.py PATH --publishable` enforces it.
- `provisional_preview`: also machine assessments of the development collection, each labelled
  `machine_provisional`. For a local preview only; never commit or publish it.

## Running it

```bash
python scripts/build_outcomes_beta.py --fetch --assessments /private/export.json \
    --site ../clinical-trial-populations --out ../clinical-trial-populations/data/outcomes
python scripts/check_outcomes_contract.py ../clinical-trial-populations/data/outcomes --publishable
python scripts/build_outcomes_fixture.py   # after a contract change: regenerates tests/fixtures/outcomes/
python -m pytest tests/outcomes -q
```

The site ignores `data/outcomes/` in git until publication is decided; writing there is a local
preview. Scheduled publication is not enabled.

## Compatibility

New files only: `outcomes/`, three scripts, `tests/outcomes/`, `tests/fixtures/outcomes/`. No
existing module, workflow, published file or cohort definition changes. `sponsors/` and
`scripts/generate_industry_sponsors.py` are imported read-only.

## Results structure and subgroup-reporting states (manual)

A second manual build in this package. It reads the posted results section of each listed trial
and writes, per trial, what the section contains and one subgroup-reporting state per dimension.
No workflow calls it. It does not change the outcomes beta build above or any weekly file.

### What it computes

`results_structure.py` holds pure functions over one ClinicalTrials.gov API v2 study record. The
rules are ported from the audit scripts under
`docs/audits/outcomes_results_structure/second_pass/scripts/`, and their definitions are in
`docs/audits/outcomes_results_structure/second_pass/definitions.json`.

| Fact | Rule and source |
|---|---|
| Results status | `hasResults`: posted, or no posted results. A record without a boolean `hasResults`, or with results announced and no results section, is a parsing failure (`unknown`). |
| Module presence | The four substantive modules: baseline, participant flow, outcome measures, adverse events. The "more information" module is administrative and is not counted. |
| Usable content | B1 (baseline), F1 (participant flow), O1 (primary outcome), A1 (adverse events), each with the reason when it fails. A rule that could not be evaluated is `null` with `not_evaluated_parse_failure`, not `false`. |
| Registered and posted outcomes | Registered outcomes by type (count, titles, time frames). Posted measures by type: how many carry data, units, parameter types, number of groups, how many state their number analysed in participants. |
| Comparative class | C1: interventional with two or more groups on a posted primary measure. C2: C1 and registered as randomised. |
| Between-arm analysis | Rule S1 on primary measures: the estimate type, whether both confidence limits are numeric, and whether the p-value is a plain number or a bound such as `<0.001`. |
| Participant flow | Started and completed per arm, first period only; the record says so and gives the number of periods. |
| Baseline population | Baseline total (rule T: the Total group is never added to its arms), number started, registered enrolment with its type, and the population description when posted. Each number names the field it came from. |
| Adverse events | Whether serious and other event rows are posted, numbers at risk per group, the frequency threshold. |
| Subgroup signals | Rule SG2 over 18 structured fields of every posted outcome measure: field path, measure index, type and title, the matched text verbatim, and the dimension (age, sex, race, ethnicity, other). |

Every record carries `method_version` (`results-structure-1`) and
`document_and_publication_review: "not_reviewed"`.

### Subgroup states

One state per study and one per dimension (age, sex, race, ethnicity):

| Value | Meaning |
|---|---|
| `not_applicable_no_posted_results` | The registry holds no posted results. This describes the registry, not this method. |
| `not_yet_processed` | The trial was requested and its record was not retrieved, so its results were not read. The site shows the same label for a trial absent from the file. |
| `processing_failed_or_indeterminate` | The record or its outcome measures could not be parsed, or the rule fired and no human review has settled it. |
| `no_explicit_subgroup_results_detected_in_reviewed_structured_fields` | The listed fields were read and the rule found nothing, or a human review rejected every signal. |
| `explicit_subgroup_results_detected` | At least one signal accepted by a human review record. |

A signal whose dimension is `other` (the word "subgroup" with no named characteristic, or
national-origin levels) leaves all four dimensions indeterminate until a person settles it.

Human reviews are read from `subgroup_reviews.csv` (columns `nct_id, dimension, signal_id,
decision, reviewer, reviewed_on, note`; decision is `accepted`, `rejected` or `unresolved`).
`load_reviews` raises on a wrong header, an empty note or reviewer, and on two rows for the same
signal and dimension. The file ships with a header and no rows, so no trial is in the detected
state; that is intended. The audit's labels
(`docs/audits/outcomes_results_structure/second_pass/subgroup_review.csv`) were made by one
automated reader and are not human reviews; the loader refuses that file. The reviewer's name and
note stay in the review file and are not written to the artifacts.

### What it does not do

- It does not read posted protocols, statistical analysis plans, study reports or publications.
- It does not infer a result for a subgroup from a trial's overall result and baseline proportions.
- It does not assess outcome switching.
- It does not judge whether a table's content is correct or complete, or whether two groups are
  two randomised arms.
- Its subgroup figures are subgroup reporting detected by the current method, not a prevalence.

### Artifacts (schema version 1)

`run.json`, `summary.json`, `records.json`; see the docstring of `results_structure_build.py`.
`run.json` records the run id, producer commit, method version, the archive's sha256, the
retrieval window, the registry API version and `dataTimestamp`, the parameters, the fields
inspected and not inspected, and the sha256 of the other two files. `summary.json` gives every
count with its numerator, denominator and definition, and keeps retrieval failures, parsing
failures, missing data and negative findings in four separate tables. `records.json` holds one
record per requested NCT id, including trials that were not retrieved. The browser computes no
aggregate.

### Running it

```bash
python scripts/build_results_structure.py --fetch --nct-list ids.txt \
    --out ../clinical-trial-populations/data/results_structure
python scripts/build_results_structure.py --archive build/results_structure_cache/NAME.jsonl.gz \
    --out build/results_structure
python scripts/build_results_structure_fixture.py   # after a rule or record change: regenerates tests/fixtures/results_structure/
python docs/audits/outcomes_results_structure/port_check/port_check.py   # the port against the audit's 2,000 cached records
python -m pytest tests/outcomes -q
```

`--fetch` asks the registry for the full record of each listed id (no key; one request at a
time, at least 0.5 s apart) and freezes them under `build/results_structure_cache/` as a sorted,
byte-stable `jsonl.gz` with its sha256. `--archive` reuses one. Run records are kept under
`docs/results_structure/runs/`.

A build can cover fewer trials than the archive holds, for example the eligible cohort of a
frame that was fetched whole:

```bash
python scripts/build_results_structure.py --archive build/results_structure_cache/NAME.jsonl.gz \
    --nct-list eligible_ids.txt --exclude-list excluded_ids.txt \
    --exclude-reason "excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded)" \
    --out ../clinical-trial-populations/data/results_structure
```

Every id the archive was fetched for must then be in one list or the other, or the build stops.
`run.json` (`scope`) and `summary.json` (`scope`) state the frame size, the number of trials
included, and the number excluded with the reason; excluded records have no record in
`records.json` and are in no count. A listed trial that the archive does not hold gets a
`not_yet_processed` record with the reason, never an empty result.

### Compatibility

New files only: `results_structure.py`, `results_structure_build.py`, `subgroup_reviews.csv`,
two scripts, `tests/outcomes/test_results_structure.py`, `tests/outcomes/rs_fixture.py`,
`tests/fixtures/results_structure/`. `acquire.encode` is imported read-only. The site reads
`data/results_structure/` when it is present and shows every trial as not yet processed when it
is absent.
