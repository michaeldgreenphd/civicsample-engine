"""Offline unit tests for src/longitudinal_provenance.py."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
import requests

from src.citation_verifier import scan_neutral_language_record
from src.longitudinal_provenance import (
    AuditHoldoutViolationError,
    LONGITUDINAL_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION,
    PublicationProvider,
    assemble_longitudinal_provenance,
    compute_day_delta_interval,
    parse_ctgov_upload_date,
    parse_iso_or_partial_date,
    parse_partial_date,
    parse_printed_doc_date,
    parse_pubmed_date,
    to_outcomes_layout_provenance,
)


class _SpyPublicationProvider:
    """In-memory spy verifying PublicationProvider protocol and call count."""

    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        self._records = list(records)
        self.calls: list[str] = []

    def get_publications_for_study(self, nct_id: str) -> Sequence[Mapping[str, Any]]:
        self.calls.append(nct_id)
        return list(self._records)


@pytest.fixture(autouse=True)
def _block_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if any test ever attempts a live network call."""

    def _forbid_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live HTTP requests are forbidden in longitudinal provenance tests")

    monkeypatch.setattr(requests.Session, "request", _forbid_request)


def _sample_study(*, nct_id: str = "NCT01234567") -> dict[str, Any]:
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


def test_audit_holdout_blocks_before_provider_invocation() -> None:
    spy = _SpyPublicationProvider([])
    assert isinstance(spy, PublicationProvider)
    study = _sample_study(nct_id="NCT09999999")

    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            study,
            publication_provider=spy,
            in_audit=True,
        )
    assert spy.calls == []

    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            study,
            publication_provider=spy,
            audit_nct_ids={"NCT09999999"},
        )
    assert spy.calls == []


def test_partial_date_parsing_and_interval_deltas() -> None:
    assert LONGITUDINAL_SCHEMA_VERSION == 1
    assert PROVENANCE_SCHEMA_VERSION == 1

    # Exact day-to-day deltas: positive, negative, zero.
    d_start = parse_partial_date("2020-05-01")
    d_after = parse_partial_date("2020-08-21")
    d_before = parse_partial_date("2020-04-21")

    delta_pos = compute_day_delta_interval(d_start, d_after)
    assert delta_pos["exact_days"] == 112
    assert delta_pos["min_days"] == 112
    assert delta_pos["max_days"] == 112
    assert delta_pos["date_precision"] == "day"

    delta_neg = compute_day_delta_interval(d_start, d_before)
    assert delta_neg["exact_days"] == -10
    assert delta_neg["min_days"] == -10
    assert delta_neg["max_days"] == -10

    delta_zero = compute_day_delta_interval(d_start, d_start)
    assert delta_zero["exact_days"] == 0

    # Month partial dates: leap year Feb 2020 vs non-leap Feb 2021 — NEVER fabricates -01 exact_days.
    feb_2020 = parse_iso_or_partial_date("2020-02")
    assert feb_2020["date_precision"] == "month"
    assert feb_2020["earliest_iso"] == "2020-02-01"
    assert feb_2020["latest_iso"] == "2020-02-29"

    feb_2021 = parse_iso_or_partial_date("2021-02")
    assert feb_2021["latest_iso"] == "2021-02-28"

    anchor = parse_iso_or_partial_date("2020-03-01")
    delta_leap = compute_day_delta_interval(feb_2020, anchor)
    assert delta_leap["exact_days"] is None
    assert delta_leap["min_days"] == 1
    assert delta_leap["max_days"] == 29
    assert delta_leap["date_precision"] == "month"

    # Year partial date.
    yr_2020 = parse_iso_or_partial_date("2020")
    delta_yr = compute_day_delta_interval(yr_2020, parse_iso_or_partial_date("2021-01-15"))
    assert delta_yr["exact_days"] is None
    assert delta_yr["min_days"] == 15
    assert delta_yr["max_days"] == 380
    assert delta_yr["date_precision"] == "year"

    # Missing / empty date -> unknown with all None deltas.
    unk = parse_partial_date(None)
    assert unk["date_precision"] == "unknown"
    delta_unk = compute_day_delta_interval(unk, anchor)
    assert delta_unk["exact_days"] is None
    assert delta_unk["min_days"] is None
    assert delta_unk["max_days"] is None
    assert delta_unk["date_precision"] == "unknown"


def test_printed_ctgov_and_pubmed_date_formats() -> None:
    # CT.gov NLM uploadDate format.
    up = parse_ctgov_upload_date("08/21/2020 14:32")
    assert up["iso_normalized"] == "2020-08-21"
    assert up["date_precision"] == "day"

    # Sponsor cover-page formats.
    assert parse_printed_doc_date("27Nov2020")["iso_normalized"] == "2020-11-27"
    assert parse_printed_doc_date("27 June 2014")["iso_normalized"] == "2014-06-27"
    assert parse_printed_doc_date("June 22, 2017")["iso_normalized"] == "2017-06-22"
    assert parse_printed_doc_date("12th June 2019")["iso_normalized"] == "2019-06-12"
    assert parse_printed_doc_date("01-Jun-2016")["iso_normalized"] == "2016-06-01"
    assert parse_printed_doc_date("20190925")["iso_normalized"] == "2019-09-25"
    assert parse_printed_doc_date("May 2015")["date_precision"] == "month"
    assert parse_printed_doc_date("May 2015")["iso_normalized"] == "2015-05"

    # Compound multi-version date stays unknown with explicit parse_note.
    multi = parse_printed_doc_date("16-Mar-2021 / 6-May-2021")
    assert multi["date_precision"] == "unknown"
    assert multi["parse_note"] == "multiple_dates_in_field"
    assert multi["raw"] == "16-Mar-2021 / 6-May-2021"

    # Ambiguous international all-numeric date resolves to year (never guesses US vs EU).
    ambig = parse_printed_doc_date("03/04/2020")
    assert ambig["date_precision"] == "year"
    assert ambig["iso_normalized"] == "2020"
    assert ambig["parse_note"] == "ambiguous_numeric_month_day"

    # PubMed formats.
    assert parse_pubmed_date("2021 May 15")["iso_normalized"] == "2021-05-15"
    assert parse_pubmed_date("2021 May")["date_precision"] == "month"
    pm_range = parse_pubmed_date("2021 May-Jun")
    assert pm_range["date_precision"] == "year"
    assert pm_range["parse_note"] == "season_or_month_range"
    pm_season = parse_pubmed_date("2021 Spring")
    assert pm_season["date_precision"] == "year"
    assert pm_season["parse_note"] == "season_or_month_range"


def test_full_assembly_large_docs_icf_exclusion_and_pub_origin_separation() -> None:
    study = _sample_study()
    extracted_docs = [
        {
            "filename": "Prot_000.pdf",
            "doc_type": "protocol",
            "sha256": "a" * 64,
            "version_label": "Amendment 3",
            "version_date_as_printed": "12th June 2019",
        },
        {
            "filename": "SAP_001.pdf",
            "doc_type": "sap",
            "sha256": "b" * 64,
            "version_label": "",
            "version_date_as_printed": "27Nov2020",
        },
    ]
    pubmed_spy = _SpyPublicationProvider(
        [
            {
                "pmid": "34000001",
                "title": "Primary results of Trial X",
                "authors": "Smith J, Doe A",
                "journal": "Lancet",
                "pub_date": "2021 May",
                "citation": "Smith J, Doe A Primary results of Trial X Lancet. 2021 May",
                "source": "pubmed",
                "type": "DERIVED",
            }
        ]
    )
    epmc_records = [
        {
            "pmid": "34000001",
            "pmcid": "PMC8123456",
            "doi": "10.1016/S0140-6736(21)00001-X",
            "firstPublicationDate": "2021-05-15",
            "isOpenAccess": "Y",
        }
    ]

    record = assemble_longitudinal_provenance(
        study,
        extracted_documents=extracted_docs,
        europe_pmc_records=epmc_records,
        publication_provider=pubmed_spy,
        assembled_at="2026-11-20T12:00:00Z",
        source_pipeline_commit="5b55d47",
    )

    assert pubmed_spy.calls == ["NCT01234567"]
    assert record["nct_id"] == "NCT01234567"
    assert record["days_from_primary_completion_to_results"] == 184
    assert record["excluded_standalone_icf_count"] == 1
    assert [d["filename"] for d in record["large_docs"]] == ["Prot_000.pdf", "SAP_001.pdf"]

    prot = record["large_docs"][0]
    assert prot["version_label"] == "Amendment 3"
    assert prot["version_date_as_printed"]["iso_normalized"] == "2019-06-12"
    assert prot["days_from_primary_completion_to_doc_upload"] == 112
    assert prot["days_from_doc_upload_to_results"] == 72

    sap = record["large_docs"][1]
    assert sap["version_label"] is None  # empty string normalized to None
    # Uploaded 2020-12-01 AFTER results_first_post 2020-11-01 -> negative delta -30 days.
    assert sap["days_from_doc_upload_to_results"] == -30

    # Publications: 3 distinct PMIDs deduplicated properly.
    pubs_by_pmid = {p["pmid"]: p for p in record["linked_publications"]}
    assert set(pubs_by_pmid.keys()) == {"34000001", "31000009", "34555555"}

    main_pub = pubs_by_pmid["34000001"]
    assert main_pub["pmcid"] == "PMC8123456"
    assert main_pub["is_open_access"] is True
    # Europe PMC day-level date preferred over PubMed month-level, both raw dates preserved.
    assert main_pub["pub_date"]["iso_normalized"] == "2021-05-15"
    assert main_pub["pub_date"]["date_precision"] == "day"
    assert main_pub["source_raw_dates"] == {"europe_pmc": "2021-05-15", "pubmed": "2021 May"}
    assert main_pub["registry_reference_types"] == ["DERIVED", "RESULT"]
    assert main_pub["link_origins"] == ["AUTO_INDEXED", "INVESTIGATOR_SUBMITTED"]
    assert main_pub["sources"] == [
        "ctgov_more_info_module",
        "ctgov_references_module",
        "europe_pmc",
        "pubmed",
    ]

    bg_pub = pubs_by_pmid["31000009"]
    assert bg_pub["registry_reference_types"] == ["BACKGROUND"]
    assert bg_pub["link_origins"] == ["INVESTIGATOR_SUBMITTED"]

    derived_pub = pubs_by_pmid["34555555"]
    assert derived_pub["registry_reference_types"] == ["DERIVED"]
    assert derived_pub["link_origins"] == ["AUTO_INDEXED"]

    # Module 3 projection & neutrality check.
    layout_proj = to_outcomes_layout_provenance(record)
    assert layout_proj["nct_id"] == "NCT01234567"
    assert set(layout_proj.keys()) >= {
        "nct_id",
        "registry_milestones",
        "posted_documents",
        "publications",
        "date_deltas",
    }
    assert scan_neutral_language_record(record) == []
    assert scan_neutral_language_record(layout_proj) == []


def test_to_outcomes_layout_provenance_feeds_build_outcomes_shards(tmp_path: Path) -> None:
    from scripts.build_outcomes_shards import main as build_shards_main

    study = _sample_study(nct_id="NCT01234567")
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
        assembled_at="2026-11-20T12:00:00Z",
        source_pipeline_commit="5b55d47",
    )
    layout_proj = to_outcomes_layout_provenance(record)

    trials_path = tmp_path / "trials.json"
    manifest_path = tmp_path / "manifest.json"
    prov_path = tmp_path / "provenance.jsonl"
    out_dir = tmp_path / "outcomes"

    trials_path.write_text(json.dumps([study]), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "nct_id": "NCT01234567",
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

    rc_build = build_shards_main(
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
    assert build_shards_main(["--check", str(out_dir)]) == 0
