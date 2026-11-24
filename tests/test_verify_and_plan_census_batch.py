"""Offline unit and CLI tests for scripts/verify_and_plan_census_batch.py."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_and_plan_census_batch import (
    PlanScopeError,
    classify_unreadable_subtype,
    compute_doc_set_fingerprint,
    compute_doc_sha256_tuple,
    load_index_rows,
    main,
    plan_incremental_cohort,
)
from src.citation_verifier import (
    AuditHoldoutViolationError,
    CitationVerificationError,
    DictPageTextProvider,
    ELEMENTS,
    InvalidNctIdError,
    verify_document_record,
)

SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64
SHA_4 = "4" * 64
SHA_5 = "5" * 64
SHA_AMENDED = "9" * 64


def _doc(
    nct_id: str,
    filename: str,
    sha256: str,
    *,
    physical_pages: int = 10,
    pages_without_text: list[int] | None = None,
    model_id: Any = "deterministic_indexer_v1",
    profile_version: Any = "1.1.0",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "filename": filename,
        "url": f"https://cdn.clinicaltrials.gov/large-docs/{nct_id[-2:]}/{nct_id}/{filename}",
        "http_status": 200,
        "sha256": sha256,
        "bytes": 4096,
        "doc_type": "protocol",
        "version_label": "v1",
        "version_date_as_printed": "2020-01-15",
        "physical_pages": physical_pages,
        "pages_without_text": [] if pages_without_text is None else list(pages_without_text),
        "retrieved_at": "2026-11-14T12:00:00Z",
        "model_id": model_id,
        "profile_version": profile_version,
    }
    if extra:
        rec.update(extra)
    return rec


def _complete_primary_locations(
    nct_id: str,
    filename: str,
    sha256: str,
    *,
    unreadable_elements: set[str] | None = None,
    unreadable_note: str = "Scanned image-only PDF with no extractable text.",
    model_id: Any = "claude-sonnet-4-6-20260601",
    prompt_version: Any = "decision-rule-v1.1",
) -> list[dict[str, Any]]:
    unreadable_set = unreadable_elements or set()
    rows: list[dict[str, Any]] = []
    for elem in ELEMENTS:
        is_unreadable = elem in unreadable_set
        rows.append(
            {
                "nct_id": nct_id,
                "primary_outcome_index": 0,
                "primary_outcome_title": "Primary Endpoint",
                "element": elem,
                "status": "UNREADABLE" if is_unreadable else "FOUND",
                "citations": (
                    []
                    if is_unreadable
                    else [
                        {
                            "filename": filename,
                            "sha256": sha256,
                            "physical_page": 2,
                            "printed_page": "2",
                            "section_heading": "9 Statistical Methods",
                            "quotation": "Primary endpoint analyzed in the ITT population.",
                        }
                    ]
                ),
                "pages_searched": "all",
                "pages_not_searched": [],
                "note": unreadable_note if is_unreadable else "Found in section 9.",
                "worker": "shard-01",
                "pass": "primary",
                "model_id": model_id,
                "prompt_version": prompt_version,
            }
        )
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _quoted_pages_provider(
    shas: list[str],
    *,
    audit_nct_ids: frozenset[str],
    ocr_pages_by_sha256: dict[str, list[str | None]] | None = None,
) -> DictPageTextProvider:
    """Page text in which page 2 of each document holds the quotation the location helpers cite.

    ``cached_complete`` needs quotations found on their pages, so a plan that is
    to file a trial there is given the pages its rows cite (10 pages, as
    ``_doc`` records).
    """
    pages: list[str | None] = [f"Page {n}." for n in range(1, 11)]
    pages[1] = "9 Statistical Methods\nPrimary endpoint analyzed in the ITT population."
    return DictPageTextProvider(
        pages_by_sha256={sha: pages for sha in shas},
        ocr_pages_by_sha256=ocr_pages_by_sha256,
        audit_nct_ids=audit_nct_ids,
    )


def test_incremental_sha256_skip_amendment_and_unreadable_partition() -> None:
    index_rows = [
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000002",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000003",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000004",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000005",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000006",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99999999",
            "in_audit": True,
            "protocol": True,
            "sap": True,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
    ]

    documents = [
        _doc("NCT99000001", "Prot_000.pdf", SHA_1),
        _doc("NCT99000002", "Prot_000.pdf", SHA_2),
        _doc("NCT99000004", "Prot_000.pdf", SHA_3, pages_without_text=list(range(1, 11))),
        _doc("NCT99000005", "Prot_000.pdf", SHA_4),
        _doc("NCT99000006", "Prot_000.pdf", SHA_5),
    ]
    locations = (
        _complete_primary_locations("NCT99000001", "Prot_000.pdf", SHA_1)
        + _complete_primary_locations("NCT99000002", "Prot_000.pdf", SHA_2)
        + _complete_primary_locations(
            "NCT99000004",
            "Prot_000.pdf",
            SHA_3,
            unreadable_elements={"alpha_and_sidedness", "margin"},
            unreadable_note="Pure scanned image-only PDF.",
        )
        + _complete_primary_locations(
            "NCT99000005",
            "Prot_000.pdf",
            SHA_4,
            unreadable_elements={"multiplicity"},
            unreadable_note="Garbled ToUnicode CMap ciphered text.",
        )
        + _complete_primary_locations(
            "NCT99000006",
            "Prot_000.pdf",
            SHA_5,
            unreadable_elements={"analysis_population"},
            unreadable_note="Sponsor redacted statistical pages with CCI / PPD white-box markers.",
        )
    )

    profiles_by_nct = {
        "NCT99000002": {
            "nct_id": "NCT99000002",
            "in_audit": False,
            "readability_summary": "machine_readable",
            "documents": [_doc("NCT99000002", "Prot_000.pdf", SHA_AMENDED)],
        }
    }
    doc_cache_by_sha256 = {
        SHA_4: {"sha256": SHA_4, "readability_status": "garbled_cmap"},
        SHA_5: {
            "sha256": SHA_5,
            "readability_status": "partial_image_or_redacted",
            "unreadable_recovery_bucket": "redacted_cci_ppd",
            "redaction_markers": ["cci", "ppd"],
        },
    }

    provider = _quoted_pages_provider([SHA_1], audit_nct_ids=frozenset({"NCT99999999"}))
    plan_delta = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=locations,
        doc_cache_by_sha256=doc_cache_by_sha256,
        profiles_by_nct=profiles_by_nct,
        cohort="industry_phase3",
        mode="incremental-delta",
        shard_size=18,
        budget_cap_usd=50.0,
        page_text_provider=provider,
    )
    assert plan_delta["excluded_in_audit_count"] == 1
    assert plan_delta["partitions"]["cached_complete"] == ["NCT99000001"]
    assert plan_delta["partitions"]["amended_doc_or_outcome_delta"] == ["NCT99000002"]
    assert plan_delta["partitions"]["unstarted_primary"] == ["NCT99000003"]
    assert plan_delta["partitions"]["unreadable_recoverable_ocr"] == [
        "NCT99000004",
        "NCT99000005",
    ]
    assert plan_delta["partitions"]["unreadable_terminal_redacted"] == ["NCT99000006"]
    assert plan_delta["unreadable_terminal_redacted_spend_usd"] == 0.0
    assert plan_delta["unreadable_subtypes"] == {
        "image_only_scan": ["NCT99000004"],
        "garbled_cmap": ["NCT99000005"],
        "redacted_cci_ppd": ["NCT99000006"],
    }
    assert plan_delta["launch_approved"] is True
    assert plan_delta["shards"][0]["nct_ids"] == ["NCT99000002", "NCT99000003"]
    assert "NCT99999999" not in str(plan_delta["shards"])

    plan_ocr = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=locations,
        doc_cache_by_sha256=doc_cache_by_sha256,
        profiles_by_nct=profiles_by_nct,
        cohort="industry_phase3",
        mode="recover-unreadable",
        shard_size=10,
        budget_cap_usd=50.0,
        page_text_provider=provider,
    )
    assert plan_ocr["pass_role"] == "ocr_recovery"
    assert plan_ocr["partitions"]["cached_complete"] == ["NCT99000001"]
    assert plan_ocr["partitions"]["amended_doc_or_outcome_delta"] == ["NCT99000002"]
    assert plan_ocr["scheduled_trial_count"] == 2
    assert plan_ocr["shards"][0]["nct_ids"] == ["NCT99000004", "NCT99000005"]
    assert plan_ocr["shards"][0]["target_elements_by_nct"]["NCT99000004"] == [
        {"primary_outcome_index": 0, "element": "alpha_and_sidedness"},
        {"primary_outcome_index": 0, "element": "margin"},
    ]
    assert plan_ocr["shards"][0]["target_elements_by_nct"]["NCT99000005"] == [
        {"primary_outcome_index": 0, "element": "multiplicity"},
    ]
    assert "NCT99000006" not in plan_ocr["shards"][0]["nct_ids"]


def test_budget_cap_guard_and_max_launchable_shards() -> None:
    index_rows: list[dict[str, Any]] = [
        {
            "nct_id": f"NCT990100{i:02d}",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        }
        for i in range(1, 55)
    ] + [
        {
            "nct_id": "NCT99999999",
            "in_audit": True,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        }
    ]
    plan = plan_incremental_cohort(
        index_rows,
        mode="resume-unstarted",
        shard_size=18,
        budget_cap_usd=3.50,
        cost_per_trial_sliced_usd=0.10,
        cost_schedule="sliced",
    )
    assert plan["scheduled_trial_count"] == 54
    assert len(plan["shards"]) == 3
    assert plan["launch_approved"] is False
    assert plan["max_launchable_shards"] == 1
    assert [s["within_budget_cap"] for s in plan["shards"]] == [True, False, False]

    plan_ok = plan_incremental_cohort(
        index_rows,
        mode="resume-unstarted",
        shard_size=18,
        budget_cap_usd=10.00,
        cost_per_trial_sliced_usd=0.10,
        cost_schedule="sliced",
    )
    assert plan_ok["launch_approved"] is True
    assert plan_ok["max_launchable_shards"] == 3


def test_f1_f2_f3_f6_cli_and_library_firewall_strictness(tmp_path: Path) -> None:
    # F1: verify MUST require --index and exit 2 BEFORE reading --documents or --locations.
    nonexistent_docs = tmp_path / "does_not_exist_docs.jsonl"
    nonexistent_locs = tmp_path / "does_not_exist_locs.jsonl"
    rc_no_index = main(
        [
            "verify",
            "--documents",
            str(nonexistent_docs),
            "--locations",
            str(nonexistent_locs),
        ]
    )
    assert rc_no_index == 2

    index_rows = [
        {
            "nct_id": "NCT99999999",
            "in_audit": True,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
    ]

    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(index_rows, explicit_nct_ids={"NCT99999999"})

    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            index_rows,
            documents=[_doc("NCT99999999", "Prot_000.pdf", SHA_1)],
        )

    # Missing or non-bool in_audit in --index raises AuditHoldoutViolationError and exits 2.
    for bad_row in (
        {"nct_id": "NCT99000001", "primary_outcomes_count": 1},
        {"nct_id": "NCT99000001", "in_audit": None, "primary_outcomes_count": 1},
        {"nct_id": "NCT99000001", "in_audit": 0, "primary_outcomes_count": 1},
        {"nct_id": "NCT99000001", "in_audit": "false", "primary_outcomes_count": 1},
    ):
        bad_index = tmp_path / "bad_index.json"
        bad_index.write_text(json.dumps({"data": [bad_row]}), encoding="utf-8")
        with pytest.raises(AuditHoldoutViolationError):
            load_index_rows(bad_index)
        assert main(["plan", "--index", str(bad_index)]) == 2

    # F2: Strict ASCII ^NCT[0-9]{8}$ enforced FIRST; CLI exits 2.
    good_index = tmp_path / "index.json"
    good_index.write_text(json.dumps({"data": index_rows}), encoding="utf-8")
    clean_docs = tmp_path / "clean_docs.jsonl"
    clean_locs = tmp_path / "clean_locs.jsonl"
    _write_jsonl(clean_docs, [_doc("NCT99000001", "Prot_000.pdf", SHA_1)])
    _write_jsonl(clean_locs, _complete_primary_locations("NCT99000001", "Prot_000.pdf", SHA_1))

    for bad_nct in (
        "nct99000001",
        "NCT99000001 ",
        " NCT99000001",
        "NCT99000001\u200b",
        "NCT\u0669\u0669\u0660\u0660\u0660\u0660\u0660\u0661",
    ):
        assert main(["plan", "--index", str(good_index), "--nct-id", bad_nct]) == 2
        bad_nct_index = tmp_path / "bad_nct_index.json"
        bad_nct_index.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "nct_id": bad_nct,
                            "in_audit": False,
                            "primary_outcomes_count": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(InvalidNctIdError):
            load_index_rows(bad_nct_index)
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(bad_nct_index),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(clean_locs),
                ]
            )
            == 2
        )

    # Audit violation in documents/locations/doc-cache/profiles/nct-id exits 2.
    bad_docs = tmp_path / "bad_docs.jsonl"
    _write_jsonl(bad_docs, [_doc("NCT99999999", "Prot_000.pdf", SHA_1)])
    assert (
        main(
            [
                "verify",
                "--index",
                str(good_index),
                "--documents",
                str(bad_docs),
                "--locations",
                str(clean_locs),
            ]
        )
        == 2
    )
    assert main(["plan", "--index", str(good_index), "--nct-id", "NCT99999999"]) == 2

    # F1: --doc-cache holding an audit or unlisted NCT row exits 2.
    bad_cache = tmp_path / "bad_doc_cache.jsonl"
    _write_jsonl(bad_cache, [{"sha256": SHA_1, "nct_id": "NCT99999999"}])
    assert (
        main(
            [
                "plan",
                "--index",
                str(good_index),
                "--doc-cache",
                str(bad_cache),
            ]
        )
        == 2
    )

    unlisted_profiles = tmp_path / "unlisted_profiles.jsonl"
    _write_jsonl(
        unlisted_profiles,
        [{"nct_id": "NCT99000077", "in_audit": False, "documents": []}],
    )
    assert (
        main(
            [
                "plan",
                "--index",
                str(good_index),
                "--profiles",
                str(unlisted_profiles),
            ]
        )
        == 2
    )

    # F3: plan --nct-id absent from --index OR outside selected cohort exits 2.
    assert main(["plan", "--index", str(good_index), "--nct-id", "NCT99000077"]) == 2
    with pytest.raises(PlanScopeError):
        plan_incremental_cohort(
            index_rows,
            cohort="other_cohort",
            explicit_nct_ids={"NCT99000001"},
        )
    assert (
        main(
            [
                "plan",
                "--index",
                str(good_index),
                "--cohort",
                "other_cohort",
                "--nct-id",
                "NCT99000001",
            ]
        )
        == 2
    )

    # F3: Unlisted nct_id in --documents or --locations during verify exits 2.
    unlisted_docs = tmp_path / "unlisted_docs.jsonl"
    _write_jsonl(unlisted_docs, [_doc("NCT99000077", "Prot_000.pdf", SHA_2)])
    assert (
        main(
            [
                "verify",
                "--index",
                str(good_index),
                "--documents",
                str(unlisted_docs),
                "--locations",
                str(clean_locs),
            ]
        )
        == 2
    )
    unlisted_locs = tmp_path / "unlisted_locs.jsonl"
    _write_jsonl(
        unlisted_locs,
        _complete_primary_locations("NCT99000077", "Prot_000.pdf", SHA_2),
    )
    assert (
        main(
            [
                "verify",
                "--index",
                str(good_index),
                "--documents",
                str(clean_docs),
                "--locations",
                str(unlisted_locs),
                "--check-completeness",
            ]
        )
        == 2
    )

    # F3: Missing, None, bool, non-int, 0, or negative expected_k refused loudly in plan & verify.
    for bad_k_row in (
        {"nct_id": "NCT99000001", "in_audit": False, "protocol": True, "sap": False},
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": None,
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": False,
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": True,
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": "2",
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 0,
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": -1,
        },
    ):
        rows_with_bad_k = [index_rows[0], bad_k_row]
        with pytest.raises(CitationVerificationError):
            plan_incremental_cohort(rows_with_bad_k)
        bad_k_index = tmp_path / "bad_k_index.json"
        bad_k_index.write_text(json.dumps({"data": rows_with_bad_k}), encoding="utf-8")
        assert main(["plan", "--index", str(bad_k_index)]) == 1
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(bad_k_index),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(clean_locs),
                    "--check-completeness",
                ]
            )
            == 1
        )

    # F6: Planner also refuses documents/locations missing or carrying blank provenance stamps.
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(
            index_rows,
            documents=[_doc("NCT99000001", "Prot_000.pdf", SHA_1, model_id="   ")],
        )
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(
            index_rows,
            documents=[_doc("NCT99000001", "Prot_000.pdf", SHA_1)],
            locations=_complete_primary_locations(
                "NCT99000001", "Prot_000.pdf", SHA_1, prompt_version=""
            ),
        )


def test_cli_verify_and_plan_end_to_end(tmp_path: Path) -> None:
    index_path = tmp_path / "outcomes_index.json"
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    pages_dir = tmp_path / "pages"
    report_path = tmp_path / "verify_report.json"
    plan_path = tmp_path / "batch_plan.json"

    index_path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "nct_id": "NCT99000067",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "cohorts": ["industry_phase3"],
                        "primary_outcomes_count": 1,
                    },
                    {
                        "nct_id": "NCT99000068",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "cohorts": ["industry_phase3"],
                        "primary_outcomes_count": 1,
                    },
                    {
                        "nct_id": "NCT99999999",
                        "in_audit": True,
                        "protocol": True,
                        "sap": False,
                        "cohorts": ["industry_phase3"],
                        "primary_outcomes_count": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    _write_jsonl(docs_path, [_doc("NCT99000067", "Prot_000.pdf", SHA_1, physical_pages=2)])
    _write_jsonl(
        locs_path,
        _complete_primary_locations("NCT99000067", "Prot_000.pdf", SHA_1),
    )
    pages_dir.mkdir(parents=True)
    (pages_dir / f"{SHA_1}.json").write_text(
        json.dumps(
            {
                "pages": [
                    "Title page",
                    "9 Statistical Methods\nPrimary endpoint analyzed in the ITT population.",
                ]
            }
        ),
        encoding="utf-8",
    )

    rc_v = main(
        [
            "verify",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_v == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["exact_ws_matches"] == 7

    rc_p = main(
        [
            "plan",
            "--index",
            str(index_path),
            "--cohort",
            "industry_phase3",
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--mode",
            "resume-unstarted",
            "--shard-size",
            "18",
            "--budget-cap-usd",
            "10.00",
            "--pages-dir",
            str(pages_dir),
            "--out-plan",
            str(plan_path),
        ]
    )
    assert rc_p == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["launch_approved"] is True
    assert plan["partitions"]["cached_complete"] == ["NCT99000067"]
    assert plan["partitions"]["unstarted_primary"] == ["NCT99000068"]
    assert plan["shards"][0]["nct_ids"] == ["NCT99000068"]

    rc_over = main(
        [
            "plan",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--mode",
            "resume-unstarted",
            "--budget-cap-usd",
            "0.01",
            "--out-plan",
            str(plan_path),
        ]
    )
    assert rc_over == 1
    over_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert over_plan["launch_approved"] is False
    assert over_plan["max_launchable_shards"] == 0


def test_helpers_fingerprint_and_unreadable_classifier() -> None:
    tup_a = compute_doc_sha256_tuple(
        [{"filename": "SAP_001.pdf", "sha256": SHA_2}, {"filename": "Prot_000.pdf", "sha256": SHA_1}]
    )
    tup_b = compute_doc_sha256_tuple(
        [{"filename": "Prot_000.pdf", "sha256": SHA_1}, {"filename": "SAP_001.pdf", "sha256": SHA_2}]
    )
    assert tup_a == tup_b
    assert compute_doc_set_fingerprint(tup_a) == compute_doc_set_fingerprint(tup_b)

    assert (
        classify_unreadable_subtype(
            [_doc("NCT99000067", "SAP_001.pdf", SHA_1)],
            [{"status": "UNREADABLE", "note": "All statistical sections show [CCI] white-box blanking."}],
            {},
        )
        == "redacted_cci_ppd"
    )
    assert (
        classify_unreadable_subtype(
            [_doc("NCT99000067", "SAP_001.pdf", SHA_1)],
            [{"status": "UNREADABLE", "note": "Broken ToUnicode CMap font encoding."}],
            {},
        )
        == "garbled_cmap"
    )
    assert (
        classify_unreadable_subtype(
            [_doc("NCT99000067", "SAP_001.pdf", SHA_1, physical_pages=5, pages_without_text=[1, 2, 3, 4, 5])],
            [{"status": "UNREADABLE", "note": "No text layer."}],
            {},
        )
        == "image_only_scan"
    )


def test_cli_verify_merged_sparse_passes_and_completeness_guards(tmp_path: Path) -> None:
    index_path = tmp_path / "outcomes_index.json"
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    pages_dir = tmp_path / "pages"
    ocr_dir = tmp_path / "ocr_pages"
    report_path = tmp_path / "verify_report.json"

    index_path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "nct_id": "NCT99000067",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "cohorts": ["industry_phase3"],
                        "primary_outcomes_count": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _write_jsonl(
        docs_path,
        [_doc("NCT99000067", "Prot_000.pdf", SHA_1, physical_pages=3, pages_without_text=[3])],
    )
    pages_dir.mkdir(parents=True)
    ocr_dir.mkdir(parents=True)
    (pages_dir / f"{SHA_1}.json").write_text(
        json.dumps(
            {
                "pages": [
                    "Title page",
                    "9 Statistical Methods\nPrimary endpoint analyzed in the ITT population.",
                    "",
                ]
            }
        ),
        encoding="utf-8",
    )
    (ocr_dir / f"{SHA_1}.json").write_text(
        json.dumps(
            {
                "pages": [
                    "Title page",
                    "9 Statistical Methods\nPrimary endpoint analyzed in the ITT population.",
                    "Non-inferiority margin is 10 percentage points.",
                ]
            }
        ),
        encoding="utf-8",
    )

    primary_rows = _complete_primary_locations(
        "NCT99000067",
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin"},
        unreadable_note="Page 3 is a scanned image without extractable text.",
    )
    for row in primary_rows:
        if row["element"] == "multiplicity":
            row["status"] = "AMBIGUOUS"
            row["note"] = "Flagged for adjudication."

    ocr_row: dict[str, Any] = {
        "nct_id": "NCT99000067",
        "primary_outcome_index": 0,
        "primary_outcome_title": "Primary Endpoint",
        "element": "margin",
        "status": "FOUND",
        "citations": [
            {
                "filename": "Prot_000.pdf",
                "sha256": SHA_1,
                "physical_page": 3,
                "printed_page": "3",
                "section_heading": "9.3 Margin",
                "quotation": "Non-inferiority margin is 10 percentage points.",
            }
        ],
        "pages_searched": "all",
        "pages_not_searched": [],
        "note": "Recovered via Vision OCR.",
        "worker": "ocr-shard-01",
        "pass": "ocr_recovery",
        "model_id": "claude-sonnet-4-6-20260601",
        "prompt_version": "decision-rule-v1.1",
        "text_source": "macos_vision_ocr_v1",
    }
    adj_row: dict[str, Any] = {
        "nct_id": "NCT99000067",
        "primary_outcome_index": 0,
        "primary_outcome_title": "Primary Endpoint",
        "element": "multiplicity",
        "status": "FOUND",
        "citations": [
            {
                "filename": "Prot_000.pdf",
                "sha256": SHA_1,
                "physical_page": 2,
                "printed_page": "2",
                "section_heading": "9 Statistical Methods",
                "quotation": "Primary endpoint analyzed in the ITT population.",
            }
        ],
        "pages_searched": "all",
        "pages_not_searched": [],
        "note": "Adjudicated multiplicity.",
        "worker": "adjudicator-01",
        "pass": "adjudication",
        "model_id": "claude-sonnet-4-6-20260601",
        "prompt_version": "decision-rule-v1.1",
    }

    _write_jsonl(locs_path, primary_rows + [ocr_row, adj_row])
    rc_merged = main(
        [
            "verify",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--ocr-dir",
            str(ocr_dir),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_merged == 0
    merged_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert merged_report["ok"] is True
    assert merged_report["locations_checked"] == 9
    assert merged_report["error_count"] == 0

    _write_jsonl(locs_path, [adj_row])
    rc_orphan = main(
        [
            "verify",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_orphan == 1
    orphan_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert orphan_report["error_count"] == len(ELEMENTS)
    assert {i["code"] for i in orphan_report["issues"]} == {"TRIAL_GRID_MISSING_CELL"}
    assert all(i["location_key"][2] == "primary" for i in orphan_report["issues"])

    oob_adj = dict(adj_row)
    oob_adj["primary_outcome_index"] = 1
    _write_jsonl(locs_path, primary_rows + [oob_adj])
    rc_oob = main(
        [
            "verify",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_oob == 1
    oob_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [i["code"] for i in oob_report["issues"]] == ["TRIAL_GRID_UNEXPECTED_CELL"]


def test_planner_adjudication_and_ocr_override_parity() -> None:
    index_rows = [
        {
            "nct_id": "NCT99000011",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000012",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000013",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99000014",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT99999999",
            "in_audit": True,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
    ]
    documents = [
        _doc("NCT99000011", "Prot_000.pdf", SHA_1, pages_without_text=[5]),
        _doc("NCT99000012", "Prot_000.pdf", SHA_2, pages_without_text=[5]),
        _doc("NCT99000013", "Prot_000.pdf", SHA_3),
        _doc("NCT99000014", "Prot_000.pdf", SHA_4, pages_without_text=[5]),
    ]

    locs_11 = _complete_primary_locations(
        "NCT99000011",
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin"},
        unreadable_note="Scanned page 5.",
    ) + [
        {
            "nct_id": "NCT99000011",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": "margin",
            "status": "FOUND",
            "citations": [
                {
                    "filename": "Prot_000.pdf",
                    "sha256": SHA_1,
                    "physical_page": 2,
                    "printed_page": "2",
                    "section_heading": "9 Statistical Methods",
                    "quotation": "Primary endpoint analyzed in the ITT population.",
                }
            ],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Adjudication resolved margin from section 9.",
            "worker": "adjudicator-01",
            "pass": "adjudication",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        }
    ]

    locs_12 = _complete_primary_locations(
        "NCT99000012",
        "Prot_000.pdf",
        SHA_2,
        unreadable_elements={"margin"},
        unreadable_note="Scanned page 5.",
    ) + [
        {
            "nct_id": "NCT99000012",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": "margin",
            "status": "FOUND",
            "citations": [
                {
                    "filename": "Prot_000.pdf",
                    "sha256": SHA_2,
                    "physical_page": 5,
                    "printed_page": "5",
                    "section_heading": "9.3 Margin",
                    "quotation": "Margin text.",
                }
            ],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "OCR recovery attempt.",
            "worker": "ocr-shard-01",
            "pass": "ocr_recovery",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        },
        {
            "nct_id": "NCT99000012",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": "margin",
            "status": "UNREADABLE",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Adjudication reverted cell to UNREADABLE due to image-only scan artifacts.",
            "worker": "adjudicator-01",
            "pass": "adjudication",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        },
    ]

    locs_13 = _complete_primary_locations("NCT99000013", "Prot_000.pdf", SHA_3) + [
        {
            "nct_id": "NCT99000013",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": "margin",
            "status": "UNREADABLE",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Adjudication confirmed [CCI] redaction over margin value.",
            "worker": "adjudicator-01",
            "pass": "adjudication",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        }
    ]

    locs_14 = _complete_primary_locations(
        "NCT99000014",
        "Prot_000.pdf",
        SHA_4,
        unreadable_elements={"margin"},
        unreadable_note="Scanned page 5.",
    ) + [
        {
            "nct_id": "NCT99000014",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": "margin",
            "status": "FOUND",
            "citations": [
                {
                    "filename": "Prot_000.pdf",
                    "sha256": SHA_4,
                    "physical_page": 5,
                    "printed_page": "5",
                    "section_heading": "9.3 Margin",
                    "quotation": "Margin text.",
                }
            ],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Recovered cleanly via OCR.",
            "worker": "ocr-shard-01",
            "pass": "ocr_recovery",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
            # The OCR row cites page 5, a page without pdfkit text; verify reads the row's
            # own text_source (else --text-source, default pdfkit) and reports
            # CITATION_PAGE_WITHOUT_TEXT for a pdfkit citation of it.
            "text_source": "macos_vision_ocr_v1",
        }
    ]

    all_locs = locs_11 + locs_12 + locs_13 + locs_14
    ocr_pages: list[str | None] = [f"OCR page {n}." for n in range(1, 11)]
    ocr_pages[4] = "Margin text."
    provider = _quoted_pages_provider(
        [SHA_1, SHA_2, SHA_3, SHA_4],
        audit_nct_ids=frozenset({"NCT99999999"}),
        ocr_pages_by_sha256={SHA_4: ocr_pages},
    )

    for mode in ("incremental-delta", "recover-unreadable"):
        plan = plan_incremental_cohort(
            index_rows,
            documents=documents,
            locations=all_locs,
            cohort="industry_phase3",
            mode=mode,
            shard_size=10,
            budget_cap_usd=50.0,
            page_text_provider=provider,
        )
        assert plan["partitions"]["cached_complete"] == ["NCT99000011", "NCT99000014"]
        assert plan["partitions"]["unreadable_recoverable_ocr"] == ["NCT99000012"]
        assert plan["partitions"]["unreadable_terminal_redacted"] == ["NCT99000013"]
        assert plan["partitions"]["amended_doc_or_outcome_delta"] == []
        if mode == "recover-unreadable":
            assert plan["scheduled_trial_count"] == 1
            assert plan["shards"][0]["nct_ids"] == ["NCT99000012"]
            assert plan["shards"][0]["target_elements_by_nct"]["NCT99000012"] == [
                {"primary_outcome_index": 0, "element": "margin"}
            ]

    # The same OCR row without text_source is one verify rejects (CITATION_PAGE_WITHOUT_TEXT
    # under pdfkit), so the trial is re-run rather than filed as cached_complete.
    untagged_ocr = {k: v for k, v in locs_14[-1].items() if k != "text_source"}
    untagged_plan = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=locs_11 + locs_12 + locs_13 + locs_14[:-1] + [untagged_ocr],
        cohort="industry_phase3",
        mode="incremental-delta",
        shard_size=10,
        budget_cap_usd=50.0,
        page_text_provider=provider,
    )
    assert untagged_plan["partitions"]["cached_complete"] == ["NCT99000011"]
    assert untagged_plan["partitions"]["amended_doc_or_outcome_delta"] == ["NCT99000014"]


def test_cli_verify_rejects_no_document_using_index_document_flags(tmp_path: Path) -> None:
    index_path = tmp_path / "outcomes_index.json"
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    report_path = tmp_path / "verify_report.json"

    _write_jsonl(docs_path, [])
    no_doc_rows = [
        {
            "nct_id": "NCT99000067",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": elem,
            "status": "NO_DOCUMENT",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "No protocol or SAP posted.",
            "worker": "shard-01",
            "pass": "primary",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        }
        for elem in ELEMENTS
    ]
    _write_jsonl(locs_path, no_doc_rows)

    for proto_flag, sap_flag in ((True, False), (False, True)):
        index_path.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "nct_id": "NCT99000067",
                            "in_audit": False,
                            "protocol": proto_flag,
                            "sap": sap_flag,
                            "primary_outcomes_count": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        rc_fail = main(
            [
                "verify",
                "--index",
                str(index_path),
                "--documents",
                str(docs_path),
                "--locations",
                str(locs_path),
                "--report-out",
                str(report_path),
            ]
        )
        assert rc_fail == 1
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        assert rep["ok"] is False
        assert rep["error_count"] == len(ELEMENTS)
        assert {i["code"] for i in rep["issues"]} == {"NO_DOCUMENT_WITH_POSTED_DOCUMENT"}

    index_path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "nct_id": "NCT99000067",
                        "in_audit": False,
                        "protocol": False,
                        "sap": False,
                        "primary_outcomes_count": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_ok = main(
        [
            "verify",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_ok == 0
    rep_ok = json.loads(report_path.read_text(encoding="utf-8"))
    assert rep_ok["ok"] is True
    assert rep_ok["error_count"] == 0


def _complete_locations_for_outcome(
    nct_id: str,
    filename: str,
    sha256: str,
    *,
    outcome_index: int,
    pass_name: str = "primary",
) -> list[dict[str, Any]]:
    rows = _complete_primary_locations(nct_id, filename, sha256)
    for row in rows:
        row["primary_outcome_index"] = outcome_index
        row["primary_outcome_title"] = f"Primary Endpoint {outcome_index}"
        row["pass"] = pass_name
    return rows


def test_c8_cached_outcome_index_set_and_grid_check() -> None:
    """C8: Cached outcome indices must match exact set(range(expected_k)), not just cardinality."""
    index_rows = [
        {
            "nct_id": "NCT99000101",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 2,
        },
        {
            "nct_id": "NCT99000102",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 2,
        },
        {
            "nct_id": "NCT99000103",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "n_primary_outcomes": 2,
        },
        {
            "nct_id": "NCT99000104",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 2,
        },
        {
            "nct_id": "NCT99999999",
            "in_audit": True,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 1,
        },
    ]
    documents = [
        _doc("NCT99000101", "Prot_000.pdf", SHA_1),
        _doc("NCT99000102", "Prot_000.pdf", SHA_2),
        _doc("NCT99000103", "Prot_000.pdf", SHA_3),
        _doc("NCT99000104", "Prot_000.pdf", SHA_4),
    ]
    locs_101 = _complete_locations_for_outcome(
        "NCT99000101", "Prot_000.pdf", SHA_1, outcome_index=0
    ) + _complete_locations_for_outcome(
        "NCT99000101", "Prot_000.pdf", SHA_1, outcome_index=2
    )
    locs_102 = _complete_locations_for_outcome(
        "NCT99000102", "Prot_000.pdf", SHA_2, outcome_index=0
    ) + _complete_locations_for_outcome(
        "NCT99000102", "Prot_000.pdf", SHA_2, outcome_index=1
    )
    locs_103 = _complete_locations_for_outcome(
        "NCT99000103", "Prot_000.pdf", SHA_3, outcome_index=0
    ) + _complete_locations_for_outcome(
        "NCT99000103", "Prot_000.pdf", SHA_3, outcome_index=2
    )
    locs_104 = _complete_locations_for_outcome(
        "NCT99000104", "Prot_000.pdf", SHA_4, outcome_index=0, pass_name="primary"
    ) + _complete_locations_for_outcome(
        "NCT99000104", "Prot_000.pdf", SHA_4, outcome_index=0, pass_name="ocr_recovery"
    )
    all_locs = locs_101 + locs_102 + locs_103 + locs_104
    provider = _quoted_pages_provider(
        [SHA_1, SHA_2, SHA_3, SHA_4], audit_nct_ids=frozenset({"NCT99999999"})
    )

    plan_delta = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=all_locs,
        mode="incremental-delta",
        page_text_provider=provider,
    )
    assert plan_delta["partitions"]["cached_complete"] == ["NCT99000102"]
    assert plan_delta["partitions"]["amended_doc_or_outcome_delta"] == [
        "NCT99000101",
        "NCT99000103",
        "NCT99000104",
    ]
    assert plan_delta["shards"][0]["nct_ids"] == [
        "NCT99000101",
        "NCT99000103",
        "NCT99000104",
    ]

    plan_ocr = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=all_locs,
        mode="recover-unreadable",
        page_text_provider=provider,
    )
    assert plan_ocr["partitions"]["cached_complete"] == ["NCT99000102"]
    assert plan_ocr["partitions"]["amended_doc_or_outcome_delta"] == [
        "NCT99000101",
        "NCT99000103",
        "NCT99000104",
    ]

    second_locs = (
        _complete_locations_for_outcome(
            "NCT99000101", "Prot_000.pdf", SHA_1, outcome_index=0, pass_name="second"
        )
        + _complete_locations_for_outcome(
            "NCT99000101", "Prot_000.pdf", SHA_1, outcome_index=2, pass_name="second"
        )
        + _complete_locations_for_outcome(
            "NCT99000102", "Prot_000.pdf", SHA_2, outcome_index=0, pass_name="second"
        )
        + _complete_locations_for_outcome(
            "NCT99000102", "Prot_000.pdf", SHA_2, outcome_index=1, pass_name="second"
        )
    )
    plan_second = plan_incremental_cohort(
        index_rows[:2] + [index_rows[-1]],
        documents=documents[:2],
        locations=second_locs,
        mode="incremental-delta",
        pass_role="second",
        page_text_provider=provider,
    )
    assert plan_second["partitions"]["cached_complete"] == ["NCT99000102"]
    assert plan_second["partitions"]["amended_doc_or_outcome_delta"] == ["NCT99000101"]


def test_outcomes_beta_rows_index_support_plan_and_verify(tmp_path: Path) -> None:
    """Live outcomes_beta index.json ('rows' + 'n_primary_outcomes') works read-only in plan & verify."""
    index_path = tmp_path / "index.json"
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    pages_dir = tmp_path / "pages"
    plan_path = tmp_path / "plan.json"
    report_path = tmp_path / "report.json"

    index_path.write_text(
        json.dumps(
            {
                "run_id": "outcomes-2026-11-09-d41f32ec4d",
                "schema_version": 1,
                "rows": [
                    {
                        "nct_id": "NCT99000067",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "n_primary_outcomes": 2,
                    },
                    {
                        "nct_id": "NCT99000068",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "n_primary_outcomes": 1,
                    },
                    {
                        "nct_id": "NCT99999999",
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

    loaded = load_index_rows(index_path)
    assert [r["nct_id"] for r in loaded] == ["NCT99000067", "NCT99000068", "NCT99999999"]

    _write_jsonl(docs_path, [_doc("NCT99000067", "Prot_000.pdf", SHA_1, physical_pages=2)])
    pages_dir.mkdir(parents=True)
    (pages_dir / f"{SHA_1}.json").write_text(
        json.dumps(
            {
                "pages": [
                    "Title page",
                    "9 Statistical Methods\nPrimary endpoint analyzed in the ITT population.",
                ]
            }
        ),
        encoding="utf-8",
    )

    locs_only_0 = _complete_locations_for_outcome(
        "NCT99000067", "Prot_000.pdf", SHA_1, outcome_index=0
    )
    _write_jsonl(locs_path, locs_only_0)

    rc_plan_incomplete = main(
        [
            "plan",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--mode",
            "incremental-delta",
            "--pages-dir",
            str(pages_dir),
            "--out-plan",
            str(plan_path),
        ]
    )
    assert rc_plan_incomplete == 0
    plan_inc = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_inc["excluded_in_audit_count"] == 1
    assert plan_inc["partitions"]["amended_doc_or_outcome_delta"] == ["NCT99000067"]
    assert plan_inc["partitions"]["unstarted_primary"] == ["NCT99000068"]
    assert plan_inc["partitions"]["cached_complete"] == []

    single_row_index = tmp_path / "single_rows_index.json"
    single_row_index.write_text(
        json.dumps(
            {
                "run_id": "outcomes-2026-11-09-d41f32ec4d",
                "schema_version": 1,
                "rows": [
                    {
                        "nct_id": "NCT99000067",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "n_primary_outcomes": 2,
                    },
                    {
                        "nct_id": "NCT99999999",
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
    rc_verify_missing = main(
        [
            "verify",
            "--index",
            str(single_row_index),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_verify_missing == 1
    rep_missing = json.loads(report_path.read_text(encoding="utf-8"))
    assert rep_missing["ok"] is False
    assert {i["code"] for i in rep_missing["issues"]} == {"TRIAL_GRID_MISSING_CELL"}
    assert rep_missing["error_count"] == len(ELEMENTS)

    locs_0_and_1 = locs_only_0 + _complete_locations_for_outcome(
        "NCT99000067", "Prot_000.pdf", SHA_1, outcome_index=1
    )
    _write_jsonl(locs_path, locs_0_and_1)

    rc_plan_complete = main(
        [
            "plan",
            "--index",
            str(index_path),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--mode",
            "incremental-delta",
            "--pages-dir",
            str(pages_dir),
            "--out-plan",
            str(plan_path),
        ]
    )
    assert rc_plan_complete == 0
    plan_ok = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_ok["partitions"]["cached_complete"] == ["NCT99000067"]
    assert plan_ok["partitions"]["unstarted_primary"] == ["NCT99000068"]
    assert plan_ok["partitions"]["amended_doc_or_outcome_delta"] == []

    rc_verify_ok = main(
        [
            "verify",
            "--index",
            str(single_row_index),
            "--documents",
            str(docs_path),
            "--locations",
            str(locs_path),
            "--pages-dir",
            str(pages_dir),
            "--check-completeness",
            "--report-out",
            str(report_path),
        ]
    )
    assert rc_verify_ok == 0
    rep_ok = json.loads(report_path.read_text(encoding="utf-8"))
    assert rep_ok["ok"] is True
    assert rep_ok["error_count"] == 0

    rc_audit = main(
        [
            "plan",
            "--index",
            str(index_path),
            "--nct-id",
            "NCT99999999",
        ]
    )
    assert rc_audit == 2


# ---------------------------------------------------------------------------
# Adversarial Red-Team Suite (strictly fictional NCT99800001..NCT99899999)
# ---------------------------------------------------------------------------

RT_AUDIT_ID = "NCT99888888"
RT_SCOPE_ID = "NCT99800001"
RT_SCOPE_ID_2 = "NCT99800002"
RT_UNLISTED_ID = "NCT99800099"


def _rt_index_rows() -> list[dict[str, Any]]:
    return [
        {
            "nct_id": RT_SCOPE_ID,
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["rt_cohort"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": RT_AUDIT_ID,
            "in_audit": True,
            "protocol": True,
            "sap": True,
            "cohorts": ["rt_cohort"],
            "primary_outcomes_count": 1,
        },
    ]


def test_redteam_cli_verify_missing_index_and_holdout_vectors(tmp_path: Path) -> None:
    """CLI verify red-team: missing --index, missing/non-bool in_audit, unlisted/audit/malformed NCTs, and contaminated --pages-dir."""
    # 1. Missing --index exits 2 BEFORE touching --documents, --locations, or --pages-dir.
    assert (
        main(
            [
                "verify",
                "--documents",
                str(tmp_path / "absent_docs.jsonl"),
                "--locations",
                str(tmp_path / "absent_locs.jsonl"),
                "--pages-dir",
                str(tmp_path / "absent_pages"),
            ]
        )
        == 2
    )

    good_index = tmp_path / "good_index.json"
    good_index.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")
    clean_docs = tmp_path / "clean_docs.jsonl"
    clean_locs = tmp_path / "clean_locs.jsonl"
    _write_jsonl(clean_docs, [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)])
    _write_jsonl(clean_locs, _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1))

    # 2. Missing or non-bool in_audit in --index exits 2.
    for bad_flag_row in (
        {"nct_id": RT_SCOPE_ID, "primary_outcomes_count": 1},
        {"nct_id": RT_SCOPE_ID, "in_audit": None, "primary_outcomes_count": 1},
        {"nct_id": RT_SCOPE_ID, "in_audit": 0, "primary_outcomes_count": 1},
        {"nct_id": RT_SCOPE_ID, "in_audit": "false", "primary_outcomes_count": 1},
    ):
        bad_idx = tmp_path / "bad_flag_index.json"
        bad_idx.write_text(json.dumps({"data": [bad_flag_row]}), encoding="utf-8")
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(bad_idx),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(clean_locs),
                ]
            )
            == 2
        )

    # 3. Malformed, audit (NCT99888888), or unlisted (NCT99800099) NCT IDs in --documents / --locations exit 2.
    for forbidden_id in (
        RT_AUDIT_ID,
        RT_UNLISTED_ID,
        "nct99888888",
        "NCT99888888 ",
        " NCT99888888",
        "NCT99888888\u200b",
        "NCT\u0669\u0669\u0668\u0668\u0668\u0668\u0668\u0668",
    ):
        bad_docs = tmp_path / "bad_docs.jsonl"
        _write_jsonl(bad_docs, [_doc(forbidden_id, "Prot_000.pdf", SHA_1)])
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(good_index),
                    "--documents",
                    str(bad_docs),
                    "--locations",
                    str(clean_locs),
                ]
            )
            == 2
        )
        bad_locs = tmp_path / "bad_locs.jsonl"
        _write_jsonl(
            bad_locs,
            _complete_primary_locations(forbidden_id, "Prot_000.pdf", SHA_1),
        )
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(good_index),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(bad_locs),
                ]
            )
            == 2
        )

    # 4. --pages-dir pointing to audit file, named NCT99888888, or holding sneaky audit filenames exits 2.
    direct_audit_file = tmp_path / "direct_p" / RT_AUDIT_ID / "Prot_000.json"
    direct_audit_file.parent.mkdir(parents=True)
    direct_audit_file.write_text(json.dumps({"pages": ["Holdout"]}), encoding="utf-8")
    assert (
        main(
            [
                "verify",
                "--index",
                str(good_index),
                "--documents",
                str(clean_docs),
                "--locations",
                str(clean_locs),
                "--pages-dir",
                str(direct_audit_file),
            ]
        )
        == 2
    )

    sneaky_pages_dir = tmp_path / "sneaky_p"
    sneaky_pages_dir.mkdir(parents=True)
    (sneaky_pages_dir / f" {RT_AUDIT_ID}.json").write_text(
        json.dumps({"pages": ["Holdout"]}), encoding="utf-8"
    )
    assert (
        main(
            [
                "verify",
                "--index",
                str(good_index),
                "--documents",
                str(clean_docs),
                "--locations",
                str(clean_locs),
                "--pages-dir",
                str(sneaky_pages_dir),
            ]
        )
        == 2
    )


def test_redteam_cli_plan_holdout_unlisted_doc_cache_and_profiles(tmp_path: Path) -> None:
    """CLI plan red-team: NCT99888888, unlisted, and malformed NCTs in --nct-id, --doc-cache, and --profiles."""
    good_index = tmp_path / "good_index.json"
    good_index.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")

    for bad_target in (
        RT_AUDIT_ID,
        RT_UNLISTED_ID,
        "nct99888888",
        "NCT99888888 ",
        "NCT99888888\u200b",
        "NCT\u0669\u0669\u0668\u0668\u0668\u0668\u0668\u0668",
    ):
        assert main(["plan", "--index", str(good_index), "--nct-id", bad_target]) == 2

    # --doc-cache smuggling NCT99888888 via nct_id, nct_ids, nctId, nested metadata, in_audit, or URL.
    sneaky_cache_rows: tuple[dict[str, Any], ...] = (
        {"sha256": SHA_1, "nct_id": RT_AUDIT_ID},
        {"sha256": SHA_1, "nct_id": RT_UNLISTED_ID},
        {"sha256": SHA_1, "nct_id": "nct99888888"},
        {"sha256": SHA_1, "nct_ids": [RT_SCOPE_ID, RT_AUDIT_ID]},
        {"sha256": SHA_1, "nctId": RT_AUDIT_ID},
        {"sha256": SHA_1, "metadata": {"nct_id": RT_AUDIT_ID}},
        {"sha256": SHA_1, "in_audit": True},
        {"sha256": SHA_1, "in_audit": None},
        {"sha256": SHA_1, "in_audit": "false"},
        {
            "sha256": SHA_1,
            "url": f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID}/Prot_000.pdf",
        },
    )
    for idx, cache_row in enumerate(sneaky_cache_rows):
        cache_path = tmp_path / f"cache_{idx}.jsonl"
        _write_jsonl(cache_path, [cache_row])
        assert (
            main(["plan", "--index", str(good_index), "--doc-cache", str(cache_path)]) == 2
        ), f"plan accepted sneaky --doc-cache row: {cache_row!r}"

    # --profiles smuggling NCT99888888 or unlisted/malformed NCTs at top level or nested documents.
    sneaky_profile_rows: tuple[dict[str, Any], ...] = (
        {"nct_id": RT_AUDIT_ID, "in_audit": False, "documents": []},
        {"nct_id": RT_UNLISTED_ID, "in_audit": False, "documents": []},
        {"nct_id": "NCT99888888\u200b", "in_audit": False, "documents": []},
        {"nct_id": RT_SCOPE_ID, "in_audit": 0, "documents": []},
        {
            "nct_id": RT_SCOPE_ID,
            "in_audit": False,
            "documents": [
                {
                    "nct_id": RT_SCOPE_ID,
                    "filename": "Prot_000.pdf",
                    "url": f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID}/Prot_000.pdf",
                }
            ],
        },
    )
    for idx, prof_row in enumerate(sneaky_profile_rows):
        prof_path = tmp_path / f"prof_{idx}.jsonl"
        _write_jsonl(prof_path, [prof_row])
        assert (
            main(["plan", "--index", str(good_index), "--profiles", str(prof_path)]) == 2
        ), f"plan accepted sneaky --profiles row: {prof_row!r}"


def test_redteam_f3_plan_expected_k_index_gaps_stale_sha_and_nodoc_cache(
    tmp_path: Path,
) -> None:
    """F3 red-team: bad expected_k, {0,2} for k=2, stale citation SHA, NO_DOCUMENT cache, and orphan sparse."""
    # 1. expected_k missing, None, False, True, '2', 0, -1, or conflicting primary_outcomes_count vs n_primary_outcomes.
    bad_k_rows: tuple[dict[str, Any], ...] = (
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "sap": False},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": None},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": False},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": True},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": "2"},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": 0},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "primary_outcomes_count": -1},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "n_primary_outcomes": None},
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": True, "n_primary_outcomes": False},
        {
            "nct_id": RT_SCOPE_ID,
            "in_audit": False,
            "protocol": True,
            "primary_outcomes_count": 1,
            "n_primary_outcomes": 2,
        },
        {"nct_id": RT_SCOPE_ID, "in_audit": False, "protocol": None, "primary_outcomes_count": 1},
    )
    audit_anchor = _rt_index_rows()[1]
    for bad_row in bad_k_rows:
        with pytest.raises(CitationVerificationError):
            plan_incremental_cohort([audit_anchor, bad_row])

    # Invalid --pass-role rejected.
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(_rt_index_rows(), pass_role="bogus_pass")

    # 2. {0, 2} for expected_k=2 routed to amended_doc_or_outcome_delta, never cached_complete.
    idx_k2 = [
        {
            "nct_id": RT_SCOPE_ID,
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 2,
        },
        audit_anchor,
    ]
    locs_0_2 = _complete_locations_for_outcome(
        RT_SCOPE_ID, "Prot_000.pdf", SHA_1, outcome_index=0
    ) + _complete_locations_for_outcome(
        RT_SCOPE_ID, "Prot_000.pdf", SHA_1, outcome_index=2
    )
    plan_gap = plan_incremental_cohort(
        idx_k2,
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
        locations=locs_0_2,
    )
    assert plan_gap["partitions"]["cached_complete"] == []
    assert plan_gap["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID]

    # 3. Amended documents.jsonl (SHA_2) while locations still cite stale SHA_1 must NOT hit cached_complete!
    plan_stale_sha = plan_incremental_cohort(
        _rt_index_rows(),
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_2)],
        locations=_complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1),
    )
    assert plan_stale_sha["partitions"]["cached_complete"] == []
    assert plan_stale_sha["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID]

    # 4. NO_DOCUMENT rows (or FOUND rows with 0 citations) when trial has posted document must NOT hit cached_complete!
    no_doc_locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in no_doc_locs:
        row["status"] = "NO_DOCUMENT"
        row["citations"] = []
    plan_nodoc = plan_incremental_cohort(
        _rt_index_rows(),
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
        locations=no_doc_locs,
    )
    assert plan_nodoc["partitions"]["cached_complete"] == []
    assert plan_nodoc["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID]

    # 5. CLI verify --check-completeness with unlisted trial (exits 2) & orphan sparse pass without primary (exits 1).
    good_idx = tmp_path / "idx.json"
    good_idx.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")
    docs_f = tmp_path / "docs.jsonl"
    locs_f = tmp_path / "locs.jsonl"
    rep_f = tmp_path / "rep.json"
    _write_jsonl(docs_f, [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)])

    for sparse_pass in ("adjudication", "ocr_recovery"):
        orphan_row = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)[0]
        orphan_row["pass"] = sparse_pass
        _write_jsonl(locs_f, [orphan_row])
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(good_idx),
                    "--documents",
                    str(docs_f),
                    "--locations",
                    str(locs_f),
                    "--check-completeness",
                    "--report-out",
                    str(rep_f),
                ]
            )
            == 1
        )
        rep = json.loads(rep_f.read_text(encoding="utf-8"))
        assert rep["ok"] is False
        assert {i["code"] for i in rep["issues"]} == {"TRIAL_GRID_MISSING_CELL"}
        assert rep["error_count"] == len(ELEMENTS)


def test_redteam_f6_cli_verify_and_plan_provenance_refusals(tmp_path: Path) -> None:
    """F6 red-team on CLI verify and plan: missing, whitespace, invisible-only, and non-str stamps."""
    idx_path = tmp_path / "index.json"
    idx_path.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")
    clean_docs = tmp_path / "clean_docs.jsonl"
    clean_locs = tmp_path / "clean_locs.jsonl"
    _write_jsonl(clean_docs, [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)])
    _write_jsonl(clean_locs, _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1))

    for bad_stamp in ("", "   ", "\u200b", "\ufeff", None, 123, True):
        bad_doc_p = tmp_path / "bad_doc.jsonl"
        _write_jsonl(
            bad_doc_p,
            [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, model_id=bad_stamp)],
        )
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(idx_path),
                    "--documents",
                    str(bad_doc_p),
                    "--locations",
                    str(clean_locs),
                ]
            )
            == 1
        )
        assert (
            main(
                [
                    "plan",
                    "--index",
                    str(idx_path),
                    "--documents",
                    str(bad_doc_p),
                    "--locations",
                    str(clean_locs),
                ]
            )
            == 1
        )

        bad_loc_p = tmp_path / "bad_loc.jsonl"
        _write_jsonl(
            bad_loc_p,
            _complete_primary_locations(
                RT_SCOPE_ID, "Prot_000.pdf", SHA_1, prompt_version=bad_stamp
            ),
        )
        assert (
            main(
                [
                    "verify",
                    "--index",
                    str(idx_path),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(bad_loc_p),
                ]
            )
            == 1
        )
        assert (
            main(
                [
                    "plan",
                    "--index",
                    str(idx_path),
                    "--documents",
                    str(clean_docs),
                    "--locations",
                    str(bad_loc_p),
                ]
            )
            == 1
        )


# ---------------------------------------------------------------------------
# Follow-up to the m4 audit (fictional NCT99800001..NCT99899999 only)
# ---------------------------------------------------------------------------


def _no_document_rows(nct_id: str) -> list[dict[str, Any]]:
    return [
        {
            "nct_id": nct_id,
            "primary_outcome_index": 0,
            "primary_outcome_title": "Primary Endpoint",
            "element": elem,
            "status": "NO_DOCUMENT",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "No protocol or SAP posted.",
            "worker": "shard-01",
            "pass": "primary",
            "model_id": "claude-sonnet-4-6-20260601",
            "prompt_version": "decision-rule-v1.1",
        }
        for elem in ELEMENTS
    ]


def test_followup_doc_cache_entries_must_tie_to_an_in_scope_document(tmp_path: Path) -> None:
    """A --doc-cache entry is used only when its sha256 is an in-scope trial's document."""
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")
    docs_path = tmp_path / "documents.jsonl"
    _write_jsonl(docs_path, [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)])

    def _plan_rc(cache_rows: list[dict[str, Any]], name: str) -> int:
        cache_path = tmp_path / f"{name}.jsonl"
        _write_jsonl(cache_path, cache_rows)
        return main(
            [
                "plan",
                "--index",
                str(index_path),
                "--documents",
                str(docs_path),
                "--doc-cache",
                str(cache_path),
            ]
        )

    # Positive control: content-addressed entry (no NCT field, as M1 writes it) tied by sha256.
    assert _plan_rc([{"sha256": SHA_1, "readability_status": "machine_readable"}], "tied") == 0

    refused: dict[str, dict[str, Any]] = {
        "audit_id_under_other_key": {"sha256": SHA_1, "trial": RT_AUDIT_ID},
        "unlisted_id_in_free_text": {
            "sha256": SHA_1,
            "section_slice": {"titles": [f"Extension of {RT_UNLISTED_ID}"]},
        },
        "audit_id_as_key": {"sha256": SHA_1, RT_AUDIT_ID: {"readability_status": "x"}},
        "sha_of_no_in_scope_document": {"sha256": SHA_2, "readability_status": "garbled_cmap"},
        "no_sha256": {"readability_status": "garbled_cmap"},
        "uppercase_sha256": {"sha256": "A" * 64},
    }
    for name, row in refused.items():
        assert _plan_rc([row], name) == 2, f"plan accepted --doc-cache row {name}: {row!r}"

    # Library entry point: same rules, plus the key must equal the entry's sha256.
    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            _rt_index_rows(),
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
            doc_cache_by_sha256={SHA_2: {"sha256": SHA_2}},
        )
    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            _rt_index_rows(),
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
            doc_cache_by_sha256={SHA_1: {"sha256": SHA_2}},
        )
    # Tied through a profile's documents rather than --documents.
    plan = plan_incremental_cohort(
        _rt_index_rows(),
        profiles_by_nct={
            RT_SCOPE_ID: {
                "nct_id": RT_SCOPE_ID,
                "documents": [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_3)],
            }
        },
        doc_cache_by_sha256={SHA_3: {"sha256": SHA_3, "readability_status": "machine_readable"}},
        planned_at="2026-11-23T00:00:00+00:00",
    )
    assert plan["partitions"]["unstarted_primary"] == [RT_SCOPE_ID]


def test_followup_missing_document_flags_are_unknown_not_no_document(tmp_path: Path) -> None:
    """An index row lacking protocol/sap flags is not read as having no posted documents."""
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(docs_path, [])
    _write_jsonl(locs_path, _no_document_rows(RT_SCOPE_ID))

    def _index(flags: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _rt_index_rows()
        for flag in ("protocol", "sap"):
            rows[0].pop(flag)
        rows[0].update(flags)
        return rows

    for name, flags in (("no_flags", {}), ("protocol_false_only", {"protocol": False})):
        index_path = tmp_path / f"index_{name}.json"
        index_path.write_text(json.dumps({"data": _index(flags)}), encoding="utf-8")
        rc = main(
            [
                "verify",
                "--index",
                str(index_path),
                "--documents",
                str(docs_path),
                "--locations",
                str(locs_path),
                "--report-out",
                str(report_path),
            ]
        )
        assert rc == 1, name
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        assert {i["code"] for i in rep["issues"]} == {"NO_DOCUMENT_PRESENCE_UNKNOWN"}, name

        # The planner neither schedules the trial nor counts it as no-document.
        with pytest.raises(CitationVerificationError):
            plan_incremental_cohort(_index(flags))
        assert main(["plan", "--index", str(index_path)]) == 1

    # Both flags stated False: NO_DOCUMENT verifies and the planner counts it as no-document.
    index_path = tmp_path / "index_both_false.json"
    known = _index({"protocol": False, "sap": False})
    index_path.write_text(json.dumps({"data": known}), encoding="utf-8")
    assert (
        main(
            [
                "verify",
                "--index",
                str(index_path),
                "--documents",
                str(docs_path),
                "--locations",
                str(locs_path),
            ]
        )
        == 0
    )
    plan = plan_incremental_cohort(known, planned_at="2026-11-23T00:00:00+00:00")
    assert plan["excluded_no_document_count"] == 1
    assert plan["scheduled_trial_count"] == 0


def test_followup_plan_records_why_the_pipeline_commit_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan's provenance stamp is never a bare null: a missing commit carries its reason."""
    import scripts.verify_and_plan_census_batch as planner

    monkeypatch.setattr(planner, "pipeline_commit", lambda: None)
    plan_missing = plan_incremental_cohort(_rt_index_rows(), planned_at="2026-11-23T00:00:00+00:00")
    assert plan_missing["source_pipeline_commit"] is None
    reason = plan_missing["source_pipeline_commit_missing_reason"]
    assert isinstance(reason, str) and "GITHUB_SHA" in reason

    monkeypatch.setattr(planner, "pipeline_commit", lambda: "0" * 40)
    plan_known = plan_incremental_cohort(_rt_index_rows(), planned_at="2026-11-23T00:00:00+00:00")
    assert plan_known["source_pipeline_commit"] == "0" * 40
    assert plan_known["source_pipeline_commit_missing_reason"] is None


def test_followup_zero_audit_manifest_still_refuses_unlisted_trials(tmp_path: Path) -> None:
    """With no in_audit=True row the CLI allows an empty audit set, but scope membership
    still refuses any trial the manifest does not list (exit 2), so a pre-filtered
    in-scope-only manifest does not open the holdout."""
    index_path = tmp_path / "in_scope_only.json"
    index_path.write_text(json.dumps({"data": [_rt_index_rows()[0]]}), encoding="utf-8")
    docs_path = tmp_path / "documents.jsonl"
    locs_path = tmp_path / "locations.jsonl"
    _write_jsonl(locs_path, [])
    for outside_id in (RT_AUDIT_ID, RT_UNLISTED_ID):
        _write_jsonl(docs_path, [_doc(outside_id, "Prot_000.pdf", SHA_1)])
        verify_args = ["verify", "--index", str(index_path)]
        verify_args += ["--documents", str(docs_path), "--locations", str(locs_path)]
        assert main(verify_args) == 2
        assert main(["plan", "--index", str(index_path), "--documents", str(docs_path)]) == 2
        assert main(["plan", "--index", str(index_path), "--nct-id", outside_id]) == 2


# ---------------------------------------------------------------------------
# Round-2 red team on m4 (fictional NCT99800001..NCT99899999 only).
#
# Each test asserts the behaviour the module's own rules call for. Tests for a
# bypass fail until the module is fixed; tests for an attempt that held pass.
# ---------------------------------------------------------------------------

_RT_M4_QUOTE = "Primary endpoint analyzed in the ITT population."
_RT_M4_PAGES: list[str] = [f"Page {n}." for n in range(1, 11)]
_RT_M4_PAGES[1] = _RT_M4_QUOTE


def _rt_m4_inputs(
    root: Path,
    name: str,
    *,
    documents: list[dict[str, Any]] | None = None,
    locations: list[dict[str, Any]] | None = None,
    index_rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Write one case's index/documents/locations and return the common verify arguments."""
    case = root / name
    case.mkdir(parents=True, exist_ok=True)
    index_path = case / "index.json"
    index_path.write_text(
        json.dumps({"data": _rt_index_rows() if index_rows is None else index_rows}),
        encoding="utf-8",
    )
    docs_path = case / "documents.jsonl"
    locs_path = case / "locations.jsonl"
    _write_jsonl(
        docs_path,
        [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)] if documents is None else documents,
    )
    _write_jsonl(
        locs_path,
        _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        if locations is None
        else locations,
    )
    return ["--index", str(index_path), "--documents", str(docs_path), "--locations", str(locs_path)]


def _rt_m4_pages_dir(root: Path, entries: dict[str, Any]) -> Path:
    """Write page maps (``{relative path: payload}``) under ``root`` and return it."""
    for rel, payload in entries.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
    return root


def test_redteam_m4_cli_audit_id_under_unrecognised_key(tmp_path: Path) -> None:
    """verify and plan accept rows naming the audit trial under keys the walk does not know.

    The doc-cache rows are scanned in every key and string
    (``_scan_strings_for_nct_tokens``); --locations rows (verify and plan) and
    --documents rows (plan) are checked only under the three NCT keys and
    five URL keys, and a nested list under ``pages`` is skipped.
    """
    audit_url = f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID}/Prot_000.pdf"
    vectors: dict[str, Any] = {
        "NCTId": RT_AUDIT_ID,
        "nctIdAliases": [RT_AUDIT_ID],
        "trial": RT_AUDIT_ID,
        "href": audit_url,
        "pages": [[{"nct_id": RT_AUDIT_ID}]],
    }
    for key, value in vectors.items():
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0][key] = value
        loc_args = _rt_m4_inputs(tmp_path, f"loc_{key}", locations=locs)
        assert main(["verify", *loc_args]) == 2, f"verify accepted a location row with {key}"
        assert main(["plan", *loc_args]) == 2, f"plan accepted a location row with {key}"

        doc_args = _rt_m4_inputs(
            tmp_path,
            f"doc_{key}",
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, extra={key: value})],
        )
        assert main(["plan", *doc_args]) == 2, f"plan accepted a document row with {key}"
        assert main(["verify", *doc_args]) == 2, (
            f"verify read a document row with {key} instead of refusing it"
        )


def test_redteam_m4_cli_pages_dir_does_not_parse_unreferenced_page_maps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--pages-dir preflight opens and parses every page map, including content no document names.

    A ``<sha256>.json`` whose sha256 is no in-scope document's names no trial,
    so it may be an audit trial's document text; the doc-cache rule refuses
    exactly such an entry. A malformed one shows the parse: verify fails
    with ``Malformed JSON`` in a file it had no reason to open.
    """
    args = _rt_m4_inputs(tmp_path, "unreferenced")
    clean = _rt_m4_pages_dir(tmp_path / "pages_clean", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    assert main(["verify", *args, "--pages-dir", str(clean)]) == 0
    capsys.readouterr()

    with_unreferenced = _rt_m4_pages_dir(
        tmp_path / "pages_unreferenced",
        {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}, f"{SHA_5}.json": "{not json"},
    )
    rc = main(["verify", *args, "--pages-dir", str(with_unreferenced)])
    err = capsys.readouterr().err
    assert "Malformed JSON" not in err, "verify parsed a page map no in-scope document names"
    assert rc in (0, 2)


def test_redteam_m4_cli_page_map_symlink_into_audit_path_refused(tmp_path: Path) -> None:
    """A page map reached through a symlink into an audit trial's path is served.

    The layout checks read the names under --pages-dir (and the root's own
    path), never the resolved target, so a file link, a per-trial directory
    link, or a root link into ``.../<audit NCT>/...`` passes and its text
    verifies the citations.
    """
    args = _rt_m4_inputs(tmp_path, "symlink_pages")
    audit_store = tmp_path / "store" / RT_AUDIT_ID
    audit_store.mkdir(parents=True)
    (audit_store / "Prot_000.json").write_text(json.dumps({"pages": _RT_M4_PAGES}), encoding="utf-8")
    (audit_store / f"{SHA_1}.json").write_text(json.dumps({"pages": _RT_M4_PAGES}), encoding="utf-8")

    file_link = tmp_path / "pages_file_link"
    file_link.mkdir()
    (file_link / f"{SHA_1}.json").symlink_to(audit_store / f"{SHA_1}.json")
    dir_link = tmp_path / "pages_dir_link"
    dir_link.mkdir()
    (dir_link / RT_SCOPE_ID).symlink_to(audit_store, target_is_directory=True)
    root_link = tmp_path / "pages_root_link"
    root_link.symlink_to(audit_store, target_is_directory=True)

    for name, pages_dir in (("file", file_link), ("directory", dir_link), ("root", root_link)):
        assert main(["verify", *args, "--pages-dir", str(pages_dir)]) == 2, (
            f"verify served page text through a {name} symlink into an audit trial's path"
        )


def test_redteam_m4_cli_pdf_root_symlink_into_audit_path_refused(tmp_path: Path) -> None:
    """--pdf-root hashes a PDF reached through a symlink into an audit trial's path.

    ``_validate_pdf_root_firewall`` checks path components relative to the
    root, and ``verify_document_record`` then reads the bytes through the link.
    """
    pdf_bytes = b"%PDF-1.4 fictional red-team document"
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    args = _rt_m4_inputs(
        tmp_path,
        "symlink_pdf",
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", pdf_sha, extra={"bytes": len(pdf_bytes)})],
        locations=_complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", pdf_sha),
    )
    control_root = tmp_path / "pdf_control"
    (control_root / RT_SCOPE_ID).mkdir(parents=True)
    (control_root / RT_SCOPE_ID / "Prot_000.pdf").write_bytes(pdf_bytes)
    # Round 5: verify without page text is not ok (exit 1), so the control gives its pages.
    control_pages = _rt_m4_pages_dir(
        tmp_path / "pdf_control_pages", {f"{pdf_sha}.json": {"pages": _RT_M4_PAGES}}
    )
    assert main(
        ["verify", *args, "--pdf-root", str(control_root), "--pages-dir", str(control_pages)]
    ) == 0

    audit_store = tmp_path / "pdf_store" / RT_AUDIT_ID
    audit_store.mkdir(parents=True)
    (audit_store / "Prot_000.pdf").write_bytes(pdf_bytes)
    file_link_root = tmp_path / "pdf_file_link"
    (file_link_root / RT_SCOPE_ID).mkdir(parents=True)
    (file_link_root / RT_SCOPE_ID / "Prot_000.pdf").symlink_to(audit_store / "Prot_000.pdf")
    dir_link_root = tmp_path / "pdf_dir_link"
    dir_link_root.mkdir()
    (dir_link_root / RT_SCOPE_ID).symlink_to(audit_store, target_is_directory=True)

    for name, pdf_root in (("file", file_link_root), ("directory", dir_link_root)):
        assert main(["verify", *args, "--pdf-root", str(pdf_root)]) == 2, (
            f"verify read a PDF through a {name} symlink into an audit trial's path"
        )


def test_redteam_m4_cli_not_found_without_documents(tmp_path: Path) -> None:
    """verify passes NOT_FOUND rows for a trial with no supplied document.

    The same trial's NO_DOCUMENT rows are refused when presence is unknown
    (NO_DOCUMENT_PRESENCE_UNKNOWN); NOT_FOUND claims a search of documents
    the corpus does not hold and is accepted whatever the index flags say.
    """
    not_found = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in not_found:
        row["status"] = "NOT_FOUND"
        row["citations"] = []

    def _index(flags: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _rt_index_rows()
        rows[0].pop("protocol")
        rows[0].pop("sap")
        rows[0].update(flags)
        return rows

    cases: dict[str, dict[str, Any]] = {
        "flags_both_false": {"protocol": False, "sap": False},
        "flags_missing": {},
        "protocol_true": {"protocol": True, "sap": False},
    }
    for name, flags in cases.items():
        args = _rt_m4_inputs(
            tmp_path, f"nf_{name}", documents=[], locations=not_found, index_rows=_index(flags)
        )
        assert main(["verify", *args, "--check-completeness"]) == 1, (
            f"{name}: NOT_FOUND verified for a trial with no supplied document"
        )


def test_redteam_m4_plan_cached_complete_only_for_rows_that_verify() -> None:
    """plan files a trial as cached_complete (never re-run) although verify rejects its rows.

    ``_verified_pass_doc_fingerprints`` checks the grid, status-to-citation
    counts and citation shape only: NOT_FOUND with unsearched pages
    (verify: NOT_FOUND_WITH_UNSEARCHED_PAGES), ``"all"`` contradicting
    ``pages_not_searched`` (SEARCH_COVERAGE_CONTRADICTION), rows missing
    required keys (LOCATION_MISSING_KEYS) and extra rows with an unknown pass
    holding a ``[None]`` citation (INVALID_PASS, INVALID_CITATION_TYPE) all
    leave the trial cached_complete.
    """
    documents = [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)]
    # Page text for the quotations, so that only the row rules under test keep a trial
    # out of cached_complete (without it no trial is cached).
    provider = _quoted_pages_provider([SHA_1], audit_nct_ids=frozenset({RT_AUDIT_ID}))
    control = plan_incremental_cohort(
        _rt_index_rows(),
        documents=documents,
        locations=_complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1),
        planned_at="2026-11-23T00:00:00+00:00",
        page_text_provider=provider,
    )
    assert control["partitions"]["cached_complete"] == [RT_SCOPE_ID]

    def _rows(update: dict[str, Any], drop: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        rows = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        for row in rows:
            row.update(update)
            for key in drop:
                row.pop(key)
        return rows

    unknown_pass = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    unknown_pass.append({**unknown_pass[0], "pass": "bogus", "citations": [None]})
    vectors: dict[str, list[dict[str, Any]]] = {
        "not_found_with_unsearched_pages": _rows(
            {
                "status": "NOT_FOUND",
                "citations": [],
                "pages_searched": {"Prot_000.pdf": list(range(1, 9))},
                "pages_not_searched": {"Prot_000.pdf": [9, 10]},
            }
        ),
        "all_contradicts_unsearched": _rows(
            {"status": "NOT_FOUND", "citations": [], "pages_not_searched": {"Prot_000.pdf": [9, 10]}}
        ),
        "missing_required_keys": _rows({}, drop=("pages_searched", "note", "worker")),
        "extra_row_unknown_pass": unknown_pass,
    }
    for name, rows in vectors.items():
        try:
            plan = plan_incremental_cohort(
                _rt_index_rows(),
                documents=documents,
                locations=rows,
                planned_at="2026-11-23T00:00:00+00:00",
                page_text_provider=provider,
            )
        except CitationVerificationError:
            continue
        assert RT_SCOPE_ID not in plan["partitions"]["cached_complete"], (
            f"{name}: rows verify rejects were filed as cached_complete"
        )


def test_redteam_m4_plan_unknown_mode_refused() -> None:
    """plan_incremental_cohort reads any unknown mode as incremental-delta.

    The CLI restricts --mode to three choices, but the library entry point
    falls through to the ``else`` branch: ``"recover_unreadable"`` (one
    character off) schedules unstarted and amended trials on the primary pass
    instead of OCR recoveries, with no error.
    """
    for bad_mode in ("recover_unreadable", "resume-unstarted ", "RESUME-UNSTARTED", ""):
        with pytest.raises(CitationVerificationError):
            plan_incremental_cohort(
                _rt_index_rows(),
                documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
                mode=bad_mode,  # type: ignore[arg-type]
                planned_at="2026-11-23T00:00:00+00:00",
            )
            pytest.fail(f"plan accepted mode={bad_mode!r}")


def test_redteam_m4_cli_index_in_audit_variants_refused(tmp_path: Path) -> None:
    """Attempt that held: an index row whose in_audit is not a JSON bool stops verify and plan."""
    for idx, flag in enumerate(("<missing>", None, "false", "False", 0, 1, "true", [], {})):
        rows = _rt_index_rows()
        if flag == "<missing>":
            rows[0].pop("in_audit")
        else:
            rows[0]["in_audit"] = flag
        args = _rt_m4_inputs(tmp_path, f"flag_{idx}", index_rows=rows)
        assert main(["verify", *args]) == 2, f"verify accepted in_audit={flag!r}"
        assert main(["plan", "--index", args[1]]) == 2, f"plan accepted in_audit={flag!r}"


def test_redteam_m4_cli_malformed_index_ids_refused(tmp_path: Path) -> None:
    """Attempt that held: lowercase, padded or zero-width IDs in the index itself stop both commands."""
    for idx, bad in enumerate(
        ("nct99800001", "NCT99800001 ", " NCT99800001", "NCT99800001​", "NCT٩٩800001")
    ):
        rows = _rt_index_rows()
        rows[0]["nct_id"] = bad
        args = _rt_m4_inputs(tmp_path, f"bad_index_id_{idx}", index_rows=rows)
        assert main(["verify", *args]) == 2, f"verify accepted index nct_id={bad!r}"
        assert main(["plan", "--index", args[1]]) == 2, f"plan accepted index nct_id={bad!r}"
        # An ID that exists only in a nested field, with no top-level nct_id, is refused too.
        nested = _rt_index_rows()
        nested[0].pop("nct_id")
        nested[0]["protocolSection"] = {"identificationModule": {"nctId": RT_SCOPE_ID}}
        nested_args = _rt_m4_inputs(tmp_path, f"nested_index_id_{idx}", index_rows=nested)
        assert main(["plan", "--index", nested_args[1]]) == 2


def test_redteam_m4_cli_empty_audit_set_still_scoped(tmp_path: Path) -> None:
    """Attempt that held: with no in_audit=True row, scope membership alone refuses other trials."""
    in_scope_only = [_rt_index_rows()[0]]
    for outside_id in (RT_AUDIT_ID, RT_UNLISTED_ID):
        args = _rt_m4_inputs(
            tmp_path,
            f"empty_audit_locs_{outside_id}",
            locations=_complete_primary_locations(outside_id, "Prot_000.pdf", SHA_1),
            index_rows=in_scope_only,
        )
        assert main(["verify", *args]) == 2
        assert main(["plan", *args]) == 2

        clean_args = _rt_m4_inputs(
            tmp_path, f"empty_audit_pages_{outside_id}", index_rows=in_scope_only
        )
        for layout in (f"{outside_id}/Prot_000.json", f"{outside_id}__Prot_000.json"):
            pages_dir = _rt_m4_pages_dir(
                tmp_path / f"pages_{outside_id}_{layout.replace('/', '_')}",
                {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}, layout: {"pages": _RT_M4_PAGES}},
            )
            assert main(["verify", *clean_args, "--pages-dir", str(pages_dir)]) == 2, layout


def test_redteam_m4_cli_doc_cache_audit_ids_in_any_form_refused(tmp_path: Path) -> None:
    """Attempt that held: --doc-cache refuses an audit ID under any key, in a nested list, or in a URL."""
    args = _rt_m4_inputs(tmp_path, "doc_cache_forms")
    plan_args = ["plan", args[0], args[1], args[2], args[3]]
    rows: dict[str, dict[str, Any]] = {
        "NCTId": {"sha256": SHA_1, "NCTId": RT_AUDIT_ID},
        "pages_list_of_lists": {"sha256": SHA_1, "pages": [[{"nct_id": RT_AUDIT_ID}]]},
        "href": {"sha256": SHA_1, "href": f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID}/x.pdf"},
        "lowercase_in_text": {"sha256": SHA_1, "title": f"See {RT_AUDIT_ID.lower()}"},
        "zero_width_in_text": {"sha256": SHA_1, "title": f"See {RT_AUDIT_ID[:5]}​{RT_AUDIT_ID[5:]}"},
    }
    for name, row in rows.items():
        cache_path = tmp_path / f"cache_{name}.jsonl"
        _write_jsonl(cache_path, [row])
        assert main([*plan_args, "--doc-cache", str(cache_path)]) == 2, name


def test_redteam_m4_cli_expected_k_variants_refused_by_verify(tmp_path: Path) -> None:
    """Attempt that held: verify checks every in-scope row's expected_k even without --check-completeness."""
    for idx, bad_k in enumerate(("<missing>", 0, -1, True, False, None, "1", 1.0)):
        rows = _rt_index_rows()
        rows[0].pop("primary_outcomes_count")
        if bad_k != "<missing>":
            rows[0]["primary_outcomes_count"] = bad_k
        args = _rt_m4_inputs(tmp_path, f"bad_k_{idx}", index_rows=rows)
        assert main(["verify", *args]) == 1, f"verify accepted primary_outcomes_count={bad_k!r}"
        assert main(["plan", "--index", args[1]]) == 1, f"plan accepted primary_outcomes_count={bad_k!r}"


def test_redteam_m4_cli_check_completeness_and_index_rows(tmp_path: Path) -> None:
    """Attempt that held: a corpus trial without an index row is refused; an index trial without rows is missing."""
    unlisted_args = _rt_m4_inputs(
        tmp_path,
        "completeness_unlisted",
        locations=_complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        + _complete_primary_locations(RT_UNLISTED_ID, "Prot_000.pdf", SHA_1),
    )
    assert main(["verify", *unlisted_args, "--check-completeness"]) == 2

    index_rows = _rt_index_rows() + [
        {
            "nct_id": RT_SCOPE_ID_2,
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "primary_outcomes_count": 1,
        }
    ]
    args = _rt_m4_inputs(tmp_path, "completeness_rowless", index_rows=index_rows)
    report_path = tmp_path / "completeness_rowless" / "report.json"
    rc = main(["verify", *args, "--check-completeness", "--report-out", str(report_path)])
    assert rc == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    missing = [i for i in report["issues"] if i["code"] == "TRIAL_GRID_MISSING_CELL"]
    assert {i["nct_id"] for i in report["issues"]} == {RT_SCOPE_ID_2}
    assert len(missing) == len(ELEMENTS)


def test_redteam_m4_cli_bad_citation_shapes_and_status_rows(tmp_path: Path) -> None:
    """Attempt that held: [None], ["x"], [{}] and citation-less NOT_APPLICABLE fail verify and are re-run by plan."""
    cases: dict[str, tuple[dict[str, Any], set[str]]] = {
        "none": ({"citations": [None]}, {"INVALID_CITATION_TYPE"}),
        "string": ({"citations": ["x"]}, {"INVALID_CITATION_TYPE"}),
        "empty_object": ({"citations": [{}]}, {"CITATION_MISSING_KEYS", "CITATION_UNKNOWN_DOCUMENT"}),
        "not_applicable_no_citation": (
            {"status": "NOT_APPLICABLE", "citations": []},
            {"STATUS_REQUIRES_CITATION"},
        ),
    }
    for name, (update, codes) in cases.items():
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0].update(update)
        args = _rt_m4_inputs(tmp_path, f"shape_{name}", locations=locs)
        report_path = tmp_path / f"shape_{name}" / "report.json"
        assert main(["verify", *args, "--report-out", str(report_path)]) == 1, name
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert codes <= {i["code"] for i in report["issues"]}, name

        plan = plan_incremental_cohort(
            _rt_index_rows(),
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
            locations=locs,
            planned_at="2026-11-23T00:00:00+00:00",
        )
        assert plan["partitions"]["cached_complete"] == [], name
        assert plan["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID], name


def test_redteam_m4_cli_mixed_nct_id_targets_refused(tmp_path: Path) -> None:
    """Attempt that held: one unlisted, audit or out-of-cohort --nct-id among valid ones stops plan."""
    rows = _rt_index_rows() + [
        {
            "nct_id": RT_SCOPE_ID_2,
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["other_cohort"],
            "primary_outcomes_count": 1,
        }
    ]
    args = _rt_m4_inputs(tmp_path, "mixed_targets", index_rows=rows)
    base = ["plan", "--index", args[1], "--cohort", "rt_cohort", "--nct-id", RT_SCOPE_ID]
    assert main(base) == 0
    for extra in (RT_UNLISTED_ID, RT_AUDIT_ID, RT_SCOPE_ID_2):
        assert main([*base, "--nct-id", extra]) == 2, extra


# ---------------------------------------------------------------------------
# Round-3 red team on m4 at e677d79 (fictional NCT99800001..NCT99899999 only).
#
# Each test asserts the behaviour the module's own rules call for. Tests for a
# bypass fail until the module is fixed; tests for an attempt that held pass.
# ---------------------------------------------------------------------------


def _rt_m4_r3_obfuscated_audit_ids() -> dict[str, str]:
    """Renderings of ``RT_AUDIT_ID`` a reader takes for it but ``nct[0-9]{7,10}`` misses."""
    prefix, digits = RT_AUDIT_ID[:3], RT_AUDIT_ID[3:]
    return {
        "space": f"{prefix} {digits}",
        "hyphen": f"{prefix}-{digits}",
        "underscore": f"{prefix}_{digits}",
        "no_break_space": f"{prefix} {digits}",
        "cyrillic_es": f"NСT{digits}",
        "variation_selector": f"{RT_AUDIT_ID[:4]}️{RT_AUDIT_ID[4:]}",
    }


def test_redteam_m4_cli_obfuscated_audit_id_tokens_refused(tmp_path: Path) -> None:
    """verify and plan accept rows, doc-cache entries and page maps naming the audit trial as ``NCT 9...``.

    The CLI's token rule (``_scan_record_strings_for_nct_tokens``) refuses the
    plain ID in any key or string; a separator between prefix and digits, a
    look-alike letter or a variation selector passes, and the row is recorded.
    """
    clean_args = _rt_m4_inputs(tmp_path, "obf_control")
    # Round 5: verify without page text is not ok (exit 1), so the control gives its pages.
    control_pages = _rt_m4_pages_dir(
        tmp_path / "obf_verify_control_pages", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}}
    )
    assert main(["verify", *clean_args, "--pages-dir", str(control_pages)]) == 0
    clean_cache = tmp_path / "obf_cache_control.jsonl"
    _write_jsonl(clean_cache, [{"sha256": SHA_1, "title": "Protocol version 2"}])
    assert main(["plan", *clean_args, "--doc-cache", str(clean_cache)]) == 0
    clean_pages = _rt_m4_pages_dir(
        tmp_path / "obf_pages_control", {f"{SHA_1}.json": {"source": "pdfkit", "pages": _RT_M4_PAGES}}
    )
    assert main(["verify", *clean_args, "--pages-dir", str(clean_pages)]) == 0

    for name, token in _rt_m4_r3_obfuscated_audit_ids().items():
        text = f"Same protocol as {token}."
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0]["note"] = text
        loc_args = _rt_m4_inputs(tmp_path, f"obf_loc_{name}", locations=locs)
        assert main(["verify", *loc_args]) == 2, f"{name}: verify accepted the location note"
        assert main(["plan", *loc_args]) == 2, f"{name}: plan accepted the location note"

        cache = tmp_path / f"obf_cache_{name}.jsonl"
        _write_jsonl(cache, [{"sha256": SHA_1, "title": text}])
        assert main(["plan", *clean_args, "--doc-cache", str(cache)]) == 2, (
            f"{name}: plan accepted the doc-cache entry"
        )

        pages = _rt_m4_pages_dir(
            tmp_path / f"obf_pages_{name}", {f"{SHA_1}.json": {"source": text, "pages": _RT_M4_PAGES}}
        )
        assert main(["verify", *clean_args, "--pages-dir", str(pages)]) == 2, (
            f"{name}: verify served a page map whose metadata names the audit trial"
        )


def test_redteam_m4_cli_top_level_list_page_map_refused(tmp_path: Path) -> None:
    """verify --pages-dir serves a list-shaped page map declaring the audit trial.

    A JSON list of page objects is parsed as pages but never firewalled
    (``_validate_payload_firewall`` returns early for a non-mapping), so
    ``nct_id`` of the audit trial and ``in_audit: true`` on every page pass.
    """
    args = _rt_m4_inputs(tmp_path, "list_map")
    objects = [
        {"physical_page": n, "text": t, "nct_id": RT_AUDIT_ID, "in_audit": True}
        for n, t in enumerate(_RT_M4_PAGES, start=1)
    ]
    layouts = {
        "sha_named": f"{SHA_1}.json",
        "trial_directory": f"{RT_SCOPE_ID}/Prot_000.json",
        "trial_prefixed": f"{RT_SCOPE_ID}__Prot_000.json",
    }
    for name, rel in layouts.items():
        pages = _rt_m4_pages_dir(tmp_path / f"list_pages_{name}", {rel: objects})
        assert main(["verify", *args, "--pages-dir", str(pages)]) == 2, (
            f"{name}: verify served a list page map declaring the audit trial"
        )


def test_redteam_m4_cli_jsonl_rows_naming_no_document_refused(tmp_path: Path) -> None:
    """verify --pages-dir parses JSONL page rows that name no document and drops them silently.

    With referenced documents, a row declaring an unreferenced sha256 is
    refused loudly (``_ingest_jsonl_file``), because a map no in-scope
    document names may hold any trial's text. A row declaring no sha256 (and
    no filename) is the same kind of map; it is parsed and discarded.
    """
    args = _rt_m4_inputs(tmp_path, "anon_rows")
    referenced = json.dumps({"sha256": SHA_1, "pages": _RT_M4_PAGES}) + "\n"
    control = _rt_m4_pages_dir(tmp_path / "anon_control", {"maps.jsonl": referenced})
    assert main(["verify", *args, "--pages-dir", str(control)]) == 0

    unnamed_rows: dict[str, dict[str, Any]] = {
        "no_sha_no_trial": {"pages": ["Text of a document no row names."]},
        "trial_without_document": {"nct_id": RT_SCOPE_ID, "pages": ["Text of an unnamed document."]},
    }
    for name, row in unnamed_rows.items():
        pages = _rt_m4_pages_dir(
            tmp_path / f"anon_{name}", {"maps.jsonl": referenced + json.dumps(row) + "\n"}
        )
        assert main(["verify", *args, "--pages-dir", str(pages)]) != 0, (
            f"{name}: a JSONL page row naming no in-scope document was parsed and dropped"
        )


def test_redteam_m4_cli_in_audit_spellings_refuse(tmp_path: Path) -> None:
    """verify and plan ignore a holdout flag spelled other than ``in_audit``.

    Rows (and the index's own in-scope rows) carrying ``inAudit: true`` or
    ``IN_AUDIT: true`` say the trial is held out; only the exact key is read,
    so they verify and are planned.
    """
    for key in ("inAudit", "IN_AUDIT"):
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0][key] = True
        loc_args = _rt_m4_inputs(tmp_path, f"spell_loc_{key}", locations=locs)
        assert main(["verify", *loc_args]) == 2, f"verify accepted a location row with {key}=true"
        assert main(["plan", *loc_args]) == 2, f"plan accepted a location row with {key}=true"

        doc_args = _rt_m4_inputs(
            tmp_path,
            f"spell_doc_{key}",
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, extra={key: True})],
        )
        assert main(["verify", *doc_args, "--allow-extra-document-keys"]) == 2, (
            f"verify accepted a document row with {key}=true"
        )

        index_rows = _rt_index_rows()
        index_rows[0][key] = True
        idx_args = _rt_m4_inputs(tmp_path, f"spell_idx_{key}", index_rows=index_rows)
        assert main(["verify", *idx_args]) == 2, f"verify admitted an index row with {key}=true"
        assert main(["plan", *idx_args]) == 2, f"plan admitted an index row with {key}=true"


def test_redteam_m4_cli_index_row_own_id_fields_naming_audit_trial_refused(tmp_path: Path) -> None:
    """An in-scope index row whose own-ID or alias field is the audit trial is admitted.

    ``_partition_index_firewall`` walks in-scope rows with the named-key walk
    only (``nct_id``/``nctId``/``trial_nct_id``, ``nct_ids``). The classic
    API's ``NCTId`` and API v2 ``nctIdAliases`` name the same trial;
    ``assemble_longitudinal_provenance`` refuses a study whose alias is an
    audit ID ("a holdout trial under another number"), the index does not, so
    the row's trial is verified and planned.
    """
    vectors: dict[str, Any] = {
        "NCTId": RT_AUDIT_ID,
        "nctIdAliases": [RT_AUDIT_ID],
        "protocolSection": {"identificationModule": {"nctIdAliases": [RT_AUDIT_ID]}},
    }
    for key, value in vectors.items():
        index_rows = _rt_index_rows()
        index_rows[0][key] = value
        args = _rt_m4_inputs(tmp_path, f"own_id_{key}", index_rows=index_rows)
        assert main(["verify", *args]) == 2, f"verify admitted an index row with {key} = audit"
        assert main(["plan", *args]) == 2, f"plan admitted an index row with {key} = audit"


def test_redteam_m4_plan_doc_cache_entry_for_another_trial_not_applied() -> None:
    """A doc-cache entry declaring another trial is applied to this trial's document.

    The entry's sha256 must be some in-scope document's, but the trial it
    declares is never compared with the trial whose document has that sha256
    (page maps raise on the same disagreement). An entry declaring trial 2 for
    trial 1's document moves trial 1 from OCR recovery to the terminal $0
    skip.
    """
    index_rows = _rt_index_rows() + [
        {
            "nct_id": RT_SCOPE_ID_2,
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["rt_cohort"],
            "primary_outcomes_count": 1,
        }
    ]
    documents = [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1), _doc(RT_SCOPE_ID_2, "Prot_000.pdf", SHA_2)]
    locations = _complete_primary_locations(
        RT_SCOPE_ID,
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin"},
        unreadable_note="Scanned image-only PDF with no extractable text.",
    )

    def _plan(cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return plan_incremental_cohort(
            index_rows,
            documents=documents,
            locations=locations,
            doc_cache_by_sha256=cache,
            planned_at="2026-11-23T00:00:00+00:00",
        )

    control = _plan({})
    assert control["partitions"]["unreadable_recoverable_ocr"] == [RT_SCOPE_ID]
    foreign = {
        SHA_1: {"sha256": SHA_1, "nct_id": RT_SCOPE_ID_2, "unreadable_recovery_bucket": "redacted_cci_ppd"}
    }
    try:
        plan = _plan(foreign)
    except CitationVerificationError:
        return
    assert plan["partitions"] == control["partitions"], (
        "a doc-cache entry declaring trial 2 reclassified trial 1's document: "
        f"{plan['partitions']}"
    )


def test_redteam_m4_cli_conflicting_doc_cache_rows_refused(tmp_path: Path) -> None:
    """Two --doc-cache rows for one sha256 that disagree are resolved silently (the later wins).

    The rows decide whether an unreadable trial is recovered by OCR or
    skipped terminally at $0; ``_load_doc_cache_by_sha256`` keeps the last
    row read, so the order of lines in the file decides.
    """
    locs = _complete_primary_locations(
        RT_SCOPE_ID,
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin"},
        unreadable_note="Scanned image-only PDF with no extractable text.",
    )
    args = _rt_m4_inputs(tmp_path, "dup_cache", locations=locs)
    single = tmp_path / "dup_cache_single.jsonl"
    _write_jsonl(single, [{"sha256": SHA_1, "unreadable_recovery_bucket": "image_only_scan"}])
    assert main(["plan", *args, "--doc-cache", str(single)]) == 0

    conflicting = tmp_path / "dup_cache_conflicting.jsonl"
    _write_jsonl(
        conflicting,
        [
            {"sha256": SHA_1, "unreadable_recovery_bucket": "image_only_scan"},
            {"sha256": SHA_1, "unreadable_recovery_bucket": "redacted_cci_ppd"},
        ],
    )
    assert main(["plan", *args, "--doc-cache", str(conflicting)]) != 0, (
        "plan resolved two conflicting doc-cache rows for one sha256 by line order"
    )


def test_redteam_m4_cli_not_found_on_unsearchable_document(tmp_path: Path) -> None:
    """verify passes NOT_FOUND for a trial whose only document failed to download.

    The document row is valid (``http_status`` 404, no sha256 or pages), so
    ``NOT_FOUND_WITHOUT_DOCUMENT`` does not fire, and the partition check
    skips a document without a page count: NOT_FOUND claims a search of pages
    that were never retrieved.
    """
    failed = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    failed.update({"http_status": 404, "sha256": None, "bytes": None, "physical_pages": None})
    not_found = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in not_found:
        row["status"] = "NOT_FOUND"
        row["citations"] = []
    args = _rt_m4_inputs(tmp_path, "nf_failed_doc", documents=[failed], locations=not_found)
    assert main(["verify", *args, "--check-completeness"]) == 1, (
        "NOT_FOUND verified for a trial whose only document was never retrieved"
    )


def test_redteam_m4_cli_refusal_messages_do_not_quote_audit_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusals print the audit trial's ID, in canonical form, to stderr.

    The text-token rule names a position only ("the text names an
    audit-holdout trial"), so that no error message quotes a holdout ID; the
    URL rule, ``enforce_audit_holdout``, the page-map path rule and the
    ``--nct-id`` check print it, upper-cased and percent-decoded from the
    input where the input hid it.
    """
    lower_url = f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID.lower()}/Prot_000.pdf"
    pct_id = f"{RT_AUDIT_ID[:3]}%{ord(RT_AUDIT_ID[3]):X}{RT_AUDIT_ID[4:]}"
    pct_url = f"https://cdn.clinicaltrials.gov/large-docs/88/{pct_id}/Prot_000.pdf"

    def _locs(update: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        rows[0].update(update)
        return rows

    clean_args = _rt_m4_inputs(tmp_path, "msg_clean")
    audit_dir_pages = _rt_m4_pages_dir(
        tmp_path / "msg_pages",
        {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}, f"{RT_AUDIT_ID}/Prot_000.json": {"pages": _RT_M4_PAGES}},
    )
    cases: dict[str, list[str]] = {
        "location_url_lowercase": [
            "verify", *_rt_m4_inputs(tmp_path, "msg_lower", locations=_locs({"source_url": lower_url}))
        ],
        "location_url_percent_encoded": [
            "verify", *_rt_m4_inputs(tmp_path, "msg_pct", locations=_locs({"source_url": pct_url}))
        ],
        "location_nct_id": [
            "verify",
            *_rt_m4_inputs(
                tmp_path,
                "msg_nct",
                locations=_complete_primary_locations(RT_AUDIT_ID, "Prot_000.pdf", SHA_1),
            ),
        ],
        "page_map_directory": ["verify", *clean_args, "--pages-dir", str(audit_dir_pages)],
        "plan_nct_id": ["plan", "--index", clean_args[1], "--nct-id", RT_AUDIT_ID],
    }
    for name, argv in cases.items():
        capsys.readouterr()
        rc = main(argv)
        captured = capsys.readouterr()
        assert rc == 2, f"{name}: expected a firewall refusal, got exit {rc}"
        assert RT_AUDIT_ID not in captured.err and RT_AUDIT_ID not in captured.out, (
            f"{name}: the refusal message quotes the audit trial's ID"
        )


def test_redteam_m4_cli_nct_id_obfuscations_refused(tmp_path: Path) -> None:
    """Attempt that held: a lowercase, padded, zero-width or non-ASCII --nct-id stops plan (exit 2)."""
    args = _rt_m4_inputs(tmp_path, "nct_id_forms")
    for bad in (
        RT_SCOPE_ID.lower(),
        f" {RT_SCOPE_ID}",
        f"{RT_SCOPE_ID} ",
        f"{RT_SCOPE_ID}​",
        "NCT" + "".join(chr(0x0660 + int(d)) for d in RT_SCOPE_ID[3:]),
    ):
        assert main(["plan", "--index", args[1], "--nct-id", bad]) == 2, repr(bad)


def test_redteam_m4_cli_pre_grouped_lines_and_non_list_citations_rejected(tmp_path: Path) -> None:
    """Attempt that held: pass-grouped location objects and non-list citations never verify or cache.

    A --locations line holding rows grouped by pass (``{"primary": [...],
    "bogus": [...]}``) is refused (no trial: exit 2) or fails verify (exit 1)
    when it carries one; the library entry points refuse a pass-keyed mapping
    in place of a row sequence. A ``citations`` value that is an object, a
    string, null or a number fails verify and keeps the trial out of
    ``cached_complete``.
    """
    grid = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    grouped: dict[str, Any] = {"primary": grid, "bogus": grid[:1]}
    for name, line in (("no_trial", grouped), ("with_trial", {"nct_id": RT_SCOPE_ID, **grouped})):
        args = _rt_m4_inputs(tmp_path, f"grouped_{name}")
        _write_jsonl(Path(args[5]), [line])
        expected = 2 if name == "no_trial" else 1
        assert main(["verify", *args, "--check-completeness"]) == expected, name
        assert main(["plan", *args]) == expected, name
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(
            _rt_index_rows(),
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
            locations=grouped,  # type: ignore[arg-type]
            planned_at="2026-11-23T00:00:00+00:00",
        )

    for idx, citations in enumerate(({"filename": "Prot_000.pdf"}, "x", None, 0)):
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0]["citations"] = citations
        args = _rt_m4_inputs(tmp_path, f"citations_type_{idx}", locations=locs)
        assert main(["verify", *args]) == 1, repr(citations)
        plan = plan_incremental_cohort(
            _rt_index_rows(),
            documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
            locations=locs,
            planned_at="2026-11-23T00:00:00+00:00",
        )
        assert plan["partitions"]["cached_complete"] == [], repr(citations)


# ---------------------------------------------------------------------------
# Regression tests for the round-3 fixes beyond the red-team cases
# (fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------


def test_m4_r3_index_row_classic_nct_id_must_be_its_trial(tmp_path: Path) -> None:
    """An in-scope index row's classic ``NCTId`` naming another trial is refused (exit 2)."""
    index_rows = _rt_index_rows()
    index_rows[0]["NCTId"] = RT_SCOPE_ID
    same = _rt_m4_inputs(tmp_path, "classic_same", index_rows=index_rows)
    # Round 5: verify without page text is not ok (exit 1), so the control gives its pages.
    pages = _rt_m4_pages_dir(tmp_path / "classic_pages", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    assert main(["verify", *same, "--pages-dir", str(pages)]) == 0
    index_rows[0]["NCTId"] = [RT_SCOPE_ID, RT_SCOPE_ID_2]
    other = _rt_m4_inputs(tmp_path, "classic_other", index_rows=index_rows)
    assert main(["verify", *other]) == 2
    assert main(["plan", *other]) == 2


def test_m4_r3_identical_doc_cache_rows_are_kept_once(tmp_path: Path) -> None:
    """Two identical --doc-cache rows for one sha256 are one row; only differing rows raise."""
    args = _rt_m4_inputs(tmp_path, "dup_cache_identical")
    cache = tmp_path / "dup_cache_identical.jsonl"
    row = {"sha256": SHA_1, "unreadable_recovery_bucket": "image_only_scan"}
    _write_jsonl(cache, [row, dict(row)])
    assert main(["plan", *args, "--doc-cache", str(cache)]) == 0


# ---------------------------------------------------------------------------
# Red team on m4 at f442dac (after the round-3 fixes; fictional
# NCT99800001..NCT99899999 only).
#
# Each test asserts the behaviour the module's own rules call for. Tests for a
# bypass fail until the module is fixed; tests for an attempt that held pass.
# ---------------------------------------------------------------------------

_RT_F442_FABRICATED = "This sentence appears on no page of the protocol."


def _rt_f442_index_rows() -> list[dict[str, Any]]:
    """``_rt_index_rows`` with a second in-scope trial, so a row may name another listed trial."""
    rows = _rt_index_rows()
    rows.insert(1, {**rows[0], "nct_id": RT_SCOPE_ID_2})
    return rows


def _rt_f442_run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the CLI and return its exit code and everything it printed."""
    capsys.readouterr()
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _rt_f442_fabricated_locations() -> list[dict[str, Any]]:
    """A complete primary grid whose every quotation appears on no page."""
    rows = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in rows:
        for cit in row["citations"]:
            cit["quotation"] = _RT_F442_FABRICATED
    return rows


def test_redteam_m4_cli_refusals_do_not_quote_an_audit_id_on_the_key_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """verify and plan print the audit trial's ID when it is a key above a field that fails.

    The prescan and the named-key walk run before the key scan that would
    refuse the audit-named key, and their messages print the key path:
    ``locations[0].NCT99888888.nct_ids must be a list``. The library planner
    also prints ``doc_cache_by_sha256`` and ``profiles_by_nct`` keys, and an
    ``nct_id`` given as a JSON number is printed by ``repr``. Each input must
    still be refused; no output may carry the audit trial's digits.
    """
    digits = RT_AUDIT_ID[3:]

    def _locs(extra: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        rows[0].update(extra)
        return rows

    cases = {
        "nct_ids_not_a_list": {RT_AUDIT_ID: {"nct_ids": "x"}},
        "nct_id_disagrees": {RT_AUDIT_ID: {"nct_id": RT_SCOPE_ID_2}},
        "url_names_other_trial": {RT_AUDIT_ID: {"source_url": f"https://example.org/{RT_SCOPE_ID_2}"}},
        "nct_id_as_a_number": {"related": {"nct_id": int(digits)}},
    }
    for name, extra in cases.items():
        args = _rt_m4_inputs(
            tmp_path, f"f442_keypath_{name}", locations=_locs(extra), index_rows=_rt_f442_index_rows()
        )
        for command in ("verify", "plan"):
            rc, printed = _rt_f442_run([command, *args], capsys)
            assert rc != 0, f"{command}/{name}: the row was accepted"
            assert digits not in printed, f"{command}/{name}: the output quotes the audit trial's ID"

    for name, kwargs in {
        "doc_cache_key": {"doc_cache_by_sha256": {RT_AUDIT_ID: "x"}},
        "profiles_key": {"profiles_by_nct": {RT_AUDIT_ID: "x"}},
        "profiles_nested": {"profiles_by_nct": {RT_AUDIT_ID: {"nct_ids": "x"}}},
    }.items():
        with pytest.raises(CitationVerificationError) as exc_info:
            plan_incremental_cohort(
                _rt_index_rows(),
                documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
                planned_at="2026-11-23T00:00:00+00:00",
                **kwargs,
            )
        assert digits not in str(exc_info.value), f"{name}: the refusal quotes the audit trial's ID"


def test_redteam_m4_cli_audit_id_split_between_key_and_value_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``{"NCT": "99888888"}`` in a --locations row or a --doc-cache row passes (exit 0).

    Neither the key nor the value is an NCT-like token on its own, and a
    JSON number is never read; the row and the plan are written with the
    holdout trial named in them.
    """
    digits = RT_AUDIT_ID[3:]
    for name, extra in {"string_value": {"NCT": digits}, "number_value": {"NCT": int(digits)}}.items():
        locs = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        locs[0].update(extra)
        args = _rt_m4_inputs(tmp_path, f"f442_split_{name}", locations=locs)
        for command in ("verify", "plan"):
            rc, _ = _rt_f442_run([command, *args], capsys)
            assert rc == 2, f"{command}/{name}: a row naming the audit trial as key + value gave exit {rc}"

        cache = tmp_path / f"f442_split_cache_{name}.jsonl"
        _write_jsonl(cache, [{"sha256": SHA_1, **extra}])
        clean = _rt_m4_inputs(tmp_path, f"f442_split_clean_{name}")
        rc, _ = _rt_f442_run(["plan", *clean, "--doc-cache", str(cache)], capsys)
        assert rc == 2, f"doc-cache/{name}: a cache row naming the audit trial as key + value gave exit {rc}"


def test_redteam_m4_cli_duplicate_json_keys_do_not_admit_a_holdout_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JSON object with a repeated key is read last-key-wins, and that admits the holdout trial.

    ``json.loads`` keeps the last of two equal keys. An index row reading
    ``"in_audit": true, ..., "in_audit": false`` is therefore in scope, and a
    --documents row for that trial verifies and is planned; a --documents row
    whose first ``nct_id`` is the audit trial and whose second is an in-scope
    trial is read as the in-scope trial's. The file says the trial is held
    out; nothing reports the conflict.
    """
    digits = RT_AUDIT_ID[3:]
    scope_row, audit_row = _rt_index_rows()
    audit_text = json.dumps(audit_row)
    assert audit_text.endswith("}")
    dup_audit_text = audit_text[:-1] + ', "in_audit": false}'
    index_text = '{"data": [' + json.dumps(scope_row) + ", " + dup_audit_text + "]}"

    args = _rt_m4_inputs(tmp_path, "f442_dup_in_audit")
    Path(args[1]).write_text(index_text, encoding="utf-8")
    _write_jsonl(
        Path(args[3]),
        [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1), _doc(RT_AUDIT_ID, "Prot_000.pdf", SHA_2)],
    )
    for command in ("verify", "plan"):
        rc, printed = _rt_f442_run([command, *args], capsys)
        assert rc == 2, f"{command}: an index row with in_audit true then false was admitted (exit {rc})"
        assert digits not in printed, f"{command}: the output names the holdout trial"

    doc_text = json.dumps(_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1))
    dup_doc_text = '{"nct_id": "' + RT_AUDIT_ID + '", ' + doc_text[1:]
    args = _rt_m4_inputs(tmp_path, "f442_dup_nct_id")
    Path(args[3]).write_text(dup_doc_text + "\n", encoding="utf-8")
    rc, _ = _rt_f442_run(["verify", *args], capsys)
    assert rc == 2, f"a document row naming the audit trial in a repeated nct_id key verified (exit {rc})"


def test_redteam_m4_cli_index_with_several_row_lists_is_not_read_in_part(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An index object holding both ``data`` and ``rows`` is read from ``data`` alone.

    ``load_index_rows`` takes the first list among ``data``, ``rows`` and
    ``trials`` without looking at the others, so the holdout row under
    ``rows`` is never read: the audit set is empty, ``allow_empty_audit_set``
    is derived True, and a document of the holdout trial is refused only as
    "unlisted", with its ID printed. The payload must be refused, or every
    list read.
    """
    digits = RT_AUDIT_ID[3:]
    payload = {"data": [_rt_index_rows()[0]], "rows": _rt_index_rows()}
    args = _rt_m4_inputs(tmp_path, "f442_data_and_rows")
    Path(args[1]).write_text(json.dumps(payload), encoding="utf-8")
    try:
        rows = load_index_rows(Path(args[1]))
    except CitationVerificationError:
        rows = None
    assert rows is None or any(row.get("in_audit") is True for row in rows), (
        "the holdout row under 'rows' was dropped and the index read as having no holdout"
    )

    _write_jsonl(
        Path(args[3]),
        [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1), _doc(RT_AUDIT_ID, "Prot_000.pdf", SHA_2)],
    )
    rc, printed = _rt_f442_run(["verify", *args], capsys)
    assert rc == 2
    assert digits not in printed, "the refusal names the holdout trial it was never told about"


def test_redteam_m4_cli_verify_without_page_text_is_not_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """verify exits 0 on quotations that appear on no page when --pages-dir is not given.

    Without a page provider no quotation is compared with any text and the
    report carries no issue saying so (``citations_checked`` 7, every match
    count 0, ``ok`` true). ``--ocr-dir`` given without ``--pages-dir`` is
    dropped without a word (``_cmd_verify`` builds a provider only for
    ``--pages-dir``), so an OCR map that does not hold the quotation fails
    nothing either.
    """
    args = _rt_m4_inputs(tmp_path, "f442_no_pages", locations=_rt_f442_fabricated_locations())
    report_path = tmp_path / "f442_no_pages_report.json"
    rc, _ = _rt_f442_run(["verify", *args, "--report-out", str(report_path)], capsys)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    matched = (
        payload["exact_ws_matches"]
        + payload["unicode_nfkc_hyphen_matches"]
        + payload["substring_mismatches"]
        + payload["quotations_not_compared"]
    )
    recorded = [issue for issue in payload["issues"] if issue["location_key"] is not None]
    assert rc != 0 or matched + len(recorded) >= payload["citations_checked"], (
        f"exit {rc}: {payload['citations_checked']} quotations were compared with no page text "
        f"and nothing in the report says so"
    )

    ocr = _rt_m4_pages_dir(tmp_path / "f442_ocr_only", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    rc, _ = _rt_f442_run(["verify", *args, "--ocr-dir", str(ocr)], capsys)
    assert rc != 0, "--ocr-dir without --pages-dir was ignored and fabricated quotations verified"


def test_redteam_m4_plan_cached_complete_needs_quotations_found_on_their_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """plan files rows whose quotations appear on no page as cached_complete (never re-run).

    ``_trial_rows_verify`` runs ``verify_location_record`` with no page
    provider ("planning reads no page text"), so the round-2 rule "cached_complete
    only for rows verify accepts" holds for structure and not for the
    quotations: ``verify --pages-dir`` rejects these rows (exit 1) while plan
    never schedules them again.
    """
    args = _rt_m4_inputs(tmp_path, "f442_fabricated", locations=_rt_f442_fabricated_locations())
    pages = _rt_m4_pages_dir(tmp_path / "f442_fabricated_pages", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    rc, _ = _rt_f442_run(["verify", *args, "--pages-dir", str(pages)], capsys)
    assert rc == 1, "control: verify with page text rejects the fabricated quotations"

    plan = plan_incremental_cohort(
        _rt_index_rows(),
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
        locations=_rt_f442_fabricated_locations(),
        planned_at="2026-11-23T00:00:00+00:00",
    )
    assert RT_SCOPE_ID not in plan["partitions"]["cached_complete"], (
        "rows whose quotations verify rejects were filed as cached_complete"
    )


def test_redteam_m4_cli_outputs_never_overwrite_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--report-out`` and ``--out-plan`` replace an input file named as the output (exit 0).

    ``_write_json`` writes wherever it is told. ``verify --report-out`` equal
    to ``--locations`` replaces the census rows (LLM output that costs money
    to produce) with the report; ``plan --out-plan`` equal to ``--index``
    replaces the holdout index with the plan. Neither is refused.
    """
    for command, flag, target_idx in (
        ("verify", "--report-out", 5),
        ("verify", "--report-out", 1),
        ("plan", "--out-plan", 1),
        ("plan", "--out-plan", 3),
    ):
        args = _rt_m4_inputs(tmp_path, f"f442_overwrite_{command}_{target_idx}")
        target = Path(args[target_idx])
        before = target.read_bytes()
        rc, _ = _rt_f442_run([command, *args, flag, str(target)], capsys)
        assert target.read_bytes() == before, (
            f"{command} {flag} {args[target_idx - 1]}: the input file was overwritten (exit {rc})"
        )


def test_redteam_m4_plan_document_without_sha256_is_not_fingerprinted_as_no_document() -> None:
    """A document row with no sha256 is left out of its trial's fingerprint without a word.

    ``compute_doc_sha256_tuple`` skips a document whose ``sha256`` is missing
    or null, so a trial whose only document has none is planned with the
    fingerprint of the empty document set: the shard manifest records "no
    documents" for a trial whose document is present but unidentified.
    """
    empty_fingerprint = compute_doc_set_fingerprint(())
    for name, mutate in {
        "missing": lambda d: d.pop("sha256"),
        "null": lambda d: d.update({"sha256": None}),
    }.items():
        doc = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
        mutate(doc)
        try:
            plan = plan_incremental_cohort(
                _rt_index_rows(), documents=[doc], planned_at="2026-11-23T00:00:00+00:00"
            )
        except CitationVerificationError:
            continue
        fingerprints = {
            nct: fp for shard in plan["shards"] for nct, fp in shard["doc_set_fingerprints"].items()
        }
        assert fingerprints.get(RT_SCOPE_ID) != empty_fingerprint, (
            f"{name} sha256: the trial was fingerprinted as having no document"
        )


def test_redteam_m4_plan_profile_documents_need_provenance_stamps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--profiles documents replace the trial's documents but need no provenance stamp.

    ``plan_incremental_cohort`` refuses a --documents row without
    ``model_id``/``profile_version`` (F6), and its docstring promises stamps
    "across all input streams"; a profile's ``documents`` list, which is what
    the trial is then planned and fingerprinted on, is never checked.
    """
    bare_doc = {"nct_id": RT_SCOPE_ID, "filename": "Prot_000.pdf", "sha256": SHA_1}
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(
            _rt_index_rows(), documents=[bare_doc], planned_at="2026-11-23T00:00:00+00:00"
        )
    with pytest.raises(CitationVerificationError):
        plan_incremental_cohort(
            _rt_index_rows(),
            profiles_by_nct={RT_SCOPE_ID: {"nct_id": RT_SCOPE_ID, "documents": [bare_doc]}},
            planned_at="2026-11-23T00:00:00+00:00",
        )
        pytest.fail("a profile's unstamped documents were planned")

    args = _rt_m4_inputs(tmp_path, "f442_profile_stamps")
    profiles = tmp_path / "f442_profiles.jsonl"
    _write_jsonl(profiles, [{"nct_id": RT_SCOPE_ID, "documents": [bare_doc]}])
    rc, _ = _rt_f442_run(["plan", "--index", args[1], "--profiles", str(profiles)], capsys)
    assert rc == 1, f"plan --profiles with unstamped documents gave exit {rc}"


def test_redteam_m4_plan_malformed_cohorts_field_is_refused() -> None:
    """An index row whose ``cohorts`` is not a list is dropped from a --cohort plan, uncounted.

    ``_trial_matches_cohort`` reads a non-list ``cohorts`` as no cohort, so
    the trial is in no partition and no exclusion count: the plan reports
    nothing scheduled and nothing excluded for an in-scope trial of that
    cohort.
    """
    for name, cohorts in {"string": "rt_cohort", "mapping": {"rt_cohort": True}}.items():
        index_rows = _rt_index_rows()
        index_rows[0]["cohorts"] = cohorts
        try:
            plan = plan_incremental_cohort(
                index_rows,
                documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
                cohort="rt_cohort",
                planned_at="2026-11-23T00:00:00+00:00",
            )
        except CitationVerificationError:
            continue
        accounted = [nct for ids in plan["partitions"].values() for nct in ids]
        assert RT_SCOPE_ID in accounted, f"{name} cohorts: the trial vanished from the plan"


def test_redteam_m4_plan_unstarted_second_pass_is_not_an_amendment() -> None:
    """With ``pass_role="second"`` a trial whose second pass never ran is filed as amended.

    The partition loop sends every trial with primary rows and no cache hit
    for the target pass to ``amended_doc_or_outcome_delta``; nothing was
    amended (same documents, same outcome count). ``--mode resume-unstarted``
    then schedules no second-pass run at all.
    """
    kwargs: dict[str, Any] = {
        "documents": [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
        "locations": _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1),
        "pass_role": "second",
        "planned_at": "2026-11-23T00:00:00+00:00",
    }
    plan = plan_incremental_cohort(_rt_index_rows(), mode="incremental-delta", **kwargs)
    assert RT_SCOPE_ID not in plan["partitions"]["amended_doc_or_outcome_delta"], (
        "an unstarted second pass was reported as a document or outcome amendment"
    )
    resume = plan_incremental_cohort(_rt_index_rows(), mode="resume-unstarted", **kwargs)
    scheduled = [nct for shard in resume["shards"] for nct in shard["nct_ids"]]
    assert RT_SCOPE_ID in scheduled, "resume-unstarted scheduled no unstarted second pass"


def test_redteam_m4_plan_stale_profile_does_not_cache_an_amended_document() -> None:
    """Attempt that held: a profile still naming the old sha256 does not make a trial cached_complete.

    --documents says Prot_000.pdf is now SHA_2 while the profile and the rows
    cite SHA_1; the cache lookup is built from --documents, so the trial is
    re-run.
    """
    plan = plan_incremental_cohort(
        _rt_index_rows(),
        documents=[_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_2)],
        locations=_complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1),
        profiles_by_nct={
            RT_SCOPE_ID: {"nct_id": RT_SCOPE_ID, "documents": [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)]}
        },
        planned_at="2026-11-23T00:00:00+00:00",
    )
    assert RT_SCOPE_ID not in plan["partitions"]["cached_complete"]


# ---------------------------------------------------------------------------
# Regression tests for the fixes of the f442 red team, beyond its cases
# (fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------


def test_m4_f442_plan_without_page_text_neither_caches_nor_schedules_complete_rows() -> None:
    """Complete, valid rows are complete_quotations_unchecked without page text; page text settles them."""
    kwargs: dict[str, Any] = {
        "documents": [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)],
        "locations": _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1),
        "planned_at": "2026-11-23T00:00:00+00:00",
    }
    unchecked = plan_incremental_cohort(_rt_index_rows(), **kwargs)
    assert unchecked["partitions"]["complete_quotations_unchecked"] == [RT_SCOPE_ID]
    assert unchecked["partitions"]["cached_complete"] == []
    assert unchecked["partition_counts"]["complete_quotations_unchecked"] == 1
    assert unchecked["scheduled_trial_count"] == 0

    provider = _quoted_pages_provider([SHA_1], audit_nct_ids=frozenset({RT_AUDIT_ID}))
    settled = plan_incremental_cohort(_rt_index_rows(), page_text_provider=provider, **kwargs)
    assert settled["partitions"]["cached_complete"] == [RT_SCOPE_ID]
    assert settled["partitions"]["complete_quotations_unchecked"] == []

    fabricated = plan_incremental_cohort(
        _rt_index_rows(),
        page_text_provider=provider,
        **{**kwargs, "locations": _rt_f442_fabricated_locations()},
    )
    assert fabricated["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID]
    assert fabricated["scheduled_trial_count"] == 1


def test_m4_f442_cli_plan_pages_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """plan --pages-dir files verified rows as cached_complete; --ocr-dir alone is refused."""
    args = _rt_m4_inputs(tmp_path, "f442_plan_pages")
    pages = _rt_m4_pages_dir(tmp_path / "f442_plan_pages_maps", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    plan_path = tmp_path / "f442_plan.json"
    rc, _ = _rt_f442_run(["plan", *args, "--out-plan", str(plan_path)], capsys)
    assert rc == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["partitions"]["complete_quotations_unchecked"] == [RT_SCOPE_ID]
    rc, _ = _rt_f442_run(
        ["plan", *args, "--pages-dir", str(pages), "--out-plan", str(plan_path)], capsys
    )
    assert rc == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["partitions"]["cached_complete"] == [RT_SCOPE_ID]
    rc, printed = _rt_f442_run(["plan", *args, "--ocr-dir", str(pages)], capsys)
    assert rc == 2 and "--ocr-dir needs --pages-dir" in printed


def test_m4_f442_cli_verify_reports_quotations_not_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """verify without --pages-dir counts the uncompared quotations, warns on stderr, and fails.

    Round 5: exit 1, not 0, since no quotation was compared with its page
    (``test_m4_r5_cli_verify_without_page_text_is_not_ok``).
    """
    args = _rt_m4_inputs(tmp_path, "f442_verify_counts")
    report_path = tmp_path / "f442_verify_counts_report.json"
    capsys.readouterr()
    assert main(["verify", *args, "--report-out", str(report_path)]) == 1
    assert "were not compared with page text" in capsys.readouterr().err
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["quotations_not_compared"] == report["citations_checked"] == len(ELEMENTS)


def test_m4_f442_document_fingerprint_records_unidentified_documents() -> None:
    """A document without a sha256 is recorded as unidentified; one without a filename raises."""
    from scripts.verify_and_plan_census_batch import DOC_SHA256_ABSENT

    doc = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    doc["sha256"] = None
    assert compute_doc_sha256_tuple([doc]) == (("Prot_000.pdf", DOC_SHA256_ABSENT),)
    assert compute_doc_set_fingerprint(compute_doc_sha256_tuple([doc])) != (
        compute_doc_set_fingerprint(())
    )
    doc.pop("filename")
    with pytest.raises(CitationVerificationError):
        compute_doc_sha256_tuple([doc])


def test_m4_f442_cli_index_row_list_key_must_hold_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An index whose single row-list key holds an object is refused (exit 2)."""
    args = _rt_m4_inputs(tmp_path, "f442_rows_object")
    Path(args[1]).write_text(json.dumps({"rows": {"0": _rt_index_rows()[0]}}), encoding="utf-8")
    rc, _ = _rt_f442_run(["verify", *args], capsys)
    assert rc == 2


# ---------------------------------------------------------------------------
# Round-5 fixes on m4 (fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------


def test_m4_r5_cli_verify_without_page_text_is_not_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """verify without --pages-dir compares no quotation with any page, so it does not pass.

    Before this fix the run exited 0 with ``ok`` true beside
    ``substring_mismatches`` 7 and ``quotations_not_compared`` 7, while the
    same rows with an empty --pages-dir exited 1: the run that knew less got
    the quieter result. Quotations nothing checked are not verified ones.
    """
    for name, locations in (
        ("r5_fabricated", _rt_f442_fabricated_locations()),
        ("r5_true_quotes", None),
    ):
        args = _rt_m4_inputs(tmp_path, name, locations=locations)
        report_path = tmp_path / f"{name}_report.json"
        rc, printed = _rt_f442_run(["verify", *args, "--report-out", str(report_path)], capsys)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["quotations_not_compared"] == report["citations_checked"] == len(ELEMENTS)
        # A quotation nothing compared has not been found to mismatch.
        assert report["substring_mismatches"] == 0, f"{name}: uncompared quotations counted as mismatches"
        assert report["ok"] is False, f"{name}: no quotation was compared, yet the report is ok"
        assert rc == 1, f"{name}: exit {rc} for a run that compared no quotation with its page"
        assert "were not compared with page text" in printed

    # Control: with page text, the true quotations verify (exit 0).
    args = _rt_m4_inputs(tmp_path, "r5_true_quotes")
    pages = _rt_m4_pages_dir(tmp_path / "r5_pages", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    rc, _ = _rt_f442_run(["verify", *args, "--pages-dir", str(pages)], capsys)
    assert rc == 0


def test_m4_r5_unreadable_trial_without_subtype_evidence_is_not_filed_redacted() -> None:
    """An UNREADABLE trial that nothing names a subtype for is unknown, not terminal redacted.

    ``classify_unreadable_subtype`` returned ``redacted_cci_ppd`` when no
    bucket, note keyword, redaction marker or blank page named a subtype, so a
    row verify accepts ("unreadable; OCR is needed") was filed under the
    ``$0`` terminal partition and never scheduled. With no evidence the
    subtype is recorded as unknown: neither terminal nor scheduled, and
    counted. A profile's ``readability_summary`` is evidence of the subtype of
    a trial with no rows.
    """
    from src.citation_verifier import verify_location_record

    assert (
        classify_unreadable_subtype(
            [{"filename": "Prot_000.pdf", "sha256": SHA_1}],
            [{"status": "UNREADABLE", "note": ""}],
            {},
        )
        is None
    )

    doc = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    locations = _complete_primary_locations(
        RT_SCOPE_ID,
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin", "multiplicity"},
        unreadable_note="The statistical section is unreadable; OCR is needed to read it.",
    )
    # Control: verify accepts the UNREADABLE rows and their note.
    for loc in locations:
        if loc["status"] == "UNREADABLE":
            _, issues = verify_location_record(
                loc,
                docs_by_nct={RT_SCOPE_ID: {"Prot_000.pdf": doc}},
                audit_nct_ids=frozenset({RT_AUDIT_ID}),
                in_scope_nct_ids=frozenset({RT_SCOPE_ID}),
            )
            assert [i.code for i in issues if i.severity == "error"] == []

    for mode in ("recover-unreadable", "incremental-delta"):
        plan = plan_incremental_cohort(
            _rt_index_rows(), documents=[doc], locations=locations, mode=mode
        )
        assert plan["partitions"]["unreadable_terminal_redacted"] == [], (
            f"{mode}: a trial with no redaction evidence was filed as terminal redacted"
        )
        assert plan["unreadable_subtypes"]["redacted_cci_ppd"] == []
        assert plan["partitions"].get("unreadable_subtype_unknown") == [RT_SCOPE_ID]
        assert plan["partition_counts"].get("unreadable_subtype_unknown") == 1
        assert plan["scheduled_trial_count"] == 0
        assert plan["ocr_prepass_queue"] == []

    # A trial with no rows whose profile says it is an image-only scan is recoverable.
    profiles = {
        RT_SCOPE_ID: {
            "nct_id": RT_SCOPE_ID,
            "in_audit": False,
            "readability_summary": "image_only_scan",
            "documents": [doc],
        }
    }
    plan = plan_incremental_cohort(
        _rt_index_rows(), profiles_by_nct=profiles, mode="recover-unreadable"
    )
    assert plan["partitions"]["unreadable_terminal_redacted"] == []
    assert plan["partitions"]["unreadable_recoverable_ocr"] == [RT_SCOPE_ID]
    assert plan["unreadable_subtypes"]["image_only_scan"] == [RT_SCOPE_ID]
    assert plan["scheduled_trial_count"] == 1


def test_m4_r5_cli_unreadable_input_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An input path that cannot be read exits 2 (the run could not start), never 1.

    Exit 1 means the inputs were read and failed verification or the budget;
    a mistyped path used to escape ``main`` as a traceback with Python's exit 1.
    """
    args = _rt_m4_inputs(tmp_path, "r5_io")
    missing = str(tmp_path / "r5_io" / "missing.jsonl")
    cases = {
        "verify, missing --index": ["verify", "--index", missing, *args[2:]],
        "verify, --index is a directory": ["verify", "--index", str(tmp_path), *args[2:]],
        "verify, missing --documents": ["verify", *args[:2], "--documents", missing, *args[4:]],
        "verify, missing --locations": ["verify", *args[:4], "--locations", missing],
        "plan, missing --index": ["plan", "--index", missing],
        "plan, missing --documents": ["plan", *args[:2], "--documents", missing],
    }
    for name, argv in cases.items():
        try:
            rc, printed = _rt_f442_run(argv, capsys)
        except OSError as exc:
            pytest.fail(f"{name}: {type(exc).__name__} escaped main instead of exit 2")
        assert rc == 2, f"{name}: exit {rc}"
        assert "ERROR [usage]" in printed, f"{name}: {printed!r}"


def test_m4_r5_runtime_dependency_lists_name_every_stdlib_import() -> None:
    """Each m4 module's stated standard-library list names every stdlib module it imports."""
    import ast
    import re
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    for rel in (
        "scripts/verify_and_plan_census_batch.py",
        "src/citation_verifier.py",
        "src/longitudinal_provenance.py",
    ):
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module)
        stdlib = {
            name
            for name in imported
            if name.split(".")[0] in sys.stdlib_module_names and name != "__future__"
        }
        docstring = ast.get_docstring(tree) or ""
        listed = re.search(r"standard library \(([^)]*)\)", docstring)
        assert listed is not None, f"{rel}: no standard-library list in the module docstring"
        named = set(re.findall(r"``([A-Za-z0-9_.]+)``", listed.group(1)))
        assert stdlib - named == set(), f"{rel}: imports {sorted(stdlib - named)} unlisted"


# ---------------------------------------------------------------------------
# Codex review of PR #113 at dab323c, threads r4663855870 and r4663855878
# (fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------


def _r6_profile(summary: str, *, physical_pages: Any = 10) -> dict[str, Any]:
    """A --profiles row for ``RT_SCOPE_ID`` naming ``summary``, with one stamped document."""
    doc = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    doc["physical_pages"] = physical_pages
    return {
        "nct_id": RT_SCOPE_ID,
        "in_audit": False,
        "readability_summary": summary,
        "documents": [doc],
    }


def test_m4_r6_profile_naming_redacted_cci_ppd_is_terminal_not_scheduled() -> None:
    """A trial with no rows whose profile says ``redacted_cci_ppd`` is the terminal $0 skip.

    ``plan_incremental_cohort`` read a profile's ``readability_summary`` only
    against ``image_only_scan``/``garbled_cmap``, so a profile naming the third
    subtype left a trial with no location rows readable: it reached
    ``unstarted_primary`` and ``resume-unstarted``/``incremental-delta``
    scheduled it (spending money) although redacted trials are never
    scheduled. A profile naming any of ``UNREADABLE_SUBTYPES`` now files the
    trial under that subtype; one naming none leaves the round-5 rule
    (UNREADABLE with no subtype evidence is ``unreadable_subtype_unknown``).
    """
    from scripts.verify_and_plan_census_batch import PLAN_MODES
    from src.citation_verifier import UNREADABLE_SUBTYPES

    assert set(UNREADABLE_SUBTYPES) == {"image_only_scan", "garbled_cmap", "redacted_cci_ppd"}

    redacted = {RT_SCOPE_ID: _r6_profile("redacted_cci_ppd")}
    for mode in PLAN_MODES:
        plan = plan_incremental_cohort(_rt_index_rows(), profiles_by_nct=redacted, mode=mode)
        assert plan["partitions"]["unreadable_terminal_redacted"] == [RT_SCOPE_ID], (
            f"{mode}: a profile naming redacted_cci_ppd was not filed terminal: "
            f"{plan['partition_counts']}"
        )
        assert plan["unreadable_subtypes"]["redacted_cci_ppd"] == [RT_SCOPE_ID]
        assert plan["partitions"]["unstarted_primary"] == []
        assert plan["partitions"]["unreadable_subtype_unknown"] == []
        assert plan["scheduled_trial_count"] == 0, f"{mode}: a redacted trial was scheduled"
        assert plan["ocr_prepass_queue"] == []
        assert plan["cost_projection"]["gating_estimated_usd"] == 0.0

    # image_only_scan and garbled_cmap behave as before: recoverable, scheduled for OCR.
    for summary in ("image_only_scan", "garbled_cmap"):
        plan = plan_incremental_cohort(
            _rt_index_rows(),
            profiles_by_nct={RT_SCOPE_ID: _r6_profile(summary)},
            mode="recover-unreadable",
        )
        assert plan["partitions"]["unreadable_recoverable_ocr"] == [RT_SCOPE_ID], summary
        assert plan["unreadable_subtypes"][summary] == [RT_SCOPE_ID], summary
        assert plan["partitions"]["unreadable_terminal_redacted"] == [], summary
        assert plan["scheduled_trial_count"] == 1, summary

    # Control: a profile naming no unreadable subtype leaves a trial with no rows unstarted.
    plan = plan_incremental_cohort(
        _rt_index_rows(),
        profiles_by_nct={RT_SCOPE_ID: _r6_profile("machine_readable")},
        mode="incremental-delta",
    )
    assert plan["partitions"]["unstarted_primary"] == [RT_SCOPE_ID]
    assert plan["scheduled_trial_count"] == 1

    # UNREADABLE rows with no subtype evidence: the profile names the subtype when it names
    # one of the three, and the round-5 rule holds when it names none.
    no_evidence_rows = _complete_primary_locations(
        RT_SCOPE_ID,
        "Prot_000.pdf",
        SHA_1,
        unreadable_elements={"margin"},
        unreadable_note="The statistical section is unreadable; OCR is needed to read it.",
    )
    for summary, partition in (
        ("machine_readable", "unreadable_subtype_unknown"),
        ("redacted_cci_ppd", "unreadable_terminal_redacted"),
        ("image_only_scan", "unreadable_recoverable_ocr"),
    ):
        plan = plan_incremental_cohort(
            _rt_index_rows(),
            locations=no_evidence_rows,
            profiles_by_nct={RT_SCOPE_ID: _r6_profile(summary)},
            mode="incremental-delta",
        )
        assert plan["partitions"][partition] == [RT_SCOPE_ID], (
            f"{summary}: {plan['partition_counts']}"
        )
        assert plan["scheduled_trial_count"] == 0, summary

    # A summary that is not a string is refused, never read as naming no subtype (a list
    # used to escape as an unhashable-type TypeError).
    for bad_summary in (["redacted_cci_ppd"], {"subtype": "redacted_cci_ppd"}, 3):
        with pytest.raises(CitationVerificationError, match="readability_summary"):
            plan_incremental_cohort(
                _rt_index_rows(),
                profiles_by_nct={
                    RT_SCOPE_ID: {
                        **_r6_profile("redacted_cci_ppd"),
                        "readability_summary": bad_summary,
                    }
                },
            )


def test_m4_r6_conflicting_profile_rows_for_one_trial_raise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two different --profiles rows for one trial raise in either order; a repeat loads once.

    ``_load_profiles_by_nct`` kept the last row read for a trial, so line order
    decided the trial's fingerprint, unreadable classification and scheduling.
    As for ``--doc-cache`` rows, rows that differ leave no way to tell which
    describes the trial and raise (exit 1, ``CitationVerificationError``); an
    identical repeat is one row. Rows compare as JSON, so ``10`` and ``10.0``
    (equal in Python, but only an ``int`` page count is read as one) differ.
    The refusal names only positions and the in-scope trial.
    """
    from scripts.verify_and_plan_census_batch import _load_profiles_by_nct

    audit = frozenset({RT_AUDIT_ID})
    scope = frozenset({RT_SCOPE_ID})
    scan = _r6_profile("image_only_scan")
    redacted = _r6_profile("redacted_cci_ppd")
    index_path = tmp_path / "r6_index.json"
    index_path.write_text(json.dumps({"data": _rt_index_rows()}), encoding="utf-8")

    conflicts = {
        "scan_then_redacted": [scan, redacted],
        "redacted_then_scan": [redacted, scan],
        "int_then_float_pages": [scan, _r6_profile("image_only_scan", physical_pages=10.0)],
        "float_then_int_pages": [_r6_profile("image_only_scan", physical_pages=10.0), scan],
    }
    for name, rows in conflicts.items():
        path = tmp_path / f"r6_profiles_{name}.jsonl"
        _write_jsonl(path, rows)
        with pytest.raises(CitationVerificationError) as excinfo:
            _load_profiles_by_nct(
                path,
                audit_nct_ids=audit,
                in_scope_nct_ids=scope,
                allow_empty_audit_set=False,
            )
        assert not isinstance(excinfo.value, AuditHoldoutViolationError), name
        rc, printed = _rt_f442_run(
            ["plan", "--index", str(index_path), "--profiles", str(path)], capsys
        )
        assert rc == 1, f"{name}: plan resolved two different profile rows by order (exit {rc})"
        assert "ERROR [verification]" in printed, f"{name}: {printed!r}"
        assert f"{path}[0]" in printed and f"{path}[1]" in printed, f"{name}: {printed!r}"
        assert RT_AUDIT_ID not in printed and RT_UNLISTED_ID not in printed, name

    # An identical repeat is kept once, and plans as the single row does.
    single = tmp_path / "r6_profiles_single.jsonl"
    repeated = tmp_path / "r6_profiles_repeated.jsonl"
    _write_jsonl(single, [scan])
    _write_jsonl(repeated, [scan, json.loads(json.dumps(scan))])
    loaded = _load_profiles_by_nct(
        repeated, audit_nct_ids=audit, in_scope_nct_ids=scope, allow_empty_audit_set=False
    )
    assert loaded == {RT_SCOPE_ID: scan}
    plans = []
    for path in (single, repeated):
        capsys.readouterr()
        rc = main(
            ["plan", "--index", str(index_path), "--profiles", str(path), "--mode",
             "recover-unreadable"]
        )
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        plan = json.loads(captured.out)
        plans.append((plan["partitions"], plan["shards"]))
    assert plans[0] == plans[1]
    assert plans[0][0]["unreadable_recoverable_ocr"] == [RT_SCOPE_ID]

    # Two rows for a trial outside the in-scope set are refused by the firewall before any
    # comparison, and the refusal does not quote the trial.
    unlisted = tmp_path / "r6_profiles_unlisted.jsonl"
    _write_jsonl(
        unlisted,
        [
            {**_r6_profile("image_only_scan"), "nct_id": RT_UNLISTED_ID, "documents": []},
            {**_r6_profile("redacted_cci_ppd"), "nct_id": RT_UNLISTED_ID, "documents": []},
        ],
    )
    rc, printed = _rt_f442_run(
        ["plan", "--index", str(index_path), "--profiles", str(unlisted)], capsys
    )
    assert rc == 2, printed
    assert "ERROR [firewall_or_scope]" in printed, printed
    assert RT_UNLISTED_ID not in printed, printed


# ---------------------------------------------------------------------------
# Round-7 regression (Codex r4680906537; fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------

_R7_LAST_PAGE = 40


def _r7_page_forty_locations() -> list[dict[str, Any]]:
    """A complete primary grid whose every citation names page 40 of Prot_000.pdf (SHA_1)."""
    rows = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in rows:
        for cit in row["citations"]:
            cit["physical_page"] = _R7_LAST_PAGE
            cit["printed_page"] = str(_R7_LAST_PAGE)
    return rows


def _r7_page_forty_provider() -> DictPageTextProvider:
    """Page text for SHA_1 that holds the cited quotation on page 40 of 40."""
    pages: list[str | None] = [f"Page {n}." for n in range(1, _R7_LAST_PAGE + 1)]
    pages[_R7_LAST_PAGE - 1] = "9 Statistical Methods\n" + _RT_M4_QUOTE
    return DictPageTextProvider(
        pages_by_sha256={SHA_1: pages}, audit_nct_ids=frozenset({RT_AUDIT_ID})
    )


def test_m4_r7_cached_complete_needs_current_documents_that_verify_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trial whose current document has ``http_status`` 200 and no page count is not cached_complete.

    The rows verdict ran every location row through ``verify_location_record``
    against the trial's current documents but never ran the documents through
    ``verify_document_record``. ``verify_citation`` range-checks a cited page
    only when the document's ``physical_pages`` is an int, so a 10-page
    document recorded with no page count let citations to page 40 through,
    and the trial was filed ``cached_complete`` (never re-run), although
    ``verify`` refuses that document (``INVALID_PHYSICAL_PAGES``). Correct:
    each current document, from ``--documents`` or from the trial's profile
    (whichever the trial is planned on), is run through
    ``verify_document_record`` first, and any error-severity issue makes the
    verdict ``rejected``, so the trial is re-run
    (``amended_doc_or_outcome_delta``). Document keys are held to
    ``verify --allow-extra-document-keys``: the optional keys are taken, any
    other key is refused. A profile document with no ``nct_id`` raises
    ``InvalidNctIdError``, as ``verify`` does for such a document.
    """
    provider = _r7_page_forty_provider()
    locations = _r7_page_forty_locations()
    no_count = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    del no_count["physical_pages"]
    null_count = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    null_count["physical_pages"] = None
    variants: dict[str, tuple[dict[str, Any], str]] = {
        # Controls: the same rows against a 40-page document verify; against the
        # document's true 10 pages page 40 is out of range.
        "forty_pages": (_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, physical_pages=40), "cached_complete"),
        "ten_pages": (_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1), "amended_doc_or_outcome_delta"),
        # The finding: no page count, so nothing range-checks page 40.
        "physical_pages_absent": (no_count, "amended_doc_or_outcome_delta"),
        "physical_pages_null": (null_count, "amended_doc_or_outcome_delta"),
        # Keys are held to verify --allow-extra-document-keys: the optional
        # readability keys the plan itself reads are taken; any other key is not.
        "optional_keys": (
            _doc(
                RT_SCOPE_ID,
                "Prot_000.pdf",
                SHA_1,
                physical_pages=40,
                extra={"readability_status": "machine_readable", "unreadable_recovery_bucket": None},
            ),
            "cached_complete",
        ),
        "unknown_key": (
            _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, physical_pages=40, extra={"reviewer_note": "ok"}),
            "amended_doc_or_outcome_delta",
        ),
    }
    for label, (doc, expected) in variants.items():
        # The trial is planned on --documents rows, or on its profile's documents
        # (which replace them); both feed the rows verdict.
        sources: dict[str, dict[str, Any]] = {
            "documents": {"documents": [doc]},
            "profile": {"profiles_by_nct": {RT_SCOPE_ID: {"nct_id": RT_SCOPE_ID, "documents": [doc]}}},
        }
        for source, stream in sources.items():
            plan = plan_incremental_cohort(
                _rt_index_rows(),
                locations=locations,
                page_text_provider=provider,
                planned_at="2026-11-24T00:00:00+00:00",
                **stream,
            )
            filed = [name for name, ids in plan["partitions"].items() if RT_SCOPE_ID in ids]
            assert filed == [expected], (label, source, filed)
        verify_document_issues = verify_document_record(
            doc,
            audit_nct_ids=frozenset({RT_AUDIT_ID}),
            in_scope_nct_ids=frozenset({RT_SCOPE_ID}),
            strict_keys=False,
        )
        assert any(issue.severity == "error" for issue in verify_document_issues) == (
            label.startswith("physical_pages_") or label == "unknown_key"
        ), (label, verify_document_issues)

    # A profile document with no nct_id is refused as verify refuses it, not judged.
    no_id = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1, physical_pages=40)
    del no_id["nct_id"]
    with pytest.raises(InvalidNctIdError):
        plan_incremental_cohort(
            _rt_index_rows(),
            locations=locations,
            profiles_by_nct={RT_SCOPE_ID: {"nct_id": RT_SCOPE_ID, "documents": [no_id]}},
            page_text_provider=provider,
            planned_at="2026-11-24T00:00:00+00:00",
        )

    # The CLI: plan --documents --pages-dir with the document recorded without a page count.
    args = _rt_m4_inputs(tmp_path, "r7_no_page_count", documents=[no_count], locations=locations)
    maps = _rt_m4_pages_dir(
        tmp_path / "r7_no_page_count_maps",
        {f"{SHA_1}.json": {"pages": [f"Page {n}." for n in range(1, _R7_LAST_PAGE)] + [_RT_M4_QUOTE]}},
    )
    plan_path = tmp_path / "r7_plan.json"
    rc, printed = _rt_f442_run(["plan", *args, "--pages-dir", str(maps), "--out-plan", str(plan_path)], capsys)
    assert rc == 0, printed
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["partitions"]["cached_complete"] == [], plan["partitions"]
    assert plan["partitions"]["amended_doc_or_outcome_delta"] == [RT_SCOPE_ID], plan["partitions"]
    rc, printed = _rt_f442_run(["verify", *args, "--pages-dir", str(maps)], capsys)
    assert rc == 1 and "INVALID_PHYSICAL_PAGES" in printed, printed


# ---------------------------------------------------------------------------
# Investigator-approved design decisions D1-D4 on PR #113 (fictional
# NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------

_D_M1_RULES_VERSION = "doc_indexer@2026-11-21.v1"


def _m1_profile_document(
    nct_id: str, filename: str, sha256: str, *, physical_pages: int = 10
) -> dict[str, Any]:
    """An indexed document exactly as m1's ``build_trial_document_record`` writes it.

    A copy of the shape (the 24 keys, their types and nesting) that
    src/document_indexer.py on main returns for a machine-readable document;
    the m4 files may not import m1 (``test_m4_static_isolation_...``). It
    carries no ``model_id`` or ``profile_version``: no model produced it.
    """
    day = {"raw": None, "iso_normalized": None, "date_precision": "unknown", "earliest_iso": None, "latest_iso": None}
    unknown_delta = {
        "exact_days": None,
        "min_days": None,
        "max_days": None,
        "uploaded_precision": "unknown",
        "anchor_precision": "unknown",
        "date_precision": "unknown",
        "relation": "unknown_missing_date",
    }
    return {
        "nct_id": nct_id,
        "filename": filename,
        "doc_type": "protocol",
        "url": f"https://cdn.clinicaltrials.gov/large-docs/{nct_id[-2:]}/{nct_id}/{filename}",
        "http_status": 200,
        "download_error": None,
        "extraction_error": None,
        "sha256": sha256,
        "bytes": 4096,
        "physical_pages": physical_pages,
        "pages_without_text": [],
        "readability_status": "machine_readable",
        "unreadable_recovery_bucket": None,
        "redaction_markers": [],
        "version_label": "Version 2.0",
        "version_date_as_printed": "15 January 2020",
        "api_doc_date": None,
        "upload_date": None,
        "upload_timing": {
            "uploaded_date": dict(day),
            "primary_completion_date": dict(day),
            "results_first_post_date": dict(day),
            "vs_primary_completion": dict(unknown_delta),
            "vs_results_first_post": dict(unknown_delta),
        },
        "pages_selected": list(range(1, physical_pages + 1)),
        "pages_deferred": [],
        "unreadable_statistical_pages": [],
        "retrieved_at": "2026-11-21T00:00:00Z",
        "cache_hit": False,
    }


def _m1_profile(
    nct_id: str,
    documents: list[dict[str, Any]],
    *,
    rules_version: str = _D_M1_RULES_VERSION,
) -> dict[str, Any]:
    """A --profiles row exactly as m1's ``build_trial_document_profile`` writes it (9 keys)."""
    return {
        "nct_id": nct_id,
        "indexer_rules_version": rules_version,
        "extracted_at": "2026-11-21T00:00:00Z",
        "pipeline_commit": "a" * 40,
        "readability_summary": "machine_readable",
        "redaction_flags": [],
        "excluded_icf_count": 0,
        "document_badge": {
            "readability_status": "machine_readable",
            "redaction_flags": [],
            "doc_count": len(documents),
            "total_physical_pages": sum(doc["physical_pages"] for doc in documents),
        },
        "documents": documents,
    }


def _d_index_path(root: Path, name: str, rows: list[dict[str, Any]] | None = None) -> Path:
    """Write an index (``_rt_index_rows`` unless ``rows``) and return its path."""
    path = root / name / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": _rt_index_rows() if rows is None else rows}), encoding="utf-8")
    return path


def test_m4_d1_plan_accepts_the_deterministic_profiles_m1_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D1: plan takes m1's --profiles, whose documents no model produced and carry no F6 stamps.

    Before: ``_validate_profile_entry`` required ``model_id`` and
    ``profile_version`` of every profile document, so ``plan --profiles`` on
    the profiles the document indexer (m1) writes exited 1, and the rows
    verdict (``verify_document_record``) would have rejected them too. Now a
    document of a profile row exactly as m1 writes it (its key set, and its
    ``indexer_rules_version`` matched whole) needs neither stamp, so a trial
    planned on it can be ``cached_complete``. Still held to F6 in full: the
    same document carrying ``model_id`` (it claims a model's output), a
    profile row m1 would not write, the document as a ``--documents`` row, and
    every row ``verify`` reads. The holdout firewall on profiles is unchanged.
    """
    doc = _m1_profile_document(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    profile = _m1_profile(RT_SCOPE_ID, [doc])
    locations = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    provider = _quoted_pages_provider([SHA_1], audit_nct_ids=frozenset({RT_AUDIT_ID}))

    # CLI: plan --profiles <m1 profiles> with page text plans the trial, cached_complete.
    index_path = _d_index_path(tmp_path, "d1")
    profiles_path = tmp_path / "d1" / "trial_document_profiles.jsonl"
    locations_path = tmp_path / "d1" / "locations.jsonl"
    _write_jsonl(profiles_path, [profile])
    _write_jsonl(locations_path, locations)
    pages = _rt_m4_pages_dir(tmp_path / "d1_maps", {f"{SHA_1}.json": {"pages": _RT_M4_PAGES}})
    plan_path = tmp_path / "d1_plan.json"
    plan_args = ["plan", "--index", str(index_path), "--locations", str(locations_path)]
    rc, printed = _rt_f442_run(
        [*plan_args, "--profiles", str(profiles_path), "--pages-dir", str(pages), "--out-plan", str(plan_path)],
        capsys,
    )
    assert rc == 0, printed
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["planner_rules_version"] == "1.9.0"
    assert plan["partitions"]["cached_complete"] == [RT_SCOPE_ID], plan["partitions"]

    # The library entry point, the same.
    plan = plan_incremental_cohort(
        _rt_index_rows(),
        locations=locations,
        profiles_by_nct={RT_SCOPE_ID: profile},
        page_text_provider=provider,
        planned_at="2026-11-24T00:00:00+00:00",
    )
    assert plan["partitions"]["cached_complete"] == [RT_SCOPE_ID], plan["partitions"]

    # The same document claiming a model's output (model_id, no profile_version): F6 in full.
    claimed = {**doc, "model_id": "claude-sonnet-4-6-20260601"}
    with pytest.raises(CitationVerificationError, match="profile_version"):
        plan_incremental_cohort(
            _rt_index_rows(),
            profiles_by_nct={RT_SCOPE_ID: _m1_profile(RT_SCOPE_ID, [claimed])},
            planned_at="2026-11-24T00:00:00+00:00",
        )
    _write_jsonl(profiles_path, [_m1_profile(RT_SCOPE_ID, [claimed])])
    rc, printed = _rt_f442_run([*plan_args, "--profiles", str(profiles_path)], capsys)
    assert rc == 1 and "profile_version" in printed, printed

    # Profile rows m1 does not write are not exempt: a rules version that is not m1's
    # (matched whole, so neither a prefix nor a longer name passes), an extra key, a
    # missing or empty run stamp.
    not_m1_rows = {
        "rules_version_unversioned": {**profile, "indexer_rules_version": "doc_indexer@latest"},
        "rules_version_longer_name": {**profile, "indexer_rules_version": "x" + _D_M1_RULES_VERSION},
        "rules_version_trailing": {**profile, "indexer_rules_version": _D_M1_RULES_VERSION + "-rc"},
        "extra_key": {**profile, "in_audit": False},
        "empty_extracted_at": {**profile, "extracted_at": " "},
        "no_pipeline_commit_key": {k: v for k, v in profile.items() if k != "pipeline_commit"},
    }
    for label, row in not_m1_rows.items():
        with pytest.raises(CitationVerificationError, match="model_id"):
            plan_incremental_cohort(
                _rt_index_rows(),
                profiles_by_nct={RT_SCOPE_ID: row},
                planned_at="2026-11-24T00:00:00+00:00",
            )
            pytest.fail(f"{label}: a profile m1 does not write was exempted from F6")
    # A document with a key m1 does not write is not exempt either.
    with pytest.raises(CitationVerificationError, match="model_id"):
        plan_incremental_cohort(
            _rt_index_rows(),
            profiles_by_nct={RT_SCOPE_ID: _m1_profile(RT_SCOPE_ID, [{**doc, "note": "edited"}])},
            planned_at="2026-11-24T00:00:00+00:00",
        )

    # The exemption is plan --profiles only: the same document as a --documents row is F6-refused.
    with pytest.raises(CitationVerificationError, match="model_id"):
        plan_incremental_cohort(
            _rt_index_rows(), documents=[doc], planned_at="2026-11-24T00:00:00+00:00"
        )
    documents_path = tmp_path / "d1" / "documents.jsonl"
    _write_jsonl(documents_path, [doc])
    rc, printed = _rt_f442_run([*plan_args, "--documents", str(documents_path)], capsys)
    assert rc == 1 and "model_id" in printed, printed

    # verify keeps F6: a census document row without model_id, and a location row without
    # it, are refused (exit 1) with their missing-key errors, page text or not.
    unstamped_doc = _doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    del unstamped_doc["model_id"]
    unstamped_locations = _complete_primary_locations(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)
    for row in unstamped_locations:
        del row["model_id"]
    for label, documents, rows, expected_code in (
        ("census_document", [unstamped_doc], locations, "DOCUMENT_MISSING_KEYS"),
        ("m1_document_as_census_row", [doc], locations, "DOCUMENT_MISSING_KEYS"),
        ("census_location", [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1)], unstamped_locations, "LOCATION_MISSING_KEYS"),
    ):
        args = _rt_m4_inputs(tmp_path, f"d1_verify_{label}", documents=documents, locations=rows)
        report_path = tmp_path / f"d1_verify_{label}.json"
        rc, printed = _rt_f442_run(
            ["verify", *args, "--allow-extra-document-keys", "--pages-dir", str(pages), "--report-out", str(report_path)],
            capsys,
        )
        assert rc == 1, (label, printed)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        missing = [i for i in report["issues"] if i["code"] == expected_code]
        assert missing and all("model_id" in i["message"] for i in missing), (label, report["issues"])

    # The holdout firewall on profiles is unchanged: an m1 profile of the audit trial is refused.
    held_out = _m1_profile(RT_AUDIT_ID, [_m1_profile_document(RT_AUDIT_ID, "Prot_000.pdf", SHA_2)])
    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            _rt_index_rows(),
            profiles_by_nct={RT_AUDIT_ID: held_out},
            planned_at="2026-11-24T00:00:00+00:00",
        )
    _write_jsonl(profiles_path, [profile, held_out])
    rc, printed = _rt_f442_run([*plan_args, "--profiles", str(profiles_path)], capsys)
    assert rc == 2 and RT_AUDIT_ID not in printed, printed


def _m1_index_line(
    sha256: str,
    rules_version: str,
    *,
    readability_status: str = "machine_readable",
    bucket: str | None = None,
    physical_pages: int = 10,
) -> dict[str, Any]:
    """A document_index_by_sha256.jsonl line exactly as m1's ``DocumentIndexEntry.to_dict`` writes it.

    A copy of the shape (keys and nesting) checked against src/document_indexer.py
    on main; m1 keeps one such line per (sha256, indexer_rules_version).
    """
    pages = list(range(1, physical_pages + 1))
    return {
        "sha256": sha256,
        "byte_count": 4096,
        "extracted_at": "2026-11-21T00:00:00Z",
        "pipeline_commit": "a" * 40,
        "indexer_rules_version": rules_version,
        "text_provider": "pdfkit",
        "physical_pages": physical_pages,
        "pages_without_text": [],
        "readability_status": readability_status,
        "unreadable_recovery_bucket": bucket,
        "cmap_metrics": {
            "text_bearing_pages": physical_pages,
            "garbled_page_count": 0,
            "garbled_page_ratio": 0.0,
            "pages_garbled_cmap": [],
            "garbled_statistical_pages": [],
            "max_page_punct_ctrl_ratio": 0.1,
            "min_page_common_word_ratio": 0.7,
        },
        "redaction": {
            "redaction_flags": [],
            "pages_with_cci": [],
            "pages_with_ppd": [],
            "pages_with_redacted_tag": [],
            "pages_header_stamp_only": [],
            "pages_orphaned_stat_headings": [],
            "unreadable_statistical_pages": [],
        },
        "version_label": "Version 2.0",
        "version_date_as_printed": "15 January 2020",
        "section_slice": {
            "physical_pages": physical_pages,
            "pages_selected": pages,
            "pages_deferred": [],
            "pages_without_text": [],
            "unreadable_statistical_pages": [],
            "has_toc": False,
            "toc_pages": [],
            "printed_page_offset": None,
            "detected_sections": [
                {
                    "category": "statistical_methods",
                    "heading": "9 Statistical Methods",
                    "source": "body_heading",
                    "start_page": 2,
                    "end_page": 2,
                }
            ],
            "selection_strategy": "full_readable_within_budget",
        },
        "selection_inputs": {"doc_type": "protocol", "min_selected_pages": 10, "max_selected_pages": 25},
    }


def test_m4_d2_doc_cache_read_under_one_indexer_rules_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D2: plan reads --doc-cache lines of one indexer rules version, named or the file's only one.

    Before: m1 keeps one line per (sha256, indexer rules version), so after any
    m1 rules bump the file holds two lines for one sha256 and plan refused it
    ("... are different doc-cache rows for sha256 ..."). Now
    ``--doc-cache-rules-version`` (library ``doc_cache_rules_version``) names
    the version read; lines of other versions are left out and counted.
    Without it, a file of several versions is refused (exit 1), naming how
    many and the option; a file of one version plans as before. Within the
    version read, two different lines for one sha256 are still refused. A
    version with no line for a document's sha256 plans as a cache with no line
    for it. The plan records the version it was made against.
    """
    v1, v2, v3 = (
        "doc_indexer@2026-11-21.v1",
        "doc_indexer@2026-12-01.v2",
        "doc_indexer@2026-12-15.v3",
    )
    documents = [_doc(RT_SCOPE_ID, "Prot_000.pdf", SHA_1), _doc(RT_SCOPE_ID, "SAP_000.pdf", SHA_2)]
    # Every cell UNREADABLE, with nothing in the rows or documents naming the subtype: the
    # document cache decides it.
    locations = _complete_primary_locations(
        RT_SCOPE_ID, "Prot_000.pdf", SHA_1, unreadable_elements=set(ELEMENTS), unreadable_note="Unreadable."
    )
    lines = [
        _m1_index_line(SHA_1, v1, readability_status="image_only_scan", bucket="image_only_scan"),
        _m1_index_line(SHA_2, v1),
        _m1_index_line(SHA_1, v2, readability_status="partial_image_or_redacted", bucket="redacted_cci_ppd"),
        _m1_index_line(SHA_2, v2),
        _m1_index_line(SHA_2, v3),
    ]
    args = _rt_m4_inputs(tmp_path, "d2", documents=documents, locations=locations)
    cache_path = tmp_path / "d2" / "document_index_by_sha256.jsonl"
    _write_jsonl(cache_path, lines)
    plan_path = tmp_path / "d2_plan.json"

    def run(*extra: str) -> tuple[int, str, dict[str, Any] | None]:
        if plan_path.exists():
            plan_path.unlink()
        rc, printed = _rt_f442_run(["plan", *args, "--out-plan", str(plan_path), *extra], capsys)
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else None
        return rc, printed, plan

    def filed(plan: dict[str, Any]) -> list[str]:
        return [name for name, ids in plan["partitions"].items() if RT_SCOPE_ID in ids]

    # Several versions, none named: refused, naming how many and the option; nothing written.
    rc, printed, plan = run("--doc-cache", str(cache_path))
    assert rc == 1 and plan is None, printed
    assert "3 indexer rules versions" in printed and "--doc-cache-rules-version" in printed, printed

    # Each version named: planned on that version's lines, the others left out and counted.
    rc, printed, plan = run("--doc-cache", str(cache_path), "--doc-cache-rules-version", v1)
    assert rc == 0 and plan is not None, printed
    assert filed(plan) == ["unreadable_recoverable_ocr"]
    assert plan["unreadable_subtypes"]["image_only_scan"] == [RT_SCOPE_ID]
    assert plan["doc_cache_rules_version"] == v1
    assert plan["doc_cache_lines_ignored_other_rules_versions"] == 3
    assert "3 of 5 --doc-cache line(s)" in printed, printed
    rc, printed, plan = run("--doc-cache", str(cache_path), "--doc-cache-rules-version", v2)
    assert rc == 0 and plan is not None, printed
    assert filed(plan) == ["unreadable_terminal_redacted"]
    assert plan["doc_cache_rules_version"] == v2

    # v3 has no line for SHA_1: planned as a cache with no line for it (here, no line that
    # names the subtype), exactly as without the cache.
    rc, printed, v3_plan = run("--doc-cache", str(cache_path), "--doc-cache-rules-version", v3)
    assert rc == 0 and v3_plan is not None, printed
    assert v3_plan["doc_cache_rules_version"] == v3
    rc, printed, no_cache_plan = run()
    assert rc == 0 and no_cache_plan is not None, printed
    assert no_cache_plan["doc_cache_rules_version"] is None
    for key in ("partitions", "unreadable_subtypes", "ocr_prepass_queue", "shards"):
        assert v3_plan[key] == no_cache_plan[key], key
    assert filed(v3_plan) == ["unreadable_subtype_unknown"]

    # A file of one version plans as before, without the option, and records the version.
    one_version = tmp_path / "d2" / "one_version.jsonl"
    _write_jsonl(one_version, lines[:2])
    rc, printed, plan = run("--doc-cache", str(one_version))
    assert rc == 0 and plan is not None, printed
    assert filed(plan) == ["unreadable_recoverable_ocr"]
    assert plan["doc_cache_rules_version"] == v1
    assert plan["doc_cache_lines_ignored_other_rules_versions"] == 0

    # Within the version read, two different lines for one sha256 are still refused.
    conflicting = tmp_path / "d2" / "conflicting.jsonl"
    _write_jsonl(conflicting, [*lines, _m1_index_line(SHA_1, v1, readability_status="garbled_cmap", bucket="garbled_cmap")])
    rc, printed, plan = run("--doc-cache", str(conflicting), "--doc-cache-rules-version", v1)
    assert rc == 1 and plan is None and "different doc-cache rows for sha256" in printed, printed

    # A rules version that is not a non-empty string says nothing about which rules ran.
    malformed = tmp_path / "d2" / "malformed.jsonl"
    _write_jsonl(malformed, [{**lines[0], "indexer_rules_version": 5}])
    rc, printed, plan = run("--doc-cache", str(malformed))
    assert rc == 1 and plan is None and "indexer_rules_version of type int" in printed, printed
    # The option without --doc-cache, or empty, is a usage error before anything is read.
    rc, printed, plan = run("--doc-cache-rules-version", v1)
    assert rc == 2 and plan is None, printed
    rc, printed, plan = run("--doc-cache", str(cache_path), "--doc-cache-rules-version", " ")
    assert rc == 2 and plan is None, printed

    # The library entry point applies the same rule to the map it is given.
    cache = {SHA_1: lines[0], SHA_2: lines[3]}
    with pytest.raises(CitationVerificationError, match="2 indexer rules versions"):
        plan_incremental_cohort(
            _rt_index_rows(),
            documents=documents,
            locations=locations,
            doc_cache_by_sha256=cache,
            planned_at="2026-11-24T00:00:00+00:00",
        )
    plan = plan_incremental_cohort(
        _rt_index_rows(),
        documents=documents,
        locations=locations,
        doc_cache_by_sha256=cache,
        doc_cache_rules_version=v1,
        planned_at="2026-11-24T00:00:00+00:00",
    )
    assert filed(plan) == ["unreadable_recoverable_ocr"]
    assert plan["doc_cache_rules_version"] == v1
    assert plan["doc_cache_lines_ignored_other_rules_versions"] == 1
