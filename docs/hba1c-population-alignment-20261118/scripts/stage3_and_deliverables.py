#!/usr/bin/env python3
"""Stage 3 derived fields and deliverable generator (offline, deterministic, no LLM/network).

Reads:
- docs/audits/hba1c_cohort_reading/records/{NCT_ID}.json.gz
- docs/audits/hba1c_population_alignment/arm_demographics.csv
- docs/audits/hba1c_population_alignment/intermediate/stage1_trials.json
- docs/audits/hba1c_population_alignment/intermediate/raw_dual_mappings.jsonl
- docs/audits/hba1c_population_alignment/intermediate/raw_adjudications.jsonl

Writes:
- docs/audits/hba1c_population_alignment/mapping.jsonl
- docs/audits/hba1c_population_alignment/contrasts.csv
- docs/audits/hba1c_population_alignment/trial_alignment.csv
- docs/audits/hba1c_population_alignment/summary.md
- docs/audits/hba1c_population_alignment/method.md
"""

from __future__ import annotations

import csv
import gzip
import json
import pathlib
import sys
from collections import Counter, defaultdict
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
COHORT_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_cohort_reading"
ALIGN_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_population_alignment"
SCRIPTS_DIR = ALIGN_DIR / "scripts"
RECORDS_DIR = COHORT_DIR / "records"
INTERMEDIATE_DIR = ALIGN_DIR / "intermediate"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import stage2_schema_and_prompts as s2  # noqa: E402

CONTRASTS_CSV_COLUMNS = [
    "nct_id",
    "measure_index",
    "measure_title",
    "analysis_index",
    "has_estimate_and_both_ci_limits",
    "rederivation_flags_json",
    "groups_compared_json",
    "groups_compared_count",
    "estimate_type_as_posted",
    "estimate",
    "ci_percent",
    "ci_lower",
    "ci_upper",
    "p_value_as_posted",
    "arm_1_og_id",
    "arm_1_og_title",
    "arm_1_role",
    "arm_1_og_to_bg_status",
    "arm_1_bg_ids_json",
    "arm_1_og_to_fg_status",
    "arm_1_fg_ids_json",
    "arm_2_og_id",
    "arm_2_og_title",
    "arm_2_role",
    "arm_2_og_to_bg_status",
    "arm_2_bg_ids_json",
    "arm_2_og_to_fg_status",
    "arm_2_fg_ids_json",
    "both_arms_map_same_arm_bg",
    "arm_demographics_omitted_reason",
    "arm_1_bg_id",
    "arm_1_baseline_n",
    "arm_1_analysed_n",
    "arm_1_attrition_n",
    "arm_1_attrition_pct_of_baseline",
    "arm_2_bg_id",
    "arm_2_baseline_n",
    "arm_2_analysed_n",
    "arm_2_attrition_n",
    "arm_2_attrition_pct_of_baseline",
    "arm_1_race_rows_json",
    "arm_1_ethnicity_rows_json",
    "arm_1_sex_rows_json",
    "arm_2_race_rows_json",
    "arm_2_ethnicity_rows_json",
    "arm_2_sex_rows_json",
    "shares_arm_among_est_and_ci_contrasts",
    "shared_arm_est_and_ci_contrasts_json",
    "shares_arm_among_all_posted_contrasts",
    "shared_arm_all_posted_contrasts_json",
    "baseline_has_other_non_total_bg_arms",
    "other_non_total_bg_ids_json",
    "contrast_quote_verification_status",
]

TRIAL_ALIGNMENT_CSV_COLUMNS = [
    "nct_id",
    "record_status",
    "coding_status",
    "not_coded_reason",
    "qualifying_measure_indices_json",
    "qualifying_measures_count",
    "rederivation_has_disagreement",
    "rederivation_disagreements_json",
    "baseline_total_n",
    "baseline_total_units",
    "period_1_started_total_n",
    "baseline_minus_period_1_started_n",
    "baseline_minus_period_1_started_pct_of_started",
    "baseline_minus_period_1_started_pct_of_baseline",
    "first_qualifying_measure_index",
    "first_qualifying_measure_analysed_sum_n",
    "baseline_minus_first_measure_analysed_n",
    "baseline_minus_first_measure_analysed_pct_of_baseline",
    "per_qualifying_measure_alignment_json",
    "baseline_table_scope",
    "baseline_table_scope_reason_summary",
    "baseline_table_scope_quotes_json",
    "all_ogs_same_arm_bg",
    "og_to_bg_statuses_json",
    "all_ogs_same_arm_fg",
    "og_to_fg_statuses_json",
    "posted_between_arm_contrasts_count",
    "est_and_ci_contrasts_count",
    "same_arm_bg_est_and_ci_contrasts_count",
    "has_shared_arm_est_and_ci_contrasts",
    "shared_arm_est_and_ci_summary_json",
    "has_shared_arm_all_posted_contrasts",
    "shared_arm_all_posted_summary_json",
    "race_table_posted",
    "ethnicity_table_posted",
    "sex_table_posted",
    "age_table_posted",
    "reader_fields_compared",
    "reader_fields_agreed",
    "reader_fields_disagreed",
    "reader_fields_unresolved",
    "adjudicated_quotes_checked",
    "adjudicated_quote_failures",
    "reader_1_quotes_checked",
    "reader_1_quote_failures",
    "reader_2_quotes_checked",
    "reader_2_quote_failures",
]


def load_jsonl_latest_ok(path: pathlib.Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    ok_map: dict[str, dict[str, Any]] = {}
    err_map: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return ok_map, err_map
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            nct = obj["nct_id"]
            if obj.get("status") == "ok":
                ok_map[nct] = obj
                err_map.pop(nct, None)
            elif nct not in ok_map:
                err_map[nct] = obj
    return ok_map, err_map


def fmt_pct(num: int, den: int) -> str:
    if den == 0:
        return "not_applicable"
    return f"{(100.0 * num) / den:.6f}"


def fmt_nct_list(ncts: list[str] | set[str]) -> str:
    s = sorted(set(ncts))
    return ", ".join(s) if s else "none"


def compute_stage3_outputs() -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, str]],
    dict[str, Any],
]:
    """Compute mapping.jsonl entries, contrasts.csv rows, trial_alignment.csv rows, and summary stats."""
    with open(INTERMEDIATE_DIR / "stage1_trials.json", encoding="utf-8") as f:
        stage1_meta = json.load(f)

    nct_ids: list[str] = stage1_meta["nct_ids"]
    trials_s1: dict[str, dict[str, Any]] = stage1_meta["trials"]

    # Index arm_demographics.csv by (nct_id, group_id)
    demo_by_nct_bg: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with open(ALIGN_DIR / "arm_demographics.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            demo_by_nct_bg[(row["nct_id"], row["group_id"])].append(row)

    dual_ok, dual_err = load_jsonl_latest_ok(INTERMEDIATE_DIR / "raw_dual_mappings.jsonl")
    adj_ok, adj_err = load_jsonl_latest_ok(INTERMEDIATE_DIR / "raw_adjudications.jsonl")

    mapping_records: list[dict[str, Any]] = []
    contrasts_rows: list[dict[str, str]] = []
    trial_rows: list[dict[str, str]] = []

    for nct_id in nct_ids:
        t1 = trials_s1[nct_id]
        rstatus = t1["record_status"]
        if rstatus != "ok":
            mapping_records.append(
                {
                    "nct_id": nct_id,
                    "record_status": rstatus,
                    "coding_status": rstatus,
                    "not_coded_reason": t1.get("record_error", rstatus),
                    "reading_1": None,
                    "reading_2": None,
                    "comparison": None,
                    "adjudication": None,
                    "adjudicated": None,
                    "quote_verification": None,
                }
            )
            trow = {col: "" for col in TRIAL_ALIGNMENT_CSV_COLUMNS}
            trow.update(
                {
                    "nct_id": nct_id,
                    "record_status": rstatus,
                    "coding_status": rstatus,
                    "not_coded_reason": t1.get("record_error", rstatus),
                }
            )
            trial_rows.append(trow)
            continue

        if nct_id not in dual_ok:
            reason = dual_err.get(nct_id, {}).get("error", "missing_dual_reading")
            mapping_records.append(
                {
                    "nct_id": nct_id,
                    "record_status": "ok",
                    "record_sha256": t1["record_sha256"],
                    "coding_status": "could_not_be_parsed",
                    "not_coded_reason": reason,
                    "reading_1": None,
                    "reading_2": None,
                    "comparison": None,
                    "adjudication": None,
                    "adjudicated": None,
                    "quote_verification": None,
                }
            )
            trow = {col: "" for col in TRIAL_ALIGNMENT_CSV_COLUMNS}
            trow.update(
                {
                    "nct_id": nct_id,
                    "record_status": "ok",
                    "coding_status": "could_not_be_parsed",
                    "not_coded_reason": reason,
                }
            )
            trial_rows.append(trow)
            continue

        with gzip.open(RECORDS_DIR / f"{nct_id}.json.gz", "rt", encoding="utf-8") as fp:
            rec = json.load(fp)

        d_obj = dual_ok[nct_id]
        r1 = d_obj["reading_1"]
        r2 = d_obj["reading_2"]
        comp = s2.compare_stage2_readings(t1, r1, r2, rec)
        qv_r1 = s2.verify_stage2_reading_quotes(rec, r1)
        qv_r2 = s2.verify_stage2_reading_quotes(rec, r2)

        adj_wrapper = adj_ok.get(nct_id)
        adj_raw = adj_wrapper["adjudication"] if adj_wrapper else None

        # Verify adjudicator deciding_quotes as well if adjudication ran
        adj_deciding_checked = 0
        adj_deciding_failures: list[dict[str, Any]] = []
        if adj_raw:
            for dec in adj_raw.get("decisions", []):
                fk = dec["field_key"]
                allowed_re = None
                if fk.startswith("og_to_bg:"):
                    allowed_re = s2.OG_BG_ALLOWED_PATH_RE
                elif fk.startswith("og_to_fg:"):
                    allowed_re = s2.OG_FG_ALLOWED_PATH_RE
                elif fk.startswith("analysis_arm_roles:"):
                    allowed_re = s2.ARM_ROLE_ALLOWED_PATH_RE
                if dec["choice"] != "unresolved" or dec.get("deciding_quotes"):
                    c_cnt, f_list = s2.verify_quotes_list(
                        rec,
                        dec.get("deciding_quotes", []),
                        f"adjudicator.{fk}",
                        allowed_re,
                    )
                    adj_deciding_checked += c_cnt
                    adj_deciding_failures.extend(f_list)

        adjudicated, unresolved_from_dis = s2.build_adjudicated_stage2(
            nct_id,
            t1,
            r1,
            r2,
            comp,
            adj_raw,
        )
        qv_adj = s2.verify_stage2_reading_quotes(rec, adjudicated)

        # Count total unresolved fields in adjudicated coding (whether both readers coded unresolved or adjudication left unresolved)
        total_unresolved_in_adj = (
            sum(1 for x in adjudicated["og_to_bg"] if x["mapping_status"] == "unresolved")
            + sum(1 for x in adjudicated["og_to_fg"] if x["mapping_status"] == "unresolved")
            + sum(1 for x in adjudicated["analysis_arm_roles"] if x["arm_role"] == "unresolved")
            + (1 if adjudicated["baseline_table_scope"]["includes_outside_hba1c_analysis"] == "unresolved" else 0)
        )

        mapping_entry = {
            "nct_id": nct_id,
            "record_status": "ok",
            "record_sha256": t1["record_sha256"],
            "coding_status": "fully_coded",
            "r1_started_at": d_obj["r1_started_at"],
            "r1_completed_at": d_obj["r1_completed_at"],
            "reading_1": r1,
            "r2_started_at": d_obj["r2_started_at"],
            "r2_completed_at": d_obj["r2_completed_at"],
            "reading_2": r2,
            "comparison": comp,
            "adjudication": (
                {
                    "started_at": adj_wrapper["started_at"],
                    "completed_at": adj_wrapper["completed_at"],
                    "raw_adjudication": adj_raw,
                    "adjudicator_quote_checks": adj_deciding_checked,
                    "adjudicator_quote_failures": adj_deciding_failures,
                }
                if adj_wrapper
                else None
            ),
            "adjudicated": adjudicated,
            "quote_verification": {
                "adjudicated": qv_adj,
                "reading_1": qv_r1,
                "reading_2": qv_r2,
            },
            "unresolved_from_disagreement_count": unresolved_from_dis,
            "total_unresolved_fields_in_adjudicated": total_unresolved_in_adj,
        }
        mapping_records.append(mapping_entry)

        # --- Stage 3 Trial-Level & Contrast-Level Derivations ---
        bg_by_id = {bg["group_id"]: bg for bg in t1["baseline_groups"]}
        non_total_bg_ids = [bg["group_id"] for bg in t1["baseline_groups"] if not bg["is_total_group"]]
        total_bg = next(bg for bg in t1["baseline_groups"] if bg["is_total_group"])
        baseline_total_n = int(total_bg["denom_count"])
        baseline_total_units = total_bg["denom_units"]

        period_1_started_total_n = sum(
            int(fg["periods"][0]["started_num_subjects"])
            for fg in t1["flow_groups"]
            if fg["periods"] and str(fg["periods"][0]["started_num_subjects"]).isdigit()
        )
        base_minus_started_n = baseline_total_n - period_1_started_total_n

        og_to_bg_idx = {
            (x["measure_index"], x["og_group_id"]): x for x in adjudicated["og_to_bg"]
        }
        og_to_fg_idx = {
            (x["measure_index"], x["og_group_id"]): x for x in adjudicated["og_to_fg"]
        }
        role_idx = {
            (x["measure_index"], x["analysis_index"], x["group_id"]): x
            for x in adjudicated["analysis_arm_roles"]
        }
        failed_quote_contexts = {
            f["context_key"].split(".quotes[", 1)[0] for f in qv_adj["failures"]
        }

        per_measure_alignment: list[dict[str, Any]] = []
        trial_contrast_specs: list[dict[str, Any]] = []

        for qm in t1["qualifying_measures"]:
            mi = qm["measure_index"]
            og_by_id = {og["group_id"]: og for og in qm["groups"]}
            analysed_sum_n = sum(
                int(og["denom_count"]) for og in qm["groups"] if str(og["denom_count"]).isdigit()
            )
            diff_n = baseline_total_n - analysed_sum_n
            per_measure_alignment.append(
                {
                    "measure_index": mi,
                    "measure_title": qm["title"],
                    "analysed_sum_n": analysed_sum_n,
                    "baseline_total_n": baseline_total_n,
                    "baseline_minus_analysed_n": diff_n,
                    "baseline_minus_analysed_pct_of_baseline": fmt_pct(diff_n, baseline_total_n),
                    "baseline_minus_analysed_pct_of_analysed": fmt_pct(diff_n, analysed_sum_n),
                }
            )

            for an in qm["analyses"]:
                if not an["is_between_arm"]:
                    continue
                ai = an["analysis_index"]
                gids = an["group_ids"]
                has_est_ci = an["has_estimate_and_both_ci_limits"]

                # Compute canonical arm identities for non-independence sharing checks
                arm_tokens: set[str] = set()
                for gid in gids:
                    bg_m = og_to_bg_idx.get((mi, gid))
                    fg_m = og_to_fg_idx.get((mi, gid))
                    if bg_m and bg_m["mapping_status"] in ("same_arm", "combined") and bg_m["bg_group_ids"]:
                        for b in bg_m["bg_group_ids"]:
                            arm_tokens.add(f"BG:{b}")
                    elif fg_m and fg_m["mapping_status"] in ("same_arm", "combined") and fg_m["fg_group_ids"]:
                        for f_id in fg_m["fg_group_ids"]:
                            arm_tokens.add(f"FG:{f_id}")
                    else:
                        arm_tokens.add(f"M{mi}:OG:{gid}")

                trial_contrast_specs.append(
                    {
                        "qm": qm,
                        "og_by_id": og_by_id,
                        "an": an,
                        "mi": mi,
                        "ai": ai,
                        "gids": gids,
                        "has_est_ci": has_est_ci,
                        "arm_tokens": arm_tokens,
                    }
                )

        # Precompute non-independence arm sharing within this trial
        est_ci_specs = [cs for cs in trial_contrast_specs if cs["has_est_ci"]]
        shared_est_ci_pairs: list[dict[str, Any]] = []
        shared_all_pairs: list[dict[str, Any]] = []

        def find_shared_for(
            spec: dict[str, Any], pool: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            matches: list[dict[str, Any]] = []
            for other in pool:
                if other["mi"] == spec["mi"] and other["ai"] == spec["ai"]:
                    continue
                same_m_shared_ogs = (
                    sorted(set(spec["gids"]) & set(other["gids"]))
                    if other["mi"] == spec["mi"]
                    else []
                )
                shared_tokens = sorted(spec["arm_tokens"] & other["arm_tokens"])
                if same_m_shared_ogs or shared_tokens:
                    matches.append(
                        {
                            "measure_index": other["mi"],
                            "analysis_index": other["ai"],
                            "other_group_ids": other["gids"],
                            "shared_og_ids_same_measure": same_m_shared_ogs,
                            "shared_underlying_arms": shared_tokens,
                        }
                    )
            return matches

        for i_idx, sp_a in enumerate(est_ci_specs):
            for sp_b in est_ci_specs[i_idx + 1 :]:
                shared_toks = sorted(sp_a["arm_tokens"] & sp_b["arm_tokens"])
                same_ogs = (
                    sorted(set(sp_a["gids"]) & set(sp_b["gids"]))
                    if sp_a["mi"] == sp_b["mi"]
                    else []
                )
                if same_ogs or shared_toks:
                    shared_est_ci_pairs.append(
                        {
                            "contrast_a": f"m{sp_a['mi']}_a{sp_a['ai']}",
                            "contrast_b": f"m{sp_b['mi']}_a{sp_b['ai']}",
                            "shared_og_ids_same_measure": same_ogs,
                            "shared_underlying_arms": shared_toks,
                        }
                    )

        for i_idx, sp_a in enumerate(trial_contrast_specs):
            for sp_b in trial_contrast_specs[i_idx + 1 :]:
                shared_toks = sorted(sp_a["arm_tokens"] & sp_b["arm_tokens"])
                same_ogs = (
                    sorted(set(sp_a["gids"]) & set(sp_b["gids"]))
                    if sp_a["mi"] == sp_b["mi"]
                    else []
                )
                if same_ogs or shared_toks:
                    shared_all_pairs.append(
                        {
                            "contrast_a": f"m{sp_a['mi']}_a{sp_a['ai']}",
                            "contrast_b": f"m{sp_b['mi']}_a{sp_b['ai']}",
                            "shared_og_ids_same_measure": same_ogs,
                            "shared_underlying_arms": shared_toks,
                        }
                    )

        same_arm_est_ci_cnt = 0

        def extract_bg_demo_rows(bg_id: str, target_domains: tuple[str, ...]) -> list[dict[str, str]]:
            out_rows: list[dict[str, str]] = []
            for drow in demo_by_nct_bg.get((nct_id, bg_id), []):
                if drow["measure_domain"] in target_domains:
                    out_rows.append(
                        {
                            "baseline_measure_index": drow["baseline_measure_index"],
                            "measure_domain": drow["measure_domain"],
                            "measure_title": drow["measure_title"],
                            "param_type": drow["param_type"],
                            "unit_of_measure": drow["unit_of_measure"],
                            "class_title": drow["class_title"],
                            "category_title": drow["category_title"],
                            "value": drow["value"],
                            "spread": drow["spread"],
                            "class_denom_count": drow["class_denom_count"],
                            "value_path": drow["value_path"],
                        }
                    )
            return out_rows

        for spec in trial_contrast_specs:
            qm = spec["qm"]
            og_by_id = spec["og_by_id"]
            an = spec["an"]
            mi = spec["mi"]
            ai = spec["ai"]
            gids = spec["gids"]
            has_est_ci = spec["has_est_ci"]

            c_flags = list(qm.get("rederivation_flags", []))
            if not has_est_ci:
                c_flags.append("missing_estimate_or_ci_limits_on_qualifying_measure")
            if len(gids) != 2:
                c_flags.append(f"group_ids_count_is_{len(gids)}")

            shared_est = find_shared_for(spec, est_ci_specs) if has_est_ci else []
            shared_all = find_shared_for(spec, trial_contrast_specs)

            crow = {col: "" for col in CONTRASTS_CSV_COLUMNS}
            crow.update(
                {
                    "nct_id": nct_id,
                    "measure_index": str(mi),
                    "measure_title": qm["title"],
                    "analysis_index": str(ai),
                    "has_estimate_and_both_ci_limits": "yes" if has_est_ci else "no",
                    "rederivation_flags_json": json.dumps(c_flags, ensure_ascii=False),
                    "groups_compared_json": json.dumps(gids, ensure_ascii=False),
                    "groups_compared_count": str(len(gids)),
                    "estimate_type_as_posted": an["param_type"],
                    "estimate": an["param_value"],
                    "ci_percent": an["ci_pct_value"],
                    "ci_lower": an["ci_lower_limit"],
                    "ci_upper": an["ci_upper_limit"],
                    "p_value_as_posted": an["p_value"],
                    "shares_arm_among_est_and_ci_contrasts": (
                        "yes" if shared_est else ("no" if has_est_ci else "not_applicable")
                    ),
                    "shared_arm_est_and_ci_contrasts_json": json.dumps(shared_est, ensure_ascii=False),
                    "shares_arm_among_all_posted_contrasts": "yes" if shared_all else "no",
                    "shared_arm_all_posted_contrasts_json": json.dumps(shared_all, ensure_ascii=False),
                }
            )

            # Check quote verification on this contrast's Stage 2 items
            contrast_q_failed = False
            for gid in gids:
                if (
                    f"og_to_bg[{mi}][{gid}]" in failed_quote_contexts
                    or f"og_to_fg[{mi}][{gid}]" in failed_quote_contexts
                    or f"analysis_arm_roles[{mi}][{ai}][{gid}]" in failed_quote_contexts
                ):
                    contrast_q_failed = True
            crow["contrast_quote_verification_status"] = (
                "quote_verification_failed" if contrast_q_failed else "verified"
            )

            if len(gids) >= 1:
                g1 = gids[0]
                m1_bg = og_to_bg_idx[(mi, g1)]
                m1_fg = og_to_fg_idx[(mi, g1)]
                r1_role = role_idx[(mi, ai, g1)]
                crow.update(
                    {
                        "arm_1_og_id": g1,
                        "arm_1_og_title": og_by_id[g1]["title"],
                        "arm_1_role": r1_role["arm_role"],
                        "arm_1_og_to_bg_status": m1_bg["mapping_status"],
                        "arm_1_bg_ids_json": json.dumps(m1_bg["bg_group_ids"], ensure_ascii=False),
                        "arm_1_og_to_fg_status": m1_fg["mapping_status"],
                        "arm_1_fg_ids_json": json.dumps(m1_fg["fg_group_ids"], ensure_ascii=False),
                    }
                )

            if len(gids) >= 2:
                g2 = gids[1]
                m2_bg = og_to_bg_idx[(mi, g2)]
                m2_fg = og_to_fg_idx[(mi, g2)]
                r2_role = role_idx[(mi, ai, g2)]
                crow.update(
                    {
                        "arm_2_og_id": g2,
                        "arm_2_og_title": og_by_id[g2]["title"],
                        "arm_2_role": r2_role["arm_role"],
                        "arm_2_og_to_bg_status": m2_bg["mapping_status"],
                        "arm_2_bg_ids_json": json.dumps(m2_bg["bg_group_ids"], ensure_ascii=False),
                        "arm_2_og_to_fg_status": m2_fg["mapping_status"],
                        "arm_2_fg_ids_json": json.dumps(m2_fg["fg_group_ids"], ensure_ascii=False),
                    }
                )

            if len(gids) != 2:
                crow["both_arms_map_same_arm_bg"] = "no"
                crow["arm_demographics_omitted_reason"] = (
                    f"only_{len(gids)}_group_id_posted_in_analysis_groupIds"
                )
                crow["baseline_has_other_non_total_bg_arms"] = "not_applicable"
                crow["other_non_total_bg_ids_json"] = "[]"
            else:
                g1, g2 = gids[0], gids[1]
                m1_bg = og_to_bg_idx[(mi, g1)]
                m2_bg = og_to_bg_idx[(mi, g2)]
                s1_st = m1_bg["mapping_status"]
                s2_st = m2_bg["mapping_status"]
                bg1_ids = m1_bg["bg_group_ids"]
                bg2_ids = m2_bg["bg_group_ids"]

                both_same = (
                    s1_st == "same_arm"
                    and len(bg1_ids) == 1
                    and bg1_ids[0] in bg_by_id
                    and not bg_by_id[bg1_ids[0]]["is_total_group"]
                    and s2_st == "same_arm"
                    and len(bg2_ids) == 1
                    and bg2_ids[0] in bg_by_id
                    and not bg_by_id[bg2_ids[0]]["is_total_group"]
                )
                if both_same:
                    if has_est_ci:
                        same_arm_est_ci_cnt += 1
                    bg1_id = bg1_ids[0]
                    bg2_id = bg2_ids[0]
                    b1_n = int(bg_by_id[bg1_id]["denom_count"])
                    o1_n = int(og_by_id[g1]["denom_count"])
                    att1_n = b1_n - o1_n

                    b2_n = int(bg_by_id[bg2_id]["denom_count"])
                    o2_n = int(og_by_id[g2]["denom_count"])
                    att2_n = b2_n - o2_n

                    other_bgs = [b for b in non_total_bg_ids if b not in (bg1_id, bg2_id)]

                    crow.update(
                        {
                            "both_arms_map_same_arm_bg": "yes",
                            "arm_demographics_omitted_reason": "",
                            "arm_1_bg_id": bg1_id,
                            "arm_1_baseline_n": str(b1_n),
                            "arm_1_analysed_n": str(o1_n),
                            "arm_1_attrition_n": str(att1_n),
                            "arm_1_attrition_pct_of_baseline": fmt_pct(att1_n, b1_n),
                            "arm_2_bg_id": bg2_id,
                            "arm_2_baseline_n": str(b2_n),
                            "arm_2_analysed_n": str(o2_n),
                            "arm_2_attrition_n": str(att2_n),
                            "arm_2_attrition_pct_of_baseline": fmt_pct(att2_n, b2_n),
                            "arm_1_race_rows_json": json.dumps(
                                extract_bg_demo_rows(bg1_id, ("race", "race_and_ethnicity")),
                                ensure_ascii=False,
                            ),
                            "arm_1_ethnicity_rows_json": json.dumps(
                                extract_bg_demo_rows(bg1_id, ("ethnicity", "race_and_ethnicity")),
                                ensure_ascii=False,
                            ),
                            "arm_1_sex_rows_json": json.dumps(
                                extract_bg_demo_rows(bg1_id, ("sex",)),
                                ensure_ascii=False,
                            ),
                            "arm_2_race_rows_json": json.dumps(
                                extract_bg_demo_rows(bg2_id, ("race", "race_and_ethnicity")),
                                ensure_ascii=False,
                            ),
                            "arm_2_ethnicity_rows_json": json.dumps(
                                extract_bg_demo_rows(bg2_id, ("ethnicity", "race_and_ethnicity")),
                                ensure_ascii=False,
                            ),
                            "arm_2_sex_rows_json": json.dumps(
                                extract_bg_demo_rows(bg2_id, ("sex",)),
                                ensure_ascii=False,
                            ),
                            "baseline_has_other_non_total_bg_arms": "yes" if other_bgs else "no",
                            "other_non_total_bg_ids_json": json.dumps(other_bgs, ensure_ascii=False),
                        }
                    )
                else:
                    reasons: list[str] = []
                    if s1_st != "same_arm":
                        extra1 = f"({'+'.join(bg1_ids)})" if bg1_ids else ""
                        reasons.append(f"arm_1_{g1}_is_{s1_st}{extra1}")
                    if s2_st != "same_arm":
                        extra2 = f"({'+'.join(bg2_ids)})" if bg2_ids else ""
                        reasons.append(f"arm_2_{g2}_is_{s2_st}{extra2}")
                    mapped_union = set(bg1_ids) | set(bg2_ids)
                    other_bgs = [b for b in non_total_bg_ids if b not in mapped_union]
                    crow.update(
                        {
                            "both_arms_map_same_arm_bg": "no",
                            "arm_demographics_omitted_reason": "; ".join(reasons),
                            "baseline_has_other_non_total_bg_arms": "yes" if other_bgs else "no",
                            "other_non_total_bg_ids_json": json.dumps(other_bgs, ensure_ascii=False),
                        }
                    )

            contrasts_rows.append(crow)

        # Build trial_alignment.csv row
        m0_align = per_measure_alignment[0]
        domains_set = set(t1["demographic_measures_present"])
        all_same_bg = all(x["mapping_status"] == "same_arm" for x in adjudicated["og_to_bg"])
        all_same_fg = all(x["mapping_status"] == "same_arm" for x in adjudicated["og_to_fg"])

        trow = {
            "nct_id": nct_id,
            "record_status": "ok",
            "coding_status": "fully_coded",
            "not_coded_reason": "",
            "qualifying_measure_indices_json": json.dumps(
                t1["active_qualifying_measure_indices"], ensure_ascii=False
            ),
            "qualifying_measures_count": str(len(t1["active_qualifying_measure_indices"])),
            "rederivation_has_disagreement": (
                "yes" if t1["rederivation_disagreements"] else "no"
            ),
            "rederivation_disagreements_json": json.dumps(
                t1["rederivation_disagreements"], ensure_ascii=False
            ),
            "baseline_total_n": str(baseline_total_n),
            "baseline_total_units": baseline_total_units,
            "period_1_started_total_n": str(period_1_started_total_n),
            "baseline_minus_period_1_started_n": str(base_minus_started_n),
            "baseline_minus_period_1_started_pct_of_started": fmt_pct(
                base_minus_started_n, period_1_started_total_n
            ),
            "baseline_minus_period_1_started_pct_of_baseline": fmt_pct(
                base_minus_started_n, baseline_total_n
            ),
            "first_qualifying_measure_index": str(m0_align["measure_index"]),
            "first_qualifying_measure_analysed_sum_n": str(m0_align["analysed_sum_n"]),
            "baseline_minus_first_measure_analysed_n": str(m0_align["baseline_minus_analysed_n"]),
            "baseline_minus_first_measure_analysed_pct_of_baseline": m0_align[
                "baseline_minus_analysed_pct_of_baseline"
            ],
            "per_qualifying_measure_alignment_json": json.dumps(
                per_measure_alignment, ensure_ascii=False
            ),
            "baseline_table_scope": adjudicated["baseline_table_scope"][
                "includes_outside_hba1c_analysis"
            ],
            "baseline_table_scope_reason_summary": adjudicated["baseline_table_scope"][
                "reason_summary"
            ],
            "baseline_table_scope_quotes_json": json.dumps(
                adjudicated["baseline_table_scope"]["quotes"], ensure_ascii=False
            ),
            "all_ogs_same_arm_bg": "yes" if all_same_bg else "no",
            "og_to_bg_statuses_json": json.dumps(
                [
                    {
                        "measure_index": x["measure_index"],
                        "og_group_id": x["og_group_id"],
                        "mapping_status": x["mapping_status"],
                        "bg_group_ids": x["bg_group_ids"],
                    }
                    for x in adjudicated["og_to_bg"]
                ],
                ensure_ascii=False,
            ),
            "all_ogs_same_arm_fg": "yes" if all_same_fg else "no",
            "og_to_fg_statuses_json": json.dumps(
                [
                    {
                        "measure_index": x["measure_index"],
                        "og_group_id": x["og_group_id"],
                        "mapping_status": x["mapping_status"],
                        "fg_group_ids": x["fg_group_ids"],
                    }
                    for x in adjudicated["og_to_fg"]
                ],
                ensure_ascii=False,
            ),
            "posted_between_arm_contrasts_count": str(len(trial_contrast_specs)),
            "est_and_ci_contrasts_count": str(len(est_ci_specs)),
            "same_arm_bg_est_and_ci_contrasts_count": str(same_arm_est_ci_cnt),
            "has_shared_arm_est_and_ci_contrasts": "yes" if shared_est_ci_pairs else "no",
            "shared_arm_est_and_ci_summary_json": json.dumps(
                shared_est_ci_pairs, ensure_ascii=False
            ),
            "has_shared_arm_all_posted_contrasts": "yes" if shared_all_pairs else "no",
            "shared_arm_all_posted_summary_json": json.dumps(
                shared_all_pairs, ensure_ascii=False
            ),
            "race_table_posted": (
                "yes" if ("race" in domains_set or "race_and_ethnicity" in domains_set) else "no"
            ),
            "ethnicity_table_posted": (
                "yes"
                if ("ethnicity" in domains_set or "race_and_ethnicity" in domains_set)
                else "no"
            ),
            "sex_table_posted": "yes" if "sex" in domains_set else "no",
            "age_table_posted": "yes" if "age" in domains_set else "no",
            "reader_fields_compared": str(comp["fields_compared_count"]),
            "reader_fields_agreed": str(comp["fields_agreed_count"]),
            "reader_fields_disagreed": str(comp["fields_disagreed_count"]),
            "reader_fields_unresolved": str(unresolved_from_dis),
            "adjudicated_quotes_checked": str(qv_adj["quotes_checked"]),
            "adjudicated_quote_failures": str(qv_adj["quote_failures_count"]),
            "reader_1_quotes_checked": str(qv_r1["quotes_checked"]),
            "reader_1_quote_failures": str(qv_r1["quote_failures_count"]),
            "reader_2_quotes_checked": str(qv_r2["quotes_checked"]),
            "reader_2_quote_failures": str(qv_r2["quote_failures_count"]),
        }
        trial_rows.append(trow)

    return mapping_records, contrasts_rows, trial_rows, stage1_meta


def generate_summary_md(
    mapping_records: list[dict[str, Any]],
    contrasts_rows: list[dict[str, str]],
    trial_rows: list[dict[str, str]],
    stage1_meta: dict[str, Any],
) -> str:
    n_assigned = len(stage1_meta["nct_ids"])
    coded_trials = [r["nct_id"] for r in trial_rows if r["coding_status"] == "fully_coded"]
    not_retrieved = [r["nct_id"] for r in trial_rows if r["record_status"] == "not_retrieved"]
    not_parsed = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] != "fully_coded" and r["record_status"] != "not_retrieved"
    ]
    n_coded = len(coded_trials)

    # Re-derivation disagreements
    rederiv_any = [r["nct_id"] for r in trial_rows if r["rederivation_has_disagreement"] == "yes"]
    rederiv_by_type: dict[str, set[str]] = defaultdict(set)
    for nct, t1 in stage1_meta["trials"].items():
        for d in t1.get("rederivation_disagreements", []):
            rederiv_by_type[d["type"]].add(nct)

    # OG-to-BG and OG-to-FG counts
    coded_mappings = [m for m in mapping_records if m["coding_status"] == "fully_coded"]
    total_og_items = sum(len(m["adjudicated"]["og_to_bg"]) for m in coded_mappings)

    bg_status_items: Counter[str] = Counter()
    bg_status_ncts: dict[str, set[str]] = defaultdict(set)
    fg_status_items: Counter[str] = Counter()
    fg_status_ncts: dict[str, set[str]] = defaultdict(set)

    for m in coded_mappings:
        nct = m["nct_id"]
        for item in m["adjudicated"]["og_to_bg"]:
            st = item["mapping_status"]
            bg_status_items[st] += 1
            bg_status_ncts[st].add(nct)
        for item in m["adjudicated"]["og_to_fg"]:
            st = item["mapping_status"]
            fg_status_items[st] += 1
            fg_status_ncts[st].add(nct)

    all_ogs_same_bg_ncts = [r["nct_id"] for r in trial_rows if r["all_ogs_same_arm_bg"] == "yes"]
    not_all_ogs_same_bg_ncts = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and r["all_ogs_same_arm_bg"] == "no"
    ]
    all_ogs_same_fg_ncts = [r["nct_id"] for r in trial_rows if r["all_ogs_same_arm_fg"] == "yes"]
    not_all_ogs_same_fg_ncts = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and r["all_ogs_same_arm_fg"] == "no"
    ]

    # Arm roles: across all posted contrasts (416) and across est+CI contrasts (410)
    est_ci_contrasts = [c for c in contrasts_rows if c["has_estimate_and_both_ci_limits"] == "yes"]
    n_posted_contrasts = len(contrasts_rows)
    n_est_ci_contrasts = len(est_ci_contrasts)

    role_items_all: Counter[str] = Counter()
    role_ncts_all: dict[str, set[str]] = defaultdict(set)
    total_role_slots_all = 0

    role_items_est_ci: Counter[str] = Counter()
    role_ncts_est_ci: dict[str, set[str]] = defaultdict(set)
    total_role_slots_est_ci = 0

    est_ci_set = {
        (c["nct_id"], int(c["measure_index"]), int(c["analysis_index"]))
        for c in est_ci_contrasts
    }
    for m in coded_mappings:
        nct = m["nct_id"]
        for r_item in m["adjudicated"]["analysis_arm_roles"]:
            rv = r_item["arm_role"]
            total_role_slots_all += 1
            role_items_all[rv] += 1
            role_ncts_all[rv].add(nct)
            if (nct, r_item["measure_index"], r_item["analysis_index"]) in est_ci_set:
                total_role_slots_est_ci += 1
                role_items_est_ci[rv] += 1
                role_ncts_est_ci[rv].add(nct)

    # Baseline-table scope (Stage 2 d)
    bscope_ncts: dict[str, list[str]] = defaultdict(list)
    for r in trial_rows:
        if r["coding_status"] == "fully_coded":
            bscope_ncts[r["baseline_table_scope"]].append(r["nct_id"])

    # Stage 3 derived counts
    b_eq_started = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and int(r["baseline_minus_period_1_started_n"]) == 0
    ]
    b_lt_started = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and int(r["baseline_minus_period_1_started_n"]) < 0
    ]
    b_gt_started = [
        r["nct_id"]
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and int(r["baseline_minus_period_1_started_n"]) > 0
    ]

    # Measure-level baseline vs sum(analysed) across all 210 qualifying measures
    total_qual_measures = 0
    m_eq_base_ncts: set[str] = set()
    m_gt_base_ncts: set[str] = set()
    m_lt_base_ncts: set[str] = set()
    m_eq_cnt, m_gt_cnt, m_lt_cnt = 0, 0, 0
    for r in trial_rows:
        if r["coding_status"] != "fully_coded":
            continue
        for pm in json.loads(r["per_qualifying_measure_alignment_json"]):
            total_qual_measures += 1
            dn = pm["baseline_minus_analysed_n"]
            if dn == 0:
                m_eq_cnt += 1
                m_eq_base_ncts.add(r["nct_id"])
            elif dn > 0:
                m_gt_cnt += 1
                m_gt_base_ncts.add(r["nct_id"])
            else:
                m_lt_cnt += 1
                m_lt_base_ncts.add(r["nct_id"])

    # Contrasts where both arms map same_arm
    same_arm_est_ci = [c for c in est_ci_contrasts if c["both_arms_map_same_arm_bg"] == "yes"]
    omitted_est_ci = [c for c in est_ci_contrasts if c["both_arms_map_same_arm_bg"] == "no"]
    same_arm_all = [c for c in contrasts_rows if c["both_arms_map_same_arm_bg"] == "yes"]
    omitted_all = [c for c in contrasts_rows if c["both_arms_map_same_arm_bg"] == "no"]

    omitted_reasons_est_ci: dict[str, set[str]] = defaultdict(set)
    omitted_reasons_est_ci_cnt: Counter[str] = Counter()
    for c in omitted_est_ci:
        # group by structural pattern (combined vs no_counterpart vs single_group_id vs unresolved)
        rs = c["arm_demographics_omitted_reason"]
        if "only_" in rs:
            bucket = "only_one_group_id_posted_in_analysis"
        elif "no_counterpart" in rs:
            bucket = "one_or_both_arms_no_counterpart_in_baseline"
        elif "combined" in rs:
            bucket = "one_or_both_arms_combined_across_multiple_baseline_groups"
        else:
            bucket = "one_or_both_arms_unresolved"
        omitted_reasons_est_ci[bucket].add(c["nct_id"])
        omitted_reasons_est_ci_cnt[bucket] += 1

    # Zero vs positive attrition among same_arm est+CI contrasts
    zero_att_est_ci = [
        c
        for c in same_arm_est_ci
        if int(c["arm_1_attrition_n"]) == 0 and int(c["arm_2_attrition_n"]) == 0
    ]
    nonzero_att_est_ci = [
        c
        for c in same_arm_est_ci
        if int(c["arm_1_attrition_n"]) != 0 or int(c["arm_2_attrition_n"]) != 0
    ]

    # Demographic availability among same_arm est+CI contrasts
    same_arm_with_race = [c for c in same_arm_est_ci if c["arm_1_race_rows_json"] != "[]"]
    same_arm_with_eth = [c for c in same_arm_est_ci if c["arm_1_ethnicity_rows_json"] != "[]"]
    same_arm_with_sex = [c for c in same_arm_est_ci if c["arm_1_sex_rows_json"] != "[]"]

    # Non-independence (shared arms)
    shared_trials_est_ci = [
        r for r in trial_rows if r["has_shared_arm_est_and_ci_contrasts"] == "yes"
    ]
    indep_trials_est_ci = [
        r
        for r in trial_rows
        if r["coding_status"] == "fully_coded" and r["has_shared_arm_est_and_ci_contrasts"] == "no"
    ]
    shared_contrasts_est_ci = [
        c for c in est_ci_contrasts if c["shares_arm_among_est_and_ci_contrasts"] == "yes"
    ]

    shared_trials_all = [
        r for r in trial_rows if r["has_shared_arm_all_posted_contrasts"] == "yes"
    ]
    shared_contrasts_all = [
        c for c in contrasts_rows if c["shares_arm_among_all_posted_contrasts"] == "yes"
    ]

    # Extra non-total BG arms outside a specific 2-arm contrast
    other_bg_est_ci = [
        c for c in est_ci_contrasts if c["baseline_has_other_non_total_bg_arms"] == "yes"
    ]

    # Inter-reader agreement BEFORE adjudication
    cat_tot: Counter[str] = Counter()
    cat_agr: Counter[str] = Counter()
    cat_dis_ncts: dict[str, set[str]] = defaultdict(set)
    trials_any_dis: set[str] = set()
    unresolved_from_dis_total = 0
    unresolved_from_dis_ncts: set[str] = set()

    for m in coded_mappings:
        nct = m["nct_id"]
        if m["unresolved_from_disagreement_count"] > 0:
            unresolved_from_dis_total += m["unresolved_from_disagreement_count"]
            unresolved_from_dis_ncts.add(nct)
        for f in m["comparison"]["fields"]:
            fc = f["field_category"]
            cat_tot[fc] += 1
            if f["agreed"]:
                cat_agr[fc] += 1
            else:
                cat_dis_ncts[fc].add(nct)
                trials_any_dis.add(nct)

    tot_fields_all = sum(cat_tot.values())
    tot_agr_all = sum(cat_agr.values())
    tot_dis_all = tot_fields_all - tot_agr_all

    # Quote verification counts
    adj_q_checked = sum(m["quote_verification"]["adjudicated"]["quotes_checked"] for m in coded_mappings)
    adj_q_fail = sum(m["quote_verification"]["adjudicated"]["quote_failures_count"] for m in coded_mappings)
    adj_q_fail_ncts = [
        m["nct_id"]
        for m in coded_mappings
        if m["quote_verification"]["adjudicated"]["quote_failures_count"] > 0
    ]

    r1_q_checked = sum(m["quote_verification"]["reading_1"]["quotes_checked"] for m in coded_mappings)
    r1_q_fail = sum(m["quote_verification"]["reading_1"]["quote_failures_count"] for m in coded_mappings)
    r1_q_fail_ncts = [
        m["nct_id"]
        for m in coded_mappings
        if m["quote_verification"]["reading_1"]["quote_failures_count"] > 0
    ]

    r2_q_checked = sum(m["quote_verification"]["reading_2"]["quotes_checked"] for m in coded_mappings)
    r2_q_fail = sum(m["quote_verification"]["reading_2"]["quote_failures_count"] for m in coded_mappings)
    r2_q_fail_ncts = [
        m["nct_id"]
        for m in coded_mappings
        if m["quote_verification"]["reading_2"]["quote_failures_count"] > 0
    ]

    adjudicator_q_checked = sum(
        m["adjudication"]["adjudicator_quote_checks"]
        for m in coded_mappings
        if m["adjudication"] is not None
    )
    adjudicator_q_fail = sum(
        len(m["adjudication"]["adjudicator_quote_failures"])
        for m in coded_mappings
        if m["adjudication"] is not None
    )
    adjudicator_q_fail_ncts = [
        m["nct_id"]
        for m in coded_mappings
        if m["adjudication"] is not None and len(m["adjudication"]["adjudicator_quote_failures"]) > 0
    ]

    md: list[str] = []
    md.append("# Baseline-to-Outcome Population Alignment Audit Summary (201 Qualifying T2D HbA1c Trials)")
    md.append("")
    md.append("## Scope and Non-Interpretation Notice")
    md.append(
        "- **Source**: Stored ClinicalTrials.gov REST API v2 records (`records/{NCT_ID}.json.gz`, API version `2.0.5`, data timestamp `2026-11-13T10:00:06`), with zero network calls."
    )
    md.append(
        "- **Missingness and error separation**: `not_stated` (record omits the information), `unresolved` (record text does not decide or readers disagreed and the record text did not decide), `not_retrieved` (stored file missing), and `could_not_be_parsed` (JSON or reader failure) are kept strictly separate throughout all outputs."
    )
    md.append(
        "- **Non-interpretation**: Nothing in this summary recommends a cohort or statistical model, interprets clinical results, or describes any record or extraction as validated."
    )
    md.append("")
    md.append("## 1. Trial Accounting, Trials Not Coded, and Re-Derivation Check")
    md.append(f"- **Assigned qualifying trials**: **{n_assigned} / {n_assigned}** — NCT IDs: {fmt_nct_list(stage1_meta['nct_ids'])}")
    md.append(f"- **Fully coded trials (`coding_status == 'fully_coded'`)**: **{n_coded} / {n_assigned}** — NCT IDs: {fmt_nct_list(coded_trials)}")
    md.append(f"- **Trials not coded due to `not_retrieved`**: **{len(not_retrieved)} / {n_assigned}** — NCT IDs: {fmt_nct_list(not_retrieved)}")
    md.append(f"- **Trials not coded due to `could_not_be_parsed`**: **{len(not_parsed)} / {n_assigned}** — NCT IDs: {fmt_nct_list(not_parsed)}")
    md.append("")
    md.append("### 1.1 Deterministic Re-Derivation vs. `cohort_sheet.csv` and `analyses.csv`")
    md.append(
        "*Rule*: Stage 1 re-derived qualifying HbA1c primary measures and their posted between-arm analyses directly from `records/{NCT_ID}.json.gz` and compared them against `cohort_sheet.csv` (`210` qualifying measures across `201` trials) and `analyses.csv` (`416` posted between-arm analyses on those `210` measures, of which `410` post `estimate`, `ci_lower`, and `ci_upper`). Neither file was silently altered; all `210` measures and `416` posted between-arm analyses are retained and flagged explicitly."
    )
    md.append("")
    md.append(f"- **Trials with zero re-derivation flags**: **{n_coded - len(rederiv_any)} / {n_coded}** — NCT IDs: {fmt_nct_list(set(coded_trials) - set(rederiv_any))}")
    md.append(f"- **Trials with >= 1 re-derivation flag**: **{len(rederiv_any)} / {n_coded}** — NCT IDs: {fmt_nct_list(rederiv_any)}")
    for rtype, rncts in sorted(rederiv_by_type.items()):
        md.append(f"  - `{rtype}`: **{len(rncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(rncts)}")
    md.append("")

    md.append("## 2. Stage 2(a): Outcome-Group to Baseline-Group Mapping (`og_to_bg`)")
    md.append(
        f"*Rule & denominators*: Reported first across all **{total_og_items}** outcome groups (`OG`s) belonging to the **{total_qual_measures}** qualifying HbA1c primary measures (`n / {total_og_items}`), and second at the trial level (`n / {n_coded}` fully coded trials; a trial with multiple `OG` mapping statuses appears in each applicable row)."
    )
    md.append("")
    md.append(f"| `mapping_status` | Outcome Groups (`n / {total_og_items}`) | Distinct Trials (`n / {n_coded}`) | NCT IDs |")
    md.append("|---|---|---|---|")
    for st in ("same_arm", "combined", "no_counterpart", "unresolved"):
        md.append(
            f"| `{st}` | {bg_status_items[st]} / {total_og_items} | {len(bg_status_ncts[st])} / {n_coded} | {fmt_nct_list(bg_status_ncts[st])} |"
        )
    md.append("")
    md.append(f"- **Trials where ALL qualifying-measure `OG`s map `same_arm` to a single non-Total `BG`**: **{len(all_ogs_same_bg_ncts)} / {n_coded}** — NCT IDs: {fmt_nct_list(all_ogs_same_bg_ncts)}")
    md.append(f"- **Trials with >= 1 `OG` NOT mapping `same_arm` to `BG`**: **{len(not_all_ogs_same_bg_ncts)} / {n_coded}** — NCT IDs: {fmt_nct_list(not_all_ogs_same_bg_ncts)}")
    md.append("")

    md.append("## 3. Stage 2(b): Outcome-Group to Participant-Flow-Group Mapping (`og_to_fg`)")
    md.append(f"| `mapping_status` | Outcome Groups (`n / {total_og_items}`) | Distinct Trials (`n / {n_coded}`) | NCT IDs |")
    md.append("|---|---|---|---|")
    for st in ("same_arm", "combined", "no_counterpart", "unresolved"):
        md.append(
            f"| `{st}` | {fg_status_items[st]} / {total_og_items} | {len(fg_status_ncts[st])} / {n_coded} | {fmt_nct_list(fg_status_ncts[st])} |"
        )
    md.append("")
    md.append(f"- **Trials where ALL qualifying-measure `OG`s map `same_arm` to `FG`**: **{len(all_ogs_same_fg_ncts)} / {n_coded}** — NCT IDs: {fmt_nct_list(all_ogs_same_fg_ncts)}")
    md.append(f"- **Trials with >= 1 `OG` NOT mapping `same_arm` to `FG`**: **{len(not_all_ogs_same_fg_ncts)} / {n_coded}** — NCT IDs: {fmt_nct_list(not_all_ogs_same_fg_ncts)}")
    md.append("")

    md.append("## 4. Stage 2(c): Arm Roles in Qualifying Between-Arm Analyses (`analysis_arm_roles`)")
    md.append(
        f"*Rule & denominators*: One role per posted `groupId` in `analyses[].groupIds`. Reported for both the **{n_est_ci_contrasts}** contrasts posting `estimate`, `ci_lower`, and `ci_upper` (**{total_role_slots_est_ci}** contrast-arm slots) and all **{n_posted_contrasts}** posted between-arm analyses on the `{total_qual_measures}` qualifying measures (**{total_role_slots_all}** contrast-arm slots)."
    )
    md.append("")
    md.append(
        f"| `arm_role` | Contrast-Arm Slots, Est+CI (`n / {total_role_slots_est_ci}`) | Trials, Est+CI (`n / {n_coded}`) | Contrast-Arm Slots, All Posted (`n / {total_role_slots_all}`) | NCT IDs (Est+CI) |"
    )
    md.append("|---|---|---|---|---|")
    for rv in (
        "experimental",
        "comparator_placebo",
        "comparator_active",
        "comparator_same_drug_other_dose",
        "other",
        "unresolved",
    ):
        md.append(
            f"| `{rv}` | {role_items_est_ci[rv]} / {total_role_slots_est_ci} | {len(role_ncts_est_ci[rv])} / {n_coded} | {role_items_all[rv]} / {total_role_slots_all} | {fmt_nct_list(role_ncts_est_ci[rv])} |"
        )
    md.append("")

    md.append("## 5. Stage 2(d) & Stage 3: Baseline-Table Scope and Trial-Level Denominator Comparisons")
    md.append("### 5.1 Stage 2(d): Does the Baseline Table Include Participants or Arms Not in the HbA1c Analysis?")
    md.append(f"| `baseline_table_scope` | Trials (`n / {n_coded}`) | NCT IDs |")
    md.append("|---|---|---|")
    for sv in ("yes", "no", "not_determinable", "unresolved"):
        md.append(f"| `{sv}` | {len(bscope_ncts[sv])} / {n_coded} | {fmt_nct_list(bscope_ncts[sv])} |")
    md.append("")

    md.append("### 5.2 Stage 3 Trial-Level Totals: `baseline_total_n` vs. Period 1 `STARTED` Total (No Thresholds Applied)")
    md.append(f"- **`baseline_total_n == period_1_started_total_n` (difference = 0)**: **{len(b_eq_started)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(b_eq_started)}")
    md.append(f"- **`baseline_total_n < period_1_started_total_n` (difference < 0)**: **{len(b_lt_started)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(b_lt_started)}")
    md.append(f"- **`baseline_total_n > period_1_started_total_n` (difference > 0)**: **{len(b_gt_started)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(b_gt_started)}")
    md.append("")

    md.append(f"### 5.3 Stage 3 Measure-Level Totals: `baseline_total_n` vs. Sum of `number_analysed` Across {total_qual_measures} Qualifying Measures")
    md.append(f"- **`baseline_total_n == analysed_sum_n` (difference = 0)**: **{m_eq_cnt} / {total_qual_measures}** measures across **{len(m_eq_base_ncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(m_eq_base_ncts)}")
    md.append(f"- **`baseline_total_n > analysed_sum_n` (difference > 0)**: **{m_gt_cnt} / {total_qual_measures}** measures across **{len(m_gt_base_ncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(m_gt_base_ncts)}")
    md.append(f"- **`baseline_total_n < analysed_sum_n` (difference < 0)**: **{m_lt_cnt} / {total_qual_measures}** measures across **{len(m_lt_base_ncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(m_lt_base_ncts)}")
    md.append("")

    md.append("## 6. Stage 3 Contrast-Level Demographic Alignment and Non-Independence")
    md.append("### 6.1 Arm-Level Demographic Availability (`both_arms_map_same_arm_bg`)")
    md.append(
        f"- **Est+CI contrasts where BOTH arms map `same_arm` to `BG` (arm-level baseline `n`, analysed `n`, attrition, and verbatim race/ethnicity/sex rows populated)**: **{len(same_arm_est_ci)} / {n_est_ci_contrasts}** contrasts across **{len({c['nct_id'] for c in same_arm_est_ci})} / {n_coded}** trials (and **{len(same_arm_all)} / {n_posted_contrasts}** across all posted contrasts) — NCT IDs: {fmt_nct_list({c['nct_id'] for c in same_arm_est_ci})}"
    )
    md.append(
        f"- **Est+CI contrasts where arm-level demographics are omitted (`both_arms_map_same_arm_bg == 'no'`)**: **{len(omitted_est_ci)} / {n_est_ci_contrasts}** contrasts across **{len({c['nct_id'] for c in omitted_est_ci})} / {n_coded}** trials (and **{len(omitted_all)} / {n_posted_contrasts}** across all posted contrasts) — NCT IDs: {fmt_nct_list({c['nct_id'] for c in omitted_est_ci})}"
    )
    for bucket, bncts in sorted(omitted_reasons_est_ci.items()):
        md.append(
            f"  - Reason `{bucket}`: **{omitted_reasons_est_ci_cnt[bucket]} / {n_est_ci_contrasts}** contrasts across **{len(bncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(bncts)}"
        )
    md.append(
        f"- **Among the {len(same_arm_est_ci)} `same_arm` Est+CI contrasts**:"
    )
    md.append(
        f"  - Zero attrition in both arms (`arm_1_attrition_n == 0` and `arm_2_attrition_n == 0`): **{len(zero_att_est_ci)} / {len(same_arm_est_ci)}** contrasts across **{len({c['nct_id'] for c in zero_att_est_ci})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in zero_att_est_ci})}"
    )
    md.append(
        f"  - Non-zero attrition in >= 1 arm: **{len(nonzero_att_est_ci)} / {len(same_arm_est_ci)}** contrasts across **{len({c['nct_id'] for c in nonzero_att_est_ci})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in nonzero_att_est_ci})}"
    )
    md.append(
        f"  - Posted baseline `race` rows present for the two arms: **{len(same_arm_with_race)} / {len(same_arm_est_ci)}** contrasts across **{len({c['nct_id'] for c in same_arm_with_race})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in same_arm_with_race})}"
    )
    md.append(
        f"  - Posted baseline `ethnicity` rows present for the two arms: **{len(same_arm_with_eth)} / {len(same_arm_est_ci)}** contrasts across **{len({c['nct_id'] for c in same_arm_with_eth})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in same_arm_with_eth})}"
    )
    md.append(
        f"  - Posted baseline `sex` rows present for the two arms: **{len(same_arm_with_sex)} / {len(same_arm_est_ci)}** contrasts across **{len({c['nct_id'] for c in same_arm_with_sex})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in same_arm_with_sex})}"
    )
    md.append(
        f"- **Est+CI contrasts where `baselineCharacteristicsModule` also contains >= 1 other non-Total `BG` group outside the contrast's two arms**: **{len(other_bg_est_ci)} / {n_est_ci_contrasts}** contrasts across **{len({c['nct_id'] for c in other_bg_est_ci})} / {n_coded}** trials — NCT IDs: {fmt_nct_list({c['nct_id'] for c in other_bg_est_ci})}"
    )
    md.append("")

    md.append("### 6.2 Non-Independence: Contrasts Sharing an Arm, Listed by Trial")
    md.append(
        f"- **Trials with >= 2 Est+CI contrasts sharing an arm**: **{len(shared_trials_est_ci)} / {n_coded}** trials (**{len(shared_contrasts_est_ci)} / {n_est_ci_contrasts}** Est+CI contrasts participate in arm-sharing) — NCT IDs: {fmt_nct_list([r['nct_id'] for r in shared_trials_est_ci])}"
    )
    md.append(
        f"- **Trials with no shared-arm Est+CI contrasts**: **{len(indep_trials_est_ci)} / {n_coded}** trials — NCT IDs: {fmt_nct_list([r['nct_id'] for r in indep_trials_est_ci])}"
    )
    md.append(
        f"- *(Across all {n_posted_contrasts} posted between-arm analyses on qualifying measures, including the 6 without CI limits)*: **{len(shared_trials_all)} / {n_coded}** trials (**{len(shared_contrasts_all)} / {n_posted_contrasts}** analyses) — NCT IDs: {fmt_nct_list([r['nct_id'] for r in shared_trials_all])}"
    )
    md.append("")
    md.append("#### Per-Trial List of Shared-Arm Contrasts (Est+CI Contrasts)")
    md.append("| NCT ID | Est+CI Contrasts in Trial | Shared-Arm Contrast Pairs (`measure_analysis` pairs & shared underlying arm IDs) |")
    md.append("|---|---|---|")
    for r in shared_trials_est_ci:
        pairs = json.loads(r["shared_arm_est_and_ci_summary_json"])
        formatted_pairs = "; ".join(
            f"{p['contrast_a']} & {p['contrast_b']} ({','.join(p['shared_underlying_arms'])})"
            for p in pairs
        )
        md.append(f"| `{r['nct_id']}` | {r['est_and_ci_contrasts_count']} | {formatted_pairs} |")
    md.append("")

    md.append("## 7. Inter-Reader Agreement Before Adjudication (Per Coded Field)")
    overall_rate = (100.0 * tot_agr_all / tot_fields_all) if tot_fields_all else 0.0
    md.append(
        f"- **Overall pre-adjudication atomic field agreement**: **{tot_agr_all} / {tot_fields_all}** (**{overall_rate:.2f}%**); disagreed on **{tot_dis_all} / {tot_fields_all}** fields across **{len(trials_any_dis)} / {n_coded}** trials — NCT IDs with >= 1 disagreement: {fmt_nct_list(trials_any_dis)}"
    )
    md.append(
        f"- **Trials with 100% pre-adjudication agreement across all Stage 2 fields**: **{n_coded - len(trials_any_dis)} / {n_coded}** — NCT IDs: {fmt_nct_list(set(coded_trials) - trials_any_dis)}"
    )
    md.append(
        f"- **Disagreed fields left `unresolved` after third-reader adjudication**: **{unresolved_from_dis_total} / {tot_dis_all}** disagreed fields (across **{len(unresolved_from_dis_ncts)} / {n_coded}** trials) — NCT IDs: {fmt_nct_list(unresolved_from_dis_ncts)}"
    )
    md.append("")
    md.append("| Stage 2 Coded Field | Agreed Before Adjudication (`n / N`) | Disagreed Before Adjudication (`n / N`) | Pre-Adjudication Agreement Rate | Trials With Disagreement (`n / N` & NCT IDs) |")
    md.append("|---|---|---|---|---|")
    for fc, label in [
        ("og_to_bg", "`a. og_to_bg` (OG-to-BG mapping)"),
        ("og_to_fg", "`b. og_to_fg` (OG-to-FG mapping)"),
        ("analysis_arm_roles", "`c. analysis_arm_roles` (per-contrast arm role)"),
        ("baseline_table_scope", "`d. baseline_table_scope` (baseline scope)"),
    ]:
        tot_f = cat_tot[fc]
        agr_f = cat_agr[fc]
        dis_f = tot_f - agr_f
        rate_f = (100.0 * agr_f / tot_f) if tot_f else 0.0
        dncts = cat_dis_ncts[fc]
        md.append(
            f"| {label} | {agr_f} / {tot_f} | {dis_f} / {tot_f} | {rate_f:.2f}% | {len(dncts)} / {n_coded}: {fmt_nct_list(dncts)} |"
        )
    md.append("")

    def split_fail_kinds(which: str) -> tuple[int, list[str], int, list[str]]:
        v_cnt, d_cnt = 0, 0
        v_ncts: set[str] = set()
        d_ncts: set[str] = set()
        for m in coded_mappings:
            for f_item in m["quote_verification"][which]["failures"]:
                if f_item["error"] == "disallowed_quote_path_for_field":
                    d_cnt += 1
                    d_ncts.add(m["nct_id"])
                else:
                    v_cnt += 1
                    v_ncts.add(m["nct_id"])
        return v_cnt, sorted(v_ncts), d_cnt, sorted(d_ncts)

    adj_vf, adj_vncts, adj_df, adj_dncts = split_fail_kinds("adjudicated")
    r1_vf, r1_vncts, r1_df, r1_dncts = split_fail_kinds("reading_1")
    r2_vf, r2_vncts, r2_df, r2_dncts = split_fail_kinds("reading_2")

    md.append("## 8. Deterministic Quote-Verification Results and Failures")
    md.append(
        "*Rule*: Every quote `{path, text}` was checked deterministically against the uncompressed JSON of `records/{NCT_ID}.json.gz` for (1) exact JSON path existence and exact case-sensitive character-for-character substring match, and (2) strict allowed path domain (`groups[].title/.description` only for `og_to_bg` and `og_to_fg`; arm titles/descriptions/`armGroups` only for `analysis_arm_roles`). Failed quotes are logged verbatim in `mapping.jsonl` and counted below; none were patched."
    )
    md.append("")
    md.append(f"- **Adjudicated final output quote failures (all checks)**: **{adj_q_fail} / {adj_q_checked}** quotes checked across **{len(adj_q_fail_ncts)} / {n_coded}** trials — NCT IDs: {fmt_nct_list(adj_q_fail_ncts)}")
    md.append(f"  - Path missing or substring not verbatim at path: **{adj_vf} / {adj_q_checked}** quotes (**{len(adj_vncts)} / {n_coded}** trials) — NCT IDs: {fmt_nct_list(adj_vncts)}")
    md.append(f"  - Auxiliary quote outside strict field path-domain (`disallowed_quote_path_for_field`): **{adj_df} / {adj_q_checked}** quotes (**{len(adj_dncts)} / {n_coded}** trials) — NCT IDs: {fmt_nct_list(adj_dncts)}")
    md.append(f"- **Independent Reader 1 raw quote failures (all checks)**: **{r1_q_fail} / {r1_q_checked}** quotes checked across **{len(r1_q_fail_ncts)} / {n_coded}** trials (non-verbatim/missing path: **{r1_vf} / {r1_q_checked}**; disallowed auxiliary path: **{r1_df} / {r1_q_checked}**) — NCT IDs: {fmt_nct_list(r1_q_fail_ncts)}")
    md.append(f"- **Independent Reader 2 raw quote failures (all checks)**: **{r2_q_fail} / {r2_q_checked}** quotes checked across **{len(r2_q_fail_ncts)} / {n_coded}** trials (non-verbatim/missing path: **{r2_vf} / {r2_q_checked}**; disallowed auxiliary path: **{r2_df} / {r2_q_checked}**) — NCT IDs: {fmt_nct_list(r2_q_fail_ncts)}")
    md.append(f"- **Third-Reader Adjudicator raw quote failures**: **{adjudicator_q_fail} / {adjudicator_q_checked}** quotes checked across **{len(adjudicator_q_fail_ncts)} / {len(trials_any_dis)}** adjudicated trials — NCT IDs: {fmt_nct_list(adjudicator_q_fail_ncts)}")
    md.append("")
    return "\n".join(md)


def generate_method_md(
    mapping_records: list[dict[str, Any]],
    stage1_meta: dict[str, Any],
) -> str:
    coded = [m for m in mapping_records if m["coding_status"] == "fully_coded"]
    adj_runs = [m for m in coded if m["adjudication"] is not None]

    r_starts = sorted(
        [m["r1_started_at"] for m in coded] + [m["r2_started_at"] for m in coded]
    )
    r_ends = sorted(
        [m["r1_completed_at"] for m in coded] + [m["r2_completed_at"] for m in coded]
    )
    a_starts = sorted(m["adjudication"]["started_at"] for m in adj_runs)
    a_ends = sorted(m["adjudication"]["completed_at"] for m in adj_runs)

    lines: list[str] = []
    lines.append("# Method: Baseline-to-Outcome Population Alignment Audit (201 Qualifying T2D HbA1c Trials)")
    lines.append("")
    lines.append("## 1. Inputs, Isolation, Rate/Concurrency Limits, and Execution Window")
    lines.append("- **Network access**: None (`0` HTTP requests). Read strictly from local `docs/audits/hba1c_cohort_reading/records/{NCT_ID}.json.gz` (ClinicalTrials.gov REST API `2.0.5`, data timestamp `2026-11-13T10:00:06`), the 201 qualifying NCT IDs in `docs/audits/hba1c_cohort_reading/summary.md` section 3.1, and `cohort_sheet.csv` + `analyses.csv` for the re-derivation comparison.")
    lines.append("- **Scoring-reference isolation**: Neither AACT, `civicsample-engine` pipeline outputs, `clinical-trial-populations` site data, nor earlier prose/chat answers were read or used.")
    lines.append("- **Model**: Gemini 4 Carbon (High) via isolated single-use `run_workflow` agents (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`).")
    lines.append(f"- **Worker counts**:")
    lines.append(f"  - Stage 1 deterministic extraction (`scripts/stage1_extract.py`): `1` local Python process (`0` LLM calls).")
    lines.append(f"  - Stage 2 Reader 1 (`hba1c_alignment_reader`): `{len(coded)}` isolated single-trial workers (`1` trial per context).")
    lines.append(f"  - Stage 2 Reader 2 (`hba1c_alignment_reader`): `{len(coded)}` isolated single-trial workers (`1` trial per context, launched concurrently with Reader 1 prior to checkpointing).")
    lines.append(f"  - Stage 2 deterministic comparator (`scripts/stage2_schema_and_prompts.py::compare_stage2_readings`): `0` LLM calls.")
    lines.append(f"  - Stage 2 third-reader Adjudicator (`hba1c_alignment_adjudicator`): `{len(adj_runs)}` isolated single-trial workers (invoked strictly on trials with `>= 1` differing field).")
    lines.append(f"  - Stage 3 derived calculations & deterministic quote verifier (`scripts/stage3_and_deliverables.py`, `scripts/verify_alignment_audit.py`): `0` LLM calls.")
    lines.append(f"  - **Total single-trial LLM worker contexts**: **{2 * len(coded) + len(adj_runs)}**.")
    lines.append("- **Concurrency and context limits**:")
    lines.append("  - Workflow semaphore cap: at most `16` concurrent LLM agent calls (`8` trials dual-read in parallel), with exponential backoff (`3s * attempt`, max `4` attempts).")
    lines.append("  - Prompt sizes across all 201 trials ranged from ~`22 KB` to `105,220` bytes (`NCT00328627`), median `38,485` bytes—well under the ~`192 KB` subagent context truncation ceiling.")
    if r_starts and r_ends:
        lines.append(f"- **Stage 2 dual-reading window (UTC)**: `{r_starts[0]}` to `{r_ends[-1]}`.")
    if a_starts and a_ends:
        lines.append(f"- **Stage 2 adjudication window (UTC)**: `{a_starts[0]}` to `{a_ends[-1]}`.")
    lines.append("")
    lines.append("## 2. Deviations From the Task Specification")
    lines.append("- **Data / analytical / workflow deviations**: None. All 201 trials were processed offline from stored gzipped records; Stage 1 and Stage 3 ran purely in deterministic Python code; Stage 2 used two independent single-trial readers per trial, deterministic comparison, third-reader adjudication only on disagreements, and unpatched deterministic quote verification.")
    lines.append("- **Inclusivity on posted between-arm analyses (`416` vs. `410`)**: Three qualifying measures (`NCT00528372[0]`, `NCT01517412[0]`, `NCT01733758[0]`) post both analyses with `estimate` + both CI limits (`410` across all `210` qualifying measures) and companion/additional between-arm entries in `analyses.csv` lacking CI limits (`6` entries). Rather than silently dropping those `6` entries, Stage 2 and `contrasts.csv` include all `416` posted between-arm analyses and mark `has_estimate_and_both_ci_limits` (`yes` = `410`, `no` = `6`), reporting counts for both denominators in `summary.md`.")
    lines.append("- **Repository commit/push handling**: Neither `michaeldgreenphd/civicsample-engine` nor `michaeldgreenphd/clinical-trial-populations` was committed or pushed to. Because this workspace is a sandboxed git worktree of `michaeldgreenphd/civicsample-engine` without sandboxed `.git` or network write access to `michaeldgreenphd/civicsample-verify-ai-private`, all outputs were left uncommitted in `docs/audits/hba1c_population_alignment/` per instruction.")
    lines.append("- **Initial path typo on `stage1_extract.py`**: An initial `write_file` call wrote `stage1_extract.py` to `/Users/michaelgreen/.gemini/antigravity/worktrees/civicsample-engine/hba1c_population_alignment/scripts/stage1_extract.py` (sibling path outside this worktree) before immediately writing the script to the workspace path `docs/audits/hba1c_population_alignment/scripts/stage1_extract.py`.")
    lines.append("")
    lines.append("## 3. Offline Reproduction and Independent Verification Commands")
    lines.append("From the repository root, with no network access:")
    lines.append("```bash")
    lines.append("python3 docs/audits/hba1c_population_alignment/scripts/stage1_extract.py")
    lines.append("python3 docs/audits/hba1c_population_alignment/scripts/stage3_and_deliverables.py")
    lines.append("python3 docs/audits/hba1c_population_alignment/scripts/verify_alignment_audit.py")
    lines.append("```")
    lines.append("")
    lines.append("## 4. Verbatim Prompts Given to Stage 2 Workers")
    lines.append("### 4.1 Reader System Prompt (`hba1c_alignment_reader`)")
    lines.append("````text")
    lines.append(s2.READER_SYSTEM_PROMPT)
    lines.append("````")
    lines.append("")
    lines.append("### 4.2 Adjudicator System Prompt (`hba1c_alignment_adjudicator`)")
    lines.append("````text")
    lines.append(s2.ADJUDICATOR_SYSTEM_PROMPT)
    lines.append("````")
    lines.append("")
    lines.append("### 4.3 Verbatim Stage 2 Coding Rules Included in Reader & Adjudicator Prompts")
    lines.append("````markdown")
    lines.append(s2.STAGE2_CODING_RULES.rstrip())
    lines.append("````")
    lines.append("")
    lines.append("### 4.4 Reader User Prompt Template")
    lines.append("````text")
    lines.append("Stage 2 alignment coding for ClinicalTrials.gov record {NCT_ID} (Independent Reader {1_or_2}).")
    lines.append("Do NOT call any tools other than `finish`. Code ONLY from the stored record and Stage 1 tables below.")
    lines.append("")
    lines.append("{STAGE2_CODING_RULES}")
    lines.append("")
    lines.append("{RENDERED_STAGE1_TABLES_AND_PATH_ANNOTATED_STORED_RECORD}")
    lines.append("````")
    lines.append("")
    lines.append("### 4.5 Adjudicator User Prompt Template")
    lines.append("````text")
    lines.append("Adjudicate Stage 2 differing fields for ClinicalTrials.gov record {NCT_ID}.")
    lines.append("Do NOT call any tools other than `finish`. Re-read ONLY the stored record and Stage 1 tables below.")
    lines.append("")
    lines.append("{STAGE2_CODING_RULES}")
    lines.append("")
    lines.append("=== DISAGREEMENTS TO ADJUDICATE ===")
    lines.append("{DISAGREEMENTS_JSON}")
    lines.append("")
    lines.append("{RENDERED_STAGE1_TABLES_AND_PATH_ANNOTATED_STORED_RECORD}")
    lines.append("````")
    lines.append("")
    return "\n".join(lines)


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    mapping_records, contrasts_rows, trial_rows, stage1_meta = compute_stage3_outputs()

    with open(ALIGN_DIR / "mapping.jsonl", "w", encoding="utf-8") as f:
        for rec in mapping_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    write_csv(ALIGN_DIR / "contrasts.csv", CONTRASTS_CSV_COLUMNS, contrasts_rows)
    write_csv(ALIGN_DIR / "trial_alignment.csv", TRIAL_ALIGNMENT_CSV_COLUMNS, trial_rows)

    summary_md = generate_summary_md(mapping_records, contrasts_rows, trial_rows, stage1_meta)
    (ALIGN_DIR / "summary.md").write_text(summary_md + "\n", encoding="utf-8")

    method_md = generate_method_md(mapping_records, stage1_meta)
    (ALIGN_DIR / "method.md").write_text(method_md + "\n", encoding="utf-8")

    coded_cnt = sum(1 for r in trial_rows if r["coding_status"] == "fully_coded")
    print(
        f"Stage 3 deliverables written: coded={coded_cnt}/{len(trial_rows)}, "
        f"contrasts={len(contrasts_rows)}, mapping_lines={len(mapping_records)}"
    )


if __name__ == "__main__":
    main()
