import pytest

from conftest import frame, rules_df
from sponsors import sponsor_roles as sr
from sponsors.audit_states import partition

NOTE = "test rule; decided by tests 2026"


def test_conflicting_rules_raise_unless_all_shared():
    sp = frame([("NCT1", "INDUSTRY", "lead", "Acme")])
    conflicting = rules_df([("Pfizer", "exact_literal", "Acme", "attribute", "no", NOTE),
                            ("Merck & Co", "exact_literal", "Acme", "attribute", "no", NOTE)])
    with pytest.raises(ValueError, match="conflicting rules for literal 'Acme'"):
        sr.build_index(sp, conflicting)
    # One shared=yes is not enough: EVERY claiming rule must be shared.
    half = rules_df([("Pfizer", "exact_literal", "Acme", "attribute", "yes", NOTE),
                     ("Merck & Co", "exact_literal", "Acme", "attribute", "no", NOTE)])
    with pytest.raises(ValueError, match="conflicting rules"):
        sr.build_index(sp, half)
    both = rules_df([("Pfizer", "exact_literal", "Acme", "attribute", "yes", NOTE),
                     ("Merck & Co", "exact_literal", "Acme", "attribute", "yes", NOTE)])
    idx = sr.build_index(sp, both)
    assert sorted(idx.canonical) == ["Merck & Co", "Pfizer"] and set(idx.shared) == {"yes"}


def test_partnership_literal_under_both_companies(shipped_rules):
    sp = frame([("NCT9", "INDUSTRY", "lead", "Bristol-Meyers Squibb & Pfizer")])
    idx = sr.build_index(sp, shipped_rules)
    assert sorted(idx.canonical) == ["Bristol-Myers Squibb", "Pfizer"]
    assert set(idx.shared) == {"yes"}
    assert set(idx.match_rule) == {"exact_literal"}


def test_both_roles_one_trial_appears_once(shipped_rules):
    sp = frame([("NCT1", "INDUSTRY", "lead", "Pfizer Inc."),
                ("NCT1", "INDUSTRY", "collaborator", "Wyeth is now a wholly owned subsidiary of Pfizer")])
    idx = sr.build_index(sp, shipped_rules)
    t = sr.trials(idx, "Pfizer")
    assert len(t) == 1 and bool(t.is_lead[0]) and bool(t.is_collaborator[0])
    assert len(sr.trials(idx, "Pfizer", role="lead")) == 1
    assert len(sr.trials(idx, "Pfizer", role="collaborator")) == 1
    # entities(): the as-registered split behind the toggle
    ents = sr.entities(idx, "Pfizer").set_index("entity")
    assert ents.loc["pfizer", "n_lead"] == 1 and ents.loc["wyeth", "n_collab"] == 1


def test_audit_three_states_are_disjoint_and_partition_covers_everything():
    sp = frame([("NCT1", "INDUSTRY", "lead", "Merck Sharp & Dohme"),
                ("NCT2", "INDUSTRY", "lead", "Merck KGaA"),
                ("NCT3", "OTHER", "lead", "Merck Family Foundation"),
                ("NCT4", "OTHER", "lead", "University of Nowhere")])
    rules = rules_df([("Merck & Co", "exact_literal", "Merck Sharp & Dohme", "attribute", "no", NOTE),
                      ("Merck & Co", "exact_literal", "Merck KGaA", "exclude", "no", "reviewed: KGaA is a different company")])
    a = sr.audit(sp, rules, "Merck & Co", stem="merck")
    names = {k: set(v["name"]) for k, v in a.items()}
    assert names["attributed"] == {"Merck Sharp & Dohme"}
    assert names["reviewed_excluded"] == {"Merck KGaA"}
    assert names["unreviewed_candidates"] == {"Merck Family Foundation"}
    for x in names:
        for y in names:
            assert x == y or not (names[x] & names[y])
    p = partition(sp, rules)
    assert p["attributed"] | p["reviewed_excluded"] | p["unreviewed"] == set(sp["name"])
    assert not (p["attributed"] & p["reviewed_excluded"]) and not (p["unreviewed"] & p["attributed"])
    assert p["unreviewed"] == {"Merck Family Foundation", "University of Nowhere"}


def test_role_and_view_vocabularies_are_closed(shipped_rules):
    idx = sr.build_index(frame([("NCT1", "INDUSTRY", "lead", "Pfizer")]), shipped_rules)
    with pytest.raises(ValueError):
        sr.trials(idx, "Pfizer", role="sponsor")
    with pytest.raises(ValueError):
        sr.trials(idx, "Pfizer", view="owner")
    assert set(idx.role) <= {"lead", "collaborator"}
