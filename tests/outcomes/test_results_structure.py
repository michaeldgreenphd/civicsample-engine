"""Results structure: usable-content rules, comparative classes, between-arm analyses, baseline
population, subgroup signals and states, and the human review file.
Fictional records only (tests/outcomes/rs_fixture.py); no network."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rs_fixture as F  # noqa: E402
from outcomes import results_structure as RS  # noqa: E402


def record(study: dict[str, Any], reviews: dict[tuple[str, str], dict[str, str]] | None = None) -> dict[str, Any]:
    rec = RS.study_record(study)
    RS.apply_reviews(rec, reviews)
    return rec


def reviews_from(tmp_path: Path, rows: list[str]) -> dict[str, dict[tuple[str, str], dict[str, str]]]:
    path = tmp_path / 'subgroup_reviews.csv'
    path.write_text(','.join(RS.REVIEW_COLUMNS) + '\n' + ''.join(r + '\n' for r in rows))
    return RS.load_reviews(path)


# ---------------------------------------------------------------- numbers

@pytest.mark.parametrize('value, expected', [('12', True), ('1,234', True), (' 3.5 ', True), ('NA', False), ('', False),
                                             (None, False), ('inf', False), ('nan', False), (0, True)])
def test_numeric_means_a_finite_number_and_absence_is_never_zero(value: Any, expected: bool) -> None:
    assert RS.is_num(value) is expected
    assert (RS.num(value) is not None) is expected


# ---------------------------------------------------------------- usable-content rules

def test_a_complete_record_passes_all_four_rules_and_more_info_is_not_counted() -> None:
    rec = record(F.study(1))
    assert {k: (m['present'], m['usable'], m['reason']) for k, m in rec['modules'].items()} == {
        'baseline': (True, True, 'ok'), 'participant_flow': (True, True, 'ok'),
        'outcome_measures': (True, True, 'ok'), 'adverse_events': (True, True, 'ok')}
    assert rec['all_four_usable'] is True
    assert set(rec['modules']) == {'baseline', 'participant_flow', 'outcome_measures', 'adverse_events'}


@pytest.mark.parametrize('module, reason', [
    (F.baseline(values=None), 'no_numeric_values'),                              # a table of NA
    (F.baseline(n=('0', '0', '0')), 'values_or_denominators_incomplete'),        # values, zero participants
    ({'groups': F.groups(('A',), 'BG'), 'denoms': [], 'measures': []}, 'no_measures'),
    ({}, 'no_groups'),
])
def test_b1_fails_with_the_reason(module: dict[str, Any], reason: str) -> None:
    rec = record(F.study(1, results=F.results_section(baselineCharacteristicsModule=module)))
    assert (rec['modules']['baseline']['usable'], rec['modules']['baseline']['reason']) == (False, reason)
    assert rec['all_four_usable'] is False


def test_b1_passes_on_the_total_alone_or_on_every_arm() -> None:
    total_only = F.baseline(values=('NA', 'NA', '54.0'))
    assert RS.baseline_usable(total_only)[2] == {'total_method': 'titled_total', 'ok_for_total': True, 'ok_for_every_arm': False,
                                                 'n_measures': 1, 'n_measures_with_numeric': 1}
    arms_only = F.baseline(values=('54.1', '53.8', 'NA'))
    assert RS.baseline_usable(arms_only)[:2] == (True, 'ok')
    assert RS.baseline_usable(arms_only)[2]['ok_for_total'] is False


@pytest.mark.parametrize('module, reason', [
    (F.flow(started=('0', '0'), completed=('0', '0')), 'counts_missing_for_a_group_or_all_zero'),
    (F.flow(started=('100', 'NA')), 'counts_missing_for_a_group_or_all_zero'),
    ({'groups': F.groups(('A',), 'FG'), 'periods': [{'milestones': [{'type': 'COMPLETED', 'achievements': []}]}]},
     'no_STARTED_milestone'),
    ({'groups': F.groups(('A',), 'FG'), 'periods': [{'milestones': [{'type': 'STARTED', 'achievements': []}]}]},
     'no_COMPLETED_milestone'),
    ({}, 'no_groups'),
])
def test_f1_fails_with_the_reason(module: dict[str, Any], reason: str) -> None:
    assert RS.flow_usable(module) == (False, reason)
    assert RS.flow_usable(F.flow()) == (True, 'ok')


@pytest.mark.parametrize('kw, reason', [
    ({'unit': None, 'values': None, 'analysed': None}, 'no_data_table'),
    ({'unit': None}, 'no_unit'),
    ({'analysed': None}, 'no_number_analysed'),
    ({'analysed': ('98', 'NA')}, 'number_analysed_missing_for_a_group'),
    ({'analysed': ('0', '0')}, 'zero_analysed_in_every_group'),
    ({'values': ('-4.2', 'NA')}, 'no_numeric_value_for_an_analysed_group'),
])
def test_o1_fails_with_the_reason(kw: dict[str, Any], reason: str) -> None:
    rec = record(F.study(1, results=F.results_section(outcomeMeasuresModule={'outcomeMeasures': [F.measure(**kw)]})))
    assert (rec['modules']['outcome_measures']['usable'], rec['modules']['outcome_measures']['reason']) == (False, reason)
    assert rec['primary_measures'][0]['reason'] == reason


def test_o1_needs_a_primary_measure_and_passes_when_one_of_several_does() -> None:
    only_secondary = record(F.study(1, results=F.results_section(
        outcomeMeasuresModule={'outcomeMeasures': [F.measure(mtype='SECONDARY')]})))
    assert only_secondary['modules']['outcome_measures']['reason'] == 'no_primary_measure'
    mixed = record(F.study(1, results=F.results_section(outcomeMeasuresModule={'outcomeMeasures': [
        F.measure(unit=None, values=None, analysed=None), F.measure('Second primary')]})))
    assert mixed['modules']['outcome_measures']['usable'] is True
    assert mixed['modules']['outcome_measures']['detail']['n_primary_usable'] == 1
    # A group nobody was analysed in does not fail O1, but it is not the strict form.
    partial = RS.measure_usable(F.measure(analysed=('98', '0'), values=('-4.2', 'NA')))
    assert partial == (True, False, 'ok')


@pytest.mark.parametrize('module, reason', [
    (None, 'module_absent'),
    ({'eventGroups': []}, 'no_event_groups'),
    (F.adverse(at_risk=('0', '0'), rows=False), 'zero_at_risk_in_every_group'),
    (F.adverse(at_risk=('100', 'NA')), 'numbers_at_risk_missing_for_a_group'),
])
def test_a1_fails_with_the_reason(module: dict[str, Any] | None, reason: str) -> None:
    rec = record(F.study(1, results=F.results_section(adverseEventsModule=module)))
    ae = rec['modules']['adverse_events']
    assert (ae['usable'], ae['reason']) == (False, reason)
    assert ae['present'] is bool(module)


def test_a1_accepts_explicit_zero_totals_without_event_rows_but_not_missing_totals() -> None:
    zeros = F.adverse(rows=False)
    assert RS.ae_usable(zeros)[:2] == (True, 'ok')
    assert RS.ae_usable(zeros)[2]['explicit_zero_only'] is True
    for g in zeros['eventGroups']:
        del g['otherNumAffected']
    assert RS.ae_usable(zeros)[:2] == (False, 'no_event_rows_and_no_group_totals')
    posted = RS.adverse_events(F.adverse())
    assert (posted['serious_events_posted'], posted['other_events_posted'], posted['frequency_threshold']) == (True, True, '5')
    assert posted['groups'][0] == {'group_id': 'EG000', 'title': 'Zorbatinib', 'serious_at_risk': 100, 'other_at_risk': 100,
                                   'deaths_at_risk': 100}
    assert RS.adverse_events(None) is None


# ---------------------------------------------------------------- comparative classes and analyses

def test_c1_is_structure_and_c2_adds_registered_randomisation() -> None:
    assert record(F.study(1))['comparative'] == {'C1': True, 'C2': True, 'primary_max_groups': 2}
    assert record(F.study(1, allocation='NON_RANDOMIZED'))['comparative'] == {'C1': True, 'C2': False, 'primary_max_groups': 2}
    assert record(F.study(1, allocation=None))['comparative']['C2'] is False
    assert record(F.study(1, study_type='OBSERVATIONAL'))['comparative']['C1'] is False
    one_group = F.results_section(outcomeMeasuresModule={'outcomeMeasures': [
        F.measure(titles=('Zorbatinib',), analysed=('98',), values=('1',))]})
    assert record(F.study(1, results=one_group))['comparative'] == {'C1': False, 'C2': False, 'primary_max_groups': 1}


@pytest.mark.parametrize('value, form', [('<0.001', 'bound'), ('< 0.05', 'bound'), ('≤0.01', 'bound'), ('>0.99', 'bound'),
                                         ('0.023', 'number'), ('=0.023', 'number'), ('.04', 'number'),
                                         ('p<0.05 (one-sided)', 'other_with_digit'), ('NS', 'no_digit'),
                                         (None, 'absent'), ('  ', 'absent')])
def test_a_p_value_bound_is_kept_apart_from_a_plain_number(value: Any, form: str) -> None:
    assert RS.p_value_form(value) == form


def analysed(*analyses: dict[str, Any], measure_kw: dict[str, Any] | None = None) -> dict[str, Any]:
    return record(F.study(1, results=F.results_section(
        outcomeMeasuresModule={'outcomeMeasures': [F.measure(analyses=list(analyses), **(measure_kw or {}))]})))


def test_between_arm_analysis_with_a_bound_p_value_and_both_limits() -> None:
    rec = analysed(F.analysis(pValue='<0.001', paramType='Hazard Ratio (HR)', paramValue='0.71', ciLowerLimit='0.60',
                              ciUpperLimit='0.84'))
    assert rec['between_arm_analysis'] == {
        'n_analyses_on_primary': 1, 'any_usable_analysis': True, 'posted': True, 'with_p_value': True,
        'with_estimate_and_both_limits': True, 'estimate_types': ['Hazard Ratio (HR)'], 'p_value_forms': ['bound']}
    a = rec['primary_measures'][0]['analyses'][0]
    assert (a['p_value'], a['p_value_form'], a['both_confidence_limits'], a['estimate_type']) == \
        ('<0.001', 'bound', True, 'Hazard Ratio (HR)')


def test_an_analysis_needs_named_groups_two_of_them_and_a_p_value_or_both_limits() -> None:
    one_group = analysed(F.analysis(('OG000',), pValue='0.03'))
    assert (one_group['between_arm_analysis']['any_usable_analysis'], one_group['between_arm_analysis']['posted']) == (True, False)
    unknown_group = analysed(F.analysis(('OG000', 'OG009'), pValue='0.03'))
    assert unknown_group['between_arm_analysis']['any_usable_analysis'] is False
    one_limit = analysed(F.analysis(paramValue='0.71', ciUpperLimit='0.84', ciNumSides='ONE_SIDED'))
    assert one_limit['between_arm_analysis']['posted'] is False
    assert one_limit['primary_measures'][0]['analyses'][0]['both_confidence_limits'] is False
    estimate_only = analysed(F.analysis(paramValue='0.71', ciLowerLimit='0.60', ciUpperLimit='0.84'))
    assert estimate_only['between_arm_analysis'] == {
        'n_analyses_on_primary': 1, 'any_usable_analysis': True, 'posted': True, 'with_p_value': False,
        'with_estimate_and_both_limits': True, 'estimate_types': [], 'p_value_forms': []}
    none = record(F.study(1))
    assert (none['between_arm_analysis']['n_analyses_on_primary'], none['between_arm_analysis']['posted']) == (0, False)
    # An analysis on a secondary measure is not a primary-measure analysis.
    secondary = record(F.study(1, results=F.results_section(outcomeMeasuresModule={'outcomeMeasures': [
        F.measure(), F.measure('Other', 'SECONDARY', analyses=[F.analysis(pValue='0.01')])]})))
    assert secondary['between_arm_analysis']['posted'] is False


# ---------------------------------------------------------------- outcomes, flow, baseline population

def test_registered_and_posted_outcomes_by_type() -> None:
    rec = record(F.studies()[0])
    assert rec['registered_outcomes']['primary'] == {'count': 1, 'items': [{'title': 'Change in fictional score', 'time_frame': 'Week 12'}]}
    assert rec['registered_outcomes']['secondary']['count'] == 1 and rec['registered_outcomes']['other']['count'] == 0
    assert rec['posted_outcomes']['PRIMARY'] == {
        'count': 1, 'with_data': 1, 'units': ['units on a scale'], 'param_types': ['MEAN'], 'n_groups_min': 2,
        'n_groups_max': 2, 'with_participant_counts': 1, 'denominator_units': ['Participants']}
    assert rec['posted_outcomes']['SECONDARY']['count'] == 2
    eyes = F.measure()
    eyes['denoms'] = [{'units': 'Eyes', 'counts': F.counts(('98', '97'), 'OG')}]
    no_data = F.measure(unit=None, values=None, analysed=None)
    posted = RS.posted_outcomes([eyes, no_data])
    assert (posted['PRIMARY']['with_data'], posted['PRIMARY']['with_participant_counts'], posted['PRIMARY']['denominator_units']) == \
        (1, 0, ['Eyes'])


def test_participant_flow_is_the_first_period_and_says_so() -> None:
    mod = F.flow()
    mod['periods'].append({'title': 'Extension', 'milestones': [
        {'type': 'STARTED', 'achievements': [{'groupId': 'FG000', 'numSubjects': '50'}, {'groupId': 'FG001', 'numSubjects': '40'}]}]})
    flow = RS.flow_first_period(mod)
    assert (flow['period'], flow['n_periods'], flow['period_title'], flow['reason']) == ('first', 2, 'Overall Study', 'ok')
    assert flow['arms'] == [{'group_id': 'FG000', 'title': 'Zorbatinib', 'started': 100, 'completed': 90},
                            {'group_id': 'FG001', 'title': 'Placebo', 'started': 100, 'completed': 95}]
    missing = RS.flow_first_period(F.flow(started=('100', 'NA')))
    assert missing['arms'][1]['started'] is None, 'a count that is not numeric is absent, not zero'


def test_a_baseline_total_group_is_not_added_to_its_arms() -> None:
    rec = record(F.study(1))
    bp = rec['baseline_population']
    assert (bp['total']['value'], bp['total']['method'], bp['total']['arms_sum']) == (200, 'titled_total', 200)
    assert "groupId == 'BG002'" in bp['total']['source_field']
    assert (bp['started']['value'], bp['enrollment']['value'], bp['enrollment']['type']) == (200, 200, 'ACTUAL')
    assert bp['relations'] == {'total_vs_started': 'equal', 'total_vs_enrollment': 'equal', 'started_vs_enrollment': 'equal'}
    assert bp['population_description'] is None, 'no population is inferred where the text is absent'


@pytest.mark.parametrize('titles, n, value, method', [
    (('Low', 'High', 'Total Title'), ('40', '60', '100'), 100, 'last_equals_sum_untitled_total'),   # naive rule gives 200
    (('Low', 'High'), ('40', '60'), 100, 'sum_of_arms'),
    (('Only',), ('40',), 40, 'single_group'),
    (('Low', 'High', 'Other'), ('40', '60', '30'), 130, 'sum_of_arms'),
    (('Period 1', 'Period 2', 'Total'), ('8', '8', '8'), 8, 'titled_total'),        # a crossover: the posted Total is used
])
def test_baseline_total_by_rule_t(titles: tuple[str, ...], n: tuple[str, ...], value: int, method: str) -> None:
    total = RS.baseline_total(F.baseline(titles, n, tuple('1' for _ in titles)))
    assert (total['value'], total['method']) == (value, method)


def test_baseline_population_records_absence_with_a_reason() -> None:
    bad = F.baseline(n=('100', 'NA', '200'))
    assert RS.baseline_total(bad)['value'] is None and RS.baseline_total(bad)['reason'] == 'non_numeric_count'
    assert RS.baseline_total({})['reason'] == 'no_denominator_row'
    assert RS.flow_started({})['reason'] == 'no_period'
    assert RS.flow_started(F.flow(started=('100', 'NA')))['reason'] == 'non_numeric_or_missing_numSubjects'
    rec = record(F.study(1, enrollment=None, results=F.results_section(
        baselineCharacteristicsModule=F.baseline(population='Participants who received a dose.'))))
    bp = rec['baseline_population']
    assert bp['enrollment'] == {'value': None, 'type': None, 'reason': 'not_registered',
                                'source_field': 'protocolSection.designModule.enrollmentInfo.count'}
    assert bp['relations']['total_vs_enrollment'] == 'missing'
    assert bp['population_description']['text'] == 'Participants who received a dose.'
    total_group = F.flow(('Zorbatinib', 'Placebo', 'Total'), ('100', '100', '200'), ('90', '95', '185'))
    assert RS.flow_started(total_group)['value'] == 200, 'a flow group titled Total is not summed with the arms'


# ---------------------------------------------------------------- subgroup signals

def signals_of(*measures: dict[str, Any]) -> list[dict[str, Any]]:
    return RS.subgroup_signals('NCT99000001', list(measures))


def test_level_signals_carry_field_path_measure_and_verbatim_text() -> None:
    [s] = signals_of(F.measure('Overall'), F.measure('Response by sex', 'SECONDARY', class_titles=('Women', 'Men')))[:1]
    assert s == {'signal_id': s['signal_id'], 'dimension': 'sex', 'rule_dimension': 'sex', 'field': 'class_titles',
                 'field_path': 'resultsSection.outcomeMeasuresModule.outcomeMeasures[1].classes[].title',
                 'measure_index': 1, 'measure_type': 'SECONDARY', 'measure_title': 'Response by sex',
                 'matched_text': ['Women', 'Men'], 'matched_text_truncated': False, 'context': None, 'review': []}
    assert RS._SIGNAL_ID.match(s['signal_id'])


@pytest.mark.parametrize('titles, dimension, rule_dimension', [
    (('Female', 'Male'), 'sex', 'sex'),
    (('White', 'Black or African American'), 'race', 'race'),
    (('Hispanic or Latino', 'Not Hispanic or Latino'), 'ethnicity', 'ethnicity'),
    (('Age 18 to 64 years', 'Age 65 years and older'), 'age', 'age'),
    (('Children', 'Adults'), 'age', 'age'),
    (('Japanese', 'Non-Japanese'), 'other', 'origin'),
])
def test_level_rules_by_dimension(titles: tuple[str, ...], dimension: str, rule_dimension: str) -> None:
    sig = signals_of(F.measure(class_titles=titles))
    assert [(s['dimension'], s['rule_dimension'], s['field']) for s in sig] == [(dimension, rule_dimension, 'class_titles')]


@pytest.mark.parametrize('titles', [('White blood cell count', 'Black box warning'), ('Week 4', 'Week 8'),
                                    ('Female',), ('Male and female', 'All'), ('Adults and children', 'All')])
def test_level_rules_do_not_fire_on_one_level_or_another_meaning(titles: tuple[str, ...]) -> None:
    assert signals_of(F.measure(class_titles=titles)) == []


def test_cue_signals_keyword_is_other_and_by_demographic_names_its_dimension() -> None:
    kw = signals_of(F.measure(description='Analysed in the subgroup of participants with a week 4 sample.'))
    assert [(s['dimension'], s['rule_dimension'], s['field'], s['matched_text']) for s in kw] == \
        [('other', 'keyword', 'measure_description', ['subgroup'])]
    assert kw[0]['context'] == 'Analysed in the subgroup of participants with a week 4 sample.'
    assert kw[0]['field_path'].endswith('outcomeMeasures[0].description')
    for text, dim in (('Response by age group', 'age'), ('Response by gender', 'sex'), ('Results by race', 'race'),
                      ('Events according to ethnicity', 'ethnicity')):
        [s] = signals_of(F.measure(text))
        assert (s['dimension'], s['rule_dimension'], s['field']) == (dim, 'by_demographic', 'measure_title'), text
    # A model description is not a cue.
    assert signals_of(F.measure(description='ANCOVA adjusted for age and sex.')) == []
    assert signals_of(F.measure('Height for age z-score')) == []


def test_analysis_text_and_group_reference_signals() -> None:
    m = F.measure(titles=('Women', 'Men'), analyses=[
        F.analysis(statisticalComment='Cochran-Mantel-Haenszel test stratified by age group'),
        F.analysis(pValue='0.04')])
    got = [(s['field'], s['dimension'], s['field_path'].split('outcomeMeasures[0].')[1]) for s in signals_of(m)]
    assert got == [('group_titles', 'sex', 'groups[].title'), ('analysis_groupIds', 'sex', 'analyses[0].groupIds'),
                   ('analysis_text:statisticalComment', 'age', 'analyses[0].statisticalComment')]


def test_signal_ids_are_stable_distinct_and_do_not_depend_on_measure_order() -> None:
    a, b = F.measure('First', class_titles=('Female', 'Male')), F.measure('Second', class_titles=('Female', 'Male'))
    ids = [s['signal_id'] for s in signals_of(a, b)]
    assert len(set(ids)) == 2
    assert sorted(s['signal_id'] for s in signals_of(b, a)) == sorted(ids)
    twice = [s['signal_id'] for s in signals_of(a, copy.deepcopy(a))]
    assert len(set(twice)) == 2, 'two identical measures still give two signal ids'
    assert RS.subgroup_signals('NCT99000002', [a])[0]['signal_id'] != ids[0]


def test_look_alike_signals_keep_their_ids_when_the_registry_reorders_the_measures() -> None:
    # same type, same title, the same matched word in the same field, in two different sentences
    a = F.measure('Response', description='Reported for the subgroup of participants who completed week 12.')
    b = F.measure('Response', description='A subgroup with baseline scores above 20 was analysed separately.')
    by_context = lambda *ms: {s['context']: s['signal_id'] for s in signals_of(*ms)}   # noqa: E731
    first, swapped = by_context(a, b), by_context(b, a)
    assert len(first) == 2 and len(set(first.values())) == 2
    assert first == swapped, 'a review of one sentence must not move to the other when the order changes'
    # an edit somewhere else in one of the measures moves no id either
    edited = copy.deepcopy(b)
    edited['unitOfMeasure'] = 'points on a different scale'
    edited['timeFrame'] = 'Week 24'
    assert by_context(a, edited) == first and by_context(edited, a) == first
    # the id follows the sentence: change the sentence and that signal, and only that one, is a new signal
    reworded = F.measure('Response', description='A subgroup with baseline scores above 30 was analysed separately.')
    after = by_context(a, reworded)
    assert set(after.values()) & set(first.values()) == {first[next(c for c in first if 'completed week 12' in c)]}


def test_the_rule_reads_exactly_eighteen_fields_and_lists_what_it_does_not() -> None:
    assert len(RS.FIELDS_INSPECTED) == 18 and len(set(RS.FIELDS_INSPECTED)) == 18
    assert any('documentSection' in f for f in RS.FIELDS_NOT_INSPECTED)
    assert any('baselineCharacteristicsModule' in f for f in RS.FIELDS_NOT_INSPECTED)
    # A demographic word in a field the rule does not read fires nothing.
    quiet = F.measure(timeFrame='Week 12, by sex')
    assert signals_of(quiet) == []


# ---------------------------------------------------------------- subgroup states

SEX = F.measure('Response', 'SECONDARY', class_titles=('Female', 'Male'))
KEYWORD = F.measure('Exposure', 'SECONDARY', description='In the subgroup of participants with a sample.')


def with_measures(*extra: dict[str, Any]) -> dict[str, Any]:
    return F.study(1, results=F.results_section(outcomeMeasuresModule={'outcomeMeasures': [F.measure(), *extra]}))


def states(rec: dict[str, Any]) -> tuple[str, dict[str, str]]:
    return rec['subgroup']['state'], rec['subgroup']['by_dimension']


def test_no_signal_is_none_detected_in_the_reviewed_structured_fields() -> None:
    rec = record(F.study(1))
    assert states(rec) == (RS.NONE_DETECTED, {d: RS.NONE_DETECTED for d in RS.DIMENSIONS})
    assert rec['subgroup']['signals'] == [] and rec['subgroup']['n_signals'] == 0
    assert rec['document_and_publication_review'] == 'not_reviewed'


def test_an_unreviewed_signal_is_indeterminate_for_its_dimension_only() -> None:
    state, dims = states(record(with_measures(SEX)))
    assert state == RS.FAILED_OR_INDETERMINATE
    assert dims == {'age': RS.NONE_DETECTED, 'sex': RS.FAILED_OR_INDETERMINATE, 'race': RS.NONE_DETECTED,
                    'ethnicity': RS.NONE_DETECTED}


def test_an_unreviewed_signal_with_no_named_dimension_leaves_all_four_indeterminate() -> None:
    state, dims = states(record(with_measures(KEYWORD)))
    assert state == RS.FAILED_OR_INDETERMINATE
    assert dims == {d: RS.FAILED_OR_INDETERMINATE for d in RS.DIMENSIONS}


def test_without_review_rows_no_trial_is_in_the_detected_state() -> None:
    for s in F.studies():
        rec = record(s)
        assert RS.DETECTED not in (rec['subgroup']['state'], *rec['subgroup']['by_dimension'].values())


def test_a_trial_without_posted_results_is_not_applicable_which_is_not_a_processing_state() -> None:
    rec = record(F.study(2, has_results=False))
    assert rec['results_status'] == 'no_posted_results'
    assert states(rec) == (RS.NOT_APPLICABLE, {d: RS.NOT_APPLICABLE for d in RS.DIMENSIONS})
    assert RS.NOT_APPLICABLE not in RS.SUBGROUP_STATES and len(RS.SUBGROUP_STATES) == 4
    assert rec['modules'] is None and rec['all_four_usable'] is None and rec['comparative'] is None
    assert rec['subgroup']['signals'] is None, 'no results were read, so there is no empty list of signals'
    assert rec['registered_outcomes']['primary']['count'] == 1
    assert rec['baseline_population']['total'] == {'value': None, 'reason': 'no_posted_results'}
    assert rec['document_and_publication_review'] == 'not_reviewed'


def test_a_record_not_retrieved_is_not_yet_processed() -> None:
    rec = RS.not_retrieved_record('NCT99000003', 'not returned by the registry for filter.ids')
    assert rec['results_status'] == 'not_retrieved' and rec['retrieval']['status'] == 'failed'
    assert states(rec) == (RS.NOT_YET_PROCESSED, {d: RS.NOT_YET_PROCESSED for d in RS.DIMENSIONS})


def test_a_parse_failure_is_recorded_with_its_reason_and_never_read_as_none_detected() -> None:
    rec = record(F.studies()[3])           # outcomeMeasures is an object, not a list
    assert rec['nct_id'] == 'NCT99000005' and rec['results_status'] == 'posted'
    assert rec['parse']['status'] == 'failed'
    assert {f['section'] for f in rec['parse']['failures']} >= {'outcome_measures', 'subgroup'}
    assert all(f['reason'] for f in rec['parse']['failures'])
    assert states(rec) == (RS.FAILED_OR_INDETERMINATE, {d: RS.FAILED_OR_INDETERMINATE for d in RS.DIMENSIONS})
    o1 = rec['modules']['outcome_measures']
    assert (o1['usable'], o1['reason']) == (None, 'not_evaluated_parse_failure')
    assert rec['all_four_usable'] is None, 'an unevaluated rule is not a failed rule'
    assert rec['modules']['baseline']['usable'] is True, 'the sections that parse are still reported'
    assert rec['subgroup']['signals'] is None and rec['subgroup']['n_signals'] is None


@pytest.mark.parametrize('study, reason', [
    ({'protocolSection': {'identificationModule': {'nctId': 'NCT99000009'}}}, 'hasResults is absent or not a boolean'),
    ({'protocolSection': {'identificationModule': {'nctId': 'NCT99000009'}}, 'hasResults': True},
     'hasResults is true but resultsSection is absent or empty'),
])
def test_an_unreadable_results_status_is_unknown_not_no_results(study: dict[str, Any], reason: str) -> None:
    rec = record(study)
    assert rec['results_status'] == 'unknown'
    assert rec['parse'] == {'status': 'failed', 'failures': [{'section': 'record', 'reason': reason}]}
    assert rec['subgroup']['state'] == RS.FAILED_OR_INDETERMINATE
    assert record('not a record')['parse']['status'] == 'failed'


def test_a_group_without_an_id_fails_its_section_and_keeps_the_record() -> None:
    mod = F.baseline()
    del mod['groups'][1]['id']
    rec = record(F.study(1, results=F.results_section(baselineCharacteristicsModule=mod)))
    assert rec['modules']['baseline'] == {'present': True, 'rule': 'B1', 'usable': None,
                                          'reason': 'not_evaluated_parse_failure', 'detail': None}
    assert {'section': 'baseline', 'reason': "KeyError: 'id'"} in rec['parse']['failures']
    assert rec['modules']['outcome_measures']['usable'] is True
    assert rec['subgroup']['state'] == RS.NONE_DETECTED, 'the outcome measures were read; the subgroup rule ran'


# ---------------------------------------------------------------- the review file

def sid(rec: dict[str, Any], dimension: str) -> str:
    return next(s['signal_id'] for s in rec['subgroup']['signals'] if s['dimension'] == dimension)


def test_the_shipped_review_file_has_the_header_and_no_rows() -> None:
    path = ROOT / 'outcomes' / 'subgroup_reviews.csv'
    assert path.read_text() == ','.join(RS.REVIEW_COLUMNS) + '\n'
    assert RS.load_reviews(path) == {}


def test_an_accepted_review_gives_detected_for_that_dimension(tmp_path: Path) -> None:
    study = with_measures(SEX)
    s = sid(record(study), 'sex')
    reviews = reviews_from(tmp_path, [f'NCT99000001,sex,{s},accepted,Fictional Reviewer,2026-11-14,posted separately for women and men'])
    rec = RS.study_record(study)
    assert RS.apply_reviews(rec, reviews['NCT99000001']) == []
    assert states(rec) == (RS.DETECTED, {'age': RS.NONE_DETECTED, 'sex': RS.DETECTED, 'race': RS.NONE_DETECTED,
                                         'ethnicity': RS.NONE_DETECTED})
    assert rec['subgroup']['signals'][0]['review'] == [{'dimension': 'sex', 'decision': 'accepted', 'reviewed_on': '2026-11-14'}]
    assert 'Fictional Reviewer' not in json.dumps(rec), 'the reviewer and the note stay in the review file'


def test_a_rejected_review_gives_none_detected_and_unresolved_stays_indeterminate(tmp_path: Path) -> None:
    study = with_measures(SEX)
    s = sid(record(study), 'sex')
    rejected = reviews_from(tmp_path, [f'NCT99000001,sex,{s},rejected,Fictional Reviewer,2026-11-14,the classes are two cohorts'])
    assert states(record(study, rejected['NCT99000001'])) == (RS.NONE_DETECTED, {d: RS.NONE_DETECTED for d in RS.DIMENSIONS})
    unresolved = reviews_from(tmp_path, [f'NCT99000001,sex,{s},unresolved,Fictional Reviewer,2026-11-14,cannot tell from the record'])
    state, dims = states(record(study, unresolved['NCT99000001']))
    assert (state, dims['sex'], dims['age']) == (RS.FAILED_OR_INDETERMINATE, RS.FAILED_OR_INDETERMINATE, RS.NONE_DETECTED)


def test_one_accepted_signal_is_enough_and_an_unreviewed_one_does_not_undo_it(tmp_path: Path) -> None:
    second = F.measure('Second response', 'SECONDARY', class_titles=('Women', 'Men'))
    study = with_measures(SEX, second)
    first = record(study)['subgroup']['signals'][0]['signal_id']
    reviews = reviews_from(tmp_path, [f'NCT99000001,sex,{first},accepted,Fictional Reviewer,2026-11-14,posted by sex'])
    assert states(record(study, reviews['NCT99000001']))[1]['sex'] == RS.DETECTED


def test_reviewing_a_signal_with_no_named_dimension(tmp_path: Path) -> None:
    study = with_measures(KEYWORD)
    s = sid(record(study), 'other')
    row = lambda dim, decision: f'NCT99000001,{dim},{s},{decision},Fictional Reviewer,2026-11-14,fictional'   # noqa: E731
    # Rejected as a whole: settled for every dimension.
    whole = reviews_from(tmp_path, [row('other', 'rejected')])
    assert states(record(study, whole['NCT99000001'])) == (RS.NONE_DETECTED, {d: RS.NONE_DETECTED for d in RS.DIMENSIONS})
    # Accepted for race: the reviewer says which characteristic; the others stay unsettled.
    race = reviews_from(tmp_path, [row('race', 'accepted')])
    state, dims = states(record(study, race['NCT99000001']))
    assert (state, dims['race'], dims['sex']) == (RS.DETECTED, RS.DETECTED, RS.FAILED_OR_INDETERMINATE)
    # Accepted as a subgroup by some other characteristic: detected for the study, not for the four.
    other = reviews_from(tmp_path, [row('other', 'accepted')])
    assert states(record(study, other['NCT99000001'])) == (RS.DETECTED, {d: RS.NONE_DETECTED for d in RS.DIMENSIONS})
    # Rejected for one dimension only: the rest are still unsettled.
    one = reviews_from(tmp_path, [row('sex', 'rejected')])
    state, dims = states(record(study, one['NCT99000001']))
    assert (state, dims['sex'], dims['age']) == (RS.FAILED_OR_INDETERMINATE, RS.NONE_DETECTED, RS.FAILED_OR_INDETERMINATE)


def test_a_review_of_a_typed_signal_under_another_dimension_raises(tmp_path: Path) -> None:
    study = with_measures(SEX)
    s = sid(record(study), 'sex')
    reviews = reviews_from(tmp_path, [f'NCT99000001,race,{s},accepted,Fictional Reviewer,2026-11-14,fictional'])
    with pytest.raises(RS.ReviewError, match="signal's dimension is 'sex'"):
        record(study, reviews['NCT99000001'])


def test_a_review_of_a_signal_the_record_no_longer_has_is_reported_not_applied(tmp_path: Path) -> None:
    reviews = reviews_from(tmp_path, ['NCT99000001,sex,sg-000000000000,accepted,Fictional Reviewer,2026-11-14,fictional'])
    rec = RS.study_record(with_measures(SEX))
    assert RS.apply_reviews(rec, reviews['NCT99000001']) == ['sg-000000000000/sex']
    assert rec['subgroup']['by_dimension']['sex'] == RS.FAILED_OR_INDETERMINATE


GOOD = 'NCT99000001,sex,sg-0123456789ab,accepted,Fictional Reviewer,2026-11-14,fictional note'


@pytest.mark.parametrize('rows, message', [
    ([GOOD, GOOD], 'duplicate review'),
    ([GOOD, GOOD.replace('accepted', 'rejected')], 'conflicting review'),
    ([GOOD.replace('fictional note', '')], 'empty note'),
    ([GOOD.replace('fictional note', '   ')], 'empty note'),
    ([GOOD.replace('Fictional Reviewer', '')], 'no reviewer'),
    ([GOOD.replace('accepted', 'confirmed')], 'decision'),
    ([GOOD.replace(',sex,', ',gender,')], 'dimension'),
    ([GOOD.replace('NCT99000001', 'NCT1')], 'not an NCT id'),
    ([GOOD.replace('sg-0123456789ab', 'measure 3')], 'not a signal id'),
    ([GOOD.replace('2026-11-14', '14 Nov 2026')], 'not YYYY-MM-DD'),
    ([GOOD + ',extra'], 'more cells than columns'),
])
def test_the_review_loader_raises_instead_of_resolving(tmp_path: Path, rows: list[str], message: str) -> None:
    with pytest.raises(RS.ReviewError, match=message):
        reviews_from(tmp_path, rows)


def test_the_review_loader_refuses_another_header_such_as_the_audit_label_table(tmp_path: Path) -> None:
    path = tmp_path / 'subgroup_review.csv'
    path.write_text('nct_id,sample_type,matched_field,matched_text,judgement,reason\n')
    with pytest.raises(RS.ReviewError, match='header must be exactly'):
        RS.load_reviews(path)
    audit = ROOT / 'docs' / 'audits' / 'outcomes_results_structure' / 'second_pass' / 'subgroup_review.csv'
    with pytest.raises(RS.ReviewError, match='header must be exactly'):
        RS.load_reviews(audit)


def test_the_same_signal_may_be_reviewed_for_two_dimensions_without_conflict(tmp_path: Path) -> None:
    rows = [GOOD.replace(',sex,', ',other,'), GOOD.replace(',sex,', ',race,').replace('accepted', 'rejected')]
    assert len(reviews_from(tmp_path, rows)['NCT99000001']) == 2


def test_a_level_signal_keeps_its_id_when_the_registry_reorders_the_sibling_rows() -> None:
    """Sibling class titles are an unordered set to the level rule. Their posted order, and which of
    them the display cut keeps, must not move the id a human review is keyed on."""
    def ids(titles: tuple[str, ...]) -> dict[str, str]:
        signals = RS.subgroup_signals('NCT99000001', [F.measure('Fictional responder rate', 'SECONDARY', class_titles=titles)])
        return {s['dimension']: s['signal_id'] for s in signals if s['field'] == 'class_titles'}
    race = ('White', 'Black or African American', 'Asian')
    assert ids(race) == ids(tuple(reversed(race))) and set(ids(race)) == {'race'}
    # three female rows and one male: the display keeps the first two female titles, the id keeps all
    sex = ('Female, premenopausal', 'Female, postmenopausal', 'Female, status unknown', 'Male')
    shown = [s['matched_text'] for s in RS.subgroup_signals('NCT99000001', [F.measure(class_titles=sex)]) if s['dimension'] == 'sex']
    assert shown == [['Female, premenopausal', 'Female, postmenopausal', 'Male']]
    assert ids(sex) == ids((sex[3], sex[2], sex[0], sex[1]))
    # the level set itself changing is a different signal: a renamed, added or removed level
    assert ids(race)['race'] != ids(('White', 'Black', 'Asian'))['race']
    assert ids(race)['race'] != ids(race + ('American Indian or Alaska Native',))['race']
    assert ids(race)['race'] != ids(race[:2])['race']
    # a sibling row that carries no level is not part of the signal
    assert ids(race)['race'] == ids(race + ('Total',))['race']
    # the function the ids are taken from, directly
    assert RS.level_identity(list(reversed(race))) == {'race': sorted(race)}
    assert RS.level_dimensions(list(race)) == {'race': list(race)}


def test_an_analysis_signal_names_only_the_dimensions_its_groups_differ_on() -> None:
    """Four groups crossing sex and age. An analysis of two groups that differ by sex alone is a sex
    signal, not an age one; an analysis across both is both."""
    titles = ('Women age 18 to 64 years', 'Men age 18 to 64 years', 'Women age 65 years and older', 'Men age 65 years and older')
    def analysis_dims(*group_ids: str) -> list[str]:
        m = F.measure(titles=titles, analysed=('50',) * 4, values=('1',) * 4, analyses=[F.analysis(group_ids)])
        assert {s['dimension'] for s in RS.detect_measure(m) if s['field'] == 'group_titles'} == {'sex', 'age'}
        return [s['dimension'] for s in RS.detect_measure(m) if s['field'] == 'analysis_groupIds']
    assert analysis_dims('OG000', 'OG001') == ['sex']
    assert analysis_dims('OG000', 'OG002') == ['age']
    assert analysis_dims('OG000', 'OG003') == ['age+sex']
    assert analysis_dims('OG000', 'OG001', 'OG002', 'OG003') == ['age+sex']
    # published: one entry per dimension the analysis splits, and none for the other
    m = F.measure(titles=titles, analysed=('50',) * 4, values=('1',) * 4, analyses=[F.analysis(('OG000', 'OG001'))])
    published = [(s['field'], s['dimension']) for s in RS.subgroup_signals('NCT99000001', [m])]
    assert ('analysis_groupIds', 'sex') in published and ('analysis_groupIds', 'age') not in published
