"""Explicit regression re-baselining for the sponsor rules (phase 2.2).

Expected counts live in tests/sponsors/expected_counts.json keyed by the
sha256 of company_aliases.csv. CI behavior (tests/sponsors/test_fixture_regression.py):

    rules unchanged (sha has a block)  -> assert the fixture reproduces it
    rules changed  (no block for sha)  -> build the block from the fixture,
                                          WRITE it, and FAIL: the re-baseline
                                          must be committed with the rules
                                          change, reviewed in the same diff
    block lacks a section in SECTIONS  -> assert the sections it has, then
    (written before compute_baseline      WRITE the missing ones into that
     produced that section)               block and FAIL until committed
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


# The sections every baseline block pins, in comparison order. A block written
# before a section existed is completed by backfill_sections() (see
# tests/sponsors/test_fixture_regression.py); a missing section is never
# compared as if it were empty.
SECTIONS = ("index", "companies", "entities", "shared")


def compute_baseline(fixture: pd.DataFrame, rules: pd.DataFrame) -> dict:
    """Everything the regression compares: index totals, per-company counts
    by scope, per-entity counts (the as_registered view), and the bridge's
    `shared` marker per company."""
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
    # `shared` pin. The counts above cannot see the bridge's `shared` column:
    # a rule or matching change that flips a row's shared value moves no
    # attribution, yet changes which trials a cross-company sum must dedupe
    # on nct_id. For each canonical with at least one shared=yes bridge row,
    # pin the number of those rows and the trials they cover by scope (the
    # same trials() semantics as `companies`). `rows` catches a flip that
    # leaves the trial counts alone (a trial that already has another
    # shared=yes row for that company in that role). A canonical that loses
    # its last shared=yes row drops out of the section, and the comparison
    # reports it. Only "yes" is counted: within the bridge "no" is its
    # complement, so a single flip either way changes `rows`.
    yes = idx[idx["shared"] == "yes"]
    shared = {}
    for c in sorted(yes.canonical.unique()):
        shared[c] = {"rows": int((yes.canonical == c).sum()),
                     "any": len(sr.trials(yes, c)),
                     "lead": len(sr.trials(yes, c, role="lead")),
                     "collaborator": len(sr.trials(yes, c, role="collaborator"))}
    return {
        "index": {"rows": int(len(idx)), "canonicals": int(idx.canonical.nunique()),
                  "trials": int(idx.nct_id.nunique())},
        "companies": companies,
        "entities": entities,
        "shared": shared,
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
    _dump(data, path)


def backfill_sections(rules_sha: str, actual: dict, sections: list[str],
                      path: str = EXPECTED_PATH) -> None:
    """Add `sections` from `actual` (a compute_baseline result) to the
    existing block for `rules_sha`: the case of a block written before
    compute_baseline produced those sections.

    The block's own generated_at and its other sections are left as they
    are. Each added section is stamped under `sections_backfilled`, so the
    block records which numbers are younger than the rest. The caller must
    first check that the block's existing sections match `actual` (the
    regression test does), so a section is never added from counts that
    have drifted from the block. A section the block already has is never
    overwritten; that would be a silent re-baseline."""
    data = load_expected(path)
    block = data.get("baselines", {}).get(rules_sha)
    if block is None:
        raise KeyError(f"no baseline block for rules sha {rules_sha[:12]}… in {path}")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in sections:
        if s in block:
            raise ValueError(f"baseline block {rules_sha[:12]}… already has section {s!r}; "
                             "refusing to overwrite it")
        block[s] = actual[s]
        block.setdefault("sections_backfilled", {})[s] = {
            "generated_at": stamp,
            "generated_by": "tests/sponsors/test_fixture_regression.py "
                            "(section added to an existing block)",
        }
    _dump(data, path)


def _dump(data: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def diff_blocks(expected: dict, actual: dict, limit: int = 15) -> list[str]:
    """Human-readable differences between two baseline blocks."""
    out = []
    for section in SECTIONS:
        if section not in expected:
            # A block written before this section existed: say so once rather
            # than list every value of the section as new.
            out.append(f"{section}: not in the earlier block, nothing to compare")
            continue
        e, a = expected[section], actual.get(section, {})
        for k in sorted(set(e) | set(a)):
            if e.get(k) != a.get(k):
                out.append(f"{section}.{k}: expected {e.get(k)} got {a.get(k)}")
            if len(out) >= limit:
                out.append("…"); return out
    return out
