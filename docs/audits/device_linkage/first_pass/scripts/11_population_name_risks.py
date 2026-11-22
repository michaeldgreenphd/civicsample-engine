#!/usr/bin/env python3
"""Item 6 inputs measured on the whole list (no network): name collisions
inside the list, company renames visible in the list, how many names are
generic by the Route B rule, and whether predicate numbers cited in the local
510(k) summaries are themselves on the list.

usage: python3 -I 11_population_name_risks.py <enriched.csv> <pdf_identifiers.json> <out.json>
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (company_brand, device_name_forms, norm_company, norm_text,  # noqa: E402
                    read_devices)


def clean(s: str) -> str:
    s = "".join(ch for ch in s if not (ord(ch) < 32 or 127 <= ord(ch) < 160))
    return re.sub(r"\s+", " ", s.replace("®", "").replace("™", "")).strip()


def main() -> None:
    rows, seen = [], set()
    for r in read_devices(sys.argv[1]):
        sn = r["Submission Number"].strip()
        if sn not in seen:
            seen.add(sn)
            rows.append(r)
    n = len(rows)
    res: dict = {"distinct_submissions": n}
    forms = [device_name_forms(clean(r["Device"]), clean(r["Company"])) for r in rows]
    res["names_generic_by_rule"] = sum(1 for f in forms if f["name_is_generic"])
    res["names_core_differs_from_raw"] = sum(1 for f in forms if f["core"] != f["raw"])
    res["names_with_gt1_model_segment"] = sum(1 for f in forms if len(f["variants"]) > 1)
    res["names_core_single_token"] = sum(1 for f in forms if len(norm_text(f["core"]).split()) == 1)
    res["names_core_single_token_le5_chars"] = sum(
        1 for f in forms if len(norm_text(f["core"]).split()) == 1 and len(norm_text(f["core"])) <= 5)
    res["names_core_contains_company_brand"] = sum(
        1 for f, r in zip(forms, rows)
        if set(company_brand(clean(r["Company"])).split()) & set(norm_text(f["core"]).split()))

    # device-family key = normalised core name; who shares it?
    fam = collections.defaultdict(list)
    for f, r in zip(forms, rows):
        fam[norm_text(f["core"])].append(r)
    multi = {k: v for k, v in fam.items() if len(v) > 1}
    res["core_name_keys"] = len(fam)
    res["core_name_keys_with_gt1_submission"] = len(multi)
    res["submissions_in_multi_submission_families"] = sum(len(v) for v in multi.values())
    res["family_size_hist"] = dict(sorted(collections.Counter(min(len(v), 6) for v in fam.values()).items()))
    cross_l1 = {k: v for k, v in multi.items() if len({norm_company(clean(r["Company"])) for r in v}) > 1}
    cross_brand = {k: v for k, v in multi.items() if len({company_brand(clean(r["Company"])) for r in v}) > 1}
    res["families_spanning_gt1_level1_company"] = len(cross_l1)
    res["families_spanning_gt1_brand_key"] = len(cross_brand)
    res["families_spanning_gt1_brand_key_list"] = [
        {"core_name": k, "companies": sorted({clean(r["Company"]) for r in v}),
         "submissions": [r["Submission Number"] for r in v]}
        for k, v in sorted(cross_brand.items(), key=lambda kv: -len(kv[1]))]
    # years spanned by a family
    def yr(r):
        y = int(r["Date of Final Decision"].split("/")[-1])
        return 1900 + y if y >= 90 else 2000 + y
    spans = [max(map(yr, v)) - min(map(yr, v)) for v in multi.values()]
    res["family_year_span_hist"] = dict(sorted(collections.Counter(min(s, 6) for s in spans).items()))

    # predicates cited in the local 510(k) summaries
    listed = {r["Submission Number"].strip() for r in rows}
    pred = []
    for d in json.load(open(sys.argv[2])):
        if d["submission_number"].startswith("K"):
            cited = d["other_submission_numbers_cited"]
            pred.append({"submission_number": d["submission_number"], "cited": cited,
                         "cited_on_ai_list": [c for c in cited if c in listed]})
    res["predicates_in_local_510k_summaries"] = pred
    json.dump(res, open(sys.argv[3], "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "families_spanning_gt1_brand_key_list"}, indent=1))
    for x in res["families_spanning_gt1_brand_key_list"]:
        print("  ", x["core_name"][:50], "|", x["companies"], x["submissions"][:6])


if __name__ == "__main__":
    main()
