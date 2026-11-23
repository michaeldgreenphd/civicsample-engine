"""
age/representation.py — exact age cutoff bounds, eligibility-vs-enrolled gap
auditing, and ACS B01001 comparison for baseline age distributions.

CORE METHODOLOGICAL PRINCIPLES
  1. No uniform interpolation inside a trial's straddling bracket.
     When a trial's reported age bracket straddles target_lo or target_hi
     (for example, "50 to 70 years" against the >=65 cutoff), the true count in
     [65, inf) can sit anywhere from 0 to that bracket's full count. The engine
     therefore computes exact [min_count, max_count] and [floor_pct, ceiling_pct]
     bounds on BOTH Denominator A (all participants in the table = n_classified +
     n_unknown) and Denominator B (of_classified = n_classified, carrying
     classified_bounds whenever a non-zero bracket straddles the cutpoint).
  2. Eligibility vs enrolled representation gaps.
     Audits nominal protocol eligibility (census.trials.eligible_ages) against
     observed baseline enrollment for older adults (>=65 and >=75) and pediatric
     participants (<18), plus out-of-bounds enrollment violations.
  3. Reference-population comparison against ACS 5-year table B01001.
     Uses census.acs.Level, census.benchmark.band_weights, and the Census
     Bureau's 90% MOE formulas (_sum, _share) restricted to [eligible_lo,
     eligible_hi) to compare expected age-band shares against each trial's exact
     [floor_pct, ceiling_pct] interval, setting direction_settled = True iff
     (ceiling_pct < expected_pct or floor_pct > expected_pct).
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional, Sequence, Union

import numpy as np

from age import parser as ap
from census import acs, benchmark, trials

STANDARD_TARGET_BANDS: dict[str, tuple[float, Optional[float]]] = {
    "pediatric_under_18": (0.0, 18.0),
    "working_age_18_64": (18.0, 65.0),
    "older_adults_65_plus": (65.0, None),
    "older_adults_75_plus": (75.0, None),
}

# Small floating-point tolerance for exact unit conversions (e.g. 12 * (1/12) vs 1.0,
# 28/365 boundary alignment, 52 * (1/52) vs 1.0).
UNIT_CONVERSION_TOL_YEARS: float = 1e-6
_CUTOFF_EPS: float = 1e-9
_SETTLEMENT_TIE_EPS_PP: float = 1e-12


# ---------------------------------------------------------------------------
# 1. Exact cutoff bounds on Denominator A & Denominator B
# ---------------------------------------------------------------------------


def _coerce_bracket(item: Union[ap.ParsedBracket, dict[str, Any]]) -> ap.ParsedBracket:
    if isinstance(item, ap.ParsedBracket):
        if (
            isinstance(item.lo_years, bool)
            or isinstance(item.hi_years, bool)
            or isinstance(item.count, bool)
        ):
            raise TypeError("boolean values are not valid numeric bracket bounds or counts")
        return item
    if isinstance(item, dict):
        lo_raw = item.get("lo_years")
        hi_raw = item.get("hi_years")
        cnt_raw = item.get("count")
        if (
            lo_raw is None
            or isinstance(lo_raw, bool)
            or isinstance(hi_raw, bool)
            or isinstance(cnt_raw, bool)
        ):
            raise TypeError("boolean or missing lo_years values are not valid in bracket dict")
        lo = float(lo_raw)
        hi = None if hi_raw is None else float(hi_raw)
        cnt = 0.0 if cnt_raw is None else float(cnt_raw)
        if not math.isfinite(lo) or lo < 0.0 or (hi is not None and (not math.isfinite(hi) or hi <= lo)):
            raise ValueError(f"invalid bracket bounds: [{lo!r}, {hi!r})")
        lbl = str(item.get("label") or f"[{lo}, {hi})")
        basis = str(item.get("bound_basis") or "from_dict")
        inferred = bool(item.get("unit_inferred", False))
        return ap.ParsedBracket(
            label=lbl,
            lo_years=lo,
            hi_years=hi,
            count=cnt,
            bound_basis=basis,
            unit_inferred=inferred,
        )
    raise TypeError(f"unsupported bracket item type: {type(item).__name__}")


def _classify_bracket_vs_target(
    bracket: ap.ParsedBracket,
    target_lo: float,
    target_hi: Optional[float],
) -> str:
    """
    Classify bracket [lo_years, hi_years) (or [lo_years, hi_years] when bound_basis
    records an explicit decimal '<=' upper bound) relative to [target_lo, target_hi):
      - "inside"     wholly contained in [target_lo, target_hi)
      - "outside"    disjoint from [target_lo, target_hi)
      - "straddling" intersects both [target_lo, target_hi) and its complement
    """
    lo_years = bracket.lo_years
    hi_years = bracket.hi_years
    hi_incl = (
        "inclusive_decimal_upper" in bracket.bound_basis
        or "_le_explicit_dec_" in bracket.bound_basis
    )

    # Disjoint below target_lo
    if hi_years is not None:
        if hi_incl:
            if hi_years < target_lo - _CUTOFF_EPS:
                return "outside"
        else:
            if hi_years <= target_lo + _CUTOFF_EPS:
                return "outside"
    # Disjoint at or above target_hi
    if target_hi is not None and lo_years >= target_hi - _CUTOFF_EPS:
        return "outside"

    # Check containment in [target_lo, target_hi)
    lo_inside = lo_years >= target_lo - _CUTOFF_EPS
    if target_hi is None:
        hi_inside = True
    elif hi_years is None:
        hi_inside = False
    elif hi_incl:
        hi_inside = hi_years < target_hi - _CUTOFF_EPS
    else:
        hi_inside = hi_years <= target_hi + _CUTOFF_EPS

    if lo_inside and hi_inside:
        return "inside"
    return "straddling"


def age_range_bounds(
    parsed_brackets: Sequence[Union[ap.ParsedBracket, dict[str, Any]]],
    n_unknown: Optional[float],
    target_lo: float,
    target_hi: Optional[float] = None,
) -> dict[str, Any]:
    """
    Compute exact participant count and percentage bounds for [target_lo, target_hi)
    without uniform interpolation inside straddling trial brackets.

    Denominator A = n_classified + n_unknown:
      - wholly inside bracket: adds count to min_count and max_count
      - wholly outside bracket: adds 0 to min_count and max_count
      - straddling bracket: adds 0 to min_count and full count to max_count
      - n_unknown: adds 0 to min_count and n_unknown to max_count

    Denominator B (of_classified) = n_classified:
      - excludes n_unknown, retaining exact classified_bounds [min_count, max_count]
        and [floor_pct, ceiling_pct] whenever a non-zero bracket straddles target_lo
        or target_hi.
    """
    if isinstance(target_lo, bool) or not isinstance(target_lo, (int, float)) or not math.isfinite(target_lo) or target_lo < 0.0:
        raise ValueError(f"target_lo must be finite numeric and >= 0, got {target_lo!r}")
    if target_hi is not None and (
        isinstance(target_hi, bool)
        or not isinstance(target_hi, (int, float))
        or not math.isfinite(target_hi)
        or target_hi <= target_lo
    ):
        raise ValueError(f"target_hi must be numeric > target_lo ({target_lo!r}), got {target_hi!r}")

    if isinstance(n_unknown, bool) or (n_unknown is not None and not isinstance(n_unknown, (int, float))):
        raise ValueError(f"n_unknown must be numeric or None, got {n_unknown!r}")
    unk = 0.0 if n_unknown is None else float(n_unknown)
    if not math.isfinite(unk) or unk < 0.0:
        raise ValueError(f"n_unknown must be finite and >= 0, got {n_unknown!r}")

    brackets = [_coerce_bracket(b) for b in parsed_brackets]
    for b in brackets:
        if not math.isfinite(b.count) or b.count < 0.0:
            raise ValueError(f"bracket count must be finite and >= 0, got {b.count!r}")

    definite_in = 0.0
    definite_out = 0.0
    straddling_count = 0.0
    straddling_labels: list[str] = []
    has_zero_count_straddle = False

    for b in brackets:
        rel = _classify_bracket_vs_target(b, target_lo, target_hi)
        if rel == "inside":
            definite_in += b.count
        elif rel == "outside":
            definite_out += b.count
        else:
            straddling_count += b.count
            if b.count > 0.0:
                straddling_labels.append(b.label)
            else:
                has_zero_count_straddle = True

    n_classified = definite_in + definite_out + straddling_count
    n_denom_a = n_classified + unk

    min_count_a = definite_in
    max_count_a = definite_in + straddling_count + unk
    floor_pct_a: Optional[float] = (100.0 * min_count_a / n_denom_a) if n_denom_a > 0.0 else None
    ceiling_pct_a: Optional[float] = (100.0 * max_count_a / n_denom_a) if n_denom_a > 0.0 else None

    has_straddling = len(straddling_labels) > 0
    cutpoint_exact = not has_straddling

    min_count_b = definite_in
    max_count_b = definite_in + straddling_count
    floor_pct_b: Optional[float] = (100.0 * min_count_b / n_classified) if n_classified > 0.0 else None
    ceiling_pct_b: Optional[float] = (100.0 * max_count_b / n_classified) if n_classified > 0.0 else None
    point_pct_b: Optional[float] = floor_pct_b if (cutpoint_exact and n_classified > 0.0) else None

    return {
        "target_lo": target_lo,
        "target_hi": target_hi,
        "n_classified": n_classified,
        "n_unknown": unk,
        "n_denominator_a": n_denom_a,
        "min_count": min_count_a,
        "max_count": max_count_a,
        "floor_pct": floor_pct_a,
        "ceiling_pct": ceiling_pct_a,
        "has_straddling_bracket": has_straddling,
        "has_zero_count_straddling_bracket": has_zero_count_straddle,
        "cutpoint_exact": cutpoint_exact,
        "straddling_count": straddling_count,
        "straddling_brackets": straddling_labels,
        "of_classified": {
            "n_classified": n_classified,
            "point_pct": point_pct_b,
            "cutpoint_exact": cutpoint_exact,
            "classified_bounds": {
                "min_count": min_count_b,
                "max_count": max_count_b,
                "floor_pct": floor_pct_b,
                "ceiling_pct": ceiling_pct_b,
            },
        },
    }


def standard_age_band_bounds(
    parsed_age: Union[ap.ParsedAgeTrial, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute age_range_bounds across all four STANDARD_TARGET_BANDS."""
    brackets, unk, usable = _extract_categorical_from_parsed(parsed_age)
    if not usable:
        return {}
    return {
        band_name: age_range_bounds(brackets, unk, lo, hi)
        for band_name, (lo, hi) in STANDARD_TARGET_BANDS.items()
    }


# ---------------------------------------------------------------------------
# 2. Eligibility vs enrolled baseline age audit
# ---------------------------------------------------------------------------


def _extract_categorical_from_parsed(
    parsed_age: Union[ap.ParsedAgeTrial, dict[str, Any]],
) -> tuple[list[Union[ap.ParsedBracket, dict[str, Any]]], Optional[float], bool]:
    """
    Extract (brackets, n_unknown, usable_participant_categorical) from either
    ParsedAgeTrial or a flat row dict. Rejects non-participant units and
    invalid/overlapping categorical tables.
    """
    if isinstance(parsed_age, ap.ParsedAgeTrial):
        if not parsed_age.parse_ok or parsed_age.is_participant_count is not True:
            return [], None, False
        n_tot = parsed_age.n_total_parsed
        if n_tot is None or n_tot <= 0.0:
            return [], None, False
        return list(parsed_age.parsed_brackets), parsed_age.n_unknown, True

    if isinstance(parsed_age, dict):
        if parsed_age.get("parse_ok") is False or parsed_age.get("is_participant_count") is not True:
            return [], None, False
        n_tot = parsed_age.get("n_total_parsed")
        unk = parsed_age.get("n_unknown")
        if isinstance(n_tot, bool) or isinstance(unk, bool):
            raise TypeError("boolean values are not valid numeric counts in age row dict")
        if n_tot is None or float(n_tot) <= 0.0:
            return [], None, False
        raw_b = parsed_age.get("parsed_brackets") or []
        return list(raw_b), (None if unk is None else float(unk)), True

    return [], None, False


def _finite_nonbool_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _extract_continuous_stats_from_parsed(
    parsed_age: Union[ap.ParsedAgeTrial, dict[str, Any]],
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Return (cont_min, cont_max, guaranteed_min_at_most, guaranteed_max_at_least)
    using Pigeonhole bounds over mean/median/geometric_mean/IQR when valid continuous
    stats exist.
    """
    if isinstance(parsed_age, ap.ParsedAgeTrial):
        if not parsed_age.parse_ok or not parsed_age.has_reported_continuous:
            return None, None, None, None
        mn = _finite_nonbool_float(parsed_age.min_years)
        mx = _finite_nonbool_float(parsed_age.max_years)
        low_cands = [
            x
            for x in (
                mn,
                _finite_nonbool_float(parsed_age.mean_years),
                _finite_nonbool_float(parsed_age.median_years),
                _finite_nonbool_float(parsed_age.geometric_mean_years),
                _finite_nonbool_float(parsed_age.iqr_low_years),
            )
            if x is not None
        ]
        high_cands = [
            x
            for x in (
                mx,
                _finite_nonbool_float(parsed_age.mean_years),
                _finite_nonbool_float(parsed_age.median_years),
                _finite_nonbool_float(parsed_age.geometric_mean_years),
                _finite_nonbool_float(parsed_age.iqr_high_years),
            )
            if x is not None
        ]
        return (
            mn,
            mx,
            (min(low_cands) if low_cands else None),
            (max(high_cands) if high_cands else None),
        )

    if isinstance(parsed_age, dict):
        if parsed_age.get("parse_ok") is False or not parsed_age.get("has_reported_continuous"):
            return None, None, None, None
        mn_d = _finite_nonbool_float(parsed_age.get("min_years"))
        mx_d = _finite_nonbool_float(parsed_age.get("max_years"))
        low_c = [
            val
            for val in (
                _finite_nonbool_float(parsed_age.get(k))
                for k in ("min_years", "mean_years", "median_years", "geometric_mean_years", "iqr_low_years")
            )
            if val is not None
        ]
        high_c = [
            val
            for val in (
                _finite_nonbool_float(parsed_age.get(k))
                for k in ("max_years", "mean_years", "median_years", "geometric_mean_years", "iqr_high_years")
            )
            if val is not None
        ]
        return (
            mn_d,
            mx_d,
            (min(low_c) if low_c else None),
            (max(high_c) if high_c else None),
        )

    return None, None, None, None


def _extract_eligibility_record(study_or_row: dict[str, Any]) -> dict[str, Any]:
    """Normalize either an API v2 study dict or an engine/table row to {'min_age', 'max_age', 'std_ages'}."""
    elig = ((study_or_row.get("protocolSection") or {}).get("eligibilityModule")) or {}
    min_age = elig.get("minimumAge")
    if min_age is None and "min_age" in study_or_row:
        min_age = study_or_row.get("min_age")
    max_age = elig.get("maximumAge")
    if max_age is None and "max_age" in study_or_row:
        max_age = study_or_row.get("max_age")
    std_raw = elig.get("stdAges")
    if std_raw is None and "std_ages" in study_or_row:
        std_raw = study_or_row.get("std_ages")
    return {
        "min_age": min_age,
        "max_age": max_age,
        "std_ages": std_raw or [],
    }


_SUBYEAR_MAX_AGE_RX = re.compile(r"^\s*(\d+)\s*([A-Za-z]+?)s?\s*$")


def _effective_exclusive_upper_bound(
    max_age_text: Any,
    eligible_hi: Optional[float],
) -> Optional[float]:
    """
    census.trials.eligible_ages adds +1.0 for 'Years' ('65 Years' -> 66.0) because
    ClinicalTrials.gov maximumAge is inclusive of that completed unit, while leaving
    sub-year strings ('23 Months') at 23/12. Grant completed-unit inclusivity on
    integer sub-year maximumAge strings ('23 Months' -> 24/12) when checking
    enrolled_outside_eligibility_bounds.
    """
    if eligible_hi is None:
        return None
    s = "" if max_age_text is None else str(max_age_text).strip()
    m = _SUBYEAR_MAX_AGE_RX.match(s)
    if m:
        unit_base = m.group(2).lower()
        if unit_base != "year" and unit_base in ap.UNIT_YEARS:
            return (float(m.group(1)) + 1.0) * ap.UNIT_YEARS[unit_base]
    return eligible_hi


def _ensure_parsed_age(
    study_or_row: dict[str, Any],
    parsed_age: Optional[Union[ap.ParsedAgeTrial, dict[str, Any]]],
) -> Union[ap.ParsedAgeTrial, dict[str, Any]]:
    if parsed_age is not None:
        return parsed_age
    if "age_report_status" in study_or_row or "parsed_brackets" in study_or_row:
        return study_or_row
    module = ((study_or_row.get("resultsSection") or {}).get("baselineCharacteristicsModule")) or {}
    enr = (
        ((study_or_row.get("protocolSection") or {}).get("designModule") or {})
        .get("enrollmentInfo")
        or {}
    ).get("count")
    return ap.parse_trial(
        module.get("measures"),
        enrollment=enr,
        baseline_groups=module.get("groups") if isinstance(module.get("groups"), list) else None,
        baseline_denoms=module.get("denoms") if isinstance(module.get("denoms"), list) else None,
    )


def audit_eligibility_vs_enrolled(
    study_or_row: dict[str, Any],
    parsed_age: Optional[Union[ap.ParsedAgeTrial, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Audit nominal protocol age eligibility (census.trials.eligible_ages) against
    actual enrolled participant baseline age.

    Computes:
      - nominally_open_65_plus, zero_enrolled_65_plus, older_adult_representation_gap
        (evidence basis: "categorical_zero_in_65_plus" or "continuous_max_below_65")
      - nominally_open_75_plus, zero_enrolled_75_plus, older_adult_75_plus_representation_gap
      - nominally_open_pediatric, zero_enrolled_pediatric, pediatric_representation_gap
      - enrolled_outside_eligibility_bounds (with UNIT_CONVERSION_TOL_YEARS tolerance)
    """
    if not isinstance(study_or_row, dict):
        return {
            "eligibility_parse_ok": False,
            "eligibility_error": "study_or_row_not_a_dict",
            "eligible_lo": None,
            "eligible_hi": None,
            "nominally_open_65_plus": None,
            "zero_enrolled_65_plus": False,
            "older_adult_representation_gap": False,
            "older_adult_gap_basis": None,
            "older_adult_gap_evidence_bases": [],
            "nominally_open_75_plus": None,
            "zero_enrolled_75_plus": False,
            "older_adult_75_plus_representation_gap": False,
            "older_adult_75_plus_gap_basis": None,
            "older_adult_75_plus_gap_evidence_bases": [],
            "nominally_open_pediatric": None,
            "zero_enrolled_pediatric": False,
            "pediatric_representation_gap": False,
            "pediatric_gap_basis": None,
            "pediatric_gap_evidence_bases": [],
            "enrolled_outside_eligibility_bounds": False,
            "outside_eligibility_reasons": [],
        }

    elig_rec = _extract_eligibility_record(study_or_row)
    p_age = _ensure_parsed_age(study_or_row, parsed_age)

    try:
        eligible_lo, eligible_hi = trials.eligible_ages(elig_rec)
    except ValueError as exc:
        return {
            "eligibility_parse_ok": False,
            "eligibility_error": str(exc),
            "eligible_lo": None,
            "eligible_hi": None,
            "nominally_open_65_plus": None,
            "zero_enrolled_65_plus": False,
            "older_adult_representation_gap": False,
            "older_adult_gap_basis": None,
            "older_adult_gap_evidence_bases": [],
            "nominally_open_75_plus": None,
            "zero_enrolled_75_plus": False,
            "older_adult_75_plus_representation_gap": False,
            "older_adult_75_plus_gap_basis": None,
            "older_adult_75_plus_gap_evidence_bases": [],
            "nominally_open_pediatric": None,
            "zero_enrolled_pediatric": False,
            "pediatric_representation_gap": False,
            "pediatric_gap_basis": None,
            "pediatric_gap_evidence_bases": [],
            "enrolled_outside_eligibility_bounds": False,
            "outside_eligibility_reasons": [],
        }

    nominally_open_65_plus = bool(eligible_hi is None or eligible_hi > 65.0)
    nominally_open_75_plus = bool(eligible_hi is None or eligible_hi > 75.0)
    nominally_open_pediatric = bool(eligible_lo < 18.0 - UNIT_CONVERSION_TOL_YEARS)

    raw_brackets, n_unk, cat_usable = _extract_categorical_from_parsed(p_age)
    cont_min, cont_max, guaranteed_min_le, guaranteed_max_ge = _extract_continuous_stats_from_parsed(p_age)

    bounds_65: Optional[dict[str, Any]] = None
    bounds_75: Optional[dict[str, Any]] = None
    bounds_ped: Optional[dict[str, Any]] = None
    if cat_usable:
        bounds_65 = age_range_bounds(raw_brackets, n_unk, 65.0, None)
        bounds_75 = age_range_bounds(raw_brackets, n_unk, 75.0, None)
        bounds_ped = age_range_bounds(raw_brackets, n_unk, 0.0, 18.0)

    cont_proves_65 = guaranteed_max_ge is not None and guaranteed_max_ge >= 65.0
    cont_proves_75 = guaranteed_max_ge is not None and guaranteed_max_ge >= 75.0
    cont_proves_ped = guaranteed_min_le is not None and guaranteed_min_le < 18.0

    cat_proves_65 = bounds_65 is not None and bounds_65["min_count"] > 0.0
    cat_proves_75 = bounds_75 is not None and bounds_75["min_count"] > 0.0
    cat_proves_ped = bounds_ped is not None and bounds_ped["min_count"] > 0.0

    # >= 65 representation gap check (never assert zero if the companion modality proves enrollment)
    bases_65: list[str] = []
    if (
        bounds_65 is not None
        and bounds_65["n_denominator_a"] > 0.0
        and bounds_65["max_count"] == 0.0
        and not cont_proves_65
    ):
        bases_65.append("categorical_zero_in_65_plus")
    if cont_max is not None and cont_max < 65.0 and not cat_proves_65:
        bases_65.append("continuous_max_below_65")
    zero_65 = len(bases_65) > 0
    gap_65 = bool(nominally_open_65_plus and zero_65)
    basis_65: Optional[str] = bases_65[0] if gap_65 else None

    # >= 75 representation gap check
    bases_75: list[str] = []
    if (
        bounds_75 is not None
        and bounds_75["n_denominator_a"] > 0.0
        and bounds_75["max_count"] == 0.0
        and not cont_proves_75
    ):
        bases_75.append("categorical_zero_in_75_plus")
    if cont_max is not None and cont_max < 75.0 and not cat_proves_75:
        bases_75.append("continuous_max_below_75")
    zero_75 = len(bases_75) > 0
    gap_75 = bool(nominally_open_75_plus and zero_75)
    basis_75: Optional[str] = bases_75[0] if gap_75 else None

    # < 18 pediatric representation gap check
    bases_ped: list[str] = []
    if (
        bounds_ped is not None
        and bounds_ped["n_denominator_a"] > 0.0
        and bounds_ped["max_count"] == 0.0
        and not cont_proves_ped
    ):
        bases_ped.append("categorical_zero_in_pediatric")
    if cont_min is not None and cont_min >= 18.0 and not cat_proves_ped:
        bases_ped.append("continuous_min_at_least_18")
    zero_ped = len(bases_ped) > 0
    gap_ped = bool(nominally_open_pediatric and zero_ped)
    basis_ped: Optional[str] = bases_ped[0] if gap_ped else None

    # Enrolled outside [eligible_lo, effective_hi_excl) check
    eff_hi_excl = _effective_exclusive_upper_bound(elig_rec.get("max_age"), eligible_hi)
    outside_reasons: list[str] = []
    if cont_min is not None and cont_min < eligible_lo - UNIT_CONVERSION_TOL_YEARS:
        outside_reasons.append("continuous_min_below_eligible_lo")
    elif guaranteed_min_le is not None and guaranteed_min_le < eligible_lo - UNIT_CONVERSION_TOL_YEARS:
        outside_reasons.append("pigeonhole_central_or_iqr_below_eligible_lo")

    if (
        cont_max is not None
        and eff_hi_excl is not None
        and cont_max >= eff_hi_excl - _SETTLEMENT_TIE_EPS_PP
    ):
        outside_reasons.append("continuous_max_at_or_above_eligible_hi")
    elif (
        guaranteed_max_ge is not None
        and eff_hi_excl is not None
        and guaranteed_max_ge >= eff_hi_excl - _SETTLEMENT_TIE_EPS_PP
    ):
        outside_reasons.append("pigeonhole_central_or_iqr_at_or_above_eligible_hi")

    if cat_usable:
        coerced_brackets = [_coerce_bracket(b) for b in raw_brackets]
        for b in coerced_brackets:
            if b.count <= 0.0:
                continue
            hi_incl = (
                "inclusive_decimal_upper" in b.bound_basis
                or "_le_explicit_dec_" in b.bound_basis
            )
            below_lo = (
                b.hi_years < eligible_lo - UNIT_CONVERSION_TOL_YEARS
                if (hi_incl and b.hi_years is not None)
                else (b.hi_years is not None and b.hi_years <= eligible_lo + UNIT_CONVERSION_TOL_YEARS)
            )
            if below_lo:
                if "categorical_bracket_below_eligible_lo" not in outside_reasons:
                    outside_reasons.append("categorical_bracket_below_eligible_lo")
            if eff_hi_excl is not None and b.lo_years >= eff_hi_excl - _SETTLEMENT_TIE_EPS_PP:
                if "categorical_bracket_at_or_above_eligible_hi" not in outside_reasons:
                    outside_reasons.append("categorical_bracket_at_or_above_eligible_hi")

    std_ages = [str(s).upper() for s in (elig_rec.get("std_ages") or [])]
    min_age_raw = elig_rec.get("min_age")
    min_age_unbounded = min_age_raw is None or not str(min_age_raw).strip() or str(min_age_raw).strip().upper() == "N/A"
    flag_unbounded_min_adult_std = bool(
        min_age_unbounded and std_ages and "CHILD" not in std_ages
    )

    return {
        "eligibility_parse_ok": True,
        "eligibility_error": None,
        "eligible_lo": eligible_lo,
        "eligible_hi": eligible_hi,
        "nominally_open_65_plus": nominally_open_65_plus,
        "zero_enrolled_65_plus": zero_65,
        "older_adult_representation_gap": gap_65,
        "older_adult_gap_basis": basis_65,
        "older_adult_gap_evidence_bases": bases_65 if gap_65 else [],
        "nominally_open_75_plus": nominally_open_75_plus,
        "zero_enrolled_75_plus": zero_75,
        "older_adult_75_plus_representation_gap": gap_75,
        "older_adult_75_plus_gap_basis": basis_75,
        "older_adult_75_plus_gap_evidence_bases": bases_75 if gap_75 else [],
        "nominally_open_pediatric": nominally_open_pediatric,
        "zero_enrolled_pediatric": zero_ped,
        "pediatric_representation_gap": gap_ped,
        "pediatric_gap_basis": basis_ped,
        "pediatric_gap_evidence_bases": bases_ped if gap_ped else [],
        "enrolled_outside_eligibility_bounds": len(outside_reasons) > 0,
        "outside_eligibility_reasons": outside_reasons,
        "flag_unbounded_min_age_adult_std_ages": flag_unbounded_min_adult_std,
        "unit_conversion_tolerance_years": UNIT_CONVERSION_TOL_YEARS,
    }


# ---------------------------------------------------------------------------
# 3. ACS B01001 expected age-band shares & interval comparison
# ---------------------------------------------------------------------------


def _intersect_intervals(
    band_lo: float,
    band_hi: Optional[float],
    elig_lo: float,
    elig_hi: Optional[float],
) -> tuple[float, Optional[float], bool]:
    """Intersect [band_lo, band_hi) with [elig_lo, elig_hi). Returns (lo, hi, is_nonempty)."""
    lo = max(band_lo, elig_lo)
    if band_hi is None and elig_hi is None:
        hi: Optional[float] = None
    elif band_hi is None:
        hi = elig_hi
    elif elig_hi is None:
        hi = band_hi
    else:
        hi = min(band_hi, elig_hi)

    if hi is not None and hi <= lo + _CUTOFF_EPS:
        return lo, lo, False
    return lo, hi, True


def compare_age_to_acs(
    parsed_age: Union[ap.ParsedAgeTrial, dict[str, Any]],
    level: acs.Level,
    geoid: str,
    *,
    sexes: tuple[str, ...],
    eligible_lo: float,
    eligible_hi: Optional[float],
) -> dict[str, Any]:
    """
    Compare a trial's categorical baseline age bounds against ACS 5-year B01001
    ('total' table) residents in `geoid` restricted to `sexes` and
    `[eligible_lo, eligible_hi)`.

    For each canonical band ([0, 18), [18, 65), [65, None), [75, None)):
      - Computes expected_pct and expected_moe90_pp using census.benchmark.band_weights,
        census.benchmark._sum, and census.benchmark._share over the intersection of
        the band with [eligible_lo, eligible_hi).
      - Compares against the trial's exact [floor_pct, ceiling_pct] on Denominator A,
        setting direction_settled = True iff (ceiling_pct < expected_pct or floor_pct > expected_pct).
    """
    if not sexes or any(s not in acs.SEXES for s in sexes):
        raise ValueError(f"sexes must be a non-empty subset of {acs.SEXES!r}, got {sexes!r}")
    if isinstance(eligible_lo, bool) or not isinstance(eligible_lo, (int, float)) or not math.isfinite(eligible_lo) or eligible_lo < 0.0:
        raise ValueError(f"eligible_lo must be finite numeric and >= 0, got {eligible_lo!r}")
    if eligible_hi is not None and (
        isinstance(eligible_hi, bool)
        or not isinstance(eligible_hi, (int, float))
        or not math.isfinite(eligible_hi)
        or eligible_hi <= eligible_lo
    ):
        raise ValueError(f"eligible_hi must be numeric > eligible_lo ({eligible_lo!r}), got {eligible_hi!r}")

    area = level.index.get(geoid)
    if area is None:
        return {
            "available": False,
            "reason": "area_not_in_this_window_file",
            "geoid": geoid,
        }

    w_elig = benchmark.band_weights(level.edges, eligible_lo, eligible_hi)
    t_tot = acs.TABLE_ORDER.index("total")
    s_idx = [acs.SEXES.index(x) for x in sexes]
    active_est = level.est[area, t_tot][s_idx][:, w_elig > 0.0]
    if active_est.size > 0 and (
        not bool(np.isfinite(active_est).all()) or bool((active_est < 0.0).any())
    ):
        return {
            "available": False,
            "reason": "estimate_not_published",
            "geoid": geoid,
        }
    den, den_moe = benchmark._sum(level, area, "total", sexes, w_elig)
    if not math.isfinite(den) or den < 0.0:
        return {
            "available": False,
            "reason": "estimate_not_published",
            "geoid": geoid,
        }
    if den == 0.0:
        return {
            "available": False,
            "reason": "no_eligible_residents_estimated",
            "geoid": geoid,
        }

    raw_brackets, n_unk, cat_usable = _extract_categorical_from_parsed(parsed_age)

    bands_out: dict[str, dict[str, Any]] = {}
    for band_name, (b_lo, b_hi) in STANDARD_TARGET_BANDS.items():
        int_lo, int_hi, nonempty = _intersect_intervals(b_lo, b_hi, eligible_lo, eligible_hi)
        if not nonempty:
            exp_pct = 0.0
            exp_moe_pp: Optional[float] = 0.0
        else:
            w_band = benchmark.band_weights(level.edges, int_lo, int_hi)
            num, num_moe = benchmark._sum(level, area, "total", sexes, w_band)
            p, m = benchmark._share(num, num_moe, den, den_moe)
            if not math.isfinite(p):
                return {
                    "available": False,
                    "reason": "estimate_not_published",
                    "geoid": geoid,
                }
            exp_pct = (100.0 * num) / den
            exp_moe_pp = None if (m is None or not math.isfinite(m)) else 100.0 * m

        if not cat_usable:
            bands_out[band_name] = {
                "target_lo": b_lo,
                "target_hi": b_hi,
                "expected_pct": exp_pct,
                "expected_moe90_pp": exp_moe_pp,
                "trial_categorical_available": False,
                "floor_pct": None,
                "ceiling_pct": None,
                "floor_diff_pp": None,
                "ceiling_diff_pp": None,
                "floor_ratio": None,
                "ceiling_ratio": None,
                "cutpoint_exact": None,
                "has_straddling_bracket": None,
                "direction_settled": None,
                "direction": None,
                "of_classified": None,
            }
            continue

        tb = age_range_bounds(raw_brackets, n_unk, b_lo, b_hi)
        fl = tb["floor_pct"]
        ce = tb["ceiling_pct"]
        assert fl is not None and ce is not None

        if ce < exp_pct - _SETTLEMENT_TIE_EPS_PP:
            settled = True
            direction = "below_expected"
        elif fl > exp_pct + _SETTLEMENT_TIE_EPS_PP:
            settled = True
            direction = "above_expected"
        else:
            settled = False
            direction = "indeterminate_or_equal"

        bands_out[band_name] = {
            "target_lo": b_lo,
            "target_hi": b_hi,
            "expected_pct": exp_pct,
            "expected_moe90_pp": exp_moe_pp,
            "trial_categorical_available": True,
            "min_count": tb["min_count"],
            "max_count": tb["max_count"],
            "n_denominator_a": tb["n_denominator_a"],
            "n_unknown": tb["n_unknown"],
            "floor_pct": fl,
            "ceiling_pct": ce,
            "floor_diff_pp": fl - exp_pct,
            "ceiling_diff_pp": ce - exp_pct,
            "floor_ratio": (fl / exp_pct) if exp_pct > 0.0 else None,
            "ceiling_ratio": (ce / exp_pct) if exp_pct > 0.0 else None,
            **(
                {}
                if exp_pct > 0.0
                else {"ratio_undefined": "reference_share_is_zero"}
            ),
            "cutpoint_exact": tb["cutpoint_exact"],
            "has_straddling_bracket": tb["has_straddling_bracket"],
            "straddling_brackets": tb["straddling_brackets"],
            "direction_settled": settled,
            "direction": direction,
            "of_classified": tb["of_classified"],
        }

    return {
        "available": True,
        "geoid": geoid,
        "area_name": level.names[area] if area < len(level.names) else "",
        "sexes": sexes,
        "eligible_lo": eligible_lo,
        "eligible_hi": eligible_hi,
        "eligible_residents": round(den),
        "eligible_residents_moe90": None if not math.isfinite(den_moe) else round(den_moe, 2),
        "trial_categorical_available": cat_usable,
        "bands": bands_out,
    }
