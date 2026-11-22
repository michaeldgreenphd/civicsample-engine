#!/usr/bin/env python3
"""Check a written device-linkage artifact directory against the producer contract.

  python3 scripts/check_device_links_contract.py PATH

Exit 0 on a pass. An engineering check (internal consistency, stated rules); it says nothing
about whether any link is correct.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devices import contract  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('path', type=Path)
    args = ap.parse_args(argv)
    problems = contract.check(contract.load_dir(args.path))
    for problem in problems:
        print(problem)
    print(f"{'FAIL' if problems else 'PASS'}: {len(problems)} problem(s) in {args.path}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
