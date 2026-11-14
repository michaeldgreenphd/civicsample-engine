# What ClinicalTrials.gov results sections contain for trials in the demographics dataset

Read-only audit, run 2026-11-13. Nothing was committed, pushed or edited in any repository; no paid API
or LLM call was made. Everything below is **measured on a seeded sample of 2,000 trials** unless it is
tagged **[inferred]** (my interpretation of a measurement), **[heuristic]** (a rule-based classification
defined in `scripts/analyze.py`) or **[documentation knowledge]** (recalled, not checked against data).
Percentages always carry their denominator; 95% intervals are Wilson intervals.

Output directory: `docs/audits/outcomes_results_structure/first_pass/`

## 0. The numbers that matter most

| # | Finding | Value |
|---|---|---|
| 1 | Sampled trials returned by the API with `hasResults` true | 2,000/2,000 (100.0%) |
| 2 | All five results modules present | 1,997/2,000 (99.8%); the only gap is `adverseEventsModule` in 3 trials |
| 3 | Registered outcome list vs posted outcome measures | identical in title, time frame, description and order for 4,406/4,406 primary and 10,833/10,833 secondary pairs: the current record holds one list, not a prior registration to compare against |
| 4 | Trials with at least one PRIMARY measure that carries data | 1,915/2,000 (95.8%; 95% CI 94.8-96.5) |
| 5 | Trials whose primary measure(s) report 2 or more groups | 1,448/2,000 (72.4%; 95% CI 70.4-74.3) |
| 6 | Trials with at least one statistical analysis on a primary measure | 621/2,000 (31.1%; 95% CI 29.1-33.1); 557/1,448 (38.5%) of trials with a 2+ group primary; 167/315 (53.0%) of Phase 3 |
| 7 | Trials with a primary analysis carrying a p-value | 511/2,000 (25.6%; 95% CI 23.7-27.5) |
| 8 | Trials with a primary analysis carrying an estimate and both CI limits | 366/2,000 (18.3%; 95% CI 16.7-20.1) |
| 9 | Distinct estimate-type strings among primary analyses | 249 distinct non-blank `paramType` strings across 2,017 analyses; 669 analyses (33.2%) have none |
| 10 | Trials where a between-arm effect could be computed from arm-level primary data **[inferred definition, section 3.3]** | 1,074/2,000 (53.7%; 95% CI 51.5-55.9) |
| 11 | Trials with demographic subgroup levels in any outcome measure's class/category titles **[heuristic]** | 20/2,000 (1.0%; 95% CI 0.6-1.5); race or ethnicity: 5/2,000 (0.2%; 95% CI 0.1-0.6) |
| 12 | Same, PRIMARY measures only **[heuristic]** | 6/2,000 (0.3%; 95% CI 0.1-0.7); race or ethnicity: 0/2,000 (0.0%; 95% CI 0.0-0.2) in class/category titles, plus 1 trial whose arms are race-defined cohorts |
| 13 | Race reported in our record AND a primary measure with a statistical analysis | 382/2,000 (19.1%; 95% CI 17.4-20.9) (race reported in 1,155/2,000 (57.8%)) |
| 14 | Serious-adverse-event counts with numbers at risk for every group | 1,993/2,000 (99.7%); all-cause mortality table in 1,267/2,000 (63.4%) (812/831 (97.7%) of results first posted 2021-2026) |
| 15 | Baseline total equals participant-flow STARTED total / equals actual enrollment | 1,585/2,000 (79.2%) / 1,448/2,000 (72.4%) |
| 16 | Primary measure titles used by only one sampled trial | 4,026/4,097 (98.3%) of distinct normalised titles; the 15 most common titles cover 100/2,000 (5.0%) of trials |

## 1. Sample

- **Frame:** `engine-data/demographics.json.gz`, `extracted_at` 2026-11-08T13:07:45.318831+00:00, `pipeline_commit` `f6a2e7b74790`, 80,890 records, all NCT IDs unique (file sha256 `2168d258dd26d1c8…`). The weekly cohort is every study with a results-first-post date (`src/api_client.py`: `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`), so every record has posted results by construction.
- **Strata:** the record's own `study_type`: INTERVENTIONAL 76,153, OBSERVATIONAL 4,737.
- **Allocation:** proportional to stratum size, 2,000 total, largest-remainder rounding. Exact quotas INTERVENTIONAL 1882.878, OBSERVATIONAL 117.122 gave **INTERVENTIONAL 1,883, OBSERVATIONAL 117**.
- **Draw:** NCT IDs sorted ascending within each stratum; one `random.Random(20261113)` generator, strata visited in name order, `rng.sample(sorted_ids, n_h)`. Script: `scripts/draw_sample.py`. Result: `sample_ids.txt` (sorted, sha256 `9bd0c403097b055f…`), parameters in `sampling.json`.
- Because allocation is proportional the sample is self-weighting: unweighted sample proportions estimate proportions over the 80,890 records. The observational stratum is small (n = 117); anything said about observational studies alone is imprecise.

## 2. Fetch

- `GET https://clinicaltrials.gov/api/v2/studies?filter.ids=<50 ids>&pageSize=50&fields=NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule`; 40 requests, one at a time, at least 0.5 s apart (2 requests/second ceiling), retry with backoff on 429/5xx. 0 retries were needed.
- Fetched 2026-11-13T22:32:29Z to 2026-11-13T22:33:01Z; API version 2.0.5, `dataTimestamp` 2026-11-13T10:00:06.
- **2,000 of 2,000 IDs returned**, 0 missing, 0 unexpected. Every response carried a `nextPageToken` although each already contained its whole batch; coverage was verified by ID, not by the token.
- Raw responses (unparsed bodies with request metadata) are in `cache/raw_responses.jsonl.gz` (sha256 `a9e4577735b5f3ec…`); `scripts/analyze.py` reads only the cache, so the audit re-runs offline.
- Frame versus live registry, five days apart: `study_type` disagreed for 0 of 2,000; enrollment count equal for 2,000/2,000 (100.0%).

## 3. What the results sections contain

### 3.1 Module presence (denominator: 2,000 sampled trials)

| Module | n / 2000 | % | 95% CI |
|---|---:|---:|---|
| participantFlowModule | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| baselineCharacteristicsModule | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| outcomeMeasuresModule | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| adverseEventsModule | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| moreInfoModule | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| all five | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| moreInfoModule.limitationsAndCaveats text | 439 / 2,000 | 21.9 | 20.2-23.8 |

"Present" means the key exists and is non-empty. By study type the only difference is the adverse-events module: 1,880/1,883 (99.8%) interventional, 117/117 (100.0%) observational. Module presence is near-universal because the dataset is defined by having posted results; it says nothing about trials without results.

### 3.2 Registered outcomes versus posted outcome measures

Registered outcomes (`protocolSection.outcomesModule`), per trial (denominator 2,000):

| | trials with any | total | median | p75 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| primary | 2,000/2,000 (100.0%) | 4,407 | 1 | 2 | 6 | 96 |
| secondary | 1,579/2,000 (79.0%) | 10,840 | 3 | 7 | 19 | 123 |
| other | 173/2,000 (8.7%) | 610 | 0 | 0 | 2 | 26 |

Posted outcome measures (`resultsSection.outcomeMeasuresModule.outcomeMeasures`), 15,922 in total:

| type | measures | % of 15,922 | trials with any (of 2,000) | median per trial | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| PRIMARY | 4,408 | 27.7 | 2,000/2,000 (100.0%) | 1 | 6 | 96 |
| SECONDARY | 10,842 | 68.1 | 1,579/2,000 (79.0%) | 3 | 19 | 123 |
| OTHER_PRE_SPECIFIED | 610 | 3.8 | 173/2,000 (8.7%) | 0 | 2 | 26 |
| POST_HOC | 62 | 0.4 | 29/2,000 (1.4%) | 0 | 0 | 14 |

Primary measures per trial (posted): 1: 1,249 (62.5%), 2: 354 (17.7%), 3: 154 (7.7%), 4-5: 115 (5.8%), 6-10: 88 (4.4%), 11-20: 28 (1.4%), 21+: 12 (0.6%).

**How often a registered primary has a posted measure: always, and by construction.**

- Count of registered primaries equals count of posted PRIMARY measures in 1,999/2,000 trials; the one exception (NCT01881828) posts the same title twice.
- Compared position by position as raw strings, title, time frame and description are identical for 4,406/4,406 primary pairs, 10,833/10,833 secondary pairs and 610/610 other pairs (time frame and description counts are the same as the title counts in each row); 1 trial's primary list and 1 trial's secondary list differ in length and are left out of the pairwise count.
- **[inferred]** In a record with results the protocol-section outcome list and the results outcome list are one list. The current record therefore cannot show whether the primary outcome changed after registration; that needs the record's version history, which this audit did not fetch. Our dataset's `primary_endpoint` is the first of that list (2,000/2,000 (100.0%) equal to the first registered primary after normalisation).

What does vary is whether the posted primary measure holds data:

- 4,208/4,408 (95.5%) of PRIMARY measures carry at least one value. The 200 that do not: POSTED, every arm has 0 analyzed: 183; NOT_POSTED (no table yet): 17.
- 133/2,000 (6.7%; 95% CI 5.6-7.8) of trials have at least one PRIMARY measure without data; 85/2,000 (4.2%; 95% CI 3.5-5.2) have no PRIMARY measure with data at all.
- `reportingStatus` across all 15,922 measures: POSTED 15,378 (96.6%), NOT_POSTED 506 (3.2%), (absent) 38 (0.2%).

### 3.3 PRIMARY posted outcome measures

Denominator for measure-level rows: **4,408 PRIMARY measures** in 2,000 trials. A few trials post very many primary measures (maximum 96), so measure-level shares over-represent them; trial-level counts (trials with at least one primary measure of that kind, denominator 2,000; a trial can appear in several rows) are given beside them.

**paramType**

| paramType | measures | % of 4,408 | trials with any | % of 2,000 |
|---|---:|---:|---:|---:|
| MEAN | 1,573 | 35.7 | 740 | 37.0 |
| NUMBER | 1,005 | 22.8 | 649 | 32.5 |
| COUNT_OF_PARTICIPANTS | 853 | 19.4 | 454 | 22.7 |
| GEOMETRIC_MEAN | 300 | 6.8 | 83 | 4.2 |
| MEDIAN | 255 | 5.8 | 168 | 8.4 |
| (absent) | 197 | 4.5 | 130 | 6.5 |
| LEAST_SQUARES_MEAN | 185 | 4.2 | 114 | 5.7 |
| GEOMETRIC_LEAST_SQUARES_MEAN | 20 | 0.5 | 10 | 0.5 |
| COUNT_OF_UNITS | 20 | 0.5 | 13 | 0.7 |

"(absent)" is the measures without a data table (section 3.2).

**unitOfMeasure, top 25** (normalised: lower-case, punctuation to spaces; 887 distinct normalised strings, 1,005 distinct raw strings)

| unit (normalised) | measures | % of 4,408 | | unit, by trials | trials with any | % of 2,000 |
|---|---:|---:|---|---|---:|---:|
| participants | 1,158 | 26.3 | | participants | 651 | 32.5 |
| percentage of participants | 340 | 7.7 | | percentage of participants | 236 | 11.8 |
| score on a scale | 274 | 6.2 | | score on a scale | 151 | 7.5 |
| (absent) | 196 | 4.4 | | (absent) | 129 | 6.5 |
| units on a scale | 192 | 4.4 | | units on a scale | 127 | 6.3 |
| months | 71 | 1.6 | | months | 57 | 2.9 |
| hours | 71 | 1.6 | | ng ml | 39 | 1.9 |
| ng ml | 45 | 1.0 | | days | 35 | 1.8 |
| milligrams per cubic centimeter mg ccm | 45 | 1.0 | | hours | 28 | 1.4 |
| days | 36 | 0.8 | | scores on a scale | 23 | 1.1 |
| pg ml | 31 | 0.7 | | minutes | 21 | 1.1 |
| nmol l | 31 | 0.7 | | percent change | 18 | 0.9 |
| scores on a scale | 30 | 0.7 | | mmhg | 18 | 0.9 |
| ratio | 30 | 0.7 | | percentage | 17 | 0.8 |
| mm hg | 29 | 0.7 | | pg ml | 17 | 0.8 |
| proportion of participants | 28 | 0.6 | | ratio | 16 | 0.8 |
| mmhg | 26 | 0.6 | | mg dl | 16 | 0.8 |
| minutes | 25 | 0.6 | | seconds | 14 | 0.7 |
| ng h ml | 25 | 0.6 | | ng h ml | 14 | 0.7 |
| millimeters mm | 24 | 0.5 | | percentage of patients | 13 | 0.7 |
| z score | 22 | 0.5 | | percentage of subjects | 13 | 0.7 |
| percent change | 20 | 0.5 | | mm | 13 | 0.7 |
| mg dl | 19 | 0.4 | | proportion of participants | 12 | 0.6 |
| h umol l | 18 | 0.4 | | adverse events | 11 | 0.6 |
| log10 copies ml | 17 | 0.4 | | g ml | 9 | 0.5 |

The left and right halves are separate rankings. "milligrams per cubic centimeter" is in the measure-level top 25 but not the trial-level one: it comes from few trials with many measures. Synonyms are not merged ("score on a scale", "units on a scale", "scores on a scale"; "mm hg", "mmhg").

**dispersionType**

| dispersionType | measures | % of 4,408 | trials with any | % of 2,000 |
|---|---:|---:|---:|---:|
| (absent) | 1,824 | 41.4 | 1,025 | 51.2 |
| Standard Deviation | 1,331 | 30.2 | 590 | 29.5 |
| 95% Confidence Interval | 493 | 11.2 | 334 | 16.7 |
| Standard Error | 248 | 5.6 | 153 | 7.7 |
| Geometric Coefficient of Variation | 228 | 5.2 | 46 | 2.3 |
| Full Range | 151 | 3.4 | 92 | 4.6 |
| Inter-Quartile Range | 92 | 2.1 | 59 | 3.0 |
| 90% Confidence Interval | 37 | 0.8 | 23 | 1.1 |
| 97.5% Confidence Interval | 3 | 0.1 | 2 | 0.1 |
| 80% Confidence Interval | 1 | 0.0 | 1 | 0.1 |

**Groups (arms), classes and categories**

| | distribution (measures, % of 4,408) |
|---|---|
| groups per measure | 0: 17 (0.4%); 1: 1,221 (27.7%); 2: 2,016 (45.7%); 3: 545 (12.4%); 4: 276 (6.3%); 5+: 333 (7.6%) |
| classes per measure | 0: 199 (4.5%); 1: 2,990 (67.8%); 2: 512 (11.6%); 3-5: 435 (9.9%); 6-10: 165 (3.7%); 11+: 107 (2.4%) |
| most categories in any class | 0: 200 (4.5%); 1: 4,113 (93.3%); 2: 28 (0.6%); 3-5: 58 (1.3%); 6+: 9 (0.2%) |
| table shape | single value per group: 2,920 (66.2%); multiple classes, one category each: 1,194 (27.1%); no classes (no data table): 199 (4.5%); one class, multiple categories: 70 (1.6%); multiple classes and categories: 25 (0.6%) |

- Any class title: 1,219/4,408 (27.7%); any category title: 97/4,408 (2.2%). Classes, not categories, are where rows of a primary result live (time points, components, subgroups).
- Trial level, largest number of groups in any primary measure (denominator 2,000): 1: 552 (27.6%); 2: 951 (47.5%); 3: 242 (12.1%); 4: 131 (6.5%); 5+: 124 (6.2%). **1,448/2,000 (72.4%; 95% CI 70.4-74.3) have a primary measure with 2 or more groups.**

**Denominators, time frame, status**

| PRIMARY measures | n / 4408 | % | 95% CI |
|---|---:|---:|---|
| measure-level `denoms` present | 4,391 / 4,408 | 99.6 | 99.4-99.8 |
| class-level `denoms` present | 284 / 4,408 | 6.4 | 5.8-7.2 |
| any `denoms` present | 4,391 / 4,408 | 99.6 | 99.4-99.8 |
| a denominator unit other than participants | 131 / 4,408 | 3.0 | 2.5-3.5 |
| `timeFrame` present | 4,406 / 4,408 | 100.0 | 99.8-100.0 |
| `description` present | 4,212 / 4,408 | 95.6 | 94.9-96.1 |
| `populationDescription` present | 3,192 / 4,408 | 72.4 | 71.1-73.7 |
| `anticipatedPostingDate` present | 32 / 4,408 | 0.7 | 0.5-1.0 |

- Denominator units (4,522 measure-level denominator rows): participants 4,391 (97.1%), eyes 39 (0.9%), samples 18 (0.4%), teeth 6 (0.1%), devices 4 (0.1%), implants 3 (0.1%); 61 distinct units in all.
- `reportingStatus` of PRIMARY measures: POSTED 4,387 (99.5%), NOT_POSTED 17 (0.4%), (absent) 4 (0.1%).

**Arm-level effect computability [inferred definition].** A trial counts when a primary measure has 2 or more groups, data, denominators, and either (binary) `COUNT_OF_PARTICIPANTS`, or `NUMBER`/`COUNT_OF_UNITS` with a participant-count unit that is not a percentage; or (continuous) a mean-type `paramType` with an SD, SE or CI dispersion. This is read from metadata only; values were not validated.

| Trials | n / see row | % | 95% CI |
|---|---:|---:|---|
| either kind | 1,074 / 2,000 | 53.7 | 51.5-55.9 |
| binary counts | 451 / 2,000 | 22.6 | 20.8-24.4 |
| continuous mean with dispersion | 705 / 2,000 | 35.2 | 33.2-37.4 |
| either kind, among trials with a 2+ group primary | 1,074 / 1,448 | 74.2 | 71.9-76.4 |

### 3.4 Statistical analyses attached to primary outcomes

**Share of trials with at least one analysis on a PRIMARY measure**

| Denominator group | n / group | % | 95% CI |
|---|---:|---:|---|
| all sampled trials | 621 / 2,000 | 31.1 | 29.1-33.1 |
| trials with a PRIMARY measure carrying data | 621 / 1,915 | 32.4 | 30.4-34.6 |
| trials with a 2+ group primary measure | 557 / 1,448 | 38.5 | 36.0-41.0 |
| trials whose primary measures are single-group only | 64 / 552 | 11.6 | 9.2-14.5 |
| any analysis on any measure type, all sampled trials | 664 / 2,000 | 33.2 | 31.2-35.3 |

Measure level: 1,057/4,408 (24.0%) of PRIMARY measures have an analysis (950/3,170 (30.0%) of those with 2+ groups); by type, PRIMARY 1,057/4,408 (24.0%), SECONDARY 2,092/10,842 (19.3%), OTHER_PRE_SPECIFIED 48/610 (7.9%), POST_HOC 33/62 (53.2%). Measures that have any carry a median of 1 (p95 5, max 55).

By stratum (trials with a primary analysis / trials in stratum; phase and sponsor class are our record's fields):

| stratum | with primary analysis | 2+ group primary |
|---|---:|---:|
| study type INTERVENTIONAL | 600/1,883 (31.9%) | 1,394/1,883 (74.0%) |
| study type OBSERVATIONAL | 21/117 (17.9%) | 54/117 (46.2%) |
| phase (blank) | 22/120 (18.3%) | 55/120 (45.8%) |
| phase EARLY_PHASE1 | 4/16 (25.0%) | 10/16 (62.5%) |
| phase NA | 161/554 (29.1%) | 394/554 (71.1%) |
| phase PHASE1 | 50/151 (33.1%) | 132/151 (87.4%) |
| phase PHASE1, PHASE2 | 12/109 (11.0%) | 73/109 (67.0%) |
| phase PHASE2 | 131/509 (25.7%) | 326/509 (64.0%) |
| phase PHASE2, PHASE3 | 12/26 (46.2%) | 24/26 (92.3%) |
| phase PHASE3 | 167/315 (53.0%) | 275/315 (87.3%) |
| phase PHASE4 | 62/200 (31.0%) | 159/200 (79.5%) |
| sponsor class INDUSTRY | 360/918 (39.2%) | 693/918 (75.5%) |
| sponsor class NIH | 13/68 (19.1%) | 50/68 (73.5%) |
| sponsor class OTHER | 224/952 (23.5%) | 653/952 (68.6%) |
| sponsor class other classes | 24/62 (38.7%) | 52/62 (83.9%) |
| results first posted 2008-2012 | 71/179 (39.7%) | 140/179 (78.2%) |
| results first posted 2013-2016 | 128/389 (32.9%) | 293/389 (75.3%) |
| results first posted 2017-2020 | 199/601 (33.1%) | 421/601 (70.0%) |
| results first posted 2021-2026 | 223/831 (26.8%) | 594/831 (71.5%) |

Phase "(blank)" is mostly the observational stratum. The share with a primary analysis is lower among recently posted results (26.8% for 2021-2026 against 39.7% for 2008-2012); this is unadjusted for phase and sponsor mix.

**Field presence** (denominator: 2,017 analyses attached to PRIMARY measures)

| Field | n / 2017 | % | 95% CI |
|---|---:|---:|---|
| pValue | 1,569 / 2,017 | 77.8 | 75.9-79.5 |
| statisticalMethod | 1,640 / 2,017 | 81.3 | 79.5-83.0 |
| paramType | 1,348 / 2,017 | 66.8 | 64.7-68.9 |
| paramValue | 1,344 / 2,017 | 66.6 | 64.5-68.7 |
| ciPctValue | 1,200 / 2,017 | 59.5 | 57.3-61.6 |
| ciLowerLimit | 1,127 / 2,017 | 55.9 | 53.7-58.0 |
| ciUpperLimit | 1,123 / 2,017 | 55.7 | 53.5-57.8 |
| ciNumSides | 1,286 / 2,017 | 63.8 | 61.6-65.8 |
| dispersionType | 333 / 2,017 | 16.5 | 15.0-18.2 |
| nonInferiorityType | 2,017 / 2,017 | 100.0 | 99.8-100.0 |
| testedNonInferiority | 483 / 2,017 | 23.9 | 22.1-25.9 |
| groupDescription | 1,067 / 2,017 | 52.9 | 50.7-55.1 |
| pValueComment | 514 / 2,017 | 25.5 | 23.6-27.4 |
| estimateComment | 439 / 2,017 | 21.8 | 20.0-23.6 |
| nonInferiorityComment | 525 / 2,017 | 26.0 | 24.2-28.0 |
| estimate (paramType and paramValue) | 1,344 / 2,017 | 66.6 | 64.5-68.7 |
| estimate with both CI limits | 1,114 / 2,017 | 55.2 | 53.1-57.4 |
| p-value but no estimate | 669 / 2,017 | 33.2 | 31.1-35.3 |
| neither p-value nor estimate | 4 / 2,017 | 0.2 | 0.1-0.5 |

- p-value form: plain number 1,001 (49.6%), upper bound (<, <=) 452 (22.4%), (absent) 448 (22.2%), lower bound or = (>, >=, =) 116 (5.8%). Of the 1,569 p-values present, 452 (28.8%) are upper bounds such as "<0.001" and 568 (36.2%) carry some operator rather than being a plain number.
- `nonInferiorityType`: SUPERIORITY_OR_OTHER 658 (32.6%), SUPERIORITY 650 (32.2%), OTHER 355 (17.6%), EQUIVALENCE 135 (6.7%), NON_INFERIORITY_OR_EQUIVALENCE 101 (5.0%), SUPERIORITY_OR_OTHER_LEGACY 55 (2.7%), NON_INFERIORITY 42 (2.1%), NON_INFERIORITY_OR_EQUIVALENCE_LEGACY 21 (1.0%).
- `ciPctValue`: 95 921 (45.7%), (absent) 817 (40.5%), 90 239 (11.8%), 80 11 (0.5%); `ciNumSides`: TWO_SIDED 1,263 (62.6%), (absent) 731 (36.2%), ONE_SIDED 23 (1.1%).
- Groups compared per analysis (`groupIds`): 1 group: 423 (21.0%), 2 groups: 1,513 (75.0%), 3 groups: 57 (2.8%), 4+ groups: 24 (1.2%). One-group analyses are within-group tests or estimates, not between-arm comparisons.

Trial level, among the 621 trials with a primary analysis:

| At least one primary analysis with | n / 621 | % | 95% CI |
|---|---:|---:|---|
| a p-value | 511 / 621 | 82.3 | 79.1-85.1 |
| a statistical method | 539 / 621 | 86.8 | 83.9-89.2 |
| an estimate | 410 / 621 | 66.0 | 62.2-69.6 |
| an estimate with both CI limits | 366 / 621 | 58.9 | 55.0-62.7 |
| type exactly SUPERIORITY | 232 / 621 | 37.4 | 33.6-41.2 |
| a non-inferiority or equivalence type | 99 / 621 | 15.9 | 13.3-19.0 |

**Top 20 estimate types** (`analyses[].paramType`, raw strings, denominator 2,017; 249 distinct non-blank strings, 214 after case and punctuation folding)

| estimate paramType | n | % of 2,017 |
|---|---:|---:|
| Mean Difference (Final Values) | 181 | 9.0 |
| LS Mean Difference | 144 | 7.1 |
| Odds Ratio (OR) | 89 | 4.4 |
| Mean Difference (Net) | 78 | 3.9 |
| Hazard Ratio (HR) | 64 | 3.2 |
| Difference in percentage of participants | 40 | 2.0 |
| Ratio | 37 | 1.8 |
| Difference in Percent | 32 | 1.6 |
| Slope | 19 | 0.9 |
| Correlation Contingency Coefficient | 17 | 0.8 |
| Geometric Mean Ratio | 15 | 0.7 |
| Risk Difference (RD) | 15 | 0.7 |
| GLS Mean Ratio | 14 | 0.7 |
| Difference | 13 | 0.6 |
| Adjusted Mean Difference | 13 | 0.6 |
| LSM difference | 13 | 0.6 |
| Ratio of adjusted geometric means | 12 | 0.6 |
| Hazard Ratio, log | 12 | 0.6 |
| Difference in percentage | 11 | 0.5 |
| Median Difference (Final Values) | 11 | 0.5 |

**[inferred]** The strings look like a pick-list plus free text: one concept appears under many strings ("LS Mean Difference", "LSM difference", "Least Squares Mean Difference"; "Geometric Mean Ratio", "GMT Ratio", "GLS Mean Ratio", "Ratio of adjusted geometric means").

**Top statistical methods** (same denominator): Mixed Models Analysis 308 (15.3%), ANCOVA 141 (7.0%), t-test, 2 sided 141 (7.0%), ANOVA 139 (6.9%), Regression, Linear 79 (3.9%), Fisher Exact 70 (3.5%), Spearman Correlation Coefficient 67 (3.3%), Log Rank 64 (3.2%), Chi-squared 63 (3.1%), Pearson Correlation Coefficient 56 (2.8%), Regression, Logistic 53 (2.6%), Cochran-Mantel-Haenszel 38 (1.9%), Wilcoxon (Mann-Whitney) 36 (1.8%), t-test, 1 sided 34 (1.7%), Miettinen & Nurminen 32 (1.6%); 132 distinct strings.

### 3.5 Demographic subgroup reporting inside outcome measures [heuristic]

Rule, applied to every one of the 15,922 posted measures (full definition in `scripts/analyze.py`, section 3e):

- **Levels (Tier 1):** the measure's class titles and category titles themselves show two or more levels of a dimension: a female-term title and a different male-term title; two or more race levels (OMB categories, or X / non-X); a Hispanic/Latino title among two or more level titles; two or more age-level titles (explicit age wording, or year/month bands when the measure title says "age", or different life stages such as children / adults). A measure titled "subgroup…" needs only one such level. Donor attributes, "white blood cell", "Child-Pugh", instrument names and bare time points are excluded.
- **Title cue only (Tier 2):** the measure title says "by sex", "by race", "age group", "subgroup analysis: sex" and the like, without Tier 1 levels.
- **Arm titles (supplementary):** the same level rule applied to the measure's group (arm) titles. The request named titles, classes and categories; arms are reported separately because age- or sex-defined cohorts are the commonest way these dimensions appear.
- **Mention only:** any sex, race, ethnicity or age-group word anywhere in a title or level title. Not subgroup reporting; shown as a loose ceiling.

Trials with at least one such measure (denominator 2,000 in every cell):

| | sex | race | ethnicity | age | any dimension | race or ethnicity |
|---|---:|---:|---:|---:|---:|---:|
| **Any measure:** levels (Tier 1) | 10/2,000 (0.5%) | 5/2,000 (0.2%) | 2/2,000 (0.1%) | 13/2,000 (0.7%) | 20/2,000 (1.0%; 95% CI 0.6-1.5) | 5/2,000 (0.2%; 95% CI 0.1-0.6) |
| Any measure: levels or title cue | 13/2,000 (0.7%) | 7/2,000 (0.3%) | 2/2,000 (0.1%) | 15/2,000 (0.8%) | 23/2,000 (1.1%; 95% CI 0.8-1.7) | |
| Any measure: arm titles (supplementary) | 6/2,000 (0.3%) | 1/2,000 (0.1%) | 0/2,000 (0.0%) | 9/2,000 (0.5%) | 15/2,000 (0.8%) | |
| Any measure: levels, title cue or arm titles | | | | | 35/2,000 (1.8%; 95% CI 1.3-2.4) | 8/2,000 (0.4%; 95% CI 0.2-0.8) |
| Any measure: mention only (ceiling) | 36/2,000 (1.8%) | 9/2,000 (0.5%) | 2/2,000 (0.1%) | 96/2,000 (4.8%) | 120/2,000 (6.0%) | |
| **PRIMARY measures:** levels (Tier 1) | 3/2,000 (0.1%) | 0/2,000 (0.0%) | 0/2,000 (0.0%) | 4/2,000 (0.2%) | 6/2,000 (0.3%; 95% CI 0.1-0.7) | 0/2,000 (0.0%; 95% CI 0.0-0.2) |
| PRIMARY: levels or title cue | 4/2,000 (0.2%) | 0/2,000 (0.0%) | 0/2,000 (0.0%) | 5/2,000 (0.2%) | 7/2,000 (0.3%; 95% CI 0.2-0.7) | |
| PRIMARY: arm titles (supplementary) | 5/2,000 (0.2%) | 1/2,000 (0.1%) | 0/2,000 (0.0%) | 8/2,000 (0.4%) | 13/2,000 (0.7%) | |
| PRIMARY: levels, title cue or arm titles | | | | | 19/2,000 (0.9%; 95% CI 0.6-1.5) | |
| PRIMARY: mention only (ceiling) | 16/2,000 (0.8%) | 2/2,000 (0.1%) | 0/2,000 (0.0%) | 44/2,000 (2.2%) | 57/2,000 (2.9%) | |

Measure level: 55 of 15,922 measures meet Tier 1 (8 of them PRIMARY; by dimension, a measure can count in several: age 42, ethnicity 2, race 13, sex 18); 12 more have a title cue only. By study type, Tier 1 trials: INTERVENTIONAL 17/1,883 (0.9%), OBSERVATIONAL 3/117 (2.6%).

**Subgroup denominators** (denominator: the 55 Tier 1 measures):

| Tier 1 measures | n / 55 | % | 95% CI |
|---|---:|---:|---|
| demographic levels are classes (rather than categories) | 53 / 55 | 96.4 | 87.7-99.0 |
| every demographic-level class has its own `denoms` | 39 / 55 | 70.9 | 57.9-81.2 |
| those class denominators differ between levels | 38 / 55 | 69.1 | 56.0-79.7 |
| levels are categories (the schema has no per-category denominator) | 2 / 55 | 3.6 | 1.0-12.3 |
| measure-level `denoms` present | 55 / 55 | 100.0 | 93.5-100.0 |

So per-subgroup denominators are given in 70.9% of subgroup measures; in the rest the subgroup n is either absent or typed into the class title as text ("Male Participants (n=70, 81)"). 6/20 (30.0%) of Tier 1 trials attach a statistical analysis to a flagged measure.

**Every flagged trial** (Tier 1 or title cue; 23 trials, one representative measure each, PRIMARY preferred; the full list of 67 flagged measures is in `subgroup_hits.jsonl`). "Reading" is my judgement of whether the measure is an observed outcome reported by participant subgroup: yes / partly / no.

| # | NCT ID | type | measure title | class / category titles (first 6) | dimensions | per-class denoms | reading |
|---:|---|---|---|---|---|---|---|
| 1 | NCT00261443 | SECONDARY | Treatment-Emergent AEs in &gt;=5% of Participants During Phase 3, by Age, Gender, Race, and Maximum Intensity | Participants &lt;= 50 Years (n=132, 131); Participants &gt;50 Years (n=34, 36); White Participants (n=63, 74); Non-White Participants (n=42, 31); Female Participants (n=96, 86); Male Participants (n=70, 81) | age, race, sex | no | **yes**: AE counts by age, sex and race (White / Non-White); subgroup n only as text inside the class titles |
| 2 | NCT00461305 | POST_HOC | Change in Total Dysmenorrhea Score at Final Evaluation in Subgroups (1): From Baseline to Cycle 6 | age: &lt; 30 years; age: &gt;= 30 years | age | no | **yes**: post-hoc efficacy by age (&lt;30 / &gt;=30) alongside non-demographic subgroups |
| 3 | NCT00980005 | SECONDARY | Geometric Mean of Haemagglutination Inhibiting (HI) Antibodies Titers Against the Three Strains, by Age-strata. | A/Brisbane at Day 0 [3-4 years]; A/Brisbane at Day 28 [3-4 years]; A/Brisbane at Day 0 [5-8 years]; A/Brisbane at Day 28 [5-8 years]; A/Brisbane at Day 0 [9-17 years]; A/Brisbane at Day 28 [9-17 years] | age | yes, differing | **yes**: immunogenicity by age stratum, with per-class denominators |
| 4 | NCT01060059 | SECONDARY | Factors of Gender, Baseline Presence of Medical Conditions, and Previous Gastrointestinal Symptoms Associated With Trea… | Female Gender; Male Gender | sex | no | **partly**: counts by sex as a "factor associated with treatment choice"; not an efficacy result |
| 5 | NCT01256658 | SECONDARY | Hemoglobin Level (g/dL) in Community Screening Campaign 1 (CSC1)/Day 1 and CSC4/Day 1 by Study Arm and Age Group (Per C… | CSC1 5- 9 years (n= 899, 431); CSC4 5 - 9 years (n= 900, 396); CSC1 10 - 14 years (n= 873, 380); CSC4 10 - 14 years (n= 837, 359); CSC1 &gt;= 15 years (n= 2904, 1279); CSC4 &gt;= 15 years (n= 2760, 1136) | age | no | **yes**: haemoglobin by age group; subgroup n only as text inside the class titles |
| 6 | NCT02165826 | SECONDARY | Median Simulated Absolute Change From Baseline in FEV1 at Weeks 4 and 12 | Age=40 years (tPDE4i=0.675,0.675); Age=60 years (tPDE4i=0.839,0.839); Age=92 years (tPDE4i=1.06,1.06); Asian (tPDE4i=0.839,0.839); non-Asian (tPDE4i=0.839,0.839); Female (tPDE4i=0.937,0.937) | age, race, sex | no | **no**: population-PK / simulated values at covariate levels (Age=40, Sex=female, Asian / non-Asian): model output, not observed subgroup results |
| 7 | NCT02246439 | PRIMARY | Treatment Success From 30 Minutes Through 24 Hours After First Dose of Study Medication - ITT Population | &lt;18 years of age; 18 years of age or older | age | yes, differing | **yes**: primary efficacy outcome by age (&lt;18 / &gt;=18) with per-class denominators |
| 8 | NCT02304406 | SECONDARY | Percentage of Participants With Association of Anaplastic Lymphoma Kinase (ALK) Rearrangement by Gender | Female; Male | sex (title cue: race) | yes, differing | **yes**: biomarker prevalence by gender (classes) and by race (title; classes White/Caucasian, Arabic, Others) |
| 9 | NCT02588664 | OTHER_PRE_SPECIFIED | Subgroup Analysis: Race | (none) | - (title cue: age, race, sex) | n/a | **no**: three measures titled "Subgroup Analysis: Race / Sex / Age" with reportingStatus POSTED but no data table |
| 10 | NCT02688764 | POST_HOC | Change in Serum Phosphorus Level From Baseline to End of Stage 1 in PA21 Group, by Age Group | &lt;2 years; &gt;=2 years to &lt;6 years; &gt;=6 years to &lt;12 years; &gt;=12 years to &lt;=18 years | age | yes, differing | **yes**: post-hoc change in serum phosphorus by age group, single arm |
| 11 | NCT03500094 | PRIMARY | Pharmacokinetics (PK): Area Under The Plasma Concentration-time Curve Over The Dosing Interval (AUCtau) Of Migalastat | 12 to &lt;16 years old; 16 to &lt;18 years old; 12 to &lt;18 years old | age | yes, differing | **yes**: primary PK parameters by age band, single arm |
| 12 | NCT03722810 | SECONDARY | The Effect of Students' Gender on Their Levels of Patient-centredness as Assessed by the PPOS-D12 Questionnaire | (none) | - (title cue: sex) | n/a | **partly**: title cue only; the arms are Female / Male students, so sex is the study design, not a subgroup of a trial |
| 13 | NCT03993119 | PRIMARY | Type of Current Non-vitamin K Antagonist Oral Anticoagulant (NOAC) and Current NOAC Dose According to Sex | Current NOAC: Dabigatran (110 mg BID …; Current NOAC: Rivaroxaban (15 mg QD a…; Current NOAC: Apixaban (2.5 mg BID an…; Current NOAC: Edoxaban (30 mg QD and …; Dabigatran 110 mg BID; Dabigatran 150 mg BID | - (title cue: age, sex) | n/a | **partly**: observational; title cue only; arms are "Sex: Male/Female" and age bands; descriptive treatment patterns |
| 14 | NCT04024228 | PRIMARY | Geometric Mean Titers (GMTs) of Influenza Antibodies in Participants Aged 60-64 Years and Greater Than or Equal to (&gt;=)… | A/H1N1: Day 28: 60-64 years; A/H3N2: Day 28: 60-64 years; B1: Day 28: 60-64 years; B2: Day 28: 60-64 years; A/H1N1: Day 28: &gt;= 65 years; A/H3N2: Day 28: &gt;=65 years | age (title cue: age) | yes, differing | **yes**: primary immunogenicity outcome by age stratum (60-64, &gt;=65) |
| 15 | NCT04105244 | PRIMARY | Positive Health Practices | Female Baseline- Time 1; Female 6 weeks- Time 2; Female 3 months- Time 3; Female 6 months- Time 4; Female 9 months- Time 5; Male Baseline- Time 1 | sex | yes, differing | **yes**: primary outcome by sex x time point, with per-class denominators |
| 16 | NCT04259762 | PRIMARY | Aim 8, INT, Pilot Test: Age/Gender-specific Breast, Colorectal, and Cervical Cancer Screening | Stool Test, Men, 45-75 years (Aim 8, …; Stool Test, Women, 45-75 years (Aim 8…; Colonoscopy, Men, 45-75 years (Aim 8,…; Colonoscopy, Women, 45-75 years (Aim …; Mammogram, Women, 45-75 years (Aim 8,…; Pap Smear, Women, 45-75 years (Aim 8,… | age, sex | yes, differing | **partly**: sex- and age-specific screening tests (eligibility-defined rows), not a subgroup contrast of one outcome |
| 17 | NCT04323137 | POST_HOC | Flu Vaccination Rate by Race | American Indian or Alaska Native; Asian; Black or African American; Native Hawaiian or Other Pacific Isla…; White | ethnicity, race, sex | yes, differing | **yes**: post-hoc vaccination rate by race (five OMB categories), ethnicity and gender |
| 18 | NCT04354155 | SECONDARY | Median Twice-daily Enoxaparin Dose Required to Achieve 4-hour Post-dose Anti-factor Xa Between 0.20-0.49 U/mL | Children 12 years or older; Children less than 12 years | age | yes, identical | **yes**: dose requirement by age group (children &lt;12 / &gt;=12 years) |
| 19 | NCT04587167 | SECONDARY | HPV Vaccination (≥1 Dose), 11-14 Year Olds at 12 Months by Sex | Female - Initiated HPV vaccine; Female - Not initiated; Male - Initiated HPV vaccine; Male - Not initiated | sex | no (levels are categories) | **yes**: vaccination by sex; levels are categories, so no per-level denominators |
| 20 | NCT04907253 | PRIMARY | Quercetin-associated Change in Surgery-associated Inflammation | Baseline Women; t1 women; t4 women; Baseline men; t1 men; t4 men | sex | yes, differing | **yes**: primary biomarker outcome by sex x time point |
| 21 | NCT05009251 | OTHER_PRE_SPECIFIED | Flu Vaccination at 2 Weeks After Final Outreach Date by Gender | Female; Male | ethnicity, race, sex | yes, differing | **yes**: vaccination rate by gender, race and ethnicity (other pre-specified) |
| 22 | NCT05329194 | SECONDARY | AAER for Asthma Exacerbations (Subgroups of Participants) | Adolescents Study Period; Adolescents Baseline Period; Black or African American Study Period | age, race | yes, differing | **yes**: exacerbation rate in listed subgroups, including adolescents and Black or African American |
| 23 | NCT05838664 | OTHER_PRE_SPECIFIED | Number of Participants According to Subgroups in OAC Exposed and Unexposed Participants | Elderly (&gt;=80 years); Non-elderly (18 to &lt;80 years) | age | no | **partly**: number of participants in each subgroup (elderly / non-elderly ...): subgroup sizes, not an outcome by subgroup |

My reading of the 23: 16 yes, 5 partly, 2 no. Counting only "yes", 16/2,000 (0.8%) of sampled trials post an observed outcome by a demographic subgroup in any measure; 5/2,000 (0.25%) do so by race or ethnicity (NCT00261443, NCT02304406, NCT04323137, NCT05009251, NCT05329194); 5/2,000 have a flagged PRIMARY measure (NCT02246439, NCT03500094, NCT04024228, NCT04105244, NCT04907253), none of them by race or ethnicity. This reading is one reviewer's (the auditing model's) and is not a second measurement.

**Trials flagged only through arm titles** (12 trials; the arms themselves are sex-, age- or race-defined cohorts)

| # | NCT ID | type | measure title | group (arm) titles (first 4) | dimension | measures with such arms |
|---:|---|---|---|---|---|---:|
| 24 | NCT00119067 | PRIMARY | Local AEs | SQ Females; IM Female; SQ Males; IM Male | sex | 2 |
| 25 | NCT00666276 | PRIMARY | Factors Considered to Affect the Safety of Linezolid - Gender. | Linezolid-male; Linezolid-female | sex | 1 |
| 26 | NCT01053663 | PRIMARY | Area Under the Concentration Versus Time Curve From Time Zero to Last Measurable Plasma Concentrati… | Oseltamivir: Age 91 to &lt; 365 Days; Oseltamivir: Age 31 to 90 Days; Oseltamivir: Age 0 to 30 Days | age | 11 |
| 27 | NCT01068262 | PRIMARY | Weighted Average Inhibition (WAI) of Urine Aminoterminal Crosslinked Telopeptide of Type I Collagen… | Odanacatib (MK-0822) in Males (Panel A); Placebo in Males (Panel A); Odnacatib (MK-0822) in Postmenopausal Fem…; Placebo in Postmenopausal Females (Panel … | sex | 3 |
| 28 | NCT01183468 | PRIMARY | C-peptide 2-hour AUC in Response to a Mixed-meal Tolerance Test at Week 52 | Part 1a(Aralast NP)-Subjects Aged 16-35 Y…; Part 1a (Aralast NP)-Subjects 8-15 Yrs; Part 1b (Aralast NP)-Subjects Aged 18-35 …; Part 1b (Aralast NP)-Subjects 8-17 Yrs | age | 1 |
| 29 | NCT01651949 | PRIMARY | Geometric Mean Titers (GMTs) to the HPV Types Contained in the 9vHPV Vaccine | Heterosexual Males; Females; Men Who Have Sex With Men | sex | 6 |
| 30 | NCT02415244 | PRIMARY | Number of Thoracic Spinal Segments Successfully Visualized With TEE | Pediatric Group; Adult Group | age | 1 |
| 31 | NCT02576678 | PRIMARY | Number of Participants With Treatment Emergent Adverse Events (TEAEs) | Group 1 Adolescents: Apremilast 20 mg; Group 1 Adolescents: Apremilast 30 mg; Group 2 Children: Apremilast 20 mg | age | 9 |
| 32 | NCT03098836 | PRIMARY | Percentage of Participants With 2-Year Progression-Free Survival (PFS) | Caucasian; African American | race | 8 |
| 33 | NCT05086315 | PRIMARY | Dose Escalation: Number of Participants With Dose Limiting Toxicities (DLTs) | Dose Escalation - Adult Arm: SAR443579 10…; Dose Escalation - Adult Arm: SAR443579 30…; Dose Escalation - Adult Arm: SAR443579 75…; Dose Escalation - Adult Arm: SAR443579 80… | age | 19 |
| 34 | NCT05093829 | PRIMARY | Number of Participants With Seroprotection for Meningitis Serogroups A, C, W and Y | Vaccination at 9 Months of Age With NmCV-5; Vaccination at 9 Months of Age With MenAC…; Vaccination at 15 Months of Age With NmCV…; Vaccination at 15 Months of Age With MenA… | age | 13 |
| 35 | NCT06815562 | PRIMARY | Mean Difference in Average Domain Z-scores Across Raters Within Two Weeks on the Clinical Scale of … | Experimental, Child Sample; Control, Child Sample; Experimental, Adolescent Sample; Control, Adolescent Sample | age | 2 |

One of these (NCT03098836) has race-defined cohorts ("Caucasian", "African American"); it is the only sampled trial the rule found whose arms are race groups, and so the only one reporting its primary outcome by race.

**What the rule leaves out: 15 "mention only" measures** (seeded draw, one per trial, from 384 unflagged measures that mention a demographic word)

| NCT ID | type | mention | measure title | first level titles |
|---|---|---|---|---|
| NCT01237587 | SECONDARY | age | Change From Baseline to 39 Week Endpoint in Brief Pain Inventory (BPI) Modified Short Form-adolescent Version… | Worst Pain; Least Pain; Pain Right Now |
| NCT02906202 | SECONDARY | age | Change From Baseline in EuroQol Quality of Life 5-Dimension Adult Scale (EQ-5D-3L) VAS Heath Status Score at … | Health state: Change at Month…; Health state: Change at Month… |
| NCT04888377 | SECONDARY | age | Ages and Stages Questionnaire, 3rd Edition (ASQ-3) Problem Solving Total Score | (none) |
| NCT04609319 | PRIMARY | race | Number of Participants Classified According to Smoking Status: All Asian Participants | Current smoker; Former smoker; Never smoker |
| NCT02688764 | SECONDARY | age | Participants With Serum Phosphorus Levels Within the Age-dependent Target Range in Each Stage | Baseline; End of Stage 1; End of Stage 2 |
| NCT04251208 | SECONDARY | age | Self-reported Retention of Child Custody Retention - Yes Responses | (none) |
| NCT01781481 | PRIMARY | age | Likelihood of Being Identified as Having a Mental Health Need on the Pediatric INTERMED Mental/Cognitive Thre… | Mental Health/Cognitive Vulne…; No Mental Health/Cognitive Vu… |
| NCT02343406 | PRIMARY | age | Pediatric Study: Area Under the Concentration-time-curve (AUC) Observed for Unconjugated Cys-mcMMAF | (none) |
| NCT01590810 | PRIMARY | sex | Change From Baseline in TWA0-24hrs cDBP in Male Participants With Mild to Moderate Hypertension Administered … | (none) |
| NCT01027351 | SECONDARY | age | Geometric Mean Concentrations Against Vaccine Antigen 287-953 in Children After Two Catch up Doses of rMenB+O… | (none) |
| NCT02519504 | PRIMARY | age | Wechsler Intelligence Scale for Children IV-Integrated (WISC-IV-Integrated): Digit Span Score | (none) |
| NCT05357144 | SECONDARY | age, sex | Distribution Contraception to Adolescent Girls and Young Women | (none) |
| NCT01113892 | PRIMARY | sex | The Number of Participants Meeting Composite Endpoint of Major Adverse Limb Events (MALE) and Periprocedural … | Major amputation; Major graft reintervention; Procedure-related death |
| NCT02712996 | PRIMARY | age | Assessing Physical Symptoms in Children Using Vyvanse Versus Placebo by Measuring Anxiety on the Revised Chil… | (none) |
| NCT00499616 | PRIMARY | age | Comparison Between Reduce Intensity of Therapy for Patients With Stage 4 Neuroblastoma and Favorable Biologic… | (none) |

These are mostly instrument names and single-sex or single-age study populations, with some acronym and laboratory-term collisions ("MALE", "white blood cell"). Subgroup results can also sit where this audit did not look: outcome descriptions, analysis `groupDescription` text, and posted protocol/SAP/CSR documents.

### 3.6 Adverse events (denominator 2,000 unless stated)

| Item | n / 2000 | % | 95% CI |
|---|---:|---:|---|
| `adverseEventsModule` present | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| `seriousEvents` rows present | 1,053 / 2,000 | 52.6 | 50.5-54.8 |
| `otherEvents` rows present | 1,383 / 2,000 | 69.2 | 67.1-71.1 |
| both | 956 / 2,000 | 47.8 | 45.6-50.0 |
| either | 1,480 / 2,000 | 74.0 | 72.0-75.9 |
| module present, no event rows of either kind | 517 / 2,000 | 25.9 | 24.0-27.8 |
| every group has `seriousNumAtRisk` | 1,993 / 2,000 | 99.7 | 99.3-99.8 |
| every group has `seriousNumAffected` | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| every group has `otherNumAtRisk` | 1,992 / 2,000 | 99.6 | 99.2-99.8 |
| every group has `otherNumAffected` | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| every group has `deathsNumAtRisk` and `deathsNumAffected` (all-cause mortality) | 1,267 / 2,000 | 63.4 | 61.2-65.4 |
| `frequencyThreshold` present | 1,997 / 2,000 | 99.8 | 99.6-99.9 |
| `timeFrame` present | 1,621 / 2,000 | 81.0 | 79.3-82.7 |
| `description` present | 863 / 2,000 | 43.1 | 41.0-45.3 |

- Absence of event rows is a reported zero, not a gap: of the 944 trials with the module but no serious-event rows, 944/944 (100.0%) report 0 participants affected in every group; for other events, 614/614 (100.0%).
- All-cause mortality table by year results were first posted: 2008-2012: 3/179 (1.7%), 2013-2016: 17/389 (4.4%), 2017-2020: 435/601 (72.4%), 2021-2026: 812/831 (97.7%).
- `frequencyThreshold` (percent, for other events): 0: 1,037 (51.9%), 5: 794 (39.7%), 1: 55 (2.8%), 2: 44 (2.2%), 3: 30 (1.5%), 4: 9 (0.5%); 23 distinct values. A threshold of 5 means non-serious events under 5% in every arm are not listed, so other-event counts are not comparable between a threshold-0 and a threshold-5 trial.
- Event groups per trial: 0: 3 (0.1%); 1: 553 (27.6%); 2: 878 (43.9%); 3: 255 (12.8%); 4: 141 (7.0%); 5+: 170 (8.5%).
- Per event-term row: `numAtRisk` in 115,055/115,055 (100.0%) serious rows and 211,325/211,325 (100.0%) other rows; `numAffected` 96.7% and 96.7%; `numEvents` 49.2% and 52.5%.
- Vocabulary per event term (85,061 terms): MedDRA (any version) 67.1%, (absent) 16.0%, CTCAE (any version) 13.6%, other 3.2%, SNOMED CT 0.0%. Assessment: SYSTEMATIC_ASSESSMENT 77.2%, NON_SYSTEMATIC_ASSESSMENT 20.5%, (absent) 2.3%.
- Event terms per trial: serious median 1, p95 86, max 768; other median 5, p95 117, max 738.
- Adverse events are reported by arm only. No adverse-event field carries a demographic breakdown.

### 3.7 Participant flow (denominator 2,000 unless stated)

| Item | n / see row | % | 95% CI |
|---|---:|---:|---|
| module present | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| first period has STARTED | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| first period has COMPLETED | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| every period has STARTED and COMPLETED | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| STARTED total parsable as integers | 2,000 / 2,000 | 100.0 | 99.8-100.0 |
| any milestone beyond STARTED / COMPLETED / NOT COMPLETED | 290 / 2,000 | 14.5 | 13.0-16.1 |
| any NOT COMPLETED count above 0 | 1,547 / 2,000 | 77.3 | 75.5-79.1 |
| any drop-withdraw reasons | 1,241 / 2,000 | 62.0 | 59.9-64.2 |
| drop-withdraw reasons, among trials with non-completers | 1,241 / 1,547 | 80.2 | 78.2-82.1 |
| `recruitmentDetails` text | 756 / 2,000 | 37.8 | 35.7-39.9 |
| `preAssignmentDetails` text | 773 / 2,000 | 38.6 | 36.5-40.8 |
| unit other than participants (`typeUnitsAnalyzed`) | 35 / 2,000 | 1.8 | 1.3-2.4 |

- Periods per trial: 1: 1,728 (86.4%); 2: 164 (8.2%); 3: 67 (3.4%); 4+: 41 (2.0%). Groups per trial: 1: 565 (28.2%); 2: 909 (45.5%); 3: 233 (11.7%); 4: 133 (6.7%); 5+: 160 (8.0%).
- First-period COMPLETED / STARTED (n = 1,995 with STARTED above 0): median 0.912, p25 0.75, p75 0.996, p05 0.0. 103/1,995 (5.2%) report zero completers in the first period (overall status: TERMINATED 56, COMPLETED 42, ACTIVE_NOT_RECRUITING 5), so COMPLETED / STARTED mixes attrition with early termination and with studies whose first period nobody was meant to 'complete'.
- Drop-withdraw reason types, by trials naming them (denominator 2,000; case-folded): withdrawal by subject 733 (36.6%), lost to follow up 531 (26.6%), adverse event 472 (23.6%), death 295 (14.8%), physician decision 224 (11.2%), protocol violation 203 (10.2%), lack of efficacy 134 (6.7%), other 106 (5.3%), pregnancy 45 (2.2%), progressive disease 38 (1.9%). 1,122 distinct reason strings over 4,809 reason rows: a standard list plus free text.
- Flow is by arm and period only; there is no demographic breakdown of who withdrew.

### 3.8 Baseline

**Baseline total.** Taken from the baseline `denoms` row in participants: the group whose title contains "total" where there is one, else the only group, else a last group equal to the sum of the others (a total under another name), else the sum of groups. This is the project's documented selection rule for AACT baselines. Matching only the exact title "Total" and summing otherwise double-counts records whose total group is titled "Total Title" or "Pooled …" (4 trials in this sample, found on a first pass). Method used: group titled Total 1,382 (69.1%), single group 617 (30.9%), last group equals the sum of the others (untitled total) 1 (0.1%); parsable for 2,000/2,000 (100.0%). In 1,382 of 1,382 records with a Total-titled group it is the last, highest-numbered `BG` group, which agrees with the schema fact the project already documents for AACT.

**Baseline total versus participant-flow STARTED** (sum over groups of the first period's STARTED; denominator 2,000): equal 1,585 (79.2%), baseline_smaller 371 (18.6%), baseline_larger 44 (2.2%). Where they differ (n = 415), baseline / STARTED has median 0.954, p25 0.862, p75 0.988, p05 0.519, p95 2.207.

**Baseline total versus actual enrollment.** `enrollmentInfo.type` is ACTUAL for 2,000/2,000. Distribution of baseline_total / enrollment_actual (denominator 2,000):

| baseline_total / enrollment_actual | trials | % |
|---|---:|---:|
| &lt;0.50 | 31 | 1.6 |
| 0.50-&lt;0.80 | 105 | 5.2 |
| 0.80-&lt;0.95 | 162 | 8.1 |
| 0.95-&lt;1.00 | 234 | 11.7 |
| =1.00 | 1,448 | 72.4 |
| &gt;1.00-1.05 | 4 | 0.2 |
| &gt;1.05-1.50 | 9 | 0.5 |
| &gt;1.50 | 7 | 0.3 |

Quantiles: p05 0.75, p25 0.996, median 1.0, p75 1.0, p95 1.0, max 1037.667. The 7 trials above 1.5 are listed below; they are registry-side oddities (a Total larger than enrollment, external or historical comparison groups counted in baseline, enrollment counted in clusters), not parsing failures. Enrollment versus STARTED: equal 1,736 (86.8%), enrollment_larger 237 (11.8%), enrollment_smaller 27 (1.4%) (denominator 2,000).

| NCT ID | baseline total | enrollment (ACTUAL) | flow STARTED | flow unit | baseline groups |
|---|---:|---:|---:|---|---|
| NCT00066703 | 5,320 | 2,672 | 2,672 | participants | T+OFS; E+OFS; Total |
| NCT00667459 | 545 | 280 | 545 | participants | Investigational; Control; Total |
| NCT02221869 | 169 | 106 | 106 | participants | Enrolled Population; Placebo (Efficacy Population); Xyrem (Efficacy Population); Total |
| NCT03771898 | 68 | 36 | 68 | participants | Group A, SHP611; Group B, SHP611; Group C, SHP611; Group F, SHP611; GLIA-MLD Matched External Control for Gr; Total |
| NCT04227340 | 80 | 43 | 43 | participants | Photobiomodulation Therapy (PBM); Historical Matched Controls; Total |
| NCT04289363 | 1,528 | 53 | 1,542 | participants | ED Patients; Patients Identified Via Chart Review; Total |
| NCT04587167 | 31,130 | 30 | 31,265 | Clinics | HPV ECHO; HPV ECHO+; Control; Total |

So the demographic composition in our records describes the enrolled population exactly in 72.4% of trials (baseline total equal to actual enrollment) and, in most of the rest, a subset of those who started (baseline / STARTED median 0.954 where the two differ).

**Baseline `populationDescription`:** present for 669/2,000 (33.5%; 95% CI 31.4-35.5); 640 distinct texts. It is present for 208/415 (50.1%) of trials whose baseline total differs from STARTED and 461/1,585 (29.1%) of those where it is equal. A measure-level population description exists on at least one baseline measure in 293/2,000 (14.7%).

Populations named **[heuristic keyword tags; a text can carry several; denominator 669 descriptions]**:

| tag | descriptions | % |
|---|---:|---:|
| intent-to-treat / ITT / mITT | 78 | 11.7 |
| full analysis set / FAS | 49 | 7.3 |
| safety population / set | 109 | 16.3 |
| all randomized | 174 | 26.0 |
| all enrolled | 142 | 21.2 |
| treated / received &gt;=1 dose | 239 | 35.7 |
| per-protocol / evaluable | 12 | 1.8 |
| completers / completed | 56 | 8.4 |
| withdrew / excluded / lost / missing explained | 66 | 9.9 |
| analyzed / analysis population (generic) | 145 | 21.7 |
| (no tag matched) | 121 | 18.1 |

30 distinct descriptions (seeded draw from the 640 distinct texts; truncated at 300 characters):

1. NCT03216954: Completing subjects.
2. NCT03531073: 300 patients with all follow up analysed
3. NCT02675114: As Treated
4. NCT06351371: Patients who were eligible, started protocol therapy, and had mutation status confirmed at the analysis time were the analyzable patients
5. NCT00369590: 16 patients were anaplastic gliomas and 42 were glioblastomas all underwent central pathologic review
6. NCT02588664: Subsequent events (i.e. secondary stroke events) were excluded within the study period.
7. NCT06681909: All enrolled participants who completed baseline were assessed.
8. NCT05458011: The intent-to-treat (ITT) analysis set included all randomized participants.
9. NCT03722810: As per assignment in Participant Flow.
10. NCT01957579: All patients who received at least 1 dose of MEDI-551.
11. NCT00960401: Age 60 years or younger at time of radiatiion to chest wall. All subjects had radiation at least 5 years prior to enrollment. Patients with clinical symtoms of CAD were excluded.
12. NCT02388724: Randomized set included all randomized participants.
13. NCT03177395: All 24 participants randomised to the study. All participants received the full dose of PP100-01 according to the allocated dosing cohort where randomised to PP100-01 treatment and all participants received as a minimum 12 hours of NAC treatment (loading dose, plus further 10 hours). Total dose w...
14. NCT01769573: The trial completed and reached its goal without needing to enroll into the 10,000 TCID50 group.
15. NCT02394925: All subjects that were dispensed the study lens.
16. NCT02209454: Twenty-six (26) subjects were randomized (14 male and 12 female) for 24 to complete the study. A total of 25 subjects (13 male and 12 female) completed the study. NOTE: Treatments were administered in a crossover manner, with the same population but one healthy subjects being exposed to the Refer...
17. NCT03801369: No participants were enrolled in Arms II - IV.
18. NCT02748356: 6 children with Spina Bifida, 1 child SCI
19. NCT03661528: Measured in the Intent-to-Treat Population (ITT), Extended Population, which included all randomized participants according to the randomized treatment.
20. NCT01537042: The baseline analysis population consisted of the Safety Set (SS), which is all subjects who were randomized and had at least 1 patch applied during the Treatment Period.
21. NCT05229146: 24 dyads (24 mothers of children 5-10 and reported on their n=24 children). Mothers reported demographic data for themselves and their children.
22. NCT04044716: No baseline demographic information was gathered on participants in the Nurses arm, so those participants are not included in this section.
23. NCT01311895: The discrepancy between the number of patients enrolled and randomized and those included in analysis is due to the following.  H2O group: never given IV opioids (2), received ketorolac or additional opioids within the 60 min (7).  1+1 group: missing data (3), didn't receive 2nd dose within 60 mi...
24. NCT01894516: Safety population included all participants who were randomized and received at least one dose of study medication.
25. NCT00780676: Individuals who were positive for more than one predictor were assigned to the marker arm for which they had the highest percentile positivity.
26. NCT03980808: Adults who are Deaf and whose primary language is Americal Sign Language (ASL), some with a diabetes diagnosis and some without. Participants were randomly assigned to either an intervention group or a control group.
27. NCT03932331: The Safety analysis set included all participants who received at least 1 dose of acalabrutinib. A total of 105 unique participants were enrolled in the study.
28. NCT03169894: Safety Analysis Set
29. NCT05903495: Only one participant enrolled in the study; assigned to the DBS-ON Only arm. No additional participants were enrolled.
30. NCT01919801: Intent-to-treat (ITT) population included all randomized participants.

**Baseline demographic measures in the API record** (by measure title, denominator 2,000): age 2,000/2,000 (100.0%), ethnicity (title contains ethnic) 1,092/2,000 (54.6%), race (title contains race) 1,322/2,000 (66.1%), region of enrollment 1,126/2,000 (56.3%), sex/gender 2,000/2,000 (100.0%). Our record's `race.reported` is true for 1,155/2,000 (57.8%); no trial has it true without a race-titled baseline measure in the API (0 of 2,000). The 167 trials with a race-titled measure but `race.reported` false are mostly the dedicated non-collection measure: Race and Ethnicity Not Collected 154, Race/Ethnicity, Customized 10, Race (NIH/OMB) 2, Race/ethnicity 1. "Race and Ethnicity Not Collected" is filed by 154/2,000 (7.7%).

### 3.9 Co-occurrence: race reported x primary measure with a statistical analysis

Rows: `race.reported` in our record. Columns: at least one posted PRIMARY outcome measure with at least one entry in `analyses`. Denominator 2,000.

| | primary analysis: yes | primary analysis: no | row total | % yes in row |
|---|---:|---:|---:|---:|
| race reported: yes | 382 | 773 | 1,155 | 33.1 (CI 30.4-35.8) |
| race reported: no | 239 | 606 | 845 | 28.3 (CI 25.4-31.4) |
| column total | 621 | 1,379 | 2,000 | |

Both conditions hold for **382/2,000 (19.1%; 95% CI 17.4-20.9)** of sampled trials (crude odds ratio 1.25). Scaled to the 80,890 records that is roughly 14,100 to 16,900 trials **[inferred: sample proportion and its interval applied to the frame size]**.

Variants (same construction; "both" is the count meeting both conditions):

| second condition / restriction | both / denominator | % | % among race-reported | % among race-not-reported |
|---|---:|---:|---:|---:|
| primary analysis; interventional only | 374 / 1,883 | 19.9 | 33.6 | 29.4 |
| primary analysis; trials with a 2+ group primary only | 343 / 1,448 | 23.7 | 41.1 | 34.9 |
| primary analysis with estimate and both CI limits | 241 / 2,000 | 12.1 | 20.9 | 14.8 |
| PRIMARY measure carrying data | 1,117 / 2,000 | 55.9 | 96.7 | 94.4 |
| arm-level effect computable [inferred definition, 3.3] | 608 / 2,000 | 30.4 | 52.6 | 55.1 |
| ethnicity reported x primary analysis | 279 / 2,000 | 13.9 | 34.1 (among ethnicity-reported) | 29.0 |
| race and ethnicity both reported x primary analysis | 265 / 2,000 | 13.2 | 34.4 (among both-reported) | 29.0 |

Our record: race reported 1,155/2,000 (57.8%), ethnicity reported 819/2,000 (41.0%), sex reported 1,962/2,000 (98.1%). The association between race reporting and having a primary analysis is weak and unadjusted for phase, sponsor class and posting year, all of which move analysis presence (section 3.4); it should not be read as a relationship.

## 4. AACT mapping

**Everything in this section is [documentation knowledge]** (the AACT schema and data dictionary as I recall them) except where marked "measured" or "read in the repo". AACT was not downloaded and no snapshot is in either repository, so no column name here was checked against data. Verify each against the AACT data dictionary for the snapshot actually used before writing code.

**What the repositories actually read (read in the repo).**

- `clinical-trial-populations/data/geo/geo_rep_2026-06-19_run063/` contains one R script, `geo_reader.R`. It reads the run's own shipped CSVs (`geo_representation_long_2026-06-19.csv`, `d2_display_rule_vocabulary.csv`, `b1_view_definitions.csv`), not AACT. `data/geo/README.md` says each run directory "ships its complete pipeline" (`refresh.R`, `run_all.R`, `build_part*.R`), but those scripts are not in the checked-out run directory, so which AACT tables the pipeline reads could not be confirmed from code.
- The only AACT flat files named anywhere in that directory are `countries.txt` and `facilities.txt` (in `metric_dictionary.csv`); the README adds that both derive from `protocolSection.contactsLocationsModule.locations` plus `derivedSection.miscInfoModule.removedCountries`. The pinned snapshot date is 2026-06-19 (`active_run.json`).
- The engine's sponsor layer (`civicsample-engine/sponsors/`) names `sponsors.txt`. No code in either repository reads an AACT outcome, analysis, milestone or adverse-event table.
- The session's AACT baseline skill records facts verified on the 2026-06-19 snapshot (not re-verified here): the baseline Total is the highest-numbered `BG` group; `baseline_measurements.number_analyzed` is a denominator id, not a count; `studies.txt` has malformed rows that shift columns.

**Module to table map.**

| API v2 path | AACT table(s) | Notes |
|---|---|---|
| `protocolSection.outcomesModule` (primary / secondary / other) | `design_outcomes` (`outcome_type`, `measure`, `time_frame`, `description`) | In a record with results this list is identical to the posted list (measured, section 3.2), so `design_outcomes` and `outcomes` will carry the same titles. |
| `resultsSection.outcomeMeasuresModule.outcomeMeasures[]` | `outcomes` (`outcome_type`, `title`, `description`, `time_frame`, `population`, `units`, `param_type`, `dispersion_type`, `units_analyzed`, anticipated posting date) | One row per measure. |
| `…outcomeMeasures[].groups[]` | `result_groups` with `result_type` = Outcome (`ctgov_group_code` OG000…, `title`, `description`) | `result_groups` also holds Baseline (BG), Participant Flow (FG) and Reported Event (EG) groups; codes are not shared across types. |
| `…classes[].categories[].measurements[]` | `outcome_measurements` (`classification` = class title, `category` = category title, `param_value`, `param_value_num`, `dispersion_value`, `dispersion_lower_limit`, `dispersion_upper_limit`, `explanation_of_na`, `ctgov_group_code`) | The nested class / category hierarchy is flattened to two text columns. |
| `…outcomeMeasures[].denoms[]` and `…classes[].denoms[]` | `outcome_counts` (`scope`, `units`, `count`, `ctgov_group_code`) | Whether class-level denominators are kept, and how `scope` distinguishes them, I could not confirm; class-level denominators matter for subgroup rows (measured: present in 6.4% of primary measures and 70.9% of flagged subgroup measures). |
| `…outcomeMeasures[].analyses[]` | `outcome_analyses` (`non_inferiority_type`, `param_type`, `param_value`, `dispersion_type`, `dispersion_value`, `p_value_modifier`, `p_value`, `ci_n_sides`, `ci_percent`, `ci_lower_limit`, `ci_upper_limit`, `method`, description / comment columns) | AACT splits the p-value into a modifier and a number; the API gives one string (measured: 452 of 1,569 posted primary p-values, 28.8%, are written as an upper bound such as "<0.001"). |
| `…analyses[].groupIds[]` | `outcome_analysis_groups` (`outcome_analysis_id`, `result_group_id`, `ctgov_group_code`) | Needed to tell between-arm from one-group analyses (measured: 21.0% of primary analyses name one group). |
| `resultsSection.baselineCharacteristicsModule.measures[]…` | `baseline_measurements` (`title`, `classification`, `category`, `units`, `param_type`, `param_value`, dispersion columns, `number_analyzed`, `ctgov_group_code`) | `number_analyzed` is not a count (skill). |
| `…baselineCharacteristicsModule.denoms[]` | `baseline_counts` (`units`, `scope`, `count`, `ctgov_group_code`) | The overall baseline N per group. |
| `…baselineCharacteristicsModule.groups[]` | `result_groups`, `result_type` = Baseline | Total = highest-numbered BG code (skill; measured in the API: 1,382 of 1,382 Total-titled groups are last). |
| `…baselineCharacteristicsModule.populationDescription` | a study-level text column (I recall `studies.baseline_population`) | Column name uncertain. |
| `resultsSection.participantFlowModule.periods[].milestones[]` | `milestones` (`period`, `title`, `count`, `ctgov_group_code`, `description`) | STARTED / COMPLETED / NOT COMPLETED are rows by `title`. |
| `…periods[].dropWithdraws[]` | `drop_withdrawals` (`period`, `reason`, `count`, `ctgov_group_code`) | |
| `…participantFlowModule.recruitmentDetails`, `preAssignmentDetails`, `groups[]` | `participant_flows` (text columns); `result_groups`, `result_type` = Participant Flow | |
| `resultsSection.adverseEventsModule.seriousEvents[]` / `otherEvents[]` | `reported_events` (`event_type` serious / other, `organ_system`, `adverse_event_term`, `subjects_affected`, `subjects_at_risk`, `event_count`, `frequency_threshold`, `time_frame`, `vocab`, `assessment`, `ctgov_group_code`) | One row per term per group; large. |
| `…adverseEventsModule.eventGroups[]` totals (serious / other / deaths affected and at risk) | `reported_event_totals` (`event_type`, `classification`, `subjects_affected`, `subjects_at_risk`); `result_groups`, `result_type` = Reported Event | Per-arm totals without summing term rows. |
| `resultsSection.moreInfoModule` | `result_agreements`, `result_contacts`; limitations text in a study-level column | Column for limitations uncertain. |
| `protocolSection.statusModule`, `designModule` | `studies` (dates, `overall_status`, `enrollment`, `enrollment_type`, `phase`, `study_type`), `designs` | `studies.txt` malformed rows (skill). |
| `documentSection.largeDocumentModule` | `provided_documents` (`has_protocol`, `has_sap`, `url`) | What the Industry Outcomes beta reads from the API today. |
| derived conveniences | `calculated_values` (counts of outcomes to measure, `were_results_reported`, `number_of_sae_subjects`, …) | AACT-computed; no API field. |

**What the API has that AACT lacks or blurs.**

- The nested shape itself: which class a category sits in, and which denominator belongs to which class, without reconstruction from text columns.
- `reportingStatus` per measure (measured: 3.2% of all measures are NOT_POSTED shells), `denomUnitsSelected`, `calculatePct`, `typeUnitsAnalyzed`. I could not confirm which of these AACT carries.
- Freshness to the day and a `dataTimestamp`; a targeted fetch of 2,000 studies took 40 requests.
- A stated correspondence to the registry's own field paths; AACT's dictionary leaves many path cells blank (skill).

**What AACT has that the API lacks.**

- The whole registry as flat files for a pinned snapshot date, which is how this project already pins the geography layer; bulk joins across about 80,000 results records without paging.
- Numeric casts beside the raw strings (`param_value_num`, `dispersion_value_num`), the split p-value, and `calculated_values`.
- One `result_groups` table spanning all four result types.

**What neither has.**

- Record history (the originally registered outcomes), which section 3.2 shows is the only place outcome changes could be seen.
- Any link between baseline demographic categories and outcome rows. The only axis that cross-classifies a result is the arm (`ctgov_group_code` / `groupId`); subgroup results exist only where a registrant typed subgroup names into class, category or arm titles (measured: 1.0% of trials by class or category titles, 1.8% counting title cues and arm titles).
- Participant-level data, and the prespecified decision rule (alpha, multiplicity, hierarchy) behind an analysis.

## 5. Comparability of primary endpoints

Denominator: 2,000 trials with at least one titled PRIMARY measure. Titles are normalised by lower-casing and turning punctuation into spaces; no synonym merging.

- 4,097 distinct normalised titles across 4,408 PRIMARY measures; 4,026/4,097 (98.3%) of titles are used by exactly one sampled trial.
- 203/2,000 (10.2%; 95% CI 8.9-11.6) of trials share an exact primary title with any other sampled trial; 126/2,000 (6.3%; 95% CI 5.3-7.5) share title and unit.
- Within our `primary_condition` grouping, 115/2,000 (5.8%; 95% CI 4.8-6.9) share a title with another sampled trial in the same group.

**The 15 most common primary measure titles**

| normalised title | trials | % of 2,000 |
|---|---:|---:|
| progression free survival | 15 | 0.8 |
| overall survival | 13 | 0.7 |
| progression free survival pfs | 11 | 0.6 |
| objective response rate orr | 7 | 0.3 |
| overall response rate orr | 7 | 0.3 |
| number of participants with adverse events | 7 | 0.3 |
| adverse events | 7 | 0.3 |
| overall survival os | 6 | 0.3 |
| number of participants with adverse events aes | 5 | 0.2 |
| number of participants with treatment emergent adverse events teaes | 5 | 0.2 |
| visual acuity | 4 | 0.2 |
| safety and tolerability | 4 | 0.2 |
| number of participants with treatment emergent adverse events teaes and treatment emergent serious adverse events tesaes | 4 | 0.2 |
| overall response rate | 4 | 0.2 |
| visual analog scale | 4 | 0.2 |

Together they cover 100/2,000 (5.0%; 95% CI 4.1-6.0) of trials. Several are the same concept under different strings: progression-free survival and overall survival each appear twice, response rate three times, adverse events five times.

**By `primary_condition`** (our record's grouping)

| group | trials | distinct primary titles | trials sharing a title within group | % | most shared title (trials) | most common unit (% of trials) |
|---|---:|---:|---:|---:|---|---|
| Oncology | 437 | 730 | 91 | 20.8 | progression free survival (14) | participants (42.3) |
| Other | 349 | 911 | 12 | 3.4 | cmax maximum observed concentration (2) | participants (26.4) |
| Infectious Disease | 138 | 430 | 0 | 0.0 | (no title shared by 2+ trials) | participants (48.6) |
| Neurology | 121 | 223 | 2 | 1.7 | lactobacillus safety (2) | participants (31.4) |
| Endocrine and Metabolic | 112 | 181 | 2 | 1.8 | change in glycosylated haemoglobin hba1c (2) | participants (12.5) |
| Autoimmune and Inflammatory | 93 | 218 | 2 | 2.2 | number of participants with adverse events (2) | participants (40.9) |
| Cardiovascular | 87 | 230 | 0 | 0.0 | (no title shared by 2+ trials) | participants (44.8) |
| Mental Health | 79 | 128 | 0 | 0.0 | (no title shared by 2+ trials) | units on a scale (27.8) |
| Respiratory | 76 | 153 | 0 | 0.0 | (no title shared by 2+ trials) | participants (21.1) |
| Reproductive and Sexual Health | 71 | 138 | 0 | 0.0 | (no title shared by 2+ trials) | participants (21.1) |
| Substance Use Disorders | 56 | 90 | 2 | 3.6 | percent heavy drinking days (2) | participants (42.9) |
| Dermatology | 55 | 98 | 0 | 0.0 | (no title shared by 2+ trials) | participants (30.9) |
| Hematology | 53 | 117 | 0 | 0.0 | (no title shared by 2+ trials) | participants (39.6) |
| Pain | 53 | 86 | 0 | 0.0 | (no title shared by 2+ trials) | units on a scale (28.3) |
| Musculoskeletal | 49 | 84 | 0 | 0.0 | (no title shared by 2+ trials) | score on a scale (26.5) |
| Ophthalmology | 44 | 88 | 4 | 9.1 | visual acuity (3) | participants (25.0) |
| Kidney and Urological | 42 | 64 | 0 | 0.0 | (no title shared by 2+ trials) | participants (35.7) |
| Gastrointestinal | 38 | 77 | 0 | 0.0 | (no title shared by 2+ trials) | participants (23.7) |
| Transplant and Immunology | 29 | 47 | 0 | 0.0 | (no title shared by 2+ trials) | participants (41.4) |
| Rare Diseases | 18 | 67 | 0 | 0.0 | (no title shared by 2+ trials) | participants (55.6) |

Oncology is the only group with real title concentration (20.8% of its trials share a title). The most common unit is a count of participants in 17 of 20 groups, which is a unit of reporting, not an endpoint.

**Endpoint families [heuristic keyword match on primary titles; a trial can match several]** (denominator 2,000)

| family | n | % of 2,000 |
|---|---:|---:|
| adverse events / safety / tolerability / DLT / MTD | 320 | 16.0 |
| tumour response (ORR, CR, response rate) | 146 | 7.3 |
| pharmacokinetics (Cmax, AUC, concentration, half-life) | 116 | 5.8 |
| progression-free / event-free / disease-free survival | 92 | 4.6 |
| pain score | 90 | 4.5 |
| overall survival / mortality / death | 81 | 4.0 |
| feasibility / adherence / retention / recruitment | 62 | 3.1 |
| HbA1c / glucose | 56 | 2.8 |
| depression / anxiety / psychiatric scale | 47 | 2.4 |
| immunogenicity (titer, GMT, seroconversion, antibody) | 39 | 1.9 |
| visual acuity / intraocular pressure | 33 | 1.6 |
| abstinence / substance use | 27 | 1.4 |
| quality of life / patient-reported function | 23 | 1.1 |
| viral load / virologic response | 21 | 1.1 |
| lipids (LDL-C etc.) | 18 | 0.9 |
| blood pressure | 17 | 0.8 |
| body weight / BMI | 15 | 0.8 |
| lung function (FEV1, FVC) | 10 | 0.5 |
| (no family matched) | 963 | 48.1 |

Within Oncology (n = 437): tumour response (ORR, CR, response rate) 126 (28.8%), adverse events / safety / tolerability / DLT / MTD 107 (24.5%), progression-free / event-free / disease-free survival 87 (19.9%), overall survival / mortality / death 40 (9.2%), feasibility / adherence / retention / recruitment 14 (3.2%), pharmacokinetics (Cmax, AUC, concentration, half-life) 11 (2.5%), (no family matched) 123 (28.1%).

Scale caveat: the sample is 2.5% of the dataset, so "shares a title with another sampled trial" understates sharing in the full dataset (a title seen once here could have dozens of users there). The scale-free readings are the share of trials covered by the commonest titles and families, and the ratio of distinct titles to measures.

## 6. What this supports and does not support

**Supports**

- **A trial-level outcomes layer from structured fields, for nearly every record.** Results modules are present in 99.8% of trials; participant flow, per-arm serious-adverse-event totals with denominators, and completion counts are near-universal and uniformly structured.
- **Safety and attrition as cross-trial outcomes.** STARTED and COMPLETED exist for every sampled trial; serious-event numbers affected and at risk for every arm in 99.7%; an all-cause mortality table in 63.4% overall and 97.7% of results first posted since 2021. **[inferred]** These are the outcome quantities whose structure is the same in every record, whatever the condition.
- **Describing statistical reporting itself.** Whether a primary analysis, p-value, estimate or CI was posted is measurable for every trial, and can be tabulated against whether race, ethnicity and sex were reported.
- **Efficacy linkage within a narrow, pre-chosen slice.** 53.7% of trials have arm-level primary data from which an effect could in principle be computed **[inferred]**, and 18.3% post an estimate with a CI. **[inferred]** A shared endpoint exists only where one dominates a field; in this sample that is oncology survival and response endpoints.
- **Using the baseline population as the composition denominator**, with a stated caveat: baseline total equals STARTED in 79.2% of trials.

**Does not support**

- **Outcomes by race, ethnicity or sex within a trial.** Structured subgroup results exist in about 1.0% of trials for any dimension and 0.2% for race or ethnicity (5 trials; upper CI bound 0.6%); no sampled trial reports a primary outcome by race or ethnicity in class or category titles, and 1 of 2,000 does so through race-defined arms (NCT03098836). Any linkage built on these fields is therefore between a trial's demographic *composition* and its *trial-level* outcome: an ecological comparison, not evidence about how any group fared.
- **Pooling efficacy across trials by endpoint.** 98.3% of primary titles are unique to one sampled trial, units run to 887 distinct strings, and estimate types to 249.
- **A registry-wide "met its primary endpoint" flag from structured fields.** Only 25.6% of trials post a primary p-value, 36.2% of posted primary p-values carry an operator rather than a plain number, and no field states the prespecified threshold, direction or multiplicity rule **[inferred]**. This is the gap the Industry Outcomes beta fills by reading protocols and SAPs for 598 trials.
- **Detecting outcome switching** from the current record (section 3.2).
- **Claims about trials without posted results.** The dataset contains none.
- **Comparing other-adverse-event rates across trials** without handling the reporting threshold (0 in 51.9%, 5 in 39.7%).

## 7. Limitations of this audit

1. **Sample size.** 2,000 of 80,890 (2.5%). Proportions near 30-70% are known to about ±2 points; rare features are not: the race-or-ethnicity subgroup count rests on 5 trials, and the observational stratum has 117. Intervals are Wilson intervals that treat the proportional stratified sample as a simple random sample and ignore the finite-population correction (both slightly conservative).
2. **One draw.** A different seed would move every figure within its interval; nothing was re-drawn.
3. **Frame versus fetch date.** IDs come from the 2026-11-08 release; the registry was read on 2026-11-13. Records may have changed in between (study type and enrollment did not, section 2).
4. **Registered-versus-posted** compares the current record with itself. No version history was fetched.
5. **Subgroup detection is a regular-expression heuristic** over English titles, class titles and category titles only. It does not read outcome descriptions, analysis descriptions, population descriptions or posted documents. Recall is unknown; the "mention only" row bounds it loosely from above. Precision was judged by the auditing model on all 23 flagged trials (16 yes, 5 partly, 2 no), not by an independent human. The rule was tightened once after reading its first output (dropping "Child-Pugh", repeated single life-stage words and donor attributes), so it is fitted to this sample.
6. **"Carries data", "effect computable" and field presence are structural.** No value was checked for plausibility, no p-value or CI parsed and validated, and the project's AACT baseline notes record a trial (NCT01507090) whose metadata says "count" while the values are percentages.
7. **Measure-level distributions over-weight trials with many measures**; trial-level counts are given for the main ones only.
8. **Baseline total** relies on a group titled "Total" (or a single group, or a last group equal to the sum of the others); 0 trials fell through to summing groups. STARTED totals sum the first period's groups, which is right for parallel and crossover-sequence designs but was not checked design by design.
9. **Population-description tags and endpoint families are keyword matches**; 18.1% of descriptions and 48.1% of trials matched no tag or family.
10. **Title comparability is exact-string after light normalisation**; it under-counts conceptually identical endpoints, and within-sample sharing under-counts sharing in the full dataset (section 5).
11. **The AACT mapping is recalled, not verified.** Column names, especially for `outcome_counts.scope`, baseline population text and limitations text, need checking against the dictionary.
12. **`phase` and `sponsor_class` strata use our record's values**, not re-derived ones.
13. **Not done:** no secondary-outcome deep dive beyond counts and analysis presence; no check of value-level agreement between API and AACT; no look at `documentSection` (protocol/SAP availability) for the sample.

## 8. Context read before measuring

- `civicsample-engine` at `76c8a93`: `outcomes/README.md`, `outcomes/evidence.py`. The Industry Outcomes beta is manual, covers industry Phase 3 randomized trials with primary completion in 2020 (648 frame records, 598 eligible on 2026-11-09), and reads `protocolSection`, `documentSection` and `hasResults` only; it does not read `resultsSection`. `src/utils.py` takes `primary_endpoint` and `secondary_outcomes` from `protocolSection.outcomesModule`.
- `clinical-trial-populations` at `73e5d58`: `data/outcomes/summary.json` (598 eligible; results posted 416; protocol or SAP posted 418; in the demographics dataset 416; audit collection 23; human-reviewed assessments 0), `industry_outcomes.js` (presents those files, computes no aggregate), `data/geo/README.md`.
- `civicsample-verify-ai-private`, `origin/main` at `808ad3a`: only the most recent outcomes status file was read (`docs/outcomes-review/checkpoint-20261109/STATUS-20261110.md`). It records that, as of 2026-11-10, the outcomes packet experiment had not been activated and no provider call had been made. Nothing else from that repository is reproduced here.

## 9. Files and re-running

| file | what it is |
|---|---|
| `report.md` | this report (rendered by `scripts/make_report.py` from the JSON files) |
| `sample_ids.txt`, `sampling.json` | the 2,000 IDs and the sampling parameters |
| `sample_frame.json` | the sampled records' fields used here, copied from the release |
| `cache/raw_responses.jsonl.gz`, `cache/fetch_manifest.json` | raw API responses and fetch record |
| `analysis.json` | every count in this report with its denominator and interval |
| `subgroup_hits.jsonl` | all 67 flagged subgroup measures |
| `scripts/draw_sample.py`, `fetch.py`, `analyze.py`, `crosscheck.py`, `make_report.py` | the pipeline; `explore_keys.py`, `inspect_data.py`, `probe_*.py`, `show.py` are the exploratory helpers used along the way |

Cross-check: `scripts/crosscheck.py` recomputes twelve headline counts by a separately written code path and compares them with `analysis.json`; all twelve agree (it caught one inconsistency during the audit, a data-presence count that had wrongly required `reportingStatus` = POSTED, which was fixed before this report was rendered).

Offline re-run (no network): `python3 -I scripts/analyze.py <outdir> && python3 -I scripts/make_report.py <outdir>`. Re-drawing needs the release file: `python3 -I scripts/draw_sample.py <demographics.json.gz> <outdir>`; re-fetching: `python3 -I scripts/fetch.py <outdir>` (skips batches already cached).
