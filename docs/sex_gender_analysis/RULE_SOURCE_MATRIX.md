# Sex/gender analysis layer: rule-to-source matrix

Status of this document: implementation record for the `sex_gender_analysis/1.0`
layer (`src/sex_gender_analysis.py`), written 2026-10-28. It records, for every
rule the layer implements or deliberately leaves open, where the rule comes
from, whether that source was readable in the environment the layer was
written in, and what the layer does with it. It is not a scientific decision
record: nothing here approves a rule, and a row marked *proposed* or
*unresolved* stays that way until the investigator records a dated decision.

## Sources and how they were checked

| Source | What it is | How it was checked here |
|---|---|---|
| `src/sex_gender_parser.py` (module 1.1.0, rules `parsers.R@2026-08-17 / outcomes@2026-09-10 / units@2026-09-14`) | The vendored manuscript parser | Read in full; sha256 `fc58d5b9c52f2984c764988ba3d18a454d0571e43fc09e0561848765eff0615b`. Unchanged by this layer. |
| `src/sex_gender_table.py` | The engine's wrapper (provenance, percent female, no balancing) | Read in full; sha256 `34541d9d78973270d91f9baa03de0150ee6e1ed3cf4c076eafd97b6263bd723e` before this change. |
| `tests/sex_gender/fixtures/fixture_trials.json`, `snapshot_baseline_2026-06-09.json` | The vendored parser's pinned cases and the 66,210-trial 2026-06-09 baseline | sha256 `a4a3e95759ab68db6bca638d074e187dbf04233f20ad230f5ef087c84fd2cd29`, `01d134623d0a79d97522075f5ef80aa228146d29672bf0cd379d8d7563e4f7e7`. |
| Manuscript draft, "Sex and Gender Reporting and Female Participation Relative to Disease Burden in ClinicalTrials.gov", `ctsexgender_JAMA_manuscript_20261026.docx` (Drive file id `14uTwSucRvGyJW_n_EuxF8Q-ev-MT65H1`, modified 2026-10-26T20:38:37Z) | The newer analysis as written for submission: Methods (Outcomes), Results, Table 1 | Read as text through the Drive connector. No byte digest could be computed in this environment (text export only). |
| Supplement 1, `ctsexgender_JAMA_supp1_20261026.docx` (Drive file id `1m8BOnhjVGtxCVFXRTYBjdehnKgx0IbfY`, modified 2026-10-26T20:51:13Z) | eMethods 1 to 4, eTable 1 (reporting states), eTable 5 (gender identity categories) | Read as text through the Drive connector. No byte digest. |
| Claude Science Stage 1 package, private branch `claude-science/stage1-design-20261028` at `010da69c12b3b87e11102c25a57441670a8f69f6`, `protocols/claude-science-20261028b/` | The condition-verification experiment design (not the sex/gender specification) | All 18 entries of its `SHA256SUMS.txt` verified byte for byte. It changes nothing in this layer. |
| `Revision_20261013/INVESTIGATOR_DECISIONS_20261021.md` (E7, E11 and later decisions) | Investigator decisions the handoff cites | **Not accessible**: the research root is a OneDrive folder on the investigator's machine; no copy is in Drive or any reachable repository. Rules citing it are recorded from the handoff's description only and marked unverified. |
| `carly_replication_20261025/` (`README_CARLY_v2.md`, `replication_carly_v2.Rmd`, `R/parsers.R`, the APPROVED Rmd, `inputs/reporting_states_trial_level_v5_2.csv`, `reporting_rule_records_v5_2.csv`, `unresolved_cases_for_investigator_v5_2.csv`) | The replication package the newer reporting layer runs in | **Not accessible** as files. Drive holds `carly_replication_code_20261025.zip` (file id `1-Dt9C4WvR-YjHhU3Xe6de6Uj-X7dWFhm`, 12.6 MB, modified 2026-10-25T12:07:36Z); the connector returns base64 only and direct download is refused by the environment's proxy, so its contents could not be read. The handoff states `R/parsers.R` there is byte-identical to the vendored parser's source snapshot (sha256 `294a8dd2…3000c`); that claim could not be rechecked here. |
| `analysis/primary_v5_2/scripts/30_reporting_states_v5_2.py`, `A20_gender_identity_categories_v5_2.py` and its sidecar report; `analysis/ppr_gates/scripts/04_gender_category_reconciliation.R` and memo; `analysis/primary_v5_5_pain_modeled_20261025/README.md`, `code/00_config_v5_5.R`; `reconciliation_20261027/REMAINING_DECISIONS_20261027.md` | The as-run derivation scripts and the open-decision list | **Not accessible** (same reason). |
| `CLAUDE_SCIENCE_RECONCILIATION_UPDATE_20261027.md`, `claude-science-handoff-20261026.md` (Drive, same folder as the manuscript) | Coordination notes for the condition-verification work | Read. They concern the clinician review and AI experiments, not the sex/gender rules. |

Where a row below says *handoff only*, the only evidence available here was the
integration handoff's description of the source. Such a row is implemented, if
at all, as an explicitly unevaluated quantity.

## Vintage and cohort

The manuscript's primary cohort is 58,007 interventional, non-terminated trials
with results first posted 2009-01-01 to 2026-04-30, linked to AACT (eTable 2);
its mixed-sex cohort is 50,400. The site's 2026-10-25 dataset holds 80,614
records with posted results since 2009 and no terminated-trial exclusion. The
two are not interchangeable: no manuscript total is an expected total for a
registry run, and no test in this repository asserts one.

## Rules

Columns: id; what the rule says; source (file, section); approval status as
far as it could be established; layer (parser = vendored and untouched,
wrapper = `sex_gender_table.py`, analysis = this layer); API v2 fields read;
fixture; whether it generalizes to new inputs.

### Measure selection and the parser states (unchanged)

| Id | Rule | Source | Status | Layer | API fields | Fixture | Generalizes |
|---|---|---|---|---|---|---|---|
| R-SEL-01 | A baseline measure is a sex/gender candidate iff its title matches `/sex\|gender/i`. | parser `select_sex_gender_measures`; verified on the 2026-06-09 extract | Approved (manuscript parser) | parser | `resultsSection.baselineCharacteristicsModule.measures[].title` | vendored suite | yes |
| R-ST-01 | Five states: reported, explicit_unknown_only, uninformative, not_reported, parse_error; never collapsed. | parser `classify_reporting`; AGENTS.md invariant | Approved | parser | measures, classes, categories, measurements | vendored suite; `tests/sex_gender_analysis/fixtures/cases.json` | yes |
| R-UNK-01 | `n_unknown` is only what the vocabulary mapped to Unknown; the enrollment gap is `enrollment_minus_parsed`, never a status input. | wrapper; `tests/sex_gender/test_table.py` | Approved | wrapper | `designModule.enrollmentInfo.count` | existing | yes |
| R-CO-01 | Composition and percent female use `reported_sex AND is_participant_count AND female + male > 0`. | wrapper `percent_female` (README D5); manuscript eMethods 2 ("less 47 counting non-participant units") | Approved | wrapper | `paramType`, `unitOfMeasure` | existing; cases NCT99000001, NCT99000007 | yes |

### The newer reporting layer (implemented here as the as-run layer)

| Id | Rule | Source | Status | Layer | API fields | Fixture | Generalizes |
|---|---|---|---|---|---|---|---|
| R-RD-01 | Reporting dimension: sex only, both, gender only, neither. Reported sex = Female and Male counts under any measure label; reported gender = a gender-diverse or cisgender/transgender category (a Gender-titled Female/Male or Woman/Man table is sex). | Manuscript Methods, Outcomes; Supp 1 eTable 1 panel A | Approved in the submission draft; mapping from the parser's `reported_sex`/`reported_gender` is an implementation choice | analysis `reporting_dimension` | as R-ST-01 | NCT99000001 (sex only), NCT99000010 (both), NCT99000013 (gender only), NCT99000004 (neither), NCT99000015 (Gender-titled F/M = sex only) | yes |
| R-NR-01 | Neither, sub-state "participants recorded only as unknown or not reported, or sex stated as not collected". | Supp 1 eMethods 2, eTable 1 | Approved definition; the mapping (parser `explicit_unknown_only`, and `uninformative` with `declared_not_collected`) is an implementation choice | analysis | measurements, `populationDescription`, `description`, measurement comments | NCT99000002, NCT99000006 | yes |
| R-NR-02 | Neither, sub-state "measure absent". | eTable 1 | Approved; equals parser `not_reported` | analysis | measures | NCT99000004 | yes |
| R-NR-03 | Neither, sub-state "measure present without a usable breakdown" (aggregate or all-zero counts). | eTable 1 | Approved; equals parser `uninformative` without a not-collected note | analysis | measurements | NCT99000005 | yes |
| R-NR-04 | Parse failure is an operational failure, never one of the substantive states. | AGENTS.md invariant; this layer | Implemented | analysis | n/a | NCT99000014 | yes |
| R-EU-01 | An explicit Unknown count can coexist with reported values; both are kept and the coexistence is flagged. Coexistence is judged per measure (a non-zero Unknown row and a non-zero value row in the same count-driving table); two tables, one with values and one with only Unknown, do not qualify, which the row-level counts alone cannot tell. | eTable 1 panel B, "Values and an explicit unknown in the same measure" | Approved | analysis `explicit_unknown_with_values` | measurements | NCT99000003; `test_explicit_unknown_with_values_is_per_measure` | yes |
| R-IC-01 | Incomplete composition: Female and Male counts covering only some participants (partial arm-level or cluster counts) carry a flag and are not used as a distribution. | Supp 1 eMethods 2 | Definition approved; **no general rule for deriving it from the API is approved**; the manuscript applies it to reconciled borderline records | analysis: carried as `incomplete_composition: null`, `incomplete_composition_evaluated: false`; a historical false outside the evaluated set is never advertised as complete composition | would need `baselineCharacteristicsModule.groups`, `denoms`, per-group `measurements[].groupId` (the raw envelope keeps them) | NCT99000008 | **decided 2026-10-28** (DECISIONS_2026-10-28.md): not evaluated in this version; no longer listed in `unresolved` |
| R-PO-01 | Patients-only rule (E7): exclude a clinician or provider group from the participant count where the evidence proves the population and the excluded group. | `INVESTIGATOR_DECISIONS_20261021.md` E7, **handoff only**; the handoff states four named historical cases are validated | Approved on named cases only; no general rule | analysis: `patients_only.status = not_evaluated`; the parser's count-driving choice is preserved and never rewritten | `populationDescription`, group titles | NCT99000009 | **decided 2026-10-28**: no general patients-only rule; `patients_only` stays not_evaluated; no longer listed in `unresolved` |
| R-FS-01 | FINAL reporting states (Table 1, eTable 1 panel B) include manual reconciliation of borderline records; the models use as-run states. U01-U03 and U08-U11 remained undecided in the inspected v5.2 file; Table 1's footnote counts "provisional readings of 7 unresolved cases (15 trials)". | Manuscript Table 1 footnote (read); `replication_carly_v2.Rmd` Reporting states and `unresolved_cases_for_investigator_v5_2.csv` (**not accessible**) | Open | analysis: `layer = as_run`, `R-FS-01` on every block | n/a | NCT99000015 | **decided 2026-10-28**: stay as-run, labelled; no FINAL overlay; no longer listed in `unresolved` |

### Gender-identity categories (eTable 5)

| Id | Rule | Source | Status | Layer | API fields | Fixture | Generalizes |
|---|---|---|---|---|---|---|---|
| R-GC-01 | D3: transgender (with or without a sex term), non-binary or gender-expansive, questioning, combined transgender or gender-diverse group, another or self-described gender (bare "Other" included). | Supp 1 eTable 5; manuscript Results | Implementation choice open to revision (E11, **handoff only**); category definitions as published in the draft | analysis `derive_gender_categories` | category and class titles, measurements | NCT99000010, NCT99000013 | yes, as a proposal |
| R-GC-02 | D5: the strict lower bound, D3 without "another or self-described". | eTable 5; manuscript Results ("strict lower bound 9,490 participants in 360 trials") | As R-GC-01 | analysis `d5_strict` | same | NCT99000010 | yes, as a proposal |
| R-GC-03 | Intersex (a sex characteristic), cisgender-labelled and explicit unknown/declined are separate rows, never D3. | eTable 5 | Approved in the draft | analysis `separate` | same | NCT99000013, NCT99000010 | yes |
| R-GC-04 | Exclusions: counts that are not participant counts (3 trials in the draft), judged on the parser's primary table and on every table that posts an identity category (a COUNT_OF_UNITS gender table beside a participant-count sex table excludes the trial, with the table named); category sum exceeding enrollment by more than 10% (11 trials). | eTable 5 footnote | Approved in the draft; **which sum** the footnote means is not stated: this layer compares `d3_total` with enrollment (an implementation choice, recorded on the block) | analysis `exclusion` | `paramType`, `unitOfMeasure` per table, enrollment | NCT99000011, NCT99000012; `test_gender_table_in_non_participant_units_excludes_the_trial` | **decided 2026-10-28**: the D3 total, as implemented |
| R-GC-05 | Each category counted once per trial at its largest value across the count-driving baseline tables (an aggregate operational rule, not person-level deduplication). The label vocabulary of each category. | eTable 5 footnote (max rule, read); `A20_gender_identity_categories_v5_2.py` and its sidecar report (**not accessible**) | Max rule approved in the draft; the vocabulary here is reconstructed from the category descriptions and the parser's bucket vocabulary, **parity with A20 unverified** | analysis | same | NCT99000010; `test_detailed_gender_category_vocabulary` | **open**: still listed on every derived or excluded block; **held off the site** until parity is shown (decided 2026-10-28) |
| R-GC-06 | Two-question tables (the same participants under sex and under gender) and possible double counting across tables are flagged, not excluded. The 14 excluded trials and 21 two-question tables of the draft are findings on one extract, not a universal list. | eTable 5 footnote | Flags only | analysis `flags` | same | NCT99000010 | yes |
| R-GC-07 | A transgender label with a sex word stays out of Female and Male (parser: cis/trans-qualified) and enters `transgender_with_sex_term`. | parser `canon_bucket` order; eTable 5 ("of which the label also names a sex") | Approved | parser + analysis | same | NCT99000010, NCT99000013 | yes |
| R-GC-08 | The old `n_gender_diverse` / `n_ambiguous_gender` buckets are not a validated gender-diverse population count; they stay as the parser's buckets beside D3/D5. | handoff; E11 (**handoff only**) | Recorded | wrapper + analysis | n/a | NCT99000010 | yes |

### Inputs and provenance

| Id | Rule | Source | Status | Layer | API fields | Fixture | Generalizes |
|---|---|---|---|---|---|---|---|
| R-EL-01 | Eligibility (sex, genderBased, genderDescription) is kept verbatim in its own namespace and is never observed participant sex or gender identity; absence is kept as absence. | API v2 `eligibilityModule`; handoff gap 1 | Implemented (`src/utils.extract_eligibility`) | extraction | `protocolSection.eligibilityModule.sex`, `.genderBased`, `.genderDescription` | `test_orchestrator_stores_the_lean_block_and_eligibility_namespace` | yes |
| R-ENV-01 | A versioned raw input envelope retains the selected measures plus the baseline module's groups, denominators, population description, measure paths, eligibility and run stamps; the existing raw file is unchanged; an archive without an envelope cannot support a rule that needs the context. | handoff gap 2 | Implemented as `select_raw_envelope` and `src.extract_all --raw-envelope`; **weekly workflow wiring is the owner's cutover decision** (schema `docs/sex_gender_analysis/schema/sex_gender_raw_envelope-1.0.schema.json`) | extraction | `baselineCharacteristicsModule.groups`, `.denoms`, `.populationDescription`, measures | `test_raw_envelope_keeps_the_context_and_reparses_to_the_same_row` | yes |
| R-PROV-01 | Every block carries its schema and rules versions and the parser's; the sidecar meta carries `source_extracted_at`, `source_pipeline_commit`, `snapshot_date`; a join across artifacts of different runs is refused. | AGENTS.md ("provenance stamps are load-bearing") | Implemented | analysis, builder | n/a | builder tests | yes |
| R-COH-01 | No manuscript total is hard-coded as an expected total for a current registry run. | handoff | Implemented (no such assertion exists) | tests | n/a | n/a | yes |

## What the layer does not do

- It does not change the parser, its vocabulary, order or `choose_total`; a
  change there goes to the manuscript bundle first and is re-vendored.
- It does not add enrollment gaps to Unknown, infer counts from free text,
  enrollment, eligibility or AI, or read eligibility at all.
- It does not sum everything across tables: D3 is a category-level maximum
  across count-driving tables, with the sum within one table.
- It does not decide R-IC-01, R-PO-01 or R-FS-01. Those remain unevaluated
  and are named on every block that leaves them open.
- It does not transfer the private physician-agreement rule for unresolved
  adjudications into any denominator.

## Owner cutover plan (not performed here)

1. Merge the engine change; the weekly job then writes `sex_gender_analysis`
   lean blocks into the full records and a `sexGenderAnalysis` block into the
   summary (both additive; the site's contract lists the record key as
   optional, so an older input stays readable).
2. Decide whether `scripts/build_sex_gender_analysis.py` runs in
   `extract.yml` after the parsed-table build, whether its sidecar table and
   meta are published to the site, and whether `--raw-envelope` is turned on
   and the envelope added to the weekly `data-*` release assets.
3. *(Done 2026-10-28: see `DECISIONS_2026-10-28.md`; RULES_VERSION bumped.)* Record dated decisions on R-IC-01 (a general incomplete-composition rule),
   R-PO-01 (a general patients-only rule or none), R-GC-04 (which sum the
   10% exclusion compares) and R-GC-05 (parity with the A20 derivation once
   that script is available to a build environment), then bump
   `RULES_VERSION`.
4. Only after a same-date replay of the retained full records and raw
   measures through the connected verification recipe, cut the site over to
   show the newer layer as the default; until then it is labelled and
   optional.
