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


def test_the_check_refuses_inferences_and_rows_that_do_not_point_at_posted_rows(family: dict[str, Any]) -> None:
    def problems(edit: Any) -> list[str]:
        f = copy.deepcopy(family)
        edit(f['records.json']['records']['NCT99000001'])
        return B.check(f)

    def has(found: list[str], text: str) -> bool:
        return any(text in p for p in found)

    # No inference key inside the copied facts or a signal's rows (roadmap, layer B: "No inference").
    def met(r: dict[str, Any]) -> None:
        r['primary_measures'][0]['analyses'][0]['met'] = True
    assert has(problems(met), "an inference key ['met']")

    def favours(r: dict[str, Any]) -> None:
        r['subgroup']['signals'][0]['rows'][0]['favours'] = 'Female'
    assert has(problems(favours), 'inference key')
    # A title-level signal lists its rows, with exactly its field's keys; any other signal has none.
    for rows in (None, []):
        def norows(r: dict[str, Any], rows: Any = rows) -> None:
            r['subgroup']['signals'][0]['rows'] = rows
        assert has(problems(norows), 'a title-level signal has no rows')

    def wrongkeys(r: dict[str, Any]) -> None:
        r['subgroup']['signals'][0]['rows'][0] = {'group_id': 'OG000', 'title': 'Female'}
    assert has(problems(wrongkeys), 'exactly the keys')
    cue = copy.deepcopy(family)
    [kw] = cue['records.json']['records']['NCT99000007']['subgroup']['signals']
    kw['rows'] = [{'class_index': 0, 'title': 'Zorbatinib'}]
    assert has(B.check(cue), 'does not read posted titles carries rows')

    # On a PRIMARY measure the rows must be exactly the ones the rule matches in the copied table:
    # each resolves at its position and title, and none is substituted or left out (Codex, PR 103).
    # The fixture's primary measure is given classes Female, Male and Overall, and groups by sex.
    def on_primary(r: dict[str, Any], rows: list[dict[str, Any]], field: str = 'class_titles') -> None:
        pm = r['primary_measures'][0]
        one = pm['classes'][0]
        pm['classes'] = [{**copy.deepcopy(one), 'title': t} for t in ('Female', 'Male', 'Overall')]
        pm['groups'][0]['title'], pm['groups'][1]['title'] = 'Zorbatinib, women', 'Placebo, men'
        sig = copy.deepcopy(r['subgroup']['signals'][0])   # a sex signal
        sig.update(field=field, measure_type='PRIMARY', measure_index=0, measure_title=pm['title'], rows=rows)
        r['subgroup']['signals'].append(sig)
    unresolved, mismatched = 'does not resolve to the posted table', 'not the ones the rule matches'
    sexes = [{'class_index': 0, 'title': 'Female'}, {'class_index': 1, 'title': 'Male'}]
    matched = problems(lambda r: on_primary(r, sexes))
    assert not has(matched, unresolved) and not has(matched, mismatched)
    assert has(problems(lambda r: on_primary(r, [sexes[0], {'class_index': 1, 'title': 'Female'}])), unresolved)
    assert has(problems(lambda r: on_primary(r, [sexes[0], {'class_index': 4, 'title': 'Male'}])), unresolved)
    assert has(problems(lambda r: on_primary(r, [sexes[0], {'class_index': 2, 'title': 'Overall'}])), mismatched)   # substituted
    assert has(problems(lambda r: on_primary(r, sexes[:1])), mismatched)                                          # one left out
    groups = [{'group_id': 'OG000', 'title': 'Zorbatinib, women'}, {'group_id': 'OG001', 'title': 'Placebo, men'}]
    by_group = problems(lambda r: on_primary(r, groups, 'group_titles'))
    assert not has(by_group, unresolved) and not has(by_group, mismatched)
    assert has(problems(lambda r: on_primary(r, [{'group_id': 'OG009', 'title': 'Zorbatinib, women'}], 'group_titles')), unresolved)
    assert has(problems(lambda r: on_primary(r, groups[:1], 'group_titles')), mismatched)
    assert has(problems(lambda r: on_primary(r, [{'class_index': 0, 'category_index': 3, 'title': 'Female'}],
                                             'category_titles')), unresolved)

    # A position is a non-negative int, never a bool: `false` equals 0 in Python and would otherwise
    # resolve to the first row and pass the comparison (Codex, PR 103).
    bad_position = 'not a non-negative integer'
    for row, field in (({'class_index': False, 'title': 'Female'}, 'class_titles'),
                       ({'class_index': -1, 'title': 'Female'}, 'class_titles'),
                       ({'class_index': 0.0, 'title': 'Female'}, 'class_titles'),
                       ({'class_index': 0, 'category_index': True, 'title': 'Female'}, 'category_titles'),
                       ({'group_id': 0, 'title': 'Zorbatinib, women'}, 'group_titles')):
        assert has(problems(lambda r, row=row, field=field: on_primary(r, [row], field)), bad_position), row
    def bool_index(r: dict[str, Any]) -> None:
        on_primary(r, sexes)
        r['subgroup']['signals'][-1]['measure_index'] = False
    assert has(problems(bool_index), "measure_index is not a non-negative integer")

    def elsewhere(r: dict[str, Any]) -> None:
        on_primary(r, sexes)
        r['subgroup']['signals'][-1]['measure_index'] = 7
    assert has(problems(elsewhere), 'a primary measure this record does not copy')

    # Primary status comes from the copied measure at the signal's index: a signal there whose own type
    # or title disagrees is refused, not let past the row checks (Codex, PR 103).
    stale = "disagrees with the copied primary measure at its index"
    def restated(r: dict[str, Any], **kw: Any) -> None:
        on_primary(r, sexes[:1])   # one row left out: the row checks would refuse it if they ran
        r['subgroup']['signals'][-1].update(kw)
    assert has(problems(lambda r: restated(r, measure_type='SECONDARY')), stale)
    assert has(problems(lambda r: restated(r, measure_type=None)), stale)
    assert has(problems(lambda r: restated(r, measure_title='Another measure')), stale)
    assert not has(problems(lambda r: on_primary(r, sexes)), stale)

    # The rows are recomputed from rule_dimension, so it must be the rule of the published dimension:
    # a sex signal cannot carry the age rule's rows (Codex, PR 103).
    named = "dimension is not the one its level rule names"
    def relabelled(r: dict[str, Any], rule: str, dim: str) -> None:
        on_primary(r, sexes)
        r['subgroup']['signals'][-1].update(rule_dimension=rule, dimension=dim)
    assert has(problems(lambda r: relabelled(r, 'age', 'sex')), named)
    assert has(problems(lambda r: relabelled(r, 'keyword', 'other')), named)
    assert has(problems(lambda r: relabelled(r, 'origin', 'race')), named)
    assert not has(problems(lambda r: relabelled(r, 'sex', 'sex')), named)

    # A primary measure whose copy failed to parse has no table to resolve against; the record
    # keeps the signal and states the failure, and that is not a reason to refuse the family. A
    # record that copies no primary measures and states no failure is refused.
    def copy_failed(r: dict[str, Any], stated: bool) -> None:
        on_primary(r, sexes)
        r['primary_measures'] = None
        r['parse'] = {'status': 'failed', 'failures': [{'section': 'primary_measures', 'reason': "KeyError: 'groupId'"}]
                      if stated else []}
    failed = problems(lambda r: copy_failed(r, True))
    assert not any('NCT99000001 sg-' in p for p in failed), failed
    assert has(problems(lambda r: copy_failed(r, False)), 'neither copies nor records a failure')


def test_the_check_refuses_a_count_or_version_of_the_wrong_json_type(family: dict[str, Any]) -> None:
    # Python's == holds for True == 1 and 1 == 1.0, and isinstance(True, int) holds, so each of
    # these passed the check before: a count or version that equals the right value but is a bool or
    # a float. The fixture has a numerator of 1 (usable_all_four), a denominator of 1 (C2) and one
    # excluded record, so `true` stands exactly where 1 belongs.
    def problems(edit: Any) -> list[str]:
        f = copy.deepcopy(family)
        edit(f)
        return B.check(f)

    def has(found: list[str], text: str) -> bool:
        return any(text in p for p in found)

    def metric(f: dict[str, Any], mid: str) -> dict[str, Any]:
        return next(m for m in f['summary.json']['metrics'] if m['id'] == mid)

    out_of_range = 'unknown denominator, numerator out of range, or no definition'
    def numerator(value: Any) -> Any:
        return lambda f: metric(f, 'usable_all_four').update(numerator=value)
    for wrong in (True, 1.0):
        found = problems(numerator(wrong))
        assert has(found, 'metric usable_all_four: ' + out_of_range), wrong
        assert has(found, 'summary.json metrics is not what records.json gives'), wrong
    assert has(problems(lambda f: f['summary.json']['denominators']['C2'].update(n=True)),
               'summary.json denominators is not what records.json gives')
    assert has(problems(lambda f: f['summary.json']['denominators']['C2'].update(n=True)), 'metric between_arm_analysis: ' + out_of_range)
    assert has(problems(lambda f: f['run.json'].update(schema_version=True)), 'run.json schema_version is not 1')
    assert has(problems(lambda f: f['summary.json'].update(schema_version=1.0)), 'from another run, schema or method version')
    assert has(problems(lambda f: f['run.json'].update(n_records=7.0)), 'record count differs')
    assert has(problems(lambda f: f['run.json']['scope']['excluded'].update(n=True)), 'excluded records need their ids')
    assert has(problems(lambda f: f['run.json']['scope']['included'].update(n=7.0)), 'the included count is not the number of records')

    def state_count(f: dict[str, Any]) -> None:
        counts = f['summary.json']['subgroup_states']['by_dimension']['age']
        k = next(k for k, v in counts.items() if v == 1)
        counts[k] = True
    assert has(problems(state_count), 'subgroup states for age must count all five values')
    # Review counts are recounted too: the fixture applies 2 review rows (Codex, PR 104).
    assert has(problems(lambda f: f['summary.json']['reviews'].update(n_review_rows_applied=2.0)),
               'summary.json reviews is not what records.json gives')
    # Counts in run.json that cannot be recounted must at least be counts (Codex, PR 104).
    not_counts = 'run.json counts that are not non-negative integers'
    for where, key, wrong in (('scope.frame', 'n', True), ('scope.frame', 'n', 8.0), ('acquisition', 'n_retries', False),
                              ('acquisition', 'n_requests', -1), ('parameters', 'n_requested', 7.0), ('parameters', 'batch_size', True),
                              ('reviews', 'n_rows', 2.0)):
        def put(f: dict[str, Any], where: str = where, key: str = key, wrong: Any = wrong) -> None:
            node = f['run.json']
            for part in where.split('.'):
                node = node[part]
            node[key] = wrong
        assert has(problems(put), f"{not_counts}: ['{where}.{key}']"), (where, key, wrong)
    # A fetch count the archive did not record is null, which records the absence and is accepted;
    # the frame count is computed by every build and may not be null.
    assert not has(problems(lambda f: f['run.json']['acquisition'].update(n_retries=None)), not_counts)
    assert has(problems(lambda f: f['run.json']['scope']['frame'].update(n=None)), f"{not_counts}: ['scope.frame.n']")
    # A float that is not a count (seconds between requests) is left alone.
    assert not has(problems(lambda f: f['run.json']['acquisition'].update(min_seconds_between_requests=0.75)), not_counts)
    # The values of the right type still pass.
    assert B.check(copy.deepcopy(family)) == []


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
        return (Response({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-13T10:00:06'}, 200) if url.endswith('/version')
                else Response({'error': 'unavailable'}, 503))

    lines, rec = B.fetch(F.REQUESTED[:2], get=get, sleep=lambda s: None)
    assert lines == {}
    assert rec['n_failed_requests'] == B.MAX_TRIES and rec['n_retries'] == B.MAX_TRIES - 1
    assert rec['not_retrieved'] == [{'nct_id': n, 'reason': 'request failed: HTTP 503'} for n in F.REQUESTED[:2]]
    assert rec['api_version'] == '2.0.5'


@pytest.mark.parametrize('version', [
    Response({'error': 'unavailable'}, 503),                        # the request fails every time
    Response({}, 200),                                              # a body without the two fields
    Response({'apiVersion': '2.0.5'}, 200),                         # one field only
    Response({'apiVersion': '2.0.5', 'dataTimestamp': ''}, 200),    # an empty value
])
def test_a_fetch_without_the_registry_version_is_refused_before_any_study_is_requested(version: Response) -> None:
    asked: list[str] = []

    def get(url: str, params: dict[str, Any]) -> Response:
        asked.append(url)
        return version if url.endswith('/version') else Response({'studies': []}, 200)

    with pytest.raises(B.BuildError, match='registry version request failed'):
        B.fetch(F.REQUESTED[:2], get=get, sleep=lambda s: None)
    assert all(u.endswith('/version') for u in asked), 'no study is requested'


def test_the_archive_is_byte_stable_and_refuses_changed_bytes(tmp_path: Path) -> None:
    from outcomes import acquire
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    record_meta = {'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': F.GENERATED_AT, 'n_records': len(lines), 'n_requested': 7}
    # two copies of one fetch, in two directories: an archive is named by its bytes and its record
    name = B.archive_name(lines, record_meta, F.REQUESTED)
    a, b = tmp_path / 'a' / name, tmp_path / 'b' / name
    meta = B.write_archive(lines, a, record_meta, F.REQUESTED)
    B.write_archive(dict(reversed(list(lines.items()))), b, record_meta, F.REQUESTED)
    assert a.read_bytes() == b.read_bytes(), 'the same records give the same bytes, whatever the order fetched'
    assert B.read_archive_meta(a)['sha256_uncompressed'] == meta['sha256_uncompressed']
    assert [RS.nct_id_of(s) for s in B.iter_archive(a)] == sorted(lines)
    a.write_bytes(gzip.compress(gzip.decompress(a.read_bytes()).replace(b'zorbatinib', b'zorbatinob'), mtime=0))
    with pytest.raises(B.BuildError, match='do not match the recorded sha256'):
        B.read_archive_meta(a)
    # The metadata's list of requested ids decides the frame of an archive-only build, so it is
    # checked against its own digest and count: an edited or left-over metadata file is refused.
    meta_path = b.with_name(b.name + '.meta.json')
    good = json.loads(meta_path.read_text())
    assert B.read_archive_meta(b)['requested_ids'] == sorted(F.REQUESTED)
    for change, message in (
            (lambda m: m['requested_ids'].append('NCT99009999'), 'do not match their recorded sha256'),
            (lambda m: m['requested_ids'].pop(), 'do not match their recorded sha256'),
            (lambda m: m.update(requested_ids_sha256='0' * 64), 'do not match their recorded sha256'),
            (lambda m: m.update(n_requested=6), 'number of requested ids does not match'),
            (lambda m: m['requested_ids'].append(m['requested_ids'][0]), 'each once'),
            (lambda m: m.update(requested_ids=['not an id']), 'each once'),
            (lambda m: m.pop('requested_ids'), 'each once'),
            # the rest of the fetch record is bound to the archive by its name
            (lambda m: m.update(retrieved_at='2026-11-14T23:00:00+00:00'), 'not the record it is named by'),
            (lambda m: m.update(not_retrieved=[{'nct_id': 'NCT99000003', 'reason': 'another reason'}]), 'not the record it is named by'),
            (lambda m: m.update(archive='records-other.jsonl.gz'), 'not the record it is named by')):
        edited = copy.deepcopy(good)
        change(edited)
        meta_path.write_text(json.dumps(edited))
        with pytest.raises(B.BuildError, match=message):
            B.read_archive_meta(b)
    # an archive under a name its record does not give (an older naming, or a renamed file)
    meta_path.write_text(json.dumps(good))
    legacy = b.with_name('records-2026-11-14-0123456789ab.jsonl.gz')
    legacy.write_bytes(b.read_bytes())
    legacy.with_name(legacy.name + '.meta.json').write_text(json.dumps({**good, 'archive': legacy.name}))
    with pytest.raises(B.BuildError, match=f'that record names {name}'):
        B.read_archive_meta(legacy)
    meta_path.unlink()
    with pytest.raises(B.BuildError, match='no metadata file'):
        B.read_archive_meta(b)


def archive(tmp_path: Path) -> Path:
    from outcomes import acquire
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    record = {'source': 'fictional', 'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': F.GENERATED_AT, 'retrieved_from': F.GENERATED_AT,
              'retrieved_to': F.GENERATED_AT, 'n_records': len(lines), 'n_requested': len(F.FRAME),
              'not_retrieved': [{'nct_id': 'NCT99000003', 'reason': 'not returned by the registry for filter.ids'}]}
    path = tmp_path / B.archive_name(lines, record, F.FRAME)
    B.write_archive(lines, path, record, F.FRAME)
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
    (meta_file,) = tmp_path.glob('records-*.jsonl.gz.meta.json')
    assert files['run.json']['acquisition']['sha256_uncompressed'] == json.loads(meta_file.read_text())['sha256_uncompressed']
    assert files['run.json']['acquisition']['archive'] + '.meta.json' == meta_file.name
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
    rec = {'source': 'fictional', 'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': F.GENERATED_AT, 'retrieved_from': F.GENERATED_AT, 'retrieved_to': F.GENERATED_AT,
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


def test_a_frame_record_cannot_be_left_out_without_a_stated_reason() -> None:
    # the archive was fetched for two ids, the build asks for one, and no exclude list says why
    with pytest.raises(B.BuildError, match='not requested and no exclude list states why'):
        B.build_scope(['NCT99000001', 'NCT99000002'], ['NCT99000001'])
    whole = B.build_scope(['NCT99000001', 'NCT99000002'], ['NCT99000002', 'NCT99000001'])
    assert whole['frame']['n'] == 2 and whole['included']['n'] == 2 and whole['excluded']['n'] == 0
    # a requested id outside the frame is still allowed: it gets a not_yet_processed record
    assert B.build_scope(['NCT99000001'], ['NCT99000001', 'NCT99000009'])['included']['n'] == 2
    named = B.build_scope(['NCT99000001', 'NCT99000002'], ['NCT99000001'], ['NCT99000002'], 'outside the cohort')
    assert named['excluded'] == {**named['excluded'], 'n': 1, 'reason': 'outside the cohort', 'nct_ids': ['NCT99000002']}


def test_cli_fetches_for_different_id_lists_never_share_an_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # both fetches return nothing, so their bytes are identical; what was asked for still differs
    monkeypatch.setattr(build_results_structure, 'ROOT', tmp_path)

    def nothing(ids: list[str]) -> tuple[dict[str, bytes], dict[str, object]]:
        return {}, {'source': 'fictional', 'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': F.GENERATED_AT, 'retrieved_from': F.GENERATED_AT,
                    'retrieved_to': F.GENERATED_AT, 'n_records': 0, 'n_requested': len(ids), 'n_requests': 1,
                    'n_failed_requests': 1, 'not_retrieved': [{'nct_id': i, 'reason': 'request failed'} for i in ids]}

    monkeypatch.setattr(B, 'fetch', nothing)
    for name, wanted in (('a', ['NCT99000001']), ('b', ['NCT99000001', 'NCT99000002'])):
        ids = tmp_path / f'{name}.txt'
        ids.write_text('\n'.join(wanted) + '\n')
        assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--out', str(tmp_path / name)]) == 0
        run = json.loads((tmp_path / name / 'run.json').read_text())
        assert run['parameters']['n_requested'] == len(wanted)
        records = json.loads((tmp_path / name / 'records.json').read_text())['records']
        assert sorted(records) == wanted and all(r['results_status'] == 'not_retrieved' for r in records.values())
    metas = sorted((tmp_path / 'build' / 'results_structure_cache').glob('*.meta.json'))
    assert len(metas) == 2 and sorted(json.loads(m.read_text())['n_requested'] for m in metas) == [1, 2]


def test_cli_a_repeated_fetch_keeps_its_own_record_even_when_the_bytes_are_the_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same day, same ids, same returned bytes; the second fetch ran later and failed for another
    reason. Each build publishes the window and the failure reason of the fetch it ran."""
    from outcomes import acquire
    monkeypatch.setattr(build_results_structure, 'ROOT', tmp_path)
    wanted = ['NCT99000001', 'NCT99000002']
    ids = tmp_path / 'ids.txt'
    ids.write_text('\n'.join(wanted) + '\n')
    fetches = {'first': ('2026-11-14T01:00:00+00:00', 'HTTP 500 after 3 attempts'),
               'second': ('2026-11-14T09:00:00+00:00', 'not returned by the registry for filter.ids')}
    for name, (at, reason) in fetches.items():
        rec = {'source': 'fictional', 'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': at, 'retrieved_from': at, 'retrieved_to': at, 'n_records': 1,
               'n_requested': 2, 'n_requests': 1, 'n_failed_requests': 0, 'not_retrieved': [{'nct_id': 'NCT99000002', 'reason': reason}]}
        monkeypatch.setattr(B, 'fetch', lambda _ids, rec=rec: ({'NCT99000001': acquire.encode([F.study(1)])}, dict(rec)))
        assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--out', str(tmp_path / name)]) == 0
        run = json.loads((tmp_path / name / 'run.json').read_text())
        assert run['retrieval_window'] == {'from': at, 'to': at}
        missing = json.loads((tmp_path / name / 'records.json').read_text())['records']['NCT99000002']
        assert missing['results_status'] == 'not_retrieved' and missing['retrieval']['reason'] == reason
    cache = tmp_path / 'build' / 'results_structure_cache'
    archives = sorted(cache.glob('*.jsonl.gz'))
    assert len(archives) == 2 and archives[0].read_bytes() == archives[1].read_bytes(), 'two fetches, two archives, the same bytes'
    assert {json.loads(p.with_name(p.name + '.meta.json').read_text())['retrieved_at'] for p in archives} == {at for at, _ in fetches.values()}
    # a file already under this fetch's name whose metadata is not this fetch's is refused, not published
    meta_path = archives[0].with_name(archives[0].name + '.meta.json')
    edited = json.loads(meta_path.read_text())
    edited['n_retries'] = 9
    meta_path.write_text(json.dumps(edited))
    at = edited['retrieved_at']
    rec = {'source': 'fictional', 'api_version': '2.0.5', 'data_timestamp': '2026-11-13T10:00:06', 'retrieved_at': at, 'retrieved_from': at, 'retrieved_to': at, 'n_records': 1, 'n_requested': 2,
           'n_requests': 1, 'n_failed_requests': 0, 'not_retrieved': edited['not_retrieved']}
    monkeypatch.setattr(B, 'fetch', lambda _ids: ({'NCT99000001': acquire.encode([F.study(1)])}, dict(rec)))
    assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--out', str(tmp_path / 'third')]) != 0
    assert not (tmp_path / 'third').exists()


def test_a_review_that_matches_nothing_is_reported_once_with_one_reason() -> None:
    row = {'decision': 'accepted', 'reviewed_on': '2026-11-14'}
    reviews = {'NCT99000001': {('sg-000000000000', 'sex'): row},      # posted results, but no such signal
               'NCT99000002': {('sg-111111111111', 'age'): row},      # no posted results: nothing to review
               'NCT99000003': {('sg-222222222222', 'age'): row}}      # requested, not in the archive
    studies = [F.study(1), F.study(2, has_results=False)]
    _, problems = B.build_records(studies, ['NCT99000001', 'NCT99000002', 'NCT99000003'], {}, reviews)
    got = sorted((x['nct_id'], x['review'], x['reason']) for x in problems['reviews_not_matched'])
    assert got == [('NCT99000001', 'sg-000000000000/sex', 'no such signal in the current record'),
                   ('NCT99000002', 'sg-111111111111/age', 'the trial has no signals to review in this build'),
                   ('NCT99000003', 'sg-222222222222/age', 'the trial has no signals to review in this build')]


def test_the_archive_encoder_is_among_the_files_whose_uncommitted_changes_are_reported() -> None:
    assert 'outcomes/acquire.py' in build_results_structure.PRODUCER_PATHS
    assert all((ROOT / p).exists() for p in build_results_structure.PRODUCER_PATHS)


def test_two_builds_that_publish_different_content_never_share_a_run_id() -> None:
    """Same archive bytes, same request, same code and reviews; the only difference is why a
    requested trial is missing, which comes from the archive's metadata file and reaches records.json."""
    def family(reason: str, generated_at: str = F.GENERATED_AT) -> dict[str, Any]:
        records, problems = B.build_records([F.study(1)], ['NCT99000001', 'NCT99000002'], {'NCT99000002': reason}, {})
        acq = {'retrieved_at': F.GENERATED_AT, 'sha256_uncompressed': 'a' * 64}
        scope = B.build_scope(['NCT99000001', 'NCT99000002'], ['NCT99000001', 'NCT99000002'])
        producer = {'repository': 'x', 'commit': 'c' * 40, 'uncommitted_changes': False, 'source_sha256': '1' * 64}
        return B.build(records, problems, acq, producer, {'sha256': 'r' * 64}, {}, scope, generated_at=generated_at)
    a, b = family('HTTP 500 after 3 attempts'), family('not returned by the registry for filter.ids')
    assert a['records.json']['records']['NCT99000002'] != b['records.json']['records']['NCT99000002']
    assert a['run.json']['run_id'] != b['run.json']['run_id']
    # the same content gives the same id, whenever it is built
    later = family('HTTP 500 after 3 attempts', generated_at='2030-01-01T00:00:00+00:00')
    assert later['run.json']['run_id'] == a['run.json']['run_id'] and B.check(a) == [] and B.check(b) == []
    # an id that does not follow from the content is refused, even with every checksum consistent
    swapped = json.loads(json.dumps(b))
    for name in ('run.json', 'summary.json', 'records.json'):
        swapped[name]['run_id'] = a['run.json']['run_id']
    swapped['run.json']['files'] = {n: B.sha256(B.dump(swapped[n])) for n in ('records.json', 'summary.json')}
    assert B.check(swapped) == ['run.json run_id does not follow from the content of the family']


def test_a_build_from_changed_code_does_not_share_a_run_id_with_a_clean_one(family: dict[str, Any], tmp_path: Path) -> None:
    def run_id(producer: dict[str, Any]) -> str:
        records, problems = B.build_records([F.study(1)], ['NCT99000001'], {}, {})
        acq = {'retrieved_at': F.GENERATED_AT, 'sha256_uncompressed': 'a' * 64}
        scope = B.build_scope(['NCT99000001'], ['NCT99000001'])
        return B.build(records, problems, acq, producer, {'sha256': 'r' * 64}, {}, scope, generated_at=F.GENERATED_AT)['run.json']['run_id']
    clean = {'repository': 'x', 'commit': 'c' * 40, 'uncommitted_changes': False, 'source_sha256': '1' * 64}
    dirty = {**clean, 'uncommitted_changes': True, 'source_sha256': '2' * 64}
    assert run_id(clean) == run_id(dict(clean)) and run_id(clean) != run_id(dirty)
    # the digest covers every producing file, as it stands on disk
    for rel in build_results_structure.PRODUCER_PATHS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    same = build_results_structure.source_digest(tmp_path)
    assert same == build_results_structure.source_digest()
    (tmp_path / 'outcomes/acquire.py').write_text((ROOT / 'outcomes/acquire.py').read_text() + '\n# an uncommitted change\n')
    assert build_results_structure.source_digest(tmp_path) != same


@pytest.mark.parametrize('run_dir', sorted((ROOT / 'docs' / 'results_structure' / 'runs').iterdir()), ids=lambda d: d.name)
def test_a_committed_run_record_agrees_with_its_manifest(run_dir: Path) -> None:
    """records.json is not committed here, so the README's quoted checksum is what a reader checks a
    downloaded copy against. It, the run id and the producing commit must be the manifest's, and the
    committed summary.json must be the file the manifest lists."""
    run = json.loads((run_dir / 'run.json').read_text())
    summary = json.loads((run_dir / 'summary.json').read_text())
    readme = (run_dir / 'README.md').read_text()
    assert B.sha256(B.dump(summary)) == run['files']['summary.json'] and summary['run_id'] == run['run_id']
    assert f"| Run | `{run['run_id']}`, generated {run['generated_at']} " in readme
    assert f"at engine commit `{run['producer']['commit'][:12]}`" in readme
    assert f"producing-code digest `{run['producer']['source_sha256'][:12]}…`" in readme
    assert f"`records.json` (sha256 `{run['files']['records.json'][:16]}…`" in readme
    assert f"| Archive | `{run['acquisition']['archive']}`, sha256 of the uncompressed bytes `{run['acquisition']['sha256_uncompressed']}`" in readme
    # the committed copy of the archive's metadata is the record the archive is named by
    meta = json.loads((run_dir / 'archive.meta.json').read_text())
    assert meta['archive'] == run['acquisition']['archive'] == B._name_of({k: v for k, v in meta.items() if k != 'archive'})


def test_cli_refuses_an_empty_fetch_list_before_any_request_and_leaves_the_output_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(build_results_structure, 'ROOT', tmp_path)
    monkeypatch.setattr(B, 'fetch', lambda ids: pytest.fail('no request is made for an empty list'))
    out = tmp_path / 'published'
    out.mkdir()
    (out / 'run.json').write_text('{"kept": true}')
    for text in ('', '# only a comment\n\n'):
        ids = tmp_path / 'ids.txt'
        ids.write_text(text)
        assert build_results_structure.main(['--fetch', '--nct-list', str(ids), '--out', str(out)]) != 0
        assert 'lists no NCT ids' in capsys.readouterr().err
        assert json.loads((out / 'run.json').read_text()) == {'kept': True}


@pytest.mark.parametrize('missing', [{}, {'api_version': None, 'data_timestamp': None}, {'api_version': '2.0.5'},
                                     {'api_version': '2.0.5', 'data_timestamp': ' '}])
def test_an_archive_without_registry_provenance_is_refused(tmp_path: Path, missing: dict[str, Any]) -> None:
    """A self-consistent archive (its name is its own record's) whose record lacks the registry's
    version or data timestamp: an archive made before the fetch required them."""
    from outcomes import acquire
    lines = {RS.nct_id_of(s): acquire.encode([s]) for s in F.studies()}
    record = {'retrieved_at': F.GENERATED_AT, 'n_records': len(lines), 'n_requested': len(F.REQUESTED), **missing}
    path = tmp_path / B.archive_name(lines, record, F.REQUESTED)
    B.write_archive(lines, path, record, F.REQUESTED)
    with pytest.raises(B.BuildError, match='no registry API version or data timestamp'):
        B.read_archive_meta(path)
