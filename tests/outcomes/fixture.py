"""Fictional ClinicalTrials.gov records and a fictional assessment export for the outcomes beta.

Every NCT number (NCT990000xx), sponsor, drug and date here is invented. The records carry only
the fields the outcomes evidence layer reads. build_fixture() turns them into a full artifact
family; the site's reader tests use copies of that output (tests/fixtures/outcomes/ there).
"""
from __future__ import annotations

from datetime import date
from typing import Any

CUTOFF = date(2026, 11, 9)


def study(n: int, *, sponsor: str = 'Fictional Pharma Inc.', sponsor_class: str = 'INDUSTRY',
          phases: tuple[str, ...] = ('PHASE3',), pcd: str = '2020-06-15', pcd_type: str = 'ACTUAL',
          status: str = 'COMPLETED', has_results: bool = True, docs: bool = True, enrollment: int = 300,
          conditions: tuple[str, ...] = ('Hypertension',), disp: str | None = None) -> dict[str, Any]:
    nct = f'NCT990000{n:02d}'
    s: dict[str, Any] = {
        'protocolSection': {
            'identificationModule': {'nctId': nct, 'briefTitle': f'Fictional trial {n} of zorbatinib'},
            'statusModule': {'overallStatus': status, 'startDateStruct': {'date': '2018-02-01'},
                             'primaryCompletionDateStruct': {'date': pcd, 'type': pcd_type},
                             'studyFirstSubmitDate': '2018-01-15', 'lastUpdatePostDateStruct': {'date': '2024-01-01'}},
            'sponsorCollaboratorsModule': {'leadSponsor': {'name': sponsor, 'class': sponsor_class},
                                           'collaborators': []},
            'conditionsModule': {'conditions': list(conditions)},
            'designModule': {'studyType': 'INTERVENTIONAL', 'phases': list(phases),
                             'designInfo': {'allocation': 'RANDOMIZED'},
                             'enrollmentInfo': {'count': enrollment, 'type': 'ACTUAL'}},
            'outcomesModule': {'primaryOutcomes': [{'measure': f'Change in fictional score {n}', 'timeFrame': 'Week 12'}]},
        },
        'hasResults': has_results,
    }
    if has_results:
        s['protocolSection']['statusModule']['resultsFirstPostDateStruct'] = {'date': '2021-05-01'}
    if disp:
        s['protocolSection']['statusModule']['dispFirstSubmitDate'] = disp
    if docs:
        s['documentSection'] = {'largeDocumentModule': {'largeDocs': [
            {'typeAbbrev': 'Prot_SAP', 'hasProtocol': True, 'hasSap': True, 'hasIcf': False,
             'label': 'Study Protocol and Statistical Analysis Plan', 'date': '2019-03-01',
             'uploadDate': '2021-04-01T10:00', 'filename': 'Prot_SAP_000.pdf', 'size': 1000}]}}
    return s


def studies() -> list[dict[str, Any]]:
    return [
        study(1),                                                     # results posted, documents
        study(2, sponsor='Other Fictional Bio', has_results=False, docs=False),             # past window
        study(3, has_results=False, docs=False, pcd='2020-12', pcd_type='ESTIMATED', status='UNKNOWN'),
        study(4, has_results=False, docs=False, status='WITHDRAWN', enrollment=0),
        study(5, phases=('PHASE2', 'PHASE3')),                        # excluded stratum
        study(6, pcd='2019-12'),                                      # outside the window
        study(7, conditions=('Type 2 Diabetes',), docs=False),
        study(8, sponsor='Fictional Pharma Inc.', has_results=False, docs=False, disp='2021-05-01'),
    ]


def unit(state: str, sub: str | None) -> dict[str, Any]:
    return {'au_id': 'M0A0', 'measure_title': 'Change in fictional score', 'time_frame': 'Week 12', 'part': None,
            'analysis_role': 'PRIMARY_ANALYSIS', 'hypothesis_type': 'NON_INFERIORITY', 'group_titles': ['Zorbatinib', 'Placebo'],
            'reported': {'paramType': 'Mean Difference', 'paramValue': '-1.0', 'ciPctValue': '95', 'ciNumSides': 'TWO_SIDED',
                         'ciLowerLimit': '-3.0', 'ciUpperLimit': '1.0', 'pValue': None, 'statisticalMethod': None},
            'decision_rule': {'alpha': None, 'alpha_sides': None, 'margin': -5.0, 'margin_side': 'lower',
                              'source': 'REGISTRY_FREE_TEXT'},
            'state': state, 'sub_reason': sub, 'rationale': 'Fictional rationale.',
            'evidence': [{'json_path': 'resultsSection.outcomeMeasuresModule.outcomeMeasures[0].analyses[0].ciLowerLimit',
                          'excerpt': '-3.0'}]}


def assessment(nct: str, kind: str, state: str, sub: str | None = None, decision: str | None = None) -> dict[str, Any]:
    reviewed = kind == 'human_reviewed'
    return {'nct_id': nct, 'method_version': '0.3.0', 'assessment_kind': kind, 'trial_state': state,
            'trial_sub_reason': sub, 'trial_rationale': 'Fictional trial rationale.', 'multiplicity': 'SINGLE_PRIMARY',
            'units': [unit(state, sub)] if state in ('MET', 'NOT_MET', 'INSUFFICIENT_EVIDENCE') else [],
            'prespecification': {'registration_timing': 'PROSPECTIVE', 'level': 'PS1'},
            'review': {'status': 'human_reviewed' if reviewed else 'awaiting_human_review',
                       'reviewed_on': '2026-11-10' if reviewed else None, 'decision': decision if reviewed else None,
                       'machine_trial_state': state, 'machine_trial_sub_reason': sub},
            'human_evidence': ([{'document': 'Prot_SAP_000.pdf', 'page': 12, 'quotation': 'Fictional quotation.'}]
                               if reviewed else [])}


def export(mode: str = 'evidence_only') -> dict[str, Any]:
    audit = ['NCT99000001', 'NCT99000002', 'NCT99000005', 'NCT99000007']
    rows = [assessment('NCT99000001', 'human_reviewed', 'MET', decision='CONFIRM')]
    if mode == 'provisional_preview':
        rows += [assessment('NCT99000002', 'machine_provisional', 'UNREPORTED', 'PAST_STANDARD_WINDOW'),
                 assessment('NCT99000007', 'machine_provisional', 'INSUFFICIENT_EVIDENCE', 'ALPHA_NOT_DETERMINABLE')]
    return {'format': 'civicsample-outcomes-assessment-export/1', 'display_mode': mode,
            'generated_at': '2026-11-09T12:00:00+00:00', 'producer': {'repository': 'fictional', 'commit': 'c' * 40},
            'method_version': '0.3.0', 'rules_sha256': 'f' * 64, 'methods_spec_sha256': 'e' * 64, 'cutoff': '2026-11-09',
            'collections': {'audit': {'definition': 'Fictional development collection.', 'nct_ids': audit,
                                      'eligible_nct_ids': [n for n in audit if n != 'NCT99000005']},
                            'human_reviewed': {'definition': 'Fictional.', 'nct_ids': ['NCT99000001']}},
            'assessments': rows}


RULES_CSV = ('canonical,rule_type,pattern,status,shared,note\n'
             'Fictional Pharma,exact_normalized,fictional pharma,attribute,no,fictional rule for tests\n')
