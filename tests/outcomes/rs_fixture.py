"""Fictional ClinicalTrials.gov records with results sections, for outcomes/results_structure.py.

Every NCT number (NCT990000xx), drug, count and text here is invented. The records carry only the
fields the results-structure rules read. scripts/build_results_structure_fixture.py turns
`studies()` into the artifact family under tests/fixtures/results_structure/, which the site's
reader tests copy (tests/fixtures/results_structure/ there, with this repository's commit in
SOURCE.json). The fixture review rows are fictional too: no real person reviewed anything here.
"""
from __future__ import annotations

from typing import Any

GENERATED_AT = '2026-11-14T12:00:00+00:00'
REVIEWER = 'Fictional Reviewer'


def groups(titles: tuple[str, ...], prefix: str, descriptions: tuple[str | None, ...] | None = None) -> list[dict[str, Any]]:
    """Registry-style groups: ids PREFIX000, PREFIX001, ... in order."""
    out = []
    for i, t in enumerate(titles):
        g: dict[str, Any] = {'id': f'{prefix}{i:03d}', 'title': t}
        if descriptions and descriptions[i]:
            g['description'] = descriptions[i]
        out.append(g)
    return out


def counts(values: tuple[str, ...], prefix: str) -> list[dict[str, str]]:
    return [{'groupId': f'{prefix}{i:03d}', 'value': v} for i, v in enumerate(values)]


def baseline(titles: tuple[str, ...] = ('Zorbatinib', 'Placebo', 'Total'), n: tuple[str, ...] = ('100', '100', '200'),
             values: tuple[str, ...] | None = ('54.1', '53.8', '54.0'), population: str | None = None) -> dict[str, Any]:
    """A baseline module with one Age measure. values=None gives a table with no numeric value."""
    mod: dict[str, Any] = {'groups': groups(titles, 'BG'), 'denoms': [{'units': 'Participants', 'counts': counts(n, 'BG')}],
                           'measures': [{'title': 'Age, Continuous', 'paramType': 'MEAN', 'unitOfMeasure': 'years',
                                         'classes': [{'categories': [{'measurements': [
                                             {'groupId': f'BG{i:03d}', 'value': (values[i] if values else 'NA')}
                                             for i in range(len(titles))]}]}]}]}
    if population:
        mod['populationDescription'] = population
    return mod


def flow(titles: tuple[str, ...] = ('Zorbatinib', 'Placebo'), started: tuple[str, ...] = ('100', '100'),
         completed: tuple[str, ...] = ('90', '95')) -> dict[str, Any]:
    return {'groups': groups(titles, 'FG'), 'periods': [{'title': 'Overall Study', 'milestones': [
        {'type': 'STARTED', 'achievements': [{'groupId': f'FG{i:03d}', 'numSubjects': v} for i, v in enumerate(started)]},
        {'type': 'COMPLETED', 'achievements': [{'groupId': f'FG{i:03d}', 'numSubjects': v} for i, v in enumerate(completed)]}]}]}


def measure(title: str = 'Change in fictional score', mtype: str = 'PRIMARY', titles: tuple[str, ...] = ('Zorbatinib', 'Placebo'),
            analysed: tuple[str, ...] | None = ('98', '97'), values: tuple[str, ...] | None = ('-4.2', '-1.1'),
            unit: str | None = 'units on a scale', class_titles: tuple[str, ...] | None = None,
            analyses: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """One outcome measure. class_titles gives one class per title, each with every group's value;
    values=None posts 'NA' for every group; unit=None with no values is a measure with no data table."""
    m: dict[str, Any] = {'type': mtype, 'title': title, 'timeFrame': 'Week 12', 'paramType': 'MEAN',
                         'groups': groups(titles, 'OG')}
    if unit:
        m['unitOfMeasure'] = unit
    if analysed is not None:
        m['denoms'] = [{'units': 'Participants', 'counts': counts(analysed, 'OG')}]
    if unit or values is not None:
        row = [{'groupId': f'OG{i:03d}', 'value': (values[i] if values else 'NA')} for i in range(len(titles))]
        m['classes'] = [{'title': t, 'categories': [{'measurements': [dict(x) for x in row]}]} for t in class_titles] \
            if class_titles else [{'categories': [{'measurements': row}]}]
    if analyses is not None:
        m['analyses'] = analyses
    m.update(extra)
    return m


def analysis(group_ids: tuple[str, ...] = ('OG000', 'OG001'), **kw: Any) -> dict[str, Any]:
    a: dict[str, Any] = {'groupIds': list(group_ids)}
    a.update(kw)
    return a


def adverse(titles: tuple[str, ...] = ('Zorbatinib', 'Placebo'), at_risk: tuple[str, ...] = ('100', '100'),
            rows: bool = True) -> dict[str, Any]:
    mod: dict[str, Any] = {'frequencyThreshold': '5', 'eventGroups': [
        {'id': f'EG{i:03d}', 'title': t, 'seriousNumAffected': '2' if rows else '0', 'seriousNumAtRisk': at_risk[i],
         'otherNumAffected': '10' if rows else '0', 'otherNumAtRisk': at_risk[i], 'deathsNumAffected': '0',
         'deathsNumAtRisk': at_risk[i]} for i, t in enumerate(titles)]}
    if rows:
        mod['seriousEvents'] = [{'term': 'Fictional event', 'stats': [{'groupId': 'EG000', 'numAffected': '2', 'numAtRisk': at_risk[0]}]}]
        mod['otherEvents'] = [{'term': 'Headache', 'stats': [{'groupId': 'EG000', 'numEvents': '12', 'numAffected': '10', 'numAtRisk': at_risk[0]}]}]
    return mod


def study(n: int, *, has_results: bool = True, allocation: str | None = 'RANDOMIZED', study_type: str = 'INTERVENTIONAL',
          enrollment: int | None = 200, results: dict[str, Any] | None = None) -> dict[str, Any]:
    """A study record. results=None with has_results gives the default complete results section."""
    s: dict[str, Any] = {
        'protocolSection': {
            'identificationModule': {'nctId': f'NCT990000{n:02d}', 'briefTitle': f'Fictional trial {n} of zorbatinib'},
            'designModule': {'studyType': study_type, 'phases': ['PHASE3'],
                             'designInfo': {'allocation': allocation} if allocation else {}},
            'outcomesModule': {'primaryOutcomes': [{'measure': 'Change in fictional score', 'timeFrame': 'Week 12'}],
                               'secondaryOutcomes': [{'measure': 'Fictional responder rate', 'timeFrame': 'Week 24'}]},
        },
        'hasResults': has_results,
    }
    if enrollment is not None:
        s['protocolSection']['designModule']['enrollmentInfo'] = {'count': enrollment, 'type': 'ACTUAL'}
    if has_results:
        s['resultsSection'] = results if results is not None else results_section()
    return s


def results_section(**kw: Any) -> dict[str, Any]:
    """A results section in which every usable-content rule passes; pass a module to replace it."""
    rs = {'participantFlowModule': flow(), 'baselineCharacteristicsModule': baseline(),
          'outcomeMeasuresModule': {'outcomeMeasures': [measure()]}, 'adverseEventsModule': adverse(),
          'moreInfoModule': {'pointOfContact': {'title': 'Fictional contact'}}}
    rs.update(kw)
    return {k: v for k, v in rs.items() if v is not None}


def studies() -> list[dict[str, Any]]:
    """The fixture frame. NCT99000003 is requested but deliberately not here (a retrieval failure);
    NCT99000009 is here but excluded from the build (EXCLUDED), so it must have no record."""
    return [
        # 1: every rule passes; randomised with two arms (C2); a between-arm analysis whose p-value is a
        #    bound; class titles by sex and by age band (two level signals, reviewed in reviews_csv).
        study(1, results=results_section(outcomeMeasuresModule={'outcomeMeasures': [
            measure(analyses=[analysis(pValue='<0.001', paramType='Mean Difference (Final Values)', paramValue='-3.1',
                                       ciPctValue='95', ciNumSides='TWO_SIDED', ciLowerLimit='-4.5', ciUpperLimit='-1.7',
                                       statisticalMethod='ANCOVA')]),
            measure('Fictional responder rate', 'SECONDARY', class_titles=('Female', 'Male')),
            measure('Fictional responder rate, older and younger participants', 'SECONDARY',
                    class_titles=('Age 18 to 64 years', 'Age 65 years and older'))]},
            baselineCharacteristicsModule=baseline(population='All randomised participants who received a dose.'))),
        study(2, has_results=False),
        study(4, has_results=False, enrollment=0),
        # 5: hasResults true, but outcomeMeasures is an object where the registry posts a list.
        study(5, results=results_section(outcomeMeasuresModule={'outcomeMeasures': {'unexpected': 'shape'}})),
        # 6: not randomised with two groups on the primary (C1, not C2); an untitled total that equals
        #    the sum of the arms; adverse events with nobody at risk (present, not usable); no signal.
        study(6, allocation='NON_RANDOMIZED', results=results_section(
            baselineCharacteristicsModule=baseline(('Low dose', 'High dose', 'Total Title'), ('40', '60', '100')),
            adverseEventsModule=adverse(at_risk=('0', '0'), rows=False))),
        # 7: one group; the baseline table and the flow post zeros, the primary has no data table; a
        #    secondary description says "subgroup" (a keyword signal with no dimension, unreviewed).
        study(7, allocation=None, results=results_section(
            participantFlowModule=flow(('Zorbatinib',), ('0',), ('0',)),
            baselineCharacteristicsModule=baseline(('Zorbatinib',), ('0',), None),
            outcomeMeasuresModule={'outcomeMeasures': [
                measure(titles=('Zorbatinib',), analysed=None, values=None, unit=None),
                measure('Fictional exposure', 'SECONDARY', titles=('Zorbatinib',), analysed=('12',), values=('3.3',),
                        description='Measured in the subgroup of participants who gave a week 4 sample.')]},
            adverseEventsModule=adverse(('Zorbatinib',), ('12',)))),
        # 9: a complete record that the build leaves out, with a stated reason.
        study(9),
    ]


EXCLUDED = ['NCT99000009']
EXCLUDE_REASON = 'fictional: excluded by the cohort\'s eligibility rules'
FRAME = ['NCT99000001', 'NCT99000002', 'NCT99000003', 'NCT99000004', 'NCT99000005', 'NCT99000006', 'NCT99000007',
         'NCT99000009']
REQUESTED = ['NCT99000001', 'NCT99000002', 'NCT99000003', 'NCT99000004', 'NCT99000005', 'NCT99000006', 'NCT99000007']


def reviews_csv() -> str:
    """Fictional review rows for NCT99000001: the sex signal accepted, the age signal rejected."""
    from outcomes import results_structure as RS
    s = studies()[0]
    sig = {g['dimension']: g['signal_id'] for g in RS.subgroup_signals(
        'NCT99000001', s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'])}
    return (','.join(RS.REVIEW_COLUMNS) + '\n'
            f"NCT99000001,sex,{sig['sex']},accepted,{REVIEWER},2026-11-14,Fictional: values are posted separately for female and male participants\n"
            f"NCT99000001,age,{sig['age']},rejected,{REVIEWER},2026-11-14,Fictional: the age bands label two cohorts given different doses\n")
