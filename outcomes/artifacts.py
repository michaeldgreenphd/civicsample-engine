"""The outcomes artifact family (schema version 1), written to data/outcomes/ of a site checkout
for local preview, or to any directory.

  run.json             manifest: run id, cutoff, cohort and how it differs from the frame,
                       source hashes, producer revision, method version, collections, sponsor
                       attribution, demographics link, display mode, and the sha256 of every
                       other file
  summary.json         evidence-availability counts, each with its numerator, a named
                       denominator and a definition; the browser computes no aggregate
  index.json           one row per eligible trial for the searchable list, plus the excluded
                       frame trials with their reasons
  records/rNN.json     evidence records on demand, sharded by NCT number (NN = id % SHARDS)

Display boundary. The private export's mode decides what assessments exist here:
evidence_only carries human-reviewed assessments only, provisional_preview adds machine
assessments labelled machine_provisional. A provisional build is for a local preview and must
not be committed or published; check_outcomes_contract.py --publishable refuses one.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import evidence as E  # noqa: E402
from outcomes.attribution import display_name  # noqa: E402
from src import condition_classifier  # noqa: E402

SCHEMA_VERSION = 1
SHARDS = 8
EXPORT_FORMAT = 'civicsample-outcomes-assessment-export/1'
DISPLAY_MODES = ('evidence_only', 'provisional_preview')
TRIAL_STATES = ('MET', 'NOT_MET', 'INSUFFICIENT_EVIDENCE', 'NOT_APPLICABLE', 'PENDING', 'UNREPORTED',
                'CONFLICTING', 'ROLL_UP_NOT_SUPPORTED')
ASSESSMENT_KINDS = ('human_reviewed', 'machine_provisional')
# Keys an imported assessment may carry (the private export's allowlist); any other key refuses
# the build, so a widened export cannot pass private material through this repository.
ASSESSMENT_KEYS = {'nct_id', 'assessment_kind', 'trial_state', 'trial_sub_reason', 'trial_rationale', 'multiplicity',
                   'units', 'prespecification', 'review', 'human_evidence', 'method_version'}
UNIT_KEYS = {'au_id', 'measure_title', 'time_frame', 'part', 'analysis_role', 'hypothesis_type', 'group_titles',
             'reported', 'decision_rule', 'state', 'sub_reason', 'rationale', 'evidence', 'amended_by_review'}
REVIEW_KEYS = {'status', 'reviewed_on', 'decision', 'machine_trial_state', 'machine_trial_sub_reason'}
# Nested objects are allowlisted too: a private field placed inside any of them must not pass.
NESTED_KEYS = {
    'prespecification': {'registration_timing', 'level'},
    'reported': {'paramType', 'paramValue', 'ciPctValue', 'ciNumSides', 'ciLowerLimit', 'ciUpperLimit', 'pValue',
                 'statisticalMethod'},
    'decision_rule': {'alpha', 'alpha_sides', 'margin', 'margin_side', 'source'},
    'evidence': {'json_path', 'excerpt'},
    'human_evidence': {'json_path', 'excerpt', 'document', 'page', 'quotation'},
}
SCALAR = (str, int, float, bool, type(None))


def _extra_keys(a: dict[str, Any]) -> set[str]:
    """Every key in one exported assessment that is outside the allowlist, at any depth, and any
    value that is not the scalar or list-of-scalars the allowlisted field holds."""
    def obj(o: Any, allowed: set[str], where: str) -> set[str]:
        if not isinstance(o, dict):
            return {f'{where} is not an object'}
        bad = {f'{where}.{k}' for k in set(o) - allowed}
        for k, v in o.items():
            if k in allowed and not (isinstance(v, SCALAR) or (isinstance(v, list) and all(isinstance(x, SCALAR) for x in v))):
                bad.add(f'{where}.{k} is not a scalar')
        return bad
    extra = {k for k in set(a) - ASSESSMENT_KEYS}
    extra |= obj(a.get('review') or {}, REVIEW_KEYS, 'review')
    extra |= obj(a.get('prespecification') or {}, NESTED_KEYS['prespecification'], 'prespecification')
    for i, e in enumerate(a.get('human_evidence') or []):
        extra |= obj(e, NESTED_KEYS['human_evidence'], f'human_evidence[{i}]')
    for u in a.get('units') or []:
        extra |= {f"unit.{k}" for k in set(u) - UNIT_KEYS}
        for k in ('reported', 'decision_rule'):
            if u.get(k) is not None:
                extra |= obj(u[k], NESTED_KEYS[k], f"{u.get('au_id')}.{k}")
        for i, e in enumerate(u.get('evidence') or []):
            extra |= obj(e, NESTED_KEYS['evidence'], f"{u.get('au_id')}.evidence[{i}]")
        for k in UNIT_KEYS - {'reported', 'decision_rule', 'evidence', 'group_titles'}:
            if k in u and not isinstance(u[k], SCALAR):
                extra.add(f"{u.get('au_id')}.{k} is not a scalar")
    return extra


class BuildError(Exception):
    pass


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode()


def shard_name(nct_id: str) -> str:
    return f'records/r{int(nct_id[3:]) % SHARDS:02d}.json'


def read_export(raw: bytes) -> dict[str, Any]:
    """The private assessment export, checked against the allowlist before anything uses it."""
    doc = json.loads(raw)
    if doc.get('format') != EXPORT_FORMAT:
        raise BuildError('the assessments file is not a civicsample outcomes assessment export')
    if doc.get('display_mode') not in DISPLAY_MODES:
        raise BuildError('unknown display mode in the assessments export')
    ids = [a.get('nct_id') for a in doc.get('assessments') or []]
    dupes = sorted({n for n in ids if ids.count(n) > 1})
    if dupes:
        raise BuildError(f'the export carries more than one assessment for {dupes}')
    for a in doc.get('assessments') or []:
        extra = _extra_keys(a)
        if extra:
            raise BuildError(f"{a.get('nct_id')}: non-allowlisted keys {sorted(extra)} in the export")
        if a.get('assessment_kind') not in ASSESSMENT_KINDS or a.get('trial_state') not in TRIAL_STATES:
            raise BuildError(f"{a.get('nct_id')}: unknown assessment kind or trial state")
        if doc['display_mode'] == 'evidence_only' and a['assessment_kind'] != 'human_reviewed':
            raise BuildError(f"{a['nct_id']}: an evidence_only export carries a machine assessment")
        if (a['assessment_kind'] == 'human_reviewed') != ((a.get('review') or {}).get('status') == 'human_reviewed'):
            raise BuildError(f"{a['nct_id']}: assessment kind and review status disagree")
    return doc


def build(studies: list[dict[str, Any]], acquisition: dict[str, Any], cutoff: date, export: dict[str, Any] | None,
          export_sha: str | None, attribution: tuple[dict[str, Any], dict[str, Any]],
          demographics: tuple[dict[str, Any], dict[str, Any]], producer: dict[str, Any],
          generated_at: str | None = None) -> dict[str, Any]:
    """{relative path: object} for the whole family. Pure apart from the classifier lookup and,
    unless generated_at is given (fixtures), the clock."""
    attr, attr_summary = attribution
    links, link_summary = demographics
    retrieved, _ = E.parse_date(str(acquisition.get('retrieved_at') or '')[:10])
    if retrieved is not None and cutoff > retrieved:
        raise BuildError(f'cutoff {cutoff} is after the records were retrieved ({retrieved}); '
                         'the registry evidence cannot speak for a later date')
    all_facts = [E.facts(s) for s in studies]
    excluded, eligible = [], []
    for f in all_facts:
        reasons = E.eligibility(f)
        (excluded.append({'nct_id': f['nct_id'], 'reasons': reasons}) if reasons else eligible.append(f))
    eligible.sort(key=lambda f: f['nct_id'])
    elig_ids = {f['nct_id'] for f in eligible}

    assessments: dict[str, dict[str, Any]] = {}
    audit_ids: set[str] = set()
    mode = 'evidence_only'
    if export is not None:
        mode = export['display_mode']
        audit = export['collections']['audit']
        audit_ids = set(audit['nct_ids'])
        # The export's eligibility and this build's must agree for every audit trial.
        export_eligible = set(audit['eligible_nct_ids'])
        frame_ids = {f['nct_id'] for f in all_facts}
        drift = sorted(n for n in audit_ids if n in frame_ids and ((n in export_eligible) != (n in elig_ids)))
        missing = sorted(audit_ids - frame_ids)
        if drift or missing:
            raise BuildError(f'eligibility disagrees with the assessments export for {drift}; '
                             f'audit trials missing from the frame: {missing}')
        assessments = {a['nct_id']: a for a in export['assessments'] if a['nct_id'] in elig_ids}
    reviewed = {n for n, a in assessments.items() if a['assessment_kind'] == 'human_reviewed'}

    rows, records = [], {}
    for f in eligible:
        n = f['nct_id']
        state, sub = E.results_state(f, cutoff)
        docs = E.document_flags(f)
        cls = condition_classifier.classify_conditions(f['conditions'])
        a = attr[n]
        assessment = assessments.get(n)
        prim = f['registered_primary_outcomes'][0]['measure'] if f['registered_primary_outcomes'] else None
        rows.append({
            'nct_id': n, 'title': f['brief_title'], 'sponsor': display_name(a), 'sponsor_state': a['review_state'],
            'recode_differs': a['differs_from_industry_recode'],
            'area': cls['primary_condition'], 'area_secondary': cls['secondary_condition'],
            'primary_objective': (prim[:200] + '…') if prim and len(prim) > 200 else prim,
            'n_primary_outcomes': len(f['registered_primary_outcomes']),
            'results_state': state, 'results_sub_reason': sub, 'results_first_post_date': f['results_first_post_date'],
            'protocol': docs['protocol'], 'sap': docs['sap'], 'in_audit': n in audit_ids,
            'assessment_kind': assessment['assessment_kind'] if assessment else None,
            'trial_state': assessment['trial_state'] if assessment else None,
            'review_status': ('human_reviewed' if n in reviewed else 'awaiting_human_review' if n in audit_ids else None),
            'in_demographics': links[n]['in_dataset'] if links.get(n) else None,
            'record': shard_name(n)})
        records.setdefault(shard_name(n), {})[n] = {
            'nct_id': n, 'registry_url': f'https://clinicaltrials.gov/study/{n}',
            'facts': {k: f[k] for k in ('brief_title', 'lead_sponsor_name', 'lead_sponsor_class', 'conditions', 'phases',
                                        'allocation', 'enrollment', 'enrollment_type', 'overall_status', 'why_stopped',
                                        'start_date', 'primary_completion_date', 'primary_completion_type',
                                        'study_first_submit_date', 'results_first_post_date',
                                        'certification_first_submit_date', 'last_update_post_date',
                                        'registered_primary_outcomes', 'documents')},
            'sponsor': {'display': display_name(a), **{k: a[k] for k in ('lead_literal', 'review_state', 'companies',
                                                                          'industry_recode_company',
                                                                          'differs_from_industry_recode')}},
            'area': {'primary': cls['primary_condition'], 'secondary': cls['secondary_condition']},
            'results': {'state': state, 'sub_reason': sub, 'as_of': cutoff.isoformat()},
            'collections': {'audit': n in audit_ids, 'human_reviewed': n in reviewed},
            'demographics_link': links.get(n),
            'assessment': ({**assessment, 'assessment_cutoff': export['cutoff']} if assessment else None)}

    summary = summarize(rows, eligible, excluded, audit_ids & elig_ids, reviewed, mode, assessments)
    run_id = 'outcomes-{}-{}'.format(cutoff.isoformat(), sha256(dump([acquisition.get('sha256_uncompressed'), export_sha,
                                                                         producer.get('commit')]))[:10])
    files: dict[str, Any] = {'summary.json': summary,
                             'index.json': {'columns_note': 'One row per eligible trial; see run.json for definitions.',
                                            'rows': rows, 'excluded': sorted(excluded, key=lambda x: x['nct_id'])}}
    for path, recs in sorted(records.items()):
        files[path] = {'records': recs}
    for obj in files.values():
        obj['run_id'] = run_id
        obj['schema_version'] = SCHEMA_VERSION
    exclusion_counts = {r: sum(1 for x in excluded if r in x['reasons']) for r in E.EXCLUSION_REASONS}
    run = {
        'schema_version': SCHEMA_VERSION, 'run_id': run_id,
        'generated_at': generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'cutoff': cutoff.isoformat(),
        'producer': producer,
        'manual_run': True,
        'display_mode': mode,
        'acquisition': acquisition,
        'cohort': {
            'definition': ('Interventional, Phase 3 (Phase 2/3 excluded as its own stratum), randomized trials with an '
                           'INDUSTRY lead sponsor class and primary completion inside 2020 (a month- or year-precision '
                           'date must lie wholly inside).'),
            'window': list(E.WINDOW), 'frame_query': E.FRAME_QUERY,
            'n_frame': len(all_facts), 'n_eligible': len(eligible), 'n_excluded': len(excluded),
            'exclusions': exclusion_counts,
            'differs_from_frame': ('The frame query matches Phase 2/3 records through its PHASE3 term; those are '
                                   'excluded here, and every other rule is re-checked on the record as retrieved.'),
        },
        'method': ({'version': export['method_version'], 'rules_sha256': export['rules_sha256'],
                    'methods_spec_sha256': export['methods_spec_sha256'], 'assessment_cutoff': export['cutoff'],
                    'export_sha256': export_sha, 'export_producer_commit': export['producer']['commit']}
                   if export else None),
        'collections': {
            'eligible': {'n': len(eligible), 'definition': 'The beta cohort above.'},
            'audit': {'n': len(audit_ids & elig_ids), 'n_including_excluded': len(audit_ids),
                      'definition': (export['collections']['audit']['definition'] if export else None)},
            'human_reviewed': {'n': len(reviewed), 'definition': 'Trials whose assessment a named person has reviewed.'},
            'demographics_linked': {'n': link_summary.get('n_in_dataset'),
                                    'definition': "Eligible trials that appear in the site's participant-demographics dataset."},
        },
        'sponsor_attribution': attr_summary,
        'demographics_link': link_summary,
        'assessments': {'display_mode': mode, 'n_human_reviewed': len(reviewed),
                        'n_machine_provisional': sum(1 for a in assessments.values() if a['assessment_kind'] == 'machine_provisional')},
        'dates': {'outcomes_cutoff': cutoff.isoformat(),
                  'assessment_cutoff': export['cutoff'] if export else None,
                  'demographics_snapshot': link_summary.get('site_snapshot_date')},
    }
    run['files'] = {p: sha256(dump(o)) for p, o in sorted(files.items())}
    return {'run.json': run, **files}


def metric(mid: str, label: str, num: int, den: str, definition: str) -> dict[str, Any]:
    return {'id': mid, 'label': label, 'numerator': num, 'denominator': den, 'definition': definition}


def summarize(rows: list[dict[str, Any]], eligible: list[dict[str, Any]], excluded: list[dict[str, Any]],
              audit: set[str], reviewed: set[str], mode: str, assessments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def count(pred: Any) -> int:
        return sum(1 for r in rows if pred(r))
    denominators = {
        'eligible': {'n': len(rows), 'definition': 'Eligible trials in the beta cohort.'},
        'frame': {'n': len(rows) + len(excluded), 'definition': 'Records matched by the frame query.'},
        'audit_eligible': {'n': len(audit), 'definition': 'Eligible trials in the development (audit) collection.'},
    }
    metrics = [
        metric('results_posted', 'Results posted', count(lambda r: r['results_state'] == 'POSTED'), 'eligible',
               'A results section is posted on ClinicalTrials.gov as of the cutoff.'),
        metric('no_results_past_window', 'No posted results, past the standard one-year window',
               count(lambda r: r['results_state'] == 'UNREPORTED'), 'eligible',
               'No results posted and more than one year since actual primary completion (and any certified delay). '
               'Not a finding of non-compliance: applicable-trial status and extensions are not established.'),
        metric('no_results_within_window', 'No posted results, within the standard window',
               count(lambda r: r['results_sub_reason'] in ('WITHIN_STANDARD_WINDOW', 'CERTIFIED_DELAY')), 'eligible',
               'No results posted; less than one year since actual primary completion, or within two years of a '
               'certification of delay.'),
        metric('no_results_status_uncertain', 'No posted results, completion not confirmed',
               count(lambda r: r['results_sub_reason'] == 'STATUS_UNCERTAIN'), 'eligible',
               'No results posted; the registry gives an estimated primary completion date or an unknown status.'),
        metric('no_results_not_applicable', 'No posted results, no participants',
               count(lambda r: r['results_state'] == 'NOT_APPLICABLE'), 'eligible',
               'Withdrawn, or terminated with an actual enrolment of zero.'),
        metric('protocol_posted', 'Protocol posted', count(lambda r: r['protocol']), 'eligible',
               'A protocol document is posted on the registry. Availability only: not whether it states a usable rule.'),
        metric('sap_posted', 'Statistical analysis plan posted', count(lambda r: r['sap']), 'eligible',
               'A SAP document is posted on the registry. Availability only: not whether it states a usable rule.'),
        metric('protocol_or_sap_posted', 'Protocol or SAP posted', count(lambda r: r['protocol'] or r['sap']), 'eligible',
               'At least one of the two is posted.'),
        metric('human_reviewed', 'Assessments reviewed by a person', len(reviewed & audit), 'audit_eligible',
               'Audit-collection trials whose assessment a named person has reviewed.'),
    ]
    # Only when the link was computed: an uncomputed link is absent, not a measured zero.
    if rows and all(r['in_demographics'] is not None for r in rows):
        metrics.append(metric('demographics_linked', 'In the participant-demographics dataset',
                              count(lambda r: r['in_demographics']), 'eligible',
                              "The trial appears in the site's participant-demographics dataset."))
    by_kind = {}
    for kind in ASSESSMENT_KINDS:
        if kind == 'machine_provisional' and mode != 'provisional_preview':
            continue
        by_kind[kind] = {s: sum(1 for a in assessments.values() if a['assessment_kind'] == kind and a['trial_state'] == s)
                         for s in TRIAL_STATES}
    return {'denominators': denominators, 'metrics': metrics,
            'assessment_states': {'denominator': 'audit_eligible', 'by_kind': by_kind,
                                  'note': ('Counts over the development collection only. Not a success rate for any '
                                           'sponsor, drug or the cohort.')}}


def write(files: dict[str, Any], out: Path) -> None:
    """Write the whole family into a staging sibling, then swap it in with renames, so a failed
    or interrupted write leaves the previous family intact and never a mix of two runs."""
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.with_name(out.name + '.staging')
    old = out.with_name(out.name + '.previous')
    for d in (stage, old):
        if d.exists():
            shutil.rmtree(d)
    (stage / 'records').mkdir(parents=True)
    for path, obj in files.items():
        if path != 'run.json':
            (stage / path).write_bytes(dump(obj))
    (stage / 'run.json').write_text(json.dumps(files['run.json'], indent=1, ensure_ascii=False) + '\n')
    if out.exists():
        out.rename(old)
    stage.rename(out)
    if old.exists():
        shutil.rmtree(old)
