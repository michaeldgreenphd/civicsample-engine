"""Unit tests for `feasibility/metrics.py`."""
from __future__ import annotations

import pytest

from feasibility.metrics import (
    DAYS_PER_MONTH,
    FACILITY_BASIS,
    RATE_BASIS,
    compute_duration,
    extract_period1_retention,
    extract_trial_metrics,
    footprint_tier_for,
    normalize_enrollment,
    normalize_phase,
)
from tests.feasibility.conftest import make_ctgov_study


def test_exact_day_duration_and_single_day_zero() -> None:
    dur = compute_duration("2020-01-15", "ACTUAL", "2020-02-14", "ACTUAL")
    assert dur["status"] == "exact_days"
    assert dur["eligible_for_realized_duration"] is True
    assert dur["days"] == 30
    assert dur["min_days"] == 30
    assert dur["max_days"] == 30
    assert dur["min_months"] == round(30 / DAYS_PER_MONTH, 6)
    assert dur["max_months"] == dur["min_months"]

    zero_day = compute_duration("2021-05-10", "ACTUAL", "2021-05-10", "ACTUAL")
    assert zero_day["status"] == "exact_days"
    assert zero_day["days"] == 0
    assert zero_day["eligible_for_realized_duration"] is True


def test_partial_month_and_year_bounds_never_fabricate_day_one() -> None:
    # 2021-05 = [2021-05-01, 2021-05-31]; 2021-06 = [2021-06-01, 2021-06-30]
    # Exact calendar bounds: min_days = 1 (May 31 -> Jun 1), max_days = 60 (May 1 -> Jun 30).
    # Fabricating -01 would have yielded days == 31!
    dur = compute_duration("2021-05", "ACTUAL", "2021-06", "ACTUAL")
    assert dur["status"] == "bounded_partial_precision"
    assert dur["eligible_for_realized_duration"] is True
    assert dur["days"] is None
    assert dur["min_days"] == 1
    assert dur["max_days"] == 60
    assert dur["midpoint_days"] == 30.5
    assert dur["min_months"] == round(1 / DAYS_PER_MONTH, 6)
    assert dur["max_months"] == round(60 / DAYS_PER_MONTH, 6)

    # Leap-year February 2020 ([2020-02-01, 2020-02-29]) -> April 2020 ([2020-04-01, 2020-04-30]):
    # min_days = (2020-04-01 - 2020-02-29) = 32; max_days = (2020-04-30 - 2020-02-01) = 89.
    leap = compute_duration("2020-02", "ACTUAL", "2020-04", "ACTUAL")
    assert leap["days"] is None
    assert (leap["min_days"], leap["max_days"]) == (32, 89)

    # Year-precision bounds: 2019 ([2019-01-01, 2019-12-31]) -> 2021 ([2021-01-01, 2021-12-31])
    yr = compute_duration("2019", "ACTUAL", "2021", "ACTUAL")
    assert yr["status"] == "bounded_partial_precision"
    assert yr["days"] is None
    assert yr["min_days"] == 367  # 2019-12-31 -> 2021-01-01 across leap year 2020
    assert yr["max_days"] == 1095


def test_overlapping_inverted_missing_and_malformed_durations() -> None:
    same_month = compute_duration("2021-05", "ACTUAL", "2021-05", "ACTUAL")
    assert same_month["status"] == "overlapping_or_zero_at_precision"
    assert same_month["eligible_for_realized_duration"] is False
    assert same_month["days"] is None
    assert same_month["min_days"] == 0
    assert same_month["max_days"] == 30

    year_month_overlap = compute_duration("2021", "ACTUAL", "2021-06", "ACTUAL")
    assert year_month_overlap["status"] == "overlapping_or_zero_at_precision"
    assert year_month_overlap["eligible_for_realized_duration"] is False

    boundary_touch = compute_duration("2021-05-01", "ACTUAL", "2021-05", "ACTUAL")
    assert boundary_touch["status"] == "overlapping_or_zero_at_precision"
    assert boundary_touch["eligible_for_realized_duration"] is False

    inv_partial = compute_duration("2021-06", "ACTUAL", "2021-05", "ACTUAL")
    assert inv_partial["status"] == "inverted_dates_anomaly"
    assert inv_partial["eligible_for_realized_duration"] is False
    assert inv_partial["min_days"] is None

    inv_exact = compute_duration("2021-05-10", "ACTUAL", "2021-05-09", "ACTUAL")
    assert inv_exact["status"] == "inverted_dates_anomaly"
    assert inv_exact["eligible_for_realized_duration"] is False

    missing = compute_duration(None, None, "2021-05-10", "ACTUAL")
    assert missing["status"] == "missing_start_or_end_date"
    assert missing["eligible_for_realized_duration"] is False

    malformed = compute_duration("2021-02-29", "ACTUAL", "2021-06-01", "ACTUAL")
    assert malformed["status"] == "malformed_date"
    assert malformed["eligible_for_realized_duration"] is False


@pytest.mark.parametrize("future_type", ["ESTIMATED", "ANTICIPATED"])
def test_estimated_and_anticipated_dates_excluded_from_realized_duration(future_type: str) -> None:
    dur_end = compute_duration("2023-01-10", "ACTUAL", "2027-06", future_type)
    assert dur_end["status"] == "excluded_estimated_or_anticipated_date"
    assert dur_end["eligible_for_realized_duration"] is False
    assert dur_end["days"] is None
    assert dur_end["min_days"] is None

    dur_start = compute_duration("2026-09", future_type, "2028-09", future_type)
    assert dur_start["status"] == "excluded_estimated_or_anticipated_date"
    assert dur_start["eligible_for_realized_duration"] is False


def test_strict_actual_vs_estimated_enrollment_separation() -> None:
    # ESTIMATED target enrollment MUST NEVER enter actual accrual or facility ratios.
    est_study = make_ctgov_study(
        "NCT99100002",
        enrollment_count=500,
        enrollment_type="ESTIMATED",
        n_sites=10,
    )
    m_est = extract_trial_metrics(est_study)
    assert m_est["enrollment"]["type"] == "ESTIMATED"
    assert m_est["enrollment"]["count"] == 500
    assert m_est["accrual"]["rate_status"] == "excluded_non_actual_enrollment"
    assert m_est["accrual"]["actual_participants_per_month"] is None
    assert m_est["accrual"]["min_rate"] is None
    assert m_est["accrual"]["max_rate"] is None
    assert m_est["accrual"]["facility_metric_status"] == "excluded_non_actual_enrollment"
    assert m_est["accrual"]["actual_participants_per_listed_facility"] is None
    assert m_est["accrual"]["rate_basis"] == RATE_BASIS
    assert m_est["accrual"]["facility_basis"] == FACILITY_BASIS

    # Legacy ANTICIPATED maps to ESTIMATED while preserving raw_type.
    ant = normalize_enrollment(250, "ANTICIPATED")
    assert ant["type"] == "ESTIMATED"
    assert ant["raw_type"] == "ANTICIPATED"

    # MISSING_TYPE is also strictly excluded.
    miss_study = make_ctgov_study("NCT99100003", enrollment_count=80, enrollment_type=None)
    m_miss = extract_trial_metrics(miss_study)
    assert m_miss["enrollment"]["type"] == "MISSING_TYPE"
    assert m_miss["accrual"]["actual_participants_per_month"] is None
    assert m_miss["accrual"]["actual_participants_per_listed_facility"] is None

    # ACTUAL enrollment over partial YYYY-MM bounds yields min_rate <= midpoint <= max_rate.
    act_study = make_ctgov_study(
        "NCT99100004",
        start_date="2020-01",
        primary_completion_date="2020-07",
        enrollment_count=180,
        enrollment_type="ACTUAL",
        n_sites=6,
    )
    m_act = extract_trial_metrics(act_study)
    acc = m_act["accrual"]
    assert acc["rate_status"] == "computed"
    assert acc["min_rate"] < acc["actual_participants_per_month"] < acc["max_rate"]
    assert acc["actual_participants_per_listed_facility"] == 30.0

    # ACTUAL enrollment with 0 listed facilities withholds facility metric without error.
    no_site = make_ctgov_study("NCT99100005", enrollment_count=60, enrollment_type="ACTUAL", n_sites=0)
    m_nosite = extract_trial_metrics(no_site)
    assert m_nosite["facilities"]["footprint_tier"] == "no_listed_sites"
    assert m_nosite["accrual"]["facility_metric_status"] == "no_listed_facilities"
    assert m_nosite["accrual"]["actual_participants_per_listed_facility"] is None


def test_footprint_tiers_and_phase_normalization() -> None:
    assert footprint_tier_for(0) == "no_listed_sites"
    assert footprint_tier_for(1) == "single_site"
    assert footprint_tier_for(2) == "2_to_10_sites"
    assert footprint_tier_for(10) == "2_to_10_sites"
    assert footprint_tier_for(11) == "11_to_50_sites"
    assert footprint_tier_for(50) == "11_to_50_sites"
    assert footprint_tier_for(51) == "51_plus_sites"

    assert normalize_phase(["PHASE3", "PHASE2"]) == "PHASE2/PHASE3"
    assert normalize_phase([]) == "PHASE_NOT_STATED"
    assert normalize_phase(["NA"]) == "NA"
    assert normalize_phase("PHASE1, PHASE2") == "PHASE1/PHASE2"


def test_period1_retention_edge_cases_and_total_group_guard() -> None:
    # Multi-group flow with a duplicate "Total" group: Total is excluded so arms aren't doubled.
    study_with_total = make_ctgov_study(
        "NCT99100010",
        flow_module={
            "preAssignmentDetails": "Screened 150 participants.",
            "groups": [
                {"id": "FG000", "title": "Drug A"},
                {"id": "FG001", "title": "Placebo"},
                {"id": "FG002", "title": "Total"},
            ],
            "periods": [
                {
                    "title": "Period 1",
                    "milestones": [
                        {
                            "type": "STARTED",
                            "achievements": [
                                {"groupId": "FG000", "numSubjects": "50"},
                                {"groupId": "FG001", "numSubjects": "50"},
                                {"groupId": "FG002", "numSubjects": "100"},
                            ],
                        },
                        {
                            "type": "COMPLETED",
                            "achievements": [
                                {"groupId": "FG000", "numSubjects": "45"},
                                {"groupId": "FG001", "numSubjects": "40"},
                                {"groupId": "FG002", "numSubjects": "85"},
                            ],
                        },
                    ],
                    "dropWithdraws": [
                        {
                            "type": "Lost to Follow-Up",
                            "reasons": [
                                {"groupId": "FG000", "numSubjects": "5"},
                                {"groupId": "FG001", "numSubjects": "10"},
                                {"groupId": "FG002", "numSubjects": "15"},
                            ],
                        }
                    ],
                },
                {"title": "Period 2 Extension", "milestones": []},
            ],
        },
    )
    ret = extract_period1_retention(study_with_total)
    assert ret["flow_status"] == "ok"
    assert ret["n_periods"] == 2
    assert ret["has_pre_assignment_details"] is True
    assert ret["period1_started"] == 100
    assert ret["period1_completed"] == 85
    assert ret["period1_completion_rate"] == 0.85
    assert ret["drop_withdraw_reasons"] == {"Lost to Follow-Up": 15}

    # Zero STARTED withholds completion rate instead of ZeroDivisionError or 0.0 coercion.
    zero_started = make_ctgov_study(
        "NCT99100011",
        flow_module={
            "groups": [{"id": "FG000", "title": "Single Arm"}],
            "periods": [
                {
                    "title": "Overall Study",
                    "milestones": [
                        {"type": "STARTED", "achievements": [{"groupId": "FG000", "numSubjects": "0"}]},
                        {"type": "COMPLETED", "achievements": [{"groupId": "FG000", "numSubjects": "0"}]},
                    ],
                }
            ],
        },
    )
    ret_zero = extract_period1_retention(zero_started)
    assert ret_zero["period1_started"] == 0
    assert ret_zero["period1_completion_rate"] is None
    assert ret_zero["completion_rate_status"] == "withheld_zero_started"
    assert ret_zero["has_pre_assignment_details"] is False

    # COMPLETED > STARTED anomaly withholds rate while preserving posted integers.
    anomaly = make_ctgov_study(
        "NCT99100012",
        flow_module={
            "groups": [{"id": "FG000", "title": "Single Arm"}],
            "periods": [
                {
                    "title": "Overall Study",
                    "milestones": [
                        {"type": "STARTED", "achievements": [{"groupId": "FG000", "numSubjects": "20"}]},
                        {"type": "COMPLETED", "achievements": [{"groupId": "FG000", "numSubjects": "22"}]},
                    ],
                }
            ],
        },
    )
    ret_anom = extract_period1_retention(anomaly)
    assert ret_anom["period1_started"] == 20
    assert ret_anom["period1_completed"] == 22
    assert ret_anom["period1_completion_rate"] is None
    assert ret_anom["completion_rate_status"] == "withheld_completed_exceeds_started_anomaly"

    # Unposted results leave has_pre_assignment_details as None (never coerced to False).
    unposted = make_ctgov_study("NCT99100013", has_results=False)
    ret_unposted = extract_period1_retention(unposted)
    assert ret_unposted["flow_status"] == "no_posted_results"
    assert ret_unposted["has_pre_assignment_details"] is None
    assert ret_unposted["period1_completion_rate"] is None


def test_weekly_record_projection_input() -> None:
    weekly = {
        "nct_id": "NCT99100020",
        "brief_title": "Weekly projection record",
        "study_type": "INTERVENTIONAL",
        "phase": "PHASE2",
        "enrollment": 0,
        "enrollment_type": "N/A",
        "status": "COMPLETED",
        "start_date": "2020-03",
        "primary_completion_date": "2021-03",
        "completion_date": "2021-06",
        "results_date": "2022-01-10",
        "why_stopped": "",
        "sponsor_class": "NIH",
        "lead_sponsor_name": "National Cancer Institute (NCI)",
        "collaborators": [],
        "study_sites": [{"facility": "Clinic 1", "country": "United States"}],
        "conditions": ["Breast Cancer"],
    }
    m = extract_trial_metrics(weekly)
    assert m["enrollment"]["type"] == "MISSING_TYPE"
    assert m["enrollment"]["count"] is None
    assert m["durations"]["primary_completion"]["status"] == "bounded_partial_precision"
    assert m["durations"]["primary_completion"]["days"] is None
    assert m["retention"]["flow_status"] == "flow_module_not_in_input_projection"
