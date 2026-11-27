"""sponsors/baseline.py: the `shared` section and completing an older block.

Fictional sponsor names and trial ids (NCT99000001..) only. The pinned
2026-06-19 fixture is exercised by test_fixture_regression.py.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from conftest import frame, rules_df
from sponsors import baseline as bl

NOTE = "test rule; decided by tests 2026"
PARTNER = "Fictional Pharma & Example Bio"   # normalizes to "fictional pharma and example bio"


def _sponsors() -> pd.DataFrame:
    return frame([
        ("NCT99000001", "INDUSTRY", "lead", PARTNER),
        ("NCT99000002", "INDUSTRY", "collaborator", PARTNER),
        ("NCT99000002", "INDUSTRY", "lead", "Fictional Pharma Inc."),   # normalizes to "fictional pharma"
        ("NCT99000003", "INDUSTRY", "lead", "Fictional Solo"),
    ])


def _rules(solo_shared: str = "no") -> pd.DataFrame:
    return rules_df([
        ("Fictional Pharma", "exact_literal", PARTNER, "attribute", "yes", NOTE),
        ("Example Bio", "exact_literal", PARTNER, "attribute", "yes", NOTE),
        ("Fictional Pharma", "exact_normalized", "fictional pharma", "attribute", "no", NOTE),
        ("Fictional Solo", "exact_literal", "Fictional Solo", "attribute", solo_shared, NOTE),
    ])


def test_shared_section_counts_shared_yes_rows_and_trials_per_company() -> None:
    block = bl.compute_baseline(_sponsors(), _rules())
    assert tuple(block) == bl.SECTIONS
    # Only canonicals with a shared=yes bridge row appear. Fictional Pharma's
    # lead row on NCT99000002 comes from its shared=no rule, so it counts in
    # `companies` but not here.
    assert block["shared"] == {
        "Example Bio": {"rows": 2, "any": 2, "lead": 1, "collaborator": 1},
        "Fictional Pharma": {"rows": 2, "any": 2, "lead": 1, "collaborator": 1},
    }
    assert block["companies"]["Fictional Pharma"] == {"any": 2, "lead": 2, "collaborator": 1}


def test_a_shared_flip_that_moves_no_attribution_now_moves_the_baseline() -> None:
    # The gap this section closes: flipping a rule's shared value leaves
    # index, companies and entities identical, so before the section existed
    # the regression could not see it.
    before = bl.compute_baseline(_sponsors(), _rules(solo_shared="no"))
    after = bl.compute_baseline(_sponsors(), _rules(solo_shared="yes"))
    for section in ("index", "companies", "entities"):
        assert before[section] == after[section]
    assert after["shared"]["Fictional Solo"] == {"rows": 1, "any": 1, "lead": 1, "collaborator": 0}
    assert "Fictional Solo" not in before["shared"]
    assert bl.diff_blocks(before, after) == [
        "shared.Fictional Solo: expected None got "
        "{'rows': 1, 'any': 1, 'lead': 1, 'collaborator': 0}"
    ]


def test_rows_catch_a_flip_that_leaves_trial_counts_alone() -> None:
    # NCT99000004 already has a shared=yes collaborator row for Fictional
    # Pharma (the partnership literal). Flipping the rule behind its second
    # collaborator row changes no trial count, only `rows`.
    sp = frame([
        ("NCT99000004", "INDUSTRY", "collaborator", PARTNER),
        ("NCT99000004", "INDUSTRY", "collaborator", "Fictional Pharma Inc."),
    ])
    no = bl.compute_baseline(sp, _rules())
    yes = bl.compute_baseline(sp, _rules().assign(
        shared=lambda r: r.shared.where(r.pattern != "fictional pharma", "yes")))
    assert no["shared"]["Fictional Pharma"] == {"rows": 1, "any": 1, "lead": 0, "collaborator": 1}
    assert yes["shared"]["Fictional Pharma"] == {"rows": 2, "any": 1, "lead": 0, "collaborator": 1}


def _write_legacy_block(path: Path, actual: dict) -> str:
    """An expected-counts file whose one block predates the shared section."""
    sha = "f" * 64
    legacy = {k: actual[k] for k in ("index", "companies", "entities")}
    path.write_text(json.dumps({
        "fixture": {"snapshot_date": "2026-06-19"},
        "baselines": {sha: {"generated_at": "2026-09-01T19:06:52+00:00",
                            "generated_by": "earlier run", "not_covered_by_fixture": [],
                            **legacy}},
    }))
    return sha


def test_backfill_adds_only_the_missing_section_and_stamps_it(tmp_path: Path) -> None:
    actual = bl.compute_baseline(_sponsors(), _rules())
    path = tmp_path / "expected_counts.json"
    sha = _write_legacy_block(path, actual)
    before = json.loads(path.read_text())["baselines"][sha]

    bl.backfill_sections(sha, actual, ["shared"], path=str(path))

    after = json.loads(path.read_text())["baselines"][sha]
    assert after["shared"] == actual["shared"]
    for key in ("generated_at", "generated_by", "not_covered_by_fixture",
                "index", "companies", "entities"):
        assert after[key] == before[key]
    assert set(after["sections_backfilled"]) == {"shared"}
    assert "section added to an existing block" in after["sections_backfilled"]["shared"]["generated_by"]

    # A section the block already has is never overwritten.
    with pytest.raises(ValueError, match="already has section 'shared'"):
        bl.backfill_sections(sha, actual, ["shared"], path=str(path))
    with pytest.raises(KeyError, match="no baseline block"):
        bl.backfill_sections("0" * 64, actual, ["shared"], path=str(path))


def test_diff_against_a_block_without_the_section_says_so(tmp_path: Path) -> None:
    actual = bl.compute_baseline(_sponsors(), _rules())
    path = tmp_path / "expected_counts.json"
    sha = _write_legacy_block(path, actual)
    legacy = json.loads(path.read_text())["baselines"][sha]
    assert bl.diff_blocks(legacy, actual) == ["shared: not in the earlier block, nothing to compare"]
