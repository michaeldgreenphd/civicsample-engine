#!/usr/bin/env python3
"""Stage 1 deterministic extraction for the 201 qualifying T2D HbA1c trials.

Reads ONLY:
- The 201 qualifying NCT IDs from docs/audits/hba1c_cohort_reading/summary.md section 3.1.
- The stored registry records in docs/audits/hba1c_cohort_reading/records/{NCT_ID}.json.gz.
- docs/audits/hba1c_cohort_reading/cohort_sheet.csv and analyses.csv to identify the
  qualifying HbA1c primary measure(s) and their posted between-arm analyses, and
  independently re-derives them from the raw JSON record, flagging any disagreement
  without silently correcting either.

Writes:
- docs/audits/hba1c_population_alignment/groups.csv
- docs/audits/hba1c_population_alignment/arm_demographics.csv
- docs/audits/hba1c_population_alignment/intermediate/stage1_trials.json

No network access and no LLM calls are used in Stage 1.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import pathlib
import re
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
COHORT_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_cohort_reading"
ALIGN_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_population_alignment"
RECORDS_DIR = COHORT_DIR / "records"
INTERMEDIATE_DIR = ALIGN_DIR / "intermediate"

GROUPS_CSV_COLUMNS = [
    "nct_id",
    "module",
    "measure_index",
    "measure_title",
    "measure_title_path",
    "population_description",
    "population_description_path",
    "group_index",
    "group_id",
    "is_total_group",
    "group_title",
    "group_title_path",
    "group_description",
    "group_description_path",
    "denom_units",
    "denom_units_path",
    "denom_count",
    "denom_count_path",
    "class_denoms_json",
    "period_0_title",
    "period_0_started",
    "period_0_started_path",
    "period_0_completed",
    "period_0_completed_path",
    "period_1_title",
    "period_1_started",
    "period_1_started_path",
    "period_1_completed",
    "period_1_completed_path",
    "period_2_title",
    "period_2_started",
    "period_2_started_path",
    "period_2_completed",
    "period_2_completed_path",
    "flow_periods_json",
    "measure_analyses_group_ids_json",
]

ARM_DEMOGRAPHICS_CSV_COLUMNS = [
    "nct_id",
    "baseline_measure_index",
    "measure_domain",
    "measure_title",
    "measure_title_path",
    "measure_population_description",
    "measure_population_description_path",
    "param_type",
    "dispersion_type",
    "unit_of_measure",
    "class_index",
    "class_title",
    "class_title_path",
    "category_index",
    "category_title",
    "category_title_path",
    "group_id",
    "group_title",
    "is_total_group",
    "baseline_group_denom_count",
    "baseline_group_denom_units",
    "class_denom_count",
    "class_denom_path",
    "value",
    "value_path",
    "spread",
    "spread_path",
    "lower_limit",
    "upper_limit",
]

ETH_ROW_PAT = re.compile(r"\b(hispanic|latino|non-hispanic|not hispanic)\b", re.I)
RACE_ROW_PAT = re.compile(
    r"\b(white|black|african|asian|caucasian|indian|alaska|hawaiian|pacific|"
    r"oriental|amerindian|native american|europid|mulatto|mestizo)\b",
    re.I,
)
HBA1C_PAT = re.compile(
    r"(hba1c|\ba1c\b|hemoglobin\s*a1|haemoglobin\s*a1|"
    r"glycosylated\s*h[ae]moglobin|glycated\s*h[ae]moglobin)",
    re.I,
)
CHANGE_PAT = re.compile(r"(change|difference|reduction|decrease)", re.I)
PROP_PAT = re.compile(
    r"^(percentage|proportion|number)\s+of\s+(participants|patients|subjects|responders)",
    re.I,
)


def load_qualifying_nct_ids() -> list[str]:
    """Read the 201 qualifying NCT IDs from summary.md section 3.1."""
    summary_path = COHORT_DIR / "summary.md"
    lines = summary_path.read_text(encoding="utf-8").splitlines()
    in_sec_31 = False
    for line in lines:
        if line.strip().startswith("### 3.1 "):
            in_sec_31 = True
        elif in_sec_31 and line.strip().startswith("### "):
            break
        elif in_sec_31 and line.startswith("- **Qualifying NCT IDs (201)**:"):
            ncts = re.findall(r"NCT\d{8}", line)
            if len(ncts) != 201 or len(set(ncts)) != 201:
                raise ValueError(f"Expected 201 unique NCT IDs in summary.md 3.1, got {len(ncts)}")
            return ncts
    raise ValueError("Could not locate 'Qualifying NCT IDs (201)' line in summary.md section 3.1")


def classify_baseline_demographic_measure(measure: dict[str, Any]) -> str | None:
    """Identify race, ethnicity, sex, and age baseline measures deterministically.

    No mapping of row labels to standardized demographic categories is performed;
    this identifies only whether a measure in baselineCharacteristicsModule.measures[]
    belongs to sex, age, race, ethnicity, or combined race_and_ethnicity.
    """
    title = measure.get("title", "").strip()
    tl = title.lower()
    if title in (
        "Sex: Female, Male",
        "Gender",
        "Sex/Gender, Customized",
        "Gender (China Substudy)",
    ) or re.search(r"\b(sex|gender)\b", tl):
        return "sex"
    if title in (
        "Age, Continuous",
        "Age, Customized",
        "Age, Categorical",
        "Age Continuous",
        "Age Categorization, Female Only",
        "Age Characterization Female Only",
        "Age Group",
        "Age Continuous (China Substudy)",
        "Age at Screening",
    ) or re.match(r"^age\b", tl):
        return "age"
    if title in ("Race (NIH/OMB)", "Race"):
        return "race"
    if title in ("Ethnicity (NIH/OMB)", "Ethnicity"):
        return "ethnicity"
    if title in ("Race/Ethnicity, Customized", "Race/Ethnicity"):
        row_texts: list[str] = []
        for cls in measure.get("classes", []):
            ct = cls.get("title", "")
            for cat in cls.get("categories", []):
                catt = cat.get("title", "")
                row_texts.append(f"{ct} {catt}".strip())
        joined = " | ".join(row_texts)
        has_eth = bool(ETH_ROW_PAT.search(joined))
        has_race = bool(RACE_ROW_PAT.search(joined))
        if has_eth and has_race:
            return "race_and_ethnicity"
        if has_eth:
            return "ethnicity"
        return "race"
    return None


def rederive_qualifying_measure_from_json(om: dict[str, Any]) -> tuple[bool, list[str]]:
    """Deterministic re-derivation of whether a raw outcomeMeasure qualifies under section 3.1."""
    flags: list[str] = []
    if om.get("type") != "PRIMARY":
        return False, ["not_primary_type"]
    title = om.get("title", "")
    desc = om.get("description", "")
    if not HBA1C_PAT.search(f"{title} {desc}"):
        return False, ["not_hba1c_measure"]
    if PROP_PAT.search(title.strip()) or not CHANGE_PAT.search(f"{title} {desc}"):
        return False, ["construct_not_change_from_baseline_in_hba1c"]

    uom = om.get("unitOfMeasure", "").strip()
    uom_low = uom.lower()
    if not uom:
        return False, ["unit_not_stated"]
    if any(tok in uom_low for tok in ("participant", "patient", "subject", "mmol")):
        return False, [f"unit_not_percent_hba1c:{uom}"]
    if "%" not in uom_low and "percent" not in uom_low:
        return False, [f"unit_not_percent_hba1c:{uom}"]
    if "change" in uom_low:
        flags.append(f"ambiguous_unit_wording_mentions_change:{uom}")

    tf = om.get("timeFrame", "")
    if re.search(r"\bup\s+to\b", tf, re.I):
        return False, [f"time_frame_up_to:{tf}"]
    raw_matches = re.findall(
        r"(?:week|wk)s?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:week|wk)s?",
        tf,
        re.I,
    )
    post_wks = sorted({float(x) for pair in raw_matches for x in pair if x and float(x) > 0})
    if len(post_wks) != 1 or not (24.0 <= post_wks[0] <= 26.0):
        return False, [f"time_frame_not_single_fixed_24_to_26_weeks:{tf}"]

    analyses = om.get("analyses", [])
    strict_est_ci = []
    for ai, a in enumerate(analyses):
        pt = a.get("paramType", "")
        if "within treatment group" in pt.lower():
            continue
        has_all = (
            a.get("paramValue") not in (None, "")
            and a.get("ciLowerLimit") not in (None, "")
            and a.get("ciUpperLimit") not in (None, "")
        )
        if has_all:
            strict_est_ci.append(ai)
            if len(a.get("groupIds", [])) != 2:
                flags.append(
                    f"analysis_{ai}_non_two_group_ids:{json.dumps(a.get('groupIds', []))}"
                )
        else:
            flags.append(f"analysis_{ai}_missing_estimate_or_ci_limits_on_measure")

    if not strict_est_ci:
        return False, ["no_between_arm_analysis_with_estimate_and_both_ci_limits"]

    if any(f.startswith("ambiguous_unit_wording_mentions_change:") for f in flags):
        return False, flags
    return True, flags


def load_cohort_reference(nct_set: set[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, int], list[dict[str, Any]]]]:
    """Load qualifying HbA1c measures and posted between-arm analyses from cohort_sheet.csv and analyses.csv."""
    analyses_by_m: dict[tuple[str, int], list[dict[str, Any]]] = {}
    with open(COHORT_DIR / "analyses.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["nct_id"] in nct_set:
                key = (row["nct_id"], int(row["measure_index"]))
                analyses_by_m.setdefault(key, []).append(row)

    qual_measures_by_nct: dict[str, list[dict[str, Any]]] = {}
    with open(COHORT_DIR / "cohort_sheet.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nct = row["nct_id"]
            if nct not in nct_set or row["hba1c_primary_measure_posted"] != "yes":
                continue
            mi = int(row["measure_index"])
            ans = analyses_by_m.get((nct, mi), [])
            try:
                tp = float(row["time_point_weeks"])
                tp_ok = 24.0 <= tp <= 26.0
            except ValueError:
                tp_ok = False
            has_est_ci = any(
                a["estimate"] not in ("", "not_stated", "unresolved")
                and a["ci_lower"] not in ("", "not_stated", "unresolved")
                and a["ci_upper"] not in ("", "not_stated", "unresolved")
                for a in ans
            )
            if (
                row["construct"] == "change_from_baseline_in_hba1c"
                and row["unit_class"] == "percent_hba1c"
                and tp_ok
                and has_est_ci
            ):
                qual_measures_by_nct.setdefault(nct, []).append(row)
    return qual_measures_by_nct, analyses_by_m


def run_stage1_extraction() -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    """Compute Stage 1 deterministic outputs in memory and return (groups_rows, arm_demo_rows, trials_meta)."""
    nct_ids = load_qualifying_nct_ids()
    nct_set = set(nct_ids)
    cs_qual_by_nct, cs_analyses_by_m = load_cohort_reference(nct_set)

    groups_rows: list[dict[str, str]] = []
    arm_demo_rows: list[dict[str, str]] = []
    trials_data: dict[str, Any] = {}

    for nct_id in nct_ids:
        rec_path = RECORDS_DIR / f"{nct_id}.json.gz"
        if not rec_path.exists():
            trials_data[nct_id] = {
                "nct_id": nct_id,
                "record_status": "not_retrieved",
                "record_error": f"Missing file {rec_path.name}",
            }
            continue

        try:
            raw_bytes = gzip.decompress(rec_path.read_bytes())
            sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
            rec = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            trials_data[nct_id] = {
                "nct_id": nct_id,
                "record_status": "could_not_be_parsed",
                "record_error": f"{type(exc).__name__}: {exc}",
            }
            continue

        results = rec.get("resultsSection", {})
        bcm = results.get("baselineCharacteristicsModule", {})
        pfm = results.get("participantFlowModule", {})
        omm = results.get("outcomeMeasuresModule", {})
        all_oms = omm.get("outcomeMeasures", [])

        # Re-derive qualifying measures from raw JSON and compare against cohort_sheet.csv & analyses.csv
        cs_measure_indices = sorted(int(r["measure_index"]) for r in cs_qual_by_nct.get(nct_id, []))
        rederived_indices: list[int] = []
        rederivation_disagreements: list[dict[str, Any]] = []
        measure_flags_by_idx: dict[int, list[str]] = {}

        for mi, om in enumerate(all_oms):
            ok, flags = rederive_qualifying_measure_from_json(om)
            if ok:
                rederived_indices.append(mi)
            in_cs = mi in cs_measure_indices
            if in_cs and flags:
                measure_flags_by_idx[mi] = flags
            if ok != in_cs:
                rederivation_disagreements.append(
                    {
                        "type": "measure_qualification_disagreement",
                        "measure_index": mi,
                        "measure_title": om.get("title", ""),
                        "in_cohort_sheet_qualifying": in_cs,
                        "rederived_from_record": ok,
                        "reasons": flags,
                    }
                )

        # Check analyses.csv vs raw JSON analyses[] on each cohort_sheet qualifying measure
        for mi in cs_measure_indices:
            om = all_oms[mi]
            raw_analyses = om.get("analyses", [])
            cs_ans = cs_analyses_by_m.get((nct_id, mi), [])
            raw_ba_indices = [
                ai
                for ai, a in enumerate(raw_analyses)
                if "within treatment group" not in a.get("paramType", "").lower()
            ]
            cs_an_indices = [int(a["analysis_index"]) for a in cs_ans]
            if raw_ba_indices != cs_an_indices:
                rederivation_disagreements.append(
                    {
                        "type": "between_arm_analysis_index_disagreement",
                        "measure_index": mi,
                        "in_analyses_csv": cs_an_indices,
                        "rederived_from_record": raw_ba_indices,
                    }
                )
            for ai in raw_ba_indices:
                a_raw = raw_analyses[ai]
                has_est_ci = (
                    a_raw.get("paramValue") not in (None, "")
                    and a_raw.get("ciLowerLimit") not in (None, "")
                    and a_raw.get("ciUpperLimit") not in (None, "")
                )
                if not has_est_ci:
                    rederivation_disagreements.append(
                        {
                            "type": "analysis_on_qualifying_measure_lacks_estimate_or_ci_limits",
                            "measure_index": mi,
                            "analysis_index": ai,
                            "group_ids": a_raw.get("groupIds", []),
                            "param_value": a_raw.get("paramValue", "not_stated"),
                            "ci_lower_limit": a_raw.get("ciLowerLimit", "not_stated"),
                            "ci_upper_limit": a_raw.get("ciUpperLimit", "not_stated"),
                        }
                    )
                if len(a_raw.get("groupIds", [])) != 2:
                    rederivation_disagreements.append(
                        {
                            "type": "analysis_group_ids_length_not_two",
                            "measure_index": mi,
                            "analysis_index": ai,
                            "group_ids": a_raw.get("groupIds", []),
                            "group_ids_path": f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].analyses[{ai}].groupIds",
                        }
                    )

        active_qual_measure_indices = sorted(set(cs_measure_indices) | set(rederived_indices))

        # --- Stage 1 Item 1: Baseline groups ---
        bcm_pop = bcm["populationDescription"] if "populationDescription" in bcm and bcm["populationDescription"] != "" else "not_stated"
        bcm_pop_path = "resultsSection.baselineCharacteristicsModule.populationDescription" if "populationDescription" in bcm and bcm["populationDescription"] != "" else ""
        bcm_denoms = bcm.get("denoms", [])
        bcm_denom_units = bcm_denoms[0].get("units", "not_stated") if bcm_denoms else "not_stated"
        bcm_denom_units_path = "resultsSection.baselineCharacteristicsModule.denoms[0].units" if bcm_denoms and "units" in bcm_denoms[0] else ""
        bcm_count_map: dict[str, tuple[str, str]] = {}
        if bcm_denoms:
            for ci, c_entry in enumerate(bcm_denoms[0].get("counts", [])):
                bcm_count_map[c_entry["groupId"]] = (
                    c_entry.get("value", "not_stated"),
                    f"resultsSection.baselineCharacteristicsModule.denoms[0].counts[{ci}].value",
                )

        stage1_bg_list: list[dict[str, Any]] = []
        for gi, g in enumerate(bcm.get("groups", [])):
            gid = g["id"]
            gtitle = g.get("title", "not_stated")
            gtitle_path = f"resultsSection.baselineCharacteristicsModule.groups[{gi}].title" if "title" in g else ""
            has_desc = "description" in g and g["description"] != ""
            gdesc = g["description"] if has_desc else "not_stated"
            gdesc_path = f"resultsSection.baselineCharacteristicsModule.groups[{gi}].description" if has_desc else ""
            is_total = "yes" if gtitle == "Total" else "no"
            d_cnt, d_cnt_path = bcm_count_map.get(gid, ("not_stated", ""))

            row = {col: "" for col in GROUPS_CSV_COLUMNS}
            row.update(
                {
                    "nct_id": nct_id,
                    "module": "baseline",
                    "population_description": bcm_pop,
                    "population_description_path": bcm_pop_path,
                    "group_index": str(gi),
                    "group_id": gid,
                    "is_total_group": is_total,
                    "group_title": gtitle,
                    "group_title_path": gtitle_path,
                    "group_description": gdesc,
                    "group_description_path": gdesc_path,
                    "denom_units": bcm_denom_units,
                    "denom_units_path": bcm_denom_units_path,
                    "denom_count": d_cnt,
                    "denom_count_path": d_cnt_path,
                    "class_denoms_json": "[]",
                    "flow_periods_json": "[]",
                    "measure_analyses_group_ids_json": "[]",
                }
            )
            groups_rows.append(row)
            stage1_bg_list.append(
                {
                    "group_index": gi,
                    "group_id": gid,
                    "is_total_group": is_total == "yes",
                    "title": gtitle,
                    "title_path": gtitle_path,
                    "description": gdesc,
                    "description_path": gdesc_path,
                    "denom_units": bcm_denom_units,
                    "denom_units_path": bcm_denom_units_path,
                    "denom_count": d_cnt,
                    "denom_count_path": d_cnt_path,
                }
            )

        # --- Stage 1 Item 2: Participant Flow groups & all periods ---
        periods = pfm.get("periods", [])
        stage1_fg_list: list[dict[str, Any]] = []
        for fi, fg in enumerate(pfm.get("groups", [])):
            fgid = fg["id"]
            ftitle = fg.get("title", "not_stated")
            ftitle_path = f"resultsSection.participantFlowModule.groups[{fi}].title" if "title" in fg else ""
            has_fdesc = "description" in fg and fg["description"] != ""
            fdesc = fg["description"] if has_fdesc else "not_stated"
            fdesc_path = f"resultsSection.participantFlowModule.groups[{fi}].description" if has_fdesc else ""

            fg_periods_detail: list[dict[str, Any]] = []
            for pi, per in enumerate(periods):
                ptitle = per.get("title", "not_stated")
                ptitle_path = f"resultsSection.participantFlowModule.periods[{pi}].title" if "title" in per else ""
                started_val, started_path, started_comment = "not_stated", "", ""
                completed_val, completed_path, completed_comment = "not_stated", "", ""
                for msi, ms in enumerate(per.get("milestones", [])):
                    mstype = ms.get("type", "")
                    if mstype in ("STARTED", "COMPLETED"):
                        for aci, ach in enumerate(ms.get("achievements", [])):
                            if ach.get("groupId") == fgid:
                                val = ach.get("numSubjects", "not_stated")
                                vpath = (
                                    f"resultsSection.participantFlowModule.periods[{pi}]"
                                    f".milestones[{msi}].achievements[{aci}].numSubjects"
                                    if "numSubjects" in ach
                                    else ""
                                )
                                comm = ach.get("comment", "")
                                if mstype == "STARTED":
                                    started_val, started_path, started_comment = val, vpath, comm
                                else:
                                    completed_val, completed_path, completed_comment = val, vpath, comm
                fg_periods_detail.append(
                    {
                        "period_index": pi,
                        "period_title": ptitle,
                        "period_title_path": ptitle_path,
                        "started_num_subjects": started_val,
                        "started_path": started_path,
                        "started_comment": started_comment,
                        "completed_num_subjects": completed_val,
                        "completed_path": completed_path,
                        "completed_comment": completed_comment,
                    }
                )

            row = {col: "" for col in GROUPS_CSV_COLUMNS}
            row.update(
                {
                    "nct_id": nct_id,
                    "module": "participant_flow",
                    "group_index": str(fi),
                    "group_id": fgid,
                    "is_total_group": "no",
                    "group_title": ftitle,
                    "group_title_path": ftitle_path,
                    "group_description": fdesc,
                    "group_description_path": fdesc_path,
                    "class_denoms_json": "[]",
                    "flow_periods_json": json.dumps(fg_periods_detail, ensure_ascii=False),
                    "measure_analyses_group_ids_json": "[]",
                }
            )
            for p_slot in range(min(3, len(fg_periods_detail))):
                pd = fg_periods_detail[p_slot]
                row[f"period_{p_slot}_title"] = pd["period_title"]
                row[f"period_{p_slot}_started"] = pd["started_num_subjects"]
                row[f"period_{p_slot}_started_path"] = pd["started_path"]
                row[f"period_{p_slot}_completed"] = pd["completed_num_subjects"]
                row[f"period_{p_slot}_completed_path"] = pd["completed_path"]
            groups_rows.append(row)

            stage1_fg_list.append(
                {
                    "group_index": fi,
                    "group_id": fgid,
                    "title": ftitle,
                    "title_path": ftitle_path,
                    "description": fdesc,
                    "description_path": fdesc_path,
                    "periods": fg_periods_detail,
                }
            )

        # --- Stage 1 Item 3: Qualifying HbA1c primary measures, OGs, denoms, and analyses ---
        stage1_measures: list[dict[str, Any]] = []
        for mi in active_qual_measure_indices:
            om = all_oms[mi]
            mtitle = om.get("title", "not_stated")
            mtitle_path = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].title" if "title" in om else ""
            has_mpop = "populationDescription" in om and om["populationDescription"] != ""
            mpop = om["populationDescription"] if has_mpop else "not_stated"
            mpop_path = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].populationDescription" if has_mpop else ""

            om_denoms = om.get("denoms", [])
            om_units = om_denoms[0].get("units", "not_stated") if om_denoms else "not_stated"
            om_units_path = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].denoms[0].units" if om_denoms and "units" in om_denoms[0] else ""
            om_count_map: dict[str, tuple[str, str]] = {}
            if om_denoms:
                for ci, c_entry in enumerate(om_denoms[0].get("counts", [])):
                    om_count_map[c_entry["groupId"]] = (
                        c_entry.get("value", "not_stated"),
                        f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].denoms[0].counts[{ci}].value",
                    )

            class_denoms_by_og: dict[str, list[dict[str, Any]]] = {}
            for cli, cls in enumerate(om.get("classes", [])):
                if "denoms" in cls and cls["denoms"]:
                    cd_units = cls["denoms"][0].get("units", "not_stated")
                    for cci, c_entry in enumerate(cls["denoms"][0].get("counts", [])):
                        class_denoms_by_og.setdefault(c_entry["groupId"], []).append(
                            {
                                "class_index": cli,
                                "class_title": cls.get("title", ""),
                                "units": cd_units,
                                "count": c_entry.get("value", "not_stated"),
                                "count_path": (
                                    f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}]"
                                    f".classes[{cli}].denoms[0].counts[{cci}].value"
                                ),
                            }
                        )

            om_analyses_list: list[dict[str, Any]] = []
            for ai, a_obj in enumerate(om.get("analyses", [])):
                pt = a_obj.get("paramType", "not_stated")
                is_within_arm = "within treatment group" in pt.lower()
                gids = list(a_obj.get("groupIds", []))
                gids_path = (
                    f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].analyses[{ai}].groupIds"
                    if "groupIds" in a_obj
                    else ""
                )
                has_est_ci = (
                    not is_within_arm
                    and a_obj.get("paramValue") not in (None, "")
                    and a_obj.get("ciLowerLimit") not in (None, "")
                    and a_obj.get("ciUpperLimit") not in (None, "")
                )
                om_analyses_list.append(
                    {
                        "analysis_index": ai,
                        "is_between_arm": not is_within_arm,
                        "has_estimate_and_both_ci_limits": has_est_ci,
                        "group_ids": gids,
                        "group_ids_path": gids_path,
                        "param_type": pt,
                        "param_value": a_obj.get("paramValue", "not_stated"),
                        "ci_pct_value": a_obj.get("ciPctValue", "not_stated"),
                        "ci_lower_limit": a_obj.get("ciLowerLimit", "not_stated"),
                        "ci_upper_limit": a_obj.get("ciUpperLimit", "not_stated"),
                        "p_value": a_obj.get("pValue", "not_stated"),
                    }
                )

            analyses_json_str = json.dumps(om_analyses_list, ensure_ascii=False)
            og_list: list[dict[str, Any]] = []
            for ogi, og in enumerate(om.get("groups", [])):
                ogid = og["id"]
                otitle = og.get("title", "not_stated")
                otitle_path = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].groups[{ogi}].title" if "title" in og else ""
                has_odesc = "description" in og and og["description"] != ""
                odesc = og["description"] if has_odesc else "not_stated"
                odesc_path = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}].groups[{ogi}].description" if has_odesc else ""
                ocnt, ocnt_path = om_count_map.get(ogid, ("not_stated", ""))
                cd_list = class_denoms_by_og.get(ogid, [])

                row = {col: "" for col in GROUPS_CSV_COLUMNS}
                row.update(
                    {
                        "nct_id": nct_id,
                        "module": "outcome_measure",
                        "measure_index": str(mi),
                        "measure_title": mtitle,
                        "measure_title_path": mtitle_path,
                        "population_description": mpop,
                        "population_description_path": mpop_path,
                        "group_index": str(ogi),
                        "group_id": ogid,
                        "is_total_group": "no",
                        "group_title": otitle,
                        "group_title_path": otitle_path,
                        "group_description": odesc,
                        "group_description_path": odesc_path,
                        "denom_units": om_units,
                        "denom_units_path": om_units_path,
                        "denom_count": ocnt,
                        "denom_count_path": ocnt_path,
                        "class_denoms_json": json.dumps(cd_list, ensure_ascii=False),
                        "flow_periods_json": "[]",
                        "measure_analyses_group_ids_json": analyses_json_str,
                    }
                )
                groups_rows.append(row)
                og_list.append(
                    {
                        "group_index": ogi,
                        "group_id": ogid,
                        "title": otitle,
                        "title_path": otitle_path,
                        "description": odesc,
                        "description_path": odesc_path,
                        "denom_units": om_units,
                        "denom_units_path": om_units_path,
                        "denom_count": ocnt,
                        "denom_count_path": ocnt_path,
                        "class_denoms": cd_list,
                    }
                )

            stage1_measures.append(
                {
                    "measure_index": mi,
                    "in_cohort_sheet_qualifying": mi in cs_measure_indices,
                    "rederived_strictly_qualifying": mi in rederived_indices,
                    "rederivation_flags": measure_flags_by_idx.get(mi, []),
                    "title": mtitle,
                    "title_path": mtitle_path,
                    "unit_of_measure": om.get("unitOfMeasure", "not_stated"),
                    "time_frame": om.get("timeFrame", "not_stated"),
                    "population_description": mpop,
                    "population_description_path": mpop_path,
                    "groups": og_list,
                    "analyses": om_analyses_list,
                }
            )

        # --- Stage 1 Item 4: Per baseline group race, ethnicity, sex, and age rows ---
        trial_demo_rows_meta: list[dict[str, str]] = []
        for bmi, bm in enumerate(bcm.get("measures", [])):
            domain = classify_baseline_demographic_measure(bm)
            if domain is None:
                continue
            bm_title = bm.get("title", "not_stated")
            bm_title_path = f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].title" if "title" in bm else ""
            has_bmpop = "populationDescription" in bm and bm["populationDescription"] != ""
            bm_pop = bm["populationDescription"] if has_bmpop else "not_stated"
            bm_pop_path = f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].populationDescription" if has_bmpop else ""
            param_type = bm.get("paramType", "not_stated")
            disp_type = bm.get("dispersionType", "not_stated")
            uom = bm.get("unitOfMeasure", "not_stated")

            for cli, cls in enumerate(bm.get("classes", [])):
                ctitle = cls.get("title", "")
                ctitle_path = (
                    f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].classes[{cli}].title"
                    if "title" in cls
                    else ""
                )
                cls_denom_map: dict[str, tuple[str, str]] = {}
                if "denoms" in cls and cls["denoms"]:
                    for cdi, cd_entry in enumerate(cls["denoms"][0].get("counts", [])):
                        cls_denom_map[cd_entry["groupId"]] = (
                            cd_entry.get("value", ""),
                            (
                                f"resultsSection.baselineCharacteristicsModule.measures[{bmi}]"
                                f".classes[{cli}].denoms[0].counts[{cdi}].value"
                            ),
                        )

                for cati, cat in enumerate(cls.get("categories", [])):
                    cattitle = cat.get("title", "")
                    cattitle_path = (
                        f"resultsSection.baselineCharacteristicsModule.measures[{bmi}]"
                        f".classes[{cli}].categories[{cati}].title"
                        if "title" in cat
                        else ""
                    )
                    meas_by_gid: dict[str, tuple[int, dict[str, Any]]] = {}
                    for mmi, meas in enumerate(cat.get("measurements", [])):
                        meas_by_gid[meas["groupId"]] = (mmi, meas)

                    for bg_item in stage1_bg_list:
                        gid = bg_item["group_id"]
                        cd_val, cd_path = cls_denom_map.get(gid, ("", ""))
                        if gid in meas_by_gid:
                            mmi, meas_obj = meas_by_gid[gid]
                            base_mpath = (
                                f"resultsSection.baselineCharacteristicsModule.measures[{bmi}]"
                                f".classes[{cli}].categories[{cati}].measurements[{mmi}]"
                            )
                            val = meas_obj["value"] if "value" in meas_obj else "not_stated"
                            val_path = f"{base_mpath}.value" if "value" in meas_obj else ""
                            spread = meas_obj.get("spread", "")
                            spread_path = f"{base_mpath}.spread" if "spread" in meas_obj else ""
                            low = meas_obj.get("lowerLimit", "")
                            upp = meas_obj.get("upperLimit", "")
                        else:
                            val, val_path = "not_stated", ""
                            spread, spread_path = "", ""
                            low, upp = "", ""

                        drow = {
                            "nct_id": nct_id,
                            "baseline_measure_index": str(bmi),
                            "measure_domain": domain,
                            "measure_title": bm_title,
                            "measure_title_path": bm_title_path,
                            "measure_population_description": bm_pop,
                            "measure_population_description_path": bm_pop_path,
                            "param_type": param_type,
                            "dispersion_type": disp_type,
                            "unit_of_measure": uom,
                            "class_index": str(cli),
                            "class_title": ctitle,
                            "class_title_path": ctitle_path,
                            "category_index": str(cati),
                            "category_title": cattitle,
                            "category_title_path": cattitle_path,
                            "group_id": gid,
                            "group_title": bg_item["title"],
                            "is_total_group": "yes" if bg_item["is_total_group"] else "no",
                            "baseline_group_denom_count": bg_item["denom_count"],
                            "baseline_group_denom_units": bg_item["denom_units"],
                            "class_denom_count": cd_val,
                            "class_denom_path": cd_path,
                            "value": val,
                            "value_path": val_path,
                            "spread": spread,
                            "spread_path": spread_path,
                            "lower_limit": low,
                            "upper_limit": upp,
                        }
                        arm_demo_rows.append(drow)
                        trial_demo_rows_meta.append(drow)

        trials_data[nct_id] = {
            "nct_id": nct_id,
            "record_status": "ok",
            "record_sha256": sha256_hex,
            "cohort_sheet_qualifying_measure_indices": cs_measure_indices,
            "rederived_qualifying_measure_indices": rederived_indices,
            "active_qualifying_measure_indices": active_qual_measure_indices,
            "rederivation_disagreements": rederivation_disagreements,
            "baseline_population_description": bcm_pop,
            "baseline_population_description_path": bcm_pop_path,
            "baseline_groups": stage1_bg_list,
            "flow_groups": stage1_fg_list,
            "qualifying_measures": stage1_measures,
            "demographic_measures_present": sorted({r["measure_domain"] for r in trial_demo_rows_meta}),
        }

    return groups_rows, arm_demo_rows, {"nct_ids": nct_ids, "trials": trials_data}


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ALIGN_DIR.mkdir(parents=True, exist_ok=True)
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    groups_rows, arm_demo_rows, stage1_meta = run_stage1_extraction()

    write_csv(ALIGN_DIR / "groups.csv", GROUPS_CSV_COLUMNS, groups_rows)
    write_csv(ALIGN_DIR / "arm_demographics.csv", ARM_DEMOGRAPHICS_CSV_COLUMNS, arm_demo_rows)
    with open(INTERMEDIATE_DIR / "stage1_trials.json", "w", encoding="utf-8") as f:
        json.dump(stage1_meta, f, ensure_ascii=False, indent=2)

    trials = stage1_meta["trials"]
    ok_cnt = sum(1 for t in trials.values() if t["record_status"] == "ok")
    dis_trials = [
        nct for nct, t in trials.items() if t.get("rederivation_disagreements")
    ]
    print(f"Stage 1 complete: {ok_cnt}/{len(stage1_meta['nct_ids'])} records parsed.")
    print(f"  groups.csv rows: {len(groups_rows)}")
    print(f"  arm_demographics.csv rows: {len(arm_demo_rows)}")
    print(f"  Trials with re-derivation flags vs cohort_sheet/analyses.csv: {len(dis_trials)} -> {dis_trials}")


if __name__ == "__main__":
    main()
