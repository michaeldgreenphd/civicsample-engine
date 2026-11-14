"""A fictional device inventory, fictional FDA pages and documents, and a fictional registry.

Every submission number (K99xxxx, DEN99xxxx, P99xxxx), NCT id (NCT990000xx), company, device and
sentence here is invented. stub_get() answers the requests the acquisition code makes, from these
tables alone, so the whole path (acquire -> freeze -> build -> contract) runs with no network.
Documents are not real PDFs: the stub serves a few bytes with a PDF header, and the page text the
extractor would have produced is written straight into the text cache.
"""
from __future__ import annotations

import csv
import gzip
import json
import re
from pathlib import Path
from typing import Any

from devices import acquire, artifacts, demographics, documents, inventory, links, organizations, pdftext, registry

GENERATED_AT = '2026-11-13T00:00:00+00:00'
DOCS = acquire.CDRH_DOCS

# (decision date, number, device, company, panel, product code)
ROWS = [
    ('3/2/21', 'K990001', 'Zorbex Scan (v2.1)', 'Fictional Imaging, Inc.', 'Radiology', 'QIH'),
    ('3/2/21', 'K990001', 'Zorbex Scan (v2.1)', 'Fictional Imaging, Inc.', 'Radiology', 'QIH'),   # identical
    ('6/9/23', 'K990002', 'Zorbex Scan', 'Fictional Imaging Inc', 'Radiology', 'QIH'),
    ('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl Co', 'Radiology', 'LLZ'),
    ('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl, Co.', 'Radiology', 'LLZ'),                  # alias only
    ('7/7/20', 'K990004', 'Lumora', 'Bright Optics LLC', 'Ophthalmic', 'AAA'),
    ('7/7/20', 'K990004', 'Lumora', 'Bright Optics LLC', 'Ophthalmic', 'BBB'),                     # conflict
    ('2/13/18', 'DEN990005', 'Quanta Triage', 'Viza Labs Ltd', 'Radiology', 'QAS'),
    ('1/11/21', 'P990006', 'Meridian Breast System', 'Meridian Medical GmbH', 'Radiology', 'QNK'),
    ('9/23/24', 'P990007/S002', 'Meridian Breast System B', 'Meridian Medical GmbH', 'Radiology', 'OTE'),
    ('4/4/24', 'K990008', 'Lumora', 'Other Optics LLC', 'Ophthalmic', 'AAA'),
]

# FDA database pages: number -> [(file under cdrh_docs, FDA's label)]; None = no answer (network).
PAGES: dict[str, list[tuple[str, str]] | None] = {
    'K990001': [('pdf99/K990001.pdf', 'Summary')],
    'K990002': [('pdf99/K990002.pdf', 'Summary')],
    'K990003': [('pdf99/K990003.pdf', 'Statement')],
    'K990004': [],
    'DEN990005': [('pdf99/DEN990005.pdf', 'Classification Order'), ('reviews/DEN990005.pdf', 'Decision Summary')],
    'P990006': [('pdf99/P990006A.pdf', 'Approval Order'), ('pdf99/P990006B.pdf', 'Summary of Safety and Effectiveness'),
                ('pdf99/P990006C.pdf', 'Labeling')],
    'P990007S002': [],
    'P990007': [('pdf99/P990007B.pdf', 'Summary of Safety and Effectiveness')],
    'K990008': None,
}
LETTER_510K = 'Re: {n}\nDear Sir:\nWe have reviewed your Section 510(k) premarket notification of intent to market.'
# Page texts the extractor "produced", by file. [] = a scan with no text layer.
TEXTS: dict[str, list[str]] = {
    'pdf99/K990001.pdf': [
        LETTER_510K.format(n='K990001'), 'Indications for Use\nZorbex Scan is intended to aid review.',
        '510(k) Summary\nPredicate device: K980111.\nA clinical study (ClinicalTrials.gov NCT99000001) enrolled 412 '
        'patients at three sites. Of the subjects, 55% were female. Race and ethnicity were recorded.'],
    'pdf99/K990002.pdf': [
        LETTER_510K.format(n='K990002'),
        '510(k) Summary\nNo clinical studies were required to support substantial equivalence. Bench testing only. '
        'An unrelated registry entry NCT99009999 is mentioned.'],
    'pdf99/K990003.pdf': [
        LETTER_510K.format(n='K990003'),
        '510(k) Statement\nI certify that, in my capacity as officer, I will make available all information '
        'included in this premarket notification on safety and effectiveness within 30 days of request.'],
    'pdf99/DEN990005.pdf': [
        'Re: DEN990005\nDear Sir:\nFDA has completed its review of your De Novo request for classification of the '
        'Quanta Triage. This order, therefore, classifies the device into class II. Special controls: standalone '
        'performance testing protocols and results.'],
    'reviews/DEN990005.pdf': [
        'EVALUATION OF AUTOMATIC CLASS III DESIGNATION FOR Quanta Triage\nDECISION SUMMARY\nA. De Novo Number: DEN990005',
        'SUMMARY OF CLINICAL INFORMATION\nA retrospective study of 300 patients from two hospitals was conducted. '
        'Mean age was 61 years; 48% were women.'],
    'pdf99/P990006A.pdf': ['Re: P990006\nThe premarket approval application (PMA) for the Meridian Breast System '
                           'is approved. You may begin commercial distribution.'],
    'pdf99/P990006B.pdf': [],
    'pdf99/P990006C.pdf': ['Meridian Breast System\nINSTRUCTIONS FOR USE\nRead before operating.'],
    'pdf99/P990007B.pdf': ['SUMMARY OF SAFETY AND EFFECTIVENESS DATA (SSED)\nPMA P990007',
                           'The pivotal study (NCT99000002) enrolled 900 subjects.'],
}


def _study(nct: str, title: str, sponsor: str, interventions: list[str] | None = None, start: str = '2019-03-01',
           primary: str | None = '2020-01-15', description: str = '', collaborators: list[str] | None = None) -> dict[str, Any]:
    return {'protocolSection': {
        'identificationModule': {'nctId': nct, 'briefTitle': title, 'officialTitle': title},
        'statusModule': {'overallStatus': 'COMPLETED', 'startDateStruct': {'date': start},
                         **({'primaryCompletionDateStruct': {'date': primary}} if primary else {})},
        'sponsorCollaboratorsModule': {'leadSponsor': {'name': sponsor, 'class': 'INDUSTRY'},
                                       'collaborators': [{'name': c} for c in collaborators or []]},
        'descriptionModule': {'briefSummary': description},
        'armsInterventionsModule': {'interventions': [{'type': 'DEVICE', 'name': i} for i in interventions or []]},
        'designModule': {'studyType': 'OBSERVATIONAL'}}, 'hasResults': False}


STUDIES = [
    _study('NCT99000001', 'Reader study of an imaging aid', 'Fictional Imaging Inc.', ['Zorbex Scan']),
    _study('NCT99000002', 'Meridian Breast System pivotal study', 'Meridian Medical GmbH', ['Meridian Breast System']),
    _study('NCT99000003', 'Stroke workflow evaluation', 'Fictional University',
           ['Device: Quanta LVO (De Novo Number DEN990005)'], start='2021-06', primary=None),
    _study('NCT99000004', 'An unrelated anaesthesia trial', 'Fictional Hospital', ['Sevoflurane']),
    _study('NCT99000005', 'Lumora light therapy for sleep', 'Unrelated Hospital', ['Lumora'], start='2025-02-01',
           primary='2026-01-01'),
    _study('NCT99000006', 'Inhaler safety study', 'Pearl Co', ['Dry powder inhaler']),
    _study('NCT99000007', 'Meridian Breast System registry', 'Meridian Medical Ltd', ['Meridian Breast System']),
]
# The registry's full-text search returns this record for K990003 although no field holds the number.
PHANTOM_HITS = {'K990003': ['NCT99000004']}


def write_inventory(path: Path) -> Path:
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(inventory.ENRICHED_COLUMNS)
        for d, sn, name, company, panel, code in ROWS:
            w.writerow([d, sn, name, company, panel, code, f"{DOCS}/pdf99/{sn.replace('/', '')}.pdf",
                        'Data/FDA Summary Statements/x.pdf'])
    return path


def write_curated(directory: Path, **rows: list[list[str]]) -> None:
    """Header-only curated files, plus any rows given by file stem (aliases=[[...]], reviews=[[...]])."""
    files = {'organization_aliases': organizations.ALIAS_COLUMNS, 'families': inventory.FAMILY_COLUMNS,
             'reviews': links.REVIEW_COLUMNS, 'documentary_statements': links.STATEMENT_COLUMNS}
    directory.mkdir(parents=True, exist_ok=True)
    for stem, columns in files.items():
        with (directory / f'{stem}.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(columns)
            w.writerows(rows.get(stem.replace('organization_', ''), []))


def _json(obj: Any, status: int = 200) -> acquire.Response:
    return status, {'Content-Type': 'application/json'}, json.dumps(obj).encode(), None


def _search(params: dict[str, Any]) -> acquire.Response:
    blob = {s['protocolSection']['identificationModule']['nctId']: json.dumps(s) for s in STUDIES}
    hits: list[dict[str, Any]] = []
    for s in STUDIES:
        nct, facts = s['protocolSection']['identificationModule']['nctId'], registry.study_facts(s)
        ok = True
        if 'query.term' in params:
            terms = [t.strip().strip('"') for t in params['query.term'].split(' OR ')]
            ok = any(t in blob[nct] or nct in PHANTOM_HITS.get(t, []) for t in terms)
        if ok and 'query.intr' in params:
            ok = any(params['query.intr'].lower() in i['name'].lower() for i in facts['interventions'])
        if ok and 'query.spons' in params:
            names = [facts['lead_sponsor']] + facts['collaborators']
            ok = any(params['query.spons'] in organizations.normalise(n)['normalised'] for n in names)
        if ok:
            hits.append(s)
    size = int(params.get('pageSize', 10))
    return _json({'totalCount': len(hits), 'studies': hits[:size]})


def stub_get(calls: list[str] | None = None, fail: set[str] | None = None) -> acquire.Get:
    """A transport over the tables above. `calls` collects every URL asked; a URL containing any
    string in `fail` gets no answer (a network failure)."""
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        params = params or {}
        if calls is not None:
            calls.append(url + ('?' + json.dumps(params, sort_keys=True) if params else ''))
        if any(x in url or x in json.dumps(params) for x in fail or ()):
            return None, {}, b'', 'ConnectionError: stub'
        if url.startswith(acquire.OPENFDA):
            wanted = re.findall(r'[A-Z]+\d{6}', params['search'])
            out = []
            first_rows = {r[1]: r for r in reversed(ROWS)}.values()     # FDA holds one record per number
            for d, sn, name, company, _, code in first_rows:
                base, _, supp = sn.partition('/')
                iso = inventory.parse_decision_date(d).isoformat()
                if base in wanted and url.endswith('510k.json'):
                    out.append({'k_number': sn, 'applicant': company, 'decision_date': iso, 'product_code': code,
                                'device_name': name, 'statement_or_summary': 'Statement' if sn == 'K990003' else 'Summary',
                                'openfda': {'registration_number': ['1'], 'fei_number': ['2'], 'device_class': '2'}})
                elif base in wanted and url.endswith('pma.json'):
                    out.append({'pma_number': base, 'supplement_number': supp, 'applicant': company,
                                'decision_date': iso, 'product_code': code, 'trade_name': name})
            return _json({'results': out}) if out else _json({'error': {'code': 'NOT_FOUND'}}, 404)
        if url.startswith(acquire.DATABASE_PAGES):
            number = params.get('ID') or params.get('id')
            listed = PAGES.get(number)
            if listed is None:
                return None, {}, b'', 'ConnectionError: stub'
            html = f'<html>{number}' + ''.join(f'<A HREF="http://www.accessdata.fda.gov/cdrh_docs/{p}" TITLE="x">{label}</A>'
                                              for p, label in listed) + '</html>'
            # A record page states its number in labelled rows, as FDA's pages do.
            row = '<TR><TH Align="Left">{}</TH><TD Align="Left">{}</TD></TR>'.format
            pma = re.fullmatch(r'(P\d{6})(S\d{3})?', number)
            if pma:
                rows = row('PMA Number', pma.group(1)) + (row('Supplement Number', pma.group(2)) if pma.group(2) else '')
            else:
                rows = row('De Novo Number' if number.startswith('DEN') else '510(k) Number', number)
            html = html.replace('</html>', rows + '</html>')
            return 200, {}, html.encode(), None
        if url.startswith(DOCS):
            name = url[len(DOCS) + 1:]
            if name in TEXTS:
                return 200, {'Content-Type': 'application/pdf', 'Last-Modified': 'Mon, 01 Jan 2024 00:00:00 GMT'}, \
                    b'%PDF-1.4 fictional ' + name.encode(), None
            return 404, {'Content-Type': 'text/html'}, b'<html>not found</html>', None
        if url == f'{registry.API}/version':
            return _json({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-13T10:00:06'})
        if url == f'{registry.API}/studies':
            return _search(params)
        if url.startswith(f'{registry.API}/studies/'):
            nct = url.rsplit('/', 1)[1]
            for s in STUDIES:
                if s['protocolSection']['identificationModule']['nctId'] == nct:
                    return _json(s)
            return _json({'error': 'not found'}, 404)
        raise AssertionError(f'unexpected request {url}')
    return get


def no_wait() -> acquire.Throttle:
    return acquire.Throttle(sleep=lambda s: None)


def write_text(cache: Path, digest: str, pages: list[str]) -> None:
    """One text-cache entry, in the shape devices/pdftext.py writes."""
    version = pdftext.extraction_version()
    with_text = sum(1 for p in pages if len(re.findall(r'[A-Za-z0-9]', p)) >= pdftext.MIN_ALNUM_PER_PAGE)
    body = {'extraction_version': version, 'status': pdftext.status_of(pages), 'n_pages': len(pages),
            'n_pages_with_text': with_text, 'pages': pages, 'page_errors': {}, 'error': None}
    path = cache / 'text' / f'{digest}.{version}.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))


def acquire_all(tmp: Path, get: acquire.Get | None = None) -> dict[str, Any]:
    """Run every acquisition stage against the stub into tmp/cache and freeze it."""
    get = get or stub_get()
    cache = tmp / 'cache'
    inv = write_inventory(tmp / 'inventory.csv')
    rows = inventory.read_rows(inv, inventory.ENRICHED_COLUMNS)
    subs = inventory.submissions(rows, inventory.ENRICHED_COLUMNS)
    t = no_wait()
    logs = {name: acquire.ResponseLog(cache / name / 'responses.jsonl') for name in ('openfda', 'fda_pages', 'registry')}
    acquire.fetch_openfda(list(subs), logs['openfda'], get, t)
    acquire.fetch_database_pages(list(subs) + ['P990007'], logs['fda_pages'], cache / 'fda_pages' / 'html', get, t)
    manifest = acquire.Manifest(cache / 'documents' / 'manifest.jsonl')
    plan: list[dict[str, Any]] = []
    for sn in subs:
        parent = logs['fda_pages'].get(f"fda_page/{sn.split('/')[0]}") if '/' in sn else None
        plan += acquire.document_plan(sn, logs['fda_pages'].get(f'fda_page/{sn}'), parent)
    acquire.fetch_documents(plan, manifest, cache / 'documents' / 'store', get, t)
    for line in manifest.lines:
        if line['outcome'] == 'retrieved':
            write_text(cache, line['sha256'], TEXTS[line['url'][len(DOCS) + 1:]])
    docs = documents.build(manifest.lines, {sn: s['pathway'] for sn, s in subs.items()}, cache,
                           pdftext.extraction_version())
    registry.fetch_version(logs['registry'], get, t, '2026-11-13')
    registry.fetch_studies(sorted({i['identifier'] for d in docs for i in d['identifiers'] if i['kind'] == 'nct'}),
                           logs['registry'], get, t)
    registry.fetch_number_search(list(subs), logs['registry'], get, t)
    registry.fetch_searches([q for s in subs.values() for q in registry.family_queries([s])], logs['registry'], get, t)
    frozen = {}
    for name, log in logs.items():
        archive = cache / name / 'responses.frozen.jsonl.gz'
        acquire.freeze(log, archive, name)
        frozen[name] = acquire.read_archive(archive)
    return {'cache': cache, 'rows': rows, 'subs': subs, 'manifest': manifest, 'frozen': frozen, 'inventory': inv}


def build_family(tmp: Path, acquired: dict[str, Any] | None = None, curated: Path | None = None,
                 extractions: Path | None = None) -> dict[str, Any]:
    """A full artifact family from the fictional inputs (header-only curated files by default)."""
    a = acquired or acquire_all(tmp)
    if curated is None:
        curated = tmp / 'curated'
        write_curated(curated)
    version = pdftext.extraction_version()

    def observations(docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if extractions is None:
            return [], None
        document_of = {d['submission_number']: d for d in docs if d['official_label'] == 'Classification Order'}
        pages_of = {d['document_id']: (documents.read_text(a['cache'], d['sha256'], version) or {}).get('pages')
                    for d in document_of.values()}
        return demographics.observations(extractions, document_of, pages_of)
    return artifacts.build(
        rows=a['rows'], inventory_meta={'file': 'inventory.csv', 'sha256': inventory.sha256_file(a['inventory']),
                                        'n_rows': len(a['rows'])},
        aliases=organizations.read_aliases(curated / 'organization_aliases.csv'),
        curated_families=inventory.read_curated_families(curated / 'families.csv'),
        reviews=links.read_reviews(curated / 'reviews.csv'),
        statements=links.read_statements(curated / 'documentary_statements.csv'), curated_meta={},
        openfda=a['frozen']['openfda'][0], openfda_meta=a['frozen']['openfda'][1],
        pages=a['frozen']['fda_pages'][0], pages_meta=a['frozen']['fda_pages'][1],
        manifest_lines=a['manifest'].lines, manifest_meta={'n_lines': len(a['manifest'].lines)}, cache=a['cache'],
        extraction_version=version, registry=a['frozen']['registry'][0], registry_meta=a['frozen']['registry'][1],
        demographic_observations=observations, producer={'repository': 'fixture', 'commit': None, 'source_sha256': '0' * 64},
        generated_at=GENERATED_AT)


def pilot_extractions(path: Path) -> Path:
    """A fictional pilot extraction file in the shape of the site's fda_demographics_extracted.json:
    one granting letter read by three models (one well-formed, one nested too deep, one malformed)."""
    def leaf(value: Any = 'Not Reported', quote: str = '', page: int = 0) -> dict[str, Any]:
        return {'value': value, 'exact_quote': quote, 'page_number': page}
    good = {'target_patient_age_range': leaf('adults', 'classification of the Quanta Triage', 1),
            'clinical_study_design': leaf(), 'total_participants': leaf(),
            'sex': {'female': leaf(), 'male': leaf()}, 'gender': {'woman': leaf()},
            'race_nih_omb': {'asian': leaf(), 'white': leaf()}, 'ethnicity': {'hispanic_or_latino': leaf()},
            'socioeconomic_status': {'income': leaf()}}
    entry = {'submission_number': 'DEN990005', 'source_url': f'{DOCS}/pdf99/DEN990005.pdf', 'models': {
        'a': {'model_id': 'fictional-model-a', 'provider': 'x', 'call_succeeded': True, 'data': good},
        'b': {'model_id': 'fictional-model-b', 'provider': 'x', 'call_succeeded': True, 'data': {'data': good}},
        'c': {'model_id': 'fictional-model-c', 'provider': 'x', 'call_succeeded': True,
              'data': {'company_sponsor_name': 'Viza Labs', 'exact_quote': 'Viza', 'page_number': 1}}}}
    path.write_text(json.dumps([entry]))
    return path
