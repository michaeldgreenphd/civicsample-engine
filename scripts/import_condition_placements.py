#!/usr/bin/env python3
"""Write condition_placements.json from the private placements file.

Run from the repository root, with a checkout of the private repository:

    python3 scripts/import_condition_placements.py \\
        /path/to/civicsample-verify-ai-private/docs/mesh-area-labelling-20261108/placements.json \\
        --source-commit <that checkout's commit>

Only disease-area rows are carried over, with nct_id, area and source; see
src/condition_placements.py for why and for how the file is applied. The
script validates what it writes with the same check the generator uses, so a
file it writes is one the generator will accept.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import condition_placements as cp  # noqa: E402


def dumps(doc: dict) -> str:
    """Indented header, one placement per line, so a re-import diffs by trial."""
    head = json.dumps({k: v for k, v in doc.items() if k != "placements"}, indent=1)
    rows = ",\n".join("  " + json.dumps(r) for r in doc["placements"])
    return head[:-2] + ',\n "placements": [\n' + rows + "\n ]\n}\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="the private placements.json")
    ap.add_argument("--source-commit", required=True, help="the private repository commit the file was read at")
    ap.add_argument("--out", default=cp.DEFAULT_PATH, help="default: %(default)s")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", a.source_commit):
        raise SystemExit("--source-commit must be a full 40-character commit sha")
    with open(a.source, encoding="utf-8") as f:
        doc = json.load(f)
    out = cp.from_private(doc, cp.ontology_areas(), a.source_commit, cp.file_sha256(a.source))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(dumps(out))
    print(f"Wrote {a.out}: {out['counts']['trials']:,} placements {out['counts']['by_source']}")


if __name__ == "__main__":
    main()
