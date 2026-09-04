# System Architecture & Development Guidelines

The source of truth for code generation (Claude Code) and pull request review
(Codex, Copilot) in this repository. `AGENTS.md` holds the review rules
themselves and the places this repository most easily causes harm; this file
describes the system those rules are applied to. Read both.

## 1. Stack & Environment

**This repository computes. It is not the website.** The dashboard is a
separate repository,
[`clinical-trial-populations`](https://github.com/michaeldgreenphd/clinical-trial-populations),
which serves static files over GitHub Pages. A change to how the dashboard
looks or behaves belongs there; a change to what the numbers *are* belongs
here.

* **Core stack:** Python 3.11 for the extraction pipelines, the sponsor
  attribution layer, and the publishing scripts. Runtime dependencies are
  `requests`, `rapidfuzz`, `tqdm`, `numpy` and `pandas` (`requirements.txt`).
  The LLM extraction stack has its own `scripts/extraction/requirements.txt`.
* **A small amount of JavaScript** ships from here as the browser-side sponsor
  filter module, tested with `node --test`. It is the only browser code in
  this repository and exists to keep one filtering rule identical on both
  sides of the split.
* **This repository writes into another repository.** The scheduled jobs push
  finished artifacts into the site repo with a deploy token and prune old
  snapshots there. That is the highest-consequence thing it does.
* **Secrets** never appear in code, config or committed data. CI injects them
  as environment variables at run time.
* **Checks:** see "Running the checks" in `AGENTS.md`. `.github/workflows/ci.yml`
  runs exactly those, in that order, on every push and pull request.

## 2. Agent Responsibilities

* **Generator (Claude Code):** writes implementations, structures multi-file
  edits, runs the checks, and opens the pull request. A claim about weekly
  output is backed by a run against real inputs or a fixture, never by
  inspection alone. It says plainly what it did not do.
* **Reviewers (Codex, Copilot):** audit for security, data loss and
  correctness; check that a change to attribution or extraction logic moves
  the counts deliberately and visibly. Do not report formatting or lint nits,
  and do not report a stylistic preference as a finding unless it violates a
  constraint stated in this file or in `AGENTS.md`.

## 3. Strict Coding Constraints

**Reproducibility is by pinning and provenance, not by seeds.** Nothing in the
deterministic pipeline draws random numbers; there is no RNG to seed, and a
review finding asking for one is noise. Reproducibility here rests on three
things instead, and a change that weakens any of them is the defect:

* **Pinned inputs.** The sponsor rules file is pinned by sha256 against
  `FIXTURE_RULES_SHA256`, and the geography layer publishes one frozen,
  audited run at a time.
* **Provenance stamps.** Artifacts carry `extracted_at`, `pipeline_commit`
  (or `source_pipeline_commit`), and for sponsor files `rules_sha256`.
  Dropping or faking one lets a derived file outrun its source with nothing
  noticing.
* **Pinned models.** The genuinely non-deterministic step is LLM extraction.
  It is made reproducible by pinning the model id and recording it with the
  output — not by a seed, which the provider does not honour. A change to a
  model id is a change to the data and is reviewed as one.

**Analytical code documents its method inline.** Every transformation states
what it assumes and why, in the code, next to the code. The undocumented
schema facts this pipeline depends on — the baseline `Total` being the
highest-numbered BG group, `number_analyzed` not being a count — are the kind
of thing that must be written down where the next reader will hit it.

**Separation of concerns.** Extraction and analysis are decoupled from
presentation. This repository emits data files; it does not emit markup, and
it does not reach into the site's rendering. The one browser module it ships
is a shared *rule*, not a view.

**Data integrity — no silent failures.** Parsing and metric code handles
missing data, edge cases and varied formatting explicitly. Absence is
recorded as absence with a reason, never coerced to zero; the three sponsor
review states (attributed, reviewed-excluded, unreviewed) are never
collapsed; conflicting rules raise rather than resolve silently; no substring
matching in production attribution paths; every rule carries a non-empty note
saying who decided and why. A pull request that makes a failure quieter
rather than rarer is the defect the mechanism exists to surface.

**Publishing and retention are data-loss surfaces.** A change to what the
scheduled job writes into the site repo, or to `scripts/prune_snapshots.py`,
can delete published data nothing else holds a copy of. Treat those paths as
irreversible and review them accordingly.

**Weekly artifacts are outputs, not source.** They are gitignored here and
committed only to the site repo by CI. The deliberate exception is the LLM
extraction results, which cost money to produce and are the record the
approval queue reviews.

## 4. Pull Request Requirements

* **Type hints on every function you add or change.** Coverage across the
  existing tree is partial, so this is a ratchet, not a sweep: new and
  modified functions are fully annotated, untouched ones are left alone, and
  a reviewer does not flag pre-existing gaps in code the pull request did not
  touch.
* **A change to attribution or extraction logic shows its effect on the
  counts** — the before and after, from a run against the fixture or real
  inputs — in the pull request body.
* **A change to published output carries a migration or compatibility note**
  covering what downstream consumers, including the site repo, will see.
* **A behaviour change carries a test**, or the pull request names the
  specific untested behaviour and the risk of leaving it untested.
* **The checks are run and their output reported**, not asserted.
* **Prefer several small, single-purpose pull requests** over one broad one; a
  reviewer that can hold the whole change in view finds more.
