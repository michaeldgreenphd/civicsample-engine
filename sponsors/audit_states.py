"""The three review states of every literal sponsor name (README invariant 2).

    attributed         a status=attribute rule maps the literal to a canonical
    reviewed_excluded  a status=exclude rule matched it: a human said "not ours"
    unreviewed         no rule of either kind touches it

The three sets partition the universe of literals present in a pull. This
module computes the partition once so the weekly audit, the tests, and any
future consumer agree on it; sponsor_roles.audit() is the per-company /
per-stem view of the same states.
"""
from __future__ import annotations

import pandas as pd

from . import sponsor_roles as sr


def literal_universe(frame: pd.DataFrame) -> pd.DataFrame:
    """Distinct literal names with the normalized forms rules match on."""
    uniq = pd.DataFrame({"name": frame["name"].dropna().unique()})
    uniq["norm"] = uniq["name"].map(sr.normalize)
    uniq["parent_norm"] = uniq["name"].map(sr.parsed_parent)
    return uniq


def partition(frame: pd.DataFrame, rules: pd.DataFrame, index: pd.DataFrame | None = None) -> dict:
    """Return {'attributed', 'reviewed_excluded', 'unreviewed'}: disjoint sets
    of literal names whose union is every literal in `frame`.

    `index` may be passed to avoid rebuilding it; otherwise build_index runs
    (and raises on rule conflicts, as it must).
    """
    if index is None:
        index = sr.build_index(frame, rules)
    uniq = literal_universe(frame)
    universe = set(uniq["name"])
    attributed = set(index["literal_name"]) & universe
    excluded: set = set()
    for _, r in rules[rules.status == "exclude"].iterrows():
        excluded.update(sr._rule_hits(uniq, r))
    excluded &= universe
    # A literal both attributed and excluded would be a rules bug: surface it.
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
