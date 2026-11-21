"""Offline unit and CLI tests for scripts/verify_and_plan_census_batch.py."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_and_plan_census_batch import (
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
    ELEMENTS,
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
            }
        )
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_incremental_sha256_skip_amendment_and_unreadable_partition() -> None:
    index_rows = [
        {
            "nct_id": "NCT00000001",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000002",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000003",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000004",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000005",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000006",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
        {
            "nct_id": "NCT00000099",
            "in_audit": True,
            "protocol": True,
            "sap": True,
            "cohorts": ["industry_phase3"],
            "primary_outcomes_count": 1,
        },
    ]

    documents = [
        _doc("NCT00000001", "Prot_000.pdf", SHA_1),
        _doc("NCT00000002", "Prot_000.pdf", SHA_2),
        # NCT00000003 is unstarted (no locations yet)
        _doc("NCT00000004", "Prot_000.pdf", SHA_3, pages_without_text=list(range(1, 11))),
        _doc("NCT00000005", "Prot_000.pdf", SHA_4),
        _doc("NCT00000006", "Prot_000.pdf", SHA_5),
    ]
    locations = (
        _complete_primary_locations("NCT00000001", "Prot_000.pdf", SHA_1)
        + _complete_primary_locations("NCT00000002", "Prot_000.pdf", SHA_2)
        + _complete_primary_locations(
            "NCT00000004",
            "Prot_000.pdf",
            SHA_3,
            unreadable_elements={"alpha_and_sidedness", "margin"},
            unreadable_note="Pure scanned image-only PDF.",
        )
        + _complete_primary_locations(
            "NCT00000005",
            "Prot_000.pdf",
            SHA_4,
            unreadable_elements={"multiplicity"},
            unreadable_note="Garbled ToUnicode CMap ciphered text.",
        )
        + _complete_primary_locations(
            "NCT00000006",
            "Prot_000.pdf",
            SHA_5,
            unreadable_elements={"analysis_population"},
            unreadable_note="Sponsor redacted statistical pages with CCI / PPD white-box markers.",
        )
    )

    # Simulate an amended PDF sha256 for NCT00000002 via Module 1 profiles.
    profiles_by_nct = {
        "NCT00000002": {
            "nct_id": "NCT00000002",
            "readability_summary": "machine_readable",
            "documents": [_doc("NCT00000002", "Prot_000.pdf", SHA_AMENDED)],
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
    )
    assert plan_delta["excluded_in_audit_count"] == 1
    assert plan_delta["partitions"]["cached_complete"] == ["NCT00000001"]
    assert plan_delta["partitions"]["amended_doc_or_outcome_delta"] == ["NCT00000002"]
    assert plan_delta["partitions"]["unstarted_primary"] == ["NCT00000003"]
    assert plan_delta["partitions"]["unreadable_recoverable_ocr"] == [
        "NCT00000004",
        "NCT00000005",
    ]
    assert plan_delta["partitions"]["unreadable_terminal_redacted"] == ["NCT00000006"]
    assert plan_delta["unreadable_terminal_redacted_spend_usd"] == 0.0
    assert plan_delta["unreadable_subtypes"] == {
        "image_only_scan": ["NCT00000004"],
        "garbled_cmap": ["NCT00000005"],
        "redacted_cci_ppd": ["NCT00000006"],
    }
    assert plan_delta["launch_approved"] is True
    assert plan_delta["shards"][0]["nct_ids"] == ["NCT00000002", "NCT00000003"]
    assert "NCT00000099" not in str(plan_delta["shards"])

    # Recover-unreadable mode schedules ONLY image_only_scan + garbled_cmap (NOT redacted_cci_ppd),
    # scopes target_elements_by_nct strictly to UNREADABLE cells, and keeps complete primary
    # trials with unchanged sha256 in cached_complete rather than amended_doc_or_outcome_delta.
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
    )
    assert plan_ocr["pass_role"] == "ocr_recovery"
    assert plan_ocr["partitions"]["cached_complete"] == ["NCT00000001"]
    assert plan_ocr["partitions"]["amended_doc_or_outcome_delta"] == ["NCT00000002"]
    assert plan_ocr["scheduled_trial_count"] == 2
    assert plan_ocr["shards"][0]["nct_ids"] == ["NCT00000004", "NCT00000005"]
    assert plan_ocr["shards"][0]["target_elements_by_nct"]["NCT00000004"] == [
        {"primary_outcome_index": 0, "element": "alpha_and_sidedness"},
        {"primary_outcome_index": 0, "element": "margin"},
    ]
    assert plan_ocr["shards"][0]["target_elements_by_nct"]["NCT00000005"] == [
        {"primary_outcome_index": 0, "element": "multiplicity"},
    ]
    assert "NCT00000006" not in plan_ocr["shards"][0]["nct_ids"]


def test_budget_cap_guard_and_max_launchable_shards() -> None:
    index_rows = [
        {
            "nct_id": f"NCT000100{i:02d}",
            "in_audit": False,
            "protocol": True,
            "sap": False,
            "cohorts": ["industry_phase3"],
        }
        for i in range(1, 55)  # 54 unstarted trials -> 3 shards of 18
    ]
    plan = plan_incremental_cohort(
        index_rows,
        mode="resume-unstarted",
        shard_size=18,
        budget_cap_usd=3.50,
        cost_per_trial_sliced_usd=0.10,  # 18 trials/shard = $1.80/shard; 3 shards = $5.40
        cost_schedule="sliced",
    )
    assert plan["scheduled_trial_count"] == 54
    assert len(plan["shards"]) == 3
    assert plan["launch_approved"] is False
    assert plan["max_launchable_shards"] == 1
    assert [s["within_budget_cap"] for s in plan["shards"]] == [True, False, False]

    # Same 54 trials easily approved under $10 cap.
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


def test_audit_holdout_violation_raises_and_exits_2(tmp_path: Path) -> None:
    index_rows = [
        {"nct_id": "NCT00000099", "in_audit": True, "protocol": True, "sap": False},
        {"nct_id": "NCT00000001", "in_audit": False, "protocol": True, "sap": False},
    ]

    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            index_rows,
            explicit_nct_ids={"NCT00000099"},
        )

    with pytest.raises(AuditHoldoutViolationError):
        plan_incremental_cohort(
            index_rows,
            documents=[_doc("NCT00000099", "Prot_000.pdf", SHA_1)],
        )

    # Non-bool in_audit rejected in index loader.
    bad_index = tmp_path / "bad_index.json"
    bad_index.write_text(
        json.dumps({"data": [{"nct_id": "NCT00000001", "in_audit": None}]}),
        encoding="utf-8",
    )
    with pytest.raises(CitationVerificationError):
        load_index_rows(bad_index)

    # CLI returns exit 2 on in_audit violation in both verify and plan.
    good_index = tmp_path / "index.json"
    good_index.write_text(json.dumps({"data": index_rows}), encoding="utf-8")
    bad_docs = tmp_path / "docs.jsonl"
    bad_locs = tmp_path / "locs.jsonl"
    _write_jsonl(bad_docs, [_doc("NCT00000099", "Prot_000.pdf", SHA_1)])
    _write_jsonl(bad_locs, [])

    rc_verify = main(
        [
            "verify",
            "--index",
            str(good_index),
            "--documents",
            str(bad_docs),
            "--locations",
            str(bad_locs),
        ]
    )
    assert rc_verify == 2

    rc_plan = main(
        [
            "plan",
            "--index",
            str(good_index),
            "--nct-id",
            "NCT00000099",
        ]
    )
    assert rc_plan == 2


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
                        "nct_id": "NCT01234567",
                        "in_audit": False,
                        "protocol": True,
                        "sap": False,
                        "cohorts": ["industry_phase3"],
                        "primary_outcomes_count": 1,
                    },
                    {
                        "nct_id": "NCT01234568",
                        "in_audit": False,
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

    _write_jsonl(docs_path, [_doc("NCT01234567", "Prot_000.pdf", SHA_1, physical_pages=2)])
    _write_jsonl(
        locs_path,
        _complete_primary_locations("NCT01234567", "Prot_000.pdf", SHA_1),
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
            "--out-plan",
            str(plan_path),
        ]
    )
    assert rc_p == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["launch_approved"] is True
    assert plan["partitions"]["cached_complete"] == ["NCT01234567"]
    assert plan["partitions"]["unstarted_primary"] == ["NCT01234568"]
    assert plan["shards"][0]["nct_ids"] == ["NCT01234568"]

    # Budget breach exits 1 and still writes plan file.
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
            [_doc("NCT01234567", "SAP_001.pdf", SHA_1)],
            [{"status": "UNREADABLE", "note": "All statistical sections show [CCI] white-box blanking."}],
            {},
        )
        == "redacted_cci_ppd"
    )
    assert (
        classify_unreadable_subtype(
            [_doc("NCT01234567", "SAP_001.pdf", SHA_1)],
            [{"status": "UNREADABLE", "note": "Broken ToUnicode CMap font encoding."}],
            {},
        )
        == "garbled_cmap"
    )
    assert (
        classify_unreadable_subtype(
            [_doc("NCT01234567", "SAP_001.pdf", SHA_1, physical_pages=5, pages_without_text=[1, 2, 3, 4, 5])],
            [{"status": "UNREADABLE", "note": "No text layer."}],
            {},
        )
        == "image_only_scan"
    )
