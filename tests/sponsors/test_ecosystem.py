"""Unit, composition, and SHA-256 regression tests for sponsors/ecosystem.py."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from conftest import FIXTURE_PATH, RULES_PATH, frame
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

    for section in ("index", "sectors", "organizations", "entities"):
        assert actual[section] == block[section], "\n".join(
            eco.diff_ecosystem_blocks(block, actual)
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
