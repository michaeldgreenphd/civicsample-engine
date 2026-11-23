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
import shutil
import sys
import urllib.parse
from datetime import date
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


def test_resumable_log_truncates_interrupted_line_and_reuses_settled_answers(tmp_path: Path) -> None:
    calls: list[str] = []
    get = F.stub_get(calls)
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    log_path = A.raw_log_path(tmp_path / 'cache', 'ctgov')

    log1 = A.ResponseLog(log_path)
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990008'], log1, get, throttle, day='2026-11-01')
    first_count = len(calls)
    assert first_count == 3  # version + 2 studies (200 and 404 are both final)

    # Simulate an interrupted process appending a truncated JSON line without '\n'
    with log_path.open('ab') as fh:
        fh.write(b'{"key":"ctgov/study/NCT09990002","partial":')

    log2 = A.ResponseLog(log_path)
    assert 'ctgov/study/NCT09990002' not in log2.records
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002', 'NCT09990008'], log2, get, throttle, day='2026-11-01')
    # Only NCT09990002 is fetched on resume; version, NCT09990001 (200), and NCT09990008 (404) are reused
    assert len(calls) == first_count + 1


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
    assert t2['submission_infos']['absent_reason'] == 'submissionInfos_absent_in_statusModule'


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
    assert set(observed.values()) == set(T.WINDOW_STATES_365D)
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
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-02', 'confirmed_same_trial_results', 'Reports primary trial results'])
        w.writerow([res_02['link_id'], 'NCT09990002', 'Dr. Fictional', '2026-11-02', 'confirmed_background_only', 'Prior phase 1 background paper'])
        w.writerow([fda_cand_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-02', 'rejected_false_positive', 'Different formulation NDA'])
        w.writerow([stmt_link['link_id'], 'NCT09990001', 'Dr. Fictional', '2026-11-02', 'confirmed_same_trial_results', 'Confirmed statement'])
        w.writerow(['link:0000000000000000', 'NCT09990001', 'Dr. Fictional', '2026-11-02', 'indeterminate', 'Old link whose text changed'])

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
        w.writerow([ctgov_04['link_id'], 'NCT09990004', 'Dr. Reviewer', '2026-11-02', 'indeterminate', 'Pending QC verification'])
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Reviewer', '2026-11-02', 'rejected_false_positive', 'Mentions unrelated trial'])

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
    res1 = A.fetch_ctgov_studies(['NCT09990001'], ct_log, flaky_get, throttle, day='2026-11-01')
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
    res2 = A.fetch_ctgov_studies(['NCT09990001'], ct_log, flaky_get, throttle, day='2026-11-01')
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
        as_of=date(2026, 11, 2),
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
        '--generated-at', F.GENERATED_AT,
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
    app = {
        'application_number': 'NDA777003',
        'sponsor_name': 'Fictional Trial Sponsor Inc',
        'note': 'Pivotal study NCT09990009',
        'submissions': [{'submission_type': 'ORIG', 'submission_status': 'TA', 'submission_status_date': '20220301'}],
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

    # The application gains an approval: the excerpt and locator stay the same, the action date changes.
    approved = {**app, 'submissions': [*app['submissions'], {
        'submission_type': 'ORIG', 'submission_status': 'AP', 'submission_status_date': '20230115',
    }]}
    after = links_for(approved, '2026-11-08')
    assert after[0]['verbatim_excerpt'] == before[0]['verbatim_excerpt']
    assert after[0]['locator'] == before[0]['locator']
    assert (before[0]['publication_or_action_date'], after[0]['publication_or_action_date']) == ('2022-03-01', '2023-01-15')
    assert after[0]['link_id'] != before[0]['link_id']

    # A review keyed on the old identity is reported as orphaned, never carried onto the changed record.
    review = {before[0]['link_id']: {
        'link_id': before[0]['link_id'], 'nct_id': 'NCT09990009', 'reviewer': 'R',
        'reviewed_at': '2026-11-02', 'decision': 'confirmed_same_trial_results', 'notes': 'n',
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
            w.writerow([lnk['link_id'], lnk['nct_id'], 'Dr. Fictional', '2026-11-02',
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
    with pytest.raises(ValueError, match='fetched_at must be an ISO 8601 timestamp'):
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

    # Without --as-of the 365-day window is evaluated at the stated data timestamp, not a fixed date.
    assert cli_build.main(['build', '--cache', str(cache), '--out', str(out), '--generated-at', F.GENERATED_AT]) == 0
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
        w.writerow([res_02['link_id'], 'NCT09990002', 'Dr. Fictional', '2026-11-02', 'confirmed_same_trial_results', 'in scope'])
        w.writerow([epmc_06['link_id'], 'NCT09990006', 'Dr. Fictional', '2026-11-02', 'confirmed_same_trial_results', 'excluded trial'])
        w.writerow(['link:0000000000000000', 'NCT09990002', 'Dr. Fictional', '2026-11-02', 'indeterminate', 'changed evidence'])
        w.writerow(['link:1111111111111111', 'NCT09999999', 'Dr. Fictional', '2026-11-02', 'indeterminate', 'trial not in cache'])

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
        ('Tiotropium 18\u00b5g once daily', 'tiotropium once daily'),
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
    assert frozen_cache.ctgov['ctgov/version/2026-11-01']['body']['dataTimestamp'] == '2026-11-01T08:00:00'
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
        w.writerow([ct_links['NCT09990001']['link_id'], 'NCT09990001', 'Dr. Fictional', '2026-11-02',
                    'rejected_false_positive', 'disputes the posted results'])
        w.writerow([ct_links['NCT09990004']['link_id'], 'NCT09990004', 'Dr. Fictional', '2026-11-02',
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
    assert [v['key'] for v in ct['versions']] == ['ctgov/version/2026-11-01']
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
    A.fetch_ctgov_studies(['NCT09990001'], log, F.stub_get(), throttle, day='2026-11-01')
    A.fetch_ctgov_studies(['NCT09990001', 'NCT09990002'], log, refreshed_get, throttle, day='2026-11-02')
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

    stated = ART.build_artifacts(resumed, **kwargs, as_of=date(2026, 11, 2))
    assert ART.check_contract(stated) == []
    ct2 = stated['run.json']['sources']['ctgov']
    assert [(v['key'], v['data_timestamp']) for v in ct2['versions']] == [
        ('ctgov/version/2026-11-01', '2026-11-01T08:00:00'),
        ('ctgov/version/2026-11-02', '2026-11-02T08:00:00'),
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
