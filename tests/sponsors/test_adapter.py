import pandas as pd

from conftest import frame
from sponsors import sponsor_roles as sr
from sponsors.adapter import (AACT_LOADER_COMMIT, UNKNOWN_LEAD_LITERAL,
                              record_rows, records_to_frame)
from sponsors.concordance import concordance

NOTE = "test rule; decided by tests 2026"


def api_record(nct, lead, lead_class, collabs):
    """A stored record in our extractor's shape (src/utils.py:467-479)."""
    return {"nct_id": nct, "lead_sponsor_name": lead, "sponsor_class": lead_class,
            "collaborators": [{"name": n, "class": c} for n, c in collabs]}


def aact_rows(nct, lead, lead_class, collabs):
    """What app/models/sponsor.rb would write: lead row, then one per collaborator."""
    rows = [(nct, lead_class, "lead", lead)]
    rows += [(nct, c, "collaborator", n) for n, c in collabs]
    return rows


def test_record_rows_reproduces_sponsor_rb_one_for_one():
    rec = api_record("NCT1", "Pfizer", "INDUSTRY", [("Wyeth", "INDUSTRY"), ("NIH", "NIH")])
    got = [(r["nct_id"], r["agency_class"], r["lead_or_collaborator"], r["name"]) for r in record_rows(rec)]
    assert got == aact_rows("NCT1", "Pfizer", "INDUSTRY", [("Wyeth", "INDUSTRY"), ("NIH", "NIH")])
    assert AACT_LOADER_COMMIT == "b8c16d3395ba7e548d852bccf3b47a7ff22af5f5"


def test_records_to_frame_policy_counts_each_defect_separately():
    recs = [
        api_record("NCT1", UNKNOWN_LEAD_LITERAL, "Unknown", [("", ""), ("Acme", "INDUSTRY")]),
        api_record("NCT2", "", "OTHER", [("Beta", "WEIRD_CLASS")]),
        api_record("NCT3", "Pfizer", "INDUSTRY", []),
    ]
    f, log = records_to_frame(recs)
    assert log.n_records == 3
    assert log.n_unknown_lead_names == 1 and log.n_empty_lead_names == 1
    assert log.n_rows_dropped_empty_collaborator_name == 1
    assert log.novel_agency_classes == {"Unknown": 1, "WEIRD_CLASS": 1}
    # kept, not dropped: the Unknown lead, the empty lead, and the novel-class collaborator
    assert (f.name == UNKNOWN_LEAD_LITERAL).sum() == 1
    assert (f.name == "").sum() == 1
    assert (f.agency_class == "WEIRD_CLASS").sum() == 1
    assert list(f.columns) == ["nct_id", "agency_class", "lead_or_collaborator", "name"]
    assert len(log.lines()) == 5


def test_parity_with_hand_built_aact_frame():
    recs = [api_record("NCT1", "Pfizer Inc.", "INDUSTRY", [("Wyeth", "INDUSTRY")]),
            api_record("NCT2", "Merck KGaA", "INDUSTRY", [])]
    f, _ = records_to_frame(recs)
    expected = frame(aact_rows("NCT1", "Pfizer Inc.", "INDUSTRY", [("Wyeth", "INDUSTRY")])
                     + aact_rows("NCT2", "Merck KGaA", "INDUSTRY", []))
    pd.testing.assert_frame_equal(f.reset_index(drop=True), expected)


def test_concordance_three_way_split_and_cause_hints():
    rules = pd.DataFrame([
        ("Pfizer", "exact_literal", "Pfizer", "attribute", "no", NOTE),
        ("Wyeth", "exact_literal", "Wyeth", "attribute", "no", NOTE)],
        columns=["canonical", "rule_type", "pattern", "status", "shared", "note"]).astype(str)
    pull = frame([("NCT1", "INDUSTRY", "lead", "Pfizer"),          # agree
                  ("NCT2", "INDUSTRY", "collaborator", "Pfizer"),  # role differs vs fixture
                  ("NCT3", "INDUSTRY", "lead", "Wyeth"),           # pull-only: literal absent in fixture
                  ("NCT9", "INDUSTRY", "lead", "Pfizer")])         # not shared trial -> ignored
    fix = frame([("NCT1", "INDUSTRY", "lead", "Pfizer"),
                 ("NCT2", "INDUSTRY", "lead", "Pfizer"),
                 ("NCT3", "INDUSTRY", "lead", "Pfizer")])          # fixture-only: Pfizer on NCT3 absent in pull
    c = concordance(sr.build_index(pull, rules), sr.build_index(fix, rules))
    assert c["n_shared_trials"] == 3 and c["agree"] == 1
    assert c["pull_only"]["n_pairs"] == 2 and c["fixture_only"]["n_pairs"] == 2
    assert c["pull_only"]["causes"] == {"role_differs": 1, "literal_absent": 1}
    assert c["fixture_only"]["causes"] == {"role_differs": 1, "literal_absent": 1}
    assert c["pull_only"]["top_literals"][0]["literal"] in {"Pfizer", "Wyeth"}
