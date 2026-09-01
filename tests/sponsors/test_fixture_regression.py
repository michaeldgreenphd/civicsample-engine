"""Snapshot regression against the 2026-06-19 AACT fixture (README count targets).

Runs unconditionally. The fixture holds exactly the sponsor rows attributed by
company_aliases.csv at sha256 3e13bc59... (test_rules_loading guards the sha);
it contains no unmatched literals, so audit coverage is tested synthetically.
"""
from sponsors import sponsor_roles as sr

TARGETS = {
    "Pfizer": (5605, 3847, 1802),
    "Merck & Co": (4445, 2323, 2130),
    "Merck KGaA": (648, 420, 249),
    "Bristol-Myers Squibb": (2848, 1540, 1308),
}


def test_count_table_exact(shipped_rules, fixture_frame):
    idx = sr.build_index(fixture_frame, shipped_rules)
    for company, (n_any, n_lead, n_collab) in TARGETS.items():
        assert len(sr.trials(idx, company)) == n_any, company
        assert len(sr.trials(idx, company, role="lead")) == n_lead, company
        assert len(sr.trials(idx, company, role="collaborator")) == n_collab, company


def test_index_totals_and_wyeth(shipped_rules, fixture_frame):
    idx = sr.build_index(fixture_frame, shipped_rules)
    assert (len(idx), idx.canonical.nunique(), idx.nct_id.nunique()) == (77319, 98, 73345)
    w = sr.trials(idx, "wyeth", view="as_registered")
    assert (len(w), int(w.is_lead.sum()), int(w.is_collaborator.sum())) == (626, 484, 142)


def test_any_le_lead_plus_collab_and_shortfall_is_both_roles(shipped_rules, fixture_frame):
    idx = sr.build_index(fixture_frame, shipped_rules)
    for company in idx.canonical.unique():
        t = sr.trials(idx, company)
        n_lead, n_collab = int(t.is_lead.sum()), int(t.is_collaborator.sum())
        both = int((t.is_lead & t.is_collaborator).sum())
        assert len(t) <= n_lead + n_collab
        assert n_lead + n_collab - len(t) == both
