"""Explicit regression re-baselining for the sponsor rules (phase 2.2).

Expected counts live in tests/sponsors/expected_counts.json keyed by the
sha256 of company_aliases.csv. CI behavior (tests/sponsors/test_fixture_regression.py):

    rules unchanged (sha has a block)  -> assert the fixture reproduces it
    rules changed  (no block for sha)  -> build the block from the fixture,
                                          WRITE it, and FAIL: the re-baseline
                                          must be committed with the rules
                                          change, reviewed in the same diff
    rule matching no fixture literal   -> reported as "not covered by fixture"
                                          (works in production; not regression
                                          tested); never fails on its own

The fixture (2026-06-19 AACT sponsors rows with agency_class in
INDUSTRY/UNKNOWN/AMBIG/null plus every row attributed under the rules at the
time) covers every industry-plausible literal, so a new industry rule can be
re-baselined without regenerating anything from the 942k-row file.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import pandas as pd

from . import sponsor_roles as sr
from .audit_states import literal_universe

EXPECTED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "tests", "sponsors", "expected_counts.json")


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def uncovered_rules(fixture: pd.DataFrame, rules: pd.DataFrame) -> list[dict]:
    """status=attribute rules whose pattern matches no literal in the fixture."""
    uniq = literal_universe(fixture)
    out = []
    for _, r in rules[rules.status == "attribute"].iterrows():
        if len(sr._rule_hits(uniq, r)) == 0:
            out.append({"canonical": r.canonical, "rule_type": r.rule_type, "pattern": r.pattern})
    return out


def compute_baseline(fixture: pd.DataFrame, rules: pd.DataFrame) -> dict:
    """Everything the regression compares: index totals, per-company counts
    by scope, per-entity counts (the as_registered view)."""
    idx = sr.build_index(fixture, rules)
    companies = {}
    for c in sorted(idx.canonical.unique()):
        companies[c] = {"any": len(sr.trials(idx, c)),
                        "lead": len(sr.trials(idx, c, role="lead")),
                        "collaborator": len(sr.trials(idx, c, role="collaborator"))}
    entities = {}
    for e in sorted(idx.entity.unique()):
        t = sr.trials(idx, e, view="as_registered")
        entities[e] = {"any": len(t), "lead": int(t.is_lead.sum()), "collaborator": int(t.is_collaborator.sum())}
    return {
        "index": {"rows": int(len(idx)), "canonicals": int(idx.canonical.nunique()),
                  "trials": int(idx.nct_id.nunique())},
        "companies": companies,
        "entities": entities,
    }


def load_expected(path: str = EXPECTED_PATH) -> dict:
    if not os.path.exists(path):
        return {"fixture": {}, "baselines": {}}
    with open(path) as f:
        return json.load(f)


def write_baseline(rules_sha: str, block: dict, uncovered: list[dict], fixture_meta: dict,
                   path: str = EXPECTED_PATH) -> None:
    data = load_expected(path)
    data["fixture"] = fixture_meta
    data.setdefault("baselines", {})[rules_sha] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "tests/sponsors/test_fixture_regression.py (auto re-baseline on rules change)",
        "not_covered_by_fixture": uncovered,
        **block,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def diff_blocks(expected: dict, actual: dict, limit: int = 15) -> list[str]:
    """Human-readable differences between two baseline blocks."""
    out = []
    for section in ("index", "companies", "entities"):
        e, a = expected.get(section, {}), actual.get(section, {})
        for k in sorted(set(e) | set(a)):
            if e.get(k) != a.get(k):
                out.append(f"{section}.{k}: expected {e.get(k)} got {a.get(k)}")
            if len(out) >= limit:
                out.append("…"); return out
    return out
