"""The three review states of every literal sponsor name (README invariant 2).

    attributed         a status=attribute rule maps the literal to a canonical
    reviewed_excluded  a status=exclude rule matched it: a human said "not ours"
    unreviewed         no rule of either kind touches it

The three sets partition the universe of literals present in a pull. Every
state here is decided by sponsor_roles.match_literals — the same call that
builds the bridge — never read back off the bridge, whose per-trial dedupe
can drop a literal that shares a (canonical, entity, role) with another.
sponsor_roles.audit() is the per-company / per-stem view of the same states.
"""
from __future__ import annotations

import pandas as pd

from . import sponsor_roles as sr

literal_universe = sr.literal_universe


def rule_matches(frame: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """The literal-level attribution decision for this frame: one row per
    (name, canonical) with match_rule and shared. Raises on rule conflicts."""
    return sr.match_literals(literal_universe(frame), rules)


def canonicals_by_literal(matches: pd.DataFrame, name_col: str = "name") -> pd.DataFrame:
    """Collapse (name, canonical, shared) rows to one row per literal:
    canonicals ('|'-joined, sorted) and shared (yes if any claim is shared).
    Works on match_literals output (name) and on a bridge (literal_name)."""
    if matches is None or matches.empty:
        return pd.DataFrame(columns=[name_col, "canonicals", "shared"]).set_index(name_col)
    return matches.groupby(name_col).agg(
        canonicals=("canonical", lambda c: "|".join(sorted(set(c)))),
        shared=("shared", lambda s: "yes" if (s == "yes").any() else "no"))


def partition(frame: pd.DataFrame, rules: pd.DataFrame) -> dict:
    """Return {'attributed', 'reviewed_excluded', 'unreviewed'}: disjoint sets
    of literal names whose union is every literal in `frame`.

    match_literals raises on an attribute/exclude overlap, so the disjointness
    check below is a second line of defence, not the first.
    """
    uniq = literal_universe(frame)
    universe = set(uniq["name"])
    matched = sr.match_literals(uniq, rules)
    attributed = set(matched["name"]) & universe
    excluded: set = set()
    for _, r in rules[rules.status == "exclude"].iterrows():
        excluded.update(sr._rule_hits(uniq, r))
    excluded &= universe
    both = attributed & excluded
    if both:
        raise ValueError(
            f"{len(both)} literal(s) are both attributed and reviewed-excluded; "
            f"fix the rules (first: {sorted(both)[0]!r})"
        )
    unreviewed = universe - attributed - excluded
    return {"attributed": attributed, "reviewed_excluded": excluded, "unreviewed": unreviewed}


def summarize_literals(frame: pd.DataFrame, names: set) -> pd.DataFrame:
    """Per-literal trial counts, roles, and class for a set of names, sorted
    by trial count descending — the shape of every audit file."""
    sub = frame[frame["name"].isin(names)]
    if sub.empty:
        return pd.DataFrame(columns=["name", "n_trials", "roles", "agency_class"])
    return (
        sub.groupby("name")
        .agg(n_trials=("nct_id", "nunique"),
             roles=("lead_or_collaborator", lambda r: ",".join(sorted(set(r)))),
             agency_class=("agency_class", "first"))
        .reset_index()
        .sort_values(["n_trials", "name"], ascending=[False, True])
        .reset_index(drop=True)
    )
