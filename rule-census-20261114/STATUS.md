# Rule-Location Census — Status Report (`rule-census-20261114`)

Reported at: `2026-11-17T14:02:56Z` (before launching any new extraction work on `2026-11-17`).

## 1. In-Scope Primary-Pass Trial Progress (Denominator = 402)

From `https://civicsample.com/data/outcomes/index.json` (`598` total rows), **402 / 598 (67.2%)** rows met `(protocol == true or sap == true) and in_audit == false`. Zero (`0`) trials with `in_audit == true` were queried, opened, or recorded.

| Trial State | Count / Denominator | Details |
|---|---|---|
| **Complete** | **348 / 402 (86.6%)** | All ClinicalTrials.gov `primaryOutcomes` (`613` primary outcomes, `4291` primary-pass element rows across all 7 decision-rule elements) recorded in `locations.jsonl`; all `678 / 678 (100.0%)` posted protocol/SAP PDFs downloaded with HTTP `200` in `documents.jsonl`; all extractable-text pages searched (`pages_searched == "all"` and `pages_not_searched == []` on `4291 / 4291 (100.0%)` rows). Includes `55 / 402 (13.7%)` completed trials where one or more elements were classified `UNREADABLE` (`356 / 4291 (8.3%)` primary-pass rows) due to scanned image-only, redacted, or garbled-CMap PDF pages. |
| **Partly done** | **0 / 402 (0.0%)** | Zero started trials have missing primary outcomes, missing decision-rule elements, failed/partial PDF downloads, or unsearched extractable-text page ranges (`pages_not_searched` is `[]` on every row). |
| **Not started** | **54 / 402 (13.4%)** | `shard-18` (`18 / 402 (4.5%)`), `shard-19` (`17 / 402 (4.2%)`), and `shard-20` (`19 / 402 (4.7%)`), held back by the cost-cap stop rules. |
| **Total in scope** | **402 / 402 (100.0%)** | `62 / 402 (15.4%)` completed in Wave 1 (`shard-01`..`shard-03`), `32 / 402 (8.0%)` in Step 1 (`sample-prim-01`..`02`), `254 / 402 (63.2%)` in Step 3 (`shard-04`..`shard-17`), and `54 / 402 (13.4%)` not started (`shard-18`..`shard-20`). |

## 2. 40-Trial Independent Second Pass

- **Did the 40-trial independent second pass run?** **Yes.**
- **How many trials completed?** **40 / 40 (100.0%)** trials in the fixed sample (`0 / 40 (0.0%)` partly done, `0 / 40 (0.0%)` not started), paired **40 / 40 (100.0%)** with completed primary-pass records (`40 / 402 (10.0%)` of all in-scope trials).
- **Execution details:** The 40-trial sample was fixed before Wave 1 by sorting all 402 in-scope `NCT_ID`s ascending by lowercase hex `sha256(NCT_ID)` and taking the first 40. Two fresh blinded workers (`second-pass-01` [`20 / 40 (50.0%)`] and `second-pass-02` [`20 / 40 (50.0%)`]) independently extracted all `61` primary outcomes (`427` second-pass element rows) without access to primary-pass outputs. Exact status consistency between model runs was **383 / 427 (89.7%)** (measures consistency between model runs, not accuracy).

## 3. Total Spend So Far Across All Workers and Coordinator (Since `2026-11-14T01:21:51Z`)

- **How spend was measured:** **Token counts times price.** Antigravity runs on plan quota and does not expose a per-run USD billing page in the workspace. Exact per-turn token counts (`input_tokens`, `cache_read_tokens`, and `output_tokens`) were summed directly from every `PLANNER_RESPONSE` step in `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl` across all **23 / 23 (100.0%)** swarm conversations (`coordinator`, `monitor`, `shard-01`..`shard-17`, `sample-prim-01`..`02`, and `second-pass-01`..`02`) from `2026-11-14T01:21:51Z` through `2026-11-17T14:02:56Z`, and multiplied per turn by published tiered API prices (`<=200k` vs. `>200k` context window per turn).
- **Cumulative logged token usage (`2026-11-14T01:21:51Z` to `2026-11-17T14:02:56Z`):**
  - **Planner turns:** `2,465` total (`260` `coordinator`, `438` `monitor`, `1,767` across the `21` extraction workers)
  - **Uncached input tokens:** `28,108,410` (`5,043,863` `coordinator`, `1,136,662` `monitor`, `21,927,885` workers)
  - **Cached input (`cache_read`) tokens:** `251,723,647` (`28,350,886` `coordinator`, `34,036,840` `monitor`, `189,335,921` workers)
  - **Output tokens:** `4,031,825` (`308,199` `coordinator`, `39,698` `monitor`, `3,683,928` workers)

| Pricing Schedule (`token counts × price`) | Coordinator (`Gemini 4 Carbon (High)`) | Monitor (`Claude Sonnet 4.6 (Thinking)`) | 21 Extraction Workers (`Gemini 4 Carbon (High)`) | Total Spend Since `2026-11-14` | Measured Cost per Completed Primary Trial (`348` trials) |
|---|---|---|---|---|---|
| **Model-matched standard API rates** (Gemini Pro: \$1.25 / \$0.125 / \$10.00 per 1M `<=200k`, \$2.50 / \$0.25 / \$15.00 `>200k`; Claude Sonnet 4.6: \$3.00 / \$0.30 / \$15.00 per 1M `<=200k`) | \$17.80 | \$14.22 | \$107.58 | **\$139.60 USD** | \$0.4011 / trial |
| **Uniform Gemini Pro standard API rates** (\$1.25 / \$0.125 / \$10.00 per 1M `<=200k`, \$2.50 / \$0.25 / \$15.00 `>200k` across all 23 agents) | \$17.80 | \$6.07 | \$107.58 | **\$131.45 USD** | \$0.3777 / trial |
| **Conservative flagship API rates** (\$2.00 / \$0.20 / \$12.00 per 1M `<=200k`, \$4.00 / \$0.40 / \$18.00 `>200k`, used during `2026-11-14` shard gating) | \$27.14 | \$9.56 | \$155.14 | **\$191.83 USD** | \$0.5512 / trial |

> **Why conservative cumulative spend is \$191.83 USD:** Wave 1 (`2026-11-14T01:21:51Z`–`03:13:29Z`, `62` trials) spent \$26.68 (uniform \$1.25) / \$38.49 (conservative \$2.00). At `2026-11-14T13:42:19Z`, continuation instructions specified a hard cap of \$150.00 *"for all further work"*, under which further-work spend stopped after `shard-17` at \$101.79 (uniform \$1.25) / \$148.81 (conservative \$2.00), bringing cumulative spend at `shard-17` completion to \$128.47 (uniform \$1.25) / \$136.62 (model-matched standard) / \$187.30 (conservative \$2.00), plus finalization and `2026-11-17` status-check turns.

## 4. What Stopped the Run (With UTC Timestamps)

1. **Wave 1 initial \$18 / \$20 cost-cap stop (`2026-11-14T03:13:29Z`):** Wave 1 launched three parallel shards (`shard-01`..`shard-03`, `62 / 402 (15.4%)` trials), which finished between `2026-11-14T02:26:55Z` and `2026-11-14T03:13:29Z`. Measured Wave 1 token spend reached \$26.68 USD (uniform \$1.25 schedule) / \$38.49 USD (conservative \$2.00 schedule), triggering the original prompt's \$18 / \$20 cost stop condition before `shard-04`.
2. **Continuation \$150.00 cost-cap stop after `shard-17` (`2026-11-14T20:37:08Z` worker completion; `2026-11-14T20:38:01Z` cap gate):** Resuming at `2026-11-14T13:42:19Z`, the swarm completed Step 1 (`sample-prim-01`..`02`, `32` trials, finished `2026-11-14T15:22:02Z`), Step 2 (`second-pass-01`..`02`, `40` trials, finished `2026-11-14T16:45:21Z`), and Step 3 `shard-04` through `shard-17` (`254` trials, `shard-17` finished `2026-11-14T20:37:08Z`). At `2026-11-14T20:38:01Z`, further-work spend reached \$148.81 USD on the conservative \$2.00 schedule (\$101.79 USD uniform \$1.25); launching `shard-18` (`18` trials, Wave 1 conservative estimate \$10.80) would have pushed further-work conservative spend to \$159.61 USD (`> \$150.00`), so execution stopped cleanly before `shard-18`.
3. **Server restart stopping parked subagents (`2026-11-17T13:45:30Z`):** A host server restart between `2026-11-14T20:50:26Z` and `2026-11-17T13:45:30Z` stopped all idle subagents and background tasks (`[Notice] All your subagents and background tasks have been stopped due to server restart`). Because all shards through `shard-17` had already finished and merged on `2026-11-14`, zero trials were left partly done.
4. **Delivery-pass \$140.00 total-census budget gate (`2026-11-17T14:02:56Z`):** Under the `2026-11-17` budget rule (\$150.00 total cap counting everything already spent; do not launch a batch taking total spend past \$140.00), cumulative spend from `2026-11-14T01:21:51Z` is already **\$139.60 USD** at model-matched standard API rates (\$0.40 below \$140.00, less than 2 trials at \$0.4011/trial), **\$191.83 USD** at conservative \$2.00 rates (`> \$140.00`), and **\$131.45 USD** at uniform \$1.25 rates (where launching 18-trial `shard-18` at \$0.3777–\$0.4159/trial plus coordinator verification/merge overhead would reach ~\$139.8–\$140.4 USD). No new batch was launched, preserving remaining budget headroom under \$150.00 to write and push the final outputs.
5. **Other stop conditions & errors checked (none stopped the run):**
   - **In-scope trial count (`!= 402`):** Not triggered (`402 / 598 (67.2%)` matched `402`).
   - **Download failure rate (`> 10%`):** Not triggered (`0 / 678 (0.0%)` download failures).
   - **ClinicalTrials.gov rate-limiting:** Not triggered (`0` HTTP `429` responses).
   - **PDF text-extraction error handled in-run (`2026-11-14T18:33Z`):** `NCT03323749/SAP_002.pdf` (`shard-08`) emitted unpaired UTF-16 surrogates from macOS `PDFKit`; fixed via UTF-16 surrogate sanitization in the local PDF text extractor without dropping or failing any trial.
