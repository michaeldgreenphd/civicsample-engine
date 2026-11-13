#!/usr/bin/env python3
"""Build the census benchmarks for single-site US trials by hand (never scheduled).

  python3 scripts/build_census_benchmarks.py --release PATH/demographics.json.gz \
      --release-tag data-2026-11-08 [--registry PATH/archive.jsonl.gz] [--out build/census]

Needs CENSUS_API_KEY in the environment and scripts/census/requirements.txt installed. Downloads
(ACS tables, boundary files, the registry archive) are cached under build/census_cache/ with a
sha256 each; nothing is written to --out unless the whole build succeeds. See census/README.md.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from census import METHOD_VERSION, SCHEMA_VERSION, acs, benchmark, geography, registry, trials  # noqa: E402

PARAMETERS = {
    'floor_trials_per_area': 50,          # below this an area is withheld with its count
    'referral_zip_min_trials': 200,       # a judgement; flags ZIPs dominated by referral centres
    'size_cap_participants': 5000,        # larger studies are mostly not local recruitment
    'min_participants_in_table': 10,      # a share out of fewer people is too coarse to pool
    'bootstrap_resamples': 2000, 'seed': 20261113, 'interval_level': 0.95,
    'cbsa_min_area_share': geography.CBSA_MIN_SHARE}
LEVEL_ORDER = ('zcta', 'county', 'cbsa', 'state', 'region', 'us')
# Which tiers each site basis admits. A registration whose own text names several sites is in
# neither: its one listed facility is known not to be the whole study.
SITE_BASES = {'one_listed_facility': ('self_described_single_site', 'one_listed_facility', 'conflicting_text'),
              'self_described_single_site': ('self_described_single_site',)}
COMPARED = {'race': list(benchmark.RACE_TABLE), 'ethnicity': ['hispanic_latino', 'not_hispanic_latino'],
            'sex': ['female', 'male']}
LIMITATIONS = [
    'The reference population is a proxy for a potential recruitment population: residents of the area around '
    'the listed site who meet the registered age and sex eligibility. It is not where participants live, the '
    'study catchment, the population with the condition, or an enrolment target. A referral centre may recruit nationally.',
    'One listed facility is not proof of a single-site study, and the registration is read as it stands today; '
    'earlier versions and the historical recruitment location are not examined.',
    'A ZIP code is matched to a ZCTA only by an identical published code; they are different kinds of area.',
    'Registry coordinates are city points, not facility locations; they are used only when no ZCTA polygon exists.',
    'An ACS 5-year estimate describes its whole window, not one year. Each trial is matched to one of four '
    'non-overlapping windows by an enrolment proxy (start to primary completion), which runs late by the length of follow-up.',
    'Margins of error are the survey\'s 90% margins carried through by the Census Bureau\'s approximations; '
    'the interval on each area is a bootstrap over its trials and does not include survey error.',
    'Unknown or unreported participants stay in each trial\'s denominator, so every observed share is a floor; '
    'the "among_known" figure divides by participants with a known category instead. Neither is the truth when unknowns are many.',
    'The survey changed how it codes race responses from 2020: "Two or More Races" and "Some Other Race" are much larger '
    'in the 2020-2024 window than before for the same people. Those two categories are not comparable across windows, '
    'and their gaps against trial tables reflect the question as much as the population.',
    'The registry table describes the baseline analysis population with all arms combined, which can differ from enrolment.',
    'Age is matched only on the reference side (eligible range); participants\' own ages by race are not reported. '
    'Sex is the registry\'s sex field; nothing here describes gender identity or sexual orientation.',
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def producer() -> dict[str, Any]:
    run = lambda *a: subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
    return {'repository': 'civicsample-engine', 'commit': run('rev-parse', 'HEAD') or None,
            'uncommitted_changes': bool(run('status', '--porcelain', '--', 'census', 'scripts/build_census_benchmarks.py'))}


def trial_row(rec: dict[str, Any], facts: dict[str, Any], referral: set[str]) -> dict[str, Any]:
    """Everything decided about one trial before any census data is read."""
    site = rec['study_sites'][0]
    zip5 = (site.get('zip') or '').strip()[:5]
    zip5 = zip5 if len(zip5) == 5 and zip5.isdigit() else None
    period = trials.enrolment_period(rec)
    row: dict[str, Any] = {
        'nct_id': rec['nct_id'], 'study_type': rec.get('study_type'),
        'site': {'facility': site.get('facility'), 'city': site.get('city'), 'state': site.get('state'),
                 'zip_as_registered': site.get('zip'), 'zip5': zip5, 'location_status': facts.get('location_status'),
                 'history': 'current registration only; earlier versions not examined'},
        'site_count': {**trials.site_tier(facts.get('text', '')), 'locations_in_release': 1,
                       'locations_in_registry_now': facts.get('n_locations_now') if facts.get('found') else None},
        'referral_zip': zip5 in referral, 'period': period, 'benchmark_period': trials.assign_window(period['midpoint']),
        'registered_enrollment': rec.get('enrollment'), 'enrollment_type': rec.get('enrollment_type'),
        '_point': facts.get('point')}
    if not facts.get('found'):
        row['site_count']['tier'] = 'registry_record_not_retrieved'
    elif facts.get('n_locations_now') != 1:
        # the release and today's record disagree about the site list: not single-site today
        row['site_count']['tier'] = 'listing_changed_since_release'
    try:
        lo, hi = trials.eligible_ages(rec)
        row['eligibility'] = {'min_age': rec.get('min_age'), 'max_age': rec.get('max_age'), 'age_from': lo,
                              'age_to_exclusive': hi, 'sex': (rec.get('eligibility') or {}).get('sex'),
                              'sexes': list(trials.eligible_sexes(rec))}
    except ValueError as exc:
        row['eligibility'] = {'min_age': rec.get('min_age'), 'max_age': rec.get('max_age'), 'error': str(exc)}
    return row


def add_dimensions(row: dict[str, Any], rec: dict[str, Any], resolution: dict[str, Any],
                   levels: dict[str, acs.Level]) -> None:
    """Observed shares, and the reference population at every level that resolved."""
    row['resolution'] = resolution
    elig = row['eligibility']
    row['dimensions'] = {}
    for dim in ('race', 'ethnicity', 'sex'):
        obs = trials.observed(rec, dim)
        if not obs['reported']:
            row['dimensions'][dim] = {'state': 'not_reported', 'reason': obs['reason']}
            continue
        n = obs['n_in_table']
        d: dict[str, Any] = {
            'state': 'reported', 'population_basis': trials.POPULATION_BASIS, 'n_in_table': n,
            'table_to_enrollment_ratio': round(n / rec['enrollment'], 3) if rec.get('enrollment') else None,
            'observed_counts': obs['counts'], 'original_categories': obs['original_categories'],
            'observed_pct': {k: round(100 * v / n, 3) for k, v in obs['counts'].items()}}
        unknown = obs['counts'].get('unknown_not_reported', obs['counts'].get('unknown', 0))
        known_n = n - unknown - sum(obs.get('unharmonised_counts', {}).values())
        # shares among participants with a known category (a sensitivity figure; see benchmark.pool)
        d['n_known'] = known_n
        d['observed_pct_of_known'] = ({k: round(100 * v / known_n, 3) for k, v in obs['counts'].items()
                                       if k not in ('unknown_not_reported', 'unknown')} if known_n > 0 else None)
        if 'unharmonised_counts' in obs:
            d['unharmonised_counts'] = obs['unharmonised_counts']
        reason = None
        if 'error' in elig:
            reason = 'eligible_ages_unreadable'
        elif dim == 'sex' and len(elig['sexes']) == 1:
            reason = 'single_sex_by_eligibility'           # the sex mix is fixed by design
        elif n > PARAMETERS['size_cap_participants']:
            reason = 'over_size_cap'
        elif n < PARAMETERS['min_participants_in_table']:
            reason = 'under_min_participants'
        d['pooled'] = reason is None
        if reason:
            d['not_pooled_reason'] = reason
        d['benchmarks'] = {}
        if 'error' not in elig:
            for lvl in LEVEL_ORDER:
                where = resolution[lvl]
                if not where.get('geoid'):
                    d['benchmarks'][lvl] = {'available': False, 'reason': where['reason']}
                elif lvl not in levels:
                    d['benchmarks'][lvl] = {'available': False, 'reason': 'level_not_published_for_this_window'}
                else:
                    exp = benchmark.expected(levels[lvl], where['geoid'], dim, tuple(elig['sexes']),
                                             elig['age_from'], elig['age_to_exclusive'])
                    if exp['available']:
                        exp.update(benchmark.compare(d['observed_pct'], exp))
                    d['benchmarks'][lvl] = {'geoid': where['geoid'], **exp}
        row['dimensions'][dim] = d


def pool_areas(rows: list[dict[str, Any]], names: dict[tuple[int, str, str], str]) -> list[dict[str, Any]]:
    out = []
    for lvl in LEVEL_ORDER:
        for dim in ('race', 'ethnicity', 'sex'):
            keys = COMPARED[dim]
            for basis, tiers in SITE_BASES.items():
                for referral in ('with_referral_zips', 'without_referral_zips'):
                    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
                    for r in rows:
                        d = r['dimensions'].get(dim) or {}
                        b = (d.get('benchmarks') or {}).get(lvl) or {}
                        if (d.get('pooled') and b.get('available') and r['site_count']['tier'] in tiers
                                and not (referral == 'without_referral_zips' and r['referral_zip'])):
                            groups[b['geoid']].append({
                                'n': d['n_in_table'], 'obs': d['observed_pct'], 'exp': b['pct'], 'moe': b['moe90_pp'],
                                'obs_known': d['observed_pct_of_known'],
                                'vintage': r['benchmark_period']['vintage'],
                                'unknown': d['observed_pct'].get('unknown_not_reported', d['observed_pct'].get('unknown'))})
                    for geoid in sorted(groups):
                        g = groups[geoid]
                        # one generator per area, seeded from its identity: results do not depend
                        # on the order areas are visited in
                        seed = int(hashlib.sha256(f'{PARAMETERS["seed"]}|{lvl}|{dim}|{basis}|{referral}|{geoid}'.encode())
                                   .hexdigest()[:16], 16)
                        res = benchmark.pool(g, keys, floor=PARAMETERS['floor_trials_per_area'],
                                             resamples=PARAMETERS['bootstrap_resamples'], rng=np.random.default_rng(seed))
                        vint = collections.Counter(x['vintage'] for x in g)
                        latest = max(vint)
                        out.append({'level': lvl, 'geoid': geoid, 'name': names.get((latest, lvl, geoid), ''),
                                    'dimension': dim, 'site_basis': basis, 'variant': referral,
                                    'windows': {f'{v - 4}-{v}': c for v, c in sorted(vint.items())},
                                    'trial_only_unknown_pct_mean': round(float(np.mean([x['unknown'] or 0 for x in g])), 2),
                                    **res})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--release', type=Path, required=True)
    ap.add_argument('--release-tag', required=True)
    ap.add_argument('--registry', type=Path, help='reuse a frozen registry archive')
    ap.add_argument('--cache', type=Path, default=ROOT / 'build' / 'census_cache')
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'census')
    ap.add_argument('--limit', type=int, help='first N cohort trials only (for a quick check; not a result)')
    args = ap.parse_args(argv)
    log = lambda *a: print(*a, file=sys.stderr, flush=True)  # noqa: E731

    with gzip.open(args.release) as fh:
        release = json.load(fh)
    cohort, funnel = trials.cohort(release['data'])
    if args.limit:
        cohort = cohort[:args.limit]
    log(f'cohort: {len(cohort)} trials with one listed US facility')

    reg_path = args.registry or args.cache / f'registry_{args.release_tag}.jsonl.gz'
    if not reg_path.exists():
        log('fetching registry text and locations...')
        registry.write_archive(registry.fetch([r['nct_id'] for r in cohort]), reg_path, len(cohort))
    studies, reg_meta = registry.read_archive(reg_path)

    by_zip = collections.Counter((r['study_sites'][0].get('zip') or '').strip()[:5] for r in cohort)
    referral = {z for z, c in by_zip.items() if z and c >= PARAMETERS['referral_zip_min_trials']}
    rows = [trial_row(r, registry.site_facts(studies.get(r['nct_id'])), referral) for r in cohort]
    recs = {r['nct_id']: r for r in cohort}

    sources: list[dict[str, Any]] = []
    names: dict[tuple[int, str, str], str] = {}
    unmatched = [r for r in rows if r['benchmark_period']['vintage'] is None]
    for r in unmatched:
        r['resolution'], r['dimensions'] = None, {}
    for vintage in sorted({r['benchmark_period']['vintage'] for r in rows} - {None}):
        mine = [r for r in rows if r['benchmark_period']['vintage'] == vintage]
        level_names = acs.LEVELS_BY_VINTAGE.get(vintage, LEVEL_ORDER)
        log(f'window {vintage - 4}-{vintage}: {len(mine)} trials; fetching tables')
        for lvl in level_names:
            for table in acs.TABLES.values():
                sources.append(acs.fetch(vintage, table, lvl, args.cache / 'acs'))
        levels = {lvl: acs.load(vintage, lvl, args.cache / 'acs') for lvl in level_names}
        for lvl, data in levels.items():
            for g, i in data.index.items():
                names[(vintage, lvl, g)] = data.names[i]
        for rel in geography.BOUNDARIES[vintage].values():
            sources.append({'product': 'cartographic boundary file', **geography.download(rel, args.cache / 'boundaries')})
        resolver = geography.Resolver(vintage, args.cache / 'boundaries', {r['site']['zip5'] for r in mine if r['site']['zip5']},
                                      set(levels['zcta'].index) if 'zcta' in levels else None)
        for r in mine:
            add_dimensions(r, recs[r['nct_id']], resolver.resolve(r['site']['zip5'], r.pop('_point'), r['site']['state']), levels)
        del levels, resolver
    for r in rows:
        r.pop('_point', None)

    areas = pool_areas([r for r in rows if r.get('resolution')], names)
    summary = summarise(rows, funnel, areas, referral)
    seen, files = set(), []
    for s in sources:                       # each downloaded file once
        if s['url'] not in seen:
            seen.add(s['url'])
            files.append(s)
    manifest = {
        'schema_version': SCHEMA_VERSION, 'method_version': METHOD_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'manual_run': True,
        'producer': producer(),
        'source_release': {'tag': args.release_tag, 'extracted_at': release.get('extracted_at'),
                           'pipeline_commit': release.get('pipeline_commit'), 'sha256': sha256_file(args.release)},
        'registry_archive': reg_meta, 'parameters': PARAMETERS,
        'windows': [f'{a}-{b}' for a, b in trials.WINDOWS], 'product': 'ACS 5-year estimates (90% margins of error)',
        'product_choice': acs.__doc__.split('\n\n')[1], 'population_basis': trials.POPULATION_BASIS,
        'limitations': LIMITATIONS, 'limit': args.limit, 'sources': files}

    stage = Path(tempfile.mkdtemp(prefix='census_', dir=args.out.parent if args.out.parent.exists() else None))
    with gzip.GzipFile(stage / 'trials.jsonl.gz', 'wb', mtime=0) as fh:
        for r in sorted(rows, key=lambda r: r['nct_id']):
            fh.write(json.dumps(r, sort_keys=True, separators=(',', ':')).encode() + b'\n')
    (stage / 'areas.json').write_text(json.dumps({'schema_version': SCHEMA_VERSION, 'areas': areas}, separators=(',', ':')) + '\n')
    (stage / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
    manifest['files'] = {p.name: sha256_file(p) for p in sorted(stage.iterdir())}
    (stage / 'run.json').write_text(json.dumps(manifest, indent=1) + '\n')
    if args.out.exists():
        shutil.rmtree(args.out)
    shutil.move(str(stage), str(args.out))
    log(f'wrote {args.out}')
    return 0


def summarise(rows: list[dict[str, Any]], funnel: dict[str, int], areas: list[dict[str, Any]],
              referral: set[str]) -> dict[str, Any]:
    """Counts with their definitions. Every reason a trial or an area is absent is counted."""
    c = collections.Counter
    res = [r for r in rows if r.get('resolution')]
    dims = {}
    for dim in ('race', 'ethnicity', 'sex'):
        ds = [r['dimensions'].get(dim) or {} for r in rows]
        dims[dim] = {'reported': sum(d.get('state') == 'reported' for d in ds),
                     'not_reported': sum(d.get('state') == 'not_reported' for d in ds),
                     'pooled': sum(bool(d.get('pooled')) for d in ds),
                     'not_pooled_reasons': dict(c(d['not_pooled_reason'] for d in ds if d.get('not_pooled_reason'))),
                     'benchmark_available_by_level': {
                         lvl: sum(bool(((d.get('benchmarks') or {}).get(lvl) or {}).get('available')) for d in ds)
                         for lvl in LEVEL_ORDER},
                     'benchmark_unavailable_reasons': {
                         lvl: dict(c(b['reason'] for d in ds for b in [((d.get('benchmarks') or {}).get(lvl) or {})]
                                     if b and not b.get('available'))) for lvl in LEVEL_ORDER}}
    agree = [r['resolution']['county'].get('agrees_with_city_point') for r in res]
    shown = c((a['level'], a['dimension'], a['site_basis'], a['variant'], a['display_rule']) for a in areas)
    return {
        'schema_version': SCHEMA_VERSION,
        'cohort_funnel': {**funnel, 'definition': 'Weekly release records by the facilities their registration lists.'},
        'site_count_tiers': dict(c(r['site_count']['tier'] for r in rows)),
        'enrolment_period_basis': dict(c(r['period']['basis'] for r in rows)),
        'benchmark_window': dict(c(str(r['benchmark_period']['window']) for r in rows)),
        'benchmark_window_reason': dict(c(r['benchmark_period']['reason'] for r in rows)),
        'resolution': {lvl: dict(c(r['resolution'][lvl].get('method') or r['resolution'][lvl]['reason'] for r in res))
                       for lvl in LEVEL_ORDER},
        'county_from_zcta_agrees_with_city_point': {'agree': agree.count(True), 'disagree': agree.count(False),
                                                    'not_comparable': agree.count(None)},
        'county_area_share': {'under_0.5': sum(1 for r in res if (r['resolution']['county'].get('area_share') or 1) < 0.5),
                              'at_least_0.9': sum(1 for r in res if (r['resolution']['county'].get('area_share') or 0) >= 0.9)},
        'referral_zips': {'n_zips': len(referral), 'n_trials': sum(r['referral_zip'] for r in rows)},
        'dimensions': dims,
        'areas': [{'level': k[0], 'dimension': k[1], 'site_basis': k[2], 'variant': k[3], 'display_rule': k[4], 'n': n}
                  for k, n in sorted(shown.items())]}


if __name__ == '__main__':
    sys.exit(main())
