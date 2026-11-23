# Drug trial cross-source evidence & disclosure timeline (manual)

Links registered drug and biological trials across **ClinicalTrials.gov API v2**, **PubMed / Europe PMC**, and **openFDA Drugs@FDA**, computing date-precision-aware disclosure timelines, 5-route search coverage, cross-source discordance states, and curated sponsor rollups.

It runs by hand; no scheduled workflow invokes it, and the weekly demographics pipeline, its cohort, and its published files are untouched.

## Entities & curated inputs

| Entity / File | Identifier | Role |
|---|---|---|
| Registered trial | `NCT\d{8}` | `studies.json`, `coverage.json`, `timelines.json` |
| Evidence link | `link:<sha256[:16]>` | `disclosure/evidence.py`, `links.json` |
| Curated human reviews | `disclosure/reviews.csv` | `link_id,nct_id,reviewer,reviewed_at,decision,notes` (header-only until edited by a human; never written by code; private `reviewer` names are never copied to artifacts) |
| Curated documentary statements | `disclosure/documentary_statements.csv` | `statement_id,nct_id,source_type,source_locator,statement_date,verbatim_excerpt,recorded_by,notes` (header-only until edited by a human) |
| Sponsor rules (read-only) | `sponsors/company_aliases.csv` | Curated attribution rules pinned by sha256; never modified here |

## Automated validation vs human review

`automated_validation` records mechanical checks only:
- `identifier_confirmed`: an identifier stands in the retrieved source and resolves (posted `resultsSection` on CT.gov, PubMed-resolved PMID in `referencesModule`, exact `NCT_ID` token in an Europe PMC record, or exact `NCT_ID` token in an openFDA `drugsfda` record).
- `quote_confirmed_in_source`: a curated row in `disclosure/documentary_statements.csv` whose `verbatim_excerpt` stands at `source_locator` in a frozen retrieved source. The excerpt is compared only with the value the locator names (`pmid:<PMID>` with no sub-path names that PMID's whole esummary record), never with the rest of the response.
- `identifier_not_resolved`: a cited PMID that fails resolution in NCBI PubMed `esummary` (404 or error).
- `rejected_by_rule`: a full-text search hit lacking an exact `NCT_ID` token in any retrieved field, or a high-volume generic drug query (`>= 25` applications) without sponsor agreement — retained for audit visibility and set aside.
- `not_validated`: heuristic sponsor+drug NDA/BLA candidates, free-text citations without a PMID, or statements whose excerpt was not confirmed; `detail.statement_check` and the validation reason say why (`source_not_retrieved`, `locator_not_resolved`, `excerpt_not_found_at_locator`).

When several drug queries for one trial return the same openFDA application, one link is kept for it: the strongest classification (`identifier_confirmed` > `not_validated` candidate > `rejected_by_rule`), ties broken by query term. A flooded generic query therefore cannot hide a precise query's reviewable candidate; `detail.drug_queries_returning_application` lists every query that returned it.

Unreviewed links are **never** called "validated". Semantic confirmation (`review_status` in `{"unreviewed", "confirmed_same_trial_results", "confirmed_background_only", "rejected_false_positive", "indeterminate"}`) comes solely from `disclosure/reviews.csv`.

## 5-route × 7-state coverage (`coverage.json`)

Routes: `ctgov_results`, `registry_references`, `literature_search`, `openfda_drugsfda`, `documentary_statements`.

States: `not_searched`, `search_incomplete_or_blocked`, `document_unavailable`, `available_document_insufficient`, `candidate_awaiting_review`, `no_candidate_in_completed_scope`, `verified_link`. Only `no_candidate_in_completed_scope` is a negative finding; `not_searched` (e.g. when offline builds run from local CT.gov archives without literature or openFDA caches) is never conflated with "none found".

## Date-precision-aware timeline & descriptive 365-day window (`timelines.json`)

- **No fabricated `-01` days**: partial dates (`YYYY`, `YYYY-MM`) keep their exact `[earliest, latest]` calendar spans (`leap-year` aware).
  - Both `YYYY-MM-DD`: `state = "exact"`, `exact_days = int`.
  - Non-overlapping partial ranges: `state = "bounded_partial"`, `bounds_days = [min_days, max_days]`, `exact_days = null`.
  - Overlapping partial ranges: `state = "indeterminate_at_recorded_precision"`, `bounds_days = null`, `exact_days = null`.
  - An endpoint bounded on one side only (`bounded_above_only`, below): `state = "indeterminate_at_recorded_precision"`, `one_sided_endpoints` names it, and no exact, minimum or maximum days are computed.
  - Missing endpoint(s): `state = "endpoint_missing"`.
- **Earliest linked publication / FDA action**: the earliest date across qualifying links lies in `[min earliest, min latest]` of their spans; when that is tighter than any single date the milestone is `bounded_composite`. An undated qualifying link may precede every dated one, so then the milestone has `precision = "bounded_above_only"`, `date = null`, `earliest_bound = null` and only `latest_bound` (with `has_undated_qualifying_link = true`); no lag is computed from it. With no dated qualifying link the milestone is `missing`.
- **QC lag & cycles**: `resultsFirstSubmitDate -> resultsFirstPostDate` plus `protocolSection.statusModule.submissionInfos` cycle counts (kept `null` when absent, never coerced to `0`).
- **Descriptive 365-day window**: `POSTED_WITHIN_365D`, `POSTED_AFTER_365D`, `PENDING_SUBMITTED_UNPOSTED`, `UNREPORTED_PAST_365D`, `UNREPORTED_WITHIN_365D`, `DATE_PRECISION_INDETERMINATE`, `COMPLETION_DATE_MISSING_OR_FUTURE`. Strictly describes registry dates; never claims legal FDAAA 801 / 42 CFR Part 11 non-compliance.
- **Cross-source discordance**: `published_results_while_ctgov_unreported`, `fda_cited_while_ctgov_unreported`, `ctgov_posted_no_linked_results_publication`, `concordant_multi_source_disclosed`, `unreported_across_completed_routes`, `coverage_incomplete_or_awaiting_review`.
- **Sponsor rollups (`sponsors.json`)**: read-only `sponsors/` attribution providing both `current_owner` (`canonical`) and `as_registered` (`entity`) views while keeping `attributed`, `reviewed_excluded`, and `unreviewed` strictly disjoint and separate.

## Running it

```bash
# 1a. Live rate-limited (<= 2 req/s per host), resumable network acquisition + sha256 freeze:
python3 scripts/build_drug_disclosure.py acquire \
    --cache build/disclosure_cache --nct-list ids.txt

# 1b. Or freeze local CT.gov v2 study records (.json / .json.gz / .jsonl.gz) with zero network calls:
python3 scripts/build_drug_disclosure.py freeze-local \
    --records /path/to/ctgov_records --cache build/disclosure_cache

# 2. Offline deterministic build from frozen cache (runs contract check before writing):
python3 scripts/build_drug_disclosure.py build \
    --cache build/disclosure_cache --out build/disclosure --as-of 2026-11-01

# 3. Producer contract check and atomic site export:
python3 scripts/check_drug_disclosure_contract.py build/disclosure
python3 scripts/export_drug_disclosure_site.py build/disclosure /tmp/site_disclosure

# 4. Offline test suite:
pytest tests/disclosure -q
```

## Compatibility

New files only (`disclosure/`, three CLI scripts under `scripts/`, and `tests/disclosure/`). Zero existing tracked files, workflows, or published site contracts are modified.
