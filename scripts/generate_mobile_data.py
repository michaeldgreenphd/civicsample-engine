#!/usr/bin/env python3
"""
Generate a pre-computed dashboard summary for mobile browsers.

The full dataset is ~136 MB compressed / 780 MB uncompressed — far too much
for mobile browsers.  Instead of sending 77K individual study records, this
script pre-computes every aggregate the dashboard charts need and writes a
single ~30 KB JSON file.

Mobile loads ONLY this file: instant dashboard with all charts, no per-study
data needed.  The Studies table and Geography tab show a "view on desktop"
prompt.  Filters are disabled (all data is pre-aggregated).
"""
import json
import gzip
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sex_gender_table as sgt  # noqa: E402

# Drill-down label lists carry at most this many distinct labels each; every
# label is still in the parsed table and the audit's label_buckets.csv.
LABEL_LIST_LIMIT = 200


def sex_gender_summary(all_studies, table_rows=None):
    """The sg=v2 block of dashboard-summary.json, built ONLY from the parser's rows.

    table_rows is the full record (sex_gender_parsed.csv.gz via
    sex_gender_table.read_table); without it the lean rows in the study records
    are used and the label drill-downs are empty. Every number here is a count
    or sum over rows the parser produced. The five states come from
    sex_report_status and nothing else; the participant totals and both
    percent-female series (README D5) are restricted to rows with reported_sex
    AND is_participant_count; enrollmentMinusParsed is the summed enrollment gap
    of reported rows and is shipped so it can be audited, never to be displayed
    as unknown. Mobile renders the Sex and Gender tabs from this block; desktop
    computes the same numbers from the per-study rows and the CSV.
    """
    by_nct = {r.get("nct_id"): r for r in (table_rows or [])}
    rows = []
    for s in all_studies:
        r = by_nct.get(s.get("nct_id")) or s.get("sex_gender")
        if not r:
            continue
        r = dict(r)
        r["year"] = (s.get("results_date") or "")[:4] or None
        r["_enrollment_registered"] = s.get("enrollment") or 0
        if r.get("percent_female") is None and "percent_female" not in r:
            r["percent_female"] = sgt.percent_female(r)
        rows.append(r)
    if not rows:
        return None

    sc = sgt.status_counts(rows)
    denom = [r for r in rows if r.get("reported_sex") and r.get("is_participant_count")]

    def tot(rs, k):
        return sum((r.get(k) or 0) for r in rs)

    totals = {"female": tot(denom, "n_female"), "male": tot(denom, "n_male"),
              "explicit_unknown": tot(denom, "n_unknown"),
              "gender_diverse": tot(denom, "n_gender_diverse"),
              "ambiguous": tot(denom, "n_ambiguous_gender")}
    reported = [r for r in rows if r.get("sex_report_status") == "reported"]
    # Excluded from the SEX composition: rows that report sex but whose
    # count-driving measure is not a participant count. A trial that is
    # "reported" only through gender-diverse or ambiguous counts never entered
    # the sex composition, so it is not "excluded" from it either (the same set
    # the side-by-side reports).
    excluded = [r for r in rows if r.get("reported_sex") and not r.get("is_participant_count")]

    def labels(key):
        # Label trails are lists on the full rows (JSON arrays in the CSV);
        # never split a string on "; ", which real labels contain.
        c = defaultdict(int)
        for r in rows:
            v = r.get(key) or []
            for l in (v if isinstance(v, list) else [v]):
                if l:
                    c[l] += 1
        top = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:LABEL_LIST_LIMIT]
        return {"distinct": len(c), "top": [[l, n] for l, n in top]}

    by_year = {}
    for r in rows:
        y = r.get("year")
        if not y:
            continue
        d = by_year.setdefault(y, {
            "total": 0,
            **{f"status_{s}": 0 for s in sgt.STATES},
            **{o: 0 for o in sgt.OUTCOMES},
            "sg_f": 0.0, "sg_m": 0.0, "sg_u": 0.0, "sg_gd": 0.0, "sg_amb": 0.0,
            "sg_denominator_trials": 0,
            "sg_pf_sum": 0.0, "sg_pf_count": 0, "sg_f_sum": 0.0, "sg_fm_sum": 0.0,
            "sg_enrollment_minus_parsed": 0.0,
            "sg_enrollment_reported": 0, "sg_enrollment_not_reported": 0,
            "sg_enrollment_uninformative": 0, "sg_enrollment_explicit_unknown_only": 0,
            "sg_enrollment_parse_error": 0,
        })
        d["total"] += 1
        st = r.get("sex_report_status")
        if st in sgt.STATES:
            d[f"status_{st}"] += 1
            d[f"sg_enrollment_{st}"] += r.get("_enrollment_registered") or 0
        for o in sgt.OUTCOMES:
            if r.get(o) is True:
                d[o] += 1
        if st == "reported" and r.get("enrollment_minus_parsed") is not None:
            d["sg_enrollment_minus_parsed"] += r["enrollment_minus_parsed"]
        if r.get("reported_sex") and r.get("is_participant_count"):
            d["sg_denominator_trials"] += 1
            d["sg_f"] += r.get("n_female") or 0
            d["sg_m"] += r.get("n_male") or 0
            d["sg_u"] += r.get("n_unknown") or 0
            d["sg_gd"] += r.get("n_gender_diverse") or 0
            d["sg_amb"] += r.get("n_ambiguous_gender") or 0
    for y, pf in sgt.percent_female_series(rows).items():
        d = by_year.setdefault(y, {})
        d["sg_pf_sum"] = pf["pf_sum"]; d["sg_pf_count"] = pf["pf_count"]
        d["sg_f_sum"] = pf["f_sum"]; d["sg_fm_sum"] = pf["fm_sum"]

    return {
        "parser_rules_version": sgt.sgp.PARSER_RULES_VERSION,
        "parser_module_version": sgt.sgp.__version__,
        "states": list(sgt.STATES),
        "statusCounts": sc["status"],
        "outcomes": sc["outcomes"],
        "uninformativeReasons": sc["uninformative_reason"],
        "declaredNotCollected": sc["declared_not_collected"],
        "refetched": sc["refetched"],
        "denominatorTrials": len(denom),
        "totals": totals,
        "excludedFromComposition": {"trials": len(excluded),
                                    "female": tot(excluded, "n_female"), "male": tot(excluded, "n_male")},
        "enrollmentMinusParsed": tot(reported, "enrollment_minus_parsed"),
        "labels": {"gender_diverse": labels("gender_diverse_labels"),
                   "ambiguous": labels("ambiguous_labels"),
                   "unknown": labels("unknown_labels")},
        "percentFemaleSeries": {
            "a": "mean of within-trial n_female / (n_female + n_male) across trials in the year (sg_pf_sum / sg_pf_count)",
            "b": "participant-weighted sum(n_female) / sum(n_female + n_male) across the same trials (sg_f_sum / sg_fm_sum)",
            "denominator": "trials with reported_sex AND is_participant_count AND n_female + n_male > 0; gender_diverse and ambiguous excluded",
        },
        "byYear": {y: v for y, v in sorted(by_year.items())},
    }


def main():
    # ── Load all studies ──
    all_studies = []
    extracted_at = None
    pipeline_commit = None

    for i in range(1, 9):
        path = f"data/demographics.part{i}.json.gz"
        if not os.path.exists(path):
            continue
        print(f"Reading {path}...")
        with gzip.open(path, "rt") as f:
            container = json.load(f)
        if extracted_at is None:
            extracted_at = container.get("extracted_at")
        if pipeline_commit is None:
            pipeline_commit = container.get("pipeline_commit")
        all_studies.extend(container["data"])

    total = len(all_studies)
    print(f"Loaded {total} studies")

    # The full sex/gender record (labels, percent_female) lives in the parsed
    # table, not in the lean per-study rows; read it when it is there.
    table_path = "data/sex_gender_parsed.csv.gz"
    table_rows = sgt.read_table(table_path) if os.path.exists(table_path) else None
    print(f"Sex/gender table: {len(table_rows) if table_rows else 'absent (lean rows only)'}")

    # ── Summary cards ──
    race_count = sum(1 for s in all_studies if (s.get("race") or {}).get("reported"))
    eth_count = sum(1 for s in all_studies if (s.get("ethnicity") or {}).get("reported"))
    sex_count = sum(1 for s in all_studies if (s.get("sex") or {}).get("reported"))
    gender_count = sum(1 for s in all_studies if (s.get("gender") or {}).get("reported"))
    both_count = sum(1 for s in all_studies
                     if (s.get("race") or {}).get("reported")
                     and (s.get("ethnicity") or {}).get("reported"))

    # ── Aggregate distributions (non-year-grouped) ──
    race_dist = defaultdict(int)
    eth_dist = defaultdict(int)
    sex_dist = defaultdict(int)
    gender_dist = defaultdict(int)
    race_subcats = defaultdict(int)
    eth_subcats = defaultdict(int)

    for s in all_studies:
        r = s.get("race") or {}
        if r.get("reported"):
            for k, v in (r.get("omb_totals") or {}).items():
                race_dist[k] += v or 0
            for k, v in (r.get("subcategory_totals") or {}).items():
                race_subcats[k] += v or 0

        e = s.get("ethnicity") or {}
        if e.get("reported"):
            for k, v in (e.get("omb_totals") or {}).items():
                eth_dist[k] += v or 0
            for k, v in (e.get("subcategory_totals") or {}).items():
                eth_subcats[k] += v or 0

        sx = s.get("sex") or {}
        if sx.get("reported"):
            for k, v in (sx.get("totals") or {}).items():
                sex_dist[k] += v or 0

        g = s.get("gender") or {}
        if g.get("reported"):
            for k, v in (g.get("totals") or {}).items():
                gender_dist[k] += v or 0

    # ── Year-grouped aggregates ──
    # Each year bucket stores everything the various chart functions need.
    years = defaultdict(lambda: {
        "total": 0,
        "race_reported": 0, "eth_reported": 0,
        "sex_reported": 0, "gender_reported": 0, "both_reported": 0,
        "enrollment": 0,
        # Race OMB sums
        "r_ai": 0, "r_as": 0, "r_bl": 0, "r_nh": 0,
        "r_wh": 0, "r_mu": 0, "r_un": 0, "r_ot": 0,
        # Race normalized (sum of per-study fractions)
        "rn_wh": 0.0, "rn_bl": 0.0, "rn_as": 0.0, "rn_count": 0,
        # Ethnicity OMB sums
        "e_hi": 0, "e_nh": 0, "e_un": 0,
        # Ethnicity normalized
        "en_hi": 0.0, "en_count": 0,
        # Sex sums
        "s_f": 0, "s_m": 0, "s_u": 0,
        # Sex normalized
        "sn_f": 0.0, "sn_count": 0,
        # Gender sums
        "g_w": 0, "g_m": 0, "g_nb": 0, "g_tg": 0, "g_ot": 0, "g_u": 0,
        # Gender normalized
        "gn_w": 0.0, "gn_m": 0.0, "gn_nb": 0.0, "gn_tg": 0.0, "gn_count": 0,
        # Full distribution (all studies, including non-reporting)
        "fd_enrollment": 0,
    })

    for s in all_studies:
        rd = (s.get("results_date") or "")[:4]
        if not rd:
            continue
        y = years[rd]
        y["total"] += 1
        enrollment = s.get("enrollment") or 0
        y["enrollment"] += enrollment
        y["fd_enrollment"] += enrollment

        r = s.get("race") or {}
        e = s.get("ethnicity") or {}
        sx = s.get("sex") or {}
        g = s.get("gender") or {}

        r_rep = r.get("reported", False)
        e_rep = e.get("reported", False)
        sx_rep = sx.get("reported", False)
        g_rep = g.get("reported", False)

        if r_rep:
            y["race_reported"] += 1
        if e_rep:
            y["eth_reported"] += 1
        if sx_rep:
            y["sex_reported"] += 1
        if g_rep:
            y["gender_reported"] += 1
        if r_rep and e_rep:
            y["both_reported"] += 1

        # Race aggregates
        if r_rep:
            omb = r.get("omb_totals") or {}
            ai = omb.get("american_indian_alaska_native", 0) or 0
            asian = omb.get("asian", 0) or 0
            bl = omb.get("black_african_american", 0) or 0
            nh = omb.get("native_hawaiian_pacific_islander", 0) or 0
            wh = omb.get("white", 0) or 0
            mu = omb.get("more_than_one_race", 0) or 0
            un = omb.get("unknown_not_reported", 0) or 0
            ot = omb.get("other", 0) or 0
            y["r_ai"] += ai; y["r_as"] += asian; y["r_bl"] += bl
            y["r_nh"] += nh; y["r_wh"] += wh; y["r_mu"] += mu
            y["r_un"] += un; y["r_ot"] += ot
            study_total = ai + asian + bl + nh + wh + mu + un + ot
            if study_total > 0:
                y["rn_count"] += 1
                y["rn_wh"] += wh / study_total
                y["rn_bl"] += bl / study_total
                y["rn_as"] += asian / study_total

        # Ethnicity aggregates
        if e_rep:
            omb = e.get("omb_totals") or {}
            hi = omb.get("hispanic_latino", 0) or 0
            nhi = omb.get("not_hispanic_latino", 0) or 0
            eun = omb.get("unknown_not_reported", 0) or 0
            y["e_hi"] += hi; y["e_nh"] += nhi; y["e_un"] += eun
            study_total = hi + nhi + eun
            if study_total > 0:
                y["en_count"] += 1
                y["en_hi"] += hi / study_total

        # Sex aggregates
        if sx_rep:
            t = sx.get("totals") or {}
            f = t.get("female", 0) or 0
            m = t.get("male", 0) or 0
            u = t.get("unknown", 0) or 0
            y["s_f"] += f; y["s_m"] += m; y["s_u"] += u
            study_total = f + m + u
            if study_total > 0:
                y["sn_count"] += 1
                y["sn_f"] += f / study_total

        # Gender aggregates
        if g_rep:
            t = g.get("totals") or {}
            gw = t.get("woman", 0) or 0
            gm = t.get("man", 0) or 0
            gnb = t.get("nonbinary", 0) or 0
            gtg = t.get("transgender", 0) or 0
            got = t.get("other", 0) or 0
            gu = t.get("unknown", 0) or 0
            y["g_w"] += gw; y["g_m"] += gm; y["g_nb"] += gnb
            y["g_tg"] += gtg; y["g_ot"] += got; y["g_u"] += gu
            study_total = gw + gm + gnb + gtg + got
            if study_total > 0:
                y["gn_count"] += 1
                y["gn_w"] += gw / study_total
                y["gn_m"] += gm / study_total
                y["gn_nb"] += gnb / study_total
                y["gn_tg"] += gtg / study_total

    # ── FDA Oversight aggregates ──
    # Mobile renders the FDA tab from these pre-computed counts since it never
    # loads the per-study payload. Keep in sync with renderFdaOversight() in app.js.
    def _is_drug(s): return s.get("is_fda_regulated_drug") is True
    def _is_device(s): return s.get("is_fda_regulated_device") is True
    def _is_unapproved(s): return s.get("is_unapproved_device") is True
    def _is_nonreg(s):
        return (s.get("is_fda_regulated_drug") is not True
                and s.get("is_fda_regulated_device") is not True
                and s.get("is_unapproved_device") is not True)

    def _reporting_pct(subset, field):
        if not subset:
            return 0.0
        rep = sum(1 for s in subset if (s.get(field) or {}).get("reported"))
        return round(rep / len(subset) * 100, 1)

    drug = [s for s in all_studies if _is_drug(s)]
    device = [s for s in all_studies if _is_device(s)]
    unapproved = [s for s in all_studies if _is_unapproved(s)]
    nonreg = [s for s in all_studies if _is_nonreg(s)]

    # Mutually exclusive regulatory classes for the redesigned FDA tab.
    # "unreported" only becomes non-empty once the extraction preserves the
    # oversight module's missing values as null (see src/utils.py).
    def _fda_class(s):
        dr, dv = s.get("is_fda_regulated_drug"), s.get("is_fda_regulated_device")
        if dr is True and dv is True:
            return "both"
        if dr is True:
            return "drug"
        if dv is True:
            return "device"
        if dr is None and dv is None:
            return "unreported"
        return "none"

    by_class = {k: [] for k in ("drug", "device", "both", "none", "unreported")}
    for s in all_studies:
        by_class[_fda_class(s)].append(s)
    class_order = ["drug", "device", "both", "none", "unreported"]

    fda = {
        # Legacy block kept verbatim so archived snapshot summaries and older
        # cached frontends keep rendering.
        "counts": {
            "drug": len(drug),
            "device": len(device),
            "unapproved": len(unapproved),
            "nonRegulated": len(nonreg),
        },
        "reporting": {
            # Parallel arrays matching the legacy category order:
            # [Regulated Drug, Regulated Device, Unapproved Device, Non-Regulated]
            "race": [_reporting_pct(drug, "race"), _reporting_pct(device, "race"),
                     _reporting_pct(unapproved, "race"), _reporting_pct(nonreg, "race")],
            "ethnicity": [_reporting_pct(drug, "ethnicity"), _reporting_pct(device, "ethnicity"),
                          _reporting_pct(unapproved, "ethnicity"), _reporting_pct(nonreg, "ethnicity")],
            "sex": [_reporting_pct(drug, "sex"), _reporting_pct(device, "sex"),
                    _reporting_pct(unapproved, "sex"), _reporting_pct(nonreg, "sex")],
        },
        # Mutually exclusive classes (drug-only / device-only / both / explicit
        # no / oversight unreported) plus the unapproved-device layer.
        "classes": {
            "order": class_order,
            "counts": {k: len(by_class[k]) for k in class_order},
            "unapproved": len(unapproved),
            "unapproved_in_device": sum(
                1 for s in unapproved if s.get("is_fda_regulated_device") is True),
            "reporting": {
                field: [_reporting_pct(by_class[k], field) for k in class_order]
                for field in ("sex", "race", "ethnicity")
            },
        },
    }

    # ── Compact recent-studies list for mobile Studies tab ──
    # Include the most recent ~500 studies by results_date with only the fields
    # renderStudiesTable() reads. Drops raw_categories/subcategory_totals and
    # full study_sites so the payload stays under ~300 KB gzipped.
    RECENT_LIMIT = 500

    def _demo(obj, totals_key):
        if not obj or not obj.get("reported"):
            return {"reported": False}
        return {"reported": True, totals_key: obj.get(totals_key) or {}}

    _COMPACT_SG = ("sex_report_status", "reported_sex", "reported_gender", "reported_both",
                   "gender_labeled_binary_only", "is_participant_count", "n_female", "n_male",
                   "n_unknown", "n_gender_diverse", "n_ambiguous_gender", "uninformative_reason",
                   "declared_not_collected", "percent_female", "parser_rules_version")

    # The study records carry only the lean row; the compact record's
    # gender_labeled_binary_only and percent_female come from the full table.
    full_by_nct = {r.get("nct_id"): r for r in (table_rows or [])}

    def _compact_sex_gender(s):
        row = full_by_nct.get(s.get("nct_id")) or s.get("sex_gender")
        if not row:
            return None
        return {k: row.get(k) for k in _COMPACT_SG}

    def _compact(s):
        return {
            "nct_id": s.get("nct_id"),
            "brief_title": s.get("brief_title"),
            "results_date": s.get("results_date"),
            "start_date": s.get("start_date"),
            "primary_completion_date": s.get("primary_completion_date"),
            "completion_date": s.get("completion_date"),
            "completion_to_report_days": s.get("completion_to_report_days"),
            "start_to_report_days": s.get("start_to_report_days"),
            "min_age": s.get("min_age"),
            "max_age": s.get("max_age"),
            "enrollment": s.get("enrollment"),
            "enrollment_type": s.get("enrollment_type"),
            "status": s.get("status"),
            "why_stopped": s.get("why_stopped"),
            "phase": s.get("phase"),
            "is_fda_regulated_drug": s.get("is_fda_regulated_drug"),
            "is_fda_regulated_device": s.get("is_fda_regulated_device"),
            "is_unapproved_device": s.get("is_unapproved_device"),
            "race": _demo(s.get("race"), "omb_totals"),
            "ethnicity": _demo(s.get("ethnicity"), "omb_totals"),
            "sex": _demo(s.get("sex"), "totals"),
            "gender": _demo(s.get("gender"), "totals"),
            # sg=v2 row, trimmed to what a table cell needs; the label trails
            # stay on the desktop rows and in the parsed table.
            "sex_gender": _compact_sex_gender(s),
            # Geography details aren't shipped to mobile (study_sites is heavy
            # and desktop-only). Leaving countries empty makes the cell show ✗
            # rather than a ✓ that opens an empty modal.
            "reference_count": len(s.get("references") or []),
        }

    with_results = [s for s in all_studies if s.get("results_date")]
    with_results.sort(key=lambda s: s.get("results_date") or "", reverse=True)
    recent_studies = [_compact(s) for s in with_results[:RECENT_LIMIT]]

    # ── Build summary JSON ──
    summary = {
        "extracted_at": extracted_at,
        "pipeline_commit": pipeline_commit,
        "totalStudies": total,
        "cards": {
            "raceCount": race_count,
            "ethCount": eth_count,
            "sexCount": sex_count,
            "genderCount": gender_count,
            "bothCount": both_count,
        },
        "raceDistribution": dict(race_dist),
        "ethnicityDistribution": dict(eth_dist),
        "sexDistribution": dict(sex_dist),
        "genderDistribution": dict(gender_dist),
        "raceSubcategories": dict(race_subcats),
        "ethnicitySubcategories": dict(eth_subcats),
        "byYear": {yr: dict(vals) for yr, vals in sorted(years.items())},
        "fda": fda,
        "recentStudies": recent_studies,
        # sg=v2: the manuscript parser's table, summarised. None on pulls that
        # predate the sex_gender row (archived snapshot summaries stay valid).
        "sexGender": sex_gender_summary(all_studies, table_rows),
    }

    path = "data/dashboard-summary.json"
    with open(path, "w") as f:
        json.dump(summary, f, separators=(",", ":"))

    size_kb = os.path.getsize(path) / 1024
    print(f"\n✓ Generated {path}: {size_kb:.1f} KB")
    print(f"  ({total} studies pre-aggregated into {len(years)} year buckets)")


if __name__ == "__main__":
    main()
