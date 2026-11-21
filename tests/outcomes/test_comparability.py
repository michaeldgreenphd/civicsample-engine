"""Offline pytest suite for `outcomes/comparability.py`, `outcomes/comparability_build.py`,
`scripts/build_outcomes_comparability.py`, `scripts/check_outcomes_comparability_contract.py`, and
`scripts/export_outcomes_comparability_site.py`.

All tests run strictly offline on synthetic fictional CT.gov API v2 study records (`NCT9900xxxx`)
and verify Python 3.11 typing/zero-RNG compliance.
"""
from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / 'scripts'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from outcomes import acquire  # noqa: E402
from outcomes import comparability as C  # noqa: E402
from outcomes import comparability_build as B  # noqa: E402

import build_outcomes_comparability  # noqa: E402
import check_outcomes_comparability_contract  # noqa: E402
import export_outcomes_comparability_site  # noqa: E402


# ---------------------------------------------------------------- fictional CT.gov v2 builders

def _groups(titles: tuple[str, ...], prefix: str, descriptions: tuple[str | None, ...] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, t in enumerate(titles):
        g: dict[str, Any] = {'id': f'{prefix}{i:03d}', 'title': t}
        if descriptions and descriptions[i] is not None:
            g['description'] = descriptions[i]
        out.append(g)
    return out


def _counts(vals: tuple[str, ...], prefix: str) -> list[dict[str, str]]:
    return [{'groupId': f'{prefix}{i:03d}', 'value': v} for i, v in enumerate(vals)]


def make_flow(
    titles: tuple[str, ...] = ('Zorbatinib 10 mg', 'Placebo'),
    started: tuple[str, ...] = ('100', '100'),
    completed: tuple[str, ...] = ('92', '85'),
    *,
    type_units_analyzed: str | None = None,
    use_num_units_only: bool = False,
) -> dict[str, Any]:
    ach_started = [
        ({'groupId': f'FG{i:03d}', 'numUnits': v} if use_num_units_only else {'groupId': f'FG{i:03d}', 'numSubjects': v})
        for i, v in enumerate(started)
    ]
    ach_completed = [
        ({'groupId': f'FG{i:03d}', 'numUnits': v} if use_num_units_only else {'groupId': f'FG{i:03d}', 'numSubjects': v})
        for i, v in enumerate(completed)
    ]
    mod: dict[str, Any] = {
        'groups': _groups(titles, 'FG'),
        'periods': [
            {
                'title': 'Period 1 (Double-Blind)',
                'milestones': [
                    {'type': 'STARTED', 'achievements': ach_started},
                    {'type': 'COMPLETED', 'achievements': ach_completed},
                ],
            }
        ],
    }
    if type_units_analyzed is not None:
        mod['typeUnitsAnalyzed'] = type_units_analyzed
    return mod


def make_baseline(
    titles: tuple[str, ...] = ('Zorbatinib 10 mg', 'Placebo', 'Total'),
    counts: tuple[str, ...] = ('99', '98', '197'),
    *,
    units: str = 'Participants',
    population: str | None = 'Full Analysis Set: all randomized participants receiving >= 1 dose.',
    descriptions: tuple[str | None, ...] | None = None,
) -> dict[str, Any]:
    mod: dict[str, Any] = {
        'groups': _groups(titles, 'BG', descriptions),
        'denoms': [{'units': units, 'counts': _counts(counts, 'BG')}],
    }
    if population is not None:
        mod['populationDescription'] = population
    return mod


def make_primary_measure(
    title: str = 'Change from Baseline in HbA1c',
    *,
    time_frame: str = 'Week 24',
    og_titles: tuple[str, ...] = ('Zorbatinib 10 mg', 'Placebo'),
    og_descriptions: tuple[str | None, ...] | None = None,
    og_counts: tuple[str, ...] = ('95', '85'),
    denom_units: str = 'Participants',
    population_description: str | None = None,
    description: str | None = None,
    classes: list[dict[str, Any]] | None = None,
    analyses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        'type': 'PRIMARY',
        'title': title,
        'timeFrame': time_frame,
        'unitOfMeasure': 'percentage of glycosylated hemoglobin',
        'paramType': 'LEAST_SQUARES_MEAN',
        'dispersionType': 'Standard Error',
        'groups': _groups(og_titles, 'OG', og_descriptions),
        'denoms': [{'units': denom_units, 'counts': _counts(og_counts, 'OG')}],
        'classes': classes if classes is not None else [
            {
                'categories': [
                    {
                        'measurements': [
                            {'groupId': f'OG{i:03d}', 'value': '-0.9' if i == 0 else '-0.2'}
                            for i in range(len(og_titles))
                        ]
                    }
                ]
            }
        ],
    }
    if population_description is not None:
        m['populationDescription'] = population_description
    if description is not None:
        m['description'] = description
    if analyses is not None:
        m['analyses'] = analyses
    return m


def make_study(
    n: int = 1,
    *,
    has_results: bool = True,
    enrollment_count: int | None = 200,
    enrollment_type: str = 'ACTUAL',
    flow: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
    measures: list[dict[str, Any]] | None = None,
    primary_outcomes: list[dict[str, str]] | None = None,
    docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nct = f'NCT9900{n:04d}'
    s: dict[str, Any] = {
        'protocolSection': {
            'identificationModule': {'nctId': nct, 'briefTitle': f'Fictional HbA1c trial {n}'},
            'statusModule': {'lastUpdatePostDateStruct': {'date': '2026-11-10'}},
            'designModule': {
                'studyType': 'INTERVENTIONAL',
                'phases': ['PHASE3'],
                'designInfo': {'allocation': 'RANDOMIZED', 'interventionModel': 'PARALLEL'},
            },
            'outcomesModule': {
                'primaryOutcomes': primary_outcomes if primary_outcomes is not None else [
                    {'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 24'},
                ],
                'secondaryOutcomes': [
                    {'measure': 'Change from Baseline in Body Weight', 'timeFrame': 'Week 24'},
                ],
            },
        },
        'hasResults': has_results,
    }
    if enrollment_count is not None:
        s['protocolSection']['designModule']['enrollmentInfo'] = {
            'count': enrollment_count,
            'type': enrollment_type,
        }
    if docs is not None:
        s['documentSection'] = {'largeDocumentModule': {'largeDocs': docs}}
    if has_results:
        s['resultsSection'] = {
            'participantFlowModule': flow if flow is not None else make_flow(),
            'baselineCharacteristicsModule': baseline if baseline is not None else make_baseline(),
            'outcomeMeasuresModule': {
                'outcomeMeasures': measures if measures is not None else [
                    make_primary_measure(
                        analyses=[
                            {
                                'groupIds': ['OG000', 'OG001'],
                                'paramType': 'LS Mean Difference',
                                'paramValue': '-0.70',
                                'ciPctValue': '95',
                                'ciNumSides': 'TWO_SIDED',
                                'ciLowerLimit': '-0.92',
                                'ciUpperLimit': '-0.48',
                                'pValue': '<0.0001',
                                'statisticalMethod': 'MMRM',
                                'nonInferiorityType': 'SUPERIORITY',
                            }
                        ]
                    )
                ]
            },
        }
    return s


# ---------------------------------------------------------------- 1. denominator funnel & class deviations

def test_denominator_funnel_exact_differences_ratios_and_class_deviations() -> None:
    pm = make_primary_measure(
        og_counts=('95', '85'),
        population_description='mITT population: randomized participants with >= 1 post-baseline HbA1c.',
        classes=[
            {
                'title': 'Week 12 Interim Row',
                'denoms': [{'units': 'Participants', 'counts': _counts(('97', '90'), 'OG')}],
                'categories': [{'measurements': _counts(('-0.6', '-0.1'), 'OG')}],
            },
            {
                'title': 'Week 24 Primary Row',
                'denoms': [{'units': 'Participants', 'counts': _counts(('95', '85'), 'OG')}],
                'categories': [{'measurements': _counts(('-0.9', '-0.2'), 'OG')}],
            },
        ],
    )
    study = make_study(
        1,
        enrollment_count=205,
        enrollment_type='ACTUAL',
        flow=make_flow(started=('100', '100')),
        baseline=make_baseline(counts=('99', '98', '197')),
        measures=[pm],
    )
    rec = C.inspect_study(study)
    funnel = rec['denominator_funnel']
    assert funnel['registered_enrollment'] == {
        'count': 205,
        'type': 'ACTUAL',
        'state': 'ok',
        'source_field': 'protocolSection.designModule.enrollmentInfo',
    }
    assert funnel['period1_started']['value'] == 200 and funnel['period1_started']['state'] == 'ok'
    assert funnel['baseline_total']['value'] == 197 and funnel['baseline_total']['method'] == 'titled_total'
    assert funnel['baseline_minus_period1_started'] == -3
    assert funnel['baseline_over_period1_started'] == 0.985

    pm_out = rec['primary_measures'][0]
    assert pm_out['analysed_total'] == 180
    assert pm_out['analysed_minus_baseline_total'] == -17
    assert pm_out['analysed_minus_period1_started'] == -20
    assert pm_out['analysed_over_period1_started'] == 0.9
    assert pm_out['analysed_over_baseline_total'] == round(180 / 197, 6)
    assert pm_out['population_description'] == 'mITT population: randomized participants with >= 1 post-baseline HbA1c.'

    # Only class 0 differs from measure-level denoms (97 vs 95 and 90 vs 85); class 1 matches.
    assert pm_out['has_class_denom_deviation'] is True
    assert len(pm_out['class_denom_deviations']) == 1
    dev = pm_out['class_denom_deviations'][0]
    assert dev['class_index'] == 0
    assert dev['json_path'] == 'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].classes[0].denoms[0]'
    assert dev['group_deviations'] == [
        {'group_id': 'OG000', 'measure_count': 95, 'class_count': 97, 'raw_class_value': '97', 'class_minus_measure': 2},
        {'group_id': 'OG001', 'measure_count': 85, 'class_count': 90, 'raw_class_value': '90', 'class_minus_measure': 5},
    ]


def test_denominator_funnel_handles_no_total_column_flow_absent_and_non_participant_units() -> None:
    # Baseline with no Total column sums non-Total groups; Period 2 in flow is ignored; Eyes unit is flagged.
    flow_eyes = make_flow(started=('190', '192'), type_units_analyzed='Eyes', use_num_units_only=True)
    base_no_tot = make_baseline(titles=('Zorbatinib 10 mg', 'Placebo'), counts=('95', '96'))
    pm_eyes = make_primary_measure(denom_units='Eyes', og_counts=('190', '192'))
    rec = C.inspect_study(make_study(2, flow=flow_eyes, baseline=base_no_tot, measures=[pm_eyes]))

    funnel = rec['denominator_funnel']
    assert funnel['period1_started']['state'] == 'unit_not_participants'
    assert funnel['period1_started']['value'] is None
    assert funnel['baseline_total']['value'] == 191
    assert funnel['baseline_total']['method'] == 'non_total_group_sum'
    assert funnel['baseline_minus_period1_started'] is None

    pm_out = rec['primary_measures'][0]
    assert pm_out['denom_state'] == 'unit_not_participants'
    assert pm_out['analysed_total'] is None
    assert pm_out['analysed_minus_baseline_total'] is None
    assert pm_out['analysed_over_period1_started'] is None

    # Absent flow module -> flow_absent (never 0)
    study_no_flow = make_study(3)
    del study_no_flow['resultsSection']['participantFlowModule']
    rec_no_flow = C.inspect_study(study_no_flow)
    assert rec_no_flow['denominator_funnel']['period1_started']['state'] == 'flow_absent'
    assert rec_no_flow['denominator_funnel']['period1_started']['value'] is None


# ---------------------------------------------------------------- 2. conservative arm alignment & attrition

def test_arm_alignment_refuses_index_only_and_count_only_matching_and_handles_reversed_order() -> None:
    # Scenario A: identical array indices (0, 1) AND identical participant counts (100, 95),
    # with unrelated titles -> MUST refuse to match (`no_title_match`) and withhold attrition!
    pm_unaligned = make_primary_measure(
        og_titles=('Active Regimen', 'Control Regimen'),
        og_counts=('100', '95'),
        analyses=[{'groupIds': ['OG000', 'OG001'], 'pValue': '0.01'}],
    )
    study_unaligned = make_study(
        10,
        flow=make_flow(titles=('Cohort Alpha', 'Cohort Beta'), started=('100', '95')),
        baseline=make_baseline(titles=('Cohort Alpha', 'Cohort Beta', 'Total'), counts=('100', '95', '195')),
        measures=[pm_unaligned],
    )
    rec_u = C.inspect_study(study_unaligned)
    pm_u = rec_u['primary_measures'][0]
    assert [a['state'] for a in pm_u['og_to_bg_alignment']['og_alignments']] == ['no_title_match', 'no_title_match']
    assert [a['state'] for a in pm_u['og_to_fg_alignment']['og_alignments']] == ['no_title_match', 'no_title_match']
    attr_u = pm_u['analyses'][0]['differential_attrition']
    assert attr_u['from_period1_started']['computable'] is False
    assert attr_u['from_baseline']['computable'] is False
    assert attr_u['from_baseline']['abs_differential_attrition'] is None

    # Scenario B: reversed order (OG000=Placebo, OG001=Zorbatinib vs BG000=Zorbatinib, BG001=Placebo)
    # -> matches by normalized unique title to BG001 and BG000, NOT array index!
    pm_rev = make_primary_measure(
        og_titles=('Placebo', 'Zorbatinib 10 mg'),
        og_counts=('80', '95'),
        analyses=[{'groupIds': ['OG000', 'OG001'], 'pValue': '0.002'}],
    )
    rec_rev = C.inspect_study(
        make_study(
            11,
            flow=make_flow(titles=('Zorbatinib 10 mg', 'Placebo'), started=('100', '100')),
            baseline=make_baseline(titles=('Zorbatinib 10 mg', 'Placebo', 'Total'), counts=('100', '100', '200')),
            measures=[pm_rev],
        )
    )
    pm_r = rec_rev['primary_measures'][0]
    bg_map = {a['og_id']: a for a in pm_r['og_to_bg_alignment']['og_alignments']}
    assert bg_map['OG000']['state'] == 'exact_unique_title_match' and bg_map['OG000']['matched_target_id'] == 'BG001'
    assert bg_map['OG001']['state'] == 'exact_unique_title_match' and bg_map['OG001']['matched_target_id'] == 'BG000'

    # Differential attrition: Placebo attrition = 1 - 80/100 = 0.20; Zorbatinib = 1 - 95/100 = 0.05;
    # abs differential attrition = |0.20 - 0.05| = 0.15!
    attr_r = pm_r['analyses'][0]['differential_attrition']['from_period1_started']
    assert attr_r['computable'] is True
    assert attr_r['arm_a']['attrition_rate'] == 0.20
    assert attr_r['arm_b']['attrition_rate'] == 0.05
    assert attr_r['signed_differential_attrition'] == 0.15
    assert attr_r['abs_differential_attrition'] == 0.15


def test_duplicate_titles_pooled_arms_boilerplate_descriptions_and_extra_baseline_cohorts() -> None:
    # Duplicate titles without disambiguating descriptions -> ambiguous_duplicate_titles
    pm_dup = make_primary_measure(og_titles=('Zorbatinib', 'Placebo'), og_counts=('50', '50'))
    rec_dup = C.inspect_study(
        make_study(
            12,
            baseline=make_baseline(titles=('Zorbatinib', 'Zorbatinib', 'Placebo', 'Total'), counts=('50', '50', '50', '150')),
            measures=[pm_dup],
        )
    )
    states_dup = {a['og_id']: a['state'] for a in rec_dup['primary_measures'][0]['og_to_bg_alignment']['og_alignments']}
    assert states_dup == {'OG000': 'ambiguous_duplicate_titles', 'OG001': 'exact_unique_title_match'}

    # Distinct dose titles sharing identical boilerplate descriptions must NEVER match via description
    pm_dose = make_primary_measure(
        og_titles=('Zorbatinib 10 mg',),
        og_descriptions=('Administered orally once daily.',),
        og_counts=('50',),
    )
    rec_dose = C.inspect_study(
        make_study(
            13,
            baseline=make_baseline(
                titles=('Zorbatinib 20 mg',),
                counts=('50',),
                descriptions=('Administered orally once daily.',),
            ),
            measures=[pm_dose],
        )
    )
    assert rec_dose['primary_measures'][0]['og_to_bg_alignment']['og_alignments'][0]['state'] == 'no_title_match'

    # Pooled OG title -> pooled_or_subset_suspected
    pm_pool = make_primary_measure(og_titles=('Pooled Zorbatinib Doses', 'Placebo'), og_counts=('190', '95'))
    rec_pool = C.inspect_study(
        make_study(
            14,
            baseline=make_baseline(
                titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo', 'Total'),
                counts=('98', '99', '98', '295'),
            ),
            measures=[pm_pool],
        )
    )
    al_pool = {a['og_id']: a['state'] for a in rec_pool['primary_measures'][0]['og_to_bg_alignment']['og_alignments']}
    assert al_pool == {'OG000': 'pooled_or_subset_suspected', 'OG001': 'exact_unique_title_match'}

    # Extra baseline cohort outside primary (e.g. open-label run-in cohort in BG)
    pm_rand = make_primary_measure(og_titles=('Zorbatinib 10 mg', 'Placebo'), og_counts=('95', '85'))
    rec_extra = C.inspect_study(
        make_study(
            15,
            baseline=make_baseline(
                titles=('Zorbatinib 10 mg', 'Placebo', 'Non-Randomized Open-Label Run-In Cohort', 'Total'),
                counts=('99', '98', '42', '239'),
            ),
            measures=[pm_rand],
        )
    )
    assert rec_extra['arm_alignment']['extra_baseline_groups_outside_primary'] is True
    assert [g['title'] for g in rec_extra['arm_alignment']['extra_baseline_groups']] == [
        'Non-Randomized Open-Label Run-In Cohort'
    ]

    # Duplicate titles uniquely disambiguated by non-empty descriptions -> exact_unique_title_match
    pm_td = make_primary_measure(
        og_titles=('Zorbatinib', 'Zorbatinib'),
        og_descriptions=('Cohort 1: Fasted', 'Cohort 2: Fed'),
        og_counts=('30', '30'),
    )
    rec_td = C.inspect_study(
        make_study(
            16,
            baseline=make_baseline(
                titles=('Zorbatinib', 'Zorbatinib', 'Total'),
                counts=('30', '30', '60'),
                descriptions=('Cohort 2: Fed', 'Cohort 1: Fasted', None),
            ),
            measures=[pm_td],
        )
    )
    td_map = {a['og_id']: a for a in rec_td['primary_measures'][0]['og_to_bg_alignment']['og_alignments']}
    assert td_map['OG000']['state'] == 'exact_unique_title_match'
    assert td_map['OG000']['matched_target_id'] == 'BG001'
    assert td_map['OG001']['state'] == 'exact_unique_title_match'
    assert td_map['OG001']['matched_target_id'] == 'BG000'

    # Placeholder titles ("Arm 1" vs "Group A") with unique identical descriptions -> exact_unique_title_match
    pm_ph = make_primary_measure(
        og_titles=('Arm 1', 'Arm 2'),
        og_descriptions=('Zorbatinib 10 mg once daily', 'Matching placebo once daily'),
        og_counts=('48', '47'),
    )
    rec_ph = C.inspect_study(
        make_study(
            17,
            baseline=make_baseline(
                titles=('Group A', 'Group B', 'Total'),
                counts=('50', '50', '100'),
                descriptions=('Zorbatinib 10 mg once daily', 'Matching placebo once daily', None),
            ),
            measures=[pm_ph],
        )
    )
    assert [a['state'] for a in rec_ph['primary_measures'][0]['og_to_bg_alignment']['og_alignments']] == [
        'exact_unique_title_match',
        'exact_unique_title_match',
    ]

    # Absent baseline groups -> module_groups_absent
    study_no_bg = make_study(18)
    del study_no_bg['resultsSection']['baselineCharacteristicsModule']
    rec_no_bg = C.inspect_study(study_no_bg)
    assert [a['state'] for a in rec_no_bg['primary_measures'][0]['og_to_bg_alignment']['og_alignments']] == [
        'module_groups_absent',
        'module_groups_absent',
    ]


def test_rule_t_untitled_total_and_malformed_sections_never_coerce_to_zero() -> None:
    # Rule T: 3+ baseline groups where last group's count equals sum of earlier groups
    rec_ut = C.inspect_study(
        make_study(
            19,
            baseline=make_baseline(
                titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Combined Study Total'),
                counts=('40', '60', '100'),
            ),
        )
    )
    bt = rec_ut['denominator_funnel']['baseline_total']
    assert bt['value'] == 100 and bt['method'] == 'last_equals_sum_untitled_total'

    # Malformed outcomeMeasures (dict instead of list) records parse failure and keeps sections None
    malformed = make_study(99)
    malformed['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'] = {'bad': 'shape'}
    rec_mal = C.inspect_study(malformed)
    assert rec_mal['parse']['status'] == 'failed'
    assert rec_mal['primary_measures'] is None
    assert rec_mal['arm_alignment'] is None
    assert rec_mal['denominator_funnel']['baseline_total']['value'] == 197

    # Non-dict record is kept as unknown/failed without raising
    rec_non_dict = C.inspect_study('not a study dict')
    assert rec_non_dict['results_status'] == 'unknown' and rec_non_dict['parse']['status'] == 'failed'


# ---------------------------------------------------------------- 3. estimands, missing data, documents, hypotheses

def test_documents_missing_data_and_intercurrent_event_signals_with_exact_paths() -> None:
    docs = [
        {
            'filename': 'Prot_000.pdf',
            'uploadDate': '2023-06-01T09:30',
            'date': '2020-01-15',
            'label': 'Study Protocol',
            'typeAbbrev': 'Prot',
            'hasProtocol': True,
            'hasSap': False,
            'hasIcf': False,
        },
        {
            'filename': 'SAP_ICF_001.pdf',
            'uploadDate': '2023-06-02T11:00',
            'date': '2022-11-10',
            'label': 'SAP and ICF',
            'typeAbbrev': 'SAP_ICF',
            'hasProtocol': False,
            'hasSap': True,
            'hasIcf': True,
        },
    ]
    pm = make_primary_measure(
        population_description=(
            'Treatment-policy estimand: all randomized participants regardless of rescue medication '
            'or premature treatment discontinuation.'
        ),
        analyses=[
            {
                'groupIds': ['OG000', 'OG001'],
                'pValue': '0.001',
                'statisticalComment': 'Primary analysis used Mixed-effects model for repeated measures (MMRM).',
                'otherAnalysisDescription': (
                    'Sensitivity analysis used LOCF excluding measurements after initiation of rescue medication.'
                ),
            }
        ],
    )
    rec = C.inspect_study(make_study(20, docs=docs, measures=[pm]))
    pd_out = rec['posted_documents']
    assert pd_out['has_posted_protocol'] is True
    assert pd_out['has_posted_sap'] is True
    assert pd_out['has_posted_icf'] is True
    assert pd_out['filenames'] == ['Prot_000.pdf', 'SAP_ICF_001.pdf']
    assert pd_out['upload_dates'] == ['2023-06-01T09:30', '2023-06-02T11:00']

    md = rec['estimand_signals']['missing_data_signal']
    assert md['state'] == 'multiple_methods_named'
    assert md['methods_detected'] == ['mmrm', 'locf']
    assert {
        (e['method'], e['json_path'], e['matched_substring'])
        for e in md['evidence']
    } == {
        (
            'mmrm',
            'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0].statisticalComment',
            'Mixed-effects model for repeated measures',
        ),
        (
            'mmrm',
            'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0].statisticalComment',
            'MMRM',
        ),
        (
            'locf',
            'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0].otherAnalysisDescription',
            'LOCF',
        ),
    }

    ice = rec['estimand_signals']['intercurrent_event_signal']
    assert ice['state'] == 'both_co_reported'
    assert {e['signal'] for e in ice['evidence']} == {
        'treatment_policy_regardless_of_rescue_or_discontinuation',
        'rescue_excluded_or_censored',
    }
    assert all(e['json_path'] and e['matched_substring'] and e['snippet'] for e in ice['evidence'])


@pytest.mark.parametrize(
    'text, expected_state',
    [
        ('Analyzed via MMRM model.', 'mmrm'),
        ('Missing Week 24 values imputed by washout multiple imputation.', 'multiple_imputation'),
        ('Last observation carried forward was applied.', 'locf'),
        ('Baseline observation carried forward (BOCF) for dropouts.', 'bocf'),
        ('Worst observation carried forward.', 'wocf'),
        ('Observed cases only without imputation of missing values.', 'observed_cases_only'),
        ('ANCOVA with treatment and stratum as fixed effects.', 'not_stated'),
    ],
)
def test_individual_missing_data_method_states(text: str, expected_state: str) -> None:
    sig = C.detect_missing_data_signals([('resultsSection.outcomeMeasuresModule.outcomeMeasures[0].populationDescription', text)])
    assert sig['state'] == expected_state


@pytest.mark.parametrize(
    'analysis_dict, expected_state',
    [
        ({'nonInferiorityType': 'SUPERIORITY'}, 'superiority_explicit'),
        (
            {'nonInferiorityType': 'NON_INFERIORITY', 'nonInferiorityComment': 'Non-inferiority margin of 0.3% HbA1c.'},
            'non_inferiority_with_margin',
        ),
        (
            {'nonInferiorityType': 'NON_INFERIORITY', 'nonInferiorityComment': 'Two-sided 95% CI was computed.'},
            'non_inferiority_missing_margin',
        ),
        (
            {'nonInferiorityType': 'EQUIVALENCE', 'nonInferiorityComment': '90% CI within 80% to 125%.'},
            'equivalence_with_margin',
        ),
        (
            {'nonInferiorityType': 'EQUIVALENCE', 'nonInferiorityComment': 'Previously provided.'},
            'equivalence_missing_margin',
        ),
        ({'nonInferiorityType': 'SUPERIORITY_OR_OTHER'}, 'ambiguous_legacy_enum'),
        ({'nonInferiorityType': 'SUPERIORITY_OR_OTHER_LEGACY', 'statisticalComment': 'ANCOVA'}, 'ambiguous_legacy_enum'),
        ({'nonInferiorityType': 'NON_INFERIORITY_OR_EQUIVALENCE'}, 'ambiguous_legacy_enum'),
        ({'nonInferiorityType': 'NON_INFERIORITY_OR_EQUIVALENCE_LEGACY'}, 'ambiguous_legacy_enum'),
        (
            {
                'nonInferiorityType': 'NON_INFERIORITY_OR_EQUIVALENCE',
                'nonInferiorityComment': 'Non-inferiority met if upper limit of 95% CI is below 0.4%.',
            },
            'non_inferiority_with_margin',
        ),
        ({'nonInferiorityType': 'OTHER'}, 'other_explicit'),
        ({}, 'not_stated'),
    ],
)
def test_hypothesis_and_margin_clarity_states(analysis_dict: dict[str, Any], expected_state: str) -> None:
    got = C.classify_analysis_hypothesis(
        analysis_dict,
        'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0]',
    )
    assert got['state'] == expected_state


# ---------------------------------------------------------------- 4. contrast cardinality & shared-arm non-independence

def test_contrast_cardinality_and_shared_arm_non_independence() -> None:
    pm = make_primary_measure(
        og_titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo'),
        og_counts=('96', '95', '94'),
        analyses=[
            {'groupIds': ['OG000', 'OG002'], 'pValue': '0.01'},                # Low vs Placebo
            {'groupIds': ['OG001', 'OG002'], 'pValue': '0.0002'},              # High vs Placebo (shares OG002!)
            {'groupIds': ['OG000', 'OG001', 'OG002'], 'pValue': '0.0005'},     # multi_arm_joint
            {'groupIds': ['OG000', 'OG000'], 'pValue': '0.03'},                # single_arm with duplicate groupId
            {'groupIds': [], 'pValue': '0.50'},                                # empty
        ],
    )
    study = make_study(
        30,
        flow=make_flow(titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo'), started=('100', '100', '100')),
        baseline=make_baseline(
            titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo', 'Total'),
            counts=('100', '100', '100', '300'),
        ),
        measures=[pm],
    )
    rec = C.inspect_study(study)
    cs = rec['primary_measures'][0]['contrast_structure']
    assert cs['cardinality_counts'] == {
        'empty': 1,
        'single_arm': 1,
        'pairwise_two_arm': 2,
        'multi_arm_joint': 1,
    }
    assert cs['n_analyses_with_duplicate_group_ids'] == 1
    assert cs['has_shared_arm_contrasts'] is True
    assert rec['contrast_structure']['has_shared_arm_contrasts'] is True
    assert len(cs['shared_arm_contrast_groups']) == 1

    sg = cs['shared_arm_contrast_groups'][0]
    assert sg['shared_group_id'] == 'OG002'
    assert sg['shared_group_title'] == 'Placebo'
    assert sg['analysis_indices'] == [0, 1]
    assert sg['includes_distinct_partner_arms'] is True
    assert sg['non_independent'] is True

    analyses = rec['primary_measures'][0]['analyses']
    assert analyses[0]['statistically_independent_within_measure'] is False
    assert analyses[0]['shares_arm_with_analysis_indices'] == [1]
    assert analyses[1]['statistically_independent_within_measure'] is False
    assert analyses[1]['shares_arm_with_analysis_indices'] == [0]


# ---------------------------------------------------------------- 5. build, run_identity, contract refusals, site export

def _sample_studies() -> list[dict[str, Any]]:
    return [
        make_study(1, docs=[{'filename': 'Prot_SAP.pdf', 'uploadDate': '2024-01-01', 'hasProtocol': True, 'hasSap': True, 'hasIcf': False}]),
        make_study(2, has_results=False, enrollment_count=150, enrollment_type='ESTIMATED'),
        make_study(
            3,
            measures=[
                make_primary_measure(
                    og_titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo'),
                    og_counts=('96', '95', '94'),
                    analyses=[
                        {'groupIds': ['OG000', 'OG002'], 'nonInferiorityType': 'SUPERIORITY_OR_OTHER', 'pValue': '0.02'},
                        {'groupIds': ['OG001', 'OG002'], 'nonInferiorityType': 'SUPERIORITY_OR_OTHER', 'pValue': '0.001'},
                    ],
                )
            ],
            flow=make_flow(titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo'), started=('100', '100', '100')),
            baseline=make_baseline(titles=('Zorbatinib 5 mg', 'Zorbatinib 10 mg', 'Placebo', 'Total'), counts=('100', '100', '100', '300')),
        ),
    ]


def test_run_identity_is_deterministic_content_sensitive_and_summary_counts_complete() -> None:
    studies = _sample_studies()
    ids = [C.nct_id_of(s) or '' for s in studies]
    acq = {
        'source': 'fictional',
        'mode': 'archive',
        'retrieved_at': '2026-11-15T10:00:00+00:00',
        'api_version': '2.0.5',
        'data_timestamp': '2026-11-15T09:00:00',
        'n_requested': len(ids),
        'n_records': len(ids),
        'requested_ids_sha256': 'a' * 64,
        'sha256_uncompressed': 'b' * 64,
        'archive': 'records-fictional.jsonl.gz',
    }
    prod = {'repository': 'civicsample-engine', 'commit': 'c' * 40, 'uncommitted_changes': False, 'source_sha256': '1' * 64}
    scope = B.build_scope(ids, ids)
    recs, probs = B.build_records(studies, ids, {})

    fam_t1 = B.build(recs, probs, acq, prod, {'source': 'archive'}, scope, generated_at='2026-11-15T12:00:00+00:00')
    fam_t2 = B.build(recs, probs, acq, prod, {'source': 'archive'}, scope, generated_at='2030-05-01T00:00:00+00:00')
    assert B.check(fam_t1) == []
    assert fam_t1['run.json']['run_id'] == fam_t2['run.json']['run_id']

    # Changing producer source_sha256 or any record content changes run_id
    fam_dirty = B.build(
        recs, probs, acq, {**prod, 'source_sha256': '2' * 64}, {'source': 'archive'}, scope,
        generated_at='2026-11-15T12:00:00+00:00',
    )
    assert fam_dirty['run.json']['run_id'] != fam_t1['run.json']['run_id']

    # Verify every count object in summary.json has value, numerator, denominator, definition
    count_objs = list(B._iter_count_objects(fam_t1['summary.json']))
    assert len(count_objs) >= 40
    for where, cobj in count_objs:
        assert set(('value', 'numerator', 'denominator', 'definition')) <= set(cobj), where
        assert cobj['value'] == cobj['numerator']
        assert isinstance(cobj['definition'], str) and cobj['definition']


def test_contract_checker_refuses_tampering_private_keys_and_broken_invariants(tmp_path: Path) -> None:
    studies = _sample_studies()
    rec_dir = tmp_path / 'records_in'
    rec_dir.mkdir()
    for s in studies:
        nct = C.nct_id_of(s)
        (rec_dir / f'{nct}.json.gz').write_bytes(gzip.compress(json.dumps(s).encode('utf-8'), mtime=0))

    out_dir = tmp_path / 'build_out'
    assert build_outcomes_comparability.main(['--records-dir', str(rec_dir), '--out', str(out_dir)]) == 0
    assert check_outcomes_comparability_contract.main([str(out_dir)]) == 0

    base = B.load_dir(out_dir)

    # 1. Tampered metric numerator -> sha256 + recount failure
    bad_num = copy.deepcopy(base)
    bad_num['summary.json']['metrics'][0]['numerator'] += 1
    bad_num['summary.json']['metrics'][0]['value'] += 1
    problems_num = B.check(bad_num)
    assert any('sha256' in p for p in problems_num)
    assert any('recount' in p for p in problems_num)

    # 2. Tampered run_id even with matching file sha256s -> run_identity refusal
    bad_rid = copy.deepcopy(base)
    fake_id = 'outcomes-comparability-2026-11-10-0000000000'
    for k in ('run.json', 'summary.json', 'records.json'):
        bad_rid[k]['run_id'] = fake_id
    bad_rid['run.json']['files'] = {k: B.sha256(B.dump(bad_rid[k])) for k in ('summary.json', 'records.json')}
    assert B.check(bad_rid) == ['run.json run_id does not follow from the content of the family']

    # 3. Leaked private key
    bad_priv = copy.deepcopy(base)
    bad_priv['records.json']['records']['NCT99000001']['reviewer'] = 'Secret Person'
    assert any('private keys' in p for p in B.check(bad_priv))

    # 4. Undisambiguated SUPERIORITY_OR_OTHER tampered to superiority_explicit -> domain refusal
    bad_hyp = copy.deepcopy(base)
    bad_hyp['records.json']['records']['NCT99000003']['primary_measures'][0]['analyses'][0]['hypothesis_clarity'][
        'state'
    ] = 'superiority_explicit'
    assert any('undisambiguated legacy hypothesis enum' in p for p in B.check(bad_hyp))

    # 5. CLI check_outcomes_comparability_contract returns exit code 1 when bytes on disk are tampered
    tampered_dir = tmp_path / 'tampered_out'
    B.write(base, tampered_dir)
    (tampered_dir / 'summary.json').write_text(
        json.dumps(base['summary.json'], indent=2) + '\n', encoding='utf-8'
    )
    assert check_outcomes_comparability_contract.main([str(tampered_dir)]) == 1


def test_cli_archive_exclude_history_and_site_export_atomic_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(B, 'fetch', lambda *a, **k: pytest.fail('network called during offline test'))
    studies = _sample_studies()
    ids = sorted(C.nct_id_of(s) or '' for s in studies)
    lines = {C.nct_id_of(s) or '': acquire.encode([s]) for s in studies}
    rec_meta = {
        'source': 'fictional',
        'mode': 'fetch',
        'api_version': '2.0.5',
        'data_timestamp': '2026-11-15T10:00:00',
        'retrieved_at': '2026-11-15T10:00:00+00:00',
        'retrieved_from': '2026-11-15T10:00:00+00:00',
        'retrieved_to': '2026-11-15T10:00:00+00:00',
        'n_records': len(lines),
        'n_requested': len(ids),
        'not_retrieved': [],
    }
    arch = tmp_path / B.archive_name(lines, rec_meta, ids)
    B.write_archive(lines, arch, rec_meta, ids)

    # Historical archive for NCT99000001 with timeframe change (Week 12 -> Week 24)
    hist_path = tmp_path / 'hist.jsonl.gz'
    hist_raw = (
        json.dumps({
            'nct_id': 'NCT99000001',
            'version_date': '2019-05-01',
            'primaryOutcomes': [{'measure': 'Change from Baseline in HbA1c', 'timeFrame': 'Week 12'}],
        })
        + '\n'
    ).encode('utf-8')
    hist_path.write_bytes(gzip.compress(hist_raw, mtime=0))

    inc_file = tmp_path / 'included.txt'
    inc_file.write_text('NCT99000001\nNCT99000003\n', encoding='utf-8')
    exc_file = tmp_path / 'excluded.txt'
    exc_file.write_text('NCT99000002\n', encoding='utf-8')

    build_dir = tmp_path / 'build'
    assert build_outcomes_comparability.main([
        '--archive', str(arch),
        '--nct-list', str(inc_file),
        '--exclude-list', str(exc_file),
        '--exclude-reason', 'no posted results in cohort rehearsal',
        '--history-archive', str(hist_path),
        '--out', str(build_dir),
    ]) == 0
    assert check_outcomes_comparability_contract.main([str(build_dir)]) == 0

    built = B.load_dir(build_dir)
    assert built['records.json']['records']['NCT99000001']['outcome_history']['version_history_state'] == 'evaluated'
    assert built['records.json']['records']['NCT99000001']['outcome_history']['version_history']['state_counts'][
        'timeframe_changed'
    ] == 1
    assert built['records.json']['records']['NCT99000003']['outcome_history']['version_history_state'] == (
        'trial_not_in_history_archive'
    )

    # Site export succeeds and verifies all file SHA-256 digests
    site_dir = tmp_path / 'site_comparability'
    assert export_outcomes_comparability_site.main([str(build_dir), str(site_dir)]) == 0
    site_run = json.loads((site_dir / 'run.json').read_text(encoding='utf-8'))
    assert site_run['build_run_id'] == built['run.json']['run_id']
    for fname, digest in site_run['files'].items():
        assert hashlib.sha256((site_dir / fname).read_bytes()).hexdigest() == digest

    # Exporting into an unrelated non-empty directory is refused without deleting it
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'important.txt').write_text('keep me', encoding='utf-8')
    assert export_outcomes_comparability_site.main([str(build_dir), str(unrelated)]) == 1
    assert (unrelated / 'important.txt').read_text(encoding='utf-8') == 'keep me'

    # Simulated OSError during final swap restores previous exported site directory intact
    before_bytes = {p.name: p.read_bytes() for p in site_dir.iterdir()}
    real_rename = Path.rename

    def fail_stage_rename(self: Path, target: Any) -> Any:
        if '.staging.' in self.name:
            raise OSError('simulated disk rename failure')
        return real_rename(self, target)

    monkeypatch.setattr(Path, 'rename', fail_stage_rename)
    with pytest.raises(OSError, match='simulated disk rename failure'):
        export_outcomes_comparability_site.export(build_dir, site_dir)
    monkeypatch.undo()
    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before_bytes

    # Tampering with build_dir causes site export to refuse non-zero
    (build_dir / 'summary.json').write_bytes(b'{"tampered":true}\n')
    assert export_outcomes_comparability_site.main([str(build_dir), str(tmp_path / 'site2')]) == 1


class _FakeResp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self.status_code = status
        self.content = json.dumps(body).encode('utf-8')

    def json(self) -> Any:
        return json.loads(self.content)


def test_fetch_rate_limiting_version_guard_and_responses_cache_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    studies = _sample_studies()[:2]
    by_id = {C.nct_id_of(s) or '': s for s in studies}
    waits: list[float] = []

    def fake_get(url: str, params: dict[str, Any]) -> _FakeResp:
        if url.endswith('/version'):
            return _FakeResp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-15T10:00:00'})
        wanted = [i for i in params['filter.ids'].split(',') if i in by_id]
        return _FakeResp({'studies': [by_id[i] for i in wanted]})

    lines, meta = B.fetch(['NCT99000001', 'NCT99000002', 'NCT99000099'], get=fake_get, sleep=waits.append)
    assert sorted(lines) == ['NCT99000001', 'NCT99000002']
    assert meta['not_retrieved'] == [{'nct_id': 'NCT99000099', 'reason': 'not returned by registry for filter.ids'}]
    assert meta['min_seconds_between_requests'] >= 0.5 and any(w > 0 for w in waits)

    # Missing /version refuses before requesting studies
    asked_urls: list[str] = []

    def bad_ver(url: str, params: dict[str, Any]) -> _FakeResp:
        asked_urls.append(url)
        return _FakeResp({}, 200)

    with pytest.raises(B.BuildError, match='registry version request failed'):
        B.fetch(['NCT99000001'], get=bad_ver, sleep=lambda _s: None)
    assert all(u.endswith('/version') for u in asked_urls)

    # CLI --fetch (stubbed) and --responses-cache
    monkeypatch.setattr(build_outcomes_comparability, 'ROOT', tmp_path)
    for rel in build_outcomes_comparability.PRODUCER_PATHS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    monkeypatch.setattr(B, 'fetch', lambda req_ids: (dict(lines), dict(meta)))
    id_list = tmp_path / 'fetch_ids.txt'
    id_list.write_text('NCT99000001\nNCT99000002\nNCT99000099\n', encoding='utf-8')
    fetch_out = tmp_path / 'fetch_out'
    assert build_outcomes_comparability.main(['--fetch', '--nct-list', str(id_list), '--out', str(fetch_out)]) == 0
    fetched_fam = B.load_dir(fetch_out)
    assert fetched_fam['records.json']['records']['NCT99000099']['results_status'] == 'not_retrieved'
    assert fetched_fam['summary.json']['tables']['retrieval_failures']['count']['numerator'] == 1

    resp_cache = tmp_path / 'raw_responses.jsonl.gz'
    resp_line = json.dumps({'fetched_at': '2026-11-15T10:00:00Z', 'body': json.dumps({'studies': studies})}) + '\n'
    resp_cache.write_bytes(gzip.compress(resp_line.encode('utf-8'), mtime=0))
    rc_out = tmp_path / 'rc_out'
    assert build_outcomes_comparability.main(['--responses-cache', str(resp_cache), '--out', str(rc_out)]) == 0
    assert check_outcomes_comparability_contract.main([str(rc_out)]) == 0


# ---------------------------------------------------------------- 6. code standards ratchets (CLAUDE.md)

@pytest.mark.parametrize(
    'rel_path',
    [
        'outcomes/comparability.py',
        'outcomes/outcome_history.py',
        'outcomes/comparability_build.py',
        'scripts/build_outcomes_comparability.py',
        'scripts/check_outcomes_comparability_contract.py',
        'scripts/export_outcomes_comparability_site.py',
    ],
)
def test_every_new_module_has_future_annotations_full_type_hints_and_zero_rng(rel_path: str) -> None:
    source = (ROOT / rel_path).read_text(encoding='utf-8')
    tree = ast.parse(source, filename=rel_path)

    has_future = any(
        isinstance(node, ast.ImportFrom)
        and node.module == '__future__'
        and any(alias.name == 'annotations' for alias in node.names)
        for node in tree.body
    )
    assert has_future, f'{rel_path}: missing from __future__ import annotations'

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != 'random', f'{rel_path}: forbidden random import'
        elif isinstance(node, ast.ImportFrom):
            assert node.module != 'random', f'{rel_path}: forbidden random import'
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.returns is not None, f'{rel_path}:{node.lineno} {node.name}() lacks return annotation'
            all_args = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            for arg in all_args:
                if arg.arg in ('self', 'cls'):
                    continue
                assert arg.annotation is not None, (
                    f'{rel_path}:{node.lineno} {node.name}() parameter {arg.arg!r} lacks type annotation'
                )
