"""The site export is a selection of a build: nothing recomputed, altered builds refused."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('export_site', ROOT / 'scripts' / 'census' / 'export_site.py')
export_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_site)


def _build(tmp: Path) -> Path:
    build = tmp / 'build'
    build.mkdir()
    areas = [{'level': 'cbsa', 'geoid': '1', 'name': 'A', 'dimension': dim, 'site_basis': basis, 'variant': 'with_referral_zips',
              'n_trials': 3, 'display_rule': 'withheld', 'gate_reason': 'fewer than 50 qualifying trials'}
             for dim in ('race', 'sex') for basis in ('one_listed_facility', 'self_described_single_site')]
    (build / 'areas.json').write_text(json.dumps({'schema_version': 1, 'areas': areas}))
    (build / 'summary.json').write_text(json.dumps({'schema_version': 1, 'cohort_funnel': {'records': 9}}))
    run = {'method_version': 'm', 'generated_at': 't', 'producer': {'commit': 'abcdef0123456789'},
           'source_release': {'tag': 'data-2099-01-01'}, 'parameters': {}, 'windows': [], 'product': 'p',
           'population_basis': 'b', 'comparison_label': 'c', 'default_level': {'level': 'cbsa'}, 'tables': {},
           'margin_of_error_level': 0.9, 'limitations': ['l'], 'registry_archive': {'retrieved_at': 'r'},
           'sources': [{'url': 'https://api.census.gov/x?key=REDACTED'}],
           'files': {n: hashlib.sha256((build / n).read_bytes()).hexdigest() for n in ('areas.json', 'summary.json')}}
    (build / 'run.json').write_text(json.dumps(run))
    return build


def test_the_export_keeps_one_site_basis_and_names_its_build(tmp_path: Path) -> None:
    out = tmp_path / 'site'
    manifest = export_site.export(_build(tmp_path), out)
    assert manifest['run_id'].startswith('census-data-2099-01-01-abcdef0123-') and len(manifest['run_id']) == 42
    race = json.loads((out / 'areas.race.json').read_text())
    assert race['run_id'] == manifest['run_id'] and len(race['areas']) == 1          # the other site basis stays with the build
    assert 'site_basis' not in race['areas'][0] and race['site_basis'] == 'one_listed_facility'
    assert json.loads((out / 'areas.ethnicity.json').read_text())['areas'] == []
    assert json.loads((out / 'summary.json').read_text())['run_id'] == manifest['run_id']
    assert set(manifest['files']) == {'areas.race.json', 'areas.ethnicity.json', 'areas.sex.json', 'summary.json'}
    assert 'sources' not in manifest and manifest['n_sources'] == 1                 # URLs stay with the build's manifest


def test_two_builds_of_one_release_at_one_commit_get_different_ids(tmp_path: Path) -> None:
    # the registry is read live, so the same code and release can give different files
    for name in 'abc':
        (tmp_path / name).mkdir()
    first = export_site.export(_build(tmp_path / 'a'), tmp_path / 'a' / 'site')
    build = _build(tmp_path / 'b')
    (build / 'summary.json').write_text(json.dumps({'schema_version': 1, 'cohort_funnel': {'records': 10}}))
    run = json.loads((build / 'run.json').read_text())
    run['files']['summary.json'] = hashlib.sha256((build / 'summary.json').read_bytes()).hexdigest()
    (build / 'run.json').write_text(json.dumps(run))
    second = export_site.export(build, tmp_path / 'b' / 'site')
    assert first['run_id'] != second['run_id'] and first['run_id'][:33] == second['run_id'][:33]
    assert export_site.export(_build(tmp_path / 'c'), tmp_path / 'c' / 'site')['run_id'] == first['run_id']


def test_an_altered_build_is_refused(tmp_path: Path) -> None:
    build = _build(tmp_path)
    (build / 'areas.json').write_text('{"schema_version": 1, "areas": []}')
    with pytest.raises(ValueError, match='do not match the build manifest'):
        export_site.export(build, tmp_path / 'site')
    assert not (tmp_path / 'site').exists()


def _load(name: str, rel: str):  # noqa: ANN202 - a module object
    s = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(s)
    s.loader.exec_module(module)
    return module


def test_a_trial_with_no_usable_date_keeps_its_tables_and_loses_only_its_benchmarks() -> None:
    build = _load('build_census_benchmarks', 'scripts/build_census_benchmarks.py')
    rec = {'nct_id': 'NCT0', 'enrollment': 40, 'min_age': '18 Years', 'max_age': 'N/A', 'eligibility': {'sex': 'ALL'},
           'study_sites': [{'country': 'United States', 'zip': '02114', 'state': 'Massachusetts'}],
           'race': {'reported': True, 'omb_totals': {'white': 30, 'asian': 6, 'unknown_not_reported': 4}},
           'sex': {'reported': True, 'totals': {'female': 20, 'male': 20}}}
    row = build.trial_row(rec, {'found': True, 'n_locations_now': 1, 'text': ''}, set())
    assert row['benchmark_period']['reason'] == 'no_usable_date'
    row.pop('_point', None)
    build.add_dimensions(row, rec, None, {})
    race = row['dimensions']['race']
    assert race['state'] == 'reported' and race['observed_counts']['white'] == 30 and race['denominators']['classified'] == 36
    assert race['pooled'] is False and race['not_pooled_reason'] == 'no_benchmark_window'
    assert race['benchmarks']['state'] == {'available': False, 'reason': 'no_benchmark_window'}
    assert row['dimensions']['ethnicity'] == {'state': 'not_reported', 'reason': 'ethnicity_not_reported'}
    assert row['resolution'] is None


def test_the_api_check_never_puts_the_key_in_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _load('check_against_api', 'scripts/census/check_against_api.py')

    class Reply:
        status_code = 429
        url = 'https://api.census.gov/data/2024/acs/acs5?get=B01001_001E&for=state%3A06&key=SECRETKEY123'

        def json(self) -> object:
            raise ValueError('not json')

    monkeypatch.setenv('CENSUS_API_KEY', 'SECRETKEY123')
    monkeypatch.setattr(check.requests, 'get', lambda *a, **k: Reply())
    with pytest.raises(RuntimeError) as err:
        check.fetch(['B01001_001E'], '06')
    assert 'SECRETKEY123' not in str(err.value) and 'key=REDACTED' in str(err.value) and '429' in str(err.value)


def test_unreadable_eligibility_ages_are_written_at_every_level() -> None:
    build = _load('build_census_benchmarks', 'scripts/build_census_benchmarks.py')
    rec = {'nct_id': 'NCT1', 'enrollment': 40, 'min_age': 'adult', 'max_age': 'N/A', 'eligibility': {'sex': 'ALL'},
           'start_date': '2016-01-01', 'primary_completion_date': '2017-01-01',
           'study_sites': [{'country': 'United States', 'zip': '02114', 'state': 'Massachusetts'}],
           'race': {'reported': True, 'omb_totals': {'white': 30, 'asian': 10}}}
    row = build.trial_row(rec, {'found': True, 'n_locations_now': 1, 'text': ''}, set())
    assert 'error' in row['eligibility']
    row.pop('_point', None)
    resolution = {lvl: {'geoid': '1'} for lvl in build.LEVEL_ORDER}
    build.add_dimensions(row, rec, resolution, {})
    race = row['dimensions']['race']
    assert race['pooled'] is False and race['not_pooled_reason'] == 'eligible_ages_unreadable'
    assert race['benchmarks'] == {lvl: {'available': False, 'reason': 'eligible_ages_unreadable'} for lvl in build.LEVEL_ORDER}
    assert race['geography_sensitivity'] == {'levels_compared': [], 'flag': None, 'reason': 'eligible_ages_unreadable'}


def test_a_lone_facility_that_moved_is_not_treated_as_the_release_site() -> None:
    build = _load('build_census_benchmarks', 'scripts/build_census_benchmarks.py')
    site = {'country': 'United States', 'zip': '02114-1234', 'state': 'Massachusetts', 'city': 'Boston', 'facility': 'Old name'}
    rec = {'nct_id': 'NCT2', 'study_sites': [site], 'min_age': '18 Years', 'max_age': 'N/A', 'eligibility': {'sex': 'ALL'}}
    same = {'city': 'boston', 'state': 'Massachusetts', 'zip': '02114', 'country': 'United States'}
    facts = {'found': True, 'n_locations_now': 1, 'text': 'A single-center study', 'point': (42.3, -71.1)}
    kept = build.trial_row(rec, {**facts, 'place_now': same}, set())
    assert kept['site_count']['tier'] == 'self_described_single_site' and kept['_point'] == (42.3, -71.1)
    moved = build.trial_row(rec, {**facts, 'place_now': {**same, 'city': 'Worcester', 'zip': '01605'}}, set())
    assert moved['site_count']['tier'] == 'listing_changed_since_release' and moved['_point'] is None
    assert moved['site_count']['place_in_registry_now']['city'] == 'Worcester'
    # the build takes the window from the unrounded midpoint, not from the rounded field it records
    edge = build.trial_row({**rec, 'start_date': '2014-12-30', 'primary_completion_date': '2014-12-31'}, facts, set())
    assert edge['period']['midpoint'] == 2015.0 and edge['benchmark_period']['window'] == '2010-2014'
    assert build.place_changed(site, None) is False          # no place recorded: no change can be shown
    # a field today's record no longer carries is not evidence of a move
    no_zip = build.trial_row(rec, {**facts, 'place_now': {**same, 'zip': None}}, set())
    assert no_zip['site_count']['tier'] == 'self_described_single_site' and no_zip['_point'] == (42.3, -71.1)
    assert no_zip['site_count']['place_fields_not_comparable'] == ['zip']
    assert 'place_fields_not_comparable' not in kept['site_count']
    # a missing field does not hide a contradiction in another one
    assert build.place_changed(site, {**same, 'zip': None, 'state': 'Maine'}) is True


def test_the_build_computes_no_known_category_share_for_a_one_sided_ethnicity_table() -> None:
    build = _load('build_census_benchmarks', 'scripts/build_census_benchmarks.py')
    rec = {'nct_id': 'NCT3', 'enrollment': 53, 'min_age': '18 Years', 'max_age': 'N/A', 'eligibility': {'sex': 'ALL'},
           'study_sites': [{'country': 'United States', 'zip': '02114', 'state': 'Massachusetts'}],
           'ethnicity': {'reported': True, 'omb_totals': {'hispanic_latino': 2, 'not_hispanic_latino': 0, 'unknown_not_reported': 51},
                         'raw_categories': [{'omb_category': 'hispanic_latino', 'original': 'Hispanic', 'count': 2}]}}
    row = build.trial_row(rec, {'found': True, 'n_locations_now': 1, 'text': ''}, set())
    row.pop('_point', None)
    build.add_dimensions(row, rec, None, {})
    eth = row['dimensions']['ethnicity']
    assert eth['observed_pct']['hispanic_latino'] == pytest.approx(3.774, abs=0.001)      # 2 of 53: the floor stands
    assert eth['observed_pct_of_classified'] is None                                       # not 100%
