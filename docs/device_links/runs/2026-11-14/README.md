# Device linkage run of 2026-11-14

The record of one manual build, kept so its counts can be checked without rerunning it.

| | |
|---|---|
| Built | 2026-11-14T17:40:35+00:00 by `scripts/build_device_links.py build` at engine commit `0c4a99458197` (no uncommitted changes; producing-code digest `d4fbb25fb29a…`), run id `devices-2026-11-14-63b371dc69`, method `device-links-1` |
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
| No candidate found in the completed search scope | 763 |
| Available document insufficient for the question | 8 |
| Not searched (name made only of device-type words) | 17 |
| Search incomplete or blocked | 348 |
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

It was rebuilt a sixth time after the fifth review round, at `e44576a` (run `devices-2026-11-14-4e740bb9b3`). That round's two fixes (an NCT id in document text must not run into a letter; an original PMA number must stand alone, not as part of a supplement's number) change no output of this run. Every output file is identical to the previous build apart from the run id.

It was rebuilt a seventh time after the sixth review round, at `e76cdf9` (run `devices-2026-11-14-948d8c09ac`). That round's two fixes (an FDA database page that answers without naming the number asked for is logged as `not_the_record` and asked again, not taken as FDA's answer; a local file that is not a PDF is not copied into the document store and does not settle its address) change no output of this run: all 1,431 database pages in the frozen cache name their own number, and all 12 recovered local files are PDFs. Every output file is identical to the previous build apart from the run id.

It was rebuilt an eighth time after the seventh review round, at `0f54e7b` (run `devices-2026-11-14-8750e49382`). That round's two fixes (a registry record citing another supplement of the same PMA is not a citation of the supplement sought or of the original; a PMA database page is confirmed from its "PMA Number" and "Supplement Number" rows, not from the base number appearing anywhere on it) change no output of this run. Every output file is identical to the previous build apart from the run id. The build reads the page verdicts recorded at acquisition, so the new page check was also run over all 1,431 cached pages: it confirms each one (1,376 510(k), 37 De Novo and 18 PMA pages).

It was rebuilt a ninth time after the eighth review round, at `d3a9c5b` (run `devices-2026-11-14-5433ca19c6`). That round's two fixes (a submission number is located in a registry field in any letter case; the acquisition stage for NCT ids also looks up the studies the curated statements name) change no output of this run: no searched number appears in lower or mixed case in the frozen registry records, and the statements file has no rows. Every output file is identical to the previous build apart from the run id. A third finding of that round, that a citation scan with pages it could not read should not read as a completed scope, changes the definition of a published state and was put to the investigator; that run kept the documented rule. The investigator decided it on 2026-11-14 (see the twenty-fifth rebuild below).

It was rebuilt a tenth time after the ninth review round, at `0d8923a` (run `devices-2026-11-14-ba872e874d`). That round's two fixes (510(k) and De Novo database pages are confirmed from their "510(k) Number" or "De Novo Number" row, as PMA pages are from theirs; an HTML page served in place of a document is asked again and leaves its route incomplete) change no output of this run. The new page check confirms all 1,431 cached pages, and no document in the manifest is `not_a_pdf`. Every output file is identical to the previous build apart from the run id.

It was rebuilt an eleventh time after the tenth review round, at `a0a5a1c` (run `devices-2026-11-14-6d322b3e15`). That round's fix (a registry record a curated statement quotes is fetched whole, not through the projected lookup) changes no output of this run: the statements file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twelfth time after the eleventh review round, at `a11cd03` (run `devices-2026-11-14-504dc82cae`). That round's two fixes (a search answer that states no count is not a completed search; a record set aside by rule counts toward coverage once a person accepts it) change no output of this run: every one of the 3,086 cached search answers states an integer count, and the reviews file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirteenth time after the twelfth review round, at `475747b` (run `devices-2026-11-14-5b2f2c7363`). That round's two fixes (a number-search hit is read only from a full record whose own id is the hit's; a JSON true/false is not a count) change no output of this run: the 4 full records held for number-search hits each carry their own id, and every cached search answer states an integer count. Every output file is identical to the previous build apart from the run id.

It was rebuilt a fourteenth time after the thirteenth review round, at `eaf16e9` (run `devices-2026-11-14-70dbf70112`). That round's two fixes (a link found by a number search that could not read every hit still stands; a registry-record statement is read only from a record that names its own study) change no output of this run: every number search in the run is completed, and the statements file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a fifteenth time after the fourteenth review round, at `7f7ebb7` (run `devices-2026-11-14-2841178b3d`). That round's three fixes (one evidence record per registry field; an original PMA is not located before a malformed supplement suffix; a document over the size limit leaves its route blocked) change no output of this run: no registry field holds two citations of one number, none of the 5 identifier-confirmed citations is a PMA or sits before a slash, and no document is over the size limit. Every output file is identical to the previous build apart from the run id.

It was rebuilt a sixteenth time after the fifteenth review round, at `5515990` (run `devices-2026-11-14-dfcf4f2eef`). That round's fix (an openFDA answer of HTTP 200 without a list of results is no answer, and is asked again) changes no output of this run: all 46 cached openFDA answers are 200 with a list of results. Every output file is identical to the previous build apart from the run id.

It was rebuilt a seventeenth time after the sixteenth review round, at `f0fd834` (run `devices-2026-11-14-29cb25de69`). That round's two fixes (a logged answer counts only for the request parameters it was logged for; a ClinicalTrials.gov study answer must state its own id) change no output of this run: all 46 cached openFDA answers were logged for the parameters their request has now, and all 18 cached study answers state their own id. Every output file is identical to the previous build apart from the run id.

It was rebuilt an eighteenth time after the seventeenth review round, at `cf2707f` (run `devices-2026-11-14-ead249edc3`). That round's two fixes (a malformed registry field path in a curated statement resolves to nothing; a registry search answered without a count is asked again) change no output of this run: all 3,086 cached search answers of HTTP 200 state a count, and the statements file has no rows. Every output file is identical to the previous build apart from the run id.

It was rebuilt a nineteenth time after the eighteenth review round, at `5f4f890` (run `devices-2026-11-14-df1f38e7b8`). That round's two fixes (the build refuses to replace an output directory that is not a device-linkage build; a registry search answer's studies must each state their id) change no output of this run: every one of the 23,222 studies in the cached search answers states its id. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twentieth time after the nineteenth review round, at `6cf47cc` (run `devices-2026-11-14-ebab207e47`). That round's two fixes (the build and the site export replace an existing directory only when it is empty or a complete device-linkage family; a study answer's id must be an NCT id) change no output of this run: all 23,240 study ids in the cached registry answers are NCT ids. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-first time after the twentieth review round, at `b1194ca` (run `devices-2026-11-14-7a3f863af1`). That round's two fixes (a build checks that every retrieved document's stored file exists, has its recorded size and hashes to its digest, and refuses otherwise; a registry version answer must carry its API version and data timestamp) change no output of this run: all 1,532 stored files check, and both cached version answers carry both fields. Every output file is identical to the previous build apart from the run id. Hashing the 1.8 GB store lengthens a build: this one took 35 s, against about 20 s before.

It was rebuilt a twenty-second time after the twenty-first review round, at `5fa7786` (run `devices-2026-11-14-6e036ec166`). That round's three fixes (each registry record carries the version stamp of the snapshot current when it was fetched; an openFDA answer must carry the submission number on every entry; a retrieved document whose stored file is missing or damaged is fetched again) change no output of this run: both cached version answers carry the same stamp, all 1,558 cached openFDA entries carry their number, and all stored files check. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-third time after the twenty-second review round, at `43ceab4` (run `devices-2026-11-14-94ea2d57b6`). That round's three fixes (an openFDA entry must carry a number its request asked for; a search count must cover the studies returned; repairing a stored file counts only its net size against a disk budget) change no output of this run: all 1,558 cached openFDA entries and all 3,086 cached search answers pass the checks, and no budget was set. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-fourth time after the twenty-third review round, at `1013cce` (run `devices-2026-11-14-17c61c62b2`). That round's fix (a settled answer must pass its caller's current shape check before acquisition reuses it) changes no output of this run, and no answer in the cache would be asked again: every cached answer passes the current checks. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-fifth time, at `ba1d46e` (run `devices-2026-11-14-cbd7d89e0b`), to apply the investigator's decision on the question left open in the eighth review round. In the investigator's words: "I like your recommendation of b now and c as the follow-up". Option (b): a citation scan that found no identifier while some page of the documents held has no usable text layer now reads `search_incomplete_or_blocked`, not `no_candidate_in_completed_scope`, because an unread page could hold the identifier. Option (c), reading those pages by OCR, is a separate follow-up. This changes `coverage.json`, `submissions.json` and `summary.json`; every other output file is identical to the previous build apart from the run id. On the citation-scan route, 375 submissions move from no candidate in the completed scope to search incomplete (1,379 → 1,004 and 1 → 376 on the route as published). Overall, 302 submissions move: 296 from no candidate in the completed scope and 6 from not searched, all to search incomplete or blocked (1,059 → 763; 23 → 17; 18 → 320). The other 73 of the 375 keep their overall state: 69 have a name candidate awaiting review, which ranks higher, and 4 were already incomplete through the name search. The 6 had a name search that was not run and a citation scan that was completed scope; an incomplete scan ranks above not searched in the overall precedence (`devices/coverage.py`), so they now read incomplete. The measurement put to the investigator counted only the 296.

It was rebuilt a twenty-sixth time after the twenty-fifth review round, at `942fad8` (run `devices-2026-11-14-e6337c1687`). That round's fix (a cached FDA database page counts only when it was confirmed to name its number; any other cached 200 is asked again, its links are not read, and the route is incomplete) changes no output of this run: all 1,431 cached page answers, in the log and in the frozen archive, are 200s confirmed to name their number. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-seventh time after the twenty-sixth review round, at `ddb05e6` (run `devices-2026-11-14-794497abd5`). That round's fix binds each text extraction to the bytes it parsed: the extractor records their sha256, the text stage checks the stored file before extracting, and the build uses text only when that hash equals the document version's digest. No cached extraction recorded it, so the text stage was run again over the frozen document store (2026-11-14, 13:26 to 13:30 UTC, no network): all 1,532 stored files hash to their digests, none was skipped, and every new extraction is identical to the one it replaces apart from the added hash. The text extraction version is unchanged (`pypdf-7.1.0-r1`). Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-eighth time after the twenty-seventh review round, at `1485ed6` (run `devices-2026-11-14-803a9ff439`). That round's fix moves the text binding into the one function every reader of cached text uses, so documentary statements and the demographic quote locations enforce it too, not only the document records. It changes no output of this run: every cached extraction is bound to its document's bytes since the previous rebuild. Every output file is identical to the previous build apart from the run id.

It was rebuilt a twenty-ninth time after the twenty-eighth review round, at `e2e443b` (run `devices-2026-11-14-d9e9d30932`). That round's two fixes (a PMA answer's supplement numbers must be absent, empty or `S` and digits; the build reads frozen search answers with the same check acquisition applies) change no output of this run: all 145 PMA entries in the cache carry an empty or three-digit supplement number, and every cached search answer passes the check. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirtieth time after the twenty-ninth review round, at `f4aefa0` (run `devices-2026-11-14-41f64d9e75`). That round's fix (an openFDA 200 must list at least one entry; openFDA reports no match with a 404) changes no output of this run: each of the 46 cached openFDA answers lists between 1 and 50 entries. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-first time after the thirtieth review round, at `93a0d9f` (run `devices-2026-11-14-057c8cacab`). That round's two fixes (a PMA supplement joined to its number by a hyphen or dash is the supplement, never a citation of the original; documents of a submission not in the inventory are left out rather than stopping the build) change no output of this run: the frozen registry answers hold no dash-joined supplement, and every document in the manifest belongs to a submission in the inventory. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-second time after the thirty-first review round, at `9d351c6` (run `devices-2026-11-14-af31bf600f`). That round's fix (a registry record fetched before any version answer has no version stamp and a reason, rather than a later snapshot's) changes no output of this run: no registry record in the cache was fetched before the first version answer (2026-11-13T23:31:12Z). Every output file is identical to the previous build apart from the run id. A second finding of that round, whether a citation scan of a document with no readable text should read as incomplete rather than as available document insufficient, changes what a published state means and was put to the investigator; this run keeps the documented rule.

It was rebuilt a thirty-third time after the thirty-second review round, at `9f7a166` (run `devices-2026-11-14-48162bb3c1`). That round's fix (a timeout or crash of the text worker is cached as a failed extraction but extracted again on the next run) changes no output of this run: the text cache holds no failed extraction. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-fourth time after the thirty-third review round, at `026fade` (run `devices-2026-11-14-a27333fd87`). That round's fix (the repair of a recorded document's damaged file is not stopped by the disk budget) changes no output of this run: no budget was set. Every output file is identical to the previous build apart from the run id. A second finding of that round, that a study's facts in `studies.json` could come from a different registry snapshot than a later evidence record for it, changes the published format of the build and the site and was put to the investigator; in this run every registry record was fetched between 23:31 and 00:28 UTC and both version answers report the same data timestamp.

It was rebuilt a thirty-fifth time after the thirty-fourth review round, at `4164314` (run `devices-2026-11-14-303af7bef3`). That round's two fixes (a repair that finds a new version of a document is held to the disk budget; a study record the build cannot read is no answer) change no output of this run: no budget was set, and all 23,240 cached studies pass the stricter check. Every output file is identical to the previous build apart from the run id. A third finding of that round, that the registry version is fetched once per UTC day, belongs with the question on registry snapshots put to the investigator in the thirty-third round.

It was rebuilt a thirty-sixth time after the thirty-fifth review round, at `e11a78d` (run `devices-2026-11-14-dfa648ea70`). That round's two fixes (a failed repair attempt does not make a recorded document new to the disk budget; a frozen full study record the build cannot read leaves the number search incomplete) change no output of this run: no budget was set, and every cached study record passes the check. Every output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-seventh time after the thirty-sixth review round, at `1e87e9a` (run `devices-2026-11-14-62e46da91a`). That round's two fixes: a document citation's evidence-record id includes its quoted sentence, so a changed sentence in a new version of the document is new evidence and a review of the old one is not carried over; and a study record whose fields have the wrong type is no answer. The first changes the ids of the 15 document-citation records (14 citing the submission's own document, 1 a parent PMA's) and so the references to them in `links.json` and `review_queue.json`; every record is otherwise identical, and no review exists to be orphaned. The second changes nothing: all 23,240 cached studies pass. Every other output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-eighth time after the thirty-seventh review round, at `83ddd5e` (run `devices-2026-11-14-505155e43c`). That round's two fixes: a name-evidence record's id includes its method and the organisation that agrees, so a change in manufacturer agreement is new evidence and a review of the old is not carried over; and a locally recovered document's damaged stored copy is copied again from its file. The first changes the ids of the 2,195 name-evidence records (1,937 name only, 258 name and organisation) and the references to them in `links.json` and `review_queue.json`; every record is otherwise identical, and no review exists to be orphaned. The second changes nothing. Every other output file is identical to the previous build apart from the run id.

It was rebuilt a thirty-ninth time after the thirty-eighth review round, at `f2f3bbb` (run `devices-2026-11-14-d53f0dece1`). That round's two fixes: the local stage restores a damaged stored copy from the last retrieval of its local file even when a later network attempt failed; and a registry citation's evidence-record id includes its whole quote, not its first 80 characters. The second changes the ids of 2 of the 7 registry-citation records (the two located citations whose quote is longer than 80 characters; the two records set aside because the number stands in no returned field are keyed without a quote) and the references to them in `links.json` and `review_queue.json`; every record is otherwise identical, and no review exists. Every other output file is identical to the previous build apart from the run id.

It was rebuilt a fortieth time after the thirty-ninth review round, at `da8539d` (run `devices-2026-11-14-4ac6505518`). That round's two fixes: every evidence-record id now includes the record's whole quoted evidence (whitespace collapsed), added in one place for every kind of record rather than by each builder, so a change in the quoted text of a name match, a documentary statement or a registry hit that does not carry the number is new evidence and a review of the old text is not carried over; and the document store's disk budget is a limit, so a document whose file would take the store past it is not written and is recorded as `skipped_disk_budget` (a repair restoring recorded bytes is exempt). The first changes the ids of 2,197 of the 2,217 evidence records (the 2,195 name-evidence records and the 2 registry records set aside because the number stands in no returned field, whose quote is the study title) and the references to them in `links.json` and `review_queue.json`; the other 20 (document citations and located registry citations) already ended with their whole quote and keep their ids. Every record is otherwise identical, and no review exists to be orphaned. The second changes nothing in this run, which set no budget. Every other output file is identical to the previous build apart from the run id.

It was rebuilt a forty-first time after the fortieth review round, at `59bff70` (run `devices-2026-11-14-ccabb9c916`). That round's fix: an openFDA answer is settled only when every field the build reads from each entry is absent, null or text (and the entry's `openfda` object, where present, is an object), so a malformed answer is asked again rather than failing every later build; the same check applied at build time reads such an answer, already settled, as an error. All 1,558 entries in the cache are well formed, and every output file is identical to the previous build apart from the run id.

It was rebuilt a forty-second time after the forty-first review round, at `f3c86bd` (run `devices-2026-11-14-9565d46877`). That round's fix: a documentary statement entered in `devices/documentary_statements.csv` now counts in a submission's overall coverage status, through a new `documentary_statement` route; before, a submission whose only link was a statement published its status from the searches alone. A live statement makes the route a candidate awaiting review, and only then does it count; with none entered the route reads `not_searched` and does not hold the submission there. **Compatibility:** `coverage.json` and the run's `coverage_routes` vocabulary gain this route, so every submission carries one more coverage cell, and `summary.json` gains its row in `coverage_by_route_and_state`; the site reads routes from the vocabulary, so its pane shows the new row without a code change. This run has no documentary statements: all 1,430 submissions read `not_searched` on the route, no overall status changes, and every other output file is identical to the previous build apart from the run id.

It was rebuilt a forty-third time after the forty-second review round, at `0d95883` (run `devices-2026-11-14-bba6cb4e60`). That round's fix: a documentary statement's evidence-record id includes its proposed relationship, the curator's claim, so changing the claim while keeping the quote makes a new record and a review of the old claim is not carried over. This run has no documentary statements, and every output file is identical to the previous build apart from the run id.

It was rebuilt a forty-fourth time after the forty-third review round, at `543b0b1` (run `devices-2026-11-14-f337a8f6a1`). That round's first fix: an openFDA answer is settled only when it holds as many results as openFDA's stated total (`meta.results.total`), so a page cut short by the request's limit, or partial, is asked again rather than read as "openFDA holds no record" for a supplement it omits. All 46 openFDA answers in the cache state a total equal to the results they hold (at most 50), and every output file is identical to the previous build apart from the run id.

It was rebuilt again for that round's second fix, at `07a39bb` (run `devices-2026-11-14-ce2564e543`): cached text whose payload names another extractor version than the one asked for is not read, so the text a build uses always matches the extraction version `run.json` stamps. All 1,532 cached extractions name their own version, and every output file is identical to the previous build apart from the run id.

It was rebuilt a forty-fifth time after the forty-fourth review round, at `efb4ef0` (run `devices-2026-11-14-3c21849a48`). That round's fix: the text stage counts a cached extraction as done only under the conditions the build reads it under, the same bytes and the same extractor version, so a file naming another version is extracted again rather than left to read as not extracted. All 1,532 cached extractions name the current version, and every output file is identical to the previous build apart from the run id.

It was rebuilt a forty-sixth time, at the commit named above, to apply the investigator's decisions of 2026-11-14 on the questions still open. In the investigator's words: "D1. 2 D2 follow up PR D3. copy then delete D4. review merge the source archive. D5 finish then mergeD6. open it after 61". D1 is the image-only question from the thirty-third review round, and option 2 was chosen: a citation scan whose held documents cannot answer reads `search_incomplete_or_blocked`, not `available_document_insufficient`, when an unread page belongs to a document that could cite a study (a summary, an SSED, a decision summary, or a file whose type is not known). A clearance letter, an approval order, labeling or a 510(k) Statement cannot cite a study, so its unread pages leave the scan insufficient. This changes `coverage.json`, `submissions.json` and `summary.json`; every other output file is identical to the previous build apart from the run id. On the citation-scan route, 30 submissions move from document insufficient to search incomplete (40 → 10 and 376 → 406 on the route): 23 whose FDA-labelled 510(k) summary has text only on its letter page, 2 image-only 510(k) summaries, 1 PMA whose SSED is image-only, and 4 image-only files with no FDA label. The 3 whose unread document cannot cite a study (2 clearance letters, 1 510(k) Statement) stay insufficient. Overall, 28 submissions move from document insufficient to search incomplete (36 → 8 and 320 → 348); the other 2 of the 30 already read candidate awaiting review, which outranks both. No other state changes. The thread's estimate was "about 29" and "about 7"; these are the measured counts. D2 (registry snapshot provenance and the same-day version refresh) is left to the follow-up pull request with OCR, as decided. D5 ("finish then merge"): no further review rounds are requested on this pull request, and the two round-45 findings about archives frozen before validation existed are carried to the same follow-up.
