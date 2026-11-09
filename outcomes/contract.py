"""Producer-side check of an outcomes artifact family (schema version 1).

check() returns every problem found (an empty list passes). The build runs it on the staged
files before anything is written, and scripts/check_outcomes_contract.py runs it on a written
directory. The site applies the matching consumer-side checks in its reader
(industry_outcomes.js outcomesDataProblem) before rendering anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from outcomes.artifacts import ASSESSMENT_KINDS, DISPLAY_MODES, SCHEMA_VERSION, TRIAL_STATES, dump
from outcomes.evidence import RESULTS_STATES

RUN_ID = re.compile(r'^outcomes-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$')
NCT = re.compile(r'^NCT\d{8}$')
# Keys that only private review or experiment records carry; none may appear anywhere here.
PRIVATE_KEYS = {'reviewer', 'rationale_text', 'elapsed_minutes', 'active_minutes', 'packet_sha256', 'raw_response',
                'model_response', 'prompt', 'line_sha256', 'started_at'}


def _walk_keys(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _walk_keys(v)
    return found


def check(files: dict[str, Any], publishable: bool = False) -> list[str]:
    """files: {relative path: parsed object}, including run.json."""
    p: list[str] = []
    run = files.get('run.json')
    if not isinstance(run, dict):
        return ['run.json is missing']
    if run.get('schema_version') != SCHEMA_VERSION:
        p.append(f"run.json schema_version {run.get('schema_version')!r} is not {SCHEMA_VERSION}")
    if not isinstance(run.get('run_id'), str) or not RUN_ID.match(run['run_id']):
        p.append('run.json run_id is malformed')
    mode = run.get('display_mode')
    if mode not in DISPLAY_MODES:
        p.append(f'unknown display_mode {mode!r}')
    if publishable and mode != 'evidence_only':
        p.append('a publishable build must be evidence_only; this is a provisional preview')
    listed = run.get('files') or {}
    present = set(files) - {'run.json'}
    for path in sorted(set(listed) ^ present):
        p.append(f'{path}: listed in run.json but absent' if path in listed else f'{path}: present but not in run.json')
    for path in sorted(set(listed) & present):
        if hashlib.sha256(dump(files[path])).hexdigest() != listed[path]:
            p.append(f'{path}: sha256 does not match run.json')
        if files[path].get('run_id') != run.get('run_id') or files[path].get('schema_version') != SCHEMA_VERSION:
            p.append(f'{path}: from another run or schema')
    leaked = _walk_keys(files) & PRIVATE_KEYS
    if leaked:
        p.append(f'private keys present: {sorted(leaked)}')
    index, summary = files.get('index.json') or {}, files.get('summary.json') or {}
    rows = index.get('rows') or []
    ids = [r.get('nct_id') for r in rows]
    if len(set(ids)) != len(ids) or not all(isinstance(n, str) and NCT.match(n) for n in ids):
        p.append('index.json rows need distinct NCT ids')
    cohort = run.get('cohort') or {}
    if cohort.get('n_eligible') != len(rows) or (run.get('collections') or {}).get('eligible', {}).get('n') != len(rows):
        p.append('eligible count differs between run.json and index.json')
    if cohort.get('n_frame') != cohort.get('n_eligible', 0) + cohort.get('n_excluded', 0) or \
            len(index.get('excluded') or []) != cohort.get('n_excluded'):
        p.append('frame, eligible and excluded counts do not add up')
    for r in rows:
        n = r.get('nct_id')
        rec = ((files.get(r.get('record') or '') or {}).get('records') or {}).get(n)
        if rec is None:
            p.append(f'{n}: no evidence record in {r.get("record")}')
            continue
        st, sub = r.get('results_state'), r.get('results_sub_reason')
        if st not in RESULTS_STATES or sub not in RESULTS_STATES[st]:
            p.append(f'{n}: results state {st}/{sub} is not in the vocabulary')
        if rec['results'] != {'state': st, 'sub_reason': sub, 'as_of': run.get('cutoff')}:
            p.append(f'{n}: index and record disagree on results availability')
        kind = r.get('assessment_kind')
        a = rec.get('assessment')
        if (kind is None) != (a is None) or (a is not None and (a.get('assessment_kind') != kind or a.get('trial_state') != r.get('trial_state'))):
            p.append(f'{n}: index and record disagree on the assessment')
        if kind is not None and kind not in ASSESSMENT_KINDS:
            p.append(f'{n}: unknown assessment kind {kind!r}')
        if kind == 'machine_provisional' and mode != 'provisional_preview':
            p.append(f'{n}: machine assessment in an evidence_only build')
        if a is not None and a.get('trial_state') not in TRIAL_STATES:
            p.append(f'{n}: unknown trial state')
        if (r.get('review_status') == 'human_reviewed') != (kind == 'human_reviewed'):
            p.append(f'{n}: review status and assessment kind disagree')
        if kind is not None and not r.get('in_audit'):
            p.append(f'{n}: an assessment outside the audit collection')
    den = summary.get('denominators') or {}
    if (den.get('eligible') or {}).get('n') != len(rows):
        p.append('summary eligible denominator differs from index.json')
    for m in summary.get('metrics') or []:
        d = den.get(m.get('denominator'))
        if d is None:
            p.append(f"metric {m.get('id')}: unknown denominator")
        elif not (isinstance(m.get('numerator'), int) and 0 <= m['numerator'] <= d['n']):
            p.append(f"metric {m.get('id')}: numerator outside 0..{d['n']}")
        if not m.get('definition'):
            p.append(f"metric {m.get('id')}: no definition")
    by_kind = (summary.get('assessment_states') or {}).get('by_kind') or {}
    for kind, states in by_kind.items():
        if set(states) != set(TRIAL_STATES) or not all(isinstance(v, int) and v >= 0 for v in states.values()):
            p.append(f'assessment_states.{kind} must count all eight trial states, none dropped or merged')
    if mode != 'provisional_preview' and 'machine_provisional' in by_kind:
        p.append('machine assessment counts in an evidence_only build')
    return p


def load_dir(out: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(out.rglob('*.json')):
        files[path.relative_to(out).as_posix()] = json.loads(path.read_text())
    return files
