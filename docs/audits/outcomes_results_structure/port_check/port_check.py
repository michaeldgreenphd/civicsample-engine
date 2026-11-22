#!/usr/bin/env python3
"""Port-fidelity check: outcomes/results_structure.py against the audit's own output.

Run from the repository root:

    python docs/audits/outcomes_results_structure/port_check/port_check.py

Reads the audit's cached registry responses (first_pass/cache/raw_responses.jsonl.gz) and the
audit's per-trial tables (second_pass/per_trial_usable.jsonl.gz, per_trial_denominators.jsonl.gz,
subgroup_detected.jsonl), runs the production module over the same 2,000 records, and compares
trial by trial. Writes comparison.json beside this script and prints the headline table.

This checks that the production code computes what the audit scripts computed on the same bytes.
It does not check that the rules measure what a study needs; that is a scientific question this
comparison cannot answer. No network call is made.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

from outcomes import results_structure as RS  # noqa: E402

CACHE = AUDIT / 'first_pass' / 'cache' / 'raw_responses.jsonl.gz'
# The audit's headline figures (second_pass/definitions.json), restated here so a change to either
# side shows as a difference rather than passing unnoticed.
AUDIT_HEADLINE = {'B1': 1991, 'F1': 1995, 'O1': 1905, 'A1': 1932, 'all_four_usable': 1852, 'C1': 1394, 'C2': 1134,
                  'between_arm_analysis_among_C2': 473, 'between_arm_estimate_with_both_limits_among_C2': 310,
                  'SG2_trials_with_a_signal': 136}


def cached_studies() -> Iterator[dict[str, Any]]:
    """Studies from the cache, one HTTP response per line with an unparsed `body` holding `studies[]`."""
    with gzip.open(CACHE, 'rt', encoding='utf-8') as fh:
        for line in fh:
            yield from json.loads(json.loads(line)['body']).get('studies', [])


def jsonl(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    ids = [l.strip() for l in (AUDIT / 'first_pass' / 'sample_ids.txt').read_text().splitlines() if l.strip()]
    usable = {r['nct_id']: r for r in jsonl(AUDIT / 'second_pass' / 'per_trial_usable.jsonl.gz')}
    denoms = {r['nct_id']: r for r in jsonl(AUDIT / 'second_pass' / 'per_trial_denominators.jsonl.gz')}
    detected = {r['nct_id']: r['signals'] for r in jsonl(AUDIT / 'second_pass' / 'subgroup_detected.jsonl')}

    ours: dict[str, dict[str, Any]] = {}
    raw_signals: dict[str, list[tuple[int, str, str, str]]] = {}
    for s in cached_studies():
        rec = RS.study_record(s)
        RS.apply_reviews(rec)
        ours[rec['nct_id']] = rec
        ms = ((s.get('resultsSection') or {}).get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []
        raw_signals[rec['nct_id']] = [(i, g['field'], g['dimension'], g['text'])
                                      for i, m in enumerate(ms) for g in RS.detect_measure(m)]

    def flag(rec: dict[str, Any], name: str) -> Any:
        m, c, b = rec['modules'], rec['comparative'], rec['between_arm_analysis']
        return {'B1': m['baseline']['usable'], 'F1': m['participant_flow']['usable'],
                'O1': m['outcome_measures']['usable'], 'A1': m['adverse_events']['usable'],
                'four_usable': rec['all_four_usable'], 'C1': c['C1'], 'C2': c['C2'],
                'S1': b['any_usable_analysis'], 'S1_between': b['posted'], 'S1_between_p': b['with_p_value'],
                'S1_between_est_ci': b['with_estimate_and_both_limits'],
                'B1_reason': m['baseline']['reason'], 'F1_reason': m['participant_flow']['reason'],
                'O1_reason': m['outcome_measures']['reason'], 'A1_reason': m['adverse_events']['reason'],
                'present_baselineCharacteristicsModule': m['baseline']['present'],
                'present_participantFlowModule': m['participant_flow']['present'],
                'present_outcomeMeasuresModule': m['outcome_measures']['present'],
                'present_adverseEventsModule': m['adverse_events']['present']}[name]

    flags = ['B1', 'F1', 'O1', 'A1', 'four_usable', 'C1', 'C2', 'S1', 'S1_between', 'S1_between_p', 'S1_between_est_ci',
             'B1_reason', 'F1_reason', 'O1_reason', 'A1_reason', 'present_baselineCharacteristicsModule',
             'present_participantFlowModule', 'present_outcomeMeasuresModule', 'present_adverseEventsModule']
    per_flag = {}
    for name in flags:
        diff = sorted(n for n in ids if n in ours and flag(ours[n], name) != usable[n][name])
        per_flag[name] = {'trials_compared': sum(1 for n in ids if n in ours), 'trials_differing': len(diff),
                          'examples': diff[:10]}

    def den(rec: dict[str, Any]) -> dict[str, Any]:
        bp = rec['baseline_population']
        method = bp['total']['method']
        if method and not bp['total'].get('units_are_participants', True):
            method += '|bt_units_not_participants'
        return {'BT': bp['total']['value'], 'BT_method': method or bp['total']['reason'],
                'FS': bp['started']['value'], 'FS_method': bp['started']['reason'],
                'EN': bp['enrollment']['value'], 'EN_type': bp['enrollment']['type'],
                'BT_arms_sum': bp['total'].get('arms_sum'),
                'BT_vs_FS': bp['relations']['total_vs_started'], 'BT_vs_EN': bp['relations']['total_vs_enrollment'],
                'FS_vs_EN': bp['relations']['started_vs_enrollment']}
    per_den = {}
    for name in ('BT', 'BT_method', 'FS', 'FS_method', 'EN', 'EN_type', 'BT_arms_sum', 'BT_vs_FS', 'BT_vs_EN', 'FS_vs_EN'):
        diff = sorted(n for n in ids if n in ours and den(ours[n])[name] != denoms[n][name])
        per_den[name] = {'trials_compared': sum(1 for n in ids if n in ours), 'trials_differing': len(diff),
                         'examples': diff[:10]}

    ours_detected = {n for n in ids if n in ours and ours[n]['subgroup']['signals']}
    sig_diff = []
    for n in ids:
        a = sorted((g['measure_index'], g['field'], g['dimension'], g['text']) for g in detected.get(n, []))
        if a != sorted(raw_signals.get(n, [])):
            sig_diff.append(n)
    # Published signals are one per dimension, so a joined rule dimension ("age+sex") becomes two.
    n_audit_signals = sum(len(v) for v in detected.values())
    n_rule_signals = sum(len(v) for v in raw_signals.values())
    n_published = sum(r['subgroup']['n_signals'] or 0 for r in ours.values())
    joined = sum(1 for v in raw_signals.values() for g in v if '+' in g[2])

    c2 = [n for n in ids if n in ours and ours[n]['comparative']['C2']]
    count = lambda name: sum(1 for n in ids if n in ours and flag(ours[n], name) is True)  # noqa: E731
    production = {'B1': count('B1'), 'F1': count('F1'), 'O1': count('O1'), 'A1': count('A1'),
                  'all_four_usable': count('four_usable'), 'C1': count('C1'), 'C2': len(c2),
                  'between_arm_analysis_among_C2': sum(1 for n in c2 if ours[n]['between_arm_analysis']['posted']),
                  'between_arm_estimate_with_both_limits_among_C2':
                      sum(1 for n in c2 if ours[n]['between_arm_analysis']['with_estimate_and_both_limits']),
                  'SG2_trials_with_a_signal': len(ours_detected)}
    out = {
        'what_this_is': ('A check of the port: the production module and the audit scripts run on the same cached '
                         'records and are compared trial by trial. It does not establish that the rules are '
                         'scientifically adequate.'),
        'method_version': RS.METHOD_VERSION,
        'input': {'cache': str(CACHE.relative_to(ROOT)), 'cache_sha256': hashlib.sha256(CACHE.read_bytes()).hexdigest(),
                  'sampled_ids': len(ids), 'records_read': len(ours),
                  'ids_not_in_cache': sorted(set(ids) - set(ours)), 'cache_ids_not_sampled': sorted(set(ours) - set(ids))},
        'parse_failures': [{'nct_id': n, 'failures': r['parse']['failures']} for n, r in sorted(ours.items())
                           if r['parse']['status'] != 'ok'],
        'headline': [{'figure': k, 'audit': AUDIT_HEADLINE[k], 'production': production[k],
                      'difference': production[k] - AUDIT_HEADLINE[k]} for k in AUDIT_HEADLINE],
        'per_trial_flags': per_flag,
        'per_trial_denominators': per_den,
        'subgroup': {
            'audit_trials_with_a_signal': len(detected), 'production_trials_with_a_signal': len(ours_detected),
            'only_in_audit': sorted(set(detected) - ours_detected), 'only_in_production': sorted(ours_detected - set(detected)),
            'audit_signals': n_audit_signals, 'production_rule_signals': n_rule_signals,
            'trials_whose_signal_lists_differ': sig_diff[:20], 'n_trials_whose_signal_lists_differ': len(sig_diff),
            'published_signals_one_per_dimension': n_published, 'rule_signals_with_a_joined_dimension': joined,
            'published_signals_by_dimension': dict(sorted(Counter(
                g['dimension'] for r in ours.values() for g in r['subgroup']['signals'] or []).items())),
            'subgroup_state_by_dimension_with_no_reviews': {
                d: dict(sorted(Counter(r['subgroup']['by_dimension'][d] for r in ours.values()).items()))
                for d in RS.DIMENSIONS},
            'study_state_with_no_reviews': dict(sorted(Counter(r['subgroup']['state'] for r in ours.values()).items())),
        },
        'p_value_forms_among_between_arm_analyses_on_primary_measures': dict(sorted(Counter(
            a['p_value_form'] for r in ours.values() for p in r['primary_measures'] or []
            for a in p['analyses'] if a['between_arm']).items())),
    }
    (HERE / 'comparison.json').write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n')
    for row in out['headline']:
        print(f"{row['figure']:52s} audit {row['audit']:5d}  production {row['production']:5d}  diff {row['difference']:+d}")
    differing = {k: v['trials_differing'] for k, v in {**per_flag, **per_den}.items() if v['trials_differing']}
    print('per-trial fields differing:', differing or 'none')
    print('signal lists differing:', len(sig_diff), '| parse failures:', len(out['parse_failures']))
    return 0 if not differing and not sig_diff and all(r['difference'] == 0 for r in out['headline']) else 1


if __name__ == '__main__':
    sys.exit(main())
