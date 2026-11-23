"""Pure, deterministic per-trial operational feasibility metrics.

Reads either a full ClinicalTrials.gov API v2 study document (`protocolSection`, optional
`resultsSection`, optional `derivedSection`, `hasResults`) or a standardized weekly full-record
dict (`src/full_records.py`). When `protocolSection` is present on a hybrid dict, raw registry
fields take precedence so date-type enums (`ACTUAL` vs `ESTIMATED`/`ANTICIPATED`), unprojected
facility rows, and absent enrollment counts are preserved without weekly defaults.

Core analytical invariants:
1. Date precision: Registry dates (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`) are mapped to their exact
   calendar day intervals `[earliest, latest]`. Day `-01` defaults are NEVER fabricated.
   - Both `YYYY-MM-DD` and `end >= start`: `status = "exact_days"`, exact integer `days`.
   - At least one partial (`YYYY-MM` or `YYYY`) and `end.earliest > start.latest`:
     `status = "bounded_partial_precision"`, `days = None`, exact `[min_days, max_days]` and
     `[min_months, max_months]` calendar bounds.
   - Partial intervals overlap or touch (`start.latest >= end.earliest` while
     `end.latest >= start.earliest`): `status = "overlapping_or_zero_at_precision"`, excluded
     from realized-duration percentiles.
   - `end.latest < start.earliest`: `status = "inverted_dates_anomaly"`, excluded.
   - Start or completion date typed `ESTIMATED` or `ANTICIPATED`:
     `status = "excluded_estimated_or_anticipated_date"`, excluded from realized durations.
   - Weekly projection record (`src/utils.py`) without a start or end date type:
     `status = "date_type_unavailable_in_weekly_projection"`, excluded from realized durations.
     The projection keeps `start_date` / `primary_completion_date` / `completion_date` but drops
     each struct's `type`, so a projected (future) completion date is indistinguishable from an
     ACTUAL one. Absence of the type is recorded, never read as ACTUAL.
2. Enrollment type separation: `enrollmentInfo.type` is normalized strictly into
   `{"ACTUAL", "ESTIMATED", "MISSING_TYPE"}` (`ANTICIPATED` maps to `ESTIMATED` while keeping
   `raw_type` verbatim). Accrual velocity (`actual_participants_per_month` and `[min_rate, max_rate]`)
   and `actual_participants_per_listed_facility` are computed ONLY when `type == "ACTUAL"` and
   `count >= 0` — `ESTIMATED` target enrollment is NEVER pooled into realized accrual.
3. Proxy metadata: Registry enrollment is trial-total over the trial's calendar window, and
   `contactsLocationsModule.locations` lists registered facilities rather than site-level accrual.
   Every trial row explicitly stamps `rate_basis` and `facility_basis`.
4. Participant Flow Period 1 retention: Reads `resultsSection.participantFlowModule` (`periods[0]`),
   excluding any duplicate `"Total"` group when multiple flow groups are posted (matching Rule T in
   `outcomes/results_structure.py`). `period1_completion_rate` is withheld (`None`) when
   `STARTED == 0`, missing/non-numeric, or when `COMPLETED > STARTED` (data-entry anomaly).
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import date
from typing import Any

# Exact Gregorian mean month length (365.2425 days/year / 12 months/year = 30.436875 days/month).
# Used strictly for unit conversion between exact day bounds and month bounds.
DAYS_PER_MONTH: float = 365.2425 / 12.0

RATE_BASIS = "trial_total_actual_over_calendar_window"
FACILITY_BASIS = "listed_contacts_locations_not_site_level_enrollment"

ENROLLMENT_TYPES = ("ACTUAL", "ESTIMATED", "MISSING_TYPE")

DURATION_STATUSES = (
    "exact_days",
    "bounded_partial_precision",
    "overlapping_or_zero_at_precision",
    "inverted_dates_anomaly",
    "excluded_estimated_or_anticipated_date",
    "date_type_unavailable_in_weekly_projection",
    "missing_start_or_end_date",
    "malformed_date",
)

ACCRUAL_RATE_STATUSES = (
    "computed",
    "excluded_non_actual_enrollment",
    "missing_enrollment_count",
    "excluded_estimated_or_anticipated_date",
    "date_type_unavailable_in_weekly_projection",
    "missing_start_or_end_date",
    "overlapping_or_zero_at_precision",
    "inverted_dates_anomaly",
    "zero_duration_window",
    "malformed_date",
)

FACILITY_METRIC_STATUSES = (
    "computed",
    "excluded_non_actual_enrollment",
    "missing_enrollment_count",
    "no_listed_facilities",
)

FOOTPRINT_TIERS = (
    "single_site",
    "2_to_10_sites",
    "11_to_50_sites",
    "51_plus_sites",
    "no_listed_sites",
)

FLOW_STATUSES = (
    "ok",
    "no_posted_results",
    "no_participant_flow_module",
    "flow_module_not_in_input_projection",
    "malformed_participant_flow",
    "no_periods",
    "no_flow_groups",
    "no_started_milestone_in_period1",
    "no_completed_milestone_in_period1",
    "missing_or_non_numeric_group_count",
)

COMPLETION_RATE_STATUSES = (
    "computed",
    "withheld_zero_started",
    "withheld_missing_started_or_completed",
    "withheld_completed_exceeds_started_anomaly",
)

PHASE_ORDER = (
    "EARLY_PHASE1",
    "PHASE1",
    "PHASE2",
    "PHASE3",
    "PHASE4",
    "NA",
)

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def is_num(val: Any) -> bool:
    """True when `val` parses as a finite number after removing thousands commas.

    Strings such as `'NA'`, `''`, and `None` return False and are never coerced to zero.
    Booleans (`True`/`False`) are rejected explicitly because `isinstance(True, int)` in Python.
    """
    if val is None or isinstance(val, bool):
        return False
    try:
        parsed = float(str(val).replace(",", "").strip())
    except ValueError:
        return False
    return math.isfinite(parsed)


def parse_num(val: Any) -> float | None:
    """Finite float value of `val`, or `None` when not numeric."""
    return float(str(val).replace(",", "").strip()) if is_num(val) else None


def plain_number(val: float | None) -> int | float | None:
    """Return integer-valued floats as `int` so whole participant counts serialize cleanly."""
    if val is None:
        return None
    return int(val) if float(val).is_integer() else val


def parse_date_bounds(raw: Any) -> dict[str, Any]:
    """Parse a registry date string into its exact calendar bounds `[earliest, latest]`.

    Supported registry precisions:
      - `YYYY-MM-DD` -> `precision = "day"`, `earliest == latest == date(Y, M, D)`
      - `YYYY-MM`    -> `precision = "month"`, `[date(Y, M, 1), date(Y, M, last_day_of_month)]`
      - `YYYY`       -> `precision = "year"`, `[date(Y, 1, 1), date(Y, 12, 31)]`

    Missing (`None` or blank) returns `state = "missing"`. Unparseable strings or invalid calendar
    dates (e.g. `2021-02-29`) return `state = "malformed"`. Day `-01` is NEVER fabricated as a
    point estimate.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {
            "raw": None,
            "state": "missing",
            "precision": None,
            "earliest": None,
            "latest": None,
        }
    if not isinstance(raw, str):
        return {
            "raw": str(raw),
            "state": "malformed",
            "precision": None,
            "earliest": None,
            "latest": None,
        }
    text = raw.strip()
    match = _DATE_RE.fullmatch(text)
    if not match:
        return {
            "raw": text,
            "state": "malformed",
            "precision": None,
            "earliest": None,
            "latest": None,
        }
    year = int(match.group(1))
    month_s = match.group(2)
    day_s = match.group(3)
    try:
        if day_s is not None and month_s is not None:
            exact = date(year, int(month_s), int(day_s))
            return {
                "raw": text,
                "state": "ok",
                "precision": "day",
                "earliest": exact,
                "latest": exact,
            }
        if month_s is not None:
            month = int(month_s)
            last_day = calendar.monthrange(year, month)[1]
            return {
                "raw": text,
                "state": "ok",
                "precision": "month",
                "earliest": date(year, month, 1),
                "latest": date(year, month, last_day),
            }
        return {
            "raw": text,
            "state": "ok",
            "precision": "year",
            "earliest": date(year, 1, 1),
            "latest": date(year, 12, 31),
        }
    except ValueError:
        return {
            "raw": text,
            "state": "malformed",
            "precision": None,
            "earliest": None,
            "latest": None,
        }


def _clean_date_type(raw_type: Any) -> str | None:
    if not isinstance(raw_type, str):
        return None
    cleaned = raw_type.strip().upper()
    return cleaned if cleaned and cleaned != "N/A" else None


def compute_duration(
    start_raw: Any,
    start_type: Any = None,
    end_raw: Any = None,
    end_type: Any = None,
    *,
    from_weekly_projection: bool = False,
) -> dict[str, Any]:
    """Compute date-precision-aware duration from a start date struct to an end date struct.

    Excludes any window where `start_type` or `end_type` is `ESTIMATED` or `ANTICIPATED` from
    realized-duration distributions (`status = "excluded_estimated_or_anticipated_date"`).

    `from_weekly_projection=True` marks a standardized weekly record (`src/utils.py`), whose
    projection writes the three date strings but no `*_date_type` fields. There a missing type
    means "not recorded", not "ACTUAL": a window whose start or end type is absent is excluded
    with `status = "date_type_unavailable_in_weekly_projection"` and no day/month bounds, so a
    projected completion date never enters realized durations or accrual velocity. Missing or
    malformed dates and explicit `ESTIMATED`/`ANTICIPATED` types keep their own, more specific
    statuses (checked first). API v2 documents (`from_weekly_projection=False`) are unchanged.
    """
    s_parsed = parse_date_bounds(start_raw)
    e_parsed = parse_date_bounds(end_raw)
    s_type = _clean_date_type(start_type)
    e_type = _clean_date_type(end_type)

    base: dict[str, Any] = {
        "start_date": s_parsed["raw"],
        "start_date_type": s_type,
        "start_precision": s_parsed["precision"],
        "end_date": e_parsed["raw"],
        "end_date_type": e_type,
        "end_precision": e_parsed["precision"],
        "status": None,
        "eligible_for_realized_duration": False,
        "days": None,
        "min_days": None,
        "max_days": None,
        "midpoint_days": None,
        "min_months": None,
        "max_months": None,
        "midpoint_months": None,
    }

    if s_parsed["state"] == "missing" or e_parsed["state"] == "missing":
        base["status"] = "missing_start_or_end_date"
        return base

    if s_parsed["state"] == "malformed" or e_parsed["state"] == "malformed":
        base["status"] = "malformed_date"
        return base

    if s_type in ("ESTIMATED", "ANTICIPATED") or e_type in ("ESTIMATED", "ANTICIPATED"):
        base["status"] = "excluded_estimated_or_anticipated_date"
        return base

    # Weekly projection: `_clean_date_type` returns None for an absent/blank/"N/A" type. Only a
    # window with BOTH types recorded (and neither ESTIMATED/ANTICIPATED, handled above) can be
    # treated as realized; otherwise the absence is recorded with its reason, never as ACTUAL.
    if from_weekly_projection and (s_type is None or e_type is None):
        base["status"] = "date_type_unavailable_in_weekly_projection"
        return base

    s_lo: date = s_parsed["earliest"]
    s_hi: date = s_parsed["latest"]
    e_lo: date = e_parsed["earliest"]
    e_hi: date = e_parsed["latest"]

    raw_min_days = (e_lo - s_hi).days
    raw_max_days = (e_hi - s_lo).days

    if raw_max_days < 0:
        base["status"] = "inverted_dates_anomaly"
        base["raw_day_bounds"] = [raw_min_days, raw_max_days]
        return base

    if s_parsed["precision"] == "day" and e_parsed["precision"] == "day":
        exact_d = raw_min_days
        months = round(exact_d / DAYS_PER_MONTH, 6)
        base.update(
            status="exact_days",
            eligible_for_realized_duration=True,
            days=exact_d,
            min_days=exact_d,
            max_days=exact_d,
            midpoint_days=float(exact_d),
            min_months=months,
            max_months=months,
            midpoint_months=months,
        )
        return base

    # At least one endpoint has YYYY-MM or YYYY precision.
    # If s_hi >= e_lo (raw_min_days <= 0), the two partial calendar intervals overlap or touch at a
    # single boundary day, so elapsed time could be zero or negative within recorded precision.
    if raw_min_days <= 0:
        base.update(
            status="overlapping_or_zero_at_precision",
            eligible_for_realized_duration=False,
            days=None,
            min_days=0,
            max_days=raw_max_days,
            raw_min_days=raw_min_days,
            min_months=0.0,
            max_months=round(raw_max_days / DAYS_PER_MONTH, 6),
        )
        return base

    mid_days = (raw_min_days + raw_max_days) / 2.0
    base.update(
        status="bounded_partial_precision",
        eligible_for_realized_duration=True,
        days=None,
        min_days=raw_min_days,
        max_days=raw_max_days,
        midpoint_days=round(mid_days, 4),
        min_months=round(raw_min_days / DAYS_PER_MONTH, 6),
        max_months=round(raw_max_days / DAYS_PER_MONTH, 6),
        midpoint_months=round(mid_days / DAYS_PER_MONTH, 6),
    )
    return base


def normalize_enrollment(
    raw_count: Any,
    raw_type: Any,
    *,
    from_weekly_projection: bool = False,
) -> dict[str, Any]:
    """Normalize registry `enrollmentInfo` into strict `{"ACTUAL", "ESTIMATED", "MISSING_TYPE"}`.

    `ANTICIPATED` (legacy ClinicalTrials.gov term for target enrollment) maps to `"ESTIMATED"`
    with `raw_type` preserved. Weekly projections (`src/utils.py`) default missing `enrollment` to
    `0` and missing `enrollment_type` to `"N/A"`; when both defaults appear together on a weekly
    record without `protocolSection`, count is recorded as `None` rather than an observed zero.
    """
    cleaned_raw_type: str | None = None
    if isinstance(raw_type, str) and raw_type.strip():
        cleaned_raw_type = raw_type.strip()

    upper_type = (cleaned_raw_type or "").upper()
    if upper_type == "ACTUAL":
        norm_type = "ACTUAL"
    elif upper_type in ("ESTIMATED", "ANTICIPATED"):
        norm_type = "ESTIMATED"
    else:
        norm_type = "MISSING_TYPE"

    count: int | None = None
    count_reason = "ok"
    if raw_count is None or (isinstance(raw_count, str) and not raw_count.strip()):
        count = None
        count_reason = "missing_enrollment_count"
    elif from_weekly_projection and upper_type in ("", "N/A") and raw_count == 0:
        count = None
        count_reason = "weekly_projection_default_with_missing_type"
    elif isinstance(raw_count, bool):
        count = None
        count_reason = "malformed_enrollment_count"
    elif isinstance(raw_count, int):
        if raw_count >= 0:
            count = raw_count
        else:
            count = None
            count_reason = "negative_enrollment_count"
    elif is_num(raw_count):
        parsed = parse_num(raw_count)
        if parsed is not None and parsed >= 0 and float(parsed).is_integer():
            count = int(parsed)
        else:
            count = None
            count_reason = "non_integer_or_negative_enrollment_count"
    else:
        count = None
        count_reason = "malformed_enrollment_count"

    return {
        "count": count,
        "type": norm_type,
        "raw_type": cleaned_raw_type,
        "count_status": count_reason,
    }


def footprint_tier_for(n_listed_facilities: int) -> str:
    """Classify the number of listed study locations into the 5 canonical footprint tiers."""
    if n_listed_facilities <= 0:
        return "no_listed_sites"
    if n_listed_facilities == 1:
        return "single_site"
    if n_listed_facilities <= 10:
        return "2_to_10_sites"
    if n_listed_facilities <= 50:
        return "11_to_50_sites"
    return "51_plus_sites"


def compute_accrual_metrics(
    enrollment: dict[str, Any],
    primary_duration: dict[str, Any],
    study_duration: dict[str, Any],
    n_listed_facilities: int,
) -> dict[str, Any]:
    """Compute realized accrual velocity and participants-per-listed-facility.

    CRITICAL INVARIANT: Both metrics require `enrollment["type"] == "ACTUAL"` and
    `enrollment["count"] >= 0`. `ESTIMATED` and `MISSING_TYPE` enrollments are strictly excluded.

    For partial-precision dates with `[min_months, max_months]` (`0 < min_months <= max_months`):
      - `min_rate = count / max_months` (longest calendar window -> slowest accrual bound)
      - `max_rate = count / min_months` (shortest calendar window -> fastest accrual bound)
      - `actual_participants_per_month = count / midpoint_months` (rate over midpoint calendar
        duration, which always satisfies `min_rate <= actual_participants_per_month <= max_rate`
        without blowing up when adjacent month boundaries differ by 1 day).
    """
    enr_type = enrollment["type"]
    enr_count = enrollment["count"]

    out: dict[str, Any] = {
        "rate_basis": RATE_BASIS,
        "facility_basis": FACILITY_BASIS,
        "rate_status": None,
        "calendar_window_used": None,
        "actual_participants_per_month": None,
        "min_rate": None,
        "max_rate": None,
        "facility_metric_status": None,
        "actual_participants_per_listed_facility": None,
    }

    if enr_type != "ACTUAL":
        out["rate_status"] = "excluded_non_actual_enrollment"
        out["facility_metric_status"] = "excluded_non_actual_enrollment"
        return out

    if enr_count is None or enr_count < 0:
        out["rate_status"] = "missing_enrollment_count"
        out["facility_metric_status"] = "missing_enrollment_count"
        return out

    # Facility proxy metric: ACTUAL enrollment / listed facilities when >= 1 facility is listed.
    if n_listed_facilities >= 1:
        out["facility_metric_status"] = "computed"
        out["actual_participants_per_listed_facility"] = round(enr_count / n_listed_facilities, 6)
    else:
        out["facility_metric_status"] = "no_listed_facilities"

    # Calendar duration window selection: prefer start -> primaryCompletion; fall back to
    # start -> studyCompletion ONLY when primaryCompletionDateStruct was absent (never when
    # primaryCompletion was ESTIMATED/ANTICIPATED, overlapping, or inverted).
    chosen_dur = primary_duration
    window_name = "start_to_primary_completion"
    if (
        primary_duration["status"] == "missing_start_or_end_date"
        and primary_duration["end_date"] is None
        and study_duration["eligible_for_realized_duration"]
    ):
        chosen_dur = study_duration
        window_name = "start_to_study_completion"

    if not chosen_dur["eligible_for_realized_duration"]:
        out["rate_status"] = chosen_dur["status"]
        return out

    min_days = chosen_dur["min_days"]
    if min_days is None or min_days <= 0:
        out["rate_status"] = "zero_duration_window"
        return out

    min_months: float = chosen_dur["min_months"]
    max_months: float = chosen_dur["max_months"]
    mid_months: float = chosen_dur["midpoint_months"]

    out["rate_status"] = "computed"
    out["calendar_window_used"] = window_name
    out["min_rate"] = round(enr_count / max_months, 6)
    out["max_rate"] = round(enr_count / min_months, 6)
    out["actual_participants_per_month"] = round(enr_count / mid_months, 6)
    return out


def extract_period1_retention(study: dict[str, Any]) -> dict[str, Any]:
    """Extract Period 1 participant flow retention and verbatim `dropWithdraws` reason tallies.

    Follows Rule T (`outcomes/results_structure.py`): when multiple flow groups are posted, any
    group titled `"Total"` (case-insensitive) is excluded from sums so arm counts are not doubled;
    when only a single group exists, it is retained regardless of title.
    """
    is_raw = "protocolSection" in study or "resultsSection" in study or "hasResults" in study
    has_results_flag = study.get("hasResults")
    results_sec = study.get("resultsSection")

    base: dict[str, Any] = {
        "flow_status": None,
        "n_periods": None,
        "period1_title": None,
        "has_pre_assignment_details": None,
        "period1_started": None,
        "period1_completed": None,
        "period1_completion_rate": None,
        "completion_rate_status": None,
        "drop_withdraw_reasons": None,
    }

    if not is_raw:
        base["flow_status"] = (
            "flow_module_not_in_input_projection" if study.get("results_date") else "no_posted_results"
        )
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    if has_results_flag is False and results_sec is None:
        base["flow_status"] = "no_posted_results"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    if not isinstance(results_sec, dict):
        base["flow_status"] = "no_posted_results" if not has_results_flag else "no_participant_flow_module"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    pf = results_sec.get("participantFlowModule")
    if pf is None or pf == {}:
        base["flow_status"] = "no_participant_flow_module"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    if not isinstance(pf, dict):
        base["flow_status"] = "malformed_participant_flow"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    pre_assign = pf.get("preAssignmentDetails")
    base["has_pre_assignment_details"] = bool(isinstance(pre_assign, str) and pre_assign.strip())

    periods = pf.get("periods")
    raw_groups = pf.get("groups")
    if (periods is not None and not isinstance(periods, list)) or (
        raw_groups is not None and not isinstance(raw_groups, list)
    ):
        base["flow_status"] = "malformed_participant_flow"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    period_list = periods or []
    base["n_periods"] = len(period_list)
    if not period_list:
        base["flow_status"] = "no_periods"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    # Every flow group must be an object with a non-empty string id. Dropping a malformed entry
    # and summing the rest would publish STARTED / COMPLETED for only some arms under
    # `flow_status = "ok"`, so one malformed group withholds the whole flow instead.
    group_list = list(raw_groups or [])
    if any(
        not isinstance(g, dict)
        or not isinstance(g.get("id"), str)
        or not g["id"].strip()
        or not isinstance(g.get("title"), (str, type(None)))  # the "Total" test strips it
        for g in group_list
    ) or len({g["id"] for g in group_list}) != len(group_list):
        # (a repeated group id would read its milestone count once per occurrence and double it)
        base["flow_status"] = "malformed_participant_flow"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base
    if len(group_list) > 1:
        arm_groups = [g for g in group_list if (g.get("title") or "").strip().casefold() != "total"]
    else:
        arm_groups = list(group_list)

    arm_ids = [g["id"] for g in arm_groups]
    if not arm_ids:
        base["flow_status"] = "no_flow_groups"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    p0 = period_list[0]
    if not isinstance(p0, dict):
        base["flow_status"] = "malformed_participant_flow"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    p0_title = p0.get("title")
    base["period1_title"] = p0_title.strip() if isinstance(p0_title, str) and p0_title.strip() else None

    # Period 1's lists must be lists when present: iterating a number raised, and one malformed
    # record stopped the whole build. A malformed list withholds the flow like a malformed group.
    if any(
        p0.get(part) is not None and not isinstance(p0.get(part), list)
        for part in ("dropWithdraws", "milestones")
    ):
        base["flow_status"] = "malformed_participant_flow"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    # Extract verbatim dropWithdraws reason tallies across non-Total flow groups in Period 1.
    # A reason's tally is the sum over every retained arm, so it is published only when each
    # `dropWithdraws` entry for that reason gives exactly one valid count (>= 0) for EVERY retained
    # arm. An arm whose count is missing, "NA", negative or repeated makes the tally `None`: a sum
    # over some arms would read as the complete total.
    arm_id_set = set(arm_ids)
    dw_tallies: dict[str, int | float | None] = {}
    dw_complete: dict[str, bool] = {}
    for dw in p0.get("dropWithdraws") or []:
        if not isinstance(dw, dict):
            # A malformed entry names no reason and no counts. Dropping it would make the published
            # reasons look complete, so it is kept as an unknown reason with a withheld tally.
            dw_tallies["UNSPECIFIED_REASON"] = None
            dw_complete["UNSPECIFIED_REASON"] = False
            continue
        raw_label = dw.get("type")
        label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else "UNSPECIFIED_REASON"
        dw_tallies.setdefault(label, 0)
        dw_complete.setdefault(label, True)
        arm_counts: dict[str, float] = {}
        reasons = dw.get("reasons")
        # A `reasons` that is not a list gives no arm counts, so the tally is withheld (None).
        entry_ok = reasons is None or isinstance(reasons, list)
        for entry in reasons if isinstance(reasons, list) else []:
            gid = entry.get("groupId") if isinstance(entry, dict) else None
            # A non-string id (a list cannot even be looked up in a set) counts for no arm.
            if not isinstance(gid, str) or gid not in arm_id_set:
                continue
            val = parse_num(entry.get("numSubjects"))
            if val is None or val < 0 or gid in arm_counts:
                entry_ok = False
                continue
            arm_counts[gid] = val
        if not entry_ok or set(arm_counts) != arm_id_set:
            dw_complete[label] = False
        elif dw_complete[label]:
            prev = dw_tallies[label] or 0
            dw_tallies[label] = plain_number(float(prev) + sum(arm_counts.values()))
    for label, complete in dw_complete.items():
        if not complete:
            dw_tallies[label] = None
    base["drop_withdraw_reasons"] = dict(sorted(dw_tallies.items()))

    milestones: dict[str, dict[str, Any]] = {}
    for ms in p0.get("milestones") or []:
        if not isinstance(ms, dict):
            # It could be a second STARTED or COMPLETED (see below), so no single count can be trusted.
            base["flow_status"] = "malformed_participant_flow"
            base["completion_rate_status"] = "withheld_missing_started_or_completed"
            return base
        mtype = ms.get("type")
        if not (isinstance(mtype, str) and mtype.strip()):
            # A milestone without a type could be a second STARTED or COMPLETED too, so the retention
            # rate is withheld rather than computed beside it. All 8,988 milestones of the cached records
            # with a participant flow carry one.
            base["flow_status"] = "malformed_participant_flow"
            base["completion_rate_status"] = "withheld_missing_started_or_completed"
            return base
        if mtype in ("STARTED", "COMPLETED") and mtype in milestones:
            # A second STARTED or COMPLETED milestone in Period 1 leaves no single count; using
            # either occurrence would publish an arbitrary one.
            base["flow_status"] = "malformed_participant_flow"
            base["completion_rate_status"] = "withheld_missing_started_or_completed"
            return base
        if mtype in ("STARTED", "COMPLETED"):
            achievements = ms.get("achievements")
            if achievements is not None and not isinstance(achievements, list):
                base["flow_status"] = "malformed_participant_flow"
                base["completion_rate_status"] = "withheld_missing_started_or_completed"
                return base
            ach: dict[str, Any] = {}
            for a in achievements or []:
                if not isinstance(a, dict) or not isinstance(a.get("groupId"), str):
                    # It could be a retained arm's second count (see below); skipping it would keep
                    # the other one as if it were the only count.
                    base["flow_status"] = "malformed_participant_flow"
                    base["completion_rate_status"] = "withheld_missing_started_or_completed"
                    return base
                if a["groupId"] in ach and a["groupId"] in arm_id_set:
                    # A retained arm counted twice in one milestone has no single count; keeping
                    # either value would publish an arbitrary STARTED / COMPLETED.
                    base["flow_status"] = "malformed_participant_flow"
                    base["completion_rate_status"] = "withheld_missing_started_or_completed"
                    return base
                ach[a["groupId"]] = a.get("numSubjects")
            milestones[mtype] = ach

    if "STARTED" not in milestones:
        base["flow_status"] = "no_started_milestone_in_period1"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    started_vals = [parse_num(milestones["STARTED"].get(gid)) for gid in arm_ids]
    if any(v is None or v < 0 for v in started_vals):
        base["flow_status"] = "missing_or_non_numeric_group_count"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    started_sum = plain_number(sum(v for v in started_vals if v is not None))
    base["period1_started"] = started_sum

    if "COMPLETED" not in milestones:
        base["flow_status"] = "no_completed_milestone_in_period1"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    completed_vals = [parse_num(milestones["COMPLETED"].get(gid)) for gid in arm_ids]
    if any(v is None or v < 0 for v in completed_vals):
        base["flow_status"] = "missing_or_non_numeric_group_count"
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
        return base

    completed_sum = plain_number(sum(v for v in completed_vals if v is not None))
    base["period1_completed"] = completed_sum
    base["flow_status"] = "ok"

    if started_sum is None or completed_sum is None:
        base["completion_rate_status"] = "withheld_missing_started_or_completed"
    elif started_sum == 0:
        base["completion_rate_status"] = "withheld_zero_started"
    elif completed_sum > started_sum:
        base["completion_rate_status"] = "withheld_completed_exceeds_started_anomaly"
    else:
        base["completion_rate_status"] = "computed"
        base["period1_completion_rate"] = round(float(completed_sum) / float(started_sum), 6)

    return base


def normalize_phase(raw_phases: list[str] | str | None) -> str:
    """Standardize CT.gov phase list or comma-joined weekly phase string into a canonical stratum.

    Missing or empty phase is `"PHASE_NOT_STATED"` — never coerced to `"NA"` (which is CT.gov's
    explicit Not Applicable enum for device/behavioral studies). Multi-phase studies such as
    `["PHASE2", "PHASE3"]` become `"PHASE2/PHASE3"`, kept distinct from `"PHASE3"`.
    """
    tokens: list[str] = []
    if isinstance(raw_phases, list):
        tokens = [str(p).strip().upper() for p in raw_phases if str(p).strip()]
    elif isinstance(raw_phases, str):
        tokens = [p.strip().upper() for p in re.split(r"[,/]", raw_phases) if p.strip()]
    if not tokens:
        return "PHASE_NOT_STATED"
    deduped = sorted(
        set(tokens),
        key=lambda t: (PHASE_ORDER.index(t) if t in PHASE_ORDER else len(PHASE_ORDER), t),
    )
    return "/".join(deduped)


def extract_trial_metrics(study: dict[str, Any]) -> dict[str, Any]:
    """Extract pure deterministic operational feasibility metrics from one study record."""
    has_protocol = isinstance(study.get("protocolSection"), dict)
    if has_protocol:
        ps = study["protocolSection"]
        id_mod = ps.get("identificationModule") or {}
        status_mod = ps.get("statusModule") or {}
        design_mod = ps.get("designModule") or {}
        sponsor_mod = ps.get("sponsorCollaboratorsModule") or {}
        cond_mod = ps.get("conditionsModule") or {}
        loc_mod = ps.get("contactsLocationsModule") or {}

        nct_id = id_mod.get("nctId")
        brief_title = id_mod.get("briefTitle")
        study_type = design_mod.get("studyType")
        phase_stratum = normalize_phase(design_mod.get("phases"))
        overall_status = status_mod.get("overallStatus")
        why_stopped = status_mod.get("whyStopped")

        start_struct = status_mod.get("startDateStruct") or {}
        pcd_struct = status_mod.get("primaryCompletionDateStruct") or {}
        cd_struct = status_mod.get("completionDateStruct") or {}

        primary_duration = compute_duration(
            start_struct.get("date"),
            start_struct.get("type"),
            pcd_struct.get("date"),
            pcd_struct.get("type"),
        )
        study_duration = compute_duration(
            start_struct.get("date"),
            start_struct.get("type"),
            cd_struct.get("date"),
            cd_struct.get("type"),
        )

        enr_info = design_mod.get("enrollmentInfo") or {}
        enrollment = normalize_enrollment(
            enr_info.get("count"),
            enr_info.get("type"),
            from_weekly_projection=False,
        )

        raw_locations = [loc for loc in (loc_mod.get("locations") or []) if isinstance(loc, dict)]
        n_listed_facilities = len(raw_locations)
        countries = sorted(
            {
                loc["country"].strip()
                for loc in raw_locations
                if isinstance(loc.get("country"), str) and loc["country"].strip()
            }
        )

        lead = sponsor_mod.get("leadSponsor") or {}
        lead_sponsor_name = lead.get("name")
        lead_sponsor_class = lead.get("class")
        collaborators = [
            {"name": c.get("name") or "", "class": c.get("class") or ""}
            for c in (sponsor_mod.get("collaborators") or [])
            if isinstance(c, dict)
        ]
        conditions = [c for c in (cond_mod.get("conditions") or []) if isinstance(c, str) and c.strip()]
    else:
        nct_id = study.get("nct_id") or None
        brief_title = study.get("brief_title") or None
        study_type = study.get("study_type") or None
        phase_stratum = normalize_phase(study.get("phase"))
        overall_status = study.get("status") or None
        why_stopped = study.get("why_stopped")

        # The weekly projection (`src/utils.py`) carries no `*_date_type` keys, so both windows
        # are recorded as `date_type_unavailable_in_weekly_projection` unless a type is present.
        primary_duration = compute_duration(
            study.get("start_date"),
            study.get("start_date_type"),
            study.get("primary_completion_date"),
            study.get("primary_completion_date_type"),
            from_weekly_projection=True,
        )
        study_duration = compute_duration(
            study.get("start_date"),
            study.get("start_date_type"),
            study.get("completion_date"),
            study.get("completion_date_type"),
            from_weekly_projection=True,
        )

        enrollment = normalize_enrollment(
            study.get("enrollment"),
            study.get("enrollment_type"),
            from_weekly_projection=True,
        )

        sites = [loc for loc in (study.get("study_sites") or []) if isinstance(loc, dict)]
        n_listed_facilities = len(sites)
        countries = sorted(
            {
                c["country"].strip()
                for c in (study.get("countries") or sites)
                if isinstance(c, dict) and isinstance(c.get("country"), str) and c["country"].strip()
            }
        )

        lead_sponsor_name = study.get("lead_sponsor_name")
        lead_sponsor_class = study.get("sponsor_class")
        collaborators = [
            {"name": c.get("name") or "", "class": c.get("class") or ""}
            for c in (study.get("collaborators") or [])
            if isinstance(c, dict)
        ]
        conditions = [c for c in (study.get("conditions") or []) if isinstance(c, str) and c.strip()]

    tier = footprint_tier_for(n_listed_facilities)
    accrual = compute_accrual_metrics(enrollment, primary_duration, study_duration, n_listed_facilities)
    retention = extract_period1_retention(study)

    return {
        "nct_id": nct_id.strip() if isinstance(nct_id, str) and nct_id.strip() else None,
        "brief_title": brief_title.strip() if isinstance(brief_title, str) and brief_title.strip() else None,
        "study_type": study_type.strip() if isinstance(study_type, str) and study_type.strip() else None,
        "phase": phase_stratum,
        "overall_status": overall_status.strip() if isinstance(overall_status, str) and overall_status.strip() else None,
        "why_stopped": why_stopped if isinstance(why_stopped, str) and why_stopped.strip() else None,
        "conditions": conditions,
        "lead_sponsor_name": lead_sponsor_name if isinstance(lead_sponsor_name, str) else None,
        "lead_sponsor_class": (
            lead_sponsor_class.strip()
            if isinstance(lead_sponsor_class, str) and lead_sponsor_class.strip() and lead_sponsor_class.strip() != "Unknown"
            else ("UNKNOWN_EXTRACTOR_DEFAULT" if lead_sponsor_class == "Unknown" else "MISSING_CLASS")
        ),
        "raw_sponsor_class": lead_sponsor_class if isinstance(lead_sponsor_class, str) else None,
        "collaborators": collaborators,
        "facilities": {
            "n_listed_facilities": n_listed_facilities,
            "footprint_tier": tier,
            "n_countries": len(countries),
            "countries": countries,
            "facility_basis": FACILITY_BASIS,
        },
        "enrollment": enrollment,
        "durations": {
            "primary_completion": primary_duration,
            "study_completion": study_duration,
        },
        "accrual": accrual,
        "retention": retention,
    }
