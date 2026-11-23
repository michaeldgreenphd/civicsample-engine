# Eligibility-Barrier & Structured-Discordance Parser

Deterministic, pure-Python-stdlib parser for ClinicalTrials.gov API v2
`protocolSection.eligibilityModule` records. Quantifies enrollment barriers
across 12 clinical and demographic representation domains, extracts structured
organ-function and performance-status cutoffs, distinguishes conditional clinical
allowances from blanket exclusions, and audits structured registry metadata
against free-text `eligibilityCriteria`.

Nothing in `eligibility/` calls an LLM, makes network requests, or modifies
weekly demographics artifacts. Reproducibility rests on SHA-256 rule-table
pinning (`VOCABULARY_SHA256`), exact character-offset evidence spans, and
non-collapsing reporting states.

## Module Architecture

| Module | Responsibility |
|---|---|
| `eligibility/sections.py` | Exact character-offset section & clause tokenizer (`SectionSpan`, `ClauseSpan`, `SectionedText`) |
| `eligibility/vocabulary.py` | Canonical 12-domain regex rule table (`RULES`), modifier/threshold patterns, extractors, and `VOCABULARY_SHA256` pin |
| `eligibility/parser.py` | Cross-clause polarity algebra, clinical exception resolution, `EvidenceSpan` construction, and `audit_structured_vs_text` |
| `eligibility/table.py` | Per-study `build_eligibility_row`, compact `lean_eligibility_row`, `parse_error_row` quarantine, and `summarize_cohort_barriers` |

## Hard Slice Invariant & Section Layouts (`eligibility/sections.py`)

`parse_eligibility_sections(raw_text)` scans `raw_text` directly without line-ending
or Unicode normalization so every emitted `SectionSpan` and `ClauseSpan` upholds:

```python
raw_text[span.start_char:span.end_char] == span.verbatim_text
```

across LF (`\n`), CRLF (`\r\n`), lone CR (`\r`), Unicode operators (`≥`, `≤`, `–`),
Markdown headings/emphasis, bulleted/numbered lists (`*`, `-`, `1.`, `(a)`),
multi-cohort protocols (`Part 1 Inclusion Criteria:`, `Cohort B Exclusion Criteria:`),
and negated obligation headers (`Participants must NOT have any of the following:`).

Indented sub-bullets and inline or trailing `Note:` / `Exception:` / `Unless` /
`Allowance:` qualifiers are attached to their parent criterion (`has_attached_subclause=True`,
with exact offset pairs in `attached_exception_spans`) so clinical exceptions are
never severed from the exclusion they temper, while parameter sublist stems
(`Adequate organ function as defined below:`) keep each organ bullet independent.

Document-level `section_layout` is classified into 5 non-collapsing states:

| `section_layout` | Meaning |
|---|---|
| `both_sections` | Both Inclusion and Exclusion headers detected |
| `inclusion_only` | Only Inclusion header(s) detected |
| `exclusion_only` | Only Exclusion header(s) detected |
| `unheaded_text` | Substantive text with no recognizable Inclusion/Exclusion header (`section_kind = "unheaded"`, never silently assumed to be Inclusion or Exclusion) |
| `empty_or_placeholder` | Missing (`None`), empty, whitespace, `"N/A"`, `"No eligibility criteria"`, `"Please contact site for information"`, or `"See protocol"` |

## Pinned Rule Vocabulary (`eligibility/vocabulary.py`)

`compute_vocabulary_sha256()` serializes `DOMAINS`, `RULES`, `MODIFIER_PATTERNS`,
`THRESHOLD_PATTERNS`, and `VOCABULARY_VERSION` into canonical sorted-key JSON and
verifies the digest against `EXPECTED_VOCABULARY_SHA256` via `verify_vocabulary_pin()`.
Every `DomainRule` requires a unique `rule_id`, canonical domain and effect,
explicit `applicable_sections`, valid regular expressions, and a non-empty
`rationale`.

### The 12 Representation & Clinical Domains

| Domain | Blanket Exclusion (`strictly_excluded`) | Threshold / Conditional (`threshold_or_conditional`) | Targeted (`explicitly_included_or_targeted`) | Key Extracted Thresholds / Flags |
|---|---|---|---|---|
| `pregnancy_or_lactation` | Exclusion of pregnant/breastfeeding/nursing women or required negative pregnancy test | Contraception-only agreement (`contraception_required`) or explicit cohort/lactation allowance (`exception_permitted`) | Maternal/obstetric or lactation trials requiring pregnancy or active breastfeeding | `contraception_mentioned`, `condition_type` |
| `hiv_infection` | Blanket HIV/AIDS exclusion or required HIV-negative serology | Controlled HIV on stable ART, undetectable viral load, or CD4 cutoff (`exception_permitted`) | HIV treatment/cure trials positively requiring HIV infection | `cd4_min_cells_ul`, `requires_undetectable_viral_load` |
| `hepatitis_b_or_c` | Blanket HBV/HCV exclusion or required negative HBsAg/anti-HCV | Cured/cleared HCV (RNA negative) or suppressed HBV DNA on antiviral therapy (`exception_permitted`) | Chronic HBV or HCV trials requiring active viral hepatitis | `hcv_rna_negative_allowed`, `hbv_dna_suppressed_allowed` |
| `renal_function` | Qualitative renal insufficiency/CKD/dialysis ban with no GFR/CrCl threshold | Quantitative eGFR, CrCl, or serum creatinine ULN / mg/dL cutoff (`numeric_cutoff`) | ESRD, dialysis, Stage 4/5 CKD, or impaired Inclusion eGFR/CrCl (`< 60 mL/min`) | `egfr_min_ml_min`, `crcl_min_ml_min`, `creatinine_max_uln`, `creatinine_max_mg_dl` |
| `hepatic_function` | Qualitative liver disease or hepatic failure exclusion with no lab threshold | AST/ALT/bilirubin ULN or Child-Pugh A (`numeric_cutoff`), or **Gilbert's syndrome** bilirubin allowance (`exception_permitted`) | Decompensated cirrhosis, Child-Pugh B/C, or hepatic-impairment PK trials | `ast_max_uln`, `alt_max_uln`, `bilirubin_max_uln`, `has_gilberts_exception`, `gilberts_bilirubin_max_uln` |
| `cardiac_function` | Blanket CHF, arrhythmia, MI, unstable angina, or NYHA I-IV exclusion | Quantitative LVEF %, QTc/QTcF ms, or NYHA Class III/IV functional cutoff (`numeric_cutoff`) | Heart-failure trials requiring HFrEF/HFpEF, NYHA II-IV, or reduced LVEF (`<= 40%`) | `lvef_min_pct`, `qtc_max_ms`, `nyha_max_allowed` |
| `cns_or_brain_metastases` | Blanket brain, CNS, or leptomeningeal metastases exclusion | Treated, radiographically stable, or asymptomatic brain metastases permitted (`exception_permitted`) | Neuro-oncology trials requiring measurable or untreated CNS/brain metastases | `condition_type` |
| `performance_status` | — (scored scales map to quantitative caps) | ECOG/Zubrod score cap (`0`, `0-1`, `0-2`, or inverted Exclusion `>= 2` -> max `1`), Karnofsky, or Lansky cutoff (`numeric_cutoff`) | Frail/poor-PS trials restricted to `ECOG >= 2` or `KPS <= 60%` | `ecog_max_allowed`, `karnofsky_min_pct`, `lansky_min_pct` |
| `psychiatric_or_substance_use` | Psychiatric illness limiting compliance, active suicidality, or alcohol/substance abuse | Controlled depression/anxiety on stable therapy or sustained remission allowance (`exception_permitted`) | Psychiatry and addiction trials requiring DSM/ICD psychiatric or substance-use disorders | `condition_type` |
| `prior_malignancy` | Blanket exclusion of any prior or concurrent second malignancy with no window or exception | Finite lookback window (`within N years` -> `numeric_cutoff`) or non-melanoma skin cancer / carcinoma in situ allowance (`exception_permitted`) | Therapy-related neoplasm or multiple-primary survivorship trials | `lookback_years`, `has_skin_or_insitu_exception` |
| `language_or_literacy` | Monolingual fluency requirement (e.g. English only) or exclusion for inability to read English / illiteracy | Multilingual option (`English or Spanish`) or interpreter / translated-consent accommodation (`exception_permitted`) | Limited English Proficiency (`LEP`) or low-health-literacy trials | `languages`, `accommodation` |
| `free_text_age_cutoff` | — | Deterministic chronological participant age bounds (`[min_age_years, max_age_years]`, `numeric_cutoff`) | — | `min_age_years`, `max_age_years`, `min_inclusive`, `max_inclusive` |

### Key Analytical Safeguards

1. **Contraception vs Pregnancy Exclusion**: Contraception requirements for participants of childbearing potential (`preg_contraception_only`) resolve to `threshold_or_conditional` (`exception_permitted`, `condition_type="contraception_required"`, carried into `lean_eligibility_row` via `has_contraception_requirement`) when standalone, and **never** override an explicit pregnancy/lactation exclusion (`strictly_excluded`).
2. **Negated-Inclusion Polarity Inversion & Treatment-Naive Guards**: Clauses or relative/comma sub-segments inside `Inclusion Criteria` carrying an explicit negation stem (`No history of ...`, `, with no history of ...`, `who must not have ...`, `Not pregnant ...`) are evaluated under exclusion polarity so coordinated or compound clauses (such as `Adults >= 18 years with NSCLC, with no history of HIV or HCV`) resolve targeted disease requirements and negated barriers independently, while treatment-naive criteria (`No prior radiation/resection/antiviral/pharmacotherapy for ...`) keep positive disease-targeting polarity.
3. **Chronological Age Polarity Inversion, Match-Local Negation, & Cohort Grouping**: `extract_clause_age_bounds` evaluates negation locally per age span rather than clause-wide, inverts Exclusion bounds (`Exclusion: Age < 18 years or > 70 years` -> `[18.0, 70.0]`), scopes cohort-headed exclusions (`Part 1 Exclusion: Age > 65`) to their respective cohort before computing the study-wide union envelope, and rejects non-chronological lookbacks (`within the past 5 years`, `20 pack-years`), milestone ages (`symptom onset at age <= 40`), third-party ages (`partner aged >= 18`, `caregiver aged`, `donor aged`), and perinatal measures (`gestational age >= 37 weeks`).
4. **Gilbert's Syndrome Allowance vs Exclusion**: `extract_hepatic_thresholds` sets `has_gilberts_exception = True` only on genuine allowance cues (`except in`, `unless due to`, `permitted for Gilbert's`) and suppresses it when Gilbert's syndrome itself is negated or excluded (`excluding Gilbert's syndrome`, `Gilbert's is not permitted`).
5. **Unheaded Polarity Conservatism**: Clauses in `unheaded_text` carry `section_kind = "unheaded"`. Bare noun phrases without explicit obligation/exclusion predicates resolve to `ambiguous_or_unheaded`.

## Domain Status Levels & Error Quarantine (`eligibility/parser.py`, `eligibility/table.py`)

Every domain resolves to one of 5 mutually exclusive, non-collapsing states:

- `strictly_excluded`
- `threshold_or_conditional` (always accompanied by `substatus` in `{"numeric_cutoff", "exception_permitted"}`)
- `explicitly_included_or_targeted`
- `ambiguous_or_unheaded`
- `not_mentioned`

When `build_eligibility_row(study, snapshot_date=None)` encounters a malformed
record or unexpected exception (for example, on fictional test study `NCT99000301`),
it returns `parse_error_row(...)` with `eligibility_parse_status = "parse_error"`,
`section_layout = None`, and `None` across domain statuses and discordance booleans —
never dropping the trial and never collapsing `parse_error` into `not_mentioned` or `False`.

## Structured-vs-Text Discordance Auditor (`audit_structured_vs_text`)

Compares `protocolSection.eligibilityModule` structured fields (`minimumAge`,
`maximumAge`, `stdAges`, `sex`, `healthyVolunteers`) against deterministic
free-text constraints:

- `hidden_upper_age_cap`: `True` when structured `maximumAge` is absent/`"N/A"` yet free text imposes a chronological upper age bound (`text_max_age_years`).
- `hidden_lower_age_bound`: `True` when structured `minimumAge` is absent/`"N/A"` yet free text imposes a positive chronological lower age bound (`text_min_age_years`).
- `conflicting_age_bounds`: `True` when structured `minimumAge` or `maximumAge` differs by `> 1.0` year (`AGE_CONFLICT_TOLERANCE_YEARS`) from free-text age bounds.
- `std_ages_discordance`: `True` when `stdAges` lists `OLDER_ADULT` while free text excludes age `>= 65`, or lists `CHILD` while free text requires age `>= 18`.
- `sex_eligibility_discordance`: `True` when structured `sex` (`ALL`, `FEMALE`, `MALE`) conflicts with free-text participant sex restrictions (while ignoring conditional reproductive/contraception clauses such as `"Women of childbearing potential must use contraception"`).
- `healthy_volunteers_discordance`: `True` when structured `healthyVolunteers` conflicts with explicit free-text healthy-volunteer inclusion or exclusion.

## Verification

Run the offline unit suite from the repository root:

```bash
python -m pytest tests/eligibility -q
```
