# Census benchmarks for single-site trials: build brief

A self-contained assignment for one session. Read all of it before writing
code. Where this brief and the repository's own instructions disagree, the
repository wins: read [AGENTS.md](../../AGENTS.md), [CLAUDE.md](../../CLAUDE.md)
and [verify.md](../../verify.md) first.

## Goal

For every single-site US trial with posted results, compare the race and
ethnicity of its participants with the census population around its one site,
restricted to residents who meet the trial's age and sex eligibility. Pool the
trials by area (metro area, state, Census region) and write one provenance-
stamped JSON file of area-level comparisons, plus a per-trial placement table.

This pull request computes and reports. It does **not** publish anything to
the site repository, change the weekly workflow, or change any published file.

## Method decisions

The investigator set these in a decision document (title "Census benchmarks
for single-site trials: methods decisions", 2026-11-10). If you can read that
document and a choice there differs from the default below, follow the
document and say so in the pull request. If you cannot read it, use these
defaults and say that you did.

| # | Decision | Default |
|---|---|---|
| 1 | Trials in scope | US trials whose `study_sites` list has exactly one entry, with `country` "United States" |
| 2 | Local comparison area | Metro or micropolitan area (CBSA); the county when the site is outside any CBSA |
| 3 | Wider levels | State and Census region (Northeast, Midwest, South, West) |
| 4 | Survey year | One pinned ACS 5-year release: the newest the API serves |
| 5 | Age and sex matching | Each trial's eligible ages and sex |
| 6 | Measure | Percentage-point difference (observed minus expected); ratio also written |
| 7 | Referral centres | Flag ZIPs with 200+ single-site trials; report every area with and without them |
| 8 | Floor | 50 qualifying trials per area, counted separately for race and for ethnicity |
| 9 | Placing a site | CT.gov coordinates, point-in-polygon against Census boundary files |

## Before writing code: check the network

Run these. Each must print 200.

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://api.census.gov/data.html"
curl -s -o /dev/null -w '%{http_code}\n' "https://www2.census.gov/geo/tiger/GENZ2023/"
curl -s -o /dev/null -w '%{http_code}\n' "https://clinicaltrials.gov/api/v2/version"
```

If a census.gov host fails, stop and ask the user to allow `api.census.gov`
and `www2.census.gov` for this environment. Do not substitute numbers from
memory, a different source, or a synthetic file presented as census data. A
Census API key is not needed at this volume.

## Inputs

**Trial records.** The weekly release asset, public:

```bash
curl -sSLO https://github.com/michaeldgreenphd/civicsample-engine/releases/download/data-2026-11-08/demographics.json.gz
```

A newer `data-*` release may exist; use the newest and record its tag. The
file is `{"extracted_at", "pipeline_commit", "data": [record, ...]}`. Fields
used, per record:

- `nct_id`, `study_sites` (`facility`, `city`, `state` as a full name, `zip`, `country`)
- `min_age`, `max_age`: strings such as `"18 Years"`, `"6 Months"`, or `"N/A"`
- `eligibility.sex`: `"ALL"`, `"FEMALE"`, `"MALE"`, rarely null
- `race.reported` and `race.omb_totals`: counts for `american_indian_alaska_native`,
  `asian`, `black_african_american`, `native_hawaiian_pacific_islander`, `white`,
  `more_than_one_race`, `other`, `unknown_not_reported`
- `ethnicity.reported` and `ethnicity.omb_totals`: `hispanic_latino`,
  `not_hispanic_latino`, `unknown_not_reported`

Expected counts in the 2026-11-08 release, for checking your filter: 30,761
single-site US trials; 30,420 with a ZIP; 18,682 with race reported; 13,351
with ethnicity reported; 2,866 female-only and 1,087 male-only; 14,781 with
`max_age` "N/A". If your counts differ on that release, find out why before
continuing.

**Coordinates.** The engine keeps the ZIP but not the coordinates
(`get_study_metadata` in `src/utils.py`). Fetch them from CT.gov API v2, 100
trials per request:

```
GET https://clinicaltrials.gov/api/v2/studies?filter.ids=NCT...,NCT...&fields=NCTId,ContactsLocationsModule&pageSize=100
```

Each `protocolSection.contactsLocationsModule.locations[]` entry has
`geoPoint: {lat, lon}`; a random sample of 200 single-site trials all had one.
Do not edit `get_study_metadata` in this pull request: that changes published
records and belongs in its own pull request.

**Census.**

- ACS 5-year tables B01001 (all residents), B01001A to B01001G (race groups)
  and B01001I (Hispanic or Latino), each sex by age. Fetch with
  `get=NAME,group(<table>)` for geographies
  `metropolitan statistical area/micropolitan statistical area:*`,
  `county:*` (with `in=state:*`), `state:*` and `region:*`.
- Derive the age bands from each table's variable labels
  (`/data/<year>/acs/acs5/groups/<table>.json`), not from hardcoded variable
  numbers. Assert the band count per sex (14 for the race tables) and that the
  bands tile 0 to 85+ with no gaps.
- Boundary files: the Census cartographic boundary county and CBSA files for
  the release's end year (`cb_<year>_us_county_500k`, `cb_<year>_us_cbsa_500k`).
  Check that the CBSA codes in the boundary file match the CBSA codes the API
  returns; stop and report if they do not.
- Record for every downloaded file: URL, retrieval time and sha256.

Category alignment (trial category → ACS table):

| Trial (`omb_totals` key) | ACS |
|---|---|
| `american_indian_alaska_native` | B01001C |
| `asian` | B01001D |
| `black_african_american` | B01001B |
| `native_hawaiian_pacific_islander` | B01001E |
| `white` | B01001A |
| `more_than_one_race` | B01001G |
| `other` | B01001F (Some other race) |
| `unknown_not_reported` | none: a trial-only row |
| `hispanic_latino` | B01001I |

## Method

Write each assumption as a comment next to the code that makes it.

1. **Place each site.** Point-in-polygon of the coordinates against the county
   and CBSA boundaries. State from the county's state FIPS; region from the
   state. If coordinates are missing, fall back to ZIP → county using the
   Census 2020 ZCTA-to-county relationship file (largest land-area overlap).
   Record the method per trial. A trial that cannot be placed is listed with a
   reason, never dropped silently.
2. **Eligible ages.** Parse `min_age`/`max_age` to years (months / 12, weeks /
   52, days / 365). "N/A" minimum = 0; "N/A" maximum = no upper limit. An
   unparseable value excludes the trial with the reason recorded.
3. **Band weights.** Weight each ACS band by the fraction of its years inside
   [min, max]. Treat 85+ as 85 to 100 for this purpose and say so in the code.
   This assumes ages are uniform within a band.
4. **Sex.** Female-only trials use the female columns; male-only the male
   columns; otherwise both.
5. **Expected share per trial.** For each category, weighted residents of that
   category over weighted residents of the B01001 total, in the trial's area.
6. **Observed share per trial.** Category count over the sum of all
   `omb_totals` for that dimension, unknown included. Both sides keep
   everyone in the denominator; the unknown row is never redistributed.
7. **Inclusion per dimension.** Race comparisons use trials with
   `race.reported` true and a positive total; ethnicity likewise. The three
   states (reported, explicitly unknown, not reported) stay distinct in the
   counts you report.
8. **Pool by area.** Observed = summed category counts over summed totals.
   Expected = the trials' expected shares weighted by their totals.
   `diff_pp` = observed − expected, in points; `ratio` = observed / expected
   (null when expected is 0, with the reason).
9. **Interval.** Bootstrap over trials within the area, 2,000 resamples,
   `numpy.random.default_rng` with a fixed seed recorded in the output.
   Report the 2.5th and 97.5th percentiles as a 95% interval and write
   `interval_level: 0.95`. This is the first random draw in the repository;
   say so in the pull request.
10. **Flags.** `concentration_flag` when one trial holds more than 50% of the
    area's participants. `referral_zip` on trials whose ZIP has 200 or more
    single-site trials (33 ZIPs, 11,830 trials in 2026-11-08). Produce every
    area twice: `with_referral` and `without_referral`.
11. **Floor.** Fewer than 50 qualifying trials → `display_rule: "withheld"`
    with `gate_reason` and the count; never filled from another level.
12. **Levels stay separate.** No output ranks or compares a CBSA with a state
    or a state with a region.

## Code layout

- `src/census_benchmarks.py`: pure functions (age parsing, band weights,
  expected and observed shares, pooling, bootstrap, flags). No network.
- `scripts/census_benchmarks.py`: the CLI. Downloads into a cache directory
  given by argument, places sites, runs the method, writes outputs to an
  output directory given by argument.
- `scripts/census/requirements.txt`: `shapely` and `pyshp` (both install from
  PyPI). Do not add them to the root `requirements.txt`.
- `tests/test_census_benchmarks.py`: offline tests on small hand-computed
  inputs: band overlap at edges and partial bands, "N/A" ages, months,
  single-sex trials, pooling weights, a withheld area, the concentration flag,
  the unknown row staying out of redistribution, and a fixed-seed bootstrap
  giving the same interval twice. The root suite must not import shapely.
- Type hints on every function. Add the cache and output directories to
  `.gitignore`; do not commit census downloads or outputs.

## Outputs

`census_benchmarks.json`:

```json
{
  "schema_version": "census_benchmarks/0.1",
  "extracted_at": "...",
  "pipeline_commit": "<this repo's HEAD>",
  "source_release": "data-2026-11-08",
  "source_pipeline_commit": "<from demographics.json.gz>",
  "acs_release": "acs5 <year>",
  "files": [{"url": "...", "retrieved_at": "...", "sha256": "..."}],
  "parameters": {"floor": 50, "referral_zip_min_trials": 200, "bootstrap_resamples": 2000, "seed": 0},
  "areas": [
    {"level": "cbsa", "geoid": "...", "name": "...", "variant": "with_referral",
     "dimension": "race", "n_trials": 0, "n_participants": 0,
     "display_rule": "show", "gate_reason": null, "concentration_flag": false,
     "categories": [{"category": "black_african_american", "observed_pct": 0.0,
       "expected_pct": 0.0, "diff_pp": 0.0, "ratio": 0.0, "lo_pp": 0.0, "hi_pp": 0.0,
       "interval_level": 0.95}]}
  ]
}
```

`trial_placements.csv`: `nct_id, lat, lon, placement_method, county_geoid,
cbsa_geoid, state_fips, region, zip, referral_zip, race_included,
ethnicity_included, exclusion_reason`.

## Acceptance criteria

- The CI checks in AGENTS.md "Running the checks" pass, run and reported.
- A full run against the real release completes, and the pull request body
  reports: trials placed by each method and unplaced with reasons; areas shown
  and withheld per level, dimension and variant; the ACS release and file
  checksums; and, for the four regions and the five largest CBSAs by trial
  count, observed, expected and `diff_pp` for each category.
- Re-running with the same inputs and seed gives a byte-identical
  `census_benchmarks.json` apart from `extracted_at`.
- The pull request says plainly what it did not do: no site changes, no
  contract, no weekly workflow, no `get_study_metadata` change, ACS margins of
  error not propagated.

## Working rules

- Work on the branch your session is assigned. Open a **draft** pull request.
  Do not merge; the owner merges. No force pushes.
- After each push that changes the diff, comment `@codex review` on the pull
  request.
- No secrets in code, logs, commits or comments. No paid API calls.
- The comparison is with **area residents**, not with people who have the
  condition. Any prose you write (docstrings, pull request body) says
  "compared with area residents" and never "underrepresented" without that
  qualifier.
- If something in this brief turns out to be wrong (a table id, a count, a
  file name), stop at that step, report what you found, and do not guess
  around it.
