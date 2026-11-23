"""End-to-end unit tests for catchments.benchmark: single-site equivalence with census,
multi-site trial evaluation across 6 levels and 3 dimensions, 2009 ZCTA vintage guard,
edge-case resilience, and deterministic cohort pooling with global-trial isolation."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from catchments import benchmark as ms_benchmark, footprint
from census import acs, benchmark as census_benchmark, geography


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


def _write_acs_level(
    cache_dir: Path,
    vintage: int,
    level_name: str,
    areas: list[tuple[str, dict[str, str], list[float], dict[str, list[float]], float]],
) -> acs.Level:
    geo_cols = list(areas[0][1].keys())
    for cat, table in acs.TABLES.items():
        spans = FINE_SPANS if cat == "total" else COARSE_SPANS
        variables = _make_variables(spans)
        cells = [k[:-1] for k in variables if k.endswith("M")]
        header = ["NAME"] + [c + "E" for c in cells] + [c + "M" for c in cells] + geo_cols
        rows = [header]
        for area_name, geo_ids, total_fine, by_cat, moe_val in areas:
            vals = total_fine if cat == "total" else by_cat.get(cat, [0.0] * 6)
            moes = [moe_val] * len(cells)
            rows.append(
                [area_name]
                + [str(v) for v in vals]
                + [str(m) for m in moes]
                + [geo_ids[c] for c in geo_cols]
            )
        with gzip.open(cache_dir / f"acs5_{vintage}_{table}_{level_name}.json.gz", "wt") as fh:
            json.dump({"variables": variables, "rows": rows}, fh)
    return acs.load(vintage, level_name, cache_dir)


class MockGeoResolver:
    """Offline geography resolver covering Boston (02114), Houston (77030, 77002),
    and rural Montana (59715, outside any CBSA)."""

    MAP: dict[str, dict[str, str | None]] = {
        "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
        "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        "77002": {"zcta": "77002", "county": "48201", "cbsa": "26420"},
        "59715": {"zcta": "59715", "county": "30031", "cbsa": None},
    }

    def resolve(
        self,
        zip5: str | None,
        point: tuple[float, float] | None,
        state_name: str | None,
    ) -> dict[str, Any]:
        hit = self.MAP.get(zip5 or "")
        out: dict[str, Any] = {}
        if hit is not None:
            for lvl in ("zcta", "county", "cbsa"):
                code = hit[lvl]
                if code:
                    out[lvl] = {"geoid": code, "method": "mock_polygon"}
                else:
                    out[lvl] = {"geoid": None, "reason": "outside_any_metro_or_micro_area"}
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


@pytest.fixture()
def full_levels_2024(tmp_path: Path) -> dict[str, acs.Level]:
    """Complete 6-level synthetic 2020-2024 ACS tables with distinct regional demographics."""
    zcta_col = acs.LEVELS["zcta"].split(":")[0]
    cbsa_col = acs.LEVELS["cbsa"].split(":")[0]

    # Boston (high white, high asian in medical ZCTA) vs Houston (high black/hispanic) vs Montana
    levels: dict[str, acs.Level] = {}
    levels["zcta"] = _write_acs_level(
        tmp_path,
        2024,
        "zcta",
        [
            (
                "ZCTA 02114",
                {zcta_col: "02114"},
                [50, 200, 300, 50, 50, 200, 300, 50],
                {
                    "white": [30, 320, 40, 30, 320, 40],
                    "black_african_american": [5, 30, 3, 5, 30, 3],
                    "asian": [15, 150, 7, 15, 150, 7],
                    "hispanic_latino": [5, 40, 3, 5, 40, 3],
                },
                3.0,
            ),
            (
                "ZCTA 77030",
                {zcta_col: "77030"},
                [40, 150, 250, 40, 40, 150, 250, 40],
                {
                    "white": [15, 160, 25, 15, 160, 25],
                    "black_african_american": [10, 80, 5, 10, 80, 5],
                    "asian": [10, 120, 5, 10, 120, 5],
                    "hispanic_latino": [10, 100, 5, 10, 100, 5],
                },
                2.5,
            ),
            (
                "ZCTA 77002",
                {zcta_col: "77002"},
                [30, 200, 200, 30, 30, 200, 200, 30],
                {
                    "white": [10, 140, 15, 10, 140, 15],
                    "black_african_american": [10, 160, 10, 10, 160, 10],
                    "asian": [5, 40, 2, 5, 40, 2],
                    "hispanic_latino": [10, 160, 5, 10, 160, 5],
                },
                3.5,
            ),
            (
                "ZCTA 59715",
                {zcta_col: "59715"},
                [40, 100, 150, 40, 40, 100, 150, 40],
                {
                    "white": [36, 225, 38, 36, 225, 38],
                    "black_african_american": [1, 5, 0, 1, 5, 0],
                    "asian": [1, 10, 1, 1, 10, 1],
                    "hispanic_latino": [2, 10, 1, 2, 10, 1],
                },
                2.0,
            ),
        ],
    )
    levels["county"] = _write_acs_level(
        tmp_path,
        2024,
        "county",
        [
            (
                "Suffolk County",
                {"state": "25", "county": "025"},
                [100, 400, 600, 100, 100, 400, 600, 100],
                {
                    "white": [50, 550, 70, 50, 550, 70],
                    "black_african_american": [25, 220, 15, 25, 220, 15],
                    "asian": [15, 130, 10, 15, 130, 10],
                    "hispanic_latino": [20, 200, 10, 20, 200, 10],
                },
                5.0,
            ),
            (
                "Harris County",
                {"state": "48", "county": "201"},
                [200, 800, 1200, 200, 200, 800, 1200, 200],
                {
                    "white": [60, 600, 140, 60, 600, 140],
                    "black_african_american": [50, 500, 40, 50, 500, 40],
                    "asian": [20, 200, 10, 20, 200, 10],
                    "hispanic_latino": [90, 900, 50, 90, 900, 50],
                },
                8.0,
            ),
            (
                "Gallatin County",
                {"state": "30", "county": "031"},
                [50, 200, 300, 50, 50, 200, 300, 50],
                {
                    "white": [45, 450, 48, 45, 450, 48],
                    "black_african_american": [1, 10, 0, 1, 10, 0],
                    "asian": [2, 15, 1, 2, 15, 1],
                    "hispanic_latino": [3, 25, 1, 3, 25, 1],
                },
                3.0,
            ),
        ],
    )
    levels["cbsa"] = _write_acs_level(
        tmp_path,
        2024,
        "cbsa",
        [
            (
                "Boston-Cambridge-Newton",
                {cbsa_col: "14460"},
                [200, 800, 1200, 200, 200, 800, 1200, 200],
                {
                    "white": [140, 1400, 170, 140, 1400, 170],
                    "black_african_american": [20, 200, 10, 20, 200, 10],
                    "asian": [25, 240, 12, 25, 240, 12],
                    "hispanic_latino": [25, 240, 10, 25, 240, 10],
                },
                6.0,
            ),
            (
                "Houston-Pasadena-The Woodlands",
                {cbsa_col: "26420"},
                [300, 1200, 1800, 300, 300, 1200, 1800, 300],
                {
                    "white": [100, 1050, 200, 100, 1050, 200],
                    "black_african_american": [70, 660, 50, 70, 660, 50],
                    "asian": [30, 300, 20, 30, 300, 20],
                    "hispanic_latino": [120, 1200, 80, 120, 1200, 80],
                },
                9.0,
            ),
        ],
    )
    levels["state"] = _write_acs_level(
        tmp_path,
        2024,
        "state",
        [
            (
                "Massachusetts",
                {"state": "25"},
                [300, 1000, 1500, 300, 300, 1000, 1500, 300],
                {
                    "white": [210, 1800, 260, 210, 1800, 260],
                    "black_african_american": [30, 250, 15, 30, 250, 15],
                    "asian": [30, 250, 15, 30, 250, 15],
                    "hispanic_latino": [40, 300, 15, 40, 300, 15],
                },
                7.0,
            ),
            (
                "Texas",
                {"state": "48"},
                [500, 2000, 3000, 500, 500, 2000, 3000, 500],
                {
                    "white": [200, 2000, 350, 200, 2000, 350],
                    "black_african_american": [70, 750, 60, 70, 750, 60],
                    "asian": [30, 350, 25, 30, 350, 25],
                    "hispanic_latino": [220, 2000, 120, 220, 2000, 120],
                },
                10.0,
            ),
            (
                "Montana",
                {"state": "30"},
                [100, 400, 600, 100, 100, 400, 600, 100],
                {
                    "white": [88, 880, 95, 88, 880, 95],
                    "black_african_american": [1, 10, 1, 1, 10, 1],
                    "asian": [2, 15, 1, 2, 15, 1],
                    "hispanic_latino": [5, 40, 2, 5, 40, 2],
                },
                4.0,
            ),
        ],
    )
    levels["region"] = _write_acs_level(
        tmp_path,
        2024,
        "region",
        [
            (
                "Northeast",
                {"region": "1"},
                [500, 2000, 3000, 500, 500, 2000, 3000, 500],
                {
                    "white": [320, 3250, 400, 320, 3250, 400],
                    "black_african_american": [70, 650, 50, 70, 650, 50],
                    "asian": [40, 450, 30, 40, 450, 30],
                    "hispanic_latino": [80, 750, 40, 80, 750, 40],
                },
                11.0,
            ),
            (
                "South",
                {"region": "3"},
                [800, 3000, 5000, 800, 800, 3000, 5000, 800],
                {
                    "white": [400, 4400, 600, 400, 4400, 600],
                    "black_african_american": [200, 1600, 120, 200, 1600, 120],
                    "asian": [40, 400, 25, 40, 400, 25],
                    "hispanic_latino": [200, 1800, 90, 200, 1800, 90],
                },
                12.0,
            ),
            (
                "West",
                {"region": "4"},
                [600, 2500, 3500, 600, 600, 2500, 3500, 600],
                {
                    "white": [300, 3300, 450, 300, 3300, 450],
                    "black_african_american": [35, 300, 25, 35, 300, 25],
                    "asian": [70, 750, 60, 70, 750, 60],
                    "hispanic_latino": [200, 1800, 90, 200, 1800, 90],
                },
                10.0,
            ),
        ],
    )
    levels["us"] = _write_acs_level(
        tmp_path,
        2024,
        "us",
        [
            (
                "United States",
                {"us": "1"},
                [2000, 8000, 12000, 2000, 2000, 8000, 12000, 2000],
                {
                    "white": [1100, 12000, 1500, 1100, 12000, 1500],
                    "black_african_american": [300, 2600, 200, 300, 2600, 200],
                    "asian": [150, 1400, 100, 150, 1400, 100],
                    "hispanic_latino": [500, 4000, 200, 500, 4000, 200],
                },
                15.0,
            )
        ],
    )
    return levels


def _sample_demographics(
    *,
    white: int = 50,
    black: int = 20,
    asian: int = 10,
    race_unk: int = 20,
    hisp: int = 15,
    not_hisp: int = 75,
    eth_unk: int = 10,
    female: int = 52,
    male: int = 48,
) -> dict[str, Any]:
    return {
        "race": {
            "reported": True,
            "omb_totals": {
                "white": white,
                "black_african_american": black,
                "asian": asian,
                "american_indian_alaska_native": 0,
                "native_hawaiian_pacific_islander": 0,
                "more_than_one_race": 0,
                "other": 0,
                "unknown_not_reported": race_unk,
            },
            "raw_categories": [
                {"original": "White", "count": white},
                {"original": "Black", "count": black},
                {"original": "Asian", "count": asian},
            ],
        },
        "ethnicity": {
            "reported": True,
            "omb_totals": {
                "hispanic_latino": hisp,
                "not_hispanic_latino": not_hisp,
                "unknown_not_reported": eth_unk,
            },
            "raw_categories": [
                {"omb_category": "hispanic_latino", "original": "Hispanic or Latino", "count": hisp},
                {"omb_category": "not_hispanic_latino", "original": "Not Hispanic or Latino", "count": not_hisp},
            ],
        },
        "sex": {
            "reported": True,
            "totals": {"female": female, "male": male, "unknown": 0},
        },
    }


# --- Single-site equivalence against census.benchmark.expected ------------------------------


def test_single_us_facility_collapses_identically_to_census_benchmark_expected(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    rec = {
        "nct_id": "NCT99000221",
        "study_type": "INTERVENTIONAL",
        "enrollment": 100,
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "ALL"},
        "start_date": "2021-01-01",
        "primary_completion_date": "2023-01-01",
        "study_sites": [
            {"facility": "MGH", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"}
        ],
        **_sample_demographics(),
    }
    res = ms_benchmark.benchmark_multisite_trial(
        rec, full_levels_2024, resolver, snapshot_date="2026-11-22"
    )
    assert res["footprint"]["footprint_tier"] == "single_us_facility"
    assert res["benchmark_period"]["vintage"] == 2024

    expected_geoids = {
        "zcta": "02114",
        "county": "25025",
        "cbsa": "14460",
        "state": "25",
        "region": "1",
        "us": "1",
    }
    for dim in ("race", "ethnicity", "sex"):
        for lvl, geoid in expected_geoids.items():
            b = res["dimensions"][dim]["benchmarks"][lvl]
            ref = census_benchmark.expected(
                full_levels_2024[lvl], geoid, dim, ("male", "female"), 18.0, 65.0
            )
            assert b["available"] is True
            assert b["bounds_unconditional"] is True
            # Exact floating-point identity across all four views and census.benchmark.expected!
            assert b["resolved_min_pct"] == ref["pct"]
            assert b["resolved_max_pct"] == ref["pct"]
            assert b["equal_facility_proxy"]["pct"] == ref["pct"]
            assert b["distinct_area_resident_proxy"]["pct"] == ref["pct"]
            assert b["equal_facility_proxy"]["moe90_pp"] == ref["moe90_pp"]
            assert b["distinct_area_resident_proxy"]["moe90_pp"] == ref["moe90_pp"]


# --- Multi-site US trials, API v2 format, and 2009 ZCTA vintage guard -----------------------


def test_multisite_trial_evaluates_all_six_levels_and_api_v2_matches_extracted_record(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    extracted = {
        "nct_id": "NCT99000222",
        "study_type": "INTERVENTIONAL",
        "enrollment": 100,
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "ALL"},
        "start_date": "2021-06-01",
        "primary_completion_date": "2023-06-01",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Site 1", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            {"facility": "Houston Site 2", "city": "Houston", "state": "TX", "zip": "77002", "country": "United States"},
        ],
        **_sample_demographics(white=85, black=5, asian=5, race_unk=5),
    }
    api_v2 = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99000222"},
            "designModule": {"studyType": "INTERVENTIONAL", "enrollmentInfo": {"count": 100, "type": "ACTUAL"}},
            "eligibilityModule": {"minimumAge": "18 Years", "maximumAge": "64 Years", "sex": "ALL"},
            "statusModule": {
                "startDateStruct": {"date": "2021-06-01"},
                "primaryCompletionDateStruct": {"date": "2023-06-01"},
            },
            "contactsLocationsModule": {
                "locations": [
                    {"facility": "Boston Site", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"},
                    {"facility": "Houston Site 1", "city": "Houston", "state": "Texas", "zip": "77030", "country": "United States"},
                    {"facility": "Houston Site 2", "city": "Houston", "state": "Texas", "zip": "77002", "country": "United States"},
                ]
            },
        },
        **_sample_demographics(white=85, black=5, asian=5, race_unk=5),
    }
    r_ext = ms_benchmark.benchmark_multisite_trial(extracted, full_levels_2024, resolver)
    r_v2 = ms_benchmark.benchmark_multisite_trial(api_v2, full_levels_2024, resolver)
    assert r_ext["footprint"]["footprint_tier"] == "multi_site_us_only"
    assert r_ext["dimensions"]["race"]["benchmarks"] == r_v2["dimensions"]["race"]["benchmarks"]

    cbsa = r_ext["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa["available"] is True and cbsa["bounds_unconditional"] is True
    assert cbsa["n_distinct_areas"] == 2
    assert cbsa["resolved_min_pct"]["white"] == pytest.approx(35.0)
    assert cbsa["resolved_max_pct"]["white"] == pytest.approx(70.0)
    # 85% white > max 70.0%: settled positive!
    assert cbsa["joint_direction_settled"]["white"] is True
    assert cbsa["joint_direction"]["white"] == "positive"

    # At 'us' national level |G| = 1, collapsing bounds and both proxies to national share (60.0%)
    us_bench = r_ext["dimensions"]["race"]["benchmarks"]["us"]
    assert us_bench["n_distinct_areas"] == 1
    assert us_bench["resolved_min_pct"]["white"] == pytest.approx(60.0)
    assert us_bench["resolved_max_pct"]["white"] == pytest.approx(60.0)
    assert us_bench["equal_facility_proxy"]["pct"]["white"] == pytest.approx(60.0)
    assert us_bench["distinct_area_resident_proxy"]["pct"]["white"] == pytest.approx(60.0)


def test_vintage_2009_marks_zcta_unpublished_even_when_unrounded_midpoint_rounds_to_2010(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    # Midpoint in last two days of 2009 rounds to 2010.0 in period['midpoint'] yet assigns 2005-2009!
    rec = {
        "nct_id": "NCT99000223",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "ALL"},
        "start_date": "2009-12-30",
        "primary_completion_date": "2009-12-31",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        **_sample_demographics(),
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    assert out["period"]["midpoint"] == 2010.0
    assert out["benchmark_period"]["vintage"] == 2009
    assert out["benchmark_period"]["window"] == "2005-2009"
    assert out["dimensions"]["race"]["benchmarks"]["zcta"] == {
        "available": False,
        "level": "zcta",
        "reason": "zcta_tables_not_published_for_this_window",
    }
    # County, CBSA, State, Region, and US remain available for 2009
    for lvl in ("county", "cbsa", "state", "region", "us"):
        assert out["dimensions"]["race"]["benchmarks"][lvl]["available"] is True


# --- Resilience to unreadable ages, missing dates, rural sites, and table structure ----------


def test_unreadable_and_inverted_ages_do_not_crash_and_block_benchmarks_cleanly(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    for nct_id, min_a, max_a in [
        ("NCT99000224", "eighteen", "65 Years"),
        ("NCT99000225", "70 Years", "18 Years"),
    ]:
        rec = {
            "nct_id": nct_id,
            "min_age": min_a,
            "max_age": max_a,
            "start_date": "2021-01-01",
            "primary_completion_date": "2022-01-01",
            "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
            **_sample_demographics(),
        }
        out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
        assert "error" in out["eligibility"]
        assert out["dimensions"]["race"]["state"] == "reported"
        assert out["dimensions"]["race"]["pooled"] is False
        assert out["dimensions"]["race"]["not_pooled_reason"] == "eligible_ages_unreadable"
        for lvl in footprint.LEVEL_ORDER:
            assert out["dimensions"]["race"]["benchmarks"][lvl]["available"] is False
            assert out["dimensions"]["race"]["benchmarks"][lvl]["reason"] == "eligible_ages_unreadable"


def test_missing_dates_and_foreign_or_empty_footprints_do_not_crash(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    no_date = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99000226",
            "min_age": "18 Years",
            "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
            **_sample_demographics(),
        },
        full_levels_2024,
        resolver,
    )
    assert no_date["benchmark_period"]["vintage"] is None
    assert no_date["dimensions"]["race"]["benchmarks"]["cbsa"]["reason"] == "no_benchmark_window"

    no_sites = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99000227",
            "min_age": "18 Years",
            "start_date": "2021-01-01",
            "locations": [],
            **_sample_demographics(),
        },
        full_levels_2024,
        resolver,
    )
    assert no_sites["dimensions"]["race"]["benchmarks"]["cbsa"]["reason"] == "no_facilities_listed"

    foreign_only = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99000228",
            "min_age": "18 Years",
            "start_date": "2021-01-01",
            "locations": [{"city": "Paris", "country": "France", "zip": "75001"}],
            **_sample_demographics(),
        },
        full_levels_2024,
        resolver,
    )
    assert foreign_only["dimensions"]["race"]["benchmarks"]["cbsa"]["reason"] == "non_us_only_no_us_catchment"


def test_rural_site_outside_cbsa_makes_cbsa_conditional_while_county_and_state_stay_unconditional(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    rec = {
        "nct_id": "NCT99000229",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "ALL"},
        "start_date": "2021-01-01",
        "primary_completion_date": "2023-01-01",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Rural MT Site", "city": "Bozeman", "state": "MT", "zip": "59715", "country": "United States"},
        ],
        **_sample_demographics(white=95, black=3, asian=2, race_unk=0),
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    cbsa = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    county = out["dimensions"]["race"]["benchmarks"]["county"]
    state = out["dimensions"]["race"]["benchmarks"]["state"]

    assert cbsa["available"] is True
    assert cbsa["bounds_unconditional"] is False
    assert cbsa["n_unresolved_or_unavailable"] == 1
    assert cbsa["joint_direction_settled"]["white"] is False
    assert cbsa["site_weight_settled"]["white"] is False

    # At county and state levels, both MA and MT sites resolve cleanly -> unconditional!
    assert county["bounds_unconditional"] is True
    assert state["bounds_unconditional"] is True
    assert state["joint_direction_settled"]["white"] is True


def test_single_sex_eligibility_and_single_ethnicity_category_guard_propagate_through_trial(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    demos = _sample_demographics()
    demos["ethnicity"] = {
        "reported": True,
        "omb_totals": {"hispanic_latino": 12, "not_hispanic_latino": 0, "unknown_not_reported": 88},
        "raw_categories": [{"omb_category": "hispanic_latino", "original": "Hispanic", "count": 12}],
    }
    rec = {
        "nct_id": "NCT99000230",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "FEMALE"},
        "start_date": "2021-01-01",
        "primary_completion_date": "2023-01-01",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        **demos,
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    assert out["dimensions"]["sex"]["pooled"] is False
    assert out["dimensions"]["sex"]["benchmarks"]["cbsa"]["reason"] == "single_sex_by_eligibility"
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["available"] is True

    eth_cbsa = out["dimensions"]["ethnicity"]["benchmarks"]["cbsa"]
    assert eth_cbsa["comparison_available"] is True
    assert eth_cbsa["of_classified"] is None
    assert eth_cbsa["classified_share_reason"] == "table_reports_one_ethnicity_category_only"


def test_combined_race_ethnicity_table_is_flagged_unusable_for_shares(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    demos = _sample_demographics()
    demos["race"] = {
        "reported": True,
        "omb_totals": {"white": 60, "black_african_american": 30, "unknown_not_reported": 10},
        "raw_categories": [
            {"original": "White/Non-Hispanic", "count": 60},
            {"original": "Black", "count": 30},
        ],
    }
    rec = {
        "nct_id": "NCT99000231",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        **demos,
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    assert out["dimensions"]["race"]["pooled"] is False
    assert out["dimensions"]["race"]["not_pooled_reason"] == "table_rows_not_one_classification"
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["available"] is True
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison_available"] is False


# --- Cohort pooling, joint settlement summary, and global-trial isolation -------------------


def _make_multisite_trial(
    nct_id: str,
    white: int,
    black: int,
    unk: int,
    *,
    locations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    total = white + black + unk
    locs = locations or [
        {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
        {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
    ]
    return {
        "nct_id": nct_id,
        "study_type": "INTERVENTIONAL",
        "enrollment": total,
        "min_age": "18 Years",
        "max_age": "64 Years",
        "eligibility": {"sex": "ALL"},
        "start_date": "2021-01-01",
        "primary_completion_date": "2023-01-01",
        "locations": locs,
        **_sample_demographics(
            white=white,
            black=black,
            asian=0,
            race_unk=unk,
            female=total // 2,
            male=total - (total // 2),
            hisp=10,
            not_hisp=max(0, total - 10),
            eth_unk=0,
        ),
    }


def test_pool_multisite_cohort_withholds_below_floor_and_repeats_deterministically(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    trials_out = [
        ms_benchmark.benchmark_multisite_trial(
            _make_multisite_trial(nct_id, 80, 15, 5),
            full_levels_2024,
            resolver,
        )
        for nct_id in ("NCT99000301", "NCT99000302", "NCT99000303", "NCT99000304")
    ]
    withheld = ms_benchmark.pool_multisite_cohort(
        trials_out, level="cbsa", dimension="race", floor=50, resamples=100, seed=11
    )
    assert withheld["display_rule"] == "withheld"
    assert withheld["gate_reason"] == "fewer than 50 qualifying trials"
    assert withheld["n_trials"] == 4

    shown_a = ms_benchmark.pool_multisite_cohort(
        trials_out, level="cbsa", dimension="race", floor=3, resamples=200, seed=11
    )
    shown_b = ms_benchmark.pool_multisite_cohort(
        trials_out, level="cbsa", dimension="race", floor=3, resamples=200, seed=11
    )
    assert shown_a == shown_b
    assert shown_a["display_rule"] == "show"


def test_pool_multisite_cohort_reports_equal_and_weighted_proxies_plus_joint_settlement_shares(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    # CBSA white bounds for (14460, 26420) are [35.0%, 70.0%]
    # Trial 1 (N=1000): 90% white, 0% unk -> settled positive
    # Trial 2 (N=100):  10% white, 5% unk (ceil 15% < 35%) -> settled negative
    # Trial 3 (N=100):  50% white, 0% unk -> unsettled within bounds
    # Trial 4 (N=100):  90% white, 0% unk, plus rural MT site -> unsettled incomplete resolution
    t1 = ms_benchmark.benchmark_multisite_trial(
        _make_multisite_trial("NCT99000401", 900, 100, 0), full_levels_2024, resolver
    )
    t2 = ms_benchmark.benchmark_multisite_trial(
        _make_multisite_trial("NCT99000402", 10, 85, 5), full_levels_2024, resolver
    )
    t3 = ms_benchmark.benchmark_multisite_trial(
        _make_multisite_trial("NCT99000403", 50, 50, 0), full_levels_2024, resolver
    )
    t4 = ms_benchmark.benchmark_multisite_trial(
        _make_multisite_trial(
            "NCT99000404",
            90,
            10,
            0,
            locations=[
                {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "Rural MT", "city": "Bozeman", "state": "MT", "zip": "59715", "country": "United States"},
            ],
        ),
        full_levels_2024,
        resolver,
    )

    pooled = ms_benchmark.pool_multisite_cohort(
        [t1, t2, t3, t4], level="cbsa", dimension="race", floor=4, resamples=200, seed=7
    )
    assert pooled["n_trials"] == 4
    assert pooled["n_participants"] == 1300
    assert pooled["concentration_flag"] is True  # 1000 / 1300 > 0.5
    assert pooled["n_unconditional_bounds_trials"] == 3
    assert pooled["n_partial_resolution_trials"] == 1

    # Equal-trial white observed mean: (90 + 10 + 50 + 90) / 4 = 60.0%
    # Participant-weighted white observed: (900 + 10 + 50 + 90) / 1300 = 1050 / 1300 = 80.77%
    eq_white = next(c for c in pooled["equal_facility_proxy"]["categories"] if c["category"] == "white")
    assert eq_white["observed_pct"] == pytest.approx(60.0)
    assert eq_white["weighted"]["observed_pct"] == pytest.approx(80.77, abs=0.01)

    js_white = pooled["joint_settlement"]["white"]
    assert js_white["n_trials"] == 4
    assert js_white["n_settled_positive"] == 1
    assert js_white["n_settled_negative"] == 1
    assert js_white["n_unsettled"] == 2
    assert js_white["n_unsettled_incomplete_resolution"] == 1
    assert js_white["n_unsettled_within_bounds_or_unknowns"] == 1
    assert js_white["settled_positive_pct"] == 25.0
    assert js_white["settled_negative_pct"] == 25.0
    assert js_white["unsettled_pct"] == 50.0


def test_mixed_us_and_international_trials_never_contaminate_primary_us_cohort(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    us_trials = [
        ms_benchmark.benchmark_multisite_trial(
            _make_multisite_trial(nct_id, 60, 30, 10),
            full_levels_2024,
            resolver,
        )
        for nct_id in (
            "NCT99000501",
            "NCT99000502",
            "NCT99000503",
            "NCT99000504",
            "NCT99000505",
        )
    ]
    global_trials = [
        ms_benchmark.benchmark_multisite_trial(
            _make_multisite_trial(
                nct_id,
                0,
                1000,
                0,
                locations=[
                    {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                    {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                    {"facility": "Paris Site", "city": "Paris", "country": "France", "zip": "75001"},
                    {"facility": "Tokyo Site", "city": "Tokyo", "country": "Japan", "zip": "100-0001"},
                ],
            ),
            full_levels_2024,
            resolver,
        )
        for nct_id in (
            "NCT99000601",
            "NCT99000602",
            "NCT99000603",
            "NCT99000604",
            "NCT99000605",
            "NCT99000606",
            "NCT99000607",
            "NCT99000608",
            "NCT99000609",
            "NCT99000610",
        )
    ]

    us_only_pool = ms_benchmark.pool_multisite_cohort(
        us_trials, level="cbsa", dimension="race", floor=3, resamples=100, seed=99
    )
    combined_input_pool = ms_benchmark.pool_multisite_cohort(
        us_trials + global_trials, level="cbsa", dimension="race", floor=3, resamples=100, seed=99
    )

    # Primary US figures must be 100% identical despite adding 10,000 global-trial participants!
    assert combined_input_pool["n_trials"] == us_only_pool["n_trials"] == 5
    assert combined_input_pool["n_participants"] == us_only_pool["n_participants"] == 500
    assert combined_input_pool["equal_facility_proxy"] == us_only_pool["equal_facility_proxy"]
    assert combined_input_pool["distinct_area_resident_proxy"] == us_only_pool["distinct_area_resident_proxy"]
    assert combined_input_pool["joint_settlement"] == us_only_pool["joint_settlement"]

    # Meanwhile the separate diagnostic block records the 10 global trials transparently
    diag = combined_input_pool["mixed_us_and_international_diagnostic"]
    assert diag["included_in_primary_us_cohort"] is False
    assert diag["global_pooled_baseline_table"] is True
    assert diag["n_trials_total"] == 10
    assert diag["n_participants_in_global_pooled_tables"] == 10000
    assert diag["mean_us_facility_share"] == pytest.approx(0.5)


def test_us_with_territories_stays_conditional_and_is_excluded_from_primary_rollup(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    rec = _make_multisite_trial(
        "NCT99000701",
        95,
        5,
        0,
        locations=[
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "PR Site", "city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
        ],
    )
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    assert out["footprint"]["footprint_tier"] == "us_with_territories"
    assert out["dimensions"]["race"]["pooled"] is False
    for lvl in footprint.LEVEL_ORDER:
        b = out["dimensions"]["race"]["benchmarks"][lvl]
        assert b["available"] is True
        assert b["bounds_unconditional"] is False
        assert b["joint_direction_settled"]["white"] is False

    pooled = ms_benchmark.pool_multisite_cohort([out], level="cbsa", dimension="race", floor=1)
    assert pooled["footprint_counts"]["us_with_territories"] == 1
    assert pooled["n_trials"] == 0
    assert pooled["display_rule"] == "withheld"


def test_geography_sensitivity_flags_large_local_spread_and_referral_zips_add_caveat(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    # ZCTA 77030 has 30% Asian (240/800) vs Texas state 8.75% (700/8000) -> > 20 pp spread!
    rec = _make_multisite_trial(
        "NCT99000702",
        40,
        30,
        30,
        locations=[
            {"facility": "TMC 1", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            {"facility": "TMC 2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    )
    out = ms_benchmark.benchmark_multisite_trial(
        rec, full_levels_2024, resolver, referral_zips={"77030"}
    )
    assert out["referral_zip"] is True
    assert "site_in_a_referral_centre_zip" in out["reference_population"]["caveats"]
    sens = out["dimensions"]["race"]["geography_sensitivity"]
    assert sens["flag"] is True
    assert sens["max_spread_pp"] >= 10.0


def test_unreported_baseline_tables_still_receive_catchment_bounds_and_proxies(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    rec = {
        "nct_id": "NCT99000703",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2022-01-01",
        "primary_completion_date": "2023-01-01",
        "locations": [
            {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, full_levels_2024, resolver)
    assert out["dimensions"]["race"]["state"] == "not_reported"
    cbsa = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa["available"] is True
    assert cbsa["comparison_available"] is False
    assert cbsa["comparison_reason"] == "race_not_reported"
    assert cbsa["resolved_min_pct"]["white"] == pytest.approx(35.0)
    assert cbsa["resolved_max_pct"]["white"] == pytest.approx(70.0)


def test_vintage_keyed_levels_and_resolvers_and_size_gates(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    rec = _make_multisite_trial("NCT99000704", 500, 100, 0)
    capped = ms_benchmark.benchmark_multisite_trial(
        rec,
        {2024: full_levels_2024},
        {2024: resolver},
        size_cap_participants=200,
    )
    assert capped["dimensions"]["race"]["pooled"] is False
    assert capped["dimensions"]["race"]["not_pooled_reason"] == "over_size_cap"

    under = ms_benchmark.benchmark_multisite_trial(
        _make_multisite_trial("NCT99000705", 3, 2, 0),
        {2024: full_levels_2024},
        {2024: resolver},
        min_participants_in_table=10,
    )
    assert under["dimensions"]["race"]["pooled"] is False
    assert under["dimensions"]["race"]["not_pooled_reason"] == "under_min_participants"


def test_pool_multisite_cohort_validates_level_and_dimension_and_pools_ethnicity_and_sex(
    full_levels_2024: dict[str, acs.Level],
) -> None:
    resolver = MockGeoResolver()
    trials_out = [
        ms_benchmark.benchmark_multisite_trial(
            _make_multisite_trial(nct_id, 50, 40, 10),
            full_levels_2024,
            resolver,
        )
        for nct_id in ("NCT99000801", "NCT99000802", "NCT99000803")
    ]
    eth_pool = ms_benchmark.pool_multisite_cohort(
        trials_out, level="state", dimension="ethnicity", floor=2, resamples=50, seed=5
    )
    sex_pool = ms_benchmark.pool_multisite_cohort(
        trials_out, level="us", dimension="sex", floor=2, resamples=50, seed=5
    )
    assert eth_pool["display_rule"] == "show" and len(eth_pool["equal_facility_proxy"]["categories"]) == 2
    assert sex_pool["display_rule"] == "show" and len(sex_pool["equal_facility_proxy"]["categories"]) == 2

    with pytest.raises(ValueError, match="unsupported geography level"):
        ms_benchmark.pool_multisite_cohort(trials_out, level="tract")
    with pytest.raises(ValueError, match="unsupported dimension"):
        ms_benchmark.pool_multisite_cohort(trials_out, dimension="age")

