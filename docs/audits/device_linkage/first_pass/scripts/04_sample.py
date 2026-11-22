#!/usr/bin/env python3
"""Seeded, year-stratified sample of devices for Route B.

Sampling frame: one row per DISTINCT submission number (the list has 1,451
rows but 1,430 distinct numbers; the first row of each duplicate is kept, and
the duplicates differ only in company spelling or not at all).

Strata: decision year, with the sparse early years pooled (<=2015, 2016-2018)
so every stratum can receive at least one draw. Allocation is proportional by
largest remainder with a floor of 1. Within a stratum the draw is
random.Random(SEED).sample over rows sorted by submission number, so the
sample does not depend on file order.

usage: python3 -I 04_sample.py <enriched.csv> <out.csv> [n=80] [seed=20261113]
"""
from __future__ import annotations

import csv
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import norm_company, parse_decision_date, pathway_of, read_devices  # noqa: E402


def stratum(year: int) -> str:
    if year <= 2015:
        return "<=2015"
    if year <= 2018:
        return "2016-2018"
    return str(year)


def clean(s: str) -> str:
    """Strip C0/C1 control characters (the list carries cp1252 TM/quote bytes
    decoded as C1 controls) and collapse whitespace."""
    s = "".join(ch for ch in s if not (ord(ch) < 32 or 127 <= ord(ch) < 160))
    s = s.replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", s).strip()


def main() -> None:
    src, out = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261113
    seen, frame = set(), []
    for r in read_devices(src):
        sn = r["Submission Number"].strip()
        if sn in seen:
            continue
        seen.add(sn)
        frame.append(r)
    strata: dict[str, list[dict]] = {}
    for r in frame:
        strata.setdefault(stratum(parse_decision_date(r["Date of Final Decision"]).year), []).append(r)
    total = len(frame)
    quota = {k: n * len(v) / total for k, v in strata.items()}
    alloc = {k: max(1, int(q)) for k, q in quota.items()}
    # largest remainder until the allocation sums to n
    order = sorted(strata, key=lambda k: quota[k] - int(quota[k]), reverse=True)
    i = 0
    while sum(alloc.values()) < n:
        alloc[order[i % len(order)]] += 1
        i += 1
    while sum(alloc.values()) > n:
        k = max(alloc, key=lambda k: alloc[k] - quota[k])
        alloc[k] -= 1
    rng = random.Random(seed)
    picked = []
    for k in sorted(strata):
        pool = sorted(strata[k], key=lambda r: r["Submission Number"])
        for r in rng.sample(pool, alloc[k]):
            picked.append((k, r))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample_id", "stratum", "stratum_size", "stratum_draws", "submission_number", "pathway",
                    "decision_date_iso", "device_raw", "device_clean", "company_raw", "company_clean",
                    "company_level1", "panel", "product_code"])
        for j, (k, r) in enumerate(picked, 1):
            w.writerow([j, k, len(strata[k]), alloc[k], r["Submission Number"].strip(),
                        pathway_of(r["Submission Number"]), parse_decision_date(r["Date of Final Decision"]).isoformat(),
                        r["Device"], clean(r["Device"]), r["Company"], clean(r["Company"]),
                        norm_company(clean(r["Company"])), r["Panel (Lead)"], r["Primary Product Code"]])
    print(f"frame={total} distinct submissions; n={len(picked)}; seed={seed}")
    for k in sorted(strata):
        print(f"  {k:>10}: {len(strata[k]):4d} in frame, {alloc[k]:2d} drawn")


if __name__ == "__main__":
    main()
