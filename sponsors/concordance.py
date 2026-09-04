"""Generic parity harness: our adapter's index vs an AACT-derived index.

Given two indexes built by sponsor_roles.build_index() from (a) our adapter
frame and (b) an AACT fixture frame, restricted to the nct_ids both source
frames hold (pass pull_trials / fixture_trials), compare the (nct_id, canonical, role) sets and report a three-way
split: agree / fixture_only / pull_only, with the literals driving each
disagreement side and a cause hint per pair so registry edits can be told
apart from adapter defects.

Cause hints (per disagreeing pair, decided from the OTHER side's rows for
that trial):
    role_differs        same literal on the same trial, other role -> adapter
                        or loader semantics defect (investigate)
    literal_absent      the literal is not on that trial on the other side ->
                        registry edit after the fixture date (or a rules
                        difference if rules_sha256 differ)
    canonical_differs   same literal, same role, different canonical -> rules
                        version mismatch
Sponsors is the first instance; later tables reuse this with their own key
columns.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

KEY = ["nct_id", "canonical", "role"]


def _pairs(index: pd.DataFrame) -> set:
    return set(map(tuple, index[KEY].itertuples(index=False, name=None)))


def _cause(row, other: pd.DataFrame) -> str:
    o = other[other.nct_id == row.nct_id]
    same_lit = o[o.literal_name == row.literal_name]
    if same_lit.empty:
        return "literal_absent"
    if (same_lit.role == row.role).any():
        return "canonical_differs"
    return "role_differs"


def concordance(pull_index: pd.DataFrame, fixture_index: pd.DataFrame,
                top_n: int = 10, pull_trials: set | None = None,
                fixture_trials: set | None = None) -> dict:
    """Compare two indexes on the trials both sides HOLD.

    Pass `pull_trials` / `fixture_trials` (the nct_ids of the two source
    frames) so the comparison is scoped by what each side saw, not by what
    each side attributed: a trial the adapter lost entirely then shows up as
    fixture_only instead of vanishing from the report. Without them the
    index nct_ids are used (the older, weaker scoping).
    """
    p_trials = set(pull_trials) if pull_trials is not None else set(pull_index.nct_id)
    f_trials = set(fixture_trials) if fixture_trials is not None else set(fixture_index.nct_id)
    shared = p_trials & f_trials
    p = pull_index[pull_index.nct_id.isin(shared)].drop_duplicates(KEY + ["literal_name"])
    f = fixture_index[fixture_index.nct_id.isin(shared)].drop_duplicates(KEY + ["literal_name"])
    pp, fp = _pairs(p), _pairs(f)
    agree = pp & fp
    pull_only = pp - fp
    fixture_only = fp - pp

    def side(index_rows: pd.DataFrame, only: set, other: pd.DataFrame) -> dict:
        rows = index_rows[[tuple(x) in only for x in index_rows[KEY].itertuples(index=False, name=None)]]
        # one cause per (nct_id, canonical, role) pair, not per literal row
        causes = Counter(_cause(r, other) for r in rows.drop_duplicates(KEY).itertuples())
        top = (rows.groupby("literal_name").nct_id.nunique()
               .sort_values(ascending=False).head(top_n))
        return {"n_pairs": len(only), "causes": dict(causes),
                "top_literals": [{"literal": k, "n_trials": int(v)} for k, v in top.items()],
                "rows": rows}

    return {
        "n_shared_trials": len(shared),
        "agree": len(agree),
        "pull_only": side(p, pull_only, f),
        "fixture_only": side(f, fixture_only, p),
    }


def cohort_count_table(index: pd.DataFrame, companies: list[str]) -> pd.DataFrame:
    """any / lead / collaborator trial counts per company on this index."""
    from . import sponsor_roles as sr
    out = []
    for c in companies:
        out.append({"company": c,
                    "any": len(sr.trials(index, c)),
                    "lead": len(sr.trials(index, c, role="lead")),
                    "collaborator": len(sr.trials(index, c, role="collaborator"))})
    return pd.DataFrame(out)
