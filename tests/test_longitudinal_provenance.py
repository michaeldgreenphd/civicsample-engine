"""Offline unit tests for src/longitudinal_provenance.py."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
import requests

from src.citation_verifier import (
    CitationVerificationError,
    InvalidNctIdError,
    scan_neutral_language_record,
)
from src.longitudinal_provenance import (
    AuditHoldoutViolationError,
    LONGITUDINAL_SCHEMA_VERSION,
    PROVENANCE_RULES_VERSION,
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

HOLDOUT_SET: frozenset[str] = frozenset({"NCT99999999"})


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


def _sample_study(*, nct_id: Any = "NCT99000001") -> dict[str, Any]:
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


def test_audit_holdout_and_strict_nct_block_before_provider_invocation() -> None:
    spy = _SpyPublicationProvider([])
    assert isinstance(spy, PublicationProvider)

    # F2: Malformed NCT IDs raise InvalidNctIdError FIRST before audit check or provider call.
    for bad_id in (
        "nct99999999",
        "NCT99999999 ",
        " NCT99999999",
        "NCT99999999\u200b",
        "NCT\u0669\u0669\u0669\u0669\u0669\u0669\u0669\u0669",
        None,
        123,
    ):
        with pytest.raises(InvalidNctIdError):
            assemble_longitudinal_provenance(
                _sample_study(nct_id=bad_id),
                publication_provider=spy,
                in_audit=False,
                audit_nct_ids=HOLDOUT_SET,
            )
    assert spy.calls == []

    # F1: Missing in_audit without in_scope_nct_ids, empty audit_nct_ids, or audit trial refused.
    clean_study = _sample_study(nct_id="NCT99000001")
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study,
            publication_provider=spy,
            audit_nct_ids=HOLDOUT_SET,
        )
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study,
            publication_provider=spy,
            in_audit=False,
            audit_nct_ids=frozenset(),
        )
    for bad_flag in (True, None, 0, "false"):
        with pytest.raises(AuditHoldoutViolationError):
            assemble_longitudinal_provenance(
                clean_study,
                publication_provider=spy,
                in_audit=bad_flag,
                audit_nct_ids=HOLDOUT_SET,
            )

    audit_study = _sample_study(nct_id="NCT99999999")
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            audit_study,
            publication_provider=spy,
            in_audit=False,
            audit_nct_ids=HOLDOUT_SET,
        )

    # Mismatched top-level study["nct_id"] vs identificationModule.nctId cannot sneak holdout ID.
    mismatched = _sample_study(nct_id="NCT99999999")
    mismatched["nct_id"] = "NCT99000001"
    with pytest.raises(CitationVerificationError):
        assemble_longitudinal_provenance(
            mismatched,
            publication_provider=spy,
            in_audit=False,
            audit_nct_ids=HOLDOUT_SET,
        )

    # Side-stream extracted_documents carrying in_audit=True or audit nct_id refused before provider.
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study,
            extracted_documents=[{"nct_id": "NCT99999999", "filename": "Prot_000.pdf"}],
            publication_provider=spy,
            in_audit=False,
            audit_nct_ids=HOLDOUT_SET,
        )
    assert spy.calls == []


def test_partial_date_parsing_and_interval_deltas() -> None:
    assert LONGITUDINAL_SCHEMA_VERSION == 1
    assert PROVENANCE_SCHEMA_VERSION == 1

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

    yr_2020 = parse_iso_or_partial_date("2020")
    delta_yr = compute_day_delta_interval(yr_2020, parse_iso_or_partial_date("2021-01-15"))
    assert delta_yr["exact_days"] is None
    assert delta_yr["min_days"] == 15
    assert delta_yr["max_days"] == 380
    assert delta_yr["date_precision"] == "year"

    unk = parse_partial_date(None)
    assert unk["date_precision"] == "unknown"
    delta_unk = compute_day_delta_interval(unk, anchor)
    assert delta_unk["exact_days"] is None
    assert delta_unk["min_days"] is None
    assert delta_unk["max_days"] is None
    assert delta_unk["date_precision"] == "unknown"


def test_printed_ctgov_and_pubmed_date_formats() -> None:
    up = parse_ctgov_upload_date("08/21/2020 14:32")
    assert up["iso_normalized"] == "2020-08-21"
    assert up["date_precision"] == "day"

    assert parse_printed_doc_date("27Nov2020")["iso_normalized"] == "2020-11-27"
    assert parse_printed_doc_date("27 June 2014")["iso_normalized"] == "2014-06-27"
    assert parse_printed_doc_date("June 22, 2017")["iso_normalized"] == "2017-06-22"
    assert parse_printed_doc_date("12th June 2019")["iso_normalized"] == "2019-06-12"
    assert parse_printed_doc_date("01-Jun-2016")["iso_normalized"] == "2016-06-01"
    assert parse_printed_doc_date("20190925")["iso_normalized"] == "2019-09-25"
    assert parse_printed_doc_date("May 2015")["date_precision"] == "month"
    assert parse_printed_doc_date("May 2015")["iso_normalized"] == "2015-05"

    multi = parse_printed_doc_date("16-Mar-2021 / 6-May-2021")
    assert multi["date_precision"] == "unknown"
    assert multi["parse_note"] == "multiple_dates_in_field"
    assert multi["raw"] == "16-Mar-2021 / 6-May-2021"

    ambig = parse_printed_doc_date("03/04/2020")
    assert ambig["date_precision"] == "year"
    assert ambig["iso_normalized"] == "2020"
    assert ambig["parse_note"] == "ambiguous_numeric_month_day"

    assert parse_pubmed_date("2021 May 15")["iso_normalized"] == "2021-05-15"
    assert parse_pubmed_date("2021 May")["date_precision"] == "month"
    pm_range = parse_pubmed_date("2021 May-Jun")
    assert pm_range["date_precision"] == "year"
    assert pm_range["parse_note"] == "season_or_month_range"
    pm_season = parse_pubmed_date("2021 Spring")
    assert pm_season["date_precision"] == "year"
    assert pm_season["parse_note"] == "season_or_month_range"


def test_full_assembly_large_docs_icf_exclusion_and_pub_origin_separation() -> None:
    study = _sample_study(nct_id="NCT99000001")
    extracted_docs = [
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
            "filename": "Prot_000.pdf",
            "doc_type": "protocol",
            "sha256": "a" * 64,
            "version_label": "Amendment 3",
            "version_date_as_printed": "12th June 2019",
        },
        {
            "nct_id": "NCT99000001",
            "in_audit": False,
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
        in_audit=False,
        audit_nct_ids=HOLDOUT_SET,
        extracted_documents=extracted_docs,
        europe_pmc_records=epmc_records,
        publication_provider=pubmed_spy,
        assembled_at="2026-11-20T12:00:00Z",
        source_pipeline_commit="aa14a2d",
    )

    assert pubmed_spy.calls == ["NCT99000001"]
    assert record["nct_id"] == "NCT99000001"
    assert record["days_from_primary_completion_to_results"] == 184
    assert record["excluded_standalone_icf_count"] == 1
    assert [d["filename"] for d in record["large_docs"]] == ["Prot_000.pdf", "SAP_001.pdf"]

    prot = record["large_docs"][0]
    assert prot["version_label"] == "Amendment 3"
    assert prot["version_date_as_printed"]["iso_normalized"] == "2019-06-12"
    assert prot["days_from_primary_completion_to_doc_upload"] == 112
    assert prot["days_from_doc_upload_to_results"] == 72

    sap = record["large_docs"][1]
    assert sap["version_label"] is None
    assert sap["days_from_doc_upload_to_results"] == -30

    pubs_by_pmid = {p["pmid"]: p for p in record["linked_publications"]}
    assert set(pubs_by_pmid.keys()) == {"34000001", "31000009", "34555555"}

    main_pub = pubs_by_pmid["34000001"]
    assert main_pub["pmcid"] == "PMC8123456"
    assert main_pub["is_open_access"] is True
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

    # The projection takes the same firewall basis as the assembly it follows.
    layout_proj = to_outcomes_layout_provenance(
        record, in_audit=False, audit_nct_ids=HOLDOUT_SET
    )
    assert layout_proj["nct_id"] == "NCT99000001"
    assert scan_neutral_language_record(record) == []
    assert scan_neutral_language_record(layout_proj) == []


def test_to_outcomes_layout_provenance_dict_contract_and_jsonl_roundtrip(
    tmp_path: Path,
) -> None:
    """Pure M4 contract check on to_outcomes_layout_provenance output shape and JSONL round-trip."""
    study = _sample_study(nct_id="NCT99000002")
    record = assemble_longitudinal_provenance(
        study,
        in_scope_nct_ids=frozenset({"NCT99000002"}),
        audit_nct_ids=HOLDOUT_SET,
        assembled_at="2026-11-20T12:00:00Z",
        source_pipeline_commit="aa14a2d",
    )
    layout_proj = to_outcomes_layout_provenance(
        record, in_scope_nct_ids=frozenset({"NCT99000002"}), audit_nct_ids=HOLDOUT_SET
    )

    expected_keys = {
        "nct_id",
        "provenance_schema_version",
        "provenance_rules_version",
        "assembled_at",
        "source_pipeline_commit",
        "registry_milestones",
        "posted_documents",
        "excluded_standalone_icf_count",
        "publications",
        "date_deltas",
    }
    assert set(layout_proj.keys()) == expected_keys
    assert layout_proj["nct_id"] == "NCT99000002"
    assert layout_proj["provenance_schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert layout_proj["provenance_rules_version"] == PROVENANCE_RULES_VERSION
    assert layout_proj["assembled_at"] == "2026-11-20T12:00:00Z"
    assert layout_proj["source_pipeline_commit"] == "aa14a2d"
    assert set(layout_proj["registry_milestones"].keys()) == {
        "study_first_post_date",
        "primary_completion_date",
        "results_first_post_date",
        "last_update_post_date",
    }
    assert [d["filename"] for d in layout_proj["posted_documents"]] == [
        "Prot_000.pdf",
        "SAP_001.pdf",
    ]
    assert layout_proj["excluded_standalone_icf_count"] == 1
    assert len(layout_proj["publications"]) == 3
    assert set(layout_proj["date_deltas"].keys()) == {
        "days_from_primary_completion_to_results",
        "primary_completion_to_results",
    }
    assert layout_proj["date_deltas"]["days_from_primary_completion_to_results"] == 184

    # JSONL serialization round-trip preserves exact structure.
    prov_path = tmp_path / "provenance.jsonl"
    prov_path.write_text(
        json.dumps(layout_proj, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reloaded = json.loads(prov_path.read_text(encoding="utf-8").strip())
    assert reloaded == layout_proj
    assert scan_neutral_language_record(reloaded) == []

    # Rejects malformed nct_id on direct projection input.
    bad_record = dict(record)
    bad_record["nct_id"] = "nct99000002"
    with pytest.raises(InvalidNctIdError):
        to_outcomes_layout_provenance(bad_record)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Adversarial Red-Team Suite (strictly fictional NCT99800001..NCT99899999)
# ---------------------------------------------------------------------------

RT_AUDIT_ID = "NCT99888888"
RT_AUDIT_SET: frozenset[str] = frozenset({RT_AUDIT_ID})
RT_SCOPE_ID = "NCT99800001"
RT_UNLISTED_ID = "NCT99800099"
RT_SCOPE_SET: frozenset[str] = frozenset({RT_SCOPE_ID})


def test_redteam_provenance_firewall_strict_nct_and_side_streams() -> None:
    """Red-team assemble_longitudinal_provenance & to_outcomes_layout_provenance against holdout/NCT attacks."""
    spy = _SpyPublicationProvider(
        [{"nct_id": RT_AUDIT_ID, "pmid": "34000001", "pub_date": "2021 May"}]
    )

    # 1. Malformed NCT IDs raise InvalidNctIdError FIRST before any audit or provider call.
    for bad_nct in (
        "nct99888888",
        "NCT99888888 ",
        " NCT99888888",
        "NCT99888888\u200b",
        "NCT\u0669\u0669\u0668\u0668\u0668\u0668\u0668\u0668",
        None,
        99888888,
    ):
        with pytest.raises(InvalidNctIdError):
            assemble_longitudinal_provenance(
                _sample_study(nct_id=bad_nct),
                in_audit=True,
                audit_nct_ids=RT_AUDIT_SET,
                publication_provider=spy,
            )

    # F2 ordering: malformed audit_nct_ids raises InvalidNctIdError even when study["in_audit"]=True.
    study_true = _sample_study(nct_id=RT_SCOPE_ID)
    study_true["in_audit"] = True
    with pytest.raises(InvalidNctIdError):
        assemble_longitudinal_provenance(
            study_true,
            audit_nct_ids={"nct99888888"},
            publication_provider=spy,
        )

    # 2. Missing in_audit, empty audit_nct_ids, non-bool in_audit, audit ID, and unlisted ID refused.
    clean_study = _sample_study(nct_id=RT_SCOPE_ID)
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(clean_study, audit_nct_ids=RT_AUDIT_SET, publication_provider=spy)
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study, in_audit=False, audit_nct_ids=set(), publication_provider=spy
        )
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_AUDIT_ID),
            in_audit=False,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
        )
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_UNLISTED_ID),
            in_scope_nct_ids=RT_SCOPE_SET,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
        )
    for bad_flag in (True, None, 0, "false"):
        with pytest.raises(AuditHoldoutViolationError):
            assemble_longitudinal_provenance(
                clean_study,
                in_audit=bad_flag,
                audit_nct_ids=RT_AUDIT_SET,
                publication_provider=spy,
            )

    # 3. Top-level camelCase study["nctId"] smuggling NCT99888888 alongside study["nct_id"].
    camel_mismatch = {"nct_id": RT_SCOPE_ID, "nctId": RT_AUDIT_ID}
    with pytest.raises((AuditHoldoutViolationError, CitationVerificationError)):
        assemble_longitudinal_provenance(
            camel_mismatch,
            in_scope_nct_ids=RT_SCOPE_SET,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
        )

    # 4. Side streams carrying audit nct_id, non-bool in_audit, embedded audit URL, or bare str stream.
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study,
            in_scope_nct_ids=RT_SCOPE_SET,
            audit_nct_ids=RT_AUDIT_SET,
            extracted_documents=[
                {
                    "nct_id": RT_SCOPE_ID,
                    "filename": "Prot_000.pdf",
                    "url": f"https://cdn.clinicaltrials.gov/large-docs/88/{RT_AUDIT_ID}/Prot_000.pdf",
                }
            ],
            publication_provider=spy,
        )
    with pytest.raises(CitationVerificationError):
        assemble_longitudinal_provenance(
            clean_study,
            in_scope_nct_ids=RT_SCOPE_SET,
            audit_nct_ids=RT_AUDIT_SET,
            extracted_documents=RT_AUDIT_ID,  # type: ignore[arg-type]
            publication_provider=spy,
        )

    # Zero calls reached spy across all pre-provider firewall refusals.
    assert spy.calls == []

    # 5. Provider returning an NCT99888888 row is refused on return validation.
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            clean_study,
            in_scope_nct_ids=RT_SCOPE_SET,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
        )
    assert spy.calls == [RT_SCOPE_ID]

    # 6. Projection refuses audit flags and malformed NCT IDs.
    valid_rec = assemble_longitudinal_provenance(
        clean_study,
        in_scope_nct_ids=RT_SCOPE_SET,
        audit_nct_ids=RT_AUDIT_SET,
    )
    for bad_flag in (True, None, 0, "false"):
        tampered = dict(valid_rec)
        tampered["in_audit"] = bad_flag
        with pytest.raises(AuditHoldoutViolationError):
            to_outcomes_layout_provenance(tampered)  # type: ignore[arg-type]


def test_followup_study_own_in_audit_false_never_admits_and_provider_untouched() -> None:
    """A study's own in_audit=False no longer stands in for the caller's flag or scope."""
    spy = _SpyPublicationProvider([])
    self_declared = {**_sample_study(nct_id=RT_SCOPE_ID), "in_audit": False}
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            self_declared,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
        )
    assert spy.calls == []

    # Admitted by the caller's explicit flag, or by in-scope membership.
    for admit in ({"in_audit": False}, {"in_scope_nct_ids": RT_SCOPE_SET}):
        record = assemble_longitudinal_provenance(
            self_declared,
            audit_nct_ids=RT_AUDIT_SET,
            publication_provider=spy,
            **admit,  # type: ignore[arg-type]
        )
        assert record["nct_id"] == RT_SCOPE_ID
    assert spy.calls == [RT_SCOPE_ID, RT_SCOPE_ID]

    # A side-stream record naming another trial in a list key needs the scope.
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_SCOPE_ID),
            in_audit=False,
            audit_nct_ids=RT_AUDIT_SET,
            pubmed_records=[{"pmid": "34000002", "nct_ids": [RT_SCOPE_ID, RT_UNLISTED_ID]}],
        )


# ---------------------------------------------------------------------------
# Round-2 red team on m4 (fictional NCT99800001..NCT99899999 only).
# ---------------------------------------------------------------------------


def test_redteam_m4_provenance_alias_and_secondary_ids_are_firewalled() -> None:
    """A study naming the audit trial in a CT.gov identification field is assembled.

    ``_resolve_study_nct_id`` reads only ``nctId``/``nct_id``, and the nested
    walk knows only ``nct_id``/``nctId``/``trial_nct_id`` and
    ``nct_ids``/``trial_nct_ids``. API v2 ``identificationModule.nctIdAliases``
    (obsolete registrations of the same trial), ``secondaryIdInfos[].id``, and
    the classic API's ``NCTId`` list all pass, and the provider is then called.
    """
    spy = _SpyPublicationProvider([])

    def _alias(study: dict[str, Any]) -> None:
        study["protocolSection"]["identificationModule"]["nctIdAliases"] = [RT_AUDIT_ID]

    def _secondary(study: dict[str, Any]) -> None:
        study["protocolSection"]["identificationModule"]["secondaryIdInfos"] = [
            {"id": RT_AUDIT_ID, "type": "OTHER", "domain": "ClinicalTrials.gov"}
        ]

    def _classic(study: dict[str, Any]) -> None:
        study["NCTId"] = [RT_AUDIT_ID]

    for name, mutate in (("nctIdAliases", _alias), ("secondaryIdInfos", _secondary), ("NCTId", _classic)):
        study = _sample_study(nct_id=RT_SCOPE_ID)
        mutate(study)
        with pytest.raises(AuditHoldoutViolationError):
            assemble_longitudinal_provenance(
                study,
                audit_nct_ids=RT_AUDIT_SET,
                in_scope_nct_ids=RT_SCOPE_SET,
                publication_provider=spy,
            )
            pytest.fail(f"study naming the audit trial under {name} was assembled")
    assert spy.calls == []


def test_redteam_m4_provenance_extracted_document_must_name_the_study() -> None:
    """An extracted document without ``nct_id`` is joined to the study by filename alone.

    ClinicalTrials.gov large-document filenames are generic (``Prot_000.pdf``),
    so a record that names no trial attaches its sha256 and version label to
    whichever study shares the filename. The doc-cache path refuses an entry
    that nothing ties to an in-scope trial; this side stream does not.
    """
    anonymous_sha = "b" * 64
    with pytest.raises(CitationVerificationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_SCOPE_ID),
            audit_nct_ids=RT_AUDIT_SET,
            in_scope_nct_ids=RT_SCOPE_SET,
            extracted_documents=[
                {"filename": "Prot_000.pdf", "sha256": anonymous_sha, "version_label": "v9"}
            ],
        )
        pytest.fail("an extracted document naming no trial was merged into the study")

    # Control: the same document naming the study is merged.
    record = assemble_longitudinal_provenance(
        _sample_study(nct_id=RT_SCOPE_ID),
        audit_nct_ids=RT_AUDIT_SET,
        in_scope_nct_ids=RT_SCOPE_SET,
        extracted_documents=[
            {"nct_id": RT_SCOPE_ID, "filename": "Prot_000.pdf", "sha256": anonymous_sha}
        ],
    )
    assert record["large_docs"][0]["sha256"] == anonymous_sha


def test_redteam_m4_layout_projection_needs_a_firewall_basis() -> None:
    """``to_outcomes_layout_provenance`` projects a record for any trial.

    It takes no audit set, scope or in_audit, so a provenance record read back
    from JSONL (the shape the existing round-trip test writes) for an audit
    trial is projected into the outcomes-bundle layout; only a record's own
    ``in_audit`` can stop it, and the module's rule is that a record's own
    flag never admits.
    """
    valid = assemble_longitudinal_provenance(
        _sample_study(nct_id=RT_SCOPE_ID),
        audit_nct_ids=RT_AUDIT_SET,
        in_scope_nct_ids=RT_SCOPE_SET,
    )
    tampered = json.loads(json.dumps(valid))
    tampered["nct_id"] = RT_AUDIT_ID
    with pytest.raises(AuditHoldoutViolationError):
        to_outcomes_layout_provenance(tampered)  # type: ignore[arg-type]
        pytest.fail("an audit trial's provenance record was projected")


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


def test_redteam_m4_provenance_obfuscated_audit_tokens_refused() -> None:
    """Publication and registry text naming the audit trial as ``NCT 9...`` is copied into the record.

    ``_scan_strings_for_audit_nct_tokens`` finds only ``nct[0-9]{7,10}``
    (after percent-decoding and NFKC/Cf folding). PubMed and registry text
    commonly writes ``NCT 0123 4567``; such a title or reference citation
    passes and is copied into ``linked_publications``, and a study whose
    reference names the holdout that way reaches the publication provider.
    """
    for name, token in _rt_m4_r3_obfuscated_audit_ids().items():
        text = f"Long-term extension of {token}."
        with pytest.raises(CitationVerificationError):
            assemble_longitudinal_provenance(
                _sample_study(nct_id=RT_SCOPE_ID),
                in_audit=False,
                audit_nct_ids=RT_AUDIT_SET,
                pubmed_records=[{"pmid": "34000002", "title": text, "pub_date": "2022 Jan"}],
            )
            pytest.fail(f"{name}: a PubMed title naming the audit trial was assembled")
        with pytest.raises(CitationVerificationError):
            assemble_longitudinal_provenance(
                _sample_study(nct_id=RT_SCOPE_ID),
                in_audit=False,
                audit_nct_ids=RT_AUDIT_SET,
                europe_pmc_records=[{"pmid": "34000003", "title": text}],
            )
            pytest.fail(f"{name}: a Europe PMC title naming the audit trial was assembled")

        spy = _SpyPublicationProvider([])
        study = _sample_study(nct_id=RT_SCOPE_ID)
        study["protocolSection"]["referencesModule"]["references"].append(
            {"pmid": "34000004", "type": "BACKGROUND", "citation": text}
        )
        with pytest.raises(CitationVerificationError):
            assemble_longitudinal_provenance(
                study,
                in_audit=False,
                audit_nct_ids=RT_AUDIT_SET,
                publication_provider=spy,
            )
            pytest.fail(f"{name}: a registry reference naming the audit trial was assembled")
        assert spy.calls == [], f"{name}: the publication provider was called"


def test_redteam_m4_provenance_large_doc_flags_absent_are_unknown() -> None:
    """A largeDocs entry with missing, null or string flags is typed by truthiness.

    ``_extract_large_docs`` reads ``bool(raw_doc.get("hasProtocol"))``: a
    missing or null flag is read as False, so a document whose type the
    registry does not state is counted as a standalone ICF and dropped from
    ``large_docs``; the strings ``"false"``/``"true"`` are both True, so it
    becomes ``combined``. Absence must stay absence (the planner reads a
    missing ``protocol``/``sap`` flag as unknown), and a non-bool flag is not
    a flag.
    """
    cases: dict[str, dict[str, Any]] = {
        "flags_missing": {"filename": "Prot_000.pdf", "uploadDate": "2020-08-21"},
        "flags_null": {"filename": "Prot_000.pdf", "hasProtocol": None, "hasSap": None},
        "flags_false_strings": {"filename": "Prot_000.pdf", "hasProtocol": "false", "hasSap": "false"},
    }
    for name, large_doc in cases.items():
        study = _sample_study(nct_id=RT_SCOPE_ID)
        study["documentSection"]["largeDocumentModule"]["largeDocs"] = [large_doc]
        try:
            record = assemble_longitudinal_provenance(
                study, in_audit=False, audit_nct_ids=RT_AUDIT_SET
            )
        except CitationVerificationError:
            continue
        assert record["excluded_standalone_icf_count"] == 0, (
            f"{name}: a document of unstated type was counted as a standalone ICF"
        )
        typed = {d["filename"]: d["doc_type"] for d in record["large_docs"]}
        assert typed.get("Prot_000.pdf") not in {"protocol", "sap", "combined"}, (
            f"{name}: a document of unstated type was typed {typed.get('Prot_000.pdf')!r}"
        )


def test_redteam_m4_provenance_duplicate_extracted_documents_refused() -> None:
    """Two extracted documents for one filename are resolved silently (the later wins).

    ``_extract_large_docs`` keys extracted rows by filename and overwrites, so
    which sha256 and version label the record carries for ``Prot_000.pdf``
    depends on row order. ``verify_census_corpus`` reports the same
    duplicate (``DUPLICATE_DOCUMENT_RECORD``).
    """
    extracted = [
        {"nct_id": RT_SCOPE_ID, "filename": "Prot_000.pdf", "sha256": "a" * 64, "version_label": "v1"},
        {"nct_id": RT_SCOPE_ID, "filename": "Prot_000.pdf", "sha256": "b" * 64, "version_label": "v2"},
    ]
    with pytest.raises(CitationVerificationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_SCOPE_ID),
            in_audit=False,
            audit_nct_ids=RT_AUDIT_SET,
            extracted_documents=extracted,
        )
        pytest.fail("two extracted documents for one filename were merged by row order")


def test_redteam_m4_provenance_malformed_ids_and_unscoped_empty_audit_set() -> None:
    """Attempt that held: malformed record IDs and an empty audit set without scope are refused."""
    for bad in (RT_SCOPE_ID.lower(), f" {RT_SCOPE_ID}", f"{RT_SCOPE_ID}​"):
        with pytest.raises(InvalidNctIdError):
            to_outcomes_layout_provenance(
                {"nct_id": bad},  # type: ignore[arg-type]
                in_audit=False,
                audit_nct_ids=RT_AUDIT_SET,
            )
        with pytest.raises(InvalidNctIdError):
            assemble_longitudinal_provenance(
                _sample_study(nct_id=bad), in_audit=False, audit_nct_ids=RT_AUDIT_SET
            )
    with pytest.raises(AuditHoldoutViolationError):
        assemble_longitudinal_provenance(
            _sample_study(nct_id=RT_SCOPE_ID), in_audit=False, allow_empty_audit_set=True
        )
    with pytest.raises(AuditHoldoutViolationError):
        to_outcomes_layout_provenance(
            {"nct_id": RT_SCOPE_ID},  # type: ignore[arg-type]
            in_audit=False,
            allow_empty_audit_set=True,
        )


# ---------------------------------------------------------------------------
# Red team on m4 at f442dac (after the round-3 fixes; fictional
# NCT99800001..NCT99899999 only).
#
# Each test asserts the behaviour the module's own rules call for. Tests for a
# bypass fail until the module is fixed; tests for an attempt that held pass.
# ---------------------------------------------------------------------------


def _rt_f442_assemble(study: dict[str, Any], **kwargs: Any) -> Any:
    """Assemble ``study`` for ``RT_SCOPE_ID`` behind the scoped firewall."""
    return assemble_longitudinal_provenance(
        study, audit_nct_ids=RT_AUDIT_SET, in_scope_nct_ids=RT_SCOPE_SET, **kwargs
    )


def test_redteam_m4_provenance_audit_id_renderings_in_study_text_refused() -> None:
    """Registry text naming the holdout trial in forms the token rule misses is copied into the record.

    ``_scan_strings_for_audit_nct_tokens`` reads each key and string alone and
    its loose reading allows at most three separators, so a reference citing
    ``NCT    99888888`` or ``NCT No. 99888888`` is assembled and its text
    copied into ``linked_publications[].citation_text``, and
    ``{"NCT": "99888888"}`` in the identification module passes. A reader
    takes each for the holdout trial.
    """
    digits = RT_AUDIT_ID[3:]
    for name, citation in {
        "four_spaces": f"Extension of NCT    {digits}.",
        "nct_no": f"Extension of NCT No. {digits}.",
    }.items():
        study = _sample_study(nct_id=RT_SCOPE_ID)
        study["protocolSection"]["referencesModule"]["references"].append(
            {"citation": citation, "type": "BACKGROUND"}
        )
        with pytest.raises(AuditHoldoutViolationError):
            _rt_f442_assemble(study)
            pytest.fail(f"{name}: a reference naming the holdout trial was copied into the record")

    study = _sample_study(nct_id=RT_SCOPE_ID)
    study["protocolSection"]["identificationModule"]["orgStudyIdInfo"] = {"NCT": digits}
    with pytest.raises(AuditHoldoutViolationError):
        _rt_f442_assemble(study)
        pytest.fail("a study naming the holdout trial as key + value was assembled")


def test_redteam_m4_provenance_conflicting_publication_ids_are_not_merged_silently() -> None:
    """Two records with one DOI and two PMIDs (or one PMID and two DOIs) merge, keeping the first.

    ``_resolve_bucket`` joins a record to an existing publication by PMID,
    else DOI, else citation text, and ``bucket["pmid"] or pmid`` keeps the
    first value: PubMed's PMID 40000001 and Europe PMC's PMID 40000002 for
    DOI 10.1000/xyz become one publication with PMID 40000001, and the second
    identifier (and title) is gone from the record without trace. Conflicting
    identifiers must raise, or both stay in the record.
    """
    cases = {
        "two_pmids_one_doi": (
            [{"pmid": "40000001", "doi": "10.1000/xyz", "title": "A"}],
            [{"pmid": "40000002", "doi": "10.1000/xyz", "title": "B"}],
            ("pmid", {"40000001", "40000002"}),
        ),
        "two_dois_one_pmid": (
            [{"pmid": "40000003", "doi": "10.1000/aaa"}],
            [{"pmid": "40000003", "doi": "10.1000/bbb"}],
            ("doi", {"10.1000/aaa", "10.1000/bbb"}),
        ),
    }
    for name, (pubmed, epmc, (field, expected)) in cases.items():
        try:
            record = _rt_f442_assemble(
                _sample_study(nct_id=RT_SCOPE_ID), pubmed_records=pubmed, europe_pmc_records=epmc
            )
        except CitationVerificationError:
            continue
        kept = {pub[field] for pub in record["linked_publications"]}
        assert expected <= kept, f"{name}: a conflicting {field} was dropped by record order"


def test_redteam_m4_provenance_large_docs_are_never_dropped_silently() -> None:
    """Posted documents vanish from the record when an entry is malformed.

    ``_extract_large_docs`` skips a ``largeDocs`` entry that is not an object
    and a protocol or SAP entry with no ``filename``, reads a ``largeDocs``
    object (not a list) as no documents, and skips an extracted document whose
    ``doc_type`` is outside the vocabulary (``"Protocol"``). None of these is
    counted (``excluded_standalone_icf_count`` counts ICFs only) or refused,
    so the record says the trial posted fewer documents than it did.
    """
    def _append(entry: Any) -> dict[str, Any]:
        study = _sample_study(nct_id=RT_SCOPE_ID)
        study["documentSection"]["largeDocumentModule"]["largeDocs"].append(entry)
        return study

    as_object = _sample_study(nct_id=RT_SCOPE_ID)
    module = as_object["documentSection"]["largeDocumentModule"]
    module["largeDocs"] = {str(i): doc for i, doc in enumerate(module["largeDocs"])}
    studies = {
        "protocol_without_filename": _append(
            {"label": "Study Protocol v2", "hasProtocol": True, "hasSap": False, "uploadDate": "01/04/2021 10:00"}
        ),
        "entry_not_an_object": _append("Prot_009.pdf"),
        "largeDocs_object": as_object,
    }
    for name, study in studies.items():
        with pytest.raises(CitationVerificationError):
            _rt_f442_assemble(study)
            pytest.fail(f"{name}: a posted document was dropped from the record")

    with pytest.raises(CitationVerificationError):
        _rt_f442_assemble(
            _sample_study(nct_id=RT_SCOPE_ID),
            extracted_documents=[
                {"nct_id": RT_SCOPE_ID, "filename": "Prot_777.pdf", "sha256": "c" * 64, "doc_type": "Protocol"}
            ],
        )
        pytest.fail("an extracted document with doc_type 'Protocol' was dropped from the record")


def test_redteam_m4_provenance_pmid_is_ascii_digits() -> None:
    """A PMID written in fullwidth digits is recorded as-is.

    ``_clean_pmid`` accepts ``str.isdigit()``, which is true for fullwidth and
    other Unicode digits, so ``"４００００１"`` is kept unnormalised: it never
    joins the same publication's ASCII PMID and is written to the record as an
    identifier PubMed does not issue.
    """
    try:
        record = _rt_f442_assemble(
            _sample_study(nct_id=RT_SCOPE_ID),
            pubmed_records=[{"pmid": "４０００００１", "title": "Fullwidth PMID"}],
        )
    except CitationVerificationError:
        return
    pmids = [pub["pmid"] for pub in record["linked_publications"] if pub["pmid"] is not None]
    assert all(pmid.isascii() for pmid in pmids), f"non-ASCII PMID recorded: {pmids!r}"


def test_redteam_m4_provenance_publication_record_naming_another_trial_refused() -> None:
    """Attempt that held: a publication record naming another trial under ``nct_id`` is refused.

    The named-key walk requires a side-stream ``nct_id`` to be the study's own.
    """
    with pytest.raises(AuditHoldoutViolationError):
        _rt_f442_assemble(
            _sample_study(nct_id=RT_SCOPE_ID),
            pubmed_records=[{"pmid": "40000009", "nct_id": RT_UNLISTED_ID}],
        )
