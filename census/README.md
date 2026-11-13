# Census benchmarks for single-site US trials (manual)

For each trial whose registration lists one US facility, compares the race, ethnicity and sex of
its participants with a **selected reference population**: residents of the area around that
facility who meet the trial's age and sex eligibility, from the American Community Survey. It
runs by hand; no workflow calls it, and the weekly job, its cohort and its files are unchanged.

## What the comparison is, and is not

The reference population is a proxy for a potential recruitment population. It is **not** where
participants live, the study's catchment area, the population with the condition, or an
enrolment target. A referral centre may recruit nationally. Every output and any text built on
it says "compared with residents of the selected area", never "underrepresented" on its own.

## What it computes

| Step | Module | Notes |
|---|---|---|
| Cohort | `trials.cohort` | Records whose registration lists exactly one facility, in the US. A trial with several facilities is never single-site, even when only one has a usable address; that case is counted. |
| Site-count tier | `trials.site_tier`, `registry.py` | The registry has no single-site field. The study's own text decides the tier: `self_described_single_site`, `one_listed_facility` (no statement), `conflicting_text`, `text_indicates_multiple_sites` (left out of every pooled figure), `listing_changed_since_release`. Self-description is not third-party verification, and only today's registration is read. |
| Enrolment period | `trials.enrolment_period` | Start date to primary completion date, the closest proxy the registry has; the basis is recorded per trial. |
| Benchmark window | `trials.assign_window` | One of four **non-overlapping** ACS 5-year windows (2005-2009 … 2020-2024) by the period's midpoint, with the reason and any gap in years. |
| Site geography | `geography.Resolver` | Per window, in that window's own boundary files. ZIP → ZCTA only when the code is published as a ZCTA; county and metro area from the ZCTA polygon's largest area share, the share kept as confidence; the registry's city point only as a labelled fallback. Unresolved units carry a reason. |
| Reference population | `acs.py`, `benchmark.expected` | Sex-by-age tables by race (B01001A–G), Hispanic origin (B01001I) and all residents (B01001), restricted to the eligible ages and sexes, at ZCTA, county, metro area, state, region and nation side by side. 90% margins of error are carried through. |
| Comparison | `benchmark.compare` | Percentage-point difference and representation ratio per category; the ratio is recorded as undefined when the reference share is zero. |
| Areas | `benchmark.pool` | Each trial counts once (the investigator's choice, 2026-11-10); a participant-weighted figure is secondary. Bootstrap interval over trials; areas under 50 qualifying trials are withheld with their count. Variants: site basis (one listed facility / self-described only) × with or without referral-centre ZIPs. |

## Harmonisation

Race and Hispanic ethnicity are separate questions on both sides and stay separate. Trial
categories map one to one onto ACS tables; the registry's `other` is paired with Some Other
Race, the loosest pairing. `unknown_not_reported` has no counterpart: it stays in the trial's
denominator as its own row and is never spread across the others. The original category labels
are kept beside the harmonised counts. Sex is the registry's sex field against the survey's;
nothing here describes gender identity or sexual orientation. Age is matched on the reference
side only: the registry does not report participants' ages by race, and the engine does not
parse baseline age distributions.

The trial side is the registry's baseline characteristics table: the baseline analysis
population with all arms combined. It can differ from enrolment; the ratio is recorded.

## Product choice and time

See the docstring of `acs.py`. In short: 5-year estimates are the only ACS product covering
every county and ZCTA; a 5-year estimate describes its whole window, not its last year; the
windows used do not overlap, so they are independent samples; 1-year estimates (areas of 65,000
or more only, and no standard 2020 release) and the decennial census are not used. ZCTA tables
do not exist for 2005-2009; that level is recorded as unavailable there.

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
python -m pytest tests/census -q
```

`CENSUS_API_KEY` must be in the environment. It is read only by `acs.py`, removed from every
recorded URL, and never written to an artifact. Downloads are cached under `build/census_cache/`
(gitignored); nothing is written to `--out` unless the whole build succeeds.

## Not done here

No site view, no published file, no workflow. The decennial census, ACS margins of error in the
bootstrap interval, facility-level geocoding, and an age comparison are not implemented.
