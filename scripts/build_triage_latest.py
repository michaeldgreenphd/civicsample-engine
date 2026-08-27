#!/usr/bin/env python3
"""Derive the Approval-Queue triage CSVs from the committed extraction JSON.

The Civic Sample triage inbox (the "(Beta) Approval Queue" tab) is spec'd to
read two single-model CSVs written by ``extraction_pipeline/{fda,lit}_extractor.py``:

    data/fda_extracted_latest.csv
    data/lit_extracted_latest.csv

Those scripts are *not* wired into CI today — the committed data instead comes
from the multi-model comparison pipeline (``scripts/extraction/*.py``), which
writes:

    data/fda_demographics_extracted.json   (multi-model, per-model `data`)
    data/lit_ses_extracted.json            (AI/ML manuscripts, no NCT)
    data/trials_lit_extracted.json         (clinical-trial manuscripts, real NCT + tier)

This bridge reshapes those *real* extractions into the documented CSV schema so
the triage inbox has genuine, auditable content to review. It does NOT
fabricate values: every value carries through from the JSON, and any field the
extractor left blank is honestly encoded as the schema sentinels
(``-1`` for missing integers, ``"Not Reported"`` for missing strings). The
``extraction_pipeline`` single-model CSVs remain the canonical source — running
that pipeline overwrites these files with first-party output in the identical
schema.

Provenance: for each document we surface the model that reported the most
fields (ties broken by capability: Opus > Sonnet > Haiku > Gemini Pro > Flash),
and stamp the row's ``model`` column with that model's id/label so the UI's
model filter and provenance tags reflect a real extraction.

Usage:
    python scripts/build_triage_latest.py
"""

import csv
import json
import os
import sys

DATA_DIR = "data"

# Capability ranking used only as a tie-breaker when two models reported the
# same number of fields. Higher = preferred. Unknown models sort to 0.
_MODEL_RANK = {
    "opus": 50, "sonnet": 40, "haiku": 30,
    "gemini-3.5-pro": 26, "gemini-3.1-pro": 25, "gemini-3-pro": 24,
    "gemini-2.5-pro": 23, "gemini-3.5-flash": 16, "gemini-3-flash": 15,
    "gemini-2.5-flash": 14, "flash-lite": 5,
}

NOT_REPORTED = "Not Reported"


def _model_rank(model_id):
    mid = (model_id or "").lower()
    best = 0
    for frag, rank in _MODEL_RANK.items():
        if frag in mid and rank > best:
            best = rank
    return best


def as_dict(node):
    """Return `node` if it's a dict, else an empty dict.

    Breakdown fields (sex / race / ses / ethnicity) are sometimes collapsed to
    the bare string ``"Not Reported"`` by the extractor when nothing was found,
    so callers can't assume a dict.
    """
    return node if isinstance(node, dict) else {}


def unwrap(node):
    """Return the scalar value from an evidence-wrapped leaf.

    The extractors emit ``{"value": X, "exact_quote": "...", "page_number": N}``
    for most leaves; some legacy payloads store the bare scalar. Return X in
    either case.
    """
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def is_nr(value):
    """True if `value` is a missing/Not-Reported sentinel in any of its forms."""
    v = unwrap(value)
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() == "not reported":
            return True
        if s.lstrip("-").isdigit():
            return int(s) == -1
        return False
    if isinstance(v, (int, float)):
        return v == -1
    if isinstance(v, list):
        return len(v) == 0
    return False


def as_int(value):
    """Coerce a value/evidence-leaf to an int count, or -1 when not reported."""
    v = unwrap(value)
    if is_nr(v):
        return -1
    if isinstance(v, bool):
        return -1
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return -1


def as_str(value):
    """Coerce a value/evidence-leaf to a clean string, or 'Not Reported'."""
    v = unwrap(value)
    if is_nr(v):
        return NOT_REPORTED
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def clean_quote(text):
    """Normalise an evidence quote; treat content-free quotes as Not Reported.

    Models occasionally emit punctuation-only evidence (e.g. a stray ``""``)
    when they find nothing. Anything with no alphanumeric character is not a
    real quote, so collapse it to the Not Reported sentinel.
    """
    if not isinstance(text, str):
        return NOT_REPORTED
    s = text.strip()
    if not s or not any(c.isalnum() for c in s):
        return NOT_REPORTED
    return s


def evidence_of(data, key):
    """Resolve the verbatim evidence quote for `key` within an extracted `data`.

    Checks, in order: a sibling ``<key>_evidence`` string, then the leaf's own
    ``exact_quote``. Returns 'Not Reported' when neither holds a real quote so
    the UI always has a string to render.
    """
    sib = clean_quote(data.get(f"{key}_evidence"))
    if sib != NOT_REPORTED:
        return sib
    node = data.get(key)
    if isinstance(node, dict):
        q = clean_quote(node.get("exact_quote"))
        if q != NOT_REPORTED:
            return q
    return NOT_REPORTED


def get_extracted(model_entry):
    """Return the per-model extracted payload, tolerating both layouts.

    FDA records put demographic fields directly under ``data``; the literature
    records nest them under ``data.extracted_data``.
    """
    data = (model_entry or {}).get("data") or {}
    if isinstance(data.get("extracted_data"), dict):
        return data["extracted_data"]
    return data


# Probe fields used to score how much a given model actually reported, so we
# surface the richest real extraction per document.
_FDA_PROBES = ["device_name", "panel", "total_participants",
               "target_patient_age_range", "sex", "race_nih_omb"]
_LIT_PROBES = ["total_participants", "study_design", "sex", "race_nih_omb",
               "socioeconomic_status", "associated_nct_ids"]


def _reported_score(extracted, probes):
    score = 0
    for key in probes:
        node = extracted.get(key)
        if node is None:
            continue
        if isinstance(node, dict) and "value" not in node:
            # Breakdown object (sex / race / ses): count reported sub-leaves.
            if any(not is_nr(v) for k, v in node.items()
                   if not k.endswith("_evidence")):
                score += 1
        elif not is_nr(node):
            score += 1
    return score


def pick_primary_model(record, probes):
    """Choose the model whose extraction to surface for this document.

    Prefers the model that reported the most probe fields; ties broken by
    capability rank. Skips models whose call failed. Returns (key, entry).
    """
    models = record.get("models") or {}
    best_key, best_entry, best = None, None, (-1, -1)
    for key, entry in models.items():
        if entry.get("call_succeeded") is False:
            continue
        extracted = get_extracted(entry)
        rank = (_reported_score(extracted, probes), _model_rank(entry.get("model_id") or entry.get("model")))
        if rank > best:
            best, best_key, best_entry = rank, key, entry
    if best_entry is None and models:  # everything failed — fall back to any
        best_key, best_entry = next(iter(models.items()))
    return best_key, (best_entry or {})


# NIH/OMB race categories -> the 7 mutually-exclusive Civic Trial buckets the
# FDA tool schema (race.white/black/asian/hispanic/native_american/other/unknown)
# uses. "other" aggregates Pacific Islander + multiracial; hispanic is sourced
# from the ethnicity object when present (NIH/OMB models ethnicity separately).
def map_race_buckets(extracted):
    src = extracted.get("race_nih_omb")
    eth = extracted.get("ethnicity")
    if not isinstance(src, dict):
        src = {}
    if not isinstance(eth, dict):
        eth = {}

    def pick(node_key, container):
        return as_int(container.get(node_key))

    white = pick("white", src)
    black = pick("black_or_african_american", src)
    asian = pick("asian", src)
    native = pick("american_indian_or_alaska_native", src)
    unknown = pick("unknown", src)
    # "other" = Native Hawaiian/Pacific Islander + more-than-one-race (sum the
    # reported ones; -1 only when both are missing).
    nhpi = pick("native_hawaiian_or_other_pacific_islander", src)
    multi = pick("more_than_one_race", src)
    others = [x for x in (nhpi, multi) if x >= 0]
    other = sum(others) if others else -1
    # hispanic lives on the ethnicity object under any key containing "hispanic"
    # that is not the "not_hispanic" bucket.
    hispanic = -1
    for k, v in eth.items() if isinstance(eth, dict) else []:
        if k.endswith("_evidence"):
            continue
        if "hispanic" in k.lower() and not k.lower().startswith("not"):
            hispanic = as_int(v)
            break

    buckets = {
        "white": white, "black": black, "asian": asian, "hispanic": hispanic,
        "native_american": native, "other": other, "unknown": unknown,
    }
    # Evidence: stitch together any non-empty per-bucket evidence quotes.
    quotes = []
    for k, v in src.items():
        q = NOT_REPORTED
        if k.endswith("_evidence"):
            q = clean_quote(v)
        elif isinstance(v, dict):
            q = clean_quote(v.get("exact_quote"))
        if q != NOT_REPORTED:
            quotes.append(q)
    ev = " | ".join(dict.fromkeys(quotes)) if quotes else NOT_REPORTED
    return buckets, ev


def derive_fda(records):
    rows = []
    for rec in records:
        key, entry = pick_primary_model(rec, _FDA_PROBES)
        data = (entry or {}).get("data") or {}
        race_buckets, race_ev = map_race_buckets(data)
        sex = as_dict(data.get("sex"))
        male = as_int(sex.get("male"))
        female = as_int(sex.get("female"))
        sex_ev = evidence_of(sex, "male")
        if sex_ev == NOT_REPORTED:
            sex_ev = evidence_of(sex, "female")
        rows.append({
            "submission_number": rec.get("submission_number", NOT_REPORTED),
            "device_name_evidence": evidence_of(data, "device_name"),
            "device_name": rec.get("device_name") or as_str(data.get("device_name")),
            "panel_evidence": evidence_of(data, "panel"),
            "panel": rec.get("panel") or as_str(data.get("panel")),
            "total_participants_evidence": evidence_of(data, "total_participants"),
            "total_participants": as_int(data.get("total_participants")),
            "sex_evidence": sex_ev,
            "sex": json.dumps({"male": male, "female": female}),
            "race_evidence": race_ev,
            "race": json.dumps(race_buckets),
            "age_range_evidence": evidence_of(data, "target_patient_age_range"),
            "age_range": as_str(data.get("target_patient_age_range")),
            "source_url": rec.get("source_url", NOT_REPORTED),
            "decision_date": rec.get("decision_date", NOT_REPORTED),
            "provider": entry.get("provider") or rec.get("provider", "anthropic"),
            "model": entry.get("model_id") or entry.get("model", NOT_REPORTED),
            "model_label": entry.get("label", NOT_REPORTED),
        })
    return rows


# SES keyword sets mirror the LIT_SYSTEM_PROMPT booleans: a flag is only true
# when the manuscript's SES evidence literally discusses that indicator.
_INCOME_KW = ("income", "earning", "wage", "salary", "poverty", "wealth", "deprivation")
_EDUCATION_KW = ("education", "schooling", "degree", "literacy", "academic")
_INSURANCE_KW = ("insur", "payer", "coverage", "medicare", "medicaid", "uninsured")


def derive_lit(ses_records, trials_records):
    rows = []

    def doi_from_slug(slug):
        if not slug:
            return NOT_REPORTED
        # The slug replaces the single registrant slash with '_'
        # (e.g. "10.1002_jmri.70189" -> "10.1002/jmri.70189").
        return slug.replace("_", "/", 1)

    def build_row(rec, *, doi, known_nct, tier):
        key, entry = pick_primary_model(rec, _LIT_PROBES)
        ex = get_extracted(entry)

        # Explicit NCT: the model quoted the registration in-text.
        assoc = as_dict(ex.get("associated_nct_ids"))
        assoc_vals = unwrap(assoc) if isinstance(assoc, dict) else assoc
        assoc_quote = assoc.get("exact_quote") if isinstance(assoc, dict) else ""
        explicit_nct = NOT_REPORTED
        if isinstance(assoc_vals, list) and assoc_vals:
            explicit_nct = str(assoc_vals[0]).strip().upper()
        nct_evidence = assoc_quote.strip() if isinstance(assoc_quote, str) and assoc_quote.strip() else NOT_REPORTED
        nct_id = explicit_nct if explicit_nct != NOT_REPORTED else (known_nct or NOT_REPORTED)

        # SES booleans + evidence, derived from the real socioeconomic_status
        # object by checking which indicators the extractor populated.
        ses = as_dict(ex.get("socioeconomic_status"))
        ses_quotes = []
        for k, v in ses.items():
            q = NOT_REPORTED
            if isinstance(v, dict):
                q = clean_quote(v.get("exact_quote"))
            elif k.endswith("_evidence"):
                q = clean_quote(v)
            if q != NOT_REPORTED:
                ses_quotes.append(q)
        ses_evidence = " | ".join(dict.fromkeys(ses_quotes)) if ses_quotes else NOT_REPORTED
        ses_blob = (ses_evidence + " " + json.dumps(ses)).lower()
        income_reported = (not is_nr(ses.get("income"))) or any(k in ses_blob for k in _INCOME_KW) and ses_evidence != NOT_REPORTED
        education_reported = (not is_nr(ses.get("education"))) or (any(k in ses_blob for k in _EDUCATION_KW) and ses_evidence != NOT_REPORTED)
        insurance_reported = any(k in ses_blob for k in _INSURANCE_KW) and ses_evidence != NOT_REPORTED
        ses_notes_parts = [f"{k}: {as_str(v)}" for k, v in ses.items()
                           if not k.endswith("_evidence") and not is_nr(v)]
        ses_notes = "; ".join(ses_notes_parts) if ses_notes_parts else "None"

        race_buckets, race_ev = map_race_buckets(ex)
        race_summary_parts = [f"{k}: {v}" for k, v in race_buckets.items() if v >= 0]
        detailed_race = "; ".join(race_summary_parts) if race_summary_parts else "None"

        return {
            "doi": doi or NOT_REPORTED,
            "nct_id_evidence": nct_evidence,
            "nct_id": nct_id,
            "study_name_evidence": evidence_of(ex, "study_design"),
            "study_name": NOT_REPORTED,  # formal trial name not captured by this pipeline
            "study_title": as_str(ex.get("study_design")) if not is_nr(ex.get("study_design")) else NOT_REPORTED,
            "ses_indicators_evidence": ses_evidence,
            "income_reported": bool(income_reported),
            "education_reported": bool(education_reported),
            "insurance_status_reported": bool(insurance_reported),
            "ses_notes": ses_notes,
            "detailed_race_breakdown_evidence": race_ev,
            "detailed_race_breakdown": detailed_race,
            "candidate_score": "",          # not computed by the JSON pipeline
            "tier": tier or "",             # real match-tier metadata when available
            "status": "Extracted" if entry else rec.get("extraction_status", "Extracted"),
            "provider": entry.get("provider") or rec.get("provider", "anthropic"),
            "model": entry.get("model_id") or entry.get("model", NOT_REPORTED),
            "model_label": entry.get("label", NOT_REPORTED),
        }

    # Clinical-trial manuscripts carry a real NCT + match tier.
    for rec in trials_records:
        rows.append(build_row(
            rec,
            doi=NOT_REPORTED,
            known_nct=str(rec.get("nct_id") or "").strip().upper(),
            tier=rec.get("tier", ""),
        ))
    # AI/ML manuscripts are DOI-anchored with no NCT linkage.
    for rec in ses_records:
        rows.append(build_row(
            rec,
            doi=doi_from_slug(rec.get("doi_slug")),
            known_nct=NOT_REPORTED,
            tier="",
        ))
    return rows


def _load(path):
    if not os.path.exists(path):
        print(f"  ! missing {path} — skipping", file=sys.stderr)
        return []
    with open(path) as f:
        return json.load(f)


def _write_csv(path, rows):
    """Write `rows` (list of dicts) to `path` with a stable column order.

    Quoting matches what a spreadsheet/pandas reader expects: fields with
    commas, quotes, or newlines are quoted and embedded quotes are doubled.
    """
    if not rows:
        # Still emit a header-less empty file so the UI fetch resolves to an
        # empty (not 404) dataset and renders its graceful empty state.
        open(path, "w").close()
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


def main():
    fda = _load(os.path.join(DATA_DIR, "fda_demographics_extracted.json"))
    ses = _load(os.path.join(DATA_DIR, "lit_ses_extracted.json"))
    trials = _load(os.path.join(DATA_DIR, "trials_lit_extracted.json"))

    fda_rows = derive_fda(fda)
    lit_rows = derive_lit(ses, trials)

    fda_path = os.path.join(DATA_DIR, "fda_extracted_latest.csv")
    lit_path = os.path.join(DATA_DIR, "lit_extracted_latest.csv")
    _write_csv(fda_path, fda_rows)
    _write_csv(lit_path, lit_rows)

    print(f"Wrote {len(fda_rows)} FDA rows  -> {fda_path}")
    print(f"Wrote {len(lit_rows)} literature rows -> {lit_path}")
    # Quick tier sanity print so a reviewer can eyeball the spread.
    from collections import Counter
    tiers = Counter(r["tier"] or "(derive in UI)" for r in lit_rows)
    print(f"  literature tier metadata: {dict(tiers)}")


if __name__ == "__main__":
    main()
