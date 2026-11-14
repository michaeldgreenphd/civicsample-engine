# Refined audit: what ClinicalTrials.gov results sections contain for trials in the demographics dataset

Run 2026-11-13 (second pass, after investigator review). Read-only: nothing was committed, pushed or edited in any repository; no paid API or LLM call was made. Output directory: `docs/audits/outcomes_results_structure/second_pass/`.

**How to read this report.** Every earlier finding was treated as provisional and recomputed or re-worded. Each number carries its numerator, denominator and the name of the rule that produced it; the rules are written out in `definitions.json` and in the docstrings of `scripts/`. Percentages on the 2,000-trial sample carry Wilson 95% intervals that treat the proportional stratified sample as a simple random sample. Registry-wide counts (section 1.3) are census counts on the retrieval date and carry no interval. Judgements made by reading records are those of one reviewer, the auditing model; they are not an independent human review.

## 0. The fifteen numbers that matter most

| # | Refined finding | Value | Rule / denominator |
|---|---|---|---|
| 1 | The frame requires posted results | 80,890 records = every study with `ResultsFirstPostDate` in 2009-01-01..2026-11-08 (registry count on 2026-11-13 for that range: 80,890); `hasResults` true for 2,000/2,000 sampled | section 1.1; frame rule FR |
| 2 | Share of interventional studies with a primary completion date in 2015-2023 that have posted results (count-level, all registered studies) | 40,704/196,860 (20.7%); completed or terminated only: 39,977/146,205 (27.3%) | section 1.3; not a per-trial audit |
| 3 | Same, Phase 3 / industry lead / industry randomised Phase 3 | 6,948/17,352 (40.0%) / 17,392/43,579 (39.9%) / 4,125/6,477 (63.7%) | section 1.3 |
| 4 | The existing candidate cohort's frame query (industry, Phase 3, randomised, primary completion 2020): with results | 443/648 (68.4%) | section 1.3 and 3.5 |
| 5 | All five results modules PRESENT (formerly "99.8%") | 1,997/2,000 (99.8%; 95% CI 99.6-99.9) | presence only; section 2.1 |
| 6 | USABLE content: baseline / participant flow / primary outcome / adverse events | 1,991/2,000 (99.5%; 95% CI 99.1-99.8) / 1,995/2,000 (99.8%; 95% CI 99.4-99.9) / 1,905/2,000 (95.2%; 95% CI 94.2-96.1) / 1,932/2,000 (96.6%; 95% CI 95.7-97.3) | rules B1, F1, O1, A1; denominator 2,000 |
| 7 | All four substantive modules usable | 1,852/2,000 (92.6%; 95% CI 91.4-93.7) | B1 and F1 and O1 and A1 |
| 8 | Comparative trials | C1 1,394/2,000 (69.7%; 95% CI 67.6-71.7); C2 (randomised) 1,134/2,000 (56.7%; 95% CI 54.5-58.9) | section 2.3 |
| 9 | Among comparative randomised trials (C2): usable between-arm analysis on a primary measure | 473/1,134 (41.7%; 95% CI 38.9-44.6); with estimate and both confidence limits 310/1,134 (27.3%; 95% CI 24.8-30.0) | rule S1_between; denominator C2 |
| 10 | Same, randomised Phase 3 (C2, release phase PHASE3) | 153/250 (61.2%; 95% CI 55.0-67.0); estimate with both limits 124/250 (49.6%; 95% CI 43.5-55.8) | denominator C2 and Phase 3 |
| 11 | Race reported AND a usable between-arm estimate with confidence limits, among C2 | 206/1,134 (18.2%; 95% CI 16.0-20.5) | co-occurrence within trials with results only |
| 12 | Subgroup reporting detected by the current method | 136/2,000 (6.8%; 95% CI 5.8-8.0) | rule SG2 (text rule over 18 fields); not a prevalence |
| 13 | Of the detected, judged by one reviewer to post a subgroup result | 41/2,000 (2.0%; 95% CI 1.5-2.8): demographic 34/2,000 (1.7%; 95% CI 1.2-2.4); race or ethnicity 6/2,000; false positives 71/2,000 (3.5%; 95% CI 2.8-4.5) | section 4.3 |
| 14 | Missed cases in reviewed negatives (demographic subgroup results) | random sample 0/60 (95% upper bound 6.0%); targeted likely-reporter sample 1/40; non-demographic subgroup results missed: 1/60 and 3/40 | section 4.4 |
| 15 | Largest narrow endpoint cohorts in the full release (randomised Phase 3; of which race reported) | T2DM: HbA1c change from baseline at 24-26 weeks: 248 (131); Depression: MADRS total change, any time point: 82 (59); Rheumatoid arthritis: ACR20, any time point: 84 (40); Breast cancer: PFS: 87 (58); Lung cancer: PFS: 112 (87); Lung cancer: OS: 105 (78) | text definitions on 80,890 records; section 3.4 |

## 1. Sampling frame

### 1.1 How the 80,890 trials were selected, and whether posted results were required

**Posted results were required. The frame is every study that has a results-first-posted date on or after 2009-01-01; nothing else is filtered.**

The selection is one registry query, built in the engine at the release's own commit (`pipeline_commit` `f6a2e7b74790c6f11982376441fb90c80d2c611e`; `src/api_client.py`, `src/extract_all.py` and `.github/workflows/extract.yml` are byte-identical between that commit and the checkout read, `0293cd099575`; only `src/utils.py` differs, in a baseline-breakdown helper that does not touch selection):

- `.github/workflows/extract.yml` line 57 runs the weekly extraction as `python -m src.extract_all --output data/demographics.json --results-after 2009-01-01 --snapshot-date "$CURRENT_DATE" --raw-envelope`.
- `src/extract_all.py` lines 342-346 pass that to the client: `studies = client.iter_all_studies_with_results(limit=args.limit, condition=args.condition, results_after=args.results_after)` (`--limit` and `--condition` are unset in the workflow).
- `src/api_client.py` lines 45-47 build the filter: `elif filters.get("results_after"):` ... `date_filter = f"AREA[ResultsFirstPostDate]RANGE[{filters['results_after']},MAX]"`; lines 55-59 send it as the only filter: `params = {"pageSize": self.page_size, "format": "json", "filter.advanced": date_filter}` to `GET https://clinicaltrials.gov/api/v2/studies` (line 66). With no date the fallback on line 53 is `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`, commented on line 52 "No date filter - just require results exist".
- `src/api_client.py` lines 92-111 (`iter_all_studies_with_results`) page through every match. There is no filter on study type, phase, status, sponsor, country, allocation or enrolment. `DEFAULT_FIELDS` (lines 11-23) is defined but not sent, so whole records are returned.
- `src/extract_all.py` lines 400-418 keep a study when `extract_demographics_from_study` returns a record and otherwise only count it (`errors += 1`); a study whose extraction raised would be absent from the release.

So the inclusion rule is: **registered on ClinicalTrials.gov, any study type, with `ResultsFirstPostDate` between 2009-01-01 and the pull date (2026-11-08), and not dropped by an extraction error.** Checks made:

- Registry count for `AREA[ResultsFirstPostDate]RANGE[2009-01-01,2026-11-08]` on 2026-11-13: **80,890**, equal to the release's 80,890 records (all NCT IDs unique). The count for the range ending 2026-11-06, the latest `results_date` in the release, is also 80,890. No record was lost to extraction errors unless an equal number entered and left the range in five days.
- The release's `results_date` runs from 2009-01-06 to 2026-11-06 with none missing. Excluded by the date floor: 41 studies with results first posted before 2009. Since the pull, the same query has grown to 80,988.
- `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]` and `AREA[HasResults]true` return the same count (81,029 and 81,029): in this registry a results-first-posted date and `hasResults` are the same condition.
- Composition of the frame: INTERVENTIONAL 76,153, OBSERVATIONAL 4,737; status COMPLETED 67,475, TERMINATED 12,030, ACTIVE_NOT_RECRUITING 1,251, other 134; `enrollment_type` ACTUAL 80,877, ESTIMATED 9, N/A 4. The registry holds 611,771 studies (466,861 interventional, 142,843 observational), so the frame is 13.2% of all registered studies.

**Consequence, stated as the investigator put it:** the frame contains only trials with results, so this audit cannot estimate whether demographic reporting and results posting occur together across all otherwise eligible trials. Every sample-based figure below is conditional on results having been posted. Section 1.3 gives the only evidence gathered here about the wider population, and it is count-level.

### 1.2 The sample (saved; seed and identifiers carried forward)

- Source population: `engine-data/demographics.json.gz`, sha256 `2168d258dd26d1c824f4a090b07df7f1dc8ad58d71a6a15598e5115c258c7fa2`, `extracted_at` 2026-11-08T13:07:45.318831+00:00, `pipeline_commit` `f6a2e7b74790c6f11982376441fb90c80d2c611e`, 80,890 records.
- Method: strata = the record's `study_type`; 2,000 allocated proportionally by largest remainder (INTERVENTIONAL 1,883, OBSERVATIONAL 117); NCT IDs sorted ascending within stratum; one `random.Random(20261113)` generator, strata visited in name order, `rng.sample(sorted_ids, n_h)`.
- **Seed 20261113.** The 2,000 NCT IDs are in `sample_ids.txt` (sorted; sha256 `9bd0c403097b055faf01b505fbd0b98a6ca21611d965c8878751f11f1e0d525f`), parameters in `sampling.json`. `scripts/verify_sample.py` re-drew the sample from the release in this pass and reproduced the file exactly (`sampling_verification.json`: match = True).
- Records analysed: the first audit's cache (`outcomes-audit/cache/raw_responses.jsonl.gz`, sha256 `a9e4577735b5f3ece14ff672a5db0f4ca70be283f237bb996c09328c3569510a`), 40 responses fetched 2026-11-13T22:32:29Z to 2026-11-13T22:33:01Z, API version 2.0.5, registry `dataTimestamp` 2026-11-13T10:00:06; fields `NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule`. Retrieved 2,000 of 2,000; 0 missing; 0 cache lines failed to parse.

### 1.3 Do eligible trials post results? (count-level, on a frame that does not require results)

This is the first of the three questions the investigator asked to keep apart. It cannot be answered from the release. It was measured on the registry itself with count queries: `GET https://clinicaltrials.gov/api/v2/studies?countTotal=true&pageSize=1&fields=NCTId&filter.advanced=<expression>`, two per cell.

- Year: `AREA[PrimaryCompletionDate]RANGE[Y-01-01,Y-12-31]` for Y = 2015..2023.
- With results: the same expression `AND AREA[ResultsFirstPostDate]RANGE[MIN,MAX]` (the engine's own condition). Without results = the difference.
- Strata (each ANDed with the year):
  - `interventional_all`: `AREA[StudyType]INTERVENTIONAL`
  - `interventional_completed_or_terminated`: `AREA[StudyType]INTERVENTIONAL AND AREA[OverallStatus](COMPLETED OR TERMINATED)`
  - `interventional_phase3`: `AREA[StudyType]INTERVENTIONAL AND AREA[Phase]PHASE3`
  - `interventional_industry_lead`: `AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY`
  - `interventional_phase3_industry_randomized`: `AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY AND AREA[Phase]PHASE3 AND AREA[DesignAllocation]RANDOMIZED`
  - `interventional_fda_regulated_drug_or_device`: `AREA[StudyType]INTERVENTIONAL AND (AREA[IsFDARegulatedDrug]true OR AREA[IsFDARegulatedDevice]true)`
  - `interventional_us_location`: `AREA[StudyType]INTERVENTIONAL AND AREA[LocationCountry]"United States"`
  - `observational_all`: `AREA[StudyType]OBSERVATIONAL`
- Retrieved 2026-11-13T23:19:50Z to 2026-11-13T23:21:54Z; API version 2.0.5, `dataTimestamp` 2026-11-13T10:00:06. 144 count requests, 0 failures.
- Consistency: for the all-interventional row the split was asked for three other ways (`NOT AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`, `aggFilters=results:with` / `results:without`, `AREA[HasResults]true`); all agree with the table in all 9 years. The release's own interventional records by primary-completion year are 2015: 4,242, 2016: 4,541, 2017: 4,299, 2018: 4,513, 2019: 4,706, 2020: 4,324, 2021: 4,424, 2022: 4,854, 2023: 4,780, within 9 of the registry's with-results counts in every year (the release is five days older).

Studies with results / all studies (%), by primary-completion year:

| Year | Interventional, all | Interventional, completed or terminated | Phase 3 (includes Phase 2/3) | Industry lead | Industry, Phase 3, randomised | FDA-regulated drug or device (flag) | Any US location | Observational, all |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2015 | 4,242/16,393 (25.9) | 4,218/13,709 (30.8) | 864/1,977 (43.7) | 1,945/4,265 (45.6) | 508/733 (69.3) | 366/503 (72.8) | 2,936/5,918 (49.6) | 267/4,396 (6.1) |
| 2016 | 4,542/17,982 (25.3) | 4,521/14,697 (30.8) | 811/1,887 (43.0) | 2,012/4,571 (44.0) | 505/758 (66.6) | 711/976 (72.8) | 3,247/6,540 (49.6) | 321/5,025 (6.4) |
| 2017 | 4,299/19,076 (22.5) | 4,273/15,133 (28.2) | 804/1,905 (42.2) | 1,969/4,428 (44.5) | 476/702 (67.8) | 1,149/1,789 (64.2) | 3,176/6,444 (49.3) | 300/5,475 (5.5) |
| 2018 | 4,516/20,435 (22.1) | 4,473/15,962 (28.0) | 795/1,847 (43.0) | 2,006/4,476 (44.8) | 481/692 (69.5) | 1,964/3,140 (62.5) | 3,488/7,078 (49.3) | 273/6,108 (4.5) |
| 2019 | 4,706/22,197 (21.2) | 4,640/17,001 (27.3) | 772/1,773 (43.5) | 2,011/4,701 (42.8) | 457/670 (68.2) | 2,585/4,273 (60.5) | 3,731/7,676 (48.6) | 306/7,161 (4.3) |
| 2020 | 4,326/21,688 (19.9) | 4,241/15,617 (27.2) | 735/1,886 (39.0) | 1,791/4,362 (41.1) | 443/648 (68.4) | 2,620/4,403 (59.5) | 3,545/7,226 (49.1) | 268/8,518 (3.1) |
| 2021 | 4,427/23,455 (18.9) | 4,332/16,563 (26.2) | 716/1,920 (37.3) | 1,839/4,989 (36.9) | 413/678 (60.9) | 2,841/5,000 (56.8) | 3,631/7,312 (49.7) | 246/8,945 (2.8) |
| 2022 | 4,857/26,867 (18.1) | 4,710/18,456 (25.5) | 732/2,059 (35.6) | 1,959/5,757 (34.0) | 424/771 (55.0) | 3,155/5,707 (55.3) | 4,040/8,303 (48.7) | 273/9,600 (2.8) |
| 2023 | 4,789/28,767 (16.6) | 4,569/19,067 (24.0) | 719/2,098 (34.3) | 1,860/6,030 (30.8) | 418/825 (50.7) | 3,048/5,859 (52.0) | 3,962/8,386 (47.2) | 293/9,888 (3.0) |
| **2015-2023** | **40,704/196,860 (20.7)** | **39,977/146,205 (27.3)** | **6,948/17,352 (40.0)** | **17,392/43,579 (39.9)** | **4,125/6,477 (63.7)** | **18,439/31,650 (58.3)** | **31,756/64,883 (48.9)** | **2,547/65,116 (3.9)** |

What this does and does not show:

- Among all registered interventional studies with a primary completion date in 2015-2023, 40,704/196,860 (20.7%) have posted results on ClinicalTrials.gov. The dataset therefore describes about one in five such studies. The share varies by stratum: 40.0% of Phase 3, 39.9% of industry-led, 63.7% of industry randomised Phase 3, 48.9% with a US site; observational studies 3.9%.
- It is a count-level answer, not a per-trial audit: no record was read. "Eligible" here means "matches the query"; it is not a determination that a study is an applicable clinical trial under FDAAA, that its deadline has passed, or that no extension or certified delay applies. A study without results on ClinicalTrials.gov may have reported elsewhere (a journal, EU CTR).
- Primary completion dates include anticipated dates on records never updated; the completed-or-terminated row removes most of those. Later years have had less time to post, and the decline from 2021 onward mixes that with any real change.
- The FDA-regulated row uses flags that exist only on records registered or updated since January 2017 (documentation knowledge, not checked here; the small 2015-2016 counts are consistent with it); in 2015-2016 it covers a small, selected subset (503 and 976 studies) and should not be compared with later years.
- These counts say nothing about demographic reporting among studies without results: those studies have no baseline table at all on the registry.

### 1.4 Among trials with results: are demographics and usable outcomes reported? (sample, n = 2,000)

Second question. Denominator: the 2,000 sampled trials, all with posted results. Demographic flags are the release's own `race.reported`, `ethnicity.reported`, `sex.reported`; "usable primary outcome" is rule O1 (section 2.2).

| | n / denominator |
|---|---|
| Race reported | 1,155/2,000 (57.8%; 95% CI 55.6-59.9) |
| Ethnicity reported | 819/2,000 (41.0%; 95% CI 38.8-43.1) |
| Sex reported | 1,962/2,000 (98.1%; 95% CI 97.4-98.6) |
| Usable primary outcome (O1) | 1,905/2,000 (95.2%; 95% CI 94.2-96.1) |
| Race reported and usable primary outcome | 1,110/2,000 (55.5%; 95% CI 53.3-57.7) |
| Race reported and all four substantive modules usable | 1,082/2,000 (54.1%; 95% CI 51.9-56.3) |
| Usable primary outcome among race-reported trials | 1,110/1,155 (96.1%; 95% CI 94.8-97.1) |
| Usable primary outcome among trials without race reported | 795/845 (94.1%; 95% CI 92.3-95.5) |

In the full release race is reported for 46,564/80,890 (57.6%). Within trials that have results, a usable primary outcome is about equally common whether or not race is reported; that is a within-frame description and carries no implication for trials without results.

### 1.5 Among appropriate comparative trials: are statistical analyses reported?

Third question. It is answered in section 2.3, on the comparative denominators C1 to C3 rather than on all 2,000.

## 2. Module presence and usable content

### 2.1 Module presence (the former "99.8%" finding, relabelled)

"Present" means only that the key exists under `resultsSection` and is a non-empty object. It is not a statement about completeness. Denominator 2,000.

| Module | Present |
|---|---|
| `participantFlowModule` | 2,000/2,000 (100.0%; 95% CI 99.8-100.0) |
| `baselineCharacteristicsModule` | 2,000/2,000 (100.0%; 95% CI 99.8-100.0) |
| `outcomeMeasuresModule` | 2,000/2,000 (100.0%; 95% CI 99.8-100.0) |
| `adverseEventsModule` | 1,997/2,000 (99.8%; 95% CI 99.6-99.9) |
| The four substantive modules together | 1,997/2,000 (99.8%; 95% CI 99.6-99.9) |
| `moreInfoModule` (administrative; see below) | 2,000/2,000 (100.0%; 95% CI 99.8-100.0) |
| All five keys (the earlier headline) | 1,997/2,000 (99.8%; 95% CI 99.6-99.9) |

**The "more information" module is not a substantive results module.** It is present in every record because it carries the results point of contact (`pointOfContact`: 2,000/2,000 (100.0%; 95% CI 99.8-100.0)) and the agreement statement (`certainAgreement`: 2,000/2,000 (100.0%; 95% CI 99.8-100.0)). Its one substantive free-text field, `limitationsAndCaveats.description`, is present in 439/2,000 (21.9%; 95% CI 20.2-23.8). It is excluded from every usable-content count below.

### 2.2 Usable content, one rule per module

"Numeric" = the registry string parses as a finite number; "NA", blank and absent never count and are never read as zero. Full rule text: docstring of `scripts/usable.py` and `definitions.json`. Per-trial flags: `per_trial_usable.jsonl`.

| Rule | Plain-language definition | Usable (of 2,000) | Not usable, by reason |
|---|---|---|---|
| **B1** baseline | at least one baseline measure with a numeric value and a numeric denominator > 0 for the Total group, or for every arm | 1,991/2,000 (99.5%; 95% CI 99.1-99.8) | no_numeric_values: 8; values_or_denominators_incomplete: 1 |
| **F1** participant flow | one period with numeric STARTED and COMPLETED counts for every flow group, STARTED > 0 in at least one | 1,995/2,000 (99.8%; 95% CI 99.4-99.9) | counts_missing_for_a_group_or_all_zero: 5 |
| **O1** primary outcome | a PRIMARY measure with a unit, a numeric number analysed for every group (> 0 in at least one) and a numeric value for every analysed group | 1,905/2,000 (95.2%; 95% CI 94.2-96.1) | no_data_table: 82; no_numeric_value_for_an_analysed_group: 8; zero_analysed_in_every_group: 3; no_data_table+no_numeric_value_for_an_analysed_group: 1; number_analysed_missing_for_a_group: 1 |
| **A1** adverse events | numeric serious and other numbers at risk for every event group (> 0 in at least one), plus event rows or explicit zero totals | 1,932/2,000 (96.6%; 95% CI 95.7-97.3) | zero_at_risk_in_every_group: 60; numbers_at_risk_missing_for_a_group: 5; module_absent: 3 |
| **S1** statistical analysis | an analysis on a PRIMARY measure naming its groups, with a p-value or an estimate with both confidence limits | 612/2,000 (30.6%; 95% CI 28.6-32.7) | see 2.3: the right denominator is comparative trials |
| B1 and F1 and O1 and A1 | all four substantive modules usable | 1,852/2,000 (92.6%; 95% CI 91.4-93.7) | |

Notes on each rule:

- **Baseline.** Usable for the Total group in 1,991/2,000 (99.5%; 95% CI 99.1-99.8) and for every arm in 1,955/2,000 (97.8%; 95% CI 97.0-98.3). The 9 failures are records whose baseline table is a template with category titles and zero participants (for example NCT00387673, NCT00712270, NCT02978885). Total group found as: titled "Total" 1,378; single group 618; last group equal to the sum of the others under another title 4.
- **Participant flow.** The 5 failures all post STARTED = 0 in every group (records closed without data, e.g. NCT00387673, NCT01016561).
- **Primary outcome.** Stricter variants: number analysed > 0 in every group, 1,872/2,000 (93.6%; 95% CI 92.4-94.6); every PRIMARY measure of the trial usable, 1,845/2,000 (92.2%; 95% CI 91.0-93.3). At measure level, of 4,408 PRIMARY measures 4,170 pass; the rest: no_data_table: 195; no_numeric_value_for_an_analysed_group: 34; zero_analysed_in_every_group: 5; number_analysed_missing_for_a_group: 2; no_groups: 1; no_unit: 1.
- **Adverse events.** This is where presence and usability differ most: the module is present in 1,997 records but in 60 of them every group has zero participants at risk (adverse events not collected or not reported; the record says so in free text, e.g. NCT05537116, NCT06290206), and 5 older records lack a number at risk. Of the usable records, 457 report no event rows and explicit zeros for every group. The all-cause mortality table (numeric `deathsNumAtRisk` for every group) is present in 1,267/2,000 (63.4%; 95% CI 61.2-65.4); it was not required by A1 because it did not exist before 2017.
- By study type: interventional B1 1,874/1,883 (99.5%; 95% CI 99.1-99.7), F1 1,878/1,883 (99.7%; 95% CI 99.4-99.9), O1 1,791/1,883 (95.1%; 95% CI 94.0-96.0), A1 1,834/1,883 (97.4%; 95% CI 96.6-98.0); observational (n = 117) B1 117/117 (100.0%; 95% CI 96.8-100.0), O1 114/117 (97.4%; 95% CI 92.7-99.1), A1 98/117 (83.8%; 95% CI 76.0-89.4).
- These rules test whether a table can be read, not whether its content is correct, complete against the protocol, or analysed appropriately.

### 2.3 Comparative trials and statistical analyses

"Comparative" is its own denominator. How it was determined (structure of the record, not a judgement of design quality):

- **C1** = `protocolSection.designModule.studyType == "INTERVENTIONAL"` and at least one PRIMARY outcome measure whose `groups[]` has 2 or more entries: **1,394/2,000 (69.7%; 95% CI 67.6-71.7)**. (Interventional: 1,883/2,000 (94.2%; 95% CI 93.0-95.1); allocation among them: RANDOMIZED 1155, NA 513, NON_RANDOMIZED 212, None 3. 54 observational studies also have a 2+ group primary measure and are not in C1.)
- **C2** = C1 and `designInfo.allocation == "RANDOMIZED"`: **1,134/2,000 (56.7%; 95% CI 54.5-58.9)** (1,134/1,155 (98.2%; 95% CI 97.2-98.8) of randomised interventional trials).
- **C3** = C2 and rule O1 passed on a PRIMARY measure that itself has 2+ groups: **1,088/2,000 (54.4%; 95% CI 52.2-56.6)**.
- Two groups on a measure are not necessarily two randomised arms (dose cohorts, periods and disease cohorts also appear as groups); C2 is the denominator closest to "appropriate comparative trials".

| Denominator | n | any analysis on a primary | S1 (named groups + p-value or estimate with both limits) | S1_between (2+ groups) | between-arm p-value | between-arm estimate with both limits |
|---|---:|---|---|---|---|---|
| All sampled | 2,000 | 621/2,000 (31.1%; 95% CI 29.1-33.1) | 612/2,000 (30.6%; 95% CI 28.6-32.7) | 520/2,000 (26.0%; 95% CI 24.1-28.0) | 436/2,000 (21.8%; 95% CI 20.0-23.7) | 332/2,000 (16.6%; 95% CI 15.0-18.3) |
| C1 comparative | 1,394 | 543/1,394 (39.0%; 95% CI 36.4-41.5) | 536/1,394 (38.5%; 95% CI 35.9-41.0) | 509/1,394 (36.5%; 95% CI 34.0-39.1) | 426/1,394 (30.6%; 95% CI 28.2-33.0) | 327/1,394 (23.5%; 95% CI 21.3-25.8) |
| C2 randomised comparative | 1,134 | 496/1,134 (43.7%; 95% CI 40.9-46.6) | 491/1,134 (43.3%; 95% CI 40.4-46.2) | 473/1,134 (41.7%; 95% CI 38.9-44.6) | 398/1,134 (35.1%; 95% CI 32.4-37.9) | 310/1,134 (27.3%; 95% CI 24.8-30.0) |
| C3 randomised, usable 2+ group primary | 1,088 | 494/1,088 (45.4%; 95% CI 42.5-48.4) | 489/1,088 (44.9%; 95% CI 42.0-47.9) | 471/1,088 (43.3%; 95% CI 40.4-46.3) | 396/1,088 (36.4%; 95% CI 33.6-39.3) | 308/1,088 (28.3%; 95% CI 25.7-31.1) |
| C2 and Phase 3 | 250 | 158/250 (63.2%; 95% CI 57.1-68.9) | 156/250 (62.4%; 95% CI 56.3-68.2) | 153/250 (61.2%; 95% CI 55.0-67.0) | 129/250 (51.6%; 95% CI 45.4-57.7) | 124/250 (49.6%; 95% CI 43.5-55.8) |
| C2, Phase 3, industry | 194 | 142/194 (73.2%; 95% CI 66.6-78.9) | 141/194 (72.7%; 95% CI 66.0-78.5) | 138/194 (71.1%; 95% CI 64.4-77.1) | 116/194 (59.8%; 95% CI 52.8-66.4) | 116/194 (59.8%; 95% CI 52.8-66.4) |
| Not C1 | 606 | 78/606 (12.9%; 95% CI 10.4-15.8) | 76/606 (12.5%; 95% CI 10.1-15.4) | 11/606 (1.8%; 95% CI 1.0-3.2) | 10/606 (1.7%; 95% CI 0.9-3.0) | 5/606 (0.8%; 95% CI 0.4-1.9) |

So among randomised comparative trials with results, 473/1,134 (41.7%; 95% CI 38.9-44.6) post a usable between-arm analysis on a primary measure and 310/1,134 (27.3%; 95% CI 24.8-30.0) post a between-arm estimate with both confidence limits; in randomised Phase 3 the figures are 153/250 (61.2%; 95% CI 55.0-67.0) and 124/250 (49.6%; 95% CI 43.5-55.8). Absence of a posted analysis is not absence of an analysis: the registry does not require one for every measure, and the publication or CSR may hold it.

Race reporting and analyses together, within C2: race reported 648/1,134 (57.1%; 95% CI 54.2-60.0); race reported and S1_between 296/1,134 (26.1%; 95% CI 23.6-28.7); race reported and a between-arm estimate with both limits 206/1,134 (18.2%; 95% CI 16.0-20.5). In C2 Phase 3 industry trials: 82/194 (42.3%; 95% CI 35.5-49.3). These are co-occurrence counts in one record; they are not outcomes by race.

## 3. Endpoint comparability

### 3.1 Exact-title uniqueness is a lexical finding

On the full release, the first registered primary outcome title (`primary_endpoint`), lower-cased with punctuation collapsed, takes 67,634 distinct values across 80,890 trials; 63,388 (93.7% of distinct titles; 78.4% of trials) are used by one trial only. The first audit's sample figure (4,026 of 4,097 distinct titles used once) says the same thing.

This is a statement about strings. Registrants write the time point, population, instrument version and study part into the title, so two trials measuring the same construct almost never share a title. **It does not establish scientific incompatibility, and it does not show that oncology is the only viable area**: the most repeated titles are oncology titles ("progression free survival pfs" 511, "overall survival" 261) because oncology endpoints are conventionally titled without a time point, not because other areas lack common constructs. Sections 3.2 to 3.4 test that directly.

### 3.2 Candidate endpoint families on the full release (80,890)

A family = an endpoint regex on `primary_endpoint` AND a condition gate. Python `re`, case-insensitive except inside `(?-i:...)`. The gate is applied to `primary_condition` or to the `"; "`-joined `conditions` list. `primary_endpoint` is the first registered primary outcome only (`src/utils.py` lines 478-480), so a family endpoint that is a trial's second primary, or a secondary, is not counted; the counts are lower bounds on use of the construct and upper bounds on a usable cohort.

| Family | Endpoint regex | Gate | Matched | Gated | Randomised Phase 3 | Race reported | Both |
|---|---|---|---:|---:|---:|---:|---:|
| overall_survival | `\boverall survival\b\|(?-i:\bOS\b)` | `primary_condition` ~ `^Oncology$` | 840 | 793 | 419 | 514 | 290 |
| progression_free_survival | `\bprogression[- ]free survival\b\|(?-i:\bPFS\b)` | `primary_condition` ~ `^Oncology$` | 2,106 | 2,026 | 538 | 1,289 | 388 |
| objective_response_rate | `\b(?:objective\|overall\|best overall) (?:tumou?r )?response\b\|(?-i:\bORR\b)` | `primary_condition` ~ `^Oncology$` | 2,245 | 2,061 | 56 | 1,425 | 46 |
| hba1c_change | `\bhb\s?a1c\b\|\ba1c\b\|\bglyc(?:osyl)?ated ha?emoglobin\b` | `conditions` ~ `diabet` | 1,043 | 1,021 | 507 | 571 | 288 |
| ldl_cholesterol | `(?-i:\bLDL\b)\|\blow[- ]density lipoprotein\b` | `conditions` ~ `cholesterol\|lipid\|lipoprotein` | 280 | 237 | 117 | 124 | 64 |
| systolic_blood_pressure | `\bsystolic\b\|(?-i:\bSBP\b)\|\bblood pressure\b` | `conditions` ~ `hypertens` | 686 | 446 | 91 | 204 | 39 |
| fev1 | `\bFEV\s?-?1\b\|\bforced expiratory volume\b` | `conditions` ~ `asthma\|copd\|chronic obstructive\|cystic fibrosis` | 623 | 586 | 256 | 310 | 140 |
| depression_scale | `\bHAM-?D\b\|\bHDRS\b\|\bhamilton\b[^.;]{0,30}\bdepression\b\|\bMADRS\b\|\bmontgomery[- ][aå]sberg\b\|\bPHQ-?9\b\|\bpatient health questionnaire\b` | `conditions` ~ `depress` | 623 | 564 | 126 | 347 | 82 |
| pain_intensity | `(?=.*\bpain\b)(?:.*\b(?:intensity\|VAS\|visual analog(?:ue)? scale\|NRS\|NPRS\|numeric(?:al)? (?:pain )?rating scale\|pain scores?)\b)` | `primary_condition` ~ `^(?:Pain\|Musculoskeletal)$` | 1,605 | 987 | 204 | 457 | 91 |
| acr20 | `\bACR\s?-?20\b\|\bamerican college of rheumatology\b[^.;]{0,25}\b20\b` | `conditions` ~ `arthritis` | 222 | 221 | 122 | 97 | 63 |
| pasi75 | `\bPASI\s?-?75\b\|\bpsoriasis area (?:and\|&) severity index\b[^.;]{0,60}\b75\b\|\b75\s?%[^.;]{0,60}\bPASI\b` | `conditions` ~ `psoria` | 110 | 110 | 48 | 64 | 24 |
| svr_or_viral_load | `\bsustained vir(?:ologic(?:al)?\|al) response\b\|(?-i:\bSVR\s?\d*\b)\|\bviral load\b\|\b(?:HIV\|HCV)(?:-1)? RNA\b\|\bHBV DNA\b\|\bvirologic(?:al)? (?:response\|suppression\|failure\|success)\b` | `conditions` ~ `\bhiv\b\|human immunodeficiency\|hepatitis\|\bhcv\b\|\bhbv\b` | 801 | 746 | 192 | 375 | 108 |
| svr_hcv | `\bsustained vir(?:ologic(?:al)?\|al) response\b\|(?-i:\bSVR\s?\d*\b)` | `conditions` ~ `hepatitis c\|\bhcv\b` | 405 | 387 | 102 | 167 | 36 |
| hiv_rna | `\bHIV(?:-1)? RNA\b\|\bviral load\b\|\bvirologic(?:al)? (?:response\|suppression\|failure\|success)\b` | `conditions` ~ `\bhiv\b\|human immunodeficiency` | 695 | 282 | 77 | 180 | 64 |
| body_weight_change | `\bbody weight\b\|\bweight (?:loss\|change\|reduction)\b\|\bchange in weight\b\|\b(?:change\|reduction\|loss)\b[^.;]{0,25}\bweight\b` | `conditions` ~ `obes\|overweight` | 494 | 307 | 54 | 209 | 41 |
| seroconversion_or_titre | `\bsero(?:conver\|protect\|respon)\w*\|\bantibody tit(?:er\|re)s?\b\|\bgeometric mean (?:tit(?:er\|re)\|concentration)s?\b\|(?-i:\bGM[TC]s?\b)\|\bha?emagglutination[- ]inhibit\w*\|\bimmunogenicity\b\|\bneutrali[sz]ing antibod\w*` | none | 818 | 818 | 313 | 387 | 163 |

Matched = endpoint regex alone; Gated = matched and gate; Randomised Phase 3 = gated and `allocation == "RANDOMIZED"` and `phase == "PHASE3"` (Phase 2/3 excluded, as in `outcomes/evidence.py`); Race reported = gated and `race.reported`; Both = randomised Phase 3 and race reported. `svr_hcv` and `hiv_rna` are sub-families of `svr_or_viral_load`. Families overlap (a title naming PFS and OS is in both). Together the 14 families (sub-families aside) cover 10,912/80,890 trials (13.5%) and 3,041/10,852 (28.0%) of randomised Phase 3 trials in the release. Within a family the titles are still nearly all distinct (for example 291 distinct titles among 507 randomised Phase 3 HbA1c trials), which is the lexical finding again.

### 3.3 What actually varies inside six families

Six families were examined: the four largest by "Both" with one shared construct (PFS, OS, HbA1c, FEV1), plus two smaller families chosen because the construct is defined by a standard (ACR20) or by a small set of instruments (depression scales). `seroconversion_or_titre` is large but spans unrelated antigens and assays and was not examined; objective response is mostly single-arm (56 randomised Phase 3 of 2,061).

Examined set per family: the family's randomised Phase 3 members in the 2,000 sample, plus, where the sample held fewer than 15, a draw of 40 more from the family's randomised Phase 3 members outside the sample (`random.Random(20261113).sample(sorted(candidates), 40)`), fetched from the API on 2026-11-13 (`cache/family_studies.jsonl.gz`; 200 requested, 200 returned; the HbA1c family already had 15 in the sample, so none were drawn for it). For each trial the family measure is the first posted PRIMARY measure whose title matches the family regex; all attributes are read from that one measure by the rules in `scripts/family_detail.py`. The attribute classes are regex-based and were spot-read, not validated; per-trial rows are in `family_detail_rows.jsonl`.

| | PFS (oncology) | OS (oncology) | HbA1c (diabetes) | FEV1 (asthma/COPD/CF) | ACR20 (arthritis) | Depression scales |
|---|---|---|---|---|---|---|
| **Randomised Phase 3 in release** | 538 | 419 | 507 | 256 | 122 | 126 |
| **Examined (sample + drawn)** | 50 (10 + 40) | 52 (12 + 40) | 15 (15 + 0) | 42 (2 + 40) | 44 (4 + 40) | 43 (3 + 40) |
| **With a matching posted primary measure / with data** | 50 / 46 | 52 / 49 | 15 / 15 | 42 / 42 | 44 / 44 | 43 / 43 |
| **Condition** | breast 12; lung 11; lymphoma/leukaemia 6; ovarian 5; myeloma 4; (+6 more values) | lung 9; other / unspecified 6; pancreatic 6; gastric/oesophageal 5; lymphoma/leukaemia 5; (+10 more values) | type 2 diabetes 12; type 1 diabetes 2; other / unspecified 1 | COPD 24; asthma 13; cystic fibrosis 5 | rheumatoid arthritis 26; psoriatic arthritis 18 | major depressive disorder 33; bipolar depression 5; treatment-resistant depression 4; other / unspecified 1 |
| **Construct** | independent/central review 23; assessor not stated 20; investigator 7 | death from any cause 34; death (cause wording absent) 18 | change from baseline 15 | trough / pre-dose 24; FEV1 (timing not stated) 8; peak / post-dose 5; AUC / serial 5 | ACR20 responder proportion 44 | change from baseline in total score 39; response/remission proportion 2; time to relapse/event 1; score / other 1 |
| **Instrument / criteria** | RECIST 1.1 20; none stated 16; RECIST 1.0/unspecified 8; IMWG (myeloma) 3; lymphoma (Cheson/Lugano/IWG) 2; (+1 more values) | n/a 52 | laboratory HbA1c 15 | absolute volume 35; % predicted 7 | ACR20 (ACR 1995 core set) 44 | MADRS 33; HAM-D 17 6; HAM-D (version not stated) 2; HAM-D 24 2 |
| **Unit (`unitOfMeasure`, normalised)** | 7 distinct strings: months 39; (none) 3; days 2; month 2; (+3 more values) | 8 distinct strings: months 36; percentage of participants 6; (none) 3; month 2; (+4 more values) | 9 distinct strings: percentage of glycosylated hemoglobin 3; percentage 3; percent 2; percentage of glycosylated haemoglobin 2; (+5 more values) | 14 distinct strings: liters 17; litres 4; liter 4; ml 3; (+10 more values) | 6 distinct strings: percentage of participants 29; participants 10; percentage of responders 2; percentage 1; (+2 more values) | 12 distinct strings: units on a scale 17; score on a scale 11; scores on a scale 5; participants 2; (+8 more values) |
| **Summary statistic (`paramType`)** | MEDIAN 42; NUMBER 3; (none) 3; MEAN 1; (+1 more values) | MEDIAN 41; NUMBER 7; (none) 3; COUNT_OF_PARTICIPANTS 1 | LEAST_SQUARES_MEAN 11; MEAN 4 | LEAST_SQUARES_MEAN 29; MEAN 11; GEOMETRIC_MEAN 1; NUMBER 1 | NUMBER 41; COUNT_OF_PARTICIPANTS 3 | LEAST_SQUARES_MEAN 26; MEAN 13; NUMBER 3; COUNT_OF_PARTICIPANTS 1 |
| **Time point** | event-driven/variable 45; fixed 5; fixed points: 5 distinct, min 26, median 96, max 208 weeks; commonest 208 wk (1), 143 wk (1), 96 wk (1) | event-driven/variable 41; fixed 11; fixed points: 10 distinct, min 4, median 156, max 416 weeks; commonest 156 wk (2), 78 wk (1), 416 wk (1) | fixed 14; event-driven/variable 1; fixed points: 4 distinct, min 24, median 26, max 54 weeks; commonest 24 wk (6), 26 wk (6), 54 wk (1) | fixed 27; event-driven/variable 15; fixed points: 10 distinct, min 4.1, median 12.1, max 52 weeks; commonest 12 wk (10), 24 wk (7), 6 wk (2) | fixed 39; event-driven/variable 5; fixed points: 7 distinct, min 12, median 24, max 48 weeks; commonest 24 wk (18), 12 wk (8), 16 wk (7) | fixed 37; event-driven/variable 6; fixed points: 12 distinct, min 0.4, median 8, max 24 weeks; commonest 8 wk (12), 6 wk (9), 14 wk (3) |
| **Analysis population (tags in `populationDescription`; a text can carry several)** | ITT 23; all randomized 21; FAS 10; other wording 6; not stated 3; (+1 more values) | all randomized 19; ITT 11; other wording 10; not stated 9; FAS 9; (+3 more values) | FAS 8; all randomized 8; safety/treated 6; evaluable/completers 2; ITT 1; (+1 more values) | all randomized 24; safety/treated 18; FAS 14; ITT 14; evaluable/completers 9; (+4 more values) | FAS 22; all randomized 17; ITT 16; safety/treated 11; other wording 2; (+3 more values) | all randomized 18; FAS 16; safety/treated 16; ITT 10; not stated 6; (+4 more values) |
| **Effect measure of between-arm analyses** | hazard ratio 38; (no estimate type) 2; no between-arm analysis posted: 10 | hazard ratio 28; (no estimate type) 1; no between-arm analysis posted: 23 | mean difference 6; LS mean difference 5; other: treatment contrast 1; difference (unspecified) 1; no between-arm analysis posted: 2 | mean difference 18; LS mean difference 9; ratio of means 2; (no estimate type) 1; no between-arm analysis posted: 13 | risk difference 11; difference (unspecified) 10; (no estimate type) 9; odds ratio 6; mean difference 2; no between-arm analysis posted: 7 | LS mean difference 18; mean difference 15; difference (unspecified) 3; (no estimate type) 2; odds ratio 1; (+1 more values); no between-arm analysis posted: 5 |
| **Between-arm estimate with both confidence limits** | 38/50 (76.0%) | 28/52 (53.8%) | 12/15 (80.0%) | 27/42 (64.3%) | 29/44 (65.9%) | 36/43 (83.7%) |
| **Control** | active-controlled 30; placebo/sham-controlled 11; experimental arms only / not typed 6; placebo/sham-controlled + active comparator 2; (+1 more values) | active-controlled 30; experimental arms only / not typed 10; placebo/sham-controlled 7; no-intervention control 3; (+1 more values) | placebo/sham-controlled 6; active-controlled 5; placebo/sham-controlled + active comparator 2; experimental arms only / not typed 2 | placebo/sham-controlled + active comparator 12; placebo/sham-controlled 12; experimental arms only / not typed 10; active-controlled 8 | placebo/sham-controlled 26; placebo/sham-controlled + active comparator 7; active-controlled 6; experimental arms only / not typed 5 | placebo/sham-controlled 29; active-controlled 7; placebo/sham-controlled + active comparator 4; experimental arms only / not typed 3 |
| **Distinct drug/biological intervention names** | 96 | 116 | 35 | 88 | 55 | 61 |
| **Race reported / and estimate with limits** | 38/50 (76.0%) / 30/50 (60.0%) | 32/52 (61.5%) / 19/52 (36.5%) | 8/15 (53.3%) / 7/15 (46.7%) | 20/42 (47.6%) / 15/42 (35.7%) | 21/44 (47.7%) / 17/44 (38.6%) | 29/43 (67.4%) / 26/43 (60.5%) |

Reading the table, attribute by attribute:

- **Construct.** HbA1c (15/15 "change from baseline"), ACR20 (44/44 the responder proportion) and OS are one construct each. PFS splits by who assessed progression (independent review 23, investigator 7, not stated 20). FEV1 splits into trough or pre-dose (24), peak or post-dose (5), AUC or serial (5) and unstated (8). Depression is mostly change in total score (39/43) but includes responder proportions and a time-to-relapse design.
- **Instrument.** None for OS and HbA1c beyond the assay. PFS depends on tumour-specific response criteria (RECIST 1.1 20, RECIST 1.0 or unspecified 8, myeloma, lymphoma and CLL criteria 6, none stated 16). Depression is MADRS in 33/43 and HAM-D in 10 (17-item 6, 24-item 2, version not stated 2). FEV1 is absolute volume in 35 and % predicted in 7.
- **Unit.** The unit strings differ almost everywhere, but mostly as spelling ("liters", "litres", "liter", "L"; nine spellings of "percent HbA1c"). Real unit differences are fewer: months vs days vs proportion event-free at a landmark for PFS and OS; L vs mL vs % predicted for FEV1; participants vs percentage of participants for ACR20 (convertible with the denominator).
- **Time point.** Time-to-event families are event-driven (45/50 PFS, 41/52 OS), so follow-up length varies by trial and is not in the title. HbA1c concentrates at 24 and 26 weeks (12 of 14 fixed points). ACR20 at week 24 (18), 12 (8) and 16 (7). FEV1 at 12 (10) and 24 (7) weeks among 10 distinct points. Depression at 8 (12) and 6 (9) weeks among 12 distinct points from under 1 to 24 weeks.
- **Analysis population.** Named in free text in nearly every measure but rarely with one term: "all randomized", ITT, FAS, mITT and "received at least one dose" overlap and co-occur, and several records define the set as a subset with baseline and post-baseline values. The registry has no coded analysis-population field.
- **Effect measure.** Uniform where convention is strong: hazard ratio in every PFS and OS trial whose between-arm analysis names an estimate type (38 and 28 trials). Difference in means or least-squares means for HbA1c, FEV1 and depression scores, under at least three labels. ACR20 is the least uniform (risk difference 11, unspecified difference 10, odds ratio 6, no estimate type 9).
- **Is a between-arm estimate with confidence limits posted?** PFS 38/50 (76.0%); depression scales 36/43 (83.7%); HbA1c 12/15 (80.0%); ACR20 29/44 (65.9%); FEV1 27/42 (64.3%); OS 28/52 (53.8%). Where it is missing the arm-level values are usually posted, but a comparable effect would have to be computed, which for time-to-event outcomes is not possible from medians.
- **Condition and intervention.** No family is one clinical question. The PFS set spans 10 tumour groups and 96 drug names; OS 14 groups and 116 names; FEV1 mixes COPD (24), asthma (13) and cystic fibrosis (5); ACR20 mixes rheumatoid (26) and psoriatic (18) arthritis; depression mixes MDD (33), bipolar depression (5) and treatment-resistant depression (4). Placebo-controlled and active-controlled trials are mixed in every family.

### 3.4 Recommended narrow cohorts, and what would have to be harmonised

Recommendation: build cohorts as **one condition + one construct + one time window + one comparator type**, in several therapeutic areas, rather than one cohort defined by sponsor class and completion year. Sizes below are text-definition counts on the full release (`scripts/cohorts.py`; randomised Phase 3; time window parsed from `primary_outcome_time_frame`); each is an upper bound until the posted measures are checked trial by trial as in 3.3.

| Candidate cohort (randomised Phase 3, results posted) | n | race reported | ethnicity reported | industry | results posted 2017+ (race reported) |
|---|---:|---:|---:|---:|---:|
| T2DM: HbA1c change from baseline at 24-26 weeks | 248 | 131 | 78 | 247 | 98 (68) |
| T2DM: HbA1c change from baseline, any time point | 406 | 227 | 153 | 405 | 174 (132) |
| MDD (not bipolar/resistant): MADRS total change at 6-8 weeks | 31 | 22 | 19 | 29 | 13 (13) |
| Depression: MADRS total change, any time point | 82 | 59 | 43 | 80 | 44 (42) |
| Rheumatoid arthritis: ACR20 at week 24 | 35 | 15 | 8 | 35 | 14 (8) |
| Rheumatoid arthritis: ACR20, any time point | 84 | 40 | 24 | 83 | 44 (33) |
| Psoriatic arthritis: ACR20 at weeks 12-24 | 34 | 21 | 15 | 34 | 25 (20) |
| COPD: trough/pre-dose FEV1 at 12-24 weeks | 49 | 32 | 6 | 49 | 16 (12) |
| COPD: any FEV1 primary | 138 | 60 | 14 | 138 | 37 (27) |
| Asthma: any FEV1 primary | 91 | 61 | 34 | 86 | 41 (39) |
| Breast cancer: PFS | 87 | 58 | 43 | 76 | 57 (47) |
| Lung cancer: PFS | 112 | 87 | 65 | 103 | 82 (76) |
| Lung cancer: OS | 105 | 78 | 53 | 81 | 66 (58) |
| Multiple myeloma: PFS | 53 | 40 | 32 | 42 | 39 (30) |
| Oncology: PFS, all tumour types | 538 | 388 | 302 | 470 | 379 (325) |
| Oncology: OS, all tumour types | 419 | 290 | 194 | 300 | 283 (237) |

Ranked by how little harmonisation they need:

1. **Type 2 diabetes, HbA1c change from baseline at 24-26 weeks.** One construct, one assay, one unit after normalising spellings, mean or LS-mean difference with a confidence interval in 12/15 examined. To harmonise: MEAN vs LEAST_SQUARES_MEAN and SD vs SE vs CI as the posted summary; FAS vs all-randomised vs treated sets and the handling of rescue medication and missing data (stated only in free text); placebo vs active comparator and background therapy; drug class. Largest cohort, but race is reported in about half (131/248), fewer among older records.
2. **Rheumatoid arthritis ACR20 at week 24, and psoriatic arthritis ACR20 at weeks 12-24, as two cohorts.** The endpoint is a published standard and the arm-level responder counts are always posted, so a risk difference or odds ratio can be recomputed uniformly even where the posted effect measure differs. To harmonise: time point (12, 16, 24 weeks), non-responder imputation rules, background methotrexate, and the two diseases kept apart. Small (35 and 34).
3. **Major depressive disorder, MADRS total score change at 6-8 weeks, placebo-controlled.** One instrument, LS-mean difference with a confidence interval in most records. To harmonise: week 6 vs 8; MMRM vs LOCF ANCOVA; fixed vs flexible dose arms (several doses against one placebo arm); exclude bipolar and treatment-resistant populations and HAM-D trials or handle them as separate cohorts. Small (31; 82 with any time point).
4. **COPD, trough FEV1 at 12-24 weeks; asthma FEV1 as a separate cohort.** To harmonise: trough vs peak vs AUC; L vs mL; 12 vs 24 weeks; mono vs combination bronchodilators against placebo or an active component. 49 trials for the narrow COPD definition.
5. **Oncology by tumour type: PFS in lung, breast and myeloma; OS in lung.** Hazard ratios with confidence limits are posted more consistently here than any other effect measure (38/50 PFS), and race reporting is high in recent records. To harmonise: assessor (independent vs investigator), response criteria, line of therapy and biomarker-selected populations, data cut-off and follow-up length, stratified vs unstratified models, and interim vs final analyses. A hazard ratio cannot be recomputed from the posted medians, so trials without a posted estimate drop out.

What this assessment does not do: it does not show that effects are combinable across the trials in a cohort (that is a clinical and statistical judgement about interventions and populations), and it counts titles, not validated measures.

### 3.5 The existing industry Phase 3 cohort as a candidate, with its selection limitations

The existing cohort (`outcomes/README.md`, `outcomes/evidence.py` lines 15-16) is defined by the query `AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY AND AREA[Phase]PHASE3 AND AREA[DesignAllocation]RANDOMIZED AND AREA[PrimaryCompletionDate]RANGE[2020-01-01,2020-12-31]`, then eligibility rules E1-E5 (Phase 2/3 excluded; completion date wholly inside 2020): 648 frame records and 598 eligible on 2026-11-09 according to the README. It is not restricted to oncology: the README and the code contain no condition filter. If an oncology restriction is intended it is not in this repository, and that should be confirmed before the cohort is described as an oncology cohort.

Treated as one candidate among those in 3.4, its limitations are:

- **It is not selected on results, which is a strength, but the demographics link is.** Unlike the demographics release, its frame does not require results: on 2026-11-13 the frame query returned 648 studies of which 443 (68.4%) had results. Only those can be linked to posted baseline demographics or posted outcomes. In the release, 416 trials meet the same definition with `phase == "PHASE3"` exactly; race is reported for 403 of them (96.9%), ethnicity for 300.
- **One completion year.** 2020 is a single year, and an unusual one: trial conduct was disrupted by the pandemic, and the year's completions include COVID-19 vaccine and treatment trials. Nothing about other years can be read from it, and year effects cannot be separated from anything else.
- **Sponsor class is the registry's own field** (`LeadSponsorClass == INDUSTRY`), which misses industry-funded trials led by academic or network sponsors and includes small companies; Phase 2/3, non-randomised and device trials without a phase are excluded by construction.
- **It is defined by who ran the trial and when, not by what was measured.** Its 416 release members carry 362 distinct first-primary titles across 19 condition categories (Oncology 79, Other 41, Autoimmune and Inflammatory 37, Infectious Disease 31, Endocrine and Metabolic 30, Neurology 29, ...). 313 of 416 fall in none of the 14 endpoint families; the largest shared endpoints are OS (30), PFS (24) and HbA1c (14). Its oncology subset is 79 trials (PFS 24, OS 30), spread over tumour types. It supports statements about evidence availability in a defined set of trials; it does not by itself give a comparable-endpoint cohort.
- **Results state is recomputed at a cutoff** (POSTED / PENDING / UNREPORTED / NOT_APPLICABLE) from registry facts; as the README says, that is a timing fact, not a finding of non-compliance.
- **Small cells.** Any cross-classification by endpoint family and race reporting within 400-600 trials leaves cells of a few dozen or fewer.

## 4. Subgroup detection

Throughout this section the figures are **subgroup reporting detected by the current method**, followed by one reviewer's reading of what was detected. They are not an established prevalence.

### 4.1 Fields inspected

For every posted outcome measure (`resultsSection.outcomeMeasuresModule.outcomeMeasures[]`, all 15,922 measures of all types in the 2,000 trials):

| Field | How it is used | In first audit? |
|---|---|---|
| `title` | cue rule | yes (title cue) |
| `description` | cue rule | no |
| `populationDescription` | cue rule | no |
| `classes[].title` | level rule over the set of class titles; cue rule on each | yes (levels) |
| `classes[].categories[].title` | level rule over the set of category titles; cue rule on each | yes (levels) |
| `groups[].title` | level rule over the set of group titles | supplementary only |
| `groups[].description` | level rule (sex and age only) when group titles did not fire | no |
| `analyses[].groupIds` | structural: an analysis naming 2+ groups that carry different levels | no |
| `analyses[].groupDescription`, `.statisticalComment`, `.otherAnalysisDescription`, `.nonInferiorityComment`, `.pValueComment`, `.estimateComment`, `.ciLowerLimitComment`, `.ciUpperLimitComment`, `.statisticalMethod` | cue rule on each | no |
| `classes[].categories[].measurements[].comment` | cue rule | no |

Every free-text key present under an outcome measure in the sample is in this list (key inventory in the first step of the audit). Not inspected: `timeFrame`, `unitOfMeasure`, `paramType`; the baseline, participant-flow and adverse-event modules; `moreInfoModule`; protocol-section text; posted protocol, SAP or CSR documents; publications.

### 4.2 The exact rule (SG2)

A trial is detected when any measure yields any signal. Source: `scripts/subgroup_rule.py` (the regexes below are printed from that module).

**Level rule** (two or more levels of one dimension among sibling titles of one measure; applied separately to class titles, category titles and group titles):

- sex: one title matches FEMALE and not MALE, and a different title matches MALE and not FEMALE.
- race: two titles carry different RACE tokens after the excluded phrases are removed ("non-white" replaces "white" in the same title).
- ethnicity: one title matches HISPANIC and the set has 2+ titles.
- origin: 2+ titles match ORIGIN or NON_ORIGIN with at least two different tokens.
- age: two titles carry different AGE tokens: the text matched by AGE_BAND, or a single LIFE_STAGE word.
- group descriptions: the sex and age rules applied to `groups[].description` when the group titles did not fire for that dimension.
- analysis group reference: an analysis whose `groupIds` name 2+ groups that the group rules separate.

**Cue rule** (one text): KEYWORD anywhere; else BY_DEMOG unless BY_DEMOG_EXCLUDE matches within 25 characters either side. "Stratified by" followed by a non-demographic factor, "adjusted for age/sex" and covariate lists are deliberately not cues.

```
FEMALE           \b(females?|wom[ae]n|girls?)\b
MALE             \b(males?|m[ae]n|boys?)\b
RACE_EXCLUDE     white blood|white cell|white matter|white coat|white light|white noise|black box|blackout|asian flu|non-?hodgkin
RACE[white]      \b(white|caucasian)s?\b
RACE[black]      \b(black|african[- ]americans?)\b
RACE[asian]      \basians?\b
RACE[aian]       american indian|alaska native
RACE[nhpi]       native hawaiian|pacific islander
RACE[multi]      multiracial|more than one race|multiple races
RACE[nonwhite]   \bnon[- ]?(white|caucasian)\b
RACE[nonblack]   \bnon[- ]?black\b
RACE[nonasian]   \bnon[- ]?asian\b
RACE[other_race] \bother races?\b
HISPANIC         \b(hispanic|latin[oax]s?|latine)\b
ORIGIN           \b(japanese|chinese|korean|taiwanese|indian)\b
NON_ORIGIN       \bnon[- ]?(japanese|chinese|korean|taiwanese|asian)\b|\brest of (the )?world\b
AGE_BAND         (?:[<>≤≥]\s?=?\s?\d{1,3}(?:\.\d+)?\s?(?:years?|yrs?|y\.?o\.?|months?|mos?)\b)|(?:\bage[ds]?\b[^,;()]{0,25}?\d{1,3})|(?:\d{1,3}\s?(?:-|–|to|through)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)\b)|(?:\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)(?:\s?(?:and|or)\s?(?:older|younger|above|below|over|under))?)|(?:\d{1,3}\s?(?:-|–|to)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?)\b)
LIFE_STAGE[infant]     \b(infants?|neonates?|newborns?|toddlers?)\b
LIFE_STAGE[child]      \b(child|children|pediatrics?|paediatrics?)\b
LIFE_STAGE[adolescent] \b(adolescents?|teens?|teenagers?)\b
LIFE_STAGE[adult]      \b(adults?)\b
LIFE_STAGE[older]      \b(elderly|older adults?|seniors?|geriatrics?)\b
KEYWORD          \bsub-?\s?(?:groups?|populations?|sets?|cohorts?)\b|\bsubpopulations?\b|\bsubsets?\b
BY_DEMOG         \b(?:by|according to|per|stratified by|within|across|for each)\s+(?:the\s+)?(?:participant\s+|patient\s+|subject\s+)?(?:sex|gender|race|racial|ethnic(?:ity)?(?: group)?|age(?: group| category| cohort| stratum| strata| range| band)?s?)\b(?!\s*(?:of|at)\b)|\b(?:sex|gender|race|ethnic|age)[- ](?:specific|stratified|based subgroups?)\b
BY_DEMOG_EXCLUDE age[- ](?:appropriate|adjusted|matched|related|standardi[sz]ed|corrected|equivalent|at\b|of\b)|(?:adjusted|corrected|standardi[sz]ed|normali[sz]ed|matched) (?:for|by) (?:age|sex|gender)|for age\b|sex[- ]hormone|gender[- ]affirm|sex(?:ual)? (?:partner|behavio|activity|function)
(all case-insensitive)
```

### 4.3 All detected positives, reviewed

**Detected by the current method: 136/2,000 (6.8%; 95% CI 5.8-8.0).** All 136 were read (the matched text and the measure's groups and classes for each; the cached record itself for 19 ambiguous ones) and classified:

- **true subgroup result**: the record posts outcome values separately for two or more levels of a participant characteristic, under the same intervention, as classes, categories or groups.
- **partial**: something short of that: one level only; levels confounded with dose or cohort by design; subgroup sizes rather than outcomes; model-derived rather than observed values; a "by subgroup" title whose table does not identify the subgroup.
- **false positive**: the wording means something else (analysis-set definitions such as "PK subset" or "responders", instrument "subscales", laboratory "cell subsets", stratification factors of a test, eligibility ages in arm descriptions), or a subgroup analysis is announced and no data are posted.

| Judgement (one reviewer) | Trials | Of 2,000 | Of 136 detected |
|---|---:|---|---|
| True subgroup result | 41 | 41/2,000 (2.0%; 95% CI 1.5-2.8) | 41/136 (30.1%; 95% CI 23.1-38.3) |
| - demographic (sex, race, ethnicity, national origin or age) | 34 | 34/2,000 (1.7%; 95% CI 1.2-2.4) | |
| - non-demographic only (biomarker, disease type, severity) | 7 | 7/2,000 (0.3%; 95% CI 0.2-0.7) | |
| Partial | 24 | 24/2,000 (1.2%; 95% CI 0.8-1.8) | |
| False positive | 71 | 71/2,000 (3.5%; 95% CI 2.8-4.5) | 71/136 (52.2%; 95% CI 43.9-60.4) |

Among the 34 trials judged to post a demographic subgroup result (a trial can count in several): age 19, sex 14, race 6, ethnicity 2, national origin 2; race or ethnicity 6 (NCT00261443, NCT02304406, NCT03098836, NCT04323137, NCT05009251, NCT05329194). 17 have the level signal on a PRIMARY measure; for race or ethnicity that is 1 (NCT03098836, whose arms are race-defined cohorts). The release reports race for 18 of the 34.

Where the signals came from, and how they fared (a trial can appear in several rows):

| Matched field | Trials | True | of which demographic | Partial | False positive |
|---|---:|---:|---:|---:|---:|
| measure_description | 54 | 12 | 8 | 10 | 32 |
| population_description | 40 | 5 | 2 | 7 | 28 |
| measure_title | 30 | 14 | 12 | 9 | 7 |
| class_titles | 20 | 15 | 15 | 4 | 1 |
| group_titles | 17 | 14 | 14 | 3 | 0 |
| group_descriptions | 15 | 3 | 2 | 4 | 8 |
| analysis_text | 7 | 2 | 1 | 2 | 3 |
| analysis_groupIds | 6 | 4 | 3 | 1 | 1 |
| category_titles | 3 | 2 | 2 | 1 | 0 |
| class_title_cue | 2 | 0 | 0 | 1 | 1 |

- Extending beyond titles mattered: 21 of the 41 true results (16 of the 34 demographic ones) have no signal in the fields the first audit's main rule used (measure title, class titles, category titles); most were found through group titles (sex- or age-defined groups).
- It also brought most of the false positives: the KEYWORD cue alone accounts for 75 detected trials, of which 57 are false positives. "Subset" and "subpopulation" in descriptions mostly define analysis sets. Level rules on class and group titles were right far more often (class titles 15 true of 20; group titles 14 of 17).
- All 23 trials flagged by the first audit's class/category/title rule are detected by this rule. Read again in this pass, 5 of those 23 received a different label (NCT03500094 yes to partial: the age-band values are model simulations; NCT02165826 no to partial; NCT03722810, NCT03993119 and NCT04259762 partly to true). That is the same kind of reviewer on two passes, and it is a direct measure of how soft these judgements are.

**Every detected trial** (136; full matched text in `subgroup_review.csv`). Field = fields that matched; text = first matched text, truncated.

| NCT ID | Matched field(s) | Matched text | Judgement | Reason |
|---|---|---|---|---|
| NCT00078819 | analysis_text:statisticalComment | Two-sided Cochran-Mantel-Haenszel test stratified by age group | false positive | [none] stratified by age group names the stratification factor of a CMH/van Elteren test; no result is posted by age |
| NCT00113516 | population_description | ITT. DR was calculated for the subgroup of subjects with objective response. 23 subjects reported CR or PR response and  | false positive | [none] subgroup/subset of subjects defines the responder set for duration of response and a sampled subset; not a subgroup result |
| NCT00119067 | group_titles | SQ Females \| IM Female \| SQ Males \| IM Male | true subgroup result | [demographic: sex] PRIMARY local AEs posted for route x sex groups (SQ Females, IM Female, SQ Males, IM Male) |
| NCT00261443 | class_titles; measure_title | Female Participants (n=96, 86) \| Male Participants (n=70, 81) | true subgroup result | [demographic: age, sex, race] SECONDARY treatment-emergent AEs posted by age (&lt;=50/&gt;50), gender and race (White/Non-White) classes |
| NCT00264849 | measure_description | For the subgroup of patients requiring maintenance oral (systemic) corticosteroids throughout the screening period the d | partial | [non-demographic] outcome posted only for the one subgroup requiring maintenance oral steroids; no complementary level |
| NCT00348309 | measure_description | sting procedure was used to control for the two rosiglitazone dose groups and the genetic subgroups. Least square mean i | true subgroup result | [non-demographic: APOE4 genotype] PRIMARY ADAS-Cog change posted for APOE4-negative, all-except-E4/E4 and full populations, with analyses |
| NCT00388505 | measure_description; population_description | Serum tobramycin concentrations were measured in a subset of participants at Week 1 (start of cycle 1), Week 5 (End of C | false positive | [none] pharmacokinetic/audiology subpopulation are analysis sets |
| NCT00391768 | group_descriptions | Subjects aged 12 to 23 months of age confirmed to have influenza. These subjects received 30mg of Oseltamivir \| Subjects | partial | [demographic: age] PK and AEs posted per age-defined dosing cohort; age and dose are confounded by design |
| NCT00423046 | population_description | Analysis was performed by age group on subjects from the According-to-Protocol (ATP) cohort for immunogenicity who were  | true subgroup result | [demographic: age] immunogenicity posted by age group (18-26, 27-35, 36-45 years) in class titles; found through the population description |
| NCT00461305 | class_titles; measure_title | age: &lt; 30 years \| age: &gt;= 30 years | true subgroup result | [demographic: age] POST_HOC dysmenorrhea score by age (&lt;30/&gt;=30) alongside weight and diagnosis subgroups |
| NCT00464945 | group_descriptions | Participants received one single 0.5mL manufacturing scale dose of 13-valent pneumococcal conjugate vaccine (1 \| Partici | false positive | [none] ages in the arm descriptions are the infant dosing schedule; arms differ by manufacturing scale |
| NCT00477464 | measure_description | For the subset of participants who showed a complete or partial response, duration of response is defined as the time fr | false positive | [none] subset of participants who showed a response defines the duration-of-response set |
| NCT00479856 | measure_description | For the subset of participants with a confirmed CR or PR, duration of response was measured as the time from first docum | false positive | [none] subset of participants with a confirmed CR or PR defines the duration-of-response set; no data posted |
| NCT00502801 | population_description | Clinically Evaluable: Subset of the ITT population who received at least 5 days of study medication unless deemed a clin | true subgroup result | [non-demographic: pneumonia type] PRIMARY clinical response posted for all evaluable and by nosocomial / ventilator-associated / healthcare-associated pneumonia |
| NCT00537394 | measure_description | Defined among the subgroup of participants experiencing the outcome of confirmed virologic failure. Newly acquired HIV d | false positive | [none] subgroup of participants experiencing virologic failure defines who the outcome applies to |
| NCT00540592 | group_descriptions | Subjects aged ≥65 years received one dose of formulation 1 of the adjuvanted influenza vaccine GSK576389A. \| Subjects ag | partial | [demographic: age] arms are vaccine formulations in adults &gt;=65 years with a differently-aged comparison cohort; age and formulation are confounded |
| NCT00666276 | group_titles; analysis_groupIds | Linezolid-female \| Linezolid-male | true subgroup result | [demographic: sex] observational PRIMARY "factors affecting safety - gender": adverse reactions posted for male vs female groups with an analysis |
| NCT00730041 | measure_description | of excessive sleepiness on multiple activities of daily living. It has 30 questions in 5 subgroups of general productivi | false positive | [none] 5 subgroups are questionnaire domains |
| NCT00758420 | measure_description | The VVSymQ is a subset of the VEINES-Sym and consists of the 5 symptoms most relevant to patients. The raw score, which  | false positive | [none] subset of the VEINES-Sym is an instrument description |
| NCT00882999 | measure_title; measure_description; population_description | Pharmacodynamics: Change From Baseline in Peripheral Blood B Cell Subsets (Absolute Cell Counts) | false positive | [none] B cell subsets are laboratory cell populations |
| NCT00910247 | measure_description | nses were converted to 0-100 point scales. The mean of the individual item scores in each subgroup were calculated, with | false positive | [none] subgroup refers to questionnaire item groups |
| NCT00961649 | population_description | idual subject visits or data points were excluded if protocol criteria were violated at a subset of the subject's visits | false positive | [none] a subset of the subject's visits is a data-exclusion note |
| NCT00964158 | group_descriptions | Healthy male or female children between 3 to 5 years of age (3-5Y), who received 2 primary doses of GSK2340272 \| Healthy | true subgroup result | [demographic: age] immunogenicity and reactogenicity posted per age cohort (3-5, 6-9, 10-17 years), same vaccine in each; the cohorts are the study's groups |
| NCT00980005 | class_titles; measure_title; group_descriptions | A/Brisbane at Day 0 [3-4 years] \| A/Brisbane at Day 28 [3-4 years] \| A/Brisbane at Day 0 [5-8 years] \| A/Brisbane at Day | true subgroup result | [demographic: age] HI titres, seroconversion and seroprotection posted "by age-strata" (3-4, 5-8, 9-17 years) |
| NCT00997893 | measure_description | Immediate recall on the Logical Memory subset of the Wechsler Memory Scale-Revised, in which higher scores indicate a be | false positive | [none] Logical Memory subset is an instrument component |
| NCT01027351 | group_descriptions | Subjects who had received four doses of rMenB vaccine (at 2,4,6 and 12 months of age) in the parent study were \| Subject | false positive | [none] ages in arm descriptions are the vaccination schedule of the parent study |
| NCT01053663 | group_titles | Oseltamivir: Age 91 to &lt; 365 Days \| Oseltamivir: Age 31 to 90 Days \| Oseltamivir: Age 0 to 30 Days | true subgroup result | [demographic: age] PRIMARY oseltamivir PK posted per infant age band (0-30, 31-90, 91-&lt;365 days); the bands are the study's cohorts |
| NCT01060059 | class_titles | Female Gender \| Male Gender | partial | [demographic: sex] counts of patients by gender per treatment arm (factor associated with treatment choice at baseline); not an outcome by sex |
| NCT01068262 | group_titles; analysis_groupIds | Odnacatib (MK-0822) in Postmenopausal Females (Panel B) \| Placebo in Postmenopausal Females (Panel B) \| Odanacatib (MK-0 | true subgroup result | [demographic: sex] PRIMARY pharmacodynamic outcome posted for male and postmenopausal female panels, drug and placebo in each, with a between-panel analysis |
| NCT01079143 | class_titles | Donor Sex - Female \| Donor Sex - Male | false positive | [none] Donor Sex / Donor Age are donor attributes used as risk factors, not participant subgroups |
| NCT01113892 | population_description | Subset of the efficacy analysis population with available data for analysis. The efficacy analysis population includ | false positive | [none] subset of the efficacy analysis population with available data |
| NCT01183468 | group_titles | Part 1a(Aralast NP)-Subjects Aged 16-35 Yrs \| Part 1a (Aralast NP)-Subjects 8-15 Yrs \| Part 1b (Aralast NP)-Subjects Age | true subgroup result | [demographic: age] PRIMARY C-peptide AUC posted per age cohort (8-15/8-17 vs 16-35/18-35 years); small phase 1 (n=17), cohorts are the study's groups |
| NCT01256658 | class_titles; measure_description | CSC1 5- 9 years (n= 899, 431) \| CSC4 5 - 9 years (n= 900, 396) \| CSC1 10 - 14 years (n= 873, 380) \| CSC4 10 - 14 years ( | true subgroup result | [demographic: age] haemoglobin by study arm and age group (5-9, 10-14, &gt;=15 years) |
| NCT01323790 | measure_title; population_description; analysis_text:pValueComment | Response (Responder/Non-responder) to Study Drug in the LIR Subgroup During Weeks 1 to 12 | partial | [non-demographic] key secondary outcomes posted for one prespecified subgroup (laxative inadequate response); no complementary level |
| NCT01345240 | group_descriptions; analysis_groupIds | This group results from the pooling of the RTS,S Regimen A Lot 1, RTS,S Regimen A Lot 2 and RTS,S Regimen A Lo \| This gr | false positive | [none] infant age at first vaccination appears in arm descriptions; groups are regimens/lots |
| NCT01358357 | measure_description | The PANSS-P is a subset of items in the PANSS, an interview-based measure of the severity of psychopathology in adults w | false positive | [none] subset of items in the PANSS is an instrument description |
| NCT01393743 | population_description | Only a subset of participants in the Full Analysis Set who experienced these seizure types during the Prerandomization P | false positive | [none] subset of participants who experienced these seizure types defines who the outcome applies to |
| NCT01557959 | measure_title; measure_description | Response Rate Among Subgroups of Patients According to Molecular Profiles Including Tumor Characteristics and Genetic Po | true subgroup result | [non-demographic: molecular profile] response rate and survival posted for Low / High Cyclin D1 subgroups |
| NCT01590888 | population_description | ITT population. MRI was performed at one site only, resulting in a subset of participants available for analysis. | false positive | [none] subset of participants available (MRI at one site) |
| NCT01650259 | population_description | Efficacy Set: A subset of the safety set, which includes all patients in the "safety set" except those who had no availa | false positive | [none] efficacy set: a subset of the safety set |
| NCT01651949 | group_titles; analysis_groupIds | Females \| Heterosexual Males \| Men Who Have Sex With Men | true subgroup result | [demographic: sex] PRIMARY HPV antibody titres posted for females, heterosexual males and MSM with a between-group analysis; the groups are enrolment cohorts |
| NCT01665040 | measure_title; measure_description; population_description | Proportion of Subjects Satisfied With Treatment in a Sub-group Utilizing More Than 2 IPG Ports at 365 Days Post-IPG Impl | partial | [non-demographic] PRIMARY posted for a single device-use sub-group (&gt;2 IPG ports) |
| NCT01669642 | group_titles | Fracture Reduction Group 2-5 Years \| Fracture Reduction Group 6-11 Years \| Fracture Reduction Group 12-17 Years \| Absces | true subgroup result | [demographic: age] PRIMARY ED50/ED95 posted per age band (2-5, 6-11, 12-17 years) within procedure type |
| NCT01781481 | analysis_text:estimateComment | Inter rater reliability for a subset of 40 patients whose Pediatric INTERMED was scored by two trained raters. The media | false positive | [none] subset of 40 patients scored by two raters (reliability) |
| NCT01790828 | class_titles | Participants &lt;18 years (n=4) \| Participants ≥18 years (n=37) | true subgroup result | [demographic: age] haemorrhages during prophylaxis posted for &lt;18 and &gt;=18 years |
| NCT01831635 | group_descriptions | Patients with Polycythemia vera (PV), essential thrombocythemia (ET) and primary myelofibrosis (PMF) will be r \| For eac | false positive | [none] age-matching of controls in the arm description |
| NCT01838447 | measure_description | Hypercalciuria will be identified using calcium:creatinine ratios defined using age-specific norms and thresholds. | false positive | [none] age-specific norms define the outcome threshold |
| NCT01917916 | class_titles | Chinese participants \| Japanese participants | true subgroup result | [demographic: origin] PK (Cmax) posted for Chinese and Japanese participants as classes |
| NCT01919801 | measure_description | n pharmacokinetic analysis approach using sparse pharmacokinetic sampling obtained from a subset of subjects was used to | false positive | [none] sparse PK sampling from a subset of subjects |
| NCT01950819 | measure_title; measure_description | of Composite of tBPAR (Treated Biopsy-proven Acute Rejection)or eGRF&lt;50 mL/Min/1.73m2 by Subgroup | partial | [non-demographic] measure titled "by Subgroup" but the table rows are month 12 / month 24; the subgroup is not identifiable in the record |
| NCT01974687 | population_description | Pharmacokinetics (PK) Population: a subpopulation of the per-protocol population, selecting all participants exposed to  | false positive | [none] PK population: a subpopulation of the per-protocol population |
| NCT02028507 | population_description | QoL population: a subset of enrolled patients with available QoL questionnaires (the baseline and at least one more). | false positive | [none] QoL population: a subset of enrolled patients |
| NCT02119156 | measure_title; measure_description; class_title_cue | Percentage Change From Baseline in B Cell Subsets | false positive | [none] B cell subsets are laboratory cell populations |
| NCT02122198 | population_description | This outcome measure was only collected for a subset of the GnRH Agonist group. | false positive | [none] collected for a subset of the arm (data availability) |
| NCT02165826 | class_titles; measure_description | Sex=female \| Sex=male | partial | [demographic: age, sex] model-derived PDE4 inhibitory activity and simulated AE rates for reference participants at covariate levels (age, sex, weight, smoking); not observed outcomes by subgroup |
| NCT02195518 | measure_title; measure_description | Percentage of Participants in the Subgroup With Confirmed Multi-drug Resistance Mutations at Baseline With Serum HBV DNA | partial | [non-demographic] outcome posted for the one subgroup with multi-drug resistance mutations at baseline |
| NCT02203786 | measure_description | he specified time point. The mean (SD) of these single item ratings is presented for each sub-group. | false positive | [none] each sub-group refers to the study's four arms (gamblers/controls x drug) |
| NCT02207829 | measure_description | epeated measures model with covariates of treatment, baseline FEV1, centre group, 24 hour subset flag, Day, Day by basel | false positive | [none] 24 hour subset flag is a model covariate |
| NCT02246439 | class_titles; analysis_text:groupDescription; analysis_text:statisticalComment; measure_title; measure_description | &lt;18 years of age \| 18 years of age or older | true subgroup result | [demographic: age] PRIMARY treatment success posted for &lt;18 and &gt;=18 years with analyses; POST_HOC subgroup measure also by age |
| NCT02304406 | class_titles; measure_title | Female \| Male | true subgroup result | [demographic: sex, race] observational: ALK rearrangement frequency posted by gender (Female/Male) and, in a second measure, by race (White/Caucasian, Arabic, Others) |
| NCT02342548 | measure_description | r function, cognition, behavior and functional abilities. Total Maximum Chorea (TMC) is a subset of the TMS assessment,  | false positive | [none] TMC is a subset of the TMS assessment (instrument) |
| NCT02343406 | measure_title | Adult Study: Overall Survival in the Subgroup With Epidermal Growth Factor Receptor (EGFRvIII) Mutation | partial | [non-demographic] overall survival posted for the one EGFRvIII-mutation subgroup |
| NCT02415244 | group_titles | Pediatric Group \| Adult Group | true subgroup result | [demographic: age] observational PRIMARY posted for pediatric vs adult groups (the study's cohorts) |
| NCT02417701 | population_description | analyzed for ORR is the response evaluable cohort as defined by the protocol, which is a subset of the total study popul | false positive | [none] response evaluable cohort ... a subset of the total study population |
| NCT02442778 | measure_description | characteristics of participants with AD. The cognitive subscale (ADAS-cog) consists of 11 subsets related to memory, pra | false positive | [none] 11 subsets of an instrument |
| NCT02507375 | population_description | Pharmacokinetic analysis subset, defined as all participants from either cohort with adequate data for performing PK ana | false positive | [none] PK analysis subset |
| NCT02542293 | population_description; measure_description | The bTMB ≥20 mut/Mb analysis set included the subset of participants in the FAS whose blood TMB status was ≥20 mut/Mb at | true subgroup result | [non-demographic: biomarker] OS/PFS/ORR posted for bTMB- and PD-L1-defined analysis sets (several levels) and for a China cohort |
| NCT02549170 | population_description | E1:Placebo Relapse / E2:GGL/KIOVIG Set included a subset of participants who had a relapse while on placebo in Epoch 1,  | false positive | [none] analysis set defined by relapse in a previous epoch |
| NCT02576678 | group_titles | Group 1 Adolescents: Apremilast 20 mg \| Group 1 Adolescents: Apremilast 30 mg \| Group 2 Children: Apremilast 20 mg | partial | [demographic: age] AEs posted for adolescent and child groups whose doses differ by group; age and dose confounded |
| NCT02588664 | measure_title; population_description | Subgroup Analysis: Race | false positive | [none] measures titled "Subgroup Analysis: Race / Sex / Age / ..." are registered but carry no data table; no subgroup result is in the record |
| NCT02593825 | group_descriptions; analysis_groupIds; analysis_text:groupDescription; population_description | Intervention incorporating cognitive factors and focusing on self-initiated movement toward achievement of ski \| Early m | true subgroup result | [non-demographic: baseline severity] PRIMARY Bayley score posted for full group, severely delayed and mildly delayed |
| NCT02612337 | analysis_text:nonInferiorityComment | The target sample size for each was 160 randomized subjects: 80 in each treatment group stratified by gender. The sample | false positive | [none] randomisation "stratified by gender" in a sample-size comment |
| NCT02662062 | measure_title; measure_description | Changes in the Immune Regulatory Molecules OX-40/LAG3/PD1/ICOS on T Cell Subsets as Assessed by Flow Cytometry. | false positive | [none] T cell subsets are laboratory cell populations |
| NCT02678572 | measure_description | Subgroup analysis of Demographic information, including age, gender, race, and ethnicity. This demographic informatio | false positive | [none] measure described as "subgroup analysis of demographic information" holds one value for one group; no result by subgroup |
| NCT02685826 | group_descriptions | High risk, transplant non-eligible \[TNE\], newly diagnosed multiple myeloma (NDMM) participants who were admi \| \&gt;= 65  | false positive | [none] age &gt;=65 is the eligibility criterion of one cohort; no second age level |
| NCT02688764 | measure_title; class_titles | Participants With Serum Phosphorus Levels Within the Age-dependent Target Range in Each Stage | true subgroup result | [demographic: age] POST_HOC serum phosphorus change posted by age group (&lt;2, 2-&lt;6, 6-&lt;12, 12-18 years) |
| NCT02726178 | group_descriptions | Oral ibuprofen (Advil® Pediatric drops for infants less than 3 months of age; Wyeth-Ayerst 40 mg/ml, DIN 22425 \| Oral pl | false positive | [none] for infants less than 3 months of age is part of a product name |
| NCT02736890 | measure_description | to be valid in an interview/self -report format. The pain affecting day-to-day activities subset of the dataset is score | false positive | [none] subset of the dataset is an instrument component |
| NCT02737397 | measure_title; population_description | Efficacy of Side Effect Prevention Using the Acute Hangover Scale (Hangover Sensitive Subpopulation) | partial | [non-demographic] POST_HOC result for one "hangover sensitive" subpopulation |
| NCT02783300 | measure_description | entific judgment or is associated with liver injury and impaired liver function. SAEs are subset of AEs. A summary of nu | false positive | [none] SAEs are subset of AEs |
| NCT02793544 | category_titles; measure_title | Female \| Male | partial | [non-demographic] descriptive result for one subgroup (4 HIV-positive participants); the sex categories are donor traits |
| NCT02806232 | group_descriptions | Participants aged 13-24 months received Levo Praziquantel ODT (150 mg) administered orally at a dose of 50 mg/ \| Partici | partial | [demographic: age] clinical cure posted per cohort; infant age cohorts receive their own formulation/dose |
| NCT02869633 | measure_title | B Cell Subsets and Signaling Assessed by Mass Cytometry | false positive | [none] B/T cell subsets are laboratory cell populations |
| NCT02935062 | population_description | The population is described by the age and using the variables of PCC, phonemes acquired The numbers shows the meand and | false positive | [none] the population is described by the age |
| NCT02938052 | measure_description | ale will be used to measure HF-specific health-related QoL (HRQoL), and an eight-question subset of the KCCQ will be use | false positive | [none] eight-question subset of the KCCQ (instrument) |
| NCT02988401 | measure_title | Fingerstick Blood Glucose (Subset) | false positive | [none] (Subset) marks a measurement taken in some participants |
| NCT03031912 | group_descriptions; analysis_groupIds | Participants randomly assigned to receive ≥2 x 107 pfu of the V920 (rVSVΔG-ZEBOV-GP) Ebola Virus Vaccine. ≥50 \| Particip | partial | [demographic: age] immunogenicity posted per cohort; cohorts are defined jointly by CD4 count and adult/adolescent status |
| NCT03098836 | group_titles | Caucasian \| African American | true subgroup result | [demographic: race] PRIMARY 2-year PFS posted for Caucasian and African American groups (race-defined cohorts, same therapy) |
| NCT03100058 | measure_title | Number of Subjects With Response Rate According to Percent Decrease in Body Weight for Subgroups | true subgroup result | [non-demographic: glycaemic status] weight-loss response posted for dysglycemic, normoglycemic and T2DM subgroups |
| NCT03207867 | group_titles | Non-Japanese Patients \| Japanese Patients | true subgroup result | [demographic: origin] PK posted for Japanese and non-Japanese patient groups |
| NCT03278782 | population_description | inator for TfH/AITL to calculate the number. We are calculating ORR for the whole and the subset so the denominators wil | partial | [non-demographic] response rate posted for the ITT set and one lymphoma subtype (TFH phenotype) |
| NCT03280563 | measure_description | minal time restarted to 0 at Stage 2. PK data for Stage was analyzed and reported for the subgroups \[crossover arms (St | false positive | [none] subgroups [crossover arms] are study arms |
| NCT03292705 | measure_description | core is not a norm-referenced measure and is not adjusted for age. EF was measured with a subset of five tasks (flanker, | false positive | [none] subset of five tasks (instrument) |
| NCT03334695 | population_description | The Intent-to-treat-Challenge (ITTc) population is a subset of the Full Analysis Set (FAS) (all participants who were ra | false positive | [none] ITTc population is a subset of the FAS |
| NCT03390673 | population_description | Pharmacokinetic Parameter (PKP) Population is a subpopulation of the PKC population who have at least one evaluable prim | false positive | [none] PKP population is a subpopulation of the PKC population |
| NCT03414970 | measure_description | s in reconstruction complication rates will be assessed separately within each of the six subgroups based on type and ti | false positive | [none] subgroup analyses by reconstruction type are described but the measure carries no data |
| NCT03443973 | measure_title; population_description | in Brain Amyloid Load as Measured by Amyloid Positron Emission Tomography (PET) Scan in a Subset of Participants | false positive | [none] in a subset of participants (PET / CSF substudy) |
| NCT03500094 | class_titles; measure_description | 12 to &lt;16 years old \| 16 to &lt;18 years old \| 12 to &lt;18 years old | partial | [demographic: age] PRIMARY PK by adolescent age band, but the values are model simulations for the age subgroups |
| NCT03574389 | group_descriptions; measure_description | Participants vaccinated 3 infant series doses of 13vPnC (single intramuscular injection): Vaccination 1 (Visit \| Partici | false positive | [none] ages in the arm descriptions are the vaccination schedule; "randomly selected subset" for an assay |
| NCT03653364 | population_description | Intent-to-Treat Influenza-Infected (ITTi) population is a subset of ITT participants who had a laboratory confirmation o | false positive | [none] ITTi population is a subset of ITT |
| NCT03686215 | measure_description | Percentage of participants having residual tumor removed within subpopulation of having negative margins after Standard  | false positive | [none] within subpopulation having negative margins defines who the outcome applies to (post-surgery) |
| NCT03722810 | group_titles | Female \| Male | true subgroup result | [demographic: sex] SECONDARY: arms combined and split into Female / Male groups to report patient-centredness by gender |
| NCT03908125 | population_description | The number analyzed in the first 2 rows are the subsets of the overall insertions based on HCP experience. | partial | [non-demographic] success rate overall and by operator (HCP) experience; the subgroup is of procedures/operators, not participants |
| NCT03993119 | group_titles; measure_title; measure_description; category_titles | Sex: Female \| Sex: Male | true subgroup result | [demographic: sex, age] observational PRIMARY measures posted "according to sex" and by age category |
| NCT04023227 | population_description | nts to whom study treatment was assigned at randomization. This measure was assessed in a subset of participants who had | false positive | [none] assessed in a subset of participants who had an ICD |
| NCT04024228 | class_titles | A/H1N1: Day 28: 60-64 years \| A/H3N2: Day 28: 60-64 years \| B1: Day 28: 60-64 years \| B2: Day 28: 60-64 years | true subgroup result | [demographic: age] PRIMARY influenza antibody titres posted for 60-64 and &gt;=65 years |
| NCT04066114 | population_description | Intent-to-treat population, which includes the same subjects as the safety population, subset to participants who remain | false positive | [none] subset to participants who remained in the study |
| NCT04105244 | class_titles | Female Baseline- Time 1 \| Female 6 weeks- Time 2 \| Male Baseline- Time 1 \| Male 6 weeks- Time 2 | true subgroup result | [demographic: sex] PRIMARY posted for Female and Male by time point |
| NCT04259762 | class_titles; measure_description; measure_title | Ever Had A Mammogram To Screen For Breast Cancer, Women 50-75 years \| Ever Completed an FOBT or Colonoscopy To Screen Fo | true subgroup result | [demographic: sex] PRIMARY screening uptake posted separately for men and women (stool test, colonoscopy); the age bands are screening-eligibility ranges, not levels |
| NCT04323137 | class_titles; measure_title | American Indian or Alaska Native \| Asian \| Black or African American \| Native Hawaiian or Other Pacific Islander | true subgroup result | [demographic: race, ethnicity, sex] POST_HOC vaccination rate by race, by ethnicity and by gender |
| NCT04354155 | measure_description | .49 anti-Xa U/mL in children hospitalized with COVID-19, and to compare dose-requirements by age group (\&lt;12 and those \ | true subgroup result | [demographic: age] enoxaparin dose requirement posted for &lt;12 and &gt;=12 years |
| NCT04433585 | population_description | A subset of modified intent-to-treat (mITT) patients with at least 1 BILAG A score or 2 BILAG B scores at Baseline. | false positive | [none] a subset of mITT patients defines analysis sets |
| NCT04511650 | population_description | e of study drug and had at least 1 post-dose efficacy evaluation. The FA Population was a subset of the ITT Population w | false positive | [none] FA population was a subset of the ITT population |
| NCT04569032 | measure_description | Participants without progression or death were censored. DOR was only calculated for the subset of participants achievin | false positive | [none] subset of participants achieving a CR or PR defines the duration-of-response set |
| NCT04587167 | category_titles; measure_title; measure_description | Female - Initiated HPV vaccine \| Female - Not initiated \| Male - Initiated HPV vaccine \| Male - Not initiated | true subgroup result | [demographic: sex] HPV vaccination posted by sex within 11-14 and 15-17 year-olds |
| NCT04681066 | measure_description; population_description; class_title_cue; analysis_text:groupDescription | teps: 1) Hazard ratio (dose vs placebo) using stratified Cox regression w/ stratification by sex and hematocrit (high/lo | partial | [non-demographic] PRIMARY posted for all treated and one high-haematocrit subgroup; "stratification by sex" is a model term |
| NCT04762160 | measure_title; measure_description | ORR in a Subset of Participants With MT EZH2 | partial | [non-demographic] ORR posted for the pooled group and one mutation subset (n=5 trial) |
| NCT04907253 | class_titles; measure_description | Baseline Women \| t1 women \| Baseline men \| t1 men | true subgroup result | [demographic: sex] PRIMARY inflammatory markers posted for women and men by time point |
| NCT04960397 | group_titles | Cohort 1 - 9-17 Yrs, 1 Dose 10^9 TCID50 Sing2016 M2SR H3N2 \| Cohort 1 - 9-17 Yrs, 1 Dose Placebo \| Cohort 2 - 2-8 Yrs, 1 | partial | [demographic: age] reactogenicity posted per cohort; age cohorts (9-17, 2-8 years) receive different doses |
| NCT04996264 | group_descriptions | Varespladib-methyl is an immediate-release (IR), oval, white, film-coated tablet at a dosage strength of 250 m \| The ora | false positive | [none] age wording occurs only in product/arm descriptions |
| NCT05009251 | class_titles; measure_title | Female \| Male | true subgroup result | [demographic: race, ethnicity, sex] vaccination posted by gender, by race and by ethnicity |
| NCT05024032 | measure_description | omains of obesity related HRQoL: Physical (7 items) and Psychosocial (13 items). A 5-item subset of the Physical domain  | false positive | [none] 5-item subset of the Physical domain (instrument) |
| NCT05055128 | population_description | ents who had been randomized and received at least 1 dose of study drug. FAS erosive is a subset of FAS consisted of pat | partial | [non-demographic] PRIMARY posted for one baseline-defined subset (erosive oesophagitis) |
| NCT05086315 | group_titles; measure_description | Dose Expansion - Adult Arm (Cohort A1): SAR443579 1000 mcg/kg \| Dose Expansion - Adult Arm (Cohort A2): SAR443579 \| Dose | partial | [demographic: age] adult and paediatric arms are separate dose cohorts; one transfusion-dependence subgroup described |
| NCT05093829 | group_titles; population_description | Vaccination at 9 Months of Age With NmCV-5 \| Vaccination at 9 Months of Age With MenACWY-TT \| Vaccination at 15 Months o | true subgroup result | [demographic: age] PRIMARY seroprotection posted for vaccination at 9 vs 15 months of age, each vaccine in each age cohort |
| NCT05195359 | population_description | roach, only including participants who did sufficiently engage with the app, that is, the subgroup of app users who trac | false positive | [none] per-protocol-style set of frequent app users (post-randomisation engagement) |
| NCT05298306 | population_description | Subgroup of the pharmacokinetic population. Data for subjects with no measurable levels of LAT8881 were excluded from | false positive | [none] subgroup of the pharmacokinetic population (measurable levels) |
| NCT05329194 | measure_description; measure_title | ted for this endpoint. The median is the descriptive statistics median calculated using a subset of Participants with an | true subgroup result | [demographic: age, race; non-demographic] exacerbation outcomes posted for listed subgroups including adolescents and Black or African American participants, plus eosinophil and allergy subgroups |
| NCT05357144 | measure_description | old females at 12-15 months from routinely collected health facility data, disaggregated by sex and age, aggregated to t | false positive | [none] disaggregated by sex and age describes the data source; the outcome is for one sex-age group |
| NCT05595642 | measure_description | or weak, sleep disturbance, and scared or worried; 3 items). The E-RS:COPD is an 11-item subset of the EXACT designed to | false positive | [none] 11-item subset of the EXACT (instrument) |
| NCT05654623 | population_description | QTc substudy analysis set= subset of SAS participants in vepdegestrant (ARV-471) arm, randomized at selected sites and h | false positive | [none] QTc substudy analysis set = subset of SAS |
| NCT05707351 | population_description | The Pharmacokinetic Analysis Set (PK AS), a subset of the PK FAS, included all PK participants who received at least 1 A | false positive | [none] PK AS, a subset of the PK FAS |
| NCT05838664 | class_titles; measure_title; measure_description | Elderly (&gt;=80 years) \| Non-elderly (18 to &lt;80 years) | partial | [demographic: age] number of participants in each subgroup (elderly / non-elderly ...): subgroup sizes, not an outcome by subgroup |
| NCT05966090 | measure_description | The pIMD was a subset of adverse events of special interest that included autoimmune diseases and other inflammatory and | false positive | [none] pIMD was a subset of adverse events |
| NCT05985304 | measure_description | Residual manifest refractive cylinder by subgroups of 0.25 D preoperative keratometric cylinder at 120 to 180 days post- | true subgroup result | [non-demographic: baseline keratometric cylinder] residual cylinder posted for low and high preoperative-cylinder groups in each arm |
| NCT06815562 | group_titles | Experimental, Child Sample \| Control, Child Sample \| Experimental, Adolescent Sample \| Control, Adolescent Sample | true subgroup result | [demographic: age] PRIMARY posted for experimental and control within child and adolescent samples |

### 4.4 Negatives: a random sample and a targeted sample of likely reporters

1,864 trials have no signal. Two samples of them were reviewed (100 trials):

- **Random, 60**: `random.Random(20261113).sample(sorted(non_detected_ids), 60)`. Sixty is the size requested; with 0 events it bounds the miss rate among non-detected trials below about 6%, which is weak, hence the second sample.
- **Targeted, 40**: the 40 highest "likely reporter" scores among the other 1,804 non-detected trials (score: +2 Phase 3; +2 enrolment >= 500 and +1 more if >= 1,000; +1 a measure with >= 6 classes; +1 a measure with >= 4 groups; +2 a measure title containing "by" or "stratified"; +2 a loose demographic word in a class, category or group title or an analysis group description; +1 "interaction" or "stratif" in analysis text; ties broken by a seeded shuffle). All 40 scored >= 6; 43 non-detected trials scored that high, so the targeted sample covers nearly all of the highest-scoring stratum. Justification: if the rule misses subgroup tables, large Phase 3 trials with many rows or groups and "by ..." titles are where they would be.
- What was read for each: all distinct group titles, up to 14 distinct class titles and 8 category titles, every measure title containing "by" or "stratified", and every loose demographic or stratification mention in any inspected field; a second scan of all class and category titles for non-demographic level words (region, prior therapy, mutation status, severity and the like); the cached record for 5 candidates. Class titles beyond the first 14 were covered only by those two scans.

| Sample | Reviewed | Demographic subgroup result missed | Non-demographic subgroup result missed | Partial | No subgroup result |
|---|---:|---:|---:|---:|---:|
| Random non-detected | 60 | 0 | 1 | 0 | 59 |
| Targeted likely reporters | 40 | 1 | 3 | 1 | 35 |

The cases the rule missed or half-missed:

| NCT ID | Sample | Judgement | What was missed and why |
|---|---|---|---|
| NCT02178722 | random | missed subgroup result | [non-demographic: tumour type / PD-L1 level] Phase 2 ORR, DOR and PFS are posted in class rows per tumour-type cohort, with NSCLC split into PD-L1 high and low/negative; no "subgroup" wording, so no signal |
| NCT00251719 | targeted | partial | [non-demographic] healing rate posted for one baseline-severity level only (erosive esophagitis grade C or D combined) (selection score 7: phase3, n&gt;=500, n&gt;=1000, title:by/stratified) |
| NCT00493870 | targeted | missed subgroup result | [non-demographic: TOP2A status] 3-year DFS Stratified by TOP2A posted for Amplification / Deletion / Normal groups within each arm; "stratified by &lt;non-demographic&gt;" is excluded from the cue rule (selection score 7: phase3, n&gt;=500, n&gt;=1000, title:by/stratified) |
| NCT02502266 | targeted | missed subgroup result | [demographic: origin] PRIMARY PFS and OS are posted separately for Japanese patients enrolled after the phase 3 cutoff (Arms V-VII, n=5/8/7); the titles say "JP Patients", which the origin lexicon does not contain (selection score 6: phase3, n&gt;=500, groups&gt;=4, analysis:interaction/stratif) |
| NCT02437318 | targeted | missed subgroup result | [non-demographic: PIK3CA status] PFS and other outcomes posted separately for PIK3CA-mutant and non-mutant cohorts (by tissue and by ctDNA); cohort wording, no "subgroup" keyword (selection score 6: phase3, n&gt;=500, classes&gt;=6, groups&gt;=4) |
| NCT01029353 | targeted | missed subgroup result | [non-demographic: pre-operative diagnosis] 20 measures titled "... Stratified by Pre-operative Diagnosis" (NEC vs isolated perforation) with treatment-by-diagnosis interaction tests; excluded "stratified by &lt;non-demographic&gt;" wording (selection score 6: n&gt;=500, groups&gt;=4, title:by/stratified, analysis:interaction/stratif) |

Assessment of missed cases:

- **Demographic subgroup results.** None in the 60 random negatives (0/60; Wilson 95% upper bound 6.0%, which over 1,864 non-detected trials would still allow up to about 112 trials). One in the 40 likely reporters: Japanese-patient arms labelled "JP". So misses exist, they are of the abbreviation or unusual-wording kind, and this review cannot put a tight number on them. The detected-and-confirmed count (34/2,000) is a floor for the sample, not an estimate of prevalence.
- **Non-demographic subgroup results** (biomarker status, tumour type, diagnosis) are missed more often: 1/60 random and 3/40 targeted. They are posted as cohort-named groups or "stratified by [factor]" measures without the word "subgroup", which the rule excludes on purpose to avoid stratified-test wording. The rule should not be used to count non-demographic subgroup reporting.
- Not assessed at all: subgroup results that exist only in posted documents or publications, and anything in languages other than English.
- One reviewer (the auditing model) made every judgement in 4.3 and 4.4, from digests in `review/`. No second reviewer, no human review, no agreement statistic. An independent human reading of `subgroup_review.csv`, at least of the 41 true and 24 partial rows and the 100 negatives, is what would validate these figures.

## 5. Denominators and historical inference

### 5.1 Baseline total versus participant-flow STARTED versus enrolment: exact fields

| Quantity | Exact JSON path and rule |
|---|---|
| **BT** baseline total | `resultsSection.baselineCharacteristicsModule.denoms[]`: the row whose `units` is "Participants" (it exists in 2,000/2,000 records; 14 records carry a second row in another unit, which is ignored). Value = `counts[].value` for the Total group chosen by rule T: the only group if there is one; else the group whose trimmed, case-folded `groups[].title` is exactly "total"; else, with 3+ groups, a last group whose count equals the sum of the earlier groups. If rule T finds none, the sum over all groups. **Never the Total plus the arms.** |
| **FS** flow started | `resultsSection.participantFlowModule.periods[0].milestones[]` where `type == "STARTED"`: `achievements[].numSubjects` summed over `participantFlowModule.groups[]`, excluding any group titled exactly "Total" (none occurs). First period only. Where `typeUnitsAnalyzed` is present (35 records) a second unit is in `numUnits`; `numSubjects` is still participants. |
| **EN** enrolment | `protocolSection.designModule.enrollmentInfo.count`, with `.type` (ACTUAL in 2,000/2,000). |
| Analysis population | `baselineCharacteristicsModule.populationDescription`, free text, present for 669/2,000; quoted, never inferred when absent. |

Rule T outcome: titled "Total" 1,378; single group 618; untitled total (last group = sum of the others) 4; no total, arms summed 0. All three quantities were numeric for 2,000/2,000.

**Double counting.** A rule that takes "the group titled Total, else the sum of all groups" double-counts the 4 records whose total has another title: NCT00453973 (correct 114, naive 228); NCT01262365 (correct 786, naive 1572); NCT01506882 (correct 71, naive 142); NCT03215277 (correct 76, naive 152) ("Total Title" three times, "Pooled ..." once). Conversely the posted Total is not always the sum of the arms: NCT00066703 (Total 5320, arms sum 2660); NCT02591537 (Total 8, arms sum 16). The first is a registry entry whose Total is twice its arms; the second is a crossover whose two arm columns are the same 8 participants. In both the posted Total is used as posted and flagged here.

| Comparison (denominator 2,000) | equal | first smaller | first larger |
|---|---|---|---|
| BT vs FS | 1,585/2,000 (79.2%; 95% CI 77.4-81.0) | 371/2,000 (18.6%; 95% CI 16.9-20.3) | 44/2,000 (2.2%; 95% CI 1.6-2.9) |
| BT vs EN | 1,448/2,000 (72.4%; 95% CI 70.4-74.3) | 532/2,000 (26.6%; 95% CI 24.7-28.6) | 20/2,000 (1.0%; 95% CI 0.6-1.5) |
| FS vs EN | 1,736/2,000 (86.8%; 95% CI 85.2-88.2) | 237/2,000 (11.8%; 95% CI 10.5-13.3) | 27/2,000 (1.4%; 95% CI 0.9-2.0) |
| All three equal | 1,405/2,000 (70.2%; 95% CI 68.2-72.2) | | |

Mismatch patterns (595 trials where the three are not all equal):

| Pattern | Trials |
|---|---:|
| BT smaller FS; BT smaller EN; FS equal EN | 324 |
| BT equal FS; BT smaller EN; FS smaller EN | 173 |
| BT smaller FS; BT smaller EN; FS smaller EN | 27 |
| BT larger FS; BT equal EN; FS smaller EN | 26 |
| BT smaller FS; BT equal EN; FS larger EN | 17 |
| BT larger FS; BT smaller EN; FS smaller EN | 8 |
| BT larger FS; BT larger EN; FS equal EN | 7 |
| BT equal FS; BT larger EN; FS larger EN | 7 |
| BT smaller FS; BT larger EN; FS larger EN | 3 |
| BT larger FS; BT larger EN; FS smaller EN | 3 |

### 5.2 Fifteen representative mismatches, reviewed before interpretation

Selection: the 15 were allocated across mismatch patterns in proportion to frequency (at least one from each of the six commonest), drawn with seed 20261113 from the sorted IDs of each pattern (`scripts/denominators.py`). "Reading" is the auditing model's reading of the record's own text (baseline population description, pre-assignment details, period and group titles); where the record gives no reason, that is said.

| NCT ID | BT | FS | EN (type) | Pattern | Baseline population (verbatim, truncated) | Reading |
|---|---:|---:|---:|---|---|---|
| NCT01473524 | 216 | 217 | 217 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | Intent-to-treat Population: All participants who were randomized and received at least 1 dose of study drug. | Baseline is the "intent-to-treat" set defined in the record as randomised and dosed; 1 of 217 randomised was never dosed. Baseline describes treated participants. |
| NCT00145470 | 324 | 326 | 326 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | Includes all participants receiving study treatment. | Baseline "includes all participants receiving study treatment"; 2 started but were not treated. |
| NCT01142791 | 115 | 121 | 121 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | Baseline data is reported for the 115 participants who underwent the ExAblate treatment. Per protocol, 6 participants receiving only 1 sonication are  | Baseline is the 115 in the primary analysis; 6 participants with one sonication sit in a separate flow group and in adverse events only (stated in the record). |
| NCT02787681 | 69 | 71 | 71 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | (none) | No population description. Two fewer in baseline than started; the record gives no reason. Cannot be interpreted. |
| NCT00095979 | 50 | 52 | 52 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | Eligible and treated patients | Baseline is "eligible and treated patients"; 2 enrolled were not. |
| NCT03566810 | 53 | 54 | 54 (ACTUAL) | BT smaller FS; BT smaller EN; FS equal EN | Safety population included all participants who received at least 1 dose of investigational medicinal product (IMP). | Crossover; baseline is the safety population (at least one dose); 1 randomised participant was not dosed. |
| NCT00435188 | 398 | 398 | 400 (ACTUAL) | BT equal FS; BT smaller EN; FS smaller EN | (none) | Flow and baseline agree at 398; enrolment is 400. No text explains the 2; plausibly enrolled but not randomised, but the record does not say. |
| NCT03639948 | 115 | 115 | 120 (ACTUAL) | BT equal FS; BT smaller EN; FS smaller EN | Intention-to-treat | Pre-assignment text: 5 of 120 enrolled were found ineligible and are excluded from flow and baseline. |
| NCT00677014 | 980 | 980 | 1060 (ACTUAL) | BT equal FS; BT smaller EN; FS smaller EN | (none) | Pre-assignment text: 1,060 enrolled; 46 not implanted and 34 not randomised before assignment; flow and baseline describe the 980 randomised. |
| NCT00589914 | 1214 | 1214 | 1221 (ACTUAL) | BT equal FS; BT smaller EN; FS smaller EN | (none) | Pre-assignment text: 1,221 enrolled (1 enrolled twice), 1,220 randomised, 1,214 dosed; flow and baseline are the dosed set. |
| NCT00533897 | 157 | 167 | 270 (ACTUAL) | BT smaller FS; BT smaller EN; FS smaller EN | All Period 1 Non-responders and Period 2 participants who received at least one dose of abatacept during the study were summarized. | Four-period design. 270 enrolled, 167 treated in the lead-in (the first period, which FS counts), baseline covers 157 (lead-in non-responders plus period-2 participants, per its description). The baseline groups are not the first-period flow groups. |
| NCT02656836 | 6 | 9 | 20 (ACTUAL) | BT smaller FS; BT smaller EN; FS smaller EN | (none) | Observational. 20 enrolled, 9 started, 6 in baseline; no explanatory text. Cannot be interpreted. |
| NCT00245856 | 67 | 28 | 67 (ACTUAL) | BT larger FS; BT equal EN; FS smaller EN | (none) | Artefact of the FS rule: the first flow period holds only 28 participants in two groups, while baseline describes all 67 treated. Not a registry discrepancy. |
| NCT01224626 | 41 | 42 | 41 (ACTUAL) | BT smaller FS; BT equal EN; FS larger EN | (none) | Observational. Flow started 42; baseline and enrolment 41. One participant unexplained in the record. |
| NCT02992925 | 365 | 30 | 370 (ACTUAL) | BT larger FS; BT smaller EN; FS smaller EN | 【cohort 2\_Age,Sex/Gender】 Excluding 5 subjects(BK1310: 1 subject, control: 4 subjects) whose all antibody titers could not be measured at Visit 4 | Artefact of the FS rule: the first period is "Cohort 1" (30 participants); cohort 2 enters in the second period. Baseline (365) also excludes 5 without antibody titres, as its description states. |

What the reviewed mismatches support:

- In 9 of 15 the record itself explains the difference: baseline is a treated, eligible or analysed set, or people were enrolled and lost before assignment. In 2 of 15 (NCT00245856, NCT02992925) the difference is produced by counting STARTED in the first period only; 38 of the 44 "BT larger than FS" trials have multi-period flows, where cohorts that enter in a later period are not in the first-period count; that pattern is probably mostly this artefact, which was not checked trial by trial. In 4 of 15 the record gives no reason.
- A baseline population description exists for 208/415 of the trials where BT differs from FS, so about half of the mismatches cannot be explained from the record.
- The general statement that survives: the baseline table describes the population the registrant chose to describe, most often everyone who started (BT = FS in 79.2%), otherwise usually a treated or analysed subset slightly smaller than those who started. It is not safe to label the baseline population "enrolled", "randomised" or "ITT" for a given trial without reading its description, and for half of the mismatching trials there is none.

### 5.3 Outcome switching: what current records cannot establish

**Current records alone cannot establish departures from prospectively specified outcomes.** In a record with results, the protocol-section outcome list and the results outcome list are one list (the first audit found title, time frame and description identical for 4,406/4,406 primary and 10,833/10,833 secondary pairs compared position by position). That identity is how the registry stores the record; it is not evidence that outcomes were unchanged. Assessing switching needs, per trial:

- **historical registry versions** (the outcome list as first registered and at each change), which the v2 API fields used here do not return and this audit did not fetch;
- **registration timing** relative to enrolment and to primary completion, to know whether the first registered list was prospective at all;
- and, for anything beyond titles, the **protocol and statistical analysis plan** (and their amendment history), because registry entries are short and are sometimes written after the protocol.

What the current records show about the availability of those inputs (sample of 2,000; `scripts/history_evidence.py`; documents fetched with `fields=NCTId,DocumentSection`, 2,000 of 2,000 returned):

| | n / denominator |
|---|---|
| First submitted to the registry after the study start date (month precision where the start date has no day) | 649/2,000 (32.5%; 95% CI 30.4-34.5) |
| First submitted after the primary completion date | 108/2,000 (5.4%; 95% CI 4.5-6.5) |
| Record updated after results were first posted | 856/2,000 (42.8%; 95% CI 40.6-45.0) |
| Protocol posted on the registry | 1,057/2,000 (52.9%; 95% CI 50.7-55.0) |
| Statistical analysis plan posted | 1,035/2,000 (51.8%; 95% CI 49.6-53.9) |
| Protocol posted, results first posted 2017 or later | 1,053/1,432 (73.5%; 95% CI 71.2-75.8) |
| Registered on or before start, with protocol and SAP posted | 810/2,000 (40.5%; 95% CI 38.4-42.7) |

These are availability counts. No history version, protocol or SAP was opened, a posted protocol is usually the final version, and "registered after start" compares two registrant-entered dates. A third of the sampled trials were registered after they started; for those, even the full registry history cannot show the prospectively specified outcome.

### 5.4 Inferences this audit does not make, and that the data do not support

- **No demographic subgroup outcome is inferred from an overall outcome and baseline proportions.** A trial that reports 30% Black participants at baseline and an overall hazard ratio says nothing about the hazard ratio among Black participants; that requires a result posted for the subgroup (section 4) or participant-level data. Every "race reported and analysis posted" figure in this report is a co-occurrence in one record and nothing more.
- **No causal effect is inferred from study-level associations.** Differences between groups of trials (race reported vs not, industry vs other, posted vs not posted, one endpoint family vs another) are differences between sets of studies that also differ in era, phase, size, sponsor, condition and regulatory status. They do not show that any of those characteristics causes reporting behaviour, and they say nothing about treatment effects in any participant group (the ecological fallacy applies in both directions).
- Nothing here is a finding of non-compliance with any reporting requirement.

## 6. Missing data, retrieval failures, parsing failures and negative findings (kept separate)

**Retrieval failures**

- Sampled study records: 0 of 2,000 missing (first audit's fetch). Count queries: 0 failures of 191 requests. Family top-up: 200 of 200 drawn records returned. Document sections: 2,000 of 2,000 returned. All 243 requests logged in `cache/fetch_manifest.jsonl` returned HTTP 200 with no retries needed.
- Not cached: 12 exploratory count requests made by hand before the caching helper existed; each was re-issued through the cache and returned the same count.

**Parsing failures**

- Cache lines that failed to parse: 0. Baseline totals, STARTED totals and enrolment counts that were non-numeric: 0, 0, 0 of 2,000.
- Endpoint families: every examined trial had a posted PRIMARY measure matching its family regex (0 unmatched of 246). Time frames that are not one fixed time point are reported as "event-driven/variable", not dropped: PFS (oncology) 45/50, OS (oncology) 41/52, HbA1c (diabetes) 1/15, FEV1 (asthma/COPD/CF) 15/42, ACR20 (arthritis) 5/44, Depression scales 6/43. Some of these are fixed time points written in a form the parser does not read.
- Regex attribute classes in section 3.3 that fell to a residual class: PFS assessor not stated 20/50, PFS criteria none stated 16/50, FEV1 timing not stated 8/42, analysis population "other wording" or "not stated" in every family. These are limits of the parser as well as of the records.

**Missing data in the records**

- Usable-content failures are listed by reason in 2.2 (B1 9, F1 5, O1 95, A1 68 trials; IDs in `usable.json` under `not_usable_ids`). 195 PRIMARY measures have no data table.
- Baseline `populationDescription` absent in 1,331/2,000. Analysis `paramType` absent in the first audit's count for 669 of 2,017 primary analyses (not recomputed).
- The release has no intervention field; intervention names in 3.3 come from the API for the 246 examined trials only. `primary_endpoint` is the first registered primary outcome only. 74 release records have no primary completion date.
- The first audit's cache did not request `ConditionsModule`, `ArmsInterventionsModule` or `DocumentSection`; conditions were taken from the release, and the other two were fetched in this pass where needed.

**Negative findings**

- No sampled trial lacks `hasResults`; the frame has no trials without results, so nothing can be said from it about non-posting.
- No race- or ethnicity-level subgroup result on a PRIMARY measure was found in class or category titles in 2,000 trials; the one PRIMARY race result is by race-defined arms (NCT03098836). Race or ethnicity subgroup results in any measure: 6/2,000.
- 0 of 60 randomly sampled non-detected trials were judged to contain a demographic subgroup result.
- The measures registered as "Subgroup Analysis: Race / Sex / Age" in NCT02588664 carry no data: an announced subgroup analysis without a posted result.
- No signal came from `measurements[].comment`, `otherAnalysisDescription`, `statisticalMethod` or the two confidence-limit comment fields; the analysis free-text fields together produced 7 detected trials.
- The frame-defining code did not change between the release commit and the checkout read.
- No oncology restriction was found in the definition of the existing industry Phase 3 cohort.

## 7. What remains provisional

- Everything about **results posting** is count-level (section 1.3). No per-trial audit of trials without results was done, legal applicability and deadlines were not established, and the "eligible" population is whatever the query matched.
- Every sample-based percentage is **conditional on posted results** and rests on one seeded sample of 2,000 (2.5% of the frame). Rare features rest on a handful of trials; the observational stratum has 117.
- The **usable-content rules** are this audit's definitions. They test readability of tables, not correctness. Reasonable alternative rules (for example requiring numbers analysed > 0 in every arm) move the figures by a point or two, as the variants in 2.2 show; requiring the all-cause mortality table, which records posted before 2017 cannot have, would cap the adverse-event figure at 63.4%.
- "**Comparative**" is structural (2+ groups on a primary measure, randomised allocation). Trials whose only posted comparison is on a secondary measure, or whose 2+ groups are cohorts, are misclassified in both directions.
- **Subgroup figures** are "detected by the current method" plus one reviewer's labels. No independent or human review; 5 of 23 labels changed between two passes by the same kind of reviewer; recall is bounded only loosely (0/60 random, 1/40 targeted). The boundary between "true" and "partial" for cohort-defined age groups is a judgement call that changes the demographic count by several trials.
- **Endpoint families** are regex definitions on the first primary outcome title. The attribute classes in 3.3 are regex classes on about 15-50 trials per family, spot-read only; the HbA1c family was examined on 15 trials because the sample already held 15. Cohort sizes in 3.4 are upper bounds. Combinability of effects within a cohort was not assessed.
- The **15 mismatch readings** and all readings of population descriptions are one reviewer's; the FS rule (first period only) is known to undercount in multi-period designs.
- **Outcome switching** was not assessed at all; only the availability of inputs was counted.
- Registry data were read as posted on 2026-11-13 (sample, counts, documents, family top-up) against a release pulled 2026-11-08; records change, and the first audit found no difference in study type or enrolment over those five days but did not compare results sections.
- First-audit figures not recomputed in this pass (unit, dispersion and estimate-type string tallies, AACT mapping, per-subgroup denominators) stand as the first audit reported them and remain provisional.

## 8. Earlier headline figures and their refined wording or value

| # | First-audit headline | First-audit value | Refined wording | Refined value | Changed materially? |
|---|---|---|---|---|---|
| 1 | Sampled trials with `hasResults` true | 2,000/2,000 (100.0%) | Not a finding: results are the inclusion rule of the frame. The question "do eligible trials post results?" is answered separately at count level. | frame = 80,890 studies with a results-first-posted date; interventional 2015-2023 with results 40,704/196,860 (20.7%) | Wording; new evidence added |
| 2 | All five results modules present | 1,997/2,000 (99.8%) | Module presence, not results completeness. `moreInfoModule` is administrative and is excluded from usable-content counts. | presence unchanged; usable: baseline 1,991, flow 1,995, primary outcome 1,905, adverse events 1,932; all four 1,852/2,000 (92.6%; 95% CI 91.4-93.7) | Yes: 99.8% present vs 92.6% usable on all four |
| 3 | Registered outcome list vs posted measures identical | 4,406/4,406 primary; 10,833/10,833 secondary | One stored list; says nothing about outcome switching, which current records cannot establish. | not recomputed; inputs available: registered after start 649/2,000 (32.5%; 95% CI 30.4-34.5); protocol posted 1,057/2,000 (52.9%; 95% CI 50.7-55.0) | Wording |
| 4 | Trials with at least one PRIMARY measure carrying data | 1,915/2,000 (95.8%) | Usable primary outcome (rule O1: unit, number analysed per group, numeric values). | 1,905/2,000 (95.2%; 95% CI 94.2-96.1) | No (10 fewer trials) |
| 5 | Trials whose primary measure(s) report 2+ groups | 1,448/2,000 (72.4%) | Comparative trials: interventional with a 2+ group primary measure (C1); randomised (C2). | C1 1,394/2,000 (69.7%; 95% CI 67.6-71.7); C2 1,134/2,000 (56.7%; 95% CI 54.5-58.9) | Yes: 54 observational studies removed; randomised denominator added |
| 6 | Trials with a statistical analysis on a primary measure | 621/2,000 (31.1%); 557/1,448 (38.5%); 167/315 Phase 3 (53.0%) | Among comparative randomised trials, a usable between-arm analysis on a primary measure. | C2: 473/1,134 (41.7%; 95% CI 38.9-44.6); C2 Phase 3: 153/250 (61.2%; 95% CI 55.0-67.0); all sampled (any S1): 612/2,000 (30.6%; 95% CI 28.6-32.7) | Denominator changed; values 41.7% and 61.2% on the appropriate denominators |
| 7 | Primary analysis with a p-value | 511/2,000 (25.6%) | Between-arm analysis with a p-value, among C2. | 398/1,134 (35.1%; 95% CI 32.4-37.9) | Denominator changed |
| 8 | Primary analysis with an estimate and both CI limits | 366/2,000 (18.3%) | Between-arm estimate with both confidence limits, among C2. | 310/1,134 (27.3%; 95% CI 24.8-30.0); C2 Phase 3 124/250 (49.6%; 95% CI 43.5-55.8) | Denominator changed |
| 9 | Distinct estimate-type strings | 249 across 2,017 analyses; 669 with none | Lexical variety of a free-text field; within an endpoint family the estimate type is far more uniform (hazard ratio in every PFS/OS analysis naming a type). | not recomputed; see 3.3 | Wording |
| 10 | Trials where a between-arm effect "could be computed" | 1,074/2,000 (53.7%) [inferred] | Withdrawn as a headline. C3 counts randomised trials with a usable 2+ group primary measure, a structural precondition only. | 1,088/2,000 (54.4%; 95% CI 52.2-56.6) | Yes: claim narrowed |
| 11 | Trials with demographic subgroup levels in any measure | 20/2,000 (1.0%); race or ethnicity 5 | Subgroup reporting detected by the current method; then one reviewer's reading. | detected 136/2,000 (6.8%; 95% CI 5.8-8.0); judged true 41/2,000 (2.0%; 95% CI 1.5-2.8), demographic 34/2,000 (1.7%; 95% CI 1.2-2.4); race or ethnicity 6 | Yes: detection 1.0% to 6.8%; confirmed demographic 16 to 34 trials |
| 12 | Same, PRIMARY measures only | 6/2,000 (0.3%); race or ethnicity 0 (+1 by arms) | Judged-true demographic result with the level signal on a PRIMARY measure. | 17/2,000; race or ethnicity 1 (NCT03098836, race-defined arms) | Yes: 6 to 17 trials, from group titles |
| 13 | Race reported AND a primary measure with a statistical analysis | 382/2,000 (19.1%) | Co-occurrence in one record among comparative randomised trials; not an outcome by race. | C2: race reported and between-arm analysis 296/1,134 (26.1%; 95% CI 23.6-28.7); with estimate and limits 206/1,134 (18.2%; 95% CI 16.0-20.5) | Denominator changed |
| 14 | Serious adverse event counts with numbers at risk for every group | 1,993/2,000 (99.7%) | Usable adverse-event table (A1): numbers at risk > 0 in at least one group, rows or explicit zeros. | 1,932/2,000 (96.6%; 95% CI 95.7-97.3); 60 records have zero at risk in every group | Yes: 99.7% to 96.6% |
| 15 | Baseline total equals STARTED / equals enrolment | 1,585/2,000 (79.2%) / 1,448/2,000 (72.4%) | Same values, now with exact fields, a no-double-count Total rule, and reviewed mismatches; baseline is the population the registrant chose to describe. | BT = FS 1,585/2,000 (79.2%; 95% CI 77.4-81.0); BT = EN 1,448/2,000 (72.4%; 95% CI 70.4-74.3); all three equal 1,405/2,000 (70.2%; 95% CI 68.2-72.2) | No (values reproduced) |
| 16 | Primary measure titles used by only one sampled trial | 4,026/4,097 (98.3%) | A lexical finding about title strings; does not establish incompatibility or that oncology is the only viable area. | full release: 63,388/67,634 distinct first-primary titles used once; 14 construct families cover 3,041/10,852 randomised Phase 3 trials | Wording; interpretation reversed |

## 9. Evidence record: sources, versions, files

| Item | Value |
|---|---|
| Source population | `engine-data/demographics.json.gz`, sha256 `2168d258dd26d1c824f4a090b07df7f1dc8ad58d71a6a15598e5115c258c7fa2`, 80,890 records |
| Extraction version | `pipeline_commit` `f6a2e7b74790c6f11982376441fb90c80d2c611e` (2026-11-07), `extracted_at` 2026-11-08T13:07:45.318831+00:00; engine checkout read at `0293cd099575c86b878d5ebe531f4de945f3f80b` |
| Inclusion rule | `AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX]` at pull time; no other filter |
| Sampling | proportional stratified by `study_type`, n = 2,000, seed 20261113; `sample_ids.txt` sha256 `9bd0c403097b055faf01b505fbd0b98a6ca21611d965c8878751f11f1e0d525f` |
| Sample records | first audit cache, sha256 `a9e4577735b5f3ece14ff672a5db0f4ca70be283f237bb996c09328c3569510a`, fetched 2026-11-13T22:32:29Z..2026-11-13T22:33:01Z |
| Registry version | API 2.0.5, `dataTimestamp` 2026-11-13T10:00:06, as reported by `/version` during the first pass (22:33Z) and during this pass (23:21Z) |
| New API responses | 243 requests, 2026-11-13T23:19:50Z..2026-11-13T23:41:15Z, all HTTP 200; manifest `cache/fetch_manifest.jsonl` (URL, time, status, bytes, sha256, purpose), summary `cache/fetch_manifest_summary.json` |
| `cache/count_queries.jsonl.gz` | 192 responses, sha256 `b4b85dfef055d4f85f350c212e3e778ac40f60e83efa7e8fa14819bb7de9b928` |
| `cache/document_section.jsonl.gz` | 40 responses, sha256 `6a6259f8549de62d429f5a294c66677bd4fd1f6625620e5c78a58c3d1e7ebc6b` |
| `cache/family_studies.jsonl.gz` | 11 responses, sha256 `338110bcb686ad3f379217f70c54eb70e1c805f0df70a294f113e1bfa0c33e17` |
| Rate | one request at a time, at least 0.5 s apart (2 per second) |

| File | Contents |
|---|---|
| `report.md` | this report |
| `definitions.json` | every rule: plain-language definition, numerator, denominator, value, source file |
| `sample_ids.txt`, `sampling.json`, `sampling_verification.json` | the 2,000 NCT IDs, sampling parameters, and the re-draw check |
| `release_meta.json`, `release_slim.jsonl.gz` | release stamps and key inventory; slim line-oriented copy of the release fields used |
| `results_posting_counts.json` | section 1.3 counts, expressions, consistency checks, frame checks |
| `usable.json`, `per_trial_usable.jsonl` | section 2: aggregates and every flag for every sampled trial |
| `families.json`, `family_members.json`, `family_examined.json`, `family_detail.json`, `family_detail_rows.jsonl`, `cohorts.json` | section 3 |
| `subgroup_detected.jsonl`, `subgroup_negative_samples.json`, `subgroup_review.csv`, `subgroup_summary.json`, `review/` | section 4: all signals, the negative samples, all 236 reviewed trials with judgements, the digests read and the judgement tables |
| `denominators.json`, `per_trial_denominators.jsonl` | section 5.1-5.2 |
| `history_evidence.json`, `per_trial_history_evidence.jsonl` | section 5.3 |
| `cache/` | cached API responses (jsonl.gz) and fetch manifest |
| `scripts/` | all code; data are kept outside it |

Re-running offline from the caches, in order (Python run with `-I`; V1 = the first audit directory, OUT = this directory):

```
python3 -I scripts/build_slim.py RELEASE_GZ OUT
python3 -I scripts/verify_sample.py OUT
python3 -I scripts/fetch_counts.py OUT        # served from cache
python3 -I scripts/usable.py V1 OUT
python3 -I scripts/denominators.py V1 OUT
python3 -I scripts/families.py OUT
python3 -I scripts/fetch_family.py V1 OUT     # served from cache
python3 -I scripts/family_detail.py V1 OUT
python3 -I scripts/cohorts.py OUT
python3 -I scripts/subgroup_detect.py V1 OUT
python3 -I scripts/subgroup_review.py V1 OUT  # needs review/judgements_*.tsv
python3 -I scripts/history_evidence.py V1 OUT # served from cache
python3 -I scripts/manifest_summary.py OUT
python3 -I scripts/make_report.py V1 OUT
```

