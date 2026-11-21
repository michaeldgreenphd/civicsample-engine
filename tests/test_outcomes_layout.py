"""Offline pytest suite for the cohort-agnostic sharded outcomes layout (Module 3).

Pins:
1. Exact shard-function parity with src/site_layout.py::shard_of across 120+ IDs.
2. Deterministic byte-identical encoding across shuffled inputs, gzip mtime=0 and
   OS byte 0xff at index 9, and top-level "data" last.
3. Multi-cohort index.json decoupling across industry_phase3, nih,
   cooperative_group, academic, us_federal_other, non_profit_foundation, and
   all_large_docs.
4. Verbatim reported_results preservation, None absence semantics, and Module 2
   analysis_population_parsed integration.
5. Module 1 TrialDocumentProfile, legacy documents.jsonl, Module 4
   LongitudinalProvenanceRecord, and locations.jsonl normalization + citation
   hygiene / page bounds / pages_not_searched consistency.
6. All 6 vectors of the in_audit holdout firewall (AuditFirewallError, subclass
   of ValueError), aborting BEFORE creating or touching target output dirs.
7. On-disk tamper detection via validate_outcomes_tree / problems(), strict
   provenance stamp checks, and budget telemetry.
8. Atomic .partial staging rollback, CLI graceful git fallback, and zero
   interference with src/dataset_folder.py::present_files.
9. .gitignore coverage for all 6 transient outcomes/cache/census-PDF paths.
"""
from __future__ import annotations

import ast
import copy
import gzip
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_outcomes_shards as cli  # noqa: E402
from src import dataset_folder as df  # noqa: E402
from src import outcomes_layout as ol  # noqa: E402
from src import site_layout as sl  # noqa: E402

SAMPLE_COMMIT = "5b55d47faf456e1d19e36336f985f178d357378c"
SAMPLE_TS = "2026-11-21T03:00:00+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _make_ctgov_study(
    nct_id: str,
    *,
    in_audit: bool = False,
    sponsor_name: str = "Acme Pharma",
    sponsor_class: str = "INDUSTRY",
    ecosystem_category: str | None = None,
    phase: str = "PHASE3",
    enrollment: int | None = 240,
    protocol: bool = True,
    sap: bool = True,
    cohorts: list[str] | None = None,
    include_native_locations: bool = False,
) -> dict[str, Any]:
    proto: dict[str, Any] = {
        "identificationModule": {
            "nctId": nct_id,
            "briefTitle": f"Trial {nct_id}",
        },
        "designModule": {
            "studyType": "INTERVENTIONAL",
            "phases": [phase] if phase else [],
            "enrollmentInfo": (
                {"count": enrollment, "type": "ACTUAL"} if enrollment is not None else {}
            ),
        },
        "statusModule": {
            "primaryCompletionDateStruct": {"date": "2022-06-15", "type": "ACTUAL"},
            "resultsFirstPostDateStruct": {"date": "2023-01-10", "type": "ACTUAL"},
        },
        "sponsorCollaboratorsModule": {
            "leadSponsor": {"name": sponsor_name, "class": sponsor_class},
        },
        "conditionsModule": {"conditions": ["Non-Small Cell Lung Cancer"]},
        "outcomesModule": {
            "primaryOutcomes": [
                {
                    "measure": "Overall Survival",
                    "timeFrame": "Up to 24 months",
                    "description": "Time from randomization to death from any cause.",
                }
            ]
        },
    }
    if include_native_locations:
        proto["contactsLocationsModule"] = {
            "locations": [{"facility": "General Hospital", "country": "United States"}]
        }

    study: dict[str, Any] = {
        "nct_id": nct_id,
        "in_audit": in_audit,
        "protocol": protocol,
        "sap": sap,
        "hasResults": True,
        "protocolSection": proto,
        "resultsSection": {
            "outcomeMeasuresModule": {
                "outcomeMeasures": [
                    {
                        "type": "PRIMARY",
                        "title": "Overall Survival",
                        "description": "Median OS in months",
                        "timeFrame": "24 months",
                        "reportingStatus": "POSTED",
                        "paramType": "MEDIAN",
                        "dispersionType": "95% Confidence Interval",
                        "unitOfMeasure": "months",
                        "populationDescription": "Intent-to-Treat (ITT) population",
                        "groups": [
                            {"id": "OG000", "title": "Drug A"},
                            {"id": "OG001", "title": "Placebo"},
                        ],
                        "denoms": [
                            {
                                "units": "Participants",
                                "counts": [
                                    {"groupId": "OG000", "value": "120"},
                                    {"groupId": "OG001", "value": "120"},
                                ],
                            }
                        ],
                        "classes": [],
                        "analyses": [
                            {
                                "groupIds": ["OG000", "OG001"],
                                "pValue": "<0.0001",
                                "statisticalMethod": "Log Rank",
                                "paramType": "Hazard Ratio (HR)",
                                "paramValue": "0.68",
                                "ciLowerLimit": "0.52",
                                "ciUpperLimit": "0.89",
                            }
                        ],
                    },
                    {
                        "type": "SECONDARY",
                        "title": "Progression-Free Survival",
                    },
                ]
            }
        },
    }
    if ecosystem_category is not None:
        study["ecosystem_category"] = ecosystem_category
    if cohorts is not None:
        study["cohorts"] = cohorts
    return study


def _make_doc_profile(nct_id: str, sha: str = SHA_A) -> dict[str, Any]:
    return {
        "nct_id": nct_id,
        "indexer_rules_version": "doc_indexer@2026-11-21.v1",
        "extracted_at": SAMPLE_TS,
        "pipeline_commit": SAMPLE_COMMIT,
        "readability_summary": "machine_readable",
        "redaction_flags": [],
        "excluded_icf_count": 0,
        "documents": [
            {
                "nct_id": nct_id,
                "filename": "Prot_SAP_000.pdf",
                "doc_type": "combined",
                "url": f"https://cdn.clinicaltrials.gov/large-docs/{nct_id[-2:]}/{nct_id}/Prot_SAP_000.pdf",
                "http_status": 200,
                "sha256": sha,
                "bytes": 123456,
                "physical_pages": 40,
                "pages_without_text": [],
                "readability_status": "machine_readable",
                "redaction_markers": [],
                "version_label": "Protocol v2.0",
                "version_date_as_printed": "2020-03-15",
                "upload_date": "08/21/2020 14:32",
                "pages_selected": [1, 10, 11, 12],
                "pages_deferred": [2, 3, 4],
                "retrieved_at": SAMPLE_TS,
            }
        ],
    }


def _make_citation(
    *,
    filename: str = "Prot_SAP_000.pdf",
    sha256: str = SHA_A,
    physical_page: int = 12,
    quotation: str = "Primary analysis will be conducted on the ITT population at two-sided alpha 0.05.",
) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": sha256,
        "physical_page": physical_page,
        "printed_page": "10",
        "section_heading": "9.1 Statistical Methods",
        "quotation": quotation,
    }


def _make_complete_locations(nct_id: str, *, include_second_pass: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for elem in ol.ELEMENTS:
        rows.append({
            "nct_id": nct_id,
            "primary_outcome_index": 0,
            "primary_outcome_title": "Overall Survival",
            "element": elem,
            "status": "FOUND",
            "citations": [_make_citation()],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Located in SAP section 9.1.",
            "worker": "shard-01",
            "pass": "primary",
        })
    if include_second_pass:
        rows.append({
            "nct_id": nct_id,
            "primary_outcome_index": 0,
            "primary_outcome_title": "Overall Survival",
            "element": "alpha_and_sidedness",
            "status": "FOUND",
            "citations": [_make_citation()],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "Confirmed on second pass.",
            "worker": "second-01",
            "pass": "second",
        })
    return rows


def _make_module4_provenance(nct_id: str, *, with_pdf_scraped: bool = False) -> dict[str, Any]:
    doc_entry: dict[str, Any] = {
        "filename": "Prot_SAP_000.pdf",
        "doc_type": "combined",
        "sha256": SHA_A if with_pdf_scraped else None,
        "registry_label": "Study Protocol and Statistical Analysis Plan",
        "version_label": "v2.0" if with_pdf_scraped else None,
        "version_date_as_printed": {
            "raw": "15 Mar 2020" if with_pdf_scraped else None,
            "iso_normalized": "2020-03-15" if with_pdf_scraped else None,
            "date_precision": "day" if with_pdf_scraped else "unknown",
            "earliest_iso": "2020-03-15" if with_pdf_scraped else None,
            "latest_iso": "2020-03-15" if with_pdf_scraped else None,
            "parse_note": None,
        },
        "registry_doc_date": {
            "raw": "2020-03-15",
            "iso_normalized": "2020-03-15",
            "date_precision": "day",
            "earliest_iso": "2020-03-15",
            "latest_iso": "2020-03-15",
            "parse_note": None,
        },
        "upload_date": {
            "raw": "08/21/2020 14:32",
            "iso_normalized": "2020-08-21",
            "date_precision": "day",
            "earliest_iso": "2020-08-21",
            "latest_iso": "2020-08-21",
            "parse_note": None,
        },
        "days_from_doc_upload_to_results": 872,
        "doc_upload_to_results_delta": {
            "exact_days": 872,
            "min_days": 872,
            "max_days": 872,
            "start_precision": "day",
            "end_precision": "day",
            "date_precision": "day",
        },
        "days_from_primary_completion_to_doc_upload": -663,
        "primary_completion_to_doc_upload_delta": {
            "exact_days": -663,
            "min_days": -663,
            "max_days": -663,
            "start_precision": "day",
            "end_precision": "day",
            "date_precision": "day",
        },
    }
    return {
        "nct_id": nct_id,
        "provenance_schema_version": 1,
        "provenance_rules_version": "1.0.0",
        "assembled_at": SAMPLE_TS,
        "source_pipeline_commit": SAMPLE_COMMIT,
        "registry_milestones": {
            "study_first_post_date": {
                "date": {
                    "raw": "2019-01-10",
                    "iso_normalized": "2019-01-10",
                    "date_precision": "day",
                    "earliest_iso": "2019-01-10",
                    "latest_iso": "2019-01-10",
                    "parse_note": None,
                },
                "date_type": "ACTUAL",
            },
            "primary_completion_date": {
                "date": {
                    "raw": "2022-06-15",
                    "iso_normalized": "2022-06-15",
                    "date_precision": "day",
                    "earliest_iso": "2022-06-15",
                    "latest_iso": "2022-06-15",
                    "parse_note": None,
                },
                "date_type": "ACTUAL",
            },
            "results_first_post_date": {
                "date": {
                    "raw": "2023-01-10",
                    "iso_normalized": "2023-01-10",
                    "date_precision": "day",
                    "earliest_iso": "2023-01-10",
                    "latest_iso": "2023-01-10",
                    "parse_note": None,
                },
                "date_type": "ACTUAL",
            },
            "last_update_post_date": {
                "date": {
                    "raw": "2024-02",
                    "iso_normalized": "2024-02",
                    "date_precision": "month",
                    "earliest_iso": "2024-02-01",
                    "latest_iso": "2024-02-29",
                    "parse_note": None,
                },
                "date_type": "ACTUAL",
            },
        },
        "days_from_primary_completion_to_results": 209,
        "primary_completion_to_results_delta": {
            "exact_days": 209,
            "min_days": 209,
            "max_days": 209,
            "start_precision": "day",
            "end_precision": "day",
            "date_precision": "day",
        },
        "large_docs": [doc_entry],
        "excluded_standalone_icf_count": 1,
        "linked_publications": [],
    }


# ── 1. Sharding vectors & parity with src/site_layout.py ─────────────────────

def test_shard_vectors_and_exact_parity_with_site_layout() -> None:
    for vec in ol.SHARD_VECTORS:
        assert ol.outcomes_shard_of(vec["nct_id"], vec["shards"]) == vec["shard"]
        assert ol.shard_of(vec["nct_id"], vec["shards"]) == vec["shard"]
        assert sl.shard_of(vec["nct_id"], vec["shards"]) == vec["shard"]

    # 120+ NCT IDs across multiple shard counts must match site_layout.shard_of 1:1.
    ids = [f"NCT{i * 31337 % 100_000_000:08d}" for i in range(128)]
    for shards in (1, 16, 128, 256, 1024):
        for nct in ids:
            assert ol.outcomes_shard_of(nct, shards) == sl.shard_of(nct, shards)


@pytest.mark.parametrize(
    "bad_nct",
    [None, 12345678, "nct01975376", "NCT1234567", "NCT123456789", "NCT1234567A", ""],
)
def test_outcomes_shard_of_rejects_invalid_nct_id(bad_nct: Any) -> None:
    with pytest.raises(ol.OutcomesLayoutError):
        ol.outcomes_shard_of(bad_nct, 256)


@pytest.mark.parametrize("bad_shards", [0, -1, 1025, True, False, "256", 256.0, None])
def test_outcomes_shard_of_rejects_invalid_shards(bad_shards: Any) -> None:
    with pytest.raises(ol.OutcomesLayoutError):
        ol.outcomes_shard_of("NCT01975376", bad_shards)


# ── 2. Byte-for-byte determinism, gzip header & "data" last ──────────────────

def test_byte_for_byte_determinism_gzip_header_and_data_last(tmp_path: pathlib.Path) -> None:
    studies = [
        _make_ctgov_study("NCT01975376", in_audit=False),
        _make_ctgov_study("NCT00663858", in_audit=True),
        _make_ctgov_study("NCT01174160", in_audit=False, sponsor_class="NIH", phase="PHASE2"),
    ]
    bundles_a = [
        ol.build_trial_bundle(
            s,
            shards=16,
            document_profile=_make_doc_profile(s["nct_id"]) if not s["in_audit"] else None,
            decision_rule_locations=_make_complete_locations(s["nct_id"]) if not s["in_audit"] else None,
        )
        for s in studies
    ]
    bundles_b = list(reversed(bundles_a))

    idx_a, shards_a = ol.build_outcomes_documents(
        bundles_a, extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=16
    )
    idx_b, shards_b = ol.build_outcomes_documents(
        bundles_b, extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=16
    )

    dir_a = tmp_path / "run_a"
    dir_b = tmp_path / "run_b"
    res_a = ol.write_outcomes_tree(dir_a, idx_a, shards_a)
    res_b = ol.write_outcomes_tree(dir_b, idx_b, shards_b)
    assert res_a.ok and res_b.ok

    # index.json identical byte-for-byte, ends with \n, and has "data" last.
    raw_idx_a = (dir_a / "index.json").read_bytes()
    raw_idx_b = (dir_b / "index.json").read_bytes()
    assert raw_idx_a == raw_idx_b
    assert raw_idx_a.endswith(b"\n")
    parsed_idx = json.loads(raw_idx_a.decode("utf-8"))
    assert tuple(parsed_idx.keys()) == ol.INDEX_HEADERS
    assert list(parsed_idx.keys())[-1] == "data"

    # Every shard 0..15 identical byte-for-byte with mtime=0, OS byte 0xff, and "data" last.
    for n in range(16):
        ra = (dir_a / "shards" / f"{n}.json.gz").read_bytes()
        rb = (dir_b / "shards" / f"{n}.json.gz").read_bytes()
        assert ra == rb
        assert ra[4:8] == b"\x00\x00\x00\x00"
        assert ra[9] == 0xFF
        sdoc = json.loads(gzip.decompress(ra).decode("utf-8"))
        assert tuple(sdoc.keys()) == ol.SHARD_HEADERS
        assert list(sdoc.keys())[-1] == "data"


# ── 3. Multi-cohort index.json decoupling ────────────────────────────────────

def test_multi_cohort_index_decoupling_and_counts() -> None:
    trials = [
        _make_ctgov_study("NCT00000001", sponsor_class="INDUSTRY", phase="PHASE3", protocol=True, sap=True),
        _make_ctgov_study("NCT00000002", sponsor_class="NIH", ecosystem_category="nih", phase="PHASE2", protocol=True, sap=False),
        _make_ctgov_study("NCT00000003", sponsor_class="OTHER", ecosystem_category="nctn_cooperative_group", phase="PHASE3", protocol=False, sap=True),
        _make_ctgov_study("NCT00000004", sponsor_class="OTHER", ecosystem_category="academic_medical_center", phase="PHASE2", protocol=False, sap=False),
        _make_ctgov_study("NCT00000005", sponsor_class="FED", ecosystem_category="us_federal_other", phase="PHASE3", protocol=True, sap=True),
    ]
    bundles = [ol.build_trial_bundle(t, shards=8) for t in trials]
    index_doc, _ = ol.build_outcomes_documents(
        bundles, extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=8
    )
    by_id = {row["nct_id"]: row for row in index_doc["data"]}
    assert by_id["NCT00000001"]["cohorts"] == ["all_large_docs", "industry_phase3"]
    assert by_id["NCT00000002"]["cohorts"] == ["all_large_docs", "nih"]
    assert by_id["NCT00000003"]["cohorts"] == ["all_large_docs", "cooperative_group"]
    assert by_id["NCT00000004"]["cohorts"] == ["academic"]
    assert by_id["NCT00000005"]["cohorts"] == ["all_large_docs", "us_federal_other"]
    assert index_doc["cohort_counts"] == {
        "academic": 1,
        "all_large_docs": 4,
        "cooperative_group": 1,
        "industry_phase3": 1,
        "nih": 1,
        "us_federal_other": 1,
    }


def test_derive_cohorts_rejects_empty_or_invalid_slug() -> None:
    with pytest.raises(ol.OutcomesLayoutError):
        ol.derive_cohorts([], protocol=False, sap=False)
    with pytest.raises(ol.OutcomesLayoutError):
        ol.derive_cohorts(["Bad-Slug!"])


# ── 4. Verbatim reported_results & None absence semantics ────────────────────

def test_reported_results_verbatim_and_none_absence() -> None:
    study = _make_ctgov_study("NCT01975376", enrollment=None, phase="")
    study["protocolSection"]["statusModule"] = {}
    parsed_pop = {
        "nct_id": "NCT01975376",
        "parser_rules_version": "analysis_pop@2026-11-20.v1",
        "outcomes": [
            {
                "outcome_index": 0,
                "outcome_type": "PRIMARY",
                "population_class": "itt",
                "overall_participants_analyzed": 240.0,
            }
        ],
    }
    row, rec = ol.build_trial_bundle(study, shards=16, parsed_populations=parsed_pop)
    assert row["enrollment"] is None  # absence never coerced to 0
    assert row["primary_completion_date"] is None
    assert row["results_first_post_date"] is None

    rr = rec["reported_results"]
    assert rr["section_label"] == "Primary results as posted"
    assert rr["results_posted"] is True
    assert len(rr["primary_outcome_measures"]) == 1  # SECONDARY excluded
    pm = rr["primary_outcome_measures"][0]
    assert pm["analyses"][0]["pValue"] == "<0.0001"  # verbatim string preserved
    assert pm["population_description"] == "Intent-to-Treat (ITT) population"
    assert pm["analysis_population_parsed"]["population_class"] == "itt"


# ── 5. Module 1, legacy documents.jsonl, locations.jsonl & Module 4 adapters ─

def test_legacy_documents_jsonl_and_raw_text_rejection() -> None:
    legacy_rows = [
        {
            "nct_id": "NCT01975376",
            "filename": "Prot_000.pdf",
            "url": "https://cdn.clinicaltrials.gov/large-docs/76/NCT01975376/Prot_000.pdf",
            "http_status": 200,
            "sha256": SHA_A,
            "bytes": 5000,
            "doc_type": "protocol",
            "version_label": "v1",
            "version_date_as_printed": "2020-01-01",
            "physical_pages": 10,
            "pages_without_text": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "retrieved_at": SAMPLE_TS,
        },
        {
            "nct_id": "NCT01975376",
            "filename": "SAP_001.pdf",
            "url": "https://cdn.clinicaltrials.gov/large-docs/76/NCT01975376/SAP_001.pdf",
            "http_status": 200,
            "sha256": SHA_B,
            "bytes": 4000,
            "doc_type": "sap",
            "version_label": "",
            "version_date_as_printed": "",
            "physical_pages": 8,
            "pages_without_text": [],
            "retrieved_at": SAMPLE_TS,
        },
    ]
    prof = ol.normalize_document_profile(legacy_rows)
    assert prof is not None
    assert prof["readability_summary"] == "partial_image_or_redacted"
    assert [d["readability_status"] for d in prof["documents"]] == ["image_only_scan", "machine_readable"]
    assert prof["documents"][1]["version_label"] is None  # empty string -> None

    # Embedded raw text arrays must be rejected so shards stay compact.
    bad_prof = _make_doc_profile("NCT01975376")
    bad_prof["documents"][0]["page_texts"] = ["raw page 1"]
    with pytest.raises(ol.OutcomesLayoutError, match="raw PDF text"):
        ol.normalize_document_profile(bad_prof)


def test_locations_validation_citations_ellipsis_page_bounds_and_unsearched() -> None:
    prof = ol.normalize_document_profile(_make_doc_profile("NCT01975376"))
    rows = _make_complete_locations("NCT01975376", include_second_pass=True)
    norm = ol.normalize_decision_rule_locations(rows, document_profile=prof, primary_outcomes_count=1)
    assert norm is not None
    assert norm["census_status"] == "complete"
    assert len(norm["primary_pass"]) == 7
    assert norm["second_pass"] is not None and len(norm["second_pass"]) == 1

    # Ellipsis rejected
    bad_ellipsis = copy.deepcopy(rows)
    bad_ellipsis[0]["citations"][0]["quotation"] = "Primary analysis ... ITT population."
    with pytest.raises(ol.OutcomesLayoutError, match="ellipsis"):
        ol.normalize_decision_rule_locations(bad_ellipsis, document_profile=prof)

    # Unicode ellipsis rejected
    bad_unicode_ellipsis = copy.deepcopy(rows)
    bad_unicode_ellipsis[0]["citations"][0]["quotation"] = "Primary analysis\u2026 ITT"
    with pytest.raises(ol.OutcomesLayoutError, match="ellipsis"):
        ol.normalize_decision_rule_locations(bad_unicode_ellipsis, document_profile=prof)

    # Out-of-bounds physical_page rejected against document_profile (max 40)
    bad_page = copy.deepcopy(rows)
    bad_page[0]["citations"][0]["physical_page"] = 41
    with pytest.raises(ol.OutcomesLayoutError, match="out of bounds"):
        ol.normalize_decision_rule_locations(bad_page, document_profile=prof)

    # NOT_FOUND with non-empty pages_not_searched rejected
    bad_not_found = copy.deepcopy(rows)
    bad_not_found[0]["status"] = "NOT_FOUND"
    bad_not_found[0]["citations"] = []
    bad_not_found[0]["pages_searched"] = {"Prot_SAP_000.pdf": [1, 2, 3]}
    bad_not_found[0]["pages_not_searched"] = {"Prot_SAP_000.pdf": [4, 5]}
    with pytest.raises(ol.OutcomesLayoutError, match="NOT_FOUND"):
        ol.normalize_decision_rule_locations(bad_not_found, document_profile=prof)

    # Duplicate (primary_outcome_index, element) rejected
    dup_rows = copy.deepcopy(rows) + [copy.deepcopy(rows[0])]
    with pytest.raises(ol.OutcomesLayoutError, match="duplicate"):
        ol.normalize_decision_rule_locations(dup_rows, document_profile=prof)


def test_module4_longitudinal_provenance_adapter() -> None:
    prov = _make_module4_provenance("NCT01975376", with_pdf_scraped=True)
    norm = ol.normalize_longitudinal_provenance(prov, in_audit=False)
    assert norm is not None
    assert "registry_milestones" in norm
    assert "posted_documents" in norm and len(norm["posted_documents"]) == 1
    assert "publications" in norm
    assert norm["date_deltas"]["days_from_primary_completion_to_results"] == 209

    # Invalid date_precision rejected
    bad_prov = copy.deepcopy(prov)
    bad_prov["primary_completion_to_results_delta"]["date_precision"] = "decade"
    with pytest.raises(ol.OutcomesLayoutError, match="date_precision"):
        ol.normalize_longitudinal_provenance(bad_prov, in_audit=False)


# ── 6. Parametrized 6-vector in_audit holdout firewall ───────────────────────

@pytest.mark.parametrize(
    "vector_name,kwargs",
    [
        ("v1_missing_in_audit", {"in_audit": None}),
        ("v1_non_bool_int", {"in_audit": 0}),
        ("v1_non_bool_str", {"in_audit": "false"}),
        ("v2_locations_dict", {"in_audit": True, "decision_rule_locations": {}}),
        ("v2_locations_list", {"in_audit": True, "decision_rule_locations": []}),
        ("v3_forbidden_key_llm", {"in_audit": True, "raw_payload": {"llm_assessment": {"x": 1}}}),
        ("v3_forbidden_key_worker", {"in_audit": True, "raw_payload": {"worker_notes": "draft"}}),
        (
            "v3_extra_shard_key",
            {
                "in_audit": True,
                "shard_record": {
                    "nct_id": "NCT00663858",
                    "in_audit": True,
                    "cohorts": ["industry_phase3"],
                    "shard": 50,
                    "reported_results": {},
                    "document_profile": None,
                    "decision_rule_locations": None,
                    "longitudinal_provenance": None,
                    "unexpected_extra": 123,
                },
            },
        ),
        ("v4_document_profile_present", {"in_audit": True, "document_profile": {"documents": []}}),
        (
            "v4_provenance_pdf_scraped_sha256",
            {
                "in_audit": True,
                "longitudinal_provenance": _make_module4_provenance("NCT00663858", with_pdf_scraped=True),
            },
        ),
        (
            "v5_bad_census_badge_status",
            {
                "in_audit": True,
                "index_row": {
                    "nct_id": "NCT00663858",
                    "brief_title": "T",
                    "study_type": None,
                    "phase": None,
                    "enrollment": None,
                    "enrollment_type": None,
                    "primary_completion_date": None,
                    "results_first_post_date": None,
                    "lead_sponsor_name": None,
                    "sponsor_class": None,
                    "ecosystem_category": None,
                    "primary_condition": None,
                    "cohorts": ["industry_phase3"],
                    "in_audit": True,
                    "protocol": True,
                    "sap": True,
                    "primary_outcomes_count": 1,
                    "has_reported_results": True,
                    "document_badge": {
                        "readability_status": "in_audit_holdout",
                        "redaction_flags": None,
                        "doc_count": None,
                        "total_physical_pages": None,
                    },
                    "census_badge": {
                        "census_status": "complete",
                        "status_counts": None,
                        "has_second_pass": False,
                    },
                },
            },
        ),
        (
            "v5_bad_doc_badge_doc_count",
            {
                "in_audit": True,
                "index_row": {
                    "nct_id": "NCT00663858",
                    "brief_title": "T",
                    "study_type": None,
                    "phase": None,
                    "enrollment": None,
                    "enrollment_type": None,
                    "primary_completion_date": None,
                    "results_first_post_date": None,
                    "lead_sponsor_name": None,
                    "sponsor_class": None,
                    "ecosystem_category": None,
                    "primary_condition": None,
                    "cohorts": ["industry_phase3"],
                    "in_audit": True,
                    "protocol": True,
                    "sap": True,
                    "primary_outcomes_count": 1,
                    "has_reported_results": True,
                    "document_badge": {
                        "readability_status": "in_audit_holdout",
                        "redaction_flags": None,
                        "doc_count": 2,
                        "total_physical_pages": None,
                    },
                    "census_badge": {
                        "census_status": "in_audit_holdout",
                        "status_counts": None,
                        "has_second_pass": False,
                    },
                },
            },
        ),
    ],
)
def test_audit_firewall_vectors_1_through_5_raise_value_error(
    vector_name: str,
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ol.AuditFirewallError):
        ol.assert_no_audit_leakage(nct_id="NCT00663858", **kwargs)
    # AuditFirewallError must also be a ValueError.
    with pytest.raises(ValueError):
        ol.assert_no_audit_leakage(nct_id="NCT00663858", **kwargs)


def test_in_audit_ctgov_study_with_native_contacts_locations_is_allowed() -> None:
    """Native CT.gov contactsLocationsModule.locations must not false-positive."""
    study = _make_ctgov_study("NCT00663858", in_audit=True, include_native_locations=True)
    prov_clean = _make_module4_provenance("NCT00663858", with_pdf_scraped=False)
    row, rec = ol.build_trial_bundle(study, shards=16, longitudinal_provenance=prov_clean)
    assert row["in_audit"] is True
    assert row["document_badge"]["readability_status"] == "in_audit_holdout"
    assert row["census_badge"]["census_status"] == "in_audit_holdout"
    assert rec["document_profile"] is None
    assert rec["decision_rule_locations"] is None


@pytest.mark.parametrize("leak_stream", ["locations", "documents"])
def test_audit_firewall_vector_6_aborts_cli_before_touching_out_dir(
    tmp_path: pathlib.Path,
    leak_stream: str,
) -> None:
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(
        json.dumps([
            _make_ctgov_study("NCT01975376", in_audit=False),
            _make_ctgov_study("NCT00663858", in_audit=True),
        ]),
        encoding="utf-8",
    )
    out_dir = tmp_path / "outcomes_target"
    argv = [
        "--trials",
        str(trials_path),
        "--extracted-at",
        SAMPLE_TS,
        "--pipeline-commit",
        SAMPLE_COMMIT,
        "--shards",
        "8",
        "--out-dir",
        str(out_dir),
    ]
    if leak_stream == "locations":
        loc_path = tmp_path / "locations.jsonl"
        loc_path.write_text(
            "\n".join(json.dumps(r) for r in _make_complete_locations("NCT00663858")) + "\n",
            encoding="utf-8",
        )
        argv += ["--locations", str(loc_path)]
    else:
        doc_path = tmp_path / "documents.jsonl"
        doc_path.write_text(json.dumps(_make_doc_profile("NCT00663858")) + "\n", encoding="utf-8")
        argv += ["--documents", str(doc_path)]

    rc = cli.main(argv)
    assert rc == 2
    assert not out_dir.exists(), "Vector 6 must abort BEFORE creating out_dir"
    assert not (tmp_path / "outcomes_target.partial").exists()


# ── 7. On-disk tamper detection & provenance validation ──────────────────────

def test_validate_outcomes_tree_detects_tampering_and_strict_commit(tmp_path: pathlib.Path) -> None:
    s1 = _make_ctgov_study("NCT01975376", in_audit=False)
    s2 = _make_ctgov_study("NCT00663858", in_audit=True)
    bundles = [
        ol.build_trial_bundle(s1, shards=8, document_profile=_make_doc_profile("NCT01975376")),
        ol.build_trial_bundle(s2, shards=8),
    ]
    idx, shards = ol.build_outcomes_documents(
        bundles, extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=8
    )
    out = tmp_path / "outcomes"
    res = ol.write_outcomes_tree(out, idx, shards)
    assert res.ok and res.budget is not None
    assert res.budget.total_bytes > 0
    assert ol.problems(out) == []

    # Tamper 1: missing shard file
    missing_case = tmp_path / "tamper_missing"
    ol.write_outcomes_tree(missing_case, idx, shards)
    (missing_case / "shards" / "7.json.gz").unlink()
    assert any("lacks 1 shard" in p for p in ol.problems(missing_case))

    # Tamper 2: wrong gzip header (non-zero mtime)
    mtime_case = tmp_path / "tamper_mtime"
    ol.write_outcomes_tree(mtime_case, idx, shards)
    shard0 = mtime_case / "shards" / "0.json.gz"
    raw = bytearray(shard0.read_bytes())
    raw[4] = 1  # mtime != 0
    shard0.write_bytes(bytes(raw))
    assert any("mtime" in p for p in ol.problems(mtime_case))

    # Tamper 3: inject decision_rule_locations into in_audit=True shard on disk
    leak_case = tmp_path / "tamper_leak"
    ol.write_outcomes_tree(leak_case, idx, shards)
    audit_shard = ol.outcomes_shard_of("NCT00663858", 8)
    sp = leak_case / "shards" / f"{audit_shard}.json.gz"
    sdoc = json.loads(gzip.decompress(sp.read_bytes()).decode("utf-8"))
    sdoc["data"]["NCT00663858"]["decision_rule_locations"] = {"leaked": True}
    sp.write_bytes(ol.encode_gz_bytes(sdoc))
    leak_res = ol.validate_outcomes_tree(leak_case)
    assert not leak_res.ok
    assert leak_res.audit_leakage_detected is True
    assert cli.main(["--check", str(leak_case)]) == 2

    # Tamper 4: null commit rejected by strict default validate_outcomes_tree
    idx_null, shards_null = ol.build_outcomes_documents(
        bundles,
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=None,
        shards=8,
        allow_null_commit=True,
    )
    null_dir = tmp_path / "null_commit"
    ol.write_outcomes_tree(null_dir, idx_null, shards_null, allow_null_commit=True)
    assert not ol.validate_outcomes_tree(null_dir).ok
    assert ol.validate_outcomes_tree(null_dir, allow_null_commit=True).ok


# ── 8. Atomic rollback, CLI sandbox fallback & dataset_folder isolation ──────

def test_atomic_rollback_and_sibling_file_preservation(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "outcomes"
    s1 = _make_ctgov_study("NCT01975376", in_audit=False)
    idx, shards = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx, shards)
    (out / "unrelated_note.txt").write_text("keep me", encoding="utf-8")
    before_index = (out / "index.json").read_bytes()

    # Corrupt a shard header before write_outcomes_tree -> staging validation fails,
    # leaving out_dir untouched and removing .partial.
    bad_shards = copy.deepcopy(shards)
    bad_shards[0]["extracted_at"] = "1999-01-01T00:00:00+00:00"
    with pytest.raises(ol.OutcomesLayoutError):
        ol.write_outcomes_tree(out, idx, bad_shards)
    assert (out / "index.json").read_bytes() == before_index
    assert (out / "unrelated_note.txt").read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "outcomes.partial").exists()


def test_dataset_folder_present_files_ignores_outcomes_directory(tmp_path: pathlib.Path) -> None:
    site_data = tmp_path / "data"
    site_data.mkdir()
    (site_data / "demographics.part1.json.gz").write_bytes(b"stub")
    (site_data / "run.json").write_text("{}", encoding="utf-8")

    s1 = _make_ctgov_study("NCT01975376", in_audit=False)
    idx, shards = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=8)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=8,
    )
    ol.write_outcomes_tree(site_data / "outcomes", idx, shards)

    present = df.present_files(str(site_data))
    assert present == ["demographics.part1.json.gz", "run.json"]
    assert not any("outcomes" in name for name in present)


def test_cli_end_to_end_and_graceful_git_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(
        json.dumps([
            _make_ctgov_study("NCT01975376", in_audit=False),
            _make_ctgov_study("NCT00663858", in_audit=True),
        ]),
        encoding="utf-8",
    )
    docs_path = tmp_path / "profiles.jsonl"
    docs_path.write_text(json.dumps(_make_doc_profile("NCT01975376")) + "\n", encoding="utf-8")
    locs_path = tmp_path / "locations.jsonl"
    locs_path.write_text(
        "\n".join(json.dumps(r) for r in _make_complete_locations("NCT01975376")) + "\n",
        encoding="utf-8",
    )
    prov_path = tmp_path / "provenance.jsonl"
    prov_path.write_text(
        json.dumps(_make_module4_provenance("NCT01975376", with_pdf_scraped=True)) + "\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    # Simulate git unavailable inside sandbox + no GITHUB_SHA.
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(
        ol.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("git blocked in sandbox")),
    )

    out_fallback = tmp_path / "out_fallback"
    rc_fallback = cli.main([
        "--trials", str(trials_path),
        "--documents", str(docs_path),
        "--locations", str(locs_path),
        "--provenance", str(prov_path),
        "--extracted-at", SAMPLE_TS,
        "--shards", "8",
        "--out-dir", str(out_fallback),
        "--report", str(report_path),
    ])
    assert rc_fallback == 0
    assert ol.validate_outcomes_tree(out_fallback, allow_null_commit=True).ok
    assert not ol.validate_outcomes_tree(out_fallback).ok  # strict check catches null commit
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["total_trials"] == 2
    assert report["audit_holdout_trials"] == 1

    # Explicit --pipeline-commit satisfies strict --check (exit 0).
    out_strict = tmp_path / "out_strict"
    rc_strict = cli.main([
        "--trials", str(trials_path),
        "--documents", str(docs_path),
        "--locations", str(locs_path),
        "--provenance", str(prov_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_strict),
    ])
    assert rc_strict == 0
    assert cli.main(["--check", str(out_strict)]) == 0


# ── 9. .gitignore coverage & Python 3.11 AST / typing gate ───────────────────

def test_gitignore_covers_transient_outcomes_and_cache_paths() -> None:
    expected_entries = [
        "data/outcomes/",
        "data/outcomes.partial/",
        "data/document_cache/",
        "data/document_index_by_sha256.jsonl",
        "data/trial_document_profiles.jsonl",
        "rule-census-20261114/pdfs/",
    ]
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for entry in expected_entries:
        assert entry in lines, f".gitignore missing required entry {entry!r}"

    # Verify via git check-ignore when inside a reachable git worktree.
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        for sample in (
            "data/outcomes/index.json",
            "data/outcomes.partial/index.json",
            "data/document_cache/url_manifest.json",
            "data/document_index_by_sha256.jsonl",
            "data/trial_document_profiles.jsonl",
            "rule-census-20261114/pdfs/NCT01975376/Prot_000.pdf",
        ):
            chk = subprocess.run(
                ["git", "check-ignore", "-q", sample],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            assert chk.returncode == 0, f"git check-ignore did not ignore {sample}"


def test_python311_ast_and_full_type_annotations() -> None:
    for rel in ("src/outcomes_layout.py", "scripts/build_outcomes_shards.py", "tests/test_outcomes_layout.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel, feature_version=(3, 11))
        has_future = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body[:5]
        )
        assert has_future, f"{rel} missing 'from __future__ import annotations'"
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.returns is not None, f"{rel}:{node.lineno} {node.name} missing return annotation"
                args = node.args
                for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                    if arg.arg in ("self", "cls"):
                        continue
                    assert arg.annotation is not None, (
                        f"{rel}:{node.lineno} {node.name} arg {arg.arg!r} missing annotation"
                    )
                if args.vararg is not None:
                    assert args.vararg.annotation is not None
                if args.kwarg is not None:
                    assert args.kwarg.annotation is not None
