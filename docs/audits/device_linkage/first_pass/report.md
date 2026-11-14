# AI/ML device list -> ClinicalTrials.gov linkage: read-only audit

Run 2026-11-13. Inputs read at `origin/main` of both repositories (site `73e5d58`, engine `76c8a93`; the audited files in both working trees are byte-identical to `origin/main`). Nothing was committed, pushed or edited in any repository; no model or paid API was called. ClinicalTrials.gov API v2.0.5, `dataTimestamp` 2026-11-13T10:00:06.

Three kinds of statement appear below and are kept apart: **measured** (a count from a script in `scripts/`, with its denominator), **my reading** (a judgement on strings a human should confirm), and **not verified here** (things I know about FDA's site in general but could not check because the host is blocked).

## Headline

1. **We hold exactly one identifier that could link to a registry record, and almost nothing links through it.** Every row has an FDA submission number; no row has an NCT ID, a predicate number, an applicant ID or a device identifier. Searching ClinicalTrials.gov full text for all 1,430 distinct submission numbers found 2 studies that genuinely cite one (2 of 1,430, 0.14%).
2. **Route A (NCT IDs inside FDA documents) could not be measured, and the pilot did not measure it either.** `accessdata.fda.gov` is blocked from this environment, so the 150-document sample was not drawn. Only 12 FDA PDFs exist on disk; all 12 have clean text, 0 contain an NCT ID -- but 10 of the 12 are De Novo *granting order letters*, which carry no study description at all. The 8 pilot LLM extractions (all on those letters) returned an empty `nct_ids` 21 times and no field 3 times, out of 24. Zero from this corpus is not evidence that FDA summaries rarely cite trials.
3. **Route B (name + manufacturer search) gives a string-evidence candidate for 11 of 80 sampled devices (13.8%, 95% CI 7.9-23.0%).** 41 of 80 (51%) have a sponsor match only, 5 of 80 a name match under a different sponsor, 23 of 80 nothing. 50 of 80 device names return zero hits on every name query.
4. **A name match is to a product family, not to a submission.** 601 of 1,430 distinct submissions (42%) share their version-stripped name with at least one other submission. The class (i) study set matched to HeartFlow's K161772 would match its two same-named siblings equally; HeartFlow has 7 submissions on the list.
5. **Timing splits the class (i) evidence roughly 60/40.** Of 34 class (i) device-study pairs, 15 completed on or before the FDA decision, 6 were under way at the decision, and 13 started after it (10 of those more than a year after). 3 of the 11 class (i) devices have only post-decision studies.

## 1. Inventory of the device list

Source: `data/ai-ml-enabled-devices-enriched.csv` (sha256 `930b79b8...`), identical in both repositories. Script: `scripts/01_inventory.py`, output `out/inventory.json`.

### 1.1 Rows are not submissions

| | Count |
|---|---|
| Rows | 1,451 |
| Distinct submission numbers | **1,430** |
| Submission numbers appearing twice | 21 (20 `K`, 1 `P`) |
| ... pairs that are byte-identical rows | 7 of 21 |
| ... pairs differing only in company spelling (`Pearl Inc.` / `Pearl, Inc.`) | 14 of 21 |
| ... pairs differing in anything else | 0 of 21 |

19 of the 21 duplicated numbers were decided in April or June 2025; the other two are K241887 (July 2024) and P210015 (October 2022). `data/fda_token_metrics.json` already reports `total_fda_tools: 1430`; the AI Devices tab counts rows, so it shows 1,451. Everything below says which denominator it uses.

The raw and enriched CSVs agree on the six shared columns for 1,450 of 1,451 rows; the one difference is a `\r` inside a company cell (DEN230027).

### 1.2 Identifier types and completeness (rows, n = 1,451)

| Column | Non-blank | Distinct values | Usable as a link key? |
|---|---|---|---|
| Submission Number | 1,451 (100%) | 1,430 | Yes -- the only true identifier. Format `K######` 1,396 rows, `DEN######` 37, `P######` 17, `P######/S###` 1 |
| Date of Final Decision | 1,451 (100%) | 981 | All parse as M/D/YY; range 1995-09-29 to 2025-12-30 |
| Device | 1,451 (100%) | 1,176 | Free text; see 1.5 |
| Company | 1,451 (100%) | 747 | Free text; see 1.6 |
| Panel (Lead) | 1,451 (100%) | 17 | Category only |
| Primary Product Code | 1,451 (100%) | 168 | Category only (all 3 capital letters); 82 codes occur once |
| pdf_url | 1,451 (100%) | 1,430 | Constructed from the number, never fetched-and-confirmed; see 1.7 |
| local_pdf_path | 1,420 with a path, 31 `Not Found` | 1,400 paths | Points outside both repositories; see 1.7 |

**Not held at all** (no column exists): NCT ID, predicate submission number, regulation number, applicant/registration/FEI number, UDI or GUDID device identifier, date received, decision code, whether the 510(k) carries a Summary or only a Statement.

### 1.3 Submission-number prefixes and what each pathway implies

| Prefix | Rows | Distinct | Pathway | What the pathway implies for trial linkage |
|---|---|---|---|---|
| `K` | 1,396 | 1,376 (96.2%) | 510(k) premarket notification | Clearance by substantial equivalence to a predicate. Clinical data is often not required; bench or retrospective reader studies are common and are frequently not registered. "No registered trial" is the expected answer. The public document is a 510(k) Summary (or only a Statement). |
| `DEN` | 37 | 37 (2.6%) | De Novo classification | Novel device, no predicate; clinical evidence is usual. The evidence is described in the De Novo *decision summary*, a separate document from the granting letter. |
| `P` | 18 | 17 (1.2%) | Premarket approval (one is a supplement, `P140011/S008`) | Class III; a pivotal clinical study is the norm and is described in the Summary of Safety and Effectiveness Data. Highest prior probability of a registered trial. |

The pathway descriptions are regulatory background, not something measured here. In the Route B sample the two PMA/De Novo devices that reached class (i) are consistent with it (section 4), but n is 4.

### 1.4 Decision year (rows, n = 1,451)

| Year | Rows | Year | Rows | Year | Rows |
|---|---|---|---|---|---|
| 1995-2015 (15 years with any) | 42 | 2019 | 80 | 2023 | 226 |
| 2016 | 18 | 2020 | 114 | 2024 | 236 |
| 2017 | 27 | 2021 | 130 | 2025 | 350 |
| 2018 | 65 | 2022 | 163 | | |

812 of 1,451 rows (56%) were decided in 2023-2025. By pathway and year: `out/inventory.json` -> `pathway_by_year`. The two digits after the prefix are the fiscal year of receipt: decision year minus submission year is 0 for 822 rows, 1 for 606, 2 for 19, 3 for 3, and 10 for the one PMA supplement.

### 1.5 Device names

| Measure | Value | Denominator |
|---|---|---|
| Distinct exact name strings | 1,176 | 1,451 rows |
| Exact names used by more than one row | 186 names covering 461 rows | 1,451 rows |
| Version-stripped "core" names (first model, parentheses and trailing version removed) | 1,048 | 1,430 distinct submissions |
| Core names shared by more than one submission | 219, covering **601 submissions (42.0%)** | 1,430 |
| ... of those families, spanning more than one normalised company | 37 | 219 |
| ... spanning more than one brand-level company | 17 | 219 |
| Names listing more than one model (`;`, ` / `, `, `) | 154 (10.8%) | 1,430 |
| Names where the core differs from the listed string | 394 (27.6%) | 1,430 |
| Core name is a single token | 298 (20.8%); 51 of them five characters or fewer | 1,430 |
| Core name made only of device-type words ("Auto Segmentation", "AI Platform") | 101 (7.1%) | 1,430 |
| Core name contains the manufacturer's own brand word | 282 (19.7%) | 1,430 |

Largest families (rows): BriefCase (Aidoc) 18 under one core name plus 7 "BriefCase-Triage" and 5 "BriefCase-Quantification"; Swoop Portable MR Imaging System 9; EPIQ/Affiniti ultrasound 8; ART-Plan 7; AI-Rad Companion 7. Eleven families span six or more decision years.

Text hygiene: 24 device names contain C1 control characters (0x99 x34, 0x93/0x94, 0x96 -- cp1252 trademark, quote and dash bytes decoded as Latin-1); 59 contain non-ASCII; 2 company cells begin with a tab and newline. Visible typos in the source list include `Viz.Al, Inc.` (letter L for I), `MRlguidance B.V.`, `iSchema View Inc.` and the panel name `Clinical Toxcicology`.

### 1.6 Manufacturers

| Level | Distinct | What it is |
|---|---|---|
| Exact strings | 747 | as listed |
| Level 1 -- case, punctuation, legal form removed | **555** | "the same legal-entity name"; 128 of these merge more than one spelling |
| Level 2 -- level 1 minus corporate filler words ("medical", "systems", "healthcare", country names) | 517 | "the same brand"; merges GE Medical Systems / GE Healthcare; 19 brand keys merge more than one level-1 name |

So: about **555 distinct manufacturers** by a conservative reading, about 517 corporate brands by a looser one. 315 of the 555 appear on exactly one row; 63 have five or more; 21 have ten or more. The ten largest level-1 names hold 287 of 1,451 rows (19.8%); the ten largest brands hold 384 (26.5%).

| Level-1 name | Rows | | Brand key | Rows |
|---|---|---|---|---|
| siemens medical solutions usa | 65 | | ge | 95 |
| canon medical systems | 38 | | siemens | 83 |
| shanghai united imaging healthcare | 34 | | canon | 40 |
| aidoc medical | 31 | | philips | 39 |
| ge medical systems ultrasound primary care diagnostics | 29 | | shanghai united | 38 |
| ge medical systems | 23 | | aidoc | 31 |
| philips ultrasound | 19 | | ischemaview | 17 |
| ischemaview | 17 | | samsung medison | 17 |
| samsung medison | 17 | | hyperfine | 13 |
| ge healthcare | 14 | | viz ai | 11 |

Level 2 is a hand-made heuristic and it failed in an instructive way during the audit: with city names in the filler list, "Shanghai United Imaging Healthcare" reduced to `united`, whose sponsor query returned 3,178 studies. The list was corrected and the queries rerun; the lesson for design is that brand keys must be curated (as `sponsors/company_aliases.csv` already is for the sponsor layer), not derived.

### 1.7 Panels, product codes, document URLs, PDFs on disk

Top 15 panels (rows, of 17 distinct): Radiology 1,104 (76.1%), Cardiovascular 141, Neurology 67, Anesthesiology 27, Gastroenterology-Urology 26, Hematology 21, Ophthalmic 10, Pathology 9, Clinical Chemistry 9, General and Plastic Surgery 7, Microbiology 6, Orthopedic 5, Dental 5, Clinical Toxcicology [sic] 5, Obstetrics and Gynecology 4.

Top 15 product codes (rows, of 168 distinct): QIH 257, LLZ 160, IYN 109, LNH 94, JAK 91, QAS 78, MYN 41, QKB 40, QFM 39, MUJ 32, QDQ 30, DQK 26, KPS 24, QNP 19, POK 19. These 15 cover 1,059 of 1,451 rows (73%).

`pdf_url`: present on 1,451 of 1,451 rows. Every value is built by `fetch_fda_pdfs.py::build_pdf_url` and follows one pattern, `https://www.accessdata.fda.gov/cdrh_docs/pdf<YY>/<SUBMISSION>.pdf` (1,450 rows), plus one malformed case for the PMA supplement (`.../pdf14/P140011/S008.pdf`). The same pattern is used for all three pathways.

`local_pdf_path`: 1,420 rows (1,400 distinct submissions) name `Data/FDA Summary Statements/2026-04-16_<SUBMISSION>.pdf`. That directory is gitignored in the engine and is not on disk in either repository: **0 of 1,420 claimed paths exist here.** 31 rows (30 distinct submissions) say `Not Found`: all 17 PMA numbers, 12 510(k)s (11 decided in 2010 or earlier, plus K233662) and 1 De Novo (DEN130013).

**PDFs actually on disk: 12**, all in the engine's `data/pilot_summary_statements/` (10 `DEN`, 2 `K`); 0 in the site repository. That is 12 of 1,430 distinct submissions (0.8%).

How the site uses these (read from `app.js`, not changed): the AI Devices tab derives pathway from the prefix (`aiPathwayOf`), links the number to FDA's database page (`getFDAUrl`), and shows a "PDF Available" badge whenever `local_pdf_path` is not `Not Found` -- so 1,420 rows carry that badge while pointing at a constructed URL nobody has confirmed from here. The extraction tab renders `cited_clinical_studies.nct_ids` as registry links and otherwise prints "None cited in the FDA summary."; for all 8 pilot documents it prints the latter. `beta/approval-queue.jsx` assigns every FDA record Tier 1 unconditionally.

## 2. Reachability from this environment

One request each, through the session proxy (`scripts/02_reachability.sh`, record in `out/reachability.txt`). No bypass was attempted.

| URL | Result |
|---|---|
| `https://clinicaltrials.gov/api/v2/version` | **200** (API 2.0.5) |
| `https://www.accessdata.fda.gov/cdrh_docs/pdf25/K253532.pdf` | **Blocked** -- proxy refused the CONNECT with 403; no bytes from FDA |
| `https://api.fda.gov/device/510k.json?limit=1` | **Blocked** -- proxy refused the CONNECT with 403 |
| `https://www.fda.gov/` | **200** |

Consequence: every FDA document and every openFDA field (applicant, decision code, summary-or-statement flag, product-code classification) is out of reach here. `www.fda.gov` is reachable but does not host the per-submission documents.

## 3. Route A -- NCT IDs cited in FDA summary documents

Scripts: `scripts/03_pdf_identifiers.py` (pypdf 7.1.0 and, independently, poppler `pdftotext`), `scripts/06_pilot_llm_vs_regex.py`. Outputs: `out/pdf_identifiers.json`, `out/pilot_llm_vs_regex.json`.

**The seeded sample of 150 additional PDFs was not drawn: the host is blocked.** What follows covers only the 12 PDFs on disk.

| Measure | Value | Denominator |
|---|---|---|
| PDFs read | 12 | 12 on disk |
| Pages read | 88 | |
| PDFs with extractable text (both extractors) | 12 | 12 |
| Image-only pages | 0 | 88 |
| PDFs containing `NCT\d{8}` (also tried with space, `#`, hyphen, and across line breaks) | **0** | 12 |
| PDFs mentioning "clinicaltrials.gov" | 0 | 12 |
| NCT IDs found | none | -- so none to check against the registry |
| PDFs stating any cohort size ("N patients/subjects/cases/images") | 0 | 12 |

What the 12 documents are, by content:

| Document type | Count | Could it cite a study? |
|---|---|---|
| De Novo granting order letter (classification, risks, special controls); no "DE NOVO CLASSIFICATION REQUEST FOR" heading, no clinical section | 10 of 12 (all 10 `DEN` files) | No -- it is not the decision summary |
| 510(k) letter + Indications for Use + 510(k) Summary | 2 of 12 (K203042, a CT scanner; K203103, imaging software) | Yes in principle; these two describe bench testing against predicates |

So the measured zero is a statement about document type. The engine's prompt (`prompts/fda_demographics.txt`) tells the model to scan "SUMMARY OF CLINICAL INFORMATION", a section that exists in De Novo decision summaries and in none of the ten De Novo files held.

*Not verified here:* my understanding is that FDA publishes De Novo decision summaries under `cdrh_docs/reviews/DEN######.pdf`, and PMA documents as `cdrh_docs/pdf<YY>/P######A.pdf` (approval order) and `...B.pdf` (SSED), neither of which `build_pdf_url` constructs. Two measured facts are consistent with that: all 10 De Novo PDFs fetched from the constructed URL are letters, and the April fetch found a PDF for 0 of 17 PMA numbers.

### Pilot LLM extractions versus regex

`data/fda_demographics_extracted.json` holds 8 documents x 3 models (Haiku 4.5, Sonnet 4.6, Opus 4.7) = 24 outputs. All 8 documents are De Novo letters from the set above.

| What `cited_clinical_studies.nct_ids` holds | Outputs | Of |
|---|---|---|
| `{"value": [], "exact_quote": "", "page_number": 0}` at the expected path | 20 | 24 |
| Same empty value, nested one level too deep (`data.data...`; Opus, DEN160026) | 1 | 24 |
| Field absent -- output malformed or truncated (Haiku: DEN140025, DEN170022, DEN170043) | 3 | 24 |
| Any NCT ID | **0** | 24 |

Regex over the same 8 PDFs also finds none: the two methods agree on 8 of 8 documents, both empty. Sonnet's `total_participants` and `clinical_study_design` are "Not Reported" on 8 of 8, which is what a letter would produce. The agreement therefore confirms that the letters contain nothing; it does not test either method's recall, because no positive case exists in the corpus.

Two side measurements from the same files:

* **Predicate numbers are regex-extractable from 510(k) summaries.** Both 510(k) PDFs cite other `K` numbers (2 and 9). Of those 11 cited numbers, 1 is itself on the AI list. n = 2 documents.
* **The manuscript stream reaches NCT IDs, but through an unreviewed fuzzy match.** One of the 8 extracted AI/ML manuscripts carries 4 NCT IDs; all 4 exist on ClinicalTrials.gov (`scripts/09_validate_nct.py`; a control ID `NCT99999999` correctly returns nothing). The manuscript is a review article attached to K252557 (Lumify) by `aiml_fuzzy_matches_pending_review.csv` with a blank `Decision`; none of the 4 trials names that device in its title or interventions. Chaining device -> fuzzy manuscript -> NCT ID would have produced 4 false links from 1 device.

## 3b. Supplementary route D -- registry records that cite the FDA submission number

Not in the brief. Added because the submission number is the one identifier held for every device, and because registry records were seen to carry `510(k)` numbers in intervention text (in `civicsample-verify-ai-private`, see section 7). Script `scripts/07_route_d_submission_number.py`; 58 batched full-text queries plus 79 follow-ups.

| Measure | Value | Denominator |
|---|---|---|
| Distinct submission numbers searched | 1,430 | 1,430 |
| Numbers with at least one full-text hit | 4 | 1,430 |
| ... where the number is actually present in the returned record, next to regulatory wording, with the device named | **2** (0.14%) | 1,430 |
| ... where the number is not in the record the API returns and the study is unrelated | 2 | 4 hits |

| Submission | Device as listed | NCT ID | Evidence in the record | Study start vs decision |
|---|---|---|---|---|
| DEN170073 | ContaCT (`Viz.Al, Inc.`) | NCT04608617 | intervention name "Device: Viz LVO (De Novo Number DEN170073)"; sponsor Viz.ai, Inc. | +3.1 years |
| K240036 | PIUR tUS Infinity (PIUR Imaging GmbH) | NCT07178080 | description: "...cleared by the U.S. Food and Drug Administration under 510(k) (K240036)..."; sponsor Sarasota Memorial Health Care System | +1.0 year |

The two false hits (K202212, K202404) are unrelated studies from one hospital whose records do not contain the string; a bare search hit must never be stored as a link. The DEN170073 case is the clearest single example in this audit: the device is listed under a superseded name ("ContaCT") and a misspelled company ("Viz.Al"), the registry calls it "Viz LVO", and neither name search nor sponsor matching connects them -- only the identifier does.

## 4. Route B -- search ClinicalTrials.gov by device and manufacturer

Scripts: `scripts/04_sample.py`, `05_route_b_fetch.py`, `08_route_b_classify.py`, `10_summarise.py`. Sample: `sample_devices.csv`. Raw responses: `raw/ctgov_route_b.jsonl.gz` (537 responses, 0 errors). Per-device results: `out/route_b_per_device.csv`; every candidate with its evidence: `out/route_b_classified.json`; full lists: `out/route_b_lists.md`.

### 4.1 Sample

80 devices drawn with `random.Random(20261113)` from the 1,430 distinct submissions, proportionally by decision year (floor of 1 per stratum, largest remainder): <=2015: 2 of 42; 2016-2018: 6 of 110; 2019: 5 of 80; 2020: 6 of 114; 2021: 7 of 130; 2022: 9 of 162; 2023: 13 of 226; 2024: 13 of 235; 2025: 19 of 331. By pathway: 76 510(k), 2 De Novo, 2 PMA.

### 4.2 Queries and hit counts

Up to nine queries per device, `pageSize=10`, `countTotal=true`, with the requested fields. Two things differ from the brief: the API rejects `Collaborators` (HTTP 400) and wants `CollaboratorName`; and I queried both the name as listed and a "core" name (first model, parentheses and trailing version removed), because the listed string often cannot match anything.

| Query | Devices queried | Zero hits | >= 1 hit | > 10 hits (page truncated) | >= 50 hits | Median | Max |
|---|---|---|---|---|---|---|---|
| `query.intr` = name as listed | 80 | 61 | 19 | 7 | 3 | 0 | 166 |
| `query.term` = "name as listed" (phrase) | 80 | 65 | 15 | 3 | 2 | 0 | 8,283 |
| `query.intr` = core name (only where it differs) | 22 | 11 | 11 | 4 | 1 | 0.5 | 299 |
| `query.term` = "core name" | 22 | 17 | 5 | 3 | 2 | 0 | 66 |
| `query.spons` = company, level 1 | 80 | 31 | 49 | 6 | 1 | 1 | 60 |
| `query.lead` = company, level 1 | 80 | 52 | 28 | 4 | 1 | 0 | 55 |
| `query.spons` = brand key (only where it differs) | 39 | 10 | 29 | 17 | 12 | 6 | 394 |
| `query.term` = "core name" AND `query.spons` = brand | 80 | 69 | 11 | 1 | 0 | 0 | 16 |
| `query.intr` = core name AND `query.spons` = brand | 80 | 68 | 12 | 0 | 0 | 0 | 9 |

* **50 of 80 devices (62.5%) get zero hits on every name query;** 30 of 80 get at least one.
* For 11 of the 22 devices whose core name differs from the listed string, the listed string returned nothing and the core name returned something. Querying the list's name verbatim loses those.
* The sponsor query returns at least one study for 55 of 80 devices (49 at level 1), the lead-sponsor query for 28. After checking the returned sponsor strings, **48 of 80 devices have a study whose sponsor or collaborator really is the manufacturer** (34 exact name, 14 more at brand level), and for 31 of 80 the manufacturer is the lead sponsor. Device makers are often collaborators, so lead-sponsor matching alone misses a third of them.
* The 80 devices returned 540 device-study pairs in total (median 3 per device).

### 4.3 Classification rule (string evidence only)

*Name evidence* counts only when a name form appears as a contiguous run of whole tokens in an intervention name, an intervention "other name", the brief title or the official title. Forms: the full listed model name with version and parentheses removed, or that name with trailing device-type words removed ("Imagio Breast Imaging System" -> "Imagio"). If what remains is only the manufacturer's own brand ("Athelas One" -> "Athelas"), that is recorded as *company-only* and is not a device-name match. A name appearing only in an intervention description is recorded but does not count.

*Sponsor evidence* (lead or any collaborator): **exact** level-1 name; **brand** (brand keys equal and both names start with it); or **family** (brand keys overlap but real words differ -- "Pearl" / "Pearl Therapeutics"). Only exact and brand count for class (i).

| Class | Definition | Devices | Share of 80 | 95% CI |
|---|---|---|---|---|
| **(i)** | device name in an intervention name or title AND sponsor/collaborator matches | **11** | 13.8% | 7.9-23.0% |
| **(ii)** | device name matches, sponsor differs | 4 | 5.0% | |
| (ii-c) | only the manufacturer's brand is named, sponsor differs | 1 | 1.3% | |
| (ii) + (ii-c) | | **5** | 6.3% | 2.7-13.8% |
| **(iii)** | sponsor matches on at least one returned study, device name not present | **41** | 51.3% | 40.5-61.9% |
| **(iv)** | no candidate | **23** | 28.8% | 20.0-39.5% |
| (iv-a) | zero hits on every name query and no sponsor match | 20 | | |
| (iv-b) | name queries returned studies, none with the name in a title or intervention | 3 | | |

No device fell into "name matches, sponsor family only".

Breakdowns (all of 80 unless stated):

* By pathway: class (i) is 9 of 76 510(k), 1 of 2 De Novo, 1 of 2 PMA.
* By name type: of the 16 sampled devices whose name is made only of device-type words, **0 reached class (i)**; 10 are class (iii), 5 class (iv), 1 class (ii-c).
* Class (iii) anatomy (41 devices): best sponsor tier is exact for 24, brand for 11, **family only for 6** -- and all 6 family-only cases look like unrelated companies or weak guesses (Exo Inc x3 -> "EXO Biologics S.A.", Pearl Inc. -> "Pearl Therapeutics", Insight Medical Systems -> "Insight Therapeutics", Springbok, Inc. -> "Springbok Analytics"). The matching party is the lead sponsor for 32 and a collaborator only for 9. In 2 of 41 the device name does appear, but only in an intervention description; in 6 of 41 a sponsor-matched study shares a distinctive word with the device name (a sibling product or earlier generation).
* Page truncation: `query.spons` exceeded 10 results for 6 devices, so their class (iii) study counts are lower bounds. The combined name-and-sponsor query exceeded 10 for one device only (FFRct, 16), so the class (i) *device* count of 11 is not affected by truncation; FFRct's pair count is a lower bound.
* How class (i) was reached: 8 of 11 devices are reachable from a name query alone, 8 of 11 from a combined query, and **2 of 11 (REMI-AI, LensHooke X12 PRO) only from the sponsor query** -- the name queries returned nothing for their long listed names, while the manufacturer's short study list contained the match.

### 4.4 Names that flood or collide

Six of 80 devices drew 50 or more results on a name query:

| Device (submission) | intr / phrase hits | Class | What the hits are |
|---|---|---|---|
| Aurora (K231355, EnsoData) | 139 / 8,283 | (ii) | Aurora kinase drugs, studies titled AURORA |
| AI Platform (AIP001) (K232501, Exo) | 299 / 66 (core) | (iii) | any study mentioning an AI platform |
| Spectral Bone Marrow (K223514, GE) | 166 / 0 | (iii) | bone-marrow studies |
| Lung-CAD (K223811, Imagen) | 93 / 1 | (iv-b) | lung and coronary-artery-disease studies |
| SIGNA 7.0T (K211118, GE) | 31 / 57 (core "SIGNA") | (i) | any GE SIGNA MR scanner |
| FFRct (K161772, HeartFlow) | 31 / 55 | (i) | genuine, plus other vendors' CT-FFR |

Across the sample, 25 studies matched a device name under a non-matching sponsor. By device: Aurora 7, SpotLight 6, FFRct 5, ZEUS 3, Venue Go 2, DeepVessel FFR 1, SIGNA 1. *My reading* of those strings: Aurora, SpotLight and ZEUS are all coincidences of a common word (drug trials, a survey platform, a stent, a radiotherapy planner); FFRct is a mix of real investigator-led HeartFlow studies and one competitor (Hemolens) using "FFRCT" as a generic term; one Venue Go hit is a real investigator-led study that names the device as an "other name", the other is "venue tracing" in an HIV study. `query.intr` is a relevance search, not a filter: "QuantX" returns a cephalometric study, and the phrase query for "QuantX" returns two trials sponsored by an unrelated drug company called QuantX Biosciences -- a device name colliding with a sponsor name.

### 4.5 All class (i) candidates

Eleven devices, 34 device-study pairs. *My reading* of each, for a human to confirm:

| # | Submission | Device | Pairs | Note |
|---|---|---|---|---|
| 1 | K161772 | FFRct (HeartFlow) | 9 | Exact name; HeartFlow lead on 5, collaborator on 4. The same 9 studies match K152733 and K182035, which carry the identical name; HeartFlow has 7 submissions on the list. Which submission a study supported cannot be read from strings. |
| 3 | K183012 | vascuCAP (Elucid Bioimaging) | 3 | Exact name as the intervention; manufacturer is lead sponsor. |
| 9 | K182798 | KIDScore D3 (Vitrolife) | 1 | "KIDScore D3 Clinical Study", manufacturer lead, completed 1.8 years before decision. Strongest single case. |
| 17 | K200921 | qER (Qure.ai) | 1 | Intervention "qER (qER EU 2.0)", NHS lead, Qure.ai collaborator; starts 3.8 years after decision. A UK deployment study of a later version. |
| 20 | K211118 | SIGNA 7.0T (GE) | 1 | **Weak.** Matched on the family word "SIGNA" only; a whole-body DWI study with GE Healthcare as collaborator, nothing in the strings says 7.0T. I would not accept it. |
| 22 | DEN200038 | Gili Pro BioSensor (ContinUse Biometrics) | 1 | Exact name as intervention, manufacturer lead, completed 1.2 years before decision. |
| 24 | P200003 | Imagio Breast Imaging System (Seno Medical) | 8 | "Imagio" in interventions, full name in one title (PIONEER-01). Manufacturer lead on all 8. PMA. |
| 55 | K240408 | REMI-AI Rapid Detection Module (Epitel) | 2 | Matched on "REMI", the hardware platform; the cleared device is an AI module on it. Family-level. |
| 62 | K243268 | TipTraQ (PranaQ) | 6 | Exact name in titles, manufacturer lead. One of the six is about a companion device (C-TraQ). |
| 65 | K242388 | LensHooke X12 PRO (Bonraybio) | 1 | "LensHooke X12 Device", manufacturer is collaborator; starts 1.1 years after decision. |
| 67 | K250650 | SpotLight / SpotLight Duo (Arineta) | 1 | "SpotLight" CT with Arineta as collaborator, 2016-2020 -- 4.4 years before this 2025 clearance. Arineta has 3 SpotLight submissions on the list; this is probably an earlier generation. |

The pairs themselves:

| # | Submission | FDA decision | NCT ID | Lead sponsor | Manufacturer's role (tier) | Name evidence | Start | Primary completion | Start minus decision (y) | Relation |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | K161772 | 2016-08-24 | NCT01757678 | HeartFlow, Inc. | lead (exact) | "ffrct" in intervention name | 2012-09 | 2013-09 | -3.9 | completed before |
| 1 | K161772 | 2016-08-24 | NCT03702244 | HeartFlow, Inc. | lead (exact) | "ffrct" in intervention name | 2018-11-27 | 2022-05-20 | +2.3 | started >1 y after |
| 1 | K161772 | 2016-08-24 | NCT05325112 | HeartFlow, Inc. | lead (exact) | "ffrct" in intervention name | 2022-07-15 | 2022-11-14 | +5.9 | started >1 y after |
| 1 | K161772 | 2016-08-24 | NCT06506448 | University Hospital Southampton NHS Foundati | collaborator (exact) | "ffrct" in intervention other name | 2025-01-20 | 2026-03 | +8.4 | started >1 y after |
| 1 | K161772 | 2016-08-24 | NCT01943903 | HeartFlow, Inc. | lead (exact) | "ffrct" in brief title | 2013-09 | 2015-12 | -2.9 | completed before |
| 1 | K161772 | 2016-08-24 | NCT02499679 | HeartFlow, Inc. | lead (exact) | "ffrct" in brief title | 2015-07 | 2019-02 | -1.1 | running at decision |
| 1 | K161772 | 2016-08-24 | NCT02813473 | ECRI bv | collaborator (exact) | "ffrct" in official title | 2016-06-27 | 2018-03-26 | -0.2 | running at decision |
| 1 | K161772 | 2016-08-24 | NCT03851276 | ECRI bv | collaborator (exact) | "ffrct" in brief title | 2019-06-28 | 2020-03-30 | +2.8 | started >1 y after |
| 1 | K161772 | 2016-08-24 | NCT04142021 | National University of Ireland, Galway, Irel | collaborator (exact) | "ffrct" in brief title | 2020-09-01 | 2022-12-31 | +4.0 | started >1 y after |
| 3 | K183012 | 2018-12-21 | NCT04439643 | Elucid Bioimaging Inc. | lead (exact) | "vascucap" in intervention name | 2020-02-05 | 2022-02-28 | +1.1 | started >1 y after |
| 3 | K183012 | 2018-12-21 | NCT04448353 | Elucid Bioimaging Inc. | lead (exact) | "vascucap" in intervention name | 2019-03-01 | 2021-12-15 | +0.2 | started <=1 y after |
| 3 | K183012 | 2018-12-21 | NCT02143102 | Elucid Bioimaging Inc. | lead (exact) | "vascucap" in official title | 2014-07 | 2017-03-14 | -4.4 | completed before |
| 9 | K182798 | 2019-07-19 | NCT03740828 | Vitrolife | lead (exact) | "kidscore d3" in intervention name | 2016-09-20 | 2017-10-12 | -2.8 | completed before |
| 17 | K200921 | 2020-06-17 | NCT06027411 | Guy's and St Thomas' NHS Foundation Trust | collaborator (brand) | "qer" in intervention name | 2024-03-27 | 2024-08 | +3.8 | started >1 y after |
| 20 | K211118 | 2021-05-13 | NCT06630845 | University of California, San Francisco | collaborator (brand) | "signa" in intervention name | 2025-02-06 | 2027-02-28 | +3.7 | started >1 y after |
| 22 | DEN200038 | 2021-04-01 | NCT04179279 | ContinUse Biometrics Ltd. | lead (exact) | "gili pro biosensor" in intervention name | 2019-10-06 | 2020-01-27 | -1.5 | completed before |
| 24 | P200003 | 2021-01-11 | NCT02364388 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2015-03-27 | 2016-02-15 | -5.8 | completed before |
| 24 | P200003 | 2021-01-11 | NCT03032198 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2017-12-04 | 2018-06-17 | -3.1 | completed before |
| 24 | P200003 | 2021-01-11 | NCT03708393 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2018-11-13 | 2018-12-09 | -2.2 | completed before |
| 24 | P200003 | 2021-01-11 | NCT04030104 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2019-07-30 | 2019-11-03 | -1.4 | completed before |
| 24 | P200003 | 2021-01-11 | NCT05022602 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2021-08-19 | 2022-02-17 | +0.6 | started <=1 y after |
| 24 | P200003 | 2021-01-11 | NCT05084729 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2020-09-11 | 2023-07-19 | -0.3 | running at decision |
| 24 | P200003 | 2021-01-11 | NCT05337280 | Seno Medical Instruments Inc. | lead (exact) | "imagio" in intervention name | 2021-03-30 | 2023-01-04 | +0.2 | started <=1 y after |
| 24 | P200003 | 2021-01-11 | NCT01943916 | Seno Medical Instruments Inc. | lead (exact) | "imagio breast imaging system" in brief title | 2012-12-21 | 2015-09-09 | -8.1 | completed before |
| 55 | K240408 | 2024-10-17 | NCT05818930 | Epitel, Inc. | lead (exact) | "remi" in intervention name | 2023-03-08 | 2025-06 | -1.6 | running at decision |
| 55 | K240408 | 2024-10-17 | NCT06027749 | Epitel, Inc. | lead (exact) | "remi" in official title | 2023-10-13 | 2025-03-30 | -1.0 | running at decision |
| 62 | K243268 | 2025-02-03 | NCT06351878 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2024-02-28 | 2024-08-23 | -0.9 | completed before |
| 62 | K243268 | 2025-02-03 | NCT06474351 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2023-06-01 | 2023-12-31 | -1.7 | completed before |
| 62 | K243268 | 2025-02-03 | NCT06474390 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2024-06-01 | 2026-04-01 | -0.7 | running at decision |
| 62 | K243268 | 2025-02-03 | NCT06633887 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2023-03-01 | 2024-02-28 | -1.9 | completed before |
| 62 | K243268 | 2025-02-03 | NCT06634693 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2023-10-10 | 2023-10-11 | -1.3 | completed before |
| 62 | K243268 | 2025-02-03 | NCT07744854 | PranaQ Pte. Ltd. | lead (exact) | "tiptraq" in brief title | 2026-06-05 | 2027-03-23 | +1.3 | started >1 y after |
| 65 | K242388 | 2025-05-09 | NCT07621991 | Indira IVF Hospital Pvt Ltd | collaborator (exact) | "lenshooke x12" in intervention name | 2026-06-01 | 2027-12-01 | +1.1 | started >1 y after |
| 67 | K250650 | 2025-04-15 | NCT02616939 | Carmel Medical Center | collaborator (exact) | "spotlight" in intervention name | 2016-01 | 2020-11 | -9.2 | completed before |

### 4.6 Ten class (ii) / (iii) examples for human judgement

All five class (ii)/(ii-c) devices and five of the 41 class (iii) devices, chosen to show the different ways the strings go wrong or nearly right. The other 36 class (iii) rows are in `out/route_b_lists.md`.

| # | Class | Device (company) | Best candidate | What the strings show | My reading |
|---|---|---|---|---|---|
| 33 | (ii) | DEEPVESSEL FFR (KeyaMed NA Inc.) | NCT04828590, "The ADAPT Study: ... DeepVessel FFR ...", lead **Keya Medical** | exact name in title; sponsor "keya medical" vs listed "keyamed na" | Same company written two ways; normalisation cannot bridge it. Probably a true link missed by the sponsor rule. |
| 29 | (ii) | Venue Go (GE ... Ultrasound and Primary Care Diagnostics) | NCT04433000, pocket ultrasound in COVID, University of Siena; also NCT01827228 | other-name "GE Healthcare Venue GO" on a 2020 comparison with a pocket scanner; and "venue tracing" in an HIV project | One real investigator-led use of a Venue Go (two years before this 2022 clearance, so an earlier version), one coincidence. |
| 32 | (ii) | ZEUS System (Zio Watch) (iRhythm) | NCT04579718 "Zeus Cloud" radiotherapy planning; a "Zeus CC Stent"; an insulin-pump study | "zeus" only | All three are other products (the third is a "Zeus" glucose sensor in a title). The distinctive name ("Zio Watch") sits in the parentheses the rule discards. |
| 47 | (ii) | Aurora (EnsoData) | NCT02219789 alisertib; 7 studies | "aurora" in other-name or title | All coincidences. The list itself has a second, unrelated "Aurora" (GE Medical Systems Israel, K251153). |
| 6 | (ii-c) | Athelas One (Athelas Inc.) | NCT07066085, University of Washington, "Athelas Home device" | manufacturer's brand in the intervention; different product name | Sibling product from the same maker, studied by a third party six years after clearance. |
| 4 | (iii) | Ahead 300 (BrainScope) | NCT01430988 and 9 more BrainScope-led studies; interventions name "BrainScope Ahead 200", "Ahead M-100" | sponsor exact; name differs by model number | Predecessor generations of the same product line. A family-level relationship, not this clearance. |
| 35 | (iii) | Ceevra Reveal 3+ (Ceevra) | NCT03334344, Ceevra lead, intervention "Ceevra Reveal" | sponsor exact; name lacks "3+" | Same product family; the study ran 2017-2019, this version was cleared in 2023. |
| 59 | (iii) | CorVista System with PH Add-On (Analytics for Life) | NCT04031989 "Repository of Phase Signals for Pulmonary Hypertension Algorithm...", intervention "CorVista Capture" | sponsor exact; shares "CorVista" | Plausibly the development study for exactly this add-on; only a reader can say. |
| 77 | (iii) | Second Opinion Periapical Radiolucency Contours (Pearl Inc.) | NCT01349803, "PT003 MDI Cardiovascular Safety Study", **Pearl Therapeutics** | sponsor family only; 60 hits on `query.spons=pearl` | Unrelated inhaler company. Sponsor-only on a short company name is a false-link generator. |
| 78 | (iii) | AIAS Cephalon (Metamorphosis GmbH) | NCT05812209, stellate ganglion block case series, **Metamorphosis LTD** | sponsor "exact" after legal form is stripped | Almost certainly a different company. Removing GmbH/LTD made two firms identical. |

## 5. Route C -- timing of class (i) studies against the FDA decision

Dates from the registry (`StartDate`, `PrimaryCompletionDate`; month-only dates placed mid-month) against the list's decision date.

| Relation to the decision date | Pairs | Of 34 |
|---|---|---|
| Primary completion on or before the decision | 15 | 44% |
| Started before, primary completion after | 6 | 18% |
| Started within 1 year after | 3 | 9% |
| Started more than 1 year after | 10 | 29% |

Per device (of 11): 7 have at least one study completed on or before the decision; 1 (REMI-AI) has only studies still running at the decision; **3 have only studies that started after it** (qER +3.8 y, SIGNA 7.0T +3.7 y, LensHooke X12 PRO +1.1 y).

For the 15 pre-decision pairs the gap from primary completion to decision has a median of 1.8 years; 9 fall within 2 years and 3 are more than 3 years before (Imagio PIONEER-01 5.3 y and a second Imagio study 4.9 y before its PMA; SpotLight 4.4 y before a clearance that is the family's third on the list). "Completed before the decision" therefore does not mean "the study this submission relied on": for a product with several clearances, an early study precedes all of them. The latest post-decision start is 8.4 years after (FFRct).

Of the 34 pairs, 21 are observational and 13 interventional; 12 have posted results.

## 6. Risks, and what they mean for the data model

Each risk is tied to something measured above.

| Risk | Evidence | Consequence for design |
|---|---|---|
| **Rows are not submissions** | 21 numbers appear twice; 1,451 rows, 1,430 numbers | Key the submission entity on the submission number; fold duplicate rows and keep their spellings as aliases. |
| **Name collisions with ordinary words and other products** | 6 of 80 names flood; 25 name-matched studies under other sponsors, most of them coincidences; "Aurora" names two unrelated devices on the list itself | A name match alone is never a link. It needs a sponsor match or an identifier, and single-word names need review even then. |
| **Generic names** | 101 of 1,430 names are only device-type words; 0 of 16 such names in the sample reached class (i) | Record these as "not searchable by name", with that reason, rather than as "no trial found". |
| **Listed names that cannot match as written** | 394 of 1,430 carry a version, model code or several models; 11 of 22 such sampled names matched only after stripping | Store the listed name and derived search forms separately, and record which form matched. |
| **Company spellings, typos and legal forms** | 747 strings -> 555 names; `Viz.Al`, `MRlguidance`; "KeyaMed NA Inc." vs "Keya Medical"; "Metamorphosis GmbH" vs "Metamorphosis LTD" | Company identity needs a curated alias table with reviewed entries, as the sponsor layer already has; automatic normalisation both over- and under-merges. |
| **Renames and acquisitions** | 17 name families span more than one brand key (a few are only spelling variants). They include Bay Labs -> Caption Health; IDx -> Digital Diagnostics; Zebra Medical Vision -> Nano-X AI; Matakina -> Volpara; Iterative Scopes -> Iterative Health; DiaCardio -> DiA Imaging Analysis; Galgo Medical -> Adas3D; Pathwork Diagnostics -> Cancer Genetics; Insight Medical Systems -> Kico Knee Innovation; Riverain Medical Group listed, Riverain Technologies in the registry | A manufacturer needs dated aliases. The trial sponsor at the time of the study may be the earlier name. |
| **Corporate families** | GE is listed under 9 legal-entity names (95 rows), Philips under 10 (39 rows), Siemens under 4 plus two more that the brand key does not merge (83 rows); `query.spons=ge` returns 394 studies | A sponsor match at brand level says nothing about a particular device of a conglomerate: all 12 sampled GE, Siemens and United Imaging devices that are class (iii) are there only because the group sponsors or collaborates on other studies. |
| **Several submissions per product** | 601 of 1,430 submissions share a core name; HeartFlow 7 submissions, 9 class (i) studies | Name evidence attaches a study to a *device family*. Only an identifier can attach it to a *submission*. The model needs both levels. |
| **Predecessor and sibling products** | Ahead 200 vs Ahead 300; Ceevra Reveal vs Reveal 3+; InferRead Lung CT.AI vs InferRead CT Stroke.AI; Athelas Home vs Athelas One | A sponsor-plus-family-word match is a pointer for a reviewer, not a link. |
| **Predicates** | Both local 510(k) summaries cite predicate numbers (2 and 9); 1 of those 11 is on the AI list | Predicate edges are extractable by regex from 510(k) summaries, mostly lead off the list, and are a submission-to-submission relation. Keep them out of the study link; a predicate's trial is not this device's trial. |
| **Wrong document for the question** | 10 of 10 De Novo PDFs held are letters; 0 of 17 PMA documents were fetched | The document entity needs a type, and "no NCT ID found" must record which document type was read. |
| **Two-hop inference** | 4 real NCT IDs reached K252557 through an unreviewed fuzzy manuscript match; none concerns the device | Never derive a device-study link through a manuscript whose own link is unreviewed. |
| **Post-market studies** | 13 of 34 class (i) pairs start after the decision | Timing is part of the relationship, not a filter; store it. |

### Which relationship types the evidence can actually distinguish

| Relationship | Distinguishable from the evidence gathered? | Observed |
|---|---|---|
| FDA document cites NCT ID | Method works (text is extractable, regex is trivial); yield unknown because the right documents are not held | 0 of 12, uninformative |
| Registry record cites submission number | Yes, exactly, at submission level | 2 of 1,430 |
| Name + sponsor match, completed before decision | Yes, at device-family level | 15 pairs / 7 of 80 devices |
| Name + sponsor match, running at decision | Yes, at device-family level | 6 pairs |
| Name + sponsor match, started after decision | Yes, at device-family level | 13 pairs / 3 of 80 devices have only these |
| Name match, other sponsor (investigator-led use *or* coincidence) | Not separable by strings | 25 studies / 5 of 80 devices |
| Sponsor only | Recognisable, but not a device relationship | 41 of 80 devices |
| "The pivotal study for this submission" | **No.** Nothing gathered here can establish it; it needs the FDA document's own description of the study | -- |

## Proposed minimal link schema

Kept small; every field is there because a measurement above requires it.

**device** (a product family)
`device_id`; `canonical_name`; `name_forms[]` (form, kind: listed | core | trimmed | alias, source); `name_searchability` (distinctive | single_common_word | generic | manufacturer_name_only) -- because 101 names are generic and collisions concentrate in single words; `manufacturer_id`.

**manufacturer**
`manufacturer_id`; `canonical_name`; `aliases[]` (string, valid_from, valid_to, source, reviewed) -- because of 747 -> 555 spellings, the renames listed above, and the Metamorphosis and Pearl false merges.

**regulatory_submission**
`submission_number` (key); `pathway` (510(k) | De Novo | PMA | PMA supplement); `decision_date`; `device_id`; `listed_device_name`, `listed_company` (verbatim, never overwritten); `panel`; `product_code`; `source_list_sha256`; `source_row_count` (to record the 21 duplicates); `predicate_submission_numbers[]` (when extracted).

**document**
`document_id`; `submission_number`; `document_type` (510k_summary | 510k_statement | de_novo_order_letter | de_novo_decision_summary | pma_approval_order | pma_ssed | other); `url`; `sha256`; `retrieved_at`; `pages`; `text_status` (text | partial | image_only | unavailable); `unavailable_reason`. The type is required because 10 of 12 documents held are the wrong type for this question.

**study**
`nct_id` (key); `brief_title`; `lead_sponsor`; `collaborators[]`; `start_date`; `primary_completion_date`; `study_type`; `has_results`; `registry_data_timestamp`.

**link** (one row per piece of evidence, not per pair)
* `link_id`; `nct_id`; `submission_number` (nullable); `device_id` -- a name-based link sets only `device_id`; an identifier-based link sets `submission_number`. This is the 42% family finding made structural.
* `method`: `fda_document_cites_nct` | `registry_cites_submission_number` | `name_and_sponsor` | `name_only` | `sponsor_only` | `manual`.
* `evidence`: `source` (document_id or nct_id), `location` (page number, or registry field path such as `armsInterventionsModule.interventions[].name`), `quote` (verbatim), `matched_name_form`, `sponsor_match_tier` (exact | alias | family | none), `sponsor_role` (lead | collaborator).
* `confidence_tier`: **A** identifier in text (either direction); **B** distinctive name in an intervention name or title, plus exact or curated-alias sponsor; **C** name with family-level sponsor, name only in a description, or a trimmed/family name form; **D** name only or sponsor only -- a review candidate, never published as a link.
* `timing`: `completed_before_decision` | `ongoing_at_decision` | `started_within_1y_after` | `started_later` | `unknown`, plus the two gaps in days. Computed per submission, so one study can be pre-market for a later clearance and post-market for an earlier one.
* `review_state`: `unreviewed` | `accepted` | `rejected`, with `reviewed_by`, `reviewed_at`, `review_note`. Three states, never collapsed -- the same rule the sponsor layer follows. On the audit's evidence, tier B needs review too: 1 of 11 class (i) devices (SIGNA) I would reject and 2 more (REMI, SpotLight) are family-level.
* Provenance: `extracted_at`, `pipeline_commit`, `registry_data_timestamp`.

**search_status** (per submission, per route) -- so absence is recorded as absence with a reason:
`route`; `status` (`searched_none_found` | `candidates_found` | `not_searchable_generic_name` | `document_unavailable` | `wrong_document_type` | `not_searched`); `searched_at`; `query_forms[]`. Without this, "no registered trial" (the normal answer for most 510(k)s) is indistinguishable from "never looked" and from "the PDF we read was a letter".

## What additional access would materially help

Stated as needs; none is assumed.

1. **`www.accessdata.fda.gov` on the network allowlist.** Needed to draw the Route A sample at all: 510(k) summaries, De Novo decision summaries and PMA SSEDs. Without it the yield of the most reliable route stays unknown. The document paths for De Novo summaries and PMA documents should be checked on the first request, since the current URL builder produces neither.
2. **`api.fda.gov` on the allowlist.** Needed for per-submission metadata the list lacks: applicant name as FDA holds it, decision code, the Summary-versus-Statement flag (which says in advance whether a 510(k) has a document worth reading), and product-code classification. Whether openFDA exposes predicate numbers is something I could not check.
3. **Read access to the 1,400 PDFs already fetched on 2026-04-16** (`Data/FDA Summary Statements/`, not in either repository). That would let Route A run on about 1,376 510(k) documents without touching FDA at all. It would not fix the De Novo and PMA document types.
4. **A reviewer's time on the 11 class (i) devices and the 6 near-miss class (iii) devices.** Every precision figure above is string evidence; none has been confirmed by a person.

Not needed: any new credential for ClinicalTrials.gov (open, and it served about 680 requests at roughly 1.2 per second with no errors).

## 7. `civicsample-verify-ai-private` -- device-related content (facts only)

No device-linkage work exists there. No file or document name refers to devices, 510(k), De Novo or the AI/ML list, and no Markdown file contains `510(k)`, a `K######` or `DEN######` number, or "ai-ml". The only occurrences are inside cached ClinicalTrials.gov study records under `inputs/mesh-area-labelling-*-sources/` (10 files): six mention "510(k)" in a description or link label, and five carry a `K######` number, in intervention, arm-group or results-group descriptions. None of those K-numbers was checked against the AI list here. Their relevance is only that they show registry records do sometimes cite a clearance number in free text, which is what prompted section 3b. ("Medical device" occurs in 75 files, again cached registry records and task inputs; "product code" occurs in 5 files that, by their names, concern vaccine product codes, not FDA device codes.)

## Limits of this audit

* Route A was not measured on a meaningful corpus. The requested 150-document sample needs a blocked host.
* Route B classes are string evidence. I did not confirm any link; the "my reading" columns are one reader's judgement.
* n = 80. The class (i) interval is 7.9-23.0%; the pathway breakdown rests on 2 De Novo and 2 PMA devices.
* Only the first 10 results of each query were examined.
* The generic-word list, the corporate-filler list and the version-stripping rule are hand-made (`scripts/common.py`) and move the class boundaries; one of them was wrong on first use and was corrected (section 1.6).
* Registry full-text search matched 2 submission numbers that are not in the returned records; I did not find out what it matched.
* ClinicalTrials.gov's own device-oversight fields (for example whether a study involves an FDA-regulated device) were not examined.

## Reproducing

All scripts are stdlib Python except `03_pdf_identifiers.py` (pypdf). Run with `python3 -I`; scripts read the repositories read-only and write only under this directory. With the caches in place, steps 5, 7 and 9 make no network calls.

```
E=/home/user/clinical-trial-populations/data/ai-ml-enabled-devices-enriched.csv
python3 -I scripts/01_inventory.py $E <raw.csv> out/inventory.json <site_repo> <engine_repo>
python  -I scripts/03_pdf_identifiers.py <engine_repo>/data/pilot_summary_statements $E out/pdf_identifiers.json
python3 -I scripts/04_sample.py $E sample_devices.csv 80 20261113
python3 -I scripts/05_route_b_fetch.py sample_devices.csv raw/ctgov_route_b.jsonl
python3 -I scripts/06_pilot_llm_vs_regex.py <site_repo>/data/fda_demographics_extracted.json out/pdf_identifiers.json out/pilot_llm_vs_regex.json
python3 -I scripts/07_route_d_submission_number.py $E raw/ctgov_route_d.jsonl out/route_d_submission_number.json
python3 -I scripts/08_route_b_classify.py sample_devices.csv raw/ctgov_route_b.jsonl out
python3 -I scripts/09_validate_nct.py raw/ctgov_validate.jsonl out/nct_validation.json NCT...
python3 -I scripts/10_summarise.py out/route_b_classified.json out/route_b_summary.json out/route_b_lists.md
python3 -I scripts/11_population_name_risks.py $E out/pdf_identifiers.json out/population_name_risks.json
```

`raw/ctgov_route_b.jsonl.gz` holds 537 responses; 12 of them are the superseded brand-key queries from before the correction in section 1.6 and are not read by the classifier.

## Files

| Path (under this directory) | Contents |
|---|---|
| `report.md` | this report |
| `sample_devices.csv` | the 80 sampled devices with stratum, cleaned name and company forms |
| `scripts/` | `common.py`, `ctgov.py`, and steps `01`-`11` |
| `raw/ctgov_route_b.jsonl.gz`, `raw/ctgov_route_d.jsonl.gz`, `raw/ctgov_validate.jsonl.gz` | every API response, with its parameters and fetch time |
| `out/inventory.json`, `out/population_name_risks.json` | section 1 and 6 numbers |
| `out/reachability.txt` | section 2 |
| `out/pdf_identifiers.json`, `out/pilot_llm_vs_regex.json`, `out/nct_validation.json` | section 3 |
| `out/route_d_submission_number.json` | section 3b |
| `out/route_b_per_device.csv`, `out/route_b_classified.json`, `out/route_b_summary.json`, `out/route_b_lists.md` | sections 4 and 5 |
