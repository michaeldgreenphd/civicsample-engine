"""Comprehensive offline test suite for `disclosure/` and its CLI scripts."""
from __future__ import annotations

import ast
import copy
import csv
import gzip
import hashlib
import importlib.util
import inspect
import json
import re
import shlex
import shutil
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

_SPEC = importlib.util.spec_from_file_location(
    'disclosure_fixture',
    Path(__file__).resolve().parent / 'fixture.py',
)
assert _SPEC is not None and _SPEC.loader is not None
F = sys.modules.setdefault('disclosure_fixture', importlib.util.module_from_spec(_SPEC))
if not hasattr(F, 'NCT_IDS'):
    _SPEC.loader.exec_module(F)

import build_drug_disclosure as cli_build  # noqa: E402
import check_drug_disclosure_contract as cli_check  # noqa: E402
import export_drug_disclosure_site as cli_export  # noqa: E402
from disclosure import acquire as A  # noqa: E402
from disclosure import artifacts as ART  # noqa: E402
from disclosure import coverage as C  # noqa: E402
from disclosure import evidence as E  # noqa: E402
from disclosure import timeline as T  # noqa: E402


@pytest.fixture(scope='module')
def frozen_cache(tmp_path_factory: pytest.TempPathFactory) -> A.FrozenCache:
    cache_dir = tmp_path_factory.mktemp('disclosure_cache')
    return F.acquire_fixture_cache(cache_dir)


@pytest.fixture(scope='module')
def built_dir(tmp_path_factory: pytest.TempPathFactory, frozen_cache: A.FrozenCache) -> Path:
    out_dir = tmp_path_factory.mktemp('disclosure_build') / 'disclosure'
    rc = cli_build.main([
        'build',
        '--cache', str(frozen_cache.cache_dir),
        '--out', str(out_dir),
        '--as-of', F.AS_OF,
        '--generated-at', F.GENERATED_AT,
    ])
    assert rc == 0
    return out_dir


@pytest.fixture(scope='module')
def built_files(built_dir: Path) -> dict[str, Any]:
    files, _ = ART.load_dir(built_dir)
    return files


# Local CT.gov records carry no acquisition time of their own, so every local freeze states one.
LOCAL_STAMPS: dict[str, str] = {'fetched_at': F.GENERATED_AT, 'data_timestamp': '2026-11-01T08:00:00'}
LOCAL_STAMP_ARGS = ['--fetched-at', LOCAL_STAMPS['fetched_at'], '--data-timestamp', LOCAL_STAMPS['data_timestamp']]


def _timeline_of(files: dict[str, Any], nct_id: str) -> dict[str, Any]:
    return next(t for t in files['timelines.json']['timelines'] if t['nct_id'] == nct_id)


def _coverage_of(files: dict[str, Any], nct_id: str) -> dict[str, Any]:
    return next(c for c in files['coverage.json']['trials'] if c['nct_id'] == nct_id)


def _cell_of(files: dict[str, Any], nct_id: str, route: str) -> dict[str, Any]:
    cov = _coverage_of(files, nct_id)
    return next(r for r in cov['routes'] if r['route'] == route)


# ---------------------------------------------------------------- 1. Curated CSV hygiene

def test_tracked_curated_csvs_are_header_only_and_untouched() -> None:
    rev_path = ROOT / 'disclosure' / 'reviews.csv'
    stmt_path = ROOT / 'disclosure' / 'documentary_statements.csv'
    assert rev_path.read_text(encoding='utf-8').splitlines() == [','.join(E.REVIEW_COLUMNS)]
    assert stmt_path.read_text(encoding='utf-8').splitlines() == [','.join(E.STATEMENT_COLUMNS)]
    reviews, _ = E.read_reviews(rev_path)
    statements, _ = E.read_statements(stmt_path)
    assert reviews == {} and statements == []


# ---------------------------------------------------------------- 2. Acquisition & cache integrity

def test_throttle_enforces_at_most_two_requests_per_second_per_host() -> None:
    with pytest.raises(ValueError, match='<= 2 requests/second'):
        A.Throttle(min_interval=0.49)

    clock_now = [10.0]
    slept: list[float] = []

    def fake_sleep(sec: float) -> None:
        slept.append(round(sec, 4))
        clock_now[0] += sec

    th = A.Throttle(min_interval=0.55, sleep=fake_sleep, clock=lambda: clock_now[0])
    th.wait('https://clinicaltrials.gov/api/v2/studies/NCT09990001')
    clock_now[0] += 0.20
    th.wait('https://clinicaltrials.gov/api/v2/studies/NCT09990002')
    assert slept == [0.35]
    # Different host is tracked independently
    th.wait('https://api.fda.gov/drug/drugsfda.json')
    assert slept == [0.35]


@pytest.mark.parametrize('interval', [float('nan'), float('inf'), 0.4, True])
def test_throttle_refuses_a_non_finite_or_too_short_interval(tmp_path: Path, interval: float) -> None:
    # NaN compared false with the floor and with every remaining wait, so requests went unthrottled.
    with pytest.raises(ValueError, match='must be a finite number >= 0.5s'):
        A.Throttle(min_interval=interval)
    if interval is not True:
        assert cli_build.main(['acquire', '--cache', str(tmp_path / 'c'), '--nct-id', 'NCT09990001',
                               '--min-interval', str(interval)]) == 1
        assert not (tmp_path / 'c').exists()


@pytest.mark.parametrize('limit', [0, -1])
def test_local_freeze_refuses_a_limit_below_one(tmp_path: Path, limit: int) -> None:
    # The cap was checked only after a record was kept, so --limit 0 froze one record.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError, match=f'limit must be a positive whole number of records, got {limit}'):
        A.ingest_local_ctgov_records(local_src, cache, limit=limit, **LOCAL_STAMPS)
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache), '--limit', str(limit),
                           '--fetched-at', LOCAL_STAMPS['fetched_at'], '--data-timestamp',
                           LOCAL_STAMPS['data_timestamp']]) == 1
    assert not A.raw_log_path(cache, 'ctgov').exists()


def test_resumable_log_truncates_interrupted_line_and_reuses_settled_answers(tmp_path: Path) -> None:
    calls: list[str] = []
    get = F.stub_get(calls)
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log_path = A.raw_log_path(tmp_path / 'cache', 'ctgov')

    log1 = A.ResponseLog(log_path)
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990008'], log1, get, throttle, run_stamp='2026-11-01T06:00:00+00:00')
    first_count = len(calls)
    assert first_count == 4  # version, 2 studies (200 and 404 are both final), version again

    # Simulate an interrupted process appending a truncated JSON line without '\n'
    with log_path.open('ab') as fh:
        fh.write(b'{"key":"ctgov/study/NCT09990002","partial":')

    log2 = A.ResponseLog(log_path)
    assert 'ctgov/study/NCT09990002' not in log2.records
    A.fetch_ctgov_studies(
        ['NCT09990001', 'NCT09990002', 'NCT09990008'], log2, get, throttle, run_stamp='2026-11-01T12:00:00+00:00'
    )
    # Only NCT09990002 is fetched on resume; NCT09990001 (200) and NCT09990008 (404) are reused. The
    # resumed run observes the CT.gov version again, before and after, as every run does.
    assert len(calls) == first_count + 3
    assert sum(c.startswith(f'{A.CTGOV_API}/version?') for c in calls[first_count:]) == 2


def test_unicode_line_separators_in_jsonl_logs_local_jsonl_and_frozen_archives(tmp_path: Path) -> None:
    """U+2028, U+2029, U+0085, VT (\\x0b), and FF (\\x0c) inside ensure_ascii=False JSON strings
    must never split JSONL records across read_jsonl, local .jsonl.gz seeding, or frozen archives."""
    tricky_title = 'Phase 3 trial\u2028line-sep\u2029para-sep\x85nel\x0bvt\x0cff of zorbatinib'
    study = copy.deepcopy(F.STUDIES['NCT09990001'])
    study['protocolSection']['identificationModule']['briefTitle'] = tricky_title

    # 1. Local `.jsonl.gz` seeding with unescaped UTF-8 U+2028 / U+2029 (`ensure_ascii=False`)
    local_jsonl = tmp_path / 'tricky_studies.jsonl.gz'
    raw_line = (json.dumps(study, ensure_ascii=False) + '\n').encode('utf-8')
    assert '\u2028'.encode('utf-8') in raw_line and '\u2029'.encode('utf-8') in raw_line
    local_jsonl.write_bytes(gzip.compress(raw_line, mtime=0))

    cache_dir = tmp_path / 'unicode_cache'
    A.ingest_local_ctgov_records(local_jsonl, cache_dir, **LOCAL_STAMPS, frozen_at=F.GENERATED_AT)

    # 2. Also append a literature ResponseLog record containing unescaped U+2028 / U+2029 in abstractText
    lit_log = A.ResponseLog(A.raw_log_path(cache_dir, 'literature'))
    epmc_body = {
        'hitCount': 1,
        'resultList': {'result': [{
            'id': '99000001',
            'pmid': '99000001',
            'title': f'Results\u2028with\u2029separators',
            'abstractText': 'Primary results\u2028of NCT09990001\u2029in adults\x85confirmed.',
            'firstPublicationDate': '2021-02-10',
        }]},
    }
    lit_log.put({
        'key': 'literature/epmc_nct/NCT09990001',
        'url': A.EPMC_REST,
        'params': {'query': '"NCT09990001"', 'format': 'json', 'pageSize': '100', 'resultType': 'core'},
        'tag': {'nct_id': 'NCT09990001'},
        'fetched_at': F.GENERATED_AT,
        'status': 200,
        'http_status': 200,
        'attempts': 1,
        'response_sha256': A.sha256_bytes(A.canonical_json_bytes(epmc_body)),
        'body': epmc_body,
        'error': None,
    })
    # Reload ResponseLog via read_jsonl and freeze + reload via _read_verified_archive
    reloaded_lit = A.ResponseLog(A.raw_log_path(cache_dir, 'literature'))
    assert '\u2028' in reloaded_lit.get('literature/epmc_nct/NCT09990001')['body']['resultList']['result'][0]['abstractText']  # type: ignore[index]

    A.freeze_cache(cache_dir, ['NCT09990001'], frozen_at=F.GENERATED_AT)
    loaded = A.load_frozen_cache(cache_dir)
    built = ART.build_artifacts(
        loaded,
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(built) == []
    assert built['studies.json']['studies'][0]['brief_title'] == tricky_title


def test_offline_cache_loader_refuses_partial_missing_or_hash_mismatched_caches(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
) -> None:
    # 1. Missing manifest.json
    c1 = tmp_path / 'no_manifest'
    shutil.copytree(frozen_cache.cache_dir, c1)
    (c1 / 'manifest.json').unlink()
    with pytest.raises(A.CacheIntegrityError, match='manifest.json'):
        A.load_frozen_cache(c1)

    # 2. Lingering `.part` file
    c2 = tmp_path / 'has_part'
    shutil.copytree(frozen_cache.cache_dir, c2)
    (c2 / 'ctgov.jsonl.gz.part').write_bytes(b'partial')
    with pytest.raises(A.CacheIntegrityError, match=r'\.part'):
        A.load_frozen_cache(c2)

    # 3. Corrupted compressed archive bytes (sha256 mismatch)
    c3 = tmp_path / 'bad_sha'
    shutil.copytree(frozen_cache.cache_dir, c3)
    (c3 / 'ctgov.jsonl.gz').write_bytes(gzip.compress(b'{"key":"tampered"}\n', mtime=0))
    with pytest.raises(A.CacheIntegrityError, match='sha256'):
        A.load_frozen_cache(c3)

    # 4. Missing `.meta.json` sidecar
    c4 = tmp_path / 'no_meta'
    shutil.copytree(frozen_cache.cache_dir, c4)
    (c4 / 'ctgov.jsonl.gz.meta.json').unlink()
    with pytest.raises(A.CacheIntegrityError, match=r'\.meta\.json'):
        A.load_frozen_cache(c4)

    # 5. Unfrozen new line appended to raw/ctgov.jsonl after freeze
    c5 = tmp_path / 'stale_freeze'
    shutil.copytree(frozen_cache.cache_dir, c5)
    log = A.ResponseLog(A.raw_log_path(c5, 'ctgov'))
    log.put({
        'key': 'ctgov/study/NCT09999999',
        'url': 'x',
        'params': {},
        'tag': {},
        'fetched_at': F.GENERATED_AT,
        'status': 404,
        'http_status': 404,
        'attempts': 1,
        'response_sha256': '0' * 64,
        'body': {},
        'error': None,
    })
    with pytest.raises(A.CacheIntegrityError, match='differs from frozen'):
        A.load_frozen_cache(c5)


@pytest.mark.parametrize('field', ['first_fetched_at', 'last_fetched_at', 'frozen_at', 'description'])
def test_an_edited_archive_sidecar_is_refused_at_load(
    tmp_path: Path, frozen_cache: A.FrozenCache, field: str
) -> None:
    # The build publishes the sidecar's fields (run.json sources) and reads last_fetched_at for the
    # as_of fallback and bound, so a sidecar edited after freezing must not load.
    cache = tmp_path / 'edited_sidecar'
    shutil.copytree(frozen_cache.cache_dir, cache)
    meta_path = cache / 'ctgov.jsonl.gz.meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta[field] = '2020-01-01T00:00:00+00:00'
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(A.CacheIntegrityError, match=f"disagree on '{field}'"):
        A.load_frozen_cache(cache)
    # Edited consistently in the manifest entry too, the fetch times still disagree with the archive's
    # own records, and frozen_at with the manifest's.
    manifest_path = cache / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['archives']['ctgov'][field] = meta[field]
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    if field == 'description':
        A.load_frozen_cache(cache)  # a label the build does not take as evidence
    else:
        with pytest.raises(A.CacheIntegrityError, match=rf'{field}'):
            A.load_frozen_cache(cache)


def _study_with(base: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """A deep copy of `base` with the part at `path` set to `value`."""
    out = copy.deepcopy(base)
    parent = out
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    return out


@pytest.mark.parametrize(
    ('layout', 'bad'),
    [
        ('file', {'protocolSection': {'identificationModule': {'briefTitle': 'no NCT ID'}}}),
        ('file', {'protocolSection': 'projected'}),
        ('list', 'NCT09990005'),
        ('list', {'hasResults': False}),
        # A part the build reads, of the wrong type, froze a cache the build could not read.
        ('file', _study_with(F.STUDIES['NCT09990005'], ('protocolSection', 'statusModule'), ['bad'])),
        ('list', _study_with(F.STUDIES['NCT09990005'], ('protocolSection', 'designModule', 'enrollmentInfo'), [1])),
        # `hasResults: false` beside a posted results section, or a posting date, contradicts itself.
        ('file', _study_with(F.STUDIES['NCT09990001'], ('hasResults',), False)),
        ('list', _study_with(F.STUDIES['NCT09990005'],
                             ('protocolSection', 'statusModule', 'resultsFirstPostDateStruct'),
                             {'date': '2021-05-01', 'type': 'ACTUAL'})),
    ],
)
@pytest.mark.parametrize('with_nct_list', [False, True])
def test_freeze_local_refuses_malformed_study_records_instead_of_dropping_them(
    tmp_path: Path, layout: str, bad: Any, with_nct_list: bool
) -> None:
    # A malformed record was skipped, so the batch froze smaller than supplied with nothing reported.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'a.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    if layout == 'file':
        (local_src / 'b.json').write_text(json.dumps(bad), encoding='utf-8')
        where = 'b.json study 1'
    else:
        (local_src / 'b.json').write_text(json.dumps({'studies': [F.STUDIES['NCT09990005'], bad]}), encoding='utf-8')
        where = 'b.json study 2'
    ids = {'nct_ids': ['NCT09990004']} if with_nct_list else {}
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError, match=rf'1 study record\(s\) are not valid .*\(first: {re.escape(where)}: '):
        A.ingest_local_ctgov_records(local_src, cache, **ids, **LOCAL_STAMPS)
    assert not (cache / 'manifest.json').exists()
    # Control: without the malformed record the batch freezes.
    (local_src / 'b.json').unlink()
    A.ingest_local_ctgov_records(local_src, cache, **ids, **LOCAL_STAMPS)


@pytest.mark.parametrize(
    ('fetched_at', 'data_timestamp', 'ok'),
    [
        ('2026-11-01T12:00:00+00:00', '2026-12-01T00:00:00', False),  # a refresh a month after retrieval
        ('2026-11-01T12:00:00+00:00', '2026-11-02T02:00:01', False),  # just over 14 hours after
        ('2026-11-01T12:00:00+00:00', '2026-11-01T08:00:00', True),
        ('2026-11-01T12:00:00+00:00', '2026-11-01T20:00:00', True),  # within any real UTC offset
    ],
)
def test_freeze_local_refuses_a_data_timestamp_later_than_retrieval(
    tmp_path: Path, fetched_at: str, data_timestamp: str, ok: bool
) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'a.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    cache = tmp_path / 'cache'
    if ok:
        A.ingest_local_ctgov_records(local_src, cache, fetched_at=fetched_at, data_timestamp=data_timestamp)
        return
    with pytest.raises(ValueError, match='is later than fetched_at'):
        A.ingest_local_ctgov_records(local_src, cache, fetched_at=fetched_at, data_timestamp=data_timestamp)
    assert not cache.exists()
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--fetched-at', fetched_at, '--data-timestamp', data_timestamp]) == 1


# ---------------------------------------------------------------- 3. Partial-date interval bounds

@pytest.mark.parametrize(
    'start_s, end_s, expected_state, expected_exact, expected_bounds',
    [
        ('2020-01-15', '2020-10-05', 'exact', 264, [264, 264]),
        # Negative exact days when publication precedes primary completion
        ('2021-06-10', '2021-05-01', 'exact', -40, [-40, -40]),
        # Leap-year February 2020 (2020-02-01..2020-02-29) -> 2021-06-10: NEVER invents 2020-02-01!
        ('2020-02', '2021-06-10', 'bounded_partial', None, [467, 495]),
        # Non-leap February 2021 (2021-02-01..2021-02-28) -> 2021-03-01: [1, 28] days
        ('2021-02', '2021-03-01', 'bounded_partial', None, [1, 28]),
        # Year precision non-overlapping: 2020 (Jan 1..Dec 31) -> 2022-01-01: [366, 731] days
        ('2020', '2022-01-01', 'bounded_partial', None, [366, 731]),
        # Overlapping partial ranges -> indeterminate_at_recorded_precision
        ('2021-06', '2021-06-15', 'indeterminate_at_recorded_precision', None, None),
        ('2021', '2021-09', 'indeterminate_at_recorded_precision', None, None),
        ('2021-05', '2021-05', 'indeterminate_at_recorded_precision', None, None),
        # Missing endpoints -> endpoint_missing
        (None, '2021-06-10', 'endpoint_missing', None, None),
        ('2021-06-10', None, 'endpoint_missing', None, None),
    ],
)
def test_partial_date_intervals_never_invent_day_one(
    start_s: str | None,
    end_s: str | None,
    expected_state: str,
    expected_exact: int | None,
    expected_bounds: list[int] | None,
) -> None:
    iv = T.interval_between(start_s, end_s)
    assert iv['state'] == expected_state
    assert iv['exact_days'] == expected_exact
    assert iv['bounds_days'] == expected_bounds


def test_qc_lag_and_submission_infos_cycles_preserve_absence_vs_presence(built_files: dict[str, Any]) -> None:
    t1 = _timeline_of(built_files, 'NCT09990001')
    assert t1['qc_lag']['state'] == 'exact'
    assert t1['qc_lag']['exact_days'] == 56
    assert t1['submission_infos']['present'] is True
    assert t1['submission_infos']['cycle_count'] == 2
    assert t1['submission_infos']['n_with_unrelease_date'] == 1
    assert t1['submission_infos']['n_with_reset_date'] == 1

    # Absent submissionInfos is recorded as None with reason, NEVER coerced to 0
    t2 = _timeline_of(built_files, 'NCT09990002')
    assert t2['submission_infos']['present'] is False
    assert t2['submission_infos']['cycle_count'] is None
    assert t2['submission_infos']['absent_reason'] == 'submissionInfos_absent'


def _with_submission_infos(value: Any) -> dict[str, Any]:
    return {'protocolSection': {'statusModule': {}},
            'derivedSection': {'miscInfoModule': {'submissionTracking': {'submissionInfos': value}}}}


def test_submission_infos_are_read_where_ctgov_records_them() -> None:
    # NCT02562443's stored record holds its one cycle here; on 30e1cae the module read
    # protocolSection.statusModule, where CT.gov does not record them, and published it as absent.
    study = _with_submission_infos([{'releaseDate': '2024-01-31', 'resetDate': '2024-02-27'}])
    got = T.extract_submission_infos(study)
    assert (got['present'], got['cycle_count'], got['n_with_reset_date'], got['absent_reason']) == (True, 1, 1, None)
    # The old location is not one CT.gov writes, so a history there is not read.
    old_place = {'protocolSection': {'statusModule': {'submissionInfos': [{'releaseDate': '2024-01-31'}]}}}
    assert T.extract_submission_infos(old_place)['absent_reason'] == 'submissionInfos_absent'
    assert T.extract_submission_infos(None)['absent_reason'] == 'submissionInfos_absent'
    assert T.extract_submission_infos({'derivedSection': {'miscInfoModule': {}}})['absent_reason'] == \
        'submissionInfos_absent'


@pytest.mark.parametrize(('study', 'reason'), [
    # On 30e1cae a dropped entry lowered cycle_count and a mistyped date was read as none, with no reason.
    (_with_submission_infos([{'releaseDate': '2020-08-10'}, 'garbage']), 'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': '2020-08-10'}, None]), 'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': '2020-08-10', 'unreleaseDate': 20200828}]),
     'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': '2020-08-10', 'resetDate': ['2020-09-10']}]),
     'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': 20200810}]), 'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': '2020-08-10', 'mcpReleaseN': '1'}]), 'submissionInfos_malformed_entry'),
    (_with_submission_infos([{'releaseDate': '2020-08-10', 'mcpReleaseN': True}]), 'submissionInfos_malformed_entry'),
    (_with_submission_infos({'releaseDate': '2020-08-10'}), 'submissionInfos_malformed_not_a_list'),
    ({'derivedSection': {'miscInfoModule': ['submissionTracking']}}, 'submissionInfos_parent_not_an_object'),
    ({'derivedSection': 'none'}, 'submissionInfos_parent_not_an_object'),
])
def test_unreadable_submission_infos_are_published_without_counts(study: dict[str, Any], reason: str) -> None:
    got = T.extract_submission_infos(study)
    assert got == {'present': False, 'cycle_count': None, 'n_with_unrelease_date': None, 'n_with_reset_date': None,
                   'absent_reason': reason, 'cycles': None}


def test_submission_infos_with_null_fields_are_read() -> None:
    # A null field is absent, as CT.gov omits it; it is not a malformed entry.
    got = T.extract_submission_infos(_with_submission_infos(
        [{'releaseDate': '2020-08-10', 'unreleaseDate': None, 'resetDate': None, 'mcpReleaseN': None}, {}]))
    counts = (got['present'], got['cycle_count'], got['n_with_unrelease_date'], got['n_with_reset_date'])
    assert counts == (True, 2, 0, 0)


# ---------------------------------------------------------------- 4. Window 365d & discordance states

def test_all_seven_descriptive_365d_window_states_and_no_compliance_claims(built_files: dict[str, Any]) -> None:
    expected = {
        'NCT09990001': 'POSTED_WITHIN_365D',
        'NCT09990002': 'UNREPORTED_PAST_365D',
        'NCT09990003': 'PENDING_SUBMITTED_UNPOSTED',
        'NCT09990004': 'POSTED_AFTER_365D',
        'NCT09990005': 'UNREPORTED_WITHIN_365D',
        'NCT09990006': 'DATE_PRECISION_INDETERMINATE',
        'NCT09990007': 'COMPLETION_DATE_MISSING_OR_FUTURE',
        'NCT09990008': 'COMPLETION_DATE_MISSING_OR_FUTURE',
    }
    observed = {t['nct_id']: t['window_365d']['state'] for t in built_files['timelines.json']['timelines']}
    assert observed == expected
    # RESULTS_STATUS_INDETERMINATE (no explicit hasResults=false) has its own test below; the fixture
    # cohort, whose records all carry a boolean hasResults, exercises the other seven states.
    assert set(observed.values()) == set(T.WINDOW_STATES_365D) - {'RESULTS_STATUS_INDETERMINATE'}
    blob = json.dumps(built_files)
    assert not ART.FORBIDDEN_COMPLIANCE_RE.search(blob)


def test_all_six_cross_source_discordance_states(built_files: dict[str, Any]) -> None:
    expected = {
        'NCT09990001': 'concordant_multi_source_disclosed',
        'NCT09990002': 'published_results_while_ctgov_unreported',
        'NCT09990003': 'fda_cited_while_ctgov_unreported',
        'NCT09990004': 'ctgov_posted_no_linked_results_publication',
        'NCT09990005': 'unreported_across_completed_routes',
        'NCT09990006': 'coverage_incomplete_or_awaiting_review',
        'NCT09990007': 'unreported_across_completed_routes',
        'NCT09990008': 'coverage_incomplete_or_awaiting_review',
    }
    observed = {t['nct_id']: t['discordance']['state'] for t in built_files['timelines.json']['timelines']}
    assert observed == expected
    assert set(observed.values()) == set(T.DISCORDANCE_STATES)


# ---------------------------------------------------------------- 5. 5-route x 7-state coverage

def test_coverage_matrix_distinguishes_all_seven_states_and_never_conflates_not_searched(
    tmp_path: Path,
    built_files: dict[str, Any],
) -> None:
    assert _cell_of(built_files, 'NCT09990001', 'ctgov_results')['state'] == 'verified_link'
    assert _cell_of(built_files, 'NCT09990001', 'documentary_statements')['state'] == 'not_searched'
    assert _cell_of(built_files, 'NCT09990005', 'literature_search')['state'] == 'no_candidate_in_completed_scope'
    assert _cell_of(built_files, 'NCT09990006', 'openfda_drugsfda')['state'] == 'candidate_awaiting_review'
    assert _cell_of(built_files, 'NCT09990007', 'registry_references')['state'] == 'available_document_insufficient'
    assert _cell_of(built_files, 'NCT09990008', 'ctgov_results')['state'] == 'document_unavailable'
    assert _cell_of(built_files, 'NCT09990008', 'literature_search')['state'] == 'search_incomplete_or_blocked'

    all_states_seen = {
        cell['state']
        for row in built_files['coverage.json']['trials']
        for cell in row['routes']
    }
    assert all_states_seen == set(C.STATES)

    # Local CT.gov-only cache (literature & openfda never acquired): must report `not_searched`,
    # NEVER `no_candidate_in_completed_scope` or `unreported_across_completed_routes`!
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    for nct_id in ('NCT09990004', 'NCT09990005'):
        (local_src / f'{nct_id}.json.gz').write_bytes(
            gzip.compress(json.dumps(F.STUDIES[nct_id]).encode('utf-8'), mtime=0)
        )
    local_cache_dir = tmp_path / 'local_cache'
    A.ingest_local_ctgov_records(local_src, local_cache_dir, **LOCAL_STAMPS, frozen_at=F.GENERATED_AT)
    local_built = ART.build_artifacts(
        A.load_frozen_cache(local_cache_dir),
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(local_built) == []
    assert _cell_of(local_built, 'NCT09990005', 'literature_search')['state'] == 'not_searched'
    assert _cell_of(local_built, 'NCT09990005', 'openfda_drugsfda')['state'] == 'not_searched'
    assert _coverage_of(local_built, 'NCT09990005')['overall']['state'] == 'not_searched'
    assert _timeline_of(local_built, 'NCT09990005')['discordance']['state'] == 'coverage_incomplete_or_awaiting_review'
    assert _timeline_of(local_built, 'NCT09990004')['discordance']['state'] == 'coverage_incomplete_or_awaiting_review'


# ---------------------------------------------------------------- 6. Human reviews & statements

def test_human_review_overrides_statements_orphans_and_private_key_stripping(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    built_files: dict[str, Any],
) -> None:
    # Unreviewed links are NEVER called "validated"
    for lnk in built_files['links.json']['links']:
        assert lnk['review_status'] == 'unreviewed'
        assert E.link_problems(lnk) == []

    # Locate the unreviewed EPMC link on NCT09990006 and the RESULT link on NCT09990002
    epmc_06 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990006' and x['source_route'] == 'literature_search'
    )
    res_02 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990002' and x['source_route'] == 'registry_references'
    )
    fda_cand_06 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990006' and x['source_route'] == 'openfda_drugsfda'
    )

    stmt_csv = tmp_path / 'documentary_statements.csv'
    with stmt_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.STATEMENT_COLUMNS)
        w.writerow([
            'stmt-001',
            'NCT09990001',
            'registry_record',
            'protocolSection.descriptionModule.briefSummary',
            '2021-01',
            'primary endpoint met at Week 24',
            'Curator Jane',
            'Verbatim summary quote in frozen CT.gov study record',
        ])

    # First build with statement only to get its deterministic `link_id`
    rev_empty = tmp_path / 'reviews_empty.csv'
    rev_empty.write_text(','.join(E.REVIEW_COLUMNS) + '\n', encoding='utf-8')
    b_stmt = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_empty,
        statements_path=stmt_csv,
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    stmt_link = next(
        x for x in b_stmt['links.json']['links']
        if x['source_route'] == 'documentary_statements'
    )
    assert stmt_link['automated_validation'] == 'quote_confirmed_in_source'
    assert _cell_of(b_stmt, 'NCT09990001', 'documentary_statements')['state'] == 'candidate_awaiting_review'

    # Now apply human reviews:
    # - Confirm NCT09990006 EPMC paper as `confirmed_same_trial_results` -> flips discordance to published_results_while_ctgov_unreported!
    # - Reclassify NCT09990002 RESULT ref as `confirmed_background_only` -> removes false results-pub claim!
    # - Reject NCT09990006 heuristic FDA link as `rejected_false_positive`
    # - Confirm statement link as `confirmed_same_trial_results` -> flips documentary_statements cell to `verified_link`!
    # - Include an orphaned review `link:0000000000000000`
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-01',
                   'confirmed_same_trial_results', 'Reports primary trial results'])
        w.writerow([res_02['link_id'], 'NCT09990002', 'Dr. Fictional', '2026-11-01',
                   'confirmed_background_only', 'Prior phase 1 background paper'])
        w.writerow([fda_cand_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-01',
                   'rejected_false_positive', 'Different formulation NDA'])
        w.writerow([stmt_link['link_id'], 'NCT09990001', 'Dr. Fictional', '2026-11-01',
                   'confirmed_same_trial_results', 'Confirmed statement'])
        w.writerow(['link:0000000000000000', 'NCT09990001', 'Dr. Fictional', '2026-11-01',
                   'indeterminate', 'Old link whose text changed'])

    reviewed_build = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_csv,
        statements_path=stmt_csv,
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(reviewed_build) == []
    assert reviewed_build['links.json']['orphaned_reviews'] == ['link:0000000000000000']
    assert reviewed_build['run.json']['reviews'] == {
        'n_rows': 5,
        'n_applied': 4,
        'orphaned_link_ids': ['link:0000000000000000'],
        'out_of_scope_link_ids': [],
    }
    assert _timeline_of(reviewed_build, 'NCT09990006')['discordance']['state'] == 'published_results_while_ctgov_unreported'
    assert _timeline_of(reviewed_build, 'NCT09990002')['discordance']['state'] == 'unreported_across_completed_routes'
    assert _cell_of(reviewed_build, 'NCT09990001', 'documentary_statements')['state'] == 'verified_link'

    # Private `reviewer` and `recorded_by` names never appear anywhere in published JSON
    dumped = json.dumps(reviewed_build)
    assert 'Dr. Fictional' not in dumped
    assert 'Curator Jane' not in dumped


def test_indeterminate_ctgov_results_review_and_human_rejected_epmc_hits(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    built_files: dict[str, Any],
) -> None:
    ctgov_04 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990004' and x['source_route'] == 'ctgov_results'
    )
    epmc_06 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990006' and x['source_route'] == 'literature_search'
    )

    rev_csv = tmp_path / 'reviews_indeterminate.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([ctgov_04['link_id'], 'NCT09990004', 'Dr. Reviewer', '2026-11-01',
                   'indeterminate', 'Pending QC verification'])
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Reviewer', '2026-11-01',
                   'rejected_false_positive', 'Mentions unrelated trial'])

    res = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_csv,
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(res) == []
    assert _cell_of(res, 'NCT09990004', 'ctgov_results')['state'] == 'candidate_awaiting_review'
    assert _timeline_of(res, 'NCT09990004')['discordance']['state'] == 'coverage_incomplete_or_awaiting_review'

    epmc_cell = _cell_of(res, 'NCT09990006', 'literature_search')
    assert epmc_cell['state'] == 'no_candidate_in_completed_scope'
    assert 'rejected as false positives by human review' in epmc_cell['reason']
    assert '0 publications mentioning' not in epmc_cell['reason']


def test_transport_timeout_status_none_logs_search_incomplete_and_retries_on_resume(tmp_path: Path) -> None:
    attempts = [0]

    def flaky_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-01T08:00:00'})
        if 'NCT09990001' in url and attempts[0] < A.MAX_ATTEMPTS:
            attempts[0] += 1
            return None, {}, b'', 'TimeoutError: simulated socket timeout'
        return F.stub_get()(url, params)

    cache_dir = tmp_path / 'timeout_cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    ct_log = A.ResponseLog(A.raw_log_path(cache_dir, 'ctgov'))
    lit_log = A.ResponseLog(A.raw_log_path(cache_dir, 'literature'))
    fda_log = A.ResponseLog(A.raw_log_path(cache_dir, 'openfda'))

    # Run 1: NCT09990001 fails all 4 attempts with status=None
    res1 = A.fetch_ctgov_studies(['NCT09990001'], ct_log, flaky_get, throttle, run_stamp='2026-11-01')
    assert res1 == {'studies_answered': 0, 'studies_unanswered': 1}
    assert ct_log.get('ctgov/study/NCT09990001') is not None
    assert ct_log.get('ctgov/study/NCT09990001')['status'] is None  # type: ignore[index]
    assert ct_log.settled('ctgov/study/NCT09990001') is None

    # Also fail EPMC and openFDA with transport status=None
    def always_timeout_get(url: str, params: dict[str, Any] | None) -> A.Response:
        return None, {}, b'', 'ConnectionError: offline'

    A.fetch_literature(['NCT09990001'], {'NCT09990001': None}, lit_log, always_timeout_get, throttle)
    A.fetch_openfda(['NCT09990001'], {'NCT09990001': None}, fda_log, always_timeout_get, throttle)
    A.freeze_cache(cache_dir, ['NCT09990001'], frozen_at=F.GENERATED_AT)

    built_timeout = ART.build_artifacts(
        A.load_frozen_cache(cache_dir),
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(built_timeout) == []
    # Every attempted-but-timed-out route MUST be `search_incomplete_or_blocked`, NEVER `not_searched`!
    for route in ('ctgov_results', 'registry_references', 'literature_search', 'openfda_drugsfda'):
        assert _cell_of(built_timeout, 'NCT09990001', route)['state'] == 'search_incomplete_or_blocked', route

    # Run 2 (resume): because status=None is unsettled, fetch_ctgov_studies retries and succeeds!
    res2 = A.fetch_ctgov_studies(['NCT09990001'], ct_log, flaky_get, throttle, run_stamp='2026-11-01')
    assert res2 == {'studies_answered': 1, 'studies_unanswered': 0}
    assert ct_log.settled('ctgov/study/NCT09990001') is not None
    assert ct_log.settled('ctgov/study/NCT09990001')['status'] == 200  # type: ignore[index]


def test_malformed_reviews_and_statements_raise_immediately(tmp_path: Path) -> None:
    bad_rev = tmp_path / 'bad_reviews.csv'
    bad_rev.write_text(
        ','.join(E.REVIEW_COLUMNS) + '\n'
        'link:1234567890abcdef,NCT09990001,Reviewer,2026-11-02,unreviewed,cannot set unreviewed in CSV\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='decision'):
        E.read_reviews(bad_rev)

    bad_stmt = tmp_path / 'bad_statements.csv'
    bad_stmt.write_text(
        ','.join(E.STATEMENT_COLUMNS) + '\n'
        's1,NCT09990001,registry_record,protocolSection,not-a-date,quote,Rec,note\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='statement_date'):
        E.read_statements(bad_stmt)


# ---------------------------------------------------------------- 7. Sponsor 3-state separation

def test_sponsor_rollups_separate_three_states_and_both_ownership_views(built_files: dict[str, Any]) -> None:
    sp = built_files['sponsors.json']
    assert sp['partition_summary']['literals'] == {
        'attributed': 4,          # Wyeth...Pfizer, BMS & Pfizer, Pfizer Inc., Pfizer
        'reviewed_excluded': 1,   # Wake Forest University Health Sciences
        'unreviewed': 1,          # Fictional BioTherapeutics GmbH
    }
    assert sp['partition_summary']['trials_by_lead_review_state'] == {
        'attributed': 4,          # NCT09990001, 02, 03, 06
        'reviewed_excluded': 1,   # NCT09990004
        'unreviewed': 2,          # NCT09990005, 07
        'study_not_retrieved': 1, # NCT09990008
    }

    by_owner = {r['canonical']: r for r in sp['by_current_owner']}
    # Pfizer gets NCT09990001 (Wyeth subsidiary), NCT09990002 (BMS & Pfizer shared=yes), NCT09990003, NCT09990006 = 4 distinct trials
    assert by_owner['Pfizer']['n_trials_any'] == 4
    assert sorted(by_owner['Pfizer']['constituent_entities']) == ['bristol-meyers squibb and pfizer', 'pfizer', 'wyeth']
    assert by_owner['Bristol-Myers Squibb']['n_trials_any'] == 1

    by_entity = {r['entity']: r for r in sp['by_as_registered']}
    # Under `as_registered`, Wyeth stays `wyeth` (1 trial) rather than rolling into `pfizer` (2 trials)
    assert by_entity['wyeth']['n_trials_any'] == 1
    assert by_entity['wyeth']['canonicals_current_owner'] == ['Pfizer']
    assert by_entity['pfizer']['n_trials_any'] == 2

    assert [r['literal_name'] for r in sp['reviewed_excluded_literals']] == ['Wake Forest University Health Sciences']
    assert [r['literal_name'] for r in sp['unreviewed_literals']] == ['Fictional BioTherapeutics GmbH']


# ---------------------------------------------------------------- 8. Determinism, contract & export

def test_byte_stable_run_identity_and_summary_count_objects(
    frozen_cache: A.FrozenCache,
    built_files: dict[str, Any],
) -> None:
    # Rebuild with a different `generated_at` timestamp: `run_identity`, `run_id`, and all sibling files are byte-identical!
    second = ART.build_artifacts(
        frozen_cache,
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at='2026-12-31T23:59:59+00:00',
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert second['run.json']['run_identity'] == built_files['run.json']['run_identity']
    assert second['run.json']['run_id'] == built_files['run.json']['run_id']
    for name in ART.FILES:
        assert ART.dump(second[name]) == ART.dump(built_files[name])

    # Changing `as_of` changes `run_identity` and `run_id`
    different = ART.build_artifacts(
        frozen_cache,
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date(2026, 10, 31),
    )
    assert different['run.json']['run_identity'] != built_files['run.json']['run_identity']

    # Every count object in summary.json carries value, numerator, denominator, definition
    summary = built_files['summary.json']
    for m_obj in summary['metrics']:
        assert {'value', 'numerator', 'denominator', 'definition'} <= set(m_obj)
        assert m_obj['value'] == m_obj['numerator']
    for tname, table in summary['tables'].items():
        for v1 in table.values():
            if tname == 'coverage_by_route':
                for v2 in v1.values():
                    assert {'value', 'numerator', 'denominator', 'definition'} <= set(v2)
            else:
                assert {'value', 'numerator', 'denominator', 'definition'} <= set(v1)


def test_contract_checker_passes_valid_build_and_refuses_tampering(
    tmp_path: Path,
    built_dir: Path,
    built_files: dict[str, Any],
) -> None:
    assert cli_check.main([str(built_dir)]) == 0

    # 1. On-disk byte tampering (even whitespace change)
    tampered_dir = tmp_path / 'tampered_bytes'
    shutil.copytree(built_dir, tampered_dir)
    (tampered_dir / 'summary.json').write_bytes((tampered_dir / 'summary.json').read_bytes() + b' ')
    assert cli_check.main([str(tampered_dir)]) == 1

    # 2. Tampered summary numerator (with recomputed file hash -> fails run_identity & recount parity)
    bad_summary = copy.deepcopy(built_files)
    bad_summary['summary.json']['metrics'][0]['numerator'] = 999
    bad_summary['summary.json']['metrics'][0]['value'] = 999
    probs = ART.check_contract(bad_summary)
    assert any('outside 0..' in p or 'sha256' in p for p in probs)

    # 3. Leaked private key `reviewer`
    leaked = copy.deepcopy(built_files)
    leaked['links.json']['links'][0]['reviewer'] = 'Secret Name'
    assert any('private review keys leaked' in p for p in ART.check_contract(leaked))

    # 4. Forbidden statutory non-compliance wording
    bad_words = copy.deepcopy(built_files)
    bad_words['timelines.json']['timelines'][0]['window_365d']['reason'] = 'Trial is in FDAAA violation'
    assert any('forbidden statutory non-compliance' in p for p in ART.check_contract(bad_words))


def test_site_export_stages_atomically_verifies_hashes_and_protects_unrelated_dirs(
    tmp_path: Path,
    built_dir: Path,
) -> None:
    site_out = tmp_path / 'site' / 'disclosure'
    assert cli_export.main([str(built_dir), str(site_out)]) == 0

    exported_run = json.loads((site_out / 'run.json').read_text(encoding='utf-8'))
    assert exported_run['build_run_id'] == json.loads((built_dir / 'run.json').read_text(encoding='utf-8'))['run_id']
    for fname, digest in exported_run['files'].items():
        actual = hashlib.sha256((site_out / fname).read_bytes()).hexdigest()
        assert actual == digest
        parsed = json.loads((site_out / fname).read_text(encoding='utf-8'))
        assert parsed['run_id'] == exported_run['run_id']

    # Re-exporting over an existing valid disclosure export succeeds atomically
    assert cli_export.main([str(built_dir), str(site_out)]) == 0

    # Exporting from a tampered build is refused and leaves existing valid `site_out` untouched
    bad_build = tmp_path / 'bad_build'
    shutil.copytree(built_dir, bad_build)
    (bad_build / 'links.json').write_bytes(b'{"corrupt":true}\n')
    before_bytes = (site_out / 'summary.json').read_bytes()
    assert cli_export.main([str(bad_build), str(site_out)]) == 1
    assert (site_out / 'summary.json').read_bytes() == before_bytes

    # Refuses to overwrite a directory containing unrelated files
    unrelated = tmp_path / 'unrelated_dir'
    unrelated.mkdir()
    (unrelated / 'important.txt').write_text('do not delete', encoding='utf-8')
    assert cli_export.main([str(built_dir), str(unrelated)]) == 1
    assert (unrelated / 'important.txt').read_text(encoding='utf-8') == 'do not delete'


def test_composite_earliest_milestone_bounds_and_historical_as_of() -> None:
    # Overlapping partial ('2021') and exact ('2021-05-10') links narrow earliest to [2021-01-01, 2021-05-10]
    ms = T.earliest_link_milestone([
        {'link_id': 'link:0000000000000001', 'publication_or_action_date': '2021'},
        {'link_id': 'link:0000000000000002', 'publication_or_action_date': '2021-05-10'},
    ])
    assert ms['precision'] == 'bounded_composite'
    assert (ms['earliest_bound'], ms['latest_bound']) == ('2021-01-01', '2021-05-10')
    iv = T.interval_from_milestones(T.milestone_from_raw('2020-01-01'), ms)
    assert iv['state'] == 'bounded_partial'
    assert iv['exact_days'] is None
    assert iv['bounds_days'] == [366, 495]

    # Historical `as_of` prior to `resultsFirstPostDate`: NCT09990001 posted 2020-10-05, submitted 2020-08-10
    assert T.classify_window_365d(F.STUDIES['NCT09990001'], date(2020, 9, 1))['state'] == 'PENDING_SUBMITTED_UNPOSTED'
    assert T.classify_window_365d(F.STUDIES['NCT09990001'], date(2020, 5, 1))['state'] == 'UNREPORTED_WITHIN_365D'


def test_conflicting_sponsor_rules_raise_value_error(tmp_path: Path) -> None:
    bad_rules = tmp_path / 'bad_aliases.csv'
    bad_rules.write_text(
        'canonical,rule_type,pattern,status,shared,note\n'
        'Company A,exact_normalized,pfizer,attribute,no,first claim\n'
        'Company B,exact_normalized,pfizer,attribute,no,conflicting non-shared claim\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='conflicting rules'):
        T.load_sponsor_context({'NCT09990006': F.STUDIES['NCT09990006']}, ['NCT09990006'], bad_rules)


# ---------------------------------------------------------------- 9. Typing, Python 3.11 & real smoke

def test_every_function_is_fully_type_annotated_zero_rng_and_python311_compatible() -> None:
    import io
    import tokenize

    py_files = sorted((ROOT / 'disclosure').glob('*.py')) + [
        ROOT / 'scripts' / 'build_drug_disclosure.py',
        ROOT / 'scripts' / 'check_drug_disclosure_contract.py',
        ROOT / 'scripts' / 'export_drug_disclosure_site.py',
    ]
    all_checked = py_files + sorted((ROOT / 'tests' / 'disclosure').glob('*.py'))
    for path in all_checked:
        src = path.read_text(encoding='utf-8')
        assert 'from __future__ import annotations' in src, f'{path.name} missing __future__ annotations'
        # Verify zero Python 3.12+ PEP 701 f-string quote reuse or backslashes inside `{...}`
        fstack: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            tname = tokenize.tok_name[tok.type]
            if tname == 'FSTRING_START':
                fstack.append(tok.string.lstrip('fFrRbBuU'))
            elif tname == 'FSTRING_END' and fstack:
                fstack.pop()
            elif fstack and tname == 'STRING':
                assert fstack[-1] not in tok.string, (
                    f'{path.name}:{tok.start[0]} reuses quote {fstack[-1]!r} inside f-string (Python 3.12+ PEP 701)'
                )
            elif fstack and tname != 'FSTRING_MIDDLE':
                assert '\\' not in tok.string, (
                    f'{path.name}:{tok.start[0]} uses backslash inside f-string expression (Python 3.12+ PEP 701)'
                )

    for path in py_files:
        src = path.read_text(encoding='utf-8')
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            # Forbid random/RNG imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != 'random', f'{path.name} imports random'
            if isinstance(node, ast.ImportFrom):
                assert node.module != 'random', f'{path.name} imports from random'
            # Forbid Python 3.12+ PEP 695 type aliases / type parameters
            assert type(node).__name__ not in ('TypeAlias', 'TypeVar', 'ParamSpec', 'TypeVarTuple'), (
                f'{path.name} uses Python 3.12+ PEP 695 syntax'
            )
            # Enforce full parameter and return annotations on every function/method
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.returns is not None, f'{path.name}:{node.lineno} {node.name}() lacks return type annotation'
                all_args = (
                    list(node.args.posonlyargs)
                    + list(node.args.args)
                    + list(node.args.kwonlyargs)
                )
                if node.args.vararg:
                    all_args.append(node.args.vararg)
                if node.args.kwarg:
                    all_args.append(node.args.kwarg)
                for arg in all_args:
                    if arg.arg in ('self', 'cls'):
                        continue
                    assert arg.annotation is not None, (
                        f'{path.name}:{node.lineno} {node.name}() parameter {arg.arg!r} lacks type annotation'
                    )


def test_real_stored_ctgov_records_offline_smoke_pipeline(tmp_path: Path) -> None:
    real_archive = ROOT / 'docs' / 'audits' / 'outcomes_results_structure' / 'first_pass' / 'cache' / 'raw_responses.jsonl.gz'
    if not real_archive.is_file():
        pytest.skip('tracked real CT.gov audit cache not present')

    cache_dir = tmp_path / 'smoke_cache'
    build_out = tmp_path / 'smoke_build'
    site_out = tmp_path / 'smoke_site'

    assert cli_build.main([
        'freeze-local',
        '--records', str(real_archive),
        '--cache', str(cache_dir),
        '--limit', '25',
        # When the tracked first-pass cache was fetched, and CT.gov's dataTimestamp then (its fetch_manifest.json).
        '--fetched-at', '2026-11-13T22:32:29+00:00',
        '--data-timestamp', '2026-11-13T10:00:06',
    ]) == 0
    assert cli_build.main([
        'build',
        '--cache', str(cache_dir),
        '--out', str(build_out),
        '--as-of', '2026-11-01',
        # No --generated-at: freeze-local stamps the cache with the current time, and a build cannot be
        # generated before the cache it reads (generated_before_frozen_problem).
    ]) == 0
    assert cli_check.main([str(build_out)]) == 0
    assert cli_export.main([str(build_out), str(site_out)]) == 0

    summary = json.loads((site_out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['denominators']['included_trials']['n'] == 25
    assert summary['denominators']['retrieved_trials']['n'] == 25


# ---------------------------------------------------------------- 10. Milestone bounds, drug terms, statements, openFDA

def _qualifying_result_ref(link_id: str, pub_date: str | None) -> dict[str, Any]:
    """A PubMed-resolved CT.gov RESULT reference: qualifies as a results publication while unreviewed."""
    return {
        'link_id': link_id,
        'source_route': 'registry_references',
        'citation_type': 'RESULT',
        'automated_validation': 'identifier_confirmed',
        'review_status': 'unreviewed',
        'publication_or_action_date': pub_date,
    }


def test_undated_qualifying_link_leaves_no_lower_bound_and_no_lag(
    built_files: dict[str, Any],
) -> None:
    dated = _qualifying_result_ref('link:00000000000000a1', '2021-05-10')
    undated = _qualifying_result_ref('link:00000000000000a2', None)

    # The undated link may precede 2021-05-10, so only the upper bound survives.
    ms = T.earliest_link_milestone([dated, undated])
    assert ms == {
        'date': None,
        'precision': 'bounded_above_only',
        'earliest_bound': None,
        'latest_bound': '2021-05-10',
        'link_ids': ['link:00000000000000a1', 'link:00000000000000a2'],
        'has_undated_qualifying_link': True,
    }

    # No lag from it, as either endpoint, at day or partial precision on the other side.
    for other in ('2020-01-01', '2020', '2022-01-01'):
        for iv in (
            T.interval_from_milestones(T.milestone_from_raw(other), ms),
            T.interval_from_milestones(ms, T.milestone_from_raw(other)),
        ):
            assert iv['state'] == 'indeterminate_at_recorded_precision'
            assert (iv['exact_days'], iv['min_days'], iv['max_days'], iv['bounds_days']) == (None, None, None, None)
            assert iv['one_sided_endpoints'] in (['start'], ['end'])
    # An endpoint with neither bound stays `endpoint_missing`, ahead of a one-sided one.
    iv_missing = T.interval_from_milestones(T.milestone_from_raw(None), ms)
    assert iv_missing['state'] == 'endpoint_missing'
    assert iv_missing['missing_endpoints'] == ['start']

    # The assembled timeline carries the one-sided milestone and computes no publication lag.
    tl = T.build_trial_timeline(
        'NCT09990001',
        F.STUDIES['NCT09990001'],
        [dated, undated],
        _coverage_of(built_files, 'NCT09990001'),
        {},
        date.fromisoformat(F.AS_OF),
    )
    assert tl['milestones']['earliest_results_publication']['precision'] == 'bounded_above_only'
    for name in (
        'results_first_posted_to_earliest_results_publication',
        'primary_completion_to_earliest_results_publication',
        'earliest_results_publication_to_earliest_fda_action',
    ):
        assert tl['intervals'][name]['exact_days'] is None
        assert tl['intervals'][name]['bounds_days'] is None

    # The contract checker refuses an exact date beside an undated link, and a lag from a one-sided endpoint.
    assert 'bounded_above_only' in built_files['run.json']['vocabularies']['milestone_precisions']
    old_style = copy.deepcopy(built_files)
    old_tl = old_style['timelines.json']['timelines'][0]
    old_tl['milestones']['earliest_results_publication'] = {
        'date': '2021-05-10',
        'precision': 'day',
        'earliest_bound': '2021-05-10',
        'latest_bound': '2021-05-10',
        'link_ids': ['link:00000000000000a1', 'link:00000000000000a2'],
        'has_undated_qualifying_link': True,
    }
    old_tl['intervals']['primary_completion_to_earliest_results_publication'] = {
        **old_tl['intervals']['primary_completion_to_earliest_results_publication'],
        'end_precision': 'bounded_above_only',
        'state': 'bounded_partial',
        'exact_days': None,
        'min_days': 400,
        'max_days': 480,
        'bounds_days': [400, 480],
    }
    probs = ART.check_contract(old_style)
    assert any('an undated qualifying link leaves no lower bound' in p for p in probs)
    assert any('lag computed from a bounded_above_only endpoint' in p for p in probs)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('DrugX 5 mg/mL', 'drugx'),
        ('DrugX 2 mg/kg', 'drugx'),
        ('Lidocaine 5% cream', 'lidocaine'),  # strength and dosage form both go (see DOSAGE_FORM_WORDS)
        ('DrugX 10 mg', 'drugx'),
        ('Docetaxel 75 mg/m2', 'docetaxel'),
        ('Insulin 100 IU / mL', 'insulin'),
        ('DrugX 0.5%', 'drugx'),
        ('Zorbatinib 10 mg', 'zorbatinib'),
        ('Placebo 5 mg', None),
    ],
)
def test_normalize_drug_term_strips_whole_strength_with_unit_denominator_and_percent(
    raw: str,
    expected: str | None,
) -> None:
    assert A.normalize_drug_term(raw) == expected


def test_statement_locator_that_does_not_resolve_is_never_quote_confirmed() -> None:
    excerpt = 'primary endpoint met at Week 24'
    lit_body = {
        'hitCount': 1,
        'resultList': {'result': [{'title': f'Trial NCT09990001: {excerpt}', 'abstractText': 'Background only.'}]},
    }
    fda_body = {
        'meta': {'results': {'total': 1}},
        'results': [{'application_number': 'NDA000001', 'sponsor_name': 'Fictional', 'note': excerpt}],
    }
    literature_records = {'literature/epmc_nct/NCT09990001': {'status': 200, 'body': lit_body}}
    openfda_records = {'openfda/nct/NCT09990001': {'status': 200, 'body': fda_body}}

    def stmt(sid: str, stype: str, locator: str) -> dict[str, str]:
        return {
            'statement_id': sid,
            'nct_id': 'NCT09990001',
            'source_type': stype,
            'source_locator': locator,
            'statement_date': '2021-01',
            'verbatim_excerpt': excerpt,
            'recorded_by': 'Curator',
            'notes': 'test row',
        }

    rows = [
        # The excerpt stands elsewhere in each body, but not at these locators.
        stmt('lit-missing', 'literature_record', 'resultList.result[0].noSuchField'),
        stmt('lit-unparseable', 'literature_record', 'pmid-not-a-path'),
        stmt('lit-elsewhere', 'literature_record', 'resultList.result[0].abstractText'),
        stmt('fda-missing', 'openfda_record', 'results[0].no_such_field'),
        stmt('fda-elsewhere', 'openfda_record', 'results[0].sponsor_name'),
        # Controls: the excerpt stands at the locator.
        stmt('lit-at', 'literature_record', 'resultList.result[0].title'),
        stmt('fda-at', 'openfda_record', 'results[0].note'),
        # Source absent from the cache.
        stmt('reg-absent', 'registry_record', 'protocolSection.descriptionModule.briefSummary'),
    ]
    links = E.build_statement_links(rows, 'b' * 64, {}, literature_records, openfda_records)
    by_sid = {x['detail']['statement_id']: x for x in links}
    expected = {
        'lit-missing': 'locator_not_resolved',
        'lit-unparseable': 'locator_not_resolved',
        'lit-elsewhere': 'excerpt_not_found_at_locator',
        'fda-missing': 'locator_not_resolved',
        'fda-elsewhere': 'excerpt_not_found_at_locator',
        'lit-at': 'quote_found_at_locator',
        'fda-at': 'quote_found_at_locator',
        'reg-absent': 'source_not_retrieved',
    }
    for sid, check in expected.items():
        lnk = by_sid[sid]
        assert lnk['detail']['statement_check'] == check, sid
        confirmed = check == 'quote_found_at_locator'
        assert (lnk['automated_validation'] == 'quote_confirmed_in_source') is confirmed, sid
        if not confirmed:
            assert lnk['automated_validation'] == 'not_validated', sid
            assert lnk['validation_reasons'][0].startswith(f'{check}:'), sid
        assert E.link_problems(lnk) == [], sid


def test_flooded_generic_query_cannot_hide_precise_querys_reviewable_candidate() -> None:
    study = F._make_study(
        'NCT09990009',
        title='Fictional combination study',
        lead_sponsor='Fictional Trial Sponsor Inc',
        # Sorted query order puts the flooded generic term first, as it claimed the application before.
        drugs=['Acetamigen', 'Zorbaprecise 10 mg'],
    )
    assert A.extract_drug_candidates(study) == ['acetamigen', 'zorbaprecise']

    shared_app = {
        'application_number': 'NDA777001',
        'sponsor_name': 'Unrelated Licensee Pharma',
        'products': [{'brand_name': 'ZORBAPRECISE'}],
        'submissions': [{'submission_type': 'ORIG', 'submission_status': 'AP', 'submission_status_date': '20220301'}],
    }
    flood_apps = [
        {'application_number': f'ANDA{900000 + i}', 'sponsor_name': f'Generic Maker {i}'}
        for i in range(29)
    ]
    openfda_records = {
        A.drug_query_key('acetamigen'): {
            'status': 200,
            'body': {'meta': {'results': {'total': 30}}, 'results': [*flood_apps, shared_app]},
        },
        A.drug_query_key('zorbaprecise'): {
            'status': 200,
            'body': {'meta': {'results': {'total': 1}}, 'results': [shared_app]},
        },
    }
    links = E.build_openfda_links('NCT09990009', study, openfda_records, E.trial_sponsor_match_keys(study))

    shared = [x for x in links if x['target_id'] == 'NDA777001']
    assert len(shared) == 1
    kept = shared[0]
    assert kept['automated_validation'] == 'not_validated'
    assert kept['detail']['matched_drug'] == 'zorbaprecise'
    assert kept['locator']['drug_query'] == 'zorbaprecise'
    assert kept['detail']['drug_queries_returning_application'] == ['acetamigen', 'zorbaprecise']
    # The flooded query's other applications are still set aside by rule, one link each.
    flooded = [x for x in links if x['target_id'].startswith('ANDA')]
    assert len(flooded) == 29 and {x['automated_validation'] for x in flooded} == {'rejected_by_rule'}
    assert all(E.link_problems(x) == [] for x in links)

    # An NCT-cited record outranks a reviewable candidate for the same application, whichever query found it.
    cited_app = {**shared_app, 'application_number': 'NDA777002', 'note': 'Pivotal study NCT09990009'}
    openfda_records[A.drug_query_key('acetamigen')]['body']['results'].append(cited_app)
    openfda_records[A.drug_query_key('acetamigen')]['body']['meta']['results']['total'] = 31
    openfda_records[A.drug_query_key('zorbaprecise')]['body']['results'].append(
        {k: v for k, v in cited_app.items() if k != 'note'}
    )
    openfda_records[A.drug_query_key('zorbaprecise')]['body']['meta']['results']['total'] = 2
    links2 = E.build_openfda_links('NCT09990009', study, openfda_records, E.trial_sponsor_match_keys(study))
    cited = [x for x in links2 if x['target_id'] == 'NDA777002']
    assert len(cited) == 1
    assert cited[0]['automated_validation'] == 'identifier_confirmed'
    assert cited[0]['detail']['matched_drug'] == 'acetamigen'


# ---------------------------------------------------------------- 11. Cache frame, producer digest, link identity

def test_freeze_and_load_refuse_requested_ids_without_a_ctgov_study_record(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
) -> None:
    # A raw cache acquired for the fixture's NCT list, then frozen for a list that adds an unlooked-up ID.
    cache = tmp_path / 'other_list_cache'
    shutil.copytree(frozen_cache.cache_dir, cache)
    manifest_before = (cache / 'manifest.json').read_bytes()
    rc = cli_build.main([
        'acquire', '--cache', str(cache),
        '--nct-id', 'NCT09990001', '--nct-id', 'NCT09991234',
        '--stage', 'freeze',
    ])
    assert rc == 1
    assert (cache / 'manifest.json').read_bytes() == manifest_before
    with pytest.raises(A.CacheIntegrityError, match=r"NCT09991234.*run the ctgov stage"):
        A.freeze_cache(cache, ['NCT09990001', 'NCT09991234'], frozen_at=F.GENERATED_AT)

    # Every logged lookup counts, whatever its status: NCT09990008 is a 404 record and freezes.
    assert frozen_cache.ctgov['ctgov/study/NCT09990008']['status'] == 404
    assert cli_build.main([
        'acquire', '--cache', str(cache),
        '--nct-id', 'NCT09990001', '--nct-id', 'NCT09990008',
        '--stage', 'freeze',
    ]) == 0
    assert A.load_frozen_cache(cache).requested_nct_ids == ['NCT09990001', 'NCT09990008']

    # A manifest edited (consistently) to list an ID with no CT.gov record is refused on load.
    edited = tmp_path / 'edited_manifest_cache'
    shutil.copytree(frozen_cache.cache_dir, edited)
    manifest = json.loads((edited / 'manifest.json').read_text(encoding='utf-8'))
    ids = sorted([*manifest['requested_nct_ids'], 'NCT09991234'])
    manifest.update({
        'requested_nct_ids': ids,
        'n_requested_nct_ids': len(ids),
        'requested_nct_ids_sha256': A.sha256_bytes('\n'.join(ids).encode('utf-8')),
    })
    (edited / 'manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    with pytest.raises(A.CacheIntegrityError, match=r'NCT09991234.*does not cover its own frame'):
        A.load_frozen_cache(edited)


def test_producer_digest_and_dirty_check_cover_the_build_import_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cli_build.producer_paths()
    # The sponsors/ attribution modules the build executes, beside disclosure/ and the CLI scripts.
    for dep in (
        'scripts/build_drug_disclosure.py',
        'scripts/check_drug_disclosure_contract.py',
        'scripts/export_drug_disclosure_site.py',
        'disclosure/__init__.py',
        'disclosure/acquire.py',
        'disclosure/artifacts.py',
        'disclosure/coverage.py',
        'disclosure/evidence.py',
        'disclosure/timeline.py',
        'disclosure/reviews.csv',
        'disclosure/documentary_statements.csv',
        'sponsors/__init__.py',
        'sponsors/adapter.py',
        'sponsors/audit_states.py',
        'sponsors/sponsor_roles.py',
    ):
        assert dep in paths, dep
    assert paths == sorted(set(paths))
    # A repo module this process loaded that the build does not import stays out, so the digest
    # does not depend on what else the process imported.
    importlib.import_module('sponsors.baseline')
    loaded = {p.relative_to(ROOT).as_posix() for p in cli_build._loaded_repo_modules(ROOT).values()}
    assert 'sponsors/baseline.py' in loaded
    assert cli_build.producer_paths() == paths
    assert 'sponsors/baseline.py' not in paths
    assert not any(p.startswith('tests/') for p in paths)

    # The digest covers every listed file: a change to a sponsors/ dependency moves it.
    for rel in paths:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    same = cli_build.source_digest(tmp_path)
    assert same == cli_build.source_digest(ROOT)
    (tmp_path / 'sponsors' / 'adapter.py').write_bytes((ROOT / 'sponsors' / 'adapter.py').read_bytes() + b'\n# changed\n')
    assert cli_build.source_digest(tmp_path) != same

    # The dirty check asks git about the same list, plus the `disclosure` directory so a deleted
    # tracked file there is reported too.
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = 'a' * 40 + '\n'

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _Done:
        calls.append(list(cmd))
        return _Done()

    monkeypatch.setattr(cli_build.subprocess, 'run', _fake_run)
    info = cli_build.producer_metadata(ROOT)
    status_call = next(c for c in calls if c[:2] == ['git', 'status'])
    assert status_call[status_call.index('--') + 1:] == ['disclosure', *paths]
    assert info['source_sha256'] == cli_build.source_digest(ROOT, paths)


def test_link_identity_changes_when_the_action_date_changes_under_the_same_excerpt() -> None:
    study = F._make_study('NCT09990009', title='Fictional study', lead_sponsor='Fictional Trial Sponsor Inc')
    # The NCT ID stands in the submission whose action it supported (a top-level field ties it to no
    # action, so its date would be unknown).
    cited = {'submission_type': 'ORIG', 'submission_status': 'TA', 'submission_status_date': '20220301',
             'submission_public_notes': 'Pivotal study NCT09990009'}
    app = {
        'application_number': 'NDA777003',
        'sponsor_name': 'Fictional Trial Sponsor Inc',
        'submissions': [cited],
    }

    def links_for(application: dict[str, Any], meta_updated: str) -> list[dict[str, Any]]:
        records = {
            'openfda/nct/NCT09990009': {
                'status': 200,
                'body': {'meta': {'last_updated': meta_updated, 'results': {'total': 1}}, 'results': [application]},
            },
        }
        return E.build_openfda_links('NCT09990009', study, records, E.trial_sponsor_match_keys(study))

    before = links_for(app, '2026-11-01')
    # The same application refetched after an unrelated openFDA data refresh: same identity.
    refreshed = links_for(app, '2026-11-08')
    assert before[0]['source_sha256'] != refreshed[0]['source_sha256']
    assert before[0]['link_id'] == refreshed[0]['link_id']

    # The cited submission is approved: the excerpt and locator stay the same, the action date changes.
    approved = {**app, 'submissions': [{**cited, 'submission_status': 'AP', 'submission_status_date': '20230115'}]}
    after = links_for(approved, '2026-11-08')
    assert after[0]['verbatim_excerpt'] == before[0]['verbatim_excerpt']
    assert after[0]['locator'] == before[0]['locator']
    assert (before[0]['publication_or_action_date'], after[0]['publication_or_action_date']) == ('2022-03-01', '2023-01-15')
    assert after[0]['link_id'] != before[0]['link_id']

    # A review keyed on the old identity is reported as orphaned, never carried onto the changed record.
    review = {before[0]['link_id']: {
        'link_id': before[0]['link_id'], 'nct_id': 'NCT09990009', 'reviewer': 'R',
        'reviewed_at': '2026-11-01', 'decision': 'confirmed_same_trial_results', 'notes': 'n',
    }}
    assert E.apply_reviews(after, review) == [before[0]['link_id']]
    assert after[0]['review_status'] == 'unreviewed'


# ---------------------------------------------------------------- 12. openFDA OR query, local-input gaps, ambiguous CT.gov

def test_openfda_drug_query_joins_fields_with_or_and_refuses_records_from_the_old_form(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = A.drug_query_params('lidocaine cream')
    # Both transports form-encode params; the separator must survive as a space (`+`), never `%2B`.
    encoded = urllib.parse.urlencode(params)
    assert '%2B' not in encoded
    assert '+OR+' in encoded
    decoded = urllib.parse.parse_qs(encoded)['search'][0]
    assert decoded.split(' OR ') == [f'{field}:"lidocaine cream"' for field in A.OPENFDA_DRUG_FIELDS]

    # A cache whose drug-query records were made with another query form is refused, at load and at freeze.
    cache = tmp_path / 'old_form_cache'
    shutil.copytree(frozen_cache.cache_dir, cache)

    def next_form(drug_norm: str) -> dict[str, str]:
        return {'search': f'products.brand_name:"{drug_norm}"', 'limit': '100'}

    monkeypatch.setattr(A, 'drug_query_params', next_form)
    with pytest.raises(A.CacheIntegrityError, match='earlier query form'):
        A.load_frozen_cache(cache)
    with pytest.raises(A.CacheIntegrityError, match='earlier query form'):
        A.freeze_cache(cache, F.NCT_IDS, frozen_at=F.GENERATED_AT)

    # Re-running the openfda stage re-fetches exactly those records; a stale record for a term no
    # requested trial has any more is never read and does not block the freeze.
    fda_log = A.ResponseLog(A.raw_log_path(cache, 'openfda'))
    fda_log.put({**fda_log.records[A.drug_query_key('lumirafenib')], 'key': A.drug_query_key('obsoleteterm'),
                 'tag': {'drug_norm': 'obsoleteterm'}})
    studies = A.studies_from_ctgov_records(frozen_cache.ctgov, F.NCT_IDS)
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    A.fetch_openfda(F.NCT_IDS, studies, fda_log, F.stub_get(), throttle)
    assert fda_log.records[A.drug_query_key('lumirafenib')]['params'] == next_form('lumirafenib')
    A.freeze_cache(cache, F.NCT_IDS, frozen_at=F.GENERATED_AT)
    assert A.load_frozen_cache(cache).openfda[A.drug_query_key('floodedgeneric')]['params'] == next_form('floodedgeneric')


def test_freeze_local_refuses_requested_ids_absent_from_the_local_records(tmp_path: Path) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    wanted = tmp_path / 'ids.txt'
    wanted.write_text('NCT09990004\nNCT09990005\n', encoding='utf-8')
    cache = tmp_path / 'local_cache'

    # NCT09990005 is absent locally: refused before anything is written, never logged as a CT.gov 404.
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--nct-list', str(wanted), *LOCAL_STAMP_ARGS]) == 1
    assert not (cache / 'manifest.json').exists()
    assert not A.raw_log_path(cache, 'ctgov').exists()
    with pytest.raises(ValueError, match=r'not in the local CT\.gov records \(\[.NCT09990005.\]\)'):
        A.ingest_local_ctgov_records(local_src, cache, nct_ids=['NCT09990004', 'NCT09990005'], **LOCAL_STAMPS)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--records', str(local_src), '--nct-list', str(wanted),
                           '--out', str(out), '--as-of', F.AS_OF, '--generated-at', F.GENERATED_AT,
                           *LOCAL_STAMP_ARGS]) == 1
    assert not out.exists()

    # With every listed ID present the freeze proceeds, and each record is the local 200 response.
    wanted.write_text('NCT09990004\n', encoding='utf-8')
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--nct-list', str(wanted), *LOCAL_STAMP_ARGS]) == 0
    loaded = A.load_frozen_cache(cache)
    assert loaded.requested_nct_ids == ['NCT09990004']
    assert {k: v['status'] for k, v in loaded.ctgov.items() if k.startswith('ctgov/study/')} == {
        'ctgov/study/NCT09990004': 200,
    }


def test_local_freeze_refuses_differing_snapshots_of_one_trial_and_accepts_identical_copies(
    tmp_path: Path,
) -> None:
    older = copy.deepcopy(F.STUDIES['NCT09990004'])
    newer = copy.deepcopy(older)
    newer['protocolSection']['identificationModule']['officialTitle'] = 'Amended official title'

    # Two files holding different snapshots of NCT09990004: refused before anything is written,
    # rather than freezing whichever file sorts first under the caller's stated timestamps.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'a_older.json').write_text(json.dumps(older), encoding='utf-8')
    (local_src / 'b_newer.json').write_text(json.dumps(newer), encoding='utf-8')
    (local_src / 'c_other.json').write_text(json.dumps(F.STUDIES['NCT09990005']), encoding='utf-8')
    cache = tmp_path / 'cache_dir_conflict'
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           *LOCAL_STAMP_ARGS]) == 1
    assert not (cache / 'manifest.json').exists()
    assert not A.raw_log_path(cache, 'ctgov').exists()
    with pytest.raises(ValueError, match=r'NCT09990004 in a_older\.json and b_newer\.json'):
        A.ingest_local_ctgov_records(local_src, cache, **LOCAL_STAMPS)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--records', str(local_src), '--out', str(out),
                           '--as-of', F.AS_OF, '--generated-at', F.GENERATED_AT, *LOCAL_STAMP_ARGS]) == 1
    assert not out.exists()

    # The same conflict inside one JSONL file (two lines) is refused too.
    jsonl = tmp_path / 'records.jsonl'
    jsonl.write_text(json.dumps(older) + '\n' + json.dumps(newer) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match=r'1 NCT ID\(s\) appear more than once with different records'):
        A.ingest_local_ctgov_records(jsonl, tmp_path / 'cache_jsonl', **LOCAL_STAMPS)

    # A conflicting trial left out by --nct-list is never read, so it does not block the others.
    wanted = tmp_path / 'ids.txt'
    wanted.write_text('NCT09990005\n', encoding='utf-8')
    subset = tmp_path / 'cache_subset'
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(subset),
                           '--nct-list', str(wanted), *LOCAL_STAMP_ARGS]) == 0
    assert A.load_frozen_cache(subset).requested_nct_ids == ['NCT09990005']

    # Byte-identical copies of one record (the same file shipped twice) freeze as one record.
    (local_src / 'b_newer.json').write_text(json.dumps(older), encoding='utf-8')
    same = tmp_path / 'cache_identical'
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(same),
                           *LOCAL_STAMP_ARGS]) == 0
    loaded = A.load_frozen_cache(same)
    assert loaded.requested_nct_ids == ['NCT09990004', 'NCT09990005']
    assert loaded.ctgov['ctgov/study/NCT09990004']['body'] == older


def _study_without_protocol() -> dict[str, Any]:
    return {k: v for k, v in F.STUDIES['NCT09990005'].items() if k != 'protocolSection'}


@pytest.mark.parametrize(('name', 'content'), [
    # On 30e1cae each was skipped, and the freeze went ahead with NCT09990004 alone.
    ('broken.json', lambda: json.dumps(_study_without_protocol())),
    ('page.json', lambda: json.dumps({'studies': {'NCT09990005': F.STUDIES['NCT09990005']}})),
    ('id.json', lambda: json.dumps('NCT09990005')),
    ('lines.jsonl', lambda: json.dumps(_study_without_protocol()) + '\n'),
    ('lines.jsonl', lambda: json.dumps([F.STUDIES['NCT09990005']]) + '\n'),
    ('log.jsonl', lambda: json.dumps({'key': 'ctgov/study/NCT09990005', 'status': 200, 'body': {'note': 'x'}}) + '\n'),
    ('log.jsonl', lambda: json.dumps({'key': 'ctgov/study/NCT09990005', 'status': 200, 'body': None}) + '\n'),
    ('log.jsonl',
     lambda: json.dumps({'key': 'ctgov/study/NCT09990005', 'status': 'unusable_200', 'body': None}) + '\n'),
    # A log line with no status cannot show its request was answered (on 30e1cae its study was frozen).
    ('log.jsonl', lambda: json.dumps({'key': 'ctgov/study/NCT09990005', 'body': F.STUDIES['NCT09990005']}) + '\n'),
])
def test_local_freeze_refuses_an_object_that_is_no_study_page_or_version_answer(
    tmp_path: Path, name: str, content: Any
) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    (local_src / name).write_text(content(), encoding='utf-8')
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError, match=rf'1 study record\(s\) are not valid .*\(first: {re.escape(name)} study 1: '):
        A.ingest_local_ctgov_records(local_src, cache, **LOCAL_STAMPS)
    assert not A.raw_log_path(cache, 'ctgov').exists()
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(tmp_path / 'cli_cache'),
                           *LOCAL_STAMP_ARGS]) == 1


def test_local_freeze_reads_every_shape_the_stored_files_hold(tmp_path: Path) -> None:
    # A study, a `studies` page, a list, a /version answer, and a response log whose failed requests
    # (null, 400 and 404 statuses) and /version line hold no study, as in the stored local files.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    st = F.STUDIES
    (local_src / 'a_study.json').write_text(json.dumps(st['NCT09990001']), encoding='utf-8')
    (local_src / 'b_page.json').write_text(json.dumps({'studies': [st['NCT09990002']], 'nextPageToken': 'x'}),
                                           encoding='utf-8')
    (local_src / 'c_list.json').write_text(json.dumps([st['NCT09990003']]), encoding='utf-8')
    version = {'apiVersion': '2.0.3', 'dataTimestamp': '2026-11-01T08:00:00'}
    (local_src / 'd_version.json').write_text(json.dumps(version), encoding='utf-8')
    log_lines = [
        {'key': 'ctgov/version/x', 'status': 200, 'body': version},
        {'key': 'ctgov/study/NCT09990009', 'status': None, 'body': None, 'error': 'transport_or_timeout_error'},
        {'key': 'q', 'status': 400, 'body': None},
        {'key': 'ctgov/study/NCT09990008', 'status': 404, 'body': {'message': 'NCT09990008 not found'}},
        {'key': 'ctgov/study/NCT09990004', 'status': 200, 'body': st['NCT09990004']},
        {'key': 'page', 'status': 200, 'body': json.dumps({'studies': [st['NCT09990005']]})},
    ]
    (local_src / 'e_log.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in log_lines), encoding='utf-8')
    (local_src / 'f_lines.jsonl').write_text(json.dumps(st['NCT09990006']) + '\n', encoding='utf-8')
    cache = tmp_path / 'cache'
    A.ingest_local_ctgov_records(local_src, cache, **LOCAL_STAMPS)
    assert A.load_frozen_cache(cache).requested_nct_ids == [f'NCT0999000{i}' for i in range(1, 7)]


def test_local_freeze_with_limit_still_compares_every_later_copy(tmp_path: Path) -> None:
    older = copy.deepcopy(F.STUDIES['NCT09990004'])
    newer = copy.deepcopy(older)
    newer['protocolSection']['identificationModule']['officialTitle'] = 'Amended official title'
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'a_first.json').write_text(json.dumps(older), encoding='utf-8')
    (local_src / 'b_other.json').write_text(json.dumps(F.STUDIES['NCT09990005']), encoding='utf-8')
    (local_src / 'c_later.json').write_text(json.dumps(newer), encoding='utf-8')

    # --limit 1 selects NCT09990004 from the first file; its differing copy two files later is still
    # compared, so the freeze is refused rather than keeping whichever snapshot sorted first.
    cache = tmp_path / 'cache_limited'
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache), '--limit', '1',
                           *LOCAL_STAMP_ARGS]) == 1
    assert not A.raw_log_path(cache, 'ctgov').exists()
    with pytest.raises(ValueError, match=r'NCT09990004 in a_first\.json and c_later\.json'):
        A.ingest_local_ctgov_records(local_src, cache, limit=1, **LOCAL_STAMPS)

    # With an identical later copy the limit applies as before: one trial, the one first in order.
    (local_src / 'c_later.json').write_text(json.dumps(older), encoding='utf-8')
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache), '--limit', '1',
                           *LOCAL_STAMP_ARGS]) == 0
    assert A.load_frozen_cache(cache).requested_nct_ids == ['NCT09990004']


def test_missing_or_non_boolean_has_results_is_insufficient_never_a_ctgov_negative(
    built_files: dict[str, Any],
) -> None:
    base = F._make_study('NCT09990011', title='No hasResults field', lead_sponsor='Fictional Sponsor')
    assert C.ctgov_results_cell('NCT09990011', {'status': 200, 'body': base}, [])['state'] == (
        'no_candidate_in_completed_scope'
    )  # control: an explicit hasResults=false is the registry's negative
    for value in ('<absent>', None, 'false', 0):
        study = copy.deepcopy(base)
        if value == '<absent>':
            study.pop('hasResults', None)
        else:
            study['hasResults'] = value
        cell = C.ctgov_results_cell('NCT09990011', {'status': 200, 'body': study}, [])
        assert cell['state'] == 'available_document_insufficient', value
        assert cell['detail']['has_results'] is None
        assert 'no boolean hasResults' in cell['reason']

    # The contract refuses a "CT.gov unreported" discordance on a trial whose hasResults is unknown.
    bad = copy.deepcopy(built_files)
    nct = 'NCT09990001'
    next(s for s in bad['studies.json']['studies'] if s['nct_id'] == nct)['has_results'] = None
    _timeline_of(bad, nct)['discordance']['state'] = 'unreported_across_completed_routes'
    assert any(f'{nct}: CT.gov record has hasResults=null, but discordance is' in p for p in ART.check_contract(bad))


def test_human_confirmed_links_verify_their_route_like_the_timeline() -> None:
    nct = 'NCT09990012'

    def link(route: str, validation: str, review: str, citation_type: str = 'RESULT') -> dict[str, Any]:
        return {
            'link_id': 'link:00000000000000c1',
            'nct_id': nct,
            'source_route': route,
            'citation_type': citation_type,
            'automated_validation': validation,
            'review_status': review,
            'publication_or_action_date': '2021-05-10',
        }

    epmc = {f'literature/epmc_nct/{nct}': {'status': 200, 'body': {'hitCount': 1, 'resultList': {'result': [{}]}}}}
    human = link('literature_search', 'not_validated', 'confirmed_same_trial_results')
    cell = C.literature_search_cell(nct, epmc, [human], True)
    # Settled by review, and the timeline counts it as a results publication: never "awaiting review".
    assert cell['state'] == 'verified_link'
    assert cell['detail']['n_human_confirmed_only'] == 1
    assert T._qualifying_results_publications([human]) == [human]
    # Controls: the same link unreviewed, or held indeterminate, is still awaiting review.
    for review in ('unreviewed', 'indeterminate'):
        assert C.literature_search_cell(nct, epmc, [link('literature_search', 'not_validated', review)], True)[
            'state'] == 'candidate_awaiting_review'
    # A mechanically confirmed link stays verified with no human-only count.
    mech = C.literature_search_cell(nct, epmc, [link('literature_search', 'identifier_confirmed', 'unreviewed')], True)
    assert mech['state'] == 'verified_link' and mech['detail']['n_human_confirmed_only'] == 0

    # The same rule on the openFDA route: a human-confirmed name-matched candidate is verified.
    study = F._make_study(nct, title='Human-confirmed FDA', lead_sponsor='Fictional Sponsor', drugs=['lumirafenib'])
    fda = link('openfda_drugsfda', 'not_validated', 'confirmed_same_trial_results', 'OPENFDA_DRUG_NAME_MATCH')
    fda_cell = C.openfda_drugsfda_cell(nct, {'status': 200, 'body': study}, study,
                                       {f'openfda/nct/{nct}': {'status': 404, 'body': None}}, [fda], True)
    assert fda_cell['state'] == 'verified_link'
    assert T._qualifying_fda_links([fda]) == [fda]


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('Metformin 500 mg tablet', 'metformin'),
        ('Pembrolizumab 200 mg IV infusion', 'pembrolizumab'),
        ('Insulin glargine injection', 'insulin glargine'),
        ('Tiotropium Inhalation Solution', 'tiotropium'),  # from the stored CT.gov records
        ('Glucophage\u00ae XR', 'glucophage'),
        ('Daratumumab SC', 'daratumumab'),
        ('Quetiapine extended-release tablets', 'quetiapine'),
        ('SR 96669', 'sr 96669'),  # a compound code: the abbreviation before a number stays
        ('CR 845 injection', 'cr 845'),
        ('Tablets', None),  # only form words (a stored CT.gov label): no ingredient, no query term
        ('Influenza vaccine', 'influenza vaccine'),  # not a dosage form
        ('Oral semaglutide', 'semaglutide'),
    ],
)
def test_normalize_drug_term_strips_dosage_form_route_and_release_words(raw: str, expected: str | None) -> None:
    # The openFDA query is a quoted phrase in name fields that never hold the dosage form or route.
    assert A.normalize_drug_term(raw) == expected


def test_every_registered_intervention_alias_is_a_query_term() -> None:
    study = F._make_study('NCT09990013', title='Many aliases', lead_sponsor='Fictional Sponsor', drugs=['Brandix'])
    study['protocolSection']['armsInterventionsModule']['interventions'][0]['otherNames'] = [
        'Brandix XR', 'BRX-101', 'Formulix tablet', 'zorbatinib mesylate', 'Zorbatinib Mesylate',
    ]
    # The generic ingredient is the fourth alias; it used to be dropped by a two-alias cap.
    assert A.extract_drug_candidates(study) == ['brandix', 'brx-101', 'formulix', 'zorbatinib mesylate']


def test_reviewed_build_with_human_confirmed_candidates_passes_the_contract(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    built_files: dict[str, Any],
) -> None:
    # One link a person can settle on each mechanical route: a name-matched openFDA candidate, a
    # literature hit set aside by rule, and a registry PMID that did not resolve.
    links = built_files['links.json']['links']
    picks = {
        'openfda_drugsfda': next(x for x in links if x['source_route'] == 'openfda_drugsfda'
                                 and x['automated_validation'] == 'not_validated'),
        'literature_search': next(x for x in links if x['source_route'] == 'literature_search'
                                  and x['automated_validation'] == 'rejected_by_rule'),
        'registry_references': next(x for x in links if x['source_route'] == 'registry_references'
                                    and x['automated_validation'] == 'identifier_not_resolved'),
    }
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        for lnk in picks.values():
            w.writerow([lnk['link_id'], lnk['nct_id'], 'Dr. Fictional', '2026-11-01',
                        'confirmed_same_trial_results', 'same trial, results reported'])
    reviewed = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_csv,
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    # Coverage verifies each route on the human confirmation, and the contract accepts it.
    assert ART.check_contract(reviewed) == []
    for route, lnk in picks.items():
        cell = _cell_of(reviewed, lnk['nct_id'], route)
        assert cell['state'] == 'verified_link', (route, cell)
        assert cell['detail']['n_human_confirmed_only'] >= 1
    # End to end: the CLI build with these reviews is written, not refused.
    out = tmp_path / 'reviewed_build'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(out), '--as-of', F.AS_OF,
                           '--generated-at', F.GENERATED_AT, '--reviews', str(rev_csv)]) == 0
    assert (out / 'run.json').exists()


@pytest.mark.parametrize('pcd_type, expect', [(None, 'absent'), ('UNKNOWN', "unrecognized ('UNKNOWN')"),
                                              ('ESTIMATED', 'ESTIMATED')])
def test_365d_window_needs_an_explicit_actual_primary_completion(pcd_type: str | None, expect: str) -> None:
    as_of = date(2026, 11, 1)
    study = F._make_study('NCT09990014', title='Untyped completion', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-15', pcd_type))
    window = T.classify_window_365d(study, as_of)
    assert window['state'] == 'COMPLETION_DATE_MISSING_OR_FUTURE'
    assert f'type is {expect}' in window['reason']
    # Control: the same record with an ACTUAL completion is in an elapsed-window state.
    actual = F._make_study('NCT09990014', title='Untyped completion', lead_sponsor='Fictional Sponsor',
                           pcd=('2020-01-15', 'ACTUAL'))
    assert T.classify_window_365d(actual, as_of)['state'] == 'UNREPORTED_PAST_365D'


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('Nivolumab + Ipilimumab', ['nivolumab ipilimumab', 'nivolumab', 'ipilimumab']),
        ('Nivolumab/Relatlimab', ['nivolumab relatlimab', 'nivolumab', 'relatlimab']),
        ('Fluticasone Propionate and Salmeterol', ['fluticasone propionate and salmeterol',
                                                   'fluticasone propionate', 'salmeterol']),
        ('Cisplatin or Carboplatin and Pemetrexed', ['cisplatin or carboplatin and pemetrexed',
                                                     'cisplatin', 'carboplatin', 'pemetrexed']),
        ('Salmeterol/fluticasone (50/500 \u03bcg)', ['salmeterol fluticasone', 'salmeterol', 'fluticasone']),
        ('DrugX 5 mg/mL', ['drugx']),  # a strength's slash never splits a label
        ('Ro 09-1978/000', ['ro 09-1978 000', 'ro 09-1978']),  # a letterless component is no term
        ('Zorbatinib + placebo', ['zorbatinib placebo', 'zorbatinib']),
    ],
)
def test_combination_labels_also_query_each_component(raw: str, expected: list[str]) -> None:
    assert A.drug_label_terms(raw) == expected


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('Tiotropium 18\u00b5g once daily', 'tiotropium'),
        ('Metformin 500 mg bid', 'metformin'),
        ('Exenatide Once Weekly', 'exenatide'),
        ('Liraglutide 0.9 mg daily', 'liraglutide'),
        ('Pembrolizumab 200 mg Q3W', 'pembrolizumab'),
        ('Drugzumab every 3 weeks', 'drugzumab'),
        ('Drugzumab 2 times per week', 'drugzumab'),
        ('Brimonidine was applied once daily for 8 days.', 'brimonidine was applied'),
        ('Empagliflozin high dose bid', 'empagliflozin'),
        ('PF-07321332 Dose 3', 'pf-07321332'),
        ('PF-06651600 Maintenance Dose #2', 'pf-06651600'),
        ('Low-dose cytarabine (LDAC)', 'cytarabine'),
        ('Single dose of PF-07104091', 'pf-07104091'),
        ('Ir-CPI - Dose 1', 'ir-cpi'),
        ('Mirikizumab - IV', 'mirikizumab'),
        # Kept: a qualifier word in the middle of a name, a compound code, and a dose word not at an end.
        ('Pronamel Daily Protection', 'pronamel daily protection'),
        ('QD 123', 'qd 123'),
        ('0.5 mL of the mid dose VAX-31', 'of the mid dose vax-31'),
        # A label made only of a qualifier names no drug.
        ('low dose', None),
    ],
)
def test_dosing_schedules_and_dose_levels_at_either_end_are_not_queried(raw: str, expected: str | None) -> None:
    assert A.normalize_drug_term(raw) == expected


def test_a_combination_with_a_schedule_queries_each_component_without_it() -> None:
    assert A.drug_label_terms('Warfarin 5 mg + Pitavastatin 4mg once daily (QD)') == [
        'warfarin pitavastatin', 'warfarin', 'pitavastatin']


def _openfda_cell_without_candidates(ctgov_rec: dict[str, Any] | None, study: dict[str, Any] | None) -> dict[str, Any]:
    records = {'openfda/nct/NCT09990019': {'status': 404}}
    return C.openfda_drugsfda_cell('NCT09990019', ctgov_rec, study, records, [], True)


def _study_with_arms(arms: Any) -> dict[str, Any]:
    ps: dict[str, Any] = {'identificationModule': {'nctId': 'NCT09990019'}}
    if arms != '<absent>':
        ps['armsInterventionsModule'] = arms
    return {'protocolSection': ps}


@pytest.mark.parametrize(
    ('ctgov_rec', 'arms', 'problem'),
    [
        (None, None, 'no CT.gov study record was retrieved'),
        ({'status': 500}, None, 'no CT.gov study record was retrieved'),
        ({'status': 200}, 'projected', 'armsInterventionsModule is not an object'),
        ({'status': 200}, {'interventions': {'name': 'X'}}, 'interventions is not a list'),
        ({'status': 200}, {'interventions': ['Drugzumab']}, 'interventions[0] is not an object'),
        ({'status': 200}, {'interventions': [{'type': 'DRUG'}]}, 'interventions[0] lacks a string type and name'),
        ({'status': 200}, {'interventions': [{'type': 'DRUG', 'name': 'X', 'otherNames': 'Y'}]},
         'otherNames is not a list of strings'),
        # On f68199e a blank name planned no drug term and a blank type hid a drug, so the route read
        # no_candidate_in_completed_scope over the NCT query alone.
        ({'status': 200}, {'interventions': [{'type': 'DRUG', 'name': ''}]},
         'interventions[0] has a blank type or name'),
        ({'status': 200}, {'interventions': [{'type': 'DRUG', 'name': '  '}]},
         'interventions[0] has a blank type or name'),
        ({'status': 200}, {'interventions': [{'type': '', 'name': 'Zorbatinib'}]},
         'interventions[0] has a blank type or name'),
    ],
)
def test_openfda_route_is_incomplete_when_drug_queries_cannot_be_planned(
    ctgov_rec: dict[str, Any] | None, arms: Any, problem: str
) -> None:
    study = None if ctgov_rec is None or ctgov_rec['status'] != 200 else _study_with_arms(arms)
    cell = _openfda_cell_without_candidates(ctgov_rec, study)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert problem in cell['reason']


@pytest.mark.parametrize('arms', ['<absent>', {}, {'interventions': []},
                                  {'interventions': [{'type': 'BEHAVIORAL', 'name': 'Exercise'}]}])
def test_a_record_that_registers_no_drug_is_a_completed_openfda_search(arms: Any) -> None:
    cell = _openfda_cell_without_candidates({'status': 200}, _study_with_arms(arms))
    assert cell['state'] == 'no_candidate_in_completed_scope', cell
    # A CT.gov 404 is the registry's own answer: no record, so nothing to plan.
    assert _openfda_cell_without_candidates({'status': 404}, None)['state'] == 'document_unavailable'


def _nct_cited_link(app: dict[str, Any]) -> dict[str, Any]:
    nct = 'NCT09990001'
    records = {f'openfda/nct/{nct}': {'status': 200, 'body': {'meta': {'results': {'total': 1}}, 'results': [app]}}}
    links = E.build_openfda_links(nct, F.STUDIES[nct], records, set())
    return next(x for x in links if x['automated_validation'] == 'identifier_confirmed')


_ORIG_2000 = {'submission_type': 'ORIG', 'submission_number': '1', 'submission_status': 'AP',
              'submission_status_date': '20000115'}


@pytest.mark.parametrize(
    ('app', 'date', 'precision'),
    [
        # On f68199e every case took the original approval, 2000-01-15.
        ({'application_number': 'NDA990001', 'submissions': [
            _ORIG_2000, {'submission_type': 'SUPPL', 'submission_number': '40', 'submission_status': 'AP',
                         'submission_status_date': '20250610',
                         'submission_public_notes': 'Supported by NCT09990001.'}]},
         '2025-06-10', 'day'),
        # cited outside any submission: no action is tied to the trial
        ({'application_number': 'NDA990001', 'sponsor_name': 'PFIZER INC (NCT09990001)', 'submissions': [_ORIG_2000]},
         None, 'missing'),
        # cited in a submission with no action date
        ({'application_number': 'NDA990001', 'submissions': [
            _ORIG_2000, {'submission_type': 'SUPPL', 'submission_public_notes': 'Supported by NCT09990001.'}]},
         None, 'missing'),
        # control: cited in the original approval itself
        ({'application_number': 'NDA990001', 'submissions': [{**_ORIG_2000, 'submission_public_notes': 'NCT09990001'}]},
         '2000-01-15', 'day'),
    ],
)
def test_an_nct_cited_fda_link_takes_the_cited_submissions_date(
    app: dict[str, Any], date: str | None, precision: str
) -> None:
    link = _nct_cited_link(app)
    assert (link['publication_or_action_date'], link['date_precision']) == (date, precision)
    if date is None:
        assert any('no FDA action date is tied to it' in r for r in link['validation_reasons'])
    # The same application returned only by a drug-term query: on 8f53cd3 that branch still took the
    # original approval date.
    nct = 'NCT09990001'
    study = F.STUDIES[nct]
    drug = A.extract_drug_candidates(study)[0]
    records = {A.drug_query_key(drug): {'status': 200, 'body': {'meta': {'results': {'total': 1}}, 'results': [app]}}}
    links = E.build_openfda_links(nct, study, records, E.trial_sponsor_match_keys(study))
    via_drug = next(x for x in links if x['automated_validation'] == 'identifier_confirmed')
    assert via_drug['detail']['matched_drug'] == drug
    assert (via_drug['publication_or_action_date'], via_drug['date_precision']) == (date, precision)


_SUPPL_2025 = {'submission_type': 'SUPPL', 'submission_number': '40', 'submission_status': 'AP',
               'submission_status_date': '20250610', 'submission_public_notes': 'Supported by NCT09990001.'}
_SUPPL_2018 = {'submission_type': 'SUPPL', 'submission_number': '22', 'submission_status': 'AP',
               'submission_status_date': '20180302', 'submission_public_notes': 'Efficacy from NCT09990001.'}


@pytest.mark.parametrize(
    ('app', 'date', 'field_path', 'n_dated'),
    [
        # On 0169935 the mention in the application's own fields came first, so the link had no date.
        ({'application_number': 'NDA990001', 'sponsor_name': 'PFIZER INC (NCT09990001)',
          'submissions': [_ORIG_2000, _SUPPL_2025]},
         '2025-06-10', 'submissions[1].submission_public_notes', 1),
        # On 0169935 an undated citing submission listed first gave no date.
        ({'application_number': 'NDA990001', 'submissions': [
            {'submission_type': 'SUPPL', 'submission_public_notes': 'NCT09990001'}, _SUPPL_2025]},
         '2025-06-10', 'submissions[1].submission_public_notes', 1),
        # On 0169935 the 2025 supplement, listed first, gave its date rather than the 2018 action.
        ({'application_number': 'NDA990001', 'submissions': [_SUPPL_2025, _ORIG_2000, _SUPPL_2018]},
         '2018-03-02', 'submissions[2].submission_public_notes', 2),
        # The earliest citing submission listed first: 0169935 already gave its date and locator; the reason
        # naming both dated submissions is new.
        ({'application_number': 'NDA990001', 'submissions': [_SUPPL_2018, _SUPPL_2025]},
         '2018-03-02', 'submissions[0].submission_public_notes', 2),
        # control: one citing submission, after an application-level mention
        ({'application_number': 'NDA990001', 'submissions': [_ORIG_2000, _SUPPL_2025]},
         '2025-06-10', 'submissions[1].submission_public_notes', 1),
    ],
)
def test_an_nct_cited_fda_link_takes_the_earliest_dated_citing_submission(
    app: dict[str, Any], date: str, field_path: str, n_dated: int
) -> None:
    nct = 'NCT09990001'
    study = F.STUDIES[nct]
    drug = A.extract_drug_candidates(study)[0]
    direct = _nct_cited_link(app)
    records = {A.drug_query_key(drug): {'status': 200, 'body': {'meta': {'results': {'total': 1}}, 'results': [app]}}}
    links = E.build_openfda_links(nct, study, records, E.trial_sponsor_match_keys(study))
    via_drug = next(x for x in links if x['automated_validation'] == 'identifier_confirmed')
    for link in (direct, via_drug):
        assert (link['publication_or_action_date'], link['date_precision']) == (date, 'day')
        assert link['locator']['json_path'] == f'results[0].{field_path}'
        assert not any('no FDA action date is tied to it' in r for r in link['validation_reasons'])
        several = [r for r in link['validation_reasons'] if 'dated submissions; the earliest' in r]
        assert several == ([f'{nct} is cited in {n_dated} dated submissions; the earliest action date is used']
                           if n_dated > 1 else [])


@pytest.mark.parametrize('raw', [20250102, 20250102.0, ['20250102'], {'date': '20250102'}, True])
def test_a_mistyped_fda_submission_date_is_unknown(raw: object) -> None:
    # On 44ce5e3 the value was passed through str(), so the number 20250102 became the action date
    # 2025-01-02 of a verified link, and of a candidate.
    nct = 'NCT09990001'
    cited = {'application_number': 'NDA990001', 'submissions': [
        {'submission_type': 'SUPPL', 'submission_status': 'AP', 'submission_status_date': raw,
         'submission_public_notes': 'Supported by NCT09990001.'}]}
    study = F.STUDIES[nct]
    drug = A.extract_drug_candidates(study)[0]
    direct = _nct_cited_link(cited)
    records = {A.drug_query_key(drug): {'status': 200, 'body': {'meta': {'results': {'total': 1}}, 'results': [cited]}}}
    via_drug = next(x for x in E.build_openfda_links(nct, study, records, E.trial_sponsor_match_keys(study))
                    if x['automated_validation'] == 'identifier_confirmed')
    for link in (direct, via_drug):
        assert (link['publication_or_action_date'], link['date_precision']) == (None, 'missing')
        assert any('no FDA action date is tied to it' in r for r in link['validation_reasons'])
    # The application-level action date, which candidates carry, reads the same way.
    candidate = {'application_number': 'NDA990002', 'submissions': [
        {**_ORIG_2000, 'submission_status_date': raw}]}
    assert E._openfda_action_date(candidate) == (None, 'missing')
    # Control: the same date as a string is read, for both.
    cited['submissions'][0]['submission_status_date'] = '20250102'
    assert _nct_cited_link(cited)['publication_or_action_date'] == '2025-01-02'
    assert E._openfda_action_date({**candidate, 'submissions': [_ORIG_2000]}) == ('2000-01-15', 'day')


def _pmid_statement_check(locator: str, excerpt: str, result: dict[str, Any]) -> str:
    row = dict.fromkeys(E.STATEMENT_COLUMNS, '')
    row.update(statement_id='stmt-pmid', nct_id='NCT09990001', source_type='literature_record',
               source_locator=locator, verbatim_excerpt=excerpt, recorded_by='Curator', notes='PubMed record')
    pmid = locator.split(':')[1]
    records = {f'literature/pubmed_pmid/{pmid}': {
        'status': 200, 'body': {'header': {'type': 'esummary', 'note': 'Primary endpoint met at Week 24'},
                                'result': result}}}
    link = E.build_statement_links([row], 'a' * 64, {}, records, {})[0]
    return link['detail']['statement_check']


_ESUMMARY_111_222 = {
    'uids': ['99000111', '99000222'],
    '99000111': {'uid': '99000111', 'title': 'A background paper'},
    '99000222': {'uid': '99000222', 'title': 'Primary endpoint met at Week 24'},
}


@pytest.mark.parametrize(
    ('locator', 'excerpt', 'result', 'check'),
    [
        # On 8a57052 each was quote_found_at_locator: the whole response was read, so the excerpt stood
        # in a sibling PMID's entry, in the response's header, or in a response without the PMID's entry.
        ('pmid:99000111', 'Primary endpoint met at Week 24', _ESUMMARY_111_222, 'excerpt_not_found_at_locator'),
        ('pmid:99000333', 'Primary endpoint met at Week 24', _ESUMMARY_111_222, 'locator_not_resolved'),
        ('pmid:99000111', 'Primary endpoint met at Week 24',
         {'uids': ['99000111'], '99000111': {'uid': '99000111', 'error': 'cannot get document summary'}},
         'locator_not_resolved'),
        # On 8a57052 a sub-path was resolved against the whole response, where no path reaches a PMID's
        # entry (a path part cannot start with a digit), so it never resolved.
        # Now it resolves, inside the PMID's entry only (the sibling's uid is not found there).
        ('pmid:99000222:title', 'Primary endpoint met', _ESUMMARY_111_222, 'quote_found_at_locator'),
        ('pmid:99000222:uid', '99000111', _ESUMMARY_111_222, 'excerpt_not_found_at_locator'),
        # control: an excerpt in the PMID's own entry
        ('pmid:99000222', 'Primary endpoint met at Week 24', _ESUMMARY_111_222, 'quote_found_at_locator'),
    ],
)
def test_a_pmid_locator_reads_only_that_pmids_esummary_entry(
    locator: str, excerpt: str, result: dict[str, Any], check: str
) -> None:
    assert _pmid_statement_check(locator, excerpt, result) == check


def _epmc_link_date(**year_fields: Any) -> tuple[str | None, str]:
    hit = {'id': 'EPMC9901', 'title': 'Results of NCT09990001', 'abstractText': 'Primary results of NCT09990001.'}
    hit.update(year_fields)
    body = {'hitCount': 1, 'resultList': {'result': [hit]}}
    records = {'literature/epmc_nct/NCT09990001': {'status': 200, 'body': body}}
    link = next(x for x in E.build_literature_search_links('NCT09990001', records)
                if x['automated_validation'] == 'identifier_confirmed')
    return link['publication_or_action_date'], link['date_precision']


@pytest.mark.parametrize(
    ('fields', 'expected'),
    [
        # On 9e87b0c a compact day in a year field became that day: 2025-01-02, a published milestone.
        ({'pubYear': 20250102}, (None, 'missing')),
        ({'pubYear': '20250102'}, (None, 'missing')),
        ({'journalInfo': {'yearOfPublication': 20250102}}, (None, 'missing')),
        # A malformed pubYear no longer hides a readable journal year behind it.
        ({'pubYear': '20250102', 'journalInfo': {'yearOfPublication': 2025}}, ('2025', 'year')),
        # controls: a year, as text or a number, in either field
        ({'pubYear': '2025'}, ('2025', 'year')),
        ({'pubYear': 2025}, ('2025', 'year')),
        ({'journalInfo': {'yearOfPublication': 2021}}, ('2021', 'year')),
        ({'pubYear': True}, (None, 'missing')),
    ],
)
def test_a_europe_pmc_year_field_is_read_only_as_a_year(fields: dict[str, Any], expected: tuple) -> None:
    assert _epmc_link_date(**fields) == expected


def _fda_link_fetched_2026_11_01(app: dict[str, Any], via_drug: bool) -> dict[str, Any]:
    nct = 'NCT09990001'
    study = F.STUDIES[nct]
    key = A.drug_query_key(A.extract_drug_candidates(study)[0]) if via_drug else f'openfda/nct/{nct}'
    records = {key: {'status': 200, 'fetched_at': '2026-11-01T12:00:00+00:00',
                     'body': {'meta': {'results': {'total': 1}}, 'results': [app]}}}
    keys = E.trial_sponsor_match_keys(study) if via_drug else set()
    return next(x for x in E.build_openfda_links(nct, study, records, keys)
                if x['automated_validation'] == 'identifier_confirmed')


_SUPPL_2100 = {'submission_type': 'SUPPL', 'submission_number': '90', 'submission_status': 'AP',
               'submission_status_date': '21000102', 'submission_public_notes': 'Supported by NCT09990001.'}


@pytest.mark.parametrize('via_drug', [False, True])
@pytest.mark.parametrize(
    ('submissions', 'date'),
    [
        # On 5f38a29 the future date was the link's, and so the trial's earliest FDA action.
        ([_SUPPL_2100], None),
        # controls: a real action beside the future one is used, and so is a date on the fetch day
        ([_SUPPL_2100, _SUPPL_2025], '2025-06-10'),
        ([{**_SUPPL_2025, 'submission_status_date': '20261101'}], '2026-11-01'),
    ],
)
def test_an_fda_action_dated_after_its_record_was_fetched_is_not_used(
    submissions: list[dict[str, Any]], date: str | None, via_drug: bool
) -> None:
    link = _fda_link_fetched_2026_11_01({'application_number': 'NDA990001', 'submissions': submissions}, via_drug)
    assert link['publication_or_action_date'] == date


@pytest.mark.parametrize('via_drug', [False, True])
def test_an_fda_action_dated_after_its_record_was_fetched_is_named(via_drug: bool) -> None:
    app = {'application_number': 'NDA990001', 'submissions': [_SUPPL_2100, _SUPPL_2025]}
    reasons = _fda_link_fetched_2026_11_01(app, via_drug)['validation_reasons']
    assert ('NCT09990001 is cited in a submission dated 2100-01-02 (submissions[0].submission_public_notes), '
            'after the record was retrieved on 2026-11-01, so that date is not used') in reasons


def test_a_candidate_fda_action_dated_after_its_record_was_fetched_is_not_used() -> None:
    # On 5f38a29 `_openfda_action_date` (which dates candidates) took no fetch day and returned 2100-01-02.
    app = {'submissions': [{**_ORIG_2000, 'submission_status_date': '21000102'}]}
    assert E._openfda_action_date(app, '2026-11-01') == (None, 'missing')
    assert E._openfda_action_date(app) == ('2100-01-02', 'day')  # with no fetch day there is no bound


def test_contract_refuses_an_fda_action_dated_after_generated_at(built_files: dict[str, Any]) -> None:
    bad = copy.deepcopy(built_files)
    target = next(x for x in bad['links.json']['links'] if x['source_route'] == 'openfda_drugsfda'
                  and x['publication_or_action_date'])
    assert ART.check_contract(bad) == []
    target['publication_or_action_date'] = '2100-01-02'
    assert any(f"{target['link_id']}: FDA action dated 2100-01-02, after generated_at" in p
               for p in ART.check_contract(bad))


@pytest.mark.parametrize('has_results', ['<absent>', None, 'false', 0, True])
def test_365d_window_needs_explicit_unposted_evidence_before_an_unreported_state(has_results: object) -> None:
    as_of = date(2026, 11, 1)
    study = F._make_study('NCT09990015', title='Unknown results flag', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-15', 'ACTUAL'))
    if has_results == '<absent>':
        study.pop('hasResults', None)
    else:
        study['hasResults'] = has_results
    window = T.classify_window_365d(study, as_of)
    assert window['state'] == 'RESULTS_STATUS_INDETERMINATE', window
    # Control: an explicit hasResults=false is the registry's negative.
    study['hasResults'] = False
    assert T.classify_window_365d(study, as_of)['state'] == 'UNREPORTED_PAST_365D'
    # Control: a results-first-post date after a historical reference date shows results were unposted
    # then, whatever hasResults says today.
    later = F._make_study('NCT09990016', title='Posted later', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-15', 'ACTUAL'), has_results=True, post_date=('2025-06-01', 'ACTUAL'))
    assert T.classify_window_365d(later, date(2022, 1, 1))['state'] == 'UNREPORTED_PAST_365D'


@pytest.mark.parametrize('has_results', ['<absent>', None, 'false', 0, True])
def test_a_submitted_record_needs_explicit_unposted_evidence_before_it_is_called_pending(has_results: object) -> None:
    as_of = date(2026, 11, 1)
    study = F._make_study('NCT09990017', title='Submitted, results flag unclear', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-15', 'ACTUAL'), submit_date='2021-03-01')
    if has_results == '<absent>':
        study.pop('hasResults', None)
    else:
        study['hasResults'] = has_results
    # A submission date alone does not show that results are still unposted.
    window = T.classify_window_365d(study, as_of)
    assert window['state'] == 'RESULTS_STATUS_INDETERMINATE', window
    # Control: an explicit hasResults=false makes it pending.
    study['hasResults'] = False
    assert T.classify_window_365d(study, as_of)['state'] == 'PENDING_SUBMITTED_UNPOSTED'
    # Control: a post date after a historical reference date shows results were unposted then.
    later = F._make_study('NCT09990018', title='Submitted, posted later', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-15', 'ACTUAL'), submit_date='2021-03-01', has_results=True,
                          post_date=('2025-06-01', 'ACTUAL'))
    assert T.classify_window_365d(later, date(2022, 1, 1))['state'] == 'PENDING_SUBMITTED_UNPOSTED'


def test_contract_refuses_a_pending_window_without_unposted_evidence(built_files: dict[str, Any]) -> None:
    bad = copy.deepcopy(built_files)
    nct = 'NCT09990003'  # PENDING_SUBMITTED_UNPOSTED in the fixture build, with hasResults=false
    assert _timeline_of(bad, nct)['window_365d']['state'] == 'PENDING_SUBMITTED_UNPOSTED'
    assert not [p for p in ART.check_contract(bad) if 'window_365d is' in p]
    next(s for s in bad['studies.json']['studies'] if s['nct_id'] == nct)['has_results'] = None
    assert any(f'{nct}: window_365d is PENDING_SUBMITTED_UNPOSTED but CT.gov hasResults is null' in p
               for p in ART.check_contract(bad))


def test_contract_refuses_an_unreported_window_without_unposted_evidence(built_files: dict[str, Any]) -> None:
    bad = copy.deepcopy(built_files)
    nct = 'NCT09990002'  # UNREPORTED_PAST_365D in the fixture build, with hasResults=false
    assert _timeline_of(bad, nct)['window_365d']['state'] == 'UNREPORTED_PAST_365D'
    assert not [p for p in ART.check_contract(bad) if 'window_365d is' in p]
    next(s for s in bad['studies.json']['studies'] if s['nct_id'] == nct)['has_results'] = None
    assert any(f'{nct}: window_365d is UNREPORTED_PAST_365D but CT.gov hasResults is null' in p
               for p in ART.check_contract(bad))


def test_failed_pubmed_lookup_makes_registry_references_incomplete_not_a_candidate() -> None:
    nct = 'NCT09990017'
    study = F._make_study(nct, title='Cited PMID', lead_sponsor='Fictional Sponsor',
                          references=[{'pmid': '31234567', 'type': 'RESULT', 'citation': 'Fictional et al.'}])
    ctgov_rec = {'status': 200, 'body': study}
    lit = {f'literature/pubmed_pmid/31234567': {'status': 503, 'body': None}}
    links = E.build_registry_reference_links(nct, ctgov_rec, lit)
    assert links and all(x['automated_validation'] == 'not_validated' for x in links)
    cell = C.registry_references_cell(nct, ctgov_rec, links, lit, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert cell['detail']['failed_pmids'] == ['31234567']
    # A missing lookup in an acquired archive is the same incomplete search.
    assert C.registry_references_cell(nct, ctgov_rec, links, {}, True)['state'] == 'search_incomplete_or_blocked'
    # A person's confirmation still settles the route.
    confirmed = [dict(links[0], review_status='confirmed_same_trial_results')]
    assert C.registry_references_cell(nct, ctgov_rec, confirmed, lit, True)['state'] == 'verified_link'


def test_partial_completion_overlapping_as_of_is_indeterminate_before_posted_or_submitted() -> None:
    as_of = date(2020, 6, 1)
    # Year-precision completion 2020 may fall after the 2020-01-15 posting and after as_of itself.
    posted = F._make_study('NCT09990018', title='Year completion, posted', lead_sponsor='Fictional Sponsor',
                           pcd=('2020', 'ACTUAL'), has_results=True, post_date=('2020-01-15', 'ACTUAL'))
    assert T.classify_window_365d(posted, as_of)['state'] == 'DATE_PRECISION_INDETERMINATE'
    submitted = F._make_study('NCT09990019', title='Year completion, submitted', lead_sponsor='Fictional Sponsor',
                              pcd=('2020', 'ACTUAL'), submit_date='2020-02-01')
    assert T.classify_window_365d(submitted, as_of)['state'] == 'DATE_PRECISION_INDETERMINATE'
    # Control: an exact completion date before the posting still gives the posted state.
    exact = F._make_study('NCT09990020', title='Exact completion', lead_sponsor='Fictional Sponsor',
                          pcd=('2020-01-10', 'ACTUAL'), has_results=True, post_date=('2020-01-15', 'ACTUAL'))
    assert T.classify_window_365d(exact, as_of)['state'] == 'POSTED_WITHIN_365D'


def test_registry_reference_pmids_unlooked_without_a_literature_archive_are_incomplete() -> None:
    nct = 'NCT09990021'
    study = F._make_study(nct, title='Cited PMID, local build', lead_sponsor='Fictional Sponsor',
                          references=[{'pmid': '31234568', 'type': 'RESULT', 'citation': 'Fictional et al.'}])
    ctgov_rec = {'status': 200, 'body': study}
    links = E.build_registry_reference_links(nct, ctgov_rec, {})
    assert links and all(x['automated_validation'] == 'not_validated' for x in links)
    cell = C.registry_references_cell(nct, ctgov_rec, links, {}, False)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert cell['detail']['unlooked_pmids'] == ['31234568']
    confirmed = [dict(links[0], review_status='confirmed_same_trial_results')]
    assert C.registry_references_cell(nct, ctgov_rec, confirmed, {}, False)['state'] == 'verified_link'


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('Epoprostenol 2 ng/kg/min', 'epoprostenol'),
        ('DrugX 75 mg/m\u00b2 IV', 'drugx'),
        ('Docetaxel 75 mg/m2', 'docetaxel'),
        ('Octreotide 100 nmol', 'octreotide'),
    ],
)
def test_nanogram_amount_and_superscript_area_doses_are_stripped(raw: str, expected: str) -> None:
    assert A.normalize_drug_term(raw) == expected


def test_documentary_statement_needs_its_quote_found_before_the_timeline_counts_it() -> None:
    base = {
        'link_id': 'link:00000000000000d1',
        'nct_id': 'NCT09990022',
        'source_route': 'documentary_statements',
        'citation_type': 'DOCUMENTARY_STATEMENT',
        'review_status': 'confirmed_same_trial_results',
        'publication_or_action_date': '2021-05-10',
    }
    unquoted = dict(
        base, automated_validation='not_validated', detail={'statement_check': 'excerpt_not_found_at_locator'}
    )
    quoted = dict(base, automated_validation='quote_confirmed_in_source')
    # Confirmed by a person but its quote was not found at the locator: not a qualifying publication,
    # and not awaiting review either (the review is settled; the source check failed).
    assert T._qualifying_results_publications([unquoted]) == []
    assert C.documentary_statements_cell('NCT09990022', [unquoted])['state'] == 'search_incomplete_or_blocked'
    assert T._qualifying_results_publications([quoted]) == [quoted]
    assert C.documentary_statements_cell('NCT09990022', [quoted])['state'] == 'verified_link'


_SEARCH_DONE = {r: 'no_candidate_in_completed_scope' for r in C.SEARCH_ROUTES}


def _statement(link_id: str, review_status: str, check: str) -> dict[str, Any]:
    return {
        'link_id': link_id,
        'nct_id': 'NCT09990022',
        'source_route': 'documentary_statements',
        'citation_type': 'DOCUMENTARY_STATEMENT',
        'review_status': review_status,
        'automated_validation': 'quote_confirmed_in_source' if check == 'quote_found_at_locator' else 'not_validated',
        'publication_or_action_date': '2021-05-10',
        'detail': {'statement_check': check},
    }


@pytest.mark.parametrize('check', ['source_not_retrieved', 'locator_not_resolved', 'excerpt_not_found_at_locator'])
@pytest.mark.parametrize('ctgov_state', ['no_candidate_in_completed_scope', 'verified_link'])
def test_a_statement_reviewed_as_results_but_not_quote_confirmed_is_not_called_pending_review(
    check: str, ctgov_state: str
) -> None:
    stmt = _statement('link:00000000000000d2', 'confirmed_same_trial_results', check)
    cell = C.documentary_statements_cell('NCT09990022', [stmt])
    # The cell names the failed source check instead of calling a settled review pending.
    assert cell['state'] == 'search_incomplete_or_blocked'
    assert 'awaiting' not in cell['reason'] and check in cell['reason']
    assert cell['detail']['statement_checks'] == {check: 1}
    # The route still counts toward the overall state, which therefore never reads as a completed negative.
    cells = [{'route': r, 'state': s} for r, s in {**_SEARCH_DONE, 'ctgov_results': ctgov_state}.items()] + [cell]
    assert C.overall_coverage(cells)['state'] == ('verified_link' if ctgov_state == 'verified_link'
                                                  else 'search_incomplete_or_blocked')
    # Left out of the unsettled-review count, but the trial does not settle on a negative state.
    assert T._unsettled_publication_candidates([stmt]) == []
    disc = T.classify_discordance([stmt], {'routes': cells})
    assert disc['n_unsettled_publication_candidates'] == 0
    assert disc['has_linked_results_publication'] is False
    assert disc['state'] == 'coverage_incomplete_or_awaiting_review'
    # Control: without the statement the same routes settle on the negative states.
    bare = [c for c in cells if c['route'] != 'documentary_statements'] + [
        {'route': 'documentary_statements', 'state': 'not_searched'}]
    assert T.classify_discordance([], {'routes': bare})['state'] == (
        'ctgov_posted_no_linked_results_publication' if ctgov_state == 'verified_link'
        else 'unreported_across_completed_routes')


def test_statements_awaiting_review_keep_the_cell_pending_and_count_the_settled_ones_apart() -> None:
    pending = _statement('link:00000000000000d3', 'unreviewed', 'quote_found_at_locator')
    settled = _statement('link:00000000000000d4', 'confirmed_same_trial_results', 'source_not_retrieved')
    cell = C.documentary_statements_cell('NCT09990022', [pending, settled])
    assert cell['state'] == 'candidate_awaiting_review'
    assert cell['detail']['n_awaiting_review'] == 1 and cell['detail']['n_reviewed_unverified'] == 1
    assert cell['reason'].startswith('1 documentary statement(s) entered (1 quote-confirmed in source) awaiting')
    assert [x['link_id'] for x in T._unsettled_publication_candidates([pending, settled])] == [pending['link_id']]


@pytest.mark.parametrize(
    ('review_status', 'set_aside'),
    [('unreviewed', True), ('rejected_false_positive', True), ('confirmed_background_only', True),
     ('confirmed_same_trial_results', False), ('indeterminate', False)],
)
def test_a_rule_rejection_is_set_aside_only_while_review_leaves_it_out_of_coverage(
    review_status: str, set_aside: bool
) -> None:
    link = {
        'automated_validation': 'rejected_by_rule',
        'review_status': review_status,
        'citation_type': 'LITERATURE_NCT_MENTION',
    }
    assert C.set_aside_by_rule(link) is set_aside
    # The export's partition and the coverage filter are the same rule.
    assert (C._active_links([link]) == []) is set_aside


def test_reviewed_rule_rejections_and_unverified_statements_export_where_coverage_uses_them(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any]
) -> None:
    rejected_07 = [x for x in built_files['links.json']['links']
                   if x['nct_id'] == 'NCT09990007' and x['automated_validation'] == 'rejected_by_rule']
    lit = next(x for x in rejected_07 if x['source_route'] == 'literature_search')
    fda = [x for x in rejected_07 if x['source_route'] == 'openfda_drugsfda']
    # A statement for NCT09990005 (otherwise unreported across completed routes) whose excerpt is not
    # in the frozen CT.gov record at its locator.
    stmt_csv = tmp_path / 'documentary_statements.csv'
    with stmt_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.STATEMENT_COLUMNS)
        w.writerow(['stmt-005', 'NCT09990005', 'registry_record', 'protocolSection.descriptionModule.briefSummary',
                    '2021-01', 'results were published in a journal that this record does not mention',
                    'Curator Jane', 'Excerpt deliberately absent from the source'])
    empty = tmp_path / 'reviews_empty.csv'
    empty.write_text(','.join(E.REVIEW_COLUMNS) + '\n', encoding='utf-8')
    probe = ART.build_artifacts(
        frozen_cache, reviews_path=empty, statements_path=stmt_csv,
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv', producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT, as_of=date.fromisoformat(F.AS_OF),
    )
    stmt = next(x for x in probe['links.json']['links'] if x['source_route'] == 'documentary_statements')
    assert stmt['detail']['statement_check'] == 'excerpt_not_found_at_locator'
    assert _timeline_of(probe, 'NCT09990005')['discordance']['n_unsettled_publication_candidates'] == 1

    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        for link_id, nct, decision in (
            (lit['link_id'], 'NCT09990007', 'confirmed_same_trial_results'),
            (fda[0]['link_id'], 'NCT09990007', 'indeterminate'),
            (fda[1]['link_id'], 'NCT09990007', 'rejected_false_positive'),
            (stmt['link_id'], 'NCT09990005', 'confirmed_same_trial_results'),
        ):
            w.writerow([link_id, nct, 'Dr. Fictional', '2026-11-01', decision, 'Reviewed'])
    build_dir = tmp_path / 'build'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(build_dir), '--as-of', F.AS_OF,
                           '--generated-at', F.GENERATED_AT, '--reviews', str(rev_csv),
                           '--statements', str(stmt_csv)]) == 0
    files, _ = ART.load_dir(build_dir)
    assert ART.check_contract(files) == []
    # The confirmed rule rejection verifies its route; the indeterminate one is a live candidate.
    assert _cell_of(files, 'NCT09990007', 'literature_search')['state'] == 'verified_link'
    assert _cell_of(files, 'NCT09990007', 'openfda_drugsfda')['state'] == 'candidate_awaiting_review'
    assert _timeline_of(files, 'NCT09990007')['discordance']['state'] == 'published_results_while_ctgov_unreported'
    # The confirmed statement whose excerpt is absent: not pending review, and no negative state.
    assert _cell_of(files, 'NCT09990005', 'documentary_statements')['state'] == 'search_incomplete_or_blocked'
    assert _coverage_of(files, 'NCT09990005')['overall']['state'] == 'search_incomplete_or_blocked'
    disc_05 = _timeline_of(files, 'NCT09990005')['discordance']
    assert disc_05['state'] == 'coverage_incomplete_or_awaiting_review'
    assert disc_05['n_unsettled_publication_candidates'] == 0

    site = tmp_path / 'site' / 'disclosure'
    assert cli_export.main([str(build_dir), str(site)]) == 0
    shown = {r['link_id'] for r in json.loads((site / 'evidence.json').read_text(encoding='utf-8'))['records']}
    aside_file = json.loads((site / 'evidence_set_aside.json').read_text(encoding='utf-8'))
    aside = {r['link_id'] for r in aside_file['records']}
    assert {lit['link_id'], fda[0]['link_id']} <= shown
    assert not {lit['link_id'], fda[0]['link_id']} & aside
    assert fda[1]['link_id'] in aside and fda[1]['link_id'] not in shown
    assert len(aside) == len(rejected_07) - 2
    run = json.loads((site / 'run.json').read_text(encoding='utf-8'))
    assert run['records']['evidence_set_aside.json'] == len(rejected_07) - 2


def test_as_of_after_the_frozen_ctgov_data_is_refused(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any]
) -> None:
    kwargs: dict[str, Any] = {
        'reviews_path': ROOT / 'disclosure' / 'reviews.csv',
        'statements_path': ROOT / 'disclosure' / 'documentary_statements.csv',
        'sponsor_rules_path': ROOT / 'sponsors' / 'company_aliases.csv',
        'producer': cli_build.producer_metadata(),
        'generated_at': F.GENERATED_AT,
    }
    with pytest.raises(ValueError, match=r'--as-of 2100-01-01 is after the CT.gov data the cache holds'):
        ART.build_artifacts(frozen_cache, **kwargs, as_of=date(2100, 1, 1))
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(out), '--as-of', '2100-01-01',
                           '--generated-at', F.GENERATED_AT]) == 1
    assert not out.exists()
    # The contract applies the same bound to an edited manifest.
    bad = copy.deepcopy(built_files)
    bad['run.json']['as_of'] = '2100-01-01'
    assert any('is after the earliest CT.gov data timestamp' in p for p in ART.check_contract(bad))


@pytest.mark.parametrize('value', ['not-a-timestamp', '2026-02-30T12:00:00+00:00', '2026-11-01',
                                   '2026-11-01T12:00:00', '2026-11-01T12:00:00+01:00', '2026-11-01T12:00:00Zjunk',
                                   '2026-11-01T12:00:00-00:00'])  # RFC 3339: -00:00 = offset unknown
def test_generated_at_must_be_a_whole_utc_timestamp(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any], value: str
) -> None:
    kwargs: dict[str, Any] = {
        'reviews_path': ROOT / 'disclosure' / 'reviews.csv',
        'statements_path': ROOT / 'disclosure' / 'documentary_statements.csv',
        'sponsor_rules_path': ROOT / 'sponsors' / 'company_aliases.csv',
        'producer': cli_build.producer_metadata(),
        'as_of': date.fromisoformat(F.AS_OF),
    }
    with pytest.raises(ValueError, match='generated_at must'):
        ART.build_artifacts(frozen_cache, generated_at=value, **kwargs)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(out), '--as-of', F.AS_OF,
                           '--generated-at', value]) == 1
    assert not out.exists()
    bad = copy.deepcopy(built_files)
    bad['run.json']['generated_at'] = value
    assert any('run.json generated_at must' in p for p in ART.check_contract(bad))
    # Control: Z and +00:00 are both accepted.
    assert ART.generated_at_problem('2026-11-01T12:00:00Z') is None
    assert ART.generated_at_problem(F.GENERATED_AT) is None


@pytest.mark.parametrize('reviewed_at', ['2026-11-02', '2100-01-01'])
def test_review_dated_after_generated_at_is_refused(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any], reviewed_at: str
) -> None:
    # On 51505fd a review dated after the build (F.GENERATED_AT is 2026-11-01) was applied, so the
    # artifact claimed a human decision that had not yet been made.
    link = built_files['links.json']['links'][0]
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([link['link_id'], link['nct_id'], 'Dr. Fictional', reviewed_at, 'indeterminate', 'unsure'])
    kwargs = {**_build_kwargs(), 'reviews_path': rev_csv, 'as_of': date.fromisoformat(F.AS_OF)}
    with pytest.raises(ValueError, match=f'dated after generated_at .*reviewed_at {reviewed_at}'):
        ART.build_artifacts(frozen_cache, **kwargs)
    # A review on the generation day itself is accepted, and the contract refuses an edited later date.
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([link['link_id'], link['nct_id'], 'Dr. Fictional', '2026-11-01', 'indeterminate', 'unsure'])
    reviewed = ART.build_artifacts(frozen_cache, **kwargs)
    assert ART.check_contract(reviewed) == []
    bad = copy.deepcopy(reviewed)
    target = next(x for x in bad['links.json']['links'] if x['link_id'] == link['link_id'])
    target['review']['reviewed_at'] = reviewed_at
    assert any(f'review dated {reviewed_at}, after generated_at' in p for p in ART.check_contract(bad))


def _write_statement(path: Path, statement_date: str) -> None:
    with path.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.STATEMENT_COLUMNS)
        w.writerow(['stmt-001', 'NCT09990001', 'registry_record', 'protocolSection.descriptionModule.briefSummary',
                    statement_date, 'primary endpoint met at Week 24', 'Curator Jane', 'Verbatim summary quote'])


@pytest.mark.parametrize('statement_date', ['2100-01-01', '2026-11-02', '2026-12', '2027'])
def test_documentary_statement_dated_after_generated_at_is_refused(
    tmp_path: Path, frozen_cache: A.FrozenCache, statement_date: str
) -> None:
    # On 0169935 each date was accepted after F.GENERATED_AT (2026-11-01); confirmed and reviewed, the
    # statement became the trial's earliest results publication, a milestone after the build.
    stmt_csv = tmp_path / 'documentary_statements.csv'
    _write_statement(stmt_csv, statement_date)
    kwargs = {**_build_kwargs(), 'statements_path': stmt_csv, 'as_of': date.fromisoformat(F.AS_OF)}
    with pytest.raises(ValueError, match=f'dated after generated_at .*statement_date {statement_date}'):
        ART.build_artifacts(frozen_cache, **kwargs)
    # Control: a statement dated on the generation day, or in a month or year containing it, is accepted,
    # and the contract refuses the statement link once its date is edited to a later one.
    for accepted in ('2026-11-01', '2026-11', '2026'):
        _write_statement(stmt_csv, accepted)
        built = ART.build_artifacts(frozen_cache, **kwargs)
        assert ART.check_contract(built) == []
    bad = copy.deepcopy(built)
    target = next(x for x in bad['links.json']['links'] if x['source_route'] == 'documentary_statements')
    target['publication_or_action_date'] = statement_date
    assert any(f'statement dated {statement_date}, after generated_at' in p for p in ART.check_contract(bad))


@pytest.mark.parametrize('value', ['2000-01-01T00:00:00Z', '2026-11-01T11:59:59+00:00'])
def test_generated_at_before_the_cache_was_frozen_is_refused(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any], value: str
) -> None:
    # The fixture cache was frozen at F.GENERATED_AT (2026-11-01T12:00:00+00:00). On a5ec32c both values
    # built, and the export published a generation time before the cache the build read.
    assert frozen_cache.manifest['frozen_at'] == F.GENERATED_AT
    kwargs = {**_build_kwargs(), 'as_of': date.fromisoformat(F.AS_OF)}
    with pytest.raises(ValueError, match=re.escape(f'generated_at {value!r} is earlier than the cache was frozen')):
        ART.build_artifacts(frozen_cache, **{**kwargs, 'generated_at': value})
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(out), '--as-of', F.AS_OF,
                           '--generated-at', value]) == 1
    assert not out.exists()
    # The contract applies the same rule to an edited run.json.
    bad = copy.deepcopy(built_files)
    bad['run.json']['generated_at'] = value
    assert any('is earlier than the cache was frozen' in p for p in ART.check_contract(bad))
    # Control: a build generated at the freeze time itself, or later, is accepted.
    assert ART.generated_before_frozen_problem(F.GENERATED_AT, F.GENERATED_AT) is None
    assert ART.generated_before_frozen_problem('2026-11-01T12:00:01Z', F.GENERATED_AT) is None
    assert built_files['run.json']['inputs']['cache_manifest_frozen_at'] == F.GENERATED_AT
    assert ART.check_contract(built_files) == []


@pytest.mark.parametrize(
    ('fetched_at', 'data_timestamp', 'expect'),
    [
        ('2026-11-01', '2026-11-01T08:00:00', 'fetched_at must be a whole'),  # a bare date names no instant
        ('2026-11-01T12:00:00', '2026-11-01T08:00:00', 'fetched_at must carry a UTC offset'),  # naive
        ('2026-11-01T12:00:00+01:00', '2026-11-01T08:00:00', 'fetched_at must carry a UTC offset'),
        # RFC 3339 writes -00:00 for an unknown local offset: a zero number, but not a stated UTC time
        ('2026-11-01T12:00:00-00:00', '2026-11-01T08:00:00', 'fetched_at must carry a UTC offset'),
        ('2026-11-01T12:00:00+00:00', '2026-11-01', 'data_timestamp must be a whole'),
        ('2026-11-01T12:00:00+00:00', 'yesterday', 'data_timestamp must be a whole'),
        # CT.gov's dataTimestamp has no offset; a zoned value's first ten characters can name the
        # wrong day (23:30 at -05:00 is 2 November in UTC)
        ('2026-11-01T12:00:00+00:00', '2026-11-01T23:30:00-05:00', 'data_timestamp must carry no UTC offset'),
        ('2026-11-01T12:00:00+00:00', '2026-11-01T08:00:00Z', 'data_timestamp must carry no UTC offset'),
    ],
)
def test_local_freeze_stamps_must_be_whole_timestamps(
    tmp_path: Path, fetched_at: str, data_timestamp: str, expect: str
) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError, match=expect):
        A.ingest_local_ctgov_records(local_src, cache, fetched_at=fetched_at, data_timestamp=data_timestamp)
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--fetched-at', fetched_at, '--data-timestamp', data_timestamp]) == 1
    assert not A.raw_log_path(cache, 'ctgov').exists()
    # Control: a UTC fetch time and CT.gov's offset-free data timestamp are accepted.
    assert A.timestamp_problem('2026-11-01T12:00:00Z', 'fetched_at', offset='utc') is None
    assert A.timestamp_problem('2026-11-13T10:00:06', 'data_timestamp', offset='none') is None


def test_ambiguous_ctgov_record_never_enables_while_ctgov_unreported_states() -> None:
    # hasResults=true with an empty resultsSection: the CT.gov route is `available_document_insufficient`.
    shell = F._make_study('NCT09990010', title='Results shell', lead_sponsor='Fictional Sponsor',
                          has_results=True, empty_results_shell=True)
    ct_cell = C.ctgov_results_cell('NCT09990010', {'status': 200, 'body': shell}, [])
    assert ct_cell['state'] == 'available_document_insufficient'

    pub = _qualifying_result_ref('link:00000000000000b1', '2021-05-10')
    fda = {
        'link_id': 'link:00000000000000b2',
        'source_route': 'openfda_drugsfda',
        'citation_type': 'OPENFDA_NCT_CITED',
        'automated_validation': 'identifier_confirmed',
        'review_status': 'unreviewed',
        'publication_or_action_date': '2022-01-01',
    }

    def coverage(ctgov_state: str) -> dict[str, Any]:
        states = {route: 'no_candidate_in_completed_scope' for route in C.ROUTES}
        states['ctgov_results'] = ctgov_state
        states['documentary_statements'] = 'not_searched'
        return {'routes': [{'route': r, 'state': st} for r, st in states.items()]}

    for links, unreported_state in (([pub], 'published_results_while_ctgov_unreported'),
                                    ([fda], 'fda_cited_while_ctgov_unreported')):
        ambiguous = T.classify_discordance(links, coverage('available_document_insufficient'))
        assert ambiguous['state'] == 'coverage_incomplete_or_awaiting_review'
        assert 'available_document_insufficient' in ambiguous['reason']
        # Control: a completed CT.gov negative still yields the discordance label.
        assert T.classify_discordance(links, coverage('no_candidate_in_completed_scope'))['state'] == unreported_state
    # Nor does the ambiguous record count toward "unreported across completed routes".
    assert T.classify_discordance([], coverage('available_document_insufficient'))['state'] == (
        'coverage_incomplete_or_awaiting_review'
    )


# ---------------------------------------------------------------- 13. Local freeze timestamps, out-of-scope reviews

def test_local_freeze_requires_stated_timestamps_and_freezes_at_the_real_time(tmp_path: Path) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    cache = tmp_path / 'local_cache'

    # No default retrieval or data timestamp: freeze-local cannot run without both.
    for missing in (['--fetched-at', '2025-06-30T18:00:00+00:00'], ['--data-timestamp', '2025-06-30T09:00:00']):
        with pytest.raises(SystemExit):
            cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache), *missing])
    assert not cache.exists()
    # build --records seeds through the same path and refuses without them.
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--records', str(local_src), '--out', str(out)]) == 1
    assert not cache.exists() and not out.exists()
    with pytest.raises(ValueError, match='fetched_at must be a whole ISO 8601 date and time'):
        A.ingest_local_ctgov_records(local_src, cache, fetched_at='last June', data_timestamp='2025-06-30T09:00:00')

    # Stated stamps are what the cache records; frozen_at is the time of freezing.
    before = A.now_utc()
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--fetched-at', '2025-06-30T18:00:00+00:00', '--data-timestamp', '2025-06-30T09:00:00']) == 0
    after = A.now_utc()
    loaded = A.load_frozen_cache(cache)
    assert loaded.ctgov['ctgov/study/NCT09990004']['fetched_at'] == '2025-06-30T18:00:00+00:00'
    assert loaded.ctgov['ctgov/version/2025-06-30']['body']['dataTimestamp'] == '2025-06-30T09:00:00'
    assert before <= loaded.manifest['frozen_at'] <= after

    # Without --as-of the 365-day window is evaluated at the stated data timestamp, not a fixed date. The
    # build is generated after the freeze (`after`), since it cannot be generated before the cache it reads.
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--generated-at', after]) == 0
    run = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    assert run['as_of'] == '2025-06-30'
    assert run['sources']['ctgov']['data_timestamp'] == '2025-06-30T09:00:00'
    timelines = json.loads((out / 'timelines.json').read_text(encoding='utf-8'))['timelines']
    assert {t['window_365d']['as_of'] for t in timelines} == {'2025-06-30'}


def test_reviews_for_trials_outside_the_build_are_out_of_scope_not_orphaned(
    tmp_path: Path,
    built_files: dict[str, Any],
    frozen_cache: A.FrozenCache,
) -> None:
    res_02 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990002' and x['source_route'] == 'registry_references'
    )
    epmc_06 = next(
        x for x in built_files['links.json']['links']
        if x['nct_id'] == 'NCT09990006' and x['source_route'] == 'literature_search'
    )
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([res_02['link_id'], 'NCT09990002', 'Dr. Fictional', '2026-11-01',
                   'confirmed_same_trial_results', 'in scope'])
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-01',
                   'confirmed_same_trial_results', 'excluded trial'])
        w.writerow(['link:0000000000000000', 'NCT09990002', 'Dr. Fictional', '2026-11-01',
                   'indeterminate', 'changed evidence'])
        w.writerow(['link:1111111111111111', 'NCT09999999', 'Dr. Fictional', '2026-11-01',
                   'indeterminate', 'trial not in cache'])

    subset = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_csv,
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=['NCT09990001', 'NCT09990002'],
    )
    assert subset['run.json']['reviews'] == {
        'n_rows': 4,
        'n_applied': 1,
        'orphaned_link_ids': ['link:0000000000000000'],
        'out_of_scope_link_ids': sorted([epmc_06['link_id'], 'link:1111111111111111']),
    }
    assert subset['links.json']['orphaned_reviews'] == ['link:0000000000000000']
    assert ART.check_contract(subset) == []

    # The contract keeps the four counts reconciled and the two lists disjoint.
    bad = copy.deepcopy(subset)
    bad['run.json']['reviews']['out_of_scope_link_ids'] = ['link:0000000000000000']
    probs = ART.check_contract(bad)
    assert any('both orphaned and out of scope' in p for p in probs)
    assert any('n_rows != n_applied + orphaned + out of scope' in p for p in probs)

    # The site export carries the out-of-scope count beside the orphan count.
    exported = cli_export.select_site_files(subset)
    assert exported['run.json']['reviews'] == {'n_rows': 4, 'n_applied': 1, 'n_orphaned': 1, 'n_out_of_scope': 2}


# ---------------------------------------------------------------- 14. Activity, amount and micro-sign strengths

@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        # Activity and amount units, with their denominators.
        ('Insulin 10 units/mL', 'insulin'),
        ('Insulin 100 U/mL', 'insulin'),
        ('Heparin 5000 units', 'heparin'),
        ('Factor VIII 25 U/kg', 'factor viii'),
        ('Potassium chloride 20 mEq/L', 'potassium chloride'),
        ('DrugX 2 mmol/L', 'drugx'),
        ('Drug 5 mcg/kg/min', 'drug'),
        # Micro sign (U+00B5) and Greek mu (U+03BC), and combination-product strengths (tracked CT.gov labels).
        ('Tiotropium 18\u00b5g once daily', 'tiotropium'),  # the schedule goes too (round 25)
        ('Formoterol Fumarate 12 \u03bcg (Foradil\u00ae Aerolizer\u00ae)', 'formoterol fumarate'),
        ('BFF MDI 320/9.6 \u03bcg', 'bff mdi'),
        ('Advair Diskus\u00ae 250/50 mcg', 'advair diskus'),
        # Genuine name tokens stay: only a whole unit right after a number is strength.
        ('Vitamin D3 1000 IU', 'vitamin d3'),
        ('Insulin U-100', 'insulin u-100'),
        ('Unituxin 17.5 mg/m2', 'unituxin'),
        ('Packed red blood cell units', 'packed red blood cell units'),
        ('Lutetium Lu 177 dotatate', 'lutetium lu 177 dotatate'),
        ('5-FU', '5-fu'),
        ('CHF 1535 100/6 \u00b5g pMDI', 'chf 1535 pmdi'),
        # A dose range is one strength (it left `prednisone 5-` in the query); codes stay.
        ('Prednisone 5-10 mg', 'prednisone'),
        ('Prednisone 5 to 10 mg', 'prednisone'),
        ('Seladelpar 5\u201310 mg', 'seladelpar'),
        ('Metformin: Glucophage, 500-2500mg/day', 'metformin glucophage'),
        ('Insulin 10-20 units', 'insulin'),
        ('5-FU 400 mg/m2', '5-fu'),
        ('Ro 09-1978/000', 'ro 09-1978 000'),
        # A comma-grouped number is one strength (`50,000 IU` left `cholecalciferol 50` in the query).
        ('Cholecalciferol 50,000 IU', 'cholecalciferol'),
        ('Heparin 10,000 U/mL', 'heparin'),
        ('Vitamin D3 1,000-4,000 IU', 'vitamin d3'),
        ('Ergocalciferol 1,250 mcg', 'ergocalciferol'),
        # Every slash-separated component may be grouped too (`50,000/10,000 IU` left `drug 50 000`).
        ('Drug 50,000/10,000 IU', 'drug'),
        ('Pancrelipase 24,000/76,000/120,000 units', 'pancrelipase'),
        ('ICI 182,780', 'ici 182 780'),  # a grouped number with no unit after it is a code, kept
        # A hyphen may join an amount to its unit (on 7402950 these kept `10-mg`, `0 5-mg`, `2-g`, `000-iu`).
        ('Prednisone 10-mg', 'prednisone'),
        ('Drug 0.5-mg tablet', 'drug'),
        ('Drug 10-mg/kg', 'drug'),
        ('Drug 2-g', 'drug'),
        ('Drug 50,000-IU', 'drug'),
        ('IL-2', 'il-2'),  # control: no unit after the hyphen
    ],
)
def test_normalize_drug_term_strips_activity_amount_and_micro_sign_strengths(
    raw: str,
    expected: str | None,
) -> None:
    assert A.normalize_drug_term(raw) == expected


# ---------------------------------------------------------------- 15. Fresh local caches, posted CT.gov results

def test_local_freeze_refuses_an_existing_cache_instead_of_mixing_its_records(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
) -> None:
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    historical = ['--fetched-at', '2025-06-30T18:00:00+00:00', '--data-timestamp', '2025-06-30T09:00:00']

    # A cache from a live acquisition holds a later CT.gov version record and literature/openFDA logs.
    cache = tmp_path / 'live_cache'
    shutil.copytree(frozen_cache.cache_dir, cache)
    assert frozen_cache.ctgov['ctgov/version/2026-11-01/begin']['body']['dataTimestamp'] == '2026-11-01T08:00:00'
    before = {p.relative_to(cache).as_posix(): p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache), *historical]) == 1
    with pytest.raises(A.CacheIntegrityError, match='new or empty cache directory'):
        A.ingest_local_ctgov_records(local_src, cache, fetched_at='2025-06-30T18:00:00+00:00',
                                     data_timestamp='2025-06-30T09:00:00')
    assert {p.relative_to(cache).as_posix(): p.read_bytes() for p in cache.rglob('*') if p.is_file()} == before

    # A new or empty directory takes the local freeze, and the stated version is the only one.
    empty = tmp_path / 'empty_cache'
    empty.mkdir()
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(empty), *historical]) == 0
    loaded = A.load_frozen_cache(empty)
    assert [k for k in loaded.ctgov if k.startswith('ctgov/version/')] == ['ctgov/version/2025-06-30']
    assert loaded.literature_meta is None and loaded.openfda_meta is None


def test_reviews_cannot_unpost_ctgov_results_or_make_the_trial_unreported(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    built_files: dict[str, Any],
) -> None:
    ct_links = {
        x['nct_id']: x for x in built_files['links.json']['links'] if x['source_route'] == 'ctgov_results'
    }
    assert set(ct_links) == {'NCT09990001', 'NCT09990004'}
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([ct_links['NCT09990001']['link_id'], 'NCT09990001', 'Dr. Fictional', '2026-11-01',
                    'rejected_false_positive', 'disputes the posted results'])
        w.writerow([ct_links['NCT09990004']['link_id'], 'NCT09990004', 'Dr. Fictional', '2026-11-01',
                    'confirmed_background_only', 'disputes the posted results'])
    reviewed = ART.build_artifacts(
        frozen_cache,
        reviews_path=rev_csv,
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
    )
    assert ART.check_contract(reviewed) == []
    for nct in ('NCT09990001', 'NCT09990004'):
        cell = _cell_of(reviewed, nct, 'ctgov_results')
        # The review is recorded on the link, and the registry's posted results still stand.
        assert next(x for x in reviewed['links.json']['links'] if x['link_id'] == ct_links[nct]['link_id'])[
            'review_status'] in ('rejected_false_positive', 'confirmed_background_only')
        assert cell['state'] == 'verified_link'
        assert 'do not override the posted record' in cell['reason']
        assert _timeline_of(reviewed, nct)['discordance'] == _timeline_of(built_files, nct)['discordance']
    assert _timeline_of(reviewed, 'NCT09990001')['discordance']['state'] == 'concordant_multi_source_disclosed'
    posted = next(m for m in reviewed['summary.json']['metrics'] if m['id'] == 'trials_ctgov_results_posted')
    assert posted['numerator'] == 2

    # The contract refuses a hasResults=true trial in a "CT.gov unreported" discordance state.
    bad = copy.deepcopy(reviewed)
    _timeline_of(bad, 'NCT09990001')['discordance']['state'] = 'published_results_while_ctgov_unreported'
    assert any('hasResults=true, but discordance is' in p for p in ART.check_contract(bad))


# ---------------------------------------------------------------- 16. CT.gov data-timestamp range, git provenance

def test_resumed_acquisition_across_a_ctgov_refresh_records_the_range_and_needs_as_of(
    tmp_path: Path,
    built_files: dict[str, Any],
    frozen_cache: A.FrozenCache,
) -> None:
    # The fixture cache holds one version record, so the default as_of is its data timestamp, as before.
    single = ART.build_artifacts(
        frozen_cache,
        reviews_path=ROOT / 'disclosure' / 'reviews.csv',
        statements_path=ROOT / 'disclosure' / 'documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors' / 'company_aliases.csv',
        producer=cli_build.producer_metadata(),
        generated_at=F.GENERATED_AT,
    )
    ct = single['run.json']['sources']['ctgov']
    assert [v['key'] for v in ct['versions']] == ['ctgov/version/2026-11-01/begin', 'ctgov/version/2026-11-01/end']
    assert ct['data_timestamp_first'] == ct['data_timestamp_last'] == ct['data_timestamp'] == '2026-11-01T08:00:00'
    assert (single['run.json']['as_of'], single['run.json']['as_of_source']) == ('2026-11-01', 'ctgov_data_timestamp')
    assert built_files['run.json']['as_of_source'] == 'as_of_argument'

    # Day 1 fetches NCT09990001; the acquisition resumes on day 2, after a CT.gov data refresh.
    def refreshed_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-02T08:00:00'})
        return F.stub_get()(url, params)

    cache = tmp_path / 'resumed_cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(cache, 'ctgov'))
    A.fetch_ctgov_studies(['NCT09990001'], log, F.stub_get(), throttle, run_stamp='2026-11-01')
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], log, refreshed_get, throttle, run_stamp='2026-11-02')
    A.freeze_cache(cache, ['NCT09990001', 'NCT09990002'], frozen_at=F.GENERATED_AT)
    resumed = A.load_frozen_cache(cache)

    kwargs: dict[str, Any] = {
        'reviews_path': ROOT / 'disclosure' / 'reviews.csv',
        'statements_path': ROOT / 'disclosure' / 'documentary_statements.csv',
        'sponsor_rules_path': ROOT / 'sponsors' / 'company_aliases.csv',
        'producer': cli_build.producer_metadata(),
        'generated_at': F.GENERATED_AT,
    }
    with pytest.raises(ValueError, match=r'2026-11-01T08:00:00 to 2026-11-02T08:00:00.*pass --as-of'):
        ART.build_artifacts(resumed, **kwargs)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--generated-at', F.GENERATED_AT]) == 1
    assert not out.exists()

    stated = ART.build_artifacts(resumed, **kwargs, as_of=date(2026, 11, 1))  # the earliest data timestamp binds
    assert ART.check_contract(stated) == []
    ct2 = stated['run.json']['sources']['ctgov']
    assert [(v['key'], v['data_timestamp']) for v in ct2['versions']] == [
        ('ctgov/version/2026-11-01/begin', '2026-11-01T08:00:00'),
        ('ctgov/version/2026-11-01/end', '2026-11-01T08:00:00'),
        ('ctgov/version/2026-11-02/begin', '2026-11-02T08:00:00'),
        ('ctgov/version/2026-11-02/end', '2026-11-02T08:00:00'),
    ]
    assert (ct2['data_timestamp_first'], ct2['data_timestamp_last'], ct2['data_timestamp']) == (
        '2026-11-01T08:00:00', '2026-11-02T08:00:00', '2026-11-02T08:00:00',
    )
    assert stated['run.json']['as_of_source'] == 'as_of_argument'

    # The contract refuses a defaulted as_of over a spanning range, and a range that disagrees with its records.
    defaulted = copy.deepcopy(stated)
    defaulted['run.json']['as_of_source'] = 'ctgov_data_timestamp'
    assert any('as_of was defaulted although' in p for p in ART.check_contract(defaulted))
    narrowed = copy.deepcopy(stated)
    narrowed['run.json']['sources']['ctgov']['data_timestamp_first'] = '2026-11-02T08:00:00'
    assert any('disagree with its version records' in p for p in ART.check_contract(narrowed))


def _version_stamps(cache: A.FrozenCache) -> list[str]:
    return sorted({
        r['body']['dataTimestamp'] for k, r in cache.ctgov.items()
        if k.startswith('ctgov/version/') and r.get('status') == 200
    })


def _build_kwargs() -> dict[str, Any]:
    return {
        'reviews_path': ROOT / 'disclosure' / 'reviews.csv',
        'statements_path': ROOT / 'disclosure' / 'documentary_statements.csv',
        'sponsor_rules_path': ROOT / 'sponsors' / 'company_aliases.csv',
        'producer': cli_build.producer_metadata(),
        'generated_at': F.GENERATED_AT,
    }


def test_a_ctgov_answer_for_another_trial_is_never_settled_frozen_or_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /studies/NCT09990002 answers with NCT09990001's record (as an obsolete ID can be answered
    # under the trial's current one).
    def other_trial_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/studies/NCT09990002':
            return F._json_resp(F.STUDIES['NCT09990001'])
        return F.stub_get()(url, params)

    ids = ['NCT09990001', 'NCT09990002']
    key = 'ctgov/study/NCT09990002'
    cache = tmp_path / 'other_trial_cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    raw = A.raw_log_path(cache, 'ctgov')
    # Acquired by code that accepted any valid study record: the answer is settled.
    with monkeypatch.context() as mp:
        mp.setattr(A, 'ctgov_study_answers', lambda body, nct_id: A.ctgov_study_valid(body))
        A.fetch_ctgov_studies(ids, A.ResponseLog(raw), other_trial_get, throttle, run_stamp='2026-11-01')
    assert A.ResponseLog(raw).records[key]['status'] == 200
    with pytest.raises(A.CacheIntegrityError, match=r'records for a different NCT ID .*NCT09990002'):
        A.freeze_cache(cache, ids, frozen_at=F.GENERATED_AT)
    # Frozen by code without the freeze check, it is refused at load.
    with monkeypatch.context() as mp:
        mp.setattr(A, 'ctgov_studies_answered_for_another_trial', lambda records: [])
        A.freeze_cache(cache, ids, frozen_at=F.GENERATED_AT)
    with pytest.raises(A.CacheIntegrityError, match=r'records for a different NCT ID .*NCT09990002'):
        A.load_frozen_cache(cache)
    # Run again, the ctgov stage logs the answer as unusable; the cache then freezes and builds, and
    # nothing of NCT09990001's record is published under NCT09990002.
    res = A.fetch_ctgov_studies(ids, A.ResponseLog(raw), other_trial_get, throttle, run_stamp='2026-11-02')
    assert res == {'studies_answered': 1, 'studies_unanswered': 1}
    assert A.ResponseLog(raw).records[key]['status'] == A.UNUSABLE_200
    A.freeze_cache(cache, ids, frozen_at=F.GENERATED_AT)
    built = ART.build_artifacts(A.load_frozen_cache(cache), **_build_kwargs())
    assert ART.check_contract(built) == []
    study = next(s for s in built['studies.json']['studies'] if s['nct_id'] == 'NCT09990002')
    assert study['registry_state'] == 'lookup_failed' and study['brief_title'] is None
    assert not [x for x in built['links.json']['links'] if x['nct_id'] == 'NCT09990002'
                and x['source_route'] == 'ctgov_results']
    assert _cell_of(built, 'NCT09990002', 'ctgov_results')['state'] == 'search_incomplete_or_blocked'


def test_posted_results_count_is_the_registry_fact_whatever_the_review(
    tmp_path: Path, frozen_cache: A.FrozenCache, built_files: dict[str, Any]
) -> None:
    ct = next(x for x in built_files['links.json']['links']
              if x['nct_id'] == 'NCT09990001' and x['source_route'] == 'ctgov_results')
    rev_csv = tmp_path / 'reviews.csv'
    with rev_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.REVIEW_COLUMNS)
        w.writerow([ct['link_id'], 'NCT09990001', 'Dr. Fictional', '2026-11-01', 'indeterminate', 'unsure'])
    reviewed = ART.build_artifacts(frozen_cache, **{**_build_kwargs(), 'reviews_path': rev_csv},
                                   as_of=date.fromisoformat(F.AS_OF))
    assert ART.check_contract(reviewed) == []
    # The review holds the CT.gov cell and the discordance flag back, as designed ...
    assert _cell_of(reviewed, 'NCT09990001', 'ctgov_results')['state'] == 'candidate_awaiting_review'
    assert _timeline_of(reviewed, 'NCT09990001')['discordance']['ctgov_results_posted'] is False
    # ... but the trial still carries a posted resultsSection, and the count says so.
    def metric(files: dict[str, Any], mid: str) -> dict[str, Any]:
        return next(m for m in files['summary.json']['metrics'] if m['id'] == mid)
    for mid in ('trials_ctgov_results_posted', 'trials_with_exact_qc_lag'):
        assert metric(reviewed, mid)['numerator'] == metric(built_files, mid)['numerator']
    assert metric(reviewed, 'trials_ctgov_results_posted')['numerator'] == 2
    assert reviewed['summary.json']['denominators']['ctgov_posted_trials']['n'] == 2


def test_same_day_resume_after_a_ctgov_refresh_records_both_data_timestamps(tmp_path: Path) -> None:
    # Both runs take their default stamps, so they fall on the same UTC day. Keyed one version record
    # per day, the resume reused the morning's version and the refresh went unrecorded.
    def refreshed_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-01T20:00:00'})
        return F.stub_get()(url, params)

    cache = tmp_path / 'same_day_cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(cache, 'ctgov'))
    A.fetch_ctgov_studies(['NCT09990001'], log, F.stub_get(), throttle)
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], log, refreshed_get, throttle)
    A.freeze_cache(cache, ['NCT09990001', 'NCT09990002'], frozen_at=F.GENERATED_AT)
    resumed = A.load_frozen_cache(cache)
    assert _version_stamps(resumed) == ['2026-11-01T08:00:00', '2026-11-01T20:00:00']
    with pytest.raises(ValueError, match=r'2026-11-01T08:00:00 to 2026-11-01T20:00:00.*pass --as-of'):
        ART.build_artifacts(resumed, **_build_kwargs())


def test_ctgov_refresh_during_one_run_records_both_data_timestamps(tmp_path: Path) -> None:
    # CT.gov refreshes once the first study has been served: the version observed after the last
    # study request records it, so the run's studies are not all attributed to the first timestamp.
    served: list[str] = []

    def refreshing_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            stamp = '2026-11-01T20:00:00' if served else '2026-11-01T08:00:00'
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': stamp})
        served.append(url)
        return F.stub_get()(url, params)

    cache = tmp_path / 'mid_run_cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], A.ResponseLog(A.raw_log_path(cache, 'ctgov')),
                          refreshing_get, throttle)
    A.freeze_cache(cache, ['NCT09990001', 'NCT09990002'], frozen_at=F.GENERATED_AT)
    frozen = A.load_frozen_cache(cache)
    assert _version_stamps(frozen) == ['2026-11-01T08:00:00', '2026-11-01T20:00:00']
    with pytest.raises(ValueError, match=r'2026-11-01T08:00:00 to 2026-11-01T20:00:00.*pass --as-of'):
        ART.build_artifacts(frozen, **_build_kwargs())


def _version_fails(phase_calls: set[int]) -> tuple[A.Get, list[str]]:
    """A transport whose n-th `/version` request (1-based) fails with a timeout when n is in
    `phase_calls`, every retry included (a retry repeats the previous call); other requests are
    served by the stub."""
    seen: list[str] = []
    n_version = [0]

    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        seen.append(url)
        if url == f'{A.CTGOV_API}/version':
            if len(seen) == 1 or seen[-2] != url:  # a new request, not a retry of the last one
                n_version[0] += 1
            if n_version[0] in phase_calls:
                return None, {}, b'', 'TimeoutError: simulated'
        return F.stub_get()(url, params)

    return get, seen


def test_failed_version_before_the_studies_requests_no_study(tmp_path: Path) -> None:
    get, seen = _version_fails({1})
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='before the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001'], log, get, throttle)
    assert all(u == f'{A.CTGOV_API}/version' for u in seen)  # no study was requested
    assert not [k for k in log.records if k.startswith('ctgov/study/')]


def test_failed_version_after_the_studies_blocks_freezing_until_a_later_run(tmp_path: Path) -> None:
    # The run's end observation fails after CT.gov refreshed; its begin observation must not be
    # taken to cover the study answers, so the cache cannot be frozen until a later run observes.
    get, _ = _version_fails({2})
    cache = tmp_path / 'cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(cache, 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='after the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], log, get, throttle)
    assert log.settled('ctgov/study/NCT09990002') is not None  # the answers are kept for the resume
    with pytest.raises(A.CacheIntegrityError, match='after the last study answer'):
        A.freeze_cache(cache, ['NCT09990001', 'NCT09990002'], frozen_at=F.GENERATED_AT)
    assert not (cache / 'manifest.json').exists()

    def refreshed_get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-01T20:00:00'})
        return F.stub_get()(url, params)

    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], log, refreshed_get, throttle)
    A.freeze_cache(cache, ['NCT09990001', 'NCT09990002'], frozen_at=F.GENERATED_AT)
    frozen = A.load_frozen_cache(cache)
    assert _version_stamps(frozen) == ['2026-11-01T08:00:00', '2026-11-01T20:00:00']
    with pytest.raises(ValueError, match=r'2026-11-01T08:00:00 to 2026-11-01T20:00:00.*pass --as-of'):
        ART.build_artifacts(frozen, **_build_kwargs())


_BAD_DATA_TIMESTAMPS = ['not-a-timestamp', '2026-11-01T23:30:00-05:00', '2026-11-01T23:30:00Z', '2026-11-01']


def _version_says(stamps: dict[int, Any]) -> A.Get:
    """A transport whose n-th `/version` answer (1-based) carries `stamps[n]` as its `dataTimestamp`
    (`2026-11-01T08:00:00` otherwise); other requests are served by the stub."""
    n_version = [0]

    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            n_version[0] += 1
            stamp = stamps.get(n_version[0], '2026-11-01T08:00:00')
            return F._json_resp({'apiVersion': '2.0.5', 'dataTimestamp': stamp})
        return F.stub_get()(url, params)

    return get


@pytest.mark.parametrize('stamp', [*_BAD_DATA_TIMESTAMPS, '', None])
def test_live_version_with_an_unreadable_data_timestamp_stops_the_run(tmp_path: Path, stamp: Any) -> None:
    # On 7c380fc the first four were logged as successful observations and frozen: `not-a-timestamp`
    # later stopped the build in `date.fromisoformat`, and a zoned value gave the wrong `as_of` day.
    # The blank and null values were refused there too.
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='before the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001'], log, _version_says({1: stamp}), throttle)
    (rec,) = [r for k, r in log.records.items() if k.startswith('ctgov/version/')]
    assert rec['status'] == A.UNUSABLE_200 and rec['error'] == 'invalid_answer_shape_on_200'
    assert not [k for k in log.records if k.startswith('ctgov/study/')]


@pytest.mark.parametrize('stamp', _BAD_DATA_TIMESTAMPS)
def test_end_version_with_an_unreadable_data_timestamp_blocks_freezing(tmp_path: Path, stamp: str) -> None:
    cache = tmp_path / 'cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(cache, 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='after the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001'], log, _version_says({2: stamp}), throttle)
    with pytest.raises(A.CacheIntegrityError, match='after the last study answer'):
        A.freeze_cache(cache, ['NCT09990001'], frozen_at=F.GENERATED_AT)


def test_logged_success_with_an_unreadable_data_timestamp_counts_as_a_failed_observation() -> None:
    # A line an earlier acquisition logged with status 200 and a zoned timestamp: the bracket check
    # does not take it to cover the studies, and run.json `sources.ctgov` leaves it out.
    def rec(key: str, stamp: str = '2026-11-01T08:00:00') -> dict[str, Any]:
        body = {'apiVersion': '2.0.5', 'dataTimestamp': stamp} if 'version' in key else {}
        return {'key': key, 'status': 200, 'fetched_at': '2026-11-01T10:00:00+00:00', 'body': body}

    begin, study = rec('ctgov/version/x/begin'), rec('ctgov/study/NCT09990001')
    zoned_end = rec('ctgov/version/x/end', '2026-11-01T23:30:00-05:00')
    assert A.ctgov_version_bracket_problem([begin, study, rec('ctgov/version/x/end')]) is None
    assert 'after the last study answer' in (A.ctgov_version_bracket_problem([begin, study, zoned_end]) or '')
    info = ART._ctgov_version_info({r['key']: r for r in (begin, study, zoned_end)})
    assert [v['key'] for v in info['versions']] == ['ctgov/version/x/begin']
    assert info['data_timestamp_last'] == '2026-11-01T08:00:00'


def test_version_bracket_rule_on_hand_made_logs() -> None:
    def rec(key: str, status: int | None = 200) -> dict[str, Any]:
        body = {'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-01T08:00:00'} if 'version' in key else {}
        return {'key': key, 'status': status, 'fetched_at': '2026-11-01T10:00:00+00:00', 'body': body}

    begin, study, end = rec('ctgov/version/x/begin'), rec('ctgov/study/NCT09990001'), rec('ctgov/version/x/end')
    problem = A.ctgov_version_bracket_problem
    # Log order decides, though every line here shares one fetched_at second.
    assert problem([begin, study, end]) is None
    assert problem([study]) is not None
    assert 'after the last study answer' in (problem([begin, study]) or '')
    assert 'before the first study answer' in (problem([study, end]) or '')
    assert 'after the last study answer' in (problem([begin, study, rec('ctgov/version/x/end', None)]) or '')
    # A study answered again later counts at its latest line.
    assert 'after the last study answer' in (problem([begin, study, end, study]) or '')
    # A later run's observation bounds an earlier run's answers.
    assert problem([begin, study, rec('ctgov/version/y/begin')]) is None
    # A study request that never got a final answer carries no CT.gov data and needs no bracket,
    # and nor does a local record, whose data timestamp is the one stated for it.
    assert problem([rec('ctgov/study/NCT09990001', None)]) is None
    local = {**study, 'tag': {'nct_id': 'NCT09990001', 'origin': 'local_records'}}
    assert problem([rec('ctgov/version/2026-11-01'), local]) is None


def test_a_cache_frozen_before_the_version_bracket_check_is_refused(
    tmp_path: Path, frozen_cache: A.FrozenCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bracket needs the raw log's order, which the key-sorted archive drops, so it cannot be
    # re-checked on load; a version-1 manifest (frozen without it) is refused by version instead.
    cache = tmp_path / 'old cache'  # a space, so the command must quote the path
    shutil.copytree(frozen_cache.cache_dir, cache)
    manifest_path = cache / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['schema_version'] == A.CACHE_SCHEMA_VERSION == 2
    manifest['schema_version'] = 1
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(A.CacheIntegrityError, match=r'schema_version 1 \(expected 2\).*freeze it again') as refused:
        A.load_frozen_cache(cache)
    out = tmp_path / 'out'
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--as-of', F.AS_OF,
                           '--generated-at', F.GENERATED_AT]) == 1
    assert not out.exists()

    # Follow the message: its command, split as a shell would, runs the script through python3 with
    # the quoted cache path; FILE is the old manifest's IDs. The ctgov stage reuses every settled
    # study and observes the version twice; freeze then succeeds.
    message = str(refused.value)
    assert f"this manifest's {len(F.NCT_IDS)} requested_nct_ids" in message
    argv = shlex.split(re.search(r'`([^`]+)`', message).group(1))  # type: ignore[union-attr]
    assert argv[:3] == ['python3', 'scripts/build_drug_disclosure.py', 'acquire']
    assert (ROOT / argv[1]).is_file()
    id_file = tmp_path / 'ids.txt'
    id_file.write_text('\n'.join(manifest['requested_nct_ids']) + '\n', encoding='utf-8')
    args = [str(id_file) if a == 'FILE' else a for a in argv[2:]]
    assert args[args.index('--cache') + 1] == str(cache)
    calls: list[str] = []
    monkeypatch.setattr(A, 'requests_get', lambda: F.stub_get(calls))
    assert cli_build.main(args) == 0
    assert calls and all(c.startswith(f'{A.CTGOV_API}/version?') for c in calls)
    assert A.load_frozen_cache(cache).manifest['schema_version'] == 2


@pytest.mark.parametrize(
    'nct', ['NCT\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668', 'NCT01234567\n', 'NCT0123456']
)
def test_nct_ids_are_eight_ascii_digits_with_nothing_after(nct: str) -> None:
    # \d matched other scripts' digits and $ allowed a trailing newline.
    assert A.NCT_RE.match(nct) is None
    assert E.NCT_RE.match(nct) is None
    assert A.NCT_RE.match('NCT01234567') is not None and E.NCT_RE.match('NCT01234567') is not None


def test_acquire_cli_refuses_when_the_version_request_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/version':
            return 400, {}, b'{}', None  # not retried
        return F.stub_get()(url, params)

    monkeypatch.setattr(A, 'requests_get', lambda: get)
    cache = tmp_path / 'cache'
    assert cli_build.main(['acquire', '--cache', str(cache), '--nct-id', 'NCT09990001']) == 1
    assert not (cache / 'manifest.json').exists()


def test_unknown_git_provenance_is_null_builds_locally_and_is_never_exported(
    tmp_path: Path,
    frozen_cache: A.FrozenCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def git_missing(cmd: list[str], **_kwargs: Any) -> _Proc:
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    def git_answers(head: tuple[int, str], status: tuple[int, str]) -> Any:
        def run(cmd: list[str], **_kwargs: Any) -> _Proc:
            return _Proc(*(head if cmd[1] == 'rev-parse' else status))
        return run

    commit = 'c' * 40
    cases: list[tuple[Any, Any, Any]] = [
        (git_missing, None, None),
        (git_answers((128, ''), (0, '')), None, None),                     # not a repository
        (git_answers((0, 'not-a-commit\n'), (0, '')), None, None),        # unusable rev-parse output
        (git_answers((0, commit + '\n'), (128, '')), None, None),         # status fails: no half provenance
        (git_answers((0, commit + '\n'), (0, '')), commit, False),
        (git_answers((0, commit + '\n'), (0, ' M disclosure/acquire.py\n')), commit, True),
    ]
    for fake, want_commit, want_dirty in cases:
        monkeypatch.setattr(cli_build.subprocess, 'run', fake)
        info = cli_build.producer_metadata()
        assert (info['commit'], info['uncommitted_changes']) == (want_commit, want_dirty)

    # With git missing the build is still written for local diagnostics, and passes the contract...
    monkeypatch.setattr(cli_build.subprocess, 'run', git_missing)
    unknown = tmp_path / 'unknown_build'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(unknown),
                           '--as-of', F.AS_OF, '--generated-at', F.GENERATED_AT]) == 0
    assert cli_check.main([str(unknown)]) == 0
    run = json.loads((unknown / 'run.json').read_text(encoding='utf-8'))
    assert (run['producer']['commit'], run['producer']['uncommitted_changes']) == (None, None)
    # ...but the site export refuses it and writes nothing.
    site = tmp_path / 'site'
    assert cli_export.main([str(unknown), str(site)]) == 1
    assert not site.exists()

    # A dirty tree is honest provenance and exports.
    monkeypatch.setattr(cli_build.subprocess, 'run', git_answers((0, commit + '\n'), (0, ' M x\n')))
    dirty = tmp_path / 'dirty_build'
    assert cli_build.main(['build', '--cache', str(frozen_cache.cache_dir), '--out', str(dirty),
                           '--as-of', F.AS_OF, '--generated-at', F.GENERATED_AT]) == 0
    assert cli_export.main([str(dirty), str(site)]) == 0

    # The contract accepts a 40-hex commit or null, and a bool or null dirty flag, never false without a commit.
    files, _ = ART.load_dir(unknown)
    bad_commit = copy.deepcopy(files)
    bad_commit['run.json']['producer']['commit'] = 'abc123'
    assert any('must be a 40-hex commit or null' in p for p in ART.check_contract(bad_commit))
    false_without_commit = copy.deepcopy(files)
    false_without_commit['run.json']['producer']['uncommitted_changes'] = False
    assert any('must be null when producer.commit is null' in p for p in ART.check_contract(false_without_commit))


@pytest.mark.parametrize(
    ('references_module', 'problem'),
    [
        ('not an object', 'referencesModule is not an object'),
        ({'references': 'PMID 31234569'}, 'referencesModule.references is not a list'),
        ({'references': ['PMID 31234569']}, 'referencesModule.references[0] is not an object'),
        (
            {'references': [{'pmid': '31234569', 'type': 'RESULT'}, 7]},
            'referencesModule.references[1] is not an object',
        ),
        ({'references': [{}]}, 'referencesModule.references[0] has no citation text or PMID'),
        ({'references': [{'type': 'RESULT', 'citation': '   '}]}, 'references[0] has no citation text or PMID'),
        ({'references': [{'pmid': 31234569, 'citation': 'Fictional et al.'}]}, 'references[0].pmid is not a string'),
        ({'references': [{'citation': ['Fictional et al.']}]}, 'references[0].citation is not a string'),
        ({'references': [{'citation': 'Fictional et al.', 'type': 3}]}, 'references[0].type is not a string'),
    ],
)
def test_malformed_references_module_is_an_incomplete_search_not_zero_citations(
    references_module: Any, problem: str,
) -> None:
    nct = 'NCT09990023'
    study = F._make_study(nct, title='Malformed references', lead_sponsor='Fictional Sponsor')
    study['protocolSection']['referencesModule'] = references_module
    ctgov_rec = {'status': 200, 'body': study}
    assert (A.registry_references_problem(study) or "").endswith(problem)
    cell = C.registry_references_cell(nct, ctgov_rec, [], {}, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert problem in cell['reason']


@pytest.mark.parametrize('references_module', ['<absent>', {}, {'references': []}])
def test_absent_or_empty_references_module_is_a_completed_search_over_zero_citations(references_module: Any) -> None:
    nct = 'NCT09990024'
    study = F._make_study(nct, title='No references', lead_sponsor='Fictional Sponsor')
    study['protocolSection'].pop('referencesModule', None)
    if references_module != '<absent>':
        study['protocolSection']['referencesModule'] = references_module
    assert A.registry_references_problem(study) is None
    assert A.extract_registry_references(study) == []
    cell = C.registry_references_cell(nct, {'status': 200, 'body': study}, [], {}, True)
    assert cell['state'] == 'no_candidate_in_completed_scope', cell


@pytest.mark.parametrize(('reference_type', 'expected'), [
    # An unreviewed BACKGROUND entry read `available_document_insufficient` (a completed search) on the old
    # head although the second entry was never read.
    ('BACKGROUND', 'search_incomplete_or_blocked'),
    # A RESULT PMID resolved on PubMed is positive evidence and still verifies the route.
    ('RESULT', 'verified_link'),
])
def test_unreadable_reference_entry_blocks_negative_states_but_not_a_verified_readable_entry(
    reference_type: str, expected: str,
) -> None:
    nct = 'NCT09990025'
    study = F._make_study(nct, title='Partly malformed references', lead_sponsor='Fictional Sponsor',
                          references=[{'pmid': '31234570', 'type': reference_type, 'citation': 'Fictional et al.'}])
    study['protocolSection']['referencesModule']['references'].append('PMID 31234571')
    ctgov_rec = {'status': 200, 'body': study}
    # A resolved esummary answer carries the PMID's entry with its `uid` (an empty `result` is no
    # evidence of resolution, test_empty_pubmed_entry_is_a_failed_lookup_not_a_verified_link).
    entry = {'uid': '31234570', 'title': 'Fictional results.', 'pubdate': '2021 Mar'}
    lit = {'literature/pubmed_pmid/31234570': {'status': 200, 'body': {'result': {'31234570': entry}}}}
    links = E.build_registry_reference_links(nct, ctgov_rec, lit)
    assert len(links) == 1
    cell = C.registry_references_cell(nct, ctgov_rec, links, lit, True)
    assert cell['state'] == expected, cell
    if expected == 'search_incomplete_or_blocked':
        assert 'referencesModule.references[1] is not an object' in cell['reason']
    # A person's confirmation of the readable entry settles the route either way.
    confirmed = [dict(links[0], review_status='confirmed_same_trial_results')]
    assert C.registry_references_cell(nct, ctgov_rec, confirmed, lit, True)['state'] == 'verified_link'


@pytest.mark.parametrize('body', [
    {'result': {}},
    {'result': {'31234570': {}}},
    {'result': {'31234570': {'title': 'No uid'}}},
    {'result': {'31234570': {'uid': '31234571', 'title': 'Another PMID'}}},
])
def test_empty_pubmed_entry_is_a_failed_lookup_not_a_verified_link(tmp_path: Path, body: dict[str, Any]) -> None:
    # On 4acf4ae each was settled at acquisition and read as `identifier_confirmed`, so a RESULT reference
    # made the route `verified_link` with no evidence that the PMID resolved.
    nct = 'NCT09990027'
    study = F._make_study(nct, title='Empty PubMed answer', lead_sponsor='Fictional Sponsor',
                          references=[{'pmid': '31234570', 'type': 'RESULT', 'citation': 'Fictional et al.'}])
    ctgov_rec = {'status': 200, 'body': study}
    assert not A.pubmed_esummary_valid(body, '31234570')
    lit = {'literature/pubmed_pmid/31234570': {'status': 200, 'body': body}}  # a cache frozen before the rule
    links = E.build_registry_reference_links(nct, ctgov_rec, lit)
    assert [x['automated_validation'] for x in links] == ['not_validated']
    cell = C.registry_references_cell(nct, ctgov_rec, links, lit, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert cell['detail']['failed_pmids'] == ['31234570']
    # At acquisition the answer is logged as unusable, never settled.
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'literature'))
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    rec = A.logged_json(log, lambda url, params: F._json_resp(body), throttle,
                        'literature/pubmed_pmid/31234570', A.NCBI_EUTILS, {'id': '31234570'},
                        valid=lambda b: A.pubmed_esummary_valid(b, '31234570'))
    assert rec['status'] == A.UNUSABLE_200


def test_openfda_result_without_an_application_number_is_an_incomplete_search() -> None:
    # On 77e9a2d the result below became `app:0`, its NCT token made the link identifier_confirmed, and
    # coverage published a verified FDA link with no identifiable application.
    nct = 'NCT09990002'
    study = F.STUDIES[nct]
    body = {'meta': {'results': {'total': 1}}, 'results': [{'notes': f'Pivotal trial {nct}.'}]}
    assert not A.openfda_drugsfda_valid(body)
    for blank in ('', '  ', 7):
        assert not A.openfda_drugsfda_valid({**body, 'results': [{'application_number': blank, 'notes': nct}]})
    records = {f'openfda/nct/{nct}': {'status': 200, 'body': body}}  # a cache frozen before the rule
    links = E.build_openfda_links(nct, study, records, E.trial_sponsor_match_keys(study))
    assert not [x for x in links if x['automated_validation'] == 'identifier_confirmed']
    cell = C.openfda_drugsfda_cell(nct, {'status': 200, 'body': study}, study, records, links, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    # Control: the same result with its application number is a valid answer.
    assert A.openfda_drugsfda_valid({**body, 'results': [{'application_number': 'NDA990002', 'notes': nct}]})


@pytest.mark.parametrize('hit', [
    {},
    {'title': 'A title but no identifier'},
    {'id': 'EPMC1'},
    {'id': '  ', 'title': 'Blank identifier'},
    {'id': 'EPMC1', 'title': ''},
    # On 30e1cae a mistyped identifier beside a readable `id` passed, and `_epmc_target` turned it into
    # text and preferred it: the numeric pmid became the target of an identifier_confirmed link.
    {'id': 'EPMC1', 'pmid': 31234570, 'title': 'T', 'abstractText': 'Results of NCT09990004.'},
    {'id': 'EPMC1', 'pmcid': ['PMC1'], 'title': 'T', 'abstractText': 'Results of NCT09990004.'},
    {'id': 'EPMC1', 'doi': {'value': '10.1/x'}, 'title': 'T', 'abstractText': 'Results of NCT09990004.'},
    {'id': 7, 'pmid': '31234570', 'title': 'T', 'abstractText': 'Results of NCT09990004.'},
])
def test_unreadable_europe_pmc_hit_is_an_incomplete_search(hit: dict[str, Any]) -> None:
    # On 77e9a2d `{}` became the placeholder `epmc:0`, was set aside for carrying no NCT token, and the
    # route read no_candidate_in_completed_scope, a completed negative.
    nct = 'NCT09990004'
    body = {'hitCount': 1, 'resultList': {'result': [hit]}}
    assert not A.epmc_search_valid(body)
    records = {f'literature/epmc_nct/{nct}': {'status': 200, 'body': body}}  # a cache frozen before the rule
    links = E.build_literature_search_links(nct, records)
    assert links == []
    cell = C.literature_search_cell(nct, records, links, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell


def test_readable_europe_pmc_hits_are_accepted() -> None:
    for hit in ({'id': 'EPMC1', 'title': 'T'}, {'pmid': '31234570', 'title': 'T'}, {'doi': '10.1/x', 'title': 'T'},
                {'id': 'EPMC1', 'pmid': None, 'pmcid': None, 'doi': None, 'title': 'T'},
                {'id': 'EPMC1', 'pmid': '', 'title': 'T'}):
        assert A.epmc_search_valid({'hitCount': 1, 'resultList': {'result': [hit]}})
    assert A.epmc_search_valid({'hitCount': 0, 'resultList': {'result': []}})


def test_resolved_and_unresolved_pubmed_entries_are_accepted() -> None:
    assert A.pubmed_esummary_valid({'result': {'31234570': {'uid': '31234570', 'title': 'x'}}}, '31234570')
    assert A.pubmed_esummary_valid({'result': {'31234570': {'uid': '31234570', 'error': 'cannot get'}}}, '31234570')
    assert A.pubmed_esummary_valid({'result': {'31234570': {'error': 'cannot get document summary'}}}, '31234570')
    assert not A.pubmed_esummary_valid({'result': {'31234570': {'error': '  '}}}, '31234570')


def test_local_freeze_refuses_a_retrieval_time_after_the_freeze(tmp_path: Path) -> None:
    # On 4acf4ae a --fetched-at in 2100 froze, and the build's default as_of followed its data timestamp.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'NCT09990004.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    cache = tmp_path / 'local_cache'
    with pytest.raises(ValueError, match='is later than the freeze'):
        A.ingest_local_ctgov_records(local_src, cache, fetched_at='2100-01-01T00:00:00Z',
                                     data_timestamp='2099-12-31T10:00:00', frozen_at=F.GENERATED_AT)
    assert not cache.exists()
    assert cli_build.main(['freeze-local', '--records', str(local_src), '--cache', str(cache),
                           '--fetched-at', '2100-01-01T00:00:00Z', '--data-timestamp', '2099-12-31T10:00:00']) == 1
    assert not cache.exists()
    # Control: a retrieval time equal to the freeze is accepted.
    manifest = A.ingest_local_ctgov_records(local_src, cache, fetched_at=F.GENERATED_AT,
                                            data_timestamp='2026-11-01T08:00:00', frozen_at=F.GENERATED_AT)
    assert manifest['frozen_at'] == F.GENERATED_AT


@pytest.mark.parametrize('entry', [{}, {'type': 'RESULT'}, {'type': 'RESULT', 'citation': '   '}])
def test_reference_entry_with_no_citation_or_pmid_is_no_candidate_and_no_negative(entry: dict[str, Any]) -> None:
    # On c5ed8dd such an entry became the candidate `ref:<nct>:0` with the excerpt `<nct> reference[0]`, and
    # a reviewer's rejection of that placeholder turned the route into `no_candidate_in_completed_scope`.
    nct = 'NCT09990026'
    study = F._make_study(nct, title='Unidentifiable reference', lead_sponsor='Fictional Sponsor', references=[entry])
    ctgov_rec = {'status': 200, 'body': study}
    assert A.extract_registry_references(study) == []
    assert E.build_registry_reference_links(nct, ctgov_rec, {}) == []
    cell = C.registry_references_cell(nct, ctgov_rec, [], {}, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert 'has no citation text or PMID' in cell['reason']


@pytest.mark.parametrize('entry', [
    {'pmid': 31234569, 'type': 'RESULT', 'citation': 'Fictional et al.'},
    {'pmid': '31234569', 'type': 'RESULT', 'citation': ['Fictional et al.']},
    {'pmid': '31234569', 'type': 3, 'citation': 'Fictional et al.'},
])
def test_reference_entry_with_a_mistyped_field_cannot_verify_the_route(entry: dict[str, Any]) -> None:
    # On c7c16f7 each entry was still extracted (the integer PMID coerced to text), resolved against the
    # cached PubMed answer and made the route `verified_link` despite the reported problem.
    nct = 'NCT09990027'
    study = F._make_study(nct, title='Mistyped reference', lead_sponsor='Fictional Sponsor', references=[entry])
    ctgov_rec = {'status': 200, 'body': study}
    lit = {'literature/pubmed_pmid/31234569': {'status': 200, 'body': {'result': {'31234569': {'title': 'T'}}}}}
    assert A.extract_registry_references(study) == []
    links = E.build_registry_reference_links(nct, ctgov_rec, lit)
    assert links == []
    cell = C.registry_references_cell(nct, ctgov_rec, links, lit, True)
    assert cell['state'] == 'search_incomplete_or_blocked', cell
    assert 'is not a string' in cell['reason']



@pytest.mark.parametrize(
    ('path', 'value', 'problem'),
    [
        (('protocolSection', 'statusModule'), ['bad'], 'protocolSection.statusModule is list, not dict'),
        (('protocolSection', 'statusModule', 'primaryCompletionDateStruct'), '2021-03',
         'primaryCompletionDateStruct is str, not dict'),
        (('protocolSection', 'statusModule', 'completionDateStruct', 'date'), 20210301,
         'completionDateStruct.date is int, not str'),
        (('protocolSection', 'designModule', 'enrollmentInfo'), [240], 'enrollmentInfo is list, not dict'),
        (('protocolSection', 'designModule', 'enrollmentInfo', 'count'), '240', 'enrollmentInfo.count is str, not int'),
        (('protocolSection', 'designModule', 'enrollmentInfo', 'count'), True, 'enrollmentInfo.count is bool, not int'),
        (('protocolSection', 'sponsorCollaboratorsModule', 'leadSponsor'), 'Fictional', 'leadSponsor is str, not dict'),
        (('protocolSection', 'sponsorCollaboratorsModule', 'collaborators'), 7, 'collaborators is int, not list'),
        (('protocolSection', 'sponsorCollaboratorsModule', 'collaborators'), ['CRO'],
         'collaborators[0] is str, not dict'),
        (('protocolSection', 'conditionsModule', 'conditions'), [7], 'conditions[0] is int, not str'),
        (('protocolSection', 'oversightModule'), 'yes', 'oversightModule is str, not dict'),
        (('resultsSection',), ['posted'], 'resultsSection is list, not dict'),
        # On 463a3d7 a results module of another type passed, and the record made a verified results link.
        (('resultsSection', 'participantFlowModule'), 'broken',
         'resultsSection.participantFlowModule is str, not dict'),
        (('resultsSection', 'outcomeMeasuresModule'), [], 'resultsSection.outcomeMeasuresModule is list, not dict'),
    ],
)
def test_study_record_with_a_mistyped_part_reads_as_a_failed_lookup_not_a_crash(
    tmp_path: Path, path: tuple[str, ...], value: Any, problem: str,
) -> None:
    # On fb74101 each of these raised (AttributeError / TypeError) in the timeline, evidence or study
    # rows, so a frozen cache holding such a record could not be built.
    nct = 'NCT09990001'
    study = _study_with(F.STUDIES[nct], path, value)
    assert problem in (A.ctgov_study_problem(study) or ''), A.ctgov_study_problem(study)
    cache = F.acquire_fixture_cache(tmp_path / 'c')
    cache.ctgov[f'ctgov/study/{nct}'] = dict(cache.ctgov[f'ctgov/study/{nct}'], body=study)
    built = ART.build_artifacts(
        cache,
        reviews_path=ROOT / 'disclosure/reviews.csv',
        statements_path=ROOT / 'disclosure/documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors/company_aliases.csv',
        producer={'commit': None, 'uncommitted_changes': None},
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=[nct],
    )
    row = next(s for s in built['studies.json']['studies'] if s['nct_id'] == nct)
    assert row['registry_state'] == 'lookup_failed'
    cov = next(c for c in built['coverage.json']['trials'] if c['nct_id'] == nct)
    assert all(cell['state'] != 'no_candidate_in_completed_scope' for cell in cov['routes']), cov


def test_contradictory_results_flag_is_not_read_as_the_registry_negative(tmp_path: Path) -> None:
    # On de5eca4 `hasResults: false` beside a non-empty resultsSection read as the CT.gov negative:
    # `no_candidate_in_completed_scope`, `UNREPORTED_PAST_365D` and `published_results_while_ctgov_unreported`.
    nct = 'NCT09990002'
    study = _study_with(F.STUDIES[nct], ('hasResults',), False)
    study['resultsSection'] = {'participantFlowModule': {'groups': [{'id': 'FG000', 'title': 'Arm A'}]}}
    assert A.ctgov_study_problem(study) == 'hasResults is false but resultsSection is non-empty'
    assert A.ctgov_study_problem({**study, 'resultsSection': {}}) is None  # an empty section is no results
    cache = F.acquire_fixture_cache(tmp_path / 'c')
    cache.ctgov[f'ctgov/study/{nct}'] = dict(cache.ctgov[f'ctgov/study/{nct}'], body=study)
    built = ART.build_artifacts(
        cache,
        reviews_path=ROOT / 'disclosure/reviews.csv',
        statements_path=ROOT / 'disclosure/documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors/company_aliases.csv',
        producer={'commit': None, 'uncommitted_changes': None},
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=[nct],
    )
    assert next(s for s in built['studies.json']['studies'] if s['nct_id'] == nct)['registry_state'] == 'lookup_failed'
    assert _cell_of(built, nct, 'ctgov_results')['state'] == 'search_incomplete_or_blocked'
    timeline = _timeline_of(built, nct)
    assert timeline['window_365d']['state'] != 'UNREPORTED_PAST_365D'
    assert timeline['discordance']['state'] == 'coverage_incomplete_or_awaiting_review'


def test_results_posting_date_beside_hasresults_false_is_not_read_as_posted_and_unreported(tmp_path: Path) -> None:
    # On 4e5a3da `hasResults: false` with a results posting date built as window `POSTED_WITHIN_365D`
    # beside `ctgov_results` `no_candidate_in_completed_scope`: posted and unreported at once.
    nct = 'NCT09990002'
    study = copy.deepcopy(F.STUDIES[nct])
    study['hasResults'] = False
    study['protocolSection']['statusModule']['resultsFirstPostDateStruct'] = {'date': '2021-05-01', 'type': 'ACTUAL'}
    assert A.ctgov_study_problem(study) == 'hasResults is false but statusModule.resultsFirstPostDateStruct has a date'
    cache = F.acquire_fixture_cache(tmp_path / 'c')
    cache.ctgov[f'ctgov/study/{nct}'] = dict(cache.ctgov[f'ctgov/study/{nct}'], body=study)
    built = ART.build_artifacts(
        cache,
        reviews_path=ROOT / 'disclosure/reviews.csv',
        statements_path=ROOT / 'disclosure/documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors/company_aliases.csv',
        producer={'commit': None, 'uncommitted_changes': None},
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=[nct],
    )
    assert _cell_of(built, nct, 'ctgov_results')['state'] == 'search_incomplete_or_blocked'
    timeline = _timeline_of(built, nct)
    assert timeline['discordance']['state'] == 'coverage_incomplete_or_awaiting_review'
    assert timeline['window_365d']['state'] != 'POSTED_WITHIN_365D'


# ---------------------------------------------------------------- dates after retrieval (CT.gov)

_LATE_STAMP = '2100-01-01T00:00:00'


def test_live_version_dated_after_retrieval_stops_the_run(tmp_path: Path) -> None:
    # On 7e8612f a /version answer of 2100 in 2026 was a successful observation: the bracket check took
    # it, the freeze kept it, and the build's default `as_of` became 2100-01-01.
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='before the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001'], log, _version_says({1: _LATE_STAMP}), throttle)
    (rec,) = [r for k, r in log.records.items() if k.startswith('ctgov/version/')]
    assert rec['status'] == A.UNUSABLE_200
    assert not [k for k in log.records if k.startswith('ctgov/study/')]


def test_end_version_dated_after_retrieval_blocks_freezing(tmp_path: Path) -> None:
    cache = tmp_path / 'cache'
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(cache, 'ctgov'))
    with pytest.raises(A.CTGovVersionUnavailable, match='after the study requests failed'):
        A.fetch_ctgov_studies(['NCT09990001'], log, _version_says({2: _LATE_STAMP}), throttle)
    with pytest.raises(A.CacheIntegrityError, match='after the last study answer'):
        A.freeze_cache(cache, ['NCT09990001'], frozen_at=F.GENERATED_AT)


def test_live_version_within_a_utc_offset_of_retrieval_is_an_observation(tmp_path: Path) -> None:
    # Control: CT.gov states no zone for dataTimestamp, so one up to 14 hours ahead of UTC is possible.
    ahead = (datetime.now(timezone.utc) + timedelta(hours=13)).replace(tzinfo=None).isoformat(timespec='seconds')
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'ctgov'))
    A.fetch_ctgov_studies(['NCT09990001'], log, _version_says({1: ahead, 2: ahead}), throttle)
    assert [r['status'] for k, r in sorted(log.records.items()) if k.startswith('ctgov/version/')] == [200, 200]


@pytest.mark.parametrize(
    ('stamp', 'kept'),
    [
        (_LATE_STAMP, False),
        ('2026-11-02T00:00:01', False),  # just over 14 hours after retrieval at 10:00 UTC
        ('2026-11-02T00:00:00', True),  # 14 hours after: within any real UTC offset
        ('2026-11-01T08:00:00', True),
    ],
)
def test_logged_version_dated_after_its_retrieval_counts_as_a_failed_observation(stamp: str, kept: bool) -> None:
    def rec(key: str, data_timestamp: str = '2026-11-01T08:00:00') -> dict[str, Any]:
        body = {'apiVersion': '2.0.5', 'dataTimestamp': data_timestamp} if 'version' in key else {}
        return {'key': key, 'status': 200, 'fetched_at': '2026-11-01T10:00:00+00:00', 'body': body}

    begin, study, end = rec('ctgov/version/x/begin'), rec('ctgov/study/NCT09990001'), rec('ctgov/version/x/end', stamp)
    problem = A.ctgov_version_bracket_problem([begin, study, end])
    info = ART._ctgov_version_info({r['key']: r for r in (begin, study, end)})
    if kept:
        assert problem is None
        assert [v['key'] for v in info['versions']] == ['ctgov/version/x/begin', 'ctgov/version/x/end']
        return
    assert 'after the last study answer' in (problem or '')
    assert [v['key'] for v in info['versions']] == ['ctgov/version/x/begin']
    assert info['data_timestamp_last'] == '2026-11-01T08:00:00'


@pytest.mark.parametrize(('stamp', 'refused'), [(_LATE_STAMP, True), ('2026-11-01T08:00:00', False)])
def test_contract_refuses_a_recorded_data_timestamp_later_than_its_retrieval(stamp: str, refused: bool) -> None:
    version = {
        'key': 'ctgov/version/x/begin',
        'fetched_at': '2026-11-01T10:00:00+00:00',
        'api_version': '2.0.5',
        'data_timestamp': stamp,
    }
    ctgov_src = {
        'versions': [version],
        'data_timestamp': stamp,
        'data_timestamp_first': stamp,
        'data_timestamp_last': stamp,
        'n_records': 3,
    }
    problems = ART._ctgov_version_problems({'as_of': '2026-11-01', 'as_of_source': 'as_of_argument'}, ctgov_src)
    assert any('later than their retrieval allows' in p for p in problems) == refused, problems


_RESULTS_DATE_PATHS = {
    'post': ('protocolSection', 'statusModule', 'resultsFirstPostDateStruct', 'date'),
    'submit': ('protocolSection', 'statusModule', 'resultsFirstSubmitDate'),
    'qc': ('protocolSection', 'statusModule', 'resultsFirstSubmitQcDate'),
}


@pytest.mark.parametrize(
    ('field', 'value', 'refused'),
    [
        ('post', '2100-01-01', True),
        ('submit', '2100-01-01', True),
        ('qc', '2100-01-01', True),
        ('post', '2026-11-03', True),
        ('post', '2026-12', True),
        # Controls: fetched at 10:00 UTC on 1 November, the 2nd had begun at UTC+14, and a month
        # that contains the retrieval day is not after it.
        ('post', '2026-11-02', False),
        ('post', '2026-11', False),
    ],
)
def test_results_date_after_retrieval_is_read_as_a_failed_lookup(
    tmp_path: Path, field: str, value: str, refused: bool
) -> None:
    # On 7e8612f a 2100 posting date in a record fetched in 2026 was published on the verified CT.gov
    # results link and as `results_first_posted`, and beside the 2020 submission date the window read
    # PENDING_SUBMITTED_UNPOSTED while coverage reported the results posted.
    nct = 'NCT09990001'
    study = _study_with(F.STUDIES[nct], _RESULTS_DATE_PATHS[field], value)
    cache = F.acquire_fixture_cache(tmp_path / 'c')
    cache.ctgov[f'ctgov/study/{nct}'] = dict(
        cache.ctgov[f'ctgov/study/{nct}'], body=study, fetched_at='2026-11-01T10:00:00+00:00'
    )
    built = ART.build_artifacts(
        cache,
        reviews_path=ROOT / 'disclosure/reviews.csv',
        statements_path=ROOT / 'disclosure/documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors/company_aliases.csv',
        producer={'commit': None, 'uncommitted_changes': None},
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=[nct],
    )
    row = next(s for s in built['studies.json']['studies'] if s['nct_id'] == nct)
    results_links = [x for x in built['links.json']['links'] if x['source_route'] == 'ctgov_results']
    if not refused:
        assert row['registry_state'] == 'retrieved'
        assert [x['publication_or_action_date'] for x in results_links] == [value]
        return
    assert row['registry_state'] == 'lookup_failed'
    assert results_links == []
    assert _cell_of(built, nct, 'ctgov_results')['state'] == 'search_incomplete_or_blocked'
    assert _timeline_of(built, nct)['window_365d']['state'] != 'PENDING_SUBMITTED_UNPOSTED'
    assert value not in json.dumps(built, default=str)


def test_study_record_problem_names_the_results_date_after_retrieval() -> None:
    study = _study_with(F.STUDIES['NCT09990001'], _RESULTS_DATE_PATHS['post'], '2026-11-03')
    rec = {'key': 'ctgov/study/NCT09990001', 'status': 200, 'fetched_at': '2026-11-01T10:00:00+00:00', 'body': study}
    assert A.ctgov_study_problem(study) is None  # the body alone is readable
    assert A.ctgov_study_record_problem(rec) == (
        'statusModule.resultsFirstPostDateStruct.date (2026-11-03) is after the record was retrieved '
        '(2026-11-01T10:00:00+00:00)'
    )
    # Text that is not a day or a month is left to the readers, which treat it as missing, and a record
    # without a readable fetched_at is not bounded.
    odd = _study_with(study, _RESULTS_DATE_PATHS['post'], 'not-a-date')
    assert A.ctgov_study_record_problem({**rec, 'body': odd}) is None
    assert A.ctgov_study_record_problem({**rec, 'fetched_at': None}) is None


def test_live_study_answer_dated_after_retrieval_is_unusable(tmp_path: Path) -> None:
    # On 7e8612f it was settled as a 200 answer and frozen.
    study = _study_with(F.STUDIES['NCT09990001'], _RESULTS_DATE_PATHS['post'], '2100-01-01')

    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        if url == f'{A.CTGOV_API}/studies/NCT09990001':
            return F._json_resp(study)
        return F.stub_get()(url, params)

    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log = A.ResponseLog(A.raw_log_path(tmp_path / 'cache', 'ctgov'))
    A.fetch_ctgov_studies(['NCT09990001'], log, get, throttle)
    assert log.records['ctgov/study/NCT09990001']['status'] == A.UNUSABLE_200
    assert A.studies_from_ctgov_records(log.records, ['NCT09990001']) == {'NCT09990001': None}


@pytest.mark.parametrize(('post', 'refused'), [('2026-11-03', True), ('2026-11-02', False)])
def test_freeze_local_refuses_a_results_date_after_the_stated_retrieval(
    tmp_path: Path, post: str, refused: bool
) -> None:
    # Stated retrieval 2026-11-01T12:00:00Z: the 2nd had begun at UTC+14, the 3rd had not anywhere.
    local_src = tmp_path / 'local_records'
    local_src.mkdir()
    (local_src / 'a.json').write_text(json.dumps(F.STUDIES['NCT09990004']), encoding='utf-8')
    late = _study_with(F.STUDIES['NCT09990001'], _RESULTS_DATE_PATHS['post'], post)
    (local_src / 'b.json').write_text(json.dumps(late), encoding='utf-8')
    cache = tmp_path / 'cache'
    if not refused:
        A.ingest_local_ctgov_records(local_src, cache, **LOCAL_STAMPS)
        return
    with pytest.raises(ValueError, match=rf'b\.json study 1: statusModule\.resultsFirstPostDateStruct\.date \({post}\) '
                       r'is after the stated fetched_at'):
        A.ingest_local_ctgov_records(local_src, cache, **LOCAL_STAMPS)
    assert not (cache / 'manifest.json').exists()


# ---------------------------------------------------------------- posted results and statement sources

@pytest.mark.parametrize(
    ('modules', 'posted'),
    [
        ({'participantFlowModule': {}}, False),
        ({'participantFlowModule': None}, False),
        ({'participantFlowModule': {}, 'moreInfoModule': {}}, False),
        # Control, on both heads: an empty module beside a non-empty one, as 214 stored sections hold.
        ({'participantFlowModule': {}, 'outcomeMeasuresModule': {'outcomeMeasures': [{'type': 'PRIMARY'}]}}, True),
    ],
)
def test_results_section_without_a_non_empty_module_is_not_posted_results(
    tmp_path: Path, modules: dict[str, Any], posted: bool
) -> None:
    # On 463a3d7 any non-empty resultsSection object made a verified CT.gov results link and the route
    # `verified_link`, though modules that are all empty or null carry nothing posted.
    nct = 'NCT09990001'
    study = _study_with(F.STUDIES[nct], ('resultsSection',), modules)
    assert A.ctgov_study_problem(study) is None
    cache = F.acquire_fixture_cache(tmp_path / 'c')
    cache.ctgov[f'ctgov/study/{nct}'] = dict(cache.ctgov[f'ctgov/study/{nct}'], body=study)
    built = ART.build_artifacts(
        cache,
        reviews_path=ROOT / 'disclosure/reviews.csv',
        statements_path=ROOT / 'disclosure/documentary_statements.csv',
        sponsor_rules_path=ROOT / 'sponsors/company_aliases.csv',
        producer={'commit': None, 'uncommitted_changes': None},
        generated_at=F.GENERATED_AT,
        as_of=date.fromisoformat(F.AS_OF),
        requested_nct_ids=[nct],
    )
    links = [x for x in built['links.json']['links'] if x['source_route'] == 'ctgov_results']
    state = _cell_of(built, nct, 'ctgov_results')['state']
    if posted:
        assert (len(links), state) == (1, 'verified_link')
        return
    assert (links, state) == ([], 'available_document_insufficient')


def _statements_csv(path: Path, source_type: str) -> Path:
    with path.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(E.STATEMENT_COLUMNS)
        w.writerow(['stmt-001', 'NCT09990001', source_type, 'protocolSection.descriptionModule.briefSummary',
                    '2021-01', 'primary endpoint met at Week 24', 'Curator Jane', 'Verbatim quote'])
    return path


@pytest.mark.parametrize(
    'source_type',
    ['fda_review', 'fda_label', 'fda_document', 'csr_synopsis', 'sponsor_statement', 'regulatory_document', 'other'],
)
def test_statement_citing_a_source_the_build_cannot_retrieve_is_refused(tmp_path: Path, source_type: str) -> None:
    # On 463a3d7 each was accepted, and `_locate_statement_source` always gave `source_not_retrieved`, so
    # the statement could never count, even after a review confirmed it.
    path = _statements_csv(tmp_path / 'documentary_statements.csv', source_type)
    with pytest.raises(ValueError, match=rf"source_type '{source_type}' names a source the build does not retrieve"):
        E.read_statements(path)


@pytest.mark.parametrize('source_type', ['registry_record', 'literature_record', 'openfda_record', 'journal_article'])
def test_statement_citing_a_retrievable_source_reads(tmp_path: Path, source_type: str) -> None:
    # Control, on both heads: the four types `_locate_statement_source` resolves against the frozen cache.
    rows, _ = E.read_statements(_statements_csv(tmp_path / 'documentary_statements.csv', source_type))
    assert [r['source_type'] for r in rows] == [source_type]
