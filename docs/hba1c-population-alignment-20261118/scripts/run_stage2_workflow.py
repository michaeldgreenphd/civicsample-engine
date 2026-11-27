"""Dynamic workflow script for Stage 2 independent dual reading & adjudication (201 trials).

Run via `run_workflow(ScriptPath=..., Args={...})`.
Checkpoints raw unmodified reader and adjudicator outputs to:
- docs/audits/hba1c_population_alignment/intermediate/raw_dual_mappings.jsonl
- docs/audits/hba1c_population_alignment/intermediate/raw_adjudications.jsonl
"""

import asyncio
import datetime
import gzip
import json
import pathlib
import sys

WORKSPACE_ROOT = pathlib.Path(WORKSPACES[0])
ALIGN_DIR = WORKSPACE_ROOT / "docs" / "audits" / "hba1c_population_alignment"
SCRIPTS_DIR = ALIGN_DIR / "scripts"
INTERMEDIATE_DIR = ALIGN_DIR / "intermediate"
RECORDS_DIR = WORKSPACE_ROOT / "docs" / "audits" / "hba1c_cohort_reading" / "records"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import stage2_schema_and_prompts as s2  # noqa: E402


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def load_jsonl_by_nct(path: pathlib.Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("status") == "ok":
                out[obj["nct_id"]] = obj
    return out


async def call_single_agent_with_retry(
    prompt_text: str,
    schema_obj: dict,
    role_title: str,
    type_name: str,
    sem: asyncio.Semaphore,
    max_attempts: int = 4,
) -> tuple[dict, str, str]:
    last_err = None
    for attempt in range(1, max_attempts + 1):
        async with sem:
            started = utc_now()
            try:
                res = await agent(
                    prompt=prompt_text,
                    schema=schema_obj,
                    role=role_title,
                    type_name=type_name,
                    workspace="inherit",
                )
                completed = utc_now()
                return res, started, completed
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log(f"[WARN] {role_title} attempt {attempt}/{max_attempts} failed: {exc}")
        if attempt < max_attempts:
            await asyncio.sleep(3.0 * attempt)
    raise RuntimeError(f"{role_title} failed after {max_attempts} attempts: {last_err}")


with open(INTERMEDIATE_DIR / "stage1_trials.json", encoding="utf-8") as f:
    stage1_meta = json.load(f)

all_ncts: list[str] = stage1_meta["nct_ids"]
trials_s1: dict[str, dict] = stage1_meta["trials"]

pilot_ncts = args.get("pilot_ncts") if isinstance(args, dict) else None
max_concurrency = int(args.get("max_concurrency", 16)) if isinstance(args, dict) else 16
target_ncts = list(pilot_ncts) if pilot_ncts else [
    nct for nct in all_ncts if trials_s1[nct]["record_status"] == "ok"
]

dual_checkpoint_path = INTERMEDIATE_DIR / "raw_dual_mappings.jsonl"
adj_checkpoint_path = INTERMEDIATE_DIR / "raw_adjudications.jsonl"
INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)

existing_dual = load_jsonl_by_nct(dual_checkpoint_path)
pending_dual = [nct for nct in target_ncts if nct not in existing_dual]

log(
    f"Stage 2 Dual Reading: target={len(target_ncts)}, "
    f"already_checkpointed={len(target_ncts) - len(pending_dual)}, pending={len(pending_dual)}, "
    f"max_agent_concurrency={max_concurrency}"
)

agent_sem = asyncio.Semaphore(max_concurrency)
file_lock = asyncio.Lock()

phase("Stage 2A: Independent Dual Readings")


async def run_dual_for_trial(nct_id: str) -> dict:
    s1_t = trials_s1[nct_id]
    with gzip.open(RECORDS_DIR / f"{nct_id}.json.gz", "rt", encoding="utf-8") as fp:
        rec = json.load(fp)
    rendered = s2.render_trial_for_stage2(nct_id, rec, s1_t)
    p1 = s2.build_reader_prompt(nct_id, 1, rendered)
    p2 = s2.build_reader_prompt(nct_id, 2, rendered)

    try:
        (r1, r1_start, r1_end), (r2, r2_start, r2_end) = await asyncio.gather(
            call_single_agent_with_retry(
                p1,
                s2.READER_SCHEMA,
                f"Alignment R1 {nct_id}",
                "hba1c_alignment_reader",
                agent_sem,
            ),
            call_single_agent_with_retry(
                p2,
                s2.READER_SCHEMA,
                f"Alignment R2 {nct_id}",
                "hba1c_alignment_reader",
                agent_sem,
            ),
        )
        comp = s2.compare_stage2_readings(s1_t, r1, r2, rec)
        qv1 = s2.verify_stage2_reading_quotes(rec, r1)
        qv2 = s2.verify_stage2_reading_quotes(rec, r2)
        entry = {
            "nct_id": nct_id,
            "status": "ok",
            "r1_started_at": r1_start,
            "r1_completed_at": r1_end,
            "reading_1": r1,
            "r1_quote_failures_count": qv1["quote_failures_count"],
            "r2_started_at": r2_start,
            "r2_completed_at": r2_end,
            "reading_2": r2,
            "r2_quote_failures_count": qv2["quote_failures_count"],
            "disagreed_count": comp["fields_disagreed_count"],
        }
    except Exception as exc:  # noqa: BLE001
        entry = {
            "nct_id": nct_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "failed_at": utc_now(),
        }

    async with file_lock:
        with open(dual_checkpoint_path, "a", encoding="utf-8") as out_f:
            out_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out_f.flush()
    if entry["status"] == "ok":
        log(
            f"[DUAL OK] {nct_id}: disagreed={entry['disagreed_count']}, "
            f"r1_qfail={entry['r1_quote_failures_count']}, r2_qfail={entry['r2_quote_failures_count']}"
        )
    else:
        log(f"[DUAL ERR] {nct_id}: {entry['error']}")
    return entry


if pending_dual:
    await asyncio.gather(*(run_dual_for_trial(nct) for nct in pending_dual))

# Reload dual readings and identify disagreements needing third-reader adjudication
all_dual = load_jsonl_by_nct(dual_checkpoint_path)
existing_adj = load_jsonl_by_nct(adj_checkpoint_path)

need_adj: list[tuple[str, list[dict]]] = []
for nct_id in target_ncts:
    if nct_id not in all_dual or nct_id in existing_adj:
        continue
    d_entry = all_dual[nct_id]
    comp = s2.compare_stage2_readings(trials_s1[nct_id], d_entry["reading_1"], d_entry["reading_2"])
    if comp["fields_disagreed_count"] > 0:
        need_adj.append((nct_id, comp["disagreements"]))

phase("Stage 2B: Third-Reader Adjudication of Disagreements")
log(f"Trials needing adjudication in this run: {len(need_adj)}")


async def run_adj_for_trial(item: tuple[str, list[dict]]) -> dict:
    nct_id, dis_list = item
    s1_t = trials_s1[nct_id]
    with gzip.open(RECORDS_DIR / f"{nct_id}.json.gz", "rt", encoding="utf-8") as fp:
        rec = json.load(fp)
    rendered = s2.render_trial_for_stage2(nct_id, rec, s1_t)
    prompt = s2.build_adjudicator_prompt(nct_id, rendered, dis_list)
    try:
        adj_res, a_start, a_end = await call_single_agent_with_retry(
            prompt,
            s2.ADJUDICATOR_SCHEMA,
            f"Alignment Adjudicator {nct_id}",
            "hba1c_alignment_adjudicator",
            agent_sem,
        )
        entry = {
            "nct_id": nct_id,
            "status": "ok",
            "started_at": a_start,
            "completed_at": a_end,
            "disagreements_count": len(dis_list),
            "adjudication": adj_res,
        }
    except Exception as exc:  # noqa: BLE001
        entry = {
            "nct_id": nct_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "failed_at": utc_now(),
        }

    async with file_lock:
        with open(adj_checkpoint_path, "a", encoding="utf-8") as out_f:
            out_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out_f.flush()
    if entry["status"] == "ok":
        log(f"[ADJ OK] {nct_id}: resolved {len(dis_list)} differing field(s)")
    else:
        log(f"[ADJ ERR] {nct_id}: {entry['error']}")
    return entry


if need_adj:
    await asyncio.gather(*(run_adj_for_trial(it) for it in need_adj))

final_dual = load_jsonl_by_nct(dual_checkpoint_path)
final_adj = load_jsonl_by_nct(adj_checkpoint_path)
ok_target = sum(1 for nct in target_ncts if nct in final_dual)
print(
    json.dumps(
        {
            "target_trials": len(target_ncts),
            "dual_completed_ok": ok_target,
            "adjudicated_ok": sum(1 for nct in target_ncts if nct in final_adj),
        },
        indent=2,
    )
)
