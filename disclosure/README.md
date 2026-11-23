# Drug trial cross-source evidence & disclosure timeline (manual)

Links registered drug and biological trials across **ClinicalTrials.gov API v2**, **PubMed / Europe PMC**, and **openFDA Drugs@FDA**, computing date-precision-aware disclosure timelines, 5-route search coverage, cross-source discordance states, and curated sponsor rollups.

It runs by hand; no scheduled workflow invokes it, and the weekly demographics pipeline, its cohort, and its published files are untouched.

## Entities & curated inputs

| Entity / File | Identifier | Role |
|---|---|---|
| Registered trial | `NCT\d{8}` | `studies.json`, `coverage.json`, `timelines.json` |
| Evidence link | `link:<sha256[:16]>` | `disclosure/evidence.py`, `links.json` |
| Curated human reviews | `disclosure/reviews.csv` | `link_id,nct_id,reviewer,reviewed_at,decision,notes` (header-only until edited by a human; never written by code; private `reviewer` names are never copied to artifacts) |
| Curated documentary statements | `disclosure/documentary_statements.csv` | `statement_id,nct_id,source_type,source_locator,statement_date,verbatim_excerpt,recorded_by,notes` (header-only until edited by a human) |
| Sponsor rules (read-only) | `sponsors/company_aliases.csv` | Curated attribution rules pinned by sha256; never modified here |

## Automated validation vs human review

`automated_validation` records mechanical checks only:
- `identifier_confirmed`: an identifier stands in the retrieved source and resolves (posted `resultsSection` on CT.gov, PubMed-resolved PMID in `referencesModule`, exact `NCT_ID` token in an Europe PMC record, or exact `NCT_ID` token in an openFDA `drugsfda` record).
- `quote_confirmed_in_source`: a curated row in `disclosure/documentary_statements.csv` whose `verbatim_excerpt` stands at `source_locator` in a frozen retrieved source. The excerpt is compared only with the value the locator names (`pmid:<PMID>` with no sub-path names that PMID's whole esummary record, `result[PMID]`, and `pmid:<PMID>:<path>` a value inside it; both had read the whole response, so an excerpt standing in a sibling PMID's entry was confirmed, and a response without that PMID's entry, or with NCBI's error for it, does not resolve), never with the rest of the response. A statement counts as a results publication in the timeline only when it is both quote-confirmed and reviewed as `confirmed_same_trial_results`, the same two conditions its coverage cell requires. A statement reviewed as `confirmed_same_trial_results` whose quote was not confirmed is not awaiting review: its cell reads `search_incomplete_or_blocked`, with the failed check in its reason and `detail.statement_checks`, it is left out of `n_unsettled_publication_candidates`, and it keeps the trial at `coverage_incomplete_or_awaiting_review` until the excerpt or locator is corrected or the review is changed.
- `identifier_not_resolved`: a cited PMID that fails resolution in NCBI PubMed `esummary` (404 or error).
- `rejected_by_rule`: a full-text search hit lacking an exact `NCT_ID` token in any retrieved field, or a high-volume generic drug query (`>= 25` applications) without sponsor agreement — retained for audit visibility and set aside. A person can overrule the rule: reviewed as `confirmed_same_trial_results` the link counts like any confirmed link, and reviewed as `indeterminate` it is a live candidate. The site export follows the same rule (`coverage.set_aside_by_rule`), so `evidence_set_aside.json` holds only the rejections that still stand after review and every link the coverage or timeline relies on is in `evidence.json`.
- `not_validated`: heuristic sponsor+drug NDA/BLA candidates, free-text citations without a PMID, or statements whose excerpt was not confirmed; `detail.statement_check` and the validation reason say why (`source_not_retrieved`, `locator_not_resolved`, `excerpt_not_found_at_locator`).

Drug query terms come from every DRUG, BIOLOGICAL or COMBINATION_PRODUCT intervention: its name and every registered alias (`otherNames`), since the generic ingredient Drugs@FDA indexes can be any of them. Each label is normalized by removing parenthesised text, strengths (`DRUG_STRENGTH_RE`; a comma-grouped amount such as `50,000 IU` is one strength, in a range or in every slash-separated component, as in `24,000/76,000/120,000 units`; a hyphen may join the amount to its unit, as in `Prednisone 10-mg`, while `5-FU` is kept), punctuation, and whole-token dosage-form, route and release words (`DOSAGE_FORM_WORDS`; the abbreviations in `DOSAGE_FORM_ABBREVIATIONS` only when no number follows, so `SR 96669` keeps its code). Dosing schedules and dose levels standing at either end of a label are removed too (`Tiotropium 18µg once daily`, `Metformin 500 mg bid`, `Pembrolizumab 200 mg Q3W`, `Empagliflozin high dose`, `PF-07321332 Dose 3`, `Low-dose cytarabine`), since no Drugs@FDA name carries them; at an end, the shorter phrase can only widen what the query matched, and the same words in the middle of a label are kept (`Pronamel Daily Protection`). A combination label (`Nivolumab + Ipilimumab`, `Nivolumab/Relatlimab`, `A and B`, `A or B`) is also queried component by component, since Drugs@FDA may index each ingredient separately; it is split only after strengths are removed, so `5 mg/mL` never splits. The openFDA query matches the term as a phrase in name fields, and Drugs@FDA records the dosage form and route separately, so `"metformin tablet"` misses an application named METFORMIN HYDROCHLORIDE. Removing words at either end of a label only widens what its phrase matches; a form word in the middle (`eribulin injection halaven`) gives a different phrase. A changed term has a new query key, so a cache acquired earlier reads `search_incomplete_or_blocked` for it until `acquire --stage openfda` fetches it. The route is also `search_incomplete_or_blocked` when a trial's drug queries could not be planned: its CT.gov record was not retrieved (other than a 404), or its interventions module, list or an entry has the wrong type, or an entry's `type` or `name` is blank (`acquire.drug_planning_problem`; a blank name planned no term and a blank type hid a drug, so the route completed over the NCT query alone). A record with no `armsInterventionsModule` at all is read as registering no interventions, a state 26 of 3,371 stored unprojected records show (all interventional), so it cannot be told from a field-projected record that dropped the module. A Drugs@FDA record that carries the trial's NCT ID, whether the direct NCT query or a drug-term query returned it, gives the verified link the action date of the submission carrying it (`evidence._cited_submission_date`), not the application's original approval: a trial cited in a 2025 supplement of an application first approved in 2000 had been given the 2000 date. Every exact mention is read, and the citing submission with the earliest action date gives the link its date and locator (`evidence._nct_citation`): taking the first mention in field order gave a missing date when the application's own fields named the trial ahead of a dated submission, and the later action when a later supplement was listed first. An NCT ID found only outside any submission, or only in undated ones, ties the trial to no action, so the link's date is unknown and says why. A `submission_status_date` is read only as a string (`evidence._submission_date`): a number such as `20250102` had been turned into the date 2025-01-02, so a submission dated that way counts as undated, for a verified link and a candidate alike. A submission dated after the day its record was fetched is not used either (`evidence._after_retrieval`): `2100-01-02` in a record fetched in 2026 had become the trial's earliest FDA action. The link's reasons name the date, and the contract refuses an FDA action dated after `generated_at`.

When several drug queries for one trial return the same openFDA application, one link is kept for it: the strongest classification (`identifier_confirmed` > `not_validated` candidate > `rejected_by_rule`), ties broken by query term. A flooded generic query therefore cannot hide a precise query's reviewable candidate; `detail.drug_queries_returning_application` lists every query that returned it.

Unreviewed links are **never** called "validated". Semantic confirmation (`review_status` in `{"unreviewed", "confirmed_same_trial_results", "confirmed_background_only", "rejected_false_positive", "indeterminate"}`) comes solely from `disclosure/reviews.csv`. The one exception is the `ctgov_results` link, which is generated from the trial's own registry record (`hasResults=true` and a non-empty `resultsSection`): a review marking it `rejected_false_positive` or `confirmed_background_only` is recorded on the link but leaves the route at `verified_link`, since a review cannot unpost registry results; `indeterminate` holds it at `candidate_awaiting_review`. A link a person has reviewed as `confirmed_same_trial_results` settles its route as `verified_link` whatever the mechanical check said, matching the timeline, which counts every such link; the cell's `n_human_confirmed_only` says how many verified links rest on review alone. The contract check refuses a "CT.gov unreported" discordance state on any trial whose `hasResults` is not exactly `false`.

A `link_id` hashes everything the link presents as evidence: trial, route, target, citation type, locator, excerpt and `publication_or_action_date`. When any of these changes on a later fetch (for example an openFDA application gains an approval date under the same excerpt), the review keyed on the old `link_id` is reported in `run.json` `reviews.orphaned_link_ids` instead of carrying onto the changed record. A review for a trial outside the build (a `build --nct-list` subset, or a trial not in the cache) is not an orphan: it is listed in `reviews.out_of_scope_link_ids`, so `n_rows = n_applied + orphaned + out of scope`. The whole-response `source_sha256` is recorded on each link but is not part of its identity, since it also changes for reasons unrelated to the linked record (openFDA's `meta.last_updated`, other search hits, any CT.gov record update).

## 5-route × 7-state coverage (`coverage.json`)

Routes: `ctgov_results`, `registry_references`, `literature_search`, `openfda_drugsfda`, `documentary_statements`.

A registry reference whose PubMed lookup failed, or never ran because the cache holds no literature archive (a `freeze-local` build), makes that route `search_incomplete_or_blocked`, never a candidate. A lookup has succeeded only when NCBI's answer carries an entry for the PMID with its `uid`, or an `error` saying it does not resolve (`acquire.pubmed_esummary_valid`); an answer with an empty entry or none is a failed lookup, at acquisition and in a cache frozen before that rule. In the same way a Europe PMC answer counts only when every hit carries an identifier (`id`, `pmid`, `pmcid` or `doi`) and a title, with every identifier it carries a string (a numeric `pmid` beside a string `id` was turned into text and became the target of a verified link), and an openFDA Drugs@FDA answer only when every result names its `application_number`: a hit with neither became a placeholder record that read as a completed negative (Europe PMC) or a verified FDA link (openFDA), so such an answer leaves its route `search_incomplete_or_blocked`. A Europe PMC hit's date is its first or electronic publication date, else its `pubYear` or `journalInfo.yearOfPublication` read only as a four-digit year (`evidence._epmc_year`): `pubYear: 20250102` had become the day 2025-01-02. So does a `referencesModule` that cannot be read: a module that is not an object, a `references` value that is not a list, an entry that is not an object, an entry whose `pmid`, `citation` or `type` is not a string, or an entry with no citation text and no PMID (`acquire.registry_references_problem`). Such a record is not read as citing nothing, and an unreadable entry yields no link: one with neither citation nor PMID is not turned into a placeholder candidate a review could reject, and one with a mistyped field (an integer PMID) cannot verify the route by coercion; only a record with no `referencesModule`, no `references` key or an empty list is a completed search over zero citations. A readable entry that verifies (a resolved RESULT PMID, or a person's `confirmed_same_trial_results`) still settles the route; otherwise the route reads `search_incomplete_or_blocked`, ahead of the candidate, insufficient and negative states, as a failed lookup does. None of the 3,371 stored unprojected records has such a module (1,859 have none, 329 have no `references` key): all 3,449 of their entries carry a citation string, 3,345 a PMID string too.

States: `not_searched`, `search_incomplete_or_blocked`, `document_unavailable`, `available_document_insufficient`, `candidate_awaiting_review`, `no_candidate_in_completed_scope`, `verified_link`. Only `no_candidate_in_completed_scope` is a negative finding; `not_searched` (e.g. when offline builds run from local CT.gov archives without literature or openFDA caches) is never conflated with "none found".

## Date-precision-aware timeline & descriptive 365-day window (`timelines.json`)

- **No fabricated `-01` days**: partial dates (`YYYY`, `YYYY-MM`) keep their exact `[earliest, latest]` calendar spans (`leap-year` aware).
  - Both `YYYY-MM-DD`: `state = "exact"`, `exact_days = int`.
  - Non-overlapping partial ranges: `state = "bounded_partial"`, `bounds_days = [min_days, max_days]`, `exact_days = null`.
  - Overlapping partial ranges: `state = "indeterminate_at_recorded_precision"`, `bounds_days = null`, `exact_days = null`.
  - An endpoint bounded on one side only (`bounded_above_only`, below): `state = "indeterminate_at_recorded_precision"`, `one_sided_endpoints` names it, and no exact, minimum or maximum days are computed.
  - Missing endpoint(s): `state = "endpoint_missing"`.
- **Earliest linked publication / FDA action**: the earliest date across qualifying links lies in `[min earliest, min latest]` of their spans; when that is tighter than any single date the milestone is `bounded_composite`. An undated qualifying link may precede every dated one, so then the milestone has `precision = "bounded_above_only"`, `date = null`, `earliest_bound = null` and only `latest_bound` (with `has_undated_qualifying_link = true`); no lag is computed from it. With no dated qualifying link the milestone is `missing`.
- **QC lag & cycles**: `resultsFirstSubmitDate -> resultsFirstPostDate` plus `derivedSection.miscInfoModule.submissionTracking.submissionInfos` cycle counts (kept `null` when absent, never coerced to `0`). That is where CT.gov API v2 records them: all 23 stored local records that carry `submissionInfos` hold it there, and none under `protocolSection.statusModule`, which the module read before, so every real trial's history read as absent. A history that cannot be read whole (an entry that is not an object, or a date or `mcpReleaseN` of another type) is published with no counts and `absent_reason: submissionInfos_malformed_entry`, not counted in part.
- **Descriptive 365-day window**: `POSTED_WITHIN_365D`, `POSTED_AFTER_365D`, `PENDING_SUBMITTED_UNPOSTED`, `UNREPORTED_PAST_365D`, `UNREPORTED_WITHIN_365D`, `DATE_PRECISION_INDETERMINATE`, `COMPLETION_DATE_MISSING_OR_FUTURE`, `RESULTS_STATUS_INDETERMINATE`. Strictly describes registry dates; never claims legal FDAAA 801 / 42 CFR Part 11 non-compliance. Only a primary completion date typed `ACTUAL` opens an elapsed-window state; `ESTIMATED`, `ANTICIPATED`, a missing or an unrecognized type gives `COMPLETION_DATE_MISSING_OR_FUTURE` with the type in the reason. `PENDING_SUBMITTED_UNPOSTED` and the two `UNREPORTED_*` states need evidence that results were unposted on `as_of`: an explicit `hasResults=false`, or a `resultsFirstPostDate` after `as_of`. Without it (`hasResults` missing or non-boolean, or `true` with neither a results section nor a post date) the state is `RESULTS_STATUS_INDETERMINATE`, even when a `resultsFirstSubmitDate` is recorded; the contract refuses a pending or unreported window state that lacks this evidence. A month- or year-precision completion date that overlaps `as_of` is `DATE_PRECISION_INDETERMINATE` before any posted or submitted state is considered, since completion may fall after both.
- **Cross-source discordance**: `published_results_while_ctgov_unreported`, `fda_cited_while_ctgov_unreported`, `ctgov_posted_no_linked_results_publication`, `concordant_multi_source_disclosed`, `unreported_across_completed_routes`, `coverage_incomplete_or_awaiting_review`. The "CT.gov unreported" states need the CT.gov results route at `no_candidate_in_completed_scope`; a record with `hasResults=true` but no `resultsSection` (`available_document_insufficient`) is ambiguous and stays `coverage_incomplete_or_awaiting_review`, and so is a record that omits `hasResults` or carries a non-boolean value: only an explicit `hasResults=false` is the registry's negative. The summary's `trials_ctgov_results_posted` (and the `ctgov_posted_trials` denominator) counts the registry fact, trials whose record generated the `ctgov_results` link, whatever a review says; the discordance flag `ctgov_results_posted` follows the reviewed route, so an `indeterminate` review sets it false.
- **Sponsor rollups (`sponsors.json`)**: read-only `sponsors/` attribution providing both `current_owner` (`canonical`) and `as_registered` (`entity`) views while keeping `attributed`, `reviewed_excluded`, and `unreviewed` strictly disjoint and separate.

## Running it

```bash
# 1a. Live rate-limited (<= 2 req/s per host), resumable network acquisition + sha256 freeze:
python3 scripts/build_drug_disclosure.py acquire \
    --cache build/disclosure_cache --nct-list ids.txt

# 1b. Or freeze local CT.gov v2 study records (.json / .json.gz / .jsonl.gz) with zero network calls.
#     The code cannot know when local files were obtained, so both stamps are required (no defaults):
#     --fetched-at is when the records were retrieved from CT.gov (each record's fetched_at), and
#     --data-timestamp is the CT.gov dataTimestamp they reflect (run.json sources.ctgov.data_timestamp,
#     and the build's as_of when --as-of is omitted). manifest frozen_at is the time of freezing.
python3 scripts/build_drug_disclosure.py freeze-local \
    --records /path/to/ctgov_records --cache build/disclosure_cache \
    --fetched-at 2026-11-13T22:32:29+00:00 --data-timestamp 2026-11-13T10:00:06

# Freezing (either way) and loading refuse a cache in which any requested NCT ID has no CT.gov
# study response record (a raw log acquired for another NCT list): run the ctgov stage for it first.
# freeze-local refuses an --nct-list ID absent from the local records instead of recording a 404,
# refuses a trial that appears more than once with different records (identical copies are fine),
# refuses a study record that is not a valid CT.gov v2 study with an NCT ID (rather than freezing a
# smaller batch) or that holds a part the build reads with the wrong type (`acquire.CTGOV_STUDY_PARTS`;
# `statusModule: ["bad"]` froze a cache whose build raised), naming the part (a record with
# `hasResults: false` but a non-empty `resultsSection` or a results posting date, which contradicts
# itself, included), refuses an object in a local file that is none of a study, a `studies` page, a
# list of studies, a CT.gov /version answer or a response-log line (a study that lost its
# protocolSection, a `studies` value that is not a list), and a log line whose status is neither 200
# nor a failed request (null or another HTTP status), or whose 200 answer is none of those, all of
# which were skipped, refuses a --data-timestamp more than 14 hours after --fetched-at (CT.gov states no
# zone for dataTimestamp, so the largest real UTC offset is allowed) and a --fetched-at later than the
# freeze itself (records cannot be retrieved after the cache that holds them),
# and needs a new or empty --cache, so no earlier CT.gov version or other source's record is mixed in.
# They also refuse openFDA drug-query records made with an earlier query form: re-run the openfda stage.
# A CT.gov answer whose record is a different trial (its own nctId differs from the one requested, as an
# obsolete ID can be answered under the current one) is logged as unusable, never settled, and freezing
# and loading refuse a cache that holds one as settled: re-run the ctgov stage. An answer with a mistyped
# part (`acquire.ctgov_study_problem`) is never settled either, and if one reaches a build its trial reads
# as `lookup_failed` with every route incomplete, never a crash or a negative.

# 2. Offline deterministic build from frozen cache (runs contract check before writing):
python3 scripts/build_drug_disclosure.py build \
    --cache build/disclosure_cache --out build/disclosure --as-of 2026-11-01
# (build --records seeds an absent cache from local files like freeze-local, with the same required stamps.)
# run.json sources.ctgov lists every CT.gov version record in the cache (`versions`: each acquisition
# run observes /version before its first study request and after its last, never reusing an earlier
# run's answer; a failed observation stops the run, and freezing needs a successful one before the first
# study answer and after the last; an answer whose dataTimestamp is not in CT.gov's form, a whole date
# and time with no offset as --data-timestamp must be, counts as failed and is left out of `versions`)
# and the range of their data timestamps (`data_timestamp_first`,
# `data_timestamp_last`). Without --as-of, as_of defaults to the cache's CT.gov data timestamp
# (`as_of_source` records which default was used); a cache whose version records span several data
# timestamps (an acquisition resumed after, or running across, a CT.gov data refresh) is refused unless
# --as-of is given. An explicit --as-of may not be later than the earliest CT.gov data timestamp in the
# cache (else its earliest fetch): the frozen records cannot show what was posted after that date. The
# contract applies the same bound. --generated-at (like freeze-local's --fetched-at) must be a whole
# ISO 8601 UTC timestamp written with Z or +00:00, checked before anything is seeded and again by the
# contract; -00:00, which RFC 3339 uses for an unknown local offset, is refused. It may not be earlier
# than the cache's frozen_at either (build and contract): a build reads the frozen cache. freeze-local
# and build --records stamp frozen_at with the current time, so a build from a cache seeded today states
# a --generated-at no earlier than the seeding, or none. No reviews.csv row may be dated after the
# --generated-at day (build and contract): a build cannot include a later decision. Nor may a
# documentary_statements.csv statement_date fall after it (build and contract), at the precision it
# was written: a year or month containing the generation day is accepted.

# 3. Producer contract check and atomic site export. run.json producer.commit and
#    uncommitted_changes are null when git was missing or failed at build time (never false); such a
#    build can be inspected locally, but the export refuses it. A dirty tree (true) is exported.
python3 scripts/check_drug_disclosure_contract.py build/disclosure
python3 scripts/export_drug_disclosure_site.py build/disclosure /tmp/site_disclosure

# 4. Offline test suite:
pytest tests/disclosure -q
```

## Compatibility

New files only (`disclosure/`, three CLI scripts under `scripts/`, and `tests/disclosure/`). Zero existing tracked files, workflows, or published site contracts are modified.
