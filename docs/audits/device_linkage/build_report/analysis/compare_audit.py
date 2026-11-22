#!/usr/bin/env python3
"""Regression of the build against the 2026-11-13 audit: its 80 sampled submissions (name and
manufacturer route) and its submission-number search. Run with `python -I`.

usage: compare_audit.py <audit dir> <build out dir> <target.json>
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    audit, out, target = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    sample = list(csv.DictReader((audit / 'sample_devices.csv').open(encoding='utf-8', newline='')))
    classified = {d['submission_number']: d for d in json.loads((audit / 'out' / 'route_b_classified.json').read_text())}
    subs = {s['submission_number']: s for s in json.loads((out / 'submissions.json').read_text())['submissions']}
    links = json.loads((out / 'links.json').read_text())['links']
    cov = {c['submission_number']: c for c in json.loads((out / 'coverage.json').read_text())['submissions']}
    by_family: dict[str, list[dict]] = {}
    for x in links:
        if x['source']['entity_type'] == 'family':
            by_family.setdefault(x['source']['id'], []).append(x)

    def build_class(sn: str) -> str:
        fl = by_family.get(subs[sn]['family_id'], [])
        if any(x['method'] == 'device_name_and_organisation_match' for x in fl):
            return 'name_and_organisation_candidate'
        if any(x['method'] == 'device_name_match' and x['automated_validation'] == 'not_validated' for x in fl):
            return 'name_only_candidate'
        if any(x['method'] == 'device_name_match' for x in fl):
            return 'name_only_rejected_common_word'
        route = next(c for c in cov[sn]['routes'] if c['route'] == 'registry_name_organisation_search')
        return 'none:' + route['state']
    rollup = {'i': 'i', 'i-f': 'i', 'ii': 'ii', 'ii-c': 'ii', 'iii': 'iii', 'iv-a': 'iv', 'iv-b': 'iv'}
    cross = Counter()
    per_device = []
    for row in sample:
        sn = row['submission_number']
        a, b = classified[sn]['class'], build_class(sn)
        cross[(rollup[a], b)] += 1
        per_device.append({'submission_number': sn, 'audit_class': a, 'build': b, 'device': row['device_clean']})
    # the audit's class (i) device-study pairs
    audit_pairs = [(sn, c['nct_id']) for sn, d in classified.items() for c in d['candidates'] if c['class'] == 'i']
    found = Counter()
    pair_rows = []
    for sn, nct in audit_pairs:
        hit = [x for x in by_family.get(subs[sn]['family_id'], []) if x['target']['id'] == nct]
        how = (hit[0]['method'] + ('' if hit[0]['automated_validation'] == 'not_validated' else ':rejected_by_rule')) \
            if hit else 'absent'
        found[how] += 1
        pair_rows.append({'submission_number': sn, 'nct_id': nct, 'in_build_as': how,
                          'build_reasons': hit[0]['confidence_reasons'] if hit else None})
    sampled_families = {subs[r['submission_number']]['family_id'] for r in sample}
    build_pairs = {(x['source']['id'], x['target']['id']): x['method'] + ':' + x['automated_validation']
                   for f in sampled_families for x in by_family.get(f, [])}
    audit_keys = {(subs[sn]['family_id'], nct) for sn, nct in audit_pairs}
    # stratified estimates over the audit's sampling frame (weights N_h / N from the sample file)
    strata: dict[str, dict] = {}
    for row in sample:
        h = strata.setdefault(row['stratum'], {'N': int(row['stratum_size']), 'n': 0, 'audit_i': 0, 'build_no': 0})
        h['n'] += 1
        h['audit_i'] += rollup[classified[row['submission_number']]['class']] == 'i'
        h['build_no'] += build_class(row['submission_number']) == 'name_and_organisation_candidate'
    total = sum(h['N'] for h in strata.values())
    census = Counter(build_class(sn) for sn in subs)
    route_d = json.loads((audit / 'out' / 'route_d_submission_number.json').read_text())
    audit_d = {(h['submission_number'], h['nct_id']): h['string_found_in_record'] for h in route_d['hits']}
    build_d = {(x['source']['id'], x['target']['id']): x['automated_validation'] for x in links
               if x['method'] == 'submission_number_in_registry_record'}
    result = {
        'sample': {'n': len(sample), 'frame': total, 'seed': 20261113,
                   'strata': {k: {'frame': v['N'], 'drawn': v['n']} for k, v in sorted(strata.items())}},
        'audit_class_by_build_class': {f'{a} -> {b}': n for (a, b), n in sorted(cross.items())},
        'audit_class_i_devices': sum(1 for d in classified.values() if rollup[d['class']] == 'i'),
        'build_name_and_organisation_among_sample': sum(1 for d in per_device if d['build'] == 'name_and_organisation_candidate'),
        'audit_class_i_pairs': len(audit_pairs), 'audit_class_i_unique_studies': len({n for _, n in audit_pairs}),
        'audit_class_i_pairs_in_build': dict(found),
        'build_pairs_for_sampled_families': len(build_pairs),
        'build_pairs_by_method': dict(Counter(build_pairs.values())),
        'build_pairs_not_in_audit_class_i': len(set(build_pairs) - audit_keys),
        'stratified_estimate_pct': {
            'audit_class_i': round(100 * sum(h['N'] / total * h['audit_i'] / h['n'] for h in strata.values()), 1),
            'unweighted_audit_class_i': round(100 * sum(h['audit_i'] for h in strata.values()) / len(sample), 1),
            'build_rule_on_sample': round(100 * sum(h['N'] / total * h['build_no'] / h['n'] for h in strata.values()), 1)},
        'build_rule_census': {'n_submissions': len(subs), 'by_class': dict(sorted(census.items())),
                              'name_and_organisation_pct': round(100 * census['name_and_organisation_candidate'] / len(subs), 1)},
        'submission_number_route': {'audit': {f'{k[0]} {k[1]}': v for k, v in sorted(audit_d.items())},
                                    'build': {f'{k[0]} {k[1]}': v for k, v in sorted(build_d.items())}},
        'per_device': per_device, 'audit_class_i_pair_detail': pair_rows,
    }
    target.write_text(json.dumps(result, indent=1) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('per_device', 'audit_class_i_pair_detail')}, indent=1))


if __name__ == '__main__':
    main()
