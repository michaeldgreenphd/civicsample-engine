"""Unit tests for dap.geography and dap.demographics offline domain invariants."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_FX_SPEC = importlib.util.spec_from_file_location(
    "dap_fixture", Path(__file__).resolve().parent / "fixture.py"
)
assert _FX_SPEC is not None and _FX_SPEC.loader is not None
fx = sys.modules.setdefault("dap_fixture", importlib.util.module_from_spec(_FX_SPEC))
if not hasattr(fx, "make_raw_ctgov_study"):
    _FX_SPEC.loader.exec_module(fx)

from dap import UNIT_BASIS  # noqa: E402
from dap.demographics import (  # noqa: E402
    SEX_GENDER_STATES,
    evaluate_unknown_bounds,
    extract_demographics,
    extract_sex_gender,
)
from dap.geography import (  # noqa: E402
    SITE_FOOTPRINT_CLASSES,
    classify_us_site_share_bin,
    extract_geography,
)
from src.utils import get_study_metadata  # noqa: E402


@pytest.mark.parametrize(
    ("n_us", "n_total", "expected_bin"),
    [
        (1, 10, "us_share_lt_25"),
        (24, 100, "us_share_lt_25"),
        (1, 4, "us_share_25_to_lt_50"),
        (25, 100, "us_share_25_to_lt_50"),
        (49, 100, "us_share_25_to_lt_50"),
        (2, 4, "us_share_50_to_lt_75"),
        (50, 100, "us_share_50_to_lt_75"),
        (74, 100, "us_share_50_to_lt_75"),
        (3, 4, "us_share_gte_75"),
        (75, 100, "us_share_gte_75"),
        (99, 100, "us_share_gte_75"),
    ],
)
def test_us_site_share_bins_exact_rational_cutpoints(
    n_us: int, n_total: int, expected_bin: str
) -> None:
    assert classify_us_site_share_bin(n_us, n_total) == expected_bin


def test_us_site_share_bin_rejects_degenerate_counts() -> None:
    with pytest.raises(ValueError):
        classify_us_site_share_bin(0, 5)
    with pytest.raises(ValueError):
        classify_us_site_share_bin(5, 5)
    with pytest.raises(ValueError):
        classify_us_site_share_bin(1, 0)


def test_weekly_study_sites_never_assert_a_footprint_class() -> None:
    """Weekly study_sites drop countryless locations (src/utils.py), so every weekly trial is footprint_unknown."""
    us = {"facility": "US1", "state": "NY", "country": "United States"}
    de = {"facility": "DE1", "country": "Germany"}
    countryless = {"facility": "Site B", "city": "Somewhere"}
    cases = {
        # raw locations -> class from the complete raw list
        "us_plus_countryless": ([us, countryless], "facilities_missing_country"),
        "ex_us_plus_countryless": ([de, countryless], "facilities_missing_country"),
        "all_countryless": ([countryless, countryless], "facilities_missing_country"),
        "us_plus_de": ([us, de], "multinational_with_us_sites"),
        "us_only": ([us], "us_only"),
        "none": ([], "no_listed_facilities"),
    }
    for name, (locations, raw_class) in cases.items():
        raw = fx.make_raw_ctgov_study("NCT99000290", locations=locations)
        assert extract_geography(raw)["site_footprint_class"] == raw_class, name

        weekly = {"nct_id": "NCT99000290", "study_sites": get_study_metadata(raw)["study_sites"]}
        assert len(weekly["study_sites"]) == sum(1 for loc in locations if loc.get("country")), name
        g = extract_geography(weekly)
        assert g["location_source"] == "weekly_study_sites", name
        assert g["site_footprint_class"] == "footprint_unknown", name
        assert g["us_site_share_bin"] is None, name
        assert g["footprint_unknown_reason"].startswith("weekly study_sites omit every location without a country")
        # Counts that depend on the dropped sites are unknown, not zero ...
        assert (g["n_listed_facilities"], g["n_facilities_missing_country"], g["partial_missing_country"]) == (
            None,
            None,
            None,
        ), name
        # ... while counts over sites with a country stay exact.
        assert g["n_us_facilities"] == sum(1 for loc in locations if loc.get("country") == "United States"), name
        assert g["n_facilities_with_country"] == len(weekly["study_sites"]), name
    assert "footprint_unknown" in SITE_FOOTPRINT_CLASSES
    raw_us = extract_geography(fx.make_raw_ctgov_study("NCT99000291", locations=[us]))
    assert "footprint_unknown_reason" not in raw_us


def test_geography_five_footprint_classes_and_unit_basis() -> None:
    # 1. us_only (including US territories under FDA jurisdiction)
    us_trial = fx.make_raw_ctgov_study(
        "NCT99000201",
        locations=[
            {"facility": "Mass General", "state": "MA", "country": "United States"},
            {"facility": "SJ Clinic", "country": "Puerto Rico"},
            {"facility": "Guam Site", "country": "Guam"},
        ],
    )
    g_us = extract_geography(us_trial)
    assert g_us["unit_basis"] == UNIT_BASIS
    assert g_us["site_footprint_class"] == "us_only"
    assert g_us["us_site_share_bin"] is None
    assert g_us["us_facility_share"] == 1.0
    assert g_us["n_us_facilities"] == 3
    assert g_us["us_state_or_territory_counts"] == {
        "Guam": 1,
        "Massachusetts": 1,
        "Puerto Rico": 1,
    }

    # 2. multinational_with_us_sites
    multi = fx.make_raw_ctgov_study(
        "NCT99000202",
        locations=[
            {"facility": "US1", "state": "California", "country": "United States"},
            {"facility": "DE1", "country": "Germany"},
            {"facility": "FR1", "country": "France"},
            {"facility": "UK1", "country": "United Kingdom"},
        ],
    )
    g_multi = extract_geography(multi)
    assert g_multi["unit_basis"] == UNIT_BASIS
    assert g_multi["site_footprint_class"] == "multinational_with_us_sites"
    assert g_multi["us_facility_share"] == 0.25
    assert g_multi["us_site_share_bin"] == "us_share_25_to_lt_50"

    # 3. ex_us_only
    exus = fx.make_raw_ctgov_study(
        "NCT99000203",
        locations=[{"facility": "DE1", "country": "Germany"}, {"facility": "JP1", "country": "Japan"}],
    )
    g_exus = extract_geography(exus)
    assert g_exus["site_footprint_class"] == "ex_us_only"
    assert g_exus["us_facility_share"] == 0.0
    assert g_exus["us_site_share_bin"] is None

    # 4a. facilities_missing_country (all missing)
    miss_all = fx.make_raw_ctgov_study(
        "NCT99000204",
        locations=[{"facility": "Site A", "city": "Boston"}],
    )
    g_miss = extract_geography(miss_all)
    assert g_miss["site_footprint_class"] == "facilities_missing_country"
    assert g_miss["us_facility_share"] is None
    assert g_miss["us_site_share_bin"] is None

    # 4b. facilities_missing_country (1 US + 1 missing country: never falsely called us_only!)
    miss_partial = fx.make_raw_ctgov_study(
        "NCT99000205",
        locations=[
            {"facility": "Site A", "state": "NY", "country": "United States"},
            {"facility": "Site B"},
        ],
    )
    g_part = extract_geography(miss_partial)
    assert g_part["site_footprint_class"] == "facilities_missing_country"
    assert g_part["partial_missing_country"] is True
    assert g_part["us_facility_share"] == 1.0  # n_us_facilities (1) / n_facilities_with_country (1)
    assert g_part["us_site_share_bin"] is None

    # 5. no_listed_facilities
    none_loc = fx.make_raw_ctgov_study("NCT99000206", locations=[])
    g_none = extract_geography(none_loc)
    assert g_none["site_footprint_class"] == "no_listed_facilities"
    assert g_none["us_facility_share"] is None
    assert g_none["n_listed_facilities"] == 0


def test_combined_race_ethnicity_quarantined_excludes_counts_and_keeps_audit_detail() -> None:
    study = fx.make_raw_ctgov_study(
        "NCT99000210",
        locations=[{"facility": "S1", "state": "TX", "country": "United States"}],
        baseline_denom=100,
        measures=[
            fx.make_measure(
                "Race/Ethnicity, Customized",
                [
                    ("White", "55"),
                    ("Black or African American", "20"),
                    ("Hispanic or Latino", "20"),
                    ("Unknown", "5"),
                ],
            )
        ],
    )
    demo = extract_demographics(study)
    assert demo["race_table_status"] == "combined_race_ethnicity_quarantined"
    assert demo["race"]["quarantined_from_omb_pooling"] is True
    assert demo["race"]["usable_for_omb_pooling"] is False
    assert demo["race"]["counts"] is None
    assert demo["race"]["of_baseline_total"] is None
    assert demo["race"]["of_classified"] is None
    assert "Hispanic or Latino" in demo["race"]["quarantined_ethnicity_labels"]

    # Meanwhile, a title saying "Race/Ethnicity, Customized" whose rows are PURE OMB race
    # is NOT quarantined (matching census/trials.py).
    pure_race_study = fx.make_raw_ctgov_study(
        "NCT99000211",
        locations=[{"facility": "S1", "state": "TX", "country": "United States"}],
        baseline_denom=100,
        measures=[
            fx.make_measure(
                "Race/Ethnicity, Customized",
                [("White", "70"), ("Black or African American", "20"), ("Asian", "10")],
            )
        ],
    )
    pure_demo = extract_demographics(pure_race_study)
    assert pure_demo["race_table_status"] == "omb_compatible"
    assert pure_demo["race"]["usable_for_omb_pooling"] is True
    assert pure_demo["race"]["of_baseline_total"]["white"]["floor"] == pytest.approx(0.70)

    # Pre-PR-#99 weekly releases emitted skipped_ethnicity_rows as [[label, count], ...]
    # rather than [{"original": ..., "count": ...}]; both shapes MUST populate
    # quarantined_ethnicity_labels and quarantine the race block.
    legacy_weekly = {
        "nct_id": "NCT99000215",
        "results_posted": True,
        "study_sites": [{"country": "United States", "state": "TX"}],
        "race": {
            "reported": True,
            "omb_totals": {
                "white": 60,
                "black_african_american": 20,
                "unknown_not_reported": 20,
            },
            "raw_categories": [
                {"omb_category": "white", "original": "White", "count": 60},
                {"omb_category": "black_african_american", "original": "Black", "count": 20},
            ],
            "skipped_ethnicity_rows": [["Hispanic", 15], ("Latino", 5)],
            "flags": ["ethnicity_rows_skipped"],
        },
    }
    legacy_demo = extract_demographics(legacy_weekly)
    assert legacy_demo["race_table_status"] == "combined_race_ethnicity_quarantined"
    assert legacy_demo["race"]["quarantined_ethnicity_labels"] == ["Hispanic", "Latino"]
    assert legacy_demo["race"]["counts"] is None

    dict_weekly = {
        **legacy_weekly,
        "nct_id": "NCT99000216",
        "race": {
            **legacy_weekly["race"],
            "skipped_ethnicity_rows": [{"original": "Hispanic or Latino", "count": 20}],
        },
    }
    dict_demo = extract_demographics(dict_weekly)
    assert dict_demo["race_table_status"] == "combined_race_ethnicity_quarantined"
    assert dict_demo["race"]["quarantined_ethnicity_labels"] == ["Hispanic or Latino"]


def test_single_category_ethnicity_withholds_of_classified_and_keeps_denominator_a() -> None:
    study = fx.make_raw_ctgov_study(
        "NCT99000212",
        locations=[{"facility": "S1", "state": "FL", "country": "United States"}],
        baseline_denom=100,
        measures=[
            fx.make_measure(
                "Ethnicity, Customized",
                [("Hispanic or Latino", "18")],
            )
        ],
    )
    demo = extract_demographics(study)
    assert demo["ethnicity_table_status"] == "single_category_ethnicity"
    assert demo["ethnicity"]["of_classified"] is None
    assert demo["ethnicity"]["of_classified_withheld_reason"] == "single_category_ethnicity"
    hisp_a = demo["ethnicity"]["of_baseline_total"]["hispanic_latino"]
    assert hisp_a["floor"] == pytest.approx(0.18)
    assert hisp_a["unknown_bounds"] == pytest.approx([0.18, 1.0])

    # Also test classified_total == 0 (all participants unknown): of_classified withheld as None.
    all_unknown = {
        "nct_id": "NCT99000213",
        "results_posted": True,
        "study_sites": [{"country": "United States", "state": "NY"}],
        "race": {
            "reported": True,
            "omb_totals": {
                "white": 0,
                "black_african_american": 0,
                "asian": 0,
                "unknown_not_reported": 50,
            },
            "raw_categories": [{"omb_category": "unknown_not_reported", "original": "Unknown", "count": 50}],
        },
    }
    demo_unk = extract_demographics(all_unknown)
    assert demo_unk["race"]["classified_total"] == 0
    assert demo_unk["race"]["of_classified"] is None
    assert demo_unk["race"]["of_classified_withheld_reason"] == "classified_total_is_zero"


def test_two_denominators_unknown_bounds_and_direction_settled() -> None:
    # 20 reported out of 100, 15 unknown, 10 unmapped -> floor=0.20, ceiling=0.45
    unsettled = evaluate_unknown_bounds(20, 15, 10, 100, reference_median=0.30)
    assert unsettled["unknown_bounds"] == pytest.approx([0.20, 0.45])
    assert unsettled["direction_settled"] is False

    settled_below = evaluate_unknown_bounds(20, 5, 2, 100, reference_median=0.30)
    assert settled_below["unknown_bounds"] == pytest.approx([0.20, 0.27])
    assert settled_below["direction_settled"] is True

    settled_above = evaluate_unknown_bounds(35, 15, 0, 100, reference_median=0.30)
    assert settled_above["unknown_bounds"] == pytest.approx([0.35, 0.50])
    assert settled_above["direction_settled"] is True

    # Absent reference_median MUST produce None, never False.
    no_ref = evaluate_unknown_bounds(20, 15, 10, 100, reference_median=None)
    assert no_ref["direction_settled"] is None
    assert no_ref["diff_at_floor"] is None


def test_non_standard_categories_accounted_in_unmapped_bounds() -> None:
    study = fx.make_raw_ctgov_study(
        "NCT99000214",
        locations=[{"facility": "S1", "state": "CA", "country": "United States"}],
        baseline_denom=100,
        measures=[
            fx.make_measure(
                "Race, Customized",
                [
                    ("White", "60"),
                    ("Black or African American", "10"),
                    ("Mestizo Amerindian", "20"),
                    ("Unknown", "10"),
                ],
            )
        ],
    )
    demo = extract_demographics(study)
    assert demo["race_table_status"] == "non_standard_categories_present"
    assert demo["race"]["unmapped_count"] == 20
    assert demo["race"]["unknown_count"] == 10
    assert demo["race"]["classified_total"] == 70
    assert demo["race"]["baseline_total"] == 100
    # Black has 10 reported + 10 unknown + 20 unmapped = [0.10, 0.40]
    assert demo["race"]["of_baseline_total"]["black_african_american"]["unknown_bounds"] == pytest.approx(
        [0.10, 0.40]
    )
    assert demo["race"]["of_classified"]["black_african_american"] == pytest.approx(10 / 70)


def test_sex_gender_preserves_all_five_states_and_never_folds_enrollment_gap() -> None:
    # 1. reported with enrollment gap (150 enrolled vs 100 parsed)
    s_rep = fx.make_raw_ctgov_study(
        "NCT99000221",
        enrollment=150,
        baseline_denom=100,
        measures=[fx.make_measure("Sex: Female, Male", [("Female", "55"), ("Male", "40"), ("Unknown", "5")])],
    )
    sg_rep = extract_sex_gender(s_rep, snapshot_date="2026-11-21")
    assert sg_rep["sex_report_status"] == "reported"
    assert sg_rep["n_unknown"] == 5.0
    assert sg_rep["enrollment_minus_parsed"] == 50.0
    # Parsed denominator is 55 + 40 + 5 = 100 (NEVER 150!)
    assert sg_rep["of_parsed_total"]["female"]["baseline_total"] == 100
    assert sg_rep["of_parsed_total"]["female"]["unknown_bounds"] == pytest.approx([0.55, 0.60])
    assert sg_rep["percent_female"] == pytest.approx(100.0 * 55 / 95)

    # 2. explicit_unknown_only
    s_euo = fx.make_raw_ctgov_study(
        "NCT99000222",
        enrollment=80,
        baseline_denom=50,
        measures=[fx.make_measure("Sex: Female, Male", [("Unknown", "50")])],
    )
    sg_euo = extract_sex_gender(s_euo)
    assert sg_euo["sex_report_status"] == "explicit_unknown_only"
    assert sg_euo["n_unknown"] == 50.0
    assert sg_euo["enrollment_minus_parsed"] == 30.0
    assert sg_euo["female_share"] is None

    # 3. uninformative
    s_uninf = fx.make_raw_ctgov_study(
        "NCT99000223",
        enrollment=40,
        baseline_denom=40,
        measures=[fx.make_measure("Sex: Female, Male", [("Female", "0"), ("Male", "0")])],
    )
    sg_uninf = extract_sex_gender(s_uninf)
    assert sg_uninf["sex_report_status"] == "uninformative"
    assert sg_uninf["female_share"] is None

    # 4. not_reported
    s_nr = fx.make_raw_ctgov_study("NCT99000224", enrollment=40, baseline_denom=40, measures=[])
    sg_nr = extract_sex_gender(s_nr)
    assert sg_nr["sex_report_status"] == "not_reported"
    assert sg_nr["n_unknown"] is None
    assert sg_nr["enrollment_minus_parsed"] is None

    # 5. parse_error preserved from precomputed row
    pre_err: dict[str, Any] = {
        "nct_id": "NCT99000225",
        "sex_gender": {
            "sex_report_status": "parse_error",
            "reported_sex": None,
            "reported_gender": None,
            "reported_both": None,
            "n_female": None,
            "n_male": None,
            "n_unknown": None,
            "n_gender_diverse": None,
            "n_ambiguous_gender": None,
            "is_participant_count": None,
            "enrollment_minus_parsed": None,
            "parser_rules_version": "1.0.0",
        },
    }
    sg_pe = extract_sex_gender(pre_err)
    assert sg_pe["sex_report_status"] == "parse_error"
    assert sg_pe["reported_sex"] is None
    assert sg_pe["female_share"] is None

    assert set(SEX_GENDER_STATES) == {
        sg_rep["sex_report_status"],
        sg_euo["sex_report_status"],
        sg_uninf["sex_report_status"],
        sg_nr["sex_report_status"],
        sg_pe["sex_report_status"],
    }

    # COUNT_OF_UNITS stays in reporting counts and out of composition
    s_cou = fx.make_raw_ctgov_study(
        "NCT99000226",
        measures=[
            fx.make_measure(
                "Sex: Female, Male",
                [("Female", "20"), ("Male", "30")],
                param_type="COUNT_OF_UNITS",
                unit_of_measure="Lesions",
            )
        ],
    )
    sg_cou = extract_sex_gender(s_cou)
    assert sg_cou["reported_sex"] is True
    assert sg_cou["is_participant_count"] is False
    assert sg_cou["percent_female"] is None
    assert sg_cou["female_share"] is None


def test_us_site_omission_audit_tracks_posted_us_trials_and_nulls_out_of_scope() -> None:
    posted_us_missing = fx.make_raw_ctgov_study(
        "NCT99000231",
        locations=[{"facility": "US1", "state": "MA", "country": "United States"}],
        has_results=True,
        measures=[fx.make_measure("Sex: Female, Male", [("Female", "25"), ("Male", "25")])],
    )
    d1 = extract_demographics(posted_us_missing)
    aud1 = d1["us_site_omission_audit"]
    assert aud1["in_posted_us_site_audit_denominator"] is True
    assert aud1["omitted_race_table"] is True
    assert aud1["omitted_ethnicity_table"] is True
    assert aud1["omitted_both_race_and_ethnicity_tables"] is True
    assert aud1["omitted_sex_table"] is False

    exus_missing = fx.make_raw_ctgov_study(
        "NCT99000232",
        locations=[{"facility": "DE1", "country": "Germany"}],
        has_results=True,
        measures=[],
    )
    d2 = extract_demographics(exus_missing)
    aud2 = d2["us_site_omission_audit"]
    assert aud2["in_posted_us_site_audit_denominator"] is False
    assert aud2["omitted_race_table"] is None
    assert aud2["omitted_ethnicity_table"] is None
