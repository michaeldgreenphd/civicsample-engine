"""Demographic observations carried forward from the pilot extractions. No extraction runs here.

An observation is what one extraction (one model, one document version) reported for the
demographic fields of one document. It is attached to that document and to the population the
DOCUMENT describes, which is not assumed to be the population of any registered study:

  * a filing's table may describe a subset, a pooled validation set, several studies together,
    or a study with no registry record at all;
  * so every observation carries `population_identity: 'unresolved'` until a person records
    otherwise, `relation_to_registry_population: 'undetermined'`, and
    `may_be_added_to_registry_counts: false`. The contract refuses a build in which any of
    these is otherwise, and no function here or elsewhere adds a filing count to a registry count.

"Not Reported" is kept as absence with the reason the extraction gave, never as zero. A model
output that is malformed (fields missing, or nested one level too deep) is a parsing failure and
is recorded as one, separately from fields that parsed and reported nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

# Sections of the pilot schema that hold participant demographics, and the two that describe
# which population they belong to.
DEMOGRAPHIC_SECTIONS = ('sex', 'gender', 'race_nih_omb', 'ethnicity', 'socioeconomic_status')
POPULATION_FIELDS = ('clinical_study_design', 'total_participants')
NOT_REPORTED = ('Not Reported', '', None)
PARSE_STATES = ('parsed', 'parsed_nested_too_deep', 'malformed')


def _leaves(obj: Any, path: str = '') -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(obj, dict):
        if {'value', 'exact_quote', 'page_number'} <= set(obj):
            yield path, obj
            return
        for k, v in obj.items():
            yield from _leaves(v, f'{path}.{k}' if path else k)


def _reported(value: Any) -> bool:
    return value not in NOT_REPORTED and value != []


def _squash(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip().lower()


def quote_located(quote: str, page_number: Any, pages: list[str] | None) -> str:
    """Whether an extraction's exact_quote stands in the document's text layer: 'on_stated_page',
    'elsewhere_in_document', 'not_found', or 'not_checked' (no quote, or no text held). An
    engineering check that the quote is real; it says nothing about whether the value is right."""
    if not quote or pages is None:
        return 'not_checked'
    q = _squash(quote)
    if isinstance(page_number, int) and 1 <= page_number <= len(pages) and q in _squash(pages[page_number - 1]):
        return 'on_stated_page'
    return 'elsewhere_in_document' if q in _squash(' '.join(pages)) else 'not_found'


def observations(extractions_path: Path, document_of: dict[str, dict[str, Any]],
                 pages_of: dict[str, list[str] | None]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(observations, provenance) from the pilot file.

    document_of: {submission number: the document record the extraction read}. The pilot read the
    file named in `local_pdf_path`, fetched from `source_url`; the document is matched on that URL
    and the build refuses an extraction whose document it does not hold.
    """
    raw = extractions_path.read_bytes()
    out: list[dict[str, Any]] = []
    for entry in json.loads(raw):
        sn = entry['submission_number']
        doc = document_of.get(sn)
        if doc is None or doc['url'].lower() != entry['source_url'].lower():
            raise ValueError(f'{sn}: the document the pilot extraction read is not in the document registry')
        pages = pages_of.get(doc['document_id'])
        for model_key, m in sorted(entry['models'].items()):
            data = m.get('data')
            state = 'parsed'
            if isinstance(data, dict) and set(data) == {'data'}:
                data, state = data['data'], 'parsed_nested_too_deep'
            leaves = dict(_leaves(data)) if isinstance(data, dict) else {}
            missing = [s for s in DEMOGRAPHIC_SECTIONS + POPULATION_FIELDS if not any(
                p == s or p.startswith(s + '.') for p in leaves)]
            if missing:
                state = 'malformed'
            fields = {}
            for path, leaf in sorted(leaves.items()):
                if path.split('.')[0] in DEMOGRAPHIC_SECTIONS:
                    rep = _reported(leaf['value'])
                    fields[path] = {'reported': rep, 'value': leaf['value'] if rep else None,
                                    'absent_reason': None if rep else 'extraction_returned_not_reported',
                                    'exact_quote': leaf['exact_quote'] or None,
                                    'page_number': leaf['page_number'] or None,
                                    'quote_located': quote_located(leaf['exact_quote'], leaf['page_number'], pages)}
            # The population the document describes, in the document's own words where the
            # extraction quoted any; otherwise unresolved, with no description invented.
            described = [{'field': f, 'value': leaves[f]['value'], 'verbatim': leaves[f]['exact_quote'],
                          'page_number': leaves[f]['page_number'] or None,
                          'quote_located': quote_located(leaves[f]['exact_quote'], leaves[f]['page_number'], pages)}
                         for f in POPULATION_FIELDS if f in leaves and _reported(leaves[f]['value'])
                         and leaves[f]['exact_quote']]
            intended = leaves.get('target_patient_age_range')
            out.append({
                'observation_id': f"obs:{sn}:{m.get('model_id')}",
                'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
                'document_type': doc['document_type'],
                'document_is_demographic_summary_candidate': doc['demographic_summary_candidate'],
                'submission_number': sn,
                'extraction': {'method': 'llm', 'model_id': m.get('model_id'), 'provider': m.get('provider'),
                               'call_succeeded': m.get('call_succeeded'), 'parse_state': state,
                               'sections_missing': missing, 'source_file': extractions_path.name},
                'population': {
                    'described_verbatim': described,
                    'population_identity': 'unresolved',
                    'population_identity_reason': (
                        'the extraction quoted no description of a study population' if not described else
                        'a description is quoted, but which registered study (if any) it is has not been determined'),
                    'relation_to_registry_population': 'undetermined',
                    'registered_study': None,
                },
                # The indicated-use population is a statement about who the device is FOR. It is
                # not a study population and is kept apart from the participant fields.
                'intended_use_population': ({'age_range': intended['value'], 'verbatim': intended['exact_quote'],
                                             'page_number': intended['page_number'] or None}
                                            if intended and _reported(intended['value']) else None),
                'demographic_fields': fields,
                'n_demographic_fields': len(fields),
                'n_demographic_fields_reported': sum(1 for f in fields.values() if f['reported']),
                'may_be_added_to_registry_counts': False,
            })
    provenance = {'file': extractions_path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                  'n_documents': len({o['submission_number'] for o in out}), 'n_extractions': len(out)}
    return out, provenance
