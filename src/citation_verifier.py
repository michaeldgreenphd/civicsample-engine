"""Mechanical single-page citation verifier and neutral-language guard.

Verifies document records, decision-rule location records, and verbatim
single-page PDF quotations for clinical trial rule censuses.

Runtime dependencies:
- Python 3.11+ standard library (``dataclasses``, ``gzip``, ``hashlib``,
  ``json``, ``pathlib``, ``re``, ``typing``, ``unicodedata``, ``urllib.parse``).
- Performs zero network calls and invokes zero external PDF binaries; consumes
  pre-extracted JSON or JSONL single-page text maps via ``PageTextProvider``.

Key invariants:
- Strict ASCII NCT ID gate (``STRICT_NCT_ID_RE = ^NCT[0-9]{8}$``): every entry
  point validates NCT IDs with ``validate_strict_nct_id`` FIRST (before any
  ``in_audit`` flag, audit-set, or allowlist check) and raises
  ``InvalidNctIdError`` immediately on any non-matching value without
  case-folding or whitespace stripping.
- Hard ``in_audit`` holdout firewall: every entry point requires explicit
  ``bool`` ``in_audit`` (or verified ``in_scope_nct_ids`` + ``audit_nct_ids``)
  and non-empty ``audit_nct_ids`` by default, raising
  ``AuditHoldoutViolationError`` unconditionally before reading or inspecting
  document text whenever an audit or unlisted trial is referenced. The
  caller's ``in_audit`` covers the record's own trial only: any other ID
  reached from a record (``docs_by_nct`` keys, nested ``nct_ids``, completeness
  rows, ``expected_*`` keys, URL tokens) is admitted only by membership of
  ``in_scope_nct_ids`` (``_enforce_derived_nct_holdout``). A record's own
  ``in_audit`` can refuse it but never admits it. ``allow_empty_audit_set=True``
  with an empty audit set also requires ``in_scope_nct_ids``, and
  ``verify_census_corpus`` always requires it.
- Document presence is tri-state (True / False / None for "the index does not
  say"); absence is never read as "no documents", and a ``NO_DOCUMENT`` row
  whose presence is unknown is reported as ``NO_DOCUMENT_PRESENCE_UNKNOWN``.
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
from urllib.parse import unquote

__version__: Final[str] = "1.2.0"
VERIFIER_RULES_VERSION: Final[str] = "1.2.0"

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
_SAFE_STEM_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$", re.ASCII)
_EMBEDDED_NCT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"nct[0-9]{7,10}",
    re.IGNORECASE | re.ASCII,
)
_NCT_FIELD_KEYS: Final[frozenset[str]] = frozenset({"nct_id", "nctId", "trial_nct_id"})
_NCT_LIST_KEYS: Final[frozenset[str]] = frozenset({"nct_ids", "trial_nct_ids"})
_URL_FIELD_KEYS: Final[frozenset[str]] = frozenset(
    {"url", "source_url", "pdf_url", "document_url", "uri"}
)
_ELLIPSIS_RE: Final[re.Pattern[str]] = re.compile(
    r"\.{3,}|(?:\.\s+){2,}\.|[\u2026\u22ee\u22ef\u22f0\u22f1\ufe19\u1801\u0eaf]"
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
_INVISIBLE_CHARS_RE: Final[re.Pattern[str]] = re.compile(
    r"[\u00ad\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)
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


def _strip_invisible(text: str) -> str:
    """Remove invisible/format Unicode characters while preserving standard whitespace."""
    cleaned = _INVISIBLE_CHARS_RE.sub("", text)
    return "".join(
        ch
        for ch in cleaned
        if unicodedata.category(ch) not in {"Cf", "Cc"} or ch in "\t\n\r"
    )


def _is_non_empty_str(value: Any) -> bool:
    """Return True only when ``value`` is an exact ``str`` with visible non-whitespace content."""
    if type(value) is not str:
        return False
    folded = _strip_invisible(unicodedata.normalize("NFKC", value)).strip()
    return len(folded) > 0


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
    spaces, lowercase prefixes, zero-width characters, string subclasses, and
    non-ASCII digits are unconditionally rejected with ``InvalidNctIdError``.
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


def _check_embedded_url_ncts(
    url_text: str,
    *,
    context: str,
    expected_nct_id: str | None,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
) -> None:
    """Reject audit, unlisted, malformed, or mismatched NCT tokens embedded inside URLs."""
    variants = [url_text]
    if "%" in url_text:
        variants.append(unquote(url_text))
    variants.append(_strip_invisible(unicodedata.normalize("NFKC", variants[-1])))

    for variant in variants:
        for match in _EMBEDDED_NCT_TOKEN_RE.finditer(variant):
            raw_tok = match.group(0)
            upper_tok = raw_tok.upper()
            if STRICT_NCT_ID_RE.fullmatch(upper_tok) is not None and upper_tok in audit_nct_ids:
                raise AuditHoldoutViolationError(
                    f"Audit holdout violation in {context}: URL references holdout trial {upper_tok!r}."
                )
            if STRICT_NCT_ID_RE.fullmatch(raw_tok) is None:
                raise InvalidNctIdError(
                    f"Malformed NCT token {raw_tok!r} in {context}: expected strict ^NCT[0-9]{{8}}$."
                )
            if in_scope_nct_ids is not None and raw_tok not in in_scope_nct_ids:
                raise AuditHoldoutViolationError(
                    f"Audit holdout refusal in {context}: URL references unlisted trial {raw_tok!r}."
                )
            # Same rule as ``_enforce_derived_nct_holdout``: with no record trial to compare
            # against and no in-scope set, the audit set alone cannot show that a URL's trial
            # is outside the holdout (it lists the holdout, not the trials outside it).
            if in_scope_nct_ids is None and expected_nct_id is None:
                raise AuditHoldoutViolationError(
                    f"Audit holdout refusal in {context}: URL references trial {raw_tok!r}, and "
                    f"neither a record nct_id nor in_scope_nct_ids establishes it is outside the "
                    f"holdout."
                )
            if expected_nct_id is not None and raw_tok != expected_nct_id:
                raise AuditHoldoutViolationError(
                    f"Audit holdout refusal in {context}: URL NCT {raw_tok!r} disagrees with "
                    f"record nct_id={expected_nct_id!r}."
                )


def _prescan_nested_nct_fields(obj: Any, *, context: str) -> None:
    """Phase-A recursive strict NCT validation across nested mappings/lists before audit checks."""
    if isinstance(obj, Mapping):
        for raw_k, val in obj.items():
            key = str(raw_k)
            child_ctx = f"{context}.{key}"
            if key in _NCT_FIELD_KEYS:
                validate_strict_nct_id(val, context=child_ctx)
            elif key in _NCT_LIST_KEYS:
                if not isinstance(val, list):
                    raise CitationVerificationError(
                        f"{child_ctx} must be a list, got {type(val).__name__}."
                    )
                for idx, item in enumerate(val):
                    validate_strict_nct_id(item, context=f"{child_ctx}[{idx}]")
            elif key == "pages" and isinstance(val, list):
                for idx, item in enumerate(val):
                    if isinstance(item, Mapping):
                        _prescan_nested_nct_fields(item, context=f"{child_ctx}[{idx}]")
            elif isinstance(val, (Mapping, list, tuple)):
                _prescan_nested_nct_fields(val, context=child_ctx)
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            if isinstance(item, (Mapping, list, tuple)):
                _prescan_nested_nct_fields(item, context=f"{context}[{idx}]")


def _validate_nested_firewall_metadata(
    obj: Any,
    *,
    context: str,
    expected_nct_id: str | None,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    allow_empty_audit_set: bool,
) -> list[str]:
    """Recursively validate ``nct_id``/``nctId``/``nct_ids``/``in_audit``/URLs in metadata."""
    discovered_ncts: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for raw_k, val in node.items():
                key = str(raw_k)
                child_ctx = f"{path}.{key}"
                if key in _NCT_FIELD_KEYS:
                    checked = validate_strict_nct_id(val, context=child_ctx)
                    _enforce_derived_nct_holdout(
                        checked,
                        verified_nct=expected_nct_id,
                        context=child_ctx,
                        audit_nct_ids=audit_nct_ids,
                        in_scope_nct_ids=in_scope_nct_ids,
                        allow_empty_audit_set=allow_empty_audit_set,
                    )
                    if expected_nct_id is not None and checked != expected_nct_id:
                        raise AuditHoldoutViolationError(
                            f"{child_ctx}={checked!r} disagrees with expected nct_id={expected_nct_id!r}."
                        )
                    discovered_ncts.append(checked)
                elif key in _NCT_LIST_KEYS:
                    if not isinstance(val, list):
                        raise CitationVerificationError(
                            f"{child_ctx} must be a list, got {type(val).__name__}."
                        )
                    for idx, item in enumerate(val):
                        checked = validate_strict_nct_id(item, context=f"{child_ctx}[{idx}]")
                        _enforce_derived_nct_holdout(
                            checked,
                            verified_nct=expected_nct_id,
                            context=f"{child_ctx}[{idx}]",
                            audit_nct_ids=audit_nct_ids,
                            in_scope_nct_ids=in_scope_nct_ids,
                            allow_empty_audit_set=allow_empty_audit_set,
                        )
                        discovered_ncts.append(checked)
                elif key == "in_audit":
                    if type(val) is not bool:
                        raise AuditHoldoutViolationError(
                            f"{child_ctx} carries non-bool in_audit={val!r}."
                        )
                    if val is True:
                        raise AuditHoldoutViolationError(
                            f"Audit holdout violation: {child_ctx} carries in_audit=True."
                        )
                elif key in _URL_FIELD_KEYS and isinstance(val, str):
                    _check_embedded_url_ncts(
                        val,
                        context=child_ctx,
                        expected_nct_id=expected_nct_id,
                        audit_nct_ids=audit_nct_ids,
                        in_scope_nct_ids=in_scope_nct_ids,
                    )
                elif key == "pages" and isinstance(val, list):
                    for idx, item in enumerate(val):
                        if isinstance(item, Mapping):
                            _walk(item, f"{child_ctx}[{idx}]")
                elif isinstance(val, (Mapping, list, tuple)):
                    _walk(val, child_ctx)
        elif isinstance(node, (list, tuple)):
            for idx, item in enumerate(node):
                if isinstance(item, (Mapping, list, tuple)):
                    _walk(item, f"{path}[{idx}]")

    _prescan_nested_nct_fields(obj, context=context)
    _walk(obj, context)
    return discovered_ncts


def enforce_audit_holdout(
    nct_id: str,
    *,
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
) -> None:
    """Hard holdout firewall: validate strict NCT IDs first, then enforce ``in_audit == False``.

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

    _require_scope_for_empty_audit_set(
        checked_audit_ids,
        checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
        context="enforce_audit_holdout",
    )


def _require_scope_for_empty_audit_set(
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    *,
    allow_empty_audit_set: bool,
    context: str,
) -> None:
    """Refuse an empty audit set unless the caller also supplies the in-scope set it verified.

    Method: an empty ``audit_nct_ids`` is legitimate only for an index whose
    rows all carry explicit ``in_audit=False`` (the CLI derives
    ``allow_empty_audit_set`` that way in ``_partition_index_firewall``). A
    library caller asserts the flag rather than deriving it, so the assertion
    alone would leave nothing between an unknown trial and the page text. The
    caller that verified those rows holds them, so it must pass them as
    ``in_scope_nct_ids``: with an empty audit set, membership of that set is
    the only remaining guard, and without it the call is refused.
    """
    if len(audit_nct_ids) > 0:
        return
    if not allow_empty_audit_set:
        raise AuditHoldoutViolationError(
            f"{context}: audit_nct_ids may not be empty unless allow_empty_audit_set=True after "
            f"verifying 100% of index rows carry explicit in_audit=False."
        )
    if in_scope_nct_ids is None:
        raise AuditHoldoutViolationError(
            f"{context}: allow_empty_audit_set=True with an empty audit_nct_ids also requires "
            f"in_scope_nct_ids (the verified index rows); without it no trial can be shown to be "
            f"outside the holdout."
        )


def _enforce_derived_nct_holdout(
    derived_nct: str,
    *,
    verified_nct: str | None,
    context: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    allow_empty_audit_set: bool,
) -> None:
    """Firewall an NCT ID reached from a record rather than supplied with its own flag.

    Method: a caller's explicit ``in_audit`` speaks for one trial only, the
    record's own ``verified_nct``, which the caller has already passed through
    ``enforce_audit_holdout``. Every other ID reached from that record (a
    ``docs_by_nct`` key, a nested ``nct_id``/``nct_ids`` entry, a completeness
    row, an ``expected_*`` key) carries no flag of its own, and passing
    ``in_audit=False`` for it would be a default. So:

    - with ``in_scope_nct_ids`` (index rows whose ``in_audit`` is explicitly
      False), the ID must be a member and outside ``audit_nct_ids``;
    - without it, only ``verified_nct`` itself is accepted (re-checked against
      ``audit_nct_ids``); any other ID is refused, because the audit set lists
      the holdout, not the trials known to be outside it.
    """
    checked = validate_strict_nct_id(derived_nct, context=context)
    if in_scope_nct_ids is not None:
        enforce_audit_holdout(
            checked,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        return
    if checked in audit_nct_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout violation in {context}: trial {checked!r} is in audit_nct_ids."
        )
    if verified_nct is None or checked != verified_nct:
        raise AuditHoldoutViolationError(
            f"Audit holdout refusal in {context}: trial {checked!r} is not the record's own "
            f"trial ({verified_nct!r}) and no in_scope_nct_ids was supplied, so nothing "
            f"establishes that it is outside the holdout."
        )


def _enforce_record_firewall(
    record: Mapping[str, Any],
    *,
    record_label: str,
    in_audit: Any,
    audit_nct_ids: AbstractSet[str],
    in_scope_nct_ids: AbstractSet[str] | None,
    allow_empty_audit_set: bool,
) -> tuple[str, frozenset[str], frozenset[str] | None]:
    """Validate strict NCT IDs FIRST across record & sets, then enforce ``in_audit`` firewall."""
    # Phase A (F2): Validate all NCT IDs FIRST before touching any in_audit flag.
    checked_nct = validate_strict_nct_id(record.get("nct_id"), context=f"{record_label}.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    _prescan_nested_nct_fields(record, context=record_label)

    # Phase B (F1): Inspect in_audit flags, membership, and embedded URLs.
    # A record's own ``in_audit`` is reported by the data under check, so it can only
    # refuse (True or non-bool). It never stands in for the caller's flag or in-scope set,
    # which come from the index: a worker or a stale file writing ``in_audit: false`` must
    # not admit a record that the index would hold out.
    if "in_audit" in record:
        row_flag = record["in_audit"]
        if type(row_flag) is not bool:
            raise AuditHoldoutViolationError(
                f"{record_label} for {checked_nct!r} carries non-bool in_audit={row_flag!r}."
            )
        if row_flag is True:
            raise AuditHoldoutViolationError(
                f"Audit holdout violation: {record_label} for {checked_nct!r} has in_audit=True."
            )

    enforce_audit_holdout(
        checked_nct,
        in_audit=in_audit,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_nested_firewall_metadata(
        record,
        context=record_label,
        expected_nct_id=checked_nct,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    return checked_nct, checked_audit_ids, checked_scope_ids


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
    - ``quotation`` is a non-empty visible single-space-normalized ``str`` of
      length ``1..300`` without ellipsis markers (``has_ellipsis``).
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
        if (
            type(quotation) is not str
            or len(quotation) == 0
            or not _is_non_empty_str(quotation)
            or not normalize_for_page_match(quotation)
        ):
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
    """Normalize PDF page text or quotation text for Tier-2 single-page matching."""
    normalized = text
    if repair_line_break_hyphens:
        normalized = _SOFT_HYPHEN_WRAP_RE.sub(r"\1\2", normalized)
        normalized = _LINE_BREAK_HYPHEN_RE.sub(r"\1\2", normalized)
    normalized = unicodedata.normalize("NFKC", normalized)
    normalized = _strip_invisible(normalized)
    normalized = normalized.translate(_DASH_TRANS)
    return " ".join(normalized.split())


def has_ellipsis(text: str) -> bool:
    """Return True if text contains literal, spaced, compatibility, or invisible-split ellipsis markers."""
    if _ELLIPSIS_RE.search(text):
        return True
    folded = _strip_invisible(unicodedata.normalize("NFKC", text))
    return bool(_ELLIPSIS_RE.search(folded))


def match_quotation_on_page(quotation: str, page_text: str) -> MatchTier:
    """Check whether ``quotation`` occurs verbatim on a single ``page_text``."""
    q_ws = " ".join(quotation.split())
    if not q_ws:
        return "mismatch"
    p_ws = " ".join(page_text.split())
    if q_ws in p_ws:
        return "exact_ws"

    q_norm = normalize_for_page_match(quotation, repair_line_break_hyphens=False)
    if not q_norm:
        return "mismatch"
    p_norm_repaired = normalize_for_page_match(page_text, repair_line_break_hyphens=True)
    if q_norm in p_norm_repaired:
        return "unicode_nfkc_hyphen"
    p_norm_plain = normalize_for_page_match(page_text, repair_line_break_hyphens=False)
    if q_norm in p_norm_plain:
        return "unicode_nfkc_hyphen"
    return "mismatch"


def scan_neutral_language(text: str) -> list[NeutralLanguageFlag]:
    """Scan commentary text for evaluative, spin, or compliance-inference claims."""
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
    """In-memory ``PageTextProvider`` for deterministic offline tests and fixtures."""

    def __init__(
        self,
        pages_by_key: Mapping[Any, str] | None = None,
        *,
        pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
        ocr_pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        in_scope_nct_ids: AbstractSet[str] | None = None,
        allow_empty_audit_set: bool = False,
    ) -> None:
        self._audit_nct_ids: frozenset[str] = _validate_nct_set(
            audit_nct_ids, context="DictPageTextProvider.audit_nct_ids"
        )
        self._in_scope_nct_ids: frozenset[str] | None = (
            _validate_nct_set(in_scope_nct_ids, context="DictPageTextProvider.in_scope_nct_ids")
            if in_scope_nct_ids is not None
            else None
        )
        if type(allow_empty_audit_set) is not bool:
            raise AuditHoldoutViolationError(
                f"allow_empty_audit_set must be bool, got {type(allow_empty_audit_set).__name__}."
            )
        self._allow_empty_audit_set: bool = allow_empty_audit_set
        if self._in_scope_nct_ids is not None and (self._audit_nct_ids & self._in_scope_nct_ids):
            raise AuditHoldoutViolationError(
                "Audit holdout violation: DictPageTextProvider in_scope_nct_ids overlaps audit_nct_ids."
            )
        if not self._allow_empty_audit_set and len(self._audit_nct_ids) == 0:
            raise AuditHoldoutViolationError(
                "DictPageTextProvider requires non-empty audit_nct_ids unless allow_empty_audit_set=True."
            )
        self._pages_by_key: dict[Any, str] = dict(pages_by_key or {})
        self._pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (pages_by_sha256 or {}).items()
        }
        self._ocr_pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (ocr_pages_by_sha256 or {}).items()
        }
        self._validate_stored_keys()

    def _validate_sha256_map_key(self, raw_sha: Any, *, context: str) -> None:
        if type(raw_sha) is str:
            cleaned = _strip_invisible(unicodedata.normalize("NFKC", raw_sha)).strip()
            if STRICT_NCT_ID_RE.fullmatch(cleaned.upper()) is not None:
                if cleaned.upper() in self._audit_nct_ids:
                    raise AuditHoldoutViolationError(
                        f"Audit holdout violation in {context}: {raw_sha!r}."
                    )
        if (
            type(raw_sha) is not str
            or len(raw_sha) != 64
            or _SHA256_RE.fullmatch(raw_sha) is None
        ):
            raise CitationVerificationError(
                f"{context} must be a 64-character lowercase hex sha256, got {raw_sha!r}."
            )

    def _validate_stored_keys(self) -> None:
        for sha_k in self._pages_by_sha256:
            self._validate_sha256_map_key(
                sha_k, context="DictPageTextProvider.pages_by_sha256 key"
            )
        for sha_k in self._ocr_pages_by_sha256:
            self._validate_sha256_map_key(
                sha_k, context="DictPageTextProvider.ocr_pages_by_sha256 key"
            )

        for key in self._pages_by_key:
            if not isinstance(key, tuple) or len(key) not in (2, 3, 4):
                raise CitationVerificationError(
                    f"Unsupported DictPageTextProvider key shape: {key!r}."
                )
            if len(key) == 2:
                self._validate_sha256_map_key(
                    key[0], context="DictPageTextProvider.pages_by_key sha256"
                )
                if not _is_strict_int(key[1]) or key[1] < 1:
                    raise CitationVerificationError(
                        f"Invalid physical_page in DictPageTextProvider key: {key!r}."
                    )
            elif len(key) == 3 and _is_strict_int(key[1]):
                self._validate_sha256_map_key(
                    key[0], context="DictPageTextProvider.pages_by_key sha256"
                )
                if key[1] < 1 or not _is_non_empty_str(key[2]):
                    raise CitationVerificationError(
                        f"Invalid (sha256, page, text_source) key in DictPageTextProvider: {key!r}."
                    )
            elif len(key) in (3, 4):
                nct_key = validate_strict_nct_id(key[0], context="DictPageTextProvider.nct_id")
                if nct_key in self._audit_nct_ids:
                    raise AuditHoldoutViolationError(
                        f"Audit holdout violation in DictPageTextProvider: {nct_key!r} is in audit_nct_ids."
                    )
                if self._in_scope_nct_ids is not None and nct_key not in self._in_scope_nct_ids:
                    raise AuditHoldoutViolationError(
                        f"Unlisted trial {nct_key!r} in DictPageTextProvider is absent from in_scope_nct_ids."
                    )
                if not _is_safe_pdf_filename(key[1]):
                    raise CitationVerificationError(
                        f"Unsafe PDF filename in DictPageTextProvider key: {key!r}."
                    )
                if not _is_strict_int(key[2]) or key[2] < 1:
                    raise CitationVerificationError(
                        f"Invalid physical_page in DictPageTextProvider key: {key!r}."
                    )
                if len(key) == 4 and not _is_non_empty_str(key[3]):
                    raise CitationVerificationError(
                        f"Invalid text_source in DictPageTextProvider key: {key!r}."
                    )

    def bind_firewall(
        self,
        *,
        audit_nct_ids: AbstractSet[str],
        in_scope_nct_ids: AbstractSet[str] | None = None,
        allow_empty_audit_set: bool = False,
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
        if merged_scope is not None and (merged_audit & merged_scope):
            raise AuditHoldoutViolationError(
                "Audit holdout violation: DictPageTextProvider in_scope_nct_ids overlaps audit_nct_ids."
            )
        _require_scope_for_empty_audit_set(
            merged_audit,
            merged_scope,
            allow_empty_audit_set=allow_empty_audit_set,
            context="DictPageTextProvider.bind_firewall",
        )
        self._audit_nct_ids = merged_audit
        self._in_scope_nct_ids = merged_scope
        self._allow_empty_audit_set = allow_empty_audit_set
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
        # Construction may leave the provider unbound (empty audit set, no scope) so that
        # ``bind_firewall`` can tighten it later; serving text from that state would rest on
        # the caller's assertion alone, so it is refused until a scope is bound.
        _require_scope_for_empty_audit_set(
            self._audit_nct_ids,
            self._in_scope_nct_ids,
            allow_empty_audit_set=self._allow_empty_audit_set,
            context="DictPageTextProvider.get_page_text",
        )
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
    """Filesystem ``PageTextProvider`` reading JSON, gzipped JSON, or JSONL page maps."""

    def __init__(
        self,
        pages_dir: Path,
        *,
        ocr_dir: Path | None = None,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        in_scope_nct_ids: AbstractSet[str] | None = None,
        allow_empty_audit_set: bool = False,
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
        if type(allow_empty_audit_set) is not bool:
            raise AuditHoldoutViolationError(
                f"allow_empty_audit_set must be bool, got {type(allow_empty_audit_set).__name__}."
            )
        self._allow_empty_audit_set: bool = allow_empty_audit_set
        if self._in_scope_nct_ids is not None and (self._audit_nct_ids & self._in_scope_nct_ids):
            raise AuditHoldoutViolationError(
                "Audit holdout violation: DirectoryPageTextProvider in_scope_nct_ids overlaps audit_nct_ids."
            )
        if not self._allow_empty_audit_set and len(self._audit_nct_ids) == 0:
            raise AuditHoldoutViolationError(
                "DirectoryPageTextProvider requires non-empty audit_nct_ids unless allow_empty_audit_set=True."
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
        allow_empty_audit_set: bool = False,
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
        if merged_scope is not None and (merged_audit & merged_scope):
            raise AuditHoldoutViolationError(
                "Audit holdout violation: DirectoryPageTextProvider in_scope_nct_ids overlaps audit_nct_ids."
            )
        _require_scope_for_empty_audit_set(
            merged_audit,
            merged_scope,
            allow_empty_audit_set=allow_empty_audit_set,
            context="DirectoryPageTextProvider.bind_firewall",
        )
        if (
            merged_audit != self._audit_nct_ids
            or merged_scope != self._in_scope_nct_ids
            or allow_empty_audit_set != self._allow_empty_audit_set
        ):
            self._audit_nct_ids = merged_audit
            self._in_scope_nct_ids = merged_scope
            self._allow_empty_audit_set = allow_empty_audit_set
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

    def _check_layout_nct_allowed(self, raw_nct: Any, *, context: str) -> str:
        """Firewall a trial named by the page-map layout (a directory, a filename prefix).

        The trial requested in ``get_page_text`` is the call's subject, which the
        verify entry points have already firewalled; a trial named by the layout
        is reached from the files instead, so it follows
        ``_enforce_derived_nct_holdout``: without ``in_scope_nct_ids`` it is refused
        rather than admitted on the audit set alone.
        """
        checked = validate_strict_nct_id(raw_nct, context=context)
        _enforce_derived_nct_holdout(
            checked,
            verified_nct=None,
            context=context,
            audit_nct_ids=self._audit_nct_ids,
            in_scope_nct_ids=self._in_scope_nct_ids,
            allow_empty_audit_set=self._allow_empty_audit_set,
        )
        return checked

    def _check_path_part_for_embedded_nct(self, part: str, *, context: str) -> None:
        """Scan a path component for exact or obfuscated NCT tokens."""
        folded = _strip_invisible(unicodedata.normalize("NFKC", part))
        for match in _EMBEDDED_NCT_TOKEN_RE.finditer(folded):
            tok = match.group(0)
            upper = tok.upper()
            if STRICT_NCT_ID_RE.fullmatch(upper) is not None and upper in self._audit_nct_ids:
                raise AuditHoldoutViolationError(
                    f"Audit holdout violation in {context}: path references holdout trial {upper!r}."
                )
            if part != tok or STRICT_NCT_ID_RE.fullmatch(tok) is None:
                raise InvalidNctIdError(
                    f"Malformed or obfuscated NCT path component {part!r} in {context}."
                )
            self._check_layout_nct_allowed(tok, context=context)

    def _validate_payload_firewall(self, payload: Any, *, source_desc: str) -> str | None:
        """Recursively validate ``nct_id``, ``nctId``, ``nct_ids``, ``in_audit``, and URLs."""
        if not isinstance(payload, Mapping):
            return None
        discovered = _validate_nested_firewall_metadata(
            payload,
            context=source_desc,
            expected_nct_id=None,
            audit_nct_ids=self._audit_nct_ids,
            in_scope_nct_ids=self._in_scope_nct_ids,
            allow_empty_audit_set=self._allow_empty_audit_set,
        )
        if "nct_id" in payload:
            return str(payload["nct_id"])
        if "nctId" in payload:
            return str(payload["nctId"])
        return discovered[0] if len(set(discovered)) == 1 else None

    @staticmethod
    def _is_jsonl_path(path: Path) -> bool:
        return path.name.endswith((".jsonl", ".jsonl.gz"))

    @staticmethod
    def _is_json_map_path(path: Path) -> bool:
        return not DirectoryPageTextProvider._is_jsonl_path(path) and path.name.endswith(
            (".json", ".json.gz")
        )

    @staticmethod
    def _strip_map_extensions(name: str) -> str:
        stem = name[:-3] if name.endswith(".gz") else name
        for ext in (".jsonl", ".json"):
            if stem.endswith(ext):
                return stem[: -len(ext)]
        return stem

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

    def _validate_file_name_layout(self, entry: Path, *, root: Path) -> str | None:
        """Enforce strict directory/filename layout whitelist inside ``root``."""
        stem = self._strip_map_extensions(entry.name)
        if entry.parent == root:
            if self._is_jsonl_path(entry):
                self._check_path_part_for_embedded_nct(stem, context=f"page_map_jsonl({entry})")
                if _SAFE_STEM_RE.fullmatch(stem) is None:
                    raise CitationVerificationError(
                        f"Unsafe page-map JSONL filename {entry.name!r} under {root}."
                    )
                return None
            if "__" in stem:
                prefix, doc_part = stem.split("__", 1)
                checked_prefix = self._check_layout_nct_allowed(
                    prefix, context=f"page_map_file({entry})"
                )
                self._check_path_part_for_embedded_nct(
                    doc_part, context=f"page_map_file({entry})"
                )
                if _SAFE_STEM_RE.fullmatch(doc_part) is None:
                    raise CitationVerificationError(
                        f"Unsafe page-map filename {entry.name!r} under {root}."
                    )
                return checked_prefix
            self._check_path_part_for_embedded_nct(stem, context=f"page_map_file({entry})")
            if len(stem) != 64 or _SHA256_RE.fullmatch(stem) is None:
                raise CitationVerificationError(
                    f"Top-level page map {entry.name!r} under {root} must be <sha256>.json[.gz] "
                    f"or <NCT_ID>__<doc>.json[.gz]."
                )
            return None

        parent = entry.parent
        if parent.parent != root:
            self._check_path_part_for_embedded_nct(
                parent.name, context=f"page_map_dir({parent})"
            )
            raise CitationVerificationError(
                f"Nested page-map directory {parent} is not permitted under {root}."
            )

        if _HEX2_DIR_RE.fullmatch(parent.name) is not None:
            self._check_path_part_for_embedded_nct(stem, context=f"page_map_file({entry})")
            if (
                not self._is_json_map_path(entry)
                or len(stem) != 64
                or _SHA256_RE.fullmatch(stem) is None
                or stem[:2] != parent.name
            ):
                raise CitationVerificationError(
                    f"Shard file {entry} must be <sha256>.json[.gz] matching prefix {parent.name!r}."
                )
            return None

        checked_dir_nct = self._check_layout_nct_allowed(
            parent.name, context=f"page_map_dir({parent})"
        )
        self._check_path_part_for_embedded_nct(stem, context=f"page_map_file({entry})")
        if not self._is_json_map_path(entry) or _SAFE_STEM_RE.fullmatch(stem) is None:
            raise CitationVerificationError(
                f"Unsafe per-trial page map {entry.name!r} under {parent}."
            )
        return checked_dir_nct

    def _scan_root(self, root: Path, *, default_source_bucket: str) -> None:
        # Check root's own path components for any embedded/direct NCT token even if absent!
        for part in root.parts:
            folded = _strip_invisible(unicodedata.normalize("NFKC", part))
            if _EMBEDDED_NCT_TOKEN_RE.search(folded) is not None:
                if part == root.name and root.is_file() and "__" in part:
                    stem = self._strip_map_extensions(part)
                    prefix, rest = stem.split("__", 1)
                    self._check_layout_nct_allowed(prefix, context=f"page_map_file({root})")
                    self._check_path_part_for_embedded_nct(
                        rest, context=f"page_map_file({root})"
                    )
                else:
                    self._check_path_part_for_embedded_nct(part, context=f"page_map_root({root})")

        if not root.exists():
            return

        if root.is_file():
            stem = self._strip_map_extensions(root.name)
            expected_nct: str | None = None
            if "__" in stem:
                prefix, doc_part = stem.split("__", 1)
                expected_nct = self._check_layout_nct_allowed(
                    prefix, context=f"page_map_file({root})"
                )
                if _SAFE_STEM_RE.fullmatch(doc_part) is None:
                    raise CitationVerificationError(
                        f"Unsafe page-map filename {root.name!r}."
                    )
            elif STRICT_NCT_ID_RE.fullmatch(root.parent.name) is not None:
                expected_nct = self._check_layout_nct_allowed(
                    root.parent.name, context=f"page_map_dir({root.parent})"
                )

            if self._is_jsonl_path(root):
                self._ingest_jsonl_file(root, default_source_bucket=default_source_bucket)
            elif self._is_json_map_path(root):
                payload = self._read_json_file(root)
                payload_nct = self._validate_payload_firewall(payload, source_desc=str(root))
                if (
                    expected_nct is not None
                    and payload_nct is not None
                    and payload_nct != expected_nct
                ):
                    raise AuditHoldoutViolationError(
                        f"Page map {root} path NCT {expected_nct!r} disagrees with payload {payload_nct!r}."
                    )
            else:
                raise CitationVerificationError(
                    f"Unsupported page-map file extension on {root}."
                )
            return

        for entry in sorted(root.rglob("*")):
            if entry.is_dir():
                if entry.parent != root:
                    self._check_path_part_for_embedded_nct(
                        entry.name, context=f"page_map_dir({entry})"
                    )
                    raise CitationVerificationError(
                        f"Nested directory {entry} is not permitted under {root}."
                    )
                if _HEX2_DIR_RE.fullmatch(entry.name) is not None:
                    continue
                self._check_path_part_for_embedded_nct(
                    entry.name, context=f"page_map_dir({entry})"
                )
                self._check_layout_nct_allowed(entry.name, context=f"page_map_dir({entry})")
                continue

            if not entry.is_file():
                continue
            path_nct = self._validate_file_name_layout(entry, root=root)
            if self._is_jsonl_path(entry):
                self._ingest_jsonl_file(entry, default_source_bucket=default_source_bucket)
            elif self._is_json_map_path(entry):
                payload = self._read_json_file(entry)
                payload_nct = self._validate_payload_firewall(payload, source_desc=str(entry))
                if (
                    path_nct is not None
                    and payload_nct is not None
                    and payload_nct != path_nct
                ):
                    raise AuditHoldoutViolationError(
                        f"Page map {entry} path NCT {path_nct!r} disagrees with payload {payload_nct!r}."
                    )
            else:
                raise CitationVerificationError(
                    f"Unexpected non-JSON file {entry} inside page-map directory {root}."
                )

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
        # See DictPageTextProvider.get_page_text: an unbound provider serves nothing.
        _require_scope_for_empty_audit_set(
            self._audit_nct_ids,
            self._in_scope_nct_ids,
            allow_empty_audit_set=self._allow_empty_audit_set,
            context="DirectoryPageTextProvider.get_page_text",
        )
        checked_nct = self._check_nct_allowed(nct_id, context="page_text.nct_id")
        if not _is_strict_int(physical_page) or physical_page < 1:
            return None
        pages = self._load_doc_pages(checked_nct, filename, sha256, text_source)
        if pages is None:
            return None
        return pages.get(physical_page)


def _validate_pdf_root_firewall(
    pdf_root: Path | None,
    *,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
) -> None:
    """Scan ``pdf_root`` path components and subdirectories for holdout/malformed NCT IDs."""
    if pdf_root is None:
        return
    root = Path(pdf_root)

    def _check_part(part: str, ctx: str) -> None:
        folded = _strip_invisible(unicodedata.normalize("NFKC", part))
        for match in _EMBEDDED_NCT_TOKEN_RE.finditer(folded):
            tok = match.group(0)
            upper = tok.upper()
            if STRICT_NCT_ID_RE.fullmatch(upper) is not None and upper in audit_nct_ids:
                raise AuditHoldoutViolationError(
                    f"Audit holdout violation in {ctx}: path references holdout trial {upper!r}."
                )
            if part != tok or STRICT_NCT_ID_RE.fullmatch(tok) is None:
                raise InvalidNctIdError(
                    f"Malformed or obfuscated NCT path component {part!r} in {ctx}."
                )
            if in_scope_nct_ids is not None and tok not in in_scope_nct_ids:
                raise AuditHoldoutViolationError(
                    f"Audit holdout refusal in {ctx}: path references unlisted trial {tok!r}."
                )

    for part in root.parts:
        _check_part(part, f"pdf_root({root})")
    if root.exists() and root.is_dir():
        for entry in sorted(root.rglob("*")):
            rel_parts = entry.relative_to(root).parts
            for part in rel_parts:
                _check_part(part, f"pdf_root_entry({entry})")


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

    nct_id, checked_audit_ids, checked_scope_ids = _enforce_record_firewall(
        doc,
        record_label="document",
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_pdf_root_firewall(
        pdf_root,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
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

    url_raw = doc.get("url")
    if not _is_non_empty_str(url_raw):
        issues.append(
            VerificationIssue(
                code="INVALID_DOCUMENT_URL",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Document url must be a non-empty string, got {url_raw!r}",
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


def _validate_docs_mapping_for_nct(
    expected_nct: str,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
    *,
    context: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    allow_empty_audit_set: bool,
) -> None:
    """Validate every document entry inside ``docs_by_filename`` against the firewall."""
    if not isinstance(docs_by_filename, Mapping):
        raise CitationVerificationError(
            f"{context} must be a mapping, got {type(docs_by_filename).__name__}."
        )
    for doc_fn, doc_obj in docs_by_filename.items():
        if not isinstance(doc_obj, Mapping):
            raise CitationVerificationError(
                f"{context}[{doc_fn!r}] must be a mapping, got {type(doc_obj).__name__}."
            )
        _validate_nested_firewall_metadata(
            doc_obj,
            context=f"{context}[{doc_fn!r}]",
            expected_nct_id=expected_nct,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )


def verify_citation(
    nct_id: str,
    location_key: tuple[int, str, str],
    citation: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
    pages_not_searched: Any = None,
    page_text_provider: PageTextProvider | None = None,
    text_source: str = "pdfkit",
) -> tuple[MatchTier | None, list[VerificationIssue]]:
    """Verify a single citation's firewall status, shape, bounds, and single-page match."""
    # Phase A (F2): Strict NCT validation FIRST across nct_id, sets, and docs_by_filename.
    checked_nct = validate_strict_nct_id(nct_id, context="citation.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    if isinstance(docs_by_filename, Mapping):
        for doc_fn, doc_obj in docs_by_filename.items():
            if isinstance(doc_obj, Mapping):
                _prescan_nested_nct_fields(
                    doc_obj, context=f"docs_by_filename[{doc_fn!r}]"
                )
    if isinstance(citation, Mapping):
        _prescan_nested_nct_fields(citation, context="citation")

    # Phase B (F1): Enforce holdout firewall on nct_id, citation, docs_by_filename, and provider.
    enforce_audit_holdout(
        checked_nct,
        in_audit=in_audit,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    if isinstance(citation, Mapping):
        _validate_nested_firewall_metadata(
            citation,
            context="citation",
            expected_nct_id=checked_nct,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
    _validate_docs_mapping_for_nct(
        checked_nct,
        docs_by_filename,
        context="docs_by_filename",
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    if isinstance(page_text_provider, (DirectoryPageTextProvider, DictPageTextProvider)):
        page_text_provider.bind_firewall(
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
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
    quotation_valid = (
        type(quotation) is str
        and len(quotation) > 0
        and _is_non_empty_str(quotation)
        and bool(normalize_for_page_match(quotation))
    )

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
    manifest_has_documents: bool | None = None,
) -> tuple[dict[str, int], list[VerificationIssue]]:
    """Verify a single location record, provenance, citations, coverage, and neutrality.

    ``manifest_has_documents`` is what the index says about posted documents for
    the record's trial: True, False, or None when the index does not say (or the
    caller did not pass it). None is kept as unknown, never read as "no
    documents": a ``NO_DOCUMENT`` row for a trial with no supplied documents and
    unknown presence is reported as ``NO_DOCUMENT_PRESENCE_UNKNOWN`` rather than
    accepted.

    ``docs_by_nct`` keys other than the record's own trial are firewalled by
    ``_enforce_derived_nct_holdout``: without ``in_scope_nct_ids`` they are
    refused, since the caller's ``in_audit`` speaks only for this record's trial.
    """
    if not isinstance(loc, Mapping):
        raise CitationVerificationError(
            f"Location record must be a mapping, got {type(loc).__name__}."
        )
    if not isinstance(docs_by_nct, Mapping):
        raise CitationVerificationError(
            f"docs_by_nct must be a mapping, got {type(docs_by_nct).__name__}."
        )
    if manifest_has_documents is not None and type(manifest_has_documents) is not bool:
        raise CitationVerificationError(
            f"manifest_has_documents must be bool or None, got {manifest_has_documents!r}."
        )

    # Phase A (F2): Validate strict NCT IDs on docs_by_nct BEFORE _enforce_record_firewall.
    for k, per_nct_docs in docs_by_nct.items():
        validate_strict_nct_id(k, context="docs_by_nct key")
        if isinstance(per_nct_docs, Mapping):
            for doc_fn, doc_obj in per_nct_docs.items():
                if isinstance(doc_obj, Mapping):
                    _prescan_nested_nct_fields(
                        doc_obj, context=f"docs_by_nct[{k!r}][{doc_fn!r}]"
                    )

    # Phase A+B on loc & sets.
    nct_id, checked_audit_ids, checked_scope_ids = _enforce_record_firewall(
        loc,
        record_label="location",
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        in_scope_nct_ids=in_scope_nct_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )

    # Phase B on docs_by_nct keys and inner document objects. The caller's in_audit
    # covers ``nct_id`` only; any other key needs in-scope membership.
    for k, per_nct_docs in docs_by_nct.items():
        _enforce_derived_nct_holdout(
            k,
            verified_nct=nct_id,
            context="docs_by_nct key",
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_docs_mapping_for_nct(
            k,
            per_nct_docs,
            context=f"docs_by_nct[{k!r}]",
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    if isinstance(page_text_provider, (DirectoryPageTextProvider, DictPageTextProvider)):
        page_text_provider.bind_firewall(
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
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
    # Posted documents are known to exist when any are supplied or the index says so;
    # known absent only when none are supplied and the index says False; otherwise unknown.
    has_posted_docs: bool | None
    if docs_for_nct or manifest_has_documents is True:
        has_posted_docs = True
    elif manifest_has_documents is False:
        has_posted_docs = False
    else:
        has_posted_docs = None
    if status_raw == "NO_DOCUMENT" and has_posted_docs is None:
        issues.append(
            VerificationIssue(
                code="NO_DOCUMENT_PRESENCE_UNKNOWN",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=(
                    f"status='NO_DOCUMENT' cannot be verified for {nct_id}: no documents were "
                    f"supplied for it and the index does not say whether it has posted "
                    f"documents (manifest_has_documents is None)."
                ),
            )
        )
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
            in_audit=in_audit,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
            pages_not_searched=pages_not_searched,
            page_text_provider=page_text_provider,
            text_source=effective_text_source,
        )
        issues.extend(cit_issues)
        if any(i.code == "CITATION_ELLIPSIS_FORBIDDEN" for i in cit_issues):
            counts["ellipsis_flags"] += 1
        if tier == "exact_ws":
            counts["exact_ws_matches"] += 1
        elif tier == "unicode_nfkc_hyphen":
            counts["unicode_nfkc_hyphen_matches"] += 1
        elif tier == "mismatch":
            counts["substring_mismatches"] += 1

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
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
) -> list[VerificationIssue]:
    """Assert exact bijection over ``range(expected_primary_outcomes) x ELEMENTS``."""
    # Phase A (F2): Validate strict NCT IDs across nct_id, sets, and locations FIRST.
    checked_nct = validate_strict_nct_id(nct_id, context="completeness.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    if isinstance(locations, (str, bytes, Mapping)) or not isinstance(locations, Sequence):
        raise CitationVerificationError(
            f"locations must be a sequence of mappings, got {type(locations).__name__}."
        )
    for idx_loc, loc in enumerate(locations):
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"Location entry {idx_loc} in verify_trial_completeness must be a mapping, "
                f"got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{idx_loc}].nct_id")
        _prescan_nested_nct_fields(loc, context=f"locations[{idx_loc}]")

    # Phase B (F1): Enforce holdout firewall on nct_id and all location entries.
    enforce_audit_holdout(
        checked_nct,
        in_audit=in_audit,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    for idx_loc, loc in enumerate(locations):
        loc_nct = loc["nct_id"]
        # The caller's in_audit covers ``nct_id`` only; rows of any other trial need
        # in-scope membership (they are skipped by the grid below, but still inspected here).
        _enforce_derived_nct_holdout(
            loc_nct,
            verified_nct=checked_nct,
            context=f"locations[{idx_loc}].nct_id",
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _validate_nested_firewall_metadata(
            loc,
            context=f"locations[{idx_loc}]",
            expected_nct_id=loc_nct,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    if type(allow_sparse) is not bool:
        raise CitationVerificationError(
            f"allow_sparse must be bool, got {allow_sparse!r}."
        )
    if type(pass_name) is not str or pass_name not in ALLOWED_PASSES:
        raise CitationVerificationError(
            f"Invalid pass_name {pass_name!r}; expected one of {ALLOWED_PASSES}."
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

    Requires ``in_scope_nct_ids``: a corpus spans many trials and this entry
    point takes no per-trial ``in_audit``, and a record's own ``in_audit`` never
    admits it (see ``_enforce_record_firewall``), so membership of the verified
    in-scope index rows is what admits each trial.

    ``expected_has_documents_by_nct`` values are True, False, or None (the index
    does not say); a corpus trial missing from a supplied map, or with no map at
    all, is passed on as None (unknown), never as False.
    """
    if isinstance(documents, (str, bytes, Mapping)) or not isinstance(documents, Sequence):
        raise CitationVerificationError(
            f"documents must be a sequence of mappings, got {type(documents).__name__}."
        )
    if isinstance(locations, (str, bytes, Mapping)) or not isinstance(locations, Sequence):
        raise CitationVerificationError(
            f"locations must be a sequence of mappings, got {type(locations).__name__}."
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
        _prescan_nested_nct_fields(doc, context=f"documents[{idx}]")
    for idx, loc in enumerate(locations):
        if not isinstance(loc, Mapping):
            raise CitationVerificationError(
                f"locations[{idx}] must be a mapping, got {type(loc).__name__}."
            )
        validate_strict_nct_id(loc.get("nct_id"), context=f"locations[{idx}].nct_id")
        _prescan_nested_nct_fields(loc, context=f"locations[{idx}]")

    # Step 2 (F1): Enforce non-empty audit_nct_ids and firewall invariants.
    if type(allow_empty_audit_set) is not bool:
        raise AuditHoldoutViolationError(
            f"allow_empty_audit_set must be bool, got {type(allow_empty_audit_set).__name__}."
        )
    if checked_scope_ids is None:
        raise AuditHoldoutViolationError(
            "verify_census_corpus requires in_scope_nct_ids (the index rows with explicit "
            "in_audit=False): it takes no per-trial in_audit, and a record's own in_audit "
            "never admits it."
        )
    if checked_audit_ids & checked_scope_ids:
        overlap = sorted(checked_audit_ids & checked_scope_ids)
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: in_scope_nct_ids overlaps audit_nct_ids: {overlap}."
        )
    _require_scope_for_empty_audit_set(
        checked_audit_ids,
        checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
        context="verify_census_corpus",
    )

    if isinstance(page_text_provider, (DirectoryPageTextProvider, DictPageTextProvider)):
        page_text_provider.bind_firewall(
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
    _validate_pdf_root_firewall(
        pdf_root,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
    )

    # expected_* keys are index-derived trial IDs with no flag of their own: admitted by
    # in-scope membership only.
    if expected_has_documents_by_nct is not None:
        for nct_id, has_docs_val in sorted(expected_has_documents_by_nct.items()):
            enforce_audit_holdout(
                nct_id,
                audit_nct_ids=checked_audit_ids,
                in_scope_nct_ids=checked_scope_ids,
                allow_empty_audit_set=allow_empty_audit_set,
            )
            if has_docs_val is not None and type(has_docs_val) is not bool:
                raise CitationVerificationError(
                    f"expected_has_documents_by_nct[{nct_id!r}] must be bool or None (unknown), "
                    f"got {has_docs_val!r}."
                )

    if expected_outcomes_by_nct is not None:
        for nct_id in sorted(expected_outcomes_by_nct.keys()):
            enforce_audit_holdout(
                nct_id,
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
        # Absent from the map (or no map) is unknown, not "no documents".
        manifest_has_docs: bool | None = (
            expected_has_documents_by_nct.get(loc_nct)
            if expected_has_documents_by_nct is not None
            else None
        )
        # Each location sees its own trial's documents only: it reads no other trial's, and
        # every document was already firewalled by verify_document_record above.
        own_docs_by_nct: dict[str, dict[str, Mapping[str, Any]]] = (
            {loc_nct: docs_by_nct[loc_nct]} if loc_nct in docs_by_nct else {}
        )
        counts, loc_issues = verify_location_record(
            loc,
            docs_by_nct=own_docs_by_nct,
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
            if pass_name in ALLOWED_PASSES:
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
            # The grid counts only this trial's rows; pass only those, admitted by scope.
            trial_locations = [loc for loc in locations if loc["nct_id"] == nct_id]
            for pass_name in sorted(passes_by_nct.get(nct_id, set()) | {"primary"}):
                all_issues.extend(
                    verify_trial_completeness(
                        nct_id,
                        trial_locations,
                        expected_primary_outcomes=expected_k,
                        pass_name=pass_name,
                        allow_sparse=pass_name in SPARSE_PASSES,
                        audit_nct_ids=checked_audit_ids,
                        in_scope_nct_ids=checked_scope_ids,
                        allow_empty_audit_set=allow_empty_audit_set,
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
