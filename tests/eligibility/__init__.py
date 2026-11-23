"""Unit tests for the deterministic `eligibility` package."""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_PROD_PKG_DIR = _REPO_ROOT / "eligibility"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if __name__ == "eligibility" and _PROD_PKG_DIR.is_dir():
    __path__ = [str(_PROD_PKG_DIR), str(_THIS_DIR)]
    _prod_init = _PROD_PKG_DIR / "__init__.py"
    exec(compile(_prod_init.read_bytes(), str(_prod_init), "exec"), globals())
