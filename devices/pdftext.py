"""Page text from one PDF. Self-contained on purpose (standard library and pypdf only): the build
runs it as a separate process, `python -I devices/pdftext.py PDF OUT.json.gz`, so that a
downloaded file is parsed in isolated mode and a parser that hangs can be timed out.

The result is cached by the document's sha256 and EXTRACTION_VERSION, so a document is parsed
once per extractor version and a changed extractor never silently reuses old text.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

# Bump the suffix when anything below changes what text comes out.
EXTRACTION_RULES = 'r1'
# A page "has text" when it yields at least this many letters or digits. Assumption: a page of a
# regulatory document with fewer is a cover, a blank or an image; scanned pages yield none.
MIN_ALNUM_PER_PAGE = 20
TEXT_STATUSES = ('text', 'partial', 'image_only', 'failed')


def extraction_version() -> str:
    import pypdf
    return f'pypdf-{pypdf.__version__}-{EXTRACTION_RULES}'


def status_of(pages: list[str]) -> str:
    """text: every page has text. partial: some do. image_only: none does (a scan without a text
    layer; no OCR is attempted here)."""
    with_text = sum(1 for p in pages if len(re.findall(r'[A-Za-z0-9]', p)) >= MIN_ALNUM_PER_PAGE)
    if not pages or with_text == 0:
        return 'image_only'
    return 'text' if with_text == len(pages) else 'partial'


def extract(pdf: Path) -> dict[str, Any]:
    """Text of every page, in order. A page that raises is recorded as empty with its error; a
    file that cannot be opened is status 'failed' with the reason. Nothing is guessed.

    `source_sha256` is the sha256 of the bytes parsed, read once and parsed from memory, so the
    text is bound to exactly those bytes: the cache is keyed by a digest, and a stored file that
    was missing, damaged or replaced when it was read must not leave text under another digest."""
    out: dict[str, Any] = {'extraction_version': extraction_version(), 'status': 'failed', 'n_pages': None,
                           'n_pages_with_text': None, 'pages': [], 'page_errors': {}, 'error': None,
                           'source_sha256': None}
    try:
        import pypdf
        raw = pdf.read_bytes()
        out['source_sha256'] = hashlib.sha256(raw).hexdigest()
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            reader.decrypt('')
        pages: list[str] = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or '')
            except Exception as exc:           # one unreadable page must not lose the others
                pages.append('')
                out['page_errors'][str(i)] = f'{type(exc).__name__}: {exc}'[:200]
        out.update(pages=pages, n_pages=len(pages), status=status_of(pages),
                   n_pages_with_text=sum(1 for p in pages if len(re.findall(r'[A-Za-z0-9]', p)) >= MIN_ALNUM_PER_PAGE))
    except Exception as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'[:300]
    return out


def main(argv: list[str]) -> int:
    pdf, target = Path(argv[1]), Path(argv[2])
    result = extract(pdf)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_bytes(gzip.compress(json.dumps(result, ensure_ascii=False).encode(), mtime=0))
    tmp.replace(target)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
