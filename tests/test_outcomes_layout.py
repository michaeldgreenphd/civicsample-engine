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
10. Hardening, each test failing on the dc38220 code: Module 4's citation rules
    on input and on disk (F5), unknown pass sections refused (F4), the output
    allowlist (L1), engine-rooted defaults and commit and a guarded report
    (L2), interrupt-safe swap and retained-backup refusal (L3), recomputed
    census status and no citation without a document (L4), the manifest as
    allowlist with strict IDs and holdout stubs (L5), repeated options (L6),
    and the live outcomes index shape (L7).

Every trial ID is fictional (NCT99xxxxxx).
"""
from __future__ import annotations

import ast
import collections
import contextlib
import copy
import dataclasses
import gzip
import hashlib
import io
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


def _write_manifest(path: pathlib.Path, studies: list[dict[str, Any]]) -> pathlib.Path:
    """Write a --cohort-manifest listing each study's nct_id and in_audit (JSONL)."""
    path.write_text(
        "".join(
            json.dumps({"nct_id": s["nct_id"], "in_audit": s["in_audit"]}) + "\n" for s in studies
        ),
        encoding="utf-8",
    )
    return path


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
    manifest_path = _write_manifest(tmp_path / "manifest.jsonl", [study])

    rc = cli.main([
        "--trials", str(trials_path),
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

    # L4: a supplied census_status is recomputed, never trusted. An empty
    # primary_pass computes to "not_started", so its "no_document" claim is
    # refused even with no documents posted (it was accepted before); a trial
    # with no documents records that with NO_DOCUMENT rows.
    with pytest.raises(ol.OutcomesLayoutError, match="never trusted"):
        ol.normalize_decision_rule_locations(
            empty_nodoc_grouped,
            document_profile=None,
            has_posted_documents=False,
        )


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
    row, rec = ol.build_trial_bundle(study, shards=16)
    assert row["in_audit"] is True
    assert row["document_badge"]["readability_status"] == "in_audit_holdout"
    assert row["census_badge"]["census_status"] == "in_audit_holdout"
    assert rec["document_profile"] is None
    assert rec["decision_rule_locations"] is None
    # L5: a holdout is a stub, so even PDF-free provenance for it is refused
    # (it was accepted, and written, before).
    prov_clean = _make_module4_provenance("NCT99000058", with_pdf_scraped=False)
    with pytest.raises(ol.AuditFirewallError):
        ol.build_trial_bundle(study, shards=16, longitudinal_provenance=prov_clean)


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
    manifest_path = _write_manifest(
        tmp_path / "manifest.jsonl",
        [
            _make_ctgov_study("NCT99000076", in_audit=False),
            _make_ctgov_study("NCT99000058", in_audit=True),
        ],
    )
    argv = [
        "--trials",
        str(trials_path),
        "--cohort-manifest",
        str(manifest_path),
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
    # L3: the failure is reported as RetainedBackupError (it was the bare
    # OSError of the failed restore); the message still carries that OSError's
    # text, and now also names where the previous shards are kept.
    with pytest.raises(ol.RetainedBackupError, match="simulated backup restore failure") as excinfo:
        ol.write_outcomes_tree(out, idx_v2, shards_v2)

    backup_dir = tmp_path / "outcomes_keep_backup.partial" / "_previous_shards"
    assert str(backup_dir) in str(excinfo.value)
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
    # L2: the default is resolved from the engine root, not the working directory.
    assert cli.DEFAULT_OUT_DIR == str(ROOT / "data" / "outcomes_bundles")

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
    # L1: the builder no longer writes under a dataset folder (its ancestor
    # holds run.json), so the bundle is built beside it and copied in to check
    # that present_files still ignores one placed there.
    with pytest.raises(ol.OutcomesLayoutError, match="ancestor"):
        ol.write_outcomes_tree(site_data / ol.DEFAULT_OUT_DIR_NAME, idx, shards)
    assert not (site_data / ol.DEFAULT_OUT_DIR_NAME).exists()
    built = tmp_path / "built" / ol.DEFAULT_OUT_DIR_NAME
    assert ol.write_outcomes_tree(built, idx, shards).ok
    import shutil

    shutil.copytree(built, site_data / ol.DEFAULT_OUT_DIR_NAME)

    present = df.present_files(str(site_data))
    assert present == ["demographics.part1.json.gz", "run.json"]
    assert not any("outcomes_bundles" in name for name in present)


def test_cli_end_to_end_and_unresolvable_commit_refused(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2: with git unavailable and no --pipeline-commit the build is refused and
    nothing is written (before, it wrote source_pipeline_commit=null and exited
    0); an explicit --pipeline-commit still builds a tree that passes --check.
    """
    studies = [
        _make_ctgov_study("NCT99000076", in_audit=False),
        _make_ctgov_study("NCT99000058", in_audit=True),
    ]
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(json.dumps(studies), encoding="utf-8")
    manifest_path = _write_manifest(tmp_path / "manifest.jsonl", studies)
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
    common = [
        "--trials", str(trials_path),
        "--cohort-manifest", str(manifest_path),
        "--documents", str(docs_path),
        "--locations", str(locs_path),
        "--provenance", str(prov_path),
        "--extracted-at", SAMPLE_TS,
        "--shards", "8",
    ]

    # Simulate git unavailable inside sandbox; GITHUB_SHA is not consulted.
    monkeypatch.setenv("GITHUB_SHA", SAMPLE_COMMIT)
    real_run = ol.subprocess.run
    monkeypatch.setattr(
        ol.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("git blocked in sandbox")),
    )
    out_fallback = tmp_path / "out_fallback"
    rc_fallback = cli.main([*common, "--out-dir", str(out_fallback), "--report", str(report_path)])
    assert rc_fallback == 1
    assert not out_fallback.exists()
    assert not pathlib.Path(f"{out_fallback}.partial").exists()
    assert not report_path.exists()
    monkeypatch.setattr(ol.subprocess, "run", real_run)

    # Explicit --pipeline-commit satisfies strict --check (exit 0).
    out_strict = tmp_path / "out_strict"
    rc_strict = cli.main([
        *common,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--out-dir", str(out_strict),
        "--report", str(report_path),
    ])
    assert rc_strict == 0
    assert cli.main(["--check", str(out_strict)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["total_trials"] == 2
    assert report["audit_holdout_trials"] == 1
    idx_doc = json.loads((out_strict / "index.json").read_text(encoding="utf-8"))
    assert idx_doc["source_pipeline_commit"] == SAMPLE_COMMIT


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


# ── 10. Hardening: F4, F5, L1-L7 (each fails on the dc38220 code) ────────────

def _valid_tree(
    out: pathlib.Path,
    *,
    nct_id: str = "NCT99000076",
    loc_rows: list[dict[str, Any]] | None = None,
    shards: int = 8,
) -> pathlib.Path:
    """Write a valid one-trial tree (documents + locations) with the library."""
    study = _make_ctgov_study(nct_id, in_audit=False)
    bundle = ol.build_trial_bundle(
        study,
        shards=shards,
        document_profile=_make_doc_profile(nct_id),
        decision_rule_locations=loc_rows if loc_rows is not None else _make_complete_locations(nct_id),
    )
    idx, shard_docs = ol.build_outcomes_documents(
        [bundle], extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=shards
    )
    assert ol.write_outcomes_tree(out, idx, shard_docs).ok
    return out


def _read_shard(out: pathlib.Path, nct_id: str, shards: int = 8) -> tuple[pathlib.Path, dict[str, Any]]:
    """Return ``(path, decoded shard document)`` for the shard holding ``nct_id``."""
    path = out / "shards" / f"{ol.outcomes_shard_of(nct_id, shards)}.json.gz"
    return path, json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _cli_build_args(
    tmp_path: pathlib.Path,
    studies: list[dict[str, Any]],
    out_dir: pathlib.Path,
    *,
    extra: list[str] | None = None,
    manifest: bool = True,
) -> list[str]:
    """argv for a CLI build of ``studies`` (trials + manifest) into ``out_dir``."""
    trials_path = tmp_path / "trials_cli.json"
    trials_path.write_text(json.dumps(studies), encoding="utf-8")
    argv = ["--trials", str(trials_path)]
    if manifest:
        argv += ["--cohort-manifest", str(_write_manifest(tmp_path / "manifest_cli.jsonl", studies))]
    argv += [
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out_dir),
    ]
    return argv + (extra or [])


# F5: one definition of the citation rules, applied on input and on disk.

def test_f5_citation_rules_are_module4_definitions() -> None:
    from src import citation_verifier as cv

    assert ol.CITATION_REQUIRED_STATUSES is cv.CITATION_REQUIRED_STATUSES
    assert ol.CITATION_FORBIDDEN_STATUSES is cv.CITATION_FORBIDDEN_STATUSES
    assert ol.validate_status_citations is cv.validate_status_citations
    assert ol.validate_citation_shape is cv.validate_citation_shape
    assert ol.validate_strict_nct_id is cv.validate_strict_nct_id


@pytest.mark.parametrize(
    "field,value,expect",
    [
        ("printed_page", 10, "printed_page"),
        ("filename", "Prot SAP 000.pdf", "pdf basename"),
        ("quotation", "Primary analysis․․․ ITT population.", "ellipsis"),
    ],
)
def test_f5_normalization_applies_module4_citation_shape(field: str, value: Any, expect: str) -> None:
    """Citations dc38220 accepted and Module 4 refuses: an int printed page, a
    filename that is not a safe .pdf basename, an ellipsis in a compatibility
    spelling (one-dot leaders)."""
    nct_id = "NCT99000076"
    raw_prof = _make_doc_profile(nct_id)
    if field == "filename":
        raw_prof["documents"][0]["filename"] = value
    prof = ol.normalize_document_profile(raw_prof)
    rows = _make_complete_locations(nct_id)
    rows[0]["citations"][0][field] = value
    with pytest.raises(ol.OutcomesLayoutError, match=expect):
        ol.normalize_decision_rule_locations(rows, document_profile=prof, primary_outcomes_count=1)


@pytest.mark.parametrize("bad", [[None], ["x"], [{}]], ids=["none", "str", "empty_object"])
def test_f5_on_disk_malformed_citations_fail_check(tmp_path: pathlib.Path, bad: list[Any]) -> None:
    nct_id = "NCT99000076"
    out = _valid_tree(tmp_path / "bundle", nct_id=nct_id)
    path, sdoc = _read_shard(out, nct_id)
    locs = sdoc["data"][nct_id]["decision_rule_locations"]
    locs["primary_pass"][0]["citations"] = copy.deepcopy(bad)
    locs["effective_pass"][0]["citations"] = copy.deepcopy(bad)
    path.write_bytes(ol.encode_gz_bytes(sdoc))
    res = ol.validate_outcomes_tree(out)
    assert not res.ok
    assert any("primary_pass[0].citations[0]" in p for p in res.problems)
    assert any("effective_pass[0].citations[0]" in p for p in res.problems)
    assert cli.main(["--check", str(out)]) == 1


# F4: unknown pass sections in a pre-grouped object are refused, not dropped.

def _primary_and_adjudication(nct_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    primary_rows = _make_complete_locations(nct_id)
    for row in primary_rows:
        if row["element"] == "margin":
            row["status"] = "AMBIGUOUS"
    adj_row = copy.deepcopy(primary_rows[4])
    adj_row["status"] = "FOUND"
    adj_row["pass"] = "adjudication"
    return primary_rows, adj_row


@pytest.mark.parametrize("unknown_key", ["ocr_pass", "adjudication", "effective_pass", "notes"])
def test_f4_pregrouped_unknown_key_refused(unknown_key: str) -> None:
    nct_id = "NCT99000076"
    prof = ol.normalize_document_profile(_make_doc_profile(nct_id))
    primary_rows, adj_row = _primary_and_adjudication(nct_id)
    grouped = {"nct_id": nct_id, "primary_pass": primary_rows, unknown_key: [adj_row]}
    with pytest.raises(ol.OutcomesLayoutError, match="unknown key"):
        ol.normalize_decision_rule_locations(grouped, document_profile=prof, primary_outcomes_count=1)


def test_f4_pregrouped_row_filed_under_another_pass_refused() -> None:
    nct_id = "NCT99000076"
    prof = ol.normalize_document_profile(_make_doc_profile(nct_id))
    primary_rows, adj_row = _primary_and_adjudication(nct_id)
    with pytest.raises(ol.OutcomesLayoutError, match="re-filed"):
        ol.normalize_decision_rule_locations(
            {"primary_pass": primary_rows[:-1] + [adj_row]},
            document_profile=prof,
            primary_outcomes_count=1,
        )


def test_f4_cli_pregrouped_misspelled_pass_is_refused(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    primary_rows, adj_row = _primary_and_adjudication(nct_id)
    locs_path = tmp_path / "grouped.jsonl"
    locs_path.write_text(
        json.dumps({"nct_id": nct_id, "primary_pass": primary_rows, "adjudication": [adj_row]}) + "\n",
        encoding="utf-8",
    )
    docs_path = tmp_path / "docs.jsonl"
    docs_path.write_text(json.dumps(_make_doc_profile(nct_id)) + "\n", encoding="utf-8")
    out = tmp_path / "bundle"
    argv = _cli_build_args(
        tmp_path,
        [_make_ctgov_study(nct_id)],
        out,
        extra=["--documents", str(docs_path), "--locations", str(locs_path)],
    )
    assert cli.main(argv) == 1
    assert not out.exists()


def test_f4_on_disk_unknown_locations_key_fails_check(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    out = _valid_tree(tmp_path / "bundle", nct_id=nct_id)
    path, sdoc = _read_shard(out, nct_id)
    sdoc["data"][nct_id]["decision_rule_locations"]["ocr_pass"] = []
    path.write_bytes(ol.encode_gz_bytes(sdoc))
    res = ol.validate_outcomes_tree(out)
    assert not res.ok
    assert any("key mismatch" in p and "ocr_pass" in p for p in res.problems)


# L4: census_status is recomputed; citations need a posted document.

def test_l4_pregrouped_census_status_is_recomputed_not_trusted() -> None:
    nct_id = "NCT99000076"
    prof = ol.normalize_document_profile(_make_doc_profile(nct_id))
    rows = _make_complete_locations(nct_id)[:3]
    with pytest.raises(ol.OutcomesLayoutError, match="never trusted"):
        ol.normalize_decision_rule_locations(
            {"primary_pass": rows, "census_status": "complete"},
            document_profile=prof,
            primary_outcomes_count=1,
        )
    agreed = ol.normalize_decision_rule_locations(
        {"primary_pass": rows, "census_status": "partial"},
        document_profile=prof,
        primary_outcomes_count=1,
    )
    assert agreed is not None and agreed["census_status"] == "partial"


def test_l4_citations_refused_when_no_document_is_posted() -> None:
    rows = _make_complete_locations("NCT99000076")
    with pytest.raises(ol.OutcomesLayoutError, match="no posted documents"):
        ol.normalize_decision_rule_locations(
            rows, document_profile=None, primary_outcomes_count=1, has_posted_documents=False
        )


def test_l4_no_document_census_with_citations_refused() -> None:
    nct_id = "NCT99000076"
    rows = _make_no_document_locations(nct_id)
    second = copy.deepcopy(_make_complete_locations(nct_id, include_second_pass=True)[-1])
    with pytest.raises(ol.OutcomesLayoutError):
        ol.normalize_decision_rule_locations(
            rows + [second], document_profile=None, primary_outcomes_count=1, has_posted_documents=False
        )


def test_l4_on_disk_census_status_recomputed(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    out = _valid_tree(tmp_path / "bundle", nct_id=nct_id)
    path, sdoc = _read_shard(out, nct_id)
    sdoc["data"][nct_id]["decision_rule_locations"]["census_status"] = "partial"
    path.write_bytes(ol.encode_gz_bytes(sdoc))
    idx_doc = json.loads((out / "index.json").read_text(encoding="utf-8"))
    idx_doc["data"][0]["census_badge"]["census_status"] = "partial"  # badge kept consistent
    (out / "index.json").write_text(ol.encode_json_text(idx_doc, trailing_newline=True), encoding="utf-8")
    res = ol.validate_outcomes_tree(out)
    assert not res.ok
    assert any("never trusted" in p for p in res.problems)


# L1: the output allowlist.

@pytest.mark.parametrize("marker", ["run.json", "summary.json", "records", "records.json", "index.json"])
def test_l1_ancestor_marking_another_family_is_refused(tmp_path: pathlib.Path, marker: str) -> None:
    family = tmp_path / "site_data"
    family.mkdir()
    if marker == "records":
        (family / "records").mkdir()
    else:
        (family / marker).write_text("{}\n", encoding="utf-8")
    target = family / "nested" / "outcomes_bundles"
    with pytest.raises(ol.OutcomesLayoutError, match="ancestor"):
        _valid_tree(target)
    assert not (family / "nested").exists()
    assert sorted(os.listdir(family)) == [marker]


def test_l1_non_empty_target_without_marker_is_refused(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "somebody_elses"
    target.mkdir()
    (target / "notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ol.OutcomesLayoutError, match="non-empty"):
        _valid_tree(target)
    assert sorted(os.listdir(target)) == ["notes.txt"]
    assert not pathlib.Path(f"{target}.partial").exists()


def test_l1_symlinked_target_ancestor_and_partial_are_refused(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link_target = tmp_path / "link_target"
    link_target.symlink_to(real, target_is_directory=True)
    with pytest.raises(ol.OutcomesLayoutError, match="symlink"):
        _valid_tree(link_target)
    link_parent = tmp_path / "link_parent"
    link_parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(ol.OutcomesLayoutError, match="symlink"):
        _valid_tree(link_parent / "bundle")
    assert os.listdir(real) == []

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "bundle.partial").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ol.OutcomesLayoutError, match="symlink"):
        _valid_tree(tmp_path / "bundle")
    assert sorted(os.listdir(elsewhere)) == ["precious.txt"]
    assert not (tmp_path / "bundle").exists()


def test_l1_foreign_partial_is_never_deleted_own_leftover_is(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "bundle"
    foreign = tmp_path / "bundle.partial"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ol.OutcomesLayoutError, match="did not create it"):
        _valid_tree(target)
    assert (foreign / "precious.txt").read_text(encoding="utf-8") == "keep"
    assert not target.exists()

    # A leftover staging directory carrying this module's staging marker is removed.
    (foreign / "precious.txt").unlink()
    (foreign / ol.STAGING_MARKER_FILE).write_text(ol._marker_text(ol.STAGING_MARKER), encoding="utf-8")
    (foreign / "shards").mkdir()
    _valid_tree(target)
    assert not foreign.exists()
    assert ol.validate_outcomes_tree(target).ok


def test_l1_bundle_marker_written_and_required(tmp_path: pathlib.Path) -> None:
    out = _valid_tree(tmp_path / "bundle")
    marker = out / ol.BUNDLE_MARKER_FILE
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "layout": "outcomes_bundles",
        "layout_version": ol.LAYOUT_VERSION,
    }
    # Rewriting an existing bundle is allowed (it carries the marker).
    _valid_tree(out)
    marker.unlink()
    res = ol.validate_outcomes_tree(out)
    assert not res.ok
    assert any(ol.BUNDLE_MARKER_FILE in p for p in res.problems)
    # Without its marker the directory is no longer this module's to overwrite.
    with pytest.raises(ol.OutcomesLayoutError, match="foreign directory"):
        _valid_tree(out)


# L2: engine-rooted defaults, engine commit, guarded report.

def test_l2_default_out_dir_is_engine_rooted_whatever_the_cwd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = cli._build_parser().parse_args(["--cohort-manifest", "m.json"])
    assert args.out_dir == str(ROOT / "data" / "outcomes_bundles")
    assert pathlib.Path(args.out_dir).is_absolute()


def _git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_l2_commit_resolved_from_engine_root_not_cwd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "other_repo"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "commit", "-q", "--allow-empty", "-m", "other")
    other_head = _git(other, "rev-parse", "HEAD")
    engine_head = _git(ROOT, "rev-parse", "HEAD")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.chdir(other)
    resolved = ol.resolve_pipeline_commit()
    assert resolved == engine_head
    assert resolved != other_head

    # A directory inside another repository is not its own checkout: no commit.
    nested = other / "engine_copy"
    nested.mkdir()
    assert ol.resolve_pipeline_commit(repo_root=nested) is None


@pytest.mark.parametrize("case", ["foreign_ancestor", "not_a_report", "symlink", "inside_bundle"])
def test_l2_report_path_is_guarded(tmp_path: pathlib.Path, case: str) -> None:
    out = tmp_path / "bundle"
    if case == "foreign_ancestor":
        (tmp_path / "site").mkdir()
        (tmp_path / "site" / "run.json").write_text("{}\n", encoding="utf-8")
        report = tmp_path / "site" / "reports" / "report.json"
    elif case == "not_a_report":
        report = tmp_path / "notes.json"
        report.write_text('{"mine": true}\n', encoding="utf-8")
    elif case == "symlink":
        real = tmp_path / "real.json"
        real.write_text("{}\n", encoding="utf-8")
        report = tmp_path / "report.json"
        report.symlink_to(real)
    else:
        report = out / "report.json"
    argv = _cli_build_args(
        tmp_path, [_make_ctgov_study("NCT99000076")], out, extra=["--report", str(report)]
    )
    assert cli.main(argv) == 1
    assert not out.exists()
    if case == "not_a_report":
        assert report.read_text(encoding="utf-8") == '{"mine": true}\n'
    if case == "foreign_ancestor":
        assert not report.parent.exists()


# L3: interrupts in the swap window, and a retained backup.

def _two_versions(out: pathlib.Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]], bytes, dict[int, bytes]]:
    s1 = _make_ctgov_study("NCT99000076", in_audit=False)
    s2 = _make_ctgov_study("NCT99000060", in_audit=False, sponsor_class="NIH", phase="PHASE2")
    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx_v1, shards_v1)
    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(s1, shards=4), ol.build_trial_bundle(s2, shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    v1_index = (out / "index.json").read_bytes()
    v1_shards = {n: (out / "shards" / f"{n}.json.gz").read_bytes() for n in range(4)}
    return idx_v2, shards_v2, v1_index, v1_shards


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_l3_interrupt_during_swap_restores_previous_tree(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    out = tmp_path / "bundle"
    idx_v2, shards_v2, v1_index, v1_shards = _two_versions(out)
    real_replace = os.replace

    def _interrupt_on_index(src: Any, dst: Any) -> None:
        if pathlib.Path(dst).name == ol.INDEX_FILE:
            raise interrupt()
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _interrupt_on_index)
    with pytest.raises(interrupt):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    monkeypatch.setattr(ol.os, "replace", real_replace)

    assert (out / "index.json").read_bytes() == v1_index
    for n in range(4):
        assert (out / "shards" / f"{n}.json.gz").read_bytes() == v1_shards[n]
    assert ol.validate_outcomes_tree(out).ok
    assert not pathlib.Path(f"{out}.partial").exists()


def test_l3_retained_backup_refuses_next_run_and_names_it(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "bundle"
    idx_v2, shards_v2, _, v1_shards = _two_versions(out)
    real_replace = os.replace

    def _fail_index_and_restore(src: Any, dst: Any) -> None:
        if pathlib.Path(dst).name == ol.INDEX_FILE:
            raise OSError("simulated index swap failure")
        if pathlib.Path(src).name == ol.PREVIOUS_SHARDS_DIR:
            raise OSError("simulated backup restore failure")
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _fail_index_and_restore)
    backup = pathlib.Path(f"{out}.partial") / ol.PREVIOUS_SHARDS_DIR
    with pytest.raises(ol.RetainedBackupError) as first:
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert str(backup) in str(first.value)
    monkeypatch.setattr(ol.os, "replace", real_replace)

    with pytest.raises(ol.RetainedBackupError) as second:
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert str(backup) in str(second.value)
    for n in range(4):
        assert (backup / f"{n}.json.gz").read_bytes() == v1_shards[n]

    manifest = _write_manifest(tmp_path / "m.jsonl", [_make_ctgov_study("NCT99000076")])
    rc = cli.main([
        "--cohort-manifest", str(manifest),
        "--pipeline-commit", SAMPLE_COMMIT,
        "--manifest-cohort", "industry_outcomes_beta",
        "--shards", "4",
        "--out-dir", str(out),
    ])
    assert rc == 1
    assert str(backup) in capsys.readouterr().err
    assert backup.is_dir()


# L5: the manifest is the allowlist; strict IDs; holdout stubs.

def test_l5_cohort_manifest_is_required(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "bundle"
    argv = _cli_build_args(tmp_path, [_make_ctgov_study("NCT99000076")], out, manifest=False)
    assert cli.main(argv) == 1
    assert not out.exists()


def test_l5_trial_absent_from_manifest_is_refused(tmp_path: pathlib.Path) -> None:
    listed = _make_ctgov_study("NCT99000076")
    unlisted = _make_ctgov_study("NCT99000060", sponsor_class="NIH", phase="PHASE2")
    trials_path = tmp_path / "trials.json"
    trials_path.write_text(json.dumps([listed, unlisted]), encoding="utf-8")
    out = tmp_path / "bundle"
    rc = cli.main([
        "--trials", str(trials_path),
        "--cohort-manifest", str(_write_manifest(tmp_path / "m.jsonl", [listed])),
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out),
    ])
    assert rc == 1
    assert not out.exists()


def _side_stream_case(tmp_path: pathlib.Path, case: str) -> tuple[list[str], int]:
    """argv and expected exit code for one bad side stream."""
    nct_id = "NCT99000076"
    study = _make_ctgov_study(nct_id)
    out = tmp_path / "bundle"
    path = tmp_path / f"{case}.jsonl"
    if case == "outside_build_provenance":
        path.write_text(json.dumps(_make_module4_provenance("NCT99000060")) + "\n", encoding="utf-8")
        return _cli_build_args(tmp_path, [study], out, extra=["--provenance", str(path)]), 1
    if case == "outside_build_locations":
        rows = _make_complete_locations("NCT99000060")
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return _cli_build_args(tmp_path, [study], out, extra=["--locations", str(path)]), 1
    if case == "non_ascii_digits":
        pop = {
            "nct_id": "NCT٩٩٠٠٠٠٧٦",
            "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY"}],
        }
        path.write_text(json.dumps(pop) + "\n", encoding="utf-8")
        return _cli_build_args(tmp_path, [study], out, extra=["--population-parsed", str(path)]), 1
    if case == "nested_other_trial":
        prof = _make_doc_profile(nct_id)
        prof["documents"][0]["nct_id"] = "NCT99000060"
        path.write_text(json.dumps(prof) + "\n", encoding="utf-8")
        return _cli_build_args(tmp_path, [study], out, extra=["--documents", str(path)]), 1
    if case in ("side_in_audit_true", "side_inAudit_true"):
        prov = _make_module4_provenance(nct_id)
        prov["in_audit" if case == "side_in_audit_true" else "inAudit"] = True
        path.write_text(json.dumps(prov) + "\n", encoding="utf-8")
        return _cli_build_args(tmp_path, [study], out, extra=["--provenance", str(path)]), 2
    if case == "holdout_population":
        holdout = _make_ctgov_study("NCT99000058", in_audit=True)
        pop = {"nct_id": "NCT99000058", "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY"}]}
        path.write_text(json.dumps(pop) + "\n", encoding="utf-8")
        return _cli_build_args(tmp_path, [study, holdout], out, extra=["--population-parsed", str(path)]), 2
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "outside_build_provenance",
        "outside_build_locations",
        "non_ascii_digits",
        "nested_other_trial",
        "side_in_audit_true",
        "side_inAudit_true",
        "holdout_population",
    ],
)
def test_l5_side_streams_are_checked_against_the_manifest(
    tmp_path: pathlib.Path,
    case: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, expected_rc = _side_stream_case(tmp_path, case)
    assert cli.main(argv) == expected_rc
    assert not (tmp_path / "bundle").exists()
    if expected_rc == 2:
        assert "NCT99000058" not in capsys.readouterr().err


def test_l5_manifest_repeating_in_audit_key_is_refused(tmp_path: pathlib.Path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(
        '[{"nct_id": "NCT99000076", "in_audit": true, "cohorts": ["nih"], "in_audit": false}]',
        encoding="utf-8",
    )
    out = tmp_path / "bundle"
    rc = cli.main([
        "--cohort-manifest", str(manifest),
        "--pipeline-commit", SAMPLE_COMMIT,
        "--out-dir", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_l5_holdout_is_written_as_an_index_only_stub(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    holdout_id = "NCT99000058"
    studies = [_make_ctgov_study("NCT99000076"), _make_ctgov_study(holdout_id, in_audit=True)]
    out = tmp_path / "bundle"
    assert cli.main(_cli_build_args(tmp_path, studies, out)) == 0
    idx_doc = json.loads((out / "index.json").read_text(encoding="utf-8"))
    row = {r["nct_id"]: r for r in idx_doc["data"]}[holdout_id]
    assert row["has_reported_results"] is None
    assert row["primary_outcomes_count"] is None  # not counted from the holdout's results
    path, sdoc = _read_shard(out, holdout_id)
    rec = sdoc["data"][holdout_id]
    assert rec["reported_results"] is None
    assert rec["document_profile"] is None
    assert rec["decision_rule_locations"] is None
    assert rec["longitudinal_provenance"] is None

    # On disk, anything beyond the stub in a holdout record is a leak (exit 2).
    rec["reported_results"] = {"results_posted": True}
    path.write_bytes(ol.encode_gz_bytes(sdoc))
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) == 2
    assert holdout_id not in capsys.readouterr().err


# L6: repeated options.

def test_l6_repeated_locations_append(tmp_path: pathlib.Path) -> None:
    nct_id = "NCT99000076"
    primary_rows, adj_row = _primary_and_adjudication(nct_id)
    a = tmp_path / "primary.jsonl"
    b = tmp_path / "adjudication.jsonl"
    a.write_text("".join(json.dumps(r) + "\n" for r in primary_rows), encoding="utf-8")
    b.write_text(json.dumps(adj_row) + "\n", encoding="utf-8")
    docs = tmp_path / "docs.jsonl"
    docs.write_text(json.dumps(_make_doc_profile(nct_id)) + "\n", encoding="utf-8")
    out = tmp_path / "bundle"
    extra = ["--documents", str(docs), "--locations", str(a), "--locations", str(b)]
    assert cli.main(_cli_build_args(tmp_path, [_make_ctgov_study(nct_id)], out, extra=extra)) == 0
    _, sdoc = _read_shard(out, nct_id)
    locs = sdoc["data"][nct_id]["decision_rule_locations"]
    assert len(locs["primary_pass"]) == 7
    assert len(locs["adjudication_pass"]) == 1

    # The same cell in two files, or the same file twice, is refused.
    out2 = tmp_path / "bundle2"
    dup = ["--documents", str(docs), "--locations", str(a), "--locations", str(tmp_path / "again.jsonl")]
    (tmp_path / "again.jsonl").write_text(json.dumps(primary_rows[0]) + "\n", encoding="utf-8")
    assert cli.main(_cli_build_args(tmp_path, [_make_ctgov_study(nct_id)], out2, extra=dup)) == 1
    same = ["--documents", str(docs), "--locations", str(a), "--locations", str(a)]
    assert cli.main(_cli_build_args(tmp_path, [_make_ctgov_study(nct_id)], out2, extra=same)) == 1
    assert not out2.exists()


@pytest.mark.parametrize("option", ["--documents", "--provenance", "--trials", "--out-dir"])
def test_l6_other_options_refuse_repetition(tmp_path: pathlib.Path, option: str) -> None:
    out = tmp_path / "bundle"
    argv = _cli_build_args(tmp_path, [_make_ctgov_study("NCT99000076")], out)
    argv += [option, str(tmp_path / "a.json"), option, str(tmp_path / "b.json")]
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2
    assert not out.exists()


# L7: the live outcomes index shape ({"rows": [...]}).

def _live_shaped_row(nct_id: str, *, docs: bool, in_audit: bool = False, posted: bool = True) -> dict[str, Any]:
    """A fictional row with exactly the keys of data/outcomes/index.json rows."""
    return {
        "nct_id": nct_id,
        "title": f"Fictional trial {nct_id[-4:]}",
        "sponsor": "Fictional Pharma",
        "sponsor_state": "attributed",
        "recode_differs": False,
        "area": "Oncology",
        "area_secondary": "",
        "primary_objective": "Efficacy",
        "n_primary_outcomes": 2,
        "results_state": "POSTED" if posted else "NOT_POSTED",
        "results_sub_reason": None if posted else "no_results_posted",
        "results_first_post_date": "2023-01-10" if posted else None,
        "protocol": docs,
        "sap": docs,
        "in_audit": in_audit,
        "assessment_kind": None,
        "trial_state": None,
        "review_status": None,
        "in_demographics": True,
        "record": "records/r00.json",
    }


def test_l7_live_outcomes_index_shape_builds_end_to_end(tmp_path: pathlib.Path) -> None:
    rows = [
        _live_shaped_row("NCT99300001", docs=True),
        _live_shaped_row("NCT99300002", docs=False),
        _live_shaped_row("NCT99300003", docs=False, posted=False),
        _live_shaped_row("NCT99300004", docs=True, in_audit=True),
    ]
    index = {
        "columns_note": "Fictional rows in the live index's shape.",
        "excluded": [{"nct_id": "NCT99300099", "reason": "fictional"}],
        "rows": rows,
        "run_id": "outcomes-2026-11-09-fictional",
        "schema_version": 1,
    }
    # The copy keeps the live file's name; it sits in its own directory because
    # a directory holding a non-bundle index.json is refused as an ancestor.
    (tmp_path / "inputs").mkdir()
    manifest = tmp_path / "inputs" / "index.json"
    manifest.write_text(json.dumps(index), encoding="utf-8")
    out = tmp_path / "bundle"
    base = [
        "--cohort-manifest", str(manifest),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out),
    ]
    # Rows without docs carry no cohort, sponsor class or phase: no cohort.
    assert cli.main(base) == 1
    assert not out.exists()

    assert cli.main([*base, "--manifest-cohort", "industry_outcomes_beta"]) == 0
    assert cli.main(["--check", str(out)]) == 0
    idx_doc = json.loads((out / "index.json").read_text(encoding="utf-8"))
    by_id = {r["nct_id"]: r for r in idx_doc["data"]}
    assert idx_doc["total_trials"] == 4
    assert idx_doc["audit_holdout_trials"] == 1
    assert idx_doc["cohort_counts"] == {"all_large_docs": 2, "industry_outcomes_beta": 4}
    assert idx_doc["component_versions"]["cohort_manifest"] == "outcomes-2026-11-09-fictional"
    assert by_id["NCT99300002"]["cohorts"] == ["industry_outcomes_beta"]
    assert by_id["NCT99300002"]["census_badge"]["census_status"] == "no_document"
    assert by_id["NCT99300002"]["primary_outcomes_count"] == 2
    assert by_id["NCT99300002"]["brief_title"] == "Fictional trial 0002"
    assert by_id["NCT99300002"]["lead_sponsor_name"] == "Fictional Pharma"
    # No CT.gov record was supplied, so whether results are posted is unknown.
    assert all(r["has_reported_results"] is None for r in idx_doc["data"])
    assert by_id["NCT99300003"]["results_first_post_date"] is None
    assert by_id["NCT99300004"]["document_badge"]["readability_status"] == "in_audit_holdout"
    assert "NCT99300099" not in by_id  # "excluded" is not a row


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


def test_m3_imports_only_module4_among_the_split_modules() -> None:
    """M3 builds on M4 only: no M1 (document indexer) or M2 (population
    ecosystem) import in the module, its script or its tests, and the module
    and script import no project module but src.citation_verifier."""
    m1_m2 = (
        "document_indexer",
        "index_trial_documents",
        "analysis_population_parser",
        "ecosystem",
    )
    for rel in ("src/outcomes_layout.py", "scripts/build_outcomes_shards.py", "tests/test_outcomes_layout.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.append(node.module)
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
        for name in modules:
            assert not any(bad in name for bad in m1_m2), f"{rel} imports {name}"
        if not rel.startswith("tests/"):
            project = {m for m in modules if m.split(".")[0] in ("src", "scripts", "sponsors")}
            allowed = {"src", "src.outcomes_layout", "src.citation_verifier"}
            assert {m for m in project if m.split(".")[0:2] != ["src", "citation_verifier"]} <= allowed, (
                f"{rel} imports {sorted(project)}"
            )


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


# ── Red team, m3 round 1 (test_redteam_m3_*) ─────────────────────────────────
# Each test asserts the behaviour the module and its CLI promise. Tests for a
# bypass found in round 1 fail until the module is fixed; the others guard
# attacks that the code already refuses. Every trial ID is fictional: in the
# fictional manifests below NCT99000058 is the in_audit holdout and
# NCT99000076 an ordinary trial. Write-safety tests run against fictional
# replicas of the site's data families (file names only, fictional content),
# never against a site checkout.

RT_HOLD = "NCT99000058"
RT_OK = "NCT99000076"
RT_HOLD_DIGITS = RT_HOLD[3:]
RT_HOLD_URL = f"https://cdn.clinicaltrials.gov/large-docs/58/{RT_HOLD}/Prot_SAP_000.pdf"


def _rt_build(
    tmp_path: pathlib.Path,
    *,
    studies: list[dict[str, Any]] | None,
    manifest_rows: list[dict[str, Any]] | None = None,
    side: list[tuple[str, Any]] | None = None,
    out: pathlib.Path | None = None,
) -> tuple[list[str], pathlib.Path]:
    """argv for a CLI build of fictional inputs, and the output directory.

    ``manifest_rows`` defaults to each study's (nct_id, in_audit); a side
    stream given as a str is written verbatim (JSONL), anything else as JSON.
    """
    out = out if out is not None else tmp_path / "bundle"
    argv: list[str] = []
    if studies is not None:
        trials_path = tmp_path / "rt_trials.json"
        trials_path.write_text(json.dumps(studies), encoding="utf-8")
        argv += ["--trials", str(trials_path)]
    rows = (
        manifest_rows
        if manifest_rows is not None
        else [{"nct_id": s["nct_id"], "in_audit": s["in_audit"]} for s in studies or []]
    )
    manifest_path = tmp_path / "rt_manifest.json"
    manifest_path.write_text(json.dumps(rows), encoding="utf-8")
    argv += [
        "--cohort-manifest", str(manifest_path),
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out),
    ]
    for pos, (option, content) in enumerate(side or []):
        path = tmp_path / f"rt_side_{pos}.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        argv += [option, str(path)]
    return argv, out


def _rt_pair() -> list[dict[str, Any]]:
    """The ordinary trial and the holdout, as CT.gov-shaped study records."""
    return [_make_ctgov_study(RT_OK), _make_ctgov_study(RT_HOLD, in_audit=True)]


def _rt_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _rt_assert_refused(
    rc: int,
    out: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    *,
    expected_rc: int | None = None,
) -> None:
    """The build refused (non-zero, or exactly ``expected_rc``), wrote nothing,
    and did not print the holdout's ID (in any spelling of its digits)."""
    err = capsys.readouterr().err
    if expected_rc is None:
        assert rc != 0
    else:
        assert rc == expected_rc
    assert not out.exists()
    assert not pathlib.Path(f"{out}.partial").exists()
    assert RT_HOLD_DIGITS not in err


# Firewall: the holdout flag under the spellings Module 4 reads as in_audit.

@pytest.mark.parametrize("flag_key", ["inAudit", "IN_AUDIT", "in-audit", "is_in_audit"])
def test_redteam_m3_trials_record_holdout_flag_spelling_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    flag_key: str,
) -> None:
    """A --trials record that says it is a holdout (under a spelling other than
    in_audit) for a trial the manifest marks in scope is a conflict: exit 2,
    nothing written. Round 1: built as an ordinary trial with its results."""
    study = _make_ctgov_study(RT_OK)
    del study["in_audit"]
    study[flag_key] = True
    argv, out = _rt_build(
        tmp_path, studies=[study], manifest_rows=[{"nct_id": RT_OK, "in_audit": False}]
    )
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("flag_key", ["inAudit", "is_in_audit", "IN_AUDIT"])
def test_redteam_m3_manifest_row_contradicting_its_own_holdout_flag_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    flag_key: str,
) -> None:
    """A manifest row carrying in_audit false and a second holdout flag that
    is true does not say which it means: exit 2. Round 1: built in scope."""
    rows = [{"nct_id": RT_OK, "in_audit": False, flag_key: True, "cohorts": ["nih"]}]
    argv, out = _rt_build(tmp_path, studies=None, manifest_rows=rows)
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


def test_redteam_m3_build_trial_bundle_refuses_holdout_flag_spelling() -> None:
    """Library entry point: in_audit=False with the record's own inAudit true
    is a conflict, as it is for the in_audit key."""
    study = _make_ctgov_study(RT_OK)
    del study["in_audit"]
    study["inAudit"] = True
    with pytest.raises(ol.AuditFirewallError):
        ol.build_trial_bundle(study, shards=8, in_audit=False)


# Firewall: holdout material nested in an ordinary trial's --trials record.

def _rt_trials_nested_case(case: str) -> dict[str, Any]:
    study = _make_ctgov_study(RT_OK)
    measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    if case == "population_names_holdout":
        measure["analysis_population_parsed"] = {"nct_id": RT_HOLD, "in_audit": True, "population": "ITT"}
    elif case == "analysis_in_audit_true":
        measure["analyses"][0]["in_audit"] = True
    elif case == "top_level_nctId_is_holdout":
        study["nctId"] = RT_HOLD
    else:
        raise AssertionError(case)
    return study


@pytest.mark.parametrize(
    "case", ["population_names_holdout", "analysis_in_audit_true", "top_level_nctId_is_holdout"]
)
def test_redteam_m3_trials_record_nested_holdout_material_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """The nested-identity rule the side streams follow (every trial-ID key
    names this trial, every holdout flag agrees with the manifest) holds for
    --trials too. Round 1: the record is not checked; a nested object stamped
    with the holdout's ID and in_audit true is published verbatim in the
    ordinary trial's shard, and a record whose nctId is the holdout is built
    as the ordinary trial with full results."""
    argv, out = _rt_build(
        tmp_path,
        studies=[_rt_trials_nested_case(case), _make_ctgov_study(RT_HOLD, in_audit=True)],
    )
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_trial_nct_id_refuses_disagreeing_camelcase_id() -> None:
    """Module 4 reads a top-level nctId as the record's ID; trial_nct_id must
    not return the flat nct_id when the nctId names another trial."""
    study = _make_ctgov_study(RT_OK)
    study["nctId"] = RT_HOLD
    with pytest.raises(ol.OutcomesLayoutError):
        ol.trial_nct_id(study)


# Firewall: side-stream content naming the holdout outside a key called nct_id.

def _rt_side_nested_case(case: str) -> tuple[str, Any]:
    if case == "population_keyed_by_holdout_id":
        return "--population-parsed", {
            "nct_id": RT_OK,
            "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "by_trial": {RT_HOLD: {"population": "ITT"}}}],
        }
    if case == "population_trial_nct_id":
        return "--population-parsed", {
            "nct_id": RT_OK,
            "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "trial_nct_id": RT_HOLD}],
        }
    if case == "provenance_milestones_keyed_by_holdout_id":
        prov = _make_module4_provenance(RT_OK)
        prov["registry_milestones"][RT_HOLD] = {"date": {"raw": "2020-01-01", "date_precision": "day"}}
        return "--provenance", prov
    if case == "provenance_document_url_names_holdout":
        prov = _make_module4_provenance(RT_OK)
        prov["large_docs"][0]["url"] = RT_HOLD_URL
        return "--provenance", prov
    if case == "provenance_zero_width_nct_id_key":
        prov = _make_module4_provenance(RT_OK)
        prov["large_docs"][0]["nct​_id"] = RT_HOLD
        return "--provenance", prov
    if case == "document_url_names_holdout":
        prof = _make_doc_profile(RT_OK)
        prof["documents"][0]["url"] = RT_HOLD_URL
        return "--documents", prof
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "population_keyed_by_holdout_id",
        "population_trial_nct_id",
        "provenance_milestones_keyed_by_holdout_id",
        "provenance_document_url_names_holdout",
        "provenance_zero_width_nct_id_key",
        "document_url_names_holdout",
    ],
)
def test_redteam_m3_side_stream_naming_holdout_outside_nct_id_key_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """A side record filed under the ordinary trial that names the holdout (as
    a mapping key, under trial_nct_id or a zero-width-spelled nct_id key, or in
    a CT.gov large-document URL, all of which Module 4 reads as naming a
    trial) is refused. Round 1: exit 0, the holdout's ID and its material
    published in the ordinary trial's shard."""
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[_rt_side_nested_case(case)])
    _rt_assert_refused(cli.main(argv), out, capsys)


@pytest.mark.parametrize("option", ["--population-parsed", "--provenance", "--documents"])
def test_redteam_m3_side_stream_is_in_audit_flag_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    """is_in_audit (a spelling Module 4 reads as in_audit) set true on a side
    record for a trial the manifest marks in scope is a conflict: exit 2.
    Round 1: exit 0, the flag published verbatim or dropped."""
    content: dict[str, Any]
    if option == "--population-parsed":
        content = {"nct_id": RT_OK, "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "is_in_audit": True}]}
    elif option == "--provenance":
        content = {**_make_module4_provenance(RT_OK), "is_in_audit": True}
    else:
        content = {**_make_doc_profile(RT_OK), "is_in_audit": True}
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[(option, content)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("case", ["is_in_audit", "trial_nct_id", "url"])
def test_redteam_m3_check_side_input_identity_reads_module4_spellings(case: str) -> None:
    """Library entry point: check_side_input_identity applies Module 4's
    readings of the holdout flag, the trial-ID fields and URLs."""
    obj: dict[str, Any] = {
        "is_in_audit": {"is_in_audit": True},
        "trial_nct_id": {"rows": [{"trial_nct_id": RT_HOLD}]},
        "url": {"documents": [{"url": RT_HOLD_URL}]},
    }[case]
    with pytest.raises(ol.OutcomesLayoutError):
        ol.check_side_input_identity(obj, expected_nct_id=RT_OK, expected_in_audit=False, label="side")


# Firewall: malformed spellings of the holdout's ID, every stream (guards).

_RT_MALFORMED = {
    "lowercase": RT_HOLD.lower(),
    "leading_space": " " + RT_HOLD,
    "trailing_newline": RT_HOLD + "\n",
    "zero_width": "NCT​" + RT_HOLD_DIGITS,
    "fullwidth_prefix": "ＮＣＴ" + RT_HOLD_DIGITS,
    "arabic_indic_digits": "NCT" + "".join(chr(0x660 + int(c)) for c in RT_HOLD_DIGITS),
}


def _rt_malformed_case(stream: str, bad_id: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None, list[tuple[str, Any]]]:
    """(studies, manifest_rows, side) placing ``bad_id`` in one stream."""
    ok_row = {"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]}
    if stream == "manifest":
        return None, [ok_row, {"nct_id": bad_id, "in_audit": True, "cohorts": ["nih"]}], []
    rows = [{"nct_id": RT_OK, "in_audit": False}, {"nct_id": RT_HOLD, "in_audit": True}]
    if stream == "trials":
        holdout = _make_ctgov_study(RT_HOLD, in_audit=True)
        holdout["nct_id"] = bad_id
        holdout["protocolSection"]["identificationModule"]["nctId"] = bad_id
        return [_make_ctgov_study(RT_OK), holdout], rows, []
    if stream == "documents":
        side: tuple[str, Any] = ("--documents", {**_make_doc_profile(RT_HOLD), "nct_id": bad_id})
    elif stream == "locations":
        locs = _make_complete_locations(RT_HOLD)
        for row in locs:
            row["nct_id"] = bad_id
        side = ("--locations", _rt_jsonl(locs))
    elif stream == "provenance":
        side = ("--provenance", {**_make_module4_provenance(RT_HOLD), "nct_id": bad_id})
    elif stream == "population_parsed":
        side = ("--population-parsed", {"nct_id": bad_id, "outcomes": [{"outcome_index": 0}]})
    elif stream == "nested_document_row":
        prof = _make_doc_profile(RT_OK)
        prof["documents"][0]["nct_id"] = bad_id
        side = ("--documents", prof)
    elif stream == "keyed_mapping":
        side = ("--provenance", {bad_id: _make_module4_provenance(RT_HOLD)})
    else:
        raise AssertionError(stream)
    return _rt_pair(), rows, [side]


@pytest.mark.parametrize("malformation", sorted(_RT_MALFORMED))
@pytest.mark.parametrize(
    "stream",
    ["manifest", "trials", "documents", "locations", "provenance", "population_parsed", "nested_document_row", "keyed_mapping"],
)
def test_redteam_m3_malformed_holdout_ids_refused_in_every_stream(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    stream: str,
    malformation: str,
) -> None:
    """A lowercase, padded, zero-width, full-width or non-ASCII-digit spelling
    of the holdout's ID is refused in every stream (exit 1), nothing is
    written and the spelling is not echoed."""
    studies, rows, side = _rt_malformed_case(stream, _RT_MALFORMED[malformation])
    argv, out = _rt_build(tmp_path, studies=studies, manifest_rows=rows, side=side)
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)


def _rt_holdout_side(option: str) -> tuple[str, Any]:
    if option == "--documents":
        return option, _make_doc_profile(RT_HOLD)
    if option == "--locations":
        return option, _rt_jsonl(_make_complete_locations(RT_HOLD))
    if option == "--provenance":
        return option, _make_module4_provenance(RT_HOLD)
    return option, {"nct_id": RT_HOLD, "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY"}]}


@pytest.mark.parametrize("option", ["--documents", "--locations", "--provenance", "--population-parsed"])
def test_redteam_m3_holdout_side_record_refused_in_every_stream(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    """A side record for the holdout is refused (exit 2) before the output
    directory is touched, with its ID withheld."""
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[_rt_holdout_side(option)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("option", ["--trials", "--documents", "--locations", "--provenance", "--population-parsed"])
def test_redteam_m3_holdout_absent_from_manifest_refused_in_every_stream(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    """A record for a trial the manifest does not list (here the holdout,
    left out of the manifest) is refused (exit 1) in every stream."""
    rows = [{"nct_id": RT_OK, "in_audit": False}]
    if option == "--trials":
        argv, out = _rt_build(tmp_path, studies=_rt_pair(), manifest_rows=rows)
    else:
        argv, out = _rt_build(
            tmp_path, studies=[_make_ctgov_study(RT_OK)], manifest_rows=rows, side=[_rt_holdout_side(option)]
        )
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)


@pytest.mark.parametrize("case", ["locations_grouped_row", "population_row", "provenance_doc_nctId", "documents_flat_row"])
def test_redteam_m3_nested_nct_id_naming_holdout_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """A nested row under a key read as the trial ID that names the holdout is
    refused in every side stream (exit 1)."""
    side: tuple[str, Any]
    if case == "locations_grouped_row":
        rows = _make_complete_locations(RT_OK)
        rows[0]["nct_id"] = RT_HOLD
        side = ("--locations", {"nct_id": RT_OK, "primary_pass": [{k: v for k, v in r.items() if k != "pass"} for r in rows]})
    elif case == "population_row":
        side = ("--population-parsed", {"nct_id": RT_OK, "outcomes": [{"outcome_index": 0, "nct_id": RT_HOLD}]})
    elif case == "provenance_doc_nctId":
        prov = _make_module4_provenance(RT_OK)
        prov["large_docs"][0]["nctId"] = RT_HOLD
        side = ("--provenance", prov)
    else:
        doc = dict(_make_doc_profile(RT_OK)["documents"][0])
        side = ("--documents", _rt_jsonl([{**doc, "nct_id": RT_OK, "source": {"NCT_ID": RT_HOLD}}]))
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[side])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)


# Firewall: holdout IDs in refusal messages, inline sections, stub fields.

@pytest.mark.parametrize("field", ["enrollment", "primary_outcomes_count"])
def test_redteam_m3_holdout_id_not_echoed_in_build_refusal(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    """A refusal about a holdout row withholds the holdout's ID. Round 1:
    "ERROR: NCT99000058.enrollment must be an int or None, got str: '240'"."""
    rows = [
        {"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]},
        {"nct_id": RT_HOLD, "in_audit": True, "cohorts": ["nih"], field: "240"},
    ]
    argv, out = _rt_build(tmp_path, studies=None, manifest_rows=rows)
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)


def test_redteam_m3_holdout_id_not_echoed_in_check_problems(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check withholds the holdout's ID in every problem about its row, not
    only in firewall problems. Round 1: an invalid holdout cohorts list is
    reported as "index.json.data[NCT99000058].cohorts is invalid: []"."""
    argv, out = _rt_build(tmp_path, studies=_rt_pair())
    assert cli.main(argv) == 0
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    for row in idx["data"]:
        if row["nct_id"] == RT_HOLD:
            row["cohorts"] = []
    (out / "index.json").write_text(ol.encode_json_text(idx, trailing_newline=True), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


@pytest.mark.parametrize(
    "inline_key", ["document_profile", "reported_results", "longitudinal_provenance", "analysis_population_parsed"]
)
def test_redteam_m3_holdout_inline_side_sections_refused_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    inline_key: str,
) -> None:
    """A side section carried inline in the holdout's own --trials record is
    holdout material in the input, refused like a side-stream record (exit 2).
    Round 1: dropped without a word (exit 0)."""
    holdout = _make_ctgov_study(RT_HOLD, in_audit=True)
    holdout[inline_key] = {
        "document_profile": _make_doc_profile(RT_HOLD),
        "reported_results": {"results_posted": True, "primary_outcome_measures": [{"title": "OS"}]},
        "longitudinal_provenance": _make_module4_provenance(RT_HOLD),
        "analysis_population_parsed": {"outcomes": [{"outcome_index": 0, "population": "ITT"}]},
    }[inline_key]
    argv, out = _rt_build(tmp_path, studies=[_make_ctgov_study(RT_OK), holdout])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("field", ["brief_title", "primary_condition", "lead_sponsor_name", "phase"])
def test_redteam_m3_holdout_stub_scalar_fields_not_coerced_from_objects(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    """A holdout stub's index fields are scalars copied 1:1; an object there is
    refused, not published as its str(). Round 1: exit 0 and the index row
    carries "{'results': {'hr': '0.68', 'p': '<0.0001'}}"."""
    rows = [
        {"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]},
        {"nct_id": RT_HOLD, "in_audit": True, "cohorts": ["nih"], field: {"results": {"hr": "0.68", "p": "<0.0001"}}},
    ]
    argv, out = _rt_build(tmp_path, studies=None, manifest_rows=rows)
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_check_refuses_object_in_holdout_index_scalar_field(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check refuses a holdout index row whose scalar field holds an object
    (results smuggled past the stub). Round 1: "outcomes tree check: ok"."""
    argv, out = _rt_build(tmp_path, studies=_rt_pair())
    assert cli.main(argv) == 0
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    for row in idx["data"]:
        if row["nct_id"] == RT_HOLD:
            row["primary_condition"] = {"primary_outcome_measures": [{"title": "OS", "classes": [{"value": "11.2"}]}]}
    (out / "index.json").write_text(ol.encode_json_text(idx, trailing_newline=True), encoding="utf-8")
    assert cli.main(["--check", str(out)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


def test_redteam_m3_check_refuses_repeated_in_audit_key(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check refuses an object that repeats in_audit, as the build refuses
    one in its inputs: a reader keeping the first value sees a holdout. Here
    the holdout's index row and shard record read in_audit true first and
    false last, and carry the full record. Round 1: "outcomes tree check: ok
    (2 trials, 0 holdout ...)", exit 0."""
    argv, out = _rt_build(tmp_path, studies=_rt_pair())
    assert cli.main(argv) == 0
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    ok_row = next(r for r in idx["data"] if r["nct_id"] == RT_OK)
    _, ok_sdoc = _read_shard(out, RT_OK)
    hold_path, hold_sdoc = _read_shard(out, RT_HOLD)

    def _dup(obj: dict[str, Any]) -> str:
        text = json.dumps(obj, separators=(",", ":"))
        return text.replace('"in_audit":false', '"in_audit":true,"in_audit":false', 1)

    full_row = {**ok_row, "nct_id": RT_HOLD}
    rows_text = [
        _dup(full_row) if r["nct_id"] == RT_HOLD else json.dumps(r, separators=(",", ":")) for r in idx["data"]
    ]
    header = {k: v for k, v in idx.items() if k != "data"}
    header["audit_holdout_trials"] = 0
    (out / "index.json").write_text(
        json.dumps(header, separators=(",", ":"))[:-1] + ',"data":[' + ",".join(rows_text) + "]}\n",
        encoding="utf-8",
    )
    full_rec = {**ok_sdoc["data"][RT_OK], "nct_id": RT_HOLD, "shard": ol.outcomes_shard_of(RT_HOLD, 8)}
    entries = [
        json.dumps(k) + ":" + (_dup(full_rec) if k == RT_HOLD else json.dumps(v, separators=(",", ":")))
        for k, v in hold_sdoc["data"].items()
    ]
    shard_header = {k: v for k, v in hold_sdoc.items() if k != "data"}
    text = json.dumps(shard_header, separators=(",", ":"))[:-1] + ',"data":{' + ",".join(entries) + "}}"
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(text.encode("ascii"))
    hold_path.write_bytes(buf.getvalue())
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


# Firewall: library entry points.

@pytest.mark.parametrize("case", ["lowercase_key", "padded_key", "value_names_holdout"])
def test_redteam_m3_cross_stream_guard_reads_malformed_keys_and_values(case: str) -> None:
    """assert_no_audit_cross_stream_leakage (vector 6) refuses a side mapping
    keyed by a malformed spelling of a holdout ID, or whose record names the
    holdout. Round 1: returns None for all three."""
    kwargs: dict[str, Any] = {
        "lowercase_key": {"documents_by_nct": {RT_HOLD.lower(): {"documents": []}}},
        "padded_key": {"locations_by_nct": {RT_HOLD + " ": []}},
        "value_names_holdout": {"provenance_by_nct": {RT_OK: {"nct_id": RT_HOLD}}},
    }[case]
    with pytest.raises(ol.OutcomesLayoutError):
        ol.assert_no_audit_cross_stream_leakage({RT_HOLD}, **kwargs)


@pytest.mark.parametrize("entry_point", ["provenance", "locations", "reported_results"])
def test_redteam_m3_section_builders_refuse_holdout_input(entry_point: str) -> None:
    """The public section builders refuse input that is a holdout's: a holdout
    stub carries no provenance, no locations and no extracted results. Round 1:
    each returns the full section."""
    with pytest.raises(ol.AuditFirewallError):
        if entry_point == "provenance":
            ol.normalize_longitudinal_provenance(_make_module4_provenance(RT_HOLD), in_audit=True)
        elif entry_point == "locations":
            ol.normalize_decision_rule_locations(
                {"nct_id": RT_HOLD, "in_audit": True, "primary_pass": _make_complete_locations(RT_HOLD)},
                document_profile=ol.normalize_document_profile(_make_doc_profile(RT_HOLD)),
            )
        else:
            ol.extract_reported_results(_make_ctgov_study(RT_HOLD, in_audit=True))


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
@pytest.mark.parametrize("badge", ["document", "census"])
def test_redteam_m3_badges_require_strict_bool_in_audit(badge: str, value: Any) -> None:
    """Vector 1 (in_audit never defaults) holds at the badge builders: a
    missing or non-bool in_audit is refused rather than read by truthiness.
    Round 1: in_audit=None returns the unmasked badge (doc_count, pages)."""
    with pytest.raises(ol.AuditFirewallError):
        if badge == "document":
            ol.build_document_badge(
                in_audit=value, document_profile=ol.normalize_document_profile(_make_doc_profile(RT_OK))
            )
        else:
            ol.build_census_badge(
                in_audit=value,
                decision_rule_locations={"census_status": "complete", "effective_pass": [{"status": "FOUND"}]},
                has_documents=True,
            )


# Provenance: the commit stamp.

def test_redteam_m3_commit_stamp_ignores_foreign_git_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With GIT_DIR naming another repository (as inside a git hook), the
    stamp is still this engine checkout's commit, or the build is refused.
    Round 1: the other repository's HEAD is stamped as source_pipeline_commit."""
    other = tmp_path / "other_repo"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "commit", "-q", "--allow-empty", "-m", "other")
    other_head = _git(other, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert ol.resolve_pipeline_commit() != other_head
    rows = [{"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]}]
    (tmp_path / "m.json").write_text(json.dumps(rows), encoding="utf-8")
    out = tmp_path / "bundle"
    rc = cli.main(["--cohort-manifest", str(tmp_path / "m.json"), "--shards", "8", "--out-dir", str(out)])
    if rc == 0:
        stamped = json.loads((out / "index.json").read_text(encoding="utf-8"))["source_pipeline_commit"]
        assert stamped != other_head
    else:
        assert rc == 1
        assert not out.exists()


# Write safety: fictional replicas of the site's data families.

_RT_FAMILIES: dict[str, tuple[str, ...]] = {
    "outcomes": ("index.json", "run.json", "summary.json", "records/r00.json", "records/r01.json"),
    "results_structure": ("run.json", "summary.json", "records.json"),
    "sponsors": ("bridge.csv.gz", "bridge_meta.json", "audit/audit_summary.json", "audit/label_changes.csv"),
    "geo": ("README.md", "active_run.json", "geo_rep_2026-06-19_run063/CONTRACT.md", "geo_rep_2026-06-19_run063/geo_dictionary.csv"),
    "sex_gender": ("methods.json", "methods.md", "audit/audit_summary.json", "audit/status_counts.json"),
}


def _rt_site_replica(tmp_path: pathlib.Path) -> pathlib.Path:
    """A fictional ``data/`` holding each family's file names (not the site's
    contents). Like a copy of the five family folders, it has no data/run.json."""
    data = tmp_path / "replica" / "data"
    for family, names in _RT_FAMILIES.items():
        for name in names:
            path = data / family / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fictional {family}/{name}\n", encoding="utf-8")
    return data


def _rt_tree_hashes(root: pathlib.Path) -> dict[str, str]:
    """sha256 of every file (and the target of every symlink) under ``root``."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = "symlink->" + os.readlink(path)
        elif path.is_dir():
            found[rel] = "dir"
        else:
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _rt_manifest_only(tmp_path: pathlib.Path, out: pathlib.Path | str, *extra: str) -> list[str]:
    rows = [{"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]}, {"nct_id": RT_HOLD, "in_audit": True, "cohorts": ["nih"]}]
    path = tmp_path / "rt_ws_manifest.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return [
        "--cohort-manifest", str(path),
        "--pipeline-commit", SAMPLE_COMMIT,
        "--extracted-at", SAMPLE_TS,
        "--shards", "8",
        "--out-dir", str(out),
        *extra,
    ]


@pytest.mark.parametrize(
    "sub",
    ["outcomes", "outcomes/bundles", "outcomes/records", "outcomes/records/b", "results_structure", "results_structure/b", "."],
)
def test_redteam_m3_known_family_subpaths_refused(tmp_path: pathlib.Path, sub: str) -> None:
    """The outcomes and results-structure families, any path under them and
    the data folder itself are refused; every file's sha256 is unchanged."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    assert cli.main(_rt_manifest_only(tmp_path, data / sub)) == 1
    assert _rt_tree_hashes(data) == before


@pytest.mark.parametrize(
    "sub",
    [
        "geo/outcomes_bundles",
        "geo/geo_rep_2026-06-19_run063/bundles",
        "sponsors/outcomes_bundles",
        "sponsors/audit/b",
        "sex_gender/outcomes_bundles",
        "sex_gender/audit/b",
    ],
)
def test_redteam_m3_unrecognised_family_subpaths_refused(tmp_path: pathlib.Path, sub: str) -> None:
    """The geography, sponsor and sex/gender families are data families too: a
    bundle is not written under them. Round 1: written (exit 0), because only
    run.json, summary.json, records, records.json and a foreign index.json mark
    a family, and these three carry none of them; in the site checkout only
    data/run.json above them stops the write."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    assert cli.main(_rt_manifest_only(tmp_path, data / sub)) == 1
    assert _rt_tree_hashes(data) == before


@pytest.mark.parametrize(
    "sub",
    [
        "outcomes/r.json",
        "results_structure/r.json",
        "geo/r.json",
        "geo/geo_rep_2026-06-19_run063/r.json",
        "sponsors/r.json",
        "sponsors/audit/r.json",
        "sex_gender/r.json",
        "sex_gender/audit/r.json",
        "geo/active_run.json",
        "sponsors/bridge_meta.json",
        "sex_gender/methods.json",
    ],
)
def test_redteam_m3_report_into_family_refused(tmp_path: pathlib.Path, sub: str) -> None:
    """--report is not written into any data family. Round 1: a new report
    file is written into geo/, sponsors/, sex_gender/ and their subfolders
    (exit 0); existing family files and the two recognised families are
    refused."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    out = tmp_path / "bundle"
    assert cli.main(_rt_manifest_only(tmp_path, out, "--report", str(data / sub))) == 1
    assert _rt_tree_hashes(data) == before
    assert not out.exists()


@pytest.mark.parametrize(
    "case",
    ["symlink_target", "symlink_ancestor", "symlink_partial", "foreign_partial", "symlink_report", "hardlink_report"],
)
def test_redteam_m3_symlinks_partials_and_report_links_refused(tmp_path: pathlib.Path, case: str) -> None:
    """A target, ancestor or .partial that is a symlink into a family, a
    non-empty .partial without the staging marker, and a report path that is
    a symlink or hard link to a family file are all refused, and no family
    file changes."""
    data = _rt_site_replica(tmp_path)
    out = tmp_path / "bundle"
    extra: list[str] = []
    if case == "symlink_target":
        out.symlink_to(data / "outcomes")
    elif case == "symlink_ancestor":
        (tmp_path / "via").symlink_to(data / "geo")
        out = tmp_path / "via" / "bundle"
    elif case == "symlink_partial":
        pathlib.Path(f"{out}.partial").symlink_to(data / "sponsors")
    elif case == "foreign_partial":
        part = pathlib.Path(f"{out}.partial")
        part.mkdir()
        (part / "keep.txt").write_text("someone else's\n", encoding="utf-8")
    elif case == "symlink_report":
        (tmp_path / "report.json").symlink_to(data / "outcomes" / "summary.json")
        extra = ["--report", str(tmp_path / "report.json")]
    else:
        os.link(data / "sex_gender" / "methods.json", tmp_path / "report.json")
        extra = ["--report", str(tmp_path / "report.json")]
    before = _rt_tree_hashes(data)
    assert cli.main(_rt_manifest_only(tmp_path, out, *extra)) == 1
    assert _rt_tree_hashes(data) == before
    if case == "foreign_partial":
        assert (pathlib.Path(f"{out}.partial") / "keep.txt").read_text(encoding="utf-8") == "someone else's\n"


def test_redteam_m3_own_partial_holding_links_is_removed_without_following(tmp_path: pathlib.Path) -> None:
    """A leftover .partial of this module's (staging marker) holding a symlink
    to a family folder and a hard link to a family file is removed without
    following either: the build succeeds and no family file changes."""
    data = _rt_site_replica(tmp_path)
    out = tmp_path / "bundle"
    part = pathlib.Path(f"{out}.partial")
    part.mkdir()
    (part / ol.STAGING_MARKER_FILE).write_text(ol._marker_text(ol.STAGING_MARKER), encoding="utf-8")
    (part / "link").symlink_to(data / "outcomes")
    os.link(data / "outcomes" / "run.json", part / "hard_run.json")
    before = _rt_tree_hashes(data)
    assert cli.main(_rt_manifest_only(tmp_path, out)) == 0
    assert _rt_tree_hashes(data) == before
    assert not part.exists()


@pytest.mark.parametrize("out_arg", ["", ".", "outcomes_bundles"])
def test_redteam_m3_relative_out_dir_from_family_cwd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    out_arg: str,
) -> None:
    """Run from inside a family folder, the default out-dir is still the
    engine's own, and a relative or empty --out-dir that lands in the family
    is refused. Round 1: "outcomes_bundles" from geo/ is written (exit 0)."""
    data = _rt_site_replica(tmp_path)
    monkeypatch.chdir(data / "geo")
    args = cli._build_parser().parse_args(["--cohort-manifest", "m.json"])
    assert args.out_dir == str(ROOT / "data" / "outcomes_bundles")
    before = _rt_tree_hashes(data)
    assert cli.main(_rt_manifest_only(tmp_path, out_arg)) == 1
    assert _rt_tree_hashes(data) == before


# Write safety: interrupts at each rename of the swap.

def _rt_swap_step(src: Any, dst: Any, target: pathlib.Path) -> str:
    """Name the swap step an ``os.replace(src, dst)`` call performs."""
    s, d = pathlib.Path(src), pathlib.Path(dst)
    if d == target / ol.BUNDLE_MARKER_FILE:
        return "marker"
    if d.name == ol.PREVIOUS_SHARDS_DIR:
        return "backup_old_shards"
    if d == target / ol.SHARDS_DIR and s.parent.name.endswith(".partial"):
        return "install_new_shards"
    if d == target / ol.INDEX_FILE:
        return "index"
    return "other"


_RT_SWAP_CASES: list[tuple[str, str]] = [
    ("existing_bundle", "backup_old_shards"),
    ("existing_bundle", "install_new_shards"),
    ("existing_bundle", "index"),
    ("absent", "marker"),
    ("absent", "install_new_shards"),
    ("absent", "index"),
    ("empty_dir", "marker"),
    ("empty_dir", "install_new_shards"),
    ("empty_dir", "index"),
]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(("state", "step"), _RT_SWAP_CASES)
def test_redteam_m3_interrupt_at_each_rename_restores_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    step: str,
    when: str,
    interrupt: type[BaseException],
) -> None:
    """KeyboardInterrupt or SystemExit at any rename of the swap leaves the
    target byte-for-byte as it was (absent, empty, or the previous bundle) and
    no .partial behind. "after" is the interrupt that lands when the rename
    has completed but before the next statement runs (where CPython delivers
    a signal that arrived during the call). Round 1, "after": the previous
    index is overwritten and lost (index step), the target is left without
    shards or with mixed versions behind a RetainedBackupError (shard steps),
    or a half-written bundle or a lone marker is left (new target)."""
    out = tmp_path / "bundle"
    if state == "existing_bundle":
        idx_v1, shards_v1 = ol.build_outcomes_documents(
            [ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4)],
            extracted_at="2026-11-20T03:00:00+00:00",
            source_pipeline_commit=SAMPLE_COMMIT,
            shards=4,
        )
        ol.write_outcomes_tree(out, idx_v1, shards_v1)
    elif state == "empty_dir":
        out.mkdir()
    before = _rt_tree_hashes(out) if out.exists() else None
    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [
            ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4),
            ol.build_trial_bundle(_make_ctgov_study("NCT99000060", sponsor_class="NIH", phase="PHASE2"), shards=4),
        ],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    real_replace = os.replace
    fired: list[str] = []

    def _replace(src: Any, dst: Any) -> None:
        if not fired and _rt_swap_step(src, dst, out) == step:
            fired.append(step)
            if when == "after":
                real_replace(src, dst)
            raise interrupt()
        real_replace(src, dst)

    monkeypatch.setattr(ol.os, "replace", _replace)
    with pytest.raises(BaseException) as excinfo:
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    monkeypatch.setattr(ol.os, "replace", real_replace)
    assert fired == [step]
    assert isinstance(excinfo.value, interrupt)
    assert (_rt_tree_hashes(out) if out.exists() else None) == before
    assert not pathlib.Path(f"{out}.partial").exists()


def test_redteam_m3_target_swapped_for_symlink_after_check_not_written_through(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the target becomes a symlink into a family folder after the
    allowlist check (another process, while the staged tree is validated), the
    swap does not write through it. Round 1: the family's index.json is
    replaced by the bundle's and shards/ and the marker are added to it."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    out = tmp_path / "bundle"
    real_validate = ol.validate_outcomes_tree

    def _validate_then_swap(path: Any, **kwargs: Any) -> ol.ValidationResult:
        result = real_validate(path, **kwargs)
        if pathlib.Path(path).name.endswith(".partial") and not os.path.lexists(out):
            out.symlink_to(data / "outcomes")
        return result

    monkeypatch.setattr(ol, "validate_outcomes_tree", _validate_then_swap)
    idx, shard_docs = ol.build_outcomes_documents(
        [ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    with contextlib.suppress(ol.OutcomesLayoutError, OSError):
        ol.write_outcomes_tree(out, idx, shard_docs)
    assert _rt_tree_hashes(data) == before


def test_redteam_m3_foreign_entry_inside_bundle_shards_not_deleted(tmp_path: pathlib.Path) -> None:
    """A directory this module never writes, found inside an existing bundle's
    shards/, is not deleted by the next write: the write is refused and the
    entry kept. Round 1: the old shards/ (entry included) is moved into the
    staging directory and removed with it, without a word."""
    out = tmp_path / "bundle"
    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx_v1, shards_v1)
    notes = out / "shards" / "notes"
    notes.mkdir()
    (notes / "keep.txt").write_text("operator notes\n", encoding="utf-8")
    with pytest.raises(ol.OutcomesLayoutError):
        ol.write_outcomes_tree(out, idx_v1, shards_v1)
    assert (notes / "keep.txt").read_text(encoding="utf-8") == "operator notes\n"


# ── Round-1 fixes: behaviours added beyond the red-team cases ────────────────
# Fictional IDs only (RT_OK ordinary, RT_HOLD the holdout), as above.

def test_m3_fix_ordinary_trial_record_text_naming_holdout_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ordinary trial's --trials record whose registry text names a holdout
    (in any reading Module 4 takes for an ID) is refused as a firewall
    violation (exit 2), nothing written, the ID not echoed: its text is
    copied into the shard, and the holdout is never recorded there."""
    study = _make_ctgov_study(RT_OK)
    study["protocolSection"]["outcomesModule"]["primaryOutcomes"][0]["description"] = (
        f"As in the parent study nct {RT_HOLD_DIGITS[:4]} {RT_HOLD_DIGITS[4:]}."
    )
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


def test_m3_fix_family_folder_by_position_refused_without_markers(tmp_path: pathlib.Path) -> None:
    """A folder named for a site data family directly under data/ is refused
    even when it holds none of the family's marker files."""
    family = tmp_path / "data" / "census"
    family.mkdir(parents=True)
    with pytest.raises(ol.OutcomesLayoutError, match="site data family folder"):
        _valid_tree(family / "bundles")
    with pytest.raises(ol.OutcomesLayoutError, match="site data family folder"):
        ol.check_report_path(family / "r.json")
    assert os.listdir(family) == []


def test_m3_fix_retained_previous_index_refuses_until_resolved(tmp_path: pathlib.Path) -> None:
    """A _previous_index.json left in the staging directory that differs from
    the live index (an interrupted swap the rollback could not finish) refuses
    every write and is named; one identical to the live index is a harmless
    leftover and is removed by the next write."""
    out = _valid_tree(tmp_path / "bundle")
    work = pathlib.Path(f"{out}.partial")
    work.mkdir()
    (work / ol.STAGING_MARKER_FILE).write_text(ol._marker_text(ol.STAGING_MARKER), encoding="utf-8")
    (work / ol.PREVIOUS_INDEX_FILE).write_text('{"older": "index"}\n', encoding="utf-8")
    with pytest.raises(ol.RetainedBackupError, match=ol.PREVIOUS_INDEX_FILE):
        _valid_tree(out)
    assert (work / ol.PREVIOUS_INDEX_FILE).read_text(encoding="utf-8") == '{"older": "index"}\n'
    (work / ol.PREVIOUS_INDEX_FILE).write_bytes((out / ol.INDEX_FILE).read_bytes())
    _valid_tree(out)
    assert not work.exists()


def test_m3_fix_extract_reported_results_refuses_holdout_parsed_populations() -> None:
    """Parsed populations that say they are a holdout's are refused by the
    reported-results builder, as the study itself is."""
    with pytest.raises(ol.AuditFirewallError):
        ol.extract_reported_results(
            _make_ctgov_study(RT_OK),
            parsed_populations={"outcomes": [{"outcome_index": 0, "inAudit": True}]},
        )


# ── Red team m3, round 2 (on 2e1717e) ────────────────────────────────────────
# Fictional IDs only (RT_OK ordinary, RT_HOLD the holdout), as in round 1.
# Each test asserts the correct behaviour; on 2e1717e every one fails.

def _rt2_write(path: pathlib.Path, obj: Any) -> pathlib.Path:
    """Write ``obj`` as JSON (a str is written verbatim) and return the path."""
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def _rt2_argv(tmp_path: pathlib.Path, *, manifest: Any, trials: Any = None, extra: list[str] | None = None) -> tuple[list[str], pathlib.Path]:
    """argv for a CLI build whose manifest and trials files hold ``manifest``
    and ``trials`` exactly as given (a container object is kept as is)."""
    out = tmp_path / "bundle"
    argv = ["--cohort-manifest", str(_rt2_write(tmp_path / "rt2_manifest.json", manifest))]
    if trials is not None:
        argv += ["--trials", str(_rt2_write(tmp_path / "rt2_trials.json", trials))]
    argv += [
        "--extracted-at", SAMPLE_TS,
        "--pipeline-commit", SAMPLE_COMMIT,
        "--shards", "8",
        "--out-dir", str(out),
        *(extra or []),
    ]
    return argv, out


_RT2_ROWS: list[dict[str, Any]] = [
    {"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]},
    {"nct_id": RT_HOLD, "in_audit": True, "cohorts": ["nih"]},
]


def _rt2_side_pair(option: str) -> tuple[Any, Any]:
    """(the ordinary trial's record, the holdout's record) for one side stream."""
    if option == "--documents":
        return _make_doc_profile(RT_OK), _make_doc_profile(RT_HOLD)
    if option == "--provenance":
        return _make_module4_provenance(RT_OK), _make_module4_provenance(RT_HOLD)
    if option == "--locations":
        return _make_complete_locations(RT_OK), _make_complete_locations(RT_HOLD)
    return (
        {"nct_id": RT_OK, "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "population": "ITT"}]},
        {"nct_id": RT_HOLD, "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "population": "ITT"}]},
    )


@pytest.mark.parametrize("extra_key", ["second_list_key", "holdout_id_key"])
@pytest.mark.parametrize("option", ["--documents", "--provenance", "--population-parsed", "--locations", "--trials"])
def test_redteam_m3_container_extra_key_holding_holdout_record_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    extra_key: str,
) -> None:
    """A JSON container ({"data": [...]}) whose other keys hold the holdout's
    record (a second list key such as "records", or a key that is the holdout's
    ID) is holdout material in the input: refused, never dropped. Round 2:
    _load_records_file returns the first of data/trials/records/rows and drops
    every other key without a word; exit 0, "wrote 2 trials (1 holdout)"."""
    if option == "--trials":
        ok_rec: Any = _make_ctgov_study(RT_OK)
        hold_rec: Any = {**_make_ctgov_study(RT_HOLD, in_audit=True), "document_profile": _make_doc_profile(RT_HOLD)}
    else:
        ok_rec, hold_rec = _rt2_side_pair(option)
    ok_list = ok_rec if isinstance(ok_rec, list) else [ok_rec]
    hold_list = hold_rec if isinstance(hold_rec, list) else [hold_rec]
    container: dict[str, Any] = {"data": ok_list}
    if extra_key == "second_list_key":
        container["records"] = hold_list
    else:
        container[RT_HOLD] = hold_list[0]
    if option == "--trials":
        argv, out = _rt2_argv(tmp_path, manifest=_RT2_ROWS, trials=container)
    else:
        side = _rt2_write(tmp_path / "rt2_side.json", container)
        argv, out = _rt2_argv(tmp_path, manifest=_RT2_ROWS, trials=_rt_pair(), extra=[option, str(side)])
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_manifest_container_with_two_disagreeing_row_lists_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manifest container carrying both "data" and "rows", one marking the
    holdout in scope and the other in_audit true, does not say which it means
    (the container-level form of a row contradicting its own in_audit): exit
    2, nothing written. Round 2: the builder takes "data", ignores "rows", and
    publishes the holdout in full: exit 0, "wrote 2 trials (0 holdout)", its
    index row has_reported_results true and its shard carries reported_results."""
    manifest = {
        "data": [{"nct_id": RT_OK, "in_audit": False}, {"nct_id": RT_HOLD, "in_audit": False}],
        "rows": [{"nct_id": RT_OK, "in_audit": False}, {"nct_id": RT_HOLD, "in_audit": True}],
    }
    argv, out = _rt2_argv(tmp_path, manifest=manifest, trials=[_make_ctgov_study(RT_OK), _make_ctgov_study(RT_HOLD)])
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_manifest_run_id_naming_holdout_not_published(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The manifest container's run_id is copied into index.json
    (component_versions.cohort_manifest); one that names the holdout (here with
    its results) is holdout material and is refused, like any other input
    text naming a holdout. Round 2: never scanned; exit 0 and index.json
    carries "NCT99000058: HR 0.68 (0.52-0.89) p<0.0001"."""
    manifest = {"run_id": f"{RT_HOLD}: HR 0.68 (0.52-0.89) p<0.0001", "rows": _RT2_ROWS}
    argv, out = _rt2_argv(tmp_path, manifest=manifest, trials=_rt_pair())
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_check_does_not_ignore_cohort_manifest(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check DIR --cohort-manifest M either checks the tree against M (where
    M marks a trial in_audit, the tree may hold only its stub) or refuses the
    combination; it never ignores M. Round 2: M is accepted and ignored, and a
    tree publishing M's holdout in full checks "ok (2 trials, 0 holdout ...)",
    exit 0."""
    rows_in_scope = [{"nct_id": RT_OK, "in_audit": False}, {"nct_id": RT_HOLD, "in_audit": False}]
    argv, out = _rt2_argv(tmp_path, manifest=rows_in_scope, trials=[_make_ctgov_study(RT_OK), _make_ctgov_study(RT_HOLD)])
    assert cli.main(argv) == 0
    authoritative = _rt2_write(tmp_path / "rt2_authoritative.json", _RT2_ROWS)
    capsys.readouterr()
    assert cli.main(["--check", str(out), "--cohort-manifest", str(authoritative)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


def _rt2_built_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A CLI-built tree: the ordinary trial with documents and provenance, and
    the holdout's stub."""
    argv, out = _rt2_argv(
        tmp_path,
        manifest=_RT2_ROWS,
        trials=_rt_pair(),
        extra=[
            "--documents", str(_rt2_write(tmp_path / "rt2_docs.json", _make_doc_profile(RT_OK))),
            "--provenance", str(_rt2_write(tmp_path / "rt2_prov.json", _make_module4_provenance(RT_OK))),
        ],
    )
    assert cli.main(argv) == 0
    return out


def _rt2_edit_index(out: pathlib.Path, edit: Any) -> None:
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    edit(idx)
    (out / "index.json").write_text(ol.encode_json_text(idx, trailing_newline=True), encoding="utf-8")


def _rt2_edit_record(out: pathlib.Path, nct_id: str, edit: Any) -> None:
    path, sdoc = _read_shard(out, nct_id)
    edit(sdoc["data"][nct_id])
    path.write_bytes(ol.encode_gz_bytes(sdoc))


@pytest.mark.parametrize("case", ["document_url_names_holdout", "nested_in_audit_true", "nested_nct_id_is_holdout"])
def test_redteam_m3_check_applies_identity_rules_to_in_scope_records(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """--check reads every in-scope shard record with the rules the builder
    applies on input (check_side_input_identity against the tree's own
    holdout set): a document URL naming the tree's holdout, a nested in_audit
    true, a nested nct_id naming the holdout. Round 2: none is read; each
    tree checks "ok (2 trials, 1 holdout ...)", exit 0."""
    out = _rt2_built_tree(tmp_path)

    def edit(rec: dict[str, Any]) -> None:
        if case == "document_url_names_holdout":
            rec["document_profile"]["documents"][0]["url"] = RT_HOLD_URL
        elif case == "nested_in_audit_true":
            rec["reported_results"]["primary_outcome_measures"][0]["analyses"][0]["in_audit"] = True
        else:
            rec["longitudinal_provenance"]["posted_documents"][0]["nct_id"] = RT_HOLD

    _rt2_edit_record(out, RT_OK, edit)
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


@pytest.mark.parametrize(
    "case",
    [
        "component_versions_object",
        "holdout_document_badge_extra_key",
        "holdout_census_badge_extra_key",
        "in_scope_document_badge_disagrees_with_shard",
        "in_scope_profile_raw_page_text",
    ],
)
def test_redteam_m3_check_refuses_content_the_builder_never_writes(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """--check refuses what the builder never writes: an object in
    component_versions (the builder writes strings), an extra key in a
    holdout's document_badge or census_badge (here carrying its results), an
    in-scope document_badge that disagrees with the shard's document_profile,
    and raw PDF page text in a document_profile (refused on input). Round 2:
    each tree checks "ok", exit 0."""
    out = _rt2_built_tree(tmp_path)
    results = {"primary_outcome_measures": [{"title": "OS", "analyses": [{"paramValue": "0.68", "pValue": "<0.0001"}]}]}

    def edit_rows(idx: dict[str, Any]) -> None:
        for row in idx["data"]:
            if case == "holdout_document_badge_extra_key" and row["nct_id"] == RT_HOLD:
                row["document_badge"]["results"] = results
            elif case == "holdout_census_badge_extra_key" and row["nct_id"] == RT_HOLD:
                row["census_badge"]["results"] = results
            elif case == "in_scope_document_badge_disagrees_with_shard" and row["nct_id"] == RT_OK:
                row["document_badge"]["doc_count"] = 7

    if case == "component_versions_object":
        _rt2_edit_index(out, lambda idx: idx["component_versions"].__setitem__("extra", results))
    elif case == "in_scope_profile_raw_page_text":
        _rt2_edit_record(out, RT_OK, lambda rec: rec["document_profile"].__setitem__("page_texts", ["page 1 text"]))
    else:
        _rt2_edit_index(out, edit_rows)
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0
    assert RT_HOLD_DIGITS not in capsys.readouterr().err


@dataclasses.dataclass
class _Rt2Population:
    """A Module 2-style parsed outcome population (dataclass, not a mapping)."""

    outcome_index: int
    outcome_type: str
    population: str
    nct_id: str
    in_audit: bool


@dataclasses.dataclass
class _Rt2Populations:
    """A Module 2-style TrialAnalysisPopulations (dataclass with .outcomes)."""

    outcomes: list[_Rt2Population]


@pytest.mark.parametrize("entry_point", ["build_trial_bundle", "extract_reported_results"])
def test_redteam_m3_dataclass_parsed_populations_identity_checked(entry_point: str) -> None:
    """Module 2's parsed populations arrive as dataclasses (the module reads
    them through .outcomes and __dict__); one naming the holdout with
    in_audit true is refused like the same content as a mapping. Round 2: the
    identity walk and _refuse_holdout_flags enter only mappings, lists and
    tuples, so the dataclass passes and is published verbatim in the ordinary
    trial's reported_results ("nct_id": "NCT99000058", "in_audit": true)."""
    pops = _Rt2Populations(outcomes=[_Rt2Population(0, "PRIMARY", "ITT", RT_HOLD, True)])
    with pytest.raises(ol.OutcomesLayoutError):
        if entry_point == "build_trial_bundle":
            ol.build_trial_bundle(
                _make_ctgov_study(RT_OK), shards=8, in_audit=False, parsed_populations=pops, audit_nct_ids={RT_HOLD}
            )
        else:
            ol.extract_reported_results(_make_ctgov_study(RT_OK), parsed_populations=pops)


@pytest.mark.parametrize("entry_point", ["build_trial_bundle", "extract_reported_results"])
def test_redteam_m3_population_parser_output_identity_checked(entry_point: str) -> None:
    """The population_parser's result is a side input like parsed_populations
    and is held to the same rules. Round 2: it is computed after every check
    and published unchecked; a result naming the holdout with in_audit true
    is returned in the ordinary trial's reported_results."""

    def parser(_study: Any) -> dict[str, Any]:
        return {"outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "nct_id": RT_HOLD, "in_audit": True, "population": "ITT"}]}

    with pytest.raises(ol.OutcomesLayoutError):
        if entry_point == "build_trial_bundle":
            ol.build_trial_bundle(
                _make_ctgov_study(RT_OK), shards=8, in_audit=False, population_parser=parser, audit_nct_ids={RT_HOLD}
            )
        else:
            ol.extract_reported_results(_make_ctgov_study(RT_OK), population_parser=parser)


@pytest.mark.parametrize(
    "entry_point",
    ["build_trial_bundle_documents", "build_trial_bundle_locations", "check_side_input_identity", "cross_stream_guard"],
)
def test_redteam_m3_non_list_sequences_identity_checked(entry_point: str) -> None:
    """Side inputs accepted as any Sequence are read as one: a document
    profile or location rows given as a collections.UserList naming the
    holdout are refused, as the same rows in a list are. Round 2: the
    identity walk enters list and tuple only; the UserList passes
    check_side_input_identity and the vector-6 guard (both return None) and
    build_trial_bundle publishes the holdout's document URL or a note naming
    it in the ordinary trial's shard."""
    docs = _make_doc_profile(RT_OK)["documents"]
    docs[0]["url"] = RT_HOLD_URL
    locs = _make_complete_locations(RT_OK)
    locs[0]["note"] = f"see {RT_HOLD} SAP p.12"
    with pytest.raises(ol.OutcomesLayoutError):
        if entry_point == "build_trial_bundle_documents":
            ol.build_trial_bundle(
                _make_ctgov_study(RT_OK), shards=8, in_audit=False, audit_nct_ids={RT_HOLD},
                document_profile=collections.UserList(docs),
            )
        elif entry_point == "build_trial_bundle_locations":
            ol.build_trial_bundle(
                _make_ctgov_study(RT_OK), shards=8, in_audit=False, audit_nct_ids={RT_HOLD},
                document_profile=_make_doc_profile(RT_OK), decision_rule_locations=collections.UserList(locs),
            )
        elif entry_point == "check_side_input_identity":
            ol.check_side_input_identity(
                collections.UserList([{"nct_id": RT_HOLD, "in_audit": True}]),
                expected_nct_id=RT_OK, expected_in_audit=False, label="documents", audit_nct_ids={RT_HOLD},
            )
        else:
            ol.assert_no_audit_cross_stream_leakage(
                {RT_HOLD}, documents_by_nct={RT_OK: collections.UserList([{"nct_id": RT_HOLD, "in_audit": True}])}
            )


@pytest.mark.parametrize("key", ["documents", "large_docs", "registry_milestones", "outcomes", "primary_pass"])
def test_redteam_m3_holdout_unwrapped_side_content_refused_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    """Side-section content carried at the top of the holdout's own --trials
    record without its section wrapper (a profile's documents, Module 4's
    large_docs or registry_milestones, parsed population outcomes, a
    primary_pass) is holdout material, refused like the wrapped sections
    (exit 2). Round 2: HOLDOUT_INLINE_SECTION_FORMS names the wrappers only;
    exit 0, the content dropped without a word."""
    content: Any = {
        "documents": _make_doc_profile(RT_HOLD)["documents"],
        "large_docs": _make_module4_provenance(RT_HOLD)["large_docs"],
        "registry_milestones": _make_module4_provenance(RT_HOLD)["registry_milestones"],
        "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "population": "ITT", "n_analyzed": 238}],
        "primary_pass": [{"primary_outcome_index": 0, "element": "margin", "status": "NOT_APPLICABLE"}],
    }[key]
    holdout = _make_ctgov_study(RT_HOLD, in_audit=True)
    holdout[key] = content
    argv, out = _rt_build(tmp_path, studies=[_make_ctgov_study(RT_OK), holdout])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("key", ["llm_assessment", "paper_extraction", "model_id", "prompt_sha256", "effective_pass"])
def test_redteam_m3_holdout_llm_keys_inside_native_sections_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    """Vector 3 (no LLM, assessment or pass keys on a holdout) holds inside
    the CT.gov native sections too, apart from the two keys CT.gov's own
    schema uses (locations, citations). Round 2: raw_payload skips
    protocolSection, resultsSection, annotationSection, documentSection and
    derivedSection wholesale; protocolSection.outcomesModule.<key> on the
    holdout's record is dropped without a word, exit 0."""
    holdout = _make_ctgov_study(RT_HOLD, in_audit=True)
    holdout["protocolSection"]["outcomesModule"][key] = (
        [{"status": "FOUND"}] if key == "effective_pass" else {"hr": "0.68", "p": "<0.0001"}
    )
    argv, out = _rt_build(tmp_path, studies=[_make_ctgov_study(RT_OK), holdout])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("key", ["in_audit_holdout", "inAuditHoldout", "audit_holdout"])
def test_redteam_m3_in_scope_record_holdout_marker_spellings_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    """A nested key that says "in_audit holdout" (the very status the badges
    use for a holdout) set true in an ordinary trial's record contradicts the
    manifest, as a nested in_audit true does: exit 2. Round 2: only the
    in_audit / is_in_audit spellings are read; exit 0 and the key is published
    verbatim in the ordinary trial's reported_results.analyses."""
    study = _make_ctgov_study(RT_OK)
    study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]["analyses"][0][key] = True
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


def _rt2_one_trial_docs() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    return ol.build_outcomes_documents(
        [ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4)],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )


@pytest.mark.parametrize("family", ["geo", "sex_gender", "sponsors"])
def test_redteam_m3_partial_turned_symlink_before_staging_not_followed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    """If <out>.partial becomes a symlink into a family folder after the
    allowlist check (another process, during the pre-flight firewall pass),
    the staging step does not delete through it. Round 2: _remove_own_staging_dir
    lists the symlink's target and removes every entry in it (the family
    folder is emptied), then fails with NotADirectoryError at rmdir."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    out = tmp_path / "bundle"
    work = pathlib.Path(f"{out}.partial")
    idx, shard_docs = _rt2_one_trial_docs()
    real_assert = ol.assert_no_audit_leakage

    def _assert_then_swap(**kwargs: Any) -> None:
        if not os.path.lexists(work):
            work.symlink_to(data / family)
        real_assert(**kwargs)

    monkeypatch.setattr(ol, "assert_no_audit_leakage", _assert_then_swap)
    with contextlib.suppress(ol.OutcomesLayoutError, OSError):
        ol.write_outcomes_tree(out, idx, shard_docs)
    assert _rt_tree_hashes(data) == before


@pytest.mark.parametrize("family", ["geo", "sex_gender", "sponsors"])
def test_redteam_m3_partial_swapped_for_symlink_during_validation_not_followed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    """If <out>.partial is replaced by a symlink into a family folder while the
    staged tree is validated, nothing is deleted or written through it. Round
    2: the swap's re-check refuses the symlinked staging directory, and the
    cleanup in write_outcomes_tree's finally then follows the symlink and
    deletes every file of the family folder."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    out = tmp_path / "bundle"
    work = pathlib.Path(f"{out}.partial")
    idx, shard_docs = _rt2_one_trial_docs()
    real_validate = ol.validate_outcomes_tree

    def _validate_then_swap(path: Any, **kwargs: Any) -> ol.ValidationResult:
        result = real_validate(path, **kwargs)
        if pathlib.Path(path) == work and not os.path.islink(work):
            os.rename(work, tmp_path / "moved_partial")
            work.symlink_to(data / family)
        return result

    monkeypatch.setattr(ol, "validate_outcomes_tree", _validate_then_swap)
    with contextlib.suppress(ol.OutcomesLayoutError, OSError):
        ol.write_outcomes_tree(out, idx, shard_docs)
    assert _rt_tree_hashes(data) == before


def _rt2_two_versions(out: pathlib.Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """Write version 1 (one trial) to ``out``; return version 2 (two trials)."""
    idx_v1, shards_v1 = ol.build_outcomes_documents(
        [ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4)],
        extracted_at="2026-11-20T03:00:00+00:00",
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    ol.write_outcomes_tree(out, idx_v1, shards_v1)
    return ol.build_outcomes_documents(
        [
            ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4),
            ol.build_trial_bundle(_make_ctgov_study("NCT99000060", sponsor_class="NIH", phase="PHASE2"), shards=4),
        ],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )


@pytest.mark.parametrize("second_at", [1, 2])
def test_redteam_m3_second_interrupt_during_rollback_keeps_previous_shards(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    second_at: int,
) -> None:
    """A second Ctrl-C landing while the rollback runs (here between its
    undo steps, at the identity check of undo step ``second_at``) never
    deletes the previous bundle: the target is as it was, or the previous
    shards are retained in .partial for a person. Round 2: the interrupted
    rollback propagates a KeyboardInterrupt, write_outcomes_tree's finally
    removes .partial with _previous_shards in it, and the target is left with
    the previous index over the new shards (mixed versions), the previous
    shards gone."""
    out = tmp_path / "bundle"
    idx_v2, shards_v2 = _rt2_two_versions(out)
    before = _rt_tree_hashes(out)
    work = pathlib.Path(f"{out}.partial")
    real_replace = os.replace
    real_same = ol._is_same_directory
    state = {"first": False, "calls": 0}

    def _replace(src: Any, dst: Any) -> None:
        if not state["first"] and _rt_swap_step(src, dst, out) == "install_new_shards":
            state["first"] = True
            real_replace(src, dst)
            raise KeyboardInterrupt()
        real_replace(src, dst)

    def _same(path: pathlib.Path, identity: tuple[int, int]) -> bool:
        if state["first"]:
            state["calls"] += 1
            if state["calls"] == second_at:
                raise KeyboardInterrupt()
        return real_same(path, identity)

    monkeypatch.setattr(ol.os, "replace", _replace)
    monkeypatch.setattr(ol, "_is_same_directory", _same)
    with pytest.raises(BaseException):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    monkeypatch.setattr(ol.os, "replace", real_replace)
    monkeypatch.setattr(ol, "_is_same_directory", real_same)
    assert _rt_tree_hashes(out) == before or (work / ol.PREVIOUS_SHARDS_DIR).is_dir()


def test_redteam_m3_interrupted_cleanup_after_completed_swap_not_reported_as_failed_restore(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the swap has completed and only the removal of the superseded
    version from .partial is interrupted, the next write proceeds: the target
    is a whole, valid new bundle and the leftovers are the old version, not a
    failed restore's backup. Round 2: the next write raises
    RetainedBackupError naming _previous_shards and _previous_index.json as
    "the previous bundle's shards or index, retained after an earlier failed
    or interrupted restore" and tells the operator to move them back into the
    target, which would put the old version (or, if only one survived, a
    mixture) over the new bundle."""
    out = tmp_path / "bundle"
    idx_v2, shards_v2 = _rt2_two_versions(out)

    def _interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(ol.shutil, "rmtree", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    monkeypatch.undo()
    assert json.loads((out / ol.INDEX_FILE).read_text(encoding="utf-8"))["total_trials"] == 2
    assert ol.validate_outcomes_tree(out).ok
    result = ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert result.ok
    assert not pathlib.Path(f"{out}.partial").exists()


@pytest.mark.parametrize("section", ["document_profile", "longitudinal_provenance"])
def test_redteam_m3_in_scope_inline_section_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    section: str,
) -> None:
    """A side section carried inline in an ordinary trial's --trials record is
    either published or refused, never dropped. Round 2: exit 0, the shard's
    section is null and the inline section is gone without a word."""
    study = _make_ctgov_study(RT_OK)
    study[section] = _make_doc_profile(RT_OK) if section == "document_profile" else _make_module4_provenance(RT_OK)
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    rc = cli.main(argv)
    capsys.readouterr()
    if rc == 0:
        _, sdoc = _read_shard(out, RT_OK)
        assert sdoc["data"][RT_OK][section] is not None
    else:
        assert not out.exists()


@pytest.mark.parametrize(
    "case",
    ["document_profile_vs_documents", "provenance_vs_provenance", "inline_population_vs_stream", "prebuilt_results_vs_results_section"],
)
def test_redteam_m3_inline_and_stream_conflict_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """When an ordinary trial's --trials record carries a section inline and a
    side stream (or the record's own CT.gov section) gives a different one,
    the two conflict and the build is refused, never resolved silently.
    Round 2: exit 0 each time; the side stream's profile, provenance or
    population is published and the inline one dropped, and a pre-built
    reported_results replaces the record's resultsSection."""
    study = _make_ctgov_study(RT_OK)
    side: list[tuple[str, Any]] = []
    if case == "document_profile_vs_documents":
        study["document_profile"] = _make_doc_profile(RT_OK, sha=SHA_B)
        side = [("--documents", _make_doc_profile(RT_OK))]
    elif case == "provenance_vs_provenance":
        study["longitudinal_provenance"] = {**_make_module4_provenance(RT_OK), "provenance_rules_version": "9.9.9"}
        side = [("--provenance", _make_module4_provenance(RT_OK))]
    elif case == "inline_population_vs_stream":
        measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
        measure["analysis_population_parsed"] = {"population": "PP (inline)"}
        side = [("--population-parsed", {"nct_id": RT_OK, "outcomes": [{"outcome_index": 0, "outcome_type": "PRIMARY", "population": "ITT (stream)"}]})]
    else:
        study["reported_results"] = {"results_posted": True, "primary_outcome_measures": [{"title": "Pre-built"}]}
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)], side=side)
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_population_for_absent_outcome_refused_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --population-parsed row for a primary outcome index the trial does
    not have is refused, not dropped. Round 2: exit 0 and the row (here
    outcome_index 5, "mITT") is nowhere in the shard."""
    side = ("--population-parsed", {"nct_id": RT_OK, "outcomes": [{"outcome_index": 5, "outcome_type": "PRIMARY", "population": "mITT"}]})
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[side])
    _rt_assert_refused(cli.main(argv), out, capsys)


# ── m3 round-2 fixes: the choices the red-team tests leave open ──────────────
# Fictional IDs only (RT_OK ordinary, RT_HOLD the holdout).

def test_m3_fix_in_scope_inline_section_is_published_and_checks_ok(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ordinary trial's top-level document_profile is published as its
    section (the red-team test accepts publish or refuse; the builder
    publishes), the badge is computed from it, and --check accepts the tree."""
    study = _make_ctgov_study(RT_OK)
    study["document_profile"] = _make_doc_profile(RT_OK)
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    assert cli.main(argv) == 0
    _, sdoc = _read_shard(out, RT_OK)
    assert [d["filename"] for d in sdoc["data"][RT_OK]["document_profile"]["documents"]] == ["Prot_SAP_000.pdf"]
    row = {r["nct_id"]: r for r in json.loads((out / "index.json").read_text(encoding="utf-8"))["data"]}[RT_OK]
    assert row["document_badge"]["doc_count"] == 1
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) == 0


def test_m3_fix_section_key_the_builder_does_not_read_is_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A section key in an ordinary record where the builder does not read it
    (here nested in protocolSection) would be dropped, so it is refused."""
    study = _make_ctgov_study(RT_OK)
    study["protocolSection"]["document_profile"] = _make_doc_profile(RT_OK)
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)


@pytest.mark.parametrize("option", ["--documents", "--out-dir", "--shards", "--manifest-cohort"])
def test_m3_fix_check_refuses_build_only_options(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    """--check refuses an option only a build reads, rather than ignoring it."""
    out = _rt2_built_tree(tmp_path)
    value = {"--documents": str(tmp_path / "rt2_docs.json"), "--out-dir": str(tmp_path / "elsewhere"),
             "--shards": "8", "--manifest-cohort": "nih"}[option]
    capsys.readouterr()
    assert cli.main(["--check", str(out), option, value]) == 1
    assert option in capsys.readouterr().err


def test_m3_fix_check_against_an_agreeing_manifest_passes_and_a_partial_one_fails(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check --cohort-manifest passes when the manifest is the tree's own,
    and fails (counts only, no IDs) when the tree holds a trial it does not list."""
    out = _rt2_built_tree(tmp_path)
    same = _rt2_write(tmp_path / "rt2_same.json", {"rows": _RT2_ROWS, "run_id": "fictional-run"})
    capsys.readouterr()
    assert cli.main(["--check", str(out), "--cohort-manifest", str(same)]) == 0
    partial = _rt2_write(tmp_path / "rt2_partial.json", [_RT2_ROWS[1]])
    capsys.readouterr()
    assert cli.main(["--check", str(out), "--cohort-manifest", str(partial)]) == 1
    err = capsys.readouterr().err
    assert "1 trial(s) in it the manifest does not list" in err
    assert RT_HOLD_DIGITS not in err and RT_OK[3:] not in err


def test_m3_fix_manifest_excluded_list_naming_a_row_is_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live index's "excluded" list is read, not dropped: a trial both
    listed as a row and excluded is refused, and an excluded entry carrying
    anything but its ID and reason(s) is refused (exit 2)."""
    both = {"rows": _RT2_ROWS, "excluded": [{"nct_id": RT_OK, "reasons": ["fictional"]}]}
    argv, out = _rt2_argv(tmp_path, manifest=both, trials=_rt_pair())
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=1)
    extra = {"rows": _RT2_ROWS, "excluded": [{"nct_id": "NCT99300099", "reason": "x", "results": {"hr": "0.68"}}]}
    argv, out = _rt2_argv(tmp_path, manifest=extra, trials=_rt_pair())
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("fd_safe", [True, False])
def test_m3_fix_staging_is_never_removed_through_a_symlink_on_either_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fd_safe: bool,
) -> None:
    """Both ways of removing the staging directory (by descriptor where the
    platform allows it, by re-checked path elsewhere) refuse a .partial that
    was swapped for a symlink into a family folder, and both clean up a
    completed swap's leftovers on the next write."""
    monkeypatch.setattr(ol, "_FD_SAFE", fd_safe)
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    out = tmp_path / "bundle"
    work = pathlib.Path(f"{out}.partial")
    idx, shard_docs = _rt2_one_trial_docs()
    real_validate = ol.validate_outcomes_tree

    def _validate_then_swap(path: Any, **kwargs: Any) -> ol.ValidationResult:
        result = real_validate(path, **kwargs)
        if pathlib.Path(path) == work and not os.path.islink(work):
            os.rename(work, tmp_path / "moved_partial")
            work.symlink_to(data / "geo")
        return result

    monkeypatch.setattr(ol, "validate_outcomes_tree", _validate_then_swap)
    with pytest.raises(ol.OutcomesLayoutError):
        ol.write_outcomes_tree(out, idx, shard_docs)
    assert _rt_tree_hashes(data) == before
    monkeypatch.setattr(ol, "validate_outcomes_tree", real_validate)
    work.unlink()

    idx_v2, shards_v2 = _rt2_two_versions(out)

    def _interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(ol.shutil, "rmtree", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert (work / ol.SWAP_COMPLETE_FILE).is_file()
    assert ol.write_outcomes_tree(out, idx_v2, shards_v2).ok
    assert not work.exists()


# ── Red team m3, round 3 (on 626100d with m4 17b5705 merged) ─────────────────
# Fictional IDs only (RT_OK ordinary, RT_HOLD the holdout), as in rounds 1-2.
# Each test asserts the correct behaviour. A test for a bypass found in round
# 3 fails until the module is fixed; the others guard attacks that the code
# already refuses. Write-safety tests use fictional replicas of the site's
# family folders (file names only), never a site checkout.

_RT3_SECRET = "HR 0.68 (95% CI 0.55-0.84)"
_RT3_SECRET_MARK = "0.68"
_RT3_STRAY = b"operator file, not a shard of this bundle\n"


def _rt3_pdf_row() -> dict[str, Any]:
    """A document row as PDF inspection writes it, carried without a wrapper key."""
    return {
        "filename": "Prot_SAP_000.pdf",
        "doc_type": "combined",
        "sha256": SHA_B,
        "physical_pages": 112,
        "readability_status": "machine_readable",
        "pages_without_text": [1],
        "version_label": "Protocol Amendment 3.0",
        "version_date_as_printed": "12 March 2019",
    }


def _rt3_holdout_carrying(case: str) -> dict[str, Any]:
    """The holdout's own CT.gov-shaped record carrying side material in ``case``."""
    study = _make_ctgov_study(RT_HOLD, in_audit=True)
    if case == "native_primary_pass":
        study["protocolSection"]["outcomesModule"]["primary_pass"] = [
            {
                "primary_outcome_index": 0,
                "element": "margin",
                "status": "FOUND",
                "pages_searched": "all",
                "pages_not_searched": [],
                "citations": [_make_citation(sha256=SHA_B)],
            }
        ]
    elif case == "native_large_docs_pdf_scraped":
        study["documentSection"] = {
            "largeDocumentModule": {
                "largeDocs": [
                    {
                        "typeAbbrev": "Prot_SAP",
                        "hasProtocol": True,
                        "hasSap": True,
                        "filename": "Prot_SAP_000.pdf",
                        "sha256": SHA_B,
                        "version_label": "Protocol Amendment 3.0",
                        "version_date_as_printed": "12 March 2019",
                    }
                ]
            }
        }
    elif case == "native_results_documents":
        study["resultsSection"]["documents"] = [_rt3_pdf_row()]
    elif case == "unlisted_wrapper_document_rows":
        study["pdf_index"] = [_rt3_pdf_row()]
    else:
        study["rule_census"] = [
            {
                "primary_outcome_index": 0,
                "element": "margin",
                "status": "NOT_FOUND",
                "pages_searched": "all",
                "pages_not_searched": [],
            }
        ]
    return study


@pytest.mark.parametrize(
    "case",
    [
        "native_primary_pass",
        "native_large_docs_pdf_scraped",
        "native_results_documents",
        "unlisted_wrapper_document_rows",
        "unlisted_wrapper_location_rows",
    ],
)
def test_redteam_m3_holdout_material_in_native_sections_or_unlisted_wrappers_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Holdout material in the holdout's own --trials record is refused (exit
    2), never dropped, wherever it sits: decision-rule location rows under
    primary_pass inside protocolSection, PDF-scraped sha256/version_label/
    version_date_as_printed on CT.gov's largeDocs, document rows under
    resultsSection.documents, and document or location rows under a wrapper
    key the module does not list. Round 3: exit 0 each time, the stub is
    written and the material is dropped without a word (native sections are
    read only for wrapped forms and vector-3 keys, where primary_pass,
    citations and the PDF keys are not; the unwrapped-content list has none
    of the per-row keys)."""
    argv, out = _rt_build(tmp_path, studies=[_make_ctgov_study(RT_OK), _rt3_holdout_carrying(case)])
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


def test_redteam_m3_holdout_manifest_row_material_under_unlisted_wrapper_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same holdout material carried on the holdout's manifest row (no
    --trials) is refused. Round 3: exit 0, dropped."""
    rows = [
        {"nct_id": RT_OK, "in_audit": False, "cohorts": ["nih"]},
        {"nct_id": RT_HOLD, "in_audit": True, "cohorts": ["nih"], "pdf_index": [_rt3_pdf_row()]},
    ]
    argv, out = _rt2_argv(tmp_path, manifest=rows)
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


_RT3_HOLDOUT_VALUE_CASES: dict[str, tuple[str, Any]] = {
    "enrollment_object": ("enrollment", {"primary_result": _RT3_SECRET}),
    "primary_outcomes_count_text": ("primary_outcomes_count", _RT3_SECRET),
    "cohorts_object": ("cohorts", {"primary_result": _RT3_SECRET}),
    "cohorts_bad_slug": ("cohorts", [_RT3_SECRET]),
    "ecosystem_category_text": ("ecosystem_category", _RT3_SECRET),
}


@pytest.mark.parametrize("case", [*_RT3_HOLDOUT_VALUE_CASES, "protocol_text_in_both_inputs"])
def test_redteam_m3_build_refusal_does_not_quote_holdout_record_values(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """A refusal about a holdout's record never quotes the record's values: a
    holdout's results text placed in enrollment, primary_outcomes_count,
    cohorts, ecosystem_category or protocol is refused without being printed.
    Round 3: the ID is withheld but the value is quoted, e.g. "ERROR: <NCT id
    withheld>.enrollment must be an int or None, got dict: {'primary_result':
    'HR 0.68 (95% CI 0.55-0.84)'}" (and "AUDIT FIREWALL BLOCKED:
    trials.protocol must be an explicit bool ..., got str: 'HR 0.68 ...'")."""
    hold = _make_ctgov_study(RT_HOLD, in_audit=True)
    rows: list[dict[str, Any]] | None = None
    if case == "protocol_text_in_both_inputs":
        hold["protocol"] = _RT3_SECRET
        rows = [{"nct_id": RT_OK, "in_audit": False}, {"nct_id": RT_HOLD, "in_audit": True, "protocol": True}]
    else:
        key, value = _RT3_HOLDOUT_VALUE_CASES[case]
        hold[key] = value
    argv, out = _rt_build(tmp_path, studies=[_make_ctgov_study(RT_OK), hold], manifest_rows=rows)
    rc = cli.main(argv)
    err = capsys.readouterr().err
    assert rc != 0
    assert not out.exists()
    assert _RT3_SECRET_MARK not in err
    assert RT_HOLD_DIGITS not in err


@pytest.mark.parametrize(
    "field",
    ["has_reported_results", "document_badge.doc_count", "census_badge.status_counts", "cohorts", "shard_record.cohorts"],
)
def test_redteam_m3_check_does_not_quote_holdout_values(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    """--check reports a holdout row or stub carrying content without quoting
    that content, on stderr or in --report. Round 3: the problem quotes it,
    e.g. "... index_row.has_reported_results must be None, got
    {'primary_result': 'HR 0.68 (95% CI 0.55-0.84)'}", and the report file
    holds the same text."""
    out = _rt2_built_tree(tmp_path)
    leaked = {"primary_result": _RT3_SECRET}
    if field == "shard_record.cohorts":
        _rt2_edit_record(out, RT_HOLD, lambda rec: rec.__setitem__("cohorts", leaked))
    else:
        *parents, last = field.split(".")

        def edit(idx: dict[str, Any]) -> None:
            for row in idx["data"]:
                if row["nct_id"] == RT_HOLD:
                    node = row
                    for part in parents:
                        node = node[part]
                    node[last] = leaked

        _rt2_edit_index(out, edit)
    report = tmp_path / "rt3_check_report.json"
    capsys.readouterr()
    assert cli.main(["--check", str(out), "--report", str(report)]) != 0
    err = capsys.readouterr().err
    assert _RT3_SECRET_MARK not in err
    assert RT_HOLD_DIGITS not in err
    if report.exists():
        text = report.read_text(encoding="utf-8")
        assert _RT3_SECRET_MARK not in text
        assert RT_HOLD_DIGITS not in text


@pytest.mark.parametrize("entry_point", ["validate_outcomes_tree", "write_outcomes_tree", "build_outcomes_documents"])
def test_redteam_m3_library_firewall_refusals_withhold_holdout_id(tmp_path: pathlib.Path, entry_point: str) -> None:
    """The library entry points that refuse a holdout leak withhold its ID,
    as the CLI does. Round 3: validate_outcomes_tree(raise_on_audit_leakage=
    True), write_outcomes_tree and build_outcomes_documents raise
    "NCT99000058: in_audit holdout leak (vector 5) — document_badge.doc_count
    must be None, got 3"; only the CLI wrapper withholds it."""
    with pytest.raises(ol.AuditFirewallError) as excinfo:
        if entry_point == "validate_outcomes_tree":
            out = _rt2_built_tree(tmp_path)

            def edit(idx: dict[str, Any]) -> None:
                for row in idx["data"]:
                    if row["nct_id"] == RT_HOLD:
                        row["document_badge"]["doc_count"] = 3

            _rt2_edit_index(out, edit)
            ol.validate_outcomes_tree(out, raise_on_audit_leakage=True)
        else:
            stub_row, stub_rec = ol.build_trial_bundle(_make_ctgov_study(RT_HOLD, in_audit=True), shards=4)
            stub_row = {**stub_row, "document_badge": {**stub_row["document_badge"], "doc_count": 3}}
            if entry_point == "build_outcomes_documents":
                ol.build_outcomes_documents(
                    [(stub_row, stub_rec)], extracted_at=SAMPLE_TS, source_pipeline_commit=SAMPLE_COMMIT, shards=4
                )
            else:
                idx, shard_docs = _rt2_one_trial_docs()
                idx["data"].append(stub_row)
                ol.write_outcomes_tree(tmp_path / "bundle", idx, shard_docs)
    assert RT_HOLD_DIGITS not in str(excinfo.value)


@pytest.mark.parametrize("where", ["trials_record", "provenance"])
@pytest.mark.parametrize("key", ["held_out", "heldOut", "is_held_out", "in_audit_set"])
def test_redteam_m3_holdout_marker_spellings_on_ordinary_trial_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
    where: str,
) -> None:
    """A record that marks an ordinary trial as held out under another
    spelling (held_out, heldOut, is_held_out, in_audit_set) contradicts the
    manifest, as in_audit_holdout does: exit 2. Round 3: exit 0 and the key
    is published verbatim (in the shard's longitudinal_provenance for the
    provenance case)."""
    study = _make_ctgov_study(RT_OK)
    side: list[tuple[str, Any]] = []
    if where == "trials_record":
        study[key] = True
    else:
        prov = _make_module4_provenance(RT_OK)
        prov["registry_milestones"][key] = True
        side = [("--provenance", prov)]
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)], side=side)
    _rt_assert_refused(cli.main(argv), out, capsys, expected_rc=2)


@pytest.mark.parametrize("case", ["profile_in_audit_true", "document_row_in_audit_true", "rows_of_two_trials"])
def test_redteam_m3_normalize_document_profile_refuses_holdout_or_mixed_input(case: str) -> None:
    """normalize_document_profile, a public section builder, refuses a
    profile or document row that says it is a holdout's (AuditFirewallError),
    as the provenance, locations and results builders do, and refuses rows of
    two trials in one profile when no expected trial is given. Round 3: all
    three return a profile (the mixed one holds the holdout's document)."""
    raw: Any
    expected: type[Exception]
    if case == "profile_in_audit_true":
        raw, expected = {**_make_doc_profile(RT_HOLD), "in_audit": True}, ol.AuditFirewallError
    elif case == "document_row_in_audit_true":
        raw, expected = [{**_make_doc_profile(RT_HOLD)["documents"][0], "in_audit": True}], ol.AuditFirewallError
    else:
        raw = [
            _make_doc_profile(RT_OK)["documents"][0],
            {**_make_doc_profile(RT_HOLD)["documents"][0], "filename": "Prot_001.pdf"},
        ]
        expected = ol.OutcomesLayoutError
    with pytest.raises(expected):
        ol.normalize_document_profile(raw)


@pytest.mark.parametrize("case", ["measure_text", "inline_population"])
def test_redteam_m3_population_parser_edits_to_the_record_not_published_unchecked(case: str) -> None:
    """A population parser that edits the record it is given (here writing
    the holdout's ID into a measure) cannot publish the edit past the
    identity checks: the build is refused or the edit is not in the bundle.
    Round 3: the parser runs after check_trial_record_identity on the
    builder's working copy, and its edit is published, holdout ID included,
    though audit_nct_ids names the holdout."""

    def _parser(study: dict[str, Any]) -> None:
        measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
        if case == "measure_text":
            measure["description"] = f"Pooled with {RT_HOLD}: {_RT3_SECRET}"
        else:
            measure["analysis_population_parsed"] = {"population": "copied", "nct_id": RT_HOLD}

    try:
        bundle = ol.build_trial_bundle(
            _make_ctgov_study(RT_OK),
            shards=8,
            in_audit=False,
            population_parser=_parser,
            audit_nct_ids=frozenset({RT_HOLD}),
        )
    except ol.OutcomesLayoutError:
        return
    assert RT_HOLD not in json.dumps(bundle)


def test_redteam_m3_explicit_cohort_slug_naming_holdout_refused() -> None:
    """build_trial_bundle's explicit_cohorts are scanned like the record: a
    cohort slug spelling the holdout's ID is refused when audit_nct_ids names
    it. Round 3: the slug is published in the ordinary trial's cohorts (the
    CLI's --manifest-cohort is caught, but only by the staged-tree check)."""
    with pytest.raises(ol.AuditFirewallError):
        ol.build_trial_bundle(
            _make_ctgov_study(RT_OK),
            shards=8,
            in_audit=False,
            explicit_cohorts=[RT_HOLD.lower()],
            audit_nct_ids=frozenset({RT_HOLD}),
        )


@pytest.mark.parametrize("with_ordinary_trial", [True, False])
def test_redteam_m3_manifest_cohort_spelling_holdout_id_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    with_ordinary_trial: bool,
) -> None:
    """--manifest-cohort spelling the holdout's ID (a valid slug) is refused,
    with or without an ordinary trial in the build (exit 2, nothing written).
    Guard: refused in round 3."""
    studies = _rt_pair() if with_ordinary_trial else [_make_ctgov_study(RT_HOLD, in_audit=True)]
    argv, out = _rt_build(tmp_path, studies=studies)
    _rt_assert_refused(cli.main([*argv, "--manifest-cohort", RT_HOLD.lower()]), out, capsys, expected_rc=2)


@pytest.mark.parametrize("swap", ["report_path", "report_parent"])
def test_redteam_m3_report_swapped_for_symlink_after_check_not_written_through(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    """If the --report path, or its parent directory, becomes a symlink into a
    family folder after write_report's check (another process), the report is
    not written through it. Round 3: write_text follows the link; the geo
    family's active_run.json is overwritten with the report, or a report.json
    is created in the sponsors family."""
    data = _rt_site_replica(tmp_path)
    before = _rt_tree_hashes(data)
    report = tmp_path / "reports" / "report.json"
    real_check = ol.check_report_path

    def _check_then_swap(path: Any, **kwargs: Any) -> pathlib.Path:
        checked = real_check(path, **kwargs)
        if sys._getframe(1).f_code.co_name == "write_report" and not os.path.lexists(tmp_path / "reports" / "report.json"):
            if swap == "report_path":
                report.parent.mkdir(parents=True, exist_ok=True)
                report.symlink_to(data / "geo" / "active_run.json")
            elif not os.path.lexists(tmp_path / "reports"):
                (tmp_path / "reports").symlink_to(data / "sponsors")
        return checked

    monkeypatch.setattr(ol, "check_report_path", _check_then_swap)
    with contextlib.suppress(ol.OutcomesLayoutError, OSError):
        cli.main(_rt_manifest_only(tmp_path, tmp_path / "bundle", "--report", str(report)))
    assert _rt_tree_hashes(data) == before


def test_redteam_m3_leftover_staging_directory_is_not_a_bundle_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staging directory left by a killed build (it holds both markers) is
    not a bundle target: building into <out>.partial is refused, or the bundle
    built there survives the next build of <out>. Round 3: check_output_target
    takes the leftover for a bundle (it holds the bundle marker), a valid
    two-trial bundle is built into it (--check ok), and the next build of
    <out> deletes it whole as its own leftover staging directory."""
    out = tmp_path / "bundle"
    work = pathlib.Path(f"{out}.partial")
    idx, shard_docs = _rt2_one_trial_docs()

    def _killed(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt()

    def _no_cleanup(*_args: Any, **_kwargs: Any) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(ol, "_swap_into_target", _killed)
        patch.setattr(ol, "_cleanup_staging", _no_cleanup)
        with pytest.raises(KeyboardInterrupt):
            ol.write_outcomes_tree(out, idx, shard_docs)
    assert (work / ol.STAGING_MARKER_FILE).is_file()
    idx_v2, shards_v2 = ol.build_outcomes_documents(
        [
            ol.build_trial_bundle(_make_ctgov_study(RT_OK), shards=4),
            ol.build_trial_bundle(_make_ctgov_study("NCT99000060", sponsor_class="NIH", phase="PHASE2"), shards=4),
        ],
        extracted_at=SAMPLE_TS,
        source_pipeline_commit=SAMPLE_COMMIT,
        shards=4,
    )
    try:
        ol.write_outcomes_tree(work, idx_v2, shards_v2)
    except ol.OutcomesLayoutError:
        return
    built = _rt_tree_hashes(work)
    with contextlib.suppress(ol.OutcomesLayoutError):
        ol.write_outcomes_tree(out, idx, shard_docs)
    assert work.is_dir()
    assert _rt_tree_hashes(work) == built


@pytest.mark.parametrize("name", ["1٣.json.gz", "1３.json.gz", "4.json.gz"])
def test_redteam_m3_shards_entry_not_named_by_the_bundle_layout_not_deleted(tmp_path: pathlib.Path, name: str) -> None:
    """A file in an existing bundle's shards/ that the bundle's own layout
    never names (non-ASCII digits, or an index beyond its shard count; --check
    calls each a stray file) is not deleted by the next write. Round 3:
    SHARD_FILENAME_RE's \\d admits other scripts' digits and any index, so
    the file is moved with the old shards and deleted without a word."""
    out = tmp_path / "bundle"
    idx_v2, shards_v2 = _rt2_two_versions(out)
    stray = out / "shards" / name
    stray.write_bytes(_RT3_STRAY)
    assert not ol.validate_outcomes_tree(out).ok
    with contextlib.suppress(ol.OutcomesLayoutError):
        ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert stray.read_bytes() == _RT3_STRAY


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_redteam_m3_interrupt_during_index_backup_copy_not_reported_as_failed_restore(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """Where hard links are unavailable, an interrupt during the byte copy of
    the previous index (before any rename) leaves the target as it was, lets
    the interrupt propagate, and does not tell the next run to restore the
    half-written copy. Round 3: the interrupt is turned into a
    RetainedBackupError saying the previous bundle "could not be put back",
    .partial is kept, and the next run refuses with an instruction to move
    the truncated _previous_index.json over the intact index.json."""
    out = tmp_path / "bundle"
    idx_v2, shards_v2 = _rt2_two_versions(out)
    before = _rt_tree_hashes(out)

    def _no_hard_links(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(1, "hard links are not supported here")

    def _copy_half(src: Any, dst: Any, **_kwargs: Any) -> None:
        data = pathlib.Path(src).read_bytes()
        pathlib.Path(dst).write_bytes(data[: len(data) // 2])
        raise interrupt()

    with monkeypatch.context() as patch:
        patch.setattr(ol.os, "link", _no_hard_links)
        patch.setattr(ol.shutil, "copyfile", _copy_half)
        with pytest.raises(BaseException) as excinfo:
            ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert isinstance(excinfo.value, interrupt)
    assert _rt_tree_hashes(out) == before
    ol.check_output_target(out)


@pytest.mark.parametrize("point", ["index_link", "completion_record"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_redteam_m3_interrupt_after_index_link_or_completion_record_restores_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    interrupt: type[BaseException],
) -> None:
    """An interrupt right after the previous index is hard-linked, or right
    after the completion record is written (the two swap steps that are not
    an os.replace), leaves the target byte-for-byte as it was and no .partial.
    Guard: restored in round 3."""
    out = tmp_path / "bundle"
    idx_v2, shards_v2 = _rt2_two_versions(out)
    before = _rt_tree_hashes(out)
    real_link = os.link
    real_write = ol._write_new_files

    def _link_then_interrupt(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        raise interrupt()

    def _write_then_interrupt(directory: pathlib.Path, identity: tuple[int, int], files: Any) -> None:
        items = list(files)
        real_write(directory, identity, items)
        if any(name == ol.SWAP_COMPLETE_FILE for name, _data in items):
            raise interrupt()

    with monkeypatch.context() as patch:
        if point == "index_link":
            patch.setattr(ol.os, "link", _link_then_interrupt)
        else:
            patch.setattr(ol, "_write_new_files", _write_then_interrupt)
        with pytest.raises(interrupt):
            ol.write_outcomes_tree(out, idx_v2, shards_v2)
    assert _rt_tree_hashes(out) == before
    assert not pathlib.Path(f"{out}.partial").exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_redteam_m3_non_finite_number_never_written_into_a_shard(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    value: float,
) -> None:
    """A NaN or Infinity in an ordinary trial's results (Python's json writes
    and reads both) is refused, or written as strict JSON: a browser's
    JSON.parse rejects the NaN token and with it every trial in the shard.
    Round 3: exit 0 and the shard holds a bare NaN / Infinity token."""
    study = _make_ctgov_study(RT_OK)
    measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["classes"] = [{"categories": [{"measurements": [{"groupId": "OG000", "value": value}]}]}]
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    rc = cli.main(argv)
    capsys.readouterr()
    if rc != 0:
        assert not out.exists()
        return
    path, _ = _read_shard(out, RT_OK)

    def _refuse_constant(token: str) -> Any:
        raise ValueError(f"non-finite JSON constant {token}")

    json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"), parse_constant=_refuse_constant)


def test_redteam_m3_check_refuses_non_finite_number_in_shard(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check refuses a shard holding a NaN token (not JSON a browser
    parses). Round 3: "outcomes tree check: ok", exit 0."""
    out = _rt2_built_tree(tmp_path)
    _rt2_edit_record(
        out,
        RT_OK,
        lambda rec: rec["reported_results"]["primary_outcome_measures"][0].__setitem__(
            "classes", [{"categories": [{"measurements": [{"groupId": "OG000", "value": float("nan")}]}]}]
        ),
    )
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0


def _rt3_tree_with_locations(tmp_path: pathlib.Path) -> pathlib.Path:
    """A CLI-built tree: the ordinary trial with documents and a complete
    primary pass of decision-rule locations, and the holdout's stub."""
    argv, out = _rt2_argv(
        tmp_path,
        manifest=_RT2_ROWS,
        trials=_rt_pair(),
        extra=[
            "--documents", str(_rt2_write(tmp_path / "rt3_docs.json", _make_doc_profile(RT_OK))),
            "--locations", str(_rt2_write(tmp_path / "rt3_locs.json", _make_complete_locations(RT_OK))),
        ],
    )
    assert cli.main(argv) == 0
    return out


@pytest.mark.parametrize("case", ["extra_row_key", "note_object", "title_object", "worker_object"])
def test_redteam_m3_check_refuses_location_rows_the_builder_never_writes(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """--check holds decision_rule_locations rows to what the builder writes:
    no key it never writes (here llm_assessment carrying results), and text
    (or null) in note, primary_outcome_title and worker, never an object.
    Round 3: each tree checks "ok", exit 0 (the round-2 fix holds the other
    sections to a normalizer fixed point, but location rows' own keys and
    values are not read)."""
    out = _rt3_tree_with_locations(tmp_path)
    leaked = {"primary_result": _RT3_SECRET}
    field = {"extra_row_key": "llm_assessment", "note_object": "note", "title_object": "primary_outcome_title", "worker_object": "worker"}[case]

    def edit(rec: dict[str, Any]) -> None:
        for pass_key in ("primary_pass", "effective_pass"):
            rec["decision_rule_locations"][pass_key][0][field] = leaked

    _rt2_edit_record(out, RT_OK, edit)
    capsys.readouterr()
    assert cli.main(["--check", str(out)]) != 0


@pytest.mark.parametrize("case", ["module4_model_id_and_prompt_version", "unknown_row_key", "note_object"])
def test_redteam_m3_location_row_content_not_dropped_or_stringified(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """A location row's content is published as given or refused, never
    dropped or turned into its str(): Module 4 requires model_id and
    prompt_version on every census location row (LOCATION_REQUIRED_KEYS;
    the model id is the record of a non-deterministic step), so they are
    published; an unknown key is kept or refused; an object in note is
    refused. Round 3: exit 0; model_id, prompt_version and the unknown key
    are dropped, and the note is published as "{'primary_result': ...}"."""
    rows = _make_complete_locations(RT_OK)
    for row in rows:
        if case == "module4_model_id_and_prompt_version":
            row["model_id"] = "fictional-model-2026-01-01"
            row["prompt_version"] = "fictional-census-prompt-v9"
        elif case == "unknown_row_key":
            row["confidence"] = 0.93
        else:
            row["note"] = {"primary_result": _RT3_SECRET}
    argv, out = _rt_build(
        tmp_path,
        studies=_rt_pair(),
        side=[("--documents", _make_doc_profile(RT_OK)), ("--locations", rows)],
    )
    rc = cli.main(argv)
    capsys.readouterr()
    if case == "module4_model_id_and_prompt_version":
        assert rc == 0
    if rc != 0:
        assert not out.exists()
        return
    _, sdoc = _read_shard(out, RT_OK)
    published = sdoc["data"][RT_OK]["decision_rule_locations"]["primary_pass"]
    for row in published:
        if case == "module4_model_id_and_prompt_version":
            assert row.get("model_id") == "fictional-model-2026-01-01"
            assert row.get("prompt_version") == "fictional-census-prompt-v9"
        elif case == "unknown_row_key":
            assert row.get("confidence") == 0.93
        else:
            assert row["note"] != str({"primary_result": _RT3_SECRET})


def test_redteam_m3_location_rows_for_absent_outcome_index_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Decision-rule location rows for a primary outcome index the trial does
    not have (here 5, of a trial with one primary outcome) are refused, as a
    parsed population for such an index is. Round 3: exit 0, the rows are
    published and the census badge reads partial with seven FOUND cells."""
    rows = _make_complete_locations(RT_OK)
    for row in rows:
        row["primary_outcome_index"] = 5
    argv, out = _rt_build(
        tmp_path,
        studies=_rt_pair(),
        side=[("--documents", _make_doc_profile(RT_OK)), ("--locations", rows)],
    )
    _rt_assert_refused(cli.main(argv), out, capsys)


@pytest.mark.parametrize("key", ["documents", "registry_milestones", "primary_pass"])
def test_redteam_m3_unwrapped_side_content_in_ordinary_record_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    """Side content carried at the top of an ordinary trial's --trials record
    without its wrapper (Module 1 documents, Module 4 registry_milestones, a
    primary_pass of location rows) is published or refused, never dropped,
    as it is refused on a holdout's record. Round 3: exit 0 and the shard's
    section is null; the content is gone without a word."""
    content = {
        "documents": _make_doc_profile(RT_OK)["documents"],
        "registry_milestones": _make_module4_provenance(RT_OK)["registry_milestones"],
        "primary_pass": _make_complete_locations(RT_OK),
    }[key]
    study = _make_ctgov_study(RT_OK)
    study[key] = content
    argv, out = _rt_build(tmp_path, studies=[study, _make_ctgov_study(RT_HOLD, in_audit=True)])
    rc = cli.main(argv)
    capsys.readouterr()
    if rc != 0:
        assert not out.exists()
        return
    _, sdoc = _read_shard(out, RT_OK)
    section = {"documents": "document_profile", "registry_milestones": "longitudinal_provenance", "primary_pass": "decision_rule_locations"}[key]
    assert sdoc["data"][RT_OK][section] is not None


def test_redteam_m3_document_badge_total_pages_not_an_undercount() -> None:
    """When a document's page count is unknown (download_failed: None), the
    badge's total_physical_pages is unknown (None), as Module 1's own badge
    states it, not the sum of the others. Round 3: doc_count 2 and
    total_physical_pages 40, the one known document's pages presented as the
    trial's total."""
    profile = _make_doc_profile(RT_OK)
    failed = {
        **profile["documents"][0],
        "filename": "ICF_001.pdf",
        "doc_type": "protocol",
        "http_status": 404,
        "sha256": None,
        "bytes": None,
        "physical_pages": None,
        "readability_status": "download_failed",
        "pages_selected": None,
        "pages_deferred": None,
    }
    profile["documents"].append(failed)
    profile["readability_summary"] = "partial_image_or_redacted"
    badge = ol.build_document_badge(in_audit=False, document_profile=ol.normalize_document_profile(profile, expected_nct_id=RT_OK))
    assert badge["doc_count"] == 2
    assert badge["total_physical_pages"] is None


@pytest.mark.parametrize("case", ["extraction_failed", "download_error_reason"])
def test_redteam_m3_module1_document_states_and_reasons_kept(case: str) -> None:
    """Module 1's own document vocabulary is read: an extraction_failed
    document (bytes obtained, no page extracted) and profile keep that state,
    and the reason Module 1 records for a document's absence
    (download_error, extraction_error) is published with it. Round 3:
    extraction_failed is refused ("not in READABILITY_STATUSES"), and
    download_error is dropped, so download_failed is published without its
    reason."""
    profile = _make_doc_profile(RT_OK)
    doc = profile["documents"][0]
    if case == "extraction_failed":
        doc.update(readability_status="extraction_failed", physical_pages=None, extraction_error="no provider extracted a page")
        profile["readability_summary"] = "extraction_failed"
    else:
        doc.update(
            readability_status="download_failed", http_status=404, sha256=None, bytes=None, physical_pages=None,
            pages_selected=None, pages_deferred=None, download_error="HTTP 404 from the registry CDN",
        )
        profile["readability_summary"] = "download_failed"
    norm = ol.normalize_document_profile(profile, expected_nct_id=RT_OK)
    if case == "extraction_failed":
        assert norm["readability_summary"] == "extraction_failed"
        assert norm["documents"][0]["readability_status"] == "extraction_failed"
        assert "no provider extracted a page" in json.dumps(norm)
    else:
        assert "HTTP 404 from the registry CDN" in json.dumps(norm)


@pytest.mark.parametrize(
    "case", ["posted_documents_vs_large_docs", "publications_vs_linked_publications", "date_deltas_vs_delta"]
)
def test_redteam_m3_provenance_spellings_that_disagree_refused(case: str) -> None:
    """A provenance record carrying a section under both of its spellings
    (posted_documents and Module 4's large_docs; publications and
    linked_publications; date_deltas and primary_completion_to_results_delta)
    with different content is refused, not resolved by precedence. Round 3:
    the first spelling is published and the other dropped without a word."""
    prov = _make_module4_provenance(RT_OK)
    if case == "posted_documents_vs_large_docs":
        prov["posted_documents"] = [{"filename": "Other_000.pdf"}]
    elif case == "publications_vs_linked_publications":
        prov["publications"] = [{"title": "A different publication"}]
    else:
        prov["date_deltas"] = {"primary_completion_to_results": {"exact_days": 1}}
    with pytest.raises(ol.OutcomesLayoutError):
        ol.normalize_longitudinal_provenance(prov)


def test_redteam_m3_side_section_provenance_stamps_published(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The provenance stamps of each side section (a Module 1 profile's
    extracted_at and pipeline_commit, a Module 4 record's assembled_at and
    source_pipeline_commit) reach the published tree, so a section cannot
    outrun its source unnoticed. Round 3: all four are dropped; the bundle
    carries only its own build stamps."""
    profile = {**_make_doc_profile(RT_OK), "pipeline_commit": "deadbee", "extracted_at": "2026-02-03T04:05:06+00:00"}
    prov = {**_make_module4_provenance(RT_OK), "source_pipeline_commit": "c0ffee1", "assembled_at": "2026-01-02T03:04:05+00:00"}
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[("--documents", profile), ("--provenance", prov)])
    assert cli.main(argv) == 0
    capsys.readouterr()
    path, _ = _read_shard(out, RT_OK)
    published = (out / "index.json").read_text(encoding="utf-8") + gzip.decompress(path.read_bytes()).decode("utf-8")
    for stamp in ("deadbee", "2026-02-03T04:05:06", "c0ffee1", "2026-01-02T03:04:05"):
        assert stamp in published


@pytest.mark.parametrize("outcome_type", ["PRIMARY ", "primary​"])
def test_redteam_m3_population_row_with_unread_outcome_type_not_dropped(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    outcome_type: str,
) -> None:
    """A --population-parsed row whose outcome_type is a padded or
    zero-width spelling of PRIMARY is published with its measure or refused,
    never skipped as if it were a secondary outcome. Round 3: exit 0 and the
    measure's analysis_population_parsed is null."""
    side = (
        "--population-parsed",
        {"nct_id": RT_OK, "outcomes": [{"outcome_index": 0, "outcome_type": outcome_type, "population": "mITT"}]},
    )
    argv, out = _rt_build(tmp_path, studies=_rt_pair(), side=[side])
    rc = cli.main(argv)
    capsys.readouterr()
    if rc != 0:
        assert not out.exists()
        return
    _, sdoc = _read_shard(out, RT_OK)
    assert sdoc["data"][RT_OK]["reported_results"]["primary_outcome_measures"][0]["analysis_population_parsed"] is not None


@pytest.mark.parametrize("case", ["outcome_index_text", "measure_null", "measure_object", "nested_unknown_key"])
def test_redteam_m3_prebuilt_results_rows_not_coerced_or_dropped(case: str) -> None:
    """A pre-built reported_results is read as written or refused: an
    outcome_index that is not an int is not replaced by the row's position
    (two measures then share index 0), a null measure stays null rather than
    becoming "", an object measure is not published as its str(), and a key
    the block does not have is not dropped. Round 3: all four are coerced or
    dropped and returned without a word."""
    reg: dict[str, Any] = {"outcome_index": 0, "measure": "Overall Survival", "time_frame": "24 months", "description": None}
    measures: list[dict[str, Any]] = [{"outcome_index": 0, "title": "Overall Survival"}]
    if case == "outcome_index_text":
        measures = [{"outcome_index": "1", "title": "Overall Survival"}, {"outcome_index": 0, "title": "PFS"}]
    elif case == "measure_null":
        reg["measure"] = None
    elif case == "measure_object":
        reg["measure"] = {"primary_result": _RT3_SECRET}
    else:
        measures[0]["llm_assessment"] = {"primary_result": _RT3_SECRET}
    study = {
        "nct_id": RT_OK,
        "reported_results": {"results_posted": True, "registered_primary_outcomes": [reg], "primary_outcome_measures": measures},
    }
    try:
        block = ol.extract_reported_results(study)
    except ol.OutcomesLayoutError:
        return
    if case == "outcome_index_text":
        indexes = [m["outcome_index"] for m in block["primary_outcome_measures"]]
        assert len(set(indexes)) == len(indexes)
    elif case == "measure_null":
        assert block["registered_primary_outcomes"][0]["measure"] is None
    elif case == "measure_object":
        assert block["registered_primary_outcomes"][0]["measure"] != str({"primary_result": _RT3_SECRET})
    else:
        assert "llm_assessment" in json.dumps(block)


# Guards: write safety against a replica of the families' real file names.

_RT3_REAL_FAMILIES: dict[str, tuple[str, ...]] = {
    "outcomes": ("index.json", "run.json", "summary.json", *(f"records/r0{n}.json" for n in range(8))),
    "results_structure": ("run.json", "summary.json", "records.json"),
    "sponsors": (
        "bridge.csv.gz", "bridge_meta.json", "audit/audit_summary.json", "audit/inbox_open.csv.gz",
        "audit/inbox_new_unmatched.csv", "audit/label_changes.csv", "audit/literals_all.csv.gz", "audit/match_changes.csv",
    ),
    "geo": (
        "README.md", "active_run.json", "geo_rep_2026-06-19_run063/CONTRACT.md",
        "geo_rep_2026-06-19_run063/geo_dictionary.csv", "geo_rep_2026-06-19_run063/column_dictionary.csv",
        "geo_rep_2026-06-19_run063/geo_reader.R",
    ),
    "sex_gender": (
        "methods.json", "methods.md", "audit/audit_summary.json", "audit/status_counts.json",
        "audit/inbox_unrecognized.csv", "audit/resolved_labels.csv", "audit/label_buckets.csv",
        "audit/bucket_changes.csv", "audit/inbox_exits.csv",
    ),
}


def _rt3_real_replica(tmp_path: pathlib.Path, *, under_data: bool = True) -> pathlib.Path:
    """Fictional files at the families' real file names, under ``data/`` (as
    in a copy of the site's data folder) or directly under a folder that is
    not named data (a family copied on its own)."""
    base = tmp_path / "replica3" / ("data" if under_data else "loose")
    for family, names in _RT3_REAL_FAMILIES.items():
        for name in names:
            path = base / family / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fictional {family}/{name}\n", encoding="utf-8")
    return base


@pytest.mark.parametrize("under_data", [True, False])
@pytest.mark.parametrize(
    "sub",
    [
        "outcomes/records",
        "outcomes/records/b",
        "results_structure",
        "sponsors",
        "sponsors/audit",
        "sponsors/audit/b",
        "geo",
        "geo/b",
        "geo/geo_rep_2026-06-19_run063",
        "sex_gender",
        "sex_gender/audit",
        "sex_gender/audit/b",
    ],
)
def test_redteam_m3_real_layout_family_subpaths_refused(tmp_path: pathlib.Path, under_data: bool, sub: str) -> None:
    """Every family folder and subfolder, at the families' real file names,
    with or without a data/ parent, is refused as --out-dir, and no family
    file changes. Guard: refused in round 3."""
    base = _rt3_real_replica(tmp_path, under_data=under_data)
    before = _rt_tree_hashes(base)
    assert cli.main(_rt_manifest_only(tmp_path, base / sub)) == 1
    assert _rt_tree_hashes(base) == before


@pytest.mark.parametrize("family", sorted(_RT3_REAL_FAMILIES))
def test_redteam_m3_relative_report_from_family_cwd_refused(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    """A relative --report run from inside a family folder lands in the
    family and is refused. Guard: refused in round 3."""
    base = _rt3_real_replica(tmp_path)
    before = _rt_tree_hashes(base)
    monkeypatch.chdir(base / family)
    assert cli.main(_rt_manifest_only(tmp_path, tmp_path / "bundle", "--report", "rt3_report.json")) == 1
    assert _rt_tree_hashes(base) == before
    assert not (tmp_path / "bundle").exists()


_RT3_CROSS_STREAM_CASES: tuple[str, ...] = (
    "documents_mapping_with_lowercase_holdout_key",
    "provenance_nctId_names_holdout",
    "provenance_filed_by_trial_nct_id_holdout",
    "second_locations_file_with_holdout_rows",
    "population_row_nested_nctId_holdout",
    "documents_row_zero_width_holdout_id",
    "documents_jsonl_padded_holdout_id",
    "trials_record_absent_from_manifest",
    "side_record_for_excluded_trial",
)


@pytest.mark.parametrize("case", _RT3_CROSS_STREAM_CASES)
def test_redteam_m3_cross_stream_holdout_and_absent_trial_variants_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Holdout material through a stream's other spellings, and records for
    trials absent from the manifest, are refused before anything is written,
    with the holdout's ID withheld. Guard: refused in round 3."""
    studies = _rt_pair()
    profile = _make_doc_profile(RT_OK)
    manifest: Any = _RT2_ROWS
    extra: list[str] = []

    def side(option: str, name: str, content: Any) -> None:
        extra.extend([option, str(_rt2_write(tmp_path / name, content))])

    if case == "documents_mapping_with_lowercase_holdout_key":
        side("--documents", "d.json", {RT_OK: profile, RT_HOLD.lower(): _make_doc_profile(RT_HOLD)})
    elif case == "provenance_nctId_names_holdout":
        side("--provenance", "p.json", {**_make_module4_provenance(RT_OK), "nctId": RT_HOLD})
    elif case == "provenance_filed_by_trial_nct_id_holdout":
        prov = {k: v for k, v in _make_module4_provenance(RT_HOLD).items() if k != "nct_id"}
        side("--provenance", "p.json", {**prov, "trial_nct_id": RT_HOLD})
    elif case == "second_locations_file_with_holdout_rows":
        side("--documents", "d.json", profile)
        side("--locations", "l1.json", _make_complete_locations(RT_OK))
        side("--locations", "l2.json", _make_complete_locations(RT_HOLD))
    elif case == "population_row_nested_nctId_holdout":
        row = {"outcome_index": 0, "outcome_type": "PRIMARY", "population": "ITT", "source": {"nctId": RT_HOLD}}
        side("--population-parsed", "pp.json", {"nct_id": RT_OK, "outcomes": [row]})
    elif case == "documents_row_zero_width_holdout_id":
        side("--documents", "d.json", [{**profile["documents"][0], "nct_id": RT_HOLD[:5] + "​" + RT_HOLD[5:]}])
    elif case == "documents_jsonl_padded_holdout_id":
        side("--documents", "d.jsonl", json.dumps({**_make_doc_profile(RT_HOLD), "nct_id": RT_HOLD + " "}) + "\n")
    elif case == "trials_record_absent_from_manifest":
        studies = [*studies, _make_ctgov_study("NCT99000099")]
    else:
        manifest = {"rows": _RT2_ROWS, "excluded": [{"nct_id": "NCT99000099", "reason": "fictional"}]}
        side("--documents", "d.json", _make_doc_profile("NCT99000099"))
    argv, out = _rt2_argv(tmp_path, manifest=manifest, trials=studies, extra=extra)
    _rt_assert_refused(cli.main(argv), out, capsys)


def test_redteam_m3_documents_and_provenance_disagreeing_on_a_document_refused(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--documents and --provenance that give one posted document
    (Prot_SAP_000.pdf) two different sha256 values conflict, and the build is
    refused rather than publishing both. Round 3: exit 0; the shard's
    document_profile says sha256 bbbb... and its longitudinal_provenance
    says aaaa... for the same file, and --check passes the tree."""
    argv, out = _rt_build(
        tmp_path,
        studies=_rt_pair(),
        side=[
            ("--documents", _make_doc_profile(RT_OK, sha=SHA_B)),
            ("--provenance", _make_module4_provenance(RT_OK, with_pdf_scraped=True)),
        ],
    )
    _rt_assert_refused(cli.main(argv), out, capsys)
