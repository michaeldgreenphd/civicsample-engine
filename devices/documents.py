"""The document registry: what was retrieved for each submission, what kind of document it is,
and what its text says about studies and identifiers.

A document is identified by its submission number and source URL (acquire.document_id). Its
content is a version, identified by sha256; the same URL fetched later with different bytes is a
new version of the same document, and both are kept. Text is extracted once per version and
extractor version (devices/pdftext.py).

Document type matters more than anything else here. The pilot read ten De Novo "documents" that
were all granting letters, which describe no study, and so learned nothing about study citation.
Every record therefore says what type the document is, by which rule, on which words, and
whether it is the kind of document a demographic table could be in.

Everything in this module is a text rule. It reads text layers only (no OCR), and its statements
about what a document "describes" are keyword findings to be confirmed by a reader, not
conclusions.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

from devices import pdftext

# type -> (category, could a demographic table be here?, why not)
DOCUMENT_TYPES: dict[str, tuple[str, bool, str]] = {
    'de_novo_decision_summary': ('decision_summary', True, ''),
    '510k_decision_summary': ('decision_summary', True, ''),
    'pma_ssed': ('summary_of_safety_and_effectiveness', True, ''),
    '510k_summary': ('510k_summary', True, ''),
    '510k_statement': ('510k_statement', False,
                       'a 510(k) Statement promises information on request and contains none'),
    '510k_clearance_letter': ('granting_letter', False,
                              'a clearance letter states the decision; it does not describe a study population'),
    'de_novo_granting_letter': ('granting_letter', False,
                                'a De Novo granting letter states the classification and special controls; the '
                                'study is described in the separate decision summary'),
    'pma_approval_order': ('granting_letter', False,
                           'an approval order states the decision and conditions; the study is described in the '
                           'summary of safety and effectiveness'),
    'pma_labeling': ('labeling', False,
                     'labeling is kept as regulatory evidence; it may restate study results but is not the '
                     'decision record, and this rule does not treat it as a demographic summary'),
    'other': ('other', False, 'the text matches no known document type; a reader has to say what it is'),
    'unclassified_no_text': ('unclassified', False, 'no extractable text and no FDA label; type unknown'),
}
CATEGORIES = tuple(sorted({v[0] for v in DOCUMENT_TYPES.values()}))
# FDA's own link labels on its database pages -> the type that label implies.
LABEL_TYPES = {
    ('510k', 'summary'): '510k_summary', ('510k', 'statement'): '510k_statement',
    ('510k', 'decision summary'): '510k_decision_summary',
    ('de_novo', 'classification order'): 'de_novo_granting_letter',
    ('de_novo', 'decision summary'): 'de_novo_decision_summary',
    ('pma', 'approval order'): 'pma_approval_order', ('pma', 'summary of safety and effectiveness'): 'pma_ssed',
    ('pma', 'labeling'): 'pma_labeling', ('pma', 'labeling part 2'): 'pma_labeling',
}
LETTER_TYPES = ('510k_clearance_letter', 'de_novo_granting_letter', 'pma_approval_order')
CLASSIFICATION_BASES = ('text', 'official_label_only', 'official_label_text_unmatched', 'official_label_unread_pages',
                        'none')
# A PDF "portfolio" is a container of embedded files; the text layer holds only this cover notice.
_PORTFOLIO_COVER = re.compile(r'open\s+this\s+PDF\s+portfolio', re.I)
STUDY_DESCRIPTION_STATES = ('cites_registry_identifier', 'describes_study_no_registry_identifier',
                            'states_no_clinical_study_only', 'no_study_language_found', 'not_assessable_no_text')
MAX_EXAMPLES = 3


def _ws(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


def _rx(words: str) -> re.Pattern[str]:
    """A phrase pattern tolerant of the whitespace a PDF text layer puts between (not inside) words."""
    return re.compile(words.replace(' ', r'\s*'), re.I)


# (type, pattern, how many leading pages it may stand on; 0 = anywhere). First match wins within
# the pathway's list; the order encodes which heading outranks which.
_RULES: dict[str, list[tuple[str, re.Pattern[str], int]]] = {
    'de_novo': [
        ('de_novo_decision_summary', _rx(r'DE NOVO CLASSIFICATION REQUEST FOR'), 2),
        ('de_novo_decision_summary', _rx(r'EVALUATION OF AUTOMATIC CLASS III DESIGNATION[^.]{0,200}?DECISION SUMMARY'), 2),
        ('de_novo_decision_summary', _rx(r'DECISION SUMMARY'), 1),
        ('de_novo_granting_letter', _rx(r'(?:review of your De Novo request|request for De Novo classification|'
                                        r'this order[, ]+therefore[, ]+classif|classification order|'
                                        r'Evaluation of Automatic Class III Designation)'), 3),
    ],
    '510k': [
        ('510k_decision_summary', _rx(r'SUBSTANTIAL EQUIVALENCE DETERMINATION DECISION (?:SUMMARY|MEMORANDUM)'), 2),
        ('510k_decision_summary', _rx(r'DECISION SUMMARY'), 1),
        ('510k_decision_summary', _rx(r'OIR Review Memorandum'), 1),
        # "5 10(k)" and "51 0(k)" are how OCR text layers of older scans space the number, and a
        # heading at a page edge can lose its last letter ("510(k) Summar"): both are accepted.
        ('510k_summary', _rx(r'5 ?1 ?0 ?\(? ?K ?\)? SUMMAR'), 0),
        ('510k_summary', _rx(r'SUMMARY OF SAFETY (?:AND|&) EFFECTIVENESS'), 0),
        ('510k_summary', _rx(r'PREMARKET NOTIFICATION[^.]{0,60}?SUMMARY'), 0),
        ('510k_statement', _rx(r'5 ?1 ?0 ?\(? ?K ?\)? STATEMENT'), 0),
        ('510k_statement', _rx(r'I certify that[^.]{0,200}?will make available all information'), 0),
        # No heading in the text layer (it is often an image), but the body of a summary is there:
        # 21 CFR 807.92 is the regulation that defines a 510(k) summary's content.
        ('510k_summary', _rx(r'(?:21 CFR ?)?807\.92'), 0),
        ('510k_summary', _rx(r'(?:Summary of (?:the )?Technological Characteristics|Technological Characteristics '
                             r'(?:Comparison|of the Device)|Substantial Equivalence (?:Summary|Comparison|Discussion))'), 0),
        ('510k_clearance_letter', _rx(r'reviewed your Section 510 ?\(? ?k ?\)? premarket notification'), 3),
    ],
    'pma': [
        ('pma_ssed', _rx(r'SUMMARY OF SAFETY (?:AND|&) EFFECTIVENESS(?: DATA)?'), 2),
        ('pma_approval_order', _rx(r'premarket approval application[^.]{0,400}?(?:is|are|has been) approved'), 3),
        ('pma_approval_order', _rx(r'(?:PMA|premarket approval)[^.]{0,200}?(?:is|are) approved'), 3),
        ('pma_labeling', _rx(r"(?:INSTRUCTIONS FOR USE|USER(?:'S)? (?:MANUAL|GUIDE)|OPERATOR(?:'S)? MANUAL|"
                             r"PHYSICIAN LABELING|PATIENT LABELING|REFERENCE MANUAL)"), 3),
    ],
}
_RULES['pma_supplement'] = _RULES['pma']
_COMPONENTS = {
    'clearance_letter': _rx(r'reviewed your Section 510 ?\(? ?k ?\)? premarket notification'),
    'indications_for_use_form': _rx(r'Indications for Use'),
    '510k_summary': _rx(r'510 ?\(? ?K ?\)? SUMMARY'),
    '510k_statement': _rx(r'510 ?\(? ?K ?\)? STATEMENT'),
}


# The body of a 510(k) summary, for files whose heading is not in the text layer. Title case is
# required for "Predicate Device": FDA's own clearance letter speaks of "predicate devices".
_SUMMARY_MARKERS = [re.compile(x) for x in (
    r'\bSubmitter\b|\bApplicant Name\b|510\s*\(k\)\s*(?:Sponsor|Owner)', r'Predicate\s+Device',
    r'Device\s+Description|Description\s+of\s+(?:the\s+)?Device', r'Technological\s+Characteristics',
    r'Substantial(?:ly)?\s+Equivalen(?:ce|t)\s+(?:Discussion|Comparison|Summary|Table|Conclusion)',
    r'Performance\s+(?:Data|Testing)|Non-?[Cc]linical\s+(?:Testing|Tests|Performance|Data)')]
SUMMARY_MARKERS_NEEDED = 3
_STOPWORDS = frozenset({'the', 'and', 'of', 'to', 'for', 'is', 'in', 'with', 'this', 'device', 'that', 'be', 'as', 'by',
                        'or', 'are', 'on'})


def garbled(page: str) -> bool:
    """True when a page's text layer is not readable text: the PDF's fonts carry no mapping back
    to characters, so the extractor returns control characters or shifted letters ("6HFWLRQ" for
    "Section"). Such a page has "text" by character count and none by content; it is treated as a
    page without a text layer (and needs OCR), not as a page that says nothing.

    Rule: a page of at least 150 characters is garbled when 5% or more of them are control
    characters; a page of at least 150 letters or digits is garbled when it has 30 or more
    alphabetic words and none of them is an ordinary English function word. A table of numbers
    has too few words to be judged and is kept.
    """
    control = sum(1 for ch in page if ord(ch) < 32 and ch not in '\n\r\t')
    if len(page.strip()) >= 150 and control >= 0.05 * len(page):
        return True
    if len(re.findall(r'[A-Za-z0-9]', page)) < 150:
        return False
    words = re.findall(r'[A-Za-z]{2,}', page.lower())
    return len(words) >= 30 and not (set(words) & _STOPWORDS)


def label_type(pathway: str, official_label: str | None) -> str | None:
    """The type FDA's own link label implies, or None when there is no label or it is not one of
    the known ones."""
    key = ('pma' if pathway == 'pma_supplement' else pathway, _ws(official_label or '').lower())
    return LABEL_TYPES.get(key)


def classify(pages: list[str], pathway: str, official_label: str | None = None) -> dict[str, Any]:
    """The document's type, with the basis, the rule and the words that decided it.

    basis `text`: a rule matched the text; FDA's link label is recorded beside it and the two are
      compared (`agrees_with_official_label`). The text decides: a file FDA labels "Statement"
      that holds only the clearance letter is a clearance letter.
    basis `official_label_only`: no text layer at all (or only a PDF-portfolio cover notice); the
      label gives the type and the content was not read.
    basis `official_label_unread_pages`: the text read is a letter, FDA labels the file as a
      summary, and the file has pages without a text layer: the summary is on the pages that could
      not be read. The label gives the type; the document is not treated as read.
    basis `official_label_text_unmatched`: the text was read and matches no rule; the label gives
      the type (labeling has no fixed wording).
    basis `none`: no text and no label: `unclassified_no_text`. Text that matches nothing and has
      no label is `other`.
    """
    implied = label_type(pathway, official_label)
    out: dict[str, Any] = {'official_label': official_label, 'type_implied_by_official_label': implied,
                           'components': [], 'rule': None, 'matched_phrase': None, 'page': None,
                           'agrees_with_official_label': None}
    full = '\n'.join(pages)
    if not full.strip() or (_PORTFOLIO_COVER.search(full) and len(full) < 600):
        return {**out, 'document_type': implied or 'unclassified_no_text',
                'basis': 'official_label_only' if implied else 'none'}
    out['components'] = [name for name, rx in _COMPONENTS.items() if rx.search(full)]
    found: dict[str, Any] | None = None
    for doc_type, rx, leading in _RULES.get(pathway, []):
        for n, page in enumerate(pages if leading == 0 else pages[:leading], start=1):
            m = rx.search(page)
            if m:
                found = {'document_type': doc_type, 'rule': rx.pattern, 'matched_phrase': _ws(m.group(0)), 'page': n}
                break
        if found:
            break
    if pathway == '510k' and (found is None or found['document_type'] == '510k_clearance_letter'):
        hits = []
        for rx in _SUMMARY_MARKERS:
            for n, page in enumerate(pages, start=1):
                m = rx.search(page)
                if m:
                    hits.append((n, _ws(m.group(0))))
                    break
        if len(hits) >= SUMMARY_MARKERS_NEEDED:
            found = {'document_type': '510k_summary', 'page': min(n for n, _ in hits),
                     'rule': f'at least {SUMMARY_MARKERS_NEEDED} section markers of a 510(k) summary body',
                     'matched_phrase': ' | '.join(text for _, text in hits)}
    unread = sum(1 for p in pages if len(re.findall(r'[A-Za-z0-9]', p)) < pdftext.MIN_ALNUM_PER_PAGE)
    if found is None:
        if implied:
            return {**out, 'document_type': implied, 'basis': 'official_label_text_unmatched'}
        return {**out, 'document_type': 'other', 'basis': 'text'}
    if found['document_type'] in LETTER_TYPES and implied and implied not in LETTER_TYPES \
            and DOCUMENT_TYPES[implied][1] and unread:
        return {**out, **found, 'document_type': implied, 'basis': 'official_label_unread_pages',
                'text_read_is': found['document_type'], 'n_pages_without_text': unread}
    return {**out, **found, 'basis': 'text',
            'agrees_with_official_label': None if implied is None else implied == found['document_type']}


def demographic_summary_candidate(doc_type: str, text_status: str | None, scope: str,
                                  basis: str = 'text') -> tuple[bool, str]:
    """(candidate?, reason). True only for a substantive summary with readable text that belongs
    to the submission itself. A granting letter is never one, whatever else is missing."""
    _, candidate, why_not = DOCUMENT_TYPES[doc_type]
    if not candidate:
        return False, why_not
    if scope != 'this_submission':
        return False, 'the document belongs to the original PMA, not to this supplement'
    if basis == 'official_label_unread_pages':
        return False, (f'FDA labels the file a {doc_type}, but only its letter has a text layer; the summary pages '
                       f'are scans and need OCR first')
    if text_status not in ('text', 'partial'):
        return False, f'a {doc_type} but its text is not extractable ({text_status}); it needs OCR first'
    return True, f'a {doc_type} with extractable text: the kind of document that describes the study population'


# ---------------------------------------------------------------- identifiers in text

# NCT + eight digits, tolerating what a text layer or a typist puts between: single spaces between
# the letters ("N C T", letter-spaced text), a space, hyphen, number sign, colon or dot after them,
# and single spaces or line breaks inside the digits. The letters must not follow a letter or
# digit, so a word that merely ends in "nct" before a number is not read as an identifier, and the
# eighth digit must not be followed by a letter or digit, so a longer token that begins with an
# NCT id ("NCT01234567ABC") is not read as that id.
_NCT = re.compile(r'(?<![A-Za-z0-9])N\s?C\s?T[\s\-#:.]{0,3}((?:\d\s?){7}\d)(?![A-Za-z0-9])', re.I)
_OTHER_IDS: dict[str, re.Pattern[str]] = {
    'isrctn': re.compile(r'\bISRCTN\s?\d{8}\b'),
    'eudract': re.compile(r'\b20\d{2}-\d{6}-\d{2}\b'),
    'chictr': re.compile(r'\bChiCTR[-\w]{6,20}\b'),
    'actrn': re.compile(r'\bACTRN\d{14}\b'),
    'drks': re.compile(r'\bDRKS\d{8}\b'),
    'jprn_umin': re.compile(r'\b(?:UMIN\d{9}|jRCT[s]?\d{9,10})\b'),
    'ctri': re.compile(r'\bCTRI/\d{4}/\d{2,3}/\d{6}\b'),
    # FDA investigational device exemption numbers: the study ran under an IDE.
    'ide': re.compile(r'\bIDE\s*(?:#|No\.?|number)?\s*:?\s*(G\d{6})\b'),
    'doi': re.compile(r'\b10\.\d{4,9}/[^\s"<>]{4,80}'),
    'pmid': re.compile(r'\bPMID:?\s*(\d{6,9})\b'),
}
_SUBMISSION_NUMBER = re.compile(r'(?<![A-Za-z0-9])(K\d{6}|DEN\d{6}|P\d{6}(?:/S\d{3})?)(?!\d)')


def sentence_around(text: str, start: int, end: int, limit: int = 320) -> str:
    """The sentence holding text[start:end], whitespace collapsed, cut at `limit` characters either
    side. Sentence ends are a full stop, question or exclamation mark followed by a space and a
    capital, digit or bracket; headings and table cells without them are cut by the limit."""
    lo = max(0, start - limit)
    left = text[lo:start]
    cut = [m.end() for m in re.finditer(r'[.!?]\s+(?=[A-Z0-9(\[])', left)]
    lo = lo + cut[-1] if cut else lo
    right = text[end:end + limit]
    m = re.search(r'[.!?](?=\s+[A-Z0-9(\[]|\s*$)', right)
    hi = end + (m.end() if m else len(right))
    return _ws(text[lo:hi])


def scan_identifiers(pages: list[str], own_number: str = '') -> list[dict[str, Any]]:
    """Every study or submission identifier in the text, with its page (1-based) and sentence.

    kind 'nct' is the registry identifier this build links on. The other registries, IDE numbers,
    DOIs and PMIDs are recorded and not followed. kind 'submission_number' lists FDA numbers other
    than the document's own: predicates and reference devices; the text rule cannot tell which,
    and a predicate's study is not this device's study, so they never feed a study link.
    """
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    def add(kind: str, identifier: str, page_no: int, sentence: str, raw: str) -> None:
        if (kind, identifier, page_no) not in seen:
            seen.add((kind, identifier, page_no))
            found.append({'kind': kind, 'identifier': identifier, 'page': page_no, 'sentence': sentence,
                          'raw_match': raw})
    for n, page in enumerate(pages, start=1):
        for m in _NCT.finditer(page):
            add('nct', 'NCT' + re.sub(r'\s', '', m.group(1)), n, sentence_around(page, m.start(), m.end()),
                _ws(m.group(0)))
        for kind, rx in _OTHER_IDS.items():
            for m in rx.finditer(page):
                ident = (m.group(1) if rx.groups else m.group(0)).rstrip('.,;)')
                add(kind, ident, n, sentence_around(page, m.start(), m.end()), _ws(m.group(0)))
        for m in _SUBMISSION_NUMBER.finditer(page):
            if m.group(1) != own_number and m.group(1) != own_number.split('/')[0]:
                add('submission_number', m.group(1), n, sentence_around(page, m.start(), m.end()), m.group(0))
    return found


# ---------------------------------------------------------------- study language

_NEGATED = re.compile(
    r'\bno\s+(?:\w+\s+){0,2}clinical\s+(?:stud(?:y|ies)|testing|tests?|trials?|data|investigations?|evaluations?)\b'
    r'|\bclinical\s+(?:stud(?:y|ies)|testing|tests?|trials?|data|investigations?|evaluations?)\s+'
    r'(?:(?:was|were|is|are|has|have)\s+)?(?:been\s+)?not\s+(?:required|needed|necessary|performed|conducted|'
    r'applicable|submitted|included|relied)'
    r'|\b(?:did|does|do)\s+not\s+(?:require|include|rely on)\s+(?:\w+\s+){0,2}clinical\s+(?:stud|test|trial|data)'
    r'|\bclinical\s+(?:stud(?:y|ies)|testing|tests?|data)\s*[:\-]?\s*(?:not\s+applicable|n/a|none)\b', re.I)
_HUMAN_STUDY = re.compile(
    r'\bclinical\s+(?:stud(?:y|ies)|trials?|investigations?|validation|evaluation)\b'
    r'|\bpivotal\s+(?:stud(?:y|ies)|trials?)\b'
    r'|\b(?:multi-?\s?reader|MRMC|reader)\s+(?:multi-?\s?case\s+)?(?:stud(?:y|ies)|evaluation)\b'
    r'|\b(?:prospective|retrospective)(?:ly)?[\s,\w-]{0,40}?\b(?:stud(?:y|ies)|trials?|cohorts?|collected|enrolled)\b'
    r'|\b(?:subjects|patients|participants)\s+were\s+(?:enrolled|recruited|included|randomi[sz]ed)\b', re.I)
_DATASET_TEST = re.compile(
    r'\bstand-?\s?alone\s+(?:performance|stud(?:y|ies)|testing|evaluation)\b'
    r'|\b\d[\d,]{1,8}\s+(?:unique\s+)?(?:patients|subjects|participants|cases|exams|examinations|scans)\b', re.I)
_DEMOGRAPHIC_TERMS = {
    'race_or_ethnicity': re.compile(r'\b(?:race|racial|ethnicity|ethnic|hispanic|latino|african\s+american|'
                                    r'caucasian|asian)\b', re.I),
    'sex_or_gender': re.compile(r'\b(?:sex|gender|females?|males?|women|men)\b', re.I),
    'age': re.compile(r'\b(?:mean\s+age|median\s+age|age\s+range|years\s+of\s+age|years\s+old|age\s+group)\b', re.I),
}


def _sentences(page: str) -> list[str]:
    return [s for s in re.split(r'(?<=[.!?])\s+(?=[A-Z0-9(\[])', _ws(page)) if s]


def study_language(pages: list[str]) -> dict[str, Any]:
    """Keyword findings about whether the text describes a study, sentence by sentence.

    A sentence that says no clinical study was done ("No clinical studies were required") counts
    only as that; it is not also counted as describing one. Remaining sentences are tested for
    words of a human-subject study (clinical study or trial, pivotal, reader study, prospective or
    retrospective collection, enrolment) and for words of dataset performance testing (standalone
    performance, "N patients/cases/exams"). Up to MAX_EXAMPLES verbatim sentences are kept per kind
    with their pages, and the counts of all.

    These are keyword rules. They find where to read; they do not establish that a study exists,
    that it involved patients, or that it supported the decision.
    """
    kinds: dict[str, dict[str, Any]] = {k: {'n_sentences': 0, 'examples': []} for k in
                                        ('human_subject_study', 'dataset_performance_testing',
                                         'states_no_clinical_study')}
    terms: dict[str, dict[str, Any]] = {k: {'n_mentions': 0, 'first': None} for k in _DEMOGRAPHIC_TERMS}

    def note(kind: str, n: int, sentence: str, m: re.Match[str]) -> None:
        kinds[kind]['n_sentences'] += 1
        if len(kinds[kind]['examples']) < MAX_EXAMPLES:
            kinds[kind]['examples'].append({'page': n, 'matched_phrase': _ws(m.group(0)), 'sentence': sentence[:500]})
    for n, page in enumerate(pages, start=1):
        for sentence in _sentences(page):
            neg = _NEGATED.search(sentence)
            if neg:
                note('states_no_clinical_study', n, sentence, neg)
                continue
            m = _HUMAN_STUDY.search(sentence)
            if m:
                note('human_subject_study', n, sentence, m)
            m = _DATASET_TEST.search(sentence)
            if m:
                note('dataset_performance_testing', n, sentence, m)
        for term, rx in _DEMOGRAPHIC_TERMS.items():
            hits = list(rx.finditer(page))
            terms[term]['n_mentions'] += len(hits)
            if hits and terms[term]['first'] is None:
                terms[term]['first'] = {'page': n, 'matched_phrase': hits[0].group(0),
                                        'sentence': sentence_around(page, hits[0].start(), hits[0].end(), 200)}
    return {**kinds, 'demographic_terms': terms}


def study_description_state(text_status: str | None, identifiers: list[dict[str, Any]],
                            language: dict[str, Any] | None) -> str:
    """One of STUDY_DESCRIPTION_STATES for a document. `describes_study_no_registry_identifier` is
    the negative finding that matters: the text has study language and no NCT id, so no
    identifier route can reach that study from this document."""
    if text_status not in ('text', 'partial') or language is None:
        return 'not_assessable_no_text'
    if any(i['kind'] == 'nct' for i in identifiers):
        return 'cites_registry_identifier'
    if language['human_subject_study']['n_sentences'] or language['dataset_performance_testing']['n_sentences']:
        return 'describes_study_no_registry_identifier'
    if language['states_no_clinical_study']['n_sentences']:
        return 'states_no_clinical_study_only'
    return 'no_study_language_found'


# ---------------------------------------------------------------- the registry

def read_text(cache: Path, digest: str, version: str) -> dict[str, Any] | None:
    """The cached extraction for one document version and extractor version, or None when that
    version has not been extracted (the build records it as not extracted; it does not extract)."""
    path = cache / 'text' / f'{digest}.{version}.json.gz'
    if not path.exists():
        return None
    return json.loads(gzip.decompress(path.read_bytes()))


def store_problem(cache: Path, digest: str, size: int | None) -> str | None:
    """Why the bytes recorded for a retrieved document are not held as recorded, or None: the
    content-addressed file must exist, have the recorded size and hash to its name."""
    import hashlib
    path = cache / 'documents' / 'store' / f'{digest}.pdf'
    if not path.is_file():
        return 'not in the document store'
    if size is not None and path.stat().st_size != size:
        return f'held with {path.stat().st_size} bytes, {size} recorded'
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return None if h.hexdigest() == digest else 'the bytes held do not hash to the recorded digest'


def build(manifest_lines: list[dict[str, Any]], pathway_of: dict[str, str], cache: Path,
          extraction_version: str, labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One record per document id, from the manifest's history and the text cache.

    The latest manifest line gives the document's current state; every line that retrieved bytes
    is listed under `versions`. A document that was not retrieved keeps its failure (status,
    outcome, error) and has no type: a missing document is a finding, recorded as one.

    `labels` ({document id: FDA's link label}) supplies the label for a document that was fetched
    by URL pattern before its database page answered; `discovery` still says how it was found.
    """
    history: dict[str, list[dict[str, Any]]] = {}
    for line in manifest_lines:
        history.setdefault(line['document_id'], []).append(line)
    by_digest: dict[str, list[str]] = {}
    docs: list[dict[str, Any]] = []
    store_checked: dict[str, str | None] = {}
    not_held: list[str] = []
    for did, lines in history.items():
        cur = dict(lines[-1])
        cur['official_label'] = cur.get('official_label') or (labels or {}).get(did)
        sn = cur['submission_number']
        versions = []
        for line in lines:
            if line['outcome'] == 'retrieved' and line['sha256'] not in [v['sha256'] for v in versions]:
                versions.append({'sha256': line['sha256'], 'bytes': line['bytes'], 'retrieved_at': line['retrieved_at'],
                                 'origin': line['origin'], 'source_last_modified': line.get('last_modified'),
                                 'source_etag': line.get('etag')})
        doc: dict[str, Any] = {
            'document_id': did, 'submission_number': sn, 'url': cur['url'], 'scope': cur['scope'],
            'discovery': cur['discovery'], 'official_label': cur.get('official_label'), 'origin': cur['origin'],
            'availability': cur['outcome'], 'http_status': cur['http_status'],
            'http_status_absent_reason': cur.get('http_status_absent_reason'),
            'retrieved_at': cur['retrieved_at'], 'attempts': cur['attempts'], 'error': cur.get('error'),
            'bytes': cur.get('bytes') if cur['outcome'] == 'retrieved' else None,
            'sha256': cur.get('sha256'), 'source_last_modified': cur.get('last_modified'),
            'n_versions': len(versions), 'versions': versions,
            'text': None, 'document_type': None, 'category': None, 'classification': None,
            'demographic_summary_candidate': False,
            'demographic_summary_reason': f"not retrieved ({cur['outcome']})",
            'identifiers': [], 'study_language': None, 'study_description': None, 'same_bytes_as': [],
        }
        if cur['outcome'] == 'retrieved':
            # The bytes a "retrieved" document stands for must still be held as recorded; a
            # build does not publish a document, or links from its text, whose source is gone.
            if cur['sha256'] not in store_checked:
                store_checked[cur['sha256']] = store_problem(cache, cur['sha256'], cur.get('bytes'))
            if store_checked[cur['sha256']]:
                not_held.append(f"{did} ({cur['sha256'][:12]}…): {store_checked[cur['sha256']]}")
            by_digest.setdefault(cur['sha256'], []).append(did)
            text = read_text(cache, cur['sha256'], extraction_version)
            if text is None:
                doc['text'] = {'status': 'not_extracted', 'extraction_version': extraction_version, 'n_pages': None,
                               'n_pages_with_text': None, 'error': 'no cached extraction for this version'}
                doc['demographic_summary_reason'] = 'text not extracted yet'
            else:
                # Garbled pages are blanked before anything reads them: they count as pages without
                # a text layer. `status` is the status after that; the extractor's own is kept.
                n_garbled = sum(1 for p in text['pages'] if garbled(p))
                pages = ['' if garbled(p) else p for p in text['pages']]
                with_text = sum(1 for p in pages if len(re.findall(r'[A-Za-z0-9]', p)) >= pdftext.MIN_ALNUM_PER_PAGE)
                effective = text['status'] if text['status'] == 'failed' else pdftext.status_of(pages)
                doc['text'] = {'status': effective, 'extractor_status': text['status'],
                               'extraction_version': text['extraction_version'],
                               'n_pages': text['n_pages'], 'n_pages_with_text': with_text if text['n_pages'] else None,
                               'n_pages_garbled': n_garbled,
                               'n_page_errors': len(text.get('page_errors') or {}), 'error': text.get('error')}
                text = {**text, 'status': effective}
                if text['status'] != 'failed' and _PORTFOLIO_COVER.search(' '.join(pages)) and \
                        sum(len(p) for p in pages) < 600:
                    # The extractor reads only the cover of a PDF portfolio. That is a parsing
                    # failure of this build, not an empty document.
                    doc['text'].update(status='failed', error='PDF portfolio: only the cover notice has a text layer; '
                                                              'the embedded files were not extracted')
                    text = {**text, 'status': 'failed'}
                    pages = []
                c = classify(pages, pathway_of[sn], cur.get('official_label'))
                doc['document_type'], doc['classification'] = c.pop('document_type'), c
                doc['category'] = DOCUMENT_TYPES[doc['document_type']][0]
                doc['is_supplement_document'] = pathway_of[sn] == 'pma_supplement' and cur['scope'] == 'this_submission'
                ok, why = demographic_summary_candidate(doc['document_type'], text['status'], cur['scope'], c['basis'])
                doc['demographic_summary_candidate'], doc['demographic_summary_reason'] = ok, why
                if text['status'] in ('text', 'partial'):
                    doc['identifiers'] = scan_identifiers(pages, sn)
                    doc['study_language'] = study_language(pages)
                doc['study_description'] = study_description_state(text['status'], doc['identifiers'],
                                                                   doc['study_language'])
        docs.append(doc)
    for doc in docs:
        if doc['sha256']:
            doc['same_bytes_as'] = sorted(d for d in by_digest[doc['sha256']] if d != doc['document_id'])
    if not_held:
        raise ValueError(f'{len(not_held)} retrieved document(s) are not held as the manifest records them, so '
                         'nothing is built from them: ' + '; '.join(not_held[:5]))
    return sorted(docs, key=lambda d: (d['submission_number'], d['url']))
