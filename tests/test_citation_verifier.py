"""Offline unit tests for src/citation_verifier.py."""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import pytest

import src.citation_verifier as cv_mod
from src.citation_verifier import (
    AuditHoldoutViolationError,
    CITATION_FORBIDDEN_STATUSES,
    CITATION_REQUIRED_KEYS,
    CITATION_REQUIRED_STATUSES,
    CitationVerificationError,
    DOCUMENT_REQUIRED_KEYS,
    DictPageTextProvider,
    DirectoryPageTextProvider,
    ELEMENTS,
    InvalidNctIdError,
    LOCATION_REQUIRED_KEYS,
    NEUTRAL_LANGUAGE_VOCAB_SHA256,
    PageTextProvider,
    SPARSE_PASSES,
    STRICT_NCT_ID_RE,
    check_neutral_language,
    check_note_page_counts,
    enforce_audit_holdout,
    has_ellipsis,
    match_quotation_on_page,
    normalize_for_page_match,
    scan_neutral_language,
    scan_neutral_language_record,
    validate_citation_shape,
    validate_status_citations,
    validate_strict_nct_id,
    verify_census_corpus,
    verify_citation,
    verify_document_record,
    verify_location_record,
    verify_trial_completeness,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
HOLDOUT_SET: frozenset[str] = frozenset({"NCT99999999"})
SCOPE_SET: frozenset[str] = frozenset({"NCT99000001"})


def _make_doc(
    *,
    nct_id: str = "NCT99000001",
    filename: str = "Prot_000.pdf",
    sha256: str = SHA_A,
    physical_pages: int = 5,
    pages_without_text: list[int] | None = None,
    http_status: int = 200,
    byte_count: int = 1024,
    doc_type: str = "protocol",
    model_id: Any = "deterministic_indexer_v1",
    profile_version: Any = "1.1.0",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "filename": filename,
        "url": f"https://cdn.clinicaltrials.gov/large-docs/01/{nct_id}/{filename}",
        "http_status": http_status,
        "sha256": sha256,
        "bytes": byte_count,
        "doc_type": doc_type,
        "version_label": "Version 2.0",
        "version_date_as_printed": "15 May 2020",
        "physical_pages": physical_pages,
        "pages_without_text": list(pages_without_text) if pages_without_text is not None else [],
        "retrieved_at": "2026-11-14T12:00:00Z",
        "model_id": model_id,
        "profile_version": profile_version,
    }
    if extra:
        rec.update(extra)
    return rec


def _make_citation(
    *,
    filename: Any = "Prot_000.pdf",
    sha256: Any = SHA_A,
    physical_page: Any = 2,
    printed_page: Any = "2",
    section_heading: Any = "9.1 Analysis Populations",
    quotation: Any = "The primary efficacy analysis will use the ITT population.",
) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": sha256,
        "physical_page": physical_page,
        "printed_page": printed_page,
        "section_heading": section_heading,
        "quotation": quotation,
    }


def _make_location(
    *,
    nct_id: str = "NCT99000001",
    primary_outcome_index: Any = 0,
    primary_outcome_title: str = "Overall Survival",
    element: str = "analysis_population",
    status: str = "FOUND",
    citations: list[dict[str, Any]] | None = None,
    pages_searched: Any = "all",
    pages_not_searched: Any = None,
    note: str = "ITT defined in SAP section 9.1.",
    worker: str = "shard-01",
    pass_name: str = "primary",
    model_id: Any = "claude-sonnet-4-6-20260601",
    prompt_version: Any = "decision-rule-v1.1",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "primary_outcome_index": primary_outcome_index,
        "primary_outcome_title": primary_outcome_title,
        "element": element,
        "status": status,
        "citations": [_make_citation()] if citations is None else citations,
        "pages_searched": pages_searched,
        "pages_not_searched": [] if pages_not_searched is None else pages_not_searched,
        "note": note,
        "worker": worker,
        "pass": pass_name,
        "model_id": model_id,
        "prompt_version": prompt_version,
    }
    if extra:
        rec.update(extra)
    return rec


def test_f2_strict_ascii_nct_id_hard_stop_before_audit_checks() -> None:
    assert STRICT_NCT_ID_RE.pattern == r"^NCT[0-9]{8}$"
    assert bool(STRICT_NCT_ID_RE.flags & re.ASCII)
    assert validate_strict_nct_id("NCT99000001") == "NCT99000001"

    bad_ids: tuple[Any, ...] = (
        "nct99000001",
        "NCT99000001 ",
        " NCT99000001",
        "NCT99000001\n",
        "NCT99000001\u200b",
        "NCT99000001\ufeff",
        "NCT\u0669\u0669\u0660\u0660\u0660\u0660\u0660\u0661",
        "NCT9900001",
        "NCT990000001",
        "",
        None,
        99000001,
        True,
    )
    for bad in bad_ids:
        with pytest.raises(InvalidNctIdError):
            validate_strict_nct_id(bad)
        # Must raise InvalidNctIdError FIRST even when audit_nct_ids is empty or missing in_audit.
        with pytest.raises(InvalidNctIdError):
            enforce_audit_holdout(bad)  # type: ignore[arg-type]
        with pytest.raises(InvalidNctIdError):
            verify_document_record(_make_doc(nct_id=bad))  # type: ignore[arg-type]
        with pytest.raises(InvalidNctIdError):
            verify_location_record(_make_location(nct_id=bad), docs_by_nct={})  # type: ignore[arg-type]
        with pytest.raises(InvalidNctIdError):
            verify_census_corpus([_make_doc(nct_id=bad)], [])  # type: ignore[arg-type]

    # Malformed entry inside audit_nct_ids or in_scope_nct_ids also raises InvalidNctIdError first.
    with pytest.raises(InvalidNctIdError):
        enforce_audit_holdout("NCT99000001", in_audit=False, audit_nct_ids={"nct99999999"})
    with pytest.raises(InvalidNctIdError):
        enforce_audit_holdout(
            "NCT99000001",
            in_scope_nct_ids={"NCT99000001 "},
            audit_nct_ids=HOLDOUT_SET,
        )


def test_f1_audit_holdout_no_defaults_and_non_empty_audit_set() -> None:
    # Missing in_audit without in_scope_nct_ids is refused with AuditHoldoutViolationError.
    with pytest.raises(AuditHoldoutViolationError):
        enforce_audit_holdout("NCT99000001", audit_nct_ids=HOLDOUT_SET)

    # Empty audit_nct_ids is refused by default even when in_audit=False.
    with pytest.raises(AuditHoldoutViolationError):
        enforce_audit_holdout("NCT99000001", in_audit=False, audit_nct_ids=frozenset())
    assert (
        enforce_audit_holdout(
            "NCT99000001",
            in_audit=False,
            audit_nct_ids=frozenset(),
            allow_empty_audit_set=True,
        )
        is None
    )

    # Explicit True or non-bool in_audit always raises AuditHoldoutViolationError.
    for bad_flag in (True, None, 0, 1, "false", "False", []):
        with pytest.raises(AuditHoldoutViolationError):
            enforce_audit_holdout(
                "NCT99000001",
                in_audit=bad_flag,
                audit_nct_ids=HOLDOUT_SET,
            )

    with pytest.raises(AuditHoldoutViolationError):
        enforce_audit_holdout(
            "NCT99999999",
            in_audit=False,
            audit_nct_ids=HOLDOUT_SET,
        )

    doc = _make_doc(nct_id="NCT99000001")
    loc = _make_location(nct_id="NCT99000001")

    # Direct record calls without explicit in_audit or in_scope_nct_ids are refused.
    with pytest.raises(AuditHoldoutViolationError):
        verify_document_record(doc, audit_nct_ids=HOLDOUT_SET)
    with pytest.raises(AuditHoldoutViolationError):
        verify_location_record(loc, docs_by_nct={}, audit_nct_ids=HOLDOUT_SET)
    with pytest.raises(AuditHoldoutViolationError):
        verify_census_corpus([doc], [], audit_nct_ids=HOLDOUT_SET)

    # Empty audit_nct_ids refused on verify_document_record, verify_location_record, verify_census_corpus.
    with pytest.raises(AuditHoldoutViolationError):
        verify_document_record(doc, in_audit=False, audit_nct_ids=frozenset())
    with pytest.raises(AuditHoldoutViolationError):
        verify_location_record(loc, docs_by_nct={}, in_audit=False, audit_nct_ids=frozenset())
    with pytest.raises(AuditHoldoutViolationError):
        verify_census_corpus(
            [doc],
            [],
            in_scope_nct_ids=SCOPE_SET,
            audit_nct_ids=frozenset(),
        )

    # Side-stream row carrying non-bool or True in_audit is refused even when in_scope_nct_ids allows NCT.
    for bad_row_flag in (True, None, 0, "false"):
        with pytest.raises(AuditHoldoutViolationError):
            verify_document_record(
                _make_doc(extra={"in_audit": bad_row_flag}),
                in_scope_nct_ids=SCOPE_SET,
                audit_nct_ids=HOLDOUT_SET,
                strict_keys=False,
            )
        with pytest.raises(AuditHoldoutViolationError):
            verify_location_record(
                _make_location(extra={"in_audit": bad_row_flag}),
                docs_by_nct={},
                in_scope_nct_ids=SCOPE_SET,
                audit_nct_ids=HOLDOUT_SET,
            )

    # Unlisted NCT ID absent from in_scope_nct_ids is refused.
    with pytest.raises(AuditHoldoutViolationError):
        verify_document_record(
            _make_doc(nct_id="NCT99000002"),
            in_scope_nct_ids=SCOPE_SET,
            audit_nct_ids=HOLDOUT_SET,
        )


def test_f5_exported_status_and_citation_shape_validators() -> None:
    assert CITATION_REQUIRED_STATUSES == ("FOUND", "AMBIGUOUS", "NOT_APPLICABLE")
    assert CITATION_FORBIDDEN_STATUSES == ("NOT_FOUND", "NO_DOCUMENT")

    # Required statuses demand >= 1 citation; forbidden statuses demand 0.
    for req_status in CITATION_REQUIRED_STATUSES:
        assert validate_status_citations(req_status, 1, has_posted_documents=True) == []
        assert len(validate_status_citations(req_status, 0, has_posted_documents=True)) == 1
    for forb_status in CITATION_FORBIDDEN_STATUSES:
        assert validate_status_citations(forb_status, 0, has_posted_documents=False) == []
        assert len(validate_status_citations(forb_status, 1, has_posted_documents=False)) >= 1

    # NO_DOCUMENT forbidden when has_posted_documents is True; citations forbidden when False.
    assert len(validate_status_citations("NO_DOCUMENT", 0, has_posted_documents=True)) == 1
    assert len(validate_status_citations("FOUND", 1, has_posted_documents=False)) == 1
    assert len(validate_status_citations("UNREADABLE", 1, has_posted_documents=False)) == 1

    # Citation shape checks: non-mappings, empty dict, extra keys, bad fields.
    valid_cit = _make_citation()
    assert validate_citation_shape(valid_cit) == []

    for non_map in (None, "x", 123, [], True):
        errs = validate_citation_shape(non_map)
        assert len(errs) == 1
        assert "JSON object" in errs[0]

    assert len(validate_citation_shape({})) == 1
    assert len(validate_citation_shape({**valid_cit, "extra_field": 1})) == 1
    assert len(validate_citation_shape(_make_citation(filename="../Prot_000.pdf"))) == 1
    assert len(validate_citation_shape(_make_citation(filename="a/Prot_000.pdf"))) == 1
    assert len(validate_citation_shape(_make_citation(filename=""))) == 1
    assert len(validate_citation_shape(_make_citation(sha256="not-a-sha"))) == 1
    assert len(validate_citation_shape(_make_citation(physical_page=True))) == 1
    assert len(validate_citation_shape(_make_citation(physical_page=0))) == 1
    assert len(validate_citation_shape(_make_citation(quotation="alpha ... 0.05"))) == 1
    assert len(validate_citation_shape(_make_citation(quotation="x" * 301))) == 1

    # printed_page and section_heading None handling gated by allow_none_optional_strings.
    cit_none_opt = _make_citation(printed_page=None, section_heading=None)
    assert len(validate_citation_shape(cit_none_opt, allow_none_optional_strings=False)) == 2
    assert validate_citation_shape(cit_none_opt, allow_none_optional_strings=True) == []
    assert (
        len(
            validate_citation_shape(
                _make_citation(printed_page=True, section_heading=123),
                allow_none_optional_strings=True,
            )
        )
        == 2
    )


def test_f6_required_model_and_version_stamps_on_documents_and_locations() -> None:
    assert "model_id" in DOCUMENT_REQUIRED_KEYS
    assert "profile_version" in DOCUMENT_REQUIRED_KEYS
    assert "model_id" in LOCATION_REQUIRED_KEYS
    assert "prompt_version" in LOCATION_REQUIRED_KEYS

    doc = _make_doc()
    docs_by_nct = {"NCT99000001": {"Prot_000.pdf": doc}}

    for key in ("model_id", "profile_version"):
        missing_doc = dict(doc)
        del missing_doc[key]
        missing_codes = {
            i.code
            for i in verify_document_record(
                missing_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET
            )
        }
        assert "DOCUMENT_MISSING_KEYS" in missing_codes

        for bad_val in ("", "   ", None, 123, True):
            bad_doc = dict(doc)
            bad_doc[key] = bad_val
            bad_issues = verify_document_record(
                bad_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET
            )
            assert len(bad_issues) >= 1

    loc = _make_location()
    for key in ("model_id", "prompt_version"):
        missing_loc = dict(loc)
        del missing_loc[key]
        _, missing_loc_issues = verify_location_record(
            missing_loc,
            docs_by_nct=docs_by_nct,
            in_audit=False,
            audit_nct_ids=HOLDOUT_SET,
        )
        assert "LOCATION_MISSING_KEYS" in {i.code for i in missing_loc_issues}

        for bad_val in ("", "   ", None, 123, True):
            bad_loc = dict(loc)
            bad_loc[key] = bad_val
            _, bad_loc_issues = verify_location_record(
                bad_loc,
                docs_by_nct=docs_by_nct,
                in_audit=False,
                audit_nct_ids=HOLDOUT_SET,
            )
            assert len(bad_loc_issues) >= 1


def test_document_schema_validation_and_disk_hash(tmp_path: Path) -> None:
    valid_doc = _make_doc()
    assert (
        verify_document_record(valid_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET) == []
    )

    missing_doc = dict(valid_doc)
    del missing_doc["sha256"]
    codes = {
        i.code
        for i in verify_document_record(missing_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET)
    }
    assert "DOCUMENT_MISSING_KEYS" in codes

    extra_doc = _make_doc(extra={"readability_status": "machine_readable"})
    assert {
        i.code
        for i in verify_document_record(
            extra_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET, strict_keys=True
        )
    } == {"DOCUMENT_EXTRA_KEYS"}
    assert (
        verify_document_record(
            extra_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET, strict_keys=False
        )
        == []
    )

    bad_doc = _make_doc(
        filename="../Prot_000.pdf",
        sha256="not-a-sha",
        physical_pages=True,  # type: ignore[arg-type]
        byte_count=True,  # type: ignore[arg-type]
        pages_without_text=[3, 2],
    )
    bad_codes = {
        i.code
        for i in verify_document_record(bad_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET)
    }
    assert {
        "INVALID_DOCUMENT_FILENAME",
        "INVALID_SHA256",
        "INVALID_BYTE_COUNT",
        "INVALID_PHYSICAL_PAGES",
        "INVALID_PAGES_WITHOUT_TEXT",
    }.issubset(bad_codes)

    pdf_bytes = b"%PDF-1.4\nminimal fixture\n%%EOF\n"
    real_sha = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_path = tmp_path / "NCT99000001" / "Prot_000.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(pdf_bytes)

    good_disk_doc = _make_doc(sha256=real_sha, byte_count=len(pdf_bytes))
    assert (
        verify_document_record(
            good_disk_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET, pdf_root=tmp_path
        )
        == []
    )

    tampered_doc = _make_doc(sha256=SHA_B, byte_count=len(pdf_bytes) + 1)
    tamper_codes = {
        i.code
        for i in verify_document_record(
            tampered_doc, in_audit=False, audit_nct_ids=HOLDOUT_SET, pdf_root=tmp_path
        )
    }
    assert tamper_codes == {"DOCUMENT_BYTES_MISMATCH", "DOCUMENT_SHA256_MISMATCH"}


def test_quotation_hygiene_and_match_tiers() -> None:
    page_text = (
        "9.1 Analysis Populations\n"
        "The primary ef\ufb01cacy analysis will use the \u201cintent\u2013to\u2013treat\u201d "
        "popula-\ntion with soft\u00ad\nhyphen wrapping and two\u2010sided alpha = 0.05."
    )
    exact_quote = "9.1 Analysis Populations"
    assert match_quotation_on_page(exact_quote, page_text) == "exact_ws"

    nfkc_quote = (
        'The primary efficacy analysis will use the "intent-to-treat" '
        "population with softhyphen wrapping and two-sided alpha = 0.05."
    )
    assert match_quotation_on_page(nfkc_quote, page_text) == "unicode_nfkc_hyphen"
    assert "efficacy" in normalize_for_page_match("ef\ufb01cacy")
    assert (
        normalize_for_page_match("random-\nization", repair_line_break_hyphens=True)
        == "randomization"
    )
    assert (
        normalize_for_page_match("sub\u00ad\njects", repair_line_break_hyphens=True)
        == "subjects"
    )

    assert has_ellipsis("alpha = 0.05 ... two-sided")
    assert has_ellipsis("alpha = 0.05 . . . two-sided")
    assert has_ellipsis("alpha = 0.05 \u2026 two-sided")
    assert not has_ellipsis("alpha = 0.05 (two-sided).")

    doc = _make_doc(physical_pages=3, pages_without_text=[3])
    docs_map = {"Prot_000.pdf": doc}

    for bad_q, expected_code in (
        ("", "CITATION_EMPTY_QUOTATION"),
        ("x" * 301, "CITATION_QUOTATION_TOO_LONG"),
        ("double  space", "CITATION_QUOTATION_UNNORMALIZED_WHITESPACE"),
        ("line\nbreak", "CITATION_QUOTATION_UNNORMALIZED_WHITESPACE"),
        ("alpha ... 0.05", "CITATION_ELLIPSIS_FORBIDDEN"),
        ("alpha \u2026 0.05", "CITATION_ELLIPSIS_FORBIDDEN"),
    ):
        _, issues = verify_citation(
            "NCT99000001",
            (0, "alpha_and_sidedness", "primary"),
            _make_citation(quotation=bad_q),
            docs_by_filename=docs_map,
        )
        assert expected_code in {i.code for i in issues}

    _, bool_page_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=True),
        docs_by_filename=docs_map,
    )
    assert "INVALID_CITATION_PAGE" in {i.code for i in bool_page_issues}

    _, range_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=4),
        docs_by_filename=docs_map,
    )
    assert "CITATION_PAGE_OUT_OF_RANGE" in {i.code for i in range_issues}

    provider = DictPageTextProvider(
        pages_by_sha256={SHA_A: ["p1", "p2", ""]},
        ocr_pages_by_sha256={SHA_A: ["p1", "p2", "OCR recovered alpha = 0.05 two-sided."]},
    )
    assert isinstance(provider, PageTextProvider)
    _, pwt_pdfkit_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=3, quotation="OCR recovered alpha = 0.05 two-sided."),
        docs_by_filename=docs_map,
        page_text_provider=provider,
        text_source="pdfkit",
    )
    assert "CITATION_PAGE_WITHOUT_TEXT" in {i.code for i in pwt_pdfkit_issues}

    tier_ocr, pwt_ocr_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "ocr_recovery"),
        _make_citation(physical_page=3, quotation="OCR recovered alpha = 0.05 two-sided."),
        docs_by_filename=docs_map,
        page_text_provider=provider,
        text_source="macos_vision_ocr_v1",
    )
    assert tier_ocr == "exact_ws"
    assert pwt_ocr_issues == []


def test_strict_single_page_cross_page_span_violation() -> None:
    doc = _make_doc(physical_pages=3)
    provider = DictPageTextProvider(
        pages_by_sha256={
            SHA_A: [
                "Cover page",
                "The primary endpoint will be analyzed using a stratified",
                "log-rank test at a two-sided alpha level of 0.05.",
            ]
        }
    )
    spanning_quote = (
        "The primary endpoint will be analyzed using a stratified "
        "log-rank test at a two-sided alpha level of 0.05."
    )
    tier, issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=2, quotation=spanning_quote),
        docs_by_filename={"Prot_000.pdf": doc},
        page_text_provider=provider,
    )
    assert tier == "mismatch"
    assert [i.code for i in issues] == ["cross_page_span_violation"]

    _, range_str_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page="2-3", quotation="Cover page"),
        docs_by_filename={"Prot_000.pdf": doc},
    )
    assert "cross_page_span_violation" in {i.code for i in range_str_issues}

    tier_absent, absent_issues = verify_citation(
        "NCT99000001",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=2, quotation="Completely unrelated fabricated text."),
        docs_by_filename={"Prot_000.pdf": doc},
        page_text_provider=provider,
    )
    assert tier_absent == "mismatch"
    assert [i.code for i in absent_issues] == ["CITATION_PAGE_SUBSTRING_MISMATCH"]


def test_pages_not_searched_and_unreadable_invariants() -> None:
    doc = _make_doc(physical_pages=5, pages_without_text=[5])
    docs_by_nct = {"NCT99000001": {"Prot_000.pdf": doc}}

    loc_contra = _make_location(
        status="NOT_FOUND",
        citations=[],
        pages_searched="all",
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_contra = verify_location_record(
        loc_contra,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert "SEARCH_COVERAGE_CONTRADICTION" in {i.code for i in issues_contra}

    loc_scoped = _make_location(
        status="NOT_FOUND",
        citations=[],
        pages_searched={"Prot_000.pdf": [1, 2]},
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_exh = verify_location_record(
        loc_scoped,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
        require_exhaustive_for_not_found=True,
    )
    assert "NOT_FOUND_WITH_UNSEARCHED_PAGES" in {i.code for i in issues_exh}

    _, issues_scoped = verify_location_record(
        loc_scoped,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
        require_exhaustive_for_not_found=False,
    )
    assert issues_scoped == []

    loc_cit_unsearched = _make_location(
        status="FOUND",
        citations=[_make_citation(physical_page=3)],
        pages_searched={"Prot_000.pdf": [1, 2]},
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_cit_un = verify_location_record(
        loc_cit_unsearched,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert "CITATION_IN_UNSEARCHED_PAGES" in {i.code for i in issues_cit_un}

    loc_unreadable_ok = _make_location(
        status="UNREADABLE",
        citations=[],
        note="SAP pages are scanned image-only without extractable text.",
    )
    _, issues_u_ok = verify_location_record(
        loc_unreadable_ok,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert issues_u_ok == []

    _, issues_u_nodoc = verify_location_record(
        loc_unreadable_ok,
        docs_by_nct={},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert "UNREADABLE_WITHOUT_DOCUMENT" in {i.code for i in issues_u_nodoc}

    clean_doc = _make_doc(
        physical_pages=5,
        pages_without_text=[],
        extra={"readability_status": "machine_readable", "redaction_markers": []},
    )
    _, issues_u_clean = verify_location_record(
        loc_unreadable_ok,
        docs_by_nct={"NCT99000001": {"Prot_000.pdf": clean_doc}},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert "UNREADABLE_ON_MACHINE_READABLE_DOCUMENT" in {i.code for i in issues_u_clean}


def test_neutral_language_scanner_and_exemptions() -> None:
    assert len(NEUTRAL_LANGUAGE_VOCAB_SHA256) == 64
    assert check_neutral_language is scan_neutral_language

    flagged_cases = [
        ("The trial met its primary endpoint at week 12.", "NL01"),
        ("The study failed to meet its primary objective.", "NL01"),
        ("Treatment achieved statistical significance over placebo.", "NL02"),
        ("Results trended toward statistical significance (p=0.06).", "NL03"),
        ("Borderline significant benefit observed in subgroup.", "NL03"),
        ("Evidence of outcome-switching and FDAAA non-compliance.", "NL04"),
        ("Sponsor violated FDAAA reporting rules and is overdue under FDAAA.", "NL04"),
    ]
    for text, expected_rule in flagged_cases:
        flags = scan_neutral_language(text)
        assert expected_rule in {f.rule_id for f in flags}, f"Expected {expected_rule} for {text!r}"

    clean_cases = [
        "Secondary endpoints tested hierarchically only if Week 16 is statistically significant.",
        "Crossover allowed for patients switching arm after disease progression.",
        "Sensitivity analysis excludes non-compliant investigational site records.",
        "Two sites failed CFDA inspection and were excluded from per-protocol set.",
        "Protocol lists GCP violations as major protocol deviations.",
        "The trial failed to enroll target sample size and stopped early.",
    ]
    for text in clean_cases:
        assert scan_neutral_language(text) == [], f"False positive on {text!r}"

    record = {
        "statement_verbatim": "The trial met its primary endpoint and achieved statistical significance.",
        "citations": [
            {
                "quotation": "Sponsor violated FDAAA in this quoted background sentence.",
            }
        ],
        "note": "Primary endpoint defined in SAP section 5.1.",
    }
    assert scan_neutral_language_record(record) == []

    bad_record = dict(record)
    bad_record["note"] = "This study was positive and achieved statistical significance."
    findings = scan_neutral_language_record(bad_record)
    assert len(findings) >= 2
    assert all(path == "note" for path, _ in findings)


def test_corpus_verification_completeness_and_directory_provider(tmp_path: Path) -> None:
    page_dir = tmp_path / "pages"
    target = page_dir / SHA_A[:2] / f"{SHA_A}.json.gz"
    target.parent.mkdir(parents=True)
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        json.dump(
            {
                "nct_id": "NCT99000001",
                "in_audit": False,
                "sha256": SHA_A,
                "physical_pages": 2,
                "pages": [
                    "Protocol Cover Page",
                    "Primary endpoint is OS analyzed in the ITT population with two-sided alpha = 0.05.",
                ],
            },
            fh,
        )

    provider = DirectoryPageTextProvider(
        page_dir,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
    )
    doc = _make_doc(physical_pages=2)
    locs = [
        _make_location(
            element=elem,
            citations=[
                _make_citation(
                    physical_page=2,
                    quotation="Primary endpoint is OS analyzed in the ITT population with two-sided alpha = 0.05.",
                )
            ],
            note="Verified from Prot_000.pdf (2 pages).",
        )
        for elem in ELEMENTS
    ]

    report = verify_census_corpus(
        [doc],
        locs,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        page_text_provider=provider,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert report.ok is True
    assert report.documents_checked == 1
    assert report.locations_checked == 7
    assert report.citations_checked == 7
    assert report.exact_ws_matches == 7
    assert report.substring_mismatches == 0
    assert report.to_dict()["error_count"] == 0

    warn_issues = check_note_page_counts(
        "NCT99000001",
        (0, "analysis_population", "primary"),
        "See Prot_000.pdf (99 pages) for SAP.",
        {"Prot_000.pdf": doc},
    )
    assert len(warn_issues) == 1
    assert warn_issues[0].code == "NOTE_PAGE_COUNT_MISMATCH"
    assert warn_issues[0].severity == "warning"

    missing_issues = verify_trial_completeness(
        "NCT99000001",
        locs[:-1],
        expected_primary_outcomes=1,
    )
    assert [i.code for i in missing_issues] == ["TRIAL_GRID_MISSING_CELL"]

    # DirectoryPageTextProvider and JSONL page maps refuse audit or malformed rows before loading.
    contaminated_dir = tmp_path / "contaminated_pages"
    contaminated_dir.mkdir(parents=True)
    jsonl_map = contaminated_dir / "page_maps.jsonl"
    jsonl_map.write_text(
        json.dumps(
            {
                "nct_id": "NCT99999999",
                "filename": "Prot_000.pdf",
                "sha256": SHA_B,
                "pages": ["Holdout page"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AuditHoldoutViolationError):
        DirectoryPageTextProvider(
            contaminated_dir,
            audit_nct_ids=HOLDOUT_SET,
            in_scope_nct_ids=SCOPE_SET,
        )


def test_no_document_consistency_with_posted_documents() -> None:
    doc = _make_doc(physical_pages=5)
    docs_by_nct = {"NCT99000001": {"Prot_000.pdf": doc}}

    loc_no_doc = _make_location(
        status="NO_DOCUMENT",
        citations=[],
        pages_searched="all",
        pages_not_searched=[],
        note="No protocol or SAP posted.",
    )

    _, issues_with_doc = verify_location_record(
        loc_no_doc,
        docs_by_nct=docs_by_nct,
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert len(issues_with_doc) == 1
    issue = issues_with_doc[0]
    assert issue.code == "NO_DOCUMENT_WITH_POSTED_DOCUMENT"
    assert issue.severity == "error"
    assert issue.nct_id == "NCT99000001"
    assert issue.location_key == (0, "analysis_population", "primary")
    assert issue.filename is None
    assert issue.physical_page is None

    no_doc_grid = [
        _make_location(
            element=elem,
            status="NO_DOCUMENT",
            citations=[],
            pages_searched="all",
            pages_not_searched=[],
            note="No protocol or SAP posted.",
        )
        for elem in ELEMENTS
    ]
    report_fail = verify_census_corpus(
        [doc],
        no_doc_grid,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert report_fail.ok is False
    assert report_fail.to_dict()["error_count"] == len(ELEMENTS)
    assert {i.code for i in report_fail.issues} == {"NO_DOCUMENT_WITH_POSTED_DOCUMENT"}

    _, issues_absent = verify_location_record(
        loc_no_doc,
        docs_by_nct={},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert issues_absent == []
    _, issues_empty = verify_location_record(
        loc_no_doc,
        docs_by_nct={"NCT99000001": {}},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
    )
    assert issues_empty == []

    report_ok = verify_census_corpus(
        [],
        no_doc_grid,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert report_ok.ok is True
    assert report_ok.issues == ()

    _, issues_manifest_flag = verify_location_record(
        loc_no_doc,
        docs_by_nct={},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
        manifest_has_documents=True,
    )
    assert len(issues_manifest_flag) == 1
    assert issues_manifest_flag[0].code == "NO_DOCUMENT_WITH_POSTED_DOCUMENT"

    _, issues_manifest_false = verify_location_record(
        loc_no_doc,
        docs_by_nct={},
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
        manifest_has_documents=False,
    )
    assert issues_manifest_false == []

    report_manifest_fail = verify_census_corpus(
        [],
        no_doc_grid,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        expected_outcomes_by_nct={"NCT99000001": 1},
        expected_has_documents_by_nct={"NCT99000001": True},
    )
    assert report_manifest_fail.ok is False
    assert report_manifest_fail.to_dict()["error_count"] == len(ELEMENTS)
    assert {i.code for i in report_manifest_fail.issues} == {"NO_DOCUMENT_WITH_POSTED_DOCUMENT"}

    report_manifest_ok = verify_census_corpus(
        [],
        no_doc_grid,
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        expected_outcomes_by_nct={"NCT99000001": 1},
        expected_has_documents_by_nct={"NCT99000001": False},
    )
    assert report_manifest_ok.ok is True
    assert report_manifest_ok.issues == ()

    with pytest.raises(AuditHoldoutViolationError):
        verify_census_corpus(
            [],
            [],
            audit_nct_ids=HOLDOUT_SET,
            in_scope_nct_ids=SCOPE_SET,
            expected_has_documents_by_nct={"NCT99999999": True},
        )


def test_sparse_pass_completeness_and_bounds() -> None:
    assert SPARSE_PASSES == ("ocr_recovery", "adjudication")

    doc = _make_doc(physical_pages=3, pages_without_text=[3])
    provider = DictPageTextProvider(
        pages_by_sha256={
            SHA_A: [
                "Protocol Cover Page",
                "Primary analysis uses ITT population and hierarchical testing at two-sided alpha = 0.05.",
                "",
            ]
        },
        ocr_pages_by_sha256={
            SHA_A: [
                "Protocol Cover Page",
                "Primary analysis uses ITT population and hierarchical testing at two-sided alpha = 0.05.",
                "Non-inferiority margin is 10 percentage points.",
            ]
        },
    )

    primary_quote = (
        "Primary analysis uses ITT population and hierarchical testing at two-sided alpha = 0.05."
    )
    ocr_quote = "Non-inferiority margin is 10 percentage points."

    primary_rows: list[dict[str, Any]] = []
    for elem in ELEMENTS:
        if elem == "margin":
            primary_rows.append(
                _make_location(
                    element=elem,
                    status="UNREADABLE",
                    citations=[],
                    note="Page 3 is a scanned image without extractable text.",
                )
            )
        elif elem == "multiplicity":
            primary_rows.append(
                _make_location(
                    element=elem,
                    status="AMBIGUOUS",
                    citations=[_make_citation(physical_page=2, quotation=primary_quote)],
                    note="Hierarchical testing phrasing flagged for adjudication.",
                )
            )
        else:
            primary_rows.append(
                _make_location(
                    element=elem,
                    status="FOUND",
                    citations=[_make_citation(physical_page=2, quotation=primary_quote)],
                    note="Verified from Prot_000.pdf (3 pages).",
                )
            )

    ocr_row = _make_location(
        element="margin",
        status="FOUND",
        citations=[_make_citation(physical_page=3, printed_page="3", quotation=ocr_quote)],
        note="Recovered from Vision OCR on page 3.",
        worker="ocr-shard-01",
        pass_name="ocr_recovery",
        extra={"text_source": "macos_vision_ocr_v1"},
    )
    adj_row = _make_location(
        element="multiplicity",
        status="FOUND",
        citations=[_make_citation(physical_page=2, quotation=primary_quote)],
        note="Adjudicated hierarchical multiplicity rule.",
        worker="adjudicator-01",
        pass_name="adjudication",
    )

    merged_report = verify_census_corpus(
        [doc],
        primary_rows + [ocr_row, adj_row],
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        page_text_provider=provider,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert merged_report.ok is True
    assert merged_report.issues == ()
    assert merged_report.locations_checked == 9
    assert merged_report.citations_checked == 8

    assert (
        verify_trial_completeness(
            "NCT99000001",
            [adj_row],
            expected_primary_outcomes=1,
            pass_name="adjudication",
            allow_sparse=True,
        )
        == []
    )
    strict_missing = verify_trial_completeness(
        "NCT99000001",
        [adj_row],
        expected_primary_outcomes=1,
        pass_name="adjudication",
    )
    assert len(strict_missing) == len(ELEMENTS) - 1
    assert {i.code for i in strict_missing} == {"TRIAL_GRID_MISSING_CELL"}

    strict_missing_explicit = verify_trial_completeness(
        "NCT99000001",
        [adj_row],
        expected_primary_outcomes=1,
        pass_name="adjudication",
        allow_sparse=False,
    )
    assert len(strict_missing_explicit) == len(ELEMENTS) - 1

    bad_k_issues = verify_trial_completeness(
        "NCT99000001",
        [adj_row],
        expected_primary_outcomes=0,
        pass_name="adjudication",
        allow_sparse=True,
    )
    assert [i.code for i in bad_k_issues] == ["INVALID_EXPECTED_OUTCOMES_COUNT"]

    dup_issues = verify_trial_completeness(
        "NCT99000001",
        [adj_row, dict(adj_row)],
        expected_primary_outcomes=1,
        pass_name="adjudication",
        allow_sparse=True,
    )
    assert [i.code for i in dup_issues] == ["TRIAL_GRID_DUPLICATE_CELL"]

    for sparse_row in (adj_row, ocr_row):
        orphan_report = verify_census_corpus(
            [doc],
            [sparse_row],
            audit_nct_ids=HOLDOUT_SET,
            in_scope_nct_ids=SCOPE_SET,
            page_text_provider=provider,
            expected_outcomes_by_nct={"NCT99000001": 1},
        )
        assert orphan_report.ok is False
        assert len(orphan_report.issues) == len(ELEMENTS)
        assert {i.code for i in orphan_report.issues} == {"TRIAL_GRID_MISSING_CELL"}
        assert all(
            i.location_key is not None and i.location_key[2] == "primary"
            for i in orphan_report.issues
        )

    oob_adj = _make_location(
        primary_outcome_index=1,
        element="multiplicity",
        status="FOUND",
        citations=[_make_citation(physical_page=2, quotation=primary_quote)],
        note="Out-of-bounds adjudication row.",
        worker="adjudicator-01",
        pass_name="adjudication",
    )
    oob_adj_report = verify_census_corpus(
        [doc],
        primary_rows + [oob_adj],
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        page_text_provider=provider,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert oob_adj_report.ok is False
    assert [i.code for i in oob_adj_report.issues] == ["TRIAL_GRID_UNEXPECTED_CELL"]
    assert oob_adj_report.issues[0].location_key == (1, "multiplicity", "adjudication")

    oob_ocr = _make_location(
        primary_outcome_index=1,
        element="margin",
        status="FOUND",
        citations=[_make_citation(physical_page=3, printed_page="3", quotation=ocr_quote)],
        note="Out-of-bounds OCR recovery row.",
        worker="ocr-shard-01",
        pass_name="ocr_recovery",
        extra={"text_source": "macos_vision_ocr_v1"},
    )
    oob_ocr_report = verify_census_corpus(
        [doc],
        primary_rows + [oob_ocr],
        audit_nct_ids=HOLDOUT_SET,
        in_scope_nct_ids=SCOPE_SET,
        page_text_provider=provider,
        expected_outcomes_by_nct={"NCT99000001": 1},
    )
    assert oob_ocr_report.ok is False
    assert [i.code for i in oob_ocr_report.issues] == ["TRIAL_GRID_UNEXPECTED_CELL"]
    assert oob_ocr_report.issues[0].location_key == (1, "margin", "ocr_recovery")


def test_m4_static_isolation_typing_fictional_ncts_and_docstring_disclosure() -> None:
    repo_root = Path(cv_mod.__file__).resolve().parents[1]
    m4_files = (
        "src/citation_verifier.py",
        "src/longitudinal_provenance.py",
        "scripts/verify_and_plan_census_batch.py",
        "tests/test_citation_verifier.py",
        "tests/test_longitudinal_provenance.py",
        "tests/test_verify_and_plan_census_batch.py",
    )
    forbidden_modules = {
        "src.document_indexer",
        "document_indexer",
        "scripts.index_trial_documents",
        "index_trial_documents",
        "src.analysis_population_parser",
        "analysis_population_parser",
        "sponsors.ecosystem",
        "ecosystem",
        "src.outcomes_layout",
        "outcomes_layout",
        "scripts.build_outcomes_shards",
        "build_outcomes_shards",
    }
    nct_token_re = re.compile(r"NCT[0-9]{8}", re.ASCII)

    for rel in m4_files:
        path = repo_root / rel
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel, feature_version=(3, 11))
        assert "from __future__ import annotations" in source
        if not rel.startswith("tests/"):
            assert "stdlib-only" not in source.lower()
            assert "stdlib only" not in source.lower()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, f"{rel} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module not in forbidden_modules, f"{rel} imports {node.module}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.returns is not None, f"{rel}:{node.lineno} {node.name} lacks return hint"
                all_args = (
                    list(node.args.posonlyargs)
                    + list(node.args.args)
                    + list(node.args.kwonlyargs)
                )
                if node.args.vararg is not None:
                    all_args.append(node.args.vararg)
                if node.args.kwarg is not None:
                    all_args.append(node.args.kwarg)
                for arg in all_args:
                    if arg.arg in {"self", "cls"}:
                        continue
                    assert (
                        arg.annotation is not None
                    ), f"{rel}:{node.lineno} {node.name}({arg.arg}) lacks type hint"

        if rel.startswith("tests/"):
            for token in nct_token_re.findall(source):
                assert token.startswith(
                    "NCT99"
                ), f"{rel} contains non-fictional NCT token {token}"
