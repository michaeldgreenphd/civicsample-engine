"""Snapshot regression against the 2026-06-19 industry fixture — explicit
re-baseline semantics (phase 2.2). See sponsors/baseline.py.
"""
import pytest

from conftest import FIXTURE_PATH, FIXTURE_SHA256, RULES_PATH
from sponsors import baseline as bl
from sponsors import sponsor_roles as sr

# The README count targets: valid for the original rules only; kept as the
# documented origin of the fixture, asserted only when that sha is current.
ORIGINAL_RULES_SHA = "3e13bc5926f6f8bc5025151c60ea7dc8f40a5f7b8d7a209026ba29472e70601c"
README_TARGETS = {"Pfizer": (5605, 3847, 1802), "Merck & Co": (4445, 2323, 2130),
                  "Merck KGaA": (648, 420, 249), "Bristol-Myers Squibb": (2848, 1540, 1308)}


def test_fixture_file_integrity():
    assert bl.sha256_file(FIXTURE_PATH) == FIXTURE_SHA256, "fixture file changed; update conftest.FIXTURE_SHA256 deliberately"


def test_regression_or_explicit_rebaseline(shipped_rules, fixture_frame):
    rules_sha = bl.sha256_file(RULES_PATH)
    actual = bl.compute_baseline(fixture_frame, shipped_rules)
    uncovered = bl.uncovered_rules(fixture_frame, shipped_rules)
    expected = bl.load_expected()
    block = expected.get("baselines", {}).get(rules_sha)

    if block is None:
        bl.write_baseline(rules_sha, actual, uncovered, {
            "file": "tests/sponsors/fixtures/sponsors_fixture_20260619_industry.csv.gz",
            "sha256": FIXTURE_SHA256, "snapshot_date": "2026-06-19"})
        prev = sorted(expected.get("baselines", {}), key=lambda k: expected["baselines"][k].get("generated_at", ""))
        drift = bl.diff_blocks(expected["baselines"][prev[-1]], actual) if prev else []
        pytest.fail(
            "company_aliases.csv changed (sha256 " + rules_sha[:12] + "…) and had no baseline.\n"
            "A re-baseline block was GENERATED and written to tests/sponsors/expected_counts.json.\n"
            "Commit that file together with the rules change so the new counts are reviewed in the same diff, "
            "then re-run.\n"
            + (f"Changes vs previous baseline ({prev[-1][:12]}…):\n  " + "\n  ".join(drift) + "\n" if drift else "")
            + (f"Rules NOT covered by the fixture (work in production, not regression-tested): "
               f"{[u['pattern'] for u in uncovered]}\n" if uncovered else "All attribute rules are covered by the fixture.\n")
        )

    for section in ("index", "companies", "entities"):
        assert actual[section] == block[section], "\n".join(bl.diff_blocks(block, actual))
    if uncovered:
        print("not covered by fixture:", [u["pattern"] for u in uncovered])


def test_readme_targets_hold_for_the_original_rules(shipped_rules, fixture_frame):
    if bl.sha256_file(RULES_PATH) != ORIGINAL_RULES_SHA:
        pytest.skip("README targets documented the original rules only; see expected_counts.json for the current baseline")
    idx = sr.build_index(fixture_frame, shipped_rules)
    for company, (n_any, n_lead, n_collab) in README_TARGETS.items():
        assert (len(sr.trials(idx, company)), len(sr.trials(idx, company, role="lead")),
                len(sr.trials(idx, company, role="collaborator"))) == (n_any, n_lead, n_collab), company
    assert (len(idx), idx.canonical.nunique(), idx.nct_id.nunique()) == (77319, 98, 73345)


def test_any_le_lead_plus_collab_and_shortfall_is_both_roles(shipped_rules, fixture_frame):
    idx = sr.build_index(fixture_frame, shipped_rules)
    for company in idx.canonical.unique():
        t = sr.trials(idx, company)
        n_lead, n_collab = int(t.is_lead.sum()), int(t.is_collaborator.sum())
        assert len(t) <= n_lead + n_collab
        assert n_lead + n_collab - len(t) == int((t.is_lead & t.is_collaborator).sum())
