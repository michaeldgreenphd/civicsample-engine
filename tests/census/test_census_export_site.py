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
    assert manifest['run_id'] == 'census-data-2099-01-01-abcdef0123'
    race = json.loads((out / 'areas.race.json').read_text())
    assert race['run_id'] == manifest['run_id'] and len(race['areas']) == 1          # the other site basis stays with the build
    assert 'site_basis' not in race['areas'][0] and race['site_basis'] == 'one_listed_facility'
    assert json.loads((out / 'areas.ethnicity.json').read_text())['areas'] == []
    assert json.loads((out / 'summary.json').read_text())['run_id'] == manifest['run_id']
    assert set(manifest['files']) == {'areas.race.json', 'areas.ethnicity.json', 'areas.sex.json', 'summary.json'}
    assert 'sources' not in manifest and manifest['n_sources'] == 1                 # URLs stay with the build's manifest


def test_an_altered_build_is_refused(tmp_path: Path) -> None:
    build = _build(tmp_path)
    (build / 'areas.json').write_text('{"schema_version": 1, "areas": []}')
    with pytest.raises(ValueError, match='do not match the build manifest'):
        export_site.export(build, tmp_path / 'site')
    assert not (tmp_path / 'site').exists()
