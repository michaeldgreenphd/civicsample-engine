#!/usr/bin/env python3
"""Stage 2 schemas, verbatim prompts, deterministic comparator, and quote verifier.

Used by:
- scripts/run_stage2_workflow.py (orchestrates isolated Reader 1, Reader 2, and Adjudicator workers)
- scripts/stage3_and_deliverables.py (builds mapping.jsonl, contrasts.csv, trial_alignment.csv, summary.md, method.md)
- scripts/verify_alignment_audit.py (independently re-verifies every quote in mapping.jsonl)
"""

from __future__ import annotations

import gzip
import json
import pathlib
import re
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
COHORT_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_cohort_reading"
ALIGN_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_population_alignment"
RECORDS_DIR = COHORT_DIR / "records"
INTERMEDIATE_DIR = ALIGN_DIR / "intermediate"

QUOTE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "text"],
    "properties": {
        "path": {"type": "string"},
        "text": {"type": "string"},
    },
}

READER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "nct_id",
        "og_to_bg",
        "og_to_fg",
        "analysis_arm_roles",
        "baseline_table_scope",
    ],
    "properties": {
        "nct_id": {"type": "string"},
        "og_to_bg": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "measure_index",
                    "og_group_id",
                    "mapping_status",
                    "bg_group_ids",
                    "quotes",
                ],
                "properties": {
                    "measure_index": {"type": "integer"},
                    "og_group_id": {"type": "string"},
                    "mapping_status": {
                        "type": "string",
                        "enum": ["same_arm", "combined", "no_counterpart", "unresolved"],
                    },
                    "bg_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "quotes": {
                        "type": "array",
                        "items": QUOTE_ITEM_SCHEMA,
                    },
                },
            },
        },
        "og_to_fg": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "measure_index",
                    "og_group_id",
                    "mapping_status",
                    "fg_group_ids",
                    "quotes",
                ],
                "properties": {
                    "measure_index": {"type": "integer"},
                    "og_group_id": {"type": "string"},
                    "mapping_status": {
                        "type": "string",
                        "enum": ["same_arm", "combined", "no_counterpart", "unresolved"],
                    },
                    "fg_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "quotes": {
                        "type": "array",
                        "items": QUOTE_ITEM_SCHEMA,
                    },
                },
            },
        },
        "analysis_arm_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "measure_index",
                    "analysis_index",
                    "group_id",
                    "arm_role",
                    "quotes",
                ],
                "properties": {
                    "measure_index": {"type": "integer"},
                    "analysis_index": {"type": "integer"},
                    "group_id": {"type": "string"},
                    "arm_role": {
                        "type": "string",
                        "enum": [
                            "experimental",
                            "comparator_placebo",
                            "comparator_active",
                            "comparator_same_drug_other_dose",
                            "other",
                            "unresolved",
                        ],
                    },
                    "quotes": {
                        "type": "array",
                        "items": QUOTE_ITEM_SCHEMA,
                    },
                },
            },
        },
        "baseline_table_scope": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "includes_outside_hba1c_analysis",
                "reason_summary",
                "quotes",
            ],
            "properties": {
                "includes_outside_hba1c_analysis": {
                    "type": "string",
                    "enum": ["yes", "no", "not_determinable"],
                },
                "reason_summary": {"type": "string"},
                "quotes": {
                    "type": "array",
                    "items": QUOTE_ITEM_SCHEMA,
                },
            },
        },
    },
}

ADJUDICATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nct_id", "decisions"],
    "properties": {
        "nct_id": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "field_key",
                    "choice",
                    "custom_value_json",
                    "deciding_quotes",
                    "rationale",
                ],
                "properties": {
                    "field_key": {"type": "string"},
                    "choice": {
                        "type": "string",
                        "enum": ["reading_1", "reading_2", "custom_verbatim", "unresolved"],
                    },
                    "custom_value_json": {"type": "string"},
                    "deciding_quotes": {
                        "type": "array",
                        "items": QUOTE_ITEM_SCHEMA,
                    },
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

READER_SYSTEM_PROMPT = """You are an independent clinical trial registry alignment reader.
You read ONE stored ClinicalTrials.gov v2 JSON record (rendered with exact JSON paths) together with its Stage 1 deterministic tables, provided directly in your prompt, and return the completed Stage 2 coding object by calling ONLY the `finish` tool.

ABSOLUTE RULES:
1. Do NOT call `read_file`, `grep_search`, `find_by_name`, `search_web`, or ANY tool other than `finish`. Everything you are permitted to read is in your prompt. Call ONLY `finish`.
2. Record ONLY what the stored registry record states, using the exact words it uses and the exact JSON path where they stand.
3. Do NOT infer, harmonise, convert, impute, or correct anything. Never use publications, sponsor websites, FDA documents, AACT, or outside pharmacology knowledge of any trial or drug.
4. Keep `not_stated` (the record omits it), `unresolved` (the text does not decide), `not_retrieved`, and `could_not_be_parsed` strictly separate.
5. String values in the rendered stored record are displayed inside `<<<...>>>` delimiters (for example `resultsSection.outcomeMeasuresModule.outcomeMeasures[0].groups[0].title: <<<Placebo>>>`). The characters inside `<<<` and `>>>` are the exact raw characters at that JSON path. Do NOT include `<<<` or `>>>` in your output. Every `quotes` item `{path, text}` MUST have `text` as a non-empty exact character-for-character, case-sensitive substring of the scalar value at `path` in the stored JSON record."""

ADJUDICATOR_SYSTEM_PROMPT = """You are the independent third-reader Adjudicator for a clinical trial registry population-alignment audit.
Two independent readers coded the same stored ClinicalTrials.gov v2 record and differed on one or more fields.
You receive ONLY:
1. The stored ClinicalTrials.gov v2 record for this single trial (rendered with exact JSON paths and `<<<...>>>` verbatim string delimiters) plus its Stage 1 deterministic tables.
2. The Stage 2 Coding Rules.
3. The list of differing fields (`field_key`), showing `reading_1` and `reading_2` (with their quotes).

ABSOLUTE RULES:
1. Do NOT call `read_file`, `grep_search`, `find_by_name`, `search_web`, or ANY tool other than `finish`. Call ONLY `finish`.
2. Re-read the stored record directly and choose what the stored record literally decides, quoting the exact verbatim text and exact JSON path in `deciding_quotes` (`[{path, text}]`). Do NOT include `<<<` or `>>>` delimiters in quotes.
3. Never infer, harmonise, convert, impute, or use outside knowledge of any trial or drug.
4. If `reading_1` follows the literal record and Stage 2 Coding Rules (and its quotes are valid verbatim quotes at allowed paths), set `choice = "reading_1"` and `custom_value_json = ""`.
5. If `reading_2` follows the literal record and Stage 2 Coding Rules (and its quotes are valid verbatim quotes at allowed paths), set `choice = "reading_2"` and `custom_value_json = ""`.
6. If the record text clearly decides the value under the Stage 2 Coding Rules, yet neither `reading_1` nor `reading_2` gave the exact canonical value + valid verbatim quotes, set `choice = "custom_verbatim"` and provide the JSON-encoded resolved value in `custom_value_json` (matching the field's structure) along with `deciding_quotes`.
7. If the text in the stored record genuinely does NOT decide between the readings, set `choice = "unresolved"` and `custom_value_json = ""`."""

STAGE2_CODING_RULES = """## Stage 2 Coding Rules (Apply Literally)

### General Quote Verification Requirement
- Every item in `og_to_bg`, `og_to_fg`, `analysis_arm_roles`, and `baseline_table_scope` MUST carry a non-empty `quotes` array (`[{"path": "...", "text": "..."}]`).
- Every `text` MUST be a non-empty exact character-for-character, case-sensitive substring of the scalar string/number at `path` in the stored JSON record (shown inside `<<<...>>>`). Never strip punctuation, fix typos, change case, or paraphrase inside `text`.

### (a) `og_to_bg`: Outcome Group (`OG`) to Baseline Group (`BG`) Mapping
For **every** outcome group (`og_group_id`) of **every** qualifying HbA1c primary measure (`measure_index`), in exact Stage 1 order:
- Choose `mapping_status` from:
  - `"same_arm"`: Exactly ONE non-Total baseline group (`BG...`) in `resultsSection.baselineCharacteristicsModule.groups` describes this outcome group's treatment arm/regimen. Set `bg_group_ids` to `["BGxxx"]` (length 1).
    - Note: Never map to the `"Total"` summary group (`is_total_group = True`).
    - Note: If `OG` and `BG` describe the same randomised treatment arm across phases (for example `OG` is `"Placebo"` or `"Omarigliptin (Phase A)"` for the 24–26 week period while `BG` is `"Placebo/Sitagliptin"` or `"Placebo to Omarigliptin (Phase A)"` naming both the 24–26 week treatment and a later extension switch), or if the outcome measure analyses an explicitly named subpopulation within that same treatment arm (e.g. renal-impairment subgroup or China subpopulation of that arm), `mapping_status` is `"same_arm"` (`["BGxxx"]`); any difference between baseline `N` and analysed `N` is recorded separately in `(d)` and Stage 3 attrition.
    - Note: Check arm titles and descriptions carefully—`OG000` is NOT always `BG000` if the sponsor listed arms in a different order across modules!
  - `"combined"`: The outcome group explicitly pools/combines TWO OR MORE non-Total baseline groups (for example `"Pooled Dulaglutide"` pooling `0.75 mg` and `1.5 mg`, `"Placebo (Combined)"` pooling two placebo titration regimens, `"Combined Ertugliflozin"` pooling three ertugliflozin arms, or `"Pioglitazone Alone"` pooling pioglitazone 15/30/45 mg). Set `bg_group_ids` to the list of constituent `BG` IDs sorted in ascending ID order (e.g. `["BG001", "BG002"]`, length >= 2).
  - `"no_counterpart"`: No group in `resultsSection.baselineCharacteristicsModule.groups` describes this outcome group's treatment arm (for example when `baselineCharacteristicsModule.groups` contains ONLY pre-randomisation open-label run-in/stabilisation cohorts such as `"Empa 10 mg OL"` and `"Empa 25 mg OL"` rather than the subsequent double-blind randomised `Linagliptin` vs `Placebo` arms). Set `bg_group_ids = []`.
  - `"unresolved"`: Group titles and descriptions are ambiguous and do not decide which `BG`(s) correspond to this `OG`. Set `bg_group_ids = []`.
- **STRICT EVIDENCE RULE FOR `og_to_bg`**:
  - Match **from group titles and descriptions ONLY**. **NEVER match on participant counts!**
  - Every `{path, text}` in `og_to_bg[].quotes` MUST have `path` strictly in:
    - `resultsSection.outcomeMeasuresModule.outcomeMeasures[m].groups[g].title`
    - `resultsSection.outcomeMeasuresModule.outcomeMeasures[m].groups[g].description`
    - `resultsSection.baselineCharacteristicsModule.groups[b].title`
    - `resultsSection.baselineCharacteristicsModule.groups[b].description`
  - Quote both the `OG` title/description and the corresponding `BG` title(s)/description(s) (or, for `no_counterpart`, the `OG` title/description and the `BG` group titles/descriptions showing none corresponds).

### (b) `og_to_fg`: Outcome Group (`OG`) to Participant Flow Group (`FG`) Mapping
For **every** outcome group (`og_group_id`) of **every** qualifying HbA1c primary measure (`measure_index`), in exact Stage 1 order:
- Same `mapping_status` codes (`"same_arm"`, `"combined"`, `"no_counterpart"`, `"unresolved"`) and `fg_group_ids` rules (`["FGxxx"]` of length 1 for `same_arm`, sorted `>= 2` IDs for `combined`, `[]` for `no_counterpart` or `unresolved`).
- Note on multi-stage flow groups: If `participantFlowModule.groups` contains separate pre-randomisation `"Lead-In"` / `"OL"` stabilisation groups alongside the randomised double-blind treatment groups, or separate `"(Extension Period)"` / `"MEE"` groups alongside the main 24–26 week double-blind treatment groups, map the 24–26 week HbA1c `OG` to its corresponding main randomised double-blind `FG` (`"same_arm"`), NOT to a lead-in or extension-only `FG`.
- **STRICT EVIDENCE RULE FOR `og_to_fg`**:
  - Match **from group titles and descriptions ONLY**. **NEVER match on participant counts!**
  - Every `{path, text}` in `og_to_fg[].quotes` MUST have `path` strictly in:
    - `resultsSection.outcomeMeasuresModule.outcomeMeasures[m].groups[g].title`
    - `resultsSection.outcomeMeasuresModule.outcomeMeasures[m].groups[g].description`
    - `resultsSection.participantFlowModule.groups[f].title`
    - `resultsSection.participantFlowModule.groups[f].description`

### (c) `analysis_arm_roles`: Arm Role in Each Qualifying Between-Arm Analysis
For **every** posted between-arm analysis (`analysis_index`) on **every** qualifying HbA1c primary measure (`measure_index`), emit one entry for each `group_id` listed in `analyses[analysis_index].groupIds`, in exact Stage 1 order:
- Choose `arm_role` relative to **that specific between-arm comparison** from:
  - `"experimental"`
  - `"comparator_placebo"`
  - `"comparator_active"`
  - `"comparator_same_drug_other_dose"`
  - `"other"`
  - `"unresolved"`
- **Decision rules (from arm titles, descriptions, and registered `armGroups` ONLY — NEVER from outcome estimates, `nonInferiorityComment`, `pValueComment`, `briefTitle`, `leadSponsor`, or outside pharmacology knowledge)**:
  1. **Placebo comparator in this contrast (`"comparator_placebo"`)**: An arm that administers placebo alone, or placebo added to the exact same continuing background therapy received by the other arm in this contrast (e.g. `Placebo + Metformin` vs `Test Drug + Metformin`, or `Empagliflozin 10 mg + Placebo` vs `Empagliflozin 10 mg + Linagliptin 5 mg`). Double-dummy matching placebo given alongside a distinct active comparator drug (e.g. `Sitagliptin + matching placebo` vs `Exenatide + matching placebo`) does NOT make an active-drug arm `comparator_placebo`—that arm receives an active drug!
     - When an active test arm is compared against a `comparator_placebo` arm, the test arm is `"experimental"` (unless `protocolSection.armsInterventionsModule.armGroups` explicitly registers that active arm as `"ACTIVE_COMPARATOR"` in a 3-arm trial comparing Active Reference vs Placebo, such as `Met + Glim` [`ACTIVE_COMPARATOR`] vs `Met Mono` [`PLACEBO_COMPARATOR`], in which case the Active Reference arm is `"comparator_active"` and the placebo arm is `"comparator_placebo"`).
  2. **Combination / add-on vs constituent component in this contrast (`Drug A + Drug B` vs `Drug A` or `Drug B`)**: When one arm in the contrast administers a multi-drug combination (`Drug A + Drug B`, or fixed-ratio combination `IDegLira`, `iGlarLixi`, `IDegAsp`, or triple therapy `A + B + C`) and the other arm in the contrast administers a proper subset of those active drugs (`Drug A` alone, `Drug B` alone, or `A + C`, with or without matching placebo), the combination/add-on arm is `"experimental"` and the constituent component arm is `"comparator_active"` **in this contrast** (even if that constituent monotherapy arm is `"experimental"` in a *different* contrast against `Placebo`!).
  3. **Same active drug(s) in both arms at different dose, concentration, frequency, meal timing, titration, or delivery method**:
     - If arm titles/descriptions or `protocolSection.armsInterventionsModule.armGroups` explicitly identifies one arm as experimental (`type == "EXPERIMENTAL"` or investigational code such as `HOE901-U300`) and the other arm as comparator (`type == "ACTIVE_COMPARATOR"` or explicit reference wording), code the experimental arm `"experimental"` and the other arm `"comparator_same_drug_other_dose"`.
     - If BOTH arms are registered as `"EXPERIMENTAL"` in `armGroups` and nothing in arm titles or descriptions identifies which of the two is experimental vs comparator (for example two experimental regimens/formulations compared against each other), code BOTH arms `"unresolved"`.
  4. **Two different active drugs/regimens (`Drug A` vs `Drug B`, neither a subset of the other)**:
     - If arm titles/descriptions explicitly identify one arm as experimental vs comparator (e.g. investigational compound code `LY900014`, `LY2189265`, `LY2963016`, `MK-1293`, `SAR342434`, `SAR341402`, or explicit `"Active comparator"` / `"Switching to [test drug]"` text) OR `protocolSection.armsInterventionsModule.armGroups` registers one arm as `"EXPERIMENTAL"` and the other as `"ACTIVE_COMPARATOR"` (linkable by `label`, `description`, or `interventionNames`), code the experimental arm `"experimental"` and the active comparator arm `"comparator_active"`.
     - If BOTH arms are registered as `"EXPERIMENTAL"` in `armGroups` and nothing in arm titles or descriptions distinguishes experimental vs comparator (e.g. two monotherapy arms in a factorial trial or a trial where all `armGroups` have `type == "EXPERIMENTAL"` and arm descriptions state only drug names and doses), code BOTH arms `"unresolved"`.
  5. **Single `groupId` posted in `analyses[a].groupIds`** (when `len(groupIds) == 1`): apply the same rule to that single `groupId` (`"experimental"` if `armGroups` / arm text distinguishes it from an active/placebo comparator, or `"unresolved"` if all `armGroups` have `type == "EXPERIMENTAL"` and arm titles/descriptions alone do not distinguish roles without seeing the omitted second arm).
- **STRICT EVIDENCE RULE FOR `analysis_arm_roles`**:
  - Always quote `resultsSection.outcomeMeasuresModule.outcomeMeasures[m].groups[g].title` (and/or `.description`), AND whenever `protocolSection.armsInterventionsModule.armGroups[k]` (`label`, `description`, `type`, `interventionNames`) or `interventions[j]` (`name`, `description`) is used to link/establish `EXPERIMENTAL` vs `ACTIVE_COMPARATOR` / `PLACEBO_COMPARATOR`, quote those exact `protocolSection.armsInterventionsModule...` paths too!

### (d) `baseline_table_scope`: Whether Baseline Table Includes Participants or Arms Not in the HbA1c Analysis
Evaluate `resultsSection.baselineCharacteristicsModule` relative to the trial's qualifying HbA1c primary outcome measure(s) and their posted between-arm analyses:
- Set `includes_outside_hba1c_analysis` to:
  - `"yes"` if the stored record shows ANY of the following:
    1. **Unanalysed / extra arm or cohort in baseline**: `baselineCharacteristicsModule.groups` (excluding `"Total"`) contains at least one group/cohort that either has `no_counterpart` in a qualifying HbA1c primary measure's `groups[]` OR is omitted from `analyses[].groupIds` on a qualifying HbA1c primary measure (e.g. an open-label high-HbA1c cohort, an open-label run-in/stabilisation cohort, an uncompared reference/placebo/dose arm in `baselineCharacteristicsModule`, an excluded MEE cohort, or arms belonging only to a second qualifying measure when another qualifying measure analyses only a subset of baseline arms).
    2. **Extra participants in baseline (`baseline denom > HbA1c outcome denom`)**: `baselineCharacteristicsModule.denoms[0]` `"Total"` count is strictly greater than the sum of `outcomeMeasures[m].denoms[0]` counts on any qualifying HbA1c primary measure (or any mapped `BG` group's `denoms[0]` count is strictly greater than its mapped `OG` group's `denoms[0]` count — for instance All-Randomized or Safety baseline `N` > mITT/FAS/PP HbA1c `N`, or global baseline `N` > China/renal/background-therapy subpopulation HbA1c `N`), OR `outcomeMeasures[m]` class-level `denoms` + `populationDescription` / `analyses` show that a posted HbA1c MMRM/complete-case analysis without full-FAS imputation excludes baseline-table participants who lacked baseline/post-baseline HbA1c data.
  - `"no"` if ALL of the following hold:
    1. Every non-Total `BG` group in `baselineCharacteristicsModule.groups` maps onto the qualifying HbA1c measure(s)'s analysed `OG` groups (`analyses[].groupIds`), with no extra unanalysed baseline groups;
    2. For every qualifying HbA1c measure (and every mapped arm), `baselineCharacteristicsModule.denoms[0]` count equals `outcomeMeasures[m].denoms[0]` count (`N_BG == N_OM`) — and if `classes[].denoms` has smaller observed visit counts, only when the measure's `populationDescription` explicitly defines the overall analysis population as the full `denoms[0]` set (`FAS` / `ITT`); AND
    3. Either `participantFlowModule.periods[0]` `STARTED` sum ALSO equals `N_BG == N_OM` (so 100% of started/randomised participants are in both tables), OR both `baselineCharacteristicsModule.populationDescription` and `outcomeMeasures[m].populationDescription` are stated and define the same participant set (or one is a mathematical subset of the other with identical finite count `N_BG == N_OM`, proving set equality).
  - `"not_determinable"` if every non-Total `BG` group maps to the analysed `OG` groups and `N_BG == N_OM`, **HOWEVER** `participantFlowModule.periods[0]` `STARTED` sum is strictly greater than `N_BG` (some started participants were excluded) AND `baselineCharacteristicsModule.populationDescription` or `outcomeMeasures[m].populationDescription` is omitted (`not_stated`) or defines potentially non-nested sets so the record text cannot prove whether the `N_BG` baseline participants are the exact same individuals as the `N_OM` HbA1c-analysed participants.
- Quote the deciding verbatim fields in `baseline_table_scope.quotes` (e.g. `baselineCharacteristicsModule.denoms[0].counts[...].value`, `outcomeMeasures[m].denoms[0].counts[...].value`, `baselineCharacteristicsModule.groups[...].title`, `baselineCharacteristicsModule.populationDescription`, `outcomeMeasures[m].populationDescription`, `participantFlowModule.periods[0].milestones[0].achievements[...].numSubjects`).
"""


def resolve_json_path(obj: Any, path: str) -> tuple[bool, Any, str]:
    """Resolve a dot-and-bracket JSON path against a parsed JSON object."""
    if not path or not isinstance(path, str):
        return False, None, "empty_path"
    cur = obj
    for part in path.split("."):
        if "[" in part:
            if not part.endswith("]"):
                return False, None, f"malformed_segment:{part}"
            key, idx_str = part[:-1].split("[", 1)
            if not idx_str.isdigit():
                return False, None, f"non_integer_index:{part}"
            idx = int(idx_str)
            if key:
                if not isinstance(cur, dict) or key not in cur:
                    return False, None, f"missing_key:{key}"
                cur = cur[key]
            if not isinstance(cur, list) or idx < 0 or idx >= len(cur):
                return False, None, f"index_out_of_range:{part}"
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return False, None, f"missing_key:{part}"
            cur = cur[part]
    return True, cur, ""


def _emit_scalar_lines(obj: Any, prefix: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            next_p = f"{prefix}.{k}" if prefix else k
            _emit_scalar_lines(v, next_p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            next_p = f"{prefix}[{i}]"
            _emit_scalar_lines(v, next_p, out)
    elif obj is not None:
        sval = str(obj)
        if "<<<" in sval or ">>>" in sval:
            raise ValueError(f"Delimiter collision at {prefix}: {sval!r}")
        out.append(f"{prefix}: <<<{sval}>>>")


def render_trial_for_stage2(nct_id: str, rec: dict[str, Any], stage1_trial: dict[str, Any]) -> str:
    """Render path-annotated stored record sections + Stage 1 deterministic tables for one trial."""
    lines: list[str] = []
    lines.append(f"=== STAGE 1 DETERMINISTIC TABLES FOR {nct_id} ===")

    # Stage 1 Table 1: Baseline Groups
    lines.append("-- Stage 1.1: Baseline Groups (NOTE: DO NOT use denom_count for OG-to-BG matching!) --")
    lines.append(
        f"baseline_population_description ({stage1_trial['baseline_population_description_path'] or 'absent'}): "
        f"{stage1_trial['baseline_population_description']!r}"
    )
    for bg in stage1_trial["baseline_groups"]:
        lines.append(
            f"  {bg['group_id']} (is_total={bg['is_total_group']}): "
            f"title={bg['title']!r} [{bg['title_path']}] | "
            f"desc={bg['description']!r} [{bg['description_path'] or 'absent'}] | "
            f"denom={bg['denom_count']} {bg['denom_units']} [{bg['denom_count_path']}]"
        )

    # Stage 1 Table 2: Flow Groups
    lines.append("-- Stage 1.2: Participant Flow Groups (NOTE: DO NOT use numSubjects for OG-to-FG matching!) --")
    for fg in stage1_trial["flow_groups"]:
        per_str = "; ".join(
            f"P{p['period_index']}({p['period_title']}): STARTED={p['started_num_subjects']} [{p['started_path']}], "
            f"COMPLETED={p['completed_num_subjects']} [{p['completed_path']}]"
            for p in fg["periods"]
        )
        lines.append(
            f"  {fg['group_id']}: title={fg['title']!r} [{fg['title_path']}] | "
            f"desc={fg['description']!r} [{fg['description_path'] or 'absent'}] | {per_str}"
        )

    # Stage 1 Table 3: Qualifying HbA1c Primary Measures & Posted Between-Arm Analyses
    lines.append("-- Stage 1.3: Qualifying HbA1c Primary Measures, Outcome Groups, and Posted Analyses --")
    for qm in stage1_trial["qualifying_measures"]:
        mi = qm["measure_index"]
        lines.append(
            f"  Measure [{mi}]: title={qm['title']!r} [{qm['title_path']}] | "
            f"unit={qm['unit_of_measure']!r} | timeFrame={qm['time_frame']!r} | "
            f"popDesc={qm['population_description']!r} [{qm['population_description_path'] or 'absent'}]"
        )
        for og in qm["groups"]:
            cd_note = f" | class_denoms={og['class_denoms']}" if og["class_denoms"] else ""
            lines.append(
                f"    {og['group_id']}: title={og['title']!r} [{og['title_path']}] | "
                f"desc={og['description']!r} [{og['description_path'] or 'absent'}] | "
                f"denom={og['denom_count']} {og['denom_units']} [{og['denom_count_path']}]{cd_note}"
            )
        for an in qm["analyses"]:
            if an["is_between_arm"]:
                lines.append(
                    f"    Analysis [{an['analysis_index']}]: groupIds={an['group_ids']} [{an['group_ids_path']}] | "
                    f"has_est_and_ci={an['has_estimate_and_both_ci_limits']} | paramType={an['param_type']!r}"
                )

    lines.append(
        f"-- Stage 1.4: Baseline Demographic Domains Present: {stage1_trial['demographic_measures_present']} --"
    )
    lines.append("")
    lines.append(f"=== STORED RECORD (EXACT JSON PATHS & <<<VERBATIM>>> VALUES) FOR {nct_id} ===")

    rec_lines: list[str] = []
    proto = rec.get("protocolSection", {})
    if "identificationModule" in proto:
        id_mod = proto["identificationModule"]
        if "nctId" in id_mod:
            _emit_scalar_lines(id_mod["nctId"], "protocolSection.identificationModule.nctId", rec_lines)
    if "armsInterventionsModule" in proto:
        _emit_scalar_lines(
            proto["armsInterventionsModule"],
            "protocolSection.armsInterventionsModule",
            rec_lines,
        )

    res = rec.get("resultsSection", {})
    if "participantFlowModule" in res:
        pfm = res["participantFlowModule"]
        for k in ("recruitmentDetails", "preAssignmentDetails", "typeUnitsAnalyzed", "groups"):
            if k in pfm:
                _emit_scalar_lines(pfm[k], f"resultsSection.participantFlowModule.{k}", rec_lines)
        for pi, per in enumerate(pfm.get("periods", [])):
            if "title" in per:
                _emit_scalar_lines(
                    per["title"],
                    f"resultsSection.participantFlowModule.periods[{pi}].title",
                    rec_lines,
                )
            if "milestones" in per:
                _emit_scalar_lines(
                    per["milestones"],
                    f"resultsSection.participantFlowModule.periods[{pi}].milestones",
                    rec_lines,
                )

    if "baselineCharacteristicsModule" in res:
        bcm = res["baselineCharacteristicsModule"]
        for k in ("populationDescription", "typeUnitsAnalyzed", "groups", "denoms"):
            if k in bcm:
                _emit_scalar_lines(
                    bcm[k],
                    f"resultsSection.baselineCharacteristicsModule.{k}",
                    rec_lines,
                )
        for bmi, bm in enumerate(bcm.get("measures", [])):
            for k in ("title", "description", "populationDescription", "paramType", "unitOfMeasure"):
                if k in bm:
                    _emit_scalar_lines(
                        bm[k],
                        f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].{k}",
                        rec_lines,
                    )
            for cli, cls in enumerate(bm.get("classes", [])):
                if "title" in cls:
                    _emit_scalar_lines(
                        cls["title"],
                        f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].classes[{cli}].title",
                        rec_lines,
                    )
                if "denoms" in cls:
                    _emit_scalar_lines(
                        cls["denoms"],
                        f"resultsSection.baselineCharacteristicsModule.measures[{bmi}].classes[{cli}].denoms",
                        rec_lines,
                    )

    qual_mi_set = set(stage1_trial["active_qualifying_measure_indices"])
    if "outcomeMeasuresModule" in res:
        omm = res["outcomeMeasuresModule"]
        for mi, om in enumerate(omm.get("outcomeMeasures", [])):
            base_p = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}]"
            if mi in qual_mi_set:
                # Emit all measure fields except bulky measurement cell values inside classes[].categories
                for k in (
                    "type",
                    "title",
                    "description",
                    "populationDescription",
                    "reportingStatus",
                    "paramType",
                    "dispersionType",
                    "unitOfMeasure",
                    "timeFrame",
                    "groups",
                    "denoms",
                ):
                    if k in om:
                        _emit_scalar_lines(om[k], f"{base_p}.{k}", rec_lines)
                for cli, cls in enumerate(om.get("classes", [])):
                    if "title" in cls:
                        _emit_scalar_lines(cls["title"], f"{base_p}.classes[{cli}].title", rec_lines)
                    if "denoms" in cls:
                        _emit_scalar_lines(cls["denoms"], f"{base_p}.classes[{cli}].denoms", rec_lines)
                if "analyses" in om:
                    _emit_scalar_lines(om["analyses"], f"{base_p}.analyses", rec_lines)
            elif om.get("type") == "PRIMARY":
                for k in ("type", "title", "timeFrame", "populationDescription"):
                    if k in om:
                        _emit_scalar_lines(om[k], f"{base_p}.{k}", rec_lines)

    lines.extend(rec_lines)
    return "\n".join(lines)


def build_reader_prompt(nct_id: str, reader_num: int, rendered_trial: str) -> str:
    return (
        f"Stage 2 alignment coding for ClinicalTrials.gov record {nct_id} (Independent Reader {reader_num}).\n"
        f"Do NOT call any tools other than `finish`. Code ONLY from the stored record and Stage 1 tables below.\n\n"
        f"{STAGE2_CODING_RULES}\n\n"
        f"{rendered_trial}\n"
    )


def build_adjudicator_prompt(
    nct_id: str,
    rendered_trial: str,
    disagreements: list[dict[str, Any]],
) -> str:
    dis_json = json.dumps(disagreements, ensure_ascii=False, indent=2)
    return (
        f"Adjudicate Stage 2 differing fields for ClinicalTrials.gov record {nct_id}.\n"
        f"Do NOT call any tools other than `finish`. Re-read ONLY the stored record and Stage 1 tables below.\n\n"
        f"{STAGE2_CODING_RULES}\n\n"
        f"=== DISAGREEMENTS TO ADJUDICATE ===\n{dis_json}\n\n"
        f"{rendered_trial}\n"
    )


OG_BG_ALLOWED_PATH_RE = re.compile(
    r"^resultsSection\.(outcomeMeasuresModule\.outcomeMeasures\[\d+\]|baselineCharacteristicsModule)"
    r"\.groups\[\d+\]\.(title|description)$"
)
OG_FG_ALLOWED_PATH_RE = re.compile(
    r"^resultsSection\.(outcomeMeasuresModule\.outcomeMeasures\[\d+\]|participantFlowModule)"
    r"\.groups\[\d+\]\.(title|description)$"
)
ARM_ROLE_ALLOWED_PATH_RE = re.compile(
    r"^(resultsSection\.(outcomeMeasuresModule\.outcomeMeasures\[\d+\]|baselineCharacteristicsModule|"
    r"participantFlowModule)\.groups\[\d+\]\.(title|description)|"
    r"protocolSection\.armsInterventionsModule\.armGroups\[\d+\]\.(label|description|type)|"
    r"protocolSection\.armsInterventionsModule\.armGroups\[\d+\]\.interventionNames\[\d+\]|"
    r"protocolSection\.armsInterventionsModule\.interventions\[\d+\]\.(name|description))$"
)


def verify_quotes_list(
    rec: dict[str, Any],
    quotes: list[dict[str, str]],
    context_key: str,
    allowed_path_re: re.Pattern[str] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Verify a list of {path, text} quotes deterministically against raw JSON record.

    Returns (quotes_checked_count, list_of_failures). Never repairs or modifies quotes.
    """
    failures: list[dict[str, Any]] = []
    if not isinstance(quotes, list) or len(quotes) == 0:
        failures.append(
            {
                "context_key": context_key,
                "error": "empty_quotes_list",
                "path": "",
                "text": "",
            }
        )
        return 1, failures

    checked = 0
    for qi, q in enumerate(quotes):
        checked += 1
        qpath = q.get("path", "") if isinstance(q, dict) else ""
        qtext = q.get("text", "") if isinstance(q, dict) else ""
        if not qtext:
            failures.append(
                {
                    "context_key": f"{context_key}.quotes[{qi}]",
                    "error": "empty_quote_text",
                    "path": qpath,
                    "text": qtext,
                }
            )
            continue
        if allowed_path_re is not None and not allowed_path_re.match(qpath):
            failures.append(
                {
                    "context_key": f"{context_key}.quotes[{qi}]",
                    "error": "disallowed_quote_path_for_field",
                    "path": qpath,
                    "text": qtext,
                }
            )
            continue
        ok, resolved, err = resolve_json_path(rec, qpath)
        if not ok:
            failures.append(
                {
                    "context_key": f"{context_key}.quotes[{qi}]",
                    "error": f"path_not_found:{err}",
                    "path": qpath,
                    "text": qtext,
                }
            )
            continue
        if isinstance(resolved, (dict, list)) or resolved is None:
            failures.append(
                {
                    "context_key": f"{context_key}.quotes[{qi}]",
                    "error": f"path_not_scalar:{type(resolved).__name__}",
                    "path": qpath,
                    "text": qtext,
                }
            )
            continue
        resolved_str = str(resolved)
        if qtext not in resolved_str:
            failures.append(
                {
                    "context_key": f"{context_key}.quotes[{qi}]",
                    "error": "verbatim_substring_not_found_at_path",
                    "path": qpath,
                    "text": qtext,
                    "actual_value_at_path": resolved_str[:300],
                }
            )
    return checked, failures


def verify_stage2_reading_quotes(rec: dict[str, Any], reading: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic quote verification across every item of a Stage 2 reading."""
    total_checked = 0
    all_failures: list[dict[str, Any]] = []

    for item in reading.get("og_to_bg", []):
        ck = f"og_to_bg[{item['measure_index']}][{item['og_group_id']}]"
        c, f = verify_quotes_list(rec, item.get("quotes", []), ck, OG_BG_ALLOWED_PATH_RE)
        total_checked += c
        all_failures.extend(f)

    for item in reading.get("og_to_fg", []):
        ck = f"og_to_fg[{item['measure_index']}][{item['og_group_id']}]"
        c, f = verify_quotes_list(rec, item.get("quotes", []), ck, OG_FG_ALLOWED_PATH_RE)
        total_checked += c
        all_failures.extend(f)

    for item in reading.get("analysis_arm_roles", []):
        ck = f"analysis_arm_roles[{item['measure_index']}][{item['analysis_index']}][{item['group_id']}]"
        c, f = verify_quotes_list(rec, item.get("quotes", []), ck, ARM_ROLE_ALLOWED_PATH_RE)
        total_checked += c
        all_failures.extend(f)

    bts = reading.get("baseline_table_scope", {})
    c, f = verify_quotes_list(rec, bts.get("quotes", []), "baseline_table_scope", None)
    total_checked += c
    all_failures.extend(f)

    return {
        "status": "verified" if len(all_failures) == 0 else "quote_verification_failed",
        "quotes_checked": total_checked,
        "quote_failures_count": len(all_failures),
        "failures": all_failures,
    }


def expected_stage2_keys(stage1_trial: dict[str, Any]) -> dict[str, list[Any]]:
    """Return expected canonical keys from Stage 1 for completeness validation."""
    og_keys: list[tuple[int, str]] = []
    role_keys: list[tuple[int, int, str]] = []
    for qm in stage1_trial["qualifying_measures"]:
        mi = qm["measure_index"]
        for og in qm["groups"]:
            og_keys.append((mi, og["group_id"]))
        for an in qm["analyses"]:
            if an["is_between_arm"]:
                ai = an["analysis_index"]
                for gid in an["group_ids"]:
                    role_keys.append((mi, ai, gid))
    return {"og_keys": og_keys, "role_keys": role_keys}


def canonicalize_og_map_val(item: dict[str, Any] | None, id_field: str) -> dict[str, Any]:
    if item is None:
        return {"mapping_status": "MISSING", "target_ids": []}
    status = item.get("mapping_status", "MISSING")
    tids = sorted(set(item.get(id_field, [])))
    if status in ("no_counterpart", "unresolved"):
        tids = []
    return {"mapping_status": status, "target_ids": tids}


def compare_stage2_readings(
    stage1_trial: dict[str, Any],
    r1: dict[str, Any],
    r2: dict[str, Any],
    rec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic field-by-field comparison of Reader 1 and Reader 2 Stage 2 codings.

    Also triggers third-reader adjudication on any field where Reader 1 and Reader 2
    agreed on the categorical value while BOTH or EITHER failed quote verification on
    the chosen reading (if r1 has a quote failure where r2 passed, comparator flags it
    or selects the quote-valid reading only if values are identical; if values differ OR
    both have quote issues, adjudicator is invoked). Wait: to be strictly transparent,
    inter-reader agreement is computed purely on the coded values BEFORE adjudication!
    """
    exp = expected_stage2_keys(stage1_trial)
    r1_bg = {(x["measure_index"], x["og_group_id"]): x for x in r1.get("og_to_bg", [])}
    r2_bg = {(x["measure_index"], x["og_group_id"]): x for x in r2.get("og_to_bg", [])}

    r1_fg = {(x["measure_index"], x["og_group_id"]): x for x in r1.get("og_to_fg", [])}
    r2_fg = {(x["measure_index"], x["og_group_id"]): x for x in r2.get("og_to_fg", [])}

    r1_role = {
        (x["measure_index"], x["analysis_index"], x["group_id"]): x
        for x in r1.get("analysis_arm_roles", [])
    }
    r2_role = {
        (x["measure_index"], x["analysis_index"], x["group_id"]): x
        for x in r2.get("analysis_arm_roles", [])
    }

    fields: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []

    # (a) OG-to-BG
    for mi, ogid in exp["og_keys"]:
        fkey = f"og_to_bg:{mi}:{ogid}"
        i1, i2 = r1_bg.get((mi, ogid)), r2_bg.get((mi, ogid))
        v1 = canonicalize_og_map_val(i1, "bg_group_ids")
        v2 = canonicalize_og_map_val(i2, "bg_group_ids")
        agreed = (
            v1 == v2
            and v1["mapping_status"] != "MISSING"
            and (
                (v1["mapping_status"] == "same_arm" and len(v1["target_ids"]) == 1)
                or (v1["mapping_status"] == "combined" and len(v1["target_ids"]) >= 2)
                or (v1["mapping_status"] in ("no_counterpart", "unresolved") and len(v1["target_ids"]) == 0)
            )
        )
        entry = {
            "field_key": fkey,
            "field_category": "og_to_bg",
            "agreed": agreed,
            "r1_value": v1,
            "r2_value": v2,
            "r1_quotes": i1.get("quotes", []) if i1 else [],
            "r2_quotes": i2.get("quotes", []) if i2 else [],
        }
        fields.append(entry)
        if not agreed:
            disagreements.append(entry)

    # (b) OG-to-FG
    for mi, ogid in exp["og_keys"]:
        fkey = f"og_to_fg:{mi}:{ogid}"
        i1, i2 = r1_fg.get((mi, ogid)), r2_fg.get((mi, ogid))
        v1 = canonicalize_og_map_val(i1, "fg_group_ids")
        v2 = canonicalize_og_map_val(i2, "fg_group_ids")
        agreed = (
            v1 == v2
            and v1["mapping_status"] != "MISSING"
            and (
                (v1["mapping_status"] == "same_arm" and len(v1["target_ids"]) == 1)
                or (v1["mapping_status"] == "combined" and len(v1["target_ids"]) >= 2)
                or (v1["mapping_status"] in ("no_counterpart", "unresolved") and len(v1["target_ids"]) == 0)
            )
        )
        entry = {
            "field_key": fkey,
            "field_category": "og_to_fg",
            "agreed": agreed,
            "r1_value": v1,
            "r2_value": v2,
            "r1_quotes": i1.get("quotes", []) if i1 else [],
            "r2_quotes": i2.get("quotes", []) if i2 else [],
        }
        fields.append(entry)
        if not agreed:
            disagreements.append(entry)

    # (c) Arm roles
    for mi, ai, gid in exp["role_keys"]:
        fkey = f"analysis_arm_roles:{mi}:{ai}:{gid}"
        i1, i2 = r1_role.get((mi, ai, gid)), r2_role.get((mi, ai, gid))
        v1 = i1.get("arm_role", "MISSING") if i1 else "MISSING"
        v2 = i2.get("arm_role", "MISSING") if i2 else "MISSING"
        agreed = v1 == v2 and v1 != "MISSING"
        entry = {
            "field_key": fkey,
            "field_category": "analysis_arm_roles",
            "agreed": agreed,
            "r1_value": v1,
            "r2_value": v2,
            "r1_quotes": i1.get("quotes", []) if i1 else [],
            "r2_quotes": i2.get("quotes", []) if i2 else [],
        }
        fields.append(entry)
        if not agreed:
            disagreements.append(entry)

    # (d) Baseline-table scope
    b1 = r1.get("baseline_table_scope", {})
    b2 = r2.get("baseline_table_scope", {})
    bv1 = b1.get("includes_outside_hba1c_analysis", "MISSING")
    bv2 = b2.get("includes_outside_hba1c_analysis", "MISSING")
    bagreed = bv1 == bv2 and bv1 != "MISSING"
    bentry = {
        "field_key": "baseline_table_scope",
        "field_category": "baseline_table_scope",
        "agreed": bagreed,
        "r1_value": bv1,
        "r2_value": bv2,
        "r1_reason_summary": b1.get("reason_summary", ""),
        "r2_reason_summary": b2.get("reason_summary", ""),
        "r1_quotes": b1.get("quotes", []),
        "r2_quotes": b2.get("quotes", []),
    }
    fields.append(bentry)
    if not bagreed:
        disagreements.append(bentry)

    return {
        "fields_compared_count": len(fields),
        "fields_agreed_count": sum(1 for f in fields if f["agreed"]),
        "fields_disagreed_count": len(disagreements),
        "fields": fields,
        "disagreements": disagreements,
    }


def build_adjudicated_stage2(
    nct_id: str,
    stage1_trial: dict[str, Any],
    r1: dict[str, Any],
    r2: dict[str, Any],
    comparison: dict[str, Any],
    adjudication: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    """Combine agreed fields and third-reader adjudication choices without patching quotes.

    When Reader 1 and Reader 2 agreed on a field, Reader 1's verbatim value and quotes
    are retained untouched (both original readings and their own quote-verification
    outcomes are also kept in mapping.jsonl). When they disagreed, the adjudicator's
    choice ('reading_1', 'reading_2', 'custom_verbatim', or 'unresolved') and quotes are
    applied without silent modification.
    """
    decisions_by_key: dict[str, dict[str, Any]] = {}
    if adjudication:
        for dec in adjudication.get("decisions", []):
            decisions_by_key[dec["field_key"]] = dec

    field_map = {f["field_key"]: f for f in comparison["fields"]}
    exp = expected_stage2_keys(stage1_trial)
    unresolved_from_disagreement = 0

    def resolve_map_item(prefix: str, mi: int, ogid: str, id_field: str) -> dict[str, Any]:
        nonlocal unresolved_from_disagreement
        fkey = f"{prefix}:{mi}:{ogid}"
        fentry = field_map[fkey]
        if fentry["agreed"]:
            val = fentry["r1_value"]
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": val["mapping_status"],
                id_field: list(val["target_ids"]),
                "quotes": list(fentry["r1_quotes"]),
            }
        dec = decisions_by_key.get(fkey)
        if dec is None or dec["choice"] == "unresolved":
            unresolved_from_disagreement += 1
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": "unresolved",
                id_field: [],
                "quotes": dec.get("deciding_quotes", []) if dec else [],
            }
        choice = dec["choice"]
        if choice == "reading_1":
            val = fentry["r1_value"]
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else fentry["r1_quotes"]
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": val["mapping_status"],
                id_field: list(val["target_ids"]),
                "quotes": list(quotes),
            }
        if choice == "reading_2":
            val = fentry["r2_value"]
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else fentry["r2_quotes"]
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": val["mapping_status"],
                id_field: list(val["target_ids"]),
                "quotes": list(quotes),
            }
        # custom_verbatim
        try:
            cobj = json.loads(dec.get("custom_value_json", "{}"))
            mstatus = cobj.get("mapping_status", "unresolved")
            tids = sorted(set(cobj.get(id_field, cobj.get("target_ids", []))))
            if mstatus in ("no_counterpart", "unresolved"):
                tids = []
            if mstatus == "unresolved":
                unresolved_from_disagreement += 1
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": mstatus,
                id_field: tids,
                "quotes": list(dec.get("deciding_quotes", [])),
            }
        except Exception:  # noqa: BLE001
            unresolved_from_disagreement += 1
            return {
                "measure_index": mi,
                "og_group_id": ogid,
                "mapping_status": "unresolved",
                id_field: [],
                "quotes": list(dec.get("deciding_quotes", [])),
            }

    adj_og_to_bg = [resolve_map_item("og_to_bg", mi, ogid, "bg_group_ids") for mi, ogid in exp["og_keys"]]
    adj_og_to_fg = [resolve_map_item("og_to_fg", mi, ogid, "fg_group_ids") for mi, ogid in exp["og_keys"]]

    adj_roles: list[dict[str, Any]] = []
    for mi, ai, gid in exp["role_keys"]:
        fkey = f"analysis_arm_roles:{mi}:{ai}:{gid}"
        fentry = field_map[fkey]
        if fentry["agreed"]:
            adj_roles.append(
                {
                    "measure_index": mi,
                    "analysis_index": ai,
                    "group_id": gid,
                    "arm_role": fentry["r1_value"],
                    "quotes": list(fentry["r1_quotes"]),
                }
            )
            continue
        dec = decisions_by_key.get(fkey)
        if dec is None or dec["choice"] == "unresolved":
            unresolved_from_disagreement += 1
            adj_roles.append(
                {
                    "measure_index": mi,
                    "analysis_index": ai,
                    "group_id": gid,
                    "arm_role": "unresolved",
                    "quotes": list(dec.get("deciding_quotes", [])) if dec else [],
                }
            )
        elif dec["choice"] == "reading_1":
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else fentry["r1_quotes"]
            adj_roles.append(
                {
                    "measure_index": mi,
                    "analysis_index": ai,
                    "group_id": gid,
                    "arm_role": fentry["r1_value"],
                    "quotes": list(quotes),
                }
            )
        elif dec["choice"] == "reading_2":
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else fentry["r2_quotes"]
            adj_roles.append(
                {
                    "measure_index": mi,
                    "analysis_index": ai,
                    "group_id": gid,
                    "arm_role": fentry["r2_value"],
                    "quotes": list(quotes),
                }
            )
        else:
            try:
                parsed = json.loads(dec.get("custom_value_json", "\"unresolved\""))
                role_val = parsed.get("arm_role", "unresolved") if isinstance(parsed, dict) else str(parsed)
            except Exception:  # noqa: BLE001
                role_val = "unresolved"
            if role_val == "unresolved":
                unresolved_from_disagreement += 1
            adj_roles.append(
                {
                    "measure_index": mi,
                    "analysis_index": ai,
                    "group_id": gid,
                    "arm_role": role_val,
                    "quotes": list(dec.get("deciding_quotes", [])),
                }
            )

    # Baseline-table scope
    bfentry = field_map["baseline_table_scope"]
    if bfentry["agreed"]:
        adj_bscope = {
            "includes_outside_hba1c_analysis": bfentry["r1_value"],
            "reason_summary": bfentry["r1_reason_summary"],
            "quotes": list(bfentry["r1_quotes"]),
        }
    else:
        dec = decisions_by_key.get("baseline_table_scope")
        if dec is None or dec["choice"] == "unresolved":
            unresolved_from_disagreement += 1
            adj_bscope = {
                "includes_outside_hba1c_analysis": "unresolved",
                "reason_summary": dec.get("rationale", "unresolved") if dec else "unresolved",
                "quotes": list(dec.get("deciding_quotes", [])) if dec else [],
            }
        elif dec["choice"] == "reading_1":
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else bfentry["r1_quotes"]
            adj_bscope = {
                "includes_outside_hba1c_analysis": bfentry["r1_value"],
                "reason_summary": dec.get("rationale", bfentry["r1_reason_summary"]),
                "quotes": list(quotes),
            }
        elif dec["choice"] == "reading_2":
            quotes = dec["deciding_quotes"] if dec.get("deciding_quotes") else bfentry["r2_quotes"]
            adj_bscope = {
                "includes_outside_hba1c_analysis": bfentry["r2_value"],
                "reason_summary": dec.get("rationale", bfentry["r2_reason_summary"]),
                "quotes": list(quotes),
            }
        else:
            try:
                parsed_b = json.loads(dec.get("custom_value_json", "\"unresolved\""))
                bval = (
                    parsed_b.get("includes_outside_hba1c_analysis", "unresolved")
                    if isinstance(parsed_b, dict)
                    else str(parsed_b)
                )
            except Exception:  # noqa: BLE001
                bval = "unresolved"
            if bval == "unresolved":
                unresolved_from_disagreement += 1
            adj_bscope = {
                "includes_outside_hba1c_analysis": bval,
                "reason_summary": dec.get("rationale", ""),
                "quotes": list(dec.get("deciding_quotes", [])),
            }

    return (
        {
            "nct_id": nct_id,
            "og_to_bg": adj_og_to_bg,
            "og_to_fg": adj_og_to_fg,
            "analysis_arm_roles": adj_roles,
            "baseline_table_scope": adj_bscope,
        },
        unresolved_from_disagreement,
    )


if __name__ == "__main__":
    # Dry-run prompt rendering across all 201 trials to verify prompt sizes and zero delimiter collisions
    with open(INTERMEDIATE_DIR / "stage1_trials.json", encoding="utf-8") as f:
        s1 = json.load(f)
    sizes: list[tuple[int, str]] = []
    for nct in s1["nct_ids"]:
        t = s1["trials"][nct]
        with gzip.open(RECORDS_DIR / f"{nct}.json.gz", "rt", encoding="utf-8") as fp:
            rec = json.load(fp)
        rendered = render_trial_for_stage2(nct, rec, t)
        prompt = build_reader_prompt(nct, 1, rendered)
        sizes.append((len(prompt.encode("utf-8")), nct))
    sizes.sort(reverse=True)
    print(f"Rendered all {len(sizes)} Stage 2 prompts cleanly.")
    print("Top 5 largest prompts (bytes):", sizes[:5])
    print("Median prompt (bytes):", sizes[len(sizes) // 2])
