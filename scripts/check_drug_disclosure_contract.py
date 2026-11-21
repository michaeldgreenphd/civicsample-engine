#!/usr/bin/env python3
"""Check a written drug-disclosure artifact directory against the producer contract.

Usage:
  python3 scripts/check_drug_disclosure_contract.py build/disclosure

Exits 0 when every manifest hash, `run_identity`, count object, coverage cell, partial-date
interval bound, human-review boundary, and sponsor 3-state invariant passes; exits 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from disclosure import artifacts as ART  # noqa: E402


def check_path(path: Path) -> list[str]:
    """Load `path` and return all contract violations (empty list when valid)."""
    try:
        files, raw_hashes = ART.load_dir(path)
    except (OSError, ValueError) as exc:
        return [f'{path}: failed to load artifact directory ({exc})']
    return ART.check_contract(files, raw_file_hashes=raw_hashes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('path', type=Path, help='Path to a drug-disclosure build directory')
    args = ap.parse_args(argv)
    problems = check_path(args.path)
    for problem in problems:
        print(problem)
    verdict = 'FAIL' if problems else 'PASS'
    print(f'{verdict}: {len(problems)} problem(s) in {args.path}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
