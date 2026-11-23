"""Round-3 clean-sweep verification tests for age/ after Round-2 fixes.

Covers 20 targeted checks across:
  - Thousands-grouped range pairs with and without spaces ('1,200 (1,000,10,000)',
    '(1,000,10,000)', '1,200 (1,000, 10,000)') and non-finite range endpoints
    ('(10, Inf)', '10 to NaN').
  - Bijective CSV '\\x1f' prefix-doubling round-trip across None, '', '\"\"',
    '\\x1f', '\\x1f\\x1f', '\\x1f\\x1f\\x1f', and prefixed strings.
  - Percentage rounding excess symmetry ('33.4%' * 3 = 100.2% on denom=500 -> 501,
    '66.7% + 33.3%', '16.7%' * 6 = 100.2%, '34%' * 3 on denom=50) vs integer-count
    'inconsistent_categorical_total' when Total=100 and brackets sum to 101.
  - Unambiguous 'minutes' vs bare 'min' unit guards, mixed-unit title with
    explicit per-bracket units, duplicate groupId guards in groups/denoms,
    active-band negative sentinel scoping in compare_age_to_acs, and README/pin parity.
"""

from __future__ import annotations

import json
import re
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
    param_type: str | None = "MEDIAN",
    dispersion_type: str | None = "FULL_RANGE",
    unit: str | None = "hours",
    value: Any = None,
    spread: Any = None,
    group_id: str = "BG000",
) -> dict[str, Any]:
    mm: dict[str, Any] = {"groupId": group_id}
    if value is not None:
        mm["value"] = str(value)
    if spread is not None:
        mm["spread"] = str(spread)
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


# ---------------------------------------------------------------------------
# 1. Thousands-grouped comma range pairs (spaced vs no-space) & non-finite pairs
# ---------------------------------------------------------------------------


def test_r3_spaced_thousands_comma_range_pair_1200_1000_10000() -> None:
    """'1,200 (1,000, 10,000)' with space after pair comma parses central, min, and max."""
    c = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="hours", value="1,200 (1,000, 10,000)")
    )
    assert c.parse_valid is True
    assert c.median_years == pytest.approx(1200.0 / 8760.0)
    assert c.min_years == pytest.approx(1000.0 / 8760.0)
    assert c.max_years == pytest.approx(10000.0 / 8760.0)


def test_r3_nospace_thousands_comma_range_pair_inline_1200_1000_10000_defect() -> None:
    """DEFECT (age/parser.py:2169-2179): '1,200 (1,000,10,000)' drops min_years/max_years to None!

    Because inner='1,000,10,000' has no space after ',' and inner.count(',') == 3 != 1,
    _parse_measurement_stat_fields fails to split ('1,000', '10,000') and loses FULL_RANGE!
    """
    c = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="hours", value="1,200 (1,000,10,000)")
    )
    assert c.parse_valid is True
    assert c.median_years == pytest.approx(1200.0 / 8760.0)
    assert c.min_years == pytest.approx(1000.0 / 8760.0)
    assert c.max_years == pytest.approx(10000.0 / 8760.0)


def test_r3_nospace_thousands_comma_range_pair_standalone_defect() -> None:
    """DEFECT (age/parser.py:2060-2068): standalone '(1,000,10,000)' & '[1,000,10,000]' fail as all_values_na!"""
    for raw_pair in ("(1,000,10,000)", "[1,000,10,000]"):
        c = ap.parse_continuous_measure(
            _cont_m("Age", param_type="NUMBER", dispersion_type="FULL_RANGE", unit="hours", value=raw_pair)
        )
        assert c.parse_valid is True
        assert c.min_years == pytest.approx(1000.0 / 8760.0)
        assert c.max_years == pytest.approx(10000.0 / 8760.0)


def test_r3_nospace_thousands_comma_iqr_1000_100000_not_merged_into_1_billion_defect() -> None:
    """DEFECT (age/parser.py:2173-2191): '5,000 (1,000,100,000)' strips all 3 commas into iqr_width=1,000,100,000!"""
    c = ap.parse_continuous_measure(
        _cont_m(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="minutes",
            value="5,000 (1,000,100,000)",
        )
    )
    assert c.parse_valid is True
    assert c.iqr_low_years == pytest.approx(1000.0 / 525600.0)
    assert c.iqr_high_years == pytest.approx(100000.0 / 525600.0)
    assert c.iqr_width_years == pytest.approx(99000.0 / 525600.0)


def test_r3_single_thousands_iqr_width_5000_1200_not_split_as_1_to_200_defect() -> None:
    """DEFECT (age/parser.py:2173-2178): '5,000 (1,200)' on INTER_QUARTILE_RANGE splits '1,200' into (1, 200)!

    When median=5,000 hours and IQR width='1,200' hours, inner.count(',')==1 with
    dt_norm=='INTER_QUARTILE_RANGE' splits '1,200' into lo=1.0, hi=200.0 and falsely
    flags impossible_continuous_value because 5000 > 200 (or on '150 (1,200)' fabricates Q1=1, Q3=200)!
    """
    c = ap.parse_continuous_measure(
        _cont_m(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="hours",
            value="5,000 (1,200)",
        )
    )
    # Must not split a single thousands-grouped width '1,200' into Q1=1, Q3=200 and falsely flag impossible!
    assert c.parse_valid is True
    assert c.flag_impossible_continuous_value is False
    assert c.iqr_width_years == pytest.approx(1200.0 / 8760.0)
    assert c.iqr_low_years is None
    assert c.iqr_high_years is None


def test_r3_nonfinite_endpoints_in_range_pairs_flagged_impossible() -> None:
    for bad_val, bad_sp in [("(10, Inf)", None), ("10 to NaN", None), ("50 (10, Infinity)", None), ("50", "(10, -Inf)")]:
        c = ap.parse_continuous_measure(
            _cont_m("Age", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="years", value=bad_val, spread=bad_sp)
        )
        assert c.parse_valid is False
        assert c.flag_impossible_continuous_value is True
        assert c.uninformative_reason == "impossible_continuous_value"


# ---------------------------------------------------------------------------
# 2. CSV '\x1f' prefix-doubling bijection
# ---------------------------------------------------------------------------


def test_r3_csv_prefix_doubling_bijection_all_escape_depths(tmp_path: Path) -> None:
    tricky_cases = [
        ("NCT99070001", None),
        ("NCT99070002", ""),
        ("NCT99070003", '""'),
        ("NCT99070004", "\x1f"),
        ("NCT99070005", "\x1f\x1f"),
        ("NCT99070006", "\x1f\x1f\x1f"),
        ("NCT99070007", "\x1fabc"),
        ("NCT99070008", "\x1f\x1fabc"),
        ("NCT99070009", 'quote "inside" and, comma'),
    ]
    rows = [
        at.parse_error_row(nct_id, 25, SNAP, min_age=s, max_age=s, error_detail=s)
        for nct_id, s in tricky_cases
    ]
    for ext in (".csv", ".csv.gz"):
        p_csv = tmp_path / f"bijection{ext}"
        assert at.write_age_csv(rows, p_csv) == len(rows)
        assert at.read_age_csv(p_csv) == rows


# ---------------------------------------------------------------------------
# 3. Percentage rounding excess vs integer-count 'inconsistent_categorical_total'
# ---------------------------------------------------------------------------


def test_r3_percentage_rounding_excess_33_4_x3_on_denom_500_passes() -> None:
    """33.4% + 33.4% + 33.4% = 100.2% on denom=500 -> 167*3 = 501 (normal +1 rounding excess) passes!"""
    den_500 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "500"}]}]
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "33.4"), ("18 to 64 years", "33.4"), (">= 65 years", "33.4")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_500,
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.n_total_parsed == 501.0
    assert p.flag_percentage_converted is True


def test_r3_percentage_rounding_66_7_plus_33_3_and_six_way_16_7_pass() -> None:
    den_300 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "300"}]}]
    p2 = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("18 to 64 years", "66.7"), (">= 65 years", "33.3")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_300,
            )
        ]
    )
    assert ap.classify_age_reporting(p2) == "reported_categorical_only"
    assert p2.n_total_parsed == 300.0

    p6 = ap.parse_trial(
        [
            _cat_m(
                "Age, Customized",
                [
                    ("0 to 17 years", "16.7"),
                    ("18 to 29 years", "16.7"),
                    ("30 to 39 years", "16.7"),
                    ("40 to 49 years", "16.7"),
                    ("50 to 64 years", "16.7"),
                    (">= 65 years", "16.7"),
                ],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_300,
            )
        ]
    )
    assert ap.classify_age_reporting(p6) == "reported_categorical_only"
    assert p6.n_total_parsed == 300.0


def test_r3_percentage_integer_rounding_34_x3_on_denom_50_passes_while_genuine_excess_fails() -> None:
    den_50 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "50"}]}]
    p_ok = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("< 18 years", "34"), ("18 to 64 years", "34"), (">= 65 years", "34")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_50,
            )
        ]
    )
    assert ap.classify_age_reporting(p_ok) == "reported_categorical_only"
    assert p_ok.n_total_parsed == 51.0

    p_bad = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("18 to 64 years", "60"), (">= 65 years", "60")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=den_50,
            )
        ]
    )
    assert ap.classify_age_reporting(p_bad) == "uninformative"
    assert p_bad.uninformative_reason == "percentage_sum_exceeds_100"
    assert p_bad.flag_percentage_converted is False


def test_r3_integer_count_inconsistent_categorical_total_exact_boundary() -> None:
    """Integer count table with Total=100 rejects 61+40=101 and 59+40=99, accepts 60+40=100."""
    den_100 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "100"}]}]
    p_exact = ap.parse_trial(
        [_cat_m("Age", [("18 to 64 years", 60), (">= 65 years", 40), ("Total", 100)], denoms=den_100)]
    )
    assert ap.classify_age_reporting(p_exact) == "reported_categorical_only"
    assert p_exact.n_total_parsed == 100.0

    p_over = ap.parse_trial(
        [_cat_m("Age", [("18 to 64 years", 61), (">= 65 years", 40), ("Total", 100)], denoms=den_100)]
    )
    assert ap.classify_age_reporting(p_over) == "uninformative"
    assert p_over.uninformative_reason == "inconsistent_categorical_total"

    p_under = ap.parse_trial(
        [_cat_m("Age", [("18 to 64 years", 59), (">= 65 years", 40), ("Total", 100)], denoms=den_100)]
    )
    assert ap.classify_age_reporting(p_under) == "uninformative"
    assert p_under.uninformative_reason == "incomplete_categorical_brackets"


def test_r3_conflicting_structural_total_and_denom_in_count_table_rejected() -> None:
    """If structural Total=90 while denom=100, no count sum can satisfy both -> rejected cleanly."""
    den_100 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "100"}]}]
    for s_val in (90, 100):
        p = ap.parse_trial(
            [_cat_m("Age", [("18 to 64 years", s_val), ("Total", 90)], denoms=den_100)]
        )
        assert ap.classify_age_reporting(p) == "uninformative"
        assert p.uninformative_reason in ("incomplete_categorical_brackets", "inconsistent_categorical_total")


# ---------------------------------------------------------------------------
# 4. Unit guards ('minutes' vs 'min', mixed title with explicit per-row units)
# ---------------------------------------------------------------------------


def test_r3_minutes_accepted_while_bare_min_rejected_in_continuous() -> None:
    c_ok = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", unit="minutes", value="60", spread="15")
    )
    assert c_ok.parse_valid is True
    assert c_ok.mean_years == pytest.approx(60.0 / 525600.0)

    c_bad = ap.parse_continuous_measure(
        _cont_m("Age", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", unit="min", value="60", spread="15")
    )
    assert c_bad.parse_valid is False
    assert c_bad.flag_unrecognized_or_mixed_unit is True


def test_r3_mixed_unit_title_with_explicit_per_bracket_units_accepted() -> None:
    """'Age (Days or Months)' succeeds when every bracket explicitly specifies its own unit."""
    p = ap.parse_trial(
        [
            _cat_m(
                "Age (Days or Months)",
                [("0 to 27 days", 12), ("28 days to 23 months", 28)],
                unit="Participants",
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.n_total_parsed == 40.0
    assert p.brackets_tile_cleanly is True


# ---------------------------------------------------------------------------
# 5. Duplicate groupId guards in baseline_groups and denoms
# ---------------------------------------------------------------------------


def test_r3_duplicate_group_id_in_baseline_groups_even_with_last_total_refused() -> None:
    groups = [
        {"id": "BG000", "title": "Arm A"},
        {"id": "BG000", "title": "Total"},
    ]
    gsel = ap.select_overall_group(groups)
    assert gsel.strategy == "ambiguous_groups"
    assert gsel.flag_ambiguous_groups is True


def test_r3_conflicting_duplicate_group_id_in_denoms_refused_for_percentages() -> None:
    bad_denoms = [
        {
            "units": "Participants",
            "counts": [
                {"groupId": "BG000", "value": "100"},
                {"groupId": "BG000", "value": "200"},
            ],
        }
    ]
    p = ap.parse_trial(
        [
            _cat_m(
                "Age, Categorical",
                [("18 to 64 years", "60"), (">= 65 years", "40")],
                param_type="PERCENTAGE",
                unit="%",
                denoms=bad_denoms,
            )
        ]
    )
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "percentage_without_denominator"


# ---------------------------------------------------------------------------
# 6. ACS active-band sentinel scoping, raw re-parse parity, & README/pin check
# ---------------------------------------------------------------------------


def test_r3_acs_negative_cell_in_zero_weight_ineligible_band_does_not_break_adult_trial() -> None:
    edges: list[tuple[float, float | None]] = [
        (0.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 18.0),
        (18.0, 20.0), (20.0, 25.0), (25.0, 30.0), (30.0, 35.0),
        (35.0, 45.0), (45.0, 55.0), (55.0, 65.0),
        (65.0, 75.0), (75.0, 85.0), (85.0, None),
    ]
    est = np.zeros((1, len(acs.TABLE_ORDER), 2, len(edges)), dtype=float)
    moe = np.zeros_like(est)
    est[0, 0, :, 0] = -555555555.0  # negative in [0, 5), which has weight 0 for >=18 adult trial!
    est[0, 0, :, 4:11] = 100.0      # 700 per sex in [18, 65)
    est[0, 0, :, 11:14] = 100.0     # 300 per sex in [65, None)
    lvl = acs.Level(["99070"], ["Adult Active Only"], edges, est, moe)

    p = ap.parse_trial([_cat_m("Age", [("18 to 64 years", 70), (">= 65 years", 30)])])
    res = ar.compare_age_to_acs(p, lvl, "99070", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None)
    assert res["available"] is True
    assert res["bands"]["older_adults_65_plus"]["expected_pct"] == pytest.approx(30.0)
    assert json.dumps(res, allow_nan=False)


def test_r3_raw_reparse_parity_on_inconsistent_and_incomplete_states(tmp_path: Path) -> None:
    den_50 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "50"}]}]
    s_over = {
        "protocolSection": {"identificationModule": {"nctId": "NCT99070101"}},
        "resultsSection": {
            "baselineCharacteristicsModule": {
                "denoms": den_50,
                "measures": [_cat_m("Age", [("18 to 64 years", 60), ("Total", 50)])],
            }
        },
    }
    direct = at.build_age_row(s_over, SNAP)
    assert direct["uninformative_reason"] == "inconsistent_categorical_total"
    raw_p = tmp_path / "raw_r3.jsonl.gz"
    at.write_raw_age_jsonl([at.select_raw_age_measures(s_over)], raw_p)
    rebuilt = at.build_age_row_from_raw(at.read_raw_age_jsonl(raw_p)[0], SNAP)
    assert rebuilt == direct


def test_r3_structural_checks_pass_across_new_uninformative_reasons() -> None:
    den_50 = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "50"}]}]
    rows = [
        at.build_age_row(
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99070201"}},
                "resultsSection": {
                    "baselineCharacteristicsModule": {
                        "denoms": den_50,
                        "measures": [_cat_m("Age", [("18-64 years", 30)])],
                    }
                },
            },
            SNAP,
        ),
        at.build_age_row(
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99070202"}},
                "resultsSection": {
                    "baselineCharacteristicsModule": {
                        "denoms": den_50,
                        "measures": [_cat_m("Age", [("18-64 years", 80)])],
                    }
                },
            },
            SNAP,
        ),
    ]
    assert rows[0]["uninformative_reason"] == "incomplete_categorical_brackets"
    assert rows[1]["uninformative_reason"] == "inconsistent_categorical_total"
    assert at.all_pass(at.structural_checks(rows))


def test_r3_readme_and_vocabulary_pin_exact_sync() -> None:
    ap.validate_vocabulary_pin()
    parser_src = (_ROOT / "age" / "parser.py").read_text(encoding="utf-8")
    readme_src = (_ROOT / "age" / "README.md").read_text(encoding="utf-8")
    emitted = set(re.findall(r'uninformative_reason\s*=\s*"([a-z0-9_]+)"', parser_src))
    assert emitted.issubset(set(ap.UNINFORMATIVE_REASON_PRIORITY))
    for reason in ap.UNINFORMATIVE_REASON_PRIORITY:
        assert f"`{reason}`" in readme_src
