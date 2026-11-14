"""Results structure: the artifact family, its producer-side check, the acquisition archive and
the build script. Fictional records only (tests/outcomes/rs_fixture.py); no network."""
from __future__ import annotations

import copy
import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'scripts'))

import rs_fixture as F  # noqa: E402
from outcomes import results_structure as RS  # noqa: E402
from outcomes import results_structure_build as B  # noqa: E402

import build_results_structure  # noqa: E402
import build_results_structure_fixture  # noqa: E402

GOOD = 'NCT99000001,sex,sg-0123456789ab,accepted,Fictional Reviewer,2026-11-14,fictional note'


# ---------------------------------------------------------------- the artifact family

@pytest.fixture(scope='module')
def family() -> dict[str, Any]:
    return json.loads(json.dumps(build_results_structure_fixture.family()))


def test_the_fixture_family_passes_the_check_and_holds_one_record_per_requested_id(family: dict[str, Any]) -> None:
    assert B.check(family) == []
    assert list(family['records.json']['records']) == F.REQUESTED
    assert 'NCT99000009' not in family['records.json']['records'], 'excluded from the build, so no record'
    assert family['run.json']['scope']['excluded'] == {
        'n': 1, 'reason': F.EXCLUDE_REASON, 'nct_ids': ['NCT99000009'],
        'definition': family['run.json']['scope']['excluded']['definition']}
    assert [(r['id'], r['n']) for r in family['summary.json']['scope']['rows']] == [('frame', 8), ('included', 7), ('excluded', 1)]
    for name in ('summary.json', 'records.json'):
        assert family[name]['run_id'] == family['run.json']['run_id']
        assert family[name]['method_version'] == RS.METHOD_VERSION == 'results-structure-1'
        assert B.sha256(B.dump(family[name])) == family['run.json']['files'][name]
    assert all(r['method_version'] == RS.METHOD_VERSION for r in family['records.json']['records'].values())


def test_the_fixture_covers_every_subgroup_state(family: dict[str, Any]) -> None:
    by = {n: r['subgroup'] for n, r in family['records.json']['records'].items()}
    assert by['NCT99000001']['by_dimension'] == {'age': RS.NONE_DETECTED, 'sex': RS.DETECTED, 'race': RS.NONE_DETECTED,
                                                 'ethnicity': RS.NONE_DETECTED}
    assert by['NCT99000002']['state'] == RS.NOT_APPLICABLE
    assert by['NCT99000003']['state'] == RS.NOT_YET_PROCESSED
    assert by['NCT99000005']['state'] == RS.FAILED_OR_INDETERMINATE      # parse failure
    assert by['NCT99000006']['state'] == RS.NONE_DETECTED
    assert by['NCT99000007']['state'] == RS.FAILED_OR_INDETERMINATE      # an unreviewed signal
    seen = {s for sg in by.values() for s in (sg['state'], *sg['by_dimension'].values())}
    assert seen == set(RS.ALL_SUBGROUP_STATES)


def test_the_manifest_carries_provenance_and_the_field_lists(family: dict[str, Any]) -> None:
    run = family['run.json']
    assert B.RUN_ID.match(run['run_id'])
    for key in ('generated_at', 'producer', 'method', 'acquisition', 'retrieval_window', 'registry', 'parameters',
                'files', 'fields_inspected', 'fields_not_inspected', 'limitations', 'reviews'):
        assert run[key], key
    assert run['method']['version'] == 'results-structure-1'
    assert run['fields_inspected'] == list(RS.FIELDS_INSPECTED) and len(run['fields_inspected']) == 18
    assert run['fields_not_inspected'] == list(RS.FIELDS_NOT_INSPECTED)
    assert run['registry'] == {'api_version': 'fictional', 'data_timestamp': '2026-11-14T10:00:00'}
    assert len(run['acquisition']['sha256_uncompressed']) == 64
    assert run['document_and_publication_review'] == 'not_reviewed'
    text = ' '.join(run['limitations'])
    for phrase in ('subgroup reporting detected by the current method', 'Outcome switching is not assessed',
                   'No result is inferred for a subgroup'):
        assert phrase in text, phrase


def test_every_count_names_its_denominator_and_definition(family: dict[str, Any]) -> None:
    summary = family['summary.json']
    den = summary['denominators']
    assert {k: v['n'] for k, v in den.items()} == {'requested': 7, 'retrieved': 6, 'posted': 4, 'C1': 2, 'C2': 1}
    assert all(v['definition'] for v in den.values())
    got = {m['id']: (m['numerator'], m['denominator']) for m in summary['metrics']}
    assert got == {
        'results_posted': (4, 'retrieved'), 'present_baseline': (4, 'posted'), 'present_participant_flow': (4, 'posted'),
        'present_outcome_measures': (4, 'posted'), 'present_adverse_events': (4, 'posted'),
        'usable_baseline': (3, 'posted'), 'usable_participant_flow': (3, 'posted'), 'usable_primary_outcome': (2, 'posted'),
        'usable_adverse_events': (3, 'posted'), 'usable_all_four': (1, 'posted'), 'C1': (2, 'posted'), 'C2': (1, 'posted'),
        'between_arm_analysis': (1, 'C2'), 'between_arm_p_value': (1, 'C2'), 'between_arm_estimate_both_limits': (1, 'C2'),
        'between_arm_p_value_bound': (1, 'C2'), 'subgroup_signal': (2, 'posted')}
    assert all(m['definition'] and m['label'] and m['denominator'] in den for m in summary['metrics'])
    for row in summary['tables']['missing_data']['rows'] + summary['tables']['negative_findings']['rows']:
        assert row['denominator'] in den and row['definition'] and isinstance(row['n'], int)
    assert all(row['scope'] for row in summary['tables']['negative_findings']['rows'])


def test_failures_missing_data_and_negative_findings_are_separate_tables(family: dict[str, Any]) -> None:
    tables = family['summary.json']['tables']
    assert set(tables) == {'retrieval_failures', 'parsing_failures', 'missing_data', 'negative_findings'}
    assert tables['retrieval_failures']['rows'] == [{'nct_id': 'NCT99000003', 'reason': 'not returned by the registry for filter.ids'}]
    assert (tables['retrieval_failures']['n'], tables['retrieval_failures']['denominator']) == (1, 'requested')
    assert (tables['parsing_failures']['n'], tables['parsing_failures']['denominator']) == (1, 'retrieved')
    assert len(tables['parsing_failures']['rows']) > 1, 'one trial, several sections: n counts trials'
    assert {(r['nct_id'], r['section']) for r in tables['parsing_failures']['rows']} >= {('NCT99000005', 'subgroup')}
    missing = {r['id']: r['n'] for r in tables['missing_data']['rows']}
    assert missing['no_posted_results'] == 2
    assert missing['adverse_events_not_usable:zero_at_risk_in_every_group'] == 1
    assert missing['outcome_measures_not_usable:no_data_table'] == 1
    assert 'outcome_measures_not_usable:not_evaluated_parse_failure' not in missing, 'not evaluated is not "not usable"'
    negative = {r['id']: r['n'] for r in tables['negative_findings']['rows']}
    assert negative['no_subgroup_signal'] == 1, 'the parse failure is not counted as a negative finding'
    assert negative['no_between_arm_analysis'] == 0


def test_subgroup_state_counts_list_all_five_values_and_sum_to_the_requested_trials(family: dict[str, Any]) -> None:
    states_block = family['summary.json']['subgroup_states']
    assert states_block['by_dimension']['sex'] == {
        RS.NOT_YET_PROCESSED: 1, RS.FAILED_OR_INDETERMINATE: 2, RS.NONE_DETECTED: 1, RS.DETECTED: 1, RS.NOT_APPLICABLE: 2}
    for d in RS.DIMENSIONS:
        assert list(states_block['by_dimension'][d]) == list(RS.ALL_SUBGROUP_STATES)
        assert sum(states_block['by_dimension'][d].values()) == 7
    assert set(states_block['definitions']) == set(RS.ALL_SUBGROUP_STATES)
    assert states_block['document_and_publication_review'] == {'not_reviewed': 7}
    assert family['summary.json']['signals']['n_signals'] == 3
    assert family['summary.json']['signals']['by_dimension'] == {'age': 1, 'sex': 1, 'race': 0, 'ethnicity': 0, 'other': 1}


def test_the_check_refuses_a_changed_file_a_dropped_state_and_an_unreviewed_detection(family: dict[str, Any]) -> None:
    changed = copy.deepcopy(family)
    changed['summary.json']['metrics'][0]['numerator'] += 1
    assert any('sha256' in p for p in B.check(changed))
    detected = copy.deepcopy(family)
    rec = detected['records.json']['records']['NCT99000006']
    rec['subgroup']['state'] = RS.DETECTED
    assert any('detected state without an accepted human review' in p for p in B.check(detected))
    vocab = copy.deepcopy(family)
    vocab['records.json']['records']['NCT99000006']['subgroup']['by_dimension']['age'] = 'none'
    assert any('outside the vocabulary' in p for p in B.check(vocab))
    leaked = copy.deepcopy(family)
    leaked['records.json']['records']['NCT99000001']['subgroup']['signals'][0]['review'][0]['reviewer'] = 'A Person'
    assert any('review-file keys' in p for p in B.check(leaked))
    silent = copy.deepcopy(family)
    del silent['records.json']['records']['NCT99000003']
    assert any('record count differs' in p for p in B.check(silent))
    unstated = copy.deepcopy(family)
    unstated['run.json']['scope']['excluded']['reason'] = None
    assert any('stated reason' in p for p in B.check(unstated))
    restated = copy.deepcopy(family)
    restated['summary.json']['scope']['rows'][0]['n'] = 7
    assert any('summary.json scope is not what' in p or 'sha256' in p for p in B.check(restated))
    noscope = copy.deepcopy(family)
    del noscope['run.json']['scope']
    assert any('no scope' in p for p in B.check(noscope))


def test_committed_fixture_is_current(family: dict[str, Any]) -> None:
    """tests/fixtures/results_structure/ is what the site's reader tests copy; regenerate it with
    scripts/build_results_structure_fixture.py when the records or the rules change."""
    assert B.load_dir(ROOT / 'tests' / 'fixtures' / 'results_structure') == family


# ---------------------------------------------------------------- acquisition and the CLI

class Response:
    def __init__(self, body: Any, status: int = 200) -> None:
        self.status_code, self.content = status, json.dumps(body).encode()

    def json(self) -> Any:
        return json.loads(self.content)


def test_fetch_asks_for_full_records_in_batches_and_reports_what_did_not_come_back() -> None:
    by_id = {RS.nct_id_of(s): s for s in F.studies()}
    calls: list[tuple[str, dict[str, Any]]] = []

    def get(url: str, params: dict[str, Any]) -> Response:
        calls.append((url, dict(params)))
        if url.endswith('/version'):
            return Response({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-13T10:00:06'})
        return Response({'studies': [by_id[n] for n in params['filter.ids'].split(',') if n in by_id]})

    waits: list[float] = []
    lines, rec = B.fetch(F.FRAME, get=get, sleep=waits.append)
    assert sorted(lines) == sorted(by_id)
    assert 'fields' not in calls[1][1], 'no fields parameter: the full record, results section included'
    assert rec['not_retrieved'] == [{'nct_id': 'NCT99000003', 'reason': 'not returned by the registry for filter.ids'}]
    assert (rec['n_requested'], rec['n_records'], rec['n_requests'], rec['n_failed_requests']) == (8, 7, 2, 0)
    assert (rec['api_version'], rec['data_timestamp']) == ('2.0.5', '2026-11-13T10:00:06')
    assert rec['min_seconds_between_requests'] >= 0.5 and any(w > 0 for w in waits), 'requests are spaced'
    assert all({'requested_at', 'status', 'bytes', 'sha256'} <= set(e) for e in rec['requests'])


def test_a_failed_batch_is_retried_then_recorded_never_dropped() -> None:
    def get(url: str, params: dict[str, Any]) -> Response:
        return Response({}, 200) if url.endswith('/version') else Response({'error': 'unavailable'}, 503)

    lines, rec = B.fetch(F.REQUESTED[:2], get=get, sleep=lambda s: None)
    assert lines == {}
    assert rec['n_failed_requests'] == B.MAX_TRIES and rec['n_retries'] == B.MAX_TRIES - 1
    assert rec['not_retrieved'] == [{'nct_id': n, 'reason': 'request failed: HTTP 503'} for n in F.REQUESTED[:2]]
    assert rec['api_version'] is None


def test_the_archive_is_byte_stable_and_refuses_changed_bytes(tmp_path: Path) -> None:
    from outcomes import acquire
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    record_meta = {'retrieved_at': F.GENERATED_AT, 'n_records': len(lines), 'n_requested': 7}
    a, b = tmp_path / 'a.jsonl.gz', tmp_path / 'b.jsonl.gz'
    meta = B.write_archive(lines, a, record_meta, F.REQUESTED)
    B.write_archive(dict(reversed(list(lines.items()))), b, record_meta, F.REQUESTED)
    assert a.read_bytes() == b.read_bytes(), 'the same records give the same bytes, whatever the order fetched'
    assert B.read_archive_meta(a)['sha256_uncompressed'] == meta['sha256_uncompressed']
    assert [RS.nct_id_of(s) for s in B.iter_archive(a)] == sorted(lines)
    a.write_bytes(gzip.compress(gzip.decompress(a.read_bytes()).replace(b'zorbatinib', b'zorbatinob'), mtime=0))
    with pytest.raises(B.BuildError, match='do not match the recorded sha256'):
        B.read_archive_meta(a)
    (tmp_path / 'b.jsonl.gz.meta.json').unlink()
    with pytest.raises(B.BuildError, match='no metadata file'):
        B.read_archive_meta(b)


def archive(tmp_path: Path) -> Path:
    from outcomes import acquire
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    path = tmp_path / 'records.jsonl.gz'
    B.write_archive(lines, path, {'source': 'fictional', 'retrieved_at': F.GENERATED_AT, 'retrieved_from': F.GENERATED_AT,
                                  'retrieved_to': F.GENERATED_AT, 'n_records': len(lines), 'n_requested': len(F.FRAME),
                                  'not_retrieved': [{'nct_id': 'NCT99000003', 'reason': 'not returned by the registry for filter.ids'}]},
                    F.FRAME)
    return path


def id_file(tmp_path: Path, name: str, ids: list[str]) -> Path:
    path = tmp_path / name
    path.write_text('\n'.join(ids) + '\n')
    return path


def test_cli_builds_from_an_archive_without_network_and_with_the_shipped_empty_review_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, 'fetch', lambda *a, **k: pytest.fail('network used'))
    out = tmp_path / 'out'
    assert build_results_structure.main(['--archive', str(archive(tmp_path)), '--out', str(out)]) == 0
    files = B.load_dir(out)
    assert B.check(files) == []
    assert sorted(p.name for p in out.iterdir()) == ['records.json', 'run.json', 'summary.json']
    assert files['run.json']['reviews']['n_rows'] == 0
    states_block = files['summary.json']['subgroup_states']
    assert all(states_block['by_dimension'][d][RS.DETECTED] == 0 for d in RS.DIMENSIONS), 'no review rows, no detected trial'
    assert states_block['by_dimension']['sex'][RS.FAILED_OR_INDETERMINATE] == 3     # 01 unreviewed, 05 parse, 07 keyword
    assert files['records.json']['records']['NCT99000003']['retrieval'] == {
        'status': 'failed', 'reason': 'not returned by the registry for filter.ids'}
    assert files['run.json']['acquisition']['sha256_uncompressed'] == json.loads(
        (tmp_path / 'records.jsonl.gz.meta.json').read_text())['sha256_uncompressed']
    # No lists given: the build covers the whole frame and excludes nothing.
    scope = files['run.json']['scope']
    assert (scope['frame']['n'], scope['included']['n'], scope['excluded']) == (8, 8, {
        'n': 0, 'reason': None, 'nct_ids': [], 'definition': scope['excluded']['definition']})
    assert 'NCT99000009' in files['records.json']['records']


REASON = "excluded by the outcomes cohort's eligibility rules (see data/outcomes/index.json excluded)"


def test_cli_builds_a_cohort_from_the_frame_archive_and_states_what_it_leaves_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, 'fetch', lambda *a, **k: pytest.fail('network used'))
    arch = archive(tmp_path)
    whole, cohort = tmp_path / 'whole', tmp_path / 'cohort'
    assert build_results_structure.main(['--archive', str(arch), '--out', str(whole)]) == 0
    assert build_results_structure.main([
        '--archive', str(arch), '--nct-list', str(id_file(tmp_path, 'eligible.txt', F.REQUESTED)),
        '--exclude-list', str(id_file(tmp_path, 'excluded.txt', F.EXCLUDED)), '--exclude-reason', REASON,
        '--out', str(cohort)]) == 0
    a, b = B.load_dir(whole), B.load_dir(cohort)
    assert B.check(b) == []
    assert b['run.json']['scope'] == {
        'frame': {'n': 8, 'definition': b['run.json']['scope']['frame']['definition']},
        'included': {'n': 7, 'definition': b['run.json']['scope']['included']['definition']},
        'excluded': {'n': 1, 'reason': REASON, 'nct_ids': ['NCT99000009'],
                     'definition': b['run.json']['scope']['excluded']['definition']}}
    assert [(r['id'], r['n']) for r in b['summary.json']['scope']['rows']] == [('frame', 8), ('included', 7), ('excluded', 1)]
    assert b['summary.json']['scope']['rows'][2]['reason'] == REASON
    assert list(b['records.json']['records']) == F.REQUESTED, 'the excluded record has no record'
    assert b['summary.json']['denominators']['requested']['n'] == 7
    assert b['summary.json']['tables']['retrieval_failures']['unrequested_records'] == [], 'excluded is not "unrequested"'
    # The same archive, unchanged; a different cohort is a different run.
    assert b['run.json']['acquisition']['sha256_uncompressed'] == a['run.json']['acquisition']['sha256_uncompressed']
    assert b['run.json']['acquisition']['n_requested'] == 8
    assert b['run.json']['run_id'] != a['run.json']['run_id']
    assert b['run.json']['parameters']['exclude_reason'] == REASON and b['run.json']['parameters']['exclude_list'] == 'excluded.txt'
    # The included trials' records are the ones the whole-frame build gives.
    for n in F.REQUESTED:
        assert b['records.json']['records'][n] == a['records.json']['records'][n], n


def test_an_eligible_trial_missing_from_the_archive_stays_not_yet_processed_with_a_reason(tmp_path: Path) -> None:
    arch = archive(tmp_path)
    out = tmp_path / 'out'
    # NCT99000003 was asked for and not returned; NCT99000008 was never in the frame at all.
    eligible = sorted([*F.REQUESTED, 'NCT99000008'])
    assert build_results_structure.main([
        '--archive', str(arch), '--nct-list', str(id_file(tmp_path, 'eligible.txt', eligible)),
        '--exclude-list', str(id_file(tmp_path, 'excluded.txt', F.EXCLUDED)), '--exclude-reason', REASON,
        '--out', str(out)]) == 0
    files = B.load_dir(out)
    recs = files['records.json']['records']
    for n, reason in (('NCT99000003', 'not returned by the registry for filter.ids'), ('NCT99000008', 'not in the archive')):
        assert recs[n]['results_status'] == 'not_retrieved'
        assert recs[n]['retrieval'] == {'status': 'failed', 'reason': reason}
        assert recs[n]['subgroup']['state'] == RS.NOT_YET_PROCESSED
        assert set(recs[n]['subgroup']['by_dimension'].values()) == {RS.NOT_YET_PROCESSED}
    table = files['summary.json']['tables']['retrieval_failures']
    assert (table['n'], [r['nct_id'] for r in table['rows']]) == (2, ['NCT99000003', 'NCT99000008'])
    assert files['summary.json']['subgroup_states']['by_dimension']['race'][RS.NOT_YET_PROCESSED] == 2
    assert files['summary.json']['subgroup_states']['by_dimension']['race'][RS.NONE_DETECTED] == 2   # 06, and 01 unreviewed has no race signal
    assert files['run.json']['scope']['included']['n'] == 8 and files['run.json']['scope']['frame']['n'] == 8


@pytest.mark.parametrize('requested, excluded, reason, message', [
    (F.REQUESTED, [], REASON, '1 frame records are neither requested nor excluded'),      # 09 dropped with no reason
    (F.REQUESTED[:-1], F.EXCLUDED, REASON, 'neither requested nor excluded'),
    (F.REQUESTED, F.EXCLUDED, '  ', 'needs a stated reason'),
    (F.FRAME, F.EXCLUDED, REASON, 'both requested and excluded'),
    (F.REQUESTED, [*F.EXCLUDED, 'NCT99000010'], REASON, 'not in the frame'),
])
def test_no_frame_record_leaves_the_build_without_a_stated_reason(requested: list[str], excluded: list[str], reason: str, message: str) -> None:
    with pytest.raises(B.BuildError, match=message):
        B.build_scope(F.FRAME, requested, excluded, reason)


def test_cli_refuses_an_exclude_list_that_does_not_account_for_the_frame(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    arch = archive(tmp_path)
    out = tmp_path / 'out'
    assert build_results_structure.main([
        '--archive', str(arch), '--nct-list', str(id_file(tmp_path, 'eligible.txt', F.REQUESTED[:-1])),
        '--exclude-list', str(id_file(tmp_path, 'excluded.txt', F.EXCLUDED)), '--exclude-reason', REASON,
        '--out', str(out)]) == 1
    assert 'neither requested nor excluded' in capsys.readouterr().err
    assert not out.exists()
    for argv in (['--archive', str(arch), '--exclude-list', str(tmp_path / 'excluded.txt'), '--exclude-reason', REASON],
                 ['--archive', str(arch), '--nct-list', str(tmp_path / 'eligible.txt'), '--exclude-list', str(tmp_path / 'excluded.txt')]):
        with pytest.raises(SystemExit):
            build_results_structure.main([*argv, '--out', str(out)])


def test_cli_fetch_freezes_the_archive_under_its_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from outcomes import acquire
    monkeypatch.setattr(build_results_structure, 'ROOT', tmp_path)
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    rec = {'source': 'fictional', 'retrieved_at': F.GENERATED_AT, 'retrieved_from': F.GENERATED_AT, 'retrieved_to': F.GENERATED_AT,
           'n_records': len(lines), 'n_requested': 7, 'n_requests': 2, 'n_failed_requests': 0,
           'not_retrieved': [{'nct_id': 'NCT99000003', 'reason': 'not returned by the registry for filter.ids'}]}
    monkeypatch.setattr(B, 'fetch', lambda ids: (dict(lines), dict(rec)))
    ids = tmp_path / 'ids.txt'
    ids.write_text('\n'.join(F.REQUESTED) + '\n')
    for _ in range(2):
        assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--out', str(tmp_path / 'o')]) == 0
    archives = list((tmp_path / 'build' / 'results_structure_cache').glob('records-2026-11-14-*.jsonl.gz'))
    assert len(archives) == 1, 'the same records are one archive, named by their hash'
    run = json.loads((tmp_path / 'o' / 'run.json').read_text())
    assert run['parameters']['nct_list'] == 'ids.txt' and run['parameters']['n_requested'] == 7


def test_cli_refuses_a_bad_review_file_before_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, 'fetch', lambda *a, **k: pytest.fail('fetched before the review file was checked'))
    reviews = tmp_path / 'subgroup_reviews.csv'
    reviews.write_text(','.join(RS.REVIEW_COLUMNS) + '\n' + GOOD + '\n' + GOOD + '\n')
    ids = tmp_path / 'ids.txt'
    ids.write_text('NCT99000001\n')
    assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--reviews', str(reviews),
                                         '--out', str(tmp_path / 'o')]) == 1
    assert not (tmp_path / 'o').exists()


def test_an_archive_line_that_is_not_json_is_reported_and_its_trial_is_not_yet_processed() -> None:
    studies: list[Any] = [F.study(1), ValueError('Expecting value: line 1 column 1')]
    records, problems = B.build_records(studies, ['NCT99000001', 'NCT99000002'], {}, {})
    assert problems['unkeyed_lines'] == [{'line': 1, 'reason': 'ValueError: Expecting value: line 1 column 1'}]
    assert records['NCT99000002']['results_status'] == 'not_retrieved'
    assert records['NCT99000002']['subgroup']['state'] == RS.NOT_YET_PROCESSED
    with pytest.raises(B.BuildError, match='more than one record'):
        B.build_records([F.study(1), F.study(1)], ['NCT99000001'], {}, {})
    _, extra = B.build_records([F.study(1), F.study(6)], ['NCT99000001'], {}, {})
    assert extra['unrequested_records'] == ['NCT99000006']


def test_the_nct_list_reader_refuses_repeats_and_other_text(tmp_path: Path) -> None:
    path = tmp_path / 'ids.txt'
    path.write_text('# frame\nNCT99000002\n\nNCT99000001\n')
    assert B.read_nct_list(path) == ['NCT99000001', 'NCT99000002']
    path.write_text('NCT99000001\nNCT99000001\n')
    with pytest.raises(B.BuildError, match='repeated'):
        B.read_nct_list(path)
    path.write_text('nct99000001\n')
    with pytest.raises(B.BuildError, match='not NCT ids'):
        B.read_nct_list(path)
