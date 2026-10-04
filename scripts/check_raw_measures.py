#!/usr/bin/env python3
"""Check the week's raw sex/gender measures are whole before they are archived.

READS   data/sex_gender_raw_measures.jsonl.gz (src/extract_all.py writes it,
        one JSON object per line, one line per extracted trial)
EXITS   1, with ::error:: lines, when the file must not replace the dated asset
        on the permanent sex-gender-raw-measures release:
          - it is missing, not gzip, truncated (a run killed mid-write
            leaves no end-of-stream marker) or not UTF-8;
          - a line is not a JSON object with an nct_id;
          - a record's snapshot_date is not this run's date;
          - the records disagree on extracted_at or pipeline_commit (two
            runs in one file);
          - an nct_id appears twice;
          - it holds no records.
INVOKED by .github/workflows/extract.yml in the archive step, before
        `gh release upload --clobber`: that upload replaces a same-day
        asset, so only a whole file of this run may reach it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import zlib
from typing import Any

MAX_REPORTED = 20


def check(path: str, snapshot_date: str) -> tuple[int, list[str]]:
    """(records read, errors); the file is fit to archive when errors is empty."""
    errors: list[str] = []
    seen: set[str] = set()
    run: tuple[Any, Any] | None = None
    records = 0

    def err(msg: str) -> None:
        if len(errors) < MAX_REPORTED:
            errors.append(msg)

    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for n, line in enumerate(fh, start=1):
                try:
                    rec = json.loads(line)
                except ValueError:
                    err(f"line {n} is not JSON")
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("nct_id"), str):
                    err(f"line {n} is not a record with an nct_id")
                    continue
                records += 1
                nct = rec["nct_id"]
                if rec.get("snapshot_date") != snapshot_date:
                    err(f"{nct}: snapshot_date is {rec.get('snapshot_date')!r}, not this run's {snapshot_date}")
                stamps = (rec.get("extracted_at"), rec.get("pipeline_commit"))
                if run is None:
                    run = stamps
                    if not all(isinstance(s, str) and s for s in stamps):
                        err(f"{nct}: extracted_at or pipeline_commit is missing ({stamps!r})")
                elif stamps != run:
                    err(f"{nct}: from another run than the first record ({stamps!r}, not {run!r})")
                if nct in seen:
                    err(f"{nct} appears twice")
                seen.add(nct)
    except UnicodeDecodeError as e:
        err(f"{path} is not UTF-8 text: {e}")
    except (OSError, EOFError, zlib.error) as e:
        err(f"{path} is not a whole gzip file: {e}")
    if not records and not errors:
        err(f"{path} holds no records")
    return records, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the raw sex/gender measures are whole before archiving.")
    ap.add_argument("path")
    ap.add_argument("--snapshot-date", required=True, help="this run's CURRENT_DATE")
    a = ap.parse_args(argv)
    records, errors = check(a.path, a.snapshot_date)
    for e in errors:
        print(f"::error::{e}")
    print(f"raw measures check: {'ok' if not errors else 'FAILED'}; {records:,} records")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
