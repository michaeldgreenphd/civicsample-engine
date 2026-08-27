# civicsample-engine

The data pipeline behind [civicsample.com](https://civicsample.com) — the
ClinicalTrials.gov demographics dashboard. This repo extracts and standardizes
demographic reporting (race, ethnicity, biological sex, gender identity) from
clinical trials, runs the LLM document-extraction streams (FDA decision
summaries, AI/ML literature, trial manuscripts), and publishes the finished
artifacts into the site repository,
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations),
which GitHub Pages serves.

```
civicsample-engine                          clinical-trial-populations
┌─────────────────────────────┐             ┌──────────────────────────────┐
│ src/        CT.gov extract  │  publishes  │ index.html / app.js / css    │
│ scripts/    split, mobile,  │ ──────────► │ data/*.json.gz, *.csv        │
│             sponsors, LLM   │  via PAT    │ snapshots/ + history.json    │
│ data/       raw inputs      │             │ condition_ontology.json      │
│ .github/    schedules       │             │        │ GitHub Pages        │
└─────────────────────────────┘             └────────┼─────────────────────┘
                                                     ▼
                                              civicsample.com
```

## Layout

| Path | What it is |
|---|---|
| `src/` | ClinicalTrials.gov API v2 client and the demographic extractors (race, ethnicity, sex, gender, condition classifier). Entry point: `python -m src.extract_all` |
| `scripts/` | Artifact builders: `split_data.py` (8 gzipped parts), `generate_mobile_data.py` (dashboard summary), `generate_industry_sponsors.py`, `prune_snapshots.py` (site retention policy), `build_triage_latest.py` (approval-queue CSVs), `gdrive_upload.py`, `validate_fixes.py` (offline test harness) |
| `scripts/extraction/` | Multi-model LLM extraction over PDFs: FDA decision summaries, AI/ML manuscripts, trial manuscripts; plus `fetch_fda_pdfs.py` corpus fetcher |
| `scripts/utils/` | Per-call LLM cost tracking, JSON repair for model output |
| `scripts/geo/advance_run.py` | Deliberate "advance the geography pin" tool — run it from inside a clone of the site repo |
| `condition_ontology.json` | Canonical condition ontology. Read by the classifier here and published to the site (which fetches it at runtime) |
| `data/` | Raw inputs tracked for CI: pilot PDF sets, review CSVs, target lists. Extraction outputs are committed here as the canonical record; full corpora stay outside git (`.gitignore`) |

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `extract.yml` | Weekly (Sun 06:00 UTC) + manual | Full CT.gov extraction → Drive backup → split into 8 parts → mobile summary → industry sponsors → publishes artifacts, archives a dated snapshot, updates `history.json`, and prunes retention **in the site repo** |
| `run_extractions.yml` | Manual | LLM extraction streams (FDA / AI/ML lit / trials lit) with Anthropic or Vertex Gemini; commits results here and publishes the site-consumed subset to the site repo |
| `backfill-releases.yml` | Manual, one-shot | Rebuilds historical `snapshots/<tag>/` dirs in the site repo from its date tags |
| `geo-snapshot-watcher.yml` | Monthly | Advisory only: opens an issue here when AACT publishes a snapshot newer than the site's pinned geography run |
| `ci.yml` | Push / PR | Compiles everything and runs the offline extractor validation harness |

## Secrets to provision (Actions → Repository secrets)

| Secret | Used for |
|---|---|
| `PIPELINE_DEPLOY_TOKEN` | Fine-grained PAT with **Contents: read & write** on `clinical-trial-populations` only — how artifacts get published to the site |
| `ANTHROPIC_API_KEY` | LLM extraction streams (Claude) |
| `VERTEX_CREDENTIALS_JSON`, `GCP_PROJECT_ID` | LLM extraction via Vertex Gemini (optional path) |
| `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID` | OAuth uploads of raw dumps, logs, and cost tracking to Google Drive |

## Local development

```bash
pip install -r requirements.txt              # weekly pipeline deps
pip install -r scripts/extraction/requirements.txt  # + LLM extraction deps

python scripts/validate_fixes.py             # offline extractor tests
python -m src.extract_all --output data/demographics.json --results-after 2009-01-01
python scripts/split_data.py                 # → data/demographics.part1-8.json.gz
```

The scripts use paths relative to the repo root — run them from here.

## Provenance

Code imported from
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations)
at commit `cbee018` as fresh files (no git history carried over). The site
repo remains the home of the dashboard frontend and all published data
artifacts; this repo is the only place pipeline code is maintained going
forward.
