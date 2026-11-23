"""Schema-version-1 artifact builder, atomic writer, and producer-side contract checker.

Artifact family written by `build_artifacts` and `write_artifacts`:
  run.json        manifest pinning input/cache sha256s, curated CSV sha256s, code `source_sha256`,
                  deterministic 64-hex `run_identity`, `run_id`, vocabularies, and sibling sha256s
  summary.json    pre-computed denominators, headline metrics, and breakdown tables where EVERY
                  count object carries `value`, `numerator`, `denominator`, and `definition` so
                  the browser computes zero aggregates
  studies.json    one normalized registry study metadata record per included NCT ID
  links.json      all cross-source evidence link records (`link:<sha256[:16]>`) and orphaned reviews
  coverage.json   per-trial x 5-route 7-state coverage matrix and overall precedence state
  timelines.json  per-trial milestones, partial-date interval bounds, QC lag, 365d window, discordance
  sponsors.json   read-only `sponsors/` rollups (`current_owner` & `as_registered`) and 3-state tables
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from disclosure import METHOD_VERSION, SCHEMA_VERSION
from disclosure import acquire as A
from disclosure import coverage as C
from disclosure import evidence as E
from disclosure import timeline as T

FILES = (
    'summary.json',
    'studies.json',
    'links.json',
    'coverage.json',
    'timelines.json',
    'sponsors.json',
)

COUNTING_UNITS: dict[str, str] = {
    'trial': 'one distinct ClinicalTrials.gov study identifier (NCT ID) in the build scope',
    'evidence_link': 'one immutable cross-source evidence record (`link:<sha256[:16]>`)',
    'route_cell': 'one `(nct_id, route)` cell in the 5-route search coverage matrix',
    'sponsor_literal': 'one distinct raw sponsor or collaborator string observed in retrieved studies',
    'canonical_company': 'one canonical company under curated `sponsors/company_aliases.csv` (`current_owner`)',
    'registered_entity': 'one normalized registered sponsor entity (`as_registered`)',
}

PRIVATE_KEYS = frozenset({'reviewer', 'recorded_by'})
FORBIDDEN_COMPLIANCE_RE = re.compile(
    r'\b(?:fdaaa\s+violation|statutory\s+violation|legally\s+non[- ]?compliant|unlawful\s+delay)\b',
    re.IGNORECASE,
)
RUN_ID_RE = re.compile(r'^disclosure-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$')
GIT_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(obj: Any) -> bytes:
    """Canonical compact UTF-8 JSON bytes with sorted keys and trailing newline."""
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')


def count_object(
    *,
    metric_id: str,
    label: str,
    numerator: int,
    denominator: str,
    denominator_n: int,
    definition: str,
) -> dict[str, Any]:
    """Standardized pre-aggregated count object so downstream readers compute no aggregate."""
    return {
        'id': metric_id,
        'label': label,
        'value': numerator,
        'numerator': numerator,
        'denominator': denominator,
        'denominator_n': denominator_n,
        'proportion': round(numerator / denominator_n, 6) if denominator_n > 0 else None,
        'pct': round(100.0 * numerator / denominator_n, 2) if denominator_n > 0 else None,
        'definition': definition,
    }


def _study_record(nct_id: str, ctgov_rec: dict[str, Any] | None) -> dict[str, Any]:
    """Extract per-trial study record from frozen CT.gov response, preserving missingness as None."""
    if ctgov_rec is None:
        return {
            'nct_id': nct_id,
            'registry_state': 'not_searched',
            'retrieved_at': None,
            'brief_title': None,
            'official_title': None,
            'overall_status': None,
            'study_type': None,
            'phases': None,
            'conditions': None,
            'drug_candidates': None,
            'enrollment_count': None,
            'enrollment_type': None,
            'has_results': None,
            'lead_sponsor_name': None,
            'lead_sponsor_class': None,
            'collaborators': None,
            'n_registry_references': None,
            'is_fda_regulated_drug': None,
        }
    status = ctgov_rec.get('status')
    if status == 404:
        return {
            'nct_id': nct_id,
            'registry_state': 'not_found',
            'retrieved_at': ctgov_rec.get('fetched_at'),
            'brief_title': None,
            'official_title': None,
            'overall_status': None,
            'study_type': None,
            'phases': None,
            'conditions': None,
            'drug_candidates': None,
            'enrollment_count': None,
            'enrollment_type': None,
            'has_results': None,
            'lead_sponsor_name': None,
            'lead_sponsor_class': None,
            'collaborators': None,
            'n_registry_references': None,
            'is_fda_regulated_drug': None,
        }
    body = ctgov_rec.get('body')
    if status != 200 or not A.ctgov_study_valid(body):
        return {
            'nct_id': nct_id,
            'registry_state': 'lookup_failed',
            'retrieved_at': ctgov_rec.get('fetched_at'),
            'brief_title': None,
            'official_title': None,
            'overall_status': None,
            'study_type': None,
            'phases': None,
            'conditions': None,
            'drug_candidates': None,
            'enrollment_count': None,
            'enrollment_type': None,
            'has_results': None,
            'lead_sponsor_name': None,
            'lead_sponsor_class': None,
            'collaborators': None,
            'n_registry_references': None,
            'is_fda_regulated_drug': None,
        }

    ps = body.get('protocolSection') or {}
    idm = ps.get('identificationModule') or {}
    sm = ps.get('statusModule') or {}
    dm = ps.get('designModule') or {}
    cm = ps.get('conditionsModule') or {}
    sc = ps.get('sponsorCollaboratorsModule') or {}
    ov = ps.get('oversightModule') or {}
    enr = dm.get('enrollmentInfo') or {}
    lead = sc.get('leadSponsor') or {}

    enr_count = enr.get('count')
    has_res = body.get('hasResults')
    fda_reg = ov.get('isFdaRegulatedDrug')

    return {
        'nct_id': nct_id,
        'registry_state': 'retrieved',
        'retrieved_at': ctgov_rec.get('fetched_at'),
        'brief_title': idm.get('briefTitle') if isinstance(idm.get('briefTitle'), str) else None,
        'official_title': idm.get('officialTitle') if isinstance(idm.get('officialTitle'), str) else None,
        'overall_status': sm.get('overallStatus') if isinstance(sm.get('overallStatus'), str) else None,
        'study_type': dm.get('studyType') if isinstance(dm.get('studyType'), str) else None,
        'phases': list(dm.get('phases')) if isinstance(dm.get('phases'), list) else [],
        'conditions': list(cm.get('conditions')) if isinstance(cm.get('conditions'), list) else [],
        'drug_candidates': A.extract_drug_candidates(body),
        'enrollment_count': enr_count if isinstance(enr_count, int) and not isinstance(enr_count, bool) else None,
        'enrollment_type': enr.get('type') if isinstance(enr.get('type'), str) else None,
        'has_results': has_res if isinstance(has_res, bool) else None,
        'lead_sponsor_name': lead.get('name') if isinstance(lead.get('name'), str) else None,
        'lead_sponsor_class': lead.get('class') if isinstance(lead.get('class'), str) else None,
        'collaborators': [
            {'name': c.get('name'), 'class': c.get('class')}
            for c in (sc.get('collaborators') or [])
            if isinstance(c, dict)
        ],
        'n_registry_references': len(A.extract_registry_references(body)),
        'is_fda_regulated_drug': fda_reg if isinstance(fda_reg, bool) else None,
    }


# How run.json `as_of` was chosen: the `--as-of` argument, or a default (in fallback order) from the
# cache's single CT.gov data timestamp, its last CT.gov fetch, or `generated_at`.
AS_OF_SOURCES = ('as_of_argument', 'ctgov_data_timestamp', 'ctgov_last_fetched_at', 'generated_at')


def _ctgov_version_info(ctgov_records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """CT.gov `/version` answers held in the cache, for run.json `sources.ctgov`.

    Acquisition logs two `ctgov/version/{run_stamp}/begin|end` records per run (a local freeze one
    `ctgov/version/{day}` record), and a resumed acquisition keeps the earlier runs' records and study
    responses, so one cache can mix CT.gov data refreshes.
    `versions` lists every valid version record by `fetched_at`; `data_timestamp_first` and
    `data_timestamp_last` are the earliest and latest `dataTimestamp` among them (ISO strings of one
    format, so string order is time order); `api_version` and `data_timestamp` stay those of the
    last-fetched record, as before.
    """
    versions = sorted(
        (
            r for k, r in ctgov_records.items()
            if k.startswith('ctgov/version/') and r.get('status') == 200 and A.ctgov_version_valid(r.get('body'))
        ),
        key=lambda r: (str(r.get('fetched_at') or ''), str(r.get('key') or '')),
    )
    if not versions:
        return {
            'api_version': None,
            'data_timestamp': None,
            'data_timestamp_first': None,
            'data_timestamp_last': None,
            'versions': [],
        }
    latest = versions[-1]['body']
    stamps = sorted({str(v['body']['dataTimestamp']) for v in versions})
    return {
        'api_version': latest.get('apiVersion'),
        'data_timestamp': latest.get('dataTimestamp'),
        'data_timestamp_first': stamps[0],
        'data_timestamp_last': stamps[-1],
        'versions': [
            {
                'key': v.get('key'),
                'fetched_at': v.get('fetched_at'),
                'api_version': v['body'].get('apiVersion'),
                'data_timestamp': v['body'].get('dataTimestamp'),
            }
            for v in versions
        ],
    }


def summarise_artifacts(
    studies: list[dict[str, Any]],
    links: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    timelines: list[dict[str, Any]],
    sponsors_obj: dict[str, Any],
    as_of_iso: str,
) -> dict[str, Any]:
    """Compute `summary.json` from the sibling payloads; contract checker re-runs this for parity."""
    n_included = len(studies)
    n_retrieved = sum(1 for s in studies if s['registry_state'] == 'retrieved')
    # A trial carries a posted resultsSection when its record generated the `ctgov_results` link
    # (`evidence.build_ctgov_results_links`: hasResults=true and a non-empty resultsSection). That is
    # the registry fact, counted whatever a review says: the discordance flag `ctgov_results_posted`
    # is false while a review holds the link `indeterminate`, so it would undercount.
    posted_ids = {x['nct_id'] for x in links if x['source_route'] == 'ctgov_results'}
    n_ctgov_posted = sum(1 for s in studies if s['nct_id'] in posted_ids)
    n_links = len(links)
    n_cells = sum(len(c['routes']) for c in coverage_rows)

    lit_part = sponsors_obj['partition_summary']['literals']
    n_literals = int(lit_part['attributed']) + int(lit_part['reviewed_excluded']) + int(lit_part['unreviewed'])

    denominators: dict[str, dict[str, Any]] = {
        'included_trials': {
            'n': n_included,
            'definition': 'All distinct NCT IDs included in this build scope.',
        },
        'retrieved_trials': {
            'n': n_retrieved,
            'definition': 'Included trials for which ClinicalTrials.gov returned a valid study record (HTTP 200).',
        },
        'ctgov_posted_trials': {
            'n': n_ctgov_posted,
            'definition': 'Included trials with a posted resultsSection on ClinicalTrials.gov.',
        },
        'evidence_links': {
            'n': n_links,
            'definition': 'All cross-source evidence link records (`links.json`), including set-aside candidates.',
        },
        'route_cells': {
            'n': n_cells,
            'definition': 'All `(nct_id, route)` cells across the 5-route coverage matrix (`5 * included_trials`).',
        },
        'sponsor_literals': {
            'n': n_literals,
            'definition': 'Distinct sponsor and collaborator literal strings across retrieved trials.',
        },
    }

    def m(mid: str, label: str, num: int, den_key: str, definition: str) -> dict[str, Any]:
        return count_object(
            metric_id=mid,
            label=label,
            numerator=num,
            denominator=den_key,
            denominator_n=denominators[den_key]['n'],
            definition=definition,
        )

    win_by_state = Counter(t['window_365d']['state'] for t in timelines)
    disc_by_state = Counter(t['discordance']['state'] for t in timelines)
    ov_by_state = Counter(c['overall']['state'] for c in coverage_rows)
    av_counts = Counter(x['automated_validation'] for x in links)
    rev_counts = Counter(x['review_status'] for x in links)
    route_link_counts = Counter(x['source_route'] for x in links)

    metrics = [
        m('trials_retrieved', 'Study record retrieved on CT.gov', n_retrieved, 'included_trials',
          'Included trials whose CT.gov study record resolved with HTTP 200.'),
        m('trials_ctgov_results_posted', 'CT.gov resultsSection posted', n_ctgov_posted, 'included_trials',
          'Included trials carrying a posted resultsSection on ClinicalTrials.gov.'),
        m('trials_with_linked_results_publication', 'Linked results publication confirmed',
          sum(1 for t in timelines if t['discordance']['has_linked_results_publication']), 'included_trials',
          'Included trials with a human-confirmed results publication or PubMed-resolved CT.gov RESULT citation.'),
        m('trials_with_fda_citation', 'FDA Drugs@FDA citation confirmed',
          sum(1 for t in timelines if t['discordance']['has_fda_citation']), 'included_trials',
          'Included trials with an identifier-confirmed or human-confirmed openFDA Drugs@FDA application link.'),
        m('trials_published_while_ctgov_unreported', 'Published results while CT.gov unreported',
          disc_by_state.get('published_results_while_ctgov_unreported', 0), 'included_trials',
          'Included trials with no posted CT.gov results and at least one confirmed results publication.'),
        m('trials_fda_cited_while_ctgov_unreported', 'FDA-cited while CT.gov unreported',
          disc_by_state.get('fda_cited_while_ctgov_unreported', 0), 'included_trials',
          'Included trials with no posted CT.gov results and at least one confirmed FDA Drugs@FDA link.'),
        m('trials_with_submission_infos', 'Trials carrying submissionInfos QC history',
          sum(1 for t in timelines if t['submission_infos']['present'] is True), 'retrieved_trials',
          'Retrieved trials whose protocolSection.statusModule carries a submissionInfos array.'),
        m('trials_with_exact_qc_lag', 'Trials with exact day-level QC lag',
          sum(1 for t in timelines if t['nct_id'] in posted_ids and t['qc_lag']['state'] == 'exact'),
          'ctgov_posted_trials',
          'Posted-results trials with day-precision resultsFirstSubmitDate and resultsFirstPostDate.'),
        m('links_identifier_confirmed', 'Identifier-confirmed evidence links',
          av_counts.get('identifier_confirmed', 0), 'evidence_links',
          'Evidence records where an identifier mechanically resolves in the retrieved source (not semantic review).'),
        m('links_quote_confirmed', 'Quote-confirmed documentary statement links',
          av_counts.get('quote_confirmed_in_source', 0), 'evidence_links',
          'Curated documentary statements whose verbatim excerpt stands at source_locator in frozen cache.'),
        m('links_human_reviewed', 'Human-reviewed evidence links',
          sum(1 for x in links if x['review_status'] != 'unreviewed'), 'evidence_links',
          'Evidence records carrying an explicit human review decision from disclosure/reviews.csv.'),
    ]

    window_table = {
        st: m(
            f'window_365d:{st}',
            st,
            win_by_state.get(st, 0),
            'included_trials',
            f'Included trials in descriptive 365-day registry window state {st} as of {as_of_iso}. '
            'Strictly descriptive; not a finding of statutory compliance.',
        )
        for st in T.WINDOW_STATES_365D
    }

    discordance_table = {
        st: m(
            f'discordance:{st}',
            st,
            disc_by_state.get(st, 0),
            'included_trials',
            f'Included trials in cross-source disclosure discordance state {st}.',
        )
        for st in T.DISCORDANCE_STATES
    }

    coverage_overall_table = {
        st: m(
            f'coverage_overall:{st}',
            st,
            ov_by_state.get(st, 0),
            'included_trials',
            f'Included trials whose highest-precedence search coverage state is {st}.',
        )
        for st in C.STATES
    }

    coverage_by_route: dict[str, dict[str, dict[str, Any]]] = {}
    for route in C.ROUTES:
        rc = Counter(
            cell['state']
            for row in coverage_rows
            for cell in row['routes']
            if cell['route'] == route
        )
        coverage_by_route[route] = {
            st: m(
                f'coverage:{route}:{st}',
                f'{route} / {st}',
                rc.get(st, 0),
                'included_trials',
                f'Included trials with coverage state {st} on route {route}.',
            )
            for st in C.STATES
        }

    links_by_av = {
        av: m(
            f'automated_validation:{av}',
            av,
            av_counts.get(av, 0),
            'evidence_links',
            f'Evidence links with automated_validation={av}.',
        )
        for av in E.AUTOMATED_VALIDATION
    }

    links_by_rev = {
        rs: m(
            f'review_status:{rs}',
            rs,
            rev_counts.get(rs, 0),
            'evidence_links',
            f'Evidence links with human review_status={rs}.',
        )
        for rs in E.REVIEW_STATUSES
    }

    links_by_route = {
        route: m(
            f'links_route:{route}',
            route,
            route_link_counts.get(route, 0),
            'evidence_links',
            f'Evidence links produced on route {route}.',
        )
        for route in E.SOURCE_ROUTES
    }

    sponsor_literals_table = {
        st: m(
            f'sponsor_literals:{st}',
            st,
            int(lit_part[st]),
            'sponsor_literals',
            f'Distinct observed sponsor/collaborator literals in review state {st}.',
        )
        for st in T.SPONSOR_REVIEW_STATES
    }

    trial_lead_counts = sponsors_obj['partition_summary']['trials_by_lead_review_state']
    trials_by_lead_sponsor_state = {
        st: m(
            f'trials_lead_sponsor:{st}',
            st,
            int(trial_lead_counts.get(st, 0)),
            'included_trials',
            f'Included trials whose lead sponsor literal is in state {st}.',
        )
        for st in (*T.SPONSOR_REVIEW_STATES, 'study_not_retrieved')
    }

    return {
        'as_of': as_of_iso,
        'window_365d_note': T.WINDOW_365D_NOTE,
        'denominators': denominators,
        'metrics': metrics,
        'tables': {
            'window_365d': window_table,
            'discordance_states': discordance_table,
            'coverage_overall': coverage_overall_table,
            'coverage_by_route': coverage_by_route,
            'links_by_automated_validation': links_by_av,
            'links_by_review_status': links_by_rev,
            'links_by_route': links_by_route,
            'sponsor_literals_by_review_state': sponsor_literals_table,
            'trials_by_lead_sponsor_review_state': trials_by_lead_sponsor_state,
        },
    }


def run_identity(run: dict[str, Any], files: dict[str, Any]) -> str:
    """Deterministic 64-hex sha256 over run manifest (minus volatile keys) and sibling file digests."""
    manifest_body = {
        k: v for k, v in run.items()
        if k not in ('run_id', 'run_identity', 'generated_at', 'files')
    }
    sibling_digests = {
        name: sha256_bytes(dump({k: v for k, v in files[name].items() if k != 'run_id'}))
        for name in sorted(FILES)
    }
    return sha256_bytes(dump([manifest_body, sibling_digests]))


def generated_at_problem(value: Any) -> str | None:
    """Why `value` is not a whole ISO 8601 UTC timestamp, or None when it is.

    `generated_at` is published as the build's generation time, so the whole value is parsed, not a
    prefix: an impossible date, trailing text, a date without a time, a naive time or a non-zero UTC
    offset is refused (the rule the feasibility and DAP builds apply; see `A.timestamp_problem`).
    """
    return A.timestamp_problem(value, 'generated_at', offset='utc')


def generated_before_frozen_problem(generated_at: Any, frozen_at: Any) -> str | None:
    """Why a build stated as generated at `generated_at` cannot have read a cache frozen at `frozen_at`,
    or None.

    A build reads the frozen cache, so it is generated no earlier than the cache was frozen: an earlier
    `--generated-at` (a 2000 timestamp for a cache frozen in 2026) was published as impossible
    provenance. `freeze_cache` stamps `frozen_at` with the current UTC time unless a caller pins it, so
    a build that seeds its cache (`build --records`) and states a generation time must state one no
    earlier than the seeding. Both values must be whole UTC timestamps; one that does not parse is left
    to `generated_at_problem` and the manifest checks.
    """
    for value, label in ((generated_at, 'generated_at'), (frozen_at, 'frozen_at')):
        if A.timestamp_problem(value, label, offset='utc'):
            return None
    if datetime.fromisoformat(generated_at) < datetime.fromisoformat(frozen_at):
        return (
            f'generated_at {generated_at!r} is earlier than the cache was frozen ({frozen_at!r}); '
            'a build reads the frozen cache, so it cannot be generated before it'
        )
    return None


def build_artifacts(
    cache: A.FrozenCache,
    *,
    reviews_path: Path,
    statements_path: Path,
    sponsor_rules_path: Path,
    producer: dict[str, Any],
    generated_at: str,
    as_of: date | None = None,
    requested_nct_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build the complete schema-version-1 disclosure artifact family strictly from `FrozenCache`."""
    gen_problem = generated_at_problem(generated_at) or generated_before_frozen_problem(
        generated_at, cache.manifest.get('frozen_at')
    )
    if gen_problem:
        raise ValueError(gen_problem)
    reviews, reviews_sha = E.read_reviews(reviews_path)
    statements, statements_sha = E.read_statements(statements_path)
    # A build cannot incorporate a human decision made after it was generated: a review dated
    # 2100-01-01 in a 2026 build (a typo or a planted row) would change verified coverage and timeline
    # states while claiming a decision that had not happened. `reviewed_at` is a day, so it may equal the
    # generation day (UTC) but not follow it. Every row is checked, applied or not: the curated file is
    # pinned by its sha256 in run.json as a whole.
    late = sorted(
        (rev['reviewed_at'], lid) for lid, rev in reviews.items() if rev['reviewed_at'] > generated_at[:10]
    )
    if late:
        raise ValueError(
            f'{reviews_path.name}: {len(late)} review(s) dated after generated_at {generated_at!r} '
            f'(first: {late[0][1]} reviewed_at {late[0][0]}); a build cannot include a later decision'
        )

    cache_ids = set(cache.requested_nct_ids)
    if requested_nct_ids is not None:
        outside = sorted(set(requested_nct_ids) - cache_ids)
        if outside:
            raise ValueError(f'requested NCT IDs not present in frozen cache manifest: {outside[:5]}')
        included_ids = sorted(set(requested_nct_ids))
    else:
        included_ids = list(cache.requested_nct_ids)
    if not included_ids:
        raise ValueError('build_artifacts requires at least one included NCT ID')

    ver_info = _ctgov_version_info(cache.ctgov)
    as_of_bound = _as_of_bound(ver_info, cache.ctgov_meta)
    if as_of is not None:
        as_of_source = 'as_of_argument'
        # The frozen records show the registry only as of the CT.gov data they hold, so a stated
        # reference date may not be later than the earliest of it: a 2026 record with hasResults=false
        # says nothing about 2100. With a resumed cache the earliest data timestamp binds, since
        # records fetched on the first day reflect that day's data.
        if as_of_bound is not None and as_of > as_of_bound:
            raise ValueError(
                f'--as-of {as_of.isoformat()} is after the CT.gov data the cache holds (earliest '
                f'{as_of_bound.isoformat()}); the frozen records cannot show what was posted after that date'
            )
    elif ver_info['data_timestamp_first'] != ver_info['data_timestamp_last']:
        # Studies fetched on different days reflect different CT.gov data refreshes; no single date
        # describes them all, so the reference date for the 365-day window must be stated.
        raise ValueError(
            f"cache holds CT.gov data timestamps from {ver_info['data_timestamp_first']} to "
            f"{ver_info['data_timestamp_last']} ({len(ver_info['versions'])} version records); pass --as-of"
        )
    else:
        fallbacks = (
            ('ctgov_data_timestamp', (ver_info.get('data_timestamp') or '')[:10]),
            ('ctgov_last_fetched_at', (cache.ctgov_meta.get('last_fetched_at') or '')[:10]),
            ('generated_at', generated_at[:10]),
        )
        as_of_source, fallback_iso = next((src, iso) for src, iso in fallbacks if iso)
        as_of = date.fromisoformat(fallback_iso)

    studies_by_nct = A.studies_from_ctgov_records(cache.ctgov, included_ids)
    sponsor_ctx = T.load_sponsor_context(studies_by_nct, included_ids, sponsor_rules_path)

    # Assemble all candidate & verified evidence links across the 5 routes
    links: list[dict[str, Any]] = []
    for nct_id in included_ids:
        ctgov_rec = cache.ctgov.get(f'ctgov/study/{nct_id}')
        study = studies_by_nct.get(nct_id)
        sp_info = sponsor_ctx['per_trial'][nct_id]
        match_keys = E.trial_sponsor_match_keys(
            study,
            sp_info['any_canonicals_current_owner'],
            sp_info['any_entities_as_registered'],
        )
        links.extend(E.build_ctgov_results_links(nct_id, ctgov_rec))
        links.extend(E.build_registry_reference_links(nct_id, ctgov_rec, cache.literature))
        links.extend(E.build_literature_search_links(nct_id, cache.literature))
        links.extend(
            E.build_openfda_links(
                nct_id,
                study,
                cache.openfda,
                match_keys,
                rules_canonicals_by_norm=sponsor_ctx['rules_canonicals_by_norm'],
                trial_canonicals=set(sp_info['any_canonicals_current_owner']),
            )
        )

    links.extend(
        E.build_statement_links(
            statements,
            statements_sha,
            cache.ctgov,
            cache.literature,
            cache.openfda,
            included_nct_ids=set(included_ids),
        )
    )

    if len({x['link_id'] for x in links}) != len(links):
        dups = [lid for lid, cnt in Counter(x['link_id'] for x in links).items() if cnt > 1]
        raise ValueError(f'duplicate link_id collision: {dups[:5]}')

    # A review whose link_id matches no built link is an orphan only when its trial was built: its
    # evidence changed or vanished. A review for a trial outside this build (`--nct-list` subset, or
    # a trial not in the cache) says nothing about its evidence, so it is reported separately as out
    # of scope. n_rows = n_applied + len(orphaned_link_ids) + len(out_of_scope_link_ids).
    unmatched_reviews = E.apply_reviews(links, reviews)
    included_set = set(included_ids)
    orphaned_reviews = [lid for lid in unmatched_reviews if reviews[lid]['nct_id'] in included_set]
    out_of_scope_reviews = [lid for lid in unmatched_reviews if reviews[lid]['nct_id'] not in included_set]
    links.sort(key=lambda x: (x['nct_id'], x['source_route'], x['target_id'], x['link_id']))

    links_by_trial: dict[str, list[dict[str, Any]]] = {n: [] for n in included_ids}
    for lnk in links:
        links_by_trial[lnk['nct_id']].append(lnk)

    literature_acquired = cache.literature_meta is not None
    openfda_acquired = cache.openfda_meta is not None

    studies_list: list[dict[str, Any]] = []
    coverage_list: list[dict[str, Any]] = []
    timelines_by_nct: dict[str, dict[str, Any]] = {}

    for nct_id in included_ids:
        ctgov_rec = cache.ctgov.get(f'ctgov/study/{nct_id}')
        study = studies_by_nct.get(nct_id)
        t_links = links_by_trial[nct_id]
        by_route: dict[str, list[dict[str, Any]]] = {r: [] for r in C.ROUTES}
        for lnk in t_links:
            by_route[lnk['source_route']].append(lnk)

        cov = C.trial_coverage(
            nct_id,
            ctgov_rec=ctgov_rec,
            study=study,
            literature_records=cache.literature,
            literature_acquired=literature_acquired,
            openfda_records=cache.openfda,
            openfda_acquired=openfda_acquired,
            links_by_route=by_route,
        )
        tl = T.build_trial_timeline(
            nct_id,
            study,
            t_links,
            cov,
            sponsor_ctx['per_trial'][nct_id],
            as_of,
        )
        studies_list.append(_study_record(nct_id, ctgov_rec))
        coverage_list.append(cov)
        timelines_by_nct[nct_id] = tl

    timelines_list = [timelines_by_nct[n] for n in included_ids]
    sponsors_payload = T.build_sponsor_rollups(sponsor_ctx, included_ids, timelines_by_nct)
    summary_payload = summarise_artifacts(
        studies_list,
        links,
        coverage_list,
        timelines_list,
        sponsors_payload,
        as_of.isoformat(),
    )

    files: dict[str, Any] = {
        'summary.json': summary_payload,
        'studies.json': {'studies': studies_list},
        'links.json': {'links': links, 'orphaned_reviews': orphaned_reviews},
        'coverage.json': {
            'routes': C.ROUTES,
            'states': list(C.STATES),
            'overall_precedence': list(C.OVERALL_PRECEDENCE),
            'trials': coverage_list,
        },
        'timelines.json': {
            'as_of': as_of.isoformat(),
            'window_365d_note': T.WINDOW_365D_NOTE,
            'timelines': timelines_list,
        },
        'sponsors.json': sponsors_payload,
    }
    for name in FILES:
        files[name] = {'schema_version': SCHEMA_VERSION, **files[name]}

    excluded_ids = sorted(cache_ids - set(included_ids))
    run: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'method_version': METHOD_VERSION,
        'scheduled': False,
        'as_of': as_of.isoformat(),
        'as_of_source': as_of_source,
        'producer': producer,
        'counting_units': COUNTING_UNITS,
        'scope': {
            'n_cache_frame': len(cache.requested_nct_ids),
            'n_included': len(included_ids),
            'n_excluded_from_frame': len(excluded_ids),
            'included_nct_ids_sha256': sha256_bytes('\n'.join(included_ids).encode('utf-8')),
        },
        'inputs': {
            'cache_manifest_frozen_at': cache.manifest.get('frozen_at'),
            'requested_nct_ids_sha256': cache.manifest.get('requested_nct_ids_sha256'),
            'curated': {
                'reviews.csv': {
                    'file': reviews_path.name,
                    'sha256': reviews_sha,
                    'n_rows': len(reviews),
                },
                'documentary_statements.csv': {
                    'file': statements_path.name,
                    'sha256': statements_sha,
                    'n_rows': len(statements),
                },
                'company_aliases.csv': {
                    'file': sponsor_rules_path.name,
                    'sha256': sponsor_ctx['rules_sha256'],
                },
            },
        },
        'sources': {
            'ctgov': {**cache.ctgov_meta, **ver_info},
            'literature': cache.literature_meta,
            'openfda': cache.openfda_meta,
        },
        'reviews': {
            'n_rows': len(reviews),
            'n_applied': len(reviews) - len(orphaned_reviews) - len(out_of_scope_reviews),
            'orphaned_link_ids': orphaned_reviews,
            'out_of_scope_link_ids': out_of_scope_reviews,
        },
        'vocabularies': {
            'source_routes': list(E.SOURCE_ROUTES),
            'target_id_types': list(E.TARGET_ID_TYPES),
            'citation_types': list(E.CITATION_TYPES),
            'date_precisions': list(E.DATE_PRECISIONS),
            'milestone_precisions': list(T.MILESTONE_PRECISIONS),
            'automated_validation': list(E.AUTOMATED_VALIDATION),
            'review_statuses': list(E.REVIEW_STATUSES),
            'coverage_routes': list(C.ROUTES),
            'coverage_states': list(C.STATES),
            'coverage_overall_precedence': list(C.OVERALL_PRECEDENCE),
            'interval_states': list(T.INTERVAL_STATES),
            'window_states_365d': list(T.WINDOW_STATES_365D),
            'discordance_states': list(T.DISCORDANCE_STATES),
            'sponsor_review_states': list(T.SPONSOR_REVIEW_STATES),
        },
    }

    identity = run_identity(run, files)
    rid = f'disclosure-{as_of.isoformat()}-{identity[:10]}'
    for name in FILES:
        files[name] = {
            'schema_version': SCHEMA_VERSION,
            'run_id': rid,
            **{k: v for k, v in files[name].items() if k != 'schema_version'},
        }
    full_run: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'run_id': rid,
        'run_identity': identity,
        'generated_at': generated_at,
        **{k: v for k, v in run.items() if k != 'schema_version'},
    }
    full_run['files'] = {name: sha256_bytes(dump(files[name])) for name in sorted(FILES)}
    return {'run.json': full_run, **files}


# ---------------------------------------------------------------- contract checker

def _walk_keys_and_strings(obj: Any) -> tuple[set[str], list[str]]:
    keys: set[str] = set()
    strings: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k))
            sub_k, sub_s = _walk_keys_and_strings(v)
            keys |= sub_k
            strings.extend(sub_s)
    elif isinstance(obj, list):
        for v in obj:
            sub_k, sub_s = _walk_keys_and_strings(v)
            keys |= sub_k
            strings.extend(sub_s)
    elif isinstance(obj, str):
        strings.append(obj)
    return keys, strings


def _check_count_obj(
    obj: Any,
    denominators: dict[str, Any],
    where: str,
) -> list[str]:
    problems: list[str] = []
    if not isinstance(obj, dict):
        return [f'{where}: count entry is not an object']
    for required in ('value', 'numerator', 'denominator', 'definition'):
        if required not in obj:
            problems.append(f'{where}: missing required key {required!r}')
    if problems:
        return problems

    num = obj.get('numerator')
    val = obj.get('value')
    den_name = obj.get('denominator')
    defn = obj.get('definition')
    if isinstance(num, bool) or not isinstance(num, int):
        problems.append(f'{where}: numerator must be an int')
    if isinstance(val, bool) or not isinstance(val, int) or val != num:
        problems.append(f'{where}: value ({val!r}) must equal integer numerator ({num!r})')
    if not isinstance(defn, str) or not defn.strip():
        problems.append(f'{where}: definition must be a non-empty string')
    den_entry = denominators.get(str(den_name))
    if not isinstance(den_entry, dict) or not isinstance(den_entry.get('n'), int):
        problems.append(f'{where}: unknown denominator {den_name!r}')
    elif isinstance(num, int) and not isinstance(num, bool):
        if not (0 <= num <= den_entry['n']):
            problems.append(f"{where}: numerator {num} outside 0..{den_entry['n']}")
    return problems


def _as_of_bound(ver_info: dict[str, Any], ctgov_meta: dict[str, Any]) -> date | None:
    """Latest reference date the frozen CT.gov records can support, or None when the cache gives none.

    The earliest CT.gov `dataTimestamp` among the version records, else the earliest study fetch time.
    """
    for raw in (ver_info.get('data_timestamp_first'), (ctgov_meta or {}).get('first_fetched_at')):
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def _ctgov_version_problems(run: dict[str, Any], ctgov_src: dict[str, Any]) -> list[str]:
    """Check run.json's recorded CT.gov version records, data-timestamp range and `as_of` choice.

    The artifact family does not carry the CT.gov archive, so this checks that the recorded range
    agrees with the recorded version records (which `_ctgov_version_info` read from the archive),
    that the record count fits the archive's `n_records`, and that `as_of` was stated whenever the
    range spans more than one data timestamp.
    """
    problems: list[str] = []
    versions = ctgov_src.get('versions')
    if not isinstance(versions, list) or not all(isinstance(v, dict) for v in versions):
        return ['run.json sources.ctgov.versions must be a list of version records']
    stamps = [str(v.get('data_timestamp')) for v in versions]
    order = [(str(v.get('fetched_at') or ''), str(v.get('key') or '')) for v in versions]
    if order != sorted(order):
        problems.append('run.json sources.ctgov.versions are not ordered by fetched_at')
    first, last = ctgov_src.get('data_timestamp_first'), ctgov_src.get('data_timestamp_last')
    expected = (min(stamps), max(stamps), stamps[-1]) if stamps else (None, None, None)
    if (first, last, ctgov_src.get('data_timestamp')) != expected:
        problems.append('run.json sources.ctgov data_timestamp, _first or _last disagree with its version records')
    n_records = ctgov_src.get('n_records')
    if isinstance(n_records, int) and len(versions) > n_records:
        problems.append('run.json sources.ctgov lists more version records than its archive holds')
    as_of_source = run.get('as_of_source')
    if as_of_source not in AS_OF_SOURCES:
        problems.append(f'run.json as_of_source {as_of_source!r} is not one of {list(AS_OF_SOURCES)}')
    elif first != last and as_of_source != 'as_of_argument':
        problems.append('run.json as_of was defaulted although CT.gov data timestamps differ across version records')
    elif as_of_source == 'ctgov_data_timestamp' and str(run.get('as_of')) != str(ctgov_src.get('data_timestamp'))[:10]:
        problems.append('run.json as_of does not match the CT.gov data timestamp it was defaulted from')
    # Same bound as build_artifacts: as_of may not be later than the earliest CT.gov data timestamp.
    if isinstance(first, str) and len(first) >= 10 and isinstance(run.get('as_of'), str) and run['as_of'] > first[:10]:
        problems.append(f"run.json as_of {run['as_of']} is after the earliest CT.gov data timestamp {first[:10]}")
    return problems


def check_contract(
    files: dict[str, Any],
    *,
    raw_file_hashes: dict[str, str] | None = None,
) -> list[str]:
    """Validate all structural, provenance, domain, and recount invariants across the family."""
    problems: list[str] = []
    run = files.get('run.json')
    if not isinstance(run, dict):
        return ['run.json is missing or not an object']

    if run.get('schema_version') != SCHEMA_VERSION:
        problems.append(f"run.json schema_version {run.get('schema_version')!r} != {SCHEMA_VERSION}")
    if run.get('scheduled') is not False:
        problems.append('run.json must state scheduled: false')
    rid = run.get('run_id')
    if not isinstance(rid, str) or not RUN_ID_RE.match(rid):
        problems.append(f'run.json run_id {rid!r} is malformed')
    # generated_at sits outside run_identity and run.json outside its own file hashes, so an edited
    # value would pass every other check; it is parsed whole again here.
    # The same holds for generation before the cache it read (`generated_before_frozen_problem`).
    inputs = run.get('inputs')
    frozen_at = inputs.get('cache_manifest_frozen_at') if isinstance(inputs, dict) else None
    gen_problem = generated_at_problem(run.get('generated_at')) or generated_before_frozen_problem(
        run.get('generated_at'), frozen_at
    )
    if gen_problem:
        problems.append(f'run.json {gen_problem}')
    rident = run.get('run_identity')
    if not isinstance(rident, str) or not HEX64_RE.match(rident):
        problems.append(f'run.json run_identity {rident!r} must be a 64-hex sha256')

    listed = run.get('files')
    if not isinstance(listed, dict):
        return problems + ['run.json lacks `files` checksum map']

    present = set(files) - {'run.json'}
    for path in sorted(set(listed) ^ present):
        problems.append(
            f'{path}: listed in run.json but absent' if path in listed else f'{path}: present on disk but not in run.json'
        )
    if set(FILES) - present:
        return problems + [f'missing required artifact file(s): {sorted(set(FILES) - present)}']

    for name in sorted(set(listed) & present):
        sibling = files[name]
        if not isinstance(sibling, dict):
            problems.append(f'{name}: root JSON value is not an object')
            continue
        expected_sha = listed[name]
        actual_dump_sha = sha256_bytes(dump(sibling))
        if actual_dump_sha != expected_sha:
            problems.append(f'{name}: canonical sha256 does not match run.json')
        if raw_file_hashes is not None and raw_file_hashes.get(name) != expected_sha:
            problems.append(f'{name}: on-disk file bytes do not match sha256 in run.json')
        if sibling.get('run_id') != rid or sibling.get('schema_version') != SCHEMA_VERSION:
            problems.append(f'{name}: run_id or schema_version disagrees with run.json')

    expected_identity = run_identity(run, files)
    if isinstance(rident, str) and rident != expected_identity:
        problems.append('run.json run_identity does not match recomputed content/input digest')
    if isinstance(rid, str) and isinstance(rident, str) and rid[-10:] != expected_identity[:10]:
        problems.append('run.json run_id suffix does not match run_identity[:10]')

    # Provenance & curated sha256 pins
    prod = run.get('producer') or {}
    if not HEX64_RE.match(str(prod.get('source_sha256') or '')):
        problems.append('run.json producer.source_sha256 is missing or invalid')
    # Git provenance is a 40-hex commit with a bool dirty flag, or unknown (both null); never false
    # without a commit. The site exporter additionally refuses the unknown case.
    commit, dirty = prod.get('commit'), prod.get('uncommitted_changes')
    if commit is not None and not GIT_COMMIT_RE.match(str(commit)):
        problems.append(f'run.json producer.commit {commit!r} must be a 40-hex commit or null')
    if dirty is not None and not isinstance(dirty, bool):
        problems.append(f'run.json producer.uncommitted_changes {dirty!r} must be true, false or null')
    if commit is None and dirty is not None:
        problems.append('run.json producer.uncommitted_changes must be null when producer.commit is null')
    curated = ((run.get('inputs') or {}).get('curated') or {})
    for cname in ('reviews.csv', 'documentary_statements.csv', 'company_aliases.csv'):
        if not HEX64_RE.match(str((curated.get(cname) or {}).get('sha256') or '')):
            problems.append(f'run.json inputs.curated.{cname} lacks a 64-hex sha256')
    ctgov_src = ((run.get('sources') or {}).get('ctgov') or {})
    if not HEX64_RE.match(str(ctgov_src.get('sha256_uncompressed') or '')):
        problems.append('run.json sources.ctgov is not pinned by sha256_uncompressed')
    problems.extend(_ctgov_version_problems(run, ctgov_src))

    # Private key leak & statutory compliance language check
    all_keys, all_strings = _walk_keys_and_strings(files)
    leaked = sorted(all_keys & PRIVATE_KEYS)
    if leaked:
        problems.append(f'private review keys leaked into artifacts: {leaked}')
    for s in all_strings:
        if FORBIDDEN_COMPLIANCE_RE.search(s):
            problems.append(f'forbidden statutory non-compliance claim in artifact text: {s[:120]!r}')
            break

    studies = files['studies.json'].get('studies') or []
    links = files['links.json'].get('links') or []
    cov_rows = files['coverage.json'].get('trials') or []
    timelines = files['timelines.json'].get('timelines') or []
    sponsors_obj = files['sponsors.json']
    summary = files['summary.json']

    study_ids = [s.get('nct_id') for s in studies if isinstance(s, dict)]
    cov_ids = [c.get('nct_id') for c in cov_rows if isinstance(c, dict)]
    tl_ids = [t.get('nct_id') for t in timelines if isinstance(t, dict)]
    if len(set(study_ids)) != len(studies) or study_ids != sorted(study_ids):
        problems.append('studies.json must hold distinct sorted NCT IDs')
    if cov_ids != study_ids or tl_ids != study_ids:
        problems.append('studies.json, coverage.json, and timelines.json disagree on included NCT IDs')
    if len(studies) != ((run.get('scope') or {}).get('n_included')):
        problems.append('run.json scope.n_included differs from studies.json length')

    # Links invariants
    link_ids = [x.get('link_id') for x in links if isinstance(x, dict)]
    if len(set(link_ids)) != len(links):
        problems.append('links.json repeats a link_id')
    known_studies = set(study_ids)
    n_reviewed = 0
    links_by_trial_route: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for x in links:
        if not isinstance(x, dict):
            problems.append('links.json contains a non-object entry')
            continue
        problems.extend(E.link_problems(x))
        review = x.get('review')
        reviewed_at = review.get('reviewed_at') if isinstance(review, dict) else None
        generated = run.get('generated_at')
        if isinstance(reviewed_at, str) and isinstance(generated, str) and reviewed_at > generated[:10]:
            problems.append(
                f"{x.get('link_id')}: review dated {reviewed_at}, after generated_at {generated!r}"
            )
        if x.get('nct_id') not in known_studies:
            problems.append(f"{x.get('link_id')}: nct_id {x.get('nct_id')!r} is not in studies.json")
        if x.get('review_status') != 'unreviewed':
            n_reviewed += 1
        links_by_trial_route.setdefault((str(x.get('nct_id')), str(x.get('source_route'))), []).append(x)
    run_reviews = run.get('reviews') or {}
    if n_reviewed != run_reviews.get('n_applied'):
        problems.append('number of reviewed links differs from run.json reviews.n_applied')
    orphaned_ids = run_reviews.get('orphaned_link_ids')
    out_of_scope_ids = run_reviews.get('out_of_scope_link_ids')
    if not isinstance(orphaned_ids, list) or not isinstance(out_of_scope_ids, list):
        problems.append('run.json reviews must list orphaned_link_ids and out_of_scope_link_ids')
    else:
        if set(orphaned_ids) & set(out_of_scope_ids):
            problems.append('run.json reviews lists a link_id as both orphaned and out of scope')
        if set(orphaned_ids + out_of_scope_ids) & set(link_ids):
            problems.append('run.json reviews lists an applied link_id as orphaned or out of scope')
        n_applied = run_reviews.get('n_applied')
        if not isinstance(n_applied, int) or run_reviews.get('n_rows') != n_applied + len(orphaned_ids) + len(out_of_scope_ids):
            problems.append('run.json reviews.n_rows != n_applied + orphaned + out of scope')

    # Coverage invariants
    for cov in cov_rows:
        nct = str(cov.get('nct_id'))
        cells = cov.get('routes') or []
        if [c.get('route') for c in cells] != list(C.ROUTES):
            problems.append(f'{nct}: coverage routes do not match {list(C.ROUTES)} in order')
            continue
        for cell in cells:
            st = cell.get('state')
            rname = str(cell.get('route'))
            if st not in C.STATES or not str(cell.get('reason') or '').strip():
                problems.append(f'{nct}/{rname}: invalid state {st!r} or empty reason')
            rl = links_by_trial_route.get((nct, rname), [])
            if st == 'verified_link':
                if rname == 'documentary_statements':
                    ok = any(
                        x['automated_validation'] == 'quote_confirmed_in_source'
                        and x['review_status'] == 'confirmed_same_trial_results'
                        for x in rl
                    )
                elif rname == 'ctgov_results':
                    # The registry's own posted resultsSection: a rejecting review does not unverify it.
                    ok = any(
                        x['automated_validation'] == 'identifier_confirmed' and x['review_status'] != 'indeterminate'
                        for x in rl
                    )
                else:
                    # The rule coverage itself applies (`coverage._verified_links` over the links
                    # `coverage._active_links` keeps live), so the two cannot drift apart: an
                    # identifier-confirmed link not held indeterminate, or a link a person reviewed as
                    # `confirmed_same_trial_results` whatever the mechanical check said.
                    live = C._active_links(
                        rl, treat_unreviewed_background_as_insufficient=(rname == 'registry_references')
                    )
                    ok = bool(C._verified_links(live))
                if not ok:
                    problems.append(f'{nct}/{rname}: verified_link without a non-rejected confirmed link')
            if rname == 'literature_search' and run.get('sources', {}).get('literature') is None and st != 'not_searched':
                problems.append(f'{nct}/literature_search: must be not_searched when literature archive is absent')
            if rname == 'openfda_drugsfda' and run.get('sources', {}).get('openfda') is None and st != 'not_searched':
                problems.append(f'{nct}/openfda_drugsfda: must be not_searched when openfda archive is absent')
        if cov.get('overall') != C.overall_coverage(cells):
            problems.append(f'{nct}: coverage overall state does not match overall_coverage(routes)')

    cov_map = {str(c.get('nct_id')): c for c in cov_rows if isinstance(c, dict)}
    links_by_nct: dict[str, list[dict[str, Any]]] = {}
    for x in links:
        if isinstance(x, dict):
            links_by_nct.setdefault(str(x.get('nct_id')), []).append(x)

    # Timeline invariants
    has_results_by_nct = {str(s.get('nct_id')): s.get('has_results') for s in studies if isinstance(s, dict)}
    for tl in timelines:
        nct = str(tl.get('nct_id'))
        wstate = (tl.get('window_365d') or {}).get('state')
        dstate = (tl.get('discordance') or {}).get('state')
        # Only a record with an explicit hasResults=false establishes that CT.gov is unreported: true never
        # does, whatever the reviews say, and a missing or non-boolean value (null in studies.json) is
        # insufficient evidence.
        if has_results_by_nct.get(nct) is not False and dstate in T.CTGOV_UNREPORTED_DISCORDANCE:
            problems.append(
                f'{nct}: CT.gov record has hasResults={json.dumps(has_results_by_nct.get(nct))}, '
                f'but discordance is {dstate!r}'
            )
        if wstate not in T.WINDOW_STATES_365D:
            problems.append(f'{nct}: invalid window_365d state {wstate!r}')
        elif wstate in ('PENDING_SUBMITTED_UNPOSTED', 'UNREPORTED_PAST_365D', 'UNREPORTED_WITHIN_365D'):
            # Unposted on `as_of` needs evidence: hasResults=false, or a results-first-posted date
            # (necessarily after `as_of`, else the window would be POSTED_*). "Submitted, not yet
            # posted" states unposted status as much as the unreported states do.
            posted_later = (((tl.get('milestones') or {}).get('results_first_posted') or {}).get('precision')
                            not in (None, 'missing'))
            if has_results_by_nct.get(nct) is not False and not posted_later:
                problems.append(
                    f'{nct}: window_365d is {wstate} but CT.gov hasResults is '
                    f'{json.dumps(has_results_by_nct.get(nct))} and no results-first-posted date is recorded'
                )
        if dstate not in T.DISCORDANCE_STATES:
            problems.append(f'{nct}: invalid discordance state {dstate!r}')
        elif nct in cov_map and len((cov_map[nct].get('routes') or [])) == len(C.ROUTES):
            expected_disc = T.classify_discordance(links_by_nct.get(nct, []), cov_map[nct])
            if tl.get('discordance') != expected_disc:
                problems.append(f'{nct}: timeline discordance disagrees with links.json and coverage.json')
        for mname, ms in (tl.get('milestones') or {}).items():
            mprec = ms.get('precision')
            if mprec not in T.MILESTONE_PRECISIONS:
                problems.append(f'{nct}/{mname}: invalid milestone precision {mprec!r}')
                continue
            # An undated qualifying link may precede every dated one: such a milestone must carry no exact
            # date and no lower bound, only `latest_bound`, under precision `bounded_above_only`.
            if mprec == 'bounded_above_only':
                if ms.get('date') is not None or ms.get('earliest_bound') is not None or not ms.get('latest_bound'):
                    problems.append(f'{nct}/{mname}: bounded_above_only requires date=None, earliest_bound=None and a latest_bound')
                if ms.get('has_undated_qualifying_link') is not True:
                    problems.append(f'{nct}/{mname}: bounded_above_only without an undated qualifying link')
            elif ms.get('has_undated_qualifying_link') is True and ms.get('latest_bound') is not None:
                problems.append(f'{nct}/{mname}: an undated qualifying link leaves no lower bound, but precision is {mprec!r}')
        for iname, iv in (tl.get('intervals') or {}).items():
            istate = iv.get('state')
            if istate not in T.INTERVAL_STATES:
                problems.append(f'{nct}/{iname}: invalid interval state {istate!r}')
                continue
            if istate in ('exact', 'bounded_partial') and 'bounded_above_only' in (
                iv.get('start_precision'), iv.get('end_precision')
            ):
                problems.append(f'{nct}/{iname}: lag computed from a bounded_above_only endpoint with no lower bound')
            if istate == 'exact':
                if iv.get('start_precision') != 'day' or iv.get('end_precision') != 'day' or not isinstance(iv.get('exact_days'), int):
                    problems.append(f'{nct}/{iname}: exact interval requires both endpoints at day precision')
            elif istate == 'bounded_partial':
                if iv.get('exact_days') is not None:
                    problems.append(f'{nct}/{iname}: partial-date interval must never invent exact_days')
                bounds = iv.get('bounds_days')
                if not (isinstance(bounds, list) and len(bounds) == 2 and bounds[0] <= bounds[1] and (bounds[1] < 0 or bounds[0] > 0)):
                    problems.append(f'{nct}/{iname}: bounded_partial requires non-overlapping [min_days, max_days]')
            else:
                if iv.get('exact_days') is not None or iv.get('bounds_days') is not None:
                    problems.append(f'{nct}/{iname}: {istate} must have exact_days=None and bounds_days=None')

    # Sponsor 3-state separation invariants
    part = ((sponsors_obj.get('partition_summary') or {}).get('literals') or {})
    if set(part) != set(T.SPONSOR_REVIEW_STATES):
        problems.append('sponsors.json partition_summary.literals must keep attributed, reviewed_excluded, unreviewed separate')
    excl_lits = {r.get('literal_name') for r in sponsors_obj.get('reviewed_excluded_literals') or []}
    unrev_lits = {r.get('literal_name') for r in sponsors_obj.get('unreviewed_literals') or []}
    if excl_lits & unrev_lits:
        problems.append('sponsors.json collapses or overlaps reviewed_excluded and unreviewed literals')

    # Summary count-object structure + exact recount parity
    den = summary.get('denominators') or {}
    for dname, dval in den.items():
        if not isinstance(dval, dict) or isinstance(dval.get('n'), bool) or not isinstance(dval.get('n'), int) or not str(dval.get('definition') or '').strip():
            problems.append(f'summary.json denominator {dname!r} lacks integer n or definition')
    seen_metric_ids: set[str] = set()
    for m_obj in summary.get('metrics') or []:
        mid = str((m_obj or {}).get('id') or '?')
        if mid in seen_metric_ids:
            problems.append(f'summary.json repeats metric id {mid!r}')
        seen_metric_ids.add(mid)
        problems.extend(_check_count_obj(m_obj, den, f'summary.metrics[{mid}]'))

    for tname, table in (summary.get('tables') or {}).items():
        if not isinstance(table, dict):
            problems.append(f'summary.tables.{tname} is not an object')
            continue
        for k1, v1 in table.items():
            if isinstance(v1, dict) and 'numerator' not in v1 and tname == 'coverage_by_route':
                for k2, v2 in v1.items():
                    problems.extend(_check_count_obj(v2, den, f'summary.tables.{tname}.{k1}.{k2}'))
            else:
                problems.extend(_check_count_obj(v1, den, f'summary.tables.{tname}.{k1}'))

    if not problems:
        recounted = summarise_artifacts(
            studies,
            links,
            cov_rows,
            timelines,
            {k: v for k, v in sponsors_obj.items() if k not in ('schema_version', 'run_id')},
            str(run.get('as_of')),
        )
        actual_summary = {k: v for k, v in summary.items() if k not in ('schema_version', 'run_id')}
        if actual_summary != recounted:
            problems.append('summary.json counts do not match recounted values from sibling artifact files')

    return problems


# ---------------------------------------------------------------- atomic staging & loading

def replace_refusal(out: Path, expected_names: set[str], *, run_prefix: str = 'disclosure-') -> str | None:
    """Return a refusal reason if `out` exists and is not an empty dir or complete disclosure family."""
    if not out.exists() and not out.is_symlink():
        return None
    if out.is_symlink() or not out.is_dir():
        return f'{out} is not a regular directory'
    entries = list(out.iterdir())
    if not entries:
        return None
    unexpected = sorted(
        p.name for p in entries
        if p.is_symlink() or not p.is_file() or p.name not in expected_names
    )
    if unexpected:
        return f'{out} holds unexpected entries ({unexpected[:5]})'
    missing = sorted(expected_names - {p.name for p in entries})
    if missing:
        return f'{out} lacks expected family files ({missing[:5]})'
    try:
        existing_run = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return f'{out}/run.json is not readable JSON'
    existing_id = existing_run.get('run_id') if isinstance(existing_run, dict) else None
    if not (isinstance(existing_id, str) and existing_id.startswith(run_prefix)):
        return f'{out}/run.json does not name a {run_prefix} family'
    return None


def write_artifacts(files: dict[str, Any], out: Path) -> None:
    """Atomically stage and swap the verified disclosure artifact family into `out`."""
    expected = set(FILES) | {'run.json'}
    refusal = replace_refusal(out, expected)
    if refusal:
        raise ValueError(f'{refusal}; refusing to replace it')

    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out.name + '.staging.', dir=out.parent))
    try:
        for name in FILES:
            (stage / name).write_bytes(dump(files[name]))
        run_text = json.dumps(files['run.json'], ensure_ascii=False, indent=1, sort_keys=True) + '\n'
        (stage / 'run.json').write_text(run_text, encoding='utf-8')

        old: Path | None = None
        if out.exists():
            old = Path(tempfile.mkdtemp(prefix=out.name + '.replaced.', dir=out.parent))
            out.rename(old / out.name)
        try:
            stage.rename(out)
        except OSError:
            if old is not None and (old / out.name).exists():
                (old / out.name).rename(out)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def load_dir(out: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Load all JSON files in `out` along with raw on-disk byte sha256 digests."""
    if not out.is_dir():
        raise ValueError(f'{out}: not a directory')
    files: dict[str, Any] = {}
    raw_hashes: dict[str, str] = {}
    for path in sorted(out.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'{path.name}: unexpected non-regular file in artifact directory')
        if path.suffix != '.json':
            raise ValueError(f'{path.name}: unexpected non-JSON file in artifact directory')
        raw = path.read_bytes()
        raw_hashes[path.name] = sha256_bytes(raw)
        files[path.name] = json.loads(raw.decode('utf-8'))
    return files, raw_hashes
