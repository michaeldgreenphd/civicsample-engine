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
from tests.feasibility.conftest import make_ctgov_study, make_weekly_record


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
    # The weekly projection carries no date types, so the window is not shown as realized.
    assert m["durations"]["primary_completion"]["status"] == "date_type_unavailable_in_weekly_projection"
    assert m["durations"]["primary_completion"]["days"] is None
    assert m["durations"]["primary_completion"]["start_precision"] == "month"
    assert m["retention"]["flow_status"] == "flow_module_not_in_input_projection"


def test_weekly_projection_future_completion_excluded_without_date_type() -> None:
    m = extract_trial_metrics(make_weekly_record())
    for window in ("primary_completion", "study_completion"):
        dur = m["durations"][window]
        assert dur["status"] == "date_type_unavailable_in_weekly_projection"
        assert dur["eligible_for_realized_duration"] is False
        assert dur["start_date_type"] is None and dur["end_date_type"] is None
        assert dur["days"] is None and dur["midpoint_days"] is None and dur["midpoint_months"] is None
    # ACTUAL enrollment stays ACTUAL, but no accrual velocity is computed over an untyped window.
    assert m["enrollment"]["type"] == "ACTUAL"
    assert m["accrual"]["rate_status"] == "date_type_unavailable_in_weekly_projection"
    assert m["accrual"]["actual_participants_per_month"] is None
    assert m["accrual"]["min_rate"] is None and m["accrual"]["max_rate"] is None
    # The facility proxy does not depend on dates and is still computed.
    assert m["accrual"]["facility_metric_status"] == "computed"


def test_weekly_projection_status_precedence_and_recorded_types() -> None:
    # Missing and malformed dates keep their own, more specific statuses.
    missing = extract_trial_metrics(make_weekly_record(primary_completion_date="", completion_date=""))
    assert missing["durations"]["primary_completion"]["status"] == "missing_start_or_end_date"
    malformed = extract_trial_metrics(make_weekly_record(primary_completion_date="2031-13"))
    assert malformed["durations"]["primary_completion"]["status"] == "malformed_date"
    # An explicit ESTIMATED type still wins over the weekly status.
    est = extract_trial_metrics(make_weekly_record(primary_completion_date_type="ESTIMATED"))
    assert est["durations"]["primary_completion"]["status"] == "excluded_estimated_or_anticipated_date"
    # One recorded type is not enough; both ends must be typed.
    half = extract_trial_metrics(make_weekly_record(start_date_type="ACTUAL"))
    assert half["durations"]["primary_completion"]["status"] == "date_type_unavailable_in_weekly_projection"
    # If a weekly record ever carries both ACTUAL types, the window is evaluated normally.
    typed = extract_trial_metrics(
        make_weekly_record(
            primary_completion_date="2025-02-01",
            start_date_type="ACTUAL",
            primary_completion_date_type="ACTUAL",
        )
    )
    assert typed["durations"]["primary_completion"]["status"] == "exact_days"
    assert typed["accrual"]["rate_status"] == "computed"


def test_api_v2_actual_date_types_unaffected_by_weekly_rule() -> None:
    m = extract_trial_metrics(make_ctgov_study("NCT99100031"))
    prim = m["durations"]["primary_completion"]
    assert prim["status"] == "exact_days"
    assert prim["eligible_for_realized_duration"] is True
    assert prim["start_date_type"] == "ACTUAL" and prim["end_date_type"] == "ACTUAL"
    assert prim["days"] == 366
    assert m["durations"]["study_completion"]["status"] == "exact_days"
    assert m["accrual"]["rate_status"] == "computed"
    assert m["accrual"]["actual_participants_per_month"] is not None
    # compute_duration's default (API v2) path still reads an untyped date as before.
    assert compute_duration("2020-01-15", None, "2021-01-15", None)["status"] == "exact_days"


@pytest.mark.parametrize(
    "bad_group",
    [None, "FG002", ["FG002"], {"title": "Third arm"}, {"id": "", "title": "Third arm"}, {"id": 2}],
)
def test_one_malformed_flow_group_withholds_retention(bad_group: object) -> None:
    """A malformed group beside valid ones must not leave a partial STARTED/COMPLETED sum."""
    import copy

    study = make_ctgov_study("NCT99100060")
    ok = extract_period1_retention(study)
    assert ok["flow_status"] == "ok" and ok["completion_rate_status"] == "computed"

    broken = copy.deepcopy(study)
    broken["resultsSection"]["participantFlowModule"]["groups"].append(bad_group)
    ret = extract_period1_retention(broken)
    assert ret["flow_status"] == "malformed_participant_flow"
    assert ret["completion_rate_status"] == "withheld_missing_started_or_completed"
    assert ret.get("period1_completion_rate") is None
    assert ret.get("period1_started") is None


@pytest.mark.parametrize(
    "placebo_count, expected",
    [
        ("2", 6),        # both arms valid: 4 + 2
        ("NA", None),    # one arm non-numeric: no partial total
        ("-1", None),    # negative count
        (None, None),    # arm missing from the reason
    ],
)
def test_withdrawal_reason_tally_needs_every_arm(placebo_count: str | None, expected: int | None) -> None:
    import copy

    study = copy.deepcopy(make_ctgov_study("NCT99100061"))
    dws = study["resultsSection"]["participantFlowModule"]["periods"][0]["dropWithdraws"]
    reasons = [{"groupId": "FG000", "numSubjects": "4"}]
    if placebo_count is not None:
        reasons.append({"groupId": "FG001", "numSubjects": placebo_count})
    dws[:] = [{"type": "Adverse Event", "reasons": reasons}]
    ret = extract_period1_retention(study)
    assert ret["flow_status"] == "ok"
    assert ret["drop_withdraw_reasons"] == {"Adverse Event": expected}


def test_withdrawal_reason_tally_sums_repeated_labels_only_when_all_complete() -> None:
    import copy

    study = copy.deepcopy(make_ctgov_study("NCT99100062"))
    dws = study["resultsSection"]["participantFlowModule"]["periods"][0]["dropWithdraws"]
    full = [{"groupId": "FG000", "numSubjects": "1"}, {"groupId": "FG001", "numSubjects": "2"}]
    dws[:] = [{"type": "Other", "reasons": full}, {"type": "Other", "reasons": full}]
    assert extract_period1_retention(study)["drop_withdraw_reasons"] == {"Other": 6}
    dws[1] = {"type": "Other", "reasons": [{"groupId": "FG000", "numSubjects": "1"},
                                           {"groupId": "FG000", "numSubjects": "1"}]}
    assert extract_period1_retention(study)["drop_withdraw_reasons"] == {"Other": None}


def test_repeated_flow_group_id_withholds_retention() -> None:
    """A repeated group id would read its milestone count twice and double STARTED/COMPLETED."""
    import copy

    study = copy.deepcopy(make_ctgov_study("NCT99100063"))
    groups = study["resultsSection"]["participantFlowModule"]["groups"]
    groups.append({"id": "FG000", "title": "Active Arm (repeated)"})
    ret = extract_period1_retention(study)
    assert ret["flow_status"] == "malformed_participant_flow"
    assert ret.get("period1_started") is None
    assert ret.get("period1_completion_rate") is None


@pytest.mark.parametrize("milestone_index", [0, 1])  # STARTED, COMPLETED
def test_repeated_milestone_achievement_withholds_retention(milestone_index: int) -> None:
    """A second count for the same retained arm would replace the first (60 -> 999 inflates STARTED)."""
    import copy

    study = copy.deepcopy(make_ctgov_study("NCT99100064"))
    ms = study["resultsSection"]["participantFlowModule"]["periods"][0]["milestones"][milestone_index]
    ms["achievements"].append({"groupId": "FG000", "numSubjects": "999"})
    ret = extract_period1_retention(study)
    assert ret["flow_status"] == "malformed_participant_flow"
    assert ret.get("period1_started") is None
    assert ret.get("period1_completion_rate") is None


@pytest.mark.parametrize("milestone_index", [0, 1])  # STARTED, COMPLETED
def test_repeated_period1_milestone_type_withholds_retention(milestone_index: int) -> None:
    """A second STARTED (or COMPLETED) milestone with other counts leaves no single count."""
    import copy

    study = copy.deepcopy(make_ctgov_study("NCT99100065"))
    milestones = study["resultsSection"]["participantFlowModule"]["periods"][0]["milestones"]
    repeat = copy.deepcopy(milestones[milestone_index])
    for a in repeat["achievements"]:
        a["numSubjects"] = "40"
    milestones.append(repeat)
    ret = extract_period1_retention(study)
    assert ret["flow_status"] == "malformed_participant_flow"
    assert ret.get("period1_completion_rate") is None
