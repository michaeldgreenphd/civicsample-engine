"""In_audit holdout EXCLUSION in the disclosure family (src/holdout_exclusion.py), on fictional trials.

Every id here is fictional. The holdout index is the committed fictional
tests/fixtures/holdout_index/index.json (holdouts NCT99777701 and NCT99777702, a non-holdout
NCT99777703). The disclosure fixture's own trials (NCT0999000x) are no holdouts; a holdout trial is
added to the frame where a test needs one.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / 'scripts'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_SPEC = importlib.util.spec_from_file_location('disclosure_fixture', Path(__file__).resolve().parent / 'fixture.py')
assert _SPEC is not None and _SPEC.loader is not None
F = sys.modules.setdefault('disclosure_fixture', importlib.util.module_from_spec(_SPEC))
if not hasattr(F, 'NCT_IDS'):
    _SPEC.loader.exec_module(F)

import build_drug_disclosure as cli_build  # noqa: E402
import check_drug_disclosure_contract as cli_check  # noqa: E402
import export_drug_disclosure_site as cli_export  # noqa: E402
from disclosure import acquire as A  # noqa: E402
from disclosure import artifacts as ART  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

HOLDOUT_INDEX = ROOT / 'tests' / 'fixtures' / 'holdout_index' / 'index.json'
HOLDOUT_ARGS = ['--holdout-index', str(HOLDOUT_INDEX)]
HOLDOUT = 'NCT99777701'
SECOND_HOLDOUT = 'NCT99777702'
HOLDOUT_TOKENS = (HOLDOUT.encode(), SECOND_HOLDOUT.encode())
LOCAL_STAMP_ARGS = ['--fetched-at', F.GENERATED_AT, '--data-timestamp', '2026-11-01T08:00:00']
# The fixture trials with a CT.gov record (NCT09990008 is the stub's 404).
LOCAL_IDS = [n for n in F.NCT_IDS if n in F.STUDIES]

# A fictional holdout trial with something on every route: a results posting, a registry-cited PMID
# and a drug term, so each acquisition stage would request something for it if it were not excluded.
HOLDOUT_STUDY = F._make_study(
    HOLDOUT,
    title='Fictional holdout trial of holdoutinib',
    lead_sponsor='Pfizer Inc.',
    drugs=['Holdoutinib'],
    has_results=True,
    post_date=('2021-01-05', 'ACTUAL'),
    references=[{'pmid': '99777701', 'type': 'RESULT', 'citation': 'Fictional holdout results. 2021.'}],
)


def holdout_stub(calls: list[str] | None = None) -> A.Get:
    """The fixture's stub, also answering for the holdout trial's CT.gov study request."""
    base = F.stub_get(calls)

    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/studies/{HOLDOUT}':
            if calls is not None:
                calls.append(f'{url}?{json.dumps(params or {}, sort_keys=True)}')
            return F._json_resp(HOLDOUT_STUDY)
        return base(url, params)

    return get


def _files_naming_a_holdout(root: Path) -> list[str]:
    """Files under `root` whose bytes (gunzipped when gzip) hold a holdout id token."""
    index = HX.load_holdout_index(HOLDOUT_INDEX)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and HX.count_holdout_mentions(p.read_bytes(), index.ids))


def _old_cache_with_a_holdout(cache_dir: Path) -> None:
    """A cache acquired and frozen as before the exclusion: its frame and records include HOLDOUT."""
    transport = holdout_stub()
    ids = sorted([*F.NCT_IDS, HOLDOUT])
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    ct_log = A.ResponseLog(A.raw_log_path(cache_dir, 'ctgov'))
    A.fetch_ctgov_studies(ids, ct_log, transport, throttle, run_stamp='2026-11-01')
    studies = A.studies_from_ctgov_records(ct_log.records, ids)
    A.fetch_literature(ids, studies, A.ResponseLog(A.raw_log_path(cache_dir, 'literature')), transport, throttle)
    A.fetch_openfda(ids, studies, A.ResponseLog(A.raw_log_path(cache_dir, 'openfda')), transport, throttle)
    A.freeze_cache(cache_dir, ids, frozen_at=F.GENERATED_AT)
    assert _files_naming_a_holdout(cache_dir)  # the old cache does hold the holdout


def _build(cache: Path, out: Path, *extra: str) -> int:
    return cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--as-of', F.AS_OF,
                           '--generated-at', F.GENERATED_AT, *extra])


@pytest.fixture(autouse=True)
def _no_throttle_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's throttle keeps its interval but never sleeps in these offline tests."""
    real = A.Throttle

    def throttle(min_interval: float = A.MIN_INTERVAL_SECONDS, **_: Any) -> A.Throttle:
        return real(min_interval=min_interval, sleep=lambda _: None)

    monkeypatch.setattr(A, 'Throttle', throttle)


@pytest.fixture(scope='module')
def old_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cache = tmp_path_factory.mktemp('old_cache') / 'cache'
    _old_cache_with_a_holdout(cache)
    return cache


@pytest.fixture(scope='module')
def holdout_build(tmp_path_factory: pytest.TempPathFactory, old_cache: Path) -> Path:
    out = tmp_path_factory.mktemp('holdout_build') / 'disclosure'
    assert _build(old_cache, out, *HOLDOUT_ARGS) == 0
    return out


# ---------------------------------------------------------------- acquisition


def test_acquire_never_requests_a_holdout_and_stamps_the_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub(calls))
    id_file = tmp_path / 'ids.txt'
    id_file.write_text('\n'.join(sorted([*F.NCT_IDS, HOLDOUT])) + '\n', encoding='utf-8')
    cache = tmp_path / 'cache'
    # A holdout given with --nct-id is dropped and counted like a listed one: both are the universe.
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-list', str(id_file),
                           '--nct-id', SECOND_HOLDOUT, *HOLDOUT_ARGS]) == 0
    assert calls, 'the stub saw no request at all'
    assert not [c for c in calls if any(t.decode() in c for t in HOLDOUT_TOKENS)]
    # Not one request names a holdout's registry-cited PMID or drug term either.
    assert not [c for c in calls if '99777701' in c or 'holdoutinib' in c.lower()]
    manifest = json.loads((cache / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['requested_nct_ids'] == F.NCT_IDS
    stamp = manifest['holdout_exclusion']
    assert stamp == {'rules_version': HX.RULES_VERSION, 'index_sha256': HX.load_holdout_index(HOLDOUT_INDEX).sha256,
                     'holdouts_in_index': 2, 'excluded_from_universe': 2}
    assert _files_naming_a_holdout(cache) == []

    # The build carries the acquisition's count forward: 2 excluded, the 8 fixture trials built. (The
    # cache was frozen now, so the build is generated now too.)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--as-of', F.AS_OF, *HOLDOUT_ARGS]) == 0
    files, _ = ART.load_dir(out)
    assert files['run.json']['holdout_exclusion']['excluded_from_universe'] == 2
    assert files['summary.json']['denominators']['included_trials']['n'] == len(F.NCT_IDS)
    assert _files_naming_a_holdout(out) == []


def test_acquire_of_only_holdouts_is_refused_before_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub(calls))
    cache = tmp_path / 'cache'
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-id', HOLDOUT, *HOLDOUT_ARGS]) == 1
    assert calls == []
    assert not cache.exists()


def test_acquire_refuses_a_cache_whose_raw_logs_hold_a_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / 'cache'
    _old_cache_with_a_holdout(cache)
    before = {p: p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub(calls))
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-id', 'NCT09990001', *HOLDOUT_ARGS]) == 1
    assert calls == []
    assert {p: p.read_bytes() for p in cache.rglob('*') if p.is_file()} == before
    err = capsys.readouterr().err
    assert 'acquire into a new --cache' in err and HOLDOUT not in err
    # freeze_cache itself refuses the same cache when it is given the exclusion.
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    with pytest.raises(A.CacheIntegrityError, match='logged for in_audit holdout trials'):
        A.freeze_cache(cache, exclusion.exclude_ids(F.NCT_IDS), exclusion=exclusion)


@pytest.mark.parametrize('command', ['acquire', 'freeze-local', 'build'])
def test_every_subcommand_requires_holdout_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub(calls))
    cache, out = tmp_path / 'cache', tmp_path / 'out'
    records = tmp_path / 'records.json'
    records.write_text(json.dumps(F.STUDIES['NCT09990001']), encoding='utf-8')
    argv = {
        'acquire': ['acquire', '--cache', str(cache), '--nct-id', 'NCT09990001'],
        'freeze-local': ['freeze-local', '--records', str(records), '--cache', str(cache), *LOCAL_STAMP_ARGS],
        'build': ['build', '--cache', str(cache), '--records', str(records), '--out', str(out), *LOCAL_STAMP_ARGS],
    }[command]
    with pytest.raises(SystemExit) as exited:
        cli_build.main(argv)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert 'usage:' in err and '--holdout-index' in err
    assert calls == [] and not cache.exists() and not out.exists()


@pytest.mark.parametrize('payload', [
    b'{"data": []}',                                                       # no rows list
    b'{"rows": []}',                                                       # rows empty
    b'{"rows": [{"nct_id": "NCT99777701", "in_audit": 1}]}',              # not a bool
    b'{"rows": [{"nct_id": "NCT99777701"}]}',                             # flag missing
    b'{"rows": [{"nct_id": "nct99777701", "in_audit": true}]}',           # not canonical
    b'{"rows": [{"nct_id": "NCT99777701", "in_audit": true, "in_audit": false}]}',  # repeated key
    b'{"rows": [{"nct_id": "NCT99777701", "in_audit": true}, {"nct_id": "NCT99777701", "in_audit": false}]}',
    b'not json',
])
def test_a_malformed_index_is_refused_before_anything_is_requested_or_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    bad = tmp_path / 'index.json'
    bad.write_bytes(payload)
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub(calls))
    cache = tmp_path / 'cache'
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-id', 'NCT09990001', '--holdout-index', str(bad)]) == 1
    assert calls == [] and not cache.exists()
    err = capsys.readouterr().err
    assert err.startswith('REFUSED: index.json') and 'NCT99777701' not in err and 'nct99777701' not in err


# ---------------------------------------------------------------- local freeze


def test_freeze_local_drops_a_holdout_record_by_id_before_reading_it(tmp_path: Path) -> None:
    # The holdout's record is malformed: read, it would refuse the whole freeze. Dropped by its id
    # first, it is never validated, and the freeze of the other trials goes ahead.
    unreadable = copy.deepcopy(HOLDOUT_STUDY)
    unreadable['protocolSection']['statusModule'] = ['not', 'a', 'module']
    lines = [json.dumps(F.STUDIES[n]) for n in LOCAL_IDS] + [json.dumps(unreadable)]
    records = tmp_path / 'records.jsonl'
    records.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    cache = tmp_path / 'cache'
    assert cli_build.main(['freeze-local', '--records', str(records), '--cache', str(cache),
                           *LOCAL_STAMP_ARGS, *HOLDOUT_ARGS]) == 0
    manifest = json.loads((cache / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['requested_nct_ids'] == LOCAL_IDS
    assert manifest['holdout_exclusion']['excluded_from_universe'] == 1
    assert _files_naming_a_holdout(cache) == []
    # Without the exclusion the same records are refused for the holdout's malformed part.
    with pytest.raises(ValueError, match='not valid CT.gov v2 study records'):
        A.ingest_local_ctgov_records(records, tmp_path / 'unfiltered', fetched_at=F.GENERATED_AT,
                                     data_timestamp='2026-11-01T08:00:00')


def test_freeze_local_drops_a_listed_holdout_and_counts_it(tmp_path: Path) -> None:
    records = tmp_path / 'records.jsonl'
    records.write_text('\n'.join(json.dumps(F.STUDIES[n]) for n in LOCAL_IDS) + '\n', encoding='utf-8')
    wanted = tmp_path / 'ids.txt'
    # The holdout is listed but absent from the local records: dropped first, it is not refused as absent.
    wanted.write_text(f'NCT09990001\n{HOLDOUT}\n', encoding='utf-8')
    cache = tmp_path / 'cache'
    assert cli_build.main(['freeze-local', '--records', str(records), '--cache', str(cache), '--nct-list',
                           str(wanted), *LOCAL_STAMP_ARGS, *HOLDOUT_ARGS]) == 0
    manifest = json.loads((cache / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['requested_nct_ids'] == ['NCT09990001']
    assert manifest['holdout_exclusion']['excluded_from_universe'] == 1


# ---------------------------------------------------------------- build


def test_build_ignores_the_holdout_an_older_cache_holds(old_cache: Path, holdout_build: Path) -> None:
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    loaded = A.load_frozen_cache(old_cache, exclusion=exclusion)
    assert loaded.requested_nct_ids == F.NCT_IDS
    assert HOLDOUT not in loaded.manifest['requested_nct_ids']
    for records in (loaded.ctgov, loaded.literature, loaded.openfda):
        assert not [k for k in records if HOLDOUT in k]
    # The archive meta published as run.json sources counts only the records the build kept.
    assert loaded.ctgov_meta['n_records'] == len(loaded.ctgov)
    assert exclusion.n_excluded == 1

    files, _ = ART.load_dir(holdout_build)
    assert files['run.json']['holdout_exclusion'] == {
        'rules_version': HX.RULES_VERSION, 'index_sha256': exclusion.index.sha256,
        'holdouts_in_index': 2, 'excluded_from_universe': 1,
    }
    assert files['run.json']['scope']['n_cache_frame'] == len(F.NCT_IDS)
    assert [s['nct_id'] for s in files['studies.json']['studies']] == F.NCT_IDS
    assert files['summary.json']['denominators']['included_trials']['n'] == len(F.NCT_IDS)
    assert _files_naming_a_holdout(holdout_build) == []
    assert cli_check.main([str(holdout_build)]) == 0


def test_build_of_an_older_cache_is_byte_identical_on_rebuild(tmp_path: Path, old_cache: Path, holdout_build: Path) -> None:
    again = tmp_path / 'again'
    assert _build(old_cache, again, *HOLDOUT_ARGS) == 0
    names = sorted(p.name for p in holdout_build.iterdir())
    assert names == sorted(p.name for p in again.iterdir())
    assert all((holdout_build / n).read_bytes() == (again / n).read_bytes() for n in names)


def test_build_matches_a_cache_that_never_held_the_holdout_on_every_trial(
    tmp_path: Path, holdout_build: Path
) -> None:
    clean = tmp_path / 'clean_cache'
    F.acquire_fixture_cache(clean, get=holdout_stub())
    out = tmp_path / 'clean_build'
    assert _build(clean, out, *HOLDOUT_ARGS) == 0
    a, _ = ART.load_dir(holdout_build)
    b, _ = ART.load_dir(out)

    def timeless(obj: Any) -> Any:
        # The two caches were acquired seconds apart: retrieval times differ, nothing else may.
        if isinstance(obj, dict):
            return {k: timeless(v) for k, v in obj.items() if k not in ('retrieved_at', 'fetched_at', 'run_id')}
        return [timeless(v) for v in obj] if isinstance(obj, list) else obj

    for name in ('studies.json', 'links.json', 'coverage.json', 'timelines.json', 'sponsors.json', 'summary.json'):
        assert timeless(a[name]) == timeless(b[name]), name


def test_build_without_holdout_index_writes_nothing(tmp_path: Path, old_cache: Path) -> None:
    out = tmp_path / 'out'
    with pytest.raises(SystemExit):
        _build(old_cache, out)
    assert not out.exists()


def test_build_refuses_a_cache_stamped_under_another_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(A, 'requests_get', lambda: holdout_stub())
    cache = tmp_path / 'cache'
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-id', 'NCT09990001',
                           *HOLDOUT_ARGS]) == 0
    other = tmp_path / 'other_index.json'
    other.write_text(json.dumps({'rows': [{'nct_id': SECOND_HOLDOUT, 'in_audit': True}]}), encoding='utf-8')
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--holdout-index', str(other)]) == 1
    assert 'was made under another holdout index' in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize('curated', ['reviews', 'statements'])
def test_build_refuses_a_curated_row_naming_a_holdout(tmp_path: Path, old_cache: Path, curated: str) -> None:
    if curated == 'reviews':
        path = tmp_path / 'reviews.csv'
        path.write_text('link_id,nct_id,reviewer,reviewed_at,decision,notes\n'
                        f'link:0000000000000001,{HOLDOUT},A Person,2026-10-01,indeterminate,fictional\n',
                        encoding='utf-8')
    else:
        path = tmp_path / 'statements.csv'
        path.write_text('statement_id,nct_id,source_type,source_locator,statement_date,verbatim_excerpt,'
                        'recorded_by,notes\n'
                        f's1,{HOLDOUT},registry_record,x,2021,text,A Person,fictional\n', encoding='utf-8')
    out = tmp_path / 'out'
    assert _build(old_cache, out, f'--{curated}', str(path), *HOLDOUT_ARGS) == 1
    assert not out.exists()


# ---------------------------------------------------------------- contract and export


def test_contract_requires_a_valid_stamp(holdout_build: Path) -> None:
    files, _ = ART.load_dir(holdout_build)
    assert ART.check_contract(files) == []
    missing = copy.deepcopy(files)
    del missing['run.json']['holdout_exclusion']
    assert any('holdout_exclusion is missing' in p for p in ART.check_contract(missing))
    for key, value in (('excluded_from_universe', True), ('holdouts_in_index', '2'), ('index_sha256', 'abc'),
                       ('rules_version', 'holdout-exclusion-0')):
        bad = copy.deepcopy(files)
        bad['run.json']['holdout_exclusion'][key] = value
        assert any(f'holdout_exclusion.{key}' in p for p in ART.check_contract(bad)), key
    extra = copy.deepcopy(files)
    extra['run.json']['holdout_exclusion']['nct_ids'] = []
    assert any('must hold exactly' in p for p in ART.check_contract(extra))


def test_export_carries_the_stamp_and_names_no_holdout(tmp_path: Path, holdout_build: Path) -> None:
    site = tmp_path / 'site'
    assert cli_export.main([str(holdout_build), str(site), *HOLDOUT_ARGS]) == 0
    run = json.loads((site / 'run.json').read_text(encoding='utf-8'))
    assert run['holdout_exclusion'] == json.loads((holdout_build / 'run.json').read_text())['holdout_exclusion']
    assert _files_naming_a_holdout(site) == []


def test_export_requires_holdout_index(tmp_path: Path, holdout_build: Path) -> None:
    site = tmp_path / 'site'
    with pytest.raises(SystemExit):
        cli_export.main([str(holdout_build), str(site)])
    assert not site.exists()


def _resealed_without_stamp(src: Path, dst: Path) -> None:
    """A copy of a build whose run.json lacks the stamp, its manifest digest recomputed (a pre-change build)."""
    shutil.copytree(src, dst)
    run = json.loads((dst / 'run.json').read_text(encoding='utf-8'))
    del run['holdout_exclusion']
    run['manifest_sha256'] = ART.manifest_sha256(run)
    (dst / 'run.json').write_text(json.dumps(run, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')


def test_export_refuses_a_build_without_the_stamp(tmp_path: Path, holdout_build: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    old = tmp_path / 'old_build'
    _resealed_without_stamp(holdout_build, old)
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'keep.txt').write_text('untouched', encoding='utf-8')
    assert cli_export.main([str(old), str(site), *HOLDOUT_ARGS]) == 1
    assert 'holdout_exclusion is missing' in capsys.readouterr().err
    assert sorted(p.name for p in site.iterdir()) == ['keep.txt']


def test_export_refuses_a_stamp_of_another_index(tmp_path: Path, holdout_build: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    changed = tmp_path / 'index.json'
    changed.write_bytes(HOLDOUT_INDEX.read_bytes() + b'\n')  # same holdouts, other bytes: the index changed
    site = tmp_path / 'site'
    assert cli_export.main([str(holdout_build), str(site), '--holdout-index', str(changed)]) == 1
    assert 'the index changed since the build' in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_build_whose_output_names_a_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-holdout trial whose own record mentions a holdout's id: the build publishes the text
    # (it is that trial's record), and the exporter's defence-in-depth scan refuses it.
    mentioning = copy.deepcopy(F.STUDIES)
    mentioning['NCT09990004']['protocolSection']['identificationModule']['briefTitle'] = f'Follow-up of {HOLDOUT}'
    monkeypatch.setattr(F, 'STUDIES', mentioning)
    cache = tmp_path / 'cache'
    F.acquire_fixture_cache(cache)
    out = tmp_path / 'out'
    assert _build(cache, out, *HOLDOUT_ARGS) == 0
    site = tmp_path / 'site'
    assert cli_export.main([str(out), str(site), *HOLDOUT_ARGS]) == 1
    err = capsys.readouterr().err
    assert 'name an in_audit holdout trial' in err and 'studies.json' in err and HOLDOUT not in err
    assert not site.exists()
