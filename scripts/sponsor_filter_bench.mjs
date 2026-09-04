// Latency bench for the company filter over the real bridge (run from repo root):
//   node scripts/sponsor_filter_bench.mjs data/sponsors/bridge.csv.gz [nct_id_universe_size]
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { performance } from 'node:perf_hooks';
import { companyTrialSet, entitiesFor, companies } from '../sponsors/company_filter.js';

const path = process.argv[2] || 'data/sponsors/bridge.csv.gz';
const universeSize = Number(process.argv[3] || 80000);

const t0 = performance.now();
const text = gunzipSync(readFileSync(path)).toString('utf8');
// Full CSV parse over the WHOLE text: a quoted field may contain commas and
// newlines alike, so rows cannot be found by splitting on '\n' first.
function parseCsv(src) {
  const rows = []; let row = []; let cur = ''; let q = false;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (q) {
      if (ch === '"') { if (src[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += ch;
    } else if (ch === '"') q = true;
    else if (ch === ',') { row.push(cur); cur = ''; }
    else if (ch === '\n') { row.push(cur); cur = ''; rows.push(row); row = []; }
    else if (ch !== '\r') cur += ch;
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
  return rows.filter(r => r.length > 1 || r[0] !== '');
}
const parsed = parseCsv(text);
const header = parsed[0];
const rows = parsed.slice(1).map(r => Object.fromEntries(r.map((v, i) => [header[i], v])));
const t1 = performance.now();

// Simulated dashboard universe: every loaded record's nct_id.
const universe = Array.from({ length: universeSize }, (_, i) => `NCT${String(i).padStart(8, '0')}`);
for (const r of rows) universe[Math.abs(hash(r.nct_id)) % universeSize] = r.nct_id;
function hash(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0; return h; }

const picks = companies(rows).slice(0, 5);
const timings = [];
for (const c of picks) {
  const a = performance.now();
  const set = companyTrialSet(rows, { company: c });
  const b = performance.now();
  let n = 0; for (const id of universe) if (set.has(id)) n++;
  const d = performance.now();
  timings.push({ company: c, set_size: set.size, build_ms: +(b - a).toFixed(2), intersect_ms: +(d - b).toFixed(2), matched: n });
}
const e0 = performance.now(); entitiesFor(rows, picks[0]); const e1 = performance.now();
console.log(JSON.stringify({
  bridge_rows: rows.length, load_and_parse_ms: +(t1 - t0).toFixed(1),
  universe: universeSize, entities_ms: +(e1 - e0).toFixed(2), filters: timings,
}, null, 2));
