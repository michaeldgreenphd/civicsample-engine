"""Shared fictional ClinicalTrials.gov v2 and weekly full-record builders for `tests/feasibility/`."""
from __future__ import annotations

from typing import Any


def make_ctgov_study(
    nct_id: str = "NCT99100001",
    *,
    brief_title: str = "Fictional operational feasibility study",
    study_type: str = "INTERVENTIONAL",
    phases: list[str] | None = None,
    overall_status: str = "COMPLETED",
    why_stopped: str | None = None,
    start_date: str | None = "2020-01-15",
    start_type: str | None = "ACTUAL",
    primary_completion_date: str | None = "2021-01-15",
    primary_completion_type: str | None = "ACTUAL",
    completion_date: str | None = "2021-04-15",
    completion_type: str | None = "ACTUAL",
    enrollment_count: int | None = 120,
    enrollment_type: str | None = "ACTUAL",
    n_sites: int = 4,
    lead_sponsor_name: str = "Pfizer",
    lead_sponsor_class: str = "INDUSTRY",
    collaborators: list[dict[str, str]] | None = None,
    conditions: list[str] | None = None,
    meshes: list[dict[str, str]] | None = None,
    has_results: bool = True,
    flow_module: dict[str, Any] | None = None,
    outcome_measures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct a fictional ClinicalTrials.gov API v2 study document."""
    status_mod: dict[str, Any] = {"overallStatus": overall_status}
    if why_stopped is not None:
        status_mod["whyStopped"] = why_stopped
    if start_date is not None or start_type is not None:
        s_struct: dict[str, Any] = {}
        if start_date is not None:
            s_struct["date"] = start_date
        if start_type is not None:
            s_struct["type"] = start_type
        status_mod["startDateStruct"] = s_struct
    if primary_completion_date is not None or primary_completion_type is not None:
        p_struct: dict[str, Any] = {}
        if primary_completion_date is not None:
            p_struct["date"] = primary_completion_date
        if primary_completion_type is not None:
            p_struct["type"] = primary_completion_type
        status_mod["primaryCompletionDateStruct"] = p_struct
    if completion_date is not None or completion_type is not None:
        c_struct: dict[str, Any] = {}
        if completion_date is not None:
            c_struct["date"] = completion_date
        if completion_type is not None:
            c_struct["type"] = completion_type
        status_mod["completionDateStruct"] = c_struct

    design_mod: dict[str, Any] = {
        "studyType": study_type,
        "phases": ["PHASE3"] if phases is None else phases,
    }
    if enrollment_count is not None or enrollment_type is not None:
        enr: dict[str, Any] = {}
        if enrollment_count is not None:
            enr["count"] = enrollment_count
        if enrollment_type is not None:
            enr["type"] = enrollment_type
        design_mod["enrollmentInfo"] = enr

    locations = [
        {
            "facility": f"Fictional Site {i + 1}",
            "city": "Boston",
            "state": "Massachusetts",
            "country": "United States",
        }
        for i in range(n_sites)
    ]

    study: dict[str, Any] = {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": brief_title},
            "statusModule": status_mod,
            "designModule": design_mod,
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": lead_sponsor_name, "class": lead_sponsor_class},
                "collaborators": collaborators or [],
            },
            "conditionsModule": {
                "conditions": ["Type 2 Diabetes Mellitus"] if conditions is None else conditions,
            },
            "contactsLocationsModule": {"locations": locations},
        },
        "hasResults": has_results,
    }

    if meshes is not None:
        study["derivedSection"] = {
            "conditionBrowseModule": {
                "meshes": meshes,
                "ancestors": [],
            }
        }

    if has_results:
        default_flow: dict[str, Any] = {
            "preAssignmentDetails": "20 screened; 120 randomized.",
            "groups": [
                {"id": "FG000", "title": "Active Arm"},
                {"id": "FG001", "title": "Placebo Arm"},
            ],
            "periods": [
                {
                    "title": "Period 1: Double-Blind Treatment",
                    "milestones": [
                        {
                            "type": "STARTED",
                            "achievements": [
                                {"groupId": "FG000", "numSubjects": "60"},
                                {"groupId": "FG001", "numSubjects": "60"},
                            ],
                        },
                        {
                            "type": "COMPLETED",
                            "achievements": [
                                {"groupId": "FG000", "numSubjects": "54"},
                                {"groupId": "FG001", "numSubjects": "51"},
                            ],
                        },
                    ],
                    "dropWithdraws": [
                        {
                            "type": "Adverse Event",
                            "reasons": [
                                {"groupId": "FG000", "numSubjects": "4"},
                                {"groupId": "FG001", "numSubjects": "2"},
                            ],
                        },
                        {
                            "type": "Withdrawal by Subject",
                            "reasons": [
                                {"groupId": "FG000", "numSubjects": "2"},
                                {"groupId": "FG001", "numSubjects": "7"},
                            ],
                        },
                    ],
                }
            ],
        }
        default_outcomes: list[dict[str, Any]] = [
            {
                "type": "PRIMARY",
                "title": "Change from Baseline in HbA1c at Week 24",
                "unitOfMeasure": "percentage of HbA1c",
                "groups": [
                    {"id": "OG000", "title": "Active Arm"},
                    {"id": "OG001", "title": "Placebo Arm"},
                ],
                "denoms": [
                    {
                        "units": "Participants",
                        "counts": [
                            {"groupId": "OG000", "value": "58"},
                            {"groupId": "OG001", "value": "57"},
                        ],
                    }
                ],
            }
        ]
        study["resultsSection"] = {
            "participantFlowModule": default_flow if flow_module is None else flow_module,
            "outcomeMeasuresModule": {
                "outcomeMeasures": default_outcomes if outcome_measures is None else outcome_measures,
            },
        }
    return study


def make_weekly_record(nct_id: str = "NCT99100030", **overrides: Any) -> dict[str, Any]:
    """A standardized weekly record shaped like the `src/utils.py` projection (no `*_date_type`)."""
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "brief_title": "Weekly projection record with a projected completion date",
        "study_type": "INTERVENTIONAL",
        "phase": "PHASE3",
        "enrollment": 240,
        "enrollment_type": "ACTUAL",
        "status": "ACTIVE_NOT_RECRUITING",
        "start_date": "2024-02-01",
        "primary_completion_date": "2031-06-30",
        "completion_date": "2032-12-31",
        "results_date": "",
        "why_stopped": "",
        "sponsor_class": "INDUSTRY",
        "lead_sponsor_name": "Pfizer",
        "collaborators": [],
        "study_sites": [{"facility": "Clinic 1", "country": "United States"}],
        "conditions": ["Hypertension"],
    }
    rec.update(overrides)
    return rec
