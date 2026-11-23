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
     reference median is available). `baseline_total` is never registered
     enrollment: a raw record keeps `enrollmentInfo` only when a
     `Participants` baseline denominator's last count parses, so a denominator
     of `"NA"` no longer balances the tables to enrollment.
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
footprint class with nothing reported. Every collaborator must also carry a non-blank `name`: one
without was read as `""` and skipped by `attribute_sponsors`, so the trial left that collaborator's
scorecards with nothing reported. Every measurement and denominator count must carry its
`groupId` and `value` as strings, CT.gov's form (`"60"`, `"NA"`): `src/utils.sum_measurements`
counts a value it cannot parse as 0, so a missing or non-string one (`{}`, `["60"]`, `true`, `null`)
moved a posted row's participants into the balanced unknown remainder. The same holds for a
measure's or class's own Number Analyzed (`denoms`), which `table_selection.number_analyzed` reads
without a type check (a `classes[0].denoms: "corrupt"` stopped the build with `AttributeError`).
A value must also not read as an infinite or NaN number (`"Infinity"`, `"1e400"`, `"nan"`), which
`src/utils.py` parsed and then failed on with `OverflowError`; `"NA"` reads as no number and is kept.
Weekly full records never pass these checks, so their own lists are checked
too (`WEEKLY_LIST_FIELDS`): `study_sites`, `collaborators` and `conditions`
must be present, as `src/utils.py` always writes them; `study_sites` and
`collaborators` must be lists of objects, with `country`, `state`, `name` and
`class` strings when present; and `conditions` must be a non-empty list of
non-blank strings, as on the raw path, an explicit `null` refused throughout. A
`conditions` string was read one character at a time, a dropped or missing
condition list moved the indication to Other, and a dropped or missing site
list lowered the exact site counts. Every site must carry a non-blank
`country`, since `src/utils.py` keeps only locations with one, and every
collaborator a non-blank `name`, as on the raw path
(`WEEKLY_REQUIRED_ENTRY_FIELDS`). The other weekly fields this module reads
must be present with the producer's type (`WEEKLY_SCALAR_FIELDS`): `nct_id`,
`study_type`, `status`, `lead_sponsor_name` and `sponsor_class` non-blank
strings, `brief_title`, the dates and `enrollment_type` strings, `enrollment`
an integer, `phase` a string (its values checked as above), and `race`,
`ethnicity`, `sex`, `gender` and `sex_gender` objects. `sex_gender`, the row
reused as it stands, must carry a `sex_report_status` in `SEX_GENDER_STATES`
and every lean column `sgt.lean_row` writes with its type
(`WEEKLY_SEX_GENDER_COLUMNS`): a partial row published a reported trial with
every flag and count empty, and a string count stopped the build. The
types follow the producer by status: `is_participant_count` is `None`
exactly when no measure was read (`not_reported`, `parse_error`) and a
boolean otherwise, since a measured row with `None` was left out of female
composition; the `reported_*` flags may be `None` only in a `parse_error` row,
which the extractor writes when the parser raises and which is kept. The three
flags must also be the ones `sgp.derive_outcomes` gives for the row's status
and counts (`WEEKLY_OUTCOME_COLUMNS`): `None` in a `parse_error` row, `False`
for every status but `reported`, and `reported_both` the conjunction of the
other two. A `not_reported` row with `reported_sex: true` was counted as both.
The `race` and `ethnicity` blocks must carry the children `dap/demographics.py`
reads, of the kind the extractors write (`WEEKLY_DEMOGRAPHIC_BLOCK_FIELDS`):
`reported` a boolean, `omb_totals` an object holding every OMB key, and no
other, as a non-negative integer (an extra key was summed into the shared
total; the `sex` block's `totals` follow the same rule), `raw_categories` objects with a string `original` and
`omb_category`, a count and string `flags`, the race block's
`quarantined_labels` objects with a string `original` and a count, its
`unknown_inferred` a count, and `flags` a list of strings. The legacy `sex`
block needs a boolean `reported` and `totals` holding `female`, `male` and
`unknown` as counts, since the race double-count check falls back to them when
the sex/gender row gives no participant total. The `gender` block needs a
boolean `reported`, since `is_results_posted` reads every block's `reported` by
truthiness. A malformed child was read as absent or zero: `omb_totals: "corrupt"`
published a reported table as absent, and `"reported": "false"` published a
trial without results as posted. A race, ethnicity or sex block's `reported`
must be true exactly when its totals are not all zero (`WEEKLY_BLOCK_TOTALS`),
as the extractors write it: `reported: false` beside positive totals dropped
those participants. For the same reason `results_date` must be
exactly `""` or a date `parse_partial_date_bounds` reads, and so must
`start_date` and `primary_completion_date`: `classify_regulatory_era` read a
start date that does not parse as missing and fell back to the primary
completion date, so `"corrupt"` moved a trial into `pre_2017_final_rule`. A raw
record's start and primary completion date structs must carry a date that parses
too (`ERA_DATE_STRUCTS`). The sex/gender row's
numbers must be finite (JSON `1e309` parses as infinity and raised
`OverflowError` in the build) and its five participant counts non-negative;
`enrollment_minus_parsed` may be negative. The row's `sex_report_status` must
itself follow from the row as `sgp.classify_reporting` derives it: a
`not_reported` or `parse_error` row has no counts, and any other status is the
one its five counts give (a `not_reported` row with posted female and male
counts published a trial as omitting sex). `parser_rules_version`, the only
record of which parsing rules produced the published aggregates, must not be
blank, and every race and ethnicity `raw_categories` entry's `omb_category`
must be one of the block's categories (`WEEKLY_TOTAL_KEYS`): an unknown one
emptied the categories present and published Denominator B for a
single-category ethnicity table. The container's `extracted_at` and
`pipeline_commit` must each be null or well formed (a whole UTC timestamp, a
40-hex commit; `weekly_source_stamp_problem`), in the build and the contract.
`src.utils.pipeline_commit` writes null outside a checkout, so a null stamp is
recorded, but the site exporter refuses to publish a weekly build with one.
The race block's flags the analyzers trust must agree with the fields they
describe (`weekly_block_flag_problem`): `ethnicity_rows_skipped` exactly when a
recorded skipped row has a positive count, `shortfall_is_ethnicity_rows` exactly
when `unknown_ethnicity_only` is positive, `denominator_balanced` exactly when a
shortfall was folded into the unknowns, `split_from_combined` and
`combined_race_ethnicity` exactly when a category entry carries them, and
`all_zero_rejection` (race or ethnicity) only on an unreported block. A false
`ethnicity_rows_skipped` quarantined the whole race table. The race and ethnicity
`omb_totals` must equal the category rows they are summed from
(`weekly_block_totals_problem`), with only the additions the block records: a race
`unknown_not_reported` adds `unknown_inferred` and `unknown_ethnicity_only`, an
ethnicity `not_hispanic_latino` adds `not_hispanic_from_race_rows`, and an
ethnicity `unknown_not_reported` exceeds its rows exactly when the block carries
`denominator_balanced`. A total edited away from its rows was published as it stood.
The race, ethnicity and sex extractors balance against one baseline participant
total, so a record's balanced blocks must sum to the same number, and every other
reported block to at least it (`weekly_shared_total_problem`). The weekly record
does not carry that total, so a balanced ethnicity unknown is pinned exactly only
when another block is balanced (3,256 of the 4,577 in the 2026-11-08 extraction),
bounded above by a reported block otherwise (1,320), and unchecked in one record
with no reported race or sex block.
A category entry flagged `unmapped` must be in the category the extractors give
such rows (`other` for race, `unknown_not_reported` for ethnicity), since the
analyzers subtract or drop flagged rows by that rule. A condition_mesh sidecar
line's string stamps must have the producer's form too: `snapshot_date` a real
`YYYY-MM-DD` day, `extracted_at` and `pipeline_commit` as above. A cached
CT.gov response line must carry its unparsed body text and that text's 64-hex
`body_sha256`, which must match. `unknown_ethnicity_only`, `skipped_ethnicity_rows` and
`not_hispanic_from_race_rows` postdate the 2026-11-08 extraction, so they are
checked only when present. A missing or
mistyped one was read as an "Unknown" sponsor, no enrollment, a block not
reported, or a sex/gender row re-parsed as `parse_error`. All 80,890 records
of the 2026-11-08 weekly extraction pass.
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
