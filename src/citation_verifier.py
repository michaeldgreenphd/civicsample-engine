"""Mechanical single-page citation verifier and neutral-language guard.

Verifies document records, decision-rule location records, and verbatim
single-page PDF quotations for clinical trial rule censuses.

Runtime dependencies:
- Python 3.11+ standard library only (``dataclasses``, ``gzip``, ``hashlib``,
  ``json``, ``pathlib``, ``re``, ``typing``, ``unicodedata``).
- Performs zero network calls and invokes zero external PDF binaries; consumes
  pre-extracted JSON or JSONL single-page text maps via ``PageTextProvider``.

Key invariants:
- Strict ASCII NCT ID gate (``STRICT_NCT_ID_RE = ^NCT[0-9]{8}$``): every entry
  point validates NCT IDs with ``validate_strict_nct_id`` FIRST (before any
  audit-set or allowlist check) and raises ``InvalidNctIdError`` immediately on
  any non-matching value without case-folding or whitespace stripping.
- Hard ``in_audit`` holdout firewall: every entry point requires explicit
  ``bool`` ``in_audit`` (or verified ``in_scope_nct_ids`` + ``audit_nct_ids``)
  and non-empty ``audit_nct_ids`` by default, raising
  ``AuditHoldoutViolationError`` unconditionally before reading or inspecting
  document text whenever an audit or unlisted trial is referenced.
- Canonical status-to-citation and 6-key citation-shape rules
  (``CITATION_REQUIRED_STATUSES``, ``CITATION_FORBIDDEN_STATUSES``,
  ``validate_status_citations``, ``validate_citation_shape``) shared across
  verification and shard layout builders.
- Mandatory provenance stamps: document rows require non-empty ``model_id`` and
  ``profile_version``; location rows require non-empty ``model_id`` and
  ``prompt_version``.
- Strict single-page quotation verification: a quotation must match within the
  single declared ``physical_page`` text (via exact whitespace match or
  NFKC/typographic/line-break-hyphen normalization). Cross-page spans are
  never joined to pass verification and are flagged as
  ``cross_page_span_violation``.
- Exhaustive search discipline: ``NOT_FOUND`` is forbidden whenever
  ``pages_not_searched`` is non-empty in default publication mode;
  ``pages_searched == "all"`` contradicts any non-empty ``pages_not_searched``.
- Deterministic neutral-language guard: flags evaluative trial-outcome,
  p-value spin, and regulatory-compliance claims in worker/system commentary
  while exempting verbatim source text (``quotation``, ``statement_verbatim``).
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import (
    AbstractSet,
    Any,
    Final,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)
import unicodedata

__version__: Final[str] = "1.1.0"
VERIFIER_RULES_VERSION: Final[str] = "1.1.0"

STRICT_NCT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^NCT[0-9]{8}$", re.ASCII)

ELEMENTS: Final[tuple[str, ...]] = (
    "primary_endpoint_analysed",
    "analysis_population",
    "hypothesis_type",
    "alpha_and_sidedness",
    "margin",
    "interim_boundary",
    "multiplicity",
)
STATUSES: Final[tuple[str, ...]] = (
    "FOUND",
    "NOT_FOUND",
    "NOT_APPLICABLE",
    "AMBIGUOUS",
    "UNREADABLE",
    "NO_DOCUMENT",
)
CITATION_REQUIRED_STATUSES: Final[tuple[str, ...]] = (
    "FOUND",
    "AMBIGUOUS",
    "NOT_APPLICABLE",
)
CITATION_FORBIDDEN_STATUSES: Final[tuple[str, ...]] = (
    "NOT_FOUND",
    "NO_DOCUMENT",
)
DOC_TYPES: Final[tuple[str, ...]] = ("protocol", "sap", "combined")
ALLOWED_PASSES: Final[tuple[str, ...]] = (
    "primary",
    "second",
    "ocr_recovery",
    "adjudication",
)
SPARSE_PASSES: Final[tuple[str, ...]] = (
    "ocr_recovery",
    "adjudication",
)
UNREADABLE_SUBTYPES: Final[tuple[str, ...]] = (
    "image_only_scan",
    "redacted_cci_ppd",
    "garbled_cmap",
)

DOCUMENT_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "nct_id",
    "filename",
    "url",
    "http_status",
    "sha256",
    "bytes",
    "doc_type",
    "version_label",
    "version_date_as_printed",
    "physical_pages",
    "pages_without_text",
    "retrieved_at",
    "model_id",
    "profile_version",
)
DOCUMENT_OPTIONAL_KEYS: Final[tuple[str, ...]] = (
    "in_audit",
    "upload_date",
    "readability_status",
    "unreadable_subtype",
    "unreadable_recovery_bucket",
    "text_source",
    "indexer_rules_version",
    "redaction_markers",
    "pages_selected",
    "pages_deferred",
    "unreadable_statistical_pages",
    "api_doc_date",
    "upload_timing",
    "download_error",
    "cache_hit",
)
LOCATION_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "nct_id",
    "primary_outcome_index",
    "primary_outcome_title",
    "element",
    "status",
    "citations",
    "pages_searched",
    "pages_not_searched",
    "note",
    "worker",
    "pass",
    "model_id",
    "prompt_version",
)
CITATION_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "filename",
    "sha256",
    "physical_page",
    "printed_page",
    "section_heading",
    "quotation",
)

MAX_QUOTATION_CHARS: Final[int] = 300

MatchTier = Literal["exact_ws", "unicode_nfkc_hyphen", "mismatch"]
IssueSeverity = Literal["error", "warning"]
NeutralLanguageCategory = Literal[
    "trial_success_or_failure_claim",
    "statistical_significance_result_claim",
    "spin_phrase",
    "misconduct_or_compliance_inference",
]

_MISSING: Final[object] = object()
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_HEX2_DIR_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{2}$", re.ASCII)
_SAFE_PDF_BASENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9._-]+\.pdf$",
    re.IGNORECASE | re.ASCII,
)
_ELLIPSIS_RE: Final[re.Pattern[str]] = re.compile(
    r"\.{3,}|(?:\.\s+){2,}\.|[\u2026\u22ee\u22ef]"
)
_DASH_TRANS: Final[dict[int, str]] = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)
_INVISIBLE_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\u00ad\u200b\u200c\u200d\ufeff]")
_SOFT_HYPHEN_WRAP_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z])\u00ad\s*\r?\n\s*([a-z])")
_LINE_BREAK_HYPHEN_RE: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z])[\-\u2010\u2011]\s*\r?\n\s*([a-z])"
)
_NOTE_PAGE_COUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Za-z0-9._-]+\.pdf)\s*\(\s*(\d+)\s+pages?\s*\)",
    re.IGNORECASE,
)
_PAGE_RANGE_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\d+\s*[-–—]\s*\d+\s*$")
_UNREADABLE_NOTE_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:unreadable|scan(?:ned)?|image[- ]only|ocr|redact(?:ed|ion)?|cci|ppd|"
    r"white[- ]box|blank(?:ed)?|garbled|cmap|font\s+encoding|cipher(?:ed)?|"
    r"no\s+extractable\s+text|pages?\s+without\s+text|missing\s+text|corrupt)\b",
    re.IGNORECASE,
)


class CitationVerificationError(ValueError):
    """Base error for hard mechanical citation verification failures."""


class InvalidNctIdError(CitationVerificationError):
    """Raised immediately whenever an NCT ID fails strict ASCII ``^NCT[0-9]{8}$`` validation."""


class AuditHoldoutViolationError(CitationVerificationError):
    """Raised immediately whenever any record or batch violates the ``in_audit`` holdout firewall."""


@runtime_checkable
class PageTextProvider(Protocol):
    """Pluggable single-page text lookup (JSON/JSONL page maps, OCR page maps, or dict fixtures)."""

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        ...


@dataclass(frozen=True)
class NeutralLanguageRule:
    """Single deterministic regex rule for neutral-language screening."""

    rule_id: str
    category: NeutralLanguageCategory
    pattern: str
    rationale: str


@dataclass(frozen=True)
class NeutralLanguageFlag:
    """Detected evaluative, spin, or compliance-inference phrase."""

    rule_id: str
    category: NeutralLanguageCategory
    matched_text: str
    span: tuple[int, int]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize flag to a JSON-compatible dictionary."""
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "matched_text": self.matched_text,
            "span": [self.span[0], self.span[1]],
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class VerificationIssue:
    """Single verification error or advisory warning."""

    code: str
    severity: IssueSeverity
    nct_id: str
    location_key: tuple[int, str, str] | None
    filename: str | None
    physical_page: int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize issue to a JSON-compatible dictionary."""
        return {
            "code": self.code,
            "severity": self.severity,
            "nct_id": self.nct_id,
            "location_key": list(self.location_key) if self.location_key is not None else None,
            "filename": self.filename,
            "physical_page": self.physical_page,
            "message": self.message,
        }


@dataclass(frozen=True)
class CorpusVerificationReport:
    """Aggregate report returned by ``verify_census_corpus``."""

    ok: bool
    verifier_rules_version: str
    neutral_vocab_sha256: str
    documents_checked: int
    locations_checked: int
    citations_checked: int
    exact_ws_matches: int
    unicode_nfkc_hyphen_matches: int
    substring_mismatches: int
    ellipsis_flags: int
    neutral_language_flags: int
    issues: tuple[VerificationIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a JSON-compatible dictionary."""
        return {
            "ok": self.ok,
            "verifier_rules_version": self.verifier_rules_version,
            "neutral_vocab_sha256": self.neutral_vocab_sha256,
            "documents_checked": self.documents_checked,
            "locations_checked": self.locations_checked,
            "citations_checked": self.citations_checked,
            "exact_ws_matches": self.exact_ws_matches,
            "unicode_nfkc_hyphen_matches": self.unicode_nfkc_hyphen_matches,
            "substring_mismatches": self.substring_mismatches,
            "ellipsis_flags": self.ellipsis_flags,
            "neutral_language_flags": self.neutral_language_flags,
            "error_count": sum(1 for issue in self.issues if issue.severity == "error"),
            "warning_count": sum(1 for issue in self.issues if issue.severity == "warning"),
            "issues": [issue.to_dict() for issue in self.issues],
        }


# Calibrated against protocol decision-rule commentary so conditional statistical
# rules ("if Week 16 is statistically significant", "switching arm",
# "non-compliant investigational site", "sites failed CFDA inspection",
# "GCP violations") produce zero false positives.
NEUTRAL_LANGUAGE_RULES: Final[tuple[NeutralLanguageRule, ...]] = (
    NeutralLanguageRule(
        rule_id="NL01",
        category="trial_success_or_failure_claim",
        pattern=(
            r"\b(?:(?:the\s+)?(?:trial|study|treatment|intervention|drug|regimen)\s+"
            r"(?:successfully\s+)?(?:met|did\s+not\s+meet|failed\s+to\s+meet)\s+"
            r"(?:its|the)\s+(?:co-)?primary\s+(?:endpoint|outcome|objective)s?"
            r"|(?:met|failed\s+to\s+meet|did\s+not\s+meet)\s+its\s+(?:co-)?primary\s+"
            r"(?:endpoint|outcome|objective)s?"
            r"|(?:the\s+)?(?:trial|study)\s+(?:failed|succeeded|was\s+a\s+failure|"
            r"was\s+a\s+success|was\s+positive|was\s+negative)(?!\s+to\s+enroll))\b"
        ),
        rationale=(
            "Evaluative claim that a trial met/failed its primary objective or "
            "succeeded/failed; record factual protocol rules and posted values only."
        ),
    ),
    NeutralLanguageRule(
        rule_id="NL02",
        category="statistical_significance_result_claim",
        pattern=(
            r"\b(?:achieved\s+statistical\s+significance"
            r"|failed\s+to\s+(?:achieve|reach|demonstrate)\s+"
            r"(?:statistical\s+significance|superiority|non-inferiority|equivalence)"
            r"|(?:trial|study|treatment|drug|intervention|results?)\s+"
            r"(?:achieved|reached|demonstrated|showed)\s+"
            r"(?:statistical\s+significance|superiority|non-inferiority|equivalence|clinical\s+benefit)"
            r"|statistically\s+significant\s+(?:improvement|benefit|difference|reduction|effect)\s+"
            r"(?:was|were)\s+(?:observed|seen|found|achieved|demonstrated))\b"
        ),
        rationale=(
            "Evaluative statistical significance result claim; do not interpret "
            "p-values or claim statistical/clinical benefit in commentary."
        ),
    ),
    NeutralLanguageRule(
        rule_id="NL03",
        category="spin_phrase",
        pattern=(
            r"\b(?:trend(?:ed|ing)?\s+toward(?:s)?\s+(?:statistical\s+)?significance"
            r"|numerical(?:ly)?\s+favor(?:ed|able)\s+without\s+significance"
            r"|borderline\s+(?:statistical\s+)?significan(?:t|ce)"
            r"|clinically\s+meaningful\s+benefit\s+despite)\b"
        ),
        rationale=(
            "Spin phrase framing non-significant findings as near-significant or "
            "clinically meaningful."
        ),
    ),
    NeutralLanguageRule(
        rule_id="NL04",
        category="misconduct_or_compliance_inference",
        pattern=(
            r"\b(?:outcome[- ]switching|endpoint[- ]switching|p-hacking|data\s+dredging"
            r"|selective\s+reporting\s+bias"
            r"|(?:fdaaa|fda|ema|regulatory|statutory|42\s*cfr)\s+(?:non-?compliance|violation)"
            r"|violat(?:ed|es|ing)\s+(?:fdaaa|fda|42\s*cfr|federal\s+law|regulatory\s+requirements)"
            r"|overdue\s+under\s+fdaaa)\b"
        ),
        rationale=(
            "Misconduct or statutory/FDAAA compliance judgement; emit factual "
            "dated milestones and day intervals only."
        ),
    ),
)

_COMPILED_NEUTRAL_RULES: Final[tuple[tuple[NeutralLanguageRule, re.Pattern[str]], ...]] = tuple(
    (rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in NEUTRAL_LANGUAGE_RULES
)


def _compute_neutral_vocab_sha256(rules: Sequence[NeutralLanguageRule]) -> str:
    payload = [
        [i, r.rule_id, r.category, r.pattern]
        for i, r in enumerate(rules)
    ]
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


NEUTRAL_LANGUAGE_VOCAB_SHA256: Final[str] = _compute_neutral_vocab_sha256(NEUTRAL_LANGUAGE_RULES)


def _is_strict_int(value: Any) -> bool:
    """Return True only for genuine int instances (explicitly excluding bool)."""
    return type(value) is int


def _is_non_empty_str(value: Any) -> bool:
    """Return True only when ``value`` is an exact ``str`` with non-whitespace content."""
    return type(value) is str and len(value.strip()) > 0


def _is_safe_pdf_filename(value: Any) -> bool:
    """Return True only when ``value`` is a safe single-component ``.pdf`` basename."""
    return (
        type(value) is str
        and len(value) > 0
        and "/" not in value
        and "\\" not in value
        and ".." not in value
        and not value.startswith(".")
        and _SAFE_PDF_BASENAME_RE.fullmatch(value) is not None
    )


def validate_strict_nct_id(value: Any, *, context: str = "nct_id") -> str:
    """Validate that ``value`` matches strict ASCII ``^NCT[0-9]{8}$`` with no stripping or folding.

    Checks ``type(value) is str``, ``len(value) == 11``, and
    ``STRICT_NCT_ID_RE.fullmatch(value)`` so trailing newlines, leading/trailing
    spaces, lowercase prefixes, zero-width characters, and non-ASCII digits are
    unconditionally rejected with ``InvalidNctIdError``.
    """
    if type(value) is not str or len(value) != 11 or STRICT_NCT_ID_RE.fullmatch(value) is None:
        raise InvalidNctIdError(
            f"Invalid {context} {value!r}: expected strict 11-character ASCII ^NCT[0-9]{{8}}$."
        )
    return value


def _validate_nct_set(
    nct_set: AbstractSet[str],
    *,
    context: str,
) -> frozenset[str]:
    """Validate an NCT ID set (rejecting str/bytes/Mapping) and every member ID."""
    if isinstance(nct_set, (str, bytes, Mapping)) or not isinstance(nct_set, AbstractSet):
        raise AuditHoldoutViolationError(
            f"{context} must be an AbstractSet[str], got {type(nct_set).__name__}."
        )
    validated: set[str] = set()
    for item in nct_set:
        validated.add(validate_strict_nct_id(item, context=f"{context} entry"))
    return frozenset(validated)


def enforce_audit_holdout(
    nct_id: str,
    *,
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
) -> None:
    """Hard holdout firewall: validate strict NCT ID first, then enforce ``in_audit == False``.

    Order of checks:
    1. ``validate_strict_nct_id`` on ``nct_id`` and all entries in
       ``audit_nct_ids`` / ``in_scope_nct_ids`` (raises ``InvalidNctIdError``).
    2. Explicit ``in_audit`` check: must be ``bool`` when supplied (never
       defaulted to ``False``); if omitted, ``in_scope_nct_ids`` is required.
    3. Rejects ``in_audit is True``, ``nct_id in audit_nct_ids``, overlap
       between ``audit_nct_ids`` and ``in_scope_nct_ids``, unlisted trials
       (``nct_id not in in_scope_nct_ids``), and empty ``audit_nct_ids`` unless
       ``allow_empty_audit_set=True``.
    """
    checked_nct = validate_strict_nct_id(nct_id, context="nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )

    if type(allow_empty_audit_set) is not bool:
        raise AuditHoldoutViolationError(
            f"allow_empty_audit_set must be bool, got {type(allow_empty_audit_set).__name__}."
        )

    if checked_scope_ids is not None and (checked_audit_ids & checked_scope_ids):
        overlap = sorted(checked_audit_ids & checked_scope_ids)
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: in_scope_nct_ids overlaps audit_nct_ids: {overlap}."
        )

    if in_audit is not _MISSING:
        if type(in_audit) is not bool:
            raise AuditHoldoutViolationError(
                f"in_audit must be an explicit bool for {checked_nct!r}, got {in_audit!r} "
                f"({type(in_audit).__name__})."
            )
        if in_audit is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: trial {checked_nct!r} has in_audit=True and may not be "
                f"inspected, verified, or scheduled for census extraction."
            )
    elif checked_scope_ids is None:
        raise AuditHoldoutViolationError(
            f"Missing explicit bool in_audit (and no verified in_scope_nct_ids supplied) "
            f"for {checked_nct!r}."
        )

    if checked_nct in checked_audit_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: trial {checked_nct!r} is in audit_nct_ids and may not be "
            f"inspected, verified, or scheduled for census extraction."
        )

    if checked_scope_ids is not None and checked_nct not in checked_scope_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout firewall refusal: trial {checked_nct!r} is absent from verified "
            f"in-scope index rows."
        )

    if not allow_empty_audit_set and len(checked_audit_ids) == 0:
        raise AuditHoldoutViolationError(
            "audit_nct_ids may not be empty unless allow_empty_audit_set=True after verifying "
            "100% of index rows carry explicit in_audit=False."
        )


def _enforce_record_firewall(
    record: Mapping[str, Any],
    *,
    record_label: str,
    in_audit: Any,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str] | None,
    allow_empty_audit_set: bool,
) -> str:
    """Validate strict NCT ID and ``in_audit`` firewall on a single record mapping."""
    checked_nct = validate_strict_nct_id(record.get("nct_id"), context=f"{record_label}.nct_id")

    has_row_flag = "in_audit" in record
    if has_row_flag:
        row_flag = record["in_audit"]
        if type(row_flag) is not bool:
            raise AuditHoldoutViolationError(
                f"{record_label} for {checked_nct!r} carries non-bool in_audit={row_flag!r}."
            )
        if row_flag is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: {record_label} for {checked_nct!r} has in_audit=True."
            )

    if in_audit is not _MISSING:
        if type(in_audit) is not bool:
            raise AuditHoldoutViolationError(
                f"in_audit must be an explicit bool for {checked_nct!r}, got {in_audit!r}."
            )
        if in_audit is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: trial {checked_nct!r} has in_audit=True."
            )
        effective_in_audit: Any = False
    elif has_row_flag:
        effective_in_audit = False
    else:
        effective_in_audit = _MISSING

    enforce_audit_holdout(
        checked_nct,
        in_audit=effective_in_audit,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    return checked_nct


def validate_status_citations(
    status: str,
    citations_count: int,
    *,
    has_posted_documents: bool | None = None,
) -> list[str]:
    """Validate status-to-citation count and document-presence consistency.

    Enforces:
    - ``>= 1`` citation for ``CITATION_REQUIRED_STATUSES``
      (``("FOUND", "AMBIGUOUS", "NOT_APPLICABLE")``).
    - ``0`` citations for ``CITATION_FORBIDDEN_STATUSES``
      (``("NOT_FOUND", "NO_DOCUMENT")``).
    - Forbids ``status == "NO_DOCUMENT"`` when ``has_posted_documents is True``.
    - Forbids ``citations_count > 0`` when ``has_posted_documents is False``.
    """
    errors: list[str] = []
    status_valid = type(status) is str and status in STATUSES
    if not status_valid:
        errors.append(f"Invalid status {status!r}; expected one of {STATUSES}.")

    count_valid = _is_strict_int(citations_count) and citations_count >= 0
    if not count_valid:
        errors.append(
            f"citations_count must be a non-negative int (not bool), got {citations_count!r}."
        )

    if has_posted_documents is not None and type(has_posted_documents) is not bool:
        errors.append(
            f"has_posted_documents must be bool or None, got {has_posted_documents!r}."
        )

    if status_valid and count_valid:
        if status in CITATION_REQUIRED_STATUSES and citations_count < 1:
            errors.append(
                f"status={status!r} requires at least 1 citation, found {citations_count}."
            )
        elif status in CITATION_FORBIDDEN_STATUSES and citations_count != 0:
            errors.append(
                f"status={status!r} requires 0 citations, found {citations_count}."
            )

    if has_posted_documents is True and status == "NO_DOCUMENT":
        errors.append(
            "status='NO_DOCUMENT' is invalid when posted document(s) exist "
            "(has_posted_documents=True)."
        )
    if has_posted_documents is False and count_valid and citations_count > 0:
        errors.append(
            f"Citations are forbidden when no posted documents exist "
            f"(has_posted_documents=False, found {citations_count})."
        )

    return errors


def validate_citation_shape(
    cit: Any,
    *,
    allow_none_optional_strings: bool = False,
) -> list[str]:
    """Validate the canonical 6-key citation object shape and quotation hygiene.

    Checks:
    - ``isinstance(cit, Mapping)`` (rejecting ``None``, ``'x'``, ``123``, ``[]``).
    - Exact 6 keys ``CITATION_REQUIRED_KEYS`` (rejecting ``{}`` and extra keys).
    - ``filename`` is a safe non-empty ``.pdf`` basename without path traversal.
    - ``sha256`` matches ``^[0-9a-f]{64}$``.
    - ``physical_page`` is a strict ``int >= 1`` (not ``bool``, not a span).
    - ``printed_page`` and ``section_heading`` are ``str`` (or ``str | None``
      only when ``allow_none_optional_strings=True``).
    - ``quotation`` is a non-empty single-space-normalized ``str`` of length
      ``1..300`` without ellipsis markers (``has_ellipsis``).
    """
    if type(allow_none_optional_strings) is not bool:
        raise CitationVerificationError(
            f"allow_none_optional_strings must be bool, got {allow_none_optional_strings!r}."
        )
    if not isinstance(cit, Mapping):
        return [f"Citation must be a JSON object, got {type(cit).__name__}."]

    errors: list[str] = []
    if not all(type(k) is str for k in cit.keys()):
        errors.append("Citation keys must all be strings.")

    keys = {k for k in cit.keys() if type(k) is str}
    required = set(CITATION_REQUIRED_KEYS)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        errors.append(f"Citation missing required keys: {missing}")
    if extra:
        errors.append(f"Citation has unexpected keys: {extra}")

    if "filename" in cit:
        filename_raw = cit["filename"]
        if not _is_safe_pdf_filename(filename_raw):
            errors.append(
                f"Citation filename must be a safe non-empty .pdf basename, got {filename_raw!r}."
            )

    if "sha256" in cit:
        sha_raw = cit["sha256"]
        if (
            type(sha_raw) is not str
            or len(sha_raw) != 64
            or _SHA256_RE.fullmatch(sha_raw) is None
        ):
            errors.append(
                f"Citation sha256 must be 64 lowercase hex characters, got {sha_raw!r}."
            )

    if "physical_page" in cit:
        phys_raw = cit["physical_page"]
        if isinstance(phys_raw, (list, tuple)) or (
            isinstance(phys_raw, str) and _PAGE_RANGE_TEXT_RE.match(phys_raw)
        ):
            errors.append(
                f"Citation physical_page must be a single int, not a span {phys_raw!r}."
            )
        elif not _is_strict_int(phys_raw) or phys_raw < 1:
            errors.append(
                f"Citation physical_page must be a positive int, got {phys_raw!r}."
            )

    if "printed_page" in cit:
        printed_raw = cit["printed_page"]
        if allow_none_optional_strings:
            if printed_raw is not None and type(printed_raw) is not str:
                errors.append(
                    f"Citation printed_page must be str or None, got {printed_raw!r}."
                )
        elif type(printed_raw) is not str:
            errors.append(f"Citation printed_page must be str, got {printed_raw!r}.")

    if "section_heading" in cit:
        heading_raw = cit["section_heading"]
        if allow_none_optional_strings:
            if heading_raw is not None and type(heading_raw) is not str:
                errors.append(
                    f"Citation section_heading must be str or None, got {heading_raw!r}."
                )
        elif type(heading_raw) is not str:
            errors.append(f"Citation section_heading must be str, got {heading_raw!r}.")

    if "quotation" in cit:
        quotation = cit["quotation"]
        if type(quotation) is not str or len(quotation) == 0:
            errors.append("Citation quotation must be a non-empty string.")
        else:
            if len(quotation) > MAX_QUOTATION_CHARS:
                errors.append(
                    f"Citation quotation length {len(quotation)} exceeds "
                    f"maximum {MAX_QUOTATION_CHARS} characters."
                )
            if quotation != " ".join(quotation.split()):
                errors.append(
                    "Citation quotation must use canonical single-space whitespace."
                )
            if has_ellipsis(quotation):
                errors.append(
                    "Citation quotation contains forbidden ellipsis omission marker."
                )

    return errors


def normalize_for_page_match(
    text: str,
    *,
    repair_line_break_hyphens: bool = False,
) -> str:
    """Normalize PDF page text or quotation text for Tier-2 single-page matching.

    Applies:
    1. Optional soft-hyphen and hyphenated line-wrap joining on raw multi-line
       PDF text before whitespace collapse (e.g., ``random-\\nization`` ->
       ``randomization``).
    2. Unicode NFKC normalization (expands ligatures like ``ﬁ`` -> ``fi``,
       ``ﬂ`` -> ``fl``, ``ﬃ`` -> ``ffi``, and normalizes NBSP).
    3. Removal of soft hyphens (``U+00AD``) and zero-width characters.
    4. Folding of Unicode dashes/minus to ASCII ``-`` and curly quotes to
       straight ASCII quotes.
    5. Canonical single-space whitespace collapse.
    """
    normalized = text
    if repair_line_break_hyphens:
        normalized = _SOFT_HYPHEN_WRAP_RE.sub(r"\1\2", normalized)
        normalized = _LINE_BREAK_HYPHEN_RE.sub(r"\1\2", normalized)
    normalized = unicodedata.normalize("NFKC", normalized)
    normalized = _INVISIBLE_CHARS_RE.sub("", normalized)
    normalized = normalized.translate(_DASH_TRANS)
    return " ".join(normalized.split())


def has_ellipsis(text: str) -> bool:
    """Return True if text contains literal, spaced, or Unicode ellipsis markers."""
    return bool(_ELLIPSIS_RE.search(text))


def match_quotation_on_page(quotation: str, page_text: str) -> MatchTier:
    """Check whether ``quotation`` occurs verbatim on a single ``page_text``.

    Never joins text across adjacent pages. Returns:
    - ``"exact_ws"``: exact substring after collapsing ASCII/Unicode whitespace.
    - ``"unicode_nfkc_hyphen"``: substring after NFKC ligature folding,
      typographic dash/quote folding, invisible-char removal, and optional
      line-break hyphen repair.
    - ``"mismatch"``: absent from this single page under both tiers.
    """
    q_ws = " ".join(quotation.split())
    if not q_ws:
        return "mismatch"
    p_ws = " ".join(page_text.split())
    if q_ws in p_ws:
        return "exact_ws"

    q_norm = normalize_for_page_match(quotation, repair_line_break_hyphens=False)
    if not q_norm:
        return "mismatch"
    # Check both with line-break hyphen repair (for wrapped words like
    # "random-\nization") and without (for genuine hyphenated terms like
    # "intent-\nto-treat" wrapped across lines).
    p_norm_repaired = normalize_for_page_match(page_text, repair_line_break_hyphens=True)
    if q_norm in p_norm_repaired:
        return "unicode_nfkc_hyphen"
    p_norm_plain = normalize_for_page_match(page_text, repair_line_break_hyphens=False)
    if q_norm in p_norm_plain:
        return "unicode_nfkc_hyphen"
    return "mismatch"


def scan_neutral_language(text: str) -> list[NeutralLanguageFlag]:
    """Scan commentary text for evaluative, spin, or compliance-inference claims.

    Intended strictly for worker/system commentary (such as ``note`` fields),
    never for verbatim source quotations (``quotation``, ``statement_verbatim``).
    """
    if not text:
        return []
    flags: list[NeutralLanguageFlag] = []
    for rule, regex in _COMPILED_NEUTRAL_RULES:
        for match in regex.finditer(text):
            flags.append(
                NeutralLanguageFlag(
                    rule_id=rule.rule_id,
                    category=rule.category,
                    matched_text=match.group(0),
                    span=(match.start(), match.end()),
                    rationale=rule.rationale,
                )
            )
    flags.sort(key=lambda item: (item.span[0], item.span[1], item.rule_id))
    return flags


# Alias so callers using either check_neutral_language or scan_neutral_language
# share the exact deterministic implementation.
check_neutral_language = scan_neutral_language


def scan_neutral_language_record(
    record: Mapping[str, Any],
    *,
    exempt_keys: AbstractSet[str] = frozenset({"quotation", "statement_verbatim"}),
    _prefix: str = "",
) -> list[tuple[str, NeutralLanguageFlag]]:
    """Recursively scan all string values in ``record`` outside verbatim exempt keys."""
    findings: list[tuple[str, NeutralLanguageFlag]] = []

    def _walk(obj: Any, path: str) -> None:
        if isinstance(obj, Mapping):
            for key, val in obj.items():
                key_str = str(key)
                if key_str in exempt_keys:
                    continue
                child_path = f"{path}.{key_str}" if path else key_str
                _walk(val, child_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                child_path = f"{path}[{idx}]"
                _walk(item, child_path)
        elif isinstance(obj, str):
            for flag in scan_neutral_language(obj):
                findings.append((path, flag))

    _walk(record, _prefix)
    return findings


class DictPageTextProvider:
    """In-memory ``PageTextProvider`` for deterministic offline tests and fixtures.

    Accepts keys in any of three lookup forms:
    1. ``(sha256, physical_page, text_source)``
    2. ``(sha256, physical_page)``
    3. ``(nct_id, filename, physical_page)`` or ``(nct_id, filename, physical_page, text_source)``
    or a nested mapping ``sha256 -> Sequence[str]`` (1-based ``physical_page``
    indexed at ``physical_page - 1``).
    """

    def __init__(
        self,
        pages_by_key: Mapping[Any, str] | None = None,
        *,
        pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
        ocr_pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        in_scope_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        self._audit_nct_ids: frozenset[str] = _validate_nct_set(
            audit_nct_ids, context="DictPageTextProvider.audit_nct_ids"
        )
        self._in_scope_nct_ids: frozenset[str] | None = (
            _validate_nct_set(in_scope_nct_ids, context="DictPageTextProvider.in_scope_nct_ids")
            if in_scope_nct_ids is not None
            else None
        )
        self._pages_by_key: dict[Any, str] = dict(pages_by_key or {})
        self._pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (pages_by_sha256 or {}).items()
        }
        self._ocr_pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (ocr_pages_by_sha256 or {}).items()
        }
        self._validate_stored_keys()

    def _validate_stored_keys(self) -> None:
        for key in self._pages_by_key:
            if (
                isinstance(key, tuple)
                and len(key) in (3, 4)
                and isinstance(key[1], str)
            ):
                nct_key = validate_strict_nct_id(key[0], context="DictPageTextProvider.nct_id")
                if nct_key in self._audit_nct_ids:
                    raise AuditHoldoutViolationError(
                        f"Audit holdout violation in DictPageTextProvider: {nct_key!r} is in audit_nct_ids."
                    )
                if self._in_scope_nct_ids is not None and nct_key not in self._in_scope_nct_ids:
                    raise AuditHoldoutViolationError(
                        f"Unlisted trial {nct_key!r} in DictPageTextProvider is absent from in_scope_nct_ids."
                    )

    def bind_firewall(
        self,
        *,
        audit_nct_ids: AbstractSet[str],
        in_scope_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        """Bind or tighten firewall sets and re-verify stored keys."""
        merged_audit = self._audit_nct_ids | _validate_nct_set(
            audit_nct_ids, context="DictPageTextProvider.audit_nct_ids"
        )
        merged_scope = self._in_scope_nct_ids
        if in_scope_nct_ids is not None:
            incoming_scope = _validate_nct_set(
                in_scope_nct_ids, context="DictPageTextProvider.in_scope_nct_ids"
            )
            merged_scope = (
                incoming_scope if merged_scope is None else (merged_scope & incoming_scope)
            )
        self._audit_nct_ids = merged_audit
        self._in_scope_nct_ids = merged_scope
        self._validate_stored_keys()

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        """Return single-page text for ``physical_page`` (1-based) or None."""
        checked_nct = validate_strict_nct_id(nct_id, context="page_text.nct_id")
        if checked_nct in self._audit_nct_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: refusing page lookup for {checked_nct!r}."
            )
        if self._in_scope_nct_ids is not None and checked_nct not in self._in_scope_nct_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout refusal: page lookup for unlisted trial {checked_nct!r}."
            )
        if not _is_strict_int(physical_page) or physical_page < 1:
            return None
        for key in (
            (sha256, physical_page, text_source),
            (checked_nct, filename, physical_page, text_source),
            (sha256, physical_page),
            (checked_nct, filename, physical_page),
        ):
            if key in self._pages_by_key:
                return self._pages_by_key[key]

        source_map = (
            self._pages_by_sha256
            if text_source == "pdfkit"
            else self._ocr_pages_by_sha256
        )
        seq = source_map.get(sha256)
        if seq is not None and 1 <= physical_page <= len(seq):
            return seq[physical_page - 1]
        return None


class DirectoryPageTextProvider:
    """Filesystem ``PageTextProvider`` reading JSON, gzipped JSON, or JSONL page maps.

    Supports flat census page directories (``{sha256}.json``,
    ``{nct_id}/{filename}.json``, ``{nct_id}__{filename}.json``), content-addressed
    cache directories (``{sha256[:2]}/{sha256}.json.gz``), and ``.jsonl`` /
    ``.jsonl.gz`` page-map files. Validates every discovered file, path prefix,
    and JSON/JSONL row ``nct_id`` / ``in_audit`` against ``validate_strict_nct_id``,
    ``audit_nct_ids``, and ``in_scope_nct_ids`` before loading any page text.
    """

    def __init__(
        self,
        pages_dir: Path,
        *,
        ocr_dir: Path | None = None,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        in_scope_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        self._pages_dir = Path(pages_dir)
        self._ocr_dir = Path(ocr_dir) if ocr_dir is not None else None
        self._audit_nct_ids: frozenset[str] = _validate_nct_set(
            audit_nct_ids, context="DirectoryPageTextProvider.audit_nct_ids"
        )
        self._in_scope_nct_ids: frozenset[str] | None = (
            _validate_nct_set(
                in_scope_nct_ids, context="DirectoryPageTextProvider.in_scope_nct_ids"
            )
            if in_scope_nct_ids is not None
            else None
        )
        self._cache: dict[tuple[str, str, str, str], dict[int, str] | None] = {}
        self._jsonl_pages: dict[tuple[str, str], dict[int, str]] = {}
        self._jsonl_trial_pages: dict[tuple[str, str, str], dict[int, str]] = {}
        self._preflight_scan_all()

    def bind_firewall(
        self,
        *,
        audit_nct_ids: AbstractSet[str],
        in_scope_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        """Bind or tighten firewall sets and re-scan page-map roots before serving pages."""
        merged_audit = self._audit_nct_ids | _validate_nct_set(
            audit_nct_ids, context="DirectoryPageTextProvider.audit_nct_ids"
        )
        merged_scope = self._in_scope_nct_ids
        if in_scope_nct_ids is not None:
            incoming_scope = _validate_nct_set(
                in_scope_nct_ids, context="DirectoryPageTextProvider.in_scope_nct_ids"
            )
            merged_scope = (
                incoming_scope if merged_scope is None else (merged_scope & incoming_scope)
            )
        if merged_audit != self._audit_nct_ids or merged_scope != self._in_scope_nct_ids:
            self._audit_nct_ids = merged_audit
            self._in_scope_nct_ids = merged_scope
            self._cache.clear()
            self._jsonl_pages.clear()
            self._jsonl_trial_pages.clear()
            self._preflight_scan_all()

    def _check_nct_allowed(self, raw_nct: Any, *, context: str) -> str:
        checked = validate_strict_nct_id(raw_nct, context=context)
        if checked in self._audit_nct_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation in {context}: trial {checked!r} is in audit_nct_ids."
            )
        if self._in_scope_nct_ids is not None and checked not in self._in_scope_nct_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout refusal in {context}: trial {checked!r} is absent from in_scope_nct_ids."
            )
        return checked

    def _validate_payload_firewall(self, payload: Any, *, source_desc: str) -> str | None:
        """Validate ``nct_id``, ``nct_ids``, and ``in_audit`` fields inside a page-map payload."""
        if not isinstance(payload, Mapping):
            return None

        payload_nct: str | None = None
        if "nct_id" in payload:
            payload_nct = self._check_nct_allowed(
                payload["nct_id"], context=f"{source_desc}.nct_id"
            )

        if "nct_ids" in payload:
            raw_ids = payload["nct_ids"]
            if not isinstance(raw_ids, list):
                raise CitationVerificationError(
                    f"{source_desc}.nct_ids must be a list, got {type(raw_ids).__name__}."
                )
            for idx, item in enumerate(raw_ids):
                self._check_nct_allowed(item, context=f"{source_desc}.nct_ids[{idx}]")

        if "in_audit" in payload:
            flag = payload["in_audit"]
            if type(flag) is not bool:
                raise AuditHoldoutViolationError(
                    f"{source_desc} carries non-bool in_audit={flag!r}."
                )
            if flag is True:
                raise AuditHoldoutViolationError(
                    f"Audit holdout violation: {source_desc} carries in_audit=True."
                )

        pages_obj = payload.get("pages")
        if isinstance(pages_obj, list):
            for idx, item in enumerate(pages_obj):
                if isinstance(item, Mapping):
                    self._validate_payload_firewall(
                        item, source_desc=f"{source_desc}.pages[{idx}]"
                    )

        return payload_nct

    @staticmethod
    def _is_jsonl_path(path: Path) -> bool:
        return path.name.endswith((".jsonl", ".jsonl.gz"))

    @staticmethod
    def _is_json_map_path(path: Path) -> bool:
        return not DirectoryPageTextProvider._is_jsonl_path(path) and path.name.endswith(
            (".json", ".json.gz")
        )

    def _ingest_jsonl_file(self, path: Path, *, default_source_bucket: str) -> None:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    row = json.loads(stripped)
                    if not isinstance(row, Mapping):
                        raise CitationVerificationError(
                            f"{path}:{line_no}: expected JSON object in page-map JSONL."
                        )
                    row_nct = self._validate_payload_firewall(
                        row, source_desc=f"{path}:{line_no}"
                    )
                    parsed_pages = self._parse_page_map_payload(row)
                    if parsed_pages is None:
                        continue
                    row_ts = row.get("text_source")
                    bucket = (
                        row_ts
                        if isinstance(row_ts, str) and row_ts
                        else default_source_bucket
                    )
                    sha = row.get("sha256")
                    if isinstance(sha, str) and sha:
                        self._jsonl_pages[(sha, bucket)] = parsed_pages
                        if bucket != default_source_bucket:
                            self._jsonl_pages.setdefault(
                                (sha, default_source_bucket), parsed_pages
                            )
                    fn = row.get("filename")
                    if row_nct is not None and isinstance(fn, str) and fn:
                        self._jsonl_trial_pages[(row_nct, fn, bucket)] = parsed_pages
                        if bucket != default_source_bucket:
                            self._jsonl_trial_pages.setdefault(
                                (row_nct, fn, default_source_bucket), parsed_pages
                            )
        except json.JSONDecodeError as exc:
            raise CitationVerificationError(f"Malformed JSON in {path}: {exc}") from exc

    def _scan_root(self, root: Path, *, default_source_bucket: str) -> None:
        if not root.exists():
            return
        if root.is_file():
            if self._is_jsonl_path(root):
                self._ingest_jsonl_file(root, default_source_bucket=default_source_bucket)
            elif self._is_json_map_path(root):
                payload = self._read_json_file(root)
                self._validate_payload_firewall(payload, source_desc=str(root))
            return

        for entry in sorted(root.rglob("*")):
            if entry.is_dir():
                if entry.parent == root and _HEX2_DIR_RE.fullmatch(entry.name) is not None:
                    continue
                if entry.parent == root or entry.name.lower().startswith("nct"):
                    self._check_nct_allowed(
                        entry.name, context=f"page_map_dir({entry})"
                    )
                continue
            if not entry.is_file():
                continue
            stem_name = entry.name[:-3] if entry.name.endswith(".gz") else entry.name
            for ext in (".jsonl", ".json"):
                if stem_name.endswith(ext):
                    stem_name = stem_name[: -len(ext)]
                    break
            if "__" in stem_name:
                prefix, _ = stem_name.split("__", 1)
                self._check_nct_allowed(prefix, context=f"page_map_file({entry})")
            elif _SHA256_RE.fullmatch(stem_name) is None and stem_name.lower().startswith("nct"):
                self._check_nct_allowed(stem_name, context=f"page_map_file({entry})")
            if self._is_jsonl_path(entry):
                self._ingest_jsonl_file(entry, default_source_bucket=default_source_bucket)
            elif self._is_json_map_path(entry):
                payload = self._read_json_file(entry)
                self._validate_payload_firewall(payload, source_desc=str(entry))

    def _preflight_scan_all(self) -> None:
        self._scan_root(self._pages_dir, default_source_bucket="pdfkit")
        if self._ocr_dir is not None:
            self._scan_root(self._ocr_dir, default_source_bucket="ocr")

    @staticmethod
    def _read_json_file(path: Path) -> Any:
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return json.load(fh)
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise CitationVerificationError(f"Malformed JSON in {path}: {exc}") from exc

    def _candidate_paths(
        self,
        base_dir: Path,
        nct_id: str,
        filename: str,
        sha256: str,
    ) -> list[Path]:
        if base_dir.is_file():
            return [base_dir] if self._is_json_map_path(base_dir) else []
        candidates: list[Path] = []
        if sha256:
            candidates.extend(
                [
                    base_dir / f"{sha256}.json",
                    base_dir / f"{sha256}.json.gz",
                    base_dir / sha256[:2] / f"{sha256}.json.gz",
                    base_dir / sha256[:2] / f"{sha256}.json",
                ]
            )
        if nct_id and filename:
            stem = Path(filename).stem
            candidates.extend(
                [
                    base_dir / nct_id / f"{filename}.json",
                    base_dir / nct_id / f"{filename}.json.gz",
                    base_dir / nct_id / f"{stem}.json",
                    base_dir / f"{nct_id}__{filename}.json",
                    base_dir / f"{nct_id}__{stem}.json",
                ]
            )
        return candidates

    @staticmethod
    def _parse_page_map_payload(payload: Any) -> dict[int, str] | None:
        if isinstance(payload, list):
            return {idx + 1: str(val) for idx, val in enumerate(payload)}
        if not isinstance(payload, Mapping):
            return None
        pages_obj = payload.get("pages")
        if isinstance(pages_obj, list):
            result: dict[int, str] = {}
            for idx, item in enumerate(pages_obj):
                if isinstance(item, str):
                    result[idx + 1] = item
                elif isinstance(item, Mapping):
                    page_num = item.get("physical_page", item.get("page", idx + 1))
                    if _is_strict_int(page_num) and page_num >= 1:
                        result[page_num] = str(item.get("text", ""))
            return result
        if isinstance(pages_obj, Mapping):
            result = {}
            for key, val in pages_obj.items():
                try:
                    page_num = int(str(key))
                except ValueError:
                    continue
                if page_num >= 1:
                    result[page_num] = str(val)
            return result
        # Flat mapping {"1": "...", "2": "..."}
        numeric_map: dict[int, str] = {}
        for key, val in payload.items():
            if isinstance(key, str) and key.isdigit() and isinstance(val, str):
                numeric_map[int(key)] = val
        return numeric_map if numeric_map else None

    def _load_doc_pages(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        text_source: str,
    ) -> dict[int, str] | None:
        cache_key = (nct_id, filename, sha256, text_source)
        if cache_key in self._cache:
            return self._cache[cache_key]

        bucket_fallback = "pdfkit" if text_source == "pdfkit" else "ocr"
        for jsonl_key in ((sha256, text_source), (sha256, bucket_fallback)):
            if jsonl_key in self._jsonl_pages:
                loaded_jsonl = self._jsonl_pages[jsonl_key]
                self._cache[cache_key] = loaded_jsonl
                return loaded_jsonl
        for trial_key in ((nct_id, filename, text_source), (nct_id, filename, bucket_fallback)):
            if trial_key in self._jsonl_trial_pages:
                loaded_trial = self._jsonl_trial_pages[trial_key]
                self._cache[cache_key] = loaded_trial
                return loaded_trial

        search_dirs: list[Path] = []
        if text_source != "pdfkit" and self._ocr_dir is not None:
            search_dirs.append(self._ocr_dir)
        else:
            search_dirs.append(self._pages_dir)

        loaded: dict[int, str] | None = None
        for base_dir in search_dirs:
            for candidate in self._candidate_paths(base_dir, nct_id, filename, sha256):
                if not candidate.is_file():
                    continue
                payload = self._read_json_file(candidate)
                payload_nct = self._validate_payload_firewall(
                    payload, source_desc=str(candidate)
                )
                if payload_nct is not None and payload_nct != nct_id:
                    raise CitationVerificationError(
                        f"Page map {candidate} declares nct_id={payload_nct!r}, "
                        f"expected {nct_id!r}."
                    )
                loaded = self._parse_page_map_payload(payload)
                if loaded is not None:
                    break
            if loaded is not None:
                break

        self._cache[cache_key] = loaded
        return loaded

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        """Return single-page text for ``physical_page`` (1-based) or None."""
        checked_nct = self._check_nct_allowed(nct_id, context="page_text.nct_id")
        if not _is_strict_int(physical_page) or physical_page < 1:
            return None
        pages = self._load_doc_pages(checked_nct, filename, sha256, text_source)
        if pages is None:
            return None
        return pages.get(physical_page)


def verify_document_record(
    doc: Mapping[str, Any],
    *,
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
    strict_keys: bool = True,
    pdf_root: Path | None = None,
) -> list[VerificationIssue]:
    """Validate a single document record against firewall, provenance, schema, and hash rules."""
    if not isinstance(doc, Mapping):
        raise CitationVerificationError(
            f"Document record must be a mapping, got {type(doc).__name__}."
        )

    nct_id = _enforce_record_firewall(
        doc,
        record_label="document",
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )

    issues: list[VerificationIssue] = []
    filename_raw = doc.get("filename")
    filename = filename_raw if type(filename_raw) is str else None

    keys = set(doc.keys())
    required = set(DOCUMENT_REQUIRED_KEYS)
    missing = sorted(required - keys)
    if missing:
        issues.append(
            VerificationIssue(
                code="DOCUMENT_MISSING_KEYS",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Document record missing required keys: {missing}",
            )
        )

    allowed = (required | {"in_audit"}) if strict_keys else (required | set(DOCUMENT_OPTIONAL_KEYS))
    extra = sorted(str(k) for k in keys - allowed)
    if extra:
        issues.append(
            VerificationIssue(
                code="DOCUMENT_EXTRA_KEYS",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Document record has unexpected keys: {extra}",
            )
        )

    if "model_id" in doc and not _is_non_empty_str(doc.get("model_id")):
        issues.append(
            VerificationIssue(
                code="INVALID_DOCUMENT_MODEL_ID",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=(
                    f"Document model_id must be a non-empty string, got {doc.get('model_id')!r}"
                ),
            )
        )

    if "profile_version" in doc and not _is_non_empty_str(doc.get("profile_version")):
        issues.append(
            VerificationIssue(
                code="INVALID_DOCUMENT_PROFILE_VERSION",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=(
                    f"Document profile_version must be a non-empty string, "
                    f"got {doc.get('profile_version')!r}"
                ),
            )
        )

    if not _is_safe_pdf_filename(filename_raw):
        issues.append(
            VerificationIssue(
                code="INVALID_DOCUMENT_FILENAME",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Unsafe or non-PDF filename {filename_raw!r}",
            )
        )

    doc_type = doc.get("doc_type")
    if type(doc_type) is not str or doc_type not in DOC_TYPES:
        issues.append(
            VerificationIssue(
                code="INVALID_DOC_TYPE",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Invalid doc_type {doc_type!r}; expected one of {DOC_TYPES}",
            )
        )

    unreadable_subtype = doc.get("unreadable_subtype")
    if unreadable_subtype is not None and (
        type(unreadable_subtype) is not str
        or unreadable_subtype not in UNREADABLE_SUBTYPES
    ):
        issues.append(
            VerificationIssue(
                code="INVALID_UNREADABLE_SUBTYPE",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=(
                    f"Invalid unreadable_subtype {unreadable_subtype!r}; "
                    f"expected one of {UNREADABLE_SUBTYPES}"
                ),
            )
        )

    http_status = doc.get("http_status")
    if http_status is not None and not _is_strict_int(http_status):
        issues.append(
            VerificationIssue(
                code="INVALID_HTTP_STATUS",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"http_status must be int or None, got {type(http_status).__name__}",
            )
        )

    sha256 = doc.get("sha256")
    byte_count = doc.get("bytes")
    physical_pages = doc.get("physical_pages")
    pages_without_text = doc.get("pages_without_text")

    if http_status == 200:
        if (
            type(sha256) is not str
            or len(sha256) != 64
            or _SHA256_RE.fullmatch(sha256) is None
        ):
            issues.append(
                VerificationIssue(
                    code="INVALID_SHA256",
                    severity="error",
                    nct_id=nct_id,
                    location_key=None,
                    filename=filename,
                    physical_page=None,
                    message=f"http_status=200 requires 64-hex lowercase sha256, got {sha256!r}",
                )
            )
        if not _is_strict_int(byte_count) or byte_count <= 0:
            issues.append(
                VerificationIssue(
                    code="INVALID_BYTE_COUNT",
                    severity="error",
                    nct_id=nct_id,
                    location_key=None,
                    filename=filename,
                    physical_page=None,
                    message=f"http_status=200 requires positive int bytes, got {byte_count!r}",
                )
            )
        if not _is_strict_int(physical_pages) or physical_pages < 1:
            issues.append(
                VerificationIssue(
                    code="INVALID_PHYSICAL_PAGES",
                    severity="error",
                    nct_id=nct_id,
                    location_key=None,
                    filename=filename,
                    physical_page=None,
                    message=(
                        f"http_status=200 requires int physical_pages >= 1, "
                        f"got {physical_pages!r}"
                    ),
                )
            )
    else:
        if physical_pages is not None and (
            not _is_strict_int(physical_pages) or physical_pages < 0
        ):
            issues.append(
                VerificationIssue(
                    code="INVALID_PHYSICAL_PAGES",
                    severity="error",
                    nct_id=nct_id,
                    location_key=None,
                    filename=filename,
                    physical_page=None,
                    message=f"physical_pages must be non-negative int, got {physical_pages!r}",
                )
            )

    if not isinstance(pages_without_text, list):
        issues.append(
            VerificationIssue(
                code="INVALID_PAGES_WITHOUT_TEXT",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message="pages_without_text must be a list of strictly ascending ints",
            )
        )
    else:
        max_page = physical_pages if _is_strict_int(physical_pages) else 0
        prev_page: int | None = None
        for p in pages_without_text:
            if not _is_strict_int(p) or p < 1 or (max_page > 0 and p > max_page):
                issues.append(
                    VerificationIssue(
                        code="INVALID_PAGES_WITHOUT_TEXT",
                        severity="error",
                        nct_id=nct_id,
                        location_key=None,
                        filename=filename,
                        physical_page=p if _is_strict_int(p) else None,
                        message=(
                            f"pages_without_text entry {p!r} out of valid range 1..{max_page}"
                        ),
                    )
                )
                break
            if prev_page is not None and p <= prev_page:
                issues.append(
                    VerificationIssue(
                        code="INVALID_PAGES_WITHOUT_TEXT",
                        severity="error",
                        nct_id=nct_id,
                        location_key=None,
                        filename=filename,
                        physical_page=p,
                        message="pages_without_text must be strictly ascending without duplicates",
                    )
                )
                break
            prev_page = p

    # Optional on-disk byte + SHA-256 verification when pdf_root is supplied.
    if (
        pdf_root is not None
        and http_status == 200
        and filename is not None
        and _is_safe_pdf_filename(filename)
        and type(sha256) is str
        and len(sha256) == 64
        and _SHA256_RE.fullmatch(sha256) is not None
    ):
        candidate_pdf = pdf_root / nct_id / filename
        if not candidate_pdf.is_file():
            candidate_pdf = pdf_root / sha256[:2] / f"{sha256}.pdf"
        if not candidate_pdf.is_file():
            issues.append(
                VerificationIssue(
                    code="DOCUMENT_PDF_MISSING_ON_DISK",
                    severity="error",
                    nct_id=nct_id,
                    location_key=None,
                    filename=filename,
                    physical_page=None,
                    message=f"Expected PDF not found under {pdf_root} for {nct_id}/{filename}",
                )
            )
        else:
            raw_bytes = candidate_pdf.read_bytes()
            if _is_strict_int(byte_count) and len(raw_bytes) != byte_count:
                issues.append(
                    VerificationIssue(
                        code="DOCUMENT_BYTES_MISMATCH",
                        severity="error",
                        nct_id=nct_id,
                        location_key=None,
                        filename=filename,
                        physical_page=None,
                        message=(
                            f"On-disk bytes ({len(raw_bytes)}) != recorded bytes ({byte_count})"
                        ),
                    )
                )
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha != sha256:
                issues.append(
                    VerificationIssue(
                        code="DOCUMENT_SHA256_MISMATCH",
                        severity="error",
                        nct_id=nct_id,
                        location_key=None,
                        filename=filename,
                        physical_page=None,
                        message=f"On-disk sha256 ({actual_sha}) != recorded sha256 ({sha256})",
                    )
                )

    return issues


def _extract_unsearched_pages_for_file(
    pages_not_searched: Any,
    filename: str,
) -> set[int]:
    """Normalize ``pages_not_searched`` entries for a specific document filename."""
    result: set[int] = set()
    if isinstance(pages_not_searched, Mapping):
        raw = pages_not_searched.get(filename)
        if isinstance(raw, list):
            for item in raw:
                if _is_strict_int(item):
                    result.add(item)
        return result
    if isinstance(pages_not_searched, list):
        for item in pages_not_searched:
            if _is_strict_int(item):
                result.add(item)
            elif isinstance(item, Mapping) and item.get("filename") == filename:
                pages = item.get("pages", [])
                if isinstance(pages, list):
                    for p in pages:
                        if _is_strict_int(p):
                            result.add(p)
    return result


def _citation_shape_error_code(message: str) -> str:
    """Map a ``validate_citation_shape`` error string to its ``VerificationIssue`` code."""
    if message.startswith("Citation must be a JSON object"):
        return "INVALID_CITATION_TYPE"
    if message.startswith("Citation missing required keys"):
        return "CITATION_MISSING_KEYS"
    if message.startswith("Citation has unexpected keys") or message.startswith(
        "Citation keys must all be strings"
    ):
        return "CITATION_EXTRA_KEYS"
    if message.startswith("Citation filename"):
        return "INVALID_CITATION_FILENAME"
    if message.startswith("Citation sha256"):
        return "INVALID_CITATION_SHA256"
    if "not a span" in message:
        return "cross_page_span_violation"
    if message.startswith("Citation physical_page"):
        return "INVALID_CITATION_PAGE"
    if message.startswith("Citation printed_page"):
        return "INVALID_CITATION_PRINTED_PAGE"
    if message.startswith("Citation section_heading"):
        return "INVALID_CITATION_SECTION_HEADING"
    if message.startswith("Citation quotation must be a non-empty string"):
        return "CITATION_EMPTY_QUOTATION"
    if "exceeds maximum" in message:
        return "CITATION_QUOTATION_TOO_LONG"
    if "canonical single-space whitespace" in message:
        return "CITATION_QUOTATION_UNNORMALIZED_WHITESPACE"
    if "ellipsis" in message:
        return "CITATION_ELLIPSIS_FORBIDDEN"
    return "INVALID_CITATION_SHAPE"


def verify_citation(
    nct_id: str,
    location_key: tuple[int, str, str],
    citation: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
    pages_not_searched: Any = None,
    page_text_provider: PageTextProvider | None = None,
    text_source: str = "pdfkit",
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
) -> tuple[MatchTier | None, list[VerificationIssue]]:
    """Verify a single citation's shape, bounds, hygiene, and single-page substring match."""
    checked_nct = validate_strict_nct_id(nct_id, context="citation.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    if checked_nct in checked_audit_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: trial {checked_nct!r} is in audit_nct_ids."
        )
    if checked_scope_ids is not None and checked_nct not in checked_scope_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout refusal: trial {checked_nct!r} is absent from in_scope_nct_ids."
        )
    if isinstance(citation, Mapping) and "in_audit" in citation:
        cit_flag = citation["in_audit"]
        if type(cit_flag) is not bool or cit_flag is True:
            raise AuditHoldoutViolationError(
                f"Citation for {checked_nct!r} carries forbidden in_audit={cit_flag!r}."
            )
    for doc_fn, doc_obj in docs_by_filename.items():
        if isinstance(doc_obj, Mapping):
            if "nct_id" in doc_obj:
                doc_nct = validate_strict_nct_id(
                    doc_obj["nct_id"], context=f"docs_by_filename[{doc_fn!r}].nct_id"
                )
                if doc_nct in checked_audit_ids:
                    raise AuditHoldoutViolationError(
                        f"Audit holdout violation in docs_by_filename[{doc_fn!r}]: {doc_nct!r}."
                    )
                if checked_scope_ids is not None and doc_nct not in checked_scope_ids:
                    raise AuditHoldoutViolationError(
                        f"Unlisted trial in docs_by_filename[{doc_fn!r}]: {doc_nct!r}."
                    )
            if "in_audit" in doc_obj:
                d_flag = doc_obj["in_audit"]
                if type(d_flag) is not bool or d_flag is True:
                    raise AuditHoldoutViolationError(
                        f"Document {doc_fn!r} carries forbidden in_audit={d_flag!r}."
                    )

    issues: list[VerificationIssue] = []
    shape_errors = validate_citation_shape(citation, allow_none_optional_strings=False)
    if not isinstance(citation, Mapping):
        for err_msg in shape_errors:
            issues.append(
                VerificationIssue(
                    code=_citation_shape_error_code(err_msg),
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=None,
                    physical_page=None,
                    message=err_msg,
                )
            )
        return None, issues

    filename_raw = citation.get("filename")
    filename = filename_raw if _is_safe_pdf_filename(filename_raw) else None
    phys_raw = citation.get("physical_page")
    phys_page = phys_raw if (_is_strict_int(phys_raw) and phys_raw >= 1) else None

    for err_msg in shape_errors:
        code = _citation_shape_error_code(err_msg)
        issue_page = (
            None
            if code in {"cross_page_span_violation", "INVALID_CITATION_PAGE"}
            else phys_page
        )
        issues.append(
            VerificationIssue(
                code=code,
                severity="error",
                nct_id=checked_nct,
                location_key=location_key,
                filename=filename_raw if type(filename_raw) is str else None,
                physical_page=issue_page,
                message=err_msg,
            )
        )

    lookup_name = filename_raw if type(filename_raw) is str else None
    doc = docs_by_filename.get(lookup_name) if lookup_name is not None else None
    doc_sha = ""
    doc_pages = 0
    if doc is None:
        issues.append(
            VerificationIssue(
                code="CITATION_UNKNOWN_DOCUMENT",
                severity="error",
                nct_id=checked_nct,
                location_key=location_key,
                filename=lookup_name,
                physical_page=phys_page,
                message=f"Citation references unknown document {lookup_name!r} for {checked_nct}",
            )
        )
    else:
        if doc.get("http_status") != 200:
            issues.append(
                VerificationIssue(
                    code="CITATION_FAILED_DOCUMENT",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=lookup_name,
                    physical_page=phys_page,
                    message=(
                        f"Citation references document {lookup_name!r} with "
                        f"http_status={doc.get('http_status')!r} != 200"
                    ),
                )
            )
        doc_sha = doc.get("sha256") if type(doc.get("sha256")) is str else ""
        cit_sha = citation.get("sha256")
        if (
            cit_sha != doc_sha
            or type(cit_sha) is not str
            or len(cit_sha) != 64
            or _SHA256_RE.fullmatch(cit_sha) is None
        ):
            issues.append(
                VerificationIssue(
                    code="CITATION_SHA256_MISMATCH",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=lookup_name,
                    physical_page=phys_page,
                    message=(
                        f"Citation sha256 {cit_sha!r} does not match document sha256 {doc_sha!r}"
                    ),
                )
            )
        raw_doc_pages = doc.get("physical_pages")
        if _is_strict_int(raw_doc_pages):
            doc_pages = raw_doc_pages
            if phys_page is not None and not (1 <= phys_page <= doc_pages):
                issues.append(
                    VerificationIssue(
                        code="CITATION_PAGE_OUT_OF_RANGE",
                        severity="error",
                        nct_id=checked_nct,
                        location_key=location_key,
                        filename=lookup_name,
                        physical_page=phys_page,
                        message=(
                            f"Citation physical_page {phys_page} out of range 1..{doc_pages}"
                        ),
                    )
                )
        pages_without_text = doc.get("pages_without_text")
        if (
            text_source == "pdfkit"
            and phys_page is not None
            and isinstance(pages_without_text, list)
            and phys_page in pages_without_text
        ):
            issues.append(
                VerificationIssue(
                    code="CITATION_PAGE_WITHOUT_TEXT",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=lookup_name,
                    physical_page=phys_page,
                    message=(
                        f"Citation under text_source='pdfkit' cites page {phys_page} "
                        f"which is listed in pages_without_text"
                    ),
                )
            )

    if lookup_name is not None and phys_page is not None and pages_not_searched:
        unsearched = _extract_unsearched_pages_for_file(pages_not_searched, lookup_name)
        if phys_page in unsearched:
            issues.append(
                VerificationIssue(
                    code="CITATION_IN_UNSEARCHED_PAGES",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=lookup_name,
                    physical_page=phys_page,
                    message=(
                        f"Citation page {phys_page} in {lookup_name!r} falls inside pages_not_searched"
                    ),
                )
            )

    quotation = citation.get("quotation")
    quotation_valid = type(quotation) is str and len(quotation) > 0

    match_tier: MatchTier | None = None
    if (
        page_text_provider is not None
        and quotation_valid
        and type(quotation) is str
        and filename is not None
        and phys_page is not None
        and doc is not None
        and doc.get("http_status") == 200
        and (doc_pages == 0 or 1 <= phys_page <= doc_pages)
    ):
        page_text = page_text_provider.get_page_text(
            checked_nct,
            filename,
            doc_sha,
            phys_page,
            text_source=text_source,
        )
        if page_text is None:
            issues.append(
                VerificationIssue(
                    code="CITATION_PAGE_TEXT_UNAVAILABLE",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Page text unavailable for {checked_nct}/{filename} page {phys_page} "
                        f"(text_source={text_source!r})"
                    ),
                )
            )
            match_tier = "mismatch"
        else:
            match_tier = match_quotation_on_page(quotation, page_text)
            if match_tier == "mismatch":
                # Check adjacent pages solely to diagnose a cross-page span
                # violation; never accept a cross-page match.
                spans_adjacent = False
                if phys_page > 1:
                    prev_text = page_text_provider.get_page_text(
                        checked_nct,
                        filename,
                        doc_sha,
                        phys_page - 1,
                        text_source=text_source,
                    )
                    if prev_text is not None and (
                        match_quotation_on_page(quotation, f"{prev_text}\n{page_text}")
                        != "mismatch"
                    ):
                        spans_adjacent = True
                if not spans_adjacent and (doc_pages == 0 or phys_page < doc_pages):
                    next_text = page_text_provider.get_page_text(
                        checked_nct,
                        filename,
                        doc_sha,
                        phys_page + 1,
                        text_source=text_source,
                    )
                    if next_text is not None and (
                        match_quotation_on_page(quotation, f"{page_text}\n{next_text}")
                        != "mismatch"
                    ):
                        spans_adjacent = True

                if spans_adjacent:
                    issues.append(
                        VerificationIssue(
                            code="cross_page_span_violation",
                            severity="error",
                            nct_id=checked_nct,
                            location_key=location_key,
                            filename=filename,
                            physical_page=phys_page,
                            message=(
                                f"Quotation spans across a page boundary adjacent to "
                                f"{filename} page {phys_page}; single-page citations only."
                            ),
                        )
                    )
                else:
                    issues.append(
                        VerificationIssue(
                            code="CITATION_PAGE_SUBSTRING_MISMATCH",
                            severity="error",
                            nct_id=checked_nct,
                            location_key=location_key,
                            filename=filename,
                            physical_page=phys_page,
                            message=(
                                f"Quotation does not match single-page text on "
                                f"{filename} page {phys_page}."
                            ),
                        )
                    )

    return match_tier, issues


def check_note_page_counts(
    nct_id: str,
    location_key: tuple[int, str, str],
    note: str,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
) -> list[VerificationIssue]:
    """Advisory check flagging ``<filename>.pdf (<N> pages)`` disagreements in ``note``."""
    checked_nct = validate_strict_nct_id(nct_id, context="note.nct_id")
    warnings: list[VerificationIssue] = []
    if not note:
        return warnings
    for match in _NOTE_PAGE_COUNT_RE.finditer(note):
        ref_filename = match.group(1)
        stated_pages = int(match.group(2))
        doc = docs_by_filename.get(ref_filename)
        if doc is None:
            continue
        actual_pages = doc.get("physical_pages")
        if _is_strict_int(actual_pages) and actual_pages != stated_pages:
            warnings.append(
                VerificationIssue(
                    code="NOTE_PAGE_COUNT_MISMATCH",
                    severity="warning",
                    nct_id=checked_nct,
                    location_key=location_key,
                    filename=ref_filename,
                    physical_page=None,
                    message=(
                        f"Note states {ref_filename} ({stated_pages} pages), "
                        f"but document metadata records physical_pages={actual_pages}."
                    ),
                )
            )
    return warnings


def _is_non_empty_pages_not_searched(pages_not_searched: Any) -> bool:
    if isinstance(pages_not_searched, list):
        return len(pages_not_searched) > 0
    if isinstance(pages_not_searched, Mapping):
        return any(bool(v) for v in pages_not_searched.values())
    return bool(pages_not_searched)


def _validate_page_partition(
    nct_id: str,
    location_key: tuple[int, str, str],
    pages_searched: Any,
    pages_not_searched: Any,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
) -> list[VerificationIssue]:
    """Validate disjoint partition when ``pages_not_searched`` is non-empty."""
    issues: list[VerificationIssue] = []
    if not isinstance(pages_searched, Mapping) or len(pages_searched) == 0:
        issues.append(
            VerificationIssue(
                code="INVALID_PARTIAL_PAGES_SEARCHED",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message=(
                    "When pages_not_searched is non-empty, pages_searched must be an "
                    "explicit non-empty per-document dict[str, list[int]]."
                ),
            )
        )
        return issues

    if not isinstance(pages_not_searched, Mapping):
        issues.append(
            VerificationIssue(
                code="INVALID_PARTIAL_PAGES_NOT_SEARCHED",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message=(
                    "When pages_not_searched is non-empty with partial search, it must "
                    "be a per-document dict[str, list[int]] matching pages_searched."
                ),
            )
        )
        return issues

    all_doc_names = set(pages_searched.keys()) | set(pages_not_searched.keys())
    for fn in sorted(all_doc_names):
        doc = docs_by_filename.get(fn)
        if doc is None:
            issues.append(
                VerificationIssue(
                    code="SEARCH_PARTITION_UNKNOWN_DOCUMENT",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=fn,
                    physical_page=None,
                    message=f"Search coverage references unknown document {fn!r}.",
                )
            )
            continue
        phys_pages = doc.get("physical_pages")
        if not _is_strict_int(phys_pages) or phys_pages < 1:
            continue

        searched_raw = pages_searched.get(fn, [])
        unsearched_raw = pages_not_searched.get(fn, [])
        blank_raw = doc.get("pages_without_text", [])

        if not isinstance(searched_raw, list) or not all(_is_strict_int(p) for p in searched_raw):
            issues.append(
                VerificationIssue(
                    code="INVALID_PARTIAL_PAGES_SEARCHED",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=fn,
                    physical_page=None,
                    message=f"pages_searched[{fn!r}] must be a list of ints.",
                )
            )
            continue
        if not isinstance(unsearched_raw, list) or not all(
            _is_strict_int(p) for p in unsearched_raw
        ):
            issues.append(
                VerificationIssue(
                    code="INVALID_PARTIAL_PAGES_NOT_SEARCHED",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=fn,
                    physical_page=None,
                    message=f"pages_not_searched[{fn!r}] must be a list of ints.",
                )
            )
            continue

        searched_set = set(searched_raw)
        unsearched_set = set(unsearched_raw)
        blank_set = set(p for p in blank_raw if _is_strict_int(p))
        if (
            len(searched_set) != len(searched_raw)
            or len(unsearched_set) != len(unsearched_raw)
            or (searched_set & unsearched_set)
            or (searched_set & blank_set)
            or (unsearched_set & blank_set)
        ):
            issues.append(
                VerificationIssue(
                    code="SEARCH_PARTITION_OVERLAP",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=fn,
                    physical_page=None,
                    message=(
                        f"pages_searched, pages_not_searched, and pages_without_text "
                        f"overlap or contain duplicates for {fn!r}."
                    ),
                )
            )
            continue

        expected_pages = set(range(1, phys_pages + 1))
        union_pages = searched_set | unsearched_set | blank_set
        if union_pages != expected_pages:
            issues.append(
                VerificationIssue(
                    code="SEARCH_PARTITION_INCOMPLETE",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=fn,
                    physical_page=None,
                    message=(
                        f"pages_searched U pages_not_searched U pages_without_text does "
                        f"not equal 1..{phys_pages} for {fn!r}."
                    ),
                )
            )

    return issues


def _verify_unreadable_validity(
    nct_id: str,
    location_key: tuple[int, str, str],
    loc: Mapping[str, Any],
    docs_by_filename: Mapping[str, Mapping[str, Any]],
) -> list[VerificationIssue]:
    """Validate that ``status == 'UNREADABLE'`` is backed by document or subtype evidence."""
    issues: list[VerificationIssue] = []
    if not docs_by_filename:
        issues.append(
            VerificationIssue(
                code="UNREADABLE_WITHOUT_DOCUMENT",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message=(
                    "status='UNREADABLE' requires at least one posted document; "
                    "use 'NO_DOCUMENT' when no in-scope document exists."
                ),
            )
        )
        return issues

    loc_subtype = loc.get("unreadable_subtype")
    if loc_subtype is not None and (
        type(loc_subtype) is not str or loc_subtype not in UNREADABLE_SUBTYPES
    ):
        issues.append(
            VerificationIssue(
                code="INVALID_UNREADABLE_SUBTYPE",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message=(
                    f"Invalid unreadable_subtype {loc_subtype!r}; "
                    f"expected one of {UNREADABLE_SUBTYPES}"
                ),
            )
        )
        return issues

    docs = list(docs_by_filename.values())
    all_explicitly_machine_readable = all(
        doc.get("readability_status") == "machine_readable"
        and not doc.get("pages_without_text")
        and not doc.get("redaction_markers")
        and doc.get("unreadable_subtype") is None
        for doc in docs
    )
    if all_explicitly_machine_readable and loc_subtype is None:
        issues.append(
            VerificationIssue(
                code="UNREADABLE_ON_MACHINE_READABLE_DOCUMENT",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message=(
                    "status='UNREADABLE' contradicts document profile where all in-scope "
                    "documents are readability_status='machine_readable' with zero blank "
                    "pages and zero redaction markers."
                ),
            )
        )
        return issues

    has_structural_signal = any(
        bool(doc.get("pages_without_text"))
        or doc.get("http_status") != 200
        or doc.get("readability_status")
        in {"image_only_scan", "garbled_cmap", "partial_image_or_redacted", "download_failed"}
        or bool(doc.get("redaction_markers"))
        or doc.get("unreadable_subtype") in UNREADABLE_SUBTYPES
        for doc in docs
    )
    note = loc.get("note") if type(loc.get("note")) is str else ""
    if not has_structural_signal and loc_subtype is None:
        if not note.strip() or not _UNREADABLE_NOTE_HINT_RE.search(note):
            issues.append(
                VerificationIssue(
                    code="UNREADABLE_MISSING_EVIDENCE",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=None,
                    physical_page=None,
                    message=(
                        "status='UNREADABLE' has no pages_without_text, no unreadable "
                        "document metadata, and no note explaining scan/redaction/garbled CMap."
                    ),
                )
            )
    return issues


def _status_citation_error_code(message: str) -> str:
    """Map a ``validate_status_citations`` error string to its ``VerificationIssue`` code."""
    if message.startswith("Invalid status"):
        return "INVALID_STATUS"
    if "requires at least 1 citation" in message:
        return "STATUS_REQUIRES_CITATION"
    if "requires 0 citations" in message:
        return "STATUS_FORBIDS_CITATIONS"
    if "status='NO_DOCUMENT' is invalid" in message:
        return "NO_DOCUMENT_WITH_POSTED_DOCUMENT"
    if "Citations are forbidden when no posted documents exist" in message:
        return "CITATIONS_WITHOUT_POSTED_DOCUMENT"
    return "INVALID_STATUS_CITATIONS"


def verify_location_record(
    loc: Mapping[str, Any],
    *,
    docs_by_nct: Mapping[str, Mapping[str, Mapping[str, Any]]],
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
    page_text_provider: PageTextProvider | None = None,
    require_exhaustive_for_not_found: bool = True,
    text_source: str = "pdfkit",
    manifest_has_documents: bool = False,
) -> tuple[dict[str, int], list[VerificationIssue]]:
    """Verify a single location record, provenance, citations, coverage, and neutrality."""
    if not isinstance(loc, Mapping):
        raise CitationVerificationError(
            f"Location record must be a mapping, got {type(loc).__name__}."
        )
    if type(manifest_has_documents) is not bool:
        raise CitationVerificationError(
            f"manifest_has_documents must be bool, got {manifest_has_documents!r}."
        )
    for k in docs_by_nct:
        validate_strict_nct_id(k, context="docs_by_nct key")

    nct_id = _enforce_record_firewall(
        loc,
        record_label="location",
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )

    if isinstance(page_text_provider, (DirectoryPageTextProvider, DictPageTextProvider)):
        page_text_provider.bind_firewall(
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
        )

    idx_raw = loc.get("primary_outcome_index")
    elem_raw = loc.get("element")
    pass_raw = loc.get("pass")
    loc_key: tuple[int, str, str] = (
        idx_raw if _is_strict_int(idx_raw) else -1,
        elem_raw if type(elem_raw) is str else "",
        pass_raw if type(pass_raw) is str else "",
    )

    counts: dict[str, int] = {
        "citations_checked": 0,
        "exact_ws_matches": 0,
        "unicode_nfkc_hyphen_matches": 0,
        "substring_mismatches": 0,
        "ellipsis_flags": 0,
        "neutral_language_flags": 0,
    }
    issues: list[VerificationIssue] = []

    keys = set(loc.keys())
    required = set(LOCATION_REQUIRED_KEYS)
    missing = sorted(required - keys)
    if missing:
        issues.append(
            VerificationIssue(
                code="LOCATION_MISSING_KEYS",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"Location record missing required keys: {missing}",
            )
        )

    if "model_id" in loc and not _is_non_empty_str(loc.get("model_id")):
        issues.append(
            VerificationIssue(
                code="INVALID_LOCATION_MODEL_ID",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=(
                    f"Location model_id must be a non-empty string, got {loc.get('model_id')!r}"
                ),
            )
        )

    if "prompt_version" in loc and not _is_non_empty_str(loc.get("prompt_version")):
        issues.append(
            VerificationIssue(
                code="INVALID_LOCATION_PROMPT_VERSION",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=(
                    f"Location prompt_version must be a non-empty string, "
                    f"got {loc.get('prompt_version')!r}"
                ),
            )
        )

    if not _is_strict_int(idx_raw) or idx_raw < 0:
        issues.append(
            VerificationIssue(
                code="INVALID_PRIMARY_OUTCOME_INDEX",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"primary_outcome_index must be non-negative int, got {idx_raw!r}",
            )
        )
    if type(elem_raw) is not str or elem_raw not in ELEMENTS:
        issues.append(
            VerificationIssue(
                code="INVALID_ELEMENT",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"Invalid element {elem_raw!r}; expected one of {ELEMENTS}",
            )
        )
    if type(pass_raw) is not str or pass_raw not in ALLOWED_PASSES:
        issues.append(
            VerificationIssue(
                code="INVALID_PASS",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"Invalid pass {pass_raw!r}; expected one of {ALLOWED_PASSES}",
            )
        )

    status_raw = loc.get("status")
    citations = loc.get("citations")
    if not isinstance(citations, list):
        issues.append(
            VerificationIssue(
                code="INVALID_CITATIONS_TYPE",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message="citations must be a list",
            )
        )
        citations = []

    docs_for_nct = docs_by_nct.get(nct_id, {})
    has_posted_docs: bool = bool(docs_for_nct) or manifest_has_documents
    status_errors = validate_status_citations(
        status_raw,  # type: ignore[arg-type]
        len(citations),
        has_posted_documents=has_posted_docs,
    )
    for err_msg in status_errors:
        code = _status_citation_error_code(err_msg)
        if code == "NO_DOCUMENT_WITH_POSTED_DOCUMENT" and docs_for_nct:
            err_msg = (
                f"status='NO_DOCUMENT' is invalid when {len(docs_for_nct)} posted "
                f"document(s) exist for {nct_id} ({sorted(docs_for_nct.keys())})."
            )
        elif code == "NO_DOCUMENT_WITH_POSTED_DOCUMENT":
            err_msg = (
                f"status='NO_DOCUMENT' is invalid when manifest/index flags indicate "
                f"posted document(s) exist for {nct_id}."
            )
        issues.append(
            VerificationIssue(
                code=code,
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=err_msg,
            )
        )

    pages_searched = loc.get("pages_searched")
    pages_not_searched = loc.get("pages_not_searched")
    has_unsearched = _is_non_empty_pages_not_searched(pages_not_searched)

    if pages_searched == "all" and has_unsearched:
        issues.append(
            VerificationIssue(
                code="SEARCH_COVERAGE_CONTRADICTION",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message="pages_searched == 'all' contradicts non-empty pages_not_searched.",
            )
        )
    elif has_unsearched:
        issues.extend(
            _validate_page_partition(
                nct_id, loc_key, pages_searched, pages_not_searched, docs_for_nct
            )
        )
        if status_raw == "NOT_FOUND" and require_exhaustive_for_not_found:
            issues.append(
                VerificationIssue(
                    code="NOT_FOUND_WITH_UNSEARCHED_PAGES",
                    severity="error",
                    nct_id=nct_id,
                    location_key=loc_key,
                    filename=None,
                    physical_page=None,
                    message=(
                        "status='NOT_FOUND' is forbidden while pages_not_searched is non-empty "
                        "in exhaustive publication mode."
                    ),
                )
            )

    if status_raw == "UNREADABLE":
        issues.extend(_verify_unreadable_validity(nct_id, loc_key, loc, docs_for_nct))

    effective_text_source = loc.get("text_source")
    if type(effective_text_source) is not str or not effective_text_source:
        effective_text_source = text_source

    for cit in citations:
        counts["citations_checked"] += 1
        tier, cit_issues = verify_citation(
            nct_id,
            loc_key,
            cit,
            docs_by_filename=docs_for_nct,
            pages_not_searched=pages_not_searched,
            page_text_provider=page_text_provider,
            text_source=effective_text_source,
        )
        issues.extend(cit_issues)
        if any(i.code == "CITATION_ELLIPSIS_FORBIDDEN" for i in cit_issues):
            counts["ellipsis_flags"] += 1
        if tier == "exact_ws":
            counts["exact_ws_matches"] += 1
        elif tier == "unicode_nfkc_hyphen_matches" or tier == "unicode_nfkc_hyphen":
            counts["unicode_nfkc_hyphen_matches"] += 1
        elif tier == "mismatch":
            counts["substring_mismatches"] += 1

    # Neutral-language scan on worker commentary (never on verbatim citations).
    note = loc.get("note")
    if type(note) is str:
        for flag in scan_neutral_language(note):
            counts["neutral_language_flags"] += 1
            issues.append(
                VerificationIssue(
                    code=f"NEUTRAL_LANGUAGE_{flag.rule_id}",
                    severity="error",
                    nct_id=nct_id,
                    location_key=loc_key,
                    filename=None,
                    physical_page=None,
                    message=(
                        f"Neutral-language violation ({flag.rule_id}/{flag.category}): "
                        f"{flag.matched_text!r} — {flag.rationale}"
                    ),
                )
            )
        issues.extend(check_note_page_counts(nct_id, loc_key, note, docs_for_nct))

    return counts, issues


def verify_trial_completeness(
    nct_id: str,
    locations: Sequence[Mapping[str, Any]],
    *,
    expected_primary_outcomes: int,
    pass_name: str = "primary",
    allow_sparse: bool = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
) -> list[VerificationIssue]:
    """Assert exact bijection over ``range(expected_primary_outcomes) x ELEMENTS``.

    When ``allow_sparse=True`` (used for targeted override passes in
    ``SPARSE_PASSES``), ``TRIAL_GRID_MISSING_CELL`` is suppressed while
    ``INVALID_EXPECTED_OUTCOMES_COUNT``, ``TRIAL_GRID_DUPLICATE_CELL``, and
    ``TRIAL_GRID_UNEXPECTED_CELL`` (``0 <= idx < expected_primary_outcomes``)
    remain strictly enforced.
    """
    checked_nct = validate_strict_nct_id(nct_id, context="completeness.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    for loc in locations:
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"Location entry in verify_trial_completeness must be a mapping, got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context="location.nct_id")
    if checked_nct in checked_audit_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: trial {checked_nct!r} is in audit_nct_ids."
        )
    if checked_scope_ids is not None and checked_nct not in checked_scope_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout refusal: trial {checked_nct!r} is absent from in_scope_nct_ids."
        )
    for loc in locations:
        loc_nct = loc["nct_id"]
        if loc_nct in checked_audit_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: location row {loc_nct!r} is in audit_nct_ids."
            )
        if checked_scope_ids is not None and loc_nct not in checked_scope_ids:
            raise AuditHoldoutViolationError(
                f"Audit holdout refusal: location row {loc_nct!r} is absent from in_scope_nct_ids."
            )
        if "in_audit" in loc:
            l_flag = loc["in_audit"]
            if type(l_flag) is not bool or l_flag is True:
                raise AuditHoldoutViolationError(
                    f"Location row for {loc_nct!r} carries forbidden in_audit={l_flag!r}."
                )

    issues: list[VerificationIssue] = []
    if not _is_strict_int(expected_primary_outcomes) or expected_primary_outcomes < 1:
        issues.append(
            VerificationIssue(
                code="INVALID_EXPECTED_OUTCOMES_COUNT",
                severity="error",
                nct_id=checked_nct,
                location_key=None,
                filename=None,
                physical_page=None,
                message=(
                    f"expected_primary_outcomes must be int >= 1, got {expected_primary_outcomes!r}"
                ),
            )
        )
        return issues

    seen: dict[tuple[int, str], int] = {}
    for loc in locations:
        if loc.get("nct_id") != checked_nct or loc.get("pass") != pass_name:
            continue
        idx = loc.get("primary_outcome_index")
        elem = loc.get("element")
        if _is_strict_int(idx) and type(elem) is str:
            key = (idx, elem)
            seen[key] = seen.get(key, 0) + 1

    expected_grid = {
        (idx, elem)
        for idx in range(expected_primary_outcomes)
        for elem in ELEMENTS
    }
    duplicates = sorted(k for k, count in seen.items() if count > 1)
    missing = sorted(expected_grid - set(seen.keys()))
    unexpected = sorted(set(seen.keys()) - expected_grid)

    for idx, elem in duplicates:
        issues.append(
            VerificationIssue(
                code="TRIAL_GRID_DUPLICATE_CELL",
                severity="error",
                nct_id=checked_nct,
                location_key=(idx, elem, pass_name),
                filename=None,
                physical_page=None,
                message=(
                    f"Duplicate location row for ({checked_nct}, outcome={idx}, "
                    f"element={elem!r}, pass={pass_name!r})."
                ),
            )
        )
    if not allow_sparse:
        for idx, elem in missing:
            issues.append(
                VerificationIssue(
                    code="TRIAL_GRID_MISSING_CELL",
                    severity="error",
                    nct_id=checked_nct,
                    location_key=(idx, elem, pass_name),
                    filename=None,
                    physical_page=None,
                    message=(
                        f"Missing location row for ({checked_nct}, outcome={idx}, "
                        f"element={elem!r}, pass={pass_name!r})."
                    ),
                )
            )
    for idx, elem in unexpected:
        issues.append(
            VerificationIssue(
                code="TRIAL_GRID_UNEXPECTED_CELL",
                severity="error",
                nct_id=checked_nct,
                location_key=(idx, elem, pass_name),
                filename=None,
                physical_page=None,
                message=(
                    f"Unexpected location row outside 0..{expected_primary_outcomes - 1} x ELEMENTS: "
                    f"({checked_nct}, outcome={idx}, element={elem!r}, pass={pass_name!r})."
                ),
            )
        )

    return issues


def verify_census_corpus(
    documents: Sequence[Mapping[str, Any]],
    locations: Sequence[Mapping[str, Any]],
    *,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
    page_text_provider: PageTextProvider | None = None,
    pdf_root: Path | None = None,
    expected_outcomes_by_nct: Mapping[str, int] | None = None,
    expected_has_documents_by_nct: Mapping[str, bool] | None = None,
    strict_document_keys: bool = True,
    require_exhaustive_for_not_found: bool = True,
    text_source: str = "pdfkit",
) -> CorpusVerificationReport:
    """Run full mechanical verification across a documents + locations corpus.

    Validates all NCT IDs via ``validate_strict_nct_id`` FIRST, requires
    non-empty ``audit_nct_ids`` unless ``allow_empty_audit_set=True``, and
    raises ``AuditHoldoutViolationError`` immediately on any audit, missing-flag,
    or unlisted trial.
    """
    if type(allow_empty_audit_set) is not bool:
        raise AuditHoldoutViolationError(
            f"allow_empty_audit_set must be bool, got {type(allow_empty_audit_set).__name__}."
        )

    # Step 1 (F2): Validate strict ASCII ^NCT[0-9]{8}$ across ALL inputs FIRST.
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    if expected_has_documents_by_nct is not None:
        for k in expected_has_documents_by_nct:
            validate_strict_nct_id(k, context="expected_has_documents_by_nct key")
    if expected_outcomes_by_nct is not None:
        for k in expected_outcomes_by_nct:
            validate_strict_nct_id(k, context="expected_outcomes_by_nct key")
    for idx, doc in enumerate(documents):
        if not isinstance(doc, Mapping):
            raise CitationVerificationError(
                f"documents[{idx}] must be a mapping, got {type(doc).__name__}."
            )
        validate_strict_nct_id(doc.get("nct_id"), context=f"documents[{idx}].nct_id")
    for idx, loc in enumerate(locations):
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"locations[{idx}] must be a mapping, got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{idx}].nct_id")

    # Step 2 (F1): Enforce non-empty audit_nct_ids and firewall invariants.
    if checked_scope_ids is not None and (checked_audit_ids & checked_scope_ids):
        overlap = sorted(checked_audit_ids & checked_scope_ids)
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: in_scope_nct_ids overlaps audit_nct_ids: {overlap}."
        )
    if not allow_empty_audit_set and len(checked_audit_ids) == 0:
        raise AuditHoldoutViolationError(
            "audit_nct_ids may not be empty unless allow_empty_audit_set=True after verifying "
            "100% of index rows carry explicit in_audit=False."
        )

    if isinstance(page_text_provider, (DirectoryPageTextProvider, DictPageTextProvider)):
        page_text_provider.bind_firewall(
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
        )

    if expected_has_documents_by_nct is not None:
        for nct_id, has_docs_val in sorted(expected_has_documents_by_nct.items()):
            enforce_audit_holdout(
                nct_id,
                in_audit=False,
                audit_nct_ids=checked_audit_ids,
                in_scope_nct_ids=checked_scope_ids,
                allow_empty_audit_set=allow_empty_audit_set,
            )
            if type(has_docs_val) is not bool:
                raise CitationVerificationError(
                    f"expected_has_documents_by_nct[{nct_id!r}] must be bool, got {has_docs_val!r}."
                )

    if expected_outcomes_by_nct is not None:
        for nct_id in sorted(expected_outcomes_by_nct.keys()):
            enforce_audit_holdout(
                nct_id,
                in_audit=False,
                audit_nct_ids=checked_audit_ids,
                in_scope_nct_ids=checked_scope_ids,
                allow_empty_audit_set=allow_empty_audit_set,
            )

    all_issues: list[VerificationIssue] = []
    docs_by_nct: dict[str, dict[str, Mapping[str, Any]]] = {}
    corpus_ncts: set[str] = set()

    for doc in documents:
        doc_issues = verify_document_record(
            doc,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
            strict_keys=strict_document_keys,
            pdf_root=pdf_root,
        )
        all_issues.extend(doc_issues)
        nct_id = doc["nct_id"]
        corpus_ncts.add(nct_id)
        filename = doc.get("filename")
        if type(filename) is str:
            per_nct = docs_by_nct.setdefault(nct_id, {})
            if filename in per_nct:
                all_issues.append(
                    VerificationIssue(
                        code="DUPLICATE_DOCUMENT_RECORD",
                        severity="error",
                        nct_id=nct_id,
                        location_key=None,
                        filename=filename,
                        physical_page=None,
                        message=f"Duplicate document record for ({nct_id}, {filename!r}).",
                    )
                )
            else:
                per_nct[filename] = doc

    citations_checked = 0
    exact_ws_matches = 0
    unicode_nfkc_hyphen_matches = 0
    substring_mismatches = 0
    ellipsis_flags = 0
    neutral_language_flags = 0
    seen_location_keys: set[tuple[str, int, str, str]] = set()
    passes_by_nct: dict[str, set[str]] = {}

    for loc in locations:
        loc_nct = loc["nct_id"]
        corpus_ncts.add(loc_nct)
        manifest_has_docs = (
            bool(expected_has_documents_by_nct.get(loc_nct, False))
            if expected_has_documents_by_nct is not None
            else False
        )
        counts, loc_issues = verify_location_record(
            loc,
            docs_by_nct=docs_by_nct,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
            page_text_provider=page_text_provider,
            require_exhaustive_for_not_found=require_exhaustive_for_not_found,
            text_source=text_source,
            manifest_has_documents=manifest_has_docs,
        )
        all_issues.extend(loc_issues)
        citations_checked += counts["citations_checked"]
        exact_ws_matches += counts["exact_ws_matches"]
        unicode_nfkc_hyphen_matches += counts["unicode_nfkc_hyphen_matches"]
        substring_mismatches += counts["substring_mismatches"]
        ellipsis_flags += counts["ellipsis_flags"]
        neutral_language_flags += counts["neutral_language_flags"]

        idx = loc.get("primary_outcome_index")
        elem = loc.get("element")
        pass_name = loc.get("pass")
        if _is_strict_int(idx) and type(elem) is str and type(pass_name) is str:
            full_key = (loc_nct, idx, elem, pass_name)
            if full_key in seen_location_keys:
                all_issues.append(
                    VerificationIssue(
                        code="DUPLICATE_LOCATION_KEY",
                        severity="error",
                        nct_id=loc_nct,
                        location_key=(idx, elem, pass_name),
                        filename=None,
                        physical_page=None,
                        message=f"Duplicate location row for {full_key}.",
                    )
                )
            else:
                seen_location_keys.add(full_key)
            passes_by_nct.setdefault(loc_nct, set()).add(pass_name)

    if expected_outcomes_by_nct is not None:
        missing_from_expected = sorted(corpus_ncts - set(expected_outcomes_by_nct.keys()))
        if missing_from_expected:
            raise AuditHoldoutViolationError(
                f"Corpus trial(s) absent from expected_outcomes_by_nct / --index: "
                f"{missing_from_expected}."
            )
        for nct_id in sorted(expected_outcomes_by_nct.keys()):
            expected_k = expected_outcomes_by_nct[nct_id]
            if not _is_strict_int(expected_k) or expected_k < 1:
                raise CitationVerificationError(
                    f"Invalid expected primary outcomes count {expected_k!r} for {nct_id!r}; "
                    f"must be strict int >= 1."
                )
            for pass_name in sorted(passes_by_nct.get(nct_id, set()) | {"primary"}):
                all_issues.extend(
                    verify_trial_completeness(
                        nct_id,
                        locations,
                        expected_primary_outcomes=expected_k,
                        pass_name=pass_name,
                        allow_sparse=pass_name in SPARSE_PASSES,
                    )
                )

    has_error = any(issue.severity == "error" for issue in all_issues)
    return CorpusVerificationReport(
        ok=not has_error,
        verifier_rules_version=VERIFIER_RULES_VERSION,
        neutral_vocab_sha256=NEUTRAL_LANGUAGE_VOCAB_SHA256,
        documents_checked=len(documents),
        locations_checked=len(locations),
        citations_checked=citations_checked,
        exact_ws_matches=exact_ws_matches,
        unicode_nfkc_hyphen_matches=unicode_nfkc_hyphen_matches,
        substring_mismatches=substring_mismatches,
        ellipsis_flags=ellipsis_flags,
        neutral_language_flags=neutral_language_flags,
        issues=tuple(all_issues),
    )


__all__ = [
    "ALLOWED_PASSES",
    "AuditHoldoutViolationError",
    "CITATION_FORBIDDEN_STATUSES",
    "CITATION_REQUIRED_KEYS",
    "CITATION_REQUIRED_STATUSES",
    "CitationVerificationError",
    "CorpusVerificationReport",
    "DOC_TYPES",
    "DOCUMENT_OPTIONAL_KEYS",
    "DOCUMENT_REQUIRED_KEYS",
    "DictPageTextProvider",
    "DirectoryPageTextProvider",
    "ELEMENTS",
    "InvalidNctIdError",
    "IssueSeverity",
    "LOCATION_REQUIRED_KEYS",
    "MAX_QUOTATION_CHARS",
    "MatchTier",
    "NEUTRAL_LANGUAGE_RULES",
    "NEUTRAL_LANGUAGE_VOCAB_SHA256",
    "NeutralLanguageCategory",
    "NeutralLanguageFlag",
    "NeutralLanguageRule",
    "PageTextProvider",
    "SPARSE_PASSES",
    "STATUSES",
    "STRICT_NCT_ID_RE",
    "UNREADABLE_SUBTYPES",
    "VERIFIER_RULES_VERSION",
    "VerificationIssue",
    "__version__",
    "check_neutral_language",
    "check_note_page_counts",
    "enforce_audit_holdout",
    "has_ellipsis",
    "match_quotation_on_page",
    "normalize_for_page_match",
    "scan_neutral_language",
    "scan_neutral_language_record",
    "validate_citation_shape",
    "validate_status_citations",
    "validate_strict_nct_id",
    "verify_census_corpus",
    "verify_citation",
    "verify_document_record",
    "verify_location_record",
    "verify_trial_completeness",
]
