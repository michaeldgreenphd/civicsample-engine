#!/usr/bin/env python3
"""Content-addressed ClinicalTrials.gov trial document pre-indexer & cache builder.

Fetches (or loads offline from local caches / page maps / legacy census JSONL)
posted clinical trial Protocols and Statistical Analysis Plans, classifies
readability and redaction markers, computes precision-aware factual upload
timing deltas, and slices high-yield statistical pages into deterministic
Pass-1 / Pass-2 partitions.

Runtime and platform dependencies:
- Imports ``requests`` and ``tqdm`` from ``requirements.txt`` (alongside
  ``src.api_client.CTGovAPIClient`` for live CT.gov API v2 queries).
- Live PDF text extraction via ``MacOSPDFKitProvider`` requires macOS and uses
  ``clang`` + Apple's ``Foundation``/``PDFKit`` frameworks (or ``ctypes.CDLL``
  bindings to ``libobjc.A.dylib`` + ``PDFKit.framework``). On non-macOS
  platforms without a pre-extracted ``--page-map-jsonl`` or cached page map in
  ``<cache-dir>/pages/``, extraction fails loudly with ``PageExtractionError``.

Hard safety guarantees:
- Enforces strict ASCII ``^NCT[0-9]{8}$`` validation (no stripping, no
  case-folding, no zero-width characters, no Unicode digits) BEFORE any audit
  check across every CLI and library entry point.
- Enforces ``enforce_not_in_audit()`` and manifest allowlist membership before
  any API query, CDN download, cache lookup, or PDF parse (including verifying
  the ``nctId`` returned inside CT.gov ``study`` payloads).
- Running with no target arguments (neither ``--manifest``, ``--nct-id``, nor
  ``--ingest-census-jsonl``) prints usage to ``stderr`` and exits with status 2
  without creating, touching, or truncating output or cache paths. So does any
  run left with no trial to index: a manifest whose rows are all
  ``in_audit=True``, ``--limit`` below 1, or an empty census file.
- Ambiguous input is refused with exit 2 before anything is read or written,
  never resolved one way: an option that takes one value given twice (a
  second ``--manifest`` used to replace the first, and its holdout set with
  it), ``--nct-id`` together with ``--ingest-census-jsonl``, a manifest or
  census JSON object naming a key twice, and a manifest object carrying more
  than one row list. A trial named twice with ``--nct-id`` is fetched once.
- Options are checked before anything is fetched, read or written (exit 2):
  ``--min-selected-pages``/``--max-selected-pages`` must satisfy
  ``1 <= min <= max``; ``--rate-limit-sec`` must be a finite number >= 0 (no
  clamping); ``--local-pdf-dir`` must be an existing directory; and neither
  ``--profiles-out`` nor ``--index-out`` may name an input file (the
  manifest, the census, a page map), anything inside ``--local-pdf-dir``,
  the cache's own files or its pages/ and blobs/ folders, ``--cache-dir``
  itself or a folder holding it, an existing directory, or the other
  output. Paths are compared after resolving links and ``..``.
- A manifest marks a holdout wherever it says ``in_audit: true``: an object
  outside the row list (an ``excluded`` list, a row's nested value) adds its
  trial to the holdout set, and one marked in_audit=False in the rows and
  in_audit=True elsewhere is refused (exit 2).
- A local PDF is read only from ``<local-pdf-dir>/<nct_id>/`` itself: a file
  or trial folder that is a symlink leading outside it (into a holdout
  trial's folder, say) is refused before it is opened (exit 2). A local PDF
  whose byte count is not the payload's ``size`` is refused (exit 1).
- A study payload is read strictly, before any request: a filename in two
  largeDocs entries, a malformed ``largeDocs`` list or entry (a non-object,
  a missing filename, a non-boolean ``hasProtocol``/``hasSap``, a size that
  is not a non-negative int), a filename that is not a plain ASCII basename,
  or a date that is not a CT.gov date is refused (exit 1); a filename that
  reads as a trial id is a firewall refusal (exit 2), and so, with no holdout
  set known, is an ``nctIdAliases`` entry not on the manifest's allowlist.
- A live run is all-or-nothing in the cache: its writes are staged and land
  only when every trial has been indexed, so a firewall or integrity refusal
  part-way through leaves no earlier trial cached without a profile.
- The census ingest type-checks every field it carries (``http_status`` an
  int or null, dates CT.gov date strings or null, ``retrieved_at`` an ISO-8601
  timestamp, a 200 row with a digest and positive ``bytes``), refuses two
  rows for one trial's file, records an unextractable document as
  ``extraction_failed``, and writes nothing to the cache unless the whole
  ingest succeeds.
- The document index is read strictly: a repeated key, a coerced type, a
  missing or extra field, two lines for one ``(sha256, rules version)`` or a
  page number outside the document stop the run with the file left as it is.
- A cached index entry is reused only for the ``--min-selected-pages``,
  ``--max-selected-pages`` and document type it was computed with. A run with
  other values re-indexes the document (from its cached page map when the
  cache holds one, so with no new extraction), and the index line is replaced
  by this run's; a document two trials post with different types keeps the
  last one's line.
- ``--limit`` with ``--ingest-census-jsonl`` bounds the trials indexed and
  cached, not only the profiles written.
- A document URL (a download's, a census row's, a profile record's) must be
  the canonical CT.gov URL of that trial's own posted file; a CT.gov payload
  whose ``nctIdAliases`` names a holdout trial is that trial and is refused.
- A trial whose CT.gov record cannot be fetched (HTTP or network error) is
  named on ``stderr`` and skipped; the other trials' profiles are written and
  the run exits 1.
- ``pipeline_commit`` is the commit of this script's checkout (or
  ``GITHUB_SHA``), whatever the working directory; when it cannot be resolved
  it is recorded as null and the reason is printed to ``stderr``.
- Firewall errors never print a trial id: they give the file, line or row
  index, and counts.
- Enforces a global inter-request rate-limit lock (<cache-dir>/.ctgov_rate_limit.lock)
  defaulting to 0.55s across processes.
- Downloads PDFs atomically via .part staging and verifies the %PDF- header and
  positive byte count before committing to the content-addressed cache.
  Redirects are not followed (a 3xx is a failed download) and a response
  that reports one is refused; an existing blob is kept only if it hashes to
  its digest, and no file is handed to a page extractor unless it does.
- A downloaded PDF whose byte count is not the payload's ``size`` is a failed
  download of that document (``download_error`` ``download_size_mismatch``),
  counted with the other failed downloads, and the run goes on. Its bytes are
  not indexed or recorded in ``url_manifest.json``, and a blob the download
  created is removed (one that was already there is kept). With no payload
  ``size`` there is nothing to compare against and the download is taken. A
  ``url_manifest.json`` record skips the download only when its byte count,
  ``size`` and upload date are the payload's; a record that does not describe
  the bytes a run then verifies and indexes is replaced by one that does.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import AbstractSet, Any, Iterable, Mapping, Sequence

import requests

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.document_indexer import (  # noqa: E402
    INDEXER_RULES_VERSION,
    AuditHoldoutError,
    CacheIntegrityError,
    ChainedPageTextProvider,
    DocumentCacheStore,
    DocumentIndexerError,
    DocumentPageExtractor,
    InvalidNctIdError,
    JsonlPageMapProvider,
    MacOSPDFKitProvider,
    PageExtractionError,
    TrialDocumentProfile,
    TrialDocumentRecord,
    atomic_write_text,
    build_trial_document_profile,
    build_trial_document_record,
    enforce_not_in_audit,
    extract_in_scope_large_docs,
    has_pdf_magic,
    ingest_legacy_census_documents,
    loads_strict_json,
    require_own_large_doc_url,
    resolve_local_trial_pdf,
    resolve_pipeline_commit,
    sha256_file,
    utc_now_iso,
    validate_ctgov_date_field,
    validate_nct_id,
    validate_page_selection_bounds,
    validate_safe_pdf_filename,
    validate_study_payload_nct_id,
)


USER_AGENT = "CivicSample-Research/1.0 (academic research)"
DEFAULT_RATE_LIMIT_SEC = 0.55
DEFAULT_TIMEOUT_SEC = 60
# A manifest key is printed in a location only when it has the shape of a
# schema field name (lowercase ASCII letters and underscores), which cannot
# hold a trial id; any other key is shown as <key>.
_PRINTABLE_MANIFEST_KEY_RE = re.compile(r"[a-z_]{1,32}", re.ASCII)


def _resolve_pipeline_commit() -> str | None:
    """Return the commit of the checkout this script belongs to, warning on stderr when there is none.

    ``resolve_pipeline_commit`` asks git about this script's repository
    (``_REPO_ROOT``), not the working directory: ``src.utils.pipeline_commit``
    runs ``git rev-parse HEAD`` wherever the process happens to be, so a run
    from outside a checkout stamped None with no reason and a run from inside
    another repository stamped that repository's commit. A None stamp is kept
    (artifacts stay writable outside a checkout, as ``src.utils`` intends), but
    its reason is printed so the gap is visible in the run log.
    """
    commit, reason = resolve_pipeline_commit(_REPO_ROOT)
    if commit is None:
        print(
            f"WARNING: pipeline_commit will be recorded as null: {reason}",
            file=sys.stderr,
        )
    return commit


def validate_rate_limit_sec(value: Any) -> float:
    """Return ``value`` as a float when it is a finite number >= 0; raise ``DocumentIndexerError`` otherwise.

    The limiter used to clamp with ``max(0.0, float(value))``: ``-1`` and
    ``nan`` (``max(0.0, nan)`` is 0.0) both became "no rate limit" without a
    word, and ``inf`` would sleep forever. 0 is an explicit choice and allowed.
    """
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise DocumentIndexerError(
            "rate limit must be a finite number of seconds >= 0 "
            f"(got {type(value).__name__} {value!r}); it is not clamped."
        )
    return float(value)


class GlobalRateLimiter:
    """Cross-process inter-request rate limiter backed by a POSIX flock lock file."""

    def __init__(self, lock_path: Path, min_interval_sec: float = DEFAULT_RATE_LIMIT_SEC) -> None:
        self.lock_path = Path(lock_path)
        self.min_interval_sec = validate_rate_limit_sec(min_interval_sec)
        self._last_local_ts: float = 0.0

    def wait(self) -> None:
        """Sleep as needed so consecutive requests are separated by >= min_interval_sec.

        The last request time is the later of this limiter's own and the lock
        file's stamp. A stamp that cannot be used as a past time (unreadable
        text, ``nan``/``inf``, or a time after now: another process's clock
        running ahead, or a hand-edited file) is taken as "a request was made
        just now", so the wait is the full interval: never shorter than the
        limit (no request goes out early) and never longer than it (a stamp a
        day ahead used to sleep a day, and ``inf`` asked ``time.sleep`` for an
        infinite sleep, which raises ``OverflowError``). No single wait exceeds
        ``min_interval_sec``.
        """
        if self.min_interval_sec <= 0.0:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                now = time.time()
                last_ts = min(self._last_local_ts, now)
                if raw:
                    try:
                        stamp = float(raw)
                    except ValueError:
                        stamp = now
                    if not math.isfinite(stamp) or stamp > now:
                        stamp = now
                    last_ts = max(last_ts, stamp)
                elapsed = max(0.0, now - last_ts)
                if elapsed < self.min_interval_sec:
                    time.sleep(min(self.min_interval_sec, self.min_interval_sec - elapsed))
                    now = time.time()
                handle.seek(0)
                handle.truncate()
                handle.write(f"{now:.6f}\n")
                handle.flush()
                self._last_local_ts = now
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class DownloadResult:
    """One download's outcome. ``blob_created`` is True only when the download put a new file at ``local_path``.

    A caller writing into a ``DocumentCacheStore`` reports such a file
    (``note_blob_created``) so that a staged block that later fails removes
    it; a blob that already held these bytes was not created by this run.
    """

    ok: bool
    http_status: int | None
    sha256: str | None
    byte_count: int | None
    local_path: Path | None
    error: str | None
    blob_created: bool = False


def _merge_allowed_sets(
    allowed_nct_ids: AbstractSet[str] | None,
    in_scope_nct_ids: AbstractSet[str] | None,
) -> frozenset[str] | None:
    """Validate and intersect optional allowlist sets."""
    if allowed_nct_ids is None and in_scope_nct_ids is None:
        return None
    first = (
        frozenset(validate_nct_id(x) for x in allowed_nct_ids)
        if allowed_nct_ids is not None
        else None
    )
    second = (
        frozenset(validate_nct_id(x) for x in in_scope_nct_ids)
        if in_scope_nct_ids is not None
        else None
    )
    if first is not None and second is not None:
        return first & second
    return first if first is not None else second


def _session_get_accepts_allow_redirects(session: Any) -> bool:
    """True when ``session.get`` takes ``allow_redirects`` (a ``requests.Session`` always does).

    An injected session (a test double, a wrapper) may have a narrower
    ``get``. For one that does not take the keyword, redirects cannot be
    switched off here, and ``download_pdf_atomic`` relies on the response's
    ``url`` and ``history`` instead (see there).
    """
    if isinstance(session, requests.Session):
        return True
    try:
        params = inspect.signature(session.get).parameters
    except (TypeError, ValueError):
        return False
    return "allow_redirects" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _response_was_redirected(resp: Any, requested_url: str) -> bool:
    """True when the response says it came from anywhere but ``requested_url``.

    ``requests`` reports the final URL in ``resp.url`` and the redirects it
    followed in ``resp.history``. Either one showing a redirect means the bytes
    are not the requested trial's posted file (they may be another trial's,
    a holdout trial's included). ``resp.url`` is compared with the requested
    URL as given and as ``requests`` re-quotes it. A response exposing
    neither attribute (a minimal test double) shows no redirect.
    """
    history = getattr(resp, "history", None)
    if history:
        return True
    final_url = getattr(resp, "url", None)
    if final_url is None:
        return False
    return final_url not in (requested_url, requests.utils.requote_uri(requested_url))


def download_pdf_atomic(
    url: str,
    dest_dir: Path,
    *,
    session: Any,
    nct_id: str,
    filename: str,
    rate_limiter: GlobalRateLimiter | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> DownloadResult:
    """Download a PDF atomically via a .part file and verify %PDF- header and non-zero size.

    Firewall, before any request or directory is made: ``nct_id`` passes
    ``enforce_not_in_audit``, and ``url`` must be that trial's own posted
    ``filename`` (``require_own_large_doc_url``). Checking the id alone let an
    in-scope id carry any URL, a holdout trial's document included, into the
    content-addressed cache. A refused URL raises ``AuditHoldoutError``.

    The checked URL must also be the one the bytes come from. Redirects are
    not followed (``allow_redirects=False`` whenever the session's ``get``
    takes it), so a 3xx is a failed download (``http_status_3xx``), and a
    response whose ``history`` or final ``url`` shows a redirect anyway is
    refused (``redirected_response_refused``) before any byte is read or any
    directory made. Neither error names the redirect target. A followed
    redirect once let another trial's document be cached, parsed and profiled
    under the requested trial.

    The blob is content-addressed. When one already exists at the downloaded
    digest's path, its bytes are hashed: if they are not that digest's (a
    damaged file, or one swapped for another document), the verified download
    replaces it and a warning naming the digest is printed. Keeping the
    existing file and discarding the fresh bytes handed the extractor a file
    that is not its digest's.
    """
    validated_nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )
    safe_filename = validate_safe_pdf_filename(filename)
    require_own_large_doc_url(
        url, nct_id=validated_nct, filename=safe_filename, origin="download_pdf_atomic"
    )

    if rate_limiter is not None:
        rate_limiter.wait()

    get_kwargs: dict[str, Any] = {"timeout": timeout_sec, "stream": True}
    if _session_get_accepts_allow_redirects(session):
        get_kwargs["allow_redirects"] = False
    try:
        resp = session.get(url, **get_kwargs)
    except Exception as exc:
        return DownloadResult(
            ok=False,
            http_status=None,
            sha256=None,
            byte_count=None,
            local_path=None,
            error=f"request_error: {exc}",
        )

    status_code = getattr(resp, "status_code", None)
    if _response_was_redirected(resp, url):
        close = getattr(resp, "close", None)
        if callable(close):
            close()
        return DownloadResult(
            ok=False,
            http_status=status_code if type(status_code) is int else None,
            sha256=None,
            byte_count=None,
            local_path=None,
            error="redirected_response_refused",
        )
    if status_code != 200:
        return DownloadResult(
            ok=False,
            http_status=status_code if type(status_code) is int else None,
            sha256=None,
            byte_count=None,
            local_path=None,
            error=f"http_status_{status_code}",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    part_path = dest_dir / f".{validated_nct}_{safe_filename}.{os.getpid()}.part"
    digest = hashlib.sha256()
    total_bytes = 0
    header_buf = bytearray()

    try:
        with part_path.open("wb") as handle:
            if hasattr(resp, "iter_content"):
                chunks: Iterable[bytes] = resp.iter_content(chunk_size=64 * 1024)
            else:
                raw_content = getattr(resp, "content", b"")
                chunks = [raw_content] if raw_content else []
            for chunk in chunks:
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                total_bytes += len(chunk)
                if len(header_buf) < 1024:
                    header_buf.extend(chunk[: 1024 - len(header_buf)])
            handle.flush()

        if total_bytes == 0:
            part_path.unlink(missing_ok=True)
            return DownloadResult(
                ok=False,
                http_status=200,
                sha256=None,
                byte_count=0,
                local_path=None,
                error="empty_response_0_bytes",
            )

        if not has_pdf_magic(bytes(header_buf)):
            part_path.unlink(missing_ok=True)
            return DownloadResult(
                ok=False,
                http_status=200,
                sha256=None,
                byte_count=total_bytes,
                local_path=None,
                error="missing_pdf_magic_header",
            )

        sha = digest.hexdigest()
        blob_path = dest_dir / sha[:2] / f"{sha}.pdf"
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_created = False
        if blob_path.exists() and sha256_file(blob_path) == (sha, total_bytes):
            # The existing blob is verified to be these bytes; keep it.
            part_path.unlink(missing_ok=True)
        else:
            if blob_path.exists():
                print(
                    f"WARNING: cached blob for sha256={sha} did not hash to its digest; it "
                    "was replaced with the verified download.",
                    file=sys.stderr,
                )
            else:
                blob_created = True
            os.replace(part_path, blob_path)

        return DownloadResult(
            ok=True,
            http_status=200,
            sha256=sha,
            byte_count=total_bytes,
            local_path=blob_path,
            error=None,
            blob_created=blob_created,
        )
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        return DownloadResult(
            ok=False,
            http_status=200,
            sha256=None,
            byte_count=None,
            local_path=None,
            error=f"write_error: {exc}",
        )


def load_manifest_rows(
    manifest_path: Path,
    *,
    audit_nct_ids: AbstractSet[str] | None = None,
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """Load manifest rows (outcomes/index.json, outcomes_bundles/index.json, JSON array, or JSONL) with strict bool in_audit.

    Returns (in_scope_rows, audit_nct_ids). Enforces strict ASCII ``^NCT[0-9]{8}$``
    before checking ``in_audit``, rejects missing or non-``bool`` ``in_audit`` rows,
    and refuses an empty ``audit_nct_ids`` set whenever the manifest contains any
    ``in_audit == True`` rows.

    Ambiguous manifests are refused with ``AuditHoldoutError``, never read one
    way: a JSON object (a row or the wrapper) that names any key twice
    (``json.loads`` would keep the last, so ``{"in_audit": true, "in_audit":
    false}`` read as False), and a wrapper object carrying more than one of
    ``data``, ``rows``, ``trials`` or a top-level ``nct_id`` (only the first was
    read, so a holdout row in a second list was never seen). Other top-level
    keys (``run_id``, ``schema_version``, ``excluded``) are allowed.

    No ``in_audit: true`` in the file is dropped. Every JSON object anywhere
    in the file that is not one of the rows read (under another top-level key
    such as ``excluded``, or nested inside a row) and carries ``in_audit`` is
    read for it: ``true`` adds its strict ``nct_id`` to the holdout set
    (refused if it names none, since that holdout cannot be honoured);
    ``false`` adds nothing (only the row list authorizes indexing); any other
    value is refused. A trial marked in_audit=False in the row list and
    in_audit=True elsewhere is refused as a conflict. Such objects used to be
    ignored, so a holdout marked outside the row list was missing from the
    holdout set, and a payload whose alias named it was indexed. Messages
    give positions, never ids.
    """
    path = Path(manifest_path)
    if not path.exists():
        raise DocumentIndexerError(f"Manifest file does not exist: {path}")

    caller_audit: frozenset[str] | None = (
        frozenset(validate_nct_id(item) for item in audit_nct_ids)
        if audit_nct_ids is not None
        else None
    )

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise AuditHoldoutError(
            f"Manifest {path} is empty; a non-empty verified manifest is required."
        )

    def _extract_mapping_list(seq: Any, label: str) -> list[Mapping[str, Any]]:
        if not isinstance(seq, list):
            raise AuditHoldoutError(f"Manifest {path} field {label!r} must be a JSON list.")
        out: list[Mapping[str, Any]] = []
        for index, item in enumerate(seq):
            if not isinstance(item, Mapping):
                # The row itself is not printed: it may hold a holdout id.
                raise AuditHoldoutError(
                    f"Manifest {path} row {index} in {label!r} is "
                    f"{type(item).__name__}, not a JSON object."
                )
            out.append(item)
        return out

    rows: list[Mapping[str, Any]] = []
    # Every JSON value the file holds, by location, for the in_audit scan
    # below: the parsed document, or each JSONL line object.
    scanned_roots: list[tuple[str, Any]] = []
    parsed: Any = None
    parsed_single_json = False
    if raw_text.startswith("{") or raw_text.startswith("["):
        try:
            # A repeated key anywhere raises AuditHoldoutError here; only a
            # JSONDecodeError (e.g. "Extra data" in a JSONL file) falls through
            # to line-by-line reading, where each line is parsed strictly too.
            parsed = loads_strict_json(raw_text, origin=f"manifest {path}")
            parsed_single_json = True
        except json.JSONDecodeError:
            parsed_single_json = False

    if parsed_single_json:
        scanned_roots.append(("the document root", parsed))
        if isinstance(parsed, Mapping):
            # A wrapper holds exactly one source of rows. Reading the first of
            # several and ignoring the rest let a holdout row under a second
            # list go unseen, so more than one is refused.
            row_sources = [key for key in ("data", "rows", "trials", "nct_id") if key in parsed]
            if len(row_sources) > 1:
                raise AuditHoldoutError(
                    f"Manifest {path} JSON object carries more than one of 'data', 'rows', "
                    f"'trials' and a top-level 'nct_id' ({', '.join(row_sources)}); a manifest "
                    "holds one row list, so it is ambiguous and refused."
                )
            if "data" in parsed:
                rows = _extract_mapping_list(parsed["data"], "data")
            elif "rows" in parsed:
                rows = _extract_mapping_list(parsed["rows"], "rows")
            elif "trials" in parsed:
                rows = _extract_mapping_list(parsed["trials"], "trials")
            elif "nct_id" in parsed:
                rows = [parsed]
            else:
                raise AuditHoldoutError(
                    f"Manifest {path} JSON object lacks 'data', 'rows', or 'trials' list."
                )
        elif isinstance(parsed, list):
            rows = _extract_mapping_list(parsed, "root")
        else:
            raise AuditHoldoutError(
                f"Manifest {path} root JSON value must be an object or array."
            )
    else:
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = loads_strict_json(line, origin=f"line {line_no} of manifest {path}")
            except json.JSONDecodeError as exc:
                raise DocumentIndexerError(
                    f"Malformed JSON on line {line_no} of manifest {path}: {exc}"
                ) from exc
            if not isinstance(obj, Mapping):
                raise AuditHoldoutError(
                    f"Manifest {path} line {line_no} is not a JSON object."
                )
            rows.append(obj)
            scanned_roots.append((f"line {line_no}", obj))

    # in_audit outside the row list (see the docstring). The scan is iterative
    # (no recursion limit on a deeply nested file) and skips only the row
    # objects themselves, whose flags are read below; their nested values are
    # scanned. A location names key names only when they cannot hold an id.
    row_object_ids = {id(row) for row in rows}
    outside_audit: set[str] = set()
    pending: list[tuple[str, Any]] = list(scanned_roots)
    while pending:
        location, node = pending.pop()
        if isinstance(node, Mapping):
            if id(node) not in row_object_ids and "in_audit" in node:
                flag = node["in_audit"]
                if type(flag) is not bool:
                    raise AuditHoldoutError(
                        f"Manifest {path} object at {location}, outside the row list, has "
                        f"non-bool in_audit (type={type(flag).__name__}); an in_audit flag "
                        "anywhere in a manifest must be a boolean."
                    )
                if flag:
                    try:
                        outside_audit.add(validate_nct_id(node.get("nct_id")))
                    except InvalidNctIdError as exc:
                        raise AuditHoldoutError(
                            f"Manifest {path} object at {location}, outside the row list, is "
                            f"marked in_audit=True but names no valid nct_id ({exc}); that "
                            "holdout cannot be added to the holdout set, so the manifest is "
                            "refused."
                        ) from None
            for key, value in node.items():
                printable = isinstance(key, str) and _PRINTABLE_MANIFEST_KEY_RE.fullmatch(key)
                pending.append((f"{location}.{key if printable else '<key>'}", value))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                pending.append((f"{location}[{index}]", item))

    if not rows:
        raise AuditHoldoutError(
            f"Manifest {path} contains zero trial rows; cannot authorize indexing."
        )

    in_scope: list[dict[str, Any]] = []
    audit_ids: set[str] = set()
    seen_flags: dict[str, bool] = {}
    has_audit_rows = False

    # Messages below name the row's position, never its id: a row whose flag is
    # missing, malformed or contradicted may be a holdout trial's.
    for index, row in enumerate(rows):
        try:
            nct_id = validate_nct_id(row.get("nct_id"))
        except InvalidNctIdError as exc:
            raise InvalidNctIdError(f"Manifest {path} row {index}: {exc}") from None
        if "in_audit" not in row or type(row.get("in_audit")) is not bool:
            raise AuditHoldoutError(
                f"Manifest {path} row {index} has non-bool or missing in_audit "
                f"(type={type(row.get('in_audit')).__name__}); explicit boolean required."
            )
        flag: bool = row["in_audit"]
        if nct_id in seen_flags:
            if seen_flags[nct_id] != flag:
                raise AuditHoldoutError(
                    f"Manifest {path} row {index} repeats an earlier trial with a "
                    "conflicting in_audit value."
                )
            continue
        seen_flags[nct_id] = flag
        if flag is True:
            has_audit_rows = True
            audit_ids.add(nct_id)
        else:
            in_scope.append(dict(row))

    if outside_audit:
        # A holdout marked outside the row list joins the holdout set; one
        # the row list marks in_audit=False is a conflict, refused like two
        # rows that disagree.
        conflicting = outside_audit & {nct for nct, flag in seen_flags.items() if flag is False}
        if conflicting:
            raise AuditHoldoutError(
                f"Manifest {path} marks {len(conflicting)} trial(s) in_audit=False in its row "
                "list and in_audit=True elsewhere in the file; conflicting flags are refused."
            )
        has_audit_rows = True
        audit_ids |= outside_audit

    if caller_audit is not None:
        if has_audit_rows and not caller_audit:
            raise AuditHoldoutError(
                f"Manifest {path} contains in_audit=True rows, so audit_nct_ids must not be empty."
            )
        missing_from_caller = audit_ids - caller_audit
        if missing_from_caller:
            raise AuditHoldoutError(
                f"Caller audit_nct_ids omits {len(missing_from_caller)} of the manifest's "
                f"{len(audit_ids)} in_audit=True trial(s)."
            )
        audit_ids |= set(caller_audit)

    if has_audit_rows and not audit_ids:
        raise AuditHoldoutError(
            f"Manifest {path} contains in_audit=True rows, but resolved audit_nct_ids is empty."
        )

    return in_scope, frozenset(audit_ids)


def _extract_study_dates(study: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return the payload's ``(primary completion date, results first posted date)``, each a CT.gov date or None.

    Read strictly: an absent ``statusModule``, date struct or ``date`` (or a
    null one) is recorded as absent (None); a ``statusModule`` or struct that
    is not an object, or a ``date`` that is not a CT.gov date string
    (``validate_ctgov_date_field``), raises ``DocumentIndexerError``. The
    dates used to be ``str()``-ed, so an object or list became its Python
    text (``"{'nct_id': ...}"``) in the profile's upload-timing facts. No
    message prints a value.
    """
    protocol = study.get("protocolSection")
    status_mod = protocol.get("statusModule") if isinstance(protocol, Mapping) else None
    if status_mod is None:
        return None, None
    if not isinstance(status_mod, Mapping):
        raise DocumentIndexerError(
            f"Study payload statusModule is {type(status_mod).__name__}, not an object; the "
            "payload is refused rather than read as carrying no dates."
        )
    dates: list[str | None] = []
    for struct_key in ("primaryCompletionDateStruct", "resultsFirstPostDateStruct"):
        struct = status_mod.get(struct_key)
        if struct is None:
            dates.append(None)
            continue
        if not isinstance(struct, Mapping):
            raise DocumentIndexerError(
                f"Study payload statusModule.{struct_key} is {type(struct).__name__}, not an "
                "object; the payload is refused."
            )
        dates.append(
            validate_ctgov_date_field(
                struct.get("date"), origin=f"Study payload statusModule.{struct_key}.date"
            )
        )
    return dates[0], dates[1]


def index_trial_from_study(
    study: Mapping[str, Any],
    *,
    cache_store: DocumentCacheStore,
    provider: DocumentPageExtractor | None,
    session: Any | None = None,
    local_pdf_dir: Path | None = None,
    rate_limiter: GlobalRateLimiter | None = None,
    offline: bool = False,
    force_download: bool = False,
    reparse_cached_pages: bool = False,
    min_selected_pages: int = 10,
    max_selected_pages: int = 25,
    extracted_at: str | None = None,
    pipeline_commit: str | None = None,
    indexer_rules_version: str = INDEXER_RULES_VERSION,
    nct_id: str | None = None,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
    in_scope_nct_ids: AbstractSet[str] | None = None,
) -> TrialDocumentProfile:
    """Index all in-scope largeDocs for a single CT.gov study JSON behind the in_audit firewall.

    Page-selection bounds are checked before anything else, so bad bounds stop
    the call before any cache lookup, download or write. A local PDF is taken
    only through ``resolve_local_trial_pdf``, which refuses (``AuditHoldoutError``)
    a file or trial folder linking outside ``<local_pdf_dir>/<nct_id>/`` before
    it is read, and only when its byte count equals the payload's ``size``
    (``CacheIntegrityError`` otherwise, or when the payload gives no size to
    check it against): a copied file (another trial's document, an old
    version) would otherwise be indexed, profiled and recorded in
    url_manifest.json as the verified content of this trial's CT.gov URL.

    The firewall is ``cache_store``'s merged with this call's arguments
    (``DocumentCacheStore.merge_firewall_sets``), for the trial and for the
    tag of every cached page map read for its documents, so a store built
    with a holdout set refuses that trial even when this call's own sets are
    left at their defaults, and a holdout set passed here refuses a cached
    map tagged with it. ``url_manifest.json`` is checked entry by entry when
    the store opens it, against the store's own sets only; the URLs this
    call looks up and records are the trial's own, built from the id that
    passed the merged check. The payload's document module and dates are read
    strictly (``extract_in_scope_large_docs``, ``_extract_study_dates``)
    before any cache lookup or request.

    All of the trial's cache writes are staged (``staged_writes``): a refusal
    for one of its documents leaves nothing of the trial in the cache. Inside
    a caller's staged block (``run_cli`` stages a whole live run) the trial's
    writes join that block.
    """
    validate_page_selection_bounds(min_selected_pages, max_selected_pages)
    audit_set, effective_allowed = cache_store.merge_firewall_sets(
        audit_nct_ids, _merge_allowed_sets(allowed_nct_ids, in_scope_nct_ids)
    )

    # Hard firewall BEFORE inspecting documents, cache, local PDFs, or network:
    # validates (a) strict ^NCT[0-9]{8}$ on study payload nctId, (b) exact equality
    # with requested nct_id when supplied, (c) audit_nct_ids / in_audit, and
    # (d) allowed_nct_ids / in_scope_nct_ids allowlist membership.
    study_nct_id = validate_study_payload_nct_id(
        study,
        expected_nct_id=nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_set,
        allowed_nct_ids=effective_allowed,
    )

    in_scope_docs, excluded_icf_count = extract_in_scope_large_docs(
        study,
        expected_nct_id=study_nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_set,
        allowed_nct_ids=effective_allowed,
    )
    primary_completion_date, results_first_post_date = _extract_study_dates(study)
    stamp = extracted_at or utc_now_iso()

    with cache_store.staged_writes():
        return _index_study_documents(
            in_scope_docs,
            excluded_icf_count=excluded_icf_count,
            study_nct_id=study_nct_id,
            primary_completion_date=primary_completion_date,
            results_first_post_date=results_first_post_date,
            stamp=stamp,
            cache_store=cache_store,
            provider=provider,
            session=session,
            local_pdf_dir=local_pdf_dir,
            rate_limiter=rate_limiter,
            offline=offline,
            force_download=force_download,
            reparse_cached_pages=reparse_cached_pages,
            min_selected_pages=min_selected_pages,
            max_selected_pages=max_selected_pages,
            pipeline_commit=pipeline_commit,
            indexer_rules_version=indexer_rules_version,
            in_audit=in_audit,
            audit_nct_ids=audit_set,
            allowed_nct_ids=effective_allowed,
        )


def _index_study_documents(
    in_scope_docs: Sequence[Mapping[str, Any]],
    *,
    excluded_icf_count: int,
    study_nct_id: str,
    primary_completion_date: str | None,
    results_first_post_date: str | None,
    stamp: str,
    cache_store: DocumentCacheStore,
    provider: DocumentPageExtractor | None,
    session: Any | None,
    local_pdf_dir: Path | None,
    rate_limiter: GlobalRateLimiter | None,
    offline: bool,
    force_download: bool,
    reparse_cached_pages: bool,
    min_selected_pages: int,
    max_selected_pages: int,
    pipeline_commit: str | None,
    indexer_rules_version: str,
    in_audit: Any,
    audit_nct_ids: frozenset[str],
    allowed_nct_ids: frozenset[str] | None,
) -> TrialDocumentProfile:
    """Resolve, index and profile one trial's in-scope documents (the body of ``index_trial_from_study``).

    Called only by ``index_trial_from_study`` after the payload passed the
    firewall and was read strictly, inside its staged block; ``audit_nct_ids``
    and ``allowed_nct_ids`` are the merged firewall sets. Each document is
    resolved from the url_manifest fast path, a local PDF (size-checked
    against the payload), or a download (size-checked against the payload
    when it gives a size; a mismatch is ``download_size_mismatch``), then
    indexed through the cache.
    """
    effective_allowed = allowed_nct_ids
    trial_records: list[TrialDocumentRecord] = []

    for meta in in_scope_docs:
        filename: str = meta["filename"]
        doc_type = meta["doc_type"]
        url: str = meta["url"]
        api_size: int | None = meta["api_size"]
        api_doc_date: str | None = meta["api_doc_date"]
        upload_date: str | None = meta["upload_date"]

        resolved_sha: str | None = None
        resolved_bytes: int | None = None
        resolved_pdf_path: Path | None = None
        http_status: int | None = 200
        download_error: str | None = None

        # 1. URL manifest fast path (0 HTTP calls when (api_size, api_upload_date) match)
        if not force_download:
            manifest_sha = cache_store.can_skip_download(
                url, api_size=api_size, api_upload_date=upload_date
            )
            if manifest_sha is not None:
                manifest_rec = cache_store.lookup_url_manifest(url)
                has_entry = (
                    cache_store.get_entry(manifest_sha, indexer_rules_version)
                    is not None
                )
                # The map's tag is checked against the merged sets, as
                # get_or_index_document checks it below.
                has_pages = (
                    cache_store.load_cached_pages(
                        manifest_sha,
                        audit_nct_ids=audit_nct_ids,
                        allowed_nct_ids=effective_allowed,
                    )
                    is not None
                )
                blob_path = cache_store.get_blob_path(manifest_sha)
                if has_entry or has_pages or blob_path is not None:
                    resolved_sha = manifest_sha
                    resolved_bytes = (
                        int(manifest_rec["byte_count"])
                        if manifest_rec and type(manifest_rec.get("byte_count")) is int
                        else api_size
                    )
                    resolved_pdf_path = blob_path

        # 2. Local PDF directory resolution without duplicating multi-MB files.
        # A file or trial folder linking outside <local_pdf_dir>/<nct_id>/ (into
        # a holdout trial's folder, say) is refused before it is opened. A
        # local file is the posted document only if its size is the payload's
        # (see the docstring of index_trial_from_study); the census ingest
        # refuses a byte-count mismatch the same way. The local file's digest
        # is not printed.
        if resolved_sha is None and local_pdf_dir is not None:
            candidate = resolve_local_trial_pdf(local_pdf_dir, study_nct_id, filename)
            if candidate is not None:
                with candidate.open("rb") as handle:
                    prefix = handle.read(1024)
                if not prefix or not has_pdf_magic(prefix):
                    download_error = "local_pdf_invalid_header_or_empty"
                    http_status = None
                else:
                    local_sha, local_bytes = sha256_file(candidate)
                    if api_size is None or local_bytes != api_size:
                        expected = "gives no size" if api_size is None else f"says {api_size} bytes"
                        raise CacheIntegrityError(
                            f"Local PDF {filename} of an in-scope trial under --local-pdf-dir has "
                            f"{local_bytes} bytes but the CT.gov payload {expected}; it is not "
                            "taken as the posted document (a copied or outdated file would be "
                            "recorded as this trial's). Remove or replace the file."
                        )
                    resolved_sha, resolved_bytes = local_sha, local_bytes
                    resolved_pdf_path = candidate

        # 3. Live HTTP download (unless offline or already resolved)
        if resolved_sha is None and download_error is None:
            if offline:
                http_status = None
                download_error = "offline_cache_miss"
            else:
                if session is None:
                    session = requests.Session()
                    session.headers.update({"User-Agent": USER_AGENT})
                dl = download_pdf_atomic(
                    url,
                    cache_store.blobs_dir,
                    session=session,
                    nct_id=study_nct_id,
                    filename=filename,
                    rate_limiter=rate_limiter,
                    in_audit=in_audit,
                    audit_nct_ids=audit_nct_ids,
                    allowed_nct_ids=effective_allowed,
                )
                http_status = dl.http_status
                if dl.blob_created and dl.local_path is not None:
                    # A new blob is removed again if this staged block fails.
                    cache_store.note_blob_created(dl.local_path)
                if (
                    dl.ok
                    and dl.byte_count is not None
                    and api_size is not None
                    and dl.byte_count != api_size
                ):
                    # A 200 %PDF- response whose length is not the payload's size
                    # is not the posted document (the local-PDF branch above
                    # refuses the same mismatch). It is a failed download of this
                    # document only, never indexed or recorded in url_manifest
                    # (can_skip_download would serve that digest on every later
                    # run). A blob this download created is removed, as nothing
                    # refers to it; one that existed before is kept. With no
                    # payload size (api_size None) there is nothing to compare
                    # against, and the download is taken below as before.
                    download_error = "download_size_mismatch"
                    if dl.blob_created and dl.local_path is not None:
                        dl.local_path.unlink(missing_ok=True)
                        try:
                            dl.local_path.parent.rmdir()  # the shard folder, if now empty
                        except OSError:
                            pass
                elif dl.ok and dl.sha256 is not None and dl.byte_count is not None:
                    resolved_sha = dl.sha256
                    resolved_bytes = dl.byte_count
                    resolved_pdf_path = dl.local_path
                    cache_store.record_url_manifest(
                        url,
                        sha256=resolved_sha,
                        byte_count=resolved_bytes,
                        api_size=api_size,
                        api_upload_date=upload_date,
                        verified_at=stamp,
                        pipeline_commit=pipeline_commit,
                    )
                else:
                    download_error = dl.error or "download_failed"

        if resolved_sha is None or resolved_bytes is None or download_error is not None:
            trial_records.append(
                build_trial_document_record(
                    nct_id=study_nct_id,
                    filename=filename,
                    doc_type=doc_type,
                    url=url,
                    http_status=http_status,
                    entry=None,
                    download_error=download_error or "download_failed",
                    api_doc_date=api_doc_date,
                    upload_date=upload_date,
                    primary_completion_date=primary_completion_date,
                    results_first_post_date=results_first_post_date,
                    retrieved_at=stamp,
                    cache_hit=False,
                    in_audit=in_audit,
                    audit_nct_ids=audit_nct_ids,
                    allowed_nct_ids=effective_allowed,
                )
            )
            continue

        # 4. Content-addressed cache lookup or page extraction + indexing
        try:
            entry, cache_hit = cache_store.get_or_index_document(
                sha256=resolved_sha,
                byte_count=resolved_bytes,
                provider=provider,
                pdf_path=resolved_pdf_path,
                nct_id=study_nct_id,
                filename=filename,
                doc_type=doc_type,
                extracted_at=stamp,
                pipeline_commit=pipeline_commit,
                indexer_rules_version=indexer_rules_version,
                min_selected_pages=min_selected_pages,
                max_selected_pages=max_selected_pages,
                reparse_cached_pages=reparse_cached_pages,
                in_audit=in_audit,
                audit_nct_ids=audit_nct_ids,
                allowed_nct_ids=effective_allowed,
            )
        except PageExtractionError as exc:
            # The bytes are in hand (resolved_sha / resolved_bytes are known);
            # only page extraction failed (on Linux with no page map, every
            # uncached document lands here). That is extraction_failed, kept
            # apart from download_failed, with the digest and size recorded and
            # no page count.
            trial_records.append(
                build_trial_document_record(
                    nct_id=study_nct_id,
                    filename=filename,
                    doc_type=doc_type,
                    url=url,
                    http_status=http_status,
                    entry=None,
                    extraction_error=f"page_extraction_error: {exc}",
                    sha256=resolved_sha,
                    byte_count=resolved_bytes,
                    api_doc_date=api_doc_date,
                    upload_date=upload_date,
                    primary_completion_date=primary_completion_date,
                    results_first_post_date=results_first_post_date,
                    retrieved_at=stamp,
                    cache_hit=False,
                    in_audit=in_audit,
                    audit_nct_ids=audit_nct_ids,
                    allowed_nct_ids=effective_allowed,
                )
            )
            continue

        # The url_manifest record is made to describe the bytes just verified and
        # indexed (a local-PDF hit too). A record that does not (none yet, one an
        # earlier version wrote at the wrong size, one for an older upload date)
        # is replaced, or the URL keeps no record of the digest it is profiled
        # with (can_skip_download trusts neither of those two), and an offline
        # run with no local folder finds no digest for it. A record that
        # already does (the fast path's, or the one the download branch just
        # wrote) is left as it is, so verified_at is not rewritten every run.
        # Where api_size is non-null the bytes here are of that size (the local
        # and download branches checked it, can_skip_download required it), so
        # record_url_manifest's size check cannot refuse this record.
        existing_rec = cache_store.lookup_url_manifest(url)
        if existing_rec is None or (
            existing_rec.get("sha256"),
            existing_rec.get("byte_count"),
            existing_rec.get("api_size"),
            existing_rec.get("api_upload_date"),
        ) != (resolved_sha, resolved_bytes, api_size, upload_date):
            cache_store.record_url_manifest(
                url,
                sha256=resolved_sha,
                byte_count=resolved_bytes,
                api_size=api_size,
                api_upload_date=upload_date,
                verified_at=stamp,
                pipeline_commit=pipeline_commit,
            )

        trial_records.append(
            build_trial_document_record(
                nct_id=study_nct_id,
                filename=filename,
                doc_type=doc_type,
                url=url,
                http_status=http_status,
                entry=entry,
                api_doc_date=api_doc_date,
                upload_date=upload_date,
                primary_completion_date=primary_completion_date,
                results_first_post_date=results_first_post_date,
                retrieved_at=stamp,
                cache_hit=cache_hit,
                in_audit=in_audit,
                audit_nct_ids=audit_nct_ids,
                allowed_nct_ids=effective_allowed,
            )
        )

    return build_trial_document_profile(
        study_nct_id,
        documents=trial_records,
        excluded_icf_count=excluded_icf_count,
        extracted_at=stamp,
        pipeline_commit=pipeline_commit,
        indexer_rules_version=indexer_rules_version,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=effective_allowed,
    )


def fetch_and_index_trial(
    nct_id: str,
    *,
    api_client: Any,
    cache_store: DocumentCacheStore,
    provider: DocumentPageExtractor | None,
    session: Any | None = None,
    local_pdf_dir: Path | None = None,
    rate_limiter: GlobalRateLimiter | None = None,
    offline: bool = False,
    force_download: bool = False,
    reparse_cached_pages: bool = False,
    min_selected_pages: int = 10,
    max_selected_pages: int = 25,
    extracted_at: str | None = None,
    pipeline_commit: str | None = None,
    indexer_rules_version: str = INDEXER_RULES_VERSION,
    in_audit: Any = False,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
    in_scope_nct_ids: AbstractSet[str] | None = None,
) -> TrialDocumentProfile:
    """Fetch a single study from CTGovAPIClient and index its documents behind the in_audit firewall.

    The firewall is ``cache_store``'s holdout set and allowlist merged with
    this call's (``DocumentCacheStore.merge_firewall_sets``), checked before
    the CT.gov record is requested: a store built with a holdout set refuses
    that trial even when this call's own sets are left at their defaults.
    """
    audit_set, effective_allowed = cache_store.merge_firewall_sets(
        audit_nct_ids, _merge_allowed_sets(allowed_nct_ids, in_scope_nct_ids)
    )
    # Firewall runs BEFORE calling api_client.get_study()!
    nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_set,
        allowed_nct_ids=effective_allowed,
    )

    if offline:
        raise DocumentIndexerError(
            f"Cannot fetch CT.gov study metadata for {nct} in --offline mode; "
            "use --ingest-census-jsonl or pass pre-fetched study JSON."
        )
    if rate_limiter is not None:
        rate_limiter.wait()
    study = api_client.get_study(nct)
    return index_trial_from_study(
        study,
        cache_store=cache_store,
        provider=provider,
        session=session,
        local_pdf_dir=local_pdf_dir,
        rate_limiter=rate_limiter,
        offline=offline,
        force_download=force_download,
        reparse_cached_pages=reparse_cached_pages,
        min_selected_pages=min_selected_pages,
        max_selected_pages=max_selected_pages,
        extracted_at=extracted_at,
        pipeline_commit=pipeline_commit,
        indexer_rules_version=indexer_rules_version,
        nct_id=nct,
        in_audit=in_audit,
        audit_nct_ids=audit_set,
        allowed_nct_ids=effective_allowed,
    )


def write_profiles_jsonl(path: Path, profiles: Sequence[TrialDocumentProfile]) -> None:
    """Atomically write trial document profiles sorted deterministically by nct_id."""
    ordered = sorted(profiles, key=lambda p: p["nct_id"])
    lines = [
        json.dumps(profile, separators=(",", ":"), ensure_ascii=True)
        for profile in ordered
    ]
    payload = ("\n".join(lines) + "\n") if lines else ""
    atomic_write_text(path, payload)


def build_default_provider(
    *,
    cache_store: DocumentCacheStore,
    page_map_paths: Sequence[Path] = (),
    audit_nct_ids: AbstractSet[str] = frozenset(),
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> ChainedPageTextProvider:
    """Build standard ChainedPageTextProvider (cache -> JSONL page maps -> macOS PDFKit)."""
    providers: list[DocumentPageExtractor] = []
    if page_map_paths or cache_store.pages_dir.exists():
        providers.append(
            JsonlPageMapProvider(
                page_map_paths,
                cache_pages_dir=cache_store.pages_dir,
                audit_nct_ids=audit_nct_ids,
                allowed_nct_ids=allowed_nct_ids,
            )
        )
    if sys.platform == "darwin":
        providers.append(MacOSPDFKitProvider(compile_dir=cache_store.cache_dir))
    return ChainedPageTextProvider(providers, cache_store=cache_store)


_REPEATED_OPTIONS_ATTR = "_repeated_single_value_options"
_SEEN_OPTIONS_ATTR = "_seen_single_value_options"


class _StoreOnceAction(argparse.Action):
    """Store a single-value option and record, rather than silently resolve, a repeat.

    argparse's default ``store`` keeps the last of a repeated option, so
    ``--manifest canon.jsonl --manifest other.jsonl`` read only other.jsonl and
    dropped the holdout set of canon.jsonl without a word, and a repeated
    ``--ingest-census-jsonl`` ingested only the last file. Every option that
    takes one value uses this action instead: the option strings given more
    than once are collected on the namespace, and ``run_cli`` refuses the run
    (exit 2, before anything is read or written). Multi-value options
    (``--nct-id``, ``--page-map-jsonl``) keep ``append``.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        seen: set[str] = getattr(namespace, _SEEN_OPTIONS_ATTR, None) or set()
        if self.dest in seen:
            repeated: list[str] = getattr(namespace, _REPEATED_OPTIONS_ATTR, None) or []
            name = self.option_strings[0]
            if name not in repeated:
                repeated.append(name)
            setattr(namespace, _REPEATED_OPTIONS_ATTR, repeated)
        seen.add(self.dest)
        setattr(namespace, _SEEN_OPTIONS_ATTR, seen)
        setattr(namespace, self.dest, values)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser; every single-value option refuses a repeat (``_StoreOnceAction``)."""
    epilog = (
        "Dependencies & platform notes:\n"
        "  - Imports requests and tqdm (requirements.txt) plus src.api_client.CTGovAPIClient.\n"
        "  - MacOSPDFKitProvider uses macOS clang + Foundation/PDFKit (or ctypes.CDLL to\n"
        "    libobjc.A.dylib + PDFKit.framework); on non-macOS platforms without a\n"
        "    pre-extracted --page-map-jsonl or cached page map, fails loudly with\n"
        "    PageExtractionError."
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--nct-id",
        action="append",
        nargs="+",
        default=[],
        help=(
            "Explicit NCT ID(s) to index (requires --manifest; raises AuditHoldoutError "
            "exit 2 if in_audit=True or unlisted in manifest; cannot be combined with "
            "--ingest-census-jsonl; a repeated id is fetched once)."
        ),
    )
    parser.add_argument(
        "--manifest",
        action=_StoreOnceAction,
        type=Path,
        default=None,
        help=(
            "Path to trial manifest JSON/JSONL (e.g. data/outcomes/index.json or "
            "data/outcomes_bundles/index.json) carrying strict bool in_audit "
            "(required for --nct-id and --ingest-census-jsonl)."
        ),
    )
    parser.add_argument(
        "--ingest-census-jsonl",
        action=_StoreOnceAction,
        type=Path,
        default=None,
        help=(
            "Offline-ingest legacy rule-census documents.jsonl into the content-addressed "
            "cache (requires --manifest)."
        ),
    )
    parser.add_argument(
        "--local-pdf-dir",
        action=_StoreOnceAction,
        type=Path,
        default=None,
        help="Directory holding pre-downloaded {nct_id}/{filename} PDFs.",
    )
    parser.add_argument(
        "--page-map-jsonl",
        action="append",
        type=Path,
        default=[],
        help="Pre-extracted page-text JSONL(.gz) file(s) for offline/Linux indexing.",
    )
    parser.add_argument(
        "--cache-dir",
        action=_StoreOnceAction,
        type=Path,
        default=Path("data/document_cache"),
        help="Root directory for content-addressed document cache.",
    )
    parser.add_argument(
        "--index-out",
        action=_StoreOnceAction,
        type=Path,
        default=Path("data/document_index_by_sha256.jsonl"),
        help="Output path for sha256-keyed DocumentIndexEntry JSONL.",
    )
    parser.add_argument(
        "--profiles-out",
        action=_StoreOnceAction,
        type=Path,
        default=Path("data/trial_document_profiles.jsonl"),
        help="Output path for TrialDocumentProfile JSONL.",
    )
    parser.add_argument("--min-selected-pages", action=_StoreOnceAction, type=int, default=10)
    parser.add_argument("--max-selected-pages", action=_StoreOnceAction, type=int, default=25)
    parser.add_argument(
        "--rate-limit-sec", action=_StoreOnceAction, type=float, default=DEFAULT_RATE_LIMIT_SEC
    )
    parser.add_argument("--limit", action=_StoreOnceAction, type=int, default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--reparse-cached-pages", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def _report_unindexed_documents(profiles: Sequence[TrialDocumentProfile]) -> None:
    """Print to stderr how many documents ended download_failed or extraction_failed.

    These are per-document states recorded in the profiles, and the run still
    exits 0 for them, as it does for a trial whose posted document is a scan.
    The count goes to the run log so that, for example, a Linux run with no page
    map (every uncached document extraction_failed) is visible without opening
    the output. Counts only: no trial ids.
    """
    counts = {"download_failed": 0, "extraction_failed": 0}
    total = 0
    for profile in profiles:
        for doc in profile["documents"]:
            total += 1
            status = doc["readability_status"]
            if status in counts:
                counts[status] += 1
    not_indexed = counts["download_failed"] + counts["extraction_failed"]
    if not_indexed:
        print(
            f"WARNING: {not_indexed} of {total} in-scope document(s) were not indexed "
            f"(download_failed={counts['download_failed']}, "
            f"extraction_failed={counts['extraction_failed']}); each record says why.",
            file=sys.stderr,
        )


def _refuse_nothing_to_index(message: str) -> int:
    """Report a run that has no trial to index, touch no output, and return exit status 2.

    A run with nothing to index used to write an empty profiles file over the
    previous one and exit 0, the failure the no-target-arguments guard (D1)
    exists to prevent. Every zero-trial path now ends here instead.
    """
    print(f"NOTHING TO INDEX: {message}; no output or cache path was written.", file=sys.stderr)
    return 2


def _refuse_usage(parser: argparse.ArgumentParser, message: str) -> int:
    """Print usage and ``message`` to stderr, touch nothing, and return exit status 2."""
    parser.print_usage(sys.stderr)
    print(
        f"index_trial_documents.py: error: {message}; nothing was fetched, read or written.",
        file=sys.stderr,
    )
    return 2


def _names_same_file(first: Path, second: Path) -> bool:
    """True when two paths name one file: equal once links and ``..`` are resolved, or one inode.

    Paths are compared after ``Path.resolve`` (so ``sub/../manifest.jsonl``
    and a symlink to the manifest both match it) and, when both exist, with
    ``os.path.samefile`` (which also catches a hard link).
    """
    try:
        if Path(first).resolve() == Path(second).resolve():
            return True
    except (OSError, RuntimeError):
        pass
    try:
        return Path(first).exists() and Path(second).exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _is_within(path: Path, folder: Path) -> bool:
    """True when ``path``, resolved, lies inside ``folder``, resolved."""
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except (OSError, RuntimeError):
        return False


def _output_path_conflicts(args: argparse.Namespace) -> list[str]:
    """Name every way ``--profiles-out`` / ``--index-out`` would overwrite an input, the cache, or each other.

    Nothing compared the output paths with the inputs: ``--profiles-out``
    equal to ``--manifest`` replaced the manifest (and its in_audit=True rows,
    the holdout set) with profiles and exited 0; equal to the census or a page
    map it destroyed that input; ``--index-out`` equal to ``--profiles-out``
    left an index the next run could not open, and equal to the manifest it
    was parsed as an index. Checked here, before anything is fetched, read or
    written: each output against every input file, the cache's own files and
    its pages/ and blobs/ folders, an existing directory, and the other
    output. Problems name the options, never the paths (which may hold ids).

    Two more inputs are folders. An output inside ``--local-pdf-dir`` is
    refused (``--profiles-out`` naming a local PDF the run reads replaced that
    PDF with the profiles). An output that is ``--cache-dir`` itself, or a
    folder holding it, is refused even before the cache folder exists (it
    passed when the cache was not yet a directory; the run then created the
    cache there and the profile write failed after the cache was written).
    """
    inputs: list[tuple[str, Path]] = []
    if args.manifest is not None:
        inputs.append(("--manifest", args.manifest))
    if args.ingest_census_jsonl is not None:
        inputs.append(("--ingest-census-jsonl", args.ingest_census_jsonl))
    for position, page_map in enumerate(args.page_map_jsonl or [], start=1):
        inputs.append((f"--page-map-jsonl (#{position})", page_map))
    cache_dir = Path(args.cache_dir)
    inputs.append(("the cache's url_manifest.json", cache_dir / "url_manifest.json"))
    inputs.append(("the cache's rate-limit lock file", cache_dir / ".ctgov_rate_limit.lock"))
    folders: list[tuple[str, Path]] = [
        ("the cache's pages/ folder", cache_dir / "pages"),
        ("the cache's blobs/ folder", cache_dir / "blobs"),
    ]

    outputs: list[tuple[str, Path]] = [
        ("--profiles-out", args.profiles_out),
        ("--index-out", args.index_out),
    ]
    problems: list[str] = []
    for out_name, out_path in outputs:
        if Path(out_path).is_dir():
            problems.append(f"{out_name} is an existing directory")
        for in_name, in_path in inputs:
            if _names_same_file(out_path, in_path):
                problems.append(f"{out_name} names the same file as {in_name}")
        for folder_name, folder in folders:
            if _is_within(out_path, folder):
                problems.append(f"{out_name} lies inside {folder_name}")
        if args.local_pdf_dir is not None and _is_within(out_path, args.local_pdf_dir):
            problems.append(f"{out_name} lies inside --local-pdf-dir")
        if _is_within(cache_dir, out_path):
            problems.append(f"{out_name} is --cache-dir or a folder holding it")
    if _names_same_file(args.profiles_out, args.index_out):
        problems.append("--profiles-out and --index-out name the same file")
    return problems


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    api_client: Any | None = None,
    session: Any | None = None,
    provider: DocumentPageExtractor | None = None,
) -> int:
    """Execute the document pre-indexer CLI.

    Exit status: 0 on success; 2 on usage errors, ``AuditHoldoutError``, or a
    run with no trial to index (no output is written); 1 on other indexer
    errors, and on a live run in which one or more trials' CT.gov records could
    not be fetched (the other trials' profiles are still written, and the
    failed trials are named on stderr).
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # A single-value option given twice is refused before anything is read
    # (see _StoreOnceAction): the earlier value would otherwise be dropped
    # without a word, and for --manifest that drops a holdout set.
    repeated_options: list[str] = getattr(args, _REPEATED_OPTIONS_ATTR, None) or []
    if repeated_options:
        parser.print_usage(sys.stderr)
        print(
            "index_trial_documents.py: error: "
            f"{', '.join(repeated_options)} may be given at most once; a repeated value "
            "would silently replace the earlier one. Nothing was read or written.",
            file=sys.stderr,
        )
        return 2

    has_nct_arg = any(group for group in (args.nct_id or []))
    if has_nct_arg and args.ingest_census_jsonl is not None:
        # --nct-id selects trials to fetch live; --ingest-census-jsonl takes its
        # trials from the census file. The ingest used to run every census row
        # and ignore --nct-id after firewall-checking it, so a run that named
        # one trial indexed, cached and profiled all of them. One run takes one
        # source of trials; filter the census file to restrict an ingest.
        parser.print_usage(sys.stderr)
        print(
            "index_trial_documents.py: error: --nct-id cannot be combined with "
            "--ingest-census-jsonl (the ingest takes its trials from the census file); "
            "nothing was read or written.",
            file=sys.stderr,
        )
        return 2
    if not has_nct_arg and args.manifest is None and args.ingest_census_jsonl is None:
        parser.print_usage(sys.stderr)
        print(
            "index_trial_documents.py: error: at least one target argument "
            "(--manifest, --nct-id, or --ingest-census-jsonl) is required.",
            file=sys.stderr,
        )
        return 2
    if args.limit is not None and args.limit < 1:
        # --limit 0 selected nothing and still truncated the profiles file; a
        # negative value silently dropped trials from the end of the list.
        parser.print_usage(sys.stderr)
        print(
            "index_trial_documents.py: error: --limit must be a positive integer.",
            file=sys.stderr,
        )
        return 2
    # Page bounds and the rate limit are checked here, like --limit. The
    # bounds used to be checked only inside index_document_pages, after the
    # CT.gov record was fetched and the PDF's blob, url_manifest.json entry
    # and page map written (exit 1); -1 and nan for the rate limit meant no
    # rate limit, with no word.
    try:
        validate_page_selection_bounds(args.min_selected_pages, args.max_selected_pages)
        validate_rate_limit_sec(args.rate_limit_sec)
    except DocumentIndexerError as exc:
        return _refuse_usage(parser, str(exc).rstrip("."))
    # A mistyped --local-pdf-dir would otherwise find no local PDF, silently.
    if args.local_pdf_dir is not None and not Path(args.local_pdf_dir).is_dir():
        return _refuse_usage(parser, "--local-pdf-dir is not an existing directory")
    output_conflicts = _output_path_conflicts(args)
    if output_conflicts:
        return _refuse_usage(
            parser,
            "an output path would overwrite an input, the cache or the other output ("
            + "; ".join(output_conflicts)
            + ")",
        )

    try:
        explicit_nct_ids = [
            validate_nct_id(item)
            for group in (args.nct_id or [])
            for item in group
        ]
        if (explicit_nct_ids or args.ingest_census_jsonl is not None) and args.manifest is None:
            raise AuditHoldoutError(
                "--manifest is required whenever --nct-id or --ingest-census-jsonl is supplied; "
                "unverified trial IDs are blocked by the audit holdout firewall."
            )

        manifest_rows: list[dict[str, Any]] = []
        audit_nct_ids: frozenset[str] = frozenset()
        if args.manifest is not None:
            manifest_rows, audit_nct_ids = load_manifest_rows(args.manifest)

        in_scope_nct_ids: frozenset[str] = (
            frozenset(validate_nct_id(r["nct_id"]) for r in manifest_rows)
            - audit_nct_ids
        )

        # Explicit --nct-id matching in_audit == True or unlisted in --manifest raises AuditHoldoutError (exit 2)
        for nct in explicit_nct_ids:
            enforce_not_in_audit(
                nct,
                in_audit=False,
                audit_nct_ids=audit_nct_ids,
                allowed_nct_ids=in_scope_nct_ids,
            )

        # A trial named more than once (--nct-id A --nct-id A, or --nct-id A A)
        # is fetched and profiled once: two profile lines for one trial would be
        # resolved silently by a consumer keyed by nct_id. Every copy has passed
        # the firewall above, so first-seen order is kept and the repeat count
        # (not the ids) is reported.
        unique_nct_ids = list(dict.fromkeys(explicit_nct_ids))
        if len(unique_nct_ids) != len(explicit_nct_ids):
            print(
                f"NOTE: {len(explicit_nct_ids) - len(unique_nct_ids)} repeated --nct-id "
                f"value(s) ignored; {len(unique_nct_ids)} trial(s) will each be fetched once.",
                file=sys.stderr,
            )
        explicit_nct_ids = unique_nct_ids

        # A manifest whose every row is in_audit=True leaves nothing any mode
        # may index. Refuse before a cache store or provider is built.
        if not in_scope_nct_ids:
            return _refuse_nothing_to_index(
                f"the manifest lists 0 in-scope trials ({len(audit_nct_ids)} in_audit=True)"
            )

        target_ncts: list[str] = []
        if args.ingest_census_jsonl is None:
            if explicit_nct_ids:
                target_ncts = explicit_nct_ids
            elif manifest_rows:
                target_ncts = [validate_nct_id(row["nct_id"]) for row in manifest_rows]
            if args.limit is not None:
                target_ncts = target_ncts[: args.limit]
            if not target_ncts:
                return _refuse_nothing_to_index("no trial was selected")
            if args.offline:
                raise DocumentIndexerError(
                    "Live CT.gov API fetching is disabled under --offline; "
                    "use --ingest-census-jsonl for offline indexing."
                )

        cache_store = DocumentCacheStore(
            cache_dir=args.cache_dir,
            index_path=args.index_out,
            audit_nct_ids=audit_nct_ids,
            allowed_nct_ids=in_scope_nct_ids,
        )
        active_provider = provider or build_default_provider(
            cache_store=cache_store,
            page_map_paths=args.page_map_jsonl,
            audit_nct_ids=audit_nct_ids,
            allowed_nct_ids=in_scope_nct_ids,
        )
        validator = getattr(active_provider, "validate_loaded", None)
        if callable(validator):
            validator()

        stamp = utc_now_iso()
        commit = _resolve_pipeline_commit()

        profiles: list[TrialDocumentProfile] = []

        if args.ingest_census_jsonl is not None:
            # --limit bounds the ingest's work, not only its output: the whole
            # census file is still validated, then only the first --limit
            # trials (nct_id order) are indexed, cached and profiled. Trimming
            # the profiles after the ingest left the cache and index holding
            # documents of trials with no profile in the run's output.
            _, census_profiles = ingest_legacy_census_documents(
                args.ingest_census_jsonl,
                cache_store=cache_store,
                provider=active_provider,
                local_pdf_dir=args.local_pdf_dir,
                audit_nct_ids=audit_nct_ids,
                allowed_nct_ids=in_scope_nct_ids,
                extracted_at=stamp,
                pipeline_commit=commit,
                indexer_rules_version=INDEXER_RULES_VERSION,
                min_selected_pages=args.min_selected_pages,
                max_selected_pages=args.max_selected_pages,
                reparse_cached_pages=args.reparse_cached_pages,
                max_trials=args.limit,
            )
            if not census_profiles:
                # An empty census file yields no rows, so nothing was indexed
                # or cached; the previous profiles file is left as it was.
                return _refuse_nothing_to_index(
                    "the census documents file holds 0 rows"
                )
            profiles.extend(census_profiles)
            write_profiles_jsonl(args.profiles_out, profiles)
            _report_unindexed_documents(profiles)
            return 0

        rate_limiter = GlobalRateLimiter(
            args.cache_dir / ".ctgov_rate_limit.lock",
            min_interval_sec=args.rate_limit_sec,
        )
        if api_client is None:
            from src.api_client import CTGovAPIClient  # lazy import

            api_client = CTGovAPIClient(rate_limit_delay=args.rate_limit_sec)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})

        iterator: Iterable[str] = target_ncts
        if len(target_ncts) > 1:
            try:
                from tqdm import tqdm  # lazy import

                iterator = tqdm(target_ncts, desc="Indexing trial docs", unit="trial")
            except ImportError:
                iterator = target_ncts

        # One trial's CT.gov record failing to arrive (a 404, a 5xx, a dropped
        # connection) no longer aborts the batch: the trial is named in
        # fetch_failures and skipped, the rest are indexed, and the run exits 1.
        # Only network/HTTP errors are caught (they are raised by the record
        # fetch, before that trial writes anything). A firewall or integrity
        # error still stops the whole run before anything is written: every
        # cache write of the run is staged (staged_writes) and lands only when
        # the loop finishes, so a refusal part-way through leaves no earlier
        # trial's blob, page map, url record or index line in the cache with
        # no profile. The price is that an interrupted run keeps nothing
        # either, and the run's page maps are held in memory until it ends.
        fetch_failures: list[tuple[str, str]] = []
        with cache_store.staged_writes():
            for nct in iterator:
                try:
                    profile = fetch_and_index_trial(
                        nct,
                        api_client=api_client,
                        cache_store=cache_store,
                        provider=active_provider,
                        session=session,
                        local_pdf_dir=args.local_pdf_dir,
                        rate_limiter=rate_limiter,
                        offline=args.offline,
                        force_download=args.force_download,
                        reparse_cached_pages=args.reparse_cached_pages,
                        min_selected_pages=args.min_selected_pages,
                        max_selected_pages=args.max_selected_pages,
                        extracted_at=stamp,
                        pipeline_commit=commit,
                        indexer_rules_version=INDEXER_RULES_VERSION,
                        in_audit=False,
                        audit_nct_ids=audit_nct_ids,
                        allowed_nct_ids=in_scope_nct_ids,
                    )
                except requests.RequestException as exc:
                    # nct passed enforce_not_in_audit before the fetch, so it is
                    # an in-scope trial and may be named.
                    fetch_failures.append((nct, f"{type(exc).__name__}: {exc}"))
                    continue
                profiles.append(profile)

        if profiles:
            write_profiles_jsonl(args.profiles_out, profiles)
            _report_unindexed_documents(profiles)
        if fetch_failures:
            print(
                f"FETCH ERRORS: {len(fetch_failures)} of {len(target_ncts)} trial(s) could not "
                "be fetched from CT.gov and have no profile in this run"
                + ("" if profiles else "; no profiles file was written")
                + ":",
                file=sys.stderr,
            )
            for nct, detail in fetch_failures:
                print(f"  {nct}: {detail}", file=sys.stderr)
            return 1
        return 0
    except AuditHoldoutError as exc:
        print(f"AUDIT HOLDOUT ERROR: {exc}", file=sys.stderr)
        return 2
    except DocumentIndexerError as exc:
        print(f"DOCUMENT INDEXER ERROR: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    sys.exit(main())
