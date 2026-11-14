#!/usr/bin/env python3
"""Offline deterministic generator for all deliverables from stored records and raw worker outputs.

Reads ONLY local files under docs/audits/hba1c_cohort_reading/:
  - records/*.json.gz
  - retrieval_log.csv & api_version.json
  - intermediate/raw_dual_readings.jsonl
  - intermediate/raw_adjudications.jsonl

Writes:
  - readings.jsonl
  - cohort_sheet.csv
  - analyses.csv
  - baseline_tables.csv
  - summary.md
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPTS_DIR.parent
RECORDS_DIR = BASE_DIR / "records"
INTERMEDIATE_DIR = BASE_DIR / "intermediate"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compare_and_verify as cv  # noqa: E402
import fetch_records as fr  # noqa: E402
import schema_and_prompts as sp  # noqa: E402


def load_jsonl_latest(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            nct = obj.get("nct_id")
            if nct:
                # Prefer non-error record over error record if retried
                if nct not in out or not obj.get("error"):
                    out[nct] = obj
    return out


def format_dispersion_display(spread: str, lower: str, upper: str) -> str:
    has_spread = spread not in ("", "not_stated", "unresolved")
    has_limits = lower not in ("", "not_stated", "unresolved") or upper not in ("", "not_stated", "unresolved")
    if has_spread and has_limits:
        return f"{spread} [{lower}, {upper}]"
    if has_spread:
        return spread
    if has_limits:
        return f"[{lower}, {upper}]"
    return "not_stated"


def is_fixed_week_in_24_to_26(val: str) -> bool:
    if not val or val in ("not_one_fixed_time_point", "not_stated", "unresolved"):
        return False
    try:
        num = float(val)
    except ValueError:
        return False
    return 24.0 <= num <= 26.0


def fmt_nct_list(ncts: list[str]) -> str:
    unique_ordered = sorted(set(ncts))
    if not unique_ordered:
        return "none"
    return ", ".join(unique_ordered)


def main() -> int:
    # 1. Load retrieval log and verify stored gzipped records offline
    retrieval_rows: dict[str, dict[str, str]] = {}
    with (BASE_DIR / "retrieval_log.csv").open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            retrieval_rows[row["nct_id"]] = row

    api_version_meta = json.loads((BASE_DIR / "api_version.json").read_text(encoding="utf-8"))
    dual_map = load_jsonl_latest(INTERMEDIATE_DIR / "raw_dual_readings.jsonl")
    adj_map = load_jsonl_latest(INTERMEDIATE_DIR / "raw_adjudications.jsonl")

    retrieval_failures: list[tuple[str, str]] = []
    parsing_failures: list[tuple[str, str]] = []
    missing_readings: list[tuple[str, str]] = []

    readings_jsonl_rows: list[dict[str, Any]] = []
    cohort_csv_rows: list[dict[str, Any]] = []
    analyses_csv_rows: list[dict[str, Any]] = []
    baseline_csv_rows: list[dict[str, Any]] = []

    # Agreement statistics: category -> {"compared": int, "agreed": int, "disagreed": int, "disagreed_ncts": set}
    agreement_by_cat: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"compared": 0, "agreed": 0, "disagreed": 0, "disagreed_ncts": set()}
    )
    all_disagreement_instances: list[dict[str, Any]] = []
    all_unresolved_instances: list[dict[str, Any]] = []
    quote_failed_rows: list[dict[str, Any]] = []

    # Processed trial objects for summary counts
    adjudicated_trials: dict[str, dict[str, Any]] = {}

    for nct_id in fr.NCT_IDS:
        rlog = retrieval_rows.get(nct_id)
        gz_path = RECORDS_DIR / f"{nct_id}.json.gz"
        if not rlog or str(rlog.get("http_status")) != "200" or not gz_path.exists():
            reason = (rlog.get("error") if rlog else "missing_from_retrieval_log") or "non_200_or_missing_gz"
            retrieval_failures.append((nct_id, reason))
            cohort_csv_rows.append({"nct_id": nct_id, "hba1c_primary_measure_posted": "not_retrieved"})
            readings_jsonl_rows.append({"nct_id": nct_id, "measure_index": None, "status": "not_retrieved", "reason": reason})
            continue

        # Verify SHA-256 of stored gzipped response body
        raw_bytes = gzip.decompress(gz_path.read_bytes())
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha != rlog["sha256"]:
            parsing_failures.append((nct_id, f"sha256_mismatch: expected {rlog['sha256']}, got {actual_sha}"))
            cohort_csv_rows.append({"nct_id": nct_id, "hba1c_primary_measure_posted": "could_not_be_parsed"})
            continue

        try:
            raw_record = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            reason = f"JSONDecodeError: {exc}"
            parsing_failures.append((nct_id, reason))
            cohort_csv_rows.append({"nct_id": nct_id, "hba1c_primary_measure_posted": "could_not_be_parsed"})
            readings_jsonl_rows.append({"nct_id": nct_id, "measure_index": None, "status": "could_not_be_parsed", "reason": reason})
            continue

        pair = dual_map.get(nct_id)
        if not pair or pair.get("error") or not pair.get("r1") or not pair.get("r2"):
            reason = (pair.get("error") if pair else "not_yet_read") or "missing_reader_output"
            missing_readings.append((nct_id, reason))
            parsing_failures.append((nct_id, f"reader_failure: {reason}"))
            cohort_csv_rows.append({"nct_id": nct_id, "hba1c_primary_measure_posted": "could_not_be_parsed"})
            readings_jsonl_rows.append({"nct_id": nct_id, "measure_index": None, "status": "could_not_be_parsed", "reason": reason})
            continue

        r1 = pair["r1"]
        r2 = pair["r2"]
        cmp_res = cv.compare_trial_readings(r1, r2)

        for f_entry in cmp_res["fields"]:
            cat = f_entry["category"]
            agreement_by_cat[cat]["compared"] += 1
            if f_entry["agreed"]:
                agreement_by_cat[cat]["agreed"] += 1
            else:
                agreement_by_cat[cat]["disagreed"] += 1
                agreement_by_cat[cat]["disagreed_ncts"].add(nct_id)
                all_disagreement_instances.append({"nct_id": nct_id, **f_entry})

        adj_entry = adj_map.get(nct_id)
        adj_raw = adj_entry.get("adjudication_raw") if (cmp_res["fields_disagreed_count"] > 0 and adj_entry and not adj_entry.get("error")) else None

        merged = cv.apply_adjudication(r1, r2, cmp_res, adj_raw)
        sheet = merged["sheet"]
        unresolved_keys = merged["unresolved_field_keys"]
        for uk in unresolved_keys:
            orig_f = next((x for x in cmp_res["fields"] if x["field_key"] == uk), {})
            adj_dec = next(
                (d for d in (adj_raw.get("decisions", []) if adj_raw else []) if d.get("field_key") == uk),
                None,
            )
            all_unresolved_instances.append({
                "nct_id": nct_id,
                "field_key": uk,
                "r1": orig_f.get("r1"),
                "r2": orig_f.get("r2"),
                "adjudicator_rationale": adj_dec.get("rationale", "adjudicator_not_available") if adj_dec else "no_adjudicator_decision",
            })

        v_res = cv.verify_trial_quotes(sheet, raw_record)
        adjudicated_trials[nct_id] = {
            "sheet": sheet,
            "comparison": cmp_res,
            "unresolved_keys": unresolved_keys,
            "verification": v_res,
        }

        # Trial-level baseline group title map from raw_record (strictly for total_group_title label lookup if needed, or keep exact)
        bg_title_map = {
            g.get("id", ""): g.get("title", "")
            for g in raw_record.get("resultsSection", {}).get("baselineCharacteristicsModule", {}).get("groups", [])
        }

        # Trial-level disagreements & unresolved counts
        trial_disagreed_fields = [f for f in cmp_res["disagreements"] if f["scope"] == "trial"]
        trial_unresolved_fields = [k for k in unresolved_keys if k.startswith("trial.")]

        if sheet.get("hba1c_primary_measure_posted") != "yes" or not sheet.get("measures"):
            row_disagreed = cmp_res["fields_disagreed_count"]
            row_unresolved = len(unresolved_keys)
            q_status = v_res["trial_status"] if sheet.get("hba1c_primary_measure_posted") == "yes" else "not_applicable"
            if q_status == "evidence_not_found":
                quote_failed_rows.append({
                    "nct_id": nct_id,
                    "measure_index": None,
                    "failures": v_res["trial_failures"],
                })
            readings_jsonl_rows.append({
                "nct_id": nct_id,
                "measure_index": None,
                "hba1c_primary_measure_posted": sheet.get("hba1c_primary_measure_posted"),
                "first_reading": r1,
                "second_reading": r2,
                "field_comparison": cmp_res,
                "adjudication_raw": adj_raw,
                "adjudicated": sheet,
                "adjudicated_decisions_applied": merged["adjudicated_decisions_applied"],
                "unresolved_field_keys": unresolved_keys,
                "quote_verification": {
                    "status": q_status,
                    "checked_quotes_count": v_res["trial_checked_quotes_count"],
                    "failures": v_res["trial_failures"],
                },
            })
            cohort_csv_rows.append({
                "nct_id": nct_id,
                "hba1c_primary_measure_posted": sheet.get("hba1c_primary_measure_posted", "no"),
                "measure_index": "",
                "measure_title": "",
                "measure_title_quote_path": "",
                "construct": "",
                "construct_quotes_json": "",
                "unit_as_posted": "",
                "unit_as_posted_quote_path": "",
                "unit_class": "",
                "time_frame_as_posted": "",
                "time_frame_as_posted_quote_path": "",
                "time_point_weeks": "",
                "population_description_as_posted": "",
                "population_description_quote_path": "",
                "population_class": "",
                "population_defined": "",
                "missing_data_method_as_posted": "",
                "missing_data_method_quotes_json": "",
                "data_after_rescue_therapy": "",
                "data_after_rescue_therapy_quotes_json": "",
                "both_rescue_estimands_present": "",
                "parameter_type": "",
                "dispersion_type": "",
                "arms_json": "",
                "comparator_class": "",
                "comparator_class_quotes_json": "",
                "background_therapy_as_posted": "",
                "background_therapy_quotes_json": "",
                "between_arm_analyses_count": "",
                "has_between_arm_estimate_and_both_ci_limits": "",
                "baseline_hba1c_posted": "",
                "baseline_hba1c_json": "",
                "race_table_posted": "",
                "ethnicity_table_posted": "",
                "sex_table_posted": "",
                "age_table_posted": "",
                "race_and_ethnicity_in_one_table": "",
                "race_and_ethnicity_in_one_table_row_labels_json": "",
                "baseline_population_total": "",
                "baseline_population_total_unit": "",
                "participants_started_by_group_json": "",
                "participants_started_sum_across_groups": "",
                "countries_site_counts_json": "",
                "fields_disagreed": row_disagreed,
                "measure_fields_disagreed": 0,
                "trial_fields_disagreed": len(trial_disagreed_fields),
                "fields_unresolved": row_unresolved,
                "quote_verification_status": q_status,
                "quote_verification_failures_json": json.dumps(v_res["trial_failures"], ensure_ascii=False),
            })
            continue

        # Build baseline_tables.csv rows once per trial
        dm_meta_map = {
            (dm["domain"], dm["baseline_measure_index"]): dm
            for dm in sheet.get("baseline_demographic_measures", [])
        }
        domains_present = {dm["domain"] for dm in sheet.get("baseline_demographic_measures", [])}

        for dr in sorted(
            sheet.get("baseline_demographic_rows", []),
            key=lambda x: (x["domain"], x["baseline_measure_index"], x["row_index"]),
        ):
            dom = dr["domain"]
            bmi = dr["baseline_measure_index"]
            dm_meta = dm_meta_map.get((dom, bmi), {})
            ct = dr.get("class_title", "")
            kt = dr.get("category_title", "")
            row_label = kt if kt else ct
            m_q_ok = not any(
                f["field"] == f"baseline.{dom}[{bmi}].measure_title"
                for f in v_res["trial_failures"]
            )
            baseline_csv_rows.append({
                "nct_id": nct_id,
                "domain": dom,
                "baseline_measure_index": bmi,
                "measure_title": dm_meta.get("measure_title", ""),
                "measure_title_quote_text": dm_meta.get("measure_title_quote_text", ""),
                "measure_title_quote_path": dm_meta.get("measure_title_quote_path", ""),
                "param_type": dm_meta.get("param_type", ""),
                "unit_as_posted": dm_meta.get("unit_as_posted", ""),
                "row_index": dr["row_index"],
                "class_index": dr["class_index"],
                "category_index": dr["category_index"],
                "class_title": ct,
                "category_title": kt,
                "row_label_as_posted": row_label,
                "total_group_id": dr["total_group_id"],
                "total_group_title": bg_title_map.get(dr["total_group_id"], ""),
                "count_or_value_in_total_group": dr["total_value"],
                "spread_in_total_group": dr["total_spread"],
                "race_and_ethnicity_in_one_table": sheet.get("race_and_ethnicity_in_one_table", ""),
                "quote_verification_status": "verified" if m_q_ok else "evidence_not_found",
            })

        # Compute arithmetic sum of STARTED across flow groups (while preserving per-group strings)
        pf_groups = sheet.get("participants_started_by_group", [])
        pf_sum_str = ""
        if pf_groups and all(str(g.get("num_subjects", "")).isdigit() for g in pf_groups):
            pf_sum_str = str(sum(int(g["num_subjects"]) for g in pf_groups))

        r1_m_map = {m["measure_index"]: m for m in r1.get("measures", [])}
        r2_m_map = {m["measure_index"]: m for m in r2.get("measures", [])}

        # Emit one row per adjudicated HbA1c primary measure
        for m in sheet.get("measures", []):
            mi = m["measure_index"]
            m_scope = f"measure:{mi}"
            m_disagreed = [f for f in cmp_res["disagreements"] if f["scope"] == m_scope]
            m_unresolved = [k for k in unresolved_keys if k.startswith(f"measure[{mi}].")]
            total_row_disagreed = len(trial_disagreed_fields) + len(m_disagreed)
            total_row_unresolved = len(trial_unresolved_fields) + len(m_unresolved)

            mv = v_res["per_measure"][mi]
            if mv["status"] == "evidence_not_found":
                quote_failed_rows.append({
                    "nct_id": nct_id,
                    "measure_index": mi,
                    "failures": mv["failures"],
                })

            # Assemble structured arms JSON for cohort_sheet.csv
            meas_by_gid: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for am in m.get("arm_measurements", []):
                meas_by_gid[am["group_id"]].append({
                    "class_index": am["class_index"],
                    "category_index": am["category_index"],
                    "class_title": am["class_title"],
                    "category_title": am["category_title"],
                    "row_number_analysed": am["row_number_analysed"],
                    "value": am["value"],
                    "dispersion_value": format_dispersion_display(am["spread"], am["lower_limit"], am["upper_limit"]),
                    "spread": am["spread"],
                    "lower_limit": am["lower_limit"],
                    "upper_limit": am["upper_limit"],
                })

            arms_combined = []
            for ag in m.get("arm_groups", []):
                gid = ag["group_id"]
                arms_combined.append({
                    "group_id": gid,
                    "group_title": ag["group_title"],
                    "group_title_quote_path": ag["group_title_quote_path"],
                    "number_analysed": ag["number_analysed"],
                    "number_analysed_unit": ag["number_analysed_unit"],
                    "rows": meas_by_gid.get(gid, []),
                })

            has_est_and_ci = any(
                an.get("estimate") not in ("", "not_stated", "unresolved")
                and an.get("ci_lower") not in ("", "not_stated", "unresolved")
                and an.get("ci_upper") not in ("", "not_stated", "unresolved")
                for an in m.get("analyses", [])
            )

            # Emit analyses.csv rows
            for an in m.get("analyses", []):
                ai = an["analysis_index"]
                an_v = mv["analysis_statuses"].get(ai, {"status": "verified", "failures": []})
                analyses_csv_rows.append({
                    "nct_id": nct_id,
                    "measure_index": mi,
                    "measure_title": m["measure_title"],
                    "analysis_index": ai,
                    "groups_compared": ";".join(an.get("groups_compared", [])),
                    "groups_compared_json": json.dumps(an.get("groups_compared", []), ensure_ascii=False),
                    "estimate_type_as_posted": an["estimate_type_as_posted"],
                    "estimate_type_quote_text": an["estimate_type_quote_text"],
                    "estimate_type_quote_path": an["estimate_type_quote_path"],
                    "estimate": an["estimate"],
                    "ci_percent": an["ci_percent"],
                    "ci_lower": an["ci_lower"],
                    "ci_upper": an["ci_upper"],
                    "ci_sides": an["ci_sides"],
                    "p_value_as_posted": an["p_value_as_posted"],
                    "hypothesis_class": an["hypothesis_class"],
                    "non_inferiority_type_raw": an.get("non_inferiority_type_raw", "not_stated"),
                    "non_inferiority_margin_as_posted": an["non_inferiority_margin_as_posted"],
                    "non_inferiority_margin_quote_text": an["non_inferiority_margin_quote_text"],
                    "non_inferiority_margin_quote_path": an["non_inferiority_margin_quote_path"],
                    "statistical_method_as_posted": an["statistical_method_as_posted"],
                    "statistical_method_quote_text": an["statistical_method_quote_text"],
                    "statistical_method_quote_path": an["statistical_method_quote_path"],
                    "quote_verification_status": an_v["status"],
                    "quote_verification_failures_json": json.dumps(an_v["failures"], ensure_ascii=False),
                })

            # Emit readings.jsonl row (keeping full untouched first & second readings!)
            row_cmp_fields = [f for f in cmp_res["fields"] if f["scope"] in ("trial", m_scope)]
            readings_jsonl_rows.append({
                "nct_id": nct_id,
                "measure_index": mi,
                "hba1c_primary_measure_posted": "yes",
                "first_reading": {
                    "trial_level": {k: v for k, v in r1.items() if k != "measures"},
                    "measure": r1_m_map.get(mi),
                },
                "second_reading": {
                    "trial_level": {k: v for k, v in r2.items() if k != "measures"},
                    "measure": r2_m_map.get(mi),
                },
                "field_comparison": {
                    "fields_compared_count": len(row_cmp_fields),
                    "fields_disagreed_count": total_row_disagreed,
                    "measure_fields_disagreed_count": len(m_disagreed),
                    "trial_fields_disagreed_count": len(trial_disagreed_fields),
                    "fields": row_cmp_fields,
                },
                "adjudication_raw": adj_raw,
                "adjudicated": {
                    "trial_level": {k: v for k, v in sheet.items() if k != "measures"},
                    "measure": m,
                },
                "adjudicated_decisions_applied": merged["adjudicated_decisions_applied"],
                "unresolved_field_keys": trial_unresolved_fields + m_unresolved,
                "quote_verification": mv,
            })

            cohort_csv_rows.append({
                "nct_id": nct_id,
                "hba1c_primary_measure_posted": "yes",
                "measure_index": mi,
                "measure_title": m["measure_title"],
                "measure_title_quote_path": m["measure_title_quote_path"],
                "construct": m["construct"],
                "construct_quotes_json": json.dumps(m["construct_quotes"], ensure_ascii=False),
                "unit_as_posted": m["unit_as_posted"],
                "unit_as_posted_quote_path": m["unit_as_posted_quote_path"],
                "unit_class": m["unit_class"],
                "time_frame_as_posted": m["time_frame_as_posted"],
                "time_frame_as_posted_quote_path": m["time_frame_as_posted_quote_path"],
                "time_point_weeks": m["time_point_weeks"],
                "population_description_as_posted": m["population_description_as_posted"],
                "population_description_quote_path": m["population_description_quote_path"],
                "population_class": m["population_class"],
                "population_defined": m["population_defined"],
                "missing_data_method_as_posted": m["missing_data_method_as_posted"],
                "missing_data_method_quotes_json": json.dumps(m["missing_data_method_quotes"], ensure_ascii=False),
                "data_after_rescue_therapy": m["data_after_rescue_therapy"],
                "data_after_rescue_therapy_quotes_json": json.dumps(m["data_after_rescue_therapy_quotes"], ensure_ascii=False),
                "both_rescue_estimands_present": "yes" if m.get("both_rescue_estimands_present") else "no",
                "parameter_type": m["parameter_type"],
                "dispersion_type": m["dispersion_type"],
                "arms_json": json.dumps(arms_combined, ensure_ascii=False),
                "comparator_class": m["comparator_class"],
                "comparator_class_quotes_json": json.dumps(m["comparator_class_quotes"], ensure_ascii=False),
                "background_therapy_as_posted": m["background_therapy_as_posted"],
                "background_therapy_quotes_json": json.dumps(m["background_therapy_quotes"], ensure_ascii=False),
                "between_arm_analyses_count": len(m.get("analyses", [])),
                "has_between_arm_estimate_and_both_ci_limits": "yes" if has_est_and_ci else "no",
                "baseline_hba1c_posted": sheet["baseline_hba1c_posted"],
                "baseline_hba1c_json": json.dumps(sheet.get("baseline_hba1c_rows", []), ensure_ascii=False),
                "race_table_posted": "yes" if "race" in domains_present else "no",
                "ethnicity_table_posted": "yes" if "ethnicity" in domains_present else "no",
                "sex_table_posted": "yes" if "sex" in domains_present else "no",
                "age_table_posted": "yes" if "age" in domains_present else "no",
                "race_and_ethnicity_in_one_table": sheet["race_and_ethnicity_in_one_table"],
                "race_and_ethnicity_in_one_table_row_labels_json": json.dumps(
                    sheet.get("race_and_ethnicity_in_one_table_row_labels", []), ensure_ascii=False
                ),
                "baseline_population_total": sheet["baseline_population_total"],
                "baseline_population_total_unit": sheet["baseline_population_total_unit"],
                "participants_started_by_group_json": json.dumps(pf_groups, ensure_ascii=False),
                "participants_started_sum_across_groups": pf_sum_str,
                "countries_site_counts_json": json.dumps(sheet.get("countries", []), ensure_ascii=False),
                "fields_disagreed": total_row_disagreed,
                "measure_fields_disagreed": len(m_disagreed),
                "trial_fields_disagreed": len(trial_disagreed_fields),
                "fields_unresolved": total_row_unresolved,
                "quote_verification_status": mv["status"],
                "quote_verification_failures_json": json.dumps(mv["failures"], ensure_ascii=False),
            })

        # Also preserve any candidate measure included by one reader and rejected by adjudication
        adj_measure_set = {m["measure_index"] for m in sheet.get("measures", [])}
        for cand_mi in sorted((set(r1_m_map.keys()) | set(r2_m_map.keys())) - adj_measure_set):
            readings_jsonl_rows.append({
                "nct_id": nct_id,
                "measure_index": cand_mi,
                "hba1c_primary_measure_posted": "adjudicated_not_hba1c_primary",
                "first_reading": r1_m_map.get(cand_mi),
                "second_reading": r2_m_map.get(cand_mi),
                "field_comparison": [f for f in cmp_res["fields"] if f["scope"] == f"measure:{cand_mi}"],
                "adjudication_raw": adj_raw,
                "adjudicated": None,
                "quote_verification": {"status": "not_applicable", "checked_quotes_count": 0, "failures": []},
            })

    # Write readings.jsonl
    with (BASE_DIR / "readings.jsonl").open("w", encoding="utf-8") as fp:
        for r in readings_jsonl_rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Write cohort_sheet.csv
    cohort_cols = [
        "nct_id",
        "hba1c_primary_measure_posted",
        "measure_index",
        "measure_title",
        "measure_title_quote_path",
        "construct",
        "construct_quotes_json",
        "unit_as_posted",
        "unit_as_posted_quote_path",
        "unit_class",
        "time_frame_as_posted",
        "time_frame_as_posted_quote_path",
        "time_point_weeks",
        "population_description_as_posted",
        "population_description_quote_path",
        "population_class",
        "population_defined",
        "missing_data_method_as_posted",
        "missing_data_method_quotes_json",
        "data_after_rescue_therapy",
        "data_after_rescue_therapy_quotes_json",
        "both_rescue_estimands_present",
        "parameter_type",
        "dispersion_type",
        "arms_json",
        "comparator_class",
        "comparator_class_quotes_json",
        "background_therapy_as_posted",
        "background_therapy_quotes_json",
        "between_arm_analyses_count",
        "has_between_arm_estimate_and_both_ci_limits",
        "baseline_hba1c_posted",
        "baseline_hba1c_json",
        "race_table_posted",
        "ethnicity_table_posted",
        "sex_table_posted",
        "age_table_posted",
        "race_and_ethnicity_in_one_table",
        "race_and_ethnicity_in_one_table_row_labels_json",
        "baseline_population_total",
        "baseline_population_total_unit",
        "participants_started_by_group_json",
        "participants_started_sum_across_groups",
        "countries_site_counts_json",
        "fields_disagreed",
        "measure_fields_disagreed",
        "trial_fields_disagreed",
        "fields_unresolved",
        "quote_verification_status",
        "quote_verification_failures_json",
    ]
    with (BASE_DIR / "cohort_sheet.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=cohort_cols, extrasaction="ignore")
        writer.writeheader()
        for row in cohort_csv_rows:
            writer.writerow({k: row.get(k, "") for k in cohort_cols})

    # Write analyses.csv
    analyses_cols = [
        "nct_id",
        "measure_index",
        "measure_title",
        "analysis_index",
        "groups_compared",
        "groups_compared_json",
        "estimate_type_as_posted",
        "estimate_type_quote_text",
        "estimate_type_quote_path",
        "estimate",
        "ci_percent",
        "ci_lower",
        "ci_upper",
        "ci_sides",
        "p_value_as_posted",
        "hypothesis_class",
        "non_inferiority_type_raw",
        "non_inferiority_margin_as_posted",
        "non_inferiority_margin_quote_text",
        "non_inferiority_margin_quote_path",
        "statistical_method_as_posted",
        "statistical_method_quote_text",
        "statistical_method_quote_path",
        "quote_verification_status",
        "quote_verification_failures_json",
    ]
    with (BASE_DIR / "analyses.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=analyses_cols)
        writer.writeheader()
        for row in analyses_csv_rows:
            writer.writerow(row)

    # Write baseline_tables.csv
    baseline_cols = [
        "nct_id",
        "domain",
        "baseline_measure_index",
        "measure_title",
        "measure_title_quote_text",
        "measure_title_quote_path",
        "param_type",
        "unit_as_posted",
        "row_index",
        "class_index",
        "category_index",
        "class_title",
        "category_title",
        "row_label_as_posted",
        "total_group_id",
        "total_group_title",
        "count_or_value_in_total_group",
        "spread_in_total_group",
        "race_and_ethnicity_in_one_table",
        "quote_verification_status",
    ]
    with (BASE_DIR / "baseline_tables.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=baseline_cols)
        writer.writeheader()
        for row in baseline_csv_rows:
            writer.writerow(row)

    # Build summary.md
    summary_text = build_summary_markdown(
        api_version_meta=api_version_meta,
        adjudicated_trials=adjudicated_trials,
        retrieval_failures=retrieval_failures,
        parsing_failures=parsing_failures,
        missing_readings=missing_readings,
        agreement_by_cat=agreement_by_cat,
        all_disagreement_instances=all_disagreement_instances,
        all_unresolved_instances=all_unresolved_instances,
        quote_failed_rows=quote_failed_rows,
        cohort_csv_rows=cohort_csv_rows,
        analyses_csv_rows=analyses_csv_rows,
    )
    (BASE_DIR / "summary.md").write_text(summary_text, encoding="utf-8")
    print(
        f"Generated deliverables: trials_adjudicated={len(adjudicated_trials)}, "
        f"cohort_rows={len(cohort_csv_rows)}, analyses={len(analyses_csv_rows)}, "
        f"baseline_rows={len(baseline_csv_rows)}, quote_failures={len(quote_failed_rows)}"
    )
    return 0


def build_summary_markdown(
    *,
    api_version_meta: dict[str, Any],
    adjudicated_trials: dict[str, dict[str, Any]],
    retrieval_failures: list[tuple[str, str]],
    parsing_failures: list[tuple[str, str]],
    missing_readings: list[tuple[str, str]],
    agreement_by_cat: dict[str, dict[str, Any]],
    all_disagreement_instances: list[dict[str, Any]],
    all_unresolved_instances: list[dict[str, Any]],
    quote_failed_rows: list[dict[str, Any]],
    cohort_csv_rows: list[dict[str, Any]],
    analyses_csv_rows: list[dict[str, Any]],
) -> str:
    total_assigned = len(fr.NCT_IDS)
    n_retrieved = total_assigned - len(retrieval_failures)
    n_parsed_and_read = len(adjudicated_trials)

    trials_with_hba1c = [
        nct for nct in fr.NCT_IDS
        if nct in adjudicated_trials
        and adjudicated_trials[nct]["sheet"].get("hba1c_primary_measure_posted") == "yes"
        and adjudicated_trials[nct]["sheet"].get("measures")
    ]
    trials_no_hba1c = [
        nct for nct in fr.NCT_IDS
        if nct in adjudicated_trials
        and adjudicated_trials[nct]["sheet"].get("hba1c_primary_measure_posted") == "no"
    ]
    trials_unresolved_hba1c = [
        nct for nct in fr.NCT_IDS
        if nct in adjudicated_trials
        and adjudicated_trials[nct]["sheet"].get("hba1c_primary_measure_posted") == "unresolved"
    ]

    # Collect all adjudicated HbA1c primary measures: list of (nct_id, measure_dict)
    all_measures: list[tuple[str, dict[str, Any]]] = []
    dual_rescue_ncts: list[str] = []
    for nct in trials_with_hba1c:
        for m in adjudicated_trials[nct]["sheet"].get("measures", []):
            all_measures.append((nct, m))
            if m.get("both_rescue_estimands_present"):
                dual_rescue_ncts.append(nct)

    M_DENOM = len(all_measures)
    A_DENOM = len(analyses_csv_rows)

    def group_measures_by(attr: str) -> list[tuple[str, int, list[str]]]:
        buckets: dict[str, list[str]] = defaultdict(list)
        counts: dict[str, int] = defaultdict(int)
        for nct, m in all_measures:
            val = str(m.get(attr, "not_stated"))
            counts[val] += 1
            buckets[val].append(nct)
        return sorted(
            [(k, counts[k], sorted(set(buckets[k]))) for k in counts],
            key=lambda x: (-x[1], x[0]),
        )

    out: list[str] = []
    out.append("# Trial-by-Trial Registry Reading Summary: Type 2 Diabetes HbA1c Primary Endpoint Cohort\n")
    out.append("## Scope and Interpretation Notice\n")
    out.append(
        "- **Source**: Public ClinicalTrials.gov REST API v2 (`/api/v2/studies/{NCT_ID}`), "
        f"API version `{api_version_meta.get('apiVersion', '')}`, data timestamp `{api_version_meta.get('dataTimestamp', '')}`, "
        f"version endpoint retrieved at `{api_version_meta.get('retrieved_at_utc', '')}`.\n"
        "- **Reproducibility vs. validity**: Agreement between two independent readings shows that the reading of the stored registry record is reproducible under the literal coding rules. It does **not** show that the registry record itself is accurate or complete, and it does **not** show that the trials are clinically or statistically comparable. Nothing in this output is described as validated, and no cohort definition is recommended.\n"
        "- **Separation of missingness and error states**: Throughout this summary and all CSV/JSONL files, `not_stated` (the stored record omits the information), `not_retrieved` (HTTP retrieval failed), `could_not_be_parsed` (JSON or reader parsing failed), and `unresolved` (readers disagreed and the record text did not decide between them) are kept strictly separate and never merged.\n"
    )

    out.append("## Ambiguities in the Assignment and Literal Choices Applied\n")
    out.append(
        "Where the assignment specification met structural features of ClinicalTrials.gov API v2 that admitted more than one literal interpretation, the most literal non-inferential rule was applied uniformly across all 248 records:\n\n"
        "1. **Posted primary HbA1c measures with zero analysed participants / empty `classes` (early-terminated trials)**: When an entry in `resultsSection.outcomeMeasuresModule.outcomeMeasures` has `type == \"PRIMARY\"` and is about HbA1c, `hba1c_primary_measure_posted` is coded as `yes` even if `denoms` is `0` and `classes`/`analyses` are absent (because the primary outcome measure object is posted in `outcomeMeasuresModule`), with unpopulated fields (`unit_as_posted`, `parameter_type`, `dispersion_type`, etc.) recorded literally as `not_stated`.\n"
        "2. **Multi-row (`classes`) HbA1c primary outcome measures and co-reported estimands**: Some posted primary Change-from-Baseline HbA1c measures include helper rows in `classes` for Baseline HbA1c and/or endpoint Week HbA1c alongside the Change row, or present only Baseline and endpoint rows in `classes` while stating in `title`/`description` that the endpoint is change from baseline; all `arm_measurements` rows are preserved verbatim in `cohort_sheet.csv` and `readings.jsonl`, while `construct` codes the measure's stated primary construct. In addition, when a single `outcomeMeasures[i]` co-reports two observation-period estimands with opposite post-rescue rules (for example `In-trial` including post-rescue values and `On-treatment without rescue medication` excluding post-rescue values), `data_after_rescue_therapy` codes the first-listed observation period (`classes[0]`) and flags `both_rescue_estimands_present = yes` "
        f"(observed in {len(set(dual_rescue_ncts))}/{len(trials_with_hba1c)} trials with a posted HbA1c primary measure: {fmt_nct_list(dual_rescue_ncts)}).\n"
        "3. **Time frames stated in months or days rather than weeks**: Under the non-conversion rule (*\"Do not infer, harmonise, convert, impute or correct anything\"*), `time_point_weeks` is given a numeric value only when `timeFrame` states one fixed post-baseline time point in **weeks** (including baseline-to-week expressions such as `\"Baseline to week 26\"` or `\"Baseline and Week 24\"`). Time frames stated solely in months or days (such as `\"Month 6\"`, `\"6 months\"`, or `\"Day 168\"`) without stating a week number in `timeFrame`, as well as open ranges (`\"Up to 24 weeks\"`) or multiple post-baseline time points, are coded as `not_one_fixed_time_point`.\n"
        "4. **`unit_class` on responder/proportion measures**: `unit_class` is classified strictly from `outcomeMeasures[i].unitOfMeasure`. On responder endpoints (`proportion_reaching_hba1c_target` or composite endpoints), a posted unit such as `\"Percentage of participants\"`, `\"% of subjects\"`, or `\"Participants\"` denotes percentage or count of *participants* rather than `% HbA1c`, and is therefore coded as `other` (never `percent_hba1c`).\n"
        "5. **`population_class` when the record uses `ITT` / `Intent-to-Treat` vs `Full Analysis Set`**: Following *\"Use the record's own term to choose; if it uses a term without defining it, choose the class for that term and set population_defined = no\"*, records naming `Full Analysis Set` / `FAS` are coded `full_analysis_set` (`population_defined = yes` if defined, `no` if undefined). Because the 8 specified `population_class` values do not have a separate `intention_to_treat` label, an undefined `ITT` / `Intent-to-Treat` label maps to `all_randomised` (`population_defined = no`), whereas an `ITT` or `mITT` label accompanied by a stated restriction maps to `all_randomised` (randomised only), `all_randomised_and_treated` (randomised and >=1 dose only), or `modified_intention_to_treat_with_stated_condition` (randomised plus baseline/post-baseline measurement or other stated condition, with `population_defined = yes`).\n"
        "6. **`comparator_class` in multi-dose trials**: Determined strictly from the measure's arm descriptions (`outcomeMeasures[i].groups`, or `protocolSection.armsInterventionsModule.armGroups` only if measure group descriptions are absent): one or more doses of an investigational drug/regimen vs placebo only (with or without identical continuing background therapy) is coded `placebo`; one or more doses of an investigational drug/regimen vs a distinct active drug only is coded `active_drug`; multiple doses/regimens/formulations of the same drug with neither placebo nor a distinct active comparator drug is coded `dose_comparison_of_same_drug`; presence of both a placebo control arm and a distinct active comparator drug arm in the same measure is coded `mixed`; single-arm measures or insufficient arm text are coded `not_determinable`.\n"
        "7. **Legacy ClinicalTrials.gov `nonInferiorityType` buckets and single-ID `groupIds` in `analyses[]`**: ClinicalTrials.gov API v2 stores legacy PRS selections as combined enums (`SUPERIORITY_OR_OTHER`, `NON_INFERIORITY_OR_EQUIVALENCE`). When neither the enum nor any prose in the measure/analysis (`nonInferiorityComment`, `pValueComment`, `groupDescription`, `statisticalComment`, `estimateComment`, `description`) disambiguates between the two halves of a combined enum, `hypothesis_class` is coded `not_stated` rather than guessing `superiority` or `non_inferiority` by default; the raw `nonInferiorityType` string is also preserved alongside `hypothesis_class` in `analyses.csv`. For `analyses[]`, explicit within-single-arm tests (e.g. `Change within treatment group`) are excluded from between-arm analyses, whereas between-arm treatment-difference analyses where the submitter checked only one `groupId` in `groupIds` are retained with `groups_compared` recorded verbatim as posted (never imputing the unchecked comparator ID).\n"
        "8. **Measure-level vs. analysis-level counts for `estimate_type_as_posted` and `hypothesis_class`**: Because a single HbA1c primary outcome measure can post zero, one, or multiple between-arm analyses, counts for `estimate_type_as_posted` and `hypothesis_class` are reported **both** across all posted between-arm analyses (denominator = all posted between-arm analyses) **and** across HbA1c primary measures (denominator = all posted HbA1c primary measures).\n"
        "9. **`participants_started` and `baseline_tables.csv` scope**: None of the 248 ClinicalTrials.gov v2 records posts a `Total` group in `participantFlowModule`; every record posts per-arm `STARTED` counts in `participantFlowModule.periods[0]`. Both the verbatim per-group `STARTED` counts and their arithmetic sum across Period 1 flow groups are reported in `cohort_sheet.csv` without reconciling with `baseline_population_total`. `baseline_tables.csv` contains one row per baseline row label for `race`, `ethnicity`, `sex`, and `age` (including both continuous and categorical age measures where both are posted; when a single baseline measure combines race and ethnicity rows in one table, it is indexed under both `race` and `ethnicity`), while per-group baseline HbA1c values from `baselineCharacteristicsModule` are stored in `cohort_sheet.csv` (`baseline_hba1c_json`) and `readings.jsonl`.\n"
    )

    out.append("## 1. Trial-Level Accounting (Denominator = 248 Assigned Trials)\n")
    out.append(f"- **Assigned trials**: {total_assigned}/248\n")
    out.append(f"- **Retrieved with HTTP 200**: {n_retrieved}/248 (retrieval failures: {len(retrieval_failures)}/248)\n")
    out.append(f"- **Parsed and dual-read**: {n_parsed_and_read}/{n_retrieved} retrieved trials (parsing/reading failures: {len(parsing_failures)}/{n_retrieved})\n")
    out.append(
        f"- **Trials with >= 1 posted HbA1c primary outcome measure (`hba1c_primary_measure_posted = yes`)**: "
        f"{len(trials_with_hba1c)}/{n_parsed_and_read} parsed trials "
        f"(producing **{M_DENOM}** HbA1c primary outcome measure rows and **{A_DENOM}** posted between-arm analysis rows)\n"
    )
    out.append(
        f"- **Trials with no posted HbA1c primary outcome measure (`hba1c_primary_measure_posted = no`)**: "
        f"{len(trials_no_hba1c)}/{n_parsed_and_read} parsed trials — NCT IDs: {fmt_nct_list(trials_no_hba1c)}\n"
    )
    if trials_unresolved_hba1c:
        out.append(
            f"- **Trials unresolved on `hba1c_primary_measure_posted`**: "
            f"{len(trials_unresolved_hba1c)}/{n_parsed_and_read} — NCT IDs: {fmt_nct_list(trials_unresolved_hba1c)}\n"
        )
    multi_m_ncts = [
        nct for nct in trials_with_hba1c
        if len(adjudicated_trials[nct]["sheet"].get("measures", [])) > 1
    ]
    out.append(
        f"- **Trials posting > 1 HbA1c primary outcome measure**: {len(multi_m_ncts)}/{len(trials_with_hba1c)} trials with an HbA1c primary measure — "
        f"NCT IDs: {fmt_nct_list(multi_m_ncts)}\n"
    )

    # Section 2: Measure distributions
    out.append("\n## 2. Distributions Across Posted HbA1c Primary Outcome Measures and Analyses\n")

    for section_label, attr, rule_desc in [
        (
            "2.1 Measures by `construct`",
            "construct",
            f"Denominator = {M_DENOM} posted HbA1c primary outcome measures across {len(trials_with_hba1c)} trials (`cohort_sheet.csv` rows with `hba1c_primary_measure_posted == 'yes'`).",
        ),
        (
            "2.2 Measures by `unit_class`",
            "unit_class",
            f"Denominator = {M_DENOM} posted HbA1c primary outcome measures, classified strictly from `outcomeMeasures[i].unitOfMeasure` without unit conversion.",
        ),
        (
            "2.3 Measures by `time_point_weeks`",
            "time_point_weeks",
            f"Denominator = {M_DENOM} posted HbA1c primary outcome measures; numeric week string iff `timeFrame` states one fixed post-baseline time point in weeks, else `not_one_fixed_time_point`.",
        ),
        (
            "2.4 Measures by `population_class`",
            "population_class",
            f"Denominator = {M_DENOM} posted HbA1c primary outcome measures, classified from `populationDescription` using the record's own population term.",
        ),
        (
            "2.5 Measures by `comparator_class`",
            "comparator_class",
            f"Denominator = {M_DENOM} posted HbA1c primary outcome measures, classified strictly from the arm descriptions.",
        ),
    ]:
        out.append(f"### {section_label}\n")
        out.append(f"*Rule & denominator*: {rule_desc}\n\n")
        out.append("| Value | Measures (`n / N`) | Distinct Trials | NCT IDs |\n|---|---|---|---|\n")
        for val, cnt, ncts in group_measures_by(attr):
            out.append(f"| `{val}` | {cnt} / {M_DENOM} | {len(ncts)} | {fmt_nct_list(ncts)} |\n")
        out.append("\n")

    # 2.6 estimate_type_as_posted (grouped only where identical after trim + lowercase; list distinct strings)
    out.append("### 2.6 `estimate_type_as_posted` (Grouped Only by `trim + lowercase`; Distinct Verbatim Strings Listed)\n")
    out.append(
        f"*Rule & denominators*: Strings in `analyses[].paramType` are grouped ONLY where identical after `.strip().lower()`, listing every distinct verbatim string observed. "
        f"Reported first at the **between-arm analysis level** (denominator = {A_DENOM} posted between-arm analyses) and second at the **measure level** (denominator = {M_DENOM} posted HbA1c primary outcome measures; note a measure with multiple analyses of different types appears in each matching row, and measures with 0 posted between-arm analyses are reported under `(no_between_arm_analysis_posted)`).\n\n"
    )

    est_an_groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"an_count": 0, "distinct_raw": defaultdict(int), "ncts": set(), "measures": set()}
    )
    for a_row in analyses_csv_rows:
        raw_s = a_row["estimate_type_as_posted"]
        norm_s = raw_s.strip().lower()
        est_an_groups[norm_s]["an_count"] += 1
        est_an_groups[norm_s]["distinct_raw"][raw_s] += 1
        est_an_groups[norm_s]["ncts"].add(a_row["nct_id"])
        est_an_groups[norm_s]["measures"].add((a_row["nct_id"], int(a_row["measure_index"])))

    no_an_measures = [(nct, m["measure_index"]) for nct, m in all_measures if not m.get("analyses")]
    no_an_ncts = sorted({nct for nct, _ in no_an_measures})

    out.append("| Normalized Key (`trim+lower`) | Distinct Verbatim Strings (`count`) | Analyses (`n / N`) | Measures (`n / N`) | Distinct Trials | NCT IDs |\n|---|---|---|---|---|---|\n")
    for norm_k, info in sorted(est_an_groups.items(), key=lambda x: (-x[1]["an_count"], x[0])):
        distinct_str = "; ".join(
            f"`{rk}` ({rc})" for rk, rc in sorted(info["distinct_raw"].items(), key=lambda x: (-x[1], x[0]))
        )
        ncts_sorted = sorted(info["ncts"])
        out.append(
            f"| `{norm_k}` | {distinct_str} | {info['an_count']} / {A_DENOM} | "
            f"{len(info['measures'])} / {M_DENOM} | {len(ncts_sorted)} | {fmt_nct_list(ncts_sorted)} |\n"
        )
    out.append(
        f"| `(no_between_arm_analysis_posted)` | — | 0 / {A_DENOM} | "
        f"{len(no_an_measures)} / {M_DENOM} | {len(no_an_ncts)} | {fmt_nct_list(no_an_ncts)} |\n\n"
    )

    # 2.7 hypothesis_class
    out.append("### 2.7 `hypothesis_class`\n")
    out.append(
        f"*Rule & denominators*: Reported across all **{A_DENOM}** posted between-arm analyses (denominator = {A_DENOM}) and across all **{M_DENOM}** posted HbA1c primary outcome measures (denominator = {M_DENOM}; a measure testing both non-inferiority and superiority across separate analyses appears in both rows, while measures with 0 posted between-arm analyses appear under `(no_between_arm_analysis_posted)`).\n\n"
    )
    hyp_groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"an_count": 0, "ncts": set(), "measures": set(), "raw_ni": defaultdict(int)})
    for a_row in analyses_csv_rows:
        hc = a_row["hypothesis_class"]
        hyp_groups[hc]["an_count"] += 1
        hyp_groups[hc]["ncts"].add(a_row["nct_id"])
        hyp_groups[hc]["measures"].add((a_row["nct_id"], int(a_row["measure_index"])))
        hyp_groups[hc]["raw_ni"][a_row["non_inferiority_type_raw"]] += 1

    out.append("| `hypothesis_class` | Raw `nonInferiorityType` Breakdown | Analyses (`n / N`) | Measures (`n / N`) | Distinct Trials | NCT IDs |\n|---|---|---|---|---|---|\n")
    for hc, info in sorted(hyp_groups.items(), key=lambda x: (-x[1]["an_count"], x[0])):
        raw_bd = "; ".join(f"`{k}` ({v})" for k, v in sorted(info["raw_ni"].items(), key=lambda x: (-x[1], x[0])))
        ncts_sorted = sorted(info["ncts"])
        out.append(
            f"| `{hc}` | {raw_bd} | {info['an_count']} / {A_DENOM} | "
            f"{len(info['measures'])} / {M_DENOM} | {len(ncts_sorted)} | {fmt_nct_list(ncts_sorted)} |\n"
        )
    out.append(
        f"| `(no_between_arm_analysis_posted)` | — | 0 / {A_DENOM} | "
        f"{len(no_an_measures)} / {M_DENOM} | {len(no_an_ncts)} | {fmt_nct_list(no_an_ncts)} |\n\n"
    )

    # Section 3: Combined Cohort Filter Counts
    out.append("## 3. Combined Endpoint Filter Counts (Trial Level)\n")
    qualifying_trials: list[str] = []
    qualifying_measures_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    single_groupid_qualifying_ncts: list[str] = []

    for nct in trials_with_hba1c:
        sheet = adjudicated_trials[nct]["sheet"]
        for m in sheet.get("measures", []):
            if (
                m.get("construct") == "change_from_baseline_in_hba1c"
                and m.get("unit_class") == "percent_hba1c"
                and is_fixed_week_in_24_to_26(str(m.get("time_point_weeks", "")))
            ):
                qual_ans = [
                    an for an in m.get("analyses", [])
                    if an.get("estimate") not in ("", "not_stated", "unresolved")
                    and an.get("ci_lower") not in ("", "not_stated", "unresolved")
                    and an.get("ci_upper") not in ("", "not_stated", "unresolved")
                ]
                if qual_ans:
                    qualifying_measures_by_trial[nct].append(m)
                    if not any(len(an.get("groups_compared", [])) >= 2 for an in qual_ans):
                        single_groupid_qualifying_ncts.append(nct)
        if qualifying_measures_by_trial[nct]:
            qualifying_trials.append(nct)

    Q_DENOM = len(qualifying_trials)
    out.append("### 3.1 Trials Satisfying the Joint Primary-Measure Criteria\n")
    out.append(
        "*Rule*: Trial has at least one posted `PRIMARY` HbA1c outcome measure where **all** of the following hold on that same measure:\n"
        "1. `construct == 'change_from_baseline_in_hba1c'`\n"
        "2. `unit_class == 'percent_hba1c'`\n"
        "3. `time_point_weeks` is one fixed numeric week value with `24 <= time_point_weeks <= 26`\n"
        "4. At least one posted between-arm analysis on that measure has `estimate`, `ci_lower`, and `ci_upper` all posted (`!= 'not_stated'` and `!= 'unresolved'`).\n\n"
        f"- **Qualifying trials**: **{Q_DENOM} / {total_assigned}** assigned trials "
        f"(and **{Q_DENOM} / {len(trials_with_hba1c)}** trials with a posted HbA1c primary measure)\n"
        f"- **Qualifying NCT IDs ({Q_DENOM})**: {fmt_nct_list(qualifying_trials)}\n"
        f"- **Non-qualifying parsed trials ({n_parsed_and_read - Q_DENOM} / {n_parsed_and_read})**: "
        f"{fmt_nct_list([n for n in adjudicated_trials if n not in set(qualifying_trials)])}\n"
    )
    if single_groupid_qualifying_ncts:
        out.append(
            f"- *Note on `groups_compared` cardinality*: Among the {Q_DENOM} qualifying trials above, "
            f"{len(set(single_groupid_qualifying_ncts))} trial(s) ({fmt_nct_list(single_groupid_qualifying_ncts)}) had between-arm difference analyses where the submitter checked only 1 `groupId` in `analyses[].groupIds` in ClinicalTrials.gov PRS. Requiring `len(groups_compared) >= 2` in addition would yield {Q_DENOM - len(set(single_groupid_qualifying_ncts))} / {total_assigned} trials.\n"
        )
    out.append("\n")

    # Split qualifying trials by comparator_class and population_class
    for sub_num, label, attr in [
        ("3.2", "Qualifying Trials Split by `comparator_class`", "comparator_class"),
        ("3.3", "Qualifying Trials Split by `population_class`", "population_class"),
    ]:
        out.append(f"### {sub_num} {label} (Denominator = {Q_DENOM} Qualifying Trials)\n")
        by_val: dict[str, list[str]] = defaultdict(list)
        multi_class_ncts: list[str] = []
        for nct in qualifying_trials:
            vals = sorted({str(m.get(attr, "not_stated")) for m in qualifying_measures_by_trial[nct]})
            if len(vals) > 1:
                multi_class_ncts.append(nct)
            for v in vals:
                by_val[v].append(nct)
        if multi_class_ncts:
            out.append(
                f"*Note*: {len(multi_class_ncts)} qualifying trial(s) had >1 qualifying HbA1c primary measure with differing `{attr}` values ({fmt_nct_list(multi_class_ncts)}) and are counted in each applicable row below.\n\n"
            )
        out.append(f"| `{attr}` | Qualifying Trials (`n / {Q_DENOM}`) | NCT IDs |\n|---|---|---|\n")
        for v, ncts in sorted(by_val.items(), key=lambda x: (-len(set(x[1])), x[0])):
            u_ncts = sorted(set(ncts))
            out.append(f"| `{v}` | {len(u_ncts)} / {Q_DENOM} | {fmt_nct_list(u_ncts)} |\n")
        out.append("\n")

    # 3.4 Race table and combined race+ethnicity among qualifying trials
    qual_with_race: list[str] = []
    qual_without_race: list[str] = []
    qual_race_combined_yes: list[str] = []
    qual_race_combined_no: list[str] = []
    qual_race_combined_other: list[str] = []

    for nct in qualifying_trials:
        sheet = adjudicated_trials[nct]["sheet"]
        has_race = any(dm.get("domain") == "race" for dm in sheet.get("baseline_demographic_measures", []))
        if has_race:
            qual_with_race.append(nct)
            comb = sheet.get("race_and_ethnicity_in_one_table")
            if comb == "yes":
                qual_race_combined_yes.append(nct)
            elif comb == "no":
                qual_race_combined_no.append(nct)
            else:
                qual_race_combined_other.append(nct)
        else:
            qual_without_race.append(nct)

    R_DENOM = len(qual_with_race)
    out.append(f"### 3.4 Baseline Race Table & Combined Race/Ethnicity Table Among the {Q_DENOM} Qualifying Trials\n")
    out.append(
        f"- **Qualifying trials with a posted baseline race table (`race` measure present in `baselineCharacteristicsModule`)**: "
        f"**{R_DENOM} / {Q_DENOM}** qualifying trials — NCT IDs: {fmt_nct_list(qual_with_race)}\n"
        f"- **Qualifying trials without a posted baseline race table**: "
        f"**{len(qual_without_race)} / {Q_DENOM}** qualifying trials — NCT IDs: {fmt_nct_list(qual_without_race)}\n"
        f"- **Of the {R_DENOM} qualifying trials with a posted race table, those putting race and ethnicity in one table (`race_and_ethnicity_in_one_table == 'yes'`)**: "
        f"**{len(qual_race_combined_yes)} / {R_DENOM}** (and {len(qual_race_combined_yes)} / {Q_DENOM} of all qualifying trials) — NCT IDs: {fmt_nct_list(qual_race_combined_yes)}\n"
        f"- **Of the {R_DENOM} qualifying trials with a posted race table, those NOT putting race and ethnicity in one table (`race_and_ethnicity_in_one_table == 'no'`)**: "
        f"**{len(qual_race_combined_no)} / {R_DENOM}** — NCT IDs: {fmt_nct_list(qual_race_combined_no)}\n"
    )
    if qual_race_combined_other:
        out.append(
            f"- **Of the {R_DENOM} qualifying trials with a posted race table, other/unresolved status**: "
            f"**{len(qual_race_combined_other)} / {R_DENOM}** — NCT IDs: {fmt_nct_list(qual_race_combined_other)}\n"
        )
    out.append("\n")

    # Separate required audit headings
    out.append("## 4. Retrieval Failures\n")
    out.append(f"- **Count**: **{len(retrieval_failures)} / {total_assigned}** assigned trials.\n")
    if retrieval_failures:
        for nct, reason in retrieval_failures:
            out.append(f"  - `{nct}`: {reason}\n")
    else:
        out.append("- None. All 248 assigned trials returned HTTP 200 and were stored gzip-compressed.\n")
    out.append("\n")

    out.append("## 5. Parsing Failures\n")
    out.append(f"- **Count**: **{len(parsing_failures)} / {n_retrieved}** retrieved trials.\n")
    if parsing_failures:
        for nct, reason in parsing_failures:
            out.append(f"  - `{nct}`: {reason}\n")
    else:
        out.append("- None. All 248 stored JSON records decompressed, verified by SHA-256, parsed cleanly, and completed dual reading.\n")
    out.append("\n")

    out.append("## 6. Values Not Stated in the Record (`not_stated`)\n")
    out.append(
        "*Rule*: Counts below include **only** records where the adjudicated value is literally `not_stated` (or empty/absent table in `baselineCharacteristicsModule` / `contactsLocationsModule`); retrieval failures, parsing failures, and unresolved reader disagreements are excluded from numerators and named separately.\n\n"
    )
    not_stated_rows: list[tuple[str, int, int, list[str]]] = []

    # Measure-level not_stated fields
    for label, attr in [
        ("measure.construct == 'not_stated'", "construct"),
        ("measure.unit_as_posted == 'not_stated'", "unit_as_posted"),
        ("measure.unit_class == 'not_stated'", "unit_class"),
        ("measure.time_frame_as_posted == 'not_stated'", "time_frame_as_posted"),
        ("measure.population_description_as_posted == 'not_stated'", "population_description_as_posted"),
        ("measure.population_class == 'not_stated'", "population_class"),
        ("measure.missing_data_method_as_posted == 'not_stated'", "missing_data_method_as_posted"),
        ("measure.data_after_rescue_therapy == 'not_stated'", "data_after_rescue_therapy"),
        ("measure.parameter_type == 'not_stated'", "parameter_type"),
        ("measure.dispersion_type == 'not_stated'", "dispersion_type"),
        ("measure.background_therapy_as_posted == 'not_stated'", "background_therapy_as_posted"),
    ]:
        hits = [nct for nct, m in all_measures if m.get(attr) == "not_stated"]
        not_stated_rows.append((f"`{label}` (measures)", len(hits), M_DENOM, hits))

    # Analysis-level not_stated fields
    for label, attr in [
        ("analysis.estimate_type_as_posted == 'not_stated'", "estimate_type_as_posted"),
        ("analysis.estimate == 'not_stated'", "estimate"),
        ("analysis.ci_percent == 'not_stated'", "ci_percent"),
        ("analysis.ci_lower == 'not_stated'", "ci_lower"),
        ("analysis.ci_upper == 'not_stated'", "ci_upper"),
        ("analysis.ci_sides == 'not_stated'", "ci_sides"),
        ("analysis.p_value_as_posted == 'not_stated'", "p_value_as_posted"),
        ("analysis.hypothesis_class == 'not_stated'", "hypothesis_class"),
        ("analysis.non_inferiority_margin_as_posted == 'not_stated'", "non_inferiority_margin_as_posted"),
        ("analysis.statistical_method_as_posted == 'not_stated'", "statistical_method_as_posted"),
    ]:
        hits = [a["nct_id"] for a in analyses_csv_rows if a.get(attr) == "not_stated"]
        not_stated_rows.append((f"`{label}` (analyses)", len(hits), A_DENOM, hits))

    # Trial-level baseline/location absence across trials_with_hba1c
    T_HBA1C_DENOM = len(trials_with_hba1c)
    no_bl_hba1c = [n for n in trials_with_hba1c if adjudicated_trials[n]["sheet"].get("baseline_hba1c_posted") == "no"]
    not_stated_rows.append(("`baseline.baseline_hba1c_posted == 'no'` (trials)", len(no_bl_hba1c), T_HBA1C_DENOM, no_bl_hba1c))
    for dom in ("race", "ethnicity", "sex", "age"):
        missing_dom = [
            n for n in trials_with_hba1c
            if not any(dm.get("domain") == dom for dm in adjudicated_trials[n]["sheet"].get("baseline_demographic_measures", []))
        ]
        not_stated_rows.append((f"`baseline.{dom}_table` not posted (trials)", len(missing_dom), T_HBA1C_DENOM, missing_dom))
    no_locs = [n for n in trials_with_hba1c if not adjudicated_trials[n]["sheet"].get("countries")]
    not_stated_rows.append(("`contactsLocationsModule.locations` empty/absent (trials)", len(no_locs), T_HBA1C_DENOM, no_locs))

    out.append("| Field / Check | Not Stated Count (`n / N`) | Distinct Trials | NCT IDs |\n|---|---|---|---|\n")
    for label, cnt, denom, ncts in not_stated_rows:
        out.append(f"| {label} | {cnt} / {denom} | {len(set(ncts))} | {fmt_nct_list(ncts)} |\n")
    out.append("\n")

    out.append("## 7. Fields Where the Two Independent Readings Disagreed and Per-Field Agreement Rates\n")
    total_cmp = sum(v["compared"] for v in agreement_by_cat.values())
    total_agr = sum(v["agreed"] for v in agreement_by_cat.values())
    total_dis = sum(v["disagreed"] for v in agreement_by_cat.values())
    overall_pct = (100.0 * total_agr / total_cmp) if total_cmp else 0.0
    trials_any_dis = sorted({d["nct_id"] for d in all_disagreement_instances})
    out.append(
        f"- **Overall atomic field comparisons**: **{total_agr} / {total_cmp}** agreed (**{overall_pct:.2f}%**), "
        f"**{total_dis} / {total_cmp}** disagreed across **{len(trials_any_dis)} / {n_parsed_and_read}** dual-read trials.\n\n"
    )
    out.append("| Field Category | Agreed (`n / N`) | Disagreed (`n / N`) | Agreement Rate | Trials Disagreed (`NCT IDs`) |\n|---|---|---|---|---|\n")
    for cat in sorted(agreement_by_cat.keys()):
        st = agreement_by_cat[cat]
        c_tot = st["compared"]
        c_agr = st["agreed"]
        c_dis = st["disagreed"]
        pct = (100.0 * c_agr / c_tot) if c_tot else 100.0
        out.append(
            f"| `{cat}` | {c_agr} / {c_tot} | {c_dis} / {c_tot} | {pct:.2f}% | {fmt_nct_list(sorted(st['disagreed_ncts']))} |\n"
        )
    out.append("\n")

    out.append("## 8. Fields Left Unresolved After Third-Reader Adjudication\n")
    out.append(
        f"- **Unresolved field count**: **{len(all_unresolved_instances)} / {total_dis}** disagreed fields "
        f"(out of **{total_cmp}** total compared fields across {n_parsed_and_read} dual-read trials).\n"
    )
    if all_unresolved_instances:
        out.append("\n| NCT ID | Field Key | Reader 1 | Reader 2 | Adjudicator Note |\n|---|---|---|---|---|\n")
        for u in all_unresolved_instances:
            r1_s = json.dumps(u["r1"], ensure_ascii=False)[:120].replace("|", "\\|")
            r2_s = json.dumps(u["r2"], ensure_ascii=False)[:120].replace("|", "\\|")
            rat_s = str(u["adjudicator_rationale"]).replace("|", "\\|").replace("\n", " ")
            out.append(f"| `{u['nct_id']}` | `{u['field_key']}` | `{r1_s}` | `{r2_s}` | {rat_s} |\n")
    else:
        out.append("- None. Every field where Reader 1 and Reader 2 differed was decided directly from verbatim text in the stored record.\n")
    out.append("\n")

    out.append("## 9. Rows That Failed Deterministic Quote Verification (`evidence_not_found`)\n")
    checked_measure_rows = sum(1 for r in cohort_csv_rows if r.get("hba1c_primary_measure_posted") == "yes")
    out.append(
        f"- **Failed rows (`evidence_not_found`)**: **{len(quote_failed_rows)} / {checked_measure_rows}** HbA1c primary measure rows in `cohort_sheet.csv`.\n"
        "- *Rule*: Every quoted string in the adjudicated sheet was checked deterministically against the exact scalar value at its stated JSON path in the uncompressed `records/{NCT_ID}.json.gz` file. Rows with any missing or non-verbatim quote were marked `evidence_not_found` and were **never** silently repaired.\n"
    )
    if quote_failed_rows:
        out.append("\n| NCT ID | Measure Index | Failing Field(s) and Reason(s) |\n|---|---|---|\n")
        for qf in quote_failed_rows:
            desc = "; ".join(
                f"{f.get('field')}: {f.get('reason')} (path={f.get('path')!r}, text={f.get('text')!r})"
                for f in qf["failures"]
            ).replace("|", "\\|").replace("\n", " ")
            out.append(f"| `{qf['nct_id']}` | `{qf['measure_index']}` | {desc} |\n")
    else:
        out.append("- None. Every quoted string across all rows was found verbatim at its stated JSON path in the stored registry record.\n")

    return "".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
