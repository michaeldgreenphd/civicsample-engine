# Census benchmark run on release data-2026-11-08

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-14T00:53:58+00:00 by `scripts/build_census_benchmarks.py` at engine commit `0295a6fe4445` (no uncommitted changes) |
| Trial records | release `data-2026-11-08`, extracted 2026-11-08, pipeline commit `f6a2e7b74790`, sha256 `2168d258dd26d1c8…` |
| Registry text and locations | 30,761 of 30,761 trials retrieved 2026-11-13, sha256 `75a2fec8613e2174…` |
| Census sources | 217 files (ACS 5-year tables and boundary files), each listed in `run.json` with its URL (key removed), retrieval time and sha256 |

## Files here

- `run.json`: the manifest. Parameters, sources, limitations, and the sha256 of every output file.
- `summary.json`: every count with its definition, including each reason a trial or an area is absent.
- `check_against_api.txt`: output of `scripts/census/check_against_api.py`, the second-code-path
  recomputation of reference shares from the Census API.

## Files not here

`areas.json` (4a6b74161db0747d…) and `trials.jsonl.gz` (ef827e9321cc3f22…) are
8 MB and 28 MB and are not committed; their sha256 values are in `run.json`. Rebuilding from the
same release, registry archive and cached Census files reproduces them byte for byte: two
consecutive builds were compared at commits `6f96dd9` and `9b30103`. `run.json` differs between
builds only in `generated_at`, which is kept as provenance.

## How this run differs from the earlier ones

The run was rebuilt once after each review round that changed the method, and compared field
by field with the build before it. Cached Census tables and boundary files are reused only
when their bytes match their recorded sha256.

| Built at | What changed in the outputs |
|---|---|
| `7b9307e` | First reviewed run. Against the unreviewed one: the margin of error of Not Hispanic or Latino (299 values), which a review fix corrected. |
| `4d541b6` | `areas.json` identical. Three trials with no usable date keep their tables and are marked as having no benchmark window, so each dimension accounts for all 30,761 cohort trials. |
| `14e0bf1` | `areas.json` identical. The one trial with unreadable eligibility ages is written as having no benchmark at every level. |
| `0295a6f` (this record) | The survey window is assigned from the unrounded enrolment midpoint. 16 of 30,758 dated trials move to the earlier window (2005-2009 +2, 2010-2014 +1, 2015-2019 +8, 2020-2024 -11), and 4 trials that were recorded as after the last window are inside it. 172 of 16,195 area records change, 139 of them shown; no area moves between shown and withheld, and the largest change in a shown difference is 0.11 points. |

Two later checks change no output of this run and it was not rebuilt for them: a single
facility at a different place in today's registration than in the release (0 of 30,761), and
a place field present in one record and missing from the other (0 of 30,761).

`check_against_api.txt` was rerun against this build.

## What this record does not establish

That the build is reproducible and its arithmetic checks out is engineering validation. Whether
a site-area benchmark is an appropriate comparison for a given trial is a scientific question
this run does not answer; see `census/README.md`.
