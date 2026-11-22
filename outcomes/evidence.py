"""Registry evidence facts, cohort eligibility and the cutoff-dependent results state.

Everything here is read from one ClinicalTrials.gov API v2 study record as published, with
absence kept as None. The rules mirror the private outcome codebook (method 0.3.0, codebook
sections 1 and the cutoff rules of METHODS_SPEC.md); the private export's eligibility for the
audit trials is cross-checked against this module on every build, so the two cannot drift
silently.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

FRAME_QUERY = ('AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY AND AREA[Phase]PHASE3 AND '
               'AREA[DesignAllocation]RANDOMIZED AND AREA[PrimaryCompletionDate]RANGE[2020-01-01,2020-12-31]')
WINDOW = ('2020-01-01', '2020-12-31')
EXCLUSION_REASONS = ('NOT_INTERVENTIONAL', 'NOT_PHASE3', 'PHASE2_PHASE3_STRATUM', 'LEAD_SPONSOR_NOT_INDUSTRY',
                     'NOT_RANDOMIZED', 'PRIMARY_COMPLETION_OUTSIDE_WINDOW')
# Results availability states, a subset of the codebook's trial states that registry facts alone
# decide. POSTED is the absence of an absence state.
RESULTS_STATES = {
    'POSTED': (None,),
    'NOT_APPLICABLE': ('WITHDRAWN_NO_PARTICIPANTS', 'TERMINATED_NO_DATA'),
    'PENDING': ('WITHIN_STANDARD_WINDOW', 'CERTIFIED_DELAY', 'STATUS_UNCERTAIN'),
    'UNREPORTED': ('PAST_STANDARD_WINDOW',),
}
CDN = 'https://cdn.clinicaltrials.gov/large-docs/'


def parse_date(s: Any) -> tuple[date | None, str | None]:
    """(date, precision) for 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'; (None, None) otherwise.

    A month-precision date is returned as the first of the month; callers that need the end of
    the period say so (month_end)."""
    if not isinstance(s, str):
        return None, None
    m = re.fullmatch(r'(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?', s.strip())
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    if d:
        return date(y, int(mo), int(d)), 'day'
    if mo:
        return date(y, int(mo), 1), 'month'
    return date(y, 1, 1), 'year'


def period_end(d: date, precision: str | None) -> date:
    """Last day of the period a date of the given precision stands for."""
    if precision == 'year':
        return date(d.year, 12, 31)
    if precision == 'month':
        nxt = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
        return date.fromordinal(nxt.toordinal() - 1)
    return d


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:          # 29 February
        return d.replace(year=d.year + n, day=28)


def facts(study: dict[str, Any]) -> dict[str, Any]:
    """The registry facts the beta displays, read verbatim."""
    p = study.get('protocolSection') or {}
    ident, status = p.get('identificationModule') or {}, p.get('statusModule') or {}
    design = p.get('designModule') or {}
    info = design.get('designInfo') or {}
    spons = p.get('sponsorCollaboratorsModule') or {}
    lead = spons.get('leadSponsor') or {}
    enr = design.get('enrollmentInfo') or {}
    pcd = status.get('primaryCompletionDateStruct') or {}
    nct = ident.get('nctId')
    docs = ((study.get('documentSection') or {}).get('largeDocumentModule') or {}).get('largeDocs') or []
    return {
        'nct_id': nct,
        'brief_title': ident.get('briefTitle'),
        'lead_sponsor_name': lead.get('name'),
        'lead_sponsor_class': lead.get('class'),
        'collaborators': [{'name': c.get('name'), 'class': c.get('class')} for c in spons.get('collaborators') or []],
        'conditions': list((p.get('conditionsModule') or {}).get('conditions') or []),
        'study_type': design.get('studyType'),
        'phases': list(design.get('phases') or []),
        'allocation': info.get('allocation'),
        'enrollment': enr.get('count'),
        'enrollment_type': enr.get('type'),
        'overall_status': status.get('overallStatus'),
        'why_stopped': status.get('whyStopped'),
        'start_date': (status.get('startDateStruct') or {}).get('date'),
        'primary_completion_date': pcd.get('date'),
        'primary_completion_type': pcd.get('type'),
        'study_first_submit_date': status.get('studyFirstSubmitDate'),
        'results_first_post_date': (status.get('resultsFirstPostDateStruct') or {}).get('date'),
        'certification_first_submit_date': status.get('dispFirstSubmitDate'),
        'last_update_post_date': (status.get('lastUpdatePostDateStruct') or {}).get('date'),
        'has_results': bool(study.get('hasResults')),
        'registered_primary_outcomes': [
            {'measure': o.get('measure'), 'time_frame': o.get('timeFrame')}
            for o in (p.get('outcomesModule') or {}).get('primaryOutcomes') or []],
        'documents': [{
            'label': d.get('label'), 'has_protocol': bool(d.get('hasProtocol')), 'has_sap': bool(d.get('hasSap')),
            'has_icf': bool(d.get('hasIcf')), 'document_date': d.get('date'), 'upload_date': d.get('uploadDate'),
            'url': f"{CDN}{nct[-2:]}/{nct}/{d.get('filename')}" if nct and d.get('filename') else None}
            for d in docs],
    }


def eligibility(f: dict[str, Any], window: tuple[str, str] = WINDOW) -> list[str]:
    """Exclusion reasons (codebook E1-E5); an empty list means eligible.

    The frame query already applies most of these; they are re-checked so that a frame built
    another way, or a record that changed after the query matched it, cannot slip in. Phase 2/3
    records match the query's PHASE3 term and are excluded as their own stratum (codebook
    change 16). A month- or year-precision primary completion date is inside the window only
    when its whole period is."""
    reasons = []
    if f['study_type'] != 'INTERVENTIONAL':
        reasons.append('NOT_INTERVENTIONAL')
    if 'PHASE3' not in f['phases']:
        reasons.append('NOT_PHASE3')
    elif 'PHASE2' in f['phases']:
        reasons.append('PHASE2_PHASE3_STRATUM')
    if f['lead_sponsor_class'] != 'INDUSTRY':
        reasons.append('LEAD_SPONSOR_NOT_INDUSTRY')
    if f['allocation'] != 'RANDOMIZED':
        reasons.append('NOT_RANDOMIZED')
    d, prec = parse_date(f['primary_completion_date'])
    lo, _ = parse_date(window[0])
    hi, _ = parse_date(window[1])
    if d is None or lo is None or hi is None or not (lo <= d and period_end(d, prec) <= hi):
        reasons.append('PRIMARY_COMPLETION_OUTSIDE_WINDOW')
    return reasons


def results_state(f: dict[str, Any], cutoff: date) -> tuple[str, str | None]:
    """Results availability at the cutoff, recomputed on every build and never carried forward.

    The one-year window after primary completion is the general FDAAA results deadline; this
    module does not establish whether a trial is an applicable clinical trial or whether an
    extension was granted, so PAST_STANDARD_WINDOW is a timing fact, not a finding of
    non-compliance. ESTIMATED primary completion or UNKNOWN status leaves the window
    undeterminable (STATUS_UNCERTAIN)."""
    posted, _ = parse_date(f['results_first_post_date'])
    # A record retrieved after the cutoff can carry results first posted after it: as of the
    # cutoff those results were not yet posted. A missing post date on a record with results
    # is read as posted (the registry sets the date whenever it posts results).
    if f['has_results'] and (posted is None or posted <= cutoff):
        return 'POSTED', None
    if f['overall_status'] == 'WITHDRAWN':
        return 'NOT_APPLICABLE', 'WITHDRAWN_NO_PARTICIPANTS'
    if f['overall_status'] == 'TERMINATED' and f['enrollment_type'] == 'ACTUAL' and f['enrollment'] == 0:
        return 'NOT_APPLICABLE', 'TERMINATED_NO_DATA'
    pcd, prec = parse_date(f['primary_completion_date'])
    if pcd is None or f['primary_completion_type'] != 'ACTUAL' or f['overall_status'] == 'UNKNOWN':
        return 'PENDING', 'STATUS_UNCERTAIN'
    pcd = period_end(pcd, prec)
    cert, _ = parse_date(f['certification_first_submit_date'])
    # A certification counts only once it exists: filed on or before the cutoff.
    if cert and cert <= cutoff < add_years(cert, 2):
        return 'PENDING', 'CERTIFIED_DELAY'
    if cutoff < add_years(pcd, 1):
        return 'PENDING', 'WITHIN_STANDARD_WINDOW'
    return 'UNREPORTED', 'PAST_STANDARD_WINDOW'


def document_flags(f: dict[str, Any]) -> dict[str, bool]:
    """Whether a protocol and a SAP are posted. Availability only: a posted document need not
    state a usable decision rule, and the beta never counts one as if it did."""
    return {'protocol': any(d['has_protocol'] for d in f['documents']),
            'sap': any(d['has_sap'] for d in f['documents'])}
