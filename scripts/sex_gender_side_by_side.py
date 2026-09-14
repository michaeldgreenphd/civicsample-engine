#!/usr/bin/env python3
"""Old engine vs new parser on ONE pull: the cutover side-by-side.

READS   data/demographics.json (one pull carrying both study["sex"]/["gender"]
        from the legacy extractors and study["sex_gender"] from the parser).
        Nothing here touches history: the retained snapshots hold no raw tables.
WRITES  --out-dir (default data/sex_gender_audit/): side_by_side.md (the table
        for review) and side_by_side.json (the same numbers; the methods text
        reads sex_unknown_inferred_share_pct from it).
INVOKED by hand before cutover, and by the report-back. Run from the repo root:
        python3 scripts/sex_gender_side_by_side.py

What it measures (plan amendment 4):
  - reported-gender count old vs new, and the flips (old gender to sex-only,
    newly reported gender);
  - sex reporting old vs new, with the five-state table;
  - sex bucket totals old vs new, with the legacy inferred remainder shown as
    its own line (it was displayed as Unknown; it is never unknown here);
  - the rows the new is_participant_count flag removes from the sex donut and
    from the industry tab's cohort, with their participant volume, split by
    paramType / unit so the COUNT_OF_UNITS, mean/median and percent classes are
    each visible (README D6);
  - percent female by results-posted year: the old rule and both new series.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from src import sex_gender_table as sgt  # noqa: E402
from src.utils import pipeline_commit  # noqa: E402
from build_sex_gender_table import load_records  # noqa: E402
import generate_industry_sponsors as gis  # noqa: E402

MEAN_LIKE = {"MEAN", "MEDIAN", "GEOMETRIC_MEAN", "LEAST_SQUARES_MEAN", "GEOMETRIC_LEAST_SQUARES_MEAN", "LOG_MEAN"}


def unit_class(row: dict) -> str:
    """Diagnostic class of the count-driving measure's units. Independent of the
    parser's own flag so the D6 classes are visible whatever version is vendored."""
    pt = (row.get("param_type") or "").upper()
    um = (row.get("unit_of_measure") or "").lower()
    if pt == "COUNT_OF_UNITS":
        return "count_of_units"
    if pt in MEAN_LIKE:
        return "mean_or_median"
    if "%" in um or "percent" in um or pt in ("PERCENTAGE", "PERCENT"):
        return "percent_like"
    return "participant_count_like"


def industry_cohort_member(rec: dict) -> bool:
    """The industry tab's Sex-tier cohort predicate as generate_industry_sponsors
    applies it (interventional, not terminated, PCD >= 2009, an attributed
    industry company, both legacy sex counts positive)."""
    if (rec.get("study_type") or "").upper() != "INTERVENTIONAL":
        return False
    if (rec.get("status") or "").upper() == "TERMINATED":
        return False
    pcd = gis.parse_iso_date(rec.get("primary_completion_date") or rec.get("completion_date"))
    if pcd is None or pcd < gis.PCD_CUTOFF:
        return False
    sex = rec.get("sex") or {}
    t = sex.get("totals") or {}
    if not (sex.get("reported") and (t.get("female") or 0) > 0 and (t.get("male") or 0) > 0):
        return False
    company, _ = gis.assign_industry_company(rec)
    return bool(company)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--out-dir", default="data/sex_gender_audit")
    a = ap.parse_args()

    records, extracted_at, commit = load_records(a.demographics, a.parts)
    recs = [r for r in records if r.get("sex_gender")]
    n = len(recs)
    legacy_present = any(r.get("sex") is not None for r in recs)
    if not legacy_present:
        raise SystemExit("this pull has no legacy sex/gender keys; run the extraction with the legacy extractors on")

    def sg(r):
        return r["sex_gender"]

    # ── A. Reported gender ──
    old_g = [r for r in recs if (r.get("gender") or {}).get("reported")]
    new_g = [r for r in recs if sg(r).get("reported_gender") is True]
    old_g_ids = {r["nct_id"] for r in old_g}
    new_g_ids = {r["nct_id"] for r in new_g}
    flip_to_sex_only = [r for r in old_g if r["nct_id"] not in new_g_ids and sg(r).get("reported_sex") is True]
    flip_to_other = [r for r in old_g if r["nct_id"] not in new_g_ids and sg(r).get("reported_sex") is not True]
    newly_gender = [r for r in new_g if r["nct_id"] not in old_g_ids]
    glb = sum(1 for r in recs if sg(r).get("gender_labeled_binary_only") is True)
    old_gender_cats = defaultdict(float)
    for r in old_g:
        for k, v in ((r.get("gender") or {}).get("totals") or {}).items():
            old_gender_cats[k] += v or 0
    old_gender_inferred = sum((r.get("gender") or {}).get("unknown_inferred") or 0 for r in old_g)

    # ── B. Sex reporting and the five states ──
    old_s = [r for r in recs if (r.get("sex") or {}).get("reported")]
    old_s_ids = {r["nct_id"] for r in old_s}
    sc = sgt.status_counts(sg(r) for r in recs)
    cross = defaultdict(int)
    for r in recs:
        cross[("old_reported" if r["nct_id"] in old_s_ids else "old_not_reported", sg(r)["sex_report_status"])] += 1
    new_sex_from_gender_titles = sum(1 for r in recs if r["nct_id"] not in old_s_ids and sg(r).get("reported_sex") is True
                                     and sg(r).get("gender_labeled_binary_only") is True)

    # ── C. Sex bucket totals ──
    old_tot = {"female": 0.0, "male": 0.0, "unknown_explicit": 0.0, "unknown_inferred": 0.0}
    for r in old_s:
        s = r["sex"]
        t = s.get("totals") or {}
        old_tot["female"] += t.get("female") or 0
        old_tot["male"] += t.get("male") or 0
        old_tot["unknown_explicit"] += s.get("unknown_explicit") or 0
        old_tot["unknown_inferred"] += s.get("unknown_inferred") or 0
    old_unknown_total = old_tot["unknown_explicit"] + old_tot["unknown_inferred"]
    inferred_share = (100.0 * old_tot["unknown_inferred"] / old_unknown_total) if old_unknown_total else None
    denom = [r for r in recs if sg(r).get("reported_sex") is True and sg(r).get("is_participant_count")]
    new_tot = {k: sum((sg(r).get(c) or 0) for r in denom) for k, c in
               (("female", "n_female"), ("male", "n_male"), ("explicit_unknown", "n_unknown"),
                ("gender_diverse", "n_gender_diverse"), ("ambiguous", "n_ambiguous_gender"))}
    reported_rows = [r for r in recs if sg(r)["sex_report_status"] == "reported"]
    emp = sum((sg(r).get("enrollment_minus_parsed") or 0) for r in reported_rows if sg(r).get("enrollment_minus_parsed") is not None)
    emp_pos = sum((sg(r).get("enrollment_minus_parsed") or 0) for r in reported_rows if (sg(r).get("enrollment_minus_parsed") or 0) > 0)

    # ── D. Removed from the sex donut and the industry tab by is_participant_count ──
    removed = [r for r in recs if sg(r).get("reported_sex") is True and not sg(r).get("is_participant_count")]
    by_class = defaultdict(lambda: {"trials": 0, "female": 0.0, "male": 0.0, "flagged_by_parser": 0})
    for r in recs:
        row = sg(r)
        if row.get("reported_sex") is not True:
            continue
        c = unit_class(row)
        d = by_class[c]
        d["trials"] += 1
        d["female"] += row.get("n_female") or 0
        d["male"] += row.get("n_male") or 0
        if not row.get("is_participant_count"):
            d["flagged_by_parser"] += 1
    industry = [r for r in recs if industry_cohort_member(r)]
    ind_by_class = defaultdict(lambda: {"trials": 0, "female_old": 0.0, "male_old": 0.0, "flagged_by_parser": 0})
    for r in industry:
        c = unit_class(sg(r))
        d = ind_by_class[c]
        d["trials"] += 1
        d["female_old"] += (r["sex"]["totals"].get("female") or 0)
        d["male_old"] += (r["sex"]["totals"].get("male") or 0)
        if not sg(r).get("is_participant_count"):
            d["flagged_by_parser"] += 1

    # ── E. Percent female by year ──
    years = defaultdict(lambda: {"old_sum": 0.0, "old_n": 0})
    for r in old_s:
        y = (r.get("results_date") or "")[:4]
        if not y:
            continue
        t = r["sex"]["totals"]
        tot = (t.get("female") or 0) + (t.get("male") or 0) + (t.get("unknown") or 0)
        if tot > 0:
            years[y]["old_sum"] += (t.get("female") or 0) / tot
            years[y]["old_n"] += 1
    for r in recs:
        sg(r)["year"] = (r.get("results_date") or "")[:4] or None
    new_series = sgt.percent_female_series(sg(r) for r in recs)
    for r in recs:
        sg(r).pop("year", None)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_commit": pipeline_commit() or commit,
        "source_extracted_at": extracted_at,
        "snapshot_date": sg(recs[0]).get("snapshot_date") if recs else None,
        "parser_rules_version": sgt.sgp.PARSER_RULES_VERSION,
        "parser_module_version": sgt.sgp.__version__,
        "n_trials": n,
        "reported_gender": {"old": len(old_g), "new": len(new_g),
                            "old_to_sex_only": len(flip_to_sex_only), "old_to_other_status": len(flip_to_other),
                            "newly_reported_gender": len(newly_gender), "gender_labeled_binary_only": glb,
                            "old_category_totals": dict(old_gender_cats), "old_unknown_inferred": old_gender_inferred,
                            "new_bucket_totals": {"gender_diverse": new_tot["gender_diverse"], "ambiguous": new_tot["ambiguous"]}},
        "reported_sex": {"old": len(old_s), "new": sc["outcomes"]["reported_sex"],
                         "new_reported_sex_from_gender_titled_binary_tables": new_sex_from_gender_titles,
                         "status": sc["status"], "outcomes": sc["outcomes"],
                         "cross": {f"{a}|{b}": v for (a, b), v in sorted(cross.items())}},
        "sex_totals": {"old": {**old_tot, "unknown_total_as_displayed": old_unknown_total},
                       "new_denominator_trials": len(denom), "new": new_tot,
                       "enrollment_minus_parsed_sum_reported_rows": emp,
                       "enrollment_minus_parsed_positive_sum": emp_pos},
        "sex_unknown_inferred_share_pct": inferred_share,
        "removed_by_is_participant_count": {"trials": len(removed),
                                            "female": sum((sg(r).get("n_female") or 0) for r in removed),
                                            "male": sum((sg(r).get("n_male") or 0) for r in removed),
                                            "by_unit_class_all_reported_sex": dict(by_class)},
        "industry_cohort": {"trials": len(industry), "by_unit_class": dict(ind_by_class)},
        "percent_female_by_year": {y: {"old_mean_incl_unknown": (years[y]["old_sum"] / years[y]["old_n"] * 100) if years[y]["old_n"] else None,
                                       "old_n": years[y]["old_n"],
                                       "new_a_mean": (new_series.get(y, {}).get("pf_sum", 0) / new_series[y]["pf_count"]) if new_series.get(y, {}).get("pf_count") else None,
                                       "new_b_weighted": (100 * new_series[y]["f_sum"] / new_series[y]["fm_sum"]) if new_series.get(y, {}).get("fm_sum") else None,
                                       "new_n": new_series.get(y, {}).get("pf_count", 0)}
                                   for y in sorted(set(years) | set(new_series))},
    }

    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "side_by_side.json"), "w") as fh:
        json.dump(out, fh, indent=2, default=str)

    L = []
    L.append(f"# Sex/gender side-by-side on one pull\n")
    L.append(f"Pull extracted {extracted_at}; snapshot {out['snapshot_date']}; {n:,} trials with results; "
             f"parser rules `{out['parser_rules_version']}` (module {out['parser_module_version']}); "
             f"pipeline commit `{out['pipeline_commit']}`.\n")
    L.append("## A. Reported gender\n")
    L.append("| Quantity | Old engine | New parser |\n|---|---|---|")
    L.append(f"| Trials reported gender | {len(old_g):,} | {len(new_g):,} |")
    L.append(f"| Old gender-reported that flip to sex-only (reported_sex, not reported_gender) | | {len(flip_to_sex_only):,} |")
    L.append(f"| Old gender-reported that end in another state (not reported_sex) | | {len(flip_to_other):,} |")
    L.append(f"| Newly reported gender (not gender-reported before) | | {len(newly_gender):,} |")
    L.append(f"| gender_labeled_binary_only (Gender-titled, Female/Male or Woman/Man only) | | {glb:,} |")
    L.append("\nOld gender category totals (participants, over old gender-reported trials) vs new buckets (over trials with reported_sex AND is_participant_count):\n")
    L.append("| Old category | Participants | New bucket | Participants |\n|---|---|---|---|")
    pairs = [("woman", "female (see C)"), ("man", "male (see C)"), ("nonbinary", "gender_diverse"), ("transgender", "ambiguous"), ("other", ""), ("unknown", "")]
    for ok, nk in pairs:
        nv = f"{new_tot[nk]:,.0f}" if nk in new_tot else ""
        L.append(f"| {ok} | {old_gender_cats.get(ok, 0):,.0f} | {nk} | {nv} |")
    L.append(f"| of old unknown, inferred by balancing | {old_gender_inferred:,.0f} | | |")
    L.append("\n## B. Sex reporting and the five states\n")
    L.append("| Quantity | Old engine | New parser |\n|---|---|---|")
    L.append(f"| Trials reported sex | {len(old_s):,} | {sc['outcomes']['reported_sex']:,} |")
    L.append(f"| of new reported_sex, previously not reported: Gender-titled binary tables | | {new_sex_from_gender_titles:,} |")
    for st, v in sc["status"].items():
        L.append(f"| status {st} | | {v:,} |")
    L.append("\nOld sex.reported by new status:\n")
    L.append("| Old | New status | Trials |\n|---|---|---|")
    for (o, s_), v in sorted(cross.items()):
        L.append(f"| {o} | {s_} | {v:,} |")
    L.append("\n## C. Sex bucket totals (participants)\n")
    L.append("| Line | Old engine (sex.reported trials) | New parser (reported_sex AND is_participant_count) |\n|---|---|---|")
    L.append(f"| Trials in the total | {len(old_s):,} | {len(denom):,} |")
    L.append(f"| Female | {old_tot['female']:,.0f} | {new_tot['female']:,.0f} |")
    L.append(f"| Male | {old_tot['male']:,.0f} | {new_tot['male']:,.0f} |")
    L.append(f"| Explicit Unknown (categories mapped to unknown) | {old_tot['unknown_explicit']:,.0f} | {new_tot['explicit_unknown']:,.0f} |")
    L.append(f"| Inferred remainder (old: added to Unknown by balancing) | {old_tot['unknown_inferred']:,.0f} | 0 (none; see next line) |")
    L.append(f"| Unknown as displayed by the old tile | {old_unknown_total:,.0f} | {new_tot['explicit_unknown']:,.0f} |")
    L.append(f"| enrollment_minus_parsed, summed over reported rows (stored, shown nowhere as unknown) | | {emp:,.0f} (positive gaps only: {emp_pos:,.0f}) |")
    L.append(f"| Gender diverse | | {new_tot['gender_diverse']:,.0f} |")
    L.append(f"| Cis/trans-qualified | | {new_tot['ambiguous']:,.0f} |")
    if inferred_share is not None:
        L.append(f"\nShare of the old Unknown tile that was the inferred remainder: **{inferred_share:.1f}%** (the tile shrinks by about that at cutover).\n")
    L.append("## D. Rows removed from composition by is_participant_count\n")
    L.append(f"Reported-sex rows the vendored parser flags is_participant_count = False: **{len(removed):,}** trials, "
             f"{out['removed_by_is_participant_count']['female']:,.0f} female / {out['removed_by_is_participant_count']['male']:,.0f} male units.\n")
    L.append("All reported-sex rows by the units class of the count-driving measure (diagnostic; the flagged column is what the vendored parser version excludes):\n")
    L.append("| Units class | Trials | Female units | Male units | Flagged by parser |\n|---|---|---|---|---|")
    for c in ("count_of_units", "mean_or_median", "percent_like", "participant_count_like"):
        d = by_class.get(c, {"trials": 0, "female": 0, "male": 0, "flagged_by_parser": 0})
        L.append(f"| {c} | {d['trials']:,} | {d['female']:,.0f} | {d['male']:,.0f} | {d['flagged_by_parser']:,} |")
    L.append(f"\nIndustry tab Sex-tier cohort (old rule: interventional, not terminated, PCD >= 2009, attributed industry company, both legacy counts > 0): **{len(industry):,}** trials.\n")
    L.append("| Units class | Cohort trials | Female (old totals) | Male (old totals) | Flagged by parser |\n|---|---|---|---|---|")
    for c in ("count_of_units", "mean_or_median", "percent_like", "participant_count_like"):
        d = ind_by_class.get(c, {"trials": 0, "female_old": 0, "male_old": 0, "flagged_by_parser": 0})
        L.append(f"| {c} | {d['trials']:,} | {d['female_old']:,.0f} | {d['male_old']:,.0f} | {d['flagged_by_parser']:,} |")
    L.append("\n## E. Percent female by results-posted year\n")
    L.append("| Year | Old (mean f/(f+m+u), unknown incl. inferred) | n | New (a) mean of within-trial f/(f+m) | New (b) participant-weighted | n |\n|---|---|---|---|---|---|")
    for y, d in out["percent_female_by_year"].items():
        f = lambda v: "" if v is None else f"{v:.1f}"
        L.append(f"| {y} | {f(d['old_mean_incl_unknown'])} | {d['old_n']:,} | {f(d['new_a_mean'])} | {f(d['new_b_weighted'])} | {d['new_n']:,} |")
    md = "\n".join(L) + "\n"
    with open(os.path.join(a.out_dir, "side_by_side.md"), "w") as fh:
        fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
