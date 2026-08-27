"""Best-effort JSON parsing for LLM output.

LLMs occasionally return JSON-shaped output with hallucinated artifacts:

  * Markdown code-fence wrappers (` ```json ... ``` `).
  * Trailing commas before closing braces / brackets.
  * Literal newlines or other control characters embedded in string
    values without being escaped.
  * Unescaped internal quotes that prematurely terminate strings —
    e.g. an `_evidence` field that quotes the source paper verbatim.

A single `json.loads()` call breaks on any of those, which kills the
whole extraction run even when the surrounding payload is fine. This
module's `clean_and_parse_json()` walks a series of progressively
more-forgiving repair stages, returning a parsed dict on success or
`{"error": "..."}` on terminal failure. Callers can apply it uniformly
across providers — already-parsed dicts (Anthropic `tool_use.input`)
pass through unchanged.

Repair stages, in order:

  1. Strip Markdown fences from the outside.
  2. Strip trailing commas before `}` or `]`.
  3. Try strict `json.loads`.
  4. Try `json.loads(..., strict=False)` — allows control chars in strings.
  5. Try `ast.literal_eval` — forgiving of single quotes / Python literals.
  6. Heuristic quote-escape pass + retry `json.loads(..., strict=False)`.
  7. Give up: return `{"error": "Failed to parse JSON: ..."}`.
"""
import ast
import json
import re

_MD_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?")
_MD_FENCE_CLOSE_RE = re.compile(r"\n?\s*```\s*$")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_markdown_fence(text):
    """Strip ` ```json ... ``` ` or plain ` ``` ... ``` ` wrappers."""
    text = _MD_FENCE_OPEN_RE.sub("", text)
    text = _MD_FENCE_CLOSE_RE.sub("", text)
    return text.strip()


def _strip_trailing_commas(text):
    """Drop trailing commas before `}` or `]`. Operates on the whole text;
    a comma immediately followed (modulo whitespace) by `}` or `]` is
    invalid JSON and the model frequently emits one anyway."""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _escape_unescaped_quotes(text):
    """Walk JSON-shaped text and escape quotes that appear inside string
    values where they would otherwise terminate the string prematurely.

    Uses a tiny state machine: `in_string` toggles on every `"` boundary.
    When inside a string and we encounter another `"`, we look ahead past
    whitespace for a structural JSON token (`,`, `:`, `}`, `]`, or end of
    input) — if we find one, this is a real close-quote; otherwise we
    treat it as a stray internal quote and emit `\\"`.

    This is a heuristic, not a parser. It correctly handles the common
    failure mode the extraction model produces (verbatim quotes inside
    `_evidence` fields like `He said "hello" to the patient.`) without
    breaking well-formed JSON.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            # Pass through any existing escape sequence verbatim.
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                out.append(c)
                i += 1
                continue
            # Inside a string. Look ahead past whitespace for a
            # structural boundary; if found, this `"` is the real close.
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j == n or text[j] in ",:}]":
                in_string = False
                out.append(c)
                i += 1
                continue
            # Otherwise treat as an internal quote and escape it.
            out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def clean_and_parse_json(raw):
    """Robust JSON parser for LLM output.

    Returns the parsed dict on success, or `{"error": "..."}` on terminal
    failure (so callers can detect failure with the same `"error" in data`
    check used elsewhere in the pipeline).

    Pass-through behaviour for already-parsed dicts so the helper can be
    applied uniformly across providers — Anthropic's `tool_use.input` is
    already a dict and is returned unchanged.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {"error": f"Cannot parse {type(raw).__name__} as JSON"}

    text = _strip_trailing_commas(_strip_markdown_fence(raw))
    if not text:
        return {"error": "Empty JSON payload after fence/comma cleanup"}

    # Stage 1: strict parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Stage 2: tolerate control characters inside strings (literal
    # newlines, tabs). The model frequently writes multi-line evidence
    # quotes without escaping the newline.
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # Stage 3: ast.literal_eval handles single-quoted strings and Python
    # literals (None / True / False). Wrapped in a try because it raises
    # on JSON-style `null` / `true` / `false` — we just move on.
    try:
        result = ast.literal_eval(text)
        if isinstance(result, dict):
            return result
    except (ValueError, SyntaxError, MemoryError):
        pass

    # Stage 4: heuristic internal-quote escape, then retry permissively.
    try:
        repaired = _escape_unescaped_quotes(text)
        return json.loads(repaired, strict=False)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse JSON: {e}"}
