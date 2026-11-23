#!/usr/bin/env python3
"""Check a written outcomes-comparability artifact directory against the producer contract.

  python3 scripts/check_outcomes_comparability_contract.py PATH

Verifies `run.json` schema/regex/`run_identity`, exact file inventory and byte SHA-256 digests,
absence of leaked private keys, per-record domain invariants, denominator bounds on every count
object in `summary.json`, and full recount parity against `records.json`. Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import comparability_build as B  # noqa: E402


def check_dir(path: Path) -> list[str]:
    """Validate raw on-disk bytes and parsed schema-1 contract of `path`."""
    if not path.is_dir():
        return [f'{path}: not a directory']

    expected = {'run.json', 'summary.json', 'records.json'}
    present_entries = [p for p in sorted(path.iterdir()) if p.name != '.DS_Store']
    unexpected = [p.name for p in present_entries if p.is_symlink() or not p.is_file() or p.name not in expected]
    if unexpected:
        return [f'{path}: unexpected directory entries {unexpected}']

    files: dict[str, Any] = {}
    raw_sha: dict[str, str] = {}
    for name in sorted(expected):
        fp = path / name
        if not fp.is_file():
            continue
        raw = fp.read_bytes()
        raw_sha[name] = hashlib.sha256(raw).hexdigest()
        try:
            files[name] = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            return [f'{name}: invalid UTF-8 JSON ({exc})']

    problems = B.check(files)
    # Also verify exact raw on-disk byte sha256 against run.json['files'] (catches whitespace/formatting
    # edits on disk that parse to the same JSON object).
    run = files.get('run.json')
    if isinstance(run, dict) and isinstance(run.get('files'), dict):
        for name, expected_hex in sorted(run['files'].items()):
            if name in raw_sha and raw_sha[name] != expected_hex:
                msg = f'{name}: on-disk byte sha256 does not match run.json'
                if msg not in problems:
                    problems.append(msg)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('path', type=Path)
    args = ap.parse_args(argv)
    problems = check_dir(args.path)
    for problem in problems:
        print(problem)
    print(f"{'FAIL' if problems else 'PASS'}: {len(problems)} problem(s) in {args.path}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
