"""Date-precision-aware disclosure milestones, interval bounds, 1-year window states,
cross-source discordance states, and read-only `sponsors/` rollups.

Key methodological invariants:
  1. Partial dates (`YYYY`, `YYYY-MM`) NEVER fabricate `-01` day defaults. Every date is
     represented by its exact calendar bounds `[earliest, latest]`:
       * Both endpoints `YYYY-MM-DD`: `state = "exact"`, `exact_days = int`
       * Partial dates whose calendar spans do not overlap: `state = "bounded_partial"`,
         `bounds_days = [min_days, max_days]`, `exact_days = None`
       * Partial dates whose calendar spans overlap: `state = "indeterminate_at_recorded_precision"`,
         `bounds_days = None`, `exact_days = None`
       * Missing endpoint(s): `state = "endpoint_missing"`, `bounds_days = None`, `exact_days = None`
  2. The 365-day window classification (`WINDOW_STATES_365D`) describes registry timestamps only
     and NEVER claims statutory or regulatory FDAAA 801 / 42 CFR Part 11 non-compliance.
  3. Sponsor attribution delegates read-only to `sponsors/` (`company_aliases.csv`), keeping
     `attributed`, `reviewed_excluded`, and `unreviewed` literals strictly separate with zero
     substring matching, and emitting both `current_owner` and `as_registered` views.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from disclosure import coverage as C
from disclosure import evidence as E

try:
    from sponsors import adapter as _SA  # type: ignore[import-not-found]
    from sponsors import audit_states as _SAS  # type: ignore[import-not-found]
    from sponsors import sponsor_roles as _SR  # type: ignore[import-not-found]
except ModuleNotFoundError as _exc:
    if _exc.name != 'pandas':
        raise
    _SA = None
    _SAS = None
    _SR = None

AACT_LOADER_COMMIT = 'b8c16d3395ba7e548d852bccf3b47a7ff22af5f5'
AACT_AGENCY_CLASSES = frozenset(
    {'INDUSTRY', 'OTHER', 'NIH', 'OTHER_GOV', 'FED', 'NETWORK', 'UNKNOWN', 'INDIV', 'AMBIG'}
)
VALID_RULE_TYPES = frozenset({'exact_literal', 'exact_normalized', 'subsidiary_of'})
VALID_RULE_STATUS = frozenset({'attribute', 'exclude'})
VALID_RULE_SHARED = frozenset({'yes', 'no'})

INTERVAL_STATES = (
    'exact',
    'bounded_partial',
    'indeterminate_at_recorded_precision',
    'endpoint_missing',
)

WINDOW_STATES_365D = (
    'POSTED_WITHIN_365D',
    'POSTED_AFTER_365D',
    'PENDING_SUBMITTED_UNPOSTED',
    'UNREPORTED_PAST_365D',
    'UNREPORTED_WITHIN_365D',
    'DATE_PRECISION_INDETERMINATE',
    'COMPLETION_DATE_MISSING_OR_FUTURE',
)

DISCORDANCE_STATES = (
    'published_results_while_ctgov_unreported',
    'fda_cited_while_ctgov_unreported',
    'ctgov_posted_no_linked_results_publication',
    'concordant_multi_source_disclosed',
    'unreported_across_completed_routes',
    'coverage_incomplete_or_awaiting_review',
)

SPONSOR_REVIEW_STATES = ('attributed', 'reviewed_excluded', 'unreviewed')

WINDOW_365D_NOTE = (
    'Descriptive calendar-day comparison against protocolSection.statusModule.primaryCompletionDateStruct '
    '(365-day window). Does NOT establish FDAAA 801 / 42 CFR Part 11 applicable clinical trial status, '
    'certification/extension eligibility, or legal compliance.'
)


@dataclass(frozen=True)
class PartialDate:
    """Exact calendar-day span `[earliest, latest]` allowed by a `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` string."""

    raw: str
    earliest: date
    latest: date
    precision: str


def parse_partial_date(raw: str | None) -> PartialDate | None:
    """Parse `'YYYY'`, `'YYYY-MM'`, or `'YYYY-MM-DD'` into its exact `[earliest, latest]` span.

    Never invents `-01` day defaults for month- or year-precision strings.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None

    m_day = E.ISO_DAY_RE.match(s)
    if m_day:
        y, mo, d = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
        try:
            dt = date(y, mo, d)
            return PartialDate(raw=s, earliest=dt, latest=dt, precision='day')
        except ValueError:
            return None

    m_month = E.ISO_MONTH_RE.match(s)
    if m_month:
        y, mo = int(m_month.group(1)), int(m_month.group(2))
        if not (1800 <= y <= 2200 and 1 <= mo <= 12):
            return None
        last_day = calendar.monthrange(y, mo)[1]
        return PartialDate(
            raw=f'{y:04d}-{mo:02d}',
            earliest=date(y, mo, 1),
            latest=date(y, mo, last_day),
            precision='month',
        )

    m_year = E.ISO_YEAR_RE.match(s)
    if m_year:
        y = int(m_year.group(1))
        if not (1800 <= y <= 2200):
            return None
        return PartialDate(
            raw=f'{y:04d}',
            earliest=date(y, 1, 1),
            latest=date(y, 12, 31),
            precision='year',
        )

    return None


def milestone_from_raw(
    raw: str | None,
    *,
    date_type: str | None = None,
    link_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a standardized milestone dict from a raw date string."""
    parsed = parse_partial_date(raw)
    out: dict[str, Any] = {
        'date': parsed.raw if parsed else None,
        'precision': parsed.precision if parsed else 'missing',
        'earliest_bound': parsed.earliest.isoformat() if parsed else None,
        'latest_bound': parsed.latest.isoformat() if parsed else None,
    }
    if date_type is not None:
        out['date_type'] = date_type
    if link_ids is not None:
        out['link_ids'] = link_ids
    if raw is not None and raw.strip() and parsed is None:
        out['unparseable_raw'] = raw
    return out


def earliest_link_milestone(qualifying_links: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the exact tight earliest-date milestone across qualifying evidence links.

    For dates `{[L_i, U_i]}`, `min_i d_i` lies in `[min_i L_i, min_i U_i]`: it cannot precede
    every `L_i` and cannot exceed any `U_i`.
    """
    dated: list[tuple[PartialDate, str]] = []
    undated_ids: list[str] = []
    for lnk in qualifying_links:
        p = parse_partial_date(lnk.get('publication_or_action_date'))
        if p is not None:
            dated.append((p, lnk['link_id']))
        else:
            undated_ids.append(lnk['link_id'])

    if not dated:
        return {
            'date': None,
            'precision': 'missing',
            'earliest_bound': None,
            'latest_bound': None,
            'link_ids': sorted(undated_ids),
            'has_undated_qualifying_link': bool(undated_ids),
        }

    min_earliest = min(p.earliest for p, _ in dated)
    min_latest = min(p.latest for p, _ in dated)
    exact_matches = [(p, lid) for p, lid in dated if p.earliest == min_earliest and p.latest == min_latest]
    contributing = sorted({lid for p, lid in dated if p.earliest <= min_latest})

    if exact_matches:
        chosen = sorted(exact_matches, key=lambda item: (item[0].raw, item[1]))[0][0]
        return {
            'date': chosen.raw,
            'precision': chosen.precision,
            'earliest_bound': min_earliest.isoformat(),
            'latest_bound': min_latest.isoformat(),
            'link_ids': contributing,
            'has_undated_qualifying_link': bool(undated_ids),
        }

    # Overlapping partial and exact dates narrow `[min_earliest, min_latest]` tighter than any single string.
    return {
        'date': f'{min_earliest.isoformat()}..{min_latest.isoformat()}',
        'precision': 'bounded_composite',
        'earliest_bound': min_earliest.isoformat(),
        'latest_bound': min_latest.isoformat(),
        'link_ids': contributing,
        'has_undated_qualifying_link': bool(undated_ids),
    }


def interval_from_milestones(start_ms: dict[str, Any], end_ms: dict[str, Any]) -> dict[str, Any]:
    """Compute date-precision-aware interval `end - start` in days without fabricating `-01`."""
    s_lo_raw, s_hi_raw = start_ms.get('earliest_bound'), start_ms.get('latest_bound')
    e_lo_raw, e_hi_raw = end_ms.get('earliest_bound'), end_ms.get('latest_bound')
    missing: list[str] = []
    if not s_lo_raw or not s_hi_raw:
        missing.append('start')
    if not e_lo_raw or not e_hi_raw:
        missing.append('end')

    base: dict[str, Any] = {
        'start_date': start_ms.get('date'),
        'start_precision': start_ms.get('precision', 'missing'),
        'end_date': end_ms.get('date'),
        'end_precision': end_ms.get('precision', 'missing'),
    }
    if missing:
        return {
            **base,
            'state': 'endpoint_missing',
            'exact_days': None,
            'min_days': None,
            'max_days': None,
            'bounds_days': None,
            'missing_endpoints': missing,
        }

    s_lo = date.fromisoformat(s_lo_raw)
    s_hi = date.fromisoformat(s_hi_raw)
    e_lo = date.fromisoformat(e_lo_raw)
    e_hi = date.fromisoformat(e_hi_raw)

    if start_ms.get('precision') == 'day' and end_ms.get('precision') == 'day' and s_lo == s_hi and e_lo == e_hi:
        days = (e_lo - s_lo).days
        return {
            **base,
            'state': 'exact',
            'exact_days': days,
            'min_days': days,
            'max_days': days,
            'bounds_days': [days, days],
        }

    min_days = (e_lo - s_hi).days
    max_days = (e_hi - s_lo).days
    # Ranges `[s_lo, s_hi]` and `[e_lo, e_hi]` overlap iff `s_lo <= e_hi` and `e_lo <= s_hi`.
    overlaps = s_lo <= e_hi and e_lo <= s_hi
    if overlaps:
        return {
            **base,
            'state': 'indeterminate_at_recorded_precision',
            'exact_days': None,
            'min_days': None,
            'max_days': None,
            'bounds_days': None,
            'overlap_span_days': [min_days, max_days],
        }
    return {
        **base,
        'state': 'bounded_partial',
        'exact_days': None,
        'min_days': min_days,
        'max_days': max_days,
        'bounds_days': [min_days, max_days],
    }


def interval_between(start_raw: str | None, end_raw: str | None) -> dict[str, Any]:
    """Convenience wrapper computing `interval_from_milestones` directly from two date strings."""
    return interval_from_milestones(milestone_from_raw(start_raw), milestone_from_raw(end_raw))


def extract_submission_infos(status_mod: dict[str, Any]) -> dict[str, Any]:
    """Extract `submissionInfos` QC cycle history, preserving absence as `None` (never `0`)."""
    if 'submissionInfos' not in status_mod or status_mod.get('submissionInfos') is None:
        return {
            'present': False,
            'cycle_count': None,
            'n_with_unrelease_date': None,
            'n_with_reset_date': None,
            'absent_reason': 'submissionInfos_absent_in_statusModule',
            'cycles': None,
        }
    raw_list = status_mod.get('submissionInfos')
    if not isinstance(raw_list, list):
        return {
            'present': False,
            'cycle_count': None,
            'n_with_unrelease_date': None,
            'n_with_reset_date': None,
            'absent_reason': 'submissionInfos_malformed_not_a_list',
            'cycles': None,
        }
    cycles: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        mcp_n = item.get('mcpReleaseN')
        cycles.append({
            'release_date': item.get('releaseDate') if isinstance(item.get('releaseDate'), str) else None,
            'unrelease_date': item.get('unreleaseDate') if isinstance(item.get('unreleaseDate'), str) else None,
            'reset_date': item.get('resetDate') if isinstance(item.get('resetDate'), str) else None,
            'mcp_release_n': mcp_n if isinstance(mcp_n, int) and not isinstance(mcp_n, bool) else None,
        })
    return {
        'present': True,
        'cycle_count': len(cycles),
        'n_with_unrelease_date': sum(1 for c in cycles if c['unrelease_date'] is not None),
        'n_with_reset_date': sum(1 for c in cycles if c['reset_date'] is not None),
        'absent_reason': None,
        'cycles': cycles,
    }


def classify_window_365d(study: dict[str, Any] | None, as_of: date) -> dict[str, Any]:
    """Classify descriptive 365-day registry disclosure window state as of `as_of`."""
    if not isinstance(study, dict):
        return {
            'state': 'COMPLETION_DATE_MISSING_OR_FUTURE',
            'as_of': as_of.isoformat(),
            'reason': 'study record was not retrieved from ClinicalTrials.gov',
            'note': WINDOW_365D_NOTE,
        }

    ps = study.get('protocolSection') or {}
    sm = ps.get('statusModule') or {}
    pcd_struct = sm.get('primaryCompletionDateStruct') or {}
    pcd_raw = pcd_struct.get('date') if isinstance(pcd_struct.get('date'), str) else None
    pcd_type = pcd_struct.get('type') if isinstance(pcd_struct.get('type'), str) else None
    pcd = parse_partial_date(pcd_raw)

    if pcd is None:
        return {
            'state': 'COMPLETION_DATE_MISSING_OR_FUTURE',
            'as_of': as_of.isoformat(),
            'reason': 'primaryCompletionDateStruct.date is absent or unparseable',
            'note': WINDOW_365D_NOTE,
        }
    if pcd_type in ('ESTIMATED', 'ANTICIPATED'):
        return {
            'state': 'COMPLETION_DATE_MISSING_OR_FUTURE',
            'as_of': as_of.isoformat(),
            'reason': f'primaryCompletionDateStruct.type is {pcd_type} (not confirmed ACTUAL completion)',
            'note': WINDOW_365D_NOTE,
        }
    if pcd.earliest > as_of:
        return {
            'state': 'COMPLETION_DATE_MISSING_OR_FUTURE',
            'as_of': as_of.isoformat(),
            'reason': f'primaryCompletionDate ({pcd.raw}) lies after reference date {as_of.isoformat()}',
            'note': WINDOW_365D_NOTE,
        }

    post_raw = (sm.get('resultsFirstPostDateStruct') or {}).get('date')
    post = parse_partial_date(post_raw if isinstance(post_raw, str) else None)
    rs = study.get('resultsSection')
    has_results_section = study.get('hasResults') is True and isinstance(rs, dict) and bool(rs)

    if has_results_section or post is not None:
        if post is None:
            return {
                'state': 'DATE_PRECISION_INDETERMINATE',
                'as_of': as_of.isoformat(),
                'reason': 'resultsSection is posted, but resultsFirstPostDateStruct.date is missing',
                'note': WINDOW_365D_NOTE,
            }
        if post.earliest <= as_of < post.latest:
            return {
                'state': 'DATE_PRECISION_INDETERMINATE',
                'as_of': as_of.isoformat(),
                'reason': f'partial resultsFirstPostDate ({post.raw}) overlaps reference date {as_of.isoformat()}',
                'note': WINDOW_365D_NOTE,
            }
        if post.latest <= as_of:
            min_lag = (post.earliest - pcd.latest).days
            max_lag = (post.latest - pcd.earliest).days
            if max_lag <= 365:
                return {
                    'state': 'POSTED_WITHIN_365D',
                    'as_of': as_of.isoformat(),
                    'reason': f'results first posted ({post.raw}) at most {max_lag} days after primary completion ({pcd.raw})',
                    'note': WINDOW_365D_NOTE,
                }
            if min_lag > 365:
                return {
                    'state': 'POSTED_AFTER_365D',
                    'as_of': as_of.isoformat(),
                    'reason': f'results first posted ({post.raw}) at least {min_lag} days after primary completion ({pcd.raw})',
                    'note': WINDOW_365D_NOTE,
                }
            return {
                'state': 'DATE_PRECISION_INDETERMINATE',
                'as_of': as_of.isoformat(),
                'reason': (
                    f'partial date span [{min_lag}, {max_lag}] days between primary completion '
                    f'({pcd.raw}) and results posting ({post.raw}) straddles 365 days'
                ),
                'note': WINDOW_365D_NOTE,
            }

    sub_raw = sm.get('resultsFirstSubmitDate')
    sub = parse_partial_date(sub_raw if isinstance(sub_raw, str) else None)
    if sub is not None:
        if sub.latest <= as_of:
            return {
                'state': 'PENDING_SUBMITTED_UNPOSTED',
                'as_of': as_of.isoformat(),
                'reason': f'resultsFirstSubmitDate ({sub.raw}) recorded on or before {as_of.isoformat()} with no posted results yet',
                'note': WINDOW_365D_NOTE,
            }
        if sub.earliest <= as_of < sub.latest:
            return {
                'state': 'DATE_PRECISION_INDETERMINATE',
                'as_of': as_of.isoformat(),
                'reason': f'partial resultsFirstSubmitDate ({sub.raw}) overlaps {as_of.isoformat()}',
                'note': WINDOW_365D_NOTE,
            }

    if pcd.latest > as_of:
        return {
            'state': 'DATE_PRECISION_INDETERMINATE',
            'as_of': as_of.isoformat(),
            'reason': f'partial primaryCompletionDate ({pcd.raw}) overlaps reference date {as_of.isoformat()}',
            'note': WINDOW_365D_NOTE,
        }

    min_elapsed = (as_of - pcd.latest).days
    max_elapsed = (as_of - pcd.earliest).days
    if min_elapsed > 365:
        return {
            'state': 'UNREPORTED_PAST_365D',
            'as_of': as_of.isoformat(),
            'reason': (
                f'no posted or submitted results on CT.gov as of {as_of.isoformat()}, '
                f'at least {min_elapsed} days after primary completion ({pcd.raw})'
            ),
            'note': WINDOW_365D_NOTE,
        }
    if max_elapsed <= 365:
        return {
            'state': 'UNREPORTED_WITHIN_365D',
            'as_of': as_of.isoformat(),
            'reason': (
                f'no posted or submitted results on CT.gov as of {as_of.isoformat()}, '
                f'at most {max_elapsed} days since primary completion ({pcd.raw})'
            ),
            'note': WINDOW_365D_NOTE,
        }
    return {
        'state': 'DATE_PRECISION_INDETERMINATE',
        'as_of': as_of.isoformat(),
        'reason': (
            f'elapsed span [{min_elapsed}, {max_elapsed}] days from partial primaryCompletionDate '
            f'({pcd.raw}) to {as_of.isoformat()} straddles 365 days'
        ),
        'note': WINDOW_365D_NOTE,
    }


def _qualifying_results_publications(trial_links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Links that establish a linked results publication (human-confirmed or resolved CT.gov RESULT)."""
    out: list[dict[str, Any]] = []
    for lnk in trial_links:
        if lnk['source_route'] not in ('registry_references', 'literature_search', 'documentary_statements'):
            continue
        rs = lnk['review_status']
        if rs == 'confirmed_same_trial_results':
            out.append(lnk)
            continue
        if rs != 'unreviewed':
            continue
        if (
            lnk['source_route'] == 'registry_references'
            and lnk['citation_type'] == 'RESULT'
            and lnk['automated_validation'] == 'identifier_confirmed'
        ):
            out.append(lnk)
    return out


def _unsettled_publication_candidates(trial_links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live literature/reference/statement links whose trial-results status is not yet settled."""
    qualifying_ids = {lnk['link_id'] for lnk in _qualifying_results_publications(trial_links)}
    out: list[dict[str, Any]] = []
    for lnk in trial_links:
        if lnk['link_id'] in qualifying_ids:
            continue
        if lnk['source_route'] not in ('registry_references', 'literature_search', 'documentary_statements'):
            continue
        rs = lnk['review_status']
        av = lnk['automated_validation']
        if rs in ('rejected_false_positive', 'confirmed_background_only'):
            continue
        if av in ('identifier_not_resolved', 'rejected_by_rule') and rs == 'unreviewed':
            continue
        if lnk['source_route'] == 'registry_references' and lnk['citation_type'] == 'BACKGROUND' and rs == 'unreviewed':
            continue
        out.append(lnk)
    return out


def _qualifying_fda_links(trial_links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Links that establish an FDA citation/action linked to the trial."""
    out: list[dict[str, Any]] = []
    for lnk in trial_links:
        if lnk['source_route'] != 'openfda_drugsfda':
            continue
        rs = lnk['review_status']
        if rs == 'confirmed_same_trial_results':
            out.append(lnk)
        elif rs == 'unreviewed' and lnk['automated_validation'] == 'identifier_confirmed':
            out.append(lnk)
    return out


def _unsettled_fda_candidates(trial_links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    qualifying_ids = {lnk['link_id'] for lnk in _qualifying_fda_links(trial_links)}
    out: list[dict[str, Any]] = []
    for lnk in trial_links:
        if lnk['link_id'] in qualifying_ids or lnk['source_route'] != 'openfda_drugsfda':
            continue
        rs = lnk['review_status']
        av = lnk['automated_validation']
        if rs in ('rejected_false_positive', 'confirmed_background_only'):
            continue
        if av == 'rejected_by_rule' and rs == 'unreviewed':
            continue
        out.append(lnk)
    return out


def classify_discordance(
    trial_links: list[dict[str, Any]],
    trial_cov: dict[str, Any],
) -> dict[str, Any]:
    """Classify cross-source disclosure concordance/discordance across the 5 routes."""
    cells = {c['route']: c['state'] for c in trial_cov['routes']}
    ctgov_state = cells['ctgov_results']
    ctgov_posted = ctgov_state == 'verified_link'
    ctgov_confirmed_unreported = ctgov_state in ('no_candidate_in_completed_scope', 'available_document_insufficient')

    res_pubs = _qualifying_results_publications(trial_links)
    unsettled_pubs = _unsettled_publication_candidates(trial_links)
    fda_links = _qualifying_fda_links(trial_links)
    unsettled_fda = _unsettled_fda_candidates(trial_links)

    has_res_pub = bool(res_pubs)
    has_fda = bool(fda_links)

    flags = {
        'ctgov_results_posted': ctgov_posted,
        'has_linked_results_publication': has_res_pub,
        'has_fda_citation': has_fda,
        'n_unsettled_publication_candidates': len(unsettled_pubs),
        'n_unsettled_fda_candidates': len(unsettled_fda),
    }

    if ctgov_posted:
        if has_res_pub or has_fda:
            return {
                'state': 'concordant_multi_source_disclosed',
                'reason': 'CT.gov results posted and at least one linked external results publication or FDA citation confirmed',
                **flags,
            }
        pub_routes_completed = (
            cells['registry_references'] in ('no_candidate_in_completed_scope', 'available_document_insufficient')
            and cells['literature_search'] in ('no_candidate_in_completed_scope', 'available_document_insufficient')
            and not unsettled_pubs
        )
        if pub_routes_completed:
            return {
                'state': 'ctgov_posted_no_linked_results_publication',
                'reason': 'CT.gov results posted; registry_references and literature_search completed with zero linked results publications',
                **flags,
            }
        return {
            'state': 'coverage_incomplete_or_awaiting_review',
            'reason': 'CT.gov results posted, but literature routes were not completed or candidate publications await review',
            **flags,
        }

    if not ctgov_confirmed_unreported:
        return {
            'state': 'coverage_incomplete_or_awaiting_review',
            'reason': f'CT.gov results route is {ctgov_state}, so unreported status is not established',
            **flags,
        }

    if has_res_pub:
        return {
            'state': 'published_results_while_ctgov_unreported',
            'reason': 'CT.gov has no posted results while at least one linked results publication is confirmed',
            **flags,
        }
    if has_fda:
        return {
            'state': 'fda_cited_while_ctgov_unreported',
            'reason': 'CT.gov has no posted results while at least one FDA Drugs@FDA citation/link is confirmed',
            **flags,
        }

    all_search_completed = all(
        cells[r] in ('no_candidate_in_completed_scope', 'available_document_insufficient')
        for r in C.SEARCH_ROUTES
    )
    if all_search_completed and not unsettled_pubs and not unsettled_fda and cells['documentary_statements'] != 'candidate_awaiting_review':
        return {
            'state': 'unreported_across_completed_routes',
            'reason': 'all 4 search routes completed with no posted CT.gov results, no results publication, and no FDA link',
            **flags,
        }

    return {
        'state': 'coverage_incomplete_or_awaiting_review',
        'reason': 'CT.gov unreported, with one or more external routes unsearched/incomplete or candidates awaiting review',
        **flags,
    }


def build_trial_timeline(
    nct_id: str,
    study: dict[str, Any] | None,
    trial_links: list[dict[str, Any]],
    trial_cov: dict[str, Any],
    sponsor_info: dict[str, Any],
    as_of: date,
) -> dict[str, Any]:
    """Assemble the complete milestone, interval, QC-lag, 1-year window, and discordance record."""
    ps = (study or {}).get('protocolSection') or {}
    sm = ps.get('statusModule') or {}

    pcd_struct = sm.get('primaryCompletionDateStruct') or {}
    cd_struct = sm.get('completionDateStruct') or {}
    post_struct = sm.get('resultsFirstPostDateStruct') or {}

    m_pcd = milestone_from_raw(
        pcd_struct.get('date') if isinstance(pcd_struct.get('date'), str) else None,
        date_type=pcd_struct.get('type') if isinstance(pcd_struct.get('type'), str) else None,
    )
    m_cd = milestone_from_raw(
        cd_struct.get('date') if isinstance(cd_struct.get('date'), str) else None,
        date_type=cd_struct.get('type') if isinstance(cd_struct.get('type'), str) else None,
    )
    m_sub = milestone_from_raw(
        sm.get('resultsFirstSubmitDate') if isinstance(sm.get('resultsFirstSubmitDate'), str) else None
    )
    m_sub_qc = milestone_from_raw(
        sm.get('resultsFirstSubmitQcDate') if isinstance(sm.get('resultsFirstSubmitQcDate'), str) else None
    )
    m_post = milestone_from_raw(
        post_struct.get('date') if isinstance(post_struct.get('date'), str) else None,
        date_type=post_struct.get('type') if isinstance(post_struct.get('type'), str) else None,
    )
    m_pub = earliest_link_milestone(_qualifying_results_publications(trial_links))
    m_fda = earliest_link_milestone(_qualifying_fda_links(trial_links))

    qc_lag = interval_from_milestones(m_sub, m_post)
    intervals = {
        'primary_completion_to_results_first_submitted': interval_from_milestones(m_pcd, m_sub),
        'results_first_submitted_to_results_first_posted': qc_lag,
        'results_first_posted_to_earliest_results_publication': interval_from_milestones(m_post, m_pub),
        'earliest_results_publication_to_earliest_fda_action': interval_from_milestones(m_pub, m_fda),
        'primary_completion_to_results_first_posted': interval_from_milestones(m_pcd, m_post),
        'primary_completion_to_earliest_results_publication': interval_from_milestones(m_pcd, m_pub),
        'primary_completion_to_earliest_fda_action': interval_from_milestones(m_pcd, m_fda),
    }

    return {
        'nct_id': nct_id,
        'as_of': as_of.isoformat(),
        'milestones': {
            'primary_completion': m_pcd,
            'completion': m_cd,
            'results_first_submitted': m_sub,
            'results_first_submit_qc': m_sub_qc,
            'results_first_posted': m_post,
            'earliest_results_publication': m_pub,
            'earliest_fda_action': m_fda,
        },
        'intervals': intervals,
        'qc_lag': qc_lag,
        'submission_infos': extract_submission_infos(sm),
        'window_365d': classify_window_365d(study, as_of),
        'discordance': classify_discordance(trial_links, trial_cov),
        'sponsor': sponsor_info,
    }


# ---------------------------------------------------------------- read-only sponsor attribution

def _stored_sponsor_record(nct_id: str, study: dict[str, Any]) -> dict[str, Any]:
    ps = study.get('protocolSection') or {}
    sc = ps.get('sponsorCollaboratorsModule') or {}
    lead = sc.get('leadSponsor') or {}
    lead_name = lead.get('name')
    lead_class = lead.get('class')
    collabs: list[dict[str, str]] = []
    for c in sc.get('collaborators') or []:
        if isinstance(c, dict):
            collabs.append({
                'name': str(c.get('name') or ''),
                'class': str(c.get('class') or ''),
            })
    return {
        'nct_id': nct_id,
        'sponsor_class': str(lead_class) if lead_class is not None else 'Unknown',
        'lead_sponsor_name': str(lead_name) if lead_name is not None else '',
        'collaborators': collabs,
    }


def _stdlib_load_rules(rules_path: Path) -> list[dict[str, str]]:
    """Stdlib CSV mirror of `sponsors.sponsor_roles.load_rules` when `pandas` is unavailable."""
    with rules_path.open(encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        cols = list(reader.fieldnames or [])
        missing = [c for c in ('canonical', 'rule_type', 'pattern') if c not in cols]
        if missing:
            raise ValueError(f'alias file missing columns: {missing}')
        raw_rows = list(reader)

    rules: list[dict[str, str]] = []
    for r in raw_rows:
        canonical = str(r.get('canonical') or '')
        rule_type = str(r.get('rule_type') or '')
        pattern = str(r.get('pattern') or '')
        status = str(r.get('status') or 'attribute') or 'attribute'
        shared = str(r.get('shared') or 'no') or 'no'
        note = str(r.get('note') or '')
        if rule_type not in VALID_RULE_TYPES:
            raise ValueError(f'unknown rule_type: {rule_type!r}')
        if status not in VALID_RULE_STATUS:
            raise ValueError(f'unknown status: {status!r}')
        if shared not in VALID_RULE_SHARED:
            raise ValueError(f'unknown shared value: {shared!r}')
        if not note.strip():
            raise ValueError(f'rule for {canonical!r} ({pattern!r}) has an empty note')
        rules.append({
            'canonical': canonical,
            'rule_type': rule_type,
            'pattern': pattern,
            'status': status,
            'shared': shared,
            'note': note,
        })
    return rules


def _stdlib_partition_and_index(
    stored_records: list[dict[str, Any]],
    rules: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, set[str]], list[dict[str, str]]]:
    """Exact 1-to-1 stdlib mirror of `sponsors.adapter`, `audit_states.partition`, and `build_index`."""
    frame_rows: list[dict[str, str]] = []
    n_dropped_empty_collab = 0
    n_unknown_lead = 0
    n_empty_lead = 0
    novel: Counter[str] = Counter()

    for rec in stored_records:
        nct_id = str(rec.get('nct_id') or '')
        lead_name = rec.get('lead_sponsor_name')
        lead_cls = str(rec.get('sponsor_class') or '')
        if lead_name == 'Unknown':
            n_unknown_lead += 1
        elif lead_name is None or str(lead_name).strip() == '':
            n_empty_lead += 1
            lead_name = '' if lead_name is None else str(lead_name)
        if lead_cls not in AACT_AGENCY_CLASSES:
            novel[lead_cls] += 1
        frame_rows.append({
            'nct_id': nct_id,
            'agency_class': lead_cls,
            'lead_or_collaborator': 'lead',
            'name': str(lead_name),
        })
        for collab in rec.get('collaborators') or []:
            cname = collab.get('name')
            if cname is None or str(cname).strip() == '':
                n_dropped_empty_collab += 1
                continue
            ccls = str(collab.get('class') or '')
            if ccls not in AACT_AGENCY_CLASSES:
                novel[ccls] += 1
            frame_rows.append({
                'nct_id': nct_id,
                'agency_class': ccls,
                'lead_or_collaborator': 'collaborator',
                'name': str(cname),
            })

    adapter_log: dict[str, Any] = {
        'n_records': len(stored_records),
        'n_rows_emitted': len(frame_rows),
        'n_rows_dropped_empty_collaborator_name': n_dropped_empty_collab,
        'n_unknown_lead_names': n_unknown_lead,
        'n_empty_lead_names': n_empty_lead,
        'novel_agency_classes': dict(novel),
    }

    universe = sorted({r['name'] for r in frame_rows})
    norm_of = {lit: E.normalize_sponsor_name(lit) for lit in universe}
    parent_of = {lit: E.parsed_sponsor_parent(lit) for lit in universe}

    def hits(rule: dict[str, str]) -> list[str]:
        rt = rule['rule_type']
        pat = rule['pattern']
        if rt == 'exact_literal':
            return [lit for lit in universe if lit == pat]
        if rt == 'exact_normalized':
            return [lit for lit in universe if norm_of[lit] == pat]
        return [lit for lit in universe if parent_of[lit] == pat]

    lit_claims: dict[str, list[tuple[str, str, str]]] = {}
    active = [r for r in rules if r['status'] == 'attribute']
    for rt in ('exact_literal', 'exact_normalized', 'subsidiary_of'):
        for r in [x for x in active if x['rule_type'] == rt]:
            for lit in hits(r):
                claims = lit_claims.setdefault(lit, [])
                if any(c == r['canonical'] for c, _, _ in claims):
                    continue
                if claims and not (r['shared'] == 'yes' and all(s == 'yes' for _, _, s in claims)):
                    others = sorted({c for c, _, _ in claims})
                    raise ValueError(
                        f"conflicting rules for literal {lit!r}: {others} vs {r['canonical']!r}"
                    )
                claims.append((r['canonical'], rt, r['shared']))

    excluded: set[str] = set()
    for r in [x for x in rules if x['status'] == 'exclude']:
        for lit in hits(r):
            if lit in lit_claims:
                claimed = sorted({c for c, _, _ in lit_claims[lit]})
                raise ValueError(
                    f'conflicting rules for literal {lit!r}: attributed to {claimed} and excluded'
                )
            excluded.add(lit)

    attributed = set(lit_claims)
    unreviewed = set(universe) - attributed - excluded
    states = {
        'attributed': attributed,
        'reviewed_excluded': excluded,
        'unreviewed': unreviewed,
    }

    seen_index_keys: set[tuple[str, str, str, str]] = set()
    index_rows: list[dict[str, str]] = []
    for frow in frame_rows:
        lit = frow['name']
        if lit not in lit_claims:
            continue
        ent = E.parsed_sponsor_entity(lit)
        role = frow['lead_or_collaborator']
        for canon, mrule, shared in lit_claims[lit]:
            dedupe = (frow['nct_id'], canon, ent, role)
            if dedupe in seen_index_keys:
                continue
            seen_index_keys.add(dedupe)
            index_rows.append({
                'nct_id': frow['nct_id'],
                'canonical': canon,
                'entity': ent,
                'role': role,
                'literal_name': lit,
                'agency_class': frow['agency_class'],
                'match_rule': mrule,
                'shared': shared,
            })

    return frame_rows, adapter_log, states, index_rows


def load_sponsor_context(
    studies_by_nct: dict[str, dict[str, Any] | None],
    requested_ids: list[str],
    rules_path: Path,
) -> dict[str, Any]:
    """Run read-only `sponsors/` attribution & 3-state partition over retrieved trials."""
    rules_sha256 = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    stored_records = [
        _stored_sponsor_record(nct_id, studies_by_nct[nct_id])  # type: ignore[arg-type]
        for nct_id in requested_ids
        if studies_by_nct.get(nct_id) is not None
    ]

    rules_canonicals_by_norm: dict[str, set[str]] = {}
    if _SR is not None and _SA is not None and _SAS is not None:
        pd_rules = _SR.load_rules(str(rules_path))
        for _, r in pd_rules[pd_rules.status == 'attribute'].iterrows():
            norm_pat = E.normalize_sponsor_name(str(r.pattern))
            if norm_pat:
                rules_canonicals_by_norm.setdefault(norm_pat, set()).add(str(r.canonical))
        pd_frame, pd_log = _SA.records_to_frame(stored_records)
        states = _SAS.partition(pd_frame, pd_rules)
        pd_index = _SR.build_index(pd_frame, pd_rules)
        frame_rows = pd_frame.to_dict('records') if not pd_frame.empty else []
        index_rows = pd_index.to_dict('records') if not pd_index.empty else []
        adapter_log = pd_log.to_dict()
        loader_commit = _SA.AACT_LOADER_COMMIT
    else:
        std_rules = _stdlib_load_rules(rules_path)
        for r in std_rules:
            if r['status'] == 'attribute':
                norm_pat = E.normalize_sponsor_name(r['pattern'])
                if norm_pat:
                    rules_canonicals_by_norm.setdefault(norm_pat, set()).add(r['canonical'])
        frame_rows, adapter_log, states, index_rows = _stdlib_partition_and_index(stored_records, std_rules)
        loader_commit = AACT_LOADER_COMMIT

    lead_canon: dict[str, list[str]] = {}
    lead_ent: dict[str, list[str]] = {}
    any_canon: dict[str, list[str]] = {}
    any_ent: dict[str, list[str]] = {}
    for row in index_rows:
        nct = str(row['nct_id'])
        can = str(row['canonical'])
        ent = str(row['entity'])
        role = str(row['role'])
        any_canon.setdefault(nct, [])
        if can not in any_canon[nct]:
            any_canon[nct].append(can)
        any_ent.setdefault(nct, [])
        if ent not in any_ent[nct]:
            any_ent[nct].append(ent)
        if role == 'lead':
            lead_canon.setdefault(nct, [])
            if can not in lead_canon[nct]:
                lead_canon[nct].append(can)
            lead_ent.setdefault(nct, [])
            if ent not in lead_ent[nct]:
                lead_ent[nct].append(ent)

    per_trial: dict[str, dict[str, Any]] = {}
    rec_by_nct = {r['nct_id']: r for r in stored_records}
    for nct_id in requested_ids:
        rec = rec_by_nct.get(nct_id)
        if rec is None:
            per_trial[nct_id] = {
                'lead_literal': None,
                'lead_class': None,
                'lead_review_state': 'study_not_retrieved',
                'lead_canonicals_current_owner': [],
                'lead_entities_as_registered': [],
                'any_canonicals_current_owner': [],
                'any_entities_as_registered': [],
            }
            continue
        lit = rec['lead_sponsor_name']
        state = (
            'attributed' if lit in states['attributed']
            else 'reviewed_excluded' if lit in states['reviewed_excluded']
            else 'unreviewed'
        )
        per_trial[nct_id] = {
            'lead_literal': lit,
            'lead_class': rec['sponsor_class'],
            'lead_review_state': state,
            'lead_canonicals_current_owner': sorted(lead_canon.get(nct_id, [])),
            'lead_entities_as_registered': sorted(lead_ent.get(nct_id, [])),
            'any_canonicals_current_owner': sorted(any_canon.get(nct_id, [])),
            'any_entities_as_registered': sorted(any_ent.get(nct_id, [])),
        }

    return {
        'rules_sha256': rules_sha256,
        'aact_loader_commit': loader_commit,
        'adapter_log': adapter_log,
        'frame_rows': frame_rows,
        'states': states,
        'index_rows': index_rows,
        'per_trial': per_trial,
        'rules_canonicals_by_norm': rules_canonicals_by_norm,
    }


def _rollup_metrics_for_ncts(
    nct_ids: set[str],
    timelines_by_nct: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(nct_ids)
    win_counts = Counter(timelines_by_nct[n]['window_365d']['state'] for n in ordered)
    disc_counts = Counter(timelines_by_nct[n]['discordance']['state'] for n in ordered)
    exact_qc = [
        timelines_by_nct[n]['qc_lag']['exact_days']
        for n in ordered
        if timelines_by_nct[n]['qc_lag']['exact_days'] is not None
    ]
    return {
        'n_trials': len(ordered),
        'window_365d': {st: win_counts.get(st, 0) for st in WINDOW_STATES_365D},
        'discordance': {st: disc_counts.get(st, 0) for st in DISCORDANCE_STATES},
        'qc_lag_exact_n': len(exact_qc),
        'qc_lag_exact_median_days': float(statistics.median(exact_qc)) if exact_qc else None,
    }


def build_sponsor_rollups(
    sponsor_ctx: dict[str, Any],
    requested_ids: list[str],
    timelines_by_nct: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build `sponsors.json` payload with `current_owner` & `as_registered` views and 3-state separation."""
    frame_rows: list[dict[str, str]] = sponsor_ctx['frame_rows']
    states: dict[str, set[str]] = sponsor_ctx['states']
    index_rows: list[dict[str, str]] = sponsor_ctx['index_rows']
    per_trial: dict[str, dict[str, Any]] = sponsor_ctx['per_trial']

    # 1. `current_owner` rollups (by `canonical`, deduplicating `nct_id` on shared/both-role trials)
    by_current_owner: list[dict[str, Any]] = []
    for canonical in sorted({str(r['canonical']) for r in index_rows}):
        sub = [r for r in index_rows if r['canonical'] == canonical]
        any_ncts = {str(r['nct_id']) for r in sub}
        lead_ncts = {str(r['nct_id']) for r in sub if r['role'] == 'lead'}
        collab_ncts = {str(r['nct_id']) for r in sub if r['role'] == 'collaborator'}
        ents = sorted({str(r['entity']) for r in sub})
        by_current_owner.append({
            'canonical': canonical,
            'view': 'current_owner',
            'constituent_entities': ents,
            'n_trials_any': len(any_ncts),
            'n_trials_lead': len(lead_ncts),
            'n_trials_collaborator': len(collab_ncts),
            'any_involvement': _rollup_metrics_for_ncts(any_ncts, timelines_by_nct),
            'lead_only': _rollup_metrics_for_ncts(lead_ncts, timelines_by_nct),
        })

    # 2. `as_registered` rollups (by `entity`, deduplicating `nct_id`)
    by_as_registered: list[dict[str, Any]] = []
    for entity in sorted({str(r['entity']) for r in index_rows}):
        sub = [r for r in index_rows if r['entity'] == entity]
        any_ncts = {str(r['nct_id']) for r in sub}
        lead_ncts = {str(r['nct_id']) for r in sub if r['role'] == 'lead'}
        collab_ncts = {str(r['nct_id']) for r in sub if r['role'] == 'collaborator'}
        canonicals = sorted({str(r['canonical']) for r in sub})
        literals = sorted({str(r['literal_name']) for r in sub})
        by_as_registered.append({
            'entity': entity,
            'view': 'as_registered',
            'canonicals_current_owner': canonicals,
            'literal_names': literals,
            'n_trials_any': len(any_ncts),
            'n_trials_lead': len(lead_ncts),
            'n_trials_collaborator': len(collab_ncts),
            'any_involvement': _rollup_metrics_for_ncts(any_ncts, timelines_by_nct),
            'lead_only': _rollup_metrics_for_ncts(lead_ncts, timelines_by_nct),
        })

    # 3. Separate non-collapsed tables for `reviewed_excluded` and `unreviewed` literals
    def _literal_table(literal_set: set[str], state_name: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not frame_rows or not literal_set:
            return rows
        for lit in sorted(literal_set):
            sub = [r for r in frame_rows if r['name'] == lit]
            if not sub:
                continue
            any_ncts = {str(r['nct_id']) for r in sub}
            lead_ncts = {str(r['nct_id']) for r in sub if r['lead_or_collaborator'] == 'lead'}
            collab_ncts = {str(r['nct_id']) for r in sub if r['lead_or_collaborator'] == 'collaborator'}
            classes = sorted({str(r['agency_class']) for r in sub})
            rows.append({
                'literal_name': lit,
                'review_state': state_name,
                'agency_classes': classes,
                'n_trials_any': len(any_ncts),
                'n_trials_lead': len(lead_ncts),
                'n_trials_collaborator': len(collab_ncts),
                'lead_only': _rollup_metrics_for_ncts(lead_ncts, timelines_by_nct),
            })
        rows.sort(key=lambda r: (-r['n_trials_any'], r['literal_name']))
        return rows

    lead_state_trial_counts = Counter(per_trial[n]['lead_review_state'] for n in requested_ids)
    partition_summary = {
        'literals': {
            'attributed': len(states['attributed']),
            'reviewed_excluded': len(states['reviewed_excluded']),
            'unreviewed': len(states['unreviewed']),
        },
        'trials_by_lead_review_state': {
            'attributed': lead_state_trial_counts.get('attributed', 0),
            'reviewed_excluded': lead_state_trial_counts.get('reviewed_excluded', 0),
            'unreviewed': lead_state_trial_counts.get('unreviewed', 0),
            'study_not_retrieved': lead_state_trial_counts.get('study_not_retrieved', 0),
        },
    }

    return {
        'method': 'read-only curated sponsor attribution (sponsors/company_aliases.csv)',
        'rules_sha256': sponsor_ctx['rules_sha256'],
        'aact_loader_commit': sponsor_ctx['aact_loader_commit'],
        'adapter_log': sponsor_ctx['adapter_log'],
        'partition_summary': partition_summary,
        'by_current_owner': by_current_owner,
        'by_as_registered': by_as_registered,
        'reviewed_excluded_literals': _literal_table(states['reviewed_excluded'], 'reviewed_excluded'),
        'unreviewed_literals': _literal_table(states['unreviewed'], 'unreviewed'),
    }
