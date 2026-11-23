"""Exact character-offset section and clause tokenizer for ClinicalTrials.gov eligibility text.

Splits `protocolSection.eligibilityModule.eligibilityCriteria` free text into
non-collapsing document layouts (`SECTION_LAYOUT_LEVELS`), `SectionSpan`s, and
`ClauseSpan`s without normalizing or shifting source indices.

Every `SectionSpan` and `ClauseSpan` upholds the hard slice invariant:
    `raw_text[span.start_char:span.end_char] == span.verbatim_text`
across LF, CRLF, lone CR, Unicode symbols, Markdown emphasis, numbered/bulleted
lists, multi-cohort protocols, and indented or inline `Note:` / `Exception:`
qualifiers.

Unheaded text is never silently assumed to be Inclusion or Exclusion: clauses
outside a recognized header carry `section_kind = "unheaded"`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SECTION_KINDS: tuple[str, ...] = ("inclusion", "exclusion", "unheaded")

SECTION_LAYOUT_LEVELS: tuple[str, ...] = (
    "both_sections",
    "inclusion_only",
    "exclusion_only",
    "unheaded_text",
    "empty_or_placeholder",
)

# Neutral outer wrappers that introduce the field itself without specifying polarity.
_NEUTRAL_BANNER_RX = re.compile(
    r"^(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    r"(?:study\s+|clinical\s+|trial\s+|general\s+|key\s+|main\s+|overall\s+)?"
    r"eligibility(?:\s+criteria|\s+requirements)?"
    r"(?:\*{1,2}|_{1,2})?\s*[:\-–—]?\s*$"
    r"|^[-=*_–—]{3,}$",
    re.IGNORECASE,
)

_NEUTRAL_INLINE_PREFIX_RX = re.compile(
    r"^\s*(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    r"(?:study\s+|clinical\s+|trial\s+|general\s+|key\s+|main\s+)?"
    r"eligibility(?:\s+criteria|\s+requirements)?"
    r"(?:\*{1,2}|_{1,2})?\s*:\s*",
    re.IGNORECASE,
)

_SINGLE_HEADER_INLINE_PREFIX_RX = re.compile(
    r"^\s*(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    r"(?:study\s+|general\s+|key\s+|main\s+)?"
    r"(?:eligibility|inclusion|exclusion)(?:\s+criteria|\s+requirements)?"
    r"(?:\*{1,2}|_{1,2})?\s*:\s*",
    re.IGNORECASE,
)

_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({
    "n/a",
    "na",
    "n.a",
    "n.a.",
    "none",
    "null",
    "nil",
    "tbd",
    "pending",
    "unknown",
    "not applicable",
    "not available",
    "not provided",
    "not specified",
    "not reported",
})

_NO_CRITERIA_PLACEHOLDER_RX = re.compile(
    r"^(?:no|none)\s+(?:specific\s+|known\s+|formal\s+|additional\s+|other\s+)?"
    r"(?:(?:study\s+)?eligibility|inclusion(?:\s+(?:or|and|/)\s+exclusion)?|exclusion)"
    r"(?:\s+criteria|\s+requirements)?"
    r"(?:\s+(?:are\s+|were\s+)?(?:provided|listed|specified|applicable|available|defined|established|known))?$"
    r"|^no\s+criteria(?:\s+(?:provided|listed|specified|applicable))?$"
    r"|^none\s+(?:listed|specified|provided|applicable|known|identified)$",
    re.IGNORECASE,
)

_CONTACT_SITE_PLACEHOLDER_RX = re.compile(
    r"^(?:please\s+)?contact\s+(?:the\s+)?(?:participating\s+|local\s+|study\s+|clinical\s+)?"
    r"(?:site|sites|investigator|team|coordinator|center|centre|sponsor)"
    r"(?:\s+for\s+(?:more\s+|further\s+|additional\s+|study\s+|eligibility\s+)?"
    r"(?:information|details|criteria|eligibility(?:\s+criteria)?))?$"
    r"|^(?:please\s+)?(?:see|refer\s+to)\s+(?:the\s+)?(?:study\s+|clinical\s+|master\s+)?"
    r"protocol(?:\s+for\s+(?:full\s+|complete\s+|detailed\s+)?(?:eligibility\s+)?criteria)?$",
    re.IGNORECASE,
)

# Bullet marker at the start of a trimmed line: unordered (*, -, +, bullets) or ordered (1., 1), (1), a., (a), i.).
# Double asterisk (**bold**) is intentionally NOT matched as an unordered bullet.
_BULLET_PREFIX_RX = re.compile(
    r"^(?:"
    r"(?:\*(?!\*)|[-+•◦▪▸‣–—])\s+"
    r"|(?:\([0-9]{1,3}\)|[0-9]{1,3}[.)])\s+"
    r"|(?:\([a-zA-Z]{1,4}\)|[a-zA-Z][.)])\s+"
    r")"
)

# Standalone section headers: standard Inclusion / Exclusion headers + multi-part/cohort + obligation headers.
_COHORT_FRAGMENT = (
    r"(?:(?:part|cohort|arm|group|substudy|sub-study|step|stage|stratum|module|phase|portion|segment)"
    r"\s+[A-Za-z0-9][A-Za-z0-9./\-]*(?:\s*\([^)\r\n]{1,50}\))?"
    r"|dose\s+(?:escalation|expansion|confirmation|finding|optimization)"
    r"|run[- ]in|lead[- ]in|screening|main\s+study|extension)"
)

_STANDALONE_INCLUSION_RX = re.compile(
    r"^(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    r"(?:(?:\([0-9IVXivx]+\)|[0-9IVXivx]+[.)])\s+)?"
    r"(?:"
    rf"(?:{_COHORT_FRAGMENT}\s*(?:[:\-–—]\s*|\s+))?"
    r"(?:(?:key|main|major|general|core|primary|specific|study|trial|overall|all[- ]participant|"
    r"disease[- ]specific|cohort[- ]specific|additional|other)\s+){0,5}"
    r"(?:eligibility\s+criteria\s*[:\-–—]\s*)?"
    r"(?:inclusion(?:\s+criteria|\s+requirements)?|criteria\s+for\s+inclusion)"
    r"(?:\s+(?:for\s+[^\r\n:]{1,60}|\([^)\r\n]{1,60}\)|\[[^\]\r\n]{1,60}\])){0,3}"
    r"|"
    r"(?:all\s+)?(?:participants?|subjects?|patients?|individuals?|volunteers?)\s+"
    r"(?:must|shall|are\s+required\s+to)\s+(?:meet|satisfy|fulfill|have)\s+"
    r"(?:all\s+of\s+)?the\s+following(?:\s+(?:criteria|conditions|requirements))?"
    r"|"
    r"to\s+be\s+eligible(?:\s+for\s+[^\r\n:,]{1,40})?,?\s*"
    r"(?:(?:participants?|subjects?|patients?|individuals?)\s+)?"
    r"must\s+(?:meet|satisfy|have)(?:\s+(?:all\s+of\s+)?the\s+following(?:\s+criteria)?)?"
    r"|"
    r"(?:participants?|subjects?|patients?|individuals?)\s+are\s+eligible\s+"
    r"(?:to\s+be\s+included(?:\s+in\s+the\s+study)?\s+)?"
    r"(?:only\s+)?if\s+(?:all\s+of\s+)?the\s+following(?:\s+criteria)?\s+apply"
    r")"
    r"(?:\*{1,2}|_{1,2})?\s*[:\-–—]?\s*(?:\*{1,2}|_{1,2})?$",
    re.IGNORECASE,
)

_STANDALONE_EXCLUSION_RX = re.compile(
    r"^(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    r"(?:(?:\([0-9IVXivx]+\)|[0-9IVXivx]+[.)])\s+)?"
    r"(?:"
    rf"(?:{_COHORT_FRAGMENT}\s*(?:[:\-–—]\s*|\s+))?"
    r"(?:(?:key|main|major|general|core|primary|specific|study|trial|overall|all[- ]participant|"
    r"disease[- ]specific|cohort[- ]specific|additional|other|medical|prior[- ]therapy)\s+){0,5}"
    r"(?:eligibility\s+criteria\s*[:\-–—]\s*)?"
    r"(?:exclusion(?:\s+criteria|\s+requirements)?|criteria\s+for\s+exclusion)"
    r"(?:\s+(?:for\s+[^\r\n:]{1,60}|\([^)\r\n]{1,60}\)|\[[^\]\r\n]{1,60}\])){0,3}"
    r"|"
    r"(?:all\s+)?(?:participants?|subjects?|patients?|individuals?|volunteers?)\s+"
    r"(?:must|may|shall|should|can)\s+not\s+(?:have|meet|present\s+with|exhibit)\s*"
    r"(?:any\s+of\s+the\s+following(?:\s+(?:criteria|conditions|characteristics))?)?"
    r"|"
    r"(?:participants?|subjects?|patients?|individuals?)\s+with\s+any\s+of\s+the\s+following\s+"
    r"(?:are|will\s+be|shall\s+be)\s+(?:excluded|ineligible)(?:\s+from\s+(?:the\s+)?study)?"
    r"|"
    r"(?:participants?|subjects?|patients?|individuals?)\s+are\s+excluded(?:\s+from\s+the\s+study)?\s+"
    r"if\s+any\s+of\s+the\s+following(?:\s+criteria)?\s+apply"
    r"|"
    r"any\s+of\s+the\s+following(?:\s+(?:criteria|conditions))?\s+"
    r"(?:will\s+)?exclude(?:s)?(?:\s+(?:a\s+|the\s+)?(?:participant|subject|patient|individual))?"
    r")"
    r"(?:\*{1,2}|_{1,2})?\s*[:\-–—]?\s*(?:\*{1,2}|_{1,2})?$",
    re.IGNORECASE,
)

# Inline header followed on the same line by clause body, e.g. "Inclusion Criteria: Age 18-65."
_INLINE_HEADER_RX = re.compile(
    r"^(?P<header>"
    r"(?:#+\s*)?(?:\*{1,2}|_{1,2})?"
    rf"(?:{_COHORT_FRAGMENT}\s*(?:[:\-–—]\s*|\s+))?"
    r"(?:(?:key|main|major|general|specific|study)\s+)?"
    r"(?P<kind>inclusion|exclusion)(?:\s+criteria)?"
    r"(?:\s*\([^)\r\n]{1,50}\))?"
    r"(?:\*{1,2}|_{1,2})?\s*:"
    r")\s*(?P<body>\S.*)$",
    re.IGNORECASE,
)

# Secondary inline header inside the same single-line paragraph, e.g. "... years. Exclusion Criteria: ..."
_MIDLINE_HEADER_RX = re.compile(
    r"(?<=[.;])\s+"
    r"(?P<header>"
    rf"(?:{_COHORT_FRAGMENT}\s+)?"
    r"(?:(?:key|main|major|general)\s+)?"
    r"(?P<kind>inclusion|exclusion)(?:\s+criteria)?\s*:"
    r")\s*(?P<body>\S)",
    re.IGNORECASE,
)

_COHORT_EXTRACT_RX = re.compile(
    r"\b(?:"
    r"(?:part|cohort|arm|group|substudy|sub-study|step|stage|stratum|module|phase|portion|segment)\s+[A-Za-z0-9][A-Za-z0-9./\-]*"
    r"|dose\s+(?:escalation|expansion|confirmation|finding|optimization)"
    r"|run[- ]in|lead[- ]in|screening|main\s+study|extension"
    r")\b",
    re.IGNORECASE,
)

# Lines that qualify/temper the immediately preceding clause rather than starting an independent criterion.
_QUALIFIER_PREFIX_RX = re.compile(
    r"^(?:[\(\[\{]\s*)?"
    r"(?:note|notes|exception|exceptions|allowance|allowances|clarification|caveat|nb|n\.b\.|"
    r"please\s+note|important\s+note)\s*[:\-–—]"
    r"|^(?:[\(\[\{]\s*)?(?:unless|except(?:\s+for|\s+if|\s+when|\s+where|\s*:)|"
    r"provided\s+that|with\s+the\s+exception\s+of|excluding\b|however\s*,)",
    re.IGNORECASE,
)

# In Exclusion sections, an explicit eligibility allowance sentence right after a bullet qualifies that bullet.
_EXCLUSION_ALLOWANCE_SENTENCE_RX = re.compile(
    r"^(?:patients?|participants?|subjects?|individuals?|women|men|persons?)\s+"
    r"(?:with|who\s+(?:have|had|are)|on|receiving)\b[^\r\n]{0,160}\b"
    r"(?:are|is|will\s+be|may\s+be)\s+"
    r"(?:eligible|permitted|allowed|acceptable|not\s+excluded)\b",
    re.IGNORECASE,
)

# Parent stem lines that introduce independent sub-criteria (e.g., organ function labs) rather than exceptions.
_SUBLIST_INTRODUCER_RX = re.compile(
    r"(?:"
    r"as\s+(?:defined|outlined|specified|listed|described)\s+(?:below|follows)"
    r"|as\s+follows"
    r"|meeting\s+(?:all\s+of\s+)?the\s+following(?:\s+(?:criteria|parameters|values|definitions))?"
    r"|must\s+(?:meet|have)\s+(?:all\s+of\s+)?the\s+following"
    r"|adequate\s+[^\r\n:]{1,60}function(?:\s+criteria|\s+defined\s+as)?"
    r"|laboratory\s+(?:values?|parameters?|criteria)"
    r")\s*:\s*$",
    re.IGNORECASE,
)

_EXCEPTION_INTRODUCER_RX = re.compile(
    r"(?:except(?:\s+for|\s+as\s+follows)?|unless(?:\s+all\s+of\s+the\s+following)?|"
    r"with\s+the\s+following\s+exceptions?|excluding)\s*:\s*$",
    re.IGNORECASE,
)

_CONTINUATION_END_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "nor", "but", "of", "to", "in", "for",
    "with", "without", "from", "by", "at", "on", "as", "into", "onto", "per",
    "via", "between", "within", "under", "over", "above", "below", "than",
    "that", "which", "who", "whom", "whose", "where", "when", "while", "if",
    "including", "such", "either", "neither", "both", "any", "all", "no",
    "not", "never", "prior", "previous", "active", "known", "acute", "chronic",
    "severe", "moderate", "mild", "uncontrolled", "untreated", "treated",
    "stable", "confirmed", "documented", "histologically", "cytologically",
    "adequate", "normal", "abnormal", "positive", "negative", "must", "should",
    "may", "can", "will", "shall", "have", "has", "had", "be", "is", "are",
    "was", "were", "been", "being", "able", "unable", "willing", "unwilling",
    "history", "evidence", "diagnosis", "presence", "absence", "requiring",
})

def _is_clause_placeholder(core_text: str) -> bool:
    cleaned = re.sub(r"^[\s*_#\-–—:;,.!]+|[\s*_#\-–—:;,.!]+$", "", core_text.strip())
    norm = re.sub(r"\s+", " ", cleaned).strip().lower()
    if not norm:
        return True
    if norm in _PLACEHOLDER_TOKENS:
        return True
    if _NO_CRITERIA_PLACEHOLDER_RX.match(norm):
        return True
    return False


@dataclass(frozen=True)
class SectionSpan:
    """Exact character-offset span of a section within `eligibilityCriteria`."""

    section_index: int
    section_kind: str
    section_header: str | None
    header_start_char: int | None
    header_end_char: int | None
    start_char: int
    end_char: int
    verbatim_text: str
    cohort_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "section_index": self.section_index,
            "section_kind": self.section_kind,
            "section_header": self.section_header,
            "header_start_char": self.header_start_char,
            "header_end_char": self.header_end_char,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "verbatim_text": self.verbatim_text,
            "cohort_label": self.cohort_label,
        }


@dataclass(frozen=True)
class ClauseSpan:
    """Exact character-offset span of an individual criterion clause (with attached sub-lines)."""

    clause_index: int
    section_index: int
    section_kind: str
    section_header: str | None
    start_char: int
    end_char: int
    verbatim_text: str
    cohort_label: str | None = None
    bullet_marker: str | None = None
    has_attached_subclause: bool = False
    attached_exception_spans: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "clause_index": self.clause_index,
            "section_index": self.section_index,
            "section_kind": self.section_kind,
            "section_header": self.section_header,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "verbatim_text": self.verbatim_text,
            "cohort_label": self.cohort_label,
            "bullet_marker": self.bullet_marker,
            "has_attached_subclause": self.has_attached_subclause,
            "attached_exception_spans": [list(sp) for sp in self.attached_exception_spans],
        }


@dataclass(frozen=True)
class SectionedText:
    """Document-level tokenization result for `eligibilityCriteria`."""

    raw_text: str
    section_layout: str
    placeholder_reason: str | None
    sections: tuple[SectionSpan, ...]
    clauses: tuple[ClauseSpan, ...]
    has_unheaded_preamble: bool
    is_multi_cohort: bool


@dataclass(frozen=True)
class _LineSlice:
    """Internal raw line or sub-line slice with exact offsets into `raw_text`."""

    raw_start: int
    raw_end: int
    trimmed_start: int
    trimmed_end: int
    indent_cols: int


@dataclass
class _ClauseAccum:
    """Mutable accumulator while grouping continuation and qualifier sub-lines into a clause."""

    start_char: int
    end_char: int
    root_indent: int
    is_bullet: bool
    bullet_marker: str | None
    introduces_sublist: bool
    last_line_text: str
    has_attached_subclause: bool
    attached_exception_spans: list[tuple[int, int]]
    in_attached_exception: bool = False


def _indent_columns(prefix: str) -> int:
    cols = 0
    for ch in prefix:
        if ch == "\t":
            cols += 4
        else:
            cols += 1
    return cols


def _scan_raw_lines(raw_text: str) -> list[_LineSlice]:
    """Scan `raw_text` into exact line slices without altering CRLF, CR, LF, or Unicode offsets."""
    out: list[_LineSlice] = []
    pos = 0
    n = len(raw_text)
    while pos < n:
        end = pos
        while end < n and raw_text[end] not in ("\r", "\n"):
            end += 1
        content_start = pos
        content_end = end
        if end < n:
            if raw_text[end] == "\r" and end + 1 < n and raw_text[end + 1] == "\n":
                next_pos = end + 2
            else:
                next_pos = end + 1
        else:
            next_pos = end

        ts = content_start
        while ts < content_end and raw_text[ts].isspace():
            ts += 1
        te = content_end
        while te > ts and raw_text[te - 1].isspace():
            te -= 1

        indent_cols = _indent_columns(raw_text[content_start:ts])
        out.append(
            _LineSlice(
                raw_start=content_start,
                raw_end=content_end,
                trimmed_start=ts,
                trimmed_end=te,
                indent_cols=indent_cols,
            )
        )
        pos = next_pos
    return out


def _classify_placeholder(raw_text: str | None) -> str | None:
    """Return placeholder reason if `raw_text` is missing, blank, or a non-substantive placeholder."""
    if raw_text is None:
        return "missing"
    if raw_text == "":
        return "empty"
    if raw_text.strip() == "":
        return "whitespace_only"

    cleaned = _SINGLE_HEADER_INLINE_PREFIX_RX.sub("", raw_text.strip())
    cleaned = re.sub(r"^[\s*_#\-–—:;,.!]+|[\s*_#\-–—:;,.!]+$", "", cleaned)
    norm = re.sub(r"\s+", " ", cleaned).strip().lower()
    if not norm:
        return "placeholder_token"
    if norm in _PLACEHOLDER_TOKENS:
        return "placeholder_token"
    if _NO_CRITERIA_PLACEHOLDER_RX.match(norm):
        return "no_criteria_placeholder"
    if _CONTACT_SITE_PLACEHOLDER_RX.match(norm):
        return "contact_site_placeholder"
    return None


def _extract_cohort_label(header_text: str) -> str | None:
    m = _COHORT_EXTRACT_RX.search(header_text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip()


def _detect_standalone_header(trimmed: str) -> tuple[str, str | None] | None:
    """Classify a standalone line as `("inclusion"|"exclusion", cohort_label)` or `None`."""
    candidate = trimmed
    # Allow Markdown bullet `* Inclusion Criteria:` ONLY when nothing substantive follows the header.
    if candidate.startswith(("* ", "- ", "• ")):
        inner = candidate[2:].strip()
        if not inner.endswith(":"):
            return None
        candidate = inner
    if _STANDALONE_INCLUSION_RX.match(candidate):
        return ("inclusion", _extract_cohort_label(candidate))
    if _STANDALONE_EXCLUSION_RX.match(candidate):
        return ("exclusion", _extract_cohort_label(candidate))
    return None


def _split_inline_headers_on_line(
    raw_text: str, line: _LineSlice
) -> list[tuple[str | None, int | None, int | None, str | None, _LineSlice | None]]:
    """Inspect one line for standalone or inline headers.

    Returns a list of events `(section_kind_if_new_header, hdr_start, hdr_end, cohort_label, body_slice)`.
    When the line is an ordinary non-header line, returns `[(None, None, None, None, line)]`.
    """
    if line.trimmed_start == line.trimmed_end:
        return [(None, None, None, None, line)]

    trimmed = raw_text[line.trimmed_start:line.trimmed_end]

    standalone = _detect_standalone_header(trimmed)
    if standalone is not None:
        kind, cohort = standalone
        return [(kind, line.trimmed_start, line.trimmed_end, cohort, None)]

    # Do not treat bullet items like "- Meets all inclusion criteria: yes" as section headers.
    if _BULLET_PREFIX_RX.match(trimmed):
        return [(None, None, None, None, line)]

    eff_start = line.trimmed_start
    m_neut = _NEUTRAL_INLINE_PREFIX_RX.match(trimmed)
    if m_neut:
        eff_start = line.trimmed_start + m_neut.end()
        while eff_start < line.trimmed_end and raw_text[eff_start].isspace():
            eff_start += 1
        if eff_start >= line.trimmed_end:
            return [(None, None, None, None, None)]

    eff_trimmed = raw_text[eff_start:line.trimmed_end]
    m_inline = _INLINE_HEADER_RX.match(eff_trimmed)
    if not m_inline:
        # Check whether a midline header follows an unheaded sentence on the same line.
        m_first_mid = _MIDLINE_HEADER_RX.search(eff_trimmed)
        if not m_first_mid:
            return [(None, None, None, None, line)]
        events_mid: list[tuple[str | None, int | None, int | None, str | None, _LineSlice | None]] = []
        pre_end = eff_start + m_first_mid.start("header")
        while pre_end > eff_start and raw_text[pre_end - 1].isspace():
            pre_end -= 1
        if pre_end > eff_start:
            events_mid.append(
                (
                    None,
                    None,
                    None,
                    None,
                    _LineSlice(
                        raw_start=eff_start,
                        raw_end=pre_end,
                        trimmed_start=eff_start,
                        trimmed_end=pre_end,
                        indent_cols=0,
                    ),
                )
            )
        tail_line = _LineSlice(
            raw_start=eff_start + m_first_mid.start("header"),
            raw_end=line.trimmed_end,
            trimmed_start=eff_start + m_first_mid.start("header"),
            trimmed_end=line.trimmed_end,
            indent_cols=0,
        )
        events_mid.extend(_split_inline_headers_on_line(raw_text, tail_line))
        return events_mid

    events: list[tuple[str | None, int | None, int | None, str | None, _LineSlice | None]] = []
    base = eff_start
    hdr_start = base + m_inline.start("header")
    hdr_end = base + m_inline.end("header")
    first_kind = m_inline.group("kind").lower()
    first_cohort = _extract_cohort_label(raw_text[hdr_start:hdr_end])
    body_abs_start = base + m_inline.start("body")
    body_abs_end = line.trimmed_end

    # Check if a second inline header occurs later on the same line (e.g. "... . Exclusion Criteria: ...").
    remainder = raw_text[body_abs_start:body_abs_end]
    cur_kind = first_kind
    cur_hdr_s = hdr_start
    cur_hdr_e = hdr_end
    cur_cohort = first_cohort
    cur_body_s = body_abs_start

    for m_mid in _MIDLINE_HEADER_RX.finditer(remainder):
        seg_end = body_abs_start + m_mid.start("header")
        while seg_end > cur_body_s and raw_text[seg_end - 1].isspace():
            seg_end -= 1
        body_slice = (
            _LineSlice(
                raw_start=cur_body_s,
                raw_end=seg_end,
                trimmed_start=cur_body_s,
                trimmed_end=seg_end,
                indent_cols=0,
            )
            if seg_end > cur_body_s
            else None
        )
        events.append((cur_kind, cur_hdr_s, cur_hdr_e, cur_cohort, body_slice))
        cur_hdr_s = body_abs_start + m_mid.start("header")
        cur_hdr_e = body_abs_start + m_mid.end("header")
        cur_kind = m_mid.group("kind").lower()
        cur_cohort = _extract_cohort_label(raw_text[cur_hdr_s:cur_hdr_e])
        cur_body_s = body_abs_start + m_mid.start("body")

    final_end = body_abs_end
    while final_end > cur_body_s and raw_text[final_end - 1].isspace():
        final_end -= 1
    final_slice = (
        _LineSlice(
            raw_start=cur_body_s,
            raw_end=final_end,
            trimmed_start=cur_body_s,
            trimmed_end=final_end,
            indent_cols=0,
        )
        if final_end > cur_body_s
        else None
    )
    events.append((cur_kind, cur_hdr_s, cur_hdr_e, cur_cohort, final_slice))
    return events


def _strip_bullet_marker(trimmed: str) -> tuple[str | None, str]:
    m = _BULLET_PREFIX_RX.match(trimmed)
    if not m:
        return None, trimmed
    marker = m.group(0).strip()
    rest = trimmed[m.end():]
    return marker, rest


def _is_qualifier_line(core_text: str, section_kind: str, indent_cols: int, root_indent: int) -> bool:
    if _QUALIFIER_PREFIX_RX.match(core_text):
        return True
    if section_kind == "exclusion" and _EXCLUSION_ALLOWANCE_SENTENCE_RX.match(core_text):
        return True
    if indent_cols > root_indent and _EXCLUSION_ALLOWANCE_SENTENCE_RX.match(core_text):
        return True
    return False


def _is_sublist_introducer(text: str) -> bool:
    if _EXCEPTION_INTRODUCER_RX.search(text):
        return False
    return bool(_SUBLIST_INTRODUCER_RX.search(text))


def _ends_with_continuation(line_text: str) -> bool:
    s = line_text.rstrip()
    if not s:
        return False
    if s[-1] in (",", ":", "(", "[", "/", "-", "–", "—"):
        return True
    if s[-1] in (".", ";"):
        # Common abbreviations ending in a period continue onto the next token.
        low = s.lower()
        if low.endswith(("e.g.", "i.e.", "vs.", "approx.", "dr.", "mr.", "mrs.", "ms.")):
            return True
        return False
    m = re.search(r"([A-Za-z]+)$", s)
    if m and m.group(1).lower() in _CONTINUATION_END_WORDS:
        return True
    return False


def _starts_as_continuation(line_text: str) -> bool:
    if not line_text:
        return False
    ch = line_text[0]
    if ch.islower():
        return True
    if ch in (")", "]", ",", ".", ";", ":"):
        return True
    low_word_m = re.match(r"^([A-Za-z]+)\b", line_text)
    if low_word_m and low_word_m.group(1).lower() in ("and", "or", "nor", "with", "without", "to", "of", "for"):
        # Lowercase-like conjunctions only if whole line begins with conjunction + lowercase continuation
        rest = line_text[low_word_m.end():].lstrip()
        if rest and rest[0].islower():
            return True
    return False


def _build_clauses_for_section(
    raw_text: str,
    body_lines: list[_LineSlice],
    section_index: int,
    section_kind: str,
    section_header: str | None,
    cohort_label: str | None,
    starting_clause_index: int,
) -> list[ClauseSpan]:
    clauses: list[ClauseSpan] = []
    cur: _ClauseAccum | None = None
    blank_gap = 0

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        verbatim = raw_text[cur.start_char:cur.end_char]
        # Skip standalone placeholder tokens like "None" or "No specific exclusion criteria" under a header.
        _, core = _strip_bullet_marker(verbatim.strip())
        if _is_clause_placeholder(core):
            cur = None
            return
        if raw_text[cur.start_char:cur.end_char] != verbatim:
            raise AssertionError("ClauseSpan slice invariant violated")
        clauses.append(
            ClauseSpan(
                clause_index=starting_clause_index + len(clauses),
                section_index=section_index,
                section_kind=section_kind,
                section_header=section_header,
                start_char=cur.start_char,
                end_char=cur.end_char,
                verbatim_text=verbatim,
                cohort_label=cohort_label,
                bullet_marker=cur.bullet_marker,
                has_attached_subclause=cur.has_attached_subclause,
                attached_exception_spans=tuple(cur.attached_exception_spans),
            )
        )
        cur = None

    for ln in body_lines:
        if ln.trimmed_start == ln.trimmed_end:
            blank_gap += 1
            continue

        trimmed = raw_text[ln.trimmed_start:ln.trimmed_end]
        # Skip neutral outer banners like "Eligibility Criteria:" when unheaded.
        if _NEUTRAL_BANNER_RX.match(trimmed):
            blank_gap += 1
            continue

        bullet_marker, core = _strip_bullet_marker(trimmed)
        is_bullet = bullet_marker is not None

        if cur is not None:
            # 1. Explicit Note: / Exception: / Unless / Allowance lines attach to their parent clause
            #    even across a single blank line, so clinical exceptions are never severed.
            if blank_gap <= 1 and _is_qualifier_line(core, section_kind, ln.indent_cols, cur.root_indent):
                cur.end_char = ln.trimmed_end
                cur.last_line_text = trimmed
                cur.has_attached_subclause = True
                cur.attached_exception_spans.append((ln.trimmed_start, ln.trimmed_end))
                cur.in_attached_exception = True
                blank_gap = 0
                continue

            # 2. Indented sub-bullets under a criterion attach to that parent criterion unless the
            #    parent explicitly introduces a standalone lab/parameter sublist ("as defined below:").
            if (
                blank_gap == 0
                and is_bullet
                and ln.indent_cols >= cur.root_indent + 2
                and not cur.introduces_sublist
            ):
                cur.end_char = ln.trimmed_end
                cur.last_line_text = trimmed
                cur.has_attached_subclause = True
                cur.attached_exception_spans.append((ln.trimmed_start, ln.trimmed_end))
                cur.in_attached_exception = True
                blank_gap = 0
                continue

            # 3. Soft-wrapped non-bullet continuation lines without a blank gap.
            if blank_gap == 0 and not is_bullet:
                if (
                    (cur.is_bullet and ln.indent_cols > cur.root_indent)
                    or _starts_as_continuation(trimmed)
                    or _ends_with_continuation(cur.last_line_text)
                ):
                    cur.end_char = ln.trimmed_end
                    cur.last_line_text = trimmed
                    cur.introduces_sublist = _is_sublist_introducer(trimmed)
                    if cur.in_attached_exception and cur.attached_exception_spans:
                        prev_s, _ = cur.attached_exception_spans[-1]
                        cur.attached_exception_spans[-1] = (prev_s, ln.trimmed_end)
                    blank_gap = 0
                    continue

            flush()

        cur = _ClauseAccum(
            start_char=ln.trimmed_start,
            end_char=ln.trimmed_end,
            root_indent=ln.indent_cols,
            is_bullet=is_bullet,
            bullet_marker=bullet_marker,
            introduces_sublist=_is_sublist_introducer(trimmed),
            last_line_text=trimmed,
            has_attached_subclause=False,
            attached_exception_spans=[],
        )
        blank_gap = 0

    flush()
    return clauses


def parse_eligibility_sections(raw_text: str | None) -> SectionedText:
    """Tokenize `eligibilityCriteria` into `SectionSpan`s and `ClauseSpan`s with exact char offsets."""
    if raw_text is not None and not isinstance(raw_text, str):
        raise TypeError(f"eligibilityCriteria must be str or None, got {type(raw_text).__name__}")

    placeholder_reason = _classify_placeholder(raw_text)
    if placeholder_reason is not None:
        return SectionedText(
            raw_text="" if raw_text is None else raw_text,
            section_layout="empty_or_placeholder",
            placeholder_reason=placeholder_reason,
            sections=(),
            clauses=(),
            has_unheaded_preamble=False,
            is_multi_cohort=False,
        )

    assert raw_text is not None
    raw_lines = _scan_raw_lines(raw_text)

    # Group lines into raw section buckets driven by standalone or inline headers.
    # Each bucket: (kind, hdr_start, hdr_end, cohort_label, list[_LineSlice])
    buckets: list[tuple[str, int | None, int | None, str | None, list[_LineSlice]]] = []
    cur_kind = "unheaded"
    cur_hdr_s: int | None = None
    cur_hdr_e: int | None = None
    cur_cohort: str | None = None
    cur_lines: list[_LineSlice] = []

    for ln in raw_lines:
        events = _split_inline_headers_on_line(raw_text, ln)
        for ev_kind, hdr_s, hdr_e, cohort, body_ln in events:
            if ev_kind is not None:
                if cur_hdr_s is not None or any(
                    x.trimmed_start < x.trimmed_end
                    and not _NEUTRAL_BANNER_RX.match(raw_text[x.trimmed_start:x.trimmed_end])
                    for x in cur_lines
                ):
                    buckets.append((cur_kind, cur_hdr_s, cur_hdr_e, cur_cohort, cur_lines))
                cur_kind = ev_kind
                cur_hdr_s = hdr_s
                cur_hdr_e = hdr_e
                cur_cohort = cohort
                cur_lines = []
                if body_ln is not None:
                    cur_lines.append(body_ln)
            elif body_ln is not None:
                cur_lines.append(body_ln)

    if cur_hdr_s is not None or any(
        x.trimmed_start < x.trimmed_end
        and not _NEUTRAL_BANNER_RX.match(raw_text[x.trimmed_start:x.trimmed_end])
        for x in cur_lines
    ):
        buckets.append((cur_kind, cur_hdr_s, cur_hdr_e, cur_cohort, cur_lines))

    sections: list[SectionSpan] = []
    all_clauses: list[ClauseSpan] = []

    for b_kind, hdr_s, hdr_e, cohort, b_lines in buckets:
        sec_idx = len(sections)
        header_str = raw_text[hdr_s:hdr_e] if (hdr_s is not None and hdr_e is not None) else None
        sec_clauses = _build_clauses_for_section(
            raw_text=raw_text,
            body_lines=b_lines,
            section_index=sec_idx,
            section_kind=b_kind,
            section_header=header_str,
            cohort_label=cohort,
            starting_clause_index=len(all_clauses),
        )

        # Compute exact section bounds from header through last substantive line or clause.
        non_empty_lines = [x for x in b_lines if x.trimmed_start < x.trimmed_end]
        if hdr_s is not None and hdr_e is not None:
            sec_start = hdr_s
            sec_end = sec_clauses[-1].end_char if sec_clauses else (
                non_empty_lines[-1].trimmed_end if non_empty_lines else hdr_e
            )
        elif sec_clauses:
            sec_start = sec_clauses[0].start_char
            sec_end = sec_clauses[-1].end_char
        elif non_empty_lines:
            sec_start = non_empty_lines[0].trimmed_start
            sec_end = non_empty_lines[-1].trimmed_end
        else:
            continue

        sec_verbatim = raw_text[sec_start:sec_end]
        if raw_text[sec_start:sec_end] != sec_verbatim:
            raise AssertionError("SectionSpan slice invariant violated")

        sections.append(
            SectionSpan(
                section_index=sec_idx,
                section_kind=b_kind,
                section_header=header_str,
                header_start_char=hdr_s,
                header_end_char=hdr_e,
                start_char=sec_start,
                end_char=sec_end,
                verbatim_text=sec_verbatim,
                cohort_label=cohort,
            )
        )
        all_clauses.extend(sec_clauses)

    # Verify the slice invariant explicitly across every emitted SectionSpan and ClauseSpan.
    for sec in sections:
        if raw_text[sec.start_char:sec.end_char] != sec.verbatim_text:
            raise AssertionError(f"SectionSpan offset mismatch at {sec.start_char}:{sec.end_char}")
    for cl in all_clauses:
        if raw_text[cl.start_char:cl.end_char] != cl.verbatim_text:
            raise AssertionError(f"ClauseSpan offset mismatch at {cl.start_char}:{cl.end_char}")

    if not all_clauses:
        return SectionedText(
            raw_text=raw_text,
            section_layout="empty_or_placeholder",
            placeholder_reason="no_criteria_placeholder",
            sections=(),
            clauses=(),
            has_unheaded_preamble=False,
            is_multi_cohort=False,
        )

    n_inc_sec = sum(1 for s in sections if s.section_kind == "inclusion")
    n_exc_sec = sum(1 for s in sections if s.section_kind == "exclusion")
    has_unheaded_preamble = bool(
        sections
        and sections[0].section_kind == "unheaded"
        and (n_inc_sec > 0 or n_exc_sec > 0)
    )

    if n_inc_sec > 0 and n_exc_sec > 0:
        layout = "both_sections"
    elif n_inc_sec > 0:
        layout = "inclusion_only"
    elif n_exc_sec > 0:
        layout = "exclusion_only"
    else:
        layout = "unheaded_text"

    distinct_cohorts = {s.cohort_label.lower() for s in sections if s.cohort_label}
    is_multi_cohort = len(distinct_cohorts) >= 2 or n_inc_sec >= 2 or n_exc_sec >= 2

    return SectionedText(
        raw_text=raw_text,
        section_layout=layout,
        placeholder_reason=None,
        sections=tuple(sections),
        clauses=tuple(all_clauses),
        has_unheaded_preamble=has_unheaded_preamble,
        is_multi_cohort=is_multi_cohort,
    )
