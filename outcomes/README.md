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
