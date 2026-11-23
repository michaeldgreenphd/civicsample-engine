"""CLI module entrypoint for ``python -m eligibility``."""
from __future__ import annotations

import sys

from eligibility.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
