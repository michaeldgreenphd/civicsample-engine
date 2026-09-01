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
`audit_states.py` (the three-state partition), `concordance.py` (parity
harness), `company_filter.js` (browser filter, not loaded by the site yet).

## How a literal sponsor name is treated

Every literal name in a pull is in exactly one of three states:

1. **attributed** — a `status=attribute` rule maps it to a canonical company
2. **reviewed-excluded** — a `status=exclude` rule says a human looked and said "not ours"
3. **unreviewed** — no rule touches it (yet)

States 2 and 3 are never collapsed. A partnership literal ("Bristol-Meyers
Squibb & Pfizer" — the registry's own misspelling) is attributed to every
named company via rules marked `shared=yes`; anything that aggregates across
companies must dedupe on `nct_id`.

## Curating (Maryam's loop)

1. Open `data/sponsor_audit/inbox_new_unmatched.csv` — unreviewed literals
   that appeared this week, biggest trial counts first.
2. For each you decide on, add a row to `company_aliases.csv`:
   `attribute` (with the canonical company) or `exclude` (with a note saying
   why it is not ours). Leave the rest — they stay in `unmatched_all.csv`.
3. Open a PR. CI loads the rules (rejecting empty notes, unknown statuses,
   unknown rule types) and builds the index (rejecting two rules that claim
   one literal for different companies unless every claim is `shared=yes`).
4. **If you touch the rules, re-baseline the fixture deliberately**: the
   2026-06-19 regression fixture is valid only for the rules at sha256
   `3e13bc59…`; `tests/sponsors/test_rules_loading.py` fails on a mismatch
   and tells you what to update.

## What the weekly run produces

| Artifact | Where | What |
|---|---|---|
| `bridge.csv.gz` | site repo `data/sponsors/` (+ each full snapshot) | One row per (nct_id, canonical, entity, role) with `literal_name, agency_class, match_rule, shared` |
| `bridge_meta.json` | same | `built_at`, `source_extracted_at`, `pipeline_commit`, `rules_sha256`, `aact_loader_commit`, counts, adapter log |
| `inbox_new_unmatched.csv` | this repo `data/sponsor_audit/` | the curation inbox |
| `match_changes.csv` | same | literals whose attribution changed vs last week (warns in the run summary) |
| `unmatched_all.csv` | same | the whole unreviewed state |

Every audit CSV row carries `rules_sha256`.

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
  pull-only, with cause hints).
