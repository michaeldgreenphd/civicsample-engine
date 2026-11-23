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
9. .gitignore coverage for the bundle directory and its staging directory.
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
    ids = [f"NCT99{i * 31337 % 1_000_000:06d}" for i in range(128)]
    for shards in (1, 16, 128, 256, 1024):
        for nct in ids:
            assert ol.outcomes_shard_of(nct, shards) == sl.shard_of(nct, shards)


@pytest.mark.parametrize(
    "bad_nct",
    [None, 12345678, "nct99000076", "NCT1234567", "NCT123456789", "NCT1234567A", ""],
)
def test_outcomes_shard_of_rejects_invalid_nct_id(bad_nct: Any) -> None:
    with pytest.raises(ol.OutcomesLayoutError):
        ol.outcomes_shard_of(bad_nct, 256)


@pytest.mark.parametrize("bad_shards", [0, -1, 1025, True, False, "256", 256.0, None])
def test_outcomes_shard_of_rejects_invalid_shards(bad_shards: Any) -> None:
    with pytest.raises(ol.OutcomesLayoutError):
        ol.outcomes_shard_of("NCT99000076", bad_shards)


# ── 2. Byte-for-byte determinism, gzip header & "data" last ──────────────────

def test_byte_for_byte_determinism_gzip_header_and_data_last(tmp_path: pathlib.Path) -> None:
    studies = [
        _make_ctgov_study("NCT99000076", in_audit=False),
        _make_ctgov_study("NCT99000058", in_audit=True),
        _make_ctgov_study("NCT99000060", in_audit=False, sponsor_class="NIH", phase="PHASE2"),
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
        _make_ctgov_study("NCT99100001", sponsor_class="INDUSTRY", phase="PHASE3", protocol=True, sap=True),
        _make_ctgov_study("NCT99100002", sponsor_class="NIH", ecosystem_category="nih", phase="PHASE2", protocol=True, sap=False),
        _make_ctgov_study("NCT99100003", sponsor_class="OTHER", ecosystem_category="nctn_cooperative_group", phase="PHASE3", protocol=False, sap=True),
        _make_ctgov_study("NCT99100004", sponsor_class="OTHER", ecosystem_category="academic_medical_center", phase="PHASE2", protocol=False, sap=False),
        _make_ctgov_study("NCT99100005", sponsor_class="FED", ecosystem_category="us_federal_other", phase="PHASE3", protocol=True, sap=True),
    ]
    bundles = [ol.build_trial_bundle(t, shards=8) for t in trials]
    index_doc, _ = ol.build_outcomes_documents(
        bundles, extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=8
    )
    by_id = {row["nct_id"]: row for row in index_doc["data"]}
    assert by_id["NCT99100001"]["cohorts"] == ["all_large_docs", "industry_phase3"]
    assert by_id["NCT99100002"]["cohorts"] == ["all_large_docs", "nih"]
    assert by_id["NCT99100003"]["cohorts"] == ["all_large_docs", "cooperative_group"]
    assert by_id["NCT99100004"]["cohorts"] == ["academic"]
    assert by_id["NCT99100005"]["cohorts"] == ["all_large_docs", "us_federal_other"]
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
    study = _make_ctgov_study("NCT99000076", enrollment=None, phase="")
    study["protocolSection"]["statusModule"] = {}
    parsed_pop = {
        "nct_id": "NCT99000076",
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
            "nct_id": "NCT99000076",
            "filename": "Prot_000.pdf",
            "url": "https://cdn.clinicaltrials.gov/large-docs/76/NCT99000076/Prot_000.pdf",
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
            "nct_id": "NCT99000076",
            "filename": "SAP_001.pdf",
            "url": "https://cdn.clinicaltrials.gov/large-docs/76/NCT99000076/SAP_001.pdf",
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
    bad_prof = _make_doc_profile("NCT99000076")
    bad_prof["documents"][0]["page_texts"] = ["raw page 1"]
    with pytest.raises(ol.OutcomesLayoutError, match="raw PDF text"):
        ol.normalize_document_profile(bad_prof)


def test_locations_validation_citations_ellipsis_page_bounds_and_unsearched() -> None:
    prof = ol.normalize_document_profile(_make_doc_profile("NCT99000076"))
    rows = _make_complete_locations("NCT99000076", include_second_pass=True)
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

    # AMBIGUOUS with 0 citations rejected
    bad_ambig = copy.deepcopy(rows)
    bad_ambig[0]["status"] = "AMBIGUOUS"
    bad_ambig[0]["citations"] = []
    with pytest.raises(ol.OutcomesLayoutError, match="AMBIGUOUS"):
        ol.normalize_decision_rule_locations(bad_ambig, document_profile=prof)

    # NOT_APPLICABLE with 0 citations rejected (C7), while >= 1 citation is accepted
    bad_na = copy.deepcopy(rows)
    bad_na[0]["status"] = "NOT_APPLICABLE"
    bad_na[0]["citations"] = []
    with pytest.raises(ol.OutcomesLayoutError, match="NOT_APPLICABLE"):
        ol.normalize_decision_rule_locations(bad_na, document_profile=prof)

    good_na = copy.deepcopy(rows)
    good_na[0]["status"] = "NOT_APPLICABLE"
    good_na[0]["citations"] = [_make_citation(quotation="Superiority trial; non-inferiority margin does not apply.")]
    norm_na = ol.normalize_decision_rule_locations(good_na, document_profile=prof, primary_outcomes_count=1)
    assert norm_na is not None
    assert norm_na["primary_pass"][0]["status"] == "NOT_APPLICABLE"
    assert len(norm_na["primary_pass"][0]["citations"]) == 1
    assert norm_na["effective_pass"][0]["status"] == "NOT_APPLICABLE"

    # NO_DOCUMENT rejected when document_profile has posted documents (C4 defense-in-depth)
    bad_nodoc = copy.deepcopy(rows)
    bad_nodoc[0]["status"] = "NO_DOCUMENT"
    bad_nodoc[0]["citations"] = []
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.normalize_decision_rule_locations(bad_nodoc, document_profile=prof)

    # NO_DOCUMENT allowed when document_profile is None or has zero documents
    nodoc_rows = [
        {
            "nct_id": "NCT99000076",
            "primary_outcome_index": 0,
            "primary_outcome_title": "Overall Survival",
            "element": elem,
            "status": "NO_DOCUMENT",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "No protocol or SAP posted.",
            "worker": "shard-01",
            "pass": "primary",
        }
        for elem in ol.ELEMENTS
    ]
    empty_prof = ol.normalize_document_profile({"documents": []})
    norm_nodoc_none = ol.normalize_decision_rule_locations(nodoc_rows, document_profile=None, primary_outcomes_count=1)
    norm_nodoc_empty = ol.normalize_decision_rule_locations(nodoc_rows, document_profile=empty_prof, primary_outcomes_count=1)
    assert norm_nodoc_none is not None and norm_nodoc_none["census_status"] == "no_document"
    assert norm_nodoc_empty is not None and norm_nodoc_empty["census_status"] == "no_document"


def test_ocr_recovery_and_adjudication_passes_effective_pass_and_cli(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    prof = ol.normalize_document_profile(_make_doc_profile(nct_id))
    assert prof is not None

    primary_rows = _make_complete_locations(nct_id)
    for row in primary_rows:
        if row["element"] == "alpha_and_sidedness":
            row["status"] = "UNREADABLE"
            row["citations"] = []
            row["note"] = "Image-only scan on statistical methods page."
        elif row["element"] == "margin":
            row["status"] = "AMBIGUOUS"
            row["citations"] = [_make_citation(quotation="Non-inferiority margin described across two sections.")]
            row["note"] = "Requires adjudication."

    ocr_row: dict[str, Any] = {
        "nct_id": nct_id,
        "primary_outcome_index": 0,
        "primary_outcome_title": "Overall Survival",
        "element": "alpha_and_sidedness",
        "status": "FOUND",
        "citations": [_make_citation(quotation="Two-sided alpha level of 0.05 recovered via OCR.")],
        "pages_searched": "all",
        "pages_not_searched": [],
        "note": "Recovered after macOS Vision OCR.",
        "worker": "ocr-01",
        "pass": "ocr_recovery",
        "text_source": "macos_vision_ocr_v1",
    }
    adj_row: dict[str, Any] = {
        "nct_id": nct_id,
        "primary_outcome_index": 0,
        "primary_outcome_title": "Overall Survival",
        "element": "margin",
        "status": "FOUND",
        "citations": [_make_citation(quotation="Final adjudicated non-inferiority margin is HR 1.15.")],
        "pages_searched": "all",
        "pages_not_searched": [],
        "note": "Adjudicated by senior reviewer.",
        "worker": "adj-01",
        "pass": "adjudication",
    }
    flat_rows = primary_rows + [ocr_row, adj_row]

    norm_flat = ol.normalize_decision_rule_locations(
        flat_rows,
        document_profile=prof,
        primary_outcomes_count=1,
    )
    assert norm_flat is not None
    assert norm_flat["census_status"] == "complete"
    assert len(norm_flat["primary_pass"]) == 7
    assert norm_flat["second_pass"] is None
    assert norm_flat["ocr_recovery_pass"] is not None and len(norm_flat["ocr_recovery_pass"]) == 1
    assert norm_flat["adjudication_pass"] is not None and len(norm_flat["adjudication_pass"]) == 1
    assert len(norm_flat["effective_pass"]) == 7

    # Full append-only pass history preserved without mutating source passes.
    primary_by_el = {r["element"]: r for r in norm_flat["primary_pass"]}
    assert primary_by_el["alpha_and_sidedness"]["status"] == "UNREADABLE"
    assert primary_by_el["margin"]["status"] == "AMBIGUOUS"
    assert all("source_pass" not in r for r in norm_flat["primary_pass"])
    assert norm_flat["ocr_recovery_pass"][0]["text_source"] == "macos_vision_ocr_v1"
    assert "source_pass" not in norm_flat["ocr_recovery_pass"][0]
    assert "source_pass" not in norm_flat["adjudication_pass"][0]

    # effective_pass resolves cells under primary -> ocr_recovery -> adjudication.
    eff_by_el = {r["element"]: r for r in norm_flat["effective_pass"]}
    assert all(r["status"] == "FOUND" for r in norm_flat["effective_pass"])
    assert eff_by_el["alpha_and_sidedness"]["source_pass"] == "ocr_recovery"
    assert eff_by_el["alpha_and_sidedness"]["text_source"] == "macos_vision_ocr_v1"
    assert eff_by_el["margin"]["source_pass"] == "adjudication"
    assert eff_by_el["primary_endpoint_analysed"]["source_pass"] == "primary"

    # Precedence: adjudication overrides ocr_recovery when both touch the same cell.
    adj_override = copy.deepcopy(ocr_row)
    adj_override["pass"] = "adjudication"
    adj_override["note"] = "Adjudication supersedes OCR."
    adj_override["text_source"] = "macos_vision_ocr_v1"
    norm_override = ol.normalize_decision_rule_locations(
        flat_rows + [adj_override],
        document_profile=prof,
        primary_outcomes_count=1,
    )
    assert norm_override is not None
    eff_override = {r["element"]: r for r in norm_override["effective_pass"]}
    assert eff_override["alpha_and_sidedness"]["source_pass"] == "adjudication"
    assert eff_override["alpha_and_sidedness"]["note"] == "Adjudication supersedes OCR."

    # Pre-grouped mapping produces byte-identical normalized output to flat rows.
    grouped_input: dict[str, Any] = {
        "nct_id": nct_id,
        "primary_pass": primary_rows,
        "ocr_recovery_pass": [ocr_row],
        "adjudication_pass": [adj_row],
    }
    norm_grouped = ol.normalize_decision_rule_locations(
        grouped_input,
        document_profile=prof,
        primary_outcomes_count=1,
    )
    assert norm_grouped == norm_flat

    # build_trial_bundle propagates effective_pass counts into index_row.census_badge.
    study = _make_ctgov_study(nct_id, in_audit=False)
    row_out, rec_out = ol.build_trial_bundle(
        study,
        shards=8,
        document_profile=_make_doc_profile(nct_id),
        decision_rule_locations=flat_rows,
    )
    assert row_out["census_badge"] == {
        "census_status": "complete",
        "has_second_pass": False,
        "status_counts": {"FOUND": 7},
    }
    assert rec_out["decision_rule_locations"] == norm_flat

    # End-to-end build_outcomes_shards.py CLI with flat locations.jsonl.
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(json.dumps([study]), encoding="utf-8")
    docs_path = tmp_path / "docs.jsonl"
    docs_path.write_text(json.dumps(_make_doc_profile(nct_id)) + "\n", encoding="utf-8")
    locs_path = tmp_path / "locations.jsonl"
    locs_path.write_text("\n".join(json.dumps(r) for r in flat_rows) + "\n", encoding="utf-8")
    out_dir = tmp_path / "outcomes_cli"

    rc = cli.main([
        "--trials", str(trials_path),
        "--documents", str(docs_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    assert cli.main(["--check", str(out_dir)]) == 0

    idx_doc = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    assert idx_doc["data"][0]["census_badge"]["status_counts"] == {"FOUND": 7}
    shard_id = ol.outcomes_shard_of(nct_id, 8)
    sdoc = json.loads(gzip.decompress((out_dir / "shards" / f"{shard_id}.json.gz").read_bytes()).decode("utf-8"))
    shard_locs = sdoc["data"][nct_id]["decision_rule_locations"]
    assert shard_locs["ocr_recovery_pass"][0]["text_source"] == "macos_vision_ocr_v1"
    assert len(shard_locs["adjudication_pass"]) == 1
    assert len(shard_locs["effective_pass"]) == 7

    # Tampering with effective_pass on disk is detected by validate_outcomes_tree.
    tampered_sdoc = copy.deepcopy(sdoc)
    tampered_sdoc["data"][nct_id]["decision_rule_locations"]["effective_pass"][0]["source_pass"] = "ocr_recovery"
    (out_dir / "shards" / f"{shard_id}.json.gz").write_bytes(ol.encode_gz_bytes(tampered_sdoc))
    tamper_res = ol.validate_outcomes_tree(out_dir)
    assert not tamper_res.ok
    assert any("effective_pass" in p for p in tamper_res.problems)

    # Non-string text_source is rejected.
    bad_ts = copy.deepcopy(ocr_row)
    bad_ts["text_source"] = 42
    with pytest.raises(ol.OutcomesLayoutError, match="text_source"):
        ol.normalize_decision_rule_locations(primary_rows + [bad_ts], document_profile=prof)

    # NO_DOCUMENT in ocr_recovery_pass rejected when document_profile has posted docs.
    bad_ocr_nodoc = copy.deepcopy(ocr_row)
    bad_ocr_nodoc["status"] = "NO_DOCUMENT"
    bad_ocr_nodoc["citations"] = []
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.normalize_decision_rule_locations(primary_rows + [bad_ocr_nodoc], document_profile=prof)


def _make_no_document_locations(nct_id: str) -> list[dict[str, Any]]:
    return [
        {
            "nct_id": nct_id,
            "primary_outcome_index": 0,
            "primary_outcome_title": "Overall Survival",
            "element": elem,
            "status": "NO_DOCUMENT",
            "citations": [],
            "pages_searched": "all",
            "pages_not_searched": [],
            "note": "No protocol or SAP posted.",
            "worker": "shard-01",
            "pass": "primary",
        }
        for elem in ol.ELEMENTS
    ]


def test_no_document_rejected_by_trial_flags_large_docs_and_provenance() -> None:
    nct_id = "NCT99000076"
    nodoc_rows = _make_no_document_locations(nct_id)

    # 1a. protocol=True, sap=False with document_profile=None -> rejected
    study_proto = _make_ctgov_study(nct_id, in_audit=False, protocol=True, sap=False)
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.build_trial_bundle(
            study_proto,
            shards=8,
            document_profile=None,
            decision_rule_locations=nodoc_rows,
        )

    # 1b. protocol=False, sap=True with document_profile=None -> rejected
    study_sap = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=True)
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.build_trial_bundle(
            study_sap,
            shards=8,
            document_profile=None,
            decision_rule_locations=nodoc_rows,
        )

    # 1c. protocol=False, sap=False masked over CT.gov largeDocs hasProtocol/hasSap -> rejected
    study_large_proto = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)
    study_large_proto["documentSection"] = {
        "largeDocumentModule": {
            "largeDocs": [{"hasProtocol": True, "hasSap": False, "filename": "Prot_000.pdf"}]
        }
    }
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.build_trial_bundle(
            study_large_proto,
            shards=8,
            document_profile=None,
            decision_rule_locations=nodoc_rows,
        )

    study_large_sap = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)
    study_large_sap["documentSection"] = {
        "largeDocumentModule": {
            "largeDocs": [{"hasProtocol": False, "hasSap": True, "filename": "SAP_000.pdf"}]
        }
    }
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.build_trial_bundle(
            study_large_sap,
            shards=8,
            document_profile=None,
            decision_rule_locations=nodoc_rows,
        )

    # 1d. protocol=False, sap=False with longitudinal_provenance posted_documents -> rejected
    study_prov = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)
    with pytest.raises(ol.OutcomesLayoutError, match="NO_DOCUMENT"):
        ol.build_trial_bundle(
            study_prov,
            shards=8,
            document_profile=None,
            decision_rule_locations=nodoc_rows,
            longitudinal_provenance=_make_module4_provenance(nct_id, with_pdf_scraped=False),
        )

    # 1e. Genuine no-document trial (protocol=False, sap=False, no profile/provenance) -> accepted
    study_nodoc = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)
    row_out, rec_out = ol.build_trial_bundle(
        study_nodoc,
        shards=8,
        document_profile=None,
        decision_rule_locations=nodoc_rows,
        longitudinal_provenance=None,
    )
    assert rec_out["decision_rule_locations"] is not None
    assert rec_out["decision_rule_locations"]["census_status"] == "no_document"
    assert row_out["census_badge"]["census_status"] == "no_document"
    assert row_out["census_badge"]["status_counts"] == {"NO_DOCUMENT": 7}


def test_pregrouped_census_status_no_document_rejected_when_docs_posted() -> None:
    empty_nodoc_grouped: dict[str, Any] = {
        "primary_pass": [],
        "census_status": "no_document",
    }
    with pytest.raises(ol.OutcomesLayoutError, match="no_document"):
        ol.normalize_decision_rule_locations(
            empty_nodoc_grouped,
            document_profile=None,
            has_posted_documents=True,
        )

    prof = ol.normalize_document_profile(_make_doc_profile("NCT99000076"))
    with pytest.raises(ol.OutcomesLayoutError, match="no_document"):
        ol.normalize_decision_rule_locations(
            empty_nodoc_grouped,
            document_profile=prof,
            has_posted_documents=False,
        )

    with pytest.raises(ol.OutcomesLayoutError, match="explicit bool"):
        ol.normalize_decision_rule_locations(
            empty_nodoc_grouped,
            has_posted_documents=1,  # type: ignore[arg-type]
        )

    allowed = ol.normalize_decision_rule_locations(
        empty_nodoc_grouped,
        document_profile=None,
        has_posted_documents=False,
    )
    assert allowed is not None
    assert allowed["census_status"] == "no_document"


def test_cli_no_document_manifest_and_trials_flag_merge_guards(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    locs_path = tmp_path / "locations.jsonl"
    locs_path.write_text(
        "\n".join(json.dumps(r) for r in _make_no_document_locations(nct_id)) + "\n",
        encoding="utf-8",
    )

    # Case 2a: --cohort-manifest only (without --trials) with protocol=True -> exit 1, out_dir untouched
    manifest_only_path = tmp_path / "manifest_proto_true.jsonl"
    manifest_only_path.write_text(
        json.dumps({
            "nct_id": nct_id,
            "brief_title": f"Trial {nct_id}",
            "in_audit": False,
            "cohorts": ["industry_phase3"],
            "protocol": True,
            "sap": False,
        })
        + "\n",
        encoding="utf-8",
    )
    out_2a = tmp_path / "out_manifest_proto_true"
    rc_2a = cli.main([
        "--cohort-manifest", str(manifest_only_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_2a),
    ])
    assert rc_2a == 1
    assert not out_2a.exists()
    assert not (tmp_path / "out_manifest_proto_true.partial").exists()

    # Case 2b: --trials protocol=False, sap=False + --cohort-manifest sap=True -> exit 1, out_dir untouched
    trials_false_path = tmp_path / "trials_false.json"
    trials_false_path.write_text(
        json.dumps([_make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)]),
        encoding="utf-8",
    )
    manifest_sap_true_path = tmp_path / "manifest_sap_true.jsonl"
    manifest_sap_true_path.write_text(
        json.dumps({
            "nct_id": nct_id,
            "in_audit": False,
            "protocol": False,
            "sap": True,
        })
        + "\n",
        encoding="utf-8",
    )
    out_2b = tmp_path / "out_trials_false_manifest_sap_true"
    rc_2b = cli.main([
        "--trials", str(trials_false_path),
        "--cohort-manifest", str(manifest_sap_true_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_2b),
    ])
    assert rc_2b == 1
    assert not out_2b.exists()
    assert not (tmp_path / "out_trials_false_manifest_sap_true.partial").exists()

    # Case 2c: asymmetric merge --trials protocol=True + --cohort-manifest protocol=False -> exit 1
    trials_proto_true_path = tmp_path / "trials_proto_true.json"
    trials_proto_true_path.write_text(
        json.dumps([_make_ctgov_study(nct_id, in_audit=False, protocol=True, sap=False)]),
        encoding="utf-8",
    )
    manifest_false_path = tmp_path / "manifest_false.jsonl"
    manifest_false_path.write_text(
        json.dumps({
            "nct_id": nct_id,
            "in_audit": False,
            "protocol": False,
            "sap": False,
        })
        + "\n",
        encoding="utf-8",
    )
    out_2c = tmp_path / "out_trials_true_manifest_false"
    rc_2c = cli.main([
        "--trials", str(trials_proto_true_path),
        "--cohort-manifest", str(manifest_false_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_2c),
    ])
    assert rc_2c == 1
    assert not out_2c.exists()
    assert not (tmp_path / "out_trials_true_manifest_false.partial").exists()

    # Case 2d: both --trials and --cohort-manifest have protocol=False, sap=False -> exit 0
    out_2d = tmp_path / "out_both_false"
    rc_2d = cli.main([
        "--trials", str(trials_false_path),
        "--cohort-manifest", str(manifest_false_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_2d),
    ])
    assert rc_2d == 0
    assert cli.main(["--check", str(out_2d)]) == 0
    idx_doc = json.loads((out_2d / "index.json").read_text(encoding="utf-8"))
    assert idx_doc["data"][0]["protocol"] is False
    assert idx_doc["data"][0]["sap"] is False
    assert idx_doc["data"][0]["census_badge"]["census_status"] == "no_document"


def test_validate_outcomes_tree_catches_tampered_protocol_on_no_document_trial(
    tmp_path: pathlib.Path,
) -> None:
    nct_id = "NCT99000076"
    study = _make_ctgov_study(nct_id, in_audit=False, protocol=False, sap=False)
    nodoc_rows = _make_no_document_locations(nct_id)
    bundle = ol.build_trial_bundle(
        study,
        shards=8,
        document_profile=None,
        decision_rule_locations=nodoc_rows,
        longitudinal_provenance=None,
    )
    idx_doc, shard_docs = ol.build_outcomes_documents(
        [bundle],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=8,
    )
    out_dir = tmp_path / "valid_nodoc_tree"
    write_res = ol.write_outcomes_tree(out_dir, idx_doc, shard_docs)
    assert write_res.ok
    assert cli.main(["--check", str(out_dir)]) == 0

    # Tamper index.json on disk: flip protocol to True while shard still carries NO_DOCUMENT
    tampered_idx = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    tampered_idx["data"][0]["protocol"] = True
    (out_dir / "index.json").write_text(
        ol.encode_json_text(tampered_idx, trailing_newline=True),
        encoding="utf-8",
    )

    check_res = ol.validate_outcomes_tree(out_dir)
    assert not check_res.ok
    assert any("NO_DOCUMENT" in p and "forbidden" in p for p in check_res.problems)
    assert any("no_document" in p and "forbidden" in p for p in check_res.problems)
    assert cli.main(["--check", str(out_dir)]) == 1

    # Restore protocol=False and tamper shard longitudinal_provenance.posted_documents -> also caught
    tampered_idx["data"][0]["protocol"] = False
    (out_dir / "index.json").write_text(
        ol.encode_json_text(tampered_idx, trailing_newline=True),
        encoding="utf-8",
    )
    assert ol.validate_outcomes_tree(out_dir).ok

    shard_id = ol.outcomes_shard_of(nct_id, 8)
    shard_path = out_dir / "shards" / f"{shard_id}.json.gz"
    sdoc = json.loads(gzip.decompress(shard_path.read_bytes()).decode("utf-8"))
    sdoc["data"][nct_id]["longitudinal_provenance"] = ol.normalize_longitudinal_provenance(
        _make_module4_provenance(nct_id, with_pdf_scraped=False),
        in_audit=False,
    )
    shard_path.write_bytes(ol.encode_gz_bytes(sdoc))
    prov_tamper_res = ol.validate_outcomes_tree(out_dir)
    assert not prov_tamper_res.ok
    assert any("NO_DOCUMENT" in p and "forbidden" in p for p in prov_tamper_res.problems)
    assert cli.main(["--check", str(out_dir)]) == 1


def test_validate_outcomes_tree_catches_on_disk_citation_status_tampering(
    tmp_path: pathlib.Path,
) -> None:
    """C7: on-disk validation catches missing citations on NOT_APPLICABLE, forbidden
    citations on NOT_FOUND, and non-list citations across all passes + effective_pass.
    """
    nct_id = "NCT99000076"
    study = _make_ctgov_study(nct_id, in_audit=False)
    prof = _make_doc_profile(nct_id)
    loc_rows = _make_complete_locations(nct_id, include_second_pass=True)
    for r in loc_rows:
        if r["pass"] == "primary" and r["element"] == "margin":
            r["status"] = "NOT_APPLICABLE"
            r["citations"] = [_make_citation(quotation="Superiority design; no non-inferiority margin.")]
        elif r["pass"] == "primary" and r["element"] == "interim_boundary":
            r["status"] = "NOT_FOUND"
            r["citations"] = []

    bundle = ol.build_trial_bundle(
        study,
        shards=8,
        document_profile=prof,
        decision_rule_locations=loc_rows,
    )
    idx_doc, shard_docs = ol.build_outcomes_documents(
        [bundle],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=8,
    )
    out_dir = tmp_path / "citation_tamper"
    assert ol.write_outcomes_tree(out_dir, idx_doc, shard_docs).ok
    assert cli.main(["--check", str(out_dir)]) == 0

    shard_id = ol.outcomes_shard_of(nct_id, 8)
    shard_path = out_dir / "shards" / f"{shard_id}.json.gz"
    clean_sdoc = json.loads(gzip.decompress(shard_path.read_bytes()).decode("utf-8"))

    # Tamper 1: clear citations on NOT_APPLICABLE in primary_pass + effective_pass
    tamper_na = copy.deepcopy(clean_sdoc)
    locs_na = tamper_na["data"][nct_id]["decision_rule_locations"]
    for bucket_name in ("primary_pass", "effective_pass"):
        for row in locs_na[bucket_name]:
            if row["element"] == "margin":
                row["citations"] = []
    shard_path.write_bytes(ol.encode_gz_bytes(tamper_na))
    res_na = ol.validate_outcomes_tree(out_dir)
    assert not res_na.ok
    assert any("primary_pass" in p and "NOT_APPLICABLE" in p for p in res_na.problems)
    assert any("effective_pass" in p and "NOT_APPLICABLE" in p for p in res_na.problems)
    assert cli.main(["--check", str(out_dir)]) == 1

    # Tamper 2: inject a citation onto NOT_FOUND in primary_pass + effective_pass
    tamper_nf = copy.deepcopy(clean_sdoc)
    locs_nf = tamper_nf["data"][nct_id]["decision_rule_locations"]
    for bucket_name in ("primary_pass", "effective_pass"):
        for row in locs_nf[bucket_name]:
            if row["element"] == "interim_boundary":
                row["citations"] = [_make_citation()]
    shard_path.write_bytes(ol.encode_gz_bytes(tamper_nf))
    res_nf = ol.validate_outcomes_tree(out_dir)
    assert not res_nf.ok
    assert any("primary_pass" in p and "NOT_FOUND" in p for p in res_nf.problems)
    assert any("effective_pass" in p and "NOT_FOUND" in p for p in res_nf.problems)

    # Tamper 3: non-list citations in second_pass
    tamper_type = copy.deepcopy(clean_sdoc)
    tamper_type["data"][nct_id]["decision_rule_locations"]["second_pass"][0]["citations"] = "invalid"
    shard_path.write_bytes(ol.encode_gz_bytes(tamper_type))
    res_type = ol.validate_outcomes_tree(out_dir)
    assert not res_type.ok
    assert any("second_pass[0].citations must be a list" in p for p in res_type.problems)


def test_module4_longitudinal_provenance_adapter() -> None:
    prov = _make_module4_provenance("NCT99000076", with_pdf_scraped=True)
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
        ("v3_forbidden_key_ocr_recovery_pass", {"in_audit": True, "raw_payload": {"ocr_recovery_pass": []}}),
        ("v3_forbidden_key_adjudication_pass", {"in_audit": True, "raw_payload": {"adjudication_pass": []}}),
        ("v3_forbidden_key_effective_pass", {"in_audit": True, "raw_payload": {"effective_pass": []}}),
        (
            "v3_extra_shard_key",
            {
                "in_audit": True,
                "shard_record": {
                    "nct_id": "NCT99000058",
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
                "longitudinal_provenance": _make_module4_provenance("NCT99000058", with_pdf_scraped=True),
            },
        ),
        (
            "v5_bad_census_badge_status",
            {
                "in_audit": True,
                "index_row": {
                    "nct_id": "NCT99000058",
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
                    "nct_id": "NCT99000058",
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
        ol.assert_no_audit_leakage(nct_id="NCT99000058", **kwargs)
    # AuditFirewallError must also be a ValueError.
    with pytest.raises(ValueError):
        ol.assert_no_audit_leakage(nct_id="NCT99000058", **kwargs)


def test_in_audit_ctgov_study_with_native_contacts_locations_is_allowed() -> None:
    """Native CT.gov contactsLocationsModule.locations must not false-positive."""
    study = _make_ctgov_study("NCT99000058", in_audit=True, include_native_locations=True)
    prov_clean = _make_module4_provenance("NCT99000058", with_pdf_scraped=False)
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
            _make_ctgov_study("NCT99000076", in_audit=False),
            _make_ctgov_study("NCT99000058", in_audit=True),
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
            "\n".join(json.dumps(r) for r in _make_complete_locations("NCT99000058")) + "\n",
            encoding="utf-8",
        )
        argv += ["--locations", str(loc_path)]
    else:
        doc_path = tmp_path / "documents.jsonl"
        doc_path.write_text(json.dumps(_make_doc_profile("NCT99000058")) + "\n", encoding="utf-8")
        argv += ["--documents", str(doc_path)]

    rc = cli.main(argv)
    assert rc == 2
    assert not out_dir.exists(), "Vector 6 must abort BEFORE creating out_dir"
    assert not (tmp_path / "outcomes_target.partial").exists()


# ── 7. On-disk tamper detection & provenance validation ──────────────────────

def test_validate_outcomes_tree_detects_tampering_and_strict_commit(tmp_path: pathlib.Path) -> None:
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    s2 = _make_ctgov_study("NCT99000058", in_audit=True)
    bundles = [
        ol.build_trial_bundle(s1, shards=8, document_profile=_make_doc_profile("NCT99000076")),
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
    audit_shard = ol.outcomes_shard_of("NCT99000058", 8)
    sp = leak_case / "shards" / f"{audit_shard}.json.gz"
    sdoc = json.loads(gzip.decompress(sp.read_bytes()).decode("utf-8"))
    sdoc["data"]["NCT99000058"]["decision_rule_locations"] = {"leaked": True}
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
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
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


def test_write_outcomes_tree_restores_v1_shards_when_index_swap_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "outcomes"
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx_v1, shards_v1)
    (out / "unrelated_note.txt").write_text("keep me", encoding="utf-8")

    v1_index_bytes = (out / "index.json").read_bytes()
    v1_shard_bytes = {
        n: (out / "shards" / f"{n}.json.gz").read_bytes()
        for n in range(4)
    }

    s2 = _make_ctgov_study("NCT99000060", in_audit=False, sponsor_class="NIH", phase="PHASE2")
    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4), ol.build_trial_bundle(s2, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )

    real_replace = os.replace
    saw_index_swap = False

    def _fail_on_index_swap(src: Any, dst: Any) -> None:
        nonlocal saw_index_swap
        if pathlib.Path(dst).name == ol.INDEX_FILE:
            saw_index_swap = True
            raise OSError("simulated index.json swap failure")
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _fail_on_index_swap)
    with pytest.raises(OSError, match="simulated index.json swap failure"):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)

    assert saw_index_swap is True
    assert (out / "index.json").read_bytes() == v1_index_bytes
    for n in range(4):
        assert (out / "shards" / f"{n}.json.gz").read_bytes() == v1_shard_bytes[n]
    assert (out / "unrelated_note.txt").read_text(encoding="utf-8") == "keep me"
    assert ol.validate_outcomes_tree(out).ok is True
    assert not (tmp_path / "outcomes.partial").exists()


def test_write_outcomes_tree_first_time_index_swap_failure_leaves_no_orphan_shards(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "fresh_outcomes"
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    idx, shards = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )

    real_replace = os.replace

    def _fail_on_index_swap(src: Any, dst: Any) -> None:
        if pathlib.Path(dst).name == ol.INDEX_FILE:
            raise OSError("simulated first-write index.json swap failure")
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _fail_on_index_swap)
    with pytest.raises(OSError, match="simulated first-write index.json swap failure"):
        ol.write_outcomes_tree(out, idx, shards)

    assert not (out / "shards").exists()
    assert not (out / "index.json").exists()
    assert not out.exists()
    assert not (tmp_path / "fresh_outcomes.partial").exists()


def test_write_outcomes_tree_preserves_backup_if_rollback_restore_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "outcomes_keep_backup"
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx_v1, shards_v1)
    v1_shard0 = (out / "shards" / "0.json.gz").read_bytes()

    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )

    real_replace = os.replace

    def _fail_index_and_restore(src: Any, dst: Any) -> None:
        src_p = pathlib.Path(src)
        dst_p = pathlib.Path(dst)
        if dst_p.name == ol.INDEX_FILE:
            raise OSError("simulated index swap failure")
        if src_p.name == "_previous_shards":
            raise OSError("simulated backup restore failure")
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _fail_index_and_restore)
    with pytest.raises(OSError, match="simulated backup restore failure"):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)

    backup_dir = tmp_path / "outcomes_keep_backup.partial" / "_previous_shards"
    assert backup_dir.is_dir(), "work/.partial must NOT be deleted when _previous_shards restore failed"
    assert (backup_dir / "0.json.gz").read_bytes() == v1_shard0


def test_write_outcomes_tree_cleans_partial_after_consecutive_overwrites(
    tmp_path: pathlib.Path,
) -> None:
    """C9: two consecutive successful writes clean up .partial and _previous_shards."""
    out = tmp_path / "outcomes_bundles"
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    s2 = _make_ctgov_study("NCT99000060", in_audit=False, sponsor_class="NIH", phase="PHASE2")

    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    res1 = ol.write_outcomes_tree(out, idx_v1, shards_v1)
    assert res1.ok
    assert not pathlib.Path(f"{out}.partial").exists()

    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4), ol.build_trial_bundle(s2, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    res2 = ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert res2.ok
    assert not pathlib.Path(f"{out}.partial").exists()
    assert ol.validate_outcomes_tree(out).ok
    live_idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert live_idx["total_trials"] == 2


def test_write_outcomes_tree_refuses_foreign_data_directories(
    tmp_path: pathlib.Path,
) -> None:
    """Refuse to overwrite live data/outcomes/, data/results_structure/, or data/."""
    assert ol.DEFAULT_OUT_DIR_NAME == "outcomes_bundles"
    assert cli.DEFAULT_OUT_DIR == os.path.join("data", "outcomes_bundles")

    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    idx, shards = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )

    # Case 1: live outcomes_beta directory (run.json, summary.json, records/r00.json, rows index.json)
    live_outcomes = tmp_path / "outcomes"
    (live_outcomes / "records").mkdir(parents=True)
    run_bytes = b'{"run_id":"outcomes-2026-11-09"}\n'
    summary_bytes = b'{"schema_version":1}\n'
    rec_bytes = b'{"records":{}}\n'
    beta_index_bytes = b'{"run_id":"outcomes-2026-11-09","schema_version":1,"rows":[]}\n'
    (live_outcomes / "run.json").write_bytes(run_bytes)
    (live_outcomes / "summary.json").write_bytes(summary_bytes)
    (live_outcomes / "records" / "r00.json").write_bytes(rec_bytes)
    (live_outcomes / "index.json").write_bytes(beta_index_bytes)

    with pytest.raises(ol.OutcomesLayoutError, match="foreign data directory"):
        ol.write_outcomes_tree(live_outcomes, idx, shards)
    assert not pathlib.Path(f"{live_outcomes}.partial").exists()
    assert (live_outcomes / "run.json").read_bytes() == run_bytes
    assert (live_outcomes / "summary.json").read_bytes() == summary_bytes
    assert (live_outcomes / "records" / "r00.json").read_bytes() == rec_bytes
    assert (live_outcomes / "index.json").read_bytes() == beta_index_bytes

    # Case 2: directory with ONLY an outcomes_beta index.json ({"rows": [...]})
    beta_idx_only = tmp_path / "beta_idx_only"
    beta_idx_only.mkdir()
    (beta_idx_only / "index.json").write_bytes(beta_index_bytes)
    with pytest.raises(ol.OutcomesLayoutError, match="foreign directory"):
        ol.write_outcomes_tree(beta_idx_only, idx, shards)
    assert not pathlib.Path(f"{beta_idx_only}.partial").exists()
    assert (beta_idx_only / "index.json").read_bytes() == beta_index_bytes

    # Case 3: results_structure directory (records.json)
    rs_dir = tmp_path / "results_structure"
    rs_dir.mkdir()
    (rs_dir / "records.json").write_bytes(b'{"records":[]}\n')
    with pytest.raises(ol.OutcomesLayoutError, match="foreign data directory"):
        ol.write_outcomes_tree(rs_dir, idx, shards)
    assert not pathlib.Path(f"{rs_dir}.partial").exists()
    assert (rs_dir / "records.json").read_bytes() == b'{"records":[]}\n'


def test_cli_builds_from_outcomes_beta_index_manifest(tmp_path: pathlib.Path) -> None:
    """build_outcomes_shards.py reads live outcomes_beta index.json ('rows',
    'n_primary_outcomes', 'title', 'sponsor') cleanly.
    """
    beta_index = {
        "run_id": "outcomes-2026-11-09-d41f32ec4d",
        "schema_version": 1,
        "columns_note": "One row per eligible trial; see run.json for definitions.",
        "rows": [
            {
                "nct_id": "NCT99000076",
                "title": "Beta Phase 3 Trial of Drug X",
                "sponsor": "Beta Pharma Inc.",
                "sponsor_state": "attributed",
                "recode_differs": False,
                "area": "Oncology",
                "area_secondary": None,
                "primary_objective": "Overall Survival",
                "n_primary_outcomes": 2,
                "results_state": "POSTED",
                "results_sub_reason": None,
                "results_first_post_date": "2023-01-10",
                "protocol": True,
                "sap": True,
                "in_audit": False,
                "assessment_kind": None,
                "trial_state": None,
                "review_status": None,
                "in_demographics": True,
                "record": "records/r00.json",
            },
            {
                "nct_id": "NCT99000058",
                "title": "Holdout Beta Trial",
                "sponsor": "Holdout Therapeutics",
                "n_primary_outcomes": 1,
                "results_first_post_date": "2022-05-01",
                "protocol": True,
                "sap": False,
                "in_audit": True,
            },
        ],
        "excluded": [],
    }
    manifest_path = tmp_path / "beta_index.json"
    manifest_path.write_text(json.dumps(beta_index), encoding="utf-8")

    # Provide locations only for outcome 0 on NCT99000076 (which has n_primary_outcomes=2)
    # so census_status resolves to "partial" rather than "complete".
    locs_path = tmp_path / "locations.jsonl"
    locs_path.write_text(
        "\n".join(json.dumps(r) for r in _make_complete_locations("NCT99000076")) + "\n",
        encoding="utf-8",
    )
    docs_path = tmp_path / "docs.jsonl"
    docs_path.write_text(json.dumps(_make_doc_profile("NCT99000076")) + "\n", encoding="utf-8")

    out_dir = tmp_path / "outcomes_bundles"
    rc = cli.main([
        "--cohort-manifest", str(manifest_path),
        "--documents", str(docs_path),
        "--locations", str(locs_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    assert cli.main(["--check", str(out_dir)]) == 0
    assert not pathlib.Path(f"{out_dir}.partial").exists()

    idx_doc = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    by_id = {row["nct_id"]: row for row in idx_doc["data"]}
    assert by_id["NCT99000076"]["brief_title"] == "Beta Phase 3 Trial of Drug X"
    assert by_id["NCT99000076"]["lead_sponsor_name"] == "Beta Pharma Inc."
    assert by_id["NCT99000076"]["primary_outcomes_count"] == 2
    assert by_id["NCT99000076"]["cohorts"] == ["all_large_docs"]
    assert by_id["NCT99000076"]["census_badge"]["census_status"] == "partial"

    assert by_id["NCT99000058"]["brief_title"] == "Holdout Beta Trial"
    assert by_id["NCT99000058"]["lead_sponsor_name"] == "Holdout Therapeutics"
    assert by_id["NCT99000058"]["primary_outcomes_count"] == 1
    assert by_id["NCT99000058"]["in_audit"] is True
    assert by_id["NCT99000058"]["document_badge"]["readability_status"] == "in_audit_holdout"
    assert by_id["NCT99000058"]["census_badge"]["census_status"] == "in_audit_holdout"


def test_dataset_folder_present_files_ignores_outcomes_bundles_directory(tmp_path: pathlib.Path) -> None:
    site_data = tmp_path / "data"
    site_data.mkdir()
    (site_data / "demographics.part1.json.gz").write_bytes(b"stub")
    (site_data / "run.json").write_text("{}", encoding="utf-8")

    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    idx, shards = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=8)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=8,
    )
    ol.write_outcomes_tree(site_data / ol.DEFAULT_OUT_DIR_NAME, idx, shards)

    present = df.present_files(str(site_data))
    assert present == ["demographics.part1.json.gz", "run.json"]
    assert not any("outcomes_bundles" in name for name in present)


def test_cli_end_to_end_and_graceful_git_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(
        json.dumps([
            _make_ctgov_study("NCT99000076", in_audit=False),
            _make_ctgov_study("NCT99000058", in_audit=True),
        ]),
        encoding="utf-8",
    )
    docs_path = tmp_path / "profiles.jsonl"
    docs_path.write_text(json.dumps(_make_doc_profile("NCT99000076")) + "\n", encoding="utf-8")
    locs_path = tmp_path / "locations.jsonl"
    locs_path.write_text(
        "\n".join(json.dumps(r) for r in _make_complete_locations("NCT99000076")) + "\n",
        encoding="utf-8",
    )
    prov_path = tmp_path / "provenance.jsonl"
    prov_path.write_text(
        json.dumps(_make_module4_provenance("NCT99000076", with_pdf_scraped=True)) + "\n",
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


# ── 8b. Module 4 provenance projection feeding the builder ───────────────────

def _m4_sample_study(nct_id: str) -> dict[str, Any]:
    """The CT.gov study of tests/test_longitudinal_provenance.py::_sample_study, copied."""
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "statusModule": {
                "studyFirstPostDateStruct": {"date": "2018-01-10", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2020-05-01", "type": "ACTUAL"},
                "resultsFirstPostDateStruct": {"date": "2020-11-01", "type": "ACTUAL"},
                "lastUpdatePostDateStruct": {"date": "2021-06-15", "type": "ACTUAL"},
            },
            "referencesModule": {
                "references": [
                    {
                        "pmid": "34000001",
                        "type": "RESULT",
                        "citation": "Smith J, et al. Lancet. 2021; doi: 10.1016/S0140-6736(21)00001-X.",
                    },
                    {
                        "pmid": "31000009",
                        "type": "BACKGROUND",
                        "citation": "Prior phase 2 study. J Clin Oncol. 2019.",
                    },
                    {
                        "pmid": "34555555",
                        "type": "DERIVED",
                        "citation": "Auto-linked secondary manuscript. 2022.",
                    },
                ]
            },
        },
        "resultsSection": {
            "moreInfoModule": {
                "citations": [
                    {
                        "pmid": "34000001",
                        "citation": "Smith J, et al. Lancet. 2021.",
                    }
                ]
            }
        },
        "documentSection": {
            "largeDocumentModule": {
                "largeDocs": [
                    {
                        "filename": "Prot_000.pdf",
                        "label": "Study Protocol",
                        "hasProtocol": True,
                        "hasSap": False,
                        "date": "2019-06-12",
                        "uploadDate": "08/21/2020 14:32",
                    },
                    {
                        "filename": "SAP_001.pdf",
                        "label": "Statistical Analysis Plan",
                        "hasProtocol": False,
                        "hasSap": True,
                        "date": "2020-11-27",
                        "uploadDate": "12/01/2020 09:15",
                    },
                    {
                        "filename": "ICF_002.pdf",
                        "label": "Informed Consent Form",
                        "hasProtocol": False,
                        "hasSap": False,
                        "date": "2018-01-01",
                        "uploadDate": "08/21/2020 14:35",
                    },
                ]
            }
        },
    }


def test_to_outcomes_layout_provenance_feeds_build_outcomes_shards(tmp_path: pathlib.Path) -> None:
    """Moved from tests/test_longitudinal_provenance.py (PR #97 at dc38220).

    The m4 branch carries no M3 code, so this test, which runs the M3 builder on
    Module 4's provenance projection, lives here now. What it asserts is
    unchanged: the builder accepts the projection and the tree passes --check.
    Two adaptations only: the trial id is fictional (NCT99xxxxxx), and Module 4
    now requires its firewall basis (in_scope_nct_ids and audit_nct_ids) on both
    calls.
    """
    from src.longitudinal_provenance import (
        assemble_longitudinal_provenance,
        to_outcomes_layout_provenance,
    )

    nct_id = "NCT99200001"
    scope = frozenset({nct_id})
    holdouts = frozenset({"NCT99999999"})
    study = _m4_sample_study(nct_id)
    study["protocolSection"]["identificationModule"]["briefTitle"] = "Phase 3 Example Trial"
    study["protocolSection"]["designModule"] = {
        "studyType": "INTERVENTIONAL",
        "phases": ["PHASE3"],
        "enrollmentInfo": {"count": 120, "type": "ACTUAL"},
    }
    study["protocolSection"]["sponsorCollaboratorsModule"] = {
        "leadSponsor": {"name": "Example Pharma", "class": "INDUSTRY"}
    }
    study["protocolSection"]["outcomesModule"] = {
        "primaryOutcomes": [{"measure": "Overall Survival", "timeFrame": "24 months"}]
    }

    record = assemble_longitudinal_provenance(
        study,
        in_scope_nct_ids=scope,
        audit_nct_ids=holdouts,
        assembled_at="2026-11-20T12:00:00Z",
        source_pipeline_commit="5b55d47",
    )
    layout_proj = to_outcomes_layout_provenance(
        record, in_scope_nct_ids=scope, audit_nct_ids=holdouts
    )

    trials_path = tmp_path / "trials.json"
    manifest_path = tmp_path / "manifest.json"
    prov_path = tmp_path / "provenance.jsonl"
    out_dir = tmp_path / "outcomes"

    trials_path.write_text(json.dumps([study]), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "nct_id": nct_id,
                    "in_audit": False,
                    "cohorts": ["industry_phase3"],
                    "protocol": True,
                    "sap": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    prov_path.write_text(json.dumps(layout_proj) + "\n", encoding="utf-8")

    rc_build = cli.main(
        [
            "--trials",
            str(trials_path),
            "--cohort-manifest",
            str(manifest_path),
            "--provenance",
            str(prov_path),
            "--extracted-at",
            "2026-11-20T12:00:00Z",
            "--source-pipeline-commit",
            "5b55d47",
            "--shards",
            "4",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc_build == 0
    assert cli.main(["--check", str(out_dir)]) == 0


# ── 9. .gitignore coverage & Python 3.11 AST / typing gate ───────────────────

def test_gitignore_covers_transient_outcomes_and_cache_paths() -> None:
    # Only the paths this module writes. The document indexer's caches and the
    # census PDFs are other modules' paths, ignored on their own branches.
    expected_entries = [
        "data/outcomes_bundles/",
        "data/outcomes_bundles.partial/",
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
            "data/outcomes_bundles/index.json",
            "data/outcomes_bundles.partial/index.json",
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
