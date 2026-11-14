"""The site export of a device-linkage build (scripts/export_device_links_site.py).

Fictional records only (tests/devices/fixture.py, built by tests/devices/site_fixture.py); no
network. ENGINEERING tests: the export selects from a verified build, its tallies add up to the
build's own counts, an altered build is refused, and a refused export leaves the output directory
as it was. None of them establishes that a link is right.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_device_links_site as site  # noqa: E402
import site_fixture  # noqa: E402
from devices import artifacts, links  # noqa: E402


@pytest.fixture(scope='module')
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fictional build on disk, as scripts/build_device_links.py writes one."""
    return site_fixture.build(tmp_path_factory.mktemp('device_site'))


@pytest.fixture(scope='module')
def exported(build: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp('site') / 'devices'
    manifest = site.export(build, out)
    files = {p.name: json.loads(p.read_text(encoding='utf-8')) for p in out.iterdir()}
    return {'out': out, 'manifest': manifest, **files}


def load(build: Path, name: str) -> dict[str, Any]:
    return json.loads((build / name).read_text(encoding='utf-8'))


def copy_build(build: Path, tmp_path: Path, change: Callable[[dict[str, Any]], None] | None = None,
               name: str = 'summary.json', rehash: bool = True) -> Path:
    """A copy of the build with one file changed; `rehash` also rewrites that file's sha256 in
    run.json, which is what a second, different build would carry."""
    target = tmp_path / 'build'
    shutil.copytree(build, target)
    if change:
        obj = load(target, name)
        change(obj)
        (target / name).write_bytes(artifacts.dump(obj))
        if rehash:
            run = load(target, 'run.json')
            run['files'][name] = hashlib.sha256((target / name).read_bytes()).hexdigest()
            (target / 'run.json').write_bytes(artifacts.dump(run))
    return target


def row(exported: dict[str, Any], sn: str) -> dict[str, Any]:
    return next(r for r in exported['submissions.json']['submissions'] if r['submission_number'] == sn)


def counts(holder: dict[str, Any], item: dict[str, Any]) -> dict[str, int]:
    return dict(zip(holder['evidence_kinds'], item['evidence_counts']))


# ---------------------------------------------------------------- the family

def test_every_file_names_one_run_and_matches_the_manifest(exported: dict[str, Any]) -> None:
    run = exported['run.json']
    assert set(run['files']) == set(site.SITE_FILES) and set(exported) - {'out', 'manifest'} == set(run['files']) | {'run.json'}
    for name, digest in run['files'].items():
        assert hashlib.sha256((exported['out'] / name).read_bytes()).hexdigest() == digest, name
        assert exported[name]['run_id'] == run['run_id'] and exported[name]['schema_version'] == 1, name
    assert run['scheduled'] is False and run['method_version'] == artifacts.METHOD_VERSION
    assert run['records'] == {'evidence.json': 9, 'evidence_set_aside.json': 1}
    assert run['reviews'] == {'n_rows': 1, 'n_applied': 1, 'n_orphaned': 0}


def test_the_run_id_is_the_builds_id_with_a_digest_of_its_files(build: Path, exported: dict[str, Any]) -> None:
    built = load(build, 'run.json')
    run = exported['run.json']
    assert run['build_run_id'] == built['run_id'] and run['build_files'] == built['files']
    assert re.fullmatch(re.escape(built['run_id']) + r'-[0-9a-f]{8}', run['run_id'])


def test_two_builds_of_one_day_and_inputs_get_different_ids_and_the_same_build_the_same(
        build: Path, tmp_path: Path) -> None:
    first = site.export(build, tmp_path / 'a' / 'devices')
    again = site.export(copy_build(build, tmp_path / 'same'), tmp_path / 'b' / 'devices')
    assert again['run_id'] == first['run_id'] and again['files'] == first['files']
    # The same inputs (so the same build id), one file different: a build code change, or a
    # registry that answered differently between two acquisitions frozen under one name.
    other = copy_build(build, tmp_path / 'other', lambda s: s['tables'].update(bytes_retrieved=1))
    second = site.export(other, tmp_path / 'c' / 'devices')
    assert second['build_run_id'] == first['build_run_id'] and second['run_id'] != first['run_id']


def test_the_summary_is_the_builds_with_only_the_run_id_changed(build: Path, exported: dict[str, Any]) -> None:
    built = load(build, 'summary.json')
    assert {**exported['summary.json'], 'run_id': built['run_id']} == built
    assert exported['summary.json']['run_id'] == exported['run.json']['run_id']


def test_the_manifest_carries_units_vocabularies_and_source_summaries_and_no_query_string(
        build: Path, exported: dict[str, Any]) -> None:
    built, run = load(build, 'run.json'), exported['run.json']
    assert run['counting_units'] == built['counting_units']
    for name, values in built['vocabularies'].items():
        assert run['vocabularies'][name] == values, name
    assert run['vocabularies']['reviewed_relationships'] == list(links.REVIEWED_RELATIONSHIPS)
    # The order of the tallies is the vocabulary's; the definitions are keyed by kind.
    assert run['vocabularies']['evidence_kinds'] == list(site.EVIDENCE_KINDS)
    assert run['evidence_kinds'] == site.EVIDENCE_KINDS
    assert run['sources']['registry']['registry_data_timestamp'] == '2026-11-13T10:00:06'
    assert run['sources']['text_extraction'] == {'extraction_version': built['sources']['text_extraction']['extraction_version'],
                                                 'ocr': False}
    assert run['sources']['demographic_extractions'] == {'file': 'pilot.json', 'n_documents': 1, 'n_extractions': 3}
    # Archive names and hashes of unpublished files stay with the build.
    assert 'archive' not in json.dumps(run['sources']) and 'sha256_uncompressed' not in json.dumps(run['sources'])
    assert not re.search(r'https?://[^\s"]*\?', json.dumps(run))


def test_curated_row_counts_come_from_the_build_manifest(build: Path, tmp_path: Path) -> None:
    def curated(run: dict[str, Any]) -> None:
        run['inputs']['curated'] = {'reviews.csv': {'file': 'reviews.csv', 'n_rows': 1, 'sha256': 'x'},
                                    'families.csv': {'file': 'families.csv', 'n_rows': 0, 'sha256': 'y'}}
    changed = copy_build(build, tmp_path, curated, name='run.json', rehash=False)
    manifest = site.export(changed, tmp_path / 'out' / 'devices')
    assert manifest['curated'] == {'families.csv': 0, 'reviews.csv': 1}


# ---------------------------------------------------------------- submissions and families

def test_a_row_keeps_every_spelling_its_duplicate_class_and_its_family_size(exported: dict[str, Any]) -> None:
    pearl = row(exported, 'K990003')
    assert pearl['companies_as_listed'] == ['Pearl Co', 'Pearl, Co.'] and pearl['organization_name'] == 'Pearl Co'
    assert (pearl['n_source_rows'], pearl['source_row_numbers'], pearl['duplicate_class']) == (2, [4, 5], 'alias_only')
    zorbex = row(exported, 'K990001')
    assert (zorbex['pathway'], zorbex['decision_date'], zorbex['device_name']) == ('510k', '2021-03-02', 'Zorbex Scan (v2.1)')
    assert zorbex['family_n_submissions'] == 2 and zorbex['family_id'] == row(exported, 'K990002')['family_id']
    assert row(exported, 'DEN990005')['duplicate_class'] is None       # one row: no class, not "identical"
    assert row(exported, 'K990004')['conflicts'] == {'Primary Product Code': ['AAA', 'BBB']}
    # "No difference" is only a finding where an official record was there to compare.
    assert all(r['official_record_available'] is True and r['official_differs_from_list'] == {}
               for r in exported['submissions.json']['submissions'])


def test_coverage_cells_expand_to_the_builds_state_and_reason_for_every_route(build: Path, exported: dict[str, Any]) -> None:
    file = exported['submissions.json']
    assert file['coverage_routes'] == exported['run.json']['vocabularies']['coverage_routes']
    for cov in load(build, 'coverage.json')['submissions']:
        r = row(exported, cov['submission_number'])
        assert (r['coverage_overall'], r['coverage_overall_routes']) == (cov['overall']['state'], cov['overall']['routes'])
        expanded = {route: (state, file['coverage_reasons'][i]) for route, (state, i) in zip(file['coverage_routes'], r['coverage'])}
        assert expanded == {c['route']: (c['state'], c['reason']) for c in cov['routes']}


def test_evidence_tallies_put_each_record_in_one_kind_and_add_up_to_the_builds_counts(
        build: Path, exported: dict[str, Any]) -> None:
    file = exported['submissions.json']
    assert counts(file, row(exported, 'K990001')) == {
        'submission_identifier': 1, 'family_name_and_organisation': 1, 'family_name_only': 0, 'parent_submission': 0,
        'set_aside_by_rule': 0, 'other': 0}
    # K990002 shares the family's name candidate and holds a cited id the registry does not have.
    assert counts(file, row(exported, 'K990002')) == {
        'submission_identifier': 0, 'family_name_and_organisation': 1, 'family_name_only': 0, 'parent_submission': 0,
        'set_aside_by_rule': 0, 'other': 1}
    assert counts(file, row(exported, 'K990003'))['set_aside_by_rule'] == 1
    assert counts(file, row(exported, 'P990007/S002'))['parent_submission'] == 1
    built = {s['submission_number']: s for s in load(build, 'submissions.json')['submissions']}
    for r in file['submissions']:
        b = built[r['submission_number']]
        assert sum(r['evidence_counts']) == b['n_evidence_records'] + b['n_family_evidence_records']
    families = exported['families.json']
    zorbex = families['families'][row(exported, 'K990001')['family_id']]
    # One family, two submissions: the family's own candidate once, and each member's own records.
    assert counts(families, zorbex) == {'submission_identifier': 1, 'family_name_and_organisation': 1,
                                        'family_name_only': 0, 'parent_submission': 0, 'set_aside_by_rule': 0, 'other': 1}
    n_records = load(build, 'summary.json')['denominators']['evidence_records']['n']
    assert sum(sum(f['evidence_counts']) for f in families['families'].values()) == n_records


def test_document_counts_are_the_submissions_own_held_documents(exported: dict[str, Any]) -> None:
    assert row(exported, 'DEN990005')['documents'] == {'n_held': 2, 'n_population_type_readable': 1}
    assert row(exported, 'P990006')['documents'] == {'n_held': 3, 'n_population_type_readable': 0}    # the SSED is a scan
    # The supplement holds its parent's document only, which is not its own.
    assert row(exported, 'P990007/S002')['documents'] == {'n_held': 0, 'n_population_type_readable': 0}


def test_families_carry_name_reason_status_and_members(build: Path, exported: dict[str, Any]) -> None:
    families = exported['families.json']['families']
    built = {f['family_id']: f for f in load(build, 'families.json')['families']}
    assert set(families) == set(built)
    for fid, f in families.items():
        assert (f['family_name'], f['grouping_reason'], f['status'], f['submission_numbers']) == \
            (built[fid]['family_name'], built[fid]['grouping_reason'], built[fid]['status'], built[fid]['submission_numbers'])
    assert {f['status'] for f in families.values()} == {'candidate'}


# ---------------------------------------------------------------- evidence records

def restore(file: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """An exported record with its reasons and registry version written back in."""
    version = {**(file['registry_version'] or {}), **record['source_version']} \
        if file['registry_version'] and 'registry' not in record['source_version'] else record['source_version']
    return {**record, 'confidence_reasons': [file['reasons'][i] for i in record['confidence_reasons']],
            'validation_reasons': [file['reasons'][i] for i in record['validation_reasons']], 'source_version': version}


def test_records_rejected_by_rule_are_in_their_own_file_and_nowhere_else(build: Path, exported: dict[str, Any]) -> None:
    shown, aside = exported['evidence.json']['records'], exported['evidence_set_aside.json']['records']
    assert {x['automated_validation'] for x in aside} == {'rejected_by_rule'}
    assert 'rejected_by_rule' not in {x['automated_validation'] for x in shown}
    built = load(build, 'links.json')['links']
    assert sorted(x['link_id'] for x in shown + aside) == sorted(x['link_id'] for x in built)
    assert (aside[0]['source']['id'], aside[0]['target']['id']) == ('K990003', 'NCT99000004')


def test_a_record_keeps_every_field_the_build_wrote(build: Path, exported: dict[str, Any]) -> None:
    built = {x['link_id']: x for x in load(build, 'links.json')['links']}
    studies = {s['nct_id']: s for s in load(build, 'studies.json')['studies']}
    for name in ('evidence.json', 'evidence_set_aside.json'):
        file = exported[name]
        for record in file['records']:
            b, r = built[record['link_id']], restore(file, record)
            for field in ('level', 'source', 'proposed_relationship', 'method', 'evidence', 'source_version', 'confidence',
                          'confidence_reasons', 'automated_validation', 'validation_reasons', 'human_review',
                          'reviewed_relationship', 'review'):
                assert r[field] == b[field], (record['link_id'], field)
            assert r['target'] == {'id': b['target']['id']}
            assert {k: v for k, v in r['timing'].items() if k != 'by_submission'} == \
                {k: v for k, v in b['timing'].items() if k != 'by_submission'}
            assert r['timing']['by_submission'] == [
                {'submission_number': t['submission_number'], 'fda_decision_date': t['fda_decision_date'],
                 'relation': t['relation']} for t in b['timing']['by_submission']]
            s = studies[b['target']['id']]
            assert r['study'] == {'brief_title': s.get('brief_title'), 'lead_sponsor': s.get('lead_sponsor'),
                                  'overall_status': s.get('overall_status'), 'registry_state': s['registry_state']}
            assert links.problems({**b, **{k: r[k] for k in ('confidence_reasons', 'validation_reasons')}}) == []


def test_the_exported_record_for_a_cited_study_reads_as_the_build_found_it(exported: dict[str, Any]) -> None:
    file = exported['evidence.json']
    (x,) = [restore(file, r) for r in file['records'] if r['source']['id'] == 'K990001']
    assert (x['level'], x['method'], x['automated_validation']) == ('submission', 'nct_id_in_submission_document', 'identifier_confirmed')
    assert 'NCT99000001' in x['evidence']['quote'] and x['evidence']['location'] == {'page': 3}
    assert x['study'] == {'brief_title': 'Reader study of an imaging aid', 'lead_sponsor': 'Fictional Imaging Inc.',
                          'overall_status': 'COMPLETED', 'registry_state': 'resolves'}
    assert x['timing']['by_submission'] == [{'submission_number': 'K990001', 'fda_decision_date': '2021-03-02',
                                             'relation': 'primary_completion_on_or_before_decision'}]
    # The one review of the fictional build is carried as the build applied it.
    assert (x['human_review'], x['reviewed_relationship']) == ('accepted', 'explicitly_cited_supporting_study')
    assert x['review'] == {'reviewed_on': '2026-11-13', 'note': site_fixture.REVIEW_NOTE}
    assert file['registry_version'] == {'registry': 'ClinicalTrials.gov', 'registry_api_version': '2.0.5',
                                        'registry_data_timestamp': '2026-11-13T10:00:06'}


def test_a_study_the_registry_does_not_hold_has_absent_facts_not_empty_ones(exported: dict[str, Any]) -> None:
    (x,) = [r for r in exported['evidence.json']['records'] if r['automated_validation'] == 'identifier_not_resolved']
    assert x['study'] == {'brief_title': None, 'lead_sponsor': None, 'overall_status': None, 'registry_state': 'not_found'}


def test_a_record_with_another_registry_version_keeps_its_own(build: Path) -> None:
    built = load(build, 'links.json')['links']
    built[0]['source_version'] = {**built[0]['source_version'], 'registry': 'ClinicalTrials.gov',
                                  'registry_api_version': '9', 'registry_data_timestamp': 'later'}
    file = site.evidence_file(built, {})
    assert file['registry_version'] is None            # two versions among the records: none is shared
    assert file['records'][0]['source_version']['registry_api_version'] == '9'
    assert all('registry_data_timestamp' in r['source_version'] for r in file['records'])


# ---------------------------------------------------------------- documents

def test_documents_are_listed_per_submission_with_hash_text_status_and_candidacy(exported: dict[str, Any]) -> None:
    docs = exported['documents.json']['documents']
    letter, summary = sorted((d for d in docs['DEN990005'] if d['availability'] == 'retrieved'),
                             key=lambda d: d['document_type'] != 'de_novo_granting_letter')
    assert letter['category'] == 'granting_letter' and letter['demographic_summary_candidate'] is False
    assert 'granting letter' in letter['demographic_summary_reason']
    assert summary['demographic_summary_candidate'] is True and summary['text_status'] == 'text'
    for d in (letter, summary):
        assert len(d['sha256']) == 64 and d['bytes'] and d['retrieved_at'] and d['n_pages'] >= 1
        assert d['url'].startswith('https://www.accessdata.fda.gov/') and '?' not in d['url']
    # The supplement's own addresses answered 404; the one document held is its parent's.
    assert {d['scope'] for d in docs['P990007/S002'] if d['availability'] == 'retrieved'} == {'parent_submission'}
    missing = [d for ds in docs.values() for d in ds if d['availability'] != 'retrieved']
    assert missing and all(d['sha256'] is None and d['text_status'] is None and d['document_type'] is None for d in missing)


def test_what_stays_with_the_build_is_not_exported(exported: dict[str, Any]) -> None:
    text = ''.join((exported['out'] / n).read_text(encoding='utf-8') for n in list(site.SITE_FILES) + ['run.json'])
    assert 'fictional-model' not in text and 'model_id' not in text          # demographics.json holds model outputs
    assert 'Data/FDA Summary Statements' not in text                         # local paths of the inventory
    assert '"study_language":' not in text and '"name_forms":' not in text and '"classification":' not in text


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize('name', ['links.json', 'summary.json', 'coverage.json'])
def test_a_build_whose_bytes_differ_from_its_manifest_is_refused_and_the_output_is_left_alone(
        build: Path, tmp_path: Path, name: str) -> None:
    out = tmp_path / 'site' / 'devices'
    site.export(build, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    altered = copy_build(build, tmp_path, lambda obj: obj.update(edited=True), name=name, rehash=False)
    with pytest.raises(ValueError, match='do not match the sha256'):
        site.export(altered, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert sorted(p.name for p in out.parent.iterdir()) == ['devices']       # no staging directory left behind


def test_a_missing_file_another_runs_file_and_another_schema_are_refused(build: Path, tmp_path: Path) -> None:
    gone = copy_build(build, tmp_path / 'gone')
    (gone / 'links.json').unlink()
    with pytest.raises(ValueError, match='missing from the build'):
        site.export(gone, tmp_path / 'out' / 'devices')
    mixed = copy_build(build, tmp_path / 'mixed', lambda obj: obj.update(run_id='devices-another-run'), name='families.json')
    with pytest.raises(ValueError, match='belongs to another run'):
        site.export(mixed, tmp_path / 'out' / 'devices')
    newer = copy_build(build, tmp_path / 'newer', lambda run: run.update(schema_version=2), name='run.json', rehash=False)
    with pytest.raises(ValueError, match='not one this export reads'):
        site.export(newer, tmp_path / 'out' / 'devices')
    with pytest.raises(ValueError, match='no run.json'):
        site.export(tmp_path, tmp_path / 'out' / 'devices')
    assert not (tmp_path / 'out' / 'devices').exists()


def test_a_tally_that_disagrees_with_the_builds_own_count_stops_the_export(build: Path, tmp_path: Path) -> None:
    def one_more(obj: dict[str, Any]) -> None:
        obj['submissions'][0]['n_evidence_records'] += 1
    with pytest.raises(ValueError, match="differ from the build's counts"):
        site.export(copy_build(build, tmp_path / 'a', one_more, name='submissions.json'), tmp_path / 'out' / 'devices')

    def fewer_confirmed(obj: dict[str, Any]) -> None:
        next(m for m in obj['metrics'] if m['id'] == 'evidence_records_identifier_confirmed')['numerator'] = 1
    with pytest.raises(ValueError, match='submission_identifier: 2 records'):
        site.export(copy_build(build, tmp_path / 'b', fewer_confirmed), tmp_path / 'out' / 'devices')

    def no_documents(obj: dict[str, Any]) -> None:
        next(m for m in obj['metrics'] if m['id'] == 'submissions_with_document')['numerator'] = 0
    with pytest.raises(ValueError, match='do not reproduce summary.json'):
        site.export(copy_build(build, tmp_path / 'c', no_documents), tmp_path / 'out' / 'devices')
    assert not (tmp_path / 'out').exists() or not list((tmp_path / 'out').iterdir())


def test_a_directory_holding_anything_else_is_never_replaced(build: Path, tmp_path: Path) -> None:
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'demographics.part1.json.gz').write_bytes(b'another dataset')
    with pytest.raises(ValueError, match='refusing to replace it'):
        site.export(build, data)
    assert [p.name for p in data.iterdir()] == ['demographics.part1.json.gz']


def test_a_failure_while_writing_leaves_the_previous_export_in_place(
        build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / 'site' / 'devices'
    site.export(build, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    real, written = site.dump, []

    def failing(obj: Any) -> bytes:
        written.append(1)
        if len(written) == 3:
            raise OSError('disk full')
        return real(obj)
    monkeypatch.setattr(site, 'dump', failing)
    with pytest.raises(OSError, match='disk full'):
        site.export(build, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert sorted(p.name for p in out.parent.iterdir()) == ['devices']


def test_the_command_reports_sizes_and_exits_1_on_a_refusal(
        build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / 'site' / 'devices'
    assert site.main([str(build), str(out)]) == 0
    printed = capsys.readouterr().out
    assert 'submissions.json' in printed and 'total; wrote' in printed
    altered = copy_build(build, tmp_path, lambda obj: obj.update(edited=True), name='links.json', rehash=False)
    assert site.main([str(altered), str(out)]) == 1
    assert 'REFUSED' in capsys.readouterr().err


@pytest.mark.parametrize('link, kind', [
    ({'automated_validation': 'rejected_by_rule', 'level': 'family', 'method': 'device_name_match',
      'source': {'entity_type': 'family'}}, 'set_aside_by_rule'),
    ({'automated_validation': 'rejected_by_rule', 'level': 'submission', 'method': 'submission_number_in_registry_record',
      'source': {'entity_type': 'submission'}}, 'set_aside_by_rule'),
    ({'automated_validation': 'identifier_confirmed', 'level': 'submission', 'method': 'nct_id_in_submission_document',
      'source': {'entity_type': 'submission'}}, 'submission_identifier'),
    ({'automated_validation': 'not_validated', 'level': 'family', 'method': 'device_name_and_organisation_match',
      'source': {'entity_type': 'family'}}, 'family_name_and_organisation'),
    ({'automated_validation': 'not_validated', 'level': 'family', 'method': 'device_name_match',
      'source': {'entity_type': 'family'}}, 'family_name_only'),
    ({'automated_validation': 'not_validated', 'level': 'family', 'method': 'nct_id_in_parent_submission_document',
      'source': {'entity_type': 'submission'}}, 'parent_submission'),
    ({'automated_validation': 'not_validated', 'level': 'family', 'method': 'submission_number_in_registry_record',
      'source': {'entity_type': 'submission'}}, 'parent_submission'),
    ({'automated_validation': 'identifier_not_resolved', 'level': 'submission', 'method': 'nct_id_in_submission_document',
      'source': {'entity_type': 'submission'}}, 'other'),
    ({'automated_validation': 'quote_confirmed_in_source', 'level': 'submission', 'method': 'documentary_statement',
      'source': {'entity_type': 'submission'}}, 'other'),
])
def test_kind_of_reads_the_fields_the_build_set(link: dict[str, Any], kind: str) -> None:
    assert site.kind_of(link) == kind
