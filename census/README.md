# Census benchmarks for single-site US trials (manual)

For each trial whose registration lists one US facility, compares the race, ethnicity and sex of
its participants with a **selected reference population**: residents of the area around that
facility who meet the trial's age and sex eligibility, from the American Community Survey. It
runs by hand; no workflow calls it, and the weekly job, its cohort and its files are unchanged.

## What the comparison is, and is not

Every figure is a **site-area population benchmark**: the trial's participants beside residents
of an area around its listed site. Nothing here records where participants live or the study's
recruitment catchment, so the area is a proxy for a potential recruitment population. It is
**not** the population with the condition, the population eligible for the trial, or an
enrolment target, and a referral centre may recruit nationally. Each trial carries
`reference_population.fit`: `contextual` whenever a general-population denominator does not
match it (it enrols patients, is limited to children or older adults or one sex, or sits in a
referral-centre ZIP), which is nearly always. Text built on these files says "compared with
residents of the selected area", never "underrepresented" on its own.

## Which area

The choice of area changes the result, sometimes by more than the gap being measured: for
trials in ZIP 77030 (Houston's medical centre) the reference share of Asian residents is 25%
in the ZCTA and 7% in the county. So no level is treated as the answer:

- every trial is benchmarked at ZCTA, county, metro area, state, region and nation, side by side;
- each trial records `geography_sensitivity`: how far the reference share of any category moves
  between the local levels available to it, flagged at 10 points or more (a judgement);
- the **default for display is the metro area**, not the smallest area. A ZCTA around a medical
  campus can hold almost no residents, ZCTA tables exist for only three of the four windows, and
  small areas have the widest margins of error. The metro area resolves for nearly every site
  and reflects commuting. It is a default, not a claim about catchment.

## What it computes

| Step | Module | Notes |
|---|---|---|
| Cohort | `trials.cohort` | Records whose registration lists exactly one facility, in the US. A trial with several facilities is never single-site, even when only one has a usable address; that case is counted. |
| Site-count tier | `trials.site_tier`, `registry.py` | The registry has no single-site field. The study's own text decides the tier: `self_described_single_site`, `one_listed_facility` (no statement), `conflicting_text`, `text_indicates_multiple_sites` (left out of every pooled figure), `listing_changed_since_release` (today's record lists a different number of facilities, or one facility at a different city, state, country or ZIP, judged only on fields both records carry; left out of every pooled figure, and today's city point is not used beside the release's ZIP. A field one record lacks is listed under `place_fields_not_comparable` and is not treated as a move). Self-description is not third-party verification, and only today's registration is read. |
| Enrolment period | `trials.enrolment_period` | Start date to primary completion date, the closest proxy the registry has; the basis is recorded per trial. |
| Benchmark window | `trials.assign_window` | One of four **non-overlapping** ACS 5-year windows (2005-2009 … 2020-2024) by the period's midpoint, with the reason and any gap in years. The midpoint is recorded to two decimals; the window is assigned from the unrounded value (`trials.enrolment_midpoint`), so a midpoint in the last days of a year stays in that year's window. |
| Site geography | `geography.Resolver` | Per window, in that window's own boundary files. ZIP → ZCTA only when the code is published as a ZCTA; county and metro area from the ZCTA polygon's largest area share, the share kept as confidence; the registry's city point only as a labelled fallback. Unresolved units carry a reason. |
| Reference population | `acs.py`, `benchmark.expected` | Sex-by-age tables by race (B01001A–G), Hispanic origin (B01001I) and all residents (B01001), restricted to the eligible ages and sexes, at ZCTA, county, metro area, state, region and nation side by side. 90% margins of error are carried through. |
| Comparison | `benchmark.compare` | Percentage-point difference and representation ratio per category; the ratio is recorded as undefined when the reference share is zero. |
| Areas | `benchmark.pool` | Each trial counts once (the investigator's choice, 2026-11-10); a participant-weighted figure is secondary. Bootstrap interval over trials; areas under 50 qualifying trials are withheld with their count. Variants: site basis (one listed facility / self-described only) × with or without referral-centre ZIPs. |

## Categories and denominators

Race and Hispanic ethnicity are separate questions on both sides and stay separate; a registry
table that combines them is not used. Trial categories map one to one onto ACS tables
(`acs.TABLES`); the registry's `other` is paired with Some Other Race, the loosest pairing.
The original category labels are kept beside the harmonised counts. Sex is the registry's sex
field against the survey's; nothing here describes gender identity or sexual orientation. Age
is matched on the reference side only: the registry does not report participants' ages by
race, and the engine does not parse baseline age distributions.

Two denominators are reported for every comparison, with their participant counts and the
unknown share beside them. Neither replaces the other.

| | Denominator | Reading |
|---|---|---|
| A | Everyone the registry table counts | Unknown or not reported is its own row, so each share is a floor. `unknown_bounds` gives the difference at that floor and at the ceiling (every unknown in the category); `direction_settled` is false when the two differ in sign. Ratios use A unless labelled otherwise. |
| B (`of_classified`) | Participants with a reported, classifiable category | A second view, **not** a correction that recovers the true distribution: people without a recorded category may differ from those with one, and B is biased where a table reports one category and files everyone else as unknown. |

Before any share is computed, `trials.category_structure` checks that the table's rows can be
read as each participant counted once, in a classification the survey's tables match. A race
table that carries an ethnicity row ("Hispanic", "White/Non-Hispanic") is a combined
classification and is not compared; a table adding up to more than the trial's sex table has
counted someone twice. Either is kept in the per-trial file with the reason and left out of
every pooled figure.

The weekly extractor leaves a purely ethnicity row to the ethnicity pipeline, so the release
carries no label for it: a table that classified people as White / Black / Hispanic arrives
looking like a race table whose Hispanic participants are "unknown". For every race table whose
title mentions ethnicity, the build therefore fetches the registry's own baseline table
(`registry.ethnicity_rows_in_race_tables`, a second frozen archive with its sha256). Where the
race rows fall short of the table's population and the registry table has a non-zero ethnicity
row, the table is not compared. Where the race rows cover everyone, it is compared with a
caution, whether or not ethnicity rows stand beside them. A flagged table with a shortfall whose
registry table could not be retrieved is not compared and says so.

The trial side is the registry's baseline characteristics table: the baseline analysis
population with all arms combined. It can differ from enrolment; the ratio is recorded.

## Product choice and time

See the docstring of `acs.py`. The product is the **ACS 5-year estimates**, tables B01001
(sex by age), B01001A–G (by race) and B01001I (Hispanic or Latino), with their 90% margins of
error. A 5-year estimate describes its whole reference period: "2020–2024" is not a count for
2024. The four windows used (2005–2009, 2010–2014, 2015–2019, 2020–2024) do not overlap, so two
trials in different windows are compared with independent samples; yearly 5-year releases share
four of their five years and are never treated as separate observations. ACS 1-year estimates
(areas of 65,000 or more only; no standard 2020 release) and the decennial census are not used.
ZCTA tables do not exist for 2005–2009; that level is recorded as unavailable there.

A trial is matched to a window by the midpoint of start date to primary completion date, the
closest proxy the registry has for enrolment; the dates, the basis, the window and the reason
are kept per trial. A pooled area mixes trials from different windows, each compared with its
own window, so an area's figure is not a trend: a change in an area's population and a change
in which trials ran there are not separated. Each window is resolved in its own boundary
files, since identifiers change (Connecticut's counties became planning regions in the 2022
files; ZCTAs were redrawn in 2020).

Race comparability across windows is affected by a measurement change. The Census Bureau's
2023 ACS Subject Definitions state that "the ACS question on race was revised in 2020 to make
it consistent with the 2020 Census race question" and that ACS race data "are not directly
comparable across all years". The 2020–2024 window lies wholly after that revision.
`summary.json` reports the national share of each table by window; how much of a change is
measurement and how much is population is not estimated here.

Margins of error are carried through by the Census Bureau's approximations for sums and
proportions. An area's figure reports the mean margin of its trials rather than a combined
one: trials in one area share the same survey estimates, so their margins are not independent
and do not average down. The bootstrap interval is over trials and excludes survey error.

## What is validated, and what is not

| Check | Kind | What it shows |
|---|---|---|
| `tests/census` | Engineering | The rules do what they say on hand-computed inputs. |
| Two builds from the same inputs give identical analytical files | Engineering | The build is deterministic; `run.json` differs only in its timestamp, which is kept. |
| `scripts/census/check_against_api.py` | Engineering | A second code path, with variables named by number, reproduces reference shares from the API. Same source and same definition, so it cannot test whether the comparison is appropriate. |
| Whether a site-area benchmark is the right comparison for a trial | Scientific | **Not validated.** No data on participants' residence or catchment is held. |
| Whether one listed facility means a single-site study | Scientific | **Not validated** beyond the study's own text. |
| Category harmonisation with the survey | Scientific | Reasoned, documented, and unreviewed by a second person. |

## Artifacts (schema version 1)

`run.json` (provenance: release, registry archive, every downloaded file with redacted URL and
sha256, parameters, limitations), `summary.json` (counts with definitions, every absence
reason), `areas.json` (pooled figures), `trials.jsonl.gz` (one line per trial: tier, site
resolution with methods, period, observed counts with original categories, and the reference
population at every level).

## Running it

```bash
pip install -r scripts/census/requirements.txt
python scripts/build_census_benchmarks.py --release demographics.json.gz --release-tag data-2026-11-08
python scripts/census/check_against_api.py build/census/trials.jsonl.gz
python scripts/census/export_site.py build/census ../clinical-trial-populations/data/census   # the files the site reads
python -m pytest tests/census -q
```

`CENSUS_API_KEY` must be in the environment. It is read only by `acs.py`, removed from every
recorded URL, and never written to an artifact. Downloads are cached under `build/census_cache/`
(gitignored); nothing is written to `--out` unless the whole build succeeds.

## Not done here

No workflow and nothing scheduled. The site's view and its data arrive by a site pull request built from `scripts/census/export_site.py`. The decennial census, ACS margins of error in the
bootstrap interval, facility-level geocoding, and an age comparison are not implemented.
