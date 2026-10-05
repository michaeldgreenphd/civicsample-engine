"""The week's full study records: data/demographics.json.

`src.extract_all` writes it, `scripts/build_sex_gender_table.py --write-back`
rewrites it in place with the lean sex/gender rows, and the weekly job keeps
it permanently, gzipped, on that week's `data-YYYY-MM-DD` release (every week
from 2026-08-28 on; earlier weeks' full records exist only as the parts in
the site repository's git history).

The site's files are cut from it by `scripts/split_data.py` for the
dashboard, as the site's own record contract says. They are not a stand-in
for it: once the site turns its split layout on they carry only the fields
the site reads, so a script that needs a field the dashboard does not show
(sponsors, status, ages, reference counts, intervention descriptions) must
read this file. `load` therefore never falls back to the parts; a missing
file is an error that says where the full records are.
"""
from __future__ import annotations

import gzip
import json
import os
from typing import Any

DEFAULT_PATH = "data/demographics.json"

WHERE_THEY_ARE = (
    "the weekly job writes it (src.extract_all). For an earlier week, download "
    "that week's copy and pass it with --demographics: "
    "gh release download data-YYYY-MM-DD -p demographics.json.gz "
    "-R michaeldgreenphd/civicsample-engine, then --demographics demographics.json.gz "
    "(releases start at data-2026-08-28; before that, the full records are the "
    "parts in that week's 'Update demographics data' commit in the site repository)"
)


def load(path: str | os.PathLike[str] | None = DEFAULT_PATH) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return (records, extracted_at, pipeline_commit) from a full-record file.

    Accepts the plain JSON the weekly job writes or the gzipped copy on the
    release. Exits with a message when the file is missing or holds no
    records: an output built from nothing would publish as if the registry
    were empty.
    """
    if not path or not os.path.exists(path):
        raise SystemExit(
            f"{path or '(no path)'}: full study records not found. This script needs the "
            f"full records, not the site's demographics.part*.json.gz; {WHERE_THEY_ARE}."
        )
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        container = json.load(f)
    records = container.get("data") if isinstance(container, dict) else None
    if not records:
        raise SystemExit(f"{path}: holds no study records")
    return records, container.get("extracted_at"), container.get("pipeline_commit")
