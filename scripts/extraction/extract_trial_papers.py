#!/usr/bin/env python3
"""
Clinical Trial Manuscript Demographic Extraction Pipeline — 3-Way Model Comparison

Reads open-access clinical trial manuscript PDFs from
`data/Clinical Trials Manuscripts/`, cross-references each filename (NCT ID)
with `data/pilot_clinical_trials_targets.csv` to inject trial metadata
(Condition, Intervention, Total Participants), and runs each manuscript
through three LLMs (small / medium / large tier) to compare extraction
quality and cost.

Provider routing (selected via AI_PROVIDER env var):
  - `anthropic`      → Claude Haiku 4.5 / Sonnet 4.6 / Opus 4.8 (default)
  - `vertex_gemini`  → Gemini 2.5 / 3.0 / 3.5 family — Flash-Lite / Flash / Pro
                       (native Vertex AI via google-cloud-aiplatform)

Filenames are expected to start with the NCT identifier, e.g.
`NCT06199934.pdf` or `NCT06199934_primary_results.pdf`. Anything that isn't
obviously an NCT ID falls back to the sanitized filename stem so the pipeline
still runs; the metadata join will simply miss on those rows.

PDF fetching is decoupled from extraction — this script only reads local
files so it can run in constrained environments (e.g., GitHub Actions)
without outbound access to publisher sites.

Mode switch:
  - `RUN_MODE=pilot-test` (default)  → process at most `PILOT_LIMIT` PDFs
                                       from the pilot folder
  - `RUN_MODE=full-extraction`       → process every PDF in the full corpus

Outputs:
  - data/trials_lit_extracted.json    (per-document, per-model results)
  - data/trials_lit_token_metrics.json (aggregate per-model token usage)

Requires:
  - ANTHROPIC_API_KEY (anthropic provider) OR GCP_PROJECT_ID +
    GOOGLE_APPLICATION_CREDENTIALS (vertex_gemini provider)
  - pip install -r scripts/extraction/requirements.txt
"""

import csv
import glob
import json
import os
import re
import sys
import time

import anthropic
import pdfplumber
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# Make scripts/utils/cost_tracker.py importable when this file runs from the
# repo root (the extraction scripts are launched via `python scripts/...`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.cost_tracker import log_api_cost
from utils.json_repair import clean_and_parse_json


# Hardcoded pre-call governor in seconds. Both providers throttle hard
# under bursty load — Vertex starts emitting 429 ResourceExhausted on the
# project-wide quota; Anthropic gets 429 / 529 once we exceed the
# per-minute tokens-in budget. Sleeping 2s before every API call keeps
# the 3-way comparison loop comfortably below either ceiling.
PRE_CALL_DELAY_SEC = 2


def _is_retriable(exc):
    """True if `exc` is a transient API failure worth retrying.

    Covers Anthropic (RateLimitError 429, OverloadedError 529, network /
    timeout errors) and Vertex AI / Gemini (ResourceExhausted,
    ServiceUnavailable, DeadlineExceeded). Matched by class name to
    avoid hard-importing google.api_core in the Anthropic-only path.
    """
    name = type(exc).__name__
    if name in {
        "RateLimitError", "OverloadedError",
        "APIConnectionError", "APITimeoutError",
        "ResourceExhausted", "ServiceUnavailable", "DeadlineExceeded",
    }:
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code in (429, 529):
        return True
    return False


# Up to 5 attempts with exponential backoff (2s, 4s, 8s, 16s — capped at
# 30s). `reraise=True` so the final failure surfaces the original
# exception, which lets the per-doc try/except mark the call as failed
# and move on instead of stalling the run when Vertex hard-throttles
# the project.
retry_api_call = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception(_is_retriable),
    reraise=True,
)

PILOT_LIMIT = 20
# PDF_DIR is resolved at runtime based on RUN_MODE — pilot-test reads from a
# small curated pilot folder; full-extraction reads the full manuscript corpus.
PILOT_PDF_DIR = "data/pilot_trials_manuscripts"
FULL_PDF_DIR  = "data/Clinical Trials Manuscripts"
METADATA_CSV = "data/pilot_clinical_trials_targets.csv"
OUTPUT_DATA = "data/trials_lit_extracted.json"
OUTPUT_METRICS = "data/trials_lit_token_metrics.json"

# Total clinical trials in the AACT dataset — used for cost-projection math
# alongside the pilot token counts.
TOTAL_STUDIES = 77347

# Pipeline identifier recorded against every row in data/token_costs.csv.
PIPELINE_NAME = "trials-lit"
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic")
GEMINI_VERSION = os.environ.get("GEMINI_VERSION", "gemini-3.5")
GEMINI_LOCATION = "global"

# Keep per-provider model lists side by side so the active one can be chosen
# at import time; downstream code never has to branch on provider when
# looking up model IDs, labels, or per-token costs.
_ANTHROPIC_MODELS = [
    {"key": "haiku_4_5",  "id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",  "input_cost_per_m": 1.00,  "output_cost_per_m": 5.00},
    {"key": "sonnet_4_6", "id": "claude-sonnet-4-6",         "label": "Sonnet 4.6", "input_cost_per_m": 3.00,  "output_cost_per_m": 15.00},
    {"key": "opus_4_8",   "id": "claude-opus-4-8",           "label": "Opus 4.8",   "input_cost_per_m": 15.00, "output_cost_per_m": 75.00},
]
# Gemini model families, selected at import time via the GEMINI_VERSION env
# var (gemini-2.5 / gemini-3.0 / gemini-3.5; the legacy values
# gemini-2.5-stable and gemini-3-preview still resolve via
# _GEMINI_FAMILY_ALIASES). Each family lists Flash-Lite / Flash / Pro tiers.
# The 3.0 middle tier ID is strictly `gemini-3-flash-preview` (no `.1`) while
# Flash-Lite and Pro keep the `3.1` branding; 3.5 Flash is GA and 3.5 Pro is
# preview-only as of May 2026. Standard-tier prices per the Vertex AI pricing
# page (https://cloud.google.com/vertex-ai/generative-ai/pricing). Gemini keys
# are model-accurate (`gemini_3_flash_lite` not the `haiku_4_5` slot-name used
# for the Anthropic tier) so a Gemini run's JSON is never misread as Claude.
_GEMINI_2_5_MODELS = [
    {"key": "gemini_25_flash_lite", "id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite", "input_cost_per_m": 0.10, "output_cost_per_m": 0.40},
    {"key": "gemini_25_flash",      "id": "gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "input_cost_per_m": 0.30, "output_cost_per_m": 2.50},
    {"key": "gemini_25_pro",        "id": "gemini-2.5-pro",        "label": "Gemini 2.5 Pro",        "input_cost_per_m": 1.25, "output_cost_per_m": 10.00},
]
# Gemini 3.x line: 3 Flash is preview-only (`gemini-3-flash-preview`, no `.1`);
# Flash-Lite and Pro carry the 3.1 branding. Prices per the Vertex AI pricing page.
_GEMINI_3_0_MODELS = [
    {"key": "gemini_3_flash_lite", "id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash-Lite", "input_cost_per_m": 0.25, "output_cost_per_m": 1.50},
    {"key": "gemini_3_flash",      "id": "gemini-3-flash-preview",        "label": "Gemini 3 Flash",        "input_cost_per_m": 0.50, "output_cost_per_m": 3.00},
    {"key": "gemini_3_pro",        "id": "gemini-3.1-pro-preview",        "label": "Gemini 3.1 Pro",        "input_cost_per_m": 2.00, "output_cost_per_m": 12.00},
]
# Gemini 3.5 line: 3.5 Flash is GA (`gemini-3.5-flash`, announced at Google I/O
# May 2026; $1.50 in / $9 out). 3.5 Pro is in limited preview as of May 2026 —
# the `-preview` id should graduate to `gemini-3.5-pro` at GA, and its pricing is
# unannounced (provisionally mirrors the 3.1 Pro tier).
_GEMINI_3_5_MODELS = [
    {"key": "gemini_35_flash", "id": "gemini-3.5-flash",       "label": "Gemini 3.5 Flash", "input_cost_per_m": 1.50, "output_cost_per_m": 9.00},
    {"key": "gemini_35_pro",   "id": "gemini-3.5-pro-preview", "label": "Gemini 3.5 Pro",   "input_cost_per_m": 2.00, "output_cost_per_m": 12.00},
]
# Selectable Gemini families, chosen via the workflow's `gemini_version` input:
#   gemini-2.5 | gemini-3.0 | gemini-3.5   (default: latest GA family)
_GEMINI_FAMILIES = {
    "gemini-2.5": _GEMINI_2_5_MODELS,
    "gemini-3.0": _GEMINI_3_0_MODELS,
    "gemini-3.5": _GEMINI_3_5_MODELS,
}
# Back-compat: map the previous selector values onto the new family keys.
_GEMINI_FAMILY_ALIASES = {"gemini-2.5-stable": "gemini-2.5", "gemini-3-preview": "gemini-3.0"}
_GEMINI_MODELS = _GEMINI_FAMILIES.get(
    _GEMINI_FAMILY_ALIASES.get(GEMINI_VERSION, GEMINI_VERSION), _GEMINI_3_5_MODELS
)
MODELS = _GEMINI_MODELS if AI_PROVIDER == "vertex_gemini" else _ANTHROPIC_MODELS

NCT_RE = re.compile(r"(NCT\d{8})", re.IGNORECASE)
# Filenames are structured like `2026-04-18_NCT02349132_01_Tier1.pdf`; the
# trailing `_Tier<N>` token is the manuscript-quality tier assigned during
# triage. `Tier[_\s-]*(\d+)` tolerates `Tier1`, `Tier 1`, `Tier_1`, etc.
TIER_RE = re.compile(r"Tier[_\s-]*(\d+)", re.IGNORECASE)


def _make_client():
    """Initialize the active provider. For Anthropic, return an Anthropic
    client. For vertex_gemini, call vertexai.init() and return None — Gemini
    uses one `GenerativeModel` per model name rather than one shared client."""
    if AI_PROVIDER == "vertex_gemini":
        import vertexai
        vertexai.init(
            project=os.environ.get("GCP_PROJECT_ID"),
            location=GEMINI_LOCATION,
        )
        return None
    return anthropic.Anthropic()


client = _make_client()

EXTRACTION_PROMPT = """\
You are a clinical data extractor specializing in published clinical trial manuscripts. Record demographic, socioeconomic, clinical-context, and functional-status data of the trial cohort via the `record_extracted_data` tool.

The manuscript text is delimited by `--- PAGE N ---` markers. Every evidence quote you record MUST come from the page that immediately precedes the text you're quoting, and you must record that page number on the field.

Before calling the extraction tool, use a <thinking> block to locate the sections containing the demographic and clinical context data. Specifically, scan "Methods", "Study Design", "Patients and Methods", and "Background" sections for clinical context, and the cohort / participants tables for demographics.

CRITICAL: The "Explicit Unknown" category is a specific reported value, completely distinct from "Not Reported" (missing) data. If researchers explicitly state a value is unknown, unrecorded, or declined, record the count under "unknown". If they fail to mention the category entirely, record "Not Reported".

CRITICAL: A breakdown row that pairs a cis/trans gender-identity qualifier with a biological-sex word (e.g. "Transgender Female", "Cisgender Male") is ambiguous and must NOT be counted toward `sex.male`/`sex.female` — record it under `sex.unknown` instead. The same row should also NOT be folded into `gender.transgender` unless it instead uses gender-role phrasing without a sex word (e.g. "Transgender Woman", "Cisgender Man" — those ARE clean gender-identity categories and belong in their normal bucket). When in doubt, prefer `unknown` over guessing.

LINKAGE CRITICAL: We must link this manuscript to ClinicalTrials.gov if possible. Scan the text for any NCT identifier (format: NCT followed by 8 digits) and place them under `associated_nct_ids`.

EVIDENCE-FIRST RULE: Every integer field has a corresponding `<field>_evidence` sibling string (e.g. `total_participants_evidence` next to `total_participants`). You MUST emit the `_evidence` string — a verbatim quote from the source text that establishes the count — BEFORE you commit to the integer itself. If the document is silent about that number, leave `_evidence` as an empty string and record "Not Reported" on the integer field. Do not invent a number for which you cannot first quote a supporting passage.

For every field, populate:
- `value`: the extracted value (or the literal string "Not Reported" for scalars / empty list for list-typed fields when the document is silent).
- `exact_quote`: a verbatim excerpt from the document that proves the value (empty string when "Not Reported").
- `page_number`: the integer page number (1-indexed) where the quote appears (0 when "Not Reported").
"""


def _ev(description: str, value_schema: dict) -> dict:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "value": value_schema,
            "exact_quote": {"type": "string", "description": "Verbatim excerpt proving the value. Empty if Not Reported."},
            "page_number": {"type": "integer", "description": "1-indexed page number from `--- PAGE N ---` markers. 0 if Not Reported."},
        },
        "required": ["value", "exact_quote", "page_number"],
    }


_EV_STR = {"type": "string", "description": "Extracted value, or the literal string 'Not Reported'."}
_EV_INT = {"type": ["integer", "string"], "description": "Extracted integer, or the literal string 'Not Reported'."}
_EV_LIST = {"type": "array", "items": {"type": "string"}, "description": "Extracted list of strings; empty array if none."}


def _evidence_schema(field_name: str, value_noun: str) -> dict:
    """Sibling `{field_name}_evidence` string schema. The model is required to
    emit this BEFORE the numeric field, so any integer it subsequently records
    is grounded in the quote it just wrote. Empty string == 'Not Reported'."""
    return {
        "type": "string",
        "description": (
            f"Verbatim quote from the source text that establishes the {value_noun}. "
            f"You MUST populate this field BEFORE deciding the numeric value of `{field_name}`. "
            "Empty string if the document is silent on this value."
        ),
    }


def _breakdown(description: str, fields: list[str]) -> dict:
    properties: dict = {}
    required: list[str] = []
    for f in fields:
        pretty = f.replace("_", " ")
        properties[f"{f}_evidence"] = _evidence_schema(f, f"count of {pretty}")
        properties[f] = _ev(f"Count of {pretty}", _EV_INT)
        required.extend([f"{f}_evidence", f])
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required,
    }


EXTRACTION_TOOL = {
    "name": "record_extracted_data",
    "description": "Record evidence-grounded demographic, clinical-context, SES, and functional-status data extracted from a published clinical trial manuscript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "associated_nct_ids": _ev("ClinicalTrials.gov NCT identifiers referenced in the manuscript.", _EV_LIST),
            "target_patient_age_range": _ev("Inclusion-criteria age range, verbatim when possible.", _EV_STR),
            "study_design": _ev("Concise 1-2 sentence summary of the trial design.", _EV_STR),
            "total_participants_evidence": _evidence_schema("total_participants", "total number of participants in the trial cohort"),
            "total_participants": _ev("Total N in the trial cohort.", _EV_INT),
            "age": _ev("String summary of how age is reported (e.g., 'Mean 65.2 (SD 5.1)').", _EV_STR),
            "geography": {
                "type": "object",
                "properties": {
                    "us_states": _ev("US states represented.", _EV_LIST),
                    "countries": _ev("Countries represented.", _EV_LIST),
                    "total_sites_evidence": _evidence_schema("total_sites", "total number of trial sites"),
                    "total_sites": _ev("Total number of trial sites.", _EV_INT),
                },
                "required": ["us_states", "countries", "total_sites_evidence", "total_sites"],
            },
            "sex": _breakdown("Participant sex breakdown.", ["female", "male", "unknown"]),
            "gender": _breakdown("Participant gender identity breakdown.",
                                 ["woman", "man", "non_binary", "transgender", "other", "unknown"]),
            "race_nih_omb": _breakdown("Participant race breakdown (NIH/OMB categories).", [
                "american_indian_or_alaska_native", "asian", "black_or_african_american",
                "native_hawaiian_or_other_pacific_islander", "white",
                "more_than_one_race", "unknown",
            ]),
            "ethnicity": _breakdown("Participant ethnicity breakdown.",
                                    ["hispanic_or_latino", "not_hispanic_or_latino", "unknown"]),
            "socioeconomic_status": {
                "type": "object",
                "properties": {
                    "education": _ev("Education summary.", _EV_STR),
                    "income": _ev("Income summary.", _EV_STR),
                    "wealth": _ev("Wealth summary.", _EV_STR),
                    "family_size": _ev("Family size / household summary.", _EV_STR),
                    "adi_area_deprivation_index": _ev("ADI summary.", _EV_STR),
                },
                "required": ["education", "income", "wealth", "family_size", "adi_area_deprivation_index"],
            },
            "disability_and_functional_limitations": _ev("Summary of disability / functional-limitation reporting.", _EV_STR),
            "religion": _ev("Summary of religion reporting.", _EV_STR),
        },
        "required": [
            "associated_nct_ids", "target_patient_age_range", "study_design",
            "total_participants", "age", "geography", "sex", "gender",
            "race_nih_omb", "ethnicity", "socioeconomic_status",
            "disability_and_functional_limitations", "religion",
        ],
    },
}


def resolve_mode() -> str:
    """Return the normalised RUN_MODE, defaulting to pilot-test."""
    mode = (os.environ.get("RUN_MODE") or "pilot-test").strip().lower()
    if mode not in ("pilot-test", "full-extraction"):
        print(f"  ! Unknown RUN_MODE {mode!r} — defaulting to pilot-test",
              file=sys.stderr)
        return "pilot-test"
    return mode


def resolve_pdf_dir(mode: str) -> str:
    """Pilot-test reads the curated `pilot_trials_manuscripts/` folder; full
    extraction reads the full `Clinical Trials Manuscripts/` corpus."""
    return FULL_PDF_DIR if mode == "full-extraction" else PILOT_PDF_DIR


def resolve_limit(mode: str) -> int | None:
    """Pilot-test caps the run at PILOT_LIMIT to match the grant's target
    sample size; full-extraction returns None (no cap)."""
    return None if mode == "full-extraction" else PILOT_LIMIT


def print_token_summary(models, model_totals, successful_docs, total_pages, attempted_docs):
    """Emit a terminal-friendly per-model token summary for CI logs.

    `successful_docs` is the run-wide count of PDFs where extraction succeeded
    (the natural denominator for the averages). `total_pages` is the sum of
    pdf.pages across those successful documents — surfaced alongside the doc
    count so the denominator context is unambiguous. Per-model `docs` may be
    lower if a given model errored on a particular document; the per-model
    block prints that count explicitly.
    """
    bar = "=" * 60
    print(f"\n{bar}")
    print("TOKEN USAGE SUMMARY")
    print(bar)
    print(f"Denominator: N={successful_docs} documents successfully processed "
          f"(out of {attempted_docs} attempted), totaling {total_pages:,} pages")
    for model in models:
        mkey = model["key"]
        t = model_totals[mkey]
        docs = t["docs"]
        denom = docs or 1
        avg_in = t["input"] / denom
        avg_out = t["output"] / denom
        print(f"\n[{model['label']} ({model['id']})]")
        print(f"  Total Input Tokens:    {t['input']:>12,}")
        print(f"  Total Output Tokens:   {t['output']:>12,}")
        print(f"  Average Input / doc:   {avg_in:>12,.1f}")
        print(f"  Average Output / doc:  {avg_out:>12,.1f}")
        print(f"  Successful calls:      {docs:>12,}")
    print(bar)


def tier_from_filename(stem: str) -> str | None:
    """Pull the `Tier N` token out of a manuscript filename stem. Returns
    None if the stem doesn't contain one."""
    m = TIER_RE.search(stem)
    return f"Tier {m.group(1)}" if m else None


def nct_from_filename(stem: str) -> str | None:
    """Extract the first NCT identifier from a filename stem, upper-cased.
    Returns None if the stem doesn't contain one."""
    m = NCT_RE.search(stem)
    return m.group(1).upper() if m else None


def build_metadata_index(csv_path: str) -> dict[str, dict]:
    """Map NCT ID -> CSV row. Returns {} if the CSV is missing."""
    if not os.path.exists(csv_path):
        print(f"  ! metadata CSV not found: {csv_path} "
              f"(continuing with empty metadata)", file=sys.stderr)
        return {}
    idx: dict[str, dict] = {}
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            nct = (row.get("NCT Number") or row.get("nct_id") or "").strip().upper()
            if nct:
                idx[nct] = row
    return idx


def lookup_metadata(index: dict[str, dict], nct: str | None) -> dict:
    """Return the metadata block for the results payload, injecting whatever
    the CSV provides. Always returns the expected shape so downstream
    consumers don't have to branch on missing metadata."""
    row = index.get(nct or "", {})
    total = row.get("Total Participants") or row.get("total_participants") or ""
    try:
        total_int = int(total) if str(total).strip().isdigit() else None
    except (ValueError, TypeError):
        total_int = None
    return {
        "nct_id": nct or "Not Reported",
        "condition": (row.get("Condition") or "Not Reported").strip() or "Not Reported",
        "intervention": (row.get("Intervention") or "Not Reported").strip() or "Not Reported",
        "ctgov_total_participants": total_int if total_int is not None else "Not Reported",
    }


def discover_pilot_pdfs(pdf_dir: str) -> list[tuple[str, str, str | None]]:
    """Return (identifier, pdf_path, tier) triples sorted by filename.
    Identifier is the NCT ID if parseable from the filename, else the raw
    stem. `tier` is "Tier 1" / "Tier 2" / … or None if not encoded in the
    filename."""
    if not os.path.isdir(pdf_dir):
        print(f"  ! manuscript directory not found: {pdf_dir}", file=sys.stderr)
        return []
    paths = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    triples: list[tuple[str, str, str | None]] = []
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        nct = nct_from_filename(stem)
        tier = tier_from_filename(stem)
        triples.append((nct or stem, p, tier))
    return triples


def extract_text_from_local_pdf(path: str) -> tuple[str | None, int]:
    """Read a local PDF and return (text, total_page_count).

    Each non-empty page is prefixed with `--- PAGE N ---` so the downstream
    tool-use extraction can cite the exact page for every evidence quote.
    The page count is the full page total reported by the PDF (not just the
    pages with extractable text) so dashboard metrics reflect document size.
    Returns (None, 0) when the PDF cannot be opened at all, and
    (None, page_count) when it opens but every page extracts as empty.
    """
    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            pages = [
                f"--- PAGE {i + 1} ---\n{p.extract_text()}"
                for i, p in enumerate(pdf.pages) if p.extract_text()
            ]
            if not pages:
                return None, page_count
            return "\n\n".join(pages), page_count
    except Exception as e:
        print(f"  ✗ PDF parse failed ({path}): {e}", file=sys.stderr)
        return None, 0


def _to_gemini_schema(schema):
    """Recursively normalize an Anthropic-style JSON schema into the strict
    subset Gemini's Structured Outputs engine accepts as a `response_schema`.

    Drops keys Gemini's state-table compiler rejects:
      - `anyOf`, `allOf`, `oneOf` — Anthropic uses these for nullability /
        union shapes; Gemini's `response_schema` validator counts each
        branch toward its serving-state budget and emits
        "400 POST: The specified schema produces a constraint that has
        too many states for serving" once a deeply nested schema includes
        them. Stripping them keeps types strict.
      - Union `type` (e.g. `["integer", "string"]`) — flattened to the
        strict primitive `"string"`. Ints round-trip fine through a string
        and "Not Reported" is the common sentinel anyway.

    Recurses through `properties` and `items`."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("anyOf", "allOf", "oneOf")}
    t = out.get("type")
    if isinstance(t, list):
        out["type"] = "string"
    if "properties" in out:
        out["properties"] = {k: _to_gemini_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _to_gemini_schema(out["items"])
    return out


def _proto_to_dict(val):
    """Convert Gemini's protobuf MapComposite / RepeatedComposite return
    values into plain Python dicts and lists so the result is JSON-serialisable
    the same way the Anthropic `tool_use.input` payload already is."""
    if hasattr(val, "items") and not isinstance(val, (str, bytes)):
        return {k: _proto_to_dict(v) for k, v in val.items()}
    if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
        return [_proto_to_dict(v) for v in val]
    return val


def extract_with_model(text: str, model_id: str) -> tuple[dict, dict]:
    """Run extraction via tool / function calling. Returns (data, token_usage).
    The tool call's arguments ARE the evidence-grounded payload — no free-form
    JSON parsing needed."""
    prompt = f"{EXTRACTION_PROMPT}\n\n--- MANUSCRIPT TEXT ---\n{text}"
    if AI_PROVIDER == "vertex_gemini":
        data, token_usage = _extract_gemini(prompt, model_id)
    else:
        data, token_usage = _extract_anthropic(prompt, model_id)
    log_api_cost(
        provider=AI_PROVIDER,
        pipeline_name=PIPELINE_NAME,
        input_tokens=token_usage["input_tokens"],
        output_tokens=token_usage["output_tokens"],
        model=model_id,
    )
    return data, token_usage


@retry_api_call
def _extract_anthropic(prompt: str, model_id: str) -> tuple[dict, dict]:
    """Anthropic path. Wrapped in `retry_api_call` so 429 / 529 / network
    blips are retried up to 5 times with exponential backoff before the
    exception bubbles up to the per-doc try/except."""
    response = client.messages.create(
        model=model_id,
        max_tokens=8192,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_extracted_data"},
        messages=[{"role": "user", "content": prompt}],
    )
    # Walk the response once: prefer a `tool_use` block (the happy path
    # under our forced tool_choice), but also collect any free-form text
    # so we can fall back to JSON-parsing it via clean_and_parse_json if
    # the tool was skipped.
    data: dict = {"error": "No tool call in response"}
    text_fallback = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use" and block.name == "record_extracted_data":
            # `tool_use.input` is already a dict; clean_and_parse_json
            # is a pass-through for dicts so the call site stays
            # identical across Anthropic / Vertex.
            data = clean_and_parse_json(block.input)
            break
        if block_type == "text":
            text_fallback.append(getattr(block, "text", "") or "")
    if isinstance(data, dict) and "error" in data and text_fallback:
        data = clean_and_parse_json("".join(text_fallback))
    return data, {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


@retry_api_call
def _extract_gemini(prompt: str, model_id: str) -> tuple[dict, dict]:
    """Vertex AI Gemini path using native Structured Outputs.

    Uses `response_mime_type="application/json"` + `response_schema=...`
    instead of FunctionDeclaration tool-calling. The deeply nested
    EXTRACTION_TOOL schema (paired evidence + value fields, expanded race
    buckets, SES sub-objects) tripped the function-calling state-table
    limit and Vertex returned `400 POST: The specified schema produces a
    constraint that has too many states for serving`. Structured Outputs
    constrains decoding by streaming-validating against the JSON schema
    rather than pre-compiling a DFA, which sidesteps that ceiling while
    still guaranteeing schema-conformant output. Token usage comes from
    `usage_metadata`: prompt_token_count → input; candidates_token_count →
    output."""
    from vertexai.generative_models import GenerationConfig, GenerativeModel

    response_schema = _to_gemini_schema(EXTRACTION_TOOL["input_schema"])

    model = GenerativeModel(model_name=model_id)
    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig(
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=response_schema,
        ),
    )

    # Hand the raw text to clean_and_parse_json, which walks several
    # progressively more-forgiving repair stages (markdown fences,
    # trailing commas, control chars, ast fallback, internal-quote
    # escape) before giving up. The happy path is still strict
    # `json.loads`, so well-formed responses pay no penalty.
    raw = getattr(response, "text", None)
    parsed = clean_and_parse_json(raw)
    if isinstance(parsed, dict) and "error" not in parsed:
        # Run the proto/dict normaliser on the parsed payload so the
        # integer-string coercion (e.g. "-1" -> -1) still applies.
        data = _proto_to_dict(parsed)
    else:
        data = parsed

    usage = response.usage_metadata
    return data, {
        "input_tokens": getattr(usage, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
    }


def main():
    if AI_PROVIDER == "vertex_gemini":
        if not os.environ.get("GCP_PROJECT_ID"):
            print("Error: GCP_PROJECT_ID not set (required for vertex_gemini)", file=sys.stderr)
            sys.exit(1)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Error: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
            sys.exit(1)

    run_mode = resolve_mode()
    pdf_dir = resolve_pdf_dir(run_mode)
    limit = resolve_limit(run_mode)

    print("Clinical Trial Manuscript 3-Way Model Comparison Pipeline")
    print(f"  RUN_MODE:      {run_mode}  (limit={'none' if limit is None else limit})")
    print(f"  PDF dir:       {pdf_dir}")
    print(f"  Metadata CSV:  {METADATA_CSV}")

    metadata_index = build_metadata_index(METADATA_CSV)
    all_pdfs = discover_pilot_pdfs(pdf_dir)
    pilot = all_pdfs if limit is None else all_pdfs[:limit]

    if not pilot:
        print(f"  No PDFs found in {pdf_dir}. Nothing to do — exiting cleanly.",
              file=sys.stderr)
        sys.exit(0)

    print(f"  Found {len(all_pdfs)} PDFs; processing {len(pilot)}\n")

    results = []
    model_totals = {m["key"]: {"input": 0, "output": 0, "docs": 0} for m in MODELS}
    successful_docs_count = 0
    total_pages_processed = 0
    interrupted_by: str | None = None

    # try/except/finally keeps the pipeline crash-safe: if Anthropic throws
    # `Insufficient Credits` (or any other fatal error) mid-run, the finally
    # block still flushes partial results + metrics to disk so the dashboard
    # reflects the exact number of docs / pages that completed.
    try:
        for i, (ident, pdf_path, tier) in enumerate(pilot):
            nct = ident if NCT_RE.fullmatch(ident) else None
            metadata = lookup_metadata(metadata_index, nct)
            tier_label = tier or "Not Reported"
            print(f"[{i+1}/{len(pilot)}] {ident}  [{tier_label}]")
            print(f"  ↪ {pdf_path}")
            print(f"  metadata: condition={metadata['condition']} "
                  f"intervention={metadata['intervention']} "
                  f"N={metadata['ctgov_total_participants']}")

            text, page_count = extract_text_from_local_pdf(pdf_path)
            if not text:
                print(f"  ✗ No text from PDF")
                results.append({
                    "identifier": ident,
                    "nct_id": nct or "Not Reported",
                    "tier": tier_label,
                    "local_pdf_path": pdf_path,
                    "page_count": page_count,
                    "metadata": metadata,
                    "extraction_status": "pdf_failed",
                    "provider": AI_PROVIDER,
                    "models": {},
                })
                continue

            print(f"  ✓ Full PDF: {len(text):,} chars, {page_count} pages")

            model_results = {}
            for model in MODELS:
                mkey = model["key"]
                mid = model["id"]
                label = model["label"]
                # Pre-call governor: sleep before every API call (both
                # providers) so the bursty per-doc × per-model loop
                # doesn't instantly trip Vertex / Anthropic quotas. The
                # retry decorator already handles transient 429s, but
                # spreading calls out here avoids hitting that path.
                time.sleep(PRE_CALL_DELAY_SEC)
                print(f"    → {label} ({mid})...", end=" ", flush=True)
                try:
                    data, tokens = extract_with_model(text, mid)
                    # Carry the manuscript tier into the per-model payload too so
                    # the dashboard can surface it even from the per-model view.
                    wrapped = {"metadata": metadata, "tier": tier_label, "extracted_data": data}
                    # A model call is only "successful" when the returned
                    # extracted_data dict actually carries extracted fields
                    # — i.e. it does NOT contain an `"error"` key. Vertex
                    # 400s, Anthropic "no tool_use" responses, and
                    # JSON-parse failures all surface as
                    # `{"error": "..."}` while still raising no Python
                    # exception. Token totals still accumulate (the API
                    # charged us either way); the doc/call count is what
                    # gates the denominator.
                    call_succeeded = isinstance(data, dict) and "error" not in data
                    # `provider` + `model` are stamped on every per-model
                    # record so back-to-back Anthropic + Vertex runs stay
                    # unambiguously labelled even after results merge.
                    model_results[mkey] = {
                        "provider": AI_PROVIDER,
                        "model": mid,
                        "model_id": mid,
                        "label": label,
                        "data": wrapped,
                        "input_tokens": tokens["input_tokens"],
                        "output_tokens": tokens["output_tokens"],
                        "call_succeeded": call_succeeded,
                    }
                    model_totals[mkey]["input"] += tokens["input_tokens"]
                    model_totals[mkey]["output"] += tokens["output_tokens"]
                    if call_succeeded:
                        model_totals[mkey]["docs"] += 1
                        print(f"{tokens['input_tokens']:,} in / {tokens['output_tokens']:,} out")
                    else:
                        err = data.get("error", "unknown") if isinstance(data, dict) else "non-dict"
                        print(f"ERROR (kept tokens): {err}")
                except Exception as e:
                    print(f"ERROR: {e}")
                    model_results[mkey] = {
                        "provider": AI_PROVIDER,
                        "model": mid,
                        "model_id": mid,
                        "label": label,
                        "data": {"metadata": metadata, "tier": tier_label, "extracted_data": {"error": str(e)}},
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "call_succeeded": False,
                    }

            results.append({
                "identifier": ident,
                "nct_id": nct or "Not Reported",
                "tier": tier_label,
                "local_pdf_path": pdf_path,
                "page_count": page_count,
                "metadata": metadata,
                "extraction_status": "success",
                "provider": AI_PROVIDER,
                "models": model_results,
            })
            # A doc only counts toward the run-wide denominator when at
            # least one of the per-model calls actually returned valid
            # data. If every model 400ed / refused / failed parsing, the
            # doc was *attempted* but not *processed*.
            if not any(m.get("call_succeeded") for m in model_results.values()):
                continue
            successful_docs_count += 1
            total_pages_processed += page_count
    except KeyboardInterrupt:
        interrupted_by = "user (Ctrl+C)"
        print(f"\n✗ Pipeline interrupted by {interrupted_by}.", file=sys.stderr)
    except Exception as e:
        interrupted_by = f"{type(e).__name__}: {e}"
        print(f"\n✗ Pipeline interrupted by {interrupted_by}.", file=sys.stderr)
    finally:
        if interrupted_by:
            print(f"  Saving partial results for {successful_docs_count} successfully "
                  f"processed documents ({total_pages_processed} pages) before exit...",
                  file=sys.stderr)

        print(f"\nWriting {len(results)} results to {OUTPUT_DATA}")
        os.makedirs(os.path.dirname(OUTPUT_DATA), exist_ok=True)
        with open(OUTPUT_DATA, "w") as f:
            json.dump(results, f, indent=2)

        per_model = {}
        for model in MODELS:
            mkey = model["key"]
            t = model_totals[mkey]
            docs = t["docs"] or 1
            per_model[mkey] = {
                "model_id": model["id"],
                "label": model["label"],
                "input_cost_per_m": model["input_cost_per_m"],
                "output_cost_per_m": model["output_cost_per_m"],
                "avg_input_per_doc": t["input"] / docs,
                "avg_output_per_doc": t["output"] / docs,
                "total_input_tokens": t["input"],
                "total_output_tokens": t["output"],
                "docs_processed": t["docs"],
            }

        metrics = {
            "run_mode": run_mode,
            "pilot_size": successful_docs_count,
            "successful_docs_count": successful_docs_count,
            "total_pages_processed": total_pages_processed,
            "attempted_docs": len(pilot),
            "interrupted_by": interrupted_by,
            "total_studies": TOTAL_STUDIES,
            "per_model": per_model,
        }
        print(f"Writing metrics to {OUTPUT_METRICS}")
        with open(OUTPUT_METRICS, "w") as f:
            json.dump(metrics, f, indent=2)

        print_token_summary(MODELS, model_totals, successful_docs_count,
                            total_pages_processed, len(pilot))

    if interrupted_by:
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
