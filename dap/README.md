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
  lines must carry one `(snapshot_date, extracted_at, pipeline_commit)` triple.
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
python3 scripts/build_dap_benchmarks.py --demographics data/demographics.json.gz --out build/dap
python3 scripts/check_dap_benchmarks_contract.py build/dap
python3 scripts/export_dap_benchmarks_site.py build/dap ../clinical-trial-populations/data/dap
python3 -m pytest tests/dap -q
```
