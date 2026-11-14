"""Workflow script executed via run_workflow to run independent dual readings and adjudication."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(WORKSPACES[0]) if WORKSPACES else Path.cwd()
AUDIT_DIR = ROOT / "docs" / "audits" / "hba1c_cohort_reading"
SCRIPTS_DIR = AUDIT_DIR / "scripts"
RECORDS_DIR = AUDIT_DIR / "records"
INTERMEDIATE_DIR = AUDIT_DIR / "intermediate"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compare_and_verify as cv  # noqa: E402
import fetch_records as fr  # noqa: E402
import schema_and_prompts as sp  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_jsonl_by_nct(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                obj = json.loads(line)
                nct = obj.get("nct_id")
                if nct and not obj.get("error"):
                    out[nct] = obj
    return out


async def run_single_reader(nct_id: str, raw_record: dict, slot: int, max_attempts: int = 4):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        prompt = sp.build_reader_prompt(nct_id, raw_record, slot, concise_retry=(attempt >= 2))
        try:
            res = await agent(
                prompt=prompt,
                schema=sp.TRIAL_READING_SCHEMA,
                role=f"R{slot} {nct_id}",
                type_name="hba1c_trial_reader",
            )
            return res, utc_now(), ""
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"[WARN] {nct_id} R{slot} attempt {attempt}/{max_attempts} failed: {last_err}")
            if attempt < max_attempts:
                await asyncio.sleep(3.0 * attempt)
    return None, utc_now(), last_err or "unknown_reader_error"


async def read_trial_pair(nct_id: str):
    try:
        raw_record = sp.load_stored_record(RECORDS_DIR, nct_id)
    except Exception as exc:
        return {
            "nct_id": nct_id,
            "r1": None,
            "r2": None,
            "r1_completed_at_utc": utc_now(),
            "r2_completed_at_utc": utc_now(),
            "error": f"RecordLoadError: {type(exc).__name__}: {exc}",
        }

    pair_results = await parallel(
        [1, 2],
        lambda slot: run_single_reader(nct_id, raw_record, slot),
    )
    (r1, t1, err1), (r2, t2, err2) = pair_results
    combined_err = "; ".join(x for x in (err1, err2) if x)
    return {
        "nct_id": nct_id,
        "r1": r1,
        "r2": r2,
        "r1_completed_at_utc": t1,
        "r2_completed_at_utc": t2,
        "error": combined_err,
    }


async def adjudicate_trial(nct_id: str, disagreements: list, max_attempts: int = 3):
    try:
        raw_record = sp.load_stored_record(RECORDS_DIR, nct_id)
    except Exception as exc:
        return {
            "nct_id": nct_id,
            "disagreed_field_keys": [d["field_key"] for d in disagreements],
            "adjudication_raw": None,
            "completed_at_utc": utc_now(),
            "error": f"RecordLoadError: {type(exc).__name__}: {exc}",
        }

    prompt = sp.build_adjudicator_prompt(nct_id, raw_record, disagreements)
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            res = await agent(
                prompt=prompt,
                schema=sp.ADJUDICATION_SCHEMA,
                role=f"Adj {nct_id}",
                type_name="hba1c_trial_adjudicator",
            )
            return {
                "nct_id": nct_id,
                "disagreed_field_keys": [d["field_key"] for d in disagreements],
                "adjudication_raw": res,
                "completed_at_utc": utc_now(),
                "error": "",
            }
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"[WARN] {nct_id} Adj attempt {attempt}/{max_attempts} failed: {last_err}")
            if attempt < max_attempts:
                await asyncio.sleep(3.0 * attempt)

    return {
        "nct_id": nct_id,
        "disagreed_field_keys": [d["field_key"] for d in disagreements],
        "adjudication_raw": None,
        "completed_at_utc": utc_now(),
        "error": last_err or "unknown_adjudicator_error",
    }


INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
DUAL_PATH = INTERMEDIATE_DIR / "raw_dual_readings.jsonl"
ADJ_PATH = INTERMEDIATE_DIR / "raw_adjudications.jsonl"

limit = int(args.get("limit", 248)) if isinstance(args, dict) else 248
batch_size = int(args.get("batch_size", 10)) if isinstance(args, dict) else 10
target_ncts = fr.NCT_IDS[:limit]

existing_dual = load_jsonl_by_nct(DUAL_PATH)
todo_dual = [n for n in target_ncts if n not in existing_dual]
log(f"Target trials: {len(target_ncts)} | Already dual-read: {len(existing_dual)} | Remaining dual-read: {len(todo_dual)}")

if todo_dual:
    phase("Independent First & Second Readings")
    with DUAL_PATH.open("a", encoding="utf-8") as dual_fp:
        for start in range(0, len(todo_dual), batch_size):
            batch = todo_dual[start : start + batch_size]
            log(f"Starting dual-reading wave {start // batch_size + 1} ({batch[0]} .. {batch[-1]}, n={len(batch)})")
            wave_out = await parallel(batch, read_trial_pair)
            errs = 0
            for item in wave_out:
                dual_fp.write(json.dumps(item, ensure_ascii=False) + "\n")
                if not item.get("error"):
                    existing_dual[item["nct_id"]] = item
                else:
                    errs += 1
            dual_fp.flush()
            log(f"Finished wave {start // batch_size + 1}: ok={len(batch) - errs}, err={errs}, cumulative_ok={len(existing_dual)}")

# Deterministic comparison across completed dual readings
phase("Deterministic Comparison & Third-Reader Adjudication")
existing_adj = load_jsonl_by_nct(ADJ_PATH)
need_adj = []
total_disagreed_fields = 0

for nct in target_ncts:
    pair = existing_dual.get(nct)
    if not pair or pair.get("error"):
        continue
    cmp_res = cv.compare_trial_readings(pair["r1"], pair["r2"])
    total_disagreed_fields += cmp_res["fields_disagreed_count"]
    if cmp_res["fields_disagreed_count"] > 0:
        prev_adj = existing_adj.get(nct)
        if not prev_adj or prev_adj.get("disagreed_field_keys") != cmp_res["disagreed_field_keys"]:
            need_adj.append((nct, cmp_res["disagreements"]))

log(
    f"Comparison complete across {len(existing_dual)}/{len(target_ncts)} trials: "
    f"total disagreed fields={total_disagreed_fields}, trials needing adjudication now={len(need_adj)}"
)

if need_adj:
    adj_batch_size = max(batch_size * 2, 12)
    with ADJ_PATH.open("a", encoding="utf-8") as adj_fp:
        for start in range(0, len(need_adj), adj_batch_size):
            batch = need_adj[start : start + adj_batch_size]
            log(f"Starting adjudication wave {start // adj_batch_size + 1} (n={len(batch)})")
            wave_adj = await parallel(batch, lambda item: adjudicate_trial(item[0], item[1]))
            a_errs = 0
            for item in wave_adj:
                adj_fp.write(json.dumps(item, ensure_ascii=False) + "\n")
                if not item.get("error"):
                    existing_adj[item["nct_id"]] = item
                else:
                    a_errs += 1
            adj_fp.flush()
            log(f"Finished adjudication wave {start // adj_batch_size + 1}: ok={len(batch) - a_errs}, err={a_errs}")

# Quick verification summary on target_ncts
verified_rows = 0
failed_quote_rows = 0
unresolved_fields_total = 0

for nct in target_ncts:
    pair = existing_dual.get(nct)
    if not pair or pair.get("error"):
        continue
    cmp_res = cv.compare_trial_readings(pair["r1"], pair["r2"])
    adj_obj = existing_adj.get(nct, {}).get("adjudication_raw") if cmp_res["fields_disagreed_count"] > 0 else None
    merged = cv.apply_adjudication(pair["r1"], pair["r2"], cmp_res, adj_obj)
    unresolved_fields_total += len(merged["unresolved_field_keys"])
    raw_rec = sp.load_stored_record(RECORDS_DIR, nct)
    v_res = cv.verify_trial_quotes(merged["sheet"], raw_rec)
    if merged["sheet"].get("hba1c_primary_measure_posted") == "no":
        verified_rows += 1
    else:
        for mi, m_stat in v_res["per_measure"].items():
            if m_stat["status"] == "verified":
                verified_rows += 1
            else:
                failed_quote_rows += 1
                log(f"[QUOTE_FAIL] {nct}[{mi}]: {m_stat['failures'][:2]}")

print(
    json.dumps(
        {
            "target_trials": len(target_ncts),
            "dual_read_ok": sum(1 for n in target_ncts if n in existing_dual),
            "total_disagreed_fields": total_disagreed_fields,
            "unresolved_fields_total": unresolved_fields_total,
            "verified_rows": verified_rows,
            "failed_quote_rows": failed_quote_rows,
        },
        indent=2,
    )
)
