"""Deterministic multi-site US catchment bounds and facility footprint engine.

Extends ``census/`` from single-site US registrations to multi-facility studies without
inventing per-site participant enrollment weights:
  * ``footprint`` extracts, normalizes, deduplicates, and classifies study facility
    listings into six mutually exclusive site-footprint tiers, separating the 50 US
    states + District of Columbia from US territories and foreign countries, and
    resolving US facilities across ACS geography levels with HHI concentration metrics.
  * ``bounds`` computes assumption-free convex-hull site-allocation envelopes
    (distinguishing resolved bounds from unconditional [0, 100] bounds whenever any US
    facility is unresolved or lacks an ACS estimate), covariance-deduplicated equal-
    facility proxy shares and 90% margins of error, distinct-area pooled resident
    proxies, and joint unknown-by-site settlement checks.
  * ``benchmark`` runs end-to-end per-trial and cohort multi-site benchmarking across
    all ACS geography levels and demographic dimensions while isolating global trials
    with pooled baseline tables from US-only cohort rollups.
"""
from __future__ import annotations

SCHEMA_VERSION: int = 1
METHOD_VERSION: str = "catchments-multisite-1"

__all__ = [
    "METHOD_VERSION",
    "SCHEMA_VERSION",
]
