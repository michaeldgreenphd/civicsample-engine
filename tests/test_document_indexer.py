"""Deterministic offline pytest suite for Module 1:
src/document_indexer.py and scripts/index_trial_documents.py.

Runs without network access or external PDF binaries on both Linux CI and macOS.
"""

from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path
import re
import subprocess
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
    assert issubclass(di.InvalidNctIdError, di.DocumentIndexerError)

    # Direct guard checks (strict ASCII validated BEFORE audit_nct_ids check)
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT99000001", in_audit=True)
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit(
            "NCT99000001", in_audit=False, audit_nct_ids={"NCT99000001"}
        )
    with pytest.raises(di.InvalidNctIdError):
        di.enforce_not_in_audit(
            "nct99000001", in_audit=False, audit_nct_ids={"NCT99000001"}
        )
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT99000001", in_audit=None)
    with pytest.raises(di.AuditHoldoutError):
        di.enforce_not_in_audit("NCT99000001", in_audit=0)  # type: ignore[arg-type]

    # Spies must record ZERO calls when holdout fires
    pdf_bytes = _make_minimal_pdf("Protected audit trial")
    pdf_sha = di.sha256_bytes(pdf_bytes)
    nct = "NCT99000099"
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
        json.dumps({"data": [{"nct_id": "NCT99000011", "in_audit": None}]}),
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
                    {"nct_id": "NCT99000011", "in_audit": False},
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
    nct_in_scope = "NCT99000011"
    nct_audit = "NCT99000099"
    nct_unlisted = "NCT99000088"

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
        **kwargs: Any,
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
    nct_ok = "NCT99000015"
    nct_other = "NCT99000025"

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

    # 2. Row carrying non-bool in_audit=None, 0, or "false" raises AuditHoldoutError (even when allowed)
    for bad_val in (None, 0, "false"):
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

    # 2b. Row omitting in_audit when allowed_nct_ids is None raises AuditHoldoutError (no fallback to False)
    jsonl_missing_flag = tmp_path / "missing_in_audit.jsonl"
    jsonl_missing_flag.write_text(
        json.dumps(_census_row(nct_ok, sha_ok)) + "\n",
        encoding="utf-8",
    )
    index_mf = tmp_path / "idx_mf.jsonl"
    cache_mf = di.DocumentCacheStore(tmp_path / "cache_mf", index_mf)
    with pytest.raises(di.AuditHoldoutError):
        di.ingest_legacy_census_documents(
            jsonl_missing_flag,
            cache_store=cache_mf,
            provider=provider_spy,
            allowed_nct_ids=None,
        )
    assert provider_spy.calls == []
    assert cache_mf.all_entries() == []
    assert not index_mf.exists()

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

    nct_a = "NCT99000021"
    nct_b = "NCT99000022"
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
    study_empty = _make_study("NCT99000001", [])
    prof_empty = cli_mod.index_trial_from_study(study_empty, cache_store=cache, provider=None)
    assert prof_empty["readability_summary"] == "no_document"
    assert prof_empty["excluded_icf_count"] == 0
    assert prof_empty["documents"] == []

    study_icf_only = _make_study(
        "NCT99000002",
        [{"filename": "ICF_000.pdf", "hasProtocol": False, "hasSap": False, "hasIcf": True}],
    )
    prof_icf = cli_mod.index_trial_from_study(study_icf_only, cache_store=cache, provider=None)
    assert prof_icf["readability_summary"] == "no_document"
    assert prof_icf["excluded_icf_count"] == 1
    assert prof_icf["documents"] == []

    # 2. download_failed: HTTP 404, timeout, HTML 200 OK lacking %PDF-, 0-byte file
    nct_fail = "NCT99000003"
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

    # 3b. 91-page recurring-stamp-only scan
    stamp_pages = ["Approved CLI-CCD-2020-001 External Use"] * 81 + [""] * 10
    e_stamp_scan = di.index_document_pages(
        stamp_pages,
        sha256="2" * 64,
        byte_count=50000,
        text_provider="fixture",
    )
    assert e_stamp_scan.readability_status == "image_only_scan"
    assert e_stamp_scan.redaction.redaction_flags == ()

    # 3c. 1-page cover sheet + 105 blank scanned pages
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
    # 4a. Punctuation-shifted CMap
    punct_garbled = [
        "#:':/9:/)'24 !\"#$%&'()*+,-./:;<=>?@ [\\]^_`{|}~ #:':/9:/)'24 !\"#$%&'()*+,-./",
        "#:':/9:/)'24 ;:<=>?@!\"#$%&'()*+,-./ #:':/9:/)'24 !\"#$%&'()*+,-./:;<=>?@",
        "!\n!\n!\n!\n!\n!",  # repeated symbol pattern
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

    # 4b. Uppercase Caesar-shifted CMap ('DGPLQLVWUDWLRQ' model)
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

    # 5. partial_image_or_redacted: single blank page (p.49 model)
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

    # 7. extraction_failed: the PDF downloads (sha256 known) but no provider can
    # extract its pages, as on Linux with no page map.
    nct_noext = "NCT99000006"
    noext_bytes = _make_minimal_pdf("Protocol that cannot be extracted here")
    noext_sha = di.sha256_bytes(noext_bytes)
    u_noext = di.build_ctgov_large_doc_url(nct_noext, "Prot_000.pdf")
    prof_noext = cli_mod.index_trial_from_study(
        _make_study(
            nct_noext,
            [{"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False}],
        ),
        cache_store=cache,
        provider=di.FixturePageTextProvider(
            {noext_sha: di.PageExtractionError("no provider on this platform")}
        ),
        session=_FakeSession({u_noext: _FakeResponse(200, noext_bytes)}),
    )
    assert prof_noext["readability_summary"] == "extraction_failed"

    # All 7 statuses verified disjoint
    observed = {
        prof_empty["readability_summary"],
        prof_fail["readability_summary"],
        prof_noext["readability_summary"],
        e_pure_scan.readability_status,
        e_punct.readability_status,
        e_partial_blank.readability_status,
        e_clean.readability_status,
    }
    assert observed == set(di.READABILITY_STATUSES)

    # Mixed trial-level summarization rules
    rec_clean = di.build_trial_document_record(
        nct_id="NCT99000004",
        filename="Prot_000.pdf",
        doc_type="protocol",
        url=di.build_ctgov_large_doc_url("NCT99000004", "Prot_000.pdf"),
        http_status=200,
        entry=e_clean,
    )
    rec_scan = di.build_trial_document_record(
        nct_id="NCT99000004",
        filename="SAP_001.pdf",
        doc_type="sap",
        url=di.build_ctgov_large_doc_url("NCT99000004", "SAP_001.pdf"),
        http_status=200,
        entry=e_pure_scan,
    )
    rec_cmap = di.build_trial_document_record(
        nct_id="NCT99000004",
        filename="SAP_002.pdf",
        doc_type="sap",
        url=di.build_ctgov_large_doc_url("NCT99000004", "SAP_002.pdf"),
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

    # Running-header-only white-box pages
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

    # Orphaned statistical headings
    pages_orphan = [
        "Study Protocol Overview for randomized patients.",
        "7. Analysis Populations\n7.1 Intent-to-Treat\n7.2 Modified Intent-to-Treat",
        "9.1.3\n9.1.4 Analysis Populations",
    ]
    r_orphan = di.detect_redactions(pages_orphan)
    assert r_orphan.pages_orphaned_stat_headings == (2, 3)
    assert "white_box_or_blank_stats" in r_orphan.redaction_flags

    # TOC-mapped blank statistical pages
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
    assert fix.get_page_text("NCT99000005", "Prot_000.pdf", sha, 1) == sanitized
    assert fix.get_page_text("NCT99000005", "Prot_000.pdf", sha, 2) == "Page two text"
    assert fix.get_page_text("NCT99000005", "Prot_000.pdf", sha, 3) is None

    # JsonlPageMapProvider (.jsonl.gz + Module 4 get_page_text parity)
    map_gz = tmp_path / "page_maps.jsonl.gz"
    with gzip.open(map_gz, "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "nct_id": "NCT99000005",
                    "filename": "SAP_001.pdf",
                    "sha256": sha,
                    "text_source": "pdfkit",
                    "pages": ["Alpha page one", "Beta page two"],
                }
            )
            + "\n"
        )
    jsonl_prov = di.JsonlPageMapProvider([map_gz])
    assert jsonl_prov.get_page_text("NCT99000005", "SAP_001.pdf", sha, 2) == "Beta page two"
    assert jsonl_prov.get_page_text("NCT99000005", "SAP_001.pdf", sha, 2, text_source="ocr") is None
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
    """Offline legacy documents.jsonl ingestion enforces allowlist/in_audit and emits F6 provenance."""
    sha = "d" * 64
    census_jsonl = tmp_path / "documents.jsonl"
    census_row = {
        "nct_id": "NCT99000015",
        "filename": "Prot_000.pdf",
        "url": di.build_ctgov_large_doc_url("NCT99000015", "Prot_000.pdf"),
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
        allowed_nct_ids={"NCT99000015"},
        audit_nct_ids={"NCT99000099"},
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
    # 14-key projection exact parity with Module 4 F6 provenance fields
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
        "model_id",
        "profile_version",
    )
    assert legacy_row["model_id"] == "deterministic_indexer"
    assert legacy_row["profile_version"] == di.INDEXER_RULES_VERSION

    custom_row = di.to_legacy_census_document_row(
        profile["documents"][0],
        model_id="custom_indexer_v2",
        profile_version="doc_indexer@2026-12-01.v2",
    )
    assert custom_row["model_id"] == "custom_indexer_v2"
    assert custom_row["profile_version"] == "doc_indexer@2026-12-01.v2"
    with pytest.raises(di.DocumentIndexerError):
        di.to_legacy_census_document_row(profile["documents"][0], model_id="")
    with pytest.raises(di.DocumentIndexerError):
        di.to_legacy_census_document_row(profile["documents"][0], profile_version="   ")

    # Cached pages accessible directly via DocumentCacheStore.get_page_text (Module 4 protocol)
    assert cache.get_page_text("NCT99000015", "Prot_000.pdf", sha, 1) is not None


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
            nct_id="NCT99000015",
            filename="Prot_000.pdf",
            doc_type="protocol",
            url=di.build_ctgov_large_doc_url("NCT99000015", "Prot_000.pdf"),
            http_status=200,
            entry=entry,
        )
    )
    bad_doc["page_texts"] = ["should never be embedded"]
    with pytest.raises(di.DocumentIndexerError, match="raw page text"):
        di.build_trial_document_profile(
            "NCT99000015",
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
                "nct_id": "NCT99000022",
                "filename": "SAP_001.pdf",
                "url": di.build_ctgov_large_doc_url("NCT99000022", "SAP_001.pdf"),
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
        # Under the CLI firewall every page-map row must name its trial.
        json.dumps({"nct_id": "NCT99000022", "sha256": sha, "pages": _clean_stat_pages(4)})
        + "\n",
        encoding="utf-8",
    )

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"data": [{"nct_id": "NCT99000022", "in_audit": False}]}),
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
    assert written_profiles[0]["nct_id"] == "NCT99000022"
    assert written_profiles[0]["readability_summary"] == "machine_readable"

    # --offline with live --nct-id (with valid in_audit=False manifest) returns exit 1 cleanly
    rc_offline_err = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--nct-id",
            "NCT99000022",
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
                        "nct_id": "NCT99000022",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "n_primary_outcomes": 2,
                    },
                    {
                        "nct_id": "NCT99000099",
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
    assert [row["nct_id"] for row in in_scope] == ["NCT99000022"]
    assert in_scope[0]["n_primary_outcomes"] == 2
    assert audit_ids == frozenset({"NCT99000099"})

    # 'trials' wrapper key is also supported with strict in_audit separation.
    trials_manifest = tmp_path / "trials_manifest.json"
    trials_manifest.write_text(
        json.dumps(
            {
                "trials": [
                    {"nct_id": "NCT99000022", "in_audit": False},
                    {"nct_id": "NCT99000099", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    in_scope_t, audit_ids_t = cli_mod.load_manifest_rows(trials_manifest)
    assert [row["nct_id"] for row in in_scope_t] == ["NCT99000022"]
    assert audit_ids_t == frozenset({"NCT99000099"})

    # End-to-end offline CLI run with outcomes_beta index.json as --manifest.
    sha = "a" * 64
    census_jsonl = tmp_path / "documents.jsonl"
    census_jsonl.write_text(
        json.dumps(
            {
                "nct_id": "NCT99000022",
                "filename": "Prot_000.pdf",
                "url": di.build_ctgov_large_doc_url("NCT99000022", "Prot_000.pdf"),
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
        # Under the CLI firewall every page-map row must name its trial.
        json.dumps({"nct_id": "NCT99000022", "sha256": sha, "pages": _clean_stat_pages(4)})
        + "\n",
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
    assert [p["nct_id"] for p in profiles] == ["NCT99000022"]

    # Holdout trial inside 'rows' still exits 2 when requested explicitly.
    rc_audit = cli_mod.run_cli(
        [
            "--manifest",
            str(beta_manifest),
            "--nct-id",
            "NCT99000099",
            "--cache-dir",
            str(tmp_path / "cache_audit"),
            "--index-out",
            str(tmp_path / "index_audit.jsonl"),
            "--profiles-out",
            str(tmp_path / "profiles_audit.jsonl"),
        ]
    )
    assert rc_audit == 2


# ---------------------------------------------------------------------------
# Group 9: D1, D2, D3, F1, F2, JsonlPageMapProvider & Module Boundary Guards
# ---------------------------------------------------------------------------


def test_d1_cli_no_target_args_exits_2_without_touching_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D1: run_cli without target args prints usage to stderr, exits 2, and leaves files untouched."""
    sentinel_profiles = b'{"nct_id":"NCT99000001","sentinel":true}\n'
    sentinel_index = b'{"sha256":"0000","sentinel":true}\n'
    profiles_path = tmp_path / "profiles.jsonl"
    index_path = tmp_path / "index.jsonl"
    cache_dir = tmp_path / "cache_untouched"

    profiles_path.write_bytes(sentinel_profiles)
    index_path.write_bytes(sentinel_index)

    rc = cli_mod.run_cli(
        [
            "--profiles-out",
            str(profiles_path),
            "--index-out",
            str(index_path),
            "--cache-dir",
            str(cache_dir),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage:" in captured.err.lower()
    assert profiles_path.read_bytes() == sentinel_profiles
    assert index_path.read_bytes() == sentinel_index
    assert not cache_dir.exists()

    # Bare run_cli([]) in clean cwd creates zero files or directories
    empty_cwd = tmp_path / "empty_cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    rc_bare = cli_mod.run_cli([])
    captured_bare = capsys.readouterr()
    assert rc_bare == 2
    assert "usage:" in captured_bare.err.lower()
    assert list(empty_cwd.iterdir()) == []

    # Subprocess execution of scripts/index_trial_documents.py with only --profiles-out
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "index_trial_documents.py"
    proc = subprocess.run(
        [sys.executable, str(script_path), "--profiles-out", str(profiles_path)],
        cwd=str(empty_cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()
    assert profiles_path.read_bytes() == sentinel_profiles
    assert list(empty_cwd.iterdir()) == []


def test_d2_study_payload_nct_id_validation_and_allowlist_zero_cdn_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2: study payload nctId is checked against strict ASCII, requested nct_id, audit, and allowlist."""
    pdf_bytes = _make_minimal_pdf("Protocol content")
    pdf_sha = di.sha256_bytes(pdf_bytes)
    nct_allowed = "NCT99000001"
    nct_second_allowed = "NCT99000002"
    nct_audit = "NCT99000098"
    nct_unlisted = "NCT99000099"

    study_unlisted = _make_study(
        nct_unlisted,
        [
            {
                "filename": "Prot_000.pdf",
                "hasProtocol": True,
                "hasSap": False,
                "size": len(pdf_bytes),
                "uploadDate": "08/21/2020 14:32",
            }
        ],
    )
    url_unlisted = di.build_ctgov_large_doc_url(nct_unlisted, "Prot_000.pdf")
    http_spy = _FakeSession({url_unlisted: _FakeResponse(200, pdf_bytes)})
    provider_spy = di.FixturePageTextProvider({pdf_sha: _clean_stat_pages(2)})
    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")

    cache_lookup_calls: list[str] = []
    orig_skip = cache.can_skip_download

    def _spy_skip(url: str, *, api_size: int | None, api_upload_date: str | None) -> str | None:
        cache_lookup_calls.append(url)
        return orig_skip(url, api_size=api_size, api_upload_date=api_upload_date)

    monkeypatch.setattr(cache, "can_skip_download", _spy_skip)

    # 1. Direct index_trial_from_study with unlisted NCT99000099 -> AuditHoldoutError, 0 CDN requests, 0 cache lookups
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.index_trial_from_study(
            study_unlisted,
            cache_store=cache,
            provider=provider_spy,
            session=http_spy,
            audit_nct_ids={nct_audit},
            allowed_nct_ids={nct_allowed},
        )
    assert http_spy.requested_urls == []
    assert cache_lookup_calls == []
    assert provider_spy.calls == []

    # 2. Mocked API returning unlisted NCT99000099 when NCT99000001 was requested -> AuditHoldoutError, 0 CDN requests
    spoofing_api = _SpyAPIClient({nct_allowed: study_unlisted})
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.fetch_and_index_trial(
            nct_allowed,
            api_client=spoofing_api,
            cache_store=cache,
            provider=provider_spy,
            session=http_spy,
            audit_nct_ids={nct_audit},
            allowed_nct_ids={nct_allowed},
        )
    assert spoofing_api.requested_ncts == [nct_allowed]
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []

    # 3. Same mocked API through run_cli -> exit 2, 0 CDN requests, 0 output files
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "data": [
                    {"nct_id": nct_allowed, "in_audit": False},
                    {"nct_id": nct_audit, "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    profiles_out = tmp_path / "profiles_spoof.jsonl"
    rc_spoof = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest),
            "--nct-id",
            nct_allowed,
            "--cache-dir",
            str(tmp_path / "cache_spoof"),
            "--index-out",
            str(tmp_path / "index_spoof.jsonl"),
            "--profiles-out",
            str(profiles_out),
        ],
        api_client=spoofing_api,
        session=http_spy,
        provider=provider_spy,
    )
    assert rc_spoof == 2
    assert http_spy.requested_urls == []
    assert provider_spy.calls == []
    assert not profiles_out.exists()

    # 4. Payload mismatch even when returned NCT99000002 is in allowed_nct_ids -> AuditHoldoutError
    study_second = _make_study(
        nct_second_allowed,
        [{"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False, "size": len(pdf_bytes)}],
    )
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.index_trial_from_study(
            study_second,
            cache_store=cache,
            provider=provider_spy,
            session=http_spy,
            nct_id=nct_allowed,
            allowed_nct_ids={nct_allowed, nct_second_allowed},
        )
    assert http_spy.requested_urls == []

    # 5. Payload returning malformed nctId (lowercase, trailing space, zero-width, non-str) -> InvalidNctIdError
    for bad_payload_nct in (
        "nct99000001",
        "NCT99000001 ",
        "NCT99000001\u200b",
        "NCT٩٩٠٠٠٠٠١",
        None,
        99000001,
    ):
        bad_study = _make_study("NCT99000001", [])
        bad_study["protocolSection"]["identificationModule"]["nctId"] = bad_payload_nct
        with pytest.raises(di.InvalidNctIdError):
            cli_mod.index_trial_from_study(
                bad_study,
                cache_store=cache,
                provider=provider_spy,
                session=http_spy,
                audit_nct_ids={nct_audit},
                allowed_nct_ids={nct_allowed},
            )
    assert http_spy.requested_urls == []


def test_d3_dependency_disclosure_no_stdlib_claims() -> None:
    """D3: zero 'stdlib-only'/'100% Python stdlib' claims; explicit dependency disclosure."""
    repo_root = Path(__file__).resolve().parent.parent
    lib_src = (repo_root / "src" / "document_indexer.py").read_text(encoding="utf-8")
    cli_src = (repo_root / "scripts" / "index_trial_documents.py").read_text(encoding="utf-8")

    for path_label, content in (("src/document_indexer.py", lib_src), ("scripts/index_trial_documents.py", cli_src)):
        low = content.lower()
        assert "100% python" not in low, f"Forbidden stdlib claim in {path_label}"
        assert "stdlib-only" not in low, f"Forbidden stdlib-only claim in {path_label}"
        assert "bare stdlib" not in low, f"Forbidden bare stdlib claim in {path_label}"

    help_text = cli_mod.build_arg_parser().format_help()
    for doc_name, text in (
        ("src/document_indexer.py docstring", di.__doc__ or ""),
        ("scripts/index_trial_documents.py docstring", cli_mod.__doc__ or ""),
        ("CLI --help", help_text),
    ):
        for required_term in (
            "requests",
            "tqdm",
            "requirements.txt",
            "MacOSPDFKitProvider",
            "clang",
            "Foundation",
            "PDFKit",
            "libobjc.A.dylib",
            "PageExtractionError",
        ):
            assert required_term in text, f"Missing {required_term!r} in {doc_name}"


def test_f1_f2_strict_ascii_nct_and_firewall_hardening(tmp_path: Path) -> None:
    """F1/F2: strict ASCII ^NCT[0-9]{8}$ before audit check, manifest empty-audit guard, JsonlPageMapProvider."""

    class _StrSub(str):
        pass

    invalid_ids: tuple[Any, ...] = (
        "nct99000001",
        "NCT99000001 ",
        " NCT99000001",
        "NCT99000001\n",
        "NCT99000001\u200b",
        "NCT٩٩٠٠٠٠٠١",
        "",
        None,
        99000001,
        True,
        _StrSub("NCT99000001"),
    )

    for bad in invalid_ids:
        with pytest.raises(di.InvalidNctIdError):
            di.validate_nct_id(bad)
        # Fires InvalidNctIdError BEFORE AuditHoldoutError even when NCT99000001 is in audit_nct_ids
        with pytest.raises(di.InvalidNctIdError):
            di.enforce_not_in_audit(
                bad,
                in_audit=True,
                audit_nct_ids={"NCT99000001"},
            )

    # Empty audit_nct_ids refused when manifest contains in_audit: true rows
    manifest_with_audit = tmp_path / "manifest_with_audit.json"
    manifest_with_audit.write_text(
        json.dumps(
            {
                "data": [
                    {"nct_id": "NCT99000001", "in_audit": False},
                    {"nct_id": "NCT99000099", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.load_manifest_rows(manifest_with_audit, audit_nct_ids=frozenset())
    with pytest.raises(di.AuditHoldoutError):
        cli_mod.load_manifest_rows(manifest_with_audit, audit_nct_ids={"NCT99000098"})

    # Manifest missing in_audit or non-bool in_audit or malformed nct_id refused
    for idx, bad_row in enumerate(
        (
            {"nct_id": "NCT99000001"},
            {"nct_id": "NCT99000001", "in_audit": "false"},
            {"nct_id": "NCT99000001", "in_audit": 0},
        )
    ):
        p_bad = tmp_path / f"bad_manifest_{idx}.json"
        p_bad.write_text(json.dumps([bad_row]), encoding="utf-8")
        with pytest.raises(di.AuditHoldoutError):
            cli_mod.load_manifest_rows(p_bad)

    p_bad_nct = tmp_path / "bad_manifest_nct.json"
    p_bad_nct.write_text(
        json.dumps([{"nct_id": "nct99000001", "in_audit": True}]),
        encoding="utf-8",
    )
    with pytest.raises(di.InvalidNctIdError):
        cli_mod.load_manifest_rows(p_bad_nct)

    # JsonlPageMapProvider enforces audit_nct_ids, allowed_nct_ids, strict nct_id, and in_audit
    sha_in = "1" * 64
    sha_audit = "2" * 64

    # (a) Audit nct_id row inside page-map JSONL refused before caching any row
    pm_audit = tmp_path / "pm_audit.jsonl"
    pm_audit.write_text(
        json.dumps(
            {
                "nct_id": "NCT99000001",
                "filename": "Prot_000.pdf",
                "sha256": sha_in,
                "pages": ["Clean page"],
                "in_audit": False,
            }
        )
        + "\n"
        + json.dumps(
            {
                "nct_id": "NCT99000099",
                "filename": "Prot_000.pdf",
                "sha256": sha_audit,
                "pages": ["Audit page"],
                "in_audit": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    prov_audit = di.JsonlPageMapProvider(
        [pm_audit],
        audit_nct_ids={"NCT99000099"},
        allowed_nct_ids={"NCT99000001"},
    )
    with pytest.raises(di.AuditHoldoutError):
        prov_audit.extract_pages(sha256=sha_in, nct_id="NCT99000001", filename="Prot_000.pdf")
    assert prov_audit._by_sha_source == {}

    # (b) Unlisted nct_id row inside page-map JSONL refused
    pm_unlisted = tmp_path / "pm_unlisted.jsonl"
    pm_unlisted.write_text(
        json.dumps(
            {
                "nct_id": "NCT99000088",
                "filename": "Prot_000.pdf",
                "sha256": sha_in,
                "pages": ["Unlisted page"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    prov_unlisted = di.JsonlPageMapProvider(
        [pm_unlisted],
        audit_nct_ids={"NCT99000099"},
        allowed_nct_ids={"NCT99000001"},
    )
    with pytest.raises(di.AuditHoldoutError):
        prov_unlisted.get_page_text("NCT99000001", "Prot_000.pdf", sha_in, 1)

    # (c) Row with in_audit=True or non-bool refused even without nct_id
    for bad_flag in (True, None, 0, "false"):
        pm_flag = tmp_path / f"pm_flag_{bad_flag}.jsonl"
        pm_flag.write_text(
            json.dumps({"sha256": sha_in, "pages": ["Page"], "in_audit": bad_flag}) + "\n",
            encoding="utf-8",
        )
        prov_flag = di.JsonlPageMapProvider(
            [pm_flag],
            audit_nct_ids={"NCT99000099"},
            allowed_nct_ids={"NCT99000001"},
        )
        with pytest.raises(di.AuditHoldoutError):
            prov_flag.extract_pages(sha256=sha_in)

    # (d) CLI wiring: --page-map-jsonl containing audit row exits 2 with zero outputs
    census_ok = tmp_path / "census_ok.jsonl"
    census_ok.write_text(
        json.dumps(
            {
                "nct_id": "NCT99000001",
                "filename": "Prot_000.pdf",
                "url": di.build_ctgov_large_doc_url("NCT99000001", "Prot_000.pdf"),
                "http_status": 200,
                "sha256": sha_in,
                "bytes": 4096,
                "doc_type": "protocol",
                "version_label": "",
                "version_date_as_printed": "",
                "physical_pages": 1,
                "pages_without_text": [],
                "retrieved_at": "2026-11-14T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_prof = tmp_path / "cli_pm_profiles.jsonl"
    out_idx = tmp_path / "cli_pm_index.jsonl"
    rc_pm = cli_mod.run_cli(
        [
            "--manifest",
            str(manifest_with_audit),
            "--ingest-census-jsonl",
            str(census_ok),
            "--page-map-jsonl",
            str(pm_audit),
            "--cache-dir",
            str(tmp_path / "cli_pm_cache"),
            "--index-out",
            str(out_idx),
            "--profiles-out",
            str(out_prof),
            "--offline",
        ]
    )
    assert rc_pm == 2
    assert not out_prof.exists()
    assert not out_idx.exists()


def test_fictional_nct99_ids_only_and_zero_cross_module_imports() -> None:
    """Enforce NCT99000001..NCT99000099 test ID rule and zero imports from M2, M3, or M4."""
    allowed_test_ncts = {f"NCT990000{i:02d}" for i in range(1, 100)}
    test_source = Path(__file__).read_text(encoding="utf-8")
    found_ncts = set(re.findall(r"NCT[0-9]{8}", test_source))
    assert found_ncts, "Expected fictional NCT990000xx IDs in test suite"
    disallowed = sorted(found_ncts - allowed_test_ncts)
    assert disallowed == [], f"Found non-fictional NCT IDs in test_document_indexer.py: {disallowed}"

    repo_root = Path(__file__).resolve().parent.parent
    m1_files = (
        repo_root / "src" / "document_indexer.py",
        repo_root / "scripts" / "index_trial_documents.py",
        Path(__file__).resolve(),
    )
    forbidden_modules = {
        "analysis_population_parser",
        "src.analysis_population_parser",
        "ecosystem",
        "sponsors.ecosystem",
        "outcomes_layout",
        "src.outcomes_layout",
        "build_outcomes_shards",
        "scripts.build_outcomes_shards",
        "citation_verifier",
        "src.citation_verifier",
        "longitudinal_provenance",
        "src.longitudinal_provenance",
        "verify_and_plan_census_batch",
        "scripts.verify_and_plan_census_batch",
    }
    for py_path in m1_files:
        src_text = py_path.read_text(encoding="utf-8")
        if py_path != Path(__file__).resolve():
            assert re.findall(r"NCT[0-9]{8}", src_text) == [], (
                f"Unexpected literal NCT ID in {py_path.name}"
            )
        tree = ast.parse(src_text, filename=str(py_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, (
                        f"Forbidden cross-module import {alias.name!r} in {py_path.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden_modules, (
                    f"Forbidden cross-module from-import {mod!r} in {py_path.name}"
                )
                for alias in node.names:
                    combined = f"{mod}.{alias.name}".strip(".")
                    assert alias.name not in forbidden_modules and combined not in forbidden_modules, (
                        f"Forbidden cross-module import {combined!r} in {py_path.name}"
                    )


# ---------------------------------------------------------------------------
# Group 10: Fixes from the audit of be5f0c7
# ---------------------------------------------------------------------------

_AUDIT_NCT = "NCT99000099"
_ALLOWED_NCT = "NCT99000031"


def _write_jsonl(path: Path, rows: Sequence[Any]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _write_cached_map(pages_dir: Path, payload: dict[str, Any]) -> Path:
    sha = payload["sha256"]
    path = pages_dir / sha[:2] / f"{sha}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(di.encode_gz_json(payload))
    return path


def _sentinel(path: Path) -> bytes:
    data = b'{"nct_id":"NCT99000001","sentinel":true}\n'
    path.write_bytes(data)
    return data


def test_page_map_audit_tag_refused_without_allowlist(tmp_path: Path) -> None:
    """The audit-id check on page maps holds on its own, with no allowlist to back it up."""
    sha = "a" * 64
    pm = _write_jsonl(
        tmp_path / "pm.jsonl",
        [{"nct_id": _AUDIT_NCT, "filename": "Prot_000.pdf", "sha256": sha, "pages": ["Audit page"]}],
    )
    prov = di.JsonlPageMapProvider([pm], audit_nct_ids={_AUDIT_NCT}, allowed_nct_ids=None)
    with pytest.raises(di.AuditHoldoutError):
        prov.validate_loaded()
    assert prov._by_sha_source == {}
    with pytest.raises(di.AuditHoldoutError):
        prov.extract_pages(sha256=sha)

    # The same check guards cached maps, through the provider and the store.
    pages_dir = tmp_path / "cache" / "pages"
    _write_cached_map(
        pages_dir,
        {"sha256": sha, "byte_count": 10, "physical_pages": 1, "nct_id": _AUDIT_NCT, "pages": ["Audit page"]},
    )
    prov_cache = di.JsonlPageMapProvider(
        cache_pages_dir=pages_dir, audit_nct_ids={_AUDIT_NCT}, allowed_nct_ids=None
    )
    with pytest.raises(di.AuditHoldoutError):
        prov_cache.extract_pages(sha256=sha)
    assert prov_cache._by_sha_source == {}
    store = di.DocumentCacheStore(
        tmp_path / "cache", tmp_path / "index.jsonl", audit_nct_ids={_AUDIT_NCT}
    )
    with pytest.raises(di.AuditHoldoutError):
        store.load_cached_pages(sha)


def test_page_map_untagged_rows_refused_whenever_firewall_configured(tmp_path: Path) -> None:
    """A page map naming no trial is refused under a holdout set or allowlist, and served only with neither."""
    sha = "b" * 64
    pm = _write_jsonl(tmp_path / "pm.jsonl", [{"sha256": sha, "pages": ["Untagged page"]}])

    for audit, allowed in (({_AUDIT_NCT}, None), (frozenset(), {_ALLOWED_NCT}), ({_AUDIT_NCT}, {_ALLOWED_NCT})):
        prov = di.JsonlPageMapProvider([pm], audit_nct_ids=audit, allowed_nct_ids=allowed)
        with pytest.raises(di.AuditHoldoutError, match="names no nct_id"):
            prov.validate_loaded()
        assert prov._by_sha_source == {}
    # Not served to an allowed trial's lookup either.
    prov_allowed = di.JsonlPageMapProvider([pm], allowed_nct_ids={_ALLOWED_NCT})
    with pytest.raises(di.AuditHoldoutError):
        prov_allowed.get_page_text(_ALLOWED_NCT, "Prot_000.pdf", sha, 1)

    # With no firewall configured (library use) an untagged map still loads.
    prov_open = di.JsonlPageMapProvider([pm])
    assert prov_open.extract_pages(sha256=sha).pages == ("Untagged page",)

    # in_audit is checked on its own: an untagged row passes the unconfigured
    # firewall, so only the in_audit check can refuse it.
    for bad_flag in (True, None, 0, "false"):
        pm_flag = _write_jsonl(
            tmp_path / f"pm_flag_{bad_flag}.jsonl",
            [{"sha256": sha, "pages": ["Page"], "in_audit": bad_flag}],
        )
        with pytest.raises(di.AuditHoldoutError):
            di.JsonlPageMapProvider([pm_flag]).validate_loaded()

    # Under an allowlist a lookup must name its trial, even for a tagged row.
    pm_tagged = _write_jsonl(
        tmp_path / "pm_tagged.jsonl",
        [{"nct_id": _ALLOWED_NCT, "filename": "Prot_000.pdf", "sha256": sha, "pages": ["Tagged page"]}],
    )
    prov_tagged = di.JsonlPageMapProvider([pm_tagged], allowed_nct_ids={_ALLOWED_NCT})
    with pytest.raises(di.AuditHoldoutError, match="Explicit nct_id"):
        prov_tagged.extract_pages(sha256=sha)
    assert prov_tagged.extract_pages(sha256=sha, nct_id=_ALLOWED_NCT).pages == ("Tagged page",)
    assert prov_tagged.get_page_text(_ALLOWED_NCT, "Prot_000.pdf", sha, 1) == "Tagged page"

    # Cached maps: an untagged map is refused by a configured store and provider.
    cache_dir = tmp_path / "cache"
    open_store = di.DocumentCacheStore(cache_dir, tmp_path / "index.jsonl")
    open_store.save_cached_pages(sha, byte_count=100, pages=["Cached untagged"])
    assert open_store.load_cached_pages(sha) is not None
    scoped_store = di.DocumentCacheStore(
        cache_dir, tmp_path / "index.jsonl", allowed_nct_ids={_ALLOWED_NCT}
    )
    with pytest.raises(di.AuditHoldoutError, match="names no nct_id"):
        scoped_store.load_cached_pages(sha)
    with pytest.raises(di.AuditHoldoutError):
        scoped_store.get_page_text(_ALLOWED_NCT, "Prot_000.pdf", sha, 1)
    with pytest.raises(di.AuditHoldoutError, match="names no nct_id"):
        di.JsonlPageMapProvider(
            cache_pages_dir=cache_dir / "pages", audit_nct_ids={_AUDIT_NCT}
        ).extract_pages(sha256=sha)

    # Maps the store writes for a trial carry that trial's tag, so a configured
    # store can reuse them.
    tagged_sha = "c" * 64
    provider = di.FixturePageTextProvider({tagged_sha: _clean_stat_pages(2)})
    scoped_store.get_or_index_document(
        sha256=tagged_sha,
        byte_count=200,
        provider=provider,
        nct_id=_ALLOWED_NCT,
        filename="Prot_000.pdf",
    )
    with gzip.open(scoped_store.page_map_path(tagged_sha), "rt", encoding="utf-8") as handle:
        assert json.load(handle)["nct_id"] == _ALLOWED_NCT
    reopened = di.DocumentCacheStore(
        cache_dir, tmp_path / "index2.jsonl", audit_nct_ids={_AUDIT_NCT}, allowed_nct_ids={_ALLOWED_NCT}
    )
    assert reopened.load_cached_pages(tagged_sha) is not None
    # The store never writes a map tagged with a holdout trial.
    with pytest.raises(di.AuditHoldoutError):
        di.DocumentCacheStore(
            cache_dir, tmp_path / "index3.jsonl", audit_nct_ids={_AUDIT_NCT}
        ).save_cached_pages("d" * 64, byte_count=1, pages=["x"], nct_id=_AUDIT_NCT)
    assert not (cache_dir / "pages" / "dd").exists()


def test_page_map_provider_strict_loading(tmp_path: Path) -> None:
    """A missing page-map file and malformed rows raise; nothing is skipped silently."""
    sha = "e" * 64
    good = {"nct_id": _ALLOWED_NCT, "sha256": sha, "pages": ["One", "Two"]}

    with pytest.raises(di.DocumentIndexerError, match="does not exist") as missing:
        di.JsonlPageMapProvider([tmp_path / "typo.jsonl"]).validate_loaded()
    # Not a PageExtractionError, which callers turn into a per-document record.
    assert not isinstance(missing.value, di.PageExtractionError)

    bad_rows: tuple[tuple[str, Any, type[Exception]], ...] = (
        ("not_json", "{not json", di.DocumentIndexerError),
        ("not_object", [1, 2], di.DocumentIndexerError),
        ("bad_sha", {**good, "sha256": "xyz"}, di.CacheIntegrityError),
        ("no_sha", {"nct_id": _ALLOWED_NCT, "pages": ["One"]}, di.CacheIntegrityError),
        ("pages_str", {**good, "pages": "One"}, di.CacheIntegrityError),
        ("pages_null_page", {**good, "pages": ["One", None]}, di.CacheIntegrityError),
        ("bad_source", {**good, "text_source": 7}, di.CacheIntegrityError),
        ("bad_filename", {**good, "filename": 7}, di.DocumentIndexerError),
    )
    for label, row, exc_type in bad_rows:
        path = tmp_path / f"{label}.jsonl"
        if isinstance(row, str):
            path.write_text(row + "\n", encoding="utf-8")
        else:
            _write_jsonl(path, [good, row])
        prov = di.JsonlPageMapProvider([path], allowed_nct_ids={_ALLOWED_NCT})
        with pytest.raises(exc_type):
            prov.validate_loaded()
        assert prov._by_sha_source == {}, label

    # Identical duplicates are harmless; conflicting duplicates raise.
    dup_ok = _write_jsonl(tmp_path / "dup_ok.jsonl", [good, good])
    assert di.JsonlPageMapProvider([dup_ok]).extract_pages(sha256=sha).physical_pages == 2
    dup_bad = _write_jsonl(tmp_path / "dup_bad.jsonl", [good, {**good, "pages": ["Other"]}])
    with pytest.raises(di.CacheIntegrityError, match="differ"):
        di.JsonlPageMapProvider([dup_bad]).validate_loaded()

    # A cached map filed under one digest must carry that digest.
    pages_dir = tmp_path / "cache" / "pages"
    path = pages_dir / sha[:2] / f"{sha}.json.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(di.encode_gz_json({"sha256": "f" * 64, "pages": ["Wrong"]}))
    with pytest.raises(di.CacheIntegrityError):
        di.JsonlPageMapProvider(cache_pages_dir=pages_dir).extract_pages(sha256=sha)

    # CLI: a mistyped --page-map-jsonl exits 1 and writes nothing.
    manifest = _write_jsonl(tmp_path / "manifest.jsonl", [{"nct_id": _ALLOWED_NCT, "in_audit": False}])
    census = _write_jsonl(
        tmp_path / "census.jsonl",
        [
            {
                "nct_id": _ALLOWED_NCT,
                "filename": "Prot_000.pdf",
                "url": di.build_ctgov_large_doc_url(_ALLOWED_NCT, "Prot_000.pdf"),
                "http_status": 200,
                "sha256": sha,
                "bytes": 4096,
                "doc_type": "protocol",
                "retrieved_at": "2026-11-14T00:00:00Z",
            }
        ],
    )
    profiles_out = tmp_path / "profiles.jsonl"
    rc = cli_mod.run_cli(
        [
            "--manifest", str(manifest),
            "--ingest-census-jsonl", str(census),
            "--page-map-jsonl", str(tmp_path / "typo.jsonl"),
            "--cache-dir", str(tmp_path / "cli_cache"),
            "--index-out", str(tmp_path / "cli_index.jsonl"),
            "--profiles-out", str(profiles_out),
            "--offline",
        ]
    )
    assert rc == 1
    assert not profiles_out.exists()
    assert not (tmp_path / "cli_index.jsonl").exists()


def test_cli_zero_trials_to_index_exits_2_and_leaves_profiles_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An all-audit manifest, --limit below 1, or an empty census leaves the profiles file as it was."""
    profiles_out = tmp_path / "profiles.jsonl"
    sentinel = _sentinel(profiles_out)
    api_spy = _SpyAPIClient({})
    http_spy = _FakeSession({})

    def _run(extra: list[str], tag: str) -> int:
        return cli_mod.run_cli(
            extra
            + [
                "--cache-dir", str(tmp_path / f"cache_{tag}"),
                "--index-out", str(tmp_path / f"index_{tag}.jsonl"),
                "--profiles-out", str(profiles_out),
            ],
            api_client=api_spy,
            session=http_spy,
        )

    all_audit = _write_jsonl(
        tmp_path / "all_audit.jsonl",
        [{"nct_id": _AUDIT_NCT, "in_audit": True}, {"nct_id": "NCT99000098", "in_audit": True}],
    )
    in_scope = _write_jsonl(tmp_path / "in_scope.jsonl", [{"nct_id": _ALLOWED_NCT, "in_audit": False}])
    empty_census = tmp_path / "empty_census.jsonl"
    empty_census.write_text("", encoding="utf-8")

    cases = (
        ("all_audit", ["--manifest", str(all_audit)]),
        ("all_audit_census", ["--manifest", str(all_audit), "--ingest-census-jsonl", str(empty_census), "--offline"]),
        ("limit_zero", ["--manifest", str(in_scope), "--limit", "0"]),
        ("limit_negative", ["--manifest", str(in_scope), "--limit", "-1"]),
        ("limit_zero_census", ["--manifest", str(in_scope), "--ingest-census-jsonl", str(empty_census), "--limit", "0", "--offline"]),
        ("empty_census", ["--manifest", str(in_scope), "--ingest-census-jsonl", str(empty_census), "--offline"]),
    )
    for tag, extra in cases:
        rc = _run(extra, tag)
        err = capsys.readouterr().err
        assert rc == 2, tag
        assert profiles_out.read_bytes() == sentinel, tag
        assert not (tmp_path / f"index_{tag}.jsonl").exists(), tag
        assert not (tmp_path / f"cache_{tag}").exists(), tag
        assert "NCT99000099" not in err and "NCT99000098" not in err, tag
    assert api_spy.requested_ncts == []
    assert http_spy.requested_urls == []


def test_downloaded_unextractable_pdf_is_extraction_failed_with_digest_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PDF that downloads but cannot be extracted keeps its sha256 and size and has no page count."""
    pdf_bytes = _make_minimal_pdf("Downloaded protocol")
    pdf_sha = di.sha256_bytes(pdf_bytes)
    nct = _ALLOWED_NCT
    url = di.build_ctgov_large_doc_url(nct, "Prot_000.pdf")
    url_missing = di.build_ctgov_large_doc_url(nct, "SAP_001.pdf")
    study = _make_study(
        nct,
        [
            {"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False},
            {"filename": "SAP_001.pdf", "hasProtocol": False, "hasSap": True},
        ],
    )
    session = _FakeSession({url: _FakeResponse(200, pdf_bytes), url_missing: _FakeResponse(404, b"")})
    cache = di.DocumentCacheStore(tmp_path / "cache", tmp_path / "index.jsonl")
    profile = cli_mod.index_trial_from_study(
        study,
        cache_store=cache,
        provider=di.ChainedPageTextProvider([], cache_store=cache),
        session=session,
    )
    docs = {d["filename"]: d for d in profile["documents"]}
    noext = docs["Prot_000.pdf"]
    assert noext["readability_status"] == "extraction_failed"
    assert noext["sha256"] == pdf_sha
    assert noext["bytes"] == len(pdf_bytes)
    assert noext["http_status"] == 200
    assert noext["physical_pages"] is None
    assert noext["download_error"] is None
    assert "page_extraction_error" in (noext["extraction_error"] or "")
    failed = docs["SAP_001.pdf"]
    assert failed["readability_status"] == "download_failed"
    assert failed["physical_pages"] is None
    assert failed["extraction_error"] is None
    # Unknown page counts make the total unknown rather than an undercount.
    assert profile["document_badge"]["total_physical_pages"] is None
    legacy = di.to_legacy_census_document_row(noext)
    assert (legacy["sha256"], legacy["bytes"], legacy["physical_pages"]) == (pdf_sha, len(pdf_bytes), None)

    # Trial summary: all extraction_failed -> extraction_failed.
    assert di.summarize_trial_readability([noext]) == "extraction_failed"
    assert di.summarize_trial_readability([failed]) == "download_failed"

    # The record builder refuses an extraction_failed record without the digest.
    with pytest.raises(di.CacheIntegrityError):
        di.build_trial_document_record(
            nct_id=nct, filename="Prot_000.pdf", doc_type="protocol", url=url,
            http_status=200, entry=None, extraction_error="no provider",
        )
    with pytest.raises(di.DocumentIndexerError):
        di.build_trial_document_record(
            nct_id=nct, filename="Prot_000.pdf", doc_type="protocol", url=url,
            http_status=200, entry=None, extraction_error="no provider",
            download_error="http_status_404", sha256=pdf_sha, byte_count=len(pdf_bytes),
        )

    # Through the CLI on a platform with no PDFKit and no page map: exit 0,
    # the record says extraction_failed, and stderr counts it.
    monkeypatch.setattr(di.sys, "platform", "linux")
    manifest = _write_jsonl(tmp_path / "manifest.jsonl", [{"nct_id": nct, "in_audit": False}])
    profiles_out = tmp_path / "profiles.jsonl"
    rc = cli_mod.run_cli(
        [
            "--manifest", str(manifest),
            "--cache-dir", str(tmp_path / "cli_cache"),
            "--index-out", str(tmp_path / "cli_index.jsonl"),
            "--profiles-out", str(profiles_out),
            "--rate-limit-sec", "0",
        ],
        api_client=_SpyAPIClient({nct: study}),
        session=_FakeSession({url: _FakeResponse(200, pdf_bytes), url_missing: _FakeResponse(404, b"")}),
    )
    err = capsys.readouterr().err
    assert rc == 0
    written = json.loads(profiles_out.read_text(encoding="utf-8"))
    statuses = sorted(d["readability_status"] for d in written["documents"])
    assert statuses == ["download_failed", "extraction_failed"]
    assert "download_failed=1, extraction_failed=1" in err


def test_legacy_census_ingest_refuses_rows_missing_provenance(tmp_path: Path) -> None:
    """A census row without retrieved_at, doc_type or url is refused, not filled in."""
    sha = "9" * 64
    full_row = {
        "nct_id": _ALLOWED_NCT,
        "filename": "Prot_000.pdf",
        "url": di.build_ctgov_large_doc_url(_ALLOWED_NCT, "Prot_000.pdf"),
        "http_status": 200,
        "sha256": sha,
        "bytes": 4096,
        "doc_type": "sap",
        "retrieved_at": "2026-11-14T00:00:00Z",
    }
    provider = di.FixturePageTextProvider({sha: _clean_stat_pages(4)})
    for missing in ("retrieved_at", "doc_type", "url"):
        for variant, row in (
            ("absent", {k: v for k, v in full_row.items() if k != missing}),
            ("empty", {**full_row, missing: ""}),
            ("null", {**full_row, missing: None}),
        ):
            census = _write_jsonl(tmp_path / f"census_{missing}_{variant}.jsonl", [row])
            index_path = tmp_path / f"index_{missing}_{variant}.jsonl"
            cache = di.DocumentCacheStore(tmp_path / f"cache_{missing}_{variant}", index_path)
            with pytest.raises(di.DocumentIndexerError, match=missing):
                di.ingest_legacy_census_documents(
                    census,
                    cache_store=cache,
                    provider=provider,
                    allowed_nct_ids={_ALLOWED_NCT},
                    extracted_at="2099-01-01T00:00:00+00:00",
                )
            assert not index_path.exists()
    assert provider.calls == []

    # A complete row keeps the census's own values; the ingest time is only extracted_at.
    census = _write_jsonl(tmp_path / "census_full.jsonl", [full_row])
    cache = di.DocumentCacheStore(tmp_path / "cache_full", tmp_path / "index_full.jsonl")
    _, profiles = di.ingest_legacy_census_documents(
        census,
        cache_store=cache,
        provider=provider,
        allowed_nct_ids={_ALLOWED_NCT},
        extracted_at="2099-01-01T00:00:00+00:00",
    )
    doc = profiles[0]["documents"][0]
    assert doc["retrieved_at"] == "2026-11-14T00:00:00Z"
    assert doc["doc_type"] == "sap"
    assert doc["url"] == full_row["url"]
    assert profiles[0]["extracted_at"] == "2099-01-01T00:00:00+00:00"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_pipeline_commit_names_this_checkout_and_reports_why_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pipeline_commit is this repository's commit whatever the cwd; a null stamp carries a reason."""
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "commit", "-q", "--allow-empty", "-m", "unrelated")
    other_commit = _git(other_repo, "rev-parse", "HEAD")

    own = subprocess.run(
        ["git", "-C", str(di.PIPELINE_REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    for cwd in (other_repo, not_a_repo):
        monkeypatch.chdir(cwd)
        commit, reason = di.resolve_pipeline_commit()
        assert commit != other_commit
        assert di.safe_pipeline_commit() == commit
        if own.returncode == 0:
            assert (commit, reason) == (own.stdout.strip(), None)
        else:
            assert commit is None and reason

    commit, reason = di.resolve_pipeline_commit(not_a_repo)
    assert commit is None
    assert reason is not None and "git rev-parse HEAD failed" in reason

    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert di.resolve_pipeline_commit(not_a_repo) == ("f" * 40, None)
    monkeypatch.delenv("GITHUB_SHA")

    # The CLI keeps a null stamp but says why on stderr.
    monkeypatch.setattr(cli_mod, "_REPO_ROOT", not_a_repo)
    assert cli_mod._resolve_pipeline_commit() is None
    assert "pipeline_commit will be recorded as null" in capsys.readouterr().err


def test_firewall_messages_never_name_holdout_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AuditHoldoutError and InvalidNctIdError messages report positions and counts, never ids."""
    messages: list[str] = []

    def _capture(fn: Any, exc_type: type[Exception] = di.AuditHoldoutError) -> None:
        with pytest.raises(exc_type) as info:
            fn()
        messages.append(str(info.value))

    audit = {_AUDIT_NCT}
    _capture(lambda: di.enforce_not_in_audit(_AUDIT_NCT, audit_nct_ids=audit))
    _capture(lambda: di.enforce_not_in_audit(_AUDIT_NCT, in_audit=True))
    _capture(lambda: di.enforce_not_in_audit(_AUDIT_NCT, in_audit=None))
    _capture(lambda: di.enforce_not_in_audit(_AUDIT_NCT, allowed_nct_ids={_ALLOWED_NCT}))
    _capture(lambda: di.enforce_not_in_audit(_ALLOWED_NCT, audit_nct_ids=audit, allowed_nct_ids=audit))
    _capture(lambda: di.validate_nct_id(_AUDIT_NCT + " "), di.InvalidNctIdError)
    _capture(lambda: di.validate_study_payload_nct_id(_make_study(_AUDIT_NCT, []), expected_nct_id=_ALLOWED_NCT))
    payload = _make_study(_AUDIT_NCT, [])
    payload["in_audit"] = True
    _capture(lambda: di.validate_study_payload_nct_id(payload))

    pm = _write_jsonl(tmp_path / "pm.jsonl", [{"nct_id": _AUDIT_NCT, "sha256": "1" * 64, "pages": ["x"]}])
    _capture(lambda: di.JsonlPageMapProvider([pm], audit_nct_ids=audit).validate_loaded())
    _capture(lambda: di.JsonlPageMapProvider([pm], allowed_nct_ids={_ALLOWED_NCT}).validate_loaded())
    _capture(lambda: di.DocumentCacheStore(tmp_path / "c", tmp_path / "i.jsonl", audit_nct_ids=audit, allowed_nct_ids=audit))

    census = _write_jsonl(tmp_path / "census.jsonl", [{"nct_id": _AUDIT_NCT, "filename": "Prot_000.pdf"}])
    store = di.DocumentCacheStore(tmp_path / "c2", tmp_path / "i2.jsonl")
    _capture(lambda: di.ingest_legacy_census_documents(census, cache_store=store, audit_nct_ids=audit))
    _capture(lambda: di.ingest_legacy_census_documents(census, cache_store=store, allowed_nct_ids={_ALLOWED_NCT}))

    m_flag = _write_jsonl(tmp_path / "m_flag.jsonl", [{"nct_id": _AUDIT_NCT, "in_audit": "true"}])
    _capture(lambda: cli_mod.load_manifest_rows(m_flag))
    m_dup = _write_jsonl(
        tmp_path / "m_dup.jsonl",
        [{"nct_id": _AUDIT_NCT, "in_audit": True}, {"nct_id": _AUDIT_NCT, "in_audit": False}],
    )
    _capture(lambda: cli_mod.load_manifest_rows(m_dup))
    m_ok = _write_jsonl(
        tmp_path / "m_ok.jsonl",
        [{"nct_id": _ALLOWED_NCT, "in_audit": False}, {"nct_id": _AUDIT_NCT, "in_audit": True}],
    )
    _capture(lambda: cli_mod.load_manifest_rows(m_ok, audit_nct_ids={"NCT99000098"}))
    m_nonobj = tmp_path / "m_nonobj.json"
    m_nonobj.write_text(json.dumps({"data": [_AUDIT_NCT]}), encoding="utf-8")
    _capture(lambda: cli_mod.load_manifest_rows(m_nonobj))
    m_badid = _write_jsonl(tmp_path / "m_badid.jsonl", [{"nct_id": _AUDIT_NCT + "​", "in_audit": True}])
    _capture(lambda: cli_mod.load_manifest_rows(m_badid), di.InvalidNctIdError)

    assert len(messages) == 18
    for msg in messages:
        assert re.search(r"NCT[0-9]{8}", msg) is None, msg

    # End to end: an explicit holdout --nct-id exits 2 without echoing the id.
    rc = cli_mod.run_cli(
        [
            "--manifest", str(m_ok),
            "--nct-id", _AUDIT_NCT,
            "--cache-dir", str(tmp_path / "cli_cache"),
            "--index-out", str(tmp_path / "cli_index.jsonl"),
            "--profiles-out", str(tmp_path / "cli_profiles.jsonl"),
        ],
        api_client=_SpyAPIClient({}),
        session=_FakeSession({}),
    )
    assert rc == 2
    assert _AUDIT_NCT not in capsys.readouterr().err


class _FlakyAPIClient(_SpyAPIClient):
    def __init__(self, studies: dict[str, dict[str, Any]], failing: dict[str, Exception]) -> None:
        super().__init__(studies)
        self._failing = failing

    def get_study(self, nct_id: str) -> dict[str, Any]:
        self.requested_ncts.append(nct_id)
        if nct_id in self._failing:
            raise self._failing[nct_id]
        return self._studies[nct_id]


def test_cli_fetch_error_skips_one_trial_and_keeps_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 404 or network error for one trial is reported and skipped; the batch's other profiles are written."""
    ok_a, bad, ok_b = "NCT99000032", "NCT99000033", "NCT99000034"
    manifest = _write_jsonl(
        tmp_path / "manifest.jsonl",
        [{"nct_id": n, "in_audit": False} for n in (ok_a, bad, ok_b)] + [{"nct_id": _AUDIT_NCT, "in_audit": True}],
    )
    profiles_out = tmp_path / "profiles.jsonl"
    sentinel = _sentinel(profiles_out)

    def _run(client: _FlakyAPIClient) -> int:
        return cli_mod.run_cli(
            [
                "--manifest", str(manifest),
                "--cache-dir", str(tmp_path / "cache"),
                "--index-out", str(tmp_path / "index.jsonl"),
                "--profiles-out", str(profiles_out),
                "--rate-limit-sec", "0",
            ],
            api_client=client,
            session=_FakeSession({}),
        )

    import requests

    flaky = _FlakyAPIClient(
        {ok_a: _make_study(ok_a, []), ok_b: _make_study(ok_b, [])},
        {bad: requests.HTTPError("404 Client Error: Not Found")},
    )
    rc = _run(flaky)
    err = capsys.readouterr().err
    assert rc == 1
    assert flaky.requested_ncts == [ok_a, bad, ok_b]
    written = [json.loads(line) for line in profiles_out.read_text(encoding="utf-8").splitlines()]
    assert [p["nct_id"] for p in written] == [ok_a, ok_b]
    assert "FETCH ERRORS: 1 of 3" in err and bad in err

    # Every fetch failing writes no profiles file at all.
    sentinel = _sentinel(profiles_out)
    all_down = _FlakyAPIClient(
        {},
        {n: requests.ConnectionError("connection refused") for n in (ok_a, bad, ok_b)},
    )
    rc_all = _run(all_down)
    err_all = capsys.readouterr().err
    assert rc_all == 1
    assert profiles_out.read_bytes() == sentinel
    assert "FETCH ERRORS: 3 of 3" in err_all and "no profiles file was written" in err_all

    # A firewall error mid-batch still aborts the whole run with nothing written.
    spoof = _FlakyAPIClient({ok_a: _make_study(ok_a, []), bad: _make_study(_AUDIT_NCT, []), ok_b: _make_study(ok_b, [])}, {})
    assert _run(spoof) == 2
    assert profiles_out.read_bytes() == sentinel


# ---------------------------------------------------------------------------
# Group 11: Red team, m1 round 1
#
# Each test states the behaviour the holdout firewall and the no-silent-failure
# rule require of one CLI path or public entry point, driven through fake API
# and session objects (no network). A failing test here names an open bypass:
# make it pass by fixing the module, never by weakening the assertion.
# ---------------------------------------------------------------------------

_RT_A = "NCT99000061"
_RT_B = "NCT99000062"
_RT_UNLISTED = "NCT99000063"
_RT_AUDIT = "NCT99000097"
_RT_STAMP = "2026-11-14T00:00:00Z"
_RT_MISSING: Any = object()


def _rt_outs(tmp_path: Path, tag: str) -> list[str]:
    """Output flags that put every path a run may write under ``tmp_path/<tag>_*``."""
    return [
        "--cache-dir", str(tmp_path / f"{tag}_cache"),
        "--index-out", str(tmp_path / f"{tag}_index.jsonl"),
        "--profiles-out", str(tmp_path / f"{tag}_profiles.jsonl"),
        "--rate-limit-sec", "0",
    ]


def _rt_written(tmp_path: Path, tag: str) -> list[Path]:
    """Every file a run tagged ``tag`` left behind: its cache tree, index and profiles."""
    found: list[Path] = [
        candidate
        for candidate in (tmp_path / f"{tag}_index.jsonl", tmp_path / f"{tag}_profiles.jsonl")
        if candidate.exists()
    ]
    cache = tmp_path / f"{tag}_cache"
    if cache.exists():
        found.extend(p for p in sorted(cache.rglob("*")) if p.is_file())
    return found


def _rt_text(path: Path) -> str:
    """A written file's text (gunzipped for ``.gz``), for searching it for a trial id."""
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def _rt_profile_ids(path: Path) -> list[str]:
    """The ``nct_id`` of each profile line, in file order; empty when no file was written."""
    if not path.exists():
        return []
    return [json.loads(line)["nct_id"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def _rt_manifest(tmp_path: Path, name: str = "rt_manifest.jsonl") -> Path:
    """A manifest listing two in-scope trials and one holdout trial, every flag a strict bool."""
    return _write_jsonl(
        tmp_path / name,
        [
            {"nct_id": _RT_A, "in_audit": False},
            {"nct_id": _RT_B, "in_audit": False},
            {"nct_id": _RT_AUDIT, "in_audit": True},
        ],
    )


def _rt_study(nct_id: str, pdf: bytes, filename: str = "Prot_000.pdf") -> dict[str, Any]:
    """A study payload posting one protocol PDF of the given bytes."""
    return _make_study(
        nct_id,
        [{"filename": filename, "hasProtocol": True, "hasSap": False, "size": len(pdf)}],
    )


def _rt_census_row(nct_id: str, pdf: bytes, **overrides: Any) -> dict[str, Any]:
    """A complete legacy documents.jsonl row for one downloaded protocol, with overrides applied."""
    row: dict[str, Any] = {
        "nct_id": nct_id,
        "filename": "Prot_000.pdf",
        "url": di.build_ctgov_large_doc_url(nct_id, "Prot_000.pdf"),
        "http_status": 200,
        "sha256": di.sha256_bytes(pdf),
        "bytes": len(pdf),
        "doc_type": "protocol",
        "retrieved_at": _RT_STAMP,
    }
    row.update(overrides)
    return row


# --- Open bypasses found in round 1 (these fail until the module is fixed) ---


def test_redteam_m1_repeated_manifest_flag_cannot_drop_the_holdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second ``--manifest`` must not silently replace the first one's holdout set.

    argparse keeps only the last value of a repeated ``--manifest``. Given the
    manifest that marks a trial in_audit=True and then one that marks it False,
    the run indexes the holdout trial. Correct: refuse a repeated --manifest, or
    apply every manifest's holdout set; either way the holdout trial is never
    fetched and nothing written names it.
    """
    canon = _write_jsonl(
        tmp_path / "canon.jsonl",
        [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": True}],
    )
    other = _write_jsonl(
        tmp_path / "other.jsonl",
        [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": False}],
    )
    for tag, extra in (("rm_explicit", ["--nct-id", _RT_AUDIT]), ("rm_all_rows", [])):
        api = _SpyAPIClient({_RT_AUDIT: _make_study(_RT_AUDIT, []), _RT_A: _make_study(_RT_A, [])})
        rc = cli_mod.run_cli(
            ["--manifest", str(canon), "--manifest", str(other), *extra, *_rt_outs(tmp_path, tag)],
            api_client=api,
            session=_FakeSession({}),
        )
        err = capsys.readouterr().err
        assert _RT_AUDIT not in api.requested_ncts, f"{tag}: holdout trial fetched"
        assert [p.name for p in _rt_written(tmp_path, tag) if _RT_AUDIT in _rt_text(p)] == []
        assert _RT_AUDIT not in err
        if extra:
            assert rc == 2
            assert _rt_written(tmp_path, tag) == []


def test_redteam_m1_duplicate_json_keys_in_manifest_rows_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest row naming ``in_audit`` or ``nct_id`` twice is ambiguous and must be refused.

    ``json.loads`` keeps the last duplicate key, so a row written
    ``{"in_audit": true, "in_audit": false}`` reads as False and the holdout
    trial is indexed. A strict loader refuses duplicate keys instead of picking one.
    """
    ok_line = json.dumps({"nct_id": _RT_A, "in_audit": False})
    dup_flag = f'{{"nct_id": "{_RT_AUDIT}", "in_audit": true, "in_audit": false}}'
    dup_id = f'{{"nct_id": "{_RT_AUDIT}", "nct_id": "{_RT_A}", "in_audit": false}}'
    variants = {
        "jsonl_flag": (tmp_path / "dup_flag.jsonl", ok_line + "\n" + dup_flag + "\n"),
        "json_flag": (tmp_path / "dup_flag.json", '{"rows": [' + ok_line + ", " + dup_flag + "]}"),
        "jsonl_id": (
            tmp_path / "dup_id.jsonl",
            json.dumps({"nct_id": _RT_AUDIT, "in_audit": True}) + "\n" + dup_id + "\n",
        ),
    }
    for label, (path, text) in variants.items():
        path.write_text(text, encoding="utf-8")
        if label != "jsonl_id":
            api = _SpyAPIClient({_RT_AUDIT: _make_study(_RT_AUDIT, []), _RT_A: _make_study(_RT_A, [])})
            rc = cli_mod.run_cli(
                ["--manifest", str(path), "--nct-id", _RT_AUDIT, *_rt_outs(tmp_path, f"dk_{label}")],
                api_client=api,
                session=_FakeSession({}),
            )
            err = capsys.readouterr().err
            assert _RT_AUDIT not in api.requested_ncts, f"{label}: holdout trial fetched"
            assert rc == 2
            assert _rt_written(tmp_path, f"dk_{label}") == []
            assert _RT_AUDIT not in err
        with pytest.raises(di.DocumentIndexerError) as info:
            cli_mod.load_manifest_rows(path)
        assert re.search(r"NCT[0-9]{8}", str(info.value)) is None


def test_redteam_m1_manifest_with_more_than_one_row_list_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest object carrying more than one of ``data``/``rows``/``trials`` must be refused.

    The loader reads the first of those keys it finds and ignores the rest, so a
    holdout row under ``rows`` (or ``trials``) is never seen when a ``data``
    (or ``rows``) list naming the same trial in_audit=False is present too.
    """
    in_scope_copy = [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": False}]
    with_holdout = [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": True}]
    shapes = {
        "data_and_rows": {"data": in_scope_copy, "rows": with_holdout},
        "rows_and_trials": {"rows": in_scope_copy, "trials": with_holdout},
    }
    for label, obj in shapes.items():
        path = tmp_path / f"two_lists_{label}.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        api = _SpyAPIClient({_RT_AUDIT: _make_study(_RT_AUDIT, []), _RT_A: _make_study(_RT_A, [])})
        rc = cli_mod.run_cli(
            ["--manifest", str(path), "--nct-id", _RT_AUDIT, *_rt_outs(tmp_path, f"tl_{label}")],
            api_client=api,
            session=_FakeSession({}),
        )
        err = capsys.readouterr().err
        assert _RT_AUDIT not in api.requested_ncts, f"{label}: holdout trial fetched"
        assert rc == 2
        assert _rt_written(tmp_path, f"tl_{label}") == []
        assert _RT_AUDIT not in err
        with pytest.raises(di.DocumentIndexerError):
            cli_mod.load_manifest_rows(path)


def test_redteam_m1_study_payload_alias_naming_a_holdout_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A payload whose ``nctIdAliases`` names a holdout trial is that trial and must be refused.

    CT.gov merges duplicate registrations and keeps the retired ids in
    ``protocolSection.identificationModule.nctIdAliases``. Only ``nctId`` is
    checked, so the holdout trial's documents are downloaded, parsed and
    profiled under the surviving id.
    """
    pdf = _make_minimal_pdf("Alias protocol")
    study = _rt_study(_RT_A, pdf)
    study["protocolSection"]["identificationModule"]["nctIdAliases"] = [_RT_AUDIT]
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    session = _FakeSession({url: _FakeResponse(200, pdf)})
    provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})

    rc = cli_mod.run_cli(
        ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, *_rt_outs(tmp_path, "alias")],
        api_client=_SpyAPIClient({_RT_A: study}),
        session=session,
        provider=provider,
    )
    err = capsys.readouterr().err
    assert session.requested_urls == [], "holdout alias: CDN document requested"
    assert provider.calls == []
    assert rc == 2
    assert _rt_written(tmp_path, "alias") == []
    assert _RT_AUDIT not in err

    with pytest.raises(di.AuditHoldoutError):
        di.validate_study_payload_nct_id(
            study,
            expected_nct_id=_RT_A,
            audit_nct_ids={_RT_AUDIT},
            allowed_nct_ids={_RT_A, _RT_B},
        )


def test_redteam_m1_census_row_url_naming_a_holdout_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A census row for an in-scope trial whose ``url`` is a holdout trial's document must be refused.

    Ingest checks the row's ``nct_id`` and carries ``url`` into the profile
    unchecked, so the profiles file records a holdout trial's document URL under
    an in-scope trial, for an http 200 row and for a failed row alike.
    """
    pdf = _make_minimal_pdf("Census protocol")
    audit_url = di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf")
    rows = {
        "http_200": _rt_census_row(_RT_A, pdf, url=audit_url),
        "http_404": _rt_census_row(_RT_A, pdf, url=audit_url, http_status=404, sha256=None, bytes=None),
    }
    for label, row in rows.items():
        census = _write_jsonl(tmp_path / f"census_url_{label}.jsonl", [row])
        provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})
        tag = f"cu_{label}"
        rc = cli_mod.run_cli(
            [
                "--manifest", str(_rt_manifest(tmp_path)),
                "--ingest-census-jsonl", str(census),
                "--offline",
                *_rt_outs(tmp_path, tag),
            ],
            provider=provider,
        )
        err = capsys.readouterr().err
        leaked = [p.name for p in _rt_written(tmp_path, tag) if _RT_AUDIT in _rt_text(p)]
        assert leaked == [], f"{label}: holdout URL recorded in {leaked}"
        assert rc == 2
        assert _rt_written(tmp_path, tag) == []
        assert provider.calls == []
        assert _RT_AUDIT not in err

        store = di.DocumentCacheStore(
            tmp_path / f"{tag}_lib_cache",
            tmp_path / f"{tag}_lib_index.jsonl",
            audit_nct_ids={_RT_AUDIT},
            allowed_nct_ids={_RT_A, _RT_B},
        )
        with pytest.raises(di.DocumentIndexerError) as info:
            di.ingest_legacy_census_documents(
                census,
                cache_store=store,
                provider=provider,
                audit_nct_ids={_RT_AUDIT},
                allowed_nct_ids={_RT_A, _RT_B},
            )
        assert _RT_AUDIT not in str(info.value)


def test_redteam_m1_cache_url_manifest_holding_a_holdout_entry_is_not_carried_forward(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A url_manifest.json already holding a holdout trial's URL must not be written back with it.

    DocumentCacheStore loads url_manifest.json whole with no firewall check, and
    every ``record_url_manifest`` rewrites the whole file, so a run for an
    in-scope trial re-caches a holdout record left by an earlier unfirewalled
    run. Correct: refuse the cache (exit 2, file left byte for byte), or never
    write the holdout entry back.
    """
    cache = tmp_path / "um_cache"
    cache.mkdir()
    manifest_path = cache / "url_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf"): {
                    "sha256": "b" * 64,
                    "byte_count": 10,
                    "api_size": 10,
                    "api_upload_date": "01/01/2020 10:00",
                    "verified_at": _RT_STAMP,
                    "pipeline_commit": None,
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = manifest_path.read_bytes()
    pdf = _make_minimal_pdf("In-scope protocol")
    session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)})
    provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})

    rc = cli_mod.run_cli(
        ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, *_rt_outs(tmp_path, "um")],
        api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)}),
        session=session,
        provider=provider,
    )
    err = capsys.readouterr().err
    after = manifest_path.read_bytes()
    assert (rc == 2 and after == before) or _RT_AUDIT not in after.decode("utf-8"), (
        f"rc={rc}: the run rewrote url_manifest.json with the holdout trial's entry in it"
    )
    assert _RT_AUDIT not in err


def test_redteam_m1_page_map_never_serves_another_digests_pages(tmp_path: Path) -> None:
    """Page-map pages are served only for the digest they were extracted from.

    When a document's sha256 has no pages, JsonlPageMapProvider falls back to a
    (nct_id, filename) index and returns the pages of whatever digest that row
    named, labelled with the requested digest. The indexer then caches them as
    ``pages/<real sha>.json.gz``: a content address for bytes the pages do not
    describe, and a way for a mislabelled row's text (a holdout trial's, say)
    to enter the cache under an in-scope document.
    """
    pdf = _make_minimal_pdf("Real protocol")
    real_sha = di.sha256_bytes(pdf)
    foreign = ["FOREIGN TEXT from another document of the study protocol"]
    pm = _write_jsonl(
        tmp_path / "pm_other_digest.jsonl",
        [{"nct_id": _RT_A, "filename": "Prot_000.pdf", "sha256": "c" * 64, "pages": foreign}],
    )
    provider = di.JsonlPageMapProvider(
        [pm], audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
    )
    assert provider.get_page_text(_RT_A, "Prot_000.pdf", real_sha, 1) is None
    with pytest.raises(di.PageExtractionError):
        provider.extract_pages(sha256=real_sha, nct_id=_RT_A, filename="Prot_000.pdf")

    session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)})
    cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--nct-id", _RT_A,
            "--page-map-jsonl", str(pm),
            *_rt_outs(tmp_path, "pmo"),
        ],
        api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)}),
        session=session,
    )
    store = di.DocumentCacheStore(
        tmp_path / "pmo_cache",
        tmp_path / "pmo_index.jsonl",
        audit_nct_ids={_RT_AUDIT},
        allowed_nct_ids={_RT_A, _RT_B},
    )
    cached = store.load_cached_pages(real_sha)
    assert cached is None or list(cached.pages) != foreign, (
        "another digest's pages were cached under the downloaded document's sha256"
    )


def test_redteam_m1_chained_provider_checks_the_requesting_trial(tmp_path: Path) -> None:
    """ChainedPageTextProvider.extract_pages must refuse a holdout, unlisted or unnamed requester.

    Its first step serves the cache store's page map by sha256 and checks only
    the map's tag, never the ``nct_id`` asking, so a request made for a holdout
    trial (or an unlisted one, or one naming no trial under an allowlist) is
    answered from the cache. Its own ``get_page_text``, JsonlPageMapProvider
    and DocumentCacheStore.get_page_text all refuse these requests.
    """
    store = di.DocumentCacheStore(
        tmp_path / "ch_cache",
        tmp_path / "ch_index.jsonl",
        audit_nct_ids={_RT_AUDIT},
        allowed_nct_ids={_RT_A},
    )
    sha = "d" * 64
    store.save_cached_pages(sha, byte_count=5, pages=["shared page text"], nct_id=_RT_A)
    chain = di.ChainedPageTextProvider([], cache_store=store)
    assert chain.extract_pages(sha256=sha, nct_id=_RT_A).pages == ("shared page text",)
    with pytest.raises(di.AuditHoldoutError):
        chain.get_page_text(_RT_AUDIT, "Prot_000.pdf", sha, 1)
    for requester in (_RT_AUDIT, _RT_UNLISTED, None):
        with pytest.raises(di.AuditHoldoutError):
            chain.extract_pages(sha256=sha, nct_id=requester)


def test_redteam_m1_download_url_must_be_the_trials_own_document(tmp_path: Path) -> None:
    """download_pdf_atomic must refuse a URL that is not the named trial's own posted document.

    It puts ``nct_id`` through the firewall and then GETs whatever ``url`` it
    was handed, so an in-scope id paired with a holdout trial's URL downloads
    the holdout document into the content-addressed cache.
    """
    pdf = _make_minimal_pdf("Holdout protocol bytes")
    audit_url = di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf")
    own_url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    session = _FakeSession({audit_url: _FakeResponse(200, pdf), own_url: _FakeResponse(200, pdf)})
    blobs = tmp_path / "dl_blobs"
    with pytest.raises(di.DocumentIndexerError):
        cli_mod.download_pdf_atomic(
            audit_url,
            blobs,
            session=session,
            nct_id=_RT_A,
            filename="Prot_000.pdf",
            audit_nct_ids={_RT_AUDIT},
            allowed_nct_ids={_RT_A},
        )
    assert session.requested_urls == []
    assert not blobs.exists()

    ok = cli_mod.download_pdf_atomic(
        own_url,
        blobs,
        session=session,
        nct_id=_RT_A,
        filename="Prot_000.pdf",
        audit_nct_ids={_RT_AUDIT},
        allowed_nct_ids={_RT_A},
    )
    assert ok.ok and session.requested_urls == [own_url]


def test_redteam_m1_repeated_census_flag_is_not_silently_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two ``--ingest-census-jsonl`` flags must not silently ingest only the last file.

    argparse keeps the last value, so the first census file is dropped with no
    message and the run exits 0. Correct: refuse the repeat (exit 2, nothing
    written) or ingest both files.
    """
    pdf_a, pdf_b = _make_minimal_pdf("Census A"), _make_minimal_pdf("Census B")
    census_a = _write_jsonl(tmp_path / "census_a.jsonl", [_rt_census_row(_RT_A, pdf_a)])
    census_b = _write_jsonl(tmp_path / "census_b.jsonl", [_rt_census_row(_RT_B, pdf_b)])
    provider = di.FixturePageTextProvider(
        {di.sha256_bytes(pdf_a): _clean_stat_pages(2), di.sha256_bytes(pdf_b): _clean_stat_pages(3)}
    )
    rc = cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--ingest-census-jsonl", str(census_a),
            "--ingest-census-jsonl", str(census_b),
            "--offline",
            *_rt_outs(tmp_path, "rcf"),
        ],
        provider=provider,
    )
    capsys.readouterr()
    ids = _rt_profile_ids(tmp_path / "rcf_profiles.jsonl")
    assert (rc == 2 and _rt_written(tmp_path, "rcf") == []) or sorted(ids) == [_RT_A, _RT_B], (
        f"rc={rc}, profiles={ids}: a census file was dropped without a word"
    )


def test_redteam_m1_duplicate_nct_id_args_fetch_and_profile_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trial named twice on the command line is fetched once and profiled once.

    ``--nct-id A --nct-id A`` (or ``--nct-id A A``) currently fetches the record
    twice and writes two profile lines for one trial, which a consumer keyed by
    nct_id resolves silently.
    """
    for tag, args in (
        ("dup_flags", ["--nct-id", _RT_A, "--nct-id", _RT_A]),
        ("dup_nargs", ["--nct-id", _RT_A, _RT_A]),
    ):
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
        cli_mod.run_cli(
            ["--manifest", str(_rt_manifest(tmp_path)), *args, *_rt_outs(tmp_path, tag)],
            api_client=api,
            session=_FakeSession({}),
        )
        capsys.readouterr()
        ids = _rt_profile_ids(tmp_path / f"{tag}_profiles.jsonl")
        assert len(api.requested_ncts) <= 1, f"{tag}: fetched {len(api.requested_ncts)} times"
        assert len(ids) == len(set(ids)), f"{tag}: duplicate profiles {ids}"


def test_redteam_m1_nct_id_filter_is_not_ignored_by_census_ingest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--nct-id A`` with ``--ingest-census-jsonl`` must not index every trial in the census file.

    The explicit ids are checked against the firewall and then ignored: every
    census row is indexed, cached and profiled. Correct: restrict the ingest to
    the named trials, or refuse the combination before writing anything.
    """
    pdf_a, pdf_b = _make_minimal_pdf("Filter A"), _make_minimal_pdf("Filter B")
    sha_b = di.sha256_bytes(pdf_b)
    census = _write_jsonl(
        tmp_path / "census_ab.jsonl", [_rt_census_row(_RT_A, pdf_a), _rt_census_row(_RT_B, pdf_b)]
    )
    provider = di.FixturePageTextProvider(
        {di.sha256_bytes(pdf_a): _clean_stat_pages(2), sha_b: _clean_stat_pages(3)}
    )
    cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--nct-id", _RT_A,
            "--ingest-census-jsonl", str(census),
            "--offline",
            *_rt_outs(tmp_path, "ncf"),
        ],
        provider=provider,
    )
    capsys.readouterr()
    assert sha_b not in [call[0] for call in provider.calls], "an unrequested trial was indexed"
    assert _RT_B not in _rt_profile_ids(tmp_path / "ncf_profiles.jsonl")


def test_redteam_m1_limit_bounds_census_ingest_work_not_only_its_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--limit 1`` with ``--ingest-census-jsonl`` must not index and cache trials it then leaves out.

    The limit is applied to the profiles after every census row has been
    indexed and written to the cache and index, so the index holds documents
    of trials that have no profile in the run's output.
    """
    pdf_a, pdf_b = _make_minimal_pdf("Limit A"), _make_minimal_pdf("Limit B")
    census = _write_jsonl(
        tmp_path / "census_lim.jsonl", [_rt_census_row(_RT_A, pdf_a), _rt_census_row(_RT_B, pdf_b)]
    )
    provider = di.FixturePageTextProvider(
        {di.sha256_bytes(pdf_a): _clean_stat_pages(2), di.sha256_bytes(pdf_b): _clean_stat_pages(3)}
    )
    cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--limit", "1",
            "--ingest-census-jsonl", str(census),
            "--offline",
            *_rt_outs(tmp_path, "lim"),
        ],
        provider=provider,
    )
    capsys.readouterr()
    index_path = tmp_path / "lim_index.jsonl"
    index_lines = index_path.read_text(encoding="utf-8").splitlines() if index_path.exists() else []
    profiles = _rt_profile_ids(tmp_path / "lim_profiles.jsonl")
    assert len(provider.calls) <= 1, f"--limit 1 parsed {len(provider.calls)} documents"
    assert len(index_lines) == len(profiles), (
        f"index holds {len(index_lines)} documents for {len(profiles)} profiled trial(s)"
    )


def test_redteam_m1_malformed_url_manifest_is_refused_not_overwritten(tmp_path: Path) -> None:
    """A url_manifest.json that is not an object of objects must raise, not be dropped and overwritten.

    The store keeps only the Mapping values of a dict and ignores anything else,
    then rewrites the file on the next ``record_url_manifest``: the malformed
    records are deleted with no message.
    """
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    for label, payload in (
        ("json_list", [{"sha256": "a" * 64}]),
        ("non_object_values", {url: "a" * 64, url + "?v=2": 5}),
    ):
        cache = tmp_path / f"umm_{label}"
        cache.mkdir()
        path = cache / "url_manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(di.CacheIntegrityError):
            store = di.DocumentCacheStore(cache, tmp_path / f"umm_{label}_index.jsonl")
            store.record_url_manifest(url, sha256="a" * 64, byte_count=3, api_size=3, api_upload_date=None)
        assert path.read_bytes() == before, f"{label}: url_manifest.json was overwritten"


# --- Attempts that held in round 1 (these pass; they pin the behaviour) ---


def test_redteam_m1_no_argument_and_flag_only_runs_touch_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no target, or a manifest and a non-positive --limit, the run exits 2 and writes nothing."""
    clean = tmp_path / "clean_cwd"
    clean.mkdir()
    monkeypatch.chdir(clean)
    for argv in (
        [],
        ["--offline"],
        ["--limit", "3", "--rate-limit-sec", "0"],
        ["--page-map-jsonl", "pm.jsonl", "--local-pdf-dir", "pdfs", "--force-download", "--reparse-cached-pages"],
    ):
        api = _SpyAPIClient({})
        session = _FakeSession({})
        rc = cli_mod.run_cli(argv, api_client=api, session=session)
        err = capsys.readouterr().err
        assert rc == 2, argv
        assert "usage:" in err.lower()
        assert api.requested_ncts == [] and session.requested_urls == []
        assert list(clean.iterdir()) == []

    manifest = _rt_manifest(tmp_path)
    for value in ("0", "-1"):
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
        tag = f"limit_{value.replace('-', 'neg')}"
        rc = cli_mod.run_cli(
            ["--manifest", str(manifest), f"--limit={value}", *_rt_outs(tmp_path, tag)],
            api_client=api,
            session=_FakeSession({}),
        )
        capsys.readouterr()
        assert rc == 2
        assert api.requested_ncts == []
        assert _rt_written(tmp_path, tag) == []


def test_redteam_m1_no_manifest_blocks_every_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--nct-id and --ingest-census-jsonl without --manifest exit 2 with zero fetches, parses or writes."""
    pdf = _make_minimal_pdf("No manifest")
    census = _write_jsonl(tmp_path / "census_nm.jsonl", [_rt_census_row(_RT_A, pdf)])
    for tag, argv in (
        ("nm_nct", ["--nct-id", _RT_A]),
        ("nm_census", ["--ingest-census-jsonl", str(census), "--offline"]),
        ("nm_both", ["--nct-id", _RT_A, "--ingest-census-jsonl", str(census)]),
    ):
        api = _SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)})
        session = _FakeSession({})
        provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})
        rc = cli_mod.run_cli(
            [*argv, *_rt_outs(tmp_path, tag)], api_client=api, session=session, provider=provider
        )
        capsys.readouterr()
        assert rc == 2, tag
        assert api.requested_ncts == [] and session.requested_urls == [] and provider.calls == []
        assert _rt_written(tmp_path, tag) == []


def test_redteam_m1_manifest_in_audit_must_be_a_json_boolean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing, null, string, numeric or container in_audit on any row stops the run before any fetch."""
    bad_values: list[Any] = [_RT_MISSING, None, "false", "False", "true", 0, 1, 0.0, [], {}]
    for index, value in enumerate(bad_values):
        for target in (_RT_AUDIT, _RT_A):
            row: dict[str, Any] = {"nct_id": target}
            if value is not _RT_MISSING:
                row["in_audit"] = value
            others = [
                r
                for r in ({"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": True})
                if r["nct_id"] != target
            ]
            tag = f"ia_{index}_{target[-2:]}"
            manifest = _write_jsonl(tmp_path / f"{tag}.jsonl", [*others, row])
            api = _SpyAPIClient({_RT_A: _make_study(_RT_A, []), _RT_AUDIT: _make_study(_RT_AUDIT, [])})
            rc = cli_mod.run_cli(
                ["--manifest", str(manifest), *_rt_outs(tmp_path, tag)],
                api_client=api,
                session=_FakeSession({}),
            )
            err = capsys.readouterr().err
            assert rc == 2, (tag, value)
            assert api.requested_ncts == []
            assert _rt_written(tmp_path, tag) == []
            assert _RT_AUDIT not in err


def test_redteam_m1_malformed_ids_never_fetch_write_or_echo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lowercase, padded, zero-width, BOM, newline and non-ASCII-digit ids are refused at every entry point.

    Each spelling of the holdout id is refused on --nct-id, in a manifest row,
    in a census row and in a page-map row, before any fetch, parse or write,
    and no message carries the id's digits.
    """
    digits = _RT_AUDIT[3:]
    variants = (
        _RT_AUDIT.lower(),
        "nCT" + digits,
        " " + _RT_AUDIT,
        _RT_AUDIT + " ",
        _RT_AUDIT + "​",
        "‍" + _RT_AUDIT,
        "﻿" + _RT_AUDIT,
        _RT_AUDIT + "\n",
        "NCT" + digits.translate(str.maketrans("0123456789", "０１２３４５６７８９")),
        "NCT" + digits.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")),
    )
    manifest = _rt_manifest(tmp_path)
    pdf = _make_minimal_pdf("Malformed ids")
    for index, bad in enumerate(variants):
        tag = f"bad_{index}"
        api = _SpyAPIClient({})
        session = _FakeSession({})
        rc = cli_mod.run_cli(
            ["--manifest", str(manifest), "--nct-id", bad, *_rt_outs(tmp_path, tag)],
            api_client=api,
            session=session,
        )
        err = capsys.readouterr().err
        assert rc != 0, repr(bad)
        assert api.requested_ncts == [] and session.requested_urls == []
        assert _rt_written(tmp_path, tag) == []
        assert digits not in err

        bad_manifest = _write_jsonl(
            tmp_path / f"{tag}_manifest.jsonl",
            [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": bad, "in_audit": True}],
        )
        with pytest.raises(di.InvalidNctIdError) as info:
            cli_mod.load_manifest_rows(bad_manifest)
        assert digits not in str(info.value)

        census = _write_jsonl(
            tmp_path / f"{tag}_census.jsonl",
            [_rt_census_row(_RT_A, pdf), {**_rt_census_row(_RT_A, pdf), "nct_id": bad}],
        )
        provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})
        store = di.DocumentCacheStore(
            tmp_path / f"{tag}_lib_cache",
            tmp_path / f"{tag}_lib_index.jsonl",
            audit_nct_ids={_RT_AUDIT},
            allowed_nct_ids={_RT_A, _RT_B},
        )
        with pytest.raises(di.InvalidNctIdError) as info:
            di.ingest_legacy_census_documents(
                census,
                cache_store=store,
                provider=provider,
                audit_nct_ids={_RT_AUDIT},
                allowed_nct_ids={_RT_A, _RT_B},
            )
        assert digits not in str(info.value)
        assert provider.calls == []
        assert not (tmp_path / f"{tag}_lib_cache").exists()

        page_map = _write_jsonl(
            tmp_path / f"{tag}_pm.jsonl", [{"nct_id": bad, "sha256": "e" * 64, "pages": ["x"]}]
        )
        with pytest.raises(di.InvalidNctIdError) as info:
            di.JsonlPageMapProvider(
                [page_map], audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
            ).validate_loaded()
        assert digits not in str(info.value)


def test_redteam_m1_api_payload_for_another_trial_is_refused_before_any_download(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CT.gov payload whose nctId is not the requested trial's stops the run before any CDN request."""
    pdf = _make_minimal_pdf("Payload swap")
    manifest = _rt_manifest(tmp_path)
    for label, payload_id, expected_rc in (
        ("holdout", _RT_AUDIT, 2),
        ("other_in_scope", _RT_B, 2),
        ("unlisted", _RT_UNLISTED, 2),
        ("lowercase", _RT_A.lower(), 1),
        ("padded", _RT_A + " ", 1),
    ):
        study = _rt_study(_RT_A, pdf)
        study["protocolSection"]["identificationModule"]["nctId"] = payload_id
        session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)})
        provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})
        tag = f"swap_{label}"
        rc = cli_mod.run_cli(
            ["--manifest", str(manifest), "--nct-id", _RT_A, *_rt_outs(tmp_path, tag)],
            api_client=_SpyAPIClient({_RT_A: study}),
            session=session,
            provider=provider,
        )
        err = capsys.readouterr().err
        assert rc == expected_rc, label
        assert session.requested_urls == [] and provider.calls == []
        assert not (tmp_path / f"{tag}_profiles.jsonl").exists()
        assert _RT_AUDIT not in err


def test_redteam_m1_census_rows_for_holdout_or_unlisted_trials_refused_before_any_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A census file with any holdout, flagged, unlisted or non-bool-flagged row is refused whole."""
    pdf_a, pdf_b = _make_minimal_pdf("Census ok"), _make_minimal_pdf("Census bad")
    good = _rt_census_row(_RT_A, pdf_a)
    variants: dict[str, list[dict[str, Any]]] = {
        "holdout_id": [good, _rt_census_row(_RT_AUDIT, pdf_b)],
        "holdout_id_flag_false": [good, _rt_census_row(_RT_AUDIT, pdf_b, in_audit=False)],
        "flag_true": [good, _rt_census_row(_RT_B, pdf_b, in_audit=True)],
        "flag_string": [good, _rt_census_row(_RT_B, pdf_b, in_audit="false")],
        "flag_null": [good, _rt_census_row(_RT_B, pdf_b, in_audit=None)],
        "flag_zero": [good, _rt_census_row(_RT_B, pdf_b, in_audit=0)],
        "unlisted": [good, _rt_census_row(_RT_UNLISTED, pdf_b)],
        "unlisted_flag_false": [good, _rt_census_row(_RT_UNLISTED, pdf_b, in_audit=False)],
    }
    for label, rows in variants.items():
        census = _write_jsonl(tmp_path / f"census_{label}.jsonl", rows)
        provider = di.FixturePageTextProvider(
            {di.sha256_bytes(pdf_a): _clean_stat_pages(2), di.sha256_bytes(pdf_b): _clean_stat_pages(3)}
        )
        tag = f"cr_{label}"
        rc = cli_mod.run_cli(
            [
                "--manifest", str(_rt_manifest(tmp_path)),
                "--ingest-census-jsonl", str(census),
                "--offline",
                *_rt_outs(tmp_path, tag),
            ],
            provider=provider,
        )
        err = capsys.readouterr().err
        assert rc == 2, label
        assert provider.calls == []
        assert _rt_written(tmp_path, tag) == []
        assert _RT_AUDIT not in err


def test_redteam_m1_page_map_rows_for_holdout_trials_refused_before_any_fetch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page-map file with a holdout, flagged, unlisted or untagged row stops the run before any fetch.

    Plain and gzipped files alike; and a cached page map tagged with a holdout
    trial, reached through an in-scope document's digest, stops the run with
    no profile written and the cached map left as it was.
    """
    good = {"nct_id": _RT_A, "sha256": "1" * 64, "pages": ["in-scope page"]}
    variants: dict[str, list[dict[str, Any]]] = {
        "holdout_tag": [good, {"nct_id": _RT_AUDIT, "sha256": "2" * 64, "pages": ["x"]}],
        "holdout_tag_flag_false": [good, {"nct_id": _RT_AUDIT, "sha256": "2" * 64, "pages": ["x"], "in_audit": False}],
        "flag_true": [good, {"nct_id": _RT_B, "sha256": "3" * 64, "pages": ["x"], "in_audit": True}],
        "flag_string": [good, {"nct_id": _RT_B, "sha256": "3" * 64, "pages": ["x"], "in_audit": "false"}],
        "untagged": [good, {"sha256": "4" * 64, "pages": ["x"]}],
        "unlisted": [good, {"nct_id": _RT_UNLISTED, "sha256": "5" * 64, "pages": ["x"]}],
    }
    manifest = _rt_manifest(tmp_path)
    for label, rows in variants.items():
        for gz in (False, True):
            tag = f"pmr_{label}_{'gz' if gz else 'plain'}"
            path = tmp_path / (f"{tag}.jsonl.gz" if gz else f"{tag}.jsonl")
            text = "".join(json.dumps(r) + "\n" for r in rows)
            path.write_bytes(gzip.compress(text.encode("utf-8")) if gz else text.encode("utf-8"))
            api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
            rc = cli_mod.run_cli(
                ["--manifest", str(manifest), "--nct-id", _RT_A, "--page-map-jsonl", str(path), *_rt_outs(tmp_path, tag)],
                api_client=api,
                session=_FakeSession({}),
            )
            err = capsys.readouterr().err
            assert rc == 2, tag
            assert api.requested_ncts == []
            assert _rt_written(tmp_path, tag) == []
            assert _RT_AUDIT not in err

    pdf = _make_minimal_pdf("Digest shared with a holdout map")
    sha = di.sha256_bytes(pdf)
    cached_map = _write_cached_map(
        tmp_path / "pmc_cache" / "pages",
        {"sha256": sha, "nct_id": _RT_AUDIT, "byte_count": len(pdf), "physical_pages": 1, "pages": ["holdout page"]},
    )
    before = cached_map.read_bytes()
    session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)})
    rc = cli_mod.run_cli(
        ["--manifest", str(manifest), "--nct-id", _RT_A, *_rt_outs(tmp_path, "pmc")],
        api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)}),
        session=session,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert not (tmp_path / "pmc_profiles.jsonl").exists()
    assert cached_map.read_bytes() == before
    assert _RT_AUDIT not in err


def test_redteam_m1_nested_or_wrapped_ids_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ids or flags wrapped in objects or lists, in a manifest, census row or page-map row, are refused."""
    shapes: dict[str, Any] = {
        "data_is_object": {"data": {"rows": [{"nct_id": _RT_A, "in_audit": False}]}},
        "row_is_list": {"rows": [[{"nct_id": _RT_A, "in_audit": False}]]},
        "id_is_object": {"rows": [{"nct_id": {"id": _RT_A}, "in_audit": False}]},
        "id_is_list": {"rows": [{"nct_id": [_RT_A], "in_audit": False}]},
        "flag_is_object": {"rows": [{"nct_id": _RT_A, "in_audit": {"value": False}}]},
        "flag_is_list": {"rows": [{"nct_id": _RT_A, "in_audit": [False]}]},
        "rows_of_bare_ids": {"rows": [_RT_A]},
        "root_is_bare_id": _RT_A,
    }
    for label, obj in shapes.items():
        path = tmp_path / f"nested_{label}.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
        tag = f"nest_{label}"
        rc = cli_mod.run_cli(
            ["--manifest", str(path), *_rt_outs(tmp_path, tag)],
            api_client=api,
            session=_FakeSession({}),
        )
        capsys.readouterr()
        assert rc in (1, 2), label
        assert api.requested_ncts == []
        assert _rt_written(tmp_path, tag) == []

    pdf = _make_minimal_pdf("Nested census")
    store = di.DocumentCacheStore(
        tmp_path / "nest_lib_cache",
        tmp_path / "nest_lib_index.jsonl",
        audit_nct_ids={_RT_AUDIT},
        allowed_nct_ids={_RT_A, _RT_B},
    )
    for label, wrapped in (("object", {"id": _RT_AUDIT}), ("list", [_RT_AUDIT])):
        census = _write_jsonl(
            tmp_path / f"nest_census_{label}.jsonl", [{**_rt_census_row(_RT_A, pdf), "nct_id": wrapped}]
        )
        with pytest.raises(di.InvalidNctIdError) as info:
            di.ingest_legacy_census_documents(
                census, cache_store=store, audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
            )
        assert _RT_AUDIT not in str(info.value)
        page_map = _write_jsonl(
            tmp_path / f"nest_pm_{label}.jsonl", [{"nct_id": wrapped, "sha256": "f" * 64, "pages": ["x"]}]
        )
        with pytest.raises(di.InvalidNctIdError) as info:
            di.JsonlPageMapProvider(
                [page_map], audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
            ).validate_loaded()
        assert _RT_AUDIT not in str(info.value)
    assert not (tmp_path / "nest_lib_cache").exists()


# ---------------------------------------------------------------------------
# Group 12: Round 1 fixes carried to sibling entry points
#
# The red-team tests above pin each reported bypass. These pin the same rule
# where the fix reached further: every single-value option, duplicate keys in
# every JSON input, the other kinds of url_manifest.json entry, the record
# builder's url, the shape of a payload's alias list, the digest a provider
# reports, and the ingest's max_trials argument. Fictional ids only.
# ---------------------------------------------------------------------------


def _m1_url_record(sha_char: str) -> dict[str, Any]:
    """A url_manifest.json record as record_url_manifest writes it, for a fictional digest."""
    return {
        "sha256": sha_char * 64,
        "byte_count": 10,
        "api_size": 10,
        "api_upload_date": "01/01/2020 10:00",
        "verified_at": _RT_STAMP,
        "pipeline_commit": None,
    }


def test_m1_fix_every_single_value_option_refuses_a_repeat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any option that takes one value, given twice, exits 2 naming the option, with no fetch or write.

    The rule is general, not only --manifest and --ingest-census-jsonl: argparse
    keeps the last of any repeated single-value option, so each would drop its
    earlier value without a word. A repeated --nct-id is not refused; it is
    fetched once and the repeat is counted on stderr without naming the id.
    """
    manifest = _rt_manifest(tmp_path)
    other = tmp_path / "rep_other"
    repeats: list[tuple[str, list[str]]] = [
        ("--manifest", ["--manifest", str(manifest)]),
        ("--cache-dir", ["--cache-dir", str(other / "cache")]),
        ("--index-out", ["--index-out", str(other / "index.jsonl")]),
        ("--profiles-out", ["--profiles-out", str(other / "profiles.jsonl")]),
        ("--rate-limit-sec", ["--rate-limit-sec", "0"]),
        ("--limit", ["--limit", "1", "--limit", "2"]),
        ("--min-selected-pages", ["--min-selected-pages", "5", "--min-selected-pages", "6"]),
        ("--max-selected-pages", ["--max-selected-pages", "20", "--max-selected-pages", "21"]),
        ("--local-pdf-dir", ["--local-pdf-dir", str(other / "a"), "--local-pdf-dir", str(other / "b")]),
    ]
    for index, (flag, extra) in enumerate(repeats):
        tag = f"rep_{index}"
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
        rc = cli_mod.run_cli(
            ["--manifest", str(manifest), "--nct-id", _RT_A, *_rt_outs(tmp_path, tag), *extra],
            api_client=api,
            session=_FakeSession({}),
        )
        err = capsys.readouterr().err
        assert rc == 2, flag
        assert flag in err
        assert api.requested_ncts == []
        assert _rt_written(tmp_path, tag) == []
        assert not other.exists()

    api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
    rc = cli_mod.run_cli(
        ["--manifest", str(manifest), "--nct-id", _RT_A, _RT_A, "--nct-id", _RT_A, *_rt_outs(tmp_path, "rep_ids")],
        api_client=api,
        session=_FakeSession({}),
    )
    err = capsys.readouterr().err
    assert rc == 0
    assert api.requested_ncts == [_RT_A]
    assert "2 repeated --nct-id value(s) ignored" in err
    assert re.search(r"NCT[0-9]{8}", err) is None


def test_m1_fix_duplicate_keys_refused_in_census_page_map_and_cached_map_inputs(tmp_path: Path) -> None:
    """A census row, a page-map row or a cached page map naming a key twice is refused, not read last-wins."""
    pdf = _make_minimal_pdf("Duplicate keys elsewhere")
    sha = di.sha256_bytes(pdf)
    audit, allowed = {_RT_AUDIT}, {_RT_A, _RT_B}

    body = json.dumps(_rt_census_row(_RT_A, pdf))[1:-1]
    census = tmp_path / "dup_census.jsonl"
    census.write_text("{" + body + ', "in_audit": true, "in_audit": false}\n', encoding="utf-8")
    provider = di.FixturePageTextProvider({sha: _clean_stat_pages(2)})
    store = di.DocumentCacheStore(
        tmp_path / "dk_cache", tmp_path / "dk_index.jsonl", audit_nct_ids=audit, allowed_nct_ids=allowed
    )
    with pytest.raises(di.AuditHoldoutError) as info:
        di.ingest_legacy_census_documents(
            census, cache_store=store, provider=provider, audit_nct_ids=audit, allowed_nct_ids=allowed
        )
    assert "'in_audit'" in str(info.value)
    assert provider.calls == []
    assert not (tmp_path / "dk_cache").exists() and not (tmp_path / "dk_index.jsonl").exists()

    page_map = tmp_path / "dup_pm.jsonl"
    page_map.write_text(
        f'{{"nct_id": "{_RT_AUDIT}", "nct_id": "{_RT_A}", "sha256": "{"e" * 64}", "pages": ["x"]}}\n',
        encoding="utf-8",
    )
    with pytest.raises(di.AuditHoldoutError) as info:
        di.JsonlPageMapProvider([page_map], audit_nct_ids=audit, allowed_nct_ids=allowed).validate_loaded()
    assert re.search(r"NCT[0-9]{8}", str(info.value)) is None

    cache_dir = tmp_path / "dkc_cache"
    map_path = cache_dir / "pages" / sha[:2] / f"{sha}.json.gz"
    map_path.parent.mkdir(parents=True)
    map_text = (
        f'{{"sha256": "{sha}", "nct_id": "{_RT_AUDIT}", "nct_id": "{_RT_A}", '
        f'"byte_count": {len(pdf)}, "physical_pages": 1, "pages": ["holdout page"]}}'
    )
    map_path.write_bytes(gzip.compress(map_text.encode("utf-8")))
    before = map_path.read_bytes()
    cached_store = di.DocumentCacheStore(
        cache_dir, tmp_path / "dkc_index.jsonl", audit_nct_ids=audit, allowed_nct_ids=allowed
    )
    with pytest.raises(di.AuditHoldoutError):
        cached_store.load_cached_pages(sha)
    with pytest.raises(di.AuditHoldoutError):
        di.JsonlPageMapProvider(
            [], cache_pages_dir=cache_dir / "pages", audit_nct_ids=audit, allowed_nct_ids=allowed
        ).get_page_text(_RT_A, "Prot_000.pdf", sha, 1)
    assert map_path.read_bytes() == before


def test_m1_fix_manifest_wrapper_with_a_row_list_and_a_top_level_row_is_refused(tmp_path: Path) -> None:
    """A wrapper holding a row list and a top-level ``nct_id`` row is two sources of rows: refused."""
    path = tmp_path / "rows_and_row.json"
    path.write_text(
        json.dumps({"nct_id": _RT_AUDIT, "in_audit": True, "rows": [{"nct_id": _RT_A, "in_audit": False}]}),
        encoding="utf-8",
    )
    with pytest.raises(di.AuditHoldoutError) as info:
        cli_mod.load_manifest_rows(path)
    assert re.search(r"NCT[0-9]{8}", str(info.value)) is None


def test_m1_fix_url_manifest_entries_pass_the_firewall_and_are_never_dropped(tmp_path: Path) -> None:
    """url_manifest.json under a configured firewall, and its malformed variants under any store.

    An unattributable key is refused (it cannot be checked against the holdout);
    an unlisted trial's entry is kept and carried unchanged through a rewrite
    but never served; no holdout, unlisted or unattributable record is written;
    unreadable JSON, a repeated URL key and a record without a valid digest or
    size are refused with the file left byte for byte.
    """
    audit, allowed = {_RT_AUDIT}, {_RT_A, _RT_B}
    url_a = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    url_unlisted = di.build_ctgov_large_doc_url(_RT_UNLISTED, "Prot_000.pdf")
    url_audit = di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf")

    bad_key_cache = tmp_path / "umf_bad_key"
    bad_key_cache.mkdir()
    bad_key_path = bad_key_cache / "url_manifest.json"
    bad_key_path.write_text(json.dumps({"https://example.org/Prot_000.pdf": _m1_url_record("a")}), encoding="utf-8")
    before = bad_key_path.read_bytes()
    with pytest.raises(di.AuditHoldoutError):
        di.DocumentCacheStore(
            bad_key_cache, tmp_path / "umf_bad_key_index.jsonl", audit_nct_ids=audit, allowed_nct_ids=allowed
        )
    assert bad_key_path.read_bytes() == before
    # With no firewall configured (library use), the same file loads, as an untagged page map does.
    di.DocumentCacheStore(bad_key_cache, tmp_path / "umf_bad_key_index.jsonl")

    kept_cache = tmp_path / "umf_unlisted"
    kept_cache.mkdir()
    kept_path = kept_cache / "url_manifest.json"
    kept_path.write_text(json.dumps({url_unlisted: _m1_url_record("b")}), encoding="utf-8")
    before = kept_path.read_bytes()
    store = di.DocumentCacheStore(
        kept_cache, tmp_path / "umf_unlisted_index.jsonl", audit_nct_ids=audit, allowed_nct_ids=allowed
    )
    with pytest.raises(di.AuditHoldoutError):
        store.lookup_url_manifest(url_unlisted)
    with pytest.raises(di.AuditHoldoutError):
        store.can_skip_download(url_unlisted, api_size=10, api_upload_date="01/01/2020 10:00")
    for refused_url in (url_unlisted, url_audit, url_a + "?v=2"):
        with pytest.raises(di.AuditHoldoutError) as info:
            store.record_url_manifest(refused_url, sha256="c" * 64, byte_count=3, api_size=3, api_upload_date=None)
        assert re.search(r"NCT[0-9]{8}", str(info.value)) is None
    assert kept_path.read_bytes() == before
    store.record_url_manifest(url_a, sha256="c" * 64, byte_count=3, api_size=3, api_upload_date=None)
    written = json.loads(kept_path.read_text(encoding="utf-8"))
    assert written[url_unlisted] == _m1_url_record("b")
    assert written[url_a]["sha256"] == "c" * 64

    record_a = json.dumps(_m1_url_record("a"))
    malformed = {
        "not_json": "{not json",
        "repeated_url_key": f'{{"{url_a}": {record_a}, "{url_a}": {json.dumps(_m1_url_record("b"))}}}',
        "bad_sha256": json.dumps({url_a: {**_m1_url_record("a"), "sha256": "XYZ"}}),
        "zero_bytes": json.dumps({url_a: {**_m1_url_record("a"), "byte_count": 0}}),
        "bool_bytes": json.dumps({url_a: {**_m1_url_record("a"), "byte_count": True}}),
    }
    for label, text in malformed.items():
        cache = tmp_path / f"umf_{label}"
        cache.mkdir()
        path = cache / "url_manifest.json"
        path.write_text(text, encoding="utf-8")
        before = path.read_bytes()
        for firewall in ({}, {"audit_nct_ids": audit, "allowed_nct_ids": allowed}):
            with pytest.raises(di.CacheIntegrityError) as info:
                di.DocumentCacheStore(cache, tmp_path / f"umf_{label}_index.jsonl", **firewall)
            assert re.search(r"NCT[0-9]{8}", str(info.value)) is None, label
        assert path.read_bytes() == before, label


def test_m1_fix_document_urls_are_tied_to_their_trial() -> None:
    """parse_ctgov_large_doc_url accepts only the canonical form; the record builder refuses any other url."""
    own = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    assert di.parse_ctgov_large_doc_url(own) == (_RT_A, "Prot_000.pdf")
    for variant in (
        own.replace("/61/", "/62/"),
        own.replace("https://", "http://"),
        own + "?v=2",
        own + "#page=2",
        own + "\n",
        own.replace("cdn.clinicaltrials.gov", "cdn.clinicaltrials.gov.example.org"),
        own.replace("Prot_000.pdf", "Prot_000.txt"),
        None,
        [own],
    ):
        assert di.parse_ctgov_large_doc_url(variant) is None, variant

    common: dict[str, Any] = {
        "nct_id": _RT_A,
        "filename": "Prot_000.pdf",
        "doc_type": "protocol",
        "http_status": 404,
        "entry": None,
        "audit_nct_ids": {_RT_AUDIT},
        "allowed_nct_ids": {_RT_A},
    }
    for foreign in (
        di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf"),
        di.build_ctgov_large_doc_url(_RT_A, "SAP_001.pdf"),
        own + "?v=2",
    ):
        with pytest.raises(di.AuditHoldoutError) as info:
            di.build_trial_document_record(url=foreign, **common)
        assert re.search(r"NCT[0-9]{8}", str(info.value)) is None
    assert di.build_trial_document_record(url=own, **common)["readability_status"] == "download_failed"


def test_m1_fix_study_alias_list_must_be_a_list_of_strict_ids() -> None:
    """A malformed nctIdAliases cannot be checked and is refused; a non-holdout alias is accepted."""
    digits = _RT_AUDIT[3:]
    for aliases, expected in (
        (_RT_AUDIT, di.AuditHoldoutError),
        ({"id": _RT_AUDIT}, di.AuditHoldoutError),
        ([_RT_AUDIT.lower()], di.InvalidNctIdError),
        ([_RT_AUDIT + " "], di.InvalidNctIdError),
        ([_RT_UNLISTED, _RT_AUDIT], di.AuditHoldoutError),
    ):
        study = _make_study(_RT_A, [])
        study["protocolSection"]["identificationModule"]["nctIdAliases"] = aliases
        with pytest.raises(expected) as info:
            di.validate_study_payload_nct_id(
                study, expected_nct_id=_RT_A, audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
            )
        assert digits not in str(info.value)

    study = _make_study(_RT_A, [])
    study["protocolSection"]["identificationModule"]["nctIdAliases"] = [_RT_UNLISTED]
    assert (
        di.validate_study_payload_nct_id(
            study, expected_nct_id=_RT_A, audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A, _RT_B}
        )
        == _RT_A
    )


class _WrongDigestProvider(di.FixturePageTextProvider):
    """A provider that labels the pages it extracts with a digest other than the one requested."""

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> di.ExtractedDocumentPages:
        got = super().extract_pages(sha256=sha256, pdf_path=pdf_path, nct_id=nct_id, filename=filename)
        return di.ExtractedDocumentPages(
            sha256="f" * 64, physical_pages=got.physical_pages, pages=got.pages, provider_name="wrong_digest"
        )


def test_m1_fix_pages_reported_for_another_digest_are_not_cached(tmp_path: Path) -> None:
    """get_or_index_document refuses a provider's pages when they are labelled with another digest."""
    sha = "a" * 64
    store = di.DocumentCacheStore(
        tmp_path / "wd_cache", tmp_path / "wd_index.jsonl", audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A}
    )
    provider = _WrongDigestProvider({sha: _clean_stat_pages(2)})
    with pytest.raises(di.CacheIntegrityError):
        store.get_or_index_document(sha256=sha, byte_count=5, provider=provider, nct_id=_RT_A)
    assert len(provider.calls) == 1
    assert store.load_cached_pages(sha) is None and store.load_cached_pages("f" * 64) is None
    assert not store.index_path.exists()


def test_m1_fix_ingest_max_trials_must_be_a_positive_int(tmp_path: Path) -> None:
    """max_trials other than None or an int >= 1 is refused before any row is read or indexed."""
    pdf = _make_minimal_pdf("Max trials")
    census = _write_jsonl(tmp_path / "mt_census.jsonl", [_rt_census_row(_RT_A, pdf)])
    provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(2)})
    store = di.DocumentCacheStore(
        tmp_path / "mt_cache", tmp_path / "mt_index.jsonl", audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A}
    )
    for bad in (0, -1, True, 1.0, "1"):
        with pytest.raises(di.DocumentIndexerError):
            di.ingest_legacy_census_documents(
                census,
                cache_store=store,
                provider=provider,
                audit_nct_ids={_RT_AUDIT},
                allowed_nct_ids={_RT_A},
                max_trials=bad,  # type: ignore[arg-type]
            )
    assert provider.calls == []
    assert not (tmp_path / "mt_cache").exists()


# ---------------------------------------------------------------------------
# Group 13: Red-team round 2 (m1)
#
# Each test states the behaviour the holdout firewall, the provenance rule or
# the no-silent-failure rule requires of one CLI path or public entry point,
# driven through fake API and session objects (no network) with fictional ids
# only. A failing test here names an open bypass: make it pass by fixing the
# module, never by weakening the assertion.
# ---------------------------------------------------------------------------


class _PdfPathRecordingProvider(di.FixturePageTextProvider):
    """A fixture provider that also records each ``(sha256, pdf_path)`` it is asked to extract."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        super().__init__(mapping)
        self.requests: list[tuple[str, Path | None]] = []

    def extract_pages(
        self,
        *,
        sha256: str,
        pdf_path: Path | None = None,
        nct_id: str | None = None,
        filename: str | None = None,
    ) -> di.ExtractedDocumentPages:
        self.requests.append((sha256.lower(), pdf_path))
        return super().extract_pages(sha256=sha256, pdf_path=pdf_path, nct_id=nct_id, filename=filename)


class _RedirectedResponse(_FakeResponse):
    """A fake response that carries the final ``url`` and redirect ``history`` requests would report."""

    def __init__(
        self,
        status_code: int,
        content: bytes,
        *,
        url: str,
        history: list[_FakeResponse],
        location: str | None = None,
    ) -> None:
        super().__init__(status_code, content)
        self.url = url
        self.history = history
        self.headers: dict[str, str] = {"Location": location} if location is not None else {}


class _RedirectingSession:
    """A session whose GET of any URL is redirected to ``target``, unless redirects are disabled.

    It honours ``allow_redirects=False`` the way requests does (the 302 itself
    is returned), so a fix that disables redirects and a fix that checks the
    final URL both satisfy the tests that use it.
    """

    def __init__(self, target: str, content: bytes) -> None:
        self._target = target
        self._content = content
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _RedirectedResponse:
        self.calls.append((url, dict(kwargs)))
        if kwargs.get("allow_redirects", True) is False:
            return _RedirectedResponse(302, b"", url=url, history=[], location=self._target)
        return _RedirectedResponse(
            200, self._content, url=self._target, history=[_FakeResponse(302)]
        )


def _rt_stat_pages() -> list[str]:
    """Two clean statistical pages, the page text every fixture document here is given."""
    return _clean_stat_pages(2)


# --- Open bypasses found in round 2 (these fail until the module is fixed) ---


def test_redteam_m1_local_pdf_symlinked_into_a_holdout_directory_is_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--local-pdf-dir`` entry for an in-scope trial that links into a holdout trial's folder is not used.

    Both the live path (``index_trial_from_study``) and the census ingest build
    ``<local-pdf-dir>/<nct_id>/<filename>`` and read whatever it points to. When
    the in-scope trial's file (or its whole folder) is a symlink into the
    holdout trial's folder, the holdout's PDF is read, hashed, handed to the
    page extractor and indexed under the in-scope trial, and its digest lands in
    the index, page map, url_manifest.json and profile (and, on a census digest
    mismatch, in the error message). Correct: a local PDF whose resolved path
    leaves ``<local-pdf-dir>/<nct_id>/`` is refused (or ignored) before it is
    read, so nothing about the holdout's file is parsed or recorded.
    """
    holdout_pdf = _make_minimal_pdf("Holdout trial local protocol file")
    own_pdf = _make_minimal_pdf("In-scope trial posted protocol file")
    holdout_sha, own_sha = di.sha256_bytes(holdout_pdf), di.sha256_bytes(own_pdf)
    own_url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    manifest = _rt_manifest(tmp_path)

    for layout in ("file_link", "dir_link"):
        pdfs = tmp_path / f"sl_{layout}_pdfs"
        (pdfs / _RT_AUDIT).mkdir(parents=True)
        holdout_file = pdfs / _RT_AUDIT / "Prot_000.pdf"
        holdout_file.write_bytes(holdout_pdf)
        if layout == "file_link":
            (pdfs / _RT_A).mkdir()
            (pdfs / _RT_A / "Prot_000.pdf").symlink_to(holdout_file)
        else:
            (pdfs / _RT_A).symlink_to(pdfs / _RT_AUDIT, target_is_directory=True)
        holdout_dir = (pdfs / _RT_AUDIT).resolve()

        runs: list[tuple[str, list[str], dict[str, Any]]] = [
            (
                f"sl_{layout}_live",
                ["--nct-id", _RT_A],
                {
                    "api_client": _SpyAPIClient({_RT_A: _rt_study(_RT_A, own_pdf)}),
                    "session": _FakeSession({own_url: _FakeResponse(200, own_pdf)}),
                },
            ),
        ]
        for claimed, claimed_sha in (("holdout_digest", holdout_sha), ("own_digest", own_sha)):
            census = _write_jsonl(
                tmp_path / f"sl_{layout}_{claimed}.jsonl",
                [_rt_census_row(_RT_A, own_pdf, sha256=claimed_sha, bytes=len(own_pdf))],
            )
            runs.append(
                (f"sl_{layout}_census_{claimed}", ["--ingest-census-jsonl", str(census), "--offline"], {})
            )

        for tag, target_args, injected in runs:
            provider = _PdfPathRecordingProvider({holdout_sha: _rt_stat_pages(), own_sha: _rt_stat_pages()})
            rc = cli_mod.run_cli(
                [
                    "--manifest", str(manifest),
                    *target_args,
                    "--local-pdf-dir", str(pdfs),
                    *_rt_outs(tmp_path, tag),
                ],
                provider=provider,
                **injected,
            )
            err = capsys.readouterr().err
            into_holdout = [
                str(path)
                for _sha, path in provider.requests
                if path is not None and holdout_dir in Path(path).resolve().parents
            ]
            assert into_holdout == [], f"{tag} (rc={rc}): holdout trial's file handed to the extractor"
            assert holdout_sha not in [sha for sha, _path in provider.requests], tag
            leaked = [p.name for p in _rt_written(tmp_path, tag) if holdout_sha in _rt_text(p)]
            assert leaked == [], f"{tag} (rc={rc}): holdout file's digest recorded in {leaked}"
            assert holdout_sha not in err, f"{tag}: holdout file's digest printed"
            assert _RT_AUDIT not in err


def test_redteam_m1_index_entry_does_not_bypass_a_holdout_tagged_page_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cached index entry must not serve a digest whose cached page map is tagged with a holdout trial.

    Without an index entry, an in-scope document whose digest has a cached
    page map tagged with a holdout trial stops the run with exit 2 (pinned in
    ``test_redteam_m1_page_map_rows_for_holdout_trials_refused_before_any_fetch``).
    With an entry for that digest in ``document_index_by_sha256.jsonl`` (which
    is untagged), ``get_or_index_document`` returns the entry as a cache hit
    before the page map is read, so the firewall never runs and the
    holdout-derived entry (readability, pages selected, version label) is
    profiled under the in-scope trial with exit 0. Correct: the same refusal
    with or without an index entry, in the live path and the census ingest.
    """
    pdf = _make_minimal_pdf("Digest whose page map is tagged with a holdout")
    sha = di.sha256_bytes(pdf)
    entry = di.index_document_pages(
        _rt_stat_pages(),
        sha256=sha,
        byte_count=len(pdf),
        text_provider="fixture",
        extracted_at=_RT_STAMP,
        pipeline_commit=None,
    )
    manifest = _rt_manifest(tmp_path)
    census = _write_jsonl(tmp_path / "ie_census.jsonl", [_rt_census_row(_RT_A, pdf)])
    for mode in ("live", "census"):
        tag = f"ie_{mode}"
        (tmp_path / f"{tag}_index.jsonl").write_text(json.dumps(entry.to_dict()) + "\n", encoding="utf-8")
        cached_map = _write_cached_map(
            tmp_path / f"{tag}_cache" / "pages",
            {
                "sha256": sha,
                "nct_id": _RT_AUDIT,
                "byte_count": len(pdf),
                "physical_pages": 2,
                "pages": _rt_stat_pages(),
            },
        )
        before = cached_map.read_bytes()
        if mode == "live":
            rc = cli_mod.run_cli(
                ["--manifest", str(manifest), "--nct-id", _RT_A, *_rt_outs(tmp_path, tag)],
                api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)}),
                session=_FakeSession(
                    {di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)}
                ),
            )
        else:
            rc = cli_mod.run_cli(
                [
                    "--manifest", str(manifest),
                    "--ingest-census-jsonl", str(census),
                    "--offline",
                    *_rt_outs(tmp_path, tag),
                ]
            )
        err = capsys.readouterr().err
        assert rc == 2, f"{mode}: rc={rc}, holdout-tagged digest served from the index"
        assert not (tmp_path / f"{tag}_profiles.jsonl").exists()
        assert cached_map.read_bytes() == before
        assert _RT_AUDIT not in err


def test_redteam_m1_download_redirected_to_another_trials_document_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A document GET that ends at another trial's URL must not be cached under the requested trial.

    ``download_pdf_atomic`` checks the URL it requests and then calls
    ``session.get(url, stream=True)``, which follows redirects, and never
    looks at the response's final ``url`` or ``history``. A redirect from the
    in-scope trial's document to a holdout trial's document puts the holdout's
    bytes into the content-addressed cache and the profile under the in-scope
    trial. Correct: refuse a response whose final URL is not the requested one
    (or disable redirects, so the 302 is a failed download); either way no
    blob is written and the holdout's bytes are not indexed.
    """
    own_url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    holdout_url = di.build_ctgov_large_doc_url(_RT_AUDIT, "Prot_000.pdf")
    holdout_pdf = _make_minimal_pdf("Holdout bytes reached through a redirect")
    holdout_sha = di.sha256_bytes(holdout_pdf)

    blobs = tmp_path / "rd_blobs"
    session = _RedirectingSession(holdout_url, holdout_pdf)
    try:
        result: Any = cli_mod.download_pdf_atomic(
            own_url,
            blobs,
            session=session,
            nct_id=_RT_A,
            filename="Prot_000.pdf",
            audit_nct_ids={_RT_AUDIT},
            allowed_nct_ids={_RT_A, _RT_B},
        )
    except di.AuditHoldoutError:
        result = None
    assert result is None or not result.ok, "redirected holdout bytes accepted as the trial's document"
    assert not blobs.exists() or [p for p in blobs.rglob("*") if p.is_file()] == []

    provider = di.FixturePageTextProvider({holdout_sha: _rt_stat_pages()})
    rc = cli_mod.run_cli(
        ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, *_rt_outs(tmp_path, "rd")],
        api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, holdout_pdf)}),
        session=_RedirectingSession(holdout_url, holdout_pdf),
        provider=provider,
    )
    err = capsys.readouterr().err
    assert holdout_sha not in [call[0] for call in provider.calls], f"rc={rc}: redirected bytes parsed"
    leaked = [p.name for p in _rt_written(tmp_path, "rd") if holdout_sha in _rt_text(p) or _RT_AUDIT in _rt_text(p)]
    assert leaked == [], f"rc={rc}: redirected holdout document recorded in {leaked}"
    assert _RT_AUDIT not in err


def test_redteam_m1_alias_unlisted_by_a_manifest_without_holdout_rows_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a manifest that names no holdout, a payload alias the manifest does not list is not accepted.

    ``validate_study_payload_nct_id`` checks ``nctIdAliases`` only against
    ``audit_nct_ids`` and accepts any alias the manifest does not list. A
    manifest holding only in-scope rows (a filtered run list) gives an empty
    holdout set, so the alias check compares against nothing: a payload for an
    in-scope trial whose alias is the holdout trial is downloaded, parsed and
    profiled with exit 0. The direct check is protected by the allowlist even
    then; the alias check is not. Correct: when no holdout set is known, an
    alias not on the allowlist cannot be shown not to be a holdout and is
    refused (exit 2, nothing fetched from the CDN, parsed or written), or a
    manifest naming no in_audit=True row is refused outright. (With a holdout
    set, an unlisted alias stays accepted, as
    ``test_m1_fix_study_alias_list_must_be_a_list_of_strict_ids`` pins.)
    """
    pdf = _make_minimal_pdf("Alias against an empty holdout set")
    in_scope_only = _write_jsonl(
        tmp_path / "in_scope_only.jsonl",
        [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_B, "in_audit": False}],
    )
    study = _rt_study(_RT_A, pdf)
    study["protocolSection"]["identificationModule"]["nctIdAliases"] = [_RT_AUDIT]
    session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)})
    provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _rt_stat_pages()})
    rc = cli_mod.run_cli(
        ["--manifest", str(in_scope_only), "--nct-id", _RT_A, *_rt_outs(tmp_path, "aliasx")],
        api_client=_SpyAPIClient({_RT_A: study}),
        session=session,
        provider=provider,
    )
    err = capsys.readouterr().err
    assert session.requested_urls == [], f"rc={rc}: document of an unverifiable alias requested"
    assert provider.calls == []
    assert rc == 2
    assert _rt_written(tmp_path, "aliasx") == []
    assert _RT_AUDIT not in err


def test_redteam_m1_census_row_fields_are_validated_not_carried_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Census fields other than ``nct_id`` and ``url`` are type-checked, not copied into the profile as given.

    The ingest checks a row's ``nct_id`` and ``url`` and then copies
    ``http_status`` (into ``download_error``), ``upload_date``,
    ``api_doc_date`` and ``retrieved_at`` into the profile unvalidated. A row
    whose fields carry a holdout trial's id writes that id into the profiles
    file, and a malformed status is silently reclassified: ``"200"`` (a
    string) or ``true`` makes a downloaded document ``download_failed``, and a
    200 row with no digest becomes ``download_failed`` with the error
    ``legacy_http_status_200``. Correct: such a row is refused (nonzero exit,
    nothing written), as a row missing ``url`` is.
    """
    pdf = _make_minimal_pdf("Census row fields")
    variants: dict[str, dict[str, Any]] = {
        "http_status_names_holdout": {"http_status": _RT_AUDIT, "sha256": None, "bytes": None},
        "upload_date_object": {"upload_date": {"nct_id": _RT_AUDIT}},
        "api_doc_date_list": {"api_doc_date": [_RT_AUDIT]},
        "retrieved_at_not_a_timestamp": {"retrieved_at": _RT_AUDIT},
        "http_status_string_200": {"http_status": "200"},
        "http_status_bool": {"http_status": True},
        "http_200_without_digest": {"sha256": None, "bytes": None},
    }
    for label, overrides in variants.items():
        census = _write_jsonl(tmp_path / f"cf_{label}.jsonl", [_rt_census_row(_RT_A, pdf, **overrides)])
        provider = di.FixturePageTextProvider({di.sha256_bytes(pdf): _rt_stat_pages()})
        tag = f"cf_{label}"
        rc = cli_mod.run_cli(
            [
                "--manifest", str(_rt_manifest(tmp_path)),
                "--ingest-census-jsonl", str(census),
                "--offline",
                *_rt_outs(tmp_path, tag),
            ],
            provider=provider,
        )
        err = capsys.readouterr().err
        leaked = [p.name for p in _rt_written(tmp_path, tag) if _RT_AUDIT in _rt_text(p)]
        assert leaked == [], f"{label}: holdout id recorded in {leaked}"
        assert rc != 0, f"{label}: malformed census row accepted"
        assert _rt_written(tmp_path, tag) == [], label
        assert _RT_AUDIT not in err


def test_redteam_m1_census_failure_after_pass_one_leaves_no_cache_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A census row that fails in the indexing pass must not leave earlier trials cached with no profile.

    ``ingest_legacy_census_documents`` says pass 1 validates every row "so a
    bad row stops the ingest with no cache writes", but a downloaded row's
    ``bytes`` and its local PDF's digest are checked only in pass 2, and a
    document no provider can extract raises from pass 2 too. Trials sorted
    before the bad one are indexed first: their page maps and index entries
    are written, then the run exits 1 with no profiles file. (The live path
    records such a document as ``extraction_failed`` instead.) Correct: a
    refused ingest writes nothing; an ingest that records an unextractable
    document as ``extraction_failed`` profiles every trial.
    """
    pdf_a, pdf_b = _make_minimal_pdf("Census pass two A"), _make_minimal_pdf("Census pass two B")
    sha_a, sha_b = di.sha256_bytes(pdf_a), di.sha256_bytes(pdf_b)
    local = tmp_path / "p2_pdfs"
    (local / _RT_B).mkdir(parents=True)
    (local / _RT_B / "Prot_000.pdf").write_bytes(_make_minimal_pdf("Not the bytes the census row names"))
    variants: dict[str, tuple[dict[str, Any], dict[str, list[str]], list[str]]] = {
        "bytes_zero": ({"bytes": 0}, {sha_a: _rt_stat_pages(), sha_b: _rt_stat_pages()}, []),
        "bytes_missing": ({"bytes": None}, {sha_a: _rt_stat_pages(), sha_b: _rt_stat_pages()}, []),
        "local_pdf_digest_mismatch": (
            {},
            {sha_a: _rt_stat_pages(), sha_b: _rt_stat_pages()},
            ["--local-pdf-dir", str(local)],
        ),
        "unextractable": ({}, {sha_a: _rt_stat_pages()}, []),
    }
    for label, (b_overrides, pages, extra) in variants.items():
        census = _write_jsonl(
            tmp_path / f"p2_{label}.jsonl",
            [_rt_census_row(_RT_A, pdf_a), _rt_census_row(_RT_B, pdf_b, **b_overrides)],
        )
        tag = f"p2_{label}"
        rc = cli_mod.run_cli(
            [
                "--manifest", str(_rt_manifest(tmp_path)),
                "--ingest-census-jsonl", str(census),
                "--offline",
                *extra,
                *_rt_outs(tmp_path, tag),
            ],
            provider=di.FixturePageTextProvider(pages),
        )
        capsys.readouterr()
        if label == "unextractable" and rc == 0:
            profiles = {
                json.loads(line)["nct_id"]: json.loads(line)
                for line in (tmp_path / f"{tag}_profiles.jsonl").read_text(encoding="utf-8").splitlines()
            }
            assert sorted(profiles) == [_RT_A, _RT_B]
            assert profiles[_RT_B]["documents"][0]["readability_status"] == "extraction_failed"
            continue
        assert rc != 0, label
        written = [p.name for p in _rt_written(tmp_path, tag)]
        assert written == [], f"{label}: refused ingest left {written}"


def test_redteam_m1_one_trial_file_is_recorded_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trial's posted file is one document: duplicate or conflicting sources are not all recorded.

    Two census rows for one trial's ``Prot_000.pdf`` with different digests
    are both indexed and both written into that trial's profile (exit 0),
    though the page-map loader refuses exactly this conflict (one trial's file
    mapped to two digests). A CT.gov payload listing one filename twice is
    downloaded twice and profiled as two documents, here a protocol and a SAP.
    A consumer keyed by ``(nct_id, filename)`` resolves either silently.
    Correct: conflicting rows are refused (nonzero exit, nothing written), and
    no profile carries one filename twice.
    """
    pdf_1, pdf_2 = _make_minimal_pdf("First version"), _make_minimal_pdf("Second version")
    pages = {di.sha256_bytes(pdf_1): _rt_stat_pages(), di.sha256_bytes(pdf_2): _rt_stat_pages()}
    conflicting = _write_jsonl(
        tmp_path / "dup_conflict.jsonl", [_rt_census_row(_RT_A, pdf_1), _rt_census_row(_RT_A, pdf_2)]
    )
    rc = cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--ingest-census-jsonl", str(conflicting),
            "--offline",
            *_rt_outs(tmp_path, "dupc"),
        ],
        provider=di.FixturePageTextProvider(pages),
    )
    capsys.readouterr()
    assert rc != 0, "one trial's file given two digests was accepted"
    assert _rt_written(tmp_path, "dupc") == []

    identical = _write_jsonl(
        tmp_path / "dup_same.jsonl", [_rt_census_row(_RT_A, pdf_1), _rt_census_row(_RT_A, pdf_1)]
    )
    study = _make_study(
        _RT_A,
        [
            {"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False, "size": len(pdf_1)},
            {"filename": "Prot_000.pdf", "hasProtocol": False, "hasSap": True, "size": len(pdf_1)},
        ],
    )
    session = _FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf_1)})
    for tag, argv, injected in (
        ("dups", ["--ingest-census-jsonl", str(identical), "--offline"], {}),
        (
            "dupp",
            ["--nct-id", _RT_A],
            {"api_client": _SpyAPIClient({_RT_A: study}), "session": session},
        ),
    ):
        cli_mod.run_cli(
            ["--manifest", str(_rt_manifest(tmp_path)), *argv, *_rt_outs(tmp_path, tag)],
            provider=di.FixturePageTextProvider(pages),
            **injected,
        )
        capsys.readouterr()
        path = tmp_path / f"{tag}_profiles.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for line in lines:
            names = [doc["filename"] for doc in json.loads(line)["documents"]]
            assert len(names) == len(set(names)), f"{tag}: one file recorded as {len(names)} documents"
    assert len(session.requested_urls) <= 1, f"one posted file downloaded {len(session.requested_urls)} times"


def test_redteam_m1_document_index_file_is_read_strictly(tmp_path: Path) -> None:
    """``document_index_by_sha256.jsonl`` is refused when ambiguous or malformed, never coerced.

    The module states that a cache JSON object naming a key twice is refused,
    but ``DocumentCacheStore`` reads the index with plain ``json.loads`` and
    ``DocumentIndexEntry.from_dict`` coerces with ``int()``/``str()``: a
    repeated ``readability_status`` is read last-wins, ``byte_count`` 12.7
    becomes 12, ``"12"`` becomes 12, a null ``extracted_at`` becomes the text
    ``"None"`` (a fabricated provenance stamp), and two lines for one
    ``(sha256, indexer_rules_version)`` that disagree are resolved last-wins.
    The next ``put_entry`` rewrites the file with the coerced values. Correct:
    opening the store raises ``DocumentIndexerError`` and the file is left as
    it is.
    """
    pdf = _make_minimal_pdf("Index entry")
    sha = di.sha256_bytes(pdf)
    record = di.index_document_pages(
        _rt_stat_pages(),
        sha256=sha,
        byte_count=len(pdf),
        text_provider="fixture",
        extracted_at=_RT_STAMP,
        pipeline_commit=None,
    ).to_dict()
    line = json.dumps(record)
    variants = {
        "repeated_key": line[:-1] + ', "readability_status": "image_only_scan"}\n',
        "fractional_byte_count": json.dumps({**record, "byte_count": len(pdf) + 0.7}) + "\n",
        "string_byte_count": json.dumps({**record, "byte_count": str(len(pdf))}) + "\n",
        "null_extracted_at": json.dumps({**record, "extracted_at": None}) + "\n",
        "conflicting_lines": line
        + "\n"
        + json.dumps({**record, "readability_status": "image_only_scan"})
        + "\n",
    }
    for label, text in variants.items():
        index_path = tmp_path / f"strict_index_{label}.jsonl"
        index_path.write_text(text, encoding="utf-8")
        before = index_path.read_bytes()
        loaded: Any = None
        with pytest.raises(di.DocumentIndexerError):
            store = di.DocumentCacheStore(tmp_path / f"strict_cache_{label}", index_path)
            loaded = store.get_entry(sha)
        assert loaded is None, f"{label}: read as {loaded}"
        assert index_path.read_bytes() == before, label


def test_redteam_m1_cached_blob_must_match_its_digest_before_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cached ``blobs/<sha>.pdf`` is handed to the page extractor only if its bytes hash to ``<sha>``.

    The url_manifest fast path takes ``get_blob_path(sha)`` as the document's
    PDF with no hash check, and ``download_pdf_atomic`` keeps an existing blob
    at the downloaded digest's path (discarding the fresh bytes) and returns
    that file. A blob whose bytes are not its digest's (a damaged file, or one
    swapped for another trial's document) is then parsed and cached as that
    digest's pages. Correct: the blob is verified (refused with
    ``CacheIntegrityError``, or replaced by the verified download) before any
    extractor sees it.
    """
    own_pdf = _make_minimal_pdf("The in-scope trial's real protocol bytes")
    sha = di.sha256_bytes(own_pdf)
    wrong_bytes = _make_minimal_pdf("Different bytes sitting in the blob slot")
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    upload = "01/02/2020 10:00"
    study = _make_study(
        _RT_A,
        [{"filename": "Prot_000.pdf", "hasProtocol": True, "hasSap": False, "size": len(own_pdf), "uploadDate": upload}],
    )
    for route in ("url_manifest_fast_path", "download_onto_existing_blob"):
        tag = f"blob_{route}"
        cache = tmp_path / f"{tag}_cache"
        blob = cache / "blobs" / sha[:2] / f"{sha}.pdf"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(wrong_bytes)
        if route == "url_manifest_fast_path":
            (cache / "url_manifest.json").write_text(
                json.dumps(
                    {
                        url: {
                            "sha256": sha,
                            "byte_count": len(own_pdf),
                            "api_size": len(own_pdf),
                            "api_upload_date": upload,
                            "verified_at": _RT_STAMP,
                            "pipeline_commit": None,
                        }
                    }
                ),
                encoding="utf-8",
            )
        provider = _PdfPathRecordingProvider({sha: _rt_stat_pages()})
        rc = cli_mod.run_cli(
            ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, *_rt_outs(tmp_path, tag)],
            api_client=_SpyAPIClient({_RT_A: study}),
            session=_FakeSession({url: _FakeResponse(200, own_pdf)}),
            provider=provider,
        )
        capsys.readouterr()
        mismatched = [
            str(path)
            for requested, path in provider.requests
            if path is not None and di.sha256_file(Path(path))[0] != requested
        ]
        assert mismatched == [], f"{route} (rc={rc}): extractor handed a file that is not its digest's bytes"


def test_redteam_m1_output_paths_may_not_overwrite_inputs_or_each_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--profiles-out``/``--index-out`` naming an input file or each other are refused before any fetch.

    Nothing compares the output paths with the inputs: ``--profiles-out``
    equal to the ``--manifest`` path overwrites the manifest with profiles
    (its in_audit=True rows, the holdout set, are gone) and exits 0;
    equal to ``--ingest-census-jsonl`` or a ``--page-map-jsonl`` file it
    destroys that input; ``--index-out`` equal to ``--profiles-out`` leaves an
    index the next run cannot open; ``--index-out`` equal to the manifest
    makes the cache store parse the manifest as an index and die with an
    uncaught ``KeyError``. Correct: exit 2 before anything is
    fetched, read or written, every input left byte for byte (a path spelled
    differently but naming the same file included).
    """
    pdf = _make_minimal_pdf("Output aliasing")
    sha = di.sha256_bytes(pdf)
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    (tmp_path / "oa_sub").mkdir()

    def _case(tag: str) -> tuple[Path, Path, Path, Path]:
        manifest = _rt_manifest(tmp_path, f"{tag}_manifest.jsonl")
        census = _write_jsonl(tmp_path / f"{tag}_census.jsonl", [_rt_census_row(_RT_A, pdf)])
        page_map = _write_jsonl(
            tmp_path / f"{tag}_pm.jsonl", [{"nct_id": _RT_A, "sha256": sha, "pages": _rt_stat_pages()}]
        )
        return manifest, census, page_map, tmp_path / f"{tag}_cache"

    cases: list[tuple[str, str, str]] = [
        ("profiles_is_manifest", "--profiles-out", "manifest"),
        ("profiles_is_manifest_respelled", "--profiles-out", "manifest_respelled"),
        ("profiles_is_page_map", "--profiles-out", "page_map"),
        ("profiles_is_census", "--profiles-out", "census"),
        ("index_is_profiles", "--index-out", "profiles"),
        ("index_is_manifest", "--index-out", "manifest"),
    ]
    for tag, flag, target in cases:
        manifest, census, page_map, cache = _case(tag)
        profiles = tmp_path / f"{tag}_profiles.jsonl"
        index = tmp_path / f"{tag}_index.jsonl"
        named = {
            "manifest": str(manifest),
            "manifest_respelled": str(tmp_path / "oa_sub" / ".." / manifest.name),
            "page_map": str(page_map),
            "census": str(census),
            "profiles": str(profiles),
        }[target]
        if flag == "--profiles-out":
            profiles_arg, index_arg = named, str(index)
        else:
            profiles_arg, index_arg = str(profiles), named
        if target == "census":
            targets = ["--ingest-census-jsonl", str(census), "--offline"]
        else:
            targets = ["--nct-id", _RT_A, "--page-map-jsonl", str(page_map)]
        inputs = {path: path.read_bytes() for path in (manifest, census, page_map)}
        api = _SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)})
        session = _FakeSession({url: _FakeResponse(200, pdf)})
        rc = cli_mod.run_cli(
            [
                "--manifest", str(manifest),
                *targets,
                "--cache-dir", str(cache),
                "--index-out", index_arg,
                "--profiles-out", profiles_arg,
                "--rate-limit-sec", "0",
            ],
            api_client=api,
            session=session,
            provider=di.FixturePageTextProvider({sha: _rt_stat_pages()}),
        )
        capsys.readouterr()
        changed = [path.name for path, data in inputs.items() if path.read_bytes() != data]
        assert changed == [], f"{tag} (rc={rc}): input overwritten: {changed}"
        assert rc == 2, tag
        assert api.requested_ncts == [] and session.requested_urls == [], tag
        assert not profiles.exists() and not index.exists() and not cache.exists(), tag


def test_redteam_m1_invalid_numeric_options_refused_before_any_fetch_or_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Page-selection bounds and the rate limit are checked before the first request, like ``--limit``.

    ``--min-selected-pages 30 --max-selected-pages 5`` (or a bound below 1)
    is refused only inside ``index_document_pages``, after the CT.gov record
    was fetched, the PDF downloaded and its blob, url_manifest.json entry and
    page map written; the run then exits 1. ``--rate-limit-sec -1`` and
    ``nan`` are taken as 0 (no rate limit) without a word. Correct: exit 2
    with nothing fetched or written.
    """
    pdf = _make_minimal_pdf("Numeric options")
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    variants: dict[str, list[str]] = {
        "min_above_max": ["--min-selected-pages", "30", "--max-selected-pages", "5"],
        "min_zero": ["--min-selected-pages", "0"],
        "min_negative": ["--min-selected-pages=-3"],
        "max_zero": ["--min-selected-pages", "1", "--max-selected-pages", "0"],
        "rate_negative": ["--rate-limit-sec=-1"],
        "rate_nan": ["--rate-limit-sec", "nan"],
    }
    for label, extra in variants.items():
        tag = f"num_{label}"
        outs = _rt_outs(tmp_path, tag)
        if any(arg.startswith("--rate-limit-sec") for arg in extra):
            cut = outs.index("--rate-limit-sec")
            outs = outs[:cut] + outs[cut + 2 :]
        api = _SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)})
        session = _FakeSession({url: _FakeResponse(200, pdf)})
        rc = cli_mod.run_cli(
            ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, *extra, *outs],
            api_client=api,
            session=session,
            provider=di.FixturePageTextProvider({di.sha256_bytes(pdf): _clean_stat_pages(12)}),
        )
        capsys.readouterr()
        assert rc == 2, f"{label}: rc={rc}"
        assert api.requested_ncts == [] and session.requested_urls == [], label
        assert _rt_written(tmp_path, tag) == [], label


# --- Attempts that held in round 2 (these pass; they pin the behaviour) ---


def test_redteam_m1_extra_keys_naming_a_holdout_never_select_or_record_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Holdout or excluded ids outside the checked fields are never fetched, and never written.

    A manifest shaped like the real outcomes index (``rows`` plus an
    ``excluded`` list and metadata keys) whose in-scope row carries extra keys
    naming the holdout; a census row with an extra key naming it; a page-map
    row with extra keys naming it. Only the in-scope trial is fetched, an
    explicit ``--nct-id`` for an excluded trial exits 2, and no written file
    carries the holdout or excluded id.
    """
    real_shape = {
        "schema_version": 1,
        "run_id": "outcomes-fixture",
        "columns_note": "fixture",
        "rows": [
            {"nct_id": _RT_A, "in_audit": False, "aliases": [_RT_AUDIT], "record": {"nct_id": _RT_AUDIT}},
            {"nct_id": _RT_AUDIT, "in_audit": True},
        ],
        "excluded": [{"nct_id": _RT_UNLISTED, "reasons": ["fixture"]}],
    }
    manifest = tmp_path / "xk_manifest.json"
    manifest.write_text(json.dumps(real_shape), encoding="utf-8")
    api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
    rc = cli_mod.run_cli(["--manifest", str(manifest), *_rt_outs(tmp_path, "xk_rows")], api_client=api, session=_FakeSession({}))
    err = capsys.readouterr().err
    assert rc == 0 and api.requested_ncts == [_RT_A]
    assert [p.name for p in _rt_written(tmp_path, "xk_rows") if _RT_AUDIT in _rt_text(p) or _RT_UNLISTED in _rt_text(p)] == []
    assert _RT_AUDIT not in err

    api = _SpyAPIClient({_RT_UNLISTED: _make_study(_RT_UNLISTED, [])})
    rc = cli_mod.run_cli(
        ["--manifest", str(manifest), "--nct-id", _RT_UNLISTED, *_rt_outs(tmp_path, "xk_excluded")],
        api_client=api,
        session=_FakeSession({}),
    )
    capsys.readouterr()
    assert rc == 2 and api.requested_ncts == [] and _rt_written(tmp_path, "xk_excluded") == []

    pdf = _make_minimal_pdf("Extra keys")
    sha = di.sha256_bytes(pdf)
    census = _write_jsonl(tmp_path / "xk_census.jsonl", [_rt_census_row(_RT_A, pdf, source_nct_id=_RT_AUDIT)])
    rc = cli_mod.run_cli(
        ["--manifest", str(_rt_manifest(tmp_path)), "--ingest-census-jsonl", str(census), "--offline", *_rt_outs(tmp_path, "xk_census")],
        provider=di.FixturePageTextProvider({sha: _rt_stat_pages()}),
    )
    capsys.readouterr()
    assert rc == 0
    assert [p.name for p in _rt_written(tmp_path, "xk_census") if _RT_AUDIT in _rt_text(p)] == []

    page_map = _write_jsonl(
        tmp_path / "xk_pm.jsonl",
        [
            {
                "nct_id": _RT_A,
                "sha256": sha,
                "filename": "Prot_000.pdf",
                "pages": _rt_stat_pages(),
                "source_nct_id": _RT_AUDIT,
                "nct_id_aliases": [_RT_AUDIT],
            }
        ],
    )
    rc = cli_mod.run_cli(
        ["--manifest", str(_rt_manifest(tmp_path)), "--nct-id", _RT_A, "--page-map-jsonl", str(page_map), *_rt_outs(tmp_path, "xk_pm")],
        api_client=_SpyAPIClient({_RT_A: _rt_study(_RT_A, pdf)}),
        session=_FakeSession({di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf"): _FakeResponse(200, pdf)}),
    )
    capsys.readouterr()
    assert rc == 0
    assert [p.name for p in _rt_written(tmp_path, "xk_pm") if _RT_AUDIT in _rt_text(p)] == []


def test_redteam_m1_abbreviated_repeat_and_payload_flags_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An abbreviated second ``--manifest`` is still a repeat; a payload ``in_audit`` that is not ``false`` is refused.

    argparse accepts unique prefixes (``--man``, ``--manif=``), and both reach
    the same once-only action, so the repeat is refused with exit 2 and no
    fetch. A CT.gov payload carrying ``in_audit`` as ``"false"``, 0, null or
    true stops the run with exit 2 and no profile.
    """
    canon = _write_jsonl(
        tmp_path / "ab_canon.jsonl",
        [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": True}],
    )
    other = _write_jsonl(
        tmp_path / "ab_other.jsonl",
        [{"nct_id": _RT_A, "in_audit": False}, {"nct_id": _RT_AUDIT, "in_audit": False}],
    )
    for index, argv in enumerate(
        (["--man", str(canon), "--manifest", str(other)], ["--manifest", str(canon), f"--manif={other}"])
    ):
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, []), _RT_AUDIT: _make_study(_RT_AUDIT, [])})
        tag = f"ab_{index}"
        rc = cli_mod.run_cli([*argv, "--nct-id", _RT_AUDIT, *_rt_outs(tmp_path, tag)], api_client=api, session=_FakeSession({}))
        err = capsys.readouterr().err
        assert rc == 2 and api.requested_ncts == [] and _rt_written(tmp_path, tag) == []
        assert _RT_AUDIT not in err

    for index, value in enumerate(("false", 0, None, True)):
        study = _make_study(_RT_A, [])
        study["in_audit"] = value
        tag = f"ab_payload_{index}"
        rc = cli_mod.run_cli(
            ["--manifest", str(canon), "--nct-id", _RT_A, *_rt_outs(tmp_path, tag)],
            api_client=_SpyAPIClient({_RT_A: study}),
            session=_FakeSession({}),
        )
        capsys.readouterr()
        assert rc == 2, repr(value)
        assert not (tmp_path / f"{tag}_profiles.jsonl").exists()


# ---------------------------------------------------------------------------
# Group 14: Round-2 fixes, behaviour beyond the red-team probes
#
# The probes above assert that each bypass is closed. These pin the edges of
# the fixes: what is still accepted, and the paths the probes do not drive.
# Fictional ids only, fake sessions only, no network.
# ---------------------------------------------------------------------------


def test_m1_r2_local_pdf_resolution_edges(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A link to a sibling file in the same trial folder is used; a dangling link and a missing directory are refused."""
    pdfs = tmp_path / "edge_pdfs"
    (pdfs / _RT_A).mkdir(parents=True)
    real = pdfs / _RT_A / "Prot_000_v2.pdf"
    real.write_bytes(_make_minimal_pdf("Sibling file of the same trial"))
    (pdfs / _RT_A / "Prot_000.pdf").symlink_to(real)
    assert di.resolve_local_trial_pdf(pdfs, _RT_A, "Prot_000.pdf") == real.resolve()
    assert di.resolve_local_trial_pdf(pdfs, _RT_B, "Prot_000.pdf") is None
    assert di.resolve_local_trial_pdf(pdfs, _RT_A, "SAP_001.pdf") is None

    (pdfs / _RT_A / "SAP_001.pdf").symlink_to(pdfs / _RT_A / "gone.pdf")
    with pytest.raises(di.AuditHoldoutError):
        di.resolve_local_trial_pdf(pdfs, _RT_A, "SAP_001.pdf")
    with pytest.raises(di.DocumentIndexerError):
        di.resolve_local_trial_pdf(tmp_path / "no_such_dir", _RT_A, "Prot_000.pdf")

    api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
    rc = cli_mod.run_cli(
        [
            "--manifest", str(_rt_manifest(tmp_path)),
            "--nct-id", _RT_A,
            "--local-pdf-dir", str(tmp_path / "no_such_dir"),
            *_rt_outs(tmp_path, "edge_missing_dir"),
        ],
        api_client=api,
        session=_FakeSession({}),
    )
    capsys.readouterr()
    assert rc == 2 and api.requested_ncts == [] and _rt_written(tmp_path, "edge_missing_dir") == []


def test_m1_r2_download_never_follows_or_accepts_a_redirect(tmp_path: Path) -> None:
    """``allow_redirects=False`` reaches a requests session; a session that cannot take it is checked by url/history."""
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    other = di.build_ctgov_large_doc_url(_RT_B, "Prot_000.pdf")
    pdf = _make_minimal_pdf("Redirect edges")
    seen: list[dict[str, Any]] = []

    class _RequestsSession(cli_mod.requests.Session):
        def get(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
            seen.append(kwargs)
            return _FakeResponse(302)

    result = cli_mod.download_pdf_atomic(
        url, tmp_path / "blobs_a", session=_RequestsSession(), nct_id=_RT_A, filename="Prot_000.pdf"
    )
    assert seen == [{"timeout": cli_mod.DEFAULT_TIMEOUT_SEC, "stream": True, "allow_redirects": False}]
    assert not result.ok and result.error == "http_status_302" and result.http_status == 302

    class _NarrowSession:
        """A ``get`` without ``allow_redirects``: redirects cannot be switched off, so the response is checked."""

        def __init__(self, response: Any) -> None:
            self.response = response
            self.calls = 0

        def get(self, url: str, timeout: int = 60, stream: bool = True) -> Any:
            self.calls += 1
            return self.response

    for label, response in (
        ("history", _RedirectedResponse(200, pdf, url=url, history=[_FakeResponse(302)])),
        ("final_url", _RedirectedResponse(200, pdf, url=other, history=[])),
    ):
        blobs = tmp_path / f"blobs_{label}"
        session = _NarrowSession(response)
        result = cli_mod.download_pdf_atomic(
            url, blobs, session=session, nct_id=_RT_A, filename="Prot_000.pdf"
        )
        assert session.calls == 1
        assert not result.ok and result.error == "redirected_response_refused", label
        assert result.sha256 is None and not blobs.exists(), label
        assert _RT_B not in (result.error or "")

    same = _NarrowSession(_RedirectedResponse(200, pdf, url=url, history=[]))
    result = cli_mod.download_pdf_atomic(
        url, tmp_path / "blobs_same", session=same, nct_id=_RT_A, filename="Prot_000.pdf"
    )
    assert result.ok and result.sha256 == di.sha256_bytes(pdf)


def test_m1_r2_census_dates_failed_rows_and_local_byte_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Census dates must be CT.gov dates; a failed row keeps its checked dates; a local PDF's size must match its row."""
    pdf = _make_minimal_pdf("Census date and size edges")
    sha = di.sha256_bytes(pdf)
    manifest = _rt_manifest(tmp_path)

    def _run(tag: str, rows: list[dict[str, Any]], extra: Sequence[str] = ()) -> int:
        census = _write_jsonl(tmp_path / f"{tag}.jsonl", rows)
        rc = cli_mod.run_cli(
            ["--manifest", str(manifest), "--ingest-census-jsonl", str(census), "--offline", *extra, *_rt_outs(tmp_path, tag)],
            provider=di.FixturePageTextProvider({sha: _rt_stat_pages()}),
        )
        capsys.readouterr()
        return rc

    assert _run("cd_text_date", [_rt_census_row(_RT_A, pdf, upload_date="uploaded " + _RT_AUDIT)]) == 1
    assert _rt_written(tmp_path, "cd_text_date") == []
    assert _run("cd_status_range", [_rt_census_row(_RT_A, pdf, http_status=42)]) == 1

    accepted = [
        _rt_census_row(_RT_A, pdf, upload_date="2020-01-02T10:00", api_doc_date="2019-12"),
        _rt_census_row(
            _RT_B, pdf, http_status=404, sha256=None, bytes=None, upload_date="01/02/2020 10:00", api_doc_date=None
        ),
    ]
    assert _run("cd_ok", accepted) == 0
    profiles = {
        json.loads(line)["nct_id"]: json.loads(line)
        for line in (tmp_path / "cd_ok_profiles.jsonl").read_text(encoding="utf-8").splitlines()
    }
    assert profiles[_RT_A]["documents"][0]["upload_date"] == "2020-01-02T10:00"
    failed = profiles[_RT_B]["documents"][0]
    assert failed["readability_status"] == "download_failed"
    assert failed["download_error"] == "legacy_http_status_404"
    assert failed["upload_date"] == "01/02/2020 10:00" and failed["api_doc_date"] is None

    local = tmp_path / "cd_pdfs"
    (local / _RT_A).mkdir(parents=True)
    (local / _RT_A / "Prot_000.pdf").write_bytes(pdf)
    assert _run("cd_local_ok", [_rt_census_row(_RT_A, pdf)], ["--local-pdf-dir", str(local)]) == 0
    assert _run("cd_local_size", [_rt_census_row(_RT_A, pdf, bytes=len(pdf) + 1)], ["--local-pdf-dir", str(local)]) == 1
    assert _rt_written(tmp_path, "cd_local_size") == []


def test_m1_r2_staged_writes_commit_on_success_and_roll_back_on_error(tmp_path: Path) -> None:
    """``staged_writes`` holds page maps, index lines and url records until the block succeeds."""
    pdf = _make_minimal_pdf("Staged writes")
    sha = di.sha256_bytes(pdf)
    url = di.build_ctgov_large_doc_url(_RT_A, "Prot_000.pdf")
    index = tmp_path / "staged_index.jsonl"
    store = di.DocumentCacheStore(tmp_path / "staged_cache", index, audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A})

    def _index_one() -> None:
        entry, hit = store.get_or_index_document(
            sha256=sha,
            byte_count=len(pdf),
            provider=di.FixturePageTextProvider({sha: _rt_stat_pages()}),
            nct_id=_RT_A,
            filename="Prot_000.pdf",
            extracted_at=_RT_STAMP,
        )
        assert not hit and store.get_entry(sha) == entry
        assert store.load_cached_pages(sha) is not None  # staged map is visible inside the block
        store.record_url_manifest(url, sha256=sha, byte_count=len(pdf), api_size=None, api_upload_date=None)

    with pytest.raises(RuntimeError):
        with store.staged_writes():
            _index_one()
            raise RuntimeError("abort after the writes")
    assert not (tmp_path / "staged_cache").exists() and not index.exists()
    assert store.get_entry(sha) is None and store.lookup_url_manifest(url) is None

    with store.staged_writes():
        _index_one()
        assert not index.exists()
    assert index.exists() and store.page_map_path(sha).exists() and store.url_manifest_path.exists()
    reopened = di.DocumentCacheStore(tmp_path / "staged_cache", index, audit_nct_ids={_RT_AUDIT}, allowed_nct_ids={_RT_A})
    assert reopened.get_entry(sha) == store.get_entry(sha)


def test_m1_r2_document_index_extra_keys_and_bool_counts_refused(tmp_path: Path) -> None:
    """An index line with an unknown key (dropped on rewrite) or a bool where an int belongs is refused; a clean line loads."""
    pdf = _make_minimal_pdf("Index strictness edges")
    sha = di.sha256_bytes(pdf)
    record = di.index_document_pages(
        _rt_stat_pages(), sha256=sha, byte_count=len(pdf), text_provider="fixture", extracted_at=_RT_STAMP
    ).to_dict()
    assert di.DocumentIndexEntry.from_dict(record).to_dict() == record
    slice_missing_offset = {k: v for k, v in record["section_slice"].items() if k != "printed_page_offset"}
    variants = {
        "extra_top_key": {**record, "note": "kept by nothing"},
        "extra_nested_key": {**record, "redaction": {**record["redaction"], "note": 1}},
        "bool_physical_pages": {**record, "physical_pages": True},
        "missing_offset": {**record, "section_slice": slice_missing_offset},
        "has_toc_int": {**record, "section_slice": {**record["section_slice"], "has_toc": 1}},
        "uppercase_sha": {**record, "sha256": sha.upper()},
    }
    for label, bad in variants.items():
        path = tmp_path / f"edge_index_{label}.jsonl"
        path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(di.CacheIntegrityError):
            di.DocumentCacheStore(tmp_path / f"edge_cache_{label}", path)
        assert path.read_bytes() == before, label


def test_m1_r2_output_paths_inside_the_cache_or_a_directory_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An output inside the cache's pages/ or blobs/, on url_manifest.json, or on an existing directory exits 2."""
    cache = tmp_path / "op_cache"
    for label, flag, target in (
        ("index_in_pages", "--index-out", cache / "pages" / "x.jsonl"),
        ("profiles_in_blobs", "--profiles-out", cache / "blobs" / "p.jsonl"),
        ("index_is_url_manifest", "--index-out", cache / "url_manifest.json"),
        ("profiles_is_dir", "--profiles-out", tmp_path),
    ):
        api = _SpyAPIClient({_RT_A: _make_study(_RT_A, [])})
        rc = cli_mod.run_cli(
            [
                "--manifest", str(_rt_manifest(tmp_path)),
                "--nct-id", _RT_A,
                "--cache-dir", str(cache),
                "--rate-limit-sec", "0",
                flag, str(target),
            ],
            api_client=api,
            session=_FakeSession({}),
        )
        capsys.readouterr()
        assert rc == 2 and api.requested_ncts == [], label
        assert not cache.exists(), label


def test_m1_r2_payload_and_alias_edges() -> None:
    """A file listed as an ICF and again as a protocol is refused; an alias on the allowlist passes an empty holdout set."""
    study = _make_study(
        _RT_A,
        [
            {"filename": "Prot_000.pdf", "hasProtocol": False, "hasSap": False, "hasIcf": True},
            {"filename": "Prot_000.pdf ", "hasProtocol": True, "hasSap": False},
        ],
    )
    with pytest.raises(di.DocumentIndexerError, match="same posted file"):
        di.extract_in_scope_large_docs(study, allowed_nct_ids={_RT_A})

    merged = _make_study(_RT_A, [])
    merged["protocolSection"]["identificationModule"]["nctIdAliases"] = [_RT_B]
    assert di.validate_study_payload_nct_id(merged, allowed_nct_ids={_RT_A, _RT_B}) == _RT_A
    # No firewall configured at all (library use): nothing to check the alias against.
    assert di.validate_study_payload_nct_id(merged) == _RT_A
    with pytest.raises(di.AuditHoldoutError) as info:
        di.validate_study_payload_nct_id(merged, allowed_nct_ids={_RT_A})
    assert re.search(r"NCT[0-9]{8}", str(info.value)) is None


def test_m1_r2_rate_limiter_refuses_unusable_intervals(tmp_path: Path) -> None:
    """``GlobalRateLimiter`` refuses a negative, NaN or infinite interval instead of clamping it to 0."""
    for value in (-1, float("nan"), float("inf"), True):
        with pytest.raises(di.DocumentIndexerError):
            cli_mod.GlobalRateLimiter(tmp_path / "lock", min_interval_sec=value)
    assert cli_mod.GlobalRateLimiter(tmp_path / "lock", min_interval_sec=0).min_interval_sec == 0.0
