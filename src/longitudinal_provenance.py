"""Longitudinal registry-to-document-to-publication provenance assembler.

Assembles strictly factual dated milestones and calendar-interval day deltas
across:
1. ClinicalTrials.gov registry status milestones (``studyFirstPostDateStruct``,
   ``primaryCompletionDateStruct``, ``resultsFirstPostDateStruct``,
   ``lastUpdatePostDateStruct``).
2. ClinicalTrials.gov ``documentSection.largeDocumentModule.largeDocs`` joined
   with extracted document metadata (``sha256``, printed version label,
   printed version date).
3. Linked publications from ``protocolSection.referencesModule``,
   ``resultsSection.moreInfoModule``, PubMed (``src.pubmed_fetcher`` compatible
   via ``PublicationProvider``), and Europe PMC — keeping ``AUTO_INDEXED`` and
   ``INVESTIGATOR_SUBMITTED`` link origins strictly separate.

Runtime dependencies:
- Python 3.11+ standard library (``calendar``, ``datetime``, ``re``,
  ``typing``) plus ``src.citation_verifier``.
- Performs zero network I/O unless an explicit ``publication_provider`` is
  injected by the caller (e.g. ``src.pubmed_fetcher.PubMedFetcher``, which
  depends on ``requests`` in ``requirements.txt``).

Key invariants:
- Strict ASCII NCT ID gate (``validate_strict_nct_id``) runs FIRST on study and
  side-stream records before any holdout check or provider call.
- Hard ``in_audit`` holdout firewall: ``enforce_audit_holdout`` requires
  explicit ``bool`` ``in_audit`` (or verified ``in_scope_nct_ids``) and
  non-empty ``audit_nct_ids`` by default before touching any provider or
  document metadata. A study's own ``in_audit`` can refuse it but never stands
  in for the caller's flag or scope. Every ID field of the study is one of its
  own (``nctId``, ``nct_id``, the classic API's ``NCTId``) or an obsolete
  registration of the same trial (``nctIdAliases``, refused when it is an audit
  ID), and no key or string of the study or of a publication record may name
  an audit trial (``_scan_strings_for_audit_nct_tokens``). Extracted documents
  are document rows: each must name the study in ``nct_id`` (CT.gov large-doc
  filenames are generic, so a filename alone ties a row to no trial), and any
  other trial they name follows the derived-ID rule, and two rows for one
  filename raise rather than the later one winning.
- A ``largeDocs`` entry's ``hasProtocol``/``hasSap`` must be bools: a missing,
  ``null`` or string flag raises, so a document of unstated type is never
  counted as a standalone ICF or typed by truthiness.
- ``to_outcomes_layout_provenance`` takes the same firewall basis
  (``in_audit`` or ``in_scope_nct_ids``, plus ``audit_nct_ids``) as assembly; a
  record read back from disk carries no proof of where it came from.
- Never fabricates ``-01`` day suffixes on partial ``YYYY-MM`` or ``YYYY``
  dates: ``exact_days`` is populated ONLY when both endpoints have
  ``date_precision == "day"``; otherwise exact calendar bounds
  ``[min_days, max_days]`` are emitted with leap-year awareness.
- Strictly factual language only: zero evaluative, spin, outcome-switching, or
  regulatory-compliance claims.
- An explicit DOI field is used only when, normalised, it is a DOI (it fully
  matches ``_EXPLICIT_DOI_RE``); a value that is not (a placeholder such as ``"N/A"``,
  a ``"doi:"`` label with nothing after it) is dropped and counted in the
  record's ``dropped_malformed_doi_count``, so its absence is not read as "no
  DOI given".
"""
from __future__ import annotations

import calendar
from datetime import date
import re
from typing import (
    AbstractSet,
    Any,
    Final,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypedDict,
    runtime_checkable,
)

from src.citation_verifier import (
    _MISSING,
    _prescan_nested_nct_fields,
    _refuse_own_in_audit_flags,
    _scan_record_strings_for_nct_tokens,
    _scan_strings_for_audit_nct_tokens,
    _validate_nct_set,
    _validate_nested_firewall_metadata,
    AuditHoldoutViolationError,
    CitationVerificationError,
    InvalidNctIdError,
    enforce_audit_holdout,
    validate_strict_nct_id,
)

__version__: Final[str] = "1.5.0"
LONGITUDINAL_SCHEMA_VERSION: Final[int] = 1
PROVENANCE_SCHEMA_VERSION: Final[int] = LONGITUDINAL_SCHEMA_VERSION
# 1.2.0: identification fields and audit tokens firewalled, extracted documents must name
# the study, and the layout projection takes a firewall basis.
# 1.3.0: largeDocs flags must be bools (missing, null or string flags raise instead of
# typing a document as an ICF), two extracted rows for one filename raise, audit tokens are
# read in their loose forms, and the holdout flag under any spelling refuses.
# 1.4.0: publications whose PMIDs or DOIs conflict raise instead of merging by record order;
# PMIDs must be ASCII digits; malformed largeDocs (not a list, an entry not an object, a
# protocol or SAP without a filename) and extracted documents without a filename or a
# vocabulary doc_type raise instead of being dropped; audit IDs split across a key and its
# value, padded or labelled ("NCT No.") are refused.
# 1.5.0: an explicit DOI must fully match the DOI grammar (_EXPLICIT_DOI_RE) once normalised; a value
# that does not ("N/A", a bare "doi:" label) is dropped, not recorded or linked as a DOI, and
# counted in the record's dropped_malformed_doi_count (new record key, not projected).
PROVENANCE_RULES_VERSION: Final[str] = "1.5.0"

DatePrecision = Literal["day", "month", "year", "unknown"]
DocType = Literal["protocol", "sap", "combined"]
# The ``doc_type`` vocabulary of extracted document rows (``documents.jsonl``), the same as
# ``src.citation_verifier.DOC_TYPES``.
_EXTRACTED_DOC_TYPES: Final[tuple[DocType, ...]] = ("protocol", "sap", "combined")
LinkOrigin = Literal["AUTO_INDEXED", "INVESTIGATOR_SUBMITTED"]

LINK_ORIGINS: Final[tuple[LinkOrigin, ...]] = (
    "AUTO_INDEXED",
    "INVESTIGATOR_SUBMITTED",
)
PRECISION_RANK: Final[dict[DatePrecision, int]] = {
    "unknown": 0,
    "year": 1,
    "month": 2,
    "day": 3,
}
_RANK_TO_PRECISION: Final[dict[int, DatePrecision]] = {
    0: "unknown",
    1: "year",
    2: "month",
    3: "day",
}

_MONTH_MAP: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_ISO_DAY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$"
)
_ISO_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})$")
_COMPACT_YMD_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_CTGOV_UPLOAD_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?$",
    re.IGNORECASE,
)
_DMY_MONTH_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/]*([A-Za-z]{3,9})[\s\-/,]*(\d{4})$",
    re.IGNORECASE,
)
_MDY_MONTH_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3,9})[\s\-/]+(\d{1,2})(?:st|nd|rd|th)?[\s,/\-]+(\d{4})$",
    re.IGNORECASE,
)
_MY_MONTH_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3,9})[\s\-/,]+(\d{4})$",
    re.IGNORECASE,
)
_PUBMED_YMD_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4})\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:\b.*)?$",
    re.IGNORECASE,
)
_PUBMED_MONTH_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4})\s+(?:[A-Za-z]{3,9}\s*[-/]\s*[A-Za-z]{3,9}|spring|summer|fall|autumn|winter)\b",
    re.IGNORECASE,
)
_PUBMED_YM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4})\s+([A-Za-z]{3,9})(?:\s|$)",
    re.IGNORECASE,
)
_PUBMED_Y_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})\b")
_ALL_NUMERIC_SLASH_DASH_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$"
)
_MULTI_DATE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\s(?:/|;|and|&)\s|;")
_YEAR_IN_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"\b((?:19|20)\d{2})\b")
_DOI_RE: Final[re.Pattern[str]] = re.compile(r"\b(10\.\d{4,9}/[^\s;\"<>]+)")
# An explicit DOI field holds one DOI and nothing else, so its suffix is any run of
# non-whitespace characters: the DOI syntax allows ";", "<" and ">" (old SICI-style
# DOIs such as 10.1002/(SICI)...<...>3.0.CO;2-X), which _DOI_RE excludes only because
# it must find where a DOI ends inside running text. Registrant digits are ASCII.
_EXPLICIT_DOI_RE: Final[re.Pattern[str]] = re.compile(r"10\.[0-9]{4,9}/\S+")


class ParsedDate(TypedDict):
    """Calendar-interval representation of an exact or partial source date."""

    raw: str | None
    iso_normalized: str | None
    date_precision: DatePrecision
    earliest_iso: str | None
    latest_iso: str | None
    parse_note: str | None


class DateDelta(TypedDict):
    """Signed day-interval difference between two ``ParsedDate`` milestones."""

    exact_days: int | None
    min_days: int | None
    max_days: int | None
    start_precision: DatePrecision
    end_precision: DatePrecision
    date_precision: DatePrecision


class RegistryMilestone(TypedDict):
    """Single registry status date preserving ACTUAL vs ESTIMATED type."""

    date: ParsedDate
    date_type: str | None


class DocumentVersionMilestone(TypedDict):
    """Factual version and upload-timing provenance for an in-scope PDF."""

    filename: str
    doc_type: DocType
    sha256: str | None
    registry_label: str | None
    version_label: str | None
    version_date_as_printed: ParsedDate
    registry_doc_date: ParsedDate
    upload_date: ParsedDate
    days_from_doc_upload_to_results: int | None
    doc_upload_to_results_delta: DateDelta
    days_from_primary_completion_to_doc_upload: int | None
    primary_completion_to_doc_upload_delta: DateDelta


class LinkedPublicationMilestone(TypedDict):
    """Deduplicated linked publication preserving link origin and source dates."""

    pmid: str | None
    pmcid: str | None
    doi: str | None
    title: str | None
    authors: str | None
    journal: str | None
    pub_date: ParsedDate
    source_raw_dates: dict[str, str]
    registry_reference_types: list[str]
    link_origins: list[str]
    sources: list[str]
    citation_text: str | None
    is_open_access: bool | None
    days_from_primary_completion_to_pub: int | None
    primary_completion_to_pub_delta: DateDelta
    days_from_results_post_to_pub: int | None
    results_post_to_pub_delta: DateDelta


class LongitudinalProvenanceRecord(TypedDict):
    """Unified per-trial longitudinal provenance record."""

    nct_id: str
    provenance_schema_version: int
    provenance_rules_version: str
    assembled_at: str | None
    source_pipeline_commit: str | None
    registry_milestones: dict[
        Literal[
            "study_first_post_date",
            "primary_completion_date",
            "results_first_post_date",
            "last_update_post_date",
        ],
        RegistryMilestone,
    ]
    days_from_primary_completion_to_results: int | None
    primary_completion_to_results_delta: DateDelta
    large_docs: list[DocumentVersionMilestone]
    excluded_standalone_icf_count: int
    linked_publications: list[LinkedPublicationMilestone]
    # Explicit DOI values (registry reference, PubMed or Europe PMC ``doi`` fields) that
    # were given but are not DOIs and were dropped (``_clean_doi``). A record's own fact,
    # like ``excluded_standalone_icf_count``; ``to_outcomes_layout_provenance`` does not
    # project it, so the layout's key set is unchanged.
    dropped_malformed_doi_count: int


@runtime_checkable
class PublicationProvider(Protocol):
    """Structurally satisfied by ``src.pubmed_fetcher.PubMedFetcher``."""

    def get_publications_for_study(self, nct_id: str) -> Sequence[Mapping[str, Any]]:
        ...


def _unknown_date(raw: str | None, *, parse_note: str | None = None) -> ParsedDate:
    return ParsedDate(
        raw=raw,
        iso_normalized=None,
        date_precision="unknown",
        earliest_iso=None,
        latest_iso=None,
        parse_note=parse_note,
    )


def _make_day(raw: str, year: int, month: int, day: int, *, parse_note: str | None = None) -> ParsedDate:
    try:
        dt = date(year, month, day)
    except ValueError:
        return _unknown_date(raw, parse_note="invalid_calendar_date")
    iso = dt.isoformat()
    return ParsedDate(
        raw=raw,
        iso_normalized=iso,
        date_precision="day",
        earliest_iso=iso,
        latest_iso=iso,
        parse_note=parse_note,
    )


def _make_month(raw: str, year: int, month: int, *, parse_note: str | None = None) -> ParsedDate:
    if not (1 <= month <= 12) or not (1800 <= year <= 2200):
        return _unknown_date(raw, parse_note="invalid_calendar_date")
    last_day = calendar.monthrange(year, month)[1]
    return ParsedDate(
        raw=raw,
        iso_normalized=f"{year:04d}-{month:02d}",
        date_precision="month",
        earliest_iso=f"{year:04d}-{month:02d}-01",
        latest_iso=f"{year:04d}-{month:02d}-{last_day:02d}",
        parse_note=parse_note,
    )


def _make_year(raw: str, year: int, *, parse_note: str | None = None) -> ParsedDate:
    if not (1800 <= year <= 2200):
        return _unknown_date(raw, parse_note="invalid_calendar_date")
    return ParsedDate(
        raw=raw,
        iso_normalized=f"{year:04d}",
        date_precision="year",
        earliest_iso=f"{year:04d}-01-01",
        latest_iso=f"{year:04d}-12-31",
        parse_note=parse_note,
    )


def parse_iso_or_partial_date(raw: str | None) -> ParsedDate:
    """Parse ISO ``YYYY-MM-DD``, ``YYYY-MM``, or ``YYYY`` without fabricating ``-01``."""
    if raw is None:
        return _unknown_date(None)
    text = raw.strip()
    if not text:
        return _unknown_date(raw)

    m = _ISO_DAY_RE.match(text)
    if m:
        return _make_day(raw, int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _ISO_MONTH_RE.match(text)
    if m:
        return _make_month(raw, int(m.group(1)), int(m.group(2)))

    m = _ISO_YEAR_RE.match(text)
    if m:
        return _make_year(raw, int(m.group(1)))

    return _unknown_date(raw, parse_note="unrecognized_iso_date")


def parse_ctgov_upload_date(raw: str | None) -> ParsedDate:
    """Parse ClinicalTrials.gov ``largeDocs[].uploadDate`` (ISO or ``MM/DD/YYYY HH:MM``)."""
    if raw is None:
        return _unknown_date(None)
    text = raw.strip()
    if not text:
        return _unknown_date(raw)

    iso_parsed = parse_iso_or_partial_date(raw)
    if iso_parsed["date_precision"] != "unknown":
        return iso_parsed

    m = _CTGOV_UPLOAD_RE.match(text)
    if m:
        # ClinicalTrials.gov API v2 uploadDate is always US-ordered MM/DD/YYYY HH:MM.
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _make_day(raw, year, month, day)

    return _unknown_date(raw, parse_note="unrecognized_ctgov_upload_date")


def parse_printed_doc_date(raw: str | None) -> ParsedDate:
    """Parse sponsor cover-page document dates while preserving ambiguity explicitly."""
    if raw is None:
        return _unknown_date(None)
    text = " ".join(raw.strip().split())
    if not text:
        return _unknown_date(raw)

    # Detect compound multi-version date strings such as "16-Mar-2021 / 6-May-2021".
    if _MULTI_DATE_SPLIT_RE.search(text) and len(_YEAR_IN_TEXT_RE.findall(text)) >= 2:
        return _unknown_date(raw, parse_note="multiple_dates_in_field")

    iso_parsed = parse_iso_or_partial_date(raw)
    if iso_parsed["date_precision"] != "unknown":
        return iso_parsed

    m = _COMPACT_YMD_RE.match(text)
    if m:
        return _make_day(raw, int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _DMY_MONTH_NAME_RE.match(text)
    if m:
        month = _MONTH_MAP.get(m.group(2).lower())
        if month is not None:
            return _make_day(raw, int(m.group(3)), month, int(m.group(1)))

    m = _MDY_MONTH_NAME_RE.match(text)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        if month is not None:
            return _make_day(raw, int(m.group(3)), month, int(m.group(2)))

    m = _MY_MONTH_NAME_RE.match(text)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        if month is not None:
            return _make_month(raw, int(m.group(2)), month)

    m = _ALL_NUMERIC_SLASH_DASH_RE.match(text)
    if m:
        first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if first == second and 1 <= first <= 12:
            return _make_day(raw, year, first, second)
        if first > 12 and 1 <= second <= 12:
            return _make_day(raw, year, second, first)
        if second > 12 and 1 <= first <= 12:
            return _make_day(raw, year, first, second)
        if 1 <= first <= 12 and 1 <= second <= 12:
            # International sponsor cover pages mix DD/MM/YYYY and MM/DD/YYYY;
            # never guess which order was intended.
            return _make_year(raw, year, parse_note="ambiguous_numeric_month_day")

    return _unknown_date(raw, parse_note="unrecognized_printed_date")


def parse_pubmed_date(raw: str | None) -> ParsedDate:
    """Parse PubMed ESummary ``PubDate`` or Europe PMC ``firstPublicationDate``."""
    if raw is None:
        return _unknown_date(None)
    text = " ".join(raw.strip().split())
    if not text:
        return _unknown_date(raw)

    iso_parsed = parse_iso_or_partial_date(raw)
    if iso_parsed["date_precision"] != "unknown":
        return iso_parsed

    m = _PUBMED_MONTH_RANGE_RE.match(text)
    if m:
        return _make_year(raw, int(m.group(1)), parse_note="season_or_month_range")

    m = _PUBMED_YMD_RE.match(text)
    if m:
        month = _MONTH_MAP.get(m.group(2).lower())
        if month is not None:
            return _make_day(raw, int(m.group(1)), month, int(m.group(3)))

    m = _PUBMED_YM_RE.match(text)
    if m:
        month = _MONTH_MAP.get(m.group(2).lower())
        if month is not None:
            return _make_month(raw, int(m.group(1)), month)

    m = _PUBMED_Y_RE.match(text)
    if m:
        return _make_year(raw, int(m.group(1)))

    return _unknown_date(raw, parse_note="unrecognized_pubmed_date")


def parse_partial_date(
    raw: str | None,
    *,
    source_hint: Literal["auto", "iso", "ctgov_upload", "printed", "pubmed"] = "auto",
) -> ParsedDate:
    """Unified partial-date parser preserving precision without fabricating ``-01``."""
    if source_hint == "iso":
        return parse_iso_or_partial_date(raw)
    if source_hint == "ctgov_upload":
        return parse_ctgov_upload_date(raw)
    if source_hint == "printed":
        return parse_printed_doc_date(raw)
    if source_hint == "pubmed":
        return parse_pubmed_date(raw)

    # Auto dispatch: ISO -> CT.gov upload -> PubMed -> printed cover format.
    if raw is None or not raw.strip():
        return _unknown_date(raw)
    for parser in (
        parse_iso_or_partial_date,
        parse_ctgov_upload_date,
        parse_pubmed_date,
        parse_printed_doc_date,
    ):
        parsed = parser(raw)
        if parsed["date_precision"] != "unknown" or parsed["parse_note"] in {
            "multiple_dates_in_field",
            "ambiguous_numeric_month_day",
            "season_or_month_range",
        }:
            return parsed
    return _unknown_date(raw, parse_note="unrecognized_date")


def compute_day_delta_interval(start: ParsedDate, end: ParsedDate) -> DateDelta:
    """Compute signed ``(end - start)`` day delta interval without day fabrication.

    - ``exact_days`` is populated ONLY when both endpoints have
      ``date_precision == "day"`` (so partial dates never yield fabricated
      exact day deltas).
    - ``min_days = (end.earliest - start.latest).days`` and
      ``max_days = (end.latest - start.earliest).days`` when neither endpoint
      is ``"unknown"``.
    - ``date_precision = min(start_precision, end_precision)`` under
      ``unknown < year < month < day``.
    """
    start_prec: DatePrecision = start["date_precision"]
    end_prec: DatePrecision = end["date_precision"]
    combined_prec: DatePrecision = _RANK_TO_PRECISION[
        min(PRECISION_RANK[start_prec], PRECISION_RANK[end_prec])
    ]

    if (
        start_prec == "unknown"
        or end_prec == "unknown"
        or start["earliest_iso"] is None
        or start["latest_iso"] is None
        or end["earliest_iso"] is None
        or end["latest_iso"] is None
    ):
        return DateDelta(
            exact_days=None,
            min_days=None,
            max_days=None,
            start_precision=start_prec,
            end_precision=end_prec,
            date_precision="unknown",
        )

    start_early = date.fromisoformat(start["earliest_iso"])
    start_late = date.fromisoformat(start["latest_iso"])
    end_early = date.fromisoformat(end["earliest_iso"])
    end_late = date.fromisoformat(end["latest_iso"])

    min_days = (end_early - start_late).days
    max_days = (end_late - start_early).days
    exact_days: int | None = min_days if (start_prec == "day" and end_prec == "day") else None

    return DateDelta(
        exact_days=exact_days,
        min_days=min_days,
        max_days=max_days,
        start_precision=start_prec,
        end_precision=end_prec,
        date_precision=combined_prec,
    )


compute_date_delta = compute_day_delta_interval


def _extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    m = _DOI_RE.search(text)
    if not m:
        return None
    return m.group(1).rstrip(".,;)").lower()


def _normalize_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


_ASCII_PMID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+", re.ASCII)


def _clean_pmid(value: Any, *, context: str = "pmid") -> str | None:
    """Return a PMID as ASCII digits, ``None`` when none is given, or raise.

    Method: PubMed issues PMIDs as ASCII decimal digits. ``str.isdigit`` also
    accepts fullwidth (``４０００００１``), superscript and other scripts'
    digits, which would be recorded as an identifier PubMed never issued and
    would never join the same publication's ASCII PMID, so only ASCII digits
    pass. ``None``, an empty or blank string is absence (no PMID). Any other
    value (non-ASCII digits, letters, a bool) is refused rather than recorded
    or dropped as if no PMID had been given.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CitationVerificationError(
            f"{context} must be a string of ASCII digits, got {type(value).__name__}."
        )
    s = str(value).strip()
    if not s:
        return None
    if _ASCII_PMID_RE.fullmatch(s) is None:
        raise CitationVerificationError(
            f"{context} {s!r} is not a PMID: PMIDs are ASCII decimal digits."
        )
    return s


def _clean_doi(value: Any) -> tuple[str | None, bool]:
    """Read an explicit DOI field: ``(doi, dropped)``.

    Method: ``None``, an empty or a blank string is absence (no DOI given):
    ``(None, False)``. Any other value is normalised as before (resolver and
    ``doi:`` label prefixes removed, trailing ``.,;)`` stripped, lowercased)
    and must then fully match ``_EXPLICIT_DOI_RE``: ``10.``, a 4-9 ASCII-digit
    registrant code, ``/`` and a non-empty suffix with no whitespace. A value that
    does not (``"N/A"``, ``"none"``, ``"doi:"`` with nothing after the label,
    a number, a bool) is not a DOI: it is not used (it would otherwise be
    recorded as the DOI ``"n/a"`` and, alone, link a publication) and is
    reported as ``(None, True)`` so the caller counts it rather than reading
    the absence as "no DOI given". Unlike ``_DOI_RE`` (which must find where
    a DOI ends inside running text), the suffix may hold ``;``, ``<`` and
    ``>``, so an old SICI-style DOI given in a DOI field is kept whole.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    s = value.strip()
    if not s:
        return None, False
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :].strip()
    s = s.rstrip(".,;)").lower()
    if _EXPLICIT_DOI_RE.fullmatch(s) is None:
        return None, True
    return s, False


def _doc_type_from_flags(has_protocol: bool, has_sap: bool) -> DocType | None:
    if has_protocol and has_sap:
        return "combined"
    if has_protocol:
        return "protocol"
    if has_sap:
        return "sap"
    return None


def _require_large_doc_flag(raw_doc: Mapping[str, Any], key: str, *, doc_idx: int) -> bool:
    """Return a ``largeDocs`` entry's ``hasProtocol``/``hasSap`` flag, which must be a bool.

    Method: the flags decide whether a posted document is a protocol, a SAP,
    both, or a standalone ICF that is counted and left out of ``large_docs``.
    A missing or ``null`` flag says nothing, and reading it as False would
    count a document of unstated type as an ICF and drop it; a string such as
    ``"false"`` is not a flag, and its truthiness would make it True. Either
    stops assembly with the entry's position, so absence stays absence rather
    than a guessed type (the registry's API v2 states both flags as booleans).
    """
    if key not in raw_doc:
        raise CitationVerificationError(
            f"study.documentSection.largeDocumentModule.largeDocs[{doc_idx}] has no {key!r}; "
            f"the document's type is not stated, so it is neither typed nor counted as an ICF."
        )
    flag = raw_doc[key]
    if type(flag) is not bool:
        raise CitationVerificationError(
            f"study.documentSection.largeDocumentModule.largeDocs[{doc_idx}].{key} is "
            f"{type(flag).__name__}, not a bool; the document's type is not stated."
        )
    return flag


def _choose_higher_precision_date(current: ParsedDate, candidate: ParsedDate) -> ParsedDate:
    if PRECISION_RANK[candidate["date_precision"]] > PRECISION_RANK[current["date_precision"]]:
        return candidate
    return current


def _extract_registry_milestones(
    study: Mapping[str, Any],
) -> dict[
    Literal[
        "study_first_post_date",
        "primary_completion_date",
        "results_first_post_date",
        "last_update_post_date",
    ],
    RegistryMilestone,
]:
    protocol = study.get("protocolSection")
    status_mod = protocol.get("statusModule", {}) if isinstance(protocol, Mapping) else {}
    if not isinstance(status_mod, Mapping):
        status_mod = {}

    def _read_struct(key: str) -> RegistryMilestone:
        struct = status_mod.get(key)
        if not isinstance(struct, Mapping):
            return RegistryMilestone(date=_unknown_date(None), date_type=None)
        raw_date = struct.get("date")
        raw_str = raw_date if isinstance(raw_date, str) else None
        date_type = _normalize_optional_str(struct.get("type"))
        return RegistryMilestone(
            date=parse_iso_or_partial_date(raw_str),
            date_type=date_type,
        )

    return {
        "study_first_post_date": _read_struct("studyFirstPostDateStruct"),
        "primary_completion_date": _read_struct("primaryCompletionDateStruct"),
        "results_first_post_date": _read_struct("resultsFirstPostDateStruct"),
        "last_update_post_date": _read_struct("lastUpdatePostDateStruct"),
    }


def _extract_large_docs(
    study: Mapping[str, Any],
    extracted_documents: Sequence[Mapping[str, Any]] | None,
    primary_completion_date: ParsedDate,
    results_first_post_date: ParsedDate,
) -> tuple[list[DocumentVersionMilestone], int]:
    """Return the study's posted protocol/SAP documents and its count of standalone ICFs.

    Method: absence is recorded or refused, never dropped. A study with no
    ``documentSection``, ``largeDocumentModule`` or ``largeDocs`` posted no
    large documents (the registry omits empty modules). A level that is
    present but of the wrong type (``largeDocs`` as an object, a module that
    is a list), a ``largeDocs`` entry that is not an object, and a protocol or
    SAP entry without a filename cannot be read as documents, and dropping
    them would make the record say the trial posted fewer documents than it
    did, so each raises. An extracted document must name its file; a
    ``doc_type`` it carries must be of the extracted-row vocabulary
    (``protocol``, ``sap``, ``combined``; ``_EXTRACTED_DOC_TYPES``), and one not
    joined to a ``largeDocs`` entry (whose flags type the joined ones) must
    carry one. A row that cannot be typed is refused rather than skipped.
    """
    doc_section = study.get("documentSection", {})
    if not isinstance(doc_section, Mapping):
        raise CitationVerificationError(
            f"study.documentSection must be an object, got {type(doc_section).__name__}."
        )
    large_doc_mod = doc_section.get("largeDocumentModule", {})
    if not isinstance(large_doc_mod, Mapping):
        raise CitationVerificationError(
            f"study.documentSection.largeDocumentModule must be an object, got "
            f"{type(large_doc_mod).__name__}."
        )
    raw_docs = large_doc_mod.get("largeDocs", [])
    if not isinstance(raw_docs, list):
        raise CitationVerificationError(
            f"study.documentSection.largeDocumentModule.largeDocs must be a list, got "
            f"{type(raw_docs).__name__}; its documents cannot be read."
        )

    # Extracted rows are joined to largeDocs by filename. Two rows for one filename (two
    # sha256 values, two version labels) leave no way to tell which is the posted file, so
    # assembly stops, as verify_census_corpus reports DUPLICATE_DOCUMENT_RECORD, instead of
    # letting the later row win.
    extracted_by_filename: dict[str, Mapping[str, Any]] = {}
    for ext_idx, ext_doc in enumerate(extracted_documents or ()):
        fn = _normalize_optional_str(ext_doc.get("filename"))
        if fn is None:
            raise CitationVerificationError(
                f"extracted_documents[{ext_idx}] names no file (filename missing or empty); "
                f"it cannot be joined to a posted document and is not dropped."
            )
        if "doc_type" in ext_doc and ext_doc["doc_type"] not in _EXTRACTED_DOC_TYPES:
            raise CitationVerificationError(
                f"extracted_documents[{ext_idx}] ({fn!r}) has doc_type {ext_doc['doc_type']!r}; "
                f"expected one of {_EXTRACTED_DOC_TYPES}, so the document cannot be typed."
            )
        if fn in extracted_by_filename:
            raise CitationVerificationError(
                f"extracted_documents[{ext_idx}] is a second row for filename {fn!r}; "
                f"which document version the study posted is not determined."
            )
        extracted_by_filename[fn] = ext_doc

    milestones: list[DocumentVersionMilestone] = []
    excluded_icf_count = 0
    seen_filenames: set[str] = set()

    for doc_idx, raw_doc in enumerate(raw_docs):
        if not isinstance(raw_doc, Mapping):
            raise CitationVerificationError(
                f"study.documentSection.largeDocumentModule.largeDocs[{doc_idx}] is "
                f"{type(raw_doc).__name__}, not an object; the posted document cannot be read."
            )
        has_protocol = _require_large_doc_flag(raw_doc, "hasProtocol", doc_idx=doc_idx)
        has_sap = _require_large_doc_flag(raw_doc, "hasSap", doc_idx=doc_idx)
        doc_type = _doc_type_from_flags(has_protocol, has_sap)
        if doc_type is None:
            # Standalone ICF (hasProtocol=False, hasSap=False) excluded from large_docs.
            excluded_icf_count += 1
            continue

        filename = _normalize_optional_str(raw_doc.get("filename"))
        if filename is None:
            raise CitationVerificationError(
                f"study.documentSection.largeDocumentModule.largeDocs[{doc_idx}] is a "
                f"{doc_type} document with no filename; it cannot be recorded and is not "
                f"dropped."
            )
        seen_filenames.add(filename)

        ext = extracted_by_filename.get(filename, {})
        sha256 = _normalize_optional_str(ext.get("sha256"))
        version_label = _normalize_optional_str(ext.get("version_label"))
        printed_raw = ext.get("version_date_as_printed")
        printed_str = printed_raw if isinstance(printed_raw, str) else None
        version_date_as_printed = parse_printed_doc_date(printed_str)

        registry_doc_date_raw = raw_doc.get("date")
        registry_doc_date = parse_iso_or_partial_date(
            registry_doc_date_raw if isinstance(registry_doc_date_raw, str) else None
        )
        upload_raw = raw_doc.get("uploadDate")
        upload_date = parse_ctgov_upload_date(
            upload_raw if isinstance(upload_raw, str) else None
        )

        # Signed deltas:
        #   doc_upload_to_results = results_first_post - upload_date
        #   primary_completion_to_doc_upload = upload_date - primary_completion
        upload_to_results = compute_day_delta_interval(upload_date, results_first_post_date)
        pc_to_upload = compute_day_delta_interval(primary_completion_date, upload_date)

        milestones.append(
            DocumentVersionMilestone(
                filename=filename,
                doc_type=doc_type,
                sha256=sha256,
                registry_label=_normalize_optional_str(raw_doc.get("label")),
                version_label=version_label,
                version_date_as_printed=version_date_as_printed,
                registry_doc_date=registry_doc_date,
                upload_date=upload_date,
                days_from_doc_upload_to_results=upload_to_results["exact_days"],
                doc_upload_to_results_delta=upload_to_results,
                days_from_primary_completion_to_doc_upload=pc_to_upload["exact_days"],
                primary_completion_to_doc_upload_delta=pc_to_upload,
            )
        )

    # Also include extracted_documents not present in study.documentSection
    # (e.g., offline records constructed from documents.jsonl alone).
    for fn, ext in sorted(extracted_by_filename.items()):
        if fn in seen_filenames:
            continue
        # A row joined to a largeDocs entry is typed by that entry's flags; a row that is not
        # joined is typed by its own doc_type (checked against _EXTRACTED_DOC_TYPES above when
        # present), and without one it cannot be typed, so it is refused, not skipped.
        if "doc_type" not in ext:
            raise CitationVerificationError(
                f"extracted document {fn!r} is not among the study's largeDocs and has no "
                f"doc_type, so it cannot be typed; it is not dropped."
            )
        doc_type_lit: DocType = ext["doc_type"]
        printed_raw = ext.get("version_date_as_printed")
        version_date_as_printed = parse_printed_doc_date(
            printed_raw if isinstance(printed_raw, str) else None
        )
        upload_raw = ext.get("upload_date")
        upload_date = parse_ctgov_upload_date(
            upload_raw if isinstance(upload_raw, str) else None
        )
        api_doc_raw = ext.get("api_doc_date")
        registry_doc_date = parse_iso_or_partial_date(
            api_doc_raw if isinstance(api_doc_raw, str) else None
        )
        upload_to_results = compute_day_delta_interval(upload_date, results_first_post_date)
        pc_to_upload = compute_day_delta_interval(primary_completion_date, upload_date)
        milestones.append(
            DocumentVersionMilestone(
                filename=fn,
                doc_type=doc_type_lit,
                sha256=_normalize_optional_str(ext.get("sha256")),
                registry_label=None,
                version_label=_normalize_optional_str(ext.get("version_label")),
                version_date_as_printed=version_date_as_printed,
                registry_doc_date=registry_doc_date,
                upload_date=upload_date,
                days_from_doc_upload_to_results=upload_to_results["exact_days"],
                doc_upload_to_results_delta=upload_to_results,
                days_from_primary_completion_to_doc_upload=pc_to_upload["exact_days"],
                primary_completion_to_doc_upload_delta=pc_to_upload,
            )
        )

    milestones.sort(key=lambda item: item["filename"])
    return milestones, excluded_icf_count


def _extract_and_merge_publications(
    study: Mapping[str, Any],
    *,
    pubmed_records: Sequence[Mapping[str, Any]] | None,
    europe_pmc_records: Sequence[Mapping[str, Any]] | None,
    primary_completion_date: ParsedDate,
    results_first_post_date: ParsedDate,
) -> tuple[list[LinkedPublicationMilestone], int]:
    """Merge registry references, PubMed, and Europe PMC keeping link origins separate.

    Returns the linked publications and the number of explicit DOI values
    dropped as not DOIs (``_clean_doi``). A record whose explicit DOI is
    dropped falls back to a DOI in its citation text, as a record with no
    DOI does; a record left with no PMID, DOI or citation links nothing.
    """
    buckets: dict[str, dict[str, Any]] = {}
    doi_to_key: dict[str, str] = {}
    pmid_to_key: dict[str, str] = {}
    citation_to_key: dict[str, str] = {}
    dropped_malformed_dois = 0

    def _explicit_doi(value: Any) -> str | None:
        """Return the DOI an explicit field gives, counting a value that is not a DOI."""
        nonlocal dropped_malformed_dois
        doi, dropped = _clean_doi(value)
        if dropped:
            dropped_malformed_dois += 1
        return doi

    def _resolve_bucket(
        pmid: str | None,
        doi: str | None,
        citation_text: str | None,
    ) -> dict[str, Any]:
        """Return the publication a record belongs to, refusing to merge conflicting IDs.

        Method: a record joins an existing publication by PMID, else DOI, else
        citation text. Every identifier the record carries must agree with the
        publication it joins: a PMID and a DOI pointing at two different
        publications, or a joined publication holding another PMID (or DOI)
        than the record's, means the sources disagree about which paper this
        is. Keeping the first value would drop the other identifier (and the
        record's title) without trace, so assembly stops instead, naming both
        identifiers.
        """
        keys_by_id: dict[str, str] = {}
        if pmid and pmid in pmid_to_key:
            keys_by_id[f"PMID {pmid}"] = pmid_to_key[pmid]
        if doi and doi in doi_to_key:
            keys_by_id[f"DOI {doi}"] = doi_to_key[doi]
        if len(set(keys_by_id.values())) > 1:
            raise CitationVerificationError(
                f"A publication record's identifiers ({', '.join(sorted(keys_by_id))}) belong to "
                f"two different publications already linked to the study; which paper it is "
                f"is not determined."
            )
        existing_key: str | None = next(iter(keys_by_id.values()), None)
        if existing_key is None and citation_text:
            norm_cit = " ".join(citation_text.lower().split())
            existing_key = citation_to_key.get(norm_cit)
        if existing_key is not None:
            joined = buckets[existing_key]
            for label, mine, theirs in (("PMID", pmid, joined["pmid"]), ("DOI", doi, joined["doi"])):
                if mine and theirs and mine != theirs:
                    raise CitationVerificationError(
                        f"A publication record with {label} {mine!r} matches a linked "
                        f"publication whose {label} is {theirs!r}; the sources disagree about "
                        f"which paper this is, so neither identifier is dropped by record order."
                    )

        if existing_key is None:
            if pmid:
                existing_key = f"pmid:{pmid}"
            elif doi:
                existing_key = f"doi:{doi}"
            else:
                norm_cit = " ".join((citation_text or "").lower().split())
                existing_key = f"cit:{norm_cit}"
            buckets[existing_key] = {
                "pmid": pmid,
                "pmcid": None,
                "doi": doi,
                "title": None,
                "authors": None,
                "journal": None,
                "pub_date": _unknown_date(None),
                "source_raw_dates": {},
                "registry_reference_types": set(),
                "link_origins": set(),
                "sources": set(),
                "citation_text": citation_text,
                "is_open_access": None,
            }

        bucket = buckets[existing_key]
        if pmid:
            bucket["pmid"] = bucket["pmid"] or pmid
            pmid_to_key[pmid] = existing_key
        if doi:
            bucket["doi"] = bucket["doi"] or doi
            doi_to_key[doi] = existing_key
        if citation_text:
            norm_cit = " ".join(citation_text.lower().split())
            citation_to_key[norm_cit] = existing_key
            if not bucket["citation_text"]:
                bucket["citation_text"] = citation_text
        return bucket

    def _ingest_entry(
        *,
        source: str,
        link_origin: LinkOrigin,
        pmid: str | None,
        pmcid: str | None,
        doi: str | None,
        title: str | None,
        authors: str | None,
        journal: str | None,
        raw_date: str | None,
        parsed_date: ParsedDate,
        ref_type: str | None,
        citation_text: str | None,
        is_open_access: bool | None,
    ) -> None:
        if not pmid and not doi and not citation_text:
            return
        bucket = _resolve_bucket(pmid, doi, citation_text)
        bucket["sources"].add(source)
        bucket["link_origins"].add(link_origin)
        if ref_type in {"BACKGROUND", "DERIVED", "RESULT"}:
            bucket["registry_reference_types"].add(ref_type)
        if pmcid and not bucket["pmcid"]:
            bucket["pmcid"] = pmcid
        if title and not bucket["title"]:
            bucket["title"] = title
        if authors and not bucket["authors"]:
            bucket["authors"] = authors
        if journal and not bucket["journal"]:
            bucket["journal"] = journal
        if raw_date:
            bucket["source_raw_dates"][source] = raw_date
        bucket["pub_date"] = _choose_higher_precision_date(bucket["pub_date"], parsed_date)
        if is_open_access is not None and bucket["is_open_access"] is None:
            bucket["is_open_access"] = is_open_access

    # 1. protocolSection.referencesModule.references
    protocol = study.get("protocolSection")
    refs_mod = protocol.get("referencesModule", {}) if isinstance(protocol, Mapping) else {}
    raw_refs = refs_mod.get("references", []) if isinstance(refs_mod, Mapping) else []
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            if not isinstance(ref, Mapping):
                continue
            pmid = _clean_pmid(
                ref.get("pmid"), context="study.protocolSection.referencesModule.references[].pmid"
            )
            citation = _normalize_optional_str(ref.get("citation"))
            doi = _explicit_doi(ref.get("doi")) or _extract_doi(citation)
            ref_type = _normalize_optional_str(ref.get("type"))
            if ref_type is not None:
                ref_type = ref_type.upper()
            # ClinicalTrials.gov DERIVED references are NLM auto-indexed from
            # PubMed NCT links; RESULT and BACKGROUND are sponsor/investigator submitted.
            origin: LinkOrigin = (
                "AUTO_INDEXED" if ref_type == "DERIVED" else "INVESTIGATOR_SUBMITTED"
            )
            _ingest_entry(
                source="ctgov_references_module",
                link_origin=origin,
                pmid=pmid,
                pmcid=_normalize_optional_str(ref.get("pmcid")),
                doi=doi,
                title=None,
                authors=None,
                journal=None,
                raw_date=None,
                parsed_date=_unknown_date(None),
                ref_type=ref_type,
                citation_text=citation,
                is_open_access=None,
            )

    # 2. resultsSection.moreInfoModule (citations / references)
    results_sec = study.get("resultsSection")
    more_info = results_sec.get("moreInfoModule", {}) if isinstance(results_sec, Mapping) else {}
    if isinstance(more_info, Mapping):
        for field_name in ("citations", "references"):
            entries = more_info.get(field_name)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, str):
                    citation = _normalize_optional_str(entry)
                    _ingest_entry(
                        source="ctgov_more_info_module",
                        link_origin="INVESTIGATOR_SUBMITTED",
                        pmid=None,
                        pmcid=None,
                        doi=_extract_doi(citation),
                        title=None,
                        authors=None,
                        journal=None,
                        raw_date=None,
                        parsed_date=_unknown_date(None),
                        ref_type=None,
                        citation_text=citation,
                        is_open_access=None,
                    )
                elif isinstance(entry, Mapping):
                    pmid = _clean_pmid(
                        entry.get("pmid"),
                        context=f"study.resultsSection.moreInfoModule.{field_name}[].pmid",
                    )
                    citation = _normalize_optional_str(entry.get("citation"))
                    doi = _explicit_doi(entry.get("doi")) or _extract_doi(citation)
                    ref_type = _normalize_optional_str(entry.get("type"))
                    if ref_type is not None:
                        ref_type = ref_type.upper()
                    _ingest_entry(
                        source="ctgov_more_info_module",
                        link_origin="INVESTIGATOR_SUBMITTED",
                        pmid=pmid,
                        pmcid=_normalize_optional_str(entry.get("pmcid")),
                        doi=doi,
                        title=_normalize_optional_str(entry.get("title")),
                        authors=_normalize_optional_str(entry.get("authors")),
                        journal=_normalize_optional_str(entry.get("journal")),
                        raw_date=None,
                        parsed_date=_unknown_date(None),
                        ref_type=ref_type,
                        citation_text=citation,
                        is_open_access=None,
                    )

    # 3. PubMed records (PubMedFetcher output shape)
    for rec in pubmed_records or ():
        if not isinstance(rec, Mapping):
            continue
        pmid = _clean_pmid(rec.get("pmid"), context="pubmed_records[].pmid")
        citation = _normalize_optional_str(rec.get("citation"))
        doi = _explicit_doi(rec.get("doi")) or _extract_doi(citation)
        raw_pub_date = _normalize_optional_str(rec.get("pub_date"))
        parsed_pub = parse_pubmed_date(raw_pub_date)
        ref_type = _normalize_optional_str(rec.get("type"))
        if ref_type is not None:
            ref_type = ref_type.upper()
        _ingest_entry(
            source="pubmed",
            link_origin="AUTO_INDEXED",
            pmid=pmid,
            pmcid=_normalize_optional_str(rec.get("pmcid")),
            doi=doi,
            title=_normalize_optional_str(rec.get("title")),
            authors=_normalize_optional_str(rec.get("authors")),
            journal=_normalize_optional_str(rec.get("journal")),
            raw_date=raw_pub_date,
            parsed_date=parsed_pub,
            ref_type=ref_type,
            citation_text=citation,
            is_open_access=None,
        )

    # 4. Europe PMC records
    for rec in europe_pmc_records or ():
        if not isinstance(rec, Mapping):
            continue
        pmid = _clean_pmid(rec.get("pmid"), context="europe_pmc_records[].pmid")
        pmcid = _normalize_optional_str(rec.get("pmcid"))
        doi = _explicit_doi(rec.get("doi"))
        raw_epmc_date = _normalize_optional_str(
            rec.get("firstPublicationDate") or rec.get("pub_date") or rec.get("pubYear")
        )
        parsed_epmc = parse_pubmed_date(raw_epmc_date)
        oa_raw = rec.get("isOpenAccess")
        is_oa: bool | None = None
        if isinstance(oa_raw, bool):
            is_oa = oa_raw
        elif isinstance(oa_raw, str):
            upper = oa_raw.strip().upper()
            if upper in {"Y", "YES", "TRUE"}:
                is_oa = True
            elif upper in {"N", "NO", "FALSE"}:
                is_oa = False
        _ingest_entry(
            source="europe_pmc",
            link_origin="AUTO_INDEXED",
            pmid=pmid,
            pmcid=pmcid,
            doi=doi,
            title=_normalize_optional_str(rec.get("title")),
            authors=_normalize_optional_str(rec.get("authorString") or rec.get("authors")),
            journal=_normalize_optional_str(rec.get("journalTitle") or rec.get("journal")),
            raw_date=raw_epmc_date,
            parsed_date=parsed_epmc,
            ref_type=None,
            citation_text=_normalize_optional_str(rec.get("citation")),
            is_open_access=is_oa,
        )

    output: list[LinkedPublicationMilestone] = []
    for bucket in buckets.values():
        pub_date: ParsedDate = bucket["pub_date"]
        pc_to_pub = compute_day_delta_interval(primary_completion_date, pub_date)
        res_to_pub = compute_day_delta_interval(results_first_post_date, pub_date)
        output.append(
            LinkedPublicationMilestone(
                pmid=bucket["pmid"],
                pmcid=bucket["pmcid"],
                doi=bucket["doi"],
                title=bucket["title"],
                authors=bucket["authors"],
                journal=bucket["journal"],
                pub_date=pub_date,
                source_raw_dates=dict(sorted(bucket["source_raw_dates"].items())),
                registry_reference_types=sorted(bucket["registry_reference_types"]),
                link_origins=sorted(bucket["link_origins"]),
                sources=sorted(bucket["sources"]),
                citation_text=bucket["citation_text"],
                is_open_access=bucket["is_open_access"],
                days_from_primary_completion_to_pub=pc_to_pub["exact_days"],
                primary_completion_to_pub_delta=pc_to_pub,
                days_from_results_post_to_pub=res_to_pub["exact_days"],
                results_post_to_pub_delta=res_to_pub,
            )
        )

    output.sort(
        key=lambda item: (
            item["pub_date"]["earliest_iso"] or "9999-99-99",
            item["pmid"] or "",
            item["doi"] or "",
            item["citation_text"] or "",
        )
    )
    return output, dropped_malformed_dois


def _resolve_study_nct_id(study: Mapping[str, Any]) -> tuple[str, list[str], list[str]]:
    """Resolve and validate the study's own NCT ID fields and its NCT aliases.

    Returns ``(nct_id, own_ids, aliases)``. Own-ID fields are
    ``identificationModule.nctId`` / ``nct_id``, top-level ``nct_id`` /
    ``nctId``, and the classic API's top-level ``NCTId`` (a string or a list
    of strings); they must all name one trial. ``identificationModule.nctIdAliases``
    lists obsolete registrations of the same trial (API v2); each must be a
    strict ID, and is returned for the audit check in
    ``assemble_longitudinal_provenance``.
    """
    protocol = study.get("protocolSection")
    id_mod = protocol.get("identificationModule") if isinstance(protocol, Mapping) else None

    candidates: list[tuple[str, str]] = []
    if isinstance(id_mod, Mapping):
        if "nctId" in id_mod:
            candidates.append(
                (
                    "study.protocolSection.identificationModule.nctId",
                    validate_strict_nct_id(
                        id_mod["nctId"],
                        context="study.protocolSection.identificationModule.nctId",
                    ),
                )
            )
        if "nct_id" in id_mod:
            candidates.append(
                (
                    "study.protocolSection.identificationModule.nct_id",
                    validate_strict_nct_id(
                        id_mod["nct_id"],
                        context="study.protocolSection.identificationModule.nct_id",
                    ),
                )
            )
    if "nct_id" in study:
        candidates.append(
            ("study.nct_id", validate_strict_nct_id(study["nct_id"], context="study.nct_id"))
        )
    if "nctId" in study:
        candidates.append(
            ("study.nctId", validate_strict_nct_id(study["nctId"], context="study.nctId"))
        )
    if "NCTId" in study:
        classic = study["NCTId"]
        classic_values = classic if isinstance(classic, list) else [classic]
        if not classic_values:
            raise CitationVerificationError("study.NCTId is an empty list; it names no trial.")
        for idx, value in enumerate(classic_values):
            candidates.append(
                (
                    f"study.NCTId[{idx}]",
                    validate_strict_nct_id(value, context=f"study.NCTId[{idx}]"),
                )
            )

    aliases: list[str] = []
    if isinstance(id_mod, Mapping) and "nctIdAliases" in id_mod:
        raw_aliases = id_mod["nctIdAliases"]
        if not isinstance(raw_aliases, list):
            raise CitationVerificationError(
                f"study.protocolSection.identificationModule.nctIdAliases must be a list, "
                f"got {type(raw_aliases).__name__}."
            )
        for idx, alias in enumerate(raw_aliases):
            aliases.append(
                validate_strict_nct_id(
                    alias,
                    context=f"study.protocolSection.identificationModule.nctIdAliases[{idx}]",
                )
            )

    if not candidates:
        return validate_strict_nct_id(None, context="study.nct_id"), [], aliases
    return candidates[0][1], [val for _, val in candidates], aliases


def _prescan_side_stream(
    records: Sequence[Mapping[str, Any]] | None,
    *,
    stream_name: str,
) -> None:
    if records is None:
        return
    if isinstance(records, (str, bytes, Mapping)) or not isinstance(records, Sequence):
        raise CitationVerificationError(
            f"{stream_name} must be a sequence of mappings, got {type(records).__name__}."
        )
    for idx, rec in enumerate(records):
        if not isinstance(rec, Mapping):
            raise CitationVerificationError(
                f"{stream_name}[{idx}] must be a mapping, got {type(rec).__name__}."
            )
        _prescan_nested_nct_fields(rec, context=f"{stream_name}[{idx}]")


def _validate_side_stream_records(
    records: Sequence[Mapping[str, Any]] | None,
    *,
    stream_name: str,
    expected_nct_id: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    allow_empty_audit_set: bool,
) -> None:
    """Validate strict NCT IDs, ``in_audit`` flags, embedded URLs and audit tokens on side streams.

    Besides the named-key walk, every key and string of each record is read for
    an audit-holdout ID (``_scan_strings_for_audit_nct_tokens``): a publication
    title or citation naming a holdout trial would otherwise be copied into
    the record.
    """
    if records is None:
        return
    _prescan_side_stream(records, stream_name=stream_name)
    for idx, rec in enumerate(records):
        _validate_nested_firewall_metadata(
            rec,
            context=f"{stream_name}[{idx}]",
            expected_nct_id=expected_nct_id,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
        _scan_strings_for_audit_nct_tokens(
            rec, context=f"{stream_name}[{idx}]", audit_nct_ids=audit_nct_ids
        )


def _validate_extracted_documents_name_study(
    records: Sequence[Mapping[str, Any]] | None,
    *,
    nct_id: str,
    audit_nct_ids: frozenset[str],
    in_scope_nct_ids: frozenset[str] | None,
    allow_empty_audit_set: bool,
) -> None:
    """Require every extracted document to name the study, and firewall what else it names.

    Method: ``_extract_large_docs`` joins extracted documents to the study's
    ``largeDocs`` by filename, and ClinicalTrials.gov large-document filenames
    are generic (``Prot_000.pdf``), so a row that names no trial would attach
    its sha256 and version label to whichever study shares the filename. Each
    row must therefore carry ``nct_id`` equal to the study's (a different one
    is already refused by the named-key walk); and, as a document row
    (``src.citation_verifier.verify_document_record`` reads rows the same way),
    every other key and string follows the derived-ID rule.
    """
    if records is None:
        return
    for idx, rec in enumerate(records):
        if "nct_id" not in rec:
            raise CitationVerificationError(
                f"extracted_documents[{idx}] names no trial (no nct_id); a filename alone does "
                f"not tie a document to {nct_id!r}."
            )
        if rec["nct_id"] != nct_id:
            raise AuditHoldoutViolationError(
                f"extracted_documents[{idx}].nct_id={rec['nct_id']!r} is not the study {nct_id!r}."
            )
        _scan_record_strings_for_nct_tokens(
            rec,
            context=f"extracted_documents[{idx}]",
            expected_nct_id=nct_id,
            audit_nct_ids=audit_nct_ids,
            in_scope_nct_ids=in_scope_nct_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )


def assemble_longitudinal_provenance(
    study: Mapping[str, Any],
    *,
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
    extracted_documents: Sequence[Mapping[str, Any]] | None = None,
    pubmed_records: Sequence[Mapping[str, Any]] | None = None,
    europe_pmc_records: Sequence[Mapping[str, Any]] | None = None,
    publication_provider: PublicationProvider | None = None,
    assembled_at: str | None = None,
    source_pipeline_commit: str | None = None,
) -> LongitudinalProvenanceRecord:
    """Assemble factual longitudinal registry, document, and publication provenance.

    Validates all NCT IDs via ``validate_strict_nct_id`` FIRST and enforces
    ``enforce_audit_holdout`` BEFORE invoking any injected
    ``publication_provider`` or reading any document metadata.
    """
    if not isinstance(study, Mapping):
        raise CitationVerificationError(
            f"study must be a mapping, got {type(study).__name__}."
        )

    # Step 1 (F2): Strict ASCII ^NCT[0-9]{8}$ check FIRST across study, sets, and side streams.
    nct_id, all_study_ncts, study_aliases = _resolve_study_nct_id(study)
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    _prescan_nested_nct_fields(study, context="study")
    _prescan_side_stream(extracted_documents, stream_name="extracted_documents")
    _prescan_side_stream(pubmed_records, stream_name="pubmed_records")
    _prescan_side_stream(europe_pmc_records, stream_name="europe_pmc_records")

    # Step 2 (F1): Hard holdout firewall BEFORE any provider call or document parse. The
    # study's own flag, under any spelling (``inAudit``, ``IN_AUDIT``), can only refuse,
    # and the message does not name a trial that says it is held out.
    _refuse_own_in_audit_flags(study, record_label="study")

    # The study's own in_audit (checked above) can only refuse. It never stands in for the
    # caller's in_audit or in_scope_nct_ids, which come from the index: a study record is
    # the data under check, and a stale or edited ``in_audit: false`` on it must not admit
    # a trial the index holds out. ``all_study_ncts`` are the study's own ID fields, all
    # the same trial (a mismatch raises below), so the caller's flag covers each of them.
    for candidate_nct in all_study_ncts:
        enforce_audit_holdout(
            candidate_nct,
            in_audit=in_audit,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )
    if len(set(all_study_ncts)) > 1:
        raise CitationVerificationError(
            f"Mismatched study NCT IDs across fields: {all_study_ncts}."
        )
    # An alias is an obsolete registration of this same trial, so the caller's admission
    # of the study covers it; an alias in the audit set means the study is a holdout
    # trial under another number, and it is refused.
    if any(alias in checked_audit_ids for alias in study_aliases):
        raise AuditHoldoutViolationError(
            "Audit holdout violation: study.protocolSection.identificationModule.nctIdAliases "
            "names an audit-holdout trial."
        )
    # Registry text is copied into the record (reference citations are), so nothing in
    # the study may name an audit trial, under any key (secondaryIdInfos[].id included).
    _scan_strings_for_audit_nct_tokens(study, context="study", audit_nct_ids=checked_audit_ids)

    _validate_nested_firewall_metadata(
        study,
        context="study",
        expected_nct_id=nct_id,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )

    _validate_side_stream_records(
        extracted_documents,
        stream_name="extracted_documents",
        expected_nct_id=nct_id,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_extracted_documents_name_study(
        extracted_documents,
        nct_id=nct_id,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_side_stream_records(
        pubmed_records,
        stream_name="pubmed_records",
        expected_nct_id=nct_id,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_side_stream_records(
        europe_pmc_records,
        stream_name="europe_pmc_records",
        expected_nct_id=nct_id,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )

    effective_pubmed = pubmed_records
    if effective_pubmed is None and publication_provider is not None:
        effective_pubmed = publication_provider.get_publications_for_study(nct_id)
        _validate_side_stream_records(
            effective_pubmed,
            stream_name="publication_provider",
            expected_nct_id=nct_id,
            audit_nct_ids=checked_audit_ids,
            in_scope_nct_ids=checked_scope_ids,
            allow_empty_audit_set=allow_empty_audit_set,
        )

    milestones = _extract_registry_milestones(study)
    pc_date = milestones["primary_completion_date"]["date"]
    results_date = milestones["results_first_post_date"]["date"]
    pc_to_results = compute_day_delta_interval(pc_date, results_date)

    large_docs, excluded_icf = _extract_large_docs(
        study,
        extracted_documents,
        pc_date,
        results_date,
    )
    linked_publications, dropped_malformed_dois = _extract_and_merge_publications(
        study,
        pubmed_records=effective_pubmed,
        europe_pmc_records=europe_pmc_records,
        primary_completion_date=pc_date,
        results_first_post_date=results_date,
    )

    return LongitudinalProvenanceRecord(
        nct_id=nct_id,
        provenance_schema_version=LONGITUDINAL_SCHEMA_VERSION,
        provenance_rules_version=PROVENANCE_RULES_VERSION,
        assembled_at=assembled_at,
        source_pipeline_commit=source_pipeline_commit,
        registry_milestones=milestones,
        days_from_primary_completion_to_results=pc_to_results["exact_days"],
        primary_completion_to_results_delta=pc_to_results,
        large_docs=large_docs,
        excluded_standalone_icf_count=excluded_icf,
        linked_publications=linked_publications,
        dropped_malformed_doi_count=dropped_malformed_dois,
    )


def to_outcomes_layout_provenance(
    record: LongitudinalProvenanceRecord,
    *,
    in_audit: Any = _MISSING,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    in_scope_nct_ids: AbstractSet[str] | None = None,
    allow_empty_audit_set: bool = False,
) -> dict[str, Any]:
    """Project a ``LongitudinalProvenanceRecord`` to the outcomes bundle shard schema.

    Emits ``nct_id`` alongside the four top-level sections consumed by the
    outcomes bundle builder (``registry_milestones``, ``posted_documents``,
    ``publications``, ``date_deltas``) and provenance version stamps.

    Firewall basis: the same as ``assemble_longitudinal_provenance``. A record
    may be read back from a file (the shape the JSONL round-trip writes), so
    nothing about it shows it was assembled behind the firewall, and its own
    ``in_audit`` can only refuse. The caller therefore supplies an explicit
    ``in_audit`` or the verified ``in_scope_nct_ids``, with ``audit_nct_ids``;
    without them the projection is refused. The record's nested ID fields
    follow the named-key walk, and none of its strings may name an audit trial.

    The record's ``dropped_malformed_doi_count`` (rules 1.5.0) is not
    projected: the projected key set, which Module 3 reads, is unchanged, and
    a record written before 1.5.0 (without the key) still projects.
    """
    if not isinstance(record, Mapping):
        raise CitationVerificationError(
            f"provenance record must be a mapping, got {type(record).__name__}."
        )
    checked_nct = validate_strict_nct_id(record.get("nct_id"), context="provenance.nct_id")
    checked_audit_ids = _validate_nct_set(audit_nct_ids, context="audit_nct_ids")
    checked_scope_ids: frozenset[str] | None = (
        _validate_nct_set(in_scope_nct_ids, context="in_scope_nct_ids")
        if in_scope_nct_ids is not None
        else None
    )
    _prescan_nested_nct_fields(record, context="provenance")
    _refuse_own_in_audit_flags(record, record_label="provenance record")
    enforce_audit_holdout(
        checked_nct,
        in_audit=in_audit,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _validate_nested_firewall_metadata(
        record,
        context="provenance",
        expected_nct_id=checked_nct,
        audit_nct_ids=checked_audit_ids,
        in_scope_nct_ids=checked_scope_ids,
        allow_empty_audit_set=allow_empty_audit_set,
    )
    _scan_strings_for_audit_nct_tokens(
        record, context="provenance", audit_nct_ids=checked_audit_ids
    )
    return {
        "nct_id": checked_nct,
        "provenance_schema_version": record["provenance_schema_version"],
        "provenance_rules_version": record["provenance_rules_version"],
        "assembled_at": record["assembled_at"],
        "source_pipeline_commit": record["source_pipeline_commit"],
        "registry_milestones": record["registry_milestones"],
        "posted_documents": record["large_docs"],
        "excluded_standalone_icf_count": record["excluded_standalone_icf_count"],
        "publications": record["linked_publications"],
        "date_deltas": {
            "days_from_primary_completion_to_results": record[
                "days_from_primary_completion_to_results"
            ],
            "primary_completion_to_results": record["primary_completion_to_results_delta"],
        },
    }


__all__ = [
    "AuditHoldoutViolationError",
    "CitationVerificationError",
    "DateDelta",
    "DatePrecision",
    "DocType",
    "DocumentVersionMilestone",
    "InvalidNctIdError",
    "LINK_ORIGINS",
    "LONGITUDINAL_SCHEMA_VERSION",
    "LinkOrigin",
    "LinkedPublicationMilestone",
    "LongitudinalProvenanceRecord",
    "PRECISION_RANK",
    "PROVENANCE_RULES_VERSION",
    "PROVENANCE_SCHEMA_VERSION",
    "ParsedDate",
    "PublicationProvider",
    "RegistryMilestone",
    "__version__",
    "assemble_longitudinal_provenance",
    "compute_date_delta",
    "compute_day_delta_interval",
    "parse_ctgov_upload_date",
    "parse_iso_or_partial_date",
    "parse_partial_date",
    "parse_printed_doc_date",
    "parse_pubmed_date",
    "to_outcomes_layout_provenance",
]
