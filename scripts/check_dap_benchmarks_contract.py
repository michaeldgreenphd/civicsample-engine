#!/usr/bin/env python3
"""Check a written DAP site-mix benchmark directory against the producer contract.

  python3 scripts/check_dap_benchmarks_contract.py PATH

Exits 0 when every file, sha256 digest, run_identity, denominator, summary
count, small-cell withholding rule, and demographic/sponsor domain invariant
holds; exits 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dap import artifacts  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for checking a DAP benchmark build directory."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path, help="Path to a build directory produced by build_dap_benchmarks.py")
    args = ap.parse_args(argv)

    if not args.path.is_dir():
        print(f"FAIL: {args.path} is not a directory")
        return 1

    try:
        loaded = artifacts.load_dir(args.path)
    except (OSError, ValueError) as exc:
        print(f"FAIL: could not read {args.path}: {exc}")
        return 1

    # A malformed build fails the check with a message, never a traceback: an
    # exception check_contract did not anticipate is reported as a failure.
    try:
        problems = artifacts.check_contract(loaded)
    except Exception as exc:
        print(f"FAIL: contract check could not complete on {args.path}: {type(exc).__name__}: {exc}")
        return 1
    for problem in problems:
        print(problem)
    status = "FAIL" if problems else "PASS"
    print(f"{status}: {len(problems)} problem(s) in {args.path}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
