"""Drug trial cross-source evidence linkage and disclosure timeline.

Links ClinicalTrials.gov drug and biological study records across three public evidence streams:
  1. ClinicalTrials.gov API v2 study records and registry-listed PubMed references
  2. Europe PMC REST full-text/abstract NCT search and NCBI E-utilities PubMed metadata
  3. openFDA Drugs@FDA NDA/BLA application records

All network acquisition is rate-limited (<= 2 requests/second per host), resumable, and frozen as
sha256-pinned gzipped JSONL archives with manifest sidecars before offline builds run. Human review
decisions live exclusively in curated CSV tables and are never populated by code.
"""
from __future__ import annotations

SCHEMA_VERSION = 1
METHOD_VERSION = 'drug-disclosure-1'
