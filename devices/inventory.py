"""Source rows -> regulatory submissions -> candidate device families.

Three counting units, never substituted for one another:

  source row    one line of FDA's AI/ML-enabled device list as downloaded (the tab's current unit)
  submission    one FDA submission number (K######, DEN######, P######, P######/S###). Rows that
                share a number are folded into one submission; every row and every disagreement
                between rows is kept.
  family        a set of submissions that this module (candidate) or a person (curated, through
                devices/families.csv) regards as one product line. A unique submission is NOT a
                unique product: a product cleared five times is five submissions.

Nothing here decides that two differently named things are the same product. The candidate
grouping key is deliberately narrow, and its only purpose is to say which submissions a
name-based registry match cannot tell apart.
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from devices import organizations as O

LIST_COLUMNS = ['Date of Final Decision', 'Submission Number', 'Device', 'Company', 'Panel (Lead)',
                'Primary Product Code']
ENRICHED_COLUMNS = LIST_COLUMNS + ['pdf_url', 'local_pdf_path']
FAMILY_COLUMNS = ['family_id', 'submission_number', 'family_name', 'source', 'note']
DUPLICATE_CLASSES = ('identical', 'alias_only', 'substantive_conflict')
PATHWAYS = ('510k', 'de_novo', 'pma', 'pma_supplement', 'unknown')

_SUBMISSION = re.compile(r'^(K\d{6}|DEN\d{6}|P\d{6}(?:/S\d{3})?|BK\d{6})$')

# Words that say what a device IS rather than what it is CALLED. A name made only of these
# ("Auto Segmentation", "AI Platform") cannot identify a product, so it is never a grouping key
# and never a registry search term. Hand-made, carried over from the 2026-11-13 audit; changing
# it moves submissions between "grouped" and "not grouped" and between "searchable" and not.
GENERIC_DEVICE_WORDS = frozenset({
    'system', 'systems', 'software', 'diagnostic', 'diagnostics', 'ultrasound', 'imaging', 'image', 'images', 'ct',
    'mr', 'mri', 'pet', 'xray', 'x', 'ray', 'digital', 'radiography', 'scanner', 'platform', 'module', 'application',
    'app', 'analysis', 'analyzer', 'workstation', 'viewer', 'deep', 'learning', 'reconstruction', 'ai', 'portable',
    'series', 'station', 'processing', 'mobile', 'solution', 'suite', 'tool', 'tools', 'kit', 'test', 'device',
    'monitor', 'monitoring', 'detection', 'assist', 'plus', 'pro', 'auto', 'automatic', 'automated', 'segmentation',
    'with', 'and', 'for', 'the', 'of', 'option', 'only', 'plug', 'in', 'version', 'medical', 'clinical', 'cardiac',
    'remote', 'triage', 'notification', 'enhancement', 'view', 'one', 'go', 'base', 'smart', 'model', 'bone',
    'marrow', 'spectral', 'lung', 'bladder', 'brain', 'breast', 'cad', 'strain', 'rapid', 'insights', 'planner',
    'classifier', 'semen', 'low', 'dose', 'cancer', 'screening', 'body', 'fluid', 'blood', 'smear', 'full', 'field',
    'peripheral', 'slide', 'loader', 'rhythm', 'express', 'critical', 'care', 'algorithm', 'endotracheal', 'tube',
})
# A trailing version: "version 2", "ver. 3.1", "v2.0", "3.1.0", "5.x". The keyword must be followed by
# a digit: without that, "VersaViewer" reads as "Ver" + "saViewer" and the whole name is removed.
_VERSION_TAIL = re.compile(
    r'(?:\b(?:software\s+)?(?:version|ver|rev|release)\b\.?\s*\d[\w.]*|\bv\s*\d[\w.]*|\b\d+(?:\.\w+)+|\b\d+\.x)\s*$',
    re.I)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path, columns: list[str]) -> list[dict[str, Any]]:
    """Every data row with its 1-based position, cell values exactly as the file holds them.
    Refuses a file whose header differs: a renamed or reordered column would otherwise be read
    as missing data."""
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != columns:
            raise ValueError(f'{path.name}: header {reader.fieldnames} is not {columns}')
        return [{'row_number': i, 'values': dict(r)} for i, r in enumerate(reader, start=1)]


def parse_decision_date(s: str) -> date | None:
    """The list writes dates as M/D/YY. Assumption: a two-digit year of 90 or more is 19xx and
    anything else 20xx (the list runs 1995-2025). None when the cell does not parse; the caller
    records that as an unparsed date, never as a default."""
    m = re.fullmatch(r'\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*', s or '')
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 1900 if y >= 90 else 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def pathway(submission_number: str) -> str:
    """Regulatory pathway read from the number's prefix. An assumption about FDA's numbering, not
    a looked-up fact: DEN = De Novo, P = premarket approval (with /S### = a supplement), K = 510(k).
    The official record (devices/acquire.py) is recorded beside it and may disagree: early De Novo
    requests were filed under a K number."""
    sn = submission_number.strip().upper()
    if sn.startswith('DEN'):
        return 'de_novo'
    if sn.startswith('P'):
        return 'pma_supplement' if '/S' in sn else 'pma'
    if sn.startswith('K') or sn.startswith('BK'):
        return '510k'
    return 'unknown'


def norm_text(s: str) -> str:
    """Lower case, accents folded, every run of non-alphanumerics one space."""
    return re.sub(r'[^a-z0-9]+', ' ', O.ascii_fold(s or '').lower()).strip()


def _core(segment: str) -> str:
    s = re.sub(r'\([^)]*\)|[()]', ' ', segment)        # parenthesised model numbers and aliases; stray brackets
    s = re.sub(r'\s+', ' ', s).strip(' ,;/-')
    prev = None
    while prev != s:                                    # stacked version tails: "X v2 3.1"
        prev = s
        s = _VERSION_TAIL.sub('', s).strip(' ,;/-')
    return s


def name_forms(device_raw: str, organisation_normalised: str = '') -> dict[str, Any]:
    """The strings a listed device name can be searched and matched by.

      display        the listed name with control characters removed
      core           the whole listed name with parentheses and a trailing version removed
      segments       the core of each model when the cell lists several (split on ';', ' / ', ', ')
      full           normalised segments that contain at least one non-generic word
      trimmed        a full form with trailing device-type words removed ("SKOUT system" -> "skout"),
                     kept only when a non-generic word remains and it is not one token under 4 letters
      company_only   a trimmed form made only of the manufacturer's own name tokens: it shows the
                     manufacturer is named, not that this device is, and never counts as a name match
      generic        true when no form survives: the name cannot be searched or grouped by
    Assumption behind 'core': text in parentheses and a trailing version number distinguish
    releases of one product, not products. It is wrong for names whose distinctive part sits in
    the parentheses ("ZEUS System (Zio Watch)"); those need a curated family.
    """
    display = O.clean_display(device_raw)
    core = _core(display)
    segments: list[str] = []
    # Parenthesised text is removed BEFORE splitting: a comma inside "(TUS-AI900, TUS-AI800)" is not
    # a boundary between models. A bracket left unbalanced by the source cell is dropped as well.
    for seg in re.split(r'\s*;\s*|\s+/\s+|,\s+', re.sub(r'\([^)]*\)|[()]', ' ', display)):
        c = _core(seg)
        if c and c not in segments:
            segments.append(c)
    org_tokens = set(organisation_normalised.split())
    full, trimmed, company_only = [], [], []
    # The whole cell is a form only when it names one model: a list of models cannot occur as such.
    for n in ([norm_text(core)] if len(segments) < 2 else []) + [norm_text(x) for x in segments]:
        toks = n.split()
        if not toks or n in full or all(t in GENERIC_DEVICE_WORDS for t in toks) or (len(toks) == 1 and len(n) < 3):
            continue
        full.append(n)
    for n in full:
        toks = n.split()
        while toks and toks[-1] in GENERIC_DEVICE_WORDS:
            toks.pop()
        t = ' '.join(toks)
        if not toks or t in full or t in trimmed or t in company_only or (len(toks) == 1 and len(t) < 4):
            continue
        (company_only if set(toks) <= org_tokens else trimmed).append(t)
    return {'display': display, 'core': core, 'segments': segments, 'full': full, 'trimmed': trimmed,
            'company_only': company_only, 'generic': not full}


def classify_duplicate(rows: list[dict[str, Any]], columns: list[str]) -> tuple[str | None, dict[str, list[str]]]:
    """How the rows sharing one submission number relate, and the fields on which they differ.

      None                  the number appears once
      identical             every cell is byte-identical: a redundant record
      alias_only            the rows differ only in Device or Company, and each differing pair is
                            the same text after normalisation (spelling, punctuation, case): the
                            extra row adds an alias and nothing else
      substantive_conflict  anything else differs (date, panel, product code, document path, or a
                            name that normalises differently): the rows carry different information
                            and a person has to decide which holds
    The second value lists, for every differing field, the distinct raw values in row order.
    """
    if len(rows) < 2:
        return None, {}
    conflicts: dict[str, list[str]] = {}
    for c in columns:
        values = list(dict.fromkeys(r['values'].get(c) or '' for r in rows))
        if len(values) > 1:
            conflicts[c] = values
    if not conflicts:
        return 'identical', {}
    for c, values in conflicts.items():
        if c == 'Company':
            keys = {O.normalise(v)['normalised'] for v in values}
        elif c == 'Device':
            keys = {norm_text(O.clean_display(v)) for v in values}
        else:
            return 'substantive_conflict', conflicts
        if len(keys) > 1:
            return 'substantive_conflict', conflicts
    return 'alias_only', conflicts


def submissions(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, dict[str, Any]]:
    """{submission number: record}, in first-appearance order. The number is the key and is the
    only thing rows are folded on. Where rows disagree the first row's value is shown and every
    value is listed under `conflicts`; nothing is chosen silently."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sn = (r['values'].get('Submission Number') or '').strip()
        if not _SUBMISSION.match(sn):
            raise ValueError(f"row {r['row_number']}: {sn!r} is not a recognised FDA submission number")
        grouped.setdefault(sn, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for sn, rs in grouped.items():
        dup, conflicts = classify_duplicate(rs, columns)
        first = rs[0]['values']
        dates = list(dict.fromkeys(r['values'].get('Date of Final Decision') or '' for r in rs))
        parsed = [parse_decision_date(d) for d in dates]
        out[sn] = {
            'submission_number': sn, 'pathway': pathway(sn),
            'n_source_rows': len(rs), 'source_rows': [{'row_number': r['row_number'], **r['values']} for r in rs],
            'duplicate_class': dup, 'conflicts': conflicts,
            'device_names_as_listed': list(dict.fromkeys(r['values'].get('Device') or '' for r in rs)),
            'companies_as_listed': list(dict.fromkeys(r['values'].get('Company') or '' for r in rs)),
            'device_name': O.clean_display(first.get('Device') or ''),
            'company': O.clean_display(first.get('Company') or ''),
            'panel': first.get('Panel (Lead)') or None, 'product_code': first.get('Primary Product Code') or None,
            # One decision date only when every row agrees and it parses; otherwise absent, with why.
            'decision_date': parsed[0].isoformat() if len(dates) == 1 and parsed[0] else None,
            'decision_date_absent_reason': (None if len(dates) == 1 and parsed[0] else
                                            'rows_disagree' if len(dates) > 1 else 'unparsed'),
            'decision_date_as_listed': dates,
        }
    return out


def read_curated_families(path: Path) -> dict[str, dict[str, str]]:
    """{submission number: row} from devices/families.csv. Refuses unknown columns, a submission
    listed twice (one submission, one family) and a row without a note saying why it belongs."""
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FAMILY_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {FAMILY_COLUMNS}')
        rows = list(reader)
    out: dict[str, dict[str, str]] = {}
    for i, r in enumerate(rows, start=2):
        sn = (r['submission_number'] or '').strip()
        if not sn or not (r['family_id'] or '').strip() or not (r['note'] or '').strip():
            raise ValueError(f'{path.name} line {i}: family_id, submission_number and note are required')
        if sn in out:
            raise ValueError(f'{path.name} line {i}: {sn} is assigned to two families')
        out[sn] = r
    return out


def family_key(sub: dict[str, Any], organization_id: str) -> tuple[str | None, str]:
    """(key, reason). The candidate grouping key is the organisation together with the WHOLE core
    name (parentheses and trailing version removed, normalised). Two submissions are grouped only
    when both parts are equal.

    Deliberately not done: grouping on a shared word ("Rapid ICH" with "Rapid LVO"), on one model
    of a multi-model cell, across organisations (two firms both list an "Aurora"), or on a name
    made only of device-type words (the same firm's two "Ultrasound System" clearances may be
    different machines). Each of those stays apart until devices/families.csv joins them.
    """
    forms = name_forms(sub['device_names_as_listed'][0], '')
    key = norm_text(forms['core'])
    if not key:
        return None, 'name_empty_after_normalisation'
    if all(t in GENERIC_DEVICE_WORDS for t in key.split()):
        return None, 'generic_name_not_grouped'
    return f'{organization_id}|{key}', 'organisation_and_whole_core_name_equal'


def families(subs: dict[str, dict[str, Any]], org_of: dict[str, str],
             curated: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    """{family_id: record}; every submission is in exactly one family.

    status 'curated' only for a family listed in devices/families.csv; everything this function
    groups by itself is 'candidate'. A candidate family of one is the normal case, not a failure.
    """
    unknown = sorted(set(curated) - set(subs))
    if unknown:
        raise ValueError(f'families.csv names submissions that are not in the inventory: {unknown}')
    fams: dict[str, dict[str, Any]] = {}
    for sn, sub in subs.items():
        if sn in curated:
            fid = 'family:curated:' + curated[sn]['family_id'].strip()
            f = fams.setdefault(fid, {'family_id': fid, 'status': 'curated', 'grouping_key': None,
                                      'grouping_reason': 'listed_in_families_csv',
                                      'family_name': curated[sn]['family_name'].strip() or sub['device_name'],
                                      'curation_notes': [], 'submission_numbers': []})
            f['curation_notes'].append({'submission_number': sn, 'source': curated[sn]['source'],
                                        'note': curated[sn]['note']})
        else:
            key, reason = family_key(sub, org_of[sn])
            fid = ('family:candidate:' + hashlib.sha256(key.encode()).hexdigest()[:12]) if key else \
                  f'family:candidate:{sn}'
            f = fams.setdefault(fid, {'family_id': fid, 'status': 'candidate', 'grouping_key': key,
                                      'grouping_reason': reason, 'family_name': sub['device_name'],
                                      'submission_numbers': []})
        f['submission_numbers'].append(sn)
    for f in fams.values():
        members = [subs[sn] for sn in f['submission_numbers']]
        f['n_submissions'] = len(members)
        f['organization_ids'] = sorted({org_of[sn] for sn in f['submission_numbers']})
        f['device_names_as_listed'] = sorted({n for m in members for n in m['device_names_as_listed']})
        dates = sorted(m['decision_date'] for m in members if m['decision_date'])
        f['decision_dates'] = {'first': dates[0] if dates else None, 'last': dates[-1] if dates else None}
        f['pathways'] = dict(sorted(Counter(m['pathway'] for m in members).items()))
    return fams


def reconcile(rows: list[dict[str, Any]], subs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Rows -> submissions arithmetic, with the duplicated numbers listed by class. The contract
    checks that n_rows = n_submissions + n_extra_rows and that the classes add up."""
    dups = {sn: s for sn, s in subs.items() if s['n_source_rows'] > 1}
    by_class = {c: sorted(sn for sn, s in dups.items() if s['duplicate_class'] == c) for c in DUPLICATE_CLASSES}
    return {'n_rows': len(rows), 'n_submissions': len(subs),
            'n_duplicated_numbers': len(dups),
            'n_extra_rows': sum(s['n_source_rows'] - 1 for s in dups.values()),
            'duplicated_numbers_by_class': by_class,
            'duplicate_detail': {sn: {'duplicate_class': s['duplicate_class'], 'n_source_rows': s['n_source_rows'],
                                      'conflicts': s['conflicts']} for sn, s in sorted(dups.items())}}
