# civicsample-engine

The data pipeline behind [civicsample.com](https://civicsample.com), the
ClinicalTrials.gov demographics dashboard.

**This repo computes; the site repo serves.** Everything here runs on a
schedule or by hand, produces data files, and pushes them to
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations),
where GitHub Pages serves them to the dashboard. Nothing in this repo is part
of the website itself — if you want to change how the dashboard looks or
behaves, you're in the wrong repo; if you want to change what the numbers
*are*, you're in the right one.

## What happens every Sunday

The weekly run (`extract.yml`, Sundays 06:00 UTC) does six things, in order:

1. **Download** — pulls every study with posted results from the
   ClinicalTrials.gov API (~80,000 trials, results posted 2009 or later).
2. **Standardize** — maps each trial's free-text race, ethnicity, sex, and
   gender labels onto NIH/OMB standard categories, classifies conditions
   against `condition_ontology.json`, and quarantines labels that don't
   belong (a real example from the test suite: a trial that listed
   "Condom" and "IUD" in its race table).
3. **Package** — builds a small summary file for mobile and rebuilds the
   industry-sponsor analysis.
4. **Back up** — attaches the week's full study records
   (`demographics.json.gz`), the raw sex/gender measures and the run log to a
   dated `data-YYYY-MM-DD` GitHub Release in this repo. Every week's release
   is kept permanently: it is the complete record of that pull, which the
   site's parts are not (releases start at 2026-08-28). Google Drive also
   gets a copy when configured.
5. **Publish** — cuts the week's records into the files the site's own
   field contract asks for (`scripts/split_data.py`): every record whole in 8
   compressed parts, or, once the site turns its split layout on, the fields
   the dashboard reads at startup in 8 parts, the Studies tab's in 8 more and
   the study pop-ups' in 256 small files, one fetched per pop-up. Before they
   replace last week's in the site's `data/` folder, the retention script
   (`scripts/prune_snapshots.py`) archives last week's dataset as a dated
   snapshot for the dashboard's "View snapshot" feature when the retention
   policy keeps it, and prunes old snapshots so the site stays deployable.
   Then it copies this week's files and the other finished files into
   `data/`, which serves the latest week, and checks what it staged against
   the site's contract and size budget (`scripts/check_site_contract.py`)
   before it pushes: a missing field, a malformed file, a snapshot the
   dashboard offers that would not open, or a file over GitHub's limits stops
   the publish (and the later steps that publish to the site), growth past
   the budget only warns.
6. **Sponsor bridge** — attributes every trial's sponsors to canonical
   companies via the curated rules (`sponsors/`), publishes the bridge table
   and the audit files to the site in a second commit, and writes the
   curation inbox here. Runs after step 5 so a rules problem can never delay
   the site's data. The audit says *why* every label moved since last week
   (a registry edit or a rules change) and how long each uncurated name has
   been waiting.

Every run ends with a summary table on its Actions page — trial count,
change vs the previous week, artifact sizes, code commit — and emits a
warning if the dataset shrank, so a bad pull announces itself instead of
waiting to be noticed.

If the numbers on the dashboard are wrong, the bug is in step 1 or 2
(`src/`). If a file the dashboard needs is missing or stale, it's in step 4
or 5 (`scripts/` or the workflow).

## The manual runs

`run_extractions.yml` (run by hand from the Actions tab) sends PDFs through
LLMs — three models each, so results can be compared — for three document
streams: FDA device decision summaries, AI/ML validation papers, and
clinical-trial manuscripts. Results are committed here as the permanent
record; the files the dashboard's "Paper Data Extraction" and "Approval
Queue" tabs display are copied to the site repo.

The instructions the models receive live in
[`scripts/extraction/prompts/`](scripts/extraction/prompts/) as plain-text
files — editing a prompt is a text change and a PR, no Python required.
See the README in that folder for the workflow (including how to test a
prompt change cheaply with a pilot run).

## Maintenance workflows

One more workflow exists for rare occasions, harmless to ignore:
`geo-snapshot-watcher.yml` (monthly check that opens an advisory issue
here when AACT publishes a newer geography snapshot than the one the site
is pinned to — it never downloads data or touches anything). Acting on
that issue happens in the site repo: it owns `scripts/geo/advance_run.py`,
because every path that script writes is a site path. No workflow rebuilds
old snapshot folders from the site's history any more (the one that did
could not rebuild a split snapshot). The full records of every week from
2026-08-28 on are on its `data-*` release; earlier weeks' exist only in the
site repository's git history, which is never rewritten.

## Where things live

| Path | One-line job |
|---|---|
| `src/` | Talk to ClinicalTrials.gov and standardize demographics. One extractor per dimension (race, ethnicity, and the legacy sex and gender extractors kept for the dashboard's sg=v1 path until cutover), each a pure function that is easy to test |
| `scripts/` | Turn extracted data into the files the site serves (split, mobile summary, industry analysis, snapshot pruning, the sex/gender table, methods text, audit and side-by-side) |
| `scripts/extraction/` | The LLM-over-PDFs streams |
| `scripts/extraction/prompts/` | The model instructions, as editable plain text — one file per stream |
| `scripts/utils/` | LLM cost logging and JSON repair, shared by the extraction streams |
| `sponsors/` | The company-level sponsor filter: curated rules (schema — PR-gated), matching module, adapter, browser filter. See `sponsors/README.md` |
| `data/sponsor_audit/` | The weekly curation inbox, the open backlog with ages, and the two change logs (literal-level and trial-level) with causes |
| `src/sex_gender_parser.py` | The sex and gender manuscript's deterministic parser, vendored unchanged. Its rules are the paper's; changes go into the bundle first, then get re-vendored |
| `src/sex_gender_table.py` | The per-trial `sex_gender` row built from that parser: five-state status, outcomes, provenance, `enrollment_minus_parsed`. No denominator balancing |
| `data/sex_gender_audit/` | The weekly sex/gender curation inbox: unrecognized labels with ages, labels that left the inbox, bucket changes with causes, and the status-count drift, all stamped with the parser rules version |
| `src/sex_gender_parser.py` | The sex and gender manuscript's deterministic parser, vendored unchanged. Its rules are the paper's; changes go into the bundle first, then get re-vendored |
| `src/sex_gender_table.py` | The per-trial `sex_gender` row built from that parser: five-state status, outcomes, provenance, `enrollment_minus_parsed`. No denominator balancing |
| `data/sex_gender_audit/` | The weekly sex/gender curation inbox: unrecognized labels with ages, labels that left the inbox, bucket changes with causes, and the status-count drift, all stamped with the parser rules version |
| `condition_ontology.json` | The condition category tree. Canonical copy — edit it here; the weekly run publishes it to the site |
| `data/` | Inputs: pilot PDF sets and review CSVs, tracked so CI can run pilots. Also the committed record of LLM extraction outputs. Weekly artifacts are *never* committed here (`.gitignore` enforces this) |
| `.github/workflows/` | The schedules. `ci.yml` gates every push: everything must compile, the extractor harness must pass, and `pytest tests` must pass (sponsor rules, index, adapter parity against AACT, the 2026-06-19 fixture regression, the three-week audit loop, snapshot pruning) |

Every script states at the top of its file what it reads, what it writes,
and what invokes it. If you're unsure whether something is safe to run,
read its header; destructive ones (like `prune_snapshots.py`) have a
`--dry-run` flag.

## Secrets (Settings → Secrets and variables → Actions)

Only two are required:

| Secret | Why |
|---|---|
| `PIPELINE_DEPLOY_TOKEN` | Fine-grained PAT, Contents read/write on `clinical-trial-populations` only. How finished files reach the site |
| `ANTHROPIC_API_KEY` | LLM extraction (Claude) |

Optional — everything works without them:

| Secret | Why |
|---|---|
| `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID` | Google Drive backups of raw data, logs, and cost tracking. When unset, backup steps are skipped; when set but failing, the run warns and continues — **a Drive problem can never block publishing** |
| `VERTEX_CREDENTIALS_JSON`, `GCP_PROJECT_ID` | Dormant Gemini path — only touched when a manual run selects `ai_provider: vertex_gemini` |
| `OPENROUTER_API_KEY` | Stored, but **not wired up yet** — see below |

No secret value ever appears in code, config, or committed data — CI
injects them as environment variables at run time.

**OpenRouter status:** the API key is stored as a secret, but the
pipeline cannot use it yet. Testing extraction with open-weight models
requires a code change first: an `openrouter` branch at the `AI_PROVIDER`
seam in the three extraction scripts, a chosen list of model IDs to run
(OpenRouter serves many; the pipeline must name which ones), matching
pricing rows in `scripts/utils/cost_tracker.py` so token costs stay
tracked, and an `openrouter` option in the workflow's `ai_provider`
dropdown. Until that lands, `anthropic` is the only live provider.

## Running it locally

```bash
pip install -r requirements.txt                       # weekly pipeline
pip install -r scripts/extraction/requirements.txt    # + LLM streams

pip install -r requirements-dev.txt  # + pytest, for the test suite

python scripts/validate_fixes.py    # the extractor test harness (no network)
python -m pytest tests -q           # the Python test suite
node --test tests/sponsors/*.test.mjs   # the browser filter module
python -m src.extract_all --output data/demographics.json --results-after 2009-01-01
# the site's dataset files, from the site's own contract (a site checkout's tests/)
python scripts/split_data.py --contract ../clinical-trial-populations/tests/record_contract.json
```

Run everything from the repo root — paths are relative to it.

## Dates, times, and versions

How runs and data are stamped, so you can always answer "what data is this
and when was it made":

- **Schedules are UTC.** The weekly run fires Sundays 06:00 UTC; the
  snapshot date is the runner's UTC date on that day.
- **Every weekly artifact says when its data was pulled.** The demographics
  parts and `dashboard-summary.json` carry `extracted_at` (the moment the
  CT.gov pull ran). Derived files carry both stamps —
  `industry_sponsors.json` has `generated_at` (when it was built) *and*
  `source_extracted_at` (which pull it was built from) — so a derived file
  can never silently outrun its source.
- **Versions of published data are the latest week and the dated snapshot
  folders** in the site repo (`snapshots/YYYY-MM-DD/`), all listed in
  `history.json`, which is what the dashboard's "View snapshot" dropdown
  reads; its `latest` names the week the site serves from `data/` (that week
  has no snapshot folder of its own until the next week's run archives it).
  How far back they go: a visitor can open four complete datasets, the
  latest week and three archived weekly snapshots about two weeks apart
  (one per fortnight, counted from a fixed Sunday), reaching back five to six
  weeks. Only a fortnight's first week is archived, so a link to the latest
  week (`?sgsnapshot=<date>`) keeps opening that week after the next run only
  when it is a fortnight's first; a fortnight's second week drops out of
  `history.json` when the next week is published, and its link then opens
  the latest data, with no notice. Every older month keeps one summary
  snapshot: the dashboard draws every chart from it, and its 500 most recent
  studies keep their own records for their pop-ups
  (`archive_records.json.gz`, written from that week's own files); filters
  and the full study table need a complete snapshot. The full
  files of every week stay in the site repository's git history, which is
  never rewritten, whether or not the week was ever archived (a fortnight's
  second week never is: it lives in `data/` only); from 2026-08-28 on, each
  week's full records are also on its `data-*` release. To get a week's files
  back, in a full clone of the site repository (not a shallow one):

  ```bash
  # any week the site served, as its weekly run published it into data/,
  # into a folder <dir>
  c=$(git log -1 --format=%H --grep='^Update demographics data <date>')
  test -n "$c" && mkdir -p <dir> && git archive "$c" data | tar -x -C <dir> --strip-components=1
  # a snapshot folder that was slimmed or deleted, back in place
  c=$(git log -1 --format=%H --diff-filter=D -- snapshots/<date>/demographics.part1.json.gz)
  git restore --source="$c^" -- snapshots/<date>/
  ```
- **LLM extraction runs are versioned by their commits and their metrics.**
  Output files keep stable names (so the site always reads the latest);
  each run's commit records who triggered it, the pipeline and mode, and
  links the Actions run, and each stream's token-metrics file carries a
  `run_info` block (UTC timestamp, code commit, workflow run id). Full
  logs and per-call token costs are archived to Google Drive under
  date-stamped names (cost logs use America/New_York wall-clock).
- **The geography tab is pinned, not rolling.** `data/geo/active_run.json`
  in the site repo names the exact run the tab displays; it only advances
  when a human runs that repo's `scripts/geo/advance_run.py` and merges the
  PR.

Two more conventions: all artifact timestamps are timezone-aware UTC, and
every artifact records the git commit of the code that produced it —
weekly files carry `pipeline_commit` (derived files carry their source's
commit, e.g. `source_pipeline_commit`), and LLM metrics carry it inside
`run_info`. A same-day manual re-run replaces that day's data in `data/`
by design; the workflow warns and says so in the commit message when it
happens.

## Ground rules

Adopted when this repo was created, to keep it from re-growing the clutter
the old monorepo had:

1. **No dead code.** If nothing invokes it, delete it — git history is the
   archive. (The import deliberately left behind two superseded extractors,
   two stale notebooks, and a config file nothing read.)
2. **One producer per file.** Every file the site serves is written by
   exactly one script, named in the tables above. If you retire a script,
   retire or re-home its outputs in the same PR.
3. **Dependencies live in requirements files**, and CI installs from them —
   never ad-hoc `pip install` lines in workflows, which is how the old
   repo's requirements drifted from what CI actually used.
4. **Tests gate merges.** `ci.yml` runs the offline harness on every push.
   When you fix an extraction bug, add the case that caught it to
   `scripts/validate_fixes.py`.
5. **Outputs aren't source.** Weekly artifacts are gitignored here and
   committed only to the site repo by CI. The one exception — LLM
   extraction results — is deliberate: they cost money to produce and are
   the record the approval queue reviews.

## Provenance

Imported from
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations)
at commit `cbee018` as fresh files (no git history carried over). That repo
keeps the dashboard and its published data; pipeline code is maintained
only here.
