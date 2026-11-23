"""Cohort-agnostic sharded outcomes and provenance layout (version 1).

This module defines the static file layout for ClinicalTrials.gov primary
outcome reporting, document readability/redaction profiles, decision-rule
locations, and longitudinal provenance:

  outcomes_bundles/
    index.json             lightweight multi-cohort catalog (one row per trial,
                           sorted ascending by nct_id, top-level "data" last)
    shards/0.json.gz       per-trial unified bundles for shard n, where
      .. {N-1}.json.gz     n = int(nct_id[3:]) % N (unpadded; default N = 256)

Every shard file is always written (with "data": {} when empty) so browser
clients computing ``Number(nctId.slice(3)) % shards`` never encounter a 404
and can distinguish "loaded, none" from "not loaded".

Invariants enforced here:
- Exact shard-function parity with ``src/site_layout.py::shard_of``
  (``int(nct_id[3:]) % shards`` for ``NCT`` + 8 digits).
- Deterministic encoding across Python 3.11, 3.12, and 3.13: compact ASCII
  JSON, top-level ``"data"`` last for O(1) KB header reads, and gzip level 9
  with ``mtime=0`` and OS byte ``0xff`` (``gzip.GzipFile`` over ``BytesIO``,
  mirroring ``src/archive_records.py::encode``).
- Hard 6-vector ``in_audit`` holdout firewall (``AuditFirewallError``):
  1. ``in_audit`` must be a strict ``bool`` (missing/``None`` never defaults).
  2. ``in_audit is True`` forbids any ``decision_rule_locations`` (even ``{}``).
  3. ``in_audit is True`` forbids LLM/assessment/extra keys anywhere in rows
     or shard records.
  4. ``in_audit is True`` forbids any ``document_profile`` or PDF-scraped
     document metadata (``version_label``, ``version_date_as_printed``,
     ``sha256``) inside ``longitudinal_provenance``.
  5. ``in_audit is True`` requires ``document_badge`` and ``census_badge``
     to remain strictly ``"in_audit_holdout"`` with ``None``/``False`` counts.
  6. Cross-stream JSONL/mapping guard rejects any holdout ``nct_id`` present
     in document or location inputs BEFORE creating or touching ``out_dir``.
- Absence is preserved as ``None`` (never coerced to ``0`` or ``""``).
- Zero interference with ``src/dataset_folder.py::present_files`` (which only
  inspects ``demographics.part*.json.gz``, ``studies_tab.part*.json.gz``,
  ``detail/``, and ``run.json``).

Rules this module shares rather than restates:
- The status-to-citation rule and the 6-key citation shape are Module 4's
  (``src.citation_verifier``: ``CITATION_REQUIRED_STATUSES``,
  ``CITATION_FORBIDDEN_STATUSES``, ``validate_status_citations``,
  ``validate_citation_shape``), applied both when rows are normalized and when
  a tree on disk is checked, so the two can never disagree. Trial IDs are
  checked with Module 4's ``validate_strict_nct_id`` (ASCII ``^NCT[0-9]{8}$``;
  ``\\d`` would also admit non-ASCII digits, which ``int()`` reads as the same
  trial). This module imports nothing else from another module.

Holdout stubs: an ``in_audit`` trial is written as its index row (fields that
come 1:1 from the trial/manifest row, with masked badges and
``has_reported_results`` ``None``) plus a shard record holding only
``nct_id``, ``in_audit``, ``cohorts`` and ``shard``; its ``reported_results``,
``document_profile``, ``decision_rule_locations`` and
``longitudinal_provenance`` are ``None``. Any side input for such a trial is
refused rather than dropped.

Writing (``write_outcomes_tree``) is allowlisted: the target must be absent,
an empty directory, or a bundle this module wrote (identified by
``outcomes_bundle.json``, which names the layout and its version). See
``check_output_target`` for every refusal.
"""
from __future__ import annotations

import dataclasses
import gzip
import io
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.citation_verifier import (
    CITATION_FORBIDDEN_STATUSES,
    CITATION_REQUIRED_KEYS,
    CITATION_REQUIRED_STATUSES,
    CitationVerificationError,
    validate_citation_shape,
    validate_status_citations,
    validate_strict_nct_id,
)

LAYOUT_VERSION = 1
SHARD_KEY = "nct_number_mod"
DEFAULT_SHARDS = 256
DEFAULT_OUTCOMES_SHARDS = DEFAULT_SHARDS
MAX_SHARDS = 1024

DEFAULT_OUT_DIR_NAME = "outcomes_bundles"
INDEX_FILE = "index.json"
SHARDS_DIR = "shards"
SHARD_FILE_TEMPLATE = "shards/{n}.json.gz"
SHARD_CLASS = "outcomes_detail"
SECTION_LABEL_REPORTED_RESULTS = "Primary results as posted"

# Markers identifying other live data families (site/data/outcomes/,
# site/data/results_structure/, site/data/) that write_outcomes_tree must
# never write into or under. An index.json that is not a bundle's own index
# (a directory without BUNDLE_MARKER_FILE) is refused the same way.
FOREIGN_DIRECTORY_MARKERS: tuple[str, ...] = (
    "run.json",
    "summary.json",
    "records",
    "records.json",
)

# The bundle marker: write_outcomes_tree writes this file into every bundle,
# and a non-empty directory is overwritten only when it holds exactly this
# content. It names the layout and its version, so a bundle of another layout
# version is not overwritten by this one either.
BUNDLE_MARKER_FILE = "outcomes_bundle.json"
BUNDLE_MARKER: dict[str, Any] = {"layout": DEFAULT_OUT_DIR_NAME, "layout_version": LAYOUT_VERSION}

# The staging marker: written first into <out_dir>.partial when this module
# creates it. A .partial without it was not made by this module and is never
# deleted; one with it is this module's leftover staging directory.
STAGING_MARKER_FILE = "outcomes_bundle_staging.json"
STAGING_MARKER: dict[str, Any] = {"staging_for": DEFAULT_OUT_DIR_NAME, "layout_version": LAYOUT_VERSION}

# Inside <out_dir>.partial: the previous shards while the new ones are swapped
# in. If putting them back fails, they stay here and every later run refuses
# until a person has dealt with them.
PREVIOUS_SHARDS_DIR = "_previous_shards"
FAILED_NEW_SHARDS_DIR = "_failed_new_shards"

# Size guardrails matching scripts/check_site_contract.py (telemetry only;
# never modifies tests/data_budget.json).
PART_HARD_LIMIT_BYTES = 100 * 1024 * 1024
TREE_HARD_LIMIT_BYTES = 1_000_000_000
TREE_WARN_BYTES = 900_000_000

# ASCII digits only: ``\d`` also matches other scripts' digits, which int()
# reads as the same number, so "NCT" + eight Arabic-Indic digits would shard
# (and could pass for) an existing trial. Validation goes through Module 4's
# validate_strict_nct_id; this pattern is kept for callers that only test.
NCT_RE = re.compile(r"NCT[0-9]{8}", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COHORT_RE = re.compile(r"[a-z][a-z0-9_]{1,63}")
SHARD_FILENAME_RE = re.compile(r"(0|[1-9]\d*)\.json\.gz")
# Any NCT-like token, in any script's digits and with separators, for
# withholding trial IDs from refusal messages (withhold_nct_ids).
_NCT_LIKE_TOKEN_RE = re.compile(r"nct[\W_]{0,3}\d{8}", re.IGNORECASE)

STANDARD_COHORTS: tuple[str, ...] = (
    "industry_phase3",
    "nih",
    "cooperative_group",
    "academic",
    "us_federal_other",
    "non_profit_foundation",
    "all_large_docs",
)

# Maps Module 2 (sponsors/ecosystem.py) sector names into canonical cohort slugs.
ECOSYSTEM_SECTOR_TO_COHORT: dict[str, str] = {
    "nih": "nih",
    "us_federal_other": "us_federal_other",
    "nctn_cooperative_group": "cooperative_group",
    "cooperative_group": "cooperative_group",
    "academic_medical_center": "academic",
    "academic": "academic",
    "non_profit_foundation": "non_profit_foundation",
}

ELEMENTS: tuple[str, ...] = (
    "primary_endpoint_analysed",
    "analysis_population",
    "hypothesis_type",
    "alpha_and_sidedness",
    "margin",
    "interim_boundary",
    "multiplicity",
)
_ELEMENT_ORDER: dict[str, int] = {name: i for i, name in enumerate(ELEMENTS)}

LOCATION_STATUSES: tuple[str, ...] = (
    "FOUND",
    "NOT_FOUND",
    "NOT_APPLICABLE",
    "AMBIGUOUS",
    "UNREADABLE",
    "NO_DOCUMENT",
)

# CITATION_REQUIRED_STATUSES and CITATION_FORBIDDEN_STATUSES are imported from
# src.citation_verifier above: Module 4 holds the one definition of which
# statuses need a citation and which forbid one.

CENSUS_STATUSES: tuple[str, ...] = (
    "complete",
    "partial",
    "not_started",
    "no_document",
)

READABILITY_STATUSES: tuple[str, ...] = (
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
    "download_failed",
    "no_document",
)

READABILITY_BADGE_STATUSES: tuple[str, ...] = (
    *READABILITY_STATUSES,
    "not_indexed",
    "in_audit_holdout",
)

REDACTION_FLAGS: tuple[str, ...] = (
    "cci",
    "ppd",
    "white_box_or_blank_stats",
)

CENSUS_BADGE_STATUSES: tuple[str, ...] = (
    "complete",
    "partial",
    "not_started",
    "no_document",
    "in_audit_holdout",
)

DATE_PRECISIONS: tuple[str, ...] = (
    "day",
    "month",
    "year",
    "unknown",
)

DOC_TYPES: tuple[str, ...] = (
    "protocol",
    "sap",
    "combined",
)

ALLOWED_PASSES: tuple[str, ...] = (
    "primary",
    "second",
    "ocr_recovery",
    "adjudication",
)

EFFECTIVE_SOURCE_PASSES: tuple[str, ...] = (
    "primary",
    "ocr_recovery",
    "adjudication",
)

# Exact shard test vectors shared with src/site_layout.py.
SHARD_VECTORS: tuple[dict[str, Any], ...] = (
    {"nct_id": "NCT01975376", "shards": 256, "shard": 80},
    {"nct_id": "NCT00663858", "shards": 256, "shard": 50},
    {"nct_id": "NCT01174160", "shards": 256, "shard": 144},
    {"nct_id": "NCT01174160", "shards": 128, "shard": 16},
)

INDEX_HEADERS: tuple[str, ...] = (
    "extracted_at",
    "source_pipeline_commit",
    "layout_version",
    "layout",
    "cohort_counts",
    "total_trials",
    "audit_holdout_trials",
    "component_versions",
    "data",
)

SHARD_HEADERS: tuple[str, ...] = (
    "extracted_at",
    "source_pipeline_commit",
    "layout_version",
    "class",
    "shard",
    "shards",
    "key",
    "data",
)

INDEX_ROW_KEYS: tuple[str, ...] = (
    "nct_id",
    "brief_title",
    "study_type",
    "phase",
    "enrollment",
    "enrollment_type",
    "primary_completion_date",
    "results_first_post_date",
    "lead_sponsor_name",
    "sponsor_class",
    "ecosystem_category",
    "primary_condition",
    "cohorts",
    "in_audit",
    "protocol",
    "sap",
    "primary_outcomes_count",
    "has_reported_results",
    "document_badge",
    "census_badge",
)

SHARD_RECORD_KEYS: tuple[str, ...] = (
    "nct_id",
    "in_audit",
    "cohorts",
    "shard",
    "reported_results",
    "document_profile",
    "decision_rule_locations",
    "longitudinal_provenance",
)

# The 6 citation keys are Module 4's (one definition, as for the status rules).
CITATION_KEYS: tuple[str, ...] = CITATION_REQUIRED_KEYS

FORBIDDEN_AUDIT_KEYS: frozenset[str] = frozenset({
    "llm_assessment",
    "llm_assessments",
    "llm_extraction",
    "paper_extraction",
    "fda_extraction",
    "ai_provider",
    "model_id",
    "prompt_sha256",
    "worker",
    "worker_notes",
    "second_pass",
    "ocr_recovery_pass",
    "adjudication_pass",
    "effective_pass",
    "citations",
    "locations",
})

FORBIDDEN_RAW_TEXT_KEYS: frozenset[str] = frozenset({
    "page_texts",
    "raw_pages",
    "pages_text",
    "full_text",
})


class OutcomesLayoutError(ValueError):
    """The outcomes layout, a shard, or a trial record violates the contract."""


class AuditFirewallError(OutcomesLayoutError):
    """Raised when an ``in_audit == True`` holdout trial leaks protected data."""


class RetainedBackupError(OutcomesLayoutError):
    """The swap into the target failed and the previous shards could not be put
    back; they are kept in ``<out_dir>.partial/_previous_shards`` (named in the
    message) and every later write refuses until a person has dealt with them.
    """


def withhold_nct_ids(text: str) -> str:
    """Replace every NCT-like token in ``text`` with a placeholder.

    Used on refusal messages that concern the ``in_audit`` holdout: such a
    message may be about a holdout trial, and a holdout trial's ID is not to be
    printed or logged. The pattern is deliberately loose (any script's digits,
    up to three separator characters after ``NCT``), so a malformed rendering
    of an ID is withheld too.
    """
    return _NCT_LIKE_TOKEN_RE.sub("<NCT id withheld>", text)


def strict_nct_id(value: Any, label: str = "nct_id") -> str:
    """Return ``value`` if it is an ASCII ``^NCT[0-9]{8}$`` string, else raise.

    Delegates to Module 4's ``validate_strict_nct_id`` (no stripping, no case
    folding, no non-ASCII digits) and re-raises its refusal as
    ``OutcomesLayoutError``; Module 4's message withholds any NCT-like value.
    """
    try:
        return validate_strict_nct_id(value, context=label)
    except CitationVerificationError as exc:
        raise OutcomesLayoutError(str(exc)) from None


# ── sharding and naming ───────────────────────────────────────────────────────

def _validate_shards(shards: Any) -> int:
    """Validate shard count as a strict int (rejecting bool) in 1..MAX_SHARDS."""
    if type(shards) is not int or not 1 <= shards <= MAX_SHARDS:
        raise OutcomesLayoutError(
            f"shards must be a whole number from 1 to {MAX_SHARDS}, got {shards!r}"
        )
    return shards


def outcomes_shard_of(nct_id: Any, shards: int = DEFAULT_OUTCOMES_SHARDS) -> int:
    """Return ``int(nct_id[3:]) % shards`` matching ``src/site_layout.py::shard_of``."""
    n_shards = _validate_shards(shards)
    checked = strict_nct_id(nct_id, "nct_id (no outcomes shard can hold it)")
    return int(checked[3:]) % n_shards


# Alias matching architecture spec #61 naming.
shard_of = outcomes_shard_of


def shard_relpath(shard: int) -> str:
    """Relative path inside an outcomes tree: ``shards/<n>.json.gz`` (unpadded)."""
    if type(shard) is not int or shard < 0:
        raise OutcomesLayoutError(f"shard index must be a non-negative int, got {shard!r}")
    return SHARD_FILE_TEMPLATE.replace("{n}", str(shard))


def layout_header(shards: int = DEFAULT_OUTCOMES_SHARDS) -> dict[str, Any]:
    """The ``layout`` metadata block written into ``outcomes_bundles/index.json``."""
    n_shards = _validate_shards(shards)
    return {
        "version": LAYOUT_VERSION,
        "index_file": INDEX_FILE,
        "shards_dir": SHARDS_DIR,
        "shard_file": SHARD_FILE_TEMPLATE,
        "shards": n_shards,
        "key": SHARD_KEY,
    }


# ── deterministic encoding and gzip header inspection ─────────────────────────

def canonicalize_nested(value: Any) -> Any:
    """Recursively sort every dict's keys while preserving list order."""
    if isinstance(value, dict):
        return {k: canonicalize_nested(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [canonicalize_nested(v) for v in value]
    if isinstance(value, tuple):
        return [canonicalize_nested(v) for v in value]
    return value


def encode_json_text(doc: Mapping[str, Any], *, trailing_newline: bool = False) -> str:
    """Encode a mapping as compact ASCII JSON preserving top-level key order.

    Top-level key order is intentionally preserved so ``"data"`` stays last
    for O(1) KB header reads; nested payloads are canonicalized separately.
    """
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True)
    return text + "\n" if trailing_newline else text


def encode_gz_bytes(doc: Mapping[str, Any]) -> bytes:
    """Compress compact ASCII JSON with level 9, ``mtime=0``, and OS byte ``0xff``.

    ``gzip.GzipFile`` writes OS byte ``0xff`` ("unknown") identically on Python
    3.11, 3.12, and 3.13 (unlike ``gzip.compress`` on 3.11/3.12), matching
    ``src/archive_records.py::encode``.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(encode_json_text(doc).encode("ascii"))
    return buf.getvalue()


def inspect_gzip_header(raw: bytes) -> tuple[int, int]:
    """Return ``(mtime, os_byte)`` from a raw gzip byte stream."""
    if len(raw) < 10 or raw[:2] != b"\x1f\x8b":
        raise OutcomesLayoutError("file does not carry a valid gzip header")
    mtime = int.from_bytes(raw[4:8], "little")
    os_byte = raw[9]
    return mtime, os_byte


# ── provenance helpers ────────────────────────────────────────────────────────

# The engine checkout this module belongs to (src/outcomes_layout.py -> root).
ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _git_output(repo_root: Path, *args: str) -> str | None:
    """Return ``git -C <repo_root> <args>`` stdout stripped, or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out or None


def resolve_pipeline_commit(
    explicit: str | None = None,
    *,
    repo_root: Path = ENGINE_ROOT,
) -> str | None:
    """Return the commit to stamp as ``source_pipeline_commit``, or ``None``.

    Method:
    1. An explicit value (``--pipeline-commit``) wins, validated against
       ``COMMIT_RE``.
    2. Otherwise ``git -C <repo_root> rev-parse HEAD``, where ``repo_root`` is
       this engine checkout (from ``__file__``), never the caller's working
       directory: run from inside another repository, a bare ``git rev-parse``
       would stamp that repository's commit. The answer counts only when
       ``git -C <repo_root> rev-parse --show-toplevel`` is ``repo_root``
       itself, so an engine copied into another repository without its own
       ``.git`` does not borrow the enclosing repository's commit.
    3. Anything else is unresolvable and returns ``None``; the caller refuses
       to build rather than stamp ``null`` (see scripts/build_outcomes_shards.py).

    ``GITHUB_SHA`` is not read: it names the commit of whichever repository's
    workflow is running, which need not be this engine checkout.
    """
    if explicit is not None:
        return validate_commit_stamp(explicit, allow_null=False, label="source_pipeline_commit")
    toplevel = _git_output(repo_root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    try:
        same_repo = Path(toplevel).resolve() == Path(repo_root).resolve()
    except OSError:
        return None
    if not same_repo:
        return None
    head = _git_output(repo_root, "rev-parse", "HEAD")
    if head is not None and COMMIT_RE.fullmatch(head):
        return head
    return None


def validate_iso_timestamp(value: Any, label: str = "extracted_at") -> str:
    """Validate a non-empty ISO-8601 timestamp string."""
    if not isinstance(value, str) or not value.strip():
        raise OutcomesLayoutError(f"{label} must be a non-empty ISO-8601 timestamp string, got {value!r}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomesLayoutError(f"{label} is not a valid ISO-8601 timestamp: {value!r}") from exc
    return value


def validate_commit_stamp(
    value: Any,
    *,
    allow_null: bool = False,
    label: str = "source_pipeline_commit",
) -> str | None:
    """Validate a 7..40 lowercase hex git commit SHA (strict non-empty by default)."""
    if value is None:
        if allow_null:
            return None
        raise OutcomesLayoutError(
            f"{label} is missing/null (pass --pipeline-commit <sha> or allow_null_commit=True)"
        )
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        raise OutcomesLayoutError(
            f"{label} must be 7..40 lowercase hex characters matching {COMMIT_RE.pattern}, got {value!r}"
        )
    return value


def _strict_int_or_none(value: Any, label: str) -> int | None:
    """Return ``int`` or ``None``; explicitly rejects ``bool`` and floats."""
    if value is None:
        return None
    if type(value) is not int:
        raise OutcomesLayoutError(f"{label} must be an int or None, got {type(value).__name__}: {value!r}")
    return value


def _require_strict_bool(value: Any, label: str) -> bool:
    """Require an explicit Python ``bool``; missing/``None`` never defaults."""
    if type(value) is not bool:
        raise AuditFirewallError(
            f"{label} must be an explicit bool (True or False), got {type(value).__name__}: {value!r}"
        )
    return value


# ── 6-vector audit holdout firewall ──────────────────────────────────────────

# Native CT.gov API v2 top-level sections whose own schema happens to contain
# benign keys named "locations" (contactsLocationsModule.locations) or
# "citations" (moreInfoModule.citations).
_CTGOV_NATIVE_SECTIONS: frozenset[str] = frozenset({
    "protocolSection",
    "resultsSection",
    "annotationSection",
    "documentSection",
    "derivedSection",
})


def _find_forbidden_keys_recursive(
    obj: Any,
    prefix: str = "",
    *,
    skip_top_keys: AbstractSet[str] = frozenset(),
) -> list[str]:
    """Scan nested dicts/lists for any key in ``FORBIDDEN_AUDIT_KEYS``."""
    hits: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key_str = str(k)
            if not prefix and key_str in skip_top_keys:
                continue
            path = f"{prefix}.{key_str}" if prefix else key_str
            if key_str in FORBIDDEN_AUDIT_KEYS:
                hits.append(path)
            hits.extend(_find_forbidden_keys_recursive(v, path))
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            hits.extend(_find_forbidden_keys_recursive(item, f"{prefix}[{idx}]"))
    return hits


def _has_pdf_scraped_value(val: Any) -> bool:
    """Return True if a metadata field holds a non-empty PDF-scraped value."""
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, Mapping):
        # Module 4 ParsedDate dicts carry raw / iso_normalized / date_precision.
        raw = val.get("raw")
        iso = val.get("iso_normalized")
        if isinstance(raw, str) and raw.strip():
            return True
        if isinstance(iso, str) and iso.strip():
            return True
        return False
    return True


def _check_provenance_pdf_leak(nct_id: str, provenance: Any) -> None:
    """Vector 4b: reject PDF-scraped document fields on an audit holdout trial."""
    if not isinstance(provenance, Mapping):
        return
    for list_key in ("posted_documents", "large_docs"):
        docs = provenance.get(list_key)
        if not isinstance(docs, Sequence) or isinstance(docs, (str, bytes)):
            continue
        for idx, doc in enumerate(docs):
            if not isinstance(doc, Mapping):
                continue
            for scraped_key in ("version_label", "version_date_as_printed", "sha256"):
                if _has_pdf_scraped_value(doc.get(scraped_key)):
                    raise AuditFirewallError(
                        f"{nct_id}: in_audit holdout leak (vector 4) — longitudinal_provenance."
                        f"{list_key}[{idx}].{scraped_key} carries PDF-scraped metadata"
                    )


def assert_no_audit_leakage(
    *,
    nct_id: str,
    in_audit: Any,
    shard_record: Mapping[str, Any] | None = None,
    index_row: Mapping[str, Any] | None = None,
    document_profile: Any = None,
    decision_rule_locations: Any = None,
    longitudinal_provenance: Any = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> None:
    """Enforce the 6-vector ``in_audit`` holdout firewall.

    Raises ``AuditFirewallError`` (a subclass of ``OutcomesLayoutError`` and
    ``ValueError``) immediately on any violation:
      1. ``in_audit`` is not a strict ``bool`` (never silently defaulted).
      2. ``in_audit is True`` with non-``None`` ``decision_rule_locations``.
      3. ``in_audit is True`` with any forbidden LLM/assessment key or extra
         key outside ``SHARD_RECORD_KEYS`` / ``INDEX_ROW_KEYS``.
      4. ``in_audit is True`` with non-``None`` ``document_profile`` or
         PDF-scraped ``version_label`` / ``version_date_as_printed`` / ``sha256``
         inside ``longitudinal_provenance``.
      5. ``in_audit is True`` with leaking ``document_badge`` or
         ``census_badge`` fields on ``index_row``.
    (Vector 6 — cross-stream JSONL/mapping rejection — is enforced by
    ``assert_no_audit_cross_stream_leakage`` prior to staging.)
    """
    # Vector 1: explicit bool required.
    is_holdout = _require_strict_bool(in_audit, f"{nct_id}.in_audit")

    if shard_record is not None:
        if "in_audit" in shard_record:
            rec_flag = _require_strict_bool(shard_record["in_audit"], f"{nct_id}.shard_record.in_audit")
            if rec_flag != is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: shard_record.in_audit ({rec_flag}) disagrees with in_audit ({is_holdout})"
                )
        extra_rec_keys = sorted(set(shard_record.keys()) - set(SHARD_RECORD_KEYS))
        if extra_rec_keys:
            if is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 3) — unexpected shard_record keys: "
                    f"{', '.join(extra_rec_keys)}"
                )
            raise OutcomesLayoutError(
                f"{nct_id}: unexpected shard_record keys: {', '.join(extra_rec_keys)}"
            )

    if index_row is not None:
        if "in_audit" in index_row:
            row_flag = _require_strict_bool(index_row["in_audit"], f"{nct_id}.index_row.in_audit")
            if row_flag != is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: index_row.in_audit ({row_flag}) disagrees with in_audit ({is_holdout})"
                )
        extra_row_keys = sorted(set(index_row.keys()) - set(INDEX_ROW_KEYS))
        if extra_row_keys:
            if is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 3) — unexpected index_row keys: "
                    f"{', '.join(extra_row_keys)}"
                )
            raise OutcomesLayoutError(
                f"{nct_id}: unexpected index_row keys: {', '.join(extra_row_keys)}"
            )

    if not is_holdout:
        return

    # Holdout stub: the shard record carries nothing beyond nct_id, in_audit,
    # cohorts and shard (the fields that mirror the index row 1:1). Results,
    # documents, locations and provenance are all None (vectors 2 and 4 below
    # name the location and document cases; these two cover the rest).
    if shard_record is not None:
        for stub_key in ("reported_results", "longitudinal_provenance"):
            if shard_record.get(stub_key) is not None:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (stub) — shard_record.{stub_key} must be None, "
                    f"got {type(shard_record.get(stub_key)).__name__}"
                )
    if index_row is not None and index_row.get("has_reported_results") is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (stub) — index_row.has_reported_results must be None, "
            f"got {index_row.get('has_reported_results')!r}"
        )

    # Vector 2: decision_rule_locations must be strictly None.
    eff_locations = (
        decision_rule_locations
        if decision_rule_locations is not None
        else (shard_record.get("decision_rule_locations") if shard_record is not None else None)
    )
    if eff_locations is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 2) — decision_rule_locations must be None, "
            f"got {type(eff_locations).__name__}"
        )

    # Vector 3: forbidden LLM / assessment / worker / citation keys anywhere.
    for label, payload, skip_keys in (
        ("shard_record", shard_record, frozenset()),
        ("index_row", index_row, frozenset()),
        ("raw_payload", raw_payload, _CTGOV_NATIVE_SECTIONS),
    ):
        if payload is None:
            continue
        forbidden_hits = _find_forbidden_keys_recursive(payload, skip_top_keys=skip_keys)
        if forbidden_hits:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 3) — forbidden key(s) in {label}: "
                f"{', '.join(forbidden_hits[:5])}"
            )

    # Vector 4: PDF inspection / document_profile block.
    eff_profile = (
        document_profile
        if document_profile is not None
        else (shard_record.get("document_profile") if shard_record is not None else None)
    )
    if eff_profile is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 4) — document_profile must be None, "
            f"got {type(eff_profile).__name__}"
        )
    eff_prov = (
        longitudinal_provenance
        if longitudinal_provenance is not None
        else (shard_record.get("longitudinal_provenance") if shard_record is not None else None)
    )
    # The PDF-scraped check names the field that leaks; a holdout stub carries
    # no provenance at all, so anything that passes it is still refused.
    _check_provenance_pdf_leak(nct_id, eff_prov)
    if eff_prov is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 4) — longitudinal_provenance must be None for a "
            f"holdout stub, got {type(eff_prov).__name__}"
        )

    # Vector 5: index badge leak block.
    if index_row is not None:
        doc_badge = index_row.get("document_badge")
        if not isinstance(doc_badge, Mapping):
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — document_badge missing or not an object"
            )
        if doc_badge.get("readability_status") != "in_audit_holdout":
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — document_badge.readability_status is "
                f"{doc_badge.get('readability_status')!r}, must be 'in_audit_holdout'"
            )
        for hidden_field in ("redaction_flags", "doc_count", "total_physical_pages"):
            if doc_badge.get(hidden_field) is not None:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 5) — document_badge.{hidden_field} must be None, "
                    f"got {doc_badge.get(hidden_field)!r}"
                )

        census_badge = index_row.get("census_badge")
        if not isinstance(census_badge, Mapping):
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge missing or not an object"
            )
        if census_badge.get("census_status") != "in_audit_holdout":
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.census_status is "
                f"{census_badge.get('census_status')!r}, must be 'in_audit_holdout'"
            )
        if census_badge.get("status_counts") is not None:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.status_counts must be None, "
                f"got {census_badge.get('status_counts')!r}"
            )
        if census_badge.get("has_second_pass") is not False:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.has_second_pass must be False, "
                f"got {census_badge.get('has_second_pass')!r}"
            )


# Alias matching architecture spec #61 naming.
enforce_audit_firewall = assert_no_audit_leakage


def assert_no_audit_cross_stream_leakage(
    audit_nct_ids: AbstractSet[str],
    *,
    documents_by_nct: Mapping[str, Any] | None = None,
    locations_by_nct: Mapping[str, Any] | None = None,
    profiles_by_nct: Mapping[str, Any] | None = None,
    provenance_by_nct: Mapping[str, Any] | None = None,
    population_parsed_by_nct: Mapping[str, Any] | None = None,
) -> None:
    """Vector 6: abort BEFORE touching output dirs if any audit NCT appears in
    any side stream (documents, locations, document profiles, provenance or
    parsed populations).

    A holdout trial is written as a stub (see the module notes), so a side
    record for one could only be dropped, and dropping it silently would hide
    that the input carried holdout material. It is refused instead. The
    message gives counts and stream names only, never a holdout trial's ID.
    """
    if not audit_nct_ids:
        return
    for stream_name, mapping in (
        ("locations", locations_by_nct),
        ("documents", documents_by_nct),
        ("document_profiles", profiles_by_nct),
        ("provenance", provenance_by_nct),
        ("population_parsed", population_parsed_by_nct),
    ):
        if not mapping:
            continue
        leaked = sum(1 for nct in mapping if nct in audit_nct_ids)
        if leaked:
            raise AuditFirewallError(
                f"in_audit holdout leak (vector 6) — {leaked} audit holdout trial(s) present "
                f"in {stream_name} stream (IDs withheld); aborting before touching output directory"
            )


# ── side-input identity: nested trial IDs and in_audit flags ─────────────────

def _normalized_key(key: Any) -> str:
    """Lower-case a key and drop ``_``, ``-`` and spaces (``inAudit`` -> ``inaudit``)."""
    return re.sub(r"[\s_\-]", "", str(key)).lower()


def _iter_keyed_values(obj: Any, wanted: str, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(path, value)`` for every key anywhere in ``obj`` whose
    ``_normalized_key`` equals ``wanted`` (so ``nct_id``, ``nctId`` and
    ``NCT-ID`` are all read as the trial-ID key, and ``in_audit``, ``inAudit``
    and ``IN-AUDIT`` as the holdout flag).
    """
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            sub = f"{path}.{key}" if path else str(key)
            if _normalized_key(key) == wanted:
                yield sub, value
            yield from _iter_keyed_values(value, wanted, sub)
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            yield from _iter_keyed_values(item, wanted, f"{path}[{idx}]")


def check_side_input_identity(
    obj: Any,
    *,
    expected_nct_id: str,
    expected_in_audit: bool,
    label: str,
) -> None:
    """Refuse a side input (document profile, locations, provenance, parsed
    populations) that names another trial or contradicts the trial's
    ``in_audit``.

    Method: every key read as a trial ID, at any depth, must hold a strict
    ASCII ``^NCT[0-9]{8}$`` string equal to ``expected_nct_id``: a nested
    document or location row naming another trial would otherwise be filed
    under this one. Every key read as the holdout flag must be the bool
    ``expected_in_audit``; a side record saying ``in_audit: true`` for a trial
    the manifest marks false (or the reverse) is a conflict, refused as
    ``AuditFirewallError`` rather than resolved either way. Messages name the
    position, never the ID found there.
    """
    for where, value in _iter_keyed_values(obj, "nctid"):
        checked = strict_nct_id(value, f"{label}.{withhold_nct_ids(where)}")
        if checked != expected_nct_id:
            raise OutcomesLayoutError(
                f"{label}.{withhold_nct_ids(where)} names a different trial than the record it is "
                f"filed under; a nested row is never re-attributed"
            )
    for where, value in _iter_keyed_values(obj, "inaudit"):
        if type(value) is not bool or value is not expected_in_audit:
            raise AuditFirewallError(
                f"{label}.{withhold_nct_ids(where)} is {value!r} but the cohort manifest marks this "
                f"trial in_audit={expected_in_audit}; a conflicting holdout flag is refused"
            )


# ── cohort derivation ─────────────────────────────────────────────────────────

def _is_phase3(phase: Any) -> bool:
    """True when phase indicates a Phase 3 (or Phase 2/3) clinical trial."""
    if isinstance(phase, str):
        norm = re.sub(r"[\s_\-/]+", "", phase.upper())
        return "PHASE3" in norm
    if isinstance(phase, Sequence) and not isinstance(phase, (str, bytes)):
        return any(_is_phase3(p) for p in phase)
    return False


def derive_cohorts(
    explicit_cohorts: Sequence[str] | None = None,
    *,
    ecosystem_category: str | None = None,
    sponsor_class: str | None = None,
    phase: Any = None,
    protocol: bool = False,
    sap: bool = False,
    include_all_large_docs: bool = True,
) -> list[str]:
    """Return a non-empty sorted unique list of valid ``COHORT_RE`` slugs.

    Combines any explicit cohort memberships with deterministic derivations from
    Module 2 ``ecosystem_category``, registry ``sponsor_class``/``phase``, and
    large-document presence (``protocol`` or ``sap`` -> ``"all_large_docs"``).
    """
    found: set[str] = set()
    if explicit_cohorts is not None:
        if isinstance(explicit_cohorts, (str, bytes)) or not isinstance(explicit_cohorts, Sequence):
            raise OutcomesLayoutError(f"cohorts must be a sequence of strings, got {explicit_cohorts!r}")
        for raw_slug in explicit_cohorts:
            if not isinstance(raw_slug, str):
                raise OutcomesLayoutError(f"cohort slug must be a string, got {raw_slug!r}")
            slug = ECOSYSTEM_SECTOR_TO_COHORT.get(raw_slug, raw_slug)
            if not COHORT_RE.fullmatch(slug):
                raise OutcomesLayoutError(
                    f"invalid cohort slug {raw_slug!r}: must match {COHORT_RE.pattern}"
                )
            found.add(slug)

    if ecosystem_category is not None:
        mapped = ECOSYSTEM_SECTOR_TO_COHORT.get(ecosystem_category, ecosystem_category)
        if mapped == "industry":
            if _is_phase3(phase):
                found.add("industry_phase3")
        elif COHORT_RE.fullmatch(mapped):
            found.add(mapped)
        else:
            raise OutcomesLayoutError(f"invalid ecosystem_category slug: {ecosystem_category!r}")

    if isinstance(sponsor_class, str):
        sc_upper = sponsor_class.strip().upper()
        if sc_upper == "INDUSTRY" and _is_phase3(phase):
            found.add("industry_phase3")
        elif sc_upper == "NIH":
            found.add("nih")
        elif sc_upper == "FED":
            found.add("us_federal_other")
        elif sc_upper == "NETWORK":
            found.add("cooperative_group")

    if include_all_large_docs and (protocol or sap):
        found.add("all_large_docs")

    if not found:
        raise OutcomesLayoutError(
            "trial resolved to zero cohorts; pass explicit cohorts (the builder's --manifest-cohort) "
            "or valid sponsor/document metadata"
        )
    return sorted(found)


# ── section 1: reported_results ───────────────────────────────────────────────

def _normalize_primary_outcome_measure(
    measure: Mapping[str, Any],
    *,
    outcome_index: int,
    parsed_pop: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one CT.gov PRIMARY outcome measure verbatim (no rounding/coercion)."""
    def _pick(*keys: str) -> Any:
        for k in keys:
            if k in measure:
                return measure[k]
        return None

    groups = _pick("groups")
    denoms = _pick("denoms")
    classes = _pick("classes")
    analyses = _pick("analyses")

    parsed_out: dict[str, Any] | None
    if parsed_pop is not None:
        parsed_out = canonicalize_nested(dict(parsed_pop))
    elif "analysis_population_parsed" in measure and measure["analysis_population_parsed"] is not None:
        raw_p = measure["analysis_population_parsed"]
        parsed_out = canonicalize_nested(dict(raw_p)) if isinstance(raw_p, Mapping) else None
    else:
        parsed_out = None

    return canonicalize_nested({
        "outcome_index": outcome_index,
        "title": _pick("title"),
        "description": _pick("description"),
        "time_frame": _pick("time_frame", "timeFrame"),
        "reporting_status": _pick("reporting_status", "reportingStatus"),
        "param_type": _pick("param_type", "paramType"),
        "dispersion_type": _pick("dispersion_type", "dispersionType"),
        "unit_of_measure": _pick("unit_of_measure", "unitOfMeasure"),
        "population_description": _pick("population_description", "populationDescription"),
        "type_units_analyzed": _pick("type_units_analyzed", "typeUnitsAnalyzed"),
        "groups": list(groups) if isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)) else [],
        "denoms": list(denoms) if isinstance(denoms, Sequence) and not isinstance(denoms, (str, bytes)) else [],
        "classes": list(classes) if isinstance(classes, Sequence) and not isinstance(classes, (str, bytes)) else [],
        "analyses": list(analyses) if isinstance(analyses, Sequence) and not isinstance(analyses, (str, bytes)) else [],
        "analysis_population_parsed": parsed_out,
    })


def _extract_parsed_population_map(
    parsed_populations: Any,
) -> dict[int, dict[str, Any]]:
    """Index Module 2 ParsedOutcomePopulation outputs by outcome_index."""
    if parsed_populations is None:
        return {}
    items: Sequence[Any]
    if isinstance(parsed_populations, Mapping):
        raw_outcomes = parsed_populations.get("outcomes")
        if isinstance(raw_outcomes, Sequence) and not isinstance(raw_outcomes, (str, bytes)):
            items = raw_outcomes
        else:
            return {}
    elif hasattr(parsed_populations, "outcomes"):
        # Dataclass TrialAnalysisPopulations from Module 2.
        items = getattr(parsed_populations, "outcomes")
    elif isinstance(parsed_populations, Sequence) and not isinstance(parsed_populations, (str, bytes)):
        items = parsed_populations
    else:
        return {}

    by_index: dict[int, dict[str, Any]] = {}
    for pos, item in enumerate(items):
        if isinstance(item, Mapping):
            row = dict(item)
        elif hasattr(item, "__dataclass_fields__"):
            # Avoid importing dataclasses.asdict unnecessarily on arbitrary objects
            row = json.loads(json.dumps(item, default=lambda o: o.__dict__))
        else:
            continue
        # Only attach PRIMARY outcomes (or rows without outcome_type specified).
        otype = row.get("outcome_type")
        if otype is not None and str(otype).upper() != "PRIMARY":
            continue
        idx = row.get("outcome_index")
        key = idx if type(idx) is int else pos
        by_index[key] = row
    return by_index


def extract_reported_results(
    study: Mapping[str, Any],
    *,
    population_parser: Callable[[Mapping[str, Any]], Any] | None = None,
    parsed_populations: Any = None,
) -> dict[str, Any]:
    """Build the factual ``reported_results`` block from a CT.gov study or pre-built block.

    Preserves verbatim strings (including ``pValue``, confidence intervals, and
    ``populationDescription``) without rounding or evaluative claims. Absence
    is kept as ``None``.
    """
    # Support pre-built reported_results dict passed directly.
    if isinstance(study.get("reported_results"), Mapping):
        pre = study["reported_results"]
        reg = pre.get("registered_primary_outcomes", [])
        measures = pre.get("primary_outcome_measures", [])
        results_posted = pre.get("results_posted")
        if results_posted is None:
            # Not stated: posted measures show results were posted; with none,
            # whether results exist is unknown, not "no".
            results_posted = True if measures else None
        elif type(results_posted) is not bool:
            raise OutcomesLayoutError(
                f"reported_results.results_posted must be a bool or None, got {results_posted!r}"
            )
        pop_by_idx = _extract_parsed_population_map(parsed_populations)
        norm_measures = [
            _normalize_primary_outcome_measure(
                m,
                outcome_index=m["outcome_index"] if type(m.get("outcome_index")) is int else i,
                parsed_pop=pop_by_idx.get(
                    m["outcome_index"] if type(m.get("outcome_index")) is int else i
                ),
            )
            for i, m in enumerate(measures)
            if isinstance(m, Mapping)
        ]
        norm_reg = [
            canonicalize_nested({
                "outcome_index": r["outcome_index"] if type(r.get("outcome_index")) is int else i,
                "measure": str(r.get("measure") or ""),
                "time_frame": r.get("time_frame", r.get("timeFrame")),
                "description": r.get("description"),
            })
            for i, r in enumerate(reg)
            if isinstance(r, Mapping)
        ]
        return canonicalize_nested({
            "section_label": SECTION_LABEL_REPORTED_RESULTS,
            "results_posted": results_posted,
            "registered_primary_outcomes": norm_reg,
            "primary_outcome_measures": norm_measures,
        })

    protocol_sec = study.get("protocolSection") if isinstance(study.get("protocolSection"), Mapping) else {}
    results_sec = study.get("resultsSection") if isinstance(study.get("resultsSection"), Mapping) else {}

    outcomes_mod = (
        protocol_sec.get("outcomesModule")
        if isinstance(protocol_sec.get("outcomesModule"), Mapping)
        else {}
    )
    raw_primary_outcomes = outcomes_mod.get("primaryOutcomes")
    registered: list[dict[str, Any]] = []
    if isinstance(raw_primary_outcomes, Sequence) and not isinstance(raw_primary_outcomes, (str, bytes)):
        for idx, item in enumerate(raw_primary_outcomes):
            if not isinstance(item, Mapping):
                continue
            registered.append(
                canonicalize_nested({
                    "outcome_index": idx,
                    "measure": str(item.get("measure") or ""),
                    "time_frame": item.get("timeFrame", item.get("time_frame")),
                    "description": item.get("description"),
                })
            )

    if parsed_populations is None and population_parser is not None and results_sec:
        parsed_populations = population_parser(study)
    pop_by_idx = _extract_parsed_population_map(parsed_populations)

    om_mod = (
        results_sec.get("outcomeMeasuresModule")
        if isinstance(results_sec.get("outcomeMeasuresModule"), Mapping)
        else {}
    )
    raw_measures = om_mod.get("outcomeMeasures")
    primary_measures: list[dict[str, Any]] = []
    if isinstance(raw_measures, Sequence) and not isinstance(raw_measures, (str, bytes)):
        primary_pos = 0
        for raw_idx, measure in enumerate(raw_measures):
            if not isinstance(measure, Mapping):
                continue
            if str(measure.get("type") or "").upper() != "PRIMARY":
                continue
            parsed_pop = pop_by_idx.get(raw_idx) or pop_by_idx.get(primary_pos)
            primary_measures.append(
                _normalize_primary_outcome_measure(
                    measure,
                    outcome_index=primary_pos,
                    parsed_pop=parsed_pop,
                )
            )
            primary_pos += 1

    # results_posted is tri-state. True when the record carries a
    # resultsSection or hasResults is true; False only when hasResults is
    # explicitly false; None when the input says neither, as for a manifest row
    # built without its CT.gov record (the live outcomes index), so that
    # absence of the record is not published as "no results posted".
    has_results_flag = study.get("hasResults")
    if has_results_flag is not None and type(has_results_flag) is not bool:
        raise OutcomesLayoutError(f"hasResults must be a bool or absent, got {has_results_flag!r}")
    results_posted: bool | None
    if results_sec or has_results_flag is True:
        results_posted = True
    elif has_results_flag is False:
        results_posted = False
    else:
        results_posted = None
    return canonicalize_nested({
        "section_label": SECTION_LABEL_REPORTED_RESULTS,
        "results_posted": results_posted,
        "registered_primary_outcomes": registered,
        "primary_outcome_measures": primary_measures,
    })


# ── section 2: document_profile (Module 1 + legacy documents.jsonl adapter) ──

def _reject_embedded_raw_text(obj: Mapping[str, Any], label: str) -> None:
    """Reject raw PDF page text arrays so outcomes shards stay compact."""
    for bad_key in FORBIDDEN_RAW_TEXT_KEYS:
        if bad_key in obj:
            raise OutcomesLayoutError(
                f"{label}: embedded raw PDF text key {bad_key!r} is forbidden in document_profile"
            )
    if "pages" in obj and isinstance(obj["pages"], Sequence) and not isinstance(obj["pages"], (str, bytes)):
        raise OutcomesLayoutError(
            f"{label}: embedded raw PDF text key 'pages' is forbidden in document_profile"
        )


def _summarize_doc_readability(docs: Sequence[Mapping[str, Any]]) -> str:
    """Compute trial readability_summary matching Module 1 (#62) rules exactly."""
    if not docs:
        return "no_document"
    statuses = [str(d["readability_status"]) for d in docs]
    if all(s == "download_failed" for s in statuses):
        return "download_failed"
    if all(s == "machine_readable" for s in statuses):
        return "machine_readable"
    if all(s in {"image_only_scan", "garbled_cmap"} for s in statuses):
        return "garbled_cmap" if "garbled_cmap" in statuses else "image_only_scan"
    return "partial_image_or_redacted"


def _normalize_single_document(
    raw_doc: Mapping[str, Any],
    *,
    expected_nct_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one Module 1 TrialDocumentRecord or legacy documents.jsonl row.

    A row's own ``nct_id``, when present, must be a strict ASCII NCT ID and,
    when the caller knows the trial (``expected_nct_id``), that trial's: a
    document row naming another trial is refused, never filed under this one.
    """
    _reject_embedded_raw_text(raw_doc, "document")
    if "nct_id" in raw_doc:
        row_nct = strict_nct_id(raw_doc["nct_id"], "document.nct_id")
        if expected_nct_id is not None and row_nct != expected_nct_id:
            raise OutcomesLayoutError(
                "document.nct_id names a different trial than the document profile it is in"
            )
    filename = raw_doc.get("filename")
    if not isinstance(filename, str) or not filename.strip() or "/" in filename or "\\" in filename:
        raise OutcomesLayoutError(f"document filename must be a safe basename, got {filename!r}")

    doc_type = raw_doc.get("doc_type")
    if doc_type not in DOC_TYPES:
        raise OutcomesLayoutError(f"{filename}: doc_type must be in {DOC_TYPES}, got {doc_type!r}")

    http_status = _strict_int_or_none(raw_doc.get("http_status"), f"{filename}.http_status")
    byte_count = _strict_int_or_none(raw_doc.get("bytes"), f"{filename}.bytes")
    physical_pages = _strict_int_or_none(raw_doc.get("physical_pages"), f"{filename}.physical_pages")
    if physical_pages is not None and physical_pages < 0:
        raise OutcomesLayoutError(f"{filename}: physical_pages cannot be negative ({physical_pages})")

    sha256 = raw_doc.get("sha256")
    if sha256 is not None:
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise OutcomesLayoutError(f"{filename}: invalid sha256 {sha256!r}")

    def _int_list_or_none(val: Any, key_name: str) -> list[int] | None:
        if val is None:
            return None
        if not isinstance(val, Sequence) or isinstance(val, (str, bytes)):
            raise OutcomesLayoutError(f"{filename}.{key_name} must be a list of ints or None")
        out: list[int] = []
        for item in val:
            if type(item) is not int or item < 1:
                raise OutcomesLayoutError(
                    f"{filename}.{key_name} must contain positive ints, got {item!r}"
                )
            out.append(item)
        return sorted(set(out))

    pages_without_text = _int_list_or_none(raw_doc.get("pages_without_text"), "pages_without_text")
    pages_selected = _int_list_or_none(raw_doc.get("pages_selected"), "pages_selected")
    pages_deferred = _int_list_or_none(raw_doc.get("pages_deferred"), "pages_deferred")

    raw_markers = raw_doc.get("redaction_markers")
    if raw_markers is None:
        redaction_markers: list[str] = []
    elif isinstance(raw_markers, Sequence) and not isinstance(raw_markers, (str, bytes)):
        bad_markers = [m for m in raw_markers if m not in REDACTION_FLAGS]
        if bad_markers:
            raise OutcomesLayoutError(f"{filename}: unknown redaction_markers {bad_markers!r}")
        redaction_markers = sorted(set(str(m) for m in raw_markers))
    else:
        raise OutcomesLayoutError(f"{filename}.redaction_markers must be a list of flags")

    raw_status = raw_doc.get("readability_status")
    if raw_status is not None:
        if raw_status not in READABILITY_STATUSES:
            raise OutcomesLayoutError(
                f"{filename}: readability_status {raw_status!r} not in {READABILITY_STATUSES}"
            )
        readability_status = str(raw_status)
    else:
        # Infer deterministically for legacy rule-census documents.jsonl rows.
        if (http_status is not None and http_status != 200) or sha256 is None or physical_pages in (None, 0):
            readability_status = "download_failed"
        elif pages_without_text is not None and physical_pages is not None and len(pages_without_text) == physical_pages:
            readability_status = "image_only_scan"
        elif (pages_without_text and len(pages_without_text) > 0) or redaction_markers:
            readability_status = "partial_image_or_redacted"
        else:
            readability_status = "machine_readable"

    def _opt_str(val: Any) -> str | None:
        if val is None:
            return None
        s = str(val)
        return s if s != "" else None

    return canonicalize_nested({
        "filename": filename,
        "doc_type": doc_type,
        "url": _opt_str(raw_doc.get("url")),
        "http_status": http_status,
        "sha256": sha256,
        "bytes": byte_count,
        "physical_pages": physical_pages,
        "pages_without_text": pages_without_text,
        "readability_status": readability_status,
        "redaction_markers": redaction_markers,
        "version_label": _opt_str(raw_doc.get("version_label")),
        "version_date_as_printed": _opt_str(raw_doc.get("version_date_as_printed")),
        "upload_date": _opt_str(raw_doc.get("upload_date")),
        "pages_selected": pages_selected,
        "pages_deferred": pages_deferred,
        "retrieved_at": _opt_str(raw_doc.get("retrieved_at")),
    })


def normalize_document_profile(
    raw: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    expected_nct_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a Module 1 ``TrialDocumentProfile`` or legacy ``documents.jsonl`` rows.

    Rejects embedded raw PDF text arrays (``page_texts``, ``raw_pages``, etc.),
    and any profile or document row whose ``nct_id`` is not strict or, given
    ``expected_nct_id``, not that trial's.
    """
    if raw is None:
        return None
    if isinstance(raw, Mapping) and "nct_id" in raw:
        profile_nct = strict_nct_id(raw["nct_id"], "document_profile.nct_id")
        if expected_nct_id is not None and profile_nct != expected_nct_id:
            raise OutcomesLayoutError("document_profile.nct_id names a different trial")
        expected_nct_id = profile_nct

    indexer_rules_version: str | None = None
    explicit_summary: str | None = None
    explicit_flags: list[str] | None = None
    raw_docs: Sequence[Mapping[str, Any]]

    if isinstance(raw, Mapping):
        _reject_embedded_raw_text(raw, "document_profile")
        if "documents" not in raw or not isinstance(raw["documents"], Sequence) or isinstance(raw["documents"], (str, bytes)):
            raise OutcomesLayoutError("document_profile mapping must contain a 'documents' list")
        raw_docs = raw["documents"]
        ver = raw.get("indexer_rules_version")
        indexer_rules_version = str(ver) if isinstance(ver, str) and ver.strip() else None
        if raw.get("readability_summary") is not None:
            rs = raw["readability_summary"]
            if rs not in READABILITY_STATUSES:
                raise OutcomesLayoutError(
                    f"document_profile.readability_summary {rs!r} not in {READABILITY_STATUSES}"
                )
            explicit_summary = str(rs)
        if raw.get("redaction_flags") is not None:
            rf = raw["redaction_flags"]
            if not isinstance(rf, Sequence) or isinstance(rf, (str, bytes)):
                raise OutcomesLayoutError("document_profile.redaction_flags must be a list")
            bad = [f for f in rf if f not in REDACTION_FLAGS]
            if bad:
                raise OutcomesLayoutError(f"document_profile.redaction_flags contains unknown flags {bad!r}")
            explicit_flags = sorted(set(str(f) for f in rf))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        raw_docs = raw
    else:
        raise OutcomesLayoutError(
            f"document_profile must be a mapping, list of document rows, or None; got {type(raw).__name__}"
        )

    docs: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()
    for item in raw_docs:
        if not isinstance(item, Mapping):
            raise OutcomesLayoutError("every item in document_profile.documents must be an object")
        norm = _normalize_single_document(item, expected_nct_id=expected_nct_id)
        fn = norm["filename"]
        if fn in seen_filenames:
            raise OutcomesLayoutError(f"duplicate document filename in document_profile: {fn!r}")
        seen_filenames.add(fn)
        docs.append(norm)
    docs.sort(key=lambda d: d["filename"])

    union_flags = sorted({flag for d in docs for flag in d["redaction_markers"]} | set(explicit_flags or []))
    summary = explicit_summary if explicit_summary is not None else _summarize_doc_readability(docs)
    return canonicalize_nested({
        "indexer_rules_version": indexer_rules_version,
        "readability_summary": summary,
        "redaction_flags": union_flags,
        "documents": docs,
    })


# ── section 3: decision_rule_locations (locations.jsonl adapter) ──────────────

def citation_problems(
    cit: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    pages_not_searched: Any,
) -> list[str]:
    """Every problem with one decision-rule citation ([] when it is sound).

    Method: the 6-key shape and quotation hygiene are Module 4's
    ``validate_citation_shape`` (optional strings may be ``None``), so this
    module cannot accept a citation the verifier refuses: ``None``, ``'x'``,
    ``{}``, a non-``.pdf`` filename, an ``int`` printed page, an ellipsis in any
    Unicode spelling. Only when the shape is sound are the two checks that need
    this module's context added: the citation must name a document of the
    trial's ``document_profile`` (same sha256, page within its page count),
    and it may not land on a page the row lists as not searched. The same
    function serves normalization (which raises) and the on-disk check
    (which reports), so the two cannot drift apart.
    """
    shape_errors = validate_citation_shape(cit, allow_none_optional_strings=True)
    if shape_errors:
        return list(shape_errors)
    errors: list[str] = []
    filename = cit["filename"]
    sha256 = cit["sha256"]
    physical_page = cit["physical_page"]
    if docs_by_filename is not None:
        if filename not in docs_by_filename:
            errors.append(f"citation references unknown document {filename!r} not in document_profile")
        else:
            doc = docs_by_filename[filename]
            if doc.get("sha256") != sha256:
                errors.append(
                    f"citation sha256 for {filename!r} ({sha256}) does not match document_profile "
                    f"({doc.get('sha256')})"
                )
            max_pages = doc.get("physical_pages")
            if type(max_pages) is int and not (1 <= physical_page <= max_pages):
                errors.append(
                    f"citation physical_page {physical_page} out of bounds 1..{max_pages} for {filename!r}"
                )
    if isinstance(pages_not_searched, Mapping):
        unsearched = pages_not_searched.get(filename)
        if isinstance(unsearched, Sequence) and physical_page in unsearched:
            errors.append(f"citation ({filename}, p.{physical_page}) falls inside pages_not_searched")
    elif isinstance(pages_not_searched, Sequence) and not isinstance(pages_not_searched, (str, bytes)):
        if physical_page in pages_not_searched:
            errors.append(f"citation ({filename}, p.{physical_page}) falls inside pages_not_searched")
    return errors


def _normalize_citation(
    raw_cit: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    pages_not_searched: Any,
) -> dict[str, Any]:
    """Validate one 6-key decision-rule citation (``citation_problems``) and
    return it with sorted keys; raise ``OutcomesLayoutError`` on any problem.
    """
    errors = citation_problems(
        raw_cit,
        docs_by_filename=docs_by_filename,
        pages_not_searched=pages_not_searched,
    )
    if errors:
        raise OutcomesLayoutError("citation refused: " + "; ".join(errors))
    return canonicalize_nested({key: raw_cit[key] for key in CITATION_REQUIRED_KEYS})


def _is_non_empty_unsearched(pages_not_searched: Any) -> bool:
    """True if pages_not_searched indicates any unsearched pages."""
    if pages_not_searched is None:
        return False
    if isinstance(pages_not_searched, Mapping):
        return any(bool(v) for v in pages_not_searched.values())
    if isinstance(pages_not_searched, Sequence) and not isinstance(pages_not_searched, (str, bytes)):
        return len(pages_not_searched) > 0
    return bool(pages_not_searched)


def _normalize_location_pass(
    rows: Sequence[Mapping[str, Any]],
    *,
    pass_label: str,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    has_posted_documents: bool = False,
    pass_name: str | None = None,
    expected_nct_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate and sort one pass's decision-rule location rows.

    The status-to-citation rule is Module 4's ``validate_status_citations``
    with ``has_posted_documents`` set from the trial's own evidence of posted
    documents: a status in ``CITATION_REQUIRED_STATUSES`` needs a citation,
    one in ``CITATION_FORBIDDEN_STATUSES`` may carry none, ``NO_DOCUMENT`` is
    refused when documents are posted, and any citation is refused when none
    are. A row that states its own ``pass`` must agree with the pass it is
    filed under (``pass_name``), and a row's ``nct_id`` must be the trial's.
    """
    has_posted_docs_flag = _require_strict_bool(
        has_posted_documents, f"{pass_label}.has_posted_documents"
    )
    effective_has_docs = has_posted_docs_flag or bool(docs_by_filename)
    seen_pairs: set[tuple[int, str]] = set()
    normalized: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise OutcomesLayoutError(f"{pass_label}: location row must be an object")
        if pass_name is not None and "pass" in raw_row and raw_row["pass"] != pass_name:
            raise OutcomesLayoutError(
                f"{pass_label}: a row stating pass {raw_row['pass']!r} is filed under {pass_name!r}; "
                f"a row is never re-filed into another pass"
            )
        if "nct_id" in raw_row:
            row_nct = strict_nct_id(raw_row["nct_id"], f"{pass_label}.nct_id")
            if expected_nct_id is not None and row_nct != expected_nct_id:
                raise OutcomesLayoutError(
                    f"{pass_label}: a location row names a different trial than its record"
                )
        outcome_idx = raw_row.get("primary_outcome_index")
        if type(outcome_idx) is not int or outcome_idx < 0:
            raise OutcomesLayoutError(
                f"{pass_label}: primary_outcome_index must be a non-negative int, got {outcome_idx!r}"
            )
        element = raw_row.get("element")
        if element not in _ELEMENT_ORDER:
            raise OutcomesLayoutError(f"{pass_label}: unknown element {element!r}")
        pair = (outcome_idx, str(element))
        if pair in seen_pairs:
            raise OutcomesLayoutError(
                f"{pass_label}: duplicate (primary_outcome_index, element) entry {pair}"
            )
        seen_pairs.add(pair)

        status = raw_row.get("status")
        if status not in LOCATION_STATUSES:
            raise OutcomesLayoutError(f"{pass_label}: unknown status {status!r}")

        pages_searched = raw_row.get("pages_searched")
        pages_not_searched = raw_row.get("pages_not_searched", [])
        has_unsearched = _is_non_empty_unsearched(pages_not_searched)

        if pages_searched == "all" and has_unsearched:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: pages_searched=='all' contradicts non-empty pages_not_searched"
            )
        if status == "NOT_FOUND" and has_unsearched:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: status 'NOT_FOUND' is forbidden when pages_not_searched is non-empty"
            )

        raw_citations = raw_row.get("citations", [])
        if not isinstance(raw_citations, Sequence) or isinstance(raw_citations, (str, bytes)):
            raise OutcomesLayoutError(f"{pass_label} {pair}: citations must be a list")
        citations = [
            _normalize_citation(
                c,
                docs_by_filename=docs_by_filename,
                pages_not_searched=pages_not_searched,
            )
            for c in raw_citations
        ]
        rule_errors = validate_status_citations(
            str(status), len(citations), has_posted_documents=effective_has_docs
        )
        if rule_errors:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: forbidden by the status-to-citation rule: " + "; ".join(rule_errors)
            )

        raw_text_source = raw_row.get("text_source")
        if raw_text_source is not None and not isinstance(raw_text_source, str):
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: text_source must be a string or None, got {raw_text_source!r}"
            )
        text_source = raw_text_source.strip() if isinstance(raw_text_source, str) else ""

        title = raw_row.get("primary_outcome_title")
        note = raw_row.get("note")
        worker = raw_row.get("worker")
        row_out: dict[str, Any] = {
            "primary_outcome_index": outcome_idx,
            "primary_outcome_title": str(title) if title is not None else None,
            "element": str(element),
            "status": str(status),
            "citations": citations,
            "pages_searched": pages_searched,
            "pages_not_searched": pages_not_searched,
            "note": str(note) if note is not None else None,
            "worker": str(worker) if worker is not None else None,
        }
        if text_source:
            row_out["text_source"] = text_source
        normalized.append(canonicalize_nested(row_out))
    normalized.sort(key=lambda r: (r["primary_outcome_index"], _ELEMENT_ORDER[r["element"]]))
    return normalized


def _resolve_effective_pass(
    primary_pass: Sequence[Mapping[str, Any]],
    ocr_recovery_pass: Sequence[Mapping[str, Any]] | None,
    adjudication_pass: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Resolve each ``(primary_outcome_index, element)`` cell under precedence
    ``primary -> ocr_recovery -> adjudication`` without mutating any pass row.
    """
    resolved: dict[tuple[int, str], dict[str, Any]] = {}
    layers: tuple[tuple[str, Sequence[Mapping[str, Any]] | None], ...] = (
        ("primary", primary_pass),
        ("ocr_recovery", ocr_recovery_pass),
        ("adjudication", adjudication_pass),
    )
    for source_pass, rows in layers:
        if not rows:
            continue
        for row in rows:
            pair = (int(row["primary_outcome_index"]), str(row["element"]))
            resolved[pair] = canonicalize_nested({
                **dict(row),
                "source_pass": source_pass,
            })
    ordered_pairs = sorted(resolved, key=lambda p: (p[0], _ELEMENT_ORDER[p[1]]))
    return [resolved[pair] for pair in ordered_pairs]


# Pass sections a pre-grouped decision_rule_locations object may carry, and
# the pass name each one's rows belong to.
GROUPED_PASS_KEYS: dict[str, str] = {
    "primary_pass": "primary",
    "second_pass": "second",
    "ocr_recovery_pass": "ocr_recovery",
    "adjudication_pass": "adjudication",
}
# Everything else a pre-grouped object may carry. Any other key is refused:
# an unknown section (an "ocr_pass", an "adjudication" without "_pass") would
# otherwise be dropped without a word, and with it that pass's rows.
GROUPED_OTHER_KEYS: frozenset[str] = frozenset({"nct_id", "in_audit", "census_status"})

# The keys of the normalized block, as written into shards.
LOCATIONS_BLOCK_KEYS: frozenset[str] = frozenset({
    "census_status",
    "elements_expected_per_outcome",
    "primary_pass",
    "second_pass",
    "ocr_recovery_pass",
    "adjudication_pass",
    "effective_pass",
})


def census_status_from_effective_pass(
    effective_pass: Sequence[Mapping[str, Any]],
    primary_outcomes_count: int | None,
) -> str:
    """Compute a trial's census status from its resolved cells.

    Method: no cells -> ``not_started``; every cell ``NO_DOCUMENT`` ->
    ``no_document``; otherwise ``complete`` when every element of every
    primary outcome has a cell (outcomes ``0..primary_outcomes_count-1`` when
    the count is known, else every outcome that has any cell), and ``partial``
    when any is missing. The status is always computed, never taken from the
    input (a supplied one must agree, see ``normalize_decision_rule_locations``),
    and the on-disk check recomputes it the same way.
    """
    if not effective_pass:
        return "not_started"
    if all(r.get("status") == "NO_DOCUMENT" for r in effective_pass):
        return "no_document"
    got_pairs = {(r.get("primary_outcome_index"), r.get("element")) for r in effective_pass}
    if primary_outcomes_count is not None and primary_outcomes_count > 0:
        expected_pairs = {
            (idx, elem)
            for idx in range(primary_outcomes_count)
            for elem in ELEMENTS
        }
        return "complete" if expected_pairs.issubset(got_pairs) else "partial"
    by_outcome: dict[Any, set[Any]] = {}
    for idx, elem in got_pairs:
        by_outcome.setdefault(idx, set()).add(elem)
    all_full = bool(by_outcome) and all(elems == set(ELEMENTS) for elems in by_outcome.values())
    return "complete" if all_full else "partial"


def _rows_with_citations(passes: Mapping[str, Any]) -> int:
    """Count rows, over every pass list in ``passes``, that carry any citation."""
    count = 0
    for rows in passes.values():
        if isinstance(rows, list):
            count += sum(
                1 for r in rows
                if isinstance(r, Mapping) and isinstance(r.get("citations"), list) and r["citations"]
            )
    return count


def normalize_decision_rule_locations(
    raw: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    document_profile: Mapping[str, Any] | None = None,
    primary_outcomes_count: int | None = None,
    has_posted_documents: bool = False,
    expected_nct_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize decision-rule location records into the unified shard block.

    Accepts either a sequence of ``locations.jsonl`` rows (grouped by ``pass``:
    ``"primary"`` / ``"second"`` / ``"ocr_recovery"`` / ``"adjudication"``) or a
    pre-grouped dict containing ``primary_pass`` and optional ``second_pass``,
    ``ocr_recovery_pass``, and ``adjudication_pass``. Retains every pass
    append-only and computes deterministic ``effective_pass`` under precedence
    ``primary -> ocr_recovery -> adjudication``.

    Refusals beyond the per-row rules (``_normalize_location_pass``):
    - a pre-grouped object with any key outside ``GROUPED_PASS_KEYS`` and
      ``GROUPED_OTHER_KEYS`` (its section would be dropped);
    - a pre-grouped ``census_status`` that differs from the one computed from
      the rows (``census_status_from_effective_pass``): a supplied status is
      never trusted;
    - ``no_document`` with a citation in any pass, and ``no_document`` when the
      trial has posted documents.
    """
    has_posted_docs_flag = _require_strict_bool(
        has_posted_documents, "decision_rule_locations.has_posted_documents"
    )
    if raw is None:
        return None

    docs_by_filename: dict[str, Mapping[str, Any]] | None = None
    if isinstance(document_profile, Mapping):
        docs = document_profile.get("documents")
        if isinstance(docs, Sequence) and not isinstance(docs, (str, bytes)):
            docs_by_filename = {
                str(d["filename"]): d
                for d in docs
                if isinstance(d, Mapping) and isinstance(d.get("filename"), str)
            }
    effective_has_docs = has_posted_docs_flag or bool(docs_by_filename)

    raw_by_pass: dict[str, list[Mapping[str, Any]]] = {name: [] for name in GROUPED_PASS_KEYS}
    supplied_census_status: str | None = None

    if isinstance(raw, Mapping):
        unknown_keys = sorted(
            str(k) for k in raw if k not in GROUPED_PASS_KEYS and k not in GROUPED_OTHER_KEYS
        )
        if unknown_keys:
            raise OutcomesLayoutError(
                f"decision_rule_locations: unknown key(s) {unknown_keys} in a pre-grouped object; the "
                f"known passes are {sorted(GROUPED_PASS_KEYS)} (an unknown section would be dropped)"
            )
        if "nct_id" in raw:
            grouped_nct = strict_nct_id(raw["nct_id"], "decision_rule_locations.nct_id")
            if expected_nct_id is not None and grouped_nct != expected_nct_id:
                raise OutcomesLayoutError("decision_rule_locations.nct_id names a different trial")
            expected_nct_id = grouped_nct
        if "in_audit" in raw and type(raw["in_audit"]) is not bool:
            raise AuditFirewallError(
                f"decision_rule_locations.in_audit must be a bool, got {raw['in_audit']!r}"
            )
        if "primary_pass" not in raw or not isinstance(raw["primary_pass"], Sequence) or isinstance(raw["primary_pass"], (str, bytes)):
            raise OutcomesLayoutError("decision_rule_locations dict must contain 'primary_pass' list")
        for pass_key in GROUPED_PASS_KEYS:
            val = raw.get(pass_key)
            if val is None:
                continue
            if not isinstance(val, Sequence) or isinstance(val, (str, bytes)):
                raise OutcomesLayoutError(f"decision_rule_locations.{pass_key} must be a list or None")
            raw_by_pass[pass_key].extend(val)
        cs = raw.get("census_status")
        if cs is not None:
            if cs not in CENSUS_STATUSES:
                raise OutcomesLayoutError(f"invalid decision_rule_locations.census_status: {cs!r}")
            supplied_census_status = str(cs)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if len(raw) == 0:
            return None
        pass_to_key = {name: key for key, name in GROUPED_PASS_KEYS.items()}
        for row in raw:
            if not isinstance(row, Mapping):
                raise OutcomesLayoutError("each row in locations must be an object")
            pass_name = row.get("pass", "primary")
            if pass_name not in ALLOWED_PASSES:
                raise OutcomesLayoutError(f"unknown location pass {pass_name!r}; expected one of {ALLOWED_PASSES}")
            raw_by_pass[pass_to_key[pass_name]].append(row)
    else:
        raise OutcomesLayoutError(
            f"decision_rule_locations must be a mapping, list of location rows, or None; got {type(raw).__name__}"
        )

    normalized_by_pass: dict[str, list[dict[str, Any]] | None] = {}
    for pass_key, pass_name in GROUPED_PASS_KEYS.items():
        rows = raw_by_pass[pass_key]
        if pass_key != "primary_pass" and not rows:
            normalized_by_pass[pass_key] = None
            continue
        normalized_by_pass[pass_key] = _normalize_location_pass(
            rows,
            pass_label=pass_key,
            docs_by_filename=docs_by_filename,
            has_posted_documents=effective_has_docs,
            pass_name=pass_name,
            expected_nct_id=expected_nct_id,
        )
    primary_pass = normalized_by_pass["primary_pass"] or []
    effective_pass = _resolve_effective_pass(
        primary_pass,
        normalized_by_pass["ocr_recovery_pass"],
        normalized_by_pass["adjudication_pass"],
    )

    census_status = census_status_from_effective_pass(effective_pass, primary_outcomes_count)
    if supplied_census_status is not None and supplied_census_status != census_status:
        raise OutcomesLayoutError(
            f"decision_rule_locations: supplied census_status {supplied_census_status!r} disagrees with "
            f"{census_status!r} computed from the rows; a supplied census_status is never trusted"
        )

    if census_status == "no_document" and effective_has_docs:
        raise OutcomesLayoutError(
            "decision_rule_locations: census_status 'no_document' is forbidden when posted document(s) are present"
        )
    if census_status == "no_document":
        cited_rows = _rows_with_citations(normalized_by_pass)
        if cited_rows:
            raise OutcomesLayoutError(
                f"decision_rule_locations: census_status 'no_document' is forbidden with citations "
                f"({cited_rows} row(s) in some pass cite a document)"
            )

    return canonicalize_nested({
        "census_status": census_status,
        "elements_expected_per_outcome": list(ELEMENTS),
        "primary_pass": primary_pass,
        "second_pass": normalized_by_pass["second_pass"],
        "ocr_recovery_pass": normalized_by_pass["ocr_recovery_pass"],
        "adjudication_pass": normalized_by_pass["adjudication_pass"],
        "effective_pass": effective_pass,
    })


# ── section 4: longitudinal_provenance (Module 4 adapter) ─────────────────────

def _validate_date_precisions_recursive(obj: Any, path: str = "longitudinal_provenance") -> None:
    """Validate every ``date_precision`` field is in ``DATE_PRECISIONS``."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            sub = f"{path}.{k}"
            if k == "date_precision" and v not in DATE_PRECISIONS:
                raise OutcomesLayoutError(
                    f"{sub} must be in {DATE_PRECISIONS}, got {v!r}"
                )
            _validate_date_precisions_recursive(v, sub)
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for i, item in enumerate(obj):
            _validate_date_precisions_recursive(item, f"{path}[{i}]")


def normalize_longitudinal_provenance(
    raw: Mapping[str, Any] | None,
    *,
    in_audit: bool = False,
) -> dict[str, Any] | None:
    """Normalize Module 4 ``LongitudinalProvenanceRecord`` or direct provenance block.

    Bridges Module 4's ``large_docs`` / ``linked_publications`` /
    ``primary_completion_to_results_delta`` into the unified shard keys
    ``{registry_milestones, posted_documents, publications, date_deltas}``
    without dropping dated milestones or precision metadata.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise OutcomesLayoutError(
            f"longitudinal_provenance must be an object or None, got {type(raw).__name__}"
        )

    _validate_date_precisions_recursive(raw)
    if in_audit:
        _check_provenance_pdf_leak(str(raw.get("nct_id") or "<trial>"), raw)

    # Support both Module 4 LongitudinalProvenanceRecord and direct Module 3 shape.
    registry_milestones = raw.get("registry_milestones")
    if not isinstance(registry_milestones, Mapping):
        raise OutcomesLayoutError("longitudinal_provenance must carry a 'registry_milestones' object")

    posted_docs_raw = raw.get("posted_documents")
    if posted_docs_raw is None:
        posted_docs_raw = raw.get("large_docs", [])
    if not isinstance(posted_docs_raw, Sequence) or isinstance(posted_docs_raw, (str, bytes)):
        raise OutcomesLayoutError("longitudinal_provenance posted_documents/large_docs must be a list")

    pubs_raw = raw.get("publications")
    if pubs_raw is None:
        pubs_raw = raw.get("linked_publications", [])
    if not isinstance(pubs_raw, Sequence) or isinstance(pubs_raw, (str, bytes)):
        raise OutcomesLayoutError("longitudinal_provenance publications/linked_publications must be a list")

    date_deltas_raw = raw.get("date_deltas")
    if date_deltas_raw is None:
        date_deltas_raw = {}
        if "primary_completion_to_results_delta" in raw:
            date_deltas_raw["primary_completion_to_results"] = raw["primary_completion_to_results_delta"]
        if "days_from_primary_completion_to_results" in raw:
            date_deltas_raw["days_from_primary_completion_to_results"] = raw[
                "days_from_primary_completion_to_results"
            ]
    elif not isinstance(date_deltas_raw, Mapping):
        raise OutcomesLayoutError("longitudinal_provenance.date_deltas must be an object")

    out: dict[str, Any] = {
        "registry_milestones": dict(registry_milestones),
        "posted_documents": list(posted_docs_raw),
        "publications": list(pubs_raw),
        "date_deltas": dict(date_deltas_raw),
    }
    for meta_key in ("provenance_schema_version", "provenance_rules_version", "excluded_standalone_icf_count"):
        if meta_key in raw:
            out[meta_key] = raw[meta_key]

    return canonicalize_nested(out)


# ── badges and trial bundle assembly ──────────────────────────────────────────

def build_document_badge(
    *,
    in_audit: bool,
    document_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build ``index_row.document_badge`` with strict holdout masking."""
    if in_audit:
        return {
            "readability_status": "in_audit_holdout",
            "redaction_flags": None,
            "doc_count": None,
            "total_physical_pages": None,
        }
    if document_profile is None:
        return {
            "readability_status": "not_indexed",
            "redaction_flags": None,
            "doc_count": None,
            "total_physical_pages": None,
        }
    docs = document_profile.get("documents", [])
    page_counts = [
        d["physical_pages"]
        for d in docs
        if isinstance(d, Mapping) and type(d.get("physical_pages")) is int
    ]
    total_pages: int | None = sum(page_counts) if page_counts else (0 if len(docs) == 0 else None)
    return {
        "readability_status": str(document_profile["readability_summary"]),
        "redaction_flags": list(document_profile.get("redaction_flags", [])),
        "doc_count": len(docs),
        "total_physical_pages": total_pages,
    }


def build_census_badge(
    *,
    in_audit: bool,
    decision_rule_locations: Mapping[str, Any] | None,
    has_documents: bool,
) -> dict[str, Any]:
    """Build ``index_row.census_badge`` with strict holdout masking."""
    if in_audit:
        return {
            "census_status": "in_audit_holdout",
            "status_counts": None,
            "has_second_pass": False,
        }
    if decision_rule_locations is None:
        return {
            "census_status": "not_started" if has_documents else "no_document",
            "status_counts": None,
            "has_second_pass": False,
        }
    effective_rows = decision_rule_locations.get("effective_pass")
    if (
        not isinstance(effective_rows, Sequence)
        or isinstance(effective_rows, (str, bytes))
        or not effective_rows
    ):
        effective_rows = decision_rule_locations.get("primary_pass", [])
    counts: dict[str, int] = {}
    for status in LOCATION_STATUSES:
        n = sum(1 for r in effective_rows if isinstance(r, Mapping) and r.get("status") == status)
        if n > 0:
            counts[status] = n
    census_status = str(decision_rule_locations["census_status"])
    return {
        "census_status": census_status,
        "status_counts": counts if census_status != "not_started" else None,
        "has_second_pass": bool(decision_rule_locations.get("second_pass")),
    }


def trial_nct_id(trial: Mapping[str, Any]) -> str:
    """The trial's ID from a flat row (``nct_id``) or a CT.gov study
    (``protocolSection.identificationModule.nctId``), strictly validated.

    When a record carries both, they must agree: a flat ``nct_id`` stamped
    onto another trial's CT.gov record is refused, not resolved either way.
    """
    proto = trial.get("protocolSection") if isinstance(trial.get("protocolSection"), Mapping) else {}
    ident = proto.get("identificationModule") if isinstance(proto.get("identificationModule"), Mapping) else {}
    flat = trial.get("nct_id")
    nested = ident.get("nctId")
    if flat is None and nested is None:
        raise OutcomesLayoutError("record carries no nct_id (neither nct_id nor identificationModule.nctId)")
    flat_checked = strict_nct_id(flat, "nct_id") if flat is not None else None
    nested_checked = (
        strict_nct_id(nested, "protocolSection.identificationModule.nctId") if nested is not None else None
    )
    if flat_checked is not None and nested_checked is not None and flat_checked != nested_checked:
        raise OutcomesLayoutError(
            "record's nct_id and protocolSection.identificationModule.nctId name different trials"
        )
    return flat_checked if flat_checked is not None else str(nested_checked)


def _study_scalar_fields(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Extract lightweight trial-index metadata from either a flat manifest row
    or a full CT.gov API v2 study object. Missing values stay ``None``.
    """
    proto = trial.get("protocolSection") if isinstance(trial.get("protocolSection"), Mapping) else {}
    ident = proto.get("identificationModule") if isinstance(proto.get("identificationModule"), Mapping) else {}
    design = proto.get("designModule") if isinstance(proto.get("designModule"), Mapping) else {}
    status_mod = proto.get("statusModule") if isinstance(proto.get("statusModule"), Mapping) else {}
    sponsor_mod = proto.get("sponsorCollaboratorsModule") if isinstance(proto.get("sponsorCollaboratorsModule"), Mapping) else {}
    lead_sponsor = sponsor_mod.get("leadSponsor") if isinstance(sponsor_mod.get("leadSponsor"), Mapping) else {}
    cond_mod = proto.get("conditionsModule") if isinstance(proto.get("conditionsModule"), Mapping) else {}
    enroll_info = design.get("enrollmentInfo") if isinstance(design.get("enrollmentInfo"), Mapping) else {}
    doc_sec = trial.get("documentSection") if isinstance(trial.get("documentSection"), Mapping) else {}
    large_doc_mod = doc_sec.get("largeDocumentModule") if isinstance(doc_sec.get("largeDocumentModule"), Mapping) else {}
    large_docs = large_doc_mod.get("largeDocs") if isinstance(large_doc_mod.get("largeDocs"), Sequence) else []

    nct_id = trial_nct_id(trial)
    brief_title = trial.get("brief_title")
    if brief_title is None:
        brief_title = ident.get("briefTitle")
    if brief_title is None:
        brief_title = trial.get("title") or ""

    study_type = trial.get("study_type") if "study_type" in trial else design.get("studyType")

    phase = trial.get("phase")
    if phase is None and "phases" in design:
        phases = design.get("phases")
        if isinstance(phases, Sequence) and not isinstance(phases, (str, bytes)) and phases:
            phase = "/".join(str(p) for p in phases)

    raw_enrollment = trial.get("enrollment") if "enrollment" in trial else enroll_info.get("count")
    enrollment = _strict_int_or_none(raw_enrollment, f"{nct_id}.enrollment")
    enrollment_type = (
        trial.get("enrollment_type")
        if "enrollment_type" in trial
        else enroll_info.get("type")
    )

    def _date_struct(key_flat: str, key_struct: str) -> str | None:
        if key_flat in trial:
            v = trial[key_flat]
            return str(v) if isinstance(v, str) and v.strip() else None
        struct = status_mod.get(key_struct)
        if isinstance(struct, Mapping):
            v = struct.get("date")
            return str(v) if isinstance(v, str) and v.strip() else None
        return None

    primary_completion_date = _date_struct("primary_completion_date", "primaryCompletionDateStruct")
    results_first_post_date = _date_struct("results_first_post_date", "resultsFirstPostDateStruct")

    if "lead_sponsor_name" in trial:
        lead_sponsor_name = trial.get("lead_sponsor_name")
    elif lead_sponsor.get("name") is not None:
        lead_sponsor_name = lead_sponsor.get("name")
    else:
        lead_sponsor_name = trial.get("sponsor")
    sponsor_class = (
        trial.get("sponsor_class")
        if "sponsor_class" in trial
        else lead_sponsor.get("class")
    )
    ecosystem_category = trial.get("ecosystem_category")

    primary_condition = trial.get("primary_condition")
    if primary_condition is None:
        conds = cond_mod.get("conditions")
        if isinstance(conds, Sequence) and not isinstance(conds, (str, bytes)) and conds:
            primary_condition = str(conds[0])

    raw_poc = trial.get("primary_outcomes_count")
    if raw_poc is None and "n_primary_outcomes" in trial:
        raw_poc = trial.get("n_primary_outcomes")
    primary_outcomes_count = _strict_int_or_none(raw_poc, f"{nct_id}.primary_outcomes_count")

    large_has_protocol = any(
        isinstance(d, Mapping) and bool(d.get("hasProtocol"))
        for d in large_docs
    )
    large_has_sap = any(
        isinstance(d, Mapping) and bool(d.get("hasSap"))
        for d in large_docs
    )
    if "protocol" in trial:
        protocol_flag = _require_strict_bool(trial["protocol"], f"{nct_id}.protocol") or large_has_protocol
    else:
        protocol_flag = large_has_protocol
    if "sap" in trial:
        sap_flag = _require_strict_bool(trial["sap"], f"{nct_id}.sap") or large_has_sap
    else:
        sap_flag = large_has_sap

    return {
        "nct_id": nct_id,
        "brief_title": str(brief_title),
        "study_type": str(study_type) if study_type is not None else None,
        "phase": str(phase) if phase is not None else None,
        "enrollment": enrollment,
        "enrollment_type": str(enrollment_type) if enrollment_type is not None else None,
        "primary_completion_date": primary_completion_date,
        "results_first_post_date": results_first_post_date,
        "lead_sponsor_name": str(lead_sponsor_name) if lead_sponsor_name is not None else None,
        "sponsor_class": str(sponsor_class) if sponsor_class is not None else None,
        "ecosystem_category": str(ecosystem_category) if ecosystem_category is not None else None,
        "primary_condition": str(primary_condition) if primary_condition is not None else None,
        "primary_outcomes_count": primary_outcomes_count,
        "protocol": protocol_flag,
        "sap": sap_flag,
    }


def build_trial_bundle(
    trial: Mapping[str, Any],
    *,
    shards: int = DEFAULT_OUTCOMES_SHARDS,
    in_audit: Any = None,
    explicit_cohorts: Sequence[str] | None = None,
    document_profile: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    decision_rule_locations: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    longitudinal_provenance: Mapping[str, Any] | None = None,
    population_parser: Callable[[Mapping[str, Any]], Any] | None = None,
    parsed_populations: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct and audit-check ``(index_row, shard_record)`` for one trial.

    ``in_audit`` (when given) and the trial's own ``in_audit`` must agree. A
    holdout trial (``in_audit`` true) is built as a stub: its index row holds
    the fields that come 1:1 from the trial/manifest row, masked badges and
    ``has_reported_results`` ``None``; its shard record holds ``nct_id``,
    ``in_audit``, ``cohorts`` and ``shard`` with every section ``None``. Its
    results are not extracted, its population parser is not called, and any
    side input for it is refused (``AuditFirewallError``), never dropped.
    For any other trial, every side input must name this trial wherever it
    names one, and any holdout flag it carries must be false
    (``check_side_input_identity``).
    """
    meta = _study_scalar_fields(trial)
    nct_id = meta["nct_id"]
    shard_id = outcomes_shard_of(nct_id, shards)

    if in_audit is not None and "in_audit" in trial and trial["in_audit"] is not in_audit:
        raise AuditFirewallError(
            f"{nct_id}: in_audit argument ({in_audit!r}) disagrees with the trial's own "
            f"in_audit ({trial['in_audit']!r})"
        )
    raw_in_audit = in_audit if in_audit is not None else trial.get("in_audit")
    # Run initial firewall check on raw inputs (Vectors 1, 2, 3, 4).
    assert_no_audit_leakage(
        nct_id=nct_id,
        in_audit=raw_in_audit,
        document_profile=document_profile,
        decision_rule_locations=decision_rule_locations,
        longitudinal_provenance=longitudinal_provenance,
        raw_payload=trial if raw_in_audit is True else None,
    )
    is_holdout = raw_in_audit is True
    if is_holdout and parsed_populations is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (stub) — parsed populations supplied for a holdout trial"
        )

    side_inputs: tuple[tuple[str, Any], ...] = (
        ("document_profile", document_profile),
        ("decision_rule_locations", decision_rule_locations),
        ("longitudinal_provenance", longitudinal_provenance),
        ("population_parsed", parsed_populations),
    )
    for side_label, side_value in side_inputs:
        if side_value is not None:
            check_side_input_identity(
                side_value,
                expected_nct_id=nct_id,
                expected_in_audit=is_holdout,
                label=side_label,
            )

    cohorts = derive_cohorts(
        explicit_cohorts if explicit_cohorts is not None else trial.get("cohorts"),
        ecosystem_category=meta["ecosystem_category"],
        sponsor_class=meta["sponsor_class"],
        phase=meta["phase"],
        protocol=meta["protocol"],
        sap=meta["sap"],
    )

    if is_holdout:
        # Stub: nothing beyond the 1:1 index fields. primary_outcomes_count is
        # the row's own count when it states one, never one counted from results.
        reported_results: dict[str, Any] | None = None
        primary_outcomes_count: int | None = meta["primary_outcomes_count"]
        norm_profile: dict[str, Any] | None = None
        norm_prov: dict[str, Any] | None = None
        norm_locations: dict[str, Any] | None = None
        has_docs = bool(meta["protocol"] or meta["sap"])
        has_reported_results: bool | None = None
    else:
        reported_results = extract_reported_results(
            trial,
            population_parser=population_parser,
            parsed_populations=parsed_populations,
        )
        if meta["primary_outcomes_count"] is not None:
            primary_outcomes_count = meta["primary_outcomes_count"]
        else:
            reg_count = len(reported_results["registered_primary_outcomes"])
            meas_count = len(reported_results["primary_outcome_measures"])
            primary_outcomes_count = reg_count if reg_count > 0 else (meas_count if meas_count > 0 else None)

        norm_profile = normalize_document_profile(document_profile, expected_nct_id=nct_id)
        norm_prov = normalize_longitudinal_provenance(longitudinal_provenance, in_audit=False)
        has_docs = (
            meta["protocol"]
            or meta["sap"]
            or bool(norm_profile and norm_profile["documents"])
            or bool(norm_prov and norm_prov["posted_documents"])
        )
        norm_locations = normalize_decision_rule_locations(
            decision_rule_locations,
            document_profile=norm_profile,
            primary_outcomes_count=primary_outcomes_count,
            has_posted_documents=bool(has_docs),
            expected_nct_id=nct_id,
        )
        has_reported_results = reported_results["results_posted"]

    doc_badge = build_document_badge(in_audit=is_holdout, document_profile=norm_profile)
    census_badge = build_census_badge(
        in_audit=is_holdout,
        decision_rule_locations=norm_locations,
        has_documents=bool(has_docs),
    )

    index_row: dict[str, Any] = {
        "nct_id": nct_id,
        "brief_title": meta["brief_title"],
        "study_type": meta["study_type"],
        "phase": meta["phase"],
        "enrollment": meta["enrollment"],
        "enrollment_type": meta["enrollment_type"],
        "primary_completion_date": meta["primary_completion_date"],
        "results_first_post_date": meta["results_first_post_date"],
        "lead_sponsor_name": meta["lead_sponsor_name"],
        "sponsor_class": meta["sponsor_class"],
        "ecosystem_category": meta["ecosystem_category"],
        "primary_condition": meta["primary_condition"],
        "cohorts": cohorts,
        "in_audit": is_holdout,
        "protocol": meta["protocol"],
        "sap": meta["sap"],
        "primary_outcomes_count": primary_outcomes_count,
        "has_reported_results": has_reported_results,
        "document_badge": canonicalize_nested(doc_badge),
        "census_badge": canonicalize_nested(census_badge),
    }

    shard_record: dict[str, Any] = {
        "nct_id": nct_id,
        "in_audit": is_holdout,
        "cohorts": cohorts,
        "shard": shard_id,
        "reported_results": reported_results,
        "document_profile": norm_profile,
        "decision_rule_locations": norm_locations,
        "longitudinal_provenance": norm_prov,
    }

    # Final firewall verification on assembled row + shard record (all 5 in-record vectors).
    assert_no_audit_leakage(
        nct_id=nct_id,
        in_audit=is_holdout,
        shard_record=shard_record,
        index_row=index_row,
    )
    return index_row, shard_record


def build_outcomes_documents(
    bundles: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    extracted_at: str,
    source_pipeline_commit: str | None,
    shards: int = DEFAULT_OUTCOMES_SHARDS,
    component_versions: Mapping[str, str | None] | None = None,
    allow_null_commit: bool = False,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """Assemble deterministic ``index.json`` and ``0..shards-1`` shard documents.

    Output is invariant to input ordering: ``index.json`` rows and every
    shard's ``data`` dict are sorted strictly ascending by ``nct_id``.
    Top-level header keys keep ``"data"`` last.
    """
    n_shards = _validate_shards(shards)
    ts = validate_iso_timestamp(extracted_at, "extracted_at")
    commit = validate_commit_stamp(source_pipeline_commit, allow_null=allow_null_commit)

    by_nct: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for index_row_raw, shard_rec_raw in bundles:
        index_row = dict(index_row_raw)
        shard_rec = dict(shard_rec_raw)
        nct_id = index_row.get("nct_id")
        expected_shard = outcomes_shard_of(nct_id, n_shards)
        if shard_rec.get("nct_id") != nct_id:
            raise OutcomesLayoutError(
                f"mismatched nct_id between index_row ({nct_id!r}) and shard_record ({shard_rec.get('nct_id')!r})"
            )
        if shard_rec.get("shard") != expected_shard:
            raise OutcomesLayoutError(
                f"{nct_id}: shard_record.shard is {shard_rec.get('shard')!r}, expected {expected_shard}"
            )
        if nct_id in by_nct:
            raise OutcomesLayoutError(f"duplicate nct_id in outcomes build: {nct_id!r}")
        assert_no_audit_leakage(
            nct_id=str(nct_id),
            in_audit=index_row.get("in_audit"),
            shard_record=shard_rec,
            index_row=index_row,
        )
        by_nct[str(nct_id)] = (index_row, shard_rec)

    sorted_ncts = sorted(by_nct)
    index_rows: list[dict[str, Any]] = []
    cohort_counts: dict[str, int] = {}
    audit_holdout_trials = 0
    shard_buckets: dict[int, dict[str, dict[str, Any]]] = {n: {} for n in range(n_shards)}

    for nct_id in sorted_ncts:
        index_row, shard_rec = by_nct[nct_id]
        # Canonicalize key order inside rows while preserving exact schema.
        ordered_row = {k: canonicalize_nested(index_row[k]) for k in INDEX_ROW_KEYS}
        ordered_rec = {k: canonicalize_nested(shard_rec[k]) for k in SHARD_RECORD_KEYS}
        index_rows.append(ordered_row)
        if ordered_row["in_audit"]:
            audit_holdout_trials += 1
        for slug in ordered_row["cohorts"]:
            cohort_counts[slug] = cohort_counts.get(slug, 0) + 1
        shard_buckets[ordered_rec["shard"]][nct_id] = ordered_rec

    norm_versions: dict[str, str | None] = (
        {str(k): (str(v) if v is not None else None) for k, v in sorted(component_versions.items())}
        if component_versions is not None
        else {}
    )

    index_doc: dict[str, Any] = {
        "extracted_at": ts,
        "source_pipeline_commit": commit,
        "layout_version": LAYOUT_VERSION,
        "layout": layout_header(n_shards),
        "cohort_counts": {k: cohort_counts[k] for k in sorted(cohort_counts)},
        "total_trials": len(index_rows),
        "audit_holdout_trials": audit_holdout_trials,
        "component_versions": norm_versions,
        "data": index_rows,
    }

    shard_docs: dict[int, dict[str, Any]] = {}
    for n in range(n_shards):
        shard_docs[n] = {
            "extracted_at": ts,
            "source_pipeline_commit": commit,
            "layout_version": LAYOUT_VERSION,
            "class": SHARD_CLASS,
            "shard": n,
            "shards": n_shards,
            "key": SHARD_KEY,
            "data": shard_buckets[n],
        }
    return index_doc, shard_docs


# ── validation telemetry and on-disk verifier ─────────────────────────────────

@dataclass(frozen=True)
class BudgetTelemetry:
    """Size telemetry for an outcomes tree (advisory budget reporting only)."""
    index_bytes: int
    total_shard_bytes: int
    total_bytes: int
    max_file_bytes: int
    max_file_path: str
    min_shard_bytes: int
    median_shard_bytes: int
    max_shard_bytes: int
    non_empty_shards: int
    empty_shards: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_bytes": self.index_bytes,
            "total_shard_bytes": self.total_shard_bytes,
            "total_bytes": self.total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_file_path": self.max_file_path,
            "min_shard_bytes": self.min_shard_bytes,
            "median_shard_bytes": self.median_shard_bytes,
            "max_shard_bytes": self.max_shard_bytes,
            "non_empty_shards": self.non_empty_shards,
            "empty_shards": self.empty_shards,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Result of ``validate_outcomes_tree``."""
    ok: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    total_trials: int
    audit_holdout_trials: int
    shards: int
    audit_leakage_detected: bool
    budget: BudgetTelemetry | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "total_trials": self.total_trials,
            "audit_holdout_trials": self.audit_holdout_trials,
            "shards": self.shards,
            "audit_leakage_detected": self.audit_leakage_detected,
            "budget": self.budget.to_dict() if self.budget is not None else None,
        }


def _shard_has_posted_documents(
    idx_row: Mapping[str, Any] | None,
    document_profile: Any = None,
    longitudinal_provenance: Any = None,
) -> bool:
    """Return True if any on-disk source proves posted protocol/SAP document(s)."""
    has_profile_docs = (
        isinstance(document_profile, Mapping)
        and isinstance(document_profile.get("documents"), list)
        and len(document_profile["documents"]) > 0
    )
    has_manifest_docs = (
        isinstance(idx_row, Mapping)
        and (idx_row.get("protocol") is True or idx_row.get("sap") is True)
    )
    has_prov_docs = (
        isinstance(longitudinal_provenance, Mapping)
        and isinstance(longitudinal_provenance.get("posted_documents"), list)
        and len(longitudinal_provenance["posted_documents"]) > 0
    )
    return has_profile_docs or has_manifest_docs or has_prov_docs


def _validate_shard_decision_rule_locations(
    rel: str,
    nct_id: str,
    locs: Any,
    idx_row: Mapping[str, Any] | None,
    document_profile: Any = None,
    longitudinal_provenance: Any = None,
) -> list[str]:
    """Validate a non-audit shard record's ``decision_rule_locations`` block.

    Applies on disk the rules normalization applies on input, from the same
    functions: Module 4's ``validate_citation_shape`` on every citation (so
    ``[None]``, ``['x']`` and ``[{}]`` fail here too) plus this module's
    document cross-check (``citation_problems``), Module 4's
    ``validate_status_citations`` on every row of every pass and of
    ``effective_pass``, the census status recomputed from ``effective_pass``
    (``census_status_from_effective_pass``) rather than read, ``no_document``
    with any citation, and the block's exact key set.
    """
    errs: list[str] = []
    prefix = f"{rel}.data[{nct_id}].decision_rule_locations"
    if not isinstance(locs, Mapping):
        return [f"{prefix} must be an object or None"]

    block_keys = set(locs.keys())
    if block_keys != LOCATIONS_BLOCK_KEYS:
        errs.append(
            f"{prefix} key mismatch (missing={sorted(LOCATIONS_BLOCK_KEYS - block_keys)}, "
            f"extra={sorted(str(k) for k in block_keys - LOCATIONS_BLOCK_KEYS)})"
        )
    if locs.get("elements_expected_per_outcome") != list(ELEMENTS):
        errs.append(f"{prefix}.elements_expected_per_outcome is not {list(ELEMENTS)}")

    has_posted_docs = _shard_has_posted_documents(
        idx_row,
        document_profile=document_profile,
        longitudinal_provenance=longitudinal_provenance,
    )
    docs_by_filename: dict[str, Mapping[str, Any]] | None = None
    if isinstance(document_profile, Mapping) and isinstance(document_profile.get("documents"), list):
        docs_by_filename = {
            str(d["filename"]): d
            for d in document_profile["documents"]
            if isinstance(d, Mapping) and isinstance(d.get("filename"), str)
        }

    census_status = locs.get("census_status")
    if census_status not in CENSUS_STATUSES:
        errs.append(f"{prefix}.census_status is invalid: {census_status!r}")
    elif census_status == "no_document" and has_posted_docs:
        errs.append(
            f"{prefix}.census_status 'no_document' is forbidden when posted document(s) are present"
        )

    primary_pass = locs.get("primary_pass")
    if not isinstance(primary_pass, list):
        errs.append(f"{prefix}.primary_pass must be a list")
        primary_pass = None

    optional_passes: dict[str, list[Any] | None] = {}
    for pass_key in ("second_pass", "ocr_recovery_pass", "adjudication_pass"):
        val = locs.get(pass_key)
        if val is not None and not isinstance(val, list):
            errs.append(f"{prefix}.{pass_key} must be a list or None")
            optional_passes[pass_key] = None
        else:
            optional_passes[pass_key] = val

    def _row_problems(where: str, row: Mapping[str, Any]) -> tuple[list[str], bool]:
        """Problems with one stored row, and whether its structure is sound."""
        row_errs: list[str] = []
        sound = True
        if type(row.get("primary_outcome_index")) is not int or row["primary_outcome_index"] < 0:
            row_errs.append(f"{where}.primary_outcome_index is invalid")
            sound = False
        if row.get("element") not in _ELEMENT_ORDER:
            row_errs.append(f"{where}.element is invalid: {row.get('element')!r}")
            sound = False
        status = row.get("status")
        status_ok = status in LOCATION_STATUSES
        if not status_ok:
            row_errs.append(f"{where}.status is invalid: {status!r}")
            sound = False
        cits = row.get("citations")
        if not isinstance(cits, list):
            row_errs.append(f"{where}.citations must be a list")
            sound = False
        else:
            for c_idx, cit in enumerate(cits):
                for problem in citation_problems(
                    cit,
                    docs_by_filename=docs_by_filename,
                    pages_not_searched=row.get("pages_not_searched"),
                ):
                    row_errs.append(f"{where}.citations[{c_idx}]: {problem}")
            if status_ok:
                for problem in validate_status_citations(
                    str(status), len(cits), has_posted_documents=has_posted_docs
                ):
                    row_errs.append(f"{where} forbidden by the status-to-citation rule: {problem}")
        if "text_source" in row:
            ts_val = row["text_source"]
            if not isinstance(ts_val, str) or not ts_val.strip():
                row_errs.append(f"{where}.text_source must be a non-empty string")
                sound = False
        return row_errs, sound

    all_pass_rows_valid = True
    for pass_key, pass_rows in (
        ("primary_pass", primary_pass),
        ("second_pass", optional_passes["second_pass"]),
        ("ocr_recovery_pass", optional_passes["ocr_recovery_pass"]),
        ("adjudication_pass", optional_passes["adjudication_pass"]),
    ):
        if pass_rows is None:
            continue
        for idx, row in enumerate(pass_rows):
            if not isinstance(row, Mapping):
                errs.append(f"{prefix}.{pass_key}[{idx}] must be an object")
                all_pass_rows_valid = False
                continue
            row_errs, sound = _row_problems(f"{prefix}.{pass_key}[{idx}]", row)
            errs.extend(row_errs)
            all_pass_rows_valid = all_pass_rows_valid and sound

    effective_pass = locs.get("effective_pass")
    if not isinstance(effective_pass, list):
        errs.append(f"{prefix}.effective_pass must be a list")
    else:
        effective_sound = True
        for idx, row in enumerate(effective_pass):
            if not isinstance(row, Mapping):
                errs.append(f"{prefix}.effective_pass[{idx}] must be an object")
                effective_sound = False
                continue
            sp = row.get("source_pass")
            if sp not in EFFECTIVE_SOURCE_PASSES:
                errs.append(f"{prefix}.effective_pass[{idx}].source_pass is invalid: {sp!r}")
            row_errs, sound = _row_problems(f"{prefix}.effective_pass[{idx}]", row)
            errs.extend(row_errs)
            effective_sound = effective_sound and sound
        if primary_pass is not None and all_pass_rows_valid:
            expected_effective = _resolve_effective_pass(
                primary_pass,
                optional_passes["ocr_recovery_pass"],
                optional_passes["adjudication_pass"],
            )
            if effective_pass != expected_effective:
                errs.append(
                    f"{prefix}.effective_pass does not match primary -> ocr_recovery -> adjudication resolution"
                )
        if effective_sound and census_status in CENSUS_STATUSES:
            count = idx_row.get("primary_outcomes_count") if isinstance(idx_row, Mapping) else None
            recomputed = census_status_from_effective_pass(
                effective_pass, count if type(count) is int else None
            )
            if recomputed != census_status:
                errs.append(
                    f"{prefix}.census_status is {census_status!r}, but its effective_pass gives "
                    f"{recomputed!r}; a stored census_status is recomputed, never trusted"
                )
    if census_status == "no_document":
        cited_rows = _rows_with_citations({
            "primary_pass": primary_pass,
            **optional_passes,
            "effective_pass": effective_pass,
        })
        if cited_rows:
            errs.append(
                f"{prefix}.census_status 'no_document' is forbidden with citations "
                f"({cited_rows} row(s) cite a document)"
            )

    if idx_row is not None and not errs:
        expected_badge = canonicalize_nested(
            build_census_badge(
                in_audit=False,
                decision_rule_locations=locs,
                has_documents=has_posted_docs,
            )
        )
        if idx_row.get("census_badge") != expected_badge:
            errs.append(
                f"{nct_id}: index census_badge ({idx_row.get('census_badge')!r}) "
                f"does not match shard decision_rule_locations ({expected_badge!r})"
            )

    return errs


def validate_outcomes_tree(
    out_dir: str | os.PathLike[str],
    *,
    allow_null_commit: bool = False,
    raise_on_audit_leakage: bool = False,
    max_file_bytes: int = PART_HARD_LIMIT_BYTES,
    max_tree_bytes: int = TREE_HARD_LIMIT_BYTES,
    warn_tree_bytes: int = TREE_WARN_BYTES,
) -> ValidationResult:
    """Validate an on-disk outcomes tree (``index.json`` + ``shards/0..N-1.json.gz``).

    Verifies:
    - Exact header key lists with ``"data"`` last in both ``index.json`` and shards.
    - Non-empty ISO-8601 ``extracted_at`` and ``source_pipeline_commit`` stamps
      (strict by default; pass ``allow_null_commit=True`` only for explicit offline runs).
    - Exact shard inventory ``0..N-1.json.gz`` with zero missing or stray files.
    - Gzip header determinism: ``mtime == 0`` and OS byte ``0xff`` at index 9.
    - Sorted ascending ``nct_id`` order in ``index.json`` and every shard's ``data``.
    - Exact shard assignment (``outcomes_shard_of(nct_id, N) == n``) and
      1:1 bijection between ``index.json`` and all shard entries.
    - The bundle marker (``outcomes_bundle.json``) names this layout and version.
    - Re-runs the 6-vector ``assert_no_audit_leakage`` firewall on every trial
      (holdout stubs included); messages about holdout trials withhold IDs.
    - The decision-rule rules of ``_validate_shard_decision_rule_locations``,
      which are the same functions normalization uses.
    - Computes budget telemetry and flags hard GitHub/Pages size limits without
      touching ``tests/data_budget.json``.
    """
    root = Path(out_dir)
    found_problems: list[str] = []
    warnings: list[str] = []
    audit_leakage = False

    if not root.is_dir():
        return ValidationResult(
            ok=False,
            problems=(f"{root} is not a directory",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    marker_problem = _bundle_marker_problem(root)
    if marker_problem is not None:
        found_problems.append(marker_problem)

    index_path = root / INDEX_FILE
    if not index_path.is_file():
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"missing {INDEX_FILE} in {root}",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    index_bytes = index_path.stat().st_size
    try:
        index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"{INDEX_FILE} is not valid UTF-8 JSON ({exc})",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    if not isinstance(index_doc, dict):
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"{INDEX_FILE} root must be a JSON object, got {type(index_doc).__name__}",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    if tuple(index_doc.keys()) != INDEX_HEADERS:
        found_problems.append(
            f"{INDEX_FILE} top-level keys are {list(index_doc.keys())}, expected {list(INDEX_HEADERS)} with 'data' last"
        )

    extracted_at = index_doc.get("extracted_at")
    try:
        validate_iso_timestamp(extracted_at, f"{INDEX_FILE}.extracted_at")
    except OutcomesLayoutError as exc:
        found_problems.append(str(exc))

    commit = index_doc.get("source_pipeline_commit")
    try:
        validate_commit_stamp(commit, allow_null=allow_null_commit, label=f"{INDEX_FILE}.source_pipeline_commit")
    except OutcomesLayoutError as exc:
        found_problems.append(str(exc))

    if type(index_doc.get("layout_version")) is not int or index_doc.get("layout_version") != LAYOUT_VERSION:
        found_problems.append(
            f"{INDEX_FILE}.layout_version is {index_doc.get('layout_version')!r}, expected {LAYOUT_VERSION}"
        )

    layout = index_doc.get("layout")
    n_shards = 0
    if not isinstance(layout, dict):
        found_problems.append(f"{INDEX_FILE}.layout must be an object")
    else:
        raw_shards = layout.get("shards")
        try:
            n_shards = _validate_shards(raw_shards)
            if layout != layout_header(n_shards):
                found_problems.append(
                    f"{INDEX_FILE}.layout is {layout!r}, expected {layout_header(n_shards)!r}"
                )
        except OutcomesLayoutError as exc:
            found_problems.append(f"{INDEX_FILE}.layout: {exc}")

    # Validate index rows.
    rows = index_doc.get("data")
    index_by_nct: dict[str, dict[str, Any]] = {}
    recomputed_cohorts: dict[str, int] = {}
    recomputed_holdouts = 0

    if not isinstance(rows, list):
        found_problems.append(f"{INDEX_FILE}.data must be a list")
    else:
        prev_nct: str | None = None
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                found_problems.append(f"{INDEX_FILE}.data[{idx}] is not an object")
                continue
            nct_id = row.get("nct_id")
            try:
                strict_nct_id(nct_id, f"{INDEX_FILE}.data[{idx}].nct_id")
            except OutcomesLayoutError as exc:
                found_problems.append(str(exc))
                continue
            if prev_nct is not None and nct_id <= prev_nct:
                found_problems.append(
                    f"{INDEX_FILE}.data is not strictly sorted ascending by nct_id ({prev_nct} before {nct_id})"
                )
            prev_nct = nct_id
            if nct_id in index_by_nct:
                found_problems.append(f"{INDEX_FILE}.data lists duplicate nct_id {nct_id}")
                continue
            if set(row.keys()) != set(INDEX_ROW_KEYS):
                missing = sorted(set(INDEX_ROW_KEYS) - set(row.keys()))
                extra = sorted(set(row.keys()) - set(INDEX_ROW_KEYS))
                found_problems.append(
                    f"{INDEX_FILE}.data[{nct_id}] key mismatch (missing={missing}, extra={extra})"
                )
            try:
                assert_no_audit_leakage(
                    nct_id=nct_id,
                    in_audit=row.get("in_audit"),
                    index_row=row,
                )
            except AuditFirewallError as exc:
                audit_leakage = True
                if raise_on_audit_leakage:
                    raise
                found_problems.append(withhold_nct_ids(str(exc)))
            except OutcomesLayoutError as exc:
                found_problems.append(str(exc))

            if row.get("in_audit") is True:
                recomputed_holdouts += 1
            cohorts = row.get("cohorts")
            if (
                not isinstance(cohorts, list)
                or not cohorts
                or cohorts != sorted(set(cohorts))
                or not all(isinstance(c, str) and COHORT_RE.fullmatch(c) for c in cohorts)
            ):
                found_problems.append(f"{INDEX_FILE}.data[{nct_id}].cohorts is invalid: {cohorts!r}")
            else:
                for c in cohorts:
                    recomputed_cohorts[c] = recomputed_cohorts.get(c, 0) + 1

            doc_badge = row.get("document_badge")
            if not isinstance(doc_badge, dict) or doc_badge.get("readability_status") not in READABILITY_BADGE_STATUSES:
                found_problems.append(f"{INDEX_FILE}.data[{nct_id}].document_badge is invalid")
            census_badge = row.get("census_badge")
            if not isinstance(census_badge, dict) or census_badge.get("census_status") not in CENSUS_BADGE_STATUSES:
                found_problems.append(f"{INDEX_FILE}.data[{nct_id}].census_badge is invalid")

            index_by_nct[nct_id] = row

        if index_doc.get("total_trials") != len(rows):
            found_problems.append(
                f"{INDEX_FILE}.total_trials is {index_doc.get('total_trials')!r}, but data has {len(rows)} rows"
            )
        if index_doc.get("audit_holdout_trials") != recomputed_holdouts:
            found_problems.append(
                f"{INDEX_FILE}.audit_holdout_trials is {index_doc.get('audit_holdout_trials')!r}, "
                f"expected {recomputed_holdouts}"
            )
        expected_cohort_counts = {k: recomputed_cohorts[k] for k in sorted(recomputed_cohorts)}
        if index_doc.get("cohort_counts") != expected_cohort_counts:
            found_problems.append(
                f"{INDEX_FILE}.cohort_counts is {index_doc.get('cohort_counts')!r}, "
                f"expected {expected_cohort_counts!r}"
            )

    # Validate shards directory and every shard file.
    shards_dir = root / SHARDS_DIR
    shard_sizes: list[int] = []
    max_file_size = index_bytes
    max_file_rel = INDEX_FILE
    non_empty_shards = 0
    empty_shards = 0
    seen_in_shards: dict[str, int] = {}

    if n_shards == 0:
        found_problems.append("cannot verify shards because layout.shards is invalid")
    elif not shards_dir.is_dir():
        found_problems.append(f"missing {SHARDS_DIR}/ directory in {root}")
    else:
        present_entries = sorted(os.listdir(shards_dir))
        expected_names = [f"{n}.json.gz" for n in range(n_shards)]
        expected_set = set(expected_names)
        present_set = set(present_entries)
        missing_shards = [name for name in expected_names if name not in present_set]
        stray_files = [name for name in present_entries if name not in expected_set]
        if missing_shards:
            found_problems.append(
                f"{SHARDS_DIR}/ lacks {len(missing_shards)} shard file(s) (e.g. {', '.join(missing_shards[:5])})"
            )
        if stray_files:
            found_problems.append(
                f"{SHARDS_DIR}/ contains unexpected file(s): {', '.join(stray_files[:5])}"
            )

        for n in range(n_shards):
            rel = shard_relpath(n)
            spath = root / rel
            if not spath.is_file():
                continue
            raw_bytes = spath.read_bytes()
            sz = len(raw_bytes)
            shard_sizes.append(sz)
            if sz > max_file_size:
                max_file_size = sz
                max_file_rel = rel
            if sz > max_file_bytes:
                found_problems.append(
                    f"{rel} is {sz:,} bytes, exceeding hard per-file limit {max_file_bytes:,}"
                )

            try:
                mtime, os_byte = inspect_gzip_header(raw_bytes)
                if mtime != 0:
                    found_problems.append(f"{rel} gzip header mtime is {mtime}, expected 0")
                if os_byte != 0xFF:
                    found_problems.append(f"{rel} gzip header OS byte is 0x{os_byte:02x}, expected 0xff")
                decompressed = gzip.decompress(raw_bytes).decode("utf-8")
                sdoc = json.loads(decompressed)
            except (OutcomesLayoutError, OSError, EOFError, UnicodeDecodeError, ValueError) as exc:
                found_problems.append(f"{rel} is not readable gzipped UTF-8 JSON ({exc})")
                continue

            if not isinstance(sdoc, dict):
                found_problems.append(f"{rel} root must be a JSON object")
                continue
            if tuple(sdoc.keys()) != SHARD_HEADERS:
                found_problems.append(
                    f"{rel} top-level keys are {list(sdoc.keys())}, expected {list(SHARD_HEADERS)} with 'data' last"
                )
            if sdoc.get("extracted_at") != extracted_at:
                found_problems.append(
                    f"{rel}.extracted_at ({sdoc.get('extracted_at')!r}) differs from {INDEX_FILE} ({extracted_at!r})"
                )
            if sdoc.get("source_pipeline_commit") != commit:
                found_problems.append(
                    f"{rel}.source_pipeline_commit ({sdoc.get('source_pipeline_commit')!r}) "
                    f"differs from {INDEX_FILE} ({commit!r})"
                )
            if sdoc.get("layout_version") != LAYOUT_VERSION:
                found_problems.append(f"{rel}.layout_version is {sdoc.get('layout_version')!r}, expected {LAYOUT_VERSION}")
            if sdoc.get("class") != SHARD_CLASS:
                found_problems.append(f"{rel}.class is {sdoc.get('class')!r}, expected {SHARD_CLASS!r}")
            if type(sdoc.get("shard")) is not int or sdoc.get("shard") != n:
                found_problems.append(f"{rel}.shard is {sdoc.get('shard')!r}, expected {n}")
            if type(sdoc.get("shards")) is not int or sdoc.get("shards") != n_shards:
                found_problems.append(f"{rel}.shards is {sdoc.get('shards')!r}, expected {n_shards}")
            if sdoc.get("key") != SHARD_KEY:
                found_problems.append(f"{rel}.key is {sdoc.get('key')!r}, expected {SHARD_KEY!r}")

            sdata = sdoc.get("data")
            if not isinstance(sdata, dict):
                found_problems.append(f"{rel}.data must be a JSON object keyed by nct_id")
                continue

            if len(sdata) == 0:
                empty_shards += 1
            else:
                non_empty_shards += 1

            keys = list(sdata.keys())
            if keys != sorted(keys):
                found_problems.append(f"{rel}.data keys are not sorted ascending by nct_id")

            for nct_id, entry in sdata.items():
                try:
                    actual_shard = outcomes_shard_of(nct_id, n_shards)
                except OutcomesLayoutError as exc:
                    found_problems.append(f"{rel}: {exc}")
                    continue
                if actual_shard != n:
                    found_problems.append(
                        f"{rel} holds {nct_id}, which belongs in shard {actual_shard}"
                    )
                if nct_id in seen_in_shards:
                    found_problems.append(
                        f"{nct_id} appears in both shard {seen_in_shards[nct_id]} and shard {n}"
                    )
                else:
                    seen_in_shards[nct_id] = n

                if not isinstance(entry, dict):
                    found_problems.append(f"{rel}.data[{nct_id}] is not an object")
                    continue
                if entry.get("nct_id") != nct_id:
                    found_problems.append(f"{rel}.data[{nct_id}].nct_id is {entry.get('nct_id')!r}")
                if entry.get("shard") != n:
                    found_problems.append(f"{rel}.data[{nct_id}].shard is {entry.get('shard')!r}, expected {n}")
                if set(entry.keys()) != set(SHARD_RECORD_KEYS):
                    missing = sorted(set(SHARD_RECORD_KEYS) - set(entry.keys()))
                    extra = sorted(set(entry.keys()) - set(SHARD_RECORD_KEYS))
                    found_problems.append(
                        f"{rel}.data[{nct_id}] key mismatch (missing={missing}, extra={extra})"
                    )

                idx_row = index_by_nct.get(nct_id)
                try:
                    assert_no_audit_leakage(
                        nct_id=nct_id,
                        in_audit=entry.get("in_audit"),
                        shard_record=entry,
                        index_row=idx_row,
                    )
                except AuditFirewallError as exc:
                    audit_leakage = True
                    if raise_on_audit_leakage:
                        raise
                    found_problems.append(withhold_nct_ids(str(exc)))
                except OutcomesLayoutError as exc:
                    found_problems.append(str(exc))

                if idx_row is not None:
                    if entry.get("in_audit") != idx_row.get("in_audit"):
                        found_problems.append(
                            f"{nct_id}: shard in_audit ({entry.get('in_audit')!r}) != index ({idx_row.get('in_audit')!r})"
                        )
                    if entry.get("cohorts") != idx_row.get("cohorts"):
                        found_problems.append(
                            f"{nct_id}: shard cohorts ({entry.get('cohorts')!r}) != index ({idx_row.get('cohorts')!r})"
                        )

                if entry.get("in_audit") is False and entry.get("decision_rule_locations") is not None:
                    found_problems.extend(
                        _validate_shard_decision_rule_locations(
                            rel,
                            nct_id,
                            entry["decision_rule_locations"],
                            idx_row,
                            document_profile=entry.get("document_profile"),
                            longitudinal_provenance=entry.get("longitudinal_provenance"),
                        )
                    )
                elif entry.get("in_audit") is False and idx_row is not None:
                    has_posted_docs = _shard_has_posted_documents(
                        idx_row,
                        document_profile=entry.get("document_profile"),
                        longitudinal_provenance=entry.get("longitudinal_provenance"),
                    )
                    expected_badge = canonicalize_nested(
                        build_census_badge(
                            in_audit=False,
                            decision_rule_locations=None,
                            has_documents=has_posted_docs,
                        )
                    )
                    if idx_row.get("census_badge") != expected_badge:
                        found_problems.append(
                            f"{nct_id}: index census_badge ({idx_row.get('census_badge')!r}) "
                            f"does not match shard document presence ({expected_badge!r})"
                        )

        # Index-to-shard bijection check.
        index_nct_set = set(index_by_nct)
        shard_nct_set = set(seen_in_shards)
        missing_from_shards = sorted(index_nct_set - shard_nct_set)
        orphan_in_shards = sorted(shard_nct_set - index_nct_set)
        if missing_from_shards:
            found_problems.append(
                f"{len(missing_from_shards)} index trial(s) missing from shards "
                f"(e.g. {', '.join(missing_from_shards[:5])})"
            )
        if orphan_in_shards:
            found_problems.append(
                f"{len(orphan_in_shards)} shard trial(s) missing from {INDEX_FILE} "
                f"(e.g. {', '.join(orphan_in_shards[:5])})"
            )

    if index_bytes > max_file_bytes:
        found_problems.append(
            f"{INDEX_FILE} is {index_bytes:,} bytes, exceeding hard per-file limit {max_file_bytes:,}"
        )

    total_shard_bytes = sum(shard_sizes)
    total_bytes = index_bytes + total_shard_bytes
    if total_bytes > max_tree_bytes:
        found_problems.append(
            f"outcomes tree is {total_bytes:,} bytes, exceeding hard tree limit {max_tree_bytes:,}"
        )
    elif total_bytes > warn_tree_bytes:
        warnings.append(
            f"outcomes tree is {total_bytes:,} bytes, exceeding warning threshold {warn_tree_bytes:,}"
        )

    if shard_sizes:
        ordered_sizes = sorted(shard_sizes)
        budget: BudgetTelemetry | None = BudgetTelemetry(
            index_bytes=index_bytes,
            total_shard_bytes=total_shard_bytes,
            total_bytes=total_bytes,
            max_file_bytes=max_file_size,
            max_file_path=max_file_rel,
            min_shard_bytes=ordered_sizes[0],
            median_shard_bytes=ordered_sizes[len(ordered_sizes) // 2],
            max_shard_bytes=ordered_sizes[-1],
            non_empty_shards=non_empty_shards,
            empty_shards=empty_shards,
        )
    else:
        budget = None

    return ValidationResult(
        ok=len(found_problems) == 0,
        problems=tuple(found_problems),
        warnings=tuple(warnings),
        total_trials=len(index_by_nct),
        audit_holdout_trials=recomputed_holdouts,
        shards=n_shards,
        audit_leakage_detected=audit_leakage,
        budget=budget,
    )


def problems(out_dir: str | os.PathLike[str], *, allow_null_commit: bool = False) -> list[str]:
    """Return every problem found in an outcomes directory ([] when valid)."""
    return list(validate_outcomes_tree(out_dir, allow_null_commit=allow_null_commit).problems)


# ── output allowlist (where a bundle, its staging dir or a report may go) ─────

def _marker_text(marker: Mapping[str, Any]) -> str:
    """The exact bytes (as text) a marker file holds: compact JSON plus newline."""
    return encode_json_text(dict(marker), trailing_newline=True)


def _holds_marker(directory: Path, name: str, marker: Mapping[str, Any]) -> bool:
    """True when ``directory/name`` is a regular file (not a symlink) holding
    exactly ``_marker_text(marker)``."""
    path = directory / name
    if os.path.islink(path) or not path.is_file():
        return False
    try:
        return path.read_text(encoding="utf-8") == _marker_text(marker)
    except (OSError, UnicodeDecodeError):
        return False


def is_bundle_dir(directory: Path) -> bool:
    """True when ``directory`` holds this layout's bundle marker."""
    return _holds_marker(directory, BUNDLE_MARKER_FILE, BUNDLE_MARKER)


def _bundle_marker_problem(root: Path) -> str | None:
    """A problem string when ``root`` lacks this layout's bundle marker, else None."""
    if is_bundle_dir(root):
        return None
    return (
        f"{root} lacks the bundle marker {BUNDLE_MARKER_FILE} holding {BUNDLE_MARKER!r} "
        f"(as a regular file); only a tree written by write_outcomes_tree is an outcomes bundle"
    )


def absolute_output_path(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` made absolute against the working directory, refusing ``..``.

    The path is deliberately not resolved: resolving would hide the symlinks
    the guards below refuse. A ``..`` component is refused because collapsing
    it lexically gives the wrong directory when the component before it is a
    symlink, and keeping it would leave the ancestor checks reading the wrong
    chain.
    """
    raw = Path(os.fspath(path))
    if any(part == ".." for part in raw.parts):
        raise OutcomesLayoutError(f"refusing output path {raw}: it contains '..'")
    return raw if raw.is_absolute() else Path.cwd() / raw


def _foreign_entries(directory: Path) -> list[str]:
    """Names in ``directory`` that mark another data family's directory.

    ``run.json``, ``summary.json``, ``records`` and ``records.json`` mark the
    site's dataset folder, its outcomes and results-structure families; an
    ``index.json`` in a directory without this layout's bundle marker is
    someone else's index. Entries are tested with ``lexists`` so a dangling
    symlink counts too.
    """
    found = [name for name in FOREIGN_DIRECTORY_MARKERS if os.path.lexists(directory / name)]
    if os.path.lexists(directory / INDEX_FILE) and not is_bundle_dir(directory):
        found.append(f"{INDEX_FILE} (not an outcomes bundle index)")
    return found


def _check_ancestors(path: Path, *, what: str) -> None:
    """Refuse when any ancestor of ``path`` is a symlink, marks another data
    family, or is itself an outcomes bundle."""
    for ancestor in path.parents:
        if os.path.islink(ancestor):
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is a symlink "
                f"(pass a path without symlinked components)"
            )
        if not ancestor.is_dir():
            continue
        foreign = _foreign_entries(ancestor)
        if foreign:
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is a foreign data "
                f"directory (found {', '.join(foreign)}); point it at a dedicated directory outside "
                f"any dataset folder"
            )
        if is_bundle_dir(ancestor):
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is an outcomes bundle"
            )


def _staging_dir_for(target: Path) -> Path:
    """``<target>.partial``, the staging directory beside the target."""
    return target.with_name(target.name + ".partial")


def check_output_target(out_dir: str | os.PathLike[str]) -> Path:
    """Refuse every output location this module may not write; return the
    absolute target when it may.

    Allowed: a target that is absent, an empty directory, or a bundle this
    module wrote (``is_bundle_dir``). Refused, before anything is created or
    deleted:
    - a target that is a symlink, a non-directory, a directory holding
      another family's markers (``_foreign_entries``), or a non-empty
      directory without the bundle marker; a bundle whose ``index.json``,
      ``shards`` or marker is a symlink;
    - an ancestor that is a symlink, marks another family, or is a bundle;
    - a ``<target>.partial`` that is a symlink or not a directory, that holds
      a retained ``_previous_shards`` backup (named in the refusal; L3), or
      that is non-empty without this module's staging marker. Only an empty
      ``.partial`` or one holding the staging marker is this module's to remove.
    """
    target = absolute_output_path(out_dir)
    _check_ancestors(target, what="outcomes bundle")

    if os.path.lexists(target):
        if os.path.islink(target):
            raise OutcomesLayoutError(f"refusing to write through symlinked output path {target}")
        if not target.is_dir():
            raise OutcomesLayoutError(f"refusing to overwrite non-directory output path {target}")
        foreign = _foreign_entries(target)
        if any(not f.startswith(INDEX_FILE) for f in foreign):
            raise OutcomesLayoutError(
                f"refusing to overwrite foreign data directory {target}: found marker(s) "
                f"{', '.join(foreign)} (point --out-dir at a dedicated {DEFAULT_OUT_DIR_NAME!r} directory)"
            )
        if foreign:
            raise OutcomesLayoutError(
                f"refusing to overwrite foreign directory {target}: existing {INDEX_FILE} is not an "
                f"{DEFAULT_OUT_DIR_NAME} index (no {BUNDLE_MARKER_FILE} marker)"
            )
        if any(target.iterdir()) and not is_bundle_dir(target):
            raise OutcomesLayoutError(
                f"refusing to write into non-empty directory {target}: it holds no "
                f"{BUNDLE_MARKER_FILE} marker, so this module did not write it"
            )
        for managed in (INDEX_FILE, SHARDS_DIR, BUNDLE_MARKER_FILE):
            if os.path.islink(target / managed):
                raise OutcomesLayoutError(f"refusing to replace symlinked bundle entry {target / managed}")

    work = _staging_dir_for(target)
    if os.path.lexists(work):
        if os.path.islink(work):
            raise OutcomesLayoutError(f"refusing to use symlinked staging directory {work}")
        if not work.is_dir():
            raise OutcomesLayoutError(f"refusing to replace non-directory staging path {work}")
        backup = work / PREVIOUS_SHARDS_DIR
        if os.path.lexists(backup):
            raise RetainedBackupError(
                f"refusing to run: {backup} holds previous shards retained after an earlier failed "
                f"restore. Move them back to {target / SHARDS_DIR} (or set them aside) and delete {work} "
                f"by hand; nothing was deleted"
            )
        if any(work.iterdir()) and not _holds_marker(work, STAGING_MARKER_FILE, STAGING_MARKER):
            raise OutcomesLayoutError(
                f"refusing to delete {work}: it is not empty and holds no {STAGING_MARKER_FILE} "
                f"marker, so this module did not create it"
            )
    return target


def check_report_path(
    report: str | os.PathLike[str],
    *,
    out_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Refuse a ``--report`` location this module may not write; return it absolute.

    The same allowlist as the bundle: no symlinked report path or ancestor, no
    ancestor that marks another data family or is a bundle; the report may not
    sit inside the bundle or its staging directory (it would be swapped away
    or flagged as a stray), may not be named like a data family's file
    (``run.json``, ``summary.json``, ``records.json``, ``index.json``), and may
    replace an existing file only when that file is a report this module
    wrote (``report_of`` names it).
    """
    path = absolute_output_path(report)
    if path.name in (*FOREIGN_DIRECTORY_MARKERS, INDEX_FILE, BUNDLE_MARKER_FILE, STAGING_MARKER_FILE):
        raise OutcomesLayoutError(f"refusing report path {path}: its name is a data family's file name")
    if out_dir is not None:
        target = absolute_output_path(out_dir)
        for inside in (target, _staging_dir_for(target)):
            if inside == path or inside in path.parents:
                raise OutcomesLayoutError(f"refusing report path {path}: it is inside {inside}")
    _check_ancestors(path, what="report")
    if os.path.lexists(path):
        if os.path.islink(path) or not path.is_file():
            raise OutcomesLayoutError(f"refusing report path {path}: it exists and is not a regular file")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            existing = None
        if not isinstance(existing, dict) or existing.get("report_of") != REPORT_OF:
            raise OutcomesLayoutError(
                f"refusing to overwrite {path}: it is not a report this builder wrote "
                f"(no report_of={REPORT_OF!r})"
            )
    return path


# Stamped into every report so an existing report can be told from any other file.
REPORT_OF = "scripts/build_outcomes_shards.py"


def write_report(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    out_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Write a JSON report after ``check_report_path`` allows it."""
    report_path = check_report_path(path, out_dir=out_dir)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    body = {"report_of": REPORT_OF, **dict(payload)}
    report_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return report_path


# ── atomic staging + swap writer ──────────────────────────────────────────────

def _remove_own_staging_dir(work: Path) -> None:
    """Delete a staging directory this module created, marker last.

    Called only on a directory ``check_output_target`` accepted (empty, or
    holding the staging marker) or one this call created. Everything but the
    marker goes first, so if removal stops partway the marker is still there
    and the next run still recognises the directory as this module's.
    """
    if not os.path.lexists(work):
        return
    marker = work / STAGING_MARKER_FILE
    for entry in list(work.iterdir()):
        if entry == marker:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    if os.path.lexists(marker):
        marker.unlink()
    work.rmdir()


def write_outcomes_tree(
    out_dir: str | os.PathLike[str],
    index_doc: Mapping[str, Any],
    shard_docs: Mapping[int, Mapping[str, Any]],
    *,
    allow_null_commit: bool = False,
) -> ValidationResult:
    """Atomically write and verify an outcomes tree via ``<out_dir>.partial``.

    Method:
    1. ``check_output_target`` refuses every location this module may not
       write (L1) and a retained backup from an earlier failed restore (L3),
       before anything is created or deleted. The audit firewall runs over
       every row and shard next, still before touching the filesystem.
    2. The tree, its bundle marker and a staging marker are written into a
       freshly created ``<out_dir>.partial`` and checked with
       ``validate_outcomes_tree``.
    3. The swap: the bundle marker (when the target has none), then
       ``shards/`` (the old one moved to ``.partial/_previous_shards``), then
       ``index.json``. Any exception in this window, ``KeyboardInterrupt`` and
       ``SystemExit`` included, rolls back before it propagates: the new
       shards move out, the old ones move back, a marker this call installed
       is removed, and a target this call created is removed if empty. If the
       old shards cannot be moved back they stay in ``_previous_shards``, the
       staging directory is kept, and ``RetainedBackupError`` names the path
       (every later call refuses until a person has dealt with it).
    4. On the way out the staging directory is removed, marker last
       (``_remove_own_staging_dir``); a failure to remove it is returned as a
       warning, not hidden. Sibling files in the target are never touched.
    """
    target = check_output_target(out_dir)

    layout = index_doc.get("layout")
    if not isinstance(layout, Mapping):
        raise OutcomesLayoutError("index_doc.layout must be an object")
    n_shards = _validate_shards(layout.get("shards"))

    # Pre-flight holdout firewall check BEFORE touching filesystem.
    rows = index_doc.get("data")
    if not isinstance(rows, list):
        raise OutcomesLayoutError("index_doc.data must be a list")
    rows_by_nct: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise OutcomesLayoutError("every index_doc.data item must be an object")
        nct_id = str(row.get("nct_id") or "")
        assert_no_audit_leakage(nct_id=nct_id, in_audit=row.get("in_audit"), index_row=row)
        rows_by_nct[nct_id] = row

    if set(shard_docs.keys()) != set(range(n_shards)):
        raise OutcomesLayoutError(
            f"shard_docs must cover exact shard indices 0..{n_shards - 1}"
        )
    for n in range(n_shards):
        sdoc = shard_docs[n]
        sdata = sdoc.get("data")
        if not isinstance(sdata, Mapping):
            raise OutcomesLayoutError(f"shard {n} data must be an object")
        for nct_id, entry in sdata.items():
            if not isinstance(entry, Mapping):
                raise OutcomesLayoutError(f"shard {n} entry {nct_id!r} must be an object")
            assert_no_audit_leakage(
                nct_id=str(nct_id),
                in_audit=entry.get("in_audit"),
                shard_record=entry,
                index_row=rows_by_nct.get(str(nct_id)),
            )

    work = _staging_dir_for(target)
    # check_output_target accepted any existing .partial as this module's own
    # (empty, or holding the staging marker, and no retained backup).
    _remove_own_staging_dir(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir()
    created_work = True
    (work / STAGING_MARKER_FILE).write_text(_marker_text(STAGING_MARKER), encoding="utf-8")
    work_shards = work / SHARDS_DIR
    work_shards.mkdir()

    target_existed_before = os.path.lexists(target)
    target_had_marker = target_existed_before and is_bundle_dir(target)
    backup_shards = work / PREVIOUS_SHARDS_DIR
    preserve_work = False

    try:
        (work / BUNDLE_MARKER_FILE).write_text(_marker_text(BUNDLE_MARKER), encoding="utf-8")
        (work / INDEX_FILE).write_text(
            encode_json_text(index_doc, trailing_newline=True),
            encoding="utf-8",
        )
        for n in range(n_shards):
            gz_bytes = encode_gz_bytes(shard_docs[n])
            (work / shard_relpath(n)).write_bytes(gz_bytes)

        result = validate_outcomes_tree(
            work,
            allow_null_commit=allow_null_commit,
            raise_on_audit_leakage=True,
        )
        if not result.ok:
            raise OutcomesLayoutError(
                f"staged outcomes tree failed validation; {target} untouched: {'; '.join(result.problems)}"
            )

        dst_shards = target / SHARDS_DIR
        dst_index = target / INDEX_FILE
        dst_marker = target / BUNDLE_MARKER_FILE
        installed_marker = False
        moved_old_shards = False
        installed_new_shards = False
        installed_new_index = False
        try:
            target.mkdir(exist_ok=True)
            if not target_had_marker:
                os.replace(work / BUNDLE_MARKER_FILE, dst_marker)
                installed_marker = True
            if os.path.lexists(dst_shards):
                os.replace(dst_shards, backup_shards)
                moved_old_shards = True
            os.replace(work_shards, dst_shards)
            installed_new_shards = True
            os.replace(work / INDEX_FILE, dst_index)
            installed_new_index = True
        except BaseException as swap_exc:
            if installed_new_index:
                # The swap completed before the interruption landed; the
                # target is the new, validated tree. Nothing to undo.
                raise
            undo_failures: list[str] = []
            if installed_new_shards and os.path.lexists(dst_shards):
                try:
                    os.replace(dst_shards, work / FAILED_NEW_SHARDS_DIR)
                except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                    undo_failures.append(f"moving the new shards out failed ({undo_exc})")
            if moved_old_shards and os.path.lexists(backup_shards) and not os.path.lexists(dst_shards):
                try:
                    os.replace(backup_shards, dst_shards)
                except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                    undo_failures.append(f"moving the previous shards back failed ({undo_exc})")
            if installed_marker and not moved_old_shards and not os.path.lexists(dst_index):
                try:
                    dst_marker.unlink()
                    if not target_existed_before and not any(target.iterdir()):
                        target.rmdir()
                except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                    undo_failures.append(f"removing the marker this call installed failed ({undo_exc})")
            elif not target_existed_before:
                try:
                    if os.path.lexists(target) and not any(target.iterdir()):
                        target.rmdir()
                except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                    undo_failures.append(f"removing the target this call created failed ({undo_exc})")
            if os.path.lexists(backup_shards):
                preserve_work = True
                raise RetainedBackupError(
                    f"writing {target} failed ({type(swap_exc).__name__}: {swap_exc}) and the previous "
                    f"shards could not be put back ({'; '.join(undo_failures) or 'restore did not run'}). "
                    f"They are retained at {backup_shards}: move them back to {dst_shards} by hand, then "
                    f"delete {work}; until then every write to {target} refuses"
                ) from swap_exc
            if undo_failures:
                swap_exc.add_note("rollback problems: " + "; ".join(undo_failures))
            raise
    finally:
        cleanup_warning: str | None = None
        if created_work and not preserve_work:
            try:
                _remove_own_staging_dir(work)
            except OSError as exc:
                cleanup_warning = (
                    f"staging directory {work} could not be removed ({exc}); it holds the staging "
                    f"marker, so the next run removes it"
                )

    if cleanup_warning is not None:
        result = dataclasses.replace(result, warnings=(*result.warnings, cleanup_warning))
    return result
