#!/usr/bin/env python3
"""Check this week's site files against the site's own contract before the push.

READS   <site>/tests/record_contract.json   every study-record field the site reads
        <site>/tests/data_budget.json       the part count and the size budget
        <site>/data/demographics.part*.json.gz   this week's parts, as staged
        the <site> working tree               its size, as GitHub Pages will publish it
WRITES  --report (JSON: what was checked, the sizes, every error and warning),
        and ::error:: / ::warning:: lines for the Actions log
EXITS   1 when the push must not happen:
          - the contract or the budget cannot be read;
          - a part is missing, is not gzipped JSON, has a wrong header, or comes
            from another run than part 1 (extracted_at, pipeline_commit);
          - an nct_id appears twice;
          - a record lacks a path the contract lists (a key may hold null or
            an empty list, which is how a record says "none"; it may not be
            absent);
          - a part is over GitHub's 100 MiB per-file push limit, or the
            published tree is over GitHub Pages' 1 GB limit.
        Being over the site's size budget (20 MiB a part today) only warns.
        That is the owner's policy: block missing fields; for size, warn
        until a hard limit, and the hard limits are the ones a push or a
        Pages deploy would hit.
INVOKED by .github/workflows/extract.yml in the publish step, after the files
        are staged in the site checkout and before git commit / git push. A
        failure leaves the site on last week's data and skips the steps that
        publish to the site after it (the sex/gender audit, the sponsor
        bridge). The week's full records and raw sex/gender measures are on
        their releases by then; those steps run before the site checkout.

The path rules are the site's (tests/study_record_contract.test.mjs there):
'status' is a key, 'race.reported' a key inside an object, and
'study_sites[].country' a key on every item of a list. The parts carry every
class today; when the split moves studies_tab and detail fields into their
own files, this check follows them there.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

# GitHub rejects a push with a file over 100 MiB. (The site serves its parts
# from GitHub Pages; the 20 MiB per-part figure in its budget is a budget,
# and only warns.)
PART_HARD_LIMIT_BYTES = 100 * 1024 * 1024
# GitHub Pages refuses to publish a site larger than 1 GB.
TREE_HARD_LIMIT_BYTES = 1_000_000_000
TREE_WARN_BYTES = 900_000_000
MAX_REPORTED = 20
EXAMPLES_PER_PATH = 3


def segments(path: str) -> list[tuple[str, bool]]:
    """'race.raw_categories[].omb_category' -> [(race, False), (raw_categories, True), (omb_category, False)]."""
    return [(s[:-2], True) if s.endswith("[]") else (s, False) for s in path.split(".")]


def missing(record: Any, path: str) -> str | None:
    """Why the record fails the path, or None when every reachable key is present."""
    level = [record]
    for key, each in segments(path):
        nxt: list[Any] = []
        for obj in level:
            if not isinstance(obj, dict) or key not in obj:
                return f"no {key}"
            value = obj[key]
            if each:
                if not isinstance(value, list):
                    return f"{key} is not a list"
                nxt.extend(value)
            else:
                nxt.append(value)
        level = nxt
    return None


def tree_size(root: str) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


def check(site: str, part_hard_limit: int = PART_HARD_LIMIT_BYTES,
          tree_hard_limit: int = TREE_HARD_LIMIT_BYTES, tree_warn: int = TREE_WARN_BYTES) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    # A missing path is counted per path, so one field gone from every record
    # reads as that, and every failing path is named once.
    missing_by_path: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"site": site, "errors": errors, "warnings": warnings,
                              "missing_by_path": missing_by_path}

    def err(msg: str) -> None:
        if len(errors) < MAX_REPORTED:
            errors.append(msg)

    try:
        with open(os.path.join(site, "tests", "record_contract.json"), encoding="utf-8") as f:
            contract = json.load(f)
        with open(os.path.join(site, "tests", "data_budget.json"), encoding="utf-8") as f:
            budget = json.load(f)
        paths = [p for cls in contract["classes"].values() for p in cls]
        part_count = int(budget["part_count"])
        part_budget = int(budget["part_gzip_max_bytes"])
        total_budget = int(budget["total_gzip_max_bytes"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        err(f"cannot read the site's record contract or data budget: {e}")
        report["ok"] = False
        return report
    report.update({"contract_paths": len(paths), "part_count": part_count,
                   "part_budget_bytes": part_budget, "total_budget_bytes": total_budget})

    data_dir = os.path.join(site, "data")
    present = sorted((f for f in os.listdir(data_dir) if re.fullmatch(r"demographics\.part\d+\.json\.gz", f)),
                     key=lambda f: int(re.search(r"part(\d+)", f).group(1)))
    expected = [f"demographics.part{i}.json.gz" for i in range(1, part_count + 1)]
    if present != expected:
        err(f"data/ holds {present}, not the {part_count} parts the site fetches")

    sizes = {f: os.path.getsize(os.path.join(data_dir, f)) for f in present}
    total = sum(sizes.values())
    report["parts_gzip_bytes"] = total
    for f, size in sizes.items():
        if size > part_hard_limit:
            err(f"{f} is {size:,} bytes, over GitHub's per-file push limit of {part_hard_limit:,}")
        elif size > part_budget:
            warnings.append(f"{f} is {size:,} bytes, over the site's per-part budget of {part_budget:,}")
    if total > total_budget:
        warnings.append(f"the parts total {total:,} bytes, over the site's budget of {total_budget:,}")

    first: dict[str, Any] | None = None
    seen: set[str] = set()
    records = 0
    for i, name in enumerate(expected, start=1):
        if name not in sizes:
            continue
        try:
            with gzip.open(os.path.join(data_dir, name), "rt", encoding="utf-8") as f:
                part = json.load(f)
        except (OSError, ValueError, EOFError) as e:
            err(f"{name} is not gzipped JSON: {e}")
            continue
        if not isinstance(part, dict):
            err(f"{name} is not a part: its JSON is a {type(part).__name__}, not an object")
            continue
        head = {k: part.get(k) for k in ("extracted_at", "pipeline_commit", "part", "total_parts")}
        if not isinstance(part.get("data"), list) or not part["data"]:
            err(f"{name} has no records")
            continue
        if head["part"] != i or head["total_parts"] != part_count:
            err(f"{name} says it is part {head['part']} of {head['total_parts']}, not {i} of {part_count}")
        if first is None:
            try:
                datetime.fromisoformat(str(head["extracted_at"]).replace("Z", "+00:00"))
            except ValueError:
                err(f"{name}: extracted_at is not a timestamp ({head['extracted_at']!r})")
            if not re.fullmatch(r"[0-9a-f]{7,40}", str(head["pipeline_commit"] or "")):
                err(f"{name}: pipeline_commit is not a commit id ({head['pipeline_commit']!r})")
            first = head
        elif (head["extracted_at"], head["pipeline_commit"]) != (first["extracted_at"], first["pipeline_commit"]):
            err(f"{name} comes from another run than part 1")
        for record in part["data"]:
            records += 1
            nct = record.get("nct_id") if isinstance(record, dict) else None
            if nct in seen:
                err(f"{nct} appears in more than one record ({name})")
            seen.add(nct)
            for path in paths:
                why = missing(record, path)
                if why:
                    entry = missing_by_path.setdefault(path, {"count": 0, "why": why, "examples": []})
                    entry["count"] += 1
                    if len(entry["examples"]) < EXAMPLES_PER_PATH:
                        entry["examples"].append(f"{nct or '(no nct_id)'} in {name}")
        del part
    report["records"] = records
    for path, entry in missing_by_path.items():
        n = entry["count"]
        errors.append(f"{path}: missing in {n:,} record{'' if n == 1 else 's'} ({entry['why']}); "
                      f"e.g. {', '.join(entry['examples'])}")

    tree = tree_size(site)
    report["tree_bytes"] = tree
    if tree > tree_hard_limit:
        err(f"the site would publish {tree:,} bytes, over GitHub Pages' limit of {tree_hard_limit:,}")
    elif tree > tree_warn:
        warnings.append(f"the site would publish {tree:,} bytes, close to GitHub Pages' limit of {tree_hard_limit:,}")

    report["error_count"] = len(errors)
    report["ok"] = not errors
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check this week's site files against the site's contract.")
    ap.add_argument("--site", required=True, help="the site checkout, with this week's files staged")
    ap.add_argument("--report", default=None, help="write the report here as JSON")
    ap.add_argument("--part-hard-limit-bytes", type=int, default=PART_HARD_LIMIT_BYTES)
    ap.add_argument("--tree-hard-limit-bytes", type=int, default=TREE_HARD_LIMIT_BYTES)
    ap.add_argument("--tree-warn-bytes", type=int, default=TREE_WARN_BYTES)
    a = ap.parse_args(argv)
    report = check(a.site, a.part_hard_limit_bytes, a.tree_hard_limit_bytes, a.tree_warn_bytes)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    for w in report["warnings"]:
        print(f"::warning::{w}")
    for e in report["errors"]:
        print(f"::error::{e}")
    mb = lambda b: f"{(b or 0) / 1e6:.1f} MB"  # noqa: E731
    print(f"site contract check: {'ok' if report['ok'] else 'FAILED'}; {report.get('records', 0):,} records, "
          f"parts {mb(report.get('parts_gzip_bytes'))} of {mb(report.get('total_budget_bytes'))} budget, "
          f"tree {mb(report.get('tree_bytes'))}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
