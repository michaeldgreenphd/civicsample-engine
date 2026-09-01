// company_filter.js — browser-side company filter over the sponsor bridge.
//
// NOT loaded by the site yet. Pure functions over bridge rows (objects with
// the bridge.csv.gz columns: nct_id, canonical, entity, role, literal_name,
// agency_class, match_rule, shared). Parse the CSV elsewhere; this module
// only filters.
//
// Semantics mirror sponsors/sponsor_roles.py trials() and entities():
//   role  any | lead | collaborator            (default any)
//   view  current_owner | as_registered        (default current_owner)
// The result is a Set of nct_ids, so both-roles and partnership literals
// dedupe structurally. Anything that aggregates ACROSS companies must union
// Sets, never add counts (README invariant 5).
//
// Composition with the dashboard's other filters is one predicate:
//   set.has(record.nct_id). Sponsor-class keeps its meaning (the lead's
//   class), so class=INDUSTRY + company=X + role=collaborator reads
//   "industry-led trials where X collaborates".
//
// For view=as_registered, `company` must be an entity value as listed by
// entitiesFor() (the bridge's entity column is already normalized).

export const ROLES = new Set(['any', 'lead', 'collaborator']);
export const VIEWS = new Set(['current_owner', 'as_registered']);

export function validate(role, view) {
  if (!ROLES.has(role)) throw new Error(`role must be one of ${[...ROLES].join(', ')}; got ${role}`);
  if (!VIEWS.has(view)) throw new Error(`view must be one of ${[...VIEWS].join(', ')}; got ${view}`);
}

export function companyTrialSet(rows, { company, role = 'any', view = 'current_owner' }) {
  validate(role, view);
  const key = view === 'current_owner' ? 'canonical' : 'entity';
  const out = new Set();
  for (const r of rows) {
    if (r[key] !== company) continue;
    if (role !== 'any' && r.role !== role) continue;
    out.add(r.nct_id);
  }
  return out;
}

// Constituent as-registered entities of a canonical company, with trial
// counts by role — the same output as sponsor_roles.entities() (A3).
export function entitiesFor(rows, canonical) {
  const acc = new Map();
  for (const r of rows) {
    if (r.canonical !== canonical) continue;
    let e = acc.get(r.entity);
    if (!e) { e = { any: new Set(), lead: new Set(), collaborator: new Set() }; acc.set(r.entity, e); }
    e.any.add(r.nct_id);
    if (r.role === 'lead') e.lead.add(r.nct_id);
    if (r.role === 'collaborator') e.collaborator.add(r.nct_id);
  }
  return [...acc.entries()]
    .map(([entity, e]) => ({ entity, n_trials: e.any.size, n_lead: e.lead.size, n_collab: e.collaborator.size }))
    .sort((a, b) => b.n_trials - a.n_trials || a.entity.localeCompare(b.entity));
}

// Sorted list of canonical companies present in the bridge, for a picker.
export function companies(rows) {
  return [...new Set(rows.map(r => r.canonical))].sort((a, b) => a.localeCompare(b));
}
