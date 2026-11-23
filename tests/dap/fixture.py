"""Fictional offline study records, MeSH sidecars, and curated sponsor rules for DAP tests.

All NCT IDs use fictional ``NCT990001xx`` identifiers; zero network calls.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any


def make_raw_ctgov_study(
    nct_id: str,
    *,
    locations: list[dict[str, Any]] | None = None,
    conditions: list[str] | None = None,
    phases: list[str] | None = None,
    start_date: str | None = "2023-04-15",
    primary_completion_date: str | None = "2024-06-30",
    lead_sponsor_name: str = "Fictional Pharma Inc.",
    lead_sponsor_class: str = "INDUSTRY",
    collaborators: list[dict[str, str]] | None = None,
    enrollment: int | None = 120,
    has_results: bool = True,
    baseline_denom: int | None = 100,
    measures: list[dict[str, Any]] | None = None,
    meshes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Construct a synthetic ClinicalTrials.gov API v2 study document."""
    status_mod: dict[str, Any] = {"overallStatus": "COMPLETED"}
    if start_date is not None:
        status_mod["startDateStruct"] = {"date": start_date, "type": "ACTUAL"}
    if primary_completion_date is not None:
        status_mod["primaryCompletionDateStruct"] = {
            "date": primary_completion_date,
            "type": "ACTUAL",
        }

    design_mod: dict[str, Any] = {
        "studyType": "INTERVENTIONAL",
        "phases": phases if phases is not None else ["PHASE3"],
    }
    if enrollment is not None:
        design_mod["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}

    sponsor_mod: dict[str, Any] = {
        "leadSponsor": {"name": lead_sponsor_name, "class": lead_sponsor_class},
    }
    if collaborators:
        sponsor_mod["collaborators"] = collaborators

    protocol: dict[str, Any] = {
        "identificationModule": {
            "nctId": nct_id,
            "briefTitle": f"Fictional trial {nct_id}",
        },
        "statusModule": status_mod,
        "designModule": design_mod,
        "conditionsModule": {
            "conditions": conditions if conditions is not None else ["Type 2 Diabetes"],
        },
        "sponsorCollaboratorsModule": sponsor_mod,
    }
    if locations is not None:
        protocol["contactsLocationsModule"] = {"locations": locations}

    study: dict[str, Any] = {
        "protocolSection": protocol,
        "hasResults": has_results,
    }

    if has_results:
        baseline_mod: dict[str, Any] = {
            "groups": [{"id": "BG000", "title": "Total"}],
            "measures": measures if measures is not None else [],
        }
        if baseline_denom is not None:
            baseline_mod["denoms"] = [
                {
                    "units": "Participants",
                    "counts": [{"groupId": "BG000", "value": str(baseline_denom)}],
                }
            ]
        study["resultsSection"] = {"baselineCharacteristicsModule": baseline_mod}

    if meshes is not None:
        study["derivedSection"] = {
            "conditionBrowseModule": {
                "meshes": meshes,
                "ancestors": [],
            }
        }

    return study


def make_measure(
    title: str,
    rows: list[tuple[str, str]],
    *,
    param_type: str = "COUNT_OF_PARTICIPANTS",
    unit_of_measure: str = "Participants",
) -> dict[str, Any]:
    """Construct a single CT.gov baseline measure with ``BG000`` counts."""
    return {
        "title": title,
        "paramType": param_type,
        "unitOfMeasure": unit_of_measure,
        "classes": [
            {
                "categories": [
                    {
                        "title": label,
                        "measurements": [{"groupId": "BG000", "value": value}],
                    }
                    for label, value in rows
                ]
            }
        ],
    }


def write_fictional_sponsor_rules(path: Path) -> Path:
    """Write a deterministic curated alias CSV exercising attribute, subsidiary, shared, and exclude rules."""
    csv_text = (
        "canonical,rule_type,pattern,status,shared,note\n"
        "Acme Bio,exact_normalized,acme bio,attribute,no,Fictional parent company rule for unit tests\n"
        "Acme Bio,subsidiary_of,acme bio,attribute,no,Fictional subsidiary rollup rule for unit tests\n"
        "Zenith Pharma,exact_normalized,zenith pharma,attribute,no,Second canonical sponsor for peer scorecards\n"
        "Acme Bio,exact_literal,Acme Bio & Zenith Pharma,attribute,yes,Shared partnership literal attributed to Acme\n"
        "Zenith Pharma,exact_literal,Acme Bio & Zenith Pharma,attribute,yes,Shared partnership literal attributed to Zenith\n"
        "Merck & Co,exact_normalized,merck,attribute,no,US Merck & Co exact normalized match\n"
        "Merck & Co,exact_normalized,merck kgaa,exclude,no,Distinct German company Merck KGaA excluded from Merck & Co\n"
    )
    path.write_text(csv_text, encoding="utf-8")
    return path


def build_fictional_cohort() -> list[dict[str, Any]]:
    """Build 12 fictional studies spanning footprints, eras, table structures, and sponsors."""
    us_loc = {"facility": "Site US-1", "city": "Boston", "state": "MA", "country": "United States"}
    pr_loc = {"facility": "Site PR-1", "city": "San Juan", "country": "Puerto Rico"}
    de_loc = {"facility": "Site DE-1", "city": "Berlin", "country": "Germany"}
    jp_loc = {"facility": "Site JP-1", "city": "Tokyo", "country": "Japan"}
    ca_loc = {"facility": "Site CA-1", "city": "Toronto", "country": "Canada"}

    studies: list[dict[str, Any]] = []

    # 6 US-only Phase 3 Type 2 Diabetes trials in post_2022_fdora so cell >= 5 (not withheld)
    shares_specs = [
        ("NCT99000101", 60, 20, 10, 5, 5, 15, 80, 5, 52, 48, "Acme Bio Inc."),
        ("NCT99000102", 55, 25, 10, 5, 5, 20, 75, 5, 50, 50, "LegacySub, a wholly owned subsidiary of Acme Bio"),
        ("NCT99000103", 50, 30, 10, 5, 5, 25, 70, 5, 55, 45, "Acme Bio & Zenith Pharma"),
        ("NCT99000104", 65, 15, 10, 5, 5, 10, 85, 5, 48, 52, "Zenith Pharma Ltd."),
        ("NCT99000105", 70, 10, 10, 0, 10, 12, 80, 8, 45, 55, "Merck & Co., Inc."),
        ("NCT99000106", 45, 35, 10, 5, 5, 30, 65, 5, 60, 40, "Merck KGaA"),
    ]
    for idx, (nct, w, b, a, o, unk, h, nh, eunk, fem, male, sponsor) in enumerate(shares_specs):
        locs = [us_loc, pr_loc] if idx == 0 else [us_loc]
        studies.append(
            make_raw_ctgov_study(
                nct,
                locations=locs,
                conditions=["Type 2 Diabetes Mellitus"],
                phases=["PHASE3"],
                start_date="2023-03-10",
                lead_sponsor_name=sponsor,
                enrollment=115,  # deliberate gap vs 100 baseline_denom to verify enrollment_minus_parsed
                baseline_denom=100,
                measures=[
                    make_measure(
                        "Race (NIH/OMB)",
                        [
                            ("White", str(w)),
                            ("Black or African American", str(b)),
                            ("Asian", str(a)),
                            ("Other", str(o)),
                            ("Unknown or Not Reported", str(unk)),
                        ],
                    ),
                    make_measure(
                        "Ethnicity (NIH/OMB)",
                        [
                            ("Hispanic or Latino", str(h)),
                            ("Not Hispanic or Latino", str(nh)),
                            ("Unknown or Not Reported", str(eunk)),
                        ],
                    ),
                    make_measure(
                        "Sex: Female, Male",
                        [("Female", str(fem)), ("Male", str(male))],
                    ),
                ],
                meshes=[{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}],
            )
        )

    # NCT99000107: Multinational with US sites (1/4 = 25% -> us_share_25_to_lt_50),
    # combined race/ethnicity table (MUST be quarantined!) + single-category ethnicity!
    studies.append(
        make_raw_ctgov_study(
            "NCT99000107",
            locations=[us_loc, de_loc, jp_loc, ca_loc],
            conditions=["Type 2 Diabetes Mellitus"],
            phases=["PHASE3"],
            start_date="2019-06-01",
            lead_sponsor_name="Unreviewed Academic Center",
            lead_sponsor_class="OTHER",
            baseline_denom=100,
            measures=[
                make_measure(
                    "Race/Ethnicity, Customized",
                    [
                        ("White", "60"),
                        ("Black or African American", "20"),
                        ("Hispanic or Latino", "15"),
                        ("Unknown", "5"),
                    ],
                ),
                make_measure(
                    "Sex: Female, Male",
                    [("Unknown", "100")],  # explicit_unknown_only!
                ),
            ],
            meshes=[{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}],
        )
    )

    # NCT99000108: US-site trial with posted results OMITTING race and ethnicity tables
    studies.append(
        make_raw_ctgov_study(
            "NCT99000108",
            locations=[us_loc, de_loc],  # 1/2 = 50% -> us_share_50_to_lt_75
            conditions=["Breast Cancer"],
            phases=["PHASE2"],
            start_date="2015-05-20",
            lead_sponsor_name="Acme Bio",
            baseline_denom=80,
            measures=[
                make_measure(
                    "Sex: Female, Male",
                    [("Female", "80"), ("Male", "0")],
                )
            ],
            meshes=[{"id": "D001943", "term": "Breast Neoplasms"}],
        )
    )

    # NCT99000109: Ex-US only trial, partial date straddling 2017-01-18 -> era_indeterminate
    studies.append(
        make_raw_ctgov_study(
            "NCT99000109",
            locations=[de_loc, jp_loc],
            conditions=["Breast Cancer"],
            phases=["PHASE3"],
            start_date="2017-01",
            lead_sponsor_name="Zenith Pharma",
            has_results=False,
        )
    )

    # NCT99000110: Facilities missing country + COUNT_OF_UNITS non-participant sex measure
    studies.append(
        make_raw_ctgov_study(
            "NCT99000110",
            locations=[{"facility": "Investigational Site 01"}],
            conditions=["Heart Failure"],
            phases=["PHASE2", "PHASE3"],
            start_date="2022-12",  # straddles 2022-12-29 -> era_indeterminate
            lead_sponsor_name="Acme Bio",
            baseline_denom=50,
            measures=[
                make_measure(
                    "Sex: Female, Male",
                    [("Female", "30"), ("Male", "20")],
                    param_type="COUNT_OF_UNITS",
                    unit_of_measure="Eyes",
                )
            ],
        )
    )

    # NCT99000111: No listed facilities + uninformative sex measure
    studies.append(
        make_raw_ctgov_study(
            "NCT99000111",
            locations=[],
            conditions=["Heart Failure"],
            phases=["PHASE1"],
            start_date=None,
            primary_completion_date="2021-05-01",
            lead_sponsor_name="Unreviewed Biotech LLC",
            baseline_denom=40,
            measures=[
                make_measure(
                    "Sex: Female, Male",
                    [("Female", "0"), ("Male", "0")],
                )
            ],
        )
    )

    return studies


def write_studies_dir(studies: list[dict[str, Any]], target_dir: Path) -> Path:
    """Write studies as individual gzipped JSON files in ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for s in studies:
        nct = s["protocolSection"]["identificationModule"]["nctId"]
        raw = (json.dumps(s, sort_keys=True) + "\n").encode("utf-8")
        (target_dir / f"{nct}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    return target_dir


WEEKLY_EXTRACTED_AT: str = "2026-11-21T00:00:00+00:00"
WEEKLY_PIPELINE_COMMIT: str = "deadbeef1234"


def build_weekly_full_records_doc() -> dict[str, Any]:
    """Five fictional weekly full records (``NCT99000401..405``) in the ``src/full_records.py`` container."""
    return {
        "extracted_at": WEEKLY_EXTRACTED_AT,
        "pipeline_commit": WEEKLY_PIPELINE_COMMIT,
        "data": [
            {
                "nct_id": f"NCT9900040{i}",
                "brief_title": f"Weekly trial {i}",
                "study_type": "INTERVENTIONAL",
                "phase": "PHASE3",
                "enrollment": 100,
                "enrollment_type": "ACTUAL",
                "status": "COMPLETED",
                "start_date": "2023-06-01",
                "primary_completion_date": "2024-06-01",
                "results_date": "2024-10-01",
                "sponsor_class": "INDUSTRY",
                "lead_sponsor_name": "Acme Bio",
                "collaborators": [],
                "study_sites": [{"facility": "US1", "state": "MA", "country": "United States"}],
                "conditions": ["Type 2 Diabetes"],
                "race": {
                    "reported": True,
                    "omb_totals": {
                        "white": 60,
                        "black_african_american": 25,
                        "asian": 10,
                        "unknown_not_reported": 5,
                    },
                    "raw_categories": [
                        {"omb_category": "white", "original": "White", "count": 60},
                        {"omb_category": "black_african_american", "original": "Black", "count": 25},
                        {"omb_category": "asian", "original": "Asian", "count": 10},
                        {"omb_category": "unknown_not_reported", "original": "Unknown", "count": 5},
                    ],
                    "skipped_ethnicity_rows": [],
                    "flags": [],
                },
                "ethnicity": {
                    "reported": True,
                    "omb_totals": {
                        "hispanic_latino": 15,
                        "not_hispanic_latino": 80,
                        "unknown_not_reported": 5,
                    },
                    "raw_categories": [
                        {"omb_category": "hispanic_latino", "original": "Hispanic or Latino", "count": 15},
                        {"omb_category": "not_hispanic_latino", "original": "Not Hispanic or Latino", "count": 80},
                    ],
                    "flags": [],
                },
                "sex_gender": {
                    "sex_report_status": "reported",
                    "reported_sex": True,
                    "reported_gender": False,
                    "reported_both": False,
                    "n_female": 50.0,
                    "n_male": 50.0,
                    "n_unknown": 0.0,
                    "n_gender_diverse": 0.0,
                    "n_ambiguous_gender": 0.0,
                    "is_participant_count": True,
                    "uninformative_reason": None,
                    "declared_not_collected": False,
                    "enrollment_minus_parsed": 0.0,
                    "parser_rules_version": "1.0.0",
                },
            }
            for i in range(1, 6)
        ],
    }
