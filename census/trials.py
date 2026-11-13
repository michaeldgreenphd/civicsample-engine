"""Trial-side rules: cohort, single-site tiers, eligible ages, enrolment period, observed shares.

Pure functions over the weekly release's records (and, for the tier, registry text). No network.
"""
from __future__ import annotations

import re
from typing import Any

# The four ACS 5-year windows used. They do not overlap, so two trials matched to different
# windows are compared with independent survey samples; adjacent yearly 5-year releases share
# four of their five years and must not be read as separate annual observations.
WINDOWS: tuple[tuple[int, int], ...] = ((2005, 2009), (2010, 2014), (2015, 2019), (2020, 2024))

RACE_KEYS = ('american_indian_alaska_native', 'asian', 'black_african_american',
             'native_hawaiian_pacific_islander', 'white', 'more_than_one_race', 'other',
             'unknown_not_reported')
ETHNICITY_KEYS = ('hispanic_latino', 'not_hispanic_latino', 'unknown_not_reported')
SEX_KEYS = ('female', 'male', 'unknown')
DIMENSION_KEYS = {'race': RACE_KEYS, 'ethnicity': ETHNICITY_KEYS, 'sex': SEX_KEYS}
# What the registry's baseline table describes. It is the baseline analysis population with all
# arms combined, which can be smaller than enrolment; it is not one arm, and not an outcome
# analysis population.
POPULATION_BASIS = ('ClinicalTrials.gov baseline characteristics: participants in the baseline '
                    'analysis population, all arms combined')

_UNIT_YEARS = {'year': 1.0, 'month': 1 / 12, 'week': 1 / 52, 'day': 1 / 365, 'hour': 1 / 8760,
               'minute': 1 / 525600}


def parse_age(text: str | None) -> float | None:
    """'18 Years' -> 18.0; 'N/A' or empty -> None (no limit). Raises ValueError when unreadable."""
    if text is None or not text.strip() or text.strip().upper() == 'N/A':
        return None
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+?)s?\s*', text)
    if not m or m.group(2).lower() not in _UNIT_YEARS:
        raise ValueError(f'unreadable age: {text!r}')
    return float(m.group(1)) * _UNIT_YEARS[m.group(2).lower()]


def eligible_ages(record: dict[str, Any]) -> tuple[float, float | None]:
    """[lo, hi) in years. The registry's maximum age is inclusive of that whole year of age
    ('65 Years' admits people aged 65), so the upper bound is max + 1 when given in years."""
    lo = parse_age(record.get('min_age')) or 0.0
    hi = parse_age(record.get('max_age'))
    if hi is not None:
        hi = hi + 1.0 if str(record.get('max_age', '')).strip().lower().endswith(('year', 'years')) else hi
        if hi <= lo:
            raise ValueError(f'maximum age below minimum: {record.get("min_age")!r}..{record.get("max_age")!r}')
    return lo, hi


def eligible_sexes(record: dict[str, Any]) -> tuple[str, ...]:
    """Sexes the registry says may enrol. An unstated value is treated as all, and flagged by
    the caller. This is the registry's eligibility field; it says nothing about gender identity."""
    sex = ((record.get('eligibility') or {}).get('sex') or 'ALL').upper()
    return {'FEMALE': ('female',), 'MALE': ('male',)}.get(sex, ('male', 'female'))


def year_fraction(date: str | None) -> float | None:
    """'2021-03-04' or '2021-03' or '2021' -> a decimal year at the stated precision's middle."""
    m = re.fullmatch(r'(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?', (date or '').strip())
    if not m:
        return None
    year, month, day = int(m.group(1)), m.group(2), m.group(3)
    if month is None:
        return year + 0.5
    return year + (int(month) - 1) / 12 + ((int(day) - 1) / 365 if day else 1 / 24)


def enrolment_period(record: dict[str, Any]) -> dict[str, Any]:
    """The period the benchmark is aligned to, and which dates stood in for enrolment.

    The registry does not record when enrolment opened and closed. Start date to primary
    completion date is the closest available proxy: it runs past the last enrolment by the
    length of follow-up, so its midpoint sits somewhat late. The basis is recorded per trial."""
    start = year_fraction(record.get('start_date'))
    end = year_fraction(record.get('primary_completion_date'))
    end_basis = 'primary_completion'
    if end is None:
        end, end_basis = year_fraction(record.get('completion_date')), 'completion'
    out: dict[str, Any] = {'start_date': record.get('start_date'),
                           'primary_completion_date': record.get('primary_completion_date'),
                           'completion_date': record.get('completion_date')}
    if start is not None and end is not None and end >= start:
        out.update(basis=f'midpoint_of_start_to_{end_basis}', midpoint=round((start + end) / 2, 2))
    elif start is not None:
        out.update(basis='start_date_only', midpoint=round(start, 2))
    elif end is not None:
        out.update(basis=f'{end_basis}_date_only', midpoint=round(end, 2))
    else:
        out.update(basis='no_usable_date', midpoint=None)
    return out


def assign_window(midpoint: float | None) -> dict[str, Any]:
    """The non-overlapping ACS window for an enrolment midpoint, with the reason for the match.

    A trial before the first window or after the last is matched to the nearest one and says so,
    with the gap in years; the caller reports those trials separately. A 5-year estimate
    describes its whole window, not the window's final year."""
    if midpoint is None:
        return {'vintage': None, 'window': None, 'reason': 'no_usable_date', 'gap_years': None}
    for lo, hi in WINDOWS:
        if lo <= midpoint < hi + 1:
            return {'vintage': hi, 'window': f'{lo}-{hi}', 'reason': 'midpoint_inside_window', 'gap_years': 0.0}
    first, last = WINDOWS[0], WINDOWS[-1]
    if midpoint < first[0]:
        return {'vintage': first[1], 'window': f'{first[0]}-{first[1]}', 'reason': 'before_first_window',
                'gap_years': round(first[0] - midpoint, 2)}
    return {'vintage': last[1], 'window': f'{last[0]}-{last[1]}', 'reason': 'after_last_window',
            'gap_years': round(midpoint - (last[1] + 1), 2)}


# A registration that lists one facility is not thereby a single-site study: sponsors may list
# only the coordinating centre. The registry has no single-site field, so the only corroboration
# available is the study's own text. Neither pattern is verification by a third party.
_SINGLE = re.compile(r'\b(?:single|mono)[\s-]?(?:cent(?:er|re|ric)|site|institution(?:al)?)\b', re.I)
# A bare number before "sites" is left out on purpose: "injected at 2 sites" is anatomy, not a
# site count. A number counts only before a word that can only mean a place of care or research.
_MULTI = re.compile(r'\bmulti[\s-]?(?:cent(?:er|re|ric)|site|institution(?:al)?|national)\w*\b'
                    r'|\b(?:two|three|four|five|six|seven|eight|nine|ten|\d{1,3}|multiple|several)\s+'
                    r'(?:\w+\s+){0,2}(?:cent(?:er|re)s|hospitals|institutions|'
                    r'(?:study|clinical|research|investigational|participating|enrolling)\s+sites)\b', re.I)


def site_tier(text: str) -> dict[str, Any]:
    """How far the study's own text supports the single listed facility being its only site."""
    single = sorted({m.group(0).lower() for m in _SINGLE.finditer(text or '')})
    multi = sorted({m.group(0).lower() for m in _MULTI.finditer(text or '')})
    if single and not multi:
        tier = 'self_described_single_site'
    elif multi and not single:
        tier = 'text_indicates_multiple_sites'
    elif single and multi:
        tier = 'conflicting_text'
    else:
        tier = 'one_listed_facility'
    return {'tier': tier, 'single_phrases': single[:5], 'multiple_phrases': multi[:5]}


def cohort(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Trials whose registration lists exactly one facility, in the United States, with a funnel.

    A trial with several facilities is never single-site here, even when only one of them has a
    usable address; that case is counted so it is visible, and left out."""
    funnel = {'records': len(records), 'no_facility_listed': 0, 'any_non_us_facility': 0,
              'us_several_facilities': 0, 'us_several_facilities_one_with_zip': 0, 'us_one_listed_facility': 0}
    keep = []
    for r in records:
        sites = r.get('study_sites') or []
        if not sites:
            funnel['no_facility_listed'] += 1
        elif any(s.get('country') != 'United States' for s in sites):
            funnel['any_non_us_facility'] += 1
        elif len(sites) > 1:
            funnel['us_several_facilities'] += 1
            funnel['us_several_facilities_one_with_zip'] += sum(1 for s in sites if s.get('zip')) == 1
        else:
            funnel['us_one_listed_facility'] += 1
            keep.append(r)
    return keep, funnel


def observed(record: dict[str, Any], dimension: str) -> dict[str, Any]:
    """Counts as the registry table gives them, with unknown kept as its own row.

    'reported': the table exists. Absence is recorded as a reason, never as zeros. The total is
    the sum of the table's own rows, so every share has the table as its denominator."""
    block = record.get(dimension) or {}
    totals = block.get('totals') if dimension == 'sex' else block.get('omb_totals')
    if not block.get('reported') or not isinstance(totals, dict):
        return {'reported': False, 'reason': f'{dimension}_not_reported'}
    counts = {k: totals.get(k) or 0 for k in DIMENSION_KEYS[dimension]}
    extra = {k: v for k, v in totals.items() if k not in counts and v}
    total = sum(counts.values()) + sum(extra.values())
    if total <= 0:
        return {'reported': False, 'reason': f'{dimension}_table_total_is_zero'}
    out: dict[str, Any] = {'reported': True, 'counts': counts, 'n_in_table': total,
                           'original_categories': [{'original': c.get('original'), 'count': c.get('count')}
                                                   for c in (block.get('raw_categories') or [])][:40]}
    if extra:   # a category this module does not harmonise stays in the denominator and is named
        out['unharmonised_counts'] = extra
    return out
