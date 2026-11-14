#!/usr/bin/env python3
"""Schemas, lossless stored-record formatter, and verbatim prompts for readers and adjudicators."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

QUOTE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Exact verbatim substring from the stored record at `path` (character-for-character, case-sensitive).",
        },
        "path": {
            "type": "string",
            "description": "Exact JSON path in the stored record, e.g. resultsSection.outcomeMeasuresModule.outcomeMeasures[0].timeFrame",
        },
    },
    "required": ["text", "path"],
}

ARM_GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group_id": {"type": "string", "description": "Exact group id as posted, e.g. OG000"},
        "group_title": {"type": "string", "description": "Exact group title string as posted"},
        "group_title_quote_text": {"type": "string", "description": "Verbatim group title string"},
        "group_title_quote_path": {
            "type": "string",
            "description": "JSON path to group title, e.g. resultsSection.outcomeMeasuresModule.outcomeMeasures[0].groups[0].title",
        },
        "number_analysed": {
            "type": "string",
            "description": "Exact count string in measure-level denoms for this group_id, or 'not_stated'",
        },
        "number_analysed_unit": {
            "type": "string",
            "description": "Exact units string in measure-level denoms (e.g. 'Participants'), or 'not_stated'",
        },
    },
    "required": [
        "group_id",
        "group_title",
        "group_title_quote_text",
        "group_title_quote_path",
        "number_analysed",
        "number_analysed_unit",
    ],
}

ARM_MEASUREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group_id": {"type": "string", "description": "Group id, e.g. OG000"},
        "class_index": {"type": "integer", "description": "0-based index in outcomeMeasures[i].classes"},
        "category_index": {"type": "integer", "description": "0-based index in classes[c].categories"},
        "class_title": {"type": "string", "description": "Exact classes[c].title if present, else ''"},
        "category_title": {"type": "string", "description": "Exact categories[k].title if present, else ''"},
        "row_number_analysed": {
            "type": "string",
            "description": "If classes[c].denoms exists, exact count for this group_id; else ''",
        },
        "value": {"type": "string", "description": "Exact measurements[].value for this group_id, or 'not_stated'"},
        "spread": {"type": "string", "description": "Exact measurements[].spread for this group_id, or 'not_stated'"},
        "lower_limit": {
            "type": "string",
            "description": "Exact measurements[].lowerLimit for this group_id, or 'not_stated'",
        },
        "upper_limit": {
            "type": "string",
            "description": "Exact measurements[].upperLimit for this group_id, or 'not_stated'",
        },
    },
    "required": [
        "group_id",
        "class_index",
        "category_index",
        "class_title",
        "category_title",
        "row_number_analysed",
        "value",
        "spread",
        "lower_limit",
        "upper_limit",
    ],
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "analysis_index": {
            "type": "integer",
            "description": "0-based index in resultsSection.outcomeMeasuresModule.outcomeMeasures[i].analyses",
        },
        "groups_compared": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact groupIds array as posted in analyses[analysis_index].groupIds (do not add or infer missing groupIds)",
        },
        "estimate_type_as_posted": {
            "type": "string",
            "description": "Exact analyses[analysis_index].paramType string if present, else 'not_stated'",
        },
        "estimate_type_quote_text": {
            "type": "string",
            "description": "Verbatim paramType string if present, else ''",
        },
        "estimate_type_quote_path": {
            "type": "string",
            "description": "JSON path to analyses[analysis_index].paramType if present, else ''",
        },
        "estimate": {
            "type": "string",
            "description": "Exact analyses[analysis_index].paramValue string if present, else 'not_stated'",
        },
        "ci_percent": {
            "type": "string",
            "description": "Exact analyses[analysis_index].ciPctValue string if present, else 'not_stated'",
        },
        "ci_lower": {
            "type": "string",
            "description": "Exact analyses[analysis_index].ciLowerLimit string if present, else 'not_stated'",
        },
        "ci_upper": {
            "type": "string",
            "description": "Exact analyses[analysis_index].ciUpperLimit string if present, else 'not_stated'",
        },
        "ci_sides": {
            "type": "string",
            "description": "Exact analyses[analysis_index].ciNumSides string if present, else 'not_stated'",
        },
        "p_value_as_posted": {
            "type": "string",
            "description": "Exact analyses[analysis_index].pValue string including any '<' or '=', or 'not_stated'",
        },
        "non_inferiority_type_raw": {
            "type": "string",
            "description": "Exact analyses[analysis_index].nonInferiorityType string if present, else 'not_stated'",
        },
        "hypothesis_class": {
            "type": "string",
            "enum": ["superiority", "non_inferiority", "equivalence", "other", "not_stated"],
        },
        "non_inferiority_margin_as_posted": {
            "type": "string",
            "description": "Exact verbatim phrase/value stating the non-inferiority margin if given in this analysis or measure, else 'not_stated'",
        },
        "non_inferiority_margin_quote_text": {
            "type": "string",
            "description": "Verbatim substring stating the non-inferiority margin if stated, else ''",
        },
        "non_inferiority_margin_quote_path": {
            "type": "string",
            "description": "JSON path where non_inferiority_margin_quote_text stands, else ''",
        },
        "statistical_method_as_posted": {
            "type": "string",
            "description": "Exact analyses[analysis_index].statisticalMethod string if present, else 'not_stated'",
        },
        "statistical_method_quote_text": {
            "type": "string",
            "description": "Verbatim statisticalMethod string if present, else ''",
        },
        "statistical_method_quote_path": {
            "type": "string",
            "description": "JSON path to analyses[analysis_index].statisticalMethod if present, else ''",
        },
    },
    "required": [
        "analysis_index",
        "groups_compared",
        "estimate_type_as_posted",
        "estimate_type_quote_text",
        "estimate_type_quote_path",
        "estimate",
        "ci_percent",
        "ci_lower",
        "ci_upper",
        "ci_sides",
        "p_value_as_posted",
        "non_inferiority_type_raw",
        "hypothesis_class",
        "non_inferiority_margin_as_posted",
        "non_inferiority_margin_quote_text",
        "non_inferiority_margin_quote_path",
        "statistical_method_as_posted",
        "statistical_method_quote_text",
        "statistical_method_quote_path",
    ],
}

MEASURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "measure_index": {
            "type": "integer",
            "description": "0-based position in resultsSection.outcomeMeasuresModule.outcomeMeasures",
        },
        "measure_title": {"type": "string", "description": "Exact title of this outcome measure"},
        "measure_title_quote_text": {"type": "string", "description": "Verbatim title string"},
        "measure_title_quote_path": {
            "type": "string",
            "description": "JSON path: resultsSection.outcomeMeasuresModule.outcomeMeasures[measure_index].title",
        },
        "construct": {
            "type": "string",
            "enum": [
                "change_from_baseline_in_hba1c",
                "hba1c_value_at_time_point",
                "proportion_reaching_hba1c_target",
                "other",
                "not_stated",
            ],
        },
        "construct_quotes": {"type": "array", "items": QUOTE_ITEM_SCHEMA},
        "unit_as_posted": {
            "type": "string",
            "description": "Exact unitOfMeasure string on this outcome measure if present, else 'not_stated'",
        },
        "unit_as_posted_quote_text": {"type": "string", "description": "Verbatim unitOfMeasure if present, else ''"},
        "unit_as_posted_quote_path": {
            "type": "string",
            "description": "JSON path to unitOfMeasure if present, else ''",
        },
        "unit_class": {
            "type": "string",
            "enum": ["percent_hba1c", "mmol_per_mol", "other", "not_stated"],
        },
        "time_frame_as_posted": {
            "type": "string",
            "description": "Exact timeFrame string on this outcome measure if present, else 'not_stated'",
        },
        "time_frame_as_posted_quote_text": {"type": "string", "description": "Verbatim timeFrame if present, else ''"},
        "time_frame_as_posted_quote_path": {
            "type": "string",
            "description": "JSON path to timeFrame if present, else ''",
        },
        "time_point_weeks": {
            "type": "string",
            "description": "Single fixed post-baseline week number as string (e.g. '24', '26'), else 'not_one_fixed_time_point'",
        },
        "population_description_as_posted": {
            "type": "string",
            "description": "Exact populationDescription string on this outcome measure if present, else 'not_stated'",
        },
        "population_description_quote_text": {
            "type": "string",
            "description": "Verbatim populationDescription if present, else ''",
        },
        "population_description_quote_path": {
            "type": "string",
            "description": "JSON path to populationDescription if present, else ''",
        },
        "population_class": {
            "type": "string",
            "enum": [
                "all_randomised",
                "all_randomised_and_treated",
                "modified_intention_to_treat_with_stated_condition",
                "full_analysis_set",
                "per_protocol",
                "completers",
                "other",
                "not_stated",
            ],
        },
        "population_defined": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
        },
        "missing_data_method_as_posted": {
            "type": "string",
            "description": "Exact verbatim phrase naming missing-data method if stated in this measure/analyses, else 'not_stated'",
        },
        "missing_data_method_quotes": {"type": "array", "items": QUOTE_ITEM_SCHEMA},
        "data_after_rescue_therapy": {
            "type": "string",
            "enum": ["included", "excluded", "not_stated"],
        },
        "data_after_rescue_therapy_quotes": {"type": "array", "items": QUOTE_ITEM_SCHEMA},
        "both_rescue_estimands_present": {
            "type": "boolean",
            "description": "True only if this single measure co-reports both an estimand including rescue data and an estimand excluding rescue data",
        },
        "parameter_type": {
            "type": "string",
            "description": "Exact paramType string on this outcome measure if present, else 'not_stated'",
        },
        "dispersion_type": {
            "type": "string",
            "description": "Exact dispersionType string on this outcome measure if present, else 'not_stated'",
        },
        "arm_groups": {"type": "array", "items": ARM_GROUP_SCHEMA},
        "arm_measurements": {"type": "array", "items": ARM_MEASUREMENT_SCHEMA},
        "comparator_class": {
            "type": "string",
            "enum": [
                "placebo",
                "active_drug",
                "dose_comparison_of_same_drug",
                "mixed",
                "not_determinable",
            ],
        },
        "comparator_class_quotes": {"type": "array", "items": QUOTE_ITEM_SCHEMA},
        "background_therapy_as_posted": {
            "type": "string",
            "description": "Exact verbatim phrase naming background therapy from arm descriptions or eligibility text if stated, else 'not_stated'",
        },
        "background_therapy_quotes": {"type": "array", "items": QUOTE_ITEM_SCHEMA},
        "analyses": {"type": "array", "items": ANALYSIS_SCHEMA},
    },
    "required": [
        "measure_index",
        "measure_title",
        "measure_title_quote_text",
        "measure_title_quote_path",
        "construct",
        "construct_quotes",
        "unit_as_posted",
        "unit_as_posted_quote_text",
        "unit_as_posted_quote_path",
        "unit_class",
        "time_frame_as_posted",
        "time_frame_as_posted_quote_text",
        "time_frame_as_posted_quote_path",
        "time_point_weeks",
        "population_description_as_posted",
        "population_description_quote_text",
        "population_description_quote_path",
        "population_class",
        "population_defined",
        "missing_data_method_as_posted",
        "missing_data_method_quotes",
        "data_after_rescue_therapy",
        "data_after_rescue_therapy_quotes",
        "both_rescue_estimands_present",
        "parameter_type",
        "dispersion_type",
        "arm_groups",
        "arm_measurements",
        "comparator_class",
        "comparator_class_quotes",
        "background_therapy_as_posted",
        "background_therapy_quotes",
        "analyses",
    ],
}

TRIAL_READING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nct_id": {"type": "string"},
        "hba1c_primary_measure_posted": {"type": "string", "enum": ["yes", "no"]},
        "measures": {"type": "array", "items": MEASURE_SCHEMA},
        "baseline_population_total": {
            "type": "string",
            "description": "Exact count in baselineCharacteristicsModule.denoms[0] for the Total/Overall group, or 'not_stated' (or 'not_applicable' if hba1c_primary_measure_posted='no')",
        },
        "baseline_population_total_unit": {
            "type": "string",
            "description": "Exact units in baselineCharacteristicsModule.denoms[0] (e.g. 'Participants'), or 'not_stated'",
        },
        "participants_started_by_group": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "group_title": {"type": "string"},
                    "num_subjects": {"type": "string"},
                },
                "required": ["group_id", "group_title", "num_subjects"],
            },
            "description": "Per-group STARTED counts from participantFlowModule.periods[0].milestones (type=='STARTED')",
        },
        "baseline_hba1c_posted": {"type": "string", "enum": ["yes", "no", "not_applicable"]},
        "baseline_hba1c_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "baseline_measure_index": {"type": "integer"},
                    "measure_title": {"type": "string"},
                    "unit_as_posted": {"type": "string"},
                    "param_type": {"type": "string"},
                    "group_id": {"type": "string"},
                    "group_title": {"type": "string"},
                    "class_title": {"type": "string"},
                    "category_title": {"type": "string"},
                    "value": {"type": "string"},
                    "spread": {"type": "string"},
                    "lower_limit": {"type": "string"},
                    "upper_limit": {"type": "string"},
                },
                "required": [
                    "baseline_measure_index",
                    "measure_title",
                    "unit_as_posted",
                    "param_type",
                    "group_id",
                    "group_title",
                    "class_title",
                    "category_title",
                    "value",
                    "spread",
                    "lower_limit",
                    "upper_limit",
                ],
            },
        },
        "baseline_demographic_measures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "enum": ["race", "ethnicity", "sex", "age"]},
                    "baseline_measure_index": {"type": "integer"},
                    "measure_title": {"type": "string"},
                    "measure_title_quote_text": {"type": "string"},
                    "measure_title_quote_path": {"type": "string"},
                    "param_type": {"type": "string"},
                    "unit_as_posted": {"type": "string"},
                },
                "required": [
                    "domain",
                    "baseline_measure_index",
                    "measure_title",
                    "measure_title_quote_text",
                    "measure_title_quote_path",
                    "param_type",
                    "unit_as_posted",
                ],
            },
        },
        "baseline_demographic_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "enum": ["race", "ethnicity", "sex", "age"]},
                    "baseline_measure_index": {"type": "integer"},
                    "row_index": {"type": "integer", "description": "0-based sequential row index within this baseline measure"},
                    "class_index": {"type": "integer"},
                    "category_index": {"type": "integer"},
                    "class_title": {"type": "string"},
                    "category_title": {"type": "string"},
                    "total_group_id": {"type": "string"},
                    "total_value": {"type": "string"},
                    "total_spread": {"type": "string"},
                },
                "required": [
                    "domain",
                    "baseline_measure_index",
                    "row_index",
                    "class_index",
                    "category_index",
                    "class_title",
                    "category_title",
                    "total_group_id",
                    "total_value",
                    "total_spread",
                ],
            },
        },
        "race_and_ethnicity_in_one_table": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
        },
        "race_and_ethnicity_in_one_table_row_labels": {
            "type": "array",
            "items": {"type": "string"},
        },
        "countries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "country": {"type": "string"},
                    "site_count": {"type": "integer"},
                },
                "required": ["country", "site_count"],
            },
        },
    },
    "required": [
        "nct_id",
        "hba1c_primary_measure_posted",
        "measures",
        "baseline_population_total",
        "baseline_population_total_unit",
        "participants_started_by_group",
        "baseline_hba1c_posted",
        "baseline_hba1c_rows",
        "baseline_demographic_measures",
        "baseline_demographic_rows",
        "race_and_ethnicity_in_one_table",
        "race_and_ethnicity_in_one_table_row_labels",
        "countries",
    ],
}

ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nct_id": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_key": {
                        "type": "string",
                        "description": "Exact field_key from the disagreement list",
                    },
                    "choice": {
                        "type": "string",
                        "enum": ["reading_1", "reading_2", "custom_verbatim", "unresolved"],
                        "description": "Choose reading_1 or reading_2 if it accurately reflects the stored record; custom_verbatim only if neither captured the exact literal value/structure in the record; unresolved if the record text genuinely does not decide it.",
                    },
                    "custom_value_json": {
                        "type": "string",
                        "description": "JSON-encoded adjudicated value when choice=='custom_verbatim'; otherwise ''",
                    },
                    "deciding_quotes": {
                        "type": "array",
                        "items": QUOTE_ITEM_SCHEMA,
                        "description": "Verbatim quote(s) and exact JSON path(s) in the stored record that decide this field (empty [] only when the deciding fact is complete absence of the field from the record, or when unresolved without deciding text).",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Brief literal explanation referencing only the stored record.",
                    },
                },
                "required": ["field_key", "choice", "custom_value_json", "deciding_quotes", "rationale"],
            },
        },
        "full_measure_if_needed": {
            "type": "array",
            "items": MEASURE_SCHEMA,
            "description": "Populated ONLY when one reader included a measure_index and the other omitted it, and adjudication concludes that measure_index IS a posted HbA1c primary measure requiring a full adjudicated measure object; otherwise [].",
        },
    },
    "required": ["nct_id", "decisions", "full_measure_if_needed"],
}


READER_SYSTEM_PROMPT = """You are an independent clinical trial registry record reader.
You read ONE stored ClinicalTrials.gov v2 JSON record provided directly in your prompt and return the completed coding sheet by calling the `finish` tool.

ABSOLUTE RULES:
1. Do NOT call any file-reading, search, or shell tools. Everything from the stored record is provided in your prompt. Call ONLY `finish`.
2. Record ONLY what the registry record states, with the exact words it uses and the exact JSON path where they stand.
3. Do NOT infer, harmonise, convert, impute, or correct anything. Never use publications, drug labels, FDA documents, sponsor websites, other registries, or your own outside knowledge of any trial or drug. If you happen to know a trial's published result or drug mechanism, it must NEVER influence or appear in your output.
4. Where the record does not state something, output `'not_stated'` (never a guess and never a default).
5. String values in the stored record are displayed inside `<<<...>>>` delimiters (for example `resultsSection.outcomeMeasuresModule.outcomeMeasures[0].timeFrame: <<<Baseline, week 26>>>`). The characters inside `<<<` and `>>>` are the exact raw characters at that JSON path. Do NOT include `<<<` or `>>>` in your output. Every quoted `text` MUST be an exact character-for-character, case-sensitive substring of the scalar value at its stated `path`.
"""


CODING_INSTRUCTIONS_BODY = """## Coding Sheet Rules (Apply Literally)

### 0. Scope: Posted Primary Outcome Measures About HbA1c
- Inspect `resultsSection.outcomeMeasuresModule.outcomeMeasures[i]` ONLY where `type == "PRIMARY"`. (Ignore `protocolSection.outcomesModule` for deciding whether a primary measure is posted in results, and ignore `SECONDARY`, `OTHER_PRE_SPECIFIED`, and `POST_HOC` measures.)
- Include every `PRIMARY` outcome measure in `resultsSection.outcomeMeasuresModule.outcomeMeasures` whose `title` or `description` is about glycated/glycosylated hemoglobin (`HbA1c`, `A1C`, `A1c`, `hemoglobin A1c`, `haemoglobin A1c`, `glycosylated hemoglobin`, `glycated haemoglobin`, including change from baseline, value at a time point, responder proportion reaching an HbA1c target, or a composite primary measure that explicitly includes HbA1c).
- Even if a posted `PRIMARY` HbA1c outcome measure has `0` analysed participants and empty `classes` (e.g. early study termination), its entry IS posted in `outcomeMeasuresModule`, so set `hba1c_primary_measure_posted = "yes"` and record absent fields as `"not_stated"`.
- ONLY if `resultsSection.outcomeMeasuresModule.outcomeMeasures` has NO `PRIMARY` measure about HbA1c: set `hba1c_primary_measure_posted = "no"`, `measures = []`, `baseline_population_total = "not_applicable"`, `baseline_population_total_unit = "not_applicable"`, `participants_started_by_group = []`, `baseline_hba1c_posted = "not_applicable"`, `baseline_hba1c_rows = []`, `baseline_demographic_measures = []`, `baseline_demographic_rows = []`, `race_and_ethnicity_in_one_table = "not_applicable"`, `race_and_ethnicity_in_one_table_row_labels = []`, `countries = []`, and stop there.

### 1. Identity (per HbA1c PRIMARY measure)
- `measure_index`: exact integer index `i` in `resultsSection.outcomeMeasuresModule.outcomeMeasures[i]`.
- `measure_title`: exact entire string at `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].title`. Set `measure_title_quote_text` to that exact string and `measure_title_quote_path` to `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].title`.

### 2. Construct & Unit
- `construct`: choose strictly from:
  - `change_from_baseline_in_hba1c`: the measure's primary construct is change (or difference/reduction/increase) from baseline in HbA1c. Note: if the measure's `title` / `description` defines the measure as change from baseline in HbA1c, choose `change_from_baseline_in_hba1c` even when `classes` also displays helper rows for Baseline and/or endpoint Week HbA1c alongside Change, or displays Baseline and endpoint rows for that change measure.
  - `hba1c_value_at_time_point`: HbA1c level/value at a post-baseline time point (not change from baseline, not responder target).
  - `proportion_reaching_hba1c_target`: proportion, percentage, or count of participants reaching a specified HbA1c threshold alone (e.g. `< 7.0%`, `<= 6.5%`), NOT combined with non-HbA1c conditions.
  - `other`: any other HbA1c construct, such as a composite endpoint combining an HbA1c target with weight/hypoglycemia/rescue conditions, time to glycemic rescue/failure, AUC of HbA1c, etc.
  - `not_stated`: construct is not stated.
  - Provide `construct_quotes` (`[{text, path}]`) from `outcomeMeasures[i].title` (and/or `.description`).
- `unit_as_posted`: exact entire string at `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].unitOfMeasure` if present and non-empty; otherwise `"not_stated"` (with `unit_as_posted_quote_text = ""` and `unit_as_posted_quote_path = ""`). Do NOT copy a unit from `title` or `description` if `unitOfMeasure` is absent.
- `unit_class`: classify strictly from `unit_as_posted` without converting units:
  - `percent_hba1c`: `unit_as_posted` denotes percent / percentage / `%` of HbA1c (e.g. `%`, `percent`, `percentage`, `Percentage of HbA1c`, `Percentage of glycosylated haemoglobin`, `% of total hemoglobin`, `percentage points`, `% HbA1c`). IMPORTANT: on responder/proportion measures, a unit like `"percentage of participants"`, `"% of participants"`, `"% of subjects"`, or `"Participants"` is percentage/count of *participants*, NOT percent HbA1c -> classify those as `"other"`.
  - `mmol_per_mol`: `unit_as_posted` denotes `mmol/mol`.
  - `other`: any other stated unit (including `Participants`, `percentage of participants`, composite units, etc.).
  - `not_stated`: when `unit_as_posted == "not_stated"`.

### 3. Time Point
- `time_frame_as_posted`: exact entire string at `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].timeFrame` if present and non-empty; otherwise `"not_stated"`.
- `time_point_weeks`:
  - Output a single number as a clean string (e.g. `"24"`, `"26"`, `"28"`, `"52"`, no trailing `.0` on integers) ONLY if `time_frame_as_posted` states ONE fixed post-baseline time point in **weeks**.
  - A baseline-to-endpoint expression such as `"Baseline to Week 26"`, `"Baseline, week 26"`, `"Baseline and Week 24"`, `"Week 0, Week 26"`, `"24 weeks"`, `"At Week 24"`, or `"Week 24 (LOCF)"` states one fixed post-baseline time point (`"26"` or `"24"`).
  - `"Up to 52 weeks"`, `"Up to Week 24"`, multiple post-baseline time points (e.g. `"Week 12 and Week 24"`, `"Baseline, Week 12, Week 24"`), or variable windows (`"24 to 52 weeks"`) are NOT one fixed time point -> output `"not_one_fixed_time_point"`.
  - Do NOT convert months or days into weeks! If `timeFrame` states only `"Month 6"`, `"6 months"`, or `"Day 168"` without stating a week number in `timeFrame`, output `"not_one_fixed_time_point"`.

### 4. Analysis Population, Missing Data, and Rescue Therapy
- `population_description_as_posted`: exact entire string at `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].populationDescription` if present and non-empty; otherwise `"not_stated"`.
- `population_class` and `population_defined`:
  Use the record's own term in `populationDescription` (and measure `description` only if `populationDescription` refers to it):
  - If `populationDescription` is `"not_stated"` or states no analysis population (e.g. states only that summaries were not produced): `population_class = "not_stated"`, `population_defined = "not_applicable"`.
  - If the record explicitly names `"Full Analysis Set"` or `"FAS"`: `population_class = "full_analysis_set"`. Set `population_defined = "yes"` if the text states who is included in the FAS (e.g. `"all randomized participants..."`), else `"no"` if it only writes `"Full Analysis Set"` / `"FAS"` without defining inclusion criteria.
  - If the record explicitly names `"Per Protocol"` / `"PP"` / `"Per-Protocol"`: `population_class = "per_protocol"`, with `population_defined = "yes"` if defined, else `"no"`.
  - If the record explicitly names `"Completers"` (or defines the population solely as participants who completed the treatment/study period): `population_class = "completers"`, with `population_defined = "yes"` if defined, else `"no"`.
  - If the record names `"Modified Intent-to-Treat"` / `"Modified Intention-to-Treat"` / `"mITT"`:
    - If it states the modification condition(s), choose `modified_intention_to_treat_with_stated_condition` (`population_defined = "yes"`), EXCEPT if defined strictly and solely as all randomised participants who received >=1 dose of study drug with NO baseline/post-baseline observation requirement, choose `all_randomised_and_treated` (`population_defined = "yes"`), or if mITT is named WITHOUT stating the condition, choose `modified_intention_to_treat_with_stated_condition` and set `population_defined = "no"`.
  - If the record names `"Intent-to-Treat"` / `"Intention-to-Treat"` / `"ITT"` (or describes the population without naming FAS/PP/Completers/mITT):
    - If named `"ITT"` / `"Intent-to-Treat"` WITHOUT defining who is included: `population_class = "all_randomised"`, `population_defined = "no"`.
    - If defined strictly as all randomised participants (with no dose or measurement requirement): `population_class = "all_randomised"`, `population_defined = "yes"`.
    - If defined strictly as all randomised participants who received at least one dose of study medication (with no baseline/post-baseline measurement requirement): `population_class = "all_randomised_and_treated"`, `population_defined = "yes"`.
    - If defined as randomised (with or without >=1 dose) PLUS a stated condition such as having a baseline and/or >=1 post-baseline assessment: `population_class = "modified_intention_to_treat_with_stated_condition"`, `population_defined = "yes"`.
  - Otherwise (e.g. non-randomised open-label cohort, safety set, or `"participants with available data"` without any of the above): `population_class = "other"`, `population_defined = "yes"` if defined else `"no"`.
- `missing_data_method_as_posted`:
  - Check `outcomeMeasures[i]` (`title`, `description`, `populationDescription`, and `analyses[]` fields `statisticalMethod`, `statisticalComment`, `groupDescription`, `otherAnalysisDescription`, `estimateComment`).
  - If the record names a method for handling missing data (for example `Last Observation Carried Forward` / `LOCF`, `Mixed Model for Repeated Measures` / `MMRM` / `mixed model for repeated measurements`, `Multiple Imputation`, `Baseline Observation Carried Forward` / `BOCF`, `return to baseline`, `pattern mixture model`, or explicit `no imputation` / `observed cases without imputation`), copy the exact verbatim substring naming it and give its `{text, path}` in `missing_data_method_quotes`.
  - Note: `"ANCOVA"` or `"t-test"` alone is NOT a missing-data method; if only `"ANCOVA"` is stated with no mention of LOCF, MI, MMRM, observed-cases/no-imputation, etc., output `"not_stated"` and `[]`.
- `data_after_rescue_therapy`:
  - Check `outcomeMeasures[i]` (`title`, `description`, `populationDescription`, `classes[].title`, and `analyses[]` comments/descriptions).
  - Output `"included"` if the measure states that measurements collected after initiation of rescue therapy/medication are included; `"excluded"` if the measure states that measurements after rescue therapy are excluded/censored/set to missing; `"not_stated"` if not stated (even if rescue therapy is mentioned elsewhere without stating whether post-rescue HbA1c values are included or excluded in this measure).
  - Special dual-estimand case: if a single `outcomeMeasures[i]` co-reports BOTH an observation period that includes post-rescue data (e.g. `"In-trial"`) AND an observation period that excludes post-rescue data (e.g. `"On-treatment without rescue medication"`), set `both_rescue_estimands_present = true`, and code `data_after_rescue_therapy` according to `classes[0]` (the first-listed observation period in `classes`), quoting `description` / `classes[0].title`. Otherwise set `both_rescue_estimands_present = false`.

### 5. Arms, Comparator Class, and Background Therapy
- `parameter_type`: exact string at `outcomeMeasures[i].paramType` if present, else `"not_stated"`. Do NOT expand enum strings (keep `"LEAST_SQUARES_MEAN"`, `"MEAN"`, etc. verbatim).
- `dispersion_type`: exact string at `outcomeMeasures[i].dispersionType` if present, else `"not_stated"`.
- `arm_groups`: in exact array order of `outcomeMeasures[i].groups[g]`, record `group_id`, `group_title` (with exact quote text & path `resultsSection.outcomeMeasuresModule.outcomeMeasures[i].groups[g].title`), `number_analysed` (exact `value` for `group_id` in `outcomeMeasures[i].denoms[0].counts`, or `"not_stated"`), and `number_analysed_unit` (exact `outcomeMeasures[i].denoms[0].units`, or `"not_stated"`).
- `arm_measurements`: iterate `c = 0..len(classes)-1`, `k = 0..len(classes[c].categories)-1`, and `outcomeMeasures[i].groups` order; for each group's measurement in `classes[c].categories[k].measurements`, record `group_id`, `class_index` (`c`), `category_index` (`k`), `class_title` (`classes[c].title` or `""`), `category_title` (`categories[k].title` or `""`), `row_number_analysed` (if `classes[c].denoms` exists, exact `value` for `group_id`, else `""`), `value` (or `"not_stated"`), `spread` (or `"not_stated"`), `lower_limit` (or `"not_stated"`), `upper_limit` (or `"not_stated"`). If `classes` is empty/absent, output `[]`.
- `comparator_class`: determine **FROM THE ARM DESCRIPTIONS ONLY** (`outcomeMeasures[i].groups[].title` and `.description`; consult `protocolSection.armsInterventionsModule.armGroups[]` ONLY if `outcomeMeasures[i].groups[].description` is absent):
  - `placebo`: 1 or more doses/regimens of the investigational drug/combination compared against a placebo control arm only (with or without identical continuing background therapy across arms), with NO distinct active comparator drug arm. (Note: double-dummy matching placebo tablets inside an active-drug arm do NOT make it a placebo control arm.)
  - `active_drug`: 1 or more doses/regimens of an investigational drug/combination compared against a different active drug/regimen (with NO placebo-only control arm).
  - `dose_comparison_of_same_drug`: all arms receive the SAME active drug(s) at different doses, frequencies, formulations, or titration algorithms, with NO placebo control arm and NO different active drug arm.
  - `mixed`: the measure's arms include BOTH a placebo control arm AND at least one distinct active comparator drug arm (for example `Test Drug`, `Active Reference Drug`, and `Placebo`; or factorial `Drug A + Drug B`, `Drug A`, `Drug B`, `Placebo`).
  - `not_determinable`: single-arm measure (only 1 group in `outcomeMeasures[i].groups`), or arm titles/descriptions do not state enough to determine the class without outside knowledge.
  - Provide `comparator_class_quotes` quoting verbatim from `outcomeMeasures[i].groups[g].description` (or `.title` if `.description` is absent, or `armGroups` only if measure group descriptions are absent).
- `background_therapy_as_posted`: check `outcomeMeasures[i].groups[]`, `protocolSection.armsInterventionsModule.armGroups[]`, and `protocolSection.eligibilityModule.eligibilityCriteria`. If the text names a continuing background antidiabetic therapy (or explicitly `"diet and exercise"` as continuing background regimen in arm descriptions) taken across the treatment period (for example `"metformin"`, `"metformin and sulfonylurea"`, `"basal insulin"`), record the concise verbatim substring from the record and its `{text, path}` in `background_therapy_quotes` (preferring `outcomeMeasures[i].groups[].description` when stated there). If none is stated (e.g. monotherapy with no stated continuing background therapy), output `"not_stated"` and `[]`.

### 6. Between-Arm Analyses
- Inspect `outcomeMeasures[i].analyses[a]` in array order `a = 0, 1, ...`.
- Include every posted **between-arm** analysis. (Exclude an analysis ONLY if the record explicitly states it is a within-single-arm test, such as `paramType: "Change within treatment group"` testing change = 0 within one arm. Keep between-arm treatment-difference analyses even if the submitter listed only 1 `groupId` in `groupIds` — and copy `groupIds` EXACTLY as posted without adding the missing groupId!)
- Copy `estimate_type_as_posted` (`paramType`), `estimate` (`paramValue`), `ci_percent` (`ciPctValue`), `ci_lower` (`ciLowerLimit`), `ci_upper` (`ciUpperLimit`), `ci_sides` (`ciNumSides`), `p_value_as_posted` (`pValue`, preserving `<` or `=`), `non_inferiority_type_raw` (`nonInferiorityType`), and `statistical_method_as_posted` (`statisticalMethod`) verbatim if present, else `"not_stated"`.
- `hypothesis_class`:
  - `"SUPERIORITY"` in `nonInferiorityType` -> `"superiority"` (unless text explicitly contradicts).
  - `"NON_INFERIORITY"` in `nonInferiorityType` -> `"non_inferiority"`.
  - `"EQUIVALENCE"` in `nonInferiorityType` -> `"equivalence"`.
  - When `nonInferiorityType` is `"SUPERIORITY_OR_OTHER"` or `"SUPERIORITY_OR_OTHER_LEGACY"`: if text in this analysis (`nonInferiorityComment`, `pValueComment`, `groupDescription`, `statisticalComment`, `estimateComment`, `otherAnalysisDescription`) or measure `description` explicitly states superiority or a test of zero difference / equality between arms, output `"superiority"`; if text explicitly describes a non-superiority other test, output `"other"`; if NO text disambiguates `"SUPERIORITY_OR_OTHER"`, output `"not_stated"` (never guess `"superiority"` by default!).
  - When `nonInferiorityType` is `"NON_INFERIORITY_OR_EQUIVALENCE"` or `"NON_INFERIORITY_OR_EQUIVALENCE_LEGACY"`: if text explicitly states non-inferiority (or gives a one-sided non-inferiority margin), output `"non_inferiority"`; if text explicitly states two-sided equivalence, output `"equivalence"`; if NO text disambiguates between non-inferiority and equivalence, output `"not_stated"`.
  - When `nonInferiorityType` is `"OTHER"`: if text in this analysis explicitly states it is a test of superiority (`"test of no difference from 0 (superiority)"`), output `"superiority"`; otherwise `"other"`.
  - When `nonInferiorityType` is absent and no text states the hypothesis type: `"not_stated"`.
- `non_inferiority_margin_as_posted`: if a non-inferiority (or equivalence) margin is stated in `analyses[a]` (`nonInferiorityComment`, `pValueComment`, `groupDescription`, etc.) or `outcomeMeasures[i].description` for this analysis, copy the exact verbatim substring stating the margin (e.g. `"0.4%"` or `"Pre-specified non-inferiority margin was 0.4%"`) and its `{text, path}`; otherwise `"not_stated"` (and `""` for quote text/path).

### 7. Baseline (`resultsSection.baselineCharacteristicsModule` and `participantFlowModule` ONLY)
- `baseline_population_total`: exact `value` in `resultsSection.baselineCharacteristicsModule.denoms[0].counts` for the Total/Overall baseline group (e.g. `BG002` titled `"Total"`), and `baseline_population_total_unit` from `denoms[0].units`.
- `participants_started_by_group`: from `resultsSection.participantFlowModule.periods[0]`, find the milestone with `type == "STARTED"` (first milestone) and list every achievement in array order with its `group_id`, matching `group_title` from `participantFlowModule.groups`, and exact `num_subjects` string. Do NOT reconcile with `baseline_population_total`.
- `baseline_hba1c_posted` & `baseline_hba1c_rows`: look ONLY in `resultsSection.baselineCharacteristicsModule.measures[]` (NEVER in `outcomeMeasuresModule`!). If one or more baseline measures report HbA1c / A1C, set `baseline_hba1c_posted = "yes"` and list every `(measure, class, category, group)` measurement in `baseline_hba1c_rows` (including all baseline groups `BG000, BG001, ..., Total`). If no measure in `baselineCharacteristicsModule.measures` is HbA1c, set `baseline_hba1c_posted = "no"` and `baseline_hba1c_rows = []`.
- For `race`, `ethnicity`, `sex`, and `age` separately (`baseline_demographic_measures` and `baseline_demographic_rows`):
  - Inspect every measure `m` in `resultsSection.baselineCharacteristicsModule.measures[m]`.
  - Classify each measure that reports participant `sex` (or `gender`), `age` (include BOTH continuous age measures and categorical age measures if both are posted!), `race`, or `ethnicity`:
    - IMPORTANT: ClinicalTrials.gov often titles two separate measures `"Race/Ethnicity, Customized"`, where one measure has race categories (`White`, `Black`, `Asian`, ...) and the other measure has ethnicity categories (`Hispanic or Latino`, `Not Hispanic or Latino`). Assign each measure to `"race"` or `"ethnicity"` based on its actual rows/description!
    - If a SINGLE `measures[m]` genuinely contains BOTH race row(s) AND ethnicity row(s) in that same measure (e.g. `White`, `Black`, `Hispanic`, `Asian`, `Other` all inside `measures[2]`), include `measures[m]` under `domain = "race"` AND also under `domain = "ethnicity"` so both domains record that combined measure's title and rows!
  - For each included `(domain, baseline_measure_index)`:
    - Record `measure_title` verbatim with `measure_title_quote_text` and `measure_title_quote_path = resultsSection.baselineCharacteristicsModule.measures[m].title`.
    - Iterate `c = 0..len(classes)-1` and `k = 0..len(classes[c].categories)-1` with sequential `row_index = 0, 1, 2, ...` within that measure, recording `class_title` (`classes[c].title` or `""`), `category_title` (`categories[k].title` or `""`), `total_group_id` (the `id` of the Total/Overall group in `baselineCharacteristicsModule.groups`), `total_value` (exact `value` for `total_group_id` in `categories[k].measurements`, or `"not_stated"`), and `total_spread` (exact `spread` for `total_group_id` if present, else `""`).
- `race_and_ethnicity_in_one_table`:
  - `"not_applicable"` if `baselineCharacteristicsModule.measures` has NO race measure and NO ethnicity measure.
  - `"yes"` ONLY if a SINGLE `baselineCharacteristicsModule.measures[m]` contains BOTH at least one race row label (e.g. `White`, `Black`, `Black or African American`, `Caucasian`, `Asian`, `American Indian or Alaska Native`, `Native Hawaiian or Other Pacific Islander`, etc.) AND at least one ethnicity row label (e.g. `Hispanic`, `Hispanic or Latino`, `Not Hispanic or Latino`, `Non-Hispanic`, `Latino`, etc.) as rows of that SAME measure `m`. When `"yes"`, list the exact verbatim row labels from that measure that made you say `"yes"` in `race_and_ethnicity_in_one_table_row_labels`.
  - `"no"` otherwise (with `race_and_ethnicity_in_one_table_row_labels = []`).

### 8. Where the Trial Ran (`countries`)
- From `protocolSection.contactsLocationsModule.locations[].country`, count the number of listed site objects in `locations` for each distinct `country` string (exact country name as posted), sorted alphabetically by `country`. (Remember: listed sites are site counts, NOT participant counts.) If `locations` is absent or empty, output `[]`.
"""


ADJUDICATOR_SYSTEM_PROMPT = """You are the independent third-reader Adjudicator for a clinical trial registry reading audit.
Two independent readers read the same stored ClinicalTrials.gov v2 record and differed on one or more fields.
You receive ONLY:
1. The stored ClinicalTrials.gov v2 record for this single trial (with exact JSON paths and `<<<...>>>` verbatim string delimiters).
2. The exact Coding Sheet Rules.
3. The list of differing fields (`field_key`), with `reading_1` and `reading_2` for each differing field.

ABSOLUTE RULES:
1. Do NOT call any tools other than `finish`.
2. Re-read the stored record directly and choose what the stored record literally states, quoting the exact verbatim text and exact JSON path in `deciding_quotes` (`[{text, path}]`). Do NOT include `<<<` or `>>>` delimiters in quotes.
3. Never infer, harmonise, convert, impute, or use any outside knowledge of the trial or drug.
4. If `reading_1` follows the literal record and Coding Sheet Rules, set `choice = "reading_1"`. If `reading_2` follows the literal record and Coding Sheet Rules, set `choice = "reading_2"`. If neither `reading_1` nor `reading_2` captured the exact literal value from the record (for example, both had a minor typo or incomplete list when the record clearly states the exact value), set `choice = "custom_verbatim"` and provide the JSON-encoded value in `custom_value_json`.
5. If the text in the stored record genuinely does NOT decide between the readings, set `choice = "unresolved"` and `custom_value_json = ""`.
"""


def load_stored_record(records_dir: Path, nct_id: str) -> dict[str, Any]:
    gz_path = records_dir / f"{nct_id}.json.gz"
    return json.loads(gzip.decompress(gz_path.read_bytes()).decode("utf-8"))


def _format_scalar(val: Any) -> str:
    if isinstance(val, str):
        return f"<<<{val}>>>"
    return json.dumps(val, ensure_ascii=False)


def _walk_lines(obj: Any, prefix: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        if not obj:
            out.append(f"{prefix}: {{}}")
            return
        # Compact leaf measurement dicts (groupId/value/spread/lowerLimit/upperLimit/numSubjects/comment)
        scalar_only = all(not isinstance(v, (dict, list)) for v in obj.values())
        if scalar_only and set(obj.keys()).issubset(
            {"groupId", "value", "spread", "lowerLimit", "upperLimit", "numSubjects", "id"}
        ):
            parts = [f"{k}: {_format_scalar(v)}" for k, v in obj.items()]
            out.append(f"{prefix}: {{{', '.join(parts)}}}")
            return
        for k, v in obj.items():
            next_p = f"{prefix}.{k}" if prefix else k
            _walk_lines(v, next_p, out)
    elif isinstance(obj, list):
        if not obj:
            out.append(f"{prefix}: []")
            return
        for idx, item in enumerate(obj):
            _walk_lines(item, f"{prefix}[{idx}]", out)
    else:
        out.append(f"{prefix}: {_format_scalar(obj)}")


def render_record_for_reader(nct_id: str, raw_record: dict[str, Any]) -> str:
    """Render all relevant sections of the stored record losslessly with exact JSON paths."""
    lines: list[str] = [f"=== STORED CLINICALTRIALS.GOV v2 RECORD FOR {nct_id} ==="]
    lines.append(f"hasResults: {_format_scalar(raw_record.get('hasResults'))}")

    ps = raw_record.get("protocolSection", {})
    lines.append("\n--- protocolSection ---")
    for mod_name in (
        "identificationModule",
        "statusModule",
        "descriptionModule",
        "conditionsModule",
        "designModule",
        "armsInterventionsModule",
        "outcomesModule",
        "eligibilityModule",
    ):
        if mod_name not in ps:
            continue
        if mod_name == "outcomesModule":
            om_proto = ps["outcomesModule"]
            if "primaryOutcomes" in om_proto:
                _walk_lines(om_proto["primaryOutcomes"], "protocolSection.outcomesModule.primaryOutcomes", lines)
            for non_pri_k in ("secondaryOutcomes", "otherOutcomes"):
                if non_pri_k in om_proto:
                    lines.append(
                        f"# protocolSection.outcomesModule.{non_pri_k}: ({len(om_proto[non_pri_k])} non-primary protocol outcomes omitted)"
                    )
        else:
            _walk_lines(ps[mod_name], f"protocolSection.{mod_name}", lines)

    cl = ps.get("contactsLocationsModule", {})
    locs = cl.get("locations", [])
    if not locs:
        lines.append("protocolSection.contactsLocationsModule.locations: []")
    else:
        lines.append(f"# Total listed location entries in protocolSection.contactsLocationsModule.locations: {len(locs)}")
        for i, loc in enumerate(locs):
            c_val = loc.get("country")
            lines.append(
                f"protocolSection.contactsLocationsModule.locations[{i}].country: {_format_scalar(c_val)}"
            )

    rs = raw_record.get("resultsSection", {})
    lines.append("\n--- resultsSection.participantFlowModule ---")
    if "participantFlowModule" in rs:
        pf_mod = rs["participantFlowModule"]
        for k, v in pf_mod.items():
            if k == "periods" and isinstance(v, list):
                for pi, per in enumerate(v):
                    for pk, pv in per.items():
                        if pk == "dropWithdraws":
                            lines.append(
                                f"# resultsSection.participantFlowModule.periods[{pi}].dropWithdraws: ({len(pv)} withdrawal-reason rows omitted)"
                            )
                        else:
                            _walk_lines(pv, f"resultsSection.participantFlowModule.periods[{pi}].{pk}", lines)
            else:
                _walk_lines(v, f"resultsSection.participantFlowModule.{k}", lines)
    else:
        lines.append("resultsSection.participantFlowModule: (not present)")

    lines.append("\n--- resultsSection.baselineCharacteristicsModule ---")
    if "baselineCharacteristicsModule" in rs:
        _walk_lines(rs["baselineCharacteristicsModule"], "resultsSection.baselineCharacteristicsModule", lines)
    else:
        lines.append("resultsSection.baselineCharacteristicsModule: (not present)")

    lines.append("\n--- resultsSection.outcomeMeasuresModule ---")
    om_mod = rs.get("outcomeMeasuresModule", {})
    for k, v in om_mod.items():
        if k != "outcomeMeasures":
            _walk_lines(v, f"resultsSection.outcomeMeasuresModule.{k}", lines)
    oms = om_mod.get("outcomeMeasures", [])
    if not oms:
        lines.append("resultsSection.outcomeMeasuresModule.outcomeMeasures: []")
    else:
        non_pri_summary = []
        for i, m in enumerate(oms):
            m_type = m.get("type")
            if m_type == "PRIMARY":
                lines.append(f"\n# outcomeMeasures[{i}] (PRIMARY - full verbatim object):")
                _walk_lines(m, f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{i}]", lines)
            else:
                non_pri_summary.append(f"{i}:{m_type}")
        if non_pri_summary:
            lines.append(
                f"\n# Non-PRIMARY outcomeMeasures omitted ({len(non_pri_summary)} items, index:type): {', '.join(non_pri_summary)}"
            )

    if "moreInfoModule" in rs:
        lines.append("\n--- resultsSection.moreInfoModule ---")
        _walk_lines(rs["moreInfoModule"], "resultsSection.moreInfoModule", lines)

    return "\n".join(lines)


def build_reader_prompt(nct_id: str, raw_record: dict[str, Any], reader_slot: int, concise_retry: bool = False) -> str:
    rendered = render_record_for_reader(nct_id, raw_record)
    retry_note = (
        "CRITICAL OPERATIONAL NOTE: Keep your internal chain-of-thought very brief and focused so you do not exhaust the output token limit before completing the `finish` tool call.\n"
        if concise_retry
        else "Keep your internal reasoning concise on multi-arm/multi-analysis records so your `finish` tool call completes cleanly in one turn.\n"
    )
    return (
        f"Read stored ClinicalTrials.gov record {nct_id} (Independent Reader {reader_slot}).\n"
        f"Do NOT call any tools other than `finish`. Read ONLY the stored record below.\n"
        f"{retry_note}\n"
        f"{CODING_INSTRUCTIONS_BODY}\n\n"
        f"{rendered}\n"
    )


def build_adjudicator_prompt(
    nct_id: str,
    raw_record: dict[str, Any],
    disagreements: list[dict[str, Any]],
) -> str:
    rendered = render_record_for_reader(nct_id, raw_record)
    diffs_json = json.dumps(disagreements, indent=2, ensure_ascii=False)
    return (
        f"Adjudicate differing fields for ClinicalTrials.gov record {nct_id}.\n"
        f"Do NOT call any tools other than `finish`. Re-read ONLY the stored record below and resolve each `field_key` in `DISAGREEMENTS`.\n\n"
        f"{CODING_INSTRUCTIONS_BODY}\n\n"
        f"=== DISAGREEMENTS TO ADJUDICATE ===\n{diffs_json}\n\n"
        f"{rendered}\n"
    )
