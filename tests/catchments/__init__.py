"""Offline unit tests for the multi-site US catchment bounds and footprint engine."""
from __future__ import annotations

from pathlib import Path

# Because repository-level tests/ has no __init__.py, pytest's default prepend import mode
# adds tests/ ahead of the repo root when tests/catchments/__init__.py is present. Extending
# __path__ with the root catchments/ package keeps both package discovery and submodule
# imports (catchments.footprint, catchments.bounds, catchments.benchmark) seamless.
_ROOT_CATCHMENTS = str(Path(__file__).resolve().parents[2] / "catchments")
if _ROOT_CATCHMENTS not in __path__:
    __path__.insert(0, _ROOT_CATCHMENTS)

SCHEMA_VERSION: int = 1
METHOD_VERSION: str = "catchments-multisite-1"
