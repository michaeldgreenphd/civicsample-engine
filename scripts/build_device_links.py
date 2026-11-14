#!/usr/bin/env python3
"""Build the AI/ML device linkage artifacts by hand (never scheduled).

Two steps, kept apart so that a build is reproducible from frozen inputs:

  python3 scripts/build_device_links.py acquire --cache build/devices_cache [--stage NAME ...]
      Talks to FDA and ClinicalTrials.gov, at most two requests a second per host, and freezes
      every answer under the cache. Resumable: rerun it and it asks only what is not yet held.
      Stages, in order: local openfda pages documents text nct registry-numbers registry-names
      freeze (default: all).

  python3 scripts/build_device_links.py build --cache build/devices_cache --out build/devices
      Reads the inventory, the curated files under devices/ and the frozen cache. Makes no network
      call. Writes nothing unless the staged family passes devices/contract.py.

Options common to both: --inventory (the enriched list), --source-list (FDA's list as downloaded),
--local-documents (PDFs an earlier fetch left on disk), --site (a site checkout, for the pilot
extractions; read only). The cache holds downloaded files and must not be inside a repository.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices import (acquire, artifacts, contract, demographics, documents, inventory, links,  # noqa: E402
                     organizations, pdftext, registry)

STAGES = ('local', 'openfda', 'pages', 'documents', 'text', 'nct', 'registry-numbers', 'registry-names', 'freeze')
TEXT_TIMEOUT_SECONDS = 300


def log(msg: str) -> None:
    print(f'[{acquire.now()}] {msg}', flush=True)


def load_inventory(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = inventory.read_rows(args.inventory, inventory.ENRICHED_COLUMNS)
    return rows, inventory.submissions(rows, inventory.ENRICHED_COLUMNS)


def text_path(cache: Path, digest: str, version: str) -> Path:
    return cache / 'text' / f'{digest}.{version}.json.gz'


def stage_local(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    manifest = acquire.Manifest(args.cache / 'documents' / 'manifest.jsonl')
    url_of = {sn: s['source_rows'][0]['pdf_url'] for sn, s in subs.items()}
    n = acquire.recover_local(args.local_documents, url_of, manifest, args.cache / 'documents' / 'store')
    log(f'local: {n} documents recovered from {args.local_documents}')


def stage_openfda(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    out = acquire.fetch_openfda(list(subs), acquire.ResponseLog(args.cache / 'openfda' / 'responses.jsonl'),
                                acquire.requests_get(), acquire.Throttle())
    log(f'openfda: {out}')


def parent_numbers(subs: dict[str, dict[str, Any]]) -> list[str]:
    """The original PMA number of every listed supplement (its documents are fetched as context)."""
    return sorted({sn.split('/')[0] for sn in subs if '/' in sn} - set(subs))


def stage_pages(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    pages = acquire.ResponseLog(args.cache / 'fda_pages' / 'responses.jsonl')
    done = [0]

    def progress(sn: str) -> None:
        done[0] += 1
        if done[0] % 100 == 0:
            log(f'pages: {done[0]} fetched (last {sn})')
    out = acquire.fetch_database_pages(list(subs) + parent_numbers(subs), pages, args.cache / 'fda_pages' / 'html',
                                       acquire.requests_get(), acquire.Throttle(), progress)
    log(f'pages: {out}')


def full_plan(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pages = acquire.ResponseLog(args.cache / 'fda_pages' / 'responses.jsonl')
    plan: list[dict[str, Any]] = []
    for sn in subs:
        parent = pages.get(f"fda_page/{sn.split('/')[0]}") if '/' in sn else None
        plan += acquire.document_plan(sn, pages.get(f'fda_page/{sn}'), parent)
    return plan


def stage_documents(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    manifest = acquire.Manifest(args.cache / 'documents' / 'manifest.jsonl')
    plan = full_plan(args, subs)
    done = [0]

    def progress(line: dict[str, Any]) -> None:
        done[0] += 1
        if done[0] % 100 == 0:
            log(f"documents: {done[0]} attempted (last {line['submission_number']} {line['outcome']})")
    budget = int(args.store_budget_gb * 1024 ** 3) if args.store_budget_gb else None
    out = acquire.fetch_documents(plan, manifest, args.cache / 'documents' / 'store', acquire.requests_get(),
                                  acquire.Throttle(), progress, budget)
    log(f'documents: {len(plan)} planned; {out}')


def stage_text(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    manifest = acquire.Manifest(args.cache / 'documents' / 'manifest.jsonl')
    version = pdftext.extraction_version()
    digests = sorted({line['sha256'] for line in manifest.latest().values() if line['outcome'] == 'retrieved'})
    todo = [d for d in digests if not text_path(args.cache, d, version).exists()]
    log(f'text: {len(digests)} document versions, {len(todo)} to extract with {version}')

    def one(digest: str) -> str:
        target = text_path(args.cache, digest, version)
        # Isolated mode (-I): the PDF is untrusted input, and the worker imports nothing local.
        cmd = [sys.executable, '-I', str(ROOT / 'devices' / 'pdftext.py'),
               str(args.cache / 'documents' / 'store' / f'{digest}.pdf'), str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=TEXT_TIMEOUT_SECONDS)
            failure = None if proc.returncode == 0 and target.exists() else f'exit {proc.returncode}'
        except subprocess.TimeoutExpired:
            failure = f'timeout after {TEXT_TIMEOUT_SECONDS} s'
        if failure:
            # The failure is itself cached, as a failed extraction with its reason.
            pdftext_failure = {'extraction_version': version, 'status': 'failed', 'n_pages': None,
                               'n_pages_with_text': None, 'pages': [], 'page_errors': {}, 'error': failure}
            acquire.write_atomic(target, gzip.compress(json.dumps(pdftext_failure).encode(), mtime=0))
        return digest
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, _ in enumerate(pool.map(one, todo), start=1):
            if i % 100 == 0:
                log(f'text: {i} of {len(todo)} extracted')
    log('text: done')


def registry_log(args: argparse.Namespace) -> acquire.ResponseLog:
    return acquire.ResponseLog(args.cache / 'registry' / 'responses.jsonl')


def name_search_plan(subs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every distinct registry search the name-and-organisation route needs: the name queries of
    every listed device name, and one sponsor query per normalised manufacturer name."""
    queries: list[dict[str, Any]] = []
    for s in subs.values():
        for name in s['device_names_as_listed']:
            queries += registry.name_queries(name)
        for company in s['companies_as_listed']:
            key = organizations.normalise(company)['normalised']
            if key:
                queries.append(registry.sponsor_query(key))
    seen: set[str] = set()
    return [q for q in queries if not (q['key'] in seen or seen.add(q['key']))]


def stage_registry_numbers(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    get, throttle, rlog = acquire.requests_get(), acquire.Throttle(), registry_log(args)
    registry.fetch_version(rlog, get, throttle, acquire.now()[:10])
    log(f'registry-numbers: {registry.fetch_number_search(list(subs), rlog, get, throttle)}')


def stage_registry_names(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    get, throttle, rlog = acquire.requests_get(), acquire.Throttle(), registry_log(args)
    registry.fetch_version(rlog, get, throttle, acquire.now()[:10])
    plan = name_search_plan(subs)
    log(f'registry-names: {len(plan)} distinct searches planned')

    def progress(n: int) -> None:
        if n % 200 == 0:
            log(f'registry-names: {n} of {len(plan)}')
    log(f'registry-names: {registry.fetch_searches(plan, rlog, get, throttle, progress)}')


ARCHIVES = {'openfda': ('openfda/responses.jsonl', 'openFDA device/510k.json and device/pma.json'),
            'fda_pages': ('fda_pages/responses.jsonl', 'FDA 510(k), De Novo and PMA release-database pages'),
            'registry': ('registry/responses.jsonl', 'ClinicalTrials.gov API v2')}


def archive_path(cache: Path, name: str) -> Path:
    return (cache / ARCHIVES[name][0]).with_name('responses.frozen.jsonl.gz')


def statement_study_ids(path: Path) -> set[str]:
    """The registry records the curated statements need: each row's study, and the record a
    registry-record statement quotes (the build checks the quote against that record, so it must be
    held, not merely returned by some other search)."""
    rows = links.read_statements(path)
    return {r['nct_id'] for r in rows} | {r['source_id'] for r in rows if r['source_kind'] == 'registry_record'}


def stage_nct(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    """Look up every NCT id the document scan found, and every study the curated statements name.
    Runs after `text`."""
    manifest = acquire.Manifest(args.cache / 'documents' / 'manifest.jsonl')
    docs = documents.build(manifest.lines, {sn: s['pathway'] for sn, s in subs.items()}, args.cache,
                           pdftext.extraction_version())
    cited = {i['identifier'] for d in docs for i in d['identifiers'] if i['kind'] == 'nct'}
    stated = statement_study_ids(ROOT / 'devices' / 'documentary_statements.csv')
    ids = sorted(cited | stated)
    get, throttle, rlog = acquire.requests_get(), acquire.Throttle(), registry_log(args)
    registry.fetch_version(rlog, get, throttle, acquire.now()[:10])
    log(f'nct: {len(cited)} distinct ids cited in documents, {len(stated - cited)} more named by curated statements; '
        f'{registry.fetch_studies(ids, rlog, get, throttle)}')


def stage_freeze(args: argparse.Namespace, subs: dict[str, dict[str, Any]]) -> None:
    for name, (rel, source) in ARCHIVES.items():
        if (args.cache / rel).exists():
            meta = acquire.freeze(acquire.ResponseLog(args.cache / rel), archive_path(args.cache, name), source)
            log(f"freeze: {name}: {meta['n_records']} records, sha256 {meta['sha256_uncompressed'][:12]}")


def producer_paths(root: Path = ROOT) -> list[str]:
    """The producing files: every file of the devices package (code and the curated files it reads)
    and this script."""
    return sorted([str(p.relative_to(root)) for p in (root / 'devices').rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts] + ['scripts/build_device_links.py'])


def source_digest(root: Path = ROOT) -> str:
    """One sha256 over the producing files as they stand on disk, path by path. A clean checkout of
    a commit always gives the same value; any uncommitted change to those files gives another, so
    two builds whose code differed cannot share a run id."""
    h = hashlib.sha256()
    for rel in producer_paths(root):
        h.update(rel.encode() + b'\0' + (root / rel).read_bytes() + b'\0')
    return h.hexdigest()


def producer() -> dict[str, object]:
    """The commit that produced the build, whether the producing files differ from it, and a digest
    of those files as they were."""
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None
    dirty = bool(subprocess.run(['git', 'status', '--porcelain', '--', 'devices', 'scripts/build_device_links.py'],
                                cwd=ROOT, capture_output=True, text=True).stdout.strip()) if commit else None
    return {'repository': 'civicsample-engine', 'commit': commit, 'uncommitted_changes': dirty,
            'source_sha256': source_digest()}


def file_meta(path: Path) -> dict[str, Any]:
    return {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def inventory_meta(args: argparse.Namespace, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The inventory's hash, and how it compares, row by row, with FDA's list as downloaded. The
    enriched file is the one read (it adds two columns); a row that differs from the source list
    on a shared column is listed, not corrected."""
    meta: dict[str, Any] = {**file_meta(args.inventory), 'n_rows': len(rows), 'columns': inventory.ENRICHED_COLUMNS,
                            'source_list': None}
    if args.source_list and args.source_list.exists():
        src = inventory.read_rows(args.source_list, inventory.LIST_COLUMNS)
        differing = [{'row_number': a['row_number'], 'submission_number': b['values']['Submission Number'],
                      'columns': [c for c in inventory.LIST_COLUMNS if a['values'][c] != b['values'][c]]}
                     for a, b in zip(src, rows) if any(a['values'][c] != b['values'][c] for c in inventory.LIST_COLUMNS)]
        meta['source_list'] = {**file_meta(args.source_list), 'n_rows': len(src),
                               'same_row_count': len(src) == len(rows), 'rows_differing_on_list_columns': differing}
    return meta


def run_build(args: argparse.Namespace, rows: list[dict[str, Any]], subs: dict[str, dict[str, Any]]) -> int:
    curated_dir = ROOT / 'devices'
    paths = {name: curated_dir / name for name in ('organization_aliases.csv', 'families.csv', 'reviews.csv',
                                                   'documentary_statements.csv')}
    try:
        aliases = organizations.read_aliases(paths['organization_aliases.csv'])
        curated_families = inventory.read_curated_families(paths['families.csv'])
        reviews = links.read_reviews(paths['reviews.csv'])
        statements = links.read_statements(paths['documentary_statements.csv'])
    except ValueError as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    curated_meta = {name: {**file_meta(path), 'n_rows': n} for (name, path), n in zip(
        paths.items(), (len(aliases), len(curated_families), len(reviews), len(statements)))}
    frozen: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for name in ARCHIVES:
        path = archive_path(args.cache, name)
        # A source that was never acquired is absent, and its routes are reported as not searched.
        frozen[name] = acquire.read_archive(path) if path.exists() else ({}, None)
    manifest_path = args.cache / 'documents' / 'manifest.jsonl'
    manifest = acquire.Manifest(manifest_path)
    times = sorted(line['retrieved_at'] for line in manifest.lines if line['origin'] == 'fda_fetch')
    manifest_meta = {'file': 'documents/manifest.jsonl',
                     'sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path.exists() else None,
                     'n_lines': len(manifest.lines), 'first_fetched_at': times[0] if times else None,
                     'last_fetched_at': times[-1] if times else None, 'max_requests_per_second_per_host': 2}
    version = pdftext.extraction_version()

    def observations(docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        path = args.site / 'data' / 'fda_demographics_extracted.json' if args.site else None
        if path is None or not path.exists():
            return [], None
        # The pilot read the files an earlier fetch left on disk: the locally recovered documents.
        document_of = {d['submission_number']: d for d in docs if d['origin'] == 'local_recovered'}
        pages_of = {d['document_id']: (documents.read_text(args.cache, d['sha256'], version) or {}).get('pages')
                    for d in document_of.values()}
        return demographics.observations(path, document_of, pages_of)
    try:
        files = artifacts.build(
            rows=rows, inventory_meta=inventory_meta(args, rows), aliases=aliases, curated_families=curated_families,
            reviews=reviews, statements=statements, curated_meta=curated_meta,
            openfda=frozen['openfda'][0], openfda_meta=frozen['openfda'][1],
            pages=frozen['fda_pages'][0], pages_meta=frozen['fda_pages'][1],
            manifest_lines=manifest.lines, manifest_meta=manifest_meta, cache=args.cache, extraction_version=version,
            registry=frozen['registry'][0], registry_meta=frozen['registry'][1],
            demographic_observations=observations, producer=producer(),
            generated_at=args.generated_at or acquire.now())
    except ValueError as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    problems = contract.check(files)
    if problems:
        print(f'contract problems ({len(problems)}); nothing written:', *problems[:60], sep='\n  ', file=sys.stderr)
        return 1
    artifacts.write(files, args.out)
    run, den = files['run.json'], files['summary.json']['denominators']
    print(f"{run['run_id']}: {den['source_rows']['n']} rows -> {den['submissions']['n']} submissions -> "
          f"{den['families']['n']} families; {den['documents_retrieved']['n']} documents held; "
          f"{den['evidence_records']['n']} evidence records over {den['pairs']['n']} pairs and "
          f"{den['studies']['n']} studies -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('acquire', 'build'):
        p = sub.add_parser(name)
        p.add_argument('--cache', type=Path, required=True)
        p.add_argument('--inventory', type=Path, default=ROOT / 'data' / 'ai-ml-enabled-devices-enriched.csv')
        p.add_argument('--source-list', type=Path, default=ROOT / 'data' / 'ai-ml-enabled-devices-csv_20260305.csv')
        p.add_argument('--local-documents', type=Path, default=ROOT / 'data' / 'pilot_summary_statements')
        p.add_argument('--site', type=Path)
        if name == 'acquire':
            p.add_argument('--stage', action='append', choices=STAGES)
            p.add_argument('--workers', type=int, default=1, help='parallel text extractions (default 1)')
            p.add_argument('--store-budget-gb', type=float,
                           help='stop requesting documents once the store holds this much; the rest are recorded '
                                'as skipped and their coverage reads search incomplete')
        else:
            p.add_argument('--out', type=Path, default=ROOT / 'build' / 'devices')
            p.add_argument('--generated-at', help='pin the run timestamp (ISO 8601) for a byte-reproducible build')
    args = ap.parse_args(argv)
    rows, subs = load_inventory(args)
    if args.command == 'acquire':
        stages = {'local': stage_local, 'openfda': stage_openfda, 'pages': stage_pages, 'documents': stage_documents,
                  'text': stage_text, 'registry-numbers': stage_registry_numbers,
                  'registry-names': stage_registry_names, 'nct': stage_nct, 'freeze': stage_freeze}
        for name in args.stage or STAGES:
            if name in stages:
                stages[name](args, subs)
        return 0
    return run_build(args, rows, subs)


if __name__ == '__main__':
    sys.exit(main())
