"""Round-2 adversarial verification and second-order regression tests for ``catchments/``.

Audits every Round-1 fix in ``catchments/footprint.py``, ``catchments/bounds.py``,
``catchments/benchmark.py``, and ``catchments/README.md`` without modifying ``catchments/``
or ``tests/catchments/test_redteam_round1.py``:
  1. Stress-tests the ``1e-12`` IEEE-754 tie guard at ``N = 10^9`` and ``N = 10^{13}``,
     multi-band fractional age weights, 200 near-tie boundary trials, proxy ``unknown_bounds``
     rounding consistency, and ``joint_diff_pp_*`` vs ``resolved_diff_pp_*`` identity when
     ``bounds_unconditional == True``.
  2. Verifies partial ACS sub-table ``NaN`` handling across ``race``, ``ethnicity``, and
     ``sex`` (full-area exclusion for race partitioning and ``skipped_geoids`` parity between
     ``site_allocation_bounds`` and ``distinct_area_resident_proxy``).
  3. Attacks jurisdiction classification with overseas US military APO/FPO/DPO bases
     (``AA``, ``AE``, ``AP``, ``09xxx``/``340xx``/``962xx-966xx``), British Virgin Islands
     (``British Virgin Islands``, ``VG``, ``VGB``, ``BVI``, ``Tortola``, ``VG1110``) vs U.S.
     Virgin Islands, ``Georgia`` (Caucasus country vs US state), and ``New Mexico`` vs ``Mexico``.
  4. Tests parameter and input-type guards (``resamples = -5``, ``resamples = True``,
     ``floor = -1``, ``floor = True``, boolean/NaN ``n_in_table`` and facility counts).
"""
from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pytest

from catchments import benchmark as ms_benchmark, bounds, footprint
from census import acs, geography


EDGES_3: list[tuple[float, float | None]] = [
    (0.0, 18.0),
    (18.0, 65.0),
    (65.0, None),
]


def _iter_ast_nodes(root: ast.AST) -> Iterator[ast.AST]:
    """Iterative AST traversal avoiding ``.walk`` for strict overnight lint compatibility."""
    stack: list[ast.AST] = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _make_level(
    areas: list[tuple[str, str, float, dict[str, float], float]],
    *,
    edges: list[tuple[float, float | None]] | None = None,
) -> acs.Level:
    use_edges = list(edges) if edges is not None else list(EDGES_3)
    n_bands = len(use_edges)
    est = np.zeros((len(areas), len(acs.TABLE_ORDER), 2, n_bands), dtype=float)
    moe = np.zeros((len(areas), len(acs.TABLE_ORDER), 2, n_bands), dtype=float)
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

    return acs.Level(geoids, names, use_edges, est, moe)


class SimpleResolver:
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


# =============================================================================================
# 1. 1e-12 IEEE TIE-GUARD STRESS TESTS & PROXY / ENVELOPE CONSISTENCY
# =============================================================================================


@pytest.mark.parametrize("prime_denom", [999_999_937, 9_999_999_999_971])
def test_large_denominator_1e9_and_1e13_exact_prime_ties_never_falsely_settle(
    prime_denom: int,
) -> None:
    """Exact rational equality at ``N ~ 10^9`` and ``N ~ 10^{13}`` (including ethnicity
    complement ``100.0 - hisp`` vs ``100.0 * (N - k) / N``) must never flip strict ``<`` or
    ``>`` due to IEEE-754 representation drift.
    """
    asian_n = prime_denom // 3
    white_n = prime_denom - asian_n
    hisp_n = prime_denom // 7
    nhisp_n = prime_denom - hisp_n

    cbsa_lvl = _make_level(
        [
            (
                "14460",
                "Metro A",
                float(prime_denom),
                {"asian": float(asian_n), "white": float(white_n), "hispanic_latino": float(hisp_n)},
                1.0,
            ),
            (
                "26420",
                "Metro B",
                float(prime_denom),
                {"asian": float(asian_n), "white": float(white_n), "hispanic_latino": float(hisp_n)},
                1.0,
            ),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99700101",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": asian_n, "white": white_n, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": asian_n}, {"original": "White", "count": white_n}],
        },
        "ethnicity": {
            "reported": True,
            "omb_totals": {"hispanic_latino": hisp_n, "not_hispanic_latino": nhisp_n, "unknown_not_reported": 0},
            "raw_categories": [
                {"omb_category": "hispanic_latino", "original": "Hispanic", "count": hisp_n},
                {"omb_category": "not_hispanic_latino", "original": "Not Hispanic", "count": nhisp_n},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    for dim, cat in (("race", "asian"), ("ethnicity", "not_hispanic_latino")):
        b = out["dimensions"][dim]["benchmarks"]["cbsa"]
        assert b["site_only_settled"][cat] is False
        assert b["joint_direction_settled"][cat] is False
        assert b["of_classified"]["by_category"][cat]["direction_settled"] is False


@pytest.mark.parametrize("denom", [1_000_000_000, 10_000_000_000_000])
def test_large_denominator_1e9_and_1e13_single_participant_strict_excess_settles_positive(
    denom: int,
) -> None:
    """At ``N = 10^9`` (``+1e-7`` pp) and ``N = 10^{13}`` (``+1e-11`` pp ``> 1e-12``), a real
    ``+1`` participant excess with ``0`` unknowns settles strictly ``'positive'``.
    """
    site_asian = (denom * 3) // 10
    trial_asian = site_asian + 1
    cbsa_lvl = _make_level(
        [
            ("14460", "A", float(denom), {"asian": float(site_asian), "white": float(denom - site_asian)}, 1.0),
            ("26420", "B", float(denom), {"asian": float(site_asian), "white": float(denom - site_asian)}, 1.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99700102",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": trial_asian, "white": denom - trial_asian, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": trial_asian}, {"original": "White", "count": denom - trial_asian}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    cbsa = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa["site_only_settled"]["asian"] is True
    assert cbsa["joint_direction_settled"]["asian"] is True
    assert cbsa["joint_direction"]["asian"] == "positive"


def test_sub_epsilon_1e15_difference_errs_conservatively_unsettled_never_wrong_sign() -> None:
    """When two shares differ by ``~1e-13`` pp (below ``_STRICT_INEQ_EPS = 1e-12``, near binary64
    machine precision), ``compare_with_catchment_bounds`` stays conservatively ``unsettled`` and
    never reports the wrong sign.
    """
    alloc = bounds.site_allocation_bounds(
        {
            "14460": {
                "available": True,
                "pct": {"asian": 30.0},
                "_exact_pct": {"asian": 30.0},
                "moe90_pp": {"asian": 0.5},
            }
        },
        {"14460": 1},
        n_us_facilities=1,
    )
    comp = bounds.compare_with_catchment_bounds(
        {"asian": 30.0},
        0.0,
        alloc,
        exact_observed_floor_pct={"asian": 30.0 + 1e-13},
        exact_observed_ceiling_pct={"asian": 30.0 + 1e-13},
    )
    assert comp["joint_direction_settled"]["asian"] is False
    assert comp["joint_direction"]["asian"] == "unsettled"


def test_multi_band_fractional_age_weights_ieee_ulp_drift_never_triggers_false_settlement() -> None:
    """Across 8 age bands with non-terminating ``band_weights`` (``min_age = '19 Months'``,
    ``max_age = '89 Years'``), an exact ``1/3`` share across every band must never trigger
    false ``settled = True`` from IEEE-754 summation drift.
    """
    edges_8: list[tuple[float, float | None]] = [
        (0.0, 5.0),
        (5.0, 15.0),
        (15.0, 25.0),
        (25.0, 35.0),
        (35.0, 50.0),
        (50.0, 65.0),
        (65.0, 85.0),
        (85.0, None),
    ]
    est = np.zeros((1, len(acs.TABLE_ORDER), 2, len(edges_8)), dtype=float)
    moe = np.ones_like(est)
    t_tot = acs.TABLE_ORDER.index("total")
    t_as = acs.TABLE_ORDER.index("asian")
    t_wh = acs.TABLE_ORDER.index("white")
    for b_idx in range(len(edges_8)):
        est[0, t_tot, :, b_idx] = 300.0 * (b_idx + 1)
        est[0, t_as, :, b_idx] = 100.0 * (b_idx + 1)
        est[0, t_wh, :, b_idx] = 200.0 * (b_idx + 1)
    lvl = acs.Level(["14460"], ["Boston"], edges_8, est, moe)
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    rec = {
        "nct_id": "NCT99700103",
        "min_age": "19 Months",
        "max_age": "89 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Cambridge", "state": "MA", "zip": "02114", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 100, "white": 200, "unknown_not_reported": 0},
            "raw_categories": [{"original": "Asian", "count": 100}, {"original": "White", "count": 200}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": lvl}, resolver)
    cbsa = out["dimensions"]["race"]["benchmarks"]["cbsa"]
    assert cbsa["site_only_settled"]["asian"] is False
    assert cbsa["joint_direction_settled"]["asian"] is False


def test_proxy_unknown_bounds_never_claim_settled_on_identical_trial_and_site_shares() -> None:
    """Round-1 regression trap: Trial ``NCT99700104`` and BOTH sites have the EXACT SAME
    Asian composition ``133,333 / 2,000,000 = 6.66665%`` and ``0`` unknowns (exact diff = ``0.0``).
    Neither ``equal_facility_proxy['unknown_bounds']['asian']['direction_settled']`` nor
    ``distinct_area_resident_proxy['unknown_bounds']['asian']['direction_settled']`` may claim
    ``True`` when true difference is ``0.0``!
    """
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 2_000_000.0, {"asian": 133_333.0, "white": 1_866_667.0}, 2.0),
            ("26420", "B", 2_000_000.0, {"asian": 133_333.0, "white": 1_866_667.0}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99700104",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 133_333, "white": 1_866_667, "unknown_not_reported": 0},
            "raw_categories": [
                {"original": "Asian", "count": 133_333},
                {"original": "White", "count": 1_866_667},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    comp = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]
    assert comp["joint_direction_settled"]["asian"] is False
    assert comp["equal_facility_proxy"]["unknown_bounds"]["asian"]["direction_settled"] is False
    assert comp["distinct_area_resident_proxy"]["unknown_bounds"]["asian"]["direction_settled"] is False


def test_proxy_unknown_bounds_never_claim_settled_negative_when_true_ceiling_exceeds_proxy() -> None:
    """When both sites have ``133,331 / 2,000,000 = 6.66655%`` Asian and trial ``NCT99700105``
    has ``20`` Asian + ``20`` Unknown out of ``600`` (true ceiling ``6.666667% > 6.66655%``),
    the unknown interval against BOTH proxies crosses zero (``[-3.333, +0.000117]``), so proxy
    ``unknown_bounds['asian']['direction_settled']`` MUST be ``False``.
    """
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 2_000_000.0, {"asian": 133_331.0, "white": 1_866_669.0}, 2.0),
            ("26420", "B", 2_000_000.0, {"asian": 133_331.0, "white": 1_866_669.0}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99700105",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"asian": 20, "white": 560, "unknown_not_reported": 20},
            "raw_categories": [{"original": "Asian", "count": 20}, {"original": "White", "count": 560}],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    comp = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]
    assert comp["equal_facility_proxy"]["unknown_bounds"]["asian"]["direction_settled"] is False
    assert comp["distinct_area_resident_proxy"]["unknown_bounds"]["asian"]["direction_settled"] is False


def test_unconditional_joint_diff_matches_resolved_diff_identically_on_denominator_a_and_b() -> None:
    """When ``bounds_unconditional == True`` (100% of US facilities resolved with ACS data),
    the unconditional envelope equals the resolved envelope, so ``joint_diff_pp_min ==
    resolved_diff_pp_min`` and ``joint_diff_pp_max == resolved_diff_pp_max`` (and likewise on
    ``of_classified``) must agree identically.
    """
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 1_000_000.0, {"white": 49_996.0, "asian": 950_004.0}, 2.0),
            ("26420", "B", 1_000_000.0, {"white": 49_996.0, "asian": 950_004.0}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99700106",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
        "race": {
            "reported": True,
            "omb_totals": {"white": 100_004, "asian": 899_996, "unknown_not_reported": 0},
            "raw_categories": [
                {"original": "White", "count": 100_004},
                {"original": "Asian", "count": 899_996},
            ],
        },
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
    comp = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]
    assert comp["bounds_unconditional"] is True
    w_cat = comp["by_category"]["white"]
    assert w_cat["joint_diff_pp_min"] == w_cat["resolved_diff_pp_min"]
    assert w_cat["joint_diff_pp_max"] == w_cat["resolved_diff_pp_max"]
    wb_cat = comp["of_classified"]["by_category"]["white"]
    assert wb_cat["diff_pp_min"] == wb_cat["resolved_diff_pp_min"]
    assert wb_cat["diff_pp_max"] == wb_cat["resolved_diff_pp_max"]


def test_fuzz_200_near_tie_trials_within_half_millipp_never_fabricate_wrong_sign_settlement() -> None:
    """Fuzz 200 multi-site trials (``NCT99710001``..``NCT99710200``) whose exact observed floor
    and ceiling sit within ``[-0.0006, +0.0006]`` pp of ``min_site_share`` / ``max_site_share``.
    Proves ``joint_direction_settled`` is sound across every single trial: whenever ``True``,
    every simplex weight vector and every unknown split has the exact claimed strict sign.
    """
    rng = np.random.default_rng(202602)
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    for idx in range(1, 201):
        site_den = 1_000_000
        min_num = int(rng.integers(50_000, 300_000))
        max_num = min_num + int(rng.integers(0, 200_000))
        cbsa_lvl = _make_level(
            [
                ("14460", "A", float(site_den), {"asian": float(min_num), "white": float(site_den - min_num)}, 1.0),
                ("26420", "B", float(site_den), {"asian": float(max_num), "white": float(site_den - max_num)}, 1.0),
            ]
        )
        n_trial = int(rng.choice([300, 600, 700, 900, 1200, 3000]))
        # Place ceiling right near min_site_share or floor right near max_site_share
        if idx % 2 == 0:
            target_ceil = round(n_trial * (min_num / site_den))
            n_as = max(0, target_ceil // 2)
            n_unk = max(0, target_ceil - n_as)
        else:
            target_floor = round(n_trial * (max_num / site_den))
            n_as = min(n_trial, target_floor)
            n_unk = min(n_trial - n_as, int(rng.integers(0, 10)))
        n_wh = n_trial - n_as - n_unk

        rec = {
            "nct_id": f"NCT{99710000 + idx}",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                "omb_totals": {"asian": n_as, "white": n_wh, "unknown_not_reported": n_unk},
                "raw_categories": [{"original": "Asian", "count": n_as}, {"original": "White", "count": n_wh}],
            },
        }
        out = ms_benchmark.benchmark_multisite_trial(rec, {"cbsa": cbsa_lvl}, resolver)
        c_as = out["dimensions"]["race"]["benchmarks"]["cbsa"]["comparison"]["by_category"]["asian"]
        true_floor = 100.0 * n_as / n_trial
        true_ceil = 100.0 * (n_as + n_unk) / n_trial
        true_min = 100.0 * min_num / site_den
        true_max = 100.0 * max_num / site_den

        if c_as["joint_direction_settled"]:
            if c_as["joint_direction"] == "negative":
                assert true_ceil < true_min
            else:
                assert c_as["joint_direction"] == "positive"
                assert true_floor > true_max


# =============================================================================================
# 2. PARTIAL ACS SUB-TABLE NaNs, RACE PARTITION INTEGRITY & skipped_geoids PARITY
# =============================================================================================


def test_partial_race_subtable_nan_marks_entire_area_unavailable_for_all_race_categories() -> None:
    """When ``B01001D`` (Asian) is ``NaN`` in ZCTA ``77030`` while ``B01001A`` (White) and
    ``B01001B`` (Black) are finite, ``77030`` MUST be excluded for ALL race categories (including
    ``white`` and ``black_african_american``) so race shares stay on one consistent area set,
    while ``ethnicity`` and ``sex`` (whose tables are finite) remain unconditional!
    """
    zcta_lvl = _make_level(
        [
            (
                "02114",
                "Boston",
                1000.0,
                {"white": 700.0, "black_african_american": 200.0, "asian": 100.0, "hispanic_latino": 150.0},
                2.0,
            ),
            (
                "77030",
                "Houston Partial NaN",
                1000.0,
                {"white": 500.0, "black_african_american": 300.0, "asian": float("nan"), "hispanic_latino": 400.0},
                2.0,
            ),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99720001",
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
            "omb_totals": {"white": 95, "black_african_american": 5, "asian": 0, "unknown_not_reported": 0},
            "raw_categories": [{"original": "White", "count": 95}, {"original": "Black", "count": 5}],
        },
        "ethnicity": {
            "reported": True,
            "omb_totals": {"hispanic_latino": 5, "not_hispanic_latino": 95, "unknown_not_reported": 0},
            "raw_categories": [
                {"omb_category": "hispanic_latino", "original": "Hispanic", "count": 5},
                {"omb_category": "not_hispanic_latino", "original": "Not Hispanic", "count": 95},
            ],
        },
        "sex": {"reported": True, "totals": {"female": 80, "male": 20, "unknown": 0}},
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    json.dumps(out, allow_nan=False)

    race_z = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert race_z["n_acs_available_facilities"] == 1
    assert race_z["bounds_unconditional"] is False
    for rcat in bounds.COMPARED_CATEGORIES["race"]:
        assert race_z["site_only_settled"][rcat] is False
        assert race_z["joint_direction_settled"][rcat] is False

    # Ethnicity and sex tables are finite in both ZCTAs -> unconditional and settled!
    assert out["dimensions"]["ethnicity"]["benchmarks"]["zcta"]["bounds_unconditional"] is True
    assert out["dimensions"]["ethnicity"]["benchmarks"]["zcta"]["joint_direction_settled"]["hispanic_latino"] is True
    assert out["dimensions"]["sex"]["benchmarks"]["zcta"]["bounds_unconditional"] is True
    assert out["dimensions"]["sex"]["benchmarks"]["zcta"]["joint_direction_settled"]["female"] is True


def test_distinct_area_resident_proxy_direct_call_matches_site_allocation_bounds_unavailable_geoids() -> None:
    zcta_lvl = _make_level(
        [
            ("02114", "Valid", 1000.0, {"white": 700.0, "asian": 100.0}, 2.0),
            ("77030", "Subtable NaN", 1000.0, {"white": 500.0, "asian": float("nan")}, 2.0),
            ("99999", "Zero Adults", 0.0, {}, 1.0),
        ]
    )
    fac_counts = {"02114": 1, "77030": 1, "88888": 1, "99999": 1}
    area_est = {
        "02114": {"available": True, "pct": {"white": 70.0, "asian": 10.0}, "moe90_pp": {"white": 1.0, "asian": 1.0}},
        "77030": {"available": True, "pct": {"white": 50.0, "asian": float("nan")}, "moe90_pp": {"white": 1.0, "asian": None}},
        "88888": {"available": False, "reason": "area_not_in_this_window_file"},
        "99999": {"available": False, "reason": "no_eligible_residents_estimated"},
    }
    alloc = bounds.site_allocation_bounds(
        area_est, fac_counts, n_us_facilities=4, categories=("white", "asian")
    )
    res_proxy = bounds.distinct_area_resident_proxy(
        zcta_lvl, list(fac_counts.keys()), "race", ("male", "female"), 18.0, 65.0
    )
    assert res_proxy["skipped_geoids"] == alloc["unavailable_resolved_geoids"] == {
        "77030": "estimate_not_published",
        "88888": "area_not_in_this_window_file",
        "99999": "no_eligible_residents_estimated",
    }


def test_benchmark_multisite_trial_resident_proxy_records_skipped_resolved_geoids_matching_bounds() -> None:
    """End-to-end ``benchmark_multisite_trial`` parity: when a resolved ``geoid`` at level ``L``
    is excluded due to missing/suppressed ACS data, ``distinct_area_resident_proxy['skipped_geoids']``
    must match ``site_allocation_bounds['unavailable_resolved_geoids']`` rather than silently
    omitting ``skipped_geoids``.
    """
    zcta_lvl = _make_level(
        [
            ("02114", "Valid", 1000.0, {"white": 700.0, "asian": 100.0}, 2.0),
            ("77030", "Subtable NaN", 1000.0, {"white": 500.0, "asian": float("nan")}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99720002",
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
    assert zb["site_allocation_bounds"]["unavailable_resolved_geoids"] == {
        "77030": "estimate_not_published"
    }
    assert zb["distinct_area_resident_proxy"].get("skipped_geoids") == {
        "77030": "estimate_not_published"
    }


def test_all_resolved_areas_having_partial_subtable_nan_disables_level_with_estimate_not_published() -> None:
    zcta_lvl = _make_level(
        [
            ("02114", "A", 1000.0, {"white": 700.0, "asian": float("nan")}, 2.0),
            ("77030", "B", 1000.0, {"white": 500.0, "asian": float("nan")}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99720003",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    json.dumps(out, allow_nan=False)
    zb = out["dimensions"]["race"]["benchmarks"]["zcta"]
    assert zb["available"] is False
    assert zb["reason"] == "estimate_not_published"


def test_ethnicity_subtable_nan_disables_ethnicity_while_preserving_race_and_sex() -> None:
    zcta_lvl = _make_level(
        [
            ("02114", "A", 1000.0, {"white": 700.0, "hispanic_latino": float("nan")}, 2.0),
            ("77030", "B", 1000.0, {"white": 500.0, "hispanic_latino": float("nan")}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    rec = {
        "nct_id": "NCT99720004",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [
            {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
            {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
        ],
    }
    out = ms_benchmark.benchmark_multisite_trial(rec, {"zcta": zcta_lvl}, resolver)
    json.dumps(out, allow_nan=False)
    assert out["dimensions"]["ethnicity"]["benchmarks"]["zcta"]["available"] is False
    assert out["dimensions"]["ethnicity"]["benchmarks"]["zcta"]["reason"] == "estimate_not_published"
    assert out["dimensions"]["race"]["benchmarks"]["zcta"]["available"] is True
    assert out["dimensions"]["sex"]["benchmarks"]["zcta"]["available"] is True


def test_single_sex_cell_nan_in_total_table_marks_area_unavailable_cleanly() -> None:
    est = np.zeros((1, len(acs.TABLE_ORDER), 2, 3), dtype=float)
    moe = np.ones_like(est)
    t_tot = acs.TABLE_ORDER.index("total")
    est[0, t_tot, 0, 1] = 500.0
    est[0, t_tot, 1, 1] = float("nan")  # Female cell suppressed
    lvl = acs.Level(["02114"], ["Boston"], list(EDGES_3), est, moe)
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99720005",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
        },
        {"zcta": lvl},
        resolver,
    )
    json.dumps(out, allow_nan=False)
    assert out["dimensions"]["sex"]["benchmarks"]["zcta"]["available"] is False


# =============================================================================================
# 3. MILITARY APO/FPO/DPO, BRITISH VS U.S. VIRGIN ISLANDS, GEORGIA & MEXICO TRAPS
# =============================================================================================


@pytest.mark.parametrize(
    "mil_loc",
    [
        {"facility": "Landstuhl RMC", "city": "APO", "state": "AE", "zip": "09180", "country": "United States"},
        {"facility": "Ramstein Clinic", "city": "APO", "state": "Armed Forces Europe", "zip": "09012", "country": "United States"},
        {"facility": "Soto Cano Clinic", "city": "APO", "state": "AA", "zip": "34042", "country": "United States"},
        {"facility": "Guantanamo Clinic", "city": "FPO", "state": "Armed Forces Americas (AA)", "zip": "34002", "country": "United States"},
        {"facility": "Naval Hospital Okinawa", "city": "FPO", "state": "AP", "zip": "96362", "country": "United States"},
        {"facility": "Brian Allgood Hospital", "city": "APO", "state": "Armed Forces Pacific", "zip": "96205", "country": "United States"},
    ],
)
def test_military_apo_fpo_dpo_state_codes_aa_ae_ap_never_classify_as_50_state_us(
    mil_loc: dict[str, str],
) -> None:
    """Overseas US military APO/FPO/DPO bases (``AA``, ``AE``, ``AP``; ``09xxx``, ``340xx``,
    ``962xx-966xx``) are outside the 50 US states + DC ACS frame and MUST NEVER be classified
    as ``us_state_or_dc``, ``multi_site_us_only``, or ``bounds_unconditional = True`` at ``us``.
    """
    boston = {"facility": "MGH", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}
    us_lvl = _make_level([("1", "United States", 5000.0, {"white": 3500.0}, 3.0)])
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    fac = footprint.normalize_facility(mil_loc)
    assert fac["jurisdiction"] != "us_state_or_dc"

    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99730001",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [boston, mil_loc],
        },
        {"us": us_lvl},
        resolver,
    )
    assert out["footprint"]["footprint_tier"] not in ("single_us_facility", "multi_site_us_only")
    assert out["footprint"]["eligible_for_us_multisite_rollup"] is False
    if out["dimensions"]["race"]["benchmarks"]["us"]["available"]:
        assert out["dimensions"]["race"]["benchmarks"]["us"]["bounds_unconditional"] is False


@pytest.mark.parametrize(
    "mil_zip_only",
    [
        {"facility": "Overseas Clinic E", "city": "APO", "zip": "09012", "country": "United States"},
        {"facility": "Overseas Clinic A", "city": "FPO", "zip": "34002", "country": "United States"},
        {"facility": "Overseas Clinic P", "city": "APO", "zip": "96205", "country": "United States"},
    ],
)
def test_military_apo_fpo_zips_without_state_under_united_states_never_classify_as_50_state_us(
    mil_zip_only: dict[str, str],
) -> None:
    fac = footprint.normalize_facility(mil_zip_only)
    assert fac["jurisdiction"] != "us_state_or_dc"


@pytest.mark.parametrize(
    "bvi_loc",
    [
        {"city": "Road Town", "country": "British Virgin Islands"},
        {"city": "Road Town", "country": "Virgin Islands, British"},
        {"city": "Road Town", "country": "Virgin Islands (British)"},
        {"city": "Tortola", "country": "VG", "zip": "VG1110"},
        {"city": "Tortola", "country": "VGB", "zip": "VG1110"},
        {"city": "Tortola", "country": "BVI"},
        {"city": "Tortola", "country": "B.V.I."},
    ],
)
def test_british_virgin_islands_country_variants_classify_as_non_us(
    bvi_loc: dict[str, str],
) -> None:
    fac = footprint.normalize_facility(bvi_loc)
    assert fac["jurisdiction"] == "non_us"
    assert fac["zip5"] is None


@pytest.mark.parametrize(
    "bvi_ambig",
    [
        {"facility": "Peebles Hospital", "city": "Road Town, Tortola", "state": "British Virgin Islands", "zip": "VG1110", "country": "Virgin Islands"},
        {"facility": "Peebles Hospital", "city": "Tortola", "state": "BVI", "zip": "VG1110", "country": "Virgin Islands"},
        {"facility": "Peebles Hospital", "city": "Tortola", "zip": "VG1110", "country": "Virgin Islands"},
    ],
)
def test_british_virgin_islands_state_or_vg_postal_code_under_generic_virgin_islands_country_is_non_us(
    bvi_ambig: dict[str, str],
) -> None:
    """When ``country='Virgin Islands'`` is paired with ``state='British Virgin Islands'`` /
    ``'BVI'`` / ``city='Tortola'`` or BVI postal code ``VG1110``, it MUST classify as ``non_us``
    rather than ``U.S. Virgin Islands``.
    """
    fac = footprint.normalize_facility(bvi_ambig)
    assert fac["jurisdiction"] == "non_us"
    assert fac["zip5"] is None


@pytest.mark.parametrize(
    "usvi_loc",
    [
        {"city": "Charlotte Amalie", "country": "Virgin Islands", "zip": "00802"},
        {"city": "St. Croix", "country": "U.S. Virgin Islands", "zip": "00820"},
        {"city": "St. Thomas", "country": "USVI", "zip": "00802"},
        {"city": "St. Thomas", "country": "VI", "zip": "00802"},
        {"city": "St. Thomas", "state": "USVI", "country": "United States"},
    ],
)
def test_us_virgin_islands_variants_classify_as_us_territory(
    usvi_loc: dict[str, str],
) -> None:
    fac = footprint.normalize_facility(usvi_loc)
    assert fac["jurisdiction"] == "us_territory"
    assert fac["state"] == "U.S. Virgin Islands"


def test_georgia_country_vs_us_state_disambiguation_across_country_fields() -> None:
    caucasus = footprint.normalize_facility(
        {"facility": "Tbilisi Clinic", "city": "Tbilisi", "country": "Georgia", "zip": "0108"}
    )
    atlanta_full = footprint.normalize_facility(
        {"facility": "Emory", "city": "Atlanta", "state": "Georgia", "zip": "30322", "country": "United States"}
    )
    atlanta_empty_country = footprint.normalize_facility(
        {"facility": "Emory", "city": "Atlanta", "state": "Georgia", "zip": "30322", "country": ""}
    )
    ga_abbr_no_zip = footprint.normalize_facility(
        {"facility": "Ambiguous", "city": "Unknown", "state": "GA", "country": ""}
    )
    assert caucasus["jurisdiction"] == "non_us" and caucasus["zip5"] is None
    assert atlanta_full["jurisdiction"] == "us_state_or_dc" and atlanta_full["state"] == "Georgia"
    assert atlanta_empty_country["jurisdiction"] == "us_state_or_dc" and atlanta_empty_country["state"] == "Georgia"
    assert ga_abbr_no_zip["jurisdiction"] == "non_us"


def test_new_mexico_vs_mexico_and_baja_california_never_confuse_jurisdictions() -> None:
    nm = footprint.normalize_facility(
        {"facility": "UNM", "city": "Albuquerque", "state": "New Mexico", "zip": "87131", "country": "United States"}
    )
    mx = footprint.normalize_facility(
        {"facility": "UNAM", "city": "Mexico City", "state": "Mexico", "zip": "04510", "country": "Mexico"}
    )
    mx_no_country = footprint.normalize_facility(
        {"facility": "UNAM", "city": "Toluca", "state": "Mexico", "zip": "50000", "country": ""}
    )
    baja = footprint.normalize_facility(
        {"facility": "Tijuana Hosp", "city": "Tijuana", "state": "Baja California", "zip": "22000", "country": ""}
    )
    assert nm["jurisdiction"] == "us_state_or_dc" and nm["state"] == "New Mexico"
    assert mx["jurisdiction"] == "non_us" and mx["zip5"] is None
    assert mx_no_country["jurisdiction"] == "non_us" and mx_no_country["zip5"] is None
    assert baja["jurisdiction"] == "non_us" and baja["zip5"] is None


@pytest.mark.parametrize("foreign_region", ["S.P.", "B.C.", "N.S.W.", "O.N.", "Q.C.", "U.K."])
def test_compact_dot_stripping_does_not_create_false_state_or_territory_collisions(
    foreign_region: str,
) -> None:
    fac = footprint.normalize_facility({"city": "Foreign City", "state": foreign_region})
    assert fac["jurisdiction"] == "non_us"


# =============================================================================================
# 4. PARAMETER VALIDATION (resamples, floor, BOOLEANS, DIRTY POOL ROWS) & INTEGRITY
# =============================================================================================


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"resamples": -5},
        {"resamples": 0},
        {"floor": -1},
        {"floor": -100},
    ],
)
def test_pool_multisite_cohort_rejects_negative_resamples_and_negative_floor(
    bad_kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        ms_benchmark.pool_multisite_cohort([], **bad_kwargs)


@pytest.mark.parametrize(
    "bool_kwargs",
    [
        {"resamples": True},
        {"resamples": False},
        {"floor": True},
        {"floor": False},
    ],
)
def test_pool_multisite_cohort_rejects_boolean_resamples_and_boolean_floor(
    bool_kwargs: dict[str, Any],
) -> None:
    """In Python ``isinstance(True, int)`` is ``True`` (``True == 1``, ``False == 0``):
    ``pool_multisite_cohort`` must reject boolean ``floor`` and ``resamples`` cleanly with
    ``TypeError`` or ``ValueError`` rather than crashing inside Cython or serializing booleans
    into ``parameters``.
    """
    cbsa_lvl = _make_level(
        [("14460", "A", 1000.0, {"white": 700.0}, 2.0), ("26420", "B", 1000.0, {"white": 400.0}, 2.0)]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    t_out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740001",
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
    with pytest.raises((TypeError, ValueError)):
        ms_benchmark.pool_multisite_cohort([t_out, t_out], **bool_kwargs)


def test_pool_multisite_cohort_handles_nan_or_boolean_n_in_table_in_corrupted_trial_dict() -> None:
    cbsa_lvl = _make_level(
        [("14460", "A", 1000.0, {"white": 700.0}, 2.0), ("26420", "B", 1000.0, {"white": 400.0}, 2.0)]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    valid = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740002",
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
    bad_nan = copy.deepcopy(valid)
    bad_nan["nct_id"] = "NCT99740003"
    bad_nan["dimensions"]["race"]["n_in_table"] = float("nan")

    bad_bool = copy.deepcopy(valid)
    bad_bool["nct_id"] = "NCT99740004"
    bad_bool["dimensions"]["race"]["n_in_table"] = True

    out = ms_benchmark.pool_multisite_cohort(
        [valid, bad_nan, bad_bool], level="cbsa", dimension="race", floor=1, resamples=30, seed=2
    )
    json.dumps(out, allow_nan=False)
    assert out["n_trials"] == 1


def test_site_allocation_bounds_rejects_boolean_n_us_facilities_and_boolean_counts() -> None:
    est = {"14460": {"available": True, "pct": {"white": 50.0}, "moe90_pp": {"white": 1.0}}}
    with pytest.raises((TypeError, ValueError)):
        bounds.site_allocation_bounds(est, {"14460": 1}, n_us_facilities=True)
    with pytest.raises((TypeError, ValueError)):
        bounds.site_allocation_bounds(est, {"14460": True}, n_us_facilities=1)


def test_sanitize_enrollment_handles_negative_inf_nan_bool_zero_and_integer_floats() -> None:
    cbsa_lvl = _make_level([("14460", "A", 1000.0, {"white": 700.0}, 2.0)])
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})

    for idx, raw_enrol, expected_enrol in [
        (1, -10, None),
        (2, float("inf"), None),
        (3, float("-inf"), None),
        (4, float("nan"), None),
        (5, False, None),
        (6, 0, 0),
        (7, 250.0, 250),
    ]:
        out = ms_benchmark.benchmark_multisite_trial(
            {
                "nct_id": f"NCT{99740100 + idx}",
                "enrollment": raw_enrol,
                "min_age": "18 Years",
                "start_date": "2021-01-01",
                "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
                "race": {
                    "reported": True,
                    "omb_totals": {"white": 50},
                    "raw_categories": [{"original": "White", "count": 50}],
                },
            },
            {"cbsa": cbsa_lvl},
            resolver,
        )
        json.dumps(out, allow_nan=False)
        assert out["registered_enrollment"] == expected_enrol
        if expected_enrol in (None, 0):
            assert out["dimensions"]["race"]["table_to_enrollment_ratio"] is None
        else:
            assert out["dimensions"]["race"]["table_to_enrollment_ratio"] == 0.2


def test_vintage_2009_with_puerto_rico_and_us_state_facility_preserves_consistent_resolution_counts() -> None:
    cbsa_lvl = _make_level([("14460", "Boston", 1000.0, {"white": 700.0}, 2.0)])
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"}})
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740201",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2006-01-01",
            "primary_completion_date": "2008-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "PR", "city": "San Juan", "country": "Puerto Rico", "zip": "00901"},
            ],
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    for lvl in footprint.LEVEL_ORDER:
        summary = out["resolution"]["by_level"][lvl]
        fac_cells = [f["levels"][lvl] for f in out["resolution"]["facilities"]]
        assert sum(1 for c in fac_cells if c.get("geoid")) == summary["n_resolved_facilities"]
        assert sum(summary["unresolved_reasons"].values()) == summary["n_unresolved_facilities"]


def test_empty_locations_and_null_study_sites_falls_back_to_api_v2_contacts_locations() -> None:
    rec = {
        "locations": [],
        "study_sites": [None],
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99740202"},
            "contactsLocationsModule": {
                "locations": [
                    {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                    {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
                ]
            },
        },
    }
    fp = footprint.classify_footprint(rec)
    assert fp["n_facilities"] == 2
    assert fp["footprint_tier"] == "multi_site_us_only"


def test_ethnicity_proxy_complement_exact_sum_to_100_across_odd_facility_weights() -> None:
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 700.0, {"hispanic_latino": 111.0}, 2.0),
            ("26420", "B", 1300.0, {"hispanic_latino": 433.0}, 3.0),
            ("31080", "C", 1700.0, {"hispanic_latino": 819.0}, 4.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
            "90095": {"zcta": "90095", "county": "06037", "cbsa": "31080"},
        }
    )
    locs = (
        [{"facility": "A1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}]
        + [
            {"facility": f"B{i}", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"}
            for i in range(2)
        ]
        + [
            {"facility": f"C{i}", "city": "LA", "state": "CA", "zip": "90095", "country": "United States"}
            for i in range(4)
        ]
    )
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740301",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": locs,
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    eth_b = out["dimensions"]["ethnicity"]["benchmarks"]["cbsa"]
    for proxy_key in ("equal_facility_proxy", "distinct_area_resident_proxy"):
        p_pct = eth_b[proxy_key]["pct"]
        p_moe = eth_b[proxy_key]["moe90_pp"]
        assert round(p_pct["hispanic_latino"] + p_pct["not_hispanic_latino"], 3) == 100.0
        assert p_moe["hispanic_latino"] == p_moe["not_hispanic_latino"]


def test_denominator_b_unrounded_tie_guard_at_one_fifteenth_never_settles() -> None:
    cbsa_lvl = _make_level(
        [
            ("14460", "A", 300.0, {"asian": 20.0, "white": 280.0}, 2.0),
            ("26420", "B", 300.0, {"asian": 90.0, "white": 210.0}, 2.0),
        ]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740302",
            "min_age": "18 Years",
            "max_age": "64 Years",
            "start_date": "2021-01-01",
            "locations": [
                {"facility": "S1", "city": "Boston", "state": "MA", "zip": "02114", "country": "United States"},
                {"facility": "S2", "city": "Houston", "state": "TX", "zip": "77030", "country": "United States"},
            ],
            "race": {
                "reported": True,
                # classified = 600 (40 Asian -> 40/600 = 1/15 == 20/300 at Site 1!), plus 100 unknown
                "omb_totals": {"asian": 40, "white": 560, "unknown_not_reported": 100},
                "raw_categories": [{"original": "Asian", "count": 40}, {"original": "White", "count": 560}],
            },
        },
        {"cbsa": cbsa_lvl},
        resolver,
    )
    b_asian = out["dimensions"]["race"]["benchmarks"]["cbsa"]["of_classified"]["by_category"]["asian"]
    assert b_asian["direction_settled"] is False
    assert b_asian["site_weight_settled"] is False


def test_geography_sensitivity_skips_unavailable_local_levels_and_requires_two_levels() -> None:
    county_lvl = _make_level([("25025", "Suffolk", 1000.0, {"white": 550.0}, 2.0)])
    state_lvl = _make_level([("25", "MA", 1000.0, {"white": 700.0}, 2.0)])
    resolver = SimpleResolver({"02114": {"zcta": "02114", "county": "25025", "cbsa": None}})
    rec = {
        "nct_id": "NCT99740303",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "start_date": "2021-01-01",
        "locations": [{"city": "Boston", "state": "MA", "zip": "02114", "country": "United States"}],
    }
    two_local = ms_benchmark.benchmark_multisite_trial(
        rec, {"county": county_lvl, "state": state_lvl}, resolver
    )
    sens2 = two_local["dimensions"]["race"]["geography_sensitivity"]
    assert sens2["levels_compared"] == ["county", "state"]
    assert sens2["flag"] is True
    assert sens2["max_spread_pp"] == 15.0

    one_local = ms_benchmark.benchmark_multisite_trial(rec, {"state": state_lvl}, resolver)
    sens1 = one_local["dimensions"]["race"]["geography_sensitivity"]
    assert sens1["levels_compared"] == ["state"]
    assert sens1["flag"] is None
    assert sens1["reason"] == "fewer_than_two_local_levels_available"


@pytest.mark.parametrize(
    "iso_country,expected_terr",
    [
        ("PR", "Puerto Rico"),
        ("GU", "Guam"),
        ("VI", "U.S. Virgin Islands"),
        ("AS", "American Samoa"),
        ("MP", "Northern Mariana Islands"),
        ("Commonwealth of Puerto Rico", "Puerto Rico"),
        ("Commonwealth of the Northern Mariana Islands", "Northern Mariana Islands"),
    ],
)
def test_territory_iso2_and_commonwealth_country_aliases_without_zip_classify_as_territory(
    iso_country: str,
    expected_terr: str,
) -> None:
    fac = footprint.normalize_facility({"city": "Capital", "country": iso_country})
    assert fac["jurisdiction"] == "us_territory"
    assert fac["state"] == expected_terr


@pytest.mark.parametrize(
    "fas_loc",
    [
        {"city": "Koror", "country": "Palau", "zip": "96940"},
        {"city": "Kolonia", "country": "Federated States of Micronesia", "zip": "96941"},
        {"city": "Majuro", "country": "Marshall Islands", "zip": "96960"},
    ],
)
def test_freely_associated_states_palau_fsm_marshall_islands_classify_as_non_us(
    fas_loc: dict[str, str],
) -> None:
    fac = footprint.normalize_facility(fas_loc)
    assert fac["jurisdiction"] == "non_us"
    assert fac["zip5"] is None


def test_pool_multisite_cohort_floor_zero_with_one_qualifying_trial_shows_cleanly() -> None:
    cbsa_lvl = _make_level(
        [("14460", "A", 1000.0, {"white": 700.0}, 2.0), ("26420", "B", 1000.0, {"white": 400.0}, 2.0)]
    )
    resolver = SimpleResolver(
        {
            "02114": {"zcta": "02114", "county": "25025", "cbsa": "14460"},
            "77030": {"zcta": "77030", "county": "48201", "cbsa": "26420"},
        }
    )
    t_out = ms_benchmark.benchmark_multisite_trial(
        {
            "nct_id": "NCT99740304",
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
    pooled = ms_benchmark.pool_multisite_cohort([t_out], floor=0, resamples=25, seed=3)
    json.dumps(pooled, allow_nan=False)
    assert pooled["display_rule"] == "show"
    assert pooled["n_trials"] == 1
    assert pooled["concentration_flag"] is True


def test_round2_source_integrity_python311_annotations_and_nct99_compliance() -> None:
    pkg_dir = Path(__file__).resolve().parents[2] / "catchments"
    nct_rx = re.compile(r"NCT\d+")
    valid_rx = re.compile(r"^" + "NCT" + r"99\d{6}$")

    for path in sorted(pkg_dir.iterdir()):
        if path.suffix not in (".py", ".md"):
            continue
        text = path.read_text(encoding="utf-8")
        assert ("/" + "Users/") not in text
        for m in nct_rx.findall(text):
            assert valid_rx.fullmatch(m)
        if path.suffix == ".py":
            tree = ast.parse(text, feature_version=(3, 11))
            for node in _iter_ast_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert node.returns is not None
