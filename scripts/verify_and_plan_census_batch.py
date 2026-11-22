#!/usr/bin/env python3
"""Offline mechanical census verifier and incremental sha256-keyed batch planner.

Subcommands:
- ``verify``: Requires ``--index``, enforces strict ASCII ``^NCT[0-9]{8}$`` IDs
  and the ``in_audit`` holdout firewall across ``--index``, ``--documents``,
  ``--locations``, and page-map directories, and runs
  ``src.citation_verifier.verify_census_corpus``.
- ``plan``: Requires ``--index``, enforces strict ASCII ``^NCT[0-9]{8}$`` IDs
  and the ``in_audit`` holdout firewall across all inputs, partitions eligible
  in-scope trials into ``cached_complete``, ``unstarted_primary``,
  ``amended_doc_or_outcome_delta``, ``unreadable_recoverable_ocr``
  (``image_only_scan`` / ``garbled_cmap``), and
  ``unreadable_terminal_redacted`` (``redacted_cci_ppd``, ``$0`` skip), computes
  pre-launch budget guards (``max_launchable_shards``, ``launch_approved``),
  and emits deterministic ``/swarm`` shard manifests.

Runtime dependencies:
- Python 3.11+ standard library (``argparse``, ``datetime``, ``gzip``,
  ``hashlib``, ``json``, ``pathlib``, ``re``, ``sys``, ``typing``) plus
  ``src.citation_verifier`` and ``src.utils.pipeline_commit`` (which invokes
  ``git`` via ``subprocess`` when ``GITHUB_SHA`` is unset). Performs zero
  network calls.

Exit codes:
- ``0``: Verification passed or batch plan approved within ``--budget-cap-usd``.
- ``1``: Verification errors found, schema/outcome-count contract failure, or
  projected batch spend exceeds budget cap.
- ``2``: CLI usage error (e.g. missing ``--index``), strict NCT ID failure
  (``InvalidNctIdError``), ``in_audit`` / unlisted-trial firewall refusal
  (``AuditHoldoutViolationError``), or ``plan --nct-id`` scope refusal
  (``PlanScopeError``).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
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
    InvalidNctIdError,
    UNREADABLE_SUBTYPES,
    VERIFIER_RULES_VERSION,
    enforce_audit_holdout,
    validate_strict_nct_id,
    verify_census_corpus,
)
from src.utils import pipeline_commit  # noqa: E402

PLANNER_SCHEMA_VERSION: Final[int] = 1
PLANNER_RULES_VERSION: Final[str] = "1.1.0"

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


class PlanScopeError(CitationVerificationError):
    """Raised when an explicit ``--nct-id`` target falls outside the selected cohort."""


def _is_non_empty_str(value: Any) -> bool:
    return type(value) is str and len(value.strip()) > 0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a ``.jsonl`` or ``.jsonl.gz`` file into a list of dicts."""
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict[str, Any]] = []
    try:
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
    except json.JSONDecodeError as exc:
        raise CitationVerificationError(f"Malformed JSON in {path}: {exc}") from exc
    return rows


def _partition_index_firewall(
    index_rows: Sequence[Mapping[str, Any]],
    *,
    source_label: str = "index_rows",
) -> tuple[frozenset[str], dict[str, Mapping[str, Any]], bool]:
    """Validate every index row's strict NCT ID and explicit ``bool`` ``in_audit`` flag.

    Returns ``(audit_nct_ids, in_scope_by_nct, allow_empty_audit_set)``.
    """
    if len(index_rows) == 0:
        raise AuditHoldoutViolationError(
            f"{source_label} must contain at least one trial row."
        )

    audit_ids: set[str] = set()
    in_scope_by_nct: dict[str, Mapping[str, Any]] = {}
    seen_ncts: set[str] = set()

    for idx, row in enumerate(index_rows):
        if not isinstance(row, Mapping):
            raise CitationVerificationError(
                f"{source_label}[{idx}] is not a JSON object ({type(row).__name__})."
            )
        # F2: Strict ASCII ^NCT[0-9]{8}$ check FIRST before any audit check.
        nct_id = validate_strict_nct_id(
            row.get("nct_id"), context=f"{source_label}[{idx}].nct_id"
        )
        if nct_id in seen_ncts:
            raise AuditHoldoutViolationError(
                f"Duplicate nct_id {nct_id!r} in {source_label} at row {idx}."
            )
        seen_ncts.add(nct_id)

        if "in_audit" not in row or type(row["in_audit"]) is not bool:
            raise AuditHoldoutViolationError(
                f"{source_label}[{idx}] ({nct_id!r}) must carry an explicit bool in_audit, "
                f"got {row.get('in_audit', '<missing>')!r}."
            )
        if row["in_audit"] is True:
            audit_ids.add(nct_id)
        else:
            in_scope_by_nct[nct_id] = row

    allow_empty_audit_set = (
        len(audit_ids) == 0
        and len(in_scope_by_nct) == len(index_rows)
        and len(index_rows) > 0
    )
    return frozenset(audit_ids), in_scope_by_nct, allow_empty_audit_set


def load_index_rows(path: Path) -> list[dict[str, Any]]:
    """Load cohort index rows from ``outcomes/index.json``, ``outcomes_bundles/index.json``, or manifest JSON/JSONL."""
    if path.suffix in {".jsonl", ".gz"} and path.name.endswith((".jsonl", ".jsonl.gz")):
        raw_rows = load_jsonl(path)
    else:
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise CitationVerificationError(f"Malformed JSON in {path}: {exc}") from exc
        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                raw_rows = payload["data"]
            elif isinstance(payload.get("rows"), list):
                raw_rows = payload["rows"]
            elif isinstance(payload.get("trials"), list):
                raw_rows = payload["trials"]
            else:
                raise CitationVerificationError(
                    f"Index file {path} must contain a list under 'data', 'rows', or 'trials'."
                )
        else:
            raise CitationVerificationError(
                f"Index file {path} must decode to a JSON object or list."
            )

    for idx, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            raise CitationVerificationError(f"Index row {idx} in {path} is not a JSON object.")

    _partition_index_firewall(raw_rows, source_label=str(path))
    return list(raw_rows)


def _require_expected_primary_outcomes(
    row: Mapping[str, Any],
    *,
    context: str = "index row",
) -> int:
    """Require a strict ``int >= 1`` expected primary outcome count on ``row``."""
    nct_label = row.get("nct_id")
    has_primary_count = "primary_outcomes_count" in row
    has_n_primary = "n_primary_outcomes" in row
    if not has_primary_count and not has_n_primary:
        raise CitationVerificationError(
            f"{context} ({nct_label!r}) is missing expected primary outcome count "
            f"('primary_outcomes_count' or 'n_primary_outcomes')."
        )

    val_primary: int | None = None
    val_n: int | None = None
    if has_primary_count:
        raw_p = row["primary_outcomes_count"]
        if type(raw_p) is not int or raw_p < 1:
            raise CitationVerificationError(
                f"{context} ({nct_label!r}) has invalid primary_outcomes_count={raw_p!r}; "
                f"expected strict int >= 1."
            )
        val_primary = raw_p

    if has_n_primary:
        raw_n = row["n_primary_outcomes"]
        if type(raw_n) is not int or raw_n < 1:
            raise CitationVerificationError(
                f"{context} ({nct_label!r}) has invalid n_primary_outcomes={raw_n!r}; "
                f"expected strict int >= 1."
            )
        val_n = raw_n

    if val_primary is not None and val_n is not None and val_primary != val_n:
        raise CitationVerificationError(
            f"{context} ({nct_label!r}) has conflicting primary_outcomes_count={val_primary} "
            f"!= n_primary_outcomes={val_n}."
        )
    return val_primary if val_primary is not None else int(val_n)  # type: ignore[arg-type]


def _validate_index_doc_flags(row: Mapping[str, Any], *, context: str) -> bool:
    """Validate optional ``protocol`` / ``sap`` flags on an index row are strict bools."""
    nct_label = row.get("nct_id")
    has_doc = False
    for flag_key in ("protocol", "sap"):
        if flag_key in row and row[flag_key] is not None:
            flag_val = row[flag_key]
            if type(flag_val) is not bool:
                raise CitationVerificationError(
                    f"{context} ({nct_label!r}) has non-bool {flag_key}={flag_val!r}."
                )
            if flag_val is True:
                has_doc = True
    return has_doc


def extract_audit_nct_ids(index_rows: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Return frozen set of all ``nct_id`` values where ``in_audit is True``."""
    audit_ids, _, _ = _partition_index_firewall(index_rows, source_label="index_rows")
    return audit_ids


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


def _validate_doc_cache_entry(
    row: Mapping[str, Any],
    *,
    context: str,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str],
    allow_empty_audit_set: bool,
) -> None:
    """Validate ``nct_id``, ``nct_ids``, and ``in_audit`` on a doc-cache entry before use."""
    if "nct_id" in row:
        checked_nct = validate_strict_nct_id(row["nct_id"], context=f"{context}.nct_id")
    else:
        checked_nct = None

    checked_nct_list: list[str] = []
    if "nct_ids" in row:
        raw_list = row["nct_ids"]
        if not isinstance(raw_list, list):
            raise CitationVerificationError(
                f"{context}.nct_ids must be a list, got {type(raw_list).__name__}."
            )
        for idx, item in enumerate(raw_list):
            checked_nct_list.append(
                validate_strict_nct_id(item, context=f"{context}.nct_ids[{idx}]")
            )

    if "in_audit" in row:
        flag = row["in_audit"]
        if type(flag) is not bool:
            raise AuditHoldoutViolationError(
                f"{context} carries non-bool in_audit={flag!r}."
            )
        if flag is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: {context} carries in_audit=True."
            )

    for target_nct in ([checked_nct] if checked_nct is not None else []) + checked_nct_list:
        enforce_audit_holdout(
            target_nct,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )


def _load_doc_cache_by_sha256(
    path: Path | None,
    *,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str],
    allow_empty_audit_set: bool,
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    by_sha: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(load_jsonl(path)):
        _validate_doc_cache_entry(
            row,
            context=f"{path}[{idx}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        sha = row.get("sha256")
        if isinstance(sha, str) and sha:
            by_sha[sha] = row
    return by_sha


def _validate_profile_entry(
    key_nct: Any,
    row: Mapping[str, Any],
    *,
    context: str,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str],
    allow_empty_audit_set: bool,
) -> str:
    checked_key = validate_strict_nct_id(key_nct, context=f"{context}.nct_id")
    if "nct_id" in row:
        checked_row = validate_strict_nct_id(row["nct_id"], context=f"{context}.row.nct_id")
        if checked_row != checked_key:
            raise CitationVerificationError(
                f"{context} key {checked_key!r} disagrees with row nct_id={checked_row!r}."
            )
    if "in_audit" in row:
        flag = row["in_audit"]
        if type(flag) is not bool:
            raise AuditHoldoutViolationError(
                f"{context} carries non-bool in_audit={flag!r}."
            )
        if flag is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: {context} carries in_audit=True."
            )
    enforce_audit_holdout(
        checked_key,
        in_audit=False,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    nested_docs = row.get("documents")
    if isinstance(nested_docs, list):
        for doc_idx, doc in enumerate(nested_docs):
            if not isinstance(doc, Mapping):
                raise CitationVerificationError(
                    f"{context}.documents[{doc_idx}] must be an object."
                )
            if "nct_id" in doc:
                doc_nct = validate_strict_nct_id(
                    doc["nct_id"], context=f"{context}.documents[{doc_idx}].nct_id"
                )
                if doc_nct != checked_key:
                    raise CitationVerificationError(
                        f"{context}.documents[{doc_idx}] declares nct_id={doc_nct!r}, "
                        f"expected {checked_key!r}."
                    )
            if "in_audit" in doc:
                d_flag = doc["in_audit"]
                if type(d_flag) is not bool or d_flag is True:
                    raise AuditHoldoutViolationError(
                        f"Audit holdout violation in {context}.documents[{doc_idx}]: "
                        f"in_audit={d_flag!r}."
                    )
    return checked_key


def _load_profiles_by_nct(
    path: Path | None,
    *,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str],
    allow_empty_audit_set: bool,
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    by_nct: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(load_jsonl(path)):
        checked_nct = _validate_profile_entry(
            row.get("nct_id"),
            row,
            context=f"{path}[{idx}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        by_nct[checked_nct] = row
    return by_nct


def _validate_document_provenance_for_plan(doc: Mapping[str, Any], *, context: str) -> None:
    """Enforce F6 provenance stamps (``model_id``, ``profile_version``) on planner document rows."""
    for field in ("model_id", "profile_version"):
        if field not in doc or not _is_non_empty_str(doc.get(field)):
            raise CitationVerificationError(
                f"{context} ({doc.get('nct_id')!r}) requires non-empty string {field!r}, "
                f"got {doc.get(field, '<missing>')!r}."
            )


def _validate_location_provenance_for_plan(loc: Mapping[str, Any], *, context: str) -> None:
    """Enforce F6 provenance stamps (``model_id``, ``prompt_version``) on planner location rows."""
    for field in ("model_id", "prompt_version"):
        if field not in loc or not _is_non_empty_str(loc.get(field)):
            raise CitationVerificationError(
                f"{context} ({loc.get('nct_id')!r}) requires non-empty string {field!r}, "
                f"got {loc.get(field, '<missing>')!r}."
            )


def classify_unreadable_subtype(
    docs: Sequence[Mapping[str, Any]],
    locations: Sequence[Mapping[str, Any]],
    doc_cache_by_sha256: Mapping[str, Mapping[str, Any]],
) -> Literal["image_only_scan", "redacted_cci_ppd", "garbled_cmap"]:
    """Deterministically partition an UNREADABLE trial into one of 3 operational buckets."""
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
        return any(type(c) is str and c == cohort for c in cohorts)
    row_cohort = row.get("cohort")
    if type(row_cohort) is str:
        return row_cohort == cohort
    return False


def _trial_has_in_scope_doc(
    row: Mapping[str, Any],
    current_docs: Sequence[Mapping[str, Any]],
) -> bool:
    has_flag_doc = _validate_index_doc_flags(row, context="plan index row")
    if current_docs:
        return True
    return has_flag_doc


def _resolve_effective_cells(
    nct_locs: Sequence[Mapping[str, Any]],
) -> dict[tuple[int, str], Mapping[str, Any]]:
    """Resolve effective per-cell rows in precedence order: primary -> ocr_recovery -> adjudication."""
    effective: dict[tuple[int, str], Mapping[str, Any]] = {}
    for pass_name in ("primary", "ocr_recovery", "adjudication"):
        for loc in nct_locs:
            if loc.get("pass") != pass_name:
                continue
            idx = loc.get("primary_outcome_index")
            elem = loc.get("element")
            if type(idx) is int and isinstance(elem, str) and elem in ELEMENTS:
                effective[(idx, elem)] = loc
    return effective


def _verified_pass_doc_fingerprints(
    locations: Sequence[Mapping[str, Any]],
    existing_docs_by_nct: Mapping[str, Sequence[Mapping[str, Any]]],
    pass_role: str,
) -> dict[tuple[str, str, tuple[tuple[str, str], ...]], bool]:
    """Build cache lookup of ``(nct_id, pass_role, doc_sha256_tuple)`` for completed trials."""
    all_locs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    locs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for loc in locations:
        nct = loc.get("nct_id")
        if not isinstance(nct, str) or not nct:
            continue
        all_locs_by_nct.setdefault(nct, []).append(loc)
        if loc.get("pass") == pass_role:
            locs_by_nct.setdefault(nct, []).append(loc)

    verified_keys: dict[tuple[str, str, tuple[tuple[str, str], ...]], bool] = {}
    for nct_id, nct_locs in locs_by_nct.items():
        by_outcome: dict[int, set[str]] = {}
        for loc in nct_locs:
            idx = loc.get("primary_outcome_index")
            elem = loc.get("element")
            if type(idx) is int and isinstance(elem, str) and elem in ELEMENTS:
                by_outcome.setdefault(idx, set()).add(elem)
        if not by_outcome:
            continue
        all_complete = all(elems == set(ELEMENTS) for elems in by_outcome.values())
        if not all_complete:
            continue

        if pass_role == "primary":
            effective_cells = _resolve_effective_cells(all_locs_by_nct.get(nct_id, ()))
            has_unreadable = any(
                cell.get("status") == "UNREADABLE" for cell in effective_cells.values()
            )
            citation_source_locs: Sequence[Mapping[str, Any]] = list(effective_cells.values())
        else:
            has_unreadable = any(loc.get("status") == "UNREADABLE" for loc in nct_locs)
            citation_source_locs = nct_locs

        if has_unreadable:
            continue

        doc_tuple = compute_doc_sha256_tuple(existing_docs_by_nct.get(nct_id, ()))
        if not doc_tuple:
            cit_pairs: set[tuple[str, str]] = set()
            for loc in citation_source_locs:
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

    Enforces strict ASCII NCT IDs, ``in_audit == False``, in-scope membership,
    ``expected_k >= 1``, and provenance stamps across all input streams.
    """
    if type(shard_size) is not int or shard_size < 1:
        raise CitationVerificationError(f"shard_size must be int >= 1, got {shard_size!r}")
    for cost_label, cost_val in (
        ("budget_cap_usd", budget_cap_usd),
        ("cost_per_trial_sliced_usd", cost_per_trial_sliced_usd),
        ("cost_per_trial_full_usd", cost_per_trial_full_usd),
        ("cost_per_trial_conservative_usd", cost_per_trial_conservative_usd),
    ):
        if (
            type(cost_val) is bool
            or not isinstance(cost_val, (int, float))
            or not math.isfinite(float(cost_val))
            or cost_val < 0
        ):
            raise CitationVerificationError(
                f"{cost_label} must be a finite non-negative number, got {cost_val!r}"
            )
    if isinstance(explicit_nct_ids, (str, bytes, Mapping)) or not isinstance(
        explicit_nct_ids, AbstractSet
    ):
        raise AuditHoldoutViolationError(
            f"explicit_nct_ids must be an AbstractSet[str], got {type(explicit_nct_ids).__name__}."
        )

    effective_pass_role = (
        pass_role
        if pass_role is not None
        else ("ocr_recovery" if mode == "recover-unreadable" else "primary")
    )

    # Step 1 (F2): Validate strict NCT IDs across explicit_nct_ids and side streams FIRST,
    # alongside index_rows validation.
    checked_explicit_ids: set[str] = set()
    for raw_nct in explicit_nct_ids:
        checked_explicit_ids.add(validate_strict_nct_id(raw_nct, context="explicit_nct_ids"))

    audit_nct_ids, in_scope_by_nct, allow_empty_audit_set = _partition_index_firewall(
        index_rows, source_label="index_rows"
    )
    in_scope_nct_ids: frozenset[str] = frozenset(in_scope_by_nct.keys())

    for doc_idx, doc in enumerate(documents):
        if not isinstance(doc, Mapping):
            raise CitationVerificationError(
                f"documents[{doc_idx}] must be a mapping, got {type(doc).__name__}."
            )
        validate_strict_nct_id(doc.get("nct_id"), context=f"documents[{doc_idx}].nct_id")
    for loc_idx, loc in enumerate(locations):
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"locations[{loc_idx}] must be a mapping, got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{loc_idx}].nct_id")

    # Step 2 (F1 & F3): Hard firewall + scope checks across explicit_nct_ids and all side streams.
    for nct_id in sorted(checked_explicit_ids):
        enforce_audit_holdout(
            nct_id,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        if not _trial_matches_cohort(in_scope_by_nct[nct_id], cohort):
            raise PlanScopeError(
                f"Requested --nct-id {nct_id!r} does not belong to cohort {cohort!r}."
            )

    for doc_idx, doc in enumerate(documents):
        doc_nct = doc["nct_id"]
        if "in_audit" in doc:
            d_flag = doc["in_audit"]
            if type(d_flag) is not bool or d_flag is True:
                raise AuditHoldoutViolationError(
                    f"documents[{doc_idx}] ({doc_nct!r}) carries forbidden in_audit={d_flag!r}."
                )
        enforce_audit_holdout(
            doc_nct,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_document_provenance_for_plan(doc, context=f"documents[{doc_idx}]")

    for loc_idx, loc in enumerate(locations):
        loc_nct = loc["nct_id"]
        if "in_audit" in loc:
            l_flag = loc["in_audit"]
            if type(l_flag) is not bool or l_flag is True:
                raise AuditHoldoutViolationError(
                    f"locations[{loc_idx}] ({loc_nct!r}) carries forbidden in_audit={l_flag!r}."
                )
        enforce_audit_holdout(
            loc_nct,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_location_provenance_for_plan(loc, context=f"locations[{loc_idx}]")

    for sha_key, cache_row in doc_cache_by_sha256.items():
        if not isinstance(cache_row, Mapping):
            raise CitationVerificationError(
                f"doc_cache_by_sha256[{sha_key!r}] must be a mapping."
            )
        _validate_doc_cache_entry(
            cache_row,
            context=f"doc_cache_by_sha256[{sha_key!r}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    for prof_key in sorted(profiles_by_nct.keys(), key=str):
        prof_row = profiles_by_nct[prof_key]
        if not isinstance(prof_row, Mapping):
            raise CitationVerificationError(
                f"profiles_by_nct[{prof_key!r}] must be a mapping."
            )
        _validate_profile_entry(
            prof_key,
            prof_row,
            context=f"profiles_by_nct[{prof_key!r}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    existing_docs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for doc in documents:
        existing_docs_by_nct.setdefault(doc["nct_id"], []).append(doc)

    locs_by_nct: dict[str, list[Mapping[str, Any]]] = {}
    for loc in locations:
        locs_by_nct.setdefault(loc["nct_id"], []).append(loc)

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
        nct_id = row["nct_id"]
        if checked_explicit_ids and nct_id not in checked_explicit_ids:
            continue
        if not _trial_matches_cohort(row, cohort):
            continue
        if row["in_audit"] is True:
            excluded_in_audit_count += 1
            continue
        eligible_rows.append(row)

    eligible_rows.sort(key=lambda r: str(r["nct_id"]))

    for row in eligible_rows:
        nct_id = str(row["nct_id"])
        # F3: Every targeted in-scope trial MUST carry strict int expected_k >= 1.
        expected_k = _require_expected_primary_outcomes(row, context="plan index row")

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
        effective_cells = _resolve_effective_cells(nct_locs)

        unresolved_unreadable_rows = [
            loc
            for loc in effective_cells.values()
            if loc.get("status") == "UNREADABLE"
        ]

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

        cache_hit = (
            (nct_id, effective_pass_role, current_doc_tuple) in verified_target_pass_cache
            or (
                effective_pass_role in {"primary", "ocr_recovery"}
                and (nct_id, "primary", current_doc_tuple) in verified_cache
            )
        )
        if cache_hit:
            grid_locs = (
                primary_locs
                if effective_pass_role in {"primary", "ocr_recovery"}
                else [loc for loc in nct_locs if loc.get("pass") == effective_pass_role]
            )
            by_outcome: dict[int, set[str]] = {}
            for loc in grid_locs:
                idx = loc.get("primary_outcome_index")
                elem = loc.get("element")
                if type(idx) is int and isinstance(elem, str) and elem in ELEMENTS:
                    by_outcome.setdefault(idx, set()).add(elem)
            existing_indices = set(by_outcome.keys())
            expected_indices = set(range(expected_k))
            all_elements = set(ELEMENTS)
            has_oob_sparse = any(
                type(loc.get("primary_outcome_index")) is not int
                or not (0 <= loc["primary_outcome_index"] < expected_k)
                for loc in nct_locs
            )
            if (
                existing_indices != expected_indices
                or not all(by_outcome.get(i) == all_elements for i in range(expected_k))
                or has_oob_sparse
            ):
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
                continue
            partitions["cached_complete"].append(nct_id)
            continue

        if primary_locs:
            if existing_doc_tuple and current_doc_tuple and existing_doc_tuple != current_doc_tuple:
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
            else:
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
        else:
            partitions["unstarted_primary"].append(nct_id)

    if mode == "resume-unstarted":
        scheduled_ncts = list(partitions["unstarted_primary"])
    elif mode == "recover-unreadable":
        scheduled_ncts = list(partitions["unreadable_recoverable_ocr"])
    else:
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
    if not args.index:
        raise AuditHoldoutViolationError(
            "--index is required for 'verify' before reading documents, locations, or page maps."
        )

    index_rows = load_index_rows(Path(args.index))
    audit_nct_ids, in_scope_by_nct, allow_empty_audit_set = _partition_index_firewall(
        index_rows, source_label=str(args.index)
    )
    in_scope_nct_ids: frozenset[str] = frozenset(in_scope_by_nct.keys())

    expected_has_documents_by_nct: dict[str, bool] = {}
    all_expected_outcomes_by_nct: dict[str, int] = {}
    for nct_id, row in sorted(in_scope_by_nct.items()):
        expected_has_documents_by_nct[nct_id] = _validate_index_doc_flags(
            row, context="verify index row"
        )
        all_expected_outcomes_by_nct[nct_id] = _require_expected_primary_outcomes(
            row, context="verify index row"
        )

    documents = load_jsonl(Path(args.documents))
    locations = load_jsonl(Path(args.locations))

    # Validate strict NCT IDs and firewall membership across documents & locations
    # BEFORE initializing DirectoryPageTextProvider or touching page maps.
    for idx, doc in enumerate(documents):
        doc_nct = validate_strict_nct_id(doc.get("nct_id"), context=f"documents[{idx}].nct_id")
        if "in_audit" in doc:
            d_flag = doc["in_audit"]
            if type(d_flag) is not bool or d_flag is True:
                raise AuditHoldoutViolationError(
                    f"documents[{idx}] ({doc_nct!r}) carries forbidden in_audit={d_flag!r}."
                )
        enforce_audit_holdout(
            doc_nct,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    for idx, loc in enumerate(locations):
        loc_nct = validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{idx}].nct_id")
        if "in_audit" in loc:
            l_flag = loc["in_audit"]
            if type(l_flag) is not bool or l_flag is True:
                raise AuditHoldoutViolationError(
                    f"locations[{idx}] ({loc_nct!r}) carries forbidden in_audit={l_flag!r}."
                )
        enforce_audit_holdout(
            loc_nct,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    provider: DirectoryPageTextProvider | None = None
    if args.pages_dir is not None:
        provider = DirectoryPageTextProvider(
            Path(args.pages_dir),
            ocr_dir=Path(args.ocr_dir) if args.ocr_dir is not None else None,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
        )

    expected_outcomes_by_nct: dict[str, int] | None = (
        all_expected_outcomes_by_nct if args.check_completeness else None
    )

    report = verify_census_corpus(
        documents,
        locations,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
        page_text_provider=provider,
        pdf_root=Path(args.pdf_root) if args.pdf_root is not None else None,
        expected_outcomes_by_nct=expected_outcomes_by_nct,
        expected_has_documents_by_nct=expected_has_documents_by_nct,
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
    raw_explicit = list(args.nct_id or ())
    for raw_id in raw_explicit:
        validate_strict_nct_id(raw_id, context="--nct-id")

    index_rows = load_index_rows(Path(args.index))
    audit_nct_ids, in_scope_by_nct, allow_empty_audit_set = _partition_index_firewall(
        index_rows, source_label=str(args.index)
    )
    in_scope_nct_ids: frozenset[str] = frozenset(in_scope_by_nct.keys())

    # Validate --nct-id against holdout, index membership, and cohort BEFORE loading side files.
    for raw_id in raw_explicit:
        enforce_audit_holdout(
            raw_id,
            in_audit=False,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        if not _trial_matches_cohort(in_scope_by_nct[raw_id], args.cohort):
            raise PlanScopeError(
                f"Requested --nct-id {raw_id!r} does not belong to cohort {args.cohort!r}."
            )

    documents = load_jsonl(Path(args.documents)) if args.documents is not None else []
    locations = load_jsonl(Path(args.locations)) if args.locations is not None else []
    doc_cache = _load_doc_cache_by_sha256(
        Path(args.doc_cache) if args.doc_cache is not None else None,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    profiles = _load_profiles_by_nct(
        Path(args.profiles) if args.profiles is not None else None,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
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
        explicit_nct_ids=frozenset(raw_explicit),
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
            "census batch planner (requires Python 3.11+ stdlib, "
            "src.citation_verifier, and src.utils.pipeline_commit)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="Verify census documents.jsonl and locations.jsonl")
    p_verify.add_argument(
        "--index",
        type=str,
        required=True,
        help="Required cohort index JSON/JSONL carrying explicit bool in_audit",
    )
    p_verify.add_argument("--documents", type=str, required=True, help="Path to documents.jsonl")
    p_verify.add_argument("--locations", type=str, required=True, help="Path to locations.jsonl")
    p_verify.add_argument("--pages-dir", type=str, default=None, help="Page-text JSON/JSONL map directory")
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
        help="Allow optional metadata keys in documents.jsonl",
    )
    p_verify.add_argument("--report-out", type=str, default=None, help="Output JSON report path")

    p_plan = sub.add_parser("plan", help="Plan incremental sha256-keyed /swarm batch shards")
    p_plan.add_argument(
        "--index",
        type=str,
        required=True,
        help="Required cohort index JSON/JSONL carrying explicit bool in_audit",
    )
    p_plan.add_argument("--cohort", type=str, default=None, help="Cohort slug filter")
    p_plan.add_argument("--documents", type=str, default=None, help="Existing documents.jsonl")
    p_plan.add_argument("--locations", type=str, default=None, help="Existing locations.jsonl")
    p_plan.add_argument(
        "--doc-cache",
        type=str,
        default=None,
        help="Content-addressed document_index_by_sha256.jsonl",
    )
    p_plan.add_argument(
        "--profiles",
        type=str,
        default=None,
        help="Per-trial trial_document_profiles.jsonl",
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
        help="Explicit NCT ID filter (repeatable; exits 2 if invalid, in_audit, unlisted, or out-of-cohort)",
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
        help="Projected USD per trial under TOC-sliced pages_selected",
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
    """CLI entry point returning 0 (ok), 1 (gate fail), or 2 (firewall/NCT/scope/usage refusal)."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            return 0
        return int(exc.code) if type(exc.code) is int else 2

    try:
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "plan":
            return _cmd_plan(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except (AuditHoldoutViolationError, InvalidNctIdError, PlanScopeError) as exc:
        print(f"ERROR [firewall_or_scope]: {exc}", file=sys.stderr)
        return 2
    except CitationVerificationError as exc:
        print(f"ERROR [verification]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
