# Rule-Location Census — Status Report (`rule-census-20261114`)

Reported at: `2026-11-21T05:44:25Z` (**COMPLETE — `402 / 402` primary-pass trials and `40 / 40` independent second-pass trials finished and mechanically verified**).

## 1. In-Scope Primary-Pass Trial Progress (Denominator = 402)

From `https://civicsample.com/data/outcomes/index.json` (`598` total rows), **402 / 598 (67.2%)** rows met `(protocol == true or sap == true) and in_audit == false`. Zero (`0`) trials with `in_audit == true` were queried, opened, or recorded.

| Trial State | Count / Denominator | Details |
|---|---|---|
| **Complete** | **402 / 402 (100.0%)** | All ClinicalTrials.gov `primaryOutcomes` (`686` primary outcomes, `4802` primary-pass element rows across all 7 decision-rule elements) recorded in `locations.jsonl`; all `782 / 782 (100.0%)` posted protocol/SAP PDFs downloaded with HTTP `200` in `documents.jsonl` (`0` HTTP errors); all extractable-text pages searched (`pages_searched == "all"` and `pages_not_searched == []` on `4802 / 4802 (100.0%)` rows). Includes `66 / 402 (16.4%)` completed trials where one or more elements were classified `UNREADABLE` (`405 / 4802 (8.4%)` primary-pass rows) due to scanned image-only, redacted, or garbled-CMap PDF pages. |
| **Partly done** | **0 / 402 (0.0%)** | Zero trials have missing primary outcomes, missing decision-rule elements, failed/partial PDF downloads, or unsearched extractable-text page ranges (`pages_not_searched` is `[]` on every row). |
| **Not started** | **0 / 402 (0.0%)** | All 54 trials previously held back by the Wave 2 cost cap (`shard-18` [`18`], `shard-19` [`17`], `shard-20` [`19`]) were completed in Wave 3 via `neon-shard-1`..`neon-shard-6`. |
| **Total in scope** | **402 / 402 (100.0%)** | `62 / 402 (15.4%)` completed in Wave 1 (`shard-01`..`shard-03`), `32 / 402 (8.0%)` in Wave 2 Step 1 (`sample-prim-01`..`02`), `254 / 402 (63.2%)` in Wave 2 Step 3 (`shard-04`..`shard-17`), and `54 / 402 (13.4%)` in Wave 3 (`neon-shard-1`..`neon-shard-6` covering `shard-18`..`shard-20`). |

### Primary-Pass Element × Status Breakdown (`4802` Rows Across `686` Outcomes of `402` Trials)

| Element | `FOUND` | `NOT_FOUND` | `NOT_APPLICABLE` | `AMBIGUOUS` | `UNREADABLE` | `NO_DOCUMENT` | Total |
|---|---|---|---|---|---|---|---|
| `primary_endpoint_analysed` | 618 | 0 | 0 | 29 | 39 | 0 | 686 |
| `analysis_population` | 528 | 0 | 0 | 110 | 48 | 0 | 686 |
| `hypothesis_type` | 378 | 58 | 140 | 43 | 67 | 0 | 686 |
| `alpha_and_sidedness` | 420 | 6 | 153 | 53 | 54 | 0 | 686 |
| `margin` | 64 | 58 | 484 | 12 | 68 | 0 | 686 |
| `interim_boundary` | 117 | 47 | 397 | 58 | 67 | 0 | 686 |
| `multiplicity` | 361 | 14 | 223 | 26 | 62 | 0 | 686 |
| **All 7 Elements** | **2486 (51.8%)** | **183 (3.8%)** | **1397 (29.1%)** | **331 (6.9%)** | **405 (8.4%)** | **0 (0.0%)** | **4802** |

## 2. 40-Trial Independent Second Pass

- **Did the 40-trial independent second pass run?** **Yes.**
- **How many trials completed?** **40 / 40 (100.0%)** trials in the fixed sample (`0 / 40 (0.0%)` partly done, `0 / 40 (0.0%)` not started), paired **40 / 40 (100.0%)** with completed primary-pass records (`40 / 402 (10.0%)` of all in-scope trials).
- **Execution details:** The 40-trial sample was fixed before Wave 1 by sorting all 402 in-scope `NCT_ID`s ascending by lowercase hex `sha256(NCT_ID)` and taking the first 40. Two fresh blinded workers (`second-pass-01` [`20 / 40 (50.0%)`] and `second-pass-02` [`20 / 40 (50.0%)`]) independently extracted all `61` primary outcomes (`427` second-pass element rows) without access to primary-pass outputs. Exact status consistency between model runs was **383 / 427 (89.7%)** (measures consistency between model runs, not accuracy).

## 3. Mechanical Verification Summary (`src/citation_verifier.py` v1.0.0)

Full offline verification ran via `src.citation_verifier.verify_census_corpus(strict_document_keys=True, require_exhaustive_for_not_found=True)` across all **402 / 402** trials (`782` documents, `5,229` location rows, `11,629` citations), preserving all `678` Wave 1/2 `documents.jsonl` rows and all `4,718` Wave 1/2 `locations.jsonl` rows **byte-for-byte untouched**:

- **Wave 3 Neon (`54` trials, `104` PDFs, `511` primary rows, `909` citations):** `ok = True`, **`0` errors**, **`0` warnings**, **`909 / 909 (100.0%)`** `exact_ws_matches`, independently audited by `census-verify-1` (`neon-shard-1..3`, `301` rows, `533/533` `exact_ws`) and `census-verify-2` (`neon-shard-4..6`, `210` rows, `376/376` `exact_ws`).
- **Full `402 / 402` Corpus (`782` PDFs, `5,229` rows, `11,629` citations):** **`0`** `in_audit` holdout violations, **`0`** schema/enum/SHA-256/byte-count errors, **`0`** outcome-grid completeness errors, **`0`** exhaustive-search violations, **`0`** ellipsis flags, **`0`** neutral-language flags, **`0`** cross-page span violations, **`11,594 / 11,629 (99.70%)`** `exact_ws_matches` against unified `MacOSPDFKitProvider` page caches.
- **Preserved Wave 1/2 Legacy Items (Untouched):**
  - `35 / 11,629 (0.30%)` legacy Wave 1/2 `CITATION_PAGE_SUBSTRING_MISMATCH` items (across `11` Wave 1/2 trials) attributable strictly to `pypdf`-vs-`PDFKit` intra-line kerning whitespace or diagonal watermark text-stream ordering (all `10,720 / 10,720` Wave 1/2 citations passed Wave 1/2's original coordinator check).
  - `35` non-blocking `NOTE_PAGE_COUNT_MISMATCH` warnings originating strictly from the untouched 7-row `NCT01265849` outcome 1 free-text note (`locations.jsonl` lines 8–14, `7 × 5` SAP filenames).

## 4. Transparent Spend & Quota Accounting (Waves 1–3)

### 4.1 Historical Wave 1 + Wave 2 Token-Price Spend (`2026-11-14T01:21:51Z` to `2026-11-17T14:02:56Z`, `348` Primary Trials + `40` Second-Pass Trials)

- **How Wave 1 + Wave 2 spend was measured:** **Token counts times price.** Antigravity runs on plan quota and does not expose a per-run USD billing page in the workspace. Exact per-turn token counts (`input_tokens`, `cache_read_tokens`, and `output_tokens`) were summed directly from every `PLANNER_RESPONSE` step in `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl` across all **23 / 23 (100.0%)** Wave 1/2 swarm conversations (`coordinator`, `monitor`, `shard-01`..`shard-17`, `sample-prim-01`..`02`, and `second-pass-01`..`02`) from `2026-11-14T01:21:51Z` through `2026-11-17T14:02:56Z`, and multiplied per turn by published tiered API prices (`<=200k` vs. `>200k` context window per turn).
- **Cumulative Wave 1 + Wave 2 logged token usage:**
  - **Planner turns:** `2,465` total (`260` `coordinator`, `438` `monitor`, `1,767` across the `21` extraction workers)
  - **Uncached input tokens:** `28,108,410` (`5,043,863` `coordinator`, `1,136,662` `monitor`, `21,927,885` workers)
  - **Cached input (`cache_read`) tokens:** `251,723,647` (`28,350,886` `coordinator`, `34,036,840` `monitor`, `189,335,921` workers)
  - **Output tokens:** `4,031,825` (`308,199` `coordinator`, `39,698` `monitor`, `3,683,928` workers)

| Pricing Schedule (`token counts × price`, Wave 1 + Wave 2) | Coordinator (`Gemini 4 Carbon (High)`) | Monitor (`Claude Sonnet 4.6 (Thinking)`) | 21 Extraction Workers (`Gemini 4 Carbon (High)`) | Wave 1 + Wave 2 Total | Measured Cost per Wave 1/2 Primary Trial (`348` trials) |
|---|---|---|---|---|---|
| **Model-matched standard API rates** (Gemini Pro: \$1.25 / \$0.125 / \$10.00 per 1M `<=200k`, \$2.50 / \$0.25 / \$15.00 `>200k`; Claude Sonnet 4.6: \$3.00 / \$0.30 / \$15.00 per 1M `<=200k`) | \$17.80 | \$14.22 | \$107.58 | **\$139.60 USD** | \$0.4011 / trial |
| **Uniform Gemini Pro standard API rates** (\$1.25 / \$0.125 / \$10.00 per 1M `<=200k`, \$2.50 / \$0.25 / \$15.00 `>200k` across all 23 agents) | \$17.80 | \$6.07 | \$107.58 | **\$131.45 USD** | \$0.3777 / trial |
| **Conservative flagship API rates** (\$2.00 / \$0.20 / \$12.00 per 1M `<=200k`, \$4.00 / \$0.40 / \$18.00 `>200k`, used during `2026-11-14` shard gating) | \$27.14 | \$9.56 | \$155.14 | **\$191.83 USD** | \$0.5512 / trial |

### 4.2 Wave 3 Completion Quota Accounting (`2026-11-21`, Final `54` Primary Trials)

- **Execution & billing mechanism:** Wave 3 (`54` trials, `104` PDFs, `73` primary outcomes, `511` primary-pass rows, `909` citations) ran on **Antigravity plan quota** via **`gemini-4-neon-high`** (`census-prefetch`, `neon-shard-1`..`neon-shard-6`, `census-verify-1`, `census-verify-2`, `census-merger`, plus `census-planner` on `gemini-4-carbon-high`).
- **Pre-indexing efficiency (`src/document_indexer.py`):** Offline macOS `PDFKit` indexing partitioned all `6,615` physical pages of the `104` Wave 3 PDFs into `1,749` (`26.4%`) `pages_selected`, `4,039` (`61.1%`) `pages_deferred`, and `827` (`12.5%`) `pages_without_text` inside `DocumentCacheStore`, eliminating redundant raw-PDF extraction inside LLM turns and enabling 100% verbatim `exact_ws` quotation accuracy (`909 / 909`).
- **API-equivalent note:** No separate per-token USD API invoice was incurred on Wave 3 (plan quota execution). For planning comparison, `census-planner` (`plan.json`) projected Wave 3's 54-trial API-equivalent cost at \$4.59 (sliced) / \$21.66 (full) / \$29.76 (conservative).

## 5. Run Timeline & Stop-Condition Audit (With UTC Timestamps)

1. **Wave 1 (`2026-11-14T01:21:51Z`–`03:13:29Z`):** Completed `shard-01`..`shard-03` (`62 / 402` primary trials), halted at the initial \$18/\$20 cap.
2. **Wave 2 continuation (`2026-11-14T13:42:19Z`–`20:38:01Z`):** Completed Step 1 (`sample-prim-01`..`02`, `32` trials), Step 2 (`second-pass-01`..`02`, `40 / 40` independent second pass), and Step 3 `shard-04`..`shard-17` (`254` trials; `348 / 402` cumulative), stopping cleanly before `shard-18` under the \$150.00 continuation cap.
3. **Delivery checkpoint (`2026-11-17T14:02:56Z`):** Reported `348 / 402` primary trials and `40 / 40` second-pass trials; held `shard-18`..`shard-20` (`54` trials) under the \$140.00 batch-launch gate.
4. **Wave 3 completion (`2026-11-21T04:34:15Z`–`2026-11-21T05:44:25Z`):**
   - `census-planner` audited Wave 1/2 (`348` trials, `678` PDFs) and produced the collision-free 6-shard Neon plan (`plan.json`).
   - `census-prefetch` downloaded all `104 / 104` HTTP `200` PDFs (`0` failures, `0` HTTP `429`s, `0` `in_audit == true` trials) and indexed all `6,615` pages offline into `DocumentCacheStore`.
   - `neon-shard-1`..`neon-shard-6` completed all `54 / 54` remaining trials (`73` primary outcomes, `511` primary-pass rows, `909` verbatim citations).
   - `census-verify-1` (`neon-shard-1..3`, `301` rows, `533/533` `exact_ws`) and `census-verify-2` (`neon-shard-4..6`, `210` rows, `376/376` `exact_ws`) independently verified all `54 / 54` trials with `0` errors and `0` warnings.
   - `census-merger` assembled the byte-preserving `402 / 402` corpus (`782` docs, `5,229` rows) and verified all `402 / 402` trials via `src/citation_verifier.py`.
5. **Safety & data-quality invariants verified across all 402 trials:**
   - **In-scope trial count (`== 402`):** Matched (`402 / 598 [67.2%]`; `0` `in_audit == true` trials queried, opened, or recorded).
   - **Download failure rate (`0.0%`):** `0 / 782` download failures; `0` HTTP `429` responses.
   - **Wave 1/2 append-only immutability:** Exact byte-prefix identity verified for all `678` Wave 1/2 `documents.jsonl` rows and all `4,718` Wave 1/2 `locations.jsonl` rows.
