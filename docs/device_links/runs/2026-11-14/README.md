# Device linkage run of 2026-11-14

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-14T04:15:25+00:00 by `scripts/build_device_links.py build` at engine commit `e44576acf1ff` (no uncommitted changes; producing-code digest `299a6ba640fd…`), run id `devices-2026-11-14-4e740bb9b3`, method `device-links-1` |
| Inventory | FDA's AI/ML-enabled device list of 2026-03-05 as enriched in `data/ai-ml-enabled-devices-enriched.csv`, sha256 `930b79b8deb1df79…`, 1,451 rows |
| FDA documents | 1,566 addresses tried 2026-11-13 to 2026-11-14; manifest sha256 `f6b047f76cd65dc4…` |
| Registry | ClinicalTrials.gov API 2.0.5, data timestamp 2026-11-13T10:00:06; 3,113 responses, sha256 `aeacdb4ffbb13579…` |
| Text extraction | `pypdf-7.1.0-r1`, text layer only, no OCR |
| Curated files | all four are header-only: no family is curated, no organisation is merged by alias, no documentary statement is entered and no link is reviewed |

## Files here

- `run.json`: the manifest. Counting units, vocabularies, every source with its retrieval window and sha256, and the sha256 of every output file.
- `summary.json`: every count with its denominator and definition.

## Files not here

The other nine artifacts (21 MB) are not committed; their sha256 values are in `run.json`. The
source cache is not committed either: 1,532 FDA documents (1.76 GB) and about 40 MB of page
text, FDA database pages and frozen API responses. Rebuilding from that cache gives the same
nine files byte for byte; this was checked between the build made in the working directory and
the build recorded here. Acquisition is resumable and re-fetches only what is missing, but FDA
and the registry can change what they serve, so a re-acquired cache is a new run.

## Counting units

| Unit | Count | Note |
|---|---|---|
| Source rows | 1,451 | Lines of FDA's list. This is what the AI Devices tab counts today. |
| Submissions | 1,430 | Distinct submission numbers. 21 numbers stand on two rows: 7 pairs identical, 14 differing only in a company spelling, 0 with a substantive conflict. |
| Candidate families | 1,120 | Same organisation and the same whole core name. None is curated, so this is not a count of products. 500 submissions share a family with another submission. |
| Organisations | 554 | Raw names joined by spelling rules only. 130 appear under more than one spelling. |

By pathway (submissions): 510(k) 1,376; De Novo 37; PMA 16; PMA supplement 1.

## Documents

| | Count | Of |
|---|---|---|
| Addresses tried | 1,566 | |
| Retrieved and held by sha256 | 1,532 | 1,566 |
| Not found (HTTP 404, all on URL-pattern guesses) | 34 | 1,566 |
| Other retrieval failures | 0 | 1,566 |
| Scans with no text layer (not read; no OCR) | 12 | 1,532 |
| Not readable by the extractor | 1 | 1,532 |
| With at least one unread page | 418 | 1,532 |
| Submissions with at least one document | 1,429 | 1,430 |
| Submissions with a readable document of a type that describes a study population | 1,388 | 1,430 |

Granting letters (60) and labeling (16) are kept and typed. They are not treated as decision
summaries or as demographic summaries.

## Links

| Kind | Evidence records | Distinct pairs | Entities |
|---|---|---|---|
| Submission-specific: a shared identifier found in a retrieved source | 19 | 15 submission–study | 11 submissions, 15 studies |
| Family-only: device name and organisation agree | 258 | 258 family–study | 108 families (169 submissions) |
| Family-only: device name only | 290 | 290 family–study | 93 families have nothing stronger |
| Set aside by rule | 1,649 | 1,649 | 1,647 name matches on a name that returns 50 or more registry records, and 2 number-search hits where the number is in no returned field |
| Parent PMA's document cites the study (not the supplement's) | 1 | 1 | P140011/S008 |
| Cited identifier the registry does not hold | 0 | 0 | |
| Reviewed by a person | 0 | | |

The 19 identifier records establish that a citation exists. They do not say what it means.
On one reading of the quotes, the 15 pairs include the device's own clinical or validation
study, drug trials whose images were reused as development or test data (K240712, DEN200055),
and studies that began after the decision and name the cleared device (DEN170073, K240036).
That reading is not recorded as a review.

11 of 1,519 readable documents cite an NCT id. By a keyword rule that has not been validated,
873 of 1,422 substantive readable documents describe a study without giving a registry
identifier, and 159 more say only that no clinical study was done.

## Coverage of the 1,430 submissions

| State | Submissions |
|---|---|
| Verified link (identifier cited; what the citation means is not reviewed) | 11 |
| Candidate link awaiting review | 282 |
| No candidate found in the completed search scope | 1,059 |
| Available document insufficient for the question | 36 |
| Not searched (name made only of device-type words) | 23 |
| Search incomplete or blocked | 18 |
| Document unavailable | 1 |

## Demographic evidence

The 24 carried-forward pilot extractions were made on 8 De Novo granting letters and report
no demographic value; 4 are malformed. Each is attached to its letter with the population
unresolved and marked as not addable to registry counts. No extraction was run by this build.

## What this record does not establish

Tests, the contract check, hash checks and the byte-identical rebuild show that the rules are
applied as written. Whether any link is right is a person's review, recorded in
`devices/reviews.csv`; none has been made. See `docs/audits/device_linkage/` for the first
audit, the build report and an independent recount.

## Rebuilt on the pull-request branch

The build was first made at `7a79adb` (run `devices-2026-11-14-cf872a1604`) on a working branch. Its commits were replayed onto `main` after the results-structure pull request merged, which gave them new hashes, and the run id was changed to a digest of what the build publishes, including a digest of the producing code (the fix made to the results-structure build in its review). The build was then rebuilt from the same frozen cache at `4d958dd` (run `devices-2026-11-14-4a05b0d599`). Every output file is identical to the first build apart from the run id; `run.json` differs in the run id, the build time, the producer (commit and code digest) and the file checksums, which cover the run id.

It was rebuilt again after the first review round, at `c415897` (run `devices-2026-11-14-272ff4e880`). That round's three fixes (a truncated number search reads as incomplete; a documentary statement must come from the submission's own document; a file served at two addresses counts once against a disk budget) change no output of this run: its single-number searches each found at most one record, the statements file has no rows, and no budget was set. Every output file is identical to the previous build apart from the run id.

It was rebuilt a third time after the second review round, at `a887a8b` (run `devices-2026-11-14-23c855dee1`). That round made the common-word rule follow the name a study was matched on (it had been set for a whole family when any of the family's names flooded the registry) and made a registry quote be checked only in the field its row names. Every count and every record's classification is unchanged: each of the 1,647 matches set aside was matched on the name whose own search flooded. The reason text on those 1,647 records now reads "a search for the matched name returned 50 or more records"; nothing else in `links.json` changed, and every other output file is identical apart from the run id.

It was rebuilt a fourth time after the third review round, at `498da39` (run `devices-2026-11-14-8049f7e796`). That round's two fixes (an HTTP 200 answer without a usable body is logged as no answer; a registry-record statement must quote its own study) change no output of this run: every 200 answer in the frozen caches has a parsed body, and the statements file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a fifth time after the fourth review round, at `d8718fc` (run `devices-2026-11-14-df7f71255b`). That round's two fixes (a submission number must end where the record's token ends; a documentary statement may not cite a parent PMA's document) change no output of this run: the five identifier-confirmed registry citations each stand as an exact token, and the statements file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a sixth time after the fifth review round, at the commit named above. That round's two fixes (an NCT id in document text must not run into a letter; an original PMA number must stand alone, not as part of a supplement's number) change no output of this run. Every output file is identical to the previous build apart from the run id.
