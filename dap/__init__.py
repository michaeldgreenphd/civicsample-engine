"""Multi-site Diversity Action Plan (FDORA) & geographic site-mix benchmarks.

Manual analytical vertical slice over ClinicalTrials.gov study records,
optional MeSH condition indexing, condition_ontology.json, and curated
read-only sponsor attribution. Never scheduled; never modifies weekly
production artifacts.
"""
from __future__ import annotations

SCHEMA_VERSION: int = 1
METHOD_VERSION: str = "dap-site-mix-benchmarks-1"
UNIT_BASIS: str = "listed_facilities_not_participants"

__all__ = [
    "METHOD_VERSION",
    "SCHEMA_VERSION",
    "UNIT_BASIS",
]
