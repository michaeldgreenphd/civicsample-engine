# AI/ML device linkage (manual)

Links the devices on FDA's AI/ML-enabled device list to registered studies through explicit
evidence records, and records what was searched, what was found and what could not be read. It
runs by hand; no workflow calls it, and the weekly demographics job, its cohort and its files are
unchanged. It emits data files only; how the AI Devices tab shows them belongs to the site repo.

## Entities, kept distinct

| Entity | Key | Where |
|---|---|---|
| Source row | row number in the list | `submissions.json` → `source_rows` |
| Regulatory submission | FDA submission number | `inventory.py`, `submissions.json` |
| Device family / product | `family:candidate:…` or `family:curated:…` | `inventory.py`, `families.csv`, `families.json` |
| Organisation | canonical normalised name | `organizations.py`, `organization_aliases.csv`, `organizations.json` |
| Source document | submission number + source URL | `documents.py`, `documents.json` |
| Document version | sha256 of the bytes | `documents.json` → `versions` |
| Registered study | NCT id | `studies.json` |
| Study / analysis population | attached to the document that describes it | `demographics.py`, `demographics.json` |
| Evidence record (link) | `link:…` (hash of what the evidence is) | `links.py`, `links.json` |

Relations are many-to-many and always pass through an evidence record: a study may be joined to
several submissions and families, a submission to several studies, and one pair by several records.

## Counting units

`run.json` → `counting_units` defines each; `summary.json` names the unit of every count.

- **Source rows** (1,451 in the 2026-03-05 list): what the tab counts today.
- **Submissions** (1,430): rows folded on the submission number. Rows that share a number are
  classified `identical`, `alias_only` (a spelling of the company or device that normalises to the
  same key) or `substantive_conflict`; every row and every differing value is kept.
- **Families**: *candidate* families group submissions with the same organisation **and** the same
  whole core name (parentheses and a trailing version removed). Nothing looser groups: not a shared
  word, not one model of a multi-model cell, not the same name under another organisation, not a
  name made only of device-type words. A family is *curated* only when `families.csv` lists it.
  A count of submissions is never a count of products.

## What can be verified automatically

`automated_validation` on an evidence record (see the docstring of `links.py` for the full rules):

| Status | Given when | Level |
|---|---|---|
| `identifier_confirmed` | a document **of that submission** cites an NCT id that resolves on the registry, or a registry record carries **that exact submission number** in a retrieved field | submission |
| `quote_confirmed_in_source` | a person entered a documentary statement in `documentary_statements.csv` and its quote stands in the retrieved source at the stated place | submission |
| `identifier_not_resolved` | a cited NCT id that the registry does not hold | submission |
| `rejected_by_rule` | a search hit kept for visibility that fails a rule: the submission number is in no field of the returned record; or a name-only match on a name that floods the registry (a name query matching 50 or more records) with no organisation agreement | as found |
| `not_validated` | everything heuristic, and anything not checked | family (name evidence), or as found |

`identifier_confirmed` confirms that a citation exists and that the identifier is real. It does not
say what the citation means; the proposed relationship stays `study_cited_in_submission_document`
or `submission_cited_in_registry_record`. Whether the study *supported* the decision, *validated*
the device or *followed* it is a reviewer's `reviewed_relationship`
(`explicitly_cited_supporting_study`, `related_validation_study`, `documented_postmarket_study`,
`same_family_other_version`, `citation_not_about_this_device`, `not_related`).

**Name similarity never establishes a submission-level link or any validated status.** A device name
in a study's title or intervention gives `name_and_organisation_candidate` or `name_only_candidate`,
always at family level and `not_validated` (or `rejected_by_rule` for a common-word name without
organisation agreement, a rule that trades recall for a usable review queue: "Loop", "Aurora" and
"Contour" alone account for most such matches); `links.problems()` and the contract refuse
anything else. Manufacturer/sponsor agreement is recorded (tier, role, both strings) and raises confidence
one tier; its absence rejects nothing; a sponsor match without the device's name creates no record
(it is tallied on the coverage cell so a reviewer sees it was looked at).

**Human review is separate and is never set by code.** `human_review` is `unreviewed` unless
`reviews.csv` holds a row for the link id (`accepted`, `rejected`, `unresolved`, with a date and a
note giving the reason; no reviewer is named). A review changes neither the evidence nor the
automated status. Reviews of links that no longer exist are reported (`orphaned_reviews`).

**Timing is described, not used.** Each record carries the study's dates and, per relevant
submission, the FDA decision date and a relation (`primary_completion_on_or_before_decision`, …,
`indeterminate_at_recorded_precision`). Timing never changes the relationship, the validation or
the confidence.

## Documents

Discovered from FDA's release-database page for each number, which lists the documents FDA
publishes with FDA's own label; URL patterns are a recorded fallback. A page is used only when its
labelled number rows confirm it is the number's own page; any other answer, including one logged
before that check existed, is asked again and leaves the route incomplete. Each record has a stable id,
sha256, size, retrieval time, HTTP status, the server's Last-Modified, text status and extractor
version, page count, a type with the rule and the words that decided it, and
`demographic_summary_candidate` with its reason.

| Type | Category | Demographic summary candidate |
|---|---|---|
| `510k_summary` | 510(k) summary | yes, when readable |
| `510k_decision_summary`, `de_novo_decision_summary` | decision summary | yes, when readable |
| `pma_ssed` | summary of safety and effectiveness | yes, when readable |
| `510k_statement` | 510(k) statement | no: it contains no information |
| `510k_clearance_letter`, `de_novo_granting_letter`, `pma_approval_order` | granting letter | **no**: kept as regulatory evidence, never treated as a decision summary |
| `pma_labeling` | labeling | no (kept; not the decision record) |
| `other`, `unclassified_no_text` | other / unclassified | no |

The type comes from the document's own words (`classification.basis: text`, with the rule, the
matched phrase and the page), and FDA's link label is compared with it. Where the text cannot
decide, the basis says why the label was used instead: `official_label_only` (no text layer),
`official_label_unread_pages` (only the letter has text; the summary pages are scans or garbled;
never treated as read), `official_label_text_unmatched` (text read, no rule matched).

A supplement's documents are marked (`is_supplement_document`); the original PMA's documents
fetched for a supplement are `scope: parent_submission` and never support a supplement-level link.
Text is read from the text layer only and no OCR is run. Each extraction records the sha256 of the
bytes it parsed (`source_sha256`), and text is used only for the document version with that digest:
the text stage checks the stored file first and skips a missing or damaged one, and an extraction
not bound to its document's bytes is made again, and until then reads as not extracted.
A scan is `image_only`; a page whose text layer is unreadable (fonts without a character map:
control characters or shifted letters) is counted as a page without text (`text.n_pages_garbled`); a PDF portfolio, of which only the cover
is readable, is a `failed` extraction. The citation scan reports how many pages it could not read,
and a scan with any such page and no identifier found is `search_incomplete_or_blocked`, not a
completed scope: an unread page could hold the identifier. This was the investigator's decision on
2026-11-14, in review of the pull request that added this build; reading those pages by OCR is the
planned follow-up.

## Coverage

`coverage.json` holds, per submission, one cell per route and source (local document, FDA document
fetch, openFDA metadata, NCT-id citation scan, submission-number registry search,
name/organisation registry search, documentary statements, demographic extraction), each in exactly one of seven states
with its reason: `not_searched`, `search_incomplete_or_blocked`, `document_unavailable`,
`available_document_insufficient`, `candidate_awaiting_review`,
`no_candidate_in_completed_scope`, `verified_link`. Only `no_candidate_in_completed_scope` is a
negative finding; `verified_link` is the automated status and human review is reported beside it.
A submission's `overall` status is taken over the three routes that can produce a link (citation
scan, submission-number search, name/organisation search), by the precedence in
`coverage.OVERALL_PRECEDENCE`; the acquisition routes feed the citation scan and are not counted
twice. A documentary statement (`documentary_statements.csv`) is a person's reading, not a search:
its route counts towards the overall status only when it holds a statement no person has rejected
(a candidate awaiting review, since a statement carries no identifier). With none entered the
route reads `not_searched` and does not hold the submission there.

## Demographic evidence

`demographics.py` carries the pilot extractions forward, each attached to the document version it
read and to the population that document describes (quoted where the extraction quoted it,
otherwise `population_identity: unresolved`). Filing counts are never added to registry counts:
every observation is `may_be_added_to_registry_counts: false` with
`relation_to_registry_population: undetermined`, and the contract refuses anything else. No
extraction is run by this build.

## Artifacts (schema version 1)

`run.json`, `summary.json`, `submissions.json`, `families.json`, `organizations.json`,
`documents.json`, `studies.json`, `links.json`, `coverage.json`, `demographics.json`,
`review_queue.json`; see the docstring of `artifacts.py`. The run manifest records the inventory's
sha256, the sha256 of every frozen source archive and of the document manifest, retrieval windows,
the registry's data timestamp, the text-extraction version, the curated files applied, the
vocabularies and the sha256 of every other file. Every count in `summary.json` names its
denominator and definition; the browser computes no aggregate.

The run id ends in a digest of what the family publishes: `run.json` apart from the id, the build
time and the file checksums (so the inputs, the sources, the producing commit and a digest of the
producing code), and the content of every other file (`artifacts.run_identity`). Two builds that
publish different content, or come from different code, never share an id; the same content from the
same code gives the same id whenever it is built. `scripts/check_device_links_contract.py`
recomputes it and refuses a family whose id does not follow from its content.

## Curated files (tracked; header only to start)

| File | Purpose |
|---|---|
| `organization_aliases.csv` | the only way two differently named organisations are merged |
| `families.csv` | the only way a family becomes curated, or differently named submissions are joined |
| `reviews.csv` | human review of evidence records, by link id |
| `documentary_statements.csv` | explicit statements, with verbatim quote and location, that tie a study to a submission without a shared identifier |

## Running it

```bash
# 1. Acquire (network; at most two requests a second per host; resumable). The cache holds
#    downloaded files: keep it outside any repository.
python scripts/build_device_links.py acquire --cache /path/to/devices_cache
# 2. Build from the frozen cache (no network). Writes nothing unless the contract passes.
python scripts/build_device_links.py build --cache /path/to/devices_cache \
    --site ../clinical-trial-populations --out build/devices
python scripts/check_device_links_contract.py build/devices
python -m pytest tests/devices -q
```

`acquire` stages (`--stage`, default all, in order): `local` (register PDFs already on disk),
`openfda`, `pages` (FDA database pages), `documents`, `text` (page text via `pdftext.py`, run in
isolated mode per document), `nct` (look up cited NCT ids), `registry-numbers`, `registry-names`,
`freeze`. A rerun asks only what is not yet answered; a 404 is recorded as a finding and not asked
again; any other non-200 answer is logged and asked again. A killed run is safe to resume: files
are written under a temporary name and renamed, and an interrupted final line of a log is dropped
with a message. `--store-budget-gb N` is a limit on the store: a document whose file would take it past
N GB is not written, requests stop once it is full, and those documents are recorded as
`skipped_disk_budget` (with the HTTP status when one was received); their coverage reads search
incomplete. A repair that restores a document's recorded bytes is exempt. Text is
extracted one document at a time by default (`--workers 1`); the build streams the text cache and
holds no document text in memory. Needs `pypdf` (listed in `devices/requirements.txt`) in
addition to the runtime requirements. The 2026-11-13/14 run made about 6,200 requests and holds
1.8 GB of documents.

## Site export

```bash
python scripts/export_device_links_site.py build/devices ../clinical-trial-populations/data/devices
```

`scripts/export_device_links_site.py` cuts a build down to the seven files the site's AI Devices
tab reads (`device_links.js` there): `run.json`, `summary.json`, `submissions.json`,
`families.json`, `evidence.json`, `evidence_set_aside.json` and `documents.json`, site schema 1.
It runs by hand, makes no network call and commits nothing; a person commits the result to the
site repository.

- **A selection, not a second count.** `summary.json` is the build's with only the run id
  changed. The per-row tallies it adds (evidence records by kind, documents held) are breakdowns
  that must add up to the build's own counts, or the export stops.
- **The build is checked first.** Every file is compared with the sha256 in the build's
  `run.json`; a build that was edited, cut short or mixed with another is refused.
- **The run id ends with a digest of the build's files**, so two builds whose bytes differ never
  share an id even when their inputs and commit are the same. The build's own id is kept as
  `build_run_id`.
- **The output directory is replaced as a whole**, staged beside it and moved into place. A
  directory holding any other file is refused, so a mistyped path cannot delete another dataset.
- **What stays with the build:** `organizations.json`, `studies.json` and `coverage.json` (the
  fields the tab shows are copied onto the rows and records), `review_queue.json`, and
  `demographics.json`, which holds model outputs and is shown only as counts.
- Records rejected by rule go to `evidence_set_aside.json`, which the site loads only on request.
  Repeated text (coverage reasons, confidence and validation reasons) is written once per file as
  a table the rows index; the script's docstring gives the layout.

The 2026-11-14 build exports to 5.9 MB. `tests/devices/site_fixture.py` writes the fictional
family the site's tests read, through the same build and export.

## Which checks are which

- **Engineering validation**: `tests/devices/` (offline, fictional records), the contract
  (`contract.py`), frozen-archive hash checks, the byte-reproducible build. They show the rules are
  applied as written and the files are consistent.
- **Scientific validation**: whether a link is right. Nothing in this package establishes that;
  it is a person's review, recorded in `reviews.csv`. Keyword findings about what a document
  "describes" (`documents.study_language`) are likewise pointers for a reader, not conclusions.

## Compatibility

New files only: `devices/`, `scripts/build_device_links.py`,
`scripts/check_device_links_contract.py`, `scripts/export_device_links_site.py`, `tests/devices/`. No existing module, workflow,
published file or cohort definition changes. The build writes nothing to the site repository.
`scripts/export_device_links_site.py` writes only the directory it is given, by hand; the site's
`data/devices/` is a new folder that no weekly job reads, writes or prunes.
