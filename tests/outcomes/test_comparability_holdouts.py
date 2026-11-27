"""In_audit holdout EXCLUSION in the outcomes comparability family (src/holdout_exclusion.py).

Every id is fictional. The holdout index is the committed fictional
tests/fixtures/holdout_index/index.json (holdouts NCT99777701 and NCT99777702). A holdout is added
as a copy of the first fictional comparability study under a holdout id.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / 'scripts'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_outcomes_comparability  # noqa: E402
import check_outcomes_comparability_contract  # noqa: E402
import export_outcomes_comparability_site  # noqa: E402
from outcomes import acquire  # noqa: E402
from outcomes import comparability as C  # noqa: E402
from outcomes import comparability_build as B  # noqa: E402
from outcomes import outcome_history as OH  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402
from tests.outcomes.test_comparability import _sample_studies  # noqa: E402

HOLDOUT_INDEX = ROOT / 'tests' / 'fixtures' / 'holdout_index' / 'index.json'
HOLDOUT_ARGS = ['--holdout-index', str(HOLDOUT_INDEX)]
HOLDOUT = 'NCT99777701'
CLEAN_PRODUCER = {'repository': 'civicsample-engine', 'commit': 'c' * 40, 'uncommitted_changes': False,
                  'source_sha256': '1' * 64}


@pytest.fixture(autouse=True)
def _offline_and_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network; a producer the exporter accepts, whatever the checkout running the test."""
    monkeypatch.setattr(B, 'fetch', lambda *a, **k: pytest.fail('network called during offline test'))
    monkeypatch.setattr(build_outcomes_comparability, 'producer', lambda *a, **k: dict(CLEAN_PRODUCER))


def _holdout_study() -> dict[str, Any]:
    first = _sample_studies()[0]
    return json.loads(json.dumps(first).replace(C.nct_id_of(first) or '', HOLDOUT))


def _records_dir(studies: list[dict[str, Any]], target: Path) -> Path:
    target.mkdir(parents=True)
    for s in studies:
        (target / f'{C.nct_id_of(s)}.json.gz').write_bytes(gzip.compress(json.dumps(s).encode('utf-8'), mtime=0))
    return target


def _build_dir(records: Path, out: Path, *extra: str) -> int:
    return build_outcomes_comparability.main(['--records-dir', str(records), '--snapshot-date', '2026-11-15',
                                              '--out', str(out), *extra])


def _files_naming_a_holdout(root: Path) -> list[str]:
    ids = HX.load_holdout_index(HOLDOUT_INDEX).ids
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and HX.count_holdout_mentions(p.read_bytes(), ids))


def _without(obj: Any, keys: tuple[str, ...] = ('run_id',)) -> Any:
    if isinstance(obj, dict):
        return {k: _without(v, keys) for k, v in obj.items() if k not in keys}
    return [_without(v, keys) for v in obj] if isinstance(obj, list) else obj


@pytest.fixture()
def builds(tmp_path: Path) -> dict[str, Path]:
    with_holdout = _records_dir([*_sample_studies(), _holdout_study()], tmp_path / 'with' / 'records')
    clean = _records_dir(_sample_studies(), tmp_path / 'clean' / 'records')
    assert _build_dir(with_holdout, tmp_path / 'with' / 'build', *HOLDOUT_ARGS) == 0
    assert _build_dir(clean, tmp_path / 'clean' / 'build', *HOLDOUT_ARGS) == 0
    return {'with': tmp_path / 'with' / 'build', 'clean': tmp_path / 'clean' / 'build', 'records': with_holdout}


def test_builder_excludes_the_holdout_and_stamps_the_count(builds: dict[str, Path]) -> None:
    fam = B.load_dir(builds['with'])
    assert fam['run.json']['holdout_exclusion'] == {
        'rules_version': HX.RULES_VERSION, 'index_sha256': HX.load_holdout_index(HOLDOUT_INDEX).sha256,
        'holdouts_in_index': 2, 'excluded_from_universe': 1,
    }
    clean = B.load_dir(builds['clean'])
    assert sorted(fam['records.json']['records']) == sorted(clean['records.json']['records'])
    assert _without(fam['records.json']) == _without(clean['records.json'])
    assert _without(fam['summary.json']) == _without(clean['summary.json'])
    assert fam['run.json']['scope'] == clean['run.json']['scope']
    assert fam['run.json']['acquisition']['n_requested'] == len(_sample_studies())
    assert _files_naming_a_holdout(builds['with']) == []
    assert check_outcomes_comparability_contract.main([str(builds['with'])]) == 0


def test_build_is_byte_identical_on_rebuild(tmp_path: Path, builds: dict[str, Path]) -> None:
    # generated_at is wall-clock time in this family; everything else is byte-stable.
    again = tmp_path / 'again'
    assert _build_dir(builds['records'], again, *HOLDOUT_ARGS) == 0
    for name in ('records.json', 'summary.json'):
        assert (again / name).read_bytes() == (builds['with'] / name).read_bytes(), name
    a, b = (json.loads((d / 'run.json').read_text()) for d in (again, builds['with']))
    assert _without(a, ('generated_at',)) == _without(b, ('generated_at',))


def test_a_listed_holdout_is_dropped_and_counted_once(tmp_path: Path, builds: dict[str, Path]) -> None:
    ids = sorted([*(C.nct_id_of(s) or '' for s in _sample_studies()), HOLDOUT])
    nct_list = tmp_path / 'ids.txt'
    nct_list.write_text('\n'.join(ids) + '\n', encoding='utf-8')
    out = tmp_path / 'out'
    assert _build_dir(builds['records'], out, '--nct-list', str(nct_list), *HOLDOUT_ARGS) == 0
    run = json.loads((out / 'run.json').read_text())
    assert run['holdout_exclusion']['excluded_from_universe'] == 1  # in the frame and the list: one trial
    assert run['parameters']['n_requested'] == len(_sample_studies())


def test_an_older_archive_and_history_drop_the_holdout_before_any_parse(tmp_path: Path) -> None:
    studies = [*_sample_studies(), _holdout_study()]
    ids = sorted(C.nct_id_of(s) or '' for s in studies)
    lines = {C.nct_id_of(s) or '': acquire.encode([s]) for s in studies}
    rec_meta = {'source': 'fictional', 'mode': 'fetch', 'api_version': '2.0.5', 'data_timestamp': '2026-11-15T10:00:00',
                'retrieved_at': '2026-11-15T10:00:00+00:00', 'retrieved_from': '2026-11-15T10:00:00+00:00',
                'retrieved_to': '2026-11-15T10:00:00+00:00', 'n_records': len(lines), 'n_requested': len(ids),
                'not_retrieved': []}
    arch = tmp_path / B.archive_name(lines, rec_meta, ids)
    B.write_archive(lines, arch, rec_meta, ids)
    # The holdout's history line is malformed: parsed, it would refuse the history archive.
    hist = tmp_path / 'hist.jsonl.gz'
    hist.write_bytes(gzip.compress((json.dumps({'nct_id': HOLDOUT, 'protocolSection': {}}) + '\n').encode(), mtime=0))
    with pytest.raises(OH.HistoryArchiveError):
        OH.load_history_archive(hist)
    out = tmp_path / 'out'
    assert build_outcomes_comparability.main(['--archive', str(arch), '--history-archive', str(hist), '--out', str(out),
                                              *HOLDOUT_ARGS]) == 0
    run = json.loads((out / 'run.json').read_text())
    assert run['holdout_exclusion']['excluded_from_universe'] == 1
    assert run['acquisition']['n_requested'] == run['acquisition']['n_records'] == len(_sample_studies())
    assert run['history_archive']['n_records'] == 0
    assert run['scope']['frame']['n'] == len(_sample_studies())
    assert _files_naming_a_holdout(out) == []


def test_a_responses_cache_drops_requested_returned_and_bare_holdouts(tmp_path: Path) -> None:
    sample = _sample_studies()
    pair_ids = [C.nct_id_of(sample[0]) or '', HOLDOUT]
    body = json.dumps({'studies': [sample[0], _holdout_study()]})
    lines = [
        {'ids': pair_ids, 'url': f"https://clinicaltrials.gov/api/v2/studies?filter.ids={','.join(pair_ids)}",
         'fetched_at': '2026-11-15T10:00:00+00:00', 'status': 200, 'body': body,
         'body_sha256': hashlib.sha256(body.encode()).hexdigest()},
        *sample[1:],
        {**_holdout_study(), 'fetched_at': 'not a date'},  # a bare holdout line: nothing else of it is read
    ]
    cache = tmp_path / 'responses.jsonl.gz'
    cache.write_bytes(gzip.compress(b''.join((json.dumps(x) + '\n').encode() for x in lines), mtime=0))
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    studies, meta = B.load_responses_cache(cache, snapshot_date='2026-11-15', exclusion=exclusion)
    assert sorted(C.nct_id_of(s) or '' for s in studies) == sorted(C.nct_id_of(s) or '' for s in sample)
    assert HOLDOUT not in meta['requested_ids'] and meta['not_retrieved'] == []
    assert exclusion.n_excluded == 1


def test_fetch_never_requests_a_holdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []
    sample = _sample_studies()

    def fake_fetch(req_ids: list[str]) -> tuple[dict[str, bytes], dict[str, Any]]:
        asked.append(list(req_ids))
        lines = {C.nct_id_of(s) or '': acquire.encode([s]) for s in sample if C.nct_id_of(s) in req_ids}
        return lines, {'source': 'fictional', 'mode': 'fetch', 'api_version': '2.0.5',
                       'data_timestamp': '2026-11-15T10:00:00', 'retrieved_at': '2026-11-15T10:00:00+00:00',
                       'n_records': len(lines), 'n_requested': len(req_ids), 'not_retrieved': []}

    monkeypatch.setattr(B, 'fetch', fake_fetch)
    monkeypatch.setattr(build_outcomes_comparability, 'ROOT', tmp_path)
    nct_list = tmp_path / 'ids.txt'
    nct_list.write_text('\n'.join(sorted([*(C.nct_id_of(s) or '' for s in sample), HOLDOUT])) + '\n', encoding='utf-8')
    out = tmp_path / 'out'
    assert build_outcomes_comparability.main(['--fetch', '--nct-list', str(nct_list), '--out', str(out), *HOLDOUT_ARGS]) == 0
    assert asked == [sorted(C.nct_id_of(s) or '' for s in sample)]
    assert json.loads((out / 'run.json').read_text())['holdout_exclusion']['excluded_from_universe'] == 1
    assert _files_naming_a_holdout(tmp_path / 'build') == []  # the frozen fetch cache holds no holdout


def test_build_without_holdout_index_exits_nonzero_writing_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = _records_dir(_sample_studies(), tmp_path / 'records')
    out = tmp_path / 'out'
    with pytest.raises(SystemExit) as exited:
        _build_dir(records, out)
    assert exited.value.code == 2 and '--holdout-index' in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize('payload', [b'{}', b'{"rows": [{"nct_id": "NCT99777701", "in_audit": "yes"}]}',
                                     b'{"rows": [{"nct_id": " NCT99777701", "in_audit": true}]}', b'\xff\xfe'])
def test_a_malformed_index_is_refused_writing_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    records = _records_dir(_sample_studies(), tmp_path / 'records')
    bad = tmp_path / 'index.json'
    bad.write_bytes(payload)
    out = tmp_path / 'out'
    assert _build_dir(records, out, '--holdout-index', str(bad)) == 1
    assert not out.exists() and HOLDOUT not in capsys.readouterr().err


def test_contract_requires_a_valid_stamp(builds: dict[str, Path]) -> None:
    fam = B.load_dir(builds['with'])
    assert B.check(fam) == []
    missing = copy.deepcopy(fam)
    del missing['run.json']['holdout_exclusion']
    assert any('holdout_exclusion is missing' in p for p in B.check(missing))
    for key, value in (('excluded_from_universe', '1'), ('holdouts_in_index', -2), ('index_sha256', None),
                       ('rules_version', 1)):
        bad = copy.deepcopy(fam)
        bad['run.json']['holdout_exclusion'][key] = value
        assert any(f'holdout_exclusion.{key}' in p for p in B.check(bad)), key


def test_export_carries_the_stamp(tmp_path: Path, builds: dict[str, Path]) -> None:
    site = tmp_path / 'site'
    assert export_outcomes_comparability_site.main([str(builds['with']), str(site), *HOLDOUT_ARGS]) == 0
    run = json.loads((site / 'run.json').read_text())
    assert run['holdout_exclusion'] == json.loads((builds['with'] / 'run.json').read_text())['holdout_exclusion']
    assert _files_naming_a_holdout(site) == []


def test_export_requires_holdout_index(tmp_path: Path, builds: dict[str, Path]) -> None:
    with pytest.raises(SystemExit):
        export_outcomes_comparability_site.main([str(builds['with']), str(tmp_path / 'site')])
    assert not (tmp_path / 'site').exists()


def test_export_refuses_a_build_without_the_stamp(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / 'old'
    old.mkdir()
    for path in builds['with'].iterdir():
        (old / path.name).write_bytes(path.read_bytes())
    run = json.loads((old / 'run.json').read_text())
    del run['holdout_exclusion']
    (old / 'run.json').write_text(json.dumps(run, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    site = tmp_path / 'site'
    assert export_outcomes_comparability_site.main([str(old), str(site), *HOLDOUT_ARGS]) == 1
    assert 'holdout_exclusion is missing' in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_stamp_of_another_index(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / 'index.json'
    changed.write_bytes(HOLDOUT_INDEX.read_bytes().replace(b'"in_audit": false', b'"in_audit": true'))
    site = tmp_path / 'site'
    assert export_outcomes_comparability_site.main([str(builds['with']), str(site), '--holdout-index', str(changed)]) == 1
    assert 'the index changed since the build' in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_build_whose_records_name_a_holdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A valid build of non-holdouts, one whose primary measure's title names a holdout's id: its
    # per-trial record carries the id, and the exporter's scan refuses the build.
    sample = _sample_studies()
    measure = sample[2]['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]
    measure['title'] = f"{measure['title']} (pooled with {HOLDOUT})"
    records = _records_dir(sample, tmp_path / 'records')
    out = tmp_path / 'build'
    assert _build_dir(records, out, *HOLDOUT_ARGS) == 0
    assert 'records.json' in _files_naming_a_holdout(out)
    site = tmp_path / 'site'
    assert export_outcomes_comparability_site.main([str(out), str(site), *HOLDOUT_ARGS]) == 1
    err = capsys.readouterr().err
    assert 'name an in_audit holdout trial' in err and HOLDOUT not in err
    assert not site.exists()
