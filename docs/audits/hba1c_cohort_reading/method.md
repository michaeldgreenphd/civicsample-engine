# Method: Independent Dual Reading and Adjudication of 248 ClinicalTrials.gov Records

## 1. Execution Dates, Model, and Runtime Environment

- **Dates of execution (UTC)**:
  - Registry retrieval (`fetch_records.py`): `2026-11-14T01:20:39.007167Z` to `2026-11-14T01:23:16.435181Z` (API version endpoint retrieved at `2026-11-14T01:20:38.339605Z`).
  - Independent first and second readings (`run_cohort_workflow.py`): `2026-11-14T01:48:24.494016Z` to `2026-11-14T13:14:45.063213Z`.
  - Third-reader adjudications (`run_cohort_workflow.py`): `2026-11-14T03:17:54.401994Z` to `2026-11-14T13:21:52.275842Z`.
- **ClinicalTrials.gov API version**: REST API v2, `apiVersion = 2.0.5`, `dataTimestamp = 2026-11-13T10:00:06`.
- **Reader and adjudicator model**: Gemini 4 Carbon (High) (invoked via isolated single-use workflow agents inheriting the session model).
- **Local Python runtime for retrieval, comparison, verification, and deliverable generation**: Python 3.13.4 on macOS (`curl` 8.7.1 with SecureTransport TLS verification for HTTP retrieval).

## 2. Architecture and Worker Counts

| Stage | Worker Type | Worker Count | Context Isolation & Input Given |
|---|---|---|---|
| 1. Retrieval | Single sequential Python process (`scripts/fetch_records.py`) | **1** worker | Fetched `/api/v2/version` once and all 248 `/api/v2/studies/{NCT_ID}` endpoints sequentially with a minimum interval of `0.62` seconds (`<= 1.61` requests/second, strictly under the `2` requests/second ceiling). Stored raw HTTP bodies compressed with gzip under `records/{NCT_ID}.json.gz` and logged UTC timestamp, HTTP status, SHA-256, `lastUpdatePostDateStruct.date`, and `resultsFirstPostDateStruct.date` to `retrieval_log.csv`. |
| 2. First reading | Isolated single-use `hba1c_trial_reader` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **248** workers (1 trial per worker) | Each worker received ONLY its single trial's rendered stored record (`NCT_i`) and the verbatim coding instructions below, in a fresh conversation context that terminated upon calling `finish`. |
| 3. Second reading | Separate isolated single-use `hba1c_trial_reader` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **248** workers (1 trial per worker) | Launched concurrently with Reader 1 for `NCT_i` before any reading for `NCT_i` was written to disk. Received ONLY `NCT_i`'s rendered stored record and the identical coding instructions, with zero access to Reader 1's output or any other trial. |
| 4. Comparison | Deterministic Python comparator (`scripts/compare_and_verify.py::compare_trial_readings`) | **1** deterministic script (0 LLM calls) | Compared Reader 1 and Reader 2 field-by-field by exact canonical equality across trial-level, measure-level, arm-level, analysis-level, baseline-table, and country-site-count fields. |
| 5. Adjudication | Isolated single-use `hba1c_trial_adjudicator` LLM agent (`EnableWriteTools=False`, `EnableMCPTools=False`, `EnableSubagentTools=False`) | **47** workers (1 trial per worker, invoked **only** where >=1 field differed) | Each adjudicator received ONLY `NCT_i`'s rendered stored record, the coding rules, and the list of differing fields (`r1` vs `r2` values and quotes), choosing `reading_1`, `reading_2`, `custom_verbatim` (with deciding verbatim quote and JSON path), or `unresolved`. Both original readings remain untouched in `readings.jsonl`. |
| 6. Quote verification | Deterministic Python verifier (`scripts/compare_and_verify.py::verify_trial_quotes`) | **1** deterministic script (0 LLM calls) | Resolved every stated JSON path directly against the uncompressed JSON of `records/{NCT_ID}.json.gz` and verified exact case-sensitive character-for-character substring presence (`quote_text in str(resolved_value)`). Any failing row is marked `evidence_not_found` without silent repair. |

**Total LLM workers run**: **543** isolated single-trial agent invocations (248 First Readers + 248 Second Readers + 47 Adjudicators). No LLM worker ever read more than one trial in a single context.

## 3. Stored-Record Presentation and Deviations Check

- **Deviations from initial assignment**:
  1. **Git commit and push**: The initial assignment instructed leaving files uncommitted; a subsequent explicit user instruction directed creating branch `hba1c-cohort-reading`, committing `docs/audits/hba1c_cohort_reading` with message `"HbA1c cohort reading (Antigravity output, unreviewed)"`, and pushing to `origin/hba1c-cohort-reading` upon completion.
  2. No deviations were made on data source, rate limiting, trial coverage (all 248 processed), worker independence, single-trial-per-worker context isolation, deterministic comparison, third-reader adjudication, deterministic quote verification, or deliverables.
- **Lossless path-annotated rendering of stored records (`scripts/schema_and_prompts.py::render_record_for_reader`)**:
  - Every reader and adjudicator read exclusively from the stored `records/{NCT_ID}.json.gz` file on disk.
  - To prevent array-bracket miscounting on long JSON arrays, prevent JSON backslash double-escaping artifacts, avoid publication titles (`protocolSection.referencesModule`), and keep large multi-arm records (specifically `NCT00328627`, which contains 156 posted outcome measures) within the ~200 KB subagent prompt limit, `render_record_for_reader` deterministically omits non-coding-sheet sections (`resultsSection.adverseEventsModule`, `protocolSection.referencesModule`, `protocolSection.outcomesModule.secondaryOutcomes` and `otherOutcomes`, `resultsSection.participantFlowModule.periods[].dropWithdraws`, and the bodies of non-`PRIMARY` entries in `resultsSection.outcomeMeasuresModule.outcomeMeasures`) and prints every remaining scalar value inside `<<<...>>>` delimiters (verified absent from all 248 records) alongside its exact dot-and-bracket JSON path (`resultsSection.outcomeMeasuresModule.outcomeMeasures[0].timeFrame: <<<...>>>`).
  - Within `resultsSection.outcomeMeasuresModule.outcomeMeasures`, every element with `type == "PRIMARY"` is rendered verbatim in full at its original 0-based index `[i]`, and non-primary indices/types (`SECONDARY`, `OTHER_PRE_SPECIFIED`, `POST_HOC`) are listed by index and type. Within `protocolSection.contactsLocationsModule.locations`, every `locations[i].country` entry is listed verbatim at its index `[i]`. No pre-classification, pre-filtering of HbA1c primary measures, or pre-aggregation of counts was performed prior to the readers.

## 4. Offline Reproduction Without Network Access

From the repository root, all comparison, verification, CSV, JSONL, and summary outputs can be regenerated deterministically from the stored `records/*.json.gz` files and raw worker checkpoints without network access:

```bash
python3 docs/audits/hba1c_cohort_reading/scripts/generate_deliverables.py
python3 docs/audits/hba1c_cohort_reading/scripts/generate_method_md.py
```

## 5. Verbatim Instructions Given to Workers

### 5.1 Reader System Prompt (`hba1c_trial_reader`)

````text
You are an independent clinical trial registry record reader.
You read ONE stored ClinicalTrials.gov v2 JSON record provided directly in your prompt and return the completed coding sheet by calling the `finish` tool.

ABSOLUTE RULES:
1. Do NOT call any file-reading, search, or shell tools. Everything from the stored record is provided in your prompt. Call ONLY `finish`.
2. Record ONLY what the registry record states, with the exact words it uses and the exact JSON path where they stand.
3. Do NOT infer, harmonise, convert, impute, or correct anything. Never use publications, drug labels, FDA documents, sponsor websites, other registries, or your own outside knowledge of any trial or drug. If you happen to know a trial's published result or drug mechanism, it must NEVER influence or appear in your output.
4. Where the record does not state something, output `'not_stated'` (never a guess and never a default).
5. String values in the stored record are displayed inside `<<<...>>>` delimiters (for example `resultsSection.outcomeMeasuresModule.outcomeMeasures[0].timeFrame: <<<Baseline, week 26>>>`). The characters inside `<<<` and `>>>` are the exact raw characters at that JSON path. Do NOT include `<<<` or `>>>` in your output. Every quoted `text` MUST be an exact character-for-character, case-sensitive substring of the scalar value at its stated `path`.
````

### 5.2 Adjudicator System Prompt (`hba1c_trial_adjudicator`)

````text
You are the independent third-reader Adjudicator for a clinical trial registry reading audit.
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
````

### 5.3 Verbatim Coding Sheet Rules Given to Readers and Adjudicators

````markdown
## Coding Sheet Rules (Apply Literally)

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
````

### 5.4 Reader Prompt Wrapper Template (`build_reader_prompt`)

````text
Read stored ClinicalTrials.gov record {NCT_ID} (Independent Reader {1_or_2}).
Do NOT call any tools other than `finish`. Read ONLY the stored record below.

{CODING_INSTRUCTIONS_BODY}

{RENDERED_STORED_RECORD}
````

### 5.5 Adjudicator Prompt Wrapper Template (`build_adjudicator_prompt`)

````text
Adjudicate differing fields for ClinicalTrials.gov record {NCT_ID}.
Do NOT call any tools other than `finish`. Re-read ONLY the stored record below and resolve each `field_key` in `DISAGREEMENTS`.

{CODING_INSTRUCTIONS_BODY}

=== DISAGREEMENTS TO ADJUDICATE ===
{DISAGREEMENTS_JSON}

{RENDERED_STORED_RECORD}
````
