# Audit record: linking FDA AI/ML devices to registered studies

**Status: provisional.** No link here has been reviewed by a person.

| Path | Contents |
|---|---|
| `first_pass/` | The feasibility audit of 2026-11-13: inventory counts, reachability of FDA hosts, identifiers in the 12 documents then on disk, and a name-and-sponsor search on a sample of 80 submissions. Report, sample, code, cached registry responses and outputs. |
| `build_report/` | The report of the full build over all 1,430 submissions (2026-11-14), with its regression against the first pass (`analysis/audit_regression.json`), a second-extractor check and a 30-document sample for reviewing the study-language rule. |
| `independent_recount/` | Two scripts written without reading the build's code, with their outputs. |

## Reading the first pass now

- **Its sample.** 80 of the 1,430 distinct submissions, drawn as a proportional stratified
  random sample by decision-year band with seed `20261113`; the identifiers are in
  `first_pass/sample_devices.csv`.
- **"11 of 80".** Under that audit's own rule, 11 sampled submissions had a study naming the
  device with an agreeing sponsor. It estimates candidate yield under that rule for this list.
  It does not estimate true links, and it does not generalise to products, pathways or other
  lists. The full build replaces it with a count: 169 of 1,430 submissions sit in a family with
  at least one name-and-organisation candidate.
- **"34 matches"** are 34 device–study pairs, which are also 34 distinct studies, all at
  family level. All 34 are in the build: 32 as name-and-organisation candidates, 1 as
  name-only, 1 set aside by rule.

## What the independent recount does and does not establish

`independent_recount/recheck_inventory.py` recounts rows, distinct submissions, duplicated
numbers and pathways from FDA's list. `recheck_documents.py` hashes every stored document and
searches each for registry identifiers with a different text extractor (pdftotext) from the
build's (pypdf).

- **What agreed:** 1,451 rows, 1,430 submissions, 21 duplicated numbers (7 identical, 14
  differing only in the company column); every one of 1,532 files hashes to its recorded
  sha256; the same 12 files have no text layer; the same 11 documents cite an identifier,
  giving the same 14 submission–study pairs.
- **What this establishes:** the inventory arithmetic and the citation scan do not depend on
  one implementation or one text extractor.
- **What it does not establish:** what any citation means; whether a document describes a
  study without naming its identifier; whether any name-based candidate is a real link. The
  recount shares the build's definition of an identifier (the pattern `NCT` plus eight digits)
  and cannot see text that neither extractor reads, which includes the 12 scans and the unread
  pages of 418 documents.

The stored documents themselves are not in the repository (1.76 GB), so `recheck_documents.py`
can be rerun only against a cache that holds them.
