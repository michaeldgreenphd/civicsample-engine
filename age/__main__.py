"""CLI module entrypoint for ``python -m age``."""
from __future__ import annotations

import sys

from age.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
