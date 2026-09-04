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
