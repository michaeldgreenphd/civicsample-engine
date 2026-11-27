"""Synthetic fictional CT.gov v2, Europe PMC, NCBI PubMed, and openFDA fixtures (`NCT0999...`).

Every trial identifier (`NCT09990001`..`NCT09990008`), PubMed ID (`990000xx`), FDA application
(`NDA9900xx`/`BLA9900xx`, and the generic `ANDA99xxxx` of the flooded query), drug name (`zorbatinib`,
`velorafenib`), and excerpt here is fictional.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from disclosure import acquire as A

AS_OF = '2026-11-01'
GENERATED_AT = '2026-11-01T12:00:00+00:00'

NCT_IDS = [
    'NCT09990001',
    'NCT09990002',
    'NCT09990003',
    'NCT09990004',
    'NCT09990005',
    'NCT09990006',
    'NCT09990007',
    'NCT09990008',
]


def _make_study(
    nct_id: str,
    *,
    title: str,
    lead_sponsor: str,
    lead_class: str = 'INDUSTRY',
    collaborators: list[tuple[str, str]] | None = None,
    drugs: list[str] | None = None,
    overall_status: str = 'COMPLETED',
    pcd: tuple[str, str] | None = ('2020-01-15', 'ACTUAL'),
    cd: tuple[str, str] | None = ('2020-03-01', 'ACTUAL'),
    submit_date: str | None = None,
    submit_qc_date: str | None = None,
    post_date: tuple[str, str] | None = None,
    has_results: bool = False,
    empty_results_shell: bool = False,
    submission_infos: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    brief_summary: str = 'Fictional randomized clinical study.',
) -> dict[str, Any]:
    sm: dict[str, Any] = {
        'overallStatus': overall_status,
        'startDateStruct': {'date': '2019-01-10', 'type': 'ACTUAL'},
    }
    if pcd is not None:
        sm['primaryCompletionDateStruct'] = {'date': pcd[0], 'type': pcd[1]}
    if cd is not None:
        sm['completionDateStruct'] = {'date': cd[0], 'type': cd[1]}
    if submit_date is not None:
        sm['resultsFirstSubmitDate'] = submit_date
    if submit_qc_date is not None:
        sm['resultsFirstSubmitQcDate'] = submit_qc_date
    if post_date is not None:
        sm['resultsFirstPostDateStruct'] = {'date': post_date[0], 'type': post_date[1]}

    interventions = [{'type': 'DRUG', 'name': d} for d in (drugs or [])]
    interventions.append({'type': 'DRUG', 'name': 'Matching Placebo'})

    ps: dict[str, Any] = {
        'identificationModule': {
            'nctId': nct_id,
            'briefTitle': title,
            'officialTitle': f'{title} (Official Protocol)',
        },
        'statusModule': sm,
        'sponsorCollaboratorsModule': {
            'leadSponsor': {'name': lead_sponsor, 'class': lead_class},
            'collaborators': [{'name': n, 'class': c} for n, c in (collaborators or [])],
        },
        'descriptionModule': {'briefSummary': brief_summary},
        'conditionsModule': {'conditions': ['Fictional Metabolic Syndrome']},
        'designModule': {
            'studyType': 'INTERVENTIONAL',
            'phases': ['PHASE3'],
            'enrollmentInfo': {'count': 240, 'type': 'ACTUAL'},
        },
        'armsInterventionsModule': {'interventions': interventions},
        'oversightModule': {'isFdaRegulatedDrug': True},
    }
    if references is not None:
        ps['referencesModule'] = {'references': references}

    study: dict[str, Any] = {
        'protocolSection': ps,
        'hasResults': has_results,
    }
    if submission_infos is not None:
        # Where CT.gov API v2 records QC review cycles (timeline.SUBMISSION_INFOS_PATH).
        study['derivedSection'] = {'miscInfoModule': {'submissionTracking': {'submissionInfos': submission_infos}}}
    if has_results and not empty_results_shell:
        study['resultsSection'] = {
            'participantFlowModule': {'groups': [{'id': 'FG000', 'title': 'Active'}]},
            'outcomeMeasuresModule': {'outcomeMeasures': [{'type': 'PRIMARY', 'title': 'HbA1c change'}]},
        }
    elif empty_results_shell:
        study['resultsSection'] = {}
    return study


STUDIES: dict[str, dict[str, Any]] = {
    # 1. Concordant multi-source disclosed + POSTED_WITHIN_365D + exact QC lag + submissionInfos + Wyeth->Pfizer
    'NCT09990001': _make_study(
        'NCT09990001',
        title='Phase 3 trial of zorbatinib in adults',
        lead_sponsor='Wyeth is now a wholly owned subsidiary of Pfizer',
        drugs=['Zorbatinib 10 mg'],
        pcd=('2020-01-15', 'ACTUAL'),
        submit_date='2020-08-10',
        submit_qc_date='2020-09-20',
        post_date=('2020-10-05', 'ACTUAL'),
        has_results=True,
        submission_infos=[
            {'releaseDate': '2020-08-10', 'unreleaseDate': '2020-08-28', 'resetDate': '2020-09-10', 'mcpReleaseN': 1},
            {'releaseDate': '2020-09-20', 'mcpReleaseN': 2},
        ],
        references=[
            {
                'pmid': '99000001',
                'type': 'RESULT',
                'citation': 'Doe J et al. Fictional zorbatinib phase 3 results. J Fict Med. 2021.',
            },
            {
                'pmid': '99000002',
                'type': 'BACKGROUND',
                'citation': 'Smith A et al. Mechanism of zorbatinib. J Fict Biol. 2018.',
            },
        ],
        brief_summary='Pivotal efficacy trial of zorbatinib; primary endpoint met at Week 24.',
    ),
    # 2. Published results while CT.gov unreported + UNREPORTED_PAST_365D + shared=yes BMS & Pfizer partnership
    'NCT09990002': _make_study(
        'NCT09990002',
        title='Phase 3 study of velorafenib monotherapy',
        lead_sponsor='Bristol-Meyers Squibb & Pfizer',
        drugs=['Velorafenib'],
        pcd=('2021-03-01', 'ACTUAL'),
        has_results=False,
        references=[
            {
                'pmid': '99000003',
                'type': 'RESULT',
                'citation': 'Roe R et al. Velorafenib phase 3 efficacy. Lancet Fict. 2022 Jun.',
            },
        ],
    ),
    # 3. FDA-cited while CT.gov unreported + PENDING_SUBMITTED_UNPOSTED
    'NCT09990003': _make_study(
        'NCT09990003',
        title='Pivotal study of neximab injection',
        lead_sponsor='Pfizer Inc.',
        drugs=['Neximab'],
        pcd=('2022-05-10', 'ACTUAL'),
        submit_date='2023-04-01',
        has_results=False,
    ),
    # 4. CT.gov posted, no linked results pub + POSTED_AFTER_365D + reviewed_excluded sponsor
    'NCT09990004': _make_study(
        'NCT09990004',
        title='Investigator study of metabolic regimen',
        lead_sponsor='Wake Forest University Health Sciences',
        lead_class='OTHER',
        drugs=['Metformix-Fictional'],
        pcd=('2020-02', 'ACTUAL'),
        submit_date='2021-05-01',
        post_date=('2021-06-10', 'ACTUAL'),
        has_results=True,
    ),
    # 5. Unreported across completed routes + UNREPORTED_WITHIN_365D + unreviewed sponsor
    'NCT09990005': _make_study(
        'NCT09990005',
        title='Recent phase 3 study of talobrutinib',
        lead_sponsor='Fictional BioTherapeutics GmbH',
        drugs=['Talobrutinib'],
        pcd=('2026-06-01', 'ACTUAL'),
        has_results=False,
    ),
    # 6. DATE_PRECISION_INDETERMINATE (2025 vs 2026-11-01) + EPMC mention + heuristic openFDA sponsor+drug candidate
    'NCT09990006': _make_study(
        'NCT09990006',
        title='Study of lumirafenib in refractory patients',
        lead_sponsor='Pfizer',
        drugs=['Lumirafenib'],
        pcd=('2025', 'ACTUAL'),
        has_results=False,
    ),
    # 7. COMPLETION_DATE_MISSING_OR_FUTURE (ESTIMATED) + unresolved PMID + phantom EPMC + openFDA rule rejects
    #    (a flooded query of unrelated ANDAs, set aside by the ANDA rule; see `stub_get`)
    'NCT09990007': _make_study(
        'NCT09990007',
        title='Ongoing phase 3 study of generic-flooded compound',
        lead_sponsor='Fictional BioTherapeutics GmbH',
        overall_status='ACTIVE_NOT_RECRUITING',
        drugs=['Floodedgeneric'],
        pcd=('2027-05-01', 'ESTIMATED'),
        has_results=False,
        references=[
            {
                'pmid': '99999999',
                'type': 'RESULT',
                'citation': 'Unresolved fictional PMID 99999999.',
            },
        ],
    ),
    # NCT09990008 is intentionally absent from STUDIES (CT.gov returns 404; EPMC returns HTTP 500)
}

PUBMED_SUMMARIES: dict[str, dict[str, Any]] = {
    '99000001': {
        'uid': '99000001',
        'epubdate': '2021 Feb 10',
        'pubdate': '2021 Mar',
        'title': 'Fictional zorbatinib phase 3 results (NCT09990001).',
        'fulljournalname': 'Journal of Fictional Medicine',
    },
    '99000002': {
        'uid': '99000002',
        'pubdate': '2018 May',
        'title': 'Preclinical mechanism of zorbatinib.',
        'fulljournalname': 'Journal of Fictional Biology',
    },
    '99000003': {
        'uid': '99000003',
        'pubdate': '2022 Jun',
        'title': 'Velorafenib phase 3 efficacy trial (NCT09990002).',
        'fulljournalname': 'The Fictional Lancet',
    },
    '99999999': {
        'uid': '99999999',
        'error': 'cannot get document summary',
    },
}


def _json_resp(obj: Any, status: int = 200) -> A.Response:
    return status, {'Content-Type': 'application/json'}, json.dumps(obj).encode('utf-8'), None


def stub_get(calls: list[str] | None = None) -> A.Get:
    """Deterministic offline HTTP stub covering CT.gov v2, Europe PMC, PubMed, and openFDA."""

    def get(url: str, params: dict[str, Any] | None) -> A.Response:
        p = params or {}
        if calls is not None:
            calls.append(f'{url}?{json.dumps(p, sort_keys=True)}')

        if url == f'{A.CTGOV_API}/version':
            return _json_resp({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-01T08:00:00'})

        if url.startswith(f'{A.CTGOV_API}/studies/'):
            nct = url.rsplit('/', 1)[1]
            if nct in STUDIES:
                return _json_resp(STUDIES[nct])
            return _json_resp({'message': f'{nct} not found'}, 404)

        if url == A.EPMC_REST:
            q = str(p.get('query') or '').strip('"')
            if q == 'NCT09990001':
                return _json_resp({
                    'hitCount': 1,
                    'resultList': {'result': [{
                        'id': '99000001',
                        'pmid': '99000001',
                        'title': 'Fictional zorbatinib phase 3 results',
                        'abstractText': 'Primary results of ClinicalTrials.gov NCT09990001.',
                        'firstPublicationDate': '2021-02-10',
                    }]},
                })
            if q == 'NCT09990006':
                return _json_resp({
                    'hitCount': 1,
                    'resultList': {'result': [{
                        'id': 'EPMC9906',
                        'pmcid': 'PMC9900006',
                        'title': 'Study protocol and design for lumirafenib NCT09990006',
                        'abstractText': 'Design of trial NCT09990006 in adults.',
                        'pubYear': '2025',
                    }]},
                })
            if q == 'NCT09990007':
                # Phantom full-text hit where exact NCT09990007 token is absent from returned fields
                return _json_resp({
                    'hitCount': 1,
                    'resultList': {'result': [{
                        'id': 'EPMC9907',
                        'doi': '10.9999/fictional.9907',
                        'title': 'Review of metabolic pathways without trial identifier',
                        'abstractText': 'Mentions NCT099900079999 typo only, not the trial ID.',
                        'firstPublicationDate': '2024-04-01',
                    }]},
                })
            if q == 'NCT09990008':
                return _json_resp({'error': 'upstream gateway failure'}, 500)
            return _json_resp({'hitCount': 0, 'resultList': {'result': []}})

        if url == A.NCBI_EUTILS:
            pmid = str(p.get('id') or '')
            entry = PUBMED_SUMMARIES.get(pmid)
            if entry is None:
                return _json_resp({'error': 'not found'}, 404)
            return _json_resp({'result': {'uids': [pmid] if 'error' not in entry else [], pmid: entry}})

        if url == A.OPENFDA_DRUGSFDA:
            search = str(p.get('search') or '')
            if '"NCT09990001"' in search:
                return _json_resp({
                    'meta': {'results': {'total': 1}},
                    'results': [{
                        'application_number': 'NDA990001',
                        'sponsor_name': 'PFIZER INC',
                        'products': [{'brand_name': 'ZORBATINIB'}],
                        'submissions': [{
                            'submission_type': 'ORIG',
                            'submission_number': '1',
                            'submission_status': 'AP',
                            'submission_status_date': '20210620',
                            'review_priority': 'STANDARD',
                            'submission_class_code_description': 'Pivotal study NCT09990001 clinical review',
                        }],
                    }],
                })
            if '"NCT09990003"' in search:
                return _json_resp({
                    'meta': {'results': {'total': 1}},
                    'results': [{
                        'application_number': 'BLA990002',
                        'sponsor_name': 'PFIZER INC',
                        'products': [{'brand_name': 'NEXIMAB'}],
                        'submissions': [{
                            'submission_type': 'ORIG',
                            'submission_number': '1',
                            'submission_status': 'AP',
                            'submission_status_date': '20230915',
                            'submission_public_notes': ' Efficacy established in trial NCT09990003.',
                        }],
                    }],
                })
            if '"lumirafenib"' in search:
                # Sponsor + drug candidate without NCT ID in record -> candidate_awaiting_review (`not_validated`)
                return _json_resp({
                    'meta': {'results': {'total': 1}},
                    'results': [{
                        'application_number': 'NDA990006',
                        'sponsor_name': 'PFIZER INC',
                        'products': [{'brand_name': 'LUMIRAFENIB'}],
                        'submissions': [{
                            'submission_type': 'ORIG',
                            'submission_number': '1',
                            'submission_status': 'AP',
                            'submission_status_date': '20260110',
                        }],
                    }],
                })
            if '"floodedgeneric"' in search:
                # Flooded common generic (>= 25 total) with non-matching sponsor -> `rejected_by_rule`.
                # The stub answers on the drug term and does not apply the request's NDA/BLA clause
                # (`A.OPENFDA_INNOVATOR_APPLICATIONS`), so these ANDAs stand for a response that still
                # carries generic applications, as one would if openFDA stopped honouring the filter.
                # Each is set aside by the ANDA rule (`evidence.is_abbreviated_application`), which
                # runs ahead of the flood rule; the flood rule on NDA/BLA results is pinned by
                # `test_guard_flooded_nda_without_sponsor_agreement_keeps_the_flood_rule`.
                return _json_resp({
                    'meta': {'results': {'total': 30}},
                    'results': [{
                        'application_number': f'ANDA99{i:04d}',
                        'sponsor_name': 'UNRELATED GENERICS CORP',
                        'products': [{'brand_name': 'FLOODEDGENERIC'}],
                        'submissions': [{
                            'submission_type': 'ORIG',
                            'submission_number': '1',
                            'submission_status': 'AP',
                            'submission_status_date': '20190101',
                        }],
                    } for i in range(30)],
                })
            return _json_resp({'error': {'code': 'NOT_FOUND', 'message': 'No matches found!'}}, 404)

        raise AssertionError(f'unexpected stub URL: {url} ({p})')

    return get


def acquire_fixture_cache(cache_dir: Path, get: A.Get | None = None) -> A.FrozenCache:
    """Run all acquisition stages against `stub_get()` and return verified `FrozenCache`."""
    transport = get or stub_get()
    throttle = A.Throttle(min_interval=0.55, sleep=lambda _: None)
    ct_log = A.ResponseLog(A.raw_log_path(cache_dir, 'ctgov'))
    lit_log = A.ResponseLog(A.raw_log_path(cache_dir, 'literature'))
    fda_log = A.ResponseLog(A.raw_log_path(cache_dir, 'openfda'))

    A.fetch_ctgov_studies(NCT_IDS, ct_log, transport, throttle, run_stamp='2026-11-01')
    studies_by_nct = A.studies_from_ctgov_records(ct_log.records, NCT_IDS)
    A.fetch_literature(NCT_IDS, studies_by_nct, lit_log, transport, throttle)
    A.fetch_openfda(NCT_IDS, studies_by_nct, fda_log, transport, throttle)
    A.freeze_cache(cache_dir, NCT_IDS, frozen_at=GENERATED_AT)
    return A.load_frozen_cache(cache_dir)
