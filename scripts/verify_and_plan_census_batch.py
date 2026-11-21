#!/usr/bin/env python3
"""Offline mechanical census verifier and incremental sha256-keyed batch planner.

Subcommands:
- ``verify``: Enforces the ``in_audit`` holdout firewall and runs
  ``src.citation_verifier.verify_census_corpus`` across ``documents.jsonl`` and
  ``locations.jsonl``.
- ``plan``: Partitions an index/manifest into ``cached_complete``,
  ``unstarted_primary``, ``amended_doc_or_outcome_delta``,
  ``unreadable_recoverable_ocr`` (``image_only_scan`` / ``garbled_cmap``), and
  ``unreadable_terminal_redacted`` (``redacted_cci_ppd``, ``$0`` skip), computes
  pre-launch budget guards (``max_launchable_shards``, ``launch_approved``),
  and emits deterministic ``/swarm`` shard manifests.

Exit codes:
- ``0``: Verification passed or batch plan approved within ``--budget-cap-usd``.
- ``1``: Verification errors found or projected batch spend exceeds budget cap.
- ``2``: Hard ``AuditHoldoutViolationError`` (any ``in_audit == True`` trial).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import AbstractSet, Any, Final, Literal, Mapping, Sequence

# Allow direct execution via `python scripts/verify_and_plan_census_batch.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.citation_verifier import (  # noqa: E402
    AuditHoldoutViolationError,
    CitationVerificationError,
    DirectoryPageTextProvider,
    ELEMENTS,
    UNREADABLE_SUBTYPES,
    VERIFIER_RULES_VERSION,
    enforce_audit_holdout,
    verify_census_corpus,
)
from src.utils import pipeline_commit  # noqa: E402

PLANNER_SCHEMA_VERSION: Final[int] = 1
PLANNER_RULES_VERSION: Final[str] = "1.0.0"

PlanMode = Literal["resume-unstarted", "recover-unreadable", "incremental-delta"]
CostSchedule = Literal["sliced", "full", "conservative"]

DEFAULT_COST_PER_TRIAL_SLICED_USD: Final[float] = 0.085
DEFAULT_COST_PER_TRIAL_FULL_USD: Final[float] = 0.4011
DEFAULT_COST_PER_TRIAL_CONSERVATIVE_USD: Final[float] = 0.5512

_GARBLED_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:garbled|cmap|tounicode|cipher(?:ed)?|caesar|broken\s+font|font\s+encoding|"
    r"corrupt(?:ed)?\s+glyph|math\s+operator\s+dropout)\b",
    re.IGNORECASE,
)
_REDACTED_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:cci|ppd|redact(?:ed|ion)?|confidential\s+commercial|protected\s+personal|"
    r"white[- ]box|blanked\s+stat(?:istical)?|censored)\b",
    re.IGNORECASE,
)
_SCAN_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:image[- ]only|scan(?:ned)?|no\s+extractable\s+text|0\s+text\s+pages?|"
    r"raster(?:ized)?|photocop(?:y|ied))\b",
    re.IGNORECASE,
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a ``.jsonl`` or ``.jsonl.gz`` file into a list of dicts."""
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            if not isinstance(obj, dict):
                raise CitationVerificationError(
                    f"{path}:{line_no}: expected JSON object, got {type(obj).__name__}"
                )
            rows.append(obj)
    return rows


def load_index_rows(path: Path) -> list[dict[str, Any]]:
    """Load cohort index rows from ``outcomes/index.json`` or manifest JSON/JSONL."""
    if path.suffix in {".jsonl", ".gz"} and path.name.endswith((".jsonl", ".jsonl.gz")):
        raw_rows = load_jsonl(path)
    else:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                raw_rows = payload["data"]
            elif isinstance(payload.get("trials"), list):
                raw_rows = payload["trials"]
            else:
                raise CitationVerificationError(
                    f"Index file {path} must contain a list under 'data' or 'trials'."
                )
        else:
            raise CitationVerificationError(
                f"Index file {path} must decode to a JSON object or list."
            )

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            raise CitationVerificationError(f"Index row {idx} in {path} is not a JSON object.")
        if "in_audit" not in row or type(row["in_audit"]) is not bool:
            raise CitationVerificationError(
                f"Index row {idx} ({row.get('nct_id')!r}) in {path} must carry explicit bool in_audit."
            )
        rows.append(row)
    return rows


def extract_audit_nct_ids(index_rows: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Return frozen set of all ``nct_id`` values where ``in_audit is True``."""
    audit_ids: set[str] = set()
    for row in index_rows:
        in_audit = row.get("in_audit")
        nct_id = row.get("nct_id")
        if type(in_audit) is not bool:
            raise CitationVerificationError(
                f"Index row for {nct_id!r} has non-bool in_audit={in_audit!r}"
            )
        if in_audit is True and isinstance(nct_id, str) and nct_id:
            audit_ids.add(nct_id)
    return frozenset(audit_ids)


def compute_doc_sha256_tuple(
    docs: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    """Return deterministic sorted ``((filename, sha256), ...)`` for in-scope docs."""
    pairs: list[tuple[str, str]] = []
    for doc in docs:
        fn = doc.get("filename")
        sha = doc.get("sha256")
        if isinstance(fn, str) and fn and isinstance(sha, str) and sha:
            pairs.append((fn, sha))
    pairs.sort()
    return tuple(pairs)


def compute_doc_set_fingerprint(
    doc_sha256_tuple: Sequence[tuple[str, str]],
) -> str:
    """Compute canonical SHA-256 fingerprint of a trial's sorted document set."""
    canonical = json.dumps(
        [[fn, sha] for fn, sha in sorted(doc_sha256_tuple)],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _load_doc_cache_by_sha256(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    by_sha: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(path):
        sha = row.get("sha256")
        if isinstance(sha, str) and sha:
            by_sha[sha] = row
    return by_sha


def _load_profiles_by_nct(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    by_nct: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(path):
        nct = row.get("nct_id")
        if isinstance(nct, str) and nct:
            by_nct[nct] = row
    return by_nct


def classify_unreadable_subtype(
    docs: Sequence[Mapping[str, Any]],
    locations: Sequence[Mapping[str, Any]],
    doc_cache_by_sha256: Mapping[str, Mapping[str, Any]],
) -> Literal["image_only_scan", "redacted_cci_ppd", "garbled_cmap"]:
    """Deterministically partition an UNREADABLE trial into one of 3 operational buckets.

    Priority order:
    1. Explicit Module 1 metadata (``unreadable_recovery_bucket``,
       ``unreadable_subtype``, ``readability_status``, ``redaction_markers``)
       from ``docs`` or ``doc_cache_by_sha256``:
       - ``garbled_cmap`` -> ``"garbled_cmap"``
       - ``image_only_scan`` -> ``"image_only_scan"``
       - ``redacted_cci_ppd`` (or redaction flags with zero blank pages) ->
         ``"redacted_cci_ppd"``
    2. Deterministic fallback from ``locations`` notes and ``pages_without_text``:
       - Explicit ``unreadable_subtype`` on location rows
       - Garbled CMap / font-encoding hints -> ``"garbled_cmap"``
       - Pure scan (all pages without text, or scan/image-only note) ->
         ``"image_only_scan"``
       - Redaction markers (CCI / PPD / white-box / redacted note) ->
         ``"redacted_cci_ppd"``
       - Default safe fallback when blank pages exist -> ``"image_only_scan"``
    """
    explicit_buckets: set[str] = set()
    has_blank_pages = False
    has_pure_zero_text_doc = False
    has_redaction_markers = False

    for doc in docs:
        sha = doc.get("sha256")
        cache_entry = doc_cache_by_sha256.get(sha, {}) if isinstance(sha, str) else {}

        for src in (doc, cache_entry):
            bucket = src.get("unreadable_recovery_bucket") or src.get("unreadable_subtype")
            if isinstance(bucket, str) and bucket in UNREADABLE_SUBTYPES:
                explicit_buckets.add(bucket)
            status = src.get("readability_status")
            if status == "garbled_cmap":
                explicit_buckets.add("garbled_cmap")
            elif status == "image_only_scan":
                explicit_buckets.add("image_only_scan")
            markers = src.get("redaction_markers")
            if not markers and isinstance(src.get("redaction"), Mapping):
                markers = src["redaction"].get("redaction_flags")
            if isinstance(markers, (list, tuple)) and len(markers) > 0:
                has_redaction_markers = True

        phys = doc.get("physical_pages")
        pwt = doc.get("pages_without_text")
        if isinstance(pwt, list) and len(pwt) > 0:
            has_blank_pages = True
            if type(phys) is int and phys > 0 and len(pwt) == phys:
                has_pure_zero_text_doc = True

    for loc in locations:
        loc_sub = loc.get("unreadable_subtype")
        if isinstance(loc_sub, str) and loc_sub in UNREADABLE_SUBTYPES:
            explicit_buckets.add(loc_sub)

    if "garbled_cmap" in explicit_buckets:
        return "garbled_cmap"
    if "image_only_scan" in explicit_buckets:
        return "image_only_scan"
    if "redacted_cci_ppd" in explicit_buckets:
        return "redacted_cci_ppd"

    unreadable_notes = " ".join(
        str(loc.get("note", ""))
        for loc in locations
        if loc.get("status") == "UNREADABLE"
    )

    if _GARBLED_NOTE_RE.search(unreadable_notes):
        return "garbled_cmap"
    if has_pure_zero_text_doc or _SCAN_NOTE_RE.search(unreadable_notes):
        return "image_only_scan"
    if has_redaction_markers or _REDACTED_NOTE_RE.search(unreadable_notes):
        return "redacted_cci_ppd"
    if has_blank_pages:
        return "image_only_scan"
    return "redacted_cci_ppd"


def _trial_matches_cohort(row: Mapping[str, Any], cohort: str | None) -> bool:
    if cohort is None:
        return True
    cohorts = row.get("cohorts")
    if isinstance(cohorts, list):
        return cohort in cohorts
    row_cohort = row.get("cohort")
    if isinstance(row_cohort, str):
        return row_cohort == cohort
    return True


def _trial_has_in_scope_doc(
    row: Mapping[str, Any],
    current_docs: Sequence[Mapping[str, Any]],
) -> bool:
    if current_docs:
        return True
    return bool(row.get("protocol")) or bool(row.get("sap"))


def _verified_pass_doc_fingerprints(
    locations: Sequence[Mapping[str, Any]],
    existing_docs_by_nct: Mapping[str, Sequence[Mapping[str, Any]]],
    pass_role: str,
) -> dict[tuple[str, str, tuple[tuple[str, str], ...]], bool]:
    """Build cache lookup of ``(nct_id, pass_role, doc_sha256_tuple)`` for completed trials."""
    locs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for loc in locations:
        if loc.get("pass") != pass_role:
            continue
        nct = loc.get("nct_id")
        if isinstance(nct, str) and nct:
            locs_by_nct.setdefault(nct, []).append(loc)

    verified_keys: dict[tuple[str, str, tuple[tuple[str, str], ...]], bool] = {}
    for nct_id, nct_locs in locs_by_nct.items():
        # Verify each outcome index has all 7 elements and zero UNREADABLE rows.
        by_outcome: dict[int, set[str]] = {}
        has_unreadable = False
        for loc in nct_locs:
            idx = loc.get("primary_outcome_index")
            elem = loc.get("element")
            if loc.get("status") == "UNREADABLE":
                has_unreadable = True
            if type(idx) is int and isinstance(elem, str) and elem in ELEMENTS:
                by_outcome.setdefault(idx, set()).add(elem)
        if not by_outcome:
            continue
        all_complete = all(elems == set(ELEMENTS) for elems in by_outcome.values())
        if not all_complete or has_unreadable:
            continue

        doc_tuple = compute_doc_sha256_tuple(existing_docs_by_nct.get(nct_id, ()))
        if not doc_tuple:
            # Fallback to unique (filename, sha256) pairs referenced in citations.
            cit_pairs: set[tuple[str, str]] = set()
            for loc in nct_locs:
                for cit in loc.get("citations", []):
                    if isinstance(cit, Mapping):
                        fn = cit.get("filename")
                        sha = cit.get("sha256")
                        if isinstance(fn, str) and fn and isinstance(sha, str) and sha:
                            cit_pairs.add((fn, sha))
            doc_tuple = tuple(sorted(cit_pairs))

        verified_keys[(nct_id, pass_role, doc_tuple)] = True
    return verified_keys


def plan_incremental_cohort(
    index_rows: Sequence[Mapping[str, Any]],
    *,
    documents: Sequence[Mapping[str, Any]] = (),
    locations: Sequence[Mapping[str, Any]] = (),
    doc_cache_by_sha256: Mapping[str, Mapping[str, Any]] = {},
    profiles_by_nct: Mapping[str, Mapping[str, Any]] = {},
    cohort: str | None = None,
    mode: PlanMode = "incremental-delta",
    pass_role: str | None = None,
    explicit_nct_ids: AbstractSet[str] = frozenset(),
    shard_size: int = 18,
    budget_cap_usd: float = 50.0,
    cost_per_trial_sliced_usd: float = DEFAULT_COST_PER_TRIAL_SLICED_USD,
    cost_per_trial_full_usd: float = DEFAULT_COST_PER_TRIAL_FULL_USD,
    cost_per_trial_conservative_usd: float = DEFAULT_COST_PER_TRIAL_CONSERVATIVE_USD,
    cost_schedule: CostSchedule = "sliced",
    planned_at: str | None = None,
) -> dict[str, Any]:
    """Partition cohort trials and emit budget-gated ``/swarm`` shard manifests.

    Enforces ``in_audit == False`` unconditionally:
    - Excludes ``in_audit == True`` index rows upfront (counted in
      ``excluded_in_audit_count``).
    - Raises ``AuditHoldoutViolationError`` immediately if ANY record in
      ``documents``, ``locations``, ``profiles_by_nct``, or ``explicit_nct_ids``
      references an ``in_audit == True`` trial.
    """
    if type(shard_size) is not int or shard_size < 1:
        raise CitationVerificationError(f"shard_size must be int >= 1, got {shard_size!r}")
    if budget_cap_usd < 0:
        raise CitationVerificationError(f"budget_cap_usd must be >= 0, got {budget_cap_usd!r}")

    effective_pass_role = (
        pass_role
        if pass_role is not None
        else ("ocr_recovery" if mode == "recover-unreadable" else "primary")
    )

    audit_nct_ids = extract_audit_nct_ids(index_rows)

    # Hard firewall scan across all input streams BEFORE any planning.
    for nct_id in sorted(explicit_nct_ids):
        enforce_audit_holdout(nct_id, in_audit=False, audit_nct_ids=audit_nct_ids)
    for doc in documents:
        nct_id = str(doc.get("nct_id", ""))
        enforce_audit_holdout(
            nct_id,
            in_audit=doc.get("in_audit", False),
            audit_nct_ids=audit_nct_ids,
        )
    for loc in locations:
        nct_id = str(loc.get("nct_id", ""))
        enforce_audit_holdout(
            nct_id,
            in_audit=loc.get("in_audit", False),
            audit_nct_ids=audit_nct_ids,
        )
    for nct_id in sorted(profiles_by_nct.keys()):
        enforce_audit_holdout(nct_id, in_audit=False, audit_nct_ids=audit_nct_ids)

    existing_docs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for doc in documents:
        nct = doc.get("nct_id")
        if isinstance(nct, str) and nct:
            existing_docs_by_nct.setdefault(nct, []).append(doc)

    locs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for loc in locations:
        nct = loc.get("nct_id")
        if isinstance(nct, str) and nct:
            locs_by_nct.setdefault(nct, []).append(loc)

    verified_cache = _verified_pass_doc_fingerprints(
        locations, existing_docs_by_nct, pass_role="primary"
    )
    verified_target_pass_cache = _verified_pass_doc_fingerprints(
        locations, existing_docs_by_nct, pass_role=effective_pass_role
    )

    excluded_in_audit_count = 0
    excluded_no_document_count = 0

    partitions: dict[str, list[str]] = {
        "cached_complete": [],
        "unstarted_primary": [],
        "amended_doc_or_outcome_delta": [],
        "unreadable_recoverable_ocr": [],
        "unreadable_terminal_redacted": [],
    }
    unreadable_by_subtype: dict[str, list[str]] = {
        "image_only_scan": [],
        "garbled_cmap": [],
        "redacted_cci_ppd": [],
    }
    doc_fingerprints: dict[str, str] = {}
    unreadable_targets_by_nct: dict[str, list[dict[str, Any]]] = {}
    ocr_prepass_queue: list[dict[str, Any]] = []

    eligible_rows: list[Mapping[str, Any]] = []
    for row in index_rows:
        nct_id = str(row.get("nct_id", ""))
        if not nct_id:
            continue
        if explicit_nct_ids and nct_id not in explicit_nct_ids:
            continue
        if not _trial_matches_cohort(row, cohort):
            continue
        if row["in_audit"] is True:
            excluded_in_audit_count += 1
            continue
        eligible_rows.append(row)

    eligible_rows.sort(key=lambda r: str(r.get("nct_id", "")))

    for row in eligible_rows:
        nct_id = str(row["nct_id"])
        profile = profiles_by_nct.get(nct_id)
        if profile is not None and isinstance(profile.get("documents"), list):
            current_docs: list[Mapping[str, Any]] = list(profile["documents"])
        else:
            current_docs = list(existing_docs_by_nct.get(nct_id, ()))

        if not _trial_has_in_scope_doc(row, current_docs):
            excluded_no_document_count += 1
            continue

        current_doc_tuple = compute_doc_sha256_tuple(current_docs)
        existing_doc_tuple = compute_doc_sha256_tuple(existing_docs_by_nct.get(nct_id, ()))
        doc_fingerprints[nct_id] = compute_doc_set_fingerprint(current_doc_tuple)

        nct_locs = locs_by_nct.get(nct_id, [])
        primary_locs = [loc for loc in nct_locs if loc.get("pass") == "primary"]
        ocr_locs = [loc for loc in nct_locs if loc.get("pass") == "ocr_recovery"]

        # Check for UNREADABLE rows in primary pass not yet resolved by ocr_recovery.
        resolved_ocr_cells = {
            (loc.get("primary_outcome_index"), loc.get("element"))
            for loc in ocr_locs
            if loc.get("status") != "UNREADABLE"
        }
        unresolved_unreadable_rows = [
            loc
            for loc in primary_locs
            if loc.get("status") == "UNREADABLE"
            and (loc.get("primary_outcome_index"), loc.get("element")) not in resolved_ocr_cells
        ]

        # Check if profile itself flags unreadable documents before primary pass.
        profile_unreadable = (
            not primary_locs
            and profile is not None
            and profile.get("readability_summary") in {"image_only_scan", "garbled_cmap"}
        )

        if unresolved_unreadable_rows or profile_unreadable:
            subtype = classify_unreadable_subtype(
                current_docs,
                unresolved_unreadable_rows or primary_locs,
                doc_cache_by_sha256,
            )
            unreadable_by_subtype[subtype].append(nct_id)
            if subtype == "redacted_cci_ppd":
                # Terminal $0 skip: never send pure sponsor-redacted trials to OCR or LLM.
                partitions["unreadable_terminal_redacted"].append(nct_id)
            else:
                partitions["unreadable_recoverable_ocr"].append(nct_id)
                targets = sorted(
                    {
                        (int(loc["primary_outcome_index"]), str(loc["element"]))
                        for loc in unresolved_unreadable_rows
                        if type(loc.get("primary_outcome_index")) is int
                        and isinstance(loc.get("element"), str)
                    }
                )
                unreadable_targets_by_nct[nct_id] = [
                    {"primary_outcome_index": idx, "element": elem}
                    for idx, elem in targets
                ]
                ocr_prepass_queue.append(
                    {
                        "nct_id": nct_id,
                        "unreadable_subtype": subtype,
                        "doc_set_fingerprint": doc_fingerprints[nct_id],
                        "documents": [
                            {
                                "filename": doc.get("filename"),
                                "sha256": doc.get("sha256"),
                                "pages_without_text": doc.get("pages_without_text", []),
                            }
                            for doc in current_docs
                            if isinstance(doc.get("filename"), str)
                        ],
                        "target_elements": unreadable_targets_by_nct[nct_id],
                    }
                )
            continue

        # Check incremental (nct_id, pass_role, doc_sha256_tuple) cache hit.
        # In ocr_recovery mode, trials that already have a complete, verified
        # primary pass with zero UNREADABLE rows and unchanged sha256 need no
        # recovery and belong in cached_complete.
        cache_hit = (
            (nct_id, effective_pass_role, current_doc_tuple) in verified_target_pass_cache
            or (
                effective_pass_role in {"primary", "ocr_recovery"}
                and (nct_id, "primary", current_doc_tuple) in verified_cache
            )
        )
        if cache_hit:
            # Verify primary_outcomes_count didn't grow since last census pass.
            expected_k = row.get("primary_outcomes_count")
            existing_indices = {
                loc.get("primary_outcome_index")
                for loc in primary_locs
                if type(loc.get("primary_outcome_index")) is int
            }
            if type(expected_k) is int and expected_k > 0 and len(existing_indices) != expected_k:
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
            else:
                partitions["cached_complete"].append(nct_id)
            continue

        if primary_locs:
            # Trial has prior primary rows, but doc_sha256_tuple changed or grid incomplete.
            if existing_doc_tuple and current_doc_tuple and existing_doc_tuple != current_doc_tuple:
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
            else:
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
        else:
            partitions["unstarted_primary"].append(nct_id)

    # Select trials to schedule according to requested planning mode.
    if mode == "resume-unstarted":
        scheduled_ncts = list(partitions["unstarted_primary"])
    elif mode == "recover-unreadable":
        scheduled_ncts = list(partitions["unreadable_recoverable_ocr"])
    else:
        # incremental-delta: schedule unstarted + amended docs/outcomes (recoverable OCR
        # stays in its dedicated Stage-1 OCR queue until pre-pass completes).
        scheduled_ncts = sorted(
            partitions["unstarted_primary"] + partitions["amended_doc_or_outcome_delta"]
        )

    unit_cost_by_schedule: dict[CostSchedule, float] = {
        "sliced": cost_per_trial_sliced_usd,
        "full": cost_per_trial_full_usd,
        "conservative": cost_per_trial_conservative_usd,
    }
    gating_unit_cost = unit_cost_by_schedule[cost_schedule]

    shards: list[dict[str, Any]] = []
    cumulative_gating_cost = 0.0
    max_launchable_shards = 0

    for shard_idx, start in enumerate(range(0, len(scheduled_ncts), shard_size), start=1):
        chunk = scheduled_ncts[start : start + shard_size]
        count = len(chunk)
        est_sliced = round(count * cost_per_trial_sliced_usd, 4)
        est_full = round(count * cost_per_trial_full_usd, 4)
        est_conservative = round(count * cost_per_trial_conservative_usd, 4)
        shard_gating_cost = count * gating_unit_cost
        within_cap = (cumulative_gating_cost + shard_gating_cost) <= budget_cap_usd + 1e-9
        if within_cap and max_launchable_shards == shard_idx - 1:
            max_launchable_shards = shard_idx
        cumulative_gating_cost += shard_gating_cost

        shard_entry: dict[str, Any] = {
            "shard_id": f"{mode}-{effective_pass_role}-shard-{shard_idx:02d}",
            "pass_role": effective_pass_role,
            "nct_ids": chunk,
            "trial_count": count,
            "doc_set_fingerprints": {nct: doc_fingerprints[nct] for nct in chunk},
            "estimated_cost_sliced_usd": est_sliced,
            "estimated_cost_full_usd": est_full,
            "estimated_cost_conservative_usd": est_conservative,
            "within_budget_cap": within_cap,
        }
        if effective_pass_role == "ocr_recovery":
            shard_entry["target_elements_by_nct"] = {
                nct: unreadable_targets_by_nct.get(nct, []) for nct in chunk
            }
        shards.append(shard_entry)

    total_scheduled = len(scheduled_ncts)
    total_sliced_usd = round(total_scheduled * cost_per_trial_sliced_usd, 4)
    total_full_usd = round(total_scheduled * cost_per_trial_full_usd, 4)
    total_conservative_usd = round(total_scheduled * cost_per_trial_conservative_usd, 4)
    total_gating_usd = round(total_scheduled * gating_unit_cost, 4)
    launch_approved = total_gating_usd <= budget_cap_usd + 1e-9

    return {
        "planner_schema_version": PLANNER_SCHEMA_VERSION,
        "planner_rules_version": PLANNER_RULES_VERSION,
        "verifier_rules_version": VERIFIER_RULES_VERSION,
        "planned_at": planned_at or datetime.now(timezone.utc).isoformat(),
        "source_pipeline_commit": pipeline_commit(),
        "cohort": cohort,
        "mode": mode,
        "pass_role": effective_pass_role,
        "shard_size": shard_size,
        "excluded_in_audit_count": excluded_in_audit_count,
        "excluded_no_document_count": excluded_no_document_count,
        "partition_counts": {k: len(v) for k, v in partitions.items()},
        "partitions": partitions,
        "unreadable_subtypes": unreadable_by_subtype,
        "unreadable_terminal_redacted_spend_usd": 0.0,
        "ocr_prepass_queue": ocr_prepass_queue,
        "scheduled_trial_count": total_scheduled,
        "cost_projection": {
            "gating_schedule": cost_schedule,
            "budget_cap_usd": round(budget_cap_usd, 4),
            "gating_estimated_usd": total_gating_usd,
            "estimated_cost_sliced_usd": total_sliced_usd,
            "estimated_cost_full_usd": total_full_usd,
            "estimated_cost_conservative_usd": total_conservative_usd,
            "max_launchable_shards": max_launchable_shards,
            "total_shards": len(shards),
            "launch_approved": launch_approved,
        },
        "launch_approved": launch_approved,
        "max_launchable_shards": max_launchable_shards,
        "shards": shards,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _cmd_verify(args: argparse.Namespace) -> int:
    audit_nct_ids: frozenset[str] = frozenset()
    expected_outcomes_by_nct: dict[str, int] | None = None
    if args.index is not None:
        index_rows = load_index_rows(Path(args.index))
        audit_nct_ids = extract_audit_nct_ids(index_rows)
        if args.check_completeness:
            expected_outcomes_by_nct = {
                str(r["nct_id"]): int(r["primary_outcomes_count"])
                for r in index_rows
                if r["in_audit"] is False
                and type(r.get("primary_outcomes_count")) is int
                and int(r["primary_outcomes_count"]) >= 1
            }

    documents = load_jsonl(Path(args.documents))
    locations = load_jsonl(Path(args.locations))

    provider: DirectoryPageTextProvider | None = None
    if args.pages_dir is not None:
        provider = DirectoryPageTextProvider(
            Path(args.pages_dir),
            ocr_dir=Path(args.ocr_dir) if args.ocr_dir is not None else None,
        )

    report = verify_census_corpus(
        documents,
        locations,
        audit_nct_ids=audit_nct_ids,
        page_text_provider=provider,
        pdf_root=Path(args.pdf_root) if args.pdf_root is not None else None,
        expected_outcomes_by_nct=expected_outcomes_by_nct,
        strict_document_keys=not args.allow_extra_document_keys,
        require_exhaustive_for_not_found=not args.allow_non_exhaustive_not_found,
        text_source=args.text_source,
    )

    payload = report.to_dict()
    if args.report_out is not None:
        _write_json(Path(args.report_out), payload)
    print(json.dumps(payload, indent=2, ensure_ascii=True))
    return 0 if report.ok else 1


def _cmd_plan(args: argparse.Namespace) -> int:
    index_rows = load_index_rows(Path(args.index))
    documents = load_jsonl(Path(args.documents)) if args.documents is not None else []
    locations = load_jsonl(Path(args.locations)) if args.locations is not None else []
    doc_cache = _load_doc_cache_by_sha256(
        Path(args.doc_cache) if args.doc_cache is not None else None
    )
    profiles = _load_profiles_by_nct(
        Path(args.profiles) if args.profiles is not None else None
    )

    plan = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=locations,
        doc_cache_by_sha256=doc_cache,
        profiles_by_nct=profiles,
        cohort=args.cohort,
        mode=args.mode,
        pass_role=args.pass_role,
        explicit_nct_ids=frozenset(args.nct_id or ()),
        shard_size=args.shard_size,
        budget_cap_usd=args.budget_cap_usd,
        cost_per_trial_sliced_usd=args.cost_per_trial_sliced_usd,
        cost_per_trial_full_usd=args.cost_per_trial_full_usd,
        cost_per_trial_conservative_usd=args.cost_per_trial_conservative_usd,
        cost_schedule=args.cost_schedule,
    )

    if args.out_plan is not None:
        _write_json(Path(args.out_plan), plan)
    print(json.dumps(plan, indent=2, ensure_ascii=True))
    return 0 if plan["launch_approved"] else 1


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for ``verify`` and ``plan`` subcommands."""
    parser = argparse.ArgumentParser(
        description=(
            "Offline mechanical citation verifier and incremental sha256-keyed "
            "census batch planner."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="Verify census documents.jsonl and locations.jsonl")
    p_verify.add_argument("--index", type=str, default=None, help="Cohort index JSON with in_audit")
    p_verify.add_argument("--documents", type=str, required=True, help="Path to documents.jsonl")
    p_verify.add_argument("--locations", type=str, required=True, help="Path to locations.jsonl")
    p_verify.add_argument("--pages-dir", type=str, default=None, help="Page-text JSON map directory")
    p_verify.add_argument("--ocr-dir", type=str, default=None, help="OCR page-text map directory")
    p_verify.add_argument("--pdf-root", type=str, default=None, help="PDF directory for hash check")
    p_verify.add_argument("--text-source", type=str, default="pdfkit", help="Page text source key")
    p_verify.add_argument(
        "--check-completeness",
        action="store_true",
        help="Verify 7*K primary outcome grid completeness against --index",
    )
    p_verify.add_argument(
        "--allow-non-exhaustive-not-found",
        action="store_true",
        help="Allow scoped NOT_FOUND with explicit disjoint pages_searched/pages_not_searched",
    )
    p_verify.add_argument(
        "--allow-extra-document-keys",
        action="store_true",
        help="Allow Module 1 optional metadata keys in documents.jsonl",
    )
    p_verify.add_argument("--report-out", type=str, default=None, help="Output JSON report path")

    p_plan = sub.add_parser("plan", help="Plan incremental sha256-keyed /swarm batch shards")
    p_plan.add_argument("--index", type=str, required=True, help="Cohort index JSON with in_audit")
    p_plan.add_argument("--cohort", type=str, default=None, help="Cohort slug filter")
    p_plan.add_argument("--documents", type=str, default=None, help="Existing documents.jsonl")
    p_plan.add_argument("--locations", type=str, default=None, help="Existing locations.jsonl")
    p_plan.add_argument(
        "--doc-cache",
        type=str,
        default=None,
        help="Module 1 data/document_index_by_sha256.jsonl",
    )
    p_plan.add_argument(
        "--profiles",
        type=str,
        default=None,
        help="Module 1 data/trial_document_profiles.jsonl",
    )
    p_plan.add_argument(
        "--mode",
        choices=("resume-unstarted", "recover-unreadable", "incremental-delta"),
        default="incremental-delta",
        help="Batch planning mode",
    )
    p_plan.add_argument("--pass-role", type=str, default=None, help="Override target pass role")
    p_plan.add_argument(
        "--nct-id",
        action="append",
        default=None,
        help="Explicit NCT ID filter (repeatable; raises exit 2 if in_audit==True)",
    )
    p_plan.add_argument("--shard-size", type=int, default=18, help="Trials per shard")
    p_plan.add_argument(
        "--budget-cap-usd",
        type=float,
        default=50.0,
        help="Hard cumulative USD budget cap for launch_approved",
    )
    p_plan.add_argument(
        "--cost-per-trial-sliced-usd",
        type=float,
        default=DEFAULT_COST_PER_TRIAL_SLICED_USD,
        help="Projected USD per trial under Module 1 TOC-sliced pages_selected",
    )
    p_plan.add_argument(
        "--cost-per-trial-full-usd",
        type=float,
        default=DEFAULT_COST_PER_TRIAL_FULL_USD,
        help="Projected USD per trial under full-PDF extraction",
    )
    p_plan.add_argument(
        "--cost-per-trial-conservative-usd",
        type=float,
        default=DEFAULT_COST_PER_TRIAL_CONSERVATIVE_USD,
        help="Conservative upper-bound USD per trial",
    )
    p_plan.add_argument(
        "--cost-schedule",
        choices=("sliced", "full", "conservative"),
        default="sliced",
        help="Cost schedule gating launch_approved and max_launchable_shards",
    )
    p_plan.add_argument("--out-plan", type=str, default=None, help="Output JSON batch plan path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning 0 (ok), 1 (gate fail), or 2 (in_audit violation)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "plan":
            return _cmd_plan(args)
        parser.error(f"Unknown command: {args.command}")
        return 1
    except AuditHoldoutViolationError as exc:
        print(f"ERROR [in_audit_holdout]: {exc}", file=sys.stderr)
        return 2
    except CitationVerificationError as exc:
        print(f"ERROR [verification]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
