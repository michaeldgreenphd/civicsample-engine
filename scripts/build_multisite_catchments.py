#!/usr/bin/env python3
"""Build multi-site US catchment footprints and ACS bounds by hand (never scheduled)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catchments.cli import run_cli  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
