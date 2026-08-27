#!/usr/bin/env python3
"""
FDA 510(k)/De Novo/PMA Summary PDF Fetcher

Reads the FDA AI/ML-enabled devices CSV, constructs the public summary-document
URL for each submission, downloads the PDF to data/fda_pdfs/, and writes an
enriched CSV at data/ai-ml-enabled-devices-enriched.csv containing two extra
columns:
  - pdf_url         constructed web link to the FDA summary PDF, or "Not Found"
                    if the submission number does not match a buildable pattern
  - local_pdf_path  repo-relative path to the downloaded file, or "Not Found"
                    if the PDF is not cached on disk

This is intentionally decoupled from the LLM extraction step in
extract_fda_demographics.py so that PDFs can be fetched, cached, and audited
independently of any model-run. `pdf_url` is populated purely from URL
construction, so it remains valid even when downloads fail or are skipped —
`local_pdf_path` is the sole signal for whether a PDF is actually cached.

Usage:
    python scripts/extraction/fetch_fda_pdfs.py [--limit N] [--force] [--offline]

    --limit N   only process the first N rows (useful for smoke tests)
    --force     re-download PDFs even if they already exist locally
    --offline   skip HTTP downloads entirely; only record URLs and detect
                already-cached PDFs. Useful for environments without FDA egress.
"""

import argparse
import csv
import os
import re
import sys
import time

import requests

INPUT_CSV = "data/ai-ml-enabled-devices-csv_20260305.csv"
OUTPUT_CSV = "data/ai-ml-enabled-devices-enriched.csv"
PDF_DIR = "data/fda_pdfs"

REQUEST_TIMEOUT = 60
REQUEST_DELAY_SEC = 0.5
USER_AGENT = "CivicSample-Research/1.0 (academic research)"
NOT_FOUND = "Not Found"


def build_pdf_url(submission_number: str) -> str | None:
    """Construct the FDA accessdata PDF URL for a given submission number.

    Matches the behavior used in extract_fda_demographics.py so the two
    scripts stay in sync.
    """
    if not submission_number:
        return None
    sn = submission_number.strip().upper()
    match = re.match(r'[A-Z]+(\d{2})', sn)
    if not match:
        return None
    year_prefix = match.group(1)
    return f"https://www.accessdata.fda.gov/cdrh_docs/pdf{year_prefix}/{sn}.pdf"


def download_pdf(url: str, dest_path: str, session: requests.Session) -> bool:
    """Download `url` to `dest_path`. Returns True on success."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as e:
        print(f"    ! network error: {e}", file=sys.stderr)
        return False

    if resp.status_code != 200:
        print(f"    ! HTTP {resp.status_code}")
        return False

    ct = resp.headers.get("Content-Type", "").lower()
    if "pdf" not in ct and not url.lower().endswith(".pdf"):
        print(f"    ! not a PDF (Content-Type: {ct})")
        return False

    tmp_path = dest_path + ".part"
    try:
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        print(f"    ! write error: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Only process the first N rows")
    p.add_argument("--force", action="store_true",
                   help="Re-download PDFs even if they already exist locally")
    p.add_argument("--offline", action="store_true",
                   help="Skip HTTP downloads; only record URLs and detect "
                        "already-cached PDFs")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not os.path.exists(INPUT_CSV):
        print(f"Error: input CSV not found at {INPUT_CSV}", file=sys.stderr)
        return 1

    os.makedirs(PDF_DIR, exist_ok=True)

    with open(INPUT_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if args.limit is not None:
        rows = rows[: args.limit]

    out_fieldnames = fieldnames + ["pdf_url", "local_pdf_path"]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    total = len(rows)
    downloaded = 0
    cached = 0
    missing = 0
    unbuildable = 0

    print(f"FDA PDF Fetcher{' (offline mode)' if args.offline else ''}")
    print(f"  Input:  {INPUT_CSV}")
    print(f"  PDFs:   {PDF_DIR}/")
    print(f"  Output: {OUTPUT_CSV}")
    print(f"  Rows:   {total}\n")

    for i, row in enumerate(rows, start=1):
        sub_num = (row.get("Submission Number") or "").strip()
        device = row.get("Device") or ""
        prefix = f"[{i}/{total}] {sub_num or '—':<10} {device[:50]}"

        url = build_pdf_url(sub_num)
        if not url:
            print(f"{prefix}  (no URL)")
            row["pdf_url"] = NOT_FOUND
            row["local_pdf_path"] = NOT_FOUND
            unbuildable += 1
            continue

        # pdf_url is always populated once we can build it — the file may or
        # may not actually be cached locally.
        row["pdf_url"] = url
        filename = f"{sub_num.upper()}.pdf"
        local_path = os.path.join(PDF_DIR, filename)

        if os.path.exists(local_path) and not args.force:
            print(f"{prefix}  (cached)")
            row["local_pdf_path"] = local_path
            cached += 1
            continue

        if args.offline:
            row["local_pdf_path"] = NOT_FOUND
            missing += 1
            continue

        print(f"{prefix}")
        ok = download_pdf(url, local_path, session)
        if ok:
            row["local_pdf_path"] = local_path
            downloaded += 1
        else:
            row["local_pdf_path"] = NOT_FOUND
            missing += 1

        time.sleep(REQUEST_DELAY_SEC)

    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    available = downloaded + cached
    print()
    print(f"Summary:")
    print(f"  Downloaded:     {downloaded}")
    print(f"  Already cached: {cached}")
    print(f"  Missing:        {missing}")
    print(f"  No URL built:   {unbuildable}")
    print(f"  Available:      {available}/{total} ({available / total * 100:.1f}%)")
    print(f"  Enriched CSV:   {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
