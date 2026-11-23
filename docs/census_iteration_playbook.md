# Census iteration playbook

How to run the next iteration of the protocol/SAP decision-rule location
census with the module branches described in
[expandable_architecture.md](expandable_architecture.md): resume trials not
yet started, recover documents classified `UNREADABLE`, and extend the census
to new cohorts. Everything here runs by hand. Every row a model writes is
unreviewed AI output until a person reviews it.

The commands use M1 (`scripts/index_trial_documents.py`), M4
(`scripts/verify_and_plan_census_batch.py`), M2 as a library, and M3
(`scripts/build_outcomes_shards.py`), so they need a checkout that carries the
branches they name. They were checked against m1 `69edfb3`, m2 `fe7399e`, m4
`dab323c` and m3 `ccfa413`.

## 0. Before anything runs

### 0.1 Governance

- The existing census is on `swarm/rule-census-20261114` (at `ee64dc9`), a
  branch of a **public** repository. Its rows, including verbatim quotations
  from posted documents, are publicly readable now. What happens to that
  branch is the investigator's decision: do not modify, move or delete it,
  and do not push census output to it or to any other public branch.
- Nothing on a public branch is private or offline.
- Publishing census output on the site is the investigator's decision. M3
  (outcomes bundles) stays on its unmerged branch until that decision is
  made, and a bundle built from it stays on local disk.
- The `in_audit` holdout trials (those whose `in_audit` is true in the site's
  `data/outcomes/index.json`) are never opened, fetched, parsed, searched,
  quoted, cached or recorded. When you need to compare ids with the holdout
  set, compare id sets and look at counts only.

### 0.2 A gitignored working directory

Every output of census work (plans, verify reports, new rows, merged corpora,
build reports) goes to `census-work/`, a local directory that git ignores
through this clone's own exclude file. The repository's `.gitignore` does not
list it, so set it up once per clone and check it before writing anything:

```bash
mkdir -p census-work
EXCLUDE="$(git rev-parse --git-path info/exclude)"
grep -qxF 'census-work/' "$EXCLUDE" || echo 'census-work/' >> "$EXCLUDE"
for p in census-work/x data/document_cache/x data/document_index_by_sha256.jsonl \
         data/trial_document_profiles.jsonl data/outcomes_bundles/x; do
  git check-ignore -q "$p" || echo "NOT IGNORED: $p"
done
```

The `data/` paths are M1's and M3's outputs; the m1 and m3 branches add them
to `.gitignore`. On a checkout without those branches' `.gitignore` they
print `NOT IGNORED`: stop, and do not write there until they are ignored.
Never `git add` anything under these paths.

### 0.3 Inputs

- **The outcomes index.** The site's `data/outcomes/index.json` (in a
  checkout of `clinical-trial-populations`). Its rows carry `nct_id`, a bool
  `in_audit`, `protocol`, `sap` and `n_primary_outcomes`, and no cohort
  field. Below it is `$INDEX`:

  ```bash
  INDEX=../clinical-trial-populations/data/outcomes/index.json
  ```

- **The existing census, copied out read-only.** `git show` reads the branch
  without checking it out or changing it:

  ```bash
  mkdir -p census-work/rule-census-20261114
  for f in documents.jsonl locations.jsonl manifest.json; do
    git show "origin/swarm/rule-census-20261114:rule-census-20261114/$f" \
      > "census-work/rule-census-20261114/$f"
  done
  ```

  At `ee64dc9` its 402 trials include none whose `in_audit` is true in the
  site index (id sets compared, counts only). M4 refuses any row of a
  holdout trial in any case.

- **The existing census cannot go through `plan` or `verify` as it is.** None
  of its 782 document rows or 5,229 location rows carries `model_id`,
  `prompt_version` or `profile_version`, which M4 requires on every row
  (F6). `plan` stops with exit 1 and writes no plan when a `--documents` or
  `--locations` row lacks `model_id`; `verify` reports
  `DOCUMENT_MISSING_KEYS` and `LOCATION_MISSING_KEYS` and exits 1. (Both
  were run on fictional rows with and without these keys.) The rows' own
  fields cannot supply the stamps: a location row's model can only be
  inferred through its `worker` (`workers.<worker>.model_id` in
  `manifest.json`), a document row's only through its trial's shard
  (`shard_assignment`), and the census recorded a `prompt_sha256` but no
  prompt or profile version. Whether to stamp a local copy of the existing
  rows, and how to mark such stamps as inferred, is the investigator's
  decision; do not invent values. Until it is made, `plan` and `verify` run
  only on rows written with the stamps, which is to say new work.

  Without the census rows, `plan` cannot tell which trials the census has
  done and schedules every in-scope trial in `--index`. To plan only the
  trials the census has not covered, give it a copy of the index in which
  those trials, and no others, carry the cohort `not_in_census`, and pass
  `--cohort not_in_census` (step 2). Every row stays in the copy. The holdout
  rows stay in, so the holdout set is still enforced. The census's rows stay
  in, unscheduled, so that its trials stay in scope: `plan` refuses (exit 2)
  input whose text names a trial outside the in-scope rows of `--index`, and
  the text M1 indexes from a remaining trial's document can name a census
  trial (a section heading, say, in a document both trials post or in the
  remaining trial's own):

  ```bash
  python - "$INDEX" census-work/rule-census-20261114/locations.jsonl \
    > census-work/index_cohort_not_in_census.json <<'EOF'
  import json, sys
  index, census = sys.argv[1:3]
  done = {json.loads(line)["nct_id"] for line in open(census) if line.strip()}
  rows = json.load(open(index))["rows"]
  for r in rows:
      if r["in_audit"] is False and r["nct_id"] not in done:
          if "cohorts" in r or "cohort" in r:
              sys.exit("an index row already names a cohort; stop")
          r["cohorts"] = ["not_in_census"]
  print(json.dumps({"rows": rows}))
  EOF
  ```

## 1. Rules for every run

1. **Holdout.** M1's CLI needs `--manifest` with `--nct-id` or
   `--ingest-census-jsonl` and refuses holdout trials with
   `AuditHoldoutError` (exit 2). M4's `verify` and `plan` need `--index` and
   refuse with `AuditHoldoutViolationError`, `InvalidNctIdError` or
   `PlanScopeError` (exit 2). M3's builder needs `--cohort-manifest` and
   refuses any side input for a holdout with `AuditFirewallError` (exit 2).
   M2 has no CLI and relies on its callers: leave holdout trials out of
   every frame or record you hand it.
2. **The firewall is keyed on NCT ids.** Byte-identical documents are posted
   under different NCT ids (5 sha256 values in the census `documents.jsonl`
   are each shared by 2 trials), so the tools cannot exclude a holdout
   trial's document text when the same file is posted under an in-scope id.
   Do not try to find out whether that happens by opening holdout trials'
   documents. Also, `verify --pages-dir` on M1's cache stops (exit 1, no
   report) at the second trial that shares a document, because M1 tags each
   page map with the first trial and M4 serves a map only to the trial it
   names.
3. **Single-page verbatim citations.** A quotation matches text on its one
   declared page (`match_quotation_on_page`), holds no ellipsis
   (`has_ellipsis`), never spans pages (`cross_page_span_violation`) and is at
   most 300 characters.
4. **Status-to-citation rule (F5).** `FOUND`, `AMBIGUOUS` and
   `NOT_APPLICABLE` need at least one citation; `NOT_FOUND` and `NO_DOCUMENT`
   take none; `NO_DOCUMENT` is wrong when the trial has posted documents. The
   rule is defined once, in `src/citation_verifier.py`
   (`validate_status_citations`), and M3 imports it.
5. **Neutral language.** Every key and string of a location row except each
   citation's `quotation` must pass `scan_neutral_language`: no claims that a
   trial succeeded or failed, no significance claims, no spin phrases, no
   compliance or misconduct inferences.
6. **Append-only passes.** The passes are `primary`, `second`,
   `ocr_recovery` and `adjudication`, recorded in each row's `"pass"`. Never
   edit or overwrite an earlier pass's rows. New rows go to new files under
   `census-work/`, and a combined corpus is a new file built from them.
7. **Per-row provenance (F6).** Every new document row carries `model_id`
   and `profile_version`, and every new location row `model_id` and
   `prompt_version`: the exact model id the run used and the version of the
   prompt or profile that produced the row. Keep `worker` and `pass` too.
8. **Review.** Mechanical verification is not review, and agreement between
   two AI passes is not review. Treat every row as unreviewed until a person
   has reviewed it, and say so wherever the rows are used.

## 2. `UNREADABLE` triage

`classify_unreadable_subtype` (M4 planner) and
`map_unreadable_recovery_bucket` (M1) sort `UNREADABLE` documents into the
same three names, by convention: no branch carries both modules, so no test
checks that they agree.

| Subtype | Meaning | Planner partition | What happens |
|---|---|---|---|
| `redacted_cci_ppd` | Statistical sections redacted (CCI, PPD, blanked boxes) | `unreadable_terminal_redacted` | Never scheduled; no spend (`unreadable_terminal_redacted_spend_usd` is 0) |
| `garbled_cmap` | Text layer present but its character map is broken | `unreadable_recoverable_ocr` | Listed in `ocr_prepass_queue`; scheduled by `--mode recover-unreadable` |
| `image_only_scan` | Pages are images with no text layer | `unreadable_recoverable_ocr` | As for `garbled_cmap` |
| (none named) | `UNREADABLE`, but nothing names the subtype | `unreadable_subtype_unknown` | Neither skipped nor scheduled; needs a person to look |

No branch contains OCR code. The planner lists what needs OCR, and `verify`
can read OCR page maps (`--ocr-dir`, which needs `--pages-dir`, with a
`--text-source` whose name contains the word `ocr`, such as
`macos_vision_ocr_v1`), but producing those maps is outside these branches.
M1's `detect_redactions` can be called on OCR text to catch a scan that turns
out to be redacted.

## 3. How the existing census was run

The census ran in three waves, with workers outside this repository; the
planner only writes shard manifests, and nothing here launches a worker.
According to its `manifest.json`, Waves 1 and 2 (coordinator, `shard-01` to
`shard-17`, `sample-prim-01`/`02`, `second-pass-01`/`02`) used
`gemini-4-carbon-high`; Wave 3 (the final 54 trials: `neon-shard-1` to
`neon-shard-6`, `census-prefetch`, `census-verify-1`/`2`, `census-merger`)
used `gemini-4-neon-high`; the Wave 3 planner used `gemini-4-carbon-high`; and
the monitor, which extracted nothing, used `claude-sonnet-4-6`. The model is
recorded per worker, not per row (see
[expandable_architecture.md](expandable_architecture.md), section 6).

The pattern for the next wave:

1. **Read-only preparation.** Index documents (step 1) and plan (step 2);
   inspect counts, holdout exclusions and projected cost. No paid worker runs
   in this phase.
2. **Extraction.** One worker per shard from the plan, each writing its own
   files under `census-work/` (step 3).
3. **Mechanical verification.** Each shard is verified before it is combined
   with anything (step 4).
4. **Human review** before any use of the rows.

## 4. The loop

### Step 1: index documents (M1)

Run once on the host that will serve page text, so workers start from cached
page maps and the selected statistical pages:

```bash
python scripts/index_trial_documents.py \
  --manifest "$INDEX" \
  --cache-dir data/document_cache \
  --index-out data/document_index_by_sha256.jsonl \
  --profiles-out data/trial_document_profiles.jsonl \
  --min-selected-pages 10 \
  --max-selected-pages 25 \
  --rate-limit-sec 0.55
```

- With `--manifest` alone, every manifest row with `in_audit: false` is
  fetched from ClinicalTrials.gov; add `--nct-id` to limit the run. Live
  requests share a rate limit across processes through
  `data/document_cache/.ctgov_rate_limit.lock`.
- Page text is extracted only on macOS (PDFKit). Elsewhere, pass
  `--page-map-jsonl` files, or each uncached document is recorded as
  `extraction_failed` and the run still exits 0, printing the count on
  stderr.
- To bring the census's own `documents.jsonl` into the cache instead, without
  any network request:
  `--manifest "$INDEX" --ingest-census-jsonl census-work/rule-census-20261114/documents.jsonl`,
  with `--local-pdf-dir` (PDFs under `<dir>/<nct_id>/`) or `--page-map-jsonl`
  so pages can be read.
- Exit 0 on success, 1 on indexer errors or when a trial's record could not
  be fetched (the others are still written), 2 on usage or holdout refusals.

### Step 2: plan the batch (M4)

```bash
mkdir -p census-work/plans
python scripts/verify_and_plan_census_batch.py plan \
  --index "$INDEX" \
  --documents census-work/<corpus>/documents.jsonl \
  --locations census-work/<corpus>/locations.jsonl \
  --doc-cache data/document_index_by_sha256.jsonl \
  --profiles data/trial_document_profiles.jsonl \
  --pages-dir data/document_cache/pages \
  --mode resume-unstarted \
  --shard-size 18 \
  --budget-cap-usd 50.00 \
  --cost-schedule sliced \
  --out-plan census-work/plans/next_batch_plan.json
```

- `<corpus>` is a set of rows that carries the F6 stamps (section 0.3).
  While the existing census cannot be given, leave out `--documents`,
  `--locations` and `--pages-dir` (without `--locations` it changes nothing
  in the plan), pass `census-work/index_cohort_not_in_census.json` as
  `--index` and add `--cohort not_in_census`, so the census's trials are not
  scheduled again.
- `plan` refuses (exit 2, no plan) three kinds of line in `--profiles` and
  `--doc-cache`: (1) a profile whose `nct_id` is not an `in_audit: false`
  row of `--index`; (2) a document-index line whose `sha256` is not that of
  a document in a profile (or `--documents` row) of an in-scope trial; and
  (3) a line of either file whose text names any trial that is not an
  `in_audit: false` row of `--index`, other than a profile's own trial. The
  text includes what M1 copied from the document, such as a section heading
  in `section_slice.detected_sections`.
- Each run of step 1 rewrites the profiles but keeps every earlier line of
  the document index. After a run with `--nct-id` or another `--manifest`,
  the document index holds lines of type (2), and the profiles hold only
  that run's trials: `plan` then schedules the other trials of `--index`
  without their documents (the shard's `doc_set_fingerprints` records an
  empty document set for them). Run step 1 over the whole `--index` again
  before planning it.
- Before each `plan`, filter both files to the `--index` it is given,
  copying each kept line unchanged, and pass the copies as
  `--profiles census-work/profiles_for_plan.jsonl` and
  `--doc-cache census-work/doc_cache_for_plan.jsonl`. The command below is
  the census case; for another `--index`, give that file as its first
  argument. Run it from the root of the checkout: it reads the index with
  M4's `load_index_rows`, as `plan` does, so it takes the same forms (rows
  under `rows`, `data` or `trials`, a bare list, JSONL) and refuses what
  `plan` refuses:

  ```bash
  python - census-work/index_cohort_not_in_census.json \
    data/trial_document_profiles.jsonl data/document_index_by_sha256.jsonl \
    census-work/profiles_for_plan.jsonl \
    census-work/doc_cache_for_plan.jsonl <<'EOF'
  import json, sys
  from pathlib import Path
  from scripts.verify_and_plan_census_batch import load_index_rows
  index, profiles, cache, profiles_out, cache_out = sys.argv[1:6]
  keep = {r["nct_id"] for r in load_index_rows(Path(index)) if r["in_audit"] is False}
  shas = set()
  with open(profiles, "rb") as src, open(profiles_out, "wb") as out:
      for line in src:
          row = json.loads(line) if line.strip() else {}
          if row.get("nct_id") in keep:
              out.write(line)
              shas.update(d["sha256"] for d in row["documents"] if d["sha256"])
  with open(cache, "rb") as src, open(cache_out, "wb") as out:
      for line in src:
          if line.strip() and json.loads(line)["sha256"] in shas:
              out.write(line)
  EOF
  ```

  The filter does not remove lines of type (3). Keeping the census's rows
  in `index_cohort_not_in_census.json` is what keeps type (3) from arising
  for a census trial. If `plan` still exits 2 after the filter, the
  indexed text of an in-scope trial's document names a trial outside the
  in-scope rows of `--index` (a holdout trial among them), and the refusal
  does not name that trial. No branch offers a way past it. Leaving the line
  out of the copy would hide from `plan` the readability fields it reads to
  name an `UNREADABLE` subtype (`classify_unreadable_subtype`), so take it to
  the investigator rather than drop the line.
- At m1 `69edfb3` and m4 `dab323c`, `plan` refuses the profiles step 1
  writes, whatever the `--index`: their document records carry no
  `model_id` or `profile_version`, which `plan` requires of a profile's
  documents as it does of a `--documents` row (exit 1, no plan; run on
  fictional M1 output). Which branch should change is not settled; until
  one does, step 2 cannot take step 1's profiles as they are.
- `--mode resume-unstarted` schedules `unstarted_primary` and
  `unstarted_target_pass` trials; `recover-unreadable` schedules
  `unreadable_recoverable_ocr`; `incremental-delta` (the default) also
  schedules `amended_doc_or_outcome_delta`.
- Leave out `--cohort` with the site index itself: its rows carry no
  `cohorts` or `cohort` field, so any `--cohort` would select none of them.
  Section 0.3's copy and a section 5 manifest carry one. With `--cohort`,
  `excluded_in_audit_count` counts only the holdout rows that belong to
  that cohort, so it is 0 for both, whose holdout rows name no cohort. Those
  trials are still refused wherever they appear.
- Without `--pages-dir`, a complete trial is filed as
  `complete_quotations_unchecked` rather than `cached_complete`, because its
  quotations were not found on their pages.
- Read `partition_counts`, `excluded_in_audit_count`, `cost_projection`,
  `max_launchable_shards` and `launch_approved` before launching anything.
  Exit 0: the whole plan is within `--budget-cap-usd`. Exit 1: it is not (or
  an input failed verification); the plan is still written when it is over
  the cap, and only shards marked `within_budget_cap` fit. Exit 2: a usage,
  holdout or scope refusal.

### Step 3: extraction (outside this repository)

Each worker takes one shard (`shard_id`, `nct_ids`, `doc_set_fingerprints`;
for `ocr_recovery`, `target_elements_by_nct`) and writes its rows to
`census-work/shards/<shard_id>/locations.jsonl`, next to the shard's
`documents.jsonl`. No CLI writes that documents file: M1's
`to_legacy_census_document_row` turns each document record of a trial's
profile into a `documents.jsonl` row, stamped by default with `model_id`
`deterministic_indexer` and `profile_version` set to M1's
`INDEXER_RULES_VERSION`. A worker that writes document rows itself stamps
them with the model it ran on.

1. Read `pages_selected` first.
2. If every one of the 7 elements for every registered primary outcome is
   resolved, record citations under rule 4 of section 1 and stop.
3. Otherwise search `pages_deferred` too, so that `pages_searched` is `"all"`
   and `pages_not_searched` is `[]`, before recording `NOT_FOUND`.
4. Stamp every row with `model_id`, `prompt_version`, `worker` and `pass`
   (rule 7).

### Step 4: verify each shard before it lands (M4)

`--check-completeness` checks every in-scope trial in `--index`, so verify a
shard against an index cut down to that shard's trials:

```bash
SHARD=resume-unstarted-primary-shard-01
mkdir -p "census-work/shards/$SHARD"
python - "$INDEX" census-work/plans/next_batch_plan.json "$SHARD" \
  > "census-work/shards/$SHARD/index.json" <<'EOF'
import json, sys
index, plan, shard_id = sys.argv[1:4]
shard = next(s for s in json.load(open(plan))["shards"] if s["shard_id"] == shard_id)
wanted = set(shard["nct_ids"])
rows = [r for r in json.load(open(index))["rows"] if r["nct_id"] in wanted and r["in_audit"] is False]
print(json.dumps({"rows": rows}))
EOF

python scripts/verify_and_plan_census_batch.py verify \
  --index "census-work/shards/$SHARD/index.json" \
  --documents "census-work/shards/$SHARD/documents.jsonl" \
  --locations "census-work/shards/$SHARD/locations.jsonl" \
  --pages-dir data/document_cache/pages \
  --check-completeness \
  --report-out "census-work/shards/$SHARD/verify_report.json"
```

- A shard lands only when `verify` exits 0: the report is `ok`, with
  `substring_mismatches`, `ellipsis_flags`, `neutral_language_flags` and
  `quotations_not_compared` all 0. Without `--pages-dir`, no quotation is
  compared and the run exits 1.
- For an `ocr_recovery` or `adjudication` shard on its own, leave out
  `--check-completeness` until it is combined with its `primary` rows. For
  `ocr_recovery`, add `--ocr-dir <ocr page maps>` and
  `--text-source macos_vision_ocr_v1` (or whichever OCR source produced the
  maps).
- A shard whose trials share a document with another trial hits the gap in
  rule 2 of section 1.

### Step 5: combine, append-only

```bash
mkdir -p census-work/merged
cat census-work/<corpus>/locations.jsonl census-work/shards/*/locations.jsonl \
  > census-work/merged/locations.jsonl
```

Do the same for documents, then verify the combined corpus (step 4's
`verify`, with `--index` limited to the trials the corpus is meant to cover,
since `--check-completeness` reports `TRIAL_GRID_MISSING_CELL` for every
in-scope index trial without a full `primary` grid). Never write the combined
corpus into a tracked file or a public branch.

### Step 6: outcomes bundles (M3; unmerged, local only)

M3 is held on its branch until the investigator decides whether census output
is published. A bundle built from it is a local file tree, gitignored on the
m3 branch, and nothing copies it to the site:

```bash
mkdir -p census-work/reports
python scripts/build_outcomes_shards.py \
  --cohort-manifest "$INDEX" \
  --manifest-cohort industry_outcomes_beta \
  --extracted-at <extracted-at> \
  --documents data/trial_document_profiles.jsonl \
  --locations census-work/merged/locations.jsonl \
  --out-dir data/outcomes_bundles \
  --report census-work/reports/outcomes_build_report.json

python scripts/build_outcomes_shards.py --check data/outcomes_bundles --cohort-manifest "$INDEX"
```

- `--manifest-cohort` is needed with the site index, whose rows carry no
  cohort: without it, a row with neither a protocol nor a SAP resolves to no
  cohort and the build is refused (exit 1).
- `--extracted-at` is needed too. The builder takes the extraction time
  from `--extracted-at`, else from the `extracted_at` of a `--trials` or
  `--cohort-manifest` container, else from `--now-utc`, and refuses the
  build (exit 1, nothing written) when none is given. The site index's
  header carries no `extracted_at`. `<extracted-at>` is the ISO-8601 UTC
  time the inputs were extracted, not the time of the build.
- `--trials`, if given, may hold only the manifest's trials (the weekly
  `data/demographics.json.gz` holds every trial and is refused). Leave
  holdout trials out of every input except the manifest.
- `--provenance` takes M4 provenance records, but no script writes them:
  `assemble_longitudinal_provenance` and `to_outcomes_layout_provenance` are
  library functions.
- The default `--out-dir` is refused when `data/run.json` exists (a weekly
  dataset in `data/`); pass another gitignored path then.
- At `ccfa413` the bundle keeps each location row's `worker`, and its
  `model_id` and `prompt_version` when the row carries them (F6), but M3
  does not require them: a row without them is published without them.
  Rows that passed step 5's `verify` carry them, because M4 requires them on
  every row. A bundle built from rows without them, such as the existing
  census's, can attribute a row to a model only through its `worker`, so it
  cannot be published as it is.

## 5. Extending to new cohorts

1. Classify sponsors with M2 (`sponsors/ecosystem.py`:
   `build_unified_index`, `trial_cohorts`) without editing
   `sponsors/company_aliases.csv`. M2 relies on its caller here: build the
   sponsor frame without holdout trials.
2. Write a cohort manifest under `census-work/`: one row per trial with
   `nct_id`, `in_audit: false`, `protocol`, `sap`, `primary_outcomes_count`
   and a `cohorts` list of slugs from `STANDARD_COHORTS` in
   `src/outcomes_layout.py`. Add every holdout trial as a row holding only
   its `nct_id` and `in_audit: true`, taken from the site index and nothing
   else, so the tools refuse those trials wherever they appear. Do not
   compute anything else for them.
3. Run step 1 with `--manifest` set to that file, then step 2 with the
   manifest as `--index`, `--cohort <slug>` and `--mode incremental-delta`.
   Filter `--profiles` and `--doc-cache` to the manifest first with step 2's
   filter (the manifest as its first argument, two new output paths under
   `census-work/`) and pass the copies: the document index still holds the
   lines of every earlier step 1 run, and `plan` refuses them (exit 2). The
   filter reads the manifest in any form `--index` takes. A document whose
   indexed text names a trial outside the manifest's `in_audit: false` rows
   is refused as in step 2 (type 3), whatever is filtered.
   Documents with identical bytes are downloaded and extracted once by M1's
   content-addressed cache, but see rule 2 of section 1 for what that means
   for verification.
4. Run steps 3 to 6 within `--budget-cap-usd`, with every output under
   `census-work/`. In step 6, pass the cohort manifest as
   `--cohort-manifest` together with `--manifest-cohort <slug>`: a holdout
   row that holds only `nct_id` and `in_audit` has no cohort of its own, and
   the builder refuses a trial that resolves to none (exit 1).
