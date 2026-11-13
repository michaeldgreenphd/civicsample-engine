#!/usr/bin/env python3
"""Recompute a sample of reference shares straight from the Census API, by a second code path.

  python3 scripts/census/check_against_api.py build/census/trials.jsonl.gz [--n 12]

What this does and does not establish. The build reads age bands from each table's labels and
sums the all-residents table onto the race tables' bands. This script does neither: it names
the variables it needs by number, written out below from the published table shells, asks the
API for exactly those cells for one state, and divides. Agreement shows the build's label
parsing, band collapsing, sex selection, area join and arithmetic are right. It uses the same
source and the same definition of the reference population, so it says nothing about whether
that population is the right comparison: that is a scientific question this cannot test.

Only trials whose eligibility needs no partial band are checked (all ages or 18 and over, no
upper limit), in the 2020-2024 window, at state level.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sys

import requests

# B01001 (all residents): male cells 003-025, female 027-049; 18 and over starts at 007 / 031.
# B01001A-I (one race or origin group): male 003-016, female 018-031; 18 and over starts at 007 / 022.
TOTAL = {'male': (3, 25, 7), 'female': (27, 49, 31)}
GROUP = {'male': (3, 16, 7), 'female': (18, 31, 22)}
TABLES = {'white': 'B01001A', 'black_african_american': 'B01001B', 'hispanic_latino': 'B01001I'}


def cells(table: str, spans: dict[str, tuple[int, int, int]], sexes: list[str], adults: bool) -> list[str]:
    return [f'{table}_{i:03d}E' for sex in sexes for i in range(spans[sex][2] if adults else spans[sex][0], spans[sex][1] + 1)]


def fetch(variables: list[str], state: str) -> float:
    total = 0.0
    for i in range(0, len(variables), 45):          # the API takes at most 50 variables a call
        r = requests.get('https://api.census.gov/data/2024/acs/acs5', timeout=120,
                         params={'get': ','.join(variables[i:i + 45]), 'for': f'state:{state}', 'key': os.environ['CENSUS_API_KEY']})
        r.raise_for_status()
        header, row = r.json()
        total += sum(float(v) for name, v in zip(header, row) if name != 'state')
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('trials')
    ap.add_argument('--n', type=int, default=12)
    ap.add_argument('--seed', type=int, default=20261113)
    args = ap.parse_args()
    pool = []
    with gzip.open(args.trials) as fh:
        for line in fh:
            t = json.loads(line)
            e, race = t.get('eligibility') or {}, (t.get('dimensions') or {}).get('race') or {}
            eth = (t.get('dimensions') or {}).get('ethnicity') or {}
            if ((t.get('benchmark_period') or {}).get('vintage') == 2024 and e.get('age_to_exclusive') is None
                    and e.get('age_from') in (0.0, 18.0) and (race.get('benchmarks') or {}).get('state', {}).get('available')
                    and (eth.get('benchmarks') or {}).get('state', {}).get('available')):
                pool.append(t)
    sample = random.Random(args.seed).sample(pool, min(args.n, len(pool)))
    worst, cache = 0.0, {}
    for t in sorted(sample, key=lambda t: t['nct_id']):
        e, state = t['eligibility'], t['resolution']['state']['geoid']
        key = (state, tuple(e['sexes']), e['age_from'])
        if key not in cache:
            adults = e['age_from'] == 18.0
            den = fetch(cells('B01001', TOTAL, e['sexes'], adults), state)
            cache[key] = {cat: 100 * fetch(cells(tab, GROUP, e['sexes'], adults), state) / den for cat, tab in TABLES.items()}
        built = {**t['dimensions']['race']['benchmarks']['state']['pct'], **t['dimensions']['ethnicity']['benchmarks']['state']['pct']}
        diffs = {cat: abs(cache[key][cat] - built[cat]) for cat in TABLES}
        worst = max(worst, *diffs.values())
        print(f"{t['nct_id']} state {state} sexes {'+'.join(e['sexes'])} from age {e['age_from']:g}: "
              + ', '.join(f'{cat} api {cache[key][cat]:.3f} build {built[cat]:.3f}' for cat in TABLES))
    print(f'checked {len(sample)} trials ({len(cache)} distinct state/sex/age combinations) from {len(pool)} eligible; '
          f'largest absolute difference {worst:.4f} percentage points')
    return 0 if worst < 0.01 else 1


if __name__ == '__main__':
    sys.exit(main())
