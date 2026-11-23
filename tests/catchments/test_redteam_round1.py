"""Round-1 adversarial red-team and mathematical/property fuzz tests for ``catchments/``.

Covers five adversarial attack surfaces without modifying ``catchments/``:
  1. Simplex / convex-hull invariant fuzzing across 500+ synthetic ``multi_site_us_only``
     trials (``NCT99100001``..``NCT99100500``) plus targeted sub-0.001 pp boundary fuzzing
     that exposes premature 3-decimal rounding before strict settlement inequalities.
  2. ACS 90% MOE covariance deduplication (``R = 10`` clinics in 1 CBSA), controlled
     ``-555555555`` MOEs, ``NaN``/``Inf`` MOEs, zero-denominator areas, negative sentinel
     estimates (``est = NaN``), zero reference shares, and strict ``json.dumps(...,
     allow_nan=False)`` validation across trial and cohort outputs.
  3. Facility deduplication, foreign 5-digit postal codes, US territory alias / dotted
     abbreviation / ZIP classification traps, coordinate bounds, and malformed locations.
  4. Unresolved-site and unavailable-ACS settlement leak guards across geography hierarchies
     and 2005-2009 ZCTA publication provenance consistency.
  5. Baseline table ``category_structure`` rejections, unharmonised/unmapped participant
     ceilings, single-category ethnicity Denominator B withholding, cohort pooling
     isolation, ``floor``/``resamples``/``n=0`` edge cases, and ``README.md`` contract checks.
"""
from __future__ import annotations

import ast
import copy
import gzip
import inspect
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import catchments
from catchments import benchmark as ms_benchmark, bounds, footprint
from census import acs, benchmark as census_benchmark, geography


COARSE_SPANS: list[tuple[str, str]] = [
    ("a", "Under 18 years"),
    ("b", "18 to 64 years"),
    ("c", "65 years and over"),
]
FINE_SPANS: list[tuple[str, str]] = [
    ("a", "Under 18 years"),
    ("b1", "18 to 39 years"),
    ("b2", "40 to 64 years"),
    ("c", "65 years and over"),
]
EDGES_3: list[tuple[float, float | None]] = [
    (0.0, 18.0),
    (18.0, 65.0),
    (65.0, None),
]


def _make_variables(spans: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"T_001E": {"label": "Estimate!!Total:"}}
    idx = 2
    for sex in ("Male", "Female"):
        out[f"T_{idx:03d}E"] = {"label": f"Estimate!!Total:!!{sex}:"}
        idx += 1
        for _, label in spans:
            out[f"T_{idx:03d}E"] = {"label": f"Estimate!!Total:!!{sex}:!!{label}"}
            out[f"T_{idx:03d}M"] = {"label": f"Margin of Error!!Total:!!{sex}:!!{label}"}
            idx += 1
    return out


def _build_synthetic_level(
    areas: list[tuple[str, str, float, dict[str, float], float | dict[str, float]]],
) -> acs.Level:
    """Build an in-memory ``acs.Level`` on ``EDGES_3`` where adult band ``[18, 65)`` is index 1."""
    n_areas = len(areas)
    n_tables = len(acs.TABLE_ORDER)
    est = np.zeros((n_areas, n_tables, 2, 3), dtype=float)
    moe = np.zeros((n_areas, n_tables, 2, 3), dtype=float)
    geoids: list[str] = []
    names: list[str] = []
    t_tot = acs.TABLE_ORDER.index("total")

    for i, (geoid, name, adult_total, cat_adults, moe_spec) in enumerate(areas):
        geoids.append(geoid)
        names.append(name)
        est[i, t_tot, 0, 1] = adult_total / 2.0
        est[i, t_tot, 1, 1] = adult_total / 2.0
        default_m = moe_spec if isinstance(moe_spec, (int, float)) else 5.0
        moe[i, t_tot, :, 1] = float(default_m)
        for cat, count_val in cat_adults.items():
            t_idx = acs.TABLE_ORDER.index(cat)
            est[i, t_idx, 0, 1] = count_val / 2.0
            est[i, t_idx, 1, 1] = count_val / 2.0
            m_val = (
                moe_spec.get(cat, 2.0)
                if isinstance(moe_spec, dict)
                else float(moe_spec)
            )
            moe[i, t_idx, :, 1] = float(m_val)

    return acs.Level(geoids, names, list(EDGES_3), est, moe)


class TableResolver:
    """Deterministic offline duck-typed geography resolver keyed by ``zip5``."""

    def __init__(self, zip_map: dict[str, dict[str, str | None]]) -> None:
        self.zip_map = zip_map

    def resolve(
        self,
        zip5: str | None,
        point: tuple[float, float] | None,
        state_name: str | None,
    ) -> dict[str, Any]:
        entry = self.zip_map.get(zip5 or "")
        out: dict[str, Any] = {}
        if entry is not None:
            for lvl in ("zcta", "county", "cbsa"):
                code = entry.get(lvl)
                if code:
                    out[lvl] = {"geoid": code, "method": "synthetic_lookup"}
                else:
                    out[lvl] = {
                        "geoid": None,
                        "reason": (
                            "outside_any_metro_or_micro_area"
                            if lvl == "cbsa"
                            else f"unresolved_{lvl}"
                        ),
                    }
        else:
            out["zcta"] = {
                "geoid": None,
                "reason": (
                    "no_zip_in_registration"
                    if not zip5
                    else "zip_has_no_zcta_in_this_window"
                ),
            }
            out["county"] = {
                "geoid": None,
                "reason": "no_zcta_polygon_and_no_coordinates",
            }
            out["cbsa"] = {
                "geoid": None,
                "reason": "outside_any_metro_or_micro_area",
            }

        st_fips = geography.STATE_FIPS.get(state_name or "")
        if st_fips:
            out["state"] = {"geoid": st_fips, "method": "registered_state_name"}
            out["region"] = {
                "geoid": geography.REGION[st_fips],
                "method": "census_region_of_state",
            }
        else:
            out["state"] = {"geoid": None, "reason": "state_not_recognised"}
            out["region"] = {"geoid": None, "reason": "state_not_in_a_census_region"}
        out["us"] = {"geoid": "1", "method": "national"}
        return out


@pytest.fixture(scope="module")
def fuzz_catalog() -> tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]]:
    """12-site US catalog across 8 CBSAs, 8 counties, 6 states, 4 regions, and US."""
    site_pool: list[tuple[str, str, str, str, str]] = [
        ("02114", "25025", "14460", "Massachusetts", "Boston"),
        ("02115", "25025", "14460", "Massachusetts", "Boston"),
        ("10065", "36061", "35620", "New York", "New York"),
        ("19104", "42101", "37980", "Pennsylvania", "Philadelphia"),
        ("30322", "13089", "12060", "Georgia", "Atlanta"),
        ("33136", "12086", "33100", "Florida", "Miami"),
        ("60611", "17031", "16980", "Illinois", "Chicago"),
        ("77030", "48201", "26420", "Texas", "Houston"),
        ("77002", "48201", "26420", "Texas", "Houston"),
        ("90095", "06037", "31080", "California", "Los Angeles"),
        ("94143", "06075", "41860", "California", "San Francisco"),
        ("98195", "53033", "42660", "Washington", "Seattle"),
    ]
    zip_map = {
        z: {"zcta": z, "county": cty, "cbsa": cbsa}
        for z, cty, cbsa, _, _ in site_pool
    }
    resolver = TableResolver(zip_map)

    rng = np.random.default_rng(20261123)

    def _make_level_for_codes(codes: list[str]) -> acs.Level:
        areas = []
        for idx, code in enumerate(sorted(set(codes))):
            pop = float(rng.integers(2_000, 40_000) * 2)
            shares = rng.dirichlet([3.0, 1.8, 1.2, 0.4, 0.2, 0.5, 0.6])
            race_keys = [
                "white",
                "black_african_american",
                "asian",
                "american_indian_alaska_native",
                "native_hawaiian_pacific_islander",
                "more_than_one_race",
                "other",
            ]
            counts = {rk: round(pop * float(sh), 2) for rk, sh in zip(race_keys, shares)}
            counts["hispanic_latino"] = round(pop * float(rng.uniform(0.03, 0.55)), 2)
            areas.append((code, f"Area-{code}", pop, counts, float(1.5 + idx % 5)))
        return _build_synthetic_level(areas)

    levels = {
        "zcta": _make_level_for_codes([s[0] for s in site_pool]),
        "county": _make_level_for_codes([s[1] for s in site_pool]),
        "cbsa": _make_level_for_codes([s[2] for s in site_pool]),
        "state": _make_level_for_codes([geography.STATE_FIPS[s[3]] for s in site_pool]),
        "region": _make_level_for_codes(
            [geography.REGION[geography.STATE_FIPS[s[3]]] for s in site_pool]
        ),
        "us": _make_level_for_codes(["1"]),
    }
    templates = [
        {"city": city, "state": state, "zip": z, "country": "United States"}
        for z, _, _, state, city in site_pool
    ]
    return levels, resolver, templates


# =============================================================================================
# 1. SIMPLEX / CONVEX-HULL INVARIANT FUZZING (500+ SYNTHETIC MULTI-SITE TRIALS + ROUNDING TRAPS)
# =============================================================================================


def test_fuzz_500_synthetic_multisite_trials_convex_hull_and_simplex_envelope(
    fuzz_catalog: tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]],
) -> None:
    """Fuzz 500 synthetic ``multi_site_us_only`` trials (``NCT99100001``..``NCT99100500``)
    across ``M in [2, 25]`` facilities, duplicate clinics in the same CBSA/county, and
    ``unknown_pct in [0, 60]``. Verifies that under every recruitment weight vector on the
    simplex ``Delta^{M-1}`` and every split of unknown participants, the true unknown-adjusted
    and site-weighted difference lies inside ``[joint_diff_pp_min, joint_diff_pp_max]`` up to
    3-decimal rounding tolerance whenever ``bounds_unconditional == True``.
    """
    levels, resolver, templates = fuzz_catalog
    rng = np.random.default_rng(99100)
    race_cats = list(bounds.COMPARED_CATEGORIES["race"])

    # Precompute exact unrounded ACS shares at CBSA level for each template
    cbsa_level = levels["cbsa"]
    exact_cbsa_pct: dict[str, dict[str, float]] = {}
    for geoid, area_idx in cbsa_level.index.items():
        den = float(cbsa_level.est[area_idx, acs.TABLE_ORDER.index("total"), :, 1].sum())
        exact_cbsa_pct[geoid] = {
            cat: 100.0
            * float(cbsa_level.est[area_idx, acs.TABLE_ORDER.index(cat), :, 1].sum())
            / den
            for cat in race_cats
        }

    for trial_num in range(1, 501):
        nct_id = f"NCT{99100000 + trial_num}"
        m_sites = int(rng.integers(2, 26))
        chosen_indices = rng.integers(0, len(templates), size=m_sites)
        locs: list[dict[str, str]] = []
        site_cbsas: list[str] = []
        for slot_idx, tpl_idx in enumerate(chosen_indices):
            tpl = templates[int(tpl_idx)]
            locs.append(
                {
                    "facility": f"Synthetic Clinic {slot_idx + 1}",
                    **tpl,
                }
            )
            site_cbsas.append(resolver.zip_map[tpl["zip"]]["cbsa"] or "")

        n_total = int(rng.integers(50, 1500))
        unk_share = float(rng.uniform(0.0, 0.60))
        n_unk = int(round(n_total * unk_share))
        n_known = n_total - n_unk
        cat_probs = rng.dirichlet(np.ones(len(race_cats)))
        known_draws = rng.multinomial(n_known, cat_probs)
        omb_totals = {
            cat: int(cnt) for cat, cnt in zip(race_cats, known_draws)
        }
        omb_totals["unknown_not_reported"] = n_unk

        rec = {
            "nct_id": nct_id,
            "study_type": "INTERVENTIONAL",
            "enrollment": n_total,
            "min_age": "18 Years",
            "max_age": "64 Years",
            "eligibility": {"sex": "ALL"},
            "start_date": "2021-03-01",
            "primary_completion_date": "2023-03-01",
            "locations": locs,
            "race": {
                "reported": True,
                "omb_totals": omb_totals,
                "raw_categories": [
                    {"original": cat, "count": omb_totals[cat]}
                    for cat in race_cats
                    if omb_totals[cat] > 0
                ],
            },
        }
        out = ms_benchmark.benchmark_multisite_trial(rec, levels, resolver)
        assert out["footprint"]["footprint_tier"] == "multi_site_us_only"
        assert out["footprint"]["n_facilities"] == m_sites

        cbsa_bench = out["dimensions"]["race"]["benchmarks"]["cbsa"]
        assert cbsa_bench["available"] is True
        assert cbsa_bench["bounds_unconditional"] is True

        # Build weight vectors: corners, uniform, skewed Dirichlet, and random Dirichlet
        weight_vectors: list[np.ndarray] = [
            np.full(m_sites, 1.0 / m_sites),
            rng.dirichlet(np.full(m_sites, 0.15)),
            rng.dirichlet(np.ones(m_sites)),
        ]
        corner_a = np.zeros(m_sites)
        corner_a[0] = 1.0
        corner_b = np.zeros(m_sites)
        corner_b[-1] = 1.0
        weight_vectors.extend([corner_a, corner_b])

        for cat in ("white", "black_african_american", "asian"):
            comp_c = cbsa_bench["comparison"]["by_category"][cat]
            j_min = float(comp_c["joint_diff_pp_min"])
            j_max = float(comp_c["joint_diff_pp_max"])
            assert j_min <= j_max

            site_vec = np.array([exact_cbsa_pct[g][cat] for g in site_cbsas])
            for w in weight_vectors:
                weighted_ref = float((w * site_vec).sum())
                # Convex-hull containment up to 3-decimal rounding (0.0005 pp)
                assert (
                    cbsa_bench["unconditional_min_pct"][cat] - 0.001
                    <= weighted_ref
                    <= cbsa_bench["unconditional_max_pct"][cat] + 0.001
                )
                for alpha in (0.0, 0.5, 1.0):
                    true_obs = 100.0 * (omb_totals[cat] + alpha * n_unk) / n_total
                    true_diff = true_obs - weighted_ref
                    # Must sit inside [joint_diff_pp_min, joint_diff_pp_max] within rounding band
                    assert j_min - 0.002 <= true_diff <= j_max + 0.002


def test_fuzz_boundary_rounding_trap_end_to_end_integer_counts_never_lies_on_joint_settlement() -> None:
    """Adversarial rounding trap (end-to-end integer counts):
    Trial ``NCT99100501`` has ``N = 600`` participants: ``20`` Asian, ``20`` Unknown, ``560`` White.
      * Exact Asian floor = ``20 / 600 = 3.333333...%`` (rounds down to ``3.333``).
      * Exact Unknown share = ``20 / 600 = 3.333333...%`` (rounds down to ``3.333``).
      * Exact Asian ceiling if all 20 unknowns are Asian = ``40 / 600 = 6.666667%``!
    Site 1 (``14460``) has ``133,331`` Asian out of ``2,000,000`` eligible adults = ``6.66655%``
    (rounds UP to ``6.667``), and Site 2 (``26420``) has ``20.0%`` Asian.

    At recruitment allocation ``w = (1.0, 0.0)`` and unknown split ``alpha = 1.0``, the TRUE
    difference is ``6.666667% - 6.66655% = +0.000117 pp > 0`` (POSITIVE), whereas at ``alpha = 0``
    it is ``-3.333217 pp < 0`` (NEGATIVE). Because the true difference crosses zero,
    ``joint_direction_settled['asian']`` MUST be ``False``.
    """
    lvl = _build_synthetic_level(
        [
            ("14460", "Metro A", 2_000_000.0, {"asian": 133_331.0, "white": 1_866_669.0}, 5.0),
            ("26420", "Metro B", 1_000_000.0, {"asian": 200_000.0, "white": 800_000.0}, 5.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99100501",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": [
            {"facility": "Clinic A", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Clinic B", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 20, "white": 560, "unknown_not_reported": 20},
            "raw_categories": [
                {"original": "Asian", "count": 20},
                {"original": "White", "count": 560},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": lvl}, resolver)
    cbsa_asian = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]["by_category"]["asian"]

    true_ceiling = 100.0 * (20 + 20) / 600.0
    true_site_min = 100.0 * 133_331.0 / 2_000_000.0
    assert true_ceiling > true_site_min  # +0.000117 pp > 0

    assert cbsa_asian["joint_direction_settled"] is False
    assert cbsa_asian["joint_direction"] == "unsettled"


def test_fuzz_boundary_rounding_trap_equal_rational_shares_at_one_fifteenth_never_settled() -> None:
    """When trial ceiling (``(20 + 20) / 600 = 1/15``) equals Site 1's exact ACS share
    (``20 / 300 = 1/15``), the exact difference at ``w = (1, 0)`` and ``alpha = 1`` is ``0.0``,
    so strict inequality fails and ``joint_direction_settled`` MUST be ``False``.
    """
    lvl = _build_synthetic_level(
        [
            ("14460", "Metro A", 300.0, {"asian": 20.0, "white": 280.0}, 2.0),
            ("26420", "Metro B", 300.0, {"asian": 60.0, "white": 240.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99100502",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": [
            {"facility": "Clinic A", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Clinic B", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 20, "white": 560, "unknown_not_reported": 20},
            "raw_categories": [
                {"original": "Asian", "count": 20},
                {"original": "White", "count": 560},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": lvl}, resolver)
    cbsa_asian = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]["by_category"]["asian"]
    assert cbsa_asian["joint_direction_settled"] is False
    assert cbsa_asian["joint_direction"] == "unsettled"


def test_compare_with_catchment_bounds_unrounded_float_inputs_never_flip_strict_inequality() -> None:
    """Direct ``site_allocation_bounds`` + ``compare_with_catchment_bounds`` with unrounded
    floats within ``0.0004`` pp of ``min_site_share``: ``obs_floor = 3.3334``, ``unk = 3.3334``
    gives true ceiling ``6.6668 > 6.6666 = min_site_share``, so ``joint_direction_settled``
    MUST be ``False``.
    """
    area_est = {
        "14460": {"available": True, "pct": {"asian": 6.6666}, "moe90_pp": {"asian": 0.5}},
        "26420": {"available": True, "pct": {"asian": 18.0}, "moe90_pp": {"asian": 0.5}},
    }
    alloc = bounds.site_allocation_bounds(
        area_est, {"14460": 1, "26420": 1}, n_us_facilities=2
    )
    comp = bounds.compare_with_catchment_bounds(
        {"asian": 3.3334}, unknown_pct=3.3334, bounds=alloc
    )
    assert comp["joint_direction_settled"]["asian"] is False
    assert comp["joint_direction"]["asian"] == "unsettled"


def test_simplex_corner_concentration_attains_resolved_min_and_max_exactly() -> None:
    area_est = {
        "10001": {"available": True, "pct": {"asian": 4.125}, "moe90_pp": {"asian": 0.3}},
        "20002": {"available": True, "pct": {"asian": 19.875}, "moe90_pp": {"asian": 0.6}},
        "30003": {"available": True, "pct": {"asian": 42.500}, "moe90_pp": {"asian": 0.9}},
    }
    alloc = bounds.site_allocation_bounds(
        area_est, {"10001": 3, "20002": 1, "30003": 2}, n_us_facilities=6
    )
    site_shares = np.array([4.125, 4.125, 4.125, 19.875, 42.5, 42.5])
    e_min = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    e_max = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
    assert float((e_min * site_shares).sum()) == alloc["unconditional_min_pct"]["asian"]
    assert float((e_max * site_shares).sum()) == alloc["unconditional_max_pct"]["asian"]


def test_equal_facility_and_resident_proxies_always_sit_inside_convex_hull_across_500_trials(
    fuzz_catalog: tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]],
) -> None:
    levels, resolver, templates = fuzz_catalog
    rng = np.random.default_rng(777)
    for i in range(1, 101):
        m_sites = int(rng.integers(2, 15))
        locs = [
            {"facility": f"Site {j}", **templates[int(idx)]}
            for j, idx in enumerate(rng.integers(0, len(templates), size=m_sites))
        ]
        rec = {
            "nct_id": f"NCT{99110000 + i}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "primary_completion_date": "2022-06-01",
            "locations": locs,
        }
        out = ms_benchmark.benchmark_multisite_trial(rec, levels, resolver)
        for lvl in ("zcta", "county", "cbsa", "state", "region", "us"):
            b = out["dimensions"]["race"]["benchmarks"][lvl]
            assert b["available"] is True
            for cat in bounds.COMPARED_CATEGORIES["race"]:
                lo = b["resolved_min_pct"][cat] - 0.002
                hi = b["resolved_max_pct"][cat] + 0.002
                assert lo <= b["equal_facility_proxy"]["pct"][cat] <= hi
                assert lo <= b["distinct_area_resident_proxy"]["pct"][cat] <= hi


def test_single_area_unrounded_pct_consistency_between_bounds_and_equal_facility_proxy() -> None:
    """When ``len(valid_geoids) == 1`` and ``area_estimates`` carries >3 decimal places,
    ``equal_facility_proxy['pct']`` must not fall outside ``[resolved_min_pct, resolved_max_pct]``
    due to inconsistent rounding between the single-geoid and multi-geoid branches.
    """
    est = {
        "14460": {
            "available": True,
            "pct": {"white": 33.3334},
            "moe90_pp": {"white": 1.23456},
        }
    }
    alloc = bounds.site_allocation_bounds(est, {"14460": 2}, n_us_facilities=2)
    eq = bounds.equal_facility_proxy(est, {"14460": 2})
    assert alloc["resolved_min_pct"]["white"] <= eq["pct"]["white"] <= alloc["resolved_max_pct"]["white"]


def test_joint_settlement_strictly_implies_site_only_settled_under_random_inputs() -> None:
    rng = np.random.default_rng(1234)
    for _ in range(300):
        lo_s = float(rng.uniform(5.0, 45.0))
        hi_s = float(rng.uniform(lo_s, 95.0))
        area_est = {
            "A": {"available": True, "pct": {"x": round(lo_s, 3)}, "moe90_pp": {"x": 1.0}},
            "B": {"available": True, "pct": {"x": round(hi_s, 3)}, "moe90_pp": {"x": 1.0}},
        }
        alloc = bounds.site_allocation_bounds(area_est, {"A": 1, "B": 1}, n_us_facilities=2)
        obs = round(float(rng.uniform(0.0, 100.0)), 3)
        unk = round(float(rng.uniform(0.0, 100.0 - obs)), 3)
        comp = bounds.compare_with_catchment_bounds({"x": obs}, unk, alloc)
        if comp["joint_direction_settled"]["x"]:
            assert comp["site_only_settled"]["x"] is True
            assert comp["site_weight_settled"]["x"] is True


def test_unknown_pct_above_complement_clamps_obs_ceiling_to_100() -> None:
    area_est = {
        "A": {"available": True, "pct": {"white": 60.0}, "moe90_pp": {"white": 1.0}},
        "B": {"available": True, "pct": {"white": 99.0}, "moe90_pp": {"white": 1.0}},
    }
    alloc = bounds.site_allocation_bounds(area_est, {"A": 1, "B": 1}, n_us_facilities=2)
    comp = bounds.compare_with_catchment_bounds({"white": 55.0}, unknown_pct=75.0, bounds=alloc)
    assert comp["by_category"]["white"]["obs_ceiling_pct"] == 100.0
    assert comp["joint_diff_pp_max"]["white"] == 40.0
    assert comp["joint_direction_settled"]["white"] is False


def test_negative_unknown_pct_is_clamped_to_zero_without_shrinking_floor() -> None:
    area_est = {"A": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}}}
    alloc = bounds.site_allocation_bounds(area_est, {"A": 1}, n_us_facilities=1)
    comp = bounds.compare_with_catchment_bounds({"white": 45.0}, unknown_pct=-12.5, bounds=alloc)
    assert comp["unknown_pct"] == 0.0
    assert comp["by_category"]["white"]["obs_ceiling_pct"] == 45.0


# =============================================================================================
# 2. MOE COVARIANCE DEDUPLICATION, ACS SENTINELS & STRICT JSON (allow_nan=False) ATTACKS
# =============================================================================================


def test_ten_facilities_in_single_cbsa_preserve_exact_cbsa_moe_without_sqrt10_shrinkage() -> None:
    """``R = 10`` distinct US clinics inside a single CBSA (``26420``) share one ACS estimate
    with correlation 1: ``equal_facility_proxy['moe90_pp']`` MUST equal the single CBSA's exact
    ``moe90_pp`` and never shrink by ``1 / sqrt(10)``.
    """
    cbsa_lvl = _build_synthetic_level(
        [
            (
                "26420",
                "Houston Metro",
                4000.0,
                {
                    "white": 1400.0,
                    "black_african_american": 900.0,
                    "asian": 500.0,
                    "hispanic_latino": 1600.0,
                },
                12.0,
            )
        ]
    )
    single_ref = census_benchmark.expected(
        cbsa_lvl, "26420", "race", ("male", "female"), 18.0, 65.0
    )
    zip_map = {f"770{i:02d}": {"zcta": f"770{i:02d}", "county": "48201", "cbsa": "26420"} for i in range(10)}
    resolver = TableResolver(zip_map)
    locs = [
        {
            "facility": f"Houston Clinic #{i + 1}",
            "city": "Houston",
            "state": "TX",
            "zip": f"770{i:02d}",
            "country": "United States",
        }
        for i in range(10)
    ]
    rec = {
        "nct_id": "NCT99200001",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": locs,
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    cbsa_out = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa_out["n_resolved_facilities"] == 10
    assert cbsa_out["n_distinct_areas"] == 1
    assert cbsa_out["facility_hhi"] == 1.0
    assert cbsa_out["equal_facility_proxy"]["pct"] == single_ref["pct"]
    assert cbsa_out["equal_facility_proxy"]["moe90_pp"] == single_ref["moe90_pp"]
    assert cbsa_out["distinct_area_resident_proxy"]["moe90_pp"] == single_ref["moe90_pp"]
    json.dumps(out, allow_nan=False)


def test_skewed_duplicate_clinics_across_three_cbsas_match_exact_quadratic_form_moe() -> None:
    estimates = {
        "14460": {"available": True, "pct": {"white": 70.0}, "moe90_pp": {"white": 3.0}},
        "26420": {"available": True, "pct": {"white": 40.0}, "moe90_pp": {"white": 5.0}},
        "31080": {"available": True, "pct": {"white": 30.0}, "moe90_pp": {"white": 4.0}},
    }
    # 6 clinics in 14460 (W=0.6), 3 in 26420 (W=0.3), 1 in 31080 (W=0.1)
    proxy = bounds.equal_facility_proxy(estimates, {"14460": 6, "26420": 3, "31080": 1})
    expected_moe = round(math.sqrt((0.6 * 3.0) ** 2 + (0.3 * 5.0) ** 2 + (0.1 * 4.0) ** 2), 3)
    naive_wrong = round(
        math.sqrt(6 * (0.1 * 3.0) ** 2 + 3 * (0.1 * 5.0) ** 2 + (0.1 * 4.0) ** 2), 3
    )
    assert proxy["moe90_pp"]["white"] == expected_moe
    assert proxy["moe90_pp"]["white"] > naive_wrong


def test_controlled_acs_moe_sentinel_minus_555555555_yields_zero_moe_and_valid_json(
    tmp_path: Path,
) -> None:
    """Census Bureau ``-555555555`` marks controlled estimates whose MOE is ``0.0`` by construction."""
    geo_col = acs.LEVELS["state"].split(":")[0]
    for cat, table in acs.TABLES.items():
        spans = FINE_SPANS if cat == "total" else COARSE_SPANS
        variables = _make_variables(spans)
        cells = [k[:-1] for k in variables if k.endswith("M")]
        header = ["NAME"] + [c + "E" for c in cells] + [c + "M" for c in cells] + [geo_col]
        rows = [header]
        for st_code, st_name, white_n in [("25", "Massachusetts", 350), ("48", "Texas", 250)]:
            vals = (
                [50, 200, 300, 50, 50, 200, 300, 50]
                if cat == "total"
                else ([20, white_n, 20, 20, white_n, 20] if cat == "white" else [0] * 6)
            )
            moes = ["-555555555"] * len(cells)
            rows.append([st_name] + [str(v) for v in vals] + moes + [st_code])
        with gzip.open(tmp_path / f"acs5_2024_{table}_state.json.gz", "wt") as fh:
            json.dump({"variables": variables, "rows": rows}, fh)

    st_level = acs.load(2024, "state", tmp_path)
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99200002",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"white": 80, "black_african_american": 20, "unknown_not_reported": 0},
            "raw_categories": [{"original": "White", "count": 80}, {"original": "Black", "count": 20}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"state": st_level}, resolver)
    st_bench = out["dimensions"]["race"]["benchmarks"]["state"]
    assert st_bench["equal_facility_proxy"]["moe90_pp"]["white"] == 0.0
    assert st_bench["distinct_area_resident_proxy"]["moe90_pp"]["white"] == 0.0
    json.dumps(out, allow_nan=False)


def test_nan_moe_in_acs_level_becomes_none_and_passes_strict_json_dumps() -> None:
    """Negative non-controlled MOE sentinels (loaded as ``NaN`` in ``acs.Level.moe``) must map
    to ``None`` (never ``NaN``) in ``benchmark_multisite_trial`` and ``pool_multisite_cohort``.
    """
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"white": 700.0, "asian": 200.0}, float("nan")),
            ("26420", "Houston", 2000.0, {"white": 800.0, "asian": 300.0}, 5.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    trials_list = []
    for idx in range(3):
        rec = {
            "nct_id": f"NCT{99200100 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "primary_completion_date": "2022-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 60, "asian": 40, "unknown_not_reported": 0},
                "raw_categories": [{"original": "White", "count": 60}, {"original": "Asian", "count": 40}],
            },
        }
        t_out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
        json.dumps(t_out, allow_nan=False)
        cbsa_b = t_out["dimensions"]["race"]["benchmarks"]["cbsa"]
        assert cbsa_b["equal_facility_proxy"]["moe90_pp"]["white"] is None
        assert cbsa_b["distinct_area_resident_proxy"]["moe90_pp"]["white"] is None
        trials_list.append(t_out)

    pooled = ms_benchmark.pool_multisite_cohort(
        trials_list, level="cbsa", dimension="race", floor=2, resamples=50, seed=3
    )
    json.dumps(pooled, allow_nan=False)
    eq_w = next(c for c in pooled["equal_facility_proxy"]["categories"] if c["category"] == "white")
    assert eq_w["expected_moe90_pp"] is None


def test_infinite_moe_in_area_estimates_never_leaks_inf_into_json_output() -> None:
    """Non-finite ``inf`` MOEs must become ``None`` (never ``inf``) so ``allow_nan=False`` passes."""
    est_single = {
        "14460": {
            "available": True,
            "pct": {"white": 60.0},
            "moe90_pp": {"white": float("inf")},
        }
    }
    eq1 = bounds.equal_facility_proxy(est_single, {"14460": 2})
    json.dumps(eq1, allow_nan=False)
    assert eq1["moe90_pp"]["white"] is None


def test_negative_sentinel_est_nan_in_acs_subtable_never_leaks_false_settlement_or_nan_json() -> None:
    """When ``B01001`` (``total``) has positive residents in both ZCTAs, while a race sub-table
    (e.g. ``B01001D`` Asian) is unpublished (``-999999999`` -> ``est = NaN``) at Site 2:
      1. ``json.dumps(out, allow_nan=False)`` MUST succeed with zero ``NaN`` floats;
      2. Site 2's missing Asian estimate MUST NOT be silently ignored by Python ``min()`` to
         produce ``bounds_unconditional = True`` and ``joint_direction_settled['asian'] = True``.
    """
    zcta_lvl = _build_synthetic_level(
        [
            ("02114", "ZCTA 02114", 1000.0, {"white": 750.0, "asian": 250.0}, 3.0),
            ("77030", "ZCTA 77030", 1000.0, {"white": 750.0, "asian": float("nan")}, 3.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99200003",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": [
            {"facility": "Boston Clinic", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Houston Clinic", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 10, "white": 90, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": 10}, {"original": "White", "count": 90}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    json.dumps(out, allow_nan=False)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    if zb.get("available") and zb.get("comparison_available"):
        assert zb["joint_direction_settled"].get("asian") is False
        assert zb["site_only_settled"].get("asian") is False


def test_all_sites_negative_sentinel_est_nan_in_total_table_returns_estimate_not_published() -> None:
    zcta_lvl = _build_synthetic_level(
        [
            ("02114", "ZCTA 02114", float("nan"), {}, float("nan")),
            ("77030", "ZCTA 77030", float("nan"), {}, float("nan")),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99200004",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert zb["available"] is False
    assert zb["reason"] == "estimate_not_published"
    json.dumps(out, allow_nan=False)


def test_zero_denominator_acs_areas_return_no_eligible_residents_and_strict_json() -> None:
    zcta_lvl = _build_synthetic_level(
        [
            ("02114", "Campus A", 0.0, {}, 1.0),
            ("77030", "Campus B", 0.0, {}, 1.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99200005",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"white": 100},
            "raw_categories": [{"original": "White", "count": 100}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert zb["available"] is False
    assert zb["reason"] == "no_eligible_residents_estimated"
    json.dumps(out, allow_nan=False)


def test_zero_reference_share_sets_ratio_none_and_ratio_undefined_in_trial_and_cohort() -> None:
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Metro A", 1000.0, {"white": 1000.0, "native_hawaiian_pacific_islander": 0.0}, 2.0),
            ("26420", "Metro B", 1000.0, {"white": 1000.0, "native_hawaiian_pacific_islander": 0.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    trials_out = []
    for idx in range(3):
        rec = {
            "nct_id": f"NCT{99200200 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 95, "native_hawaiian_pacific_islander": 5},
                "raw_categories": [
                    {"original": "White", "count": 95},
                    {"original": "Native Hawaiian", "count": 5},
                ],
            },
        }
        t_out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
        comp = t_out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]
        assert comp["equal_facility_proxy"]["ratio"]["native_hawaiian_pacific_islander"] is None
        assert (
            comp["equal_facility_proxy"]["ratio_undefined"]["native_hawaiian_pacific_islander"]
            == "reference_share_is_zero"
        )
        assert comp["of_classified"]["equal_facility_proxy"]["ratio"]["native_hawaiian_pacific_islander"] is None
        json.dumps(t_out, allow_nan=False)
        trials_out.append(t_out)

    pooled = ms_benchmark.pool_multisite_cohort(
        trials_out, level="cbsa", dimension="race", floor=2, resamples=40, seed=9
    )
    json.dumps(pooled, allow_nan=False)
    nhpi = next(
        c
        for c in pooled["equal_facility_proxy"]["categories"]
        if c["category"] == "native_hawaiian_pacific_islander"
    )
    assert nhpi["ratio"] is None
    assert nhpi["ratio_undefined"] == "reference_share_is_zero"
    assert nhpi["of_classified"]["ratio"] is None


def test_nan_or_boolean_registered_enrollment_never_corrupts_ratio_or_breaks_strict_json() -> None:
    """``enrollment = float('nan')`` or ``enrollment = True`` in dirty input records must not
    leak ``NaN`` into ``registered_enrollment`` (breaking ``allow_nan=False``) or treat ``True``
    as ``1`` participant in ``table_to_enrollment_ratio``.
    """
    cbsa_lvl = _build_synthetic_level([("14460", "Metro A", 1000.0, {"white": 700.0}, 2.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    rec_nan = {
        "nct_id": "NCT99200006",
        "enrollment": float("nan"),
        "min_age": "18 Years",
        "start_date": "2021-01-01",
        "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
        "race": {
            "reported": True,
            "omb_totals": {"white": 50},
            "raw_categories": [{"original": "White", "count": 50}],
        },
    }
    out_nan = ms_benchmark.benchmark_multisite_trial(rec_nan, {"cbsa": cbsa_lvl}, resolver)
    json.dumps(out_nan, allow_nan=False)

    rec_bool = copy.deepcopy(rec_nan)
    rec_bool["nct_id"] = "NCT99200007"
    rec_bool["enrollment"] = True
    out_bool = ms_benchmark.benchmark_multisite_trial(rec_bool, {"cbsa": cbsa_lvl}, resolver)
    assert out_bool["dimensions"]["race"]["table_to_enrollment_ratio"] is None


def test_strict_json_allow_nan_false_across_multi_trial_cohort_pooling(
    fuzz_catalog: tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]],
) -> None:
    levels, resolver, templates = fuzz_catalog
    results = []
    for idx in range(6):
        rec = {
            "nct_id": f"NCT{99200300 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "eligibility": {"sex": "ALL"},
            "start_date": "2021-01-01",
            "primary_completion_date": "2023-01-01",
            "locations": [
                {"facility": "Site A", **templates[idx]},
                {"facility": "Site B", **templates[idx + 3]},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 60, "black_african_american": 25, "asian": 10, "unknown_not_reported": 5},
                "raw_categories": [
                    {"original": "White", "count": 60},
                    {"original": "Black", "count": 25},
                    {"original": "Asian", "count": 10},
                ],
            },
            "ethnicity": {
                "reported": True,
                "omb_totals": {"hispanic_latino": 20, "not_hispanic_latino": 75, "unknown_not_reported": 5},
                "raw_categories": [
                    {"omb_category": "hispanic_latino", "original": "Hispanic", "count": 20},
                    {"omb_category": "not_hispanic_latino", "original": "Not Hispanic", "count": 75},
                ],
            },
            "sex": {"reported": True, "totals": {"female": 55, "male": 45, "unknown": 0}},
        }
        t_out = ms_benchmark.benchmark_multisite_trial(rec, levels, resolver)
        json.dumps(t_out, allow_nan=False)
        results.append(t_out)

    for dim in ("race", "ethnicity", "sex"):
        for lvl in footprint.LEVEL_ORDER:
            pooled = ms_benchmark.pool_multisite_cohort(
                results, level=lvl, dimension=dim, floor=3, resamples=50, seed=17
            )
            json.dumps(pooled, allow_nan=False)


# =============================================================================================
# 3. FACILITY DEDUPLICATION, TERRITORY CLASSIFICATION & FOREIGN ZIP / COORDINATE TRAPS
# =============================================================================================


def test_duplicate_named_clinics_merge_while_distinct_unnamed_clinics_in_same_zip_stay_separate() -> None:
    locs = [
        {"facility": "Mass General Hospital", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
        {"facility": "MASS  GENERAL HOSPITAL", "city": "boston", "state": "Massachusetts", "zip": "02114-2696", "country": "USA"},
        {"facility": None, "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
        {"facility": "   ", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
    ]
    fp = footprint.classify_footprint({"nct_id": "NCT99300101", "locations": locs})
    assert fp["n_raw_locations"] == 4
    assert fp["n_duplicate_locations_removed"] == 1
    assert fp["n_facilities"] == 3
    assert fp["footprint_tier"] == "multi_site_us_only"


def test_generic_placeholder_variants_plural_and_british_spelling_stay_distinct_by_default() -> None:
    locs = [
        {"facility": "Clinical Centre", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Clinical Centre", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Research Sites", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        {"facility": "Research Sites", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
    ]
    fp = footprint.classify_footprint({"nct_id": "NCT99300102", "locations": locs})
    assert fp["n_facilities"] == 4
    assert fp["n_duplicate_locations_removed"] == 0


@pytest.mark.parametrize(
    "foreign_loc",
    [
        {"facility": "Charite", "city": "Berlin", "zip": "10115", "country": "Germany"},
        {"facility": "Hotel-Dieu", "city": "Paris", "zip": "75001", "country": "France"},
        {"facility": "IMSS", "city": "Cuauhtemoc", "state": "CDMX", "zip": "06600", "country": "Mexico"},
        {"facility": "Policlinico", "city": "Roma", "state": "RM", "zip": "00185", "country": "Italy"},
        {"facility": "Tbilisi Central", "city": "Tbilisi", "zip": "0108", "country": "Georgia"},
    ],
)
def test_foreign_five_digit_zips_and_country_georgia_never_get_us_zip5(
    foreign_loc: dict[str, str],
) -> None:
    fac = footprint.normalize_facility(foreign_loc)
    assert fac["jurisdiction"] == "non_us"
    assert fac["zip5"] is None
    fp = footprint.classify_footprint({"nct_id": "NCT99300103", "locations": [foreign_loc]})
    assert fp["footprint_tier"] == "non_us_only"


@pytest.mark.parametrize(
    "territory_loc,expected_terr",
    [
        ({"city": "San Juan", "state": "Puerto Rico", "zip": "00901", "country": "United States"}, "Puerto Rico"),
        ({"city": "Ponce", "state": "PR", "zip": "00716", "country": "United States"}, "Puerto Rico"),
        ({"city": "San Juan", "state": "San Juan", "zip": "00901", "country": "United States"}, "Puerto Rico"),
        ({"city": "Adjuntas", "zip": "00601", "country": "United States"}, "Puerto Rico"),
        ({"city": "Tamuning", "state": "Guam", "zip": "96913", "country": "United States"}, "Guam"),
        ({"city": "Dededo", "zip": "96929", "country": "United States"}, "Guam"),
        ({"city": "St. Thomas", "state": "VI", "zip": "00802", "country": "United States"}, "U.S. Virgin Islands"),
        ({"city": "Pago Pago", "zip": "96799", "country": "United States"}, "American Samoa"),
        ({"city": "Saipan", "zip": "96950", "country": "United States"}, "Northern Mariana Islands"),
    ],
)
def test_puerto_rico_guam_usvi_as_mp_under_united_states_classify_as_us_with_territories(
    territory_loc: dict[str, str],
    expected_terr: str,
) -> None:
    boston = {"facility": "MGH", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}
    fp = footprint.classify_footprint(
        {"nct_id": "NCT99300104", "locations": [boston, territory_loc]}
    )
    assert fp["footprint_tier"] == "us_with_territories"
    assert fp["us_territories"] == [expected_terr]
    assert fp["eligible_for_us_multisite_rollup"] is False


@pytest.mark.parametrize(
    "terr_loc,expected_terr",
    [
        ({"facility": "San Juan Hosp", "city": "San Juan", "state": "P.R.", "country": "United States"}, "Puerto Rico"),
        ({"facility": "St Thomas Hosp", "city": "Charlotte Amalie", "state": "Virgin Islands", "country": "United States"}, "U.S. Virgin Islands"),
        ({"facility": "St Croix Hosp", "city": "Christiansted", "state": "U.S.V.I.", "country": "United States"}, "U.S. Virgin Islands"),
        ({"facility": "Guam Hosp", "city": "Tamuning", "state": "G.U.", "country": "United States"}, "Guam"),
    ],
)
def test_dotted_territory_abbreviations_and_virgin_islands_under_united_states_not_misclassified_as_50_state(
    terr_loc: dict[str, str],
    expected_terr: str,
) -> None:
    """Dotted territory abbreviations (``P.R.``, ``U.S.V.I.``, ``G.U.`` — matching ``D.C.`` and
    ``MA.`` support for states) and ``state='Virgin Islands'`` under ``country='United States'``
    MUST classify as ``us_territory`` (``us_with_territories``), never as 50-state+DC
    ``multi_site_us_only``!
    """
    boston = {"facility": "MGH", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}
    fp = footprint.classify_footprint(
        {"nct_id": "NCT99300105", "locations": [boston, terr_loc]}
    )
    assert fp["footprint_tier"] == "us_with_territories"
    assert fp["us_territories"] == [expected_terr]
    assert fp["eligible_for_us_multisite_rollup"] is False


def test_virgin_islands_country_and_territory_iso2_with_territory_zip_classify_as_us_territory() -> None:
    """``country='Virgin Islands'`` with USVI ZIP ``00802`` and ``country='PR'`` with PR ZIP
    ``00901`` must classify as ``us_territory``, not ``non_us`` foreign countries.
    """
    vi_fac = footprint.normalize_facility(
        {"facility": "Schneider Regional", "city": "St. Thomas", "country": "Virgin Islands", "zip": "00802"}
    )
    pr_fac = footprint.normalize_facility(
        {"facility": "Auxilio Mutuo", "city": "San Juan", "country": "PR", "zip": "00901"}
    )
    assert vi_fac["jurisdiction"] == "us_territory"
    assert vi_fac["state"] == "U.S. Virgin Islands"
    assert pr_fac["jurisdiction"] == "us_territory"
    assert pr_fac["state"] == "Puerto Rico"


def test_holtsville_ny_00501_zip_stays_50_state_us_never_puerto_rico() -> None:
    fac = footprint.normalize_facility(
        {"facility": "Brookhaven Clinic", "city": "Holtsville", "state": "NY", "zip": "00501", "country": "United States"}
    )
    assert fac["jurisdiction"] == "us_state_or_dc"
    assert fac["state"] == "New York"
    assert fac["zip5"] == "00501"


@pytest.mark.parametrize(
    "coord_loc,expected_point",
    [
        ({"country": "United States", "lat": 95.0, "lon": -71.0}, None),
        ({"country": "United States", "lat": 42.0, "lon": 200.0}, None),
        ({"country": "United States", "lat": "nan", "lon": "-71.0"}, None),
        ({"country": "United States", "lat": "-inf", "lon": "-71.0"}, None),
        ({"country": "United States", "lat": True, "lon": -71.0}, None),
        ({"country": "United States", "lat": "42.3601", "lon": "-71.0589"}, (42.3601, -71.0589)),
        ({"country": "United States", "point": ("-90.0", "180.0")}, (-90.0, 180.0)),
    ],
)
def test_out_of_range_nan_inf_boolean_and_string_coordinates_handled_safely(
    coord_loc: dict[str, Any],
    expected_point: tuple[float, float] | None,
) -> None:
    assert footprint.normalize_facility(coord_loc)["point"] == expected_point


def test_none_and_non_mapping_entries_in_locations_list_and_sequence_input() -> None:
    """Both ``classify_footprint({'locations': [None, ...]})`` and direct sequence calls
    ``classify_footprint([None, ...])`` / ``resolve_us_facilities([None, ...], resolver)``
    must ignore ``None`` entries without raising ``TypeError``.
    """
    valid = {"facility": "MGH", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}
    fp_rec = footprint.classify_footprint({"nct_id": "NCT99300106", "locations": [None, valid]})
    assert fp_rec["n_facilities"] == 1

    fp_seq = footprint.classify_footprint([None, valid])  # type: ignore[list-item]
    assert fp_seq["n_facilities"] == 1

    res_seq = footprint.resolve_us_facilities([None, valid], TableResolver({}))  # type: ignore[list-item]
    assert res_seq["n_us_facilities"] == 1


def test_empty_locations_list_does_not_shadow_populated_study_sites_or_api_v2_locations() -> None:
    """An empty ``locations: []`` placeholder on a record that carries ``study_sites`` (or
    ``protocolSection.contactsLocationsModule.locations``) must not drop the trial's facilities.
    """
    rec = {
        "nct_id": "NCT99300107",
        "locations": [],
        "study_sites": [
            {"facility": "MGH", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "TMC", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    }
    fp = footprint.classify_footprint(rec)
    assert fp["n_facilities"] == 2
    assert fp["footprint_tier"] == "multi_site_us_only"


def test_extract_facilities_normalizes_state_and_zip_plus4_before_deduplication() -> None:
    rec = {
        "nct_id": "NCT99300108",
        "locations": [
            {"facility": "Johns Hopkins", "city": "Baltimore", "state": "MD", "zip": "21287", "country": "United States"},
            {"facility": "johns hopkins", "city": "BALTIMORE", "state": "Maryland", "zip": "21287-0005", "country": "U.S.A."},
        ],
    }
    facs, meta = footprint.extract_facilities(rec)
    assert len(facs) == 1
    assert meta["n_duplicate_locations_removed"] == 1


def test_facility_hhi_bounds_and_monotone_aggregation_across_hierarchy(
    fuzz_catalog: tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]],
) -> None:
    _, resolver, templates = fuzz_catalog
    locs = [{"facility": f"Clinic {i}", **templates[i]} for i in range(8)]
    res = footprint.resolve_us_facilities(locs, resolver)
    hierarchy = ("zcta", "county", "state", "region", "us")
    hhis = [res["by_level"][lvl]["facility_hhi"] for lvl in hierarchy]
    for h in hhis:
        assert h is not None
        assert 1.0 / 8.0 - 1e-9 <= h <= 1.0 + 1e-9
    for earlier, later in zip(hhis, hhis[1:]):
        assert earlier <= later + 1e-9


# =============================================================================================
# 4. UNRESOLVED-SITE SETTLEMENT LEAKS & VINTAGE PROVENANCE CONSISTENCY
# =============================================================================================


def test_five_us_sites_four_metro_one_rural_blocks_cbsa_settlement_while_state_and_us_settle() -> None:
    """5 US facilities: 4 resolve to CBSAs with ``20%-30%`` Asian share and 1 rural clinic
    resolves to ``cbsa.geoid = None`` (``outside_any_metro_or_micro_area``), while trial
    observed Asian share is ``5%``:
      * At ``cbsa``: ``bounds_unconditional``, ``site_only_settled``, and
        ``joint_direction_settled`` MUST all be ``False``!
      * At ``state`` and ``us`` (where all 5 sites DO resolve with Asian share >= 12%):
        ``bounds_unconditional``, ``site_only_settled``, and ``joint_direction_settled``
        are ``True`` (``'negative'``)!
    """
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston CBSA", 1000.0, {"asian": 200.0, "white": 800.0}, 2.0),
            ("26420", "Houston CBSA", 1000.0, {"asian": 300.0, "white": 700.0}, 2.0),
        ]
    )
    state_lvl = _build_synthetic_level(
        [
            ("25", "Massachusetts", 1000.0, {"asian": 200.0, "white": 800.0}, 2.0),
            ("48", "Texas", 1000.0, {"asian": 250.0, "white": 750.0}, 2.0),
            ("30", "Montana", 1000.0, {"asian": 120.0, "white": 880.0}, 2.0),
        ]
    )
    us_lvl = _build_synthetic_level(
        [("1", "United States", 3000.0, {"asian": 570.0, "white": 2430.0}, 3.0)]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "02115": {"zcta": "02115", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
            "77002": {"zcta": "77002", "county": "48201", "cbsa": "26420"},
            "59715": {"zcta": "59715", "county": "30031", "cbsa": None},
        }
    )
    rec = {
        "nct_id": "NCT99400001",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "primary_completion_date": "2022-01-01",
        "locations": [
            {"facility": "MA 1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "MA 2", "city": "Boston", "state": "MA", "zip": "02115", "country": "United States"},
            {"facility": "TX 1", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            {"facility": "TX 2", "city": "Houston", "state": "TX", "zip": "77002", "country": "United States"},
            {"facility": "MT Rural", "city": "Bozeman", "state": "MT", "zip": "59715", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 5, "white": 95, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": 5}, {"original": "White", "count": 95}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(
        rec, {"cbsa": cbsa_lvl, "state": state_lvl, "us": us_lvl}, resolver
    )
    cbsa_b = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa_b["n_us_facilities"] == 5
    assert cbsa_b["n_resolved_facilities"] == 4
    assert cbsa_b["n_unresolved_or_unavailable"] == 1
    assert cbsa_b["bounds_unconditional"] is False
    assert cbsa_b["resolved_min_pct"]["asian"] == 20.0
    assert cbsa_b["resolved_max_pct"]["asian"] == 30.0
    assert cbsa_b["unconditional_min_pct"]["asian"] == 0.0
    assert cbsa_b["unconditional_max_pct"]["asian"] == 100.0
    assert cbsa_b["site_only_settled"]["asian"] is False
    assert cbsa_b["site_weight_settled"]["asian"] is False
    assert cbsa_b["joint_direction_settled"]["asian"] is False
    assert cbsa_b["joint_direction"]["asian"] == "unsettled"
    assert cbsa_b["of_classified"]["by_category"]["asian"]["direction_settled"] is False

    for full_lvl in ("state", "us"):
        fb = out["dimensions"]["race"]["benchmarks"][full_lvl]
        assert fb["bounds_unconditional"] is True
        assert fb["site_only_settled"]["asian"] is True
        assert fb["joint_direction_settled"]["asian"] is True
        assert fb["joint_direction"]["asian"] == "negative"


def test_resolved_geoid_missing_from_acs_window_file_drops_bounds_unconditional_to_false() -> None:
    cbsa_lvl = _build_synthetic_level(
        [("14460", "Boston CBSA", 1000.0, {"asian": 250.0, "white": 750.0}, 2.0)]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "99991": {"zcta": "99991", "county": "48201", "cbsa": "99991"},
        }
    )
    rec = {
        "nct_id": "NCT99400002",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "MA 1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Ghost CBSA", "city": "Houston", "state": "TX", "zip": "99991", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 2, "white": 98, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": 2}, {"original": "White", "count": 98}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    cbsa_b = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa_b["available"] is True
    assert cbsa_b["n_resolved_facilities"] == 2
    assert cbsa_b["n_acs_available_facilities"] == 1
    assert cbsa_b["n_unresolved_or_unavailable"] == 1
    assert cbsa_b["bounds_unconditional"] is False
    assert cbsa_b["site_allocation_bounds"]["unavailable_resolved_geoids"] == {
        "99991": "area_not_in_this_window_file"
    }
    assert cbsa_b["site_only_settled"]["asian"] is False
    assert cbsa_b["joint_direction_settled"]["asian"] is False


def test_resolved_geoid_with_zero_eligible_residents_drops_bounds_unconditional_to_false() -> None:
    zcta_lvl = _build_synthetic_level(
        [
            ("02114", "Boston ZCTA", 1000.0, {"asian": 300.0, "white": 700.0}, 2.0),
            ("77030", "Medical Campus Zero Adults", 0.0, {}, 1.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99400003",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "MA", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Campus", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 1, "white": 99, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": 1}, {"original": "White", "count": 99}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert zb["bounds_unconditional"] is False
    assert zb["site_allocation_bounds"]["unavailable_resolved_geoids"] == {
        "77030": "no_eligible_residents_estimated"
    }
    assert zb["site_only_settled"]["asian"] is False
    assert zb["joint_direction_settled"]["asian"] is False


def test_resolved_geoid_with_unpublished_total_estimate_drops_bounds_unconditional_to_false() -> None:
    zcta_lvl = _build_synthetic_level(
        [
            ("02114", "Boston ZCTA", 1000.0, {"asian": 300.0, "white": 700.0}, 2.0),
            ("77030", "Suppressed ZCTA", float("nan"), {}, float("nan")),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99400004",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "MA", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "TX", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 1, "white": 99},
            "raw_categories": [{"original": "Asian", "count": 1}, {"original": "White", "count": 99}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert zb["bounds_unconditional"] is False
    assert zb["site_allocation_bounds"]["unavailable_resolved_geoids"] == {
        "77030": "estimate_not_published"
    }
    assert zb["joint_direction_settled"]["asian"] is False


def test_us_territory_plus_four_us_states_blocks_unconditional_bounds_even_at_national_us_level() -> None:
    us_lvl = _build_synthetic_level([("1", "United States", 5000.0, {"asian": 1000.0, "white": 4000.0}, 4.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    locs = [
        {"facility": f"MA {i}", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}
        for i in range(4)
    ] + [{"facility": "PR Site", "city": "San Juan", "country": "Puerto Rico", "zip": "00901"}]
    rec = {
        "nct_id": "NCT99400005",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": locs,
        "race": {
            "reported": True,
            "omb_totals": {"asian": 1, "white": 99},
            "raw_categories": [{"original": "Asian", "count": 1}, {"original": "White", "count": 99}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"us": us_lvl}, resolver)
    us_b = out["dimensions"]["race"]["benchmarks"]["us"]
    assert us_b["n_us_facilities"] == 5
    assert us_b["n_resolved_facilities"] == 4
    assert us_b["bounds_unconditional"] is False
    assert us_b["joint_direction_settled"]["asian"] is False


def test_mixed_us_and_international_trial_carries_global_pooled_caveat_and_never_pools() -> None:
    us_lvl = _build_synthetic_level([("1", "United States", 5000.0, {"white": 3500.0}, 4.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    rec = {
        "nct_id": "NCT99400006",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "Boston", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Berlin", "city": "Berlin", "zip": "10115", "country": "Germany"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"white": 90, "unknown_not_reported": 10},
            "raw_categories": [{"original": "White", "count": 90}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"us": us_lvl}, resolver)
    assert out["footprint"]["global_pooled_baseline_table"] is True
    assert "mixed_us_and_international_sites_pooled_baseline_table" in out["reference_population"]["caveats"]
    assert out["dimensions"]["race"]["pooled"] is False
    assert out["dimensions"]["race"]["not_pooled_reason"] == "mixed_us_and_international_pooled_baseline"


def test_vintage_2009_zcta_resolution_per_facility_matches_by_level_summary() -> None:
    """For 2005-2009 trials (``vintage == 2009``), ``resolution['by_level']['zcta']`` marks all
    US facilities unresolved (``zcta_tables_not_published_for_this_window``); per-facility
    ``resolution['facilities'][i]['levels']['zcta']`` MUST agree rather than still claiming a
    resolved ``geoid``.
    """
    cbsa_lvl = _build_synthetic_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    rec = {
        "nct_id": "NCT99400007",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2007-01-01",
        "primary_completion_date": "2008-06-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Cambridge", "state": "MA", "zip": "02114", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    assert out["benchmark_period"]["vintage"] == 2009
    zcta_summary = out["resolution"]["by_level"]["zcta"]
    per_fac_resolved = sum(
        1 for f in out["resolution"]["facilities"] if f["levels"]["zcta"].get("geoid")
    )
    assert per_fac_resolved == zcta_summary["n_resolved_facilities"] == 0


def test_state_mismatch_withdrawal_propagates_to_conditional_local_bounds_and_unconditional_state() -> None:
    class StateMismatchResolver:
        def resolve(self, zip5: str | None, point: Any, state_name: str | None) -> dict[str, Any]:
            if zip5 == "02114" and state_name == "Texas":
                withdrawn = {"geoid": None, "reason": "resolved_place_is_outside_the_registered_state"}
                return {
                    "zcta": withdrawn,
                    "county": withdrawn,
                    "cbsa": withdrawn,
                    "state": {"geoid": "48", "method": "registered_state_name"},
                    "region": {"geoid": "3", "method": "census_region_of_state"},
                    "us": {"geoid": "1", "method": "national"},
                }
            return {
                "zcta": {"geoid": "02114", "method": "zip"},
                "county": {"geoid": "25025", "method": "poly"},
                "cbsa": {"geoid": "14460", "method": "poly"},
                "state": {"geoid": "25", "method": "registered_state_name"},
                "region": {"geoid": "1", "method": "census_region_of_state"},
                "us": {"geoid": "1", "method": "national"},
            }

    cbsa_lvl = _build_synthetic_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    state_lvl = _build_synthetic_level(
        [("25", "MA", 1000.0, {"white": 700.0}, 2.0), ("48", "TX", 1000.0, {"white": 450.0}, 2.0)]
    )
    rec = {
        "nct_id": "NCT99400008",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "Valid MA", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "Bad ZIP TX", "city": "Houston", "state": "TX", "zip": "02114", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(
        rec, {"cbsa": cbsa_lvl, "state": state_lvl}, StateMismatchResolver()
    )
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["bounds_unconditional"] is False
    assert out["dimensions"]["race"]["benchmarks"]["state"]["bounds_unconditional"] is True


def test_site_allocation_bounds_rejects_negative_or_inconsistent_facility_counts() -> None:
    est = {"14460": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}}}
    with pytest.raises(ValueError, match="cannot be negative"):
        bounds.site_allocation_bounds(est, {}, n_us_facilities=-1)
    with pytest.raises(ValueError, match="less than resolved"):
        bounds.site_allocation_bounds(est, {"14460": 2}, n_us_facilities=1)

    zero_ignored = bounds.site_allocation_bounds(
        est, {"14460": 1, "26420": 0}, n_us_facilities=1
    )
    assert zero_ignored["bounds_unconditional"] is True
    assert zero_ignored["n_distinct_areas"] == 1


# =============================================================================================
# 5. BASELINE TABLE STRUCTURE, UNMAPPED CATEGORIES & COHORT POOLING TRAPS
# =============================================================================================


def test_race_table_with_combined_ethnicity_label_or_shortfall_rejected_by_category_structure() -> None:
    cbsa_lvl = _build_synthetic_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    rec_shortfall = {
        "nct_id": "NCT99500001",
        "min_age": "18 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Cambridge", "state": "MA", "zip": "02114", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "flags": ["combined_race_ethnicity"],
            "unknown_inferred": 15,
            "omb_totals": {"white": 85, "unknown_not_reported": 15},
            "raw_categories": [{"original": "White", "count": 85}],
        },
    }
    out_unchecked = ms_benchmark.benchmark_multisite_trial(rec_shortfall, {"cbsa": cbsa_lvl}, resolver)
    assert out_unchecked["dimensions"]["race"]["structure"]["usable_for_shares"] is False
    assert out_unchecked["dimensions"]["race"]["structure"]["check"] == "combined_table_not_checked_against_registry"
    assert out_unchecked["dimensions"]["race"]["pooled"] is False

    out_checked = ms_benchmark.benchmark_multisite_trial(
        rec_shortfall,
        {"cbsa": cbsa_lvl},
        resolver,
        registry_ethnicity_rows=[{"label": "Hispanic", "largest_count": 15}],
    )
    assert out_checked["dimensions"]["race"]["structure"]["usable_for_shares"] is False
    assert out_checked["dimensions"]["race"]["structure"]["check"] == "race_table_has_ethnicity_rows"
    assert out_checked["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison_available"] is False


def test_race_and_ethnicity_tables_exceeding_sex_table_total_rejected_as_double_counted() -> None:
    cbsa_lvl = _build_synthetic_level(
        [("14460", "Boston", 1000.0, {"white": 700.0, "hispanic_latino": 150.0}, 2.0)]
    )
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    rec_double = {
        "nct_id": "NCT99500002",
        "min_age": "18 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Cambridge", "state": "MA", "zip": "02114", "country": "United States"},
        ],
        "sex": {"reported": True, "totals": {"female": 50, "male": 50, "unknown": 0}},
        "race": {
            "reported": True,
            "omb_totals": {"white": 70, "black_african_american": 35, "unknown_not_reported": 0},
            "raw_categories": [{"original": "White", "count": 70}, {"original": "Black", "count": 35}],
        },
        "ethnicity": {
            "reported": True,
            "omb_totals": {"hispanic_latino": 20, "not_hispanic_latino": 81, "unknown_not_reported": 0},
            "raw_categories": [
                {"omb_category": "hispanic_latino", "original": "Hispanic", "count": 20},
                {"omb_category": "not_hispanic_latino", "original": "Not Hispanic", "count": 81},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec_double, {"cbsa": cbsa_lvl}, resolver)
    # Race total 105 > 100 + max(1, 1.0) = 101 -> double counted!
    assert out["dimensions"]["race"]["structure"]["usable_for_shares"] is False
    assert out["dimensions"]["race"]["structure"]["check"] == "table_total_exceeds_sex_table"
    assert out["dimensions"]["race"]["pooled"] is False
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison_available"] is False
    # Ethnicity total 101 <= 101 rounding tolerance -> usable!
    assert out["dimensions"]["ethnicity"]["structure"]["usable_for_shares"] is True


def test_single_category_ethnicity_table_with_unknowns_withholds_of_classified_in_trial_and_cohort() -> None:
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"hispanic_latino": 150.0}, 2.0),
            ("26420", "Houston", 1000.0, {"hispanic_latino": 450.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    trials_out = []
    for idx in range(3):
        rec = {
            "nct_id": f"NCT{99500100 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "ethnicity": {
                "reported": True,
                "omb_totals": {"hispanic_latino": 15, "not_hispanic_latino": 0, "unknown_not_reported": 85},
                "raw_categories": [
                    {"omb_category": "hispanic_latino", "original": "Hispanic", "count": 15}
                ],
            },
        }
        t_out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
        eth = t_out["dimensions"]["ethnicity"]
        assert eth["observed_pct_of_classified"] is None
        assert eth["classified_share_reason"] == "table_reports_one_ethnicity_category_only"
        assert eth["benchmarks"]["cbsa"]["of_classified"] is None
        assert (
            eth["benchmarks"]["cbsa"]["classified_share_reason"]
            == "table_reports_one_ethnicity_category_only"
        )
        trials_out.append(t_out)

    pooled = ms_benchmark.pool_multisite_cohort(
        trials_out, level="cbsa", dimension="ethnicity", floor=2, resamples=40, seed=5
    )
    assert pooled["display_rule"] == "show"
    for cat_block in pooled["equal_facility_proxy"]["categories"]:
        assert "of_classified" not in cat_block


def test_unmapped_baseline_categories_widen_unknown_ceiling_and_pool_unknown_or_unmapped_mean() -> None:
    """When ``omb_totals`` carries unharmonised participant counts (``unmapped_category > 0``,
    e.g. ``'indigenous': 50`` alongside ``'asian': 5, 'white': 45, 'unknown_not_reported': 0``):
      1. ``denominators['unknown_or_unmapped_pct']`` is ``50.0%``; those 50 unharmonised
         participants could belong to ``asian`` under OMB classification, so ``obs_ceiling_pct``
         for ``asian`` MUST be ``55.0%`` (not ``5.0%``!) and ``joint_direction_settled['asian']``
         against a ``20.0%`` site benchmark MUST be ``False``!
      2. ``pool_multisite_cohort(...)['denominators']['unknown_or_unmapped_pct_mean']`` MUST
         equal ``50.0`` (not ``0.0``!).
    """
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"asian": 200.0, "white": 800.0}, 2.0),
            ("26420", "Houston", 1000.0, {"asian": 200.0, "white": 800.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99500003",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 5, "white": 45, "unknown_not_reported": 0, "indigenous": 50},
            "raw_categories": [
                {"original": "Asian", "count": 5},
                {"original": "White", "count": 45},
                {"original": "Indigenous", "count": 50},
            ],
        },
    }
    t_out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    assert t_out["dimensions"]["race"]["denominators"]["unknown_or_unmapped_pct"] == 50.0
    asian_comp = t_out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]["by_category"]["asian"]
    assert asian_comp["obs_ceiling_pct"] == 55.0
    assert asian_comp["joint_direction_settled"] is False

    pooled = ms_benchmark.pool_multisite_cohort(
        [t_out, copy.deepcopy(t_out)], level="cbsa", dimension="race", floor=1, resamples=20, seed=1
    )
    assert pooled["denominators"]["unknown_or_unmapped_pct_mean"] == 50.0


def test_all_six_footprint_tiers_in_cohort_input_only_multi_site_us_only_enters_primary_pool() -> None:
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"white": 700.0}, 2.0),
            ("26420", "Houston", 1000.0, {"white": 400.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    base_race = {
        "reported": True,
        "omb_totals": {"white": 80, "unknown_not_reported": 20},
        "raw_categories": [{"original": "White", "count": 80}],
    }
    tier_locs = [
        ("NCT99500201", []),
        ("NCT99500202", [{"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}]),
        (
            "NCT99500203",
            [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
        ),
        (
            "NCT99500204",
            [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "PR", "city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
            ],
        ),
        (
            "NCT99500205",
            [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "DE", "city": "Berlin", "country": "Germany", "zip": "10115"},
            ],
        ),
        ("NCT99500206", [{"facility": "DE", "city": "Berlin", "country": "Germany", "zip": "10115"}]),
    ]
    benchmarked = [
        ms_benchmark.benchmark_multisite_trial(
            {
                "nct_id": nct,
                "min_age": "18 Years",
                "max_age": "64 Years",
                "start_date": "2021-01-01",
                "locations": locs,
                "race": base_race,
            },
            {"cbsa": cbsa_lvl},
            resolver,
        )
        for nct, locs in tier_locs
    ]
    pooled = ms_benchmark.pool_multisite_cohort(
        benchmarked, level="cbsa", dimension="race", floor=1, resamples=25, seed=4
    )
    assert all(v == 1 for v in pooled["footprint_counts"].values())
    assert pooled["n_multisite_us_candidates"] == 1
    assert pooled["n_trials"] == 1
    assert pooled["n_participants"] == 100
    assert pooled["mixed_us_and_international_diagnostic"]["n_trials_total"] == 1


def test_pool_multisite_cohort_floor_boundary_withholds_at_floor_minus_one_and_shows_at_floor() -> None:
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"white": 700.0}, 2.0),
            ("26420", "Houston", 1000.0, {"white": 400.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    items = [
        ms_benchmark.benchmark_multisite_trial(
            {
                "nct_id": f"NCT{99500300 + i}",
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
        for i in range(5)
    ]
    assert (
        ms_benchmark.pool_multisite_cohort(items[:4], floor=5, resamples=30)["display_rule"]
        == "withheld"
    )
    assert (
        ms_benchmark.pool_multisite_cohort(items, floor=5, resamples=30)["display_rule"]
        == "show"
    )


def test_pool_multisite_cohort_floor_zero_on_empty_cohort_does_not_crash_with_broadcast_error() -> None:
    """``pool_multisite_cohort([], floor=0)`` must either withhold an empty 0-trial cohort
    cleanly or raise a clear ``floor`` validation ``ValueError``, never crash inside numpy
    with ``ValueError: operands could not be broadcast together``.
    """
    try:
        out = ms_benchmark.pool_multisite_cohort([], floor=0, resamples=10)
    except ValueError as exc:
        assert "floor" in str(exc).lower()
    else:
        json.dumps(out, allow_nan=False)
        assert out["display_rule"] == "withheld"
        assert out["n_trials"] == 0


def test_pool_multisite_cohort_resamples_zero_raises_value_error_not_numpy_index_error() -> None:
    """``resamples=0`` must either validate ``resamples >= 1`` via ``ValueError`` or return
    valid JSON without raising raw numpy ``IndexError`` inside ``np.percentile``.
    """
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"white": 700.0}, 2.0),
            ("26420", "Houston", 1000.0, {"white": 400.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    t_out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99500401",
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
    try:
        out = ms_benchmark.pool_multisite_cohort([t_out, t_out], floor=1, resamples=0)
    except ValueError as exc:
        assert "resamples" in str(exc).lower()
    else:
        json.dumps(out, allow_nan=False)


def test_pool_multisite_cohort_zero_participant_trials_never_emit_nan_in_json() -> None:
    """Records with ``n_in_table == 0`` passed to ``pool_multisite_cohort`` (either all ``n=0``
    or mixed with ``n > 0`` trials where bootstrap draws sample only ``n=0`` rows) must never
    emit ``NaN`` in participant-weighted means or bootstrap intervals under ``allow_nan=False``.
    """
    cbsa_lvl = _build_synthetic_level(
        [
            ("14460", "Boston", 1000.0, {"white": 700.0}, 2.0),
            ("26420", "Houston", 1000.0, {"white": 400.0}, 2.0),
        ]
    )
    resolver = TableResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    valid_trial = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99500501",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"white": 75, "unknown_not_reported": 25},
                "raw_categories": [{"original": "White", "count": 75}],
            },
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    zero_a = copy.deepcopy(valid_trial)
    zero_a["nct_id"] = "NCT99500502"
    zero_a["dimensions"]["race"]["n_in_table"] = 0
    zero_b = copy.deepcopy(zero_a)
    zero_b["nct_id"] = "NCT99500503"

    mixed_out = ms_benchmark.pool_multisite_cohort(
        [zero_a, zero_b, valid_trial], level="cbsa", dimension="race", floor=1, resamples=200, seed=1
    )
    json.dumps(mixed_out, allow_nan=False)

    all_zero_out = ms_benchmark.pool_multisite_cohort(
        [zero_a, zero_b], level="cbsa", dimension="race", floor=1, resamples=50, seed=1
    )
    json.dumps(all_zero_out, allow_nan=False)


def test_joint_settlement_cohort_counts_partition_qualifying_trials_exactly(
    fuzz_catalog: tuple[dict[str, acs.Level], TableResolver, list[dict[str, str]]],
) -> None:
    levels, resolver, templates = fuzz_catalog
    rng = np.random.default_rng(555)
    cohort_items = []
    for idx in range(55):
        w_cnt = int(rng.integers(0, 101))
        u_cnt = int(rng.integers(0, 101 - w_cnt))
        b_cnt = 100 - w_cnt - u_cnt
        locs = [
            {"facility": "A", **templates[int(rng.integers(0, len(templates)))]},
            {"facility": "B", **templates[int(rng.integers(0, len(templates)))]},
        ]
        if idx % 7 == 0:
            locs.append({"facility": "Rural", "city": "Bozeman", "state": "MT", "zip": "59715", "country": "United States"})
        rec = {
            "nct_id": f"NCT{99500600 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": locs,
            "race": {
                "reported": True,
                "omb_totals": {"white": w_cnt, "black_african_american": b_cnt, "unknown_not_reported": u_cnt},
                "raw_categories": [{"original": "White", "count": w_cnt}, {"original": "Black", "count": b_cnt}],
            },
        }
        cohort_items.append(ms_benchmark.benchmark_multisite_trial(rec, levels, resolver))

    pooled = ms_benchmark.pool_multisite_cohort(
        cohort_items, level="cbsa", dimension="race", floor=50, resamples=100, seed=12
    )
    assert pooled["display_rule"] == "show"
    for cat in bounds.COMPARED_CATEGORIES["race"]:
        js = pooled["joint_settlement"][cat]
        assert js["n_settled_negative"] + js["n_settled_positive"] + js["n_unsettled"] == js["n_trials"]
        assert (
            js["n_unsettled_incomplete_resolution"] + js["n_unsettled_within_bounds_or_unknowns"]
            == js["n_unsettled"]
        )


def test_all_unknown_baseline_table_withholds_denominator_b_with_no_classified_reason() -> None:
    cbsa_lvl = _build_synthetic_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    rec = {
        "nct_id": "NCT99500701",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Cambridge", "state": "MA", "zip": "02114", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"white": 0, "unknown_not_reported": 50},
            "raw_categories": [],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    assert out["dimensions"]["race"]["observed_pct_of_classified"] is None
    assert out["dimensions"]["race"]["classified_share_reason"] == "no_classified_participants"
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["of_classified"] is None
    assert out["dimensions"]["race"]["benchmarks"]["cbsa"]["joint_direction_settled"]["white"] is False


# =============================================================================================
# 6. DOCUMENTATION (README.md), PUBLIC SIGNATURES & SCHEMA_VERSION AUDIT
# =============================================================================================


def test_schema_and_method_versions_match_across_package_and_outputs() -> None:
    assert catchments.SCHEMA_VERSION == 1
    assert catchments.METHOD_VERSION == "catchments-multisite-1"

    cbsa_lvl = _build_synthetic_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = TableResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    t_out = ms_benchmark.benchmark_multisite_trial(
        {"nct_id": "NCT99600001", "min_age": "18 Years", "start_date": "2021-01-01", "locations": []},
        {"cbsa": cbsa_lvl},
        resolver,
    )
    p_out = ms_benchmark.pool_multisite_cohort([t_out], floor=5)
    assert t_out["schema_version"] == p_out["schema_version"] == catchments.SCHEMA_VERSION
    assert t_out["method_version"] == p_out["method_version"] == catchments.METHOD_VERSION


def test_readme_documented_tiers_functions_and_field_names_match_implementation() -> None:
    pkg_dir = Path(__file__).resolve().parents[2] / "catchments"
    readme_text = (pkg_dir / "README.md").read_text(encoding="utf-8")

    for tier in footprint.FOOTPRINT_TIERS:
        assert f'"{tier}"' in readme_text

    expected_Callables = [
        (bounds, "site_allocation_bounds"),
        (bounds, "equal_facility_proxy"),
        (bounds, "distinct_area_resident_proxy"),
        (bounds, "compare_with_catchment_bounds"),
        (ms_benchmark, "benchmark_multisite_trial"),
        (ms_benchmark, "pool_multisite_cohort"),
    ]
    for mod, fn_name in expected_Callables:
        assert fn_name in readme_text
        fn = getattr(mod, fn_name)
        assert callable(fn)
        sig = inspect.signature(fn)
        assert len(sig.parameters) >= 1

    for documented_field in (
        "bounds_unconditional",
        "resolved_min_pct",
        "resolved_max_pct",
        "unconditional_min_pct",
        "unconditional_max_pct",
        "n_unresolved_or_unavailable",
        "site_only_settled",
        "site_weight_settled",
        "joint_direction_settled",
        "obs_floor_pct",
        "obs_ceiling_pct",
        "joint_diff_pp_min",
        "joint_diff_pp_max",
        "of_classified",
        "classified_share_reason",
        "global_pooled_baseline_table",
    ):
        assert documented_field in readme_text


def test_catchments_source_and_docs_enforce_fictional_nct99_ids_and_full_type_annotations() -> None:
    pkg_dir = Path(__file__).resolve().parents[2] / "catchments"
    nct_rx = re.compile(r"NCT\d+")
    valid_nct99_rx = re.compile(r"^" + "NCT" + r"99\d{6}$")

    for path in sorted(pkg_dir.iterdir()):
        if path.suffix not in (".py", ".md"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "/" + "Users/" not in text
        for match in nct_rx.findall(text):
            assert valid_nct99_rx.fullmatch(match), f"Non-fictional NCT ID {match} in {path.name}"

        if path.suffix == ".py":
            tree = ast.parse(text, filename=path.name)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert node.returns is not None, f"Missing return annotation on {path.name}:{node.name}"
                    all_args = (
                        node.args.posonlyargs
                        + node.args.args
                        + node.args.kwonlyargs
                    )
                    for arg in all_args:
                        if arg.arg in ("self", "cls"):
                            continue
                        assert arg.annotation is not None, (
                            f"Missing annotation on {path.name}:{node.name}({arg.arg})"
                        )
