"""Cross-source evidence link records and curated human-review / statement merging.

Every piece of cross-source evidence for a registered trial (`NCT_ID`) is represented as an
immutable dictionary keyed by `link:<sha256[:16]>` (`make_link_id`), derived deterministically
from `(nct_id, source_route, target_id_type, target_id, citation_type, locator_key, excerpt,
publication_or_action_date)`: everything the link presents as evidence to a reviewer. If any of it
changes in a later fetch (altered text, or a record that gains or changes its publication or FDA
action date while its excerpt stays the same), its `link_id` changes so prior human reviews become
explicitly `orphaned_reviews` rather than silently carrying onto the changed evidence.

The whole-response `source_sha256` is recorded on the link but deliberately kept out of its
identity: a response also changes for reasons unrelated to the linked record (openFDA's
`meta.last_updated` and the other applications a drug query returns, Europe PMC's other hits,
any update to a CT.gov study record, a new row in `documentary_statements.csv`), and keying on it
would orphan every review on every re-acquisition.

Automated checks (`automated_validation`) verify mechanical facts only — whether a cited identifier
exists and resolves, or whether a verbatim quote stands in a retrieved source. Unreviewed links are
NEVER called "validated". Semantic confirmation (`review_status`) comes exclusively from human
entries in `disclosure/reviews.csv` and is never set by code.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from disclosure import acquire as A

try:
    from sponsors import sponsor_roles as _SR  # type: ignore[import-not-found]
except ModuleNotFoundError as _exc:
    if _exc.name != 'pandas':
        raise
    _SR = None

_SPONSOR_SUFFIX_RE = re.compile(
    r'\s+(incorporated|inc|llc|llp|ltd|limited|gmbh|ag|plc|corp|corporation|'
    r'co|company|sa|nv|bv|ab|oy|kk|spa|sas)$'
)
_SUBSIDIARY_RE = re.compile(
    r'^(?P<sub>.+?),?\s+(?:is\s+)?(?:now\s+)?(?:a\s+)?(?:wholly[- ]owned\s+)?'
    r'subsidiar\w+\s+of\s+(?P<parent>.+)$',
    re.IGNORECASE,
)


def normalize_sponsor_name(name: str) -> str:
    """Exact mirror of `sponsors.sponsor_roles.normalize` (mechanical legal-suffix stripping only)."""
    if _SR is not None:
        return str(_SR.normalize(name))
    s = str(name).lower().strip()
    s = re.sub(r'[.,;:()\"\']', ' ', s)
    s = re.sub(r'&', ' and ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SPONSOR_SUFFIX_RE.sub('', s).strip()
        s = re.sub(r'\s+and$', '', s)
    return s


def parsed_sponsor_parent(name: str) -> str | None:
    """Exact mirror of `sponsors.sponsor_roles.parsed_parent`."""
    if _SR is not None:
        res = _SR.parsed_parent(name)
        return str(res) if res is not None else None
    m = _SUBSIDIARY_RE.match(str(name))
    if not m:
        return None
    parent = m.group('parent')
    if _SUBSIDIARY_RE.match(parent):
        return parsed_sponsor_parent(parent)
    parent = re.sub(r'\(.*?\)\s*$', '', parent).strip(' ,.')
    return normalize_sponsor_name(parent)


def parsed_sponsor_entity(name: str) -> str:
    """Exact mirror of `sponsors.sponsor_roles.parsed_entity`."""
    if _SR is not None:
        return str(_SR.parsed_entity(name))
    m = _SUBSIDIARY_RE.match(str(name))
    if not m:
        return normalize_sponsor_name(name)
    return normalize_sponsor_name(m.group('sub'))

SOURCE_ROUTES = (
    'ctgov_results',
    'registry_references',
    'literature_search',
    'openfda_drugsfda',
    'documentary_statements',
)
TARGET_ID_TYPES = (
    'nct_results',
    'pmid',
    'pmcid',
    'doi',
    'literature_record',
    'fda_application',
    'documentary_locator',
)
CITATION_TYPES = (
    'CTGOV_POSTED_RESULTS',
    'RESULT',
    'BACKGROUND',
    'DERIVED',
    'UNSPECIFIED_REFERENCE',
    'LITERATURE_NCT_MENTION',
    'OPENFDA_NCT_CITED',
    'OPENFDA_SPONSOR_DRUG_CANDIDATE',
    'DOCUMENTARY_STATEMENT',
)
DATE_PRECISIONS = ('day', 'month', 'year', 'missing')
AUTOMATED_VALIDATION = (
    'identifier_confirmed',
    'quote_confirmed_in_source',
    'identifier_not_resolved',
    'rejected_by_rule',
    'not_validated',
)
REVIEW_STATUSES = (
    'unreviewed',
    'confirmed_same_trial_results',
    'confirmed_background_only',
    'rejected_false_positive',
    'indeterminate',
)
REVIEW_DECISIONS = REVIEW_STATUSES[1:]

REVIEW_COLUMNS = ['link_id', 'nct_id', 'reviewer', 'reviewed_at', 'decision', 'notes']
STATEMENT_COLUMNS = [
    'statement_id',
    'nct_id',
    'source_type',
    'source_locator',
    'statement_date',
    'verbatim_excerpt',
    'recorded_by',
    'notes',
]
STATEMENT_SOURCE_TYPES = (
    'registry_record',
    'literature_record',
    'openfda_record',
    'fda_review',
    'fda_label',
    'fda_document',
    'csr_synopsis',
    'journal_article',
    'sponsor_statement',
    'regulatory_document',
    'other',
)

OPENFDA_DRUG_FLOOD_THRESHOLD = 25
# Strength of an openFDA drug-query link for one application, strongest first: an exact NCT token in
# the record, then a reviewable candidate, then a candidate set aside by the flood rule.
OPENFDA_CANDIDATE_STRENGTH = {'identifier_confirmed': 0, 'not_validated': 1, 'rejected_by_rule': 2}

LINK_ID_RE = re.compile(r'^link:[0-9a-f]{16}$')
NCT_RE = re.compile(r'^NCT[0-9]{8}\Z')  # ASCII digits, no trailing newline (as acquire.NCT_RE)
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
ISO_DAY_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
ISO_MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')
ISO_YEAR_RE = re.compile(r'^(\d{4})$')
YYYYMMDD_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})$')
PUBMED_DATE_RE = re.compile(
    r'^(\d{4})(?:\s+([A-Za-z]{3,9})(?:\s+(\d{1,2}))?(?:\s*[-/]\s*[A-Za-z0-9]+)?)?$'
)

MONTH_MAP: dict[str, int] = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def squash_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def make_link_id(*parts: Any) -> str:
    """Deterministic `link:<sha256[:16]>` hash over evidence identity components."""
    joined = '|'.join(squash_whitespace(str(p)) for p in parts)
    return 'link:' + hashlib.sha256(joined.encode('utf-8')).hexdigest()[:16]


def normalize_publication_date(raw: str | None) -> tuple[str | None, str]:
    """Normalize a source date string without ever fabricating missing days or months.

    Returns `(canonical_date_or_None, precision)` where `precision` is one of
    `'day'`, `'month'`, `'year'`, or `'missing'`.
    """
    if raw is None:
        return None, 'missing'
    s = raw.strip()
    if not s:
        return None, 'missing'

    m_day = ISO_DAY_RE.match(s)
    if m_day:
        y, mo, d = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
        try:
            return date(y, mo, d).isoformat(), 'day'
        except ValueError:
            return None, 'missing'

    m_compact = YYYYMMDD_RE.match(s)
    if m_compact:
        y, mo, d = int(m_compact.group(1)), int(m_compact.group(2)), int(m_compact.group(3))
        try:
            return date(y, mo, d).isoformat(), 'day'
        except ValueError:
            return None, 'missing'

    m_month = ISO_MONTH_RE.match(s)
    if m_month:
        y, mo = int(m_month.group(1)), int(m_month.group(2))
        if 1800 <= y <= 2200 and 1 <= mo <= 12:
            calendar.monthrange(y, mo)
            return f'{y:04d}-{mo:02d}', 'month'
        return None, 'missing'

    m_year = ISO_YEAR_RE.match(s)
    if m_year:
        y = int(m_year.group(1))
        if 1800 <= y <= 2200:
            return f'{y:04d}', 'year'
        return None, 'missing'

    m_pm = PUBMED_DATE_RE.match(s)
    if m_pm:
        y = int(m_pm.group(1))
        if not (1800 <= y <= 2200):
            return None, 'missing'
        mon_token = (m_pm.group(2) or '').lower()
        day_token = m_pm.group(3)
        if not mon_token:
            return f'{y:04d}', 'year'
        # Multi-month or seasonal PubMed strings such as "2021 May-Jun" or "2021 Spring" stay year-level.
        if '-' in s or '/' in s or mon_token not in MONTH_MAP:
            return f'{y:04d}', 'year'
        mo = MONTH_MAP[mon_token]
        if day_token:
            try:
                return date(y, mo, int(day_token)).isoformat(), 'day'
            except ValueError:
                return f'{y:04d}-{mo:02d}', 'month'
        return f'{y:04d}-{mo:02d}', 'month'

    return None, 'missing'


def _new_link(
    *,
    nct_id: str,
    source_route: str,
    target_id_type: str,
    target_id: str,
    citation_type: str,
    publication_or_action_date: str | None,
    date_precision: str,
    verbatim_excerpt: str,
    source_sha256: str,
    locator: dict[str, Any],
    automated_validation: str,
    validation_reasons: list[str],
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    excerpt = squash_whitespace(verbatim_excerpt)
    locator_key = json.dumps(locator, sort_keys=True, separators=(',', ':'))
    # The date is part of the identity (module docstring): a review keyed on `link_id` must not
    # carry onto a record whose date, which the timeline reads, changed under the same excerpt.
    lid = make_link_id(
        nct_id, source_route, target_id_type, target_id, citation_type, locator_key, excerpt,
        publication_or_action_date,
    )
    return {
        'link_id': lid,
        'nct_id': nct_id,
        'source_route': source_route,
        'target_id_type': target_id_type,
        'target_id': target_id,
        'citation_type': citation_type,
        'publication_or_action_date': publication_or_action_date,
        'date_precision': date_precision,
        'verbatim_excerpt': excerpt,
        'source_sha256': source_sha256,
        'locator': locator,
        'automated_validation': automated_validation,
        'validation_reasons': validation_reasons,
        'review_status': 'unreviewed',
        'review': None,
        'detail': detail or {},
    }


def _record_sha256(rec: dict[str, Any]) -> str:
    existing = rec.get('response_sha256')
    if isinstance(existing, str) and HEX64_RE.match(existing):
        return existing
    return A.sha256_bytes(A.canonical_json_bytes(rec.get('body')))


def _walk_strings(obj: Any, path: str = '') -> Iterator[tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            nxt = f'{path}.{k}' if path else str(k)
            yield from _walk_strings(v, nxt)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            yield from _walk_strings(v, f'{path}[{idx}]')
    elif isinstance(obj, str):
        yield path, obj


def _iter_exact_nct(obj: Any, nct_id: str, window: int = 160) -> Iterator[tuple[str, str]]:
    """Every string in `obj` holding an exact token occurrence of `nct_id`, in traversal order, as
    `(json_path, excerpt)`; the excerpt surrounds the first occurrence in that string."""
    pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(nct_id) + r'(?![A-Za-z0-9])', re.IGNORECASE)
    for path, text in _walk_strings(obj):
        m = pattern.search(text)
        if m:
            lo = max(0, m.start() - window)
            hi = min(len(text), m.end() + window)
            yield path, squash_whitespace(text[lo:hi])


def _find_exact_nct(obj: Any, nct_id: str, window: int = 160) -> tuple[str, str] | None:
    """Locate the first exact token occurrence of `nct_id` in `obj`; return `(json_path, excerpt)`."""
    return next(_iter_exact_nct(obj, nct_id, window), None)


def resolve_json_locator(obj: Any, locator: str) -> str | None:
    """Resolve dotted path (`a.b[0].c`) in `obj`; return string or JSON text, else None."""
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\[\d+\])*(?:\.[A-Za-z_]\w*(?:\[\d+\])*)*', locator or ''):
        return None
    node = obj
    for part in re.findall(r'[^.\[\]]+|\[\d+\]', locator):
        try:
            node = node[int(part[1:-1])] if part.startswith('[') else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    if node is None or node is obj:
        return None
    return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- route 1: CT.gov results

def build_ctgov_results_links(nct_id: str, ctgov_rec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Emit a verified `ctgov_results` link when the retrieved study holds a posted resultsSection."""
    if ctgov_rec is None or ctgov_rec.get('status') != 200:
        return []
    body = ctgov_rec.get('body')
    if not A.ctgov_study_record_valid(ctgov_rec) or body.get('hasResults') is not True:
        return []
    rs = body.get('resultsSection')
    if not isinstance(rs, dict) or not rs:
        return []
    ps = body.get('protocolSection') or {}
    sm = ps.get('statusModule') or {}
    post_raw = (sm.get('resultsFirstPostDateStruct') or {}).get('date')
    pub_date, prec = normalize_publication_date(post_raw if isinstance(post_raw, str) else None)
    title = ((ps.get('identificationModule') or {}).get('briefTitle') or nct_id)
    modules_present = sorted(k for k, v in rs.items() if isinstance(v, dict) and v)
    excerpt = f"{nct_id} posted resultsSection ({', '.join(modules_present) or 'present'}): {title}"
    return [
        _new_link(
            nct_id=nct_id,
            source_route='ctgov_results',
            target_id_type='nct_results',
            target_id=nct_id,
            citation_type='CTGOV_POSTED_RESULTS',
            publication_or_action_date=pub_date,
            date_precision=prec,
            verbatim_excerpt=excerpt,
            source_sha256=_record_sha256(ctgov_rec),
            locator={'json_path': 'resultsSection'},
            automated_validation='identifier_confirmed',
            validation_reasons=[
                'ClinicalTrials.gov study record sets hasResults=true and carries a non-empty resultsSection'
            ],
            detail={'modules_present': modules_present},
        )
    ]


# ---------------------------------------------------------------- route 2: registry references

def _pubmed_summary_date(entry: dict[str, Any]) -> tuple[str | None, str]:
    for field in ('epubdate', 'pubdate', 'sortpubdate'):
        val = entry.get(field)
        if isinstance(val, str) and val.strip():
            raw = val.strip().split(' ')[0] if field == 'sortpubdate' and '/' in val else val.strip()
            raw = raw.replace('/', '-') if field == 'sortpubdate' else raw
            norm, prec = normalize_publication_date(raw)
            if norm is not None:
                return norm, prec
    return None, 'missing'


def build_registry_reference_links(
    nct_id: str,
    ctgov_rec: dict[str, Any] | None,
    literature_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build links from `protocolSection.referencesModule.references`, resolving PMIDs via PubMed."""
    if ctgov_rec is None or ctgov_rec.get('status') != 200:
        return []
    body = ctgov_rec.get('body')
    if not A.ctgov_study_record_valid(ctgov_rec):
        return []
    ctgov_sha = _record_sha256(ctgov_rec)
    out: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()

    for ref in A.extract_registry_references(body):
        pmid = ref['pmid']
        ref_type = ref['type']
        citation_type = ref_type if ref_type in ('RESULT', 'BACKGROUND', 'DERIVED') else 'UNSPECIFIED_REFERENCE'
        citation_text = ref['citation'] or (f'PMID:{pmid}' if pmid else f'{nct_id} reference[{ref["index"]}]')
        dedupe_key = (pmid or citation_text, citation_type)
        if dedupe_key in seen_targets:
            continue
        seen_targets.add(dedupe_key)

        if pmid and A.PMID_RE.match(pmid):
            pm_rec = literature_records.get(f'literature/pubmed_pmid/{pmid}')
            if pm_rec is None:
                out.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='registry_references',
                        target_id_type='pmid',
                        target_id=pmid,
                        citation_type=citation_type,
                        publication_or_action_date=None,
                        date_precision='missing',
                        verbatim_excerpt=citation_text,
                        source_sha256=ctgov_sha,
                        locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                        automated_validation='not_validated',
                        validation_reasons=[
                            'PMID listed in protocolSection.referencesModule; PubMed esummary lookup not in cache'
                        ],
                        detail={'retractions': ref['retractions']},
                    )
                )
            elif pm_rec.get('status') == 404:
                out.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='registry_references',
                        target_id_type='pmid',
                        target_id=pmid,
                        citation_type=citation_type,
                        publication_or_action_date=None,
                        date_precision='missing',
                        verbatim_excerpt=citation_text,
                        source_sha256=_record_sha256(pm_rec),
                        locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                        automated_validation='identifier_not_resolved',
                        validation_reasons=['PubMed esummary answered HTTP 404 for the cited PMID'],
                        detail={'retractions': ref['retractions']},
                    )
                )
            # The acquisition rule (`A.pubmed_esummary_valid`), applied again to a cache frozen before it:
            # an entry without a matching `uid` or an `error` is not evidence that the PMID resolved.
            elif pm_rec.get('status') == 200 and A.pubmed_esummary_valid(pm_rec.get('body'), pmid):
                entry = pm_rec['body']['result'][pmid]
                if entry.get('error'):
                    out.append(
                        _new_link(
                            nct_id=nct_id,
                            source_route='registry_references',
                            target_id_type='pmid',
                            target_id=pmid,
                            citation_type=citation_type,
                            publication_or_action_date=None,
                            date_precision='missing',
                            verbatim_excerpt=citation_text,
                            source_sha256=_record_sha256(pm_rec),
                            locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                            automated_validation='identifier_not_resolved',
                            validation_reasons=[f"PubMed esummary reported unresolved PMID: {entry.get('error')}"],
                            detail={'retractions': ref['retractions']},
                        )
                    )
                else:
                    pub_date, prec = _pubmed_summary_date(entry)
                    pm_title = squash_whitespace(str(entry.get('title') or ''))
                    excerpt = citation_text if citation_text else (pm_title or f'PMID:{pmid}')
                    out.append(
                        _new_link(
                            nct_id=nct_id,
                            source_route='registry_references',
                            target_id_type='pmid',
                            target_id=pmid,
                            citation_type=citation_type,
                            publication_or_action_date=pub_date,
                            date_precision=prec,
                            verbatim_excerpt=excerpt,
                            source_sha256=_record_sha256(pm_rec),
                            locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                            automated_validation='identifier_confirmed',
                            validation_reasons=[
                                'PMID cited in protocolSection.referencesModule resolves in NCBI PubMed esummary; '
                                'automated check confirms identifier resolution only, not whether it reports primary results'
                            ],
                            detail={
                                'pubmed_title': pm_title or None,
                                'journal': entry.get('fulljournalname') or entry.get('source') or None,
                                'retractions': ref['retractions'],
                            },
                        )
                    )
            else:
                out.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='registry_references',
                        target_id_type='pmid',
                        target_id=pmid,
                        citation_type=citation_type,
                        publication_or_action_date=None,
                        date_precision='missing',
                        verbatim_excerpt=citation_text,
                        source_sha256=ctgov_sha,
                        locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                        automated_validation='not_validated',
                        validation_reasons=['PubMed esummary lookup did not return a usable final response'],
                        detail={'retractions': ref['retractions']},
                    )
                )
        else:
            # Citation string without a valid numeric PMID (or malformed PMID string).
            validation = 'identifier_not_resolved' if pmid else 'not_validated'
            reason = (
                f'malformed non-numeric PMID {pmid!r} cannot resolve on PubMed'
                if pmid
                else 'registry reference carries free-text citation only, with no PMID to resolve mechanically'
            )
            out.append(
                _new_link(
                    nct_id=nct_id,
                    source_route='registry_references',
                    target_id_type='pmid' if pmid else 'literature_record',
                    target_id=pmid or f'ref:{nct_id}:{ref["index"]}',
                    citation_type=citation_type,
                    publication_or_action_date=None,
                    date_precision='missing',
                    verbatim_excerpt=citation_text,
                    source_sha256=ctgov_sha,
                    locator={'json_path': f'protocolSection.referencesModule.references[{ref["index"]}]'},
                    automated_validation=validation,
                    validation_reasons=[reason],
                    detail={'retractions': ref['retractions']},
                )
            )
    return out


# ---------------------------------------------------------------- route 3: Europe PMC search

def _epmc_target(hit: dict[str, Any], fallback_idx: int) -> tuple[str, str]:
    pmid = str(hit.get('pmid') or '').strip()
    if pmid and A.PMID_RE.match(pmid):
        return 'pmid', pmid
    pmcid = str(hit.get('pmcid') or '').strip()
    if pmcid:
        return 'pmcid', pmcid
    doi = str(hit.get('doi') or '').strip()
    if doi:
        return 'doi', doi
    rec_id = str(hit.get('id') or f'epmc:{fallback_idx}').strip()
    return 'literature_record', rec_id


def _epmc_year(value: Any) -> str | None:
    """A Europe PMC year field as `YYYY`, or None: four digits, as a string or an integer.

    The field was turned into text and passed to `normalize_publication_date`, which also reads the
    compact day `YYYYMMDD`, so `pubYear: 20250102` became the day 2025-01-02 and could be published as a
    trial's earliest results publication. A year field names a year and nothing finer.
    """
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, int) else value.strip() if isinstance(value, str) else ''
    return text if re.fullmatch(r'[0-9]{4}', text) else None


def _epmc_date(hit: dict[str, Any]) -> tuple[str | None, str]:
    """A Europe PMC hit's publication date: its first or electronic publication date, else the year of
    `pubYear` or `journalInfo.yearOfPublication` (`_epmc_year`), else missing."""
    for key in ('firstPublicationDate', 'electronicPublicationDate'):
        val = hit.get(key)
        if isinstance(val, str) and val.strip():
            norm, prec = normalize_publication_date(val)
            if norm is not None:
                return norm, prec
    journal = hit.get('journalInfo')
    for raw in (hit.get('pubYear'), journal.get('yearOfPublication') if isinstance(journal, dict) else None):
        year = _epmc_year(raw)
        if year is not None:
            norm, prec = normalize_publication_date(year)
            if norm is not None:
                return norm, prec
    return None, 'missing'


def build_literature_search_links(
    nct_id: str,
    literature_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build links from Europe PMC search results for `nct_id`."""
    rec = literature_records.get(f'literature/epmc_nct/{nct_id}')
    if rec is None or rec.get('status') != 200:
        return []
    body = rec.get('body')
    if not A.epmc_search_valid(body):
        return []
    rec_sha = _record_sha256(rec)
    results = body['resultList']['result']
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for idx, hit in enumerate(results):
        target_type, target_id = _epmc_target(hit, idx)
        if (target_type, target_id) in seen:
            continue
        seen.add((target_type, target_id))

        pub_date, prec = _epmc_date(hit)
        located = _find_exact_nct(hit, nct_id)
        title = squash_whitespace(str(hit.get('title') or target_id))
        if located is not None:
            field_path, snippet = located
            out.append(
                _new_link(
                    nct_id=nct_id,
                    source_route='literature_search',
                    target_id_type=target_type,
                    target_id=target_id,
                    citation_type='LITERATURE_NCT_MENTION',
                    publication_or_action_date=pub_date,
                    date_precision=prec,
                    verbatim_excerpt=snippet,
                    source_sha256=rec_sha,
                    locator={'json_path': f'resultList.result[{idx}].{field_path}'},
                    automated_validation='identifier_confirmed',
                    validation_reasons=[
                        f'exact {nct_id} token confirmed in Europe PMC record field {field_path}; '
                        'automated check confirms citation presence only, not whether results are reported'
                    ],
                    detail={'title': title, 'pub_type': hit.get('pubType')},
                )
            )
        else:
            # Europe PMC full-text index matched, but no returned metadata/abstract field carries NCT_ID.
            out.append(
                _new_link(
                    nct_id=nct_id,
                    source_route='literature_search',
                    target_id_type=target_type,
                    target_id=target_id,
                    citation_type='LITERATURE_NCT_MENTION',
                    publication_or_action_date=pub_date,
                    date_precision=prec,
                    verbatim_excerpt=title,
                    source_sha256=rec_sha,
                    locator={'json_path': f'resultList.result[{idx}].title'},
                    automated_validation='rejected_by_rule',
                    validation_reasons=[
                        f'returned by Europe PMC search for {nct_id}, but exact {nct_id} token stands in '
                        'no retrieved metadata or abstract field'
                    ],
                    detail={'title': title, 'pub_type': hit.get('pubType')},
                )
            )
    return out


# ---------------------------------------------------------------- route 4: openFDA Drugs@FDA

def _submission_date(sub: Any) -> tuple[str | None, str]:
    """A Drugs@FDA submission's `submission_status_date`, normalized, or `(None, 'missing')`.

    Only a string is read. The value had been passed through `str()`, so a number such as 20250102
    became the date 2025-01-02 and was published as an FDA action date; a value of another type is not
    a date the source states, so the date is unknown, as it is for a string that does not parse.
    """
    raw = sub.get('submission_status_date') if isinstance(sub, dict) else None
    if not isinstance(raw, str):
        return None, 'missing'
    norm, prec = normalize_publication_date(raw)
    return (norm, prec) if norm is not None else (None, 'missing')


def _retrieved_day(rec: dict[str, Any]) -> str | None:
    """The `YYYY-MM-DD` day a cached record was fetched, from its `fetched_at`, or None."""
    fetched = rec.get('fetched_at')
    return fetched[:10] if isinstance(fetched, str) and ISO_DAY_RE.match(fetched[:10]) else None


def _after_retrieval(date_text: str, retrieved_day: str | None) -> bool:
    """Whether a normalized date starts after the day its record was retrieved. A year or month that
    contains that day is a prefix of it and sorts before it, so it is not after."""
    return retrieved_day is not None and date_text > retrieved_day


def _openfda_action_date(app: dict[str, Any], retrieved_day: str | None = None) -> tuple[str | None, str]:
    """Select original approval (`ORIG` + `AP`) date, else earliest `AP` date, else earliest submission date.

    A submission without a readable date (`_submission_date`), or dated after `retrieved_day`, the day
    the record was fetched (`_after_retrieval`), takes no part in the choice."""
    subs = app.get('submissions')
    if not isinstance(subs, list) or not subs:
        return None, 'missing'
    orig_ap: list[tuple[str, str]] = []
    any_ap: list[tuple[str, str]] = []
    any_dated: list[tuple[str, str]] = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        norm, prec = _submission_date(sub)
        if norm is None or _after_retrieval(norm, retrieved_day):
            continue
        any_dated.append((norm, prec))
        if str(sub.get('submission_status') or '').upper() == 'AP':
            any_ap.append((norm, prec))
            if str(sub.get('submission_type') or '').upper() == 'ORIG':
                orig_ap.append((norm, prec))
    chosen = sorted(orig_ap) or sorted(any_ap) or sorted(any_dated)
    return chosen[0] if chosen else (None, 'missing')


_CITED_SUBMISSION_RX = re.compile(r'submissions\[(\d+)\]')


def _cited_submission_date(app: dict[str, Any], field_path: str) -> tuple[str | None, str]:
    """The action date of the Drugs@FDA submission whose record carries the NCT ID, or `(None, 'missing')`.

    An application's original approval is not the action a trial supported when the trial is cited
    in a later supplement: an ORIG approval in 2000 beside a 2025 supplement citing the trial gave the
    link the 2000 date, and the timeline an FDA action years before the trial ended. So the date is
    the cited submission's `submission_status_date`, and an NCT ID found outside any submission (the
    application's own fields, its products) ties the trial to no action, so its date is unknown. So
    does a cited submission whose date is not a readable date string (`_submission_date`).
    """
    match = _CITED_SUBMISSION_RX.match(field_path)
    subs = app.get('submissions')
    if match is None or not isinstance(subs, list) or int(match.group(1)) >= len(subs):
        return None, 'missing'
    return _submission_date(subs[int(match.group(1))])


def _nct_citation(
    app: dict[str, Any], nct_id: str, app_no: str, retrieved_day: str | None = None
) -> tuple[str, str, str | None, str, list[str]] | None:
    """Where a Drugs@FDA record cites `nct_id` and the FDA action tied to it, for the direct NCT query and
    the drug-term queries alike: `(field_path, excerpt, date, precision, validation_reasons)`, or None
    when no exact token stands in the record.

    An application can name the trial in its own fields and in several submissions. Taking the first
    mention in traversal order let field order decide the date: a mention in the application's own
    fields ahead of a dated submission citing the trial gave a missing date, and two citing submissions
    gave whichever was listed first rather than the first action. So every exact occurrence is read,
    and the citing submission with the earliest action date (`_cited_submission_date`) is the link's
    locator and date; equal dates keep traversal order. The dates are normalized strings, so a year or
    month sorts before any day inside it, that is, by its earliest possible day. Only when no citing
    submission is dated does the link fall back to the first mention, with a missing date.

    A submission dated after `retrieved_day`, the day the record was fetched, is no action the record
    can show: `2100-01-02` in a record fetched in 2026 was published as the trial's earliest FDA action.
    Such a date is not used, and the link's reasons name it (`_after_retrieval`).
    """
    mentions = list(_iter_exact_nct(app, nct_id))
    if not mentions:
        return None
    dated: list[tuple[str, int, str, str, str]] = []
    future: list[str] = []
    for order, (path, excerpt) in enumerate(mentions):
        cited_date, precision = _cited_submission_date(app, path)
        if cited_date is not None and _after_retrieval(cited_date, retrieved_day):
            future.append(f'{nct_id} is cited in a submission dated {cited_date} ({path}), after the record was '
                          f'retrieved on {retrieved_day}, so that date is not used')
        elif cited_date is not None:
            dated.append((cited_date, order, path, excerpt, precision))
    if not dated:
        path, excerpt = mentions[0]
        return path, excerpt, None, 'missing', [
            f'exact {nct_id} token stands in openFDA Drugs@FDA record {app_no} ({path})',
            *future,
            f'{nct_id} is not cited in a submission with a readable action date (a `submission_status_date` '
            'string), so no FDA action date is tied to it',
        ]
    cited_date, _, path, excerpt, precision = min(dated)
    reasons = [f'exact {nct_id} token stands in openFDA Drugs@FDA record {app_no} ({path})', *future]
    # A dated path starts with its submission, `submissions[i].`, so the prefix counts submissions.
    n_dated = len({p.split('.', 1)[0] for _, _, p, _, _ in dated})
    if n_dated > 1:
        reasons.append(f'{nct_id} is cited in {n_dated} dated submissions; the earliest action date is used')
    return path, excerpt, cited_date, precision, reasons


def _openfda_sponsors_norm(app: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    sp = app.get('sponsor_name')
    if isinstance(sp, str) and sp.strip():
        norm = normalize_sponsor_name(sp)
        if norm:
            names.add(norm)
    ofda = app.get('openfda')
    if isinstance(ofda, dict):
        mfrs = ofda.get('manufacturer_name')
        if isinstance(mfrs, list):
            for mfr in mfrs:
                if isinstance(mfr, str) and mfr.strip():
                    norm = normalize_sponsor_name(mfr)
                    if norm:
                        names.add(norm)
    return names


def trial_sponsor_match_keys(
    study: dict[str, Any] | None,
    trial_canonicals: Iterable[str] = (),
    trial_entities: Iterable[str] = (),
) -> set[str]:
    """Normalized exact sponsor/entity/canonical tokens for deterministic FDA applicant comparison.

    Uses `sponsors.sponsor_roles.normalize` and curated `canonical`/`entity` tokens only — zero
    substring matching.
    """
    keys: set[str] = (
        {normalize_sponsor_name(c) for c in trial_canonicals if c}
        | {normalize_sponsor_name(e) for e in trial_entities if e}
    )
    if isinstance(study, dict):
        sc = (study.get('protocolSection') or {}).get('sponsorCollaboratorsModule') or {}
        lead = (sc.get('leadSponsor') or {}).get('name')
        if isinstance(lead, str) and lead.strip():
            keys.add(normalize_sponsor_name(lead))
            keys.add(parsed_sponsor_entity(lead))
        for collab in sc.get('collaborators') or []:
            if isinstance(collab, dict) and isinstance(collab.get('name'), str) and collab['name'].strip():
                keys.add(normalize_sponsor_name(collab['name']))
                keys.add(parsed_sponsor_entity(collab['name']))
    keys.discard('')
    return keys


def build_openfda_links(
    nct_id: str,
    study: dict[str, Any] | None,
    openfda_records: dict[str, dict[str, Any]],
    sponsor_match_keys: set[str],
    rules_canonicals_by_norm: dict[str, set[str]] | None = None,
    trial_canonicals: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build direct NCT-cited and sponsor/drug candidate links from frozen openFDA Drugs@FDA responses."""
    out: list[dict[str, Any]] = []
    confirmed_apps: set[str] = set()

    # 1. Direct NCT query (`openfda/nct/{nct_id}`)
    nct_rec = openfda_records.get(f'openfda/nct/{nct_id}')
    if nct_rec is not None and nct_rec.get('status') == 200 and A.openfda_drugsfda_valid(nct_rec.get('body')):
        rec_sha = _record_sha256(nct_rec)
        nct_day = _retrieved_day(nct_rec)
        for idx, app in enumerate(nct_rec['body']['results']):
            app_no = squash_whitespace(str(app.get('application_number') or f'app:{idx}'))
            act_date, prec = _openfda_action_date(app, nct_day)
            cited = _nct_citation(app, nct_id, app_no, nct_day)
            if cited is not None:
                field_path, snippet, cited_date, cited_prec, reasons = cited
                confirmed_apps.add(app_no)
                out.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_NCT_CITED',
                        publication_or_action_date=cited_date,
                        date_precision=cited_prec,
                        verbatim_excerpt=snippet,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}].{field_path}', 'application_number': app_no},
                        automated_validation='identifier_confirmed',
                        validation_reasons=reasons,
                        detail={'sponsor_name': app.get('sponsor_name')},
                    )
                )
            else:
                excerpt = squash_whitespace(f"{app_no} ({app.get('sponsor_name') or 'unknown sponsor'})")
                out.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_NCT_CITED',
                        publication_or_action_date=act_date,
                        date_precision=prec,
                        verbatim_excerpt=excerpt,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}].application_number', 'application_number': app_no},
                        automated_validation='rejected_by_rule',
                        validation_reasons=[
                            f'returned by openFDA search for {nct_id}, but exact {nct_id} token stands in '
                            'no retrieved field of the application record'
                        ],
                        detail={'sponsor_name': app.get('sponsor_name')},
                    )
                )

    # 2. Candidate drug intervention queries (`openfda/drug/{slug}`)
    # Several drug queries can return the same application. Every (application, query) pair is
    # classified into `pair_links` first; one link per application is kept afterwards.
    canon_lookup = rules_canonicals_by_norm or {}
    wanted_canon = trial_canonicals or set()
    pair_links: list[dict[str, Any]] = []

    for drug in A.extract_drug_candidates(study):
        drec = openfda_records.get(A.drug_query_key(drug))
        if drec is None or drec.get('status') != 200 or not A.openfda_drugsfda_valid(drec.get('body')):
            continue
        rec_sha = _record_sha256(drec)
        drug_day = _retrieved_day(drec)
        total = int(drec['body']['meta']['results']['total'])
        flooded = total >= OPENFDA_DRUG_FLOOD_THRESHOLD
        for idx, app in enumerate(drec['body']['results']):
            app_no = squash_whitespace(str(app.get('application_number') or f'app:{drug}:{idx}'))
            if app_no in confirmed_apps:
                continue
            act_date, prec = _openfda_action_date(app, drug_day)
            cited = _nct_citation(app, nct_id, app_no, drug_day)
            if cited is not None:
                # The same date rule as the direct NCT query: the cited submission's, never the original
                # approval's (a trial cited in a 2025 supplement was given the 2000 approval date here).
                field_path, snippet, cited_date, cited_prec, reasons = cited
                pair_links.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_NCT_CITED',
                        publication_or_action_date=cited_date,
                        date_precision=cited_prec,
                        verbatim_excerpt=snippet,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}].{field_path}', 'application_number': app_no},
                        automated_validation='identifier_confirmed',
                        validation_reasons=reasons,
                        detail={'matched_drug': drug, 'sponsor_name': app.get('sponsor_name')},
                    )
                )
                continue

            fda_norms = _openfda_sponsors_norm(app)
            fda_canonicals: set[str] = set()
            for fn in fda_norms:
                fda_canonicals |= canon_lookup.get(fn, set())
            sponsor_agrees = bool((fda_norms & sponsor_match_keys) or (wanted_canon and (fda_canonicals & wanted_canon)))
            products = app.get('products') if isinstance(app.get('products'), list) else []
            brand_names = sorted({
                str(prod.get('brand_name')).strip()
                for prod in products
                if isinstance(prod, dict) and prod.get('brand_name')
            })
            excerpt = squash_whitespace(
                f"{app_no} | applicant={app.get('sponsor_name') or 'unknown'} | "
                f"drug_query={drug} | brands={', '.join(brand_names[:3]) or 'none'}"
            )
            if sponsor_agrees:
                pair_links.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_SPONSOR_DRUG_CANDIDATE',
                        publication_or_action_date=act_date,
                        date_precision=prec,
                        verbatim_excerpt=excerpt,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}]', 'application_number': app_no, 'drug_query': drug},
                        automated_validation='not_validated',
                        validation_reasons=[
                            'candidate NDA/BLA matched by investigational drug term and normalized/curated sponsor '
                            'agreement; openFDA Drugs@FDA metadata carries no NCT ID, so human review is required'
                        ],
                        detail={
                            'matched_drug': drug,
                            'sponsor_name': app.get('sponsor_name'),
                            'sponsor_agreement': True,
                            'query_total_applications': total,
                        },
                    )
                )
            elif flooded:
                pair_links.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_SPONSOR_DRUG_CANDIDATE',
                        publication_or_action_date=act_date,
                        date_precision=prec,
                        verbatim_excerpt=excerpt,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}]', 'application_number': app_no, 'drug_query': drug},
                        automated_validation='rejected_by_rule',
                        validation_reasons=[
                            f'drug query {drug!r} matched {total} (>= {OPENFDA_DRUG_FLOOD_THRESHOLD}) openFDA '
                            'applications and FDA applicant does not match trial sponsor/collaborator'
                        ],
                        detail={
                            'matched_drug': drug,
                            'sponsor_name': app.get('sponsor_name'),
                            'sponsor_agreement': False,
                            'query_total_applications': total,
                        },
                    )
                )
            else:
                pair_links.append(
                    _new_link(
                        nct_id=nct_id,
                        source_route='openfda_drugsfda',
                        target_id_type='fda_application',
                        target_id=app_no,
                        citation_type='OPENFDA_SPONSOR_DRUG_CANDIDATE',
                        publication_or_action_date=act_date,
                        date_precision=prec,
                        verbatim_excerpt=excerpt,
                        source_sha256=rec_sha,
                        locator={'json_path': f'results[{idx}]', 'application_number': app_no, 'drug_query': drug},
                        automated_validation='not_validated',
                        validation_reasons=[
                            'candidate NDA/BLA matched on investigational drug term without sponsor agreement '
                            '(possible licensing/acquisition or unrelated product); requires human review'
                        ],
                        detail={
                            'matched_drug': drug,
                            'sponsor_name': app.get('sponsor_name'),
                            'sponsor_agreement': False,
                            'query_total_applications': total,
                        },
                    )
                )

    # Keep one link per application: the strongest classification (`OPENFDA_CANDIDATE_STRENGTH`),
    # ties broken by position in `pair_links`, which runs in sorted query-term order and then result
    # index. Keeping whichever query reached an application first would let a flooded generic query's
    # `rejected_by_rule` hide a precise query's reviewable candidate for the same application. Every
    # query that returned the application is recorded on the kept link.
    best: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    queries_by_app: dict[str, set[str]] = {}
    for pos, lnk in enumerate(pair_links):
        app_no = lnk['target_id']
        queries_by_app.setdefault(app_no, set()).add(lnk['detail']['matched_drug'])
        key = (OPENFDA_CANDIDATE_STRENGTH[lnk['automated_validation']], pos)
        if app_no not in best or key < best[app_no][0]:
            best[app_no] = (key, lnk)
    for _, lnk in sorted(best.values(), key=lambda item: item[0][1]):
        lnk['detail']['drug_queries_returning_application'] = sorted(queries_by_app[lnk['target_id']])
        out.append(lnk)
    return out


# ---------------------------------------------------------------- route 5: documentary statements

def read_statements(path: Path) -> tuple[list[dict[str, str]], str]:
    """Read and validate `disclosure/documentary_statements.csv`; return `(rows, file_sha256)`."""
    raw = path.read_bytes()
    file_sha = A.sha256_bytes(raw)
    with path.open(encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != STATEMENT_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {STATEMENT_COLUMNS}')
        rows = list(reader)
    seen_ids: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        sid = (row.get('statement_id') or '').strip()
        nct = (row.get('nct_id') or '').strip()
        stype = (row.get('source_type') or '').strip()
        sloc = (row.get('source_locator') or '').strip()
        sdate = (row.get('statement_date') or '').strip()
        excerpt = (row.get('verbatim_excerpt') or '').strip()
        rec_by = (row.get('recorded_by') or '').strip()
        notes = (row.get('notes') or '').strip()
        if not sid or not NCT_RE.match(nct) or stype not in STATEMENT_SOURCE_TYPES or not sloc or not excerpt or not rec_by or not notes:
            raise ValueError(
                f'{path.name} line {line_no}: requires statement_id, valid nct_id, valid source_type '
                f'({STATEMENT_SOURCE_TYPES}), source_locator, verbatim_excerpt, recorded_by, and notes'
            )
        if sid in seen_ids:
            raise ValueError(f'{path.name} line {line_no}: duplicate statement_id {sid!r}')
        seen_ids.add(sid)
        if sdate:
            norm, _ = normalize_publication_date(sdate)
            if norm is None:
                raise ValueError(f'{path.name} line {line_no}: invalid statement_date {sdate!r}')
    return rows, file_sha


def _locate_statement_source(
    row: dict[str, str],
    ctgov_records: dict[str, dict[str, Any]],
    literature_records: dict[str, dict[str, Any]],
    openfda_records: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, str]:
    """Return `(text_at_locator, source_sha256, status)` for a statement's frozen source.

    `status` is `resolved`, `locator_not_resolved` (the source is in the cache but `source_locator`
    names no value in it), or `source_not_retrieved`. A locator that does not resolve never falls
    back to the whole response: an excerpt found elsewhere in the body does not stand at the locator.
    The one locator that names a whole record is `pmid:<PMID>` with no sub-path, which resolves to
    that PMID's own PubMed esummary record, `body['result'][PMID]`; `pmid:<PMID>:<path>` resolves the
    path inside that record. Both had read the whole response: with no sub-path an excerpt standing in
    a sibling PMID's entry, or in the response's metadata, was confirmed at `pmid:<PMID>`, and a sub-path
    could not reach the PMID's entry at all (a path part cannot start with a digit). A response without
    that PMID's entry, or whose entry is NCBI's error for an unresolved PMID, does not resolve.
    """
    nct_id = row['nct_id'].strip()
    stype = row['source_type'].strip()
    locator = row['source_locator'].strip()

    def _at_locator(rec: dict[str, Any], path: str) -> tuple[str | None, str | None, str]:
        text = resolve_json_locator(rec['body'], path)
        return text, _record_sha256(rec), 'resolved' if text is not None else 'locator_not_resolved'

    if stype == 'registry_record':
        rec = ctgov_records.get(f'ctgov/study/{nct_id}')
        if rec and rec.get('status') == 200 and isinstance(rec.get('body'), dict):
            return _at_locator(rec, locator)
        return None, None, 'source_not_retrieved'

    if stype in ('literature_record', 'journal_article'):
        if locator.startswith('pmid:'):
            pmid, _, subpath = locator[5:].partition(':')
            rec = literature_records.get(f'literature/pubmed_pmid/{pmid}')
            if rec and rec.get('status') == 200 and isinstance(rec.get('body'), dict):
                result = rec['body'].get('result')
                entry = result.get(pmid) if isinstance(result, dict) else None
                if not isinstance(entry, dict) or entry.get('uid') != pmid or 'error' in entry:
                    return None, _record_sha256(rec), 'locator_not_resolved'
                text = (
                    json.dumps(entry, ensure_ascii=False, sort_keys=True) if not subpath
                    else resolve_json_locator(entry, subpath)
                )
                return text, _record_sha256(rec), 'resolved' if text is not None else 'locator_not_resolved'
        rec = literature_records.get(f'literature/epmc_nct/{nct_id}')
        if rec and rec.get('status') == 200 and isinstance(rec.get('body'), dict):
            return _at_locator(rec, locator)
        return None, None, 'source_not_retrieved'

    if stype == 'openfda_record':
        rec = openfda_records.get(f'openfda/nct/{nct_id}')
        if rec and rec.get('status') == 200 and isinstance(rec.get('body'), dict):
            return _at_locator(rec, locator)
        return None, None, 'source_not_retrieved'

    return None, None, 'source_not_retrieved'


def build_statement_links(
    statements: list[dict[str, str]],
    statements_sha256: str,
    ctgov_records: dict[str, dict[str, Any]],
    literature_records: dict[str, dict[str, Any]],
    openfda_records: dict[str, dict[str, Any]],
    included_nct_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert curated `documentary_statements.csv` rows into evidence link records."""
    out: list[dict[str, Any]] = []
    for row in statements:
        nct_id = row['nct_id'].strip()
        if included_nct_ids is not None and nct_id not in included_nct_ids:
            continue
        sid = row['statement_id'].strip()
        stype = row['source_type'].strip()
        sloc = row['source_locator'].strip()
        sdate_raw = row['statement_date'].strip() or None
        pub_date, prec = normalize_publication_date(sdate_raw)
        excerpt = row['verbatim_excerpt'].strip()

        source_text, retrieved_sha, locate_status = _locate_statement_source(
            row, ctgov_records, literature_records, openfda_records
        )
        # The excerpt is compared only with the value at `source_locator`, never with the whole response.
        # `check` records why it was or was not confirmed, in `validation_reasons` and
        # `detail.statement_check`: quote_found_at_locator, excerpt_not_found_at_locator,
        # locator_not_resolved, or source_not_retrieved.
        if locate_status != 'resolved' or source_text is None:
            check = 'source_not_retrieved' if locate_status == 'source_not_retrieved' else 'locator_not_resolved'
        elif squash_whitespace(excerpt).lower() in squash_whitespace(source_text).lower():
            check = 'quote_found_at_locator'
        else:
            check = 'excerpt_not_found_at_locator'
        quote_found = check == 'quote_found_at_locator'
        validation = 'quote_confirmed_in_source' if quote_found else 'not_validated'
        reason = {
            'quote_found_at_locator': 'verbatim_excerpt confirmed at source_locator in frozen retrieved source',
            'excerpt_not_found_at_locator': (
                'excerpt_not_found_at_locator: curated documentary statement; verbatim_excerpt does not '
                'stand in the value at source_locator in the frozen retrieved source'
            ),
            'locator_not_resolved': (
                'locator_not_resolved: curated documentary statement; source_locator names no value in the '
                'frozen retrieved source, so the verbatim quote was not checked'
            ),
            'source_not_retrieved': (
                'source_not_retrieved: curated documentary statement; verbatim quote not verified against a '
                'frozen retrieved source'
            ),
        }[check]
        out.append(
            _new_link(
                nct_id=nct_id,
                source_route='documentary_statements',
                target_id_type='documentary_locator',
                target_id=f'{stype}:{sid}',
                citation_type='DOCUMENTARY_STATEMENT',
                publication_or_action_date=pub_date,
                date_precision=prec,
                verbatim_excerpt=excerpt,
                source_sha256=retrieved_sha or statements_sha256,
                locator={'statement_id': sid, 'source_type': stype, 'source_locator': sloc},
                automated_validation=validation,
                validation_reasons=[reason],
                detail={
                    'statement_id': sid,
                    'source_type': stype,
                    'notes': row['notes'].strip(),
                    'source_sha256_basis': 'retrieved_source' if retrieved_sha else 'statements_csv',
                    'statement_check': check,
                },
            )
        )
    return out


# ---------------------------------------------------------------- human reviews & invariant check

def read_reviews(path: Path) -> tuple[dict[str, dict[str, str]], str]:
    """Read and validate `disclosure/reviews.csv`; return `({link_id: row}, file_sha256)`."""
    raw = path.read_bytes()
    file_sha = A.sha256_bytes(raw)
    with path.open(encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != REVIEW_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {REVIEW_COLUMNS}')
        rows = list(reader)

    out: dict[str, dict[str, str]] = {}
    for line_no, row in enumerate(rows, start=2):
        lid = (row.get('link_id') or '').strip()
        nct = (row.get('nct_id') or '').strip()
        reviewer = (row.get('reviewer') or '').strip()
        reviewed_at = (row.get('reviewed_at') or '').strip()
        decision = (row.get('decision') or '').strip()
        notes = (row.get('notes') or '').strip()

        if not LINK_ID_RE.match(lid):
            raise ValueError(f'{path.name} line {line_no}: invalid link_id {lid!r}')
        if not NCT_RE.match(nct):
            raise ValueError(f'{path.name} line {line_no}: invalid nct_id {nct!r}')
        if not reviewer:
            raise ValueError(f'{path.name} line {line_no}: reviewer is required')
        norm_date, prec = normalize_publication_date(reviewed_at)
        if norm_date is None or prec != 'day' or not ISO_DAY_RE.match(reviewed_at):
            raise ValueError(f'{path.name} line {line_no}: reviewed_at must be YYYY-MM-DD')
        if decision not in REVIEW_DECISIONS:
            raise ValueError(
                f'{path.name} line {line_no}: decision {decision!r} must be one of {REVIEW_DECISIONS}'
            )
        if not notes:
            raise ValueError(f'{path.name} line {line_no}: non-empty notes are required')
        if lid in out:
            raise ValueError(f'{path.name} line {line_no}: duplicate review for {lid}')
        out[lid] = {
            'link_id': lid,
            'nct_id': nct,
            'reviewer': reviewer,
            'reviewed_at': reviewed_at,
            'decision': decision,
            'notes': notes,
        }
    return out, file_sha


def apply_reviews(links: list[dict[str, Any]], reviews: dict[str, dict[str, str]]) -> list[str]:
    """Merge human review rows onto `links` without mutating automated fields; return orphaned IDs.

    Private `reviewer` names stay in `disclosure/reviews.csv` and are never copied to published JSON.
    """
    by_id = {lnk['link_id']: lnk for lnk in links}
    for lid, rev in sorted(reviews.items()):
        target = by_id.get(lid)
        if target is None:
            continue
        if target['nct_id'] != rev['nct_id']:
            raise ValueError(
                f"reviews.csv: {lid} names nct_id {rev['nct_id']}, "
                f"but evidence record belongs to {target['nct_id']}"
            )
        target['review_status'] = rev['decision']
        target['review'] = {
            'reviewed_at': rev['reviewed_at'],
            'notes': rev['notes'],
        }
    return sorted(set(reviews) - set(by_id))


def link_problems(link: dict[str, Any], reviewed_ids: set[str] | None = None) -> list[str]:
    """Return every domain/schema rule violation for one link record (empty list when valid)."""
    problems: list[str] = []
    lid = str(link.get('link_id') or '?')
    if not LINK_ID_RE.match(lid):
        problems.append(f'{lid}: malformed link_id')
    if not NCT_RE.match(str(link.get('nct_id') or '')):
        problems.append(f'{lid}: invalid nct_id {link.get("nct_id")!r}')
    if link.get('source_route') not in SOURCE_ROUTES:
        problems.append(f'{lid}: unknown source_route {link.get("source_route")!r}')
    if link.get('target_id_type') not in TARGET_ID_TYPES:
        problems.append(f'{lid}: unknown target_id_type {link.get("target_id_type")!r}')
    if not str(link.get('target_id') or '').strip():
        problems.append(f'{lid}: empty target_id')
    if link.get('citation_type') not in CITATION_TYPES:
        problems.append(f'{lid}: unknown citation_type {link.get("citation_type")!r}')

    prec = link.get('date_precision')
    pdate = link.get('publication_or_action_date')
    if prec not in DATE_PRECISIONS:
        problems.append(f'{lid}: unknown date_precision {prec!r}')
    elif prec == 'missing':
        if pdate is not None:
            problems.append(f'{lid}: date_precision is missing but publication_or_action_date is {pdate!r}')
    else:
        norm, check_prec = normalize_publication_date(pdate if isinstance(pdate, str) else None)
        if norm != pdate or check_prec != prec:
            problems.append(f'{lid}: publication_or_action_date {pdate!r} does not match precision {prec!r}')

    if not str(link.get('verbatim_excerpt') or '').strip():
        problems.append(f'{lid}: missing verbatim_excerpt')
    if not HEX64_RE.match(str(link.get('source_sha256') or '')):
        problems.append(f'{lid}: missing or invalid 64-hex source_sha256')
    if not isinstance(link.get('locator'), dict) or not link['locator']:
        problems.append(f'{lid}: missing locator object')

    av = link.get('automated_validation')
    if av not in AUTOMATED_VALIDATION:
        problems.append(f'{lid}: unknown automated_validation {av!r}')
    reasons = link.get('validation_reasons')
    if not isinstance(reasons, list) or not reasons or not all(isinstance(r, str) and r.strip() for r in reasons):
        problems.append(f'{lid}: validation_reasons must be a non-empty list of strings')

    if av == 'quote_confirmed_in_source' and link.get('source_route') != 'documentary_statements':
        problems.append(f'{lid}: quote_confirmed_in_source is reserved for documentary_statements')
    if link.get('citation_type') == 'OPENFDA_SPONSOR_DRUG_CANDIDATE' and av not in ('not_validated', 'rejected_by_rule'):
        problems.append(f'{lid}: heuristic openFDA drug candidate cannot carry automated_validation {av!r}')

    rs = link.get('review_status')
    if rs not in REVIEW_STATUSES:
        problems.append(f'{lid}: unknown review_status {rs!r}')
    reviewed = rs != 'unreviewed'
    if reviewed != isinstance(link.get('review'), dict):
        problems.append(f'{lid}: review_status and review metadata disagree')
    if reviewed_ids is not None and reviewed != (lid in reviewed_ids):
        problems.append(f'{lid}: review_status set without matching row in reviews.csv')

    # Unreviewed links must never be described as "validated".
    if rs == 'unreviewed' and isinstance(reasons, list):
        for r in reasons:
            # Allow the technical field value `not_validated`, forbid claiming the link is validated.
            cleaned = r.lower().replace('not_validated', '').replace('not validated', '').replace('unvalidated', '')
            if re.search(r'\bvalidated\b', cleaned):
                problems.append(f'{lid}: unreviewed link must never be described as validated')
    return problems
