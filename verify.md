# Verification guide

Run commands from the **civicsample-engine repository root**, unless stated
otherwise. Read [AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md) and applicable
nested/override instructions before choosing checks and before completion.
This guide describes commands established by source and configuration; it is
not a record of a successful run. Report executed commands, versions, results,
skips and limitations separately in the task or PR.

## Setup and quick local path

The weekly pipeline and [CI](.github/workflows/ci.yml) target **Python 3.11**;
CI pins **Node 22** for the built-in JavaScript test runner. The manual LLM
workflow targets **Python 3.12**. Shared Python code must work on both target
versions. There is no backend server, database, container or test service to
start, and ordinary checks require no credentials. The first-view excerpt
test fetches two public GitHub files at the revision pinned in
[tests/fixtures/site_overview/SOURCE.json](tests/fixtures/site_overview/SOURCE.json).
It skips on connection failure outside CI, but fails when `CI` is set. A local
pass with that skip is not complete CI verification. Other ordinary tests use
local fixtures or stub their external operations.

Inspect an existing environment before reusing it; its directory name does
not establish its Python version. For a fresh ordinary development environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
node --version
python -m pip install -r requirements-dev.txt
```

[requirements-dev.txt](requirements-dev.txt) includes [requirements.txt](requirements.txt).
Versions have lower bounds; there is no Python lockfile. Keep dependencies in
these files, and do not introduce an ad-hoc workflow install. The only
package.json, [sponsors/package.json](sponsors/package.json), declares ESM; it
has no dependencies or install step, and no npm lockfile is expected. Tests
also exercise Bash and Git; `jq` is needed for the workflow-summary cases
(those cases explicitly skip when it is absent). GitHub's Ubuntu runner has
these tools. Report a non-target runtime as a verification limitation.

Start every change with branch/status inspection. After setup, the quick
complete local path is the existing CI checks, without reinstalling:

```bash
git status --short --branch
python -m compileall -q src scripts sponsors tests
python scripts/validate_fixes.py
python -m pytest tests -q -rs
node --test tests/sponsors/*.test.mjs
git diff --check
git status --short
```

Expected evidence: every command exits 0, the extractor harness reports its
cases passing, pytest reports passes and explains skips, Node reports zero
failures, and diff-check emits no whitespace errors. Run each command even
when investigating an earlier failure, then report the failure; do not mask it
with a pipeline, `|| true`, changed expectations or weaker gates. For exact
CI equivalence, use Python 3.11 and Node 22 and install with
`pip install -r requirements-dev.txt` first, as CI does. Run
`CI=true python -m pytest tests -q -rs` when checking CI's mandatory network
behavior; do not unset CI to turn an unreachable pinned source into a skip.

Compileall creates ignored `__pycache__` directories; pytest uses temporary
fixture directories and an ignored `.pytest_cache`. The sponsor regression
test intentionally **writes** [tests/sponsors/expected_counts.json](tests/sponsors/expected_counts.json)
and fails when a changed rules hash has no baseline. Review and commit that
block with a deliberate rules change; never reset unrelated changes or weaken
the guard to get green. For auditing a read-only checkout, run a disposable
local copy/clone instead. Tests that stub publication do not push or contact
GitHub. Deactivate the virtual environment when finished; remove only temporary
directories created for the task, after retaining evidence.

## Select checks by change

Use targeted checks while iterating. A completed full-suite run already covers
those tests; repeat checks only after relevant changes, failures or unresolved
concerns. The table selects diagnostics and additional evidence without
replacing the full CI gates above before completion.

| Change | Targeted command/evidence |
|---|---|
| Documentation or instructions | `python -m pytest tests/test_docs_wiring.py -q`; check each changed link and command against the source. This test covers AGENTS/CLAUDE claims, not every README/verify link. Keep AGENTS' first Bash block identical to CI's run steps. |
| Extractor behavior, eligibility or demographics | `python scripts/validate_fixes.py`; add the failing study shape to that offline harness. For sex/gender, also `python -m pytest tests/sex_gender -q`. Show before/after counts and provenance for analytical changes. |
| Sponsor rules, filtering or audits | `python -m pytest tests/sponsors -q` and `node --test tests/sponsors/*.test.mjs`; inspect the hash-keyed baseline diff and uncovered-rule report. See [sponsors/README.md](sponsors/README.md). |
| Dataset layout, source fields or shared contract | `python -m pytest tests/test_split_data.py tests/test_check_site_contract.py tests/test_full_records.py -q`; run the site's tests and the connected path below on compatible revisions. |
| Initial Overview summary or the site's default filters | `python -m pytest tests/test_first_view.py -q -rs`; run the real-site first-view parity command below. The pinned excerpt fixture alone does not establish compatibility with the selected frontend revision. |
| Retention, backup, raw-measure archives or publishing workflow | `python -m pytest tests/test_prune_snapshots.py tests/test_raw_measures_archive.py tests/test_full_records.py tests/test_check_site_contract.py tests/test_backfill_archives.py -q`; inspect workflow conditions, staged paths and failure ordering. For retention, run `python /absolute/path/to/civicsample-engine/scripts/prune_snapshots.py --dry-run` **from the site root**. |
| Browser UI | Implement and verify in the frontend repository using its root verify.md in the [frontend repository](https://github.com/michaeldgreenphd/clinical-trial-populations); this repository has no dashboard render path. The sponsor module's Node tests are rule tests, not a browser UI test. |
| LLM prompt, schema or model | Compile + CI; read [prompts/README.md](scripts/extraction/prompts/README.md). A live pilot is additional paid/network verification, not part of ordinary CI. |
| Runtime/dependency or infrastructure configuration | Full CI on its pinned runtimes; inspect affected workflows, permissions, secret names, schedules and publish guards. Do not dispatch publishing workflows merely to validate documentation. |

## Inputs, fixtures and established invariants

- [scripts/validate_fixes.py](scripts/validate_fixes.py) constructs mock CT.gov
  studies for alias mapping, row routing, quarantine and refetch behavior.
  It does not establish current registry availability or API compatibility.
- [tests/sex_gender/](tests/sex_gender/) covers the vendored parser, five
  distinct reporting states, no enrollment-gap balancing, participant-unit
  eligibility, raw-measure reparse parity, provenance, and table write-back
  coverage/failure behavior. A parser error remains a trial with `parse_error`,
  not a dropped row. Percent female requires reported sex, participant counts
  and female + male > 0. The mean of within-trial shares and the participant-
  weighted ratio use the same eligible trials; `test_table.py` tests both.
- The sex/gender snapshot test's external-table case skips unless
  `SEX_GENDER_TABLE` names a parsed CSV gzip. `SEX_GENDER_TABLE_META` optionally
  names its meta JSON; otherwise the path is inferred. Only compare with the
  pinned 2026-06-09 snapshot when claiming an exact regression: its baseline is
  66,210 trials with a sex/gender-titled baseline measure, not every trial in
  a later weekly dataset. Another snapshot's drift is reported as a warning;
  only a table dated 2026-06-09 that misses the baseline fails. Supply the real
  same-run CSV/meta explicitly, preserving both and the baseline:

  ```bash
  SEX_GENDER_TABLE=/absolute/path/to/sex_gender_parsed.csv.gz \
  SEX_GENDER_TABLE_META=/absolute/path/to/sex_gender_parsed_meta.json \
    python -m pytest tests/sex_gender/test_snapshot_regression.py -q -rs
  ```

  Record source_extracted_at, source_pipeline_commit, snapshot_date, row count
  and the warning. Matching counts on a newer table do not establish the
  manuscript's exact regression.
- Sponsor fixtures are the separately SHA-pinned 2026-06-19 industry data and
  a stored 300-trial API/AACT parity sample under
  [tests/sponsors/fixtures/](tests/sponsors/fixtures/). Tests preserve exact
  matching, conflicting-rule failures, disjoint attributed/reviewed-excluded/
  unreviewed states, role/view vocabularies and NCT-ID deduplication for
  partnership and both-role trials. The original-rules count test skips when
  the current rules hash differs; the current hash's regression still runs.
- [tests/split_helpers.py](tests/split_helpers.py) builds synthetic extracted
  records. [tests/fixtures/site_record_contract_layout_v1.json](tests/fixtures/site_record_contract_layout_v1.json)
  is a copied version-1 site contract. The split tests cover whole/split
  layouts, deterministic bytes, modulo shard vectors, invalid contract/data
  rejection, source preservation and layout rollback. Publish tests use real
  temporary Git repositories with stubbed external operations; they do not
  exercise the live site checkout or a GitHub push.
- [tests/test_first_view.py](tests/test_first_view.py) tests summary counts,
  run stamps, JavaScript parity and the pinned site excerpt. The real-site
  parity command uses the selected checkout's app.js and index.html; its VM
  stubs non-Overview functions and DOM operations, so it does not replace the
  served-browser check. Inspect `site_functions_stubbed` and
  `predeclared_stubs_run` in its report. Exit 1 means mismatched results;
  exit 2 means it could not run faithfully. Both block publication.
- The separate industry-analysis cohort in
  [scripts/generate_industry_sponsors.py](scripts/generate_industry_sponsors.py)
  requires interventional, non-terminated studies, primary completion since
  2009-01-01 and positive female and male counts. Both-positive is the stated
  proxy for mixed-sex eligibility because the input lacks eligibility-sex.
  Its industry lead/first alphabetical industry collaborator selection and
  legacy company-grouping rules are distinct from the curated sponsor bridge.
  The full-record tests cover its input path; they do not establish dedicated
  cohort/model regression coverage. Changes need explicit count comparisons
  and targeted cases rather than relying only on a green packaging suite.
- The deterministic pipeline does not randomly sample and has no RNG seed.
  Registry eligibility is results posted, optionally filtered by condition
  and date (`src/api_client.py`); the weekly command uses results since
  2009-01-01. `--limit` takes the first returned studies and is a debug cap,
  not a representative sample or a guaranteed cardinality if upstream returns
  fewer studies. There is no dedicated API-client test for live pagination,
  invalid CLI limits or registry request failures; do not infer those guarantees
  from fixture coverage. LLM outputs are non-deterministic; model IDs and run
  provenance, not seeds, identify the run.

## Connected verification

The interface is **published files**, not a live engine API. The site owns
`tests/record_contract.json` (read fields and layout version/switch) and
`tests/data_budget.json` (part count and size budgets). The engine's
[split_data.py](scripts/split_data.py) reads that exact contract;
[check_site_contract.py](scripts/check_site_contract.py) validates current
records, run stamps, summary and every snapshot offered by history.json.
Missing fields, duplicate IDs, wrong stamps, unsupported layouts, broken shards,
unopenable snapshots and hard size limits block; budget excess warns in the
engine and fails the site's own size-budget tests.

The supported production layout is selected by the site contract, not by
whether the engine has split support. With layout.enabled false, the engine
writes whole-record parts with no layout header; the reader also supports
version-1 split datasets. Turning split on is a separate owner decision after
the reader has shipped. Neither missing contract files nor a failed test is
permission to enable it, invent a contract, use the fixture copy, or select
older revisions merely to obtain a pass. Check the contract and its reader
implementation together in the intended frontend revision.

Full source records remain in permanent data-* releases beginning 2026-08-28.
Earlier full records survive in site Git history. Site parts must not
substitute for full-record producer input. Other producer-owned files include
industry analysis, sex/gender table/meta/methods, sponsor bridge/audits,
condition ontology and manual LLM outputs. The weekly
[extract workflow](.github/workflows/extract.yml) and manual
[LLM workflow](.github/workflows/run_extractions.yml) define the exact publish
inventories. Geography is a separately pinned site-owned contract; preserve
its pin and files throughout a rehearsal.

### Readiness and existing-published-data check

Use a frontend checkout at its intended review base plus the proposed commits;
consult its root verify.md. In an isolated worktree the sibling default below
may point to the wrong checkout: set site_root to the actual path. Inspect
both branches, dirty state and refs; fetch the configured upstream when
accessible and record a failure to fetch. Do not assume local main equals
origin/main. Resolve the selected comparison base once to a commit SHA and
use that same SHA for every compared frontend run.

```bash
site_root="$(cd ../clinical-trial-populations && pwd)"
git status --short --branch
git rev-parse HEAD main origin/main
git -C "$site_root" status --short --branch
git -C "$site_root" rev-parse HEAD main origin/main
# Confirm origin/main is the intended review base; substitute it if necessary.
comparison_base="$(git -C "$site_root" rev-parse 'origin/main^{commit}')"
printf '%s\n' "$comparison_base"
test -f "$site_root/tests/record_contract.json"
test -f "$site_root/tests/data_budget.json"
```

After both readiness tests pass, run the engine gates against the existing
published site files. This does not need a full producer input. Run the block
in Bash with Python 3.11 and Node 22 selected; its subshell stops at a failed
gate, leaving its report for inspection. A frontend npm install can require
registry access.

```bash
verification_dir="$(mktemp -d /tmp/civicsample-site-check.XXXXXX)"
(
set -eu
site_latest="$(python - "$site_root/history.json" <<'PY'
from datetime import date
import json, sys
with open(sys.argv[1]) as fh:
    latest = json.load(fh).get('latest')
if not isinstance(latest, str) or date.fromisoformat(latest).isoformat() != latest:
    raise SystemExit('history.latest must be YYYY-MM-DD')
print(latest)
PY
)"
python scripts/check_site_contract.py --site "$site_root" --latest "$site_latest" \
    --report "$verification_dir/site_check.json"
record_args=()
for part in "$site_root"/data/demographics.part*.json.gz; do
    record_args+=(--records "$part")
done
node scripts/first_view_parity.mjs "${record_args[@]}" \
    --summary "$site_root/data/dashboard-summary.json" \
    --app "$site_root/app.js" --index "$site_root/index.html" \
    > "$verification_dir/first_view_parity.json"
cat "$verification_dir/first_view_parity.json"
(cd "$site_root" && npm ci && DOCS_WIRING_BASE="$comparison_base" npm test)
)
```

Expected: checker exit 0 with `site contract check: ok`, record/byte counts and
warnings; parity exit 0 with ok true and its checked count; frontend zero
failures. This exercises the actual published data and selected consumer code,
not registry extraction or regeneration. A legacy dataset lacking run.json,
history.latest or firstView may fail the current publish gate while still
rendering in the backward-compatible reader. Diagnose its publish provenance;
do not synthesize missing metadata to make the gate pass.

The cache-key test compares the merge base with committed HEAD. For local
edits also inspect `git -C "$site_root" diff -- app.js styles.css geo/ index.html`
and check changed assets' query-string versions; repeat the explicit-base
comparison after the local commits exist. Preserve reports before removing
only this task's verification_dir. A browser check uses the frontend guide's
served-page procedure and remains separate evidence.

### Producer-to-browser smoke test

Run this for a contract/publishing change or when real-input integration is
needed. It uses substantial disk and memory and may need network access for
npm dependencies. Prerequisites are the authoritative contract and reader,
a complete full-record file (demographics.json or .json.gz), and its matching
sex/gender CSV/meta. Record the source release/date, file digests and both code
revisions. Use an existing retained input or an explicitly requested registry
run; do not silently download a new dataset for a documentation change.
Confirm the CSV's row count, NCT-ID set, snapshot_date and parser_rules_version
agree with its meta and full-record inputs. The generators do not independently
prove that a supplied CSV belongs to a matching meta file.

The input's snapshot_date must be at least the newest date already offered by
the selected site. Older local inputs cannot exercise the current publication
recipe: retention refuses to move history backwards. Obtain a matching full
input from the selected date's retained release, or a newer complete run with
its table/meta. Do not remove newer archives, restamp old records or change
expectations to get a pass. An isolated historical replay is different evidence
and requires its own explicitly selected historical publication state.

The clone checks the frontend's committed HEAD and keeps Git metadata. If
verifying uncommitted frontend changes, apply only their reviewed tracked diff
and intended new files at the marked point. Retain the comparison_base SHA
resolved above: origin/main inside a local clone can name the source checkout's
local main, not its current upstream. The check below verifies that the exact
comparison commit is available in the disposable copy.

From the engine root, set the three input paths to existing same-run files.
Use site_root and comparison_base from readiness above. This block stops before
subsequent writes on failure; the temporary copy remains for diagnosis.

```bash
full_records=/absolute/path/to/demographics.json.gz
parsed_table=/absolute/path/to/sex_gender_parsed.csv.gz
parsed_meta=/absolute/path/to/sex_gender_parsed_meta.json
integration_dir="$(mktemp -d /tmp/civicsample-integration.XXXXXX)"
(
set -eu
test -f "$full_records"
test -f "$parsed_table"
test -f "$parsed_meta"
frontend_revision="$(git -C "$site_root" rev-parse HEAD)"
git rev-parse HEAD
git status --short
git -C "$site_root" status --short
git clone --quiet --no-hardlinks "$site_root" "$integration_dir/site"
git -C "$integration_dir/site" checkout --quiet --detach "$frontend_revision"
# If testing uncommitted frontend work, apply only its intended changes here.
test -f "$integration_dir/site/tests/record_contract.json"
test -f "$integration_dir/site/tests/data_budget.json"
git -C "$integration_dir/site" cat-file -e "$comparison_base^{commit}"
snapshot_date="$(python - "$full_records" "$parsed_meta" "$integration_dir/site/history.json" <<'PY'
from datetime import date
import json, sys
from src import full_records
with open(sys.argv[2]) as fh:
    meta = json.load(fh)
snapshot_date = meta.get('snapshot_date', '')
if date.fromisoformat(snapshot_date).isoformat() != snapshot_date:
    raise SystemExit('snapshot_date must be YYYY-MM-DD')
with open(sys.argv[3]) as fh:
    history = json.load(fh)
dates = history.get('dates', []) + ([history['latest']] if history.get('latest') else [])
if dates and snapshot_date < max(dates):
    raise SystemExit('input predates the site history: obtain a same-date or newer complete run')
_, extracted_at, pipeline_commit = full_records.load(sys.argv[1])
if not extracted_at or not pipeline_commit:
    raise SystemExit('full records lack run stamps')
if (meta.get('source_extracted_at'), meta.get('source_pipeline_commit')) != (extracted_at, pipeline_commit):
    raise SystemExit('parsed table metadata and full records are from different runs')
print(snapshot_date)
PY
)"
mkdir -p "$integration_dir/generated"
python scripts/generate_mobile_data.py --demographics "$full_records" \
    --table "$parsed_table" --out "$integration_dir/generated/dashboard-summary.json"
python scripts/generate_industry_sponsors.py "$integration_dir/generated" --demographics "$full_records"
python scripts/split_data.py --contract "$integration_dir/site/tests/record_contract.json" \
    --demographics "$full_records" --out-dir "$integration_dir/dataset" \
    --snapshot-date "$snapshot_date"
# Archive/prune while the disposable site's data/ still holds its outgoing run.
python scripts/prune_snapshots.py --site "$integration_dir/site" \
    --latest "$snapshot_date" --report "$integration_dir/retention.json"
python - "$integration_dir" <<'PY'
from pathlib import Path
import shutil, sys
root = Path(sys.argv[1]); data = root / 'site' / 'data'
for pattern in ('demographics.part*.json.gz', 'studies_tab.part*.json.gz', 'run.json'):
    for path in data.glob(pattern):
        path.unlink()
if (data / 'detail').exists():
    shutil.rmtree(data / 'detail')
shutil.copytree(root / 'dataset', data, dirs_exist_ok=True)
PY
cp "$integration_dir/generated/dashboard-summary.json" "$integration_dir/generated/industry_sponsors.json" "$integration_dir/site/data/"
cp "$parsed_table" "$integration_dir/site/data/sex_gender_parsed.csv.gz"
cp "$parsed_meta" "$integration_dir/site/data/sex_gender_parsed_meta.json"
python scripts/check_site_contract.py --site "$integration_dir/site" \
    --latest "$snapshot_date" --report "$integration_dir/site_check.json"
record_args=()
for part in "$integration_dir/site"/data/demographics.part*.json.gz; do
    record_args+=(--records "$part")
done
node scripts/first_view_parity.mjs "${record_args[@]}" \
    --summary "$integration_dir/site/data/dashboard-summary.json" \
    --app "$integration_dir/site/app.js" --index "$integration_dir/site/index.html" \
    > "$integration_dir/first_view_parity.json"
cat "$integration_dir/first_view_parity.json"
(cd "$integration_dir/site" && npm ci && DOCS_WIRING_BASE="$comparison_base" npm test)
python -m http.server 8001 --bind 127.0.0.1 --directory "$integration_dir/site"
)
```

Expect a passing checker, parity and frontend suite before the server starts.
Once it announces readiness, open [the local rehearsal](http://127.0.0.1:8001/).
Compare a representative study ID, record count, source stamps and displayed
summary values with the generated files using the frontend's root verify.md.
Selecting the newest date must load data/; select one retained complete
snapshot and one aggregate to check the supported historical behavior. For
split layout observe deferred Studies/detail requests and matching stamps;
inline layout needs no such requests. Exercise loading, empty-filter and
fetch-failure states as the frontend guide specifies.

This smoke regenerates the dataset, summary and industry analysis, stages the
same-run sex/gender CSV/meta, and exercises retention in the disposable copy.
It leaves the newest run only in data/, as production does. Sponsor/audit/
methods, LLM, condition ontology and other untouched inputs are copied from
the source site; do not claim those flows were rebuilt or share the new run.
For a complete publication rehearsal follow their relevant producers in
extract.yml against temporary outputs and rerun both gates. No production
files, geography pin, branches, releases or remote services are changed.

Ctrl-C stops the server. Preserve the reports, then remove only this task's
temporary directory with `rm -rf -- "$integration_dir"`. No publish, commit,
push or scheduled job is part of the smoke test.

## Network and paid verification

A live registry check is warranted for API-query, pagination or refetch
changes when fixture tests cannot establish the upstream behavior. The normal
CLI is `python -m src.extract_all --output data/demographics.json --results-after 2009-01-01`;
`--limit 10` bounds a diagnostic run. It also writes raw-measure data and a log,
so run it in a disposable engine checkout, retain provenance/output and use
the producer smoke above. It is not needed for routine documentation edits.

LLM runs use a separate Python 3.12 environment and
`python -m pip install -r scripts/extraction/requirements.txt`. Environment
names are `ANTHROPIC_API_KEY`, `AI_PROVIDER`, `RUN_MODE`, and, for the Vertex
path, `GCP_PROJECT_ID`, `GEMINI_VERSION` and `GOOGLE_APPLICATION_CREDENTIALS`.
The workflow creates the credential file from `VERTEX_CREDENTIALS_JSON`.
Read the selected script before running it; pilot mode caps documents but
still makes paid calls and writes retained outputs. The Actions pilot also
commits/publishes, so use it only when that external action is intended.
`OPENROUTER_API_KEY` is not wired into the scripts.

Publishing needs `PIPELINE_DEPLOY_TOKEN`; release steps use `GH_TOKEN` from
the Actions token. Optional Drive secrets are `GDRIVE_CLIENT_ID`,
`GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID`, mapped by
the workflows to `CLIENT_ID`, `CLIENT_SECRET`, `REFRESH_TOKEN`, `FOLDER_ID`
plus `CHILD_FOLDER`, `FILE_PATH`, `FILE_NAME`. `CURRENT_DATE`, `GITHUB_SHA`
and `GITHUB_RUN_ID` supply run provenance in CI. No secret is necessary for
offline checks; do not print or persist secret values as verification evidence.
