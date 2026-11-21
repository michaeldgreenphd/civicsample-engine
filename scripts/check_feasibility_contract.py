#!/usr/bin/env python3
"""Check a written feasibility-benchmarks artifact directory against the producer contract.

  python3 scripts/check_feasibility_contract.py PATH

Verifies `run.json` schema and `run_identity`, SHA-256 byte integrity of every sibling file,
absence of leaked private review columns, per-trial operational/accrual/retention/termination
invariants, small-cell withholding, and full `summary.json` recount parity against `trials.jsonl.gz`.
Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feasibility import artifacts  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path, help="Path to a feasibility build directory.")
    args = ap.parse_args(argv)

    try:
        parsed_family, raw_bytes = artifacts.load_dir(args.path)
    except (OSError, ValueError) as exc:
        print(f"FAIL: could not load {args.path}: {exc}")
        return 1

    problems = artifacts.check_contract(parsed_family, raw_bytes)
    for problem in problems:
        print(problem)
    status = "FAIL" if problems else "PASS"
    print(f"{status}: {len(problems)} problem(s) in {args.path}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
