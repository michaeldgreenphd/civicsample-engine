"""The device-linkage artifact family (schema version 1), written to any directory.

  run.json            manifest: run id, inputs and their hashes, every source with its retrieval
                      window and frozen-archive hash, extraction and method versions, the curated
                      files applied, the vocabularies, the counting units, and the sha256 of every
                      other file
  summary.json        every count with its numerator, a named denominator and a definition; plus
                      the breakdown tables. The browser computes no aggregate
  submissions.json    one record per FDA submission number: source rows, conflicts between rows,
                      official metadata and where it disagrees with the list, family, documents
  families.json       candidate and curated device families
  organizations.json  manufacturers: raw spellings, normalised keys, aliases applied
  documents.json      the document registry (type, hash, text status, identifiers, study language)
  studies.json        every registered study that is the target of an evidence record
  links.json          evidence records (devices/links.py) and the pairs they form
  coverage.json       per submission, per route: one of seven states with its reason
  demographics.json   demographic observations carried forward, each on its document and population
  review_queue.json   what a reviewer should look at first, and why

Counting units are never mixed: a count says whether it counts source rows, submissions,
families, documents, evidence records, pairs or studies.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from devices import acquire as A
from devices import coverage as C
from devices import documents as D
from devices import inventory as I
from devices import links as L
from devices import organizations as O
from devices import registry as R

SCHEMA_VERSION = 1
METHOD_VERSION = 'device-links-1'
FILES = ('summary.json', 'submissions.json', 'families.json', 'organizations.json', 'documents.json',
         'studies.json', 'links.json', 'coverage.json', 'demographics.json', 'review_queue.json')
COUNTING_UNITS = {
    'source_row': 'one line of FDA\'s AI/ML-enabled device list as downloaded; what the AI Devices tab counts today',
    'submission': 'one distinct FDA submission number; rows sharing a number are one submission',
    'family': 'submissions grouped as one product line: candidate (same organisation and whole core name) or '
              'curated (devices/families.csv). Not a count of products unless curated',
    'organization': 'one canonical manufacturer key: spellings joined by the normalisation rules or the alias file',
    'document': 'one source URL for one submission; a version is one sha256 of its bytes',
    'evidence_record': 'one piece of evidence joining a submission or family to a registered study',
    'pair': 'one distinct (submission or family, registered study) joined by at least one evidence record',
    'study': 'one distinct NCT id',
}


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode()


def _year_band(iso: str | None) -> str:
    """Decision-year bands for tables: sparse early years pooled, as the audit's sample strata were."""
    if not iso:
        return 'unknown'
    y = int(iso[:4])
    return '<=2015' if y <= 2015 else '2016-2018' if y <= 2018 else str(y)


def _official_differences(sub: dict[str, Any], official: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Where FDA's record and the list disagree, field by field. Recorded, never resolved: the
    list value stays the listed value and the official one stands beside it."""
    if not official.get('available'):
        return {}
    diff: dict[str, dict[str, Any]] = {}
    if official['decision_date'] and sub['decision_date'] and official['decision_date'] != sub['decision_date']:
        diff['decision_date'] = {'listed': sub['decision_date'], 'official': official['decision_date']}
    if official['product_code'] and sub['product_code'] and official['product_code'] != sub['product_code']:
        diff['product_code'] = {'listed': sub['product_code'], 'official': official['product_code']}
    if official['applicant'] and all(O.normalise(official['applicant'])['normalised'] != O.normalise(c)['normalised']
                                     for c in sub['companies_as_listed']):
        diff['applicant'] = {'listed': sub['companies_as_listed'], 'official': official['applicant']}
    if official['device_name'] and all(I.norm_text(O.clean_display(official['device_name'])) !=
                                       I.norm_text(O.clean_display(n)) for n in sub['device_names_as_listed']):
        diff['device_name'] = {'listed': sub['device_names_as_listed'], 'official': official['device_name']}
    return diff


def _metric(mid: str, numerator: int, denominator: str, definition: str) -> dict[str, Any]:
    return {'id': mid, 'numerator': numerator, 'denominator': denominator, 'definition': definition}


def build(*, rows: list[dict[str, Any]], inventory_meta: dict[str, Any], aliases: dict[str, dict[str, str]],
          curated_families: dict[str, dict[str, str]], reviews: dict[str, dict[str, str]],
          statements: list[dict[str, str]], curated_meta: dict[str, Any],
          openfda: dict[str, dict[str, Any]], openfda_meta: dict[str, Any] | None,
          pages: dict[str, dict[str, Any]], pages_meta: dict[str, Any] | None,
          manifest_lines: list[dict[str, Any]], manifest_meta: dict[str, Any], cache: Path, extraction_version: str,
          registry: dict[str, dict[str, Any]], registry_meta: dict[str, Any] | None,
          demographic_observations: Any, producer: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """Assemble every file of the family from frozen inputs. No network, no clock (generated_at is
    passed in), no randomness: the same inputs give the same bytes."""
    # ---- inventory: rows -> submissions -> organisations -> families
    subs = I.submissions(rows, I.ENRICHED_COLUMNS)
    reconciliation = I.reconcile(rows, subs)
    orgs = O.build([c for s in subs.values() for c in s['companies_as_listed']], aliases)
    org_of = {sn: O.organization_id(O.canonical(O.normalise(s['companies_as_listed'][0])['normalised'], aliases))
              for sn, s in subs.items()}
    fams = I.families(subs, org_of, curated_families)
    family_of = {sn: f['family_id'] for f in fams.values() for sn in f['submission_numbers']}
    pathway_of = {sn: s['pathway'] for sn, s in subs.items()}

    # ---- official metadata
    fda = A.openfda_by_submission(openfda, list(subs)) if openfda_meta else {}
    for sn, s in subs.items():
        s['organization_id'], s['family_id'] = org_of[sn], family_of[sn]
        s['official'] = A.official_metadata(fda[sn]) if sn in fda else {'available': False, 'reason': 'not requested'}
        s['official_differs_from_list'] = _official_differences(s, s['official'])
        s['name_forms'] = I.name_forms(s['device_names_as_listed'][0],
                                       O.normalise(s['companies_as_listed'][0])['normalised'])

    # ---- documents
    labels = {item['document_id']: item['official_label'] for sn in subs
              for item in A.document_plan(sn, pages.get(f'fda_page/{sn}')) if item['official_label']
              and item['discovery'] == 'fda_database_page'}
    docs = D.build(manifest_lines, pathway_of, cache, extraction_version, labels)
    docs_of: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        docs_of.setdefault(d['submission_number'], []).append(d)

    # ---- registry version stamp
    versions = sorted((r for k, r in registry.items() if k.startswith('ctgov/version/')), key=lambda r: r['fetched_at'])
    api = (versions[-1].get('body') or {}) if versions else {}
    registry_version = {'registry': 'ClinicalTrials.gov', 'registry_api_version': api.get('apiVersion'),
                        'registry_data_timestamp': api.get('dataTimestamp')}

    # ---- evidence records
    links: list[dict[str, Any]] = []
    studies: dict[str, dict[str, Any]] = {}

    def remember(nct: str, facts: dict[str, Any] | None, state: str, retrieved_at: str | None) -> None:
        if nct not in studies or (facts and not studies[nct].get('brief_title')):
            studies[nct] = {'nct_id': nct, 'registry_state': state, 'retrieved_at': retrieved_at,
                            **{k: v for k, v in (facts or {}).items() if k not in ('nct_id', 'returned_by', 'retrieved_at')}}
    # route: NCT ids cited in documents
    for d in docs:
        seen_ids: set[str] = set()
        for ident in d['identifiers']:
            if ident['kind'] != 'nct' or ident['identifier'] in seen_ids:
                continue   # one record per (document, id): the first page it stands on
            seen_ids.add(ident['identifier'])
            state, facts = R.nct_state(registry, ident['identifier'])
            rec = registry.get(f"ctgov/study/{ident['identifier']}")
            remember(ident['identifier'], facts, state, (rec or {}).get('fetched_at'))
            link = L.from_document_citation(d, ident, state, facts, subs[d['submission_number']]['decision_date'],
                                            {**registry_version, 'registry_retrieved_at': (rec or {}).get('fetched_at')})
            link['evidence']['other_pages'] = sorted({i['page'] for i in d['identifiers'] if i['kind'] == 'nct'
                                                      and i['identifier'] == ident['identifier']} - {ident['page']})
            links.append(link)
    # route: registry records citing a submission number
    number_results = R.number_search_results(registry, list(subs)) if registry_meta else {}
    for sn, result in number_results.items():
        for hit in result['hits']:
            remember(hit['nct_id'], hit['study'], 'resolves', hit.get('retrieved_at'))
            links += L.from_registry_citation(sn, hit, subs[sn]['decision_date'], registry_version)
    # route: name and organisation (family level)
    name_search: dict[str, dict[str, Any]] = {}
    for fid, f in fams.items():
        members = [subs[sn] for sn in f['submission_numbers']]
        found = R.family_name_search(members, registry, aliases) if registry_meta else None
        if found is None:
            continue
        name_search[fid] = found
        decisions = {m['submission_number']: m['decision_date'] for m in members}
        for m in found['matches']:
            remember(m['study']['nct_id'], m['study'], 'resolves', m['study'].get('retrieved_at'))
            links.append(L.from_name_match(f, m['study'], m['name'], m['organisation'], decisions, registry_version,
                                           m['flags'], m['common_word']))
    # route: documentary statements entered by a person
    doc_by_id = {d['document_id']: d for d in docs}
    for row in statements:
        if row['submission_number'] not in subs:
            raise ValueError(f"documentary_statements.csv: {row['submission_number']} is not in the inventory")
        text, version = None, {}
        if row['source_kind'] == 'fda_document' and row['source_id'] in doc_by_id:
            d = doc_by_id[row['source_id']]
            if d['submission_number'] != row['submission_number'] or d.get('scope') != 'this_submission':
                # A parent PMA's document is filed under the supplement but is the original
                # approval's; it supports family-level evidence only, never a submission's statement.
                # A statement must be read from the submission's own document; another
                # submission's would attribute its evidence to the wrong device.
                raise ValueError(f"documentary_statements.csv: {row['source_id']} is a document of "
                                 f"{d['submission_number']} ({d.get('scope')}), not of {row['submission_number']} itself")
            cached = D.read_text(cache, d['sha256'], extraction_version) if d['sha256'] else None
            page = int(row['page_or_field']) if row['page_or_field'].strip().isdigit() else 0
            if cached and 1 <= page <= len(cached['pages']):
                text = cached['pages'][page - 1]
            version = {'document_sha256': d['sha256'], 'retrieved_at': d['retrieved_at']}
        elif row['source_kind'] == 'registry_record':
            # The quote must come from the target study's own record; another study's record would
            # attribute its words to the target.
            if row['source_id'].strip() != row['nct_id']:
                raise ValueError(f"documentary_statements.csv: the registry record cited ({row['source_id']}) "
                                 f"is not the statement's study ({row['nct_id']})")
            rec = registry.get(f"ctgov/study_full/{row['source_id']}") or registry.get(f"ctgov/study/{row['source_id']}")
            own = ((((rec or {}).get('body') or {}).get('protocolSection') or {}).get('identificationModule') or {}).get('nctId')
            if rec and own and own != row['nct_id']:
                raise ValueError(f"documentary_statements.csv: the record held for {row['source_id']} is study {own}")
            # The quote is read only from a record answered 200 that names itself as the
            # statement's study; a record without its own id has no established owner, so the
            # quote reads not validated. It is checked only in the field the row names (a dotted
            # path such as protocolSection.descriptionModule.briefSummary, with [i] for list
            # items); a path that does not resolve gives no text, so the quote reads not validated.
            owned = rec is not None and rec.get('status') == 200 and own == row['nct_id']
            text = registry_field_text(rec['body'], row['page_or_field'].strip()) if owned else None
            version = {**registry_version, 'retrieved_at': (rec or {}).get('fetched_at')}
        state, facts = R.nct_state(registry, row['nct_id'])
        remember(row['nct_id'], facts, state, None)
        links.append(L.from_statement(row, text, version or {'unavailable': 'source not held'}, facts,
                                      subs[row['submission_number']]['decision_date']))
    if len({x['link_id'] for x in links}) != len(links):
        dup = [k for k, v in Counter(x['link_id'] for x in links).items() if v > 1]
        raise ValueError(f'evidence records collide on link_id: {dup[:5]}')
    orphaned = L.apply_reviews(links, reviews)
    links.sort(key=lambda x: (x['source']['id'], x['target']['id'], x['link_id']))

    # ---- demographics
    observations, demographics_meta = demographic_observations(docs) if demographic_observations else ([], None)
    obs_of: dict[str, list[dict[str, Any]]] = {}
    for o in observations:
        obs_of.setdefault(o['submission_number'], []).append(o)

    # ---- coverage
    sub_links: dict[str, list[dict[str, Any]]] = {sn: [] for sn in subs}
    fam_links: dict[str, list[dict[str, Any]]] = {fid: [] for fid in fams}
    for x in links:
        (sub_links if x['source']['entity_type'] == 'submission' else fam_links)[x['source']['id']].append(x)
    coverage: dict[str, dict[str, Any]] = {}
    for sn, s in subs.items():
        mine, own_docs = sub_links[sn], docs_of.get(sn, [])
        doc_links = [x for x in mine if x['evidence']['source_kind'] == 'fda_document'
                     and x['method'] != 'documentary_statement']
        found = name_search.get(family_of[sn])
        cells = [
            C.local_document(own_docs, doc_links),
            C.fda_document_fetch(pages.get(f'fda_page/{sn}'), own_docs, doc_links),
            C.openfda_metadata(fda.get(sn)),
            C.nct_citation_scan(own_docs, doc_links),
            C.registry_submission_number_search(
                number_results.get(sn), [x for x in mine if x['method'] == 'submission_number_in_registry_record']),
            C.registry_name_organisation_search(
                found['search'] if found else None,
                [x for x in fam_links[family_of[sn]] if x['method'] in L.HEURISTIC_METHODS],
                found['tallies'] if found else None),
            C.demographic_extraction(obs_of.get(sn, []), own_docs),
        ]
        coverage[sn] = {'submission_number': sn, 'overall': C.overall(cells), 'routes': cells}
        s['coverage_overall'] = coverage[sn]['overall']['state']
        s['document_ids'] = [d['document_id'] for d in own_docs]
        s['n_evidence_records'] = len(mine)
        s['n_family_evidence_records'] = len(fam_links[family_of[sn]])
    for fid, f in fams.items():
        f['n_evidence_records'] = len(fam_links[fid])
        f['name_search_tallies'] = (name_search.get(fid) or {}).get('tallies')

    # ---- pairs: unique (entity, study), as distinct from evidence records and from studies
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for x in links:
        key = (x['source']['id'], x['target']['id'])
        p = pairs.setdefault(key, {'source': x['source'], 'study': x['target']['id'], 'level': x['level'],
                                   'link_ids': [], 'methods': [], 'automated_validation': [], 'human_review': []})
        p['link_ids'].append(x['link_id'])
        for field, value in (('methods', x['method']), ('automated_validation', x['automated_validation']),
                             ('human_review', x['human_review'])):
            if value not in p[field]:
                p[field].append(value)
        if x['level'] == 'submission':
            p['level'] = 'submission'

    files: dict[str, Any] = {
        'submissions.json': {'submissions': list(subs.values()), 'reconciliation': reconciliation},
        'families.json': {'families': list(fams.values())},
        'organizations.json': {'organizations': list(orgs.values())},
        'documents.json': {'documents': [_slim_document(d) for d in docs]},
        'studies.json': {'studies': [studies[k] for k in sorted(studies)]},
        'links.json': {'links': links, 'pairs': [pairs[k] for k in sorted(pairs)], 'orphaned_reviews': orphaned},
        'coverage.json': {'states': list(C.STATES), 'routes': C.ROUTES, 'overall_precedence': list(C.OVERALL_PRECEDENCE),
                          'submissions': [coverage[sn] for sn in subs]},
        'demographics.json': {'observations': observations, 'source': demographics_meta},
        'review_queue.json': review_queue(subs, fams, docs_of, links, obs_of),
    }
    files['summary.json'] = summarise(rows, subs, fams, orgs, docs, links, pairs, studies, coverage, observations,
                                      reconciliation, name_search, number_results)
    for name in FILES:
        files[name] = {'schema_version': SCHEMA_VERSION, **files[name]}
    files['run.json'] = {
        'schema_version': SCHEMA_VERSION, 'producer': producer,
        'method_version': METHOD_VERSION, 'scheduled': False,
        'counting_units': COUNTING_UNITS,
        'inputs': {'inventory': inventory_meta, 'curated': curated_meta},
        'sources': {'openfda': openfda_meta, 'fda_database_pages': pages_meta, 'fda_documents': manifest_meta,
                    'registry': {**(registry_meta or {}), **registry_version} if registry_meta else None,
                    'text_extraction': {'extraction_version': extraction_version, 'ocr': False},
                    'demographic_extractions': demographics_meta},
        'vocabularies': {
            'pathways': list(I.PATHWAYS), 'duplicate_classes': list(I.DUPLICATE_CLASSES),
            'document_types': {k: {'category': v[0], 'demographic_summary_type': v[1]} for k, v in D.DOCUMENT_TYPES.items()},
            'document_outcomes': list(A.DOCUMENT_OUTCOMES), 'text_statuses': list(D.pdftext.TEXT_STATUSES) + ['not_extracted'],
            'study_description_states': list(D.STUDY_DESCRIPTION_STATES),
            'link_levels': list(L.LEVELS), 'link_methods': list(L.METHODS),
            'heuristic_methods': list(L.HEURISTIC_METHODS), 'identifier_methods': list(L.IDENTIFIER_METHODS),
            'proposed_relationships': list(L.PROPOSED_RELATIONSHIPS),
            'reviewed_relationships': list(L.REVIEWED_RELATIONSHIPS),
            'automated_validation': list(L.AUTOMATED_VALIDATION), 'human_review': list(L.HUMAN_REVIEW),
            'confidence': list(L.CONFIDENCE), 'timing_relations': list(L.TIMING_RELATIONS),
            'coverage_states': list(C.STATES), 'coverage_routes': list(C.ROUTES), 'nct_states': list(R.NCT_STATES)},
        'reviews': {'n_rows': len(reviews), 'n_applied': len(reviews) - len(orphaned), 'orphaned_link_ids': orphaned},
    }
    # The id follows from what is published (run_identity): the inputs and sources, the producing
    # commit and code digest are in the manifest, and what they gave is in the other files. A list
    # of inputs would have to be kept complete by hand, and would give one id to builds made from
    # different code.
    run_id = f'devices-{generated_at[:10]}-{run_identity(files)}'
    for name in FILES:
        files[name] = {'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                       **{k: v for k, v in files[name].items() if k != 'schema_version'}}
    files['run.json'] = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'generated_at': generated_at,
                         **{k: v for k, v in files['run.json'].items() if k != 'schema_version'}}
    files['run.json']['files'] = {name: sha256(dump(files[name])) for name in FILES}
    return files


def run_identity(files: dict[str, Any]) -> str:
    """The ten hex digits that end a run id: a digest of everything the family publishes except the
    id itself, the build time and the file checksums (which cover the id). Two families that differ
    in any published value, the producing commit and code digest included, never share an id; the
    same content gives the same id whenever it is built. `files` maps every file name, run.json
    included, to its object, with or without `run_id` set."""
    run = {k: v for k, v in files['run.json'].items() if k not in ('run_id', 'generated_at', 'files')}
    content = {name: sha256(dump({k: v for k, v in files[name].items() if k != 'run_id'})) for name in sorted(FILES)}
    return sha256(dump([run, content]))[:10]


def registry_field_text(record: Any, path: str) -> str | None:
    """The text at a dotted path in a registry record (`a.b[0].c`), or None when the path does not
    resolve. A value that is not a string is given as its JSON, so a number or list can be quoted."""
    import re
    node = record
    for part in re.findall(r'[^.\[\]]+|\[\d+\]', path or ''):
        try:
            node = node[int(part[1:-1])] if part.startswith('[') else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    if node is None or node is record:
        return None
    return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)


def _slim_document(d: dict[str, Any]) -> dict[str, Any]:
    """The published document record: study identifiers in full; other FDA numbers cited in the
    text reduced to the distinct numbers with the first page each stands on."""
    out = dict(d)
    cited: dict[str, int] = {}
    for i in d['identifiers']:
        if i['kind'] == 'submission_number':
            cited.setdefault(i['identifier'], i['page'])
    out['identifiers'] = [i for i in d['identifiers'] if i['kind'] != 'submission_number']
    out['other_submission_numbers_cited'] = [{'number': k, 'first_page': v, 'role': 'undetermined'}
                                             for k, v in sorted(cited.items())]
    return out


def review_queue(subs: dict[str, dict[str, Any]], fams: dict[str, dict[str, Any]],
                 docs_of: dict[str, list[dict[str, Any]]], links: list[dict[str, Any]],
                 obs_of: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What to review first. Four tiers, each a list with the reason an item is in it:

      1  submissions with an identifier-confirmed link awaiting a person's reading of what the
         citation means
      2  families with a name-and-organisation candidate (moderate confidence first)
      3  submissions holding a substantive document with demographic language and no demographic
         extraction: evidence exists and has not been read
      4  families with name-only candidates, and cited identifiers that did not resolve
    Within a tier, items are ordered by how much evidence they hold, then by id. Reviewed items
    stay listed with their decision, so work is not repeated.
    """
    tiers: dict[str, list[dict[str, Any]]] = {'1_identifier_confirmed': [], '2_name_and_organisation': [],
                                              '3_documents_without_demographic_evidence': [],
                                              '4_name_only_or_unresolved_identifier': []}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for x in links:
        by_source.setdefault(x['source']['id'], []).append(x)
    for sid, ls in sorted(by_source.items()):
        confirmed = [x for x in ls if x['automated_validation'] == 'identifier_confirmed']
        strong = [x for x in ls if x['method'] == 'device_name_and_organisation_match']
        weak = [x for x in ls if (x['method'] == 'device_name_match' and x['automated_validation'] != 'rejected_by_rule')
                or x['automated_validation'] == 'identifier_not_resolved']
        label = subs[sid]['device_name'] if sid in subs else fams[sid]['family_name']
        for tier, chosen in (('1_identifier_confirmed', confirmed), ('2_name_and_organisation', strong),
                             ('4_name_only_or_unresolved_identifier', weak)):
            if chosen and not (tier.startswith('4') and (confirmed or strong)):
                tiers[tier].append({
                    'entity': sid, 'name': label, 'n_evidence_records': len(chosen),
                    'studies': sorted({x['target']['id'] for x in chosen}),
                    'n_unreviewed': sum(1 for x in chosen if x['human_review'] == 'unreviewed'),
                    'link_ids': [x['link_id'] for x in chosen],
                    'submission_numbers': [sid] if sid in subs else fams[sid]['submission_numbers']})
    for sn, s in subs.items():
        if obs_of.get(sn) and any(o['n_demographic_fields_reported'] for o in obs_of[sn]):
            continue
        cands = [d for d in docs_of.get(sn, []) if d['demographic_summary_candidate']]
        scored = []
        for d in cands:
            terms = (d['study_language'] or {}).get('demographic_terms') or {}
            scored.append((int(bool(terms.get('race_or_ethnicity', {}).get('n_mentions'))),
                           int(bool(terms.get('sex_or_gender', {}).get('n_mentions'))), d))
        if scored:
            race, sex = max(x[0] for x in scored), max(x[1] for x in scored)
            tiers['3_documents_without_demographic_evidence'].append({
                'entity': sn, 'name': s['device_name'], 'pathway': s['pathway'],
                'document_ids': [x[2]['document_id'] for x in scored],
                'document_types': sorted({x[2]['document_type'] for x in scored}),
                'text_mentions_race_or_ethnicity': bool(race), 'text_mentions_sex_or_gender': bool(sex),
                'study_description': sorted({x[2]['study_description'] for x in scored}),
                'has_identifier_confirmed_link': any(x['automated_validation'] == 'identifier_confirmed'
                                                     for x in by_source.get(sn, []))})
    tiers['1_identifier_confirmed'].sort(key=lambda r: (-r['n_evidence_records'], r['entity']))
    tiers['2_name_and_organisation'].sort(key=lambda r: (-r['n_evidence_records'], r['entity']))
    tiers['4_name_only_or_unresolved_identifier'].sort(key=lambda r: (-r['n_evidence_records'], r['entity']))
    tiers['3_documents_without_demographic_evidence'].sort(
        key=lambda r: (-int(r['has_identifier_confirmed_link']), -int(r['text_mentions_race_or_ethnicity']),
                       -int(r['text_mentions_sex_or_gender']), {'pma': 0, 'pma_supplement': 0, 'de_novo': 1}.get(r['pathway'], 2),
                       r['entity']))
    return {'tiers': tiers, 'n_by_tier': {k: len(v) for k, v in tiers.items()}}


def _table(pairs_: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for a, b in pairs_:
        out.setdefault(a, {})
        out[a][b] = out[a].get(b, 0) + 1
    return {a: dict(sorted(v.items())) for a, v in sorted(out.items())}


def summarise(rows: list[dict[str, Any]], subs: dict[str, dict[str, Any]], fams: dict[str, dict[str, Any]],
              orgs: dict[str, dict[str, Any]], docs: list[dict[str, Any]], links: list[dict[str, Any]],
              pairs: dict[tuple[str, str], dict[str, Any]], studies: dict[str, dict[str, Any]],
              coverage: dict[str, dict[str, Any]], observations: list[dict[str, Any]],
              reconciliation: dict[str, Any], name_search: dict[str, dict[str, Any]],
              number_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every published count: a numerator, a NAMED denominator and a definition."""
    held = [d for d in docs if d['availability'] == 'retrieved']
    readable = [d for d in held if (d['text'] or {}).get('status') in ('text', 'partial')]
    own_held = [d for d in held if d['scope'] == 'this_submission']
    subs_with_doc = {d['submission_number'] for d in own_held}
    subs_with_substantive = {d['submission_number'] for d in own_held if d['demographic_summary_candidate']}
    den = {
        'source_rows': {'n': len(rows), 'definition': COUNTING_UNITS['source_row']},
        'submissions': {'n': len(subs), 'definition': COUNTING_UNITS['submission']},
        'families': {'n': len(fams), 'definition': COUNTING_UNITS['family']},
        'organizations': {'n': len(orgs), 'definition': COUNTING_UNITS['organization']},
        'duplicated_numbers': {'n': reconciliation['n_duplicated_numbers'],
                               'definition': 'submission numbers that appear on more than one source row'},
        'documents_tried': {'n': len(docs), 'definition': 'distinct (submission, source URL) addresses requested or recovered'},
        'documents_retrieved': {'n': len(held), 'definition': 'documents whose bytes are held under a recorded sha256'},
        'documents_readable': {'n': len(readable), 'definition': 'retrieved documents with a text layer on at least one page'},
        'substantive_documents_readable': {
            'n': sum(1 for d in readable if d['demographic_summary_candidate']),
            'definition': 'readable documents of a type that describes a study population (510(k) summary, decision '
                          'summary, summary of safety and effectiveness) and belong to the submission itself'},
        'evidence_records': {'n': len(links), 'definition': COUNTING_UNITS['evidence_record']},
        'pairs': {'n': len(pairs), 'definition': COUNTING_UNITS['pair']},
        'studies': {'n': len(studies), 'definition': 'distinct NCT ids that are the target of at least one evidence record'},
        'demographic_extractions': {'n': len(observations), 'definition': 'one model output for one document, carried forward'},
    }
    by = lambda pred: sum(1 for x in links if pred(x))                       # noqa: E731
    sub_pairs = lambda pred: len({k for k, p in pairs.items() if pred(p)})   # noqa: E731
    confirmed = [x for x in links if x['automated_validation'] == 'identifier_confirmed']
    metrics = [
        _metric('extra_rows', reconciliation['n_extra_rows'], 'source_rows',
                'source rows beyond the first for a submission number (rows minus distinct submissions)'),
        _metric('duplicates_identical', len(reconciliation['duplicated_numbers_by_class']['identical']), 'duplicated_numbers',
                'duplicated numbers whose rows are byte-identical in every column'),
        _metric('duplicates_alias_only', len(reconciliation['duplicated_numbers_by_class']['alias_only']), 'duplicated_numbers',
                'duplicated numbers whose rows differ only in a company or device spelling that normalises to the same key'),
        _metric('duplicates_substantive_conflict', len(reconciliation['duplicated_numbers_by_class']['substantive_conflict']),
                'duplicated_numbers', 'duplicated numbers whose rows disagree on anything else'),
        _metric('families_curated', sum(1 for f in fams.values() if f['status'] == 'curated'), 'families',
                'families listed in devices/families.csv; all others are candidates grouped by rule'),
        _metric('families_with_several_submissions', sum(1 for f in fams.values() if f['n_submissions'] > 1), 'families',
                'families holding more than one submission'),
        _metric('submissions_in_multi_submission_families',
                sum(f['n_submissions'] for f in fams.values() if f['n_submissions'] > 1), 'submissions',
                'submissions that share their family with at least one other submission; a name match cannot tell them apart'),
        _metric('organizations_with_several_spellings', sum(1 for o in orgs.values() if len(o['raw_names']) > 1),
                'organizations', 'organisations listed under more than one raw spelling'),
        _metric('organizations_merged_by_alias', sum(1 for o in orgs.values() if o['aliases_applied']), 'organizations',
                'organisations formed by a curated alias (the only way differently named organisations merge)'),
        _metric('official_record_found', sum(1 for s in subs.values() if s['official'].get('available')), 'submissions',
                'submissions for which openFDA returned exactly one record with the number'),
        _metric('official_differs_from_list', sum(1 for s in subs.values() if s['official_differs_from_list']), 'submissions',
                'submissions whose official record disagrees with the list on decision date, product code, applicant or device name'),
        _metric('submissions_with_document', len(subs_with_doc), 'submissions',
                'submissions with at least one retrieved document of their own'),
        _metric('submissions_with_substantive_readable_document', len(subs_with_substantive), 'submissions',
                'submissions holding a readable document of a type that describes a study population'),
        _metric('documents_retrieved', len(held), 'documents_tried', 'addresses that returned a PDF now held by hash'),
        _metric('documents_not_found', sum(1 for d in docs if d['availability'] == 'not_found'), 'documents_tried',
                'addresses the host answered 404 for'),
        _metric('documents_failed', sum(1 for d in docs if d['availability'] in ('http_error', 'network_error', 'not_a_pdf',
                                                                                'too_large', 'skipped_disk_budget')),
                'documents_tried', 'addresses that failed for any other reason (see table documents_by_availability)'),
        _metric('documents_image_only', sum(1 for d in held if (d['text'] or {}).get('status') == 'image_only'),
                'documents_retrieved', 'retrieved documents with no text layer on any page (scans; no OCR is run)'),
        _metric('documents_with_garbled_pages', sum(1 for d in held if (d['text'] or {}).get('n_pages_garbled')),
                'documents_retrieved',
                'retrieved documents with at least one page whose text layer is unreadable (fonts without a character '
                'map); those pages are treated as having no text'),
        _metric('documents_with_unread_pages',
                sum(1 for d in held if ((d['text'] or {}).get('n_pages') or 0) > ((d['text'] or {}).get('n_pages_with_text') or 0)),
                'documents_retrieved', 'retrieved documents with at least one page that has no usable text layer'),
        _metric('documents_text_failed', sum(1 for d in held if (d['text'] or {}).get('status') in ('failed', 'not_extracted')),
                'documents_retrieved', 'retrieved documents the extractor could not read or has not read'),
        _metric('documents_citing_nct', sum(1 for d in readable if d['study_description'] == 'cites_registry_identifier'),
                'documents_readable', 'readable documents whose text contains at least one NCT id'),
        _metric('substantive_documents_describing_study_without_registry_id',
                sum(1 for d in readable if d['demographic_summary_candidate']
                    and d['study_description'] == 'describes_study_no_registry_identifier'),
                'substantive_documents_readable',
                'substantive readable documents with study language (keyword rule) and no NCT id: a negative finding'),
        _metric('substantive_documents_stating_no_clinical_study',
                sum(1 for d in readable if d['demographic_summary_candidate']
                    and d['study_description'] == 'states_no_clinical_study_only'),
                'substantive_documents_readable',
                'substantive readable documents whose only study language says no clinical study was done'),
        _metric('evidence_records_submission_level', by(lambda x: x['level'] == 'submission'), 'evidence_records',
                'evidence records whose source is one submission'),
        _metric('evidence_records_family_level', by(lambda x: x['level'] == 'family'), 'evidence_records',
                'evidence records whose source is a family (or a parent submission): not specific to one submission'),
        _metric('evidence_records_identifier_confirmed', len(confirmed), 'evidence_records',
                'records where an identifier in a retrieved source resolves to the other entity (automated check)'),
        _metric('evidence_records_identifier_not_resolved', by(lambda x: x['automated_validation'] == 'identifier_not_resolved'),
                'evidence_records', 'records whose cited NCT id the registry does not hold: unresolved'),
        _metric('evidence_records_rejected_by_rule', by(lambda x: x['automated_validation'] == 'rejected_by_rule'),
                'evidence_records', 'search hits kept for visibility that fail an explicit rule'),
        _metric('evidence_records_not_validated', by(lambda x: x['automated_validation'] == 'not_validated'),
                'evidence_records', 'heuristic or otherwise unconfirmed records'),
        _metric('evidence_records_name_and_organisation', by(lambda x: x['method'] == 'device_name_and_organisation_match'),
                'evidence_records', 'family-level candidates: device name in a title or intervention and the sponsor agrees'),
        _metric('evidence_records_name_only', by(lambda x: x['method'] == 'device_name_match'
                                                  and x['automated_validation'] == 'not_validated'), 'evidence_records',
                'family-level candidates: device name in a title or intervention, sponsor does not agree'),
        _metric('evidence_records_name_only_common_word', by(lambda x: x['method'] == 'device_name_match'
                                                              and x['automated_validation'] == 'rejected_by_rule'),
                'evidence_records',
                'name-only matches rejected by rule: the name floods the registry and no organisation agrees'),
        _metric('evidence_records_human_reviewed', by(lambda x: x['human_review'] != 'unreviewed'), 'evidence_records',
                'records a person has decided (accepted, rejected or unresolved) in devices/reviews.csv'),
        _metric('pairs_submission_specific', sub_pairs(lambda p: p['level'] == 'submission' and
                                                        'identifier_confirmed' in p['automated_validation']), 'pairs',
                'distinct (submission, study) pairs with at least one identifier-confirmed record'),
        _metric('pairs_family_only', sub_pairs(lambda p: p['source']['entity_type'] == 'family'
                                               and p['automated_validation'] != ['rejected_by_rule']), 'pairs',
                'distinct (family, study) pairs resting on name evidence only, not rejected by rule'),
        _metric('pairs_rejected_by_rule', sub_pairs(lambda p: p['automated_validation'] == ['rejected_by_rule']), 'pairs',
                'distinct pairs whose every evidence record was rejected by rule'),
        _metric('pairs_parent_submission_only', sub_pairs(lambda p: p['methods'] == ['nct_id_in_parent_submission_document']),
                'pairs', 'distinct pairs resting only on a citation in the ORIGINAL PMA of a listed supplement'),
        _metric('submissions_with_identifier_confirmed_link', len({x['source']['id'] for x in confirmed}), 'submissions',
                'submissions with at least one identifier-confirmed record, by any route'),
        _metric('submissions_with_nct_cited_in_own_document',
                len({x['source']['id'] for x in confirmed if x['method'] == 'nct_id_in_submission_document'}), 'submissions',
                'submissions whose own document cites an NCT id that resolves'),
        _metric('submissions_cited_in_registry_record',
                len({x['source']['id'] for x in confirmed if x['method'] == 'submission_number_in_registry_record'}),
                'submissions', 'submissions whose exact number stands in a retrieved field of a registry record'),
        _metric('studies_identifier_confirmed', len({x['target']['id'] for x in confirmed}), 'studies',
                'distinct studies joined to a submission by an identifier-confirmed record'),
        _metric('families_with_name_and_organisation_candidate',
                len({x['source']['id'] for x in links if x['method'] == 'device_name_and_organisation_match'}), 'families',
                'families with at least one name-and-organisation candidate'),
        _metric('families_with_name_only_candidate_only',
                len({x['source']['id'] for x in links if x['method'] == 'device_name_match'
                     and x['automated_validation'] == 'not_validated'} -
                    {x['source']['id'] for x in links if x['method'] == 'device_name_and_organisation_match'}), 'families',
                'families whose only candidates are name-only (not rejected by rule)'),
        _metric('submissions_in_families_with_name_and_organisation_candidate',
                len({sn for sn, s in subs.items() if s['family_id'] in
                     {x['source']['id'] for x in links if x['method'] == 'device_name_and_organisation_match'}}),
                'submissions', 'submissions whose family has a name-and-organisation candidate; the candidate is not '
                               'specific to any one of them'),
        _metric('demographic_extractions_with_any_value', sum(1 for o in observations if o['n_demographic_fields_reported']),
                'demographic_extractions', 'carried-forward extractions that report at least one demographic value'),
        _metric('demographic_extractions_malformed', sum(1 for o in observations if o['extraction']['parse_state'] == 'malformed'),
                'demographic_extractions', 'carried-forward model outputs with demographic sections missing (parsing failure)'),
    ]
    tallies = Counter()
    for found in name_search.values():
        tallies.update(found['tallies'])
    tables = {
        'submissions_by_pathway': dict(sorted(Counter(s['pathway'] for s in subs.values()).items())),
        'submissions_by_decision_year_band': dict(sorted(Counter(_year_band(s['decision_date']) for s in subs.values()).items())),
        'families_by_size': {str(k): v for k, v in sorted(Counter(f['n_submissions'] for f in fams.values()).items())},
        'families_by_grouping_reason': dict(sorted(Counter(f['grouping_reason'] for f in fams.values()).items())),
        'statement_or_summary_flag': dict(sorted(Counter(str(s['official'].get('statement_or_summary'))
                                                         for s in subs.values() if s['pathway'] == '510k').items())),
        'official_differences_by_field': dict(sorted(Counter(k for s in subs.values() for k in s['official_differs_from_list']).items())),
        'documents_by_availability': dict(sorted(Counter(d['availability'] for d in docs).items())),
        'documents_not_retrieved_by_http_status': dict(sorted(Counter(str(d['http_status']) for d in docs
                                                                      if d['availability'] != 'retrieved').items())),
        'documents_by_discovery': _table([(d['discovery'], d['availability']) for d in docs]),
        'documents_by_type': dict(sorted(Counter(d['document_type'] for d in held if d['document_type']).items())),
        'documents_by_category': dict(sorted(Counter(d['category'] for d in held if d['category']).items())),
        'documents_by_classification_basis': dict(sorted(Counter((d['classification'] or {}).get('basis') or 'not_classified' for d in held).items())),
        'document_type_vs_official_label': dict(sorted(Counter(str((d['classification'] or {}).get('agrees_with_official_label'))
                                                               for d in held).items())),
        'documents_by_text_status': dict(sorted(Counter((d['text'] or {}).get('status') or 'none' for d in held).items())),
        'documents_by_study_description': _table([(d['document_type'] or 'none', d['study_description'] or 'none') for d in held]),
        'documents_by_origin': dict(sorted(Counter(d['origin'] for d in held).items())),
        'bytes_retrieved': sum(d['bytes'] or 0 for d in held),
        'pages_retrieved': sum((d['text'] or {}).get('n_pages') or 0 for d in held),
        'pages_with_text': sum((d['text'] or {}).get('n_pages_with_text') or 0 for d in held),
        'pages_garbled': sum((d['text'] or {}).get('n_pages_garbled') or 0 for d in held),
        'submission_document_availability_by_pathway': _table(
            [(s['pathway'], 'has_document' if sn in subs_with_doc else 'no_document') for sn, s in subs.items()]),
        'submission_document_availability_by_year_band': _table(
            [(_year_band(s['decision_date']), 'has_document' if sn in subs_with_doc else 'no_document') for sn, s in subs.items()]),
        'submission_substantive_document_by_pathway': _table(
            [(s['pathway'], 'has_substantive_readable_document' if sn in subs_with_substantive else 'none')
             for sn, s in subs.items()]),
        'other_identifiers_in_documents': dict(sorted(Counter(i['kind'] for d in held for i in d['identifiers']
                                                              if i['kind'] not in ('nct', 'submission_number')).items())),
        'evidence_records_by_method_and_validation': _table([(x['method'], x['automated_validation']) for x in links]),
        'evidence_records_by_level_and_review': _table([(x['level'], x['human_review']) for x in links]),
        'evidence_records_by_confidence': dict(sorted(Counter(x['confidence'] for x in links).items())),
        'timing_by_method': _table([(x['method'], t['relation']) for x in links for t in x['timing']['by_submission']]),
        'registry_states_of_cited_nct_ids': dict(sorted(Counter(s['registry_state'] for s in studies.values()).items())),
        'coverage_by_route_and_state': _table([(c['route'], c['state']) for cov in coverage.values() for c in cov['routes']]),
        'coverage_overall': dict(sorted(Counter(cov['overall']['state'] for cov in coverage.values()).items())),
        'name_search_studies_read': dict(sorted(tallies.items())),
        'number_search': {'n_numbers_with_search_hits': sum(1 for r in number_results.values() if r['n_search_hits']),
                          'n_hit_records': sum(len(r['hits']) for r in number_results.values()),
                          'n_incomplete': sum(1 for r in number_results.values() if r['state'] != 'completed')},
    }
    return {'denominators': den, 'metrics': metrics, 'tables': tables}


def write(files: dict[str, Any], out: Path) -> None:
    """Replace the family in `out` as a whole: staged beside it, then moved into place."""
    stage = out.with_name(out.name + '.staging')
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for name, obj in files.items():
        (stage / name).write_bytes(dump(obj))
    if out.exists():
        shutil.rmtree(out)
    stage.rename(out)
