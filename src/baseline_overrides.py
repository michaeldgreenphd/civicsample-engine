"""Per-trial readings of a baseline module the general rules cannot make.

A few trials post a baseline module that no general rule reads correctly and
that one documented exception can. Each exception here names the trial, the
posted structure it was written for (pinned, so a registry update cannot be
read under a stale rule), what it changes and why. apply_baseline_override()
returns the study as the engine's own demographic readers see it; every other
reader, and the record's metadata, keep the study as posted.

Applied by src.extract_all.extract_demographics_from_study, before the race,
ethnicity, legacy sex and gender extractors and the three breakdowns. Not
applied to the sex/gender manuscript parser (src.sex_gender_table and
src.sex_gender_analysis) or its retained raw measures: that parser is vendored
from the manuscript (AGENTS.md), and a change to what it reads goes into the
manuscript bundle first.

Provenance. A block read through an exception carries the flag
baseline_override. When the posted module no longer matches the pinned
structure, the exception is not applied, the module is read as posted, the
blocks carry baseline_override_not_applied and a warning is logged: absence
of the reading is recorded, never silent.

── NCT07465926: four matched comparisons sharing two treated cohorts ────────

A real-world study of adults with obesity, type 2 diabetes and
cardiovascular-kidney-metabolic stage 2-3, posting eight baseline groups in
four matched comparisons and a Total column, BG008, of 224,048: the sum of the
eight groups (denominators 17,146 x 2, 40,152 x 2, 19,202 x 2, 35,524 x 2).
Every row of every table is tagged with its comparison ("White (Comparison 1)",
classes "Comparison 1".."Comparison 4" in the ethnicity table), and carries
counts only in that comparison's two groups. Read as posted, the race rows
named no race and all 224,048 were counted as other; ethnicity and sex summed
the Total column.

The comparisons share treated cohorts, so the Total counts people twice. By
the group descriptions, BG000 and BG002 ("GLP-1 RA With SGLT2i Add-On",
Comparisons 1 and 2) are matched copies of one treated cohort, and BG004 and
BG006 ("SGLT2i With GLP-1 RA Add-On", Comparisons 3 and 4) of another. The
four comparators (BG001 GLP-1 RA with DPP-4i/SU add-on, BG003 GLP-1 RA only,
BG005 SGLT2i with DPP-4i/SU add-on, BG007 SGLT2i only) are distinct treatment
paths, disjoint by definition.

Reading (the maintainer's choice, 2026-11-21, among three posed: the Total
column, 224,048 with people counted twice; this one; and Comparisons 2 and 4
only, 151,352). BG000 and BG004, the smaller copy of each treated cohort, are
dropped; the larger copy (BG002, BG006) and all four comparators are kept. The
Total column of every count table is recomputed as the sum of the kept six
groups, the denominators likewise (187,700), and rows (and classes) that
differ only by their comparison tag are merged, their kept groups summed per
group. Every table reads as one table of 187,700 participants.

Limits. 187,700 is a lower bound on the distinct people: the matching selects
different subsets of each treated cohort, so some people in BG000 or BG004 may
not be in BG002 or BG006, and the true number lies between 187,700 and
224,048. The disjointness of the kept groups rests on the group descriptions;
the counts cannot confirm it. Composition hardly moves between the readings
(White 66.9% of the Total column, 66.8% here, 66.5% for Comparisons 2 and 4;
Black 17.4%, 17.4%, 17.5%). The registry's enrollment, 451,036, is not changed.

Read through this exception, on the trial as posted in the 2026-11-15
snapshot (re-fetched 2026-11-18): race White 125,294, Black 32,705, Asian
7,313, "Other Races" 6,517 and "Other, Unknown, or Not Reported" 15,871;
ethnicity Hispanic or Latino 20,548, not Hispanic or Latino 135,074, unknown
32,078; sex female 97,899, male 89,520, unknown 281.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

APPLIED_FLAG = "baseline_override"
NOT_APPLIED_FLAG = "baseline_override_not_applied"

# A comparison tag at the end of a row or class title ("White (Comparison 1)"),
# or a class title that is only the tag ("Comparison 1").
_COMPARISON_SUFFIX_RX = re.compile(r"\s*\(\s*comparison\s+\d+\s*\)\s*$", re.IGNORECASE)
_COMPARISON_ONLY_RX = re.compile(r"^\s*comparison\s+\d+\s*$", re.IGNORECASE)

# The exceptions: the posted groups (id, title) and denominators each was
# written for, the groups dropped and the group holding the Total column.
OVERRIDES: Dict[str, Dict[str, Any]] = {
    "NCT07465926": {
        "groups": (
            ("BG000", "GLP-1 RA With SGLT2i Add-On (Comparison 1)"),
            ("BG001", "GLP-1 RA With DPP-4i/SU Add-On (Comparison 1)"),
            ("BG002", "GLP-1 RA With SGLT2i Add-On (Comparison 2)"),
            ("BG003", "GLP-1 RA Only (No Early Add-On) (Comparison 2)"),
            ("BG004", "SGLT2i With GLP-1 RA Add-On (Comparison 3)"),
            ("BG005", "SGLT2i With DPP-4i/SU Add-On (Comparison 3)"),
            ("BG006", "SGLT2i With GLP-1 RA Add-On (Comparison 4)"),
            ("BG007", "SGLT2i Only (No Early Add-On) (Comparison 4)"),
            ("BG008", "Total"),
        ),
        "denoms": {"BG000": 17146, "BG001": 17146, "BG002": 40152, "BG003": 40152, "BG004": 19202,
                   "BG005": 19202, "BG006": 35524, "BG007": 35524, "BG008": 224048},
        "drop": ("BG000", "BG004"),
        "total": "BG008",
    },
}


def _nct_id(study: dict) -> Optional[str]:
    return (((study or {}).get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")


def _module(study: dict) -> dict:
    return ((study or {}).get("resultsSection") or {}).get("baselineCharacteristicsModule") or {}


def _count(value: Any) -> Optional[int]:
    """A posted count as an integer; None when it is not a whole number."""
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _posted_denoms(module: dict) -> Dict[str, Optional[int]]:
    counts: Dict[str, Optional[int]] = {}
    for denom in module.get("denoms") or []:
        if str(denom.get("units", "")).lower() == "participants":
            for c in denom.get("counts") or []:
                counts[c.get("groupId")] = _count(c.get("value"))
    return counts


def _mismatch(module: dict, spec: Dict[str, Any]) -> Optional[str]:
    """Why the posted module is not the structure the exception was written
    for, or None when it is."""
    groups = tuple((g.get("id") or g.get("groupId"), g.get("title")) for g in module.get("groups") or [])
    if groups != spec["groups"]:
        return "baseline groups differ from the pinned ones"
    if _posted_denoms(module) != spec["denoms"]:
        return "baseline denominators differ from the pinned ones"
    for measure in module.get("measures") or []:
        if measure.get("paramType") != "COUNT_OF_PARTICIPANTS":
            return f"measure {measure.get('title')!r} does not count participants"
        for cls in measure.get("classes") or []:
            for cat in cls.get("categories") or []:
                for m in cat.get("measurements") or []:
                    if _count(m.get("value")) is None:
                        return f"measure {measure.get('title')!r} posts a value that is not a count"
    return None


def _untagged(title: Optional[str]) -> Optional[str]:
    """A row or class title without its comparison tag; None when nothing
    else remains."""
    if title is None or _COMPARISON_ONLY_RX.match(title):
        return None
    return _COMPARISON_SUFFIX_RX.sub("", title) or None


def _merged_measure(measure: dict, kept: List[str], total: str) -> dict:
    """The measure with the dropped groups removed, rows and classes that
    differ only by their comparison tag merged (kept groups summed per group),
    and the Total column the sum of the kept groups."""
    classes: Dict[Optional[str], Dict[Optional[str], Dict[str, int]]] = {}
    for cls in measure.get("classes") or []:
        rows = classes.setdefault(_untagged(cls.get("title")), {})
        for cat in cls.get("categories") or []:
            sums = rows.setdefault(_untagged(cat.get("title")), {gid: 0 for gid in kept})
            for m in cat.get("measurements") or []:
                if m.get("groupId") in sums:
                    sums[m["groupId"]] += _count(m.get("value")) or 0

    out = {k: v for k, v in measure.items() if k not in ("classes", "denoms")}
    out["classes"] = []
    for class_title, rows in classes.items():
        cls_out: Dict[str, Any] = {} if class_title is None else {"title": class_title}
        cls_out["categories"] = []
        for row_title, sums in rows.items():
            cat_out: Dict[str, Any] = {} if row_title is None else {"title": row_title}
            cat_out["measurements"] = ([{"groupId": gid, "value": str(sums[gid])} for gid in kept]
                                       + [{"groupId": total, "value": str(sum(sums.values()))}])
            cls_out["categories"].append(cat_out)
        out["classes"].append(cls_out)
    return out


def apply_baseline_override(study: dict) -> Tuple[dict, Optional[str]]:
    """The study as the engine's own demographic readers see it, and the flag
    the blocks read from it carry (APPLIED_FLAG, NOT_APPLIED_FLAG, or None
    when the trial has no exception). The study passed in is never changed.
    Module docstring: the exceptions, their reasons and limits."""
    nct = _nct_id(study)
    spec = OVERRIDES.get(nct or "")
    if spec is None:
        return study, None
    module = _module(study)
    why = _mismatch(module, spec)
    if why is not None:
        logger.warning(f"[{nct}] baseline override not applied ({why}); read as posted")
        return study, NOT_APPLIED_FLAG

    kept = [gid for gid, _title in spec["groups"] if gid not in spec["drop"] and gid != spec["total"]]
    total = spec["total"]
    read = copy.deepcopy(study)
    read_module = read["resultsSection"]["baselineCharacteristicsModule"]
    read_module["groups"] = [g for g in read_module["groups"] if (g.get("id") or g.get("groupId")) not in spec["drop"]]
    kept_denoms = {gid: spec["denoms"][gid] for gid in kept}
    read_module["denoms"] = [{"units": "Participants",
                              "counts": ([{"groupId": gid, "value": str(n)} for gid, n in kept_denoms.items()]
                                         + [{"groupId": total, "value": str(sum(kept_denoms.values()))}])}]
    read_module["measures"] = [_merged_measure(m, kept, total) for m in read_module.get("measures") or []]
    return read, APPLIED_FLAG


def flag_blocks(blocks: List[Optional[dict]], flag: Optional[str]) -> None:
    """Add the override's flag to each demographic block read from the study,
    keeping the extractors' sorted, de-duplicated flag lists."""
    if flag is None:
        return
    for block in blocks:
        if block is not None:
            block["flags"] = sorted(set(block.get("flags") or []) | {flag})
