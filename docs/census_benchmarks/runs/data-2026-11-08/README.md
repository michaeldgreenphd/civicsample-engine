# Census benchmark run on release data-2026-11-08

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-14T00:31:37+00:00 by `scripts/build_census_benchmarks.py` at engine commit `14e0bf1bf32b` (no uncommitted changes) |
| Trial records | release `data-2026-11-08`, extracted 2026-11-08, pipeline commit `f6a2e7b74790`, sha256 `2168d258dd26d1c8…` |
| Registry text and locations | 30,761 of 30,761 trials retrieved 2026-11-13, sha256 `75a2fec8613e2174…` |
| Census sources | 217 files (ACS 5-year tables and boundary files), each listed in `run.json` with its URL (key removed), retrieval time and sha256 |

## Files here

- `run.json`: the manifest. Parameters, sources, limitations, and the sha256 of every output file.
- `summary.json`: every count with its definition, including each reason a trial or an area is absent.
- `check_against_api.txt`: output of `scripts/census/check_against_api.py`, the second-code-path
  recomputation of reference shares from the Census API.

## Files not here

`areas.json` (950664a9c1a05171…) and `trials.jsonl.gz` (5e6e81688d78952c…) are
8 MB and 28 MB and are not committed; their sha256 values are in `run.json`. Rebuilding from the
same release, registry archive and cached Census files reproduces them byte for byte: two
consecutive builds were compared at commits `6f96dd9` and `9b30103`. This run was built once, at the commit above, from Census tables and boundary files whose cached bytes are checked against their recorded sha256. Against the first reviewed run its area figures differ only in the margin of error of Not Hispanic or Latino (299 values), which a review fix corrected; the area file is byte-for-byte the same as the run before it. Each dimension now accounts for all 30,761 cohort trials: three trials with no usable date keep their tables and are marked as having no benchmark window. The one trial whose eligibility ages cannot be read is now written as having no benchmark at every level, with that reason, so each level's available and unavailable benchmarks add up to the reported tables; against the build at `4d541b6` that is the only change, and `areas.json` is byte-for-byte the same. `check_against_api.txt` was produced against that same `areas.json` and was not rerun. No single-facility trial is at a different place in today's registration than in the release (0 of 30,761), so the check added for that case changes nothing here.
`run.json` differs between
builds only in `generated_at`, which is kept as provenance.

## What this record does not establish

That the build is reproducible and its arithmetic checks out is engineering validation. Whether
a site-area benchmark is an appropriate comparison for a given trial is a scientific question
this run does not answer; see `census/README.md`.
