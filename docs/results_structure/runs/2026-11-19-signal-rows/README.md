# Results-structure run for the 2020 industry Phase 3 cohort: the 2026-11-19 records, with signal rows

The record of one manual build, kept so its counts can be checked without rerunning it. It is built
from the same archive and id lists as the 2026-11-19 group-values run (`../2026-11-19-group-values/`),
at the engine commit that adds signal rows. It replaces that run on the site.

| | |
|---|---|
| Run | `results-structure-2026-11-19-d6bd5eadb6`, generated 2026-11-22T00:01:59+00:00 by `scripts/build_results_structure.py` at engine commit `27e3da92c375` (no uncommitted changes; producing-code digest `d8c73e623942…`) |
| Method | `results-structure-1`: usable-content rules B1, F1, O1, A1; comparative classes C1, C2; analysis rule S1; subgroup rule SG2. The rules are unchanged since 2026-11-14; this build adds a field, not a rule |
| Scope | Frame 648 registry records; **598 trials in this build** (the eligible cohort of the Industry Outcomes beta); 50 frame records left out: excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded). The id lists are the 2026-11-14 run's, copied here unchanged: `frame_ids.txt` (sha256 `637bb40370bedcb8…`), `eligible_ids.txt` (`dcfc1e3fc8829b23…`) and `excluded_ids.txt` (`e7f7925eacf48b28…`) |
| Registry records | No new fetch. The 2026-11-19 run's archive: 648 of 648 frame records retrieved from the ClinicalTrials.gov API v2, 2026-11-19T13:38:09+00:00 to 2026-11-19T13:38:26+00:00; API version 2.0.5, registry `dataTimestamp` 2026-11-19T10:00:07 |
| Requests | None for this build. The archive's 27 requests are in `archive.meta.json`, copied unchanged from `../2026-11-19/` |
| Archive | `records-2026-11-19-d5e1d8feda6d-c9c328d6.jsonl.gz`, sha256 of the uncompressed bytes `d5e1d8feda6d0ea999a64654313ae6be27373a29fa6d8a25428afb80b5e12fcf` |
| Human reviews | `outcomes/subgroup_reviews.csv`, 0 rows (sha256 `173e1437b0922e3e…`) |

## What changed since the 2026-11-19 group-values run

- **Counts: none.** `summary.json` is identical to that run's apart from the run id, so every count
  and table in `../2026-11-14/README.md`, `../2026-11-19/README.md` and
  `../2026-11-19-group-values/README.md` holds for this run. No signal is added or removed, no
  `signal_id` moves, and no subgroup state changes.
- **Records: only the new field.** With `rows` and the run id removed, every one of the 598
  records equals that run's published `records.json`.
- **New field.** Every subgroup signal now has `rows`:
  - a signal of the level rule on class, category or group titles lists the posted rows (classes or
    categories) or group columns whose titles carry a level: each one's position and title as
    posted, every one, in posted order;
  - every other signal (a cue, a group-description match, an analysis of level-defined groups)
    points at wording, not at a row, and has `rows: null`.

A listed row is what rule SG2 matched. It is not a finding that the row is a subgroup result:
only a human review record can settle a signal, and there are none.

## Signal rows

| | Count |
|---|---:|
| Subgroup signals | 440 |
| … with rows (title-level) | 18 |
| … … on class titles | 14 |
| … … on group titles | 4 |
| … … on a primary measure | 3 |
| … … on a secondary measure | 15 |
| … with `rows: null` | 422 |
| Rows listed | 127 |
| Title-level signals whose rows include titles `matched_text` leaves out (its display cut) | 13 |
| Trials with a title-level signal | 7 |

By dimension, the 18 title-level signals are age 16 and sex 2. The three on primary measures, whose
tables the site already shows under "Primary results as posted":

| Trial | Measure | Rule read | Dimension | Rows |
|---|---:|---|---|---:|
| NCT02687451 | `outcomeMeasures[0]` | group titles | age | 4 |
| NCT04024228 | `outcomeMeasures[0]` | class titles | age | 8 |
| NCT04127786 | `outcomeMeasures[34]` | class titles | age | 2 |

The seven trials are NCT02527200, NCT02687451, NCT03112603, NCT03615924, NCT03788967,
NCT04024228 and NCT04127786. `records.json` grows by 12,658 bytes (0.24%), to 5,211,469 bytes
(488,517 gzipped).

## Checks added with this build

The producer check, run before anything is written, now also refuses:

- a `state`, `met`, `significant` or `favours` key inside `primary_measures` or a signal's `rows`;
- a title-level signal without rows, rows on any other signal, or a row without exactly its field's
  keys;
- a row on a primary measure that does not resolve to the copied table at its position and title.

This build passes all of them.

## Files here

- `run.json`: the manifest, with the sha256 of `summary.json` and `records.json`.
- `summary.json`: the scope and every count with its numerator, denominator and definition.
- `frame_ids.txt`, `eligible_ids.txt`, `excluded_ids.txt`: the id lists, as in the 2026-11-14 run.
- `archive.meta.json`: the retrieval record written beside the archive, copied from
  `../2026-11-19/` because this build reads the same archive.

## Files not here

`records.json` (sha256 `f3fa4f28027528e7…`, 5.2 MB) is written to the site repository at
`data/results_structure/`. The archive of registry records is under the gitignored
`build/results_structure_cache/`; its sha256 is in `run.json`.

## Limitations

The earlier runs' limitations hold. In addition:

- Rows come from title-level signals only. A subgroup posted as a separate outcome measure, named
  only in a measure's description, or tested in an analysis has no rows; a keyword signal points at
  wording.
- The rule reads titles, so a row is listed because its title carries a level, not because its
  values were checked. Of the 152 primary measures with more than one class (85 trials), the rule
  reads levels in the class titles of 2. A sample of the others read for this increment showed
  components such as serotypes, symptoms or severity grades; they are not listed.
- Rows of secondary measures are listed, but their tables are not copied (only primary measures
  are), so a reader sees the row titles without the values.
