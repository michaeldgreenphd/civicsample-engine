"""Unit tests for catchments.bounds: convex-hull site-allocation bounds, unresolved-site
widening, covariance-deduplicated equal-facility 90% MOEs, distinct-area pooled resident
proxies, and Denominator A/B joint settlement checks."""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from catchments import bounds
from census import acs, benchmark as census_benchmark


COARSE_SPANS = [
    ("a", "Under 18 years"),
    ("b", "18 to 64 years"),
    ("c", "65 years and over"),
]
FINE_SPANS = [
    ("a", "Under 18 years"),
    ("b1", "18 to 39 years"),
    ("b2", "40 to 64 years"),
    ("c", "65 years and over"),
]


def _make_variables(spans: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    out = {"T_001E": {"label": "Estimate!!Total:"}}
    idx = 2
    for sex in ("Male", "Female"):
        out[f"T_{idx:03d}E"] = {"label": f"Estimate!!Total:!!{sex}:"}
        idx += 1
        for _, label in spans:
            out[f"T_{idx:03d}E"] = {"label": f"Estimate!!Total:!!{sex}:!!{label}"}
            out[f"T_{idx:03d}M"] = {"label": f"Margin of Error!!Total:!!{sex}:!!{label}"}
            idx += 1
    return out


def _build_multi_area_cbsa_level(tmp_path: Path) -> acs.Level:
    """Three synthetic CBSAs on real census.acs.load arrays:
    - 14460 (Area A, 1000 adults 18-64): 70% white, 10% black, 15% hispanic
    - 26420 (Area B, 4000 adults 18-64): 30% white, 40% black, 45% hispanic
    - 99999 (Zero-eligible campus area): 0 adults 18-64
    """
    areas = [
        (
            "14460",
            "Boston Metro",
            [100, 200, 300, 100, 100, 200, 300, 100],  # 500M + 500F = 1000 aged 18-64
            {
                "white": [50, 350, 80, 50, 350, 80],
                "black_african_american": [10, 50, 10, 10, 50, 10],
                "asian": [20, 100, 10, 20, 100, 10],
                "hispanic_latino": [15, 75, 5, 15, 75, 5],
            },
            4.0,
        ),
        (
            "26420",
            "Houston Metro",
            [200, 800, 1200, 200, 200, 800, 1200, 200],  # 2000M + 2000F = 4000 aged 18-64
            {
                "white": [60, 600, 150, 60, 600, 150],
                "black_african_american": [80, 800, 40, 80, 800, 40],
                "asian": [40, 400, 10, 40, 400, 10],
                "hispanic_latino": [90, 900, 60, 90, 900, 60],
            },
            10.0,
        ),
        (
            "99999",
            "Campus Zero Adult Area",
            [50, 0, 0, 0, 50, 0, 0, 0],  # 0 aged 18+
            {},
            1.0,
        ),
    ]
    geo_col = acs.LEVELS["cbsa"].split(":")[0]
    for cat, table in acs.TABLES.items():
        spans = FINE_SPANS if cat == "total" else COARSE_SPANS
        variables = _make_variables(spans)
        cells = [k[:-1] for k in variables if k.endswith("M")]
        header = ["NAME"] + [c + "E" for c in cells] + [c + "M" for c in cells] + [geo_col]
        rows = [header]
        for geoid, name, tot_fine, by_cat, moe_val in areas:
            vals = tot_fine if cat == "total" else by_cat.get(cat, [0.0] * 6)
            moes = [moe_val] * len(cells)
            rows.append([name] + [str(v) for v in vals] + [str(m) for m in moes] + [geoid])
        with gzip.open(tmp_path / f"acs5_2024_{table}_cbsa.json.gz", "wt") as fh:
            json.dump({"variables": variables, "rows": rows}, fh)
    return acs.load(2024, "cbsa", tmp_path)


# --- Site-allocation convex-hull bounds -----------------------------------------------------


def test_convex_hull_contains_every_simplex_allocation_when_all_sites_resolve() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"white": 72.5, "black": 8.0}, "moe90_pp": {"white": 1.2, "black": 0.5}},
        "26420": {"available": True, "pct": {"white": 31.0, "black": 24.5}, "moe90_pp": {"white": 0.9, "black": 0.8}},
        "12060": {"available": True, "pct": {"white": 45.0, "black": 38.2}, "moe90_pp": {"white": 1.1, "black": 1.0}},
    }
    counts = {"14460": 1, "26420": 2, "12060": 1}
    b = bounds.site_allocation_bounds(estimates, counts, n_us_facilities=4)
    assert b["available"] is True and b["bounds_unconditional"] is True
    assert b["resolved_min_pct"] == {"white": 31.0, "black": 8.0}
    assert b["resolved_max_pct"] == {"white": 72.5, "black": 38.2}
    assert b["min_geoid"] == {"white": "26420", "black": "14460"}
    assert b["max_geoid"] == {"white": "14460", "black": "12060"}
    assert b["unconditional_min_pct"] == b["resolved_min_pct"]
    assert b["unconditional_max_pct"] == b["resolved_max_pct"]

    # Every non-negative 4-facility enrollment allocation lies inside [min, max]
    site_white = np.array([72.5, 31.0, 31.0, 45.0])
    rng = np.random.default_rng(42)
    for _ in range(200):
        w = rng.dirichlet([0.5, 0.5, 0.5, 0.5])
        expected_w = float((w * site_white).sum())
        assert b["unconditional_min_pct"]["white"] - 1e-9 <= expected_w <= b["unconditional_max_pct"]["white"] + 1e-9


def test_min_and_max_geoid_break_ties_deterministically_by_geoid() -> None:
    estimates = {
        "36": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}},
        "06": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}},
    }
    b = bounds.site_allocation_bounds(estimates, {"36": 1, "06": 1}, n_us_facilities=2)
    assert b["min_geoid"]["white"] == "06"
    assert b["max_geoid"]["white"] == "06"
    assert b["by_category"]["white"]["resolved_span_pp"] == 0.0


def test_unresolved_us_site_widens_unconditional_bounds_to_0_100() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"white": 70.0}, "moe90_pp": {"white": 1.0}},
        "26420": {"available": True, "pct": {"white": 55.0}, "moe90_pp": {"white": 1.0}},
    }
    # 3 US facilities total, only 2 resolved to CBSAs (1 rural site outside any CBSA)
    b = bounds.site_allocation_bounds(estimates, {"14460": 1, "26420": 1}, n_us_facilities=3)
    assert b["available"] is True
    assert b["bounds_unconditional"] is False
    assert b["n_unresolved_or_unavailable"] == 1
    assert b["resolved_min_pct"]["white"] == 55.0
    assert b["resolved_max_pct"]["white"] == 70.0
    assert b["unconditional_min_pct"]["white"] == 0.0
    assert b["unconditional_max_pct"]["white"] == 100.0


def test_unavailable_acs_estimate_at_resolved_site_also_sets_bounds_unconditional_false() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"white": 70.0}, "moe90_pp": {"white": 1.0}},
        "99999": {"available": False, "reason": "no_eligible_residents_estimated"},
    }
    # Both US facilities resolved geographically, but 99999 has 0 eligible residents in ACS
    b = bounds.site_allocation_bounds(estimates, {"14460": 2, "99999": 1}, n_us_facilities=3)
    assert b["available"] is True
    assert b["n_geo_resolved_facilities"] == 3
    assert b["n_resolved_with_acs"] == 2
    assert b["n_unresolved_or_unavailable"] == 1
    assert b["bounds_unconditional"] is False
    assert b["unconditional_min_pct"]["white"] == 0.0
    assert b["unconditional_max_pct"]["white"] == 100.0
    assert b["unavailable_resolved_geoids"] == {"99999": "no_eligible_residents_estimated"}


def test_site_allocation_bounds_handles_zero_valid_areas_and_rejects_bad_facility_totals() -> None:
    none_avail = bounds.site_allocation_bounds(
        {"99999": {"available": False, "reason": "no_eligible_residents_estimated"}},
        {"99999": 2},
        n_us_facilities=2,
    )
    assert none_avail["available"] is False
    assert none_avail["reason"] == "no_eligible_residents_estimated"
    assert none_avail["bounds_unconditional"] is False

    with pytest.raises(ValueError, match="cannot be negative or less than"):
        bounds.site_allocation_bounds({}, {"14460": 3}, n_us_facilities=2)


# --- Equal-facility proxy and mandatory MOE covariance deduplication ------------------------


def test_same_geoid_clinics_preserve_exact_single_area_moe_never_shrinking_by_sqrt_r() -> None:
    houston = {
        "26420": {
            "available": True,
            "pct": {"white": 30.0, "black_african_american": 40.0},
            "moe90_pp": {"white": 2.45, "black_african_american": 1.80},
        }
    }
    # 4 clinics in the exact same Houston CBSA share 1 ACS estimate (correlation = 1)
    proxy_4 = bounds.equal_facility_proxy(houston, {"26420": 4})
    proxy_1 = bounds.equal_facility_proxy(houston, {"26420": 1})
    assert proxy_4["pct"] == {"white": 30.0, "black_african_american": 40.0}
    assert proxy_4["moe90_pp"] == {"white": 2.45, "black_african_american": 1.80}
    assert proxy_4["moe90_pp"] == proxy_1["moe90_pp"]
    assert proxy_4["weights_by_geoid"] == {"26420": 1.0}


def test_equal_facility_proxy_deduplicates_weights_by_unique_geoid_before_moe_propagation() -> None:
    estimates = {
        "26420": {"available": True, "pct": {"white": 60.0}, "moe90_pp": {"white": 4.0}},
        "14460": {"available": True, "pct": {"white": 20.0}, "moe90_pp": {"white": 4.0}},
    }
    # 3 clinics in 26420 (W = 0.75) + 1 clinic in 14460 (W = 0.25)
    proxy = bounds.equal_facility_proxy(estimates, {"26420": 3, "14460": 1})
    assert proxy["pct"]["white"] == pytest.approx(50.0)
    # Correct covariance-deduplicated MOE: sqrt((0.75 * 4)^2 + (0.25 * 4)^2) = sqrt(9 + 1) = sqrt(10)
    expected_moe = round(math.sqrt(10.0), 3)
    # Naive wrong independent-clinic MOE would be sqrt(3*(0.25*4)^2 + (0.25*4)^2) = sqrt(4) = 2.0
    assert proxy["moe90_pp"]["white"] == expected_moe
    assert proxy["moe90_pp"]["white"] > 2.0


def test_equal_facility_proxy_propagates_none_when_any_contributing_area_lacks_moe() -> None:
    estimates = {
        "26420": {"available": True, "pct": {"white": 60.0, "asian": 10.0}, "moe90_pp": {"white": 2.0, "asian": None}},
        "14460": {"available": True, "pct": {"white": 20.0, "asian": 30.0}, "moe90_pp": {"white": 2.0, "asian": 1.5}},
    }
    proxy = bounds.equal_facility_proxy(estimates, {"26420": 1, "14460": 1})
    assert proxy["moe90_pp"]["white"] == round(math.sqrt(0.5**2 * 4 + 0.5**2 * 4), 3)
    assert proxy["moe90_pp"]["asian"] is None


def test_equal_facility_proxy_preserves_ethnicity_complement_sum_to_100() -> None:
    estimates = {
        "01": {"available": True, "pct": {"hispanic_latino": 33.333, "not_hispanic_latino": 66.667}, "moe90_pp": {"hispanic_latino": 1.2, "not_hispanic_latino": 1.2}},
        "02": {"available": True, "pct": {"hispanic_latino": 10.001, "not_hispanic_latino": 89.999}, "moe90_pp": {"hispanic_latino": 0.8, "not_hispanic_latino": 0.8}},
        "04": {"available": True, "pct": {"hispanic_latino": 20.001, "not_hispanic_latino": 79.999}, "moe90_pp": {"hispanic_latino": 1.0, "not_hispanic_latino": 1.0}},
    }
    proxy = bounds.equal_facility_proxy(estimates, {"01": 1, "02": 1, "04": 1})
    assert round(proxy["pct"]["hispanic_latino"] + proxy["pct"]["not_hispanic_latino"], 3) == 100.0
    assert proxy["moe90_pp"]["hispanic_latino"] == proxy["moe90_pp"]["not_hispanic_latino"]


# --- Distinct-area resident proxy -----------------------------------------------------------


def test_distinct_area_resident_proxy_counts_each_geoid_once_and_matches_hand_calculation(
    tmp_path: Path,
) -> None:
    cbsa_level = _build_multi_area_cbsa_level(tmp_path)
    # Pass 26420 three times and 14460 once: each unique CBSA is counted ONCE!
    res = bounds.distinct_area_resident_proxy(
        cbsa_level, ["26420", "26420", "26420", "14460"], "race", ("male", "female"), 18.0, 65.0
    )
    assert res["available"] is True
    assert res["n_distinct_areas"] == 2
    assert res["geoids"] == ["14460", "26420"]
    # Area 14460 has 1000 adults (700 white); Area 26420 has 4000 adults (1200 white)
    # Combined: 5000 eligible residents, 1900 white -> 38.0%
    assert res["eligible_residents"] == 5000
    assert res["pct"]["white"] == pytest.approx(38.0)
    # Compare with equal-facility proxy for 3 Houston + 1 Boston: 0.75 * 30 + 0.25 * 70 = 40.0%
    e_14460 = census_benchmark.expected(cbsa_level, "14460", "race", ("male", "female"), 18.0, 65.0)
    e_26420 = census_benchmark.expected(cbsa_level, "26420", "race", ("male", "female"), 18.0, 65.0)
    eq = bounds.equal_facility_proxy({"14460": e_14460, "26420": e_26420}, {"14460": 1, "26420": 3})
    assert eq["pct"]["white"] == pytest.approx(40.0)
    # Both proxies sit inside [resolved_min_pct=30.0, resolved_max_pct=70.0]
    assert 30.0 <= res["pct"]["white"] <= 70.0
    assert 30.0 <= eq["pct"]["white"] <= 70.0


def test_distinct_area_resident_proxy_single_sex_and_missing_geoid_guards(tmp_path: Path) -> None:
    cbsa_level = _build_multi_area_cbsa_level(tmp_path)
    assert bounds.distinct_area_resident_proxy(
        cbsa_level, ["14460", "26420"], "sex", ("female",), 18.0, 65.0
    ) == {"available": False, "reason": "single_sex_by_eligibility"}
    assert bounds.distinct_area_resident_proxy(
        cbsa_level, [], "race", ("male", "female"), 18.0, 65.0
    ) == {"available": False, "reason": "no_resolved_geoids"}
    assert bounds.distinct_area_resident_proxy(
        cbsa_level, ["99999"], "race", ("male", "female"), 18.0, 65.0
    ) == {"available": False, "reason": "no_eligible_residents_estimated"}


# --- Joint settlement and critical unresolved-site invariants -------------------------------


def test_joint_settlement_distinguishes_site_only_settled_from_joint_direction_settled() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"black": 15.0, "white": 70.0}, "moe90_pp": {"black": 1.0, "white": 1.0}},
        "26420": {"available": True, "pct": {"black": 30.0, "white": 45.0}, "moe90_pp": {"black": 1.0, "white": 1.0}},
    }
    b = bounds.site_allocation_bounds(estimates, {"14460": 1, "26420": 1}, n_us_facilities=2)
    eq = bounds.equal_facility_proxy(estimates, {"14460": 1, "26420": 1})

    # Black observed floor = 10%, unknown = 10% -> ceiling = 20%.
    # Envelope is [15%, 30%]: floor (10%) < 15% so site_only_settled is True,
    # BUT ceiling (20%) >= 15% so joint_direction_settled is False!
    # White observed floor = 80%, unknown = 10% -> floor (80%) > 70% so BOTH settled positive!
    comp = bounds.compare_with_catchment_bounds(
        {"black": 10.0, "white": 80.0},
        unknown_pct=10.0,
        bounds=b,
        equal_proxy=eq,
        observed_pct_of_classified={"black": 11.111, "white": 88.889},
    )
    assert comp["site_only_settled"] == {"black": True, "white": True}
    assert comp["site_weight_settled"] == {"black": True, "white": True}
    assert comp["joint_direction_settled"] == {"black": False, "white": True}
    assert comp["joint_direction"] == {"black": "unsettled", "white": "positive"}
    assert comp["joint_diff_pp_min"]["black"] == -20.0   # 10 - 30
    assert comp["joint_diff_pp_max"]["black"] == 5.0     # 20 - 15
    assert comp["joint_diff_pp_min"]["white"] == 10.0    # 80 - 70
    assert comp["joint_diff_pp_max"]["white"] == 45.0    # 90 - 45


def test_joint_direction_settled_negative_when_ceiling_is_strictly_below_envelope_min() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"asian": 18.0}, "moe90_pp": {"asian": 1.0}},
        "26420": {"available": True, "pct": {"asian": 28.0}, "moe90_pp": {"asian": 1.0}},
    }
    b = bounds.site_allocation_bounds(estimates, {"14460": 1, "26420": 1}, n_us_facilities=2)
    # Floor = 5%, unknown = 8% -> ceiling = 13% < min (18%) -> settled negative!
    comp = bounds.compare_with_catchment_bounds({"asian": 5.0}, unknown_pct=8.0, bounds=b)
    assert comp["joint_direction_settled"]["asian"] is True
    assert comp["joint_direction"]["asian"] == "negative"
    assert comp["joint_diff_pp_min"]["asian"] == -23.0
    assert comp["joint_diff_pp_max"]["asian"] == -5.0


@pytest.mark.parametrize(
    "obs_floor,unk",
    [
        (0.0, 0.0),
        (2.0, 0.0),
        (50.0, 10.0),
        (99.5, 0.5),
        (100.0, 0.0),
    ],
)
def test_critical_invariant_settled_flags_never_true_when_bounds_unconditional_is_false(
    obs_floor: float, unk: float
) -> None:
    estimates = {
        "14460": {"available": True, "pct": {"white": 40.0}, "moe90_pp": {"white": 1.0}},
        "26420": {"available": True, "pct": {"white": 60.0}, "moe90_pp": {"white": 1.0}},
    }
    # 3 US sites, only 2 resolved -> bounds_unconditional is False -> unconditional [0, 100]
    b = bounds.site_allocation_bounds(estimates, {"14460": 1, "26420": 1}, n_us_facilities=3)
    assert b["bounds_unconditional"] is False
    comp = bounds.compare_with_catchment_bounds(
        {"white": obs_floor},
        unknown_pct=unk,
        bounds=b,
        observed_pct_of_classified={"white": obs_floor},
    )
    assert comp["site_only_settled"]["white"] is False
    assert comp["site_weight_settled"]["white"] is False
    assert comp["joint_direction_settled"]["white"] is False
    assert comp["joint_direction"]["white"] == "unsettled"
    assert comp["of_classified"]["by_category"]["white"]["site_weight_settled"] is False


def test_denominator_b_omitted_with_reason_for_single_ethnicity_category_table() -> None:
    estimates = {
        "14460": {
            "available": True,
            "pct": {"hispanic_latino": 15.0, "not_hispanic_latino": 85.0},
            "moe90_pp": {"hispanic_latino": 1.0, "not_hispanic_latino": 1.0},
        }
    }
    b = bounds.site_allocation_bounds(estimates, {"14460": 1}, n_us_facilities=1)
    comp = bounds.compare_with_catchment_bounds(
        {"hispanic_latino": 10.0, "not_hispanic_latino": 0.0},
        unknown_pct=90.0,
        bounds=b,
        observed_pct_of_classified=None,
        classified_share_reason="table_reports_one_ethnicity_category_only",
    )
    assert comp["of_classified"] is None
    assert comp["classified_share_reason"] == "table_reports_one_ethnicity_category_only"


def test_zero_reference_share_records_undefined_ratio_never_infinity() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"nhpi": 0.0, "white": 50.0}, "moe90_pp": {"nhpi": 0.1, "white": 1.0}}
    }
    b = bounds.site_allocation_bounds(estimates, {"14460": 1}, n_us_facilities=1)
    eq = bounds.equal_facility_proxy(estimates, {"14460": 1})
    comp = bounds.compare_with_catchment_bounds(
        {"nhpi": 2.0, "white": 40.0},
        unknown_pct=0.0,
        bounds=b,
        equal_proxy=eq,
        observed_pct_of_classified={"nhpi": 2.0, "white": 40.0},
    )
    assert comp["equal_facility_proxy"]["ratio"]["nhpi"] is None
    assert comp["equal_facility_proxy"]["ratio_undefined"] == {"nhpi": "reference_share_is_zero"}
    assert comp["of_classified"]["equal_facility_proxy"]["ratio"]["nhpi"] is None


def test_exact_boundary_equality_is_not_marked_settled() -> None:
    """At exact equality (obs_floor == max or obs_ceiling == min), diff is 0.0, so direction is not strictly settled."""
    estimates = {
        "14460": {"available": True, "pct": {"white": 40.0}, "moe90_pp": {"white": 1.0}},
        "26420": {"available": True, "pct": {"white": 60.0}, "moe90_pp": {"white": 1.0}},
    }
    b = bounds.site_allocation_bounds(estimates, {"14460": 1, "26420": 1}, n_us_facilities=2)

    at_max = bounds.compare_with_catchment_bounds({"white": 60.0}, unknown_pct=0.0, bounds=b)
    assert at_max["site_only_settled"]["white"] is False
    assert at_max["joint_direction_settled"]["white"] is False
    assert at_max["joint_diff_pp_min"]["white"] == 0.0

    at_min_ceil = bounds.compare_with_catchment_bounds({"white": 30.0}, unknown_pct=10.0, bounds=b)
    assert at_min_ceil["site_only_settled"]["white"] is True   # floor 30 < 40
    assert at_min_ceil["joint_direction_settled"]["white"] is False  # ceiling 40 == 40
    assert at_min_ceil["joint_diff_pp_max"]["white"] == 0.0


def test_distinct_area_resident_proxy_ratio_fallback_and_skipped_geoid_provenance(
    tmp_path: Path,
) -> None:
    # Construct a level where numerator MOE exceeds p * denominator MOE Is False -> negative radicand
    edges = [(0.0, 18.0), (18.0, 65.0), (65.0, None)]
    n_tables = len(acs.TABLE_ORDER)
    est = np.zeros((2, n_tables, 2, 3))
    moe = np.zeros((2, n_tables, 2, 3))
    t_tot = acs.TABLE_ORDER.index("total")
    t_wh = acs.TABLE_ORDER.index("white")
    # Area '10': 500 total (moe 100), 450 white (moe 10) -> num_moe^2 (100) < p^2 * den_moe^2 (0.81 * 10000)
    est[0, t_tot, :, 1] = 250.0
    moe[0, t_tot, :, 1] = 100.0
    est[0, t_wh, :, 1] = 225.0
    moe[0, t_wh, :, 1] = 10.0
    # Area '20': same structure
    est[1, t_tot, :, 1] = 250.0
    moe[1, t_tot, :, 1] = 100.0
    est[1, t_wh, :, 1] = 225.0
    moe[1, t_wh, :, 1] = 10.0

    lvl = acs.Level(["10", "20"], ["Area 10", "Area 20"], edges, est, moe)
    res = bounds.distinct_area_resident_proxy(
        lvl, ["10", "20", "88888"], "race", ("male", "female"), 18.0, 65.0
    )
    assert res["available"] is True
    assert res["n_distinct_areas"] == 2
    assert res["skipped_geoids"] == {"88888": "area_not_in_this_window_file"}
    assert res["pct"]["white"] == pytest.approx(90.0)
    # Ratio fallback: num=900, num_moe=sqrt(400)=20; den=1000, den_moe=sqrt(40000)=200
    # under = 20^2 - 0.81 * 200^2 < 0 -> uses sqrt(20^2 + 0.81 * 200^2) / 1000
    expected_moe = round(100.0 * math.sqrt(400.0 + 0.81 * 40000.0) / 1000.0, 3)
    assert res["moe90_pp"]["white"] == expected_moe


def test_bounds_and_proxy_error_handling_for_invalid_inputs(tmp_path: Path) -> None:
    cbsa_level = _build_multi_area_cbsa_level(tmp_path)
    assert bounds.equal_facility_proxy({}, {"14460": 2}) == {
        "available": False,
        "reason": "no_resolved_areas_with_acs_estimates",
    }
    with pytest.raises(ValueError, match="unsupported dimension"):
        bounds.distinct_area_resident_proxy(
            cbsa_level, ["14460"], "income", ("male", "female"), 18.0, 65.0
        )
    with pytest.raises(ValueError, match="available bounds"):
        bounds.compare_with_catchment_bounds({"white": 50.0}, 0.0, {"available": False})


def test_equal_facility_proxy_handles_reversed_ethnicity_category_order() -> None:
    estimates = {
        "01": {"available": True, "pct": {"hispanic_latino": 25.0, "not_hispanic_latino": 75.0}, "moe90_pp": {"hispanic_latino": 1.0, "not_hispanic_latino": 1.0}},
        "02": {"available": True, "pct": {"hispanic_latino": 55.0, "not_hispanic_latino": 45.0}, "moe90_pp": {"hispanic_latino": 2.0, "not_hispanic_latino": 2.0}},
    }
    proxy = bounds.equal_facility_proxy(
        estimates, {"01": 1, "02": 1}, categories=("not_hispanic_latino", "hispanic_latino")
    )
    assert proxy["pct"] == {"not_hispanic_latino": 60.0, "hispanic_latino": 40.0}

