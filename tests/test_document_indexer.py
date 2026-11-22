"""Deterministic offline pytest suite for Module 1:
src/document_indexer.py and scripts/index_trial_documents.py.

Runs without network access or external PDF binaries on both Linux CI and macOS.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

import pytest

from scripts import index_trial_documents as cli_mod
from src import document_indexer as di


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def iter_content(self, chunk_size: int = 65536) -> list[bytes]:
        return [self.content[i : i + chunk_size] for i in range(0, len(self.content), chunk_size)]


class _FakeSession:
    def __init__(self, responses: dict[str, _FakeResponse | Exception]) -> None:
        self._responses = responses
        self.requested_urls: list[str] = []

    def get(self, url: str, timeout: int = 60, stream: bool = True) -> _FakeResponse:
        self.requested_urls.append(url)
        resp = self._responses[url]
        if isinstance(resp, Exception):
            raise resp
        return resp


class _SpyAPIClient:
    def __init__(self, studies: dict[str, dict[str, Any]]) -> None:
        self._studies = studies
        self.requested_ncts: list[str] = []

    def get_study(self, nct_id: str) -> dict[str, Any]:
        self.requested_ncts.append(nct_id)
        return self._studies[nct_id]


def _make_study(
    nct_id: str,
    large_docs: list[dict[str, Any]],
    *,
    primary_completion_date: str | None = "2020-05-01",
    results_first_post_date: str | None = "2020-11-15",
) -> dict[str, Any]:
    status_mod: dict[str, Any] = {}
    if primary_completion_date is not None:
        status_mod["primaryCompletionDateStruct"] = {"date": primary_completion_date}
    if results_first_post_date is not None:
        status_mod["resultsFirstPostDateStruct"] = {"date": results_first_post_date}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "statusModule": status_mod,
        },
        "documentSection": {
            "largeDocumentModule": {
                "largeDocs": large_docs,
            }
        },
    }


def _make_minimal_pdf(text: str) -> bytes:
    """Build a minimal spec-compliant 1-page ASCII %PDF-1.4 document with exact xref offsets."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("ascii")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            b"5 0 obj\n<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream\nendobj\n"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


_DISTINCT_TOPICS: tuple[str, ...] = (
    "screening assessments",
    "randomization allocation",
    "blinding procedures",
    "concomitant medications",
    "dose modifications",
    "laboratory evaluations",
    "adverse event reporting",
    "serious adverse events",
    "pharmacokinetic sampling",
    "biomarker collection",
    "quality of life questionnaires",
    "protocol deviations",
    "data monitoring committee",
    "source data verification",
    "ethics committee approval",
    "informed consent process",
)


def _clean_stat_pages(n_pages: int = 4) -> list[str]:
    base = [
        (
            "Study Protocol Version 2.0\n"
            "Date: 15 May 2020\n"
            "Clinical Trial Protocol for Primary Efficacy and Safety Study."
        ),
        (
            "1. Objectives and Endpoints\n"
            "The primary endpoint is overall survival in all randomized patients."
        ),
        (
            "2. Analysis Populations\n"
            "The intent-to-treat population includes all randomized subjects in the trial."
        ),
        (
            "3. Statistical Methods and Sample Size\n"
            "The primary analysis uses a two-sided alpha of 0.05 with 90% power and "
            "a log-rank test comparing the treatment groups."
        ),
    ]
    if n_pages <= len(base):
        return base[:n_pages]
    out = list(base)
    for i in range(len(base) + 1, n_pages + 1):
        topic = _DISTINCT_TOPICS[(i - 1) % len(_DISTINCT_TOPICS)]
        cycle = (i - 1) // len(_DISTINCT_TOPICS)
        qualifier = ("initial", "follow-up", "extended", "long-term")[cycle % 4]
        out.append(
            f"Section {i} {qualifier.title()} {topic.title()}\n"
            f"All patients in the study undergo {qualifier} {topic} procedures "
            f"(item code {chr(65 + (i % 26))}{chr(97 + ((i * 7) % 26))}) according to the protocol."
        )
    return out


# ---------------------------------------------------------------------------
# Group 1: Audit Holdout Firewall
# ---------------------------------------------------------------------------


def test_audit_holdout_hard_block(tmp_path: Path) -> None:
    """in_audit=True, audit_nct_ids membership, or non-bool in_audit blocks before any I/O."""
    assert issubclass(di.AuditHoldoutError, di.DocumentIndexerError)
    assert issubclass(di.AuditHoldoutError, ValueError)

    # Direct guard checks
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT01234567", in_audit=True)
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit(
            "nct01234567", in_audit=False, audit_nct_ids={"NCT01234567"}
        )
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT01234567", in_audit=None)
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT01234567", in_audit=0)  # type: ignore[arg-type]

    # Spies must record ZERO calls when holdout fires
    pdf_bytes = _make_minimal_pdf("Protected audit trial")
    pdf_sha = di.sha256_bytes(pdf_bytes)
    nct = "NCT09999999"
    url = di.build_ctgov_large_doc_url(nct, "Prot_000.pdf")
    study = _make_study(
        nct,
        [{"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False, "size": len(pdf_bytes)}],
    )
    api_spy = _SpyAPIClient({nct: study})
    http_spy = _FakeSession({url: _FakeResponse(200, pdf_bytes)})
    provider_spy = di.FixturePageTextProvider({pdf_sha: _clean_stat_pages(2)})
    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")

    with pytest.raises(di.AuditHoldoutError):
        cli_mod.fetch_and_index_trial(
            nct,
            api_client=api_spy,
            cache_store=cache,
            provider=provider_spy,
            session=http_spy,
            in_audit=True,
        )
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.fetch_and_index_trial(
            nct,
            api_client=api_spy,
            cache_store=cache,
            provider=provider_spy,
            session=http_spy,
            in_audit=False,
            audit_nct_ids={nct},
        )
    with pytest.raises(di.AuditHoldoutError):
        cache.get_or_index_document(
            sha256=pdf_sha,
            byte_count=len(pdf_bytes),
            provider=provider_spy,
            nct_id=nct,
            filename="Prot_000.pdf",
            in_audit=True,
        )
    # Unconditional guard even when nct_id is None
    with pytest.raises(di.AuditHoldoutError):
        cache.get_or_index_document(
            sha256=pdf_sha,
            byte_count=len(pdf_bytes),
            provider=provider_spy,
            nct_id=None,
            in_audit=True,
        )
    with pytest.raises(di.AuditHoldoutError):
        cache.get_or_index_document(
            sha256=pdf_sha,
            byte_count=len(pdf_bytes),
            provider=provider_spy,
            nct_id=None,
            in_audit=None,
        )

    assert api_spy.requested_ncts == []
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []

    # Strict manifest mode rejects non-bool in_audit=None
    bad_manifest = tmp_path / "bad_manifest.json"
    bad_manifest.write_text(
        json.dumps({"data": [{"nct_id": "NCT01111111", "in_audit": None}]}),
        encoding="utf-8",
    )
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.load_manifest_rows(bad_manifest)

    # CLI skips in_audit=True manifest rows and returns exit 2 for explicit --nct-id in audit
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "data": [
                    {"nct_id": nct, "in_audit": True},
                    {"nct_id": "NCT01111111", "in_audit": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_audit = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--nct-id",
            nct,
            "--cache-dir",
            str(tmp_path / "c2"),
            "--index-out",
            str(tmp_path / "i2.jsonl"),
            "--profiles-out",
            str(tmp_path / "p2.jsonl"),
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_audit == 2
    assert api_spy.requested_ncts == []
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []


def test_cli_requires_manifest_and_blocks_unlisted_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI exits 2 with zero I/O when --manifest is missing or --nct-id/census row is unlisted."""
    pdf_bytes = _make_minimal_pdf("Guarded protocol text")
    pdf_sha = di.sha256_bytes(pdf_bytes)
    nct_in_scope = "NCT01111111"
    nct_audit = "NCT09999999"
    nct_unlisted = "NCT08888888"

    study_unlisted = _make_study(
        nct_unlisted,
        [
            {
                "filename": "Prot_000.pdf",
                "hasProtocol": True,
                "hasSap": False,
                "size": len(pdf_bytes),
            }
        ],
    )
    url_unlisted = di.build_ctgov_large_doc_url(nct_unlisted, "Prot_000.pdf")

    api_spy = _SpyAPIClient({nct_unlisted: study_unlisted})
    http_spy = _FakeSession({url_unlisted: _FakeResponse(200, pdf_bytes)})
    provider_spy = di.FixturePageTextProvider({pdf_sha: _clean_stat_pages(2)})

    census_jsonl = tmp_path / "documents.jsonl"
    census_jsonl.write_text(
        json.dumps(
            {
                "nct_id": nct_unlisted,
                "filename": "Prot_000.pdf",
                "url": url_unlisted,
                "http_status": 200,
                "sha256": pdf_sha,
                "bytes": len(pdf_bytes),
                "doc_type": "protocol",
                "version_label": "",
                "version_date_as_printed": "",
                "physical_pages": 2,
                "pages_without_text": [],
                "retrieved_at": "2026-11-14T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "data": [
                    {"nct_id": nct_in_scope, "in_audit": False},
                    {"nct_id": nct_audit, "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    cache_inits: list[tuple[Path, Path]] = []
    real_cache_store = cli_mod.DocumentCacheStore

    def _forbid_cache_init(
        cache_dir: Path = Path("data/document_cache"),
        index_path: Path = Path("data/document_index_by_sha256.jsonl"),
    ) -> di.DocumentCacheStore:
        cache_inits.append((Path(cache_dir), Path(index_path)))
        raise AssertionError("DocumentCacheStore must not be instantiated before manifest firewall")

    monkeypatch.setattr(cli_mod, "DocumentCacheStore", _forbid_cache_init)

    # (a) --nct-id without --manifest -> exit 2, zero I/O, no cache init
    cache_a = tmp_path / "cache_a"
    index_a = tmp_path / "index_a.jsonl"
    profiles_a = tmp_path / "profiles_a.jsonl"
    rc_a = cli_mod.run_cli(
        [
            "--nct-id",
            nct_unlisted,
            "--cache-dir",
            str(cache_a),
            "--index-out",
            str(index_a),
            "--profiles-out",
            str(profiles_a),
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_a == 2
    assert not cache_a.exists()
    assert not index_a.exists()
    assert not profiles_a.exists()

    # (b) --ingest-census-jsonl without --manifest -> exit 2, zero I/O, no cache init
    cache_b = tmp_path / "cache_b"
    index_b = tmp_path / "index_b.jsonl"
    profiles_b = tmp_path / "profiles_b.jsonl"
    rc_b = cli_mod.run_cli(
        [
            "--ingest-census-jsonl",
            str(census_jsonl),
            "--cache-dir",
            str(cache_b),
            "--index-out",
            str(index_b),
            "--profiles-out",
            str(profiles_b),
            "--offline",
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_b == 2
    assert not cache_b.exists()
    assert not index_b.exists()
    assert not profiles_b.exists()

    # (c) --nct-id absent from --manifest -> exit 2, zero I/O, no cache init
    cache_c = tmp_path / "cache_c"
    index_c = tmp_path / "index_c.jsonl"
    profiles_c = tmp_path / "profiles_c.jsonl"
    rc_c = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--nct-id",
            nct_unlisted,
            "--cache-dir",
            str(cache_c),
            "--index-out",
            str(index_c),
            "--profiles-out",
            str(profiles_c),
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_c == 2
    assert not cache_c.exists()
    assert not index_c.exists()
    assert not profiles_c.exists()

    # Malformed --nct-id without --manifest still exits 1 before manifest/cache checks
    rc_malformed = cli_mod.run_cli(
        [
            "--nct-id",
            "BAD_NCT",
            "--cache-dir",
            str(tmp_path / "cache_bad"),
            "--index-out",
            str(tmp_path / "index_bad.jsonl"),
            "--profiles-out",
            str(tmp_path / "profiles_bad.jsonl"),
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_malformed == 1

    assert cache_inits == []
    assert api_spy.requested_ncts == []
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []

    # --ingest-census-jsonl with --manifest still blocks unlisted census rows (exit 2, zero writes)
    monkeypatch.setattr(cli_mod, "DocumentCacheStore", real_cache_store)
    cache_d = tmp_path / "cache_d"
    index_d = tmp_path / "index_d.jsonl"
    profiles_d = tmp_path / "profiles_d.jsonl"
    rc_d = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--ingest-census-jsonl",
            str(census_jsonl),
            "--cache-dir",
            str(cache_d),
            "--index-out",
            str(index_d),
            "--profiles-out",
            str(profiles_d),
            "--offline",
        ],
        api_client=api_spy,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_d == 2
    assert not cache_d.exists()
    assert not index_d.exists()
    assert not profiles_d.exists()
    assert api_spy.requested_ncts == []
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []


def test_ingest_legacy_census_documents_audit_and_allowlist_guard(tmp_path: Path) -> None:
    """ingest_legacy_census_documents honors row-level in_audit and allowed_nct_ids before any I/O."""
    sha_ok = "1" * 64
    sha_bad = "2" * 64
    nct_ok = "NCT01543490"
    nct_other = "NCT02543490"

    def _census_row(nct: str, sha: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "nct_id": nct,
            "filename": "Prot_000.pdf",
            "url": di.build_ctgov_large_doc_url(nct, "Prot_000.pdf"),
            "http_status": 200,
            "sha256": sha,
            "bytes": 4096,
            "doc_type": "protocol",
            "version_label": "Version 1.0",
            "version_date_as_printed": "15 May 2020",
            "physical_pages": 4,
            "pages_without_text": [],
            "retrieved_at": "2026-11-14T00:00:00Z",
        }
        if extra is not None:
            row.update(extra)
        return row

    provider_spy = di.FixturePageTextProvider(
        {
            sha_ok: _clean_stat_pages(4),
            sha_bad: _clean_stat_pages(4),
        }
    )

    # 1. Row carrying explicit in_audit=True raises AuditHoldoutError (even when nct_id is allowed)
    jsonl_true = tmp_path / "in_audit_true.jsonl"
    jsonl_true.write_text(
        json.dumps(_census_row(nct_ok, sha_ok, {"in_audit": False}))
        + "\n"
        + json.dumps(_census_row(nct_other, sha_bad, {"in_audit": True}))
        + "\n",
        encoding="utf-8",
    )
    index_true = tmp_path / "idx_true.jsonl"
    cache_true = di.DocumentCacheStore(tmp_path / "cache_true", index_true)
    with pytest.raises(di.AuditHoldoutError):
        di.ingest_legacy_census_documents(
            jsonl_true,
            cache_store=cache_true,
            provider=provider_spy,
            allowed_nct_ids={nct_ok, nct_other},
        )
    assert provider_spy.calls == []
    assert cache_true.all_entries() == []
    assert not index_true.exists()

    # 2. Row carrying non-bool in_audit=None or int raises AuditHoldoutError
    for bad_val in (None, 0):
        jsonl_non_bool = tmp_path / f"in_audit_{bad_val}.jsonl"
        jsonl_non_bool.write_text(
            json.dumps(_census_row(nct_ok, sha_ok, {"in_audit": bad_val})) + "\n",
            encoding="utf-8",
        )
        index_nb = tmp_path / f"idx_{bad_val}.jsonl"
        cache_nb = di.DocumentCacheStore(tmp_path / f"cache_{bad_val}", index_nb)
        with pytest.raises(di.AuditHoldoutError):
            di.ingest_legacy_census_documents(
                jsonl_non_bool,
                cache_store=cache_nb,
                provider=provider_spy,
                allowed_nct_ids={nct_ok},
            )
        assert provider_spy.calls == []
        assert cache_nb.all_entries() == []
        assert not index_nb.exists()

    # 3. Row with nct_id outside allowed_nct_ids (non-empty or empty allowlist) raises AuditHoldoutError
    jsonl_unlisted = tmp_path / "unlisted.jsonl"
    jsonl_unlisted.write_text(
        json.dumps(_census_row(nct_ok, sha_ok, {"in_audit": False}))
        + "\n"
        + json.dumps(_census_row(nct_other, sha_bad, {"in_audit": False}))
        + "\n",
        encoding="utf-8",
    )
    for tag, allowlist in (("subset", {nct_ok}), ("empty", frozenset())):
        index_unlisted = tmp_path / f"idx_unlisted_{tag}.jsonl"
        cache_unlisted = di.DocumentCacheStore(tmp_path / f"cache_unlisted_{tag}", index_unlisted)
        with pytest.raises(di.AuditHoldoutError):
            di.ingest_legacy_census_documents(
                jsonl_unlisted,
                cache_store=cache_unlisted,
                provider=provider_spy,
                allowed_nct_ids=allowlist,
            )
        assert provider_spy.calls == []
        assert cache_unlisted.all_entries() == []
        assert not index_unlisted.exists()

    # 4. Positive control: row with in_audit=False inside allowed_nct_ids succeeds
    jsonl_ok = tmp_path / "ok.jsonl"
    jsonl_ok.write_text(
        json.dumps(_census_row(nct_ok, sha_ok, {"in_audit": False})) + "\n",
        encoding="utf-8",
    )
    index_ok = tmp_path / "idx_ok.jsonl"
    cache_ok = di.DocumentCacheStore(tmp_path / "cache_ok", index_ok)
    entries, profiles = di.ingest_legacy_census_documents(
        jsonl_ok,
        cache_store=cache_ok,
        provider=provider_spy,
        allowed_nct_ids={nct_ok},
    )
    assert len(entries) == 1
    assert len(profiles) == 1
    assert profiles[0]["nct_id"] == nct_ok
    assert len(provider_spy.calls) == 1
    assert index_ok.exists()


# ---------------------------------------------------------------------------
# Group 2: Content-Addressed Cache Immutability & Cross-Trial Dedup
# ---------------------------------------------------------------------------


def test_sha256_cache_dedup_and_rule_bump(tmp_path: Path) -> None:
    """Twin-trial sha256 dedup, url_manifest skip, rule-bump cached re-index, and integrity guard."""
    sap_bytes = _make_minimal_pdf("Shared pooled Statistical Analysis Plan")
    sap_sha = di.sha256_bytes(sap_bytes)
    sap_size = len(sap_bytes)

    nct_a = "NCT02163759"
    nct_b = "NCT02171429"
    url_a = di.build_ctgov_large_doc_url(nct_a, "SAP_001.pdf")
    url_b = di.build_ctgov_large_doc_url(nct_b, "SAP_001.pdf")

    study_a = _make_study(
        nct_a,
        [
            {
                "filename": "SAP_001.pdf",
                "hasProtocol": False,
                "hasSap": True,
                "size": sap_size,
                "uploadDate": "08/21/2020 14:32",
            }
        ],
    )
    study_b = _make_study(
        nct_b,
        [
            {
                "filename": "SAP_001.pdf",
                "hasProtocol": False,
                "hasSap": True,
                "size": sap_size,
                "uploadDate": "09/10/2020 09:00",
            }
        ],
    )

    session = _FakeSession(
        {
            url_a: _FakeResponse(200, sap_bytes),
            url_b: _FakeResponse(200, sap_bytes),
        }
    )
    provider = di.FixturePageTextProvider({sap_sha: _clean_stat_pages(4)})
    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")

    prof_a = cli_mod.index_trial_from_study(
        study_a,
        cache_store=cache,
        provider=provider,
        session=session,
        extracted_at="2026-11-20T01:00:00+00:00",
        pipeline_commit="aaaaaaa1111111",
    )
    assert len(provider.calls) == 1
    assert prof_a["documents"][0]["cache_hit"] is False
    assert len(session.requested_urls) == 1

    # Twin trial B shares the exact same SAP sha256 -> provider called STILL once,
    # original (extracted_at, pipeline_commit) preserved on the immutable sha256 entry!
    prof_b = cli_mod.index_trial_from_study(
        study_b,
        cache_store=cache,
        provider=provider,
        session=session,
        extracted_at="2026-11-21T02:00:00+00:00",
        pipeline_commit="bbbbbbb2222222",
    )
    assert len(provider.calls) == 1
    assert prof_b["documents"][0]["cache_hit"] is True
    stored_entry = cache.get_entry(sap_sha)
    assert stored_entry is not None
    assert stored_entry.extracted_at == "2026-11-20T01:00:00+00:00"
    assert stored_entry.pipeline_commit == "aaaaaaa1111111"

    # Re-running trial A with matching (url, api_size, api_upload_date) makes 0 HTTP calls
    calls_before = len(session.requested_urls)
    prof_a_again = cli_mod.index_trial_from_study(
        study_a,
        cache_store=cache,
        provider=provider,
        session=session,
        extracted_at="2026-11-22T03:00:00+00:00",
        pipeline_commit="ccccccc3333333",
    )
    assert len(session.requested_urls) == calls_before
    assert len(provider.calls) == 1
    assert prof_a_again["documents"][0]["cache_hit"] is True

    # Bumping indexer_rules_version re-indexes from cached pages/<sha[:2]>/<sha>.json.gz
    # with ZERO provider calls and ZERO HTTP calls
    assert cache.page_map_path(sap_sha).exists()
    bumped_entry, hit = cache.get_or_index_document(
        sha256=sap_sha,
        byte_count=sap_size,
        provider=provider,
        doc_type="sap",
        extracted_at="2026-11-23T04:00:00+00:00",
        pipeline_commit="ddddddd4444444",
        indexer_rules_version="doc_indexer@2026-12-01.v2",
    )
    assert hit is False
    assert len(provider.calls) == 1
    assert bumped_entry.text_provider == "cached_page_map"
    assert bumped_entry.indexer_rules_version == "doc_indexer@2026-12-01.v2"

    # reparse_cached_pages=True overwrites the stored entry with updated slicer parameters
    long_bytes = _make_minimal_pdf("Long protocol for reparse overwrite test")
    long_sha = di.sha256_bytes(long_bytes)
    long_provider = di.FixturePageTextProvider({long_sha: _clean_stat_pages(30)})
    entry_15, hit_first = cache.get_or_index_document(
        sha256=long_sha,
        byte_count=len(long_bytes),
        provider=long_provider,
        doc_type="protocol",
        min_selected_pages=15,
        max_selected_pages=25,
    )
    assert hit_first is False
    assert len(entry_15.section_slice.pages_selected) >= 15
    assert len(long_provider.calls) == 1

    reparsed_8, hit_reparse = cache.get_or_index_document(
        sha256=long_sha,
        byte_count=len(long_bytes),
        provider=long_provider,
        doc_type="protocol",
        min_selected_pages=5,
        max_selected_pages=8,
        reparse_cached_pages=True,
    )
    assert hit_reparse is False
    assert len(long_provider.calls) == 1  # reparsed strictly from cached pages
    assert len(reparsed_8.section_slice.pages_selected) <= 8
    assert reparsed_8 != entry_15
    persisted_after_reparse = cache.get_entry(long_sha)
    assert persisted_after_reparse == reparsed_8
    # Reload from disk to confirm atomic index persistence of the reparsed entry
    reloaded_cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")
    assert reloaded_cache.get_entry(long_sha) == reparsed_8

    # Conflicting byte_count for existing sha256 raises CacheIntegrityError
    with pytest.raises(di.CacheIntegrityError):
        cache.get_or_index_document(
            sha256=sap_sha,
            byte_count=sap_size + 1,
            provider=provider,
            doc_type="sap",
        )


# ---------------------------------------------------------------------------
# Group 3: All 6 Readability Statuses Never Collapsed
# ---------------------------------------------------------------------------


def test_six_readability_statuses_disjoint(tmp_path: Path) -> None:
    """Every one of the 6 readability statuses is distinct and accurately triggered."""
    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")

    # 1. no_document: 0 largeDocs AND ICF-only trial
    study_empty = _make_study("NCT01000001", [])
    prof_empty = cli_mod.index_trial_from_study(study_empty, cache_store=cache, provider=None)
    assert prof_empty["readability_summary"] == "no_document"
    assert prof_empty["excluded_icf_count"] == 0
    assert prof_empty["documents"] == []

    study_icf_only = _make_study(
        "NCT01000002",
        [{"filename": "ICF_000.pdf", "hasProtocol": False, "hasSap": False, "hasIcf": True}],
    )
    prof_icf = cli_mod.index_trial_from_study(study_icf_only, cache_store=cache, provider=None)
    assert prof_icf["readability_summary"] == "no_document"
    assert prof_icf["excluded_icf_count"] == 1
    assert prof_icf["documents"] == []

    # 2. download_failed: HTTP 404, timeout, HTML 200 OK lacking %PDF-, 0-byte file
    nct_fail = "NCT01000003"
    u404 = di.build_ctgov_large_doc_url(nct_fail, "Prot_000.pdf")
    utime = di.build_ctgov_large_doc_url(nct_fail, "SAP_001.pdf")
    uhtml = di.build_ctgov_large_doc_url(nct_fail, "Prot_002.pdf")
    uzero = di.build_ctgov_large_doc_url(nct_fail, "SAP_003.pdf")
    fail_session = _FakeSession(
        {
            u404: _FakeResponse(404, b"Not Found"),
            utime: TimeoutError("read timed out"),
            uhtml: _FakeResponse(200, b"<html><body>Gateway Error</body></html>"),
            uzero: _FakeResponse(200, b""),
        }
    )
    study_fail = _make_study(
        nct_fail,
        [
            {"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False},
            {"filename": "SAP_001.pdf", "hasProtocol": False, "hasSap": True},
            {"filename": "Prot_002.pdf", "hasProtocol": True, "hasSap": False},
            {"filename": "SAP_003.pdf", "hasProtocol": False, "hasSap": True},
        ],
    )
    prof_fail = cli_mod.index_trial_from_study(
        study_fail, cache_store=cache, provider=None, session=fail_session
    )
    assert prof_fail["readability_summary"] == "download_failed"
    assert [d["readability_status"] for d in prof_fail["documents"]] == ["download_failed"] * 4
    assert all(d["sha256"] is None for d in prof_fail["documents"])

    # 3. image_only_scan:
    # 3a. Pure 0/N blank scan
    e_pure_scan = di.index_document_pages(
        ["", "   ", "\n"],
        sha256="1" * 64,
        byte_count=1000,
        text_provider="fixture",
    )
    assert e_pure_scan.readability_status == "image_only_scan"
    assert e_pure_scan.unreadable_recovery_bucket == "image_only_scan"

    # 3b. 91-page recurring-stamp-only scan (NCT03197818 model)
    stamp_pages = ["Approved CLI-CCD-2020-001 External Use"] * 81 + [""] * 10
    e_stamp_scan = di.index_document_pages(
        stamp_pages,
        sha256="2" * 64,
        byte_count=50000,
        text_provider="fixture",
    )
    assert e_stamp_scan.readability_status == "image_only_scan"
    assert e_stamp_scan.redaction.redaction_flags == ()

    # 3c. 1-page cover sheet + 105 blank scanned pages (NCT02687451 model)
    cover_scan = [
        "CONFIDENTIAL\nStudy XYZ-100\nSponsor: Example Pharma\nDate: 2019-01-10"
    ] + [""] * 105
    e_cover_scan = di.index_document_pages(
        cover_scan,
        sha256="3" * 64,
        byte_count=90000,
        text_provider="fixture",
    )
    assert e_cover_scan.readability_status == "image_only_scan"

    # 4. garbled_cmap:
    # 4a. Punctuation-shifted CMap (NCT02579265 model)
    punct_garbled = [
        "#:':/9:/)'24 !\"#$%&'()*+,-./:;<=>?@ [\\]^_`{|}~ #:':/9:/)'24 !\"#$%&'()*+,-./",
        "#:':/9:/)'24 ;:<=>?@!\"#$%&'()*+,-./ #:':/9:/)'24 !\"#$%&'()*+,-./:;<=>?@",
        "!\n!\n!\n!\n!\n!",  # NCT03420781 repeated symbol pattern
    ]
    e_punct = di.index_document_pages(
        punct_garbled,
        sha256="4" * 64,
        byte_count=20000,
        text_provider="fixture",
    )
    assert e_punct.readability_status == "garbled_cmap"
    assert e_punct.unreadable_recovery_bucket == "garbled_cmap"
    assert e_punct.cmap_metrics.garbled_page_count == 3

    # 4b. Uppercase Caesar-shifted CMap (NCT03937882 'DGPLQLVWUDWLRQ' model)
    caesar_page = (
        "DGPLQLVWUDWLRQ SURWRFRO VWDWLVWLFDO DQDOBVLV SODQ FOLQLFDO WULDO "
        "SULPDUB HQGSRLQW VDPSOH VLCH SRZHU FDOFXODWLRQ WKH VWXGB GHVLJQ"
    )
    e_caesar = di.index_document_pages(
        [caesar_page, caesar_page, caesar_page],
        sha256="5" * 64,
        byte_count=20000,
        text_provider="fixture",
    )
    assert e_caesar.readability_status == "garbled_cmap"

    # 4c. Dense numeric tables (digit_ratio >= 0.65) and clean Greek/math prose NOT flagged!
    dense_numeric = (
        "Table 14.2.1\n"
        "101 202 303 404 505 606 707 808 909 100 112 223 334 445 556 667 778 889 990\n"
        "123 456 789 012 345 678 901 234 567 890 111 222 333 444 555 666 777 888 999\n"
    )
    greek_math = (
        "The primary hypothesis H0: μ1 − μ2 ≤ δ is tested using a two-sided α = 0.05 "
        "significance level with power 1 − β = 0.90, σ = 2.5, χ² test statistic, "
        "and Σ wi = 1 across all randomized patients in the study."
    )
    assert di.analyze_page_cmap(dense_numeric).digit_ratio >= 0.65
    assert di.analyze_page_cmap(dense_numeric).is_garbled is False
    assert di.analyze_page_cmap(greek_math).is_garbled is False
    e_clean_math = di.index_document_pages(
        [dense_numeric, greek_math, *_clean_stat_pages(2)],
        sha256="6" * 64,
        byte_count=12000,
        text_provider="fixture",
    )
    assert e_clean_math.readability_status == "machine_readable"

    # 5. partial_image_or_redacted: single blank page (NCT03496012 p.49 model)
    pages_50 = _clean_stat_pages(50)
    pages_50[48] = ""  # p.49 blank
    e_partial_blank = di.index_document_pages(
        pages_50,
        sha256="7" * 64,
        byte_count=80000,
        text_provider="fixture",
    )
    assert e_partial_blank.readability_status == "partial_image_or_redacted"
    assert e_partial_blank.pages_without_text == (49,)
    assert e_partial_blank.redaction.pages_header_stamp_only == ()
    assert e_partial_blank.unreadable_recovery_bucket == "image_only_scan"

    # 6. machine_readable: all pages clean
    e_clean = di.index_document_pages(
        _clean_stat_pages(5),
        sha256="8" * 64,
        byte_count=15000,
        text_provider="fixture",
    )
    assert e_clean.readability_status == "machine_readable"
    assert e_clean.redaction.pages_header_stamp_only == ()
    assert e_clean.unreadable_recovery_bucket is None

    # All 6 statuses verified disjoint
    observed = {
        prof_empty["readability_summary"],
        prof_fail["readability_summary"],
        e_pure_scan.readability_status,
        e_punct.readability_status,
        e_partial_blank.readability_status,
        e_clean.readability_status,
    }
    assert observed == set(di.READABILITY_STATUSES)

    # Mixed trial-level summarization rules
    rec_clean = di.build_trial_document_record(
        nct_id="NCT01000099",
        filename="Prot_000.pdf",
        doc_type="protocol",
        url=di.build_ctgov_large_doc_url("NCT01000099", "Prot_000.pdf"),
        http_status=200,
        entry=e_clean,
    )
    rec_scan = di.build_trial_document_record(
        nct_id="NCT01000099",
        filename="SAP_001.pdf",
        doc_type="sap",
        url=di.build_ctgov_large_doc_url("NCT01000099", "SAP_001.pdf"),
        http_status=200,
        entry=e_pure_scan,
    )
    rec_cmap = di.build_trial_document_record(
        nct_id="NCT01000099",
        filename="SAP_002.pdf",
        doc_type="sap",
        url=di.build_ctgov_large_doc_url("NCT01000099", "SAP_002.pdf"),
        http_status=200,
        entry=e_punct,
    )
    assert di.summarize_trial_readability([rec_clean, rec_scan]) == "partial_image_or_redacted"
    assert di.summarize_trial_readability([rec_scan, rec_cmap]) == "garbled_cmap"
    assert di.summarize_trial_readability([rec_scan]) == "image_only_scan"


# ---------------------------------------------------------------------------
# Group 4: Redaction-Marker Precision & Zero False Positives
# ---------------------------------------------------------------------------


def test_redaction_markers_no_false_positives() -> None:
    """Detect CCI/PPD/REDACTED/white-box/orphaned headings with 0 false positives on clinical prose."""
    # Positive detections
    pages_cci = [
        "Protocol Cover\nAll patients enrolled in the trial.",
        "Section 8 Statistical Considerations\nCCI\nPrimary endpoint analysis.",
        "Appendix B\n[CCI] removed by sponsor.",
    ]
    r_cci = di.detect_redactions(pages_cci)
    assert "cci" in r_cci.redaction_flags
    assert r_cci.pages_with_cci == (2, 3)

    pages_ppd = [
        "Protocol Cover\nStudy population overview.",
        "PPD\nPPD\nANALYSIS POPULATION",
        "Contact details [PPD] omitted.",
    ]
    r_ppd = di.detect_redactions(pages_ppd)
    assert "ppd" in r_ppd.redaction_flags
    assert r_ppd.pages_with_ppd == (2, 3)

    pages_redacted_tag = [
        "Study Protocol Version 1.0",
        "Section 9 Sample Size\n[REDACTED] for confidentiality.",
    ]
    r_tag = di.detect_redactions(pages_redacted_tag)
    assert "white_box_or_blank_stats" in r_tag.redaction_flags
    assert r_tag.pages_with_redacted_tag == (2,)

    # Running-header-only white-box pages (NCT03426267 pattern)
    header = "Sponsor Protocol ABC-123\nCONFIDENTIAL"
    body_lines = [
        "Background and study rationale for randomized evaluation.",
        "Inclusion and exclusion criteria for adult participants.",
        "Treatment administration schedule and dose escalation rules.",
        "Primary efficacy endpoint definitions and adjudication charter.",
        "Secondary safety endpoints and laboratory monitoring cadence.",
        "Randomization stratification factors and interactive response system.",
        "Handling of protocol deviations and missing outcome assessments.",
    ]
    pages_wb = [
        f"{header}\nPage {i} of 10\n{body_lines[i - 1]}"
        for i in range(1, 8)
    ] + [
        f"{header}\nPage {i} of 10"
        for i in range(8, 11)
    ]
    r_wb = di.detect_redactions(pages_wb)
    assert r_wb.pages_header_stamp_only == (8, 9, 10)
    assert "white_box_or_blank_stats" in r_wb.redaction_flags

    # Orphaned statistical headings (NCT03925727 & NCT03426267 pattern)
    pages_orphan = [
        "Study Protocol Overview for randomized patients.",
        "7. Analysis Populations\n7.1 Intent-to-Treat\n7.2 Modified Intent-to-Treat",
        "9.1.3\n9.1.4 Analysis Populations",
    ]
    r_orphan = di.detect_redactions(pages_orphan)
    assert r_orphan.pages_orphaned_stat_headings == (2, 3)
    assert "white_box_or_blank_stats" in r_orphan.redaction_flags

    # TOC-mapped blank statistical pages (NCT03616899 pattern)
    pages_toc_blank = [
        "Protocol Cover Version 1.0",
        "Table of Contents\n1. Introduction ........ 3\n2. Statistical Methods ........ 4\n3. References ........ 6",
        "1. Introduction\nThis study evaluates patients in a clinical trial.",
        "",  # blank stat page 4
        "",  # blank stat page 5
        "3. References\nStandard clinical trial references listed here.",
    ]
    entry_toc_blank = di.index_document_pages(
        pages_toc_blank,
        sha256="9" * 64,
        byte_count=15000,
        text_provider="fixture",
    )
    assert entry_toc_blank.readability_status == "partial_image_or_redacted"
    assert 4 in entry_toc_blank.redaction.unreadable_statistical_pages
    assert "white_box_or_blank_stats" in entry_toc_blank.redaction.redaction_flags

    # Pure redacted (no blank pages) maps to redacted_cci_ppd recovery bucket
    entry_pure_cci = di.index_document_pages(
        pages_cci,
        sha256="a" * 64,
        byte_count=15000,
        text_provider="fixture",
    )
    assert entry_pure_cci.readability_status == "partial_image_or_redacted"
    assert entry_pure_cci.unreadable_recovery_bucket == "redacted_cci_ppd"

    # ZERO false positives on benign clinical/CRO prose
    benign_pages = [
        "Baseline comorbidities were summarized using the Charlson Comorbidity Index (CCI) score.",
        "Data management and biostatistics services were conducted by PPD, Inc. under sponsor oversight.",
        "Tuberculosis screening used the purified protein derivative (PPD) skin test prior to randomization.",
        (
            "7. Analysis Populations\n"
            "7.1 Intent-to-Treat\n"
            "The intent-to-treat population includes all randomized patients who received at least "
            "one dose of study treatment and had a baseline efficacy assessment."
        ),
    ]
    r_benign = di.detect_redactions(benign_pages)
    assert r_benign.redaction_flags == ()
    assert r_benign.pages_with_cci == ()
    assert r_benign.pages_with_ppd == ()
    assert r_benign.pages_orphaned_stat_headings == ()


# ---------------------------------------------------------------------------
# Group 5: Upload-Timing Factual Deltas & Partial-Date Precision
# ---------------------------------------------------------------------------


def test_upload_timing_deltas_and_neutrality() -> None:
    """Precision-aware upload timing never fabricates '-01' and emits zero compliance/evaluative words."""
    # Day/day after completion
    t_after = di.compute_upload_timing(
        "08/21/2020 14:32",
        primary_completion_date="2020-05-01",
        results_first_post_date="2020-08-21",
    )
    assert t_after["uploaded_date"]["iso_normalized"] == "2020-08-21"
    assert t_after["uploaded_date"]["date_precision"] == "day"
    assert t_after["vs_primary_completion"]["exact_days"] == 112
    assert t_after["vs_primary_completion"]["min_days"] == 112
    assert t_after["vs_primary_completion"]["max_days"] == 112
    assert t_after["vs_primary_completion"]["relation"] == "after"
    # Same day
    assert t_after["vs_results_first_post"]["exact_days"] == 0
    assert t_after["vs_results_first_post"]["relation"] == "same_day"

    # Uploaded before completion
    t_before = di.compute_upload_timing(
        "04/01/2020 08:00",
        primary_completion_date="2020-05-01",
        results_first_post_date="2020-09-01",
    )
    assert t_before["vs_primary_completion"]["exact_days"] == -30
    assert t_before["vs_primary_completion"]["relation"] == "before"

    # Month-only anchor overlapping upload date -> exact_days is None, [-10, 20], ambiguous_partial_date
    t_month = di.compute_upload_timing(
        "08/21/2020 14:32",
        primary_completion_date="2020-08",
        results_first_post_date="2020",
    )
    assert t_month["primary_completion_date"]["iso_normalized"] == "2020-08"
    assert t_month["primary_completion_date"]["date_precision"] == "month"
    assert t_month["vs_primary_completion"]["exact_days"] is None
    assert t_month["vs_primary_completion"]["min_days"] == -10
    assert t_month["vs_primary_completion"]["max_days"] == 20
    assert t_month["vs_primary_completion"]["relation"] == "ambiguous_partial_date"
    assert t_month["vs_results_first_post"]["exact_days"] is None
    assert t_month["vs_results_first_post"]["date_precision"] == "year"

    # Leap-year February 2020 (29 days) vs non-leap February 2021 (28 days)
    feb_2020 = di.parse_ctgov_date("2020-02")
    feb_2021 = di.parse_ctgov_date("2021-02")
    assert feb_2020["earliest_iso"] == "2020-02-01"
    assert feb_2020["latest_iso"] == "2020-02-29"
    assert feb_2021["earliest_iso"] == "2021-02-01"
    assert feb_2021["latest_iso"] == "2021-02-28"

    # Missing date -> unknown / unknown_missing_date
    t_missing = di.compute_upload_timing(
        None,
        primary_completion_date="2020-05-01",
        results_first_post_date=None,
    )
    assert t_missing["uploaded_date"]["date_precision"] == "unknown"
    assert t_missing["vs_primary_completion"]["exact_days"] is None
    assert t_missing["vs_primary_completion"]["relation"] == "unknown_missing_date"
    assert t_missing["vs_results_first_post"]["relation"] == "unknown_missing_date"

    # Zero compliance / FDAAA / evaluative words in serialized output
    serialized = json.dumps([t_after, t_before, t_month, t_missing]).lower()
    forbidden_words = (
        "fdaaa",
        "compliant",
        "compliance",
        "non-compliant",
        "violation",
        "overdue",
        "late",
        "timely",
        "switching",
        "p-hacking",
        "spin",
    )
    for word in forbidden_words:
        assert not re.search(rf"\b{re.escape(word)}\b", serialized), f"Found forbidden word {word!r}"


# ---------------------------------------------------------------------------
# Group 6: TOC & Statistical Section Slicer Invariants
# ---------------------------------------------------------------------------


def _assert_exact_partition(result: di.SectionSliceResult, n_pages: int) -> None:
    assert result.physical_pages == n_pages
    assert result.pages_selected == sorted(set(result.pages_selected))
    assert result.pages_deferred == sorted(set(result.pages_deferred))
    assert result.pages_without_text == sorted(set(result.pages_without_text))
    sel = set(result.pages_selected)
    defd = set(result.pages_deferred)
    blank = set(result.pages_without_text)
    assert sel.isdisjoint(defd)
    assert sel.isdisjoint(blank)
    assert defd.isdisjoint(blank)
    assert sorted(sel | defd | blank) == list(range(1, n_pages + 1))


def test_select_statistical_pages_invariants() -> None:
    """Exact 1..N partition across empty, short, offset-TOC, bundled Prot_SAP, TOC-less, and memo PDFs."""
    # Compact page range lossless round-trip
    sample_pages = [1, 2, 3, 5, 7, 8, 9, 15, 20, 21]
    formatted = di.format_compact_page_ranges(sample_pages)
    assert formatted == ["1-3", "5", "7-9", "15", "20-21"]
    assert di.expand_compact_page_ranges(formatted) == sample_pages
    assert di.format_compact_page_ranges([]) == []
    assert di.expand_compact_page_ranges([]) == []
    with pytest.raises(di.DocumentIndexerError):
        di.expand_compact_page_ranges(["10-5"])
    with pytest.raises(di.DocumentIndexerError):
        di.format_compact_page_ranges([0])

    # 1. Empty PDF
    res_empty = di.select_statistical_pages([])
    _assert_exact_partition(res_empty, 0)
    assert res_empty.selection_strategy == "no_readable_pages"

    # 2. Short 15-page SAP -> pages_deferred == []
    res_short = di.select_statistical_pages(_clean_stat_pages(15), doc_type="sap")
    _assert_exact_partition(res_short, 15)
    assert res_short.pages_deferred == []
    assert res_short.selection_strategy == "full_readable_within_budget"

    # 3. 150-page protocol with +15 Roman-numeral front-matter offset (physical = printed + 15)
    pages_150: list[str] = [
        f"Front matter page {i}\nProtocol ABC-001 Version 3.0" for i in range(1, 16)
    ]
    pages_150[2] = (
        "Table of Contents\n"
        "1. Study Synopsis ........................ 1\n"
        "2. Objectives and Endpoints .............. 10\n"
        "3. Study Design .......................... 25\n"
        "4. Analysis Populations .................. 60\n"
        "5. Sample Size Determination ............. 70\n"
        "6. Interim Analysis ...................... 80\n"
        "7. Multiplicity Adjustment ............... 90\n"
        "8. Statistical Methods ................... 100\n"
        "9. Administrative Procedures ............. 115\n"
    )
    for printed in range(1, 136):
        pages_150.append(
            f"Routine protocol text on printed page {printed}.\nPage {printed} of 135"
        )
    # Place matching body headings at physical_page = printed + 15 (0-based index = printed + 14)
    heading_specs = {
        1: "1. Study Synopsis\nSummary of the randomized phase 3 trial.",
        10: "2. Objectives and Endpoints\nThe primary endpoint is progression-free survival.",
        60: "4. Analysis Populations\nThe intent-to-treat (ITT) and per-protocol populations.",
        70: "5. Sample Size Determination\nSample size of 400 patients gives 90% power.",
        80: "6. Interim Analysis\nOne interim analysis using Lan-DeMets O'Brien-Fleming stopping boundary.",
        90: "7. Multiplicity Adjustment\nHierarchical gatekeeping testing controls family-wise error.",
        100: "8. Statistical Methods\nPrimary analysis uses stratified log-rank test at two-sided alpha 0.05.",
    }
    for printed, body in heading_specs.items():
        phys = printed + 15
        pages_150[phys - 1] = f"{body}\nPage {printed} of 135"

    res_150 = di.select_statistical_pages(pages_150, doc_type="protocol")
    _assert_exact_partition(res_150, 150)
    assert res_150.has_toc is True
    assert res_150.printed_page_offset == 15
    assert res_150.selection_strategy == "toc_and_headings"
    assert 10 <= len(res_150.pages_selected) <= 25
    # Verify all 7 StatisticalSectionKind categories are detected and their start pages selected!
    detected_cats = {sec.category for sec in res_150.detected_sections}
    assert detected_cats == set(di.STATISTICAL_SECTION_KINDS)
    for printed in heading_specs:
        assert (printed + 15) in res_150.pages_selected

    # 4. Bundled Prot_SAP PDF with TWO separate TOCs (Protocol TOC p.2 + SAP TOC p.52)
    bundled = [f"Filler page {i} of combined document." for i in range(1, 91)]
    bundled[1] = (
        "Table of Contents\n"
        "1. Study Synopsis ................ 5\n"
        "2. Objectives and Endpoints ...... 12\n"
        "3. Study Procedures .............. 20\n"
    )
    bundled[4] = "1. Study Synopsis\nCombined protocol synopsis."
    bundled[11] = "2. Objectives and Endpoints\nPrimary outcome measure."
    bundled[51] = (
        "Table of Contents\n"
        "1. Analysis Populations .......... 10\n"
        "2. Sample Size ................... 15\n"
        "3. Statistical Methods ........... 20\n"
        "4. Multiplicity .................. 25\n"
    )
    # SAP printed page 10 -> physical page 65 (offset +55)
    bundled[64] = "1. Analysis Populations\nITT and FAS definitions.\nPage 10 of 35"
    bundled[69] = "2. Sample Size\nPower of 90% with 300 patients.\nPage 15 of 35"
    bundled[74] = "3. Statistical Methods\nMMRM primary analysis model.\nPage 20 of 35"
    bundled[79] = "4. Multiplicity\nHochberg adjustment procedure.\nPage 25 of 35"
    res_bundled = di.select_statistical_pages(bundled, doc_type="combined")
    _assert_exact_partition(res_bundled, 90)
    assert res_bundled.has_toc is True
    assert 2 in res_bundled.toc_pages and 52 in res_bundled.toc_pages
    assert 65 in res_bundled.pages_selected
    assert 75 in res_bundled.pages_selected

    # 5. TOC-less protocol (direct body headings + keywords)
    tocless = [f"Site logistics page {i} without statistical rules." for i in range(1, 41)]
    tocless[19] = "8. Statistical Methods\nTwo-sided alpha 0.05 superiority test with Cox proportional hazards."
    tocless[25] = "9. Sample Size\nSample size calculation for 85% power."
    res_tocless = di.select_statistical_pages(tocless, doc_type="protocol")
    _assert_exact_partition(res_tocless, 40)
    assert res_tocless.has_toc is False
    assert res_tocless.selection_strategy == "headings_and_keywords"
    assert 20 in res_tocless.pages_selected
    assert 26 in res_tocless.pages_selected

    # 6. Unstructured fallback memo (no headings or statistical keywords)
    memo = [f"Administrative logistics memorandum page {i} regarding clinic shipping." for i in range(1, 36)]
    res_memo = di.select_statistical_pages(memo, min_selected_pages=10, max_selected_pages=25)
    _assert_exact_partition(res_memo, 35)
    assert res_memo.selection_strategy == "fallback_leading_pages"
    assert res_memo.pages_selected == list(range(1, 11))


# ---------------------------------------------------------------------------
# Group 7: Providers, UTF-16 Surrogate Sanitization & macOS PDFKit Smoke Test
# ---------------------------------------------------------------------------


def test_providers_linux_and_macos(tmp_path: Path) -> None:
    """Exercise Fixture/Jsonl/Chained providers, UTF-16 surrogate sanitization, and macOS PDFKit."""
    # Unpaired UTF-16 surrogate sanitization -> U+FFFD valid UTF-8 JSON round-trip
    raw_with_surrogate = "Primary endpoint \ud800 text \udfff end"
    sanitized = di.sanitize_page_text(raw_with_surrogate)
    assert sanitized == "Primary endpoint \ufffd text \ufffd end"
    encoded = json.dumps({"text": sanitized}).encode("utf-8")
    assert json.loads(encoded.decode("utf-8"))["text"] == sanitized

    sha = "b" * 64
    fix = di.FixturePageTextProvider({sha: [raw_with_surrogate, "Page two text"]})
    assert isinstance(fix, di.PageTextProvider)
    assert isinstance(fix, di.DocumentPageExtractor)
    extracted = fix.extract_pages(sha256=sha)
    assert extracted.pages[0] == sanitized
    assert fix.get_page_text("NCT01234567", "Prot_000.pdf", sha, 1) == sanitized
    assert fix.get_page_text("NCT01234567", "Prot_000.pdf", sha, 2) == "Page two text"
    assert fix.get_page_text("NCT01234567", "Prot_000.pdf", sha, 3) is None

    # JsonlPageMapProvider (.jsonl.gz + Module 4 get_page_text parity)
    map_gz = tmp_path / "page_maps.jsonl.gz"
    with gzip.open(map_gz, "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "nct_id": "NCT01234567",
                    "filename": "SAP_001.pdf",
                    "sha256": sha,
                    "text_source": "pdfkit",
                    "pages": ["Alpha page one", "Beta page two"],
                }
            )
            + "\n"
        )
    jsonl_prov = di.JsonlPageMapProvider([map_gz])
    assert jsonl_prov.get_page_text("NCT01234567", "SAP_001.pdf", sha, 2) == "Beta page two"
    assert jsonl_prov.get_page_text("NCT01234567", "SAP_001.pdf", sha, 2, text_source="ocr") is None
    assert jsonl_prov.extract_pages(sha256=sha).provider_name == "jsonl_page_map"

    # ChainedPageTextProvider falls through and gives clear guidance when nothing matches
    chain = di.ChainedPageTextProvider([jsonl_prov, fix])
    assert chain.extract_pages(sha256=sha).provider_name == "jsonl_page_map"
    with pytest.raises(di.PageExtractionError, match="page-map-jsonl"):
        chain.extract_pages(sha256="c" * 64)

    # Conditional macOS PDFKit smoke test on minimal 1-page valid ASCII %PDF-1.4 fixture
    if sys.platform == "darwin":
        pdf_path = tmp_path / "smoke.pdf"
        pdf_bytes = _make_minimal_pdf("Statistical Analysis Plan Alpha 0.05")
        pdf_path.write_bytes(pdf_bytes)
        pdf_sha = di.sha256_bytes(pdf_bytes)

        # CLI helper mode (compiled without -fmodules)
        pdfkit_cli = di.MacOSPDFKitProvider(
            compile_dir=tmp_path / "pdfkit_bin",
            allow_ctypes_fallback=False,
        )
        res_cli = pdfkit_cli.extract_pages(sha256=pdf_sha, pdf_path=pdf_path)
        assert res_cli.provider_name == "macos_pdfkit_cli"
        assert res_cli.physical_pages == 1
        assert "Statistical Analysis Plan" in res_cli.pages[0]

        # Ctypes fallback mode
        pdfkit_ctypes = di.MacOSPDFKitProvider(prefer_ctypes=True)
        res_ctypes = pdfkit_ctypes.extract_pages(sha256=pdf_sha, pdf_path=pdf_path)
        assert res_ctypes.provider_name == "macos_pdfkit_ctypes"
        assert res_ctypes.physical_pages == 1
        assert "Statistical Analysis Plan" in res_ctypes.pages[0]


# ---------------------------------------------------------------------------
# Group 8: Legacy Census Adapter & Module 3/4 Profile Parity
# ---------------------------------------------------------------------------


def test_legacy_census_ingest_and_profile_parity(tmp_path: Path) -> None:
    """Offline legacy documents.jsonl ingestion preserves 12-key schema and Module 3 profile shape."""
    sha = "d" * 64
    census_jsonl = tmp_path / "documents.jsonl"
    census_row = {
        "nct_id": "NCT01543490",
        "filename": "Prot_000.pdf",
        "url": di.build_ctgov_large_doc_url("NCT01543490", "Prot_000.pdf"),
        "http_status": 200,
        "sha256": sha,
        "bytes": 4096,
        "doc_type": "protocol",
        "version_label": "Version 2.0",
        "version_date_as_printed": "15 May 2020",
        "physical_pages": 4,
        "pages_without_text": [],
        "retrieved_at": "2026-11-14T00:00:00Z",
    }
    census_jsonl.write_text(json.dumps(census_row) + "\n", encoding="utf-8")

    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")
    provider = di.FixturePageTextProvider({sha: _clean_stat_pages(4)})

    entries, profiles = di.ingest_legacy_census_documents(
        census_jsonl,
        cache_store=cache,
        provider=provider,
        extracted_at="2026-11-21T00:00:00+00:00",
        pipeline_commit="deadbeef",
    )
    assert len(entries) == 1
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["readability_summary"] == "machine_readable"
    assert profile["document_badge"] == {
        "readability_status": "machine_readable",
        "redaction_flags": [],
        "doc_count": 1,
        "total_physical_pages": 4,
    }
    # Legacy 12-key projection exact parity with Module 4 citation_verifier
    legacy_row = di.to_legacy_census_document_row(profile["documents"][0])
    assert tuple(legacy_row.keys()) == (
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
    # Cached pages accessible directly via DocumentCacheStore.get_page_text (Module 4 protocol)
    assert cache.get_page_text("NCT01543490", "Prot_000.pdf", sha, 1) is not None


def test_serialization_roundtrip_and_profile_text_guard() -> None:
    """DocumentIndexEntry round-trips losslessly and profile rejects embedded raw page arrays."""
    entry = di.index_document_pages(
        _clean_stat_pages(6),
        sha256="e" * 64,
        byte_count=24576,
        text_provider="fixture",
        doc_type="protocol",
        extracted_at="2026-11-21T01:02:03+00:00",
        pipeline_commit="abcdef1234567",
    )
    rehydrated = di.DocumentIndexEntry.from_dict(entry.to_dict())
    assert rehydrated == entry
    assert entry.version_label == "Protocol Version 2.0"
    assert entry.version_date_as_printed == "15 May 2020"

    bad_doc: dict[str, Any] = dict(
        di.build_trial_document_record(
            nct_id="NCT01543490",
            filename="Prot_000.pdf",
            doc_type="protocol",
            url=di.build_ctgov_large_doc_url("NCT01543490", "Prot_000.pdf"),
            http_status=200,
            entry=entry,
        )
    )
    bad_doc["page_texts"] = ["should never be embedded"]
    with pytest.raises(di.DocumentIndexerError, match="raw page text"):
        di.build_trial_document_profile(
            "NCT01543490",
            documents=[bad_doc],  # type: ignore[list-item]
        )


def test_cli_ingest_census_offline_and_non_darwin_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI offline census ingest, --offline guard, GlobalRateLimiter, and non-darwin PDFKit guard."""
    sha = "f" * 64
    census_jsonl = tmp_path / "documents.jsonl"
    census_jsonl.write_text(
        json.dumps(
            {
                "nct_id": "NCT02222222",
                "filename": "SAP_001.pdf",
                "url": di.build_ctgov_large_doc_url("NCT02222222", "SAP_001.pdf"),
                "http_status": 200,
                "sha256": sha,
                "bytes": 8192,
                "doc_type": "sap",
                "version_label": "",
                "version_date_as_printed": "",
                "physical_pages": 4,
                "pages_without_text": [],
                "retrieved_at": "2026-11-14T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    page_map = tmp_path / "pages.jsonl"
    page_map.write_text(
        json.dumps({"sha256": sha, "pages": _clean_stat_pages(4)}) + "\n",
        encoding="utf-8",
    )

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"data": [{"nct_id": "NCT02222222", "in_audit": False}]}),
        encoding="utf-8",
    )

    index_out = tmp_path / "index.jsonl"
    profiles_out = tmp_path / "profiles.jsonl"
    rc = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--ingest-census-jsonl",
            str(census_jsonl),
            "--page-map-jsonl",
            str(page_map),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--index-out",
            str(index_out),
            "--profiles-out",
            str(profiles_out),
            "--offline",
        ]
    )
    assert rc == 0
    assert index_out.exists()
    assert profiles_out.exists()
    written_profiles = [
        json.loads(line)
        for line in profiles_out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(written_profiles) == 1
    assert written_profiles[0]["nct_id"] == "NCT02222222"
    assert written_profiles[0]["readability_summary"] == "machine_readable"

    # --offline with live --nct-id (with valid in_audit=False manifest) returns exit 1 cleanly
    rc_offline_err = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--nct-id",
            "NCT02222222",
            "--offline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--index-out",
            str(index_out),
            "--profiles-out",
            str(profiles_out),
        ]
    )
    assert rc_offline_err == 1

    # Invalid --nct-id returns exit 1 cleanly instead of raising unhandled exception
    rc_bad_nct = cli_mod.run_cli(
        [
            "--nct-id",
            "NOT_AN_NCT_ID",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--index-out",
            str(index_out),
            "--profiles-out",
            str(profiles_out),
        ]
    )
    assert rc_bad_nct == 1

    # GlobalRateLimiter creates lock file cleanly
    lock_file = tmp_path / "cache" / ".ctgov_rate_limit.lock"
    limiter = cli_mod.GlobalRateLimiter(lock_file, min_interval_sec=0.01)
    limiter.wait()
    limiter.wait()
    assert lock_file.exists()

    # MacOSPDFKitProvider raises cleanly on non-darwin platforms
    monkeypatch.setattr(di.sys, "platform", "linux")
    pdfkit = di.MacOSPDFKitProvider(compile_dir=tmp_path)
    with pytest.raises(di.PageExtractionError, match="only available on macOS"):
        pdfkit.extract_pages(sha256=sha, pdf_path=tmp_path / "missing.pdf")


def test_load_manifest_rows_outcomes_beta_and_trials_shapes(tmp_path: Path) -> None:
    """load_manifest_rows and CLI accept live outcomes_beta index.json ('rows') and 'trials' manifests."""
    beta_manifest = tmp_path / "outcomes_beta_index.json"
    beta_manifest.write_text(
        json.dumps(
            {
                "run_id": "outcomes-2026-11-09-d41f32ec4d",
                "schema_version": 1,
                "rows": [
                    {
                        "nct_id": "NCT02222222",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "n_primary_outcomes": 2,
                    },
                    {
                        "nct_id": "NCT09999999",
                        "in_audit": True,
                        "protocol": True,
                        "sap": True,
                        "n_primary_outcomes": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    in_scope, audit_ids = cli_mod.load_manifest_rows(beta_manifest)
    assert [row["nct_id"] for row in in_scope] == ["NCT02222222"]
    assert in_scope[0]["n_primary_outcomes"] == 2
    assert audit_ids == frozenset({"NCT09999999"})

    # 'trials' wrapper key is also supported with strict in_audit separation.
    trials_manifest = tmp_path / "trials_manifest.json"
    trials_manifest.write_text(
        json.dumps(
            {
                "trials": [
                    {"nct_id": "NCT02222222", "in_audit": False},
                    {"nct_id": "NCT09999999", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    in_scope_t, audit_ids_t = cli_mod.load_manifest_rows(trials_manifest)
    assert [row["nct_id"] for row in in_scope_t] == ["NCT02222222"]
    assert audit_ids_t == frozenset({"NCT09999999"})

    # End-to-end offline CLI run with outcomes_beta index.json as --manifest.
    sha = "a" * 64
    census_jsonl = tmp_path / "documents.jsonl"
    census_jsonl.write_text(
        json.dumps(
            {
                "nct_id": "NCT02222222",
                "filename": "Prot_000.pdf",
                "url": di.build_ctgov_large_doc_url("NCT02222222", "Prot_000.pdf"),
                "http_status": 200,
                "sha256": sha,
                "bytes": 4096,
                "doc_type": "protocol",
                "version_label": "",
                "version_date_as_printed": "",
                "physical_pages": 4,
                "pages_without_text": [],
                "retrieved_at": "2026-11-14T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    page_map = tmp_path / "pages.jsonl"
    page_map.write_text(
        json.dumps({"sha256": sha, "pages": _clean_stat_pages(4)}) + "\n",
        encoding="utf-8",
    )
    index_out = tmp_path / "index.jsonl"
    profiles_out = tmp_path / "profiles.jsonl"

    rc_ok = cli_mod.run_cli(
        [
            "--manifest",
            str(beta_manifest),
            "--ingest-census-jsonl",
            str(census_jsonl),
            "--page-map-jsonl",
            str(page_map),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--index-out",
            str(index_out),
            "--profiles-out",
            str(profiles_out),
            "--offline",
        ]
    )
    assert rc_ok == 0
    profiles = [
        json.loads(line)
        for line in profiles_out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [p["nct_id"] for p in profiles] == ["NCT02222222"]

    # Holdout trial inside 'rows' still exits 2 when requested explicitly.
    rc_audit = cli_mod.run_cli(
        [
            "--manifest",
            str(beta_manifest),
            "--nct-id",
            "NCT09999999",
            "--cache-dir",
            str(tmp_path / "cache_audit"),
            "--index-out",
            str(tmp_path / "index_audit.jsonl"),
            "--profiles-out",
            str(tmp_path / "profiles_audit.jsonl"),
        ]
    )
    assert rc_audit == 2
