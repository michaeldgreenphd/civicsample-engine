import test from 'node:test';
import assert from 'node:assert/strict';
import { companyTrialSet, entitiesFor, companies, validate } from '../../sponsors/company_filter.js';

const row = (nct_id, canonical, entity, role, literal_name, shared = 'no') =>
  ({ nct_id, canonical, entity, role, literal_name, agency_class: 'INDUSTRY', match_rule: 'exact_normalized', shared });

const rows = [
  row('NCT1', 'Pfizer', 'pfizer', 'lead', 'Pfizer'),
  row('NCT1', 'Pfizer', 'wyeth', 'collaborator', 'Wyeth is now a wholly owned subsidiary of Pfizer'), // both roles, two literals
  row('NCT2', 'Pfizer', 'wyeth', 'lead', 'Wyeth'),
  row('NCT3', 'Pfizer', 'bristol-meyers squibb and pfizer', 'lead', 'Bristol-Meyers Squibb & Pfizer', 'yes'),
  row('NCT3', 'Bristol-Myers Squibb', 'bristol-meyers squibb and pfizer', 'lead', 'Bristol-Meyers Squibb & Pfizer', 'yes'),
  row('NCT4', 'Merck KGaA', 'merck kgaa', 'collaborator', 'Merck KGaA'),
];

test('any-involvement dedupes a company holding both roles on one trial', () => {
  const s = companyTrialSet(rows, { company: 'Pfizer' });
  assert.deepEqual([...s].sort(), ['NCT1', 'NCT2', 'NCT3']);
});

test('role scopes', () => {
  assert.deepEqual([...companyTrialSet(rows, { company: 'Pfizer', role: 'lead' })].sort(), ['NCT1', 'NCT2', 'NCT3']);
  assert.deepEqual([...companyTrialSet(rows, { company: 'Pfizer', role: 'collaborator' })], ['NCT1']);
});

test('as_registered view keys on entity: Wyeth stays Wyeth', () => {
  assert.deepEqual([...companyTrialSet(rows, { company: 'wyeth', view: 'as_registered' })].sort(), ['NCT1', 'NCT2']);
  assert.equal(companyTrialSet(rows, { company: 'Pfizer', view: 'as_registered' }).size, 0);
});

test('partnership literal lands in both companies; union dedupes', () => {
  const p = companyTrialSet(rows, { company: 'Pfizer' });
  const b = companyTrialSet(rows, { company: 'Bristol-Myers Squibb' });
  assert.ok(p.has('NCT3') && b.has('NCT3'));
  assert.equal(new Set([...p, ...b]).size, 3); // not 4
});

test('entitiesFor matches sponsor_roles.entities() shape and counts', () => {
  assert.deepEqual(entitiesFor(rows, 'Pfizer'), [
    { entity: 'wyeth', n_trials: 2, n_lead: 1, n_collab: 1 },
    { entity: 'bristol-meyers squibb and pfizer', n_trials: 1, n_lead: 1, n_collab: 0 },
    { entity: 'pfizer', n_trials: 1, n_lead: 1, n_collab: 0 },
  ]);
});

test('companies() lists canonicals sorted', () => {
  assert.deepEqual(companies(rows), ['Bristol-Myers Squibb', 'Merck KGaA', 'Pfizer']);
});

test('invalid role or view throws', () => {
  assert.throws(() => validate('sponsor', 'current_owner'), /role must be one of/);
  assert.throws(() => companyTrialSet(rows, { company: 'Pfizer', view: 'owner' }), /view must be one of/);
});

test('entitiesFor order and counts match sponsor_roles.entities() on the shared parity fixture', async () => {
  const { readFileSync } = await import('node:fs');
  const url = new URL('./fixtures/entities_parity.json', import.meta.url);
  const fx = JSON.parse(readFileSync(url, 'utf8'));
  const parityRows = fx.rows.map(r => Object.fromEntries(fx.columns.map((c, i) => [c, r[i]])));
  assert.deepEqual(entitiesFor(parityRows, fx.canonical), fx.expected);
});
