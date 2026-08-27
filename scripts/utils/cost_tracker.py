"""Append-only CSV logger for per-call LLM token usage and cost.

Extraction pipelines call log_api_cost() after every successful model call —
Anthropic messages.create() or Vertex Gemini generate_content() — so we can
compare spend across providers. Rows land in data/token_costs.csv; the file is
created with a header row on first write.

All timestamps in this module are anchored to America/New_York (24-hour clock,
EST in winter / EDT in summer) so the log is comparable across machines and
unambiguous for the team in NYC.
"""
import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Single source of truth for the project's display timezone. Everywhere a
# datetime is stamped (CSV log row, run timestamp filenames, metrics JSON)
# uses this so logs are directly comparable.
EASTERN_TZ = ZoneInfo("America/New_York")
LOG_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S %Z"

COST_CSV = os.path.join("data", "token_costs.csv")
HEADERS = [
    "Date",
    "Provider",
    "Pipeline",
    "Model",
    "Input Tokens",
    "Output Tokens",
    "Total Cost ($)",
]

# Per-million-token pricing keyed by model family. Sonnet is the default
# fallback when the model string doesn't match a known family — this matches
# the $3 / $15 rates the pilot spec calls out for Sonnet.
#
# Order matters: more specific keys come first so "gemini-3.1-flash-lite"
# matches before the "gemini-3.1-flash" substring (the iteration returns the
# first hit in insertion order).
#
# Gemini 3.1 Preview pricing sourced from the Vertex AI pricing page
# (https://cloud.google.com/vertex-ai/generative-ai/pricing, Standard tier).
_PRICING = {
    # Gemini 3.5: Flash is GA ($1.50 in / $9 out, Google I/O May 2026); 3.5 Pro
    # pricing is unannounced as of May 2026 and provisionally mirrors 3.1 Pro.
    "gemini-3.5-flash":      (1.50,  9.00),
    "gemini-3.5-pro":        (2.00, 12.00),
    "gemini-3.1-flash-lite": (0.25,  1.50),
    "gemini-3.1-flash":      (0.50,  3.00),
    "gemini-3-flash":        (0.50,  3.00),
    "gemini-3.1-pro":        (2.00, 12.00),
    "gemini-2.5-flash-lite": (0.10,  0.40),
    "gemini-2.5-flash":      (0.30,  2.50),
    "gemini-2.5-pro":        (1.25, 10.00),
    "claude-haiku-4-5":      (1.00,  5.00),
    "claude-sonnet-4-6":     (3.00, 15.00),
    "claude-opus-4-8":       (5.00, 25.00),
    "haiku":                 (1.00,  5.00),
    "sonnet":                (3.00, 15.00),
    "opus":                  (5.00, 25.00),
}


def _rates_for(model: str) -> tuple[float, float]:
    key = (model or "").lower()
    for family, rates in _PRICING.items():
        if family in key:
            return rates
    return _PRICING["sonnet"]


def cost_for(model, input_tokens, output_tokens):
    """Pure cost calculator used by both `log_api_cost` and the per-run
    metrics writers in extraction_pipeline/. Returns total USD spend for
    `(input_tokens, output_tokens)` against the rate-card row that matches
    `model`. No side effects — safe to call repeatedly.
    """
    in_rate, out_rate = _rates_for(model)
    return (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate


def log_api_cost(provider, pipeline_name, input_tokens, output_tokens, model="claude-3-5-sonnet"):
    """Calculate token cost and append one row to data/token_costs.csv.

    Cost is (input/1M)*in_rate + (output/1M)*out_rate. Rates are looked up by
    model family (haiku/sonnet/opus); unknown models fall back to Sonnet
    pricing ($3 input / $15 output per million tokens).

    The Date column is written in America/New_York with an explicit timezone
    abbreviation (e.g. `2026-04-28 14:30:45 EDT`), 24-hour clock — never UTC.
    Returns the dollar cost so callers can aggregate in-process if they want.
    """
    total_cost = cost_for(model, input_tokens, output_tokens)

    os.makedirs(os.path.dirname(COST_CSV) or ".", exist_ok=True)
    new_file = not os.path.exists(COST_CSV)
    with open(COST_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(HEADERS)
        writer.writerow([
            datetime.now(EASTERN_TZ).strftime(LOG_TIMESTAMP_FMT),
            provider,
            pipeline_name,
            model,
            input_tokens,
            output_tokens,
            f"{total_cost:.6f}",
        ])
    return total_cost
