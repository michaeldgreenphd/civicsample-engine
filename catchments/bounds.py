"""Assumption-free site-allocation bounds and covariance-correct ACS 90% MOE proxies.

ClinicalTrials.gov lists study facilities without per-site enrollment counts. For any
non-negative per-site participant weights ``w_1, ..., w_M`` summing to 1 across ``M`` US
facilities, the enrollment-weighted reference share ``sum_i w_i e_i`` lies in the convex
hull ``[min_i e_i, max_i e_i]`` whenever all ``M`` US facilities resolve to areas with
valid ACS estimates (``R == M``, ``bounds_unconditional = True``).

When ``0 < R < M`` (for example, a rural site outside every CBSA, a registration missing a
ZIP code, a US territory outside the 50-state+DC ACS frame, or a medical-campus ZCTA with
zero estimated eligible residents or a suppressed ACS sub-table), an unresolved site could
hold anywhere from 0% to 100% of participants. The unconditional envelope therefore widens
to ``[0.0, 100.0]`` with ``bounds_unconditional = False`` while preserving
``resolved_min_pct`` and ``resolved_max_pct`` alongside ``n_unresolved_or_unavailable = M - R``.
Neither ``site_only_settled`` / ``site_weight_settled`` nor ``joint_direction_settled`` can
ever be ``True`` when ``bounds_unconditional`` is ``False``.

Alongside the bounds, two clearly labelled reference proxies are computed:
  1. ``equal_facility_proxy``: weights each resolved US facility with valid ACS data by
     ``1 / R``, aggregating weights by unique ``geoid`` FIRST (``W_g = c_g / R``) because
     multiple clinics in the same ``geoid`` share one ACS estimate with correlation 1.
  2. ``distinct_area_resident_proxy``: sums raw ACS age/sex-weighted numerator and
     denominator resident counts across unique ``geoid``s once per area, propagating 90%
     margins of error through the Census Bureau's sum and proportion/ratio formulas.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from census import acs, benchmark as census_benchmark

COMPARED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "race": tuple(census_benchmark.RACE_TABLE.keys()),
    "ethnicity": ("hispanic_latino", "not_hispanic_latino"),
    "sex": ("female", "male"),
}

# Tolerance guarding IEEE-754 floating-point tie representation (e.g. 40/600 vs 20/300)
# so exact rational equality is never misread as a strict inequality (< or >).
_STRICT_INEQ_EPS: float = 1e-12


def _is_finite_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _area_has_finite_shares(
    est: Mapping[str, Any],
    categories: Sequence[str] | None,
) -> bool:
    pct_map = est.get("pct")
    if not isinstance(pct_map, Mapping) or not pct_map:
        return False
    keys = list(categories) if categories is not None else list(pct_map.keys())
    if not keys:
        return False
    exact_map = est.get("_exact_pct")
    for k in keys:
        if k not in pct_map or not _is_finite_number(pct_map[k]):
            return False
        if isinstance(exact_map, Mapping) and k in exact_map:
            if not _is_finite_number(exact_map[k]):
                return False
    return True


def _infer_categories(
    area_estimates: Mapping[str, Mapping[str, Any]],
    valid_geoids: Sequence[str],
    categories: Sequence[str] | None,
) -> list[str]:
    if categories is not None:
        return list(categories)
    first_pct = area_estimates[valid_geoids[0]].get("pct") or {}
    return [str(k) for k in first_pct.keys()]


def _exact_pct_of_area(est: Mapping[str, Any], cat: str) -> float:
    exact_map = est.get("_exact_pct")
    if isinstance(exact_map, Mapping) and cat in exact_map and _is_finite_number(exact_map[cat]):
        return float(exact_map[cat])
    return float(est["pct"][cat])


def _require_strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    return value


def site_allocation_bounds(
    area_estimates: Mapping[str, Mapping[str, Any]],
    facility_counts_by_geoid: Mapping[str, int],
    *,
    n_us_facilities: int,
    categories: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compute exact convex-hull site-allocation bounds across US facilities.

    Any resolved ``geoid`` whose ACS ``pct`` is missing or non-finite (e.g. a suppressed
    Census sub-table loaded as ``NaN``) is treated as unavailable with reason
    ``'estimate_not_published'``, setting ``bounds_unconditional = False``.
    """
    n_us_checked = _require_strict_int(n_us_facilities, "n_us_facilities")
    positive_counts: dict[str, int] = {}
    for g, c in sorted(facility_counts_by_geoid.items()):
        c_int = _require_strict_int(c, f"facility_counts_by_geoid[{g!r}]")
        if c_int < 0:
            raise ValueError(f"facility_counts_by_geoid[{g!r}] cannot be negative: {c_int}")
        if c_int > 0:
            positive_counts[str(g)] = c_int
    n_geo_resolved = sum(positive_counts.values())
    if n_us_checked < 0 or n_us_checked < n_geo_resolved:
        raise ValueError(
            f"n_us_facilities ({n_us_checked}) cannot be negative or less than "
            f"resolved facility count ({n_geo_resolved})"
        )

    valid_counts: dict[str, int] = {}
    unavailable_geoids: dict[str, str] = {}
    for g, count in positive_counts.items():
        est = area_estimates.get(g)
        if not isinstance(est, Mapping):
            unavailable_geoids[g] = "area_missing_from_estimates"
        elif est.get("available") is not True:
            unavailable_geoids[g] = str(est.get("reason") or "acs_estimate_unavailable")
        elif not _area_has_finite_shares(est, categories):
            unavailable_geoids[g] = "estimate_not_published"
        else:
            valid_counts[g] = count

    r_valid = sum(valid_counts.values())
    n_unresolved_or_unavailable = n_us_facilities - r_valid

    if r_valid == 0:
        if n_us_facilities == 0:
            fail_reason = "no_us_facilities"
        elif n_geo_resolved == 0:
            fail_reason = "no_geographically_resolved_us_facilities"
        elif len(set(unavailable_geoids.values())) == 1:
            fail_reason = next(iter(unavailable_geoids.values()))
        else:
            fail_reason = "no_resolved_areas_with_acs_estimates"
        return {
            "available": False,
            "reason": fail_reason,
            "n_us_facilities": n_us_facilities,
            "n_geo_resolved_facilities": n_geo_resolved,
            "n_resolved_with_acs": 0,
            "n_unresolved_or_unavailable": n_unresolved_or_unavailable,
            "bounds_unconditional": False,
            "unavailable_resolved_geoids": unavailable_geoids,
        }

    bounds_unconditional = bool(n_us_facilities > 0 and r_valid == n_us_facilities)
    valid_geoids = list(valid_counts.keys())
    cats = _infer_categories(area_estimates, valid_geoids, categories)

    by_category: dict[str, dict[str, Any]] = {}
    exact_res_min_map: dict[str, float] = {}
    exact_res_max_map: dict[str, float] = {}
    exact_uncond_min_map: dict[str, float] = {}
    exact_uncond_max_map: dict[str, float] = {}

    for cat in cats:
        min_g = min(
            valid_geoids,
            key=lambda g: (_exact_pct_of_area(area_estimates[g], cat), g),
        )
        max_g = min(
            valid_geoids,
            key=lambda g: (-_exact_pct_of_area(area_estimates[g], cat), g),
        )
        exact_min = _exact_pct_of_area(area_estimates[min_g], cat)
        exact_max = _exact_pct_of_area(area_estimates[max_g], cat)
        res_min = round(float(area_estimates[min_g]["pct"][cat]), 3)
        res_max = round(float(area_estimates[max_g]["pct"][cat]), 3)
        uncond_min = res_min if bounds_unconditional else 0.0
        uncond_max = res_max if bounds_unconditional else 100.0
        exact_res_min_map[cat] = exact_min
        exact_res_max_map[cat] = exact_max
        exact_uncond_min_map[cat] = exact_min if bounds_unconditional else 0.0
        exact_uncond_max_map[cat] = exact_max if bounds_unconditional else 100.0

        by_category[cat] = {
            "resolved_min_pct": res_min,
            "resolved_max_pct": res_max,
            "resolved_span_pp": round(res_max - res_min, 3),
            "min_geoid": min_g,
            "max_geoid": max_g,
            "unconditional_min_pct": uncond_min,
            "unconditional_max_pct": uncond_max,
            "unconditional_span_pp": round(uncond_max - uncond_min, 3),
            "bounds_unconditional": bounds_unconditional,
        }

    return {
        "available": True,
        "n_us_facilities": n_us_facilities,
        "n_geo_resolved_facilities": n_geo_resolved,
        "n_resolved_with_acs": r_valid,
        "n_unresolved_or_unavailable": n_unresolved_or_unavailable,
        "n_distinct_areas": len(valid_counts),
        "bounds_unconditional": bounds_unconditional,
        "unavailable_resolved_geoids": unavailable_geoids,
        "resolved_min_pct": {c: by_category[c]["resolved_min_pct"] for c in cats},
        "resolved_max_pct": {c: by_category[c]["resolved_max_pct"] for c in cats},
        "min_geoid": {c: by_category[c]["min_geoid"] for c in cats},
        "max_geoid": {c: by_category[c]["max_geoid"] for c in cats},
        "unconditional_min_pct": {c: by_category[c]["unconditional_min_pct"] for c in cats},
        "unconditional_max_pct": {c: by_category[c]["unconditional_max_pct"] for c in cats},
        "exact_resolved_min_pct": exact_res_min_map,
        "exact_resolved_max_pct": exact_res_max_map,
        "exact_unconditional_min_pct": exact_uncond_min_map,
        "exact_unconditional_max_pct": exact_uncond_max_map,
        "by_category": by_category,
    }


def equal_facility_proxy(
    area_estimates: Mapping[str, Mapping[str, Any]],
    facility_counts_by_geoid: Mapping[str, int],
    *,
    categories: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compute the equal-facility reference proxy with covariance-deduplicated 90% MOEs.

    Multiple facilities in the same ``geoid`` share the identical ACS estimate
    (correlation = 1). Weights are therefore aggregated by unique ``geoid`` FIRST:
    ``W_g = c_g / R`` where ``R = sum_{g in G} c_g`` and ``sum_{g in G} W_g = 1``.
    """
    valid_counts: dict[str, int] = {}
    for g, c in sorted(facility_counts_by_geoid.items()):
        c_int = _require_strict_int(c, f"facility_counts_by_geoid[{g!r}]")
        if c_int <= 0:
            continue
        est = area_estimates.get(str(g))
        if (
            isinstance(est, Mapping)
            and est.get("available") is True
            and _area_has_finite_shares(est, categories)
        ):
            valid_counts[str(g)] = c_int

    if not valid_counts:
        return {
            "available": False,
            "reason": "no_resolved_areas_with_acs_estimates",
        }

    r_total = sum(valid_counts.values())
    valid_geoids = list(valid_counts.keys())
    cats = _infer_categories(area_estimates, valid_geoids, categories)
    weights = {g: count / r_total for g, count in valid_counts.items()}

    pct: dict[str, float] = {}
    exact_pct: dict[str, float] = {}
    moe90_pp: dict[str, float | None] = {}

    if len(valid_geoids) == 1:
        g0 = valid_geoids[0]
        est0 = area_estimates[g0]
        moe0_map = est0.get("moe90_pp") or {}
        for cat in cats:
            exact_pct[cat] = _exact_pct_of_area(est0, cat)
            pct[cat] = round(float(est0["pct"][cat]), 3)
            raw_m = moe0_map.get(cat)
            moe90_pp[cat] = (
                round(float(raw_m), 3) if _is_finite_number(raw_m) else None
            )
    else:
        is_ethnicity_pair = set(cats) == {"hispanic_latino", "not_hispanic_latino"}
        for cat in cats:
            if (
                is_ethnicity_pair
                and cat == "not_hispanic_latino"
                and "hispanic_latino" in pct
            ):
                pct[cat] = round(100.0 - pct["hispanic_latino"], 3)
                exact_pct[cat] = 100.0 - exact_pct["hispanic_latino"]
            else:
                weighted_pct = sum(
                    weights[g] * float(area_estimates[g]["pct"][cat]) for g in valid_geoids
                )
                weighted_exact = sum(
                    weights[g] * _exact_pct_of_area(area_estimates[g], cat)
                    for g in valid_geoids
                )
                pct[cat] = round(weighted_pct, 3)
                exact_pct[cat] = float(weighted_exact)

            any_missing_moe = False
            sq_sum = 0.0
            for g in valid_geoids:
                moe_map = area_estimates[g].get("moe90_pp") or {}
                m_val = moe_map.get(cat)
                if not _is_finite_number(m_val):
                    any_missing_moe = True
                    break
                sq_sum += (weights[g] * float(m_val)) ** 2
            if any_missing_moe or not math.isfinite(sq_sum):
                moe90_pp[cat] = None
            else:
                moe90_pp[cat] = round(math.sqrt(sq_sum), 3)

        if is_ethnicity_pair and "not_hispanic_latino" in cats and "hispanic_latino" not in pct:
            pct["hispanic_latino"] = round(100.0 - pct["not_hispanic_latino"], 3)
            exact_pct["hispanic_latino"] = 100.0 - exact_pct["not_hispanic_latino"]

    return {
        "available": True,
        "weighting": "equal_per_resolved_facility_covariance_deduplicated_by_geoid",
        "n_facilities": r_total,
        "n_distinct_areas": len(valid_counts),
        "facility_counts_by_geoid": valid_counts,
        "weights_by_geoid": {g: round(w, 6) for g, w in weights.items()},
        "pct": pct,
        "exact_pct": exact_pct,
        "moe90_pp": moe90_pp,
    }


def _area_subtables_finite(
    level: acs.Level,
    area: int,
    dimension: str,
    sexes: tuple[str, ...],
    w: np.ndarray,
) -> bool:
    if dimension == "race":
        for table in census_benchmark.RACE_TABLE.values():
            num_g, _ = census_benchmark._sum(level, area, table, sexes, w)
            if not math.isfinite(num_g):
                return False
        return True
    if dimension == "ethnicity":
        num_g, _ = census_benchmark._sum(level, area, "hispanic_latino", sexes, w)
        return math.isfinite(num_g)
    if dimension == "sex":
        for key in ("female", "male"):
            num_g, _ = census_benchmark._sum(level, area, "total", (key,), w)
            if not math.isfinite(num_g):
                return False
        return True
    raise ValueError(f"unsupported dimension: {dimension!r}")


def distinct_area_resident_proxy(
    level: acs.Level,
    geoids: Sequence[str],
    dimension: str,
    sexes: tuple[str, ...],
    lo: float,
    hi: float | None,
) -> dict[str, Any]:
    """Pool raw ACS age/sex-weighted resident counts across unique non-overlapping ``geoid``s.

    Each distinct ``geoid`` in ``geoids`` is counted once regardless of how many trial
    clinics sit inside it, representing the combined eligible resident population of the
    distinct areas where the trial opened US sites. Reuses ``census.benchmark.band_weights``,
    ``_sum``, and ``_share`` for exact Census Bureau sum and proportion/ratio MOE propagation.
    """
    if dimension not in ("race", "ethnicity", "sex"):
        raise ValueError(f"unsupported dimension: {dimension!r}")
    unique_geoids = sorted({str(g) for g in geoids if g})
    if not unique_geoids:
        return {"available": False, "reason": "no_resolved_geoids"}
    if dimension == "sex" and len(sexes) != 2:
        return {"available": False, "reason": "single_sex_by_eligibility"}

    w = census_benchmark.band_weights(level.edges, lo, hi)
    contributing: list[tuple[str, int, float, float]] = []
    skipped_geoids: dict[str, str] = {}

    for g in unique_geoids:
        area = level.index.get(g)
        if area is None:
            skipped_geoids[g] = "area_not_in_this_window_file"
            continue
        den_g, den_moe_g = census_benchmark._sum(level, area, "total", sexes, w)
        if not (den_g > 0):
            skipped_geoids[g] = (
                "no_eligible_residents_estimated" if den_g == 0 else "estimate_not_published"
            )
            continue
        if not _area_subtables_finite(level, area, dimension, sexes, w):
            skipped_geoids[g] = "estimate_not_published"
            continue
        contributing.append((g, area, den_g, den_moe_g))

    if not contributing:
        reasons = set(skipped_geoids.values())
        reason = next(iter(reasons)) if len(reasons) == 1 else "no_eligible_residents_in_resolved_areas"
        return {"available": False, "reason": reason}

    if len(contributing) == 1:
        den, den_moe = contributing[0][2], contributing[0][3]
    else:
        den = float(sum(item[2] for item in contributing))
        den_moe = float(np.sqrt(sum(item[3] ** 2 for item in contributing)))

    def _pool_table(table_name: str, table_sexes: tuple[str, ...]) -> tuple[float, float]:
        if len(contributing) == 1:
            return census_benchmark._sum(level, contributing[0][1], table_name, table_sexes, w)
        parts = [
            census_benchmark._sum(level, item[1], table_name, table_sexes, w)
            for item in contributing
        ]
        num_total = float(sum(p[0] for p in parts))
        num_moe_total = float(np.sqrt(sum(p[1] ** 2 for p in parts)))
        return num_total, num_moe_total

    pct: dict[str, float] = {}
    exact_pct: dict[str, float] = {}
    moe90_pp: dict[str, float | None] = {}

    def _put(key: str, num: float, num_moe: float) -> None:
        p, m = census_benchmark._share(num, num_moe, den, den_moe)
        exact_pct[key] = 100.0 * p
        pct[key] = round(100 * p, 3)
        moe90_pp[key] = round(100 * m, 3) if _is_finite_number(m) else None  # type: ignore[arg-type]

    if dimension == "race":
        for key, table in census_benchmark.RACE_TABLE.items():
            _put(key, *_pool_table(table, sexes))
    elif dimension == "ethnicity":
        _put("hispanic_latino", *_pool_table("hispanic_latino", sexes))
        exact_pct["not_hispanic_latino"] = 100.0 - exact_pct["hispanic_latino"]
        pct["not_hispanic_latino"] = round(100 - pct["hispanic_latino"], 3)
        moe90_pp["not_hispanic_latino"] = moe90_pp["hispanic_latino"]
    else:
        for key in ("female", "male"):
            _put(key, *_pool_table("total", (key,)))

    out: dict[str, Any] = {
        "available": True,
        "weighting": "pooled_eligible_residents_across_distinct_geoids",
        "eligible_residents": round(den),
        "n_distinct_areas": len(contributing),
        "geoids": [item[0] for item in contributing],
        "pct": pct,
        "exact_pct": exact_pct,
        "moe90_pp": moe90_pp,
    }
    if skipped_geoids:
        out["skipped_geoids"] = skipped_geoids
    return out


def _compare_with_proxy(
    observed_pct: Mapping[str, float],
    unknown_pct: float,
    proxy: Mapping[str, Any],
    *,
    exact_observed_floor_pct: Mapping[str, float] | None = None,
    exact_observed_ceiling_pct: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Compare Denominator A observed shares (plus unknown bounds) against one proxy."""
    base = census_benchmark.compare(dict(observed_pct), dict(proxy))
    unk = max(0.0, float(unknown_pct))
    exact_proxy_map = proxy.get("exact_pct") or proxy["pct"]
    unknown_bounds: dict[str, dict[str, Any]] = {}
    for key, exp_val in proxy["pct"].items():
        raw_floor = (
            float(exact_observed_floor_pct[key])
            if exact_observed_floor_pct is not None and key in exact_observed_floor_pct
            else float(observed_pct[key])
        )
        raw_ceil = (
            float(exact_observed_ceiling_pct[key])
            if exact_observed_ceiling_pct is not None and key in exact_observed_ceiling_pct
            else min(100.0, raw_floor + unk)
        )
        raw_ceil = min(100.0, max(raw_floor, raw_ceil))
        exact_exp = (
            float(exact_proxy_map[key])
            if isinstance(exact_proxy_map, Mapping) and key in exact_proxy_map
            else float(exp_val)
        )
        diff_floor = round(raw_floor - exact_exp, 3)
        diff_ceil = round(raw_ceil - exact_exp, 3)
        settled = bool(
            (raw_floor > exact_exp + _STRICT_INEQ_EPS)
            or (raw_ceil < exact_exp - _STRICT_INEQ_EPS)
        )
        unknown_bounds[key] = {
            "diff_pp_if_no_unknown_is_in_category": diff_floor,
            "diff_pp_if_every_unknown_is_in_category": diff_ceil,
            "direction_settled": settled,
        }
    return {**base, "unknown_bounds": unknown_bounds}


def compare_with_catchment_bounds(
    observed_pct: Mapping[str, float],
    unknown_pct: float,
    bounds: Mapping[str, Any],
    *,
    equal_proxy: Mapping[str, Any] | None = None,
    resident_proxy: Mapping[str, Any] | None = None,
    observed_pct_of_classified: Mapping[str, float] | None = None,
    classified_share_reason: str | None = None,
    exact_observed_floor_pct: Mapping[str, float] | None = None,
    exact_observed_ceiling_pct: Mapping[str, float] | None = None,
    exact_observed_classified_pct: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Compare trial Denominator A and Denominator B shares against site-allocation bounds
    and optional proxy views.

    Evaluates strict directional settlement (``<`` / ``>``) on unrounded exact shares
    before display rounding so sub-0.001 pp rounding never fabricates a settled direction,
    and enforces ``site_only_settled == site_weight_settled == joint_direction_settled == False``
    whenever ``bounds['bounds_unconditional']`` is ``False``.
    """
    if not bounds.get("available"):
        raise ValueError("compare_with_catchment_bounds requires an available bounds dict")

    bounds_unconditional = bool(bounds.get("bounds_unconditional", False))
    raw_unk = max(0.0, float(unknown_pct))
    cats = list(bounds["resolved_min_pct"].keys())
    exact_res_min_map = bounds.get("exact_resolved_min_pct") or bounds["resolved_min_pct"]
    exact_res_max_map = bounds.get("exact_resolved_max_pct") or bounds["resolved_max_pct"]
    exact_min_map = bounds.get("exact_unconditional_min_pct") or bounds["unconditional_min_pct"]
    exact_max_map = bounds.get("exact_unconditional_max_pct") or bounds["unconditional_max_pct"]

    by_category: dict[str, dict[str, Any]] = {}
    for cat in cats:
        raw_floor = (
            float(exact_observed_floor_pct[cat])
            if exact_observed_floor_pct is not None and cat in exact_observed_floor_pct
            else float(observed_pct[cat])
        )
        raw_ceil = (
            float(exact_observed_ceiling_pct[cat])
            if exact_observed_ceiling_pct is not None and cat in exact_observed_ceiling_pct
            else min(100.0, raw_floor + raw_unk)
        )
        raw_ceil = min(100.0, max(raw_floor, raw_ceil))

        obs_floor = round(raw_floor, 3)
        obs_ceil = round(raw_ceil, 3)
        res_min = float(bounds["resolved_min_pct"][cat])
        res_max = float(bounds["resolved_max_pct"][cat])
        exact_res_min = float(exact_res_min_map[cat])
        exact_res_max = float(exact_res_max_map[cat])
        uncond_min = float(bounds["unconditional_min_pct"][cat]) if bounds_unconditional else 0.0
        uncond_max = float(bounds["unconditional_max_pct"][cat]) if bounds_unconditional else 100.0
        exact_uncond_min = float(exact_min_map[cat]) if bounds_unconditional else 0.0
        exact_uncond_max = float(exact_max_map[cat]) if bounds_unconditional else 100.0

        site_settled = bool(
            bounds_unconditional
            and (
                raw_floor < exact_uncond_min - _STRICT_INEQ_EPS
                or raw_floor > exact_uncond_max + _STRICT_INEQ_EPS
            )
        )
        joint_diff_min = round(raw_floor - exact_uncond_max, 3)
        joint_diff_max = round(raw_ceil - exact_uncond_min, 3)
        joint_settled = bool(
            bounds_unconditional
            and (
                (raw_ceil < exact_uncond_min - _STRICT_INEQ_EPS)
                or (raw_floor > exact_uncond_max + _STRICT_INEQ_EPS)
            )
        )
        if not joint_settled:
            joint_dir = "unsettled"
        elif raw_ceil < exact_uncond_min - _STRICT_INEQ_EPS:
            joint_dir = "negative"
        else:
            joint_dir = "positive"

        by_category[cat] = {
            "obs_floor_pct": obs_floor,
            "unknown_pct": round(raw_unk, 3),
            "obs_ceiling_pct": obs_ceil,
            "resolved_min_pct": res_min,
            "resolved_max_pct": res_max,
            "unconditional_min_pct": uncond_min,
            "unconditional_max_pct": uncond_max,
            "bounds_unconditional": bounds_unconditional,
            "site_only_settled": site_settled,
            "site_weight_settled": site_settled,
            "joint_diff_pp_min": joint_diff_min,
            "joint_diff_pp_max": joint_diff_max,
            "joint_direction_settled": joint_settled,
            "joint_direction": joint_dir,
            "resolved_diff_pp_min": round(raw_floor - exact_res_max, 3),
            "resolved_diff_pp_max": round(raw_ceil - exact_res_min, 3),
        }

    out: dict[str, Any] = {
        "bounds_unconditional": bounds_unconditional,
        "unknown_pct": round(raw_unk, 3),
        "by_category": by_category,
        "site_only_settled": {c: by_category[c]["site_only_settled"] for c in cats},
        "site_weight_settled": {c: by_category[c]["site_weight_settled"] for c in cats},
        "joint_diff_pp_min": {c: by_category[c]["joint_diff_pp_min"] for c in cats},
        "joint_diff_pp_max": {c: by_category[c]["joint_diff_pp_max"] for c in cats},
        "joint_direction_settled": {c: by_category[c]["joint_direction_settled"] for c in cats},
        "joint_direction": {c: by_category[c]["joint_direction"] for c in cats},
    }

    if equal_proxy is not None and equal_proxy.get("available"):
        out["equal_facility_proxy"] = _compare_with_proxy(
            observed_pct,
            raw_unk,
            equal_proxy,
            exact_observed_floor_pct=exact_observed_floor_pct,
            exact_observed_ceiling_pct=exact_observed_ceiling_pct,
        )
    if resident_proxy is not None and resident_proxy.get("available"):
        out["distinct_area_resident_proxy"] = _compare_with_proxy(
            observed_pct,
            raw_unk,
            resident_proxy,
            exact_observed_floor_pct=exact_observed_floor_pct,
            exact_observed_ceiling_pct=exact_observed_ceiling_pct,
        )

    # Denominator B (of_classified): reported beside A where structurally valid, omitted
    # with an explicit reason when an ethnicity table reports one category plus unknowns.
    if observed_pct_of_classified is not None and classified_share_reason is None:
        b_cats: dict[str, dict[str, Any]] = {}
        for cat in cats:
            raw_obs_b = (
                float(exact_observed_classified_pct[cat])
                if exact_observed_classified_pct is not None and cat in exact_observed_classified_pct
                else float(observed_pct_of_classified[cat])
            )
            obs_b = round(raw_obs_b, 3)
            exact_res_min = float(exact_res_min_map[cat])
            exact_res_max = float(exact_res_max_map[cat])
            exact_uncond_min = float(exact_min_map[cat]) if bounds_unconditional else 0.0
            exact_uncond_max = float(exact_max_map[cat]) if bounds_unconditional else 100.0
            b_settled = bool(
                bounds_unconditional
                and (
                    raw_obs_b < exact_uncond_min - _STRICT_INEQ_EPS
                    or raw_obs_b > exact_uncond_max + _STRICT_INEQ_EPS
                )
            )
            b_cats[cat] = {
                "observed_pct": obs_b,
                "diff_pp_min": round(raw_obs_b - exact_uncond_max, 3),
                "diff_pp_max": round(raw_obs_b - exact_uncond_min, 3),
                "resolved_diff_pp_min": round(raw_obs_b - exact_res_max, 3),
                "resolved_diff_pp_max": round(raw_obs_b - exact_res_min, 3),
                "site_weight_settled": b_settled,
                "direction_settled": b_settled,
            }
        b_block: dict[str, Any] = {"by_category": b_cats}
        if equal_proxy is not None and equal_proxy.get("available"):
            b_block["equal_facility_proxy"] = census_benchmark.compare(
                dict(observed_pct_of_classified), dict(equal_proxy)
            )
        if resident_proxy is not None and resident_proxy.get("available"):
            b_block["distinct_area_resident_proxy"] = census_benchmark.compare(
                dict(observed_pct_of_classified), dict(resident_proxy)
            )
        out["of_classified"] = b_block
    else:
        out["of_classified"] = None
        if classified_share_reason is not None:
            out["classified_share_reason"] = classified_share_reason

    return out
