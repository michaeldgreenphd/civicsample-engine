"""CLI module entrypoint for ``python -m catchments``."""
from __future__ import annotations

import sys

from catchments.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
