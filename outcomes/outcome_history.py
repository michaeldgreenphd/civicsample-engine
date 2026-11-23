"""Deterministic registration-to-results and historical-snapshot primary outcome fidelity diffs.

Pure functions over ClinicalTrials.gov API v2 study records (`protocolSection`, `resultsSection`,
`hasResults`) plus an optional frozen `jsonl.gz` archive of historical study-version snapshots
keyed by `nct_id`.

Two independent comparisons are reported per trial:

1. Registration-to-results (`protocol_to_results`): compares current
   `protocolSection.outcomesModule.primaryOutcomes` against posted `PRIMARY` outcome measures in
   `resultsSection.outcomeMeasuresModule.outcomeMeasures`. Evaluated whenever posted results exist,
   whether or not a historical snapshot archive is supplied.
2. Historical-to-current registration (`version_history`): compares an optional frozen initial or
   pre-completion snapshot's outcomes against current `protocolSection.outcomesModule`. When no
   historical archive is supplied, `version_history_state` is recorded explicitly as
   `"history_archive_not_provided"` with `diffs = None` (never coerced to `"unchanged"`).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

METHOD_VERSION = 'outcome-history-1'

PROTOCOL_TO_RESULTS_STATES = (
    'verbatim_measure_and_timeframe_match',
    'timeframe_differing',
    'measure_title_differing',
    'registered_primary_missing_in_results',
    'results_primary_unlisted_in_protocol',
)

PROTOCOL_TO_RESULTS_RECORD_STATES = (
    'evaluated',
    'not_applicable_no_posted_results',
    'not_retrieved',
    'parse_failed',
)

VERSION_HISTORY_STATES = (
    'history_archive_not_provided',
    'trial_not_in_history_archive',
    'evaluated',
    'parse_failed',
)

HISTORY_DIFF_STATES = (
    'unchanged',
    'primary_added',
    'primary_dropped',
    'primary_demoted_to_secondary',
    'secondary_promoted_to_primary',
    'timeframe_changed',
    'measure_reworded',
)

_NCT_RE = re.compile(r'^NCT\d{8}$')
_TOKEN_RE = re.compile(r'[a-z0-9]+')

# Function words and statistical summary-framing words are excluded when testing whether two
# differently worded outcome titles refer to the same clinical construct. Keeping framing words out
# prevents "Change from Baseline in Systolic Blood Pressure" and "Change from Baseline in Body
# Weight" from pairing on {"change", "baseline"} when one endpoint was dropped and another added.
_STOPWORDS = frozenset({
    'a', 'an', 'and', 'as', 'at', 'be', 'by', 'for', 'from', 'in', 'into', 'is', 'of', 'on', 'or',
    'per', 'that', 'the', 'their', 'through', 'to', 'up', 'was', 'were', 'with', 'within',
})

_FRAMING_WORDS = frozenset({
    'adjusted', 'all', 'analysis', 'arm', 'arms', 'assessed', 'assessment', 'average', 'baseline',
    'between', 'change', 'changes', 'compared', 'comparison', 'count', 'counts', 'difference',
    'differences', 'endpoint', 'evaluated', 'group', 'groups', 'least', 'ls', 'mean', 'means',
    'measure', 'measured', 'measurement', 'measurements', 'median', 'number', 'outcome',
    'outcomes', 'participant', 'participants', 'patient', 'patients', 'pct', 'percent',
    'percentage', 'primary', 'proportion', 'rate', 'rates', 'raw', 'reported', 'square', 'squares',
    'study', 'subject', 'subjects', 'summary', 'total', 'treatment', 'trial', 'unadjusted',
    'value', 'values',
})

# Mutually exclusive clinical poles: if one title names one pole and the other names its opposite
# (and neither names both), they measure distinct clinical constructs even when other words match
# (e.g. "Systolic Blood Pressure" vs "Diastolic Blood Pressure", "Overall Survival" vs
# "Progression-Free Survival").
_OPPOSING_POLES: tuple[tuple[str, str], ...] = (
    ('systolic', 'diastolic'),
    ('overall', 'progression'),
    ('sensitivity', 'specificity'),
    (' relapse ', ' remission '),
    ('invasive', 'noninvasive'),
)


class HistoryArchiveError(ValueError):
    """Raised when a historical version snapshot archive is malformed or fails integrity checks."""


def normalize_outcome_text(text: Any) -> str:
    """Unicode-NFC, whitespace-collapsed, case-folded text; empty string when absent or blank."""
    if not isinstance(text, str):
        return ''
    folded = unicodedata.normalize('NFC', text).strip()
    if not folded:
        return ''
    return ' '.join(folded.split()).casefold()


def construct_tokens(text: Any) -> frozenset[str]:
    """Substantive clinical-construct tokens from an outcome measure title."""
    norm = normalize_outcome_text(text)
    if not norm:
        return frozenset()
    raw_tokens = _TOKEN_RE.findall(norm)
    filtered = frozenset(t for t in raw_tokens if t not in _STOPWORDS and t not in _FRAMING_WORDS)
    if filtered:
        return filtered
    # Fallback when a title consists solely of framing tokens (e.g. "Number of Participants"):
    # retain non-stopword tokens so identical/near-identical administrative titles still tokenize.
    return frozenset(t for t in raw_tokens if t not in _STOPWORDS)


def _has_opposing_pole_conflict(tokens_a: frozenset[str], tokens_b: frozenset[str]) -> bool:
    for left, right in _OPPOSING_POLES:
        l_tok, r_tok = left.strip(), right.strip()
        if (l_tok in tokens_a and r_tok in tokens_b and r_tok not in tokens_a and l_tok not in tokens_b):
            return True
        if (r_tok in tokens_a and l_tok in tokens_b and l_tok not in tokens_a and r_tok not in tokens_b):
            return True
    return False


def titles_share_construct(title_a: Any, title_b: Any) -> tuple[bool, float]:
    """Return `(related, jaccard)` for two non-identical outcome titles.

    Two titles are treated as reworded versions of the same clinical construct when they have no
    opposing-pole conflict and either:
      - construct-token Jaccard similarity >= 0.50,
      - every construct token of the shorter title appears in the longer title (containment == 1.0,
        e.g. "Change in HbA1c" -> {"hba1c"} inside "Mean Change in Glycosylated Hemoglobin HbA1c"),
      - at least two construct tokens overlap with Jaccard >= 0.34.
    """
    ta = construct_tokens(title_a)
    tb = construct_tokens(title_b)
    if not ta or not tb:
        return False, 0.0
    if _has_opposing_pole_conflict(ta, tb):
        return False, 0.0
    inter = len(ta & tb)
    if inter == 0:
        return False, 0.0
    union = len(ta | tb)
    jaccard = inter / union
    containment = inter / min(len(ta), len(tb))
    related = jaccard >= 0.50 or containment >= 1.0 or (inter >= 2 and jaccard >= 0.34)
    return related, round(jaccard, 6)


def _extract_outcome_items(raw_items: Any, title_key: str) -> list[dict[str, Any]]:
    """Normalize a protocol or results outcome list while preserving index and verbatim text."""
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise TypeError(f'expected a list of outcomes, got {type(raw_items).__name__}')
    items: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            raise TypeError(f'outcome entry {idx} is not an object')
        title = entry.get(title_key)
        # Accept alternate key when reading external history snapshots ('measure' vs 'title').
        if title is None and title_key == 'measure':
            title = entry.get('title')
        time_frame = entry.get('timeFrame') if 'timeFrame' in entry else entry.get('time_frame')
        items.append({
            'index': idx,
            'measure_index': entry.get('_measure_index', idx),
            'title': title if isinstance(title, str) else None,
            'time_frame': time_frame if isinstance(time_frame, str) else None,
            'norm_title': normalize_outcome_text(title),
            'norm_time_frame': normalize_outcome_text(time_frame),
        })
    return items


def compare_protocol_to_results(study: dict[str, Any], results_status: str) -> dict[str, Any]:
    """Compare `protocolSection.outcomesModule.primaryOutcomes` to posted `PRIMARY` measures.

    When `results_status` is not `'posted'`, returns an explicit non-evaluated record state with
    `alignments = None` and `state_counts = None` so absence of posted results is never conflated
    with `registered_primary_missing_in_results`.
    """
    protocol = study.get('protocolSection') if isinstance(study.get('protocolSection'), dict) else {}
    om_proto = protocol.get('outcomesModule')
    if om_proto is not None and not isinstance(om_proto, dict):
        raise TypeError('protocolSection.outcomesModule is not an object')
    reg_raw = (om_proto or {}).get('primaryOutcomes')
    reg_items = _extract_outcome_items(reg_raw, 'measure')

    if results_status == 'not_retrieved':
        return {
            'state': 'not_retrieved',
            'n_registered_primary': None,
            'n_results_primary': None,
            'alignments': None,
            'state_counts': None,
            'has_discrepancy': None,
        }
    if results_status != 'posted':
        return {
            'state': 'not_applicable_no_posted_results' if results_status == 'no_posted_results' else 'parse_failed',
            'n_registered_primary': len(reg_items),
            'n_results_primary': None,
            'alignments': None,
            'state_counts': None,
            'has_discrepancy': None,
        }

    rs = study.get('resultsSection')
    if not isinstance(rs, dict):
        raise TypeError('resultsSection is not an object')
    om_res = rs.get('outcomeMeasuresModule')
    if om_res is not None and not isinstance(om_res, dict):
        raise TypeError('resultsSection.outcomeMeasuresModule is not an object')
    all_measures = (om_res or {}).get('outcomeMeasures')
    if all_measures is not None and not isinstance(all_measures, list):
        raise TypeError('resultsSection.outcomeMeasuresModule.outcomeMeasures is not a list')

    primaries_raw: list[dict[str, Any]] = []
    for m_idx, m in enumerate(all_measures or []):
        if not isinstance(m, dict):
            raise TypeError(f'outcomeMeasures[{m_idx}] is not an object')
        if m.get('type') == 'PRIMARY':
            primaries_raw.append({**m, '_measure_index': m_idx})
    res_items = _extract_outcome_items(primaries_raw, 'title')

    used_reg: set[int] = set()
    used_res: set[int] = set()
    paired: list[tuple[int | None, int | None, str, dict[str, Any]]] = []

    # Pass 1a: byte-verbatim non-empty title and timeframe match (in posted order).
    for ri, r in enumerate(reg_items):
        if not r['norm_title']:
            continue
        for pj, p in enumerate(res_items):
            if pj in used_res:
                continue
            if (r['title'] or '').strip() == (p['title'] or '').strip() and \
                    (r['time_frame'] or '').strip() == (p['time_frame'] or '').strip():
                used_reg.add(ri)
                used_res.add(pj)
                paired.append((ri, pj, 'verbatim_measure_and_timeframe_match', {'exact_case_and_whitespace': True}))
                break

    # Pass 1b: normalized whitespace/case-equivalent title and timeframe match.
    for ri, r in enumerate(reg_items):
        if ri in used_reg or not r['norm_title']:
            continue
        for pj, p in enumerate(res_items):
            if pj in used_res:
                continue
            if r['norm_title'] == p['norm_title'] and r['norm_time_frame'] == p['norm_time_frame']:
                used_reg.add(ri)
                used_res.add(pj)
                paired.append((ri, pj, 'verbatim_measure_and_timeframe_match', {'exact_case_and_whitespace': False}))
                break

    # Pass 2: identical normalized measure title, differing timeframe.
    for ri, r in enumerate(reg_items):
        if ri in used_reg or not r['norm_title']:
            continue
        for pj, p in enumerate(res_items):
            if pj in used_res:
                continue
            if r['norm_title'] == p['norm_title'] and r['norm_time_frame'] != p['norm_time_frame']:
                used_reg.add(ri)
                used_res.add(pj)
                paired.append((ri, pj, 'timeframe_differing', {}))
                break

    # Pass 3: shared clinical construct with differing measure title (deterministic best Jaccard,
    # preferring matching timeframe on ties, then earliest indices).
    candidates: list[tuple[float, int, int, int, bool]] = []
    for ri, r in enumerate(reg_items):
        if ri in used_reg:
            continue
        for pj, p in enumerate(res_items):
            if pj in used_res:
                continue
            related, jaccard = titles_share_construct(r['title'], p['title'])
            if related:
                same_tf = int(bool(r['norm_time_frame']) and r['norm_time_frame'] == p['norm_time_frame'])
                candidates.append((-jaccard, -same_tf, ri, pj, r['norm_time_frame'] != p['norm_time_frame']))
    candidates.sort()
    for neg_j, _neg_tf, ri, pj, tf_diff in candidates:
        if ri in used_reg or pj in used_res:
            continue
        used_reg.add(ri)
        used_res.add(pj)
        paired.append((ri, pj, 'measure_title_differing', {
            'construct_token_jaccard': round(-neg_j, 6),
            'timeframe_also_differing': tf_diff,
        }))

    # Pass 4: unmatched registered primaries and unlisted results primaries.
    for ri in range(len(reg_items)):
        if ri not in used_reg:
            paired.append((ri, None, 'registered_primary_missing_in_results', {}))
    for pj in range(len(res_items)):
        if pj not in used_res:
            paired.append((None, pj, 'results_primary_unlisted_in_protocol', {}))

    paired.sort(key=lambda item: (0 if item[0] is not None else 1,
                                  item[0] if item[0] is not None else 10**6,
                                  item[1] if item[1] is not None else 10**6))

    alignments: list[dict[str, Any]] = []
    for ri, pj, state, extra in paired:
        r_obj = reg_items[ri] if ri is not None else None
        p_obj = res_items[pj] if pj is not None else None
        alignments.append({
            'state': state,
            'protocol_primary_index': ri,
            'protocol_measure': r_obj['title'] if r_obj else None,
            'protocol_time_frame': r_obj['time_frame'] if r_obj else None,
            'protocol_json_path': (
                f'protocolSection.outcomesModule.primaryOutcomes[{ri}]' if ri is not None else None
            ),
            'results_primary_ordinal': pj,
            'results_measure_index': p_obj['measure_index'] if p_obj else None,
            'results_title': p_obj['title'] if p_obj else None,
            'results_time_frame': p_obj['time_frame'] if p_obj else None,
            'results_json_path': (
                f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{p_obj['measure_index']}]"
                if p_obj is not None else None
            ),
            **extra,
        })

    counts = {s: sum(1 for a in alignments if a['state'] == s) for s in PROTOCOL_TO_RESULTS_STATES}
    has_discrepancy = any(
        counts[s] > 0 for s in PROTOCOL_TO_RESULTS_STATES if s != 'verbatim_measure_and_timeframe_match'
    )
    return {
        'state': 'evaluated',
        'n_registered_primary': len(reg_items),
        'n_results_primary': len(res_items),
        'alignments': alignments,
        'state_counts': counts,
        'has_discrepancy': has_discrepancy,
    }


def _parse_history_entry(raw: dict[str, Any], line_no: int) -> tuple[str, dict[str, Any]]:
    """Extract `(nct_id, normalized_snapshot)` from one line of a historical versions archive."""
    nct = raw.get('nct_id') or raw.get('nctId')
    if nct is None and isinstance(raw.get('protocolSection'), dict):
        nct = (raw['protocolSection'].get('identificationModule') or {}).get('nctId')
    if not isinstance(nct, str) or not _NCT_RE.match(nct.strip()):
        raise HistoryArchiveError(f'line {line_no}: missing or invalid nct_id {nct!r}')
    nct = nct.strip()

    # Support flat outcomes keys, nested protocolSection.outcomesModule, or nested snapshot blocks.
    container: dict[str, Any] = raw
    snapshot_kind = raw.get('snapshot_kind') or raw.get('version_kind') or 'historical_snapshot'
    version_date = raw.get('version_date') or raw.get('submitted_date') or raw.get('snapshot_date')

    # In every shape the historical primary list must be recorded: an absent outcomesModule, an
    # absent primaryOutcomes key, or a null value is an unrecorded list, not an empty one, and reading
    # it as [] would turn every current primary outcome into `primary_added`. A version that genuinely
    # registered no primary outcome is written as `primaryOutcomes: []`, which is accepted.
    om_path: str | None = None
    if isinstance(raw.get('protocolSection'), dict):
        om_path = 'protocolSection.outcomesModule'
        om = raw['protocolSection'].get('outcomesModule')
        if om is None:
            raise HistoryArchiveError(
                f'{nct} (line {line_no}): historical snapshot has no {om_path}; '
                f'record {om_path}.primaryOutcomes ([] if the version registered none)'
            )
        if not isinstance(om, dict):
            raise HistoryArchiveError(f'{nct} (line {line_no}): {om_path} is not an object')
        prim_raw = om.get('primaryOutcomes')
        sec_raw = om.get('secondaryOutcomes')
    elif isinstance(raw.get('outcomesModule'), dict):
        om_path = 'outcomesModule'
        om = raw['outcomesModule']
        prim_raw = om.get('primaryOutcomes')
        sec_raw = om.get('secondaryOutcomes')
    else:
        for sub_key in ('initial', 'pre_completion', 'snapshot'):
            if isinstance(raw.get(sub_key), dict):
                container = raw[sub_key]
                snapshot_kind = raw.get('snapshot_kind') or sub_key
                version_date = version_date or container.get('version_date') or container.get('snapshot_date')
                break
        prim_raw = container.get('primaryOutcomes') if 'primaryOutcomes' in container else container.get('primary_outcomes')
        sec_raw = container.get('secondaryOutcomes') if 'secondaryOutcomes' in container else container.get('secondary_outcomes')
        if prim_raw is None:
            raise HistoryArchiveError(
                f'{nct} (line {line_no}): historical snapshot must supply primaryOutcomes (or protocolSection.outcomesModule)'
            )
    if om_path is not None and prim_raw is None:
        raise HistoryArchiveError(
            f'{nct} (line {line_no}): historical snapshot must supply {om_path}.primaryOutcomes '
            '([] if the version registered none)'
        )

    try:
        prim_items = _extract_outcome_items(prim_raw, 'measure')
        sec_items = _extract_outcome_items(sec_raw, 'measure') if sec_raw is not None else []
    except TypeError as exc:
        raise HistoryArchiveError(f'{nct} (line {line_no}): {exc}') from exc

    return nct, {
        'nct_id': nct,
        'snapshot_kind': str(snapshot_kind) if snapshot_kind is not None else 'historical_snapshot',
        'version_date': str(version_date) if isinstance(version_date, str) and version_date.strip() else None,
        'version_number': raw.get('version_number') if isinstance(raw.get('version_number'), int) else None,
        'primary_outcomes': prim_items,
        'secondary_outcomes': sec_items,
        'secondary_outcomes_supplied': sec_raw is not None or isinstance(raw.get('protocolSection'), dict),
    }


def load_history_archive(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load a frozen `jsonl.gz` archive of historical study-version snapshots keyed by `nct_id`.

    If `<archive>.meta.json` is present alongside `path`, verifies `sha256_uncompressed` and
    `n_records` before returning. Raises `HistoryArchiveError` on corrupt gzip, malformed JSON,
    duplicate `nct_id` rows, or metadata checksum mismatch.
    """
    if not path.is_file():
        raise HistoryArchiveError(f'{path}: historical archive file does not exist')
    try:
        raw_bytes = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as exc:
        raise HistoryArchiveError(f'{path.name}: could not decompress gzip archive ({exc})') from exc

    uncompressed_sha = hashlib.sha256(raw_bytes).hexdigest()
    compressed_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    meta_path = path.with_name(path.name + '.meta.json')
    meta_verified = False
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise HistoryArchiveError(f'{meta_path.name}: unreadable metadata JSON ({exc})') from exc
        expected_sha = meta.get('sha256_uncompressed')
        if expected_sha is not None and expected_sha != uncompressed_sha:
            raise HistoryArchiveError(
                f'{path.name}: uncompressed bytes do not match {meta_path.name} sha256_uncompressed'
            )
        meta_verified = True
    else:
        meta = {}

    snapshots: dict[str, dict[str, Any]] = {}
    # Split strictly on 0x0A (`b'\n'`), never `str.splitlines()`, because real CT.gov text with
    # `ensure_ascii=False` may contain U+2028, U+2029, U+0085, \x0b, or \x0c inside JSON strings.
    raw_lines = raw_bytes.split(b'\n')
    non_empty_lines = 0
    for line_no, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue
        non_empty_lines += 1
        try:
            line = raw_line.decode('utf-8')
            obj = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HistoryArchiveError(f'{path.name} line {line_no}: invalid JSON ({exc})') from exc
        if not isinstance(obj, dict):
            raise HistoryArchiveError(f'{path.name} line {line_no}: expected JSON object')
        nct, snap = _parse_history_entry(obj, line_no)
        if nct in snapshots:
            raise HistoryArchiveError(f'{path.name} line {line_no}: duplicate historical snapshot for {nct}')
        snapshots[nct] = snap

    if meta_verified and 'n_records' in meta and meta.get('n_records') != non_empty_lines:
        raise HistoryArchiveError(
            f"{path.name}: record count ({non_empty_lines}) does not match metadata ({meta.get('n_records')})"
        )

    archive_info = {
        'provided': True,
        'state': 'provided',
        'file': path.name,
        'sha256_compressed': compressed_sha,
        'sha256_uncompressed': uncompressed_sha,
        'meta_verified': meta_verified,
        'n_records': len(snapshots),
    }
    return snapshots, archive_info


def compare_version_history(
    study: dict[str, Any] | None,
    nct_id: str | None,
    history_snapshots: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Compare historical primary/secondary outcomes to current `protocolSection.outcomesModule`.

    When `history_snapshots is None`, cleanly reports
    `version_history_state = "history_archive_not_provided"` with `diffs = None`.
    """
    if history_snapshots is None:
        return {
            'version_history_state': 'history_archive_not_provided',
            'snapshot_kind': None,
            'historical_version_date': None,
            'n_historical_primary': None,
            'n_current_primary': None,
            'overall_change_state': None,
            'diffs': None,
            'state_counts': None,
        }

    if not nct_id or nct_id not in history_snapshots:
        return {
            'version_history_state': 'trial_not_in_history_archive',
            'snapshot_kind': None,
            'historical_version_date': None,
            'n_historical_primary': None,
            'n_current_primary': None,
            'overall_change_state': None,
            'diffs': None,
            'state_counts': None,
        }

    if not isinstance(study, dict):
        return {
            'version_history_state': 'parse_failed',
            'snapshot_kind': history_snapshots[nct_id]['snapshot_kind'],
            'historical_version_date': history_snapshots[nct_id]['version_date'],
            'n_historical_primary': len(history_snapshots[nct_id]['primary_outcomes']),
            'n_current_primary': None,
            'overall_change_state': None,
            'diffs': None,
            'state_counts': None,
        }

    protocol = study.get('protocolSection') if isinstance(study.get('protocolSection'), dict) else {}
    om_curr = protocol.get('outcomesModule')
    if om_curr is not None and not isinstance(om_curr, dict):
        raise TypeError('protocolSection.outcomesModule is not an object')
    curr_prim = _extract_outcome_items((om_curr or {}).get('primaryOutcomes'), 'measure')
    curr_sec = _extract_outcome_items((om_curr or {}).get('secondaryOutcomes'), 'measure')

    snap = history_snapshots[nct_id]
    hist_prim: list[dict[str, Any]] = snap['primary_outcomes']
    hist_sec: list[dict[str, Any]] = snap['secondary_outcomes']

    used_hp: set[int] = set()
    used_cp: set[int] = set()
    used_hs: set[int] = set()
    used_cs: set[int] = set()

    # Reserve secondary outcomes that remained secondary in both versions so an unchanged secondary
    # is never double-claimed as a demotion/promotion target when duplicate titles exist.
    for hsi, hs in enumerate(hist_sec):
        if not hs['norm_title']:
            continue
        for csi, cs in enumerate(curr_sec):
            if csi in used_cs:
                continue
            if hs['norm_title'] == cs['norm_title'] and hs['norm_time_frame'] == cs['norm_time_frame']:
                used_hs.add(hsi)
                used_cs.add(csi)
                break

    diffs: list[dict[str, Any]] = []

    # Step 1: unchanged (same normalized measure title and timeframe).
    for hi, hp in enumerate(hist_prim):
        if not hp['norm_title']:
            continue
        for cj, cp in enumerate(curr_prim):
            if cj in used_cp:
                continue
            if hp['norm_title'] == cp['norm_title'] and hp['norm_time_frame'] == cp['norm_time_frame']:
                used_hp.add(hi)
                used_cp.add(cj)
                diffs.append({
                    'state': 'unchanged',
                    'historical_primary_index': hi,
                    'historical_measure': hp['title'],
                    'historical_time_frame': hp['time_frame'],
                    'current_primary_index': cj,
                    'current_measure': cp['title'],
                    'current_time_frame': cp['time_frame'],
                })
                break

    # Step 2: timeframe_changed (same normalized measure title, differing timeframe).
    for hi, hp in enumerate(hist_prim):
        if hi in used_hp or not hp['norm_title']:
            continue
        for cj, cp in enumerate(curr_prim):
            if cj in used_cp:
                continue
            if hp['norm_title'] == cp['norm_title'] and hp['norm_time_frame'] != cp['norm_time_frame']:
                used_hp.add(hi)
                used_cp.add(cj)
                diffs.append({
                    'state': 'timeframe_changed',
                    'historical_primary_index': hi,
                    'historical_measure': hp['title'],
                    'historical_time_frame': hp['time_frame'],
                    'current_primary_index': cj,
                    'current_measure': cp['title'],
                    'current_time_frame': cp['time_frame'],
                })
                break

    # Step 3: primary_demoted_to_secondary (historical primary appears in current secondaryOutcomes).
    for hi, hp in enumerate(hist_prim):
        if hi in used_hp or not hp['norm_title']:
            continue
        matched_cs: int | None = None
        for csi, cs in enumerate(curr_sec):
            if csi in used_cs:
                continue
            if hp['norm_title'] == cs['norm_title']:
                matched_cs = csi
                break
        if matched_cs is None:
            for csi, cs in enumerate(curr_sec):
                if csi in used_cs:
                    continue
                related, _ = titles_share_construct(hp['title'], cs['title'])
                if related and hp['norm_time_frame'] == cs['norm_time_frame']:
                    matched_cs = csi
                    break
        if matched_cs is not None:
            used_hp.add(hi)
            used_cs.add(matched_cs)
            cs_obj = curr_sec[matched_cs]
            diffs.append({
                'state': 'primary_demoted_to_secondary',
                'historical_primary_index': hi,
                'historical_measure': hp['title'],
                'historical_time_frame': hp['time_frame'],
                'current_secondary_index': matched_cs,
                'current_measure': cs_obj['title'],
                'current_time_frame': cs_obj['time_frame'],
            })

    # Step 4: secondary_promoted_to_primary (historical secondary appears in current primaryOutcomes).
    for cj, cp in enumerate(curr_prim):
        if cj in used_cp or not cp['norm_title']:
            continue
        matched_hs: int | None = None
        for hsi, hs in enumerate(hist_sec):
            if hsi in used_hs:
                continue
            if cp['norm_title'] == hs['norm_title']:
                matched_hs = hsi
                break
        if matched_hs is None:
            for hsi, hs in enumerate(hist_sec):
                if hsi in used_hs:
                    continue
                related, _ = titles_share_construct(cp['title'], hs['title'])
                if related and cp['norm_time_frame'] == hs['norm_time_frame']:
                    matched_hs = hsi
                    break
        if matched_hs is not None:
            used_cp.add(cj)
            used_hs.add(matched_hs)
            hs_obj = hist_sec[matched_hs]
            diffs.append({
                'state': 'secondary_promoted_to_primary',
                'historical_secondary_index': matched_hs,
                'historical_measure': hs_obj['title'],
                'historical_time_frame': hs_obj['time_frame'],
                'current_primary_index': cj,
                'current_measure': cp['title'],
                'current_time_frame': cp['time_frame'],
            })

    # Step 5: measure_reworded (remaining historical primary vs remaining current primary sharing
    # substantive clinical construct tokens).
    reword_candidates: list[tuple[float, int, int, int, bool]] = []
    for hi, hp in enumerate(hist_prim):
        if hi in used_hp:
            continue
        for cj, cp in enumerate(curr_prim):
            if cj in used_cp:
                continue
            related, jaccard = titles_share_construct(hp['title'], cp['title'])
            if related:
                same_tf = int(bool(hp['norm_time_frame']) and hp['norm_time_frame'] == cp['norm_time_frame'])
                reword_candidates.append((-jaccard, -same_tf, hi, cj, hp['norm_time_frame'] != cp['norm_time_frame']))
    reword_candidates.sort()
    for neg_j, _neg_tf, hi, cj, tf_changed in reword_candidates:
        if hi in used_hp or cj in used_cp:
            continue
        used_hp.add(hi)
        used_cp.add(cj)
        hp_obj = hist_prim[hi]
        cp_obj = curr_prim[cj]
        diffs.append({
            'state': 'measure_reworded',
            'historical_primary_index': hi,
            'historical_measure': hp_obj['title'],
            'historical_time_frame': hp_obj['time_frame'],
            'current_primary_index': cj,
            'current_measure': cp_obj['title'],
            'current_time_frame': cp_obj['time_frame'],
            'construct_token_jaccard': round(-neg_j, 6),
            'timeframe_also_changed': tf_changed,
        })

    # Step 6: remaining unmatched historical primaries were dropped; remaining current were added.
    for hi, hp in enumerate(hist_prim):
        if hi not in used_hp:
            diffs.append({
                'state': 'primary_dropped',
                'historical_primary_index': hi,
                'historical_measure': hp['title'],
                'historical_time_frame': hp['time_frame'],
                'current_primary_index': None,
                'current_measure': None,
                'current_time_frame': None,
            })
    for cj, cp in enumerate(curr_prim):
        if cj not in used_cp:
            diffs.append({
                'state': 'primary_added',
                'historical_primary_index': None,
                'historical_measure': None,
                'historical_time_frame': None,
                'current_primary_index': cj,
                'current_measure': cp['title'],
                'current_time_frame': cp['time_frame'],
            })

    diffs.sort(key=lambda d: (
        0 if d.get('historical_primary_index') is not None else 1,
        d.get('historical_primary_index') if d.get('historical_primary_index') is not None else 10**6,
        d.get('current_primary_index') if d.get('current_primary_index') is not None else 10**6,
    ))

    counts = {s: sum(1 for d in diffs if d['state'] == s) for s in HISTORY_DIFF_STATES}
    has_change = any(counts[s] > 0 for s in HISTORY_DIFF_STATES if s != 'unchanged')
    return {
        'version_history_state': 'evaluated',
        'snapshot_kind': snap['snapshot_kind'],
        'historical_version_date': snap['version_date'],
        'n_historical_primary': len(hist_prim),
        'n_current_primary': len(curr_prim),
        'overall_change_state': 'changed' if has_change else 'unchanged',
        'diffs': diffs,
        'state_counts': counts,
    }


def inspect_outcome_history(
    study: Any,
    results_status: str,
    history_snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full outcome-fidelity record combining protocol-vs-results and optional version-history diffs."""
    nct: str | None = None
    if isinstance(study, dict):
        ident = ((study.get('protocolSection') or {}).get('identificationModule') or {})
        if isinstance(ident, dict) and isinstance(ident.get('nctId'), str):
            nct = ident['nctId']

    failures: list[dict[str, str]] = []
    if not isinstance(study, dict):
        proto_res: dict[str, Any] = {
            'state': 'not_retrieved' if results_status == 'not_retrieved' else 'parse_failed',
            'n_registered_primary': None,
            'n_results_primary': None,
            'alignments': None,
            'state_counts': None,
            'has_discrepancy': None,
        }
    else:
        try:
            proto_res = compare_protocol_to_results(study, results_status)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            failures.append({'section': 'protocol_to_results', 'reason': f'{type(exc).__name__}: {exc}'})
            proto_res = {
                'state': 'parse_failed',
                'n_registered_primary': None,
                'n_results_primary': None,
                'alignments': None,
                'state_counts': None,
                'has_discrepancy': None,
            }

    try:
        ver_hist = compare_version_history(study if isinstance(study, dict) else None, nct, history_snapshots)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        failures.append({'section': 'version_history', 'reason': f'{type(exc).__name__}: {exc}'})
        ver_hist = {
            'version_history_state': 'parse_failed',
            'snapshot_kind': None,
            'historical_version_date': None,
            'n_historical_primary': None,
            'n_current_primary': None,
            'overall_change_state': None,
            'diffs': None,
            'state_counts': None,
        }

    return {
        'method_version': METHOD_VERSION,
        'version_history_state': ver_hist['version_history_state'],
        'protocol_to_results': proto_res,
        'version_history': ver_hist,
        'parse_failures': failures,
    }
