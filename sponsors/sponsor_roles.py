"""
sponsor_roles.py  (v3.1: bundle v3 + CivicSample amendment A6)
==============================================================
Company-level sponsor filtering for ClinicalTrials.gov / AACT.

Purpose
-------
Given the AACT sponsors table (nct_id | agency_class | lead_or_collaborator | name),
answer: "which trials does company X touch, and in what role?" with three filter
scopes, in this order of use:

    1. any involvement   (lead OR collaborator)
    2. lead sponsor
    3. collaborator

Proposed design rules (drafted 2026-08-31, pending sign-off; written so a
co-author can run and extend this without the lead author in the room)
-----------------------------------------------------------------------
* Matching is LIST-DRIVEN, not substring-driven. A trial is attributed to a
  company only if its literal sponsor name matches a rule in the alias table.
  Substring search is used only in audit() to surface candidates for review.
  Reason: substring matching conflates distinct companies (Merck & Co. vs
  Merck KGaA is the canonical failure).
* THREE REVIEW STATES, mirroring the program's three-state rule. Every literal
  sponsor name is in exactly one state:
      (1) attributed   - a rule maps it to a canonical company
      (2) reviewed-excluded - a human looked and recorded "not one of ours"
                              (rule with status=exclude; the note says why)
      (3) unreviewed   - no rule touches it yet
  audit() reports all three. States 2 and 3 are never collapsed.
* TWO OWNERSHIP VIEWS, both always recoverable:
      view="current_owner"  - trials roll up to the acquiring parent
                              (Wyeth trial counts as Pfizer)
      view="as_registered"  - trials stay with the registered entity
                              (Wyeth trial counts as Wyeth)
  The index carries both `canonical` (current owner) and `entity`
  (as-registered identity, parsed from the literal name), so the dashboard
  can toggle between them and compare an entity before vs after acquisition.
* PARTNERSHIP LITERALS (one string naming several companies, e.g.
  "Bristol-Myers Squibb & Pfizer") are attributed to EVERY named company,
  each via its own rule row marked shared=yes. Trial-level filters stay
  correct; anything that sums across companies must dedupe on nct_id or it
  will double count these trials. shared=yes is the marker for that.
* Conflicting rules for one literal raise an error instead of last-write-wins,
  UNLESS every competing rule is marked shared=yes (the partnership case).

Inputs
------
1. AACT sponsors.txt (pipe-delimited).
2. company_aliases.csv, one rule per row:
       canonical   - canonical company label, e.g. "Pfizer"
       rule_type   - exact_literal | exact_normalized | subsidiary_of
       pattern     - raw string for exact_literal; normalized string otherwise
       status      - attribute (default) | exclude
                     exclude = reviewed, deliberately NOT attributed; the
                     canonical column then names what it was reviewed against
       shared      - yes | no (default no); yes marks partnership literals
                     that legitimately map to several canonicals
       note        - who decided, when, and why. REQUIRED free text.
3. Optional acquisitions.csv for the ownership timeline:
       entity, canonical, effective_date (YYYY-MM-DD), source, note
   Dates must come from a primary source; leave blank until verified.

Typical use
-----------
    import sponsor_roles as sr
    sp    = sr.load_sponsors(SPONSORS_PATH)
    rules = sr.load_rules(ALIAS_PATH)
    index = sr.build_index(sp, rules)

    sr.trials(index, "Pfizer")                          # 1. any involvement
    sr.trials(index, "Pfizer", role="lead")             # 2. lead sponsor
    sr.trials(index, "Pfizer", role="collaborator")     # 3. collaborator

    sr.trials(index, "wyeth", view="as_registered")     # pre-acquisition entity
    sr.entities(index, "Pfizer")                        # constituent entities

    flags = sr.company_flags(index, ["Pfizer", "Merck & Co"])
    sr.audit(sp, rules, "Merck & Co", stem="merck")     # three-state coverage
"""

from __future__ import annotations

import re
import pandas as pd

# --------------------------------------------------------------------------- #
# 1. Loading                                                                   #
# --------------------------------------------------------------------------- #

REQUIRED_COLS = ["nct_id", "agency_class", "lead_or_collaborator", "name"]
VALID_ROLES = {"any", "lead", "collaborator"}
VALID_RULE_TYPES = {"exact_literal", "exact_normalized", "subsidiary_of"}
VALID_STATUS = {"attribute", "exclude"}
VALID_VIEWS = {"current_owner", "as_registered"}


def load_sponsors(path: str) -> pd.DataFrame:
    """Read AACT sponsors.txt and verify the columns we depend on."""
    sp = pd.read_csv(path, sep="|", dtype=str)
    missing = [c for c in REQUIRED_COLS if c not in sp.columns]
    if missing:
        raise ValueError(f"sponsors file missing columns: {missing}")
    return sp


def load_rules(path: str) -> pd.DataFrame:
    """Read the curated alias table; validate types, statuses, and notes."""
    rules = pd.read_csv(path, dtype=str)
    for col, default in [("status", "attribute"), ("shared", "no"), ("note", "")]:
        if col not in rules.columns:
            rules[col] = default
    rules["status"] = rules["status"].fillna("attribute")
    rules["shared"] = rules["shared"].fillna("no")

    required = ["canonical", "rule_type", "pattern"]
    missing = [c for c in required if c not in rules.columns]
    if missing:
        raise ValueError(f"alias file missing columns: {missing}")
    bad = set(rules.rule_type) - VALID_RULE_TYPES
    if bad:
        raise ValueError(f"unknown rule_type values: {bad}")
    bad = set(rules.status) - VALID_STATUS
    if bad:
        raise ValueError(f"unknown status values: {bad}")
    no_note = rules.note.isna() | (rules.note.str.strip() == "")
    if no_note.any():
        raise ValueError(
            f"{int(no_note.sum())} rule(s) have an empty note. Every rule must "
            "record who decided and why (first offender: "
            f"{rules.loc[no_note, ['canonical', 'pattern']].iloc[0].to_dict()})"
        )
    return rules


def load_acquisitions(path: str) -> pd.DataFrame:
    """Read the optional acquisitions timeline (entity -> parent, dated)."""
    acq = pd.read_csv(path, dtype=str)
    required = ["entity", "canonical", "effective_date", "source"]
    missing = [c for c in required if c not in acq.columns]
    if missing:
        raise ValueError(f"acquisitions file missing columns: {missing}")
    acq["effective_date"] = pd.to_datetime(acq["effective_date"], errors="coerce")
    acq.attrs["source_path"] = str(path)
    return acq


# --------------------------------------------------------------------------- #
# 2. Name normalization (mechanical layer only)                               #
# --------------------------------------------------------------------------- #

# Trailing legal-form tokens stripped repeatedly ("Pfizer Inc." -> "pfizer").
# NOTE: 'kgaa' is deliberately NOT stripped: it distinguishes Merck KGaA
# from Merck & Co. after normalization.
_SUFFIX_RE = re.compile(
    r"\s+(incorporated|inc|llc|llp|ltd|limited|gmbh|ag|plc|corp|corporation|"
    r"co|company|sa|nv|bv|ab|oy|kk|spa|sas)$"
)


def normalize(name: str) -> str:
    """Lowercase, drop punctuation, strip trailing legal suffixes.

    This collapses trivial variants only ("Pfizer" == "Pfizer Inc.").
    It never merges distinct companies; that judgment lives in the alias CSV.
    """
    s = str(name).lower().strip()
    s = re.sub(r"[.,;:()\"']", " ", s)
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"\s+", " ", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
        s = re.sub(r"\s+and$", "", s)  # "merck and co" -> "merck and" -> "merck"
    return s


# "X, a wholly owned subsidiary of Y" -- the registry text names the parent.
_SUBSIDIARY_RE = re.compile(
    r"^(?P<sub>.+?),?\s+(?:is\s+)?(?:now\s+)?(?:a\s+)?(?:wholly[- ]owned\s+)?"
    r"subsidiar\w+\s+of\s+(?P<parent>.+)$",
    re.IGNORECASE,
)


def _subsidiary_parts(name: str) -> tuple[str | None, str | None]:
    """Return (normalized entity, normalized ultimate parent) for
    self-described subsidiaries, else (None, None). Nested chains
    ("X, a subsidiary of Y, a subsidiary of Z") resolve entity=X, parent=Z."""
    m = _SUBSIDIARY_RE.match(str(name))
    if not m:
        return None, None
    entity = normalize(m.group("sub"))
    parent = m.group("parent")
    deeper = _SUBSIDIARY_RE.match(parent)
    if deeper:
        _, parent_norm = _subsidiary_parts(parent)
        return entity, parent_norm
    parent = re.sub(r"\(.*?\)\s*$", "", parent).strip(" ,.")
    return entity, normalize(parent)


def parsed_parent(name: str) -> str | None:
    """Normalized ultimate parent for self-described subsidiaries, else None."""
    return _subsidiary_parts(name)[1]


def parsed_entity(name: str) -> str:
    """The as-registered identity of a literal sponsor name, normalized.

    For "Wyeth is now a wholly owned subsidiary of Pfizer" this is "wyeth";
    for a plain name it is just normalize(name)."""
    entity, _ = _subsidiary_parts(name)
    return entity if entity is not None else normalize(name)


# --------------------------------------------------------------------------- #
# 3. Building the trial-company-role index                                    #
# --------------------------------------------------------------------------- #

def build_index(sponsors: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Attribute sponsor rows to canonical companies.

    Returns one row per (nct_id, canonical, role) with columns:
        nct_id, canonical, entity, role, literal_name, agency_class,
        match_rule, shared
    `canonical` is the current-owner label from the rule; `entity` is the
    as-registered identity parsed from the literal name (normalized).
    Only status=attribute rules produce rows; exclusions and coverage review
    are audit()'s job. Match precedence per literal name: exact_literal >
    exact_normalized > subsidiary_of. Within one literal, several canonicals
    are allowed only if every claiming rule is shared=yes; otherwise the
    conflict raises.
    """
    sp = sponsors.copy()
    uniq = pd.DataFrame({"name": sp["name"].dropna().unique()})
    uniq["norm"] = uniq["name"].map(normalize)
    uniq["parent_norm"] = uniq["name"].map(parsed_parent)

    active = rules[rules.status == "attribute"]

    # literal -> list of (canonical, rule_type, shared)
    lit_map: dict[str, list[tuple[str, str, str]]] = {}

    def _assign(literal: str, canonical: str, rule: str, shared: str) -> None:
        claims = lit_map.setdefault(literal, [])
        if any(c == canonical for c, _, _ in claims):
            return  # same canonical via a second rule: fine, keep first
        if claims and not (shared == "yes" and all(s == "yes" for _, _, s in claims)):
            others = sorted({c for c, _, _ in claims})
            raise ValueError(
                f"conflicting rules for literal {literal!r}: {others} vs "
                f"{canonical!r} (via {rule}). If this literal genuinely names "
                "several companies, mark EVERY claiming rule shared=yes."
            )
        claims.append((canonical, rule, shared))

    for rt in ["exact_literal", "exact_normalized", "subsidiary_of"]:
        for _, r in active[active.rule_type == rt].iterrows():
            if rt == "exact_literal":
                hits = uniq.loc[uniq["name"] == r.pattern, "name"]
            elif rt == "exact_normalized":
                hits = uniq.loc[uniq["norm"] == r.pattern, "name"]
            else:  # subsidiary_of
                hits = uniq.loc[uniq["parent_norm"] == r.pattern, "name"]
            for lit in hits:
                _assign(lit, r.canonical, rt, r.shared)

    matched = pd.DataFrame(
        [(lit, canon, rule, shared)
         for lit, claims in lit_map.items()
         for canon, rule, shared in claims],
        columns=["name", "canonical", "match_rule", "shared"],
    )
    if matched.empty:
        return pd.DataFrame(columns=[
            "nct_id", "canonical", "entity", "role", "literal_name",
            "agency_class", "match_rule", "shared"])
    matched["entity"] = matched["name"].map(parsed_entity)

    out = sp.merge(matched, on="name", how="inner")
    out = out.rename(columns={"lead_or_collaborator": "role", "name": "literal_name"})
    out = (
        out[["nct_id", "canonical", "entity", "role", "literal_name",
             "agency_class", "match_rule", "shared"]]
        .drop_duplicates(subset=["nct_id", "canonical", "entity", "role"])
        .reset_index(drop=True)
    )
    return out


def attach_acquisition_dates(index: pd.DataFrame,
                             acquisitions: pd.DataFrame) -> pd.DataFrame:
    """Left-join acquisition effective dates onto the index by
    (entity, canonical). Rows without a dated acquisition get NaT.
    Use with a trial start-date column to split an entity's trials into
    pre- and post-acquisition eras."""
    # Amendment A6: no pre/post-acquisition view until dates carry a primary
    # source. Refuse outright while every effective_date is blank.
    if acquisitions["effective_date"].isna().all():
        src = acquisitions.attrs.get("source_path", "acquisitions.csv")
        raise ValueError(
            f"every effective_date in {src} is blank; add dates with a primary "
            "source before attaching acquisition dates"
        )
    acq = acquisitions.copy()
    acq["entity"] = acq["entity"].map(normalize)
    keep = acq[["entity", "canonical", "effective_date"]].drop_duplicates()
    return index.merge(keep, on=["entity", "canonical"], how="left")


# --------------------------------------------------------------------------- #
# 4. Filtering: any involvement -> lead -> collaborator                       #
# --------------------------------------------------------------------------- #

def trials(index: pd.DataFrame, company: str, role: str = "any",
           view: str = "current_owner") -> pd.DataFrame:
    """Trials touching `company` in the given scope.

    role='any'          lead OR collaborator (default, broadest)
    role='lead'         lead sponsor only
    role='collaborator' collaborator only

    view='current_owner'  `company` is a canonical label ("Pfizer"); trials
                          by acquired entities roll up to it.
    view='as_registered'  `company` is a normalized entity name ("wyeth");
                          trials stay with the registered identity.

    Returns one row per trial: nct_id, roles (comma-joined), is_lead,
    is_collaborator.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    if view not in VALID_VIEWS:
        raise ValueError(f"view must be one of {sorted(VALID_VIEWS)}")
    key = "canonical" if view == "current_owner" else "entity"
    target = company if view == "current_owner" else normalize(company)
    sub = index[index[key] == target]
    if role != "any":
        sub = sub[sub.role == role]
    if sub.empty:
        return pd.DataFrame(columns=["nct_id", "roles", "is_lead", "is_collaborator"])
    g = sub.groupby("nct_id")["role"].agg(lambda r: ",".join(sorted(set(r))))
    out = g.reset_index().rename(columns={"role": "roles"})
    out["is_lead"] = out.roles.str.contains("lead")
    out["is_collaborator"] = out.roles.str.contains("collaborator")
    return out


def entities(index: pd.DataFrame, company: str) -> pd.DataFrame:
    """Constituent as-registered entities of a canonical company, with trial
    counts by role. This is the before/after-acquisition entry point: pick an
    entity, then compare its trials against acquisition effective_date."""
    sub = index[index.canonical == company]
    if sub.empty:
        return pd.DataFrame(columns=["entity", "n_trials", "n_lead", "n_collab"])
    n_any = sub.groupby("entity").nct_id.nunique().rename("n_trials")
    n_lead = (sub[sub.role == "lead"].groupby("entity").nct_id
              .nunique().rename("n_lead"))
    n_collab = (sub[sub.role == "collaborator"].groupby("entity").nct_id
                .nunique().rename("n_collab"))
    g = (
        pd.concat([n_any, n_lead, n_collab], axis=1)
        .fillna(0).astype(int)
        .reset_index()
        .sort_values("n_trials", ascending=False)
    )
    return g


def company_flags(index: pd.DataFrame, companies: list[str]) -> pd.DataFrame:
    """Wide boolean table for merging onto an analysis dataset.

    One row per nct_id, columns <slug>_any / <slug>_lead / <slug>_collab
    for each requested company (current-owner view).
    """
    frames = []
    for comp in companies:
        slug = re.sub(r"\W+", "_", comp.lower()).strip("_")
        t = trials(index, comp)  # any involvement
        if t.empty:
            continue
        f = t[["nct_id"]].copy()
        f[f"{slug}_any"] = True
        f[f"{slug}_lead"] = t["is_lead"].values
        f[f"{slug}_collab"] = t["is_collaborator"].values
        frames.append(f.set_index("nct_id"))
    if not frames:
        return pd.DataFrame(columns=["nct_id"])
    wide = pd.concat(frames, axis=1)
    wide = wide.astype("boolean").fillna(False).astype(bool).reset_index()
    return wide


# --------------------------------------------------------------------------- #
# 5. Audit: the three review states, per company                              #
# --------------------------------------------------------------------------- #

def _rule_hits(uniq: pd.DataFrame, r: pd.Series) -> pd.Series:
    if r.rule_type == "exact_literal":
        return uniq.loc[uniq["name"] == r.pattern, "name"]
    if r.rule_type == "exact_normalized":
        return uniq.loc[uniq["norm"] == r.pattern, "name"]
    return uniq.loc[uniq["parent_norm"] == r.pattern, "name"]


def audit(sponsors: pd.DataFrame, rules: pd.DataFrame, company: str,
          stem: str) -> dict:
    """Coverage review for one company: the three review states.

    Returns {'attributed': DataFrame, 'reviewed_excluded': DataFrame,
             'unreviewed_candidates': DataFrame}.
    `attributed`: literal names mapped to `company`, with trial counts.
    `reviewed_excluded`: literals matched by a status=exclude rule (any
        company) that contain `stem`; a human looked and said no.
    `unreviewed_candidates`: literals containing `stem` in NO rule's reach.
        These need a decision: attribute, exclude with a note, or leave for
        the weekly queue. Never treat these as excluded.
    """
    index = build_index(sponsors, rules)
    uniq = pd.DataFrame({"name": sponsors["name"].dropna().unique()})
    uniq["norm"] = uniq["name"].map(normalize)
    uniq["parent_norm"] = uniq["name"].map(parsed_parent)

    attributed_lits = set(index.loc[index.canonical == company, "literal_name"])
    all_attributed = set(index["literal_name"])
    excluded_lits: set[str] = set()
    for _, r in rules[rules.status == "exclude"].iterrows():
        excluded_lits.update(_rule_hits(uniq, r))

    def _summarize(names: set[str]) -> pd.DataFrame:
        subs = sponsors[sponsors.name.isin(names)]
        if subs.empty:
            return pd.DataFrame(columns=["name", "n_trials", "roles", "agency_class"])
        return (
            subs.groupby("name")
            .agg(n_trials=("nct_id", "nunique"),
                 roles=("lead_or_collaborator", lambda r: ",".join(sorted(set(r)))),
                 agency_class=("agency_class", "first"))
            .reset_index()
            .sort_values("n_trials", ascending=False)
        )

    stem_mask = uniq.name.str.contains(stem, case=False, na=False)
    stem_names = set(uniq.loc[stem_mask, "name"])
    unreviewed = stem_names - all_attributed - excluded_lits

    return {
        "attributed": _summarize(attributed_lits),
        "reviewed_excluded": _summarize(stem_names & excluded_lits),
        "unreviewed_candidates": _summarize(unreviewed),
    }
