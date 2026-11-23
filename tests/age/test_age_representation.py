"""Unit tests for age/representation.py (cutoff bounds, eligibility gaps, and ACS B01001)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from age import parser as ap  # noqa: E402
from age import representation as ar  # noqa: E402
from age import table as at  # noqa: E402
from census import acs  # noqa: E402

SNAP = "2026-11-23"


def _make_acs_level(
    *,
    geoid: str = "24510",
    name: str = "Baltimore city, Maryland",
    male_counts: list[float] | None = None,
    female_counts: list[float] | None = None,
    moe_fraction: float = 0.05,
) -> acs.Level:
    """
    Construct a synthetic acs.Level using standard 14-band ACS race/age edges:
    [0,5), [5,10), [10,15), [15,18), [18,20), [20,25), [25,30), [30,35),
    [35,45), [45,55), [55,65), [65,75), [75,85), [85,None).
    """
    edges: list[tuple[float, float | None]] = [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 15.0),
        (15.0, 18.0),
        (18.0, 20.0),
        (20.0, 25.0),
        (25.0, 30.0),
        (30.0, 35.0),
        (35.0, 45.0),
        (45.0, 55.0),
        (55.0, 65.0),
        (65.0, 75.0),
        (75.0, 85.0),
        (85.0, None),
    ]
    m_cnt = male_counts or [
        250.0, 250.0, 250.0, 250.0,  # [0, 18): 1,000 male
        300.0, 300.0, 300.0, 300.0, 600.0, 600.0, 600.0,  # [18, 65): 3,000 male
        600.0, 300.0, 100.0,  # [65, None): 1,000 male ([75, None): 400 male)
    ]
    f_cnt = female_counts or list(m_cnt)

    n_tables = len(acs.TABLE_ORDER)
    est = np.zeros((1, n_tables, 2, len(edges)), dtype=float)
    moe = np.zeros_like(est)

    t_total = acs.TABLE_ORDER.index("total")
    est[0, t_total, 0, :] = np.array(m_cnt, dtype=float)
    est[0, t_total, 1, :] = np.array(f_cnt, dtype=float)
    moe[0, t_total, 0, :] = est[0, t_total, 0, :] * moe_fraction
    moe[0, t_total, 1, :] = est[0, t_total, 1, :] * moe_fraction

    return acs.Level([geoid], [name], edges, est, moe)


def _study_with_age(
    nct_id: str,
    measures: list[dict],
    *,
    min_age: str = "18 Years",
    max_age: str = "N/A",
    std_ages: list[str] | None = None,
    enrollment: int = 100,
) -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "designModule": {"enrollmentInfo": {"count": enrollment, "type": "ACTUAL"}},
            "eligibilityModule": {
                "minimumAge": min_age,
                "maximumAge": max_age,
                "stdAges": std_ages if std_ages is not None else ["ADULT", "OLDER_ADULT"],
            },
        },
        "resultsSection": {
            "baselineCharacteristicsModule": {"measures": measures},
        },
    }


def _cat_m(rows: list[tuple[str, int | str]]) -> dict:
    return {
        "title": "Age, Categorical",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "categories": [
                    {"title": lbl, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                    for lbl, v in rows
                ]
            }
        ],
    }


def _cont_range_m(median: str, lo: str, hi: str, unit: str = "years") -> dict:
    return {
        "title": "Age, Continuous",
        "paramType": "MEDIAN",
        "dispersionType": "FULL_RANGE",
        "unitOfMeasure": unit,
        "classes": [
            {
                "categories": [
                    {
                        "measurements": [
                            {
                                "groupId": "BG000",
                                "value": median,
                                "lowerLimit": lo,
                                "upperLimit": hi,
                            }
                        ]
                    }
                ]
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Exact cutoff bounds (no uniform interpolation inside straddling brackets)
# ---------------------------------------------------------------------------


def test_age_range_bounds_exact_cutpoints_with_and_without_unknowns() -> None:
    p = ap.parse_trial(
        [_cat_m([("< 18 years", 10), ("18 to 64 years", 60), ("65 to 74 years", 20), (">= 75 years", 10)])]
    )
    b65 = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 65.0, None)
    assert b65["cutpoint_exact"] is True
    assert b65["has_straddling_bracket"] is False
    assert b65["min_count"] == 30.0
    assert b65["max_count"] == 30.0
    assert b65["floor_pct"] == pytest.approx(30.0)
    assert b65["ceiling_pct"] == pytest.approx(30.0)
    assert b65["of_classified"]["point_pct"] == pytest.approx(30.0)

    # With n_unknown = 25 (total Denom A = 125, Denom B = 100):
    b65_unk = ar.age_range_bounds(p.parsed_brackets, 25.0, 65.0, None)
    assert b65_unk["cutpoint_exact"] is True
    assert b65_unk["min_count"] == 30.0
    assert b65_unk["max_count"] == 55.0
    assert b65_unk["floor_pct"] == pytest.approx(24.0)
    assert b65_unk["ceiling_pct"] == pytest.approx(44.0)
    # Denominator B (of_classified) is unaffected by n_unknown!
    assert b65_unk["of_classified"]["point_pct"] == pytest.approx(30.0)
    assert b65_unk["of_classified"]["classified_bounds"]["floor_pct"] == pytest.approx(30.0)
    assert b65_unk["of_classified"]["classified_bounds"]["ceiling_pct"] == pytest.approx(30.0)


def test_age_range_bounds_straddling_bracket_never_interpolates() -> None:
    # "50 to 70 years" ([50, 71)) straddles 65.0: NO 6/21 uniform interpolation allowed!
    p = ap.parse_trial([_cat_m([("18 to 49 years", 40), ("50 to 70 years", 40), (">= 71 years", 10), ("Unknown", 10)])])
    b65 = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 65.0, None)

    assert b65["has_straddling_bracket"] is True
    assert b65["cutpoint_exact"] is False
    assert b65["straddling_brackets"] == ["50 to 70 years"]
    assert b65["straddling_count"] == 40.0
    # Denominator A (N = 100): definite >=71 is 10; max adds 40 straddling + 10 unknown = 60
    assert b65["min_count"] == 10.0
    assert b65["max_count"] == 60.0
    assert b65["floor_pct"] == pytest.approx(10.0)
    assert b65["ceiling_pct"] == pytest.approx(60.0)

    # Denominator B (of_classified, N = 90): [10, 50] -> [11.111%, 55.555%], point_pct None
    of_cls = b65["of_classified"]
    assert of_cls["point_pct"] is None
    assert of_cls["classified_bounds"]["min_count"] == 10.0
    assert of_cls["classified_bounds"]["max_count"] == 50.0
    assert of_cls["classified_bounds"]["floor_pct"] == pytest.approx(100.0 * 10.0 / 90.0)
    assert of_cls["classified_bounds"]["ceiling_pct"] == pytest.approx(100.0 * 50.0 / 90.0)


def test_age_range_bounds_straddling_closed_target_and_validation() -> None:
    # Bracket "12 to 21 years" ([12, 22)) straddles 18.0 for BOTH [0, 18) and [18, 65)
    p = ap.parse_trial([_cat_m([("0 to 11 years", 20), ("12 to 21 years", 30), ("22 to 64 years", 50)])])
    ped = ar.age_range_bounds(p.parsed_brackets, 0.0, 0.0, 18.0)
    wrk = ar.age_range_bounds(p.parsed_brackets, 0.0, 18.0, 65.0)
    assert ped["min_count"] == 20.0 and ped["max_count"] == 50.0 and ped["has_straddling_bracket"] is True
    assert wrk["min_count"] == 50.0 and wrk["max_count"] == 80.0 and wrk["has_straddling_bracket"] is True

    with pytest.raises(ValueError, match="target_lo"):
        ar.age_range_bounds([], 0.0, -1.0, 18.0)
    with pytest.raises(ValueError, match="target_hi"):
        ar.age_range_bounds([], 0.0, 65.0, 65.0)


def test_standard_age_band_bounds_from_flat_row() -> None:
    s = _study_with_age("NCT99000601", [_cat_m([("< 18 years", 5), ("18 to 64 years", 75), (">= 65 years", 20)])], min_age="0 Years")
    row = at.build_age_row(s, SNAP)
    std = ar.standard_age_band_bounds(row)
    assert set(std.keys()) == set(ar.STANDARD_TARGET_BANDS.keys())
    assert std["pediatric_under_18"]["floor_pct"] == pytest.approx(5.0)
    assert std["working_age_18_64"]["floor_pct"] == pytest.approx(75.0)
    assert std["older_adults_65_plus"]["floor_pct"] == pytest.approx(20.0)
    # ">= 65 years" ([65, None)) straddles 75.0!
    assert std["older_adults_75_plus"]["min_count"] == 0.0
    assert std["older_adults_75_plus"]["max_count"] == 20.0
    assert std["older_adults_75_plus"]["has_straddling_bracket"] is True


# ---------------------------------------------------------------------------
# 2. Eligibility vs enrolled baseline age audit
# ---------------------------------------------------------------------------


def test_older_adult_representation_gap_categorical_and_continuous() -> None:
    # Categorical zero in >=65 while max_age="N/A"
    s_cat = _study_with_age(
        "NCT99000701",
        [_cat_m([("18 to 64 years", 80), (">= 65 years", 0)])],
        min_age="18 Years",
        max_age="N/A",
    )
    aud_cat = ar.audit_eligibility_vs_enrolled(s_cat)
    assert aud_cat["nominally_open_65_plus"] is True
    assert aud_cat["zero_enrolled_65_plus"] is True
    assert aud_cat["older_adult_representation_gap"] is True
    assert aud_cat["older_adult_gap_basis"] == "categorical_zero_in_65_plus"

    # Continuous FULL_RANGE max = 61 years (< 65.0) while max_age = "80 Years"
    s_cont = _study_with_age(
        "NCT99000702",
        [_cont_range_m("48", "22", "61")],
        min_age="18 Years",
        max_age="80 Years",
    )
    aud_cont = ar.audit_eligibility_vs_enrolled(s_cont)
    assert aud_cont["nominally_open_65_plus"] is True
    assert aud_cont["zero_enrolled_65_plus"] is True
    assert aud_cont["older_adult_representation_gap"] is True
    assert aud_cont["older_adult_gap_basis"] == "continuous_max_below_65"
    assert aud_cont["older_adult_75_plus_representation_gap"] is True
    assert aud_cont["older_adult_75_plus_gap_basis"] == "continuous_max_below_75"


def test_no_false_gap_when_protocol_excludes_65_or_bracket_straddles() -> None:
    # Protocol explicitly caps at "64 Years" -> eligible_hi == 65.0 -> NOT nominally open to 65+
    s_capped = _study_with_age(
        "NCT99000703",
        [_cat_m([("18 to 64 years", 80), (">= 65 years", 0)])],
        min_age="18 Years",
        max_age="64 Years",
    )
    aud_capped = ar.audit_eligibility_vs_enrolled(s_capped)
    assert aud_capped["nominally_open_65_plus"] is False
    assert aud_capped["zero_enrolled_65_plus"] is True
    assert aud_capped["older_adult_representation_gap"] is False
    assert aud_capped["older_adult_gap_basis"] is None

    # Straddling bracket "50 to 70 years": 30 -> max_count_65_plus == 30 > 0 -> NOT proven zero!
    s_strad = _study_with_age(
        "NCT99000704",
        [_cat_m([("18 to 49 years", 50), ("50 to 70 years", 30), (">= 71 years", 0)])],
        min_age="18 Years",
        max_age="N/A",
    )
    aud_strad = ar.audit_eligibility_vs_enrolled(s_strad)
    assert aud_strad["zero_enrolled_65_plus"] is False
    assert aud_strad["older_adult_representation_gap"] is False


def test_pediatric_and_75_plus_representation_gaps() -> None:
    # Nominally open to children (min_age="6 Years"), yet enrolled min_years = 19.0
    s_ped_cont = _study_with_age(
        "NCT99000705",
        [_cont_range_m("34", "19", "68")],
        min_age="6 Years",
        max_age="N/A",
        std_ages=["CHILD", "ADULT", "OLDER_ADULT"],
    )
    aud_ped = ar.audit_eligibility_vs_enrolled(s_ped_cont)
    assert aud_ped["nominally_open_pediatric"] is True
    assert aud_ped["zero_enrolled_pediatric"] is True
    assert aud_ped["pediatric_representation_gap"] is True
    assert aud_ped["pediatric_gap_basis"] == "continuous_min_at_least_18"

    # Categorical pediatric zero + >=75 zero
    s_ped_cat = _study_with_age(
        "NCT99000706",
        [_cat_m([("< 18 years", 0), ("18 to 64 years", 70), ("65 to 74 years", 20), (">= 75 years", 0)])],
        min_age="12 Years",
        max_age="N/A",
    )
    aud_cat = ar.audit_eligibility_vs_enrolled(s_ped_cat)
    assert aud_cat["pediatric_representation_gap"] is True
    assert aud_cat["pediatric_gap_basis"] == "categorical_zero_in_pediatric"
    assert aud_cat["older_adult_representation_gap"] is False
    assert aud_cat["older_adult_75_plus_representation_gap"] is True
    assert aud_cat["older_adult_75_plus_gap_basis"] == "categorical_zero_in_75_plus"


def test_enrolled_outside_eligibility_bounds_and_unit_conversion_tolerance() -> None:
    # Continuous min below 18 and max >= 66 (when max_age="65 Years" -> eligible_hi=66.0)
    s_out_cont = _study_with_age(
        "NCT99000707",
        [_cont_range_m("45", "16", "66")],
        min_age="18 Years",
        max_age="65 Years",
    )
    aud = ar.audit_eligibility_vs_enrolled(s_out_cont)
    assert aud["enrolled_outside_eligibility_bounds"] is True
    assert "continuous_min_below_eligible_lo" in aud["outside_eligibility_reasons"]
    assert "continuous_max_at_or_above_eligible_hi" in aud["outside_eligibility_reasons"]

    # Max = 65 on max_age="65 Years" (eligible_hi=66.0) is INSIDE bounds!
    s_in_cont = _study_with_age(
        "NCT99000708",
        [_cont_range_m("45", "18", "65")],
        min_age="18 Years",
        max_age="65 Years",
    )
    assert ar.audit_eligibility_vs_enrolled(s_in_cont)["enrolled_outside_eligibility_bounds"] is False

    # Unit conversion tolerance: min_age="12 Months" (1.0 yr) vs continuous min="52 weeks" (1.0 yr)
    s_tol = _study_with_age(
        "NCT99000709",
        [_cont_range_m("104", "52", "156", unit="weeks")],
        min_age="12 Months",
        max_age="4 Years",
    )
    assert ar.audit_eligibility_vs_enrolled(s_tol)["enrolled_outside_eligibility_bounds"] is False

    # Categorical non-zero bracket wholly outside vs zero-count bracket outside
    s_cat_out = _study_with_age(
        "NCT99000710",
        [_cat_m([("< 18 years", 3), ("18 to 64 years", 50)])],
        min_age="18 Years",
        max_age="65 Years",
    )
    aud_co = ar.audit_eligibility_vs_enrolled(s_cat_out)
    assert aud_co["enrolled_outside_eligibility_bounds"] is True
    assert "categorical_bracket_below_eligible_lo" in aud_co["outside_eligibility_reasons"]

    s_cat_zero = _study_with_age(
        "NCT99000711",
        [_cat_m([("< 18 years", 0), ("18 to 64 years", 50)])],
        min_age="18 Years",
        max_age="65 Years",
    )
    assert ar.audit_eligibility_vs_enrolled(s_cat_zero)["enrolled_outside_eligibility_bounds"] is False


# ---------------------------------------------------------------------------
# 3. ACS B01001 expected age-band comparison & direction_settled
# ---------------------------------------------------------------------------


def test_compare_age_to_acs_expected_shares_and_direction_settled() -> None:
    # Synthetic area: [0,18) = 2,000; [18,65) = 6,000; [65,None) = 2,000 ([75,None) = 800).
    # For adult >=18 trial (eligible_lo=18.0, eligible_hi=None):
    # Eligible residents = 8,000 -> [0,18)=0%, [18,65)=75%, [65,None)=25%, [75,None)=10%.
    lvl = _make_acs_level()

    # Trial 1: exact cutpoints, 10% in >=65 (below 25% expected), 0 unknowns -> direction settled!
    p_low = ap.parse_trial([_cat_m([("18 to 64 years", 90), ("65 to 74 years", 8), (">= 75 years", 2)])])
    cmp_low = ar.compare_age_to_acs(
        p_low, lvl, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    assert cmp_low["available"] is True
    assert cmp_low["eligible_residents"] == 8000

    b_ped = cmp_low["bands"]["pediatric_under_18"]
    b_wrk = cmp_low["bands"]["working_age_18_64"]
    b_65 = cmp_low["bands"]["older_adults_65_plus"]
    b_75 = cmp_low["bands"]["older_adults_75_plus"]

    assert b_ped["expected_pct"] == pytest.approx(0.0)
    assert b_wrk["expected_pct"] == pytest.approx(75.0)
    assert b_65["expected_pct"] == pytest.approx(25.0)
    assert b_75["expected_pct"] == pytest.approx(10.0)
    assert (
        b_ped["expected_pct"] + b_wrk["expected_pct"] + b_65["expected_pct"]
    ) == pytest.approx(100.0)

    assert b_65["floor_pct"] == pytest.approx(10.0)
    assert b_65["ceiling_pct"] == pytest.approx(10.0)
    assert b_65["direction_settled"] is True
    assert b_65["direction"] == "below_expected"
    assert b_wrk["direction_settled"] is True
    assert b_wrk["direction"] == "above_expected"


def test_compare_age_to_acs_indeterminate_direction_under_straddle_or_unknowns() -> None:
    lvl = _make_acs_level()  # >=65 expected = 25.0% among >=18

    # Straddling bracket "55 to 70 years" gives floor_pct = 10%, ceiling_pct = 40% around 25% -> unsettled!
    p_strad = ap.parse_trial([_cat_m([("18 to 54 years", 60), ("55 to 70 years", 30), (">= 71 years", 10)])])
    cmp_s = ar.compare_age_to_acs(
        p_strad, lvl, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    b65 = cmp_s["bands"]["older_adults_65_plus"]
    assert b65["floor_pct"] == pytest.approx(10.0)
    assert b65["ceiling_pct"] == pytest.approx(40.0)
    assert b65["direction_settled"] is False
    assert b65["direction"] == "indeterminate_or_equal"

    # Even with straddling bracket, if ceiling_pct < expected_pct (e.g. floor=2%, ceiling=15% < 25%), direction IS settled!
    p_strad_low = ap.parse_trial([_cat_m([("18 to 54 years", 85), ("55 to 70 years", 13), (">= 71 years", 2)])])
    cmp_sl = ar.compare_age_to_acs(
        p_strad_low, lvl, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    assert cmp_sl["bands"]["older_adults_65_plus"]["direction_settled"] is True
    assert cmp_sl["bands"]["older_adults_65_plus"]["direction"] == "below_expected"


def test_compare_age_to_acs_restricted_eligibility_and_unavailable_cases() -> None:
    lvl = _make_acs_level()

    # Restricted eligibility [25.0, 75.0) & single sex ("female")
    p = ap.parse_trial([_cat_m([("25 to 64 years", 70), ("65 to 74 years", 30)])])
    res = ar.compare_age_to_acs(p, lvl, "24510", sexes=("female",), eligible_lo=25.0, eligible_hi=75.0)
    assert res["available"] is True
    assert res["bands"]["pediatric_under_18"]["expected_pct"] == pytest.approx(0.0)
    assert res["bands"]["older_adults_75_plus"]["expected_pct"] == pytest.approx(0.0)
    assert (
        res["bands"]["working_age_18_64"]["expected_pct"]
        + res["bands"]["older_adults_65_plus"]["expected_pct"]
    ) == pytest.approx(100.0)

    # Missing geoid
    missing = ar.compare_age_to_acs(p, lvl, "99999", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None)
    assert missing == {"available": False, "reason": "area_not_in_this_window_file", "geoid": "99999"}

    # Continuous-only trial returns expected ACS shares with trial_categorical_available = False
    p_cont = ap.parse_trial([_cont_range_m("55", "20", "72")])
    res_cont = ar.compare_age_to_acs(p_cont, lvl, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None)
    assert res_cont["available"] is True
    assert res_cont["trial_categorical_available"] is False
    assert res_cont["bands"]["older_adults_65_plus"]["expected_pct"] == pytest.approx(25.0)
    assert res_cont["bands"]["older_adults_65_plus"]["floor_pct"] is None
    assert res_cont["bands"]["older_adults_65_plus"]["direction_settled"] is None


def test_compare_age_to_acs_nan_moe_and_json_strict_serialization() -> None:
    lvl = _make_acs_level()
    # Inject NaN MOEs and zero-denominator area
    lvl.moe[:] = np.nan
    p = ap.parse_trial([_cat_m([("18 to 64 years", 70), (">= 65 years", 30)])])
    res = ar.compare_age_to_acs(p, lvl, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None)
    assert res["available"] is True
    assert res["eligible_residents_moe90"] is None
    assert res["bands"]["older_adults_65_plus"]["expected_moe90_pp"] is None
    # Strict JSON check: zero NaN/Inf anywhere in output
    encoded = json.dumps(res, allow_nan=False)
    assert "NaN" not in encoded

    # Zero eligible residents
    lvl_zero = _make_acs_level(male_counts=[0.0] * 14, female_counts=[0.0] * 14)
    res_zero = ar.compare_age_to_acs(p, lvl_zero, "24510", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None)
    assert res_zero == {"available": False, "reason": "no_eligible_residents_estimated", "geoid": "24510"}
