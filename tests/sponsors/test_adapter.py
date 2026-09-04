import pandas as pd

from conftest import FIXTURE_PATH, RECORDS_SAMPLE_PATH, frame
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
    # A5: each defect class on its own line, with its own count
    lines = log.lines()
    assert len(lines) == 5
    assert any('"Unknown"' in ln and ln.endswith(": 1") for ln in lines)
    assert any(ln.startswith("lead names empty") and ln.endswith(": 1") for ln in lines)
    assert any(ln.startswith("collaborator rows dropped") and ln.endswith(": 1") for ln in lines)
    assert any("outside AACT vocabulary" in ln and "Unknown=1" in ln and "WEIRD_CLASS=1" in ln for ln in lines)


def test_parity_with_hand_built_aact_frame():
    recs = [api_record("NCT1", "Pfizer Inc.", "INDUSTRY", [("Wyeth", "INDUSTRY")]),
            api_record("NCT2", "Merck KGaA", "INDUSTRY", [])]
    f, _ = records_to_frame(recs)
    expected = frame(aact_rows("NCT1", "Pfizer Inc.", "INDUSTRY", [("Wyeth", "INDUSTRY")])
                     + aact_rows("NCT2", "Merck KGaA", "INDUSTRY", []))
    pd.testing.assert_frame_equal(f.reset_index(drop=True), expected)


def test_parity_with_aact_loader_output_on_the_fixture():
    """C1: the adapter reproduces app/models/sponsor.rb. The sample holds
    stored records (our extractor's shape) for trials whose rows in the AACT
    2026-06-19 fixture were unchanged at the 2026-08-30 pull; the adapter
    must emit exactly the fixture's rows for them."""
    import gzip
    import json

    with gzip.open(RECORDS_SAMPLE_PATH, "rt") as fh:
        sample = json.load(fh)
    fixture = pd.read_csv(FIXTURE_PATH, sep="|", dtype=str, keep_default_na=False)
    ncts = {r["nct_id"] for r in sample["records"]}
    assert len(ncts) == sample["n"] >= 250
    ours, log = records_to_frame(sample["records"])
    theirs = fixture[fixture.nct_id.isin(ncts)]
    cols = ["nct_id", "agency_class", "lead_or_collaborator", "name"]
    a = ours[cols].sort_values(cols).reset_index(drop=True)
    b = theirs[cols].sort_values(cols).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    assert log.n_rows_emitted == len(theirs)
    # the sample exercises collaborators, not just lead rows
    assert (b.lead_or_collaborator == "collaborator").sum() >= 100


def test_concordance_scopes_by_source_trials_so_a_lost_trial_is_reported():
    rules = pd.DataFrame([("Pfizer", "exact_literal", "Pfizer", "attribute", "no", NOTE)],
                         columns=["canonical", "rule_type", "pattern", "status", "shared", "note"]).astype(str)
    pull = frame([("NCT1", "INDUSTRY", "lead", "Pfizer")])
    fix = frame([("NCT1", "INDUSTRY", "lead", "Pfizer"),
                 ("NCT2", "INDUSTRY", "lead", "Pfizer")])          # NCT2 lost by the adapter entirely
    pi, fi = sr.build_index(pull, rules), sr.build_index(fix, rules)
    weak = concordance(pi, fi)                                     # index-scoped: NCT2 invisible
    assert weak["n_shared_trials"] == 1 and weak["fixture_only"]["n_pairs"] == 0
    strong = concordance(pi, fi, pull_trials={"NCT1", "NCT2"}, fixture_trials=set(fix.nct_id))
    assert strong["n_shared_trials"] == 2 and strong["fixture_only"]["n_pairs"] == 1
    assert strong["fixture_only"]["causes"] == {"literal_absent": 1}


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
