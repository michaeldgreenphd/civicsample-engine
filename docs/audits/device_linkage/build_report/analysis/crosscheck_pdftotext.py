#!/usr/bin/env python3
"""Independent check of the NCT-id scan: read every held PDF with poppler's pdftotext and compare
the NCT ids found with those the build found in the pypdf text. Run with `python -I`.

A scientific-validity check on recall of the scan (two extractors, one pattern), not part of the
build. usage: crosscheck_pdftotext.py <cache dir> <documents.json> <out.json>
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

NCT = re.compile(r'(?<![A-Za-z0-9])N\s?C\s?T[\s\-#:.]{0,3}((?:\d\s?){8})(?!\d)', re.I)


def ids_of(pdf: Path) -> tuple[str, list[str] | None, int]:
    try:
        out = subprocess.run(['pdftotext', '-q', str(pdf), '-'], capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return pdf.stem, None, 0
    if out.returncode != 0:
        return pdf.stem, None, 0
    text = out.stdout.decode('utf-8', 'replace')
    return pdf.stem, sorted({'NCT' + re.sub(r'\s', '', m.group(1)) for m in NCT.finditer(text)}), \
        len(re.findall(r'[A-Za-z0-9]', text))


def main() -> None:
    cache, documents, target = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    docs = [d for d in json.loads(documents.read_text())['documents'] if d['availability'] == 'retrieved']
    build = {}
    for d in docs:
        build.setdefault(d['sha256'], {'ids': set(), 'status': (d['text'] or {}).get('status')})
        build[d['sha256']]['ids'] |= {i['identifier'] for i in d['identifiers'] if i['kind'] == 'nct'}
    # One document at a time: nothing is held in memory but the ids found.
    results = [ids_of(cache / 'documents' / 'store' / f'{s}.pdf') for s in sorted(build)]
    rows, failed = [], 0
    agree = only_build = only_poppler = text_gain = 0
    for digest, ids, n_alnum in results:
        if ids is None:
            failed += 1
            continue
        b = build[digest]['ids']
        agree += set(ids) == b
        if set(ids) != b:
            rows.append({'sha256': digest, 'pypdf': sorted(b), 'pdftotext': ids, 'pypdf_text_status': build[digest]['status']})
        only_build += bool(b - set(ids))
        only_poppler += bool(set(ids) - b)
        text_gain += build[digest]['status'] in ('image_only', 'failed', 'not_extracted') and n_alnum >= 200
    out = {'n_document_versions': len(build), 'n_pdftotext_failed': failed, 'n_same_nct_set': agree,
           'n_with_ids_only_pypdf_found': only_build, 'n_with_ids_only_pdftotext_found': only_poppler,
           'n_without_pypdf_text_where_pdftotext_found_text': text_gain,
           'n_versions_with_any_nct_pypdf': sum(1 for v in build.values() if v['ids']),
           'n_versions_with_any_nct_pdftotext': sum(1 for _, ids, _ in results if ids),
           'differences': rows}
    target.write_text(json.dumps(out, indent=1) + '\n')
    print({k: v for k, v in out.items() if k != 'differences'})


if __name__ == '__main__':
    main()
