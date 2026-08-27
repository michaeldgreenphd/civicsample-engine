#!/usr/bin/env python3
"""
Prune historical snapshots to the site's retention policy.

GitHub Pages syncs the whole repository tree on every deploy; unbounded
weekly snapshots (~140 MB each) push the published site into the multi-GB
range, where the Pages "syncing_files" phase becomes slow and prone to
"Deployment failed, try again later" timeouts.

Retention policy:
  - Bi-weekly tier: the 4 most recent snapshots spaced at least 14 days
    apart, walking back from the newest (the newest is always kept).
  - Monthly tier: for months older than the bi-weekly window that are not
    already represented by a kept snapshot, keep that month's latest
    snapshot as an AGGREGATE archive - dashboard-summary.json (and
    industry_sponsors.json when present) only, with the heavy
    demographics part files stripped. The dashboard renders these dates
    from the summary (all charts; filters and the full study table need
    a full snapshot). Published GitHub Pages sites are capped at ~1 GB,
    which full monthly archives would exceed within months.
  - Everything else is deleted, and history.json is rewritten to exactly
    the kept dates so the dashboard's "View snapshot" selector never
    offers a date whose files are gone.
  - Safety: a monthly-tier snapshot is only stripped to summary form if
    its dashboard-summary.json exists; otherwise its parts are kept and
    a warning is printed.

Run from the repository root (the weekly extract workflow runs it after
archiving the new snapshot):  python3 scripts/prune_snapshots.py [--dry-run]
"""
import json
import os
import re
import shutil
import sys
from datetime import date, timedelta

SNAPSHOT_DIR = "snapshots"
HISTORY_FILE = "history.json"
BIWEEKLY_KEEP = 4
BIWEEKLY_SPACING = timedelta(days=14)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse(d):
    y, m, dd = d.split("-")
    return date(int(y), int(m), int(dd))


def compute_keep(dates):
    """Given snapshot date strings, return the set to KEEP under the policy."""
    ordered = sorted({d for d in dates if DATE_RE.match(d)}, key=parse, reverse=True)
    if not ordered:
        return set()

    keep = [ordered[0]]                       # newest is always kept
    for d in ordered[1:]:
        if len(keep) >= BIWEEKLY_KEEP:
            break
        if parse(keep[-1]) - parse(d) >= BIWEEKLY_SPACING:
            keep.append(d)

    kept_months = {d[:7] for d in keep}
    oldest_biweekly = parse(keep[-1])
    monthly = {}
    for d in ordered:
        if parse(d) >= oldest_biweekly or d[:7] in kept_months:
            continue
        # latest snapshot of each not-yet-represented month
        if d[:7] not in monthly or parse(d) > parse(monthly[d[:7]]):
            monthly[d[:7]] = d
    return set(keep), set(monthly.values())


def main():
    dry = "--dry-run" in sys.argv

    on_disk = sorted(d for d in os.listdir(SNAPSHOT_DIR)
                     if DATE_RE.match(d) and os.path.isdir(os.path.join(SNAPSHOT_DIR, d))) \
        if os.path.isdir(SNAPSHOT_DIR) else []
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            history = json.load(f).get("dates", [])

    all_dates = sorted(set(on_disk) | {d for d in history if DATE_RE.match(d)}, key=parse)
    biweekly, monthly = compute_keep(all_dates)
    keep = biweekly | monthly

    removed, slimmed = [], []
    for d in on_disk:
        if d not in keep:
            removed.append(d)
            if not dry:
                shutil.rmtree(os.path.join(SNAPSHOT_DIR, d))
    for d in sorted(monthly, key=parse):
        sdir = os.path.join(SNAPSHOT_DIR, d)
        if not os.path.isdir(sdir):
            continue
        parts = [f for f in os.listdir(sdir) if f.startswith("demographics.part")]
        if not parts:
            continue  # already summary-only
        if not os.path.exists(os.path.join(sdir, "dashboard-summary.json")):
            print(f"  WARNING: {d} has no dashboard-summary.json; keeping its part files")
            continue
        slimmed.append(d)
        if not dry:
            for f in parts:
                os.remove(os.path.join(sdir, f))

    kept_dates = sorted(d for d in all_dates if d in keep)
    if not dry:
        with open(HISTORY_FILE, "w") as f:
            json.dump({"dates": kept_dates}, f, indent=2)
            f.write("\n")

    print(f"Snapshot retention ({'dry run' if dry else 'applied'}):")
    print(f"  bi-weekly (full)      ({len(biweekly)}): {', '.join(sorted(biweekly, key=parse))}")
    print(f"  monthly (aggregate)   ({len(monthly)}): {', '.join(sorted(monthly, key=parse)) if monthly else '-'}")
    print(f"  stripped to aggregate ({len(slimmed)}): {', '.join(slimmed) if slimmed else '-'}")
    print(f"  removed               ({len(removed)}): {', '.join(removed) if removed else '-'}")


if __name__ == "__main__":
    main()
