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

DEPENDS  pandas (requirements.txt: pandas>=2.2.0), imported at module level:
         importing this module imports pandas. Its sibling modules
         sponsors.adapter, sponsors.baseline and sponsors.sponsor_roles import
         pandas at module level too; src/holdout_guard.py (the shared audit
         holdout guard) imports only the standard library. The other imports
         are from the Python standard library.

Audit holdout (in_audit). The only files this module reads or writes are the
two rules CSVs and the aggregate baseline JSON above; it never reads or
writes per-trial data. A trial whose `in_audit` is true in the site's
data/outcomes/index.json must not be attributed, counted or recorded. Which
entry point guards that:

  Enforce when the input carries `in_audit`, raising
  EcosystemAuditHoldoutError before any rule runs, any record is adapted or
  any rules file is read. The rules (which names are the flag, which values
  and input types are refused, what cannot be seen) are src/holdout_guard.py's,
  shared with src/analysis_population_parser.py:
      a sponsor frame or index (hg.check_frame): build_ecosystem_index,
      partition_ecosystem, build_unified_index, partition_unified,
      sector_trials, organization_trials, trial_cohorts, audit_ecosystem,
      uncovered_ecosystem_rules, compute_ecosystem_baseline, and the two
      functions that take a literal universe, match_ecosystem_literals and
      check_cross_layer_conflicts (a universe sr.literal_universe builds
      carries none; one assembled by hand from trial rows may). Anything
      that is not a plain DataFrame is a TypeError; a frame is refused when
      a flag column, row-index level, cell or `attrs` entry holds a True or
      a non-bool, when the flag is only inside cells, when a column label or
      cell is not plain data, or when, flagged, it has no nct_id column, an
      nct_id is not exactly ^NCT[0-9]{8}$, or two nct_ids for one row
      disagree. A False in `attrs` alone is ignored.
      study records (hg.check_batch): attribute_study_sponsors. Each record
      must be a dict of JSON data, else it is refused by type; a flagged
      record is refused when the flag is True or not a bool, or when its
      nct_id (top-level `nct_id` of a stored record,
      protocolSection.identificationModule.nctId of a CT.gov dict) is not
      canonical or another nct_id in it disagrees; a batch in which only
      some records carry the flag is refused; a single record or a string
      passed where a batch is expected is a TypeError.
    Argument errors name a type, never a value, so an audit id set passed
    positionally as a sector or role is not copied into a message. An
    AACT-shaped sponsor frame, a CT.gov study dict and the index these
    functions build carry no `in_audit`.
  Rely on the caller: every entry point above, for input without
  `in_audit` (no entry point receives an audit id set).
  Not trial-shaped (rules, file paths or aggregate counts, no nct_id):
  validate_ecosystem_rules, load_ecosystem_rules, sha256_file,
  load_expected_ecosystem, write_ecosystem_baseline, diff_ecosystem_blocks.
  write_ecosystem_baseline enforces it: a block, uncovered list or
  fixture_meta that is not the aggregate shape their producers give, or
  that names an NCT id anywhere (also with separators or invisible
  characters between its letters and digits), raises ValueError before the
  file is read or written (_refuse_trial_shaped_baseline).
  Callers today: none in code. The only importer in this repository is
  tests/sponsors/test_ecosystem.py. PR #97's outcomes layout
  (src/outcomes_layout.py at dc38220, on its own branch) reads sector names
  from a trial's or cohort manifest's `ecosystem_category` field without
  importing this module, and has no holdout backstop for it:
  _study_scalar_fields reads `ecosystem_category` from the trial, and
  build_trial_bundle copies it into the index row and passes it to
  derive_cohorts for every trial, in_audit ones included; no firewall
  vector checks that field for a holdout. Whoever fills that field
  must not compute it for a holdout trial; the backstop (refusing a
  holdout's `ecosystem_category`) belongs on that consumer branch, and its
  test there: this module may not import it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from src import holdout_guard as hg

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
# 0. Audit holdout refusal (in_audit): the rules are src/holdout_guard.py's
# ---------------------------------------------------------------------------

HOLDOUT_REFUSAL_REASONS: tuple[str, ...] = hg.HOLDOUT_REFUSAL_REASONS


class EcosystemAuditHoldoutError(hg.AuditHoldoutError):
    """Raised when an entry point is handed sponsor rows it must not attribute.

    This module's subclass of src/holdout_guard.py AuditHoldoutError:
    `reason` is one of HOLDOUT_REFUSAL_REASONS. Deliberately NOT a subclass
    of ValueError or EcosystemRuleValidationError: an `except ValueError`
    written for a rules problem must not swallow a holdout refusal. Messages
    give counts, row positions and value types, never an nct_id or a sponsor
    name, so the refusal itself records nothing about a holdout trial.
    """


# The columns sr.literal_universe builds and sr._rule_hits reads.
_LITERAL_UNIVERSE_COLUMNS: tuple[str, ...] = ("name", "norm", "parent_norm")


def _refuse_literal_universe_holdout(uniq: Any, where: str) -> None:
    """Refuse a literal universe that is not one, or that carries in_audit.

    Method: a DataFrame lacking a column sr.literal_universe builds
    (_LITERAL_UNIVERSE_COLUMNS) is not a universe and raises KeyError, as
    reading it would have (a sponsor frame passed by mistake still fails
    that way). Then the universe gets the full frame check
    (hg.check_frame): a universe built by sr.literal_universe
    holds distinct names only, with no nct_id and no in_audit, so it passes
    unchanged; one that was assembled by hand and still carries a trial's
    in_audit is refused like a sponsor frame.
    """
    if isinstance(uniq, pd.DataFrame):
        missing = [c for c in _LITERAL_UNIVERSE_COLUMNS if c not in uniq.columns]
        if missing:
            raise KeyError(
                f"{where} expects a literal universe (sr.literal_universe columns "
                f"{list(_LITERAL_UNIVERSE_COLUMNS)}); missing {missing}"
            )
    hg.check_frame(uniq, where, EcosystemAuditHoldoutError)


def _raw_record_nct_id(record: dict[str, Any]) -> Any:
    """The nct_id a study record would be attributed under, exactly as stored.

    A raw CT.gov API v2 dict (has `protocolSection`) keeps it at
    protocolSection.identificationModule.nctId; a CivicSample stored record
    keeps it at top-level `nct_id`. This mirrors
    _normalize_study_record_for_adapter without its .get() fallbacks, so a
    missing or non-dict nested level yields None, which the holdout check
    refuses. hg.check_record calls it only for a flagged dict.
    """
    if "protocolSection" not in record:
        return record.get("nct_id")
    proto = record.get("protocolSection")
    ident = proto.get("identificationModule") if isinstance(proto, dict) else None
    return ident.get("nctId") if isinstance(ident, dict) else None


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
    When several rules claim a literal for the same canonical, the
    highest-precedence one is kept as its match_rule, and its `shared` is
    "yes" only if every one of those rules says "yes".
    Raises EcosystemRuleValidationError when:
      * two attribute rules claim one literal for different canonicals and not
        EVERY claiming rule (each rule of each canonical) has shared == 'yes', or
      * an attribute rule and an exclude rule both match the same literal.

    Audit holdout: ENFORCES when `uniq` carries `in_audit`. `uniq` is meant
    to be a literal universe (sr.literal_universe: distinct sponsor names,
    no nct_id), which carries none, so the functions that build it from
    trial rows enforce first. A frame without the universe's columns raises
    KeyError; a universe that carries in_audit anyway (assembled by hand
    from trial rows) gets the full frame check, raising
    EcosystemAuditHoldoutError as build_ecosystem_index does (see
    _refuse_literal_universe_holdout). Callers today: build_ecosystem_index,
    partition_ecosystem, check_cross_layer_conflicts, partition_unified and
    audit_ecosystem in this module (and through them build_unified_index,
    attribute_study_sponsors and compute_ecosystem_baseline); none elsewhere
    in code (see the module docstring).
    """
    _refuse_literal_universe_holdout(uniq, "match_ecosystem_literals")
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
        for pos, (c, c_sector, c_rule, c_shared) in enumerate(claims):
            if c != canonical:
                continue
            # Same canonical via a second, lower-precedence rule: the first
            # rule is kept, but the literal is shared only if every claiming
            # rule says so, or a "no" would be dropped and the conflict
            # checks (below, and check_cross_layer_conflicts) would see "yes".
            merged = "yes" if c_shared == "yes" and shared == "yes" else "no"
            claims[pos] = (c, c_sector, c_rule, merged)
            if merged != "yes" and len(claims) > 1:
                others = sorted({o for o, _, _, _ in claims if o != canonical})
                raise EcosystemRuleValidationError(
                    f"conflicting ecosystem rules for literal {literal!r}: {others} vs "
                    f"{canonical!r} (via {rule}, shared={shared}). If this literal "
                    "genuinely names several organizations, mark EVERY claiming rule "
                    "shared=yes."
                )
            return
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
    """Attribute sponsor rows to canonical non-industry ecosystem organizations.

    Audit holdout: ENFORCES when `sponsors` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    build_unified_index and compute_ecosystem_baseline in this module; none
    elsewhere in code (see the module docstring).
    """
    hg.check_frame(sponsors, "build_ecosystem_index", EcosystemAuditHoldoutError)
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
    """Return disjoint {'attributed', 'reviewed_excluded', 'unreviewed'} literal sets.

    Audit holdout: ENFORCES when `frame` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    hg.check_frame(frame, "partition_ecosystem", EcosystemAuditHoldoutError)
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

    Audit holdout: ENFORCES when `uniq` carries `in_audit`, exactly as
    match_ecosystem_literals does (_refuse_literal_universe_holdout), before
    either layer's rules run. A universe built by sr.literal_universe
    carries none. Callers today: build_unified_index and partition_unified
    in this module (and through build_unified_index,
    attribute_study_sponsors); none elsewhere in code (see the module
    docstring).
    """
    _refuse_literal_universe_holdout(uniq, "check_cross_layer_conflicts")
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
    """Build unified industry + ecosystem sponsor index with sector column.

    Audit holdout: ENFORCES when `sponsors` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame (or the frame sponsors.adapter builds from
    records) carries no `in_audit`; for it this function relies on its caller
    to exclude holdout trials. Callers today: attribute_study_sponsors in this
    module; none elsewhere in code (see the module docstring).
    """
    hg.check_frame(sponsors, "build_unified_index", EcosystemAuditHoldoutError)
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
    """Compose industry (company_aliases.csv) + ecosystem attribution for study records.

    Audit holdout: ENFORCES on every record that carries `in_audit`, for all
    records before any rules file is read or any record is adapted
    (hg.check_batch, with _raw_record_nct_id as the stamped id: see the
    module docstring and src/holdout_guard.py), raising
    EcosystemAuditHoldoutError. Each record must be a dict of JSON data; a
    batch in which only some records carry `in_audit` is refused; a single
    record or a string passed in place of an iterable of records is a
    TypeError before anything is read (hg.refuse_single_record). A stored
    record or a CT.gov study dict carries no `in_audit`; for a batch of them
    this function relies on its caller to exclude holdout trials. Callers
    today: none in code (see the module docstring).
    """
    hg.refuse_single_record(records, "attribute_study_sponsors")
    record_list: list[dict[str, Any]] = list(records)
    hg.check_batch(
        record_list, "attribute_study_sponsors", EcosystemAuditHoldoutError, _raw_record_nct_id
    )
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
    normalized_records = [_normalize_study_record_for_adapter(r) for r in record_list]
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

    Audit holdout: ENFORCES when `frame` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    hg.check_frame(frame, "partition_unified", EcosystemAuditHoldoutError)
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
    """Trials touching `sector` in the given role scope, deduplicated on nct_id.

    Audit holdout: ENFORCES when `index` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    The index build_ecosystem_index and build_unified_index return
    (ECOSYSTEM_INDEX_COLUMNS only) carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    compute_ecosystem_baseline in this module; none elsewhere in code (see the
    module docstring).
    """
    hg.check_frame(index, "sector_trials", EcosystemAuditHoldoutError)
    # The messages name the argument's type, never its value: an audit id
    # set passed positionally as sector or role would otherwise be copied
    # into the message, and into any log or traceback that keeps it.
    if sector not in UNIFIED_SECTORS:
        raise EcosystemRuleValidationError(
            f"sector must be one of {list(UNIFIED_SECTORS)}; got a "
            f"{type(sector).__name__} that is not one of them"
        )
    if role not in sr.VALID_ROLES:
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got a "
            f"{type(role).__name__} that is not one of them"
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


def _require_str_argument(name: str, value: object) -> None:
    """Raise TypeError unless `value` is exactly a str.

    Without this, a canonical that is not text (an int, or a tuple of ids
    passed by position) is compared with ``==`` against a column of names,
    matches nothing and returns empty results: a silent failure. The
    message names the argument's type only, never its value, so an audit
    id set passed here does not reach a log (see sector_trials).
    """
    if type(value) is not str:
        raise TypeError(f"{name} must be a str; got a {type(value).__name__}")


def organization_trials(
    index: pd.DataFrame,
    canonical: str,
    role: str = "any",
    view: str = "current_owner",
) -> pd.DataFrame:
    """Trials touching a canonical organization (or as-registered entity).

    Audit holdout: ENFORCES when `index` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    The index build_ecosystem_index and build_unified_index return
    (ECOSYSTEM_INDEX_COLUMNS only) carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    compute_ecosystem_baseline in this module; none elsewhere in code (see the
    module docstring).
    """
    hg.check_frame(index, "organization_trials", EcosystemAuditHoldoutError)
    _require_str_argument("canonical", canonical)
    return sr.trials(index, canonical, role=role, view=view)


def trial_cohorts(
    unified_index: pd.DataFrame,
    role: str = "any",
) -> dict[str, list[str]]:
    """Map each attributed nct_id to its sorted unique list of touched sectors.

    Audit holdout: ENFORCES when `unified_index` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    The index build_unified_index returns (ECOSYSTEM_INDEX_COLUMNS only)
    carries no `in_audit`; for it this function relies on its caller to
    exclude holdout trials. Callers today: none in code (see the module
    docstring).
    """
    hg.check_frame(unified_index, "trial_cohorts", EcosystemAuditHoldoutError)
    if role not in sr.VALID_ROLES:
        # The type, never the value: an audit id set passed as role must not
        # reach the message (see sector_trials).
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got a "
            f"{type(role).__name__} that is not one of them"
        )
    sub = unified_index if role == "any" else unified_index[unified_index["role"] == role]
    if sub.empty:
        return {}
    grouped = sub.groupby("nct_id")["sector"].agg(lambda s: sorted(set(s.astype(str))))
    # hg.plain_text, not str(): each key is the stored id's characters as a
    # plain str, never what a str subclass's __str__ says.
    return {hg.plain_text(nct): list(sectors) for nct, sectors in grouped.items()}


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

    Audit holdout: ENFORCES when `sponsors` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    hg.check_frame(sponsors, "audit_ecosystem", EcosystemAuditHoldoutError)
    _require_str_argument("canonical", canonical)
    _require_str_argument("stem", stem)
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
    """Return status == 'attribute' ecosystem rules matching zero fixture literals.

    Audit holdout: ENFORCES when `fixture` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    hg.check_frame(fixture, "uncovered_ecosystem_rules", EcosystemAuditHoldoutError)
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
    """Compute deterministic baseline block: index, sectors, organizations, entities,
    shared.

    The sections are ECOSYSTEM_BASELINE_SECTIONS, in that order. `shared`
    pins the index's `shared` column, which the other sections cannot see
    (see the comment where it is built).

    Audit holdout: ENFORCES when `fixture` carries `in_audit` (a column,
    row-index level, cell or `attrs` entry under a flag name), raising
    EcosystemAuditHoldoutError before any rule runs; anything that is not
    a plain DataFrame is a TypeError (hg.check_frame: see the module
    docstring and src/holdout_guard.py).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    hg.check_frame(fixture, "compute_ecosystem_baseline", EcosystemAuditHoldoutError)
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

    # `shared` pin (the same rule as sponsors/baseline.py's industry section).
    # The counts above cannot see the index's `shared` column: a rule or
    # matching change that flips a row's shared value moves no attribution,
    # yet changes whether build_unified_index and check_cross_layer_conflicts
    # let another claim on that literal through, and which trials a sum
    # across organizations must dedupe on nct_id. For each organization with
    # at least one shared=yes index row, pin the number of those rows and the
    # trials they cover by scope (organization_trials, as `organizations`).
    # `rows` catches a flip that leaves the trial counts alone (a trial that
    # already has another shared=yes row for that organization in that
    # role). An organization that loses its last shared=yes row drops out of
    # the section, and the comparison reports it. Only "yes" is counted:
    # within the index "no" is its complement, so a single flip either way
    # changes `rows`. While every ecosystem rule is shared=no the section is
    # empty, and that emptiness is what it pins.
    yes = idx[idx["shared"] == "yes"]
    shared: dict[str, dict[str, int]] = {}
    for c in sorted(yes["canonical"].unique()):
        shared[str(c)] = {
            "rows": int((yes["canonical"] == c).sum()),
            "any": int(len(organization_trials(yes, str(c)))),
            "lead": int(len(organization_trials(yes, str(c), role="lead"))),
            "collaborator": int(len(organization_trials(yes, str(c), role="collaborator"))),
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
        "shared": shared,
    }


# The sections of a baseline block, in comparison order. A block written
# before a section existed is completed through write_ecosystem_baseline's
# complete_sections (see tests/sponsors/test_ecosystem.py); a missing
# section is never compared as if it were empty.
ECOSYSTEM_BASELINE_SECTIONS: tuple[str, ...] = (
    "index", "sectors", "organizations", "entities", "shared",
)
# The shape write_ecosystem_baseline accepts: what compute_ecosystem_baseline,
# uncovered_ecosystem_rules and the fixture description produce
# (_refuse_trial_shaped_baseline).
_BASELINE_SECTIONS: frozenset[str] = frozenset(ECOSYSTEM_BASELINE_SECTIONS)
_BASELINE_INDEX_KEYS: frozenset[str] = frozenset({"rows", "sectors", "organizations", "trials"})
_BASELINE_COUNT_KEYS: frozenset[str] = frozenset({"any", "lead", "collaborator"})
_SHARED_COUNT_KEYS: frozenset[str] = _BASELINE_COUNT_KEYS | {"rows"}
_UNCOVERED_KEYS: frozenset[str] = frozenset({"canonical", "sector", "rule_type", "pattern"})
_FIXTURE_META_KEYS: frozenset[str] = frozenset({"file", "sha256", "snapshot_date"})
_RULES_SHA_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")
# An NCT id written anywhere in a string: "N", "C" and "T" with any
# separators (punctuation, spaces, characters that print as nothing) between
# them, then eight digits with any separators before and between them
# ("NCT9900-0101"), in any case. Wider than the canonical form on purpose:
# the holdout guard refuses such ids as possible holdouts in disguise, so the
# baseline must not hold one either. Look-alike or full-width letters are not
# mapped (see src/holdout_guard.py on what is not seen).
_NCT_ID_ANYWHERE_RE: re.Pattern[str] = re.compile(
    r"N[\W_]*C[\W_]*T(?:[\W_]*\d){8}", re.IGNORECASE
)


def _counts_section_ok(
    section: Any,
    keys: frozenset[str] | None,
    count_keys: frozenset[str] = _BASELINE_COUNT_KEYS,
) -> bool:
    """True when `section` maps names to {count name: int}, or (keys given) names to ints.

    With `keys`, `section` itself must map exactly those names to ints (the
    "index" section); without, it maps any str name to a mapping of exactly
    `count_keys` to ints (_BASELINE_COUNT_KEYS by default; _SHARED_COUNT_KEYS
    for "shared"). An int is `type(v) is int`: a bool is not a count.
    """
    if not isinstance(section, dict):
        return False
    if keys is not None:
        return set(section) == keys and all(type(v) is int for v in section.values())
    return all(
        isinstance(name, str)
        and isinstance(counts, dict)
        and set(counts) == count_keys
        and all(type(v) is int for v in counts.values())
        for name, counts in section.items()
    )


def _strings_of(value: Any) -> list[str]:
    """Every str key and value in a JSON-shaped value (dicts, lists, scalars)."""
    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _refuse_trial_shaped_baseline(
    rules_sha: Any, block: Any, uncovered: Any, fixture_meta: Any
) -> None:
    """Refuse a baseline write whose parts are not the aggregate shape documented for it.

    write_ecosystem_baseline writes the committed baseline file
    (tests/sponsors/expected_ecosystem_counts.json by default), which the
    module docstring lists as not trial-shaped: rules, file paths and
    aggregate counts, no nct_id and no in_audit. Method: before anything
    is read or written, each part must have exactly the shape its producer
    gives it, or ValueError is raised:
      * rules_sha: 64 lowercase hex digits (sha256_file);
      * block: exactly the sections ECOSYSTEM_BASELINE_SECTIONS ("index",
        "sectors", "organizations", "entities" and "shared":
        compute_ecosystem_baseline); "index" maps "rows", "sectors",
        "organizations" and "trials" to ints; "sectors", "organizations"
        and "entities" map a name to "any", "lead" and "collaborator" ints,
        and "shared" maps a name to "rows", "any", "lead" and
        "collaborator" ints (a bool is not an int here); "sectors" names
        only sectors this module defines, and "shared" only organizations
        the block's "organizations" names;
      * uncovered: a list of records with exactly the str fields
        "canonical", "sector", "rule_type" and "pattern"
        (uncovered_ecosystem_rules);
      * fixture_meta: a mapping of any of "file", "sha256" and
        "snapshot_date" to str;
      * no key or str anywhere in them may contain an NCT id ("NCT" then
        eight digits, case and separators ignored: _NCT_ID_ANYWHERE_RE).
    So a trial_cohorts() result, a manifest row or an in_audit flag cannot
    land in the committed file. Messages name the part and a count, never a
    key or value. Assumption: compute_ecosystem_baseline,
    uncovered_ecosystem_rules and the fixture description keep these
    shapes; a change to one of them changes this check in the same commit.
    """
    if not isinstance(rules_sha, str) or _RULES_SHA_RE.fullmatch(rules_sha) is None:
        raise ValueError(
            "write_ecosystem_baseline: rules_sha must be 64 lowercase hex digits "
            "(sha256_file of the rules); nothing is written"
        )
    if not isinstance(block, dict) or set(block) != _BASELINE_SECTIONS:
        extra = len(set(block) - _BASELINE_SECTIONS) if isinstance(block, dict) else 0
        raise ValueError(
            "write_ecosystem_baseline: block must hold exactly the sections index, "
            f"sectors, organizations, entities and shared ({extra} other key(s)); a "
            "baseline holds aggregate counts only, so nothing is written"
        )
    sections_ok = (
        _counts_section_ok(block["index"], _BASELINE_INDEX_KEYS)
        and all(
            _counts_section_ok(block[section], None)
            for section in ("sectors", "organizations", "entities")
        )
        and _counts_section_ok(block["shared"], None, _SHARED_COUNT_KEYS)
        and set(block["sectors"]) <= VALID_ECOSYSTEM_SECTORS | set(UNIFIED_SECTORS)
        and set(block["shared"]) <= set(block["organizations"])
    )
    if not sections_ok:
        raise ValueError(
            "write_ecosystem_baseline: a block section is not names mapped to "
            "integer counts as compute_ecosystem_baseline gives them; nothing is written"
        )
    if not isinstance(uncovered, list) or not all(
        isinstance(rule, dict)
        and set(rule) == _UNCOVERED_KEYS
        and all(isinstance(v, str) for v in rule.values())
        for rule in uncovered
    ):
        raise ValueError(
            "write_ecosystem_baseline: uncovered must be a list of rule records "
            "(canonical, sector, rule_type, pattern) as uncovered_ecosystem_rules "
            "gives them; nothing is written"
        )
    if not isinstance(fixture_meta, dict) or not (
        set(fixture_meta) <= _FIXTURE_META_KEYS
        and all(isinstance(v, str) for v in fixture_meta.values())
    ):
        raise ValueError(
            "write_ecosystem_baseline: fixture_meta may hold only file, sha256 and "
            "snapshot_date strings; nothing is written"
        )
    parts = (("block", block), ("uncovered", uncovered), ("fixture_meta", fixture_meta))
    for part_name, part in parts:
        held = sum(
            1
            for text in _strings_of(part)
            if _NCT_ID_ANYWHERE_RE.search(text) is not None
        )
        if held:
            raise ValueError(
                f"write_ecosystem_baseline: {part_name} holds {held} string(s) naming "
                "an NCT id; a baseline is not trial-shaped, so nothing is written"
            )


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
    *,
    complete_sections: Iterable[str] = (),
) -> None:
    """Write a generated baseline block keyed by ecosystem_rules.csv SHA-256.

    Not trial-shaped, and enforced: before the file is read or written,
    rules_sha, block, uncovered and fixture_meta must have exactly the
    aggregate shape their producers give them (sha256_file,
    compute_ecosystem_baseline, uncovered_ecosystem_rules, the fixture
    description), with no NCT id anywhere in them; otherwise ValueError is
    raised and nothing is written (_refuse_trial_shaped_baseline).

    complete_sections: instead of writing a new block, add these sections of
    `block` to the block already stored for `rules_sha`: the case of a block
    written before compute_ecosystem_baseline produced them. Method: after
    the shape check above, the names must be sections of
    ECOSYSTEM_BASELINE_SECTIONS (checked before the file is read); the
    stored block must exist, lack every named section, and hold every
    other section with exactly `block`'s counts, so a section is never
    added beside counts that have drifted; and the file's fixture
    description must equal `fixture_meta`. Otherwise ValueError, and
    nothing is written. Only the named sections are added, each stamped
    under `sections_backfilled`; generated_at, generated_by,
    not_covered_by_fixture and the fixture description stay as they are. A
    stored section is never overwritten (that would be a silent
    re-baseline). Messages name sections and counts, never a block key or
    value (_complete_stored_block).
    """
    _refuse_trial_shaped_baseline(rules_sha, block, uncovered, fixture_meta)
    to_complete = _sections_to_complete(complete_sections)
    data = load_expected_ecosystem(path)
    if to_complete:
        _complete_stored_block(data, rules_sha, block, fixture_meta, to_complete)
    else:
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


def _sections_to_complete(sections: Any) -> tuple[str, ...]:
    """The validated complete_sections of write_ecosystem_baseline, in section order.

    Method: an iterable (not a str) of distinct names, each one of
    ECOSYSTEM_BASELINE_SECTIONS, leaving at least one section to compare
    the stored block against; otherwise ValueError naming a count only, so a
    stray value is never copied into a message. Runs before any file is read.
    """
    if isinstance(sections, (str, bytes)):
        raise ValueError(
            "write_ecosystem_baseline: complete_sections must be a collection of "
            "section names, not one string; nothing is written"
        )
    names = list(sections)
    unknown = sum(1 for n in names if not isinstance(n, str) or n not in _BASELINE_SECTIONS)
    if unknown or len(set(names)) != len(names):
        raise ValueError(
            f"write_ecosystem_baseline: complete_sections holds {unknown} name(s) that "
            "are not baseline sections, or a repeated name; nothing is written"
        )
    if names and len(set(names)) == len(ECOSYSTEM_BASELINE_SECTIONS):
        raise ValueError(
            "write_ecosystem_baseline: complete_sections may not name every section; "
            "at least one stored section must be compared, so nothing is written"
        )
    return tuple(s for s in ECOSYSTEM_BASELINE_SECTIONS if s in names)


def _complete_stored_block(
    data: dict[str, Any],
    rules_sha: str,
    block: dict[str, Any],
    fixture_meta: dict[str, Any],
    sections: tuple[str, ...],
) -> None:
    """Add `sections` of `block` to the stored block for `rules_sha` in `data`.

    Method: the checks write_ecosystem_baseline's complete_sections
    documents, in order (stored block present, same fixture description,
    named sections absent, every other section present and equal), each a
    ValueError that names sections only; then the named sections are added
    and stamped under `sections_backfilled`. Mutates `data`; reads and
    writes no file (write_ecosystem_baseline does).
    """
    stored = data.get("baselines", {}).get(rules_sha)
    if not isinstance(stored, dict):
        raise ValueError(
            "write_ecosystem_baseline: complete_sections needs a block already "
            "stored for rules_sha; nothing is written"
        )
    if data.get("fixture") != fixture_meta:
        raise ValueError(
            "write_ecosystem_baseline: the stored block was computed on another "
            "fixture description than fixture_meta; nothing is written"
        )
    already = [s for s in sections if s in stored]
    if already:
        raise ValueError(
            f"write_ecosystem_baseline: {', '.join(already)} already stored; a stored "
            "section is never overwritten, so nothing is written"
        )
    kept = [s for s in ECOSYSTEM_BASELINE_SECTIONS if s not in sections]
    absent = [s for s in kept if s not in stored]
    if absent:
        raise ValueError(
            f"write_ecosystem_baseline: the stored block also lacks {', '.join(absent)}, "
            "which complete_sections does not name; nothing is written"
        )
    drifted = [s for s in kept if stored[s] != block[s]]
    if drifted:
        raise ValueError(
            f"write_ecosystem_baseline: stored {', '.join(drifted)} differ from block; a "
            "section is added only beside matching counts, so nothing is written"
        )
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in sections:
        stored[s] = block[s]
        stored.setdefault("sections_backfilled", {})[s] = {
            "generated_at": stamp,
            "generated_by": (
                "tests/sponsors/test_ecosystem.py (section added to an existing block)"
            ),
        }


def diff_ecosystem_blocks(
    expected: dict[str, Any],
    actual: dict[str, Any],
    limit: int = 15,
) -> list[str]:
    """Human-readable differences between two ecosystem baseline blocks.

    A section the earlier block lacks (written before the section existed)
    is reported once as such, not as every one of its values being new.
    """
    out: list[str] = []
    for section in ECOSYSTEM_BASELINE_SECTIONS:
        if section not in expected:
            out.append(f"{section}: not in the earlier block, nothing to compare")
            continue
        e = expected.get(section, {})
        a = actual.get(section, {})
        for k in sorted(set(e) | set(a)):
            if e.get(k) != a.get(k):
                out.append(f"{section}.{k}: expected {e.get(k)} got {a.get(k)}")
            if len(out) >= limit:
                out.append("…")
                return out
    return out
