"""End-to-end multi-site trial and cohort catchment benchmark engine.

Integrates ``catchments.footprint`` and ``catchments.bounds`` with ``census.acs``,
``census.benchmark``, and ``census.trials``:
  * ``benchmark_multisite_trial`` evaluates one trial across geography levels
    ``('zcta', 'county', 'cbsa', 'state', 'region', 'us')`` (respecting
    ``acs.LEVELS_BY_VINTAGE`` where 2005-2009 ZCTA tables do not exist) and dimensions
    ``('race', 'ethnicity', 'sex')``, handling unreadable age bounds, missing dates,
    zero-facility or foreign-only footprints, single-sex eligibility, and combined
    race/ethnicity baseline tables without crashing.
  * On a ``single_us_facility`` trial, ``resolved_min_pct``, ``resolved_max_pct``,
    ``equal_facility_proxy['pct']``, and ``distinct_area_resident_proxy['pct']`` collapse
    identically to ``census.benchmark.expected(...)['pct']``.
  * ``pool_multisite_cohort`` summarizes ``multi_site_us_only`` trials deterministically
    with equal-trial and participant-weighted comparisons against both proxies plus
    joint-settlement shares (negative vs positive vs unsettled), while keeping
    ``mixed_us_and_international`` trials strictly isolated in a contextual diagnostic
    block because ClinicalTrials.gov baseline tables pool US and non-US participants.
"""
from __future__ import annotations

import collections
import math
from typing import Any, Collection, Mapping, Sequence

import numpy as np

from census import acs, benchmark as census_benchmark, trials

from . import METHOD_VERSION, SCHEMA_VERSION, bounds, footprint

LEVEL_ORDER: tuple[str, ...] = footprint.LEVEL_ORDER
DIMENSIONS: tuple[str, ...] = ("race", "ethnicity", "sex")
SENSITIVITY_LEVELS: tuple[str, ...] = ("zcta", "county", "cbsa", "state")
GEOGRAPHY_SPREAD_FLAG_PP: float = 10.0

GLOBAL_POOLED_WARNING: str = (
    "ClinicalTrials.gov baseline tables pool US and international participants without "
    "country-stratified counts; US facility areas are not a valid standalone catchment "
    "for mixed US/international studies and are excluded from primary US-only rollups."
)


def _sanitize_enrollment(value: Any) -> int | float | None:
    """Reject booleans, negative values, and NaN/Inf so JSON output stays strict."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return int(value) if value.is_integer() else value
    return None


def _normalize_trial_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize metadata across extracted release records and raw API v2 records."""
    protocol = record.get("protocolSection")
    p_map: Mapping[str, Any] = protocol if isinstance(protocol, Mapping) else {}
    ident = p_map.get("identificationModule")
    ident_map: Mapping[str, Any] = ident if isinstance(ident, Mapping) else {}
    design = p_map.get("designModule")
    design_map: Mapping[str, Any] = design if isinstance(design, Mapping) else {}
    enrol_info = design_map.get("enrollmentInfo")
    enrol_map: Mapping[str, Any] = enrol_info if isinstance(enrol_info, Mapping) else {}
    elig_mod = p_map.get("eligibilityModule")
    elig_mod_map: Mapping[str, Any] = elig_mod if isinstance(elig_mod, Mapping) else {}
    status_mod = p_map.get("statusModule")
    status_map: Mapping[str, Any] = status_mod if isinstance(status_mod, Mapping) else {}

    def _status_date(struct_key: str) -> str | None:
        struct = status_map.get(struct_key)
        if isinstance(struct, Mapping):
            val = struct.get("date")
            return str(val) if val is not None else None
        return None

    nct_id = record.get("nct_id") or ident_map.get("nctId") or ""
    study_type = record.get("study_type") or design_map.get("studyType")
    raw_enrollment = (
        record.get("enrollment")
        if "enrollment" in record
        else enrol_map.get("count")
    )
    enrollment = _sanitize_enrollment(raw_enrollment)
    enrollment_type = record.get("enrollment_type") or enrol_map.get("type")
    min_age = (
        record.get("min_age")
        if "min_age" in record
        else elig_mod_map.get("minimumAge")
    )
    max_age = (
        record.get("max_age")
        if "max_age" in record
        else elig_mod_map.get("maximumAge")
    )

    raw_elig = record.get("eligibility")
    if isinstance(raw_elig, Mapping):
        eligibility_dict: dict[str, Any] = dict(raw_elig)
    else:
        eligibility_dict = {"sex": elig_mod_map.get("sex")}

    healthy_volunteers = (
        record.get("healthy_volunteers")
        if "healthy_volunteers" in record
        else elig_mod_map.get("healthyVolunteers", False)
    )

    start_date = (
        record.get("start_date")
        if "start_date" in record
        else _status_date("startDateStruct")
    )
    primary_completion_date = (
        record.get("primary_completion_date")
        if "primary_completion_date" in record
        else _status_date("primaryCompletionDateStruct")
    )
    completion_date = (
        record.get("completion_date")
        if "completion_date" in record
        else _status_date("completionDateStruct")
    )

    return {
        "nct_id": str(nct_id),
        "study_type": study_type,
        "enrollment": enrollment,
        "enrollment_type": enrollment_type,
        "min_age": min_age,
        "max_age": max_age,
        "eligibility": eligibility_dict,
        "healthy_volunteers": healthy_volunteers,
        "start_date": start_date,
        "primary_completion_date": primary_completion_date,
        "completion_date": completion_date,
        "race": record.get("race"),
        "ethnicity": record.get("ethnicity"),
        "sex": record.get("sex"),
    }


def _select_levels_for_vintage(
    levels_by_name: Mapping[str, acs.Level] | Mapping[int, Mapping[str, acs.Level]] | None,
    vintage: int | None,
) -> Mapping[str, acs.Level]:
    if levels_by_name is None or vintage is None:
        return {}
    if not levels_by_name:
        return {}
    first_key = next(iter(levels_by_name.keys()))
    if isinstance(first_key, int):
        vintage_map = levels_by_name.get(vintage)  # type: ignore[arg-type]
        return vintage_map if isinstance(vintage_map, Mapping) else {}
    return levels_by_name  # type: ignore[return-value]


def _select_resolver_for_vintage(
    resolver: Any,
    vintage: int | None,
) -> Any:
    if resolver is None or vintage is None:
        return None
    if isinstance(resolver, Mapping):
        return resolver.get(vintage)
    return resolver


def _exact_expected_shares(
    level: acs.Level,
    geoid: str,
    dimension: str,
    sexes: tuple[str, ...],
    lo: float,
    hi: float | None,
) -> dict[str, float] | None:
    """Compute unrounded exact ACS expected percentage shares for strict inequality checks."""
    area = level.index.get(geoid)
    if area is None:
        return None
    if dimension == "sex" and len(sexes) != 2:
        return None
    w = census_benchmark.band_weights(level.edges, lo, hi)
    den, _ = census_benchmark._sum(level, area, "total", sexes, w)
    if not (den > 0) or not math.isfinite(den):
        return None

    exact: dict[str, float] = {}
    if dimension == "race":
        for key, table in census_benchmark.RACE_TABLE.items():
            num, _ = census_benchmark._sum(level, area, table, sexes, w)
            if not math.isfinite(num):
                return None
            exact[key] = 100.0 * (num / den)
    elif dimension == "ethnicity":
        num, _ = census_benchmark._sum(level, area, "hispanic_latino", sexes, w)
        if not math.isfinite(num):
            return None
        hisp = 100.0 * (num / den)
        exact["hispanic_latino"] = hisp
        exact["not_hispanic_latino"] = 100.0 - hisp
    elif dimension == "sex":
        for key in ("female", "male"):
            num, _ = census_benchmark._sum(level, area, "total", (key,), w)
            if not math.isfinite(num):
                return None
            exact[key] = 100.0 * (num / den)
    return exact


def _compute_geography_sensitivity(
    benchmarks_by_level: Mapping[str, Mapping[str, Any]],
    categories: Sequence[str],
) -> dict[str, Any]:
    avail_proxy: dict[str, Mapping[str, float]] = {}
    for lvl in SENSITIVITY_LEVELS:
        b = benchmarks_by_level.get(lvl)
        if isinstance(b, Mapping) and b.get("available") is True:
            eq = b.get("equal_facility_proxy")
            if isinstance(eq, Mapping) and eq.get("available") is True:
                pct_map = eq.get("pct") or {}
                if all(
                    cat in pct_map and math.isfinite(float(pct_map[cat]))
                    for cat in categories
                ):
                    avail_proxy[lvl] = pct_map

    if len(avail_proxy) < 2:
        return {
            "levels_compared": list(avail_proxy.keys()),
            "flag": None,
            "reason": "fewer_than_two_local_levels_available",
        }

    spread = {
        cat: max(float(v[cat]) for v in avail_proxy.values())
        - min(float(v[cat]) for v in avail_proxy.values())
        for cat in categories
    }
    worst = max(spread, key=lambda k: spread[k])
    return {
        "levels_compared": list(avail_proxy.keys()),
        "max_spread_pp": round(spread[worst], 2),
        "category": worst,
        "flag": bool(spread[worst] >= GEOGRAPHY_SPREAD_FLAG_PP),
    }


def benchmark_multisite_trial(
    record: Mapping[str, Any],
    levels_by_name: Mapping[str, acs.Level] | Mapping[int, Mapping[str, acs.Level]] | None,
    resolver: Any,
    *,
    snapshot_date: str | None = None,
    registry_ethnicity_rows: list[dict[str, Any]] | None = None,
    referral_zips: Collection[str] | None = None,
    deduplicate: bool = True,
    collapse_unnamed: bool = False,
    min_participants_in_table: int = 1,
    size_cap_participants: int | None = None,
) -> dict[str, Any]:
    """Compute a complete multi-site catchment benchmark record for one trial.

    Evaluates all 6 geography levels ``('zcta', 'county', 'cbsa', 'state', 'region', 'us')``
    (enforcing ``acs.LEVELS_BY_VINTAGE`` where ``2009`` publishes no ZCTA tables) and all
    3 demographic dimensions ``('race', 'ethnicity', 'sex')``. Never crashes on unreadable
    age strings, inverted min/max ages, or missing dates.
    """
    norm = _normalize_trial_record(record)
    fp = footprint.classify_footprint(
        record, deduplicate=deduplicate, collapse_unnamed=collapse_unnamed
    )
    referral_set = set(referral_zips) if referral_zips is not None else set()
    any_referral_zip = any(
        bool(fac.get("zip5") and fac["zip5"] in referral_set)
        for fac in fp["facilities"]
    )

    period = trials.enrolment_period(norm)
    _, exact_midpoint = trials.enrolment_midpoint(norm)
    bench_period = trials.assign_window(exact_midpoint)
    vintage: int | None = bench_period["vintage"]

    active_levels = _select_levels_for_vintage(levels_by_name, vintage)
    active_resolver = _select_resolver_for_vintage(resolver, vintage)

    published_levels = (
        set(acs.LEVELS_BY_VINTAGE.get(vintage, LEVEL_ORDER))
        if vintage is not None
        else set()
    )

    if vintage is None:
        resolution = footprint.resolve_us_facilities(
            fp["facilities"],
            None,
            levels=LEVEL_ORDER,
            no_resolver_reason="no_benchmark_window",
        )
    else:
        unpub_reasons = {
            lvl: (
                "zcta_tables_not_published_for_this_window"
                if lvl == "zcta"
                else "level_not_published_for_this_window"
            )
            for lvl in LEVEL_ORDER
            if lvl not in published_levels
        }
        resolution = footprint.resolve_us_facilities(
            fp["facilities"],
            active_resolver,
            levels=LEVEL_ORDER,
            no_resolver_reason="no_resolver_provided",
            unpublished_levels=unpub_reasons,
        )

    elig_info: dict[str, Any]
    ref_pop: dict[str, Any] | None = None
    try:
        lo, hi = trials.eligible_ages(norm)
        sexes = trials.eligible_sexes(norm)
        elig_info = {
            "min_age": norm.get("min_age"),
            "max_age": norm.get("max_age"),
            "age_from": lo,
            "age_to_exclusive": hi,
            "sex": (norm.get("eligibility") or {}).get("sex"),
            "sexes": list(sexes),
            "healthy_volunteers": bool(norm.get("healthy_volunteers")),
        }
        caveats = trials.reference_caveats(norm, lo, hi, sexes, any_referral_zip)
        if fp["n_us_facilities"] > 1:
            caveats.append("multi_site_per_facility_enrollment_weights_unobserved")
        if fp["global_pooled_baseline_table"]:
            caveats.append("mixed_us_and_international_sites_pooled_baseline_table")
        if fp["n_us_territory_facilities"] > 0:
            caveats.append("includes_us_territory_facilities_outside_50_state_dc_acs")
        ref_pop = {
            "label": "multi-site catchment population benchmark",
            "fit": "contextual" if caveats else "general",
            "caveats": caveats,
        }
    except ValueError as exc:
        elig_info = {
            "min_age": norm.get("min_age"),
            "max_age": norm.get("max_age"),
            "error": str(exc),
        }

    dimensions_out: dict[str, dict[str, Any]] = {}
    for dim in DIMENSIONS:
        cats = bounds.COMPARED_CATEGORIES[dim]
        obs = trials.observed(
            norm,
            dim,
            registry_ethnicity_rows if dim == "race" else None,
        )

        dim_entry: dict[str, Any]
        observed_pct: dict[str, float] | None = None
        exact_floor_pct: dict[str, float] | None = None
        exact_ceil_pct: dict[str, float] | None = None
        observed_pct_classified: dict[str, float] | None = None
        exact_classified_pct: dict[str, float] | None = None
        classified_reason: str | None = None
        non_classified_pct: float = 0.0
        usable_for_shares = False
        n_in_table = 0

        if not obs["reported"]:
            dim_entry = {
                "state": "not_reported",
                "reason": obs["reason"],
                "pooled": False,
                "not_pooled_reason": obs["reason"],
            }
        else:
            n_in_table = int(obs["n_in_table"])
            structure = obs["structure"]
            usable_for_shares = bool(structure["usable_for_shares"])
            known_n = int(obs["n_classified"])
            n_non_classified = n_in_table - known_n

            exact_floor_pct = {
                k: 100.0 * float(v) / n_in_table
                for k, v in obs["counts"].items()
            }
            exact_ceil_pct = {
                k: min(100.0, 100.0 * float(v + n_non_classified) / n_in_table)
                for k, v in obs["counts"].items()
            }
            observed_pct = {
                k: round(val, 3) for k, val in exact_floor_pct.items()
            }
            unknown_only_pct = round(100.0 * int(obs["n_unknown"]) / n_in_table, 3)
            non_classified_pct = 100.0 * float(n_non_classified) / n_in_table

            if not usable_for_shares:
                observed_pct_classified = None
                classified_reason = "table_rows_not_one_classification"
            elif structure.get("classified_share_usable") is False:
                observed_pct_classified = None
                classified_reason = str(
                    structure.get("classified_share_reason")
                    or "table_reports_one_ethnicity_category_only"
                )
            elif known_n <= 0:
                observed_pct_classified = None
                classified_reason = "no_classified_participants"
            else:
                exact_classified_pct = {
                    k: 100.0 * float(v) / known_n
                    for k, v in obs["counts"].items()
                    if not k.startswith("unknown")
                }
                observed_pct_classified = {
                    k: round(val, 3) for k, val in exact_classified_pct.items()
                }
                classified_reason = None

            enrollment_val = norm.get("enrollment")
            ratio_enrol = (
                round(n_in_table / float(enrollment_val), 3)
                if enrollment_val is not None and float(enrollment_val) > 0
                else None
            )

            dim_entry = {
                "state": "reported",
                "population_basis": trials.POPULATION_BASIS,
                "n_in_table": n_in_table,
                "table_to_enrollment_ratio": ratio_enrol,
                "observed_counts": obs["counts"],
                "original_categories": obs["original_categories"],
                "structure": structure,
                "observed_pct": observed_pct,
                "observed_pct_of_classified": observed_pct_classified,
                **(
                    {"classified_share_reason": classified_reason}
                    if classified_reason is not None
                    else {}
                ),
                "denominators": {
                    "all_in_table": n_in_table,
                    "classified": known_n,
                    "unknown_or_not_reported": int(obs["n_unknown"]),
                    "unmapped_category": int(obs["n_unmapped"]),
                    "unknown_pct": unknown_only_pct,
                    "unknown_or_unmapped_pct": round(non_classified_pct, 3),
                },
            }
            if "unharmonised_counts" in obs:
                dim_entry["unharmonised_counts"] = obs["unharmonised_counts"]

            not_pooled: str | None = None
            if fp["footprint_tier"] == "mixed_us_and_international":
                not_pooled = "mixed_us_and_international_pooled_baseline"
            elif fp["footprint_tier"] not in ("multi_site_us_only", "single_us_facility"):
                not_pooled = f"footprint_tier_{fp['footprint_tier']}"
            elif "error" in elig_info:
                not_pooled = "eligible_ages_unreadable"
            elif vintage is None:
                not_pooled = "no_benchmark_window"
            elif not usable_for_shares:
                not_pooled = "table_rows_not_one_classification"
            elif dim == "sex" and len(elig_info["sexes"]) != 2:
                not_pooled = "single_sex_by_eligibility"
            elif size_cap_participants is not None and n_in_table > size_cap_participants:
                not_pooled = "over_size_cap"
            elif n_in_table < min_participants_in_table:
                not_pooled = "under_min_participants"

            dim_entry["pooled"] = not_pooled is None
            if not_pooled is not None:
                dim_entry["not_pooled_reason"] = not_pooled

        # Evaluate benchmarks across all 6 geography levels
        benchmarks_by_lvl: dict[str, dict[str, Any]] = {}
        global_block: str | None = None
        if "error" in elig_info:
            global_block = "eligible_ages_unreadable"
        elif vintage is None:
            global_block = "no_benchmark_window"
        elif fp["n_facilities"] == 0:
            global_block = "no_facilities_listed"
        elif fp["n_us_facilities"] == 0:
            global_block = "non_us_only_no_us_catchment"
        elif dim == "sex" and len(elig_info["sexes"]) != 2:
            global_block = "single_sex_by_eligibility"

        if global_block is not None:
            for lvl in LEVEL_ORDER:
                benchmarks_by_lvl[lvl] = {
                    "available": False,
                    "level": lvl,
                    "reason": global_block,
                }
            dim_entry["benchmarks"] = benchmarks_by_lvl
            dim_entry["geography_sensitivity"] = {
                "levels_compared": [],
                "flag": None,
                "reason": global_block,
            }
            dimensions_out[dim] = dim_entry
            continue

        sexes_tuple = tuple(elig_info["sexes"])
        age_lo = float(elig_info["age_from"])
        age_hi: float | None = elig_info["age_to_exclusive"]
        m_us = int(fp["n_us_facilities"])

        for lvl in LEVEL_ORDER:
            if lvl not in published_levels:
                unpub = (
                    "zcta_tables_not_published_for_this_window"
                    if lvl == "zcta"
                    else "level_not_published_for_this_window"
                )
                benchmarks_by_lvl[lvl] = {
                    "available": False,
                    "level": lvl,
                    "reason": unpub,
                }
                continue

            acs_level = active_levels.get(lvl)
            if acs_level is None:
                benchmarks_by_lvl[lvl] = {
                    "available": False,
                    "level": lvl,
                    "reason": "level_not_loaded",
                }
                continue

            lvl_res = resolution["by_level"][lvl]
            fac_counts: dict[str, int] = lvl_res["facility_counts_by_geoid"]
            if lvl_res["n_resolved_facilities"] == 0:
                reasons = lvl_res["unresolved_reasons"]
                only_reason = (
                    next(iter(reasons.keys()))
                    if len(reasons) == 1
                    else "no_us_facilities_resolved_at_level"
                )
                benchmarks_by_lvl[lvl] = {
                    "available": False,
                    "level": lvl,
                    "reason": only_reason,
                    "n_us_facilities": m_us,
                    "n_resolved_facilities": 0,
                    "n_unresolved_or_unavailable": m_us,
                    "bounds_unconditional": False,
                }
                continue

            area_estimates: dict[str, dict[str, Any]] = {}
            for geoid in fac_counts:
                exp_dict = census_benchmark.expected(
                    acs_level, geoid, dim, sexes_tuple, age_lo, age_hi
                )
                if exp_dict.get("available") is True:
                    exact_shares = _exact_expected_shares(
                        acs_level, geoid, dim, sexes_tuple, age_lo, age_hi
                    )
                    if exact_shares is None:
                        exp_dict = {
                            "available": False,
                            "reason": "estimate_not_published",
                        }
                    else:
                        exp_dict = {**exp_dict, "_exact_pct": exact_shares}
                area_estimates[geoid] = exp_dict

            alloc_bounds = bounds.site_allocation_bounds(
                area_estimates,
                fac_counts,
                n_us_facilities=m_us,
                categories=cats,
            )
            if not alloc_bounds["available"]:
                benchmarks_by_lvl[lvl] = {
                    "available": False,
                    "level": lvl,
                    "reason": alloc_bounds["reason"],
                    "n_us_facilities": m_us,
                    "n_resolved_facilities": lvl_res["n_resolved_facilities"],
                    "n_acs_available_facilities": 0,
                    "n_unresolved_or_unavailable": m_us,
                    "bounds_unconditional": False,
                    "unavailable_resolved_geoids": alloc_bounds.get(
                        "unavailable_resolved_geoids", {}
                    ),
                }
                continue

            eq_proxy = bounds.equal_facility_proxy(
                area_estimates, fac_counts, categories=cats
            )
            res_proxy = bounds.distinct_area_resident_proxy(
                acs_level, list(fac_counts.keys()), dim, sexes_tuple, age_lo, age_hi
            )

            lvl_payload: dict[str, Any] = {
                "available": True,
                "level": lvl,
                "n_us_facilities": m_us,
                "n_resolved_facilities": lvl_res["n_resolved_facilities"],
                "n_acs_available_facilities": alloc_bounds["n_resolved_with_acs"],
                "n_unresolved_or_unavailable": alloc_bounds["n_unresolved_or_unavailable"],
                "n_distinct_areas": alloc_bounds["n_distinct_areas"],
                "facility_hhi": lvl_res["facility_hhi"],
                "bounds_unconditional": alloc_bounds["bounds_unconditional"],
                "resolved_min_pct": alloc_bounds["resolved_min_pct"],
                "resolved_max_pct": alloc_bounds["resolved_max_pct"],
                "min_geoid": alloc_bounds["min_geoid"],
                "max_geoid": alloc_bounds["max_geoid"],
                "unconditional_min_pct": alloc_bounds["unconditional_min_pct"],
                "unconditional_max_pct": alloc_bounds["unconditional_max_pct"],
                "site_allocation_bounds": alloc_bounds,
                "equal_facility_proxy": eq_proxy,
                "distinct_area_resident_proxy": res_proxy,
            }

            if observed_pct is not None and usable_for_shares:
                comp = bounds.compare_with_catchment_bounds(
                    observed_pct,
                    non_classified_pct,
                    alloc_bounds,
                    equal_proxy=eq_proxy,
                    resident_proxy=res_proxy,
                    observed_pct_of_classified=observed_pct_classified,
                    classified_share_reason=classified_reason,
                    exact_observed_floor_pct=exact_floor_pct,
                    exact_observed_ceiling_pct=exact_ceil_pct,
                    exact_observed_classified_pct=exact_classified_pct,
                )
                lvl_payload["comparison_available"] = True
                lvl_payload["comparison"] = comp
                lvl_payload["site_only_settled"] = comp["site_only_settled"]
                lvl_payload["site_weight_settled"] = comp["site_weight_settled"]
                lvl_payload["joint_diff_pp_min"] = comp["joint_diff_pp_min"]
                lvl_payload["joint_diff_pp_max"] = comp["joint_diff_pp_max"]
                lvl_payload["joint_direction_settled"] = comp["joint_direction_settled"]
                lvl_payload["joint_direction"] = comp["joint_direction"]
                lvl_payload["of_classified"] = comp["of_classified"]
                if "classified_share_reason" in comp:
                    lvl_payload["classified_share_reason"] = comp[
                        "classified_share_reason"
                    ]
            else:
                lvl_payload["comparison_available"] = False
                lvl_payload["comparison_reason"] = (
                    obs["reason"]
                    if not obs["reported"]
                    else "table_rows_not_one_classification"
                )
                lvl_payload["comparison"] = None

            benchmarks_by_lvl[lvl] = lvl_payload

        dim_entry["benchmarks"] = benchmarks_by_lvl
        dim_entry["geography_sensitivity"] = _compute_geography_sensitivity(
            benchmarks_by_lvl, cats
        )
        dimensions_out[dim] = dim_entry

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "snapshot_date": snapshot_date,
        "nct_id": norm["nct_id"],
        "study_type": norm["study_type"],
        "registered_enrollment": norm["enrollment"],
        "enrollment_type": norm["enrollment_type"],
        "referral_zip": any_referral_zip,
        "footprint": fp,
        "resolution": resolution,
        "period": period,
        "benchmark_period": bench_period,
        "eligibility": elig_info,
        "reference_population": ref_pop,
        "dimensions": dimensions_out,
    }


def _build_proxy_pool_row(
    dim_block: Mapping[str, Any],
    proxy_block: Mapping[str, Any],
) -> dict[str, Any]:
    denom = dim_block["denominators"]
    unk_or_unmapped = denom.get("unknown_or_unmapped_pct", denom.get("unknown_pct", 0.0))
    return {
        "n": int(dim_block["n_in_table"]),
        "obs": dict(dim_block["observed_pct"]),
        "exp": dict(proxy_block["pct"]),
        "moe": dict(proxy_block["moe90_pp"]),
        "obs_known": dim_block.get("observed_pct_of_classified"),
        "n_known": int(denom["classified"]),
        "unknown": float(unk_or_unmapped),
    }


def _summarize_joint_settlement(
    qualifying_benchmarks: Sequence[Mapping[str, Any]],
    categories: Sequence[str],
    trial_weights_n: np.ndarray,
) -> dict[str, dict[str, Any]]:
    """Summarize joint settlement outcomes and bounds envelopes across qualifying trials."""
    n_trials = len(qualifying_benchmarks)
    total_n = float(trial_weights_n.sum())
    summary: dict[str, dict[str, Any]] = {}

    for cat in categories:
        n_neg = 0
        n_pos = 0
        n_unsettled = 0
        n_unsettled_incomplete = 0
        n_unsettled_within = 0
        n_site_settled = 0

        res_mins = np.zeros(n_trials, dtype=float)
        res_maxs = np.zeros(n_trials, dtype=float)
        uncond_mins = np.zeros(n_trials, dtype=float)
        uncond_maxs = np.zeros(n_trials, dtype=float)

        for idx, b in enumerate(qualifying_benchmarks):
            comp_cat = b["comparison"]["by_category"][cat]
            res_mins[idx] = float(comp_cat["resolved_min_pct"])
            res_maxs[idx] = float(comp_cat["resolved_max_pct"])
            uncond_mins[idx] = float(comp_cat["unconditional_min_pct"])
            uncond_maxs[idx] = float(comp_cat["unconditional_max_pct"])

            if comp_cat["site_only_settled"]:
                n_site_settled += 1
            if comp_cat["joint_direction_settled"]:
                if comp_cat["joint_direction"] == "negative":
                    n_neg += 1
                else:
                    n_pos += 1
            else:
                n_unsettled += 1
                if not comp_cat["bounds_unconditional"]:
                    n_unsettled_incomplete += 1
                else:
                    n_unsettled_within += 1

        weighted_res: list[float] | None = (
            [
                round(float((res_mins * trial_weights_n).sum() / total_n), 2),
                round(float((res_maxs * trial_weights_n).sum() / total_n), 2),
            ]
            if total_n > 0
            else None
        )
        weighted_uncond: list[float] | None = (
            [
                round(float((uncond_mins * trial_weights_n).sum() / total_n), 2),
                round(float((uncond_maxs * trial_weights_n).sum() / total_n), 2),
            ]
            if total_n > 0
            else None
        )

        summary[cat] = {
            "n_trials": n_trials,
            "n_settled_negative": n_neg,
            "n_settled_positive": n_pos,
            "n_unsettled": n_unsettled,
            "n_unsettled_incomplete_resolution": n_unsettled_incomplete,
            "n_unsettled_within_bounds_or_unknowns": n_unsettled_within,
            "settled_negative_pct": round(100.0 * n_neg / n_trials, 2),
            "settled_positive_pct": round(100.0 * n_pos / n_trials, 2),
            "unsettled_pct": round(100.0 * n_unsettled / n_trials, 2),
            "n_site_only_settled": n_site_settled,
            "site_only_settled_pct": round(100.0 * n_site_settled / n_trials, 2),
            "mean_resolved_bounds_pct": [
                round(float(res_mins.mean()), 2),
                round(float(res_maxs.mean()), 2),
            ],
            "mean_unconditional_bounds_pct": [
                round(float(uncond_mins.mean()), 2),
                round(float(uncond_maxs.mean()), 2),
            ],
            "weighted_resolved_bounds_pct": weighted_res,
            "weighted_unconditional_bounds_pct": weighted_uncond,
        }

    return summary


def _build_mixed_international_diagnostic(
    mixed_trials: Sequence[Mapping[str, Any]],
    *,
    level: str,
    dimension: str,
) -> dict[str, Any]:
    """Contextual diagnostic summary for mixed US + international trials.

    Strictly separated from US-only rollups because ClinicalTrials.gov baseline tables
    pool US and foreign participants without country-level stratification.
    """
    n_total = len(mixed_trials)
    us_shares = [
        float(r["footprint"]["us_facility_share"])
        for r in mixed_trials
        if r["footprint"].get("us_facility_share") is not None
    ]
    usable_with_us_bench = 0
    global_table_participants = 0
    for r in mixed_trials:
        d = (r.get("dimensions") or {}).get(dimension) or {}
        b = (d.get("benchmarks") or {}).get(level) or {}
        if d.get("state") == "reported":
            global_table_participants += _valid_positive_table_total(d.get("n_in_table")) or 0
        if b.get("available") and b.get("comparison_available"):
            usable_with_us_bench += 1

    return {
        "footprint_tier": "mixed_us_and_international",
        "included_in_primary_us_cohort": False,
        "global_pooled_baseline_table": True,
        "warning": GLOBAL_POOLED_WARNING,
        "n_trials_total": n_total,
        "n_trials_with_reported_table_and_us_sites_at_level": usable_with_us_bench,
        "n_participants_in_global_pooled_tables": global_table_participants,
        "mean_us_facility_share": (
            round(float(np.mean(us_shares)), 4) if us_shares else None
        ),
    }


def _valid_positive_table_total(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if math.isfinite(value) and value > 0 and value.is_integer():
            return int(value)
        return None
    return None


def pool_multisite_cohort(
    trial_results: Sequence[Mapping[str, Any]],
    *,
    level: str = "cbsa",
    dimension: str = "race",
    floor: int = 50,
    resamples: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """Pool ``multi_site_us_only`` trial benchmark records into a deterministic cohort summary.

    Reports equal-trial-weighted (primary) and participant-weighted (secondary)
    comparisons against both ``equal_facility_proxy`` and ``distinct_area_resident_proxy``,
    plus the share of trials where ``joint_direction_settled`` is negative, positive, or
    unsettled. Keeps ``mixed_us_and_international`` strictly isolated in a contextual
    diagnostic block and never pools it into US-only figures.
    """
    if level not in LEVEL_ORDER:
        raise ValueError(f"unsupported geography level: {level!r}")
    if dimension not in DIMENSIONS:
        raise ValueError(f"unsupported dimension: {dimension!r}")
    if isinstance(floor, bool) or not isinstance(floor, int):
        raise TypeError(f"floor must be an integer, got {type(floor).__name__}")
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        raise TypeError(f"resamples must be an integer, got {type(resamples).__name__}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"seed must be an integer, got {type(seed).__name__}")
    if floor < 0:
        raise ValueError(f"floor must be >= 0, got {floor}")
    if resamples < 1:
        raise ValueError(f"resamples must be >= 1, got {resamples}")

    cats = list(bounds.COMPARED_CATEGORIES[dimension])
    tier_counts: dict[str, int] = {t: 0 for t in footprint.FOOTPRINT_TIERS}
    multisite_us_trials: list[Mapping[str, Any]] = []
    mixed_intl_trials: list[Mapping[str, Any]] = []

    for rec in trial_results:
        tier = str((rec.get("footprint") or {}).get("footprint_tier") or "")
        if tier in tier_counts:
            tier_counts[tier] += 1
        if tier == "multi_site_us_only":
            multisite_us_trials.append(rec)
        elif tier == "mixed_us_and_international":
            mixed_intl_trials.append(rec)

    exclusion_reasons: collections.Counter[str] = collections.Counter()
    eq_rows: list[dict[str, Any]] = []
    res_rows: list[dict[str, Any]] = []
    qualifying_bench: list[Mapping[str, Any]] = []

    for rec in multisite_us_trials:
        d = (rec.get("dimensions") or {}).get(dimension) or {}
        if d.get("state") != "reported":
            exclusion_reasons[str(d.get("reason") or f"{dimension}_not_reported")] += 1
            continue
        valid_total = _valid_positive_table_total(d.get("n_in_table"))
        if valid_total is None:
            exclusion_reasons[f"{dimension}_table_total_is_zero"] += 1
            continue
        if not d.get("pooled"):
            exclusion_reasons[str(d.get("not_pooled_reason") or "not_pooled")] += 1
            continue
        b = (d.get("benchmarks") or {}).get(level) or {}
        if not b.get("available"):
            exclusion_reasons[
                f"level_unavailable:{b.get('reason') or 'unavailable'}"
            ] += 1
            continue
        if not b.get("comparison_available"):
            exclusion_reasons[
                str(b.get("comparison_reason") or "comparison_unavailable")
            ] += 1
            continue

        eq_rows.append(_build_proxy_pool_row(d, b["equal_facility_proxy"]))
        res_rows.append(_build_proxy_pool_row(d, b["distinct_area_resident_proxy"]))
        qualifying_bench.append(b)

    n_qualifying = len(eq_rows)
    n_participants = sum(int(r["n"]) for r in eq_rows)
    n_unconditional = sum(
        1 for b in qualifying_bench if b.get("bounds_unconditional") is True
    )
    n_partial = n_qualifying - n_unconditional

    mixed_diag = _build_mixed_international_diagnostic(
        mixed_intl_trials, level=level, dimension=dimension
    )

    base_out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "cohort_scope": "multi_site_us_only",
        "level": level,
        "dimension": dimension,
        "parameters": {
            "floor": floor,
            "resamples": resamples,
            "seed": seed,
        },
        "footprint_counts": tier_counts,
        "n_multisite_us_candidates": len(multisite_us_trials),
        "n_trials": n_qualifying,
        "n_participants": n_participants,
        "n_unconditional_bounds_trials": n_unconditional,
        "n_partial_resolution_trials": n_partial,
        "excluded_multisite_us_reasons": {
            k: exclusion_reasons[k] for k in sorted(exclusion_reasons)
        },
        "mixed_us_and_international_diagnostic": mixed_diag,
    }

    if n_qualifying == 0 or n_qualifying < floor:
        gate_msg = (
            "no qualifying trials"
            if n_qualifying == 0 and floor == 0
            else f"fewer than {floor} qualifying trials"
        )
        return {
            **base_out,
            "display_rule": "withheld",
            "gate_reason": gate_msg,
        }

    eq_pooled = census_benchmark.pool(
        eq_rows,
        cats,
        floor=floor,
        resamples=resamples,
        rng=np.random.default_rng(seed),
    )
    res_pooled = census_benchmark.pool(
        res_rows,
        cats,
        floor=floor,
        resamples=resamples,
        rng=np.random.default_rng(seed),
    )
    weights_n = np.array([r["n"] for r in eq_rows], dtype=float)
    joint_summary = _summarize_joint_settlement(qualifying_bench, cats, weights_n)

    return {
        **base_out,
        "display_rule": "show",
        "gate_reason": None,
        "concentration_flag": eq_pooled["concentration_flag"],
        "denominators": eq_pooled["denominators"],
        "equal_facility_proxy": eq_pooled,
        "distinct_area_resident_proxy": res_pooled,
        "joint_settlement": joint_summary,
    }
