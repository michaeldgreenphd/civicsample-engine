#!/usr/bin/env python3
"""Item 3 (Route A): identifiers cited in locally held FDA summary PDFs.

Reads PDFs as untrusted data (run with `python -I`, script lives apart from
the data). Two independent text extractors are used so a "no NCT ID" result is
not an artefact of one library: pypdf, and poppler's `pdftotext -layout` when
it is on PATH. No network.

usage: python -I 03_pdf_identifiers.py <pdf_dir> <enriched.csv> <out.json>
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys

from pypdf import PdfReader

# Strict form, and a tolerant form that survives a space, hyphen, '#', colon or
# line break between the prefix and the digits ("NCT 01234567", "NCT#0123...").
NCT_STRICT = re.compile(r"NCT\d{8}")
NCT_LOOSE = re.compile(r"NCT[\s#:\-‐-―]{0,3}(\d{8})(?!\d)", re.I)
SUBMISSION = re.compile(r"\b(DEN\d{6}|BK\d{6}|K\d{6}|P\d{6})\b")
DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")
SCANNED_PAGE_CHARS = 50  # a page yielding fewer characters is treated as image-only

# Document-part detection. An FDA "summary PDF" is a bundle; which parts it
# holds decides whether it can cite a study at all:
#   * a 510(k) file is normally: SE letter + Indications for Use form + 510(k) Summary
#   * a De Novo file at cdrh_docs/pdfYY/DENxxxxxx.pdf is the granting ORDER
#     LETTER (classification + special controls). The De Novo DECISION SUMMARY,
#     which describes the clinical evidence, is a different document whose
#     first-page heading is "DE NOVO CLASSIFICATION REQUEST FOR <device>".
DOC_PART_PATTERNS = [
    ("letter", re.compile(r"\bDear\s+(Mr|Ms|Mrs|Dr|[A-Z][a-z]+)")),
    ("de_novo_order_language", re.compile(r"De Novo request|de novo classification|Automatic Class III Designation", re.I)),
    ("de_novo_decision_summary_heading", re.compile(r"DE NOVO CLASSIFICATION REQUEST FOR")),
    ("special_controls", re.compile(r"special controls", re.I)),
    ("indications_for_use_form", re.compile(r"Indications for Use", re.I)),
    ("510k_summary", re.compile(r"510\(k\)\s+Summary", re.I)),
    ("pma_ssed", re.compile(r"SUMMARY OF SAFETY AND EFFECTIVENESS", re.I)),
    ("reports_a_cohort_size", re.compile(r"\b\d[\d,]*\s+(subjects|patients|participants|cases|exams|studies|images|scans)\b", re.I)),
]


def classify_document(parts: list[str]) -> str:
    if "pma_ssed" in parts:
        return "PMA SSED"
    if "de_novo_decision_summary_heading" in parts:
        return "De Novo decision summary"
    if "de_novo_order_language" in parts and "510k_summary" not in parts:
        return "De Novo granting order letter only (no decision summary)"
    if "510k_summary" in parts:
        return "510(k) letter + IFU + 510(k) Summary"
    if "letter" in parts:
        return "letter only"
    return "unclassified"


EVIDENCE_TERMS = {
    "mentions_clinicaltrials_gov": re.compile(r"clinical\s*trials?\.gov", re.I),
    "mentions_clinical_study": re.compile(r"clinical (study|studies|trial|trials|investigation|validation)", re.I),
    "mentions_ide": re.compile(r"\bIDE\b|investigational device exemption", re.I),
    "mentions_pivotal": re.compile(r"\bpivotal\b", re.I),
    "mentions_retrospective": re.compile(r"retrospective", re.I),
    "mentions_prospective": re.compile(r"prospective", re.I),
    "mentions_predicate": re.compile(r"predicate", re.I),
    "mentions_reader_study": re.compile(r"reader study|MRMC|multi-reader", re.I),
}


def pypdf_pages(path: str) -> list[str]:
    reader = PdfReader(path)
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except Exception as exc:  # recorded, not swallowed
            out.append(f"")
            print(f"  ! pypdf page error in {os.path.basename(path)}: {exc}", file=sys.stderr)
    return out


def pdftotext_pages(path: str) -> list[str] | None:
    if not shutil.which("pdftotext"):
        return None
    p = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, timeout=120)
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace").split("\f")


def find_ncts(text: str) -> dict:
    strict = sorted(set(NCT_STRICT.findall(text)))
    loose = sorted({"NCT" + m for m in NCT_LOOSE.findall(text)})
    # also de-hyphenate across line breaks ("NCT0123-\n4567")
    joined = re.sub(r"-?\s*\n\s*", "", text)
    joined_hits = sorted(set(NCT_STRICT.findall(joined)))
    return {"strict": strict, "loose": loose, "line_joined": joined_hits,
            "union": sorted(set(strict) | set(loose) | set(joined_hits))}


def main() -> None:
    pdf_dir, enriched, out = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(enriched, encoding="utf-8-sig", newline="") as f:
        meta = {r["Submission Number"].strip(): r for r in csv.DictReader(f)}
    results = []
    for path in sorted(glob.glob(os.path.join(pdf_dir, "*.pdf"))):
        base = os.path.basename(path)
        m = re.search(r"(DEN\d{6}|BK\d{6}|K\d{6}|P\d{6})", base)  # "_" defeats \b
        sn = m.group(1) if m else None
        row = meta.get(sn, {})
        pages = pypdf_pages(path)
        alt = pdftotext_pages(path)
        text = "\n".join(pages)
        alt_text = "\n".join(alt) if alt else ""
        chars = [len(p.strip()) for p in pages]
        scanned_pages = sum(1 for c in chars if c < SCANNED_PAGE_CHARS)
        per_page_nct = {i + 1: NCT_STRICT.findall(p) for i, p in enumerate(pages) if NCT_STRICT.search(p)}
        other_subs = sorted(set(SUBMISSION.findall(text)) - {sn})
        rec = {
            "file": base, "submission_number": sn, "bytes": os.path.getsize(path),
            "device": row.get("Device"), "company": row.get("Company"),
            "decision_date": row.get("Date of Final Decision"),
            "pdf_url_in_csv": row.get("pdf_url"),
            "pages": len(pages), "chars_pypdf": sum(chars), "chars_pdftotext": len(alt_text.strip()),
            "pages_image_only": scanned_pages,
            "text_status": ("no_text" if sum(chars) < SCANNED_PAGE_CHARS else
                            "partial_text" if scanned_pages else "text"),
            "doc_parts_detected": (parts := [name for name, rx in DOC_PART_PATTERNS if rx.search(text)]),
            "document_class": classify_document(parts),
            "nct_pypdf": find_ncts(text), "nct_pdftotext": find_ncts(alt_text),
            "nct_by_page": per_page_nct,
            "own_submission_number_in_text": bool(sn and sn in text),
            "other_submission_numbers_cited": other_subs,
            "dois": sorted(set(DOI.findall(text)))[:20],
            **{k: len(rx.findall(text)) for k, rx in EVIDENCE_TERMS.items()},
            "first_300_chars": re.sub(r"\s+", " ", text)[:300],
        }
        results.append(rec)
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    for r in results:
        print(r["submission_number"], r["pages"], "pp", r["chars_pypdf"], "/", r["chars_pdftotext"], "chars",
              r["text_status"], "img-only pages:", r["pages_image_only"], "|", r["document_class"],
              "| NCT:", r["nct_pypdf"]["union"], r["nct_pdftotext"]["union"],
              "| ctgov:", r["mentions_clinicaltrials_gov"], "| other subs:", r["other_submission_numbers_cited"][:8],
              "| dois:", len(r["dois"]))


if __name__ == "__main__":
    main()
