#!/usr/bin/env python3
"""Generate method.md documenting exact worker counts, verbatim prompts, model, dates, and reproducibility."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPTS_DIR.parent
INTERMEDIATE_DIR = BASE_DIR / "intermediate"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import schema_and_prompts as sp  # noqa: E402


def load_jsonl_latest(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            nct = obj.get("nct_id")
            if nct and (nct not in out or not obj.get("error")):
                out[nct] = obj
    return out


def main() -> int:
    api_ver = json.loads((BASE_DIR / "api_version.json").read_text(encoding="utf-8"))
    rlog_rows = list(csv.DictReader((BASE_DIR / "retrieval_log.csv").open("r", encoding="utf-8")))
    dual_map = load_jsonl_latest(INTERMEDIATE_DIR / "raw_dual_readings.jsonl")
    adj_map = load_jsonl_latest(INTERMEDIATE_DIR / "raw_adjudications.jsonl")

    ret_times = sorted(r["retrieved_at_utc"] for r in rlog_rows if r.get("retrieved_at_utc"))
    r_times = sorted(
        t
        for v in dual_map.values()
        for t in (v.get("r1_completed_at_utc", ""), v.get("r2_completed_at_utc", ""))
        if t
    )
    a_times = sorted(v.get("completed_at_utc", "") for v in adj_map.values() if v.get("completed_at_utc"))

    n_r1 = sum(1 for v in dual_map.values() if v.get("r1") is not None)
    n_r2 = sum(1 for v in dual_map.values() if v.get("r2") is not None)
    n_adj = sum(1 for v in adj_map.values() if v.get("adjudication_raw") is not None)

    text = f"""# Method: Independent Dual Reading and Adjudication of 248 ClinicalTrials.gov Records

## 1. Execution Dates, Model, and Runtime Environment

- **Dates of execution (UTC)**:
  - Registry retrieval (`fetch_records.py`): `{ret_times[0] if ret_times else 'N/A'}` to `{ret_times[-1] if ret_times else 'N/A'}` (API version endpoint retrieved at `{api_ver.get('retrieved_at_utc', 'N/A')}`).
  - Independent first and second readings (`run_cohort_workflow.py`): `{r_times[0] if r_times else 'N/A'}` to `{r_times[-1] if r_times else 'N/A'}`.
  - Third-reader adjudications (`run_cohort_workflow.py`): `{a_times[0] if a_times else 'N/A'}` to `{a_times[-1] if a_times else 'N/A'}`.
- **ClinicalTrials.gov API version**: REST API v2, `apiVersion = {api_ver.get('apiVersion', '')}`, `dataTimestamp = {api_ver.get('dataTimestamp', '')}`.
- **Reader and adjudicator model**: Gemini 4 Carbon (High) (invoked via isolated single-use workflow agents inheriting the session model).
- **Local Python runtime for retrieval, comparison, verification, and deliverable generation**: Python 3.13.4 on macOS (`curl` 8.7.1 with SecureTransport TLS verification for HTTP retrieval).

## 2. Architecture and Worker Counts

| Stage | Worker Type | Worker Count | Context Isolation & Input Given |
|---|---|---|---|
| 1. Retrieval | Single sequential Python process (`scripts/fetch_records.py`) | **1** worker | Fetched `/api/v2/version` once and all 248 `/api/v2/studies/{{NCT_ID}}` endpoints sequentially with a minimum interval of `0.62` seconds (`<= 1.61` requests/second, strictly under the `2` requests/second ceiling). Stored raw HTTP bodies compressed with gzip under `records/{{NCT_ID}}.json.gz` and logged UTC timestamp, HTTP status, SHA-256, `lastUpdatePostDateStruct.date`, and `resultsFirstPostDateStruct.date` to `retrieval_log.csv`. |
| 2. First reading | Isolated single-use `hba1c_trial_reader` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **{n_r1}** workers (1 trial per worker) | Each worker received ONLY its single trial's rendered stored record (`NCT_i`) and the verbatim coding instructions below, in a fresh conversation context that terminated upon calling `finish`. |
| 3. Second reading | Separate isolated single-use `hba1c_trial_reader` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **{n_r2}** workers (1 trial per worker) | Launched concurrently with Reader 1 for `NCT_i` before any reading for `NCT_i` was written to disk. Received ONLY `NCT_i`'s rendered stored record and the identical coding instructions, with zero access to Reader 1's output or any other trial. |
| 4. Comparison | Deterministic Python comparator (`scripts/compare_and_verify.py::compare_trial_readings`) | **1** deterministic script (0 LLM calls) | Compared Reader 1 and Reader 2 field-by-field by exact canonical equality across trial-level, measure-level, arm-level, analysis-level, baseline-table, and country-site-count fields. |
| 5. Adjudication | Isolated single-use `hba1c_trial_adjudicator` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **{n_adj}** workers (1 trial per worker, invoked **only** where >=1 field differed) | Each adjudicator received ONLY `NCT_i`'s rendered stored record, the coding rules, and the list of differing fields (`r1` vs `r2` values and quotes), choosing `reading_1`, `reading_2`, `custom_verbatim` (with deciding verbatim quote and JSON path), or `unresolved`. Both original readings remain untouched in `readings.jsonl`. |
| 6. Quote verification | Deterministic Python verifier (`scripts/compare_and_verify.py::verify_trial_quotes`) | **1** deterministic script (0 LLM calls) | Resolved every stated JSON path directly against the uncompressed JSON of `records/{{NCT_ID}}.json.gz` and verified exact case-sensitive character-for-character substring presence (`quote_text in str(resolved_value)`). Any failing row is marked `evidence_not_found` without silent repair. |

**Total LLM workers run**: **{n_r1 + n_r2 + n_adj}** isolated single-trial agent invocations ({n_r1} First Readers + {n_r2} Second Readers + {n_adj} Adjudicators). No LLM worker ever read more than one trial in a single context.

## 3. Stored-Record Presentation and Deviations Check

- **Deviations from initial assignment**:
  1. **Git commit and push**: The initial assignment instructed leaving files uncommitted; a subsequent explicit user instruction directed creating branch `hba1c-cohort-reading`, committing `docs/audits/hba1c_cohort_reading` with message `"HbA1c cohort reading (Antigravity output, unreviewed)"`, and pushing to `origin/hba1c-cohort-reading` upon completion.
  2. No deviations were made on data source, rate limiting, trial coverage (all 248 processed), worker independence, single-trial-per-worker context isolation, deterministic comparison, third-reader adjudication, deterministic quote verification, or deliverables.
- **Lossless path-annotated rendering of stored records (`scripts/schema_and_prompts.py::render_record_for_reader`)**:
  - Every reader and adjudicator read exclusively from the stored `records/{{NCT_ID}}.json.gz` file on disk.
  - To prevent array-bracket miscounting on long JSON arrays, prevent JSON backslash double-escaping artifacts, avoid publication titles (`protocolSection.referencesModule`), and keep large multi-arm records (specifically `NCT00328627`, which contains 156 posted outcome measures) within the ~200 KB subagent prompt limit, `render_record_for_reader` deterministically omits non-coding-sheet sections (`resultsSection.adverseEventsModule`, `protocolSection.referencesModule`, `protocolSection.outcomesModule.secondaryOutcomes` and `otherOutcomes`, `resultsSection.participantFlowModule.periods[].dropWithdraws`, and the bodies of non-`PRIMARY` entries in `resultsSection.outcomeMeasuresModule.outcomeMeasures`) and prints every remaining scalar value inside `<<<...>>>` delimiters (verified absent from all 248 records) alongside its exact dot-and-bracket JSON path (`resultsSection.outcomeMeasuresModule.outcomeMeasures[0].timeFrame: <<<...>>>`).
  - Within `resultsSection.outcomeMeasuresModule.outcomeMeasures`, every element with `type == "PRIMARY"` is rendered verbatim in full at its original 0-based index `[i]`, and non-primary indices/types (`SECONDARY`, `OTHER_PRE_SPECIFIED`, `POST_HOC`) are listed by index and type. Within `protocolSection.contactsLocationsModule.locations`, every `locations[i].country` entry is listed verbatim at its index `[i]`. No pre-classification, pre-filtering of HbA1c primary measures, or pre-aggregation of counts was performed prior to the readers.

## 4. Offline Reproduction Without Network Access

From the repository root, all comparison, verification, CSV, JSONL, and summary outputs can be regenerated deterministically from the stored `records/*.json.gz` files and raw worker checkpoints without network access:

```bash
python3 docs/audits/hba1c_cohort_reading/scripts/generate_deliverables.py
python3 docs/audits/hba1c_cohort_reading/scripts/generate_method_md.py
```

## 5. Verbatim Instructions Given to Workers

### 5.1 Reader System Prompt (`hba1c_trial_reader`)

````text
{sp.READER_SYSTEM_PROMPT.strip()}
````

### 5.2 Adjudicator System Prompt (`hba1c_trial_adjudicator`)

````text
{sp.ADJUDICATOR_SYSTEM_PROMPT.strip()}
````

### 5.3 Verbatim Coding Sheet Rules Given to Readers and Adjudicators

````markdown
{sp.CODING_INSTRUCTIONS_BODY.strip()}
````

### 5.4 Reader Prompt Wrapper Template (`build_reader_prompt`)

````text
Read stored ClinicalTrials.gov record {{NCT_ID}} (Independent Reader {{1_or_2}}).
Do NOT call any tools other than `finish`. Read ONLY the stored record below.

{{CODING_INSTRUCTIONS_BODY}}

{{RENDERED_STORED_RECORD}}
````

### 5.5 Adjudicator Prompt Wrapper Template (`build_adjudicator_prompt`)

````text
Adjudicate differing fields for ClinicalTrials.gov record {{NCT_ID}}.
Do NOT call any tools other than `finish`. Re-read ONLY the stored record below and resolve each `field_key` in `DISAGREEMENTS`.

{{CODING_INSTRUCTIONS_BODY}}

=== DISAGREEMENTS TO ADJUDICATE ===
{{DISAGREEMENTS_JSON}}

{{RENDERED_STORED_RECORD}}
````
"""
    (BASE_DIR / "method.md").write_text(text, encoding="utf-8")
    print(f"Generated method.md (R1={n_r1}, R2={n_r2}, Adj={n_adj})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
