"""Offline unit and CLI tests for scripts/verify_and_plan_census_batch.py."""
from __future__ import annotations

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
    ELEMENTS,
    InvalidNctIdError,
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
        }
    ]

    all_locs = locs_11 + locs_12 + locs_13 + locs_14

    for mode in ("incremental-delta", "recover-unreadable"):
        plan = plan_incremental_cohort(
            index_rows,
            documents=documents,
            locations=all_locs,
            cohort="industry_phase3",
            mode=mode,
            shard_size=10,
            budget_cap_usd=50.0,
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

    plan_delta = plan_incremental_cohort(
        index_rows,
        documents=documents,
        locations=all_locs,
        mode="incremental-delta",
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
