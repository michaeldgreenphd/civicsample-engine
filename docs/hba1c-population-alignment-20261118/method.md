# Method: Baseline-to-Outcome Population Alignment Audit (201 Qualifying T2D HbA1c Trials)

## 1. Inputs, Isolation, Rate/Concurrency Limits, and Execution Window
- **Network access**: None (`0` HTTP requests). Read strictly from local `docs/audits/hba1c_cohort_reading/records/{NCT_ID}.json.gz` (ClinicalTrials.gov REST API `2.0.5`, data timestamp `2026-11-13T10:00:06`), the 201 qualifying NCT IDs in `docs/audits/hba1c_cohort_reading/summary.md` section 3.1, and `cohort_sheet.csv` + `analyses.csv` for the re-derivation comparison.
- **Scoring-reference isolation**: Neither AACT, `civicsample-engine` pipeline outputs, `clinical-trial-populations` site data, nor earlier prose/chat answers were read or used.
- **Model**: Gemini 4 Carbon (High) via isolated single-use `run_workflow` agents (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`).
- **Worker counts**:
  - Stage 1 deterministic extraction (`scripts/stage1_extract.py`): `1` local Python process (`0` LLM calls).
  - Stage 2 Reader 1 (`hba1c_alignment_reader`): `201` isolated single-trial workers (`1` trial per context).
  - Stage 2 Reader 2 (`hba1c_alignment_reader`): `201` isolated single-trial workers (`1` trial per context, launched concurrently with Reader 1 prior to checkpointing).
  - Stage 2 deterministic comparator (`scripts/stage2_schema_and_prompts.py::compare_stage2_readings`): `0` LLM calls.
  - Stage 2 third-reader Adjudicator (`hba1c_alignment_adjudicator`): `0` isolated single-trial workers (invoked strictly on trials with `>= 1` differing field).
  - Stage 3 derived calculations & deterministic quote verifier (`scripts/stage3_and_deliverables.py`, `scripts/verify_alignment_audit.py`): `0` LLM calls.
  - **Total single-trial LLM worker contexts**: **402**.
- **Concurrency and context limits**:
  - Workflow semaphore cap: at most `16` concurrent LLM agent calls (`8` trials dual-read in parallel), with exponential backoff (`3s * attempt`, max `4` attempts).
  - Prompt sizes across all 201 trials ranged from ~`22 KB` to `105,220` bytes (`NCT00328627`), median `38,485` bytes—well under the ~`192 KB` subagent context truncation ceiling.
- **Stage 2 dual-reading window (UTC)**: `2026-11-17T19:05:35.271719Z` to `2026-11-17T20:24:35.750878Z`.

## 2. Deviations From the Task Specification
- **Data / analytical / workflow deviations**: None. All 201 trials were processed offline from stored gzipped records; Stage 1 and Stage 3 ran purely in deterministic Python code; Stage 2 used two independent single-trial readers per trial, deterministic comparison, third-reader adjudication only on disagreements, and unpatched deterministic quote verification.
- **Inclusivity on posted between-arm analyses (`416` vs. `410`)**: Three qualifying measures (`NCT00528372[0]`, `NCT01517412[0]`, `NCT01733758[0]`) post both analyses with `estimate` + both CI limits (`410` across all `210` qualifying measures) and companion/additional between-arm entries in `analyses.csv` lacking CI limits (`6` entries). Rather than silently dropping those `6` entries, Stage 2 and `contrasts.csv` include all `416` posted between-arm analyses and mark `has_estimate_and_both_ci_limits` (`yes` = `410`, `no` = `6`), reporting counts for both denominators in `summary.md`.
- **Repository commit/push handling**: Neither `michaeldgreenphd/civicsample-engine` nor `michaeldgreenphd/clinical-trial-populations` was committed or pushed to. Because this workspace is a sandboxed git worktree of `michaeldgreenphd/civicsample-engine` without sandboxed `.git` or network write access to `michaeldgreenphd/civicsample-verify-ai-private`, all outputs were left uncommitted in `docs/audits/hba1c_population_alignment/` per instruction.
- **Initial path typo on `stage1_extract.py`**: An initial `write_file` call wrote `stage1_extract.py` to `<local Antigravity worktree>/hba1c_population_alignment/scripts/stage1_extract.py` (sibling path outside this worktree) before immediately writing the script to the workspace path `docs/audits/hba1c_population_alignment/scripts/stage1_extract.py`.

## 3. Offline Reproduction and Independent Verification Commands
From the repository root, with no network access:
```bash
python3 docs/audits/hba1c_population_alignment/scripts/stage1_extract.py
python3 docs/audits/hba1c_population_alignment/scripts/stage3_and_deliverables.py
python3 docs/audits/hba1c_population_alignment/scripts/verify_alignment_audit.py
```

## 4. Verbatim Prompts Given to Stage 2 Workers
### 4.1 Reader System Prompt (`hba1c_alignment_reader`)
````text
You are an independent clinical trial registry alignment reader.
You read ONE stored ClinicalTrials.gov v2 JSON record (rendered with exact JSON paths) together with its Stage 1 deterministic tables, provided directly in your prompt, and return the completed Stage 2 coding object by calling ONLY the `finish` tool.

ABSOLUTE RULES:
1. Do NOT call `read_file`, `grep_search`, `find_by_name`, `search_web`, or ANY tool other than `finish`. Everything you are permitted to read is in your prompt. Call ONLY `finish`.
2. Record ONLY what the stored registry record states, using the exact words it uses and the exact JSON path where they stand.
3. Do NOT infer, harmonise, convert, impute, or correct anything. Never use publications, sponsor websites, FDA documents, AACT, or outside pharmacology knowledge of any trial or drug.
4. Keep `not_stated` (the record omits it), `unresolved` (the text does not decide), `not_retrieved`, and `could_not_be_parsed` strictly separate.
5. String values in the rendered stored record are displayed inside `<<<...>>>` delimiters (for example `resultsSection.outcomeMeasuresModule.outcomeMeasures[0].groups[0].title: <<<Placebo>>>`). The characters inside `<<<` and `>>>` are the exact raw characters at that JSON path. Do NOT include `<<<` or `>>>` in your output. Every `quotes` item `{path, text}` MUST have `text` as a non-empty exact character-for-character, case-sensitive substring of the scalar value at `path` in the stored JSON record.
````

### 4.2 Adjudicator System Prompt (`hba1c_alignment_adjudicator`)
````text
You are the independent third-reader Adjudicator for a clinical trial registry population-alignment audit.
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
7. If the text in the stored record genuinely does NOT decide between the readings, set `choice = "unresolved"` and `custom_value_json = ""`.
````

### 4.3 Verbatim Stage 2 Coding Rules Included in Reader & Adjudicator Prompts
````markdown
## Stage 2 Coding Rules (Apply Literally)

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
````

### 4.4 Reader User Prompt Template
````text
Stage 2 alignment coding for ClinicalTrials.gov record {NCT_ID} (Independent Reader {1_or_2}).
Do NOT call any tools other than `finish`. Code ONLY from the stored record and Stage 1 tables below.

{STAGE2_CODING_RULES}

{RENDERED_STAGE1_TABLES_AND_PATH_ANNOTATED_STORED_RECORD}
````

### 4.5 Adjudicator User Prompt Template
````text
Adjudicate Stage 2 differing fields for ClinicalTrials.gov record {NCT_ID}.
Do NOT call any tools other than `finish`. Re-read ONLY the stored record and Stage 1 tables below.

{STAGE2_CODING_RULES}

=== DISAGREEMENTS TO ADJUDICATE ===
{DISAGREEMENTS_JSON}

{RENDERED_STAGE1_TABLES_AND_PATH_ANNOTATED_STORED_RECORD}
````

