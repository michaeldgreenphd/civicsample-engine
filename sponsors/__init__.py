"""Company-level sponsor attribution for CivicSample.

Layout:
    sponsor_roles.py     matching + filtering module (bundle v3, amended: A6 guard)
    company_aliases.csv  the curated rules — schema, versioned, PR-gated
    acquisitions.csv     ownership timeline (dates blank until primary-sourced)
    adapter.py           API v2 records -> AACT-shaped sponsors frame (cites AACT source)
    audit_states.py      the three-state partition of every literal sponsor name
    concordance.py       generic parity harness: our adapter vs an AACT fixture
    company_filter.js    browser-side filter (not loaded by the site yet)
"""
