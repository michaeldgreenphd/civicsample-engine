"""ClinicalTrials.gov lookups for the device inventory, with every response frozen.

Three questions are asked of the registry, and they are different routes with different strength:

  1. Does an NCT id cited in an FDA document exist?            lookup(), nct_state()
  2. Does any registry record cite one of our submission numbers?  number search, locate_identifier()
  3. Which records name a device and its manufacturer?         name search, name_evidence(),
                                                               organisation_evidence()

Routes 1 and 2 rest on an identifier and can be confirmed mechanically. Route 3 rests on strings
and can only ever propose a candidate, at family level (devices/links.py enforces that).

A search hit is never a link by itself. The registry's full-text search returns records that do
not contain the searched string in any field the API returns, and its intervention search is a
relevance ranking, not a filter. Every candidate therefore needs the matched text located in a
named field of the returned record.

Responses are logged append-only (devices/acquire.ResponseLog) and frozen by sha256; the build
reads only the frozen archive.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator

from devices import acquire as A
from devices import inventory as I
from devices import organizations as O

API = 'https://clinicaltrials.gov/api/v2'
NCT = re.compile(r'^NCT\d{8}$')
# CollaboratorName, not Collaborators: the API rejects the latter with HTTP 400.
SEARCH_FIELDS = ','.join([
    'NCTId', 'BriefTitle', 'OfficialTitle', 'LeadSponsorName', 'LeadSponsorClass', 'CollaboratorName',
    'InterventionType', 'InterventionName', 'InterventionOtherName', 'InterventionDescription', 'StartDate',
    'PrimaryCompletionDate', 'CompletionDate', 'StudyFirstPostDate', 'LastUpdatePostDate', 'OverallStatus',
    'StudyType', 'EnrollmentCount', 'EnrollmentType', 'HasResults'])
NUMBER_BATCH = 25
NAME_PAGE_SIZE = 25
SPONSOR_PAGE_SIZE = 100
# A search that matches more records than its page returned is asked once more for up to
# FULL_PAGE_SIZE records (the API's maximum page), so that everything it matched is read. A search
# matching more than that is not followed: it is a common word or a very large sponsor, and is
# reported as cut off.
FULL_PAGE_SIZE = 1000
NCT_STATES = ('resolves', 'not_found', 'resolved_to_different_id', 'lookup_failed', 'not_looked_up')
NAME_FIELDS = ('intervention_name', 'intervention_other_name', 'brief_title', 'official_title')


def _key(prefix: str, params: dict[str, Any]) -> str:
    return f"{prefix}/{hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]}"


def study_facts(study: dict[str, Any]) -> dict[str, Any]:
    """The registry fields the linkage reads, flattened. Dates stay the strings the registry
    holds (YYYY-MM-DD or YYYY-MM); precision is not invented."""
    ps = study.get('protocolSection') or {}
    idm, sm, dm = ps.get('identificationModule') or {}, ps.get('statusModule') or {}, ps.get('designModule') or {}
    sc = ps.get('sponsorCollaboratorsModule') or {}
    return {
        'nct_id': idm.get('nctId'), 'brief_title': idm.get('briefTitle') or '',
        'official_title': idm.get('officialTitle') or '',
        'lead_sponsor': (sc.get('leadSponsor') or {}).get('name') or '',
        'lead_sponsor_class': (sc.get('leadSponsor') or {}).get('class'),
        'collaborators': [c.get('name') or '' for c in sc.get('collaborators') or []],
        'interventions': [{'type': i.get('type'), 'name': i.get('name') or '', 'other_names': i.get('otherNames') or [],
                           'description': i.get('description') or ''}
                          for i in (ps.get('armsInterventionsModule') or {}).get('interventions') or []],
        'start_date': (sm.get('startDateStruct') or {}).get('date'),
        'primary_completion_date': (sm.get('primaryCompletionDateStruct') or {}).get('date'),
        'completion_date': (sm.get('completionDateStruct') or {}).get('date'),
        'first_posted_date': (sm.get('studyFirstPostDateStruct') or {}).get('date'),
        'last_update_posted_date': (sm.get('lastUpdatePostDateStruct') or {}).get('date'),
        'overall_status': sm.get('overallStatus'), 'study_type': dm.get('studyType'),
        'enrollment_count': (dm.get('enrollmentInfo') or {}).get('count'),
        'enrollment_type': (dm.get('enrollmentInfo') or {}).get('type'),
        'has_results': study.get('hasResults'),
    }


# ---------------------------------------------------------------- route 1: NCT ids exist

def fetch_version(log: A.ResponseLog, get: A.Get, throttle: A.Throttle, day: str) -> dict[str, Any] | None:
    """The API version and data timestamp, once per acquisition day: the registry's own version
    stamp for everything fetched alongside it."""
    return A.logged_json(log, get, throttle, f'ctgov/version/{day}', f'{API}/version')


def fetch_studies(nct_ids: list[str], log: A.ResponseLog, get: A.Get, throttle: A.Throttle) -> dict[str, int]:
    """One request per id to /studies/{id}: 200 with the record, or 404. Asked once."""
    answered = unanswered = 0
    for nct in sorted(set(nct_ids)):
        if not NCT.match(nct):
            raise ValueError(f'{nct!r} is not an NCT id')
        rec = A.logged_json(log, get, throttle, f'ctgov/study/{nct}', f'{API}/studies/{nct}',
                            {'fields': SEARCH_FIELDS}, tag={'nct_id': nct})
        answered, unanswered = answered + (rec is not None), unanswered + (rec is None)
    return {'lookups_answered': answered, 'lookups_without_answer': unanswered}


def nct_state(records: dict[str, dict[str, Any]], nct_id: str) -> tuple[str, dict[str, Any] | None]:
    """(state, study facts or None) for one cited id.

      resolves                  the registry returned a record whose own id is the cited id
      resolved_to_different_id  it returned a record under another id (an obsolete or alias id);
                                the cited string is not that record's identifier
      not_found                 the registry answered 404: no such record (a typo, or not an NCT id)
      lookup_failed             it answered with another error
      not_looked_up             no answer is held; nothing is assumed
    Only `resolves` supports an identifier-confirmed link.
    """
    rec = records.get(f'ctgov/study/{nct_id}')
    if rec is None:
        return 'not_looked_up', None
    if rec['status'] == 404:
        return 'not_found', None
    if rec['status'] != 200 or not isinstance(rec.get('body'), dict):
        return 'lookup_failed', None
    facts = study_facts(rec['body'])
    return ('resolves' if facts['nct_id'] == nct_id else 'resolved_to_different_id'), facts


# ---------------------------------------------------------------- route 2: records citing a submission number

def searchable_number(submission_number: str) -> str:
    """What is typed into the search box: the number, or the base PMA number of a supplement (the
    slash is query syntax). Whether the record cites the supplement itself is decided afterwards
    by locate_identifier()."""
    return submission_number.split('/')[0]


def number_batches(submission_numbers: list[str]) -> list[list[str]]:
    numbers = sorted({searchable_number(sn) for sn in submission_numbers})
    return [numbers[i:i + NUMBER_BATCH] for i in range(0, len(numbers), NUMBER_BATCH)]


def _batch_params(chunk: list[str]) -> dict[str, str]:
    return {'query.term': ' OR '.join(chunk), 'pageSize': '1', 'countTotal': 'true', 'fields': 'NCTId'}


def _single_params(number: str) -> dict[str, str]:
    return {'query.term': number, 'pageSize': '100', 'countTotal': 'true', 'fields': 'NCTId'}


def fetch_number_search(submission_numbers: list[str], log: A.ResponseLog, get: A.Get,
                        throttle: A.Throttle) -> dict[str, int]:
    """Full-text search for every number: NUMBER_BATCH numbers joined with OR per request; a batch
    with any hit is asked again one number at a time; every hit record is then fetched whole so
    the string can be located in a named field."""
    asked = unanswered = 0
    for chunk in number_batches(submission_numbers):
        p = _batch_params(chunk)
        rec = A.logged_json(log, get, throttle, _key('ctgov/number_batch', p), f'{API}/studies', p,
                            tag={'numbers': chunk})
        asked += 1
        if rec is None:
            unanswered += 1
            continue
        if rec['status'] != 200 or not (rec['body'] or {}).get('totalCount'):
            continue
        for number in chunk:
            p1 = _single_params(number)
            one = A.logged_json(log, get, throttle, _key('ctgov/number', p1), f'{API}/studies', p1,
                                tag={'number': number})
            asked += 1
            if one is None:
                unanswered += 1
                continue
            for st in (one['body'] or {}).get('studies') or []:
                nct = ((st.get('protocolSection') or {}).get('identificationModule') or {}).get('nctId')
                if nct:
                    full = A.logged_json(log, get, throttle, f'ctgov/study_full/{nct}', f'{API}/studies/{nct}', None,
                                         tag={'nct_id': nct})
                    asked += 1
                    unanswered += full is None
    return {'requests': asked, 'requests_without_answer': unanswered}


def _walk(obj: Any, path: str = '') -> Iterator[tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f'{path}.{k}' if path else k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path + '[]')
    elif isinstance(obj, str):
        yield path, obj


def locate_identifier(study: dict[str, Any], submission_number: str, window: int = 150) -> list[dict[str, Any]]:
    """Every place the EXACT submission number stands in a returned record: the registry field
    path and the verbatim text around it.

    Exact means the whole number as a token: not preceded by a letter or digit, and the whole number
    (with its supplement, when one is cited) not followed by a letter or digit, in any letter case, so "K990001ABC" and
    "P140011/S0089" are not "K990001" and "P140011/S008". For a PMA supplement the base number must be followed by its supplement ("P140011/S008",
    "P140011 S008", "P140011S008"); a record citing only the original PMA is reported with
    `supplement_cited: false` and is not a citation of the supplement. A record citing another
    supplement ("P140011/S007" when S008 is sought) cites neither and is not located. For an
    original PMA the number must stand alone: "P140011/S008" is the supplement, not P140011.
    """
    base, _, supp = submission_number.partition('/')
    # Case is ignored: a record may write the number in lower or mixed case ("k990001").
    pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(base) + r'(?:\s*/?\s*(S\d{3}))?(?![A-Za-z0-9])', re.IGNORECASE)
    found = []
    for path, text in _walk(study):
        for m in pattern.finditer(text):
            if not supp and m.group(1):
                # An original PMA is cited only by its number standing alone; "P140011/S008" is a
                # supplement, another submission.
                continue
            if supp and m.group(1) and m.group(1).upper() != supp.upper():
                # Another supplement of the same PMA is another submission; reporting it as
                # `supplement_cited: false` would read as a citation of the original PMA.
                continue
            quote = re.sub(r'\s+', ' ', text[max(0, m.start() - window): m.end() + window]).strip()
            found.append({'registry_field': path, 'quote': quote,
                          'supplement_cited': ((m.group(1) or '').upper() == supp.upper()) if supp else None})
    return found


def number_search_results(records: dict[str, dict[str, Any]], submission_numbers: list[str]) -> dict[str, dict[str, Any]]:
    """{submission number: {'state', 'n_search_hits', 'hits'}} from the frozen answers.

    state: completed (the batch covering the number was answered, and every follow-up it needed)
           | incomplete (some request is missing or answered with an error; the reason says which).
    Each hit lists where the number was located in the record; an empty list means the search
    engine returned the record although no returned field contains the number.
    """
    out: dict[str, dict[str, Any]] = {}
    batch_of = {n: chunk for chunk in number_batches(submission_numbers) for n in chunk}
    for sn in submission_numbers:
        number = searchable_number(sn)
        batch = records.get(_key('ctgov/number_batch', _batch_params(batch_of[number])))
        if batch is None or batch['status'] != 200:
            out[sn] = {'state': 'incomplete', 'reason': 'the batch request covering this number has no usable answer',
                       'n_search_hits': None, 'hits': []}
            continue
        if not (batch['body'] or {}).get('totalCount'):
            out[sn] = {'state': 'completed', 'reason': 'full-text search over the batch returned no record',
                       'n_search_hits': 0, 'hits': [], 'searched_at': batch['fetched_at']}
            continue
        one = records.get(_key('ctgov/number', _single_params(number)))
        if one is None or one['status'] != 200:
            out[sn] = {'state': 'incomplete', 'reason': 'the single-number follow-up has no usable answer',
                       'n_search_hits': None, 'hits': []}
            continue
        hits, missing = [], []
        returned = (one['body'] or {}).get('studies') or []
        total = (one['body'] or {}).get('totalCount')
        if not isinstance(total, int) or total > len(returned):
            # One page of up to 100 records is asked for. A search that found more, or did not say
            # how many it found, was not read in full, so it cannot support "no candidate".
            out[sn] = {'state': 'incomplete',
                       'reason': (f'the search found {total} records and the page read holds {len(returned)}; the rest were not read'
                                  if isinstance(total, int) else 'the search did not state how many records it found'),
                       'n_search_hits': total, 'hits': [], 'searched_at': one['fetched_at']}
            continue
        for st in returned:
            nct = st['protocolSection']['identificationModule']['nctId']
            full = records.get(f'ctgov/study_full/{nct}')
            if full is None or full['status'] != 200:
                missing.append(nct)
                continue
            hits.append({'nct_id': nct, 'located': locate_identifier(full['body'], sn),
                         'study': study_facts(full['body']), 'retrieved_at': full['fetched_at']})
        out[sn] = {'state': 'incomplete' if missing else 'completed',
                   'reason': (f'full records not held for {missing}' if missing else
                              'full-text search answered; each hit record fetched and read'),
                   'n_search_hits': (one['body'] or {}).get('totalCount'), 'hits': hits,
                   'searched_at': one['fetched_at']}
    return out


# ---------------------------------------------------------------- route 3: name and organisation

def phrase(s: str) -> str:
    return '"' + s.replace('"', ' ') + '"'


def name_queries(listed_name: str) -> list[dict[str, Any]]:
    """The name searches for one listed device name: its core name as an intervention search and
    as a quoted full-text phrase. When the cell lists several models the first two are searched
    separately (the whole cell cannot occur in a registry record). None for a name, or a model,
    made only of device-type words: it would return every study of that device type."""
    forms = I.name_forms(listed_name)
    strings = forms['segments'][:2] if len(forms['segments']) > 1 else [forms['core']]
    out = []
    for s in strings:
        toks = I.norm_text(s).split()
        if not toks or all(t in I.GENERIC_DEVICE_WORDS for t in toks):
            continue
        for kind, params in (('intervention', {'query.intr': s}), ('phrase', {'query.term': phrase(s)})):
            p = {**params, 'pageSize': str(NAME_PAGE_SIZE), 'countTotal': 'true', 'fields': SEARCH_FIELDS}
            out.append({'key': _key('ctgov/name', p), 'params': p, 'kind': kind, 'string': s})
    return out


def sponsor_query(organisation_normalised: str) -> dict[str, Any]:
    """The sponsor-or-collaborator search for one normalised organisation name."""
    p = {'query.spons': organisation_normalised, 'pageSize': str(SPONSOR_PAGE_SIZE), 'countTotal': 'true',
         'fields': SEARCH_FIELDS}
    return {'key': _key('ctgov/sponsor', p), 'params': p, 'kind': 'sponsor', 'string': organisation_normalised}


def full_variant(q: dict[str, Any]) -> dict[str, Any]:
    """The same search asked for FULL_PAGE_SIZE records."""
    p = {**q['params'], 'pageSize': str(FULL_PAGE_SIZE)}
    return {**q, 'key': _key('ctgov/full', p), 'params': p}


def _cut_off(rec: dict[str, Any] | None) -> int | None:
    """The total a 200 answer matched when it returned fewer records than that, else None."""
    body = (rec or {}).get('body') or {}
    total = body.get('totalCount')
    if rec and rec.get('status') == 200 and isinstance(total, int) and total > len(body.get('studies') or []):
        return total
    return None


def answer(records: dict[str, dict[str, Any]], q: dict[str, Any]) -> dict[str, Any] | None:
    """The answer to read for a search: the full-page follow-up when one was made and answered
    200, otherwise the first answer."""
    full = records.get(full_variant(q)['key'])
    return full if full is not None and full.get('status') == 200 else records.get(q['key'])


def fetch_searches(queries: list[dict[str, Any]], log: A.ResponseLog, get: A.Get, throttle: A.Throttle,
                   progress: Any = None) -> dict[str, int]:
    answered = unanswered = followed = 0
    seen: set[str] = set()
    for q in queries:
        if q['key'] in seen:
            continue
        seen.add(q['key'])
        rec = A.logged_json(log, get, throttle, q['key'], f'{API}/studies', q['params'],
                            tag={'kind': q['kind'], 'string': q['string']})
        answered, unanswered = answered + (rec is not None), unanswered + (rec is None)
        total = _cut_off(rec)
        if total is not None and total <= FULL_PAGE_SIZE:
            f = full_variant(q)
            followed += A.logged_json(log, get, throttle, f['key'], f'{API}/studies', f['params'],
                                      tag={'kind': q['kind'], 'string': q['string'], 'follow_up': True}) is not None
        if progress:
            progress(answered + unanswered)
    return {'searches_answered': answered, 'searches_without_answer': unanswered, 'follow_ups_answered': followed}


def contains_run(field_norm: str, phrase_norm: str) -> bool:
    """True when the phrase's tokens stand in the field as a contiguous run of WHOLE tokens, or
    when a run of up to four field tokens equals the phrase with its spaces removed ("Kardia AI"
    meets "KardiaAI"). Substring matching inside a token is never used."""
    if not phrase_norm or not field_norm:
        return False
    if f' {phrase_norm} ' in f' {field_norm} ':
        return True
    squashed, toks = phrase_norm.replace(' ', ''), field_norm.split()
    return any(''.join(toks[i:i + w]) == squashed for w in range(1, 5) for i in range(len(toks) - w + 1))


def name_evidence(study: dict[str, Any], forms: dict[str, Any]) -> dict[str, Any] | None:
    """Where a device-name form stands in a study, strongest first, with the field's verbatim text.

    Counted as name evidence: a `full` or `trimmed` form in an intervention name, an intervention
    other-name, the brief title or the official title. Recorded but not counted: a `company_only`
    form in those fields (the manufacturer is named, not the device) and any form only in an
    intervention description (`description_only`).
    """
    fields = [('intervention_name', i['name']) for i in study['interventions']]
    fields += [('intervention_other_name', o) for i in study['interventions'] for o in i['other_names']]
    fields += [('brief_title', study['brief_title']), ('official_title', study['official_title'])]
    for strength in ('full', 'trimmed', 'company_only'):
        for form in forms[strength]:
            for where, text in fields:
                if contains_run(I.norm_text(text), form):
                    return {'strength': strength, 'matched_form': form, 'registry_field': where, 'quote': text}
    for form in forms['full'] + forms['trimmed']:
        for i in study['interventions']:
            if contains_run(I.norm_text(i['description']), form):
                return {'strength': 'description_only', 'matched_form': form,
                        'registry_field': 'intervention_description', 'quote': i['description'][:400]}
    return None


def organisation_evidence(study: dict[str, Any], company_raw: str,
                          aliases: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    """How the listed manufacturer agrees with the study's lead sponsor or a collaborator, by
    organizations.match_tier (normalised name or curated alias; nothing weaker). The lead sponsor
    is preferred when both agree at the same tier."""
    rank = {'normalised_exact': 3, 'curated_alias': 2, 'normalised_legal_form_differs': 1}
    best: dict[str, Any] | None = None
    for role, name in [('lead_sponsor', study['lead_sponsor'])] + [('collaborator', c) for c in study['collaborators']]:
        tier = O.match_tier(company_raw, name, aliases)
        if tier and (best is None or rank[tier] > rank[best['tier']]):
            best = {'tier': tier, 'role': role, 'registry_name': name, 'listed_name': company_raw}
    return best


def search_state(records: dict[str, dict[str, Any]], queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether a set of searches was answered, and whether any answer was cut off by the page
    size (more records matched than were returned and read)."""
    detail, missing, failed, truncated = [], 0, 0, 0
    for q in queries:
        rec = answer(records, q)
        if rec is None:
            missing += 1
            detail.append({'kind': q['kind'], 'string': q['string'], 'answered': False})
            continue
        body = rec.get('body') or {}
        total, returned = body.get('totalCount'), len(body.get('studies') or [])
        ok = rec['status'] == 200
        failed += not ok
        cut = bool(ok and isinstance(total, int) and total > returned)
        truncated += cut
        detail.append({'kind': q['kind'], 'string': q['string'], 'answered': True, 'http_status': rec['status'],
                       'total_count': total, 'n_returned': returned, 'truncated': cut,
                       'searched_at': rec['fetched_at']})
    return {'n_queries': len(queries), 'n_missing': missing, 'n_failed': failed, 'n_truncated': truncated,
            'queries': detail}


def studies_from(records: dict[str, dict[str, Any]], queries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{nct id: study facts + which queries returned it} over the answered queries."""
    out: dict[str, dict[str, Any]] = {}
    for q in queries:
        rec = answer(records, q)
        if rec is None or rec['status'] != 200:
            continue
        for st in (rec.get('body') or {}).get('studies') or []:
            facts = study_facts(st)
            if facts['nct_id']:
                entry = out.setdefault(facts['nct_id'], {**facts, 'returned_by': [], 'retrieved_at': rec['fetched_at']})
                entry['returned_by'].append(f"{q['kind']}:{q['string']}")
    return out


_NAME_RANK = {'full': 3, 'trimmed': 2, 'company_only': 1, 'description_only': 0}
_ORG_RANK = {'normalised_exact': 3, 'curated_alias': 2, 'normalised_legal_form_differs': 1}
FLOOD = 50   # a name query matching this many records is noted on its candidates: a common word


def family_queries(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The distinct searches for one family: the name queries of every listed name of every
    member, and one sponsor query per normalised manufacturer name."""
    queries: list[dict[str, Any]] = []
    for s in members:
        for name in s['device_names_as_listed']:
            queries += name_queries(name)
        for company in s['companies_as_listed']:
            key = O.normalise(company)['normalised']
            if key:
                queries.append(sponsor_query(key))
    seen: set[str] = set()
    return [q for q in queries if not (q['key'] in seen or seen.add(q['key']))]


def family_name_search(members: list[dict[str, Any]], records: dict[str, dict[str, Any]],
                       aliases: dict[str, dict[str, str]]) -> dict[str, Any]:
    """What the name-and-organisation route found for one family, from the frozen answers.

    Every study any of the family's queries returned is read once. It becomes a `match` only when
    a full or trimmed name form stands in a title or intervention name (name_evidence). Everything
    else is tallied and set aside, so that the dashboard can show it was seen:

      n_name_only_common_word    a match on a name that floods the registry (FLOOD or more records
                                 for one of that name's own queries), without organisation
                                 agreement: kept as a record, rejected by rule
      n_organisation_only        sponsor agrees, device not named: NOT a candidate (shared
                                 sponsorship does not establish device identity)
      n_manufacturer_name_only   only the manufacturer's own name stands in a title/intervention
      n_description_only         the name stands only in an intervention description
      n_neither                  returned by the search engine with neither kind of evidence
    """
    queries = family_queries(members)
    state = search_state(records, queries)
    state['n_name_queries'] = sum(1 for q in queries if q['kind'] != 'sponsor')
    # A listed name floods when one of its own name queries matched FLOOD or more records. The flag
    # follows the name a study was matched on, never another name of the family: a distinctive
    # model name is not set aside because a sibling's name is a common word.
    flooded_names: dict[str, bool] = {}
    for s in members:
        for listed in s['device_names_as_listed']:
            flooded_names[listed] = flooded_names.get(listed, False) or any(
                isinstance(((answer(records, q) or {}).get('body') or {}).get('totalCount'), int)
                and (answer(records, q) or {}).get('status') == 200
                and answer(records, q)['body']['totalCount'] >= FLOOD for q in name_queries(listed))
    matches: list[dict[str, Any]] = []
    tallies = {'n_studies_returned': 0, 'n_name_and_organisation': 0, 'n_name_only': 0, 'n_name_only_common_word': 0,
               'n_organisation_only': 0,
               'n_manufacturer_name_only': 0, 'n_description_only': 0, 'n_neither': 0}
    for nct, study in sorted(studies_from(records, queries).items()):
        tallies['n_studies_returned'] += 1
        name: dict[str, Any] | None = None
        org: dict[str, Any] | None = None
        for s in members:
            for company in s['companies_as_listed']:
                o = organisation_evidence(study, company, aliases)
                if o and (org is None or _ORG_RANK[o['tier']] > _ORG_RANK[org['tier']]):
                    org = o
                for listed in s['device_names_as_listed']:
                    n = name_evidence(study, I.name_forms(listed, O.normalise(company)['normalised']))
                    # Strongest form first; between equally strong matches, a name that does not
                    # flood the registry is the better evidence.
                    rank = (_NAME_RANK[n['strength']], not flooded_names.get(listed, False)) if n else None
                    if n and (name is None or rank > name['_rank']):
                        name = {**n, 'listed_name': listed, 'via_submission': s['submission_number'], '_rank': rank}
        if name:
            name = {k: v for k, v in name.items() if k != '_rank'}
        flooded = bool(name) and flooded_names.get(name['listed_name'], False)
        agrees = bool(org) and org['tier'] in ('normalised_exact', 'curated_alias')
        if name and name['strength'] in ('full', 'trimmed'):
            flags = []
            if name['strength'] == 'trimmed':
                flags.append('matched on a trimmed name form (trailing device-type words removed): may be a '
                             'sibling or earlier product of the same line')
            if flooded and not agrees:
                flags.append(f'a search for the matched name returned {FLOOD} or more records: the name is a common '
                             f'word or shared with other products')
            tallies['n_name_and_organisation' if agrees else
                    'n_name_only_common_word' if flooded else 'n_name_only'] += 1
            matches.append({'study': study, 'name': name, 'organisation': org, 'flags': flags,
                            'common_word': flooded})
        elif agrees:
            tallies['n_organisation_only'] += 1
        elif name and name['strength'] == 'company_only':
            tallies['n_manufacturer_name_only'] += 1
        elif name:
            tallies['n_description_only'] += 1
        else:
            tallies['n_neither'] += 1
    return {'search': state, 'matches': matches, 'tallies': tallies}
