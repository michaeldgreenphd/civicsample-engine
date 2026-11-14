# Primary-Analysis Decision Rule Census — Inventory Summary

## Run Status & Budget Summary

Execution completed **348 / 402 (86.6%)** in-scope primary-pass trials and **40 / 40 (100.0%)** independent second-pass trials under the **\$150.00 USD** continuation hard cap (stopping before launching any wave whose Wave 1 per-trial estimate plus running total would exceed \$150.00).

- **In-scope trial count check:** 402 / 598 (67.2%) rows in `index.json` had `(protocol == true or sap == true) and in_audit == false` (matched expected 402; 0 `in_audit == true` trials queried or recorded).
- **Download failure check:** 0 / 678 (0.0%) posted protocol/SAP PDF downloads failed.
- **ClinicalTrials.gov rate-limit check:** 0 HTTP 429 rate-limit responses observed.
- **Continuation spend (hard cap \$150.00 USD):** `1922` planner turns (`20,728,929` uncached input tokens, `205,949,981` cached context tokens, `3,062,178` output tokens) = **\$101.79 USD** on the standard Gemini Pro \$1.25 / \$0.125 / \$10.00 (<=200k) & \$2.50 / \$0.25 / \$15.00 (>200k) schedule (**\$148.81 USD** on the conservative \$2.00 / \$0.20 / \$12.00 & \$4.00 / \$0.40 / \$18.00 schedule). Cumulative spend including Wave 1 (\$26.68 / \$38.49): **\$128.47 USD** (standard) / **\$187.30 USD** (conservative).

---

## 1. Counts of Trials, Documents, and Elements by Status

### 1.1 Trials

| Trial Metric | Count / Denominator |
|---|---|
| Total rows in `civicsample.com/data/outcomes/index.json` | 598 / 598 (100.0%) |
| In-scope trials (`(protocol or sap) and not in_audit`) | 402 / 598 (67.2%) |
| Primary pass — completed trials | 348 / 402 (86.6%) |
| Primary pass — unstarted trials | 54 / 402 (13.4%) |
| Completed primary-pass trials with >=1 downloaded protocol/SAP PDF | 348 / 348 (100.0%) |
| Completed primary-pass trials with 0 downloaded protocol/SAP PDFs (`NO_DOCUMENT`) | 0 / 348 (0.0%) |
| Fixed 40 / 402 (10.0%) sample — primary pass completed | 40 / 40 (100.0%) |
| Fixed 40 / 402 (10.0%) sample — independent second pass completed | 40 / 40 (100.0%) |
| Fixed 40 / 402 (10.0%) sample — paired (`primary` + `second`) trials | 40 / 40 (100.0%) |

- **Primary-pass outcomes & element records:** **613** primary outcomes across **348** completed trials (`4291` primary-pass element rows).
- **Second-pass outcomes & element records:** **61** primary outcomes across **40** second-pass trials (`427` second-pass element rows; **4718** total rows in `locations.jsonl`).

### 1.2 Documents (`documents.jsonl`, Denominator = 678 Posted Protocol/SAP PDFs)

| Document Metric / Type | Count / Denominator |
|---|---|
| Total unique protocol/SAP/combined documents recorded | 678 / 678 (100.0%) |
| HTTP 200 successful PDF downloads | 678 / 678 (100.0%) |
| Failed PDF downloads (`http_status != 200`) | 0 / 678 (0.0%) |
| `doc_type = protocol` | 321 / 678 (47.3%) |
| `doc_type = sap` | 318 / 678 (46.9%) |
| `doc_type = combined` | 39 / 678 (5.8%) |
| Documents with all extractable-text pages (`pages_without_text == []`) | 359 / 678 (52.9%) |
| Partially image-only documents (`0 < len(pages_without_text) < physical_pages`) | 279 / 678 (41.2%) |
| Fully image-only documents (`len(pages_without_text) == physical_pages`) | 40 / 678 (5.9%) |
| Documents exceeding 400 physical pages (`physical_pages > 400`) | 14 / 678 (2.1%) |

### 1.3 Primary-Pass Elements by Status — Overall (Denominator = 4291 Primary-Pass Element Records)

| Status | Primary-Pass Count / Denominator | Second-Pass Count / Denominator |
|---|---|---|
| `FOUND` | 2209 / 4291 (51.5%) | 228 / 427 (53.4%) |
| `NOT_FOUND` | 164 / 4291 (3.8%) | 13 / 427 (3.0%) |
| `NOT_APPLICABLE` | 1265 / 4291 (29.5%) | 103 / 427 (24.1%) |
| `AMBIGUOUS` | 297 / 4291 (6.9%) | 29 / 427 (6.8%) |
| `UNREADABLE` | 356 / 4291 (8.3%) | 54 / 427 (12.6%) |
| `NO_DOCUMENT` | 0 / 4291 (0.0%) | 0 / 427 (0.0%) |
| **Total** | **4291 / 4291 (100.0%)** | **427 / 427 (100.0%)** |

### 1.4 Primary-Pass Elements by Status — Per Decision-Rule Element (Denominator = 613 Primary Outcomes per Element)

| Element | `FOUND` | `NOT_FOUND` | `NOT_APPLICABLE` | `AMBIGUOUS` | `UNREADABLE` | `NO_DOCUMENT` | Row Total |
|---|---|---|---|---|---|---|---|
| `primary_endpoint_analysed` | 554 / 613 | 0 / 613 | 0 / 613 | 24 / 613 | 35 / 613 | 0 / 613 | 613 / 613 |
| `analysis_population` | 474 / 613 | 0 / 613 | 0 / 613 | 97 / 613 | 42 / 613 | 0 / 613 | 613 / 613 |
| `hypothesis_type` | 331 / 613 | 53 / 613 | 131 / 613 | 40 / 613 | 58 / 613 | 0 / 613 | 613 / 613 |
| `alpha_and_sidedness` | 367 / 613 | 6 / 613 | 144 / 613 | 48 / 613 | 48 / 613 | 0 / 613 | 613 / 613 |
| `margin` | 51 / 613 | 53 / 613 | 438 / 613 | 12 / 613 | 59 / 613 | 0 / 613 | 613 / 613 |
| `interim_boundary` | 112 / 613 | 39 / 613 | 347 / 613 | 54 / 613 | 61 / 613 | 0 / 613 | 613 / 613 |
| `multiplicity` | 320 / 613 | 13 / 613 | 205 / 613 | 22 / 613 | 53 / 613 | 0 / 613 | 613 / 613 |
| **All 7 Elements** | 2209 / 4291 | 164 / 4291 | 1265 / 4291 | 297 / 4291 | 356 / 4291 | 0 / 4291 | **4291 / 4291** |

---

## 2. Download Failures

- **0 / 678 (0.0%)** download failures across all 348 / 402 completed primary-pass trials (and 40 / 40 second-pass trials).

---

## 3. 40-Trial Independent Second-Pass Agreement Table (Consistency Between Model Runs, Not Accuracy)

> **Methodological label:** This table measures **consistency between independent model runs, not accuracy** (it is neither human review nor a reference standard). The 40-trial sample was fixed before Wave 1 by sorting all 402 in-scope `NCT_ID`s ascending by lowercase hex `sha256(NCT_ID)` and taking the first 40. Separate fresh second-pass workers (`second-pass-01`, `second-pass-02`) processed those 40 trials without seeing any primary-pass output.

Paired trials: **40 / 40 (100.0%)** | Paired `(trial, primary_outcome, element)` rows: **427**.

### 3.1 Consistency Between Model Runs by Element

| Element | Exact Status Agreement (`agree / paired`) | `FOUND` in Both | `NOT_FOUND` in Both | `NOT_APPLICABLE` in Both | `AMBIGUOUS` in Both | `UNREADABLE` in Both | `NO_DOCUMENT` in Both | Status Disagreements |
|---|---|---|---|---|---|---|---|---|
| `primary_endpoint_analysed` | 54 / 61 (88.5%) | 46 / 61 | 0 / 61 | 0 / 61 | 1 / 61 | 7 / 61 | 0 / 61 | 7 / 61 (11.5%) |
| `analysis_population` | 60 / 61 (98.4%) | 44 / 61 | 0 / 61 | 0 / 61 | 10 / 61 | 6 / 61 | 0 / 61 | 1 / 61 (1.6%) |
| `hypothesis_type` | 54 / 61 (88.5%) | 37 / 61 | 0 / 61 | 7 / 61 | 3 / 61 | 7 / 61 | 0 / 61 | 7 / 61 (11.5%) |
| `alpha_and_sidedness` | 60 / 61 (98.4%) | 39 / 61 | 0 / 61 | 7 / 61 | 7 / 61 | 7 / 61 | 0 / 61 | 1 / 61 (1.6%) |
| `margin` | 52 / 61 (85.2%) | 7 / 61 | 0 / 61 | 37 / 61 | 0 / 61 | 8 / 61 | 0 / 61 | 9 / 61 (14.8%) |
| `interim_boundary` | 54 / 61 (88.5%) | 5 / 61 | 4 / 61 | 34 / 61 | 2 / 61 | 9 / 61 | 0 / 61 | 7 / 61 (11.5%) |
| `multiplicity` | 49 / 61 (80.3%) | 32 / 61 | 2 / 61 | 3 / 61 | 3 / 61 | 9 / 61 | 0 / 61 | 12 / 61 (19.7%) |
| **All 7 Elements** | **383 / 427 (89.7%)** | 210 / 427 | 6 / 427 | 88 / 427 | 26 / 427 | 53 / 427 | 0 / 427 | **44 / 427 (10.3%)** |

### 3.2 Consistency Between Model Runs by Primary- vs. Second-Pass Status Cross-Tabulation

| Primary Pass \ Second Pass | `FOUND` | `NOT_FOUND` | `NOT_APPLICABLE` | `AMBIGUOUS` | `UNREADABLE` | `NO_DOCUMENT` | Primary Row Total |
|---|---|---|---|---|---|---|---|
| `FOUND` | 210 / 427 | 1 / 427 | 0 / 427 | 2 / 427 | 1 / 427 | 0 / 427 | 214 / 427 (50.1%) |
| `NOT_FOUND` | 5 / 427 | 6 / 427 | 7 / 427 | 0 / 427 | 0 / 427 | 0 / 427 | 18 / 427 (4.2%) |
| `NOT_APPLICABLE` | 2 / 427 | 6 / 427 | 88 / 427 | 1 / 427 | 0 / 427 | 0 / 427 | 97 / 427 (22.7%) |
| `AMBIGUOUS` | 8 / 427 | 0 / 427 | 6 / 427 | 26 / 427 | 0 / 427 | 0 / 427 | 40 / 427 (9.4%) |
| `UNREADABLE` | 3 / 427 | 0 / 427 | 2 / 427 | 0 / 427 | 53 / 427 | 0 / 427 | 58 / 427 (13.6%) |
| `NO_DOCUMENT` | 0 / 427 | 0 / 427 | 0 / 427 | 0 / 427 | 0 / 427 | 0 / 427 | 0 / 427 (0.0%) |

---

## 4. Complete List of Everything Left Incomplete and Why

### 4.1 Unstarted Primary-Pass Trials

- **54 / 402 (13.4%)** primary-pass trials left unstarted due to the **\$150.00 USD** continuation cost cap:
  - **`shard-18` (18 / 18 trials unstarted, reason: \$150.00 USD cost cap):** `NCT04003155`, `NCT04007367`, `NCT04009577`, `NCT04016779`, `NCT04024228`, `NCT04032158`, `NCT04032171`, `NCT04034355`, `NCT04036292`, `NCT04039503`, `NCT04042077`, `NCT04050865`, `NCT04052620`, `NCT04058353`, `NCT04074928`, `NCT04078126`, `NCT04105972`, `NCT04106557`
  - **`shard-19` (17 / 17 trials unstarted, reason: \$150.00 USD cost cap):** `NCT04109976`, `NCT04120194`, `NCT04127786`, `NCT04137887`, `NCT04140695`, `NCT04146012`, `NCT04152083`, `NCT04163185`, `NCT04172831`, `NCT04206020`, `NCT04211363`, `NCT04211389`, `NCT04218110`, `NCT04234464`, `NCT04244084`, `NCT04249583`, `NCT04268303`
  - **`shard-20` (19 / 19 trials unstarted, reason: \$150.00 USD cost cap):** `NCT04276883`, `NCT04292730`, `NCT04292899`, `NCT04317040`, `NCT04320615`, `NCT04327388`, `NCT04358081`, `NCT04362813`, `NCT04368429`, `NCT04370548`, `NCT04372186`, `NCT04421508`, `NCT04446299`, `NCT04513652`, `NCT04620213`, `NCT04957212`, `NCT06192420`, `NCT06834490`, `NCT06854601`

### 4.2 Unstarted Second-Pass Sample Trials

- **0 / 40 (0.0%)** second-pass trials left unstarted (all 40 fixed-sample trials completed).

### 4.3 Partially Searched Documents (`pages_not_searched`)

- **0 / 4718 (0.0%)** location records left any extractable-text pages unsearched (`pages_searched == "all"` across all rows).

### 4.4 Image-Only / Unreadable PDFs (`UNREADABLE` Elements in Primary Pass)

- **Fully image-only PDFs (40 / 678 (5.9%)):** `NCT01265849/SAP_002.pdf` (4 / 4 pages without text), `NCT01265849/SAP_003.pdf` (3 / 3 pages without text), `NCT01265849/SAP_004.pdf` (3 / 3 pages without text), `NCT01265849/SAP_005.pdf` (2 / 2 pages without text), `NCT01265849/SAP_006.pdf` (3 / 3 pages without text), `NCT02111577/SAP_001.pdf` (83 / 83 pages without text), `NCT02525861/Prot_000.pdf` (98 / 98 pages without text), `NCT02525861/Prot_002.pdf` (95 / 95 pages without text), `NCT02685709/Prot_000.pdf` (162 / 162 pages without text), `NCT02685709/SAP_001.pdf` (44 / 44 pages without text), `NCT02725593/SAP_001.pdf` (86 / 86 pages without text), `NCT03699124/Prot_000.pdf` (93 / 93 pages without text), `NCT03699124/SAP_001.pdf` (38 / 38 pages without text), `NCT04247074/Prot_000.pdf` (97 / 97 pages without text), `NCT04247074/SAP_001.pdf` (54 / 54 pages without text), `NCT04249687/Prot_000.pdf` (88 / 88 pages without text), `NCT04249687/SAP_001.pdf` (38 / 38 pages without text), `NCT02899299/Prot_000.pdf` (137 / 137 pages without text), `NCT02899299/SAP_001.pdf` (63 / 63 pages without text), `NCT02915744/SAP_001.pdf` (22 / 22 pages without text), `NCT02914522/Prot_000.pdf` (122 / 122 pages without text), `NCT03197818/Prot_000.pdf` (81 / 81 pages without text), `NCT02929329/SAP_001.pdf` (59 / 59 pages without text), `NCT03237325/Prot_002.pdf` (67 / 67 pages without text), `NCT03277261/SAP_001.pdf` (52 / 52 pages without text), `NCT03284853/SAP_001.pdf` (45 / 45 pages without text), `NCT03299244/SAP_001.pdf` (36 / 36 pages without text), `NCT03315143/SAP_001.pdf` (39 / 39 pages without text), `NCT03471078/Prot_000.pdf` (84 / 84 pages without text), `NCT03521934/SAP_001.pdf` (40 / 40 pages without text), `NCT03627767/Prot_000.pdf` (183 / 183 pages without text), `NCT03660943/Prot_SAP_000.pdf` (133 / 133 pages without text), `NCT03664674/Prot_SAP_000.pdf` (58 / 58 pages without text), `NCT03700320/Prot_000.pdf` (108 / 108 pages without text), `NCT03746522/Prot_000.pdf` (96 / 96 pages without text), `NCT03788967/SAP_001.pdf` (40 / 40 pages without text), `NCT03831880/Prot_000.pdf` (104 / 104 pages without text), `NCT03831880/SAP_001.pdf` (34 / 34 pages without text), `NCT03857542/Prot_000.pdf` (122 / 122 pages without text), `NCT03954834/Prot_000.pdf` (91 / 91 pages without text)
- **Primary-pass `UNREADABLE` element records (356 / 4291 (8.3%) across 55 / 348 (15.8%) trials):**
  - `NCT01265849` (7 element records: outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Prot_000.pdf (p.75) and SAP_001.pdf (p.26) mention post-surgery risk-stratum analyses only as subgroup/secondary analyses and do not define 'OS in Low Risk Subjects' as a primary endpoint, while SAP_002.pdf (2 pages), SAP_003.pdf (2 pages), SAP_004.pdf (10 pages), SAP_005.pdf (1 page), and SAP_006.pdf (5 pages) are 100 percent scanned image-only PDFs with no extractable text.
  - `NCT01554618` (11 element records: outcome 0 `interim_boundary`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`, outcome 2 `hypothesis_type`, outcome 2 `alpha_and_sidedness`, outcome 2 `margin`, outcome 2 `interim_boundary`, outcome 2 `multiplicity`) — SAP_003.pdf makes no statement regarding interim analyses, and Prot_002.pdf (208 pages) is scanned/redacted without extractable body text.
  - `NCT02032004` (6 element records: outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Prot_000.pdf has 160/230 image-only pages without extractable text (including all of Section 9 Statistical Analysis, pp.175–193) and SAP_001.pdf has 208/209 image-only pages without extractable text (pp.2–209).
  - `NCT02065570` (10 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 0 `multiplicity`, outcome 1 `hypothesis_type`, outcome 1 `margin`, outcome 1 `multiplicity`, outcome 2 `hypothesis_type`, outcome 2 `alpha_and_sidedness`, outcome 2 `margin`, outcome 2 `multiplicity`) — Prot_000.pdf does not state superiority/non-inferiority/equivalence, and the Induction SAP in SAP_001.pdf (pp.52–104) is image-only without extractable text.
  - `NCT02075047` (4 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Prot_001.pdf pages 87–90 (end of Section 9 Data Analysis/Statistical Methods) and SAP_000.pdf page 9 (Section 3 Interim Analyses and Section 4 Hypotheses and Decision Rules) and page 15 have no extractable text.
  - `NCT02412735` (2 element records: outcome 0 `alpha_and_sidedness`, outcome 0 `interim_boundary`) — Protocol v6.0 p. 142 states 'The value of the posterior probability threshold for success will be discussed in the study statistical analysis plan (SAP)'; Protocol v7.0 p. 25, Protocol v6.0 Sample Size p. 129, and bundled SAP body pages p. 170, 173-253 have no extractable text.
  - `NCT02435992` (7 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`) — Prot_000.pdf Synopsis (p. 1-6, 8-19) and Section 13 Planned Statistical Methods (p. 87-93) have no extractable text; SAP_001.pdf (p. 27) states only equality vs inequality of proportions without stating superiority, non-inferiority, or equivalence.
  - `NCT02579265` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — All extractable text in Prot_000.pdf (76 pages with text; 5 pages without text) and SAP_001.pdf (56 pages with text) has garbled font/CMap character encoding, rendering statistical sections unreadable.
  - `NCT02600507` (2 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`) — Prot_000.pdf does not state superiority, non-inferiority, or equivalence, and SAP_001.pdf has 59 of 65 pages without extractable text covering all statistical methods sections.
  - `NCT02685709` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — All pages of Prot_000.pdf (162/162 pages) and SAP_001.pdf (44/44 pages) have no extractable text.
  - `NCT02687451` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Prot_000.pdf has 105 of 106 pages without extractable text and SAP_001.pdf has 31 of 32 pages without extractable text (page 1 of each contains only a one-sentence FDA post-marketing cover statement).
  - `NCT02899299` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Both Prot_000.pdf (137/137 pages without extractable text) and SAP_001.pdf (63/63 pages without extractable text) are scanned image-only PDFs with zero extractable text under PDFKit.
  - `NCT02915744` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — SAP_001.pdf (22/22 pages without extractable text) is a scanned image-only PDF with zero extractable text, while Protocol Amendment 3.0 (14 January 2016) defines safety as the protocol primary endpoint and lists survival status under secondary objectives with by-subject listings.
  - `NCT02971683` (2 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`) — Prot_004.pdf p.8 states abatacept will achieve a higher responder rate, but SAP_005.pdf pages 3–4, 11–15 (covering Sections 3–5 Objectives/Hypotheses/Sample Size), and 27–28 and Prot_004.pdf pages 101–102 in Section 8 have no extractable text.
  - `NCT03180307` (6 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — In Prot_000.pdf (Version 2.0, 27 Nov 2018; 59/87 pages without text) and SAP_001.pdf (Version 1.0, 16 May 2017; 13/23 pages without text), the synopsis, objectives, primary efficacy endpoint, primary statistical methods, sample size, interim analysis, and multiplicity pages listed in the readable Tables of Contents (Prot physical pages 4-11, 34-35, 42-46, 49; SAP physical pages 7, 12, 15-17, 22-23) are image-only with zero extractable text.
  - `NCT03183128` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Both Prot_000.pdf (69 pages; only isolated bullet characters and '13 April, 2020' on p.3 are extractable) and SAP_001.pdf (56 pages; only isolated section headings such as 'Primary Analysis of the Primary Endpoint' on p.29 and 'Modified Intent-to-Treat (mITT) Population Intent-to-Treat (ITT) Population' on p.19 and italicized species names are extractable) have their body text flattened/rasterized into non-extractable images.
  - `NCT03197818` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Both Prot_000.pdf (81 pages; 81/81 pages have zero extractable text) and SAP_001.pdf (91 pages; 10 pages have zero extractable text and 81 pages contain only the document stamp 'Approved CLI-CCD-05993AA1-14-CSR APP-01709 _ 1.0 External Use' with occasional bullet 'o' glyphs) are scanned image-only PDFs without readable body text.
  - `NCT03237325` (2 element records: outcome 0 `analysis_population`, outcome 0 `interim_boundary`) — SAP Version: 3.0 Section 9 specifies the mITT population for primary efficacy, and the Table of Contents maps Section 3.2.3 Modified Intent-to-treat (mITT) Population to page 15, which is image-only without extractable text (pages_without_text=[15]); all 67 pages of Prot_002.pdf are also image-only without extractable text.
  - `NCT03277248` (2 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`) — The primary efficacy model pages (Prot_000.pdf physical page 88 and SAP_001.pdf physical page 24) and sample size pages (Prot_000.pdf physical pages 81-83) are image-only without extractable text; readable pages do not explicitly state superiority, non-inferiority, or equivalence.
  - `NCT03277261` (3 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 0 `interim_boundary`) — Prot_000.pdf sample size pages (physical pages 80-82), Primary Efficacy Variable page (physical page 87), and all 52 pages of SAP_001.pdf are image-only without extractable text; readable pages do not state superiority, non-inferiority, or equivalence.
  - `NCT03315143` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — SAP_001.pdf (39 pages, API date 2020-08-21) is a scanned image-only PDF with zero extractable text across all 39 pages (pages_without_text 1..39). In Prot_000.pdf (AMENDED CLINICAL TRIAL PROTOCOL 01, 29-August-2018), the primary endpoints are time to first 3-point MACE and time to first CV death or HHF (p.57, 91, 93), whereas total occurrences of CV death, HHF, and urgent HF visit is a secondary endpoint (p.58, 92). The primary-analysis decision rule for this registered primary outcome resides in the unreadable SAP_001.pdf.
  - `NCT03420781` (8 element records: outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `multiplicity`) — SAP_001.pdf (Amendment 1, 09Dec2020) is an abbreviated post-early-termination SAP specifying descriptive efficacy analyses only; Prot_002.pdf (Protocol Amendment 2, 10 March 2021) P.2-111 is unreadable (74 image-only pages without text and 36 pages with garbled CMap encoding).
  - `NCT03426267` (6 element records: outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Body text of statistical sections in Prot_000.pdf (Version 01/Amendment 02, 02 Aug 2019, Section 9, P.45-51) and SAP_001.pdf (Plan Version No. : 1, 17SEP2020, Sections 6-12, P.7-8 and P.14-26) is redacted/blank.
  - `NCT03449134` (1 element records: outcome 0 `interim_boundary`) — Stated in Prot_SAP_000.pdf (027-03, 26-APR-2019). Section 9.1 (P.75) states one interim futility analysis at ~40 percent of the Main study period and refers to Section 9.7 for details, and Section 9.7 on P.85-86 is completely redacted as 'CCI'.
  - `NCT03449147` (1 element records: outcome 0 `interim_boundary`) — Stated in Prot_SAP_000.pdf (030-04, 26-APR-2019). P.76 states 1 interim futility analysis at ~40 percent and P.87 states Haybittle-Peto 0.0001 alpha spending, while the interim futility stopping boundary in Section 9.7.1 (P.86) is redacted as 'CCI'.
  - `NCT03450629` (5 element records: outcome 0 `analysis_population`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Statistical methodology sections in Prot_000.pdf (Amendment 03, 20Sep 2018, P.12 and P.53-55) and SAP_001.pdf (Plan Version No. : 1, 22APR2021, P.4-5, P.7-8, P.10-12, and P.14-30) are completely redacted/blank.
  - `NCT03471078` (2 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`) — SAP_002.pdf (V1.0, 31AUG2020) specifies a 2-sided CMH test at alpha=0.05 between avatrombopag and placebo on its 24 extractable pages without explicitly naming superiority, non-inferiority, or equivalence, while Prot_000.pdf (84/84 pages) and SAP_002.pdf pages 1-3, 6, 8 are image-only without extractable text.
  - `NCT03496012` (1 element records: outcome 0 `hypothesis_type`) — Protocol Version 5.0 (Amendment 4.0) physical page 49 (printed Page 48 of 59, continuation of Section 13.6.1) has zero extractable text, and SAP Version 4.0 (11 February 2021) physical page 22 drops the mathematical relation operator in H11/H12 ('phigh pcont') due to font CMap encoding while neither readable text uses the words superiority, non-inferiority, or equivalence.
  - `NCT03521934` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — SAP_001.pdf is a scanned image-only PDF (40/40 pages without extractable text). In Prot_000.pdf (Amended Protocol 01, 17-Dec-2018), the registered primary outcome (total occurrences of CV death, HHF, and urgent HF visits) is listed as secondary hypothesis H3 while time to first CV death or HHF (H1 in LVEF<50 percent, H2 in total population) are the primary endpoints; the SAP governing the registered primary outcome cannot be read.
  - `NCT03546816` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — All text-bearing pages of Prot_000.pdf (73 pages; 1 page without text) and SAP_001.pdf (165 pages; 1 page without text) have corrupted/garbled font CMap encoding yielding unreadable ciphered ASCII characters.
  - `NCT03561701` (3 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 0 `multiplicity`) — Prot_000.pdf describes a Chi-square test for active treatment versus placebo without explicitly stating superiority/non-inferiority/equivalence in readable text, and Prot_000.pdf physical page 95 (start of Section 12) plus SAP_001.pdf physical pages 2-39 are scanned image-only without extractable text.
  - `NCT03562156` (3 element records: outcome 0 `hypothesis_type`, outcome 0 `margin`, outcome 0 `multiplicity`) — Prot_000.pdf states that the primary statistical objective is to determine if there are differences in proportions via a Chi-square test without explicitly stating superiority/non-inferiority/equivalence, and defers full analysis details to SAP_001.pdf, whose body (pages 2-39) is scanned image-only without extractable text.
  - `NCT03616899` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Prot_SAP_000.pdf (Amendment 01, 27 Mar 2019; 74 pages) has 15 blank/redacted pages with zero extractable text (physical pages [5, 6, 9, 10, 11, 23, 28, 30, 34, 36, 38, 49, 50, 51, 59]), covering the Synopsis endpoint/statistical pages (5-6, 9-11), Section 7.2 Number of Subjects (23), and the entire Section 13 Statistics (printed pages 48-50 = physical pages 49-51: 13.2 Analysis of Efficacy, 13.4 Sample Size Estimation, 13.5 Level of Significance).
  - `NCT03660943` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Prot_SAP_000.pdf has 133 physical pages and zero pages with extractable text (100 percent scanned image-only PDF).
  - `NCT03664674` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Prot_SAP_000.pdf has 58 physical pages and zero pages with extractable text (100 percent scanned image-only PDF).
  - `NCT03677401` (1 element records: outcome 0 `interim_boundary`) — Protocol Version 3.0 (21 March 2019) states an optional sample-size re-estimation interim analysis at ~50 percent information and defers full boundary/adjustment details to a separate plan, while all 154 pages of SAP_000.pdf and bundled protocol versions on Prot_001.pdf pages 1-77 and 146-221 have unreadable garbled CMap font encoding.
  - `NCT03699124` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Both Prot_000.pdf (93/93 physical pages in pages_without_text) and SAP_001.pdf (38/38 physical pages in pages_without_text) contain zero extractable text via macOS PDFKit.
  - `NCT03700320` (4 element records: outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`) — No hypothesis type or explicit non-hypothesis statement was found across all 79 pages of SAP_001.pdf (where Sections 10, 12, and 13 on physical pages 21-25 and 35-36 have redacted body text), and Prot_000.pdf is an image-only scan with 0/108 pages containing extractable text.
  - `NCT03712137` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — All 66 pages of Prot_000.pdf and all 48 text-bearing pages of SAP_001.pdf (plus 5 pages without text: 45, 46, 47, 48, 50) have garbled CMap character encoding in PDFKit text extraction with zero readable English text.
  - `NCT03721016` (4 element records: outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`) — Unredacted hypothesis statements on Prot_002.pdf p.51 and SAP_003.pdf p.17 state equal vs not-equally-effective hypotheses without using superiority/non-inferiority/equivalence terminology, while the statistical and sample size sections (Prot_002.pdf p.52-59; SAP_003.pdf p.16, 18, 20-21, 28) are redacted with no extractable body text.
  - `NCT03732833` (6 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`) — All synopsis, objectives/endpoints, and primary efficacy analysis pages have no extractable text due to redaction (Prot_004.pdf has 64/104 pages without text including pp. 7-12, 17-30, 50-57; SAP_006.pdf has 21/40 pages without text including pp. 6, 8-9, 12, 14, 16-23, 28); all 40 readable pages of Prot_004.pdf and 19 readable pages of SAP_006.pdf were searched.
  - `NCT03746522` (1 element records: outcome 0 `analysis_population`) — SAP_001.pdf (Version 3.0, 29 October 2020) specifies patients >=12 years of age at informed consent in the Full Analysis Set (FAS) of the pooled pivotal cohort, but the membership definition of FAS in SAP Section 2.1 (physical pages 21-22) is PPD-redacted and Prot_000.pdf is a 96/96 image-only scan.
  - `NCT03785145` (6 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`) — Prot_000.pdf has 65/99 pages with no extractable text and all statistical pages redacted; SAP_001.pdf has 18/39 pages with no extractable text and body text redacted on pages 2-3, 6-9, 30, 32-33, and 38 covering analysis populations, efficacy endpoints, hypotheses, alpha/sidedness, and multiplicity.
  - `NCT03788967` (6 element records: outcome 0 `multiplicity`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Prot_000.pdf contains no explicit multiplicity statement across primary/secondary endpoints, and all 40 pages of SAP_001.pdf are scanned image-only without extractable text.
  - `NCT03796676` (2 element records: outcome 0 `interim_boundary`, outcome 1 `interim_boundary`) — SAP_001.pdf Section 7 states that an E-DMC monitors efficacy and safety according to the E-DMC charter without specifying stopping boundaries or alpha spending in the SAP, while Prot_000.pdf (155 pages) has a rasterized image-only body with only the approval header text extractable.
  - `NCT03812224` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Prot_000.pdf pages 1-124 (124/126 pages) and SAP_001.pdf (46/47 pages, with physical page 17 containing only '28') are scanned images without extractable text; Prot_000.pdf pages 125-126 contain only Amendment 1 (19 December 2018) administrative/clinical summary bullets with no statistical decision-rule elements.
  - `NCT03831880` (28 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`, outcome 2 `primary_endpoint_analysed`, outcome 2 `analysis_population`, outcome 2 `hypothesis_type`, outcome 2 `alpha_and_sidedness`, outcome 2 `margin`, outcome 2 `interim_boundary`, outcome 2 `multiplicity`, outcome 3 `primary_endpoint_analysed`, outcome 3 `analysis_population`, outcome 3 `hypothesis_type`, outcome 3 `alpha_and_sidedness`, outcome 3 `margin`, outcome 3 `interim_boundary`, outcome 3 `multiplicity`) — Both posted documents (Prot_000.pdf, 104/104 pages without extractable text; SAP_001.pdf, 34/34 pages without extractable text) are scanned image-only PDFs yielding zero extractable text.
  - `NCT03875092` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Prot_002.pdf (30-Oct-2019; 136/137 pages without extractable text) and SAP_003.pdf (29-Jan-2018; 56/57 pages without extractable text) contain extractable text only on their 1-page cover sheet (official title, NCT number, document date); all body pages are scanned images.
  - `NCT03918629` (10 element records: outcome 0 `interim_boundary`, outcome 1 `interim_boundary`, outcome 2 `interim_boundary`, outcome 3 `interim_boundary`, outcome 4 `interim_boundary`, outcome 5 `interim_boundary`, outcome 6 `interim_boundary`, outcome 7 `interim_boundary`, outcome 8 `interim_boundary`, outcome 9 `interim_boundary`) — Protocol Section 9.5 (Prot_000.pdf physical page 67) and SAP Section 7 Interim Analyses (SAP_001.pdf physical page 33) are redacted as 'CCI'.
  - `NCT03925727` (4 element records: outcome 0 `analysis_population`, outcome 0 `interim_boundary`, outcome 1 `analysis_population`, outcome 1 `interim_boundary`) — SAP Section 8.2 names the ITT population for primary efficacy analyses, while the text defining the Intent-to-Treat population under Protocol Section 10.1.1 and SAP Section 7.1 is white-box redacted.
  - `NCT03937882` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Both Prot_002.pdf (Amendment 2, 15Jul2020) and SAP_003.pdf have corrupted/garbled font CMap character encoding across all body and statistical sections (only Appendix 3 header text on Prot_002.pdf page 71 is legible), preventing extraction of readable verbatim statistical text.
  - `NCT03954834` (5 element records: outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `multiplicity`) — SAP_001.pdf page 18 references EAS and FAS, but the population definition sections in SAP_001.pdf (30/41 pages without text) and all 91 pages of Prot_000.pdf have no extractable text.
  - `NCT04024072` (3 element records: outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Readable pages of Prot_000.pdf and SAP_001.pdf do not state the bioequivalence margin, and the specific efficacy-analysis sections — Prot_000.pdf physical p.49 (printed Page 48, Section 8.5 Efficacy Measures and Analysis) and SAP_001.pdf physical pages 4 and 6-16 (entire SAP body including Section 5.7.1 Primary Endpoint) — are in pages_without_text (zero extractable text via PDFKit).
  - `NCT04247074` (14 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`, outcome 1 `primary_endpoint_analysed`, outcome 1 `analysis_population`, outcome 1 `hypothesis_type`, outcome 1 `alpha_and_sidedness`, outcome 1 `margin`, outcome 1 `interim_boundary`, outcome 1 `multiplicity`) — Both Prot_000.pdf (97/97 physical pages in pages_without_text) and SAP_001.pdf (54/54 physical pages in pages_without_text) contain zero extractable text via macOS PDFKit.
  - `NCT04249687` (7 element records: outcome 0 `primary_endpoint_analysed`, outcome 0 `analysis_population`, outcome 0 `hypothesis_type`, outcome 0 `alpha_and_sidedness`, outcome 0 `margin`, outcome 0 `interim_boundary`, outcome 0 `multiplicity`) — Both Prot_000.pdf (88/88 physical pages in pages_without_text) and SAP_001.pdf (38/38 physical pages in pages_without_text) contain zero extractable text via macOS PDFKit.

---

## 5. Coordinator Schema & Mechanical PDF Verification (No Worker Records Modified)

| Mechanical Check | Result (Count / Denominator) |
|---|---|
| `documents.jsonl` records matching required 12-key schema & enums | 678 / 678 (100.0%) |
| Downloaded PDFs matching SHA-256, byte count, and `PDFKit` `physical_pages` | 678 / 678 (100.0%) |
| `locations.jsonl` records matching required 11-key schema & enums | 4718 / 4718 (100.0%) |
| Citations matching required 6-key schema, `1 <= len <= 300`, normalized whitespace | 10720 / 10720 (100.0%) |
| Citations verbatim match on cited `physical_page` (exact whitespace-normalized `PDFKit` substring) | 10720 / 10720 (100.0%) |
| Citations matching cited `physical_page` after NFKC / soft-hyphen / dash normalization | 10720 / 10720 (100.0%) |
| Citations not matched on cited `physical_page` by `PDFKit` substring check | 0 / 10720 (0.0%) |
| Citations containing literal `...` or `…` characters | 0 / 10720 (0.0%) |

### Listed Inconsistencies (Unresolved, Preserved As Emitted by Workers)

- **Structured schema, hash, page-count, citation, ellipsis, and single-page substring checks:** **0 / 678 (0.0%)** document-record inconsistencies, **0 / 4718 (0.0%)** structured location-record inconsistencies, and **0 / 10720 (0.0%)** single-page substring mismatches.
- **Free-text `note` vs. `documents.jsonl` `physical_pages` discrepancy (7 / 4718 (0.1%) location rows; left unmodified):** In `locations.jsonl` lines 8–14 (`NCT01265849`, `primary_outcome_index = 1`, all 7 elements, `worker = "shard-01"`), the worker's free-text `note` parenthetically refers to `SAP_002.pdf (2 pages)`, `SAP_003.pdf (2 pages)`, `SAP_004.pdf (10 pages)`, `SAP_005.pdf (1 page)`, and `SAP_006.pdf (5 pages)`, whereas verified `physical_pages` in `documents.jsonl` are `4`, `3`, `3`, `2`, and `3`.
