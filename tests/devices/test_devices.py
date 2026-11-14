"""Device linkage: inventory, organisations, documents, acquisition, registry evidence, links,
coverage, reviews, demographics, artifacts and the contract.

Fictional records only (tests/devices/fixture.py); no network. These are ENGINEERING tests: they
establish that the rules are applied as written. None of them establishes that a real link is
right.
"""
from __future__ import annotations

import copy
import csv
import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Loaded by path under its own name: tests/outcomes has a fixture.py too, and in one pytest run
# a plain `import fixture` gives whichever directory was imported first to both packages.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location('devices_fixture', Path(__file__).resolve().parent / 'fixture.py')
F = sys.modules.setdefault('devices_fixture', importlib.util.module_from_spec(_spec))
if not hasattr(F, 'ROWS'):
    _spec.loader.exec_module(F)
from devices import (acquire, artifacts, contract, coverage, demographics, documents, inventory, links,  # noqa: E402
                     organizations, pdftext, registry)

sys.path.insert(0, str(ROOT / 'scripts'))
import build_device_links  # noqa: E402


@pytest.fixture(scope='module')
def acquired(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return F.acquire_all(tmp_path_factory.mktemp('devices'))


@pytest.fixture(scope='module')
def family(tmp_path_factory: pytest.TempPathFactory, acquired: dict[str, Any]) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp('family')
    return F.build_family(tmp, acquired, extractions=F.pilot_extractions(tmp / 'pilot.json'))


def rows(*values: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{'row_number': i, 'values': dict(zip(inventory.LIST_COLUMNS, v))} for i, v in enumerate(values, start=1)]


def by_id(family: dict[str, Any], sn: str) -> dict[str, Any]:
    return next(s for s in family['submissions.json']['submissions'] if s['submission_number'] == sn)


def cell(family: dict[str, Any], sn: str, route: str) -> dict[str, Any]:
    cov = next(c for c in family['coverage.json']['submissions'] if c['submission_number'] == sn)
    return next(c for c in cov['routes'] if c['route'] == route)


# ---------------------------------------------------------------- inventory

def test_rows_fold_on_the_submission_number_and_every_row_is_kept(family: dict[str, Any]) -> None:
    rec = family['submissions.json']['reconciliation']
    assert (rec['n_rows'], rec['n_submissions'], rec['n_extra_rows'], rec['n_duplicated_numbers']) == (11, 8, 3, 3)
    assert rec['duplicated_numbers_by_class'] == {'identical': ['K990001'], 'alias_only': ['K990003'],
                                                  'substantive_conflict': ['K990004']}
    assert [r['row_number'] for r in by_id(family, 'K990001')['source_rows']] == [1, 2]


@pytest.mark.parametrize('second, expected, fields', [
    (('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl Co', 'Radiology', 'LLZ'), 'identical', []),
    (('1/5/22', 'K990003', 'Auto Segmentation', 'PEARL, CO.', 'Radiology', 'LLZ'), 'alias_only', ['Company']),
    (('1/5/22', 'K990003', 'Auto-Segmentation', 'Pearl Co', 'Radiology', 'LLZ'), 'alias_only', ['Device']),
    (('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl Therapeutics', 'Radiology', 'LLZ'), 'substantive_conflict', ['Company']),
    (('1/6/22', 'K990003', 'Auto Segmentation', 'Pearl Co', 'Radiology', 'LLZ'), 'substantive_conflict',
     ['Date of Final Decision']),
    (('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl Co', 'Radiology', 'QIH'), 'substantive_conflict',
     ['Primary Product Code']),
])
def test_duplicate_rows_are_classified_by_what_differs(second: tuple[str, ...], expected: str, fields: list[str]) -> None:
    first = ('1/5/22', 'K990003', 'Auto Segmentation', 'Pearl Co', 'Radiology', 'LLZ')
    cls, conflicts = inventory.classify_duplicate(rows(first, second), inventory.LIST_COLUMNS)
    assert cls == expected and sorted(conflicts) == sorted(fields)
    if fields:
        assert conflicts[fields[0]] == [first[inventory.LIST_COLUMNS.index(fields[0])],
                                        second[inventory.LIST_COLUMNS.index(fields[0])]]


def test_conflicting_rows_keep_every_value_and_a_disputed_date_is_absent_not_chosen() -> None:
    subs = inventory.submissions(rows(('1/5/22', 'K990003', 'A-Scan', 'Pearl Co', 'Radiology', 'LLZ'),
                                      ('2/5/22', 'K990003', 'A-Scan', 'Pearl Co', 'Radiology', 'LLZ')),
                                 inventory.LIST_COLUMNS)
    s = subs['K990003']
    assert s['decision_date'] is None and s['decision_date_absent_reason'] == 'rows_disagree'
    assert s['decision_date_as_listed'] == ['1/5/22', '2/5/22'] and s['n_source_rows'] == 2


def test_a_row_without_a_recognisable_number_stops_the_build() -> None:
    with pytest.raises(ValueError, match='not a recognised FDA submission number'):
        inventory.submissions(rows(('1/5/22', 'X123', 'A', 'B', 'C', 'D')), inventory.LIST_COLUMNS)


@pytest.mark.parametrize('number, expected', [('K203042', '510k'), ('DEN170073', 'de_novo'), ('P200003', 'pma'),
                                              ('P140011/S008', 'pma_supplement')])
def test_pathway_is_read_from_the_prefix(number: str, expected: str) -> None:
    assert inventory.pathway(number) == expected


@pytest.mark.parametrize('cell_, expected', [('12/30/25', '2025-12-30'), ('9/29/95', '1995-09-29'), ('2/30/21', None),
                                             ('soon', None)])
def test_decision_dates_parse_or_are_none(cell_: str, expected: str | None) -> None:
    d = inventory.parse_decision_date(cell_)
    assert (d.isoformat() if d else None) == expected


@pytest.mark.parametrize('name, core', [('ART-Plan+ (v3.1.0)', 'ART-Plan+'), ('IB NEURO, VERSION 1.0', 'IB NEURO'),
                                        ('VersaViewer', 'VersaViewer'), ('qER 2.0', 'qER')])
def test_core_name_drops_parentheses_and_a_trailing_version_only(name: str, core: str) -> None:
    assert inventory.name_forms(name)['core'] == core


def test_a_comma_inside_parentheses_does_not_split_models() -> None:
    forms = inventory.name_forms('Aplio i900 Diagnostic Ultrasound System, Software V7.0 (TUS-AI900, TUS-AI800)')
    assert forms['segments'] == ['Aplio i900 Diagnostic Ultrasound System', 'Software']


def test_name_forms_separate_trimmed_from_manufacturer_only_and_flag_generic_names() -> None:
    assert inventory.name_forms('SKOUT system')['trimmed'] == ['skout']
    assert inventory.name_forms('Athelas One', 'athelas')['company_only'] == ['athelas']
    assert inventory.name_forms('Deep Learning Image Reconstruction')['generic'] is True


def _families(*items: tuple[str, str, str], curated: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, Any]]:
    subs = inventory.submissions(rows(*[('1/5/22', sn, name, company, 'Radiology', 'LLZ') for sn, name, company in items]),
                                 inventory.LIST_COLUMNS)
    org_of = {sn: organizations.organization_id(organizations.normalise(s['companies_as_listed'][0])['normalised'])
              for sn, s in subs.items()}
    return inventory.families(subs, org_of, curated or {})


def test_family_grouping_needs_the_same_organisation_and_the_same_whole_core_name() -> None:
    fams = _families(('K990001', 'Zorbex Scan (v2.1)', 'Fictional Imaging, Inc.'),
                     ('K990002', 'Zorbex Scan', 'Fictional Imaging Inc'),
                     ('K990011', 'Zorbex Scan', 'Another Maker LLC'),          # same name, other organisation
                     ('K990012', 'Zorbex Triage', 'Fictional Imaging Inc'),     # shares a word only
                     ('K990013', 'Rapid ICH', 'Fictional Imaging Inc'),
                     ('K990014', 'Rapid LVO', 'Fictional Imaging Inc'))
    groups = sorted(sorted(f['submission_numbers']) for f in fams.values())
    assert groups == [['K990001', 'K990002'], ['K990011'], ['K990012'], ['K990013'], ['K990014']]
    assert all(f['status'] == 'candidate' for f in fams.values())


def test_a_name_made_only_of_device_type_words_never_groups_even_within_one_organisation() -> None:
    fams = _families(('K990021', 'Ultrasound System', 'Fictional Imaging Inc'),
                     ('K990022', 'Ultrasound System', 'Fictional Imaging Inc'))
    assert len(fams) == 2
    assert {f['grouping_reason'] for f in fams.values()} == {'generic_name_not_grouped'}


def test_only_the_curated_file_makes_a_curated_family_or_joins_differently_named_products() -> None:
    curated = {sn: {'family_id': 'zorbex', 'submission_number': sn, 'family_name': 'Zorbex', 'source': 'labeling',
                    'note': 'same product line per the manufacturer'} for sn in ('K990001', 'K990012')}
    fams = _families(('K990001', 'Zorbex Scan', 'Fictional Imaging Inc'), ('K990012', 'Zorbex Triage', 'Fictional Imaging Inc'),
                     ('K990002', 'Zorbex Scan', 'Fictional Imaging Inc'), curated=curated)
    assert fams['family:curated:zorbex']['submission_numbers'] == ['K990001', 'K990012']
    assert fams['family:curated:zorbex']['status'] == 'curated'
    assert sum(f['status'] == 'candidate' for f in fams.values()) == 1


def test_curated_families_file_is_validated(tmp_path: Path) -> None:
    F.write_curated(tmp_path, families=[['f1', 'K990001', 'X', 'src', 'why'], ['f2', 'K990001', 'X', 'src', 'why']])
    with pytest.raises(ValueError, match='assigned to two families'):
        inventory.read_curated_families(tmp_path / 'families.csv')
    with pytest.raises(ValueError, match='not in the inventory'):
        _families(('K990001', 'A-Scan', 'B Co'), curated={'K999999': {'family_id': 'f', 'submission_number': 'K999999',
                                                                    'family_name': '', 'source': '', 'note': 'n'}})


# ---------------------------------------------------------------- organisations

@pytest.mark.parametrize('a, b, same', [
    ('Pearl Inc.', 'Pearl, Inc.', True), ('RaySearch Laboratories AB (PUBL)', 'RaySearch Laboratories AB (publ)', True),
    ('Peek Health, S.A.', 'PEEK HEALTH SA', True), ('Vitrolife A/S', 'Vitrolife', True),
    ('MRIguidance B.V', 'MRIguidance BV', True), ('Q Bio, Inc.', 'Bio Inc', False), ('X-Nav Technologies, LLC', 'Nav Technologies', False),
    ('Nobel Biocare C/O Medicim NV', 'Nobel Biocare', True), ('Siemens Medical Solutions USA, Inc.', 'Siemens Healthcare GmbH', False),
    ('Pearl Inc.', 'Pearl Therapeutics', False), ('Viz.Al, Inc.', 'Viz.ai, Inc.', False), ('Exo Inc', 'EXO Biologics S.A.', False),
])
def test_normalisation_joins_spellings_and_nothing_else(a: str, b: str, same: bool) -> None:
    assert (organizations.normalise(a)['normalised'] == organizations.normalise(b)['normalised']) is same


def test_raw_name_is_preserved_and_what_was_removed_is_recorded() -> None:
    n = organizations.normalise('Acme Imaging GmbH (Siemens) d/b/a Acme')
    assert n['raw'] == 'Acme Imaging GmbH (Siemens) d/b/a Acme' and n['normalised'] == 'acme imaging'
    assert n['legal_form_tokens'] == ['gmbh'] and n['parenthesised'] == ['Siemens'] and n['dba'] == 'Acme'
    assert organizations.normalise('Nobel Biocare C/O Medicim NV')['care_of'] == 'Medicim NV'


def test_two_differently_named_organisations_merge_only_through_the_alias_file(tmp_path: Path) -> None:
    names = ['Bay Labs, Inc.', 'Caption Health, Inc.', 'Caption Health Inc']
    apart = organizations.build(names, {})
    assert len(apart) == 2 and apart['org:caption-health']['merge_basis'] == 'normalisation_rules'
    F.write_curated(tmp_path, aliases=[['bay labs', 'caption health', 'Caption Health', '', '2020-01-01', 'press release',
                                        'renamed in 2020']])
    aliases = organizations.read_aliases(tmp_path / 'organization_aliases.csv')
    joined = organizations.build(names, aliases)
    assert list(joined) == ['org:caption-health']
    org = joined['org:caption-health']
    assert org['merge_basis'] == 'curated_alias' and org['raw_names'] == sorted(names)
    assert org['aliases_applied'][0]['note'] == 'renamed in 2020'
    assert organizations.match_tier('Bay Labs, Inc.', 'Caption Health', aliases) == 'curated_alias'
    assert organizations.match_tier('Bay Labs, Inc.', 'Caption Health', {}) is None


def test_agreement_only_after_different_legal_forms_is_its_own_weaker_tier() -> None:
    assert organizations.match_tier('Metamorphosis GmbH', 'Metamorphosis LTD', {}) == 'normalised_legal_form_differs'
    assert organizations.match_tier('HeartFlow, Inc.', 'HeartFlow', {}) == 'normalised_exact'


@pytest.mark.parametrize('row, message', [
    (['bay labs', 'caption health', '', '', '', 'src', ''], 'required'),
    (['Bay Labs, Inc.', 'caption health', '', '', '', 'src', 'n'], 'already be normalised'),
    (['bay labs', 'bay labs', '', '', '', 'src', 'n'], 'point at itself'),
])
def test_alias_file_rows_are_refused_when_they_would_resolve_silently(tmp_path: Path, row: list[str], message: str) -> None:
    F.write_curated(tmp_path, aliases=[row])
    with pytest.raises(ValueError, match=message):
        organizations.read_aliases(tmp_path / 'organization_aliases.csv')


def test_alias_chains_and_repeats_are_refused(tmp_path: Path) -> None:
    F.write_curated(tmp_path, aliases=[['a labs', 'b labs', '', '', '', 's', 'n'], ['b labs', 'c labs', '', '', '', 's', 'n']])
    with pytest.raises(ValueError, match='chains'):
        organizations.read_aliases(tmp_path / 'organization_aliases.csv')
    F.write_curated(tmp_path, aliases=[['a labs', 'b labs', '', '', '', 's', 'n'], ['a labs', 'c labs', '', '', '', 's', 'n']])
    with pytest.raises(ValueError, match='listed twice'):
        organizations.read_aliases(tmp_path / 'organization_aliases.csv')


# ---------------------------------------------------------------- documents

LETTER = 'Re: K990001\nDear Sir:\nWe have reviewed your Section 510(k) premarket notification of intent to market.'


@pytest.mark.parametrize('pathway, pages, label, expected, candidate', [
    ('de_novo', F.TEXTS['pdf99/DEN990005.pdf'], 'Classification Order', 'de_novo_granting_letter', False),
    ('de_novo', F.TEXTS['reviews/DEN990005.pdf'], 'Decision Summary', 'de_novo_decision_summary', True),
    ('de_novo', ['DE NOVO CLASSIFICATION REQUEST FOR QUANTA TRIAGE\nREGULATORY INFORMATION'], None,
     'de_novo_decision_summary', True),
    ('510k', F.TEXTS['pdf99/K990001.pdf'], 'Summary', '510k_summary', True),
    ('510k', [LETTER, 'K990001\n5 1 0 ( k )   S U M M A R Y'.replace('5 1 0 ( k )   S U M M A R Y', '510(k)\nSummary')], None,
     '510k_summary', True),
    ('510k', F.TEXTS['pdf99/K990003.pdf'], 'Statement', '510k_statement', False),
    ('510k', [LETTER, 'Indications for Use\nFor review of images.'], 'Summary', '510k_clearance_letter', False),
    ('510k', ['510(k) SUBSTANTIAL EQUIVALENCE DETERMINATION DECISION SUMMARY\nA. 510(k) Number: K990001'], 'Decision Summary',
     '510k_decision_summary', True),
    ('pma', F.TEXTS['pdf99/P990006A.pdf'], 'Approval Order', 'pma_approval_order', False),
    ('pma', F.TEXTS['pdf99/P990007B.pdf'], 'Summary of Safety and Effectiveness', 'pma_ssed', True),
    ('pma', F.TEXTS['pdf99/P990006C.pdf'], 'Labeling', 'pma_labeling', False),
    ('510k', ['Minutes of a meeting about nothing in particular, with enough words.'], None, 'other', False),
])
def test_document_type_comes_from_the_text_with_the_words_that_decided_it(
        pathway: str, pages: list[str], label: str | None, expected: str, candidate: bool) -> None:
    c = documents.classify(pages, pathway, label)
    assert c['document_type'] == expected and c['basis'] == 'text'
    if expected != 'other':
        assert c['matched_phrase'] and c['page'] >= 1 and c['rule']
    assert documents.demographic_summary_candidate(expected, 'text', 'this_submission')[0] is candidate


def test_a_letter_filed_under_another_label_is_reported_as_disagreeing_with_fda() -> None:
    # FDA posts the clearance letter alone under the "Statement" link; the text decides.
    c = documents.classify([LETTER, 'Indications for Use\nFor review of images by trained readers.'], '510k', 'Statement')
    assert c['document_type'] == '510k_clearance_letter' and c['agrees_with_official_label'] is False
    assert c['basis'] == 'text'


@pytest.mark.parametrize('page, phrase', [
    ('K 0X773\n1 5 10(k) Summary\nSubmitter: Fictional AB', '5 10(k) Summar'),        # OCR spacing of an old scan
    ('K990001\n510(k) Summar\nFictional Health, Inc. 510(k) Owner Name', '510(k) Summar'),   # heading lost its last letter
    ('This summary is submitted in accordance with 21 CFR 807.92.', '21 CFR 807.92'),   # heading is an image
    ('8. Summary of Technological Characteristics Comparison\nTable 1 shows', 'Summary of Technological Characteristics'),
])
def test_a_510k_summary_is_recognised_without_a_clean_heading(page: str, phrase: str) -> None:
    c = documents.classify([LETTER, page], '510k', 'Summary')
    assert c['document_type'] == '510k_summary' and c['matched_phrase'] == phrase and c['page'] == 2


def test_a_summary_on_pages_without_text_is_typed_by_fda_label_and_not_treated_as_read() -> None:
    c = documents.classify([LETTER, 'Indications for Use form with enough words on it.', '', '', ''], '510k', 'Summary')
    assert (c['document_type'], c['basis'], c['text_read_is'], c['n_pages_without_text']) == \
        ('510k_summary', 'official_label_unread_pages', '510k_clearance_letter', 3)
    ok, why = documents.demographic_summary_candidate('510k_summary', 'partial', 'this_submission', c['basis'])
    assert ok is False and 'OCR' in why
    # With every page read and no summary found, the letter is a letter whatever the label says.
    assert documents.classify([LETTER], '510k', 'Summary')['document_type'] == '510k_clearance_letter'


def test_text_that_matches_no_rule_takes_fda_label_and_says_so() -> None:
    c = documents.classify(['Fictional AED\nQuickstart Guide\nIFU-00132 Rev A, with enough words.'], 'pma', 'Labeling Part 2')
    assert (c['document_type'], c['basis'], c['matched_phrase']) == ('pma_labeling', 'official_label_text_unmatched', None)
    memo = documents.classify(['SPECIAL 510(k): Device Modification\nOIR Review Memorandum\nTo: THE FILE'], '510k',
                              'Decision Summary')
    assert memo['document_type'] == '510k_decision_summary' and memo['basis'] == 'text'


def test_a_summary_body_without_any_heading_is_recognised_by_its_section_markers() -> None:
    body = ('K990001 Applicant Name: Fictional, Inc\nPredicate Device: Fictional Aid (K980111)\n'
            'Device Description\nThe software reads radiographs.')
    c = documents.classify([LETTER, body], '510k', 'Summary')
    assert c['document_type'] == '510k_summary' and c['basis'] == 'text' and c['page'] == 2
    assert c['matched_phrase'] == 'Applicant Name | Predicate Device | Device Description'
    # The clearance letter's own wording ("predicate devices", "substantially equivalent") is not enough.
    letter = LETTER + ' The device is substantially equivalent to legally marketed predicate devices.'
    assert documents.classify([letter], '510k', 'Summary')['document_type'] == '510k_clearance_letter'


def test_a_garbled_text_layer_counts_as_no_text() -> None:
    readable = 'The device is intended for use by trained readers in the review of images. ' * 4
    shifted = ''.join(chr(ord(c) - 29) if c.isalpha() else '\x03' for c in readable)     # fonts without a character map
    symbols = 'Ã+-@#DK9 ;KÃ1 #8ÃK1 Ã#;+ N9#;ÃG DQ3 GÃ bÃS bÃ wÃ ' + ' '.join(['qzx', 'vkj', 'wpf', 'ghb'] * 12)
    table = ' '.join(f'{n} {n * 3} {n * 7}' for n in range(100, 160))
    assert documents.garbled(shifted) and documents.garbled(symbols)
    assert not documents.garbled(readable) and not documents.garbled(table) and not documents.garbled('Short page.')


def test_a_pdf_portfolio_cover_is_a_parsing_failure_not_a_document_that_says_nothing() -> None:
    cover = ['For the best experience, open this PDF portfolio in\nAcrobat X or Adobe Reader X, or later.']
    c = documents.classify(cover, '510k', 'Summary')
    assert (c['document_type'], c['basis']) == ('510k_summary', 'official_label_only')


def test_without_text_the_label_gives_the_type_and_says_the_content_was_not_read() -> None:
    c = documents.classify([], 'pma', 'Summary of Safety and Effectiveness')
    assert (c['document_type'], c['basis'], c['matched_phrase']) == ('pma_ssed', 'official_label_only', None)
    ok, why = documents.demographic_summary_candidate('pma_ssed', 'image_only', 'this_submission')
    assert ok is False and 'OCR' in why
    assert documents.classify(['', ' '], '510k', None)['document_type'] == 'unclassified_no_text'


def test_a_granting_letter_is_never_a_demographic_summary_and_a_parent_document_is_not_the_supplements() -> None:
    for t in ('de_novo_granting_letter', '510k_clearance_letter', 'pma_approval_order', '510k_statement', 'pma_labeling'):
        ok, why = documents.demographic_summary_candidate(t, 'text', 'this_submission')
        assert ok is False and why
    assert documents.demographic_summary_candidate('pma_ssed', 'text', 'parent_submission')[0] is False


@pytest.mark.parametrize('text, expected', [
    ('registered as NCT01234567.', ['NCT01234567']), ('(ClinicalTrials.gov NCT 01234567)', ['NCT01234567']),
    ('NCT#0123\n4567 and nct-07654321', ['NCT01234567', 'NCT07654321']), ('N C T 0 1 2 3 4 5 6 7', ['NCT01234567']),
    ('NCT012345678', []), ('NCT0123456', []), ('distinct 12345678', []), ('XNCT01234567', []),
    # a longer token that begins with an NCT id is not that id; a space or punctuation still ends it
    ('NCT01234567ABC', []), ('NCT01234567x', []), ('NCT01234567 and', ['NCT01234567']), ('(NCT01234567)', ['NCT01234567']),
])
def test_nct_pattern(text: str, expected: list[str]) -> None:
    found = documents.scan_identifiers([text])
    assert [i['identifier'] for i in found if i['kind'] == 'nct'] == expected


def test_identifier_scan_gives_page_and_sentence_and_keeps_other_identifiers_apart() -> None:
    pages = ['Cover page.', 'Background. The pivotal study (NCT01234567) enrolled 300 patients under IDE G190123. '
                            'See ISRCTN12345678 and predicate K123456. Own number K990001.']
    found = {i['kind']: i for i in documents.scan_identifiers(pages, 'K990001')}
    assert found['nct']['page'] == 2
    assert found['nct']['sentence'] == 'The pivotal study (NCT01234567) enrolled 300 patients under IDE G190123.'
    assert found['ide']['identifier'] == 'G190123' and found['isrctn']['identifier'] == 'ISRCTN12345678'
    assert found['submission_number']['identifier'] == 'K123456'      # the document's own number is not listed


def test_study_language_separates_a_described_study_from_a_statement_that_none_was_done() -> None:
    lang = documents.study_language(F.TEXTS['reviews/DEN990005.pdf'])
    assert lang['human_subject_study']['n_sentences'] == 1 and lang['states_no_clinical_study']['n_sentences'] == 0
    assert lang['human_subject_study']['examples'][0]['page'] == 2
    assert lang['demographic_terms']['sex_or_gender']['n_mentions'] == 1
    none = documents.study_language(['510(k) Summary. No clinical studies were required to support equivalence.'])
    assert none['states_no_clinical_study']['n_sentences'] == 1 and none['human_subject_study']['n_sentences'] == 0
    assert documents.study_description_state('text', [], lang) == 'describes_study_no_registry_identifier'
    assert documents.study_description_state('text', [], none) == 'states_no_clinical_study_only'
    assert documents.study_description_state('image_only', [], None) == 'not_assessable_no_text'
    assert documents.study_description_state('text', [{'kind': 'nct'}], lang) == 'cites_registry_identifier'


def test_document_registry_records_hash_status_type_and_failures(family: dict[str, Any]) -> None:
    docs = {d['url'].rsplit('cdrh_docs/', 1)[1]: d for d in family['documents.json']['documents']}
    held = docs['reviews/DEN990005.pdf']
    assert held['availability'] == 'retrieved' and len(held['sha256']) == 64 and held['http_status'] == 200
    assert held['text']['extraction_version'] == pdftext.extraction_version() and held['text']['n_pages'] == 2
    assert held['official_label'] == 'Decision Summary' and held['classification']['agrees_with_official_label'] is True
    assert held['document_id'] == acquire.document_id('DEN990005', held['url'])
    scan = docs['pdf99/P990006B.pdf']
    assert scan['text']['status'] == 'image_only' and scan['classification']['basis'] == 'official_label_only'
    assert scan['demographic_summary_candidate'] is False and scan['study_description'] == 'not_assessable_no_text'
    missing = docs['reviews/K990004.pdf']
    assert (missing['availability'], missing['http_status'], missing['sha256'], missing['document_type']) == \
        ('not_found', 404, None, None)
    parent = docs['pdf99/P990007B.pdf']
    assert parent['scope'] == 'parent_submission' and parent['demographic_summary_candidate'] is False


def test_page_text_status() -> None:
    words = 'twenty letters or digits at least here'
    assert pdftext.status_of([words, words]) == 'text'
    assert pdftext.status_of([words, '']) == 'partial'
    assert pdftext.status_of(['', ' \n']) == 'image_only' and pdftext.status_of([]) == 'image_only'


# ---------------------------------------------------------------- acquisition

def test_throttle_spaces_requests_per_host_only() -> None:
    clock, slept = [0.0], []

    def sleep(s: float) -> None:
        slept.append(round(s, 2))
        clock[0] += s
    t = acquire.Throttle(0.55, sleep=sleep, clock=lambda: clock[0])
    for url in ('https://a.example/x', 'https://a.example/y', 'https://b.example/z', 'https://a.example/w'):
        t.wait(url)
    assert slept == [0.55, 0.55]          # a second and third request to host a; none for host b
    assert acquire.MIN_INTERVAL_SECONDS >= 0.5   # at most two requests a second


def test_retries_on_server_errors_and_never_on_a_404() -> None:
    answers = iter([(503, {}, b'', None), (None, {}, b'', 'ConnectionError'), (200, {}, b'ok', None)])
    waits: list[float] = []
    throttle = acquire.Throttle(0.0, sleep=waits.append)
    resp, attempts = acquire.polite_get(lambda u, p: next(answers), throttle, 'https://a.example/x')
    assert (resp[0], attempts, waits) == (200, 3, [2.0, 4.0])
    calls: list[str] = []

    def not_found(url: str, params: Any) -> acquire.Response:
        calls.append(url)
        return 404, {}, b'', None
    assert acquire.polite_get(not_found, throttle, 'https://a.example/x')[1] == 1 and len(calls) == 1
    always_down = acquire.polite_get(lambda u, p: (None, {}, b'', 'ConnectionError'), throttle, 'https://a.example/x')
    assert always_down[1] == acquire.MAX_ATTEMPTS and always_down[0][0] is None


def test_acquisition_is_resumable_and_never_downloads_a_recorded_hash_again(tmp_path: Path) -> None:
    calls: list[str] = []
    first = F.acquire_all(tmp_path, F.stub_get(calls))
    n_first, n_lines = len(calls), len(first['manifest'].lines)
    assert n_first > 0
    calls.clear()
    F.acquire_all(tmp_path, F.stub_get(calls))
    # Only what never got an answer is asked again: the database page whose request failed.
    assert all('K990008' in c for c in calls) and len(calls) < n_first
    assert len(acquire.Manifest(tmp_path / 'cache' / 'documents' / 'manifest.jsonl').lines) == n_lines


def test_a_missing_document_is_a_recorded_finding_and_a_failed_request_is_retried_later(tmp_path: Path) -> None:
    manifest = acquire.Manifest(tmp_path / 'manifest.jsonl')
    plan = [{'document_id': acquire.document_id('K990001', f'{F.DOCS}/{name}'), 'submission_number': 'K990001',
             'url': f'{F.DOCS}/{name}', 'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}
            for name in ('pdf99/K990001.pdf', 'pdf99/nothing.pdf')]
    counts = acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(fail={'K990001.pdf'}), F.no_wait())
    assert (counts['network_error'], counts['not_found']) == (1, 1)
    again = acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(), F.no_wait())
    assert (again['retrieved'], again['already_settled']) == (1, 1)
    line = manifest.latest()[plan[0]['document_id']]
    assert (tmp_path / 'store' / f"{line['sha256']}.pdf").read_bytes().startswith(b'%PDF-')
    assert line['last_modified'] and line['http_status'] == 200
    assert len(manifest.lines) == 3          # the failure stays in the history


def test_an_html_error_page_served_with_status_200_is_not_stored_as_a_document(tmp_path: Path) -> None:
    manifest = acquire.Manifest(tmp_path / 'manifest.jsonl')
    plan = [{'document_id': 'doc:x', 'submission_number': 'K990001', 'url': f'{F.DOCS}/pdf99/K990001.pdf',
             'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}]
    counts = acquire.fetch_documents(plan, manifest, tmp_path / 'store', lambda u, p: (200, {}, b'<html>', None), F.no_wait())
    assert counts['not_a_pdf'] == 1 and manifest.lines[0]['sha256'] is None and not (tmp_path / 'store').exists()
    # An HTML page in place of the file is no answer about the file: not settled, asked again.
    assert not acquire.settled(manifest.lines[0])
    calls: list[str] = []
    again = acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(calls), F.no_wait())
    assert again['retrieved'] == 1 and len(calls) == 1


def test_a_route_holding_a_non_pdf_answer_is_incomplete() -> None:
    doc = {'document_id': 'doc:x', 'origin': 'fda_fetch', 'scope': 'this_submission', 'availability': 'not_a_pdf',
           'url': f'{F.DOCS}/pdf99/K990001.pdf', 'http_status': 200, 'document_type': None, 'text': None}
    page = _page([{'url': doc['url'], 'official_label': 'Summary'}])
    cell = coverage.fda_document_fetch(page, [doc], [])
    assert cell['state'] == 'search_incomplete_or_blocked'
    assert cell['detail']['not_retrieved'] == [{'url': doc['url'], 'outcome': 'not_a_pdf', 'http_status': 200}]


def test_local_documents_are_recovered_once_and_then_not_fetched(tmp_path: Path) -> None:
    local = tmp_path / 'local'
    local.mkdir()
    (local / '2026-04-16_K990001.pdf').write_bytes(b'%PDF-1.4 local copy')
    (local / 'notes.pdf').write_bytes(b'%PDF-1.4 unrelated')
    url = f'{F.DOCS}/pdf99/K990001.pdf'
    manifest = acquire.Manifest(tmp_path / 'manifest.jsonl')
    assert acquire.recover_local(local, {'K990001': url}, manifest, tmp_path / 'store') == 1
    assert acquire.recover_local(local, {'K990001': url}, manifest, tmp_path / 'store') == 0
    line = manifest.lines[0]
    assert (line['origin'], line['retrieved_at'], line['http_status']) == ('local_recovered', '2026-04-16', None)
    assert line['http_status_absent_reason']
    calls: list[str] = []
    plan = acquire.document_plan('K990001', _page([{'url': url, 'official_label': 'Summary'}]))
    assert acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(calls), F.no_wait())['already_settled'] == 1
    assert calls == []



def test_a_local_file_that_is_not_a_pdf_is_not_stored_and_does_not_settle_the_address(tmp_path: Path) -> None:
    local = tmp_path / 'local'
    local.mkdir()
    (local / '2026-04-16_K990001.pdf').write_bytes(b'<html>Service unavailable</html>')
    url = f'{F.DOCS}/pdf99/K990001.pdf'
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    assert acquire.recover_local(local, {'K990001': url}, manifest, store) == 1
    line = manifest.lines[0]
    assert (line['outcome'], line['bytes'], line['sha256']) == ('not_a_pdf', None, None)
    assert not store.exists() or not any(store.iterdir())     # no junk in the store, nothing against its budget
    assert not acquire.settled(line)
    assert acquire.recover_local(local, {'K990001': url}, manifest, store) == 0     # registered once
    calls: list[str] = []
    plan = acquire.document_plan('K990001', _page([{'url': url, 'official_label': 'Summary'}]))
    assert acquire.fetch_documents(plan, manifest, store, F.stub_get(calls), F.no_wait())['retrieved'] == 1
    assert len(calls) == 1 and manifest.latest()[line['document_id']]['origin'] == 'fda_fetch'

@pytest.mark.parametrize('number, directory', [('K955841', 'pdf'), ('K003301', 'pdf'), ('K033840', 'pdf3'),
                                               ('K092967', 'pdf9'), ('K203042', 'pdf20'), ('DEN170073', 'pdf17'),
                                               ('P140011/S008', 'pdf14')])
def test_document_directory_by_receipt_year(number: str, directory: str) -> None:
    assert acquire.cdrh_docs_dir(number) == directory


def test_document_plan_prefers_fda_page_links_and_marks_parent_documents() -> None:
    page = _page([{'url': f'{F.DOCS}/pdf12/K124067.pdf', 'official_label': 'Classification Order'},
                                        {'url': f'{F.DOCS}/reviews/K124067.pdf', 'official_label': 'Decision Summary'}])
    plan = acquire.document_plan('DEN130013', page)
    assert [p['discovery'] for p in plan] == ['fda_database_page'] * 2 + ['url_pattern'] * 2
    assert plan[1]['url'].endswith('reviews/K124067.pdf')      # an early De Novo filed under a K number
    assert [p['url'].rsplit('/', 2)[-2:] for p in acquire.document_plan('K203042', None)] == \
        [['pdf20', 'K203042.pdf'], ['reviews', 'K203042.pdf']]
    supp = acquire.document_plan('P990007/S002', _page([]),
                                 _page([{'url': f'{F.DOCS}/pdf99/P990007B.pdf', 'official_label': 'SSED'}]))
    assert supp[-1]['scope'] == 'parent_submission' and all(p['scope'] == 'this_submission' for p in supp[:-1])


def test_database_page_links_are_parsed_with_fda_labels() -> None:
    html = ('<A HREF="https://www.accessdata.fda.gov/cdrh_docs/pdf9/K092967.pdf" TITLE="x"  style="y">Summary</A>'
            '<a href="http://www.accessdata.fda.gov/cdrh_docs/reviews/K092967.pdf" title="z">Decision Summary</a>'
            '<a href="/scripts/cdrh/cfdocs/cfpmn/pmn.cfm">New Search</a>')
    assert acquire.parse_document_links(html) == [
        {'url': f'{F.DOCS}/pdf9/K092967.pdf', 'official_label': 'Summary'},
        {'url': f'{F.DOCS}/reviews/K092967.pdf', 'official_label': 'Decision Summary'}]


def test_a_non_final_answer_is_logged_and_asked_again(tmp_path: Path) -> None:
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    answers = iter([(400, {}, b'', None), (200, {}, _record_page('510(k) Number', 'K990001').encode(), None)])
    get = lambda u, p: next(answers)  # noqa: E731
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    assert log.get('fda_page/K990001')['status'] == 400 and log.settled('fda_page/K990001') is None
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    assert acquire.ResponseLog(tmp_path / 'log.jsonl').settled('fda_page/K990001')['status'] == 200



def test_a_database_page_that_does_not_name_the_number_is_not_an_answer(tmp_path: Path) -> None:
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    other = (b'<html>K990002 <a href="https://www.accessdata.fda.gov/cdrh_docs/pdf99/K990002.pdf">'
             b'Summary</a></html>')
    answers = iter([(200, {}, other, None), (200, {}, _record_page('510(k) Number', 'K990001').encode(), None)])
    get = lambda u, p: next(answers)
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    rec = log.get('fda_page/K990001')
    assert (rec['status'], rec['http_status'], rec['body']['document_links']) == ('not_the_record', 200, [])
    assert log.settled('fda_page/K990001') is None
    # Guessed addresses that 404 do not turn into "FDA holds no document" on the strength of it.
    cell = coverage.fda_document_fetch(rec, [], [])
    assert cell['state'] == 'search_incomplete_or_blocked' and 'does not name it' in cell['reason']
    assert coverage.fda_document_fetch({**rec, 'status': 200, 'page_names_the_submission': True}, [], [])['state'] == \
        'document_unavailable'
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    assert acquire.ResponseLog(tmp_path / 'log.jsonl').settled('fda_page/K990001')['status'] == 200

@pytest.mark.parametrize('marker', [None, False])
def test_a_cached_page_answer_not_confirmed_to_name_the_number_is_not_trusted(tmp_path: Path, marker: bool | None) -> None:
    """A 200 logged before the page-identity check existed (no marker), or logged as not naming
    the number, is asked again, and until then its links are not read and it is not FDA's list."""
    url = f'{F.DOCS}/pdf99/K990002.pdf'
    old = {'key': 'fda_page/K990001', 'url': 'u', 'params': {}, 'status': 200, 'http_status': 200,
           'body': {'document_links': [{'url': url, 'official_label': 'Summary'}]}}
    if marker is not None:
        old['page_names_the_submission'] = marker
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    log.put(old)
    assert acquire.document_plan('K990001', old)[0]['discovery'] == 'url_pattern'
    cell = coverage.fda_document_fetch(old, [], [])
    assert cell['state'] == 'search_incomplete_or_blocked' and 'not confirmed to name it' in cell['reason']
    calls: list[str] = []

    def get(u: str, p: Any) -> tuple[int, dict[str, Any], bytes, None]:
        calls.append(u)
        return 200, {}, _record_page('510(k) Number', 'K990001').encode(), None
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    rec = acquire.ResponseLog(tmp_path / 'log.jsonl').get('fda_page/K990001')
    assert len(calls) == 1 and rec['page_names_the_submission'] is True and acquire.database_page_answered(rec)
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    assert len(calls) == 1      # a confirmed page, and a 404, settle the number
    log.put({**rec, 'status': 404, 'http_status': 404, 'page_names_the_submission': False})
    acquire.fetch_database_pages(['K990001'], log, tmp_path / 'html', get, F.no_wait())
    assert len(calls) == 1


def test_openfda_records_map_to_submissions_and_absence_is_stated(acquired: dict[str, Any]) -> None:
    found = acquire.openfda_by_submission(acquired['frozen']['openfda'][0], list(acquired['subs']))
    meta = acquire.official_metadata(found['K990003'])
    assert meta['statement_or_summary'] == 'Statement' and meta['applicant'] == 'Pearl Co'
    assert meta['predicate'] is None and meta['predicate_absent_reason'] == 'not_exposed_by_openfda'
    assert found['P990007/S002']['record']['supplement_number'] == 'S002'
    assert 'registration_number' not in found['K990001']['record']['openfda']
    # A number nobody asked openFDA about has no answer; it is not reported as "no record".
    assert acquire.openfda_by_submission({}, ['K999999'])['K999999']['state'] == 'no_answer'
    q = acquire.openfda_requests(['K999999'])[0]
    absent = acquire.openfda_by_submission({q['key']: {'status': 404, 'params': q['params'], 'body': {'error': {}},
                                                       'fetched_at': 't'}}, ['K999999'])
    assert absent['K999999']['state'] == 'no_record'
    assert acquire.official_metadata({'record': None, 'reason': 'none held'}) == {'available': False, 'reason': 'none held'}


def test_frozen_archive_is_byte_stable_and_refuses_changed_bytes(tmp_path: Path) -> None:
    log = acquire.ResponseLog(tmp_path / 'a.jsonl')
    for key in ('b', 'a'):
        log.put({'key': key, 'fetched_at': '2026-11-13T00:00:00+00:00', 'status': 200, 'body': {'k': key}})
    archive = tmp_path / 'a.frozen.jsonl.gz'
    meta = acquire.freeze(log, archive, 'test')
    records, read_meta = acquire.read_archive(archive)
    assert list(records) == ['a', 'b'] and read_meta['sha256_uncompressed'] == meta['sha256_uncompressed']
    assert acquire.encode_archive(reversed(list(log.records.values()))) == gzip.decompress(archive.read_bytes())
    archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()).replace(b'"k":"a"', b'"k":"z"'), mtime=0))
    with pytest.raises(ValueError, match='do not match the recorded sha256'):
        acquire.read_archive(archive)


def test_an_interrupted_final_line_is_dropped_and_asked_again(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / 'log.jsonl'
    path.write_text('{"key": "a", "status": 200}\n{"key": "b", "sta')        # the process was killed mid-append
    log = acquire.ResponseLog(path)
    assert list(log.records) == ['a'] and 'dropped an interrupted final line' in capsys.readouterr().out
    assert path.read_text() == '{"key": "a", "status": 200}\n'
    acquire.write_atomic(tmp_path / 'store' / 'x.pdf', b'%PDF-1.4')
    assert [p.name for p in (tmp_path / 'store').iterdir()] == ['x.pdf']         # no partial file left behind


def test_a_disk_budget_stops_requests_and_the_skipped_documents_read_as_incomplete(tmp_path: Path) -> None:
    manifest, calls = acquire.Manifest(tmp_path / 'manifest.jsonl'), []
    plan = [{'document_id': acquire.document_id('K990001', f'{F.DOCS}/pdf99/{n}.pdf'), 'submission_number': n,
             'url': f'{F.DOCS}/pdf99/{n}.pdf', 'official_label': 'Summary', 'discovery': 'fda_database_page',
             'scope': 'this_submission'} for n in ('K990001', 'K990002')]
    counts = acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(calls), F.no_wait(), None, 10)
    assert (counts['retrieved'], counts['skipped_disk_budget'], len(calls)) == (1, 1, 1)
    skipped = manifest.latest()[plan[1]['document_id']]
    assert skipped['outcome'] == 'skipped_disk_budget' and 'budget' in skipped['error'] and not acquire.settled(skipped)
    doc = {'document_id': 'd', 'origin': 'fda_fetch', 'scope': 'this_submission', 'url': 'u', 'http_status': None,
           'availability': 'skipped_disk_budget', 'document_type': None, 'demographic_summary_candidate': False,
           'demographic_summary_reason': 'not retrieved', 'text': None, 'identifiers': [], 'study_description': None}
    scan = coverage.nct_citation_scan([doc], [])
    assert scan['state'] == 'search_incomplete_or_blocked' and 'disk budget' in scan['reason']
    # With room again, the skipped document is fetched; the one already held is not.
    again = acquire.fetch_documents(plan, manifest, tmp_path / 'store', F.stub_get(calls), F.no_wait())
    assert (again['retrieved'], again['already_settled'], len(calls)) == (1, 1, 2)


# ---------------------------------------------------------------- registry evidence

def test_nct_validation_states(acquired: dict[str, Any]) -> None:
    records = dict(acquired['frozen']['registry'][0])
    assert registry.nct_state(records, 'NCT99000001')[0] == 'resolves'
    assert registry.nct_state(records, 'NCT99009999') == ('not_found', None)
    assert registry.nct_state(records, 'NCT99000042') == ('not_looked_up', None)
    records['ctgov/study/NCT99000050'] = {'status': 500, 'body': None}
    assert registry.nct_state(records, 'NCT99000050')[0] == 'lookup_failed'
    records['ctgov/study/NCT99000051'] = {'status': 200, 'body': F.STUDIES[0]}      # the registry answered with another id
    assert registry.nct_state(records, 'NCT99000051')[0] == 'resolved_to_different_id'
    with pytest.raises(ValueError):
        registry.fetch_studies(['K990001'], acquire.ResponseLog(Path('/nonexistent/x')), F.stub_get(), F.no_wait())


@pytest.mark.parametrize('text, number, n, supplement', [
    ('Device: Viz LVO (De Novo Number DEN990005)', 'DEN990005', 1, None),
    ('cleared under 510(k) (K990001), see K9900012 and XK990001', 'K990001', 1, None),
    ('approved as P990007/S002', 'P990007/S002', 1, True), ('approved as P990007 S002', 'P990007/S002', 1, True),
    ('approved as P990007', 'P990007/S002', 1, False),
    # another supplement of the same PMA is not located; the original standing alone still is
    ('approved as P990007/S005 and as P990007', 'P990007/S002', 1, False),
    # a longer identifier that begins with the number is not the number
    ('model K990001ABC and K990001', 'K990001', 1, None),
    # an original PMA is cited only by its number standing alone
    ('approved as P990007/S002 and as P990007', 'P990007', 1, None),
    # a malformed supplement suffix is no citation; the well-formed supplement after it is
    ('approved as P990007/S0029 and as P990007/S002', 'P990007/S002', 1, True),
    ('approved with P990007/P990008', 'P990007', 1, None),         # a slash before another number is a list
    ('cleared as K990001/K990002', 'K990001', 1, None),
    # letter case is ignored, with the same boundaries and supplement checks
    ('cleared as k990001', 'K990001', 1, None), ('De Novo den990005 granted', 'DEN990005', 1, None),
    ('approved as p990007/s002', 'P990007/S002', 1, True), ('approved as p990007', 'P990007/S002', 1, False),
    ('model k990001abc and K990001', 'K990001', 1, None),
])
def test_a_submission_number_must_stand_exactly_in_a_named_field(text: str, number: str, n: int, supplement: Any) -> None:
    found = registry.locate_identifier({'protocolSection': {'descriptionModule': {'briefSummary': text}}}, number)
    assert len(found) == n and found[0]['supplement_cited'] is supplement
    assert found[0]['registry_field'] == 'protocolSection.descriptionModule.briefSummary' and found[0]['quote'] == text


def test_acquisition_looks_up_the_studies_curated_statements_name(tmp_path: Path) -> None:
    path = tmp_path / 'documentary_statements.csv'
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=links.STATEMENT_COLUMNS)
        w.writeheader()
        base = {'page_or_field': 'p. 4', 'verbatim_quote': 'the pivotal study', 'recorded_on': '2026-11-14', 'note': 'n',
                'proposed_relationship': links.STATEMENT_RELATIONSHIPS[0]}
        w.writerow({**base, 'submission_number': 'K990001', 'nct_id': 'NCT99000001', 'source_kind': 'fda_document',
                    'source_id': 'doc:K990001:abc'})
        w.writerow({**base, 'submission_number': 'K990002', 'nct_id': 'NCT99000002', 'source_kind': 'registry_record',
                    'source_id': 'NCT99000002', 'page_or_field': 'protocolSection.descriptionModule.briefSummary'})
    assert build_device_links.statement_study_ids(path) == {'NCT99000001', 'NCT99000002'}
    assert build_device_links.statement_quoted_records(path) == {'NCT99000002'}


def test_a_record_a_statement_quotes_is_fetched_whole_under_its_own_key(tmp_path: Path) -> None:
    asked: list[tuple[str, Any]] = []
    record = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000002'},
                                  'descriptionModule': {'briefSummary': 'the pivotal study'}}}
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        asked.append((url, params))
        return 200, {}, json.dumps(record).encode(), None
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    # a projected lookup already held for the id does not stand in for the whole record
    log.put({'key': 'ctgov/study/NCT99000002', 'status': 200, 'body': {'protocolSection': {}}})
    out = registry.fetch_full_studies(['NCT99000002'], log, get, F.no_wait())
    assert out == {'full_records_answered': 1, 'full_records_without_answer': 0}
    assert asked == [(f'{registry.API}/studies/NCT99000002', None)]         # no `fields` projection
    held = acquire.ResponseLog(tmp_path / 'log.jsonl').settled('ctgov/study_full/NCT99000002')
    assert held['body']['protocolSection']['descriptionModule']['briefSummary'] == 'the pivotal study'
    registry.fetch_full_studies(['NCT99000002'], log, get, F.no_wait())
    assert len(asked) == 1                                                  # asked once
    with pytest.raises(ValueError):
        registry.fetch_full_studies(['K990001'], log, get, F.no_wait())
    # the committed file is read the same way (it has no rows yet)
    assert build_device_links.statement_study_ids(build_device_links.ROOT / 'devices' / 'documentary_statements.csv') == set()


def test_a_search_answer_that_states_no_count_is_not_a_completed_search() -> None:
    chunk = registry.number_batches(['K990001'])[0]
    key = registry._key('ctgov/number_batch', registry._batch_params(chunk))
    def run(body: Any) -> dict[str, Any]:
        records = {key: {'status': 200, 'body': body, 'fetched_at': '2026-11-13T00:00:00+00:00'}}
        return registry.number_search_results(records, ['K990001'])['K990001']
    for body in ({'error': {'message': 'bad query'}}, {'studies': []}, {'totalCount': None}, {'totalCount': '0'}, None):
        out = run(body)
        assert out['state'] == 'incomplete' and out['n_search_hits'] is None, body
        assert 'did not state how many' in out['reason']
    assert run({'totalCount': 0})['state'] == 'completed'
    # the name searches: an answer with no count is counted apart and leaves the route incomplete
    q = {'kind': 'name', 'string': 'Quanta', 'key': 'ctgov/name/x', 'params': {}}
    for body, uncounted in (({'studies': []}, 1), ({'totalCount': 0, 'studies': []}, 0)):
        state = registry.search_state({'ctgov/name/x': {'status': 200, 'body': body, 'fetched_at': 't'}}, [q])
        assert (state['n_uncounted'], state['n_failed']) == (uncounted, 0)
    cell = coverage.registry_name_organisation_search(
        {**registry.search_state({'ctgov/name/x': {'status': 200, 'body': {'studies': []}, 'fetched_at': 't'}}, [q]),
         'n_name_queries': 1}, [])
    assert cell['state'] == 'search_incomplete_or_blocked' and 'without stating how many records matched' in cell['reason']



def _number_search(single: Any, full_id: str = 'NCT99000001') -> dict[str, Any]:
    chunk = registry.number_batches(['K990001'])[0]
    t = '2026-11-13T00:00:00+00:00'
    records = {registry._key('ctgov/number_batch', registry._batch_params(chunk)): {'status': 200, 'body': {'totalCount': 1}, 'fetched_at': t},
               registry._key('ctgov/number', registry._single_params('K990001')): {'status': 200, 'body': single, 'fetched_at': t},
               'ctgov/study_full/NCT99000001': {'status': 200, 'fetched_at': t, 'body': {'protocolSection': {
                   'identificationModule': {'nctId': full_id}, 'descriptionModule': {'briefSummary': 'cleared as K990001'}}}}}
    return registry.number_search_results(records, ['K990001'])['K990001']


def test_a_single_number_search_with_a_boolean_count_is_incomplete() -> None:
    hit = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}
    for total, studies in ((False, []), (True, [hit])):
        out = _number_search({'totalCount': total, 'studies': studies})
        assert out['state'] == 'incomplete' and out['n_search_hits'] is None and 'did not state how many' in out['reason']
    good = _number_search({'totalCount': 1, 'studies': [hit]})
    assert good['state'] == 'completed' and good['hits'][0]['located']


def test_a_full_record_held_under_another_id_is_not_read_for_the_hit() -> None:
    out = _number_search({'totalCount': 1, 'studies': [{'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}]},
                         full_id='NCT99000009')
    assert out['state'] == 'incomplete' and out['hits'] == []
    assert 'NCT99000001 (the record held is NCT99000009)' in out['reason']


@pytest.mark.parametrize('review, counts', [('accepted', True), ('unreviewed', False), ('unresolved', False),
                                            ('rejected', False)])
def test_a_record_set_aside_by_rule_counts_once_a_person_accepts_it(review: str, counts: bool) -> None:
    link = {'automated_validation': 'rejected_by_rule', 'human_review': review, 'target': {'id': 'NCT99000001'}}
    state, _ = coverage._link_outcome([link])
    assert (state == 'candidate_awaiting_review') is counts and (state is None) is not counts

@pytest.mark.parametrize('text', ['approved as P990007/S005', 'approved as P990007 S005', 'approved as P990007s005'])
def test_another_supplement_of_the_pma_is_not_a_citation_of_the_supplement_or_the_original(text: str) -> None:
    assert registry.locate_identifier({'protocolSection': {'descriptionModule': {'briefSummary': text}}},
                                      'P990007/S002') == []


@pytest.mark.parametrize('text', ['approved as P990007/S0029', 'approved as P990007/XYZ', 'approved as P990007 S0029',
                                  'approved as P990007 / s01'])
def test_an_original_pma_followed_by_a_malformed_suffix_is_not_located(text: str) -> None:
    assert registry.locate_identifier({'protocolSection': {'descriptionModule': {'briefSummary': text}}}, 'P990007') == []


def test_a_field_that_names_the_number_twice_is_one_citation() -> None:
    study = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001', 'briefTitle': 'T'},
                                 'descriptionModule': {'briefSummary': 'K990001; see K990001'}}}
    hit = {'nct_id': 'NCT99000001', 'located': registry.locate_identifier(study, 'K990001'),
           'study': registry.study_facts(study), 'retrieved_at': '2026-11-13'}
    assert len(hit['located']) == 2
    made = links.from_registry_citation('K990001', hit, '2026-01-01', {})
    assert len(made) == 1 and made[0]['automated_validation'] == 'identifier_confirmed'
    # the exact citation is kept where a field also cites only the original PMA
    supp = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001', 'briefTitle': 'T'},
                                'descriptionModule': {'briefSummary': 'P990007, later P990007/S002'}}}
    hit = {'nct_id': 'NCT99000001', 'located': registry.locate_identifier(supp, 'P990007/S002'),
           'study': registry.study_facts(supp), 'retrieved_at': '2026-11-13'}
    made = links.from_registry_citation('P990007/S002', hit, '2026-01-01', {})
    assert [(x['level'], x['automated_validation']) for x in made] == [('submission', 'identifier_confirmed')]


def test_a_document_over_the_size_limit_leaves_the_route_blocked() -> None:
    doc = {'document_id': 'doc:x', 'origin': 'fda_fetch', 'scope': 'this_submission', 'availability': 'too_large',
           'url': f'{F.DOCS}/pdf99/K990001.pdf', 'http_status': 200, 'document_type': None, 'text': None}
    page = _page([{'url': doc['url'], 'official_label': 'Summary'}])
    for cell in (coverage.fda_document_fetch(page, [doc], []), coverage.nct_citation_scan([doc], [])):
        assert cell['state'] == 'search_incomplete_or_blocked' and 'over the size limit' in cell['reason']


def _page(links: list[dict[str, str]]) -> dict[str, Any]:
    """A logged database-page answer confirmed to name the number asked for."""
    return {'status': 200, 'page_names_the_submission': True, 'body': {'document_links': links}}


def _record_page(label: str, number: str) -> str:
    return f'<html><TABLE><TR><TH Align="Left">{label}</TH><TD Align="Left">{number}</TD></TR></TABLE></html>'


def _pma_page(pma: str, supplement: str | None = None) -> str:
    rows = f'<TR><TH Align="Left">PMA Number</TH><TD Align="Left">{pma}</TD></TR>'
    if supplement:
        rows += f'<TR><TH Align="Left">Supplement Number</TH><TD Align="Left">{supplement}</TD></TR>'
    return f'<html><a href="pma.cfm?id={pma}">original PMA</a><TABLE>{rows}</TABLE></html>'


@pytest.mark.parametrize('number, page, names_it', [
    ('P990007/S002', _pma_page('P990007', 'S002'), True),
    ('P990007/S002', _pma_page('P990007'), False),                 # the original's page
    ('P990007/S002', _pma_page('P990007', 'S005'), False),         # another supplement's page
    ('P990007', _pma_page('P990007'), True),
    ('P990007', _pma_page('P990007', 'S002'), False),              # a supplement's page for the original
    ('P990007', _pma_page('P990008'), False),
    ('P990007', '<html>P990007 is not found</html>', False),       # the number is printed, but no record rows
    ('K990001', _record_page('510(k) Number', 'K990001'), True),
    ('K990001', _record_page('510(k) Number', 'K990002'), False),
    ('K990001', '<html>K990001 is not found</html>', False),     # an error page that echoes the request
    ('DEN990005', _record_page('De Novo Number', 'DEN990005'), True),
    ('DEN990005', '<html>De Novo DEN990005: no record</html>', False),
    ('DEN990005', _record_page('510(k) Number', 'DEN990005'), False),   # the number in another record's row
])
def test_a_database_page_is_confirmed_from_its_number_rows(number: str, page: str, names_it: bool) -> None:
    assert acquire.page_names_the_submission(number, page) is names_it


def test_the_original_pmas_page_served_for_a_supplement_is_not_its_answer(tmp_path: Path) -> None:
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    page = _pma_page('P990007').encode()
    acquire.fetch_database_pages(['P990007/S002'], log, tmp_path / 'html', lambda u, p: (200, {}, page, None), F.no_wait())
    rec = log.get('fda_page/P990007/S002')
    assert (rec['status'], rec['http_status'], rec['page_names_the_submission']) == ('not_the_record', 200, False)
    assert log.settled('fda_page/P990007/S002') is None


def test_a_search_hit_without_the_number_in_the_record_is_reported_as_such(acquired: dict[str, Any]) -> None:
    results = registry.number_search_results(acquired['frozen']['registry'][0], list(acquired['subs']))
    assert results['K990003']['n_search_hits'] == 1 and results['K990003']['hits'][0]['located'] == []
    assert results['DEN990005']['hits'][0]['located'][0]['registry_field'].endswith('interventions[].name')
    assert results['K990001']['state'] == 'completed' and results['K990001']['hits'] == []
    assert registry.number_search_results({}, ['K990001'])['K990001']['state'] == 'incomplete'


def test_name_evidence_needs_whole_tokens_in_a_title_or_intervention_name() -> None:
    study = registry.study_facts(F._study('NCT99000090', 'A study of KardiaAI in clinics', 'X', ['Zorbex Scanner'],
                                          description='uses Lumora'))
    study['interventions'][0]['description'] = 'The Lumora lamp'
    assert registry.name_evidence(study, inventory.name_forms('Kardia AI'))['registry_field'] == 'brief_title'
    assert registry.name_evidence(study, inventory.name_forms('Zorbex Scan')) is None       # 'scan' is not 'scanner'
    assert registry.name_evidence(study, inventory.name_forms('Lumora'))['strength'] == 'description_only'
    only = registry.name_evidence(registry.study_facts(F._study('NCT99000091', 'Athelas Home study', 'X')),
                                  inventory.name_forms('Athelas One', 'athelas'))
    assert only['strength'] == 'company_only'


def test_generic_names_get_no_name_query_and_unbalanced_brackets_never_reach_the_api() -> None:
    assert registry.name_queries('Deep Learning Image Reconstruction') == []
    strings = {q['string'] for q in registry.name_queries('Aplio i900, Software V7.0 (TUS-AI900, TUS-AI800')}
    assert 'Aplio i900' in strings and not any('(' in s or ')' in s for s in strings)
    assert len(registry.name_queries('EPIQ Series; Affiniti Series; Third Series')) == 4     # first two models only


def test_a_cut_off_search_is_followed_once_and_a_flood_is_reported_as_cut_off(tmp_path: Path) -> None:
    q = registry.sponsor_query('pearl')
    log = acquire.ResponseLog(tmp_path / 'r.jsonl')
    asked: list[str] = []

    def get(url: str, params: Any) -> acquire.Response:
        asked.append(params['pageSize'])
        n = int(params['pageSize'])
        return 200, {}, json.dumps({'totalCount': 150, 'studies': F.STUDIES[:1] * min(n, 150)}).encode(), None
    out = registry.fetch_searches([q], log, get, F.no_wait())
    assert asked == ['100', '1000'] and out['follow_ups_answered'] == 1
    assert registry.search_state(log.records, [q])['n_truncated'] == 0
    flood = acquire.ResponseLog(tmp_path / 'f.jsonl')
    registry.fetch_searches([q], flood, lambda u, p: (200, {}, json.dumps(
        {'totalCount': 5000, 'studies': F.STUDIES[:1]}).encode(), None), F.no_wait())
    assert registry.search_state(flood.records, [q])['n_truncated'] == 1


# ---------------------------------------------------------------- links

def links_of(family: dict[str, Any], **where: str) -> list[dict[str, Any]]:
    return [x for x in family['links.json']['links'] if all(x[k] == v for k, v in where.items())]


def test_every_record_carries_the_fields_the_requirements_list(family: dict[str, Any]) -> None:
    for x in family['links.json']['links']:
        assert {'source', 'target', 'level', 'proposed_relationship', 'method', 'evidence', 'source_version', 'confidence',
                'confidence_reasons', 'automated_validation', 'validation_reasons', 'human_review', 'timing'} <= set(x)
        assert x['evidence']['quote'] and (x['evidence']['location'].get('page') or x['evidence']['location'].get('registry_field'))
        assert links.problems(x) == []


def test_a_cited_nct_id_that_resolves_is_identifier_confirmed_at_submission_level(family: dict[str, Any]) -> None:
    (x,) = links_of(family, method='nct_id_in_submission_document', automated_validation='identifier_confirmed')
    assert (x['source']['id'], x['target']['id'], x['level']) == ('K990001', 'NCT99000001', 'submission')
    assert x['proposed_relationship'] == 'study_cited_in_submission_document'       # "cited", not "supporting"
    assert 'NCT99000001' in x['evidence']['quote'] and x['evidence']['location'] == {'page': 3}
    assert len(x['source_version']['document_sha256']) == 64 and x['source_version']['registry_data_timestamp']
    assert x['human_review'] == 'unreviewed' and x['reviewed_relationship'] is None


def test_a_cited_id_the_registry_does_not_hold_is_unresolved_not_confirmed(family: dict[str, Any]) -> None:
    (x,) = links_of(family, automated_validation='identifier_not_resolved')
    assert (x['source']['id'], x['target']['id'], x['confidence']) == ('K990002', 'NCT99009999', 'low')
    assert cell(family, 'K990002', 'nct_citation_scan')['state'] == 'candidate_awaiting_review'


def test_a_registry_record_citing_the_number_is_confirmed_and_a_bare_hit_is_rejected_by_rule(family: dict[str, Any]) -> None:
    (ok,) = links_of(family, method='submission_number_in_registry_record', automated_validation='identifier_confirmed')
    assert ok['source']['id'] == 'DEN990005' and 'DEN990005' in ok['evidence']['quote']
    assert ok['evidence']['location']['registry_field'].endswith('interventions[].name')
    (bad,) = links_of(family, automated_validation='rejected_by_rule')
    assert bad['source']['id'] == 'K990003' and bad['target']['id'] == 'NCT99000004'


def test_a_parent_pma_document_cannot_confirm_a_link_for_the_supplement(family: dict[str, Any]) -> None:
    (x,) = links_of(family, method='nct_id_in_parent_submission_document')
    assert (x['level'], x['automated_validation'], x['source']['id']) == ('family', 'not_validated', 'P990007/S002')


def test_name_matches_are_family_level_candidates_and_never_validated(family: dict[str, Any]) -> None:
    named = [x for x in family['links.json']['links'] if x['method'] in links.HEURISTIC_METHODS]
    assert len(named) == 5
    assert {(x['level'], x['automated_validation'], x['source']['entity_type']) for x in named} == \
        {('family', 'not_validated', 'family')}
    assert {x['confidence'] for x in named} <= {'moderate', 'low'}
    zorbex = next(x for x in named if x['target']['id'] == 'NCT99000001')
    # One family, two submissions: the study is set against both decision dates, and cannot be
    # attached to either submission by its name.
    assert [t['submission_number'] for t in zorbex['timing']['by_submission']] == ['K990001', 'K990002']
    assert zorbex['evidence']['organisation']['tier'] == 'normalised_exact'


def test_manufacturer_agreement_is_recorded_but_not_required_and_never_sufficient(family: dict[str, Any]) -> None:
    lumora = [x for x in family['links.json']['links'] if x['target']['id'] == 'NCT99000005']
    assert len(lumora) == 2 and {x['proposed_relationship'] for x in lumora} == {'name_only_candidate'}
    assert all(x['evidence']['organisation'] is None for x in lumora)
    # A different legal form after the same name is not agreement.
    weak = next(x for x in family['links.json']['links'] if x['target']['id'] == 'NCT99000007')
    assert weak['method'] == 'device_name_match' and weak['evidence']['organisation']['tier'] == 'normalised_legal_form_differs'
    # Shared sponsorship without the device's name gives no record at all, only a tally.
    assert not [x for x in family['links.json']['links'] if x['target']['id'] == 'NCT99000006']
    pearl = cell(family, 'K990003', 'registry_name_organisation_search')
    assert pearl['detail']['n_organisation_only'] == 1 and pearl['state'] == 'not_searched'


def test_a_common_word_name_without_organisation_agreement_is_rejected_by_rule_and_kept(acquired: dict[str, Any]) -> None:
    lumora = acquired['subs']['K990004']
    records = dict(acquired['frozen']['registry'][0])
    found = registry.family_name_search([lumora], records, {})
    assert found['tallies']['n_name_only'] == 1 and found['matches'][0]['common_word'] is False
    # The same name, now matching hundreds of records: an ordinary word.
    q = registry.name_queries('Lumora')[0]
    records[q['key']] = {**records[q['key']], 'body': {**records[q['key']]['body'], 'totalCount': 400}}
    flooded = registry.family_name_search([lumora], records, {})
    assert flooded['tallies']['n_name_only_common_word'] == 1 and flooded['matches'][0]['common_word'] is True
    m = flooded['matches'][0]
    family = {'family_id': 'family:candidate:x'}
    x = links.from_name_match(family, m['study'], m['name'], m['organisation'], {'K990004': '2020-07-07'}, {}, m['flags'], True)
    assert (x['automated_validation'], x['level'], x['confidence']) == ('rejected_by_rule', 'family', 'low')
    assert links.problems(x) == []
    cell_ = coverage.registry_name_organisation_search(
        {**flooded['search'], 'n_truncated': 0}, [x], flooded['tallies'])
    assert cell_['state'] == 'no_candidate_in_completed_scope' and cell_['detail']['n_matches_rejected_by_rule'] == 1
    # Organisation agreement keeps a common-word match in front of a reviewer.
    agreeing = {'tier': 'normalised_exact', 'role': 'lead_sponsor', 'registry_name': 'Bright Optics', 'listed_name': 'Bright Optics LLC'}
    y = links.from_name_match(family, m['study'], m['name'], agreeing, {'K990004': '2020-07-07'}, {}, [], True)
    assert (y['automated_validation'], y['method']) == ('not_validated', 'device_name_and_organisation_match')


@pytest.mark.parametrize('change, message', [
    ({'level': 'submission'}, 'name similarity can only reach a family'),
    ({'automated_validation': 'identifier_confirmed'}, 'must stay not_validated'),
    ({'automated_validation': 'quote_confirmed_in_source'}, 'must stay not_validated'),
    ({'confidence': 'identifier'}, 'cannot carry identifier'),
    ({'proposed_relationship': 'explicitly_cited_supporting_study'}, 'only by a documentary statement'),
    ({'human_review': 'accepted'}, 'without a row in reviews.csv'),
    ({'proposed_relationship': 'completed_before_decision'}, 'not in the vocabulary'),
])
def test_name_evidence_cannot_be_promoted(family: dict[str, Any], change: dict[str, str], message: str) -> None:
    x = copy.deepcopy(next(x for x in family['links.json']['links'] if x['method'] == 'device_name_and_organisation_match'))
    x.update(change)
    assert any(message in p for p in links.problems(x))


@pytest.mark.parametrize('mutate, message', [
    (lambda x: x['evidence'].update(quote=' '), 'no verbatim evidence'),
    (lambda x: x['evidence'].update(location={}), 'neither a document page nor a registry field'),
    (lambda x: x['evidence'].update(quote='A clinical study enrolled 412 patients.'), 'identifier is not in the quoted evidence'),
    (lambda x: x.update(level='family'), 'identifier method at submission level'),
    (lambda x: x.pop('timing'), 'timing must be recorded as its own object'),
    (lambda x: x.update(confidence_reasons=[]), 'need a reason'),
])
def test_an_identifier_confirmed_record_must_show_its_identifier(family: dict[str, Any], mutate: Any, message: str) -> None:
    x = copy.deepcopy(links_of(family, method='nct_id_in_submission_document', automated_validation='identifier_confirmed')[0])
    mutate(x)
    assert any(message in p for p in links.problems(x))


@pytest.mark.parametrize('start, primary, decision, expected', [
    ('2019-03-01', '2020-01-15', '2021-03-02', 'primary_completion_on_or_before_decision'),
    ('2019-03-01', '2022-01-15', '2021-03-02', 'started_on_or_before_decision_not_complete'),
    ('2019-03-01', None, '2021-03-02', 'started_on_or_before_decision_not_complete'),
    ('2021-06-01', '2022-01-15', '2021-03-02', 'started_within_365_days_after_decision'),
    ('2023-06-01', None, '2021-03-02', 'started_more_than_365_days_after_decision'),
    ('2021-03', '2022-01', '2021-03-15', 'indeterminate_at_recorded_precision'),      # the month straddles the decision
    ('2021-02', '2021-03', '2021-03-15', 'indeterminate_at_recorded_precision'),
    ('2021-05', '2022-01', '2021-03-15', 'started_within_365_days_after_decision'),
    (None, '2020-01-15', '2021-03-02', 'start_date_absent'), ('2019-03-01', '2020-01-15', None, 'decision_date_absent'),
])
def test_timing_is_described_per_decision_and_never_guessed(start: Any, primary: Any, decision: Any, expected: str) -> None:
    t = links.timing_for({'start_date': start, 'primary_completion_date': primary}, {'K990001': decision})
    assert t['by_submission'][0]['relation'] == expected
    assert t['study_start_date'] == start                      # the registry's own string, unaltered


def test_timing_does_not_change_the_relationship_validation_or_confidence(family: dict[str, Any]) -> None:
    x = links_of(family, method='nct_id_in_submission_document', automated_validation='identifier_confirmed')[0]
    assert x['timing']['by_submission'][0]['relation'] == 'primary_completion_on_or_before_decision'
    after = links.from_registry_citation('DEN990005', {'nct_id': 'NCT99000003', 'retrieved_at': None, 'located': [
        {'registry_field': 'f', 'quote': 'De Novo Number DEN990005', 'supplement_cited': None}],
        'study': {'start_date': '2030-01-01', 'brief_title': 't'}}, '2018-02-13', {})[0]
    assert after['timing']['by_submission'][0]['relation'] == 'started_more_than_365_days_after_decision'
    assert (after['automated_validation'], after['proposed_relationship']) == \
        ('identifier_confirmed', 'submission_cited_in_registry_record')
    assert not any(word in r for r in links.PROPOSED_RELATIONSHIPS[:5] for word in ('before', 'after', 'premarket', 'postmarket'))


def test_a_documentary_statement_gives_a_submission_link_without_an_identifier(tmp_path: Path, acquired: dict[str, Any]) -> None:
    docs = documents.build(acquired['manifest'].lines, {sn: s['pathway'] for sn, s in acquired['subs'].items()},
                           acquired['cache'], pdftext.extraction_version())
    summary = next(d for d in docs if d['url'].endswith('reviews/DEN990005.pdf'))
    quote = 'A retrospective study of 300 patients from two hospitals was conducted.'
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', 'NCT99000002', 'fda_document', summary['document_id'], '2', quote, 'related_validation_study',
         '2026-11-13', 'the summary names the two hospitals of this registered study'],
        ['DEN990005', 'NCT99000001', 'fda_document', summary['document_id'], '2', 'words that are not in the document',
         'explicitly_cited_supporting_study', '2026-11-13', 'misquoted on purpose']])
    family = F.build_family(tmp_path, acquired, curated)
    stated = {x['target']['id']: x for x in family['links.json']['links'] if x['method'] == 'documentary_statement'}
    ok, bad = stated['NCT99000002'], stated['NCT99000001']
    assert (ok['level'], ok['automated_validation'], ok['confidence']) == ('submission', 'quote_confirmed_in_source', 'documentary')
    assert ok['proposed_relationship'] == 'related_validation_study' and ok['human_review'] == 'unreviewed'
    assert bad['automated_validation'] == 'not_validated'
    assert contract.check(family) == []


# ---------------------------------------------------------------- human review

def test_reviews_are_applied_from_the_file_and_change_nothing_else(tmp_path: Path, acquired: dict[str, Any],
                                                                    family: dict[str, Any]) -> None:
    confirmed = links_of(family, method='nct_id_in_submission_document', automated_validation='identifier_confirmed')[0]
    named = next(x for x in family['links.json']['links'] if x['target']['id'] == 'NCT99000005')
    curated = tmp_path / 'curated'
    F.write_curated(curated, reviews=[
        [confirmed['link_id'], 'accepted', 'explicitly_cited_supporting_study', '2026-11-14', 'summary section 7 names it'],
        [named['link_id'], 'rejected', 'not_related', '2026-11-14', 'a light-therapy lamp, not the device'],
        ['link:0000000000000000', 'unresolved', '', '2026-11-14', 'evidence has since changed']])
    reviewed = F.build_family(tmp_path, acquired, curated)
    after = {x['link_id']: x for x in reviewed['links.json']['links']}
    a, r = after[confirmed['link_id']], after[named['link_id']]
    assert (a['human_review'], a['reviewed_relationship']) == ('accepted', 'explicitly_cited_supporting_study')
    assert a['review'] == {'reviewed_on': '2026-11-14', 'note': 'summary section 7 names it'}
    # The machine's findings are untouched by the review, in both directions.
    assert (a['automated_validation'], a['proposed_relationship']) == ('identifier_confirmed', 'study_cited_in_submission_document')
    assert (r['human_review'], r['automated_validation'], r['level']) == ('rejected', 'not_validated', 'family')
    assert reviewed['links.json']['orphaned_reviews'] == ['link:0000000000000000']
    assert reviewed['run.json']['reviews'] == {'n_rows': 3, 'n_applied': 2, 'orphaned_link_ids': ['link:0000000000000000']}
    assert sum(x['human_review'] != 'unreviewed' for x in after.values()) == 2
    assert contract.check(reviewed) == []
    # A rejected candidate no longer holds the route in "awaiting review".
    lumora = next(s for s in reviewed['submissions.json']['submissions'] if s['submission_number'] == 'K990004')
    assert cell(reviewed, lumora['submission_number'], 'registry_name_organisation_search')['state'] != 'candidate_awaiting_review'
    assert cell(family, 'K990004', 'registry_name_organisation_search')['state'] == 'candidate_awaiting_review'


@pytest.mark.parametrize('row, message', [
    (['link:1', 'verified', '', '2026-11-14', 'n'], 'human_review must be one of'),
    (['link:1', 'accepted', '', '2026-11-14', 'n'], 'which relationship was accepted'),
    (['link:1', 'accepted', 'pivotal', '2026-11-14', 'n'], 'unknown reviewed_relationship'),
    (['link:1', 'rejected', 'not_related', '', 'n'], 'reviewed_on'),
    (['link:1', 'rejected', 'not_related', '2026-11-14', ''], 'note are required'),
])
def test_review_rows_are_refused_rather_than_guessed(tmp_path: Path, row: list[str], message: str) -> None:
    F.write_curated(tmp_path, reviews=[row])
    with pytest.raises(ValueError, match=message):
        links.read_reviews(tmp_path / 'reviews.csv')


def test_the_tracked_curated_files_start_with_only_their_headers() -> None:
    expected = {'organization_aliases.csv': organizations.ALIAS_COLUMNS, 'families.csv': inventory.FAMILY_COLUMNS,
                'reviews.csv': links.REVIEW_COLUMNS, 'documentary_statements.csv': links.STATEMENT_COLUMNS}
    for name, columns in expected.items():
        with (ROOT / 'devices' / name).open(encoding='utf-8', newline='') as f:
            assert next(csv.reader(f)) == columns


# ---------------------------------------------------------------- coverage

def test_every_submission_has_every_route_in_one_of_seven_states_with_a_reason(family: dict[str, Any]) -> None:
    assert len(coverage.STATES) == 7
    for cov in family['coverage.json']['submissions']:
        assert [c['route'] for c in cov['routes']] == list(coverage.ROUTES)
        assert all(c['state'] in coverage.STATES and c['reason'] and c['source'] for c in cov['routes'])


@pytest.mark.parametrize('sn, route, state', [
    ('K990001', 'nct_citation_scan', 'verified_link'),
    ('K990001', 'fda_document_fetch', 'verified_link'),
    ('K990001', 'local_document', 'document_unavailable'),
    ('K990001', 'openfda_metadata', 'available_document_insufficient'),
    ('K990001', 'registry_submission_number_search', 'no_candidate_in_completed_scope'),
    ('K990001', 'registry_name_organisation_search', 'candidate_awaiting_review'),
    ('K990001', 'demographic_extraction', 'not_searched'),
    ('K990003', 'nct_citation_scan', 'available_document_insufficient'),        # only a Statement is held
    ('K990003', 'registry_name_organisation_search', 'not_searched'),           # generic name: no name query possible
    ('K990003', 'registry_submission_number_search', 'no_candidate_in_completed_scope'),   # the one hit was rejected by rule
    ('K990004', 'fda_document_fetch', 'document_unavailable'),                  # page answered, lists nothing, patterns 404
    ('K990004', 'nct_citation_scan', 'document_unavailable'),
    ('DEN990005', 'nct_citation_scan', 'no_candidate_in_completed_scope'),      # decision summary read, no NCT id
    ('DEN990005', 'registry_submission_number_search', 'verified_link'),
    ('DEN990005', 'demographic_extraction', 'available_document_insufficient'), # the pilot read the granting letter
    ('P990006', 'nct_citation_scan', 'available_document_insufficient'),        # SSED is a scan; letter and labeling only
    ('K990008', 'fda_document_fetch', 'search_incomplete_or_blocked'),          # the database page never answered
    ('P990007/S002', 'nct_citation_scan', 'document_unavailable'),              # nothing of its own; parent's is not its own
])
def test_coverage_states(family: dict[str, Any], sn: str, route: str, state: str) -> None:
    assert cell(family, sn, route)['state'] == state


def test_not_knowing_is_never_reported_as_no_candidate() -> None:
    doc = {'document_id': 'd', 'origin': 'fda_fetch', 'scope': 'this_submission', 'url': 'u', 'http_status': None,
           'availability': 'network_error', 'document_type': None, 'demographic_summary_candidate': False,
           'demographic_summary_reason': 'not retrieved', 'text': None, 'identifiers': [], 'study_description': None}
    assert coverage.nct_citation_scan([doc], [])['state'] == 'search_incomplete_or_blocked'
    assert coverage.nct_citation_scan([], [])['state'] == 'not_searched'
    assert coverage.openfda_metadata(None)['state'] == 'not_searched'
    assert coverage.openfda_metadata({'state': 'no_answer', 'reason': 'not answered'})['state'] == 'search_incomplete_or_blocked'
    assert coverage.openfda_metadata({'state': 'no_record', 'reason': 'none'})['state'] == 'document_unavailable'
    assert coverage.registry_submission_number_search(None, [])['state'] == 'not_searched'
    assert coverage.registry_submission_number_search({'state': 'incomplete', 'reason': 'x'}, [])['state'] == \
        'search_incomplete_or_blocked'
    search = {'n_queries': 3, 'n_name_queries': 2, 'n_missing': 0, 'n_failed': 0, 'n_truncated': 1, 'queries': []}
    assert coverage.registry_name_organisation_search(search, [])['state'] == 'search_incomplete_or_blocked'
    assert coverage.registry_name_organisation_search({**search, 'n_truncated': 0}, [])['state'] == \
        'no_candidate_in_completed_scope'
    assert coverage.registry_name_organisation_search({**search, 'n_missing': 1}, [])['state'] == 'search_incomplete_or_blocked'
    unread = {**doc, 'availability': 'retrieved', 'text': {'status': 'not_extracted'}}
    assert coverage.nct_citation_scan([unread], [])['state'] == 'search_incomplete_or_blocked'


def test_overall_status_shows_a_positive_and_claims_no_candidate_only_when_nothing_is_unknown(family: dict[str, Any]) -> None:
    states = {c['submission_number']: c['overall']['state'] for c in family['coverage.json']['submissions']}
    assert states['K990001'] == 'verified_link' and states['K990002'] == 'candidate_awaiting_review'
    # The overall status is over the three routes that can produce a link. openFDA (which carries
    # no study identifier) and a file missing from local disk do not hold it back.
    assert coverage.LINKAGE_ROUTES == ('nct_citation_scan', 'registry_submission_number_search',
                                       'registry_name_organisation_search')
    cells = [{'route': r, 'state': 'no_candidate_in_completed_scope'} for r in coverage.LINKAGE_ROUTES]
    cells += [{'route': 'openfda_metadata', 'state': 'available_document_insufficient'},
              {'route': 'local_document', 'state': 'document_unavailable'}]
    assert coverage.overall(cells)['state'] == 'no_candidate_in_completed_scope'
    cells[0] = {'route': cells[0]['route'], 'state': 'document_unavailable'}
    assert coverage.overall(cells)['state'] == 'document_unavailable'
    cells[1] = {'route': cells[1]['route'], 'state': 'search_incomplete_or_blocked'}
    assert coverage.overall(cells)['state'] == 'search_incomplete_or_blocked'


# ---------------------------------------------------------------- demographics

def test_pilot_extractions_are_attached_to_their_document_and_never_made_addable(family: dict[str, Any]) -> None:
    obs = family['demographics.json']['observations']
    assert len(obs) == 3 and {o['submission_number'] for o in obs} == {'DEN990005'}
    letter = next(d for d in family['documents.json']['documents'] if d['document_id'] == obs[0]['document_id'])
    assert letter['document_type'] == 'de_novo_granting_letter' and obs[0]['document_sha256'] == letter['sha256']
    for o in obs:
        assert o['may_be_added_to_registry_counts'] is False
        assert o['population']['population_identity'] == 'unresolved'
        assert o['population']['relation_to_registry_population'] == 'undetermined' and o['population']['registered_study'] is None
    assert family['demographics.json']['source']['sha256']


def test_not_reported_is_absence_with_a_reason_and_malformed_output_is_a_parsing_failure(family: dict[str, Any]) -> None:
    by_model = {o['extraction']['model_id']: o for o in family['demographics.json']['observations']}
    good, nested, bad = by_model['fictional-model-a'], by_model['fictional-model-b'], by_model['fictional-model-c']
    assert good['extraction']['parse_state'] == 'parsed' and good['n_demographic_fields'] == 7
    assert good['n_demographic_fields_reported'] == 0
    assert good['demographic_fields']['sex.female'] == {'reported': False, 'value': None, 'exact_quote': None,
                                                         'absent_reason': 'extraction_returned_not_reported',
                                                         'page_number': None, 'quote_located': 'not_checked'}
    assert nested['extraction']['parse_state'] == 'parsed_nested_too_deep' and nested['n_demographic_fields'] == 7
    assert bad['extraction']['parse_state'] == 'malformed' and 'sex' in bad['extraction']['sections_missing']
    # The indicated-use population is kept apart from study participants, with its quote checked.
    assert good['intended_use_population']['age_range'] == 'adults'
    assert demographics.quote_located('classification of the Quanta Triage', 1, F.TEXTS['pdf99/DEN990005.pdf']) == 'on_stated_page'
    assert demographics.quote_located('not in the letter', 1, F.TEXTS['pdf99/DEN990005.pdf']) == 'not_found'


def test_an_extraction_whose_document_is_not_held_stops_the_build(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='not in the document registry'):
        demographics.observations(F.pilot_extractions(tmp_path / 'p.json'), {}, {})


# ---------------------------------------------------------------- artifacts and contract

def test_fixture_family_passes_the_contract_and_is_byte_reproducible(tmp_path: Path, acquired: dict[str, Any],
                                                                     family: dict[str, Any]) -> None:
    assert contract.check(family) == []
    again = F.build_family(tmp_path, acquired, extractions=F.pilot_extractions(tmp_path / 'pilot.json'))
    assert {k: artifacts.dump(v) for k, v in again.items()} == {k: artifacts.dump(v) for k, v in family.items()}
    artifacts.write(family, tmp_path / 'out')
    assert contract.check(contract.load_dir(tmp_path / 'out')) == []
    assert sorted(p.name for p in (tmp_path / 'out').iterdir()) == sorted(list(artifacts.FILES) + ['run.json'])


def test_counts_name_their_unit_and_denominator(family: dict[str, Any]) -> None:
    summary, run = family['summary.json'], family['run.json']
    assert {'source_row', 'submission', 'family', 'pair', 'study', 'evidence_record'} <= set(run['counting_units'])
    den = summary['denominators']
    assert (den['source_rows']['n'], den['submissions']['n'], den['families']['n']) == (11, 8, 7)
    metric = {m['id']: m for m in summary['metrics']}
    assert all(m['definition'] and m['denominator'] in den for m in metric.values())
    # Records, pairs and studies are three different counts of the same links.
    assert (den['evidence_records']['n'], den['pairs']['n'], den['studies']['n']) == (10, 10, 7)
    assert metric['pairs_submission_specific']['numerator'] == 2 and metric['pairs_family_only']['numerator'] == 5
    assert metric['evidence_records_rejected_by_rule']['numerator'] == 1
    assert metric['evidence_records_identifier_not_resolved']['numerator'] == 1
    assert metric['substantive_documents_describing_study_without_registry_id']['numerator'] == 1
    assert run['scheduled'] is False and run['sources']['text_extraction']['ocr'] is False


def test_official_metadata_stands_beside_the_list_and_differences_are_recorded(family: dict[str, Any]) -> None:
    s = by_id(family, 'K990004')
    assert s['conflicts'] == {'Primary Product Code': ['AAA', 'BBB']} and s['product_code'] == 'AAA'
    assert s['official']['available'] is True and s['official_differs_from_list'] == {}
    assert by_id(family, 'K990001')['official']['statement_or_summary'] == 'Summary'


def test_review_queue_puts_identifier_confirmed_first_and_documents_without_demographics_third(family: dict[str, Any]) -> None:
    tiers = family['review_queue.json']['tiers']
    assert [r['entity'] for r in tiers['1_identifier_confirmed']] == ['DEN990005', 'K990001']
    assert len(tiers['2_name_and_organisation']) == 2
    third = tiers['3_documents_without_demographic_evidence']
    assert [r['entity'] for r in third][0] == 'K990001' and third[0]['text_mentions_race_or_ethnicity'] is True
    assert 'P990006' not in [r['entity'] for r in third]        # its only summary is a scan: nothing readable yet


def _mutated(family: dict[str, Any], mutate: Any) -> list[str]:
    files = copy.deepcopy(family)
    mutate(files)
    # Re-stamp the hashes so that only the invariant under test can fail.
    files['run.json']['files'] = {name: artifacts.sha256(artifacts.dump(files[name])) for name in artifacts.FILES}
    return contract.check(files)


def _link(files: dict[str, Any], method: str) -> dict[str, Any]:
    return next(x for x in files['links.json']['links'] if x['method'] == method)


@pytest.mark.parametrize('mutate, message', [
    (lambda f: _link(f, 'device_name_match').update(automated_validation='identifier_confirmed'), 'must stay not_validated'),
    (lambda f: _link(f, 'device_name_and_organisation_match').update(level='submission'), 'can only reach a family'),
    (lambda f: _link(f, 'nct_id_in_submission_document')['evidence'].update(quote=''), 'no verbatim evidence'),
    (lambda f: _link(f, 'nct_id_in_submission_document')['evidence'].update(location={'page': 0}), 'neither a document page'),
    (lambda f: _link(f, 'device_name_match').update(human_review='accepted', review={'reviewed_on': 'x', 'note': 'y'}),
     'differs from the reviews applied'),
    (lambda f: f['studies.json']['studies'][0].update(registry_state='not_found'), 'does not resolve on the registry'),
    (lambda f: _link(f, 'device_name_match')['target'].update(id='NCT99000099'), 'not in studies.json'),
    (lambda f: f['submissions.json']['reconciliation'].update(n_rows=10), 'rows do not reconcile'),
    (lambda f: f['submissions.json']['submissions'][0]['source_rows'].pop(), 'does not carry all of its source rows'),
    (lambda f: f['submissions.json']['reconciliation']['duplicated_numbers_by_class']['identical'].clear(),
     'exactly one duplicate class'),
    (lambda f: f['families.json']['families'][0]['submission_numbers'].append('K990004'), 'exactly one family'),
    (lambda f: f['families.json']['families'][0].update(status='curated'), 'only a family listed in families.csv'),
    (lambda f: f['organizations.json']['organizations'][0]['normalised_keys'].append('another name'),
     'merged without an alias row'),
    (lambda f: next(d for d in f['documents.json']['documents'] if d['document_type'] == 'de_novo_granting_letter')
     .update(demographic_summary_candidate=True), 'treated as a demographic summary'),
    (lambda f: next(d for d in f['documents.json']['documents'] if d['availability'] == 'not_found')
     .update(document_type='510k_summary'), 'not retrieved, yet carries'),
    (lambda f: next(d for d in f['documents.json']['documents'] if d['document_type'] == '510k_summary')['classification']
     .update(matched_phrase=None), 'without the matched phrase'),
    (lambda f: f['coverage.json']['submissions'][0]['routes'][0].update(state='searched'), 'outside the seven'),
    (lambda f: f['coverage.json']['submissions'][0]['routes'][0].update(reason=''), 'no reason'),
    (lambda f: next(c for c in f['coverage.json']['submissions'] if c['submission_number'] == 'K990004')['routes'][3]
     .update(state='verified_link'), 'verified_link without an identifier-confirmed record'),
    (lambda f: f['coverage.json']['submissions'][0]['routes'].pop(), 'each route once'),
    (lambda f: f['summary.json']['metrics'][0].update(numerator=10 ** 6), 'numerator outside'),
    (lambda f: f['summary.json']['metrics'][0].update(definition=''), 'no definition'),
    (lambda f: f['summary.json']['metrics'][0].update(denominator='devices'), 'unknown denominator'),
    (lambda f: f['summary.json']['denominators']['submissions'].update(n=11), 'denominator submissions differs'),
    (lambda f: f['demographics.json']['observations'][0].update(may_be_added_to_registry_counts=True),
     'must not be addable to registry counts'),
    (lambda f: f['demographics.json']['observations'][0]['population'].update(relation_to_registry_population='same_participants'),
     'unresolved population cannot have a determined relation'),
    (lambda f: f['demographics.json']['observations'][0]['population'].update(population_identity='same_as_registered_study'),
     'needs the study and the reason'),
    (lambda f: f['demographics.json']['observations'][0]['demographic_fields']['sex.female'].update(value=0),
     'must be absent with a reason, not a value'),
    (lambda f: f['demographics.json']['observations'][0].update(document_sha256='0' * 64), 'not attached to a held document'),
    (lambda f: f['run.json'].update(scheduled=True), 'scheduled: false'),
    (lambda f: f['links.json']['pairs'].pop(), 'pairs do not partition'),
])
def test_contract_refuses(family: dict[str, Any], mutate: Any, message: str) -> None:
    problems = _mutated(family, mutate)
    assert any(message in p for p in problems), problems[:5]


def test_contract_refuses_a_changed_file_and_a_missing_one(family: dict[str, Any]) -> None:
    files = copy.deepcopy(family)
    files['links.json']['links'][0]['confidence_reasons'].append('edited after the build')
    assert any('sha256 does not match run.json' in p for p in contract.check(files))
    files = copy.deepcopy(family)
    del files['coverage.json']
    assert any('coverage.json' in p for p in contract.check(files))
    assert contract.check({}) == ['run.json is missing']


# ---------------------------------------------------------------- the build script

def test_build_script_reads_only_the_frozen_cache_and_writes_only_a_passing_family(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    a = F.acquire_all(tmp_path)
    curated = tmp_path / 'repo' / 'devices'
    F.write_curated(curated)
    monkeypatch.setattr(build_device_links, 'ROOT', tmp_path / 'repo')

    def no_network(*args: Any, **kw: Any) -> None:
        raise AssertionError('the build must not open a connection')
    monkeypatch.setattr(acquire, 'requests_get', no_network)
    argv = ['build', '--cache', str(a['cache']), '--inventory', str(a['inventory']), '--source-list', str(tmp_path / 'none.csv'),
            '--out', str(tmp_path / 'out'), '--generated-at', F.GENERATED_AT]
    assert build_device_links.main(argv) == 0
    out = contract.load_dir(tmp_path / 'out')
    assert contract.check(out) == [] and out['run.json']['inputs']['inventory']['n_rows'] == 11
    assert '11 rows -> 8 submissions -> 7 families' in capsys.readouterr().out
    # A curated file that breaks its own rules refuses the build and leaves the last output alone.
    before = (tmp_path / 'out' / 'run.json').read_bytes()
    F.write_curated(curated, reviews=[['link:1', 'verified', '', '2026-11-14', 'n']])
    assert build_device_links.main(argv) == 1 and (tmp_path / 'out' / 'run.json').read_bytes() == before
    assert 'refused' in capsys.readouterr().err


def test_nothing_schedules_this_build() -> None:
    workflows = ROOT / '.github' / 'workflows'
    if not workflows.exists():
        pytest.skip('no workflows directory beside this package (staging copy)')
    for path in workflows.glob('*.y*ml'):
        text = path.read_text()
        assert 'build_device_links' not in text and 'devices/' not in text, path.name


def test_garbled_summary_pages_leave_the_document_unread_in_the_registry(tmp_path: Path) -> None:
    readable = 'The device is intended for use by trained readers in the review of images. ' * 4
    shifted = ''.join(chr(ord(c) - 29) if c.isalpha() else '\x03' for c in readable)
    pdf = b'%PDF-1.4 garbled summary'
    digest = acquire.sha256(pdf)
    (tmp_path / 'documents' / 'store').mkdir(parents=True)
    (tmp_path / 'documents' / 'store' / f'{digest}.pdf').write_bytes(pdf)
    F.write_text(tmp_path, digest, [LETTER, shifted, shifted])
    line = {'document_id': 'doc:K990001:x', 'submission_number': 'K990001', 'url': f'{F.DOCS}/pdf99/K990001.pdf',
            'official_label': 'Summary', 'discovery': 'fda_database_page', 'scope': 'this_submission', 'origin': 'fda_fetch',
            'retrieved_at': 't', 'http_status': 200, 'bytes': len(pdf), 'sha256': digest, 'outcome': 'retrieved', 'attempts': 1}
    (doc,) = documents.build([line], {'K990001': '510k'}, tmp_path, pdftext.extraction_version())
    assert doc['text']['extractor_status'] == 'text' and doc['text']['status'] == 'partial'
    assert (doc['text']['n_pages_garbled'], doc['text']['n_pages_with_text']) == (2, 1)
    assert doc['document_type'] == '510k_summary' and doc['classification']['basis'] == 'official_label_unread_pages'
    assert doc['demographic_summary_candidate'] is False and 'OCR' in doc['demographic_summary_reason']
    scan = coverage.nct_citation_scan([doc], [])
    assert scan['state'] == 'available_document_insufficient' and scan['detail']['n_pages_without_usable_text'] == 2


def test_the_run_id_follows_from_the_published_content(family: dict[str, Any]) -> None:
    """The id is a digest of what the family publishes (the producing code's digest included), not of
    a hand-kept list of inputs; the contract recomputes it."""
    assert contract.check(family) == []
    assert family['run.json']['run_id'][-10:] == artifacts.run_identity(family)
    # one published value changed: another id, and the old id is refused for the changed content
    changed = copy.deepcopy(family)
    changed['summary.json']['denominators'] = {**changed['summary.json'].get('denominators', {}), 'extra': {'n': 1}}
    assert artifacts.run_identity(changed) != artifacts.run_identity(family)
    changed['run.json']['files']['summary.json'] = artifacts.sha256(artifacts.dump(changed['summary.json']))
    assert 'run.json run_id does not follow from the content of the family' in contract.check(changed)
    # the same content built from other code: another id
    other_code = copy.deepcopy(family)
    other_code['run.json']['producer']['source_sha256'] = '1' * 64
    assert artifacts.run_identity(other_code) != artifacts.run_identity(family)
    # the build time is not part of it
    later = copy.deepcopy(family)
    later['run.json']['generated_at'] = '2030-01-01T00:00:00+00:00'
    assert artifacts.run_identity(later) == artifacts.run_identity(family)
    # a manifest without the code digest is refused
    no_digest = copy.deepcopy(family)
    del no_digest['run.json']['producer']['source_sha256']
    assert 'run.json producer does not carry a digest of the producing code' in contract.check(no_digest)


def test_the_code_digest_covers_every_producing_file(tmp_path: Path) -> None:
    paths = build_device_links.producer_paths()
    assert 'scripts/build_device_links.py' in paths and 'devices/links.py' in paths and 'devices/reviews.csv' in paths
    assert not any('__pycache__' in p for p in paths)
    for rel in paths:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    before = build_device_links.source_digest(tmp_path)
    assert before == build_device_links.source_digest(ROOT)
    (tmp_path / 'devices' / 'reviews.csv').write_bytes((tmp_path / 'devices' / 'reviews.csv').read_bytes() + b'\n')
    assert build_device_links.source_digest(tmp_path) != before


def test_a_number_search_that_found_more_than_it_read_is_incomplete() -> None:
    """One page of up to 100 records is read per number. A search reporting more records than the
    page holds, or no count at all, cannot support a "no candidate" finding."""
    number = registry.searchable_number('K990001')
    batch_key = registry._key('ctgov/number_batch', registry._batch_params(registry.number_batches(['K990001'])[0]))
    one_key = registry._key('ctgov/number', registry._single_params(number))
    hit = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}
    def result(body: dict[str, Any]) -> dict[str, Any]:
        records = {batch_key: {'status': 200, 'body': {'totalCount': 1}, 'fetched_at': 't'},
                   one_key: {'status': 200, 'body': body, 'fetched_at': 't'},
                   'ctgov/study_full/NCT99000001': {'status': 200, 'body': hit, 'fetched_at': 't'}}
        return registry.number_search_results(records, ['K990001'])['K990001']
    assert result({'totalCount': 1, 'studies': [hit]})['state'] == 'completed'
    more = result({'totalCount': 250, 'studies': [hit]})
    assert more['state'] == 'incomplete' and 'found 250 records and the page read holds 1' in more['reason']
    assert more['n_search_hits'] == 250 and more['hits'] == []
    assert result({'studies': [hit]})['state'] == 'incomplete'


def test_a_documentary_statement_must_come_from_the_submissions_own_document(tmp_path: Path, acquired: dict[str, Any]) -> None:
    docs = documents.build(acquired['manifest'].lines, {sn: s['pathway'] for sn, s in acquired['subs'].items()},
                           acquired['cache'], pdftext.extraction_version())
    other = next(d for d in docs if d['submission_number'] != 'DEN990005' and d['sha256'])
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', 'NCT99000002', 'fda_document', other['document_id'], '1', 'any words', 'related_validation_study',
         '2026-11-13', 'the document id belongs to another submission']])
    with pytest.raises(ValueError, match=rf"is a document of {other['submission_number']} \(this_submission\), not of DEN990005 itself"):
        F.build_family(tmp_path, acquired, curated)


def test_the_disk_budget_counts_a_file_once_when_two_addresses_serve_it(tmp_path: Path) -> None:
    """Two planned addresses return the same bytes. The content-addressed store holds one file, and
    the budget counts it once, so a third document still fits."""
    body = b'%PDF-1.4 the same fictional file'
    def get(url: str, params: dict[str, Any] | None = None) -> acquire.Response:
        return (200, {'content-type': 'application/pdf'}, body, None)
    plan = [{'document_id': acquire.document_id(n, f'{F.DOCS}/pdf99/{n}.pdf'), 'submission_number': n,
             'url': f'{F.DOCS}/pdf99/{n}.pdf', 'official_label': 'Summary', 'discovery': 'fda_database_page',
             'scope': 'this_submission'} for n in ('K990001', 'K990002', 'K990003')]
    counts = acquire.fetch_documents(plan, acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store', get,
                                     F.no_wait(), None, len(body) + 1)
    assert (counts['retrieved'], counts['skipped_disk_budget']) == (3, 0)
    assert len(list((tmp_path / 'store').glob('*.pdf'))) == 1


def test_the_common_word_rule_follows_the_name_a_study_was_matched_on(acquired: dict[str, Any]) -> None:
    """A family with two listed names. A sibling's name flooding the registry does not set aside a
    study matched on the family's other, distinctive name."""
    lumora = acquired['subs']['K990004']
    records = dict(acquired['frozen']['registry'][0])
    sibling = {**lumora, 'submission_number': 'K990099', 'device_names_as_listed': ['Contour']}
    for q in registry.name_queries('Contour'):
        records[q['key']] = {'status': 200, 'body': {'totalCount': 400, 'studies': []}, 'fetched_at': 't'}
    found = registry.family_name_search([lumora, sibling], records, {})
    assert found['tallies']['n_name_only'] == 1 and found['tallies']['n_name_only_common_word'] == 0
    m = found['matches'][0]
    assert m['common_word'] is False and m['name']['listed_name'] == 'Lumora'
    assert not any('50 or more records' in f for f in m['flags'])
    # the matched name itself flooding still sets the match aside
    q = registry.name_queries('Lumora')[0]
    records[q['key']] = {**records[q['key']], 'body': {**records[q['key']]['body'], 'totalCount': 400}}
    assert registry.family_name_search([lumora, sibling], records, {})['tallies']['n_name_only_common_word'] == 1


@pytest.mark.parametrize('path, expected', [
    ('protocolSection.identificationModule.briefTitle', 'A fictional study'),
    ('protocolSection.armsInterventionsModule.interventions[1].name', 'Second'),
    ('protocolSection.designModule.enrollmentInfo.count', '200'),
    ('protocolSection.identificationModule.nope', None),
    ('protocolSection.armsInterventionsModule.interventions[5].name', None),
    ('', None),
])
def test_a_registry_quote_is_read_from_the_named_field_only(path: str, expected: str | None) -> None:
    record = {'protocolSection': {'identificationModule': {'briefTitle': 'A fictional study'},
                                  'armsInterventionsModule': {'interventions': [{'name': 'First'}, {'name': 'Second'}]},
                                  'designModule': {'enrollmentInfo': {'count': 200}}}}
    assert artifacts.registry_field_text(record, path) == expected


def test_a_registry_statement_quoted_from_another_field_is_not_validated(tmp_path: Path, acquired: dict[str, Any]) -> None:
    """The quote stands in the record, but not in the field the row names."""
    nct = 'NCT99000001'
    body = acquired['frozen']['registry'][0][f'ctgov/study/{nct}']['body']
    title = body['protocolSection']['identificationModule']['briefTitle']
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', nct, 'registry_record', nct, 'protocolSection.identificationModule.briefTitle', title,
         'related_validation_study', '2026-11-13', 'the title is quoted from the named field'],
        ['DEN990005', nct, 'registry_record', nct, 'protocolSection.descriptionModule.briefSummary', title,
         'explicitly_cited_supporting_study', '2026-11-13', 'the same words, under a field that does not hold them']])
    family = F.build_family(tmp_path, acquired, curated)
    stated = {x['proposed_relationship']: x for x in family['links.json']['links'] if x['method'] == 'documentary_statement'}
    assert stated['related_validation_study']['automated_validation'] == 'quote_confirmed_in_source'
    assert stated['explicitly_cited_supporting_study']['automated_validation'] == 'not_validated'



def test_a_registry_statement_is_read_only_from_a_record_that_names_its_study(tmp_path: Path, acquired: dict[str, Any]) -> None:
    """The quote stands in the field named, but the record held does not say which study it is."""
    nct = 'NCT99000001'
    a = copy.deepcopy(acquired)
    records, _ = a['frozen']['registry']
    body = copy.deepcopy(records[f'ctgov/study/{nct}']['body'])
    title = body['protocolSection']['identificationModule']['briefTitle']
    del body['protocolSection']['identificationModule']['nctId']
    records[f'ctgov/study_full/{nct}'] = {**records[f'ctgov/study/{nct}'], 'key': f'ctgov/study_full/{nct}', 'body': body}
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', nct, 'registry_record', nct, 'protocolSection.identificationModule.briefTitle', title,
         'related_validation_study', '2026-11-13', 'the record held carries no study id']])
    family = F.build_family(tmp_path, a, curated)
    stated = [x for x in family['links.json']['links'] if x['method'] == 'documentary_statement']
    assert [x['automated_validation'] for x in stated] == ['not_validated']


def test_a_link_found_by_a_partial_number_search_still_stands() -> None:
    link = {'automated_validation': 'identifier_confirmed', 'human_review': 'unreviewed', 'target': {'id': 'NCT99000001'}}
    partial = {'state': 'incomplete', 'reason': "full records not held for ['NCT99000002']", 'n_search_hits': 2,
               'searched_at': '2026-11-13T00:00:00+00:00'}
    cell = coverage.registry_submission_number_search(partial, [link])
    assert cell['state'] == 'verified_link' and 'the search was not complete' in cell['reason']
    assert coverage.registry_submission_number_search(partial, [])['state'] == 'search_incomplete_or_blocked'


def test_an_openfda_200_without_a_list_of_results_is_no_answer(tmp_path: Path) -> None:
    log = acquire.ResponseLog(tmp_path / 'openfda.jsonl')
    envelope = json.dumps({'error': {'code': 'SERVER_ERROR', 'message': 'try again'}}).encode()
    calls: list[str] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(url)
        return 200, {'content-type': 'application/json'}, envelope, None
    acquire.fetch_openfda(['K990001', 'K990002'], log, get, F.no_wait())
    req = acquire.openfda_requests(['K990001', 'K990002'])[0]
    rec = log.get(req['key'])
    assert (rec['status'], rec['http_status']) == (acquire.UNUSABLE_200, 200) and log.settled(req['key']) is None
    found = acquire.openfda_by_submission({req['key']: rec}, ['K990001', 'K990002'])
    assert {v['state'] for v in found.values()} == {'error'}              # not "no record"
    # asked again on the next run
    acquire.fetch_openfda(['K990001', 'K990002'], log, get, F.no_wait())
    assert len(calls) == 2
    # an answer logged as 200 before answers were checked for shape is still no answer
    legacy = {req['key']: {**rec, 'status': 200, 'body': {'meta': {}}}}
    assert {v['state'] for v in acquire.openfda_by_submission(legacy, ['K990001', 'K990002']).values()} == {'error'}


def test_an_answer_logged_for_other_parameters_is_not_this_requests_answer(tmp_path: Path) -> None:
    """Two 510(k) batches can share a key (first number, last number, length) and differ inside."""
    old = acquire.openfda_requests(['K990001', 'K990002', 'K990009'])[0]
    new = acquire.openfda_requests(['K990001', 'K990005', 'K990009'])[0]
    assert old['key'] == new['key'] and old['params'] != new['params']
    log = acquire.ResponseLog(tmp_path / 'openfda.jsonl')
    calls: list[Any] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(params)
        return 200, {}, json.dumps({'results': [{'k_number': 'K990005'}]}).encode(), None
    log.put({'key': old['key'], 'params': old['params'], 'status': 200, 'body': {'results': []}, 'fetched_at': 't'})
    # the reader does not take the old batch's answer for the new batch
    stale = acquire.openfda_by_submission(log.records, ['K990001', 'K990005', 'K990009'])
    assert stale['K990005']['state'] == 'no_answer' and 'different request' in stale['K990005']['reason']
    # acquisition asks the new batch, and its answer becomes the one held under the key
    acquire.fetch_openfda(['K990001', 'K990005', 'K990009'], log, get, F.no_wait())
    assert calls == [new['params']]
    fresh = acquire.openfda_by_submission(acquire.ResponseLog(tmp_path / 'openfda.jsonl').records,
                                          ['K990001', 'K990005', 'K990009'])
    assert fresh['K990005']['state'] == 'found'


@pytest.mark.parametrize('body', [{'error': {'message': 'try again'}}, {'protocolSection': {}}, {}])
def test_a_study_answer_without_its_own_id_is_no_answer(tmp_path: Path, body: Any) -> None:
    log = acquire.ResponseLog(tmp_path / 'registry.jsonl')
    calls: list[str] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(url)
        return 200, {}, json.dumps(body).encode(), None
    for fetch, key in ((registry.fetch_studies, 'ctgov/study/NCT99000001'),
                       (registry.fetch_full_studies, 'ctgov/study_full/NCT99000001')):
        fetch(['NCT99000001'], log, get, F.no_wait())
        assert log.get(key)['status'] == acquire.UNUSABLE_200 and log.settled(key) is None
    # a 200 of that shape logged before the check is a failed lookup, not a record under another id
    assert registry.nct_state({'ctgov/study/NCT99000001': {'status': 200, 'body': body}}, 'NCT99000001')[0] == 'lookup_failed'



@pytest.mark.parametrize('path', ['protocolSection..identificationModule.briefTitle',
                                  'protocolSection.identificationModule.briefTitle.', '.protocolSection.identificationModule.briefTitle',
                                  'protocolSection.identificationModule[.briefTitle', 'protocolSection.identificationModule]briefTitle',
                                  'protocolSection identificationModule briefTitle'])
def test_a_malformed_registry_field_path_resolves_to_nothing(path: str) -> None:
    record = {'protocolSection': {'identificationModule': {'briefTitle': 'A title'}, 'armsInterventionsModule': {
        'interventions': [{'name': 'A device'}]}}}
    assert artifacts.registry_field_text(record, path) is None
    assert artifacts.registry_field_text(record, 'protocolSection.identificationModule.briefTitle') == 'A title'
    assert artifacts.registry_field_text(record, 'protocolSection.armsInterventionsModule.interventions[0].name') == 'A device'


@pytest.mark.parametrize('body', [{'error': {'message': 'try again'}}, {'studies': []}, {'totalCount': True, 'studies': []},
                                  {'totalCount': '3'}, {'totalCount': 1, 'studies': {}}])
def test_a_search_answer_without_a_count_is_asked_again(tmp_path: Path, body: Any) -> None:
    log = acquire.ResponseLog(tmp_path / 'registry.jsonl')
    calls: list[str] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(url)
        return 200, {}, json.dumps(body).encode(), None
    q = {'kind': 'name', 'string': 'Quanta', 'key': 'ctgov/name/x', 'params': {'query.term': 'Quanta'}}
    registry.fetch_searches([q], log, get, F.no_wait())
    assert log.get('ctgov/name/x')['status'] == acquire.UNUSABLE_200 and log.settled('ctgov/name/x') is None
    registry.fetch_number_search(['K990001'], log, get, F.no_wait())
    batch = registry._key('ctgov/number_batch', registry._batch_params(registry.number_batches(['K990001'])[0]))
    assert log.get(batch)['status'] == acquire.UNUSABLE_200 and log.settled(batch) is None
    study = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}
    assert registry.search_answer({'totalCount': 0}) and registry.search_answer({'totalCount': 1, 'studies': [study]})
    # a returned study without its own id would be dropped by the reader: the answer is no answer
    assert not registry.search_answer({'totalCount': 2, 'studies': [study, {}]})


def test_a_build_does_not_replace_a_directory_that_is_not_a_device_build(tmp_path: Path, family: dict[str, Any]) -> None:
    other = tmp_path / 'not-a-build'
    other.mkdir()
    (other / 'notes.txt').write_text('keep me')
    with pytest.raises(ValueError, match='refusing to replace it'):
        artifacts.write(family, other)
    assert (other / 'notes.txt').read_text() == 'keep me'
    a_file = tmp_path / 'a-file'
    a_file.write_text('keep me too')
    with pytest.raises(ValueError):
        artifacts.write(family, a_file)
    assert a_file.read_text() == 'keep me too'
    # a stray directory named like the old fixed staging path is left alone
    out = tmp_path / 'build'
    (tmp_path / 'build.staging').mkdir()
    (tmp_path / 'build.staging' / 'keep').write_text('x')
    artifacts.write(family, out)
    artifacts.write(family, out)                                    # a device build is replaced
    assert sorted(p.name for p in out.iterdir()) == sorted(family)
    assert (tmp_path / 'build.staging' / 'keep').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['a-file', 'build', 'build.staging', 'not-a-build']


def test_a_build_does_not_replace_a_directory_that_only_borrows_its_names(tmp_path: Path, family: dict[str, Any]) -> None:
    only_summary = tmp_path / 'a'
    only_summary.mkdir()
    (only_summary / 'summary.json').write_text('{"mine": true}')
    nested = tmp_path / 'b'
    (nested / 'run.json').mkdir(parents=True)
    (nested / 'run.json' / 'keep.txt').write_text('keep me')
    for out in (only_summary, nested):
        with pytest.raises(ValueError, match='refusing to replace it'):
            artifacts.write(family, out)
    assert (only_summary / 'summary.json').read_text() == '{"mine": true}'
    assert (nested / 'run.json' / 'keep.txt').read_text() == 'keep me'
    other = tmp_path / 'c'
    artifacts.write(family, other)
    (other / 'run.json').write_text('{"run_id": "census-2026"}')
    with pytest.raises(ValueError, match='does not name a device-linkage run'):
        artifacts.write(family, other)
    empty = tmp_path / 'd'
    empty.mkdir()
    artifacts.write(family, empty)
    assert sorted(p.name for p in empty.iterdir()) == sorted(family)


@pytest.mark.parametrize('nct', ['error', 'NCT123', 'nct99000001', 'NCT99000001x', ''])
def test_a_study_answer_needs_an_nct_shaped_id(nct: str) -> None:
    assert not registry.study_answer({'protocolSection': {'identificationModule': {'nctId': nct}}})
    assert registry.study_answer({'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}})



def test_a_retrieved_document_whose_bytes_are_not_held_stops_the_build(tmp_path: Path) -> None:
    pdf = b'%PDF-1.4 a summary'
    digest = acquire.sha256(pdf)
    store = tmp_path / 'documents' / 'store'
    store.mkdir(parents=True)
    line = {'document_id': 'doc:K990001:x', 'submission_number': 'K990001', 'url': f'{F.DOCS}/pdf99/K990001.pdf',
            'official_label': 'Summary', 'discovery': 'fda_database_page', 'scope': 'this_submission', 'origin': 'fda_fetch',
            'retrieved_at': 't', 'http_status': 200, 'bytes': len(pdf), 'sha256': digest, 'outcome': 'retrieved', 'attempts': 1}
    def build() -> list[dict[str, Any]]:
        return documents.build([line], {'K990001': '510k'}, tmp_path, pdftext.extraction_version())
    for content, why in ((None, 'not in the document store'), (pdf[:-3], 'bytes, '),
                         (b'%PDF-1.4 a summarx', 'do not hash to the recorded digest')):
        if content is not None:
            (store / f'{digest}.pdf').write_bytes(content)
        with pytest.raises(ValueError, match=why):
            build()
    (store / f'{digest}.pdf').write_bytes(pdf)
    assert build()[0]['availability'] == 'retrieved'


@pytest.mark.parametrize('body', [{'error': 'x'}, {'apiVersion': '2.0.5'}, {'dataTimestamp': '2026-11-13T10:00:06'},
                                  {'apiVersion': '', 'dataTimestamp': 't'}])
def test_a_registry_version_answer_without_its_stamp_is_asked_again(tmp_path: Path, body: Any) -> None:
    log = acquire.ResponseLog(tmp_path / 'registry.jsonl')
    registry.fetch_version(log, lambda u, p: (200, {}, json.dumps(body).encode(), None), F.no_wait(), '2026-11-14')
    assert log.get('ctgov/version/2026-11-14')['status'] == acquire.UNUSABLE_200
    assert log.settled('ctgov/version/2026-11-14') is None
    assert registry.version_answer({'apiVersion': '2.0.5', 'dataTimestamp': '2026-11-13T10:00:06'})


def test_the_build_stamps_records_from_the_latest_answered_version(tmp_path: Path, acquired: dict[str, Any]) -> None:
    a = copy.deepcopy(acquired)
    records, _ = a['frozen']['registry']
    good = next(r for k, r in records.items() if k.startswith('ctgov/version/'))
    records['ctgov/version/2099-01-01'] = {**good, 'key': 'ctgov/version/2099-01-01', 'fetched_at': '2099-01-01T00:00:00+00:00',
                                           'body': {'error': 'x'}}
    family = F.build_family(tmp_path, a)
    stamped = {x['source_version'].get('registry_api_version') for x in family['links.json']['links']
               if x['evidence']['source_kind'] == 'registry_record'}
    assert stamped == {good['body']['apiVersion']}



def test_each_registry_record_carries_the_version_stamp_of_its_own_snapshot(tmp_path: Path, acquired: dict[str, Any]) -> None:
    """Acquisition resumed after the registry's daily refresh: records keep the stamp current
    when they were fetched, and the run as a whole names the latest."""
    a = copy.deepcopy(acquired)
    records, _ = a['frozen']['registry']
    good = next(r for k, r in records.items() if k.startswith('ctgov/version/'))
    later = {'apiVersion': '9.9.9', 'dataTimestamp': '2099-01-01T00:00:00'}
    records['ctgov/version/2099-01-01'] = {**good, 'key': 'ctgov/version/2099-01-01',
                                           'fetched_at': '2099-01-01T00:00:00+00:00', 'body': later}
    family = F.build_family(tmp_path, a)
    stamped = {(x['source_version'].get('registry_api_version'), x['source_version'].get('registry_data_timestamp'))
               for x in family['links.json']['links'] if x['evidence']['source_kind'] == 'registry_record'}
    assert stamped == {(good['body']['apiVersion'], good['body']['dataTimestamp'])}
    assert family['run.json']['sources']['registry']['registry_api_version'] == '9.9.9'


@pytest.mark.parametrize('results', [[{}], [{'k_number': ''}], [{'k_number': 'K990001'}, {'device_name': 'x'}], ['K990001']])
def test_an_openfda_answer_with_an_entry_without_its_number_is_no_answer(tmp_path: Path, results: list[Any]) -> None:
    assert not acquire.openfda_answer({'results': results})
    log = acquire.ResponseLog(tmp_path / 'openfda.jsonl')
    acquire.fetch_openfda(['K990001'], log, lambda u, p: (200, {}, json.dumps({'results': results}).encode(), None),
                          F.no_wait())
    key = acquire.openfda_requests(['K990001'])[0]['key']
    assert log.get(key)['status'] == acquire.UNUSABLE_200 and log.settled(key) is None
    assert acquire.openfda_answer({'results': [{'k_number': 'K990001'}, {'pma_number': 'P990007'}]})


def test_a_retrieved_document_whose_stored_file_is_damaged_is_fetched_again(tmp_path: Path) -> None:
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    plan = [{'document_id': acquire.document_id('K990001', f'{F.DOCS}/pdf99/K990001.pdf'), 'submission_number': 'K990001',
             'url': f'{F.DOCS}/pdf99/K990001.pdf', 'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}]
    calls: list[str] = []
    assert acquire.fetch_documents(plan, manifest, store, F.stub_get(calls), F.no_wait())['retrieved'] == 1
    line = manifest.latest()[plan[0]['document_id']]
    stored = store / f"{line['sha256']}.pdf"
    assert acquire.fetch_documents(plan, manifest, store, F.stub_get(calls), F.no_wait())['already_settled'] == 1
    for damage in (lambda: stored.write_bytes(stored.read_bytes()[:-1]), stored.unlink):
        damage()
        assert acquire.fetch_documents(plan, manifest, store, F.stub_get(calls), F.no_wait())['retrieved'] == 1
        assert acquire.store_problem(store, line['sha256'], line['bytes']) is None
    assert len(calls) == 3



@pytest.mark.parametrize('results', [[{'pma_number': 'P990001'}], [{'k_number': 'error'}], [{'k_number': 'K990009'}]])
def test_an_openfda_entry_must_carry_a_number_the_request_asked_for(tmp_path: Path, results: list[Any]) -> None:
    q = acquire.openfda_requests(['K990001', 'K990002'])[0]
    assert not acquire.openfda_answer({'results': results}, q)
    log = acquire.ResponseLog(tmp_path / 'openfda.jsonl')
    acquire.fetch_openfda(['K990001', 'K990002'], log, lambda u, p: (200, {}, json.dumps({'results': results}).encode(), None),
                          F.no_wait())
    assert log.get(q['key'])['status'] == acquire.UNUSABLE_200
    # the PMA endpoint answers under pma_number, for the base number of a supplement
    pma = acquire.openfda_requests(['P990007/S002'])[0]
    assert acquire.openfda_answer({'results': [{'pma_number': 'P990007', 'supplement_number': 'S002'}]}, pma)
    assert not acquire.openfda_answer({'results': [{'k_number': 'P990007'}]}, pma)
    assert acquire.openfda_answer({'results': [{'k_number': 'K990002'}]}, q)


@pytest.mark.parametrize('body', [{'totalCount': -1, 'studies': []}, {'totalCount': 0, 'studies': [
    {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}]}])
def test_a_search_count_below_the_studies_returned_is_no_answer(body: dict[str, Any]) -> None:
    assert not registry.search_answer(body)


def test_repairing_a_stored_file_counts_only_the_net_bytes_against_the_budget(tmp_path: Path) -> None:
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    urls = [f'{F.DOCS}/pdf99/K990001.pdf', f'{F.DOCS}/pdf99/K990002.pdf']
    plan = [{'document_id': acquire.document_id(u.rsplit('/', 1)[1][:7], u), 'submission_number': u.rsplit('/', 1)[1][:7],
             'url': u, 'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'} for u in urls]
    acquire.fetch_documents(plan[:1], manifest, store, F.stub_get(), F.no_wait())
    stored = store / f"{manifest.latest()[plan[0]['document_id']]['sha256']}.pdf"
    size = stored.stat().st_size
    stored.write_bytes(b'X' * size)                   # same size, damaged
    second = len(F.stub_get()(urls[1], None)[2])
    # a budget that fits the two files once: the repair adds nothing, so the second still fits
    out = acquire.fetch_documents(plan, manifest, store, F.stub_get(), F.no_wait(), store_budget_bytes=size + second)
    assert (out['retrieved'], out['skipped_disk_budget']) == (2, 0)



def test_a_settled_answer_logged_before_its_shape_check_is_asked_again(tmp_path: Path) -> None:
    """A cache made under older rules: a 200 the search check would now refuse is not reused."""
    log = acquire.ResponseLog(tmp_path / 'registry.jsonl')
    q = {'kind': 'name', 'string': 'Quanta', 'key': 'ctgov/name/x', 'params': {'query.term': 'Quanta'}}
    log.put({'key': q['key'], 'params': q['params'], 'status': 200, 'body': {'totalCount': 1, 'studies': [{}]},
             'fetched_at': 't'})
    good = {'totalCount': 1, 'studies': [{'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}]}
    calls: list[str] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(url)
        return 200, {}, json.dumps(good).encode(), None
    registry.fetch_searches([q], log, get, F.no_wait())
    assert len(calls) >= 1 and acquire.ResponseLog(tmp_path / 'registry.jsonl').get(q['key'])['body'] == good
    # an answer that passes is reused, and a settled 404 is not asked again
    calls.clear()
    registry.fetch_searches([q], log, get, F.no_wait())
    log.put({'key': 'k404', 'params': {}, 'status': 404, 'body': None, 'fetched_at': 't'})
    acquire.logged_json(log, get, F.no_wait(), 'k404', 'https://example.invalid/x', valid=registry.search_answer)
    assert calls == []



def test_a_citation_scan_with_unread_pages_is_not_a_completed_scope() -> None:
    """Investigator's decision of 2026-11-14: an unread page could hold the identifier, so a scan
    that found nothing on the pages it read, with pages it could not read, is incomplete."""
    def doc(n_pages: int, with_text: int) -> dict[str, Any]:
        return {'document_id': 'doc:K990001:x', 'origin': 'fda_fetch', 'scope': 'this_submission', 'availability': 'retrieved',
                'url': f'{F.DOCS}/pdf99/K990001.pdf', 'http_status': 200, 'document_type': '510k_summary',
                'demographic_summary_candidate': True, 'demographic_summary_reason': 'a 510(k) summary',
                'text': {'status': 'partial' if with_text < n_pages else 'text', 'n_pages': n_pages,
                         'n_pages_with_text': with_text}, 'identifiers': [], 'study_description': None}
    partial = coverage.nct_citation_scan([doc(10, 9)], [])
    assert partial['state'] == 'search_incomplete_or_blocked'
    assert partial['detail']['n_pages_without_usable_text'] == 1 and 'did not cover every page' in partial['reason']
    assert coverage.nct_citation_scan([doc(10, 10)], [])['state'] == 'no_candidate_in_completed_scope'
    # a link found on the pages read still stands
    link = {'automated_validation': 'identifier_confirmed', 'human_review': 'unreviewed', 'target': {'id': 'NCT99000001'},
            'evidence': {'source_id': 'doc:K990001:x'}}
    assert coverage.nct_citation_scan([doc(10, 9)], [link])['state'] == 'verified_link'


@pytest.mark.parametrize('body', [b'', b'<html>proxy error</html>', b'{"studies": [', b'null'])
def test_a_200_without_a_usable_body_is_no_answer_and_is_asked_again(tmp_path: Path, body: bytes) -> None:
    """An empty, cut-off, non-JSON or null body under HTTP 200 is logged, not settled, and reads as
    no answer, so a route it belongs to stays incomplete."""
    log = acquire.ResponseLog(tmp_path / 'responses.jsonl')
    calls: list[str] = []
    def get(url: str, params: dict[str, Any] | None) -> acquire.Response:
        calls.append(url)
        return (200, {'content-type': 'application/json'}, body, None)
    rec = acquire.logged_json(log, get, F.no_wait(), 'k', 'https://example.invalid/x')
    first = len(calls)
    assert rec['status'] == acquire.UNUSABLE_200 and rec['http_status'] == 200 and rec['body'] is None and rec['error']
    assert log.settled('k') is None
    acquire.logged_json(log, get, F.no_wait(), 'k', 'https://example.invalid/x')
    assert len(calls) == 2 * first, 'asked again on the next run'
    # a number search whose batch answer is such a record is incomplete, never "no record found"
    batch_key = registry._key('ctgov/number_batch', registry._batch_params(registry.number_batches(['K990001'])[0]))
    out = registry.number_search_results({batch_key: {**rec, 'key': batch_key}}, ['K990001'])['K990001']
    assert out['state'] == 'incomplete'


def test_a_registry_statement_must_quote_the_study_it_names(tmp_path: Path, acquired: dict[str, Any]) -> None:
    body = acquired['frozen']['registry'][0]['ctgov/study/NCT99000002']['body']
    title = body['protocolSection']['identificationModule']['briefTitle']
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', 'NCT99000001', 'registry_record', 'NCT99000002', 'protocolSection.identificationModule.briefTitle',
         title, 'related_validation_study', '2026-11-13', 'the record cited is another study']])
    with pytest.raises(ValueError, match=r'the registry record cited \(NCT99000002\) is not the statement.s study \(NCT99000001\)'):
        F.build_family(tmp_path, acquired, curated)


def test_a_longer_identifier_that_begins_with_the_number_is_not_located() -> None:
    def where(text: str, number: str) -> list[Any]:
        return registry.locate_identifier({'protocolSection': {'descriptionModule': {'briefSummary': text}}}, number)
    assert where('model K990001ABC', 'K990001') == []
    assert where('model K9900012', 'K990001') == []
    # a supplement number with a fourth digit is not the supplement, nor a citation of the original:
    # the base does not stand alone there
    assert where('approved as P990007/S0029', 'P990007/S002') == []
    assert [f['supplement_cited'] for f in where('approved as P990007/S002.', 'P990007/S002')] == [True]


def test_a_documentary_statement_may_not_cite_a_parent_pma_document(tmp_path: Path, acquired: dict[str, Any]) -> None:
    """A document of the original PMA, filed under the supplement it was planned for, supports
    family-level evidence only; a statement about the supplement cannot cite it."""
    docs = documents.build(acquired['manifest'].lines, {sn: s['pathway'] for sn, s in acquired['subs'].items()},
                           acquired['cache'], pdftext.extraction_version())
    parents = [d for d in docs if d.get('scope') == 'parent_submission']
    if not parents:
        pytest.skip('the fixture plans no parent PMA document')
    parent = parents[0]
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        [parent['submission_number'], 'NCT99000002', 'fda_document', parent['document_id'], '1', 'any words',
         'related_validation_study', '2026-11-13', 'cites the original approval document']])
    with pytest.raises(ValueError, match=r'\(parent_submission\), not of .* itself'):
        F.build_family(tmp_path, acquired, curated)


def test_an_original_pma_is_not_located_in_a_supplement_number() -> None:
    def where(text: str) -> list[Any]:
        return registry.locate_identifier({'protocolSection': {'descriptionModule': {'briefSummary': text}}}, 'P990007')
    assert where('approved as P990007/S002') == []
    assert where('approved as P990007 S002') == []
    assert [f['supplement_cited'] for f in where('approved as P990007.')] == [None]


def _blank_pdf() -> bytes:
    import io

    import pypdf
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_an_extraction_records_the_sha256_of_the_bytes_it_parsed(tmp_path: Path) -> None:
    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(_blank_pdf())
    out = pdftext.extract(pdf)
    assert out['source_sha256'] == acquire.sha256(pdf.read_bytes()) and out['n_pages'] == 1
    gone = pdftext.extract(tmp_path / 'missing.pdf')
    assert (gone['status'], gone['source_sha256']) == ('failed', None)


def _held_document(tmp_path: Path, pdf: bytes) -> tuple[dict[str, Any], str]:
    digest = acquire.sha256(pdf)
    store = tmp_path / 'documents' / 'store'
    store.mkdir(parents=True, exist_ok=True)
    (store / f'{digest}.pdf').write_bytes(pdf)
    line = {'document_id': 'doc:K990001:x', 'submission_number': 'K990001', 'url': f'{F.DOCS}/pdf99/K990001.pdf',
            'official_label': 'Summary', 'discovery': 'fda_database_page', 'scope': 'this_submission', 'origin': 'fda_fetch',
            'retrieved_at': 't', 'http_status': 200, 'bytes': len(pdf), 'sha256': digest, 'outcome': 'retrieved', 'attempts': 1}
    return line, digest


@pytest.mark.parametrize('bound_to', ['this', None, 'another'])
def test_text_not_recorded_as_parsed_from_the_documents_bytes_is_not_used(tmp_path: Path, bound_to: str | None) -> None:
    line, digest = _held_document(tmp_path, b'%PDF-1.4 a summary')
    F.write_text(tmp_path, digest, ['510(k) Summary. The study NCT99000001 enrolled 40 patients at 3 sites.'])
    path = build_device_links.text_path(tmp_path, digest, pdftext.extraction_version())
    body = json.loads(gzip.decompress(path.read_bytes()))
    if bound_to is None:
        body.pop('source_sha256')
    elif bound_to == 'another':
        body['source_sha256'] = acquire.sha256(b'another file')
    path.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))
    doc = documents.build([line], {'K990001': '510k'}, tmp_path, pdftext.extraction_version())[0]
    if bound_to == 'this':
        assert doc['text']['status'] == 'text' and [i['identifier'] for i in doc['identifiers']] == ['NCT99000001']
    else:
        assert doc['text']['status'] == 'not_extracted' and doc['identifiers'] == []
        assert 'does not record that it parsed these bytes' in doc['text']['error']


def test_the_text_stage_skips_a_damaged_file_and_redoes_text_not_bound_to_its_bytes(tmp_path: Path) -> None:
    import argparse
    pdf = _blank_pdf()
    line, digest = _held_document(tmp_path, pdf)
    acquire.Manifest(tmp_path / 'documents' / 'manifest.jsonl').append(line)
    stored = tmp_path / 'documents' / 'store' / f'{digest}.pdf'
    target = build_device_links.text_path(tmp_path, digest, pdftext.extraction_version())
    args = argparse.Namespace(cache=tmp_path, workers=1)

    def cached() -> dict[str, Any]:
        return json.loads(gzip.decompress(target.read_bytes()))
    stored.write_bytes(pdf[:-5])                    # damaged: nothing is extracted or cached
    build_device_links.stage_text(args, {})
    assert not target.exists()
    stored.write_bytes(pdf)                         # repaired: extracted from these bytes
    build_device_links.stage_text(args, {})
    assert cached()['source_sha256'] == digest and cached()['n_pages'] == 1
    stale = {**cached(), 'n_pages': 99}
    stale.pop('source_sha256')                      # made before extractions recorded their bytes
    target.write_bytes(gzip.compress(json.dumps(stale).encode(), mtime=0))
    build_device_links.stage_text(args, {})
    assert cached()['source_sha256'] == digest and cached()['n_pages'] == 1


def test_no_reader_uses_cached_text_not_bound_to_the_documents_bytes(tmp_path: Path, acquired: dict[str, Any]) -> None:
    """A documentary statement is checked against the document's cached text; text that does not
    record parsing that document's bytes confirms nothing, and the document reads not extracted."""
    import shutil
    a = copy.deepcopy(acquired)
    a['cache'] = tmp_path / 'cache'
    shutil.copytree(acquired['cache'], a['cache'])
    version = pdftext.extraction_version()
    docs = documents.build(a['manifest'].lines, {sn: s['pathway'] for sn, s in a['subs'].items()}, a['cache'], version)
    summary = next(d for d in docs if d['url'].endswith('reviews/DEN990005.pdf'))
    quote = 'A retrospective study of 300 patients from two hospitals was conducted.'
    curated = tmp_path / 'curated'
    F.write_curated(curated, documentary_statements=[
        ['DEN990005', 'NCT99000002', 'fda_document', summary['document_id'], '2', quote, 'related_validation_study',
         '2026-11-13', 'the summary names the two hospitals of this registered study']])
    assert documents.read_text(a['cache'], summary['sha256'], version) is not None
    path = build_device_links.text_path(a['cache'], summary['sha256'], version)
    body = json.loads(gzip.decompress(path.read_bytes()))
    body['source_sha256'] = acquire.sha256(b'another file')
    path.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))
    assert documents.read_text(a['cache'], summary['sha256'], version) is None
    family = F.build_family(tmp_path / 'out', a, curated)
    stated = next(x for x in family['links.json']['links'] if x['method'] == 'documentary_statement')
    assert stated['automated_validation'] == 'not_validated'
    held = next(d for d in family['documents.json']['documents'] if d['document_id'] == summary['document_id'])
    assert held['text']['status'] == 'not_extracted'


@pytest.mark.parametrize('supplement, ok', [(None, True), ('', True), ('S019', True), ('S1000', True),
                                            (True, False), (19, False), ('19', False), ('S19', False), ('s019', False)])
def test_a_pma_answer_needs_well_formed_supplement_numbers(supplement: Any, ok: bool) -> None:
    q = acquire.openfda_requests(['P990007/S019'])[0]
    entry = {'pma_number': 'P990007'} if supplement is None else {'pma_number': 'P990007', 'supplement_number': supplement}
    body = {'results': [{'pma_number': 'P990007', 'supplement_number': ''}, entry]}
    assert acquire.openfda_answer(body, q) is ok
    found = acquire.openfda_by_submission({q['key']: {'key': q['key'], 'status': 200, 'params': q['params'], 'body': body,
                                                     'fetched_at': 't'}}, ['P990007/S019'])['P990007/S019']
    # A malformed answer is no answer ("error"), never "openFDA holds no record".
    assert found['state'] == ('found' if supplement == 'S019' else 'no_record' if ok else 'error')


def test_the_build_applies_the_search_answer_check_to_frozen_answers() -> None:
    """Archives frozen before search answers were checked are read with the same check."""
    q = {'kind': 'name', 'string': 'Quanta', 'key': 'ctgov/name/x', 'params': {}}
    unreadable = {'totalCount': 1, 'studies': [{}]}
    records = {'ctgov/name/x': {'status': 200, 'body': unreadable, 'fetched_at': 't'}}
    state = registry.search_state(records, [q])
    assert (state['n_unusable'], state['n_uncounted'], state['n_truncated']) == (1, 0, 0)
    assert registry.studies_from(records, [q]) == {}
    cell = coverage.registry_name_organisation_search({**state, 'n_name_queries': 1}, [])
    assert cell['state'] == 'search_incomplete_or_blocked' and 'study without its own id' in cell['reason']
    # a full-page follow-up that cannot be read is not preferred to the first answer
    good = {'totalCount': 0, 'studies': []}
    both = {'ctgov/name/x': {'status': 200, 'body': good, 'fetched_at': 't'},
            registry.full_variant(q)['key']: {'status': 200, 'body': unreadable, 'fetched_at': 't'}}
    assert registry.answer(both, q)['body'] == good
    # the number searches: a batch or a single-number answer that cannot be read is incomplete
    chunk = registry.number_batches(['K990001'])[0]
    batch_key = registry._key('ctgov/number_batch', registry._batch_params(chunk))
    out = registry.number_search_results({batch_key: {'status': 200, 'body': {'totalCount': 1, 'studies': [{}]},
                                                      'fetched_at': 't'}}, ['K990001'])['K990001']
    assert out['state'] == 'incomplete' and 'could not be read in full' in out['reason']
    single = _number_search({'totalCount': 1, 'studies': [{'protocolSection': {}}]})
    assert single['state'] == 'incomplete' and 'could not be read in full' in single['reason']


def test_an_openfda_200_with_an_empty_result_list_is_no_answer(tmp_path: Path) -> None:
    """openFDA answers a search with no match with HTTP 404; a 200 listing nothing is malformed."""
    q = acquire.openfda_requests(['K990001'])[0]
    assert acquire.openfda_answer({'results': []}, q) is False
    assert acquire.openfda_answer({'results': []}) is False
    found = acquire.openfda_by_submission({q['key']: {'key': q['key'], 'status': 200, 'params': q['params'],
                                                     'body': {'results': []}, 'fetched_at': 't'}}, ['K990001'])
    assert found['K990001']['state'] == 'error'          # not "openFDA holds no record"
    log = acquire.ResponseLog(tmp_path / 'log.jsonl')
    acquire.fetch_openfda(['K990001'], log, lambda u, p: (200, {}, b'{"results": []}', None), F.no_wait())
    assert log.get(q['key'])['status'] == acquire.UNUSABLE_200 and log.settled(q['key']) is None


@pytest.mark.parametrize('text', ['cleared as P990007-S002', 'P990007 – S002', 'P990007—S002', 'P990007 ‐ S002'])
def test_a_supplement_joined_by_a_hyphen_or_dash_is_the_supplement_not_the_original(text: str) -> None:
    record = {'protocolSection': {'descriptionModule': {'briefSummary': text}}}
    assert registry.locate_identifier(record, 'P990007') == []
    assert [h['supplement_cited'] for h in registry.locate_identifier(record, 'P990007/S002')] == [True]
    assert registry.locate_identifier(record, 'P990007/S003') == []
    # a hyphen before a word is not a supplement: the original stands alone there
    plain = {'protocolSection': {'descriptionModule': {'briefSummary': 'P990007 - related device'}}}
    assert len(registry.locate_identifier(plain, 'P990007')) == 1


def test_cached_documents_of_submissions_not_in_the_inventory_are_left_out(tmp_path: Path) -> None:
    line, _ = _held_document(tmp_path, b'%PDF-1.4 a summary')
    gone = {**line, 'document_id': 'doc:K990009:x', 'submission_number': 'K990009'}
    docs = documents.build([line, gone], {'K990001': '510k'}, tmp_path, pdftext.extraction_version())
    assert [d['submission_number'] for d in docs] == ['K990001']
    assert documents.outside_inventory([line, gone], {'K990001': {}}) == 1


def test_a_record_fetched_before_any_version_answer_is_not_stamped_with_a_later_snapshot(
        tmp_path: Path, acquired: dict[str, Any]) -> None:
    """The /version request failed while studies were fetched, and a later run fetched it: the later
    snapshot may not hold those records, so their stamp is unknown, with the reason."""
    a = copy.deepcopy(acquired)
    records, _ = a['frozen']['registry']
    for k in [k for k in records if k.startswith('ctgov/version/')]:
        records[k] = {**records[k], 'fetched_at': '2099-01-01T00:00:00+00:00'}
    family = F.build_family(tmp_path, a)
    versions = [x['source_version'] for x in family['links.json']['links'] if x['evidence']['source_kind'] == 'registry_record']
    assert versions and all(v.get('registry_api_version') is None and v.get('registry_data_timestamp') is None
                            for v in versions)
    assert {v.get('registry_version_absent_reason') for v in versions} == \
        {'no registry version answer was fetched at or before this record'}
    assert family['run.json']['sources']['registry']['registry_api_version']        # the run names the latest


def test_a_text_worker_failure_is_extracted_again_on_the_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout or crash of the isolated worker is cached as a failed extraction (the build reads
    the document as failed) but is not the extractor's verdict on the file: the next run tries again."""
    import argparse
    import subprocess as sp
    pdf = _blank_pdf()
    line, digest = _held_document(tmp_path, pdf)
    acquire.Manifest(tmp_path / 'documents' / 'manifest.jsonl').append(line)
    target = build_device_links.text_path(tmp_path, digest, pdftext.extraction_version())
    args = argparse.Namespace(cache=tmp_path, workers=1)
    real_run = sp.run

    def crash(*a: Any, **k: Any) -> Any:
        raise sp.TimeoutExpired(cmd='pdftext', timeout=build_device_links.TEXT_TIMEOUT_SECONDS)
    monkeypatch.setattr(build_device_links.subprocess, 'run', crash)
    build_device_links.stage_text(args, {})
    failed = json.loads(gzip.decompress(target.read_bytes()))
    assert (failed['status'], failed['source_sha256']) == ('failed', digest)
    doc = documents.build([line], {'K990001': '510k'}, tmp_path, pdftext.extraction_version())[0]
    assert doc['text']['status'] == 'failed'                    # read as failed until it is tried again
    monkeypatch.setattr(build_device_links.subprocess, 'run', real_run)
    build_device_links.stage_text(args, {})
    again = json.loads(gzip.decompress(target.read_bytes()))
    assert again['n_pages'] == 1 and 'worker_failure' not in again
    assert failed.get('worker_failure') is True


def test_a_store_at_its_budget_still_repairs_a_damaged_recorded_file(tmp_path: Path) -> None:
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    u = f'{F.DOCS}/pdf99/K990001.pdf'
    plan = [{'document_id': acquire.document_id('K990001', u), 'submission_number': 'K990001', 'url': u,
             'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}]
    acquire.fetch_documents(plan, manifest, store, F.stub_get(), F.no_wait())
    line = manifest.latest()[plan[0]['document_id']]
    stored = store / f"{line['sha256']}.pdf"
    good = stored.read_bytes()
    stored.write_bytes(b'X' * len(good))              # same size, damaged; the store is exactly at its budget
    calls: list[str] = []
    out = acquire.fetch_documents(plan, manifest, store, F.stub_get(calls), F.no_wait(), store_budget_bytes=len(good))
    assert (out['retrieved'], out['skipped_disk_budget'], len(calls)) == (1, 0, 1)
    assert stored.read_bytes() == good and acquire.store_problem(store, line['sha256'], line['bytes']) is None


def test_a_repair_that_finds_a_new_version_is_held_to_the_budget(tmp_path: Path) -> None:
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    u = f'{F.DOCS}/pdf99/K990001.pdf'
    plan = [{'document_id': acquire.document_id('K990001', u), 'submission_number': 'K990001', 'url': u,
             'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}]
    acquire.fetch_documents(plan, manifest, store, F.stub_get(), F.no_wait())
    line = manifest.latest()[plan[0]['document_id']]
    stored = store / f"{line['sha256']}.pdf"
    stored.write_bytes(b'X' * line['bytes'])          # damaged; the store is exactly at its budget
    new = b'%PDF-1.4 a revised summary, other bytes'

    def serves_new(url: str, params: Any) -> tuple[int, dict[str, Any], bytes, None]:
        return 200, {}, new, None
    out = acquire.fetch_documents(plan, manifest, store, serves_new, F.no_wait(), store_budget_bytes=line['bytes'])
    assert (out['retrieved'], out['skipped_disk_budget']) == (0, 1)
    assert not (store / f'{acquire.sha256(new)}.pdf').exists()
    skipped = manifest.latest()[plan[0]['document_id']]
    assert skipped['outcome'] == 'skipped_disk_budget' and skipped['http_status'] == 200 and 'new version' in skipped['error']
    # with room, the new version is stored
    out = acquire.fetch_documents(plan, manifest, store, serves_new, F.no_wait())
    assert out['retrieved'] == 1 and (store / f'{acquire.sha256(new)}.pdf').exists()


@pytest.mark.parametrize('module', [{'sponsorCollaboratorsModule': {'collaborators': [None]}},
                                    {'sponsorCollaboratorsModule': {'leadSponsor': 'Acme'}},
                                    {'armsInterventionsModule': {'interventions': ['a drug']}},
                                    {'statusModule': {'startDateStruct': '2020-01'}}])
def test_a_study_with_a_field_the_build_cannot_read_is_no_answer(module: dict[str, Any]) -> None:
    study = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}, **module}}
    assert not registry.study_answer(study)
    assert not registry.search_answer({'totalCount': 1, 'studies': [study]})
    # the build reads such a frozen answer as incomplete rather than crashing on it
    q = {'kind': 'name', 'string': 'Quanta', 'key': 'ctgov/name/x', 'params': {}}
    records = {'ctgov/name/x': {'status': 200, 'body': {'totalCount': 1, 'studies': [study]}, 'fetched_at': 't'}}
    assert registry.search_state(records, [q])['n_unusable'] == 1 and registry.studies_from(records, [q]) == {}
    good = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'},
                                'sponsorCollaboratorsModule': {'collaborators': [{'name': 'Acme'}]}}}
    assert registry.study_answer(good)


def test_a_failed_repair_attempt_does_not_make_a_recorded_document_new_to_the_budget(tmp_path: Path) -> None:
    manifest, store = acquire.Manifest(tmp_path / 'manifest.jsonl'), tmp_path / 'store'
    u = f'{F.DOCS}/pdf99/K990001.pdf'
    plan = [{'document_id': acquire.document_id('K990001', u), 'submission_number': 'K990001', 'url': u,
             'official_label': None, 'discovery': 'url_pattern', 'scope': 'this_submission'}]
    acquire.fetch_documents(plan, manifest, store, F.stub_get(), F.no_wait())
    line = manifest.latest()[plan[0]['document_id']]
    stored = store / f"{line['sha256']}.pdf"
    good = stored.read_bytes()
    stored.write_bytes(b'X' * len(good))              # damaged; the store is exactly at its budget
    out = acquire.fetch_documents(plan, manifest, store, lambda u, p: (503, {}, b'', None), F.no_wait(),
                                  store_budget_bytes=len(good))
    assert out['http_error'] == 1 and manifest.latest()[plan[0]['document_id']]['outcome'] == 'http_error'
    out = acquire.fetch_documents(plan, manifest, store, F.stub_get(), F.no_wait(), store_budget_bytes=len(good))
    assert (out['retrieved'], out['skipped_disk_budget']) == (1, 0) and stored.read_bytes() == good


def test_a_frozen_full_record_the_build_cannot_read_leaves_the_number_search_incomplete() -> None:
    chunk = registry.number_batches(['K990001'])[0]
    t = '2026-11-13T00:00:00+00:00'
    hit = {'protocolSection': {'identificationModule': {'nctId': 'NCT99000001'}}}
    records = {registry._key('ctgov/number_batch', registry._batch_params(chunk)): {'status': 200, 'body': {'totalCount': 1}, 'fetched_at': t},
               registry._key('ctgov/number', registry._single_params('K990001')): {'status': 200, 'fetched_at': t,
                                                                                   'body': {'totalCount': 1, 'studies': [hit]}},
               'ctgov/study_full/NCT99000001': {'status': 200, 'fetched_at': t, 'body': {'protocolSection': {
                   'identificationModule': {'nctId': 'NCT99000001'}, 'descriptionModule': {'briefSummary': 'cleared as K990001'},
                   'sponsorCollaboratorsModule': {'collaborators': [None]}}}}}
    out = registry.number_search_results(records, ['K990001'])['K990001']
    assert out['state'] == 'incomplete' and out['hits'] == [] and 'could not be read' in out['reason']


def test_a_document_citation_is_a_new_evidence_record_when_its_quoted_sentence_changes() -> None:
    """A review is keyed on the evidence record's id: a new version of the document that changes
    the sentence around the NCT id is new evidence, so an old review is not carried onto it."""
    doc = {'submission_number': 'K990001', 'scope': 'this_submission', 'document_type': '510k_summary',
           'document_id': 'doc:K990001:x', 'sha256': 'a' * 64, 'retrieved_at': 't', 'source_last_modified': None,
           'text': {'extraction_version': 'v'}}
    study = {'nct_id': 'NCT99000001', 'start_date': None, 'primary_completion_date': None, 'completion_date': None}

    def lid(sentence: str, sha: str = 'a' * 64) -> str:
        ident = {'identifier': 'NCT99000001', 'page': 2, 'sentence': sentence, 'raw_match': 'NCT99000001'}
        return links.from_document_citation({**doc, 'sha256': sha}, ident, 'resolves', study, '2026-01-01', {})['link_id']
    base = lid('The pivotal study NCT99000001 enrolled 300 patients.')
    assert lid('The pivotal  study\nNCT99000001 enrolled 300 patients.') == base       # spacing is not evidence
    assert lid('The pivotal study NCT99000001 enrolled 300 patients.', sha='b' * 64) == base   # same words, new bytes
    assert lid('A predicate study NCT99000001 is cited for background.') != base


@pytest.mark.parametrize('field', [{'identificationModule': {'nctId': 'NCT99000001', 'briefTitle': {'bad': 1}}},
                                   {'identificationModule': {'nctId': 'NCT99000001'},
                                    'sponsorCollaboratorsModule': {'collaborators': [{'name': {'bad': 1}}]}},
                                   {'identificationModule': {'nctId': 'NCT99000001'},
                                    'sponsorCollaboratorsModule': {'leadSponsor': {'name': 7}}},
                                   {'identificationModule': {'nctId': 'NCT99000001'},
                                    'designModule': {'enrollmentInfo': {'count': '40'}}},
                                   {'identificationModule': {'nctId': 'NCT99000001'},
                                    'armsInterventionsModule': {'interventions': [{'name': 'X', 'otherNames': [3]}]}}])
def test_a_study_whose_fields_have_the_wrong_type_is_no_answer(field: dict[str, Any]) -> None:
    study = {'protocolSection': field}
    assert not registry.study_answer(study)
    assert not registry.search_answer({'totalCount': 1, 'studies': [study]})
