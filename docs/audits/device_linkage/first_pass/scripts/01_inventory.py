#!/usr/bin/env python3
"""Item 1: inventory of the FDA AI/ML-enabled device list.

usage: python3 -I 01_inventory.py <enriched.csv> <raw.csv> <out.json> <repo_root> [<repo_root> ...]
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (company_family_key, norm_company, norm_text,  # noqa: E402
                    parse_decision_date, pathway_of, read_devices)


def main() -> None:
    enriched, raw, out = sys.argv[1], sys.argv[2], sys.argv[3]
    roots = sys.argv[4:]
    rows = read_devices(enriched)
    raw_rows = read_devices(raw)
    n = len(rows)
    res: dict = {"n_rows_enriched": n, "n_rows_raw": len(raw_rows)}
    cols = list(rows[0].keys())
    res["columns"] = cols

    # completeness: blank or literal "Not Found"
    comp = {}
    for c in cols:
        blank = sum(1 for r in rows if not (r[c] or "").strip())
        nf = sum(1 for r in rows if (r[c] or "").strip() == "Not Found")
        comp[c] = {"non_blank": n - blank, "blank": blank, "literal_not_found": nf,
                   "distinct": len({(r[c] or "").strip() for r in rows})}
    res["completeness"] = comp

    # raw vs enriched agreement on the six shared columns
    shared = [c for c in raw_rows[0].keys()]
    res["raw_equals_enriched_on_shared_columns"] = (
        len(raw_rows) == n and all(all(a[c] == b[c] for c in shared) for a, b in zip(raw_rows, rows)))

    # submission numbers
    sns = [r["Submission Number"].strip() for r in rows]
    res["submission_number"] = {
        "distinct": len(set(sns)),
        "duplicated_values": {k: v for k, v in collections.Counter(sns).items() if v > 1},
        "not_upper_or_padded": [s for r in rows if (s := r["Submission Number"]) != s.strip().upper()],
    }
    shape = collections.Counter()
    for s in sns:
        shape[re.sub(r"\d", "9", s)] += 1
    res["submission_number"]["shapes"] = dict(shape.most_common())
    prefix = collections.Counter(re.match(r"[A-Z]+", s).group(0) if re.match(r"[A-Z]+", s) else "?" for s in sns)
    res["submission_number"]["prefix_counts"] = dict(prefix.most_common())
    res["submission_number"]["pathway_counts"] = dict(collections.Counter(pathway_of(s) for s in sns).most_common())
    res["submission_number"]["with_supplement_suffix"] = [s for s in sns if "/" in s or "S0" in s[3:]]

    # duplicate-row anatomy: which columns differ inside a duplicated submission number
    by_sn = collections.defaultdict(list)
    for r in rows:
        by_sn[r["Submission Number"].strip()].append(r)
    dup_diff = collections.Counter()
    dup_dates = collections.Counter()
    for k, v in by_sn.items():
        if len(v) > 1:
            diff = tuple(sorted(c for c in cols if len({x[c] for x in v}) > 1))
            dup_diff[", ".join(diff) or "(byte-identical rows)"] += 1
            dup_dates[v[0]["Date of Final Decision"].split("/")[0] + "/20" + v[0]["Date of Final Decision"].split("/")[2]] += 1
    res["submission_number"]["duplicate_groups_by_differing_columns"] = dict(dup_diff)
    res["submission_number"]["duplicate_groups_by_decision_month"] = dict(dup_dates)
    res["submission_number"]["duplicate_groups_same_level1_company"] = sum(
        1 for v in by_sn.values() if len(v) > 1 and len({norm_company(x["Company"]) for x in v}) == 1)

    # text hygiene: control characters (C0/C1) and non-ASCII in the name fields
    def has_ctrl(s: str) -> bool:
        return any((ord(ch) < 32 or 127 <= ord(ch) < 160) for ch in s)
    res["text_hygiene"] = {
        c: {"rows_with_control_chars": sum(1 for r in rows if has_ctrl(r[c])),
            "rows_with_non_ascii": sum(1 for r in rows if any(ord(ch) > 127 for ch in r[c])),
            "rows_with_leading_or_trailing_space": sum(1 for r in rows if r[c] != r[c].strip()),
            "control_char_codes": dict(collections.Counter(
                hex(ord(ch)) for r in rows for ch in r[c] if (ord(ch) < 32 or 127 <= ord(ch) < 160)))}
        for c in ("Device", "Company")}

    # decision dates
    years = collections.Counter()
    bad_dates = []
    for r in rows:
        d = parse_decision_date(r["Date of Final Decision"])
        if d is None:
            bad_dates.append(r["Date of Final Decision"])
        else:
            years[d.year] += 1
    res["decision_year_counts"] = dict(sorted(years.items()))
    res["unparseable_dates"] = bad_dates
    ds = [parse_decision_date(r["Date of Final Decision"]) for r in rows]
    res["decision_date_min"], res["decision_date_max"] = str(min(ds)), str(max(ds))
    # submission-number year vs decision year (K25xxxx => FY2025 receipt)
    lag = collections.Counter()
    for r, d in zip(rows, ds):
        m = re.match(r"[A-Z]+(\d{2})", r["Submission Number"].strip())
        if m and d:
            yy = int(m.group(1)); sy = 1900 + yy if yy >= 90 else 2000 + yy
            lag[d.year - sy] += 1
    res["decision_year_minus_submission_year"] = dict(sorted(lag.items()))
    res["pathway_by_year"] = {}
    for r, d in zip(rows, ds):
        res["pathway_by_year"].setdefault(str(d.year), collections.Counter())[pathway_of(r["Submission Number"])] += 1
    res["pathway_by_year"] = {k: dict(v) for k, v in sorted(res["pathway_by_year"].items())}

    # panels, product codes
    panel = collections.Counter(r["Panel (Lead)"].strip() for r in rows)
    pc = collections.Counter(r["Primary Product Code"].strip() for r in rows)
    res["panel_distinct"] = len(panel)
    res["panel_top15"] = panel.most_common(15)
    res["product_code_distinct"] = len(pc)
    res["product_code_top15"] = pc.most_common(15)
    res["product_code_singletons"] = sum(1 for v in pc.values() if v == 1)
    res["product_code_bad_shape"] = sorted({k for k in pc if not re.fullmatch(r"[A-Z]{3}", k)})

    # device-name duplicates
    dn_exact = collections.Counter(r["Device"].strip() for r in rows)
    dn_norm = collections.defaultdict(list)
    for r in rows:
        dn_norm[norm_text(r["Device"])].append(r)
    # family key: strip parenthesised versions and trailing version tokens
    def family(name: str) -> str:
        s = re.sub(r"\([^)]*\)", " ", name)
        s = norm_text(s)
        s = re.sub(r"\b(v|ver|version|rev|release|model)\s*\d[\w ]*$", "", s).strip()
        s = re.sub(r"\b(v?\d+(\s\d+)*[a-z]?)$", "", s).strip()
        return s or norm_text(name)  # never collapse a name to the empty key
    dn_fam = collections.defaultdict(list)
    for r in rows:
        dn_fam[family(r["Device"])].append(r)
    def grp_stats(groups: dict) -> dict:
        multi = {k: v for k, v in groups.items() if len(v) > 1}
        return {
            "distinct_keys": len(groups),
            "keys_with_gt1_submission": len(multi),
            "submissions_in_such_groups": sum(len(v) for v in multi.values()),
            "of_which_groups_spanning_gt1_normalised_company": sum(
                1 for v in multi.values() if len({norm_company(r["Company"]) for r in v}) > 1),
        }
    res["device_name"] = {
        "distinct_exact": len(dn_exact),
        "exact_names_with_gt1_submission": sum(1 for v in dn_exact.values() if v > 1),
        "submissions_sharing_exact_name": sum(v for v in dn_exact.values() if v > 1),
        "normalised": grp_stats(dn_norm),
        "family_version_stripped": grp_stats(dn_fam),
        "top20_family": [(k, len(v), sorted({norm_company(r["Company"]) for r in v})[:6])
                         for k, v in sorted(dn_fam.items(), key=lambda kv: -len(kv[1]))[:20]],
        "same_name_different_company_examples": [
            (k, sorted({r["Company"] for r in v}), [r["Submission Number"] for r in v])
            for k, v in sorted(dn_norm.items(), key=lambda kv: -len(kv[1]))
            if len(v) > 1 and len({norm_company(r["Company"]) for r in v}) > 1][:40],
        "name_len_words_hist": dict(sorted(collections.Counter(
            min(len(norm_text(r["Device"]).split()), 8) for r in rows).items())),
        "names_with_version_or_parenthesis": sum(
            1 for r in rows if re.search(r"\(|\b[vV]\d|\b\d+\.\d+", r["Device"])),
        "names_with_semicolon_or_multiple_models": sum(1 for r in rows if re.search(r";|,| and | / ", r["Device"])),
    }

    # companies
    co_exact = collections.Counter(r["Company"].strip() for r in rows)
    co_norm = collections.defaultdict(collections.Counter)
    for r in rows:
        co_norm[norm_company(r["Company"])][r["Company"].strip()] += 1
    co_fam = collections.defaultdict(collections.Counter)
    for k, v in co_norm.items():
        co_fam[company_family_key(next(iter(v)))][k] += sum(v.values())
    sizes = sorted((sum(v.values()) for v in co_norm.values()), reverse=True)
    res["company"] = {
        "distinct_exact_strings": len(co_exact),
        "distinct_level1_normalised": len(co_norm),
        "distinct_level2_brand_families": len(co_fam),
        "level1_clusters_merging_gt1_spelling": sum(1 for v in co_norm.values() if len(v) > 1),
        "level1_singletons_one_submission": sum(1 for s in sizes if s == 1),
        "level1_with_ge5": sum(1 for s in sizes if s >= 5),
        "level1_with_ge10": sum(1 for s in sizes if s >= 10),
        "top25_level1": [(k, sum(v.values()), dict(v)) for k, v in
                         sorted(co_norm.items(), key=lambda kv: -sum(kv[1].values()))[:25]],
        "top25_level2_families": [(k, sum(v.values()), dict(v.most_common(12))) for k, v in
                                  sorted(co_fam.items(), key=lambda kv: -sum(kv[1].values()))[:25]],
        "families_merging_gt1_level1_name": sum(1 for v in co_fam.values() if len(v) > 1),
        "share_top10_level1": sum(sizes[:10]),
        "share_top10_level2": sum(sorted((sum(v.values()) for v in co_fam.values()), reverse=True)[:10]),
        "casing": dict(collections.Counter(
            "ALLCAPS" if c.upper() == c else "Mixed" for c in co_exact.elements())),
    }

    # pdf_url
    urls = [r.get("pdf_url", "").strip() for r in rows]
    pat = collections.Counter(re.sub(r"\d", "9", re.sub(r"/[A-Z]+\d+\.pdf$", "/<SN>.pdf", u)) for u in urls)
    res["pdf_url"] = {
        "present": sum(1 for u in urls if u and u != "Not Found"),
        "patterns": dict(pat.most_common()),
        "by_pathway_pattern": dict(collections.Counter(
            (pathway_of(r["Submission Number"]), re.sub(r"pdf\d*/.*", "pdf<YY>/", r["pdf_url"])) .__str__()
            for r in rows)),
        "url_sn_matches_row_sn": sum(1 for r in rows if r["pdf_url"].endswith("/" + r["Submission Number"].strip().upper() + ".pdf")),
        "pre2002_pdf_dir_without_yy": sum(1 for u in urls if re.search(r"/pdf/", u)),
    }
    lp = [r.get("local_pdf_path", "").strip() for r in rows]
    res["local_pdf_path"] = {
        "claimed_present": sum(1 for p in lp if p and p != "Not Found"),
        "dir_patterns": dict(collections.Counter(os.path.dirname(p) for p in lp).most_common()),
        "date_prefixes": dict(collections.Counter(os.path.basename(p)[:10] for p in lp if p != "Not Found").most_common()),
    }
    # do the claimed paths exist anywhere on disk?
    exist = collections.Counter()
    for root in roots:
        for p in lp:
            if p and p != "Not Found" and os.path.exists(os.path.join(root, p)):
                exist[root] += 1
    res["local_pdf_path"]["claimed_paths_existing_under_root"] = {r: exist.get(r, 0) for r in roots}
    on_disk = {}
    sn_set = set(sns)
    for root in roots:
        found = []
        for dp, dn, fn in os.walk(root):
            if "/.git" in dp:
                continue
            for f in fn:
                if f.lower().endswith(".pdf"):
                    m = re.search(r"((?:DEN|BK|K|P)\d{6})", f)
                    if m:
                        found.append((os.path.relpath(os.path.join(dp, f), root), m.group(1), m.group(1) in sn_set))
        on_disk[root] = found
    res["fda_summary_pdfs_on_disk"] = {r: {"count": len(v), "in_device_list": sum(1 for x in v if x[2]),
                                           "files": v} for r, v in on_disk.items()}

    with open(out, "w") as f:
        json.dump(res, f, indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
