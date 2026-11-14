#!/usr/bin/env python3
"""Deterministic field-by-field comparison, adjudication application, and verbatim quote verification."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

SEGMENT_RE = re.compile(r"^([A-Za-z0-9_]+)((?:\[\d+\])*)$")
INDEX_RE = re.compile(r"\[(\d+)\]")


def resolve_json_path(raw_record: dict[str, Any], path_str: str) -> tuple[bool, Any, str]:
    """Deterministically resolve a dot-and-bracket JSON path in raw_record.

    Returns (found, value, error_reason). Does NOT fuzzy-match or repair paths.
    """
    if not path_str or not isinstance(path_str, str):
        return False, None, "empty_path"
    cleaned = path_str[2:] if path_str.startswith("$.") else path_str
    cur: Any = raw_record
    for part in cleaned.split("."):
        m = SEGMENT_RE.match(part)
        if not m:
            return False, None, f"invalid_path_segment:{part}"
        key, idx_part = m.group(1), m.group(2)
        if not isinstance(cur, dict) or key not in cur:
            return False, None, f"missing_key:{key}"
        cur = cur[key]
        if idx_part:
            for idx_m in INDEX_RE.finditer(idx_part):
                idx = int(idx_m.group(1))
                if not isinstance(cur, list) or idx < 0 or idx >= len(cur):
                    return False, None, f"index_out_of_range:{key}[{idx}]"
                cur = cur[idx]
    if cur is None or isinstance(cur, (dict, list)):
        return False, cur, f"non_scalar_target:{type(cur).__name__}"
    return True, cur, ""


def _canon_demo_domain(reading: dict[str, Any], domain: str) -> dict[str, Any]:
    measures = [
        {
            "baseline_measure_index": m["baseline_measure_index"],
            "measure_title": m["measure_title"],
            "param_type": m["param_type"],
            "unit_as_posted": m["unit_as_posted"],
        }
        for m in sorted(
            (x for x in reading.get("baseline_demographic_measures", []) if x.get("domain") == domain),
            key=lambda x: x["baseline_measure_index"],
        )
    ]
    rows = [
        {
            "baseline_measure_index": r["baseline_measure_index"],
            "row_index": r["row_index"],
            "class_index": r["class_index"],
            "category_index": r["category_index"],
            "class_title": r["class_title"],
            "category_title": r["category_title"],
            "total_group_id": r["total_group_id"],
            "total_value": r["total_value"],
            "total_spread": r["total_spread"],
        }
        for r in sorted(
            (x for x in reading.get("baseline_demographic_rows", []) if x.get("domain") == domain),
            key=lambda x: (x["baseline_measure_index"], x["row_index"]),
        )
    ]
    return {"measures": measures, "rows": rows}


def _canon_arm_groups(m: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "group_id": g["group_id"],
            "group_title": g["group_title"],
            "number_analysed": g["number_analysed"],
            "number_analysed_unit": g["number_analysed_unit"],
        }
        for g in m.get("arm_groups", [])
    ]


def _canon_arm_measurements(m: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "group_id": x["group_id"],
            "class_index": x["class_index"],
            "category_index": x["category_index"],
            "class_title": x["class_title"],
            "category_title": x["category_title"],
            "row_number_analysed": x["row_number_analysed"],
            "value": x["value"],
            "spread": x["spread"],
            "lower_limit": x["lower_limit"],
            "upper_limit": x["upper_limit"],
        }
        for x in sorted(
            m.get("arm_measurements", []),
            key=lambda r: (r["class_index"], r["category_index"], r["group_id"]),
        )
    ]


def _canon_baseline_hba1c(reading: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        reading.get("baseline_hba1c_rows", []),
        key=lambda r: (r["baseline_measure_index"], r["group_id"], r["class_title"], r["category_title"]),
    )


def _canon_countries(reading: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(reading.get("countries", []), key=lambda c: c["country"])


def compare_trial_readings(r1: dict[str, Any], r2: dict[str, Any]) -> dict[str, Any]:
    """Compare two independent readings field-by-field deterministically (no LLM judgement)."""
    fields: list[dict[str, Any]] = []

    def add_cmp(field_key: str, category: str, scope: str, v1: Any, v2: Any, q1: Any = None, q2: Any = None) -> None:
        agreed = v1 == v2
        entry: dict[str, Any] = {
            "field_key": field_key,
            "category": category,
            "scope": scope,  # "trial" or f"measure:{mi}"
            "r1": v1,
            "r2": v2,
            "agreed": agreed,
        }
        if q1 is not None or q2 is not None:
            entry["r1_quotes"] = q1
            entry["r2_quotes"] = q2
        fields.append(entry)

    p1 = r1.get("hba1c_primary_measure_posted")
    p2 = r2.get("hba1c_primary_measure_posted")
    add_cmp("trial.hba1c_primary_measure_posted", "hba1c_primary_measure_posted", "trial", p1, p2)

    m1_map = {m["measure_index"]: m for m in r1.get("measures", [])}
    m2_map = {m["measure_index"]: m for m in r2.get("measures", [])}
    idx1 = sorted(m1_map.keys())
    idx2 = sorted(m2_map.keys())
    add_cmp("trial.measure_indices", "hba1c_primary_measure_indices", "trial", idx1, idx2)

    if p1 == "yes" or p2 == "yes":
        add_cmp(
            "trial.baseline_population_total",
            "baseline_population_total",
            "trial",
            {"value": r1.get("baseline_population_total"), "unit": r1.get("baseline_population_total_unit")},
            {"value": r2.get("baseline_population_total"), "unit": r2.get("baseline_population_total_unit")},
        )
        add_cmp(
            "trial.participants_started_by_group",
            "participants_started",
            "trial",
            r1.get("participants_started_by_group", []),
            r2.get("participants_started_by_group", []),
        )
        add_cmp(
            "trial.baseline_hba1c_posted",
            "baseline_hba1c_posted",
            "trial",
            r1.get("baseline_hba1c_posted"),
            r2.get("baseline_hba1c_posted"),
        )
        add_cmp(
            "trial.baseline_hba1c_rows",
            "baseline_hba1c_values",
            "trial",
            _canon_baseline_hba1c(r1),
            _canon_baseline_hba1c(r2),
        )
        for dom in ("race", "ethnicity", "sex", "age"):
            q1_dom = [
                {"text": m["measure_title_quote_text"], "path": m["measure_title_quote_path"]}
                for m in r1.get("baseline_demographic_measures", [])
                if m.get("domain") == dom
            ]
            q2_dom = [
                {"text": m["measure_title_quote_text"], "path": m["measure_title_quote_path"]}
                for m in r2.get("baseline_demographic_measures", [])
                if m.get("domain") == dom
            ]
            add_cmp(
                f"trial.baseline_{dom}",
                f"baseline_{dom}_table",
                "trial",
                _canon_demo_domain(r1, dom),
                _canon_demo_domain(r2, dom),
                q1_dom,
                q2_dom,
            )
        add_cmp(
            "trial.race_and_ethnicity_in_one_table",
            "race_and_ethnicity_in_one_table",
            "trial",
            r1.get("race_and_ethnicity_in_one_table"),
            r2.get("race_and_ethnicity_in_one_table"),
        )
        add_cmp(
            "trial.race_and_ethnicity_in_one_table_row_labels",
            "race_and_ethnicity_in_one_table_row_labels",
            "trial",
            r1.get("race_and_ethnicity_in_one_table_row_labels", []),
            r2.get("race_and_ethnicity_in_one_table_row_labels", []),
        )
        add_cmp(
            "trial.countries",
            "where_the_trial_ran_countries",
            "trial",
            _canon_countries(r1),
            _canon_countries(r2),
        )

    all_measure_indices = sorted(set(idx1) | set(idx2))
    for mi in all_measure_indices:
        scope = f"measure:{mi}"
        if mi not in m1_map or mi not in m2_map:
            add_cmp(
                f"measure[{mi}].included",
                "measure_included_as_hba1c_primary",
                scope,
                mi in m1_map,
                mi in m2_map,
            )
            continue

        m1 = m1_map[mi]
        m2 = m2_map[mi]

        add_cmp(
            f"measure[{mi}].measure_title",
            "measure_title",
            scope,
            m1["measure_title"],
            m2["measure_title"],
            [{"text": m1["measure_title_quote_text"], "path": m1["measure_title_quote_path"]}],
            [{"text": m2["measure_title_quote_text"], "path": m2["measure_title_quote_path"]}],
        )
        add_cmp(
            f"measure[{mi}].construct",
            "construct",
            scope,
            m1["construct"],
            m2["construct"],
            m1.get("construct_quotes", []),
            m2.get("construct_quotes", []),
        )
        add_cmp(
            f"measure[{mi}].unit_as_posted",
            "unit_as_posted",
            scope,
            m1["unit_as_posted"],
            m2["unit_as_posted"],
            [{"text": m1["unit_as_posted_quote_text"], "path": m1["unit_as_posted_quote_path"]}] if m1["unit_as_posted"] != "not_stated" else [],
            [{"text": m2["unit_as_posted_quote_text"], "path": m2["unit_as_posted_quote_path"]}] if m2["unit_as_posted"] != "not_stated" else [],
        )
        add_cmp(f"measure[{mi}].unit_class", "unit_class", scope, m1["unit_class"], m2["unit_class"])
        add_cmp(
            f"measure[{mi}].time_frame_as_posted",
            "time_frame_as_posted",
            scope,
            m1["time_frame_as_posted"],
            m2["time_frame_as_posted"],
            [{"text": m1["time_frame_as_posted_quote_text"], "path": m1["time_frame_as_posted_quote_path"]}] if m1["time_frame_as_posted"] != "not_stated" else [],
            [{"text": m2["time_frame_as_posted_quote_text"], "path": m2["time_frame_as_posted_quote_path"]}] if m2["time_frame_as_posted"] != "not_stated" else [],
        )
        add_cmp(f"measure[{mi}].time_point_weeks", "time_point_weeks", scope, m1["time_point_weeks"], m2["time_point_weeks"])
        add_cmp(
            f"measure[{mi}].population_description_as_posted",
            "population_description_as_posted",
            scope,
            m1["population_description_as_posted"],
            m2["population_description_as_posted"],
            [{"text": m1["population_description_quote_text"], "path": m1["population_description_quote_path"]}] if m1["population_description_as_posted"] != "not_stated" else [],
            [{"text": m2["population_description_quote_text"], "path": m2["population_description_quote_path"]}] if m2["population_description_as_posted"] != "not_stated" else [],
        )
        add_cmp(f"measure[{mi}].population_class", "population_class", scope, m1["population_class"], m2["population_class"])
        add_cmp(f"measure[{mi}].population_defined", "population_defined", scope, m1["population_defined"], m2["population_defined"])
        add_cmp(
            f"measure[{mi}].missing_data_method_as_posted",
            "missing_data_method_as_posted",
            scope,
            m1["missing_data_method_as_posted"],
            m2["missing_data_method_as_posted"],
            m1.get("missing_data_method_quotes", []),
            m2.get("missing_data_method_quotes", []),
        )
        add_cmp(
            f"measure[{mi}].data_after_rescue_therapy",
            "data_after_rescue_therapy",
            scope,
            m1["data_after_rescue_therapy"],
            m2["data_after_rescue_therapy"],
            m1.get("data_after_rescue_therapy_quotes", []),
            m2.get("data_after_rescue_therapy_quotes", []),
        )
        add_cmp(f"measure[{mi}].parameter_type", "parameter_type", scope, m1["parameter_type"], m2["parameter_type"])
        add_cmp(f"measure[{mi}].dispersion_type", "dispersion_type", scope, m1["dispersion_type"], m2["dispersion_type"])
        add_cmp(
            f"measure[{mi}].arm_groups",
            "arm_groups",
            scope,
            _canon_arm_groups(m1),
            _canon_arm_groups(m2),
            [{"text": g["group_title_quote_text"], "path": g["group_title_quote_path"]} for g in m1.get("arm_groups", [])],
            [{"text": g["group_title_quote_text"], "path": g["group_title_quote_path"]} for g in m2.get("arm_groups", [])],
        )
        add_cmp(
            f"measure[{mi}].arm_measurements",
            "arm_measurements",
            scope,
            _canon_arm_measurements(m1),
            _canon_arm_measurements(m2),
        )
        add_cmp(
            f"measure[{mi}].comparator_class",
            "comparator_class",
            scope,
            m1["comparator_class"],
            m2["comparator_class"],
            m1.get("comparator_class_quotes", []),
            m2.get("comparator_class_quotes", []),
        )
        add_cmp(
            f"measure[{mi}].background_therapy_as_posted",
            "background_therapy_as_posted",
            scope,
            m1["background_therapy_as_posted"],
            m2["background_therapy_as_posted"],
            m1.get("background_therapy_quotes", []),
            m2.get("background_therapy_quotes", []),
        )

        a1_map = {a["analysis_index"]: a for a in m1.get("analyses", [])}
        a2_map = {a["analysis_index"]: a for a in m2.get("analyses", [])}
        a_idx1 = sorted(a1_map.keys())
        a_idx2 = sorted(a2_map.keys())
        add_cmp(f"measure[{mi}].analysis_indices", "between_arm_analysis_indices", scope, a_idx1, a_idx2)

        for ai in sorted(set(a_idx1) | set(a_idx2)):
            if ai not in a1_map or ai not in a2_map:
                add_cmp(
                    f"measure[{mi}].analysis[{ai}].included",
                    "analysis_included",
                    scope,
                    ai in a1_map,
                    ai in a2_map,
                )
                continue
            an1 = a1_map[ai]
            an2 = a2_map[ai]
            add_cmp(f"measure[{mi}].analysis[{ai}].groups_compared", "analysis.groups_compared", scope, an1["groups_compared"], an2["groups_compared"])
            add_cmp(
                f"measure[{mi}].analysis[{ai}].estimate_type_as_posted",
                "analysis.estimate_type_as_posted",
                scope,
                an1["estimate_type_as_posted"],
                an2["estimate_type_as_posted"],
                [{"text": an1["estimate_type_quote_text"], "path": an1["estimate_type_quote_path"]}] if an1["estimate_type_as_posted"] != "not_stated" else [],
                [{"text": an2["estimate_type_quote_text"], "path": an2["estimate_type_quote_path"]}] if an2["estimate_type_as_posted"] != "not_stated" else [],
            )
            add_cmp(f"measure[{mi}].analysis[{ai}].estimate", "analysis.estimate", scope, an1["estimate"], an2["estimate"])
            add_cmp(f"measure[{mi}].analysis[{ai}].ci_percent", "analysis.ci_percent", scope, an1["ci_percent"], an2["ci_percent"])
            add_cmp(f"measure[{mi}].analysis[{ai}].ci_lower", "analysis.ci_lower", scope, an1["ci_lower"], an2["ci_lower"])
            add_cmp(f"measure[{mi}].analysis[{ai}].ci_upper", "analysis.ci_upper", scope, an1["ci_upper"], an2["ci_upper"])
            add_cmp(f"measure[{mi}].analysis[{ai}].ci_sides", "analysis.ci_sides", scope, an1["ci_sides"], an2["ci_sides"])
            add_cmp(f"measure[{mi}].analysis[{ai}].p_value_as_posted", "analysis.p_value_as_posted", scope, an1["p_value_as_posted"], an2["p_value_as_posted"])
            add_cmp(f"measure[{mi}].analysis[{ai}].hypothesis_class", "analysis.hypothesis_class", scope, an1["hypothesis_class"], an2["hypothesis_class"])
            add_cmp(
                f"measure[{mi}].analysis[{ai}].non_inferiority_margin_as_posted",
                "analysis.non_inferiority_margin_as_posted",
                scope,
                an1["non_inferiority_margin_as_posted"],
                an2["non_inferiority_margin_as_posted"],
                [{"text": an1["non_inferiority_margin_quote_text"], "path": an1["non_inferiority_margin_quote_path"]}] if an1["non_inferiority_margin_as_posted"] != "not_stated" else [],
                [{"text": an2["non_inferiority_margin_quote_text"], "path": an2["non_inferiority_margin_quote_path"]}] if an2["non_inferiority_margin_as_posted"] != "not_stated" else [],
            )
            add_cmp(
                f"measure[{mi}].analysis[{ai}].statistical_method_as_posted",
                "analysis.statistical_method_as_posted",
                scope,
                an1["statistical_method_as_posted"],
                an2["statistical_method_as_posted"],
                [{"text": an1["statistical_method_quote_text"], "path": an1["statistical_method_quote_path"]}] if an1["statistical_method_as_posted"] != "not_stated" else [],
                [{"text": an2["statistical_method_quote_text"], "path": an2["statistical_method_quote_path"]}] if an2["statistical_method_as_posted"] != "not_stated" else [],
            )

    disagreements = [f for f in fields if not f["agreed"]]
    return {
        "fields_compared_count": len(fields),
        "fields_disagreed_count": len(disagreements),
        "disagreed_field_keys": [f["field_key"] for f in disagreements],
        "fields": fields,
        "disagreements": disagreements,
    }


def apply_adjudication(
    r1: dict[str, Any],
    r2: dict[str, Any],
    comparison: dict[str, Any],
    adj_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build final adjudicated trial sheet deterministically from r1, r2, and Adjudicator decisions."""
    # Start from deep copy of r1 as base for all agreed fields
    final = copy.deepcopy(r1)
    unresolved_keys: list[str] = []
    adjudication_map: dict[str, dict[str, Any]] = {}
    if adj_raw:
        for dec in adj_raw.get("decisions", []):
            adjudication_map[dec["field_key"]] = dec

    full_adj_measures = {
        m["measure_index"]: copy.deepcopy(m)
        for m in (adj_raw.get("full_measure_if_needed", []) if adj_raw else [])
    }

    def pick_scalar_or_obj(fk: str, val_r1: Any, val_r2: Any) -> tuple[Any, str, list[dict[str, str]]]:
        dec = adjudication_map.get(fk)
        if dec is None:
            unresolved_keys.append(fk)
            return "unresolved", "unresolved", []
        ch = dec["choice"]
        quotes = dec.get("deciding_quotes", [])
        if ch == "reading_1":
            return copy.deepcopy(val_r1), "reading_1", quotes
        if ch == "reading_2":
            return copy.deepcopy(val_r2), "reading_2", quotes
        if ch == "custom_verbatim":
            try:
                parsed = json.loads(dec["custom_value_json"])
                return parsed, "custom_verbatim", quotes
            except Exception:
                return dec["custom_value_json"], "custom_verbatim", quotes
        unresolved_keys.append(fk)
        return "unresolved", "unresolved", quotes

    disagreed_set = set(comparison["disagreed_field_keys"])
    if not disagreed_set:
        return {
            "sheet": final,
            "unresolved_field_keys": [],
            "adjudicated_decisions_applied": {},
        }

    applied_log: dict[str, Any] = {}

    # 1. Trial-level fields
    if "trial.hba1c_primary_measure_posted" in disagreed_set:
        val, src, qs = pick_scalar_or_obj(
            "trial.hba1c_primary_measure_posted",
            r1["hba1c_primary_measure_posted"],
            r2["hba1c_primary_measure_posted"],
        )
        final["hba1c_primary_measure_posted"] = val
        applied_log["trial.hba1c_primary_measure_posted"] = {"choice": src, "value": val, "quotes": qs}

    if "trial.baseline_population_total" in disagreed_set:
        dec = adjudication_map.get("trial.baseline_population_total")
        if dec and dec["choice"] == "reading_2":
            final["baseline_population_total"] = r2["baseline_population_total"]
            final["baseline_population_total_unit"] = r2["baseline_population_total_unit"]
            applied_log["trial.baseline_population_total"] = {"choice": "reading_2", "quotes": dec.get("deciding_quotes", [])}
        elif dec and dec["choice"] == "reading_1":
            applied_log["trial.baseline_population_total"] = {"choice": "reading_1", "quotes": dec.get("deciding_quotes", [])}
        elif dec and dec["choice"] == "custom_verbatim":
            val, src, qs = pick_scalar_or_obj("trial.baseline_population_total", None, None)
            if isinstance(val, dict):
                final["baseline_population_total"] = str(val.get("value", "unresolved"))
                final["baseline_population_total_unit"] = str(val.get("unit", "unresolved"))
            else:
                final["baseline_population_total"] = str(val)
            applied_log["trial.baseline_population_total"] = {"choice": src, "value": val, "quotes": qs}
        else:
            unresolved_keys.append("trial.baseline_population_total")
            final["baseline_population_total"] = "unresolved"
            applied_log["trial.baseline_population_total"] = {"choice": "unresolved"}

    for fk, attr in [
        ("trial.participants_started_by_group", "participants_started_by_group"),
        ("trial.baseline_hba1c_posted", "baseline_hba1c_posted"),
        ("trial.baseline_hba1c_rows", "baseline_hba1c_rows"),
        ("trial.race_and_ethnicity_in_one_table", "race_and_ethnicity_in_one_table"),
        ("trial.race_and_ethnicity_in_one_table_row_labels", "race_and_ethnicity_in_one_table_row_labels"),
        ("trial.countries", "countries"),
    ]:
        if fk in disagreed_set:
            val, src, qs = pick_scalar_or_obj(fk, r1.get(attr), r2.get(attr))
            if src == "unresolved" and isinstance(r1.get(attr), list):
                # Keep empty list on unresolved list fields while flagging unresolved_keys
                final[attr] = []
            else:
                final[attr] = val
            applied_log[fk] = {"choice": src, "quotes": qs}

    for dom in ("race", "ethnicity", "sex", "age"):
        fk = f"trial.baseline_{dom}"
        if fk in disagreed_set:
            dec = adjudication_map.get(fk)
            # Remove r1's entries for this domain first, then insert chosen domain entries
            other_m = [x for x in final.get("baseline_demographic_measures", []) if x.get("domain") != dom]
            other_r = [x for x in final.get("baseline_demographic_rows", []) if x.get("domain") != dom]
            if dec and dec["choice"] == "reading_1":
                dom_m = [copy.deepcopy(x) for x in r1.get("baseline_demographic_measures", []) if x.get("domain") == dom]
                dom_r = [copy.deepcopy(x) for x in r1.get("baseline_demographic_rows", []) if x.get("domain") == dom]
                final["baseline_demographic_measures"] = other_m + dom_m
                final["baseline_demographic_rows"] = other_r + dom_r
                applied_log[fk] = {"choice": "reading_1", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "reading_2":
                dom_m = [copy.deepcopy(x) for x in r2.get("baseline_demographic_measures", []) if x.get("domain") == dom]
                dom_r = [copy.deepcopy(x) for x in r2.get("baseline_demographic_rows", []) if x.get("domain") == dom]
                final["baseline_demographic_measures"] = other_m + dom_m
                final["baseline_demographic_rows"] = other_r + dom_r
                applied_log[fk] = {"choice": "reading_2", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "custom_verbatim":
                val, src, qs = pick_scalar_or_obj(fk, None, None)
                dom_m = []
                dom_r = []
                if isinstance(val, dict):
                    for m in val.get("measures", []):
                        q = qs[0] if qs else {"text": m.get("measure_title", ""), "path": ""}
                        dom_m.append({
                            "domain": dom,
                            "baseline_measure_index": m["baseline_measure_index"],
                            "measure_title": m["measure_title"],
                            "measure_title_quote_text": m.get("measure_title_quote_text", q["text"]),
                            "measure_title_quote_path": m.get("measure_title_quote_path", q["path"]),
                            "param_type": m.get("param_type", "not_stated"),
                            "unit_as_posted": m.get("unit_as_posted", "not_stated"),
                        })
                    for r in val.get("rows", []):
                        dom_r.append({"domain": dom, **r})
                final["baseline_demographic_measures"] = other_m + dom_m
                final["baseline_demographic_rows"] = other_r + dom_r
                applied_log[fk] = {"choice": src, "quotes": qs}
            else:
                unresolved_keys.append(fk)
                final["baseline_demographic_measures"] = other_m
                final["baseline_demographic_rows"] = other_r
                applied_log[fk] = {"choice": "unresolved"}

    # 2. Measure list and measure-level fields
    m1_map = {m["measure_index"]: m for m in r1.get("measures", [])}
    m2_map = {m["measure_index"]: m for m in r2.get("measures", [])}
    target_indices = sorted(m1_map.keys())
    if "trial.measure_indices" in disagreed_set:
        val, src, qs = pick_scalar_or_obj("trial.measure_indices", sorted(m1_map.keys()), sorted(m2_map.keys()))
        if isinstance(val, list):
            target_indices = [int(x) for x in val]
        applied_log["trial.measure_indices"] = {"choice": src, "value": val, "quotes": qs}

    merged_measures: list[dict[str, Any]] = []
    for mi in target_indices:
        inc_fk = f"measure[{mi}].included"
        if inc_fk in disagreed_set:
            inc_val, inc_src, inc_qs = pick_scalar_or_obj(inc_fk, mi in m1_map, mi in m2_map)
            applied_log[inc_fk] = {"choice": inc_src, "value": inc_val, "quotes": inc_qs}
            if inc_val is False:
                continue

        if mi in m1_map and mi in m2_map:
            m_cur = copy.deepcopy(m1_map[mi])
            m_alt = m2_map[mi]
        elif mi in full_adj_measures:
            merged_measures.append(full_adj_measures[mi])
            continue
        elif mi in m1_map:
            merged_measures.append(copy.deepcopy(m1_map[mi]))
            continue
        elif mi in m2_map:
            merged_measures.append(copy.deepcopy(m2_map[mi]))
            continue
        else:
            continue

        # Simple measure fields + quote pairs
        simple_quote_fields = [
            ("measure_title", "measure_title_quote_text", "measure_title_quote_path"),
            ("unit_as_posted", "unit_as_posted_quote_text", "unit_as_posted_quote_path"),
            ("time_frame_as_posted", "time_frame_as_posted_quote_text", "time_frame_as_posted_quote_path"),
            ("population_description_as_posted", "population_description_quote_text", "population_description_quote_path"),
        ]
        for attr, qt_attr, qp_attr in simple_quote_fields:
            fk = f"measure[{mi}].{attr}"
            if fk in disagreed_set:
                val, src, qs = pick_scalar_or_obj(fk, m_cur[attr], m_alt[attr])
                m_cur[attr] = str(val)
                if src == "reading_2":
                    m_cur[qt_attr] = m_alt[qt_attr]
                    m_cur[qp_attr] = m_alt[qp_attr]
                elif src == "custom_verbatim":
                    if qs:
                        m_cur[qt_attr] = qs[0]["text"]
                        m_cur[qp_attr] = qs[0]["path"]
                    elif m_cur[attr] in ("not_stated", "unresolved"):
                        m_cur[qt_attr] = ""
                        m_cur[qp_attr] = ""
                elif src == "unresolved":
                    m_cur[qt_attr] = ""
                    m_cur[qp_attr] = ""
                applied_log[fk] = {"choice": src, "value": m_cur[attr], "quotes": qs}

        list_quote_fields = [
            ("construct", "construct_quotes"),
            ("missing_data_method_as_posted", "missing_data_method_quotes"),
            ("data_after_rescue_therapy", "data_after_rescue_therapy_quotes"),
            ("comparator_class", "comparator_class_quotes"),
            ("background_therapy_as_posted", "background_therapy_quotes"),
        ]
        for attr, q_list_attr in list_quote_fields:
            fk = f"measure[{mi}].{attr}"
            if fk in disagreed_set:
                val, src, qs = pick_scalar_or_obj(fk, m_cur[attr], m_alt[attr])
                m_cur[attr] = str(val)
                if src == "reading_2":
                    m_cur[q_list_attr] = copy.deepcopy(m_alt.get(q_list_attr, []))
                    if not m_cur[q_list_attr] and qs and m_cur[attr] != "not_stated":
                        m_cur[q_list_attr] = qs
                elif src == "reading_1":
                    if not m_cur.get(q_list_attr) and qs and m_cur[attr] != "not_stated":
                        m_cur[q_list_attr] = qs
                elif src == "custom_verbatim":
                    m_cur[q_list_attr] = qs if m_cur[attr] != "not_stated" else []
                else:
                    m_cur[q_list_attr] = []
                applied_log[fk] = {"choice": src, "value": m_cur[attr], "quotes": qs}

        for attr in ("unit_class", "time_point_weeks", "population_class", "population_defined", "parameter_type", "dispersion_type"):
            fk = f"measure[{mi}].{attr}"
            if fk in disagreed_set:
                val, src, qs = pick_scalar_or_obj(fk, m_cur[attr], m_alt[attr])
                m_cur[attr] = str(val)
                applied_log[fk] = {"choice": src, "value": m_cur[attr], "quotes": qs}

        if f"measure[{mi}].arm_groups" in disagreed_set:
            fk = f"measure[{mi}].arm_groups"
            dec = adjudication_map.get(fk)
            if dec and dec["choice"] == "reading_2":
                m_cur["arm_groups"] = copy.deepcopy(m_alt["arm_groups"])
                applied_log[fk] = {"choice": "reading_2", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "reading_1":
                applied_log[fk] = {"choice": "reading_1", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "custom_verbatim":
                val, src, qs = pick_scalar_or_obj(fk, None, None)
                if isinstance(val, list):
                    rebuilt = []
                    for idx_g, g in enumerate(val):
                        q_path = g.get(
                            "group_title_quote_path",
                            f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].groups[{idx_g}].title",
                        )
                        rebuilt.append({
                            "group_id": g["group_id"],
                            "group_title": g["group_title"],
                            "group_title_quote_text": g.get("group_title_quote_text", g["group_title"]),
                            "group_title_quote_path": q_path,
                            "number_analysed": g.get("number_analysed", "not_stated"),
                            "number_analysed_unit": g.get("number_analysed_unit", "not_stated"),
                        })
                    m_cur["arm_groups"] = rebuilt
                applied_log[fk] = {"choice": src, "quotes": qs}
            else:
                unresolved_keys.append(fk)
                applied_log[fk] = {"choice": "unresolved"}

        if f"measure[{mi}].arm_measurements" in disagreed_set:
            fk = f"measure[{mi}].arm_measurements"
            dec = adjudication_map.get(fk)
            if dec and dec["choice"] == "reading_2":
                m_cur["arm_measurements"] = copy.deepcopy(m_alt["arm_measurements"])
                applied_log[fk] = {"choice": "reading_2", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "reading_1":
                applied_log[fk] = {"choice": "reading_1", "quotes": dec.get("deciding_quotes", [])}
            elif dec and dec["choice"] == "custom_verbatim":
                val, src, qs = pick_scalar_or_obj(fk, None, None)
                if isinstance(val, list):
                    m_cur["arm_measurements"] = val
                applied_log[fk] = {"choice": src, "quotes": qs}
            else:
                unresolved_keys.append(fk)
                applied_log[fk] = {"choice": "unresolved"}

        # Analyses on measure mi
        a1_map = {a["analysis_index"]: a for a in m_cur.get("analyses", [])}
        a2_map = {a["analysis_index"]: a for a in m_alt.get("analyses", [])}
        target_a_indices = sorted(a1_map.keys())
        if f"measure[{mi}].analysis_indices" in disagreed_set:
            val, src, qs = pick_scalar_or_obj(
                f"measure[{mi}].analysis_indices",
                sorted(a1_map.keys()),
                sorted(a2_map.keys()),
            )
            if isinstance(val, list):
                target_a_indices = [int(x) for x in val]
            applied_log[f"measure[{mi}].analysis_indices"] = {"choice": src, "value": val, "quotes": qs}

        merged_analyses: list[dict[str, Any]] = []
        for ai in target_a_indices:
            inc_afk = f"measure[{mi}].analysis[{ai}].included"
            if inc_afk in disagreed_set:
                aval, asrc, aqs = pick_scalar_or_obj(inc_afk, ai in a1_map, ai in a2_map)
                applied_log[inc_afk] = {"choice": asrc, "value": aval, "quotes": aqs}
                if aval is False:
                    continue

            if ai in a1_map and ai in a2_map:
                an_cur = copy.deepcopy(a1_map[ai])
                an_alt = a2_map[ai]
            elif ai in a1_map:
                merged_analyses.append(copy.deepcopy(a1_map[ai]))
                continue
            elif ai in a2_map:
                merged_analyses.append(copy.deepcopy(a2_map[ai]))
                continue
            else:
                continue

            if f"measure[{mi}].analysis[{ai}].groups_compared" in disagreed_set:
                fk = f"measure[{mi}].analysis[{ai}].groups_compared"
                val, src, qs = pick_scalar_or_obj(fk, an_cur["groups_compared"], an_alt["groups_compared"])
                an_cur["groups_compared"] = val if isinstance(val, list) else [str(val)]
                applied_log[fk] = {"choice": src, "value": val, "quotes": qs}

            an_quote_fields = [
                ("estimate_type_as_posted", "estimate_type_quote_text", "estimate_type_quote_path"),
                ("non_inferiority_margin_as_posted", "non_inferiority_margin_quote_text", "non_inferiority_margin_quote_path"),
                ("statistical_method_as_posted", "statistical_method_quote_text", "statistical_method_quote_path"),
            ]
            for attr, qt_attr, qp_attr in an_quote_fields:
                fk = f"measure[{mi}].analysis[{ai}].{attr}"
                if fk in disagreed_set:
                    val, src, qs = pick_scalar_or_obj(fk, an_cur[attr], an_alt[attr])
                    an_cur[attr] = str(val)
                    if src == "reading_2":
                        an_cur[qt_attr] = an_alt[qt_attr]
                        an_cur[qp_attr] = an_alt[qp_attr]
                    elif src == "custom_verbatim":
                        if qs and an_cur[attr] != "not_stated":
                            an_cur[qt_attr] = qs[0]["text"]
                            an_cur[qp_attr] = qs[0]["path"]
                        else:
                            an_cur[qt_attr] = ""
                            an_cur[qp_attr] = ""
                    elif src == "unresolved":
                        an_cur[qt_attr] = ""
                        an_cur[qp_attr] = ""
                    applied_log[fk] = {"choice": src, "value": an_cur[attr], "quotes": qs}

            for attr in ("estimate", "ci_percent", "ci_lower", "ci_upper", "ci_sides", "p_value_as_posted", "hypothesis_class"):
                fk = f"measure[{mi}].analysis[{ai}].{attr}"
                if fk in disagreed_set:
                    val, src, qs = pick_scalar_or_obj(fk, an_cur[attr], an_alt[attr])
                    an_cur[attr] = str(val)
                    if src == "reading_2" and attr == "hypothesis_class":
                        an_cur["non_inferiority_type_raw"] = an_alt.get("non_inferiority_type_raw", an_cur.get("non_inferiority_type_raw", "not_stated"))
                    applied_log[fk] = {"choice": src, "value": an_cur[attr], "quotes": qs}

            merged_analyses.append(an_cur)

        m_cur["analyses"] = merged_analyses
        merged_measures.append(m_cur)

    final["measures"] = merged_measures
    return {
        "sheet": final,
        "unresolved_field_keys": sorted(set(unresolved_keys)),
        "adjudicated_decisions_applied": applied_log,
    }


def verify_trial_quotes(sheet: dict[str, Any], raw_record: dict[str, Any]) -> dict[str, Any]:
    """Deterministically verify every [quote] string in `sheet` against `raw_record` at its stated JSON path.

    Never repairs anything. Returns per-measure and trial-level verification results.
    """
    trial_failures: list[dict[str, Any]] = []
    trial_checked = 0

    def check_one(container: list[dict[str, Any]], label: str, text: str, path: str) -> int:
        if not text or not path:
            container.append({"field": label, "text": text, "path": path, "reason": "missing_quote_or_path"})
            return 1
        ok, resolved, err = resolve_json_path(raw_record, path)
        if not ok:
            container.append({"field": label, "text": text, "path": path, "reason": err})
            return 1
        resolved_str = str(resolved)
        if text not in resolved_str:
            container.append({
                "field": label,
                "text": text,
                "path": path,
                "reason": "quote_not_verbatim_substring_at_path",
                "actual_at_path_preview": resolved_str[:200],
            })
            return 1
        return 1

    if sheet.get("hba1c_primary_measure_posted") == "yes":
        for dm in sheet.get("baseline_demographic_measures", []):
            dom = dm.get("domain")
            bmi = dm.get("baseline_measure_index")
            trial_checked += check_one(
                trial_failures,
                f"baseline.{dom}[{bmi}].measure_title",
                dm.get("measure_title_quote_text", ""),
                dm.get("measure_title_quote_path", ""),
            )

    per_measure: dict[int, dict[str, Any]] = {}
    for m in sheet.get("measures", []):
        mi = m["measure_index"]
        m_failures: list[dict[str, Any]] = []
        m_checked = 0

        # 1. measure_title [quote]
        if m["measure_title"] not in ("not_stated", "unresolved"):
            m_checked += check_one(
                m_failures,
                f"measure[{mi}].measure_title",
                m.get("measure_title_quote_text", ""),
                m.get("measure_title_quote_path", ""),
            )

        # 2. construct [quote]
        if m["construct"] not in ("not_stated", "unresolved"):
            qs = m.get("construct_quotes", [])
            if not qs:
                m_failures.append({"field": f"measure[{mi}].construct", "text": "", "path": "", "reason": "empty_quotes_list"})
                m_checked += 1
            for q in qs:
                m_checked += check_one(m_failures, f"measure[{mi}].construct", q.get("text", ""), q.get("path", ""))

        # 3. unit_as_posted [quote]
        if m["unit_as_posted"] not in ("not_stated", "unresolved"):
            m_checked += check_one(
                m_failures,
                f"measure[{mi}].unit_as_posted",
                m.get("unit_as_posted_quote_text", ""),
                m.get("unit_as_posted_quote_path", ""),
            )

        # 4. time_frame_as_posted [quote]
        if m["time_frame_as_posted"] not in ("not_stated", "unresolved"):
            m_checked += check_one(
                m_failures,
                f"measure[{mi}].time_frame_as_posted",
                m.get("time_frame_as_posted_quote_text", ""),
                m.get("time_frame_as_posted_quote_path", ""),
            )

        # 5. population_description_as_posted [quote]
        if m["population_description_as_posted"] not in ("not_stated", "unresolved"):
            m_checked += check_one(
                m_failures,
                f"measure[{mi}].population_description_as_posted",
                m.get("population_description_quote_text", ""),
                m.get("population_description_quote_path", ""),
            )

        # 6. missing_data_method_as_posted [quote]
        if m["missing_data_method_as_posted"] not in ("not_stated", "unresolved"):
            qs = m.get("missing_data_method_quotes", [])
            if not qs:
                m_failures.append({"field": f"measure[{mi}].missing_data_method_as_posted", "text": "", "path": "", "reason": "empty_quotes_list"})
                m_checked += 1
            for q in qs:
                m_checked += check_one(m_failures, f"measure[{mi}].missing_data_method_as_posted", q.get("text", ""), q.get("path", ""))

        # 7. data_after_rescue_therapy [quote]
        if m["data_after_rescue_therapy"] not in ("not_stated", "unresolved"):
            qs = m.get("data_after_rescue_therapy_quotes", [])
            if not qs:
                m_failures.append({"field": f"measure[{mi}].data_after_rescue_therapy", "text": "", "path": "", "reason": "empty_quotes_list"})
                m_checked += 1
            for q in qs:
                m_checked += check_one(m_failures, f"measure[{mi}].data_after_rescue_therapy", q.get("text", ""), q.get("path", ""))

        # 8. arm_groups[].group_title [quote]
        for g in m.get("arm_groups", []):
            gid = g.get("group_id", "")
            m_checked += check_one(
                m_failures,
                f"measure[{mi}].arm_group[{gid}].group_title",
                g.get("group_title_quote_text", ""),
                g.get("group_title_quote_path", ""),
            )

        # 9. comparator_class [quote] (required whenever arm_groups is non-empty and not unresolved)
        if m.get("arm_groups") and m["comparator_class"] != "unresolved":
            qs = m.get("comparator_class_quotes", [])
            if not qs:
                m_failures.append({"field": f"measure[{mi}].comparator_class", "text": "", "path": "", "reason": "empty_quotes_list"})
                m_checked += 1
            for q in qs:
                m_checked += check_one(m_failures, f"measure[{mi}].comparator_class", q.get("text", ""), q.get("path", ""))

        # 10. background_therapy_as_posted [quote]
        if m["background_therapy_as_posted"] not in ("not_stated", "unresolved"):
            qs = m.get("background_therapy_quotes", [])
            if not qs:
                m_failures.append({"field": f"measure[{mi}].background_therapy_as_posted", "text": "", "path": "", "reason": "empty_quotes_list"})
                m_checked += 1
            for q in qs:
                m_checked += check_one(m_failures, f"measure[{mi}].background_therapy_as_posted", q.get("text", ""), q.get("path", ""))

        # 11-13. Analysis quotes
        an_statuses: dict[int, dict[str, Any]] = {}
        for an in m.get("analyses", []):
            ai = an["analysis_index"]
            a_failures: list[dict[str, Any]] = []
            a_checked = 0
            if an["estimate_type_as_posted"] not in ("not_stated", "unresolved"):
                a_checked += check_one(
                    a_failures,
                    f"measure[{mi}].analysis[{ai}].estimate_type_as_posted",
                    an.get("estimate_type_quote_text", ""),
                    an.get("estimate_type_quote_path", ""),
                )
            if an["non_inferiority_margin_as_posted"] not in ("not_stated", "unresolved"):
                a_checked += check_one(
                    a_failures,
                    f"measure[{mi}].analysis[{ai}].non_inferiority_margin_as_posted",
                    an.get("non_inferiority_margin_quote_text", ""),
                    an.get("non_inferiority_margin_quote_path", ""),
                )
            if an["statistical_method_as_posted"] not in ("not_stated", "unresolved"):
                a_checked += check_one(
                    a_failures,
                    f"measure[{mi}].analysis[{ai}].statistical_method_as_posted",
                    an.get("statistical_method_quote_text", ""),
                    an.get("statistical_method_quote_path", ""),
                )
            m_checked += a_checked
            m_failures.extend(a_failures)
            an_statuses[ai] = {
                "status": "evidence_not_found" if a_failures else "verified",
                "checked_count": a_checked,
                "failures": a_failures,
            }

        combined_row_failures = trial_failures + m_failures
        per_measure[mi] = {
            "status": "evidence_not_found" if combined_row_failures else "verified",
            "checked_quotes_count": trial_checked + m_checked,
            "failures": combined_row_failures,
            "measure_only_failures": m_failures,
            "analysis_statuses": an_statuses,
        }

    return {
        "trial_status": "evidence_not_found" if trial_failures else "verified",
        "trial_checked_quotes_count": trial_checked,
        "trial_failures": trial_failures,
        "per_measure": per_measure,
    }
