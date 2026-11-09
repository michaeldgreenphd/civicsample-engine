#!/usr/bin/env python3
"""Check a written outcomes artifact directory against the producer contract.

  python3 scripts/check_outcomes_contract.py PATH [--publishable]

--publishable also refuses a provisional preview (machine assessments). Exit 0 on a pass.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from outcomes import contract  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('path', type=Path)
    ap.add_argument('--publishable', action='store_true')
    args = ap.parse_args(argv)
    problems = contract.check(contract.load_dir(args.path), publishable=args.publishable)
    for problem in problems:
        print(problem)
    print(f"{'FAIL' if problems else 'PASS'}: {len(problems)} problem(s) in {args.path}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
