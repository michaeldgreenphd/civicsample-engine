"""Reviewed disease-area placements for trials the keyword ontology leaves
Uncategorized.

The keyword classifier (src/condition_classifier.py) files a trial whose
condition text matches no ontology keyword as primary "Other", secondary
"Uncategorized". On the 2026-11-01 data that was 4,647 of the 30,368
industry-cohort trials (15.3%). Their placements were worked out in the
private repository civicsample-verify-ai-private
(docs/mesh-area-labelling-20261108/) and reviewed there; this module carries
the result into the public analysis.

What the file holds, and what it does not
-----------------------------------------
Only trials placed in a disease area, each with the area and how it was
placed (`source`):

  mesh_map_v3     the trial's CT.gov MeSH condition terms
                  (derivedSection.conditionBrowseModule) reach the area
                  through the reviewed MeSH-to-area map, version 3;
  ai_label        no usable MeSH terms: the area was labelled from the
                  registry text by a model and audited by a second model,
                  and the audit's label is final. The model ids, prompts and
                  every answer are in the private experiment records named
                  in `provenance.experiments`;
  procedure_rule  the audited label was UNCERTAIN and a reviewed
                  condition-text rule names the area (dental, joint and
                  limb, bowel surgery, eye, urology, cardiac device, ear).

Trials placed nowhere are left out on purpose: "No disease area"
(healthy-volunteer studies and others that study no disease), "Other"
(MeSH terms that reach no site area), "Uncertain" and "Missing". They stay
Other / Uncategorized, exactly as before. How the site should show the
no-disease group is an open decision, and this file does not make it.

When a placement applies
------------------------
Only when the trial's record is still primary "Other", secondary
"Uncategorized" (`APPLIES_WHEN`). The placements were computed on one week's
data. If the keyword classifier now places a trial, the classifier's category
stands and the placement is counted as not applied; a trial that became
Uncategorized after that week has no placement and stays Uncategorized. The
secondary category stays "Uncategorized": a placement names an area, not a
sub-category, and the site's heatmap axis already leaves "Uncategorized" out.

Every check raises; nothing is skipped quietly. A malformed file stops the
build rather than publishing a partial set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from typing import Any, Iterable

FORMAT = "civicsample-condition-placements/1"
PRIVATE_FORMAT = "civicsample-uncategorized-placements/1"
APPLIES_WHEN = ("Other", "Uncategorized")
SOURCES = ("mesh_map_v3", "ai_label", "procedure_rule")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "condition_placements.json")
ONTOLOGY_PATH = os.path.join(ROOT, "condition_ontology.json")


class PlacementError(ValueError):
    """The placements file is not one this code can apply safely."""


# The provenance a published placement set must carry: without these an
# output cannot be traced back to the reviewed private file it came from.
PROVENANCE_FIELDS = {
    "repository": re.compile(r"[A-Za-z0-9_.-]+"),
    "commit": re.compile(r"[0-9a-f]{40}"),
    "file": re.compile(r"\S.*"),
    "file_sha256": re.compile(r"[0-9a-f]{64}"),
}


def check_provenance(prov: Any) -> None:
    """Raise unless `prov` names the source repository, a full commit sha,
    the source file and its sha256."""
    if not isinstance(prov, dict):
        raise PlacementError("no provenance block")
    for key, shape in PROVENANCE_FIELDS.items():
        value = prov.get(key)
        if not isinstance(value, str) or not shape.fullmatch(value):
            raise PlacementError(f"provenance.{key} is missing or malformed: {value!r}")


def ontology_areas(path: str = ONTOLOGY_PATH) -> set[str]:
    """The site's primary categories. A placement must name one exactly,
    because the site filters industry rows by primary name."""
    with open(path, encoding="utf-8") as f:
        return set(json.load(f)["categories"])


def check(doc: dict[str, Any], areas: set[str]) -> dict[str, dict[str, str]]:
    """{nct_id: {"area", "source"}} from a parsed placements file, or raise."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise PlacementError(f"not a {FORMAT} file")
    if doc.get("applies_when") != {"primary": APPLIES_WHEN[0], "secondary": APPLIES_WHEN[1]}:
        raise PlacementError("applies_when must be primary Other, secondary Uncategorized")
    check_provenance(doc.get("provenance"))
    rows = doc.get("placements")
    if not isinstance(rows, list) or not rows:
        raise PlacementError("no placements")
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"nct_id", "area", "source"}:
            raise PlacementError(f"a placement must have exactly nct_id, area and source: {row!r}")
        nct, area, source = row["nct_id"], row["area"], row["source"]
        if not (isinstance(nct, str) and nct.startswith("NCT") and nct[3:].isdigit() and len(nct) == 11):
            raise PlacementError(f"not an NCT id: {nct!r}")
        if nct in out:
            raise PlacementError(f"{nct} is placed twice")
        if area not in areas:
            raise PlacementError(f"{nct}: {area!r} is not a primary category of the ontology")
        if source not in SOURCES:
            raise PlacementError(f"{nct}: unknown source {source!r}")
        out[nct] = {"area": area, "source": source}
    counts = doc.get("counts")
    if counts != summarize(out):
        raise PlacementError("the counts block does not match the placements")
    return out


def summarize(placements: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Counts by source and by area, as the file states them."""
    return {"trials": len(placements),
            "by_source": dict(sorted(Counter(p["source"] for p in placements.values()).items())),
            "by_area": dict(sorted(Counter(p["area"] for p in placements.values()).items()))}


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load(path: str = DEFAULT_PATH, areas: set[str] | None = None) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """(placements, provenance) from the committed file. Provenance carries
    the file's own sha256 so an output names exactly what it applied."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    placements = check(doc, ontology_areas() if areas is None else areas)
    return placements, {"format": FORMAT, "file_sha256": file_sha256(path),
                        "provenance": doc.get("provenance")}


def apply(rows: Iterable[dict[str, Any]], placements: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Set `primary` on cohort rows that are still Other / Uncategorized and
    have a placement. Rows need `nct_id`, `primary` and `secondary`; the
    secondary is left as it is. Returns what applied and what did not."""
    rows = list(rows)
    in_cohort: set[str] = set()
    applied: Counter[str] = Counter()
    applied_area: Counter[str] = Counter()
    categorized_now = 0
    uncategorized_left = 0
    for r in rows:
        nct = r["nct_id"]
        in_cohort.add(nct)
        p = placements.get(nct)
        if (r["primary"], r["secondary"]) != APPLIES_WHEN:
            if p is not None:
                categorized_now += 1          # the keyword classifier places it now; its category stands
            continue
        if p is None:
            uncategorized_left += 1
            continue
        r["primary"] = p["area"]
        applied[p["source"]] += 1
        applied_area[p["area"]] += 1
    return {
        "applied": sum(applied.values()),
        "applied_by_source": dict(sorted(applied.items())),
        "applied_by_area": dict(sorted(applied_area.items())),
        "not_applied": {"not_in_cohort": len(set(placements) - in_cohort),
                        "categorized_by_keywords_now": categorized_now},
        "uncategorized_remaining": uncategorized_left,
    }


def from_private(doc: dict[str, Any], areas: set[str], source_commit: str, source_sha256: str) -> dict[str, Any]:
    """The public file from the private placements file: disease-area rows
    only, with nct_id, area and source, and nothing from the labelling
    records (no quotations, rationales or answers)."""
    if not isinstance(doc, dict) or doc.get("format") != PRIVATE_FORMAT:
        raise PlacementError(f"not a {PRIVATE_FORMAT} file")
    # Every private row is checked for a repeated id, not only the disease-area
    # ones carried over: a repeat means the source is not one row per trial,
    # and keying by id below would keep one of the two without saying so.
    seen: set[str] = set()
    for r in doc["rows"]:
        if r["nct_id"] in seen:
            raise PlacementError(f"{r['nct_id']} appears twice in the private placements")
        seen.add(r["nct_id"])
    rows = sorted(({"nct_id": r["nct_id"], "area": r["placement"], "source": r["source"]}
                   for r in doc["rows"] if r["placement"] in areas), key=lambda r: r["nct_id"])
    for r in rows:
        if r["source"] not in SOURCES:
            raise PlacementError(f"{r['nct_id']}: a disease-area placement from {r['source']!r}")
    placements = {r["nct_id"]: {"area": r["area"], "source": r["source"]} for r in rows}
    out = {
        "format": FORMAT,
        "applies_when": {"primary": APPLIES_WHEN[0], "secondary": APPLIES_WHEN[1]},
        "provenance": {
            "repository": "civicsample-verify-ai-private",
            "commit": source_commit,
            "file": "docs/mesh-area-labelling-20261108/placements.json",
            "file_sha256": source_sha256,
            "population": ("industry-cohort trials (scripts/generate_industry_sponsors.py build_cohort) "
                           "that the keyword ontology left Other / Uncategorized in the 2026-11-01 data"),
            "population_trials": doc["trials"],
            "population_by_placement": doc["by_placement"],
            "experiments": {k: {"ledger_tip": v["ledger_tip"]} for k, v in sorted(doc["experiments"].items())},
        },
        "counts": summarize(placements),
        "placements": rows,
    }
    check(out, areas)
    return out
