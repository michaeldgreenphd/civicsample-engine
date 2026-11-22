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
    # a trial is flagged when the reference share of any category moves by this much between
    # the area levels available to it; a judgement about what counts as a material change
    'geography_spread_flag_pp': 10.0,
    'cbsa_min_area_share': geography.CBSA_MIN_SHARE}
LEVEL_ORDER = ('zcta', 'county', 'cbsa', 'state', 'region', 'us')
# Which tiers each site basis admits. A registration whose own text names several sites is in
# neither: its one listed facility is known not to be the whole study.
SITE_BASES = {'one_listed_facility': ('self_described_single_site', 'one_listed_facility', 'conflicting_text'),
              'self_described_single_site': ('self_described_single_site',)}
SENSITIVITY_LEVELS = ('zcta', 'county', 'cbsa', 'state')
# The level shown first. Not the smallest area: a ZCTA around a medical campus can hold almost
# no residents (1,297 race comparisons found none who met eligibility) and exists for only three
# of the four windows, and its margins of error are the widest. The metro area is the Census
# Bureau's commuting-based unit, resolves for nearly every site, and has stable estimates.
# County and ZCTA are published beside it; none is "the" benchmark.
DEFAULT_LEVEL = {'level': 'cbsa', 'label': 'Metropolitan or micropolitan statistical area',
                 'reason': 'Widest coverage among local levels and stable estimates; reflects commuting patterns. '
                           'A default for display, not a claim about where participants live.'}
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
    'Two denominators are reported and neither replaces the other. A: everyone the registry table counts, with unknown '
    'kept as its own row, so each observed share is a floor ("unknown_bounds" gives the difference at that floor and at '
    'the ceiling where every unknown is in the category; "direction_settled" is false when their signs differ). '
    'B ("of_classified"): participants with a reported, classifiable category. B is not a correction that recovers the '
    'true distribution; missingness may be systematic. Tables whose rows overlap or combine race with ethnicity are not pooled.',
    'Race comparability across windows is affected by a measurement change: the ACS question on race was revised in 2020 '
    'to be consistent with the 2020 Census question, and the Census Bureau states that ACS race data "are not directly '
    'comparable across all years" (2023 ACS Subject Definitions, Race, Comparability). The 2020-2024 window lies wholly '
    'after the revision and the three earlier windows wholly before it. summary.json reports the national share of each '
    'race table by window; how much of any change is measurement and how much is population is not estimated here.',
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


PLACE_FIELDS = ('city', 'state', 'country', 'zip')


def _place(record: dict[str, Any], field: str) -> str:
    """One place field in comparable form: case and spacing removed, a ZIP cut to five digits."""
    value = ' '.join(str(record.get(field) or '').casefold().split())
    return value[:5] if field == 'zip' else value


def place_changed(release_site: dict[str, Any], place_now: dict[str, Any] | None) -> bool:
    """Whether the single facility in today's record is somewhere other than the release's.

    Only a field that both records carry, with different values, shows a move. A field that
    one record lacks shows nothing either way (see place_fields_not_comparable). The facility
    name is left out: institutions are renamed without moving."""
    if not place_now:
        return False
    return any(_place(release_site, f) and _place(place_now, f) and _place(release_site, f) != _place(place_now, f)
               for f in PLACE_FIELDS)


def place_fields_not_comparable(release_site: dict[str, Any], place_now: dict[str, Any] | None) -> list[str]:
    """The place fields that one record carries and the other lacks.

    These are insufficient for comparison, not evidence of a move: a registration can drop
    a ZIP while the facility stays where it was. They are written on the trial so that a
    'no change found' is never read as 'every field was checked'."""
    if not place_now:
        return []
    return [f for f in PLACE_FIELDS if bool(_place(release_site, f)) != bool(_place(place_now, f))]


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
        'referral_zip': zip5 in referral, 'period': period,
        # from the unrounded midpoint: the rounded one in `period` can cross a year boundary
        'benchmark_period': trials.assign_window(trials.enrolment_midpoint(rec)[1]),
        'registered_enrollment': rec.get('enrollment'), 'enrollment_type': rec.get('enrollment_type'),
        '_point': facts.get('point')}
    if not facts.get('found'):
        row['site_count']['tier'] = 'registry_record_not_retrieved'
    elif facts.get('n_locations_now') != 1:
        # the release and today's record disagree about the site list: not single-site today
        row['site_count']['tier'] = 'listing_changed_since_release'
    elif place_changed(site, facts.get('place_now')):
        # Still one facility, but not where the release recorded it. The ZIP comes from the
        # release and the city point from today's record, so together they would describe two
        # different sites: the point is dropped and the trial leaves both site bases.
        row['site_count']['tier'] = 'listing_changed_since_release'
        row['site_count']['place_in_registry_now'] = facts['place_now']
        row['_point'] = None
    missing = place_fields_not_comparable(site, facts.get('place_now'))
    if missing:
        row['site_count']['place_fields_not_comparable'] = missing
    try:
        lo, hi = trials.eligible_ages(rec)
        row['eligibility'] = {'min_age': rec.get('min_age'), 'max_age': rec.get('max_age'), 'age_from': lo,
                              'age_to_exclusive': hi, 'sex': (rec.get('eligibility') or {}).get('sex'),
                              'sexes': list(trials.eligible_sexes(rec)),
                              'healthy_volunteers': bool(rec.get('healthy_volunteers'))}
        caveats = trials.reference_caveats(rec, lo, hi, trials.eligible_sexes(rec), row['referral_zip'])
        # 'contextual': the area's residents are background for this trial, not a like-for-like
        # denominator. True of nearly every trial, since most enrol patients.
        row['reference_population'] = {'label': 'site-area population benchmark',
                                       'fit': 'contextual' if caveats else 'general', 'caveats': caveats}
    except ValueError as exc:
        row['eligibility'] = {'min_age': rec.get('min_age'), 'max_age': rec.get('max_age'), 'error': str(exc)}
    return row


def add_dimensions(row: dict[str, Any], rec: dict[str, Any], resolution: dict[str, Any] | None,
                   levels: dict[str, acs.Level], registry_ethnicity_rows: list[dict[str, Any]] | None = None) -> None:
    """Observed shares, and the reference population at every level that resolved.

    A trial with no benchmark window (no usable date) has no resolution: what its tables
    report is kept in full, and only its benchmarks are marked unavailable, with the reason."""
    row['resolution'] = resolution
    elig = row['eligibility']
    row['dimensions'] = {}
    for dim in ('race', 'ethnicity', 'sex'):
        obs = trials.observed(rec, dim, registry_ethnicity_rows if dim == 'race' else None)
        if not obs['reported']:
            row['dimensions'][dim] = {'state': 'not_reported', 'reason': obs['reason']}
            continue
        n = obs['n_in_table']
        d: dict[str, Any] = {
            'state': 'reported', 'population_basis': trials.POPULATION_BASIS, 'n_in_table': n,
            'table_to_enrollment_ratio': round(n / rec['enrollment'], 3) if rec.get('enrollment') else None,
            'observed_counts': obs['counts'], 'original_categories': obs['original_categories'],
            'structure': obs['structure'],
            # denominator A: everyone the table counts
            'observed_pct': {k: round(100 * v / n, 3) for k, v in obs['counts'].items()},
            'denominators': {'all_in_table': n, 'classified': obs['n_classified'], 'unknown_or_not_reported': obs['n_unknown'],
                             'unmapped_category': obs['n_unmapped'],
                             'unknown_or_unmapped_pct': round(100 * (n - obs['n_classified']) / n, 3)}}
        known_n = obs['n_classified']
        # denominator B: participants with a reported, classifiable category. Only where the rows
        # can be read as one classification of each person; never a correction for missing data.
        d['observed_pct_of_classified'] = (
            {k: round(100 * v / known_n, 3) for k, v in obs['counts'].items() if not k.startswith('unknown')}
            if known_n > 0 and obs['structure']['usable_for_shares']
            and obs['structure'].get('classified_share_usable', True) else None)
        if 'unharmonised_counts' in obs:
            d['unharmonised_counts'] = obs['unharmonised_counts']
        reason = None
        if 'error' in elig:
            reason = 'eligible_ages_unreadable'
        elif resolution is None:
            reason = 'no_benchmark_window'                              # no usable date to match one
        elif not obs['structure']['usable_for_shares']:
            reason = 'table_rows_not_one_classification'             # see trials.category_structure
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
        # Two conditions leave no reference population at any level. Each is written at every
        # level with its reason, so the count of benchmarks always equals the count of tables.
        blocked = ('eligible_ages_unreadable' if 'error' in elig
                   else 'no_benchmark_window' if resolution is None else None)
        if blocked:
            d['benchmarks'] = {lvl: {'available': False, 'reason': blocked} for lvl in LEVEL_ORDER}
            d['geography_sensitivity'] = {'levels_compared': [], 'flag': None, 'reason': blocked}
        else:
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
            # How far the reference share moves with the choice of area, for this trial.
            avail = {lvl: d['benchmarks'][lvl]['pct'] for lvl in SENSITIVITY_LEVELS
                     if d['benchmarks'].get(lvl, {}).get('available')}
            if len(avail) >= 2:
                spread = {k: max(v[k] for v in avail.values()) - min(v[k] for v in avail.values()) for k in COMPARED[dim]}
                worst = max(spread, key=lambda k: spread[k])
                d['geography_sensitivity'] = {
                    'levels_compared': list(avail), 'max_spread_pp': round(spread[worst], 2), 'category': worst,
                    'flag': bool(spread[worst] >= PARAMETERS['geography_spread_flag_pp'])}
            else:
                d['geography_sensitivity'] = {'levels_compared': list(avail), 'flag': None,
                                              'reason': 'fewer_than_two_local_levels_available'}
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
                                'obs_known': d['observed_pct_of_classified'], 'n_known': d['denominators']['classified'],
                                'geo_flag': (d.get('geography_sensitivity') or {}).get('flag'),
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
                                    'trials_sensitive_to_geography': sum(1 for x in g if x['geo_flag']),
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

    # A quick run on the first N trials keeps its own archives, and any cached archive is reused
    # only if it was written for exactly the trials asked for now (registry.covers).
    suffix = f'_limit{args.limit}' if args.limit else ''
    ids = [r['nct_id'] for r in cohort]
    reg_path = args.registry or args.cache / f'registry_{args.release_tag}{suffix}.jsonl.gz'
    studies, reg_meta = registry.cached_or_fetched(reg_path, ids, may_fetch=args.registry is None, log=log)

    # Race tables titled with ethnicity: the registry's own table shows whether an ethnicity row
    # stood among the race rows, which the release cannot (trials.category_structure).
    titled = [r['nct_id'] for r in cohort if (r.get('race') or {}).get('reported')
              and trials.race_table_titled_with_ethnicity(r)]
    base_path = args.cache / f'registry_baseline_{args.release_tag}{suffix}.jsonl.gz'
    baseline, base_meta = (registry.cached_or_fetched(base_path, titled, registry.BASELINE_FIELDS, log=log)
                           if titled else ({}, None))
    ethnicity_rows = {n: registry.ethnicity_rows_in_race_tables(baseline.get(n)) for n in titled}

    by_zip = collections.Counter((r['study_sites'][0].get('zip') or '').strip()[:5] for r in cohort)
    referral = {z for z, c in by_zip.items() if z and c >= PARAMETERS['referral_zip_min_trials']}
    rows = [trial_row(r, registry.site_facts(studies.get(r['nct_id'])), referral) for r in cohort]
    recs = {r['nct_id']: r for r in cohort}

    sources: list[dict[str, Any]] = []
    names: dict[tuple[int, str, str], str] = {}
    reference_series: dict[str, dict[str, float]] = {}
    unmatched = [r for r in rows if r['benchmark_period']['vintage'] is None]
    for r in unmatched:
        r.pop('_point', None)
        add_dimensions(r, recs[r['nct_id']], None, {}, ethnicity_rows.get(r['nct_id']))
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
        us = levels['us']                    # all ages, both sexes: the published national shares
        total = float(np.nansum(us.est[0, acs.TABLE_ORDER.index('total')]))
        reference_series[f'{vintage - 4}-{vintage}'] = {
            cat: round(100 * float(np.nansum(us.est[0, t])) / total, 2) for t, cat in enumerate(acs.TABLE_ORDER) if cat != 'total'}
        for rel in geography.BOUNDARIES[vintage].values():
            sources.append({'product': 'cartographic boundary file', **geography.download(rel, args.cache / 'boundaries')})
        resolver = geography.Resolver(vintage, args.cache / 'boundaries', {r['site']['zip5'] for r in mine if r['site']['zip5']},
                                      set(levels['zcta'].index) if 'zcta' in levels else None)
        for r in mine:
            add_dimensions(r, recs[r['nct_id']], resolver.resolve(r['site']['zip5'], r.pop('_point'), r['site']['state']), levels,
                           ethnicity_rows.get(r['nct_id']))
        del levels, resolver
    for r in rows:
        r.pop('_point', None)

    areas = pool_areas([r for r in rows if r.get('resolution')], names)
    summary = summarise(rows, funnel, areas, referral)
    summary['national_share_by_window_pct'] = {
        'definition': 'Share of all US residents in each ACS table, by window, as pulled. A change between windows mixes '
                      'population change with the 2020 revision of the race question; the two are not separated.',
        'series': reference_series}
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
        'registry_archive': reg_meta, 'registry_baseline_archive': base_meta, 'parameters': PARAMETERS,
        'windows': [f'{a}-{b}' for a, b in trials.WINDOWS], 'product': 'ACS 5-year estimates (90% margins of error)',
        'product_choice': acs.__doc__.split('\n\n')[1], 'population_basis': trials.POPULATION_BASIS,
        'comparison_label': 'site-area population benchmark', 'default_level': DEFAULT_LEVEL,
        'tables': acs.TABLES, 'margin_of_error_level': 0.90,
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
                     'category_structure': dict(c(d['structure']['check'] for d in ds if d.get('structure'))),
                     'category_cautions': dict(c(x for d in ds for x in (d.get('structure') or {}).get('cautions', []))),
                     'classified_share_computed': sum(d.get('observed_pct_of_classified') is not None for d in ds),
                     'classified_share_not_computed_reasons': dict(c(
                         d['structure'].get('classified_share_reason') or ('rows_not_one_classification' if not d['structure']['usable_for_shares'] else 'nobody_classified')
                         for d in ds if d.get('structure') and d.get('observed_pct_of_classified') is None)),
                     'geography_sensitivity': dict(c(str((d.get('geography_sensitivity') or {}).get('flag')) for d in ds
                                                     if d.get('state') == 'reported')),
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
        'reference_population_fit': dict(c((r.get('reference_population') or {}).get('fit', 'not_assessed') for r in rows)),
        # the denominator for "contextual": trials whose eligibility could be read, so fit could be judged
        'reference_population_assessed': {
            'n': sum(1 for r in rows if r.get('reference_population')),
            'definition': 'Trials in the cohort whose registered eligibility could be read, so the fit of a '
                          'general-population benchmark could be assessed. The rest are counted as not_assessed.'},
        'reference_caveats': dict(c(x for r in rows for x in (r.get('reference_population') or {}).get('caveats', []))),
        'dimensions': dims,
        'areas': [{'level': k[0], 'dimension': k[1], 'site_basis': k[2], 'variant': k[3], 'display_rule': k[4], 'n': n}
                  for k, n in sorted(shown.items())]}


if __name__ == '__main__':
    sys.exit(main())
