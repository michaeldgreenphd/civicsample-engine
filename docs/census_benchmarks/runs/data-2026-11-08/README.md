# Census benchmark run on release data-2026-11-08

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-14T01:17:17+00:00 by `scripts/build_census_benchmarks.py` at engine commit `cc15b60c9808` (no uncommitted changes) |
| Trial records | release `data-2026-11-08`, extracted 2026-11-08, pipeline commit `f6a2e7b74790`, sha256 `2168d258dd26d1c8…` |
| Registry text and locations | 30,761 of 30,761 trials retrieved 2026-11-13, sha256 `75a2fec8613e2174…` |
| Posted baseline tables | 3,489 of 3,489 race tables titled with ethnicity, retrieved 2026-11-14, sha256 `87c92cc03d422114…` |
| Census sources | 217 files (ACS 5-year tables and boundary files), each listed in `run.json` with its URL (key removed), retrieval time and sha256 |

## Files here

- `run.json`: the manifest. Parameters, sources, limitations, and the sha256 of every output file.
- `summary.json`: every count with its definition, including each reason a trial or an area is absent.
- `check_against_api.txt`: output of `scripts/census/check_against_api.py`, the second-code-path
  recomputation of reference shares from the Census API.

## Files not here

`areas.json` (867cec48e2011829…) and `trials.jsonl.gz` (c7964ccffb6bdec4…) are
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
| `0295a6f` | The survey window is assigned from the unrounded enrolment midpoint. 16 of 30,758 dated trials move to the earlier window (2005-2009 +2, 2010-2014 +1, 2015-2019 +8, 2020-2024 -11), and 4 trials that were recorded as after the last window are inside it. 172 of 16,195 area records change, 139 of them shown; no area moves between shown and withheld, and the largest change in a shown difference is 0.11 points. |
| `cc15b60` (this record) | Two corrections to how combined race and ethnicity tables are read. **Race:** the registry's own baseline table was fetched for all 3,489 race tables titled with ethnicity; 1,501 have a non-zero ethnicity row among the race rows and race rows that fall short of the population, and are no longer compared (1,578 race tables are now excluded as combined classifications, against 77 before). Pooled race tables fall from 15,775 to 14,371. The national difference for White moves from -3.39 to -0.94 points and for Black or African American from +5.90 to +6.15; the mean unknown share falls from 7.5% to 5.0%. Eight area records move from shown to withheld. **Ethnicity:** 1,380 ethnicity tables carry a row for only one of the two categories with unknowns beside it, and no longer get a known-category share; the national known-category Hispanic share moves from 24.12% to 14.51%. The all-participants ethnicity figures and the sex figures are unchanged. |

Two later checks change no output of this run and it was not rebuilt for them: a single
facility at a different place in today's registration than in the release (0 of 30,761), and
a place field present in one record and missing from the other (0 of 30,761).

`check_against_api.txt` was rerun against this build.

## What this record does not establish

That the build is reproducible and its arithmetic checks out is engineering validation. Whether
a site-area benchmark is an appropriate comparison for a given trial is a scientific question
this run does not answer; see `census/README.md`.
