# Expandable Outcomes & Provenance Platform Architecture

This document specifies the 4-module expandable platform in `civicsample-engine`
that extends ClinicalTrials.gov trial analytics beyond Industry Phase 3 to NIH,
US Federal, NCTN Cooperative Group, Academic Medical Center, Non-Profit
Foundation, and all `largeDocs`-bearing trials without modifying any weekly
production publishing surface.

## 1. Non-Interference & Safety Boundaries

The expandable platform is strictly additive and isolated from the weekly
dashboard pipeline:

- **Untouched weekly publishing surfaces:** `.github/workflows/extract.yml`,
  `.github/workflows/ci.yml`, `src/extract_all.py`, `src/site_layout.py`,
  `src/dataset_folder.py`, `scripts/split_data.py`, `scripts/prune_snapshots.py`,
  and `scripts/check_site_contract.py` are unchanged.
- **Untouched industry sponsor rules:** `sponsors/company_aliases.csv` and
  `tests/sponsors/expected_counts.json` are preserved byte-for-byte.
- **Runtime compatibility:** All platform library modules under `src/` use
  100% Python standard library with `from __future__ import annotations`, full
  parameter and return type annotations, and compatibility across Python 3.11
  (`.github/workflows/ci.yml`), Python 3.12 (`.github/workflows/run_extractions.yml`),
  and Python 3.13 (local host).
- **Zero-touch `in_audit` holdout firewall:** Every module rejects
  `in_audit == True` trials unconditionally before any API query, CDN download,
  PDF parse, citation check, publication fetch, batch plan, or shard write.
- **Namespace separation from existing `outcomes/` pipelines:**
  - `data/outcomes/` belongs to `scripts/build_outcomes_beta.py` (`run.json`,
    `summary.json`, `index.json` with `"rows"`, and `records/r00.json..r07.json`).
  - `data/results_structure/` belongs to `scripts/build_results_structure.py`
    (`SG2` subgroup-reporting family: `run.json`, `summary.json`, `records.json`).
  - Module 3 writes exclusively to `data/outcomes_bundles/`
    (`outcomes_bundles/index.json` + `outcomes_bundles/shards/{n}.json.gz`,
    with `.gitignore` covering `data/outcomes_bundles/` and
    `data/outcomes_bundles.partial/`). `write_outcomes_tree` refuses to
    overwrite foreign directories (such as `data/outcomes/` or
    `data/results_structure/`).
  - Passing `--manifest data/outcomes/index.json`, `--index data/outcomes/index.json`,
    or `--cohort-manifest data/outcomes/index.json` remains valid as a
    **read-only input manifest** (`"rows"` + `"n_primary_outcomes"`), while
    `--out-dir` targets `data/outcomes_bundles`.
- **Private/offline research artifact governance:** AI-generated protocol/SAP
  decision-rule census quotations and location rows are **private/offline
  research artifacts** and are NOT published to the public site unless and
  until the investigator explicitly decides otherwise after completing blinded
  human review. All `in_audit == true` trials remain strictly quarantined from
  all AI queries, downloads, caches, and outputs.

## 2. End-to-End Platform Dataflow

```text
ClinicalTrials.gov API v2 + Posted PDFs + PubMed / Europe PMC
        │
        ├──► Module 1: src/document_indexer.py + scripts/index_trial_documents.py
        │       ├─ Content-addressed sha256 cache (DocumentCacheStore)
        │       ├─ 6-state readability + redaction classifier (CCI/PPD/white-box/CMap)
        │       ├─ Precision-aware upload timing (compute_upload_timing)
        │       └─ TOC & statistical section slicer (select_statistical_pages)
        │
        ├──► Module 2: src/analysis_population_parser.py + sponsors/ecosystem.py
        │       ├─ 9-class deterministic analysis-population parser (parse_study_outcomes)
        │       ├─ Flow-to-outcome alignment & arm_started_minus_analysed
        │       └─ Curated non-industry sponsor ecosystem (sponsors/ecosystem_rules.csv)
        │
        ├──► Module 4: src/citation_verifier.py + src/longitudinal_provenance.py
        │              + scripts/verify_and_plan_census_batch.py
        │       ├─ Single-page verbatim verifier & neutral-language guard
        │       ├─ Factual registry + document + publication interval assembler
        │       └─ Incremental sha256-keyed batch planner & budget-gated /swarm shards
        │
        └──► Module 3: src/outcomes_layout.py + scripts/build_outcomes_shards.py
                ├─ Cohort-agnostic outcomes_bundles/index.json + outcomes_bundles/shards/{n}.json.gz
                ├─ Exact parity with src/site_layout.py (nct_number_mod, 256 shards)
                └─ Atomic .partial staging + foreign-dir guard + AuditFirewallError
```

## 3. Module-by-Module Specification

### Module 1 — Document Pre-Indexer, Readability/Redaction Classifier & TOC Slicer

- **Files:** `src/document_indexer.py`, `scripts/index_trial_documents.py`,
  `tests/test_document_indexer.py`.
- **Content-addressed caching (`DocumentCacheStore`):** Separates content-pure
  `sha256`-keyed `DocumentIndexEntry` records (`data/document_index_by_sha256.jsonl`)
  and gzipped page-text maps (`data/document_cache/pages/<sha256[:2]>/<sha256>.json.gz`)
  from trial-specific `TrialDocumentProfile` bindings
  (`data/trial_document_profiles.jsonl`). Identical PDFs shared across trials
  are extracted once and re-indexed offline whenever `INDEXER_RULES_VERSION`
  advances without re-running HTTP or PDFKit. Conflicting byte counts raise
  `CacheIntegrityError`.
- **Holdout guard:** `enforce_not_in_audit` raises `AuditHoldoutError` before
  any API request, download, cache lookup, or PDF parse.
- **Non-collapsing readability & redaction taxonomy:**
  - `READABILITY_STATUSES`: `machine_readable`, `partial_image_or_redacted`,
    `image_only_scan`, `garbled_cmap`, `download_failed`, `no_document`.
  - `REDACTION_FLAGS`: `cci`, `ppd`, `white_box_or_blank_stats` via
    `detect_redactions` (zero false positives on `"Charlson Comorbidity Index (CCI)"`,
    `"PPD, Inc."`, or `"purified protein derivative (PPD)"`).
  - `analyze_page_cmap` / `detect_garbled_cmap` detect broken `ToUnicode` CMaps
    while exempting dense numeric tables.
  - `map_unreadable_recovery_bucket` emits `UNREADABLE_RECOVERY_BUCKETS`
    (`image_only_scan`, `redacted_cci_ppd`, `garbled_cmap`) aligned with Module 4.
- **Upload timing:** `parse_ctgov_date` and `compute_upload_timing` compute
  signed calendar intervals without fabricating `-01` day suffixes and with
  zero compliance or evaluative language.
- **TOC & statistical section slicer:** `select_statistical_pages` partitions
  pages `1..N` losslessly into `pages_selected`, `pages_deferred`, and
  `pages_without_text` across `STATISTICAL_SECTION_KINDS`, with lossless range
  serialization via `format_compact_page_ranges` and `expand_compact_page_ranges`.
- **Pluggable page-text providers:** `DocumentPageExtractor` (structurally
  compatible with Module 4's `PageTextProvider.get_page_text`) is implemented
  by `FixturePageTextProvider`, `JsonlPageMapProvider`, `MacOSPDFKitProvider`
  (with UTF-16 lone-surrogate sanitization via `sanitize_page_text`), and
  `ChainedPageTextProvider`.
- **Legacy bridge:** `to_legacy_census_document_row` and
  `ingest_legacy_census_documents` bridge existing `documents.jsonl` records.
- **CLI flags (`scripts/index_trial_documents.py`):**
  `--nct-id`, `--manifest` (required whenever `--nct-id` or
  `--ingest-census-jsonl` is supplied so every requested/ingested trial is
  verified against the manifest's `in_audit == False` cohort; accepts live
  read-only `data/outcomes/index.json` `"rows"` as well as `"data"`,
  `"trials"`, JSON arrays, and JSONL),
  `--ingest-census-jsonl`, `--local-pdf-dir`, `--page-map-jsonl`,
  `--cache-dir`, `--index-out`, `--profiles-out`, `--min-selected-pages`,
  `--max-selected-pages`, `--rate-limit-sec`, `--limit`, `--offline`,
  `--force-download`, `--reparse-cached-pages`.

### Module 2 — Deterministic Analysis-Population Parser & Sponsor Ecosystem

#### 2A. Analysis-Population Parser

- **Files:** `src/analysis_population_parser.py`,
  `tests/test_analysis_population_parser.py`.
- **Provenance & taxonomy:** `PARSER_RULES_VERSION` and pinned
  `VOCABULARY_SHA256` (computed by `compute_vocabulary_sha256` over ordered
  `VOCABULARY_RULES`).
  - `POPULATION_TAXONOMY` (9 non-collapsing classes): `itt`, `modified_itt`,
    `full_analysis_set`, `per_protocol`, `evaluable`, `safety`, `other_stated`,
    `not_stated`, `ambiguous_multi`.
  - `DENOM_STATUS_LEVELS`, `ARM_GAP_REASONS`, and `AGGREGATION_METHODS` record
    absence explicitly (`None` with reason, never coerced to `0`).
- **Core entry points:** `classify_population_description` (reads text only;
  attrition gaps never alter `population_class`), `extract_flow_started`,
  `align_flow_and_outcome_groups`, `parse_outcome_measure`,
  `parse_study_outcomes`, `select_raw_outcome_populations`,
  `build_outcomes_from_raw` (exact re-parse parity), `parse_error_trial_outcomes`,
  `structural_checks`, and `population_audit`.

#### 2B. Non-Industry Sponsor Ecosystem Attribution

- **Files:** `sponsors/ecosystem.py`, `sponsors/ecosystem_rules.csv`,
  `tests/sponsors/expected_ecosystem_counts.json`,
  `tests/sponsors/fixtures/sponsors_fixture_ecosystem.csv`,
  `tests/sponsors/test_ecosystem.py`.
- **Sectors:** `VALID_ECOSYSTEM_SECTORS` (`nih`, `us_federal_other`,
  `nctn_cooperative_group`, `academic_medical_center`,
  `non_profit_foundation`) compose with `industry` into `UNIFIED_SECTORS`.
- **Invariants:** Reuses `sponsors/sponsor_roles.py` normalization with zero
  substring matching in production (`load_ecosystem_rules`,
  `build_ecosystem_index`, `partition_ecosystem`, `check_cross_layer_conflicts`,
  `build_unified_index`, `partition_unified`, `sector_trials`,
  `organization_trials`, `trial_cohorts`, `audit_ecosystem`). Every rule
  carries a non-empty `note`; conflicting rules raise `ValueError`;
  `compute_ecosystem_baseline` and `write_ecosystem_baseline` enforce
  SHA-256-keyed regression baselines in `tests/sponsors/expected_ecosystem_counts.json`.

### Module 3 — Cohort-Agnostic Sharded Outcomes & Provenance Layout

- **Files:** `src/outcomes_layout.py`, `scripts/build_outcomes_shards.py`,
  `tests/test_outcomes_layout.py`.
- **Output directory & foreign-directory guard:** Writes to
  `data/outcomes_bundles/` (`outcomes_bundles/index.json` +
  `outcomes_bundles/shards/{n}.json.gz`, ignored in `.gitignore` via
  `data/outcomes_bundles/` and `data/outcomes_bundles.partial/`). Never writes
  to `data/outcomes/` (owned by `scripts/build_outcomes_beta.py`,
  `records/r00..r07.json`) or `data/results_structure/` (owned by
  `scripts/build_results_structure.py`, `SG2`); `write_outcomes_tree` refuses
  to overwrite foreign directories and cleans `<out_dir>.partial` after a
  successful swap (retaining it only if rollback restoration fails).
  `--cohort-manifest data/outcomes/index.json` remains valid as a **read-only
  input manifest**, while `--out-dir` targets `data/outcomes_bundles`.
- **Sharding & canonical encoding:** `outcomes_shard_of` uses `nct_number_mod`
  over `DEFAULT_SHARDS = 256` shards (`outcomes_bundles/shards/{n}.json.gz`),
  matching `src/site_layout.py` exactly (`SHARD_VECTORS`). `encode_json_text`
  keeps `"data"` last (`INDEX_HEADERS`, `SHARD_HEADERS`); `encode_gz_bytes`
  writes deterministic gzip bytes (`compresslevel=9, mtime=0`, OS byte `0xff`).
- **Decoupled multi-cohort index (`outcomes_bundles/index.json`):**
  `derive_cohorts` assigns sorted cohort tags from `STANDARD_COHORTS`
  (`industry_phase3`, `nih`, `cooperative_group`, `academic`,
  `us_federal_other`, `non_profit_foundation`, `all_large_docs`), keeping
  per-trial rows compact (`INDEX_ROW_KEYS`).
- **Unified shard record (`SHARD_RECORD_KEYS`):** Combines `reported_results`
  (`extract_reported_results`), `document_profile` (`normalize_document_profile`),
  `decision_rule_locations` (`normalize_decision_rule_locations`, retaining
  append-only `primary_pass`, `second_pass`, `ocr_recovery_pass`, and
  `adjudication_pass` alongside deterministic `effective_pass`, enforcing
  `CITATION_REQUIRED_STATUSES` where `FOUND`, `AMBIGUOUS`, and `NOT_APPLICABLE`
  each require `>= 1` citation across normalization and on-disk validation
  while `NOT_FOUND` and `NO_DOCUMENT` forbid citations, and rejecting
  `NO_DOCUMENT` / `census_status="no_document"` whenever posted documents are
  established by `document_profile`, manifest/trial `protocol`/`sap` flags or
  `largeDocs`, or `longitudinal_provenance`), and
  `longitudinal_provenance` (`normalize_longitudinal_provenance`) via
  `build_trial_bundle` and `build_outcomes_documents`.
- **Hard `in_audit` firewall:** `assert_no_audit_leakage` and
  `assert_no_audit_cross_stream_leakage` raise `AuditFirewallError` across
  bundle construction, JSONL ingestion, and on-disk `validate_outcomes_tree` /
  `problems` verification before atomic swap in `write_outcomes_tree`.
- **CLI flags (`scripts/build_outcomes_shards.py`):**
  `--trials`, `--cohort-manifest`, `--documents`, `--locations`,
  `--provenance`, `--population-parsed`, `--extracted-at`, `--now-utc`,
  `--source-pipeline-commit`, `--allow-null-commit`, `--require-commit`,
  `--shards`, `--out-dir` (default `data/outcomes_bundles`), `--report`,
  `--check`.

### Module 4 — Mechanical Citation Verifier, Longitudinal Provenance & Batch Planner

#### 4A. Mechanical Citation Verifier

- **Files:** `src/citation_verifier.py`, `tests/test_citation_verifier.py`.
- **Hard holdout firewall:** `enforce_audit_holdout` raises
  `AuditHoldoutViolationError` unconditionally on `in_audit=True` or
  `nct_id in audit_nct_ids`.
- **Single-page verbatim verification:** `normalize_for_page_match` handles
  NFKC ligatures, Unicode dashes/quotes, invisible characters, and soft-hyphen
  or hyphenated line-wraps; `match_quotation_on_page` evaluates `"exact_ws"`
  and `"unicode_nfkc_hyphen"` strictly on one physical page. `verify_citation`
  forbids ellipses (`has_ellipsis`), rejects cross-page spans with
  `cross_page_span_violation`, and checks `pages_without_text` and
  `pages_not_searched` bounds against any `PageTextProvider`
  (`DictPageTextProvider`, `DirectoryPageTextProvider`, or Module 1's
  `DocumentPageExtractor`).
- **Corpus & coverage invariants:** `verify_document_record`,
  `verify_location_record`, `verify_trial_completeness`, and
  `verify_census_corpus` enforce `CITATION_REQUIRED_STATUSES` (`FOUND`,
  `AMBIGUOUS`, and `NOT_APPLICABLE` each require `>= 1` citation, matching
  Module 3), `NOT_FOUND` requiring `pages_not_searched == []`
  in exhaustive mode, `UNREADABLE` validity checks, `NO_DOCUMENT` rejection when
  posted documents exist in `documents.jsonl` or manifest/index flags
  (`manifest_has_documents` / `expected_has_documents_by_nct`, emitting
  `NO_DOCUMENT_WITH_POSTED_DOCUMENT`), and exact `7 * K` grid completeness on
  `"primary"` (always verified even when only sparse passes are present) and
  `"second"`, while sparse override passes in `SPARSE_PASSES` (`ocr_recovery`,
  `adjudication`) run `verify_trial_completeness(..., allow_sparse=True)` to
  allow targeted cell overrides while still enforcing `0..K-1` bounds and
  duplicate checks, stamped with `VERIFIER_RULES_VERSION` and
  `NEUTRAL_LANGUAGE_VOCAB_SHA256`.
- **Neutral-language guard:** `scan_neutral_language` (aliased as
  `check_neutral_language`) and `scan_neutral_language_record` block objective-met
  claims, statistical significance result claims, p-value spin phrases, and
  FDAAA/misconduct inferences in commentary while exempting verbatim source
  fields (`quotation`, `statement_verbatim`).

#### 4B. Longitudinal Provenance Assembler

- **Files:** `src/longitudinal_provenance.py`,
  `tests/test_longitudinal_provenance.py`.
- **Schema & partial-date calculus:** Stamped with
  `LONGITUDINAL_SCHEMA_VERSION` (`PROVENANCE_SCHEMA_VERSION`) and
  `PROVENANCE_RULES_VERSION`. `parse_partial_date`, `parse_iso_or_partial_date`,
  `parse_ctgov_upload_date`, `parse_printed_doc_date`, and `parse_pubmed_date`
  preserve calendar bounds without fabricating `-01` day suffixes;
  `compute_day_delta_interval` populates `exact_days` only when both endpoints
  have `date_precision == "day"` and emits leap-year-aware `[min_days, max_days]`
  otherwise.
- **Publication origin separation:** `assemble_longitudinal_provenance`
  consumes registry `referencesModule`, `resultsSection.moreInfoModule`,
  `PubMedFetcher`-compatible `PublicationProvider` records, and Europe PMC
  records, deduplicating deterministically while keeping `LINK_ORIGINS`
  (`AUTO_INDEXED` vs `INVESTIGATOR_SUBMITTED`) and `registry_reference_types`
  (`BACKGROUND`, `DERIVED`, `RESULT`) strictly separate.
  `to_outcomes_layout_provenance` projects directly to Module 3's shard schema.

#### 4C. Incremental Batch Planner & Verifier CLI

- **Files:** `scripts/verify_and_plan_census_batch.py`,
  `tests/test_verify_and_plan_census_batch.py`.
- **Subcommands:** `verify` and `plan` (exit `0` ok, `1` verification or budget
  gate failure, `2` `AuditHoldoutViolationError`).
- **Incremental `sha256` cache & `UNREADABLE` partitioning:**
  `plan_incremental_cohort` accepts live read-only `data/outcomes/index.json`
  (`"rows"` + `"n_primary_outcomes"`) alongside `"data"` / `"trials"` +
  `"primary_outcomes_count"`, computes `compute_doc_sha256_tuple` and
  `compute_doc_set_fingerprint`, resolves per-cell effective status in
  precedence order (`primary -> ocr_recovery -> adjudication`) when identifying
  unresolved `UNREADABLE` targets and cache hits, skips already-verified
  `(nct_id, pass_role, doc_sha256_tuple)` trials at `$0` only when the
  full-grid pass covers exact `set(range(expected_k))` with all 7 elements per
  outcome (routing index mismatches such as `{0, 2}` for `expected_k=2` to
  `amended_doc_or_outcome_delta`), detects amended PDFs,
  partitions `UNREADABLE` trials via `classify_unreadable_subtype` into
  `image_only_scan`, `garbled_cmap`, and terminal `$0` `redacted_cci_ppd`,
  computes `max_launchable_shards` and `launch_approved` against
  `--budget-cap-usd`, and emits ready-to-launch `/swarm` shard manifests.
  In `verify` mode, supplying `--index` also populates
  `expected_has_documents_by_nct` from each `in_audit == False` row's
  `protocol` / `sap` flags and `expected_outcomes_by_nct` from
  `primary_outcomes_count` / `n_primary_outcomes`.

## 4. Verification Commands

Run all commands from the repository root inside the configured virtual
environment:

```bash
python -m compileall -q src scripts sponsors tests
python scripts/validate_fixes.py
python -m pytest tests -q -rs
node --test tests/sponsors/*.test.mjs
```
