# Census Iteration & `/swarm` Operating Playbook

This playbook defines the deterministic, budget-gated workflow for resuming
unstarted census shards, recovering `UNREADABLE` PDFs without wasting tokens on
redacted documents, and expanding rule censuses to new cohorts (`nih`,
`cooperative_group`, `academic`, `us_federal_other`, `non_profit_foundation`,
and `all_large_docs`) under the Carbon -> Neon `/swarm` operating model.

## 1. Non-Negotiable Operational Invariants

1. **Hard `in_audit` holdout firewall:** No trial with `in_audit == True` may
   be fetched, downloaded, OCR-processed, verified, planned into a shard, or
   written with PDF/census metadata. Every CLI enforces this upfront and exits
   with code `2` on violation (`AuditHoldoutError` in `src/document_indexer.py`,
   `AuditHoldoutViolationError` in `src/citation_verifier.py`,
   `AuditFirewallError` in `src/outcomes_layout.py`).
2. **Incremental `sha256`-keyed work skipping:** Trials already verified for
   `(nct_id, pass_role, doc_sha256_tuple)` are skipped at `$0` unless a posted
   PDF's `sha256` changes or registered primary outcomes change.
3. **Strict single-page verbatim citations:** Every quotation must match one
   physical page (`match_quotation_on_page` in `src/citation_verifier.py`),
   carry no ellipses (`has_ellipsis`), and never cross a page boundary
   (`cross_page_span_violation`).
4. **Neutral-language discipline:** All commentary fields (`note`) must pass
   `scan_neutral_language` with zero evaluative, spin, or FDAAA compliance
   claims. Verbatim `quotation` and `statement_verbatim` strings remain exact.
5. **Append-only pass history:** `primary`, `second`, `ocr_recovery`, and
   `adjudication` passes coexist in `locations.jsonl` keyed by `"pass"`. Never
   overwrite an earlier pass in place.

## 2. Three-Bucket `UNREADABLE` Recovery Triage

`scripts/verify_and_plan_census_batch.py` (`classify_unreadable_subtype`) and
`src/document_indexer.py` (`map_unreadable_recovery_bucket`) partition every
`UNREADABLE` trial into three disjoint operational buckets:

| Bucket (`UNREADABLE_SUBTYPES`) | Root Cause | Action | LLM / OCR Spend |
|---|---|---|---|
| `redacted_cci_ppd` | Sponsor-redacted statistical sections (`CCI`, `PPD`, white-box blanking) | Terminal skip; keep `status = "UNREADABLE"` and stamp `unreadable_subtype = "redacted_cci_ppd"` | `$0` (never scheduled) |
| `garbled_cmap` | Broken `ToUnicode` CMap or ciphered font table with clean vector outlines | Stage-1 local macOS Vision OCR (`text_source="macos_vision_ocr_v1"`) -> Stage-2 targeted `ocr_recovery` shard | Targeted cells only |
| `image_only_scan` | Pure scanned PDFs or recurring watermark-only scans | Stage-1 local Vision OCR + Module 1 `detect_redactions` filter (drops scanned `CCI`/`PPD` to `$0` terminal skip) -> Stage-2 targeted `ocr_recovery` shard | Unredacted cells only |

## 3. Carbon -> Neon `/swarm` Wave Pattern

Use a two-phase `/swarm` wave whenever adding a new cohort, rule pass, or
pipeline capability:

- **Phase 1 (Carbon — Read-Only Architecture & Pre-Flight):**
  - Inspect cohort counts, `in_audit` boundaries, cache hit rates, and cost
    projections read-only.
  - Lock file ownership boundaries, schema contracts, and budget caps before
    modifying any tracked file or spawning paid extraction workers.
- **Phase 2 (Neon — Parallel Sharded Execution & Independent Verification):**
  - Spawn parallel workers with disjoint shard assignments from
    `scripts/verify_and_plan_census_batch.py plan`.
  - Pair execution shards with a read-only verifier running mechanical checks
    before landing outputs.

## 4. The 5-Step `/swarm` Census Loop

### Step 1: Pre-Fetch & Pre-Index Outside Workers

Run Module 1 once on the coordinator host so every worker starts from cached
`sha256` page maps and pre-sliced statistical pages (`pages_selected`, `10-25`
pages/trial instead of full `150+`-page protocols) while obeying the global
`0.55s` CT.gov rate limit:

```bash
python scripts/index_trial_documents.py \
  --manifest data/outcomes/index.json \
  --cache-dir data/document_cache \
  --index-out data/document_index_by_sha256.jsonl \
  --profiles-out data/trial_document_profiles.jsonl \
  --min-selected-pages 10 \
  --max-selected-pages 25 \
  --rate-limit-sec 0.55
```

Always pass `--manifest` with `scripts/index_trial_documents.py`, including when
targeting specific trials via `--nct-id` or bridging legacy records via
`--ingest-census-jsonl`; omitting `--manifest` or referencing a trial outside
the manifest's `in_audit == False` cohort raises `AuditHoldoutError` (exit `2`).

Then compute the incremental batch plan and verify that `launch_approved` is
`true` under your budget cap:

```bash
python scripts/verify_and_plan_census_batch.py plan \
  --index data/outcomes/index.json \
  --cohort industry_phase3 \
  --documents rule-census-20261114/documents.jsonl \
  --locations rule-census-20261114/locations.jsonl \
  --doc-cache data/document_index_by_sha256.jsonl \
  --profiles data/trial_document_profiles.jsonl \
  --mode resume-unstarted \
  --shard-size 18 \
  --budget-cap-usd 50.00 \
  --cost-schedule sliced \
  --out-plan /tmp/next_swarm_batch_plan.json
```

Inspect `cost_projection`, `max_launchable_shards`, `excluded_in_audit_count`,
and `partition_counts` before spawning workers:

- Exit `0`: `launch_approved == true`; all emitted shards fit inside
  `--budget-cap-usd`.
- Exit `1`: projected spend exceeds `--budget-cap-usd`; the plan JSON is still
  written so you can launch only the first `max_launchable_shards` shards
  (`within_budget_cap == true`) or adjust `--cohort` / `--shard-size`.
- Exit `2`: hard `AuditHoldoutViolationError`; an `in_audit == True` trial
  appeared in `--documents`, `--locations`, `--profiles`, or `--nct-id`.

### Step 2: Offline Vision OCR Pre-Pass Before Recovery Shards

For `UNREADABLE` recovery waves, generate the recovery plan first:

```bash
python scripts/verify_and_plan_census_batch.py plan \
  --index data/outcomes/index.json \
  --cohort industry_phase3 \
  --documents rule-census-20261114/documents.jsonl \
  --locations rule-census-20261114/locations.jsonl \
  --doc-cache data/document_index_by_sha256.jsonl \
  --profiles data/trial_document_profiles.jsonl \
  --mode recover-unreadable \
  --shard-size 18 \
  --budget-cap-usd 50.00 \
  --out-plan /tmp/ocr_recovery_plan.json
```

1. Consume `ocr_prepass_queue` offline on macOS using local Vision OCR
   (`VNRecognizeTextRequest`, `usesLanguageCorrection=False`) to write
   `pages_ocr/{sha256}.json` (`text_source="macos_vision_ocr_v1"`).
2. Run `src/document_indexer.py`'s `detect_redactions` over recovered OCR text.
   Any scanned page revealing `CCI` or `PPD` markers reclassifies immediately
   to `redacted_cci_ppd` at `$0` LLM spend.
3. Dispatch `pass="ocr_recovery"` workers **only** on trials with recovered
   unredacted statistical prose, scoped strictly to each shard's
   `target_elements_by_nct` (never re-running already-`FOUND` elements).

### Step 3: Scoped Escalation Rule Inside Workers

1. Each worker reads the pre-sliced `pages_selected` first.
2. If all 7 elements (`ELEMENTS`) for every registered primary outcome are
   resolved (`FOUND` or `NOT_APPLICABLE`), the worker records citations and
   stops.
3. If any element remains unresolved, the worker **must** search
   `pages_deferred` so `pages_searched == "all"` and `pages_not_searched == []`
   before emitting `status = "NOT_FOUND"`. In default publication mode,
   `verify_location_record` rejects `NOT_FOUND` whenever `pages_not_searched`
   is non-empty (`NOT_FOUND_WITH_UNSEARCHED_PAGES`), and rejects `NO_DOCUMENT`
   whenever posted documents exist in `documents.jsonl` or `--index`
   `protocol` / `sap` flags (`expected_has_documents_by_nct` /
   `manifest_has_documents`, emitting `NO_DOCUMENT_WITH_POSTED_DOCUMENT`).

### Step 4: Mechanical Gate Before Posting `[done]`

Every worker (or shard verifier) runs `verify` over its shard output before
declaring completion:

```bash
python scripts/verify_and_plan_census_batch.py verify \
  --index data/outcomes/index.json \
  --documents rule-census-20261114/documents.jsonl \
  --locations rule-census-20261114/locations.jsonl \
  --pages-dir data/document_cache/pages \
  --check-completeness \
  --report-out /tmp/census_verify_report.json
```

When verifying a merged corpus with `--check-completeness`,
`verify_census_corpus` always enforces the full `7 * K` grid on
`pass = "primary"` (so orphan sparse rows without `"primary"` fail with
`TRIAL_GRID_MISSING_CELL`), while sparse override passes in `SPARSE_PASSES`
(`ocr_recovery`, `adjudication`) run `verify_trial_completeness` with
`allow_sparse=True` — permitting targeted cell overrides while still enforcing
`0 <= primary_outcome_index < K` bounds (`TRIAL_GRID_UNEXPECTED_CELL`) and
duplicate checks (`TRIAL_GRID_DUPLICATE_CELL`). Passing `--index` also
populates `expected_has_documents_by_nct` from each `in_audit == False` row's
`protocol` / `sap` flags so `NO_DOCUMENT` is rejected even when
`documents.jsonl` omits those records. For standalone `ocr_recovery` or
`adjudication` shards prior to merging with `"primary"`, omit
`--check-completeness` (and for `ocr_recovery`, pass `--ocr-dir <pages_ocr_dir>`
and `--text-source macos_vision_ocr_v1`). Note that `--check-completeness`
validates every `in_audit == False` trial present in `--index`; single-shard
verification runs should pass a shard-scoped `--index` (or omit
`--check-completeness` until verifying the merged corpus).

A shard is eligible to land only when `verify` exits `0` (`ok == true`,
`substring_mismatches == 0`, `ellipsis_flags == 0`,
`neutral_language_flags == 0`, and zero `AuditHoldoutViolationError`s).

### Step 5: Append-Only Merge & Sharded Outcomes Build

1. Append verified shard rows to `locations.jsonl` preserving each row's
   `"pass"` (`primary`, `second`, `ocr_recovery`, `adjudication`).
2. Assemble factual longitudinal provenance per trial with
   `src/longitudinal_provenance.py` (`assemble_longitudinal_provenance` /
   `to_outcomes_layout_provenance`).
3. Build and validate the sharded outcomes tree atomically via Module 3:

```bash
python scripts/build_outcomes_shards.py \
  --trials data/demographics.json.gz \
  --cohort-manifest data/outcomes_manifest.json \
  --documents data/trial_document_profiles.jsonl \
  --locations rule-census-20261114/locations.jsonl \
  --provenance data/longitudinal_provenance.jsonl \
  --out-dir data/outcomes \
  --report /tmp/outcomes_build_report.json

python scripts/build_outcomes_shards.py --check data/outcomes
```

## 5. Expanding to New Cohorts

To launch a new cohort (for example `nih`, `cooperative_group`, or `academic`):

1. Classify trial sponsors using `sponsors/ecosystem.py`
   (`build_unified_index` and `trial_cohorts`) without editing
   `sponsors/company_aliases.csv`.
2. Build or update the cohort manifest carrying explicit boolean `in_audit`
   flags and cohort tags matching `STANDARD_COHORTS` in `src/outcomes_layout.py`.
3. Run Step 1 (`scripts/index_trial_documents.py --manifest ...` followed by
   `scripts/verify_and_plan_census_batch.py plan --cohort <slug> --mode incremental-delta`).
   Cross-trial duplicate PDFs (same `sha256`) automatically hit
   `DocumentCacheStore` at zero re-download/re-parse cost.
4. Execute Steps 2–5 cleanly within `--budget-cap-usd`.
