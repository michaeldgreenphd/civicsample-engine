# Audit record: what posted results sections contain

Two read-only audits of ClinicalTrials.gov results sections for the trials in
the demographics dataset, and an independent recount of their headline
figures. They were run on 2026-11-13 to decide whether an analysis linking
representation to posted outcomes is feasible, and for which trials.

**Status: provisional.** These are feasibility findings. Nothing here changes
the weekly pipeline, and no figure here is published on the site. The second
pass supersedes the first wherever they differ; section 8 of
`second_pass/report.md` lists each earlier figure beside its refined wording
or value.

No paid API or model call was made by any script here. Judgements made by
reading records (the subgroup review and the endpoint readings) are those of
one reviewer, the auditing model. They are not a human review.

## What is in this directory

| Path | Contents |
|---|---|
| `first_pass/` | First audit: report, sampling record, the 2,000 sampled identifiers, the cached registry responses, analysis output and code. |
| `second_pass/` | Refined audit after investigator review: report, the rule definitions (`definitions.json`), per-trial flags, endpoint families and candidate cohorts, the subgroup review table and the digests that were read, cached responses, code. |
| `independent_recount/` | Two short scripts that recount headline figures from the cached responses, with their output. |

## Source population and inclusion rule

- **Source:** the weekly release `data-2026-11-08`, file
  `demographics.json.gz`, sha256
  `2168d258dd26d1c824f4a090b07df7f1dc8ad58d71a6a15598e5115c258c7fa2`,
  80,890 records, `pipeline_commit`
  `f6a2e7b74790c6f11982376441fb90c80d2c611e`, `extracted_at`
  2026-11-08T13:07:45Z.
- **Inclusion rule:** every study with a results-first-posted date on or
  after 2009-01-01 (`AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX]`), with
  no other filter. **Posted results are therefore required.** Every
  percentage computed on the sample is conditional on a trial having posted
  results, and nothing in the sample describes trials that have not. Section
  1.1 of the second-pass report traces the rule through the code.
- **Results posting among all registered studies** is reported separately,
  from registry count queries (section 1.3). Those are counts on the
  retrieval date, not a per-trial audit.

## Sampling

- Proportional stratified by study type: 1,883 interventional and 117
  observational, 2,000 in total (2.5% of the frame).
- Seed `20261113`. The identifiers are in `first_pass/sample_ids.txt`
  (sha256
  `9bd0c403097b055faf01b505fbd0b98a6ca21611d965c8878751f11f1e0d525f`), and
  the second pass re-drew the sample from the release and got the same list
  (`second_pass/sampling_verification.json`).
- Intervals in the reports are Wilson 95% intervals that treat the stratified
  sample as a simple random sample.

## Retrieval and versions

- **Sampled records:** fetched from the ClinicalTrials.gov API
  2026-11-13T22:32:29Z to 22:33:01Z, API version 2.0.5, registry
  `dataTimestamp` 2026-11-13T10:00:06. 2,000 of 2,000 returned. The
  unparsed responses are in `first_pass/cache/raw_responses.jsonl.gz`
  (sha256
  `a9e4577735b5f3ece14ff672a5db0f4ca70be283f237bb996c09328c3569510a`).
- **Second pass:** 243 further requests (count queries, document sections,
  endpoint-family top-up), 2026-11-13T23:19:50Z to 23:41:15Z, all HTTP 200.
  Each is logged with URL, time, status, size and sha256 in
  `second_pass/cache/fetch_manifest.jsonl`.
- The records were read five days after the release was pulled. Registry
  records change; a re-fetch today would not return the same bytes, which is
  why the responses themselves are kept.

## Missing data, failures and negative findings

Section 6 of the second-pass report keeps these apart: retrieval failures
(none), parsing failures (none in the cache; the parser's residual classes
are listed), missing data in the records, and negative findings. A negative
finding is stated with the scope of the search that produced it.

## The three questions, kept separate

The first pass ran these together. The second pass reports each on its own
denominator:

1. **Whether eligible trials post results.** Count-level only, from registry
   queries; the sample cannot answer it.
2. **Among trials with posted results, whether demographics and usable
   outcomes are present.** Module *presence* (1,997 of 2,000 have all five
   modules) is distinct from *usable content*, which is defined per module by
   rules B1, F1, O1 and A1 in `second_pass/definitions.json`. The "more
   information" module is not counted as substantive.
3. **Among comparative trials, whether statistical analyses are posted.**
   Denominator: randomised trials with two or more groups on a primary
   measure.

## What the independent recount does and does not establish

`independent_recount/` holds two scripts written from the reports' wording,
without reading the audits' analysis code. They read the same cached
responses and the same release.

- **What agreed:** every first-pass headline count recounted, and every
  second-pass comparative count recounted (C1 1,394; C2 1,134; between-arm
  analysis 473; estimate with both limits 310; the Phase 3 figures; race
  reported with an estimate 206).
- **One difference:** "primary measure carrying data" is 1,913 in the
  recount and 1,915 in the first pass. The two scripts define "carrying
  data" slightly differently. The second pass replaced this figure with rule
  O1 (1,905), so it is no longer used.
- **What this establishes:** the counts follow from the stated definitions
  and the cached records, and a second implementation of those definitions
  reaches them. It is an engineering check.
- **What it does not establish:** that the definitions measure what the
  study needs. The recount takes the reports' definitions as given, reads
  the same fields and shares their assumptions, for example that two or more
  groups on a primary measure means a comparative trial, and that an
  analysis row with a numeric estimate and both limits is a usable effect
  estimate. Whether those are the right constructs is a scientific question
  the recount cannot answer. Nor does it check the reader's judgements in
  the subgroup review, which have had no second reviewer.

**The 2020 cohort build.** `independent_recount/recheck_cohort.py` recounts the results-structure
build of the 598 eligible trials (`docs/results_structure/runs/2026-11-14/`) from its frozen
registry archive: 416 with posted results, 406 comparative (C1 and C2), 291 with a between-arm
analysis on a primary measure and 258 with an estimate and both limits. All five equal the
build's figures. The same limits apply: it confirms the counting under the stated rules, not
the rules. The archive (7.9 MB) is not committed; its sha256 is in that run's `run.json`.

## What is not stored here, and how to regenerate it

Two derived files are left out for size. Both are rebuilt from the release
file named above.

| File | Size | Rebuilt by |
|---|---|---|
| `first_pass/sample_frame.json` | 9.8 MB | `first_pass/scripts/draw_sample.py RELEASE_GZ first_pass` |
| `second_pass/release_slim.jsonl.gz` | 66 MB | `second_pass/scripts/build_slim.py RELEASE_GZ second_pass` |

Four per-trial tables in `second_pass/` are stored gzip-compressed
(`per_trial_*.jsonl.gz`, `family_detail_rows.jsonl.gz`). The scripts write
them uncompressed.

## Running the recount

The release file is the `demographics.json.gz` asset of the `data-2026-11-08`
release. From this directory:

```bash
python3 -I independent_recount/recheck.py first_pass/cache/raw_responses.jsonl.gz RELEASE_GZ
```

```bash
python3 -I independent_recount/recheck_v2.py first_pass/cache/raw_responses.jsonl.gz RELEASE_GZ
```

The output of each should equal the stored `recheck_output.txt` and
`recheck_v2_output.txt`. Section 9 of the second-pass report gives the full
offline re-run order for the audit itself.

## Limits that apply to every figure

- Conditional on posted results, as above.
- One seeded sample; rare features rest on a handful of trials.
- "Subgroup reporting detected by the current method" is what a text rule
  over 18 structured fields found, plus one reviewer's labels: 136 trials
  detected, 41 judged to post a subgroup result, 71 judged false positives.
  It is not a prevalence. Documents and publications were not reviewed.
- Endpoint families are text definitions on the first registered primary
  outcome title. Identical titles are lexically identical only; construct,
  instrument, unit, time point, analysis population and effect measure were
  read for a limited number of trials per family (section 3.3).
- Outcome switching cannot be established from these records and was not
  assessed.
- No subgroup outcome is inferred from aggregate results, and no
  study-level association here supports a causal reading.
