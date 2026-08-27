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
from collections import defaultdict


def main():
    # ── Load all studies ──
    all_studies = []
    extracted_at = None

    for i in range(1, 9):
        path = f"data/demographics.part{i}.json.gz"
        if not os.path.exists(path):
            continue
        print(f"Reading {path}...")
        with gzip.open(path, "rt") as f:
            container = json.load(f)
        if extracted_at is None:
            extracted_at = container.get("extracted_at")
        all_studies.extend(container["data"])

    total = len(all_studies)
    print(f"Loaded {total} studies")

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
    }

    path = "data/dashboard-summary.json"
    with open(path, "w") as f:
        json.dump(summary, f, separators=(",", ":"))

    size_kb = os.path.getsize(path) / 1024
    print(f"\n✓ Generated {path}: {size_kb:.1f} KB")
    print(f"  ({total} studies pre-aggregated into {len(years)} year buckets)")


if __name__ == "__main__":
    main()
