"""Round-2 adversarial red-team verification tests for age/ after Round-1 fixes.

Audits second-order edge cases and regressions across:
  1. _resolve_target_denom, 'incomplete_categorical_brackets', percentage
     rounding slack across small and large denominators, and count totals
     exceeding attested table totals.
  2. _infer_categorical_default_unit: valid parenthetical/title units vs
     conflicting title/unitOfMeasure/row units, mixed-unit titles, and the
     'min' ('minimum' vs 'minute') token collision.
  3. Decimal closed upper bounds ('<= 65.0 years', '0.0 to <= 65.0 years',
     '0.0 to 64.9 years' + '65.0 years and older', '<= 17.99 years', and
     point-seam overlap on '<= 65.0' + '>= 65.0').
  4. _strip_thousands_commas vs 3-digit upper bounds in no-space comma pairs
     ('50 (10,100)', '(18,100)', '[18,105]'), thousands-formatted ranges with
     spaces ('1,200 (18, 2,400)'), European decimal commas ('12,50'), and
     duplicate groupId measurement entries.
  5. bool-as-int rejection across all public functions and dict-row pathways
     (age_range_bounds, standard_age_band_bounds, audit_eligibility_vs_enrolled,
     compare_age_to_acs, build_age_row, select_raw_age_measures).
  6. compare_age_to_acs edge cases: unpublished/NaN/Inf/negative sentinel ACS
     cells, missing geoid, single-sex restriction, and pediatric [0, 18) trials
     evaluated on zero-expected >=65 / >=75 bands.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from age import parser as ap  # noqa: E402
from age import representation as ar  # noqa: E402
from age import table as at  # noqa: E402
from census import acs  # noqa: E402

SNAP = "2026-11-23"


def _cat_m(
    title: str,
    rows: list[tuple[str, Any]],
    *,
    param_type: str = "COUNT_OF_PARTICIPANTS",
    unit: str = "Participants",
    group_id: str = "BG000",
    denoms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "title": title,
        "paramType": param_type,
        "unitOfMeasure": unit,
        "classes": [
            {
                "categories": [
                    {
                        "title": lbl,
                        "measurements": [{"groupId": group_id, "value": str(val)}],
                    }
                    for lbl, val in rows
                ]
            }
        ],
    }
    if denoms is not None:
        m["denoms"] = denoms
    return m


def _cont_m(
    title: str = "Age, Continuous",
    *,
    param_type: str | None = "MEAN",
    dispersion_type: str | None = "STANDARD_DEVIATION",
    unit: str | None = "years",
    value: Any = "54.5",
    spread: Any = None,
    lower: Any = None,
    upper: Any = None,
    dispersion_value: Any = None,
    group_id: str = "BG000",
) -> dict[str, Any]:
    mm: dict[str, Any] = {"groupId": group_id}
    if value is not None:
        mm["value"] = str(value)
    if spread is not None:
        mm["spread"] = str(spread)
    if lower is not None:
        mm["lowerLimit"] = str(lower)
    if upper is not None:
        mm["upperLimit"] = str(upper)
    if dispersion_value is not None:
        mm["dispersionValue"] = str(dispersion_value)
    m: dict[str, Any] = {
        "title": title,
        "classes": [{"categories": [{"measurements": [mm]}]}],
    }
    if param_type is not None:
        m["paramType"] = param_type
    if dispersion_type is not None:
        m["dispersionType"] = dispersion_type
    if unit is not None:
        m["unitOfMeasure"] = unit
    return m


def _study(
    nct_id: str,
    measures: Any,
    *,
    enrollment: Any = 100,
    min_age: str | None = "0 Years",
    max_age: str | None = "N/A",
    std_ages: list[str] | None = None,
    groups: list[dict[str, Any]] | None = None,
    denoms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    elig: dict[str, Any] = {}
    if min_age is not None:
        elig["minimumAge"] = min_age
    if max_age is not None:
        elig["maximumAge"] = max_age
    if std_ages is not None:
        elig["stdAges"] = std_ages

    design: dict[str, Any] = {}
    if enrollment is not None:
        design["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}

    bm: dict[str, Any] = {"measures": measures}
    if groups is not None:
        bm["groups"] = groups
    if denoms is not None:
        bm["denoms"] = denoms

    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "designModule": design,
            "eligibilityModule": elig,
        },
        "resultsSection": {
            "baselineCharacteristicsModule": bm,
        },
    }


def _acs_level(
    geoid: str,
    male_14: list[float],
    female_14: list[float] | None = None,
    moe_fraction: float = 0.05,
) -> acs.Level:
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
    f_14 = male_14 if female_14 is None else female_14
    est = np.zeros((1, len(acs.TABLE_ORDER), 2, len(edges)), dtype=float)
    moe = np.zeros_like(est)
    t_tot = acs.TABLE_ORDER.index("total")
    est[0, t_tot, 0, :] = np.array(male_14, dtype=float)
    est[0, t_tot, 1, :] = np.array(f_14, dtype=float)
    moe[0, t_tot, 0, :] = np.abs(est[0, t_tot, 0, :]) * moe_fraction
    moe[0, t_tot, 1, :] = np.abs(est[0, t_tot, 1, :]) * moe_fraction
    return acs.Level([geoid], ["Synthetic Catchment"], edges, est, moe)


# ===========================================================================
# 1. _resolve_target_denom, Percentage Rounding Slack, & Completeness Guards
# ===========================================================================


def test_percentage_rounding_99_9_and_100_1_pass_on_small_cohorts() -> None:
    """33.3%*3 = 99.9% on denom=300 and 33.4%+33.4%+33.3% = 100.1% on denom=100 pass cleanly."""
    den_300 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "300"}]}]
    p_999 = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "33.3"), ("18 to 64 years", "33.3"), (">= 65 years", "33.3")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_300,
            )
        ]
    )
    assert ap.classify_age_reporting(p_999) == "reported_categorical_only"
    assert p_999.n_total_parsed == 300.0

    den_100 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "100"}]}]
    p_1001 = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "33.4"), ("18 to 64 years", "33.4"), (">= 65 years", "33.3")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_100,
            )
        ]
    )
    assert ap.classify_age_reporting(p_1001) == "reported_categorical_only"
    assert p_1001.n_total_parsed == 99.0


def test_percentage_integer_rounding_99_pct_passes_on_denom_100() -> None:
    """33% + 33% + 33% = 99% on denom=100 passes within 1-unit integer rounding slack."""
    den_100 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "100"}]}]
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "33"), ("18 to 64 years", "33"), (">= 65 years", "33")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_100,
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.n_total_parsed == 99.0


def test_percentage_genuinely_omitted_bracket_70_pct_rejected_and_resets_flags() -> None:
    """30% + 40% = 70% on denom=100 is rejected as 'incomplete_categorical_brackets' without stale converted flag."""
    den_100 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "100"}]}]
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "30"), ("18 to 64 years", "40")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_100,
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "incomplete_categorical_brackets"
    assert p.n_total_parsed is None
    assert p.flag_percentage_converted is False


@pytest.mark.parametrize(
    ("denom", "pct_str"),
    [
        (3000, "33.3"),   # 99.9% (1-decimal rounding of 1000/3000 x 3 -> 999*3 = 2997 vs 3000)
        (10000, "33.3"),  # 99.9% on N=10,000 -> 3330*3 = 9990 vs 10,000
        (300, "33"),      # 99% (integer-pct rounding of 100/300 x 3 -> 99*3 = 297 vs 300)
        (1000, "33"),     # 99% (integer-pct rounding of 333/1000, 333/1000, 334/1000 or 1/3 x 3)
    ],
)
def test_percentage_rounding_slack_scales_with_denom_and_precision_defect(
    denom: int, pct_str: str
) -> None:
    """DEFECT (age/parser.py:1769): fixed headcount max_round_slack=2.0 falsely rejects 99.9% / 99% on larger denoms!

    1-decimal '33.3%' x 3 = 99.9% has 0.15% legitimate rounding slack (4.5 participants on
    denom=3000), and integer '33%' x 3 = 99% has 1.5% legitimate rounding slack (4.5 on
    denom=300, 15 on denom=1000), yet fixed headcount slack 2.0 rejects all of them as
    'incomplete_categorical_brackets'!
    """
    den = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(denom)}]}]
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", pct_str), ("18 to 64 years", pct_str), (">= 65 years", pct_str)],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den,
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "reported_categorical_only"


def test_categorical_count_sum_exceeding_attested_total_and_denom_rejected_defect() -> None:
    """DEFECT (age/parser.py:1807-1818): count completeness guard checks < attested_total but ignores > attested_total!

    When both the measure's 'Total' row and 'Participants' denominator attest N=50, yet the
    categorical rows sum to 100 (e.g. overlapping/duplicated sub-cohorts), parse_categorical_measure
    accepts n_total_parsed=100.0 instead of rejecting the inconsistent table!
    """
    den = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "50"}]}]
    m = _cat_m(
        "Age, Categorical",
        [("18 to 64 years", 60), (">= 65 years", 40), ("Total", 50)],
        denoms=den,
    )
    p = ap.parse_trial([m])
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.n_total_parsed is None


def test_resolve_target_denom_across_explicit_total_and_summed_arms() -> None:
    """Verify _resolve_target_denom enforces completeness on both explicit_total and sum_disjoint_arms."""
    groups_tot = [
        {"id": "BG000", "title": "Arm A"},
        {"id": "BG001", "title": "Arm B"},
        {"id": "BG002", "title": "Total"},
    ]
    denoms_tot = [
        {
            "units": "Participants",
            "counts": [
                {"groupId": "BG000", "value": "40"},
                {"groupId": "BG001", "value": "60"},
                {"groupId": "BG002", "value": "100"},
            ],
        }
    ]
    m_incomplete = _cat_m(
        "Age, Categorical",
        [("18 to 64 years", 70)],
        group_id="BG002",
    )
    p_inc = ap.parse_trial([m_incomplete], baseline_groups=groups_tot, baseline_denoms=denoms_tot)
    assert ap.classify_age_reporting(p_inc) == "uninformative"
    assert p_inc.uninformative_reason == "incomplete_categorical_brackets"

    groups_arms = [{"id": "BG000", "title": "Arm A"}, {"id": "BG001", "title": "Arm B"}]
    denoms_arms = [
        {
            "units": "Participants",
            "counts": [{"groupId": "BG000", "value": "40"}, {"groupId": "BG001", "value": "60"}],
        }
    ]
    m_arms_inc = {
        "title": "Age, Categorical",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "categories": [
                    {
                        "title": "18 to 64 years",
                        "measurements": [
                            {"groupId": "BG000", "value": "25"},
                            {"groupId": "BG001", "value": "35"},
                        ],
                    }
                ]
            }
        ],
    }
    p_arms = ap.parse_trial([m_arms_inc], baseline_groups=groups_arms, baseline_denoms=denoms_arms)
    assert ap.classify_age_reporting(p_arms) == "uninformative"
    assert p_arms.uninformative_reason == "incomplete_categorical_brackets"


# ===========================================================================
# 2. _infer_categorical_default_unit & Conflicting / Ambiguous Unit Traps
# ===========================================================================


@pytest.mark.parametrize(
    ("title", "unit", "exp_hi_first_bracket"),
    [
        ("Age (Months)", "Participants", 6.0 / 12.0),
        ("Age, Categorical (Days)", "Participants", 6.0 / 365.0),
        ("Age in Weeks", "Participants", 6.0 / 52.0),
        ("Age, Categorical", "Months", 6.0 / 12.0),
    ],
)
def test_categorical_unambiguous_title_and_unit_resolution(
    title: str, unit: str, exp_hi_first_bracket: float
) -> None:
    p = ap.parse_trial([_cat_m(title, [("0 to 5", 20), ("6 to 11", 30)], unit=unit)])
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.parsed_brackets[0].hi_years == pytest.approx(exp_hi_first_bracket)


def test_categorical_title_min_token_collision_with_minute_defect() -> None:
    """DEFECT (age/parser.py:129,1521): 'Age, Categorical (min 18)' matches 'min' as 'minute'!

    Adult participants in '18 to 64' and '>= 65' are converted at 1/525600 years/minute!
    """
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical (min 18)",
                [("18 to 64", 60), (">= 65", 40)],
                unit="Participants",
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.parsed_brackets[0].lo_years == pytest.approx(18.0)
    assert p.parsed_brackets[0].hi_years == pytest.approx(65.0)
    assert p.parsed_brackets[1].lo_years == pytest.approx(65.0)


def test_categorical_conflicting_title_months_vs_unit_years_refused_defect() -> None:
    """DEFECT (age/parser.py:1516-1524): conflicting title='Age in Months' and unitOfMeasure='Years' guesses Years!"""
    p = ap.parse_trial(
        [
            _cat_m(
                "Age in Months",
                [("0 to 11", 20), ("12 to 23", 30)],
                unit="Years",
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.flag_unrecognized_or_mixed_unit is True
    assert p.uninformative_reason == "unrecognized_or_mixed_unit"


def test_continuous_conflicting_title_months_vs_unit_years_refused_defect() -> None:
    """DEFECT (age/parser.py:1933-1950,2037): continuous title='Age in Months' + unitOfMeasure='Years' guesses Years!"""
    p = ap.parse_trial(
        [_cont_m("Age in Months", param_type="MEAN", unit="Years", value="12.0", spread="3.0")]
    )
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.flag_unrecognized_or_mixed_unit is True
    assert p.mean_years is None


def test_categorical_mixed_units_in_title_days_or_months_refused_defect() -> None:
    """DEFECT (age/parser.py:1521): 'Age (Days or Months)' with unitless brackets guesses 'month' by token length!"""
    p = ap.parse_trial(
        [
            _cat_m(
                "Age (Days or Months)",
                [("0 to 10", 20), ("11 to 20", 30)],
                unit="Participants",
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.flag_unrecognized_or_mixed_unit is True
    assert p.uninformative_reason == "unrecognized_or_mixed_unit"


# ===========================================================================
# 3. Decimal Closed Upper Bounds: Straddling & Non-Overlapping Adjacency
# ===========================================================================


def test_decimal_closed_le_65_0_and_gt_65_0_straddles_and_tiles_cleanly() -> None:
    p = ap.parse_trial([_cat_m("Age Group", [("<= 65.0 years", 40), ("> 65.0 years", 10)])])
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False
    b65 = ar.age_range_bounds(p.parsed_brackets, 0.0, 65.0, None)
    assert b65["has_straddling_bracket"] is True
    assert b65["cutpoint_exact"] is False
    assert b65["min_count"] == 10.0
    assert b65["max_count"] == 50.0


def test_decimal_range_explicit_le_65_0_and_gt_65_0_tiles_and_straddles() -> None:
    p = ap.parse_trial([_cat_m("Age Group", [("0.0 to <= 65.0 years", 35), ("> 65.0 years", 15)])])
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    b65 = ar.age_range_bounds(p.parsed_brackets, 0.0, 65.0, None)
    assert b65["has_straddling_bracket"] is True
    assert b65["min_count"] == 15.0
    assert b65["max_count"] == 50.0


def test_decimal_0_to_64_9_and_65_0_and_older_non_overlapping_exact_65_cutoff() -> None:
    p = ap.parse_trial(
        [_cat_m("Age Group", [("0.0 to 64.9 years", 70), ("65.0 years and older", 30)])]
    )
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is True
    b65 = ar.age_range_bounds(p.parsed_brackets, 0.0, 65.0, None)
    assert b65["cutpoint_exact"] is True
    assert b65["has_straddling_bracket"] is False
    assert b65["min_count"] == 30.0
    assert b65["max_count"] == 30.0


def test_decimal_le_17_99_and_ge_18_0_inside_pediatric_and_non_overlapping() -> None:
    p = ap.parse_trial([_cat_m("Age Group", [("<= 17.99 years", 25), (">= 18.0 years", 75)])])
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is True
    b_ped = ar.age_range_bounds(p.parsed_brackets, 0.0, 0.0, 18.0)
    assert b_ped["cutpoint_exact"] is True
    assert b_ped["min_count"] == 25.0
    assert b_ped["max_count"] == 25.0


def test_decimal_le_65_0_and_ge_65_0_point_seam_overlap_rejected_when_both_positive() -> None:
    """Both '<= 65.0 years' and '>= 65.0 years' include 65.0 -> point-seam overlap rejected when positive!"""
    p = ap.parse_trial([_cat_m("Age Group", [("<= 65.0 years", 40), (">= 65.0 years", 20)])])
    assert p.flag_overlapping_brackets is True
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "overlapping_age_brackets"


# ===========================================================================
# 4. _strip_thousands_commas, 3-Digit Comma Pairs, European Commas & Dup GroupId
# ===========================================================================


def test_spaced_comma_pair_with_3_digit_upper_and_thousands_numbers_valid() -> None:
    """'50 (10, 100)' and '1,200 (18, 2,400)' (with space after pair comma) parse cleanly."""
    c1 = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="years", value="50 (10, 100)")
    )
    assert c1.parse_valid is True
    assert c1.median_years == pytest.approx(50.0)
    assert c1.min_years == pytest.approx(10.0)
    assert c1.max_years == pytest.approx(100.0)

    c2 = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="days", value="1,200 (18, 2,400)")
    )
    assert c2.parse_valid is True
    assert c2.median_years == pytest.approx(1200.0 / 365.0)
    assert c2.min_years == pytest.approx(18.0 / 365.0)
    assert c2.max_years == pytest.approx(2400.0 / 365.0)


@pytest.mark.parametrize(
    ("param_type", "disp_type", "val_str", "exp_min", "exp_max"),
    [
        ("MEDIAN", "INTER_QUARTILE_RANGE", "50 (10,100)", 10.0, 100.0),
        ("MEDIAN", "FULL_RANGE", "50 (18,105)", 18.0, 105.0),
        ("NUMBER", "FULL_RANGE", "(18,100)", 18.0, 100.0),
        ("NUMBER", "FULL_RANGE", "[18,105]", 18.0, 105.0),
    ],
)
def test_nospace_comma_pair_3_digit_upper_bound_thousands_corruption_defect(
    param_type: str, disp_type: str, val_str: str, exp_min: float, exp_max: float
) -> None:
    """DEFECT (age/parser.py:831,1900,1977): _strip_thousands_commas turns '(10,100)' into '10100'!

    On '50 (10,100)' with INTER_QUARTILE_RANGE, parse_continuous_measure corrupts (10,100)
    into iqr_width_years = 10,100.0 years (parse_valid = True)!
    """
    c = ap.parse_continuous_measure(
        _cont_m("Age", param_type=param_type, dispersion_type=disp_type, unit="years", value=val_str)
    )
    assert c.parse_valid is True
    if disp_type == "INTER_QUARTILE_RANGE":
        assert c.iqr_low_years == pytest.approx(exp_min)
        assert c.iqr_high_years == pytest.approx(exp_max)
        assert c.iqr_width_years == pytest.approx(exp_max - exp_min)
    else:
        assert c.min_years == pytest.approx(exp_min)
        assert c.max_years == pytest.approx(exp_max)


def test_unbracketed_european_decimal_comma_in_spread_not_parsed_as_range_pair_defect() -> None:
    """DEFECT (age/parser.py:1890,1972): bare '12,50' (no parens/brackets, no space) parsed as range (12.0, 50.0)!"""
    c = ap.parse_continuous_measure(
        _cont_m(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="25.0",
            spread="12,50",
        )
    )
    assert c.iqr_low_years is None
    assert c.iqr_high_years is None


def test_duplicate_group_id_in_single_category_measurements_refused_defect() -> None:
    """DEFECT (age/parser.py:1445,2096): duplicate BG000 entries inside one category overwrite 30 with 40!"""
    m_cat_dup = {
        "title": "Age, Categorical",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "categories": [
                    {
                        "title": "18 to 64 years",
                        "measurements": [
                            {"groupId": "BG000", "value": "30"},
                            {"groupId": "BG000", "value": "40"},
                        ],
                    }
                ]
            }
        ],
    }
    p_cat = ap.parse_trial([m_cat_dup])
    assert ap.classify_age_reporting(p_cat) == "uninformative"
    assert p_cat.n_classified is None

    m_cont_dup = {
        "title": "Age, Continuous",
        "paramType": "MEAN",
        "dispersionType": "STANDARD_DEVIATION",
        "unitOfMeasure": "years",
        "classes": [
            {
                "categories": [
                    {
                        "measurements": [
                            {"groupId": "BG000", "value": "45.0", "spread": "5.0"},
                            {"groupId": "BG000", "value": "65.0", "spread": "5.0"},
                        ]
                    }
                ]
            }
        ],
    }
    p_cont = ap.parse_trial([m_cont_dup])
    assert ap.classify_age_reporting(p_cont) == "uninformative"
    assert p_cont.mean_years is None


# ===========================================================================
# 5. bool Subclassing int Across All Public Functions & Dict Pathways
# ===========================================================================


def test_age_range_bounds_rejects_bool_parameters_and_bracket_fields() -> None:
    b_valid = [ap.ParsedBracket("18-64 years", 18.0, 65.0, 10.0, "range_ge_le_int_plus_one_year")]
    with pytest.raises(ValueError, match="target_lo"):
        ar.age_range_bounds(b_valid, 0.0, True, 65.0)
    with pytest.raises(ValueError, match="target_hi"):
        ar.age_range_bounds(b_valid, 0.0, 0.0, True)
    with pytest.raises(ValueError, match="n_unknown"):
        ar.age_range_bounds(b_valid, True, 0.0, 65.0)
    with pytest.raises(TypeError, match="boolean"):
        ar.age_range_bounds([{"lo_years": True, "hi_years": 65.0, "count": 10.0}], 0.0, 0.0, 65.0)
    with pytest.raises(TypeError, match="boolean"):
        ar.age_range_bounds([{"lo_years": 18.0, "hi_years": 65.0, "count": True}], 0.0, 0.0, 65.0)


def test_compare_age_to_acs_rejects_bool_eligible_bounds() -> None:
    lvl = _acs_level("99020", [100.0] * 14)
    p = ap.parse_trial([_cat_m("Age", [("18 to 64 years", 50)])])
    with pytest.raises(ValueError, match="eligible_lo"):
        ar.compare_age_to_acs(p, lvl, "99020", sexes=("male",), eligible_lo=False, eligible_hi=65.0)
    with pytest.raises(ValueError, match="eligible_hi"):
        ar.compare_age_to_acs(p, lvl, "99020", sexes=("male",), eligible_lo=0.0, eligible_hi=True)


def test_build_age_row_and_raw_envelope_reject_bool_enrollment_defect() -> None:
    """DEFECT (age/table.py:315-319): select_raw_age_measures preserves enrollment=True as bool True!"""
    s = _study("NCT99060101", [_cat_m("Age", [("18 to 64 years", 25)])], enrollment=True)
    row = at.build_age_row(s, SNAP)
    assert row["enrollment"] is None
    assert row["enrollment_minus_parsed"] is None
    raw_env = at.select_raw_age_measures(s)
    assert raw_env["enrollment"] is None


def test_representation_dict_row_pathway_rejects_bool_numeric_fields_defect() -> None:
    """DEFECT (age/representation.py:285,289,339-350): float(True)->1.0 bypasses bool guard on dict rows!"""
    row_cat_bool: dict[str, Any] = {
        "age_report_status": "reported_categorical_only",
        "parse_ok": True,
        "is_participant_count": True,
        "n_total_parsed": 10,
        "n_unknown": True,
        "parsed_brackets": [{"label": "18-64 years", "lo_years": 18.0, "hi_years": 65.0, "count": 10.0}],
    }
    with pytest.raises((TypeError, ValueError)):
        ar.standard_age_band_bounds(row_cat_bool)

    row_cont_bool: dict[str, Any] = {
        "age_report_status": "reported_continuous_only",
        "parse_ok": True,
        "has_reported_continuous": True,
        "min_years": True,
        "max_years": False,
        "min_age": "18 Years",
        "max_age": "N/A",
    }
    aud = ar.audit_eligibility_vs_enrolled(row_cont_bool)
    assert aud["zero_enrolled_65_plus"] is False
    assert aud["enrolled_outside_eligibility_bounds"] is False


def test_raw_jsonl_writer_sanitizes_nan_inside_tuples_defect(tmp_path: Path) -> None:
    """DEFECT (age/table.py:243-255): _sanitize_json_value skips tuples containing NaN, crashing write_raw_age_jsonl!"""
    s = _study("NCT99060102", [])
    s["resultsSection"]["baselineCharacteristicsModule"]["populationDescription"] = (float("nan"), "text")
    env = at.select_raw_age_measures(s)
    out_p = tmp_path / "tuple_nan.jsonl.gz"
    assert at.write_raw_age_jsonl([env], out_p) == 1
    assert len(at.read_raw_age_jsonl(out_p)) == 1


# ===========================================================================
# 6. compare_age_to_acs Edge Cases: NaN/Inf/Negative ACS, Single-Sex, Zero Denom
# ===========================================================================


def test_compare_age_to_acs_pediatric_trial_on_older_adult_bands_zero_expected() -> None:
    """Pediatric [0, 18) trial evaluated on >=65 and >=75 bands where eligible ACS share is 0.0%."""
    lvl = _acs_level("99030", [200.0] * 14)
    # 90 classified in <18, 10 Unknown -> on >=65: floor=0.0%, ceiling=10.0% vs expected=0.0% -> unsettled!
    p_unk = ap.parse_trial([_cat_m("Age", [("< 18 years", 90), ("Unknown", 10)])])
    res_unk = ar.compare_age_to_acs(
        p_unk, lvl, "99030", sexes=("male", "female"), eligible_lo=0.0, eligible_hi=18.0
    )
    assert res_unk["available"] is True
    assert res_unk["bands"]["pediatric_under_18"]["expected_pct"] == pytest.approx(100.0)
    b65 = res_unk["bands"]["older_adults_65_plus"]
    assert b65["expected_pct"] == pytest.approx(0.0)
    assert b65["expected_moe90_pp"] == pytest.approx(0.0)
    assert b65["floor_ratio"] is None
    assert b65["ceiling_ratio"] is None
    assert b65["ratio_undefined"] == "reference_share_is_zero"
    assert b65["direction_settled"] is False
    assert b65["direction"] == "indeterminate_or_equal"
    assert json.dumps(res_unk, allow_nan=False)

    # Zero unknowns & zero >=65 -> floor=0.0%, ceiling=0.0% == expected=0.0% -> unsettled ('indeterminate_or_equal')
    p_exact = ap.parse_trial([_cat_m("Age", [("< 18 years", 100)])])
    res_exact = ar.compare_age_to_acs(
        p_exact, lvl, "99030", sexes=("male", "female"), eligible_lo=0.0, eligible_hi=18.0
    )
    assert res_exact["bands"]["older_adults_65_plus"]["direction_settled"] is False
    assert res_exact["bands"]["pediatric_under_18"]["direction_settled"] is False


def test_compare_age_to_acs_single_sex_male_vs_female_asymmetric_populations() -> None:
    male_14 = [100.0] * 11 + [50.0, 30.0, 20.0]     # adult [18,65): 700, >=65: 100 (12.5%)
    female_14 = [100.0] * 11 + [300.0, 200.0, 200.0]  # adult [18,65): 700, >=65: 700 (50.0%)
    lvl = _acs_level("99031", male_14, female_14)
    p = ap.parse_trial([_cat_m("Age", [("18 to 64 years", 75), (">= 65 years", 25)])])

    res_m = ar.compare_age_to_acs(p, lvl, "99031", sexes=("male",), eligible_lo=18.0, eligible_hi=None)
    res_f = ar.compare_age_to_acs(p, lvl, "99031", sexes=("female",), eligible_lo=18.0, eligible_hi=None)
    assert res_m["bands"]["older_adults_65_plus"]["expected_pct"] == pytest.approx(12.5)
    assert res_m["bands"]["older_adults_65_plus"]["direction"] == "above_expected"
    assert res_f["bands"]["older_adults_65_plus"]["expected_pct"] == pytest.approx(50.0)
    assert res_f["bands"]["older_adults_65_plus"]["direction"] == "below_expected"

    with pytest.raises(ValueError, match="sexes"):
        ar.compare_age_to_acs(p, lvl, "99031", sexes=(), eligible_lo=18.0, eligible_hi=None)
    with pytest.raises(ValueError, match="sexes"):
        ar.compare_age_to_acs(p, lvl, "99031", sexes=("invalid",), eligible_lo=18.0, eligible_hi=None)


def test_compare_age_to_acs_nan_and_inf_estimates_return_unavailable_cleanly() -> None:
    p = ap.parse_trial([_cat_m("Age", [("18 to 64 years", 60), (">= 65 years", 40)])])

    m_nan = [100.0] * 14
    m_nan[11] = float("nan")
    lvl_nan = _acs_level("99032", m_nan)
    r_nan = ar.compare_age_to_acs(p, lvl_nan, "99032", sexes=("male",), eligible_lo=18.0, eligible_hi=None)
    assert r_nan == {"available": False, "reason": "estimate_not_published", "geoid": "99032"}
    assert json.dumps(r_nan, allow_nan=False)

    m_inf = [100.0] * 14
    m_inf[5] = float("inf")
    lvl_inf = _acs_level("99033", m_inf)
    r_inf = ar.compare_age_to_acs(p, lvl_inf, "99033", sexes=("male",), eligible_lo=18.0, eligible_hi=None)
    assert r_inf == {"available": False, "reason": "estimate_not_published", "geoid": "99033"}


def test_compare_age_to_acs_negative_sentinel_cell_returns_unavailable_defect() -> None:
    """DEFECT (age/representation.py:710-736): negative ACS cell (-200 or sentinel) yields expected_pct = -25% & 125%!"""
    p = ap.parse_trial([_cat_m("Age", [("18 to 64 years", 60), (">= 65 years", 40)])])
    m_neg = [0.0] * 14
    m_neg[4] = 1000.0
    m_neg[11] = -200.0  # negative / unmasked sentinel cell while sum den = 800 > 0
    lvl_neg = _acs_level("99034", m_neg)
    res = ar.compare_age_to_acs(p, lvl_neg, "99034", sexes=("male",), eligible_lo=18.0, eligible_hi=None)
    assert res["available"] is False
    assert res["reason"] == "estimate_not_published"


# ===========================================================================
# 7. Additional Second-Order Checks Across Customized Sub-Categories,
#    Inline Unit Conflicts, Sub-Year Eligibility, Pigeonhole, & Priority Pins
# ===========================================================================


def test_customized_multicategory_explicit_total_subcategory_not_double_counted() -> None:
    """In customized class with Female (30), Male (40), and Total (70), Total is selected (not 140)."""
    m = {
        "title": "Age, Customized",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "title": "18 to 64 years",
                "categories": [
                    {"title": "Female", "measurements": [{"groupId": "BG000", "value": "30"}]},
                    {"title": "Male", "measurements": [{"groupId": "BG000", "value": "40"}]},
                    {"title": "Total", "measurements": [{"groupId": "BG000", "value": "70"}]},
                ],
            }
        ],
    }
    p = ap.parse_trial([m])
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.n_classified == 70.0


def test_customized_multicategory_ambiguous_or_duplicate_subcategories_refused() -> None:
    for sub_titles in [("Arm A", "Arm A"), ("Safety Set", "ITT Set"), ("", "Male")]:
        m = {
            "title": "Age, Customized",
            "paramType": "COUNT_OF_PARTICIPANTS",
            "unitOfMeasure": "Participants",
            "classes": [
                {
                    "title": "18 to 64 years",
                    "categories": [
                        {"title": sub_titles[0], "measurements": [{"groupId": "BG000", "value": "20"}]},
                        {"title": sub_titles[1], "measurements": [{"groupId": "BG000", "value": "30"}]},
                    ],
                }
            ],
        }
        p = ap.parse_trial([m])
        assert ap.classify_age_reporting(p) == "uninformative"
        assert p.uninformative_reason == "ambiguous_multicategory_class"


def test_continuous_inline_range_unit_conflict_with_unit_of_measure_refused() -> None:
    """unitOfMeasure='years' with spread='12 to 24 months' must refuse mixed units."""
    c = ap.parse_continuous_measure(
        _cont_m(
            "Age",
            param_type="MEDIAN",
            dispersion_type="FULL_RANGE",
            unit="years",
            value="18.0",
            spread="12 to 24 months",
        )
    )
    assert c.parse_valid is False
    assert c.flag_unrecognized_or_mixed_unit is True
    assert c.uninformative_reason == "unrecognized_or_mixed_unit"
    assert c.min_years is None
    assert c.max_years is None


def test_continuous_cross_measure_median_outside_iqr_invalidated() -> None:
    """Measure 1 MEDIAN=30.0 + Measure 2 IQR=[40.0, 60.0] must invalidate continuous stats."""
    m1 = _cont_m("Age", param_type="MEDIAN", dispersion_type="STANDARD_DEVIATION", value="30.0", spread="5.0")
    m2 = _cont_m(
        "Age",
        param_type="NUMBER",
        dispersion_type="INTER_QUARTILE_RANGE",
        value=None,
        lower="40.0",
        upper="60.0",
    )
    # Note: m2 alone without central has no usable stat unless central or full_range exists; give m2 central=50:
    m2_valid = _cont_m(
        "Age",
        param_type="GEOMETRIC_MEAN",
        dispersion_type="INTER_QUARTILE_RANGE",
        value="50.0",
        lower="40.0",
        upper="60.0",
    )
    p = ap.parse_trial([m1, m2_valid])
    assert p.flag_impossible_continuous_value is True
    assert p.has_reported_continuous is False
    assert p.median_years is None
    assert p.iqr_low_years is None
    assert p.iqr_high_years is None


def test_eligibility_audit_subyear_max_age_days_and_weeks_inclusivity() -> None:
    s_ok = _study(
        "NCT99060201",
        [_cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="days", value="14", lower="0", upper="27")],
        min_age="0 Days",
        max_age="27 Days",
        std_ages=["CHILD"],
    )
    assert ar.audit_eligibility_vs_enrolled(s_ok)["enrolled_outside_eligibility_bounds"] is False

    s_over = _study(
        "NCT99060202",
        [_cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="days", value="14", lower="0", upper="28")],
        min_age="0 Days",
        max_age="27 Days",
        std_ages=["CHILD"],
    )
    aud_over = ar.audit_eligibility_vs_enrolled(s_over)
    assert aud_over["enrolled_outside_eligibility_bounds"] is True
    assert "continuous_max_at_or_above_eligible_hi" in aud_over["outside_eligibility_reasons"]


def test_eligibility_audit_pigeonhole_below_eligible_lo_detected() -> None:
    s = _study(
        "NCT99060203",
        [_cont_m("Age", param_type="MEDIAN", dispersion_type="INTER_QUARTILE_RANGE", unit="years", value="22", lower="15", upper="30")],
        min_age="18 Years",
        max_age="N/A",
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["enrolled_outside_eligibility_bounds"] is True
    assert "pigeonhole_central_or_iqr_below_eligible_lo" in aud["outside_eligibility_reasons"]


def test_eligibility_audit_cross_modal_pediatric_and_senior_guards() -> None:
    den = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "300"}]}]
    s = _study(
        "NCT99060204",
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "0"), ("18 to 64 years", "100")],
                param_type="PERCENTAGE",
                unit="%",
            ),
            _cont_m(
                "Age, Continuous",
                param_type="MEDIAN",
                dispersion_type="FULL_RANGE",
                unit="years",
                value="35",
                lower="12",
                upper="60",
            ),
        ],
        min_age="0 Years",
        max_age="N/A",
        denoms=den,
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["zero_enrolled_pediatric"] is False
    assert aud["pediatric_representation_gap"] is False


def test_csv_round_trip_literal_double_quote_sentinel_collision_defect(tmp_path: Path) -> None:
    """DEFECT (age/table.py:661,683): _EMPTY_STR_CSV_SENTINEL = '\"\"' turns literal '\"\"' string into ''!"""
    r = at.parse_error_row("NCT99060205", 20, SNAP, error_detail='""')
    assert r["error_detail"] == '""'
    p_csv = tmp_path / "quote_sentinel.csv.gz"
    at.write_age_csv([r], p_csv)
    assert at.read_age_csv(p_csv)[0]["error_detail"] == '""'


def test_vocabulary_pin_and_uninformative_reason_priority_completeness() -> None:
    import re

    ap.validate_vocabulary_pin()
    parser_src = (_ROOT / "age" / "parser.py").read_text(encoding="utf-8")
    readme_src = (_ROOT / "age" / "README.md").read_text(encoding="utf-8")
    emitted = set(re.findall(r'uninformative_reason\s*=\s*"([a-z0-9_]+)"', parser_src))
    assert emitted.issubset(set(ap.UNINFORMATIVE_REASON_PRIORITY))
    for code in ap.UNINFORMATIVE_REASON_PRIORITY:
        assert f"`{code}`" in readme_src

