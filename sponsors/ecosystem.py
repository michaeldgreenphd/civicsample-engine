"""Non-industry sponsor ecosystem attribution & unified cohort composition.

READS   Curated ecosystem rules (`sponsors/ecosystem_rules.csv`), AACT-shaped
        sponsors DataFrames (nct_id | agency_class | lead_or_collaborator | name),
        or study records via `sponsors.adapter.records_to_frame`.
WRITES  Nothing during attribution; `write_ecosystem_baseline()` writes the
        SHA-256-keyed regression file (`tests/sponsors/expected_ecosystem_counts.json`)
        only during deliberate re-baselining.

Design invariants (shared with `sponsors/sponsor_roles.py` and `AGENTS.md`):
  1. List-driven exact matching ONLY in production attribution paths
     (`exact_literal`, `exact_normalized`, `subsidiary_of` via `sr._rule_hits`).
     Zero substring or regex matching in production attribution; plain
     case-insensitive substring `stem` is restricted strictly to
     `audit_ecosystem()` to surface unreviewed candidates for human curation.
  2. Three disjoint review states (`attributed`, `reviewed_excluded`,
     `unreviewed`) partition `literal_universe(frame)` and are NEVER collapsed.
  3. Every rule carries a non-empty `note` recording rationale; conflicting
     rules (attribute vs attribute without `shared=yes`, attribute vs exclude,
     or one `canonical` mapped to multiple sectors) raise
     `EcosystemRuleValidationError` (a `ValueError` subclass) at build time.
  4. Composes cleanly with `sponsors/company_aliases.csv` via
     `build_unified_index()` and `attribute_study_sponsors()`:
     - Industry rows receive `sector = "industry"`.
     - Cross-layer `attribute` vs `attribute` overlap raises unless EVERY
       claiming rule across both files has `shared == "yes"`.
     - `status == "exclude"` in `company_aliases.csv` ("reviewed: not industry")
       cleanly composes with `status == "attribute"` in `ecosystem_rules.csv`
       (e.g., Wake Forest University Health Sciences).
  5. `sponsors/company_aliases.csv` and `tests/sponsors/expected_counts.json`
     are never modified by this module.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from . import adapter
from . import baseline as bl
from . import sponsor_roles as sr

MODULE_VERSION: str = "v1.0"
RULES_FORMAT_VERSION: str = "ecosystem_rules@v1.0"

VALID_ECOSYSTEM_SECTORS: frozenset[str] = frozenset(
    {
        "nih",
        "us_federal_other",
        "nctn_cooperative_group",
        "academic_medical_center",
        "non_profit_foundation",
    }
)

UNIFIED_SECTORS: tuple[str, ...] = (
    "industry",
    "nih",
    "us_federal_other",
    "nctn_cooperative_group",
    "academic_medical_center",
    "non_profit_foundation",
)

ECOSYSTEM_RULE_COLUMNS: list[str] = [
    "canonical",
    "sector",
    "rule_type",
    "pattern",
    "status",
    "shared",
    "note",
]

ECOSYSTEM_INDEX_COLUMNS: list[str] = [
    "nct_id",
    "canonical",
    "sector",
    "entity",
    "role",
    "literal_name",
    "agency_class",
    "match_rule",
    "shared",
]

_SPONSORS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SPONSORS_DIR)
DEFAULT_INDUSTRY_RULES_PATH: str = os.path.join(_SPONSORS_DIR, "company_aliases.csv")
DEFAULT_ECOSYSTEM_RULES_PATH: str = os.path.join(_SPONSORS_DIR, "ecosystem_rules.csv")
EXPECTED_ECOSYSTEM_PATH: str = os.path.join(
    _REPO_ROOT, "tests", "sponsors", "expected_ecosystem_counts.json"
)


class EcosystemRuleValidationError(ValueError):
    """Raised on malformed, conflicting, or inconsistent ecosystem rules."""


# ---------------------------------------------------------------------------
# 1. Loading & validating curated ecosystem rules
# ---------------------------------------------------------------------------


def validate_ecosystem_rules(rules: pd.DataFrame) -> pd.DataFrame:
    """Validate an ecosystem rules DataFrame and return a normalized copy.

    Enforces:
      - required columns (canonical, sector, rule_type, pattern, status, shared, note)
      - non-empty canonical and pattern
      - sector in VALID_ECOSYSTEM_SECTORS
      - rule_type in sr.VALID_RULE_TYPES (no substring/regex rules)
      - status in sr.VALID_STATUS
      - shared in sr.VALID_SHARED
      - non-empty note on every rule
      - single-sector consistency per canonical across all status == 'attribute' rows
    """
    df = rules.copy()
    for col, default in [("status", "attribute"), ("shared", "no"), ("note", "")]:
        if col not in df.columns:
            df[col] = default

    required = ["canonical", "sector", "rule_type", "pattern"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise EcosystemRuleValidationError(
            f"ecosystem rules missing columns: {missing}"
        )

    df["status"] = df["status"].fillna("attribute").astype(str)
    df["shared"] = df["shared"].fillna("no").astype(str)

    empty_canon = df["canonical"].isna() | (df["canonical"].astype(str).str.strip() == "")
    if empty_canon.any():
        raise EcosystemRuleValidationError(
            f"{int(empty_canon.sum())} ecosystem rule(s) have an empty canonical"
        )

    empty_pat = df["pattern"].isna() | (df["pattern"].astype(str).str.strip() == "")
    if empty_pat.any():
        raise EcosystemRuleValidationError(
            f"{int(empty_pat.sum())} ecosystem rule(s) have an empty pattern"
        )

    bad_sectors = set(df["sector"].dropna().astype(str)) - VALID_ECOSYSTEM_SECTORS
    if bad_sectors or df["sector"].isna().any():
        raise EcosystemRuleValidationError(
            f"unknown ecosystem sector values: {sorted(bad_sectors)} "
            f"(must be in {sorted(VALID_ECOSYSTEM_SECTORS)})"
        )

    bad_types = set(df["rule_type"].dropna().astype(str)) - sr.VALID_RULE_TYPES
    if bad_types or df["rule_type"].isna().any():
        raise EcosystemRuleValidationError(
            f"unknown rule_type values: {sorted(bad_types)} "
            f"(must be in {sorted(sr.VALID_RULE_TYPES)})"
        )

    bad_status = set(df["status"]) - sr.VALID_STATUS
    if bad_status:
        raise EcosystemRuleValidationError(
            f"unknown status values: {sorted(bad_status)}"
        )

    bad_shared = set(df["shared"]) - sr.VALID_SHARED
    if bad_shared:
        raise EcosystemRuleValidationError(
            f"unknown shared values: {sorted(bad_shared)} (must be yes or no)"
        )

    no_note = df["note"].isna() | (df["note"].astype(str).str.strip() == "")
    if no_note.any():
        offender = df.loc[no_note, ["canonical", "pattern"]].iloc[0].to_dict()
        raise EcosystemRuleValidationError(
            f"{int(no_note.sum())} ecosystem rule(s) have an empty note. "
            f"Every rule must record rationale (first offender: {offender})"
        )

    # Single-sector consistency per canonical across all attribute rules
    attr = df[df["status"] == "attribute"]
    if not attr.empty:
        sectors_per_canon = attr.groupby("canonical")["sector"].nunique()
        multi = sectors_per_canon[sectors_per_canon > 1]
        if not multi.empty:
            offending = {
                str(c): sorted(set(attr.loc[attr["canonical"] == c, "sector"]))
                for c in sorted(multi.index)
            }
            raise EcosystemRuleValidationError(
                f"canonical mapped to multiple sectors: {offending}"
            )

    return df


def load_ecosystem_rules(path: str = DEFAULT_ECOSYSTEM_RULES_PATH) -> pd.DataFrame:
    """Read and validate curated ecosystem rules CSV (7-column schema)."""
    rules = pd.read_csv(path, dtype=str)
    return validate_ecosystem_rules(rules)


# ---------------------------------------------------------------------------
# 2. Exact literal matching & ecosystem index construction
# ---------------------------------------------------------------------------


def match_ecosystem_literals(
    uniq: pd.DataFrame,
    rules: pd.DataFrame,
) -> pd.DataFrame:
    """Apply curated ecosystem rules to a literal universe using exact matching ONLY.

    Precedence per literal: exact_literal > exact_normalized > subsidiary_of.
    Raises EcosystemRuleValidationError when:
      * two attribute rules claim one literal for different canonicals and not
        EVERY claiming rule has shared == 'yes', or
      * an attribute rule and an exclude rule both match the same literal.
    """
    validated = validate_ecosystem_rules(rules)
    active = validated[validated["status"] == "attribute"]

    # literal -> list of (canonical, sector, rule_type, shared)
    lit_map: dict[str, list[tuple[str, str, str, str]]] = {}

    def _assign(
        literal: str,
        canonical: str,
        sector: str,
        rule: str,
        shared: str,
    ) -> None:
        claims = lit_map.setdefault(literal, [])
        if any(c == canonical for c, _, _, _ in claims):
            return  # same canonical via a second rule: keep higher-precedence rule
        if claims and not (shared == "yes" and all(s == "yes" for _, _, _, s in claims)):
            others = sorted({c for c, _, _, _ in claims})
            raise EcosystemRuleValidationError(
                f"conflicting ecosystem rules for literal {literal!r}: {others} vs "
                f"{canonical!r} (via {rule}). If this literal genuinely names "
                "several organizations, mark EVERY claiming rule shared=yes."
            )
        claims.append((canonical, sector, rule, shared))

    for rt in ("exact_literal", "exact_normalized", "subsidiary_of"):
        for _, r in active[active["rule_type"] == rt].iterrows():
            for lit in sr._rule_hits(uniq, r):
                _assign(str(lit), str(r.canonical), str(r.sector), rt, str(r.shared))

    for _, r in validated[validated["status"] == "exclude"].iterrows():
        for lit in sr._rule_hits(uniq, r):
            lit_s = str(lit)
            if lit_s in lit_map:
                claimed = sorted({c for c, _, _, _ in lit_map[lit_s]})
                raise EcosystemRuleValidationError(
                    f"conflicting ecosystem rules for literal {lit_s!r}: attributed to "
                    f"{claimed} but also excluded by rule ({r.canonical!r}, "
                    f"{r.rule_type}, {r.pattern!r}). A literal is attributed or "
                    "reviewed-excluded, never both."
                )

    return pd.DataFrame(
        [
            (lit, canon, sector, rule, shared)
            for lit, claims in lit_map.items()
            for canon, sector, rule, shared in claims
        ],
        columns=["name", "canonical", "sector", "match_rule", "shared"],
    )


def build_ecosystem_index(
    sponsors: pd.DataFrame,
    rules: pd.DataFrame,
) -> pd.DataFrame:
    """Attribute sponsor rows to canonical non-industry ecosystem organizations."""
    sp = sponsors.copy()
    bad_roles = set(sp["lead_or_collaborator"].dropna().unique()) - sr.FRAME_ROLES
    if bad_roles:
        raise EcosystemRuleValidationError(
            f"lead_or_collaborator must be one of {sorted(sr.FRAME_ROLES)}; "
            f"found {sorted(bad_roles)}"
        )

    matched = match_ecosystem_literals(sr.literal_universe(sp), rules)
    if matched.empty:
        return pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)

    matched["entity"] = matched["name"].map(sr.parsed_entity)
    out = sp.merge(matched, on="name", how="inner")
    out = out.rename(columns={"lead_or_collaborator": "role", "name": "literal_name"})
    out = (
        out[ECOSYSTEM_INDEX_COLUMNS]
        .drop_duplicates(subset=["nct_id", "canonical", "entity", "role"])
        .reset_index(drop=True)
    )
    return out


def partition_ecosystem(
    frame: pd.DataFrame,
    rules: pd.DataFrame,
) -> dict[str, set[str]]:
    """Return disjoint {'attributed', 'reviewed_excluded', 'unreviewed'} literal sets."""
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(frame)
    universe = set(uniq["name"].astype(str))
    matched = match_ecosystem_literals(uniq, validated)
    attributed = set(matched["name"].astype(str)) & universe

    excluded: set[str] = set()
    for _, r in validated[validated["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    excluded &= universe

    both = attributed & excluded
    if both:
        raise EcosystemRuleValidationError(
            f"{len(both)} literal(s) are both attributed and reviewed-excluded "
            f"(first: {sorted(both)[0]!r})"
        )
    unreviewed = universe - attributed - excluded
    return {
        "attributed": attributed,
        "reviewed_excluded": excluded,
        "unreviewed": unreviewed,
    }


# ---------------------------------------------------------------------------
# 3. Cross-layer composition with company_aliases.csv (industry + ecosystem)
# ---------------------------------------------------------------------------


def check_cross_layer_conflicts(
    uniq: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> None:
    """Ensure no literal is attributed across both layers unless every claim is shared=yes.

    Note: status == 'exclude' in company_aliases.csv ("not industry") cleanly
    composes with status == 'attribute' in ecosystem_rules.csv (e.g., Wake Forest
    University Health Sciences).
    """
    ind_matched = sr.match_literals(uniq, industry_rules)
    eco_matched = match_ecosystem_literals(uniq, ecosystem_rules)

    if ind_matched.empty or eco_matched.empty:
        return

    overlap = set(ind_matched["name"].astype(str)) & set(eco_matched["name"].astype(str))
    for lit in sorted(overlap):
        ind_rows = ind_matched[ind_matched["name"] == lit]
        eco_rows = eco_matched[eco_matched["name"] == lit]
        all_shared = (ind_rows["shared"] == "yes").all() and (
            eco_rows["shared"] == "yes"
        ).all()
        if not all_shared:
            ind_canons = sorted(set(ind_rows["canonical"].astype(str)))
            eco_canons = sorted(set(eco_rows["canonical"].astype(str)))
            raise EcosystemRuleValidationError(
                f"cross-layer conflict for literal {lit!r}: attributed in industry "
                f"to {ind_canons} and in ecosystem to {eco_canons} without every "
                "claiming rule marked shared=yes."
            )


def build_unified_index(
    sponsors: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> pd.DataFrame:
    """Build unified industry + ecosystem sponsor index with sector column."""
    sp = sponsors.copy()
    bad_roles = set(sp["lead_or_collaborator"].dropna().unique()) - sr.FRAME_ROLES
    if bad_roles:
        raise EcosystemRuleValidationError(
            f"lead_or_collaborator must be one of {sorted(sr.FRAME_ROLES)}; "
            f"found {sorted(bad_roles)}"
        )

    uniq = sr.literal_universe(sp)
    check_cross_layer_conflicts(uniq, industry_rules, ecosystem_rules)

    ind_idx = sr.build_index(sp, industry_rules)
    if ind_idx.empty:
        ind_idx = pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)
    else:
        ind_idx = ind_idx.copy()
        ind_idx["sector"] = "industry"
        ind_idx = ind_idx[ECOSYSTEM_INDEX_COLUMNS]

    eco_idx = build_ecosystem_index(sp, ecosystem_rules)

    frames = [f for f in (ind_idx, eco_idx) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined = (
        combined[ECOSYSTEM_INDEX_COLUMNS]
        .drop_duplicates(subset=["nct_id", "canonical", "sector", "entity", "role"])
        .reset_index(drop=True)
    )
    return combined


def _normalize_study_record_for_adapter(record: dict[str, Any]) -> dict[str, Any]:
    """Accept either CivicSample stored records or raw CT.gov API v2 study dicts."""
    if "protocolSection" not in record:
        return record
    proto = record.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    sc_mod = proto.get("sponsorCollaboratorsModule") or {}
    lead = sc_mod.get("leadSponsor") or {}
    collabs = sc_mod.get("collaborators") or []
    return {
        "nct_id": ident.get("nctId"),
        "lead_sponsor_name": lead.get("name"),
        "sponsor_class": lead.get("class"),
        "collaborators": [
            {"name": c.get("name"), "class": c.get("class")}
            for c in collabs
            if isinstance(c, dict)
        ],
    }


def attribute_study_sponsors(
    records: Iterable[dict[str, Any]],
    industry_rules: pd.DataFrame | None = None,
    ecosystem_rules: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, adapter.AdapterLog]:
    """Compose industry (company_aliases.csv) + ecosystem attribution for study records."""
    ind_rules = (
        sr.load_rules(DEFAULT_INDUSTRY_RULES_PATH)
        if industry_rules is None
        else industry_rules
    )
    eco_rules = (
        load_ecosystem_rules(DEFAULT_ECOSYSTEM_RULES_PATH)
        if ecosystem_rules is None
        else ecosystem_rules
    )
    normalized_records = [_normalize_study_record_for_adapter(r) for r in records]
    frame, log = adapter.records_to_frame(normalized_records)
    unified = build_unified_index(frame, ind_rules, eco_rules)
    return unified, log


def partition_unified(
    frame: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> dict[str, set[str]]:
    """Return disjoint 3-state literal partition across industry + ecosystem rules.

    A literal excluded by one layer (e.g., not industry) and attributed by the
    other (e.g., academic_medical_center) belongs strictly to 'attributed'.
    """
    uniq = sr.literal_universe(frame)
    universe = set(uniq["name"].astype(str))
    check_cross_layer_conflicts(uniq, industry_rules, ecosystem_rules)

    ind_matched = sr.match_literals(uniq, industry_rules)
    eco_matched = match_ecosystem_literals(uniq, ecosystem_rules)
    attributed = (
        set(ind_matched["name"].astype(str)) | set(eco_matched["name"].astype(str))
    ) & universe

    excluded: set[str] = set()
    for _, r in industry_rules[industry_rules["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    eco_valid = validate_ecosystem_rules(ecosystem_rules)
    for _, r in eco_valid[eco_valid["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    excluded = (excluded & universe) - attributed

    unreviewed = universe - attributed - excluded
    return {
        "attributed": attributed,
        "reviewed_excluded": excluded,
        "unreviewed": unreviewed,
    }


# ---------------------------------------------------------------------------
# 4. Sector, organization & multi-sector cohort queries (nct_id deduplicated)
# ---------------------------------------------------------------------------


def sector_trials(
    index: pd.DataFrame,
    sector: str,
    role: str = "any",
) -> pd.DataFrame:
    """Trials touching `sector` in the given role scope, deduplicated on nct_id."""
    if sector not in UNIFIED_SECTORS:
        raise EcosystemRuleValidationError(
            f"sector must be one of {list(UNIFIED_SECTORS)}; got {sector!r}"
        )
    if role not in sr.VALID_ROLES:
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got {role!r}"
        )
    sub = index[index["sector"] == sector]
    if role != "any":
        sub = sub[sub["role"] == role]
    if sub.empty:
        return pd.DataFrame(columns=["nct_id", "roles", "is_lead", "is_collaborator"])
    g = sub.groupby("nct_id")["role"].agg(lambda r: ",".join(sorted(set(r))))
    out = g.reset_index().rename(columns={"role": "roles"})
    out["is_lead"] = out["roles"].str.contains("lead")
    out["is_collaborator"] = out["roles"].str.contains("collaborator")
    return out


def organization_trials(
    index: pd.DataFrame,
    canonical: str,
    role: str = "any",
    view: str = "current_owner",
) -> pd.DataFrame:
    """Trials touching a canonical organization (or as-registered entity)."""
    return sr.trials(index, canonical, role=role, view=view)


def trial_cohorts(
    unified_index: pd.DataFrame,
    role: str = "any",
) -> dict[str, list[str]]:
    """Map each attributed nct_id to its sorted unique list of touched sectors."""
    if role not in sr.VALID_ROLES:
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got {role!r}"
        )
    sub = unified_index if role == "any" else unified_index[unified_index["role"] == role]
    if sub.empty:
        return {}
    grouped = sub.groupby("nct_id")["sector"].agg(lambda s: sorted(set(s.astype(str))))
    return {str(nct): list(sectors) for nct, sectors in grouped.items()}


# ---------------------------------------------------------------------------
# 5. Three-state coverage audit (the ONLY path that uses substring `stem`)
# ---------------------------------------------------------------------------


def audit_ecosystem(
    sponsors: pd.DataFrame,
    rules: pd.DataFrame,
    canonical: str,
    stem: str,
) -> dict[str, pd.DataFrame]:
    """Three-state coverage review for one ecosystem organization.

    Returns pairwise-disjoint DataFrames:
      - 'attributed': literals mapped to `canonical`
      - 'attributed_elsewhere': literals containing `stem` mapped to another canonical
      - 'reviewed_excluded': literals containing `stem` matched by status == 'exclude'
      - 'unreviewed_candidates': literals containing `stem` matched by no rule
    """
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(sponsors)
    matched = match_ecosystem_literals(uniq, validated)

    attributed_lits = set(
        matched.loc[matched["canonical"] == canonical, "name"].astype(str)
    )
    all_attributed = set(matched["name"].astype(str))
    excluded_lits: set[str] = set()
    for _, r in validated[validated["status"] == "exclude"].iterrows():
        excluded_lits.update(str(x) for x in sr._rule_hits(uniq, r))

    def _summarize(names: set[str]) -> pd.DataFrame:
        subs = sponsors[sponsors["name"].isin(names)]
        if subs.empty:
            return pd.DataFrame(columns=["name", "n_trials", "roles", "agency_class"])
        return (
            subs.groupby("name")
            .agg(
                n_trials=("nct_id", "nunique"),
                roles=("lead_or_collaborator", lambda r: ",".join(sorted(set(r)))),
                agency_class=("agency_class", "first"),
            )
            .reset_index()
            .sort_values(["n_trials", "name"], ascending=[False, True])
            .reset_index(drop=True)
        )

    stem_mask = uniq["name"].str.contains(stem, case=False, regex=False, na=False)
    stem_names = set(uniq.loc[stem_mask, "name"].astype(str))
    elsewhere = (stem_names & all_attributed) - attributed_lits
    unreviewed = stem_names - all_attributed - excluded_lits

    elsewhere_df = _summarize(elsewhere)
    canon_of = matched.groupby("name")["canonical"].agg(
        lambda c: "|".join(sorted(set(c.astype(str))))
    )
    elsewhere_df["canonicals"] = (
        elsewhere_df["name"].map(canon_of) if len(elsewhere_df) else []
    )

    return {
        "attributed": _summarize(attributed_lits),
        "attributed_elsewhere": elsewhere_df,
        "reviewed_excluded": _summarize(stem_names & excluded_lits),
        "unreviewed_candidates": _summarize(unreviewed),
    }


# ---------------------------------------------------------------------------
# 6. SHA-256 Regression Baseline Helpers (mirroring sponsors/baseline.py)
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a file (delegates to sponsors.baseline)."""
    return bl.sha256_file(path)


def uncovered_ecosystem_rules(
    fixture: pd.DataFrame,
    rules: pd.DataFrame,
) -> list[dict[str, str]]:
    """Return status == 'attribute' ecosystem rules matching zero fixture literals."""
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(fixture)
    out: list[dict[str, str]] = []
    for _, r in validated[validated["status"] == "attribute"].iterrows():
        if len(sr._rule_hits(uniq, r)) == 0:
            out.append(
                {
                    "canonical": str(r.canonical),
                    "sector": str(r.sector),
                    "rule_type": str(r.rule_type),
                    "pattern": str(r.pattern),
                }
            )
    return out


def compute_ecosystem_baseline(
    fixture: pd.DataFrame,
    rules: pd.DataFrame,
) -> dict[str, Any]:
    """Compute deterministic baseline block: index, sectors, organizations, entities."""
    idx = build_ecosystem_index(fixture, rules)

    sectors: dict[str, dict[str, int]] = {}
    for s in sorted(idx["sector"].unique()):
        sectors[str(s)] = {
            "any": int(len(sector_trials(idx, str(s)))),
            "lead": int(len(sector_trials(idx, str(s), role="lead"))),
            "collaborator": int(len(sector_trials(idx, str(s), role="collaborator"))),
        }

    organizations: dict[str, dict[str, int]] = {}
    for c in sorted(idx["canonical"].unique()):
        organizations[str(c)] = {
            "any": int(len(organization_trials(idx, str(c)))),
            "lead": int(len(organization_trials(idx, str(c), role="lead"))),
            "collaborator": int(
                len(organization_trials(idx, str(c), role="collaborator"))
            ),
        }

    entities: dict[str, dict[str, int]] = {}
    for e in sorted(idx["entity"].unique()):
        t = organization_trials(idx, str(e), view="as_registered")
        entities[str(e)] = {
            "any": int(len(t)),
            "lead": int(t["is_lead"].sum()),
            "collaborator": int(t["is_collaborator"].sum()),
        }

    return {
        "index": {
            "rows": int(len(idx)),
            "sectors": int(idx["sector"].nunique()),
            "organizations": int(idx["canonical"].nunique()),
            "trials": int(idx["nct_id"].nunique()),
        },
        "sectors": sectors,
        "organizations": organizations,
        "entities": entities,
    }


def load_expected_ecosystem(path: str = EXPECTED_ECOSYSTEM_PATH) -> dict[str, Any]:
    """Load SHA-256-keyed expected ecosystem counts JSON."""
    if not os.path.exists(path):
        return {"fixture": {}, "baselines": {}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_ecosystem_baseline(
    rules_sha: str,
    block: dict[str, Any],
    uncovered: list[dict[str, str]],
    fixture_meta: dict[str, Any],
    path: str = EXPECTED_ECOSYSTEM_PATH,
) -> None:
    """Write a generated baseline block keyed by ecosystem_rules.csv SHA-256."""
    data = load_expected_ecosystem(path)
    data["fixture"] = fixture_meta
    data.setdefault("baselines", {})[rules_sha] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": (
            "tests/sponsors/test_ecosystem.py (auto re-baseline on rules change)"
        ),
        "not_covered_by_fixture": uncovered,
        **block,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def diff_ecosystem_blocks(
    expected: dict[str, Any],
    actual: dict[str, Any],
    limit: int = 15,
) -> list[str]:
    """Human-readable differences between two ecosystem baseline blocks."""
    out: list[str] = []
    for section in ("index", "sectors", "organizations", "entities"):
        e = expected.get(section, {})
        a = actual.get(section, {})
        for k in sorted(set(e) | set(a)):
            if e.get(k) != a.get(k):
                out.append(f"{section}.{k}: expected {e.get(k)} got {a.get(k)}")
            if len(out) >= limit:
                out.append("…")
                return out
    return out
