#!/usr/bin/env python3
"""
Generate data/industry_sponsors.json for the gated Industry Sponsor
Representation view (#industry).

Rebuilds, against this repository's own weekly ClinicalTrials.gov extraction,
the industry-sponsor female-enrollment analyses from the standalone R pipeline
(sex_gender_sponsor_models_standalone v8 + parsers.R):

  Cohort (mirrors the Rmd's Option 1 / Objective 2 chain as closely as this
  dataset allows):
    - interventional studies with posted results,
    - primary completion date on/after 2009-01-01 (FDAAA window),
    - not terminated,
    - sex reported with BOTH female and male counts > 0. The R pipeline
      restricts to eligibility "All" (mixed-sex) trials; this dataset does not
      carry the eligibility-sex field, so requiring both counts positive is
      the auditable proxy for excluding single-sex-eligibility trials.
    - percent female = 100 * female / (female + male). Explicit "unknown"
      counts are reported data and are kept OUT of the denominator and never
      folded into missing (the parsers.R contract). Non-participant unit
      tables are mitigated upstream: the extractor only converts percentage
      measures using participant-unit denominators (src/utils.py
      get_measure_denom), the analogue of restrict_to_participant_counts.

  Sponsor assignment (AACT sponsorCollaboratorsModule contract: leadSponsor ->
  role "lead", collaborators -> role "collaborator", each with class + name):
    - the lead sponsor's company when the lead is INDUSTRY,
    - else the alphabetically-first INDUSTRY collaborator (the Rmd's
      lead-first, min(company) fallback).
  Company canonicalization ports the Rmd's sponsor_recode tribble (first
  pattern wins, case-insensitive) with its legal-suffix stripper as the
  fallback. The AACT fork defines the field-extraction contract above but
  carries no company-grouping rules, so there is nothing to override.

  Outputs:
    - compact per-trial rows so the frontend can recompute the heatmap and
      time-trend medians under the dashboard's global filters,
    - the top-10 sponsor list (by cohort volume) + "Other Industry",
    - the heatmap's condition axis (top secondary categories by volume,
      excluding sex-specific categories, mirroring the Rmd's exclusion of
      sex-specific conditions),
    - per-sponsor adjusted contrasts vs Other Industry: OLS of percent female
      on a sponsor indicator holding phase, log enrollment, completion year
      (centered at 2009), country count, and seven therapeutic-area flags
      fixed - the Rmd's Section 12 two-group models - plus the pooled
      reference fit's n and R-squared.

Run from the repository root: python3 scripts/generate_industry_sponsors.py
Optionally pass a directory containing demographics.part*.json.gz.
"""
import glob
import gzip
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime

import numpy as np

MIN_CELL = 10          # heatmap cells below this many trials are uncolored (Rmd ppr_min_cell)
TOP_N_SPONSORS = 10
PCD_CUTOFF = date(2009, 1, 1)

# ── Company canonicalization (ported from the Rmd's sponsor_recode tribble; ──
# first match wins, case-insensitive).
SPONSOR_RECODE = [
    (r"upjohn.*mylan.*viatris|^viatris", "Viatris"),
    (r"merck kgaa|merck serono|merck healthcare kgaa|^emd serono", "Merck KGaA"),
    (r"^organon", "Organon"),
    (r"mead johnson", "Mead Johnson"),
    (r"wyeth|hospira|array ?biopharma|king pharmaceuticals|^pharmacia|medivation|anacor|^pfizer|seagen", "Pfizer"),
    (r"glaxo|smithkline|^gsk\b|stiefel.*gsk|human genome sciences.*gsk|sirtris.*gsk", "GlaxoSmithKline"),
    (r"hoffmann-la roche|^roche\b|roche pharma|roche-genentech|roche diagnostics|roche farma", "Roche"),
    (r"merck sharp|^msd\b|schering-plough|subsidiary of merck & co|merck frosst|acceleron pharma", "Merck Sharp & Dohme"),
    (r"^sanofi|sanofi-synthelabo|aventis|genzyme|bioverativ|^ablynx|principia biopharma", "Sanofi"),
    (r"bristol-?myers|^celgene|juno therapeutics.*(bristol|celgene)|karuna.*bristol|adnexus.*bristol", "Bristol-Myers Squibb"),
    (r"^astrazeneca|medimmune|^alexion", "AstraZeneca"),
    (r"^abbvie|pharmacyclics|abbvie \(prior sponsor", "AbbVie"),
    (r"^abbott", "Abbott"),
    (r"^novartis", "Novartis"),
    (r"^janssen|johnson & johnson", "Johnson & Johnson"),
    (r"^boehringer", "Boehringer Ingelheim"),
    (r"eli lilly|^lilly\b", "Eli Lilly and Company"),
    (r"novo nordisk", "Novo Nordisk"),
    (r"^takeda", "Takeda"),
    (r"^amgen", "Amgen"),
    (r"^bayer", "Bayer"),
    (r"gilead", "Gilead Sciences"),
    (r"^biogen", "Biogen"),
    (r"^astellas", "Astellas Pharma"),
]
_RECODE_COMPILED = [(re.compile(p, re.IGNORECASE), c) for p, c in SPONSOR_RECODE]

_SUFFIX_RE = re.compile(
    r"[ ,]*(inc|corp|corporation|co|company|ltd|limited|llc|l\.l\.c|lp|l\.p|"
    r"plc|gmbh|ag|s\.?a|s\.?p\.?a|a/s|n\.?v|ulc|k\.k|s\.r\.o|sas|"
    r"pharmaceuticals?|pharma)\.?$", re.IGNORECASE)


def strip_legal_suffix(name):
    x = " ".join(name.split())
    for _ in range(4):
        x = _SUFFIX_RE.sub("", x).strip()
    return " ".join(x.split())


def canon_sponsor(name):
    raw = " ".join((name or "").split())
    for rx, canonical in _RECODE_COMPILED:
        if rx.search(raw):
            return canonical
    return strip_legal_suffix(raw)


def phase_clean(p):
    z = re.sub(r"[_\s|,/]+", "", (p or "").lower())
    if "earlyphase1" in z:
        return "Phase 1"
    if z == "phase1":
        return "Phase 1"
    if "phase1phase2" in z:
        return "Phase 1/2"
    if z == "phase2":
        return "Phase 2"
    if "phase2phase3" in z:
        return "Phase 2/3"
    if z == "phase3":
        return "Phase 3"
    if z == "phase4":
        return "Phase 4"
    return "N/A or other"


PHASE_LEVELS = ["Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "N/A or other"]

# Therapeutic-area flags from the dashboard's own primary condition category
# (the Rmd derives the same seven flags from MeSH terms).
AREA_BY_PRIMARY = {
    "Oncology": "is_oncology",
    "Cardiovascular": "is_cardio",
    "Mental Health": "is_mental_health",
    "Infectious Disease": "is_infectious",
    "Endocrine and Metabolic": "is_metabolic",
    "Neurology": "is_neuro",
    "Respiratory": "is_respiratory",
}
AREA_VARS = list(AREA_BY_PRIMARY.values())

# Sex-specific secondary categories excluded from the heatmap's condition axis
# (the Rmd excludes sex-specific conditions so single-sex case mix cannot
# masquerade as a sponsor effect).
SEX_SPECIFIC_SECONDARIES = {
    "Breast Cancer", "Prostate Cancer", "Infertility",
    "Pregnancy Complications", "Menopause and Hormonal",
}
EXCLUDED_SECONDARIES = SEX_SPECIFIC_SECONDARIES | {"Uncategorized", "Other", ""}


# Race/ethnicity category order shared with the frontend (indices into the
# per-trial share arrays). Shares are % of the trial's explicitly reported
# categories; "unknown_not_reported" stays out of every denominator per the
# parsers.R contract.
RACE_CATS = ["white", "black_african_american", "asian",
             "american_indian_alaska_native", "native_hawaiian_pacific_islander",
             "more_than_one_race", "other"]
ETH_CATS = ["hispanic_latino", "not_hispanic_latino"]


def demo_shares(obj, cats):
    """[share%...] of reported categories for one trial, or None."""
    if not obj or not obj.get("reported"):
        return None
    totals = obj.get("omb_totals") or {}
    vals = [max(0, totals.get(c) or 0) for c in cats]
    known = sum(vals)
    if known <= 0:
        return None
    return [round(100.0 * v / known, 1) for v in vals]


def parse_iso_date(s):
    if not s:
        return None
    s = str(s)
    for fmt, ln in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(s[:ln], fmt).date()
        except ValueError:
            continue
    return None


def assign_industry_company(rec):
    """Returns (company, via_lead) or (None, None).

    AACT sponsorCollaboratorsModule contract: the lead sponsor's company when
    the lead is INDUSTRY (via_lead True), else the alphabetically-first
    INDUSTRY collaborator (via_lead False - the frontend's "Lead Sponsor
    Only" role toggle drops these trials).
    """
    if (rec.get("sponsor_class") or "").upper() == "INDUSTRY" and rec.get("lead_sponsor_name"):
        return canon_sponsor(rec["lead_sponsor_name"]), True
    collabs = [c.get("name") for c in (rec.get("collaborators") or [])
               if (c.get("class") or "").upper() == "INDUSTRY" and c.get("name")]
    if collabs:
        return min(canon_sponsor(n) for n in collabs), False
    return None, None


def load_parts(data_dir):
    parts = sorted(glob.glob(os.path.join(data_dir, "demographics.part*.json.gz")))
    if not parts:
        raise SystemExit(f"No demographics.part*.json.gz under {data_dir}")
    extracted_at, records = None, []
    for p in parts:
        with gzip.open(p, "rt") as f:
            container = json.load(f)
        extracted_at = extracted_at or container.get("extracted_at")
        records.extend(container["data"])
        print(f"  read {os.path.basename(p)}: {len(container['data']):,} records")
    return extracted_at, records


def build_cohort(records):
    rows = []
    for r in records:
        if (r.get("study_type") or "").upper() != "INTERVENTIONAL":
            continue
        if (r.get("status") or "").upper() == "TERMINATED":
            continue
        pcd = parse_iso_date(r.get("primary_completion_date") or r.get("completion_date"))
        if pcd is None or pcd < PCD_CUTOFF:
            continue
        sex = r.get("sex") or {}
        totals = sex.get("totals") or {}
        f, m = totals.get("female") or 0, totals.get("male") or 0
        # Mixed-sex proxy for the Sex tier: both counts positive. Trials
        # without usable sex data stay in the cohort for the Race/Ethnicity
        # tiers (pf = None), so those tiers are no longer scaffolded on the
        # sex-reporting requirement (single-sex trials included).
        pf = 100.0 * f / (f + m) if (sex.get("reported") and f > 0 and m > 0) else None
        company, via_lead = assign_industry_company(r)
        if not company:
            continue
        results = parse_iso_date(r.get("results_date"))
        enrollment = r.get("enrollment")
        race_shares = demo_shares(r.get("race"), RACE_CATS)
        eth_shares = demo_shares(r.get("ethnicity"), ETH_CATS)
        if pf is None and race_shares is None and eth_shares is None:
            continue
        rows.append({
            "company": company,
            "via_lead": bool(via_lead),
            "pf": pf,
            "results_year": results.year if results else None,
            "pcd_year": pcd.year,
            "primary": r.get("primary_condition") or "",
            "secondary": r.get("secondary_condition") or "",
            "phase": phase_clean(r.get("phase")),
            "log_enroll": math.log(enrollment) if isinstance(enrollment, (int, float)) and enrollment and enrollment > 0 else None,
            "n_countries": len({c.get("country") for c in (r.get("countries") or []) if c.get("country")}),
            "has_unknown": 1 if (totals.get("unknown") or 0) > 0 else 0,
            "race_shares": race_shares,
            "eth_shares": eth_shares,
        })
    return rows


def fit_contrast_set(rows, top10):
    """Per-sponsor adjusted contrasts + the pooled reference fit for a row set."""
    contrasts = [c for c in (ols_contrast(rows, sp) for sp in top10) if c]
    return contrasts, pooled_fit(rows, top10)


def ols_contrast(rows, sponsor):
    """Two-group OLS: `sponsor` vs Other Industry (Rmd Section 12).

    percent_female ~ vs_other + phase dummies + pcd_year_c + log_enrollment +
    n_countries + area flags. Returns beta/lo/hi/p/n for the sponsor indicator.
    Normal-approximation p/CI (cohort n is in the thousands).
    """
    sub = [r for r in rows if r["bucket"] in (sponsor, "Other Industry") and r["log_enroll"] is not None]
    if len(sub) < 50:
        return None
    y = np.array([r["pf"] for r in sub])
    cols, names = [np.ones(len(sub))], ["intercept"]
    cols.append(np.array([1.0 if r["bucket"] == sponsor else 0.0 for r in sub])); names.append("vs_other")
    for lvl in PHASE_LEVELS[1:]:  # Phase 1 = reference
        v = np.array([1.0 if r["phase"] == lvl else 0.0 for r in sub])
        if v.std() > 0:
            cols.append(v); names.append(f"phase:{lvl}")
    for name, arr in (
        ("pcd_year_c", np.array([float(r["pcd_year"] - 2009) for r in sub])),
        ("log_enrollment", np.array([r["log_enroll"] for r in sub])),
        ("n_countries", np.array([float(r["n_countries"]) for r in sub])),
    ):
        if arr.std() > 0:
            cols.append(arr); names.append(name)
    for area, flag in AREA_BY_PRIMARY.items():
        v = np.array([1.0 if r["primary"] == area else 0.0 for r in sub])
        if v.std() > 0:
            cols.append(v); names.append(flag)
    X = np.column_stack(cols)
    beta, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(sub) - rank
    s2 = float(resid @ resid) / dof
    xtx_inv = np.linalg.pinv(X.T @ X)
    i = names.index("vs_other")
    se = math.sqrt(s2 * xtx_inv[i, i])
    b = float(beta[i])
    z = b / se if se > 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"sponsor": sponsor, "n": len(sub), "beta": round(b, 2),
            "lo": round(b - 1.96 * se, 2), "hi": round(b + 1.96 * se, 2),
            "p": round(p, 4)}


def pooled_fit(rows, top10):
    sub = [r for r in rows if r["log_enroll"] is not None]
    y = np.array([r["pf"] for r in sub])
    cols, names = [np.ones(len(sub))], ["intercept"]
    for sp in top10:  # Other Industry = reference
        cols.append(np.array([1.0 if r["bucket"] == sp else 0.0 for r in sub])); names.append(sp)
    for lvl in PHASE_LEVELS[1:]:
        v = np.array([1.0 if r["phase"] == lvl else 0.0 for r in sub])
        if v.std() > 0:
            cols.append(v); names.append(f"phase:{lvl}")
    cols.append(np.array([float(r["pcd_year"] - 2009) for r in sub])); names.append("pcd_year_c")
    cols.append(np.array([r["log_enroll"] for r in sub])); names.append("log_enrollment")
    cols.append(np.array([float(r["n_countries"]) for r in sub])); names.append("n_countries")
    for area in AREA_BY_PRIMARY:
        v = np.array([1.0 if r["primary"] == area else 0.0 for r in sub])
        if v.std() > 0:
            cols.append(v); names.append(area)
    X = np.column_stack(cols)
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot
    k = X.shape[1] - 1
    adj = 1 - (1 - r2) * (len(sub) - 1) / (len(sub) - k - 1)
    return {"n": len(sub), "r2": round(r2, 4), "adj_r2": round(adj, 4)}


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    print(f"Loading parts from {data_dir}/ ...")
    extracted_at, records = load_parts(data_dir)
    print(f"Loaded {len(records):,} studies")

    rows = build_cohort(records)
    print(f"Industry mixed-sex, sex-reporting cohort: {len(rows):,} trials")

    # ALL industry sponsors, sorted by eligible-cohort trial volume (ties
    # alphabetical). The first TOP_N_SPONSORS entries are the named-sponsor
    # set the adjusted models use; everything below them pools into the
    # frontend's derived "Other Industry" bucket (company index >= 10).
    counts = defaultdict(int)
    for r in rows:
        counts[r["company"]] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    companies = [c for c, _ in ranked]
    company_n = [n for _, n in ranked]
    top10 = companies[:TOP_N_SPONSORS]
    for r in rows:
        r["bucket"] = r["company"] if r["company"] in top10 else "Other Industry"
    print(f"Industry sponsors after canonical grouping: {len(companies):,}")
    print("Top 10 industry sponsors:", top10)

    # Heatmap condition axis: top secondary categories by cohort volume,
    # excluding sex-specific / catch-all categories.
    sec_counts = defaultdict(int)
    for r in rows:
        if r["secondary"] not in EXCLUDED_SECONDARIES:
            sec_counts[r["secondary"]] += 1
    heatmap_conditions = [c for c, _ in sorted(sec_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]
    sec_counts_all = defaultdict(int)
    for r in rows:
        if r["secondary"] not in {"Uncategorized", "Other", ""}:
            sec_counts_all[r["secondary"]] += 1
    heatmap_conditions_all = [c for c, _ in sorted(sec_counts_all.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]

    # Two role modes for the frontend's Role toggle: the full lead-or-
    # collaborator assignment (default), and the lead-sponsor-only subset.
    sex_rows = [r for r in rows if r["pf"] is not None]
    contrasts, pooled = fit_contrast_set(sex_rows, top10)
    lead_rows = [r for r in rows if r["via_lead"]]
    contrasts_lead, pooled_lead = fit_contrast_set([r for r in lead_rows if r["pf"] is not None], top10)
    print(f"Sex-reporting (mixed-sex) trials: {len(sex_rows):,} of {len(rows):,} cohort")

    # Race/ethnicity "balance" contrasts: the selected category's share of
    # (category + White) - the analogue of female/(female+male) - modeled the
    # same way (two-group OLS vs Other Industry, same covariates), for both
    # role modes. White itself uses its plain share of reported participants.
    def fit_balance(value_fn):
        out = {}
        for mode, base in (("any", rows), ("lead", lead_rows)):
            rx = [dict(r, pf=v) for r in base if (v := value_fn(r)) is not None]
            out[mode] = fit_contrast_set(rx, top10)[0] if len(rx) >= 200 else []
        return out

    def race_balance(i):
        def fn(r):
            s = r["race_shares"]
            if s is None:
                return None
            if i == 0:
                return s[0]                     # White: plain share of reported
            denom = s[i] + s[0]
            return 100.0 * s[i] / denom if denom > 0 else None
        return fn

    def eth_balance(i):
        def fn(r):
            s = r["eth_shares"]
            if s is None:
                return None
            denom = s[0] + s[1]
            return 100.0 * s[i] / denom if denom > 0 else None
        return fn

    contrasts_demo = {
        "race": {c: fit_balance(race_balance(i)) for i, c in enumerate(RACE_CATS)},
        "ethnicity": {c: fit_balance(eth_balance(i)) for i, c in enumerate(ETH_CATS)},
    }
    n_race = sum(1 for r in rows if r["race_shares"] is not None)
    n_eth = sum(1 for r in rows if r["eth_shares"] is not None)
    print(f"Race reported in {n_race:,} cohort trials; ethnicity in {n_eth:,}")
    print(f"Adjusted contrasts fit for {len(contrasts)} sponsors; pooled n={pooled['n']:,}, R2={pooled['r2']}")
    print(f"Lead-only mode: {len(lead_rows):,} trials; pooled n={pooled_lead['n']:,}, R2={pooled_lead['r2']}")

    company_idx = {c: i for i, c in enumerate(companies)}
    primaries = sorted({r["primary"] for r in rows})
    secondaries = sorted({r["secondary"] for r in rows})
    p_idx = {p: i for i, p in enumerate(primaries)}
    s_idx = {s: i for i, s in enumerate(secondaries)}

    # Compact per-trial rows: [company_idx, pf(1dp), results_year, pcd_year,
    # primary_idx, secondary_idx, has_explicit_unknown, via_lead]. company_idx
    # points at the REAL company in the volume-ranked companies list; indices
    # >= TOP_N_SPONSORS pool into "Other Industry" on the frontend.
    trials = [[company_idx[r["company"]], (round(r["pf"], 1) if r["pf"] is not None else None), r["results_year"] or 0,
               r["pcd_year"], p_idx[r["primary"]], s_idx[r["secondary"]], r["has_unknown"],
               1 if r["via_lead"] else 0, r["race_shares"], r["eth_shares"]]
              for r in rows]

    out = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_extracted_at": extracted_at,
        "cohort_n": len(rows),
        "min_cell": MIN_CELL,
        "companies": companies,
        "company_n": company_n,
        "top_n": TOP_N_SPONSORS,
        "primaries": primaries,
        "secondaries": secondaries,
        "heatmap_conditions": heatmap_conditions,
        "contrasts": contrasts,
        "pooled": pooled,
        "contrasts_lead": contrasts_lead,
        "pooled_lead": pooled_lead,
        "contrasts_demo": contrasts_demo,
        "race_categories": RACE_CATS,
        "eth_categories": ETH_CATS,
        # Disease-prevalence benchmarks by race/ethnicity are not yet
        # integrated; the heatmap stays on the cohort's own condition
        # baselines until these are populated (e.g. from PCORI prevalences).
        "prevalence_benchmarks": {"status": "pending", "by_sex": None, "by_race": None, "by_ethnicity": None},
        # Population-share benchmarks (2020 U.S. Census: race alone; Hispanic
        # origin) for the "Census share" benchmark option. These answer "does
        # enrollment mirror the population?" - the pending disease-prevalence
        # benchmarks will answer "does it mirror the patients?".
        "census": {
            "source": "2020 U.S. Census (race alone; Hispanic origin)",
            "race": {"white": 61.6, "black_african_american": 12.4, "asian": 6.0,
                      "american_indian_alaska_native": 1.1,
                      "native_hawaiian_pacific_islander": 0.2,
                      "more_than_one_race": 10.2, "other": 8.4},
            "ethnicity": {"hispanic_latino": 18.7, "not_hispanic_latino": 81.3},
        },
        "sex_specific_conditions": sorted(SEX_SPECIFIC_SECONDARIES),
        "heatmap_conditions_all": heatmap_conditions_all,
        "trial_fields": ["company", "pf", "results_year", "pcd_year", "primary", "secondary", "has_explicit_unknown", "via_lead", "race_shares", "eth_shares"],
        "trials": trials,
    }
    out_path = os.path.join(data_dir, "industry_sponsors.json")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {os.path.getsize(out_path) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
