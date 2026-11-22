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
  without creating, touching, or truncating output or cache paths.
- Enforces a global inter-request rate-limit lock (<cache-dir>/.ctgov_rate_limit.lock)
  defaulting to 0.55s across processes.
- Downloads PDFs atomically via .part staging and verifies the %PDF- header and
  positive byte count before committing to the content-addressed cache.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import AbstractSet, Any, Iterable, Mapping, Sequence

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
    ChainedPageTextProvider,
    DocumentCacheStore,
    DocumentIndexerError,
    DocumentPageExtractor,
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
    safe_pipeline_commit,
    sha256_file,
    utc_now_iso,
    validate_nct_id,
    validate_safe_pdf_filename,
    validate_study_payload_nct_id,
)


USER_AGENT = "CivicSample-Research/1.0 (academic research)"
DEFAULT_RATE_LIMIT_SEC = 0.55
DEFAULT_TIMEOUT_SEC = 60


def _resolve_pipeline_commit() -> str | None:
    """Resolve current pipeline commit via src.utils.pipeline_commit with graceful fallback."""
    try:
        from src.utils import pipeline_commit as _utils_pipeline_commit

        commit = _utils_pipeline_commit()
        if commit:
            return str(commit)
    except Exception:
        pass
    return safe_pipeline_commit()


class GlobalRateLimiter:
    """Cross-process inter-request rate limiter backed by a POSIX flock lock file."""

    def __init__(self, lock_path: Path, min_interval_sec: float = DEFAULT_RATE_LIMIT_SEC) -> None:
        self.lock_path = Path(lock_path)
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self._last_local_ts: float = 0.0

    def wait(self) -> None:
        """Sleep as needed so consecutive requests are separated by >= min_interval_sec."""
        if self.min_interval_sec <= 0.0:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                last_ts = self._last_local_ts
                if raw:
                    try:
                        last_ts = max(last_ts, float(raw))
                    except ValueError:
                        pass
                now = time.time()
                elapsed = now - last_ts
                if elapsed < self.min_interval_sec:
                    time.sleep(self.min_interval_sec - elapsed)
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
    ok: bool
    http_status: int | None
    sha256: str | None
    byte_count: int | None
    local_path: Path | None
    error: str | None


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
    """Download a PDF atomically via a .part file and verify %PDF- header and non-zero size."""
    validated_nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=allowed_nct_ids,
    )
    safe_filename = validate_safe_pdf_filename(filename)

    if rate_limiter is not None:
        rate_limiter.wait()

    try:
        resp = session.get(url, timeout=timeout_sec, stream=True)
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
        if blob_path.exists():
            part_path.unlink(missing_ok=True)
        else:
            os.replace(part_path, blob_path)

        return DownloadResult(
            ok=True,
            http_status=200,
            sha256=sha,
            byte_count=total_bytes,
            local_path=blob_path,
            error=None,
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
        for item in seq:
            if not isinstance(item, Mapping):
                raise AuditHoldoutError(
                    f"Manifest {path} contains non-object row {item!r} in {label!r}."
                )
            out.append(item)
        return out

    rows: list[Mapping[str, Any]] = []
    parsed: Any = None
    parsed_single_json = False
    if raw_text.startswith("{") or raw_text.startswith("["):
        try:
            parsed = json.loads(raw_text)
            parsed_single_json = True
        except json.JSONDecodeError:
            parsed_single_json = False

    if parsed_single_json:
        if isinstance(parsed, Mapping):
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
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DocumentIndexerError(
                    f"Malformed JSON on line {line_no} of manifest {path}: {exc}"
                ) from exc
            if not isinstance(obj, Mapping):
                raise AuditHoldoutError(
                    f"Manifest {path} line {line_no} is not a JSON object."
                )
            rows.append(obj)

    if not rows:
        raise AuditHoldoutError(
            f"Manifest {path} contains zero trial rows; cannot authorize indexing."
        )

    in_scope: list[dict[str, Any]] = []
    audit_ids: set[str] = set()
    seen_flags: dict[str, bool] = {}
    has_audit_rows = False

    for row in rows:
        nct_id = validate_nct_id(row.get("nct_id"))
        if "in_audit" not in row or type(row.get("in_audit")) is not bool:
            raise AuditHoldoutError(
                f"Manifest row for {nct_id} has non-bool or missing in_audit={row.get('in_audit')!r}; "
                "explicit boolean required."
            )
        flag: bool = row["in_audit"]
        if nct_id in seen_flags:
            if seen_flags[nct_id] != flag:
                raise AuditHoldoutError(
                    f"Manifest {path} contains conflicting duplicate in_audit values for {nct_id}."
                )
            continue
        seen_flags[nct_id] = flag
        if flag is True:
            has_audit_rows = True
            audit_ids.add(nct_id)
        else:
            in_scope.append(dict(row))

    if caller_audit is not None:
        if has_audit_rows and not caller_audit:
            raise AuditHoldoutError(
                f"Manifest {path} contains in_audit=True rows, so audit_nct_ids must not be empty."
            )
        missing_from_caller = audit_ids - caller_audit
        if missing_from_caller:
            raise AuditHoldoutError(
                f"Caller audit_nct_ids omits manifest in_audit=True trials: {sorted(missing_from_caller)}"
            )
        audit_ids |= set(caller_audit)

    if has_audit_rows and not audit_ids:
        raise AuditHoldoutError(
            f"Manifest {path} contains in_audit=True rows, but resolved audit_nct_ids is empty."
        )

    return in_scope, frozenset(audit_ids)


def _extract_study_dates(study: Mapping[str, Any]) -> tuple[str | None, str | None]:
    protocol = study.get("protocolSection")
    status_mod = (
        protocol.get("statusModule") if isinstance(protocol, Mapping) else {}
    )
    if not isinstance(status_mod, Mapping):
        return None, None
    pcd_struct = status_mod.get("primaryCompletionDateStruct")
    rfp_struct = status_mod.get("resultsFirstPostDateStruct")
    pcd = (
        str(pcd_struct.get("date")).strip()
        if isinstance(pcd_struct, Mapping) and pcd_struct.get("date")
        else None
    )
    rfp = (
        str(rfp_struct.get("date")).strip()
        if isinstance(rfp_struct, Mapping) and rfp_struct.get("date")
        else None
    )
    return pcd, rfp


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
    """Index all in-scope largeDocs for a single CT.gov study JSON behind the in_audit firewall."""
    effective_allowed = _merge_allowed_sets(allowed_nct_ids, in_scope_nct_ids)

    # Hard firewall BEFORE inspecting documents, cache, local PDFs, or network:
    # validates (a) strict ^NCT[0-9]{8}$ on study payload nctId, (b) exact equality
    # with requested nct_id when supplied, (c) audit_nct_ids / in_audit, and
    # (d) allowed_nct_ids / in_scope_nct_ids allowlist membership.
    study_nct_id = validate_study_payload_nct_id(
        study,
        expected_nct_id=nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=effective_allowed,
    )

    in_scope_docs, excluded_icf_count = extract_in_scope_large_docs(
        study,
        expected_nct_id=study_nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
        allowed_nct_ids=effective_allowed,
    )
    primary_completion_date, results_first_post_date = _extract_study_dates(study)
    stamp = extracted_at or utc_now_iso()

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
                has_pages = cache_store.load_cached_pages(manifest_sha) is not None
                blob_path = cache_store.get_blob_path(manifest_sha)
                if has_entry or has_pages or blob_path is not None:
                    resolved_sha = manifest_sha
                    resolved_bytes = (
                        int(manifest_rec["byte_count"])
                        if manifest_rec and type(manifest_rec.get("byte_count")) is int
                        else api_size
                    )
                    resolved_pdf_path = blob_path

        # 2. Local PDF directory resolution without duplicating multi-MB files
        if resolved_sha is None and local_pdf_dir is not None:
            candidate = Path(local_pdf_dir) / study_nct_id / filename
            if candidate.exists():
                prefix = candidate.read_bytes()[:1024]
                if not prefix or not has_pdf_magic(prefix):
                    download_error = "local_pdf_invalid_header_or_empty"
                    http_status = None
                else:
                    resolved_sha, resolved_bytes = sha256_file(candidate)
                    resolved_pdf_path = candidate

        # 3. Live HTTP download (unless offline or already resolved)
        if resolved_sha is None and download_error is None:
            if offline:
                http_status = None
                download_error = "offline_cache_miss"
            else:
                if session is None:
                    import requests  # lazy import

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
                if dl.ok and dl.sha256 is not None and dl.byte_count is not None:
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
            trial_records.append(
                build_trial_document_record(
                    nct_id=study_nct_id,
                    filename=filename,
                    doc_type=doc_type,
                    url=url,
                    http_status=http_status,
                    entry=None,
                    download_error=f"page_extraction_error: {exc}",
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

        # Ensure url_manifest records local-PDF hits too when API size/uploadDate exist
        if cache_store.lookup_url_manifest(url) is None:
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
    """Fetch a single study from CTGovAPIClient and index its documents behind the in_audit firewall."""
    effective_allowed = _merge_allowed_sets(allowed_nct_ids, in_scope_nct_ids)
    # Firewall runs BEFORE calling api_client.get_study()!
    nct = enforce_not_in_audit(
        nct_id,
        in_audit=in_audit,
        audit_nct_ids=audit_nct_ids,
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
        audit_nct_ids=audit_nct_ids,
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


def build_arg_parser() -> argparse.ArgumentParser:
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
            "exit 2 if in_audit=True or unlisted in manifest)."
        ),
    )
    parser.add_argument(
        "--manifest",
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
        type=Path,
        default=None,
        help=(
            "Offline-ingest legacy rule-census documents.jsonl into the content-addressed "
            "cache (requires --manifest)."
        ),
    )
    parser.add_argument(
        "--local-pdf-dir",
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
        type=Path,
        default=Path("data/document_cache"),
        help="Root directory for content-addressed document cache.",
    )
    parser.add_argument(
        "--index-out",
        type=Path,
        default=Path("data/document_index_by_sha256.jsonl"),
        help="Output path for sha256-keyed DocumentIndexEntry JSONL.",
    )
    parser.add_argument(
        "--profiles-out",
        type=Path,
        default=Path("data/trial_document_profiles.jsonl"),
        help="Output path for TrialDocumentProfile JSONL.",
    )
    parser.add_argument("--min-selected-pages", type=int, default=10)
    parser.add_argument("--max-selected-pages", type=int, default=25)
    parser.add_argument("--rate-limit-sec", type=float, default=DEFAULT_RATE_LIMIT_SEC)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--reparse-cached-pages", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    api_client: Any | None = None,
    session: Any | None = None,
    provider: DocumentPageExtractor | None = None,
) -> int:
    """Execute the document pre-indexer CLI. Returns 0 on success, 2 on AuditHoldoutError/usage, 1 on other errors."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    has_nct_arg = any(group for group in (args.nct_id or []))
    if not has_nct_arg and args.manifest is None and args.ingest_census_jsonl is None:
        parser.print_usage(sys.stderr)
        print(
            "index_trial_documents.py: error: at least one target argument "
            "(--manifest, --nct-id, or --ingest-census-jsonl) is required.",
            file=sys.stderr,
        )
        return 2

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

        target_ncts: list[str] = []
        if args.ingest_census_jsonl is None:
            if explicit_nct_ids:
                target_ncts = explicit_nct_ids
            elif manifest_rows:
                target_ncts = [validate_nct_id(row["nct_id"]) for row in manifest_rows]
            if args.limit is not None:
                target_ncts = target_ncts[: args.limit]
            if target_ncts and args.offline:
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
            )
            if args.limit is not None:
                census_profiles = census_profiles[: args.limit]
            profiles.extend(census_profiles)
            write_profiles_jsonl(args.profiles_out, profiles)
            return 0

        if not target_ncts:
            write_profiles_jsonl(args.profiles_out, profiles)
            return 0

        rate_limiter = GlobalRateLimiter(
            args.cache_dir / ".ctgov_rate_limit.lock",
            min_interval_sec=args.rate_limit_sec,
        )
        if api_client is None:
            from src.api_client import CTGovAPIClient  # lazy import

            api_client = CTGovAPIClient(rate_limit_delay=args.rate_limit_sec)
        if session is None:
            import requests  # lazy import

            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})

        iterator: Iterable[str] = target_ncts
        if len(target_ncts) > 1:
            try:
                from tqdm import tqdm  # lazy import

                iterator = tqdm(target_ncts, desc="Indexing trial docs", unit="trial")
            except ImportError:
                iterator = target_ncts

        for nct in iterator:
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
            profiles.append(profile)

        write_profiles_jsonl(args.profiles_out, profiles)
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
