"""Round-3 clean-sweep adversarial verification tests for ``catchments/``.

Specifically stresses all six Round-2 fixes in ``catchments/footprint.py``,
``catchments/bounds.py``, and ``catchments/benchmark.py`` without modifying production
code or Round-1 / Round-2 test files.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import numpy as np
import pytest

from catchments import benchmark as ms_benchmark, bounds, footprint
from census import acs, geography


EDGES_3: list[tuple[float, float | None]] = [
    (0.0, 18.0),
    (18.0, 65.0),
    (65.0, None),
]


def _make_level(
    areas: list[tuple[str, str, float, dict[str, float], float]],
) -> acs.Level:
    est = np.zeros((len(areas), len(acs.TABLE_ORDER), 2, 3), dtype=float)
    moe = np.zeros((len(areas), len(acs.TABLE_ORDER), 2, 3), dtype=float)
    geoids: list[str] = []
    names: list[str] = []
    t_tot = acs.TABLE_ORDER.index("total")

    for i, (geoid, name, adult_pop, cats, moe_val) in enumerate(areas):
        geoids.append(geoid)
        names.append(name)
        est[i, t_tot, 0, 1] = adult_pop / 2.0
        est[i, t_tot, 1, 1] = adult_pop / 2.0
        moe[i, t_tot, :, 1] = moe_val
        for cat, val in cats.items():
            t_idx = acs.TABLE_ORDER.index(cat)
            est[i, t_idx, 0, 1] = val / 2.0
            est[i, t_idx, 1, 1] = val / 2.0
            moe[i, t_idx, :, 1] = moe_val

    return acs.Level(geoids, names, list(EDGES_3), est, moe)


class MapResolver:
    def __init__(self, mapping: dict[str, dict[str, str | None]]) -> None:
        self.mapping = mapping

    def resolve(
        self,
        zip5: str | None,
        point: tuple[float, float] | None,
        state_name: str | None,
    ) -> dict[str, Any]:
        hit = self.mapping.get(zip5 or "")
        out: dict[str, Any] = {}
        if hit is not None:
            for lvl in ("zcta", "county", "cbsa"):
                code = hit.get(lvl)
                out[lvl] = (
                    {"geoid": code, "method": "table"}
                    if code
                    else {"geoid": None, "reason": "outside_any_metro_or_micro_area"}
                )
        else:
            out["zcta"] = {"geoid": None, "reason": "no_zip_in_registration"}
            out["county"] = {"geoid": None, "reason": "no_zcta_polygon_and_no_coordinates"}
            out["cbsa"] = {"geoid": None, "reason": "outside_any_metro_or_micro_area"}

        st = geography.STATE_FIPS.get(state_name or "")
        if st:
            out["state"] = {"geoid": st, "method": "registered_state_name"}
            out["region"] = {"geoid": geography.REGION[st], "method": "census_region_of_state"}
        else:
            out["state"] = {"geoid": None, "reason": "state_not_recognised"}
            out["region"] = {"geoid": None, "reason": "state_not_in_a_census_region"}
        out["us"] = {"geoid": "1", "method": "national"}
        return out


# 1. Exact proxy share algebra & settlement consistency ---------------------------------------


def test_r3_proxy_exact_pct_present_and_consistent_with_rounded_pct_single_and_multi() -> None:
    lvl = _make_level(
        [
            ("14460", "A", 300.0, {"asian": 20.0, "white": 280.0}, 2.0),
            ("26420", "B", 700.0, {"asian": 100.0, "white": 600.0}, 3.0),
        ]
    )
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    for nct, locs in [
        (
            "NCT99800001",
            [{"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
        ),
        (
            "NCT99800002",
            [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
        ),
    ]:
        out = ms_benchmark.benchmark_multisite_trial(
            {"nct_id": nct, "min_age": "18 Years", "max_age": "64 Years", "start_date": "2021-01-01", "locations": locs},
            {"cbsa": lvl},
            resolver,
        )
        cbsa = out["dimensions"]["race"]["benchmarks"]["cbsa"]
        for p_key in ("equal_facility_proxy", "distinct_area_resident_proxy"):
            px = cbsa[p_key]
            assert "exact_pct" in px
            for cat in bounds.COMPARED_CATEGORIES["race"]:
                assert abs(px["pct"][cat] - px["exact_pct"][cat]) <= 0.001


def test_r3_proxy_unknown_bounds_direction_settled_agrees_with_envelope_when_all_sites_equal() -> None:
    rng = np.random.default_rng(99800)
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    for idx in range(1, 41):
        den = int(rng.choice([300, 600, 700, 2_000_000]))
        as_site = int(rng.integers(1, den // 2))
        lvl = _make_level(
            [
                ("14460", "A", float(den), {"asian": float(as_site), "white": float(den - as_site)}, 1.0),
                ("26420", "B", float(den), {"asian": float(as_site), "white": float(den - as_site)}, 1.0),
            ]
        )
        n_trial = 600
        as_cnt = int(round(n_trial * (as_site / den))) // 2
        unk_cnt = int(round(n_trial * (as_site / den))) - as_cnt
        wh_cnt = n_trial - as_cnt - unk_cnt
        out = ms_benchmark.benchmark_multisite_trial(
            {
                "nct_id": f"NCT{99800100 + idx}",
                "min_age": "18 Years",
                "max_age": "64 Years",
                "start_date": "2021-01-01",
                "locations": [
                    {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                    {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                ],
                "race": {
                    "reported": True,
                    "omb_totals": {"asian": as_cnt, "white": wh_cnt, "unknown_not_reported": unk_cnt},
                    "raw_categories": [{"original": "Asian", "count": as_cnt}, {"original": "White", "count": wh_cnt}],
                },
            },
            {"cbsa": lvl},
            resolver,
        )
        comp = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]
        env_settled = comp["joint_direction_settled"]["asian"]
        assert comp["equal_facility_proxy"]["unknown_bounds"]["asian"]["direction_settled"] == env_settled
        assert comp["distinct_area_resident_proxy"]["unknown_bounds"]["asian"]["direction_settled"] == env_settled


def test_r3_proxy_ethnicity_reversed_category_order_preserves_exact_pct_complement() -> None:
    est = {
        "01": {
            "available": True,
            "pct": {"hispanic_latino": 33.333, "not_hispanic_latino": 66.667},
            "_exact_pct": {"hispanic_latino": 100.0 / 3.0, "not_hispanic_latino": 200.0 / 3.0},
            "moe90_pp": {"hispanic_latino": 1.0, "not_hispanic_latino": 1.0},
        },
        "02": {
            "available": True,
            "pct": {"hispanic_latino": 16.667, "not_hispanic_latino": 83.333},
            "_exact_pct": {"hispanic_latino": 100.0 / 6.0, "not_hispanic_latino": 500.0 / 6.0},
            "moe90_pp": {"hispanic_latino": 1.0, "not_hispanic_latino": 1.0},
        },
    }
    eq = bounds.equal_facility_proxy(
        est, {"01": 1, "02": 1}, categories=("not_hispanic_latino", "hispanic_latino")
    )
    assert eq["exact_pct"]["not_hispanic_latino"] == pytest.approx(75.0)
    assert eq["exact_pct"]["hispanic_latino"] == pytest.approx(25.0)
    assert eq["exact_pct"]["hispanic_latino"] + eq["exact_pct"]["not_hispanic_latino"] == 100.0


# 2. Overseas US military APO/FPO/DPO & domestic ZIP boundary guards -------------------------


def test_r3_military_apo_fpo_deduplication_and_all_levels_unresolved_reason() -> None:
    locs = [
        {"facility": "Landstuhl RMC", "city": "APO", "state": "AE", "zip": "09180", "country": "United States"},
        {"facility": "landstuhl rmc", "city": "APO", "state": "Armed Forces Europe (AE)", "zip": "09180-0001", "country": "USA"},
    ]
    fp = footprint.classify_footprint({"nct_id": "NCT99800201", "locations": locs})
    assert fp["n_facilities"] == 1
    assert fp["n_duplicate_locations_removed"] == 1
    assert fp["footprint_tier"] == "us_with_territories"

    res = footprint.resolve_us_facilities(fp["facilities"], MapResolver({}))
    for lvl in footprint.LEVEL_ORDER:
        assert res["by_level"][lvl]["all_us_facilities_resolved"] is False
        assert res["by_level"][lvl]["unresolved_reasons"] == {
            "us_territory_outside_50_states_dc_acs_frame": 1
        }


def test_r3_military_dpo_city_without_state_or_zip_under_united_states_is_not_50_state() -> None:
    fac = footprint.normalize_facility({"facility": "Embassy Health Unit", "city": "DPO", "country": "United States"})
    assert fac["jurisdiction"] == "us_territory"
    assert fac["state"] == "U.S. Armed Forces Overseas"


def test_r3_foreign_two_letter_ap_andhra_pradesh_without_zip_stays_non_us() -> None:
    fac = footprint.normalize_facility({"facility": "NIMS", "city": "Hyderabad", "state": "AP"})
    assert fac["jurisdiction"] == "non_us"


def test_r3_domestic_border_zips_adjacent_to_military_ranges_stay_50_state_us() -> None:
    """USPS domestic ZIPs immediately bordering military ``09xxx``, ``340xx``, and ``962xx-966xx``
    ranges must remain ``us_state_or_dc`` even when ``state`` is omitted in registration.
    """
    for z_code in ("08901", "10001", "33901", "34102", "96161", "96701"):
        fac = footprint.normalize_facility({"city": "Domestic City", "zip": z_code, "country": "United States"})
        assert fac["jurisdiction"] == "us_state_or_dc", f"Domestic ZIP {z_code} misclassified!"
        assert fac["zip5"] == z_code


# 3. British Virgin Islands vs US / Canada disambiguation ------------------------------------


def test_r3_bvi_all_island_tokens_and_vg_zip_under_virgin_islands_classify_as_non_us() -> None:
    for island in ("Tortola", "Road Town", "Virgin Gorda", "Anegada", "Jost Van Dyke"):
        fac = footprint.normalize_facility({"city": island, "country": "Virgin Islands"})
        assert fac["jurisdiction"] == "non_us"
        assert fac["zip5"] is None


def test_r3_british_columbia_omitted_country_and_us_british_city_not_hijacked_by_bvi_detector() -> None:
    """``_has_bvi_indicator`` must not hijack:
      1. A 50-state US clinic in ``British Hollow, Wisconsin 53820`` (``country='United States'``)
         into ``non_us`` / ``British Virgin Islands``;
      2. A Canadian clinic in ``Vancouver, British Columbia`` (when ``country`` is omitted) into
         reporting ``country == 'British Virgin Islands'`` in ``foreign_countries``!
    """
    wi = footprint.normalize_facility(
        {
            "facility": "Grant County Clinic",
            "city": "British Hollow",
            "state": "Wisconsin",
            "zip": "53820",
            "country": "United States",
        }
    )
    assert wi["jurisdiction"] == "us_state_or_dc"
    assert wi["state"] == "Wisconsin"
    assert wi["zip5"] == "53820"

    bc_fp = footprint.classify_footprint(
        {
            "nct_id": "NCT99800202",
            "locations": [
                {"facility": "Vancouver General", "city": "Vancouver", "state": "British Columbia"},
            ],
        }
    )
    assert bc_fp["footprint_tier"] == "non_us_only"
    assert "British Virgin Islands" not in bc_fp["foreign_countries"]


def test_r3_usvi_st_thomas_st_croix_st_john_with_008xx_stay_us_territory() -> None:
    for city, z_code in [("St. Thomas", "00802"), ("St. Croix", "00820"), ("St. John", "00830")]:
        fac = footprint.normalize_facility({"city": city, "zip": z_code, "country": "Virgin Islands"})
        assert fac["jurisdiction"] == "us_territory"
        assert fac["state"] == "U.S. Virgin Islands"
        assert fac["zip5"] == z_code


def test_r3_virgin_utah_84779_stays_50_state_us_never_virgin_islands() -> None:
    fac = footprint.normalize_facility(
        {"facility": "Zion Clinic", "city": "Virgin", "state": "UT", "zip": "84779", "country": "United States"}
    )
    assert fac["jurisdiction"] == "us_state_or_dc"
    assert fac["state"] == "Utah"


# 4. Strict integer & corrupted n_in_table guards ---------------------------------------------


def test_r3_boolean_seed_rejected_by_pool_multisite_cohort() -> None:
    for bad_seed in (True, False):
        with pytest.raises(TypeError, match="seed"):
            ms_benchmark.pool_multisite_cohort([], seed=bad_seed)


def test_r3_non_integer_float_and_string_parameters_rejected_cleanly() -> None:
    est = {"14460": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}}}
    with pytest.raises(TypeError):
        ms_benchmark.pool_multisite_cohort([], floor=2.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ms_benchmark.pool_multisite_cohort([], resamples="100")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bounds.site_allocation_bounds(est, {"14460": 1}, n_us_facilities=2.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bounds.site_allocation_bounds(est, {"14460": 1.5}, n_us_facilities=2)  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="cannot be negative"):
        bounds.site_allocation_bounds(est, {"14460": -1}, n_us_facilities=2)


def test_r3_equal_facility_proxy_rejects_boolean_facility_counts() -> None:
    est = {"14460": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}}}
    with pytest.raises(TypeError):
        bounds.equal_facility_proxy(est, {"14460": True})


def test_r3_mixed_international_diagnostic_handles_corrupted_nan_or_bool_n_in_table() -> None:
    """``_build_mixed_international_diagnostic`` must apply the same ``_valid_positive_table_total``
    guard as ``multisite_us_trials`` so a corrupted ``n_in_table = float('nan')`` or ``True`` on
    a ``mixed_us_and_international`` record never crashes ``pool_multisite_cohort`` or adds ``1``.
    """
    cbsa_lvl = _make_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = MapResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    mixed = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99800301",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "Boston", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "Paris", "city": "Paris", "zip": "75001", "country": "France"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 80, "unknown_not_reported": 20},
                "raw_categories": [{"original": "White", "count": 80}],
            },
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    mixed_nan = copy.deepcopy(mixed)
    mixed_nan["nct_id"] = "NCT99800302"
    mixed_nan["dimensions"]["race"]["n_in_table"] = float("nan")

    mixed_bool = copy.deepcopy(mixed)
    mixed_bool["nct_id"] = "NCT99800303"
    mixed_bool["dimensions"]["race"]["n_in_table"] = True

    pooled = ms_benchmark.pool_multisite_cohort(
        [mixed, mixed_nan, mixed_bool], level="cbsa", dimension="race", floor=5
    )
    json.dumps(pooled, allow_nan=False)
    diag = pooled["mixed_us_and_international_diagnostic"]
    assert diag["n_trials_total"] == 3
    assert diag["n_participants_in_global_pooled_tables"] == 100


def test_r3_non_integer_float_n_in_table_excluded_while_integral_float_accepted() -> None:
    cbsa_lvl = _make_level(
        [("14460", "A", 1000.0, {"white": 700.0}, 2.0), ("26420", "B", 1000.0, {"white": 400.0}, 2.0)]
    )
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    base = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99800304",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 60, "unknown_not_reported": 40},
                "raw_categories": [{"original": "White", "count": 60}],
            },
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    frac_row = copy.deepcopy(base)
    frac_row["dimensions"]["race"]["n_in_table"] = 12.5
    int_float_row = copy.deepcopy(base)
    int_float_row["dimensions"]["race"]["n_in_table"] = 100.0

    pooled = ms_benchmark.pool_multisite_cohort(
        [frac_row, int_float_row], level="cbsa", dimension="race", floor=1, resamples=20, seed=4
    )
    assert pooled["n_trials"] == 1
    assert pooled["n_participants"] == 100


# 5. Exact resolved_diff_pp_* & multi-reason skipped_geoids parity ---------------------------


def test_r3_partial_resolution_keeps_resolved_diff_distinct_from_unconditional_joint_diff() -> None:
    est = {
        "14460": {
            "available": True,
            "pct": {"white": 40.0},
            "_exact_pct": {"white": 40.0},
            "moe90_pp": {"white": 1.0},
        }
    }
    alloc = bounds.site_allocation_bounds(est, {"14460": 1}, n_us_facilities=2)
    comp = bounds.compare_with_catchment_bounds({"white": 70.0}, unknown_pct=5.0, bounds=alloc)
    wc = comp["by_category"]["white"]
    assert wc["bounds_unconditional"] is False
    assert wc["joint_diff_pp_min"] == -30.0   # 70 - 100
    assert wc["joint_diff_pp_max"] == 75.0    # 75 - 0
    assert wc["resolved_diff_pp_min"] == 30.0  # 70 - 40
    assert wc["resolved_diff_pp_max"] == 35.0  # 75 - 40


def test_r3_unconditional_resolved_diff_equals_joint_diff_across_all_three_dimensions() -> None:
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 1_000_000.0, {"white": 499_996.0, "hispanic_latino": 199_996.0}, 2.0),
            ("26420", "B", 1_000_000.0, {"white": 699_996.0, "hispanic_latino": 399_996.0}, 2.0),
        ]
    )
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99800401",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "eligibility": {"sex": "ALL"},
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 100_004, "asian": 899_996, "unknown_not_reported": 0},
                "raw_categories": [{"original": "White", "count": 100_004}, {"original": "Asian", "count": 899_996}],
            },
            "ethnicity": {
                "reported": True,
                "omb_totals": {"hispanic_latino": 100_004, "not_hispanic_latino": 899_996, "unknown_not_reported": 0},
                "raw_categories": [
                    {"omb_category": "hispanic_latino", "original": "Hispanic", "count": 100_004},
                    {"omb_category": "not_hispanic_latino", "original": "Not Hispanic", "count": 899_996},
                ],
            },
            "sex": {"reported": True, "totals": {"female": 500_004, "male": 499_996, "unknown": 0}},
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    for dim in ("race", "ethnicity", "sex"):
        comp = out["dimensions"][dim]["benchmarks"]["cbsa"]["comparison"]
        assert comp["bounds_unconditional"] is True
        for cat, c_block in comp["by_category"].items():
            assert c_block["joint_diff_pp_min"] == c_block["resolved_diff_pp_min"]
            assert c_block["joint_diff_pp_max"] == c_block["resolved_diff_pp_max"]
            b_block = comp["of_classified"]["by_category"][cat]
            assert b_block["diff_pp_min"] == b_block["resolved_diff_pp_min"]
            assert b_block["diff_pp_max"] == b_block["resolved_diff_pp_max"]


def test_r3_end_to_end_skipped_geoids_parity_across_multiple_unavailable_reasons() -> None:
    zcta_lvl = _make_level(
        [
            ("02114", "Valid", 1000.0, {"white": 700.0, "asian": 100.0}, 2.0),
            ("77030", "Subtable NaN", 1000.0, {"white": 500.0, "asian": float("nan")}, 2.0),
            ("99999", "Zero Denom", 0.0, {}, 1.0),
        ]
    )
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
            "88888": {"zcta": "88888", "county": "48201", "cbsa": "26420"},
            "99999": {"zcta": "99999", "county": "48201", "cbsa": "26420"},
        }
    )
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99800402",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                {"facility": "S3", "city": "Houston", "state": "TX", "zip": "88888", "country": "United States"},
                {"facility": "S4", "city": "Houston", "state": "TX", "zip": "99999", "country": "United States"},
            ],
        },
        {"zcta": zcta_lvl},
        resolver,
    )
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    expected_unavail = {
        "77030": "estimate_not_published",
        "88888": "area_not_in_this_window_file",
        "99999": "no_eligible_residents_estimated",
    }
    assert zb["site_allocation_bounds"]["unavailable_resolved_geoids"] == expected_unavail
    assert zb["distinct_area_resident_proxy"]["skipped_geoids"] == expected_unavail


def test_r3_strict_json_allow_nan_false_on_exact_pct_and_all_round2_fields() -> None:
    zcta_lvl = _make_level(
        [
            ("02114", "Valid", 1000.0, {"white": 700.0, "asian": 100.0}, 2.0),
            ("77030", "Subtable NaN", 1000.0, {"white": 500.0, "asian": float("nan")}, 2.0),
        ]
    )
    resolver = MapResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99800403",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                {"facility": "Mil", "city": "APO", "state": "AE", "zip": "09180", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 80, "asian": 10, "unknown_not_reported": 10},
                "raw_categories": [{"original": "White", "count": 80}, {"original": "Asian", "count": 10}],
            },
        },
        {"zcta": zcta_lvl},
        resolver,
    )
    encoded = json.dumps(out, allow_nan=False)
    decoded = json.loads(encoded)
    assert decoded["nct_id"] == "NCT99800403"
