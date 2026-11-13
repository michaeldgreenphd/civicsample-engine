# Census benchmark run on release data-2026-11-08

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-13T23:30:42+00:00 by `scripts/build_census_benchmarks.py` at engine commit `c372ceae82e6` (no uncommitted changes) |
| Trial records | release `data-2026-11-08`, extracted 2026-11-08, pipeline commit `f6a2e7b74790`, sha256 `2168d258dd26d1c8…` |
| Registry text and locations | 30,761 of 30,761 trials retrieved 2026-11-13, sha256 `75a2fec8613e2174…` |
| Census sources | 217 files (ACS 5-year tables and boundary files), each listed in `run.json` with its URL (key removed), retrieval time and sha256 |

## Files here

- `run.json`: the manifest. Parameters, sources, limitations, and the sha256 of every output file.
- `summary.json`: every count with its definition, including each reason a trial or an area is absent.
- `check_against_api.txt`: output of `scripts/census/check_against_api.py`, the second-code-path
  recomputation of reference shares from the Census API.

## Files not here

`areas.json` (b187ba881d6ea303…) and `trials.jsonl.gz` (6ad20f4e93ddc38a…) are
7 MB and 27 MB and are not committed; their sha256 values are in `run.json`. Rebuilding from the
same release, registry archive and cached Census files reproduces them byte for byte: two
consecutive builds were compared at commits `6f96dd9` and `9b30103`. `run.json` differs between
builds only in `generated_at`, which is kept as provenance.

## What this record does not establish

That the build is reproducible and its arithmetic checks out is engineering validation. Whether
a site-area benchmark is an appropriate comparison for a given trial is a scientific question
this run does not answer; see `census/README.md`.
