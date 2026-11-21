"""Mechanical single-page citation verifier and neutral-language guard.

Verifies document records, decision-rule location records, and verbatim
single-page PDF quotations for clinical trial rule censuses. Designed to run
100% offline with Python 3.11+ stdlib only, zero network calls, and zero
external PDF binary dependencies in Linux CI.

Key invariants:
- Hard ``in_audit`` holdout firewall: any reference to an ``in_audit == True``
  trial raises ``AuditHoldoutViolationError`` unconditionally before reading
  or inspecting document text.
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
from typing import AbstractSet, Any, Final, Literal, Mapping, Protocol, Sequence, runtime_checkable
import unicodedata

__version__: Final[str] = "1.0.0"
VERIFIER_RULES_VERSION: Final[str] = "1.0.0"

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
DOC_TYPES: Final[tuple[str, ...]] = ("protocol", "sap", "combined")
ALLOWED_PASSES: Final[tuple[str, ...]] = (
    "primary",
    "second",
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
)
DOCUMENT_OPTIONAL_KEYS: Final[tuple[str, ...]] = (
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

_NCT_RE: Final[re.Pattern[str]] = re.compile(r"^NCT\d{8}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PDF_BASENAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+\.pdf$", re.IGNORECASE)
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


class AuditHoldoutViolationError(CitationVerificationError):
    """Raised immediately whenever any record or batch references a trial with in_audit == True."""


@runtime_checkable
class PageTextProvider(Protocol):
    """Pluggable single-page text lookup (macOS PDFKit JSON maps, OCR page maps, or CI dict fixtures).

    Structurally compatible with Module 1's ``DocumentPageExtractor.get_page_text``.
    """

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


# Calibrated against all 4,718 real location notes in rule-census-20261114
# (0 false positives on protocol decision rules like "if Week 16 is statistically
# significant", "switching arm", "non-compliant investigational site", "sites
# failed CFDA inspection", or "GCP violations").
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


def enforce_audit_holdout(
    nct_id: str,
    *,
    in_audit: bool = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
) -> None:
    """Hard holdout firewall: abort immediately on any ``in_audit == True`` trial.

    Raises ``AuditHoldoutViolationError`` unconditionally (never downgraded to
    a warning or issue) when ``in_audit is True`` or ``nct_id in audit_nct_ids``.
    Rejects non-boolean ``in_audit`` values so missing flags can never silently
    coerce to ``False``.
    """
    if type(in_audit) is not bool:
        raise CitationVerificationError(
            f"in_audit must be an explicit bool for {nct_id!r}, got {type(in_audit).__name__}"
        )
    if in_audit is True or nct_id in audit_nct_ids:
        raise AuditHoldoutViolationError(
            f"Audit holdout violation: trial {nct_id!r} has in_audit=True and may not be "
            f"inspected, verified, or scheduled for census extraction."
        )


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


# Spec #60 alias so callers using either check_neutral_language or
# scan_neutral_language share the exact deterministic implementation.
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
    3. ``(nct_id, filename, physical_page)``
    or a nested mapping ``sha256 -> Sequence[str]`` (1-based ``physical_page``
    indexed at ``physical_page - 1``).
    """

    def __init__(
        self,
        pages_by_key: Mapping[Any, str] | None = None,
        *,
        pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
        ocr_pages_by_sha256: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._pages_by_key: dict[Any, str] = dict(pages_by_key or {})
        self._pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (pages_by_sha256 or {}).items()
        }
        self._ocr_pages_by_sha256: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (ocr_pages_by_sha256 or {}).items()
        }

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
        if not _is_strict_int(physical_page) or physical_page < 1:
            return None
        for key in (
            (sha256, physical_page, text_source),
            (nct_id, filename, physical_page, text_source),
            (sha256, physical_page),
            (nct_id, filename, physical_page),
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
    """Filesystem ``PageTextProvider`` reading JSON or gzipped JSON page maps.

    Supports both flat census page directories (``{sha256}.json``,
    ``{nct_id}/{filename}.json``, ``{nct_id}__{filename}.json``) and Module 1's
    content-addressed cache layout (``{sha256[:2]}/{sha256}.json.gz``).
    """

    def __init__(
        self,
        pages_dir: Path,
        *,
        ocr_dir: Path | None = None,
    ) -> None:
        self._pages_dir = Path(pages_dir)
        self._ocr_dir = Path(ocr_dir) if ocr_dir is not None else None
        self._cache: dict[tuple[str, str, str, str], dict[int, str] | None] = {}

    def _candidate_paths(
        self,
        base_dir: Path,
        nct_id: str,
        filename: str,
        sha256: str,
    ) -> list[Path]:
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
                if candidate.suffix == ".gz":
                    with gzip.open(candidate, "rt", encoding="utf-8") as fh:
                        payload = json.load(fh)
                else:
                    with candidate.open("r", encoding="utf-8") as fh:
                        payload = json.load(fh)
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
        if not _is_strict_int(physical_page) or physical_page < 1:
            return None
        pages = self._load_doc_pages(nct_id, filename, sha256, text_source)
        if pages is None:
            return None
        return pages.get(physical_page)


def verify_document_record(
    doc: Mapping[str, Any],
    *,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    strict_keys: bool = True,
    pdf_root: Path | None = None,
) -> list[VerificationIssue]:
    """Validate a single document record against schema, hash, and page-bound invariants."""
    nct_id_raw = doc.get("nct_id")
    nct_id = nct_id_raw if isinstance(nct_id_raw, str) else ""
    in_audit_val = doc.get("in_audit", False)
    enforce_audit_holdout(nct_id, in_audit=in_audit_val, audit_nct_ids=audit_nct_ids)

    issues: list[VerificationIssue] = []
    filename_raw = doc.get("filename")
    filename = filename_raw if isinstance(filename_raw, str) else None

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

    allowed = required if strict_keys else (required | set(DOCUMENT_OPTIONAL_KEYS))
    extra = sorted(keys - allowed)
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

    if not isinstance(nct_id_raw, str) or not _NCT_RE.fullmatch(nct_id_raw):
        issues.append(
            VerificationIssue(
                code="INVALID_NCT_ID",
                severity="error",
                nct_id=nct_id,
                location_key=None,
                filename=filename,
                physical_page=None,
                message=f"Invalid nct_id {nct_id_raw!r}; expected ^NCT\\d{{8}}$",
            )
        )

    if (
        not isinstance(filename_raw, str)
        or "/" in filename_raw
        or "\\" in filename_raw
        or not _SAFE_PDF_BASENAME_RE.fullmatch(filename_raw)
    ):
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
    if not isinstance(doc_type, str) or doc_type not in DOC_TYPES:
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
        not isinstance(unreadable_subtype, str)
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
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
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
                    message=f"http_status=200 requires int physical_pages >= 1, got {physical_pages!r}",
                )
            )
    else:
        if physical_pages is not None and (not _is_strict_int(physical_pages) or physical_pages < 0):
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
        and isinstance(nct_id_raw, str)
        and isinstance(filename_raw, str)
        and isinstance(sha256, str)
        and _SHA256_RE.fullmatch(sha256)
    ):
        candidate_pdf = pdf_root / nct_id_raw / filename_raw
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


def verify_citation(
    nct_id: str,
    location_key: tuple[int, str, str],
    citation: Mapping[str, Any],
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]],
    pages_not_searched: Any = None,
    page_text_provider: PageTextProvider | None = None,
    text_source: str = "pdfkit",
) -> tuple[MatchTier | None, list[VerificationIssue]]:
    """Verify a single citation's schema, bounds, hygiene, and single-page substring match."""
    issues: list[VerificationIssue] = []
    if not isinstance(citation, Mapping):
        issues.append(
            VerificationIssue(
                code="INVALID_CITATION_TYPE",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=None,
                physical_page=None,
                message="Citation must be a JSON object.",
            )
        )
        return None, issues

    filename_raw = citation.get("filename")
    filename = filename_raw if isinstance(filename_raw, str) else None
    phys_raw = citation.get("physical_page")
    phys_page = phys_raw if _is_strict_int(phys_raw) else None

    keys = set(citation.keys())
    required = set(CITATION_REQUIRED_KEYS)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        issues.append(
            VerificationIssue(
                code="CITATION_MISSING_KEYS",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=phys_page,
                message=f"Citation missing required keys: {missing}",
            )
        )
    if extra:
        issues.append(
            VerificationIssue(
                code="CITATION_EXTRA_KEYS",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=phys_page,
                message=f"Citation has unexpected keys: {extra}",
            )
        )

    # Strict single-page physical_page check (rejects bool, ranges, lists).
    if isinstance(phys_raw, (list, tuple)) or (
        isinstance(phys_raw, str) and _PAGE_RANGE_TEXT_RE.match(phys_raw)
    ):
        issues.append(
            VerificationIssue(
                code="cross_page_span_violation",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=None,
                message=f"Citation physical_page must be a single int, not a span {phys_raw!r}",
            )
        )
    elif not _is_strict_int(phys_raw) or phys_raw < 1:
        issues.append(
            VerificationIssue(
                code="INVALID_CITATION_PAGE",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=None,
                message=f"Citation physical_page must be a positive int, got {phys_raw!r}",
            )
        )

    doc = docs_by_filename.get(filename) if filename is not None else None
    doc_sha = ""
    doc_pages = 0
    if doc is None:
        issues.append(
            VerificationIssue(
                code="CITATION_UNKNOWN_DOCUMENT",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=phys_page,
                message=f"Citation references unknown document {filename!r} for {nct_id}",
            )
        )
    else:
        if doc.get("http_status") != 200:
            issues.append(
                VerificationIssue(
                    code="CITATION_FAILED_DOCUMENT",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Citation references document {filename!r} with "
                        f"http_status={doc.get('http_status')!r} != 200"
                    ),
                )
            )
        doc_sha = doc.get("sha256") if isinstance(doc.get("sha256"), str) else ""
        cit_sha = citation.get("sha256")
        if cit_sha != doc_sha or not isinstance(cit_sha, str) or not _SHA256_RE.fullmatch(cit_sha):
            issues.append(
                VerificationIssue(
                    code="CITATION_SHA256_MISMATCH",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
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
                        nct_id=nct_id,
                        location_key=location_key,
                        filename=filename,
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
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Citation under text_source='pdfkit' cites page {phys_page} "
                        f"which is listed in pages_without_text"
                    ),
                )
            )

    if filename is not None and phys_page is not None and pages_not_searched:
        unsearched = _extract_unsearched_pages_for_file(pages_not_searched, filename)
        if phys_page in unsearched:
            issues.append(
                VerificationIssue(
                    code="CITATION_IN_UNSEARCHED_PAGES",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Citation page {phys_page} in {filename!r} falls inside pages_not_searched"
                    ),
                )
            )

    quotation = citation.get("quotation")
    quotation_valid = True
    if not isinstance(quotation, str) or len(quotation) == 0:
        quotation_valid = False
        issues.append(
            VerificationIssue(
                code="CITATION_EMPTY_QUOTATION",
                severity="error",
                nct_id=nct_id,
                location_key=location_key,
                filename=filename,
                physical_page=phys_page,
                message="Citation quotation must be a non-empty string.",
            )
        )
    else:
        if len(quotation) > MAX_QUOTATION_CHARS:
            issues.append(
                VerificationIssue(
                    code="CITATION_QUOTATION_TOO_LONG",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Citation quotation length {len(quotation)} exceeds "
                        f"maximum {MAX_QUOTATION_CHARS} characters"
                    ),
                )
            )
        if quotation != " ".join(quotation.split()):
            issues.append(
                VerificationIssue(
                    code="CITATION_QUOTATION_UNNORMALIZED_WHITESPACE",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message="Citation quotation must use canonical single-space whitespace.",
                )
            )
        if has_ellipsis(quotation):
            issues.append(
                VerificationIssue(
                    code="CITATION_ELLIPSIS_FORBIDDEN",
                    severity="error",
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message="Citation quotation contains forbidden ellipsis omission marker.",
                )
            )

    match_tier: MatchTier | None = None
    if (
        page_text_provider is not None
        and quotation_valid
        and isinstance(quotation, str)
        and filename is not None
        and phys_page is not None
        and doc is not None
        and doc.get("http_status") == 200
        and (doc_pages == 0 or 1 <= phys_page <= doc_pages)
    ):
        page_text = page_text_provider.get_page_text(
            nct_id,
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
                    nct_id=nct_id,
                    location_key=location_key,
                    filename=filename,
                    physical_page=phys_page,
                    message=(
                        f"Page text unavailable for {nct_id}/{filename} page {phys_page} "
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
                        nct_id,
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
                        nct_id,
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
                            nct_id=nct_id,
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
                            nct_id=nct_id,
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
                    nct_id=nct_id,
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
        not isinstance(loc_subtype, str) or loc_subtype not in UNREADABLE_SUBTYPES
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
    note = loc.get("note") if isinstance(loc.get("note"), str) else ""
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


def verify_location_record(
    loc: Mapping[str, Any],
    *,
    docs_by_nct: Mapping[str, Mapping[str, Mapping[str, Any]]],
    audit_nct_ids: AbstractSet[str] = frozenset(),
    page_text_provider: PageTextProvider | None = None,
    require_exhaustive_for_not_found: bool = True,
    text_source: str = "pdfkit",
) -> tuple[dict[str, int], list[VerificationIssue]]:
    """Verify a single location record, its citations, search coverage, and note neutrality."""
    nct_id_raw = loc.get("nct_id")
    nct_id = nct_id_raw if isinstance(nct_id_raw, str) else ""
    in_audit_val = loc.get("in_audit", False)
    enforce_audit_holdout(nct_id, in_audit=in_audit_val, audit_nct_ids=audit_nct_ids)

    idx_raw = loc.get("primary_outcome_index")
    elem_raw = loc.get("element")
    pass_raw = loc.get("pass")
    loc_key: tuple[int, str, str] = (
        idx_raw if _is_strict_int(idx_raw) else -1,
        elem_raw if isinstance(elem_raw, str) else "",
        pass_raw if isinstance(pass_raw, str) else "",
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

    if not isinstance(nct_id_raw, str) or not _NCT_RE.fullmatch(nct_id_raw):
        issues.append(
            VerificationIssue(
                code="INVALID_NCT_ID",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"Invalid nct_id {nct_id_raw!r}",
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
    if not isinstance(elem_raw, str) or elem_raw not in ELEMENTS:
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
    if not isinstance(pass_raw, str) or pass_raw not in ALLOWED_PASSES:
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

    status = loc.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        issues.append(
            VerificationIssue(
                code="INVALID_STATUS",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"Invalid status {status!r}; expected one of {STATUSES}",
            )
        )

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

    if status in {"FOUND", "AMBIGUOUS", "NOT_APPLICABLE"} and len(citations) < 1:
        issues.append(
            VerificationIssue(
                code="STATUS_REQUIRES_CITATION",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"status={status!r} requires at least 1 citation, found 0.",
            )
        )
    elif status in {"NOT_FOUND", "NO_DOCUMENT"} and len(citations) != 0:
        issues.append(
            VerificationIssue(
                code="STATUS_FORBIDS_CITATIONS",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=f"status={status!r} requires 0 citations, found {len(citations)}.",
            )
        )

    docs_for_nct = docs_by_nct.get(nct_id, {})
    if status == "NO_DOCUMENT" and docs_for_nct:
        issues.append(
            VerificationIssue(
                code="NO_DOCUMENT_WITH_POSTED_DOCUMENT",
                severity="error",
                nct_id=nct_id,
                location_key=loc_key,
                filename=None,
                physical_page=None,
                message=(
                    f"status='NO_DOCUMENT' is invalid when {len(docs_for_nct)} posted "
                    f"document(s) exist for {nct_id} ({sorted(docs_for_nct.keys())})."
                ),
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
                message=(
                    "pages_searched == 'all' contradicts non-empty pages_not_searched."
                ),
            )
        )
    elif has_unsearched:
        issues.extend(
            _validate_page_partition(
                nct_id, loc_key, pages_searched, pages_not_searched, docs_for_nct
            )
        )
        if status == "NOT_FOUND" and require_exhaustive_for_not_found:
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

    if status == "UNREADABLE":
        issues.extend(_verify_unreadable_validity(nct_id, loc_key, loc, docs_for_nct))

    # Effective text source: allow ocr_recovery pass to default to OCR when
    # caller kept default text_source="pdfkit", unless explicitly specified.
    effective_text_source = loc.get("text_source")
    if not isinstance(effective_text_source, str) or not effective_text_source:
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
        elif tier == "unicode_nfkc_hyphen":
            counts["unicode_nfkc_hyphen_matches"] += 1
        elif tier == "mismatch":
            counts["substring_mismatches"] += 1

    # Neutral-language scan on worker commentary (never on verbatim citations).
    note = loc.get("note")
    if isinstance(note, str):
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
) -> list[VerificationIssue]:
    """Assert exact bijection over ``range(expected_primary_outcomes) x ELEMENTS``."""
    issues: list[VerificationIssue] = []
    if not _is_strict_int(expected_primary_outcomes) or expected_primary_outcomes < 1:
        issues.append(
            VerificationIssue(
                code="INVALID_EXPECTED_OUTCOMES_COUNT",
                severity="error",
                nct_id=nct_id,
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
        if loc.get("nct_id") != nct_id or loc.get("pass") != pass_name:
            continue
        idx = loc.get("primary_outcome_index")
        elem = loc.get("element")
        if _is_strict_int(idx) and isinstance(elem, str):
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
                nct_id=nct_id,
                location_key=(idx, elem, pass_name),
                filename=None,
                physical_page=None,
                message=(
                    f"Duplicate location row for ({nct_id}, outcome={idx}, "
                    f"element={elem!r}, pass={pass_name!r})."
                ),
            )
        )
    for idx, elem in missing:
        issues.append(
            VerificationIssue(
                code="TRIAL_GRID_MISSING_CELL",
                severity="error",
                nct_id=nct_id,
                location_key=(idx, elem, pass_name),
                filename=None,
                physical_page=None,
                message=(
                    f"Missing location row for ({nct_id}, outcome={idx}, "
                    f"element={elem!r}, pass={pass_name!r})."
                ),
            )
        )
    for idx, elem in unexpected:
        issues.append(
            VerificationIssue(
                code="TRIAL_GRID_UNEXPECTED_CELL",
                severity="error",
                nct_id=nct_id,
                location_key=(idx, elem, pass_name),
                filename=None,
                physical_page=None,
                message=(
                    f"Unexpected location row outside 0..{expected_primary_outcomes - 1} x ELEMENTS: "
                    f"({nct_id}, outcome={idx}, element={elem!r}, pass={pass_name!r})."
                ),
            )
        )

    return issues


def verify_census_corpus(
    documents: Sequence[Mapping[str, Any]],
    locations: Sequence[Mapping[str, Any]],
    *,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    page_text_provider: PageTextProvider | None = None,
    pdf_root: Path | None = None,
    expected_outcomes_by_nct: Mapping[str, int] | None = None,
    strict_document_keys: bool = True,
    require_exhaustive_for_not_found: bool = True,
    text_source: str = "pdfkit",
) -> CorpusVerificationReport:
    """Run full mechanical verification across a documents + locations corpus.

    Raises ``AuditHoldoutViolationError`` immediately if ANY document or
    location record references an ``in_audit == True`` trial.
    """
    all_issues: list[VerificationIssue] = []
    docs_by_nct: dict[str, dict[str, Mapping[str, Any]]] = {}

    for doc in documents:
        doc_issues = verify_document_record(
            doc,
            audit_nct_ids=audit_nct_ids,
            strict_keys=strict_document_keys,
            pdf_root=pdf_root,
        )
        all_issues.extend(doc_issues)
        nct_id = doc.get("nct_id")
        filename = doc.get("filename")
        if isinstance(nct_id, str) and isinstance(filename, str):
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
        counts, loc_issues = verify_location_record(
            loc,
            docs_by_nct=docs_by_nct,
            audit_nct_ids=audit_nct_ids,
            page_text_provider=page_text_provider,
            require_exhaustive_for_not_found=require_exhaustive_for_not_found,
            text_source=text_source,
        )
        all_issues.extend(loc_issues)
        citations_checked += counts["citations_checked"]
        exact_ws_matches += counts["exact_ws_matches"]
        unicode_nfkc_hyphen_matches += counts["unicode_nfkc_hyphen_matches"]
        substring_mismatches += counts["substring_mismatches"]
        ellipsis_flags += counts["ellipsis_flags"]
        neutral_language_flags += counts["neutral_language_flags"]

        nct_id = loc.get("nct_id")
        idx = loc.get("primary_outcome_index")
        elem = loc.get("element")
        pass_name = loc.get("pass")
        if (
            isinstance(nct_id, str)
            and _is_strict_int(idx)
            and isinstance(elem, str)
            and isinstance(pass_name, str)
        ):
            full_key = (nct_id, idx, elem, pass_name)
            if full_key in seen_location_keys:
                all_issues.append(
                    VerificationIssue(
                        code="DUPLICATE_LOCATION_KEY",
                        severity="error",
                        nct_id=nct_id,
                        location_key=(idx, elem, pass_name),
                        filename=None,
                        physical_page=None,
                        message=f"Duplicate location row for {full_key}.",
                    )
                )
            else:
                seen_location_keys.add(full_key)
            passes_by_nct.setdefault(nct_id, set()).add(pass_name)

    if expected_outcomes_by_nct is not None:
        for nct_id in sorted(expected_outcomes_by_nct.keys()):
            enforce_audit_holdout(nct_id, in_audit=False, audit_nct_ids=audit_nct_ids)
            expected_k = expected_outcomes_by_nct[nct_id]
            for pass_name in sorted(passes_by_nct.get(nct_id, {"primary"})):
                if pass_name == "ocr_recovery":
                    # Recovery passes intentionally cover only previously UNREADABLE cells.
                    continue
                all_issues.extend(
                    verify_trial_completeness(
                        nct_id,
                        locations,
                        expected_primary_outcomes=expected_k,
                        pass_name=pass_name,
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
