"""Operational feasibility, accrual velocity, retention, and early-termination benchmarks.

Manual analytical package over full ClinicalTrials.gov API v2 study records or weekly
full-record snapshots (`src/full_records.py`). Never scheduled by `.github/workflows/extract.yml`,
and never reads or modifies site-projected `demographics.part*.json.gz` files.
"""
from __future__ import annotations

from feasibility.artifacts import METHOD_VERSION, SCHEMA_VERSION
from feasibility.termination import TERMINATION_RULES_VERSION

__all__ = [
    "METHOD_VERSION",
    "SCHEMA_VERSION",
    "TERMINATION_RULES_VERSION",
]
