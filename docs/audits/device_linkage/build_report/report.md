# AI/ML device list → registered studies: evidence-linked build

Run 2026-11-13 23:20 UTC to 2026-11-14 00:30 UTC. Run id `devices-2026-11-14-cf872a1604`. Nothing was committed, pushed or edited in any repository; no model or paid API was called; no curated file was filled in. The run was interrupted once by a container restart and resumed from the files on disk (section 9).

Every count below names its unit and denominator. Three kinds of statement are kept apart: **measured** (from `out/summary.json`, reproducible from the frozen cache), **rule finding** (a keyword or string rule fired; a reader has to confirm what it means), and **my reading** (a judgement on strings).

All paths are under `<device-build working directory>/`.

## 1. Headline

1. **The inventory is 1,451 source rows, 1,430 submissions, 1,120 candidate families, 554 organisations.** The 21 duplicated numbers are 7 redundant rows and 14 alias-only rows (a company spelling); none carries conflicting information.
2. **Official documents are now held for 1,429 of 1,430 submissions**, 1,532 documents, 1.85 GB, 19,145 pages. For every one of the 37 De Novo numbers both the granting letter and the decision summary are held. The one submission with nothing of its own is the PMA supplement P140011/S008.
3. **FDA documents almost never cite a registry identifier.** 11 of 1,519 readable documents contain an NCT id (14 distinct ids, all 14 resolve). That gives identifier-confirmed links for 9 of 1,430 submissions. A second, independent extractor (poppler) finds the same ids in the same documents, 1,532 of 1,532.
4. **Most summaries nevertheless describe a study.** 873 of 1,422 substantive readable documents have study language and no NCT id (rule finding). No identifier route can reach those studies; that is the main negative finding.
5. **Registry records almost never cite a submission number.** 2 of 1,430 (DEN170073, K240036), as the audit found.
6. **Identifier-confirmed in total: 11 of 1,430 submissions, 15 submission–study pairs, 15 studies, 19 evidence records.** Confirmed means the citation exists and the identifier is real, not that the study supported the decision: K240712's three confirmed NCT ids are aducanumab drug trials whose MRI scans were used as development data.
7. **Name evidence gives family-level candidates only:** 258 name-and-organisation candidates for 108 of 1,120 families (169 of 1,430 submissions sit in those families), 290 name-only candidates, and 1,647 common-word matches rejected by rule. None is validated; none is attached to a submission.
8. **No link has been reviewed by a person** (0 of 2,217 evidence records). Nothing here is a scientifically verified link.

## 2. What was built

`engine_staging/` mirrors the engine repository layout (5,700 lines including tests).

| Path | What it does |
|---|---|
| `devices/inventory.py` | rows → submissions (keyed on the number; every row and every differing value kept; pathway from the prefix) → candidate families (same organisation **and** same whole core name) |
| `devices/organizations.py` | raw string kept; normalised key by documented rules; `organization_aliases.csv` is the only way differently named organisations merge |
| `devices/acquire.py` | polite, resumable acquisition: openFDA, FDA release-database pages, documents; frozen response archives; content-addressed store; disk budget |
| `devices/pdftext.py` | page text of one PDF, run as an isolated process; cached by sha256 and extractor version |
| `devices/documents.py` | document registry: type with rule and matched phrase, text status, garbled-page detection, identifier scan, study-language findings, `demographic_summary_candidate` with reason |
| `devices/registry.py` | ClinicalTrials.gov: NCT resolution, exact submission-number citation, name and organisation evidence |
| `devices/links.py` | evidence records, vocabularies, timing, reviews, documentary statements, and `problems()` (the rules as a check) |
| `devices/coverage.py` | per submission, per route: one of the seven states with its reason |
| `devices/demographics.py` | pilot extractions carried forward, attached to document and population; never addable to registry counts |
| `devices/artifacts.py`, `devices/contract.py` | the eleven output files; a checker that refuses a build breaking an invariant |
| `devices/README.md`, `devices/requirements.txt` | method and vocabulary; `pypdf` for text extraction |
| `devices/organization_aliases.csv`, `families.csv`, `reviews.csv`, `documentary_statements.csv` | curated files, header only |
| `scripts/build_device_links.py`, `scripts/check_device_links_contract.py` | `acquire` (network) and `build` (frozen cache, no network); contract check |
| `tests/devices/` | 216 offline tests on fictional records (215 pass; 1 skips outside the repository) |

Outputs: `out/` (11 JSON files, 21 MB), `cache/` (1.84 GB: documents, text, frozen API archives, manifest, logs), `analysis/` (audit regression, extractor cross-check, artifact hashes, a seeded review sample).

### Vocabulary defined in code and README

* **Level**: `submission` or `family`.
* **Proposed relationship** (what the evidence says): `study_cited_in_submission_document`, `submission_cited_in_registry_record`, `study_cited_in_parent_submission_document`, `name_and_organisation_candidate`, `name_only_candidate`; and, only through a documentary statement a person enters, `explicitly_cited_supporting_study`, `related_validation_study`, `documented_postmarket_study`.
* **Reviewed relationship** (what a person concludes; kept beside, never instead of, the proposed one): the last three plus `same_family_other_version`, `citation_not_about_this_device`, `not_related`.
* **Automated validation**: `identifier_confirmed` (a document of that submission cites an NCT id that resolves, or a registry record carries that exact submission number in a retrieved field); `quote_confirmed_in_source` (a person's documentary statement whose quote stands in the retrieved source); `identifier_not_resolved`; `rejected_by_rule`; `not_validated`.
* **Human review**: `unreviewed`, `accepted`, `rejected`, `unresolved`; read from `devices/reviews.csv`, never set by code.
* **Timing**: its own object on every record, per relevant submission; never changes relationship, validation or confidence.
* **Coverage**: the seven states of the requirements, per route and source.

`links.problems()` and the contract refuse a name match that is submission-level, validated, or carries identifier confidence.

## 3. Connectivity

One request each before any other use, 2026-11-13 about 23:20 UTC, through the session proxy.

| Host | Request | Result |
|---|---|---|
| `www.accessdata.fda.gov` | `cdrh_docs/pdf25/K253532.pdf` | 200, PDF, 685,678 bytes |
| `api.fda.gov` | `device/510k.json?limit=1` | 200, JSON |
| `www.fda.gov` | `/` | 200 |
| `clinicaltrials.gov` | `api/v2/version` | 200 (API 2.0.5, data timestamp 2026-11-13T10:00:06) |

No host blocked the run. About 6,200 requests in total, at most two per second per host.

### URL patterns: recollections against what the hosts returned

| Recollection | Finding |
|---|---|
| 510(k): `cdrh_docs/pdf{YY}/{K}.pdf` | True for receipts from 2010. 2002–2009 use one digit (`pdf8/K082483.pdf`); 2001 and earlier use `pdf/` (`pdf/K003301.pdf`). The inventory's `pdf_url` pads to two digits and is wrong for those years, which is why the April fetch found nothing for 11 old 510(k)s. |
| De Novo decision summary: `cdrh_docs/reviews/{DEN}.pdf` | True for 36 of 37. DEN130013 is filed under its original 510(k) number: `pdf12/K124067.pdf` and `reviews/K124067.pdf`. |
| PMA SSED: `cdrh_docs/pdf{YY}/{P}B.pdf` | True for 12 of 16 PMAs (with `A` = approval order, `C` = labeling). The four pre-1999 PMAs are single scanned files (`pdf/P980025.pdf`). |
| not recalled | 510(k)s for in-vitro diagnostics also have an FDA review memo at `reviews/{K}.pdf` (36 held). |

Documents were discovered from FDA's release-database page for each number, which lists them with FDA's own label; patterns were a recorded fallback (8 documents were first found by pattern: the four pre-1999 single-file PMAs, and four 510(k) summaries whose database page answered HTTP 400 on the first request).

## 4. Inventory and deduplication (measured)

Source: `ai-ml-enabled-devices-enriched.csv`, sha256 `930b79b8…`, 1,451 rows. It agrees with FDA's list as downloaded (`…csv_20260305.csv`, sha256 `71a43ef2…`) on the six shared columns for 1,450 of 1,451 rows; the one difference is a stray carriage return in the company cell of DEN230027.

| Count | Numerator / denominator | Definition |
|---|---|---|
| Source rows | 1,451 | lines of the list |
| Distinct submissions | 1,430 of 1,451 rows | distinct submission numbers; 21 extra rows |
| Duplicated numbers | 21 of 1,430 submissions | numbers on more than one row (each on exactly two) |
| … identical | 7 of 21 | every cell byte-identical: redundant records |
| … alias only | 14 of 21 | rows differ only in the company spelling, which normalises to one key |
| … substantive conflict | 0 of 21 | anything else differs |
| Candidate families | 1,120 | same organisation and same whole core name |
| … with several submissions | 190 of 1,120 families | covering 500 of 1,430 submissions (35.0%) |
| … curated | 0 of 1,120 | `families.csv` is empty |
| Organisations | 554, from 748 raw strings | canonical normalised keys |
| … with several spellings | 130 of 554 | joined by the normalisation rules |
| … merged by alias | 0 of 554 | `organization_aliases.csv` is empty |

Identical: K240795, K242748, K243558, K250268, K250443, K251397, P210015.

Alias only (the two spellings): K240398 (`RaySearch Laboratories AB (PUBL)` / `(publ)`), K241887 (`Cosmo Artificial Intelligence - AI Ltd` / `- Ai, Ltd.`), K242295 (`Bunkerhill` / `BunkerHill Health`), K242600 and K243234 (`Pearl Inc.` / `Pearl, Inc.`), K242737 (`Empatica S.r.l.` / `S.R.L.`), K243144 (`Scopio Labs , Ltd.` / `Scopio Labs Ltd.`), K243567 (`Tyto Care Ltd.` / `Tyto Care , Ltd.`), K243743 (`Holberg Eeg AS` / `EEG AS`), K243769 (`Qfr Solutions BV` / `QFR Solutions bv`), K250151 (`Eko.ai Pte. Ltd. d/b/a Us2.ai`, two capitalisations), K250436 (`Siemens Shenzhen Magnetic Resonance Ltd.` / `, Ltd.`), K250749 (`Physcade` / `PhysCade, Inc.`), K251153 (`Ge` / `GE Medical Systems Israel`).

So the duplicate rows add an alias and nothing else. The audit reported the 14 as "Pearl Inc. / Pearl, Inc."; only two of them are Pearl.

**Counting unit for the tab.** `run.json → counting_units` defines source row, submission, family, organisation, document, evidence record, pair and study, and `summary.json` gives rows (1,451), submissions (1,430) and families (1,120; all candidate) as separate denominators. A submission count is not a product count: BriefCase (Aidoc) is 18 submissions in one candidate family, with "BriefCase-Triage" (7) in another because the names differ. Whether those are one product is a curation decision.

**Pathways** (from the prefix): 1,376 510(k), 37 De Novo, 16 PMA, 1 PMA supplement. openFDA agrees on all: 37 `DENG` decisions, 17 PMA records.

**Official metadata** (openFDA, 46 requests): a record for 1,430 of 1,430 submissions. It disagrees with the list for 19 of 1,430: applicant 15, device name 2, product code 2 (K223757 listed LLZ, official QIH; P950009 listed NMN, official MNM), decision date 1 (P980025 listed 1998-09-02, official 1998-09-25). Both values are stored; neither replaces the other. The Statement-or-Summary flag on the 1,376 510(k)s: Summary 1,367, Statement 8, blank 1. openFDA exposes no predicate and no study identifier; recorded as `predicate_absent_reason: not_exposed_by_openfda`.

## 5. Document acquisition (measured)

### Availability

| Count | Numerator / denominator |
|---|---|
| Addresses tried or recovered | 1,566 |
| Documents retrieved (bytes held by sha256) | 1,532 of 1,566 |
| … fetched from FDA this run | 1,520 |
| … recovered from local disk, not downloaded again | 12 |
| Not found (HTTP 404) | 34 of 1,566, all URL-pattern probes |
| Other failures | 0 of 1,566 |
| Submissions with at least one document of their own | 1,429 of 1,430 |
| Submissions with a substantive readable document | 1,388 of 1,430 |

By pathway, submissions with a document of their own / with a substantive readable one:

| Pathway | Submissions | With a document | With a substantive readable document |
|---|---|---|---|
| 510(k) | 1,376 | 1,376 | 1,340 |
| De Novo | 37 | 37 | 37 |
| PMA | 16 | 16 | 11 |
| PMA supplement | 1 | 0 | 0 |

By decision year, submissions with a substantive readable document: ≤2015 32 of 42; 2016–2018 107 of 110; 2019 77 of 80; 2020 109 of 114; 2021 126 of 130; 2022 158 of 162; 2023 221 of 226; 2024 231 of 235; 2025 327 of 331. A document of some kind is held for every submission in every band except the 2024 supplement.

Volume: 1,845,175,799 bytes; 19,145 pages; 18,019 pages with usable text; 942 pages with no text layer; 184 pages with a garbled text layer. Largest file 26.2 MB (P200003 labeling). No file was skipped for size or disk budget.

### Document types (1,532 retrieved)

| Type | Count | Category | Demographic summary candidate |
|---|---|---|---|
| `510k_summary` | 1,365 | 510(k) summary | yes when readable |
| `510k_decision_summary` (FDA review memo, IVDs) | 36 | decision summary | yes when readable |
| `de_novo_decision_summary` | 37 | decision summary | yes when readable |
| `pma_ssed` | 13 (12 own, 1 of the original PMA of the supplement) | summary of safety and effectiveness | yes when readable and own |
| `de_novo_granting_letter` | 37 | granting letter | **no** |
| `pma_approval_order` | 13 (12 own, 1 parent) | granting letter | **no** |
| `510k_clearance_letter` | 10 | granting letter | **no** |
| `510k_statement` | 1 | 510(k) statement | no |
| `pma_labeling` | 16 (15 own, 1 parent) | labeling | no |
| `unclassified_no_text` | 4 | unclassified | no |

Basis of the type: the document's own text for 1,489; FDA's label alone for 9 (no text layer); FDA's label because only the letter is readable for 23; FDA's label because the text matched no rule for 7 (labeling, and one De Novo summary that is mostly scanned); none for 4.

Text status: all pages readable 1,113; some pages readable 406; image only 12; extraction failed 1.

**The 10 granting letters the pilot read are confirmed as letters** (all 10 De Novo files on disk are `de_novo_granting_letter`), and the decision summary for each is now held as a separate document.

**A file FDA labels "Statement" is the clearance letter.** For the 8 submissions flagged Statement, the linked file contains the letter and the indications form and no statement of safety and effectiveness; typed `510k_clearance_letter` (K150102, K173749, K182181, K192571, K232367, K233826, K242825, K250754). These 8 have no public summary at all.

### Retrieval failures

| What | Count | Status | Meaning |
|---|---|---|---|
| URL-pattern probes that returned 404 | 34 | 404 | Not missing documents. 24 are PMA name variants that do not exist (`…A/B/C` for single-file PMAs, the bare number for lettered ones), 4 are supplement patterns for P140011/S008, 4 are `reviews/` probes for 510(k)s, 2 are DEN130013 under its own number. |
| FDA database pages answered HTTP 400 | 4 of 1,431 on first request | 400 | Transient: K201232, K173872, K182310, K181502 returned a bare 400 and were served normally on the rerun. The log keeps both answers. This is why a non-200, non-404 answer is asked again. |
| Submission with no document of its own | 1 of 1,430 | page lists none; 4 patterns 404 | P140011/S008. The original PMA's order, SSED and labeling were fetched and are marked `parent_submission`. |
| Network errors, timeouts, non-PDF responses, oversize | 0 | — | — |
| ClinicalTrials.gov searches answered HTTP 400 | 4 of 2,862 | 400 | Two device names the search syntax rejects (K212265 "TEX20/TEX20 Pro/…", K043341 "BIOPLEX 2200 … WITH …"). Their name route is `search_incomplete_or_blocked`. |

### Parsing failures

| What | Count | Consequence |
|---|---|---|
| Image-only documents (scans, no text layer) | 12 of 1,532 | K003301, K955841 (510(k) summaries), K130831 (a 44-page review memo), P000041 (order, SSED, labeling), P090012 labeling, P110014 order, and the single-file PMAs P940029, P950009, P970058, P980025 (280 pages). Not scanned; need OCR. |
| Documents with garbled pages | 58 of 1,532; 184 pages | Fonts without a character map: the extractor returns control characters or shifted letters. Those pages are treated as having no text. poppler garbles them the same way. |
| Summary on unread pages | 23 of 1,532 | FDA labels the file a Summary, only the letter is readable; typed by label, **not** treated as read. |
| PDF portfolio | 1 of 1,532 | K183593: only the cover notice is readable; recorded as a failed extraction. |
| Documents with at least one unread page | 418 of 1,532 | 1,126 pages of submissions' own documents were not read. 375 of the 1,379 submissions whose citation scan reports "no candidate" have at least one such page; the cell's reason says so. |
| Pilot model outputs malformed | 4 of 24 | Haiku on DEN140025, DEN170022, DEN170043, DEN170073: demographic sections missing. One more (Opus, DEN160026) is nested one level too deep and was read. |

No OCR was run. 36 510(k)s, 5 PMAs and the supplement have no substantive readable document for these reasons or because only a letter exists.

## 6. Linkage (measured, with rule findings marked)

### Route: NCT ids cited in FDA documents

| Count | Numerator / denominator |
|---|---|
| Readable documents containing an NCT id | 11 of 1,519 |
| Distinct NCT ids | 14; all 14 resolve on the registry |
| Submissions whose own document cites a resolving id | 9 of 1,430 |
| Identifier-confirmed records / pairs / studies on this route | 14 / 13 / 13 |
| Citation only in the original PMA of a supplement (family level, not validated) | 1 (P140011/S008, NCT01373671) |

| Submission | Device | Document | NCT id(s) | What the sentence says (my reading) |
|---|---|---|---|---|
| DEN200055 | GI Genius | decision summary p. 6, 15 | NCT01694966, NCT04079478 | first is a drug study whose videos were reused as test data; second is the device's own trial |
| K173821 | LungQ Software | 510(k) summary p. 10 | NCT01101958 | scans taken from a multicentre study |
| K200667 | EyeArt | 510(k) summary p. 6 | NCT03112005 | "validated in a prospective, multi-center pivotal clinical trial" |
| K213357 | Study Watch with Irregular Pulse Monitor | 510(k) summary p. 5 | NCT04546763 | algorithm validation data |
| K240712 | icobrain aria | 510(k) summary p. 6 | NCT01677572, NCT02477800, NCT02484547 | aducanumab drug trials whose MRI scans were development data |
| K241676 | TriVerity | 510(k) summary p. 21 and review memo p. 17 | NCT04094818 | the clinical performance study |
| K243812 | Volta AF-Xplorer | 510(k) summary p. 9 | NCT04702451 | a randomised trial of the device |
| K251221 | Assert-IQ | 510(k) summary p. 9 | NCT06172699 | "post-market study" of the predecessor |
| P230022 | Jewel Patch WCD | labeling p. 8 | NCT05201495, NCT05490459 | the IDE study and a second study |

Four of the nine are clearly the device's own validation study; the others cite data sources. This is why the confirmed relationship is "cited" and the stronger labels need a reviewer.

Other identifiers recorded and not followed: 126 DOIs, 47 PMIDs, 6 IDE numbers (G070182, G100247, G090108, G180065) in PMA documents; no ISRCTN, EudraCT, ChiCTR, ACTRN, DRKS, UMIN/jRCT or CTRI number in any document. FDA numbers other than the document's own (predicates and reference devices) are listed per document and never feed a study link.

### Route: registry records citing a submission number

1,430 numbers searched (58 batched requests, 75 follow-ups, 4 full records). 4 numbers returned a record; 2 of 1,430 carry the exact number in a retrieved field (identifier-confirmed, 5 records, 2 pairs): DEN170073 ↔ NCT04608617 (intervention name "Viz LVO (De Novo Number DEN170073)") and K240036 ↔ NCT07178080 (description, intervention description and a link to the FDA file). 2 hits were rejected by rule because no returned field contains the number (K202212 ↔ NCT06575179, K202404 ↔ NCT06442943). Identical to the audit.

### Route: device name and organisation (family level)

2,862 distinct searches (intervention, quoted phrase, sponsor) plus 87 follow-ups for answers cut off by the page size; 28,265 study readings (a study is read once per family that returned it).

| Outcome of a reading | Count | Becomes |
|---|---|---|
| Name in a title or intervention name **and** sponsor or collaborator agrees | 258 | `name_and_organisation_candidate`, family level, not validated |
| Name in a title or intervention name, no organisation agreement | 290 | `name_only_candidate`, family level, not validated |
| Same, but the name floods the registry (a name query matching 50 or more records) | 1,647 | kept as a record, `rejected_by_rule` |
| Sponsor agrees, device not named | 9,454 | no record; tallied on the coverage cell |
| Name only in an intervention description | 1,169 | no record; tallied |
| Only the manufacturer's own name | 7 | no record; tallied |
| Neither | 15,440 | no record; tallied |

The 1,647 rejected matches come from 42 families; three names account for 921 of them: "Loop System" (493 on "loop"), "Contour+" (206), "Aurora" (111 for each of two unrelated devices). In the 258 name-and-organisation candidates the manufacturer is the lead sponsor in 202 and a collaborator in 56; 40 matched on a trimmed name form and are low confidence.

### Unique studies, pairs and records

| | Evidence records | Distinct pairs | Distinct studies | Entities |
|---|---|---|---|---|
| **Submission-specific** (identifier-confirmed) | 19 | 15 submission–study | 15 | 11 submissions |
| **Family-only**, name and organisation | 258 | 258 family–study | 240 | 108 families |
| **Family-only**, name only | 290 | 290 family–study | 271 | 93 families have only these |
| **Rejected by rule** | 1,649 | 1,649 | 1,504 | 2 submissions, 42 families |
| **Unresolved** (cited id the registry does not hold) | 0 | 0 | 0 | — |
| Parent-document citation (supplement) | 1 | 1 | 1 | P140011/S008 |
| **All** | 2,217 | 2,213 | 1,980 | |

60 studies are joined to more than one entity (for example the same four studies match the families "FFRangio" and "FFRangio System"), and one family is typically joined to several studies. Human review: 0 of 2,217 records reviewed.

### Timing (described, not used)

Per record and per member submission. Identifier-confirmed citations in documents: 14 of 14 had primary completion on or before the decision. Identifier-confirmed registry citations: the DEN170073 study started more than a year after the decision, the K240036 study within a year after it. Name-and-organisation candidates, 414 family-member rows: completed on or before the decision 161; started before and not complete 82; started within a year after 52; started more than a year after 117; indeterminate at month precision 2. None of this changes a relationship or a status.

### Coverage by route (submissions, n = 1,430)

| Route | verified link | candidate awaiting review | no candidate in completed scope | available document insufficient | document unavailable | search incomplete or blocked | not searched |
|---|---|---|---|---|---|---|---|
| Local document | 0 | 0 | 2 | 10 | 1,418 | 0 | 0 |
| FDA document fetch | 9 | 1 | 1,377 | 40 | 2 | 1 | 0 |
| openFDA metadata | 0 | 0 | 0 | 1,430 | 0 | 0 | 0 |
| NCT-id citation scan | 9 | 0 | 1,379 | 40 | 1 | 1 | 0 |
| Submission-number registry search | 2 | 0 | 1,428 | 0 | 0 | 0 | 0 |
| Name/organisation registry search | 0 | 289 | 1,099 | 0 | 0 | 19 | 23 |
| Demographic extraction | 0 | 0 | 0 | 8 | 0 | 0 | 1,422 |

Overall, over the three routes that can produce a link: verified link 11; candidate awaiting review 282; search incomplete 18; not searched 23 (names made only of device-type words); available document insufficient 36; document unavailable 1; no candidate in the completed scope 1,059.

Changed after this run (investigator's decision, 2026-11-14): a citation scan with any unread page and no identifier found now reads search incomplete or blocked, not a completed scope. The counts above are this run's; the current counts are in `docs/device_links/runs/2026-11-14/README.md`.

## 7. Negative findings

Kept apart from failures: these routes ran and found nothing, or found something that is not a link.

1. **Study described, no registry identifier (rule finding).** 873 of 1,422 substantive readable documents contain study language and no NCT id: 642 with human-subject wording (clinical study, pivotal, reader study, prospective or retrospective collection, enrolment), 231 with dataset-testing wording only (standalone performance, "N patients/cases"). 159 say only that no clinical study was done; 381 have no study language; 9 cite an id. The keyword rule is not validated; `analysis/study_language_review_sample.csv` holds 30 of the 873 for a reader (drawn with `random.Random(20261114).sample` from the 873 sorted by document id; ids in the file).
2. **No registry citation of the number**: 1,428 of 1,430 submissions.
3. **No name candidate after complete searches**: 1,099 of 1,430 submissions.
4. **Sponsor agrees, device not named**: 9,454 readings. Not candidates; visible per submission in `coverage.json`.
5. **The pilot extractions hold no demographic value.** 24 outputs (8 documents × 3 models), all on granting letters: 485 demographic fields parsed, 0 reported; 0 describe a study population; 9 quote an intended-use age range, which is kept apart as a statement about who the device is for. Each is attached to its letter with `population_identity: unresolved` and `may_be_added_to_registry_counts: false`.
6. **Granting letters contain study words without describing a study**: 20 of 37 De Novo letters trip the keyword rule on special-controls language ("standalone performance testing"). Letters are excluded from finding 1 for that reason.
7. **A document missing from local disk is not a missing document**: 0 of the 1,420 local paths the inventory names exist; 1,418 submissions read "document unavailable" on the local route and have their document from FDA.

## 8. The audit's figures, interpreted

**How the 80 were sampled** (`device-audit/scripts/04_sample.py`). Frame: one row per distinct submission number, 1,430. Strata: decision year with sparse years pooled (≤2015, 2016–2018, then each year 2019–2025). Allocation proportional to stratum size with a floor of one, by largest remainder: 2, 6, 5, 6, 7, 9, 13, 13, 19 of 42, 110, 80, 114, 130, 162, 226, 235, 331. Draw: `random.Random(20261113)`, one generator used across strata in sorted order of stratum label, `sample()` over the stratum sorted by submission number. Sampled identifiers: `device-audit/sample_devices.csv`.

**What 11 of 80 can and cannot be generalised to.**

* It is a proportionally allocated stratified random sample of submissions, so 13.8% (the weighted and unweighted estimates coincide; Wilson 95% interval 7.9–23.0%, which ignores the stratification) estimates, for the 1,430 submissions on the 2026-03-05 list, the share with at least one string-evidence candidate **under the audit's rule and queries**.
* It is not a rate of true links: no candidate was reviewed, and the audit's own reading doubted 1 of the 11 and called 2 more family-level.
* It is not a rate for products or families (the unit is the submission; a product cleared several times is drawn several times), for rows, for a pathway (2 De Novo, 2 PMA), or for any other list or date.
* It no longer needs generalising. The build ran the route on all 1,430 submissions: 169 of 1,430 (11.8%) belong to a family with a name-and-organisation candidate under the build's stricter rule. On the audit's 80 the same rule gives 9 of 80 (11.2%), consistent with the census.

**Are the 34 matches studies or pairs?** They are 34 device–study pairs over 11 sampled submissions, and also 34 distinct studies (no study recurs in the sample). They are family-level: the 9 FFRct studies attached to K161772 match K152733 and K182035 equally. In the build they are counted under the family (HeartFlow "FFRct": 10 candidates for one family of 3 submissions).

**Regression on the audit's sample** (`analysis/audit_regression.json`).

| Audit class | Devices | In the build |
|---|---|---|
| (i) name and sponsor | 11 | 9 name-and-organisation; 1 name-only (qER: sponsor agreed only at the audit's "brand" tier, which the build does not have); 1 rejected by rule (SIGNA 7.0T, the one the audit would not accept) |
| (ii) name, other sponsor | 5 | 3 name-only; 1 rejected as a common word (Aurora); 1 none (Athelas One: only the manufacturer's name matched) |
| (iii) sponsor only | 41 | 0 candidates: 35 no candidate, 6 not searched (generic name) |
| (iv) nothing | 23 | 0 candidates: 21 no candidate, 2 not searched |

All 34 audit class (i) pairs are present in the build: 32 as name-and-organisation, 1 name-only, 1 rejected by rule. The four submission-number hits agree exactly (2 confirmed, 2 rejected). The build's deeper pages return 158 further family–study pairs for the sampled families, 139 of them common-word matches rejected by rule.

Audit statements this run corrects: "14 of 21 pairs differ only as Pearl Inc. / Pearl, Inc." (2 of the 14); "about 555 manufacturers" (554 under the rules here, 748 raw strings); the PMA and De Novo document paths it could not verify (section 3); 3 malformed Haiku outputs (4).

## 9. Engineering validation and scientific validation

**Engineering** (the rules are applied as written; the files are consistent; the build reproduces):

| Check | Result |
|---|---|
| `python -m pytest tests/devices -q` (offline, fictional records) | 215 passed, 1 skipped (the workflow check, which needs the repository) |
| `scripts/check_device_links_contract.py out` | PASS, 0 problems |
| Two builds from the frozen cache with a pinned timestamp | byte-identical, and identical to `out/` (`analysis/artifact_sha256.txt`) |
| Every function annotated (AST check over `engine_staging/`) | 0 without full annotations |
| Content-addressed store after the restart | 1,483 of 1,483 files matched their sha256 before resuming |
| Frozen archives | sha256 recorded in `run.json`; the build refuses bytes that do not match |
| Rate limit | 0.55 s between request starts per host; tested |

Run on Python 3.11.17 with pypdf 7.1.0. Not run: Python 3.12, the engine's full suite, `compileall` over the whole repository, the documentation-wiring tests (the package is not in the repository yet).

**Scientific** (is a link right, is a finding true):

| Check | Result | What it does and does not show |
|---|---|---|
| NCT ids found by a second extractor (poppler `pdftotext`) | same set as pypdf in 1,532 of 1,532 document versions; 11 with any id in both | The scan's recall does not depend on the extractor. Both fail the same way on scans and garbled fonts. |
| Cited NCT ids resolve on the registry | 14 of 14 | The identifiers are real. Not that the study concerns the device. |
| Document type from text against FDA's own label | agree 1,478, disagree 11, of 1,489 typed from text with a label | The classifier matches FDA's labelling; 8 of the 11 are the "Statement" files, where the text is right about the content. |
| Regression against the audit's 80 and its number search | section 8 | Two independent implementations agree on what the strings show. Neither confirms a link. |
| Human review of links | 0 of 2,217 | **No link is scientifically validated.** |
| Study-language keyword rule | not validated | A 30-document sample is prepared for a reader. |
| Common-word rule (50 or more records) | not validated | Threshold hand-set; known to set aside true investigator-led studies with the coincidences (FFRct). |

## 10. What is provisional, and the review list

Provisional: every family (all candidate); every organisation merge beyond spelling; every link's meaning; the study-language findings; the flood threshold; the generic-word list; the classification of the 23 label-typed summaries; the demographic layer (no value yet).

**Priority 1: identifier-confirmed, awaiting a reading of what the citation means (11 submissions, 19 records).** K240712 icobrain aria (3 studies), DEN200055 GI Genius (2), P230022 Jewel Patch WCD (2), K241676 TriVerity, K240036 PIUR tUS Infinity, DEN170073 ContaCT, K173821 LungQ, K200667 EyeArt, K213357 Study Watch, K243812 Volta AF-Xplorer, K251221 Assert-IQ. A substantive document is held for all 11 and none has a demographic extraction.

**Priority 2: name-and-organisation candidates (108 families, 258 records; 169 submissions).** Most evidence first: LiverMultiScan (15 studies; K213960, K202170), FFRct (10; K152733, K161772, K182035), Imagio Breast Imaging System (8; P200003), PMD-200 (8; DEN210022), EyeBOX (7; DEN170091, K191183, K201841), FibriCheck (6), TipTraQ (6), Study Watch (6), MATRx plus (5), SOZO (5; four submissions), TAVIPILOT (5), Dario (5), Kosmos (5). For a family of several submissions the reviewer's first question is which submission, if any.

**Priority 3: documents held, no demographic evidence (1,388 submissions).** Ordered in `review_queue.json`: the 11 of priority 1 first; then documents whose text mentions race or ethnicity (335 submissions: 309 510(k), 20 De Novo, 6 PMA); then sex or gender only (343); then neither (710). The 12 PMA SSEDs and 37 De Novo decision summaries are the densest.

**Priority 4: name-only candidates (93 families with nothing stronger).**

**Also for a person:** alias rows suggested by the official records and the audit, none applied: `iSchema View Inc.` / Ischemaview; `MEDICALIP` / Medical IP; `Body Vision Medical` / Bodyvision; `Nico.Lab` / Nicolab; `Cella Vision` / CellaVision; `Tempus Labs` → Tempus AI; `Neuromedical Systems` → BD Diagnostics (P940029); `Viz.Al` / Viz.ai; `KeyaMed NA` / Keya Medical. OCR for 12 scanned documents and the unread pages of 418.

## 11. Where the requirements had to be interpreted

1. **"Verified link" as a coverage state** is the automated status (`identifier_confirmed`); human review is reported beside it. Nothing is called verified on name evidence.
2. **`rejected_by_rule` on common-word name matches** departs from the brief's "everything heuristic stays `not_validated`". Without it 1,939 name-only records would await review, 1,647 of them on words like "loop". They remain stored and reviewable. If you prefer the literal rule, it is one line in `links.from_name_match`; the queue then needs another filter.
3. **Review controls and evidence "within the AI Devices tab"**: this repository emits data only and I may not edit the site. The artifacts carry what the tab needs (evidence with quote and location, coverage per route, the review queue, review status); the control itself is `devices/reviews.csv`, changed by pull request. A tab-side control is site work.
4. **Curated families**: none exists. Candidate families are deliberately narrow; "BriefCase" and "BriefCase-Triage" stay apart.
5. **openFDA route** is always `available_document_insufficient` when a record is found, because the record cannot answer the linkage question. The overall status therefore uses only the three routes that can produce a link.
6. **Demographic extraction** is a seventh route, so that "no extraction run" is visible per submission.
7. **Labeling** is kept and typed but not treated as a demographic summary, although P230022's labeling is where its trials are cited.
8. **Local documents**: registered as fetched from the row's `pdf_url` on the date in the file name, HTTP status absent. They were not downloaded again, so equality with FDA's current file is not checked (one probe matched in size).
9. **Supplement**: the original PMA's documents are held as `parent_submission` and cannot support a supplement-level link.
10. **Submission-specific without an identifier**: implemented as `documentary_statements.csv` (a person enters the quote and location; the build checks the quote stands in the source). The file is empty; the path is tested.
11. **Source of record**: the enriched CSV, compared row by row with FDA's list.
12. **Thresholds set by hand**: flood at 50 records; follow-up page of 1,000; a page "has text" at 20 letters or digits; garbled-page rule; three section markers for a headless 510(k) summary.
13. **Requests carry a generic client name** and nothing identifying a person or project.

## 12. The interruption

The container restarted at about 23:57 UTC while documents and registry searches were downloading. On resuming: 1,483 stored files were re-hashed and all matched; the logs ended on complete lines; the remaining 68 document addresses, 4 retried pages and the outstanding searches were fetched without repeating anything recorded. Changes made because of it: files are written under a temporary name and renamed; an interrupted final log line is dropped with a message; text extraction runs one document at a time; `--store-budget-gb` stops acquisition at a disk limit and records the rest as incomplete (the limit of 2.8 GB was not reached; the store is 1.76 GB).

## 13. Running it

```bash
cd engine_staging                      # or the engine repository once moved
PY=/home/user/civicsample-engine/.venv/bin/python
$PY -m pytest tests/devices -q
$PY scripts/build_device_links.py build --cache ../cache \
    --inventory /home/user/civicsample-engine/data/ai-ml-enabled-devices-enriched.csv \
    --source-list /home/user/civicsample-engine/data/ai-ml-enabled-devices-csv_20260305.csv \
    --site /home/user/clinical-trial-populations --out ../out
$PY scripts/check_device_links_contract.py ../out
# to refresh or resume acquisition (network; resumable; nothing recorded is fetched again):
$PY scripts/build_device_links.py acquire --cache ../cache --inventory … --store-budget-gb 2.8
```

For moving it into the repository: add `-r devices/requirements.txt` to `requirements-dev.txt` (the tests import pypdf for the extractor version); add a row to the "Start here" table of `AGENTS.md` and the directories to the `compileall` gate if wanted; keep the cache outside the repository; `scripts/build_device_links.py` defaults `--inventory` and `--local-documents` to `data/` in the repository.
