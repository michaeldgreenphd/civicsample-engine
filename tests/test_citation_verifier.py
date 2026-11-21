"""Offline unit tests for src/citation_verifier.py."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src.citation_verifier import (
    AuditHoldoutViolationError,
    CitationVerificationError,
    DictPageTextProvider,
    DirectoryPageTextProvider,
    ELEMENTS,
    NEUTRAL_LANGUAGE_VOCAB_SHA256,
    PageTextProvider,
    check_neutral_language,
    check_note_page_counts,
    enforce_audit_holdout,
    has_ellipsis,
    match_quotation_on_page,
    normalize_for_page_match,
    scan_neutral_language,
    scan_neutral_language_record,
    verify_census_corpus,
    verify_citation,
    verify_document_record,
    verify_location_record,
    verify_trial_completeness,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _make_doc(
    *,
    nct_id: str = "NCT01234567",
    filename: str = "Prot_000.pdf",
    sha256: str = SHA_A,
    physical_pages: int = 5,
    pages_without_text: list[int] | None = None,
    http_status: int = 200,
    byte_count: int = 1024,
    doc_type: str = "protocol",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "filename": filename,
        "url": f"https://cdn.clinicaltrials.gov/large-docs/67/{nct_id}/{filename}",
        "http_status": http_status,
        "sha256": sha256,
        "bytes": byte_count,
        "doc_type": doc_type,
        "version_label": "Version 2.0",
        "version_date_as_printed": "15 May 2020",
        "physical_pages": physical_pages,
        "pages_without_text": list(pages_without_text) if pages_without_text is not None else [],
        "retrieved_at": "2026-11-14T12:00:00Z",
    }
    if extra:
        rec.update(extra)
    return rec


def _make_citation(
    *,
    filename: str = "Prot_000.pdf",
    sha256: str = SHA_A,
    physical_page: Any = 2,
    printed_page: str = "2",
    section_heading: str = "9.1 Analysis Populations",
    quotation: str = "The primary efficacy analysis will use the ITT population.",
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
    nct_id: str = "NCT01234567",
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
    }
    if extra:
        rec.update(extra)
    return rec


def test_audit_holdout_hard_blocks_all_paths() -> None:
    with pytest.raises(AuditHoldoutViolationError):
        enforce_audit_holdout("NCT01234567", in_audit=True)

    with pytest.raises(AuditHoldoutViolationError):
        enforce_audit_holdout("NCT01234567", audit_nct_ids={"NCT01234567"})

    with pytest.raises(CitationVerificationError):
        enforce_audit_holdout("NCT01234567", in_audit=None)  # type: ignore[arg-type]

    doc = _make_doc(nct_id="NCT99999999")
    with pytest.raises(AuditHoldoutViolationError):
        verify_document_record(doc, audit_nct_ids={"NCT99999999"})

    loc = _make_location(nct_id="NCT99999999")
    with pytest.raises(AuditHoldoutViolationError):
        verify_location_record(loc, docs_by_nct={}, audit_nct_ids={"NCT99999999"})

    with pytest.raises(AuditHoldoutViolationError):
        verify_census_corpus([doc], [], audit_nct_ids={"NCT99999999"})

    doc_flagged = _make_doc(extra={"in_audit": True})
    with pytest.raises(AuditHoldoutViolationError):
        verify_document_record(doc_flagged, strict_keys=False)


def test_document_schema_validation_and_disk_hash(tmp_path: Path) -> None:
    valid_doc = _make_doc()
    assert verify_document_record(valid_doc) == []

    # Missing key and extra key in strict mode.
    missing_doc = dict(valid_doc)
    del missing_doc["sha256"]
    codes = {i.code for i in verify_document_record(missing_doc)}
    assert "DOCUMENT_MISSING_KEYS" in codes

    extra_doc = _make_doc(extra={"readability_status": "machine_readable"})
    assert {i.code for i in verify_document_record(extra_doc, strict_keys=True)} == {
        "DOCUMENT_EXTRA_KEYS"
    }
    assert verify_document_record(extra_doc, strict_keys=False) == []

    # Reject bool-as-int, bad sha256, unsafe path, unsorted pages_without_text.
    bad_doc = _make_doc(
        filename="../Prot_000.pdf",
        sha256="not-a-sha",
        physical_pages=True,  # type: ignore[arg-type]
        byte_count=True,  # type: ignore[arg-type]
        pages_without_text=[3, 2],
    )
    bad_codes = {i.code for i in verify_document_record(bad_doc)}
    assert {
        "INVALID_DOCUMENT_FILENAME",
        "INVALID_SHA256",
        "INVALID_BYTE_COUNT",
        "INVALID_PHYSICAL_PAGES",
        "INVALID_PAGES_WITHOUT_TEXT",
    }.issubset(bad_codes)

    # On-disk byte + sha256 verification.
    pdf_bytes = b"%PDF-1.4\nminimal fixture\n%%EOF\n"
    real_sha = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_path = tmp_path / "NCT01234567" / "Prot_000.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(pdf_bytes)

    good_disk_doc = _make_doc(sha256=real_sha, byte_count=len(pdf_bytes))
    assert verify_document_record(good_disk_doc, pdf_root=tmp_path) == []

    tampered_doc = _make_doc(sha256=SHA_B, byte_count=len(pdf_bytes) + 1)
    tamper_codes = {i.code for i in verify_document_record(tampered_doc, pdf_root=tmp_path)}
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
    assert normalize_for_page_match("random-\nization", repair_line_break_hyphens=True) == "randomization"
    assert normalize_for_page_match("sub\u00ad\njects", repair_line_break_hyphens=True) == "subjects"

    # Ellipsis detector catches literal, spaced, and Unicode ellipses.
    assert has_ellipsis("alpha = 0.05 ... two-sided")
    assert has_ellipsis("alpha = 0.05 . . . two-sided")
    assert has_ellipsis("alpha = 0.05 \u2026 two-sided")
    assert not has_ellipsis("alpha = 0.05 (two-sided).")

    doc = _make_doc(physical_pages=3, pages_without_text=[3])
    docs_map = {"Prot_000.pdf": doc}

    # Empty, 301-char, unnormalized whitespace, ellipsis, bool page, out-of-range page.
    for bad_q, expected_code in (
        ("", "CITATION_EMPTY_QUOTATION"),
        ("x" * 301, "CITATION_QUOTATION_TOO_LONG"),
        ("double  space", "CITATION_QUOTATION_UNNORMALIZED_WHITESPACE"),
        ("line\nbreak", "CITATION_QUOTATION_UNNORMALIZED_WHITESPACE"),
        ("alpha ... 0.05", "CITATION_ELLIPSIS_FORBIDDEN"),
        ("alpha \u2026 0.05", "CITATION_ELLIPSIS_FORBIDDEN"),
    ):
        _, issues = verify_citation(
            "NCT01234567",
            (0, "alpha_and_sidedness", "primary"),
            _make_citation(quotation=bad_q),
            docs_by_filename=docs_map,
        )
        assert expected_code in {i.code for i in issues}

    _, bool_page_issues = verify_citation(
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=True),
        docs_by_filename=docs_map,
    )
    assert "INVALID_CITATION_PAGE" in {i.code for i in bool_page_issues}

    _, range_issues = verify_citation(
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=4),
        docs_by_filename=docs_map,
    )
    assert "CITATION_PAGE_OUT_OF_RANGE" in {i.code for i in range_issues}

    # Citing pages_without_text under pdfkit fails, but succeeds under OCR text_source.
    provider = DictPageTextProvider(
        pages_by_sha256={SHA_A: ["p1", "p2", ""]},
        ocr_pages_by_sha256={SHA_A: ["p1", "p2", "OCR recovered alpha = 0.05 two-sided."]},
    )
    assert isinstance(provider, PageTextProvider)
    _, pwt_pdfkit_issues = verify_citation(
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=3, quotation="OCR recovered alpha = 0.05 two-sided."),
        docs_by_filename=docs_map,
        page_text_provider=provider,
        text_source="pdfkit",
    )
    assert "CITATION_PAGE_WITHOUT_TEXT" in {i.code for i in pwt_pdfkit_issues}

    tier_ocr, pwt_ocr_issues = verify_citation(
        "NCT01234567",
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
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=2, quotation=spanning_quote),
        docs_by_filename={"Prot_000.pdf": doc},
        page_text_provider=provider,
    )
    assert tier == "mismatch"
    assert [i.code for i in issues] == ["cross_page_span_violation"]

    # Explicit range string in physical_page also triggers cross_page_span_violation.
    _, range_str_issues = verify_citation(
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page="2-3", quotation="Cover page"),
        docs_by_filename={"Prot_000.pdf": doc},
    )
    assert "cross_page_span_violation" in {i.code for i in range_str_issues}

    # Truly absent quotation emits CITATION_PAGE_SUBSTRING_MISMATCH.
    tier_absent, absent_issues = verify_citation(
        "NCT01234567",
        (0, "alpha_and_sidedness", "primary"),
        _make_citation(physical_page=2, quotation="Completely unrelated fabricated text."),
        docs_by_filename={"Prot_000.pdf": doc},
        page_text_provider=provider,
    )
    assert tier_absent == "mismatch"
    assert [i.code for i in absent_issues] == ["CITATION_PAGE_SUBSTRING_MISMATCH"]


def test_pages_not_searched_and_unreadable_invariants() -> None:
    doc = _make_doc(physical_pages=5, pages_without_text=[5])
    docs_by_nct = {"NCT01234567": {"Prot_000.pdf": doc}}

    # Contradiction: pages_searched == "all" with non-empty pages_not_searched.
    loc_contra = _make_location(
        status="NOT_FOUND",
        citations=[],
        pages_searched="all",
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_contra = verify_location_record(loc_contra, docs_by_nct=docs_by_nct)
    assert "SEARCH_COVERAGE_CONTRADICTION" in {i.code for i in issues_contra}

    # Exhaustive default mode forbids NOT_FOUND when pages_not_searched is non-empty.
    loc_scoped = _make_location(
        status="NOT_FOUND",
        citations=[],
        pages_searched={"Prot_000.pdf": [1, 2]},
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_exh = verify_location_record(
        loc_scoped, docs_by_nct=docs_by_nct, require_exhaustive_for_not_found=True
    )
    assert "NOT_FOUND_WITH_UNSEARCHED_PAGES" in {i.code for i in issues_exh}

    # Scoped triage mode allows NOT_FOUND ONLY with valid disjoint partition.
    _, issues_scoped = verify_location_record(
        loc_scoped, docs_by_nct=docs_by_nct, require_exhaustive_for_not_found=False
    )
    assert issues_scoped == []

    # Citing a page inside pages_not_searched fails.
    loc_cit_unsearched = _make_location(
        status="FOUND",
        citations=[_make_citation(physical_page=3)],
        pages_searched={"Prot_000.pdf": [1, 2]},
        pages_not_searched={"Prot_000.pdf": [3, 4]},
    )
    _, issues_cit_un = verify_location_record(loc_cit_unsearched, docs_by_nct=docs_by_nct)
    assert "CITATION_IN_UNSEARCHED_PAGES" in {i.code for i in issues_cit_un}

    # UNREADABLE validity checks.
    loc_unreadable_ok = _make_location(
        status="UNREADABLE",
        citations=[],
        note="SAP pages are scanned image-only without extractable text.",
    )
    _, issues_u_ok = verify_location_record(loc_unreadable_ok, docs_by_nct=docs_by_nct)
    assert issues_u_ok == []

    # UNREADABLE on trial with zero documents fails.
    _, issues_u_nodoc = verify_location_record(loc_unreadable_ok, docs_by_nct={})
    assert "UNREADABLE_WITHOUT_DOCUMENT" in {i.code for i in issues_u_nodoc}

    # UNREADABLE contradicting explicitly machine_readable document fails.
    clean_doc = _make_doc(
        physical_pages=5,
        pages_without_text=[],
        extra={"readability_status": "machine_readable", "redaction_markers": []},
    )
    _, issues_u_clean = verify_location_record(
        loc_unreadable_ok,
        docs_by_nct={"NCT01234567": {"Prot_000.pdf": clean_doc}},
    )
    assert "UNREADABLE_ON_MACHINE_READABLE_DOCUMENT" in {i.code for i in issues_u_clean}


def test_neutral_language_scanner_and_exemptions() -> None:
    assert len(NEUTRAL_LANGUAGE_VOCAB_SHA256) == 64
    assert check_neutral_language is scan_neutral_language

    # Evaluative / spin / FDAAA compliance claims MUST be flagged.
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

    # Legitimate protocol / census commentary MUST produce zero flags.
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

    # Verbatim source fields (`quotation`, `statement_verbatim`) are exempt.
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
    # Write gzipped Module 1 style page map under <sha[:2]>/<sha>.json.gz.
    page_dir = tmp_path / "pages"
    target = page_dir / SHA_A[:2] / f"{SHA_A}.json.gz"
    target.parent.mkdir(parents=True)
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        json.dump(
            {
                "sha256": SHA_A,
                "physical_pages": 2,
                "pages": [
                    "Protocol Cover Page",
                    "Primary endpoint is OS analyzed in the ITT population with two-sided alpha = 0.05.",
                ],
            },
            fh,
        )

    provider = DirectoryPageTextProvider(page_dir)
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
        page_text_provider=provider,
        expected_outcomes_by_nct={"NCT01234567": 1},
    )
    assert report.ok is True
    assert report.documents_checked == 1
    assert report.locations_checked == 7
    assert report.citations_checked == 7
    assert report.exact_ws_matches == 7
    assert report.substring_mismatches == 0
    assert report.to_dict()["error_count"] == 0

    # Advisory warning on wrong page count in note does not flip report.ok.
    warn_issues = check_note_page_counts(
        "NCT01234567",
        (0, "analysis_population", "primary"),
        "See Prot_000.pdf (99 pages) for SAP.",
        {"Prot_000.pdf": doc},
    )
    assert len(warn_issues) == 1
    assert warn_issues[0].code == "NOTE_PAGE_COUNT_MISMATCH"
    assert warn_issues[0].severity == "warning"

    # Missing cell caught by verify_trial_completeness.
    missing_issues = verify_trial_completeness(
        "NCT01234567",
        locs[:-1],
        expected_primary_outcomes=1,
    )
    assert [i.code for i in missing_issues] == ["TRIAL_GRID_MISSING_CELL"]


def test_no_document_consistency_with_posted_documents() -> None:
    doc = _make_doc(physical_pages=5)
    docs_by_nct = {"NCT01234567": {"Prot_000.pdf": doc}}

    loc_no_doc = _make_location(
        status="NO_DOCUMENT",
        citations=[],
        pages_searched="all",
        pages_not_searched=[],
        note="No protocol or SAP posted.",
    )

    # Non-empty docs_for_nct MUST fail verify_location_record with NO_DOCUMENT_WITH_POSTED_DOCUMENT.
    _, issues_with_doc = verify_location_record(loc_no_doc, docs_by_nct=docs_by_nct)
    assert len(issues_with_doc) == 1
    issue = issues_with_doc[0]
    assert issue.code == "NO_DOCUMENT_WITH_POSTED_DOCUMENT"
    assert issue.severity == "error"
    assert issue.nct_id == "NCT01234567"
    assert issue.location_key == (0, "analysis_population", "primary")
    assert issue.filename is None
    assert issue.physical_page is None

    # Full 7-element NO_DOCUMENT grid for a trial with a posted document fails verify_census_corpus.
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
        expected_outcomes_by_nct={"NCT01234567": 1},
    )
    assert report_fail.ok is False
    assert report_fail.to_dict()["error_count"] == len(ELEMENTS)
    assert {i.code for i in report_fail.issues} == {"NO_DOCUMENT_WITH_POSTED_DOCUMENT"}

    # Empty docs_for_nct (absent NCT or empty dict) with citations=[] continues to pass cleanly.
    _, issues_absent = verify_location_record(loc_no_doc, docs_by_nct={})
    assert issues_absent == []
    _, issues_empty = verify_location_record(loc_no_doc, docs_by_nct={"NCT01234567": {}})
    assert issues_empty == []

    report_ok = verify_census_corpus(
        [],
        no_doc_grid,
        expected_outcomes_by_nct={"NCT01234567": 1},
    )
    assert report_ok.ok is True
    assert report_ok.issues == ()
