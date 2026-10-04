# AGENTS.md

Guidance for AI agents working in this repository.

## What this repository is

The data pipeline behind [civicsample.com](https://civicsample.com). It
computes; the site repo serves. Everything here runs on a schedule or by hand,
produces data files, and pushes them to
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations),
where GitHub Pages serves them to the dashboard.

Nothing here is part of the website itself. A change to how the dashboard
looks or behaves belongs in the site repo; a change to what the numbers *are*
belongs here.

## Repository workflow

Every pull request in this repository is independently reviewed by Codex
agents. Keep changes focused and testable, and include migration or
compatibility notes whenever a change affects published data files, stored
state, or anything the site repo or a downstream consumer depends on.

Practical consequences:

- Prefer several small, single-purpose pull requests over one broad one; a
  reviewer that can hold the whole change in view finds more.
- State what you verified and how. A claim about weekly output should be
  backed by a run against real inputs or a fixture, not by inspection alone.
- Say plainly what you did *not* do, and why, rather than leaving it implied.

## Code Review Rules

- Flag changes that could corrupt, silently alter, or irreversibly delete
  stored data.
- Flag backward-incompatible API, database, configuration, or schema changes
  that lack a documented migration or compatibility path.
- For authentication and authorization changes, verify every relevant entry
  point—not only the primary request path.
- Prioritize concrete correctness, security, data-loss, and regression risks
  over stylistic preferences.
- Confirm that behavior-changing code has appropriate tests, or identify the
  specific untested behavior and resulting risk.
- Do not report formatting or lint issues that should be handled
  deterministically by CI.
- Include the affected scenario and evidence when reporting a finding; do not
  report speculative issues without a plausible failure path.

## Repository-specific review notes

These are the places where a change most easily causes the harm the rules
above are meant to catch.

**This repository writes to another repository.** The weekly job
(`.github/workflows/extract.yml`) pushes finished files into the site repo
using a deploy token, and prunes old snapshots there. A change to what it
writes, or to `scripts/prune_snapshots.py`, can delete published data that
nothing else holds a copy of. Treat retention and publish steps as data-loss
surfaces.

**The sponsor rules file is schema, not data.** `sponsors/company_aliases.csv`
changes only by deliberate commit, and every version of it is keyed by its own
sha256. `tests/sponsors/expected_counts.json` holds one baseline block per
rules sha — index size, per-company any/lead/collaborator, per-entity counts
on the 2026-06-19 industry fixture — and
`test_fixture_regression.py::test_regression_or_explicit_rebaseline` looks up
the block for the rules as they stand.

When a rules change has no block, the test **generates one, writes it, and
fails**, asking for it to be committed alongside the rules change so the moved
counts are reviewed in the same diff. The failure names the drift against the
previous baseline and lists the rules the fixture cannot exercise — those work
in production but are not regression-tested, and are reported rather than
failed. `sponsors/baseline.py` computes all of it.

The fixture file itself is pinned separately, by `FIXTURE_SHA256` in
`tests/sponsors/conftest.py`, so the data under the baseline cannot move
without saying so either.

A pull request that changes the rules and loosens either guard instead of
committing the generated block is the defect the mechanism exists to
surface.

Related invariants worth checking in review: no substring matching in
production attribution paths; the three review states (attributed,
reviewed-excluded, unreviewed) are never collapsed; conflicting rules raise
rather than resolve silently; and every rule carries a non-empty note recording
why it exists, which `load_rules` enforces. That note is free text holding
rationale, not a named decider — do not flag a rule for omitting one.

**The sex/gender parser is vendored, not owned.** `src/sex_gender_parser.py`
is the deterministic parser from the sex and gender manuscript, copied into
this repository unchanged; its regex vocabulary, evaluation order,
`choose_total` and `is_participant_count` rule are the paper's, and a change
to any of them goes into the manuscript bundle first (with a test and a
regenerated vocabulary audit) and is then re-vendored. `src/sex_gender_table.py`
wraps it into the per-trial row stored under `sex_gender` and adds only
provenance and derived columns. Invariants a review checks there: the five
reporting states (`reported`, `explicit_unknown_only`, `uninformative`,
`not_reported`, `parse_error`) are never collapsed; `n_unknown` is only what
the vocabulary mapped to Unknown, and the enrollment gap is stored as
`enrollment_minus_parsed`, shown nowhere as unknown and never a status input
(the legacy extractors' denominator balancing has no code path into the
table — `tests/sex_gender/test_table.py` pins that); `reported_gender` is
category-based, never title-based; cis/trans-qualified labels are never summed
into Female or Male; `COUNT_OF_UNITS` tables stay in the reporting counts and
out of composition; every row carries `parser_rules_version`. The vendored
suite under `tests/sex_gender/` must keep passing as shipped, and the weekly
inbox `data/sex_gender_audit/` may not silently drop a label. The retained raw
measures (`sex_gender_raw_measures.jsonl.gz`, one record per trial with its
`snapshot_date`, `extracted_at` and `pipeline_commit`) are what make a
snapshot re-parseable after a rule change; besides the weekly `data-*`
release and the best-effort Drive copy they go to the permanent
`sex-gender-raw-measures` release. That upload replaces a same-day asset, so
it runs only after a successful extraction and only once
`scripts/check_raw_measures.py` finds the file whole and this run's. A
change that drops that upload, or lets a partial file reach it, is a
data-loss defect.

**The weekly `data-*` releases are the permanent full record.** Each holds
that week's `demographics.json.gz`, every field of every study record. They
are never pruned. The release runs before the site checkout, so a site-side
failure cannot skip it; if it fails, the files are kept as a 90-day workflow
artifact and the run turns red. Releases start at `data-2026-08-28`: for
earlier weeks the full records exist only as the parts committed to the site
repository, whose history is never rewritten. The
site's `demographics.part*.json.gz` are a projection for the dashboard, not a
copy: a script that needs a field the dashboard does not show reads the full
records through `src/full_records.py`, which never falls back to the parts. A
change that deletes `data-*` releases, or reads study fields from the parts
that the site's record contract (tests/record_contract.json in the site repo)
does not list, is a data-loss or correctness defect. The publish step runs
`scripts/check_site_contract.py` on the staged site checkout before it
commits: a part missing a contract field, malformed, from another run or over
GitHub's 100 MiB per-file push limit, or a site over GitHub Pages' 1 GB limit
stops the push and leaves the site on last week's data. The steps that
publish to the site after it (the sex/gender audit, the sponsor bridge) are
skipped too; the week's full-record and raw-measure releases run before the
site checkout and are not. Growth past the site's size budget
(tests/data_budget.json there) only warns. A change that makes that check
advisory, or runs it after the push, is the defect it exists to catch.

**Weekly artifacts are outputs, not source.** They are gitignored here and
committed only to the site repo by CI. The deliberate exception is the LLM
extraction results, which cost money to produce and are the record the
approval queue reviews.

**Provenance stamps are load-bearing.** Artifacts carry `extracted_at`,
`pipeline_commit` (or `source_pipeline_commit`), and for sponsor files
`rules_sha256`. Dropping or faking one makes a derived file able to outrun its
source without anything noticing.

**Secrets never appear in code, config, or committed data.** CI injects them
as environment variables at run time.

## Running the checks

```bash
pip install -r requirements-dev.txt                 # includes requirements.txt
python -m compileall -q src scripts sponsors tests  # everything compiles
python scripts/validate_fixes.py                    # offline extractor harness
python -m pytest tests -q                           # the whole Python suite
node --test tests/sponsors/*.test.mjs               # the browser filter module
```

The LLM extraction stack has its own `scripts/extraction/requirements.txt` and
is not needed for these checks.

`.github/workflows/ci.yml` runs exactly these, in this order, on every push and
pull request.
