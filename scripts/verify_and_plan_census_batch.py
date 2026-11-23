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

Firewall basis: every ``enforce_audit_holdout`` call here passes
``in_scope_nct_ids`` (the index rows carrying explicit ``in_audit=False``) and
no ``in_audit``, so membership of that set, read from the index, is what admits
a trial; no call writes a literal ``in_audit=False`` on a trial's behalf. A
``--doc-cache`` entry names no trial of its own, so it is admitted only when
its sha256 is a document of an in-scope trial and none of its keys or strings
names an audit, unlisted or malformed NCT ID; an entry that does name a trial
must name one whose document has that sha256, and two different rows for one
sha256 raise. An in-scope index row may not name an audit trial in any key or
string (an alias included), and its classic ``NCTId`` must be its ``nct_id``.
``--documents``, ``--locations``
and ``--profiles`` rows follow the same token rule under every key and string,
with the row's own trial also admitted. ``verify --pages-dir`` hands the page
provider the ``(nct_id, sha256)`` pairs of the in-scope documents, so no
content-addressed page map outside them is opened. ``plan`` files a trial as
``cached_complete`` only when every one of its rows passes
``verify_location_record``. An index row's missing ``protocol``/``sap`` flag is
unknown, not "no document".

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
    ALLOWED_PASSES,
    _is_non_empty_str,
    _prescan_nested_nct_fields,
    _scan_record_strings_for_nct_tokens,
    _scan_strings_for_audit_nct_tokens,
    _validate_nested_firewall_metadata,
    AuditHoldoutViolationError,
    CitationVerificationError,
    DirectoryPageTextProvider,
    ELEMENTS,
    InvalidNctIdError,
    UNREADABLE_SUBTYPES,
    VERIFIER_RULES_VERSION,
    enforce_audit_holdout,
    validate_citation_shape,
    validate_status_citations,
    validate_strict_nct_id,
    verify_census_corpus,
    verify_location_record,
)
from src.utils import pipeline_commit  # noqa: E402

PLANNER_SCHEMA_VERSION: Final[int] = 1
# 1.3.0: cached_complete only for trials whose every row verify accepts; unknown modes refused.
# 1.4.0: conflicting --doc-cache rows for one sha256 raise; an entry declaring a trial must
# declare one whose document has its sha256; in-scope index rows are read for audit IDs in
# every key and string and their classic NCTId must be their nct_id.
PLANNER_RULES_VERSION: Final[str] = "1.4.0"

PlanMode = Literal["resume-unstarted", "recover-unreadable", "incremental-delta"]
CostSchedule = Literal["sliced", "full", "conservative"]
# The single list of each, used by the CLI choices and by plan_incremental_cohort, which
# refuses anything else rather than reading an unknown mode as incremental-delta.
PLAN_MODES: Final[tuple[str, ...]] = ("resume-unstarted", "recover-unreadable", "incremental-delta")
COST_SCHEDULES: Final[tuple[str, ...]] = ("sliced", "full", "conservative")

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
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PIPELINE_COMMIT_MISSING_REASON: Final[str] = (
    "GITHUB_SHA is unset and `git rev-parse HEAD` failed, so the code version that "
    "produced this plan is unknown"
)

_SCAN_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:image[- ]only|scan(?:ned)?|no\s+extractable\s+text|0\s+text\s+pages?|"
    r"raster(?:ized)?|photocop(?:y|ied))\b",
    re.IGNORECASE,
)


class PlanScopeError(CitationVerificationError):
    """Raised when an explicit ``--nct-id`` target falls outside the selected cohort."""


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

    Method: the exact key ``in_audit`` of each row is the index's flag and the
    only thing that admits a trial. An in-scope row is then read as the
    record of its trial: (1) the named-key walk, in which the holdout flag
    under any other spelling (``inAudit``, ``IN_AUDIT``) can only refuse;
    (2) its own-ID fields, the classic API's ``NCTId`` (a string or a list of
    strings), must be the row's ``nct_id``; (3) no key or string of the row,
    ``nctIdAliases`` at any depth included, may name an audit trial in any
    reading (``_scan_strings_for_audit_nct_tokens``), since a row whose alias
    is an audit ID is a holdout trial under another number, as
    ``assemble_longitudinal_provenance`` refuses it. Other trials named in an
    index row's text are not refused: index rows carry registry text (titles)
    that names related trials for good reasons. Messages name rows by
    position, never by the ID of a row that is, or may be, held out.
    """
    if len(index_rows) == 0:
        raise AuditHoldoutViolationError(
            f"{source_label} must contain at least one trial row."
        )

    audit_ids: set[str] = set()
    in_scope_by_nct: dict[str, Mapping[str, Any]] = {}
    seen_ncts: set[str] = set()

    # Phase A (F2): Validate strict ASCII ^NCT[0-9]{8}$ across ALL index rows FIRST.
    validated_ncts: list[str] = []
    for idx, row in enumerate(index_rows):
        if not isinstance(row, Mapping):
            raise CitationVerificationError(
                f"{source_label}[{idx}] is not a JSON object ({type(row).__name__})."
            )
        validated_ncts.append(
            validate_strict_nct_id(row.get("nct_id"), context=f"{source_label}[{idx}].nct_id")
        )
        _prescan_nested_nct_fields(row, context=f"{source_label}[{idx}]")

    # Phase B (F1): Enforce uniqueness and explicit bool in_audit on every row.
    for idx, row in enumerate(index_rows):
        nct_id = validated_ncts[idx]
        if nct_id in seen_ncts:
            raise AuditHoldoutViolationError(
                f"Duplicate nct_id in {source_label} at row {idx} (the trial is not named: "
                f"it may be held out)."
            )
        seen_ncts.add(nct_id)

        if "in_audit" not in row or type(row["in_audit"]) is not bool:
            shown_flag = (
                "<missing>" if "in_audit" not in row else type(row["in_audit"]).__name__
            )
            raise AuditHoldoutViolationError(
                f"{source_label}[{idx}] must carry an explicit bool in_audit, got {shown_flag} "
                f"(the trial is not named: its holdout status is unknown)."
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
    frozen_audit = frozenset(audit_ids)
    frozen_scope = frozenset(in_scope_by_nct.keys())
    for nct_id, row in sorted(in_scope_by_nct.items()):
        row_context = f"{source_label}[{nct_id}]"
        _scan_strings_for_audit_nct_tokens(row, context=row_context, audit_nct_ids=frozen_audit)
        _validate_nested_firewall_metadata(
            row,
            context=row_context,
            expected_nct_id=nct_id,
            audit_nct_ids=frozen_audit,
            in_scope_nct_ids=frozen_scope,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _check_index_row_classic_nct_id(row, nct_id=nct_id, context=row_context)
    return frozen_audit, in_scope_by_nct, allow_empty_audit_set


def _check_index_row_classic_nct_id(
    row: Mapping[str, Any], *, nct_id: str, context: str
) -> None:
    """Require an index row's classic-API ``NCTId`` (a string or list of strings) to be its trial.

    ``nctId`` and ``nct_id`` are already own-ID keys of the named-key walk; the
    classic API's ``NCTId`` names the same trial and, read as free text only,
    could name another while the row is admitted under ``nct_id``.
    """
    if "NCTId" not in row:
        return
    classic = row["NCTId"]
    values = classic if isinstance(classic, list) else [classic]
    if not values:
        raise CitationVerificationError(f"{context}.NCTId is an empty list; it names no trial.")
    for idx, value in enumerate(values):
        checked = validate_strict_nct_id(value, context=f"{context}.NCTId[{idx}]")
        if checked != nct_id:
            raise AuditHoldoutViolationError(
                f"{context}.NCTId[{idx}]={checked!r} is not the row's nct_id; one index row "
                f"names one trial."
            )


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


def _validate_index_doc_flags(row: Mapping[str, Any], *, context: str) -> bool | None:
    """Read an index row's ``protocol`` / ``sap`` flags as posted-document presence.

    Each flag present must be a strict bool. Returns True when either flag is
    True; False only when both flags are present and both False; None when a
    flag is absent and the other does not settle it. A missing flag is
    absence of information, not evidence that no document was posted, so it is
    returned as None (unknown) and never coerced to False.
    """
    nct_label = row.get("nct_id")
    flags: dict[str, bool] = {}
    for flag_key in ("protocol", "sap"):
        if flag_key in row:
            flag_val = row[flag_key]
            if type(flag_val) is not bool:
                raise CitationVerificationError(
                    f"{context} ({nct_label!r}) has non-bool {flag_key}={flag_val!r}."
                )
            flags[flag_key] = flag_val
    if any(flags.values()):
        return True
    if len(flags) == 2:
        return False
    return None


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


def _scan_strings_for_nct_tokens(
    node: Any,
    *,
    context: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> None:
    """Refuse an audit, unlisted or malformed NCT token in any key or string value.

    The named-key checks in ``_validate_nested_firewall_metadata`` see only
    ``nct_id``-style keys and URL keys; a doc-cache entry can name a trial under
    any key (``{"trial": ...}``) or inside free text. Every string, keys
    included, is therefore read with the verifier's single text-token rule
    (``src.citation_verifier._check_embedded_text_ncts``): an NCT-like token must
    be strict, outside the audit set and, since a doc-cache entry has no trial
    of its own (``expected_nct_id=None``), in scope. This can refuse an in-scope
    document whose indexed text names another trial; that refusal is loud
    (exit 2) and deliberate, since a silent pass is the failure mode.
    """
    _scan_record_strings_for_nct_tokens(
        node,
        context=context,
        expected_nct_id=None,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )


def _validate_doc_cache_entry(
    row: Mapping[str, Any],
    *,
    context: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> tuple[str, frozenset[str]]:
    """Firewall one doc-cache entry and return its sha256 and the trials it declares.

    Method: entries of ``document_index_by_sha256.jsonl`` are content-addressed
    and usually name no trial at all, so a trial cannot be read off the entry.
    The entry must therefore (1) carry a valid lowercase 64-hex ``sha256``, the
    only thing that can tie it to a document (``plan_incremental_cohort`` then
    requires that sha to be a document of an in-scope trial); (2) pass the
    named-key checks (``nct_id``-style keys, the holdout flag, URL keys); and
    (3) hold no audit, unlisted or malformed NCT token in any other key or
    string. The trials it names under ID keys (``nct_id``, ``nct_ids``, at any
    depth) are returned: when it names any, ``plan_incremental_cohort``
    requires them to be trials whose documents have that sha256.
    """
    declared = _validate_nested_firewall_metadata(
        row,
        context=context,
        expected_nct_id=None,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _scan_strings_for_nct_tokens(
        row,
        context=context,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    sha = row.get("sha256")
    if type(sha) is not str or _SHA256_HEX_RE.fullmatch(sha) is None:
        raise AuditHoldoutViolationError(
            f"{context} has no valid lowercase 64-hex sha256 (got {sha!r}), so it cannot be "
            f"tied to a document of an in-scope trial."
        )
    return sha, frozenset(declared)


def _load_doc_cache_by_sha256(
    path: Path | None,
    *,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> dict[str, dict[str, Any]]:
    """Load ``--doc-cache`` rows keyed by sha256, firewalling each row as it is keyed.

    A row without a valid sha256 is refused rather than skipped
    (``_validate_doc_cache_entry``). Two rows for one sha256 must be identical:
    rows that differ (one says ``image_only_scan``, the other
    ``redacted_cci_ppd``) leave no way to tell which describes the document,
    and keeping the last one read would let line order decide whether a trial
    is recovered by OCR or skipped at $0, so they raise. An identical repeat is
    kept once.
    """
    if path is None:
        return {}
    rows = load_jsonl(path)
    for idx, row in enumerate(rows):
        _prescan_nested_nct_fields(row, context=f"{path}[{idx}]")
    by_sha: dict[str, dict[str, Any]] = {}
    first_line_by_sha: dict[str, int] = {}
    for idx, row in enumerate(rows):
        sha, _ = _validate_doc_cache_entry(
            row,
            context=f"{path}[{idx}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        if sha in by_sha:
            if by_sha[sha] != row:
                raise CitationVerificationError(
                    f"{path}[{idx}] and {path}[{first_line_by_sha[sha]}] are different doc-cache "
                    f"rows for sha256 {sha!r}; which describes the document is not determined."
                )
            continue
        by_sha[sha] = row
        first_line_by_sha[sha] = idx
    return by_sha


def _validate_profile_entry(
    key_nct: Any,
    row: Mapping[str, Any],
    *,
    context: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> str:
    checked_key = validate_strict_nct_id(key_nct, context=f"{context}.nct_id")
    _prescan_nested_nct_fields(row, context=context)
    enforce_audit_holdout(
        checked_key,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_nested_firewall_metadata(
        row,
        context=context,
        expected_nct_id=checked_key,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    # A profile is a record about one trial, read like a document row: any key or
    # string naming another trial follows the derived-ID rule.
    _scan_record_strings_for_nct_tokens(
        row,
        context=context,
        expected_nct_id=checked_key,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    nested_docs = row.get("documents")
    if nested_docs is not None and not isinstance(nested_docs, list):
        raise CitationVerificationError(
            f"{context}.documents must be a list, got {type(nested_docs).__name__}."
        )
    if isinstance(nested_docs, list):
        for doc_idx, doc in enumerate(nested_docs):
            if not isinstance(doc, Mapping):
                raise CitationVerificationError(
                    f"{context}.documents[{doc_idx}] must be an object."
                )
    return checked_key


def _load_profiles_by_nct(
    path: Path | None,
    *,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    rows = load_jsonl(path)
    for idx, row in enumerate(rows):
        validate_strict_nct_id(row.get("nct_id"), context=f"{path}[{idx}].nct_id")
        _prescan_nested_nct_fields(row, context=f"{path}[{idx}]")
    by_nct: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(rows):
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
    """Return whether a planned trial has posted documents, refusing to guess.

    Supplied documents settle it; otherwise the index flags do. When neither
    does (no documents and a ``protocol``/``sap`` flag missing), the trial is
    neither scheduled nor counted as no-document: the planner raises, naming
    the row, as it does for a missing expected outcome count.
    """
    has_flag_doc = _validate_index_doc_flags(row, context="plan index row")
    if current_docs:
        return True
    if has_flag_doc is None:
        raise CitationVerificationError(
            f"plan index row ({row.get('nct_id')!r}) has no supplied documents and lacks a "
            f"'protocol' or 'sap' flag, so whether it has posted documents is unknown; it is "
            f"neither scheduled nor counted as having no document."
        )
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
        seen_cells: set[tuple[int, str]] = set()
        malformed_or_duplicate = False
        for loc in nct_locs:
            idx = loc.get("primary_outcome_index")
            elem = loc.get("element")
            if type(idx) is not int or idx < 0 or type(elem) is not str or elem not in ELEMENTS:
                malformed_or_duplicate = True
                break
            cell_key = (idx, elem)
            if cell_key in seen_cells:
                malformed_or_duplicate = True
                break
            seen_cells.add(cell_key)
            by_outcome.setdefault(idx, set()).add(elem)
        if malformed_or_duplicate or not by_outcome:
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

        # Validate status-to-citation rules and 6-key citation shape across all effective cells.
        rows_valid = True
        cit_pairs: set[tuple[str, str]] = set()
        for loc in citation_source_locs:
            cits = loc.get("citations")
            status_val = loc.get("status")
            if not isinstance(cits, list) or type(status_val) is not str:
                rows_valid = False
                break
            if validate_status_citations(
                status_val, len(cits), has_posted_documents=True
            ):
                rows_valid = False
                break
            for cit in cits:
                if validate_citation_shape(cit):
                    rows_valid = False
                    break
                cit_pairs.add((str(cit["filename"]), str(cit["sha256"])))
            if not rows_valid:
                break
        if not rows_valid:
            continue

        doc_tuple = compute_doc_sha256_tuple(existing_docs_by_nct.get(nct_id, ()))
        if doc_tuple:
            if not cit_pairs.issubset(set(doc_tuple)):
                continue
        else:
            doc_tuple = tuple(sorted(cit_pairs))

        verified_keys[(nct_id, pass_role, doc_tuple)] = True
    return verified_keys


def _trial_rows_verify(
    nct_id: str,
    nct_locs: Sequence[Mapping[str, Any]],
    current_docs: Sequence[Mapping[str, Any]],
    *,
    manifest_has_documents: bool | None,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str],
    allow_empty_audit_set: bool,
) -> bool:
    """Return whether every location row of one trial is a row ``verify`` would accept.

    Method: ``cached_complete`` means "never re-run", so it may only be given to
    rows that verification accepts. The cache lookup
    (``_verified_pass_doc_fingerprints``) checks the grid, status-to-citation
    counts and citation shape; rather than copy more of verify's row rules here
    (search coverage, required keys, pass names, provenance, neutral language),
    every row of the trial, of every pass, is run through
    ``src.citation_verifier.verify_location_record`` against the trial's current
    documents, with no page provider (planning reads no page text) and
    exhaustive NOT_FOUND required (verify's default). Any error-severity issue,
    two rows for one (outcome, element, pass) cell, or two current documents
    with one filename returns False. Warnings (``NOTE_PAGE_COUNT_MISMATCH``) do
    not, as they do not fail verify.
    """
    docs_by_filename: dict[str, Mapping[str, Any]] = {}
    for doc in current_docs:
        fn = doc.get("filename")
        if type(fn) is not str or fn in docs_by_filename:
            return False
        docs_by_filename[fn] = doc
    seen_cells: set[tuple[str, str, str]] = set()
    for loc in nct_locs:
        # repr keeps unhashable or oddly typed values (a list index) comparable.
        cell = (
            repr(loc.get("primary_outcome_index")),
            repr(loc.get("element")),
            repr(loc.get("pass")),
        )
        if cell in seen_cells:
            return False
        seen_cells.add(cell)
        _, issues = verify_location_record(
            loc,
            docs_by_nct={nct_id: docs_by_filename},
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
            manifest_has_documents=manifest_has_documents,
        )
        if any(issue.severity == "error" for issue in issues):
            return False
    return True


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
    ``expected_k >= 1``, and provenance stamps across all input streams. ``mode``
    and ``cost_schedule`` must be one of ``PLAN_MODES`` / ``COST_SCHEDULES``
    exactly; anything else is refused, never read as the default.
    """
    if type(mode) is not str or mode not in PLAN_MODES:
        raise CitationVerificationError(
            f"Invalid mode {mode!r}; expected one of {PLAN_MODES}."
        )
    if type(cost_schedule) is not str or cost_schedule not in COST_SCHEDULES:
        raise CitationVerificationError(
            f"Invalid cost_schedule {cost_schedule!r}; expected one of {COST_SCHEDULES}."
        )
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
    if type(effective_pass_role) is not str or effective_pass_role not in ALLOWED_PASSES:
        raise CitationVerificationError(
            f"Invalid pass_role {effective_pass_role!r}; expected one of {ALLOWED_PASSES}."
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
        _prescan_nested_nct_fields(doc, context=f"documents[{doc_idx}]")
    for loc_idx, loc in enumerate(locations):
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"locations[{loc_idx}] must be a mapping, got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{loc_idx}].nct_id")
        _prescan_nested_nct_fields(loc, context=f"locations[{loc_idx}]")
    for sha_key, cache_row in doc_cache_by_sha256.items():
        if not isinstance(cache_row, Mapping):
            raise CitationVerificationError(
                f"doc_cache_by_sha256[{sha_key!r}] must be a mapping."
            )
        _prescan_nested_nct_fields(cache_row, context=f"doc_cache_by_sha256[{sha_key!r}]")
    for prof_key in sorted(profiles_by_nct.keys(), key=str):
        validate_strict_nct_id(prof_key, context=f"profiles_by_nct[{prof_key!r}].nct_id")
        prof_row = profiles_by_nct[prof_key]
        if not isinstance(prof_row, Mapping):
            raise CitationVerificationError(
                f"profiles_by_nct[{prof_key!r}] must be a mapping."
            )
        _prescan_nested_nct_fields(prof_row, context=f"profiles_by_nct[{prof_key!r}]")

    # Step 2 (F1 & F3): Hard firewall + scope checks across explicit_nct_ids and all side streams.
    for nct_id in sorted(checked_explicit_ids):
        enforce_audit_holdout(
            nct_id,
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
        enforce_audit_holdout(
            doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_nested_firewall_metadata(
            doc,
            context=f"documents[{doc_idx}]",
            expected_nct_id=doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _scan_record_strings_for_nct_tokens(
            doc,
            context=f"documents[{doc_idx}]",
            expected_nct_id=doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_document_provenance_for_plan(doc, context=f"documents[{doc_idx}]")

    for loc_idx, loc in enumerate(locations):
        loc_nct = loc["nct_id"]
        enforce_audit_holdout(
            loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_nested_firewall_metadata(
            loc,
            context=f"locations[{loc_idx}]",
            expected_nct_id=loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _scan_record_strings_for_nct_tokens(
            loc,
            context=f"locations[{loc_idx}]",
            expected_nct_id=loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_location_provenance_for_plan(loc, context=f"locations[{loc_idx}]")

    # A doc-cache entry is tied to a trial only through its sha256: it must be the sha256 of
    # a document of an in-scope trial (from ``documents`` or a profile's documents, both
    # firewalled above). An entry that no in-scope document points at is refused, since
    # nothing shows whose content it is.
    # Each in-scope document sha256, with the trials whose documents carry it.
    trials_by_doc_sha: dict[str, set[str]] = {}
    for doc in documents:
        if type(doc.get("sha256")) is str:
            trials_by_doc_sha.setdefault(doc["sha256"], set()).add(doc["nct_id"])
    for prof_key, prof_row in profiles_by_nct.items():
        prof_docs = prof_row.get("documents")
        if isinstance(prof_docs, list):
            for pdoc in prof_docs:
                if isinstance(pdoc, Mapping) and type(pdoc.get("sha256")) is str:
                    trials_by_doc_sha.setdefault(pdoc["sha256"], set()).add(prof_key)
    in_scope_doc_shas: set[str] = set(trials_by_doc_sha)
    for sha_key, cache_row in doc_cache_by_sha256.items():
        if not isinstance(cache_row, Mapping):
            raise CitationVerificationError(
                f"doc_cache_by_sha256[{sha_key!r}] must be a mapping."
            )
        _scan_strings_for_nct_tokens(
            sha_key,
            context="doc_cache_by_sha256 key",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        entry_sha, entry_trials = _validate_doc_cache_entry(
            cache_row,
            context=f"doc_cache_by_sha256[{sha_key!r}]",
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        if sha_key != entry_sha:
            raise AuditHoldoutViolationError(
                f"doc_cache_by_sha256 key {sha_key!r} disagrees with its entry's "
                f"sha256={entry_sha!r}."
            )
        if entry_sha not in in_scope_doc_shas:
            raise AuditHoldoutViolationError(
                f"doc_cache_by_sha256 entry sha256={entry_sha!r} is not the sha256 of any "
                f"document of an in-scope trial in documents or profiles, so it cannot be tied "
                f"to an in-scope trial."
            )
        # An entry that names a trial must name one whose document has its sha256: an entry
        # declaring trial B applied to trial A's document would move A between partitions
        # (OCR recovery or the terminal $0 skip) on another trial's description, as a page
        # map declaring another trial is refused.
        foreign_trials = entry_trials - trials_by_doc_sha[entry_sha]
        if foreign_trials:
            raise AuditHoldoutViolationError(
                f"doc_cache_by_sha256 entry sha256={entry_sha!r} declares "
                f"{len(foreign_trials)} trial(s) whose documents do not have that sha256; it "
                f"describes another trial's document."
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
        # True / False / None (unknown) from the index flags, as verify reads them.
        manifest_has_documents = _validate_index_doc_flags(row, context="plan index row")

        current_doc_tuple = compute_doc_sha256_tuple(current_docs)
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

        cache_hit = bool(current_doc_tuple) and (
            (nct_id, effective_pass_role, current_doc_tuple) in verified_target_pass_cache
            or (
                effective_pass_role in {"primary", "ocr_recovery"}
                and (nct_id, "primary", current_doc_tuple) in verified_cache
            )
        )
        if cache_hit and not _trial_rows_verify(
            nct_id,
            nct_locs,
            current_docs,
            manifest_has_documents=manifest_has_documents,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        ):
            # A cache entry exists, but some row of the trial would fail verify: the rows
            # are re-run, like rows that no longer match the documents.
            partitions["amended_doc_or_outcome_delta"].append(nct_id)
            continue
        if cache_hit:
            grid_locs = (
                primary_locs
                if effective_pass_role in {"primary", "ocr_recovery"}
                else [loc for loc in nct_locs if loc.get("pass") == effective_pass_role]
            )
            cit_check_locs: Sequence[Mapping[str, Any]] = (
                list(effective_cells.values())
                if effective_pass_role in {"primary", "ocr_recovery"}
                else grid_locs
            )
            allowed_doc_pairs = set(current_doc_tuple)
            has_stale_cit_pair = any(
                (str(cit.get("filename")), str(cit.get("sha256"))) not in allowed_doc_pairs
                for loc in cit_check_locs
                for cit in (loc.get("citations") or ())
                if isinstance(cit, Mapping)
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
                or has_stale_cit_pair
            ):
                partitions["amended_doc_or_outcome_delta"].append(nct_id)
                continue
            partitions["cached_complete"].append(nct_id)
            continue

        if primary_locs:
            # Primary rows exist but no verified cache entry matches the current documents:
            # either the document set changed (amendment) or the existing grid/citations do
            # not verify. Both are re-run, so both land in the same partition.
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

    # pipeline_commit() returns None outside a checkout and without GITHUB_SHA; the plan
    # still records that absence, with its reason, instead of a bare null.
    source_commit: str | None = pipeline_commit() or None

    return {
        "planner_schema_version": PLANNER_SCHEMA_VERSION,
        "planner_rules_version": PLANNER_RULES_VERSION,
        "verifier_rules_version": VERIFIER_RULES_VERSION,
        "planned_at": planned_at or datetime.now(timezone.utc).isoformat(),
        "source_pipeline_commit": source_commit,
        "source_pipeline_commit_missing_reason": (
            None if source_commit is not None else _PIPELINE_COMMIT_MISSING_REASON
        ),
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

    # True / False from the index's protocol+sap flags, None where the index does not say.
    expected_has_documents_by_nct: dict[str, bool | None] = {}
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
        validate_strict_nct_id(doc.get("nct_id"), context=f"documents[{idx}].nct_id")
        _prescan_nested_nct_fields(doc, context=f"documents[{idx}]")
    for idx, loc in enumerate(locations):
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{idx}].nct_id")
        _prescan_nested_nct_fields(loc, context=f"locations[{idx}]")

    for idx, doc in enumerate(documents):
        doc_nct = doc["nct_id"]
        enforce_audit_holdout(
            doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_nested_firewall_metadata(
            doc,
            context=f"documents[{idx}]",
            expected_nct_id=doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _scan_record_strings_for_nct_tokens(
            doc,
            context=f"documents[{idx}]",
            expected_nct_id=doc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    for idx, loc in enumerate(locations):
        loc_nct = loc["nct_id"]
        enforce_audit_holdout(
            loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_nested_firewall_metadata(
            loc,
            context=f"locations[{idx}]",
            expected_nct_id=loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _scan_record_strings_for_nct_tokens(
            loc,
            context=f"locations[{idx}]",
            expected_nct_id=loc_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    provider: DirectoryPageTextProvider | None = None
    if args.pages_dir is not None:
        # Content-addressed page maps name no trial, so the provider is told which
        # (trial, sha256) pairs this run's in-scope documents (firewalled above) name: it
        # never opens a <sha256> map outside them, refuses a JSONL row for one, and
        # refuses a lookup of any other pair.
        referenced_documents: frozenset[tuple[str, str]] = frozenset(
            (doc["nct_id"], doc["sha256"])
            for doc in documents
            if type(doc.get("sha256")) is str and _SHA256_HEX_RE.fullmatch(doc["sha256"])
        )
        provider = DirectoryPageTextProvider(
            Path(args.pages_dir),
            ocr_dir=Path(args.ocr_dir) if args.ocr_dir is not None else None,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
            referenced_documents=referenced_documents,
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
        choices=PLAN_MODES,
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
        choices=COST_SCHEDULES,
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
