"""Search coverage: for every submission, every route and source, what was done and what came of it.

"Searched / not searched" hides the difference between never looking, looking at the wrong
document, being blocked, and looking properly and finding nothing. Each (submission, route) cell
therefore holds exactly one of seven states, with a reason in words:

  not_searched                     nothing was attempted on this route
  search_incomplete_or_blocked     attempted, but a request failed, was refused, was cut off by a
                                   page limit, or a step has not run yet; the answer is not known
  document_unavailable             the source answered and holds no document (or record) for this
                                   submission
  available_document_insufficient  something was retrieved, but it cannot answer the question: a
                                   granting letter, a Statement, a scan without text, or a metadata
                                   record that carries no study identifier
  candidate_awaiting_review        the route produced a candidate link that no machine check
                                   confirms and no person has decided
  no_candidate_in_completed_scope  the route ran to completion on material able to answer, and
                                   produced nothing. The scope is the route's, not the world's
  verified_link                    the route produced an identifier-confirmed link (links.py). This
                                   is the automated status; whether a person has reviewed it is
                                   reported beside it, never merged into it

Only `no_candidate_in_completed_scope` is a negative finding. The first four are different ways of
not knowing, and none of them is ever counted as "no study".
"""
from __future__ import annotations

from typing import Any

STATES = ('not_searched', 'search_incomplete_or_blocked', 'document_unavailable',
          'available_document_insufficient', 'candidate_awaiting_review', 'no_candidate_in_completed_scope',
          'verified_link')
ROUTES: dict[str, str] = {
    'local_document': 'PDFs an earlier fetch left on disk (data/pilot_summary_statements)',
    'fda_document_fetch': 'FDA release-database page and the documents it links (www.accessdata.fda.gov)',
    'openfda_metadata': 'openFDA device/510k.json and device/pma.json (api.fda.gov)',
    'nct_citation_scan': 'text of every document held for the submission, scanned for NCT ids',
    'registry_submission_number_search': 'ClinicalTrials.gov full-text search for the submission number',
    'registry_name_organisation_search': 'ClinicalTrials.gov intervention, phrase and sponsor searches',
    'demographic_extraction': 'pilot LLM extraction of demographic fields from a document',
}
# Overall status of a submission: the first of these that any ANSWERING route reached. A positive
# result on one route is not hidden by ignorance on another; "no candidate" is claimed only when
# no answering route is left in a state of not knowing.
# The answering routes are the three that can produce a link. The acquisition routes (local
# document, FDA document fetch, openFDA metadata) feed the citation scan and are not counted a
# second time: a document missing from local disk but fetched from FDA is not "unavailable", and
# openFDA, which carries no study identifier, would otherwise hold every submission at
# "insufficient". demographic_extraction is not a linkage route at all.
OVERALL_PRECEDENCE = ('verified_link', 'candidate_awaiting_review', 'search_incomplete_or_blocked', 'not_searched',
                      'available_document_insufficient', 'document_unavailable', 'no_candidate_in_completed_scope')
LINKAGE_ROUTES = ('nct_citation_scan', 'registry_submission_number_search', 'registry_name_organisation_search')
# Outcomes that are no answer about the document: asked again on the next acquisition, and a
# route holding one is incomplete. `not_a_pdf` is an HTML page served in place of the file.
TRANSIENT = ('http_error', 'network_error', 'not_a_pdf', 'skipped_disk_budget')


def _cell(route: str, state: str, reason: str, **detail: Any) -> dict[str, Any]:
    assert state in STATES and reason
    return {'route': route, 'source': ROUTES[route], 'state': state, 'reason': reason, 'detail': detail}


def _link_outcome(links: list[dict[str, Any]]) -> tuple[str | None, str]:
    """verified_link / candidate_awaiting_review / None from a route's links, honouring review:
    a link a person rejected is neither; one a person accepted or left unresolved stays visible."""
    live = [x for x in links if x['human_review'] != 'rejected' and x['automated_validation'] != 'rejected_by_rule']
    confirmed = [x for x in live if x['automated_validation'] == 'identifier_confirmed']
    if confirmed:
        reviewed = sum(1 for x in confirmed if x['human_review'] != 'unreviewed')
        return 'verified_link', (f'{len(confirmed)} identifier-confirmed evidence record(s) to '
                                 f"{len({x['target']['id'] for x in confirmed})} study(ies); "
                                 f'{reviewed} reviewed by a person')
    if live:
        return 'candidate_awaiting_review', (f'{len(live)} candidate record(s) to '
                                             f"{len({x['target']['id'] for x in live})} study(ies), none "
                                             f'identifier-confirmed')
    return None, ''


def _documents_cell(route: str, docs: list[dict[str, Any]], links: list[dict[str, Any]],
                    nothing: tuple[str, str]) -> dict[str, Any]:
    """The state a set of documents puts a route in. `nothing` is (state, reason) for no documents."""
    held = [d for d in docs if d['availability'] == 'retrieved']
    detail = {'n_documents_tried': len(docs), 'n_retrieved': len(held),
              'document_types': sorted({d['document_type'] for d in held if d['document_type']}),
              'not_retrieved': [{'url': d['url'], 'outcome': d['availability'], 'http_status': d['http_status']}
                                for d in docs if d['availability'] != 'retrieved']}
    if not docs:
        return _cell(route, nothing[0], nothing[1], **detail)
    doc_ids = {d['document_id'] for d in held}
    state, why = _link_outcome([x for x in links if x['evidence']['source_id'] in doc_ids])
    if state:
        return _cell(route, state, why, **detail)
    transient = [d for d in docs if d['availability'] in TRANSIENT]
    unread = [d for d in held if (d['text'] or {}).get('status') in ('not_extracted', 'failed')]
    if transient or unread:
        budget = sum(1 for d in transient if d['availability'] == 'skipped_disk_budget')
        return _cell(route, 'search_incomplete_or_blocked',
                     f'{len(transient) - budget} document request(s) failed without an answer, {budget} were not '
                     f'requested because the document store reached its disk budget, and {len(unread)} retrieved '
                     f'document(s) could not be read', **detail)
    if not held:
        return _cell(route, 'document_unavailable',
                     f'the source answered for {len(docs)} address(es) and holds no document at any of them', **detail)
    substantive = [d for d in held if d['demographic_summary_candidate']]
    if substantive:
        return _cell(route, 'no_candidate_in_completed_scope',
                     f'{len(substantive)} substantive document(s) read ({", ".join(detail["document_types"])}); '
                     f'no registry identifier in the text', **detail)
    reasons = sorted({d['demographic_summary_reason'] for d in held})
    return _cell(route, 'available_document_insufficient',
                 f'{len(held)} document(s) held, none able to answer: ' + '; '.join(reasons), **detail)


def local_document(docs: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    local = [d for d in docs if d['origin'] == 'local_recovered']
    return _documents_cell('local_document', local, links,
                           ('document_unavailable', 'no file for this submission among the PDFs held on disk'))


def fda_document_fetch(page: dict[str, Any] | None, docs: list[dict[str, Any]],
                       links: list[dict[str, Any]]) -> dict[str, Any]:
    """The database page is how FDA says which documents exist. Without its answer, addresses
    tried by pattern that return 404 do not show that no document exists, so the route is
    incomplete unless a document was nevertheless retrieved and yielded a link."""
    fetched = [d for d in docs if d['origin'] == 'fda_fetch']
    page_ok = page is not None and page.get('status') == 200
    if page is None and not fetched:
        return _cell('fda_document_fetch', 'not_searched', 'neither the database page nor any document was requested')
    cell = _documents_cell('fda_document_fetch', fetched, links,
                           ('document_unavailable', 'the database page lists no document and no address was tried'))
    if not page_ok and cell['state'] not in ('verified_link', 'candidate_awaiting_review'):
        cell['state'] = 'search_incomplete_or_blocked'
        how = ('answered with a page that does not name it' if (page or {}).get('status') == 'not_the_record'
               else 'not answered')
        cell['reason'] = (f'the FDA database page for this number was {how}, so the documents FDA lists are '
                          'not known; ' + cell['reason'])
    cell['detail']['database_page_status'] = (page or {}).get('status')
    cell['detail']['database_page_links'] = len(((page or {}).get('body') or {}).get('document_links') or [])
    return cell


def openfda_metadata(found: dict[str, Any] | None) -> dict[str, Any]:
    if found is None:
        return _cell('openfda_metadata', 'not_searched', 'openFDA was not asked about this number')
    if found['state'] == 'found':
        return _cell('openfda_metadata', 'available_document_insufficient',
                     'official record retrieved and stored on the submission; openFDA exposes no study identifier '
                     'and no predicate, so it cannot answer the linkage question')
    if found['state'] == 'no_record':
        return _cell('openfda_metadata', 'document_unavailable', found['reason'])
    return _cell('openfda_metadata', 'search_incomplete_or_blocked', found['reason'])


def nct_citation_scan(docs: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    own = [d for d in docs if d['scope'] == 'this_submission']
    cell = _documents_cell('nct_citation_scan', own, links,
                           ('not_searched', 'no document was sought for this submission, so nothing was scanned'))
    held = [d for d in own if d['availability'] == 'retrieved']
    unread = sum(((d['text'] or {}).get('n_pages') or 0) - ((d['text'] or {}).get('n_pages_with_text') or 0) for d in held)
    cell['detail']['n_pages_without_usable_text'] = unread
    if unread and cell['state'] == 'no_candidate_in_completed_scope':
        cell['reason'] += f'; {unread} page(s) of the documents held have no usable text layer and were not read'
    cell['detail']['n_documents_scanned'] = sum(1 for d in held if (d['text'] or {}).get('status') in ('text', 'partial'))
    cell['detail']['n_documents_without_text'] = sum(1 for d in held if (d['text'] or {}).get('status') == 'image_only')
    cell['detail']['n_nct_ids_found'] = len({i['identifier'] for d in held for i in d['identifiers'] if i['kind'] == 'nct'})
    cell['detail']['study_description'] = sorted({d['study_description'] for d in held if d['study_description']})
    return cell


def registry_submission_number_search(result: dict[str, Any] | None, links: list[dict[str, Any]]) -> dict[str, Any]:
    route = 'registry_submission_number_search'
    if result is None:
        return _cell(route, 'not_searched', 'the registry was not searched for this number')
    if result['state'] != 'completed':
        return _cell(route, 'search_incomplete_or_blocked', result['reason'])
    state, why = _link_outcome(links)
    detail = {'n_search_hits': result['n_search_hits'], 'searched_at': result.get('searched_at'),
              'n_hits_rejected_by_rule': sum(1 for x in links if x['automated_validation'] == 'rejected_by_rule')}
    if state:
        return _cell(route, state, why, **detail)
    if result['n_search_hits']:
        return _cell(route, 'no_candidate_in_completed_scope',
                     f"{result['n_search_hits']} record(s) returned by full-text search, none carrying the number "
                     f'in a retrieved field', **detail)
    return _cell(route, 'no_candidate_in_completed_scope', 'full-text search for the number returned no record', **detail)


def registry_name_organisation_search(search: dict[str, Any] | None, links: list[dict[str, Any]],
                                      tallies: dict[str, int] | None = None) -> dict[str, Any]:
    """`search` is registry.search_state() over the family's queries plus `n_name_queries`;
    `tallies` counts what was returned and set aside (sponsor-only studies and the like), so a
    reviewer can see it was looked at."""
    route = 'registry_name_organisation_search'
    detail = {**(tallies or {}), 'queries': (search or {}).get('queries', [])}
    if search is None or search['n_queries'] == 0:
        return _cell(route, 'not_searched', 'no query was made for this submission')
    state, why = _link_outcome(links)
    if state:
        return _cell(route, state, why + ' (family level: shared by every submission of the family)', **detail)
    set_aside = sum(1 for x in links if x['automated_validation'] == 'rejected_by_rule')
    detail['n_matches_rejected_by_rule'] = set_aside
    if search['n_missing'] or search['n_failed']:
        return _cell(route, 'search_incomplete_or_blocked',
                     f"{search['n_missing']} query(ies) without an answer, {search['n_failed']} answered with an error",
                     **detail)
    if search.get('n_name_queries') == 0:
        return _cell(route, 'not_searched',
                     'the listed name is made only of device-type words, so no name query is possible; the sponsor '
                     'query ran, and a sponsor match alone is not evidence about a device', **detail)
    if search['n_truncated']:
        return _cell(route, 'search_incomplete_or_blocked',
                     f"{search['n_truncated']} query(ies) matched more records than were returned and read; no "
                     f'candidate among those read ({set_aside} common-word match(es) rejected by rule)', **detail)
    if set_aside:
        return _cell(route, 'no_candidate_in_completed_scope',
                     f"{search['n_queries']} query(ies) answered in full; {set_aside} name-only match(es) on a name that "
                     f'floods the registry were rejected by rule and are kept as records; nothing else names the device',
                     **detail)
    return _cell(route, 'no_candidate_in_completed_scope',
                 f"{search['n_queries']} query(ies) answered in full; no returned study names the device in a title "
                 f'or intervention', **detail)


def demographic_extraction(observations: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Not a linkage route: whether demographic values have been extracted from a document of this
    submission. Only ever not_searched, insufficient, or completed-with-nothing here; the build
    runs no extraction of its own."""
    route = 'demographic_extraction'
    candidates = [d['document_id'] for d in docs if d['demographic_summary_candidate']]
    if not observations:
        return _cell(route, 'not_searched', 'no extraction has been run on any document of this submission',
                     n_demographic_summary_candidates_held=len(candidates))
    types = sorted({o['document_type'] or 'unknown' for o in observations})
    reported = sum(o['n_demographic_fields_reported'] for o in observations)
    if all(not o['document_is_demographic_summary_candidate'] for o in observations):
        return _cell(route, 'available_document_insufficient',
                     f'the pilot extraction read a {", ".join(types)}, which describes no study population; '
                     f'{reported} demographic value(s) reported', n_demographic_summary_candidates_held=len(candidates),
                     n_extractions=len(observations))
    return _cell(route, 'no_candidate_in_completed_scope' if not reported else 'candidate_awaiting_review',
                 f'{reported} demographic value(s) extracted from {", ".join(types)}; population identity unresolved',
                 n_demographic_summary_candidates_held=len(candidates), n_extractions=len(observations))


def overall(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """One status per submission over the linkage routes, by OVERALL_PRECEDENCE, with the routes
    that put it there."""
    linkage = [c for c in cells if c['route'] in LINKAGE_ROUTES]
    for state in OVERALL_PRECEDENCE:
        routes = [c['route'] for c in linkage if c['state'] == state]
        if routes:
            return {'state': state, 'routes': routes}
    return {'state': 'not_searched', 'routes': []}
