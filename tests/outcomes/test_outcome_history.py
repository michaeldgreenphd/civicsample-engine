"""Offline unit tests for `outcomes/outcome_history.py`: protocol-vs-results primary outcome
fidelity diffs and optional frozen historical study-version snapshot diffs."""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import outcome_history as OH  # noqa: E402


def _make_study(
    nct_id: str = 'NCT99000101',
    *,
    has_results: bool = True,
    primary_outcomes: list[dict[str, str]] | None = None,
    secondary_outcomes: list[dict[str, str]] | None = None,
    results_measures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    study: dict[str, Any] = {
        'protocolSection': {
            'identificationModule': {'nctId': nct_id, 'briefTitle': 'Fictional outcome fidelity study'},
            'outcomesModule': {
                'primaryOutcomes': primary_outcomes if primary_outcomes is not None else [
                    {'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 24'},
                ],
                'secondaryOutcomes': secondary_outcomes if secondary_outcomes is not None else [
                    {'measure': 'Change from Baseline in Body Weight', 'timeFrame': 'Week 24'},
                ],
            },
        },
        'hasResults': has_results,
    }
    if has_results:
        study['resultsSection'] = {
            'outcomeMeasuresModule': {
                'outcomeMeasures': results_measures if results_measures is not None else [
                    {
                        'type': 'PRIMARY',
                        'title': 'Change from Baseline in HbA1c',
                        'timeFrame': 'Week 24',
                    },
                ],
            },
        }
    return study


def _write_history_archive(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    with_meta: bool = True,
) -> Path:
    raw = ''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows).encode('utf-8')
    path.write_bytes(gzip.compress(raw, mtime=0))
    if with_meta:
        meta = {
            'sha256_uncompressed': hashlib.sha256(raw).hexdigest(),
            'n_records': len(rows),
        }
        path.with_name(path.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n', encoding='utf-8')
    return path


# ---------------------------------------------------------------- protocol vs results

def test_protocol_to_results_covers_all_five_states() -> None:
    study = _make_study(
        primary_outcomes=[
            {'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 24'},
            {'measure': 'Fasting Plasma Glucose', 'timeFrame': 'Week 24'},
            {'measure': 'Change in HbA1c', 'timeFrame': 'Week 52'},
            {'measure': 'All-Cause Mortality', 'timeFrame': 'Up to Week 52'},
        ],
        results_measures=[
            # 0: SECONDARY should be ignored by primary comparison
            {'type': 'SECONDARY', 'title': 'Hypoglycemia Events', 'timeFrame': 'Week 24'},
            # 1: verbatim match (with extra whitespace/case normalized)
            {'type': 'PRIMARY', 'title': '  change from baseline in   HbA1c ', 'timeFrame': 'week 24'},
            # 2: same measure title, differing timeframe
            {'type': 'PRIMARY', 'title': 'Fasting Plasma Glucose', 'timeFrame': 'Week 26'},
            # 3: reworded measure title sharing clinical construct HbA1c
            {
                'type': 'PRIMARY',
                'title': 'Mean Change from Baseline in Glycosylated Hemoglobin (HbA1c)',
                'timeFrame': 'Week 52',
            },
            # 4: unlisted primary measure in results
            {'type': 'PRIMARY', 'title': 'Seated Systolic Blood Pressure', 'timeFrame': 'Week 24'},
        ],
    )
    res = OH.compare_protocol_to_results(study, 'posted')
    assert res['state'] == 'evaluated'
    assert res['has_discrepancy'] is True
    assert res['state_counts'] == {
        'verbatim_measure_and_timeframe_match': 1,
        'timeframe_differing': 1,
        'measure_title_differing': 1,
        'registered_primary_missing_in_results': 1,
        'results_primary_unlisted_in_protocol': 1,
    }
    by_state = {a['state']: a for a in res['alignments']}
    assert by_state['verbatim_measure_and_timeframe_match']['results_measure_index'] == 1
    assert by_state['timeframe_differing']['protocol_time_frame'] == 'Week 24'
    assert by_state['timeframe_differing']['results_time_frame'] == 'Week 26'
    assert by_state['measure_title_differing']['protocol_measure'] == 'Change in HbA1c'
    assert by_state['registered_primary_missing_in_results']['protocol_measure'] == 'All-Cause Mortality'
    assert by_state['results_primary_unlisted_in_protocol']['results_title'] == 'Seated Systolic Blood Pressure'


def test_distinct_clinical_constructs_never_pair_solely_on_framing_words_or_opposing_poles() -> None:
    # "Change from Baseline in ..." shared framing words must NOT pair SBP with Body Weight,
    # and opposing clinical poles (Overall Survival vs Progression-Free Survival; Systolic vs
    # Diastolic Blood Pressure) must NOT pair as mere rewordings.
    for reg_title, res_title in (
        ('Change from Baseline in Systolic Blood Pressure', 'Change from Baseline in Body Weight'),
        ('Overall Survival', 'Progression-Free Survival'),
        ('Seated Systolic Blood Pressure', 'Seated Diastolic Blood Pressure'),
    ):
        related, _ = OH.titles_share_construct(reg_title, res_title)
        assert related is False, (reg_title, res_title)
        study = _make_study(
            primary_outcomes=[{'measure': reg_title, 'timeFrame': 'Week 24'}],
            results_measures=[{'type': 'PRIMARY', 'title': res_title, 'timeFrame': 'Week 24'}],
        )
        out = OH.compare_protocol_to_results(study, 'posted')
        assert out['state_counts']['measure_title_differing'] == 0
        assert out['state_counts']['registered_primary_missing_in_results'] == 1
        assert out['state_counts']['results_primary_unlisted_in_protocol'] == 1


def test_no_posted_results_and_not_retrieved_never_report_missing_primary() -> None:
    no_res = _make_study(has_results=False)
    r1 = OH.inspect_outcome_history(no_res, 'no_posted_results', history_snapshots=None)
    assert r1['protocol_to_results'] == {
        'state': 'not_applicable_no_posted_results',
        'n_registered_primary': 1,
        'n_results_primary': None,
        'alignments': None,
        'state_counts': None,
        'has_discrepancy': None,
    }
    assert r1['version_history_state'] == 'history_archive_not_provided'
    assert r1['version_history']['diffs'] is None

    r2 = OH.inspect_outcome_history(None, 'not_retrieved', history_snapshots=None)
    assert r2['protocol_to_results']['state'] == 'not_retrieved'
    assert r2['protocol_to_results']['alignments'] is None
    assert r2['version_history_state'] == 'history_archive_not_provided'


# ---------------------------------------------------------------- historical snapshots

def test_without_history_archive_cleanly_reports_history_archive_not_provided() -> None:
    study = _make_study()
    rec = OH.inspect_outcome_history(study, 'posted', history_snapshots=None)
    assert rec['version_history_state'] == 'history_archive_not_provided'
    assert rec['version_history'] == {
        'version_history_state': 'history_archive_not_provided',
        'snapshot_kind': None,
        'historical_version_date': None,
        'n_historical_primary': None,
        'n_current_primary': None,
        'overall_change_state': None,
        'diffs': None,
        'state_counts': None,
    }
    # Protocol-to-results still runs cleanly without historical versions!
    assert rec['protocol_to_results']['state'] == 'evaluated'
    assert rec['protocol_to_results']['state_counts']['verbatim_measure_and_timeframe_match'] == 1


def test_historical_snapshot_covers_all_seven_diff_states(tmp_path: Path) -> None:
    hist_path = _write_history_archive(
        tmp_path / 'history.jsonl.gz',
        [
            {
                'nct_id': 'NCT99000101',
                'snapshot_kind': 'initial_registration',
                'version_date': '2019-02-10',
                'primaryOutcomes': [
                    {'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 24'},         # unchanged
                    {'measure': 'Fasting Plasma Glucose', 'timeFrame': 'Week 12'},                # timeframe_changed -> Week 24
                    {'measure': 'Time to First MACE', 'timeFrame': 'Up to Month 36'},             # primary_demoted_to_secondary
                    {'measure': 'Change in ACR20 Responder Rate', 'timeFrame': 'Week 24'},        # measure_reworded
                    {'measure': 'All-Cause Hospitalization', 'timeFrame': 'Week 52'},             # primary_dropped
                ],
                'secondaryOutcomes': [
                    {'measure': 'Change from Baseline in Body Weight', 'timeFrame': 'Week 24'},   # secondary_promoted_to_primary
                    {'measure': 'EQ-5D Visual Analogue Scale', 'timeFrame': 'Week 24'},           # stays secondary
                ],
            }
        ],
    )
    snapshots, meta = OH.load_history_archive(hist_path)
    assert meta['provided'] is True and meta['n_records'] == 1 and meta['meta_verified'] is True

    current_study = _make_study(
        'NCT99000101',
        primary_outcomes=[
            {'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 24'},
            {'measure': 'Fasting Plasma Glucose', 'timeFrame': 'Week 24'},
            {'measure': 'Change from Baseline in Body Weight', 'timeFrame': 'Week 24'},
            {'measure': 'Proportion of Participants Achieving ACR20 Responder Rate', 'timeFrame': 'Week 24'},
            {'measure': 'Seated Systolic Blood Pressure', 'timeFrame': 'Week 24'},               # primary_added
        ],
        secondary_outcomes=[
            {'measure': 'Time to First MACE', 'timeFrame': 'Up to Month 36'},
            {'measure': 'EQ-5D Visual Analogue Scale', 'timeFrame': 'Week 24'},
        ],
    )
    out = OH.inspect_outcome_history(current_study, 'posted', history_snapshots=snapshots)
    assert out['version_history_state'] == 'evaluated'
    vh = out['version_history']
    assert vh['overall_change_state'] == 'changed'
    assert vh['historical_version_date'] == '2019-02-10'
    assert vh['state_counts'] == {
        'unchanged': 1,
        'primary_added': 1,
        'primary_dropped': 1,
        'primary_demoted_to_secondary': 1,
        'secondary_promoted_to_primary': 1,
        'timeframe_changed': 1,
        'measure_reworded': 1,
    }

    # A trial absent from a provided archive gets trial_not_in_history_archive, never unchanged.
    other = _make_study('NCT99000999')
    out_other = OH.inspect_outcome_history(other, 'posted', history_snapshots=snapshots)
    assert out_other['version_history_state'] == 'trial_not_in_history_archive'
    assert out_other['version_history']['diffs'] is None


def test_history_archive_supports_nested_protocol_section_and_refuses_tampering(tmp_path: Path) -> None:
    # Nested protocolSection.outcomesModule shape
    p_nested = _write_history_archive(
        tmp_path / 'nested.jsonl.gz',
        [
            {
                'protocolSection': {
                    'identificationModule': {'nctId': 'NCT99000202'},
                    'outcomesModule': {
                        'primaryOutcomes': [{'measure': 'Change in HbA1c', 'timeFrame': 'Week 24'}],
                    },
                },
                'version_date': '2020-01-01',
            }
        ],
        with_meta=False,
    )
    snaps, meta = OH.load_history_archive(p_nested)
    assert 'NCT99000202' in snaps and meta['meta_verified'] is False

    # Tampered bytes vs .meta.json sha256_uncompressed must raise HistoryArchiveError
    p_tampered = _write_history_archive(
        tmp_path / 'tampered.jsonl.gz',
        [{'nct_id': 'NCT99000202', 'primaryOutcomes': [{'measure': 'HbA1c', 'timeFrame': 'Week 24'}]}],
        with_meta=True,
    )
    tampered_raw = gzip.decompress(p_tampered.read_bytes()).replace(b'Week 24', b'Week 26')
    p_tampered.write_bytes(gzip.compress(tampered_raw, mtime=0))
    with pytest.raises(OH.HistoryArchiveError, match='sha256_uncompressed'):
        OH.load_history_archive(p_tampered)

    # Duplicate NCT IDs in archive must raise HistoryArchiveError
    p_dup = _write_history_archive(
        tmp_path / 'dup.jsonl.gz',
        [
            {'nct_id': 'NCT99000202', 'primaryOutcomes': []},
            {'nct_id': 'NCT99000202', 'primaryOutcomes': []},
        ],
        with_meta=False,
    )
    with pytest.raises(OH.HistoryArchiveError, match='duplicate'):
        OH.load_history_archive(p_dup)

    # Invalid NCT ID must raise HistoryArchiveError
    p_bad_id = _write_history_archive(
        tmp_path / 'bad_id.jsonl.gz',
        [{'nct_id': 'BAD123', 'primaryOutcomes': []}],
        with_meta=False,
    )
    with pytest.raises(OH.HistoryArchiveError, match='invalid nct_id'):
        OH.load_history_archive(p_bad_id)
