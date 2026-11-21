"""Schema-version-1 artifact family, `run_identity`, contract checker, and atomic writer.

Artifact family written by `scripts/build_feasibility_benchmarks.py`:
  - `run.json`        manifest: `run_id` ending in 10-hex `run_identity`, `generated_at`,
                      `producer` (commit, dirty flag, `source_sha256`), `inputs` (records source
                      and sha256, sponsor rules sha256, curated termination reviews sha256,
                      optional `condition_mesh` sha256), `parameters`, `vocabularies`, `reviews`,
                      and `files` (exact byte sha256 of every sibling file).
  - `summary.json`    every count/metric naming `value`, `numerator`, `denominator`, and
                      `definition`, plus named `denominators` and breakdown `tables`. The browser
                      computes no aggregate.
  - `strata.json`     indication x phase x footprint tier x sponsor class benchmarks + MeSH strata,
                      withholding percentiles below `min_cell_trials` while reporting `n_trials`.
  - `sponsors.json`   read-only `sponsors/` attribution benchmarks keeping `attributed`,
                      `reviewed_excluded`, and `unreviewed` separate (`nct_id`-deduplicated).
  - `trials.jsonl.gz` sorted per-trial operational, retention, sponsor, and termination records
                      compressed deterministically (`gzip.compress(..., mtime=0)`).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from feasibility.benchmarks import (
    ACTIVE_STATUSES,
    DEFAULT_MIN_CELL_TRIALS,
    SPONSOR_REVIEW_STATES,
    build_strata_and_sponsors,
    summarize_cell,
)
from feasibility.metrics import (
    ACCRUAL_RATE_STATUSES,
    COMPLETION_RATE_STATUSES,
    DURATION_STATUSES,
    ENROLLMENT_TYPES,
    FACILITY_BASIS,
    FACILITY_METRIC_STATUSES,
    FLOW_STATUSES,
    FOOTPRINT_TIERS,
    RATE_BASIS,
)
from feasibility.termination import (
    CLASSIFICATION_SOURCES,
    PRIVATE_REVIEW_KEYS,
    SIGNAL_CATEGORIES,
    STOPPED_STATUSES,
    STOPPED_STUDY_RESULTS_STATES,
    TERMINATION_CATEGORIES,
    TERMINATION_RULES_VERSION,
)

SCHEMA_VERSION = 1
METHOD_VERSION = "feasibility-benchmarks-1"

JSON_FILES = ("summary.json", "strata.json", "sponsors.json")
TRIALS_FILE = "trials.jsonl.gz"
FILES = JSON_FILES + (TRIALS_FILE,)

RUN_ID_RE = re.compile(r"^feasibility-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
NCT_RE = re.compile(r"^NCT\d{8}$")

PERCENTILE_BLOCK_KEYS = (
    "realized_primary_duration_days",
    "realized_primary_duration_months",
    "realized_study_duration_months",
    "actual_participants_per_month",
    "actual_participants_per_listed_facility",
    "period1_completion_rate",
)


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def dump_canonical(obj: Any) -> bytes:
    """Canonical compact key-sorted JSON line with trailing newline."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def dump_manifest(obj: Any) -> bytes:
    """Human-readable indented key-sorted JSON for `run.json`."""
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def encode_trials_jsonl_gz(trials: Sequence[dict[str, Any]]) -> bytes:
    """Deterministic `mtime=0` gzipped JSONL over sorted per-trial records."""
    raw = b"".join(dump_canonical(t) for t in trials)
    return gzip.compress(raw, mtime=0)


def decode_trials_jsonl_gz(raw_gz: bytes) -> list[dict[str, Any]]:
    """Decompress and parse `trials.jsonl.gz` lines."""
    text = gzip.decompress(raw_gz).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _count_item(
    item_id: str,
    label: str,
    numerator: int,
    denominator_key: str,
    denominators: dict[str, dict[str, Any]],
    definition: str,
) -> dict[str, Any]:
    """Construct one `summary.json` count object with `value`, `numerator`, `denominator`, `definition`.

    `value` is the exact proportion `round(numerator / den_n, 6)` when `den_n > 0`, and `None`
    with `value_absent_reason = "denominator_is_zero"` when `den_n == 0` (0/0 is undefined and
    never coerced to `0.0`).
    """
    den_n = denominators[denominator_key]["n"]
    item: dict[str, Any] = {
        "id": item_id,
        "label": label,
        "value": round(numerator / den_n, 6) if den_n > 0 else None,
        "numerator": numerator,
        "denominator": denominator_key,
        "denominator_n": den_n,
        "definition": definition,
    }
    if den_n == 0:
        item["value_absent_reason"] = "denominator_is_zero"
    return item


def summarize_trials(
    trials: Sequence[dict[str, Any]],
    *,
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
) -> dict[str, Any]:
    """Build `summary.json` content (without `schema_version` / `run_id`) from `trials`."""
    n_all = len(trials)
    resolved = [
        t for t in trials if t["overall_status"] in ("COMPLETED", "TERMINATED", "WITHDRAWN")
    ]
    stopped = [t for t in trials if t["termination"]["is_stopped_study"]]
    actual_enr = [t for t in trials if t["enrollment"]["type"] == "ACTUAL"]
    flow_ok = [t for t in trials if t["retention"]["flow_status"] == "ok"]

    denominators: dict[str, dict[str, Any]] = {
        "trials": {
            "n": n_all,
            "definition": "All study records included in this feasibility benchmark build.",
        },
        "resolved_trials": {
            "n": len(resolved),
            "definition": "Trials in a terminal operational state (COMPLETED, TERMINATED, or WITHDRAWN).",
        },
        "stopped_trials": {
            "n": len(stopped),
            "definition": "Early-stopped or halted studies with overallStatus in {TERMINATED, WITHDRAWN, SUSPENDED}.",
        },
        "actual_enrollment_trials": {
            "n": len(actual_enr),
            "definition": "Trials whose registered enrollmentInfo.type is ACTUAL (strictly excluding ESTIMATED/ANTICIPATED and MISSING_TYPE).",
        },
        "period1_flow_ok_trials": {
            "n": len(flow_ok),
            "definition": "Trials with posted participantFlowModule carrying numeric Period 1 STARTED and COMPLETED counts across flow groups.",
        },
    }

    n_completed = sum(1 for t in trials if t["overall_status"] == "COMPLETED")
    n_terminated = sum(1 for t in trials if t["overall_status"] == "TERMINATED")
    n_withdrawn = sum(1 for t in trials if t["overall_status"] == "WITHDRAWN")
    n_suspended = sum(1 for t in trials if t["overall_status"] == "SUSPENDED")
    n_active = sum(1 for t in trials if t["overall_status"] in ACTIVE_STATUSES)
    n_realized_prim = sum(
        1 for t in trials if t["durations"]["primary_completion"]["eligible_for_realized_duration"]
    )
    n_realized_study = sum(
        1 for t in trials if t["durations"]["study_completion"]["eligible_for_realized_duration"]
    )
    n_accrual_computed = sum(1 for t in trials if t["accrual"]["rate_status"] == "computed")
    n_facility_computed = sum(
        1 for t in trials if t["accrual"]["facility_metric_status"] == "computed"
    )
    n_retention_computed = sum(
        1 for t in trials if t["retention"]["completion_rate_status"] == "computed"
    )
    n_pre_assign = sum(
        1 for t in trials if t["retention"]["has_pre_assignment_details"] is True
    )
    n_multi_signal = sum(
        1 for t in stopped if t["termination"]["category"] == "multiple_signals_detected"
    )
    n_empty_shell = sum(
        1
        for t in stopped
        if t["termination"]["stopped_study_results_state"] == "empty_results_shell_zero_analysed"
    )

    metrics = [
        _count_item(
            "completed_of_all",
            "Completed trials (share of all trials)",
            n_completed,
            "trials",
            denominators,
            "Trials with overallStatus == COMPLETED over all cohort trials.",
        ),
        _count_item(
            "completed_of_resolved",
            "Completed trials (rate among resolved terminal trials)",
            n_completed,
            "resolved_trials",
            denominators,
            "Trials with overallStatus == COMPLETED over trials in {COMPLETED, TERMINATED, WITHDRAWN}.",
        ),
        _count_item(
            "terminated_of_resolved",
            "Terminated trials (rate among resolved terminal trials)",
            n_terminated,
            "resolved_trials",
            denominators,
            "Trials with overallStatus == TERMINATED over trials in {COMPLETED, TERMINATED, WITHDRAWN}.",
        ),
        _count_item(
            "withdrawn_of_resolved",
            "Withdrawn trials (rate among resolved terminal trials)",
            n_withdrawn,
            "resolved_trials",
            denominators,
            "Trials with overallStatus == WITHDRAWN over trials in {COMPLETED, TERMINATED, WITHDRAWN}.",
        ),
        _count_item(
            "early_stopped_of_all",
            "Stopped studies (TERMINATED, WITHDRAWN, or SUSPENDED)",
            len(stopped),
            "trials",
            denominators,
            "Trials with overallStatus in {TERMINATED, WITHDRAWN, SUSPENDED} over all cohort trials.",
        ),
        _count_item(
            "suspended_of_all",
            "Suspended studies",
            n_suspended,
            "trials",
            denominators,
            "Trials with overallStatus == SUSPENDED over all cohort trials.",
        ),
        _count_item(
            "active_or_recruiting_of_all",
            "Active or recruiting studies",
            n_active,
            "trials",
            denominators,
            "Trials with overallStatus in {NOT_YET_RECRUITING, RECRUITING, ENROLLING_BY_INVITATION, ACTIVE_NOT_RECRUITING}.",
        ),
        _count_item(
            "actual_enrollment_reported",
            "Trials with ACTUAL enrollmentInfo.type",
            len(actual_enr),
            "trials",
            denominators,
            "Trials whose enrollmentInfo.type is ACTUAL (eligible for realized accrual evaluation when duration/sites permit).",
        ),
        _count_item(
            "realized_primary_duration_eligible",
            "Trials with realized start-to-primary-completion duration",
            n_realized_prim,
            "trials",
            denominators,
            "Trials with non-estimated, non-inverted, non-overlapping start and primary completion dates.",
        ),
        _count_item(
            "realized_study_duration_eligible",
            "Trials with realized start-to-study-completion duration",
            n_realized_study,
            "trials",
            denominators,
            "Trials with non-estimated, non-inverted, non-overlapping start and study completion dates.",
        ),
        _count_item(
            "actual_accrual_rate_computed",
            "ACTUAL-enrollment trials with computable monthly accrual velocity",
            n_accrual_computed,
            "actual_enrollment_trials",
            denominators,
            "Trials with ACTUAL enrollment >= 0 and a positive realized calendar duration window.",
        ),
        _count_item(
            "actual_facility_metric_computed",
            "ACTUAL-enrollment trials with >= 1 listed facility",
            n_facility_computed,
            "actual_enrollment_trials",
            denominators,
            "Trials with ACTUAL enrollment >= 0 and at least one location in contactsLocationsModule.locations.",
        ),
        _count_item(
            "period1_retention_computed",
            "Trials with computable Period 1 retention rate",
            n_retention_computed,
            "trials",
            denominators,
            "Trials with posted Period 1 STARTED > 0 and 0 <= COMPLETED <= STARTED.",
        ),
        _count_item(
            "pre_assignment_details_present",
            "Trials posting pre-assignment participant flow details",
            n_pre_assign,
            "trials",
            denominators,
            "Trials with non-empty resultsSection.participantFlowModule.preAssignmentDetails.",
        ),
        _count_item(
            "stopped_multiple_signals_detected",
            "Stopped studies with multiple whyStopped signal categories",
            n_multi_signal,
            "stopped_trials",
            denominators,
            "Stopped studies whose whyStopped text matched two or more distinct signal categories without silent tie-breaking.",
        ),
        _count_item(
            "stopped_empty_results_shells",
            "Stopped studies posting empty zero-analysed primary outcome shells",
            n_empty_shell,
            "stopped_trials",
            denominators,
            "Stopped studies with posted results where primary outcome numeric denominators exist and are all zero.",
        ),
    ]

    enr_c = Counter(t["enrollment"]["type"] for t in trials)
    prim_c = Counter(t["durations"]["primary_completion"]["status"] for t in trials)
    study_c = Counter(t["durations"]["study_completion"]["status"] for t in trials)
    rate_c = Counter(t["accrual"]["rate_status"] for t in trials)
    fac_c = Counter(t["accrual"]["facility_metric_status"] for t in trials)
    tier_c = Counter(t["facilities"]["footprint_tier"] for t in trials)
    term_c = Counter(t["termination"]["category"] for t in stopped)
    tsrc_c = Counter(t["termination"]["classification_source"] for t in stopped)
    sres_c = Counter(t["termination"]["stopped_study_results_state"] for t in stopped)
    flow_c = Counter(t["retention"]["flow_status"] for t in trials)
    comp_c = Counter(t["retention"]["completion_rate_status"] for t in trials)
    spon_c = Counter(t["sponsor"]["review_state"] for t in trials)
    mesh_c = Counter(t["indication"]["condition_mesh"]["state"] for t in trials)

    mesh_states_ordered = (
        "indexed",
        "not_indexed",
        "no_module",
        "no_derived",
        "malformed",
        "not_provided",
    )

    tables: dict[str, dict[str, Any]] = {
        "enrollment_types": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by normalized enrollmentInfo.type.",
            "rows": [
                _count_item(
                    f"enrollment_type:{et}",
                    f"Enrollment type {et}",
                    enr_c.get(et, 0),
                    "trials",
                    denominators,
                    f"Trials with normalized enrollmentInfo.type == {et}.",
                )
                for et in ENROLLMENT_TYPES
            ],
        },
        "primary_duration_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by start-to-primary-completion duration precision/exclusion state.",
            "rows": [
                _count_item(
                    f"primary_duration:{st}",
                    f"Primary completion duration: {st}",
                    prim_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose startDateStruct -> primaryCompletionDateStruct status is {st}.",
                )
                for st in DURATION_STATUSES
            ],
        },
        "study_duration_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by start-to-study-completion duration precision/exclusion state.",
            "rows": [
                _count_item(
                    f"study_duration:{st}",
                    f"Study completion duration: {st}",
                    study_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose startDateStruct -> completionDateStruct status is {st}.",
                )
                for st in DURATION_STATUSES
            ],
        },
        "accrual_rate_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by monthly accrual-velocity eligibility state.",
            "rows": [
                _count_item(
                    f"accrual_rate:{st}",
                    f"Accrual velocity state: {st}",
                    rate_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose accrual.rate_status is {st}.",
                )
                for st in ACCRUAL_RATE_STATUSES
            ],
        },
        "facility_metric_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by participants-per-listed-facility eligibility state.",
            "rows": [
                _count_item(
                    f"facility_metric:{st}",
                    f"Facility metric state: {st}",
                    fac_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose accrual.facility_metric_status is {st}.",
                )
                for st in FACILITY_METRIC_STATUSES
            ],
        },
        "footprint_tiers": {
            "denominator": "trials",
            "definition": "Partition of cohort trials across the 5 listed-facility footprint tiers.",
            "rows": [
                _count_item(
                    f"footprint_tier:{tier}",
                    f"Footprint tier: {tier}",
                    tier_c.get(tier, 0),
                    "trials",
                    denominators,
                    f"Trials in listed-facility footprint tier {tier}.",
                )
                for tier in FOOTPRINT_TIERS
            ],
        },
        "termination_categories": {
            "denominator": "stopped_trials",
            "definition": "Partition of stopped studies (TERMINATED, WITHDRAWN, SUSPENDED) across whyStopped categories.",
            "rows": [
                _count_item(
                    f"termination_category:{cat}",
                    f"Termination category: {cat}",
                    term_c.get(cat, 0),
                    "stopped_trials",
                    denominators,
                    f"Stopped studies classified as {cat} under rules v{TERMINATION_RULES_VERSION} (with curated overrides applied).",
                )
                for cat in TERMINATION_CATEGORIES
            ],
        },
        "termination_classification_sources": {
            "denominator": "stopped_trials",
            "definition": "Partition of stopped studies by classification provenance (deterministic rule vs human review override).",
            "rows": [
                _count_item(
                    f"classification_source:{src}",
                    f"Classification source: {src}",
                    tsrc_c.get(src, 0),
                    "stopped_trials",
                    denominators,
                    f"Stopped studies whose final category came from {src}.",
                )
                for src in CLASSIFICATION_SOURCES
            ],
        },
        "stopped_study_results_states": {
            "denominator": "stopped_trials",
            "definition": "Partition of stopped studies by posted-results availability and primary outcome denominator state.",
            "rows": [
                _count_item(
                    f"stopped_results:{st}",
                    f"Stopped-study results state: {st}",
                    sres_c.get(st, 0),
                    "stopped_trials",
                    denominators,
                    f"Stopped studies with stopped_study_results_state == {st}.",
                )
                for st in STOPPED_STUDY_RESULTS_STATES
            ],
        },
        "period1_flow_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by Period 1 participantFlowModule parsing/availability state.",
            "rows": [
                _count_item(
                    f"period1_flow:{st}",
                    f"Period 1 flow state: {st}",
                    flow_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose retention.flow_status is {st}.",
                )
                for st in FLOW_STATUSES
            ],
        },
        "period1_completion_rate_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by Period 1 completion-rate eligibility/withholding state.",
            "rows": [
                _count_item(
                    f"period1_completion:{st}",
                    f"Period 1 completion rate: {st}",
                    comp_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose retention.completion_rate_status is {st}.",
                )
                for st in COMPLETION_RATE_STATUSES
            ],
        },
        "sponsor_lead_review_states": {
            "denominator": "trials",
            "definition": "Disjoint 3-state partition of lead sponsor literals against curated sponsors/company_aliases.csv.",
            "rows": [
                _count_item(
                    f"sponsor_review_state:{st}",
                    f"Lead sponsor review state: {st}",
                    spon_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose lead sponsor literal is in review state {st} (attributed, reviewed_excluded, and unreviewed are never collapsed).",
                )
                for st in SPONSOR_REVIEW_STATES
            ],
        },
        "condition_mesh_states": {
            "denominator": "trials",
            "definition": "Partition of cohort trials by ClinicalTrials.gov conditionBrowseModule MeSH indexing state.",
            "rows": [
                _count_item(
                    f"condition_mesh:{st}",
                    f"Condition MeSH state: {st}",
                    mesh_c.get(st, 0),
                    "trials",
                    denominators,
                    f"Trials whose condition_mesh.state is {st}.",
                )
                for st in mesh_states_ordered
            ],
        },
    }

    return {
        "method_version": METHOD_VERSION,
        "termination_rules_version": TERMINATION_RULES_VERSION,
        "proxy_metadata": {
            "rate_basis": RATE_BASIS,
            "facility_basis": FACILITY_BASIS,
        },
        "denominators": denominators,
        "metrics": metrics,
        "tables": tables,
        "overall_benchmarks": summarize_cell(trials, min_cell_trials=min_cell_trials),
    }


def run_identity(
    run_manifest: dict[str, Any],
    json_payloads: dict[str, dict[str, Any]],
    trials: Sequence[dict[str, Any]],
) -> str:
    """Compute the 10-hex `run_identity` digest over all published content excluding `run_id`,
    `generated_at`, and `files`.

    Any change to inputs, producer code digest, curated sponsor rules, termination review CSV,
    parameters, summary counts, strata, sponsor benchmarks, or any per-trial field produces a
    distinct `run_identity`. Re-running on identical inputs at a different wall-clock timestamp
    produces the exact same `run_identity`.
    """
    manifest_body = {
        k: v for k, v in run_manifest.items() if k not in ("run_id", "generated_at", "files")
    }
    json_digests = {
        name: sha256_bytes(dump_canonical({k: v for k, v in obj.items() if k != "run_id"}))
        for name, obj in sorted(json_payloads.items())
    }
    trials_digest = sha256_bytes(
        b"".join(dump_canonical({k: v for k, v in t.items() if k != "run_id"}) for t in trials)
    )
    return sha256_bytes(dump_canonical([manifest_body, json_digests, trials_digest]))[:10]


def build_artifact_family(
    *,
    trials: Sequence[dict[str, Any]],
    sponsor_meta: dict[str, Any],
    review_meta: dict[str, Any],
    inputs_meta: dict[str, Any],
    producer_meta: dict[str, Any],
    generated_at: str,
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Assemble all schema-version-1 feasibility objects and their exact serialized byte payloads."""
    date_part = generated_at[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
        raise ValueError(f"generated_at must begin with YYYY-MM-DD, got {generated_at!r}")

    strata_obj, sponsors_obj = build_strata_and_sponsors(
        trials,
        sponsor_meta=sponsor_meta,
        min_cell_trials=min_cell_trials,
    )
    summary_obj = summarize_trials(trials, min_cell_trials=min_cell_trials)

    json_payloads: dict[str, dict[str, Any]] = {
        "summary.json": {"schema_version": SCHEMA_VERSION, **summary_obj},
        "strata.json": {"schema_version": SCHEMA_VERSION, **strata_obj},
        "sponsors.json": {"schema_version": SCHEMA_VERSION, **sponsors_obj},
    }
    stamped_trials_pre = [{"schema_version": SCHEMA_VERSION, **t} for t in trials]

    run_base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scheduled": False,
        "method_version": METHOD_VERSION,
        "termination_rules_version": TERMINATION_RULES_VERSION,
        "producer": producer_meta,
        "inputs": inputs_meta,
        "parameters": {
            "min_cell_trials": min_cell_trials,
            "rate_basis": RATE_BASIS,
            "facility_basis": FACILITY_BASIS,
        },
        "vocabularies": {
            "enrollment_types": list(ENROLLMENT_TYPES),
            "duration_statuses": list(DURATION_STATUSES),
            "accrual_rate_statuses": list(ACCRUAL_RATE_STATUSES),
            "facility_metric_statuses": list(FACILITY_METRIC_STATUSES),
            "footprint_tiers": list(FOOTPRINT_TIERS),
            "flow_statuses": list(FLOW_STATUSES),
            "completion_rate_statuses": list(COMPLETION_RATE_STATUSES),
            "stopped_statuses": list(STOPPED_STATUSES),
            "signal_categories": list(SIGNAL_CATEGORIES),
            "termination_categories": list(TERMINATION_CATEGORIES),
            "classification_sources": list(CLASSIFICATION_SOURCES),
            "stopped_study_results_states": list(STOPPED_STUDY_RESULTS_STATES),
            "sponsor_review_states": list(SPONSOR_REVIEW_STATES),
        },
        "reviews": review_meta,
        "n_trials": len(trials),
    }

    ident = run_identity(run_base, json_payloads, stamped_trials_pre)
    run_id = f"feasibility-{date_part}-{ident}"

    final_json: dict[str, dict[str, Any]] = {}
    raw_bytes: dict[str, bytes] = {}
    for name in JSON_FILES:
        obj = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            **{k: v for k, v in json_payloads[name].items() if k != "schema_version"},
        }
        final_json[name] = obj
        raw_bytes[name] = dump_canonical(obj)

    final_trials = [
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            **{k: v for k, v in t.items() if k != "schema_version"},
        }
        for t in stamped_trials_pre
    ]
    raw_bytes[TRIALS_FILE] = encode_trials_jsonl_gz(final_trials)

    run_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        **{k: v for k, v in run_base.items() if k != "schema_version"},
        "files": {name: sha256_bytes(raw_bytes[name]) for name in FILES},
    }
    raw_bytes["run.json"] = dump_manifest(run_manifest)

    parsed_family: dict[str, Any] = {
        "run.json": run_manifest,
        **final_json,
        TRIALS_FILE: final_trials,
    }
    return parsed_family, raw_bytes


def _find_private_keys(obj: Any, path: str = "") -> list[str]:
    """Recursively scan parsed JSON structures for leaked private review columns."""
    leaks: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else str(k)
            if k in PRIVATE_REVIEW_KEYS:
                leaks.append(sub)
            leaks.extend(_find_private_keys(v, sub))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            leaks.extend(_find_private_keys(item, f"{path}[{idx}]"))
    return leaks


def _check_cell_withholding(cell: dict[str, Any], min_cell_trials: int, where: str) -> list[str]:
    problems: list[str] = []
    n_trials = cell.get("n_trials")
    if not isinstance(n_trials, int) or n_trials < 0:
        return [f"{where}: n_trials must be a non-negative integer"]
    for block_key in PERCENTILE_BLOCK_KEYS:
        block = cell.get(block_key)
        if not isinstance(block, dict):
            problems.append(f"{where}.{block_key}: missing percentile block")
            continue
        n_elig = block.get("n_eligible")
        if not isinstance(n_elig, int) or not (0 <= n_elig <= n_trials):
            problems.append(f"{where}.{block_key}: invalid n_eligible {n_elig!r} for n_trials={n_trials}")
            continue
        must_withhold = n_trials < min_cell_trials or n_elig < min_cell_trials
        pct_vals = [block.get(q) for q in ("p10", "p25", "p50", "p75", "p90")]
        if must_withhold:
            if block.get("withheld") is not True or any(v is not None for v in pct_vals):
                problems.append(
                    f"{where}.{block_key}: cell with n_trials={n_trials}, n_eligible={n_elig} "
                    f"(< {min_cell_trials}) failed to withhold percentiles"
                )
        else:
            if block.get("withheld") is not False or any(
                not isinstance(v, (int, float)) or isinstance(v, bool) for v in pct_vals
            ):
                problems.append(
                    f"{where}.{block_key}: eligible cell (n_trials={n_trials}, n_eligible={n_elig}) "
                    "missing numeric percentiles"
                )
    return problems


def _check_count_object(
    m: Any,
    denominators: dict[str, Any],
    where: str,
) -> list[str]:
    problems: list[str] = []
    if not isinstance(m, dict):
        return [f"{where}: count object is not a dict"]
    for req_key in ("value", "numerator", "denominator", "definition"):
        if req_key not in m:
            problems.append(f"{where}: missing required key {req_key!r}")
    if problems:
        return problems
    den_key = m["denominator"]
    den = denominators.get(den_key)
    if not isinstance(den, dict) or not isinstance(den.get("n"), int):
        problems.append(f"{where}: unknown denominator {den_key!r}")
        return problems
    den_n = den["n"]
    num = m["numerator"]
    if not isinstance(num, int) or isinstance(num, bool) or not (0 <= num <= den_n):
        problems.append(f"{where}: numerator {num!r} outside 0..{den_n}")
    if not isinstance(m["definition"], str) or not m["definition"].strip():
        problems.append(f"{where}: empty definition")
    val = m["value"]
    if den_n == 0:
        if val is not None:
            problems.append(f"{where}: value must be None when denominator is 0, got {val!r}")
    else:
        expected_val = round(num / den_n, 6)
        if not isinstance(val, (int, float)) or isinstance(val, bool) or abs(float(val) - expected_val) > 1e-9:
            problems.append(f"{where}: value {val!r} != expected {expected_val}")
    return problems


def check_contract(
    parsed_family: dict[str, Any],
    raw_bytes: dict[str, bytes] | None = None,
) -> list[str]:
    """Validate manifest hashes, `run_identity`, domain invariants, small-cell withholding,
    and full `summary.json` recount parity. Returns an empty list on a valid build.
    """
    problems: list[str] = []
    run = parsed_family.get("run.json")
    if not isinstance(run, dict):
        return ["run.json is missing or not a JSON object"]

    if run.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"run.json: schema_version {run.get('schema_version')!r} != {SCHEMA_VERSION}")
    if run.get("scheduled") is not False:
        problems.append("run.json: scheduled must be false")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        problems.append(f"run.json: malformed run_id {run_id!r}")

    prod = run.get("producer") or {}
    if not HEX64_RE.fullmatch(str(prod.get("source_sha256"))):
        problems.append("run.json: producer.source_sha256 is missing or not 64-hex")

    inputs = run.get("inputs") or {}
    rec_in = inputs.get("records") or {}
    if not HEX64_RE.fullmatch(str(rec_in.get("sha256"))):
        problems.append("run.json: inputs.records.sha256 is missing or not 64-hex")
    if not HEX64_RE.fullmatch(str((inputs.get("sponsor_rules") or {}).get("sha256"))):
        problems.append("run.json: inputs.sponsor_rules.sha256 is missing or not 64-hex")
    if not HEX64_RE.fullmatch(str((inputs.get("termination_reviews") or {}).get("sha256"))):
        problems.append("run.json: inputs.termination_reviews.sha256 is missing or not 64-hex")

    listed_files = run.get("files")
    if not isinstance(listed_files, dict):
        return problems + ["run.json: files map is missing"]

    present_siblings = set(parsed_family) - {"run.json"}
    for fname in sorted(set(listed_files) ^ present_siblings):
        problems.append(
            f"{fname}: listed in run.json but missing"
            if fname in listed_files
            else f"{fname}: present on disk/memory but not listed in run.json"
        )
    if set(FILES) - present_siblings:
        return problems + [f"missing required family files: {sorted(set(FILES) - present_siblings)}"]

    if raw_bytes is not None:
        for fname in FILES:
            if fname not in raw_bytes:
                problems.append(f"{fname}: raw bytes missing")
                continue
            actual_sha = sha256_bytes(raw_bytes[fname])
            if actual_sha != listed_files.get(fname):
                problems.append(f"{fname}: sha256 mismatch against run.json")

    for jname in JSON_FILES:
        jobj = parsed_family[jname]
        if not isinstance(jobj, dict):
            problems.append(f"{jname}: not a JSON object")
            continue
        if jobj.get("schema_version") != SCHEMA_VERSION or jobj.get("run_id") != run_id:
            problems.append(f"{jname}: schema_version or run_id disagrees with run.json")

    trials = parsed_family[TRIALS_FILE]
    if not isinstance(trials, list):
        return problems + [f"{TRIALS_FILE}: decoded trials is not a list"]

    if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id):
        expected_ident = run_identity(
            run,
            {name: parsed_family[name] for name in JSON_FILES},
            trials,
        )
        if run_id[-10:] != expected_ident:
            problems.append(
                f"run.json: run_id suffix {run_id[-10:]!r} != recomputed run_identity {expected_ident!r}"
            )

    for fname, obj in sorted(parsed_family.items()):
        for leak in _find_private_keys(obj, fname):
            problems.append(f"private review key leaked into artifact: {leak}")

    # ---- Trial-level domain invariants
    nct_ids: list[str] = []
    n_overrides_seen = 0
    for idx, t in enumerate(trials):
        if not isinstance(t, dict):
            problems.append(f"trials[{idx}]: not a JSON object")
            continue
        if t.get("schema_version") != SCHEMA_VERSION or t.get("run_id") != run_id:
            problems.append(f"trials[{idx}]: schema_version or run_id mismatch")
        nct = t.get("nct_id")
        if not isinstance(nct, str) or not NCT_RE.fullmatch(nct):
            problems.append(f"trials[{idx}]: invalid nct_id {nct!r}")
            continue
        nct_ids.append(nct)

        for dur_name in ("primary_completion", "study_completion"):
            d = (t.get("durations") or {}).get(dur_name) or {}
            st = d.get("status")
            if st not in DURATION_STATUSES:
                problems.append(f"{nct}.durations.{dur_name}: invalid status {st!r}")
            if d.get("start_date_type") in ("ESTIMATED", "ANTICIPATED") or d.get("end_date_type") in (
                "ESTIMATED",
                "ANTICIPATED",
            ):
                if d.get("eligible_for_realized_duration") is not False or st not in (
                    "excluded_estimated_or_anticipated_date",
                    "missing_start_or_end_date",
                    "malformed_date",
                ):
                    problems.append(f"{nct}.durations.{dur_name}: ESTIMATED/ANTICIPATED date not excluded")
            if d.get("start_precision") in ("month", "year") or d.get("end_precision") in ("month", "year"):
                if d.get("days") is not None:
                    problems.append(f"{nct}.durations.{dur_name}: partial date fabricated exact days")
                if st == "bounded_partial_precision":
                    if not (
                        isinstance(d.get("min_days"), int)
                        and isinstance(d.get("max_days"), int)
                        and 0 < d["min_days"] <= d["max_days"]
                    ):
                        problems.append(f"{nct}.durations.{dur_name}: invalid partial day bounds")

        enr = t.get("enrollment") or {}
        acc = t.get("accrual") or {}
        if enr.get("type") not in ENROLLMENT_TYPES:
            problems.append(f"{nct}.enrollment: invalid type {enr.get('type')!r}")
        if acc.get("rate_basis") != RATE_BASIS or acc.get("facility_basis") != FACILITY_BASIS:
            problems.append(f"{nct}.accrual: missing or wrong proxy basis metadata")
        if enr.get("type") != "ACTUAL":
            if any(
                acc.get(k) is not None
                for k in (
                    "actual_participants_per_month",
                    "min_rate",
                    "max_rate",
                    "actual_participants_per_listed_facility",
                )
            ):
                problems.append(f"{nct}.accrual: non-ACTUAL enrollment leaked into accrual/facility metrics")

        ret = t.get("retention") or {}
        if ret.get("period1_started") in (None, 0) and ret.get("period1_completion_rate") is not None:
            problems.append(f"{nct}.retention: period1_completion_rate not withheld for zero/missing STARTED")
        if (
            isinstance(ret.get("period1_started"), (int, float))
            and isinstance(ret.get("period1_completed"), (int, float))
            and ret["period1_completed"] > ret["period1_started"]
            and ret.get("period1_completion_rate") is not None
        ):
            problems.append(f"{nct}.retention: period1_completion_rate not withheld when COMPLETED > STARTED")

        term = t.get("termination") or {}
        is_stopped = t.get("overall_status") in STOPPED_STATUSES
        if term.get("is_stopped_study") != is_stopped:
            problems.append(f"{nct}.termination: is_stopped_study disagrees with overall_status")
        if is_stopped:
            if term.get("category") not in TERMINATION_CATEGORIES:
                problems.append(f"{nct}.termination: invalid category {term.get('category')!r}")
            if term.get("stopped_study_results_state") not in STOPPED_STUDY_RESULTS_STATES:
                problems.append(f"{nct}.termination: invalid stopped_study_results_state")
            if term.get("classification_source") not in CLASSIFICATION_SOURCES:
                problems.append(f"{nct}.termination: invalid classification_source")
            sigs = term.get("matched_signals")
            if not isinstance(sigs, list):
                problems.append(f"{nct}.termination: matched_signals must be a list on stopped study")
            elif len(sigs) >= 2 and term.get("classification_source") == "deterministic_rule":
                if term.get("category") != "multiple_signals_detected":
                    problems.append(f"{nct}.termination: multi-signal study did not set multiple_signals_detected")
            if term.get("classification_source") == "human_review_override":
                n_overrides_seen += 1

        spon = t.get("sponsor") or {}
        if spon.get("review_state") not in SPONSOR_REVIEW_STATES:
            problems.append(f"{nct}.sponsor: invalid review_state {spon.get('review_state')!r}")
        if spon.get("review_state") in ("reviewed_excluded", "unreviewed") and spon.get(
            "lead_canonical_companies"
        ):
            problems.append(f"{nct}.sponsor: unattributed review_state carries lead_canonical_companies")

    if nct_ids != sorted(nct_ids) or len(set(nct_ids)) != len(nct_ids):
        problems.append(f"{TRIALS_FILE}: nct_ids must be strictly sorted and unique")
    if run.get("n_trials") != len(trials):
        problems.append(f"run.json: n_trials ({run.get('n_trials')}) != len(trials) ({len(trials)})")
    if n_overrides_seen != (run.get("reviews") or {}).get("n_applied"):
        problems.append(
            f"override count in trials ({n_overrides_seen}) != run.reviews.n_applied "
            f"({(run.get('reviews') or {}).get('n_applied')})"
        )

    # ---- Small-cell withholding checks across strata.json and sponsors.json
    min_cell = int((run.get("parameters") or {}).get("min_cell_trials") or DEFAULT_MIN_CELL_TRIALS)
    strata = parsed_family["strata.json"]
    if isinstance(strata.get("overall"), dict):
        problems.extend(_check_cell_withholding(strata["overall"], min_cell, "strata.overall"))
    for list_key in ("by_indication", "by_phase", "by_footprint_tier", "by_mesh_descriptor", "cross_strata"):
        for idx, cell in enumerate(strata.get(list_key) or []):
            problems.extend(_check_cell_withholding(cell, min_cell, f"strata.{list_key}[{idx}]"))

    sponsors = parsed_family["sponsors.json"]
    lead_state_rows = sponsors.get("by_lead_review_state") or []
    if [r.get("review_state") for r in lead_state_rows] != list(SPONSOR_REVIEW_STATES):
        problems.append("sponsors.json: by_lead_review_state must list attributed, reviewed_excluded, unreviewed separately")
    for list_key in (
        "by_sponsor_class",
        "by_lead_review_state",
        "by_canonical_company_lead",
        "by_canonical_company_any_role",
    ):
        for idx, cell in enumerate(sponsors.get(list_key) or []):
            problems.extend(_check_cell_withholding(cell, min_cell, f"sponsors.{list_key}[{idx}]"))

    # ---- Summary schema & recount parity against trials.jsonl.gz
    summary = parsed_family["summary.json"]
    den = summary.get("denominators") or {}
    for dname, dobj in den.items():
        if not isinstance(dobj, dict) or not isinstance(dobj.get("n"), int) or not dobj.get("definition"):
            problems.append(f"summary.denominators.{dname}: missing integer n or definition")
    seen_metric_ids: set[str] = set()
    for idx, m in enumerate(summary.get("metrics") or []):
        mid = m.get("id") if isinstance(m, dict) else None
        if mid in seen_metric_ids:
            problems.append(f"summary.metrics[{idx}]: duplicate metric id {mid!r}")
        if isinstance(mid, str):
            seen_metric_ids.add(mid)
        problems.extend(_check_count_object(m, den, f"summary.metrics[{idx}]"))

    for tname, tobj in (summary.get("tables") or {}).items():
        if not isinstance(tobj, dict) or not isinstance(tobj.get("rows"), list):
            problems.append(f"summary.tables.{tname}: malformed table")
            continue
        for ridx, row in enumerate(tobj["rows"]):
            problems.extend(_check_count_object(row, den, f"summary.tables.{tname}.rows[{ridx}]"))

    recounted = summarize_trials(
        [{k: v for k, v in t.items() if k not in ("schema_version", "run_id")} for t in trials],
        min_cell_trials=min_cell,
    )
    actual_summary_body = {
        k: v for k, v in summary.items() if k not in ("schema_version", "run_id")
    }
    if dump_canonical(actual_summary_body) != dump_canonical(recounted):
        problems.append("summary.json: counts or benchmarks do not match recount from trials.jsonl.gz")

    return problems


def load_dir(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Load an on-disk feasibility build directory into `(parsed_family, raw_bytes)`."""
    if not path.is_dir():
        raise ValueError(f"{path}: directory does not exist")
    parsed: dict[str, Any] = {}
    raw: dict[str, bytes] = {}
    for entry in sorted(path.iterdir()):
        if not entry.is_file():
            continue
        data = entry.read_bytes()
        raw[entry.name] = data
        if entry.name.endswith(".json"):
            parsed[entry.name] = json.loads(data.decode("utf-8"))
        elif entry.name == TRIALS_FILE:
            parsed[entry.name] = decode_trials_jsonl_gz(data)
        else:
            parsed[entry.name] = None
    return parsed, raw


def replace_refusal(out: Path, expected_names: set[str], *, run_id_prefix: str = "feasibility-") -> str | None:
    """Return a refusal reason if `out` cannot safely be atomically replaced, else `None`."""
    if not out.exists() and not out.is_symlink():
        return None
    if out.is_symlink() or not out.is_dir():
        return f"{out} is not a regular directory"
    entries = list(out.iterdir())
    if not entries:
        return None
    unexpected = sorted(
        p.name for p in entries if p.is_symlink() or not p.is_file() or p.name not in expected_names
    )
    if unexpected:
        return f"{out} holds entries outside this artifact family ({unexpected[:5]})"
    missing = sorted(expected_names - {p.name for p in entries})
    if missing:
        return f"{out} lacks {missing[:5]}, so it is not a complete feasibility artifact family"
    try:
        run_doc = json.loads((out / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{out}/run.json is not readable JSON"
    rid = run_doc.get("run_id") if isinstance(run_doc, dict) else None
    if not (isinstance(rid, str) and rid.startswith(run_id_prefix)):
        return f"{out}/run.json does not name a {run_id_prefix}* run"
    return None


def write_artifact_family(raw_bytes: dict[str, bytes], out: Path) -> None:
    """Atomically stage `raw_bytes` in a sibling directory and swap into `out`."""
    expected = set(FILES) | {"run.json"}
    refusal = replace_refusal(out, expected, run_id_prefix="feasibility-")
    if refusal:
        raise ValueError(f"{refusal}; refusing to replace it")

    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out.name + ".staging.", dir=out.parent))
    try:
        for name, content in sorted(raw_bytes.items()):
            (stage / name).write_bytes(content)
        old = None
        if out.exists():
            old = Path(tempfile.mkdtemp(prefix=out.name + ".replaced.", dir=out.parent))
            out.rename(old / out.name)
        try:
            stage.rename(out)
        except OSError:
            if old is not None:
                (old / out.name).rename(out)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
