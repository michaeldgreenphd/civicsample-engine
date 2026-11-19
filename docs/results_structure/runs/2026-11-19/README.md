# Results-structure run for the 2020 industry Phase 3 cohort, retrieved 2026-11-19

The record of one manual build, kept so its counts can be checked without rerunning it. It is the
first build with the reported primary results (layer B) copied into `records.json`
(civicsample-engine#76), and it replaces the 2026-11-14 run on the site.

| | |
|---|---|
| Run | `results-structure-2026-11-19-9a3ff2e956`, generated 2026-11-19T13:51:06+00:00 by `scripts/build_results_structure.py` at engine commit `6d4fd08af366` (no uncommitted changes; producing-code digest `cb2131b05515…`) |
| Method | `results-structure-1`: usable-content rules B1, F1, O1, A1; comparative classes C1, C2; analysis rule S1; subgroup rule SG2. Unchanged since 2026-11-14 |
| Scope | Frame 648 registry records fetched; **598 trials in this build** (the eligible cohort of the Industry Outcomes beta); 50 frame records left out: excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded). The id lists are the 2026-11-14 run's, copied here unchanged: `frame_ids.txt` (sha256 `637bb40370bedcb8…`), `eligible_ids.txt` (`dcfc1e3fc8829b23…`) and `excluded_ids.txt` (`e7f7925eacf48b28…`) |
| Registry records | 648 of 648 frame records retrieved from the ClinicalTrials.gov API v2, 2026-11-19T13:38:09+00:00 to 2026-11-19T13:38:26+00:00; API version 2.0.5, registry `dataTimestamp` 2026-11-19T10:00:07 |
| Requests | 27 (26 for study records in batches of 25 ids, 1 for the API version), one at a time and at least 0.5 s apart; 0 retries, 0 failed |
| Archive | `records-2026-11-19-d5e1d8feda6d-c9c328d6.jsonl.gz`, sha256 of the uncompressed bytes `d5e1d8feda6d0ea999a64654313ae6be27373a29fa6d8a25428afb80b5e12fcf` |
| Human reviews | `outcomes/subgroup_reviews.csv`, 0 rows (sha256 `173e1437b0922e3e…`) |

## What changed since 2026-11-14

- **Counts: none.** `summary.json` is identical to the 2026-11-14 run's apart from the run id, so
  every count and table in `docs/results_structure/runs/2026-11-14/README.md` holds for this run.
- **Records: only the new fields.** With the layer B fields and the run id removed, every one of
  the 598 records equals the published 2026-11-14 `records.json`. The registry records of this
  cohort gave the same results-structure facts five days later.
- **New fields** (`run.json` lists them under `fields_copied`): for each posted primary measure,
  its `source_path`, `population`, `groups` and `denominators`; for each of its analyses, its
  `source_path`, `estimate_value`, `ci_lower_limit` and `ci_upper_limit`. The estimate type,
  interval percent and sides, p-value, method and non-inferiority type were already copied.

## Reported primary results (layer B)

Values copied verbatim from the registry, with the JSON path of the measure or analysis. Nothing is
cast, rounded, relabelled or judged; an absent value is null.

| | Count |
|---|---:|
| Trials with posted primary measures | 416 |
| Primary measures | 723 |
| … with an analysis population stated | 667 |
| … with no statistical analysis posted | 291 |
| … with a denominator row in units other than participants | 1 |
| Analyses on primary measures | 814 |
| … with an estimate value | 727 |
| … with both confidence limits | 720 |
| … with a p-value | 635 |
| … naming a single group | 36 |

## Files here

- `run.json`: the manifest, now with `fields_copied` beside the fields the subgroup rule inspected
  and those it did not, and the sha256 of `summary.json` and `records.json`.
- `summary.json`: the scope and every count with its numerator, denominator and definition.
- `frame_ids.txt`, `eligible_ids.txt`, `excluded_ids.txt`: the id lists, as in the 2026-11-14 run.
- `archive.meta.json`: the retrieval record written beside the archive, with one entry per request.

## Files not here

`records.json` (sha256 `ced74e5f142e766b…`, 4.6 MB) is written to the site
repository at `data/results_structure/`. The archive of registry records is under the gitignored
`build/results_structure_cache/`; its sha256 is in `run.json`.

## Limitations

The 2026-11-14 run's limitations hold. In addition, for the reported values:

- They are what the registry posts, not a verified result: no value is checked against the
  protocol, the statistical analysis plan or a publication.
- An estimate or a p-value is not a conclusion about whether a trial met its primary objective;
  that needs the prespecified rule and a person's review.
- An analysis may name a single group; the comparison is then described in free text
  (`groupDescription`) that is not copied.
- The values each group reports (means, rates and their dispersion) are not copied; for the
  291 primary measures with no analysis posted, those are the only results the registry holds.
- The population is stated once per measure and so applies to all of its analyses.
