#!/usr/bin/env python3
"""Derive the sex/gender analysis sidecar from the retained raw measures.

READS   --from-raw  data/sex_gender_raw_measures.jsonl.gz (the retained SELECTED
        measures, one record per trial with its stamps) or
        --from-envelope data/sex_gender_raw_envelope.jsonl.gz (the versioned
        envelope, src/sex_gender_table.select_raw_envelope). Every record is
        parsed with the vendored parser exactly as scripts/build_sex_gender_table.py
        parses it, then run through src/sex_gender_analysis.analyze.
WRITES  data/sex_gender_analysis.csv.gz       one row per trial, columns in
                                             src/sex_gender_analysis.COLUMNS
        data/sex_gender_analysis_meta.json   provenance, the analysis and
                                             parser rules versions, the counts
                                             the summary block will carry, and
                                             the unresolved rules
        --write-back demographics.json       every study's sex_gender_analysis
                                             key becomes the lean block derived
                                             here (one-to-one coverage required,
                                             as the parsed-table write-back)
INVOKED by hand, and by .github/workflows/extract.yml once the owner wires it
        in after scripts/build_sex_gender_table.py (the cutover plan in
        docs/sex_gender_analysis/RULE_SOURCE_MATRIX.md). Run from the repo root.

A record that cannot be parsed is a parse_error block, never a dropped trial.
A file that mixes two extractions is refused (as the table builder refuses
it), and a write-back whose coverage is not one-to-one writes nothing back.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sex_gender_analysis as sga  # noqa: E402
from src import sex_gender_table as sgt  # noqa: E402
from src.utils import pipeline_commit  # noqa: E402


def iter_records(path: str):
    with gzip.open(path, "rt") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if line:
                yield n, line


def derive(path: str, snapshot_date: str | None, envelope: bool) -> tuple[list[dict], list[dict], dict]:
    """(flat rows, lean blocks keyed by nct in order, stamps) from a raw file."""
    rows: list[dict] = []
    leans: list[dict] = []
    stamps: dict = {"snapshot_date": None, "extracted_at": None, "source_pipeline_commit": None,
                    "n_parse_errors": 0, "n_records": 0, "envelope_version": None}
    for n, line in iter_records(path):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            full = sga.analyze(sgt.parse_error_row(None, None, snapshot_date or sgt.today_utc()), None, None)
            rows.append(sga.flat_row(None, full, snapshot_date))
            leans.append({"nct_id": None, **sga.lean_block(full)})
            stamps["n_parse_errors"] += 1
            continue
        stamps["n_records"] += 1
        for k, src in (("snapshot_date", "snapshot_date"), ("extracted_at", "extracted_at"),
                       ("source_pipeline_commit", "pipeline_commit")):
            v = raw.get(src)
            if v is None:
                continue
            if stamps[k] is None:
                stamps[k] = v
            elif stamps[k] != v:
                raise SystemExit(f"{path}: mixed provenance: {src} {stamps[k]!r} and {v!r} (record {raw.get('nct_id')}); "
                                 "refusing to derive from records of more than one extraction")
        if envelope:
            if raw.get("envelope_version") != sgt.ENVELOPE_VERSION:
                raise SystemExit(f"{path} line {n}: not a {sgt.ENVELOPE_VERSION} envelope "
                                 f"({raw.get('envelope_version')!r}); use --from-raw for a raw-measure file")
            stamps["envelope_version"] = raw.get("envelope_version")
        snap = snapshot_date or raw.get("snapshot_date") or sgt.today_utc()
        try:
            row = sgt.build_row_from_envelope(raw, snap) if envelope else sgt.build_row_from_raw(raw, snap)
            full = sga.analyze(row, raw.get("measures") or [], raw.get("enrollment"))
        except Exception as e:  # noqa: BLE001 - parse_error is the state for exactly this
            print(f"::warning::{raw.get('nct_id')}: parser or analysis failed on the retained record "
                  f"({type(e).__name__}: {e}); filed as parse_error")
            full = sga.analyze(sgt.parse_error_row(raw.get("nct_id"), raw.get("enrollment"), snap), None, None)
            stamps["n_parse_errors"] += 1
        rows.append(sga.flat_row(raw.get("nct_id"), full, snap))
        leans.append({"nct_id": raw.get("nct_id"), **sga.lean_block(full)})
    return rows, leans, stamps


def write_table(rows: list[dict], out_csv: str) -> None:
    """The sidecar table: JSON for the list/object columns (labels can hold
    any character), empty cell for None, True/False for booleans."""
    import csv
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    opener = gzip.open if out_csv.endswith(".gz") else open
    with opener(out_csv, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=sga.COLUMNS)
        w.writeheader()
        for r in rows:
            out = {}
            for c in sga.COLUMNS:
                v = r.get(c)
                if c in sga.JSON_COLUMNS:
                    out[c] = json.dumps(v if v is not None else ([] if c != "gc_labels" else {}), ensure_ascii=False)
                elif v is None:
                    out[c] = ""
                else:
                    out[c] = v
            w.writerow(out)


def replace_json(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-raw", default=None, help="the retained raw-measure records (jsonl.gz)")
    src.add_argument("--from-envelope", default=None, help="the versioned raw envelope records (jsonl.gz)")
    ap.add_argument("--snapshot-date", default=None, help="override the stamped snapshot date")
    ap.add_argument("--out", default="data/sex_gender_analysis.csv.gz")
    ap.add_argument("--meta", default="data/sex_gender_analysis_meta.json")
    ap.add_argument("--write-back", default=None,
                    help="demographics.json to update in place: every study's sex_gender_analysis key becomes "
                         "the lean block derived here")
    a = ap.parse_args(argv)
    path = a.from_envelope or a.from_raw
    if not os.path.exists(path):
        raise SystemExit(f"{path}: no such file")
    rows, leans, stamps = derive(path, a.snapshot_date, envelope=bool(a.from_envelope))
    snapshot_date = a.snapshot_date or stamps["snapshot_date"]

    container = None
    written_back: dict | None = None
    by_nct: dict = {}
    if a.write_back:
        with open(a.write_back) as f:
            container = json.load(f)
        # Both source stamps must agree: the same extracted_at from another code
        # revision is another pull's output, and a write-back would hide that
        # behind the target's own stamps.
        c_at = container.get("extracted_at")
        c_commit = container.get("pipeline_commit")
        if stamps["extracted_at"] and c_at and c_at != stamps["extracted_at"]:
            raise SystemExit(f"--write-back refused: the raw records were extracted at {stamps['extracted_at']} but "
                             f"{a.write_back} was extracted at {c_at}; nothing written")
        if stamps["source_pipeline_commit"] and c_commit and c_commit != stamps["source_pipeline_commit"]:
            raise SystemExit(f"--write-back refused: the raw records came from pipeline commit "
                             f"{stamps['source_pipeline_commit']} but {a.write_back} carries {c_commit}; nothing written")
        ids = [l.get("nct_id") for l in leans]
        no_id = sum(1 for i in ids if not i)
        dupes = sorted(i for i, n in Counter(i for i in ids if i).items() if n > 1)
        by_nct = {l["nct_id"]: {k: v for k, v in l.items() if k != "nct_id"} for l in leans if l.get("nct_id")}
        record_ids = [s.get("nct_id") for s in container["data"]]
        unmatched = sorted(set(i for i in record_ids if i) - set(by_nct))
        extra = sorted(set(by_nct) - set(i for i in record_ids if i))
        coverage_ok = not (no_id or dupes or unmatched or extra)
        written_back = {"path": a.write_back, "records": len(container["data"]), "updated": 0,
                        "coverage_ok": coverage_ok, "rows_without_id": no_id, "duplicate_ids": dupes[:20],
                        "records_without_row": unmatched[:20], "rows_without_record": extra[:20]}

    write_table(rows, a.out)
    if container is not None and written_back is not None:
        if written_back["coverage_ok"]:
            for s in container["data"]:
                s["sex_gender_analysis"] = by_nct[s["nct_id"]]
            replace_json(a.write_back, container)
            written_back["updated"] = len(container["data"])
            print(f"wrote back lean sex_gender_analysis blocks into {a.write_back}: {written_back['updated']} records")
        else:
            print("::error::write-back coverage is not one-to-one; nothing written back")

    summary = sga.summarize({k: v for k, v in l.items() if k != "nct_id"} for l in leans)
    meta: dict[str, Any] = {
        "written_back": written_back,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_commit": pipeline_commit(),
        "source_pipeline_commit": stamps["source_pipeline_commit"] or (container or {}).get("pipeline_commit"),
        "source_extracted_at": stamps["extracted_at"] or (container or {}).get("extracted_at"),
        "snapshot_date": snapshot_date,
        "source": f"derived from {path}" + (" (raw envelope)" if a.from_envelope else " (raw measures)"),
        "raw_envelope_version": stamps["envelope_version"],
        "n_rows": len(rows),
        "n_parse_errors": stamps["n_parse_errors"],
        "schema_version": sga.SCHEMA_VERSION,
        "rules_version": sga.RULES_VERSION,
        "layer": sga.LAYER,
        "parser_rules_version": sgt.sgp.PARSER_RULES_VERSION,
        "parser_module_version": sgt.sgp.__version__,
        "columns": sga.COLUMNS,
        "summary": summary,
    }
    os.makedirs(os.path.dirname(a.meta) or ".", exist_ok=True)
    with open(a.meta, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(f"wrote {a.out}: {len(rows)} rows; rules {sga.RULES_VERSION}")
    print("reporting dimension:", summary["reportingDimension"])
    print("neither reason:", summary["neitherReason"])
    print("gender categories:", summary["genderCategoriesStatus"])
    if written_back and not written_back["coverage_ok"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
