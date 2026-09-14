#!/usr/bin/env python3
"""Write the methods-page text for the sex/gender table (backend-served).

READS   data/sex_gender_parsed_meta.json (this week's counts and provenance) and,
        when present, data/sex_gender_audit/side_by_side.json (the measured
        old-vs-new figures) so the text cites measured numbers, not estimates.
WRITES  data/sex_gender/methods.json   sections the dashboard will render, plus
                                       parser_rules_version and provenance
        data/sex_gender/methods.md     the same text, readable on GitHub
INVOKED by .github/workflows/extract.yml after build_sex_gender_table.py; the
        site copies data/sex_gender/ verbatim. Not yet linked from the UI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sex_gender_parser as sgp  # noqa: E402
from src.utils import pipeline_commit  # noqa: E402

FIDELITY = {"status_exact": 66177, "status_total": 66210, "status_pct": "99.95%",
            "extract_date": "2026-06-09"}


def sections(meta: dict, sbs: dict | None) -> list:
    rules = sgp.PARSER_RULES_VERSION
    counts = (meta.get("status_counts") or {}).get("status") or {}
    inferred_share = None
    if sbs and sbs.get("sex_unknown_inferred_share_pct") is not None:
        inferred_share = f"{sbs['sex_unknown_inferred_share_pct']:.1f}%"
    tile_note = (f"In the last snapshot published under the retired rule, {inferred_share} of the participants "
                 f"in the Unknown tile were that inferred remainder, so the tile shrinks by about that share at cutover."
                 if inferred_share else
                 "In the 2026-09-06 snapshot published under the retired rule, 87.7% of the participants in the "
                 "Unknown tile were that inferred remainder, so the tile shrinks by roughly that share at cutover.")
    return [
        {"id": "source", "heading": "Where the sex and gender numbers come from",
         "text": ("Every count on the Sex and Gender tabs is produced by the deterministic parser from our sex and "
                  "gender manuscript, applied to each trial's baseline-characteristics tables on ClinicalTrials.gov "
                  "(API v2). A baseline measure is a candidate sex or gender table if, and only if, its title contains "
                  "\"sex\" or \"gender\". Labels are mapped to five buckets (Female, Male, Explicit Unknown, Gender "
                  "diverse, Cis/trans-qualified) by an ordered vocabulary; nothing is inferred from enrollment.")},
        {"id": "states", "heading": "The five reporting states",
         "text": ("Every trial with posted results is in exactly one state. Reported: at least one Female, Male, gender "
                  "diverse or cis/trans-qualified count above zero. Explicit Unknown only: the sponsor posted a sex or "
                  "gender table whose only non-zero category is Unknown (or a synonym such as Not reported, Missing, "
                  "Prefer not to answer). Uninformative: a sex or gender table was posted but carries no usable count "
                  "(an empty template, every value NA, every value zero, or no recognizable label); the reason and "
                  "any free-text \"not collected\" note are shown as sub-labels. Not Reported (Missing): the trial has "
                  "no sex- or gender-titled baseline table at all. Parse error: the record could not be read; these "
                  "trials are counted on the audit page and nowhere else. These states are never merged, and "
                  "\"Not Reported (Missing)\" is never computed from enrollment arithmetic.")},
        {"id": "gender_rule", "heading": "What counts as reporting gender",
         "text": ("A trial reports gender only if it posted a gender-diverse category (for example Non-binary, "
                  "Genderqueer, Transgender, Two-Spirit, Intersex) or a cis/trans-qualified category (for example "
                  "Transgender Female, Cisgender Man) with a non-zero count. A table titled \"Gender\" that carries "
                  "only Female and Male, or only Woman and Man, is sex data under a gender label: it is counted as "
                  "reported sex, and flagged as gender-labeled binary only. Cis/trans-qualified categories are never "
                  "added to Female or Male; they are shown as their own bucket with the source labels on drill-down.")},
        {"id": "other", "heading": "Why a bare \"Other\" counts as gender diverse",
         "text": ("A category labelled simply \"Other\" in a sex or gender table is counted toward the gender-diverse "
                  "bucket, because in these tables it is the sponsor's catch-all for identities outside Female and "
                  "Male and the manuscript's vocabulary accepted it as such; it is the single largest source of "
                  "gender reporting, and the source labels are listed on drill-down so a reader can see how much of "
                  "the bucket it is.")},
        {"id": "units", "heading": "Which tables enter the composition and percent-female figures",
         "text": ("Reporting status counts every sex or gender table, but participant composition and percent female "
                  "use only tables that count participants: a table whose values are counts of units (tests, eyes, "
                  "fractures), means, medians or percentages is kept in the reporting counts and excluded from "
                  "composition (is_participant_count is false).")},
        {"id": "percent_female", "heading": "How percent female is calculated",
         "text": ("Two series are shown, over the same trials: those that report sex with a participant-count table "
                  "and have at least one Female or Male participant. The primary series is the average across trials "
                  "of each trial's own female share, female / (female + male); the secondary series is participant-"
                  "weighted, the sum of female participants over the sum of female plus male participants. Gender-"
                  "diverse and cis/trans-qualified participants are excluded from both denominators, and explicitly "
                  "Unknown participants are never counted as missing.")},
        {"id": "unknown_tile", "heading": "Why the Explicit Unknown tile is smaller than the old Unknown tile",
         "text": ("The Explicit Unknown tile holds only participants the sponsor placed in an Unknown category. The "
                  "retired pipeline added the gap between registered enrollment and the sum of the posted table to the "
                  "same tile (\"denominator balancing\"). That gap is now stored separately as enrollment minus parsed, "
                  "is shown nowhere as unknown, and never affects a trial's reporting state. " + tile_note)},
        {"id": "declared_not_collected", "heading": "Trials that say sex was not collected",
         "text": ("A few trials post a sex table with no participants and a note that sex was not collected. They are "
                  "filed as Uninformative with the declared-not-collected sub-label. In the manuscript's 2026-06-09 "
                  "extract three such trials (NCT06668909, NCT07280208, NCT05591014) were filed as Not Reported by a "
                  "manual step the dashboard does not replicate.")},
        {"id": "fidelity", "heading": "How closely the parser matches the manuscript's manual review",
         "text": (f"On the manuscript's {FIDELITY['extract_date']} extract of {FIDELITY['status_total']:,} trials with a "
                  f"sex- or gender-titled table, the parser's reporting state agreed with the manually corrected ground "
                  f"truth for {FIDELITY['status_exact']:,} trials ({FIDELITY['status_pct']}). Treat that as the ceiling "
                  "of a deterministic parser; the 33 disagreements are listed with the parser bundle.")},
        {"id": "series_break", "heading": "Why the trend charts start a new series",
         "text": ("Snapshots published before the parser cutover were produced by the retired rule, and the stored "
                  "records from those weeks do not hold the raw tables, so they cannot be re-parsed. The Sex and Gender "
                  "trend charts therefore start a new series at the first pull under the parser; earlier points are "
                  "labelled as produced by the retired rule and the two series are not spliced. From the cutover on, "
                  "the selected raw tables are retained with every weekly backup so any future rule change can be "
                  "re-applied to every snapshot.")},
        {"id": "race_ethnicity", "heading": "Race and ethnicity still use the earlier rule",
         "text": ("The Race and Ethnicity tabs keep the earlier pipeline, including denominator balancing, in this "
                  "release; their quality charts therefore still derive the missing layer from enrollment. The same "
                  "three-state separation is scheduled for those dimensions separately.")},
        {"id": "version", "heading": "Version",
         "text": (f"Parser rules version: {rules}. Parser module version: {sgp.__version__}. "
                  f"Current snapshot reporting states: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()) + "."
                  if counts else f"Parser rules version: {rules}. Parser module version: {sgp.__version__}.")},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="data/sex_gender_parsed_meta.json")
    ap.add_argument("--side-by-side", default="data/sex_gender_audit/side_by_side.json")
    ap.add_argument("--out-dir", default="data/sex_gender")
    a = ap.parse_args()
    meta = json.load(open(a.meta)) if os.path.exists(a.meta) else {}
    sbs = json.load(open(a.side_by_side)) if os.path.exists(a.side_by_side) else None
    secs = sections(meta, sbs)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_commit": pipeline_commit(),
        "source_extracted_at": meta.get("source_extracted_at"),
        "snapshot_date": meta.get("snapshot_date"),
        "parser_rules_version": sgp.PARSER_RULES_VERSION,
        "parser_module_version": sgp.__version__,
        "fidelity": FIDELITY,
        "sections": secs,
    }
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "methods.json"), "w") as fh:
        json.dump(doc, fh, indent=2)
    with open(os.path.join(a.out_dir, "methods.md"), "w") as fh:
        fh.write("# Sex and gender: methods\n\n")
        fh.write(f"Parser rules version `{sgp.PARSER_RULES_VERSION}` (module {sgp.__version__}); "
                 f"snapshot {meta.get('snapshot_date')}; generated {doc['generated_at']}.\n\n")
        for s in secs:
            fh.write(f"## {s['heading']}\n\n{s['text']}\n\n")
    print(f"wrote {a.out_dir}/methods.json and methods.md ({len(secs)} sections, rules {sgp.PARSER_RULES_VERSION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
