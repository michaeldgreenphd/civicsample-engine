# Results-structure run for the 2020 industry Phase 3 cohort: the 2026-11-19 records, with group values

The record of one manual build, kept so its counts can be checked without rerunning it. It is built
from the same archive as the 2026-11-19 run (`../2026-11-19/`), at engine main after
civicsample-engine#80. It is the first build with the value each group posted on each primary
measure copied into `records.json`, and it replaces the 2026-11-19 run on the site.

| | |
|---|---|
| Run | `results-structure-2026-11-19-95e957547e`, generated 2026-11-19T15:34:17+00:00 by `scripts/build_results_structure.py` at engine commit `61928d034e1e` (no uncommitted changes; producing-code digest `a2829d21dfaf…`) |
| Method | `results-structure-1`: usable-content rules B1, F1, O1, A1; comparative classes C1, C2; analysis rule S1; subgroup rule SG2. Unchanged since 2026-11-14 |
| Scope | Frame 648 registry records; **598 trials in this build** (the eligible cohort of the Industry Outcomes beta); 50 frame records left out: excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded). The id lists are the 2026-11-14 run's, copied here unchanged: `frame_ids.txt` (sha256 `637bb40370bedcb8…`), `eligible_ids.txt` (`dcfc1e3fc8829b23…`) and `excluded_ids.txt` (`e7f7925eacf48b28…`) |
| Registry records | No new fetch. The 2026-11-19 run's archive: 648 of 648 frame records retrieved from the ClinicalTrials.gov API v2, 2026-11-19T13:38:09+00:00 to 2026-11-19T13:38:26+00:00; API version 2.0.5, registry `dataTimestamp` 2026-11-19T10:00:07 |
| Requests | None for this build. The archive's 27 requests are in `archive.meta.json`, copied unchanged from `../2026-11-19/` |
| Archive | `records-2026-11-19-d5e1d8feda6d-c9c328d6.jsonl.gz`, sha256 of the uncompressed bytes `d5e1d8feda6d0ea999a64654313ae6be27373a29fa6d8a25428afb80b5e12fcf` |
| Human reviews | `outcomes/subgroup_reviews.csv`, 0 rows (sha256 `173e1437b0922e3e…`) |

## What changed since the 2026-11-19 run

- **Counts: none.** `summary.json` is identical to the 2026-11-19 run's apart from the run id, so
  every count and table in `../2026-11-14/README.md` and `../2026-11-19/README.md` holds for
  this run.
- **Records: only the new fields.** With the new fields and the run id removed, every one of the
  598 records equals the 2026-11-19 run's published `records.json`: same archive, same rules.
- **New fields** (`run.json` lists them under `fields_copied`, 20 to 33 paths): for each posted
  primary measure, its `dispersion_type`, its `reporting_status`, and `classes`, the registry's
  own nesting. Each class has its `title` (a time point, a subgroup or a row; null when there is
  one untitled class), its own `denominators` where posted, and its `categories`. Each category
  has a `title` and one `measurements` entry per group: `group_id`, `value`, `spread`,
  `lower_limit`, `upper_limit` and `comment`.

## The value each group posted

Values copied verbatim from the registry. Nothing is cast, rounded, relabelled or judged; an
absent value is null, and `NA` stays `NA`.

| | Count |
|---|---:|
| Trials with posted primary measures | 416 |
| Primary measures | 723 |
| … with at least one group value | 719 |
| … with no statistical analysis posted | 291 |
| … with no analysis posted but with group values | 287 |
| … with a dispersion type | 484 |
| … with titled classes (time points, subgroups, rows) | 152 |
| … with class-level denominators | 62 |
| Group values | 3,350 |
| … not a plain decimal number (every one `NA`) | 20 |
| … with a comment | 38 |
| … whose spread and lower limit are the same posted number | 48 |
| … with a spread or limits but no dispersion type on the measure | 2 |
| Category-and-group cells with no value posted | 52 |

All 723 measures have `reporting_status` `POSTED`. "Not a plain decimal number": the value does
not match an optional minus sign, digits, and an optional decimal part.

## Files here

- `run.json`: the manifest, with `fields_copied` beside the fields the subgroup rule inspected and
  those it did not, and the sha256 of `summary.json` and `records.json`.
- `summary.json`: the scope and every count with its numerator, denominator and definition.
- `frame_ids.txt`, `eligible_ids.txt`, `excluded_ids.txt`: the id lists, as in the 2026-11-14 run.
- `archive.meta.json`: the retrieval record written beside the archive, copied from
  `../2026-11-19/` because this build reads the same archive.

## Files not here

`records.json` (sha256 `5080e8b342c03977…`, 5.2 MB) is written to the site
repository at `data/results_structure/`. The archive of registry records is under the gitignored
`build/results_structure_cache/`; its sha256 is in `run.json`.

## Limitations

The 2026-11-14 and 2026-11-19 runs' limitations hold, except that the values each group posted are
now copied. In addition:

- The group values are what the registry posts, not a verified result. No value is checked against
  the protocol, the statistical analysis plan or a publication, and no two groups are compared.
- For 48 values the registry's API returns one number in two fields, as the spread and as the
  lower limit. Both are copied as returned; this build does not decide which is meant.
- A value's `comment` is free text from the registry (for example, why a value is `NA`). It is
  copied, not read.
- The dispersion type is stated once per measure, so it applies to every value on it.
