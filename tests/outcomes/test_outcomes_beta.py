"""Industry Outcomes beta: evidence rules, acquisition archive, attribution, demographics link,
artifact family and its contract. Fictional records only (tests/outcomes/fixture.py); no network."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture as F  # noqa: E402
from outcomes import acquire, artifacts, attribution, contract, evidence, link  # noqa: E402

sys.path.insert(0, str(ROOT / 'scripts'))
import build_outcomes_beta  # noqa: E402
import build_outcomes_fixture  # noqa: E402


def facts(**kw: Any) -> dict[str, Any]:
    return evidence.facts(F.study(1, **kw))


# ---------------------------------------------------------------- evidence rules

@pytest.mark.parametrize('kw, reasons', [
    ({}, []),
    ({'phases': ('PHASE2', 'PHASE3')}, ['PHASE2_PHASE3_STRATUM']),
    ({'phases': ('PHASE2',)}, ['NOT_PHASE3']),
    ({'sponsor_class': 'OTHER'}, ['LEAD_SPONSOR_NOT_INDUSTRY']),
    ({'pcd': '2020-01'}, []),                 # month precision, wholly inside
    ({'pcd': '2019-12'}, ['PRIMARY_COMPLETION_OUTSIDE_WINDOW']),
    ({'pcd': '2020'}, []),                    # year precision, wholly inside
    ({'pcd': 'not a date'}, ['PRIMARY_COMPLETION_OUTSIDE_WINDOW']),
])
def test_eligibility_reasons(kw: dict[str, Any], reasons: list[str]) -> None:
    assert evidence.eligibility(facts(**kw)) == reasons


def test_results_state_moves_with_the_cutoff_and_is_never_carried_forward() -> None:
    f = facts(has_results=False, pcd='2020-06-15')
    assert evidence.results_state(f, date(2021, 6, 14)) == ('PENDING', 'WITHIN_STANDARD_WINDOW')
    assert evidence.results_state(f, date(2021, 6, 15)) == ('UNREPORTED', 'PAST_STANDARD_WINDOW')


def test_month_precision_completion_counts_from_the_end_of_the_month() -> None:
    f = facts(has_results=False, pcd='2020-01')
    assert evidence.results_state(f, date(2021, 1, 30))[0] == 'PENDING'
    assert evidence.results_state(f, date(2021, 1, 31))[0] == 'UNREPORTED'


@pytest.mark.parametrize('kw, expected', [
    ({'has_results': True}, ('POSTED', None)),
    ({'has_results': False, 'status': 'WITHDRAWN', 'enrollment': 0}, ('NOT_APPLICABLE', 'WITHDRAWN_NO_PARTICIPANTS')),
    ({'has_results': False, 'status': 'TERMINATED', 'enrollment': 0}, ('NOT_APPLICABLE', 'TERMINATED_NO_DATA')),
    ({'has_results': False, 'status': 'TERMINATED', 'enrollment': 14, 'pcd_type': 'ESTIMATED'}, ('PENDING', 'STATUS_UNCERTAIN')),
    ({'has_results': False, 'status': 'UNKNOWN'}, ('PENDING', 'STATUS_UNCERTAIN')),
    ({'has_results': False, 'disp': '2021-05-01'}, ('UNREPORTED', 'PAST_STANDARD_WINDOW')),
])
def test_results_state_vocabulary(kw: dict[str, Any], expected: tuple[str, str | None]) -> None:
    got = evidence.results_state(facts(**kw), F.CUTOFF)
    assert got == expected
    assert got[1] in evidence.RESULTS_STATES[got[0]]


def test_certified_delay_extends_pending_two_years_from_certification() -> None:
    f = facts(has_results=False, disp='2021-05-01')
    assert evidence.results_state(f, date(2023, 4, 30)) == ('PENDING', 'CERTIFIED_DELAY')


def test_a_certification_filed_after_the_cutoff_does_not_apply_at_it() -> None:
    f = facts(has_results=False, pcd='2020-06-15', disp='2024-05-01')
    assert evidence.results_state(f, date(2022, 6, 15)) == ('UNREPORTED', 'PAST_STANDARD_WINDOW')
    assert evidence.results_state(f, date(2024, 5, 1)) == ('PENDING', 'CERTIFIED_DELAY')


def test_results_posted_after_a_historical_cutoff_were_not_yet_posted() -> None:
    f = facts(has_results=True)                      # results first posted 2021-05-01
    assert evidence.results_state(f, date(2021, 5, 1)) == ('POSTED', None)
    assert evidence.results_state(f, date(2021, 4, 30)) == ('PENDING', 'WITHIN_STANDARD_WINDOW')
    assert evidence.results_state(f, date(2020, 12, 31)) == ('PENDING', 'WITHIN_STANDARD_WINDOW')


def test_a_cutoff_after_retrieval_is_refused(tmp_path: Path) -> None:
    rules = tmp_path / 'r.csv'
    rules.write_text(F.RULES_CSV)
    studies = F.studies()
    elig = [evidence.facts(s) for s in studies if not evidence.eligibility(evidence.facts(s))]
    with pytest.raises(artifacts.BuildError, match='after the records were retrieved'):
        artifacts.build(studies, {'retrieved_at': '2026-11-09T12:00:00+00:00'}, date(2026, 11, 10), None, None,
                        attribution.attribute(elig, rules), ({}, {}), {'commit': 'c'})


def test_document_flags_are_availability_only() -> None:
    assert evidence.document_flags(facts()) == {'protocol': True, 'sap': True}
    assert evidence.document_flags(facts(docs=False)) == {'protocol': False, 'sap': False}


# ---------------------------------------------------------------- acquisition

class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.body


def test_fetch_follows_page_tokens_and_asks_only_for_the_needed_sections() -> None:
    pages = [{'studies': F.studies()[:3], 'nextPageToken': 't2'}, {'studies': F.studies()[3:]}]
    seen: list[dict[str, Any]] = []

    def get(url: str, params: dict[str, Any]) -> FakeResponse:
        seen.append(dict(params))
        return FakeResponse(pages[len(seen) - 1])
    out = acquire.fetch(evidence.FRAME_QUERY, get=get, delay=0)
    assert len(out) == len(F.studies())
    assert seen[1]['pageToken'] == 't2' and 'pageToken' not in seen[0]
    assert seen[0]['fields'] == 'protocolSection,documentSection,hasResults'


def test_archive_round_trip_is_byte_stable_and_refuses_tampering(tmp_path: Path) -> None:
    p1, p2 = tmp_path / 'a.jsonl.gz', tmp_path / 'b.jsonl.gz'
    m1 = acquire.write_archive(F.studies(), p1, 'q')
    m2 = acquire.write_archive(list(reversed(F.studies())), p2, 'q')
    assert p1.read_bytes() == p2.read_bytes() and m1['sha256_uncompressed'] == m2['sha256_uncompressed']
    studies, meta = acquire.read_archive(p1, 'q')
    assert len(studies) == meta['n_records'] == len(F.studies())
    raw = gzip.decompress(p1.read_bytes()).replace(b'Fictional trial 1 ', b'Fictional trial X ')
    p1.write_bytes(gzip.compress(raw))
    with pytest.raises(ValueError, match='sha256'):
        acquire.read_archive(p1, 'q')


def test_an_archive_for_another_query_is_refused(tmp_path: Path) -> None:
    p = tmp_path / 'a.jsonl.gz'
    acquire.write_archive(F.studies(), p, 'AREA[Phase]PHASE2')
    with pytest.raises(ValueError, match='another query'):
        acquire.read_archive(p, evidence.FRAME_QUERY)


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(ValueError):
        acquire.encode([F.study(1), F.study(1)])


# ---------------------------------------------------------------- attribution and link

def test_attribution_keeps_review_states_and_shows_unreviewed_literals_verbatim(tmp_path: Path) -> None:
    rules = tmp_path / 'rules.csv'
    rules.write_text(F.RULES_CSV)
    fs = [evidence.facts(F.study(1)), evidence.facts(F.study(2, sponsor='Other Fictional Bio'))]
    out, summary = attribution.attribute(fs, rules)
    assert out['NCT99000001']['review_state'] == 'attributed' and out['NCT99000001']['companies'] == ['Fictional Pharma']
    assert out['NCT99000002']['review_state'] == 'unreviewed' and out['NCT99000002']['companies'] == []
    assert attribution.display_name(out['NCT99000002']) == 'Other Fictional Bio'
    assert summary['lead_review_states'] == {'attributed': 1, 'reviewed_excluded': 0, 'unreviewed': 1}
    assert summary['rules_sha256'] == hashlib.sha256(rules.read_bytes()).hexdigest()
    # the Industry recode is computed alongside, not replaced
    assert out['NCT99000002']['industry_recode_company'] == 'Other Fictional Bio'


def write_site(tmp_path: Path, records: list[dict[str, Any]], total_parts: int = 1, stamp: str = 'x') -> Path:
    data = tmp_path / 'site' / 'data'
    data.mkdir(parents=True)
    (data / 'run.json').write_text(json.dumps({'extracted_at': stamp, 'snapshot_date': '2026-11-08',
                                               'pipeline_commit': 'p', 'total_parts': total_parts}))
    (data / 'demographics.part1.json.gz').write_bytes(gzip.compress(json.dumps(
        {'extracted_at': stamp, 'pipeline_commit': 'p', 'part': 1, 'total_parts': total_parts, 'data': records}).encode()))
    return tmp_path / 'site'


def test_link_reads_contract_fields_and_reports_absence_with_a_reason(tmp_path: Path) -> None:
    rec = {'nct_id': 'NCT99000001', 'sex': {'reported': True, 'totals': {'female': 10, 'male': 12}},
           'race': {'reported': False}, 'ethnicity': {'reported': False}, 'study_type': 'INTERVENTIONAL',
           'status': 'COMPLETED', 'primary_completion_date': '2020-06-15', 'sponsor_class': 'INDUSTRY',
           'lead_sponsor_name': 'Fictional Pharma', 'collaborators': [], 'results_date': '2021-05-01',
           'enrollment': 22, 'phase': 'PHASE3', 'primary_condition': 'Cardiovascular', 'secondary_condition': '',
           'countries': []}
    site = write_site(tmp_path, [rec])
    out, summary = link.link(['NCT99000001', 'NCT99000002'], site)
    assert out['NCT99000001'] == {'in_dataset': True, 'sex_reported': True, 'race_reported': False,
                                  'ethnicity_reported': False, 'in_industry_demographics_cohort': True}
    assert out['NCT99000002']['in_dataset'] is False
    assert summary['state'] == 'COMPUTED' and summary['n_in_dataset'] == 1
    missing, s2 = link.link(['NCT99000001'], None)
    assert missing == {'NCT99000001': None} and s2['state'] == 'NOT_COMPUTED'
    broken = write_site(tmp_path / 'b', [rec], total_parts=2)
    _, s3 = link.link(['NCT99000001'], broken)
    assert s3['state'] == 'NOT_COMPUTED' and 'parts' in s3['reason']
    split = write_site(tmp_path / 's', [rec])
    part = split / 'data' / 'demographics.part1.json.gz'
    doc = json.loads(gzip.decompress(part.read_bytes()))
    doc['layout'] = {'version': 1}
    part.write_bytes(gzip.compress(json.dumps(doc).encode()))
    out4, s4 = link.link(['NCT99000001'], split)
    assert s4['state'] == 'NOT_COMPUTED' and 'split' in s4['reason'] and out4 == {'NCT99000001': None}
    corrupt = write_site(tmp_path / 'c', [rec])
    (corrupt / 'data' / 'demographics.part1.json.gz').write_bytes(b'not gzip')
    _, s5 = link.link(['NCT99000001'], corrupt)
    assert s5['state'] == 'NOT_COMPUTED' and 'could not be read' in s5['reason']
    mixed = write_site(tmp_path / 'm', [rec])
    run = json.loads((mixed / 'data' / 'run.json').read_text())
    run['pipeline_commit'] = 'other'
    (mixed / 'data' / 'run.json').write_text(json.dumps(run))
    _, s6 = link.link(['NCT99000001'], mixed)
    assert s6['state'] == 'NOT_COMPUTED' and 'another run' in s6['reason']


# ---------------------------------------------------------------- artifacts and contract

@pytest.fixture(scope='module')
def preview() -> dict[str, Any]:
    return build_outcomes_fixture.family('provisional_preview')


@pytest.fixture(scope='module')
def public() -> dict[str, Any]:
    return build_outcomes_fixture.family('evidence_only')


def test_cohort_counts_and_exclusions(public: dict[str, Any]) -> None:
    run = public['run.json']
    assert (run['cohort']['n_frame'], run['cohort']['n_eligible'], run['cohort']['n_excluded']) == (8, 6, 2)
    assert run['cohort']['exclusions']['PHASE2_PHASE3_STRATUM'] == 1
    assert run['cohort']['exclusions']['PRIMARY_COMPLETION_OUTSIDE_WINDOW'] == 1
    assert run['collections']['audit']['n'] == 3 and run['collections']['audit']['n_including_excluded'] == 4


def test_evidence_only_carries_human_reviewed_assessments_only(public: dict[str, Any]) -> None:
    rows = public['index.json']['rows']
    assert {r['nct_id']: r['assessment_kind'] for r in rows if r['assessment_kind']} == {'NCT99000001': 'human_reviewed'}
    assert 'machine_provisional' not in public['summary.json']['assessment_states']['by_kind']
    assert contract.check(public, publishable=True) == []


def test_preview_is_labelled_and_never_publishable(preview: dict[str, Any]) -> None:
    kinds = {r['nct_id']: r['assessment_kind'] for r in preview['index.json']['rows'] if r['assessment_kind']}
    assert kinds == {'NCT99000001': 'human_reviewed', 'NCT99000002': 'machine_provisional', 'NCT99000007': 'machine_provisional'}
    assert contract.check(preview) == []
    assert contract.check(preview, publishable=True) == ['a publishable build must be evidence_only; this is a provisional preview']


def test_summary_keeps_every_state_and_explicit_denominators(preview: dict[str, Any]) -> None:
    s = preview['summary.json']
    for counts in s['assessment_states']['by_kind'].values():
        assert set(counts) == set(artifacts.TRIAL_STATES)
    m = {x['id']: x for x in s['metrics']}
    assert (m['results_posted']['numerator'], m['results_posted']['denominator']) == (2, 'eligible')
    assert m['no_results_past_window']['numerator'] == 2
    assert 'not a finding of non-compliance' in m['no_results_past_window']['definition'].lower()
    assert 'usable rule' in m['protocol_posted']['definition']
    assert (m['human_reviewed']['numerator'], m['human_reviewed']['denominator']) == (1, 'audit_eligible')


def test_record_and_index_agree_and_dates_are_independent(preview: dict[str, Any]) -> None:
    rec = preview['records/r01.json']['records']['NCT99000001']
    assert rec['results'] == {'state': 'POSTED', 'sub_reason': None, 'as_of': '2026-11-09'}
    assert rec['assessment']['review']['status'] == 'human_reviewed'
    assert preview['run.json']['dates'] == {'outcomes_cutoff': '2026-11-09', 'assessment_cutoff': '2026-11-09',
                                            'demographics_snapshot': '2026-11-08'}


def test_contract_catches_tampering_leaks_and_cross_file_drift(public: dict[str, Any]) -> None:
    bad = copy.deepcopy(public)
    bad['index.json']['rows'][0]['results_state'] = 'POSTED_LATER'
    assert any('sha256' in p for p in contract.check(bad))
    bad = copy.deepcopy(public)
    bad['records/r01.json']['records']['NCT99000001']['assessment']['review']['reviewer'] = 'Someone'
    assert any('private keys' in p for p in contract.check(bad))
    bad = copy.deepcopy(public)
    bad['summary.json']['assessment_states']['by_kind']['human_reviewed'].pop('CONFLICTING')
    assert any('eight trial states' in p for p in contract.check(bad))
    bad = copy.deepcopy(public)
    del bad['records/r07.json']
    assert any('NCT99000007' in p or 'r07' in p for p in contract.check(bad))


def test_export_is_checked_before_use() -> None:
    exp = F.export('evidence_only')
    exp['assessments'][0]['reviewer'] = 'Someone'
    with pytest.raises(artifacts.BuildError, match='non-allowlisted'):
        artifacts.read_export(json.dumps(exp).encode())
    exp = F.export('provisional_preview')
    exp['display_mode'] = 'evidence_only'
    with pytest.raises(artifacts.BuildError, match='machine assessment'):
        artifacts.read_export(json.dumps(exp).encode())


@pytest.mark.parametrize('where', ['reported', 'decision_rule', 'evidence', 'prespecification', 'human_evidence'])
def test_private_keys_nested_anywhere_in_the_export_are_refused(where: str) -> None:
    exp = F.export('evidence_only')
    a = exp['assessments'][0]
    target = {'reported': a['units'][0]['reported'], 'decision_rule': a['units'][0]['decision_rule'],
              'evidence': a['units'][0]['evidence'][0], 'prespecification': a['prespecification'],
              'human_evidence': a['human_evidence'][0]}[where]
    target['reviewer_note'] = 'private'
    with pytest.raises(artifacts.BuildError, match='non-allowlisted'):
        artifacts.read_export(json.dumps(exp).encode())


def test_a_nested_object_where_a_scalar_belongs_is_refused() -> None:
    exp = F.export('evidence_only')
    exp['assessments'][0]['units'][0]['rationale'] = {'raw_model_response': 'x'}
    with pytest.raises(artifacts.BuildError, match='not a scalar'):
        artifacts.read_export(json.dumps(exp).encode())


def test_duplicate_assessments_are_refused() -> None:
    exp = F.export('evidence_only')
    exp['assessments'].append(copy.deepcopy(exp['assessments'][0]))
    with pytest.raises(artifacts.BuildError, match='more than one assessment'):
        artifacts.read_export(json.dumps(exp).encode())


def test_contract_requires_the_assessment_kinds_of_the_mode(public: dict[str, Any], preview: dict[str, Any]) -> None:
    bad = copy.deepcopy(public)
    bad['summary.json']['assessment_states']['by_kind'] = {}
    assert any('must hold exactly' in p for p in contract.check(bad))
    bad = copy.deepcopy(preview)
    del bad['summary.json']['assessment_states']['by_kind']['machine_provisional']
    assert any('must hold exactly' in p for p in contract.check(bad))


def test_write_swaps_the_family_in_whole_and_keeps_the_old_one_on_failure(tmp_path: Path, public: dict[str, Any],
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / 'outcomes'
    artifacts.write(public, out)
    before = contract.load_dir(out)
    stale = out / 'records' / 'r05.json'
    stale.write_text('{}')                        # a file from an older family must not survive a write
    artifacts.write(public, out)
    assert not stale.exists() and contract.load_dir(out) == before
    real_dump = artifacts.dump
    calls = {'n': 0}

    def failing(obj: Any) -> bytes:
        calls['n'] += 1
        if calls['n'] == 3:
            raise OSError('disk full')
        return real_dump(obj)
    monkeypatch.setattr(artifacts, 'dump', failing)
    with pytest.raises(OSError):
        artifacts.write(public, out)
    assert contract.load_dir(out) == before and not (tmp_path / 'outcomes.previous').exists()


def test_eligibility_drift_against_the_export_refuses_the_build(tmp_path: Path) -> None:
    exp = F.export('evidence_only')
    exp['collections']['audit']['eligible_nct_ids'].append('NCT99000005')   # the export thinks the 2/3 trial is eligible
    raw = json.dumps(exp).encode()
    rules = tmp_path / 'r.csv'
    rules.write_text(F.RULES_CSV)
    studies = F.studies()
    elig = [evidence.facts(s) for s in studies if not evidence.eligibility(evidence.facts(s))]
    with pytest.raises(artifacts.BuildError, match='eligibility disagrees'):
        artifacts.build(studies, {}, F.CUTOFF, artifacts.read_export(raw), 'x', attribution.attribute(elig, rules),
                        ({}, {}), {'commit': 'c'})


def test_cli_builds_from_an_archive_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = tmp_path / 'frame.jsonl.gz'
    acquire.write_archive(F.studies(), arch, evidence.FRAME_QUERY)
    exp = tmp_path / 'export.json'
    exp.write_text(json.dumps(F.export('evidence_only')))
    rules = tmp_path / 'rules.csv'
    rules.write_text(F.RULES_CSV)
    monkeypatch.setattr(attribution, 'RULES_PATH', rules)
    monkeypatch.setattr(attribution.attribute, '__defaults__', (rules,))
    monkeypatch.setattr(acquire, 'fetch', lambda *a, **k: pytest.fail('network used'))
    out = tmp_path / 'out'
    assert build_outcomes_beta.main(['--archive', str(arch), '--assessments', str(exp), '--out', str(out),
                                     '--cutoff', '2026-11-09']) == 0
    assert contract.check(contract.load_dir(out), publishable=True) == []
    assert json.loads((out / 'run.json').read_text())['demographics_link']['state'] == 'NOT_COMPUTED'
    # an uncomputed link publishes no demographics count, rather than a measured zero
    metrics = {m['id'] for m in json.loads((out / 'summary.json').read_text())['metrics']}
    assert 'demographics_linked' not in metrics and 'results_posted' in metrics


def test_committed_fixtures_are_current() -> None:
    """tests/fixtures/outcomes/ is what the site's reader tests copy; regenerate with
    scripts/build_outcomes_fixture.py when the contract changes."""
    for mode in artifacts.DISPLAY_MODES:
        fam = build_outcomes_fixture.family(mode)
        on_disk = contract.load_dir(ROOT / 'tests' / 'fixtures' / 'outcomes' / mode)
        assert on_disk == json.loads(json.dumps(fam)), f'{mode} fixture is stale'
