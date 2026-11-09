"""Lead-sponsor attribution for the outcomes beta.

The beta names each trial's lead sponsor through the curated rules in sponsors/ (the
company_aliases.csv rules, keyed by their sha256), not the Industry tab's regex recode in
scripts/generate_industry_sponsors.py. Both are computed so the difference is visible, and the
Industry demographics view is not changed.

Every lead literal keeps one of the three review states (attributed, reviewed_excluded,
unreviewed); an unreviewed or excluded literal is shown verbatim, never coerced to a company.
A literal attributed to several companies by shared rules lists all of them.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sponsors import adapter, audit_states, sponsor_roles  # noqa: E402

RULES_PATH = ROOT / 'sponsors' / 'company_aliases.csv'


def _industry_recode() -> Any:
    """The Industry tab's recode, imported from its script without running it."""
    scripts = str(ROOT / 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import generate_industry_sponsors as gis   # noqa: E402
    return gis


def stored_record(f: dict[str, Any]) -> dict[str, Any]:
    """The weekly pipeline's stored sponsor fields (src/utils.py), from evidence facts."""
    return {'nct_id': f['nct_id'], 'sponsor_class': f['lead_sponsor_class'] or 'Unknown',
            'lead_sponsor_name': f['lead_sponsor_name'] if f['lead_sponsor_name'] is not None else 'Unknown',
            'collaborators': [{'name': c['name'] or '', 'class': c['class'] or ''} for c in f['collaborators']]}


def attribute(facts_list: list[dict[str, Any]], rules_path: Path = RULES_PATH) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """({nct_id: attribution}, summary). attribution: lead literal, review state, curated
    canonical companies, the Industry recode's company and whether the two differ."""
    records = [stored_record(f) for f in facts_list]
    frame, _log = adapter.records_to_frame(records)
    rules = sponsor_roles.load_rules(str(rules_path))
    states = audit_states.partition(frame, rules)
    index = sponsor_roles.build_index(frame, rules)
    lead_rows = index[index['role'] == 'lead']
    canon: dict[str, list[str]] = {}
    for nct, c in zip(lead_rows['nct_id'], lead_rows['canonical']):
        canon.setdefault(nct, [])
        if c not in canon[nct]:
            canon[nct].append(c)
    gis = _industry_recode()
    out, n_differ = {}, 0
    differ_by_state = {'attributed': 0, 'reviewed_excluded': 0, 'unreviewed': 0}
    counts = {'attributed': 0, 'reviewed_excluded': 0, 'unreviewed': 0}
    for rec in records:
        lit = rec['lead_sponsor_name']
        state = ('attributed' if lit in states['attributed'] else
                 'reviewed_excluded' if lit in states['reviewed_excluded'] else 'unreviewed')
        counts[state] += 1
        companies = sorted(canon.get(rec['nct_id'], []))
        recode, via_lead = gis.assign_industry_company(rec)
        # "Differs" compares the display names: the curated canonical(s) when attributed, else the
        # literal as shown; the recode's company otherwise.
        shown = companies if state == 'attributed' else [lit]
        differs = recode not in shown
        n_differ += differs
        differ_by_state[state] += differs
        out[rec['nct_id']] = {'lead_literal': lit, 'review_state': state, 'companies': companies,
                              'industry_recode_company': recode, 'industry_recode_via_lead': via_lead,
                              'differs_from_industry_recode': differs}
    summary = {'method': 'curated lead-sponsor rules (sponsors/company_aliases.csv)',
               'rules_sha256': hashlib.sha256(rules_path.read_bytes()).hexdigest(),
               'lead_review_states': counts,
               'n_differs_from_industry_recode': n_differ,
               # attributed: the curated canonical names another company or a different level
               # (registered entity vs parent); unreviewed/excluded: the literal is shown as
               # registered where the recode assigns a company.
               'n_differs_by_review_state': differ_by_state,
               'industry_recode': 'scripts/generate_industry_sponsors.py assign_industry_company (unchanged)'}
    return out, summary


def display_name(a: dict[str, Any]) -> str:
    return ' / '.join(a['companies']) if a['review_state'] == 'attributed' and a['companies'] else a['lead_literal']
