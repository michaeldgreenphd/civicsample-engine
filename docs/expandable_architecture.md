# Expandable outcomes and provenance platform: architecture

This document describes four modules that extend the engine's trial-level
work beyond the industry Phase 3 outcomes cohort: to trials sponsored by the
NIH, other US federal agencies, NCTN cooperative groups, academic medical
centers and non-profit foundations, and to any trial with posted protocol or
statistical analysis plan documents. The modules arrived together in PR #97
(head `dc38220`) and were split into separate branches. All four have since
merged to `main` (PRs #112, #111, #113 and #114).
None of them runs on a schedule, and none publishes anything to the site.

[census_iteration_playbook.md](census_iteration_playbook.md) is the operating
procedure for the rule-location census these modules serve.

## 1. Branches

PR #97 was split into four module branches and this documentation branch.
Each module branch carries only its own files, so each can be reviewed and
merged on its own; M3 is the exception, because it imports M4.

| Branch | Base | Files | Imports from another module | State |
|---|---|---|---|---|
| `antigravity/m1-document-indexer` | `aa14a2d` | `src/document_indexer.py`, `scripts/index_trial_documents.py`, `tests/test_document_indexer.py`, `.gitignore` (the three M1 data paths) | none | merged to `main` (PR #112, merge commit `2932cac`; branch head `4defbef`) |
| `antigravity/m2-population-ecosystem` | `aa14a2d` | `src/analysis_population_parser.py`, `sponsors/ecosystem.py`, `src/holdout_guard.py` (the holdout guard both modules import), `sponsors/ecosystem_rules.csv`, `tests/test_analysis_population_parser.py`, `tests/sponsors/test_ecosystem.py`, `tests/sponsors/expected_ecosystem_counts.json`, `tests/sponsors/fixtures/sponsors_fixture_ecosystem.csv` | none (it uses the existing `sponsors.adapter`, `sponsors.baseline` and `sponsors.sponsor_roles`) | merged to `main` (PR #111, merge commit `3f1bcb9`; branch head `b9a2ebc`) |
| `antigravity/m4-citation-verifier` | `aa14a2d` | `src/citation_verifier.py`, `src/longitudinal_provenance.py`, `scripts/verify_and_plan_census_batch.py`, `tests/test_citation_verifier.py`, `tests/test_longitudinal_provenance.py`, `tests/test_verify_and_plan_census_batch.py` | none | merged to `main` (PR #113, merge commit `c144f12`; branch head `d68652a`) |
| `antigravity/m3-outcomes-bundles` | `main` (it merged the m4 branch at `d68652a`, then `main` at `c144f12`) | `src/outcomes_layout.py`, `scripts/build_outcomes_shards.py`, `tests/test_outcomes_layout.py`, `.gitignore` (the two M3 paths) | `src.citation_verifier` (M4) only | merged to `main` (PR #114, merge commit `f6cf568`; branch head `51f2ba8`) |
| `antigravity/docs-platform` | `aa14a2d` | this file and `docs/census_iteration_playbook.md` | not applicable | merged to `main` (PR #115, merge commit `4804236`) |

The revisions read for this document were m1 `4defbef` (merged to `main` in
`2932cac`), m2 `b9a2ebc` (merged to `main` in `3f1bcb9`), m4 `d68652a`
(merged to `main` in `c144f12`), m3 `e2a8c21` and `51f2ba8` (merged to `main`
in `f6cf568`), and `main` at `4804236`. m3 `51f2ba8` merges `main` at `c144f12` into `e2a8c21`; of M3's
files only `.gitignore` changed (it gains M1's lines), and
`src/outcomes_layout.py`, `scripts/build_outcomes_shards.py` and
`tests/test_outcomes_layout.py` are identical, so a statement below made at
`e2a8c21` also holds at `51f2ba8`. A later commit on any of these branches,
or on `main`, can change a detail stated here, so re-check a detail against
the code before relying on it.

M3 imports M4; both are on `main`.

## 2. Boundaries with the weekly pipeline

- No module branch changes `.github/workflows/`, `requirements.txt`,
  `requirements-dev.txt`, `src/extract_all.py`, `src/site_layout.py`,
  `src/dataset_folder.py`, `scripts/split_data.py`,
  `scripts/prune_snapshots.py`, `scripts/check_site_contract.py`,
  `sponsors/company_aliases.csv` or `tests/sponsors/expected_counts.json`.
- No workflow runs any of the modules' scripts. Everything here runs by hand.
- No module writes to the site repository. M3 writes a bundle tree on local
  disk (by default `data/outcomes_bundles/`, which `.gitignore` on `main` ignores),
  and nothing copies that tree to the site.
- CI (`.github/workflows/ci.yml`) runs each branch's tests on Python 3.11.
  None of these modules belongs to the LLM extraction stack that
  `run_extractions.yml` runs on Python 3.12, and they were not run on 3.12 for
  this document.

## 3. Dependencies

Every third-party package below is already in `requirements.txt`; no branch
adds a dependency.

| File | Third-party packages | Other requirements |
|---|---|---|
| M1 `src/document_indexer.py` | none | Page-text extraction (`MacOSPDFKitProvider`) needs macOS (`sys.platform == "darwin"`). It compiles a helper with `clang` against Apple's `Foundation` and `PDFKit` frameworks, or falls back to `ctypes.CDLL` bindings to `/usr/lib/libobjc.A.dylib`, `Foundation.framework` and `PDFKit.framework`; a prebuilt helper can be named in `CIVICSAMPLE_PDFKIT_HELPER`. On any other platform, a document with neither a pre-extracted page map (`--page-map-jsonl`) nor a cached one raises `PageExtractionError`. |
| M1 `scripts/index_trial_documents.py` | `requests` (imported when the script loads); `tqdm` (imported for a run over more than one trial; if it is not installed the run continues without a progress bar) | `src.api_client.CTGovAPIClient` and network access to ClinicalTrials.gov's API and document URLs for a live run. A census ingest (`--ingest-census-jsonl`) makes no network request. |
| M2 `sponsors/ecosystem.py` | `pandas`, imported when the module loads (the sibling modules it uses, `sponsors.adapter`, `sponsors.baseline` and `sponsors.sponsor_roles`, import it too) | `src.holdout_guard` (M2) |
| M2 `src/analysis_population_parser.py` | `pandas`, imported only inside `parse_outcomes_frame` (calling that function without pandas raises `ImportError`) | `src.holdout_guard` (M2) |
| M2 `src/holdout_guard.py` | `pandas`, imported only inside `check_frame`, the sponsor-frame check (the parser never calls it) | none: the Python standard library otherwise |
| M4 `src/citation_verifier.py` | none | No network and no PDF binary: it reads page text that was extracted beforehand. |
| M4 `src/longitudinal_provenance.py` | none | `src.citation_verifier`. It makes network requests only through a `publication_provider` the caller passes in; `src.pubmed_fetcher.PubMedFetcher` fits that interface and uses `requests`. |
| M4 `scripts/verify_and_plan_census_batch.py` | none | `src.citation_verifier` and `src.utils.pipeline_commit`, which runs `git` when `GITHUB_SHA` is unset. No network. |
| M3 `src/outcomes_layout.py` | none | `src.citation_verifier` (M4), and `git`, run through `subprocess`, to resolve the engine's commit. |
| M3 `scripts/build_outcomes_shards.py` | none | `src.outcomes_layout`. |

When M1 cannot extract a document's pages, the indexer does not stop: it
records that document as `extraction_failed`, with its sha256 and size and
`physical_pages` null, prints the number of such documents on stderr, and
the run exits 0. On a platform other than macOS without page maps, every
uncached document ends this way.

## 4. The `in_audit` holdout, module by module

The holdout is the set of trials whose `in_audit` is true in the site's
`data/outcomes/index.json`. Nothing may open, fetch, parse, cache or record
anything for those trials. The modules enforce this in different places, and
M2 leaves it to its callers.

### M1 (document indexer)

- **CLI.** `--manifest` is required whenever `--nct-id` or
  `--ingest-census-jsonl` is given; without it the run is refused with
  `AuditHoldoutError` (exit 2). With `--manifest` alone, every manifest row
  with `in_audit: false` is indexed. The manifest's `in_audit: false` rows are
  the allowlist. An `in_audit: true` anywhere in the file (a row, the
  `excluded` list, a nested object) adds that trial to the holdout set, and
  an `in_audit` that is not a bool is refused. Every `--nct-id`, census row,
  CT.gov payload (its `nctIdAliases` included), document URL, page-map tag and
  `url_manifest.json` entry is checked before any request, download, cache
  read or parse. A firewall refusal exits 2, and its message gives positions
  and counts, never a trial id.
- **Library.** `enforce_not_in_audit` and the functions that call it check
  only the sets they are given, and `DocumentCacheStore` adds its own sets to
  each call's. The defaults (`in_audit=False`, an empty audit set, no
  allowlist) admit any trial, so library code that passes no sets has no
  holdout protection. Build the store with the run's sets, as `run_cli` does.

### M2 (population parser and sponsor ecosystem)

- M2 has no CLI, does no I/O of per-trial data, and no entry point receives
  an audit id set.
- The rules are in `src/holdout_guard.py`, which both M2 modules import
  (`from src import holdout_guard as hg`). Each module raises its own
  subclass of `hg.AuditHoldoutError`, whose `reason` is one of
  `HOLDOUT_REFUSAL_REASONS`: `PopulationAuditHoldoutError` from
  `parse_study_outcomes`, `parse_outcomes_frame`,
  `select_raw_outcome_populations` and `build_outcomes_from_raw`;
  `EcosystemAuditHoldoutError` from `build_ecosystem_index`,
  `partition_ecosystem`, `build_unified_index`, `partition_unified`,
  `attribute_study_sponsors`, `sector_trials`, `organization_trials`,
  `trial_cohorts`, `audit_ecosystem`, `uncovered_ecosystem_rules`,
  `compute_ecosystem_baseline`, `match_ecosystem_literals` and
  `check_cross_layer_conflicts`.
- Input that carries the flag (under a name `is_flag_key` accepts, at any
  depth) is refused when a flag is True (`in_audit_true`); when a flag is
  not a bool (0/1, `"True"`, None and NaN are never coerced), a batch
  carries it for some records only, or a frame carries it only inside cells
  (`in_audit_not_bool`); or when the id the entry point would stamp is not
  exactly `^NCT[0-9]{8}$` or another id the input carries disagrees
  (`nct_id_malformed`). Several flags that agree, such as a frame's
  `in_audit_x` and `in_audit_y`, pass when none is True.
- Flag or not, input must be plain JSON data or it is refused by type
  (`in_audit_not_bool`): in the module's words, "a record is exactly a dict
  with exactly-str keys and, at any depth, values that are exactly dict,
  list, str, int, float, bool or None". A dict subclass, a tuple, a numpy
  value or an object with attributes is not opened. A sponsor frame must be
  exactly a DataFrame (otherwise `TypeError`); its flag columns and index
  levels, its cells and its `attrs` are read.
- `is_flag_key` reads as the flag the exact names, with pandas' `_x`/`_y`
  merge suffixes removed (`in_audit`, `inAudit`, `IN_AUDIT`, `in-audit`,
  `is_in_audit`, `in_audit_x`), and any name whose ASCII words hold "in"
  then "audit" (`in_audit_manifest`, `manifest_in_audit`, `in_audit_flag`).
  `domain_audit`, `main_audit`, `login_audit` and `within_audit` are not
  the flag.
- Not seen, by design (the module's list): "a flag the caller removed or
  renamed to other words, written with look-alike or full-width letters,
  serialized into a string (JSON or repr text, a CSV round trip of a dict
  column), held as a value (DataFrame.melt, key/value lists), or set as a
  DataFrame attribute instead of a column" (an attribute of the frame
  object; `attrs` is read). "A caller who deletes the flag defeats any
  scanner; the guarantee rests on the caller's exclusion."
- A CT.gov study dict, an AACT-shaped sponsor frame and the indexes built
  from them carry no `in_audit`. For those inputs every M2 entry point relies
  on its caller to leave holdout trials out. The parser's fragment functions
  (`parse_outcome_measure`, `extract_flow_started`,
  `align_flow_and_outcome_groups`, `parse_error_trial_outcomes`,
  `classify_population_description`, `normalize_population_text`,
  `population_audit`, `structural_checks`) always rely on the caller.
- `write_ecosystem_baseline` refuses a baseline block that names an NCT id.
- No code calls M2 today except its own tests. M3 reads M2's output from a
  file (`--population-parsed`) and reads a sector from a manifest's
  `ecosystem_category`; see M3 below.

### M4 (citation verifier, provenance, batch planner)

- **CLI.** `verify` and `plan` both require `--index`; a missing `--index` is
  a usage error (exit 2). Every index row must carry a bool `in_audit`. The
  rows with `in_audit: false` are the in-scope set, and only membership of
  that set admits a trial. Every row of `--documents`, `--locations`,
  `--profiles` and `--doc-cache`, and every `--nct-id`, is checked against it
  before any page map is opened; a refusal exits 2
  (`AuditHoldoutViolationError`, `InvalidNctIdError` or `PlanScopeError`).
  `plan` also refuses (exit 2) a `--profiles` row whose trial is not in that
  set and a `--doc-cache` row whose `sha256` is not that of a document of an
  in-scope trial, so M1 output written from a larger index is refused
  against a smaller one; the playbook's step 2 filters those rows out.
- **Free text (m4 `d68652a`, D3).** A string that is not under an identifier
  key (an NCT id key, a URL key, `filename`) is free text: a quotation, a
  note, a registry-copied title, a section heading M1 copied from a
  document. An audit id in free text, in any reading, is still refused
  before anything is written (exit 2), and so is a malformed id-like token.
  Any other trial it names, in the index or not, is counted and reported as
  the warning `NOTE_FREE_TEXT_NAMES_OTHER_TRIAL` (`free_text_other_trials`
  in `verify`'s report and in the plan, counts only, never an id), and does
  not stop the run. Identifier keys and values, URLs and file names keep the
  strict rule, and so does longitudinal provenance: a study's provenance is
  still refused when a `seeAlsoLinks` URL names another trial.
- **Page maps and shared documents (D4).** `verify --pages-dir` gives the
  page provider only the `(nct_id, sha256)` pairs of in-scope documents, so
  no other page map is opened. A content-addressed page map of such a
  document, or a `--doc-cache` entry for it, may be tagged with any trial
  that is not held out, and the map is served to every in-scope trial that
  posts the document. A map or entry tagged with a holdout trial refuses the
  run before any page text is read (exit 2).
- **Library.** `verify_census_corpus`, `verify_document_record`,
  `verify_location_record`, `verify_citation`, `verify_trial_completeness`
  and `enforce_audit_holdout` need an explicit bool `in_audit` or
  `in_scope_nct_ids`, and a non-empty `audit_nct_ids` unless
  `allow_empty_audit_set=True` (which in turn needs `in_scope_nct_ids`);
  `verify_census_corpus` always needs `in_scope_nct_ids`.
  `assemble_longitudinal_provenance` and `to_outcomes_layout_provenance` take
  the same basis. The pure helpers (`check_note_page_counts`,
  `check_neutral_language`, `scan_neutral_language`,
  `scan_neutral_language_record`, `match_quotation_on_page`,
  `normalize_for_page_match`, `has_ellipsis`, `validate_citation_shape`,
  `validate_status_citations`, `validate_strict_nct_id`) take no holdout
  argument and rely on the caller.

### M3 (outcomes bundles)

- **Build.** `--cohort-manifest` is required. It is the allowlist, and its
  bool `in_audit` is the only flag that admits a trial. A holdout trial is
  written as a stub: its index row holds the fields copied from its manifest
  or trial row (`ecosystem_category` included, when the row has one), masked
  badges and `has_reported_results` null, and its shard record holds
  `nct_id`, `in_audit`, `cohorts` and `shard`, with its four sections
  (`reported_results`, `document_profile`, `decision_rule_locations`,
  `longitudinal_provenance`) null. A bundle therefore names the
  holdout trials, as the site's outcomes index does, and carries none of
  their results, documents, locations or provenance. Any side input for a
  holdout trial (a `--documents`, `--locations`, `--provenance` or
  `--population-parsed` record, or a section carried in its own record) is
  refused with `AuditFirewallError` (exit 2) before `--out-dir` is touched,
  and the population parser is never called for it.
- Whoever writes the manifest must not compute a holdout trial's
  `ecosystem_category` with M2: M3 copies the field as given.
- **Check.** `--check DIR --cohort-manifest M` requires the tree to hold
  exactly M's trials, each holdout as a stub.

## 5. Design limit: the firewall is keyed on NCT ids

Every module decides what is a holdout by NCT id. ClinicalTrials.gov posts
byte-identical documents under different NCT ids: in the census
`documents.jsonl` on `swarm/rule-census-20261114`, 5 sha256 values are each
shared by 2 trials. An id-keyed firewall therefore cannot keep a holdout
trial's document text out when the same file is also posted under an
in-scope id. The in-scope trial's copy is fetched, cached and quoted under
the in-scope id, and nothing in any module notices.

Excluding such text would take a list of the holdout trials' document
digests, and building one means handling the holdout trials' documents,
which is itself a decision about holdout material. No branch builds or
consults such a list, and this document does not investigate whether any
holdout trial's document is shared.

A document shared by two in-scope trials is verifiable for both. M1 writes
one page map per sha256 and tags it with the trial that first passed the
firewall for those bytes. Up to m4 `97789ee`, M4 served such a map only to
the trial it named, so `verify --pages-dir` stopped at the second in-scope
trial that posted the document (exit 1), or before any row when the tagged
trial was outside a shard's index (exit 2). Since m4 `d68652a` (D4), M4
serves the map to every in-scope trial that posts the document, whichever
trial it is tagged with, and refuses a map tagged with a holdout trial
before it reads any page text (exit 2, no report). On fictional M1 output at
m1 `4defbef` and m4 `d68652a`: two trials citing one shared protocol
verified with exit 0; a shard whose index lacked the tagged trial verified
with exit 0; and the same map with its tag changed to the fictional holdout
stopped `verify` with exit 2. D4 leaves the design limit above as it is: the
firewall still decides by NCT id, so a holdout trial's document posted
under an in-scope id is read under that id.

## 6. Model provenance and review status

- **Per-row model ids (F6).** M4 requires every location row to carry a
  non-empty `model_id` and `prompt_version`, and every document row a
  non-empty `model_id` and `profile_version` (`LOCATION_REQUIRED_KEYS`,
  `DOCUMENT_REQUIRED_KEYS`). `verify` reports a row without them as
  `LOCATION_MISSING_KEYS` or `DOCUMENT_MISSING_KEYS` and exits 1. `plan`
  stops with exit 1 and writes no plan when any `--documents` or
  `--locations` row, or any document of a `--profiles` row that is not
  exactly as M1 writes it, lacks `model_id`. Both were run on fictional rows
  with and without these keys.
- **M1's profiles (m4 `d68652a`, D1).** M1's profile documents carry neither
  `model_id` nor `profile_version`, and `plan` takes them as M1 writes them:
  a `--profiles` row whose key set is exactly the nine keys M1 writes, whose
  `indexer_rules_version` matches `doc_indexer@YYYY-MM-DD.vN` whole, and
  whose documents have exactly M1's 24 document keys is exempt from F6,
  since no model produced it. A profile document that carries `model_id` is
  held to F6 in full, and so are every `--documents` row, every location row
  and everything `verify` reads. On fictional M1 output at m1 `4defbef`,
  `plan` exits 0 on the profiles as written.
- **The existing census records model ids at worker level only.** None of
  its 782 document rows or 5,229 location rows carries `model_id`,
  `prompt_version` or `profile_version`. Its `manifest.json` names a
  `model_id` per worker:
  - Waves 1 and 2 (the coordinator, the 17 extraction shards `shard-01` to
    `shard-17`, `sample-prim-01`/`02` and `second-pass-01`/`02`):
    `gemini-4-carbon-high`; for example `manifest.json` lines 30
    (coordinator), 75 (`shard-01`), 185 (`second-pass-01`) and 536 (the last
    such entry). `STATUS.md` line 56 also names "Gemini 4 Carbon (High)" for
    the 21 extraction workers.
  - Wave 3 (the final 54 trials: `neon-shard-1` to `neon-shard-6`,
    `census-prefetch`, `census-verify-1`/`2`, `census-merger`):
    `gemini-4-neon-high`; for example `manifest.json` lines 544
    (`neon-shard-1`) and 1469 (`wave3_neon_shards.neon-shard-1`),
    `STATUS.md` line 64 and `summary.md` line 11.
  - The Wave 3 planner, `census-planner`: `gemini-4-carbon-high`.
  - The monitor (not an extractor): `claude-sonnet-4-6`, `manifest.json`
    line 52.
  - Tally of `model_id` values in the manifest: `gemini-4-carbon-high` 23,
    `gemini-4-neon-high` 16, `claude-sonnet-4-6` 1.
  - The manifest records a `prompt_sha256` (`manifest.json` line 2,
    `29029eb6...e498`) but no `prompt_version` or `profile_version`.

  So the census is attributable to two extraction models, split by wave.
  Which model produced a given row can be inferred only through the row's
  worker or shard, not from a field on the row. Every location row names a
  worker that the manifest lists (4,718 rows name a Wave 1/2 worker, 511 a
  Wave 3 worker); document rows name no worker, so a document row's model can
  be inferred only through the shard its trial was assigned to
  (`shard_assignment` in `manifest.json`).
- **The census under `verify` at m4 `d68652a`** (run on copies of its
  files, counts only, no page text): no row is refused by the firewall. The
  only errors are F6's, on all 782 document and 5,229 location rows (all
  402 trials). The free text of 7 rows of 1 trial names 1 trial absent from
  the index, reported as `NOTE_FREE_TEXT_NAMES_OTHER_TRIAL` (before D3 this
  refused the whole run, exit 2); 35 `NOTE_PAGE_COUNT_MISMATCH` warnings
  fall in 1 trial; and, with no page text given, all 11,629 quotations are
  uncompared. `verify` exits 1.
- **M3 at `e2a8c21` (its own files unchanged since `ba916ec`) keeps a
  location row's `model_id` and `prompt_version` when the row carries them,
  but does not require them.** It publishes each
  row's `worker`, adds `model_id` and `prompt_version` when present, and
  refuses either one when it is present but empty or not a string. A key
  outside M4's row keys (`LOCATION_ROW_INPUT_KEYS`) is refused rather than
  dropped. A document row's `model_id` and `profile_version` are kept the
  same way. A row without the stamps (every row of the existing census
  lacks them) is published without them, so a bundle built from such rows can
  attribute a row to a model only through its worker. Such a bundle does not
  meet F6 and cannot be published as it is. This was run on fictional rows
  with and without the stamps, most recently at `ba916ec` (28 rows: with the
  stamps, all 28 published with them; without, none, each with its
  `worker`).
- **Review status.** AI output is unreviewed until a person reviews it.
  M4's checks are mechanical: a quotation is found on the cited page, the
  row has the required shape, the status-to-citation rule holds, and the
  commentary passes the neutral-language guard. They do not establish that a
  model located the right rule. Agreement between two AI passes is not review
  either: in the census, the second pass (`second-pass-01`/`02`) ran on the
  same model as the Wave 1/2 primary passes. No branch records a human review
  of any census row, so every row is unreviewed.

## 7. Governance

- `swarm/rule-census-20261114` (at `ee64dc9`) is a branch of
  `michaeldgreenphd/civicsample-engine`, which is a public repository. Its
  `documents.jsonl`, `locations.jsonl` (with verbatim quotations from posted
  documents), `manifest.json`, `STATUS.md` and `summary.md` are publicly
  readable now.
- What happens to that branch is the investigator's decision. These
  documents do not modify it, and nothing in the module branches writes to
  it.
- Nothing on a public branch is private or offline. Census rows on a public
  branch are published, whether or not the site shows them.
- Census output is not published on the site. On 2026-11-27 the
  investigator decided to keep it off the site for now; publishing it later
  is also the investigator's decision. No module writes to the site. M3 is on
  `main` (PR #114), but it only builds a local bundle tree, and nothing copies
  a bundle to the site. This does not make the census private: its rows stay
  publicly readable on the branch above for as long as that branch exists.
- New census work (plans, reports, new rows, merged corpora) goes to a
  gitignored local path, never to a tracked file and never to a public
  branch; the playbook sets that path up and checks it.

## 8. Module reference

### M1: document pre-indexer, readability classifier and page slicer

- **Cache.** `DocumentCacheStore` under `--cache-dir` (default
  `data/document_cache`): `blobs/`, `pages/<sha256[:2]>/<sha256>.json.gz` (a
  page map per document, tagged with a trial id as described in section 5),
  and `url_manifest.json`. `--index-out` (default
  `data/document_index_by_sha256.jsonl`) holds `DocumentIndexEntry` records,
  one line per `(sha256, indexer_rules_version)`; `--profiles-out` (default
  `data/trial_document_profiles.jsonl`) holds one `TrialDocumentProfile` per
  trial. These three paths are in `.gitignore` on `main` since PR #112.
  Identical bytes are extracted once. `INDEXER_RULES_VERSION` is
  `doc_indexer@2026-11-21.v1`. A change of it adds a second line for every
  document indexed again; M4's `plan` reads the lines of one version
  (`--doc-cache-rules-version`, playbook step 2).
- **Index entries are reused only for their selection inputs.** An entry
  records the inputs that shaped its page selection (`selection_inputs`:
  `doc_type`, `min_selected_pages`, `max_selected_pages`) and is served only
  to a request with the same three. Otherwise, and for a line written before
  the inputs were recorded, the document is indexed again from its cached
  page map, with no new extraction, and the new entry replaces the old. A
  document that two trials post with different `doc_type`s keeps the entry
  of the trial indexed last; each trial's profile is built from its own
  request.
- **Integrity.** `CacheIntegrityError` is raised for a cache that disagrees
  with a file's digest or size; for an index line that names a page outside
  `1..physical_pages`, a reversed section range, a page partition that is
  not exact, or a partition list that is not strictly ascending (m1
  `4defbef`; checked when a line is loaded and before one is written); and
  for a page map already in a document's slot that is not the same map
  (another `text_source`, such as an OCR map, or other page text), which is
  left as it is rather than replaced. Through the CLI the last two stop the
  run with exit 1, with the index file and the existing map left as they
  were. A run writes to the cache only if it succeeds whole.
- **Readability.** `READABILITY_STATUSES` has seven values:
  `machine_readable`, `partial_image_or_redacted`, `image_only_scan`,
  `garbled_cmap`, `extraction_failed`, `download_failed`, `no_document`.
  `extraction_failed` (bytes obtained, pages not extracted) and
  `download_failed` (bytes never obtained) both record `physical_pages` as
  null, never 0.
- **Redaction and broken text.** `detect_redactions` sets `REDACTION_FLAGS`
  (`cci`, `ppd`, `white_box_or_blank_stats`); its tests check that
  "Charlson Comorbidity Index (CCI)", "PPD, Inc." and "purified protein
  derivative (PPD)" are not flagged. `analyze_page_cmap` and
  `detect_garbled_cmap` detect broken `ToUnicode` maps. `map_unreadable_recovery_bucket` returns one of
  `UNREADABLE_RECOVERY_BUCKETS` (`image_only_scan`, `redacted_cci_ppd`,
  `garbled_cmap`). These lists are meant to equal M3's `REDACTION_FLAGS` and
  M4's `UNREADABLE_SUBTYPES`; no branch carries M1 together with M3 or M4,
  so no test checks that they do.
- **Upload timing.** `parse_ctgov_date` and `compute_upload_timing` compute
  signed calendar intervals; a partial date (`YYYY-MM`, `YYYY`) is never
  padded with a `-01` day, and an exact day count is given only when both
  dates have day precision.
- **Page slicing.** `select_statistical_pages` partitions pages `1..N` into
  `pages_selected`, `pages_deferred` and `pages_without_text` over the seven
  `STATISTICAL_SECTION_KINDS`; `format_compact_page_ranges` and
  `expand_compact_page_ranges` write and read the ranges.
- **Page-text providers.** `DocumentPageExtractor` is implemented by
  `FixturePageTextProvider`, `JsonlPageMapProvider`, `MacOSPDFKitProvider`
  (which cleans lone UTF-16 surrogates with `sanitize_page_text`) and
  `ChainedPageTextProvider`. Its `get_page_text` is written to match M4's
  `PageTextProvider`; no branch carries both modules, so no test pins that.
  The page layouts do agree: in the check described in section 5, M4 read
  M1's map for the trial it was tagged with.
- **Census bridge.** `to_legacy_census_document_row` and
  `ingest_legacy_census_documents` read census `documents.jsonl` rows.
- **CLI (`scripts/index_trial_documents.py`).** `--nct-id`, `--manifest`,
  `--ingest-census-jsonl`, `--local-pdf-dir`, `--page-map-jsonl`,
  `--cache-dir`, `--index-out`, `--profiles-out`, `--min-selected-pages`
  (default 10), `--max-selected-pages` (default 25), `--rate-limit-sec`
  (default 0.55, shared across processes through
  `<cache-dir>/.ctgov_rate_limit.lock`), `--limit`, `--offline`,
  `--force-download`, `--reparse-cached-pages`. A single-value option given
  twice, `--nct-id` together with `--ingest-census-jsonl`, or an output path
  that names an input or the cache is refused (exit 2). `--offline` refuses a
  live run (exit 1). Exit 0 on success; 1 on other indexer errors and when a
  trial's CT.gov record could not be fetched; 2 on usage and firewall
  refusals.

### M2: analysis-population parser and sponsor ecosystem

**Parser (`src/analysis_population_parser.py`).** `PARSER_RULES_VERSION` is
`analysis_pop@2026-11-24.v2` on `main` (`v1` at `b9a2ebc`; PR #118 bumped
it); `VOCABULARY_SHA256` is computed by
`compute_vocabulary_sha256` over `VOCABULARY_RULES`. `POPULATION_TAXONOMY`
has nine classes that are never merged: `itt`, `modified_itt`,
`full_analysis_set`, `per_protocol`, `evaluable`, `safety`, `other_stated`,
`not_stated`, `ambiguous_multi`. `DENOM_STATUS_LEVELS`, `ARM_GAP_REASONS` and
`AGGREGATION_METHODS` record why a value is absent; a missing count is None,
never 0. A measure with no measure-level denominators whose classes carry a
`Participants` denominator in some classes and none in others has
`denom_status` `class_level_incomplete`, `denom_source` `"none"` and no
`overall_participants_analyzed` (run on a fictional measure). The units
side follows the same rule since PR #118 (`b77314f`): a measure whose
classes carry a unit denominator (such as `Eyes`) in some classes and none
in others has `units_denom_status` `class_level_incomplete` and no
`overall_units_analyzed`, and `structural_checks` holds `units_denom_status`
to `DENOM_STATUS_LEVELS` (`units_denom_status_in_vocabulary`). At
`b9a2ebc`, before that fix, such a measure was reported from the classes
that have a unit denominator, with `units_denom_status` `present`. The
fictional-measure tests that PR added pass at `main` `03fd911`.
`arm_started_minus_analysed` is stored for audit and has no path into
`classify_population_description`. Entry points:
`classify_population_description`, `normalize_population_text`,
`extract_flow_started`, `align_flow_and_outcome_groups`,
`parse_outcome_measure`, `parse_study_outcomes`, `parse_outcomes_frame`,
`select_raw_outcome_populations`, `build_outcomes_from_raw` (re-parses a
retained raw record with the same result as `parse_study_outcomes`),
`parse_error_trial_outcomes`, `structural_checks`, `population_audit`.

**Sponsor ecosystem (`sponsors/ecosystem.py`, `sponsors/ecosystem_rules.csv`).**
`VALID_ECOSYSTEM_SECTORS` (`nih`, `us_federal_other`,
`nctn_cooperative_group`, `academic_medical_center`, `non_profit_foundation`)
and `industry` make up `UNIFIED_SECTORS`. Production attribution uses exact
matching only (`exact_literal`, `exact_normalized`, `subsidiary_of`);
case-insensitive substring `stem` matching is confined to `audit_ecosystem`,
which lists unreviewed candidates for a person to curate. The three review
states (`attributed`, `reviewed_excluded`, `unreviewed`) are never merged.
Every rule carries a non-empty `note`; conflicting rules raise
`EcosystemRuleValidationError` (a `ValueError`). When two rules claim one
literal for the same canonical, the literal is `shared=yes` only when both
rules say yes; every rule in the shipped `ecosystem_rules.csv` says
`shared=no`, and `expected_ecosystem_counts.json` is unchanged by the rule.
The industry layer's `match_literals` (`sponsors/sponsor_roles.py`) had the
same fault, and `check_cross_layer_conflicts` reads the industry claims
through it; it is fixed on `main` (PR #116), so on `main` the cross-layer
check reads the corrected marker too.
`compute_ecosystem_baseline` and `write_ecosystem_baseline` keep
SHA-256-keyed baselines in `tests/sponsors/expected_ecosystem_counts.json`.
Since PR #119 each block also pins the index's `shared` column per
organization, and since PR #124 per as-registered entity
(`ECOSYSTEM_BASELINE_SECTIONS` ends with `shared` and `shared_entities`).
Every shipped rule says `shared=no`, so both sections of the stored block
are empty; each is recorded under `sections_backfilled` as added to the
existing block.
`sponsors/company_aliases.csv` and `tests/sponsors/expected_counts.json` are
not changed.

### M4: citation verifier, longitudinal provenance and batch planner

**Verifier (`src/citation_verifier.py`).** `VERIFIER_RULES_VERSION` is
`1.7.0`. Seven `ELEMENTS` per primary outcome; six `STATUSES` (`FOUND`,
`NOT_FOUND`, `NOT_APPLICABLE`, `AMBIGUOUS`, `UNREADABLE`, `NO_DOCUMENT`).

- **Status-to-citation rule (F5), defined once here.**
  `validate_status_citations`: `CITATION_REQUIRED_STATUSES` (`FOUND`,
  `AMBIGUOUS`, `NOT_APPLICABLE`) need at least one citation;
  `CITATION_FORBIDDEN_STATUSES` (`NOT_FOUND`, `NO_DOCUMENT`) take none;
  `NO_DOCUMENT` is refused when posted documents exist, and any citation is
  refused when the trial has none. `validate_citation_shape` holds the
  six-key citation shape (`CITATION_REQUIRED_KEYS`: `filename`, `sha256`,
  `physical_page`, `printed_page`, `section_heading`, `quotation`). M3
  imports these instead of restating them.
- **Single-page quotations.** `match_quotation_on_page` matches a quotation
  within its one declared page, exactly (`exact_ws`) or after
  NFKC/typographic/line-break-hyphen normalization (`unicode_nfkc_hyphen`,
  via `normalize_for_page_match`). A quotation may not hold an ellipsis
  (`has_ellipsis`), may not span pages (`cross_page_span_violation`), and is
  at most 300 characters (`MAX_QUOTATION_CHARS`).
- **Searches and documents.** `NOT_FOUND` is refused while
  `pages_not_searched` is non-empty (`NOT_FOUND_WITH_UNSEARCHED_PAGES`)
  unless `--allow-non-exhaustive-not-found` is given. Document presence is
  true, false or unknown: `NO_DOCUMENT` is refused when posted documents exist
  (`NO_DOCUMENT_WITH_POSTED_DOCUMENT`) and reported when presence is unknown
  (`NO_DOCUMENT_PRESENCE_UNKNOWN`). A trial-keyed page map that declares no
  sha256 cannot verify a citation (`CITATION_PAGE_VERSION_UNKNOWN`).
- **Neutral language.** `scan_neutral_language` (alias
  `check_neutral_language`) flags claims that a trial succeeded or failed,
  statistical-significance result claims, spin phrases and compliance or
  misconduct inferences. In location verification it reads every key and
  string of the row except each citation's `quotation`.
- **Quotations not compared.** A quotation checked against no page text is
  counted in `quotations_not_compared`, not passed: `verify` without
  `--pages-dir` reports not ok and exits 1.
- **Completeness.** With `--check-completeness`, every in-scope trial in
  `--index` must have the full grid of 7 elements times K primary outcomes on
  the `primary` pass, and on every other pass it has; the sparse passes
  (`SPARSE_PASSES`: `ocr_recovery`, `adjudication`) may cover some cells only,
  within bounds and without duplicates (`TRIAL_GRID_MISSING_CELL`,
  `TRIAL_GRID_UNEXPECTED_CELL`, `TRIAL_GRID_DUPLICATE_CELL`). K comes from
  each index row's `primary_outcomes_count` or `n_primary_outcomes`.
- **Free text and shared documents (1.7.0).** Free text naming a trial that
  is not held out gives the warning `NOTE_FREE_TEXT_NAMES_OTHER_TRIAL`, one
  per record (a document, a location row, a citation checked on its own),
  and the report's `free_text_other_trials` counts them (`rows`,
  `distinct_ids`, `distinct_ids_in_scope`, `distinct_ids_not_in_scope`);
  `CorpusVerificationReport` has the three counts as fields. Warnings do not
  fail a run. A content-addressed page map is served to every in-scope
  trial that posts its document unless its tag is a holdout trial (section
  4).

**Provenance (`src/longitudinal_provenance.py`).** Library only; no script
writes a provenance file. `LONGITUDINAL_SCHEMA_VERSION`
(= `PROVENANCE_SCHEMA_VERSION`) is 1 and `PROVENANCE_RULES_VERSION` is
`1.5.0`. `parse_partial_date`, `parse_iso_or_partial_date`,
`parse_ctgov_upload_date`, `parse_printed_doc_date` and `parse_pubmed_date`
keep each date's precision; `compute_day_delta_interval` gives `exact_days`
only when both dates have day precision, and a `[min_days, max_days]` range
otherwise. `assemble_longitudinal_provenance` combines the registry's
references (`registry_reference_types` `BACKGROUND`, `DERIVED`, `RESULT`),
`pubmed_records`, `europe_pmc_records` and records from an injected
`PublicationProvider`, keeping
`LINK_ORIGINS` (`AUTO_INDEXED`, `INVESTIGATOR_SUBMITTED`) separate.
`to_outcomes_layout_provenance` projects a record into M3's shard schema.
An explicit DOI (a registry reference's, `moreInfoModule`'s, or the `doi`
field of a PubMed or Europe PMC record) must, once normalised, match
`_EXPLICIT_DOI_RE` whole (`10.`, a 4 to 9 ASCII-digit registrant code, `/`
and a suffix with no whitespace, so SICI-style DOIs are kept). A value that
does not, such as `"N/A"` or a bare `"doi:"`, is not used as a DOI: the
record falls back to a DOI in its citation text, and the dropped value is
counted in the record's `dropped_malformed_doi_count`, which the projection
leaves out.

**Planner and verifier CLI (`scripts/verify_and_plan_census_batch.py`).**
`PLANNER_RULES_VERSION` is `1.9.0`.

- `verify`: `--index` (required), `--documents` (required), `--locations`
  (required), `--pages-dir`, `--ocr-dir` (only with `--pages-dir`),
  `--pdf-root`, `--text-source` (default `pdfkit`), `--check-completeness`,
  `--allow-non-exhaustive-not-found`, `--allow-extra-document-keys`,
  `--report-out`.
- `plan`: `--index` (required), `--cohort`, `--documents`, `--locations`,
  `--doc-cache`, `--profiles`, `--mode` (`resume-unstarted`,
  `recover-unreadable`, `incremental-delta`; default `incremental-delta`),
  `--pass-role`, `--nct-id` (repeatable), `--shard-size` (default 18),
  `--budget-cap-usd` (default 50.0), `--cost-per-trial-sliced-usd` (default
  0.085), `--cost-per-trial-full-usd` (default 0.4011),
  `--cost-per-trial-conservative-usd` (default 0.5512), `--cost-schedule`
  (`sliced`, `full`, `conservative`; default `sliced`), `--pages-dir`,
  `--ocr-dir`, `--text-source`, `--doc-cache-rules-version`, `--out-plan`.
- `plan` files each in-scope trial in one partition: `cached_complete`,
  `complete_quotations_unchecked`, `unstarted_primary`,
  `unstarted_target_pass`, `amended_doc_or_outcome_delta`,
  `unreadable_recoverable_ocr`, `unreadable_terminal_redacted`,
  `unreadable_subtype_unknown`. A trial is `cached_complete` only when every
  one of its current documents (its profile's documents, else its
  `--documents` rows) passes `verify_document_record` (planner 1.8.0) and
  every one of its rows passes `verify_location_record` with page text
  (`--pages-dir`). A document `verify` would refuse (`http_status` 200 with
  no page count, say) files the trial `amended_doc_or_outcome_delta`, to be
  run again. Without page text a complete trial is
  `complete_quotations_unchecked`. `classify_unreadable_subtype` names the
  UNREADABLE subtype from the trial's rows and document-index lines; when it
  names none, the trial's profile `readability_summary` names it if that is
  one of `UNREADABLE_SUBTYPES` (a summary that is not a string is refused,
  exit 1), and a trial with no rows is UNREADABLE only when its summary
  names one. An UNREADABLE trial whose subtype nothing names is
  `unreadable_subtype_unknown`, neither skipped as redacted nor scheduled.
  M1 has no status for a document redacted throughout. It gives a document
  `readability_status` `partial_image_or_redacted` when any page carries a
  redaction flag, has no text or has a broken character map, and the
  recovery bucket `redacted_cci_ppd` when the document has redaction flags
  and no page without text: a document readable apart from its redacted
  pages. A trial whose documents are of that kind is summarised
  `partial_image_or_redacted`, so `plan` files it, when it has no rows,
  `unstarted_primary` and schedules it, and its readable pages are
  extracted. The summary rule for `redacted_cci_ppd` applies only to a
  profile whose trial summary says so, which M1 does not write.
- `plan` keys `--profiles` by trial and `--doc-cache` by sha256: two
  different rows for one trial, or for one sha256 within the rules version
  read, raise `CitationVerificationError` (exit 1, no plan), and an
  identical repeat is kept once.
- `--doc-cache` lines are read under one M1 rules version (D2).
  `--doc-cache-rules-version V` reads the lines whose
  `indexer_rules_version` is `V` and leaves the others out, counted, with a
  NOTE on stderr; a document with no line of `V` is planned as one with no
  index line. Without the option a file of one version is read as before,
  and a file holding two or more is refused (exit 1), with a message naming
  the option. The option without `--doc-cache`, or empty, is a usage error
  (exit 2). Every line is firewalled first, the lines left out included.
- `plan` takes M1's profiles as written (D1; section 6).
- The plan reports `partition_counts`, `excluded_in_audit_count`,
  `ocr_prepass_queue`, `cost_projection`, `max_launchable_shards`,
  `launch_approved`, the shard manifests (each with `within_budget_cap`),
  `doc_cache_rules_version` (the version read, or null),
  `doc_cache_lines_ignored_other_rules_versions` and
  `free_text_other_trials`.
- `--cohort` selects rows whose `cohorts` list (or `cohort` string) holds
  the slug; a row with neither field belongs to no cohort.
- Exit codes: 0 when verification passes or the plan is within the budget
  cap; 1 on verification errors, quotations not compared, contract failures,
  a `--doc-cache` of several rules versions without
  `--doc-cache-rules-version`, or a plan over the cap (the plan is still
  written); 2 on usage errors and firewall or scope refusals. Warnings do not
  change the exit code.

### M3: cohort-agnostic outcomes bundles

- **Layout.** `index.json` (one row per trial, sorted by `nct_id`, top-level
  `"data"` last) and `shards/{n}.json.gz` with `n = int(nct_id[3:]) % N`,
  the same function as `src/site_layout.py` `shard_of`; `N` defaults to 256
  (`DEFAULT_SHARDS`, at most `MAX_SHARDS` = 1024), and every shard file is
  written, empty or not. `outcomes_bundle.json` marks a tree this module
  wrote. JSON is compact ASCII and gzip is level 9 with `mtime=0` and OS byte
  `0xff`, so the bytes are deterministic.
- **Records.** `SHARD_RECORD_KEYS`: `nct_id`, `in_audit`, `cohorts`, `shard`,
  `reported_results`, `document_profile`, `decision_rule_locations`,
  `longitudinal_provenance`. `derive_cohorts` assigns slugs from
  `STANDARD_COHORTS` (`industry_phase3`, `nih`, `cooperative_group`,
  `academic`, `us_federal_other`, `non_profit_foundation`,
  `all_large_docs`), mapping M2 sector names through
  `ECOSYSTEM_SECTOR_TO_COHORT`. Decision-rule locations keep each pass
  (`primary`, `second`, `ocr_recovery`, `adjudication`) and an effective pass
  in which an `ocr_recovery` cell overrides the `primary` one and an
  `adjudication` cell overrides both; M4's
  status-to-citation rule is applied when rows are normalized and again when
  a tree on disk is checked.
- **Writing.** `write_outcomes_tree` writes only to a target that is absent,
  empty, or a bundle it wrote. It refuses a folder of a site data family
  (`SITE_DATA_FAMILIES`: `outcomes`, `results_structure`, `sponsors`, `geo`,
  `sex_gender`, `census`, `devices`) or a folder holding another family's
  marker files, stages the tree in `<out-dir>.partial`, validates it with
  `validate_outcomes_tree`, and then swaps it in. The default target is
  refused when the engine's `data/` holds a weekly dataset (`data/run.json`).
- **CLI (`scripts/build_outcomes_shards.py`).** `--cohort-manifest`
  (required for a build), `--manifest-cohort`, `--trials` (every trial must be
  in the manifest), `--documents`, `--locations` (repeatable), `--provenance`,
  `--population-parsed`, `--extracted-at` (else the `extracted_at` of the
  `--trials` and `--cohort-manifest` containers, which must agree, else
  `--now-utc`; a build refuses when none is given and never stamps the build
  time), `--now-utc`,
  `--source-pipeline-commit` (alias `--pipeline-commit`; default the engine
  checkout's HEAD; a build refuses when neither resolves), `--shards`,
  `--out-dir` (default `data/outcomes_bundles`, resolved from the script's
  location), `--report`, `--check DIR`, and `--allow-null-commit` (with
  `--check` only). `--check` refuses any build-only option. Exit 0 on
  success, 1 on contract, input or validation errors, 2 on
  `AuditFirewallError` and, with `--check`, on a holdout leak found in the
  tree.
- **Timestamps.** Every timestamp the builder reads (`--extracted-at`,
  `--now-utc`, a container's `extracted_at`, a Module 1 profile's
  `extracted_at`, a Module 4 record's `assembled_at` when it is not null) is
  a UTC instant written `YYYY-MM-DDTHH:MM[:SS[.ffffff]]` then `Z` or
  `+00:00` (`UTC_TIMESTAMP_RE`). A date alone, a time with no offset,
  another offset, `-00:00` and the basic form are refused (exit 1; run on
  fictional inputs). A null `assembled_at`, which Module 4 writes by
  default, is published as null.
- **Input containers.** A JSON `--cohort-manifest` or `--trials` container
  holds its rows under one of `data`, `trials`, `records` or `rows`. Beside
  them it may hold only header stamps (`INPUT_STAMP_KEYS`: `extracted_at`,
  `pipeline_commit`, `source_pipeline_commit`, `indexer_rules_version`,
  `run_id`, `schema_version`) and, in a manifest, `columns_note` and
  `excluded`. Any other key is refused with `AuditFirewallError` (exit 2),
  because its content would be dropped; a fictional index with a
  `generated_at` header was refused this way at `ba916ec`, and the rule is
  in `ccfa413` too. The site index's top-level keys on 2026-11-24
  (`columns_note`, `excluded`, `rows`, `run_id`, `schema_version`) are all
  accepted in a manifest.
- **Input shapes.** Provenance metadata is held to Module 4's
  `LongitudinalProvenanceRecord` types (`provenance_schema_version` an int,
  `provenance_rules_version` a str, `excluded_standalone_icf_count` an int
  that is not negative). A CT.gov `primaryOutcomes` or `outcomeMeasures`
  that is present and not a list, or a `protocolSection`, `resultsSection`,
  `outcomesModule` or `outcomeMeasuresModule` that is present and not an
  object, is refused, naming its type; absent or null reads as none.
  `--check` reports a cohort or location `element` that is not a string as
  an error (exit 1) instead of failing on it.

## 9. Verification

The CI gate, as `.github/workflows/ci.yml` runs it, from the repository root
after `pip install -r requirements-dev.txt`:

```bash
python -m compileall -q src scripts sponsors tests
python scripts/validate_fixes.py
python -m pytest tests -q
node --test tests/sponsors/*.test.mjs
```
