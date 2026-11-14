#!/usr/bin/env python3
"""Cut a device-linkage build down to the files the site's AI Devices tab reads.

  python3 scripts/export_device_links_site.py BUILD_DIR OUT_DIR
  python3 scripts/export_device_links_site.py build/devices ../clinical-trial-populations/data/devices

A selection of an existing build (devices/artifacts.py, schema version 1). Every published count
is the build's: summary.json is copied with only its run id changed, and the per-row tallies this
script writes (evidence records by kind, documents held) are breakdowns that must add up to the
build's own numbers, or the export stops. It runs by hand; no workflow calls it. It makes no
network call and reads only BUILD_DIR.

Files written (site schema 1; each carries schema_version and run_id):

  run.json                 the manifest: run id, the build it came from, counting units,
                           vocabularies, source summaries, curated-file row counts, and the sha256
                           of every other file here
  summary.json             the build's summary, unchanged apart from the run id
  submissions.json         one slim row per submission
  families.json            family id -> name, grouping reason, status, members
  evidence.json            every evidence record except those rejected by rule
  evidence_set_aside.json  the records rejected by rule, in the same shape (the site loads this
                           file only when a reader asks for it)
  documents.json           per submission, its documents

Three things are written once per file instead of once per row, because the same text or the
same key names would otherwise repeat thousands of times (the export is 8.5 MB without this and
under 6 MB with it). Each file names the table it uses, so a reader needs no other file:

  * a coverage cell is [state, index into the file's `coverage_reasons`], in the order of the
    file's `coverage_routes`;
  * an evidence tally is a list of counts in the order of the file's `evidence_kinds`;
  * a record's `confidence_reasons` and `validation_reasons` are indexes into the file's
    `reasons`, and the registry version every record shares is the file's `registry_version`.

What stays with the build: organizations.json (the canonical name is copied onto each row),
studies.json (title, sponsor and status are copied onto each evidence record), coverage.json
(state and reason per route are copied onto each row; the per-route detail stays),
demographics.json and review_queue.json. The demographic file holds model outputs and is not
shown record by record; its counts are in summary.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

SITE_SCHEMA_VERSION = 1
BUILD_SCHEMA_VERSION = 1
SITE_FILES = ('summary.json', 'submissions.json', 'families.json', 'evidence.json', 'evidence_set_aside.json',
              'documents.json')
# What the per-row evidence tallies count. A record falls in exactly one kind (kind_of), so the
# kinds add up to the build's number of evidence records. The site shows these definitions.
EVIDENCE_KINDS = {
    'submission_identifier': 'submission-level: an identifier found in a retrieved source (automated validation '
                             'identifier_confirmed). The citation exists; what it means is not reviewed',
    'family_name_and_organisation': 'family-level candidate: the device name stands in a study title or intervention '
                                    'and the sponsor agrees with the manufacturer. Not specific to one submission',
    'family_name_only': 'family-level candidate: the device name stands in a study title or intervention and no '
                        'sponsor agrees. Not specific to one submission',
    'parent_submission': 'a record whose source is a listed supplement but whose evidence is the original '
                         'submission\'s (its document, or a registry record citing its number). Not specific to the '
                         'supplement',
    'set_aside_by_rule': 'kept for visibility and rejected by rule: the name returns 50 or more registry records and '
                         'no sponsor agrees, or the submission number is in no returned field',
    'other': 'any other record: a documentary statement entered by a person, or a cited identifier the registry '
             'does not hold',
}
# The build's summary metric that each kind must equal over all records. `other` and
# `parent_submission` have no metric of their own; the total over all kinds is checked instead.
KIND_METRICS = {'submission_identifier': 'evidence_records_identifier_confirmed',
                'family_name_and_organisation': 'evidence_records_name_and_organisation',
                'family_name_only': 'evidence_records_name_only',
                'set_aside_by_rule': 'evidence_records_rejected_by_rule'}
# Source summaries: named keys only. The build's manifest holds archive names and hashes of files
# that are not published; the site needs the windows, the registry's data timestamp and the
# extraction version. Nothing here is a URL.
SOURCE_KEYS = {
    'openfda': ('source', 'first_fetched_at', 'last_fetched_at', 'n_records'),
    'fda_database_pages': ('source', 'first_fetched_at', 'last_fetched_at', 'n_records'),
    'fda_documents': ('first_fetched_at', 'last_fetched_at', 'n_lines', 'sha256', 'max_requests_per_second_per_host'),
    'registry': ('source', 'registry', 'registry_api_version', 'registry_data_timestamp', 'first_fetched_at',
                 'last_fetched_at', 'n_records'),
    'text_extraction': ('extraction_version', 'ocr'),
    'demographic_extractions': ('file', 'n_documents', 'n_extractions'),
}


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    """Compact, key-sorted JSON with a final newline: the same inputs give the same bytes."""
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode()


def read_build(build: Path) -> dict[str, Any]:
    """The build's files, parsed, after checking each against the sha256 in its run.json.

    A build whose bytes differ from its manifest was edited, cut short or mixed with another
    build; exporting it would publish numbers no recorded run produced, so it is refused whole.
    """
    run_path = build / 'run.json'
    if not run_path.is_file():
        raise ValueError(f'{build}: no run.json; not a device-linkage build')
    run = json.loads(run_path.read_text(encoding='utf-8'))
    if run.get('schema_version') != BUILD_SCHEMA_VERSION:
        raise ValueError(f"run.json: build schema {run.get('schema_version')!r} is not one this export reads")
    listed = run.get('files')
    if not isinstance(listed, dict) or not listed:
        raise ValueError('run.json lists no files with their sha256')
    files: dict[str, Any] = {'run.json': run}
    for name, digest in sorted(listed.items()):
        path = build / name
        if not path.is_file():
            raise ValueError(f'{name}: listed in run.json and missing from the build')
        raw = path.read_bytes()
        if sha256_bytes(raw) != digest:
            raise ValueError(f'{name}: bytes do not match the sha256 in the build\'s run.json')
        files[name] = json.loads(raw)
        if files[name].get('run_id') != run['run_id']:
            raise ValueError(f'{name}: belongs to another run ({files[name].get("run_id")!r})')
    needed = {'summary.json', 'submissions.json', 'families.json', 'organizations.json', 'documents.json',
              'studies.json', 'links.json', 'coverage.json'}
    if needed - set(files):
        raise ValueError(f'the build lacks {sorted(needed - set(files))}')
    return files


def site_run_id(run: dict[str, Any]) -> str:
    """The build's run id with a digest of the build's file hashes appended.

    The build's own id is a digest of its INPUTS. Two builds at one engine commit from the same
    inputs are byte-identical and share that id, which is right; but the id alone does not say
    which bytes were exported, and a change to the build code between two runs of one day changes
    the files without changing the inputs. The appended digest covers every file the build's
    manifest lists, so two builds whose files differ never share a site run id.
    """
    digest = sha256_bytes(json.dumps(sorted(run['files'].items())).encode())
    return f"{run['run_id']}-{digest[:8]}"


def kind_of(link: dict[str, Any]) -> str:
    """The one tally a record is counted in (EVIDENCE_KINDS), read from fields the build set.

    Order matters and is the order of the checks below: a record rejected by rule is set aside
    whatever its method; identifier_confirmed exists only at submission level (devices/links.py
    refuses anything else); a name match always has a family as its source; a family-LEVEL
    record whose source is a submission is the supplement case, where the evidence belongs to
    the original submission. Everything else (a documentary statement, an identifier the
    registry does not hold) is `other`, so that no record is dropped from the tallies.
    """
    if link['automated_validation'] == 'rejected_by_rule':
        return 'set_aside_by_rule'
    if link['automated_validation'] == 'identifier_confirmed' and link['level'] == 'submission':
        return 'submission_identifier'
    if link['method'] == 'device_name_and_organisation_match':
        return 'family_name_and_organisation'
    if link['method'] == 'device_name_match':
        return 'family_name_only'
    if link['level'] == 'family' and link['source']['entity_type'] == 'submission':
        return 'parent_submission'
    return 'other'


def _tally(links: list[dict[str, Any]]) -> list[int]:
    """Counts in the order of EVIDENCE_KINDS (written to each file as `evidence_kinds`). A kind
    with no record is a counted 0, not an absence: every record was classified."""
    counts = Counter(kind_of(x) for x in links)
    return [counts.get(kind, 0) for kind in EVIDENCE_KINDS]


class _Table:
    """Strings written once: index(text) gives a text's position in `values`, in order of first
    use, so the same build always gives the same table."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._index: dict[str, int] = {}

    def index(self, text: str) -> int:
        if text not in self._index:
            self._index[text] = len(self.values)
            self.values.append(text)
        return self._index[text]


REGISTRY_VERSION_KEYS = ('registry', 'registry_api_version', 'registry_data_timestamp')


def slim_evidence(link: dict[str, Any], study: dict[str, Any] | None, reasons: _Table,
                  registry_version: dict[str, Any] | None) -> dict[str, Any]:
    """One evidence record for display. Every field of the record is kept as built except:

      * `target` loses its two constants (entity type, registry name); the NCT id stays;
      * `confidence_reasons` and `validation_reasons` are indexes into the file's `reasons`;
      * `source_version` loses the registry version when it is the one every record of the
        build shares (`registry_version`, written once in the file); a record with any other
        version keeps its own in full;
      * `timing.by_submission` keeps the submission, the FDA decision date and the relation, and
        drops the day-gap ranges, which are arithmetic on the dates beside them;
      * `study` is added from studies.json: brief title, lead sponsor, overall status and the
        registry's answer for the id. A study the build holds no facts for is given as absent
        fields (None), never as empty strings.
    """
    study = study or {}
    version = link['source_version']
    if registry_version and all(version.get(k) == registry_version[k] for k in REGISTRY_VERSION_KEYS):
        version = {k: v for k, v in version.items() if k not in REGISTRY_VERSION_KEYS}
    return {
        'link_id': link['link_id'], 'level': link['level'], 'source': link['source'],
        'target': {'id': link['target']['id']},
        'proposed_relationship': link['proposed_relationship'], 'method': link['method'],
        'evidence': link['evidence'], 'source_version': version,
        'confidence': link['confidence'], 'confidence_reasons': [reasons.index(r) for r in link['confidence_reasons']],
        'automated_validation': link['automated_validation'],
        'validation_reasons': [reasons.index(r) for r in link['validation_reasons']],
        'human_review': link['human_review'], 'reviewed_relationship': link['reviewed_relationship'],
        'review': link['review'],
        'timing': {**{k: v for k, v in link['timing'].items() if k != 'by_submission'},
                   'by_submission': [{'submission_number': t['submission_number'],
                                      'fda_decision_date': t['fda_decision_date'], 'relation': t['relation']}
                                     for t in link['timing']['by_submission']]},
        'study': {'brief_title': study.get('brief_title'), 'lead_sponsor': study.get('lead_sponsor'),
                  'overall_status': study.get('overall_status'), 'registry_state': study.get('registry_state')},
    }


def slim_document(d: dict[str, Any]) -> dict[str, Any]:
    """One document for display: what it is, where FDA serves it, which bytes are held and
    whether they could be read. The classification detail, the keyword findings and the version
    history stay with the build."""
    text = d.get('text') or {}
    return {
        'document_id': d['document_id'], 'document_type': d['document_type'], 'category': d['category'],
        'scope': d['scope'], 'url': d['url'], 'availability': d['availability'], 'sha256': d['sha256'],
        'bytes': d['bytes'], 'retrieved_at': d['retrieved_at'], 'official_label': d.get('official_label'),
        'text_status': text.get('status'), 'n_pages': text.get('n_pages'),
        'n_pages_with_text': text.get('n_pages_with_text'),
        'demographic_summary_candidate': d['demographic_summary_candidate'],
        'demographic_summary_reason': d['demographic_summary_reason'],
    }


def evidence_file(links: list[dict[str, Any]], study_of: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One evidence file: the records, the reasons table they index and the shared registry
    version. The version is shared only when every record that names a registry names the same
    one; otherwise none is hoisted and each record keeps its own."""
    named = {tuple(x['source_version'].get(k) for k in REGISTRY_VERSION_KEYS) for x in links
             if 'registry' in x['source_version']}
    registry_version = dict(zip(REGISTRY_VERSION_KEYS, named.pop())) if len(named) == 1 else None
    reasons = _Table()
    records = [slim_evidence(x, study_of.get(x['target']['id']), reasons, registry_version) for x in links]
    return {'registry_version': registry_version, 'reasons': reasons.values, 'records': records}


def _metric(summary: dict[str, Any], metric_id: str) -> int:
    for m in summary['metrics']:
        if m['id'] == metric_id:
            return m['numerator']
    raise ValueError(f'summary.json has no metric {metric_id!r}')


def select(build: dict[str, Any]) -> dict[str, Any]:
    """The site files, as objects, from a verified build. Raises when a tally written here does
    not add up to the build's own count: the tallies are breakdowns, never a second opinion."""
    run, summary = build['run.json'], build['summary.json']
    run_id = site_run_id(run)
    subs = build['submissions.json']['submissions']
    families = build['families.json']['families']
    org_name = {o['organization_id']: o['display_name'] for o in build['organizations.json']['organizations']}
    study_of = {s['nct_id']: s for s in build['studies.json']['studies']}
    coverage_of = {c['submission_number']: c for c in build['coverage.json']['submissions']}
    links = build['links.json']['links']

    by_source: dict[str, list[dict[str, Any]]] = {}
    for x in links:
        by_source.setdefault(x['source']['id'], []).append(x)
    docs_of: dict[str, list[dict[str, Any]]] = {}
    for d in build['documents.json']['documents']:
        docs_of.setdefault(d['submission_number'], []).append(d)
    family_of = {f['family_id']: f for f in families}

    route_order = list(run['vocabularies']['coverage_routes'])
    coverage_reasons = _Table()
    rows = []
    for s in subs:
        sn, fid = s['submission_number'], s['family_id']
        own, of_family = by_source.get(sn, []), by_source.get(fid, [])
        # The build counted both; a mismatch means this script and the build disagree on what
        # belongs to a submission, and neither number could then be shown beside the other.
        if len(own) != s['n_evidence_records'] or len(of_family) != s['n_family_evidence_records']:
            raise ValueError(f'{sn}: evidence records found ({len(own)}, {len(of_family)}) differ from the build\'s '
                             f"counts ({s['n_evidence_records']}, {s['n_family_evidence_records']})")
        cov = coverage_of[sn]
        if cov['overall']['state'] != s['coverage_overall']:
            raise ValueError(f'{sn}: coverage.json and submissions.json disagree on the overall state')
        cells = {c['route']: c for c in cov['routes']}
        if sorted(cells) != sorted(route_order) or len(cov['routes']) != len(route_order):
            raise ValueError(f'{sn}: coverage routes are not the routes the build\'s vocabulary lists')
        # Held = retrieved and the submission's own (a parent PMA's documents are not the
        # supplement's): the definition summary.json uses for submissions_with_document.
        held = [d for d in docs_of.get(sn, []) if d['availability'] == 'retrieved' and d['scope'] == 'this_submission']
        rows.append({
            'submission_number': sn, 'pathway': s['pathway'], 'decision_date': s['decision_date'],
            'decision_date_as_listed': s['decision_date_as_listed'],
            'device_name': s['device_name'], 'device_names_as_listed': s['device_names_as_listed'],
            'companies_as_listed': s['companies_as_listed'], 'panel': s['panel'], 'product_code': s['product_code'],
            'organization_id': s['organization_id'], 'organization_name': org_name[s['organization_id']],
            'family_id': fid, 'family_n_submissions': family_of[fid]['n_submissions'],
            'n_source_rows': s['n_source_rows'], 'source_row_numbers': [r['row_number'] for r in s['source_rows']],
            'duplicate_class': s['duplicate_class'], 'conflicts': s['conflicts'],
            'coverage_overall': cov['overall']['state'],
            # One [state, reason index] per route, in the order of `coverage_routes` below.
            'coverage': [[cells[r]['state'], coverage_reasons.index(cells[r]['reason'])] for r in route_order],
            # Own records and the family's, each record once: a family-level candidate is counted
            # on every member of the family because it is specific to none of them.
            'evidence_counts': _tally(own + of_family),
            'documents': {'n_held': len(held),
                          'n_population_type_readable': sum(1 for d in held if d['demographic_summary_candidate'])},
            'official_differs_from_list': s['official_differs_from_list'],
        })

    family_rows = {}
    for f in families:
        members = [x for sn in f['submission_numbers'] for x in by_source.get(sn, [])]
        family_rows[f['family_id']] = {
            'family_name': f['family_name'], 'grouping_reason': f['grouping_reason'], 'status': f['status'],
            'submission_numbers': f['submission_numbers'],
            'organization_names': [org_name[o] for o in f['organization_ids']],
            'decision_dates': f['decision_dates'],
            # Records whose source is the family, and records whose source is a member
            # submission, each once.
            'evidence_counts': _tally(by_source.get(f['family_id'], []) + members),
        }

    # ---- the tallies against the build's own counts
    totals = _tally(links)
    for kind, n in zip(EVIDENCE_KINDS, totals):
        metric_id = KIND_METRICS.get(kind)
        if metric_id and n != _metric(summary, metric_id):
            raise ValueError(f'{kind}: {n} records by this export\'s rule, {_metric(summary, metric_id)} '
                             f'in summary.json ({metric_id})')
    if sum(totals) != summary['denominators']['evidence_records']['n']:
        raise ValueError('the evidence kinds do not add up to the build\'s number of evidence records')
    for metric_id, key in (('submissions_with_document', 'n_held'),
                           ('submissions_with_substantive_readable_document', 'n_population_type_readable')):
        if sum(1 for r in rows if r['documents'][key]) != _metric(summary, metric_id):
            raise ValueError(f'documents.{key}: the rows do not reproduce summary.json ({metric_id})')

    set_aside = [x for x in links if x['automated_validation'] == 'rejected_by_rule']
    shown = [x for x in links if x['automated_validation'] != 'rejected_by_rule']
    stamp = {'schema_version': SITE_SCHEMA_VERSION, 'run_id': run_id}
    sources = run.get('sources') or {}
    inventory = (run.get('inputs') or {}).get('inventory') or {}
    files: dict[str, Any] = {
        # The build's summary with its run id replaced: no count is touched.
        'summary.json': {**summary, 'run_id': run_id},
        'submissions.json': {**stamp, 'evidence_kinds': list(EVIDENCE_KINDS), 'coverage_routes': route_order,
                             'coverage_reasons': coverage_reasons.values, 'submissions': rows},
        'families.json': {**stamp, 'evidence_kinds': list(EVIDENCE_KINDS), 'families': family_rows},
        'evidence.json': {**stamp, **evidence_file(shown, study_of)},
        'evidence_set_aside.json': {**stamp, **evidence_file(set_aside, study_of)},
        'documents.json': {**stamp, 'documents': {sn: [slim_document(d) for d in ds]
                                                  for sn, ds in sorted(docs_of.items())}},
    }
    files['run.json'] = {
        **stamp, 'build_run_id': run['run_id'], 'build_schema_version': run['schema_version'],
        'generated_at': run['generated_at'], 'producer': run['producer'], 'method_version': run['method_version'],
        'scheduled': False,
        'counting_units': run['counting_units'],
        'vocabularies': {**run['vocabularies'], 'evidence_kinds': list(EVIDENCE_KINDS),
                         'coverage_overall_precedence': build['coverage.json']['overall_precedence']},
        'evidence_kinds': EVIDENCE_KINDS,
        'coverage_routes': build['coverage.json']['routes'],
        'sources': {
            'inventory': {k: inventory.get(k) for k in ('file', 'n_rows', 'sha256')},
            **{name: ({k: sources[name].get(k) for k in keys} if sources.get(name) else None)
               for name, keys in SOURCE_KEYS.items()}},
        'curated': {name: meta.get('n_rows') for name, meta in sorted(((run.get('inputs') or {}).get('curated')
                                                                      or {}).items())},
        'reviews': {'n_rows': run['reviews']['n_rows'], 'n_applied': run['reviews']['n_applied'],
                    'n_orphaned': len(run['reviews']['orphaned_link_ids'])},
        'records': {'evidence.json': len(shown), 'evidence_set_aside.json': len(set_aside)},
        'build_files': run['files'],
    }
    return files


def export(build: Path, out: Path) -> dict[str, Any]:
    """Verify the build, select the site files and replace `out` with them as a whole.

    The files are staged in a temporary directory beside `out` and moved into place only when
    all of them are written, so a failure leaves `out` as it was. An existing `out` that holds
    anything but this family's files is refused: the directory is replaced, and a mistyped path
    must not delete another dataset.
    """
    files = select(read_build(build))
    names = set(SITE_FILES) | {'run.json'}
    if out.exists():
        strangers = sorted(p.name for p in out.iterdir() if p.name not in names)
        if not out.is_dir() or strangers:
            raise ValueError(f'{out} holds files that are not this export\'s ({strangers[:5]}); refusing to replace it')
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out.name + '.staging.', dir=out.parent))
    try:
        digests = {}
        for name in SITE_FILES:
            raw = dump(files[name])
            (stage / name).write_bytes(raw)
            digests[name] = sha256_bytes(raw)
        manifest = {**files['run.json'], 'files': digests}
        # Indented: the manifest is the one file a person reads.
        (stage / 'run.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                                        encoding='utf-8')
        old = None
        if out.exists():
            old = Path(tempfile.mkdtemp(prefix=out.name + '.replaced.', dir=out.parent))
            out.rename(old / out.name)
        try:
            stage.rename(out)
        except OSError:
            if old is not None:             # put the previous family back before reporting
                (old / out.name).rename(out)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('build', type=Path, help='a build written by scripts/build_device_links.py build')
    ap.add_argument('out', type=Path, help='the directory to replace, e.g. the site checkout\'s data/devices')
    args = ap.parse_args(argv)
    try:
        manifest = export(args.build, args.out)
    except ValueError as err:
        print(f'REFUSED: {err}', file=sys.stderr)
        return 1
    total = 0
    for name in sorted(p.name for p in args.out.iterdir()):
        size = (args.out / name).stat().st_size
        total += size
        print(f'{size:>10,}  {name}')
    print(f'{total:>10,}  total; wrote {args.out} ({manifest["run_id"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
