"""Unit tests for catchments.footprint: extraction, deduplication, 6-tier classification,
and multi-level US facility geographic resolution."""
from __future__ import annotations

from typing import Any

import pytest

from catchments import footprint
from census import geography


class DummyResolver:
    """Deterministic offline duck-typed resolver matching census.geography.Resolver."""

    def __init__(self, table: dict[str, dict[str, str | None]]) -> None:
        self.table = table

    def resolve(
        self,
        zip5: str | None,
        point: tuple[float, float] | None,
        state_name: str | None,
    ) -> dict[str, Any]:
        entry = self.table.get(zip5 or "")
        out: dict[str, Any] = {}
        if entry is not None:
            for lvl in ("zcta", "county", "cbsa"):
                code = entry.get(lvl)
                if code:
                    out[lvl] = {"geoid": code, "method": "mock_table"}
                else:
                    out[lvl] = {"geoid": None, "reason": f"mock_missing_{lvl}"}
        else:
            out["zcta"] = {
                "geoid": None,
                "reason": "no_zip_in_registration" if not zip5 else "zip_has_no_zcta_in_this_window",
            }
            if point is not None:
                out["county"] = {"geoid": "48201", "method": "city_point_in_polygon"}
                out["cbsa"] = {"geoid": "26420", "method": "city_point_in_polygon"}
            else:
                out["county"] = {"geoid": None, "reason": "no_zcta_polygon_and_no_coordinates"}
                out["cbsa"] = {"geoid": None, "reason": "outside_any_metro_or_micro_area"}

        state_fips = geography.STATE_FIPS.get(state_name or "")
        if state_fips:
            out["state"] = {"geoid": state_fips, "method": "registered_state_name"}
            out["region"] = {"geoid": geography.REGION[state_fips], "method": "census_region_of_state"}
        else:
            out["state"] = {"geoid": None, "reason": "state_not_recognised"}
            out["region"] = {"geoid": None, "reason": "state_not_in_a_census_region"}
        out["us"] = {"geoid": "1", "method": "national"}
        return out


# --- ZIP, coordinate, and jurisdiction normalization ----------------------------------------


@pytest.mark.parametrize(
    "raw_zip,expected",
    [
        ("02114", "02114"),
        ("77030-4009", "77030"),
        ("  10065-1234  ", "10065"),
        ("00901", "00901"),
        ("00000", None),
        ("00000-0000", None),
        ("1234", None),
        ("100000", None),
        ("02114-12", None),
        ("M5G 2C4", None),
        ("SW1A 1AA", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_zip5_accepts_us_formats_and_rejects_malformed_codes(
    raw_zip: str | None, expected: str | None
) -> None:
    assert footprint.normalize_zip5(raw_zip) == expected


def test_foreign_five_digit_postal_codes_never_receive_us_zip5() -> None:
    paris = footprint.normalize_facility(
        {"facility": "Hopital Saint-Louis", "city": "Paris", "zip": "75010", "country": "France"}
    )
    berlin = footprint.normalize_facility(
        {"facility": "Charite", "city": "Berlin", "zip": "10117", "country": "Germany"}
    )
    assert paris["jurisdiction"] == "non_us"
    assert paris["zip5"] is None and paris["zip_as_registered"] == "75010"
    assert berlin["jurisdiction"] == "non_us"
    assert berlin["zip5"] is None and berlin["zip_as_registered"] == "10117"


def test_coordinates_normalize_from_api_v2_geopoint_and_flat_fields() -> None:
    v2 = footprint.normalize_facility(
        {"country": "United States", "state": "TX", "geoPoint": {"lat": 29.707, "lon": -95.401}}
    )
    flat = footprint.normalize_facility(
        {"country": "United States", "state": "MA", "latitude": "42.3626", "longitude": "-71.0686"}
    )
    tup = footprint.normalize_facility(
        {"country": "United States", "state": "CA", "point": [34.0522, -118.2437]}
    )
    assert v2["point"] == (29.707, -95.401)
    assert flat["point"] == (42.3626, -71.0686)
    assert tup["point"] == (34.0522, -118.2437)


@pytest.mark.parametrize(
    "bad_loc",
    [
        {"country": "United States", "geoPoint": {"lat": 95.0, "lon": -70.0}},
        {"country": "United States", "geoPoint": {"lat": 40.0, "lon": -185.0}},
        {"country": "United States", "geoPoint": {"lat": float("nan"), "lon": -70.0}},
        {"country": "United States", "geoPoint": {"lat": float("inf"), "lon": -70.0}},
        {"country": "United States", "geoPoint": {"lat": True, "lon": False}},
        {"country": "United States", "geoPoint": {"lat": "north", "lon": "west"}},
        {"country": "United States", "geoPoint": {"lat": 40.0}},
    ],
)
def test_invalid_coordinates_are_rejected_to_none(bad_loc: dict[str, Any]) -> None:
    assert footprint.normalize_facility(bad_loc)["point"] is None


def test_all_50_states_and_dc_map_to_us_state_or_dc() -> None:
    assert len(geography.STATE_FIPS) == 51
    for state_name in geography.STATE_FIPS:
        fac = footprint.normalize_facility(
            {"city": "Capital", "state": state_name.lower(), "country": "United States"}
        )
        assert fac["jurisdiction"] == "us_state_or_dc"
        assert fac["state"] == state_name


def test_postal_codes_and_dc_aliases_map_to_canonical_state_names() -> None:
    ma = footprint.normalize_facility({"state": "MA", "zip": "02114", "country": "USA"})
    dc = footprint.normalize_facility({"state": "Washington, D.C.", "zip": "20007", "country": "U.S."})
    assert ma["state"] == "Massachusetts" and ma["jurisdiction"] == "us_state_or_dc"
    assert dc["state"] == "District of Columbia" and dc["jurisdiction"] == "us_state_or_dc"


@pytest.mark.parametrize(
    "loc,expected_territory",
    [
        ({"country": "Puerto Rico", "city": "San Juan", "zip": "00901"}, "Puerto Rico"),
        ({"country": "United States", "state": "PR", "zip": "00936-1000"}, "Puerto Rico"),
        ({"country": "United States", "zip": "00921"}, "Puerto Rico"),
        ({"country": "Guam", "city": "Tamuning", "zip": "96913"}, "Guam"),
        ({"country": "United States", "state": "Guam", "zip": "96913"}, "Guam"),
        ({"country": "Virgin Islands (U.S.)", "city": "St. Thomas", "zip": "00802"}, "U.S. Virgin Islands"),
        ({"country": "United States", "state": "VI", "zip": "00820"}, "U.S. Virgin Islands"),
        ({"country": "American Samoa", "city": "Pago Pago", "zip": "96799"}, "American Samoa"),
        ({"country": "Northern Mariana Islands", "city": "Saipan", "zip": "96950"}, "Northern Mariana Islands"),
        ({"country": "United States", "state": "MP", "zip": "96950"}, "Northern Mariana Islands"),
    ],
)
def test_all_five_us_territories_are_distinguished_from_states_and_foreign_countries(
    loc: dict[str, Any], expected_territory: str
) -> None:
    fac = footprint.normalize_facility(loc)
    assert fac["jurisdiction"] == "us_territory"
    assert fac["state"] == expected_territory
    assert fac["zip5"] is not None


# --- Extraction across record formats and deduplication -------------------------------------


def test_extract_facilities_reads_locations_study_sites_and_raw_api_v2() -> None:
    loc_rec = {
        "nct_id": "NCT99000201",
        "locations": [{"facility": "Clinic A", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
    }
    sites_rec = {
        "nct_id": "NCT99000202",
        "study_sites": [{"facility": "Clinic A", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"}],
    }
    v2_rec = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99000203"},
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "Clinic A",
                        "city": "Boston",
                        "state": "Massachusetts",
                        "zip": "02114-2696",
                        "country": "United States",
                        "geoPoint": {"lat": 42.36, "lon": -71.07},
                    }
                ]
            },
        }
    }
    f1, m1 = footprint.extract_facilities(loc_rec)
    f2, m2 = footprint.extract_facilities(sites_rec)
    f3, m3 = footprint.extract_facilities(v2_rec)
    assert m1["n_facilities"] == m2["n_facilities"] == m3["n_facilities"] == 1
    assert f1[0]["zip5"] == f2[0]["zip5"] == f3[0]["zip5"] == "02114"
    assert f1[0]["state"] == f2[0]["state"] == f3[0]["state"] == "Massachusetts"
    assert f3[0]["point"] == (42.36, -71.07)


def test_deduplicate_facilities_merges_exact_named_duplicates_and_retains_coordinates() -> None:
    raw = [
        {"facility": "Mayo Clinic", "city": "Rochester", "state": "MN", "zip": "55905", "country": "United States"},
        {
            "facility": "  mayo  clinic ",
            "city": "ROCHESTER",
            "state": "Minnesota",
            "zip": "55905-0001",
            "country": "United States",
            "geoPoint": {"lat": 44.0225, "lon": -92.4669},
            "status": "RECRUITING",
        },
    ]
    fp = footprint.classify_footprint({"nct_id": "NCT99000204", "locations": raw})
    assert fp["n_raw_locations"] == 2
    assert fp["n_duplicate_locations_removed"] == 1
    assert fp["n_facilities"] == 1
    assert fp["footprint_tier"] == "single_us_facility"
    assert fp["facilities"][0]["point"] == (44.0225, -92.4669)
    assert fp["facilities"][0]["status"] == "RECRUITING"


def test_deduplicate_preserves_distinct_clinics_and_generic_placeholders_in_same_zip() -> None:
    raw = [
        {"facility": "Investigational Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Investigational Site", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
    ]
    fp_default = footprint.classify_footprint({"nct_id": "NCT99000205", "locations": raw})
    assert fp_default["n_facilities"] == 4
    assert fp_default["n_duplicate_locations_removed"] == 0
    assert fp_default["footprint_tier"] == "multi_site_us_only"

    fp_collapsed = footprint.classify_footprint(
        {"nct_id": "NCT99000205", "locations": raw}, collapse_unnamed=True
    )
    assert fp_collapsed["n_facilities"] == 2
    assert fp_collapsed["n_duplicate_locations_removed"] == 2


# --- Six mutually exclusive, exhaustive footprint tiers -------------------------------------


def test_tier_1_no_facilities_listed() -> None:
    fp = footprint.classify_footprint({"nct_id": "NCT99000206", "locations": []})
    assert fp["footprint_tier"] == "no_facilities_listed"
    assert fp["n_facilities"] == 0 and fp["n_us_facilities"] == 0
    assert fp["global_pooled_baseline_table"] is False
    assert fp["eligible_for_us_single_site"] is False
    assert fp["eligible_for_us_multisite_rollup"] is False


def test_tier_2_single_us_facility() -> None:
    fp = footprint.classify_footprint(
        {"nct_id": "NCT99000207", "study_sites": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}]}
    )
    assert fp["footprint_tier"] == "single_us_facility"
    assert fp["eligible_for_us_single_site"] is True
    assert fp["eligible_for_us_multisite_rollup"] is False
    assert fp["global_pooled_baseline_table"] is False


def test_tier_3_multi_site_us_only() -> None:
    fp = footprint.classify_footprint(
        {
            "nct_id": "NCT99000208",
            "locations": [
                {"facility": "Site 1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "Site 2", "city": "Washington", "state": "DC", "zip": "20007", "country": "United States"},
            ],
        }
    )
    assert fp["footprint_tier"] == "multi_site_us_only"
    assert fp["eligible_for_us_multisite_rollup"] is True
    assert fp["global_pooled_baseline_table"] is False
    assert fp["us_states_and_dc"] == ["District of Columbia", "Massachusetts"]


def test_tier_4_us_with_territories_covers_territory_alone_and_state_plus_territory() -> None:
    pr_only = footprint.classify_footprint(
        {"nct_id": "NCT99000209", "locations": [{"city": "San Juan", "country": "Puerto Rico", "zip": "00901"}]}
    )
    fl_plus_pr = footprint.classify_footprint(
        {
            "nct_id": "NCT99000210",
            "locations": [
                {"city": "Miami", "state": "Florida", "zip": "33136", "country": "United States"},
                {"city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
            ],
        }
    )
    assert pr_only["footprint_tier"] == "us_with_territories"
    assert fl_plus_pr["footprint_tier"] == "us_with_territories"
    assert fl_plus_pr["n_us_state_or_dc_facilities"] == 1
    assert fl_plus_pr["n_us_territory_facilities"] == 1
    assert fl_plus_pr["n_us_facilities"] == 2
    assert fl_plus_pr["global_pooled_baseline_table"] is False
    assert fl_plus_pr["eligible_for_us_multisite_rollup"] is False


def test_tier_5_mixed_us_and_international_sets_global_pooled_warning_flag() -> None:
    mixed = footprint.classify_footprint(
        {
            "nct_id": "NCT99000211",
            "locations": [
                {"city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                {"city": "Toronto", "state": "Ontario", "zip": "M5G 2C4", "country": "Canada"},
            ],
        }
    )
    pr_plus_spain = footprint.classify_footprint(
        {
            "nct_id": "NCT99000212",
            "locations": [
                {"city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
                {"city": "Madrid", "country": "Spain", "zip": "28046"},
            ],
        }
    )
    assert mixed["footprint_tier"] == "mixed_us_and_international"
    assert mixed["global_pooled_baseline_table"] is True
    assert mixed["eligible_for_us_multisite_rollup"] is False
    assert mixed["us_facility_share"] == pytest.approx(0.5)
    assert mixed["foreign_countries"] == ["Canada"]
    assert pr_plus_spain["footprint_tier"] == "mixed_us_and_international"
    assert pr_plus_spain["global_pooled_baseline_table"] is True


def test_tier_6_non_us_only() -> None:
    fp = footprint.classify_footprint(
        {
            "nct_id": "NCT99000213",
            "locations": [
                {"city": "London", "country": "United Kingdom", "zip": "SW1A 1AA"},
                {"city": "Paris", "country": "France", "zip": "75001"},
            ],
        }
    )
    assert fp["footprint_tier"] == "non_us_only"
    assert fp["n_us_facilities"] == 0 and fp["n_non_us_facilities"] == 2
    assert fp["global_pooled_baseline_table"] is False


# --- Per-level geographic resolution accounting & HHI concentration -------------------------


def test_resolve_us_facilities_computes_counts_distinct_areas_and_hhi() -> None:
    resolver = DummyResolver(
        {
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
            "77002": {"zcta": "77002", "county": "48201", "cbsa": "26420"},
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
        }
    )
    # 3 facilities in Houston CBSA 26420 (2 in 77030, 1 in 77002) + 1 in Boston CBSA 14460
    facilities = [
        {"facility": "Clinic 1", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Clinic 2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Clinic 3", "city": "Houston", "state": "TX", "zip": "77002", "country": "United States"},
        {"facility": "Clinic 4", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
    ]
    res = footprint.resolve_us_facilities(facilities, resolver)
    assert res["n_us_facilities"] == 4

    zcta = res["by_level"]["zcta"]
    assert zcta["all_us_facilities_resolved"] is True
    assert zcta["n_distinct_areas"] == 3
    assert zcta["facility_counts_by_geoid"] == {"02114": 1, "77002": 1, "77030": 2}
    # HHI = (2/4)^2 + (1/4)^2 + (1/4)^2 = 0.25 + 0.0625 + 0.0625 = 0.375
    assert zcta["facility_hhi"] == pytest.approx(0.375)

    cbsa = res["by_level"]["cbsa"]
    assert cbsa["facility_counts_by_geoid"] == {"14460": 1, "26420": 3}
    # HHI = (3/4)^2 + (1/4)^2 = 0.5625 + 0.0625 = 0.625
    assert cbsa["facility_hhi"] == pytest.approx(0.625)

    us = res["by_level"]["us"]
    assert us["facility_counts_by_geoid"] == {"1": 4}
    assert us["facility_hhi"] == 1.0


def test_resolve_us_facilities_tracks_partial_resolution_and_territory_exclusions() -> None:
    resolver = DummyResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "59715": {"zcta": "59715", "county": "30031", "cbsa": None},
        }
    )
    facilities = [
        {"facility": "Boston Site", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
        {"facility": "Rural Site", "city": "Bozeman", "state": "MT", "zip": "59715", "country": "United States"},
        {"facility": "PR Site", "city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
        {"facility": "Foreign Site", "city": "Toronto", "country": "Canada", "zip": "M5G 2C4"},
    ]
    res = footprint.resolve_us_facilities(facilities, resolver)
    # Foreign site excluded; 2 states + 1 territory = 3 US facilities
    assert res["n_us_facilities"] == 3

    cbsa = res["by_level"]["cbsa"]
    assert cbsa["n_resolved_facilities"] == 1
    assert cbsa["n_unresolved_facilities"] == 2
    assert cbsa["all_us_facilities_resolved"] is False
    assert cbsa["unresolved_reasons"] == {
        "mock_missing_cbsa": 1,
        "us_territory_outside_50_states_dc_acs_frame": 1,
    }
    assert cbsa["facility_hhi"] == 1.0

    # Even at 'us' national level, the Puerto Rico site prevents unconditional 50-state+DC resolution
    assert res["by_level"]["us"]["all_us_facilities_resolved"] is False
    assert res["by_level"]["us"]["n_resolved_facilities"] == 2


def test_zero_us_facilities_and_missing_resolver_produce_false_all_resolved_and_none_hhi() -> None:
    empty_res = footprint.resolve_us_facilities(
        [{"city": "Paris", "country": "France"}], DummyResolver({})
    )
    for lvl in footprint.LEVEL_ORDER:
        assert empty_res["by_level"][lvl]["n_us_facilities"] == 0
        assert empty_res["by_level"][lvl]["all_us_facilities_resolved"] is False
        assert empty_res["by_level"][lvl]["facility_hhi"] is None

    no_win = footprint.resolve_us_facilities(
        [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
        None,
        no_resolver_reason="no_benchmark_window",
    )
    assert no_win["by_level"]["cbsa"]["all_us_facilities_resolved"] is False
    assert no_win["by_level"]["cbsa"]["unresolved_reasons"] == {"no_benchmark_window": 1}
    assert no_win["by_level"]["cbsa"]["facility_hhi"] is None


def test_exhaustive_partition_over_state_territory_foreign_facility_counts() -> None:
    """Every (n_state_dc, n_territory, n_foreign) triple in {0, 1, 2}^3 maps to one valid tier."""
    for n_s in (0, 1, 2):
        for n_t in (0, 1, 2):
            for n_f in (0, 1, 2):
                locs: list[dict[str, str]] = []
                for i in range(n_s):
                    locs.append(
                        {
                            "facility": f"US Site {i}",
                            "city": "Boston",
                            "state": "Massachusetts",
                            "zip": "02114",
                            "country": "United States",
                        }
                    )
                for i in range(n_t):
                    locs.append(
                        {
                            "facility": f"Territory Site {i}",
                            "city": "San Juan",
                            "country": "Puerto Rico",
                            "zip": "00901",
                        }
                    )
                for i in range(n_f):
                    locs.append(
                        {
                            "facility": f"Foreign Site {i}",
                            "city": "Paris",
                            "country": "France",
                            "zip": "75001",
                        }
                    )
                fp = footprint.classify_footprint({"nct_id": "NCT99000214", "locations": locs})
                tier = fp["footprint_tier"]
                assert tier in footprint.FOOTPRINT_TIERS
                if n_s + n_t + n_f == 0:
                    assert tier == "no_facilities_listed"
                elif n_s + n_t == 0:
                    assert tier == "non_us_only"
                elif n_f > 0:
                    assert tier == "mixed_us_and_international"
                    assert fp["global_pooled_baseline_table"] is True
                elif n_t > 0:
                    assert tier == "us_with_territories"
                elif n_s == 1:
                    assert tier == "single_us_facility"
                else:
                    assert tier == "multi_site_us_only"


def test_country_omitted_jurisdiction_inference_is_conservative_on_two_letter_codes() -> None:
    full_state = footprint.normalize_facility({"city": "Los Angeles", "state": "California", "zip": "90095"})
    abbr_with_zip = footprint.normalize_facility({"city": "Los Angeles", "state": "CA", "zip": "90095"})
    abbr_without_zip = footprint.normalize_facility({"city": "Unknown", "state": "CA"})
    assert full_state["jurisdiction"] == "us_state_or_dc" and full_state["country"] == "United States"
    assert abbr_with_zip["jurisdiction"] == "us_state_or_dc" and abbr_with_zip["state"] == "California"
    assert abbr_without_zip["jurisdiction"] == "non_us"


def test_name_and_postal_code_fallbacks_and_deduplicate_false_option() -> None:
    locs = [
        {"name": "Dana-Farber", "city": "Boston", "state": "MA", "postalCode": "02215-5450", "country": "United States"},
        {"name": "Dana-Farber", "city": "Boston", "state": "MA", "postalCode": "02215", "country": "United States"},
    ]
    fp_no_dedup = footprint.classify_footprint({"nct_id": "NCT99000215", "locations": locs}, deduplicate=False)
    assert fp_no_dedup["n_facilities"] == 2
    assert fp_no_dedup["n_duplicate_locations_removed"] == 0
    assert fp_no_dedup["facilities"][0]["zip5"] == "02215"


def test_resolver_missing_level_keys_handled_with_explicit_reason() -> None:
    class PartialResolver:
        def resolve(
            self,
            zip5: str | None,
            point: tuple[float, float] | None,
            state_name: str | None,
        ) -> dict[str, Any]:
            return {"us": {"geoid": "1", "method": "national"}}

    res = footprint.resolve_us_facilities(
        [{"facility": "Clinic", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
        PartialResolver(),
    )
    assert res["by_level"]["us"]["all_us_facilities_resolved"] is True
    assert res["by_level"]["cbsa"]["all_us_facilities_resolved"] is False
    assert res["by_level"]["cbsa"]["unresolved_reasons"] == {"level_not_returned_by_resolver": 1}

