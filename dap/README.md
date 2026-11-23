# Multi-Site Diversity Action Plan (FDORA) & Geographic Site-Mix Benchmarks (manual)

Offline analytical pipeline benchmarking demographic enrollment distributions,
reporting completeness, and geographic facility footprints across multi-site
and single-site clinical trials for FDA Diversity Action Plan (FDORA Section
3601, 21 U.S.C. 355(z)) context.

Runs strictly by hand (`scheduled: false`); no scheduled GitHub Actions
workflow invokes it, and no weekly production artifact or curated CSV is
modified.

## What this module measures — and what it does not

1. **Facility share is listed facilities, never US participant enrollment.**
   Every geography record, stratum, summary, and company scorecard carries
   `unit_basis = "listed_facilities_not_participants"`. ClinicalTrials.gov
   baseline tables pool participants across all countries; `us_facility_share`
   is strictly `n_us_facilities / n_facilities_with_country` from
   `protocolSection.contactsLocationsModule.locations` (or weekly
   `study_sites`).
2. **Site footprint classification (`site_footprint_class`)** partitions trials
   into six mutually exclusive classes:
   - `us_only` (>= 1 listed facility, all in the 50 US states, DC, or inhabited
     US territories under FDA jurisdiction, and zero missing-country sites)
   - `multinational_with_us_sites` (>= 1 US facility and >= 1 non-US facility),
     further stratified into `us_site_share_bin` in
     `{"us_share_lt_25", "us_share_25_to_lt_50", "us_share_50_to_lt_75", "us_share_gte_75"}`
   - `ex_us_only` (>= 1 listed facility, zero US facilities, zero missing-country sites)
   - `facilities_missing_country` (listed facilities where missing country
     prevents establishing footprint without guessing)
   - `no_listed_facilities` (zero listed facilities)
   - `footprint_unknown` (the site list cannot establish any class). **Every
     weekly full record (`--demographics`) gets it.** `src/utils.py`
     `get_study_metadata` drops each location without a country before writing
     `study_sites`, and the record keeps no total location count, so a trial
     with one US site and one countryless site would read as `us_only`, and
     one whose sites all lack a country as `no_listed_facilities`. The row
     carries `footprint_unknown_reason`; `n_listed_facilities`,
     `n_facilities_missing_country` and `partial_missing_country` are null,
     while counts over sites with a country (`n_us_facilities`,
     `n_facilities_with_country`, `us_facility_share`) stay exact. A weekly
     build therefore has no US-site trials, US-share bins or indication US
     references; site-mix benchmarks need raw CT.gov API v2 records
     (`--records`, `--records-dir`), which keep every location.
3. **Six-state baseline table structure & two denominators** (following
   `census/`):
   - `race_table_status` and `ethnicity_table_status` in
     `{"omb_compatible", "combined_race_ethnicity_quarantined", "single_category_ethnicity", "non_standard_categories_present", "table_absent_in_posted_results", "results_not_posted"}`.
   - Race measures containing Hispanic/Latino or combined race+ethnicity rows
     are classified `combined_race_ethnicity_quarantined`, excluded from all
     pooled OMB race distributions, and counted explicitly.
   - **Denominator A (`of_baseline_total`)**: `floor = count / baseline_total`,
     `unknown_bounds = [floor, (count + unknown + unmapped) / baseline_total]`,
     and `direction_settled` (`True` when both bounds lie strictly on the same
     side of the matched stratum reference median `p50`; `None` when no
     reference median is available).
   - **Denominator B (`of_classified`)**: `count / classified_total`; withheld
     as `None` whenever `single_category_ethnicity` holds or
     `classified_total == 0`.
4. **Sex/gender five-state fidelity**: reads `src/sex_gender_table.py` rows
   (or parses raw studies through `sex_gender_table.build_row` without
   touching `src/sex_gender_parser.py`), preserving all five states
   (`reported`, `explicit_unknown_only`, `uninformative`, `not_reported`,
   `parse_error`) and never folding `enrollment_minus_parsed` into
   `n_unknown`.
5. **Curated read-only sponsor attribution**: joins `sponsors/` (`current_owner`
   and `as_registered`; `any`, `lead`, and `collaborator`) with zero substring
   matching and strict separation of `attributed`, `reviewed_excluded`, and
   `unreviewed` literals.

## Artifacts (schema version 1)

| File | Contents |
|---|---|
| `run.json` | Deterministic `run_id` (`dap-YYYY-MM-DD-<run_identity>`), input SHA-256s, producing commit, source SHA-256 and uncommitted-changes flag over every repository module the build imports (`dap/`, `src/`, `sponsors/`, `scripts/`), parameters, counting units, vocabularies, limitations, and byte-level `files` SHA-256s |
| `summary.json` | Named `denominators`, headline `metrics`, and partition `breakdowns`; every count carries `value`, `numerator`, `denominator`, and `definition` |
| `strata.json` | Indication (ontology + optional MeSH descriptor) x phase x regulatory era x footprint / US-share-bin cells with trial-weighted `p10..p90` (primary) and participant-weighted share (secondary), withholding cells `< min_cell_trials` (default 5). MeSH cells are one per descriptor ID; an ID that records carry under several terms shows the term most trials carry (ties: lexicographically first) and is listed in `mesh_descriptor_term_conflicts` |
| `sponsors.json` | Canonical (`current_owner`) and `as_registered` company peer scorecards and 3-state attribution summary |
| `trials.jsonl.gz` | Deterministic `nct_id`-sorted per-trial records (kept with the engine build; omitted by `export_dap_benchmarks_site.py`) |

`export_dap_benchmarks_site.py` writes the site's `run.json`, `summary.json`,
`strata.json` and `sponsors.json`. The site `run.json` keeps the build's
`producer` block and adds an `exporter` block (commit, uncommitted changes and
source SHA-256 over the exporter's own import closure). The site `run_id` is
`<build run_id>-<8 hex>`, the hex being a digest of the exported content minus
`run_id`: a changed exporter or commit gives a new id for the same build. A
build made with `--limit` (diagnostic only; `inputs.study_records.limit_applied`
is not null) passes the contract but is never exported. Nor is a build, or an
exporter run, without readable git provenance: when git is missing or a git
command fails, `commit` and/or `uncommitted_changes` are recorded as null (never
false) and the build is still written for local diagnostics, but the exporter
refuses it. A dirty tree (`uncommitted_changes: true`) is published as recorded.

## Study record inputs

`--records` and `--records-dir` take raw CT.gov API v2 study documents: one
study, a `studies`/`records` list, a top-level list, a JSONL file of studies,
or a JSONL log of cached API responses (one `{"status", "body", "body_sha256",
"ids" or "url"}` line per request). Nothing is skipped: a non-object entry
refuses the input, and each response line must be a whole, successful answer
(status 200, a body matching `body_sha256`, at least one study, returned NCT
ids equal to the requested ones, and no trial returned twice across lines),
or the build is refused, naming the line.

**Records must be complete.** A response requested with a `fields=` projection
is refused, and so is any raw study missing a protocol module CT.gov requires
on every registration: `identificationModule`, `statusModule`,
`sponsorCollaboratorsModule`, `conditionsModule` or `designModule`. A projected
record would otherwise build with every trial read as ontology Other, no
listed facilities, no MeSH and an unreviewed "Unknown" sponsor, and the
contract would pass. Inside those modules a record must also carry the fields
every registration holds (`briefTitle`, `overallStatus`, `statusVerifiedDate`,
`studyType`), which a field-by-field projection keeping the module objects drops;
its conditions (`conditionsModule.conditions`, a non-empty list of non-blank
strings) and its lead sponsor (`sponsorCollaboratorsModule.leadSponsor`, an
object with a non-blank `name` and `class`), which would otherwise read as
ontology Other and an "Unknown" sponsor; and a boolean `hasResults`, with a
non-empty `resultsSection` whenever it is true (otherwise the baseline table
would read as never posted). The lead sponsor class is required because every
stored unprojected record carries one. In the 2026-06-19 AACT sponsors fixture
all 976 lead rows without a class are named "[Redacted]" (the fixture holds no
study status, so that they are withheld trials is inferred, not checked), and
the one such record cached from the API (NCT04060524, `WITHHELD`) has no study
type either.
Parts the build or the shared race, ethnicity and sex extractors read without a type check must
have their API v2 type when present (the start and primary completion date structs,
`enrollmentInfo`, and the baseline table's groups, denominators, measures, classes, categories and
measurements; see `TYPED_PROTOCOL_PARTS` and `BASELINE_SHAPE`), so a malformed record is refused by
name instead of stopping the build with a traceback. The locations module and list, and every
location and collaborator entry, are checked the same way (`LIST_ENTRY_FIELDS`): a skipped `null`
location, or a module read as empty, would lower or zero the listed facilities and move the
footprint class with nothing reported.
Weekly full records never pass these checks, so their own lists are checked
too (`WEEKLY_LIST_FIELDS`): `study_sites`, `collaborators` and `conditions`
must be present, as `src/utils.py` always writes them; `study_sites` and
`collaborators` must be lists of objects, with `country`, `state`, `name` and
`class` strings when present; and `conditions` must be a non-empty list of
non-blank strings, as on the raw path, an explicit `null` refused throughout. A
`conditions` string was read one character at a time, a dropped or missing
condition list moved the indication to Other, and a dropped or missing site
list lowered the exact site counts. All 80,890 records of the 2026-11-08
weekly extraction pass.
`contactsLocationsModule` and `derivedSection` are not required: their absence
is a real "no listed facilities" / `no_derived`, so a whole-module projection
that omits them cannot be told apart record by record.
**The audit caches under `docs/audits/outcomes_results_structure/` are not
valid DAP inputs**: every request in them used `fields=` (first pass:
`NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule`).

## Condition MeSH sidecar (`--condition-mesh`)

The optional `condition_mesh.jsonl.gz` (`src/condition_mesh.py`) replaces each
trial's own `derivedSection` MeSH descriptors, so the build refuses one that
does not belong to its study records (`benchmarks.check_condition_mesh_provenance`):

- **Every line:** it must keep the `src/condition_mesh.py` contract
  (`benchmarks.condition_mesh_line_problem`): its `schema_version`, `meshes`
  and `ancestors` null or lists of `{id, term}`, and a state that agrees with
  them (`indexed` has descriptors, `not_indexed` none, `no_module` and
  `no_derived` no lists, `malformed` a `problem`). The loader refuses the
  first line that does not.
- **Every input:** the sidecar must hold a line for every input trial (lines
  for other trials, e.g. under `--limit`, are allowed and counted), and all its
  lines must carry one `(snapshot_date, extracted_at, pipeline_commit)` triple,
  each stamp a string or null.
- **Weekly full records (`--demographics`, or `--records` given a full-record
  container):** `src/extract_all.py` stamps the `demographics.json` container
  and every sidecar line with the same `extracted_at` and `pipeline_commit`,
  so every line must equal the records' pair. Another week's or another
  commit's sidecar is refused, as are records without both stamps.
- **Raw API v2 inputs (`--records-dir`, or `--records` given study documents
  or a JSONL archive):** they carry no extraction stamps, so only coverage and
  the one-run check apply.

`run.json` `inputs.condition_mesh` records the sidecar's three stamps (null
when its lines carry none), `provenance_check`, `provenance_reason`,
`n_cohort_trials` and `n_lines_outside_cohort`. `export_dap_benchmarks_site.py`
copies them into the site's `run.json`, and refuses a build whose
`inputs.condition_mesh` lacks them (built before these checks existed).

## Commands

```bash
python3 scripts/build_dap_benchmarks.py --demographics data/demographics.json.gz \
  --condition-mesh data/condition_mesh.jsonl.gz --out build/dap
python3 scripts/check_dap_benchmarks_contract.py build/dap
python3 scripts/export_dap_benchmarks_site.py build/dap ../clinical-trial-populations/data/dap
python3 -m pytest tests/dap -q
```

`--snapshot-date YYYY-MM-DD` names the registry snapshot the sex/gender
measures were parsed from. It must be a real calendar day, is recorded as
`run.json` `parameters.snapshot_date` (so it enters the run identity and the
site manifest), and the contract re-checks it. Where it comes from depends on
the input (`artifacts.resolve_snapshot_date`):

- **Raw CT.gov v2 studies** are parsed in this build, so the value is stamped
  on their rows: `--snapshot-date`, or the `--generated-at` date without it.
- **Weekly full records** arrive with their sex/gender rows already parsed, and
  those rows are reused, so the build date would name nothing about them. The
  date is the pull's own: a `snapshot_date` on the rows, the condition_mesh
  sidecar's `snapshot_date` (written from the same `--snapshot-date` by
  `src/extract_all.py`, and already tied to the records by `extracted_at` and
  `pipeline_commit`), or `--snapshot-date`. Every value given must agree. With
  none, the build is refused: the published rows drop the stamp
  (`sgt.lean_row`), so a weekly build without the sidecar needs
  `--snapshot-date` with the pull's date. `extracted_at` is not used, because
  the weekly job sets it after taking the snapshot date and the two can name
  different days. The contract refuses a weekly manifest whose
  `parameters.snapshot_date` differs from `inputs.condition_mesh.snapshot_date`.
