#!/usr/bin/env python3
"""
Merge 2-part demographics gzip files back into a single JSON file.
Used by the backfill workflow to re-split old snapshots into 8 parts.
"""
import gzip
import json


def main():
    parts_data = []
    extracted_at = None
    for i in [1, 2]:
        path = f"data/demographics.part{i}.json.gz"
        print(f"Reading {path}...")
        with gzip.open(path, "rt") as f:
            part = json.load(f)
            parts_data.extend(part["data"])
            if not extracted_at:
                extracted_at = part["extracted_at"]

    merged = {"extracted_at": extracted_at, "data": parts_data}
    with open("data/demographics.json", "w") as f:
        json.dump(merged, f)
    print(f"Merged {len(parts_data)} studies into data/demographics.json")


if __name__ == "__main__":
    main()
