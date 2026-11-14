"""Producer-side check of a device-linkage artifact family (schema version 1).

check() returns every problem found (an empty list passes). The build runs it on the staged
files before anything is written, and scripts/check_device_links_contract.py runs it on a written
directory. It is an ENGINEERING check: it establishes that the files are internally consistent
and that no record breaks a stated rule. It cannot establish that any link is right; that is a
person's review, recorded in devices/reviews.csv.

What it refuses, in the order checked:
  * a family whose files, hashes or run id do not agree with run.json
  * counts that do not reconcile rows -> submissions -> families
  * an organisation merged by anything but the normalisation rules or the alias file
  * a document record that claims a type without bytes, or treats a granting letter as a
    demographic summary
  * an evidence record that breaks a rule of devices/links.py: a heuristic match marked as
    validated or as submission-level, a record without verbatim evidence or location, an
    identifier-confirmed record whose identifier is not in its evidence or does not resolve, a
    human review status with no review behind it, timing folded into the relationship
  * a coverage cell outside the seven states, without a reason, or claiming a link that is absent
  * a published count without a named denominator and a definition, or outside its denominator
  * a demographic observation not attached to a held document, marked as addable to registry
    counts, or carrying a resolved population identity that no record supports
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from devices import coverage as C
from devices import documents as D
from devices import inventory as I
from devices import links as L
from devices.artifacts import FILES, SCHEMA_VERSION, dump

RUN_ID = re.compile(r'^devices-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ROUTE_METHODS = {'nct_citation_scan': ('nct_id_in_submission_document',),
                 'local_document': ('nct_id_in_submission_document',),
                 'fda_document_fetch': ('nct_id_in_submission_document',),
                 'registry_submission_number_search': ('submission_number_in_registry_record',)}


def check(files: dict[str, Any]) -> list[str]:
    """files: {relative path: parsed object}, including run.json."""
    p: list[str] = []
    run = files.get('run.json')
    if not isinstance(run, dict):
        return ['run.json is missing']
    if run.get('schema_version') != SCHEMA_VERSION:
        p.append(f"run.json schema_version {run.get('schema_version')!r} is not {SCHEMA_VERSION}")
    if not isinstance(run.get('run_id'), str) or not RUN_ID.match(run['run_id']):
        p.append('run.json run_id is malformed')
    if run.get('scheduled') is not False:
        p.append('run.json must state scheduled: false (this build runs by hand)')
    listed, present = run.get('files') or {}, set(files) - {'run.json'}
    for path in sorted(set(listed) ^ present):
        p.append(f'{path}: listed in run.json but absent' if path in listed else f'{path}: present but not in run.json')
    for path in sorted(set(listed) & present):
        if hashlib.sha256(dump(files[path])).hexdigest() != listed[path]:
            p.append(f'{path}: sha256 does not match run.json')
        if files[path].get('run_id') != run.get('run_id') or files[path].get('schema_version') != SCHEMA_VERSION:
            p.append(f'{path}: from another run or schema')
    if set(FILES) - present:
        return p + [f'missing files: {sorted(set(FILES) - present)}']
    inv = ((run.get('inputs') or {}).get('inventory') or {})
    if not HEX64.match(str(inv.get('sha256'))):
        p.append('run.json does not pin the inventory by sha256')
    for name, src in (run.get('sources') or {}).items():
        if name in ('openfda', 'fda_database_pages', 'registry') and src is not None and \
                not HEX64.match(str(src.get('sha256_uncompressed'))):
            p.append(f'run.json source {name} is not pinned by sha256')
    if not ((run.get('sources') or {}).get('text_extraction') or {}).get('extraction_version'):
        p.append('run.json does not record the text extraction version')

    subs = files['submissions.json'].get('submissions') or []
    rec = files['submissions.json'].get('reconciliation') or {}
    fams = files['families.json'].get('families') or []
    orgs = files['organizations.json'].get('organizations') or []
    docs = files['documents.json'].get('documents') or []
    studies = {s.get('nct_id'): s for s in files['studies.json'].get('studies') or []}
    links = files['links.json'].get('links') or []
    pairs = files['links.json'].get('pairs') or []
    cov = files['coverage.json'].get('submissions') or []
    obs = files['demographics.json'].get('observations') or []
    summary = files['summary.json']
    by_sn = {s.get('submission_number'): s for s in subs}
    by_fam = {f.get('family_id'): f for f in fams}

    # ---- rows -> submissions -> families
    n_rows = sum(s.get('n_source_rows', 0) for s in subs)
    if len(by_sn) != len(subs):
        p.append('submissions.json repeats a submission number')
    if rec.get('n_rows') != n_rows or rec.get('n_submissions') != len(subs) or \
            rec.get('n_rows') != rec.get('n_submissions', 0) + rec.get('n_extra_rows', -1):
        p.append('rows do not reconcile to submissions (rows = submissions + extra rows)')
    if any(len(s.get('source_rows') or []) != s.get('n_source_rows') for s in subs):
        p.append('a submission does not carry all of its source rows')
    dup = {sn for sn, s in by_sn.items() if s.get('n_source_rows', 0) > 1}
    classed = [sn for c in I.DUPLICATE_CLASSES for sn in (rec.get('duplicated_numbers_by_class') or {}).get(c, [])]
    if set(classed) != dup or len(classed) != len(dup) or rec.get('n_duplicated_numbers') != len(dup):
        p.append('duplicated numbers are not each in exactly one duplicate class')
    for sn in sorted(dup):
        s = by_sn[sn]
        if s.get('duplicate_class') not in I.DUPLICATE_CLASSES or \
                (s['duplicate_class'] == 'identical') != (not s.get('conflicts')):
            p.append(f'{sn}: duplicate class and recorded conflicts disagree')
    for s in subs:
        if s.get('n_source_rows') == 1 and s.get('duplicate_class') is not None:
            p.append(f"{s.get('submission_number')}: a single row cannot have a duplicate class")
        if s.get('pathway') not in I.PATHWAYS:
            p.append(f"{s.get('submission_number')}: unknown pathway")
        if (s.get('decision_date') is None) == (s.get('decision_date_absent_reason') is None):
            p.append(f"{s.get('submission_number')}: a decision date is either present or absent with a reason")
    members = [sn for f in fams for sn in f.get('submission_numbers') or []]
    if sorted(members) != sorted(by_sn):
        p.append('every submission must be in exactly one family')
    for f in fams:
        if f.get('status') not in ('candidate', 'curated') or f.get('n_submissions') != len(f.get('submission_numbers') or []):
            p.append(f"{f.get('family_id')}: bad status or size")
        if (f.get('status') == 'curated') != str(f.get('family_id')).startswith('family:curated:'):
            p.append(f"{f.get('family_id')}: only a family listed in families.csv is curated")
        if any(by_sn.get(sn, {}).get('family_id') != f.get('family_id') for sn in f.get('submission_numbers') or []):
            p.append(f"{f.get('family_id')}: members point at another family")
    org_ids = {o.get('organization_id') for o in orgs}
    if any(s.get('organization_id') not in org_ids for s in subs):
        p.append('a submission names an organisation that is not in organizations.json')
    for o in orgs:
        merged = len(o.get('normalised_keys') or []) > 1
        if merged != bool(o.get('aliases_applied')):
            p.append(f"{o.get('organization_id')}: differently named organisations merged without an alias row")

    # ---- documents
    doc_by_id = {d.get('document_id'): d for d in docs}
    if len(doc_by_id) != len(docs):
        p.append('documents.json repeats a document id')
    for d in docs:
        did = d.get('document_id')
        if d.get('submission_number') not in by_sn:
            p.append(f'{did}: document of an unknown submission')
        if d.get('availability') == 'retrieved':
            if not HEX64.match(str(d.get('sha256'))) or not d.get('bytes') or not d.get('versions'):
                p.append(f'{did}: retrieved without a hash, size and version')
            text = d.get('text') or {}
            if text.get('status') not in ('text', 'partial', 'image_only', 'failed', 'not_extracted') or \
                    not text.get('extraction_version'):
                p.append(f'{did}: text status or extraction version missing')
            if text.get('status') != 'not_extracted':
                if d.get('document_type') not in D.DOCUMENT_TYPES:
                    p.append(f'{did}: unknown document type {d.get("document_type")!r}')
                    continue
                c = d.get('classification') or {}
                if c.get('basis') not in D.CLASSIFICATION_BASES:
                    p.append(f'{did}: unknown classification basis {c.get("basis")!r}')
                if c.get('basis') == 'text' and d['document_type'] != 'other' and not (c.get('matched_phrase') and c.get('page')):
                    p.append(f'{did}: classified from text without the matched phrase and page')
                if c.get('basis') == 'official_label_unread_pages' and d.get('demographic_summary_candidate'):
                    p.append(f'{did}: a summary whose pages were not read is treated as a demographic summary')
                category, capable, _ = D.DOCUMENT_TYPES[d['document_type']]
                if d.get('category') != category:
                    p.append(f'{did}: category does not follow from its type')
                if d.get('demographic_summary_candidate') and (
                        not capable or text.get('status') not in ('text', 'partial') or d.get('scope') != 'this_submission'):
                    p.append(f'{did}: a {d["document_type"]} ({text.get("status")}) is treated as a demographic summary')
        else:
            if d.get('sha256') or d.get('document_type') or d.get('demographic_summary_candidate'):
                p.append(f'{did}: not retrieved, yet carries a hash, a type or a demographic flag')
            if d.get('availability') not in ('not_found', 'not_a_pdf', 'too_large', 'http_error', 'network_error',
                                             'skipped_disk_budget'):
                p.append(f'{did}: unknown availability {d.get("availability")!r}')
        if not d.get('demographic_summary_reason'):
            p.append(f'{did}: demographic_summary_candidate has no reason')

    # ---- evidence records
    ids = [x.get('link_id') for x in links]
    if len(set(ids)) != len(ids):
        p.append('links.json repeats a link id')
    n_reviewed = 0
    for x in links:
        p += L.problems(x)
        lid, src, tgt = x.get('link_id'), x.get('source') or {}, (x.get('target') or {}).get('id')
        n_reviewed += x.get('human_review') != 'unreviewed'
        if src.get('id') not in (by_sn if src.get('entity_type') == 'submission' else by_fam):
            p.append(f'{lid}: source entity does not exist')
        if tgt not in studies:
            p.append(f'{lid}: target study is not in studies.json')
        if x.get('level') == 'submission' and src.get('entity_type') != 'submission':
            p.append(f'{lid}: a submission-level record needs a submission as its source')
        if x.get('automated_validation') == 'identifier_confirmed':
            if (studies.get(tgt) or {}).get('registry_state') != 'resolves':
                p.append(f'{lid}: identifier_confirmed but the study does not resolve on the registry')
            if x.get('method') == 'nct_id_in_submission_document':
                d = doc_by_id.get((x.get('evidence') or {}).get('source_id')) or {}
                if d.get('submission_number') != src.get('id') or d.get('scope') != 'this_submission' or \
                        d.get('availability') != 'retrieved':
                    p.append(f'{lid}: identifier_confirmed from a document that is not a held document of the submission')
    if n_reviewed != (run.get('reviews') or {}).get('n_applied'):
        p.append('the number of reviewed records differs from the reviews applied (a review status set by code?)')
    linked = [lid for pr in pairs for lid in pr.get('link_ids') or []]
    if sorted(linked) != sorted(ids) or len({(pr['source']['id'], pr['study']) for pr in pairs}) != len(pairs):
        p.append('pairs do not partition the evidence records')

    # ---- coverage
    links_of: dict[str, list[dict[str, Any]]] = {}
    for x in links:
        links_of.setdefault((x.get('source') or {}).get('id'), []).append(x)
    if sorted(c.get('submission_number') for c in cov) != sorted(by_sn):
        p.append('coverage.json must hold every submission exactly once')
    for c in cov:
        sn = c.get('submission_number')
        cells = c.get('routes') or []
        if [cell.get('route') for cell in cells] != list(C.ROUTES):
            p.append(f'{sn}: coverage must hold each route once, in order')
            continue
        for cell in cells:
            if cell.get('state') not in C.STATES or not str(cell.get('reason') or '').strip():
                p.append(f"{sn}/{cell.get('route')}: state outside the seven, or no reason")
            mine = links_of.get(sn, []) + links_of.get((by_sn.get(sn) or {}).get('family_id'), [])
            if cell.get('state') == 'verified_link' and not any(
                    x['automated_validation'] == 'identifier_confirmed' and x['human_review'] != 'rejected'
                    and x['method'] in ROUTE_METHODS.get(cell['route'], ()) for x in mine):
                p.append(f"{sn}/{cell.get('route')}: verified_link without an identifier-confirmed record on that route")
            if cell.get('state') == 'candidate_awaiting_review' and not mine and cell['route'] != 'demographic_extraction':
                p.append(f"{sn}/{cell.get('route')}: candidate_awaiting_review without any evidence record")
        if c.get('overall') != C.overall(cells):
            p.append(f'{sn}: overall coverage state does not follow from its routes')

    # ---- summary
    den = summary.get('denominators') or {}
    expected = {'source_rows': n_rows, 'submissions': len(subs), 'families': len(fams), 'organizations': len(orgs),
                'documents_tried': len(docs), 'evidence_records': len(links), 'pairs': len(pairs), 'studies': len(studies),
                'demographic_extractions': len(obs)}
    for name, n in expected.items():
        if (den.get(name) or {}).get('n') != n:
            p.append(f'summary denominator {name} differs from the files ({(den.get(name) or {}).get("n")} vs {n})')
    for name, d in den.items():
        if not isinstance(d.get('n'), int) or not d.get('definition'):
            p.append(f'summary denominator {name}: needs a count and a definition')
    seen: set[str] = set()
    for m in summary.get('metrics') or []:
        d = den.get(m.get('denominator'))
        if m.get('id') in seen:
            p.append(f"metric {m.get('id')}: listed twice")
        seen.add(m.get('id'))
        if d is None:
            p.append(f"metric {m.get('id')}: unknown denominator")
        elif not (isinstance(m.get('numerator'), int) and 0 <= m['numerator'] <= d['n']):
            p.append(f"metric {m.get('id')}: numerator outside 0..{d['n']}")
        if not m.get('definition'):
            p.append(f"metric {m.get('id')}: no definition")

    # ---- demographics
    for o in obs:
        oid = o.get('observation_id')
        d = doc_by_id.get(o.get('document_id')) or {}
        if d.get('availability') != 'retrieved' or d.get('sha256') != o.get('document_sha256'):
            p.append(f'{oid}: not attached to a held document version')
        pop = o.get('population') or {}
        if o.get('may_be_added_to_registry_counts') is not False:
            p.append(f'{oid}: a filing observation must not be addable to registry counts')
        if pop.get('population_identity') == 'unresolved':
            if pop.get('relation_to_registry_population') != 'undetermined' or pop.get('registered_study'):
                p.append(f'{oid}: an unresolved population cannot have a determined relation to a registry population')
        elif not (pop.get('registered_study') and pop.get('population_identity_reason')):
            p.append(f'{oid}: a resolved population identity needs the study and the reason')
        for path, f in (o.get('demographic_fields') or {}).items():
            if f.get('reported') is not True and (f.get('value') is not None or not f.get('absent_reason')):
                p.append(f'{oid}/{path}: an unreported field must be absent with a reason, not a value')
    return p


def load_dir(out: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(out.rglob('*.json')):
        files[path.relative_to(out).as_posix()] = json.loads(path.read_text())
    return files
