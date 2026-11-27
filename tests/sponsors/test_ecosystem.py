"""Unit, composition, and SHA-256 regression tests for sponsors/ecosystem.py."""

from __future__ import annotations

import ast
import builtins
import collections
import inspect
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest

from conftest import FIXTURE_PATH, RULES_PATH, frame, rules_df
from sponsors import baseline as bl
from sponsors import ecosystem as eco
from sponsors import sponsor_roles as sr

REPO_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
ECOSYSTEM_RULES_PATH: str = os.path.join(REPO_ROOT, "sponsors", "ecosystem_rules.csv")
ECOSYSTEM_FIXTURE_PATH: str = os.path.join(
    REPO_ROOT, "tests", "sponsors", "fixtures", "sponsors_fixture_ecosystem.csv"
)
ECOSYSTEM_FIXTURE_SHA256: str = (
    "b765e49669440bd4266269378d4ad609f1b305856f7a46ecae6fd35aaf6cf640"
)
EXPECTED_COMPANY_ALIASES_SHA256: str = (
    "3e13bc5926f6f8bc5025151c60ea7dc8f40a5f7b8d7a209026ba29472e70601c"
)


def eco_rules_df(rows: list[tuple[str, str, str, str, str, str, str]]) -> pd.DataFrame:
    """Build an in-memory ecosystem rules DataFrame."""
    return pd.DataFrame(rows, columns=eco.ECOSYSTEM_RULE_COLUMNS).astype(str)


@pytest.fixture(scope="session")
def shipped_ecosystem_rules() -> pd.DataFrame:
    return eco.load_ecosystem_rules(ECOSYSTEM_RULES_PATH)


@pytest.fixture(scope="session")
def ecosystem_fixture_frame() -> pd.DataFrame:
    return pd.read_csv(
        ECOSYSTEM_FIXTURE_PATH, sep="|", dtype=str, keep_default_na=False
    )


def _check_or_rebaseline_ecosystem(
    fixture: pd.DataFrame,
    rules: pd.DataFrame,
    rules_path: str,
    expected_path: str,
    fixture_meta: dict[str, Any],
) -> None:
    """Mirror tests/sponsors/test_fixture_regression.py re-baselining semantics."""
    rules_sha = eco.sha256_file(rules_path)
    actual = eco.compute_ecosystem_baseline(fixture, rules)
    uncovered = eco.uncovered_ecosystem_rules(fixture, rules)
    expected = eco.load_expected_ecosystem(expected_path)
    block = expected.get("baselines", {}).get(rules_sha)

    if block is None:
        eco.write_ecosystem_baseline(
            rules_sha, actual, uncovered, fixture_meta, path=expected_path
        )
        prev = sorted(
            expected.get("baselines", {}),
            key=lambda k: expected["baselines"][k].get("generated_at", ""),
        )
        drift = (
            eco.diff_ecosystem_blocks(expected["baselines"][prev[-1]], actual)
            if prev
            else []
        )
        pytest.fail(
            "ecosystem_rules.csv changed (sha256 "
            + rules_sha[:12]
            + "…) and had no baseline.\n"
            "A re-baseline block was GENERATED and written to "
            + expected_path
            + ".\n"
            + (
                f"Changes vs previous baseline ({prev[-1][:12]}…):\n  "
                + "\n  ".join(drift)
                + "\n"
                if drift
                else ""
            )
            + (
                f"Rules NOT covered by fixture: {[u['pattern'] for u in uncovered]}\n"
                if uncovered
                else "All attribute rules are covered by the fixture.\n"
            )
        )

    for section in eco.ECOSYSTEM_BASELINE_SECTIONS:
        if section in block:
            assert actual[section] == block[section], "\n".join(
                eco.diff_ecosystem_blocks(block, actual)
            )
    missing = [s for s in eco.ECOSYSTEM_BASELINE_SECTIONS if s not in block]
    if missing:
        # A block written before compute_ecosystem_baseline produced these
        # sections. Its other sections matched above, so the missing ones
        # come from the same rules, fixture and counting code: add them
        # through write_ecosystem_baseline (which re-checks the match and the
        # trial-shape refusal) and fail, so they are committed and reviewed
        # like any re-baseline. A missing section never passes.
        eco.write_ecosystem_baseline(
            rules_sha, actual, uncovered, fixture_meta, path=expected_path,
            complete_sections=missing,
        )
        pytest.fail(
            "the baseline block for ecosystem_rules.csv (sha256 "
            + rules_sha[:12]
            + f"…) has no {missing} section(s); its other sections match the fixture.\n"
            "The missing section(s) were GENERATED and written into that block in "
            + expected_path
            + " (stamped under sections_backfilled).\n"
            "Commit that file so the added numbers are reviewed in the diff, then re-run."
        )


# ---------------------------------------------------------------------------
# 1. Existing industry rules & expected_counts.json remain 100% untouched
# ---------------------------------------------------------------------------


def test_company_aliases_and_expected_counts_untouched() -> None:
    assert bl.sha256_file(RULES_PATH) == EXPECTED_COMPANY_ALIASES_SHA256
    expected_ind = bl.load_expected()
    assert EXPECTED_COMPANY_ALIASES_SHA256 in expected_ind.get("baselines", {})
    block = expected_ind["baselines"][EXPECTED_COMPANY_ALIASES_SHA256]
    assert "sectors" not in block
    assert "National Cancer Institute (NCI)" not in block.get("companies", {})


# ---------------------------------------------------------------------------
# 2. load_ecosystem_rules / validate_ecosystem_rules invariants
# ---------------------------------------------------------------------------


def test_load_ecosystem_rules_validation(
    tmp_path: Path,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    assert len(shipped_ecosystem_rules) == 65
    assert set(shipped_ecosystem_rules["sector"]) <= eco.VALID_ECOSYSTEM_SECTORS
    assert eco.RULES_FORMAT_VERSION == "ecosystem_rules@v1.0"

    bad_cases = [
        # Empty note
        [("Org A", "nih", "exact_normalized", "org a", "attribute", "no", "   ")],
        # Unknown sector
        [("Org A", "pharma", "exact_normalized", "org a", "attribute", "no", "bad sector")],
        # Substring / regex rule_type forbidden
        [("Org A", "nih", "substring", "org a", "attribute", "no", "no substring")],
        [("Org A", "nih", "regex", "org.*", "attribute", "no", "no regex")],
        # Unknown status
        [("Org A", "nih", "exact_normalized", "org a", "pending", "no", "bad status")],
        # Unknown shared
        [("Org A", "nih", "exact_normalized", "org a", "attribute", "maybe", "bad shared")],
        # Single canonical mapped to two different sectors
        [
            ("Dual Org", "nih", "exact_normalized", "dual org a", "attribute", "no", "r1"),
            (
                "Dual Org",
                "us_federal_other",
                "exact_normalized",
                "dual org b",
                "attribute",
                "no",
                "r2",
            ),
        ],
    ]
    for idx, rows in enumerate(bad_cases):
        csv_path = tmp_path / f"bad_rules_{idx}.csv"
        eco_rules_df(rows).to_csv(csv_path, index=False)
        with pytest.raises(eco.EcosystemRuleValidationError):
            eco.load_ecosystem_rules(str(csv_path))


# ---------------------------------------------------------------------------
# 3. Zero substring matching negative controls
# ---------------------------------------------------------------------------


def test_no_substring_matching_negative_controls(
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    negatives = frame(
        [
            ("NCT1", "OTHER_GOV", "lead", "National Cancer Institute, France"),
            ("NCT2", "OTHER", "lead", "Korean Gynecologic Oncology Group"),
            ("NCT3", "OTHER", "lead", "Radiology Imaging Network of Korea"),
            ("NCT4", "OTHER", "lead", "Duke University Marine Lab"),
            ("NCT5", "OTHER", "lead", "Gates Foundationx"),
        ]
    )
    idx = eco.build_ecosystem_index(negatives, shipped_ecosystem_rules)
    assert idx.empty


# ---------------------------------------------------------------------------
# 4. Intra-ecosystem conflicts raise unless EVERY claiming rule is shared=yes
# ---------------------------------------------------------------------------


def test_intra_ecosystem_conflicts_raise_unless_all_shared() -> None:
    sp = frame([("NCT1", "OTHER", "lead", "Joint Cancer Consortium")])

    # Two attribute rules without shared=yes -> raises
    conflict_rules = eco_rules_df(
        [
            (
                "Org A",
                "nih",
                "exact_normalized",
                "joint cancer consortium",
                "attribute",
                "no",
                "claim A",
            ),
            (
                "Org B",
                "academic_medical_center",
                "exact_normalized",
                "joint cancer consortium",
                "attribute",
                "yes",
                "claim B",
            ),
        ]
    )
    with pytest.raises(eco.EcosystemRuleValidationError):
        eco.build_ecosystem_index(sp, conflict_rules)

    # Attribute + exclude overlap -> raises
    attr_excl_rules = eco_rules_df(
        [
            (
                "Org A",
                "nih",
                "exact_normalized",
                "joint cancer consortium",
                "attribute",
                "no",
                "claim A",
            ),
            (
                "Org B",
                "nih",
                "exact_normalized",
                "joint cancer consortium",
                "exclude",
                "no",
                "excluded B",
            ),
        ]
    )
    with pytest.raises(eco.EcosystemRuleValidationError):
        eco.build_ecosystem_index(sp, attr_excl_rules)

    # Both shared=yes -> attributes to both; sector_trials & organization_trials dedupe on nct_id
    shared_rules = eco_rules_df(
        [
            (
                "Org A",
                "nih",
                "exact_normalized",
                "joint cancer consortium",
                "attribute",
                "yes",
                "shared co-sponsor A",
            ),
            (
                "Org B",
                "nih",
                "exact_normalized",
                "joint cancer consortium",
                "attribute",
                "yes",
                "shared co-sponsor B",
            ),
        ]
    )
    idx = eco.build_ecosystem_index(sp, shared_rules)
    assert sorted(idx["canonical"].tolist()) == ["Org A", "Org B"]
    assert len(eco.sector_trials(idx, "nih")) == 1
    assert len(eco.organization_trials(idx, "Org A")) == 1
    assert len(eco.organization_trials(idx, "Org B")) == 1


def test_a_second_rule_for_the_same_canonical_keeps_its_shared_no() -> None:
    # Codex P2 on PR #111 (r4663843581): when a second, lower-precedence rule
    # claimed a literal for the same canonical, _assign returned early and
    # dropped that rule's shared value, so the literal stayed shared=yes
    # although a claiming rule says no. check_cross_layer_conflicts (and the
    # within-layer check) then let another claim through. The first rule is
    # kept; shared is "yes" only when both rules say yes.
    sp = frame([("NCT99000901", "OTHER", "lead", "Fictional Health")])
    uniq = sr.literal_universe(sp)
    eco_rules = eco_rules_df(
        [
            ("Fictional Health System", "academic_medical_center", "exact_literal",
             "Fictional Health", "attribute", "yes", "first claim, shared"),
            ("Fictional Health System", "academic_medical_center", "exact_normalized",
             "fictional health", "attribute", "no", "second claim, not shared"),
        ]
    )
    industry = rules_df(
        [("Fictional Pharma", "exact_literal", "Fictional Health", "attribute", "yes",
          "industry claim, shared")]
    )
    matched = eco.match_ecosystem_literals(uniq, eco_rules)
    assert matched[["canonical", "match_rule", "shared"]].values.tolist() == [
        ["Fictional Health System", "exact_literal", "no"]
    ]
    with pytest.raises(eco.EcosystemRuleValidationError, match="cross-layer conflict"):
        eco.check_cross_layer_conflicts(uniq, industry, eco_rules)
    # Within the layer: a later claim for another canonical, shared=yes, is a
    # conflict once the first canonical's claims are not all shared.
    three = eco_rules_df(
        [
            ("Fictional Health System", "academic_medical_center", "exact_literal",
             "Fictional Health", "attribute", "yes", "first claim, shared"),
            ("Fictional Health System", "academic_medical_center", "exact_normalized",
             "fictional health", "attribute", "no", "second claim, not shared"),
            ("Fictional Foundation", "non_profit_foundation", "exact_normalized",
             "fictional health", "attribute", "yes", "other canonical, shared"),
        ]
    )
    with pytest.raises(eco.EcosystemRuleValidationError, match="conflicting ecosystem rules"):
        eco.match_ecosystem_literals(uniq, three)
    # Both rules shared=yes: the literal stays shared, and the cross-layer
    # claim composes.
    both_yes = eco_rules.assign(shared="yes")
    assert eco.match_ecosystem_literals(uniq, both_yes)["shared"].tolist() == ["yes"]
    assert eco.check_cross_layer_conflicts(uniq, industry, both_yes) is None


# ---------------------------------------------------------------------------
# 5. Three-state partition and audit disjointness
# ---------------------------------------------------------------------------


def test_three_state_partition_and_audit_disjointness(
    shipped_ecosystem_rules: pd.DataFrame,
    ecosystem_fixture_frame: pd.DataFrame,
) -> None:
    part = eco.partition_ecosystem(ecosystem_fixture_frame, shipped_ecosystem_rules)
    attr = part["attributed"]
    excl = part["reviewed_excluded"]
    unrev = part["unreviewed"]

    universe = set(sr.literal_universe(ecosystem_fixture_frame)["name"])
    assert attr.isdisjoint(excl)
    assert attr.isdisjoint(unrev)
    assert excl.isdisjoint(unrev)
    assert (attr | excl | unrev) == universe

    assert "National Cancer Institute, France" in excl
    assert "Alliance Foundation Trials, LLC." in excl
    assert "PrECOG, LLC." in excl
    assert "Kaohsiung Veterans General Hospital." in excl
    assert "Korean Gynecologic Oncology Group" in unrev
    assert "Regional Community Medical Center" in unrev

    # Audit disjointness for NCI with stem="cancer institute"
    audit = eco.audit_ecosystem(
        ecosystem_fixture_frame,
        shipped_ecosystem_rules,
        canonical="National Cancer Institute (NCI)",
        stem="cancer institute",
    )
    sets = [
        set(audit[k]["name"])
        for k in (
            "attributed",
            "attributed_elsewhere",
            "reviewed_excluded",
            "unreviewed_candidates",
        )
    ]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert sets[i].isdisjoint(sets[j])
    assert "National Cancer Institute (NCI)" in sets[0]
    assert "Dana-Farber Cancer Institute" in sets[1]
    assert "National Cancer Institute, France" in sets[2]


# ---------------------------------------------------------------------------
# 6. VA and DoD entity vs current_owner views
# ---------------------------------------------------------------------------


def test_va_and_dod_entity_vs_current_owner_views(
    shipped_ecosystem_rules: pd.DataFrame,
    ecosystem_fixture_frame: pd.DataFrame,
) -> None:
    idx = eco.build_ecosystem_index(ecosystem_fixture_frame, shipped_ecosystem_rules)

    va_owner = eco.organization_trials(
        idx, "U.S. Department of Veterans Affairs", view="current_owner"
    )
    assert sorted(va_owner["nct_id"].tolist()) == ["NCT90000018", "NCT90000019"]

    va_ord = eco.organization_trials(
        idx, "VA Office of Research and Development", view="as_registered"
    )
    assert sorted(va_ord["nct_id"].tolist()) == ["NCT90000018", "NCT90000019"]
    assert list(va_ord.loc[va_ord["nct_id"] == "NCT90000018", "is_lead"]) == [True]
    assert list(va_ord.loc[va_ord["nct_id"] == "NCT90000019", "is_collaborator"]) == [True]

    dod_owner = eco.organization_trials(
        idx, "U.S. Department of Defense", view="current_owner"
    )
    assert sorted(dod_owner["nct_id"].tolist()) == ["NCT90000020", "NCT90000021"]

    usamrdc = eco.organization_trials(
        idx,
        "U.S. Army Medical Research and Development Command",
        view="as_registered",
    )
    assert sorted(usamrdc["nct_id"].tolist()) == ["NCT90000021"]


# ---------------------------------------------------------------------------
# 7. Cross-layer composition and conflict guard (Wake Forest + Pfizer)
# ---------------------------------------------------------------------------


def test_cross_layer_composition_and_conflict_guard(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    sp = frame(
        [
            ("NCT100", "OTHER", "lead", "Wake Forest University Health Sciences"),
            ("NCT101", "INDUSTRY", "lead", "Pfizer"),
        ]
    )
    unified = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)
    wf_rows = unified[unified["nct_id"] == "NCT100"]
    assert len(wf_rows) == 1
    assert wf_rows.iloc[0]["canonical"] == "Wake Forest University Health Sciences"
    assert wf_rows.iloc[0]["sector"] == "academic_medical_center"

    part = eco.partition_unified(sp, shipped_rules, shipped_ecosystem_rules)
    assert "Wake Forest University Health Sciences" in part["attributed"]
    assert "Wake Forest University Health Sciences" not in part["reviewed_excluded"]

    # Adding Pfizer as attribute (shared=no) in ecosystem rules MUST raise
    bad_eco = pd.concat(
        [
            shipped_ecosystem_rules,
            eco_rules_df(
                [
                    (
                        "Pfizer Non-Profit",
                        "non_profit_foundation",
                        "exact_normalized",
                        "pfizer",
                        "attribute",
                        "no",
                        "deliberate cross-layer conflict test",
                    )
                ]
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(eco.EcosystemRuleValidationError):
        eco.check_cross_layer_conflicts(sr.literal_universe(sp), shipped_rules, bad_eco)
    with pytest.raises(eco.EcosystemRuleValidationError):
        eco.build_unified_index(sp, shipped_rules, bad_eco)


# ---------------------------------------------------------------------------
# 8. Zero conflicts against full 233k industry fixture + exact industry counts
# ---------------------------------------------------------------------------


def test_zero_conflicts_against_full_233k_industry_fixture(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
    fixture_frame: pd.DataFrame,
) -> None:
    assert os.path.exists(FIXTURE_PATH)
    ind_only = sr.build_index(fixture_frame, shipped_rules)
    unified = eco.build_unified_index(
        fixture_frame, shipped_rules, shipped_ecosystem_rules
    )
    unified_ind = unified[unified["sector"] == "industry"].drop(columns=["sector"])
    assert len(unified_ind) == len(ind_only)
    assert int(unified_ind["nct_id"].nunique()) == int(ind_only["nct_id"].nunique())
    assert int(unified_ind["canonical"].nunique()) == int(ind_only["canonical"].nunique())


# ---------------------------------------------------------------------------
# 9. Multi-sector trial_cohorts & attribute_study_sponsors deduplication
# ---------------------------------------------------------------------------


def test_trial_cohorts_multi_sector_deduplication(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    sp = frame(
        [
            ("NCT555", "NETWORK", "lead", "SWOG Cancer Research Network"),
            ("NCT555", "NIH", "collaborator", "National Cancer Institute (NCI)"),
            (
                "NCT555",
                "NIH",
                "collaborator",
                "National Institute of Allergy and Infectious Diseases (NIAID)",
            ),
            ("NCT555", "INDUSTRY", "collaborator", "Merck Sharp & Dohme"),
        ]
    )
    idx = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)
    assert eco.trial_cohorts(idx, role="any")["NCT555"] == [
        "industry",
        "nctn_cooperative_group",
        "nih",
    ]
    assert eco.trial_cohorts(idx, role="lead")["NCT555"] == [
        "nctn_cooperative_group"
    ]
    assert len(eco.sector_trials(idx, "nih")) == 1

    # End-to-end attribute_study_sponsors with both stored and API v2 shapes
    stored_rec: dict[str, Any] = {
        "nct_id": "NCT555",
        "lead_sponsor_name": "SWOG Cancer Research Network",
        "sponsor_class": "NETWORK",
        "collaborators": [
            {"name": "National Cancer Institute (NCI)", "class": "NIH"},
            {"name": "Merck Sharp & Dohme LLC", "class": "INDUSTRY"},
        ],
    }
    api_rec: dict[str, Any] = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT556"},
            "sponsorCollaboratorsModule": {
                "leadSponsor": {
                    "name": "Wake Forest University Health Sciences",
                    "class": "OTHER",
                },
                "collaborators": [
                    {"name": "Pfizer", "class": "INDUSTRY"},
                ],
            },
        }
    }
    u_idx, log = eco.attribute_study_sponsors(
        [stored_rec, api_rec],
        industry_rules=shipped_rules,
        ecosystem_rules=shipped_ecosystem_rules,
    )
    assert log.n_records == 2
    assert eco.trial_cohorts(u_idx)["NCT555"] == [
        "industry",
        "nctn_cooperative_group",
        "nih",
    ]
    assert eco.trial_cohorts(u_idx)["NCT556"] == [
        "academic_medical_center",
        "industry",
    ]


# ---------------------------------------------------------------------------
# 10. Ecosystem fixture SHA-256 integrity & explicit re-baseline guard
# ---------------------------------------------------------------------------


def test_ecosystem_fixture_integrity_and_sha256_rebaseline(
    tmp_path: Path,
    shipped_ecosystem_rules: pd.DataFrame,
    ecosystem_fixture_frame: pd.DataFrame,
) -> None:
    assert eco.sha256_file(ECOSYSTEM_FIXTURE_PATH) == ECOSYSTEM_FIXTURE_SHA256

    fixture_meta: dict[str, Any] = {
        "file": "tests/sponsors/fixtures/sponsors_fixture_ecosystem.csv",
        "sha256": ECOSYSTEM_FIXTURE_SHA256,
        "snapshot_date": "2026-11-20",
    }
    _check_or_rebaseline_ecosystem(
        fixture=ecosystem_fixture_frame,
        rules=shipped_ecosystem_rules,
        rules_path=ECOSYSTEM_RULES_PATH,
        expected_path=eco.EXPECTED_ECOSYSTEM_PATH,
        fixture_meta=fixture_meta,
    )

    # Verify in tmp_path that an unbaselined rule edit writes the new block and fails
    edited_rules_path = tmp_path / "ecosystem_rules_edited.csv"
    temp_expected_path = tmp_path / "expected_ecosystem_counts.json"
    temp_expected_path.write_text(
        Path(eco.EXPECTED_ECOSYSTEM_PATH).read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    extra_rows = eco_rules_df(
        [
            (
                "Regional Community Medical Center",
                "academic_medical_center",
                "exact_normalized",
                "regional community medical center",
                "attribute",
                "no",
                "temp test rule hitting fixture",
            ),
            (
                "Unseen Foundation",
                "non_profit_foundation",
                "exact_normalized",
                "unseen foundation",
                "attribute",
                "no",
                "temp test rule not covered by fixture",
            ),
        ]
    )
    edited_rules = pd.concat([shipped_ecosystem_rules, extra_rows], ignore_index=True)
    edited_rules.to_csv(edited_rules_path, index=False)

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _check_or_rebaseline_ecosystem(
            fixture=ecosystem_fixture_frame,
            rules=edited_rules,
            rules_path=str(edited_rules_path),
            expected_path=str(temp_expected_path),
            fixture_meta=fixture_meta,
        )

    msg = str(exc_info.value)
    assert "Changes vs previous baseline" in msg
    assert "organizations.Regional Community Medical Center" in msg
    assert "unseen foundation" in msg

    updated_expected = eco.load_expected_ecosystem(str(temp_expected_path))
    new_sha = eco.sha256_file(str(edited_rules_path))
    assert new_sha in updated_expected["baselines"]
    assert updated_expected["baselines"][new_sha]["not_covered_by_fixture"] == [
        {
            "canonical": "Unseen Foundation",
            "sector": "non_profit_foundation",
            "rule_type": "exact_normalized",
            "pattern": "unseen foundation",
        }
    ]


# ---------------------------------------------------------------------------
# 10b. The `shared` baseline section and completing an older block
#
# Fictional organizations and ids (NCT99000951..NCT99000954) only.
# ---------------------------------------------------------------------------

_SHARED_PARTNER: str = "Fictional University & Example Foundation"
_SHARED_META: dict[str, str] = {"file": "fictional_fixture.csv", "snapshot_date": "2026-11-20"}
_SHARED_SHA: str = "a" * 64


def _shared_sponsors() -> pd.DataFrame:
    return frame([
        ("NCT99000951", "OTHER", "lead", _SHARED_PARTNER),
        ("NCT99000952", "OTHER", "collaborator", _SHARED_PARTNER),
        ("NCT99000952", "OTHER", "lead", "Fictional University"),
        ("NCT99000953", "OTHER", "lead", "Fictional Solo Clinic"),
    ])


def _shared_rules(solo_shared: str = "no", base_shared: str = "no") -> pd.DataFrame:
    return eco_rules_df([
        ("Fictional University", "academic_medical_center", "exact_literal",
         _SHARED_PARTNER, "attribute", "yes", "partnership literal, shared"),
        ("Example Foundation", "non_profit_foundation", "exact_literal",
         _SHARED_PARTNER, "attribute", "yes", "partnership literal, shared"),
        ("Fictional University", "academic_medical_center", "exact_normalized",
         "fictional university", "attribute", base_shared, "base name"),
        ("Fictional Solo Clinic", "academic_medical_center", "exact_literal",
         "Fictional Solo Clinic", "attribute", solo_shared, "single organization"),
    ])


def test_shared_section_counts_shared_yes_rows_and_trials_per_organization() -> None:
    block = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules())
    assert tuple(block) == eco.ECOSYSTEM_BASELINE_SECTIONS
    # Only organizations with a shared=yes index row appear. Fictional
    # University's lead row on NCT99000952 comes from its shared=no rule, so
    # it counts in `organizations` but not here.
    assert block["shared"] == {
        "Example Foundation": {"rows": 2, "any": 2, "lead": 1, "collaborator": 1},
        "Fictional University": {"rows": 2, "any": 2, "lead": 1, "collaborator": 1},
    }
    assert block["organizations"]["Fictional University"] == {"any": 2, "lead": 2, "collaborator": 1}


def test_shipped_ecosystem_rules_give_an_empty_shared_section(
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Every shipped ecosystem rule is shared=no, so no index row is shared;
    # the section pins that emptiness until a rule says otherwise.
    assert set(shipped_ecosystem_rules["shared"]) == {"no"}
    assert eco.compute_ecosystem_baseline(_holdout_frame(), shipped_ecosystem_rules)["shared"] == {}


def test_a_shared_flip_that_moves_no_attribution_now_moves_the_ecosystem_baseline() -> None:
    before = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules(solo_shared="no"))
    after = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules(solo_shared="yes"))
    for section in ("index", "sectors", "organizations", "entities"):
        assert before[section] == after[section]
    assert "Fictional Solo Clinic" not in before["shared"]
    assert after["shared"]["Fictional Solo Clinic"] == {"rows": 1, "any": 1, "lead": 1, "collaborator": 0}
    assert eco.diff_ecosystem_blocks(before, after) == [
        "shared.Fictional Solo Clinic: expected None got "
        "{'rows': 1, 'any': 1, 'lead': 1, 'collaborator': 0}"
    ]


def test_shared_rows_catch_a_flip_that_leaves_trial_counts_alone() -> None:
    # NCT99000954 already has a shared=yes collaborator row for Fictional
    # University (the partnership literal); flipping the rule behind its
    # second collaborator row changes no trial count, only `rows`.
    sp = frame([
        ("NCT99000954", "OTHER", "collaborator", _SHARED_PARTNER),
        ("NCT99000954", "OTHER", "collaborator", "Fictional University"),
    ])
    no = eco.compute_ecosystem_baseline(sp, _shared_rules(base_shared="no"))
    yes = eco.compute_ecosystem_baseline(sp, _shared_rules(base_shared="yes"))
    assert no["shared"]["Fictional University"] == {"rows": 1, "any": 1, "lead": 0, "collaborator": 1}
    assert yes["shared"]["Fictional University"] == {"rows": 2, "any": 1, "lead": 0, "collaborator": 1}


def _write_legacy_ecosystem_file(path: Path, actual: dict[str, Any], **changes: Any) -> None:
    """An expected-counts file whose one block predates the shared section."""
    legacy: dict[str, Any] = {s: actual[s] for s in ("index", "sectors", "organizations", "entities")}
    legacy.update(changes)
    path.write_text(json.dumps({
        "fixture": _SHARED_META,
        "baselines": {_SHARED_SHA: {
            "generated_at": "2026-11-21T03:26:46+00:00",
            "generated_by": "earlier run",
            "not_covered_by_fixture": [],
            **legacy,
        }},
    }), encoding="utf-8")


def test_complete_sections_adds_only_the_missing_section_and_stamps_it(tmp_path: Path) -> None:
    actual = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules())
    path = tmp_path / "expected_ecosystem_counts.json"
    _write_legacy_ecosystem_file(path, actual)
    before = json.loads(path.read_text(encoding="utf-8"))

    eco.write_ecosystem_baseline(
        _SHARED_SHA, actual, [], dict(_SHARED_META), path=str(path), complete_sections=["shared"]
    )

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["fixture"] == before["fixture"]
    stored, old = after["baselines"][_SHARED_SHA], before["baselines"][_SHARED_SHA]
    assert stored["shared"] == actual["shared"]
    for key in old:
        assert stored[key] == old[key]
    assert set(stored) == set(old) | {"shared", "sections_backfilled"}
    assert set(stored["sections_backfilled"]) == {"shared"}
    assert "section added to an existing block" in stored["sections_backfilled"]["shared"]["generated_by"]


def test_complete_sections_refusals_write_nothing(tmp_path: Path) -> None:
    actual = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules())
    path = tmp_path / "expected_ecosystem_counts.json"

    def refused(match: str, sections: Any, sha: str = _SHARED_SHA,
                meta: dict[str, str] | None = None, **legacy_changes: Any) -> str:
        _write_legacy_ecosystem_file(path, actual, **legacy_changes)
        before = path.read_bytes()
        with pytest.raises(ValueError, match=match) as exc_info:
            eco.write_ecosystem_baseline(
                sha, actual, [], dict(meta or _SHARED_META), path=str(path),
                complete_sections=sections,
            )
        assert path.read_bytes() == before
        return str(exc_info.value)

    refused("not one string", "shared")
    msg = refused("not baseline sections", ["NCT99000951"])
    assert "NCT99000951" not in msg
    refused("repeated name", ["shared", "shared"])
    refused("may not name every section", list(eco.ECOSYSTEM_BASELINE_SECTIONS))
    refused("needs a block already stored", ["shared"], sha="b" * 64)
    refused("another fixture description", ["shared"], meta={"file": "other.csv"})
    refused("shared already stored", ["shared"], shared=actual["shared"])
    drifted = json.loads(json.dumps(actual["organizations"]))
    drifted["Fictional University"]["any"] += 1
    refused("stored organizations differ from block", ["shared"], organizations=drifted)
    _write_legacy_ecosystem_file(path, actual)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["baselines"][_SHARED_SHA]["entities"]
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="also lacks entities"):
        eco.write_ecosystem_baseline(
            _SHARED_SHA, actual, [], dict(_SHARED_META), path=str(path), complete_sections=["shared"]
        )
    assert path.read_bytes() == before


def test_write_refuses_a_shared_section_of_the_wrong_shape(tmp_path: Path) -> None:
    actual = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules())
    path = tmp_path / "expected_ecosystem_counts.json"
    bad_blocks: list[dict[str, Any]] = []
    without = {k: v for k, v in actual.items() if k != "shared"}
    bad_blocks.append(without)
    for counts in (
        {"any": 2, "lead": 1, "collaborator": 1},                   # no rows
        {"rows": True, "any": 2, "lead": 1, "collaborator": 1},     # a bool is not a count
        {"rows": 2, "any": 2, "lead": 1, "collaborator": 1, "x": 1},
    ):
        block = json.loads(json.dumps(actual))
        block["shared"]["Fictional University"] = counts
        bad_blocks.append(block)
    not_an_org = json.loads(json.dumps(actual))
    not_an_org["shared"]["Unattributed Org"] = {"rows": 1, "any": 1, "lead": 1, "collaborator": 0}
    bad_blocks.append(not_an_org)
    for block in bad_blocks:
        with pytest.raises(ValueError):
            eco.write_ecosystem_baseline(_SHARED_SHA, block, [], dict(_SHARED_META), path=str(path))
        assert not path.exists()
    eco.write_ecosystem_baseline(_SHARED_SHA, actual, [], dict(_SHARED_META), path=str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["baselines"][_SHARED_SHA]["shared"] == actual["shared"]


def test_diff_against_an_ecosystem_block_without_shared_says_so() -> None:
    actual = eco.compute_ecosystem_baseline(_shared_sponsors(), _shared_rules())
    legacy = {s: actual[s] for s in ("index", "sectors", "organizations", "entities")}
    assert eco.diff_ecosystem_blocks(legacy, actual) == [
        "shared: not in the earlier block, nothing to compare"
    ]


# ---------------------------------------------------------------------------
# 11. Audit holdout (in_audit) refusals
#
# Every id below is fictional (NCT99000001..NCT99999999). Input that carries
# in_audit (a frame column or a record key) is refused when any value is
# True, when any is not a bool, or when any nct_id is not exactly
# ^NCT[0-9]{8}$; input without it is left to the caller, unchanged.
# ---------------------------------------------------------------------------

_HOLDOUT_ROWS: list[tuple[str, str, str, str]] = [
    ("NCT99000101", "NIH", "lead", "National Cancer Institute (NCI)"),
    ("NCT99000101", "OTHER", "collaborator", "Wake Forest University Health Sciences"),
    ("NCT99000102", "INDUSTRY", "lead", "Pfizer"),
    ("NCT99000102", "NETWORK", "collaborator", "SWOG Cancer Research Network"),
]

_MALFORMED_NCT_IDS: list[Any] = [
    "nct99000102",
    " NCT99000102",
    "NCT99000102 ",
    "NCT99000102\n",
    "NCT9900010",
    "NCT990001022",
    "NCT" + "٩" * 8,
    "NCT-99000102",
    "",
    None,
]

# in_audit values that are present but are not a bool. The strings are what a
# CSV read with dtype=str produces.
_NON_BOOL_IN_AUDIT: list[Any] = ["True", "False", "true", 1, 0, None, float("nan")]


def _holdout_frame(in_audit: list[Any] | None = None) -> pd.DataFrame:
    """The fictional sponsor rows, with an in_audit column when one is given."""
    sp = frame(_HOLDOUT_ROWS)
    if in_audit is not None:
        sp["in_audit"] = pd.Series(in_audit, dtype=object)
    return sp


_FRAME_ENTRY_POINTS: list[tuple[str, Any]] = [
    ("build_ecosystem_index", lambda f, ind, er: eco.build_ecosystem_index(f, er)),
    ("partition_ecosystem", lambda f, ind, er: eco.partition_ecosystem(f, er)),
    ("build_unified_index", lambda f, ind, er: eco.build_unified_index(f, ind, er)),
    ("partition_unified", lambda f, ind, er: eco.partition_unified(f, ind, er)),
    (
        "audit_ecosystem",
        lambda f, ind, er: eco.audit_ecosystem(
            f, er, canonical="National Cancer Institute (NCI)", stem="cancer"
        ),
    ),
    ("uncovered_ecosystem_rules", lambda f, ind, er: eco.uncovered_ecosystem_rules(f, er)),
    ("compute_ecosystem_baseline", lambda f, ind, er: eco.compute_ecosystem_baseline(f, er)),
]

_INDEX_ENTRY_POINTS: list[tuple[str, Any]] = [
    ("sector_trials", lambda idx: eco.sector_trials(idx, "nih")),
    (
        "organization_trials",
        lambda idx: eco.organization_trials(idx, "National Cancer Institute (NCI)"),
    ),
    ("trial_cohorts", lambda idx: eco.trial_cohorts(idx)),
]


def _assert_same(a: Any, b: Any) -> None:
    """Equal outputs, whether DataFrames, dicts of DataFrames or plain values."""
    if isinstance(a, pd.DataFrame):
        pd.testing.assert_frame_equal(a, b)
    elif isinstance(a, dict) and a and all(isinstance(v, pd.DataFrame) for v in a.values()):
        assert a.keys() == b.keys()
        for key in a:
            pd.testing.assert_frame_equal(a[key], b[key])
    else:
        assert a == b


def _assert_refused(exc: BaseException, reason: str) -> None:
    assert isinstance(exc, eco.EcosystemAuditHoldoutError)
    assert exc.reason == reason
    text = str(exc)
    assert "NCT99" not in text
    for _, _, _, name in _HOLDOUT_ROWS:
        assert name not in text


@pytest.mark.parametrize(("name", "entry"), _FRAME_ENTRY_POINTS)
def test_frame_entry_points_refuse_in_audit_true(
    name: str,
    entry: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    held = _holdout_frame([False, False, True, True])
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(held, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_true")
    assert "2 row(s)" in str(exc_info.value)
    # The refusal comes before any rule is read: unusable rules do not get a
    # chance to raise first.
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(held, object(), object())
    _assert_refused(exc_info.value, "in_audit_true")
    # A bool-dtype column (numpy.bool_ values) is refused the same way.
    as_bool_dtype = _holdout_frame()
    as_bool_dtype["in_audit"] = pd.Series([False, True, False, False], dtype=bool)
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(as_bool_dtype, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_true")


@pytest.mark.parametrize(("name", "entry"), _FRAME_ENTRY_POINTS)
@pytest.mark.parametrize("flag", _NON_BOOL_IN_AUDIT)
def test_frame_entry_points_refuse_in_audit_not_bool(
    name: str,
    entry: Any,
    flag: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    held = _holdout_frame([False, False, flag, False])
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(held, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")
    assert "row position 2" in str(exc_info.value)


@pytest.mark.parametrize(("name", "entry"), _FRAME_ENTRY_POINTS)
def test_frame_entry_points_refuse_csv_string_flags_and_duplicate_columns(
    name: str,
    entry: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A frame read from CSV with dtype=str holds "False", not False.
    as_csv = _holdout_frame([False] * 4).astype(str)
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(as_csv, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")

    # Two in_audit columns are read alike. Agreeing (all False) they pass
    # and change nothing (before the consolidation any second source was
    # refused); disagreeing, one holds a True and the frame is refused.
    doubled = pd.concat(
        [_holdout_frame([False] * 4), pd.Series([False] * 4, name="in_audit")], axis=1
    )
    _assert_same(
        entry(doubled, shipped_rules, shipped_ecosystem_rules),
        entry(_holdout_frame(), shipped_rules, shipped_ecosystem_rules),
    )
    disagreeing = pd.concat(
        [_holdout_frame([False] * 4), pd.Series([False, False, True, False], name="in_audit")],
        axis=1,
    )
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(disagreeing, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_true")


@pytest.mark.parametrize(("name", "entry"), _FRAME_ENTRY_POINTS)
@pytest.mark.parametrize("nct_id", _MALFORMED_NCT_IDS)
def test_frame_entry_points_refuse_malformed_nct_id_when_in_audit_false(
    name: str,
    entry: Any,
    nct_id: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    held = _holdout_frame([False] * 4)
    held["nct_id"] = pd.Series(
        ["NCT99000101", "NCT99000101", nct_id, "NCT99000102"], dtype=object
    )
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(held, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "nct_id_malformed")
    assert "row position 2" in str(exc_info.value)

    no_ids = _holdout_frame([False] * 4).drop(columns=["nct_id"])
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(no_ids, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "nct_id_malformed")


@pytest.mark.parametrize(("name", "entry"), _FRAME_ENTRY_POINTS)
def test_frame_entry_points_in_audit_false_matches_frame_without_column(
    name: str,
    entry: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    expected = entry(_holdout_frame(), shipped_rules, shipped_ecosystem_rules)
    as_object = _holdout_frame([False] * 4)
    as_bool_dtype = _holdout_frame()
    as_bool_dtype["in_audit"] = pd.Series([False] * 4, dtype=bool)
    for clean in (as_object, as_bool_dtype):
        _assert_same(entry(clean, shipped_rules, shipped_ecosystem_rules), expected)


def _index_with_in_audit(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
    in_audit: list[Any] | None,
) -> pd.DataFrame:
    """A unified index of the fictional rows, with an in_audit column added."""
    idx = eco.build_unified_index(_holdout_frame(), shipped_rules, shipped_ecosystem_rules)
    if in_audit is not None:
        idx = idx.copy()
        idx["in_audit"] = pd.Series((in_audit * len(idx))[: len(idx)], dtype=object)
    return idx


@pytest.mark.parametrize(("name", "entry"), _INDEX_ENTRY_POINTS)
def test_index_entry_points_refusals_and_clean_parity(
    name: str,
    entry: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    plain = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
    assert len(plain) >= 4 and "in_audit" not in plain.columns

    held = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False, True])
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        entry(held)
    _assert_refused(exc_info.value, "in_audit_true")

    for flag in _NON_BOOL_IN_AUDIT:
        bad = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False, flag])
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(bad)
        _assert_refused(exc_info.value, "in_audit_not_bool")

    for nct_id in _MALFORMED_NCT_IDS:
        bad_id = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False])
        bad_id["nct_id"] = bad_id["nct_id"].astype(object)
        bad_id.iloc[1, bad_id.columns.get_loc("nct_id")] = nct_id
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(bad_id)
        _assert_refused(exc_info.value, "nct_id_malformed")

    clean = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False])
    _assert_same(entry(clean), entry(plain))


def _stored_record(nct_id: Any, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "nct_id": nct_id,
        "lead_sponsor_name": "SWOG Cancer Research Network",
        "sponsor_class": "NETWORK",
        "collaborators": [{"name": "National Cancer Institute (NCI)", "class": "NIH"}],
    }
    rec.update(extra)
    return rec


def _api_record(nct_id: Any, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "sponsorCollaboratorsModule": {
                "leadSponsor": {
                    "name": "Wake Forest University Health Sciences",
                    "class": "OTHER",
                },
                "collaborators": [{"name": "Pfizer", "class": "INDUSTRY"}],
            },
        }
    }
    rec.update(extra)
    return rec


def test_attribute_study_sponsors_holdout_refusals(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    def run(records: list[Any]) -> Any:
        return eco.attribute_study_sponsors(
            records,
            industry_rules=shipped_rules,
            ecosystem_rules=shipped_ecosystem_rules,
        )

    for make in (_stored_record, _api_record):
        clean_other = _stored_record("NCT99000201")
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            run([clean_other, make("NCT99000202", in_audit=True)])
        _assert_refused(exc_info.value, "in_audit_true")
        assert "position 1" in str(exc_info.value)

        for flag in _NON_BOOL_IN_AUDIT + [pytest.importorskip("numpy").bool_(False)]:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                run([make("NCT99000202", in_audit=flag)])
            _assert_refused(exc_info.value, "in_audit_not_bool")

        for nct_id in _MALFORMED_NCT_IDS + [99000202]:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                run([make(nct_id, in_audit=False)])
            _assert_refused(exc_info.value, "nct_id_malformed")

    missing_stored = _stored_record("NCT99000203", in_audit=False)
    del missing_stored["nct_id"]
    missing_api = _api_record("NCT99000204", in_audit=False)
    del missing_api["protocolSection"]["identificationModule"]
    for rec in (missing_stored, missing_api):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            run([rec])
        _assert_refused(exc_info.value, "nct_id_malformed")

    # in_audit False with a canonical id: same index and log as without the key.
    with_key = run(
        [_stored_record("NCT99000205", in_audit=False), _api_record("NCT99000206", in_audit=False)]
    )
    without_key = run([_stored_record("NCT99000205"), _api_record("NCT99000206")])
    pd.testing.assert_frame_equal(with_key[0], without_key[0])
    assert with_key[1] == without_key[1]
    assert eco.trial_cohorts(with_key[0])["NCT99000206"] == [
        "academic_medical_center",
        "industry",
    ]


def test_attribute_study_sponsors_refuses_before_reading_rules_or_adapting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ran before the holdout refusal")

    monkeypatch.setattr(eco.sr, "load_rules", must_not_run)
    monkeypatch.setattr(eco, "load_ecosystem_rules", must_not_run)
    monkeypatch.setattr(eco.adapter, "records_to_frame", must_not_run)
    held = (_stored_record("NCT99000207", in_audit=True) for _ in range(1))
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        eco.attribute_study_sponsors(held)
    _assert_refused(exc_info.value, "in_audit_true")


def test_ecosystem_holdout_error_contract() -> None:
    assert not issubclass(eco.EcosystemAuditHoldoutError, ValueError)
    assert not issubclass(eco.EcosystemAuditHoldoutError, eco.EcosystemRuleValidationError)
    assert eco.HOLDOUT_REFUSAL_REASONS == (
        "in_audit_true",
        "in_audit_not_bool",
        "nct_id_malformed",
    )
    with pytest.raises(ValueError):
        eco.EcosystemAuditHoldoutError("nct_id_in_audit_set", "not a reason")


# ---------------------------------------------------------------------------
# 12. Red team, M2 round 1: holdout bypass attempts (test_redteam_m2_*)
#
# Each test asserts the CORRECT behaviour, which is refusal, for one attack
# on an entry point that enforces the holdout. A test that fails here names
# a bypass that is still open; it is not a test to loosen. Every id is
# fictional (NCT99000001..NCT99999999); no test touches the network, and
# none reads a file other than the shipped rules fixtures.
# ---------------------------------------------------------------------------


def _redteam_mapping_wrappers() -> list[tuple[str, Any]]:
    """Mappings that are not dict subclasses but answer ``in`` and ``.get``.

    Method: sponsors.adapter.record_rows reads a record only through
    ``.get``, so each of these is attributed in full; the holdout check
    skips anything that is not a dict. pandas Series rows are what
    df.iterrows() yields.
    """
    return [
        ("UserDict", collections.UserDict),
        ("MappingProxyType", types.MappingProxyType),
        ("pandas.Series", pd.Series),
    ]


def _redteam_run(
    records: Any, shipped_rules: pd.DataFrame, shipped_ecosystem_rules: pd.DataFrame
) -> Any:
    """attribute_study_sponsors with the shipped rules passed in (no file read)."""
    return eco.attribute_study_sponsors(
        records, industry_rules=shipped_rules, ecosystem_rules=shipped_ecosystem_rules
    )


def test_redteam_m2_attribute_study_sponsors_refuses_non_dict_mapping_records(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout record as a Mapping that is not a dict, alone or
    # after a clean dict record. _refuse_audit_holdout_records skipped
    # non-dicts ("left to sponsors.adapter, which logs them"), but the
    # adapter does not log them: it reads them with .get and the holdout
    # trial is attributed to its sectors. Correct: refusal. A record that is
    # not exactly a dict is now refused by type ("in_audit_not_bool").
    for _wrapper_name, wrap in _redteam_mapping_wrappers():
        for make in (_stored_record, _api_record):
            held = wrap(make("NCT99000301", in_audit=True))
            for batch in ([held], [_stored_record("NCT99000302", in_audit=False), held]):
                with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                    _redteam_run(batch, shipped_rules, shipped_ecosystem_rules)
                _assert_refused(exc_info.value, "in_audit_not_bool")


def test_redteam_m2_attribute_study_sponsors_refuses_single_record_not_list(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout record itself where an iterable of records is
    # expected (iterating it yields its keys). Expected: it raises, so
    # nothing is attributed; a holdout refusal or a TypeError are both
    # refusals, an index is not.
    held = _stored_record("NCT99000303", in_audit=True)
    with pytest.raises((eco.EcosystemAuditHoldoutError, TypeError, AttributeError)):
        _redteam_run(held, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_attribute_study_sponsors_refuses_batch_with_a_record_missing_in_audit(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a manifest-merged batch where one record lost its in_audit key
    # (a left join with no manifest row). The frame entry points refuse the
    # same gap, a NaN in the in_audit column, so pd.DataFrame(records) is
    # refused while the record list is attributed. Correct: once any record
    # in a batch carries in_audit, every record must.
    for make in (_stored_record, _api_record):
        batch = [_stored_record("NCT99000304", in_audit=False), make("NCT99000305")]
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run(batch, shipped_rules, shipped_ecosystem_rules)
        assert exc_info.value.reason in eco.HOLDOUT_REFUSAL_REASONS
    as_frame = frame(_HOLDOUT_ROWS)
    as_frame["in_audit"] = pd.Series([False, False, None, None], dtype=object)
    with pytest.raises(eco.EcosystemAuditHoldoutError):
        eco.build_unified_index(as_frame, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_attribute_study_sponsors_refuses_nested_in_audit(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the record marks itself as a holdout below the top level, so
    # the top-level key test misses it: beside the id it describes
    # (identificationModule), on protocolSection, or in a merged manifest
    # sub-record. Correct: refusal.
    in_ident = _api_record("NCT99000306")
    in_ident["protocolSection"]["identificationModule"]["in_audit"] = True
    in_proto = _api_record("NCT99000307")
    in_proto["protocolSection"]["in_audit"] = True
    manifest_api = _api_record("NCT99000308", manifest={"in_audit": True})
    manifest_stored = _stored_record("NCT99000309", manifest={"in_audit": True})
    for held in (in_ident, in_proto, manifest_api, manifest_stored):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_true")


def test_redteam_m2_attribute_study_sponsors_refuses_conflicting_ids(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a manifest row (in_audit False, one trial) merged onto another
    # trial's CT.gov payload leaves two ids in one record: top-level nct_id
    # from the manifest, nested nctId from the payload. The check reads only
    # the nested id and attributes a trial the flag does not describe.
    # Correct: an id disagreement is refused rather than resolved.
    held = _api_record("NCT99000310", in_audit=False)
    held["nct_id"] = "NCT99000311"
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "nct_id_malformed")


_REDTEAM_KEY_VARIANTS: list[str] = ["inAudit", "In_Audit", "IN_AUDIT", "in_audit ", "in-audit"]


@pytest.mark.parametrize("key", _REDTEAM_KEY_VARIANTS)
def test_redteam_m2_in_audit_key_and_column_spelling_variants_are_refused(
    key: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout flag under a spelling a camelCase merge, a
    # spreadsheet header or a stray space produces, as a record key and as
    # a frame or index column. The exact-name test misses it and the trial
    # is attributed and counted. Correct: a name that reads as in_audit
    # once case, spaces, hyphens and underscores are ignored is in_audit.
    for make in (_stored_record, _api_record):
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([make("NCT99000312", **{key: True})], shipped_rules, shipped_ecosystem_rules)
    held = _holdout_frame([True] * 4).rename(columns={"in_audit": key})
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(held, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [True])
    idx = idx.rename(columns={"in_audit": key})
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(idx)


@pytest.mark.parametrize("levels", [["in_audit"], ["nct_id", "in_audit"]])
def test_redteam_m2_frame_and_index_entry_points_refuse_in_audit_as_index_level(
    levels: list[str],
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: move in_audit (and optionally nct_id) from the columns into
    # the row index with set_index. The check looks only at frame.columns;
    # with in_audit alone in the index every frame entry point attributes
    # or counts the holdout rows, and with nct_id too the three entry
    # points that never read nct_id (partition_ecosystem, partition_unified,
    # uncovered_ecosystem_rules) still do. Correct: a holdout refusal, not a
    # result and not a KeyError.
    held = _holdout_frame([True] * 4).set_index(levels)
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(held, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_true")
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [True]).set_index(levels)
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(idx)
        _assert_refused(exc_info.value, "in_audit_true")


def test_redteam_m2_literal_entry_points_refuse_a_column_mapping_carrying_in_audit(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a column mapping ({column: Series}) instead of a DataFrame.
    # The check returned early for anything that is not a DataFrame, and
    # sr.literal_universe only needs sponsors["name"], so the entry points
    # that stop at the literal universe partitioned the holdout's sponsor
    # names. Correct: refusal. Anything that is not exactly a DataFrame is
    # now a TypeError (src/holdout_guard.py check_frame), flag or not.
    held = _holdout_frame([True] * 4)
    columns = {c: held[c] for c in held.columns}
    literal_entry_points: list[tuple[str, Any]] = [
        ("partition_ecosystem", lambda f: eco.partition_ecosystem(f, shipped_ecosystem_rules)),
        (
            "partition_unified",
            lambda f: eco.partition_unified(f, shipped_rules, shipped_ecosystem_rules),
        ),
        (
            "uncovered_ecosystem_rules",
            lambda f: eco.uncovered_ecosystem_rules(f, shipped_ecosystem_rules),
        ),
    ]
    for name, entry in literal_entry_points:
        with pytest.raises(TypeError, match="expects a pandas DataFrame"):
            entry(columns)


_REDTEAM_ODD_NCT_IDS: list[str] = [
    "NCT99000102​",
    "​NCT99000102",
    "NCT99000‍102",
    "﻿NCT99000102",
    "NCT99000102 ",
    "NCT99000102\t",
    "NCT99000102\r",
    "NCT99000102\x00",
    "ＮＣＴ99000102",
    "NCT99000102́",
]


@pytest.mark.parametrize("nct_id", _REDTEAM_ODD_NCT_IDS)
def test_redteam_m2_odd_nct_ids_with_in_audit_false_are_refused(
    nct_id: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: in_audit False on an id that only looks canonical (invisible,
    # whitespace or combining characters, full-width letters), through every
    # frame, index and record entry point. Expected: nct_id_malformed.
    held = _holdout_frame([False] * 4)
    held["nct_id"] = pd.Series(
        ["NCT99000101", "NCT99000101", nct_id, "NCT99000102"], dtype=object
    )
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(held, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False])
    idx["nct_id"] = idx["nct_id"].astype(object)
    idx.iloc[0, idx.columns.get_loc("nct_id")] = nct_id
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(idx)
        _assert_refused(exc_info.value, "nct_id_malformed")
    for make in (_stored_record, _api_record):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([make(nct_id, in_audit=False)], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")


def test_redteam_m2_odd_in_audit_values_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: in_audit values a CSV, a form or a JSON writer produces, none
    # of them a bool: lowercase strings, empty string, bytes, floats, NA
    # markers, containers. Frames accept numpy bools (a bool-dtype column
    # holds them); records accept only Python bools.
    np = pytest.importorskip("numpy")
    odd: list[Any] = [
        "false", "FALSE", "", "0", "no", b"False", b"True", 0.0, -0.0,
        pd.NA, pd.NaT, {}, (), np.int64(0),
    ]
    for flag in odd:
        held = _holdout_frame([False, False, flag, False])
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_not_bool")
        for make in (_stored_record, _api_record):
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                _redteam_run([make("NCT99000313", in_audit=flag)], shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_not_bool")
    nullable = _holdout_frame()
    nullable["in_audit"] = pd.Series([False, False, pd.NA, False], dtype="boolean")
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(nullable, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
    for flag in (np.True_, np.False_):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([_stored_record("NCT99000314", in_audit=flag)], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
    np_held = _holdout_frame([np.False_, np.False_, np.True_, np.False_])
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(np_held, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_true")


def test_redteam_m2_lists_and_frames_mixing_clean_and_holdout_refuse_wherever_it_sits(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout first, last or between clean rows and records,
    # from a list or a one-shot generator, with a malformed id on it too.
    # Expected: refused before anything is adapted or attributed.
    for position in (0, 1, 2, 3):
        flags = [False] * 4
        flags[position] = True
        held = _holdout_frame(flags)
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_true")

    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("adapted a batch that holds a holdout record")

    monkeypatch.setattr(eco.adapter, "records_to_frame", must_not_run)
    for position in (0, 1, 2):
        for nct_id in ("NCT99000315", "nct99000315", "NCT99000315​"):
            records = [_stored_record("NCT99000316", in_audit=False) for _ in range(2)]
            records.insert(position, _api_record(nct_id, in_audit=True))
            for source in (records, iter(list(records))):
                with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                    _redteam_run(source, shipped_rules, shipped_ecosystem_rules)
                _assert_refused(exc_info.value, "in_audit_true")


def test_redteam_m2_entry_points_reject_an_audit_id_set_argument(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a caller passes the audit id set (a list, with odd ids) in the
    # belief that the module filters on it. No entry point takes one, so it
    # must fail loudly (TypeError) rather than be swallowed by **kwargs.
    audit_ids: list[Any] = ["NCT99000101", "nct99000101", " NCT99000101", None]
    sp = _holdout_frame()
    calls: list[Any] = [
        lambda: eco.build_ecosystem_index(sp, shipped_ecosystem_rules, audit_ids=audit_ids),
        lambda: eco.build_unified_index(
            sp, shipped_rules, shipped_ecosystem_rules, audit_ids=audit_ids
        ),
        lambda: eco.attribute_study_sponsors(
            [_stored_record("NCT99000101")],
            industry_rules=shipped_rules,
            ecosystem_rules=shipped_ecosystem_rules,
            audit_ids=audit_ids,
        ),
        lambda: eco.trial_cohorts(sp, audit_ids=audit_ids),
    ]
    for call in calls:
        with pytest.raises(TypeError):
            call()
    for attr, obj in vars(eco).items():
        if attr.startswith("_") or not inspect.isfunction(obj) or obj.__module__ != eco.__name__:
            continue
        params = inspect.signature(obj).parameters.values()
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), attr


def test_redteam_m2_relies_on_caller_entry_points_do_no_io(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Claim under test: with rules passed in, every trial entry point on
    # input without in_audit (where it relies on its caller) reads only the
    # objects it is handed and writes nothing. Every file, socket and CSV
    # primitive raises while the entry points run.
    sp = _holdout_frame()
    records = [_stored_record("NCT99000317"), _api_record("NCT99000318")]
    idx = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("I/O attempted by an entry point that relies on its caller")

    for target, attr in (
        (builtins, "open"), (io, "open"), (os, "open"), (Path, "open"),
        (Path, "write_text"), (Path, "read_text"), (socket, "socket"),
        (socket, "create_connection"), (pd, "read_csv"), (pd, "read_json"),
        (pd.DataFrame, "to_csv"), (pd.DataFrame, "to_json"), (eco.json, "dump"),
        (eco.json, "load"), (eco.sr, "load_rules"), (eco, "load_ecosystem_rules"),
    ):
        monkeypatch.setattr(target, attr, refuse)
    for name, entry in _FRAME_ENTRY_POINTS:
        entry(sp, shipped_rules, shipped_ecosystem_rules)
    for name, entry in _INDEX_ENTRY_POINTS:
        entry(idx)
    _redteam_run(records, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_ecosystem_module_reads_and_writes_only_the_documented_files() -> None:
    # Static half of the claim "The only files this module reads or writes
    # are the two rules CSVs and the aggregate baseline JSON": the only
    # functions whose bodies reach a file are the documented loaders and
    # writer, and nothing imports a network or process module.
    tree = ast.parse(inspect.getsource(eco))
    io_names = {"open", "read_csv", "to_csv", "read_json", "to_json", "load", "dump", "exists"}
    reaching: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if called in io_names:
                    reaching.add(node.name)
    assert reaching == {"load_ecosystem_rules", "load_expected_ecosystem", "write_ecosystem_baseline"}
    imported: set[str] = set()
    from_src: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            if node.module == "src":
                from_src.update(alias.name for alias in node.names)
    # From this repository, besides its sibling sponsors modules, only the
    # shared holdout guard, which imports the standard library alone.
    assert imported <= {"__future__", "json", "os", "re", "datetime", "typing", "pandas", "", "src"}
    assert from_src == {"holdout_guard"}


def test_redteam_m2_ecosystem_has_no_in_code_caller_on_this_branch() -> None:
    # Claim under test (module docstring): "Callers today: none in code. The
    # only importer in this repository is tests/sponsors/test_ecosystem.py."
    repo = Path(REPO_ROOT)
    importers: list[str] = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if rel.parts[0] in {"tests", ".git", ".venv", "venv"} or "__pycache__" in rel.parts:
            continue
        if rel.as_posix() == "sponsors/ecosystem.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [
                    f"{node.module or ''}.{alias.name}" for alias in node.names
                ]
            if any(n == "sponsors.ecosystem" or n.endswith(".ecosystem") or n == "ecosystem" for n in names):
                importers.append(str(rel))
    assert importers == []


@pytest.mark.parametrize("dtype", ["category", "string", "str"])
def test_redteam_m2_missing_values_in_typed_columns_are_refused(
    dtype: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a missing nct_id or in_audit value inside a categorical or
    # string-dtype column. If Series.map skipped missing values, NaN would
    # reach .to_numpy(dtype=bool) as True and pass both checks. Expected:
    # the missing in_audit is refused (as not a bool or as held) and the
    # missing id as malformed.
    flags = _holdout_frame([False] * 4)
    flags["in_audit"] = pd.Series([False, False, None, False], dtype=object).astype(
        "category" if dtype == "category" else object
    )
    ids = _holdout_frame([False] * 4)
    ids["nct_id"] = pd.Series(["NCT99000101", "NCT99000101", None, "NCT99000102"], dtype=dtype)
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(flags, shipped_rules, shipped_ecosystem_rules)
        assert exc_info.value.reason in {"in_audit_not_bool", "in_audit_true"}
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(ids, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")


# ---------------------------------------------------------------------------
# 13. Holdout fixes for red team round 1: cases the fix covers beyond the
# red team's own tests, and inputs that must still pass. Every id is
# fictional (NCT99000001..NCT99999999); no test touches the network, and
# none reads a file other than the shipped rules fixtures.
# ---------------------------------------------------------------------------


# Spellings still read: invisible characters are dropped by the flag-key
# rule (ASCII letters only). A bytes key or label is not a str: refused by
# type. A full-width spelling is no longer read (a look-alike disguise; see
# src/holdout_guard.py).
_INVISIBLE_AND_WIDE_KEYS: list[Any] = [
    "in\u200b_audit",  # zero-width space
    "\ufeffin_audit",  # byte-order mark
    "In Audit",
    b"in_audit",  # a bytes key or column label
]


@pytest.mark.parametrize("key", _INVISIBLE_AND_WIDE_KEYS)
def test_in_audit_invisible_fullwidth_and_bytes_spellings_are_refused(
    key: Any,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    expected = "in_audit_not_bool" if isinstance(key, bytes) else "in_audit_true"
    for make in (_stored_record, _api_record):
        rec = make("NCT99000330")
        rec[key] = True
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([rec], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, expected)
    held = _holdout_frame([True] * 4).rename(columns={"in_audit": key})
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(held, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, expected)


def test_frame_cells_carrying_in_audit_are_checked(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # pd.DataFrame(records) from manifest-merged records keeps a nested
    # manifest as a column of dicts. A flag inside a cell is checked like a
    # row-level one: True is a holdout, a non-bool is refused, flags found
    # only inside cells cannot describe every row, and an nct_id beside a
    # flag must be its row's. Rows 0-1 are NCT99000101, rows 2-3 NCT99000102.
    def with_cells(row_flags: list[Any] | None, cells: list[Any]) -> pd.DataFrame:
        sp = _holdout_frame(row_flags)
        sp["manifest"] = pd.Series(cells, dtype=object)
        return sp

    cases: list[tuple[pd.DataFrame, str]] = [
        (with_cells([False] * 4, [None, None, {"nct_id": "NCT99000102", "in_audit": True}, None]), "in_audit_true"),
        (with_cells([False] * 4, [[{"in_audit": True}], None, None, None]), "in_audit_true"),
        (with_cells([False] * 4, [{"in_audit": "False"}, None, None, None]), "in_audit_not_bool"),
        (with_cells(None, [{"in_audit": False}] * 4), "in_audit_not_bool"),
        (with_cells([False] * 4, [None, None, {"nct_id": "NCT99000101", "in_audit": False}, None]), "nct_id_malformed"),
    ]
    for sp, reason in cases:
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(sp, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, reason)
    # Cells whose flags are False and whose ids match their rows pass, and the
    # literal partition is the one the plain frame gives.
    agreeing = with_cells(
        [False] * 4,
        [{"nct_id": "NCT99000101", "in_audit": False}, None, None, {"nct_id": "NCT99000102", "in_audit": False}],
    )
    assert eco.partition_ecosystem(agreeing, shipped_ecosystem_rules) == eco.partition_ecosystem(
        _holdout_frame(), shipped_ecosystem_rules
    )


def test_frame_nct_id_sources_must_agree(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Two columns (or a column and an index level) whose names read as
    # nct_id are both read; where they disagree on a row the frame is
    # refused rather than one of them chosen.
    disagree = _holdout_frame([False] * 4)
    disagree["NCT ID"] = pd.Series(
        ["NCT99000101", "NCT99000101", "NCT99000101", "NCT99000102"], dtype=object
    )
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(disagree, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
        assert "row position 2" in str(exc_info.value)
    # set_index("nct_id", drop=False) keeps the id in both places, agreeing.
    both = _holdout_frame([False] * 4).set_index("nct_id", drop=False)
    assert eco.partition_ecosystem(both, shipped_ecosystem_rules) == eco.partition_ecosystem(
        _holdout_frame(), shipped_ecosystem_rules
    )


def test_frame_and_index_entry_points_refuse_non_dataframe_input(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # The holdout check needs a frame's columns and row index, so any other
    # input is refused with TypeError, whether it carries a flag or not
    # (before the consolidation, one that carried a flag was refused as a
    # holdout instead).
    clean = _holdout_frame()
    for bad in ({c: clean[c] for c in clean.columns}, clean.to_dict("records"), clean.iloc[0], [], None):
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(TypeError):
                entry(bad, shipped_rules, shipped_ecosystem_rules)
        for name, entry in _INDEX_ENTRY_POINTS:
            with pytest.raises(TypeError):
                entry(bad)
    as_rows = _holdout_frame([False, False, True, False]).to_dict("records")
    odd_mapping = {c: clean[c] for c in clean.columns}
    odd_mapping["in_audit"] = ["False"] * 4
    for bad in (as_rows, odd_mapping):
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(TypeError, match="expects a pandas DataFrame"):
                entry(bad, shipped_rules, shipped_ecosystem_rules)


def test_attribute_study_sponsors_refuses_clean_non_dict_records_by_type(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Before the consolidation a UserDict, a MappingProxyType or a pandas
    # Series row carrying in_audit False with a canonical id was scanned and
    # attributed exactly as the dict. A record must now be exactly a dict
    # (src/holdout_guard.py): the others are refused by type, flag or not,
    # while the dicts themselves are attributed.
    def records(wrap: Any, **flag: Any) -> list[Any]:
        return [
            wrap(_stored_record("NCT99000331", **flag)),
            wrap(_api_record("NCT99000332", **flag)),
        ]

    plain = _redteam_run(records(dict, in_audit=False), shipped_rules, shipped_ecosystem_rules)
    assert len(plain[0]) > 0
    for wrapper_name, wrap in _redteam_mapping_wrappers():
        for flag in ({"in_audit": False}, {}):
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                _redteam_run(records(wrap, **flag), shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_not_bool")
            assert wrapper_name


def test_attribute_study_sponsors_refuses_a_manifest_flag_for_another_trial(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A nested manifest whose nct_id is another trial's describes that
    # trial; refused. One naming this trial changes nothing. CT.gov's
    # nctIdAliases does not read as nct_id, so it is no disagreement.
    for make in (_stored_record, _api_record):
        other = make("NCT99000333", manifest={"nct_id": "NCT99000334", "in_audit": False})
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([other], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
        same = make("NCT99000333", manifest={"nct_id": "NCT99000333", "in_audit": False})
        got = _redteam_run([same], shipped_rules, shipped_ecosystem_rules)
        want = _redteam_run([make("NCT99000333")], shipped_rules, shipped_ecosystem_rules)
        pd.testing.assert_frame_equal(got[0], want[0])
        assert got[1] == want[1]
    aliased = _api_record("NCT99000335", in_audit=False)
    aliased["protocolSection"]["identificationModule"]["nctIdAliases"] = ["NCT99000336"]
    got = _redteam_run([aliased], shipped_rules, shipped_ecosystem_rules)
    assert eco.trial_cohorts(got[0])["NCT99000335"] == ["academic_medical_center", "industry"]


def test_attribute_study_sponsors_batch_gap_is_refused_in_either_order(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # The record without in_audit may come first; the batch is still refused
    # before anything is adapted.
    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("adapted a batch with a record missing in_audit")

    monkeypatch.setattr(eco.adapter, "records_to_frame", must_not_run)
    batch = [_stored_record("NCT99000337"), _api_record("NCT99000338", in_audit=False)]
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run(iter(batch), shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")
    assert "position 0" in str(exc_info.value)


def test_attribute_study_sponsors_refuses_input_that_is_not_a_batch(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Iterating a mapping or a string yields keys or characters, not records.
    clean = _stored_record("NCT99000339", in_audit=False)
    for single in (
        clean,
        collections.UserDict(clean),
        pd.Series(clean),
        pd.DataFrame([clean]),
        "NCT99000339",
    ):
        with pytest.raises(TypeError):
            _redteam_run(single, shipped_rules, shipped_ecosystem_rules)


def test_namedtuple_and_attrs_holdouts_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # df.itertuples() rows are namedtuples, not dicts: refused by type
    # ("in_audit_not_bool"). A True in a frame's attrs metadata refuses the
    # frame; a non-bool there is refused as one.
    rows = list(pd.DataFrame([_stored_record("NCT99000340", in_audit=True)]).itertuples(index=False))
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run(rows, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")
    for flag, reason in ((True, "in_audit_true"), ("yes", "in_audit_not_bool")):
        held = _holdout_frame()
        held.attrs["in_audit"] = flag
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, reason)
        idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
        idx.attrs["manifest"] = {"in_audit": flag}
        for name, entry in _INDEX_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(idx)
            _assert_refused(exc_info.value, reason)


def test_a_false_flag_in_attrs_does_not_refuse_frames_derived_from_a_clean_one(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # pandas copies attrs onto derived frames (copy, merge, concat), so the
    # index built from a clean flagged frame can inherit attrs in_audit False
    # without its row-level column. That False alone is ignored, so the
    # clean pipeline gives what the unflagged one gives.
    flagged = _holdout_frame([False] * 4)
    flagged.attrs["in_audit"] = False
    baseline = eco.compute_ecosystem_baseline(flagged, shipped_ecosystem_rules)
    assert baseline == eco.compute_ecosystem_baseline(_holdout_frame(), shipped_ecosystem_rules)
    idx = eco.build_unified_index(flagged, shipped_rules, shipped_ecosystem_rules)
    plain_idx = eco.build_unified_index(_holdout_frame(), shipped_rules, shipped_ecosystem_rules)
    assert eco.trial_cohorts(idx) == eco.trial_cohorts(plain_idx)


# ---------------------------------------------------------------------------
# 13. Red team round 2 (test_redteam_m2_*). Each test asserts the correct
# behaviour: a frame, index or record that marks a trial in_audit, however
# it is shaped, is refused before anything is attributed, counted or
# recorded. Tests for a bypass fail until the module is fixed. Every id is
# fictional (NCT99000001..NCT99999999); nothing touches the network, and no
# file is read except the shipped rules and, for the dc38220 claims, git
# objects of this repository.
# ---------------------------------------------------------------------------

_REDTEAM_HOLDOUT_ID: str = "NCT99000101"  # the first trial of _HOLDOUT_ROWS

# Names that qualify in_audit rather than spell it and are read as the flag
# (src/holdout_guard.py is_flag_key): pandas' default merge suffixes _x/_y,
# a json_normalize path (sep "."), an "is_" prefix, and a custom join suffix
# or prefix (in_audit_manifest, manifest_in_audit), read because their words
# hold "in" then "audit".
_REDTEAM_QUALIFIED_IN_AUDIT_NAMES: list[str] = [
    "in_audit_x",
    "in_audit_y",
    "manifest.in_audit",
    "is_in_audit",
    "in_audit_manifest",
    "manifest_in_audit",
]


def _redteam_manifest_frame() -> pd.DataFrame:
    """The current manifest: the first fictional trial is a holdout."""
    return pd.DataFrame(
        {"nct_id": [_REDTEAM_HOLDOUT_ID, "NCT99000102"], "in_audit": [True, False]}
    )


def _redteam_nested_rows(manifest_of: Callable[[str], Any]) -> list[dict[str, Any]]:
    """The fictional sponsor rows as records, each with a nested ``manifest`` value."""
    return [
        {
            "nct_id": nct_id,
            "agency_class": agency_class,
            "lead_or_collaborator": role,
            "name": name,
            "manifest": manifest_of(nct_id),
        }
        for nct_id, agency_class, role, name in _HOLDOUT_ROWS
    ]


def _redteam_assert_frame_refused(
    held: pd.DataFrame, shipped_rules: pd.DataFrame, shipped_ecosystem_rules: pd.DataFrame
) -> None:
    """Every frame entry point refuses ``held`` as a holdout, naming no trial."""
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(held.copy(), shipped_rules, shipped_ecosystem_rules)
        assert "NCT99" not in str(exc_info.value), name


def test_redteam_m2_frame_merge_suffixed_in_audit_columns_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a sponsor frame that already carries a stale in_audit False
    # is merged with the current manifest, which marks the first trial a
    # holdout. pandas suffixes both columns (in_audit_x, in_audit_y) and
    # neither folds to "inaudit". Observed: every frame entry point
    # attributes and counts the holdout (build_ecosystem_index returns 3
    # rows, two of them the holdout's), and the index entry points count it
    # after the same merge. Correct: refused.
    merged = _holdout_frame([False] * 4).merge(_redteam_manifest_frame(), on="nct_id", how="left")
    assert {"in_audit_x", "in_audit_y"} <= set(merged.columns)
    _redteam_assert_frame_refused(merged, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False])
    idx = idx.merge(_redteam_manifest_frame(), on="nct_id", how="left")
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(idx.copy())


def test_redteam_m2_frame_json_normalized_manifest_flag_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: sponsor records with a merged manifest sub-record, flattened
    # with pd.json_normalize, the usual way to turn nested records into a
    # frame. The flag becomes the column "manifest.in_audit"; the "." is
    # not ignored by the folding. Observed: attributed and counted.
    # Correct: refused.
    flat = pd.json_normalize(
        _redteam_nested_rows(lambda n: {"nct_id": n, "in_audit": n == _REDTEAM_HOLDOUT_ID})
    )
    assert "manifest.in_audit" in flat.columns
    _redteam_assert_frame_refused(flat, shipped_rules, shipped_ecosystem_rules)


@pytest.mark.parametrize("key", _REDTEAM_QUALIFIED_IN_AUDIT_NAMES)
def test_redteam_m2_qualified_in_audit_names_are_refused(
    key: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout flag under a name that qualifies in_audit, as a
    # frame column, an index column, a record key and a frame.attrs key.
    # _folded_name compares the whole folded name with "inaudit".
    # Observed: every entry point attributes or counts the holdout.
    # Correct: a name that contains in_audit as a component reads as it.
    held = _holdout_frame([True] * 4).rename(columns={"in_audit": key})
    _redteam_assert_frame_refused(held, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [True])
    idx = idx.rename(columns={"in_audit": key})
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(idx)
    for make in (_stored_record, _api_record):
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([make(_REDTEAM_HOLDOUT_ID, **{key: True})], shipped_rules, shipped_ecosystem_rules)
    in_attrs = _holdout_frame()
    in_attrs.attrs[key] = True
    _redteam_assert_frame_refused(in_attrs, shipped_rules, shipped_ecosystem_rules)


def _redteam_unwalked_containers() -> list[tuple[str, Callable[[str], Any]]]:
    """Makers of a container holding a trial's manifest that _scan_in_audit does not open.

    Method: the scan walks mapping-like values, namedtuples, dataclass
    instances, list/tuple/set/frozenset and numpy object arrays. A
    types.SimpleNamespace (or any plain object with attributes), a
    collections.deque and a dict view are none of these.
    """

    def manifest(nct_id: str) -> dict[str, Any]:
        return {"nct_id": nct_id, "in_audit": nct_id == _REDTEAM_HOLDOUT_ID}

    return [
        ("SimpleNamespace", lambda n: types.SimpleNamespace(**manifest(n))),
        ("deque", lambda n: collections.deque([manifest(n)])),
        ("dict_values", lambda n: {"manifest": manifest(n)}.values()),
    ]


def test_redteam_m2_flags_in_containers_the_scan_does_not_walk_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the merged manifest is held in a container the scan does not
    # walk, as a record value or as the cells of a frame column. Observed:
    # the holdout record is attributed (2 index rows) and the frame entry
    # points attribute and count the holdout. Correct: refused.
    for label, make in _redteam_unwalked_containers():
        record = _stored_record(_REDTEAM_HOLDOUT_ID, manifest=make(_REDTEAM_HOLDOUT_ID))
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([record], shipped_rules, shipped_ecosystem_rules)
        held = pd.DataFrame(_redteam_nested_rows(make))
        _redteam_assert_frame_refused(held, shipped_rules, shipped_ecosystem_rules)
        assert label


def test_redteam_m2_nested_sub_record_id_that_disagrees_with_its_trial_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: in_audit False sits where it is read (a record's top level, a
    # frame's column) and a nested manifest without a flag names another
    # trial: the manifest row was merged onto the wrong trial, so the False
    # may describe the other one. Only top-level ids and ids beside a flag
    # are compared. Observed: attributed under the record's or row's own
    # id. Correct: "nct_id_malformed". The same manifest naming the trial's
    # own id is accepted, unchanged.
    own = _stored_record("NCT99000330", in_audit=False, manifest={"nct_id": "NCT99000330"})
    plain = _stored_record("NCT99000330", in_audit=False)
    _assert_same(
        _redteam_run([own], shipped_rules, shipped_ecosystem_rules)[0],
        _redteam_run([plain], shipped_rules, shipped_ecosystem_rules)[0],
    )
    same_cells = _holdout_frame([False] * 4)
    same_cells["manifest"] = [{"nct_id": n, "cohorts": ["redteam"]} for n in same_cells["nct_id"]]
    for name, entry in _FRAME_ENTRY_POINTS:
        _assert_same(
            entry(same_cells.copy(), shipped_rules, shipped_ecosystem_rules),
            entry(_holdout_frame([False] * 4), shipped_rules, shipped_ecosystem_rules),
        )
    for record in (
        _stored_record("NCT99000330", in_audit=False, manifest={"nct_id": "NCT99000331"}),
        _api_record("NCT99000330", in_audit=False, manifest={"nctId": "NCT99000331"}),
    ):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([record], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
    other_cells = _holdout_frame([False] * 4)
    other_cells["manifest"] = [{"nct_id": "NCT99000109", "cohorts": ["redteam"]}] * 4
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(other_cells.copy(), shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")


def test_redteam_m2_literal_universe_functions_refuse_a_frame_carrying_in_audit(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: match_ecosystem_literals and check_cross_layer_conflicts are
    # documented as "not a trial entry point" because `uniq` is a literal
    # universe with no nct_id. Nothing checks that. Hand them a universe
    # that still carries the holdout's nct_id and in_audit True. Observed:
    # match_ecosystem_literals returns the holdout's 3 matched literals,
    # check_cross_layer_conflicts returns None. Correct: refused (the
    # sponsor frame itself, given by mistake, already fails with KeyError).
    uniq = sr.literal_universe(_holdout_frame()).assign(
        nct_id=_REDTEAM_HOLDOUT_ID, in_audit=True
    )
    with pytest.raises(eco.EcosystemAuditHoldoutError):
        eco.match_ecosystem_literals(uniq, shipped_ecosystem_rules)
    with pytest.raises(eco.EcosystemAuditHoldoutError):
        eco.check_cross_layer_conflicts(uniq, shipped_rules, shipped_ecosystem_rules)
    with pytest.raises(KeyError):
        eco.match_ecosystem_literals(_holdout_frame([True] * 4), shipped_ecosystem_rules)


def test_redteam_m2_records_whose_field_names_the_scan_cannot_read_are_refused_before_adapting(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout record as a row type whose field names the scan
    # does not read: numpy.record (df.to_records()), a namedtuple whose
    # in_audit field itertuples() renamed (a column "In Audit" becomes
    # "_N"), a plain tuple (itertuples(name=None)), and one namedtuple
    # passed where a batch is expected. Observed: the holdout check passes
    # each one; the first three reach sponsors.adapter.records_to_frame
    # (patched here to fail; unpatched it raises AttributeError, "no
    # attribute 'get'"), and the single namedtuple fails with an incidental
    # TypeError ("argument of type 'bool' is not iterable") in
    # _normalize_study_record_for_adapter. Nothing is attributed today, but
    # only by accident. Correct: refused before any record is adapted.
    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("adapted a batch whose holdout check could not read it")

    monkeypatch.setattr(eco.adapter, "records_to_frame", must_not_run)
    df = pd.DataFrame([_stored_record(_REDTEAM_HOLDOUT_ID, in_audit=True)])
    renamed = df.rename(columns={"in_audit": "In Audit"})
    assert next(renamed.itertuples(index=False))._fields[-1].startswith("_")
    batches: list[tuple[str, Any]] = [
        ("to_records", list(df.to_records(index=False))),
        ("itertuples with a renamed field", list(renamed.itertuples(index=False))),
        ("itertuples(name=None)", list(df.itertuples(index=False, name=None))),
    ]
    for label, batch in batches:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run(batch, shipped_rules, shipped_ecosystem_rules)
        assert label
    with pytest.raises((eco.EcosystemAuditHoldoutError, TypeError)) as exc_info:
        _redteam_run(next(df.itertuples(index=False)), shipped_rules, shipped_ecosystem_rules)
    if exc_info.type is TypeError:
        assert "attribute_study_sponsors" in str(exc_info.value)


def test_redteam_m2_audit_id_set_passed_positionally_fails_loudly(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): the audit id set passed positionally, where a rules
    # frame, a sector, a canonical or a role is expected. Expected and
    # observed: every call raises (AttributeError, TypeError or
    # EcosystemRuleValidationError) and nothing is returned.
    audit_ids: list[Any] = [_REDTEAM_HOLDOUT_ID, "nct99000101", None]
    sp = _holdout_frame()
    idx = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)
    calls: list[Callable[[], Any]] = [
        lambda: eco.build_ecosystem_index(sp, audit_ids),
        lambda: eco.partition_ecosystem(sp, audit_ids),
        lambda: eco.build_unified_index(sp, audit_ids, shipped_ecosystem_rules),
        lambda: eco.partition_unified(sp, shipped_rules, audit_ids),
        lambda: eco.audit_ecosystem(sp, audit_ids, "National Cancer Institute (NCI)", "cancer"),
        lambda: eco.uncovered_ecosystem_rules(sp, audit_ids),
        lambda: eco.compute_ecosystem_baseline(sp, audit_ids),
        lambda: eco.sector_trials(idx, audit_ids),
        lambda: eco.organization_trials(idx, "National Cancer Institute (NCI)", audit_ids),
        lambda: eco.trial_cohorts(idx, audit_ids),
        lambda: eco.attribute_study_sponsors([_stored_record(_REDTEAM_HOLDOUT_ID)], audit_ids),
    ]
    for call in calls:
        with pytest.raises(Exception):
            call()


def _redteam_undocumented_callers(module: types.ModuleType) -> dict[str, list[str]]:
    """Public in-module callers missing from each docstring's "Callers today:".

    Method: AST only. A public function reaches a target when it calls it
    directly or through private helpers (a "_" name, followed transitively).
    For every top-level function whose whitespace-normalised docstring has
    "Callers today:", the public functions reaching it must all be named in
    the text after that marker. Returns {function: [missing callers]}.
    """
    tree = ast.parse(inspect.getsource(module))
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    calls: dict[str, set[str]] = {
        name: {
            sub.func.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in funcs
        }
        for name, node in funcs.items()
    }

    def public_reachers(target: str) -> set[str]:
        found: set[str] = set()
        frontier, seen = [target], {target}
        while frontier:
            current = frontier.pop()
            for caller, callees in calls.items():
                if current not in callees or caller == current:
                    continue
                if caller.startswith("_"):
                    if caller not in seen:
                        seen.add(caller)
                        frontier.append(caller)
                else:
                    found.add(caller)
        return found

    missing: dict[str, list[str]] = {}
    for name, node in funcs.items():
        doc = " ".join((ast.get_docstring(node) or "").split())
        marker = re.search(r"Callers today:(.*)", doc)
        if marker is None:
            continue
        named = {f for f in funcs if re.search(rf"(?<![\w.]){re.escape(f)}(?!\w)", marker.group(1))}
        gaps = sorted(public_reachers(name) - named)
        if gaps:
            missing[name] = gaps
    return missing


def test_redteam_m2_documented_callers_name_every_in_module_caller() -> None:
    # Claim under test: each docstring's "Callers today:" names every public
    # function of this module that reaches it, because those callers carry
    # the duty to exclude holdout trials. Expected and observed: it does.
    assert _redteam_undocumented_callers(eco) == {}


class _RedteamIoRecorder:
    """Process-wide audit hook state: inert unless ``events`` is a list.

    Method: sys.addaudithook sees every file open, socket, subprocess and
    os-level call made by any code (gzip.open, pathlib, urllib, shutil and
    tempfile included), which monkeypatching a few names cannot. A hook
    cannot be removed, so it is installed once and records only while a
    test sets ``events``.
    """

    installed: bool = False
    events: list[str] | None = None
    prefixes: tuple[str, ...] = (
        "os.", "socket.", "subprocess.", "shutil.", "urllib.", "http.", "ftplib.",
        "smtplib.", "sqlite3.", "tempfile.", "glob.", "webbrowser.",
    )


def _redteam_io_audit_hook(event: str, _args: tuple[Any, ...]) -> None:
    """Record a file, process or network audit event while recording is on."""
    events = _RedteamIoRecorder.events
    if events is not None and (event == "open" or event.startswith(_RedteamIoRecorder.prefixes)):
        events.append(event)


def _redteam_io_events_during(calls: list[Callable[[], Any]]) -> list[str]:
    """Audit events for file, process or network access raised while ``calls`` run.

    Method: every call runs once unrecorded first, so lazy imports (which
    open files) happen before recording starts; then again while
    recording. Exceptions are swallowed in both runs (refusals are calls
    too) and nothing is formatted while recording, so no traceback reads a
    source file.
    """
    if not _RedteamIoRecorder.installed:
        sys.addaudithook(_redteam_io_audit_hook)
        _RedteamIoRecorder.installed = True
    for call in calls:
        try:
            call()
        except Exception:  # noqa: BLE001 - refusals are expected here
            pass
    _RedteamIoRecorder.events = []
    try:
        for call in calls:
            try:
                call()
            except Exception:  # noqa: BLE001
                pass
        return list(_RedteamIoRecorder.events)
    finally:
        _RedteamIoRecorder.events = None


def test_redteam_m2_entry_points_raise_no_io_audit_event(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Claim under test: with rules passed in, the trial entry points (where
    # they rely on the caller, and when they refuse) and the functions that
    # are not trial-shaped, other than the documented loaders and writer,
    # read only the objects they are handed. Round 1 patched a list of
    # names; this records every file, process and network audit event. The
    # harness is checked first: an open() while recording must be seen.
    assert "open" in _redteam_io_events_during([lambda: open(os.devnull).close()])
    sp = _holdout_frame()
    held = _holdout_frame([True] * 4)
    idx = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)
    uniq = sr.literal_universe(sp)
    records = [_stored_record("NCT99000317"), _api_record("NCT99000318")]
    calls: list[Callable[[], Any]] = [
        lambda: eco.validate_ecosystem_rules(shipped_ecosystem_rules),
        lambda: eco.match_ecosystem_literals(uniq, shipped_ecosystem_rules),
        lambda: eco.check_cross_layer_conflicts(uniq, shipped_rules, shipped_ecosystem_rules),
        lambda: eco.diff_ecosystem_blocks({"index": {"rows": 1}}, {"index": {"rows": 2}}),
        lambda: _redteam_run(records, shipped_rules, shipped_ecosystem_rules),
        lambda: _redteam_run(
            [_stored_record("NCT99000319", in_audit=True)], shipped_rules, shipped_ecosystem_rules
        ),
    ]
    for name, entry in _FRAME_ENTRY_POINTS:
        calls.append(lambda entry=entry: entry(sp, shipped_rules, shipped_ecosystem_rules))
        calls.append(lambda entry=entry: entry(held, shipped_rules, shipped_ecosystem_rules))
    for name, entry in _INDEX_ENTRY_POINTS:
        calls.append(lambda entry=entry: entry(idx))
    assert _redteam_io_events_during(calls) == []


def _redteam_git_show(revision: str, path: str) -> str:
    """``git show revision:path`` from this repository, or skip when unavailable.

    Method: the claims below are about PR #97's own branch (dc38220), which
    this branch may not contain; a shallow CI checkout lacks the object, so
    the test skips there rather than fetch anything.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    probe = subprocess.run(
        ["git", "-C", REPO_ROOT, "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"commit {revision} is not in this checkout")
    shown = subprocess.run(
        ["git", "-C", REPO_ROOT, "show", f"{revision}:{path}"],
        capture_output=True,
        check=True,
        text=True,
    )
    return shown.stdout


def test_redteam_m2_dc38220_caller_claims_in_the_ecosystem_docstring_hold() -> None:
    # Claim under test (module docstring): at dc38220, src/outcomes_layout.py
    # does not import this module; _study_scalar_fields reads
    # ecosystem_category from the trial; build_trial_bundle passes it to
    # derive_cohorts and copies it into the index row for every trial; no
    # firewall vector (FORBIDDEN_AUDIT_KEYS, assert_no_audit_leakage)
    # checks that field. scripts/build_outcomes_shards.py does not import
    # this module either. Only source code is read.
    layout = ast.parse(_redteam_git_show("dc38220", "src/outcomes_layout.py"))
    shards = ast.parse(_redteam_git_show("dc38220", "scripts/build_outcomes_shards.py"))
    for tree in (layout, shards):
        imported = [
            alias.name if isinstance(node, ast.Import) else f"{node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        assert not any(name.endswith("ecosystem") for name in imported)
    funcs = {n.name: n for n in layout.body if isinstance(n, ast.FunctionDef)}
    assert any(
        isinstance(call, ast.Call) and ast.unparse(call) == "trial.get('ecosystem_category')"
        for call in ast.walk(funcs["_study_scalar_fields"])
    )
    bundle = funcs["build_trial_bundle"]
    assert any(
        isinstance(call, ast.Call)
        and ast.unparse(call.func) == "derive_cohorts"
        and any(
            kw.arg == "ecosystem_category" and ast.unparse(kw.value) == "meta['ecosystem_category']"
            for kw in call.keywords
        )
        for call in ast.walk(bundle)
    )
    assert any(
        isinstance(node, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "ecosystem_category" for k in node.keys)
        for node in ast.walk(bundle)
    )
    forbidden = next(
        node.value
        for node in ast.walk(layout)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "FORBIDDEN_AUDIT_KEYS"
    )
    keys = {c.value for c in ast.walk(forbidden) if isinstance(c, ast.Constant)}
    assert keys and "ecosystem_category" not in keys
    assert "ecosystem_category" not in ast.unparse(funcs["assert_no_audit_leakage"])


def test_redteam_m2_chainmap_multiindex_and_gap_rule_attempts_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): a ChainMap layering in_audit True over a stored
    # record (not a dict: refused by type); a frame whose MultiIndex columns
    # hold ("manifest", "in_audit") (tuple labels: refused by type); and a
    # record batch whose clean records carry in_audit False while the
    # holdout carries only in_audit_y True (read as the flag: in_audit_true).
    # Expected and observed: refused.
    chained = collections.ChainMap({"in_audit": True}, _stored_record(_REDTEAM_HOLDOUT_ID))
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run([chained], shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")
    wide = pd.concat(
        {"sponsor": _holdout_frame(), "manifest": pd.DataFrame({"in_audit": [True] * 4})}, axis=1
    )
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(wide, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
    for position in (0, 1, 2):
        records: list[Any] = [_stored_record("NCT99000332", in_audit=False) for _ in range(2)]
        records.insert(position, _stored_record(_REDTEAM_HOLDOUT_ID, in_audit_y=True))
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run(records, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_true")
        assert f"position {position}" in str(exc_info.value)


@pytest.mark.parametrize(
    "variant",
    ["nct99000101", " NCT99000101", "NCT99000101​", "NCT99000101\n", "ＮＣＴ99000101"],
)
def test_redteam_m2_odd_id_beside_a_cell_flag_is_refused(
    variant: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): the row-level nct_id is canonical and a manifest
    # cell beside its in_audit False carries a lowercase, padded,
    # zero-width, newline or full-width variant of the first trial's id.
    # Expected and observed: "nct_id_malformed" for that row's mismatch.
    held = _holdout_frame([False] * 4)
    held["manifest"] = [
        {"nct_id": variant if n == _REDTEAM_HOLDOUT_ID else n, "in_audit": False}
        for n in held["nct_id"]
    ]
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(held, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")


def test_redteam_m2_odd_flags_in_attrs_and_cells_and_ids_only_in_cells_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): in_audit in frame.attrs or inside a cell as "false",
    # 0, None or numpy.False_ (none exactly a Python bool); and an in_audit
    # column on a frame whose nct_id exists only inside cells. Expected and
    # observed: "in_audit_not_bool" for the first two, "nct_id_malformed"
    # for the last.
    np = pytest.importorskip("numpy")
    for odd in ("false", 0, None, np.False_):
        in_attrs = _holdout_frame()
        in_attrs.attrs["manifest"] = {"in_audit": odd}
        in_cells = _holdout_frame()
        in_cells["manifest"] = [{"nct_id": n, "in_audit": odd} for n in in_cells["nct_id"]]
        for held in (in_attrs, in_cells):
            for name, entry in _FRAME_ENTRY_POINTS:
                with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                    entry(held, shipped_rules, shipped_ecosystem_rules)
                _assert_refused(exc_info.value, "in_audit_not_bool")
    ids_in_cells = _holdout_frame([False] * 4)
    ids_in_cells["manifest"] = [{"nct_id": n} for n in ids_in_cells["nct_id"]]
    ids_in_cells = ids_in_cells.drop(columns=["nct_id"])
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(ids_in_cells, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")


# ---------------------------------------------------------------------------
# 14. Holdout fixes for red team round 2: cases the fix covers beyond the red
# team's own tests, and inputs that must still pass. Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network, and no file is
# read except the shipped rules.
# ---------------------------------------------------------------------------


def test_sponsor_names_that_look_bracketed_change_nothing(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Sponsor names are text cells. Only text that serializes a container
    # with in_audit or nct_id in key position is read, so names in brackets
    # or ending in ")" (and prose that mentions an audit) pass unchanged,
    # with or without an in_audit column.
    odd_names = [
        ("NCT99000401", "OTHER", "lead", "[Practices in audit arm: audit and feedback]"),
        ("NCT99000401", "OTHER", "collaborator", "Sponsor(Holdings)"),
        ("NCT99000402", "OTHER", "lead", "{in audit}"),
    ]
    plain = frame(_HOLDOUT_ROWS + odd_names)
    flagged = plain.copy()
    flagged["in_audit"] = pd.Series([False] * len(flagged), dtype=object)
    for name, entry in _FRAME_ENTRY_POINTS:
        _assert_same(
            entry(flagged, shipped_rules, shipped_ecosystem_rules),
            entry(plain, shipped_rules, shipped_ecosystem_rules),
        )
    partition = eco.partition_ecosystem(plain, shipped_ecosystem_rules)
    assert {name for *_, name in odd_names} <= partition["unreviewed"]


def test_cells_that_cannot_be_read_are_refused_without_a_flag(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A cell holding a one-shot iterator, or a bool that lost its name, may
    # hold a flag: refused even when the frame carries no in_audit column.
    for make in (
        lambda n: (x for x in [{"nct_id": n, "in_audit": False}]),
        lambda n: [n, False],
    ):
        held = _holdout_frame()
        held["manifest"] = [make(n) for n in held["nct_id"]]
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_not_bool")


def test_string_dtype_and_json_normalized_id_columns_are_read(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A json_normalize "manifest.nct_id" column is an nct_id source that must
    # agree with "nct_id" row by row. (A string-dtype column of serialized
    # manifests is text and is no longer read; see src/holdout_guard.py.)
    flat = pd.json_normalize(
        _redteam_nested_rows(lambda n: {"nct_id": "NCT99000109", "in_audit": False})
    )
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(flat.copy(), shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
    agreeing = pd.json_normalize(_redteam_nested_rows(lambda n: {"nct_id": n, "in_audit": False}))
    for name, entry in _FRAME_ENTRY_POINTS:
        _assert_same(
            entry(agreeing.copy(), shipped_rules, shipped_ecosystem_rules),
            entry(_holdout_frame(), shipped_rules, shipped_ecosystem_rules),
        )


def test_literal_universe_functions_keep_their_contract(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A universe sr.literal_universe builds passes unchanged; a frame without
    # its columns is a KeyError, as before; a hand-built universe carrying
    # in_audit False beside the names (no nct_id) is refused as malformed.
    uniq = sr.literal_universe(_holdout_frame())
    matched = eco.match_ecosystem_literals(uniq, shipped_ecosystem_rules)
    assert len(matched) >= 3
    assert eco.check_cross_layer_conflicts(uniq, shipped_rules, shipped_ecosystem_rules) is None
    with pytest.raises(KeyError):
        eco.check_cross_layer_conflicts(uniq[["name"]], shipped_rules, shipped_ecosystem_rules)
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        eco.match_ecosystem_literals(uniq.assign(in_audit=False), shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "nct_id_malformed")


def test_records_batch_rules_and_single_named_records(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Before any record is adapted: records carrying in_audit under
    # different names are each checked (the True is reported as
    # in_audit_true at its position; the earlier rule refusing mixed names
    # first is gone); a nested one-shot iterator is refused by type; a
    # qualified True is reported as in_audit_true; a dataclass instance
    # passed as the batch is a TypeError naming the function.
    import dataclasses

    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("adapted a batch the holdout check should have refused")

    monkeypatch.setattr(eco.adapter, "records_to_frame", must_not_run)
    mixed = [
        _stored_record(_REDTEAM_HOLDOUT_ID, in_audit=True),
        _stored_record("NCT99000332", in_audit_y=False),
    ]
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run(mixed, shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_true")
    assert "position 0" in str(exc_info.value)
    lazy = _stored_record("NCT99000333", collaborators=(c for c in [{"name": "Pfizer"}]))
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run([lazy], shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")
    for key in ("in_audit_y", "manifest.in_audit", "is_in_audit"):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run(
                [_api_record(_REDTEAM_HOLDOUT_ID, **{key: True})],
                shipped_rules,
                shipped_ecosystem_rules,
            )
        _assert_refused(exc_info.value, "in_audit_true")

    @dataclasses.dataclass
    class Row:
        nct_id: str

    with pytest.raises(TypeError) as type_info:
        _redteam_run(Row("NCT99000334"), shipped_rules, shipped_ecosystem_rules)
    assert "attribute_study_sponsors" in str(type_info.value)


# ---------------------------------------------------------------------------
# 14. Red team round 3 (test_redteam_m2_*). Each test asserts the correct
# behaviour: a frame, index or record that marks a trial in_audit is refused
# before anything is attributed, counted or recorded. Tests for a bypass
# fail until the module is fixed; tests for attempts that are already
# refused say so ("Attack (refused)"). Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network, and no file is
# read except the shipped rules and baseline, or written except under
# pytest's tmp_path.
# ---------------------------------------------------------------------------

_REDTEAM3_OTHER_ID: str = "NCT99000102"  # the second trial of _HOLDOUT_ROWS


def _redteam3_must_not_adapt(*_args: Any, **_kwargs: Any) -> Any:
    """Stands in for sponsors.adapter.records_to_frame: reaching it is the bypass."""
    raise AssertionError("adapted a batch that carries a holdout")


def _redteam3_index_named_rows(merged: pd.DataFrame) -> list[tuple[str, list[Any]]]:
    """Row batches of a manifest-merged frame whose in_audit became the row index.

    Method: iterrows() and apply(axis=1) yield Series rows that keep the
    flag's value only as their .name (True, or (position, True) with
    append=True); the level name "in_audit" stays on the frame.
    """
    indexed = merged.set_index("in_audit")
    applied: list[Any] = []
    # copy(): apply may hand every row in one reused Series object.
    indexed.apply(lambda row: applied.append(row.copy()), axis=1)
    return [
        ("iterrows after set_index('in_audit')", [r for _, r in indexed.iterrows()]),
        (
            "iterrows after set_index('in_audit', append=True)",
            [r for _, r in merged.set_index("in_audit", append=True).iterrows()],
        ),
        ("apply(axis=1) rows after set_index('in_audit')", applied),
    ]


def test_redteam_m2_attribute_study_sponsors_refuses_rows_whose_index_value_holds_in_audit(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a manifest-merged batch (holdout True, other trial False),
    # indexed by in_audit and handed over as Series rows. No row has an
    # "in_audit" label, and a bool .name is never read as a flag that lost
    # its name, so no row carries a flag and the gap rule has nothing to
    # compare. Observed (stored and CT.gov records alike): attributed in
    # full, 2 index rows for the holdout and trial_cohorts giving its
    # sectors. Correct: refused. itertuples() keeps the flag as the field
    # "Index"; those rows pass the check and only an AttributeError in the
    # adapter stops them (patched here to fail), so they must be refused
    # before adapting.
    for make in (_stored_record, _api_record):
        merged = pd.DataFrame(
            [make(_REDTEAM_HOLDOUT_ID, in_audit=True), make(_REDTEAM3_OTHER_ID, in_audit=False)]
        )
        for label, rows in _redteam3_index_named_rows(merged):
            with pytest.raises(eco.EcosystemAuditHoldoutError):
                _redteam_run(rows, shipped_rules, shipped_ecosystem_rules)
            assert label
        monkeypatch.setattr(eco.adapter, "records_to_frame", _redteam3_must_not_adapt)
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run(
                list(merged.set_index("in_audit").itertuples()), shipped_rules, shipped_ecosystem_rules
            )
        monkeypatch.undo()


# Invisible characters the name folding does not drop (it drops zero-width
# space/joiners, the word joiner, the soft hyphen and the BOM).
_REDTEAM3_INVISIBLE: list[str] = [
    "‎",  # LEFT-TO-RIGHT MARK
    "‏",  # RIGHT-TO-LEFT MARK
    "؜",  # ARABIC LETTER MARK
    "‪",  # LEFT-TO-RIGHT EMBEDDING
    "⁦",  # LEFT-TO-RIGHT ISOLATE
    "⁣",  # INVISIBLE SEPARATOR
    "️",  # VARIATION SELECTOR-16
    "᠎",  # MONGOLIAN VOWEL SEPARATOR
    "\U000e0020",  # TAG SPACE
]


@pytest.mark.parametrize("char", _REDTEAM3_INVISIBLE)
def test_redteam_m2_in_audit_name_with_other_invisible_characters_is_refused(
    char: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: in_audit True under "in<char>_audit" as a frame column, an
    # index column, a record key, a frame.attrs key and a literal-universe
    # column. Observed: every frame entry point attributes and counts the
    # holdout, the index entry points count it, attribute_study_sponsors
    # attributes it and the literal functions match its literals. Correct:
    # refused.
    key = f"in{char}_audit"
    held = _holdout_frame().assign(**{key: True})
    _redteam_assert_frame_refused(held, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None).assign(**{key: True})
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(idx.copy())
        assert name
    for make in (_stored_record, _api_record):
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([make(_REDTEAM_HOLDOUT_ID, **{key: True})], shipped_rules, shipped_ecosystem_rules)
    in_attrs = _holdout_frame()
    in_attrs.attrs[key] = True
    _redteam_assert_frame_refused(in_attrs, shipped_rules, shipped_ecosystem_rules)
    uniq = sr.literal_universe(_holdout_frame()).assign(nct_id=_REDTEAM_HOLDOUT_ID, **{key: True})
    with pytest.raises(eco.EcosystemAuditHoldoutError):
        eco.match_ecosystem_literals(uniq, shipped_ecosystem_rules)
    with pytest.raises(eco.EcosystemAuditHoldoutError):
        eco.check_cross_layer_conflicts(uniq, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_non_text_key_or_label_holding_a_text_flag_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: an enum member (str() gives "Key.IN_AUDIT") as a record key
    # and as a frame column label, holding the CSV string "True". A bool
    # under a non-text key is refused as unreadable; a string is a leaf, and
    # a non-text column label is never a flag source. Observed: attributed
    # and counted. Correct: refused.
    import enum

    class Key(enum.Enum):
        IN_AUDIT = "in_audit"

    for make in (_stored_record, _api_record):
        held = make(_REDTEAM_HOLDOUT_ID)
        held[Key.IN_AUDIT] = "True"
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
    framed = _holdout_frame()
    framed[Key.IN_AUDIT] = ["True", "True", "False", "False"]
    _redteam_assert_frame_refused(framed, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_chainmap_shadowing_a_true_flag_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a ChainMap layering {"in_audit": False} over the holdout's
    # record (in_audit True), and the same ChainMap in frame.attrs. The scan
    # read ChainMap.items(), where the True is shadowed. Observed: the record
    # was attributed in full; every frame entry point accepted the frame.
    # Correct: refused. A ChainMap is not a dict: refused by type
    # ("in_audit_not_bool") in a record and in attrs alike.
    for make in (_stored_record, _api_record):
        held = collections.ChainMap({"in_audit": False}, make(_REDTEAM_HOLDOUT_ID, in_audit=True))
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
    in_attrs = _holdout_frame()
    in_attrs.attrs["manifest"] = collections.ChainMap({"in_audit": False}, {"in_audit": True})
    _redteam_assert_frame_refused(in_attrs, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_in_audit_held_where_the_scan_reads_no_names_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: in_audit True as an instance attribute, property or class
    # attribute of a dict subclass, as an attribute beside a dataclass's or
    # namedtuple subclass's fields, inside a callable (functools.partial, a
    # callable namespace), as a bare Index's name, and on a str subclass.
    # _keyed_items reads items, _asdict() and declared fields only;
    # _attribute_items skips callables; a bare Index's name is read nowhere.
    # Observed: the dict subclasses and the record- or cell-held callables,
    # Index and str subclass are attributed and counted; the dataclass and
    # namedtuple subclass pass the check and are stopped only by accident
    # (a TypeError in _normalize_study_record_for_adapter for the
    # dataclass; the adapter, patched here to fail, for the namedtuple).
    # Correct: refused.
    import dataclasses
    import functools

    class WithAttribute(dict):  # type: ignore[type-arg]
        pass

    class WithProperty(dict):  # type: ignore[type-arg]
        @property
        def in_audit(self) -> bool:
            return True

    class WithClassFlag(dict):  # type: ignore[type-arg]
        in_audit = True

    @dataclasses.dataclass
    class Fields:
        nct_id: str

    Pair = collections.namedtuple("Pair", ["nct_id"])

    class PairWithDict(Pair):  # type: ignore[misc, valid-type]
        pass

    class CallableNamespace(types.SimpleNamespace):
        def __call__(self) -> None:
            return None

    class TaggedText(str):
        pass

    def tagged(text: str, flag: bool) -> TaggedText:
        value = TaggedText(text)
        value.in_audit = flag  # type: ignore[attr-defined]
        return value

    attr = WithAttribute(_stored_record(_REDTEAM_HOLDOUT_ID))
    attr.in_audit = True  # type: ignore[attr-defined]
    for held in (attr, WithProperty(_stored_record(_REDTEAM_HOLDOUT_ID)), WithClassFlag(_api_record(_REDTEAM_HOLDOUT_ID))):
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
    holders: list[tuple[str, Callable[[str, bool], Any]]] = [
        ("functools.partial", lambda n, f: functools.partial(dict, nct_id=n, in_audit=f)),
        ("callable namespace", lambda n, f: CallableNamespace(nct_id=n, in_audit=f)),
        ("pd.Index named in_audit", lambda n, f: pd.Index([str(f)], name="in_audit")),
        ("str subclass with an attribute", lambda n, f: tagged("cohort-a", f)),
    ]
    for label, holder in holders:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run(
                [_stored_record(_REDTEAM_HOLDOUT_ID, manifest=holder(_REDTEAM_HOLDOUT_ID, True))],
                shipped_rules,
                shipped_ecosystem_rules,
            )
        column = pd.Series(
            [holder(n, n == _REDTEAM_HOLDOUT_ID) for n, _, _, _ in _HOLDOUT_ROWS], dtype=object
        )
        _redteam_assert_frame_refused(
            _holdout_frame().assign(manifest=column), shipped_rules, shipped_ecosystem_rules
        )
        assert label
    monkeypatch.setattr(eco.adapter, "records_to_frame", _redteam3_must_not_adapt)
    fields = Fields(_REDTEAM_HOLDOUT_ID)
    fields.in_audit = True  # type: ignore[attr-defined]
    pair = PairWithDict(_REDTEAM_HOLDOUT_ID)
    pair.in_audit = True  # type: ignore[attr-defined]
    for held in (fields, pair):
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)


@pytest.mark.parametrize("key", ["nct_id_y", "nct_id_m", "nct_id_manifest", "NCT Number", "nctNumber"])
def test_redteam_m2_qualified_nct_id_name_that_disagrees_is_refused(
    key: str,
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the holdout's record or rows carry another trial's manifest
    # row (in_audit False) whose id sits under a merge-suffixed or
    # CT.gov-export name. Only names equal to or ending in nct_id are ids,
    # while in_audit is read under any qualified name. Observed: the record
    # is attributed in full, the frame and index entry points count the
    # holdout. Correct: refused as nct_id_malformed.
    for make in (_stored_record, _api_record):
        held = make(_REDTEAM_HOLDOUT_ID, in_audit=False, **{key: _REDTEAM3_OTHER_ID})
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "nct_id_malformed")
    framed = _holdout_frame([False] * 4).assign(**{key: _REDTEAM3_OTHER_ID})
    _redteam_assert_frame_refused(framed, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False]).assign(
        **{key: _REDTEAM3_OTHER_ID}
    )
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            entry(idx.copy())
        assert name


def test_redteam_m2_frame_joined_to_the_manifest_by_position_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: the sponsor frame joined to the manifest by row position (the
    # default of DataFrame.join), suffixes ("", "_m"). The manifest has more
    # rows than the frame, so the holdout's own True row is dropped and its
    # rows get other trials' False. Observed: build_ecosystem_index returns
    # the holdout's 2 rows. Correct: refused, because nct_id_m names other
    # trials.
    manifest = pd.DataFrame(
        {
            "nct_id": [_REDTEAM3_OTHER_ID, _REDTEAM3_OTHER_ID, "NCT99000103", "NCT99000103", _REDTEAM_HOLDOUT_ID],
            "in_audit": [False, False, False, False, True],
        }
    )
    joined = _holdout_frame().merge(
        manifest, left_index=True, right_index=True, how="left", suffixes=("", "_m")
    )
    assert "nct_id_m" in joined.columns and not joined["in_audit"].any()
    _redteam_assert_frame_refused(joined, shipped_rules, shipped_ecosystem_rules)


def test_redteam_m2_flag_in_row_index_values_or_an_unnamed_axis_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack: a bool column under a non-text label (0). The frame check
    # skipped bool columns. Observed: every frame and index entry point
    # attributes or counts the holdout. Correct: refused; a column label
    # that is not a str is now refused by type (src/holdout_guard.py). (The
    # earlier cases of this test, serialized manifests as the row index
    # values and set_index("in_audit") followed by rename_axis(None), hold
    # the flag as text or delete its name, and are no longer read.)
    flags = [n == _REDTEAM_HOLDOUT_ID for n, _, _, _ in _HOLDOUT_ROWS]
    labelled_zero = _holdout_frame()
    labelled_zero[0] = flags
    _redteam_assert_frame_refused(labelled_zero, shipped_rules, shipped_ecosystem_rules)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
    idx_flags = (idx["nct_id"] == _REDTEAM_HOLDOUT_ID).tolist()
    idx_labelled_zero = idx.copy()
    idx_labelled_zero[0] = idx_flags
    for held_idx in (idx_labelled_zero,):
        for name, entry in _INDEX_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError):
                entry(held_idx.copy())
            assert name


def test_redteam_m2_sparse_object_column_of_manifests_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW): the nested manifests in a Sparse[object] column. Its
    # dtype is neither object, a string dtype nor categorical, so
    # _frame_cell_scan skips it as a scalar column. Observed: attributed
    # and counted. Correct: refused.
    column = pd.arrays.SparseArray(
        [{"nct_id": n, "in_audit": n == _REDTEAM_HOLDOUT_ID} for n, _, _, _ in _HOLDOUT_ROWS],
        dtype=object,
    )
    _redteam_assert_frame_refused(
        _holdout_frame().assign(manifest=column), shipped_rules, shipped_ecosystem_rules
    )


def test_redteam_m2_write_ecosystem_baseline_refuses_a_trial_shaped_block(
    tmp_path: Path,
) -> None:
    # Attack (LOW): write_ecosystem_baseline is documented as "not
    # trial-shaped (rules, file paths or aggregate counts, no nct_id)" and
    # the module as never writing per-trial data, but it writes whatever
    # block it is handed, so a trial_cohorts() result or a manifest row
    # lands in the committed baseline file. Observed: the file is written
    # with the holdout's id and "in_audit": true. Correct: a block,
    # uncovered list or fixture_meta carrying in_audit or an NCT id is
    # refused and nothing is written.
    path = tmp_path / "expected_ecosystem_counts.json"
    with pytest.raises((eco.EcosystemAuditHoldoutError, ValueError, TypeError)):
        eco.write_ecosystem_baseline(
            "0" * 64,
            {"cohorts": {_REDTEAM_HOLDOUT_ID: ["nih"]}, "in_audit": True},
            [],
            {},
            path=str(path),
        )
    assert not path.exists()


def _redteam3_open_paths_during(calls: list[Callable[[], Any]]) -> list[str]:
    """Paths passed to open() while ``calls`` run (each run once beforehand).

    Method: a module-level audit hook (they cannot be removed) records the
    path argument of "open" events only while recording is on; every call
    runs once unrecorded first so lazy imports open their files then.
    """
    if not _Redteam3OpenRecorder.installed:
        sys.addaudithook(_redteam3_open_hook)
        _Redteam3OpenRecorder.installed = True
    for call in calls:
        call()
    _Redteam3OpenRecorder.paths = []
    try:
        for call in calls:
            call()
        return list(_Redteam3OpenRecorder.paths)
    finally:
        _Redteam3OpenRecorder.paths = None


class _Redteam3OpenRecorder:
    """State for _redteam3_open_hook."""

    installed: bool = False
    paths: list[str] | None = None


def _redteam3_open_hook(event: str, args: tuple[Any, ...]) -> None:
    """Record the path of an open() while recording is on."""
    paths = _Redteam3OpenRecorder.paths
    if paths is not None and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        paths.append(os.fsdecode(args[0]))


def test_redteam_m2_ecosystem_opens_only_the_documented_files() -> None:
    # Claim under test (refused/holds): "The only files this module reads or
    # writes are the two rules CSVs and the aggregate baseline JSON". The
    # loaders, sha256_file and attribute_study_sponsors without rules
    # (which loads both rules files) are run with their defaults. Expected
    # and observed: every path opened is one of the three.
    documented = {
        os.path.realpath(eco.DEFAULT_INDUSTRY_RULES_PATH),
        os.path.realpath(eco.DEFAULT_ECOSYSTEM_RULES_PATH),
        os.path.realpath(eco.EXPECTED_ECOSYSTEM_PATH),
    }
    calls: list[Callable[[], Any]] = [
        lambda: eco.load_ecosystem_rules(),
        lambda: eco.load_expected_ecosystem(),
        lambda: eco.sha256_file(eco.DEFAULT_ECOSYSTEM_RULES_PATH),
        lambda: eco.attribute_study_sponsors([_stored_record("NCT99000340")]),
    ]
    opened = {os.path.realpath(path) for path in _redteam3_open_paths_during(calls)}
    assert opened and opened <= documented


def test_redteam_m2_round3_attempts_that_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): a ChainMap (False over True) in a frame's cells (not
    # JSON: refused by type); a list in frame.attrs["in_audit"] (not a bool);
    # a clean dict with in_audit False beside the holdout as an iterrows()
    # row of a frame indexed by in_audit (a Series record: refused by type);
    # a bool under an enum key and a str enum key (keys that are not exactly
    # str); in_audit spelled with a combining grapheme joiner, a no-break
    # space or an ideographic space (dropped by the flag-key rule); a 0-d
    # object array holding the manifest (not JSON: refused by type, where it
    # was a TypeError before the consolidation). Expected and observed: each
    # raises, so nothing is attributed or counted.
    import enum

    import numpy as np

    class Key(enum.Enum):
        IN_AUDIT = "in_audit"

    class TextKey(str, enum.Enum):
        IN_AUDIT = "in_audit"

    cells = _holdout_frame().assign(
        manifest=[
            collections.ChainMap({"in_audit": False}, {"in_audit": n == _REDTEAM_HOLDOUT_ID, "nct_id": n})
            for n, _, _, _ in _HOLDOUT_ROWS
        ]
    )
    _redteam_assert_frame_refused(cells, shipped_rules, shipped_ecosystem_rules)
    listed = _holdout_frame([False] * 4)
    listed.attrs["in_audit"] = [True]
    _redteam_assert_frame_refused(listed, shipped_rules, shipped_ecosystem_rules)
    row = next(
        pd.DataFrame([_stored_record(_REDTEAM_HOLDOUT_ID, in_audit=True)]).set_index("in_audit").iterrows()
    )[1]
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run(
            [_stored_record(_REDTEAM3_OTHER_ID, in_audit=False), row], shipped_rules, shipped_ecosystem_rules
        )
    _assert_refused(exc_info.value, "in_audit_not_bool")
    plain_enum = _stored_record(_REDTEAM_HOLDOUT_ID)
    plain_enum[Key.IN_AUDIT] = True
    text_enum = _stored_record(_REDTEAM_HOLDOUT_ID)
    text_enum[TextKey.IN_AUDIT] = True
    spelled = [
        _stored_record(_REDTEAM_HOLDOUT_ID, **{f"in{char}_audit": True})
        for char in ("͏", " ", "　")
    ]
    for held in [plain_enum, text_enum, *spelled]:
        with pytest.raises(eco.EcosystemAuditHoldoutError):
            _redteam_run([held], shipped_rules, shipped_ecosystem_rules)
    zero_d = _stored_record(
        _REDTEAM_HOLDOUT_ID,
        manifest=np.array({"nct_id": _REDTEAM_HOLDOUT_ID, "in_audit": True}, dtype=object),
    )
    with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
        _redteam_run([zero_d], shipped_rules, shipped_ecosystem_rules)
    _assert_refused(exc_info.value, "in_audit_not_bool")


# ---------------------------------------------------------------------------
# 15. Red team round 4 (test_redteam_m2_*). Each test asserts the correct
# behaviour, which is refusal, for one attack on an entry point that
# enforces the holdout; a test that fails here names a bypass still open at
# fc7bc78 and is not a test to loosen. Tests for attempts that are already
# refused say so ("Attack (refused)"). Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network, no file is read
# except the shipped rules, and none is written except under pytest's
# tmp_path (the sqlite rows come from an in-memory database).
# ---------------------------------------------------------------------------


def _redteam4_row_flags() -> list[bool]:
    """in_audit for each row of _HOLDOUT_ROWS: True for the holdout's two rows."""
    return [nct_id == _REDTEAM_HOLDOUT_ID for nct_id, _, _, _ in _HOLDOUT_ROWS]


def _redteam4_not_refused(cases: list[tuple[str, Callable[[], Any]]]) -> list[str]:
    """Labels of the calls that do not raise EcosystemAuditHoldoutError.

    Method: each call runs on its own. One that returns is listed by its
    label; one that raises another exception is listed with that
    exception's type name (stopped, but not as a holdout, so a caller
    catching that type would go on). Messages are never formatted.
    """
    missed: list[str] = []
    for label, call in cases:
        try:
            call()
        except eco.EcosystemAuditHoldoutError:
            continue
        except Exception as exc:  # noqa: BLE001 - reported by type only
            missed.append(f"{label} ({type(exc).__name__})")
            continue
        missed.append(label)
    return missed


def _redteam4_frame_calls(
    label: str, held: pd.DataFrame, ind: pd.DataFrame, er: pd.DataFrame
) -> list[tuple[str, Callable[[], Any]]]:
    """Every frame entry point called on ``held`` itself (a copy would drop instance attributes)."""
    return [
        (f"{label} -> {name}", lambda entry=entry: entry(held, ind, er))
        for name, entry in _FRAME_ENTRY_POINTS
    ]


def _redteam4_index_calls(label: str, held: pd.DataFrame) -> list[tuple[str, Callable[[], Any]]]:
    """Every index entry point called on ``held`` itself."""
    return [(f"{label} -> {name}", lambda entry=entry: entry(held)) for name, entry in _INDEX_ENTRY_POINTS]


def _redteam4_record_calls(
    label: str, value: Any, ind: pd.DataFrame, er: pd.DataFrame, key: Any = "manifest"
) -> list[tuple[str, Callable[[], Any]]]:
    """attribute_study_sponsors on the holdout as a stored record and as a CT.gov dict, ``value`` under ``key``."""
    calls: list[tuple[str, Callable[[], Any]]] = []
    for make in (_stored_record, _api_record):
        record = make(_REDTEAM_HOLDOUT_ID)
        record[key] = value
        calls.append(
            (f"{label} -> {make.__name__}", lambda record=record: _redteam_run([record], ind, er))
        )
    return calls


def _redteam4_per_row(value_of: Callable[[str, bool], Any]) -> pd.Series:
    """An object column holding ``value_of(nct_id, in_audit)`` for each row of _HOLDOUT_ROWS."""
    return pd.Series(
        [value_of(nct_id, flag) for (nct_id, _, _, _), flag in zip(_HOLDOUT_ROWS, _redteam4_row_flags())],
        dtype=object,
    )


def _redteam4_sqlite_row(nct_id: str, flag: bool) -> Any:
    """A manifest row as a sqlite3.Row from an in-memory database (no file is opened).

    sqlite stores a bool as the integer 1 or 0, so the row holds
    (nct_id, 1): fields with names (Row.keys()) but no items(), and an int,
    not a bool, where the flag was.
    """
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE manifest (nct_id TEXT, in_audit BOOLEAN)")
        connection.execute("INSERT INTO manifest VALUES (?, ?)", (nct_id, flag))
        return connection.execute("SELECT nct_id, in_audit FROM manifest").fetchone()
    finally:
        connection.close()


def test_redteam_m2_round4_sqlite_rows_carrying_in_audit_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW-MEDIUM): manifest rows read from SQLite with
    # row_factory=sqlite3.Row, kept as a record's manifest or as a frame
    # column. sqlite3.Row has keys() and [] but no items(), so the scan
    # walks it by position: a str and the int 1 (sqlite's True), both
    # leaves. Observed: attributed and counted. Correct: refused
    # (in_audit is 1, not a bool).
    column = _redteam4_per_row(_redteam4_sqlite_row)
    cases: list[tuple[str, Callable[[], Any]]] = []
    cases.extend(
        _redteam4_record_calls(
            "nested sqlite3.Row", _redteam4_sqlite_row(_REDTEAM_HOLDOUT_ID, True), shipped_rules, shipped_ecosystem_rules
        )
    )
    cases.extend(
        _redteam4_frame_calls(
            "column of sqlite3.Row", _holdout_frame().assign(manifest=column), shipped_rules, shipped_ecosystem_rules
        )
    )
    assert _redteam4_not_refused(cases) == []


def test_redteam_m2_round4_int_enum_key_or_label_naming_in_audit_is_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW; round 3's "enum key holding the CSV string 'True'",
    # still open for int-valued enums): an IntEnum or IntFlag member named
    # IN_AUDIT as a record key holding "True" or 1, or as a frame column
    # label holding "True"/"False" strings or 1/0. _name_texts returns no
    # text for any int before it reaches str(), so the name is not read.
    # Observed: attributed and counted. Correct: refused (the values are
    # not bools), as a plain Enum key is.
    import enum

    class IntKey(enum.IntEnum):
        IN_AUDIT = 1

    class FlagKey(enum.IntFlag):
        IN_AUDIT = 1

    flags = _redteam4_row_flags()
    cases: list[tuple[str, Callable[[], Any]]] = []
    for key in (IntKey.IN_AUDIT, FlagKey.IN_AUDIT):
        for value in ("True", 1):
            cases.extend(
                _redteam4_record_calls(
                    f"{type(key).__name__} key holding {value!r}", value, shipped_rules, shipped_ecosystem_rules, key=key
                )
            )
        for values in ([str(flag) for flag in flags], [int(flag) for flag in flags]):
            held = _holdout_frame()
            held[key] = values
            cases.extend(
                _redteam4_frame_calls(
                    f"{type(key).__name__} column of {type(values[0]).__name__}",
                    held,
                    shipped_rules,
                    shipped_ecosystem_rules,
                )
            )
    assert _redteam4_not_refused(cases) == []


def test_redteam_m2_round4_tagged_number_cells_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW): an object column of int (or float) subclass values,
    # each carrying its row's in_audit as an attribute. pandas infers the
    # column as "integer" ("floating"), which _SCALAR_INFERRED_DTYPES skips
    # without reading a cell; the record scan refuses the same value
    # (_beside_items), and round 3 made the frame path read a str
    # subclass's attributes. Observed: attributed and counted. Correct:
    # refused.
    class TaggedInt(int):
        pass

    class TaggedFloat(float):
        pass

    def tagged(kind: type, flag: bool) -> Any:
        value = kind(1)
        value.in_audit = flag
        return value

    cases: list[tuple[str, Callable[[], Any]]] = []
    for kind in (TaggedInt, TaggedFloat):
        column = _redteam4_per_row(lambda _nct_id, flag, kind=kind: tagged(kind, flag))
        assert pd.api.types.infer_dtype(column) in ("integer", "floating")
        cases.extend(
            _redteam4_frame_calls(
                f"object column of {kind.__name__}",
                _holdout_frame().assign(rank=column),
                shipped_rules,
                shipped_ecosystem_rules,
            )
        )
    assert _redteam4_not_refused(cases) == []


def test_redteam_m2_round4_output_is_stamped_with_the_id_that_was_checked(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW; needs a str subclass): rows and a record of the clean
    # trial, in_audit False, whose nct_id is a str subclass with content
    # NCT99000102 and str() NCT99000101 (the holdout). The check reads the
    # raw value (canonical, every id agrees); the index keeps the object,
    # and trial_cohorts and any str() of the index stamp the holdout's id.
    # Observed: trial_cohorts returns NCT99000101. Correct: refused, or the
    # output stamped with exactly the plain str that was checked.
    class Shown(str):
        def __str__(self) -> str:
            return _REDTEAM_HOLDOUT_ID

    rows = [
        (Shown(_REDTEAM3_OTHER_ID), agency, role, name)
        for nct_id, agency, role, name in _HOLDOUT_ROWS
        if nct_id == _REDTEAM3_OTHER_ID
    ]
    framed = pd.DataFrame(rows, columns=["nct_id", "agency_class", "lead_or_collaborator", "name"], dtype=object)
    framed["in_audit"] = False
    record = _stored_record(Shown(_REDTEAM3_OTHER_ID), in_audit=False)
    built: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        ("frame", lambda: eco.build_unified_index(framed, shipped_rules, shipped_ecosystem_rules)),
        ("record", lambda: _redteam_run([record], shipped_rules, shipped_ecosystem_rules)[0]),
    ]
    stamped: dict[str, list[str]] = {}
    for label, build in built:
        try:
            index = build()
        except eco.EcosystemAuditHoldoutError:
            continue
        stamped[label] = sorted(
            "holdout id" if str(key) == _REDTEAM_HOLDOUT_ID else type(key).__name__
            for key in [*eco.trial_cohorts(index), *index["nct_id"]]
            if not (type(key) is str and key == _REDTEAM3_OTHER_ID)
        )
    assert all(not wrong for wrong in stamped.values()), stamped


def test_redteam_m2_round4_audit_id_set_passed_positionally_is_not_echoed(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW): the audit id set (a tuple or frozenset, so hashable)
    # passed where a sector or a role is expected. sector_trials and
    # trial_cohorts raise EcosystemRuleValidationError with ``got
    # {sector!r}`` / ``got {role!r}``, so the message, and any log or
    # traceback that keeps it, records the holdout ids; the module's own
    # refusals and the parser's argument checks name types only. Observed:
    # the messages contain NCT99000101. Correct: raise, naming no id.
    audit_ids = (_REDTEAM_HOLDOUT_ID, "nct99000101")
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
    sp = _holdout_frame()
    calls: list[tuple[str, Callable[[], Any]]] = [
        ("sector_trials(sector=ids)", lambda: eco.sector_trials(idx, audit_ids)),  # type: ignore[arg-type]
        ("sector_trials(role=ids)", lambda: eco.sector_trials(idx, "nih", audit_ids)),  # type: ignore[arg-type]
        ("trial_cohorts(role=ids)", lambda: eco.trial_cohorts(idx, audit_ids)),  # type: ignore[arg-type]
        ("trial_cohorts(role=frozenset(ids))", lambda: eco.trial_cohorts(idx, frozenset(audit_ids))),  # type: ignore[arg-type]
        ("organization_trials(role=ids)", lambda: eco.organization_trials(idx, "National Cancer Institute (NCI)", audit_ids)),  # type: ignore[arg-type]
        ("audit_ecosystem(canonical=ids)", lambda: eco.audit_ecosystem(sp, shipped_ecosystem_rules, audit_ids, "cancer")),  # type: ignore[arg-type]
    ]
    echoed: list[str] = []
    for label, call in calls:
        with pytest.raises(Exception) as exc_info:
            call()
        if "nct99" in str(exc_info.value).lower():
            echoed.append(label)
    assert echoed == []


# NCT ids written with a zero-width space inside "NCT" or separators between
# the digits, which _NCT_ID_ANYWHERE_RE ("NCT", any non-word separators,
# eight digits, IGNORECASE) reads. Full-width or Cyrillic letters in "NCT"
# are look-alike disguises the consolidated guard no longer maps (see
# src/holdout_guard.py), so they are not listed.
_REDTEAM4_DISGUISED_IDS: list[tuple[str, str]] = [
    ("zero-width space inside NCT", "N\u200bCT99000101"),
    ("hyphen between the digits", "NCT9900-0101"),
]


def test_redteam_m2_round4_write_ecosystem_baseline_refuses_a_disguised_nct_id(
    tmp_path: Path,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (LOW): a baseline block of the right shape whose organization
    # name, or fixture_meta "file", is a holdout id written with full-width
    # letters, a zero-width space or a Cyrillic letter in "NCT", or a
    # hyphen between the digits. The parser and the frame check refuse such
    # ids as possible holdouts in disguise; this check reads "NCT" then
    # eight digits only. Observed: the file is written with the id.
    # Correct: ValueError and nothing written.
    block = eco.compute_ecosystem_baseline(_holdout_frame(), shipped_ecosystem_rules)
    written: list[str] = []
    attempts = [
        (label, disguised, part)
        for label, disguised in _REDTEAM4_DISGUISED_IDS
        for part in ("organizations", "fixture_meta")
    ]
    for number, (label, disguised, part) in enumerate(attempts):
        path = tmp_path / f"expected_ecosystem_counts-{number}.json"
        trial_block = json.loads(json.dumps(block))
        meta = {"file": "sponsors_fixture_ecosystem.csv"}
        if part == "organizations":
            trial_block["organizations"][disguised] = {"any": 1, "lead": 1, "collaborator": 0}
        else:
            meta["file"] = f"records/{disguised}.json"
        try:
            eco.write_ecosystem_baseline("0" * 64, trial_block, [], meta, path=str(path))
        except ValueError:
            pass
        if path.exists():
            written.append(f"{label} in {part}")
    assert written == []


def test_redteam_m2_round4_attempts_that_are_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Attack (refused): a Series row given in_audit by attribute assignment,
    # an int subclass with an in_audit attribute in a record and an email
    # header "In-Audit" as a record's manifest (none of them JSON: refused by
    # type); a frame whose attrs hold the manifest DataFrame (not JSON) and
    # one with an IntEnum column label (not a str). Expected and observed:
    # every call raises EcosystemAuditHoldoutError. (A query string, cells
    # of to_json text and a get_dummies frame, "in_audit_True", hold the
    # flag as text or as a value and are no longer read.)
    import email.message
    import enum
    import warnings

    class TaggedInt(int):
        pass

    tagged = TaggedInt(1)
    tagged.in_audit = True  # type: ignore[attr-defined]
    header = email.message.Message()
    header["In-Audit"] = "true"
    row = pd.Series(_stored_record(_REDTEAM_HOLDOUT_ID))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        row.in_audit = True  # the attribute, not a label: pandas warns and sets it

    class IntKey(enum.IntEnum):
        IN_AUDIT = 1

    flags = _redteam4_row_flags()
    manifest = pd.DataFrame({"nct_id": [n for n, _, _, _ in _HOLDOUT_ROWS], "in_audit": flags})
    in_attrs = _holdout_frame()
    in_attrs.attrs["manifest"] = manifest
    int_label = _holdout_frame()
    int_label[IntKey.IN_AUDIT] = flags
    cases: list[tuple[str, Callable[[], Any]]] = [
        ("Series row with an in_audit attribute", lambda: _redteam_run([row], shipped_rules, shipped_ecosystem_rules)),
    ]
    cases.extend(_redteam4_record_calls("int subclass with an in_audit attribute", tagged, shipped_rules, shipped_ecosystem_rules, key="rank"))
    cases.extend(_redteam4_record_calls("email header In-Audit", header, shipped_rules, shipped_ecosystem_rules))
    for label, held in (
        ("manifest DataFrame in attrs", in_attrs),
        ("IntEnum column label holding bools", int_label),
    ):
        cases.extend(_redteam4_frame_calls(label, held, shipped_rules, shipped_ecosystem_rules))
    assert _redteam4_not_refused(cases) == []
    # A sqlite3.Row manifest in frame.attrs: attrs are read first, and a
    # sqlite3.Row is not JSON, so it is refused by type (pandas would raise
    # TypeError deep-copying it when a column is read).
    sqlite_attrs = _holdout_frame()
    sqlite_attrs.attrs["manifest"] = _redteam4_sqlite_row(_REDTEAM_HOLDOUT_ID, True)
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises((eco.EcosystemAuditHoldoutError, TypeError)):
            entry(sqlite_attrs, shipped_rules, shipped_ecosystem_rules)
        assert name


@pytest.mark.parametrize(
    "bad",
    [123, None, ("NCT99000101", "NCT99000102"), b"National Cancer Institute (NCI)"],
    ids=["int", "none", "tuple-of-ids", "bytes"],
)
def test_a_canonical_or_stem_that_is_not_a_str_is_refused_naming_its_type_only(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
    bad: Any,
) -> None:
    # Before: audit_ecosystem compared `canonical` with == against the
    # matched names, so an int or a tuple matched nothing and the call
    # returned empty frames; organization_trials passed it on to
    # sr.trials, which did the same. Now both raise TypeError, and the
    # message names the type, never the value.
    sp = _holdout_frame()
    idx = eco.build_unified_index(sp, shipped_rules, shipped_ecosystem_rules)
    calls: list[Callable[[], Any]] = [
        lambda: eco.audit_ecosystem(sp, shipped_ecosystem_rules, bad, "cancer"),
        lambda: eco.audit_ecosystem(
            sp, shipped_ecosystem_rules, "National Cancer Institute (NCI)", bad
        ),
        lambda: eco.organization_trials(idx, bad),
    ]
    for call in calls:
        with pytest.raises(TypeError) as exc_info:
            call()
        assert "nct99" not in str(exc_info.value).lower()
        assert type(bad).__name__ in str(exc_info.value)


# ---------------------------------------------------------------------------
# 16. The consolidated holdout guard (src/holdout_guard.py): names that are
# not the flag, agreeing merge-suffixed flags, and refusal by type at every
# entry point. Every id is fictional (NCT99000001..NCT99999999); nothing
# touches the network, and no file is read except the shipped rules.
# ---------------------------------------------------------------------------

_AUDIT_WORDS: tuple[str, ...] = ("domain_audit", "main_audit", "login_audit")


def test_audit_words_that_are_not_the_flag_change_nothing(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Before the consolidation any name whose folded form contained
    # "inaudit" was the flag, so a sponsor frame with a domain_audit,
    # main_audit or login_audit column (or attrs key), and a record with
    # such a key, was refused although clean. They are ordinary fields now.
    plain_frame = _holdout_frame()
    for word in _AUDIT_WORDS:
        for values in ([True] * 4, ["yes", "no", "yes", None]):
            held = _holdout_frame().assign(**{word: pd.Series(values, dtype=object)})
            for name, entry in _FRAME_ENTRY_POINTS:
                _assert_same(
                    entry(held, shipped_rules, shipped_ecosystem_rules),
                    entry(plain_frame, shipped_rules, shipped_ecosystem_rules),
                )
        in_attrs = _holdout_frame()
        in_attrs.attrs[word] = True
        assert eco.partition_ecosystem(in_attrs, shipped_ecosystem_rules) == eco.partition_ecosystem(
            plain_frame, shipped_ecosystem_rules
        )
        idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
        for name, entry in _INDEX_ENTRY_POINTS:
            _assert_same(entry(idx.assign(**{word: True})), entry(idx))
        for make in (_stored_record, _api_record):
            got = _redteam_run([make("NCT99000501", **{word: True})], shipped_rules, shipped_ecosystem_rules)
            want = _redteam_run([make("NCT99000501")], shipped_rules, shipped_ecosystem_rules)
            pd.testing.assert_frame_equal(got[0], want[0])
            assert got[1] == want[1]


def test_agreeing_merge_suffixed_flag_columns_are_accepted_and_disagreeing_refused(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # A sponsor frame that already carries in_audit merged with a manifest
    # that carries it too has in_audit_x and in_audit_y. Before the
    # consolidation two row-level sources were refused even when both said
    # False ("more than one row-level source"). Now sources that agree (no
    # True anywhere) pass and change nothing; a True in either refuses.
    manifest = pd.DataFrame(
        {"nct_id": ["NCT99000101", "NCT99000102"], "in_audit": [False, False]}
    )
    merged = _holdout_frame([False] * 4).merge(manifest, on="nct_id", how="left")
    assert {"in_audit_x", "in_audit_y"} <= set(merged.columns)
    for name, entry in _FRAME_ENTRY_POINTS:
        _assert_same(
            entry(merged.copy(), shipped_rules, shipped_ecosystem_rules),
            entry(_holdout_frame(), shipped_rules, shipped_ecosystem_rules),
        )
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, [False])
    idx_merged = idx.merge(manifest, on="nct_id", how="left")
    assert {"in_audit_x", "in_audit_y"} <= set(idx_merged.columns)
    for name, entry in _INDEX_ENTRY_POINTS:
        _assert_same(entry(idx_merged.copy()), entry(idx.drop(columns=["in_audit"])))
    for x_flags, y_flags in (
        ([False] * 4, [False, False, True, True]),
        ([True, True, False, False], [False] * 4),
    ):
        held = _holdout_frame().assign(in_audit_x=x_flags, in_audit_y=y_flags)
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_true")
            assert "2 row(s)" in str(exc_info.value)
    stringly = _holdout_frame().assign(in_audit_x=[False] * 4, in_audit_y=["False"] * 4)
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(stringly, shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
    agreeing_records = [
        _stored_record("NCT99000502", in_audit_x=False, in_audit_y=False),
        _api_record("NCT99000503", in_audit_x=False, in_audit_y=False),
    ]
    got = _redteam_run(agreeing_records, shipped_rules, shipped_ecosystem_rules)
    want = _redteam_run(
        [_stored_record("NCT99000502"), _api_record("NCT99000503")], shipped_rules, shipped_ecosystem_rules
    )
    pd.testing.assert_frame_equal(got[0], want[0])


def test_non_json_input_is_refused_by_type_at_every_entry_point(
    shipped_rules: pd.DataFrame,
    shipped_ecosystem_rules: pd.DataFrame,
) -> None:
    # Frames: exactly a DataFrame (TypeError otherwise, a subclass too), str
    # column labels, and object cells that are JSON data, or the frame is
    # refused by type ("in_audit_not_bool"), flag or not. Records: a dict of
    # JSON data. Nothing inside a refused value is read.
    import decimal
    import enum

    np = pytest.importorskip("numpy")

    class Text(str):
        pass

    class Key(enum.Enum):
        NOTE = "note"

    class SubFrame(pd.DataFrame):
        pass

    cells: list[tuple[str, Any]] = [
        ("Decimal", decimal.Decimal("1")),
        ("tuple", ("a",)),
        ("SimpleNamespace", types.SimpleNamespace(note="a")),
        ("str subclass", Text("a")),
        ("numpy int", np.int64(1)),
        ("dict holding a set", {"note": {"a"}}),
    ]
    frames: list[tuple[str, pd.DataFrame]] = [
        (label, _holdout_frame().assign(extra=pd.Series([value] * 4, dtype=object)))
        for label, value in cells
    ]
    int_label = _holdout_frame()
    int_label[7] = ["a"] * 4
    frames.append(("int column label", int_label))
    enum_label = _holdout_frame()
    enum_label[Key.NOTE] = ["a"] * 4
    frames.append(("Enum column label", enum_label))
    frames.append(("MultiIndex columns", pd.concat({"sponsor": _holdout_frame()}, axis=1)))
    for label, held in frames:
        for name, entry in _FRAME_ENTRY_POINTS:
            with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
                entry(held, shipped_rules, shipped_ecosystem_rules)
            _assert_refused(exc_info.value, "in_audit_not_bool")
            assert "refused by type" in str(exc_info.value), (label, name)
    idx = _index_with_in_audit(shipped_rules, shipped_ecosystem_rules, None)
    for name, entry in _INDEX_ENTRY_POINTS:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            entry(idx.assign(extra=pd.Series([("a",)] * len(idx), dtype=object)))
        _assert_refused(exc_info.value, "in_audit_not_bool")
    uniq = sr.literal_universe(_holdout_frame()).assign(extra=pd.Series([("a",)] * 4, dtype=object))
    for call in (
        lambda: eco.match_ecosystem_literals(uniq, shipped_ecosystem_rules),
        lambda: eco.check_cross_layer_conflicts(uniq, shipped_rules, shipped_ecosystem_rules),
    ):
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            call()
        _assert_refused(exc_info.value, "in_audit_not_bool")
    for name, entry in _FRAME_ENTRY_POINTS:
        with pytest.raises(TypeError, match="expects a pandas DataFrame"):
            entry(SubFrame(_holdout_frame()), shipped_rules, shipped_ecosystem_rules)
    values: list[tuple[str, Any]] = [
        ("tuple", ("a",)),
        ("set", {"a"}),
        ("bytes", b"a"),
        ("UserDict", collections.UserDict(note="a")),
        ("numpy int", np.int64(1)),
        ("str subclass", Text("a")),
        ("bool in a list", ["a", False]),
    ]
    records: list[tuple[str, Any]] = []
    for label, value in values:
        for make in (_stored_record, _api_record):
            records.append((label, make("NCT99000504", extra=value)))
    keyed = _stored_record("NCT99000504")
    keyed[Key.NOTE] = "a"
    records.append(("Enum key", keyed))
    records.append(("UserDict record", collections.UserDict(_stored_record("NCT99000504"))))
    for label, record in records:
        with pytest.raises(eco.EcosystemAuditHoldoutError) as exc_info:
            _redteam_run([record], shipped_rules, shipped_ecosystem_rules)
        _assert_refused(exc_info.value, "in_audit_not_bool")
        assert "refused by type" in str(exc_info.value), label
    # Plain data passes: a NaN, None, numbers and nested JSON in object cells.
    plain = _holdout_frame()
    tolerated = plain.assign(
        extra=pd.Series([None, float("nan"), 3, {"note": ["a", 1, None]}], dtype=object)
    )
    for name, entry in _FRAME_ENTRY_POINTS:
        _assert_same(
            entry(tolerated, shipped_rules, shipped_ecosystem_rules),
            entry(plain, shipped_rules, shipped_ecosystem_rules),
        )
