# Sponsor company filter

The matching layer behind the dashboard's "company" filter: which trials a
company touches, in which role, under which ownership view. The dashboard
does not use it yet; the backend produces and tests it every week so the UI
can be wired later with no surprises.

## The three files that matter

| File | Role |
|---|---|
| `company_aliases.csv` | **The rules — treat as schema.** One row per matching rule: `canonical, rule_type, pattern, status, shared, note`. Versioned, PR-gated, and every row needs a `note` saying who decided, when, why (the loader rejects the file otherwise). |
| `sponsor_roles.py` | The matching/filtering module (bundle v3, plus amendment A6). Pure pandas. Do not add substring matching to production paths — that is the one thing this design exists to prevent (Merck & Co ≠ Merck KGaA). |
| `adapter.py` | Turns our stored study records into the AACT-shaped frame the module expects. Cites the AACT source it reproduces (`app/models/sponsor.rb` at the fork commit). |

Also here: `acquisitions.csv` (ownership timeline — dates blank until a
primary source is recorded; the module refuses to use it until then),
`alias_review_queue.csv` (the seed review queue from the bundle),
`audit_states.py` (the three-state partition), `baseline.py` (the CI
regression baseline, keyed by rules sha256), `concordance.py` (parity
harness), `company_filter.js` (browser filter, not loaded by the site yet).

## How a literal sponsor name is treated

Every literal name in a pull is in exactly one of three states:

1. **attributed** — a `status=attribute` rule maps it to a canonical company
2. **reviewed-excluded** — a `status=exclude` rule says a human looked and said "not ours"
3. **unreviewed** — no rule touches it (yet)

States 2 and 3 are never collapsed, and no literal is ever in two states:
one literal claimed by both an attribute rule and an exclude rule is a
**build-time error** (`match_literals()` raises, so the bridge is not
written). Review state is always decided from the rules, never read back off
the bridge — two literals on one trial that collapse to the same
(canonical, entity, role) share one bridge row, and the one that lost the
dedupe is still attributed.

A partnership literal ("Bristol-Meyers
Squibb & Pfizer" — the registry's own misspelling) is attributed to every
named company via rules marked `shared=yes`; anything that aggregates across
companies must dedupe on `nct_id`.

## Curating (Maryam's loop)

1. Open `data/sponsor_audit/inbox_new_unmatched.csv` — unreviewed literals
   that appeared this week, biggest trial counts first.
2. For each you decide on, add a row to `company_aliases.csv`:
   `attribute` (with the canonical company) or `exclude` (with a note saying
   why it is not ours). Leave the rest — they stay in `inbox_open.csv.gz`,
   ageing, until someone decides.
3. Open a PR. CI loads the rules (rejecting empty notes, unknown statuses,
   unknown rule types, an unknown `shared` value) and builds the index
   (rejecting two rules that claim one literal for different companies
   unless every claim is `shared=yes`, and any attribute/exclude overlap).
4. **A rules change re-baselines the fixture, deliberately and visibly.**
   `tests/sponsors/expected_counts.json` holds one baseline block per rules
   sha256: index size, per-company any/lead/collaborator counts, per-entity
   counts, all measured on the 2026-06-19 fixture. Change the rules and the
   first CI run **fails**, writes the new block into that file, and tells you
   to commit it with the rules change — so the diff shows exactly which
   counts your rule moved. Nothing re-baselines silently. Rules the fixture
   cannot exercise are listed under `not_covered_by_fixture` and never fail
   the run.

Start from `data/sponsor_audit/curation_batches/` when you want a batch to
work through: `scripts/sponsor_curation_batch.py` ranks the top-100
unreviewed INDUSTRY lead literals by trial count and writes a
`draft_rule_line` you can paste into `company_aliases.csv` after filling in
the canonical company and the note.

## What the weekly run produces

| Artifact | Where | What |
|---|---|---|
| `bridge.csv.gz` | site repo `data/sponsors/` (+ each full snapshot) | One row per (nct_id, canonical, entity, role) with `literal_name, agency_class, match_rule, shared` |
| `bridge_meta.json` | same | `built_at`, `source_extracted_at`, `pipeline_commit`, `rules_sha256`, `aact_loader_commit`, counts, adapter log |
| `literals_all.csv.gz` | this repo `data/sponsor_audit/` (+ site `data/sponsors/audit/`) | every literal in the pull with its review state, canonicals, trial count |
| `inbox_new_unmatched.csv` | same | the curation inbox: unreviewed literals that appeared this week |
| `inbox_open.csv.gz` | same | every open unreviewed literal with `first_seen` and `weeks_open` — the backlog, so an old item cannot hide |
| `match_changes.csv` | same | literals whose attribution changed vs last week, with a `cause` (warns in the run summary) |
| `label_changes.csv` | same | the trial-level change: every (nct_id, canonical, role) added or removed, with a `cause` |
| `trial_sponsors.csv.gz` | same | this week's per-trial literals — next week's "was it there before?" |
| `audit_summary.json` | same | the counts the run summary prints |

Every audit CSV row carries `rules_sha256`; `audit_summary.json` also carries
`pipeline_commit`.

**Every label change is attributable.** `cause` on both change files is one of

| cause | meaning |
|---|---|
| `registry_edit` | the literal appeared in, or vanished from, the pull; the rules did not change |
| `rules_change` | the literal was in both pulls and `company_aliases.csv` changed |
| `both` | its presence changed *and* the rules changed — either could explain it |
| `unknown` | present both weeks, rules unchanged: nothing in the inputs explains it, so a code change did. Investigate |
| `unknown_no_prior` | last week's artifacts were unavailable (first run) |

An empty literal (a record with no lead sponsor name) is counted in
`literals_all.csv.gz` and in `n_blank_literal_trials`, but never occupies an
inbox line: no rule can name it.

## Filter semantics (mirrored in `company_filter.js`)

- `role`: `any` (default) | `lead` | `collaborator`
- `view`: `current_owner` (default; Wyeth trials count as Pfizer) |
  `as_registered` (Wyeth stays Wyeth — pick an entity from `entitiesFor()`)
- Result: a Set of `nct_id`s, so both-roles and partnership literals dedupe
  structurally. Compose with other dashboard filters as one predicate.
- Sponsor-class keeps its current meaning (the lead's class).

## Provenance

- Adapter reproduces `app/models/sponsor.rb:3-21` of
  `michaeldgreenphd/aact @ b8c16d33` (an unmodified mirror of upstream
  `ctti-clinicaltrials/aact` `dev`): lead → one row, each collaborator → one
  row, `class` verbatim. Our records store exactly those fields
  (`src/utils.py:467-479`).
- Parity check: `python scripts/sponsor_concordance.py` compares our
  adapter's index with the AACT fixture per trial (agree / fixture-only /
  pull-only, with cause hints), scoped by the trials each *source* holds so a
  trial the adapter lost entirely shows up instead of disappearing.
  `tests/sponsors/test_adapter.py` pins the same comparison for 300 sampled
  trials, so CI fails if the adapter ever stops reproducing `sponsor.rb`.

## Failure semantics

A rules problem fails loudly and changes nothing. `build_sponsor_bridge.py`
raises before it writes, and the weekly job runs it **after** the demographics
publish, so a bad rules change turns the run red while the site keeps last
week's bridge and this week's demographics.
`tests/sponsors/test_failure_semantics.py` asserts exactly that: conflicting
rules, non-zero exit, no bridge written, the published bridge untouched.
