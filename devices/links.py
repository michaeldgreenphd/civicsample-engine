"""Evidence records linking submissions and device families to registered studies.

One record is one piece of evidence, not one pair: a submission and a study may be joined by
several records, a study may be joined to several submissions and families, and a submission to
several studies. Every record carries, as separate fields that are never folded into one another:

  source, target          the two entities (a submission or a family; a registered study)
  level                   'submission' or 'family'
  proposed_relationship   what the evidence says, in the vocabulary below
  method                  how the evidence was found
  evidence                the verbatim text, and the document page or registry field it stands in
  source_version          the hash and retrieval date of the document, or the registry's data
                          timestamp and retrieval date
  confidence, reasons     a tier and why
  automated_validation    what a machine checked (below)
  human_review            what a person decided; read from devices/reviews.csv and never set here
  timing                  study dates against FDA decision dates; never part of the relationship

What can be verified automatically, and what that means
-------------------------------------------------------
`identifier_confirmed` is given in exactly two cases:
  * a document that belongs to the submission cites an NCT id in its text, and the registry
    returns a record with that id (method nct_id_in_submission_document);
  * a registry record carries the exact submission number in a field that was retrieved
    (method submission_number_in_registry_record).
It confirms that the citation exists and that the identifier is real. It does NOT confirm what
the citation means: a summary may cite a predicate's trial or background literature, and a
registry record may cite a clearance it merely uses. The proposed relationship of such a record
is therefore only "cited"; whether the study supported the decision is a human reading, recorded
as `reviewed_relationship`.

A third route to a submission-level link needs no shared identifier: an explicit documentary
statement, entered by a person in devices/documentary_statements.csv with its verbatim quote and
location. The build checks only that the quote stands in the retrieved source
(`quote_confirmed_in_source`); the statement's meaning is again a human matter.

Name similarity is never enough. A name match, with or without an organisation match, produces a
family-level candidate with automated_validation `not_validated` (or `rejected_by_rule`, when the
name is a common word and no organisation agrees), and problems() refuses anything else. Organisation agreement raises confidence one tier and is recorded; its absence is recorded
too and rejects nothing (investigators, collaborators, acquirers and subsidiaries sponsor studies
of devices they do not make), and shared sponsorship with no name evidence is not a link at all.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

LEVELS = ('submission', 'family')
METHODS = ('nct_id_in_submission_document', 'submission_number_in_registry_record', 'documentary_statement',
           'nct_id_in_parent_submission_document', 'device_name_and_organisation_match', 'device_name_match')
IDENTIFIER_METHODS = ('nct_id_in_submission_document', 'submission_number_in_registry_record')
HEURISTIC_METHODS = ('device_name_and_organisation_match', 'device_name_match')
# What the evidence itself can say. None of these words is about time.
PROPOSED_RELATIONSHIPS = (
    'study_cited_in_submission_document',         # an FDA document of the submission cites the NCT id
    'submission_cited_in_registry_record',        # the registry record cites the submission number
    'study_cited_in_parent_submission_document',  # the ORIGINAL PMA's document cites it; the supplement's does not
    'name_and_organisation_candidate',            # device name in a title/intervention and sponsor agrees
    'name_only_candidate',                        # device name in a title/intervention, sponsor does not agree
    'explicitly_cited_supporting_study',          # only through a documentary statement entered by a person
    'related_validation_study',                   # "
    'documented_postmarket_study',                # "
)
STATEMENT_RELATIONSHIPS = PROPOSED_RELATIONSHIPS[5:]
# What a reviewer may conclude (devices/reviews.csv, reviewed_relationship). Kept beside, never
# instead of, the proposed relationship.
REVIEWED_RELATIONSHIPS = ('explicitly_cited_supporting_study', 'related_validation_study',
                          'documented_postmarket_study', 'same_family_other_version',
                          'citation_not_about_this_device', 'not_related')
AUTOMATED_VALIDATION = ('identifier_confirmed', 'quote_confirmed_in_source', 'identifier_not_resolved',
                        'rejected_by_rule', 'not_validated')
HUMAN_REVIEW = ('unreviewed', 'accepted', 'rejected', 'unresolved')
CONFIDENCE = ('identifier', 'documentary', 'moderate', 'low')
TIMING_RELATIONS = ('primary_completion_on_or_before_decision', 'started_on_or_before_decision_not_complete',
                    'started_within_365_days_after_decision', 'started_more_than_365_days_after_decision',
                    'indeterminate_at_recorded_precision', 'start_date_absent', 'decision_date_absent')
REVIEW_COLUMNS = ['link_id', 'human_review', 'reviewed_relationship', 'reviewed_on', 'note']
STATEMENT_COLUMNS = ['submission_number', 'nct_id', 'source_kind', 'source_id', 'page_or_field', 'verbatim_quote',
                     'proposed_relationship', 'recorded_on', 'note']
_NCT = re.compile(r'^NCT\d{8}$')


def link_id(*parts: Any) -> str:
    """Stable across builds: a hash of what the evidence is (entities, method, where it stands),
    not of when it was found. A review in devices/reviews.csv is keyed on it."""
    return 'link:' + hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- timing

def _bounds(s: str | None) -> tuple[date, date] | None:
    """The earliest and latest day a registry date string can mean. The registry holds
    YYYY-MM-DD or YYYY-MM; a month is not placed on a day."""
    m = re.fullmatch(r'(\d{4})-(\d{2})(?:-(\d{2}))?', s or '')
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    try:
        if m.group(3):
            d = date(y, mo, int(m.group(3)))
            return d, d
        return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    except ValueError:
        return None


def _relation(start: date, primary: date | None, decision: date) -> str:
    if primary is not None and primary <= decision:
        return 'primary_completion_on_or_before_decision'
    if start <= decision:
        return 'started_on_or_before_decision_not_complete'
    return ('started_within_365_days_after_decision' if (start - decision).days <= 365
            else 'started_more_than_365_days_after_decision')


def timing_for(study: dict[str, Any] | None, decisions: dict[str, str | None]) -> dict[str, Any]:
    """Study dates set against each relevant FDA decision date. A description, not evidence.

    A study completed before a decision did not necessarily support it, and one that starts
    afterwards may be post-market surveillance, a new indication or a later version's validation.
    So this is computed for every link, stored apart from the relationship, and never changes
    confidence or validation. For a family the relation is given per member submission: the same
    study precedes a later clearance and follows an earlier one.

    When a month-precision date makes the relation depend on the day, it is
    `indeterminate_at_recorded_precision` rather than guessed.
    """
    study = study or {}
    start, primary = _bounds(study.get('start_date')), _bounds(study.get('primary_completion_date'))
    out: dict[str, Any] = {'study_start_date': study.get('start_date'),
                           'study_primary_completion_date': study.get('primary_completion_date'),
                           'study_completion_date': study.get('completion_date'),
                           'study_first_posted_date': study.get('first_posted_date'), 'by_submission': []}
    for sn, iso in sorted(decisions.items()):
        row: dict[str, Any] = {'submission_number': sn, 'fda_decision_date': iso,
                               'start_minus_decision_days': None, 'primary_completion_minus_decision_days': None}
        if not iso:
            row['relation'] = 'decision_date_absent'
        elif start is None:
            row['relation'] = 'start_date_absent'
        else:
            d = date.fromisoformat(iso)
            relations = {_relation(s, p, d) for s in start for p in (primary or (None, None))}
            row['relation'] = relations.pop() if len(relations) == 1 else 'indeterminate_at_recorded_precision'
            # Day gaps as [earliest, latest] the recorded precision allows.
            row['start_minus_decision_days'] = [(start[0] - d).days, (start[1] - d).days]
            if primary:
                row['primary_completion_minus_decision_days'] = [(primary[0] - d).days, (primary[1] - d).days]
        out['by_submission'].append(row)
    return out


# ---------------------------------------------------------------- constructors

def _record(*, lid: str, level: str, source_type: str, source_id: str, nct_id: str, relationship: str, method: str,
            evidence: dict[str, Any], source_version: dict[str, Any], confidence: str, confidence_reasons: list[str],
            validation: str, validation_reasons: list[str], timing: dict[str, Any]) -> dict[str, Any]:
    return {'link_id': lid, 'level': level, 'source': {'entity_type': source_type, 'id': source_id},
            'target': {'entity_type': 'registered_study', 'registry': 'ClinicalTrials.gov', 'id': nct_id},
            'proposed_relationship': relationship, 'method': method, 'evidence': evidence,
            'source_version': source_version, 'confidence': confidence, 'confidence_reasons': confidence_reasons,
            'automated_validation': validation, 'validation_reasons': validation_reasons,
            # Set only by apply_reviews(), from a row a person wrote.
            'human_review': 'unreviewed', 'reviewed_relationship': None, 'review': None,
            'timing': timing}


def from_document_citation(doc: dict[str, Any], identifier: dict[str, Any], state: str,
                           study: dict[str, Any] | None, decision_date: str | None,
                           registry_version: dict[str, Any]) -> dict[str, Any]:
    """An NCT id found in a document's text. Submission level and identifier-confirmed only when
    the document belongs to the submission itself and the id resolves; a parent PMA's document
    gives a family-level, unvalidated record; an id the registry does not hold gives
    `identifier_not_resolved`."""
    sn, nct = doc['submission_number'], identifier['identifier']
    own = doc['scope'] == 'this_submission'
    method = 'nct_id_in_submission_document' if own else 'nct_id_in_parent_submission_document'
    reasons = [f"NCT id in the text of a {doc['document_type']} (page {identifier['page']})"]
    if state == 'resolves' and own:
        validation, vreasons = 'identifier_confirmed', ['the cited id resolves to a registry record with that id',
                                                        'the document belongs to this submission']
    elif state == 'resolves':
        validation, vreasons = 'not_validated', ['the cited id resolves, but the document belongs to the original '
                                                 'PMA, not to this supplement']
    elif state in ('not_found', 'resolved_to_different_id'):
        validation, vreasons = 'identifier_not_resolved', [f'registry lookup: {state}']
    else:
        validation, vreasons = 'not_validated', [f'registry lookup: {state}']
    if own:
        reasons.append('a citation shows the document mentions the study; it does not show the study supported '
                       'this decision (it may be a predicate\'s study or background)')
    return _record(
        # The quoted sentence is part of what the evidence is (as the quote is for a registry
        # citation): a new version of the document that changes it is new evidence, and a review
        # of the old sentence is reported orphaned rather than carried over.
        lid=link_id('submission', sn, nct, method, doc['document_id'], identifier['page'],
                    re.sub(r'\s+', ' ', identifier['sentence'] or '').strip()),
        level='submission' if own else 'family', source_type='submission', source_id=sn, nct_id=nct,
        relationship='study_cited_in_submission_document' if own else 'study_cited_in_parent_submission_document',
        method=method,
        evidence={'source_kind': 'fda_document', 'source_id': doc['document_id'], 'document_type': doc['document_type'],
                  'location': {'page': identifier['page']}, 'quote': identifier['sentence'],
                  'matched_text': identifier['raw_match'], 'organisation': None},
        source_version={'document_sha256': doc['sha256'], 'retrieved_at': doc['retrieved_at'],
                        'source_last_modified': doc['source_last_modified'],
                        'extraction_version': (doc['text'] or {}).get('extraction_version'), **registry_version},
        confidence='identifier' if validation == 'identifier_confirmed' else 'low', confidence_reasons=reasons,
        validation=validation, validation_reasons=vreasons, timing=timing_for(study, {sn: decision_date}))


def from_registry_citation(submission_number: str, hit: dict[str, Any], decision_date: str | None,
                           registry_version: dict[str, Any]) -> list[dict[str, Any]]:
    """Records for one registry record returned by a submission-number search.

    One identifier-confirmed record per field that holds the exact number. A record returned by
    the search engine with the number in no retrieved field is kept as `rejected_by_rule` (so the
    hit is visible and nobody searches it again), with the brief title as its evidence text. A
    record citing only the original PMA of a listed supplement is family-level and unvalidated.
    """
    nct, study = hit['nct_id'], hit['study']
    version = {**registry_version, 'retrieved_at': hit.get('retrieved_at')}
    timing = timing_for(study, {submission_number: decision_date})
    method = 'submission_number_in_registry_record'
    if not hit['located']:
        return [_record(
            lid=link_id('submission', submission_number, nct, method, 'not_located'), level='submission',
            source_type='submission', source_id=submission_number, nct_id=nct,
            relationship='submission_cited_in_registry_record', method=method,
            evidence={'source_kind': 'registry_record', 'source_id': nct,
                      'location': {'registry_field': 'protocolSection.identificationModule.briefTitle'},
                      'quote': study.get('brief_title') or nct, 'matched_text': None, 'organisation': None},
            source_version=version, confidence='low',
            confidence_reasons=['returned by full-text search for the number'], validation='rejected_by_rule',
            validation_reasons=['the submission number stands in no field of the returned record; a search hit '
                                'alone is not a citation'], timing=timing)]
    # One record per registry field: a field that names the number twice is one citation. Where a
    # field holds both the exact number and only the original PMA's, the exact citation is kept.
    by_field: dict[str, dict[str, Any]] = {}
    for loc in hit['located']:
        held = by_field.get(loc['registry_field'])
        if held is None or (held['supplement_cited'] is False and loc['supplement_cited'] is not False):
            by_field[loc['registry_field']] = loc
    out = []
    for loc in by_field.values():
        exact = loc['supplement_cited'] is not False
        out.append(_record(
            lid=link_id('submission', submission_number, nct, method, loc['registry_field'], loc['quote'][:80]),
            level='submission' if exact else 'family', source_type='submission', source_id=submission_number,
            nct_id=nct, relationship='submission_cited_in_registry_record', method=method,
            evidence={'source_kind': 'registry_record', 'source_id': nct,
                      'location': {'registry_field': loc['registry_field']}, 'quote': loc['quote'],
                      'matched_text': submission_number if exact else submission_number.split('/')[0],
                      'organisation': None},
            source_version=version, confidence='identifier' if exact else 'low',
            confidence_reasons=[f"the registry record carries the number in {loc['registry_field']}",
                                'a citation shows the record mentions the submission; whether the study supported '
                                'it, used the cleared device, or followed it is a human reading'],
            validation='identifier_confirmed' if exact else 'not_validated',
            validation_reasons=(['the exact submission number stands in a retrieved field of the record'] if exact else
                                ['the record cites the original PMA number, not this supplement']),
            timing=timing))
    return out


def from_name_match(family: dict[str, Any], study: dict[str, Any], name: dict[str, Any],
                    organisation: dict[str, Any] | None, decisions: dict[str, str | None],
                    registry_version: dict[str, Any], flags: list[str], common_word: bool = False) -> dict[str, Any]:
    """A candidate from a device name standing in a study's title or intervention. Always family
    level and never validated: the listed name is shared by every submission of the family, and a
    string cannot say which clearance, if any, the study concerns.

    `common_word`: a name query for the family matched so many registry records that the name is
    an ordinary word or is shared with other products ("Loop", "Aurora", "Contour"). Such a match
    WITHOUT organisation agreement is kept, so it is visible and not searched again, but is
    `rejected_by_rule` rather than put in front of a reviewer. The rule costs recall: an
    investigator-led study of a device with a common name is set aside with the coincidences, and
    a reviewer can still accept one in reviews.csv."""
    agrees = bool(organisation) and organisation['tier'] in ('normalised_exact', 'curated_alias')
    method = 'device_name_and_organisation_match' if agrees else 'device_name_match'
    reasons = [f"name form '{name['matched_form']}' ({name['strength']}) in {name['registry_field']}"]
    if agrees:
        reasons.append(f"manufacturer agrees with the study's {organisation['role']} by {organisation['tier']}")
    elif organisation:
        reasons.append(f"manufacturer and {organisation['role']} agree only after different legal forms were "
                       f"removed ({organisation['registry_name']!r}); treated as not agreeing")
    else:
        reasons.append('no sponsor or collaborator agrees with the listed manufacturer by normalised name or '
                       'curated alias (an investigator-led study, a renamed company, or a coincidence of names)')
    reasons += flags
    return _record(
        lid=link_id('family', family['family_id'], study['nct_id'], 'name', name['registry_field'], name['matched_form']),
        level='family', source_type='family', source_id=family['family_id'], nct_id=study['nct_id'],
        relationship='name_and_organisation_candidate' if agrees else 'name_only_candidate', method=method,
        evidence={'source_kind': 'registry_record', 'source_id': study['nct_id'],
                  'location': {'registry_field': name['registry_field']}, 'quote': name['quote'],
                  'matched_text': name['matched_form'], 'name_form_strength': name['strength'],
                  'organisation': organisation},
        source_version={**registry_version, 'retrieved_at': study.get('retrieved_at')},
        confidence='moderate' if agrees and not flags else 'low', confidence_reasons=reasons,
        validation='rejected_by_rule' if common_word and not agrees else 'not_validated',
        validation_reasons=(['the name matches too many registry records to identify a device, and no sponsor or '
                             'collaborator agrees with the manufacturer'] if common_word and not agrees else
                            ['string evidence only; no identifier joins the study to any submission of the family']),
        timing=timing_for(study, decisions))


# ---------------------------------------------------------------- documentary statements

def read_statements(path: Path) -> list[dict[str, str]]:
    """Rows of devices/documentary_statements.csv: explicit statements a person found in a
    document or registry record that tie a study to a submission without a shared identifier."""
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != STATEMENT_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {STATEMENT_COLUMNS}')
        rows = list(reader)
    for i, r in enumerate(rows, start=2):
        if not _NCT.match(r['nct_id'] or '') or r['source_kind'] not in ('fda_document', 'registry_record') or \
                r['proposed_relationship'] not in STATEMENT_RELATIONSHIPS or not (r['verbatim_quote'] or '').strip() \
                or not (r['page_or_field'] or '').strip() or not (r['note'] or '').strip():
            raise ValueError(f'{path.name} line {i}: needs an NCT id, a source kind, a statement relationship, '
                             f'a location, the verbatim quote and a note')
    return rows


def _squash(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip().lower()


def from_statement(row: dict[str, str], source_text: str | None, source_version: dict[str, Any],
                   study: dict[str, Any] | None, decision_date: str | None) -> dict[str, Any]:
    """A submission-level record from a documentary statement. The build confirms only that the
    quote stands in the retrieved source at the stated place (whitespace and case aside)."""
    located = source_text is not None and _squash(row['verbatim_quote']) in _squash(source_text)
    is_page = row['page_or_field'].strip().isdigit()
    return _record(
        lid=link_id('submission', row['submission_number'], row['nct_id'], 'documentary_statement', row['source_id'],
                    row['page_or_field'], _squash(row['verbatim_quote'])[:80]),
        level='submission', source_type='submission', source_id=row['submission_number'], nct_id=row['nct_id'],
        relationship=row['proposed_relationship'], method='documentary_statement',
        evidence={'source_kind': row['source_kind'], 'source_id': row['source_id'],
                  'location': ({'page': int(row['page_or_field'])} if is_page else
                               {'registry_field': row['page_or_field'].strip()}),
                  'quote': row['verbatim_quote'], 'matched_text': None, 'organisation': None,
                  'recorded_on': row['recorded_on'] or None, 'note': row['note']},
        source_version=source_version, confidence='documentary',
        confidence_reasons=['an explicit statement recorded by a person, with its quote and location'],
        validation='quote_confirmed_in_source' if located else 'not_validated',
        validation_reasons=(['the quote stands in the retrieved source at the stated place'] if located else
                            ['the quote was not found in the retrieved source at the stated place']),
        timing=timing_for(study, {row['submission_number']: decision_date}))


# ---------------------------------------------------------------- human review

def read_reviews(path: Path) -> dict[str, dict[str, str]]:
    """{link_id: row} from devices/reviews.csv. Refuses unknown columns or values, a link reviewed
    twice, and a decision without a date and a note (the note holds the reason; no reviewer is
    named in a published file)."""
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != REVIEW_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {REVIEW_COLUMNS}')
        rows = list(reader)
    out: dict[str, dict[str, str]] = {}
    for i, r in enumerate(rows, start=2):
        lid, state, rel = (r['link_id'] or '').strip(), (r['human_review'] or '').strip(), \
            (r['reviewed_relationship'] or '').strip()
        if state not in HUMAN_REVIEW[1:]:
            raise ValueError(f'{path.name} line {i}: human_review must be one of {HUMAN_REVIEW[1:]}')
        if rel and rel not in REVIEWED_RELATIONSHIPS:
            raise ValueError(f'{path.name} line {i}: unknown reviewed_relationship {rel!r}')
        if state == 'accepted' and not rel:
            raise ValueError(f'{path.name} line {i}: an accepted link must say which relationship was accepted')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', (r['reviewed_on'] or '').strip()) or not (r['note'] or '').strip():
            raise ValueError(f'{path.name} line {i}: reviewed_on (YYYY-MM-DD) and note are required')
        if lid in out:
            raise ValueError(f'{path.name} line {i}: {lid} is reviewed twice')
        out[lid] = r
    return out


def apply_reviews(links: list[dict[str, Any]], reviews: dict[str, dict[str, str]]) -> list[str]:
    """Write each review onto its link, changing nothing else about the link (the proposed
    relationship, the validation status and the evidence stay as found). Returns the link ids
    that were reviewed but no longer exist in this build, so that a decision is never lost
    silently when the evidence under it changes."""
    by_id = {link['link_id']: link for link in links}
    for lid, r in reviews.items():
        if lid in by_id:
            by_id[lid]['human_review'] = r['human_review'].strip()
            by_id[lid]['reviewed_relationship'] = r['reviewed_relationship'].strip() or None
            by_id[lid]['review'] = {'reviewed_on': r['reviewed_on'].strip(), 'note': r['note'].strip()}
    return sorted(set(reviews) - set(by_id))


# ---------------------------------------------------------------- the rules, as a check

def _norm_id(s: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def problems(link: dict[str, Any], reviewed_ids: set[str] | None = None) -> list[str]:
    """Every rule of this module a record breaks (empty when it breaks none). The contract runs
    this on every record of a build; a build with any problem is refused."""
    p: list[str] = []
    lid = link.get('link_id', '?')
    ev = link.get('evidence') or {}
    for field, vocab in (('level', LEVELS), ('method', METHODS), ('proposed_relationship', PROPOSED_RELATIONSHIPS),
                         ('automated_validation', AUTOMATED_VALIDATION), ('human_review', HUMAN_REVIEW),
                         ('confidence', CONFIDENCE)):
        if link.get(field) not in vocab:
            p.append(f'{lid}: {field} {link.get(field)!r} is not in the vocabulary')
    if not _NCT.match(str((link.get('target') or {}).get('id'))):
        p.append(f'{lid}: target is not an NCT id')
    if not (link.get('source') or {}).get('id'):
        p.append(f'{lid}: no source entity')
    if not str(ev.get('quote') or '').strip():
        p.append(f'{lid}: no verbatim evidence')
    loc = ev.get('location') or {}
    if not ((isinstance(loc.get('page'), int) and loc['page'] >= 1) or str(loc.get('registry_field') or '').strip()):
        p.append(f'{lid}: evidence has neither a document page nor a registry field')
    if not ev.get('source_id') or not link.get('source_version'):
        p.append(f'{lid}: evidence source or source version missing')
    if not link.get('confidence_reasons') or not link.get('validation_reasons'):
        p.append(f'{lid}: confidence and validation each need a reason')
    method, validation = link.get('method'), link.get('automated_validation')
    if method in HEURISTIC_METHODS:
        if link.get('level') != 'family':
            p.append(f'{lid}: a name match is submission-level; name similarity can only reach a family')
        if validation not in ('not_validated', 'rejected_by_rule'):
            p.append(f'{lid}: a heuristic match is marked {validation}; it must stay not_validated')
        if link.get('confidence') not in ('moderate', 'low'):
            p.append(f'{lid}: a heuristic match cannot carry identifier or documentary confidence')
        if (link.get('source') or {}).get('entity_type') != 'family':
            p.append(f'{lid}: a name match must have a family as its source')
    if validation == 'identifier_confirmed':
        if method not in IDENTIFIER_METHODS or link.get('level') != 'submission':
            p.append(f'{lid}: identifier_confirmed needs an identifier method at submission level')
        expected = (link.get('target') or {}).get('id') if method == 'nct_id_in_submission_document' \
            else (link.get('source') or {}).get('id')
        if _norm_id(str(expected)) not in _norm_id(str(ev.get('quote'))):
            p.append(f'{lid}: identifier_confirmed but the identifier is not in the quoted evidence')
    if validation == 'quote_confirmed_in_source' and method != 'documentary_statement':
        p.append(f'{lid}: quote_confirmed_in_source is only for documentary statements')
    if link.get('proposed_relationship') in STATEMENT_RELATIONSHIPS and method != 'documentary_statement':
        p.append(f'{lid}: {link.get("proposed_relationship")} can be proposed only by a documentary statement')
    if link.get('confidence') == 'identifier' and validation != 'identifier_confirmed':
        p.append(f'{lid}: identifier confidence without an identifier-confirmed validation')
    reviewed = link.get('human_review') != 'unreviewed'
    if reviewed != bool(link.get('review')) or (reviewed_ids is not None and reviewed != (lid in reviewed_ids)):
        p.append(f'{lid}: human_review is set without a row in reviews.csv (or a row was not applied)')
    if link.get('reviewed_relationship') and link['reviewed_relationship'] not in REVIEWED_RELATIONSHIPS:
        p.append(f'{lid}: unknown reviewed_relationship')
    timing = link.get('timing')
    if not isinstance(timing, dict) or 'by_submission' not in timing:
        p.append(f'{lid}: timing must be recorded as its own object')
    else:
        for row in timing['by_submission']:
            if row.get('relation') not in TIMING_RELATIONS:
                p.append(f'{lid}: unknown timing relation {row.get("relation")!r}')
    return p
