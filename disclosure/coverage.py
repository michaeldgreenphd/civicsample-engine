"""Per-trial, 5-route, 7-state search coverage matrix.

Following `devices/coverage.py`, "not searched" is strictly separated from "none found":
each `(nct_id, route)` cell takes exactly one of seven states with a human-readable `reason`:

  not_searched                     route was not attempted for this trial
  search_incomplete_or_blocked     attempted, but a request failed, lacked a valid answer, or was
                                   truncated by a page cap without yielding a live link
  document_unavailable             upstream source answered HTTP 404 (record does not exist)
  available_document_insufficient  record/citation was retrieved, but cannot establish trial
                                   results disclosure (e.g., malformed resultsSection, background-
                                   only citations, or unresolved PMIDs)
  candidate_awaiting_review        route produced a candidate link that is not identifier-confirmed
                                   (or awaits human confirmation)
  no_candidate_in_completed_scope  route ran to completion with full pagination and produced zero
                                   live candidates (the ONLY negative finding)
  verified_link                    route produced an identifier-confirmed link (or quote-confirmed
                                   human-verified statement) not rejected by human review
"""
from __future__ import annotations

from typing import Any

from disclosure import acquire as A

ROUTES: dict[str, str] = {
    'ctgov_results': 'ClinicalTrials.gov study record resultsSection (/api/v2/studies/{nct_id})',
    'registry_references': 'ClinicalTrials.gov protocolSection.referencesModule + NCBI PubMed esummary',
    'literature_search': 'Europe PMC REST full-text and metadata citation search by NCT ID',
    'openfda_drugsfda': 'openFDA Drugs@FDA (/drug/drugsfda.json) NDA/BLA search by NCT ID and drug candidates',
    'documentary_statements': 'Curated documentary statements in disclosure/documentary_statements.csv',
}

STATES = (
    'not_searched',
    'search_incomplete_or_blocked',
    'document_unavailable',
    'available_document_insufficient',
    'candidate_awaiting_review',
    'no_candidate_in_completed_scope',
    'verified_link',
)

OVERALL_PRECEDENCE = (
    'verified_link',
    'candidate_awaiting_review',
    'search_incomplete_or_blocked',
    'not_searched',
    'available_document_insufficient',
    'document_unavailable',
    'no_candidate_in_completed_scope',
)

SEARCH_ROUTES = (
    'ctgov_results',
    'registry_references',
    'literature_search',
    'openfda_drugsfda',
)


def _cell(route: str, state: str, reason: str, **detail: Any) -> dict[str, Any]:
    if route not in ROUTES:
        raise ValueError(f'unknown route: {route!r}')
    if state not in STATES:
        raise ValueError(f'unknown coverage state: {state!r}')
    if not reason.strip():
        raise ValueError(f'{route}: coverage reason must be non-empty')
    return {
        'route': route,
        'source': ROUTES[route],
        'state': state,
        'reason': reason,
        'detail': detail,
    }


def _active_links(links: list[dict[str, Any]], *, treat_unreviewed_background_as_insufficient: bool = False) -> list[dict[str, Any]]:
    """Links that remain live candidates or verified links after rule and human review filters."""
    out: list[dict[str, Any]] = []
    for lnk in links:
        rs = lnk['review_status']
        av = lnk['automated_validation']
        if rs in ('rejected_false_positive', 'confirmed_background_only'):
            continue
        if av == 'identifier_not_resolved' and rs != 'confirmed_same_trial_results':
            continue
        if av == 'rejected_by_rule' and rs not in ('confirmed_same_trial_results', 'indeterminate'):
            continue
        if treat_unreviewed_background_as_insufficient and lnk['citation_type'] == 'BACKGROUND' and rs == 'unreviewed':
            continue
        out.append(lnk)
    return out


def _verified_links(live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live links that settle a route as `verified_link`.

    A link verifies its route when it is identifier-confirmed mechanically and not held
    `indeterminate` by review, or when a person has reviewed it as `confirmed_same_trial_results`
    whatever the mechanical check said (`_active_links` keeps such a link live). The second case
    matches `timeline._qualifying_results_publications` and `timeline._qualifying_fda_links`, which
    count every `confirmed_same_trial_results` link; reporting that link as `candidate_awaiting_review`
    would call a settled review pending while the timeline already relies on it.
    """
    return [
        x for x in live
        if x['review_status'] == 'confirmed_same_trial_results'
        or (x['automated_validation'] == 'identifier_confirmed' and x['review_status'] != 'indeterminate')
    ]


def _human_confirmed_only(verified: list[dict[str, Any]]) -> int:
    """How many verified links rest on human confirmation alone (no mechanical identifier match)."""
    return sum(1 for x in verified if x['automated_validation'] != 'identifier_confirmed')


def ctgov_results_cell(
    nct_id: str,
    ctgov_rec: dict[str, Any] | None,
    route_links: list[dict[str, Any]],
) -> dict[str, Any]:
    """Coverage state for `ctgov_results`."""
    route = 'ctgov_results'
    if ctgov_rec is None:
        return _cell(route, 'not_searched', f'ClinicalTrials.gov record for {nct_id} was not requested')
    status = ctgov_rec.get('status')
    if status == 404:
        return _cell(
            route,
            'document_unavailable',
            f'ClinicalTrials.gov answered HTTP 404; no study record exists for {nct_id}',
            http_status=404,
        )
    if status != 200 or not A.ctgov_study_valid(ctgov_rec.get('body')):
        return _cell(
            route,
            'search_incomplete_or_blocked',
            f"ClinicalTrials.gov study lookup did not return a valid 200 record (status={status!r})",
            http_status=ctgov_rec.get('http_status'),
        )

    body = ctgov_rec['body']
    has_results = body.get('hasResults')
    rs_mod = body.get('resultsSection')
    live = _active_links(route_links)
    # The ctgov_results link is generated only from the retrieved record's own posted resultsSection
    # (hasResults=true and non-empty), so it states a registry fact about this NCT ID. A human review
    # marking it `rejected_false_positive` or `confirmed_background_only` is recorded on the link but
    # cannot turn that fact into a negative; only `indeterminate` holds the route at awaiting review.
    confirmed = [
        x for x in route_links
        if x['automated_validation'] == 'identifier_confirmed' and x['review_status'] != 'indeterminate'
    ]
    if confirmed:
        reviewed = sum(1 for x in confirmed if x['review_status'] != 'unreviewed')
        overridden = sum(1 for x in confirmed if x['review_status'] in ('rejected_false_positive', 'confirmed_background_only'))
        note = (
            f'; {overridden} review(s) marking it rejected or background-only are recorded on the link '
            'but do not override the posted record'
            if overridden else ''
        )
        return _cell(
            route,
            'verified_link',
            f'retrieved study record carries a posted resultsSection ({reviewed}/{len(confirmed)} human-reviewed){note}',
            has_results=True,
            n_links=len(confirmed),
        )
    if live:
        return _cell(
            route,
            'candidate_awaiting_review',
            f'{len(live)} CT.gov results link(s) marked indeterminate or awaiting settled human review',
            has_results=bool(has_results),
            n_candidates=len(live),
        )
    if has_results is True and (not isinstance(rs_mod, dict) or not rs_mod):
        return _cell(
            route,
            'available_document_insufficient',
            'study record sets hasResults=true, but resultsSection is absent or empty',
            has_results=True,
        )
    # Only an explicit hasResults=false is the registry's own negative. A record that omits the field
    # or carries a non-boolean (a field-projected or hand-edited local record passes
    # `ctgov_study_valid`) says nothing about whether results were posted, so it is insufficient
    # evidence, never `no_candidate_in_completed_scope`, and cannot enable a "CT.gov unreported" state.
    if has_results is not False:
        return _cell(
            route,
            'available_document_insufficient',
            f'study record carries no boolean hasResults (got {has_results!r}); whether results were '
            'posted cannot be read from it',
            has_results=None,
        )
    return _cell(
        route,
        'no_candidate_in_completed_scope',
        'retrieved ClinicalTrials.gov study record has hasResults=false; no results section posted',
        has_results=False,
    )


def registry_references_cell(
    nct_id: str,
    ctgov_rec: dict[str, Any] | None,
    route_links: list[dict[str, Any]],
    literature_records: dict[str, dict[str, Any]],
    literature_acquired: bool,
) -> dict[str, Any]:
    """Coverage state for `registry_references` (`protocolSection.referencesModule`)."""
    route = 'registry_references'
    if ctgov_rec is None:
        return _cell(route, 'not_searched', f'ClinicalTrials.gov record for {nct_id} was not requested')
    status = ctgov_rec.get('status')
    if status == 404:
        return _cell(
            route,
            'document_unavailable',
            f'ClinicalTrials.gov answered HTTP 404; referencesModule unavailable for {nct_id}',
            http_status=404,
        )
    if status != 200 or not A.ctgov_study_valid(ctgov_rec.get('body')):
        return _cell(
            route,
            'search_incomplete_or_blocked',
            f"ClinicalTrials.gov study lookup did not return a valid 200 record (status={status!r})",
        )

    refs = A.extract_registry_references(ctgov_rec['body'])
    if not refs:
        return _cell(
            route,
            'no_candidate_in_completed_scope',
            'retrieved study record lists zero citations in protocolSection.referencesModule',
            n_references=0,
        )

    live = _active_links(route_links, treat_unreviewed_background_as_insufficient=True)
    confirmed = _verified_links(live)
    if confirmed:
        reviewed = sum(1 for x in confirmed if x['review_status'] != 'unreviewed')
        human_only = _human_confirmed_only(confirmed)
        return _cell(
            route,
            'verified_link',
            f'{len(confirmed) - human_only} non-background registry reference PMID(s) resolved on PubMed '
            f'and {human_only} confirmed by human review only ({reviewed} human-reviewed)',
            n_references=len(refs),
            n_verified_links=len(confirmed),
            n_human_confirmed_only=human_only,
        )
    if live:
        return _cell(
            route,
            'candidate_awaiting_review',
            f'{len(live)} registry reference candidate(s) present without PubMed identifier resolution or settled review',
            n_references=len(refs),
            n_candidates=len(live),
        )

    # Check if any PMID failed transiently when literature acquisition was attempted
    pmids = [r['pmid'] for r in refs if r['pmid'] and A.PMID_RE.match(r['pmid'])]
    if literature_acquired and pmids:
        failed_pmids = [
            p for p in pmids
            if (literature_records.get(f'literature/pubmed_pmid/{p}') or {}).get('status') not in (200, 404)
        ]
        if failed_pmids:
            return _cell(
                route,
                'search_incomplete_or_blocked',
                f'PubMed esummary lookup incomplete or errored for {len(failed_pmids)} cited PMID(s)',
                n_references=len(refs),
                failed_pmids=failed_pmids,
            )

    if route_links and all(x['review_status'] == 'rejected_false_positive' for x in route_links):
        return _cell(
            route,
            'no_candidate_in_completed_scope',
            f'all {len(route_links)} registry reference(s) were rejected as false positives by human review',
            n_references=len(refs),
        )

    return _cell(
        route,
        'available_document_insufficient',
        'referencesModule citations are all BACKGROUND/confirmed_background_only or carry unresolvable PMIDs',
        n_references=len(refs),
    )


def literature_search_cell(
    nct_id: str,
    literature_records: dict[str, dict[str, Any]],
    route_links: list[dict[str, Any]],
    literature_acquired: bool,
) -> dict[str, Any]:
    """Coverage state for `literature_search` (Europe PMC REST search by NCT ID)."""
    route = 'literature_search'
    epmc_rec = literature_records.get(f'literature/epmc_nct/{nct_id}')
    if not literature_acquired or epmc_rec is None:
        return _cell(route, 'not_searched', f'Europe PMC search was not run for {nct_id}')

    status = epmc_rec.get('status')
    body = epmc_rec.get('body')
    valid_200 = status == 200 and A.epmc_search_valid(body)

    live = _active_links(route_links)
    confirmed = _verified_links(live)
    if confirmed:
        reviewed = sum(1 for x in confirmed if x['review_status'] != 'unreviewed')
        human_only = _human_confirmed_only(confirmed)
        return _cell(
            route,
            'verified_link',
            f'{len(confirmed) - human_only} Europe PMC publication(s) carry an exact {nct_id} token and '
            f'{human_only} were confirmed by human review only ({reviewed} human-reviewed)',
            n_verified_links=len(confirmed),
            n_human_confirmed_only=human_only,
        )
    if live:
        return _cell(
            route,
            'candidate_awaiting_review',
            f'{len(live)} Europe PMC candidate link(s) awaiting human review',
            n_candidates=len(live),
        )

    if status == 404:
        return _cell(route, 'document_unavailable', f'Europe PMC endpoint answered HTTP 404 for {nct_id}')
    if not valid_200:
        return _cell(
            route,
            'search_incomplete_or_blocked',
            f'Europe PMC search for {nct_id} did not return a valid 200 response (status={status!r})',
            http_status=epmc_rec.get('http_status'),
        )

    hit_count = int(body['hitCount'])
    returned = len(body['resultList']['result'])
    if hit_count > returned:
        return _cell(
            route,
            'search_incomplete_or_blocked',
            f'Europe PMC matched {hit_count} records but only {returned} were retrieved on the page',
            hit_count=hit_count,
            n_returned=returned,
        )

    if route_links and any(x['review_status'] == 'confirmed_background_only' for x in route_links):
        return _cell(
            route,
            'available_document_insufficient',
            f'Europe PMC returned {len(route_links)} record(s), reviewed as background/protocol only',
            hit_count=hit_count,
        )

    n_human_rejected = sum(1 for x in route_links if x['review_status'] == 'rejected_false_positive')
    n_set_aside = sum(1 for x in route_links if x['automated_validation'] == 'rejected_by_rule')
    if n_human_rejected and not n_set_aside:
        msg = (
            f'Europe PMC search completed ({hit_count} hit(s)); all {n_human_rejected} candidate '
            'publication(s) were rejected as false positives by human review'
        )
    elif n_set_aside and n_human_rejected:
        msg = (
            f'Europe PMC search completed ({hit_count} hit(s)); {n_set_aside} hit(s) rejected by rule '
            f'and {n_human_rejected} rejected as false positives by human review'
        )
    elif n_set_aside:
        msg = (
            f'Europe PMC search completed ({hit_count} hit(s)); {n_set_aside} hit(s) lacked an exact {nct_id} '
            'token in retrieved fields and were rejected by rule'
        )
    else:
        msg = f'Europe PMC search completed with 0 publications mentioning {nct_id}'
    return _cell(
        route,
        'no_candidate_in_completed_scope',
        msg,
        hit_count=hit_count,
        n_rejected_by_rule=n_set_aside,
        n_rejected_by_human_review=n_human_rejected,
    )


def openfda_drugsfda_cell(
    nct_id: str,
    ctgov_rec: dict[str, Any] | None,
    study: dict[str, Any] | None,
    openfda_records: dict[str, dict[str, Any]],
    route_links: list[dict[str, Any]],
    openfda_acquired: bool,
) -> dict[str, Any]:
    """Coverage state for `openfda_drugsfda`."""
    route = 'openfda_drugsfda'
    nct_rec = openfda_records.get(f'openfda/nct/{nct_id}')
    if not openfda_acquired or nct_rec is None:
        return _cell(route, 'not_searched', f'openFDA Drugs@FDA was not searched for {nct_id}')

    live = _active_links(route_links)
    confirmed = _verified_links(live)
    if confirmed:
        reviewed = sum(1 for x in confirmed if x['review_status'] != 'unreviewed')
        human_only = _human_confirmed_only(confirmed)
        return _cell(
            route,
            'verified_link',
            f'{len(confirmed) - human_only} openFDA Drugs@FDA application record(s) carry an exact {nct_id} '
            f'citation and {human_only} were confirmed by human review only ({reviewed} human-reviewed)',
            n_verified_links=len(confirmed),
            n_human_confirmed_only=human_only,
        )
    if live:
        return _cell(
            route,
            'candidate_awaiting_review',
            f'{len(live)} candidate Drugs@FDA NDA/BLA record(s) matched by investigational drug term awaiting review',
            n_candidates=len(live),
        )

    # Check completeness of direct NCT query + planned drug queries
    drugs = A.extract_drug_candidates(study)
    queries_to_check: list[tuple[str, dict[str, Any] | None]] = [(f'openfda/nct/{nct_id}', nct_rec)]
    for d in drugs:
        queries_to_check.append((A.drug_query_key(d), openfda_records.get(A.drug_query_key(d))))

    incomplete_reasons: list[str] = []
    for qkey, qrec in queries_to_check:
        if qrec is None:
            incomplete_reasons.append(f'{qkey} was not answered')
            continue
        qstatus = qrec.get('status')
        if qstatus == 404:
            continue
        if qstatus != 200 or not A.openfda_drugsfda_valid(qrec.get('body')):
            incomplete_reasons.append(f'{qkey} failed with status={qstatus!r}')
            continue
        total = int(qrec['body']['meta']['results']['total'])
        returned = len(qrec['body']['results'])
        if total > returned:
            incomplete_reasons.append(f'{qkey} truncated ({returned}/{total} records returned)')

    if incomplete_reasons:
        return _cell(
            route,
            'search_incomplete_or_blocked',
            'openFDA Drugs@FDA search incomplete: ' + '; '.join(incomplete_reasons),
            planned_drugs=drugs,
        )

    if ctgov_rec is not None and ctgov_rec.get('status') == 404 and nct_rec.get('status') == 404:
        return _cell(
            route,
            'document_unavailable',
            f'CT.gov holds no record for {nct_id} (no drug interventions known) and openFDA NCT search returned 404',
        )

    if route_links and any(x['review_status'] == 'confirmed_background_only' for x in route_links):
        return _cell(
            route,
            'available_document_insufficient',
            'matched Drugs@FDA application record(s) were reviewed as background/unrelated indication only',
            planned_drugs=drugs,
        )

    n_human_rejected = sum(1 for x in route_links if x['review_status'] == 'rejected_false_positive')
    n_set_aside = sum(1 for x in route_links if x['automated_validation'] == 'rejected_by_rule')
    return _cell(
        route,
        'no_candidate_in_completed_scope',
        f'openFDA Drugs@FDA search completed across NCT ID and {len(drugs)} drug candidate(s) with '
        f'zero live candidates ({n_set_aside} rejected by rule, {n_human_rejected} rejected by human review)',
        planned_drugs=drugs,
        n_rejected_by_rule=n_set_aside,
        n_rejected_by_human_review=n_human_rejected,
    )


def documentary_statements_cell(nct_id: str, route_links: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage state for `documentary_statements` (`disclosure/documentary_statements.csv`)."""
    route = 'documentary_statements'
    if not route_links:
        return _cell(
            route,
            'not_searched',
            f'no documentary statement has been entered for {nct_id}; statements are entered by a person '
            'and absence of rows is not a negative finding',
            n_statements=0,
        )

    live = _active_links(route_links)
    verified = [
        x for x in live
        if x['automated_validation'] == 'quote_confirmed_in_source'
        and x['review_status'] == 'confirmed_same_trial_results'
    ]
    if verified:
        return _cell(
            route,
            'verified_link',
            f'{len(verified)} documentary statement(s) quote-confirmed in source and human-confirmed as trial results',
            n_statements=len(route_links),
            n_verified=len(verified),
        )
    if live:
        n_quote = sum(1 for x in live if x['automated_validation'] == 'quote_confirmed_in_source')
        return _cell(
            route,
            'candidate_awaiting_review',
            f'{len(live)} documentary statement(s) entered ({n_quote} quote-confirmed in source) awaiting human review',
            n_statements=len(route_links),
            n_quote_confirmed=n_quote,
        )
    if any(x['review_status'] == 'confirmed_background_only' for x in route_links):
        return _cell(
            route,
            'available_document_insufficient',
            'entered documentary statement(s) were reviewed as background-only rather than trial results',
            n_statements=len(route_links),
        )
    return _cell(
        route,
        'no_candidate_in_completed_scope',
        f'all {len(route_links)} documentary statement(s) for {nct_id} were rejected by human review',
        n_statements=len(route_links),
    )


def overall_coverage(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-trial overall coverage across search routes (+ active documentary statements)."""
    considered = [
        c for c in cells
        if c['route'] in SEARCH_ROUTES
        or (c['route'] == 'documentary_statements' and c['state'] in ('verified_link', 'candidate_awaiting_review'))
    ]
    for state in OVERALL_PRECEDENCE:
        matching_routes = [c['route'] for c in considered if c['state'] == state]
        if matching_routes:
            return {'state': state, 'routes': matching_routes}
    return {'state': 'not_searched', 'routes': []}


def trial_coverage(
    nct_id: str,
    *,
    ctgov_rec: dict[str, Any] | None,
    study: dict[str, Any] | None,
    literature_records: dict[str, dict[str, Any]],
    literature_acquired: bool,
    openfda_records: dict[str, dict[str, Any]],
    openfda_acquired: bool,
    links_by_route: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble the 5-route coverage record and overall precedence state for `nct_id`."""
    cells = [
        ctgov_results_cell(nct_id, ctgov_rec, links_by_route.get('ctgov_results', [])),
        registry_references_cell(
            nct_id,
            ctgov_rec,
            links_by_route.get('registry_references', []),
            literature_records,
            literature_acquired,
        ),
        literature_search_cell(
            nct_id,
            literature_records,
            links_by_route.get('literature_search', []),
            literature_acquired,
        ),
        openfda_drugsfda_cell(
            nct_id,
            ctgov_rec,
            study,
            openfda_records,
            links_by_route.get('openfda_drugsfda', []),
            openfda_acquired,
        ),
        documentary_statements_cell(nct_id, links_by_route.get('documentary_statements', [])),
    ]
    return {
        'nct_id': nct_id,
        'overall': overall_coverage(cells),
        'routes': cells,
    }
