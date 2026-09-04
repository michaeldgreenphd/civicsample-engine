import pandas as pd
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


# ── added with the audit-tab phase (review findings on PR #5) ──────────────

def test_attribute_and_exclude_on_one_literal_is_a_build_time_conflict():
    """Invariant 6 + 2: a literal reached by an attribute rule AND an exclude
    rule raises when the bridge is built, not only in the Sunday audit."""
    sp = frame([("NCT1", "INDUSTRY", "lead", "Merck KGaA")])
    rules = rules_df([("Merck & Co", "exact_normalized", "merck kgaa", "attribute", "no", NOTE),
                      ("Merck & Co", "exact_literal", "Merck KGaA", "exclude", "no", "reviewed: not ours")])
    with pytest.raises(ValueError, match="conflicting rules for literal 'Merck KGaA'.*excluded"):
        sr.build_index(sp, rules)
    with pytest.raises(ValueError, match="conflicting rules"):
        sr.audit(sp, rules, "Merck & Co", stem="merck")
    with pytest.raises(ValueError, match="conflicting rules"):
        partition(sp, rules)


def test_literal_hidden_by_bridge_dedupe_is_still_attributed():
    """Two literals on one trial that collapse to the same (canonical, entity,
    role) keep one bridge row; the review state must not read that as
    'unreviewed' for the literal that lost the dedupe."""
    sp = frame([("NCT1", "INDUSTRY", "collaborator", "Pfizer"),
                ("NCT1", "INDUSTRY", "collaborator", "Pfizer Inc.")])
    rules = rules_df([("Pfizer", "exact_normalized", "pfizer", "attribute", "no", NOTE)])
    idx = sr.build_index(sp, rules)
    assert len(idx) == 1                      # the bridge keeps one row per (trial, canonical, entity, role)
    p = partition(sp, rules)
    assert p["attributed"] == {"Pfizer", "Pfizer Inc."} and p["unreviewed"] == set()
    m = sr.match_literals(sr.literal_universe(sp), rules)
    assert set(m.name) == {"Pfizer", "Pfizer Inc."}


def test_role_vocabulary_is_enforced_where_the_bridge_is_built(shipped_rules):
    sp = frame([("NCT1", "INDUSTRY", "sponsor", "Pfizer")])
    with pytest.raises(ValueError, match="lead_or_collaborator must be one of"):
        sr.build_index(sp, shipped_rules)


def test_no_substring_matching_negative_examples():
    """Invariant 1: a literal that merely CONTAINS a pattern is not attributed."""
    sp = frame([("NCT1", "INDUSTRY", "lead", "Pfizer Inc."),          # normalizes to 'pfizer' -> match
                ("NCT2", "OTHER", "lead", "Pfizer Foundation"),        # contains 'pfizer' -> no
                ("NCT3", "INDUSTRY", "lead", "Pfizerx"),               # prefix -> no
                ("NCT4", "INDUSTRY", "lead", "The Pfizer"),            # not the exact literal -> no
                ("NCT5", "INDUSTRY", "collaborator", "Wyeth Pharmaceuticals, a subsidiary of Pfizer Inc")])
    rules = rules_df([("Pfizer", "exact_normalized", "pfizer", "attribute", "no", NOTE),
                      ("Pfizer", "subsidiary_of", "pfizer", "attribute", "no", NOTE)])
    idx = sr.build_index(sp, rules)
    assert set(idx.literal_name) == {"Pfizer Inc.", "Wyeth Pharmaceuticals, a subsidiary of Pfizer Inc"}
    assert set(idx.match_rule) == {"exact_normalized", "subsidiary_of"}
    p = partition(sp, rules)
    assert p["unreviewed"] == {"Pfizer Foundation", "Pfizerx", "The Pfizer"}


def test_audit_lists_stem_literals_attributed_to_another_company_and_stem_is_not_a_regex():
    sp = frame([("NCT1", "INDUSTRY", "lead", "Merck Sharp & Dohme"),
                ("NCT2", "INDUSTRY", "lead", "Merck KGaA"),
                ("NCT3", "OTHER", "lead", "Merck Family Foundation")])
    rules = rules_df([("Merck & Co", "exact_literal", "Merck Sharp & Dohme", "attribute", "no", NOTE),
                      ("Merck KGaA", "exact_literal", "Merck KGaA", "attribute", "no", NOTE)])
    a = sr.audit(sp, rules, "Merck & Co", stem="merck")
    assert set(a["attributed"].name) == {"Merck Sharp & Dohme"}
    assert set(a["attributed_elsewhere"].name) == {"Merck KGaA"}
    assert list(a["attributed_elsewhere"].canonicals) == ["Merck KGaA"]
    assert set(a["unreviewed_candidates"].name) == {"Merck Family Foundation"}
    # a stem with regex metacharacters is a plain substring, never a pattern error
    b = sr.audit(sp, rules, "Merck & Co", stem="sharp & (dohme")
    assert all(len(v) == 0 for v in b.values()) or set(b["attributed"].name) == {"Merck Sharp & Dohme"}
    c = sr.audit(sp, rules, "Merck & Co", stem="sharp & dohme")
    assert set(c["attributed"].name) == {"Merck Sharp & Dohme"}


def test_company_flags_partnership_literal_dedupes_on_nct_id(shipped_rules):
    """Invariant 5 on the Python side: one trial with a shared=yes literal
    yields ONE row flagged for both companies, not two rows."""
    sp = frame([("NCT9", "INDUSTRY", "lead", "Bristol-Meyers Squibb & Pfizer"),
                ("NCT1", "INDUSTRY", "lead", "Pfizer Inc.")])
    idx = sr.build_index(sp, shipped_rules)
    w = sr.company_flags(idx, ["Pfizer", "Bristol-Myers Squibb"]).set_index("nct_id")
    assert len(w) == 2 and w.index.is_unique
    assert bool(w.loc["NCT9", "pfizer_any"]) and bool(w.loc["NCT9", "bristol_myers_squibb_any"])
    assert bool(w.loc["NCT1", "pfizer_lead"]) and not bool(w.loc["NCT1", "bristol_myers_squibb_any"])


def test_entities_order_and_counts_match_the_browser_filter(shipped_rules):
    """A3/parity: sponsor_roles.entities() and company_filter.js entitiesFor()
    return the same rows in the same order, including ties. Both read this
    fixture; tests/sponsors/company_filter.test.mjs asserts the JS half."""
    import json
    import os

    from conftest import ROOT
    fx = json.load(open(os.path.join(ROOT, "tests", "sponsors", "fixtures", "entities_parity.json")))
    idx = pd.DataFrame(fx["rows"], columns=fx["columns"])
    got = sr.entities(idx, fx["canonical"]).to_dict("records")
    assert got == fx["expected"]
