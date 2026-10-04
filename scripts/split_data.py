#!/usr/bin/env python3
"""
Split large demographics.json into N compressed parts.

Each part must stay under 20 MB (gzipped) so that jsDelivr can serve
historical snapshots via its GitHub CDN mirror.  With ~140 MB of gzipped
data, 8 parts keeps each comfortably under the limit.

Also writes data/run.json, the run's stamps (extracted_at, pipeline_commit,
total_parts, studies), which the site uses as its data cache key.
"""
import json
import gzip
import math
import os

NUM_PARTS = 8
MAX_PART_MB = 20  # jsDelivr per-file limit


def main() -> None:
    print("Loading full dataset...")
    with open('data/demographics.json', 'r') as f:
        full_data = json.load(f)

    studies = full_data['data']
    total = len(studies)
    chunk_size = math.ceil(total / NUM_PARTS)

    print(f"Splitting {total} studies into {NUM_PARTS} parts (~{chunk_size} each)...")

    # Remove old 2-part files if present (backward compat cleanup)
    for old in ['data/demographics.part1.json.gz', 'data/demographics.part2.json.gz']:
        if os.path.exists(old):
            os.remove(old)

    for i in range(NUM_PARTS):
        start = i * chunk_size
        end = min(start + chunk_size, total)
        part_data = {
            'extracted_at': full_data['extracted_at'],
            'pipeline_commit': full_data.get('pipeline_commit'),
            'part': i + 1,
            'total_parts': NUM_PARTS,
            'data': studies[start:end]
        }
        path = f'data/demographics.part{i + 1}.json.gz'
        print(f"  Part {i + 1}: studies {start}–{end - 1} ({end - start} studies)")
        with gzip.open(path, 'wt', compresslevel=9) as f:
            json.dump(part_data, f, separators=(',', ':'))

        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"    → {size_mb:.1f} MB")
        if size_mb > MAX_PART_MB:
            print(f"    ⚠ WARNING: part exceeds {MAX_PART_MB} MB limit!")

    # The run's stamps, published as data/run.json next to the parts. The
    # site keys every data URL by extracted_at, so a browser keeps a run's
    # files across visits and fetches new ones when a new run lands, a
    # same-day re-run included.
    run = {
        'extracted_at': full_data['extracted_at'],
        'pipeline_commit': full_data.get('pipeline_commit'),
        'total_parts': NUM_PARTS,
        'studies': total,
    }
    with open('data/run.json', 'w') as f:
        json.dump(run, f, indent=2)

    print(f"✓ Split {total} studies into {NUM_PARTS} parts; wrote data/run.json")


if __name__ == '__main__':
    main()
