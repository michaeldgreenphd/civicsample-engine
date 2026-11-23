"""Content-addressed ClinicalTrials.gov document pre-indexer, readability/redaction
classifier, factual upload-timing calculator, and deterministic TOC statistical
section slicer.

Runtime and platform dependencies:
- Core indexing, readability/redaction classification, upload-timing math,
  content-addressed cache persistence, ``FixturePageTextProvider``, and
  ``JsonlPageMapProvider`` use Python 3.11+ standard-library modules.
- The companion CLI ``scripts/index_trial_documents.py`` imports ``requests``
  and ``tqdm`` from ``requirements.txt`` (alongside ``src.api_client.CTGovAPIClient``).
- ``MacOSPDFKitProvider`` requires macOS (``sys.platform == 'darwin'``) and
  invokes either ``clang`` against Apple's ``Foundation`` and ``PDFKit``
  frameworks or ``ctypes.CDLL`` bindings to ``/usr/lib/libobjc.A.dylib``,
  ``Foundation.framework``, and ``PDFKit.framework``. On non-macOS platforms
  without a pre-extracted JSONL or cached page map, page extraction fails loudly
  with ``PageExtractionError``.

Analytical invariants:
- Hard in_audit holdout firewall: every entry point validates strict ASCII
  ``^NCT[0-9]{8}$`` (``type(v) is str``, no stripping, no case-folding, no
  zero-width characters, no Unicode digits) BEFORE checking ``audit_nct_ids``,
  ``in_audit``, and ``allowed_nct_ids``. ``enforce_not_in_audit()`` raises
  ``AuditHoldoutError`` unconditionally before any API query, download, cache
  lookup, or PDF parse whenever ``nct_id`` is in ``audit_nct_ids``, ``in_audit``
  is ``True`` or non-``bool``, or ``nct_id`` is outside ``allowed_nct_ids``.
- Firewall error messages never name a trial. An id that fails the holdout
  check may be a holdout trial, so ``AuditHoldoutError`` and
  ``InvalidNctIdError`` messages report positions (file, line, row index) and
  counts only, and run logs never carry holdout ids. No message echoes a
  refused field's value (a filename, a date, a stamp): it is described by its
  type and length.
- Every entry point that is handed a ``DocumentCacheStore`` runs under the
  store's holdout set and allowlist merged with its own arguments
  (``DocumentCacheStore.merge_firewall_sets``), for the requesting trial and
  for the tag of every cached page map it reads, so a store built with a
  holdout set refuses that trial whatever the call's defaulted arguments say,
  and a holdout set passed to the call refuses a cached map tagged with it.
  ``url_manifest.json`` is checked entry by entry when the store opens it,
  against the store's own sets only (a call's sets do not exist yet); the
  URLs a call looks up or records are its own trial's, which passed the
  merged check. Build the store with the run's sets (``run_cli`` does) to
  have that file checked against them.
- No written file carries a holdout id from any field. A posted filename is a
  plain ASCII basename (``validate_safe_pdf_filename``: no ``%``, no
  separators, nothing that reads as a trial id); a date copied into a record
  is a whole CT.gov date string, never text after one, an object's
  ``str()`` or anything else (``parse_ctgov_date``,
  ``validate_ctgov_date_field``); a timestamp read from input (a census
  ``retrieved_at``, a url_manifest ``verified_at``) holds nothing that reads
  as a trial id, so no basic-form ``YYYYMMDD``; a manifest marks a holdout
  wherever in the file it says ``in_audit: true``.
- Under a configured firewall every request for pages names its trial: an
  unnamed request (``nct_id=None``) is refused before anything is extracted
  or written, as an untagged page map is refused.
- Page maps are attributed before they are loaded. Whenever a holdout set or an
  allowlist is configured, every page map (a ``--page-map-jsonl`` row or a
  cached ``pages/<sha>.json.gz``) must carry the ``nct_id`` it was extracted
  for; one that names no trial cannot be checked against the holdout and is
  refused. ``DocumentCacheStore`` tags the maps it writes with the trial that
  passed the firewall. Only with no firewall configured at all (library use
  with no audit set and no allowlist) is an untagged map loaded.
- Every document is tied to the trial that passed the firewall. A record's,
  census row's or download's URL must be the canonical CT.gov URL of that
  trial's own posted file; a study payload whose ``nctIdAliases`` names a
  holdout trial is that trial and is refused; page maps serve pages only for
  the digest they were extracted from; every ``url_manifest.json`` entry passes
  the firewall when the cache is opened, and a malformed one is refused rather
  than dropped on the next rewrite. A manifest, census, page-map or cache JSON
  object that names a key twice is refused, never read last-wins.
- Bytes are what their digest says. A PDF file is handed to a page extractor
  only after it hashes to the requested sha256 and byte count, and a local
  PDF is read only from its own trial's folder (``resolve_local_trial_pdf``
  refuses a symlink leading out of it) and only when its byte count is the
  one its census row or CT.gov payload states. The holdout tag of every
  cached page map for a digest (the sharded map and the flat
  ``pages/<sha>.json(.gz)`` ones) is checked before the index entry for that
  digest is served.
- Stored state is read strictly. ``document_index_by_sha256.jsonl`` entries
  and ``url_manifest.json`` records carry exactly their schema's keys and
  types (no ``int()``/``str()`` coercion; a null provenance stamp is not
  turned into text), and one ``(sha256, indexer_rules_version)`` has one
  line. Census rows and CT.gov payloads carry type-checked fields only, one
  entry per trial file. A census ingest, and a live run, write to the cache
  only when they succeed whole (``DocumentCacheStore.staged_writes``).
- Non-collapsing readability taxonomy: all 7 DocumentReadabilityStatus values
  (machine_readable, partial_image_or_redacted, image_only_scan, garbled_cmap,
  extraction_failed, download_failed, no_document) remain distinct and are
  evaluated in strict deterministic order. ``extraction_failed`` is a document
  that downloaded (its sha256 and byte count are known and recorded) but whose
  pages no provider could extract; ``download_failed`` is a document whose bytes
  were never obtained. Neither has a page count, so both record
  ``physical_pages=None`` rather than 0.
- Content-addressed immutability: DocumentIndexEntry is keyed by (sha256,
  indexer_rules_version) and separated from trial-specific bindings so identical
  PDFs shared across trials are downloaded and parsed once while preserving
  original provenance stamps (extracted_at, pipeline_commit).
- Precision-aware upload timing: partial dates (YYYY-MM, YYYY) never fabricate
  '-01' day suffixes; exact_days is populated only when both endpoints have
  day precision, and all keys/values are strictly factual with zero compliance
  or evaluative labels.
- Lossless TOC/section page slicing: pages_selected, pages_deferred, and
  pages_without_text always form a disjoint exact partition of 1..physical_pages.
"""

from __future__ import annotations

import calendar
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import (
    AbstractSet,
    Any,
    Final,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypedDict,
    runtime_checkable,
)


__version__: Final[str] = "1.0.0"
INDEXER_RULES_VERSION: Final[str] = "doc_indexer@2026-11-21.v1"

DocType = Literal["protocol", "sap", "combined"]
ContentReadabilityStatus = Literal[
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
]
DocumentReadabilityStatus = Literal[
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
    "extraction_failed",
    "download_failed",
    "no_document",
]
# Intended to equal Module 3's outcomes_layout.REDACTION_FLAGS. Module 3 is not
# on this branch and this module may not import it, so nothing here tests the
# equality; a branch that carries both modules must add that parity test.
RedactionFlag = Literal["cci", "ppd", "white_box_or_blank_stats"]
# Intended to equal Module 4's citation_verifier.UNREADABLE_SUBTYPES. Same
# caveat: untested on this branch, to be pinned by a parity test once both
# modules share a branch.
UnreadableRecoveryBucket = Literal[
    "image_only_scan",
    "redacted_cci_ppd",
    "garbled_cmap",
]

StatisticalSectionKind = Literal[
    "synopsis",
    "objectives_endpoints",
    "analysis_populations",
    "sample_size",
    "interim_analysis",
    "multiplicity",
    "statistical_methods",
]
DatePrecision = Literal["day", "month", "year", "unknown"]
TimingRelation = Literal[
    "before",
    "same_day",
    "after",
    "ambiguous_partial_date",
    "unknown_missing_date",
]
SelectionStrategy = Literal[
    "full_readable_within_budget",
    "toc_and_headings",
    "headings_and_keywords",
    "fallback_leading_pages",
    "no_readable_pages",
]

READABILITY_STATUSES: Final[tuple[DocumentReadabilityStatus, ...]] = (
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
    "extraction_failed",
    "download_failed",
    "no_document",
)
REDACTION_FLAGS: Final[tuple[RedactionFlag, ...]] = (
    "cci",
    "ppd",
    "white_box_or_blank_stats",
)
UNREADABLE_RECOVERY_BUCKETS: Final[tuple[UnreadableRecoveryBucket, ...]] = (
    "image_only_scan",
    "redacted_cci_ppd",
    "garbled_cmap",
)
STATISTICAL_SECTION_KINDS: Final[tuple[StatisticalSectionKind, ...]] = (
    "synopsis",
    "objectives_endpoints",
    "analysis_populations",
    "sample_size",
    "interim_analysis",
    "multiplicity",
    "statistical_methods",
)

NCT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^NCT[0-9]{8}$", re.ASCII)
STRICT_NCT_ID_RE: Final[re.Pattern[str]] = NCT_ID_RE
SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
CT_GOV_LARGE_DOCS_BASE_URL: Final[str] = "https://cdn.clinicaltrials.gov/large-docs"
DEFAULT_DOCUMENT_MODEL_ID: Final[str] = "deterministic_indexer"

_FORBIDDEN_PROFILE_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"page_texts", "raw_pages", "pages"}
)
_PRECISION_RANK: Final[dict[DatePrecision, int]] = {
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

# Frozen 50-word English function + clinical-trial vocabulary used by the
# calibrated CMap detector. Clean English and biostatistics prose reliably hits
# >= 0.04 on this vocabulary even when surrounded by Greek/math notation, whereas
# punctuation-shifted and Caesar-shifted CMaps score ~0.00.
CALIBRATED_COMMON_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "of",
        "and",
        "to",
        "in",
        "for",
        "is",
        "with",
        "by",
        "on",
        "at",
        "from",
        "or",
        "as",
        "be",
        "are",
        "was",
        "were",
        "will",
        "that",
        "this",
        "not",
        "an",
        "all",
        "study",
        "protocol",
        "statistical",
        "analysis",
        "patients",
        "subjects",
        "participants",
        "primary",
        "secondary",
        "endpoint",
        "endpoints",
        "treatment",
        "group",
        "groups",
        "trial",
        "clinical",
        "data",
        "population",
        "sample",
        "size",
        "efficacy",
        "safety",
        "table",
        "section",
        "page",
        "version",
    }
)


class DocumentIndexerError(ValueError):
    """Base error for document indexing contract or integrity violations."""


class InvalidNctIdError(DocumentIndexerError):
    """Raised before any audit check when an NCT ID is not strict ASCII ^NCT[0-9]{8}$."""


class AuditHoldoutError(DocumentIndexerError):
    """Raised unconditionally before any API query, download, or PDF parse when in_audit is violated."""


class CacheIntegrityError(DocumentIndexerError):
    """Raised when sha256 digest, byte count, or immutable cache entry invariants disagree."""


class PageExtractionError(DocumentIndexerError):
    """Raised when a PDF container cannot be opened or no PageTextProvider can service it."""


# A repeated key is named in an error message only when it has the shape of a
# schema field name: lowercase ASCII letters and underscores. Such a key cannot
# hold a trial id (ids are upper-case letters and digits); any other key (a
# URL keying url_manifest.json, an id used as a key) may, and is not printed.
_NAMEABLE_JSON_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z_]{1,32}$", re.ASCII)


def loads_strict_json(
    text: str,
    *,
    origin: str,
    error_cls: type[DocumentIndexerError] = AuditHoldoutError,
) -> Any:
    """Parse JSON text, refusing any object, at any depth, that names one key twice.

    ``json.loads`` keeps the last of a repeated key, so a manifest row written
    ``{"in_audit": true, "in_audit": false}`` reads as False, and a row naming
    ``nct_id`` twice reads as whichever id came last. The file holds both
    values and nothing says which was meant, so the firewall cannot choose
    between them: the input is refused instead of read last-wins.

    ``error_cls`` is ``AuditHoldoutError`` (the default) for inputs that decide
    what may be indexed (manifests, census rows, page maps) and
    ``CacheIntegrityError`` for cache bookkeeping. The message names ``origin``
    and the repeated key only under ``_NAMEABLE_JSON_KEY_RE``. Malformed JSON
    still raises ``json.JSONDecodeError`` for the caller to report.
    """

    def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                shown = repr(key) if _NAMEABLE_JSON_KEY_RE.fullmatch(key) else "a key"
                raise error_cls(
                    f"JSON object at {origin} names {shown} more than once; a repeated "
                    "key is ambiguous and is refused, not read last-wins."
                )
            out[key] = value
        return out

    return json.loads(text, object_pairs_hook=_refuse_repeated_keys)


# ---------------------------------------------------------------------------
# Hard in_audit holdout guard & core helpers
# ---------------------------------------------------------------------------


def _describe_rejected_id(value: Any) -> str:
    """Describe a rejected NCT id by its shape only, never by its value.

    A malformed id can still be a holdout id with one stray character (a
    trailing space, a zero-width joiner), so echoing it would put a holdout id
    into a run log. The type, length and ASCII-ness are enough to tell a user
    which kind of mistake was made.
    """
    if type(value) is str:
        return f"type=str, length={len(value)}, ascii={value.isascii()}"
    return f"type={type(value).__name__}"


def validate_nct_id(nct_id: Any) -> str:
    """Validate strict canonical ASCII NCT ID (^NCT[0-9]{8}$).

    Enforces exact ``type(nct_id) is str`` with zero whitespace stripping, zero
    case-folding, zero Unicode digits, and zero invisible/zero-width characters.
    The error message describes the rejected value's shape, not the value
    (see ``_describe_rejected_id``).
    """
    if (
        type(nct_id) is not str
        or len(nct_id) != 11
        or not nct_id.isascii()
        or NCT_ID_RE.fullmatch(nct_id) is None
    ):
        raise InvalidNctIdError(
            f"Invalid NCT ID ({_describe_rejected_id(nct_id)}): must be strict "
            "ASCII str matching ^NCT[0-9]{8}$."
        )
    return nct_id


validate_strict_nct_id = validate_nct_id


def _validate_nct_id_set(
    ids: AbstractSet[str],
    *,
    label: str = "nct_ids",
) -> frozenset[str]:
    """Validate every member of an NCT ID set against strict ASCII ^NCT[0-9]{8}$."""
    if not isinstance(ids, AbstractSet):
        raise InvalidNctIdError(f"{label} must be a set/frozenset of strict NCT IDs.")
    out: set[str] = set()
    for item in ids:
        out.add(validate_nct_id(item))
    return frozenset(out)


def enforce_not_in_audit(
    nct_id: str,
    *,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> str:
    """Enforce strict NCT format and the in_audit holdout firewall before any I/O.

    Checks in strict order:
    1. ``nct_id`` matches strict ASCII ``^NCT[0-9]{8}$`` (raises ``InvalidNctIdError``).
    2. All entries in ``audit_nct_ids`` and ``allowed_nct_ids`` match strict ASCII.
    3. ``nct_id`` is not in ``audit_nct_ids`` (raises ``AuditHoldoutError``).
    4. ``in_audit`` is a strict ``bool`` equal to ``False`` (raises ``AuditHoldoutError``).
    5. When ``allowed_nct_ids`` is supplied, ``nct_id`` is a member (raises ``AuditHoldoutError``).

    No message names the trial: the caller knows which id it passed, and a run
    log must never carry a holdout id. Set overlaps are reported as counts.
    """
    validated_nct = validate_nct_id(nct_id)
    validated_audit = _validate_nct_id_set(audit_nct_ids, label="audit_nct_ids")
    validated_allowed = (
        _validate_nct_id_set(allowed_nct_ids, label="allowed_nct_ids")
        if allowed_nct_ids is not None
        else None
    )
    if validated_allowed is not None and (validated_allowed & validated_audit):
        raise AuditHoldoutError(
            f"Conflicting firewall sets: {len(validated_allowed & validated_audit)} id(s) "
            "present in both allowed_nct_ids and audit_nct_ids."
        )
    if validated_nct in validated_audit:
        raise AuditHoldoutError(
            "Requested trial is in the audit holdout set; "
            "API queries, downloads, cache lookups, and PDF parsing are forbidden."
        )
    if type(in_audit) is not bool:
        raise AuditHoldoutError(
            f"Requested trial has non-bool in_audit (type={type(in_audit).__name__}); "
            "an explicit boolean False is required to pass the audit holdout firewall."
        )
    if in_audit:
        raise AuditHoldoutError(
            "Requested trial has in_audit=True; "
            "API queries, downloads, cache lookups, and PDF parsing are forbidden."
        )
    if validated_allowed is not None and validated_nct not in validated_allowed:
        raise AuditHoldoutError(
            "Requested trial is not in the verified in-scope allowlist; "
            "unlisted trials are blocked by the audit holdout firewall."
        )
    return validated_nct


def validate_study_payload_nct_id(
    study: Mapping[str, Any],
    *,
    expected_nct_id: str | None = None,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> str:
    """Validate protocolSection.identificationModule.nctId inside an API study payload.

    Enforces before any CDN request, cache lookup, or PDF parse:
    (a) strict ASCII ``^NCT[0-9]{8}$`` on the study's ``nctId`` (no strip/upper),
    (b) exact equality with ``expected_nct_id`` when supplied,
    (c) absence from ``audit_nct_ids`` and strict ``in_audit is False`` (both caller
        flag and any ``study['in_audit']`` field),
    (d) membership in ``allowed_nct_ids`` when supplied, and
    (e) every retired id in ``identificationModule.nctIdAliases`` is strict
        ASCII and none is in ``audit_nct_ids`` (a merged registration is each
        of the trials it absorbed); and, when no holdout set is known (an
        empty ``audit_nct_ids``) but an allowlist is, every alias is on the
        allowlist, since an alias checked against an empty holdout set has
        been checked against nothing.
    """
    if not isinstance(study, Mapping):
        raise DocumentIndexerError("study payload must be a Mapping.")

    validated_expected: str | None = None
    if expected_nct_id is not None:
        validated_expected = enforce_not_in_audit(
            expected_nct_id,
            in_audit=in_audit,
            audit_nct_ids=audit_nct_ids,
            allowed_nct_ids=allowed_nct_ids,
        )

    protocol_section = study.get("protocolSection")
    id_mod = (
        protocol_section.get("identificationModule")
        if isinstance(protocol_section, Mapping)
        else None
    )
    raw_study_nct = id_mod.get("nctId") if isinstance(id_mod, Mapping) else None
    study_nct_id = validate_nct_id(raw_study_nct)

    if "in_audit" in study:
        payload_in_audit = study["in_audit"]
        if type(payload_in_audit) is not bool or payload_in_audit is True:
            raise AuditHoldoutError(
                "Study payload carries disallowed in_audit "
                f"(type={type(payload_in_audit).__name__}, value is not False)."
            )

    enforce_not_in_audit(
        study_nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )

    # (e) The payload's retired ids. CT.gov merges duplicate registrations of
    # one study and keeps the retired ids in identificationModule.nctIdAliases,
    # so a payload whose alias list names a holdout trial IS that holdout
    # trial, whatever its surviving nctId: its documents are the holdout's
    # documents. Every alias must be a strict id (a malformed one cannot be
    # checked) and none may be in the holdout set. With a holdout set known,
    # an alias the manifest does not list is accepted: the allowlist governs
    # the trial being indexed (nctId, checked above), and a merged
    # registration's retired id need not appear in a manifest of surviving ids.
    #
    # With NO holdout set (a manifest carrying only in_audit=False rows, such
    # as a filtered run list), the holdout check above compares each alias
    # with nothing, so an unlisted alias may be a holdout trial the manifest
    # simply did not mention. The allowlist is then the only list that can
    # vouch for an alias: one not on it is refused. With neither set
    # configured (library use with no firewall) aliases are accepted, as an
    # untagged page map is.
    raw_aliases = id_mod.get("nctIdAliases") if isinstance(id_mod, Mapping) else None
    if raw_aliases is not None:
        if not isinstance(raw_aliases, list):
            raise AuditHoldoutError(
                "Study payload nctIdAliases is "
                f"{type(raw_aliases).__name__}, not a list, so it cannot be checked "
                "against the audit holdout."
            )
        validated_audit = _validate_nct_id_set(audit_nct_ids, label="audit_nct_ids")
        alias_allowlist = (
            _validate_nct_id_set(allowed_nct_ids, label="allowed_nct_ids")
            if allowed_nct_ids is not None
            else None
        )
        for index, alias in enumerate(raw_aliases):
            try:
                validated_alias = validate_nct_id(alias)
            except InvalidNctIdError as exc:
                raise InvalidNctIdError(
                    f"Study payload nctIdAliases[{index}]: {exc}"
                ) from None
            if validated_alias in validated_audit:
                raise AuditHoldoutError(
                    f"Study payload nctIdAliases[{index}] is an audit holdout trial; a "
                    "merged registration is that trial too, so its documents are "
                    "blocked by the audit holdout firewall."
                )
            if (
                not validated_audit
                and alias_allowlist is not None
                and validated_alias not in alias_allowlist
            ):
                raise AuditHoldoutError(
                    f"Study payload nctIdAliases[{index}] is not on the in-scope allowlist "
                    "and no audit holdout set is known (the manifest lists no "
                    "in_audit=True row), so nothing shows the merged trial is not a "
                    "holdout trial; its documents are blocked. Run with the manifest that "
                    "marks the holdout."
                )

    if validated_expected is not None and study_nct_id != validated_expected:
        # Neither id is printed: the payload's id is unverified and may be a
        # holdout trial's.
        raise AuditHoldoutError(
            "Study payload nctId does not match the requested nct_id; mismatched "
            "study payloads are blocked by the audit holdout firewall."
        )

    return study_nct_id


_SURROGATE_RE: Final[re.Pattern[str]] = re.compile(r"[\ud800-\udfff]")


def sanitize_page_text(text: str) -> str:
    """Replace unpaired UTF-16 surrogate code points (U+D800..U+DFFF) with U+FFFD.

    macOS PDFKit occasionally emits lone UTF-16 surrogates from malformed font
    glyph streams, which crash strict UTF-8 encoding and JSON serialization
    unless sanitized.
    """
    if not text:
        return ""
    return _SURROGATE_RE.sub("\ufffd", text)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# The checkout this module was loaded from. ``pipeline_commit`` stamps the code
# that produced an artifact, so git is asked about this directory, never about
# the process's working directory (which may be outside any checkout, or inside
# a different repository whose commit would then be stamped).
PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_GIT_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$", re.ASCII)


def resolve_pipeline_commit(
    repo_root: Path | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(commit, reason)``: the code's commit, or None and why it is absent.

    Order: ``GITHUB_SHA`` (CI), then ``git -C <repo_root> rev-parse HEAD`` where
    ``repo_root`` defaults to the checkout holding this module. Exactly one of
    the two values is None. The reason is a short phrase for a run log; callers
    must surface it, since a None stamp without a reason is the silent failure
    the provenance rule forbids.

    A set ``GITHUB_SHA`` must be a commit id (40 or 64 lowercase hex, as git
    prints one); any other value is not stamped (it is free text and would be
    written into every artifact, url_manifest.json included, which refuses a
    non-commit stamp) and the reason says so without echoing it.
    """
    sha = os.environ.get("GITHUB_SHA")
    if sha and sha.strip():
        if not _GIT_COMMIT_RE.fullmatch(sha.strip()):
            return None, "GITHUB_SHA is set but is not a 40- or 64-hex commit id"
        return sha.strip(), None
    root = Path(repo_root) if repo_root is not None else PIPELINE_REPO_ROOT
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return None, "git executable not found and GITHUB_SHA is unset"
    except subprocess.TimeoutExpired:
        return None, f"git rev-parse HEAD timed out in {root}"
    except OSError as exc:
        return None, f"git could not be run in {root}: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        first = detail[0] if detail else f"exit status {proc.returncode}"
        return None, f"git rev-parse HEAD failed in {root}: {first}"
    out = proc.stdout.strip()
    if not _GIT_COMMIT_RE.fullmatch(out):
        return None, f"git rev-parse HEAD in {root} returned no commit id"
    return out, None


def safe_pipeline_commit(repo_root: Path | None = None) -> str | None:
    """Return the commit from ``resolve_pipeline_commit`` and drop the reason.

    Kept for callers that only need the stamp. A caller that writes an artifact
    should use ``resolve_pipeline_commit`` and report the reason when the stamp
    is None.
    """
    commit, _reason = resolve_pipeline_commit(repo_root)
    return commit


def encode_gz_json(obj: Any) -> bytes:
    """Encode an object as deterministic gzipped ASCII JSON (compresslevel=9, mtime=0, OS=0xff)."""
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(payload)
    raw = bytearray(buf.getvalue())
    # Pin gzip header OS byte (offset 9) to 0xff (unknown) for byte-identical
    # output across macOS and Linux CI (mirrors src/archive_records.py).
    if len(raw) >= 10:
        raw[9] = 0xFF
    return bytes(raw)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically using a same-directory temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically using a same-directory temp file and os.replace."""
    atomic_write_bytes(path, text.encode("utf-8"))


def _rmdir_if_empty(path: Path) -> None:
    """Remove the directory ``path`` when it exists and is empty; leave it (and say nothing) otherwise.

    Used only by a staged-write rollback to remove folders its own removed
    files emptied. A non-empty or missing folder is not an error there: it
    holds files the rollback did not create, or was never made.
    """
    try:
        Path(path).rmdir()
    except OSError:
        pass


def sha256_bytes(data: bytes) -> str:
    """Return lowercase 64-hex SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    """Stream-hash a file and return (sha256_hex, byte_count)."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def has_pdf_magic(data_prefix: bytes) -> bool:
    """Return True if '%PDF-' appears within the first 1024 bytes of a file."""
    return b"%PDF-" in data_prefix[:1024]


def _describe_rejected_filename(value: Any) -> str:
    """Describe a refused filename by its shape, never its text (it may name a holdout trial)."""
    if type(value) is str:
        return f"type=str, length={len(value)}, ascii={value.isascii()}"
    return f"type={type(value).__name__}"


def _check_pdf_basename(filename: Any) -> str:
    """Return ``filename`` when it is a non-empty str basename ending in ``.pdf``; raise otherwise.

    The minimal shape ``build_ctgov_large_doc_url`` needs to assemble a URL
    string: no ``/`` or ``\\``, no ``..``, a ``.pdf`` suffix. It is NOT the
    check for a filename taken from a payload, a census row or a page map;
    those go through ``validate_safe_pdf_filename``, which every entry point
    applies before a URL is built, requested or recorded. The message
    describes the value's shape only.
    """
    if (
        type(filename) is not str
        or not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or not filename.lower().endswith(".pdf")
    ):
        raise DocumentIndexerError(
            f"Unsafe or invalid PDF filename ({_describe_rejected_filename(filename)}); "
            "the filename is not printed."
        )
    return filename


# A posted CT.gov document filename is a plain basename such as
# ``Prot_000.pdf``, ``SAP_001.pdf`` or ``Prot_SAP_000.pdf``. Only ASCII letters,
# digits, ``_``, ``-`` and ``.`` are accepted, starting with a letter or digit.
# This refuses ``%`` (a percent-encoded ``../`` or ``/`` reads as a path into
# another trial's folder once a server decodes it, while the URL string still
# names the in-scope trial), whitespace, ``?``/``#`` and every non-ASCII
# character (a zero-width or full-width variant of an id).
_POSTED_PDF_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,250}\.[Pp][Dd][Ff]", re.ASCII
)
# A filename that reads as a trial id is refused: "NCT" (any case) followed by
# optional separators and a digit, or any run of 8 digits (an id's digits).
# CT.gov keeps the trial id in the folder, never in the filename, and a run log
# or written record must never carry a holdout id from any field.
_TRIAL_ID_IN_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"[Nn][Cc][Tt][._\-]*[0-9]|[0-9]{8}", re.ASCII
)


def validate_safe_pdf_filename(filename: Any) -> str:
    """Validate a posted PDF filename before anything is requested, read or recorded under it.

    Refused (``DocumentIndexerError``): anything but a non-empty str; a
    directory separator or ``..``; any character outside ASCII letters,
    digits, ``_``, ``-`` and ``.`` (``%`` in particular: the filename
    ``%2E%2E%2F...%2F<holdout id>%2FProt_000.pdf`` made a "canonical" URL of the
    in-scope trial that a server decodes to the holdout trial's document); a
    name not ending in ``.pdf``. Refused with ``AuditHoldoutError``: a name that
    reads as a trial id (``NCT`` and a digit, or 8 digits in a row), since the
    id could be a holdout trial's and would be requested from the CDN and
    written into the profile and url_manifest.json.

    No message echoes the filename (it may name a holdout trial); the value's
    type and length are given instead.
    """
    name = _check_pdf_basename(filename)
    if not _POSTED_PDF_FILENAME_RE.fullmatch(name):
        raise DocumentIndexerError(
            f"Unsafe or invalid PDF filename ({_describe_rejected_filename(name)}): a posted "
            "filename is a plain ASCII basename of letters, digits, '_', '-' and '.' ending "
            "in .pdf (no '%', whitespace or other characters); the filename is not printed."
        )
    if _TRIAL_ID_IN_FILENAME_RE.search(name):
        raise AuditHoldoutError(
            f"A posted PDF filename ({_describe_rejected_filename(name)}) carries text that "
            "reads as a trial id ('NCT' and a digit, or 8 digits in a row); it may name a "
            "holdout trial, so it is not requested or recorded (the filename is not printed)."
        )
    return name


def build_ctgov_large_doc_url(nct_id: str, filename: str) -> str:
    """Construct the ClinicalTrials.gov CDN URL string for ``nct_id``'s posted ``filename``.

    A pure string builder: the filename needs only the basename shape
    (``_check_pdf_basename``). Whether a filename may be requested or
    recorded is decided by ``validate_safe_pdf_filename``, which every entry
    point applies first, and ``parse_ctgov_large_doc_url`` treats a URL as
    canonical only when its filename passes that stricter check.
    """
    nct = validate_nct_id(nct_id)
    fname = _check_pdf_basename(filename)
    return f"{CT_GOV_LARGE_DOCS_BASE_URL}/{nct[-2:]}/{nct}/{fname}"


_LARGE_DOC_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://cdn\.clinicaltrials\.gov/large-docs/[0-9]{2}/(NCT[0-9]{8})/([^/]+)$",
    re.ASCII,
)


def parse_ctgov_large_doc_url(url: Any) -> tuple[str, str] | None:
    """Return ``(nct_id, filename)`` for a canonical CT.gov document URL, else None.

    A URL is canonical when it is exactly what ``build_ctgov_large_doc_url``
    returns for the id and filename it names (same scheme, host, two-digit
    directory, id and basename; no query, fragment or trailing text) and its
    filename passes ``validate_safe_pdf_filename``. This is how a URL is tied
    to the trial it belongs to: a URL in any other form cannot be attributed,
    and callers under a configured firewall refuse it. A percent-encoded
    filename (which a server may decode to a path into another trial's folder)
    or one that reads as a trial id is not canonical.
    """
    if type(url) is not str:
        return None
    match = _LARGE_DOC_URL_RE.fullmatch(url)
    if match is None:
        return None
    nct, filename = match.group(1), match.group(2)
    try:
        validate_safe_pdf_filename(filename)
        if build_ctgov_large_doc_url(nct, filename) != url:
            return None
    except DocumentIndexerError:
        return None
    return nct, filename


def require_own_large_doc_url(
    url: Any,
    *,
    nct_id: str,
    filename: str,
    origin: str,
) -> str:
    """Return ``url`` if it is ``nct_id``'s own posted ``filename``; raise ``AuditHoldoutError`` otherwise.

    The document a record describes, or a download fetches, is fixed by the
    trial and filename that passed the firewall: its URL is
    ``build_ctgov_large_doc_url(nct_id, filename)``. Any other URL (another
    trial's document, a different file, another host) would put a document
    the firewall never checked under an in-scope trial, so it is refused. The
    message names ``origin`` only; the URL is not printed, since it may name a
    holdout trial. ``filename`` itself must pass ``validate_safe_pdf_filename``
    (every caller has checked it already; this repeats the check so the rule
    does not depend on the caller).
    """
    expected = build_ctgov_large_doc_url(nct_id, validate_safe_pdf_filename(filename))
    if type(url) is not str or url != expected:
        raise AuditHoldoutError(
            f"{origin} carries a document url that is not the canonical CT.gov URL "
            "of this trial's own posted file; a record or download may not name "
            "another trial's document (the url is not printed)."
        )
    return url


def resolve_local_trial_pdf(local_pdf_dir: Path, nct_id: str, filename: str) -> Path | None:
    """Return the real path of ``<local_pdf_dir>/<nct_id>/<filename>``, None when there is no such file.

    The path is built from the trial that passed the firewall, but what it
    names on disk is up to the file system: a symlink (the file, or the
    trial's whole folder) can lead into another trial's folder, a holdout
    trial's included, and the file it reaches would then be read, hashed,
    parsed and indexed as the in-scope trial's document, its digest recorded
    in the index, page map, url_manifest.json and profile. So, before the file
    is opened:

    - ``local_pdf_dir`` must be an existing directory (a mistyped directory
      would otherwise quietly find no local PDFs). It is itself resolved, so
      the directory may be a link chosen by the caller.
    - The trial folder ``<nct_id>`` must be a real directory, not a symlink.
    - The file's fully resolved path must lie directly in that trial folder. A
      link to a sibling file of the same trial stays in the folder and is
      allowed; a link leading anywhere else, or one that cannot be resolved
      (dangling, looping), is refused. The resolved path must be a regular file.

    A refusal raises ``AuditHoldoutError`` (``DocumentIndexerError`` for a
    missing directory or a non-file) and names neither the link's target nor
    any trial: the target may be a holdout trial's file. A hard link cannot be
    told apart from a copy by its path, so this check does not cover one.
    """
    nct = validate_nct_id(nct_id)
    safe_filename = validate_safe_pdf_filename(filename)
    root = Path(local_pdf_dir)
    if not root.is_dir():
        raise DocumentIndexerError(
            "--local-pdf-dir is not an existing directory; a mistyped directory would "
            "silently find no local PDFs."
        )
    root_real = root.resolve(strict=True)
    trial_dir = root_real / nct
    if not os.path.lexists(trial_dir):
        return None
    if trial_dir.is_symlink() or not trial_dir.is_dir():
        raise AuditHoldoutError(
            "A trial folder under --local-pdf-dir is a symlink or not a directory, so "
            "its files may belong to another trial; local PDFs are read only from a "
            "trial's own real folder (the link target is not printed)."
        )
    candidate = trial_dir / safe_filename
    if not os.path.lexists(candidate):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AuditHoldoutError(
            "A local PDF under --local-pdf-dir is a link that cannot be resolved; it is "
            "not read (the link target is not printed)."
        ) from None
    if resolved.parent != trial_dir:
        raise AuditHoldoutError(
            "A local PDF under --local-pdf-dir resolves outside its trial's own folder "
            "(a symlink into another trial's folder); it is not read, hashed or indexed "
            "(the link target is not printed)."
        )
    if not resolved.is_file():
        raise DocumentIndexerError(
            "A local PDF under --local-pdf-dir is not a regular file; it is not read."
        )
    return resolved


def validate_page_selection_bounds(min_selected_pages: Any, max_selected_pages: Any) -> None:
    """Raise ``DocumentIndexerError`` unless ``1 <= min_selected_pages <= max_selected_pages`` (both int).

    ``select_statistical_pages`` enforces the same rule, but only once a
    document's pages are in hand: by then a live run has fetched the CT.gov
    record, downloaded the PDF and written its blob, url_manifest.json entry
    and page map. Entry points call this first, so bad bounds stop a run
    before any fetch or cache write.
    """
    if type(min_selected_pages) is not int or min_selected_pages < 1:
        raise DocumentIndexerError(
            "min_selected_pages must be an int >= 1 "
            f"(got {type(min_selected_pages).__name__} {min_selected_pages!r})."
        )
    if type(max_selected_pages) is not int or max_selected_pages < min_selected_pages:
        raise DocumentIndexerError(
            "max_selected_pages must be an int >= min_selected_pages "
            f"(got {type(max_selected_pages).__name__} {max_selected_pages!r} with "
            f"min_selected_pages={min_selected_pages})."
        )


def classify_doc_type(has_protocol: bool, has_sap: bool) -> DocType | None:
    """Map CT.gov largeDocs boolean flags to DocType, or None for standalone ICFs."""
    if has_protocol and has_sap:
        return "combined"
    if has_protocol:
        return "protocol"
    if has_sap:
        return "sap"
    return None


def extract_in_scope_large_docs(
    study: Mapping[str, Any],
    *,
    expected_nct_id: str | None = None,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Extract in-scope protocol/SAP largeDocs from a CT.gov v2 study JSON record.

    Standalone ICFs (hasProtocol=False and hasSap=False) are excluded and counted
    in excluded_icf_count so absence of protocol/SAP is never confused with an
    unindexed trial.

    A payload listing one ``filename`` in more than one ``largeDocs`` entry is
    refused (``DocumentIndexerError``). A trial's posted file is one document
    at one URL; two entries for it (say, one flagged as the protocol and one as
    the SAP) would download it twice and record it as two documents, which a
    consumer keyed by ``(nct_id, filename)`` resolves silently one way or the
    other. CT.gov marks a combined protocol and SAP with both flags on one
    entry, so a repeated filename is malformed rather than combined, and it is
    not merged here. The message gives the entry positions, not the trial.

    The document module is read strictly; a malformed payload is refused
    (``DocumentIndexerError``), never skipped or coerced. Skipping an entry
    profiled the trial as ``no_document``; ``bool("false")`` downloaded a
    consent form as a combined protocol/SAP. Absent is still absent: no
    ``documentSection``, ``largeDocumentModule`` or ``largeDocs`` (a trial that
    posts no documents) gives no documents. Otherwise:

    - ``documentSection`` and ``largeDocumentModule`` are objects and
      ``largeDocs`` is a list;
    - every entry is an object with a non-empty string ``filename`` and
      boolean ``hasProtocol`` and ``hasSap`` (CT.gov writes both on every
      entry; a missing flag cannot be told from False);
    - an in-scope entry's filename passes ``validate_safe_pdf_filename``; its
      ``size`` is absent/null or a non-negative int; its ``uploadDate`` and
      ``date`` are absent/null or CT.gov date strings
      (``validate_ctgov_date_field``), so a date field cannot carry other
      text into the profile or url_manifest.json.

    Messages name the entry position and field, never a value.
    """
    nct_id = validate_study_payload_nct_id(
        study,
        expected_nct_id=expected_nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )

    doc_section = study.get("documentSection")
    if doc_section is None:
        return [], 0
    if not isinstance(doc_section, Mapping):
        raise DocumentIndexerError(
            f"Study payload documentSection is {type(doc_section).__name__}, not an object; "
            "the payload is refused rather than read as posting no documents."
        )
    large_mod = doc_section.get("largeDocumentModule")
    if large_mod is None:
        return [], 0
    if not isinstance(large_mod, Mapping):
        raise DocumentIndexerError(
            "Study payload documentSection.largeDocumentModule is "
            f"{type(large_mod).__name__}, not an object; the payload is refused."
        )
    raw_docs = large_mod.get("largeDocs")
    if raw_docs is None:
        return [], 0
    if not isinstance(raw_docs, list):
        raise DocumentIndexerError(
            f"Study payload largeDocs is {type(raw_docs).__name__}, not a list; the payload "
            "is refused rather than read as posting no documents."
        )

    # Shape of every entry first, before anything is classified: an object
    # with a non-empty string filename and boolean flags (see the docstring).
    for position, item in enumerate(raw_docs):
        where = f"Study payload largeDocs[{position}]"
        if not isinstance(item, Mapping):
            raise DocumentIndexerError(
                f"{where} is {type(item).__name__}, not an object; the payload is refused "
                "rather than the entry skipped."
            )
        listed = item.get("filename")
        if type(listed) is not str or not listed.strip():
            raise DocumentIndexerError(
                f"{where} has no non-empty string 'filename' (got {type(listed).__name__}); "
                "the payload is refused rather than the document skipped."
            )
        for flag in ("hasProtocol", "hasSap"):
            if type(item.get(flag)) is not bool:
                raise DocumentIndexerError(
                    f"{where} field {flag!r} is {type(item.get(flag)).__name__}, not a "
                    "boolean; it is not read with bool() (the string 'false' is truthy), so "
                    "the payload is refused."
                )

    # One posted file, one entry (see the docstring). Compared after the same
    # strip() the in-scope filename gets below, over every entry, ICFs
    # included: a file listed as an ICF and again as a protocol would
    # otherwise be both counted as excluded and indexed.
    first_position: dict[str, int] = {}
    for position, item in enumerate(raw_docs):
        key = item["filename"].strip()
        if key in first_position:
            raise DocumentIndexerError(
                f"Study payload largeDocs[{first_position[key]}] and largeDocs[{position}] "
                "name the same posted file; one file listed twice would be downloaded "
                "twice and recorded as two documents, so the payload is refused."
            )
        first_position[key] = position

    in_scope: list[dict[str, Any]] = []
    excluded_icf_count = 0
    for position, item in enumerate(raw_docs):
        where = f"Study payload largeDocs[{position}]"
        doc_type = classify_doc_type(item["hasProtocol"], item["hasSap"])
        if doc_type is None:
            # A standalone ICF is counted, not requested or recorded, so its
            # filename and dates are not examined further.
            excluded_icf_count += 1
            continue
        filename = validate_safe_pdf_filename(item["filename"].strip())
        raw_size = item.get("size")
        if raw_size is not None and (type(raw_size) is not int or raw_size < 0):
            shown = "negative int" if type(raw_size) is int else type(raw_size).__name__
            raise DocumentIndexerError(
                f"{where} field 'size' must be a non-negative int or null (got {shown}); it is "
                "not read as an unknown size."
            )
        api_size: int | None = raw_size
        upload_date = validate_ctgov_date_field(
            item.get("uploadDate"), origin=f"{where} field 'uploadDate'"
        )
        api_doc_date = validate_ctgov_date_field(item.get("date"), origin=f"{where} field 'date'")
        raw_label = item.get("label")
        label = (
            raw_label.strip()
            if isinstance(raw_label, str) and raw_label.strip()
            else None
        )
        in_scope.append(
            {
                "nct_id": nct_id,
                "filename": filename,
                "doc_type": doc_type,
                "url": build_ctgov_large_doc_url(nct_id, filename),
                "api_size": api_size,
                "api_doc_date": api_doc_date,
                "upload_date": upload_date,
                "label": label,
            }
        )

    in_scope.sort(key=lambda d: d["filename"])
    return in_scope, excluded_icf_count


# ---------------------------------------------------------------------------
# Strict readers for stored index entries
#
# ``document_index_by_sha256.jsonl`` is read back by ``DocumentCacheStore`` and
# rewritten whole by ``flush_index`` from what was read. Coercing a stored value
# (``int(576.7)``, ``int("576")``, ``str(None)``) changes the record and then
# writes the change back, with nothing said: a null ``extracted_at`` became the
# provenance stamp ``"None"``. A key the reader ignores is dropped on the next
# rewrite, and a missing one used to stop the run with a bare ``KeyError``. So
# every stored field is type-checked exactly (``bool`` is not an int), every
# object must carry exactly its schema's keys, and anything else raises
# ``CacheIntegrityError`` naming the line and field, never the value.
# ---------------------------------------------------------------------------


def _stored_object(data: Any, expected: frozenset[str], *, origin: str) -> Mapping[str, Any]:
    """Return ``data`` when it is a JSON object with exactly the ``expected`` keys; raise otherwise."""
    if not isinstance(data, Mapping):
        raise CacheIntegrityError(f"{origin} is {type(data).__name__}, not a JSON object.")
    keys = set(data.keys())
    missing = sorted(expected - keys)
    extra = keys - expected
    if missing or extra:
        named_extra = sorted(
            k for k in extra if isinstance(k, str) and _NAMEABLE_JSON_KEY_RE.fullmatch(k)
        )
        raise CacheIntegrityError(
            f"{origin} does not carry exactly its schema's keys (missing {missing}; "
            f"{len(extra)} unexpected, named: {named_extra}); a stored record is refused "
            "rather than read with defaults or rewritten without its extra keys."
        )
    return data


def _stored_int(value: Any, *, origin: str, field: str, minimum: int | None = None) -> int:
    """Return ``value`` when it is an int (not a bool) of at least ``minimum``; never coerce."""
    if type(value) is not int or (minimum is not None and value < minimum):
        bound = f" >= {minimum}" if minimum is not None else ""
        shown = f"int below {minimum}" if type(value) is int else type(value).__name__
        raise CacheIntegrityError(
            f"{origin} field {field!r} must be an int{bound} (got {shown}); it is not coerced."
        )
    return value


def _stored_float(value: Any, *, origin: str, field: str) -> float:
    """Return ``value`` as a float when it is a finite JSON number (int or float, not bool)."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise CacheIntegrityError(
            f"{origin} field {field!r} must be a finite number (got {type(value).__name__})."
        )
    return float(value)


def _stored_str(value: Any, *, origin: str, field: str, non_empty: bool = False) -> str:
    """Return ``value`` when it is a str (non-empty after strip when ``non_empty``); never ``str()`` it."""
    if type(value) is not str or (non_empty and not value.strip()):
        need = "a non-empty str" if non_empty else "a str"
        raise CacheIntegrityError(
            f"{origin} field {field!r} must be {need} (got {type(value).__name__}); a "
            "missing value is not turned into text."
        )
    return value


def _stored_bool(value: Any, *, origin: str, field: str) -> bool:
    """Return ``value`` when it is a JSON boolean; ``bool()`` of anything else is not taken."""
    if type(value) is not bool:
        raise CacheIntegrityError(
            f"{origin} field {field!r} must be a boolean (got {type(value).__name__})."
        )
    return value


def _stored_pages(value: Any, *, origin: str, field: str) -> list[int]:
    """Return ``value`` when it is a JSON list of 1-based page numbers (ints >= 1)."""
    if not isinstance(value, list):
        raise CacheIntegrityError(
            f"{origin} field {field!r} must be a list of page numbers (got {type(value).__name__})."
        )
    return [
        _stored_int(page, origin=origin, field=f"{field}[{index}]", minimum=1)
        for index, page in enumerate(value)
    ]


def _stored_choice(value: Any, allowed: Sequence[str], *, origin: str, field: str) -> str:
    """Return ``value`` when it is one of ``allowed``; the value itself is not printed."""
    if type(value) is not str or value not in allowed:
        raise CacheIntegrityError(
            f"{origin} field {field!r} is not one of the allowed values "
            f"(got {type(value).__name__})."
        )
    return value


_SELECTION_STRATEGIES: Final[tuple[SelectionStrategy, ...]] = (
    "full_readable_within_budget",
    "toc_and_headings",
    "headings_and_keywords",
    "fallback_leading_pages",
    "no_readable_pages",
)
_CONTENT_READABILITY_STATUSES: Final[tuple[ContentReadabilityStatus, ...]] = (
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
)


# ---------------------------------------------------------------------------
# Data models (Dataclasses & TypedDicts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GarbledCMapMetrics:
    text_bearing_pages: int
    garbled_page_count: int
    garbled_page_ratio: float
    pages_garbled_cmap: tuple[int, ...]
    garbled_statistical_pages: tuple[int, ...]
    max_page_punct_ctrl_ratio: float
    min_page_common_word_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "text_bearing_pages": self.text_bearing_pages,
            "garbled_page_count": self.garbled_page_count,
            "garbled_page_ratio": round(self.garbled_page_ratio, 6),
            "pages_garbled_cmap": list(self.pages_garbled_cmap),
            "garbled_statistical_pages": list(self.garbled_statistical_pages),
            "max_page_punct_ctrl_ratio": round(self.max_page_punct_ctrl_ratio, 6),
            "min_page_common_word_ratio": round(self.min_page_common_word_ratio, 6),
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, origin: str = "cmap_metrics"
    ) -> GarbledCMapMetrics:
        """Read a stored ``to_dict`` object strictly (exact keys and types; see the strict readers above)."""
        obj = _stored_object(
            data,
            frozenset(
                {
                    "text_bearing_pages",
                    "garbled_page_count",
                    "garbled_page_ratio",
                    "pages_garbled_cmap",
                    "garbled_statistical_pages",
                    "max_page_punct_ctrl_ratio",
                    "min_page_common_word_ratio",
                }
            ),
            origin=origin,
        )
        return cls(
            text_bearing_pages=_stored_int(
                obj["text_bearing_pages"], origin=origin, field="text_bearing_pages", minimum=0
            ),
            garbled_page_count=_stored_int(
                obj["garbled_page_count"], origin=origin, field="garbled_page_count", minimum=0
            ),
            garbled_page_ratio=_stored_float(
                obj["garbled_page_ratio"], origin=origin, field="garbled_page_ratio"
            ),
            pages_garbled_cmap=tuple(
                _stored_pages(obj["pages_garbled_cmap"], origin=origin, field="pages_garbled_cmap")
            ),
            garbled_statistical_pages=tuple(
                _stored_pages(
                    obj["garbled_statistical_pages"],
                    origin=origin,
                    field="garbled_statistical_pages",
                )
            ),
            max_page_punct_ctrl_ratio=_stored_float(
                obj["max_page_punct_ctrl_ratio"], origin=origin, field="max_page_punct_ctrl_ratio"
            ),
            min_page_common_word_ratio=_stored_float(
                obj["min_page_common_word_ratio"], origin=origin, field="min_page_common_word_ratio"
            ),
        )


@dataclass(frozen=True)
class RedactionSummary:
    redaction_flags: tuple[RedactionFlag, ...]
    pages_with_cci: tuple[int, ...]
    pages_with_ppd: tuple[int, ...]
    pages_with_redacted_tag: tuple[int, ...]
    pages_header_stamp_only: tuple[int, ...]
    pages_orphaned_stat_headings: tuple[int, ...]
    unreadable_statistical_pages: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "redaction_flags": list(self.redaction_flags),
            "pages_with_cci": list(self.pages_with_cci),
            "pages_with_ppd": list(self.pages_with_ppd),
            "pages_with_redacted_tag": list(self.pages_with_redacted_tag),
            "pages_header_stamp_only": list(self.pages_header_stamp_only),
            "pages_orphaned_stat_headings": list(self.pages_orphaned_stat_headings),
            "unreadable_statistical_pages": list(self.unreadable_statistical_pages),
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, origin: str = "redaction"
    ) -> RedactionSummary:
        """Read a stored ``to_dict`` object strictly (exact keys and types; see the strict readers above)."""
        page_fields = (
            "pages_with_cci",
            "pages_with_ppd",
            "pages_with_redacted_tag",
            "pages_header_stamp_only",
            "pages_orphaned_stat_headings",
            "unreadable_statistical_pages",
        )
        obj = _stored_object(
            data, frozenset({"redaction_flags", *page_fields}), origin=origin
        )
        raw_flags = obj["redaction_flags"]
        if not isinstance(raw_flags, list):
            raise CacheIntegrityError(
                f"{origin} field 'redaction_flags' must be a list (got {type(raw_flags).__name__})."
            )
        flags: list[RedactionFlag] = []
        for index, flag in enumerate(raw_flags):
            flags.append(
                _stored_choice(  # type: ignore[arg-type]
                    flag, REDACTION_FLAGS, origin=origin, field=f"redaction_flags[{index}]"
                )
            )
        pages = {
            name: tuple(_stored_pages(obj[name], origin=origin, field=name)) for name in page_fields
        }
        return cls(
            redaction_flags=tuple(flags),
            pages_with_cci=pages["pages_with_cci"],
            pages_with_ppd=pages["pages_with_ppd"],
            pages_with_redacted_tag=pages["pages_with_redacted_tag"],
            pages_header_stamp_only=pages["pages_header_stamp_only"],
            pages_orphaned_stat_headings=pages["pages_orphaned_stat_headings"],
            unreadable_statistical_pages=pages["unreadable_statistical_pages"],
        )


@dataclass(frozen=True)
class DetectedSectionRange:
    category: StatisticalSectionKind
    heading: str
    source: Literal["toc", "body_heading", "synopsis_block"]
    start_page: int
    end_page: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "heading": self.heading,
            "source": self.source,
            "start_page": self.start_page,
            "end_page": self.end_page,
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, origin: str = "detected section"
    ) -> DetectedSectionRange:
        """Read a stored ``to_dict`` object strictly (exact keys and types; see the strict readers above)."""
        obj = _stored_object(
            data,
            frozenset({"category", "heading", "source", "start_page", "end_page"}),
            origin=origin,
        )
        category = _stored_choice(
            obj["category"], STATISTICAL_SECTION_KINDS, origin=origin, field="category"
        )
        source = _stored_choice(
            obj["source"], ("toc", "body_heading", "synopsis_block"), origin=origin, field="source"
        )
        return cls(
            category=category,  # type: ignore[arg-type]
            heading=_stored_str(obj["heading"], origin=origin, field="heading"),
            source=source,  # type: ignore[arg-type]
            start_page=_stored_int(obj["start_page"], origin=origin, field="start_page", minimum=1),
            end_page=_stored_int(obj["end_page"], origin=origin, field="end_page", minimum=1),
        )


@dataclass(frozen=True)
class SectionSliceResult:
    physical_pages: int
    pages_selected: list[int]
    pages_deferred: list[int]
    pages_without_text: list[int]
    unreadable_statistical_pages: list[int]
    has_toc: bool
    toc_pages: list[int]
    printed_page_offset: int | None
    detected_sections: list[DetectedSectionRange]
    selection_strategy: SelectionStrategy

    def to_dict(self) -> dict[str, Any]:
        return {
            "physical_pages": self.physical_pages,
            "pages_selected": list(self.pages_selected),
            "pages_deferred": list(self.pages_deferred),
            "pages_without_text": list(self.pages_without_text),
            "unreadable_statistical_pages": list(self.unreadable_statistical_pages),
            "has_toc": self.has_toc,
            "toc_pages": list(self.toc_pages),
            "printed_page_offset": self.printed_page_offset,
            "detected_sections": [sec.to_dict() for sec in self.detected_sections],
            "selection_strategy": self.selection_strategy,
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, origin: str = "section_slice"
    ) -> SectionSliceResult:
        """Read a stored ``to_dict`` object strictly (exact keys and types; see the strict readers above).

        ``printed_page_offset`` must be present: null (no offset) and an int
        are both stored values; an absent key is not read as null.
        """
        obj = _stored_object(
            data,
            frozenset(
                {
                    "physical_pages",
                    "pages_selected",
                    "pages_deferred",
                    "pages_without_text",
                    "unreadable_statistical_pages",
                    "has_toc",
                    "toc_pages",
                    "printed_page_offset",
                    "detected_sections",
                    "selection_strategy",
                }
            ),
            origin=origin,
        )
        offset = obj["printed_page_offset"]
        raw_sections = obj["detected_sections"]
        if not isinstance(raw_sections, list):
            raise CacheIntegrityError(
                f"{origin} field 'detected_sections' must be a list "
                f"(got {type(raw_sections).__name__})."
            )
        return cls(
            physical_pages=_stored_int(
                obj["physical_pages"], origin=origin, field="physical_pages", minimum=0
            ),
            pages_selected=_stored_pages(obj["pages_selected"], origin=origin, field="pages_selected"),
            pages_deferred=_stored_pages(obj["pages_deferred"], origin=origin, field="pages_deferred"),
            pages_without_text=_stored_pages(
                obj["pages_without_text"], origin=origin, field="pages_without_text"
            ),
            unreadable_statistical_pages=_stored_pages(
                obj["unreadable_statistical_pages"],
                origin=origin,
                field="unreadable_statistical_pages",
            ),
            has_toc=_stored_bool(obj["has_toc"], origin=origin, field="has_toc"),
            toc_pages=_stored_pages(obj["toc_pages"], origin=origin, field="toc_pages"),
            printed_page_offset=(
                None
                if offset is None
                else _stored_int(offset, origin=origin, field="printed_page_offset")
            ),
            detected_sections=[
                DetectedSectionRange.from_dict(item, origin=f"{origin} detected_sections[{index}]")
                for index, item in enumerate(raw_sections)
            ],
            selection_strategy=_stored_choice(  # type: ignore[arg-type]
                obj["selection_strategy"],
                _SELECTION_STRATEGIES,
                origin=origin,
                field="selection_strategy",
            ),
        )


@dataclass(frozen=True)
class DocumentIndexEntry:
    sha256: str
    byte_count: int
    extracted_at: str
    pipeline_commit: str | None
    indexer_rules_version: str
    text_provider: str
    physical_pages: int
    pages_without_text: tuple[int, ...]
    readability_status: ContentReadabilityStatus
    unreadable_recovery_bucket: UnreadableRecoveryBucket | None
    cmap_metrics: GarbledCMapMetrics
    redaction: RedactionSummary
    version_label: str
    version_date_as_printed: str
    section_slice: SectionSliceResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "extracted_at": self.extracted_at,
            "pipeline_commit": self.pipeline_commit,
            "indexer_rules_version": self.indexer_rules_version,
            "text_provider": self.text_provider,
            "physical_pages": self.physical_pages,
            "pages_without_text": list(self.pages_without_text),
            "readability_status": self.readability_status,
            "unreadable_recovery_bucket": self.unreadable_recovery_bucket,
            "cmap_metrics": self.cmap_metrics.to_dict(),
            "redaction": self.redaction.to_dict(),
            "version_label": self.version_label,
            "version_date_as_printed": self.version_date_as_printed,
            "section_slice": self.section_slice.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, origin: str = "document index entry"
    ) -> DocumentIndexEntry:
        """Read one stored index entry strictly; ``CacheIntegrityError`` on anything ``to_dict`` would not write.

        Exact keys at every level, exact types (no ``int()``/``str()``
        coercion), a lowercase 64-hex ``sha256``, a positive ``byte_count``
        and ``physical_pages``, non-empty provenance strings
        (``extracted_at``, ``indexer_rules_version``, ``text_provider``) and a
        ``pipeline_commit`` that is a non-empty str or null. ``origin`` names
        the file and line in messages; no stored value is printed.
        """
        obj = _stored_object(
            data,
            frozenset(
                {
                    "sha256",
                    "byte_count",
                    "extracted_at",
                    "pipeline_commit",
                    "indexer_rules_version",
                    "text_provider",
                    "physical_pages",
                    "pages_without_text",
                    "readability_status",
                    "unreadable_recovery_bucket",
                    "cmap_metrics",
                    "redaction",
                    "version_label",
                    "version_date_as_printed",
                    "section_slice",
                }
            ),
            origin=origin,
        )
        sha = obj["sha256"]
        if type(sha) is not str or not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError(
                f"{origin} field 'sha256' is not 64 lowercase hex characters "
                f"(got {type(sha).__name__})."
            )
        status = _stored_choice(
            obj["readability_status"],
            _CONTENT_READABILITY_STATUSES,
            origin=origin,
            field="readability_status",
        )
        bucket = obj["unreadable_recovery_bucket"]
        if bucket is not None:
            bucket = _stored_choice(
                bucket,
                UNREADABLE_RECOVERY_BUCKETS,
                origin=origin,
                field="unreadable_recovery_bucket",
            )
        commit = obj["pipeline_commit"]
        if commit is not None:
            commit = _stored_str(commit, origin=origin, field="pipeline_commit", non_empty=True)
        return cls(
            sha256=sha,
            byte_count=_stored_int(obj["byte_count"], origin=origin, field="byte_count", minimum=1),
            extracted_at=_stored_str(
                obj["extracted_at"], origin=origin, field="extracted_at", non_empty=True
            ),
            pipeline_commit=commit,
            indexer_rules_version=_stored_str(
                obj["indexer_rules_version"],
                origin=origin,
                field="indexer_rules_version",
                non_empty=True,
            ),
            text_provider=_stored_str(
                obj["text_provider"], origin=origin, field="text_provider", non_empty=True
            ),
            physical_pages=_stored_int(
                obj["physical_pages"], origin=origin, field="physical_pages", minimum=1
            ),
            pages_without_text=tuple(
                _stored_pages(obj["pages_without_text"], origin=origin, field="pages_without_text")
            ),
            readability_status=status,  # type: ignore[arg-type]
            unreadable_recovery_bucket=bucket,  # type: ignore[arg-type]
            cmap_metrics=GarbledCMapMetrics.from_dict(
                obj["cmap_metrics"], origin=f"{origin} cmap_metrics"
            ),
            redaction=RedactionSummary.from_dict(obj["redaction"], origin=f"{origin} redaction"),
            version_label=_stored_str(obj["version_label"], origin=origin, field="version_label"),
            version_date_as_printed=_stored_str(
                obj["version_date_as_printed"], origin=origin, field="version_date_as_printed"
            ),
            section_slice=SectionSliceResult.from_dict(
                obj["section_slice"], origin=f"{origin} section_slice"
            ),
        )


class ParsedPartialDate(TypedDict):
    raw: str | None
    iso_normalized: str | None
    date_precision: DatePrecision
    earliest_iso: str | None
    latest_iso: str | None


class UploadTimingDelta(TypedDict):
    exact_days: int | None
    min_days: int | None
    max_days: int | None
    uploaded_precision: DatePrecision
    anchor_precision: DatePrecision
    date_precision: DatePrecision
    relation: TimingRelation


class UploadTimingFacts(TypedDict):
    uploaded_date: ParsedPartialDate
    primary_completion_date: ParsedPartialDate
    results_first_post_date: ParsedPartialDate
    vs_primary_completion: UploadTimingDelta
    vs_results_first_post: UploadTimingDelta


class TrialDocumentRecord(TypedDict):
    """One posted document bound to one trial.

    ``download_error`` is set only when the bytes were never obtained
    (``readability_status='download_failed'``; sha256 and bytes are None).
    ``extraction_error`` is set only when the bytes were obtained but no
    provider could extract their pages (``readability_status='extraction_failed'``;
    sha256 and bytes are kept). In both cases ``physical_pages`` is None: the
    page count is unknown, not zero.
    """

    nct_id: str
    filename: str
    doc_type: DocType
    url: str
    http_status: int | None
    download_error: str | None
    extraction_error: str | None
    sha256: str | None
    bytes: int | None
    physical_pages: int | None
    pages_without_text: list[int]
    readability_status: DocumentReadabilityStatus
    unreadable_recovery_bucket: UnreadableRecoveryBucket | None
    redaction_markers: list[RedactionFlag]
    version_label: str
    version_date_as_printed: str
    api_doc_date: str | None
    upload_date: str | None
    upload_timing: UploadTimingFacts
    pages_selected: list[int]
    pages_deferred: list[int]
    unreadable_statistical_pages: list[int]
    retrieved_at: str
    cache_hit: bool


class TrialDocumentProfile(TypedDict):
    nct_id: str
    indexer_rules_version: str
    extracted_at: str
    pipeline_commit: str | None
    readability_summary: DocumentReadabilityStatus
    redaction_flags: list[RedactionFlag]
    excluded_icf_count: int
    document_badge: dict[str, Any]
    documents: list[TrialDocumentRecord]


# ---------------------------------------------------------------------------
# Upload-timing date parser and precision-aware delta calculator
# ---------------------------------------------------------------------------

# Every pattern is matched whole (``fullmatch``) over ASCII digits only. The
# time of day after a date is read only in the shapes CT.gov writes (``HH:MM``,
# optionally ``:SS``, fractional seconds and a ``Z`` or ``+HH:MM`` offset, after
# a single ``T`` or space): it was once ``(?:[T\s].*)?``, which read
# ``"2020-01-01 <trial id>"`` as a day-precision date and carried the text
# after the date into the profile. The time is checked for range and dropped;
# only the calendar date is used.
_CTGOV_SLASH_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?", re.ASCII
)
_ISO_DAY_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?",
    re.ASCII,
)
_ISO_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"(\d{4})-(\d{2})", re.ASCII)
_ISO_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"(\d{4})", re.ASCII)


def _clock_in_range(hour: str | None, minute: str | None, second: str | None) -> bool:
    """True when an optional ``HH:MM[:SS]`` read by the date patterns is a real time of day."""
    if hour is None:
        return True
    if not 0 <= int(hour) <= 23 or minute is None or not 0 <= int(minute) <= 59:
        return False
    return second is None or 0 <= int(second) <= 59


def parse_ctgov_date(raw: str | None) -> ParsedPartialDate:
    """Parse a CT.gov ISO partial date or NLM uploadDate ('MM/DD/YYYY HH:MM').

    Partial dates (YYYY-MM, YYYY) record their true calendar [earliest_iso,
    latest_iso] interval (leap-year aware) and preserve date_precision rather
    than fabricating a '-01' day suffix.

    The whole string (after ``strip``) must be one of the accepted shapes
    (see the patterns above); anything else, including a valid date followed
    by other text, is ``date_precision='unknown'``. Callers that copy a date
    into a record refuse an unknown one (``validate_ctgov_date_field``,
    ``_census_optional_date``) rather than carry its text.
    """
    if not isinstance(raw, str) or not raw.strip():
        return {
            "raw": raw if isinstance(raw, str) else None,
            "iso_normalized": None,
            "date_precision": "unknown",
            "earliest_iso": None,
            "latest_iso": None,
        }

    cleaned = raw.strip()

    # ClinicalTrials.gov API v2 largeDocs[].uploadDate format: "MM/DD/YYYY HH:MM"
    m_slash = _CTGOV_SLASH_DATE_RE.fullmatch(cleaned)
    if m_slash:
        month, day, year = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        try:
            if not _clock_in_range(m_slash.group(4), m_slash.group(5), m_slash.group(6)):
                raise ValueError("time of day out of range")
            parsed = date(year, month, day)
        except ValueError:
            return {
                "raw": raw,
                "iso_normalized": None,
                "date_precision": "unknown",
                "earliest_iso": None,
                "latest_iso": None,
            }
        iso = parsed.isoformat()
        return {
            "raw": raw,
            "iso_normalized": iso,
            "date_precision": "day",
            "earliest_iso": iso,
            "latest_iso": iso,
        }

    # ISO day: "YYYY-MM-DD", optionally with a time of day (see the pattern)
    m_day = _ISO_DAY_RE.fullmatch(cleaned)
    if m_day:
        year, month, day = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
        try:
            if not _clock_in_range(m_day.group(4), m_day.group(5), m_day.group(6)):
                raise ValueError("time of day out of range")
            parsed = date(year, month, day)
        except ValueError:
            return {
                "raw": raw,
                "iso_normalized": None,
                "date_precision": "unknown",
                "earliest_iso": None,
                "latest_iso": None,
            }
        iso = parsed.isoformat()
        return {
            "raw": raw,
            "iso_normalized": iso,
            "date_precision": "day",
            "earliest_iso": iso,
            "latest_iso": iso,
        }

    # ISO month: "YYYY-MM"
    m_month = _ISO_MONTH_RE.fullmatch(cleaned)
    if m_month:
        year, month = int(m_month.group(1)), int(m_month.group(2))
        if 1 <= month <= 12 and 1 <= year <= 9999:
            last_day = calendar.monthrange(year, month)[1]
            return {
                "raw": raw,
                "iso_normalized": f"{year:04d}-{month:02d}",
                "date_precision": "month",
                "earliest_iso": f"{year:04d}-{month:02d}-01",
                "latest_iso": f"{year:04d}-{month:02d}-{last_day:02d}",
            }

    # ISO year: "YYYY"
    m_year = _ISO_YEAR_RE.fullmatch(cleaned)
    if m_year:
        year = int(m_year.group(1))
        if 1 <= year <= 9999:
            return {
                "raw": raw,
                "iso_normalized": f"{year:04d}",
                "date_precision": "year",
                "earliest_iso": f"{year:04d}-01-01",
                "latest_iso": f"{year:04d}-12-31",
            }

    return {
        "raw": raw,
        "iso_normalized": None,
        "date_precision": "unknown",
        "earliest_iso": None,
        "latest_iso": None,
    }


def validate_ctgov_date_field(value: Any, *, origin: str) -> str | None:
    """Return a payload date field as a stripped CT.gov date string, or None when it is absent.

    ``None`` (an absent or null field) is recorded as absent. A string must
    read as a CT.gov date under ``parse_ctgov_date`` once stripped; the
    stripped string is returned. Anything else (an empty string, a string
    that is not a date or carries text after one, an object, a list, a
    number) raises ``DocumentIndexerError``: copied as given (or ``str()``-ed,
    as the status dates once were) it would carry its text, a holdout trial's
    id included, into the profile and url_manifest.json. ``origin`` names the
    field's position; the value is never printed.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise DocumentIndexerError(
            f"{origin} must be a CT.gov date string or null (got {type(value).__name__}); "
            "it is not coerced or copied (the value is not printed)."
        )
    cleaned = value.strip()
    if parse_ctgov_date(cleaned)["date_precision"] == "unknown":
        raise DocumentIndexerError(
            f"{origin} is not a CT.gov date (YYYY, YYYY-MM, YYYY-MM-DD with an optional "
            "time of day, or MM/DD/YYYY[ HH:MM]); the payload is refused (the value is not "
            "printed)."
        )
    return cleaned


def _check_record_date(value: Any, *, field: str) -> None:
    """Raise unless a record's date argument is None or a string ``parse_ctgov_date`` reads as a date.

    ``build_trial_document_record`` copies these into the profile as given;
    the value is checked, not changed (a caller may keep its own spelling).
    """
    if value is None:
        return
    if type(value) is not str or parse_ctgov_date(value)["date_precision"] == "unknown":
        raise DocumentIndexerError(
            f"Trial document record field {field!r} must be a CT.gov date string or None "
            f"(got {type(value).__name__} that is not one); it is not copied into the "
            "profile (the value is not printed)."
        )


def _compute_delta(
    uploaded: ParsedPartialDate,
    anchor: ParsedPartialDate,
) -> UploadTimingDelta:
    u_prec = uploaded["date_precision"]
    a_prec = anchor["date_precision"]
    combined_prec = _RANK_TO_PRECISION[min(_PRECISION_RANK[u_prec], _PRECISION_RANK[a_prec])]

    if (
        u_prec == "unknown"
        or a_prec == "unknown"
        or uploaded["earliest_iso"] is None
        or uploaded["latest_iso"] is None
        or anchor["earliest_iso"] is None
        or anchor["latest_iso"] is None
    ):
        return {
            "exact_days": None,
            "min_days": None,
            "max_days": None,
            "uploaded_precision": u_prec,
            "anchor_precision": a_prec,
            "date_precision": "unknown",
            "relation": "unknown_missing_date",
        }

    u_early = date.fromisoformat(uploaded["earliest_iso"])
    u_late = date.fromisoformat(uploaded["latest_iso"])
    a_early = date.fromisoformat(anchor["earliest_iso"])
    a_late = date.fromisoformat(anchor["latest_iso"])

    # Signed convention: (uploaded - anchor).days
    min_days = (u_early - a_late).days
    max_days = (u_late - a_early).days

    if u_prec == "day" and a_prec == "day":
        exact_days: int | None = min_days
        if min_days < 0:
            relation: TimingRelation = "before"
        elif min_days == 0:
            relation = "same_day"
        else:
            relation = "after"
    else:
        exact_days = None
        if max_days < 0:
            relation = "before"
        elif min_days > 0:
            relation = "after"
        else:
            relation = "ambiguous_partial_date"

    return {
        "exact_days": exact_days,
        "min_days": min_days,
        "max_days": max_days,
        "uploaded_precision": u_prec,
        "anchor_precision": a_prec,
        "date_precision": combined_prec,
        "relation": relation,
    }


def compute_upload_timing(
    uploaded_date: str | None,
    *,
    primary_completion_date: str | None,
    results_first_post_date: str | None,
) -> UploadTimingFacts:
    """Compute strictly factual signed day deltas between document upload and trial dates.

    Negative values indicate upload prior to the milestone; zero indicates the
    same calendar day; positive values indicate upload after the milestone.
    Partial dates produce exact_days=None with interval [min_days, max_days].
    """
    parsed_upload = parse_ctgov_date(uploaded_date)
    parsed_completion = parse_ctgov_date(primary_completion_date)
    parsed_results = parse_ctgov_date(results_first_post_date)
    return {
        "uploaded_date": parsed_upload,
        "primary_completion_date": parsed_completion,
        "results_first_post_date": parsed_results,
        "vs_primary_completion": _compute_delta(parsed_upload, parsed_completion),
        "vs_results_first_post": _compute_delta(parsed_upload, parsed_results),
    }


# ---------------------------------------------------------------------------
# Calibrated CMap garbling detector
# ---------------------------------------------------------------------------

_ALPHA_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]{2,}")


@dataclass(frozen=True)
class PageCMapAnalysis:
    is_garbled: bool
    non_ws_chars: int
    digit_ratio: float
    punct_ctrl_ratio: float
    ctrl_pua_ratio: float
    common_word_ratio: float
    vowel_ratio: float
    unspaced_run_ratio: float
    word_count: int


def analyze_page_cmap(page_text: str) -> PageCMapAnalysis:
    """Analyze a single page's extracted text for broken PDF ToUnicode CMap garbling.

    Calibrated empirically against clinical trial PDFs:
    - Exempts dense numeric tables (digit_ratio >= 0.65).
    - Preserves clean Greek/math biostatistics prose (high common_word_ratio).
    - Catches short repeated-symbol pages (e.g. '!\\n!\\n!'),
      punctuation-shifted CMaps, and uppercase Caesar-shifted
      glyph-index CMaps ('DGPLQLVWUDWLRQ').
    """
    non_ws = "".join(ch for ch in page_text if not ch.isspace())
    n = len(non_ws)
    if n < 5:
        return PageCMapAnalysis(
            is_garbled=False,
            non_ws_chars=n,
            digit_ratio=0.0,
            punct_ctrl_ratio=0.0,
            ctrl_pua_ratio=0.0,
            common_word_ratio=0.0,
            vowel_ratio=0.0,
            unspaced_run_ratio=0.0,
            word_count=0,
        )

    n_digits = sum(1 for ch in non_ws if ch.isdigit())
    n_alpha = sum(1 for ch in non_ws if ch.isalpha())
    n_ctrl_pua = sum(
        1
        for ch in non_ws
        if ord(ch) < 32
        or (0x7F <= ord(ch) <= 0x9F)
        or (0xE000 <= ord(ch) <= 0xF8FF)
        or ch == "\ufffd"
    )
    n_punct_ctrl = sum(1 for ch in non_ws if not ch.isalnum())

    digit_ratio = n_digits / n
    punct_ctrl_ratio = n_punct_ctrl / n
    ctrl_pua_ratio = n_ctrl_pua / n

    words = _ALPHA_WORD_RE.findall(page_text)
    word_count = len(words)
    common_hits = sum(1 for w in words if w.lower() in CALIBRATED_COMMON_WORDS)
    common_word_ratio = (common_hits / word_count) if word_count > 0 else 0.0

    ascii_letters = [ch.lower() for ch in non_ws if "a" <= ch.lower() <= "z"]
    vowel_ratio = (
        sum(1 for ch in ascii_letters if ch in "aeiou") / len(ascii_letters)
        if ascii_letters
        else 0.0
    )

    unspaced_chars = 0
    for token in page_text.split():
        low = token.lower()
        if low.startswith(("http://", "https://", "www.")) or "doi.org" in low:
            continue
        if len(token) >= 25:
            unspaced_chars += len(token)
    unspaced_run_ratio = unspaced_chars / n

    if digit_ratio >= 0.65:
        is_garbled = False
    elif 5 <= n < 15:
        is_garbled = len(set(non_ws)) <= 2 and n_alpha == 0 and n_digits == 0
    elif 15 <= n < 40:
        is_garbled = (
            digit_ratio < 0.50
            and punct_ctrl_ratio > 0.60
            and common_word_ratio == 0.0
        )
    else:
        is_garbled = (
            (ctrl_pua_ratio > 0.15 and common_word_ratio < 0.10)
            or (
                common_word_ratio < 0.04
                and (
                    punct_ctrl_ratio > 0.22
                    or vowel_ratio < 0.24
                    or unspaced_run_ratio > 0.20
                    or word_count < 3
                )
            )
        )

    return PageCMapAnalysis(
        is_garbled=is_garbled,
        non_ws_chars=n,
        digit_ratio=digit_ratio,
        punct_ctrl_ratio=punct_ctrl_ratio,
        ctrl_pua_ratio=ctrl_pua_ratio,
        common_word_ratio=common_word_ratio,
        vowel_ratio=vowel_ratio,
        unspaced_run_ratio=unspaced_run_ratio,
        word_count=word_count,
    )


def detect_garbled_cmap(
    pages: Sequence[str],
    *,
    statistical_pages: AbstractSet[int] = frozenset(),
) -> tuple[bool, GarbledCMapMetrics]:
    """Compute document-level garbled-CMap metrics and classification flag."""
    text_bearing_pages = 0
    garbled_pages: list[int] = []
    max_punct_ctrl = 0.0
    min_common_word = 1.0
    stat_text_pages: set[int] = set()

    for idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        text_bearing_pages += 1
        if idx in statistical_pages:
            stat_text_pages.add(idx)
        analysis = analyze_page_cmap(page_text)
        if analysis.punct_ctrl_ratio > max_punct_ctrl:
            max_punct_ctrl = analysis.punct_ctrl_ratio
        if analysis.common_word_ratio < min_common_word:
            min_common_word = analysis.common_word_ratio
        if analysis.is_garbled:
            garbled_pages.append(idx)

    if text_bearing_pages == 0:
        min_common_word = 0.0
        garbled_ratio = 0.0
    else:
        garbled_ratio = len(garbled_pages) / text_bearing_pages

    garbled_set = set(garbled_pages)
    garbled_stat = tuple(sorted(garbled_set & stat_text_pages))

    doc_is_garbled = False
    if text_bearing_pages > 0:
        if garbled_ratio >= 0.25 and (
            len(garbled_pages) >= 2 or text_bearing_pages == 1
        ):
            doc_is_garbled = True
        elif (
            len(stat_text_pages) > 0
            and len(garbled_stat) >= 2
            and (len(garbled_stat) / len(stat_text_pages)) >= 0.50
        ):
            doc_is_garbled = True

    metrics = GarbledCMapMetrics(
        text_bearing_pages=text_bearing_pages,
        garbled_page_count=len(garbled_pages),
        garbled_page_ratio=round(garbled_ratio, 6),
        pages_garbled_cmap=tuple(garbled_pages),
        garbled_statistical_pages=garbled_stat,
        max_page_punct_ctrl_ratio=round(max_punct_ctrl, 6),
        min_page_common_word_ratio=round(min_common_word, 6),
    )
    return doc_is_garbled, metrics


# ---------------------------------------------------------------------------
# Explicit redaction markers & running-header/stamp stripping
# ---------------------------------------------------------------------------

# Case-sensitive standalone CCI marker line or explicit [CCI] / full expansion.
# Does not match "Charlson Comorbidity Index (CCI)" in running prose.
_CCI_STANDALONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^\s*(?:[•\-*▪◦]\s*)*(?:\[\s*CCI\s*\]|\(\s*CCI\s*\)|CCI)(?:\s*[•\-*▪◦])?\s*$"
)
_CCI_INLINE_RE: Final[re.Pattern[str]] = re.compile(
    r"\[\s*CCI\s*\]|(?i:\bConfidential\s+Commercial\s+Information\b)"
)

# Case-sensitive standalone PPD marker line or explicit [PPD] / full expansion.
# Does not match "PPD, Inc." CRO references or tuberculin "purified protein derivative (PPD)".
_PPD_STANDALONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^\s*(?:[•\-*▪◦]\s*)*(?:\[\s*PPD\s*\]|PPD(?:\s+PPD)*)\s*$"
)
_PPD_INLINE_RE: Final[re.Pattern[str]] = re.compile(
    r"\[\s*PPD\s*\]|(?i:\bProtected\s+Personal\s+Data\b)"
)

_REDACTED_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)(?:\[\s*REDACTED[^\]]*\]|<\s*REDACTED[^>]*>|\bSection\s+Redacted\b|"
    r"^\s*REDACTED\s*$)"
)

_BARE_PAGE_OR_STAMP_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^(?:"
    r"page\s+\d+(?:\s*(?:of|/)\s*\d+)?"
    r"|\d+\s*(?:of|/)\s*\d+"
    r"|\d{1,4}"
    r"|[ivxlcdm]{1,6}"
    r"|confidential(?:\s+and\s+proprietary)?"
    r"|proprietary(?:\s+and\s+confidential)?"
    r"|strictly\s+confidential"
    r"|internal\s+use\s+only"
    r"|external\s+use(?:\s+only)?"
    r")$"
)

_DIGIT_NORMALIZE_RE: Final[re.Pattern[str]] = re.compile(r"\d+")

# Numbered headings and statistical heading keywords for orphaned-heading detection.
_NUMBERED_HEADING_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:Section\s+|Appendix\s+)?\d+(?:\.\d+)*\.?(?:\s+.*)?$",
    re.IGNORECASE,
)
_STAT_HEADING_KEYWORDS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:"
    r"statistical|statistics|analysis\s+populations?|populations?\s+for\s+analysis|"
    r"intent[- ]to[- ]treat|modified\s+intent[- ]to[- ]treat|per[- ]protocol|"
    r"full\s+analysis\s+set|efficacy\s+analysis|primary\s+(?:endpoint|outcome|efficacy|analysis)|"
    r"sample\s+size|power\s+calculation|interim\s+analys|stopping\s+boundar|"
    r"multiplicity|multiple\s+testing|multiple\s+comparison|hypothesis|hypotheses|"
    r"significance\s+level|alpha\s+spending|non[- ]inferiority|superiority|equivalence"
    r")\b"
)


def _is_redaction_marker_line(line: str) -> bool:
    return bool(
        _CCI_STANDALONE_RE.match(line)
        or _CCI_INLINE_RE.search(line)
        or _PPD_STANDALONE_RE.match(line)
        or _PPD_INLINE_RE.search(line)
        or _REDACTED_TAG_RE.search(line)
    )


def strip_running_headers_and_stamps(
    pages: Sequence[str],
) -> tuple[list[str], tuple[int, ...]]:
    """Strip recurring running headers/footers/stamps from a copy of page texts.

    For documents with physical_pages >= 5, lines whose digit-normalized form
    re.sub(r'\\d+', '#', ln.strip()) repeats across >= max(3, ceil(0.20 * N))
    pages (plus bare 'Page # of #' / 'CONFIDENTIAL' lines) are stripped. Explicit
    CCI/PPD/REDACTED marker lines are never stripped so heavily redacted PDFs
    are not misclassified as stamp-only scans.

    Returns (residual_pages, pages_header_stamp_only) where pages_header_stamp_only
    contains 1-based physical pages having raw_chars > 0 and residual_chars == 0.
    """
    n_pages = len(pages)
    recurring: set[str] = set()
    if n_pages >= 5:
        threshold = max(3, math.ceil(0.20 * n_pages))
        line_page_counts: dict[str, int] = {}
        for page_text in pages:
            if not page_text.strip():
                continue
            seen_on_page: set[str] = set()
            for raw_ln in page_text.splitlines():
                stripped = raw_ln.strip()
                if not stripped or _is_redaction_marker_line(stripped):
                    continue
                norm = _DIGIT_NORMALIZE_RE.sub("#", stripped)
                seen_on_page.add(norm)
            for norm in seen_on_page:
                line_page_counts[norm] = line_page_counts.get(norm, 0) + 1
        recurring = {
            norm for norm, count in line_page_counts.items() if count >= threshold
        }

    residual_pages: list[str] = []
    header_stamp_only: list[int] = []
    for idx, page_text in enumerate(pages, start=1):
        raw_stripped = page_text.strip()
        if not raw_stripped:
            residual_pages.append("")
            continue
        kept_lines: list[str] = []
        for raw_ln in page_text.splitlines():
            stripped = raw_ln.strip()
            if not stripped:
                continue
            if _is_redaction_marker_line(stripped):
                kept_lines.append(stripped)
                continue
            if _BARE_PAGE_OR_STAMP_LINE_RE.match(stripped):
                continue
            if recurring and _DIGIT_NORMALIZE_RE.sub("#", stripped) in recurring:
                continue
            kept_lines.append(stripped)
        residual = "\n".join(kept_lines).strip()
        residual_pages.append(residual)
        if not residual:
            header_stamp_only.append(idx)

    return residual_pages, tuple(header_stamp_only)


def _is_orphaned_stat_heading_page(residual_text: str) -> bool:
    """Detect non-TOC pages containing numbered statistical headings with blanked body."""
    if not residual_text.strip():
        return False
    lines = [ln.strip() for ln in residual_text.splitlines() if ln.strip()]
    if not lines:
        return False

    has_numbered_stat_heading = False
    non_heading_words = 0

    for line in lines:
        if _is_redaction_marker_line(line):
            continue
        is_numbered = bool(_NUMBERED_HEADING_LINE_RE.match(line))
        # A heading line is short (typically <= 14 words) and lacks sentence-ending
        # prose structure; a long paragraph starting with a number is body prose.
        line_words = re.findall(r"[A-Za-z0-9]+", line)
        if is_numbered and len(line_words) <= 14:
            if _STAT_HEADING_KEYWORDS_RE.search(line):
                has_numbered_stat_heading = True
        else:
            non_heading_words += len(line_words)

    return has_numbered_stat_heading and non_heading_words < 12


def detect_redactions(
    pages: Sequence[str],
    *,
    toc_pages: AbstractSet[int] = frozenset(),
    toc_statistical_pages: AbstractSet[int] = frozenset(),
    residual_pages: Sequence[str] | None = None,
    pages_header_stamp_only: Sequence[int] | None = None,
) -> RedactionSummary:
    """Detect explicit CCI/PPD/REDACTED tags and white-box blanked statistical pages."""
    if residual_pages is None or pages_header_stamp_only is None:
        res_pages, stamp_only = strip_running_headers_and_stamps(pages)
    else:
        res_pages = list(residual_pages)
        stamp_only = tuple(pages_header_stamp_only)

    pages_with_cci: list[int] = []
    pages_with_ppd: list[int] = []
    pages_with_redacted_tag: list[int] = []
    pages_orphaned_stat: list[int] = []
    pages_without_text_set: set[int] = set()
    effective_body_pages = 0

    for idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            pages_without_text_set.add(idx)
            continue
        if _CCI_STANDALONE_RE.search(page_text) or _CCI_INLINE_RE.search(page_text):
            pages_with_cci.append(idx)
        if _PPD_STANDALONE_RE.search(page_text) or _PPD_INLINE_RE.search(page_text):
            pages_with_ppd.append(idx)
        if _REDACTED_TAG_RE.search(page_text):
            pages_with_redacted_tag.append(idx)

        residual = res_pages[idx - 1]
        if residual:
            effective_body_pages += 1
            if idx not in toc_pages and _is_orphaned_stat_heading_page(residual):
                pages_orphaned_stat.append(idx)

    stamp_only_set = set(stamp_only)
    orphaned_set = set(pages_orphaned_stat)
    unreadable_stat_set = (
        set(toc_statistical_pages)
        & (pages_without_text_set | stamp_only_set | orphaned_set)
    ) | orphaned_set

    flags: list[RedactionFlag] = []
    if pages_with_cci:
        flags.append("cci")
    if pages_with_ppd:
        flags.append("ppd")
    # Pure watermark-only scans (effective_body_pages == 0) are image_only_scan
    # rather than white-box redactions unless explicit redaction tags or orphaned
    # headings are present.
    has_white_box_signal = bool(
        pages_with_redacted_tag
        or pages_orphaned_stat
        or unreadable_stat_set
        or (effective_body_pages > 0 and stamp_only)
    )
    if has_white_box_signal:
        flags.append("white_box_or_blank_stats")

    return RedactionSummary(
        redaction_flags=tuple(flags),
        pages_with_cci=tuple(pages_with_cci),
        pages_with_ppd=tuple(pages_with_ppd),
        pages_with_redacted_tag=tuple(pages_with_redacted_tag),
        pages_header_stamp_only=tuple(stamp_only),
        pages_orphaned_stat_headings=tuple(pages_orphaned_stat),
        unreadable_statistical_pages=tuple(sorted(unreadable_stat_set)),
    )


# ---------------------------------------------------------------------------
# Compact page-range formatting & deterministic TOC/statistical page slicer
# ---------------------------------------------------------------------------

_PAGE_RANGE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)(?:-(\d+))?$")


def format_compact_page_ranges(pages: Iterable[int]) -> list[str]:
    """Format 1-based page numbers into compact ascending ranges (e.g. ['1-3', '5', '7-8'])."""
    validated: set[int] = set()
    for p in pages:
        if type(p) is not int or p < 1:
            raise DocumentIndexerError(f"Page numbers must be positive ints, got {p!r}")
        validated.add(p)
    if not validated:
        return []
    ordered = sorted(validated)
    ranges: list[str] = []
    start = ordered[0]
    end = ordered[0]
    for p in ordered[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = p
            end = p
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ranges


def expand_compact_page_ranges(ranges: Iterable[str]) -> list[int]:
    """Expand compact range strings (e.g. ['1-3', '5', '7-8']) to sorted unique ints."""
    result: set[int] = set()
    for item in ranges:
        if not isinstance(item, str):
            raise DocumentIndexerError(f"Page range token must be str, got {item!r}")
        cleaned = item.strip()
        m = _PAGE_RANGE_TOKEN_RE.match(cleaned)
        if not m:
            raise DocumentIndexerError(f"Invalid page range token: {item!r}")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if start < 1 or end < start:
            raise DocumentIndexerError(f"Invalid page range bounds: {item!r}")
        result.update(range(start, end + 1))
    return sorted(result)


# TOC & Statistical section detection patterns
_TOC_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^\s*(?:table\s+of\s+contents|contents)\s*$"
)
_TOC_NUMBERED_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:Section\s+|Appendix\s+)?(\d+(?:\.\d+)*|[A-Z]\.\d+)\.?\s+"
    r"(.+?)\s*(?:\.{2,}|\s{2,})\s*(\d{1,4})\s*$",
    re.IGNORECASE,
)
_TOC_DOTTED_UNNUMBERED_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ,/&()\-:]{2,90}?)\s*\.{3,}\s*(\d{1,4})\s*$"
)
_PAGE_FOOTER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\bpage\s+(\d{1,4})\s+(?:of|/)\s+(\d{1,4})\b"
)

_SECTION_PATTERNS: Final[tuple[tuple[StatisticalSectionKind, re.Pattern[str]], ...]] = (
    (
        "synopsis",
        re.compile(
            r"(?i)\b(?:protocol\s+synopsis|study\s+synopsis|trial\s+synopsis|synopsis)\b"
        ),
    ),
    (
        "objectives_endpoints",
        re.compile(
            r"(?i)\b(?:objectives?\s+and\s+endpoints?|primary\s+(?:and\s+secondary\s+)?(?:objectives?|endpoints?|outcomes?|efficacy\s+endpoints?)|"
            r"study\s+objectives?|efficacy\s+endpoints?|immunogenicity\s+endpoints?|pharmacokinetic\s+endpoints?)\b"
        ),
    ),
    (
        "analysis_populations",
        re.compile(
            r"(?i)\b(?:analysis\s+(?:populations?|sets?)|populations?\s+for\s+analysis|"
            r"intent[- ]to[- ]treat|modified\s+intent[- ]to[- ]treat|per[- ]protocol\s+(?:population|set)|"
            r"full\s+analysis\s+set|efficacy\s+(?:population|analysis\s+set)|safety\s+population)\b"
        ),
    ),
    (
        "sample_size",
        re.compile(
            r"(?i)\b(?:sample\s+size|power\s+(?:calculation|determination|justification)|"
            r"determination\s+of\s+sample\s+size|sample\s+size\s+(?:and\s+power|justification|calculation))\b"
        ),
    ),
    (
        "interim_analysis",
        re.compile(
            r"(?i)\b(?:interim\s+analys(?:is|es)|group\s+sequential|stopping\s+(?:rules?|boundar(?:y|ies))|"
            r"futility\s+analys(?:is|es)|data\s+(?:monitoring|safety)\s+committee|dmc\s+review|alpha\s+spending)\b"
        ),
    ),
    (
        "multiplicity",
        re.compile(
            r"(?i)\b(?:multiplicity|multiple\s+(?:testing|comparisons?|endpoints?)|"
            r"type\s*(?:i|1)\s*error\s*(?:control|rate|adjustment)|family[- ]?wise\s+error|"
            r"hierarchical\s+testing|gatekeeping|bonferroni|hochberg|holm)\b"
        ),
    ),
    (
        "statistical_methods",
        re.compile(
            r"(?i)\b(?:statistical\s+(?:methods?|considerations?|analysis(?:\s+plan)?|methodology|hypotheses?)|"
            r"primary\s+(?:efficacy\s+|safety\s+|pk\s+|immunogenicity\s+)?analysis|"
            r"efficacy\s+analys(?:is|es)|hypothesis\s+testing|handling\s+of\s+missing\s+data)\b"
        ),
    ),
)

_HIGH_SIGNAL_TERM_WEIGHTS: Final[tuple[tuple[re.Pattern[str], float, StatisticalSectionKind], ...]] = (
    (
        re.compile(r"(?i)\b(?:intent[- ]to[- ]treat|modified\s+intent[- ]to[- ]treat|\bm?itt\b|full\s+analysis\s+set|\bfas\b|per[- ]protocol)\b"),
        4.0,
        "analysis_populations",
    ),
    (
        re.compile(r"(?i)\b(?:non[- ]inferiority|superiority|equivalence)\s+(?:margin|hypothesis|test|bound|limit)\b"),
        5.0,
        "statistical_methods",
    ),
    (
        re.compile(r"(?i)\b(?:two[- ]sided|one[- ]sided)\b.{0,40}\b(?:alpha|significance\s+level|0\.0[1-5]|0\.025)\b"),
        5.0,
        "statistical_methods",
    ),
    (
        re.compile(r"(?i)\b(?:multiplicity|gatekeeping|bonferroni|hochberg|holm|alpha[- ]spending|hierarchical\s+testing|family[- ]?wise\s+error)\b"),
        5.0,
        "multiplicity",
    ),
    (
        re.compile(r"(?i)\b(?:interim\s+analys(?:is|es)|stopping\s+boundar(?:y|ies)|lan[- ]demets|o['’]?brien[- ]fleming|pocock|group\s+sequential)\b"),
        5.0,
        "interim_analysis",
    ),
    (
        re.compile(r"(?i)\b(?:sample\s+size|statistical\s+power|\b(?:80|85|90|95)%\s+power\b|power\s+of\s+(?:80|85|90|95)%)\b"),
        4.5,
        "sample_size",
    ),
    (
        re.compile(r"(?i)\b(?:mmrm|cox\s+proportional|log[- ]rank|ancova|kaplan[- ]meier|cochran[- ]mantel[- ]haenszel|\bcmh\b|mixed[- ]effects?\s+model)\b"),
        3.5,
        "statistical_methods",
    ),
    (
        re.compile(r"(?i)\bprimary\s+(?:endpoint|outcome|efficacy\s+variable|objective)\b"),
        3.5,
        "objectives_endpoints",
    ),
)

_ALL_CAPS_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z0-9][A-Z0-9\s\-/:,&()]{4,90}$"
)


def _match_section_kind(text: str) -> StatisticalSectionKind | None:
    for kind, pattern in _SECTION_PATTERNS:
        if pattern.search(text):
            return kind
    return None


@dataclass(frozen=True)
class _TocEntry:
    section_num: str
    title: str
    printed_page: int
    toc_page: int


def _parse_toc_entries_on_page(page_text: str, physical_page: int) -> list[_TocEntry]:
    entries: list[_TocEntry] = []
    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m_num = _TOC_NUMBERED_LINE_RE.match(line)
        if m_num:
            sec_num = m_num.group(1).strip()
            title = re.sub(r"\s*\.{2,}\s*$", "", m_num.group(2)).strip()
            printed = int(m_num.group(3))
            if title and 1 <= printed <= 5000:
                entries.append(
                    _TocEntry(
                        section_num=sec_num,
                        title=title,
                        printed_page=printed,
                        toc_page=physical_page,
                    )
                )
            continue
        m_dot = _TOC_DOTTED_UNNUMBERED_RE.match(line)
        if m_dot:
            title = m_dot.group(1).strip()
            printed = int(m_dot.group(2))
            if title and 1 <= printed <= 5000:
                entries.append(
                    _TocEntry(
                        section_num="",
                        title=title,
                        printed_page=printed,
                        toc_page=physical_page,
                    )
                )
    return entries


def _detect_toc_blocks(
    pages: Sequence[str],
) -> tuple[list[int], list[tuple[int, int, list[_TocEntry]]]]:
    """Detect all TOC blocks across the entire document (including bundled Prot_SAP PDFs)."""
    toc_page_entries: dict[int, list[_TocEntry]] = {}
    for p_idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        entries = _parse_toc_entries_on_page(page_text, p_idx)
        has_header = any(
            _TOC_HEADER_RE.match(ln) for ln in page_text.splitlines()[:8]
        )
        if (has_header and len(entries) >= 2) or len(entries) >= 5:
            toc_page_entries[p_idx] = entries

    if not toc_page_entries:
        return [], []

    sorted_toc_pages = sorted(toc_page_entries)
    blocks: list[tuple[int, int, list[_TocEntry]]] = []
    block_start = sorted_toc_pages[0]
    block_end = sorted_toc_pages[0]
    block_entries: list[_TocEntry] = list(toc_page_entries[block_start])

    for p in sorted_toc_pages[1:]:
        # Allow contiguous pages (or a single blank page gap) within one TOC block;
        # a larger gap indicates a second TOC (e.g. bundled SAP TOC mid-PDF).
        if p <= block_end + 2:
            block_end = p
            block_entries.extend(toc_page_entries[p])
        else:
            blocks.append((block_start, block_end, block_entries))
            block_start = p
            block_end = p
            block_entries = list(toc_page_entries[p])
    blocks.append((block_start, block_end, block_entries))
    return sorted_toc_pages, blocks


def _title_prefix_tokens(title: str) -> list[str]:
    return [tok.lower() for tok in re.findall(r"[A-Za-z0-9]+", title)[:3]]


def _calibrate_toc_block_offset(
    pages: Sequence[str],
    toc_end: int,
    window_end: int,
    entries: Sequence[_TocEntry],
) -> int | None:
    """Calibrate physical_page - printed_page offset for one TOC block by weighted voting."""
    votes: dict[int, int] = {}
    search_start = toc_end + 1
    if search_start > window_end:
        return None

    # Pre-extract normalized lines per page in window for heading matching.
    page_lines: dict[int, list[str]] = {}
    for p in range(search_start, window_end + 1):
        text = pages[p - 1]
        if not text.strip():
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        page_lines[p] = lines
        # Footer anchor (+1 vote): "Page X of Y"
        for ln in lines[-5:]:
            m_foot = _PAGE_FOOTER_RE.search(ln)
            if m_foot:
                printed_num = int(m_foot.group(1))
                offset = p - printed_num
                if -5 <= offset <= window_end:
                    votes[offset] = votes.get(offset, 0) + 1

    # Body heading anchor (+3 votes): section_num + title prefix on physical page > toc_end
    for entry in entries:
        tokens = _title_prefix_tokens(entry.title)
        if not tokens:
            continue
        sec_escaped = re.escape(entry.section_num) if entry.section_num else ""
        sec_re = (
            re.compile(rf"^(?:Section\s+|Appendix\s+)?{sec_escaped}\.?\s+", re.IGNORECASE)
            if sec_escaped
            else None
        )
        matched_pages: list[int] = []
        for p, lines in page_lines.items():
            for ln in lines:
                if len(ln) > 160:
                    continue
                if sec_re is not None:
                    if not sec_re.match(ln):
                        continue
                    ln_tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", ln)]
                    # Match at least first 2 title tokens (or 1 if title is single word)
                    need = min(2, len(tokens))
                    if all(tok in ln_tokens for tok in tokens[:need]):
                        matched_pages.append(p)
                        break
                else:
                    ln_low = ln.lower()
                    if entry.title.lower()[:14] in ln_low:
                        matched_pages.append(p)
                        break
        # Vote when uniquely or cleanly anchored (avoid noisy repeated headers)
        if 1 <= len(matched_pages) <= 3:
            for p in matched_pages:
                offset = p - entry.printed_page
                if -5 <= offset <= window_end:
                    votes[offset] = votes.get(offset, 0) + 3

    if not votes:
        return None
    best_offset = sorted(votes.items(), key=lambda kv: (-kv[1], abs(kv[0])))[0][0]
    return best_offset


def select_statistical_pages(
    pages: Sequence[str],
    *,
    doc_type: DocType = "protocol",
    min_selected_pages: int = 10,
    max_selected_pages: int = 25,
) -> SectionSliceResult:
    """Select high-yield statistical pages while maintaining an exact 1..N partition.

    Invariants:
    - pages_without_text = [p for p in 1..N if not pages[p-1].strip()]
    - pages_selected and pages_deferred are strictly ascending, disjoint, and
      partition pages_with_text (no blank page is ever in pages_selected).
    - When 0 < len(pages_with_text) <= max_selected_pages, selects every
      readable page (pages_deferred == []).
    """
    if type(min_selected_pages) is not int or min_selected_pages < 1:
        raise DocumentIndexerError(
            f"min_selected_pages must be an int >= 1, got {min_selected_pages!r}"
        )
    if type(max_selected_pages) is not int or max_selected_pages < min_selected_pages:
        raise DocumentIndexerError(
            f"max_selected_pages must be >= min_selected_pages, got {max_selected_pages!r}"
        )

    n_pages = len(pages)
    pages_without_text = [p for p in range(1, n_pages + 1) if not pages[p - 1].strip()]
    pages_with_text = [p for p in range(1, n_pages + 1) if pages[p - 1].strip()]
    blank_set = set(pages_without_text)

    if n_pages == 0 or not pages_with_text:
        return SectionSliceResult(
            physical_pages=n_pages,
            pages_selected=[],
            pages_deferred=[],
            pages_without_text=pages_without_text,
            unreadable_statistical_pages=[],
            has_toc=False,
            toc_pages=[],
            printed_page_offset=None,
            detected_sections=[],
            selection_strategy="no_readable_pages",
        )

    residual_pages, header_stamp_only = strip_running_headers_and_stamps(pages)
    header_stamp_only_set = set(header_stamp_only)

    # Stage A: TOC detection and per-block printed-page offset calibration
    toc_pages, toc_blocks = _detect_toc_blocks(pages)
    toc_pages_set = set(toc_pages)
    has_toc = bool(toc_pages)

    detected_sections: list[DetectedSectionRange] = []
    toc_stat_pages: set[int] = set()
    primary_offset: int | None = None
    page_scores: dict[int, float] = {p: 0.0 for p in pages_with_text}
    category_anchor_scores: dict[StatisticalSectionKind, dict[int, float]] = {
        k: {} for k in STATISTICAL_SECTION_KINDS
    }

    for block_idx, (toc_start, toc_end, entries) in enumerate(toc_blocks):
        window_end = (
            toc_blocks[block_idx + 1][0] - 1
            if block_idx + 1 < len(toc_blocks)
            else n_pages
        )
        offset = _calibrate_toc_block_offset(pages, toc_end, window_end, entries)
        if primary_offset is None and offset is not None:
            primary_offset = offset
        effective_offset = offset if offset is not None else 0

        # Sort entries in reading order by printed page
        ordered_entries = sorted(entries, key=lambda e: (e.printed_page, e.toc_page))
        for idx_e, entry in enumerate(ordered_entries):
            kind = _match_section_kind(entry.title)
            if kind is None:
                continue
            phys_start = entry.printed_page + effective_offset
            if phys_start < 1 or phys_start > n_pages:
                continue
            # Determine section end from the next TOC entry on a later printed page
            next_phys: int | None = None
            for later in ordered_entries[idx_e + 1 :]:
                if later.printed_page > entry.printed_page:
                    next_phys = later.printed_page + effective_offset
                    break
            if next_phys is not None and next_phys > phys_start:
                phys_end = min(window_end, max(phys_start, next_phys - 1), phys_start + 15)
            else:
                phys_end = min(window_end, phys_start + 4)
            phys_end = max(phys_start, min(n_pages, phys_end))

            detected_sections.append(
                DetectedSectionRange(
                    category=kind,
                    heading=f"{entry.section_num} {entry.title}".strip(),
                    source="toc",
                    start_page=phys_start,
                    end_page=phys_end,
                )
            )
            for p in range(phys_start, phys_end + 1):
                toc_stat_pages.add(p)
                if p in page_scores and p not in toc_pages_set:
                    bonus = 18.0 if p == phys_start else 12.0
                    page_scores[p] += bonus
                    category_anchor_scores[kind][p] = (
                        category_anchor_scores[kind].get(p, 0.0) + bonus
                    )

    # Stage B: Direct body headings, synopsis blocks, and high-signal term scoring
    orphaned_stat_pages: set[int] = set()
    body_section_seen: set[tuple[StatisticalSectionKind, int]] = set()
    any_signal_found = bool(detected_sections)

    for p in pages_with_text:
        if p in toc_pages_set:
            continue
        residual = residual_pages[p - 1]
        if residual and _is_orphaned_stat_heading_page(residual):
            orphaned_stat_pages.add(p)

        text = pages[p - 1]
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in lines:
            if len(ln) > 130:
                continue
            is_heading_shape = bool(
                _NUMBERED_HEADING_LINE_RE.match(ln)
                or _ALL_CAPS_HEADING_RE.match(ln)
                or re.match(r"(?i)^(?:protocol|study|trial)\s+synopsis\b", ln)
            )
            if not is_heading_shape:
                continue
            kind = _match_section_kind(ln)
            if kind is None:
                continue
            any_signal_found = True
            source_kind: Literal["body_heading", "synopsis_block"] = (
                "synopsis_block" if kind == "synopsis" else "body_heading"
            )
            if (kind, p) not in body_section_seen:
                body_section_seen.add((kind, p))
                detected_sections.append(
                    DetectedSectionRange(
                        category=kind,
                        heading=ln[:120],
                        source=source_kind,
                        start_page=p,
                        end_page=p,
                    )
                )
            page_scores[p] += 10.0
            category_anchor_scores[kind][p] = (
                category_anchor_scores[kind].get(p, 0.0) + 10.0
            )

        # High-signal statistical decision-rule keywords
        for kw_re, weight, kw_kind in _HIGH_SIGNAL_TERM_WEIGHTS:
            if kw_re.search(text):
                any_signal_found = True
                page_scores[p] += weight
                category_anchor_scores[kw_kind][p] = (
                    category_anchor_scores[kw_kind].get(p, 0.0) + weight
                )

    unreadable_statistical_pages = sorted(
        (toc_stat_pages & (blank_set | header_stamp_only_set | orphaned_stat_pages))
        | orphaned_stat_pages
    )

    # Short documents: every readable page fits within max_selected_pages
    if len(pages_with_text) <= max_selected_pages:
        return SectionSliceResult(
            physical_pages=n_pages,
            pages_selected=list(pages_with_text),
            pages_deferred=[],
            pages_without_text=pages_without_text,
            unreadable_statistical_pages=unreadable_statistical_pages,
            has_toc=has_toc,
            toc_pages=toc_pages,
            printed_page_offset=primary_offset,
            detected_sections=detected_sections,
            selection_strategy="full_readable_within_budget",
        )

    # No headings/TOC/keywords matched at all -> deterministic fallback to leading pages
    if not any_signal_found:
        fallback_selected = pages_with_text[:min_selected_pages]
        fallback_set = set(fallback_selected)
        fallback_deferred = [p for p in pages_with_text if p not in fallback_set]
        return SectionSliceResult(
            physical_pages=n_pages,
            pages_selected=fallback_selected,
            pages_deferred=fallback_deferred,
            pages_without_text=pages_without_text,
            unreadable_statistical_pages=unreadable_statistical_pages,
            has_toc=has_toc,
            toc_pages=toc_pages,
            printed_page_offset=primary_offset,
            detected_sections=detected_sections,
            selection_strategy="fallback_leading_pages",
        )

    # Category-coverage bonus (+20.0 to top anchor page of each present category)
    # ensures one long statistical appendix cannot crowd out other categories.
    for kind in STATISTICAL_SECTION_KINDS:
        cat_pages = category_anchor_scores[kind]
        if not cat_pages:
            continue
        top_page = sorted(cat_pages.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        page_scores[top_page] += 20.0

    # Slight cover-page bonus (+5.0) for version label/date context on first readable page
    first_readable = pages_with_text[0]
    if first_readable not in toc_pages_set:
        page_scores[first_readable] += 5.0

    # SAP documents get a small boost for early statistical setup pages
    if doc_type == "sap":
        for p in pages_with_text[:3]:
            if p not in toc_pages_set:
                page_scores[p] += 2.0

    positive_pages = [p for p in pages_with_text if page_scores[p] > 0.0]
    ranked = sorted(positive_pages, key=lambda p: (-page_scores[p], p))
    selected_set = set(ranked[:max_selected_pages])

    # Pad to min_selected_pages if fewer positive-score pages were found
    if len(selected_set) < min_selected_pages:
        for p in pages_with_text:
            if p not in selected_set and p not in toc_pages_set:
                selected_set.add(p)
                if len(selected_set) >= min_selected_pages:
                    break
        if len(selected_set) < min_selected_pages:
            for p in pages_with_text:
                if p not in selected_set:
                    selected_set.add(p)
                    if len(selected_set) >= min_selected_pages:
                        break

    pages_selected = sorted(selected_set)
    pages_deferred = [p for p in pages_with_text if p not in selected_set]
    has_toc_sections = has_toc and any(s.source == "toc" for s in detected_sections)
    strategy: SelectionStrategy = (
        "toc_and_headings" if has_toc_sections else "headings_and_keywords"
    )

    return SectionSliceResult(
        physical_pages=n_pages,
        pages_selected=pages_selected,
        pages_deferred=pages_deferred,
        pages_without_text=pages_without_text,
        unreadable_statistical_pages=unreadable_statistical_pages,
        has_toc=has_toc,
        toc_pages=toc_pages,
        printed_page_offset=primary_offset,
        detected_sections=detected_sections,
        selection_strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Deterministic cover/header version metadata parser
# ---------------------------------------------------------------------------

_VERSION_LABEL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?i)\b((?:protocol|sap|statistical\s+analysis\s+plan)\s+"
        r"(?:amendment|version)\s*(?:no\.?\s*)?[A-Za-z0-9][A-Za-z0-9.\-]{0,15})\b"
    ),
    re.compile(r"(?i)\b(amendment\s+(?:no\.?\s*)?\d+(?:\.\d+)?)\b"),
    re.compile(r"(?i)\b(version\s*(?:no\.?\s*)?\d+(?:\.\d+)*[A-Za-z]?)\b"),
    re.compile(r"(?i)\b(final\s+v\d+(?:\.\d+)*)\b"),
)

_MONTH_NAMES: Final[str] = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_PRINTED_DATE_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?i)\b("
    rf"\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+{_MONTH_NAMES}[\s\-,]+\d{{4}}"
    rf"|{_MONTH_NAMES}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}{_MONTH_NAMES}\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|{_MONTH_NAMES}\s+\d{{4}}"
    rf")\b"
)
_DATE_LABEL_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:date|dated|final|version\s+date|protocol\s+date|sap\s+date|amendment\s+date)\b"
)


def extract_document_version_metadata(pages: Sequence[str]) -> tuple[str, str]:
    """Deterministically extract (version_label, version_date_as_printed) from leading pages.

    Returns empty strings ('', '') when absent — never fabricates a value.
    """
    lead_texts = [p for p in pages[:3] if p.strip()]
    if not lead_texts:
        return "", ""
    head_block = "\n".join(lead_texts)

    version_label = ""
    for pat in _VERSION_LABEL_PATTERNS:
        m = pat.search(head_block)
        if m:
            version_label = " ".join(m.group(1).split())
            break

    version_date = ""
    for line in head_block.splitlines():
        if _DATE_LABEL_PREFIX_RE.search(line):
            m_date = _PRINTED_DATE_RE.search(line)
            if m_date:
                version_date = " ".join(m_date.group(1).split())
                break
    if not version_date:
        m_any = _PRINTED_DATE_RE.search(lead_texts[0])
        if m_any:
            version_date = " ".join(m_any.group(1).split())

    return version_label, version_date


# ---------------------------------------------------------------------------
# Readability classification & recovery bucket mapping
# ---------------------------------------------------------------------------


def _count_substantive_selected_pages(
    pages: Sequence[str],
    section_slice: SectionSliceResult,
) -> int:
    """Count pages_selected that carry actual statistical sections or decision-rule terms."""
    section_pages: set[int] = set()
    for sec in section_slice.detected_sections:
        section_pages.update(range(sec.start_page, sec.end_page + 1))

    count = 0
    for p in section_slice.pages_selected:
        if p in section_pages:
            count += 1
            continue
        text = pages[p - 1]
        kw_hits = sum(1 for kw_re, _, _ in _HIGH_SIGNAL_TERM_WEIGHTS if kw_re.search(text))
        if kw_hits >= 2:
            count += 1
    return count


def map_unreadable_recovery_bucket(
    readability_status: DocumentReadabilityStatus,
    redaction: RedactionSummary | None,
    pages_without_text: Sequence[int],
    pages_garbled_cmap: Sequence[int] = (),
) -> UnreadableRecoveryBucket | None:
    """Map non-machine_readable documents to Module 4's operational recovery bucket."""
    if readability_status in ("machine_readable", "no_document", "download_failed"):
        return None
    if readability_status == "garbled_cmap":
        return "garbled_cmap"
    if readability_status == "image_only_scan":
        return "image_only_scan"
    # partial_image_or_redacted:
    if len(pages_without_text) > 0:
        return "image_only_scan"
    if redaction is not None and len(redaction.redaction_flags) > 0:
        return "redacted_cci_ppd"
    if len(pages_garbled_cmap) > 0:
        return "garbled_cmap"
    return None


def index_document_pages(
    pages: Sequence[str],
    *,
    sha256: str,
    byte_count: int,
    text_provider: str,
    doc_type: DocType = "protocol",
    extracted_at: str | None = None,
    pipeline_commit: str | None = None,
    indexer_rules_version: str = INDEXER_RULES_VERSION,
    min_selected_pages: int = 10,
    max_selected_pages: int = 25,
) -> DocumentIndexEntry:
    """Classify readability/redaction and slice statistical pages for an extracted PDF."""
    sha = sha256.strip().lower()
    if not SHA256_RE.fullmatch(sha):
        raise CacheIntegrityError(f"Invalid sha256: {sha256!r}")
    if type(byte_count) is not int or byte_count <= 0:
        raise CacheIntegrityError(f"byte_count must be positive int, got {byte_count!r}")

    sanitized_pages = [sanitize_page_text(p) for p in pages]
    physical_pages = len(sanitized_pages)
    if physical_pages < 1:
        raise PageExtractionError(
            f"PDF {sha} has 0 physical pages; cannot create content index entry."
        )

    section_slice = select_statistical_pages(
        sanitized_pages,
        doc_type=doc_type,
        min_selected_pages=min_selected_pages,
        max_selected_pages=max_selected_pages,
    )
    pages_without_text = tuple(section_slice.pages_without_text)

    residual_pages, header_stamp_only = strip_running_headers_and_stamps(sanitized_pages)
    effective_body_pages = sum(1 for r in residual_pages if r)

    toc_stat_pages: set[int] = set()
    detected_stat_pages: set[int] = set()
    for sec in section_slice.detected_sections:
        rng = range(sec.start_page, sec.end_page + 1)
        detected_stat_pages.update(rng)
        if sec.source == "toc":
            toc_stat_pages.update(rng)

    redaction = detect_redactions(
        sanitized_pages,
        toc_pages=set(section_slice.toc_pages),
        toc_statistical_pages=toc_stat_pages,
        residual_pages=residual_pages,
        pages_header_stamp_only=header_stamp_only,
    )
    doc_is_garbled, cmap_metrics = detect_garbled_cmap(
        sanitized_pages,
        statistical_pages=detected_stat_pages,
    )

    substantive_selected_count = _count_substantive_selected_pages(
        sanitized_pages, section_slice
    )

    # Strict deterministic classification order (never collapsed):
    # 1. image_only_scan
    # 2. garbled_cmap
    # 3. partial_image_or_redacted
    # 4. machine_readable
    if (
        len(pages_without_text) == physical_pages
        or effective_body_pages == 0
        or (
            physical_pages >= 10
            and effective_body_pages <= 1
            and substantive_selected_count == 0
        )
    ):
        readability_status: ContentReadabilityStatus = "image_only_scan"
    elif doc_is_garbled:
        readability_status = "garbled_cmap"
    elif (
        len(pages_without_text) > 0
        or len(redaction.redaction_flags) > 0
        or len(cmap_metrics.pages_garbled_cmap) > 0
    ):
        readability_status = "partial_image_or_redacted"
    else:
        readability_status = "machine_readable"

    bucket = map_unreadable_recovery_bucket(
        readability_status,
        redaction,
        pages_without_text,
        cmap_metrics.pages_garbled_cmap,
    )

    version_label, version_date = extract_document_version_metadata(sanitized_pages)
    stamp_time = extracted_at or utc_now_iso()

    return DocumentIndexEntry(
        sha256=sha,
        byte_count=byte_count,
        extracted_at=stamp_time,
        pipeline_commit=pipeline_commit,
        indexer_rules_version=indexer_rules_version,
        text_provider=text_provider,
        physical_pages=physical_pages,
        pages_without_text=pages_without_text,
        readability_status=readability_status,
        unreadable_recovery_bucket=bucket,
        cmap_metrics=cmap_metrics,
        redaction=redaction,
        version_label=version_label,
        version_date_as_printed=version_date,
        section_slice=section_slice,
    )


# ---------------------------------------------------------------------------
# Pluggable PageTextProvider / DocumentPageExtractor classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedDocumentPages:
    sha256: str
    physical_pages: int
    pages: tuple[str, ...]
    provider_name: str


@runtime_checkable
class PageTextProvider(Protocol):
    """Single-page text lookup protocol.

    Written to match the signature of Module 4's citation_verifier.PageTextProvider.
    Module 4 is not on this branch, so the match is untested here; a branch that
    carries both modules must pin it with a test.
    """

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None: ...


@runtime_checkable
class DocumentPageExtractor(PageTextProvider, Protocol):
    """Full-document page extraction + single-page lookup protocol."""

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> ExtractedDocumentPages: ...


class FixturePageTextProvider:
    """Deterministic in-memory page provider for offline Linux/macOS CI unit tests."""

    def __init__(
        self,
        mapping: Mapping[str, Sequence[str] | BaseException],
        *,
        provider_name: str = "fixture",
    ) -> None:
        self._mapping: dict[str, Sequence[str] | BaseException] = dict(mapping)
        self._provider_name = provider_name
        self.calls: list[tuple[str, str | None, str | None]] = []

    def _resolve(
        self, sha256: str, filename: str | None = None
    ) -> Sequence[str] | BaseException | None:
        sha_low = sha256.lower()
        if sha_low in self._mapping:
            return self._mapping[sha_low]
        if sha256 in self._mapping:
            return self._mapping[sha256]
        if filename is not None and filename in self._mapping:
            return self._mapping[filename]
        return None

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> ExtractedDocumentPages:
        if nct_id is not None:
            validate_nct_id(nct_id)
        self.calls.append((sha256.lower(), nct_id, filename))
        entry = self._resolve(sha256, filename)
        if entry is None:
            raise PageExtractionError(
                f"FixturePageTextProvider has no entry for sha256={sha256!r}, filename={filename!r}"
            )
        if isinstance(entry, BaseException):
            raise entry
        sanitized = tuple(sanitize_page_text(str(p)) for p in entry)
        return ExtractedDocumentPages(
            sha256=sha256.lower(),
            physical_pages=len(sanitized),
            pages=sanitized,
            provider_name=self._provider_name,
        )

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        validate_nct_id(nct_id)
        if text_source != "pdfkit":
            return None
        entry = self._resolve(sha256, filename)
        if entry is None or isinstance(entry, BaseException):
            return None
        if 1 <= physical_page <= len(entry):
            return sanitize_page_text(str(entry[physical_page - 1]))
        return None


def check_page_map_firewall(
    obj: Mapping[str, Any],
    *,
    origin: str,
    audit_nct_ids: AbstractSet[str],
    allowed_nct_ids: AbstractSet[str] | None,
) -> str | None:
    """Apply the holdout firewall to one page map before any of its pages are kept.

    A page map is one ``--page-map-jsonl`` row or one cached
    ``pages/<sha>.json.gz`` object. Checks, in order:

    1. ``nct_id``, when present, is strict ASCII ``^NCT[0-9]{8}$``, is not in
       ``audit_nct_ids`` and, when ``allowed_nct_ids`` is supplied, is on it.
    2. ``in_audit``, when present, is boolean ``False``.
    3. A page map with no ``nct_id`` is refused whenever the firewall is
       configured (a non-empty ``audit_nct_ids`` or any ``allowed_nct_ids``):
       nothing ties its pages to a trial, so nothing can show they are not a
       holdout trial's. It is accepted only when neither set is configured.

    Returns the validated ``nct_id``, or None for an untagged map accepted under
    an unconfigured firewall. Messages name the origin (file and line), never
    the id, which may be a holdout trial's.
    """
    firewall_configured = bool(audit_nct_ids) or allowed_nct_ids is not None
    validated_nct: str | None = None
    if "nct_id" in obj:
        try:
            validated_nct = validate_nct_id(obj["nct_id"])
        except InvalidNctIdError as exc:
            raise InvalidNctIdError(f"Page map at {origin}: {exc}") from None
        if validated_nct in audit_nct_ids:
            raise AuditHoldoutError(
                f"Page map at {origin} is tagged with an audit holdout trial; "
                "loading or caching audit holdout pages is forbidden."
            )
        if allowed_nct_ids is not None and validated_nct not in allowed_nct_ids:
            raise AuditHoldoutError(
                f"Page map at {origin} is tagged with a trial not in allowed_nct_ids; "
                "unlisted trial page maps are blocked by the audit holdout firewall."
            )
    if "in_audit" in obj:
        row_in_audit = obj["in_audit"]
        if type(row_in_audit) is not bool or row_in_audit is True:
            raise AuditHoldoutError(
                f"Page map at {origin} carries disallowed in_audit "
                f"(type={type(row_in_audit).__name__}, value is not False); "
                "audit holdout rows are forbidden."
            )
    if validated_nct is None and firewall_configured:
        raise AuditHoldoutError(
            f"Page map at {origin} names no nct_id, so it cannot be checked against "
            "the audit holdout; tag every page map with the nct_id of the trial it "
            "was extracted for (or re-extract it, which tags it)."
        )
    return validated_nct


def _validated_page_list(raw_pages: Any, *, origin: str) -> tuple[str, ...]:
    """Return a page map's pages, raising unless they are a JSON list of strings.

    A non-list, or a page that is not a string, is malformed input: coercing
    ``null`` to the text ``"None"`` (as ``str(p)`` would) invents page text.
    """
    if not isinstance(raw_pages, list):
        raise CacheIntegrityError(
            f"Page map at {origin} has no 'pages' list (got {type(raw_pages).__name__})."
        )
    for index, page in enumerate(raw_pages, start=1):
        if type(page) is not str:
            raise CacheIntegrityError(
                f"Page map at {origin} page {index} is {type(page).__name__}, not a string."
            )
    return tuple(sanitize_page_text(p) for p in raw_pages)


def _validated_text_source(obj: Mapping[str, Any], *, origin: str) -> str:
    """Return a page map's ``text_source``; absent means ``"pdfkit"``, anything else must be a non-empty string."""
    if "text_source" not in obj or obj["text_source"] is None:
        return "pdfkit"
    source = obj["text_source"]
    if type(source) is not str or not source.strip():
        raise CacheIntegrityError(
            f"Page map at {origin} has an invalid text_source (type={type(source).__name__})."
        )
    return source


class JsonlPageMapProvider:
    """Offline page provider backed by pre-extracted JSONL(.gz) files and cache page maps.

    Firewall: every page map passes ``check_page_map_firewall`` before any of its
    pages are kept, so with a holdout set or an allowlist configured every row
    must name the trial it belongs to. Under either set a lookup must also name
    the requesting trial; ``nct_id=None`` is refused, as
    ``DocumentCacheStore.get_or_index_document`` refuses it.

    Pages are served for the digest they were extracted from and no other: a
    lookup for a digest with no pages is a miss, never another digest's pages
    found through the row's ``(nct_id, filename)``.

    Strict loading, no silent skips: a configured path that does not exist, a
    line that is not JSON or not an object (or that names a key twice), a
    ``sha256`` that is not 64 hex
    characters, ``pages`` that is not a list of strings, an invalid ``filename``
    or ``text_source``, and two rows giving one ``(sha256, text_source)``
    different pages all raise. Staging is all-or-nothing: if any configured file
    raises, nothing from any of them is kept.
    """

    def __init__(
        self,
        paths: Sequence[Path] = (),
        *,
        cache_pages_dir: Path | None = None,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        allowed_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        self._paths = [Path(p) for p in paths]
        self._cache_pages_dir = Path(cache_pages_dir) if cache_pages_dir is not None else None
        self._audit_nct_ids = _validate_nct_id_set(audit_nct_ids, label="audit_nct_ids")
        self._allowed_nct_ids = (
            _validate_nct_id_set(allowed_nct_ids, label="allowed_nct_ids")
            if allowed_nct_ids is not None
            else None
        )
        if self._allowed_nct_ids is not None and (self._allowed_nct_ids & self._audit_nct_ids):
            raise AuditHoldoutError(
                "JsonlPageMapProvider conflicting sets: "
                f"{len(self._allowed_nct_ids & self._audit_nct_ids)} id(s) in both "
                "allowed_nct_ids and audit_nct_ids."
            )
        self._loaded = False
        # Keyed by (sha256_lower, text_source) -> tuple[str, ...]; the only
        # index pages are served from (see _lookup_pages).
        self._by_sha_source: dict[tuple[str, str], tuple[str, ...]] = {}
        # (nct_id, filename, text_source) -> sha256_lower. Load-time consistency
        # check only (one trial's file may not map to two digests across the
        # configured rows); never used to serve pages.
        self._by_trial_file: dict[tuple[str, str, str], str] = {}

    def _validate_page_map_row(self, obj: Mapping[str, Any], *, origin: str) -> str | None:
        """Apply ``check_page_map_firewall`` with this provider's audit set and allowlist."""
        return check_page_map_firewall(
            obj,
            origin=origin,
            audit_nct_ids=self._audit_nct_ids,
            allowed_nct_ids=self._allowed_nct_ids,
        )

    def validate_loaded(self) -> None:
        """Eagerly load and validate all configured page-map JSONL files."""
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """Load every configured file once; a missing file is a configuration error, not an empty map."""
        if self._loaded:
            return
        staged_by_sha: dict[tuple[str, str], tuple[str, ...]] = {}
        staged_by_trial: dict[tuple[str, str, str], str] = {}
        for path in self._paths:
            if not path.is_file():
                # A mistyped --page-map-jsonl must not quietly leave the provider
                # with fewer pages; every document would then fall through to
                # extraction_failed with no hint of why.
                raise DocumentIndexerError(
                    f"Page-map file does not exist or is not a file: {path}"
                )
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    self._load_jsonl_stream(
                        handle,
                        origin=str(path),
                        staged_by_sha=staged_by_sha,
                        staged_by_trial=staged_by_trial,
                    )
            else:
                with path.open("r", encoding="utf-8") as handle:
                    self._load_jsonl_stream(
                        handle,
                        origin=str(path),
                        staged_by_sha=staged_by_sha,
                        staged_by_trial=staged_by_trial,
                    )
        self._by_sha_source.update(staged_by_sha)
        self._by_trial_file.update(staged_by_trial)
        self._loaded = True

    def _load_jsonl_stream(
        self,
        stream: Iterable[str],
        *,
        origin: str,
        staged_by_sha: dict[tuple[str, str], tuple[str, ...]],
        staged_by_trial: dict[tuple[str, str, str], str],
    ) -> None:
        """Validate and stage every row of one page-map file; any malformed row raises."""
        for line_no, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            where = f"{origin}:{line_no}"
            try:
                # A row naming nct_id or in_audit twice is refused, not read last-wins.
                obj = loads_strict_json(line, origin=where)
            except json.JSONDecodeError as exc:
                raise DocumentIndexerError(
                    f"Page map at {where} is not valid JSON: {exc.msg}"
                ) from None
            if not isinstance(obj, Mapping):
                raise DocumentIndexerError(
                    f"Page map at {where} must be a JSON object, got {type(obj).__name__}."
                )
            # Firewall first: nothing else about an audit row is examined.
            validated_nct = self._validate_page_map_row(obj, origin=where)
            raw_sha = obj.get("sha256")
            if type(raw_sha) is not str or not SHA256_RE.fullmatch(raw_sha.lower()):
                raise CacheIntegrityError(
                    f"Page map at {where} has no valid sha256 (64 hex characters)."
                )
            sha = raw_sha.lower()
            source = _validated_text_source(obj, origin=where)
            pages_tuple = _validated_page_list(obj.get("pages"), origin=where)
            key = (sha, source)
            previous = staged_by_sha.get(key)
            if previous is not None and previous != pages_tuple:
                raise CacheIntegrityError(
                    f"Page map at {where} gives sha256={sha} ({source}) pages that differ "
                    "from an earlier row; conflicting page maps are not resolved silently."
                )
            staged_by_sha[key] = pages_tuple
            if "filename" in obj:
                fn = obj["filename"]
                if type(fn) is not str or not fn:
                    raise DocumentIndexerError(
                        f"Page map at {where} has an invalid filename (type={type(fn).__name__})."
                    )
                safe_fn = validate_safe_pdf_filename(fn)
                if validated_nct is not None:
                    trial_key = (validated_nct, safe_fn, source)
                    mapped = staged_by_trial.get(trial_key)
                    if mapped is not None and mapped != sha:
                        raise CacheIntegrityError(
                            f"Page map at {where} maps one trial's {safe_fn} ({source}) to a "
                            "different sha256 than an earlier row."
                        )
                    staged_by_trial[trial_key] = sha

    def _load_from_cache_dir(
        self, sha256: str, *, text_source: str = "pdfkit"
    ) -> tuple[str, ...] | None:
        """Load a cached ``pages/<sha>.json.gz`` map, applying the same firewall and integrity checks."""
        if self._cache_pages_dir is None:
            return None
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            return None
        candidates = (
            self._cache_pages_dir / sha[:2] / f"{sha}.json.gz",
            self._cache_pages_dir / f"{sha}.json.gz",
            self._cache_pages_dir / f"{sha}.json",
        )
        for cand in candidates:
            if not cand.exists():
                continue
            try:
                if cand.suffix == ".gz":
                    with gzip.open(cand, "rt", encoding="utf-8") as handle:
                        obj = loads_strict_json(handle.read(), origin=str(cand))
                else:
                    with cand.open("r", encoding="utf-8") as handle:
                        obj = loads_strict_json(handle.read(), origin=str(cand))
            except (OSError, EOFError, json.JSONDecodeError) as exc:
                raise CacheIntegrityError(
                    f"Cached page map {cand} cannot be read: {type(exc).__name__}"
                ) from None
            if not isinstance(obj, Mapping):
                raise CacheIntegrityError(
                    f"Cached page map {cand} must be a JSON object, got {type(obj).__name__}."
                )
            self._validate_page_map_row(obj, origin=str(cand))
            if obj.get("sha256") != sha:
                raise CacheIntegrityError(
                    f"Cached page map {cand} does not carry its own sha256; a map filed "
                    "under one digest must describe that digest's bytes."
                )
            obj_source = _validated_text_source(obj, origin=str(cand))
            if obj_source != text_source:
                # A map for another text source (e.g. OCR) is a different
                # document view, not a malformed one: keep looking.
                continue
            pages_tuple = _validated_page_list(obj.get("pages"), origin=str(cand))
            self._by_sha_source[(sha, text_source)] = pages_tuple
            return pages_tuple
        return None

    def _lookup_pages(
        self,
        *,
        sha256: str,
        nct_id: str | None = None,
        filename: str | None = None,
        text_source: str = "pdfkit",
    ) -> tuple[str, ...] | None:
        """Serve the pages extracted from ``sha256``'s bytes, after checking the requesting trial.

        Pages are served by digest only. A document is its bytes, and a page
        map's pages describe the bytes of the digest they were extracted from;
        serving another digest's pages under the requested one (as a former
        ``(nct_id, filename)`` fallback did when the requested digest had no
        pages) labels text with a content address it does not describe, and
        lets a mislabelled row's text (a holdout trial's, say) reach an
        in-scope document's cache entry. A digest with no pages here is a miss
        (None), which ``extract_pages`` reports as ``PageExtractionError``.
        ``filename`` is validated but does not select pages.

        An unnamed lookup (``nct_id=None``) is refused under a holdout set or
        an allowlist, before any map is loaded or opened: nothing shows the
        pages are not asked for on a holdout trial's behalf, the rule an
        untagged map is held to (``DocumentCacheStore.enforce_requester``
        applies the same one). A ``ChainedPageTextProvider`` over this provider
        therefore refuses it too, with or without a cache store.
        """
        if nct_id is not None:
            enforce_not_in_audit(
                nct_id,
                in_audit=False,
                audit_nct_ids=self._audit_nct_ids,
                allowed_nct_ids=self._allowed_nct_ids,
            )
        elif self._audit_nct_ids or self._allowed_nct_ids is not None:
            raise AuditHoldoutError(
                "Explicit nct_id is required for a page-map lookup when a holdout set or "
                "allowed_nct_ids is configured."
            )
        if filename:
            validate_safe_pdf_filename(filename)
        self._ensure_loaded()
        sha = sha256.lower()
        pages = self._by_sha_source.get((sha, text_source))
        if pages is not None:
            return pages
        return self._load_from_cache_dir(sha, text_source=text_source)

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> ExtractedDocumentPages:
        pages = self._lookup_pages(
            sha256=sha256, nct_id=nct_id, filename=filename, text_source="pdfkit"
        )
        if pages is None:
            raise PageExtractionError(
                f"JsonlPageMapProvider has no cached pages for sha256={sha256!r}"
            )
        return ExtractedDocumentPages(
            sha256=sha256.lower(),
            physical_pages=len(pages),
            pages=pages,
            provider_name="jsonl_page_map",
        )

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        pages = self._lookup_pages(
            sha256=sha256, nct_id=nct_id, filename=filename, text_source=text_source
        )
        if pages is None:
            return None
        if 1 <= physical_page <= len(pages):
            return pages[physical_page - 1]
        return None


# Embedded ~55-line Objective-C PDFKit helper compiled WITHOUT -fmodules so
# sandboxed macOS builds never touch /var/folders module caches. Performs
# explicit UTF-16 getCharacters:range: lone-surrogate replacement -> U+FFFD.
_PDFKIT_HELPER_OBJC: Final[str] = r"""
#import <Foundation/Foundation.h>
#import <PDFKit/PDFKit.h>
#include <stdlib.h>

static NSString *sanitizeUTF16(NSString *src) {
    if (!src) return @"";
    NSUInteger len = [src length];
    if (len == 0) return @"";
    unichar *buf = (unichar *)malloc(len * sizeof(unichar));
    if (!buf) return @"";
    [src getCharacters:buf range:NSMakeRange(0, len)];
    for (NSUInteger i = 0; i < len; i++) {
        unichar c = buf[i];
        if (c >= 0xD800 && c <= 0xDBFF) {
            if (i + 1 < len && buf[i + 1] >= 0xDC00 && buf[i + 1] <= 0xDFFF) {
                i++;
            } else {
                buf[i] = 0xFFFD;
            }
        } else if (c >= 0xDC00 && c <= 0xDFFF) {
            buf[i] = 0xFFFD;
        }
    }
    NSString *clean = [[NSString alloc] initWithCharacters:buf length:len];
    free(buf);
    return clean ?: @"";
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "usage: pdfkit_extract <pdf_path>\n");
            return 2;
        }
        NSString *path = [NSString stringWithUTF8String:argv[1]];
        NSURL *url = [NSURL fileURLWithPath:path];
        PDFDocument *doc = [[PDFDocument alloc] initWithURL:url];
        if (!doc) {
            fprintf(stderr, "failed to open PDF\n");
            return 1;
        }
        NSUInteger count = [doc pageCount];
        NSMutableArray<NSString *> *pages = [NSMutableArray arrayWithCapacity:count];
        for (NSUInteger i = 0; i < count; i++) {
            @autoreleasepool {
                PDFPage *page = [doc pageAtIndex:i];
                NSString *raw = page ? [page string] : @"";
                [pages addObject:sanitizeUTF16(raw)];
            }
        }
        NSDictionary *payload = @{
            @"physical_pages": @(count),
            @"pages": pages,
        };
        NSError *err = nil;
        NSData *jsonData = [NSJSONSerialization dataWithJSONObject:payload options:0 error:&err];
        if (!jsonData || err) {
            fprintf(stderr, "json serialization failed\n");
            return 1;
        }
        fwrite([jsonData bytes], 1, [jsonData length], stdout);
        return 0;
    }
}
"""


def _decode_utf16_units_sanitized(units: Sequence[int]) -> str:
    """Decode UTF-16 code units into a Python str, replacing lone surrogates with U+FFFD."""
    out: list[str] = []
    i = 0
    n = len(units)
    while i < n:
        c = units[i]
        if 0xD800 <= c <= 0xDBFF:
            if i + 1 < n and 0xDC00 <= units[i + 1] <= 0xDFFF:
                codepoint = 0x10000 + ((c - 0xD800) << 10) + (units[i + 1] - 0xDC00)
                out.append(chr(codepoint))
                i += 2
                continue
            out.append("\ufffd")
            i += 1
        elif 0xDC00 <= c <= 0xDFFF:
            out.append("\ufffd")
            i += 1
        else:
            out.append(chr(c))
            i += 1
    return "".join(out)


class _NSRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_ulong), ("length", ctypes.c_ulong)]


class MacOSPDFKitProvider:
    """macOS PDFKit page extractor using ``clang`` + ``Foundation``/``PDFKit`` frameworks
    with a ``ctypes.CDLL`` fallback to ``/usr/lib/libobjc.A.dylib`` + ``PDFKit.framework``.

    Guarded by ``sys.platform == 'darwin'``. On non-macOS platforms (such as Linux CI)
    without a pre-extracted JSONL or cached page map, fails loudly with
    ``PageExtractionError`` without importing or loading macOS frameworks.
    """

    def __init__(
        self,
        *,
        helper_bin: Path | None = None,
        compile_dir: Path | None = None,
        allow_ctypes_fallback: bool = True,
        prefer_ctypes: bool = False,
        timeout_sec: int = 120,
    ) -> None:
        env_helper = os.environ.get("CIVICSAMPLE_PDFKIT_HELPER")
        if helper_bin is not None:
            self._helper_bin: Path | None = Path(helper_bin)
        elif env_helper:
            self._helper_bin = Path(env_helper)
        else:
            self._helper_bin = None
        self._compile_dir = (
            Path(compile_dir)
            if compile_dir is not None
            else Path("data/document_cache")
        )
        self._allow_ctypes_fallback = allow_ctypes_fallback
        self._prefer_ctypes = prefer_ctypes
        self._timeout_sec = timeout_sec
        self._compiled_bin: Path | None = None
        self._last_pages_cache: dict[str, tuple[str, ...]] = {}

    def _ensure_cli_helper(self) -> Path:
        if self._helper_bin is not None:
            if not self._helper_bin.exists():
                raise PageExtractionError(
                    f"Configured PDFKit helper binary does not exist: {self._helper_bin}"
                )
            return self._helper_bin
        if self._compiled_bin is not None and self._compiled_bin.exists():
            return self._compiled_bin

        src_hash = hashlib.sha256(_PDFKIT_HELPER_OBJC.encode("utf-8")).hexdigest()[:12]
        self._compile_dir.mkdir(parents=True, exist_ok=True)
        target_bin = self._compile_dir / f"pdfkit_extract_{src_hash}"
        if target_bin.exists() and os.access(target_bin, os.X_OK):
            self._compiled_bin = target_bin
            return target_bin

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._compile_dir),
            prefix=".pdfkit_extract.",
            suffix=".tmp",
        )
        os.close(fd)
        tmp_bin = Path(tmp_name)
        try:
            proc = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-O2",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "PDFKit",
                    "-",
                    "-o",
                    str(tmp_bin),
                ],
                input=_PDFKIT_HELPER_OBJC.encode("utf-8"),
                capture_output=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                raise PageExtractionError(
                    f"Failed to compile macOS PDFKit helper via clang: {stderr}"
                )
            os.chmod(tmp_bin, 0o755)
            os.replace(tmp_bin, target_bin)
        except Exception:
            try:
                tmp_bin.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        self._compiled_bin = target_bin
        return target_bin

    def _extract_via_cli(self, pdf_path: Path, sha256: str) -> ExtractedDocumentPages:
        bin_path = self._ensure_cli_helper()
        try:
            proc = subprocess.run(
                [str(bin_path), str(pdf_path)],
                capture_output=True,
                timeout=self._timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PageExtractionError(
                f"macOS PDFKit helper timed out after {self._timeout_sec}s on {pdf_path}"
            ) from exc

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise PageExtractionError(
                f"macOS PDFKit helper failed (exit {proc.returncode}) on {pdf_path}: {stderr}"
            )
        try:
            payload = json.loads(proc.stdout.decode("utf-8"))
        except Exception as exc:
            raise PageExtractionError(
                f"macOS PDFKit helper returned invalid JSON for {pdf_path}: {exc}"
            ) from exc
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list):
            raise PageExtractionError(
                f"macOS PDFKit helper output missing 'pages' list for {pdf_path}"
            )
        pages = tuple(sanitize_page_text(str(p)) for p in raw_pages)
        return ExtractedDocumentPages(
            sha256=sha256.lower(),
            physical_pages=len(pages),
            pages=pages,
            provider_name="macos_pdfkit_cli",
        )

    def _extract_via_ctypes(self, pdf_path: Path, sha256: str) -> ExtractedDocumentPages:
        """ctypes.CDLL binding to macOS libobjc.A.dylib + Foundation/PDFKit with UTF-16 surrogate sanitization."""
        try:
            objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
            ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
            ctypes.CDLL("/System/Library/Frameworks/PDFKit.framework/PDFKit")
        except OSError as exc:
            raise PageExtractionError(
                f"Unable to load macOS Objective-C/PDFKit frameworks: {exc}"
            ) from exc

        objc_getClass = objc.objc_getClass
        objc_getClass.argtypes = [ctypes.c_char_p]
        objc_getClass.restype = ctypes.c_void_p

        sel_registerName = objc.sel_registerName
        sel_registerName.argtypes = [ctypes.c_char_p]
        sel_registerName.restype = ctypes.c_void_p

        msg_ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_ptr_ptr = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        msg_ptr_cstr = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
        )(("objc_msgSend", objc))
        msg_ulong = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_ptr_ulong = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong
        )(("objc_msgSend", objc))
        msg_void = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_get_chars = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            _NSRange,
        )(("objc_msgSend", objc))

        ns_pool_cls = objc_getClass(b"NSAutoreleasePool")
        ns_string_cls = objc_getClass(b"NSString")
        ns_url_cls = objc_getClass(b"NSURL")
        pdf_doc_cls = objc_getClass(b"PDFDocument")

        sel_alloc = sel_registerName(b"alloc")
        sel_init = sel_registerName(b"init")
        sel_drain = sel_registerName(b"drain")
        sel_release = sel_registerName(b"release")
        sel_str_utf8 = sel_registerName(b"stringWithUTF8String:")
        sel_file_url = sel_registerName(b"fileURLWithPath:")
        sel_init_url = sel_registerName(b"initWithURL:")
        sel_page_count = sel_registerName(b"pageCount")
        sel_page_at = sel_registerName(b"pageAtIndex:")
        sel_string = sel_registerName(b"string")
        sel_length = sel_registerName(b"length")
        sel_get_chars = sel_registerName(b"getCharacters:range:")

        pool = msg_ptr(msg_ptr(ns_pool_cls, sel_alloc), sel_init)
        doc = None
        try:
            ns_path = msg_ptr_cstr(
                ns_string_cls, sel_str_utf8, str(pdf_path).encode("utf-8")
            )
            url = msg_ptr_ptr(ns_url_cls, sel_file_url, ns_path)
            doc = msg_ptr_ptr(msg_ptr(pdf_doc_cls, sel_alloc), sel_init_url, url)
            if not doc:
                raise PageExtractionError(
                    f"PDFKit ctypes failed to open PDF: {pdf_path}"
                )
            count = int(msg_ulong(doc, sel_page_count))
            pages: list[str] = []
            for idx in range(count):
                inner_pool = msg_ptr(msg_ptr(ns_pool_cls, sel_alloc), sel_init)
                try:
                    page = msg_ptr_ulong(doc, sel_page_at, idx)
                    if not page:
                        pages.append("")
                        continue
                    ns_text = msg_ptr(page, sel_string)
                    if not ns_text:
                        pages.append("")
                        continue
                    length = int(msg_ulong(ns_text, sel_length))
                    if length == 0:
                        pages.append("")
                        continue
                    buf = (ctypes.c_uint16 * length)()
                    msg_get_chars(ns_text, sel_get_chars, buf, _NSRange(0, length))
                    pages.append(_decode_utf16_units_sanitized(buf))
                finally:
                    msg_void(inner_pool, sel_drain)
            return ExtractedDocumentPages(
                sha256=sha256.lower(),
                physical_pages=len(pages),
                pages=tuple(pages),
                provider_name="macos_pdfkit_ctypes",
            )
        finally:
            if doc:
                msg_void(doc, sel_release)
            msg_void(pool, sel_drain)

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> ExtractedDocumentPages:
        if nct_id is not None:
            validate_nct_id(nct_id)
        if sys.platform != "darwin":
            raise PageExtractionError(
                "MacOSPDFKitProvider is only available on macOS (sys.platform == 'darwin')."
            )
        if pdf_path is None or not Path(pdf_path).exists():
            raise PageExtractionError(
                f"MacOSPDFKitProvider requires an existing pdf_path, got {pdf_path!r}"
            )
        resolved = Path(pdf_path)

        if self._prefer_ctypes:
            result = self._extract_via_ctypes(resolved, sha256)
        else:
            try:
                result = self._extract_via_cli(resolved, sha256)
            except PageExtractionError:
                if not self._allow_ctypes_fallback:
                    raise
                result = self._extract_via_ctypes(resolved, sha256)

        self._last_pages_cache[sha256.lower()] = result.pages
        return result

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        validate_nct_id(nct_id)
        if text_source != "pdfkit":
            return None
        pages = self._last_pages_cache.get(sha256.lower())
        if pages is None:
            return None
        if 1 <= physical_page <= len(pages):
            return pages[physical_page - 1]
        return None


class ChainedPageTextProvider:
    """Checks cache page maps -> JSONL page maps -> macOS PDFKit in order.

    Firewall: before the cache store answers a request, the requesting trial
    is checked against the store's own holdout set and allowlist
    (``DocumentCacheStore.enforce_requester``), the same check the store's
    ``get_page_text`` and ``JsonlPageMapProvider`` apply. Without it a request
    made for a holdout trial, for an unlisted one, or (under a holdout set or
    an allowlist) for no named trial was answered from the cache, which checks
    only the tag of the map it reads. The check runs before any child
    provider is called, so a refused request extracts nothing.
    """

    def __init__(
        self,
        providers: Sequence[DocumentPageExtractor],
        *,
        cache_store: DocumentCacheStore | None = None,
    ) -> None:
        self._providers = list(providers)
        self._cache_store = cache_store

    def validate_loaded(self) -> None:
        """Validate any child providers that eagerly verify page-map inputs."""
        for provider in self._providers:
            validator = getattr(provider, "validate_loaded", None)
            if callable(validator):
                validator()

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> ExtractedDocumentPages:
        if nct_id is not None:
            validate_nct_id(nct_id)
        if self._cache_store is not None:
            # Requester first: nothing is read for a refused trial.
            self._cache_store.enforce_requester(nct_id)
        self.validate_loaded()
        if self._cache_store is not None:
            cached = self._cache_store.load_cached_pages(sha256)
            if cached is not None:
                return cached

        errors: list[str] = []
        for provider in self._providers:
            try:
                return provider.extract_pages(
                    sha256=sha256,
                    pdf_path=pdf_path,
                    nct_id=nct_id,
                    filename=filename,
                )
            except PageExtractionError as exc:
                errors.append(f"{type(provider).__name__}: {exc}")

        guidance = (
            "Supply a pre-extracted --page-map-jsonl or populate data/document_cache/pages/ "
            "when running on Linux without macOS PDFKit."
        )
        detail = "; ".join(errors) if errors else "no providers configured"
        raise PageExtractionError(
            f"Unable to extract pages for sha256={sha256!r} ({detail}). {guidance}"
        )

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        validate_nct_id(nct_id)
        self.validate_loaded()
        if self._cache_store is not None:
            text = self._cache_store.get_page_text(
                nct_id, filename, sha256, physical_page, text_source=text_source
            )
            if text is not None:
                return text
        for provider in self._providers:
            text = provider.get_page_text(
                nct_id, filename, sha256, physical_page, text_source=text_source
            )
            if text is not None:
                return text
        return None


# ---------------------------------------------------------------------------
# Content-addressed on-disk DocumentCacheStore
# ---------------------------------------------------------------------------


_URL_MANIFEST_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {"sha256", "byte_count", "api_size", "api_upload_date", "verified_at", "pipeline_commit"}
)
# A url_manifest.json ``pipeline_commit`` is a git commit id (an abbreviated
# one included): lowercase hex, 7 to 64 characters, or null.
_URL_MANIFEST_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,64}", re.ASCII)


def _validated_url_manifest_record(record: Any, *, origin: str) -> dict[str, Any]:
    """Return a copy of one url_manifest.json record when it is exactly what ``record_url_manifest`` writes.

    Exactly the six keys (``_URL_MANIFEST_RECORD_KEYS``) and, without
    coercion: ``sha256`` 64 lowercase hex; ``byte_count`` a positive int (not
    a bool); ``api_size`` null or a non-negative int; ``api_upload_date`` null
    or a CT.gov date string (``parse_ctgov_date``); ``verified_at`` an
    ISO-8601 timestamp (``datetime.fromisoformat``) with nothing in it that
    reads as a trial id (``_TRIAL_ID_IN_FILENAME_RE``: 8 digits in a row, as
    the basic form ``YYYYMMDD`` has); ``pipeline_commit`` null
    or a lowercase-hex commit id (7 to 64 characters). Anything else raises
    ``CacheIntegrityError`` naming ``origin`` and the field, never the value:
    every field is written back whole by the next rewrite, so a field read
    loosely (an extra key, a date or stamp holding other text) would be
    carried forward, a holdout trial's id included. Used on load and on write.
    """
    obj = _stored_object(record, _URL_MANIFEST_RECORD_KEYS, origin=origin)
    sha = obj["sha256"]
    if type(sha) is not str or not SHA256_RE.fullmatch(sha):
        raise CacheIntegrityError(f"{origin} has no valid sha256 (64 lowercase hex characters).")
    byte_count = obj["byte_count"]
    if type(byte_count) is not int or byte_count <= 0:
        raise CacheIntegrityError(
            f"{origin} has no positive int byte_count (got {type(byte_count).__name__})."
        )
    api_size = obj["api_size"]
    if api_size is not None and (type(api_size) is not int or api_size < 0):
        shown = "negative int" if type(api_size) is int else type(api_size).__name__
        raise CacheIntegrityError(
            f"{origin} field 'api_size' must be a non-negative int or null (got {shown})."
        )
    upload = obj["api_upload_date"]
    if upload is not None and (
        type(upload) is not str or parse_ctgov_date(upload)["date_precision"] == "unknown"
    ):
        raise CacheIntegrityError(
            f"{origin} field 'api_upload_date' must be a CT.gov date string or null (got "
            f"{type(upload).__name__} that is not one); the value is not printed."
        )
    verified_at = obj["verified_at"]
    try:
        if type(verified_at) is not str or not verified_at.strip():
            raise ValueError("not a non-empty string")
        datetime.fromisoformat(verified_at)
        # A stamp holding 8 digits in a row (Python 3.11 reads YYYYMMDD and
        # long fractional seconds) reads as a trial id, as in a filename.
        if _TRIAL_ID_IN_FILENAME_RE.search(verified_at):
            raise ValueError("reads as a trial id")
    except ValueError:
        raise CacheIntegrityError(
            f"{origin} field 'verified_at' must be an ISO-8601 timestamp with no 8 digits "
            f"in a row (got {type(verified_at).__name__} that is not one); the value is "
            "not printed."
        ) from None
    commit = obj["pipeline_commit"]
    if commit is not None and (type(commit) is not str or not _URL_MANIFEST_COMMIT_RE.fullmatch(commit)):
        raise CacheIntegrityError(
            f"{origin} field 'pipeline_commit' must be a lowercase-hex commit id (7 to 64 "
            f"characters) or null (got {type(commit).__name__} that is not one)."
        )
    return dict(obj)


class DocumentCacheStore:
    """Content-addressed sha256 cache for PDFs, gzipped page maps, URL manifest, and index entries."""

    def __init__(
        self,
        cache_dir: Path = Path("data/document_cache"),
        index_path: Path = Path("data/document_index_by_sha256.jsonl"),
        *,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        allowed_nct_ids: AbstractSet[str] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.index_path = Path(index_path)
        self.pages_dir = self.cache_dir / "pages"
        self.blobs_dir = self.cache_dir / "blobs"
        self.url_manifest_path = self.cache_dir / "url_manifest.json"
        self._audit_nct_ids = _validate_nct_id_set(audit_nct_ids, label="audit_nct_ids")
        self._allowed_nct_ids = (
            _validate_nct_id_set(allowed_nct_ids, label="allowed_nct_ids")
            if allowed_nct_ids is not None
            else None
        )
        if self._allowed_nct_ids is not None and (self._allowed_nct_ids & self._audit_nct_ids):
            raise AuditHoldoutError(
                "DocumentCacheStore conflicting sets: "
                f"{len(self._allowed_nct_ids & self._audit_nct_ids)} id(s) in both "
                "allowed_nct_ids and audit_nct_ids."
            )
        self._entries: dict[tuple[str, str], DocumentIndexEntry] = {}
        self._sha_invariants: dict[str, tuple[int, int]] = {}
        self._url_manifest: dict[str, dict[str, Any]] = {}
        # Writes held back by ``staged_writes`` (None when writing through):
        # page-map bytes by path, and whether the index / url manifest need
        # rewriting once the staged block succeeds.
        self._staged_page_maps: dict[Path, bytes] | None = None
        self._staged_index_dirty = False
        self._staged_url_manifest_dirty = False
        # Blob files written inside the outermost staged block that did not
        # exist before it (``note_blob_created``, ``save_blob``), in creation
        # order; removed if the block fails.
        self._staged_created_blobs: list[Path] = []
        self._load_existing()

    def _load_existing(self) -> None:
        """Read the index and url manifest strictly; any refusal raises before anything is written.

        Each index line is parsed with ``loads_strict_json`` (a repeated key is
        refused, not read last-wins) and ``DocumentIndexEntry.from_dict``
        (exact keys and types, no coercion). Two lines for one
        ``(sha256, indexer_rules_version)`` are refused whether or not they
        agree: ``flush_index`` writes one line per key, so a second line was
        not written by this store and nothing says which one is meant. A
        refusal raises ``CacheIntegrityError`` naming the file and line, and
        the file is left byte for byte.
        """
        if self.index_path.exists():
            origin_file = str(self.index_path)
            try:
                text = self.index_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise CacheIntegrityError(
                    f"Document index {origin_file} cannot be read ({type(exc).__name__}); it is "
                    "left as it is."
                ) from None
            # split("\n"), not splitlines(): the lines are the file's own, as a
            # file iterator reads them, whatever other separators a value holds.
            for line_no, raw_line in enumerate(text.split("\n"), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                where = f"Document index {origin_file}:{line_no}"
                try:
                    obj = loads_strict_json(line, origin=where, error_cls=CacheIntegrityError)
                except json.JSONDecodeError as exc:
                    raise CacheIntegrityError(
                        f"{where} is not valid JSON ({exc.msg}); the file is left as it is."
                    ) from None
                entry = DocumentIndexEntry.from_dict(obj, origin=where)
                key = (entry.sha256, entry.indexer_rules_version)
                if key in self._entries:
                    raise CacheIntegrityError(
                        f"{where} repeats the (sha256, indexer_rules_version) of an earlier "
                        "line; a second line for one key is refused, not read last-wins, and "
                        "the file is left as it is."
                    )
                self._check_sha_invariants(
                    entry.sha256, entry.byte_count, entry.physical_pages
                )
                self._entries[key] = entry
                self._sha_invariants[entry.sha256] = (
                    entry.byte_count,
                    entry.physical_pages,
                )
        if self.url_manifest_path.exists():
            self._url_manifest = self._load_url_manifest()

    def _staging_savepoint(self) -> tuple[Any, ...]:
        """Copy the in-memory state a staged block may change, for ``_restore_savepoint``."""
        return (
            dict(self._entries),
            dict(self._sha_invariants),
            {url: dict(record) for url, record in self._url_manifest.items()},
            dict(self._staged_page_maps) if self._staged_page_maps is not None else None,
            self._staged_index_dirty,
            self._staged_url_manifest_dirty,
            len(self._staged_created_blobs),
        )

    def _restore_savepoint(self, savepoint: tuple[Any, ...]) -> None:
        """Return to ``savepoint``: in-memory state restored, blob files created since then removed."""
        (
            self._entries,
            self._sha_invariants,
            self._url_manifest,
            self._staged_page_maps,
            self._staged_index_dirty,
            self._staged_url_manifest_dirty,
            created_before,
        ) = savepoint
        for path in reversed(self._staged_created_blobs[created_before:]):
            path.unlink(missing_ok=True)
            # The two-character shard folder, when the removal emptied it.
            _rmdir_if_empty(path.parent)
        del self._staged_created_blobs[created_before:]

    @contextmanager
    def staged_writes(self) -> Iterator[None]:
        """Hold every cache write made inside the block, and write them only if the block succeeds.

        Used by ``ingest_legacy_census_documents`` and by the live path (each
        ``index_trial_from_study`` call, and ``run_cli`` around a whole live
        run), whose promise is that a refused run writes nothing: a trial can
        still be refused after earlier ones were indexed (a page map tagged
        with a holdout trial, a payload whose alias names one, a local PDF
        that disagrees with its record), and writing through would leave those
        earlier trials' blobs, page maps, url records and index lines in the
        cache with no profile.

        Inside the block, page maps, index entries and url_manifest records
        are kept in memory, and every read (``load_cached_pages``,
        ``get_entry``, ``lookup_url_manifest``) sees them. On a clean exit
        they are written: page maps first, then url_manifest.json, then the
        index, the same order an unstaged run writes a document in. On an
        exception nothing is written and the in-memory index and manifest are
        restored. Blob files (a download is streamed to disk, never held in
        memory) are written through, and every one created inside the block
        is registered (``note_blob_created``, ``save_blob``) and removed on an
        exception, with the shard folder, ``blobs/`` and the cache folder when
        the removal empties a folder the block created. (A write failing
        part-way through the commit, a full disk say, can still leave some
        files written; that is an I/O failure, not a refusal. A damaged blob
        that a download replaced with its digest's verified bytes is not put
        back.)

        A block opened inside another is a savepoint: its exception restores
        the state at its start (and removes the blobs created since) and
        propagates; its clean exit writes nothing, leaving the outermost block
        to write everything once.
        """
        if self._staged_page_maps is not None:
            savepoint = self._staging_savepoint()
            try:
                yield
            except BaseException:
                self._restore_savepoint(savepoint)
                raise
            return
        savepoint = self._staging_savepoint()
        cache_dir_existed = self.cache_dir.exists()
        blobs_dir_existed = self.blobs_dir.exists()
        self._staged_page_maps = {}
        self._staged_index_dirty = False
        self._staged_url_manifest_dirty = False
        try:
            yield
        except BaseException:
            self._restore_savepoint(savepoint)
            if not blobs_dir_existed:
                _rmdir_if_empty(self.blobs_dir)
            if not cache_dir_existed:
                _rmdir_if_empty(self.cache_dir)
            raise
        staged_maps = self._staged_page_maps
        index_dirty = self._staged_index_dirty
        manifest_dirty = self._staged_url_manifest_dirty
        self._staged_page_maps = None
        self._staged_index_dirty = False
        self._staged_url_manifest_dirty = False
        self._staged_created_blobs = []
        for path, data in sorted(staged_maps.items()):
            atomic_write_bytes(path, data)
        if manifest_dirty:
            self._write_url_manifest()
        if index_dirty:
            self.flush_index()

    @property
    def staging(self) -> bool:
        """True inside a ``staged_writes`` block."""
        return self._staged_page_maps is not None

    def note_blob_created(self, path: Path) -> None:
        """Register a blob file a caller just created under ``blobs/``, so a failing staged block removes it.

        ``download_pdf_atomic`` streams a download straight into
        ``blobs/<sha[:2]>/<sha>.pdf``; the caller reports the file here when the
        download created it (not when an existing verified blob was kept).
        Outside a staged block this does nothing. A path that is not a
        ``<sha>.pdf`` file in this store's blob layout is refused
        (``CacheIntegrityError``): only this store's own blob files may ever be
        removed by a rollback.
        """
        if self._staged_page_maps is None:
            return
        candidate = Path(path)
        sha = candidate.stem.lower()
        if (
            candidate.suffix != ".pdf"
            or not SHA256_RE.fullmatch(sha)
            or candidate.resolve() != self.blob_path(sha).resolve()
        ):
            raise CacheIntegrityError(
                "note_blob_created was given a path that is not a blob file of this cache "
                "store (blobs/<sha[:2]>/<sha>.pdf); it is not registered for removal."
            )
        self._staged_created_blobs.append(self.blob_path(sha))

    def _read_page_map_object(self, path: Path) -> Any | None:
        """Parse the page map at ``path`` (staged bytes first, then disk), or None when there is none.

        A map naming any key twice (``nct_id`` or ``in_audit``, say) is
        refused by ``loads_strict_json``, not read last-wins.
        """
        data: bytes | None = None
        if self._staged_page_maps is not None:
            data = self._staged_page_maps.get(path)
        if data is None:
            if not path.exists():
                return None
            data = path.read_bytes()
        return loads_strict_json(gzip.decompress(data).decode("utf-8"), origin=str(path))

    def _read_flat_page_map_object(self, path: Path) -> Any | None:
        """Parse a cached page map at a flat location (``pages/<sha>.json.gz`` or ``.json``), or None.

        ``JsonlPageMapProvider`` serves maps from these locations as well as
        from the sharded ``pages/<sha[:2]>/<sha>.json.gz`` this store writes,
        so their tags are checked too (``load_cached_pages``). An unreadable
        map raises ``CacheIntegrityError`` naming the file.
        """
        if not path.exists():
            return None
        try:
            if path.suffix == ".gz":
                text = gzip.decompress(path.read_bytes()).decode("utf-8")
            else:
                text = path.read_text(encoding="utf-8")
            return loads_strict_json(text, origin=str(path), error_cls=CacheIntegrityError)
        except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheIntegrityError(
                f"Cached page map {path} cannot be read: {type(exc).__name__}"
            ) from None

    @property
    def firewall_configured(self) -> bool:
        """True when this store has a holdout set or an allowlist (the rule ``check_page_map_firewall`` uses)."""
        return bool(self._audit_nct_ids) or self._allowed_nct_ids is not None

    @property
    def audit_nct_ids(self) -> frozenset[str]:
        """This store's holdout set (validated strict ids)."""
        return self._audit_nct_ids

    @property
    def allowed_nct_ids(self) -> frozenset[str] | None:
        """This store's allowlist (validated strict ids), or None when it has none."""
        return self._allowed_nct_ids

    def merge_firewall_sets(
        self,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        allowed_nct_ids: AbstractSet[str] | None = None,
    ) -> tuple[frozenset[str], frozenset[str] | None]:
        """Return the firewall a call through this store runs under: ``(holdout set, allowlist)``.

        The holdout set is the union of this store's and the call's; the
        allowlist is the intersection of the two (either alone when only one is
        given, None when neither is). Every entry point handed a store merges
        its own arguments with the store's this way before any request, so a
        store built with a holdout set refuses that trial whatever the call's
        arguments say (they default to no firewall). An id in both the merged
        holdout set and the merged allowlist is refused (``AuditHoldoutError``,
        counts only).
        """
        combined_audit = self._audit_nct_ids | _validate_nct_id_set(
            audit_nct_ids, label="audit_nct_ids"
        )
        call_allowed = (
            _validate_nct_id_set(allowed_nct_ids, label="allowed_nct_ids")
            if allowed_nct_ids is not None
            else None
        )
        combined_allowed: frozenset[str] | None
        if self._allowed_nct_ids is not None and call_allowed is not None:
            combined_allowed = self._allowed_nct_ids & call_allowed
        elif self._allowed_nct_ids is not None:
            combined_allowed = self._allowed_nct_ids
        else:
            combined_allowed = call_allowed
        if combined_allowed is not None and (combined_allowed & combined_audit):
            raise AuditHoldoutError(
                f"Conflicting firewall sets: {len(combined_allowed & combined_audit)} id(s) "
                "present in both the allowlist and the audit holdout set."
            )
        return combined_audit, combined_allowed

    def enforce_requester(self, nct_id: str | None) -> str | None:
        """Check the trial asking for cached pages against this store's firewall.

        A named trial must be strict ASCII, not in the holdout set and, under an
        allowlist, on it (``enforce_not_in_audit``). An unnamed request
        (``nct_id=None``) is refused whenever the firewall is configured (a
        holdout set or an allowlist), as ``JsonlPageMapProvider`` and
        ``get_or_index_document`` refuse it: nothing shows the pages are not
        asked for on a holdout trial's behalf, the rule an untagged page map is
        held to. It is accepted only with no firewall at all (library use).
        Returns the validated id or None.
        """
        if nct_id is not None:
            return enforce_not_in_audit(
                nct_id,
                in_audit=False,
                audit_nct_ids=self._audit_nct_ids,
                allowed_nct_ids=self._allowed_nct_ids,
            )
        if self.firewall_configured:
            raise AuditHoldoutError(
                "Explicit nct_id is required for a cached page lookup when a holdout set "
                "or allowed_nct_ids is configured."
            )
        return None

    def _check_url_firewall(self, url: Any, *, origin: str, serving: bool) -> None:
        """Apply the holdout firewall to one url_manifest.json key (a document URL).

        Only under a configured firewall (``firewall_configured``); with none,
        any key is accepted, as an untagged page map is. Under one:

        - the key must be a canonical CT.gov document URL
          (``parse_ctgov_large_doc_url``); any other key cannot be tied to a
          trial, so nothing shows it is not a holdout trial's, and it is
          refused, as an untagged page map is;
        - the trial it names must not be in the holdout set;
        - when ``serving`` (a lookup or a new record, as opposed to loading the
          file), the trial must also be on the allowlist. An entry for a trial
          this run's manifest does not list is kept when the file is loaded
          (one cache may serve several manifests) and carried unchanged
          through a whole-file rewrite, but it is never served, and this store
          never records a new one;
        - except that with NO holdout set known (an empty audit set under an
          allowlist: a manifest carrying only in_audit=False rows), an entry
          for a trial not on the allowlist is refused on load too. Checked
          against an empty holdout set it has been checked against nothing,
          so it may be a holdout trial's, and a rewrite would carry it
          forward (the rule ``validate_study_payload_nct_id`` applies to an
          alias in the same case). Run with the manifest that marks the
          holdout, or remove the entry by hand.

        Messages name ``origin`` only, never the URL, which names a trial.
        """
        if not self.firewall_configured:
            return
        parsed = parse_ctgov_large_doc_url(url)
        if parsed is None:
            raise AuditHoldoutError(
                f"{origin} is not keyed by a canonical CT.gov document URL, so it cannot "
                "be tied to a trial and checked against the audit holdout."
            )
        url_nct, _filename = parsed
        if url_nct in self._audit_nct_ids:
            raise AuditHoldoutError(
                f"{origin} records a document of an audit holdout trial; a cache holding "
                "audit holdout records is refused and left as it is (remove the entry "
                "by hand, outside this tool)."
            )
        if self._allowed_nct_ids is not None and url_nct not in self._allowed_nct_ids:
            if serving:
                raise AuditHoldoutError(
                    f"{origin} is for a trial not in allowed_nct_ids; unlisted trials' "
                    "cache records are not served or written."
                )
            if not self._audit_nct_ids:
                raise AuditHoldoutError(
                    f"{origin} is for a trial not in allowed_nct_ids and no audit holdout "
                    "set is known (the manifest lists no in_audit=True row), so nothing "
                    "shows it is not a holdout trial's record; the cache is refused and "
                    "left as it is. Run with the manifest that marks the holdout."
                )

    def _load_url_manifest(self) -> dict[str, dict[str, Any]]:
        """Read ``url_manifest.json`` whole, raising on anything this store would otherwise drop or carry.

        The file is a JSON object mapping a document URL to the record
        ``record_url_manifest`` writes. Every ``record_url_manifest`` rewrites
        the whole file from what was loaded here, so a record this loader
        skipped would be deleted on the next write with no message, and a
        field it kept unchecked would be written back as it was (an extra
        key, or a ``verified_at`` holding a holdout trial's id). Nothing is
        skipped or carried unchecked instead: unreadable JSON, a repeated
        key, a root that is not an object, and a record that is not exactly
        what ``record_url_manifest`` writes (``_validated_url_manifest_record``:
        its six keys and their types) raise ``CacheIntegrityError``, and each
        key passes ``_check_url_firewall`` (``AuditHoldoutError``), firewall
        first. A raise happens before any write, so the file is left byte for
        byte. Entries are named by their 1-based position in the file, never by
        URL, and no field's value is printed.
        """
        origin = str(self.url_manifest_path)
        try:
            text = self.url_manifest_path.read_text(encoding="utf-8")
            loaded = loads_strict_json(text, origin=origin, error_cls=CacheIntegrityError)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheIntegrityError(
                f"Cache URL manifest {origin} cannot be read as JSON ({type(exc).__name__}); "
                "it is left as it is."
            ) from None
        if not isinstance(loaded, dict):
            raise CacheIntegrityError(
                f"Cache URL manifest {origin} is a JSON {type(loaded).__name__}, not an object "
                "keyed by document URL; it is left as it is rather than overwritten."
            )
        manifest: dict[str, dict[str, Any]] = {}
        for position, (url, record) in enumerate(loaded.items(), start=1):
            where = f"Cache URL manifest {origin} entry {position}"
            self._check_url_firewall(url, origin=where, serving=False)
            manifest[url] = _validated_url_manifest_record(record, origin=where)
        return manifest

    def _check_sha_invariants(
        self,
        sha256: str,
        byte_count: int,
        physical_pages: int | None = None,
    ) -> None:
        existing = self._sha_invariants.get(sha256)
        if existing is None:
            return
        ex_bytes, ex_pages = existing
        if ex_bytes != byte_count:
            raise CacheIntegrityError(
                f"Conflicting byte_count for sha256={sha256}: "
                f"existing={ex_bytes}, new={byte_count}"
            )
        if physical_pages is not None and ex_pages != physical_pages:
            raise CacheIntegrityError(
                f"Conflicting physical_pages for sha256={sha256}: "
                f"existing={ex_pages}, new={physical_pages}"
            )

    def page_map_path(self, sha256: str) -> Path:
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError(f"Invalid sha256: {sha256!r}")
        return self.pages_dir / sha[:2] / f"{sha}.json.gz"

    def blob_path(self, sha256: str) -> Path:
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError(f"Invalid sha256: {sha256!r}")
        return self.blobs_dir / sha[:2] / f"{sha}.pdf"

    def get_entry(
        self,
        sha256: str,
        rules_version: str = INDEXER_RULES_VERSION,
        *,
        expected_byte_count: int | None = None,
    ) -> DocumentIndexEntry | None:
        sha = sha256.lower()
        if expected_byte_count is not None:
            self._check_sha_invariants(sha, expected_byte_count)
        return self._entries.get((sha, rules_version))

    def put_entry(
        self, entry: DocumentIndexEntry, *, overwrite: bool = False
    ) -> DocumentIndexEntry:
        """Persist a (sha256, indexer_rules_version) entry.

        Preserves the original entry's (extracted_at, pipeline_commit) when the
        same (sha256, indexer_rules_version) already exists unless overwrite=True
        (used by explicit --reparse-cached-pages). Always raises
        CacheIntegrityError if byte_count or physical_pages disagrees for the
        same sha256.
        """
        self._check_sha_invariants(
            entry.sha256, entry.byte_count, entry.physical_pages
        )
        key = (entry.sha256, entry.indexer_rules_version)
        if key in self._entries and not overwrite:
            return self._entries[key]
        self._entries[key] = entry
        self._sha_invariants[entry.sha256] = (entry.byte_count, entry.physical_pages)
        if self._staged_page_maps is not None:
            # Inside staged_writes: the index is rewritten once, on success.
            self._staged_index_dirty = True
        else:
            self.flush_index()
        return entry

    def flush_index(self) -> None:
        """Atomically write document_index_by_sha256.jsonl sorted by (sha256, rules_version)."""
        lines = [
            json.dumps(entry.to_dict(), separators=(",", ":"), ensure_ascii=True)
            for _, entry in sorted(self._entries.items(), key=lambda kv: kv[0])
        ]
        payload = ("\n".join(lines) + "\n") if lines else ""
        atomic_write_text(self.index_path, payload)

    def all_entries(self) -> list[DocumentIndexEntry]:
        return [entry for _, entry in sorted(self._entries.items(), key=lambda kv: kv[0])]

    def save_cached_pages(
        self,
        sha256: str,
        *,
        byte_count: int,
        pages: Sequence[str],
        text_source: str = "pdfkit",
        extracted_at: str | None = None,
        pipeline_commit: str | None = None,
        nct_id: str | None = None,
    ) -> Path:
        """Write ``pages/<sha[:2]>/<sha>.json.gz`` once; an existing map must agree with it.

        ``nct_id`` is the trial that passed the firewall when these pages were
        extracted, and is written into the map as its ``nct_id`` tag so that a
        later load under a configured firewall can check the map against the
        holdout (``check_page_map_firewall``). The map stays content-addressed:
        a second trial with the same bytes reuses it, and the tag keeps naming
        the first. The tag is checked here too, so this store never writes a
        map tagged with a holdout or unlisted trial, and under a configured
        firewall (``enforce_requester``) never an untagged one, which this
        store would then refuse to read back.
        """
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError(f"Invalid sha256: {sha256!r}")
        self.enforce_requester(nct_id)
        sanitized = [sanitize_page_text(p) for p in pages]
        self._check_sha_invariants(sha, byte_count, len(sanitized))
        path = self.page_map_path(sha)
        existing = self._read_page_map_object(path)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise CacheIntegrityError(
                    f"Existing cached page map {path} is not a JSON object."
                )
            if existing.get("byte_count") != byte_count or existing.get("physical_pages") != len(sanitized):
                raise CacheIntegrityError(
                    f"Existing cached page map for {sha} disagrees with byte_count/physical_pages."
                )
            return path

        payload: dict[str, Any] = {
            "sha256": sha,
            "byte_count": byte_count,
            "physical_pages": len(sanitized),
            "text_source": text_source,
            "extracted_at": extracted_at or utc_now_iso(),
            "pipeline_commit": pipeline_commit,
            "pages": sanitized,
        }
        if nct_id is not None:
            payload["nct_id"] = nct_id
        if self._staged_page_maps is not None:
            # Inside staged_writes: held in memory, written on success only.
            self._staged_page_maps[path] = encode_gz_json(payload)
        else:
            atomic_write_bytes(path, encode_gz_json(payload))
        return path

    def load_cached_pages(
        self,
        sha256: str,
        *,
        expected_byte_count: int | None = None,
        text_source: str = "pdfkit",
        audit_nct_ids: AbstractSet[str] = frozenset(),
        allowed_nct_ids: AbstractSet[str] | None = None,
    ) -> ExtractedDocumentPages | None:
        """Load this store's cached page map for ``sha256``, or None when there is none.

        The map passes ``check_page_map_firewall`` with this store's audit set
        and allowlist merged with the call's (``merge_firewall_sets``) before
        anything else in it is read, so under a configured firewall an
        untagged map (one written before maps were tagged, or placed in
        ``pages/`` by hand) is refused rather than served, and a holdout set
        handed to ``get_or_index_document`` refuses a map tagged with that
        holdout as a store built with it does.

        Maps at the flat locations ``pages/<sha>.json.gz`` and
        ``pages/<sha>.json``, which ``JsonlPageMapProvider`` serves, pass the
        same check first, whether or not the sharded map exists. They are not
        served from here; their tag is what is checked, so that
        ``get_or_index_document`` (which calls this before serving an index
        entry) refuses a digest whose map at any location names a holdout
        trial, with or without an index entry.
        """
        audit, allowed = self.merge_firewall_sets(audit_nct_ids, allowed_nct_ids)
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            return None
        for flat in (self.pages_dir / f"{sha}.json.gz", self.pages_dir / f"{sha}.json"):
            flat_obj = self._read_flat_page_map_object(flat)
            if flat_obj is None:
                continue
            if not isinstance(flat_obj, Mapping):
                raise CacheIntegrityError(f"Malformed cached page map {flat}: expected JSON object")
            check_page_map_firewall(
                flat_obj, origin=str(flat), audit_nct_ids=audit, allowed_nct_ids=allowed
            )
        path = self.page_map_path(sha)
        # A map naming nct_id or in_audit twice is refused, not read last-wins.
        obj = self._read_page_map_object(path)
        if obj is None:
            return None
        if not isinstance(obj, Mapping):
            raise CacheIntegrityError(f"Malformed cached page map {path}: expected JSON object")
        check_page_map_firewall(obj, origin=str(path), audit_nct_ids=audit, allowed_nct_ids=allowed)
        if obj.get("sha256") != sha:
            raise CacheIntegrityError(
                f"Page map file {path} contains mismatched sha256={obj.get('sha256')!r}"
            )
        if expected_byte_count is not None and obj.get("byte_count") != expected_byte_count:
            raise CacheIntegrityError(
                f"Cached page map for {sha} has byte_count={obj.get('byte_count')}, "
                f"expected {expected_byte_count}"
            )
        if _validated_text_source(obj, origin=str(path)) != text_source:
            return None
        pages = _validated_page_list(obj.get("pages"), origin=str(path))
        return ExtractedDocumentPages(
            sha256=sha,
            physical_pages=len(pages),
            pages=pages,
            provider_name="cached_page_map",
        )

    def get_page_text(
        self,
        nct_id: str,
        filename: str,
        sha256: str,
        physical_page: int,
        *,
        text_source: str = "pdfkit",
    ) -> str | None:
        """Direct single-page lookup with the ``PageTextProvider`` signature (Module 4 match untested here)."""
        validate_nct_id(nct_id)
        self.enforce_requester(nct_id)
        cached = self.load_cached_pages(sha256, text_source=text_source)
        if cached is None:
            return None
        if 1 <= physical_page <= cached.physical_pages:
            return cached.pages[physical_page - 1]
        return None

    def save_blob(self, sha256: str, data: bytes) -> Path:
        """Write ``blobs/<sha[:2]>/<sha>.pdf`` once; a blob it creates inside a staged block is removed if the block fails."""
        actual = sha256_bytes(data)
        sha = sha256.lower()
        if actual != sha:
            raise CacheIntegrityError(
                f"Blob sha256 mismatch: expected {sha}, computed {actual}"
            )
        self._check_sha_invariants(sha, len(data))
        path = self.blob_path(sha)
        if not path.exists():
            atomic_write_bytes(path, data)
            self.note_blob_created(path)
        return path

    def get_blob_path(self, sha256: str) -> Path | None:
        path = self.blob_path(sha256)
        return path if path.exists() else None

    def lookup_url_manifest(self, url: str) -> dict[str, Any] | None:
        """Return a copy of ``url``'s record, after ``_check_url_firewall`` (serving); None when there is none."""
        self._check_url_firewall(url, origin="url_manifest lookup", serving=True)
        entry = self._url_manifest.get(url)
        return dict(entry) if entry is not None else None

    def can_skip_download(
        self,
        url: str,
        *,
        api_size: int | None,
        api_upload_date: str | None,
    ) -> str | None:
        """Return cached sha256 when url_manifest matches non-null (api_size, api_upload_date).

        The url passes ``_check_url_firewall`` (serving) first, so a holdout or
        unlisted trial's record is never served. The record's ``byte_count``
        must also equal ``api_size``: a record an earlier version wrote for a
        download whose length was not the payload's size (which
        ``record_url_manifest`` now refuses) still loads, but is not trusted,
        so the document is downloaded again rather than that digest reused.
        """
        self._check_url_firewall(url, origin="url_manifest lookup", serving=True)
        if api_size is None or api_upload_date is None:
            return None
        rec = self._url_manifest.get(url)
        if rec is None:
            return None
        if (
            rec.get("api_size") == api_size
            and rec.get("byte_count") == api_size
            and rec.get("api_upload_date") == api_upload_date
            and isinstance(rec.get("sha256"), str)
            and SHA256_RE.fullmatch(rec["sha256"])
        ):
            return rec["sha256"]
        return None

    def record_url_manifest(
        self,
        url: str,
        *,
        sha256: str,
        byte_count: int,
        api_size: int | None,
        api_upload_date: str | None,
        verified_at: str | None = None,
        pipeline_commit: str | None = None,
    ) -> None:
        """Record ``url``'s verified digest and rewrite ``url_manifest.json`` whole.

        The url passes ``_check_url_firewall`` (serving) before anything is
        changed, so this store never writes a record for a holdout, unlisted or
        unattributable URL. The rewrite carries every record loaded by
        ``_load_url_manifest``, which refuses (rather than drops) any it cannot
        read, so no record is lost by the rewrite. The new record is checked
        with the loader's own rule (``_validated_url_manifest_record``) before
        it is kept, so this store never writes a record it would refuse to
        read back. A record whose non-null ``api_size`` is not its
        ``byte_count`` is refused too (``CacheIntegrityError``): bytes of
        another length are not the payload's posted document, and a record of
        them would be served by ``can_skip_download`` on every later run. With
        ``api_size`` None there is nothing to compare against.
        """
        self._check_url_firewall(url, origin="url_manifest record", serving=True)
        sha = sha256.lower() if type(sha256) is str else sha256
        if type(sha) is not str or not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError("Invalid sha256 for url_manifest (not 64 hex characters).")
        if type(byte_count) is not int or byte_count <= 0:
            raise CacheIntegrityError(
                f"url_manifest byte_count must be a positive int, got {type(byte_count).__name__}."
            )
        self._check_sha_invariants(sha, byte_count)
        record = _validated_url_manifest_record(
            {
                "sha256": sha,
                "byte_count": byte_count,
                "api_size": api_size,
                "api_upload_date": api_upload_date,
                "verified_at": verified_at or utc_now_iso(),
                "pipeline_commit": pipeline_commit,
            },
            origin="New url_manifest record",
        )
        # Checked on write only, after the loader's rule has made api_size an
        # int or None (so only ints are printed). A url_manifest.json an earlier
        # version wrote with a mismatch still loads, whole, and
        # can_skip_download does not trust that record.
        if record["api_size"] is not None and record["api_size"] != record["byte_count"]:
            raise CacheIntegrityError(
                f"url_manifest record refused: byte_count {record['byte_count']} is not the "
                f"CT.gov payload size {record['api_size']}, so the bytes are not taken as the "
                "posted document."
            )
        self._url_manifest[url] = record
        if self._staged_page_maps is not None:
            # Inside staged_writes: the file is rewritten once, on success.
            self._staged_url_manifest_dirty = True
            return
        self._write_url_manifest()

    def _write_url_manifest(self) -> None:
        """Rewrite ``url_manifest.json`` whole from the records held in memory, sorted by URL."""
        payload = json.dumps(
            dict(sorted(self._url_manifest.items())),
            indent=2,
            ensure_ascii=True,
        ) + "\n"
        atomic_write_text(self.url_manifest_path, payload)

    def get_or_index_document(
        self,
        *,
        sha256: str,
        byte_count: int,
        provider: DocumentPageExtractor | None,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
        doc_type: DocType = "protocol",
        extracted_at: str | None = None,
        pipeline_commit: str | None = None,
        indexer_rules_version: str = INDEXER_RULES_VERSION,
        min_selected_pages: int = 10,
        max_selected_pages: int = 25,
        reparse_cached_pages: bool = False,
        in_audit: Any = False,
        audit_nct_ids: AbstractSet[str] = frozenset(),
        allowed_nct_ids: AbstractSet[str] | None = None,
    ) -> tuple[DocumentIndexEntry, bool]:
        """Resolve a DocumentIndexEntry from cache, cached pages, or PageTextProvider.

        Returns (entry, cache_hit) where cache_hit is True when an existing
        (sha256, indexer_rules_version) entry was reused without re-indexing.

        Order, after the requesting trial passes the firewall and the page
        bounds are checked (``validate_page_selection_bounds``, before any
        write):

        The firewall is this store's merged with the call's
        (``merge_firewall_sets``), for the requesting trial and for the tag of
        every cached page map in step 1 alike. Under any configured firewall
        the request must name its trial: ``nct_id=None`` is refused before
        anything is read, extracted or written.

        1. Every cached page map for ``sha256`` (the sharded one this store
           writes and the flat ``pages/<sha>.json(.gz)`` ones a page-map
           provider reads; see ``load_cached_pages``) passes
           ``check_page_map_firewall`` BEFORE an index entry is served. The
           index line is untagged; the map is what names the trial the pages
           were extracted for. Serving the entry first let a digest whose map
           is tagged with a holdout trial through as a cache hit, while the
           same digest without an entry was refused.
        2. An existing entry is served (cache hit) unless re-parsing.
        3. Otherwise the cached pages are re-indexed, or the provider extracts
           them.

        A supplied ``pdf_path`` must hash to ``sha256`` and ``byte_count``
        after step 1 and before step 2 (``CacheIntegrityError`` otherwise:
        no entry is served, nothing is re-indexed, the provider is not
        called): a cached blob or local file whose bytes are not that
        digest's (damaged, or swapped for another trial's document) would
        otherwise be a successful cache hit for that digest, or be parsed and
        cached as its pages. The error names the expected digest and the
        file, never the digest the file actually has. With ``pdf_path`` None
        no file is hashed.
        """
        validate_page_selection_bounds(min_selected_pages, max_selected_pages)
        combined_audit, combined_allowed = self.merge_firewall_sets(audit_nct_ids, allowed_nct_ids)

        if nct_id is not None:
            enforce_not_in_audit(
                nct_id,
                in_audit=in_audit,
                audit_nct_ids=combined_audit,
                allowed_nct_ids=combined_allowed,
            )
        else:
            if type(in_audit) is not bool:
                raise AuditHoldoutError(
                    f"Non-bool in_audit (type={type(in_audit).__name__}); "
                    "explicit boolean False required."
                )
            if in_audit:
                raise AuditHoldoutError(
                    "Document indexing refused because in_audit=True."
                )
            # Unnamed indexing is refused under any configured firewall: the
            # pages would be parsed and written as an untagged map, which this
            # store then refuses to read (check_page_map_firewall), and nothing
            # shows they are not a holdout trial's.
            if combined_allowed is not None or combined_audit:
                raise AuditHoldoutError(
                    "Explicit nct_id is required when a holdout set or allowed_nct_ids is "
                    "configured."
                )
        sha = sha256.lower()
        if not SHA256_RE.fullmatch(sha):
            raise CacheIntegrityError(f"Invalid sha256: {sha256!r}")
        if type(byte_count) is not int or byte_count <= 0:
            raise CacheIntegrityError(f"byte_count must be positive int, got {byte_count!r}")
        self._check_sha_invariants(sha, byte_count)

        existing = self.get_entry(
            sha, indexer_rules_version, expected_byte_count=byte_count
        )
        # Step 1: the map's holdout tag is checked whether or not an entry
        # exists (load_cached_pages runs check_page_map_firewall before
        # reading anything else in the map), against the merged sets, so a
        # holdout set handed to this call refuses a map tagged with it. It
        # also serves step 3 below: cached pages enable zero-PDFKit/zero-HTTP
        # rule-bump re-indexing.
        cached_pages = self.load_cached_pages(
            sha,
            expected_byte_count=byte_count,
            audit_nct_ids=combined_audit,
            allowed_nct_ids=combined_allowed,
        )
        if pdf_path is not None:
            # Checked before any of steps 2 and 3 (see the docstring); with
            # pdf_path None no file is read.
            actual_sha, actual_bytes = sha256_file(Path(pdf_path))
            if actual_sha != sha or actual_bytes != byte_count:
                raise CacheIntegrityError(
                    f"The PDF file {pdf_path} handed to the page extractor for sha256={sha} "
                    "does not hash to that digest and byte count; it is not parsed or "
                    "cached. Remove the damaged or substituted file (its own digest is "
                    "not printed)."
                )
        if existing is not None and not reparse_cached_pages:
            return existing, True

        if cached_pages is not None:
            entry = index_document_pages(
                cached_pages.pages,
                sha256=sha,
                byte_count=byte_count,
                text_provider=cached_pages.provider_name,
                doc_type=doc_type,
                extracted_at=extracted_at,
                pipeline_commit=pipeline_commit,
                indexer_rules_version=indexer_rules_version,
                min_selected_pages=min_selected_pages,
                max_selected_pages=max_selected_pages,
            )
            stored = self.put_entry(entry, overwrite=reparse_cached_pages)
            return stored, False

        if provider is None:
            raise PageExtractionError(
                f"No cached pages and no PageTextProvider supplied for sha256={sha}"
            )

        extracted = provider.extract_pages(
            sha256=sha,
            pdf_path=pdf_path,
            nct_id=nct_id,
            filename=filename,
        )
        # The pages are cached under ``sha``, a content address for the bytes
        # they describe; a provider reporting another digest's pages is refused
        # before anything is written.
        if type(extracted.sha256) is not str or extracted.sha256.lower() != sha:
            raise CacheIntegrityError(
                f"Page provider {extracted.provider_name!r} returned pages for a different "
                f"digest than the requested sha256={sha}; they are not cached under it."
            )
        # The map is tagged with the trial that just passed the firewall above,
        # so a later run can check it against the holdout before reusing it.
        self.save_cached_pages(
            sha,
            byte_count=byte_count,
            pages=extracted.pages,
            text_source="pdfkit",
            extracted_at=extracted_at,
            pipeline_commit=pipeline_commit,
            nct_id=nct_id,
        )
        entry = index_document_pages(
            extracted.pages,
            sha256=sha,
            byte_count=byte_count,
            text_provider=extracted.provider_name,
            doc_type=doc_type,
            extracted_at=extracted_at,
            pipeline_commit=pipeline_commit,
            indexer_rules_version=indexer_rules_version,
            min_selected_pages=min_selected_pages,
            max_selected_pages=max_selected_pages,
        )
        stored = self.put_entry(entry, overwrite=reparse_cached_pages)
        return stored, False


# ---------------------------------------------------------------------------
# Trial-level profile assembly & legacy census adapters
# ---------------------------------------------------------------------------


def summarize_trial_readability(
    docs: Sequence[TrialDocumentRecord],
) -> DocumentReadabilityStatus:
    """Summarize trial-level readability across posted in-scope documents.

    Rules, in order (the per-document states stay in ``documents``):
    1. no in-scope documents -> ``no_document``;
    2. every document ``download_failed`` -> ``download_failed``;
    3. every document ``extraction_failed`` -> ``extraction_failed`` (all bytes
       obtained, no pages extracted; kept apart from rule 2 because the
       documents exist and a page map or macOS run can still index them);
    4. every document ``machine_readable`` -> ``machine_readable``;
    5. every document ``image_only_scan`` or ``garbled_cmap`` -> ``garbled_cmap``
       if any is garbled, else ``image_only_scan``;
    6. any other mixture -> ``partial_image_or_redacted``.

    Rule 6 is the summary this module had before ``extraction_failed`` existed,
    and a mixture that includes an unexamined document (one ``download_failed``
    or ``extraction_failed`` next to a readable one) still falls into it.
    """
    if not docs:
        return "no_document"
    statuses = [d["readability_status"] for d in docs]
    if all(s == "download_failed" for s in statuses):
        return "download_failed"
    if all(s == "extraction_failed" for s in statuses):
        return "extraction_failed"
    if all(s == "machine_readable" for s in statuses):
        return "machine_readable"
    if all(s in ("image_only_scan", "garbled_cmap") for s in statuses):
        return "garbled_cmap" if any(s == "garbled_cmap" for s in statuses) else "image_only_scan"
    return "partial_image_or_redacted"


def build_trial_document_record(
    *,
    nct_id: str,
    filename: str,
    doc_type: DocType,
    url: str,
    http_status: int | None,
    entry: DocumentIndexEntry | None,
    download_error: str | None = None,
    extraction_error: str | None = None,
    sha256: str | None = None,
    byte_count: int | None = None,
    api_doc_date: str | None = None,
    upload_date: str | None = None,
    primary_completion_date: str | None = None,
    results_first_post_date: str | None = None,
    retrieved_at: str | None = None,
    cache_hit: bool = False,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> TrialDocumentRecord:
    """Bind a content-addressed DocumentIndexEntry (or a failure) to a trial.

    Three shapes, chosen by the arguments:
    - ``entry`` given: an indexed document; ``download_error`` and
      ``extraction_error`` must be None.
    - ``entry=None`` with ``extraction_error``: the bytes were downloaded (or
      found locally) and hashed, but no provider could extract pages. The known
      ``sha256`` and ``byte_count`` are required and recorded;
      ``readability_status='extraction_failed'``; ``physical_pages`` is None.
    - ``entry=None`` without ``extraction_error``: the bytes were never
      obtained; ``readability_status='download_failed'``; sha256, bytes and
      ``physical_pages`` are None, and ``download_error`` (default
      ``"download_failed"``) says why.

    A page count is recorded only when pages were extracted; an unknown count is
    None, never 0, so a total over documents cannot quietly absorb it.

    In every shape ``url`` must be the trial's own posted ``filename``
    (``require_own_large_doc_url``): the record binds a document to the trial
    that passed the firewall, and a URL naming another trial would carry that
    trial's id and document into the profile.

    Every date argument (``api_doc_date``, ``upload_date``,
    ``primary_completion_date``, ``results_first_post_date``) must be None or
    a string ``parse_ctgov_date`` reads as a CT.gov date
    (``DocumentIndexerError`` otherwise): they are copied into the record and
    its upload-timing facts (``raw``), so a non-date string would carry its
    text, a trial id included, into the profile.
    """
    validated_nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )
    safe_filename = validate_safe_pdf_filename(filename)
    require_own_large_doc_url(
        url, nct_id=validated_nct, filename=safe_filename, origin="Trial document record"
    )
    _check_record_date(api_doc_date, field="api_doc_date")
    _check_record_date(upload_date, field="upload_date")
    _check_record_date(primary_completion_date, field="primary_completion_date")
    _check_record_date(results_first_post_date, field="results_first_post_date")
    timing = compute_upload_timing(
        upload_date,
        primary_completion_date=primary_completion_date,
        results_first_post_date=results_first_post_date,
    )
    stamp = retrieved_at or utc_now_iso()
    if entry is not None and (download_error is not None or extraction_error is not None):
        raise DocumentIndexerError(
            "An indexed document cannot also carry a download_error or extraction_error."
        )
    if entry is None and extraction_error is not None:
        if download_error is not None:
            raise DocumentIndexerError(
                "A document cannot be both download_failed and extraction_failed."
            )
        if type(extraction_error) is not str or not extraction_error.strip():
            raise DocumentIndexerError("extraction_error must be a non-empty str.")
        sha_low = sha256.lower() if type(sha256) is str else None
        if sha_low is None or not SHA256_RE.fullmatch(sha_low):
            raise CacheIntegrityError(
                "An extraction_failed record requires the downloaded document's sha256."
            )
        if type(byte_count) is not int or byte_count <= 0:
            raise CacheIntegrityError(
                "An extraction_failed record requires the downloaded document's positive byte count."
            )
        return {
            "nct_id": validated_nct,
            "filename": safe_filename,
            "doc_type": doc_type,
            "url": url,
            "http_status": http_status,
            "download_error": None,
            "extraction_error": extraction_error,
            "sha256": sha_low,
            "bytes": byte_count,
            "physical_pages": None,
            "pages_without_text": [],
            "readability_status": "extraction_failed",
            "unreadable_recovery_bucket": None,
            "redaction_markers": [],
            "version_label": "",
            "version_date_as_printed": "",
            "api_doc_date": api_doc_date,
            "upload_date": upload_date,
            "upload_timing": timing,
            "pages_selected": [],
            "pages_deferred": [],
            "unreadable_statistical_pages": [],
            "retrieved_at": stamp,
            "cache_hit": cache_hit,
        }
    if entry is None:
        return {
            "nct_id": validated_nct,
            "filename": safe_filename,
            "doc_type": doc_type,
            "url": url,
            "http_status": http_status,
            "download_error": download_error or "download_failed",
            "extraction_error": None,
            "sha256": None,
            "bytes": None,
            "physical_pages": None,
            "pages_without_text": [],
            "readability_status": "download_failed",
            "unreadable_recovery_bucket": None,
            "redaction_markers": [],
            "version_label": "",
            "version_date_as_printed": "",
            "api_doc_date": api_doc_date,
            "upload_date": upload_date,
            "upload_timing": timing,
            "pages_selected": [],
            "pages_deferred": [],
            "unreadable_statistical_pages": [],
            "retrieved_at": stamp,
            "cache_hit": cache_hit,
        }

    return {
        "nct_id": validated_nct,
        "filename": safe_filename,
        "doc_type": doc_type,
        "url": url,
        "http_status": http_status,
        "download_error": None,
        "extraction_error": None,
        "sha256": entry.sha256,
        "bytes": entry.byte_count,
        "physical_pages": entry.physical_pages,
        "pages_without_text": list(entry.pages_without_text),
        "readability_status": entry.readability_status,
        "unreadable_recovery_bucket": entry.unreadable_recovery_bucket,
        "redaction_markers": list(entry.redaction.redaction_flags),
        "version_label": entry.version_label,
        "version_date_as_printed": entry.version_date_as_printed,
        "api_doc_date": api_doc_date,
        "upload_date": upload_date,
        "upload_timing": timing,
        "pages_selected": list(entry.section_slice.pages_selected),
        "pages_deferred": list(entry.section_slice.pages_deferred),
        "unreadable_statistical_pages": list(entry.redaction.unreadable_statistical_pages),
        "retrieved_at": stamp,
        "cache_hit": cache_hit,
    }


def build_trial_document_profile(
    nct_id: str,
    *,
    documents: Sequence[TrialDocumentRecord],
    excluded_icf_count: int = 0,
    extracted_at: str | None = None,
    pipeline_commit: str | None = None,
    indexer_rules_version: str = INDEXER_RULES_VERSION,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> TrialDocumentProfile:
    """Assemble a TrialDocumentProfile behind the in_audit firewall.

    The shape is meant for Modules 3 and 4, which are not on this branch;
    compatibility with them is untested here.

    A record for another trial is refused with its position in ``documents``
    and no id: the record's id was never checked against the holdout (it may
    be a holdout trial's), and the profile's own id is left out too, as every
    firewall message leaves it out.
    """
    nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )

    for position, doc in enumerate(documents):
        try:
            doc_nct = validate_nct_id(doc.get("nct_id"))
        except InvalidNctIdError as exc:
            raise InvalidNctIdError(f"TrialDocumentRecord at position {position}: {exc}") from None
        if doc_nct != nct:
            raise DocumentIndexerError(
                f"TrialDocumentRecord at position {position} names a different trial than the "
                "profile; a profile holds only its own trial's records (neither id is printed)."
            )
        forbidden = _FORBIDDEN_PROFILE_TEXT_KEYS.intersection(doc.keys())
        if forbidden:
            raise DocumentIndexerError(
                f"TrialDocumentRecord at position {position} must not embed raw page text keys: "
                f"{sorted(forbidden)}"
            )

    sorted_docs = sorted(documents, key=lambda d: d["filename"])
    summary = summarize_trial_readability(sorted_docs)

    flag_set: set[RedactionFlag] = set()
    for doc in sorted_docs:
        for flag in doc["redaction_markers"]:
            flag_set.add(flag)
    ordered_flags: list[RedactionFlag] = [f for f in REDACTION_FLAGS if f in flag_set]
    # A total is stated only when every document's page count is known. A
    # download_failed or extraction_failed document has no count (None), and
    # summing the others would present an undercount as the total.
    page_counts = [d["physical_pages"] for d in sorted_docs]
    total_pages: int | None = (
        sum(c for c in page_counts if c is not None)
        if all(c is not None for c in page_counts)
        else None
    )

    return {
        "nct_id": nct,
        "indexer_rules_version": indexer_rules_version,
        "extracted_at": extracted_at or utc_now_iso(),
        "pipeline_commit": pipeline_commit,
        "readability_summary": summary,
        "redaction_flags": ordered_flags,
        "excluded_icf_count": excluded_icf_count,
        "document_badge": {
            "readability_status": summary,
            "redaction_flags": ordered_flags,
            "doc_count": len(sorted_docs),
            "total_physical_pages": total_pages,
        },
        "documents": sorted_docs,
    }


def to_legacy_census_document_row(
    doc: TrialDocumentRecord,
    *,
    model_id: str = DEFAULT_DOCUMENT_MODEL_ID,
    profile_version: str = INDEXER_RULES_VERSION,
) -> dict[str, Any]:
    """Project a TrialDocumentRecord onto the 14-key documents.jsonl census schema with F6 provenance."""
    if type(model_id) is not str or not model_id.strip():
        raise DocumentIndexerError(f"model_id must be a non-empty str, got {model_id!r}")
    if type(profile_version) is not str or not profile_version.strip():
        raise DocumentIndexerError(
            f"profile_version must be a non-empty str, got {profile_version!r}"
        )
    nct = validate_nct_id(doc.get("nct_id"))
    fname = validate_safe_pdf_filename(doc.get("filename"))  # type: ignore[arg-type]
    return {
        "nct_id": nct,
        "filename": fname,
        "url": doc["url"],
        "http_status": doc["http_status"],
        "sha256": doc["sha256"],
        "bytes": doc["bytes"],
        "doc_type": doc["doc_type"],
        "version_label": doc["version_label"],
        "version_date_as_printed": doc["version_date_as_printed"],
        "physical_pages": doc["physical_pages"],
        "pages_without_text": list(doc["pages_without_text"]),
        "retrieved_at": doc["retrieved_at"],
        "model_id": model_id,
        "profile_version": profile_version,
    }


def _census_timestamp(value: Any, *, where: str, field: str) -> str:
    """Return ``value`` when it is an ISO-8601 timestamp string; ``DocumentIndexerError`` otherwise.

    ``retrieved_at`` is a provenance stamp carried into the profile as the
    census recorded it. A string that ``datetime.fromisoformat`` cannot read is
    not a time at all (it could be a trial id), so it is refused rather than
    carried. So is a string it does read that holds 8 digits in a row
    (``_TRIAL_ID_IN_FILENAME_RE``, the rule ``validate_safe_pdf_filename``
    holds a filename to): on Python 3.11 it reads the basic form
    ``YYYYMMDD`` and fractional seconds of any length, so a trial id's
    digits passed as a date. The extended form this module writes
    (``utc_now_iso``, at most 6 digits in a row) passes. The value is not
    printed.
    """
    if type(value) is not str or not value.strip():
        raise DocumentIndexerError(
            f"Census row at {where} lacks a non-empty string {field!r}; legacy rows must "
            "carry their own provenance and are not filled in."
        )
    try:
        datetime.fromisoformat(value)
        if _TRIAL_ID_IN_FILENAME_RE.search(value):
            raise ValueError("reads as a trial id")
    except ValueError:
        raise DocumentIndexerError(
            f"Census row at {where} field {field!r} is not an ISO-8601 timestamp, or holds "
            "8 digits in a row (which read as a trial id); the row is refused, not carried "
            "(the value is not printed)."
        ) from None
    return value


def _census_optional_date(value: Any, *, where: str, field: str) -> str | None:
    """Return a census date field: null, or a string ``parse_ctgov_date`` reads as a date.

    ``upload_date`` and ``api_doc_date`` are copied into the profile and its
    upload-timing facts. An object, a list, a number or a string that is not
    a CT.gov date (it could be a trial id) is refused, not copied; null (or an
    absent key) is recorded as absent. ``parse_ctgov_date`` reads the whole
    string, so a valid date with text after it (``"2020-01-01 NCT..."``) is
    not a date and is refused too. The value is not printed.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise DocumentIndexerError(
            f"Census row at {where} field {field!r} must be a date string or null "
            f"(got {type(value).__name__}); it is not copied into the profile."
        )
    if parse_ctgov_date(value)["date_precision"] == "unknown":
        raise DocumentIndexerError(
            f"Census row at {where} field {field!r} is not a CT.gov date (YYYY, YYYY-MM, "
            "YYYY-MM-DD[...] or MM/DD/YYYY[ HH:MM]); the row is refused (the value is not "
            "printed)."
        )
    return value


def _census_http_status(value: Any, *, where: str) -> int | None:
    """Return a census ``http_status``: null (no response), or an int HTTP status 100..599.

    A string (``"200"``), a boolean (``true`` is not 200) or any other value is
    refused: read loosely, ``"200"`` turned a downloaded document into
    ``download_failed`` and a string was written into the profile as
    ``legacy_http_status_<value>``. The value is not printed.
    """
    if value is None:
        return None
    if type(value) is not int or not 100 <= value <= 599:
        shown = "int outside 100..599" if type(value) is int else type(value).__name__
        raise DocumentIndexerError(
            f"Census row at {where} field 'http_status' must be an int HTTP status or null "
            f"(got {shown}); the row is refused."
        )
    return value


def ingest_legacy_census_documents(
    documents_jsonl: Path,
    *,
    cache_store: DocumentCacheStore,
    provider: DocumentPageExtractor | None = None,
    local_pdf_dir: Path | None = None,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
    extracted_at: str | None = None,
    pipeline_commit: str | None = None,
    indexer_rules_version: str = INDEXER_RULES_VERSION,
    min_selected_pages: int = 10,
    max_selected_pages: int = 25,
    reparse_cached_pages: bool = False,
    max_trials: int | None = None,
) -> tuple[list[DocumentIndexEntry], list[TrialDocumentProfile]]:
    """Offline-ingest legacy rule-census documents.jsonl rows into DocumentCacheStore.

    Enforces strict ASCII ``^NCT[0-9]{8}$`` and the in_audit firewall on every
    row BEFORE touching local PDFs, page providers, or cache state, with the
    holdout set and allowlist of ``cache_store`` merged into this call's
    (``DocumentCacheStore.merge_firewall_sets``):
    - Any row with ``nct_id`` in ``audit_nct_ids`` raises ``AuditHoldoutError``.
    - If ``"in_audit"`` is present on a row, any value other than strict ``bool``
      ``False`` raises ``AuditHoldoutError`` unconditionally (even if ``nct_id``
      is in ``allowed_nct_ids``).
    - If ``"in_audit"`` is absent on a row, ``allowed_nct_ids`` from a verified
      manifest MUST be supplied and contain ``nct_id``; otherwise raises
      ``AuditHoldoutError`` (never falls back to ``False``).

    Provenance is carried, never supplied: every row must have non-empty
    ``url`` and ``doc_type`` and an ISO-8601 ``retrieved_at``
    (``DocumentIndexerError`` otherwise). The ingest time is the profile's
    ``extracted_at``, not any document's ``retrieved_at``.

    The ``url`` must be the row trial's own posted ``filename``
    (``require_own_large_doc_url``; ``AuditHoldoutError`` otherwise), for a
    downloaded row and a failed one alike: a row's ``nct_id`` passing the
    firewall says nothing about a URL naming another trial, which the profile
    would carry. A row naming any key twice is refused (``loads_strict_json``).

    Every other field the profile carries is type-checked in pass 1, never
    copied as given (``DocumentIndexerError``): ``http_status`` is an int
    HTTP status or null; ``upload_date`` and ``api_doc_date`` are CT.gov date
    strings or null; a row with ``http_status`` 200 carries a 64-hex
    ``sha256`` and a positive int ``bytes`` (a 200 row without a digest used
    to become ``download_failed`` with no word). A trial's posted file is one
    document: two rows for one ``(nct_id, filename)`` are refused, identical or
    not, as the page-map loader refuses one trial's file mapped to two digests.

    No cache writes unless the whole ingest succeeds. Pass 1 validates every
    row before anything is read from disk or indexed. Pass 2 runs inside
    ``DocumentCacheStore.staged_writes``: a failure there (a local PDF that
    leaves its trial's folder or disagrees with its row's digest or byte
    count, a page map tagged with a holdout trial, any integrity error) raises
    and writes nothing, so no trial is left cached without a profile. A
    document whose pages no provider can extract (``PageExtractionError``) is
    not a failure of the ingest: it is recorded as ``extraction_failed`` with
    its digest and byte count, as the live path records it.

    Local PDFs are found with ``resolve_local_trial_pdf``, which refuses a file
    (or trial folder) linking outside ``<local_pdf_dir>/<nct_id>/`` before it is
    read. A local PDF's byte count must equal the row's ``bytes``.

    ``max_trials`` bounds the work, not only the output: every row is still
    validated in pass 1 (one bad row refuses the whole file), then only the
    first ``max_trials`` trials in ``nct_id`` order (the order profiles are
    written in) are indexed, cached and profiled. None indexes every trial.
    """
    if max_trials is not None and (type(max_trials) is not int or max_trials < 1):
        raise DocumentIndexerError(
            f"max_trials must be a positive int or None, got {type(max_trials).__name__}."
        )
    validate_page_selection_bounds(min_selected_pages, max_selected_pages)
    # The firewall is the store's merged with this call's (the union of holdout
    # sets, the intersection of allowlists): a store built with a holdout set
    # refuses that trial's rows, a failed-download row included, whatever this
    # call's own (defaulted) arguments say.
    validated_audit, validated_allowed = cache_store.merge_firewall_sets(
        audit_nct_ids, allowed_nct_ids
    )

    src_path = Path(documents_jsonl)
    if not src_path.exists():
        raise DocumentIndexerError(f"Census documents JSONL does not exist: {src_path}")

    # Pass 1 validates every row before anything is read from disk or indexed.
    # Firewall messages give the line number, never the row's id (which may be
    # a holdout trial's), and no message echoes a field's value.
    rows_by_nct: dict[str, list[dict[str, Any]]] = {}
    first_line_of_file: dict[tuple[str, str], int] = {}
    with src_path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            where = f"{documents_jsonl}:{line_no}"
            try:
                # A row naming nct_id, in_audit or url twice is refused, not read last-wins.
                row = loads_strict_json(line, origin=where)
            except json.JSONDecodeError as exc:
                raise DocumentIndexerError(
                    f"Census row at {where} is not valid JSON: {exc.msg}"
                ) from None
            if not isinstance(row, Mapping):
                raise DocumentIndexerError(
                    f"Census row at {where} must be a JSON object."
                )
            try:
                nct_id = validate_nct_id(row.get("nct_id"))
            except InvalidNctIdError as exc:
                raise InvalidNctIdError(f"Census row at {where}: {exc}") from None
            if nct_id in validated_audit:
                raise AuditHoldoutError(
                    f"Census row at {where} is for an audit holdout trial; "
                    "audit holdout trials are forbidden."
                )
            if "in_audit" in row:
                row_in_audit = row["in_audit"]
                if type(row_in_audit) is not bool:
                    raise AuditHoldoutError(
                        f"Census row at {where} has non-bool in_audit "
                        f"(type={type(row_in_audit).__name__}); explicit boolean False required."
                    )
                if row_in_audit is True:
                    raise AuditHoldoutError(
                        f"Census row at {where} has in_audit=True; "
                        "audit holdout trials are forbidden."
                    )
                if validated_allowed is not None and nct_id not in validated_allowed:
                    raise AuditHoldoutError(
                        f"Census row at {where} is for a trial not in allowed_nct_ids; "
                        "unlisted trials are blocked by the audit holdout firewall."
                    )
            else:
                if validated_allowed is None:
                    raise AuditHoldoutError(
                        f"Census row at {where} omits 'in_audit' and no verified "
                        "allowed_nct_ids allowlist was supplied; implicit in_audit=False "
                        "fallback is forbidden."
                    )
                if nct_id not in validated_allowed:
                    raise AuditHoldoutError(
                        f"Census row at {where} is for a trial not in allowed_nct_ids; "
                        "unlisted trials are blocked by the audit holdout firewall."
                    )
            row_filename = validate_safe_pdf_filename(row.get("filename"))  # type: ignore[arg-type]
            # The census documents.jsonl schema requires url, doc_type and
            # retrieved_at on every row (they are among the 14 keys
            # to_legacy_census_document_row projects back out). A row without
            # one is refused: a default would put a document type, a URL or a
            # retrieval time into the output that the source never recorded.
            for required in ("url", "doc_type"):
                value = row.get(required)
                if type(value) is not str or not value.strip():
                    raise DocumentIndexerError(
                        f"Census row at {where} lacks a non-empty string {required!r}; "
                        "legacy rows must carry their own provenance and are not "
                        "filled in."
                    )
            _census_timestamp(row.get("retrieved_at"), where=where, field="retrieved_at")
            if row["doc_type"] not in ("protocol", "sap", "combined"):
                raise DocumentIndexerError(
                    f"Census row at {where} has an invalid doc_type (the value is not printed)."
                )
            # The url is the document the record will name. It must be this
            # row's own trial's posted file, whatever the row's http_status: a
            # failed row's url is carried into the profile just the same.
            require_own_large_doc_url(
                row["url"], nct_id=nct_id, filename=row_filename, origin=f"Census row at {where}"
            )
            # Every other field the profile carries is checked here, not
            # copied as given (see the docstring).
            http_status = _census_http_status(row.get("http_status"), where=where)
            _census_optional_date(row.get("upload_date"), where=where, field="upload_date")
            _census_optional_date(row.get("api_doc_date"), where=where, field="api_doc_date")
            if http_status == 200:
                raw_sha = row.get("sha256")
                if type(raw_sha) is not str or not SHA256_RE.fullmatch(raw_sha.lower()):
                    raise DocumentIndexerError(
                        f"Census row at {where} has http_status 200 but no valid sha256 (64 "
                        "hex characters); a downloaded document's digest is required, and "
                        "the row is not recorded as a failed download instead."
                    )
                raw_bytes = row.get("bytes")
                if type(raw_bytes) is not int or raw_bytes < 1:
                    shown = "int below 1" if type(raw_bytes) is int else type(raw_bytes).__name__
                    raise DocumentIndexerError(
                        f"Census row at {where} has http_status 200 but 'bytes' is not a "
                        f"positive int (got {shown})."
                    )
            # One posted file, one row. Two rows for it (whether or not they
            # agree) are refused: both would be indexed and profiled as two
            # documents, and a consumer keyed by (nct_id, filename) would pick
            # one silently.
            file_key = (nct_id, row_filename)
            if file_key in first_line_of_file:
                raise DocumentIndexerError(
                    f"Census rows at lines {first_line_of_file[file_key]} and {line_no} of "
                    f"{documents_jsonl} name the same trial's posted file; one file is one "
                    "document, so the census is refused rather than recording it twice or "
                    "choosing one row."
                )
            first_line_of_file[file_key] = line_no
            rows_by_nct.setdefault(nct_id, []).append(dict(row))

    stamp = extracted_at or utc_now_iso()
    indexed_entries: dict[str, DocumentIndexEntry] = {}
    profiles: list[TrialDocumentProfile] = []

    # The trials this call indexes, chosen after the whole file has passed
    # pass 1 and before any cache write (see max_trials above).
    selected_ncts = sorted(rows_by_nct)
    if max_trials is not None:
        selected_ncts = selected_ncts[:max_trials]

    # Pass 2: every cache write is staged and lands only if every trial is
    # indexed and profiled (see the docstring).
    with cache_store.staged_writes():
        for nct_id in selected_ncts:
            trial_docs: list[TrialDocumentRecord] = []
            for row in sorted(rows_by_nct[nct_id], key=lambda r: str(r.get("filename", ""))):
                filename = validate_safe_pdf_filename(row["filename"])
                # Every field below passed pass 1 and is carried through as
                # the census recorded it.
                doc_type = row["doc_type"]
                url = row["url"]
                http_status: int | None = row.get("http_status")
                retrieved_at = row["retrieved_at"]
                upload_date: str | None = row.get("upload_date")
                api_doc_date: str | None = row.get("api_doc_date")

                if http_status != 200:
                    trial_docs.append(
                        build_trial_document_record(
                            nct_id=nct_id,
                            filename=filename,
                            doc_type=doc_type,
                            url=url,
                            http_status=http_status,
                            entry=None,
                            download_error=f"legacy_http_status_{http_status}",
                            api_doc_date=api_doc_date,
                            upload_date=upload_date,
                            retrieved_at=retrieved_at,
                            cache_hit=False,
                            in_audit=False,
                            audit_nct_ids=validated_audit,
                            allowed_nct_ids=validated_allowed,
                        )
                    )
                    continue

                sha = row["sha256"].lower()
                byte_count: int = row["bytes"]
                pdf_path: Path | None = None
                if local_pdf_dir is not None:
                    # Refuses a file (or trial folder) that links outside
                    # <local_pdf_dir>/<nct_id>/ before anything is read.
                    local = resolve_local_trial_pdf(local_pdf_dir, nct_id, filename)
                    if local is not None:
                        actual_sha, actual_bytes = sha256_file(local)
                        if actual_sha != sha:
                            raise CacheIntegrityError(
                                f"Local PDF {filename} of an in-scope trial "
                                f"does not hash to its census row's sha256={sha}; the census is "
                                "refused (the file's own digest is not printed)."
                            )
                        if actual_bytes != byte_count:
                            raise CacheIntegrityError(
                                f"Local PDF {filename} of an in-scope trial has "
                                f"{actual_bytes} bytes but its census row says {byte_count}; "
                                "the row's byte count is not overridden silently."
                            )
                        pdf_path = local

                try:
                    entry, cache_hit = cache_store.get_or_index_document(
                        sha256=sha,
                        byte_count=byte_count,
                        provider=provider,
                        pdf_path=pdf_path,
                        nct_id=nct_id,
                        filename=filename,
                        doc_type=doc_type,
                        extracted_at=stamp,
                        pipeline_commit=pipeline_commit,
                        indexer_rules_version=indexer_rules_version,
                        min_selected_pages=min_selected_pages,
                        max_selected_pages=max_selected_pages,
                        reparse_cached_pages=reparse_cached_pages,
                        in_audit=False,
                        audit_nct_ids=validated_audit,
                        allowed_nct_ids=validated_allowed,
                    )
                except PageExtractionError as exc:
                    # The digest and size are known (the census row carries
                    # them); only the pages could not be extracted. Recorded
                    # as extraction_failed, as the live path records it,
                    # rather than aborting the ingest.
                    trial_docs.append(
                        build_trial_document_record(
                            nct_id=nct_id,
                            filename=filename,
                            doc_type=doc_type,
                            url=url,
                            http_status=200,
                            entry=None,
                            extraction_error=f"page_extraction_error: {exc}",
                            sha256=sha,
                            byte_count=byte_count,
                            api_doc_date=api_doc_date,
                            upload_date=upload_date,
                            retrieved_at=retrieved_at,
                            cache_hit=False,
                            in_audit=False,
                            audit_nct_ids=validated_audit,
                            allowed_nct_ids=validated_allowed,
                        )
                    )
                    continue
                indexed_entries[entry.sha256] = entry
                trial_docs.append(
                    build_trial_document_record(
                        nct_id=nct_id,
                        filename=filename,
                        doc_type=doc_type,
                        url=url,
                        http_status=200,
                        entry=entry,
                        api_doc_date=api_doc_date,
                        upload_date=upload_date,
                        retrieved_at=retrieved_at,
                        cache_hit=cache_hit,
                        in_audit=False,
                        audit_nct_ids=validated_audit,
                        allowed_nct_ids=validated_allowed,
                    )
                )

            profiles.append(
                build_trial_document_profile(
                    nct_id,
                    documents=trial_docs,
                    excluded_icf_count=0,
                    extracted_at=stamp,
                    pipeline_commit=pipeline_commit,
                    indexer_rules_version=indexer_rules_version,
                    in_audit=False,
                    audit_nct_ids=validated_audit,
                    allowed_nct_ids=validated_allowed,
                )
            )

    return [indexed_entries[k] for k in sorted(indexed_entries)], profiles
