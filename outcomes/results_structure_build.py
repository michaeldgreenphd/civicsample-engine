"""The results-structure artifact family (schema version 1) and its acquisition archive.

  run.json       manifest: run id, generated_at, producer commit, method version, the archive's
                 sha256, retrieval window, registry API version and dataTimestamp, parameters,
                 the fields the subgroup rule inspected and those it did not, and the sha256 of
                 the other two files
  summary.json   every count with its numerator, a named denominator and a definition; retrieval
                 failures, parsing failures, missing data and negative findings as four separate
                 tables. The browser computes no aggregate.
  records.json   one record per requested NCT id (outcomes/results_structure.py), including the
                 trials the registry did not return and the records that could not be parsed

Scope. The archive holds every record that was fetched (the frame). A build may cover fewer: the
requested list names the trials it covers, and an exclude list names the frame records it leaves
out, with one stated reason. run.json and summary.json both carry the three counts (frame,
included, excluded) and the reason, so a reader never has to work out why a count's denominator
is smaller than the number of records fetched. A frame record that is neither requested nor
excluded stops the build when an exclude list is given.

Built by hand with scripts/build_results_structure.py; no workflow calls it. The conventions are
those of outcomes/artifacts.py: canonical bytes from `dump`, a manifest that lists each file's
sha256, a producer-side `check` run before anything is written, and an atomic swap on write.

Acquisition. `fetch` asks the ClinicalTrials.gov API v2 for the full record of each listed NCT id
(no `fields` parameter, so the results section is included; outcomes/acquire.py fetches the
protocol and document sections only). One request at a time, at least `delay` seconds apart. The
result is frozen as a sorted, byte-stable jsonl.gz in the encoding of `acquire.encode`, with the
sha256 of the uncompressed bytes and a metadata file beside it. Records are held and processed one
at a time as bytes, not as one list of parsed objects: a full Phase 3 record with results is about
200 kB of JSON.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import acquire  # noqa: E402
from outcomes import results_structure as RS  # noqa: E402

SCHEMA_VERSION = 1
API = 'https://clinicaltrials.gov/api/v2'
BATCH = 25            # NCT ids per request; a response of 25 full records is a few megabytes
MIN_DELAY = 0.5       # seconds between requests: one at a time, at most two a second
MAX_TRIES = 5
RUN_ID = re.compile(r'^results-structure-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$')
NCT = re.compile(r'^NCT\d{8}$')
# Keys that belong to the private review file and must not reach a published artifact.
PRIVATE_KEYS = {'reviewer', 'note'}


class BuildError(Exception):
    pass


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    """Canonical artifact bytes, as outcomes/artifacts.py writes them: sorted keys, no spaces."""
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode()


def read_nct_list(path: Path) -> list[str]:
    """NCT ids from a text file, one per line; blank lines and lines starting with '#' are skipped.
    Raises on anything that is not an NCT id and on a repeated id, so the requested list is exactly
    the denominator the summary names."""
    ids = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith('#')]
    bad = [i for i in ids if not NCT.match(i)]
    if bad:
        raise BuildError(f'{path.name}: not NCT ids: {bad[:5]}')
    if len(set(ids)) != len(ids):
        raise BuildError(f'{path.name}: repeated NCT ids')
    return sorted(ids)


# ---------------------------------------------------------------- acquisition

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fetch(ids: list[str], get: Callable[..., Any] | None = None, delay: float = MIN_DELAY,
          sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Full records for the listed NCT ids. Returns ({nct_id: canonical line}, retrieval record).

    One request at a time, `delay` seconds apart (never below MIN_DELAY). A request that fails
    with 429 or a 5xx status, or with a network error, is retried with a doubling wait up to
    MAX_TRIES; a batch that still fails is recorded with its error and its ids are reported as not
    retrieved. An id the registry does not return is recorded too. Nothing is skipped silently.
    The registry's API version and dataTimestamp are read from /version with one more request."""
    delay = max(delay, MIN_DELAY)
    session = requests.Session()
    session.headers['User-Agent'] = 'civicsample-results-structure'
    get = get or (lambda url, params: session.get(url, params=params, timeout=120))
    lines: dict[str, bytes] = {}
    log: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    last = [0.0]

    def request(url: str, params: dict[str, Any], purpose: str) -> Any | None:
        """One logged request with retries; the parsed body, or None after the last failure."""
        error = None
        for attempt in range(MAX_TRIES):
            wait = delay - (time.monotonic() - last[0])
            if wait > 0:
                sleep(wait)
            entry: dict[str, Any] = {'purpose': purpose, 'requested_at': _now(), 'attempt': attempt + 1}
            log.append(entry)
            try:
                r = get(url, params)
            except requests.RequestException as exc:
                last[0] = time.monotonic()
                error = f'{type(exc).__name__}: {exc}'
                entry.update(status=None, error=error)
            else:
                last[0] = time.monotonic()
                entry.update(status=r.status_code, bytes=len(r.content), sha256=sha256(r.content))
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError as exc:
                        error = f'the response is not JSON: {exc}'
                        entry['error'] = error
                else:
                    error = f'HTTP {r.status_code}'
                    entry['error'] = error
                    if r.status_code not in (429, 500, 502, 503, 504):
                        break
            if attempt + 1 < MAX_TRIES:
                sleep(min(60, 2 ** attempt * 2))
        log[-1]['gave_up'] = error
        return None

    started = _now()
    version = request(f'{API}/version', {}, 'version') or {}
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        token, pages = None, 0
        while True:
            # pageSize above the batch size, so one page holds the whole batch; a page token is
            # followed all the same in case the registry splits the response.
            params = {'filter.ids': ','.join(batch), 'pageSize': 100, 'format': 'json'}
            if token:
                params['pageToken'] = token
            body = request(f'{API}/studies', params, f'studies {batch[0]}..{batch[-1]} ({len(batch)} ids)')
            if body is None:
                for n in batch:
                    if n not in lines:
                        failed[n] = f"request failed: {log[-1].get('gave_up')}"
                break
            for s in body.get('studies') or []:
                n = RS.nct_id_of(s)
                if n in batch:
                    lines[n] = acquire.encode([s])
                else:
                    failed[str(n)] = 'returned by the registry but not requested'
            token = body.get('nextPageToken')
            pages += 1
            if not token or pages >= 5:
                break
        for n in batch:
            if n not in lines and n not in failed:
                failed[n] = 'not returned by the registry for filter.ids'
    studies_requests = [e for e in log if e['purpose'] != 'version']
    record = {
        'source': 'clinicaltrials.gov API v2', 'endpoint': f'{API}/studies',
        'request': f'filter.ids in batches of {BATCH}, no fields parameter (full record), format=json',
        'retrieved_from': started, 'retrieved_to': _now(), 'retrieved_at': started,
        'api_version': version.get('apiVersion'), 'data_timestamp': version.get('dataTimestamp'),
        'n_requested': len(ids), 'n_records': len(lines),
        'not_retrieved': [{'nct_id': n, 'reason': r} for n, r in sorted(failed.items())],
        'n_requests': len(log), 'n_studies_requests': len(studies_requests),
        'n_retries': sum(1 for e in log if e['attempt'] > 1),
        'n_failed_requests': sum(1 for e in log if 'error' in e),
        'min_seconds_between_requests': delay, 'requests': log}
    return lines, record


def write_archive(lines: dict[str, bytes], path: Path, record: dict[str, Any], requested: list[str]) -> dict[str, Any]:
    """Freeze fetched records: one sorted-key JSON object per line, by NCT id, gzip with mtime 0
    so the same records give the same bytes. The metadata beside it holds the sha256 of the
    uncompressed bytes, the requested ids and the retrieval record."""
    raw = b''.join(lines[n] for n in sorted(lines))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, mtime=0))
    meta = {**record, 'requested_ids': sorted(requested), 'requested_ids_sha256': sha256('\n'.join(sorted(requested)).encode()),
            'sha256_uncompressed': sha256(raw), 'archive': path.name}
    path.with_name(path.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def read_archive_meta(path: Path) -> dict[str, Any]:
    """The archive's metadata, after checking that the bytes are the ones it records. An archive
    with no metadata file is refused: its retrieval time and requested ids would be unknown."""
    meta_path = path.with_name(path.name + '.meta.json')
    if not meta_path.exists():
        raise BuildError(f'{path.name}: no metadata file beside the archive')
    meta = json.loads(meta_path.read_text())
    digest, n = hashlib.sha256(), 0
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            digest.update(line)
            n += bool(line.strip())
    if digest.hexdigest() != meta.get('sha256_uncompressed'):
        raise BuildError(f'{path.name}: bytes do not match the recorded sha256')
    if n != meta.get('n_records'):
        raise BuildError(f'{path.name}: record count does not match the metadata')
    return meta


def iter_archive(path: Path) -> Iterator[Any]:
    """Study records from a frozen archive, one at a time. A line that is not JSON is yielded as
    the exception it raised, so the caller records it as a parsing failure instead of stopping."""
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    yield exc


# ---------------------------------------------------------------- records, summary, manifest

def build_scope(frame: list[str], requested: list[str], excluded: list[str] | None = None,
                reason: str | None = None) -> dict[str, Any]:
    """Which of the fetched records a build covers: the frame (every id the archive was fetched
    for), the included trials (the requested list, one record each) and the excluded frame records
    with the one reason they are left out.

    With an exclude list, every frame id must be either requested or excluded, an id may not be
    both, an excluded id must be in the frame, and the reason must be stated; anything else raises,
    so no fetched record leaves the build without a recorded reason. A requested id that is not in
    the frame is allowed: it has no record in the archive, and build_records gives it a
    not_yet_processed record with that reason.

    Without an exclude list nothing may be left out: a frame id that is not requested would have
    no record and no stated reason while the scope said that nothing was excluded, so that
    raises too. The build then covers the whole frame or names what it leaves out and why."""
    frame_set, want, out = set(frame), set(requested), sorted(excluded or [])
    if excluded is not None:
        if not (reason or '').strip():
            raise BuildError('an exclude list needs a stated reason')
        if len(set(out)) != len(out):
            raise BuildError('repeated ids in the exclude list')
        if want & set(out):
            raise BuildError(f'ids both requested and excluded: {sorted(want & set(out))[:5]}')
        if set(out) - frame_set:
            raise BuildError(f'excluded ids that are not in the frame: {sorted(set(out) - frame_set)[:5]}')
        left = frame_set - want - set(out)
        if left:
            raise BuildError(f'{len(left)} frame records are neither requested nor excluded: {sorted(left)[:5]}')
    elif frame_set - want:
        left = frame_set - want
        raise BuildError(f'{len(left)} frame records are not requested and no exclude list states why: '
                         f'{sorted(left)[:5]}; give --exclude-list and --exclude-reason')
    return {'frame': {'n': len(frame_set), 'definition': 'Registry records the archive was fetched for.'},
            'included': {'n': len(want), 'definition': 'Trials this build covers: the requested list, one record each in records.json.'},
            'excluded': {'n': len(out), 'reason': reason.strip() if out and reason else None, 'nct_ids': out,
                         'definition': 'Frame records left out of this build. They have no record in records.json and are in no count.'}}


def build_records(studies: Iterable[Any], requested: list[str], not_retrieved: dict[str, str],
                  reviews: dict[str, dict[tuple[str, str], dict[str, str]]],
                  excluded: frozenset[str] = frozenset()) -> tuple[dict[str, Any], dict[str, Any]]:
    """One record per requested NCT id. Returns (records by id, problems).

    A requested id with no record in the archive gets a not-retrieved record (its results were not
    read). An archive line that is not JSON, or has no NCT id, cannot be keyed to a trial and is
    reported in problems['unkeyed_lines']; the trial it was for then shows as not retrieved with
    that reason. A record for an id that was not requested is reported and left out; a record
    whose id is in `excluded` (build_scope) is left out without a report, because the scope
    already states it and its reason. Reviews of
    trials outside the requested list, or of signals no current record has, are reported in
    problems['reviews_not_matched'] and applied to nothing."""
    records: dict[str, Any] = {}
    problems: dict[str, Any] = {'unkeyed_lines': [], 'unrequested_records': [], 'reviews_not_matched': []}
    wanted = set(requested)
    for i, s in enumerate(studies):
        if isinstance(s, Exception):
            problems['unkeyed_lines'].append({'line': i, 'reason': f'{type(s).__name__}: {s}'})
            continue
        rec = RS.study_record(s)
        n = rec['nct_id']
        if n is None:
            problems['unkeyed_lines'].append({'line': i, 'reason': 'no NCT id in protocolSection.identificationModule'})
        elif n in excluded:
            continue
        elif n not in wanted:
            problems['unrequested_records'].append(n)
        elif n in records:
            raise BuildError(f'{n}: more than one record in the archive')
        else:
            unmatched = RS.apply_reviews(rec, reviews.get(n))
            # A record with no signals at all (no posted results, or measures that could not be
            # parsed) reports its reviews once, below, with that reason; not here as well.
            if rec['subgroup']['signals'] is not None:
                for key in unmatched:
                    problems['reviews_not_matched'].append({'nct_id': n, 'review': key, 'reason': 'no such signal in the current record'})
            records[n] = rec
    for n in requested:
        if n not in records:
            reason = not_retrieved.get(n) or ('archive line could not be read' if problems['unkeyed_lines']
                                              else 'not in the archive')
            records[n] = RS.not_retrieved_record(n, reason)
    for n in sorted(set(reviews) - wanted):
        for sid, dim in sorted(reviews[n]):
            problems['reviews_not_matched'].append({'nct_id': n, 'review': f'{sid}/{dim}', 'reason': 'trial not in the requested list'})
    for n, rows in reviews.items():
        if n in records and records[n]['subgroup']['signals'] is None:
            for sid, dim in sorted(rows):
                problems['reviews_not_matched'].append({'nct_id': n, 'review': f'{sid}/{dim}', 'reason': 'the trial has no signals to review in this build'})
    return dict(sorted(records.items())), problems


def metric(mid: str, label: str, numerator: int, denominator: str, definition: str) -> dict[str, Any]:
    return {'id': mid, 'label': label, 'numerator': numerator, 'denominator': denominator, 'definition': definition}


STATE_DEFINITIONS = {
    RS.NOT_APPLICABLE: 'The registry holds no posted results for the trial (hasResults is false). This describes the registry, not this method.',
    RS.NOT_YET_PROCESSED: 'The trial was requested but its record was not retrieved, so its results were not read with this method version.',
    RS.FAILED_OR_INDETERMINATE: ('The record or its outcome measures could not be parsed, or rule SG2 found at least one '
                                 'signal that no human review record has settled.'),
    RS.NONE_DETECTED: ('The 18 listed fields of every posted outcome measure were read and rule SG2 found no signal '
                       'for the dimension, or a human review record rejected every signal. Posted documents and '
                       'publications were not read.'),
    RS.DETECTED: 'At least one signal accepted by a human review record.',
}


def scope_table(scope: dict[str, Any]) -> dict[str, Any]:
    """The scope as the three rows the page states: how many records were fetched, how many
    trials this build covers, and how many frame records are left out and why."""
    ex = scope['excluded']
    return {'definition': 'Which of the fetched registry records this build covers. Every count in this file is over the included trials.',
            'rows': [
                {'id': 'frame', 'label': 'Registry records fetched (the frame)', 'n': scope['frame']['n'],
                 'definition': scope['frame']['definition']},
                {'id': 'included', 'label': 'Trials in this build', 'n': scope['included']['n'],
                 'definition': scope['included']['definition']},
                {'id': 'excluded', 'label': 'Frame records left out', 'n': ex['n'], 'reason': ex['reason'],
                 'definition': ex['definition']}]}


def summarize(records: dict[str, Any], problems: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Every count the page shows, with its numerator, a named denominator and a definition, and
    four separate tables: retrieval failures, parsing failures, missing data, negative findings.

    A trial whose rule could not be evaluated (a parsing failure) stays in the denominator and is
    not in the numerator; it is listed in the parsing-failure table, so "not usable" and "could not
    be evaluated" are never the same row."""
    recs = list(records.values())
    retrieved = [r for r in recs if r['results_status'] != 'not_retrieved']
    posted = [r for r in recs if r['results_status'] == 'posted']
    c1 = [r for r in posted if (r['comparative'] or {}).get('C1')]
    c2 = [r for r in posted if (r['comparative'] or {}).get('C2')]

    def n(pool: list[dict[str, Any]], pred: Callable[[dict[str, Any]], Any]) -> int:
        return sum(1 for r in pool if pred(r))

    denominators = {
        'requested': {'n': len(recs), 'definition': 'Trials this build covers (the requested list). See scope for the records fetched and those left out.'},
        'retrieved': {'n': len(retrieved), 'definition': 'Requested trials whose record the registry returned.'},
        'posted': {'n': len(posted), 'definition': 'Retrieved trials with posted results (hasResults true and a results section).'},
        'C1': {'n': len(c1), 'definition': 'Posted-results trials that are interventional with two or more groups on a posted primary measure.'},
        'C2': {'n': len(c2), 'definition': 'C1 trials whose registered allocation is RANDOMIZED.'},
    }
    def module(k: str) -> Callable[[dict[str, Any]], Any]:
        return lambda r: r['modules'] is not None and r['modules'][k]['present']

    def usable(k: str) -> Callable[[dict[str, Any]], Any]:
        return lambda r: r['modules'] is not None and r['modules'][k]['usable'] is True

    ba = lambda r: r['between_arm_analysis'] or {}   # noqa: E731
    metrics = [
        metric('results_posted', 'Results posted', len(posted), 'retrieved',
               'hasResults is true and the record carries a results section.'),
        metric('present_baseline', 'Baseline module present', n(posted, module('baseline')), 'posted',
               'resultsSection.baselineCharacteristicsModule exists and is a non-empty object. Presence only.'),
        metric('present_participant_flow', 'Participant flow module present', n(posted, module('participant_flow')), 'posted',
               'resultsSection.participantFlowModule exists and is a non-empty object. Presence only.'),
        metric('present_outcome_measures', 'Outcome measures module present', n(posted, module('outcome_measures')), 'posted',
               'resultsSection.outcomeMeasuresModule exists and is a non-empty object. Presence only.'),
        metric('present_adverse_events', 'Adverse events module present', n(posted, module('adverse_events')), 'posted',
               'resultsSection.adverseEventsModule exists and is a non-empty object. Presence only.'),
        metric('usable_baseline', 'Baseline table usable (rule B1)', n(posted, usable('baseline')), 'posted',
               'At least one baseline measure with a numeric value and a numeric denominator above zero for the Total group, or for every arm.'),
        metric('usable_participant_flow', 'Participant flow usable (rule F1)', n(posted, usable('participant_flow')), 'posted',
               'One period with numeric STARTED and COMPLETED counts for every flow group, STARTED above zero in at least one.'),
        metric('usable_primary_outcome', 'Primary outcome usable (rule O1)', n(posted, usable('outcome_measures')), 'posted',
               'A posted primary measure with a unit, a numeric number analysed for every group (above zero in at least one) and a numeric value for every analysed group.'),
        metric('usable_adverse_events', 'Adverse events usable (rule A1)', n(posted, usable('adverse_events')), 'posted',
               'Numeric serious and other numbers at risk for every event group (above zero in at least one), plus event rows or explicit totals.'),
        metric('usable_all_four', 'All four tables usable', n(posted, lambda r: r['all_four_usable'] is True), 'posted',
               'Rules B1, F1, O1 and A1 all pass. These rules test whether a table can be read, not whether its content is correct or complete.'),
        metric('C1', 'Two or more groups on a primary measure (C1)', len(c1), 'posted',
               'Interventional, with at least one posted primary measure that has two or more groups. Structure of the record: two groups are not necessarily two randomised arms.'),
        metric('C2', 'C1 and registered as randomised (C2)', len(c2), 'posted',
               'C1 and protocolSection.designModule.designInfo.allocation is RANDOMIZED.'),
        metric('between_arm_analysis', 'Between-arm analysis posted on a primary measure', n(c2, lambda r: ba(r).get('posted')), 'C2',
               'An analysis on a primary measure that names two or more of its groups and has a p-value or an estimate with both confidence limits.'),
        metric('between_arm_p_value', 'Between-arm analysis with a p-value', n(c2, lambda r: ba(r).get('with_p_value')), 'C2',
               'Such an analysis with a p-value string that contains a digit (a plain number or a bound).'),
        metric('between_arm_estimate_both_limits', 'Between-arm estimate with both confidence limits',
               n(c2, lambda r: ba(r).get('with_estimate_and_both_limits')), 'C2',
               'Such an analysis with a numeric estimate, a numeric lower limit and a numeric upper limit.'),
        metric('between_arm_p_value_bound', 'Between-arm p-value posted as a bound',
               n(c2, lambda r: 'bound' in (ba(r).get('p_value_forms') or [])), 'C2',
               'At least one between-arm analysis on a primary measure posts its p-value as a bound such as "<0.001"; the exact value is not posted.'),
        metric('subgroup_signal', 'Subgroup reporting detected by the current method: at least one signal',
               n(posted, lambda r: bool(r['subgroup']['signals'])), 'posted',
               'Rule SG2 found at least one signal in the 18 structured fields of a posted outcome measure. A signal is wording that looks like subgroup reporting; it awaits human review and is not a finding that a subgroup result is posted.'),
    ]

    states = {'denominator': 'requested', 'definitions': STATE_DEFINITIONS,
              'study': {s: n(recs, lambda r, s=s: r['subgroup']['state'] == s) for s in RS.ALL_SUBGROUP_STATES},
              'by_dimension': {d: {s: n(recs, lambda r, s=s, d=d: r['subgroup']['by_dimension'][d] == s)
                                   for s in RS.ALL_SUBGROUP_STATES} for d in RS.DIMENSIONS},
              'document_and_publication_review': {RS.DOCUMENT_AND_PUBLICATION_REVIEW: len(recs)},
              'note': ('Each trial is in exactly one state per dimension. With no human review record, no trial '
                       'can be in the detected state.')}
    all_signals = [s for r in posted for s in r['subgroup']['signals'] or []]
    signals = {'n_signals': len(all_signals), 'n_trials_with_a_signal': n(posted, lambda r: bool(r['subgroup']['signals'])),
               'denominator': 'posted',
               'by_dimension': {d: sum(1 for s in all_signals if s['dimension'] == d) for d in RS.SIGNAL_DIMENSIONS},
               'by_field': dict(sorted(Counter(s['field'] for s in all_signals).items())),
               'n_reviewed': sum(1 for s in all_signals if s['review']),
               'definition': ('One signal is one match of rule SG2 in one field of one posted outcome measure, for '
                              'one dimension. "other" is a signal that does not name age, sex, race or ethnicity.')}

    def reasons(k: str) -> list[dict[str, Any]]:
        c = Counter(r['modules'][k]['reason'] for r in posted if r['modules'] is not None and r['modules'][k]['usable'] is False)
        return [{'id': f'{k}_not_usable:{reason}', 'label': f'{RS.MODULES[k][1]} not usable: {reason}', 'n': cnt,
                 'denominator': 'posted', 'definition': f'Rule {RS.MODULES[k][1]} failed with this reason.'}
                for reason, cnt in sorted(c.items())]

    bp = lambda r: r['baseline_population'] or {}   # noqa: E731
    missing = [
        {'id': 'no_posted_results', 'label': 'No posted results', 'n': n(retrieved, lambda r: r['results_status'] == 'no_posted_results'),
         'denominator': 'retrieved', 'definition': 'hasResults is false. Nothing about these trials\' results is stated.'},
        *({'id': f'module_absent:{k}', 'label': f'{k} module absent', 'n': n(posted, lambda r, k=k: r['modules'] is not None and not r['modules'][k]['present']),
           'denominator': 'posted', 'definition': f'resultsSection.{RS.MODULES[k][0]} is absent or empty.'} for k in RS.MODULES),
        *(row for k in RS.MODULES for row in reasons(k)),
        {'id': 'baseline_total_not_formed', 'label': 'Baseline total cannot be formed',
         'n': n(posted, lambda r: bp(r).get('total', {}).get('value') is None), 'denominator': 'posted',
         'definition': 'No participants denominator row, or a non-numeric count for a group. Recorded as missing, not zero.'},
        {'id': 'flow_started_not_formed', 'label': 'Number started cannot be formed',
         'n': n(posted, lambda r: bp(r).get('started', {}).get('value') is None), 'denominator': 'posted',
         'definition': 'No first period, no STARTED milestone in it, or a non-numeric count for a group.'},
        {'id': 'enrollment_not_registered', 'label': 'No registered enrolment count',
         'n': n(retrieved, lambda r: bp(r).get('enrollment', {}).get('value') is None), 'denominator': 'retrieved',
         'definition': 'protocolSection.designModule.enrollmentInfo.count is absent.'},
        {'id': 'no_baseline_population_description', 'label': 'No baseline population description',
         'n': n(posted, lambda r: r['baseline_population'] is not None and r['baseline_population']['population_description'] is None),
         'denominator': 'posted', 'definition': 'baselineCharacteristicsModule.populationDescription is absent or blank; no population is inferred.'},
        {'id': 'no_frequency_threshold', 'label': 'No adverse-event frequency threshold stated',
         'n': n(posted, lambda r: r['adverse_events'] is not None and r['adverse_events']['frequency_threshold'] is None),
         'denominator': 'posted', 'definition': 'adverseEventsModule is present without frequencyThreshold.'},
    ]
    negative = [
        {'id': 'no_subgroup_signal', 'label': 'No subgroup signal found', 'n': n(posted, lambda r: r['subgroup']['signals'] == []),
         'denominator': 'posted',
         'scope': 'Rule SG2 over the 18 listed fields of every posted outcome measure. Posted documents, publications and every field not listed were not read.',
         'definition': 'The rule fired on nothing. This is not a finding that the trial reported no subgroup result anywhere.'},
        {'id': 'no_between_arm_analysis', 'label': 'No between-arm analysis posted on a primary measure',
         'n': n(c2, lambda r: not ba(r).get('posted')), 'denominator': 'C2',
         'scope': 'analyses[] of posted primary measures in the registry record.',
         'definition': 'Absence of a posted analysis is not absence of an analysis: the registry does not require one for every measure, and a publication or study report may hold it.'},
        {'id': 'no_analysis_on_primary', 'label': 'No statistical analysis of any kind on a primary measure',
         'n': n(posted, lambda r: r['between_arm_analysis'] is not None and r['between_arm_analysis']['n_analyses_on_primary'] == 0),
         'denominator': 'posted', 'scope': 'analyses[] of posted primary measures in the registry record.',
         'definition': 'No analyses[] row on any posted primary measure.'},
        {'id': 'serious_events_no_rows', 'label': 'No serious adverse event rows posted',
         'n': n(posted, lambda r: r['adverse_events'] is not None and not r['adverse_events']['serious_events_posted']),
         'denominator': 'posted', 'scope': 'adverseEventsModule.seriousEvents[].',
         'definition': 'The module is present with no serious-event row. With numeric totals this is a report of none; without them it is not.'},
    ]
    return {
        'scope': scope_table(scope),
        'denominators': denominators, 'metrics': metrics, 'subgroup_states': states, 'signals': signals,
        'tables': {
            # n is published with each table so the page never counts rows itself.
            'retrieval_failures': {'n': n(recs, lambda r: r['results_status'] == 'not_retrieved'), 'denominator': 'requested',
                                   'definition': 'Requested trials whose record was not retrieved. Their results were not read.',
                                   'rows': [{'nct_id': r['nct_id'], 'reason': r['retrieval']['reason']} for r in recs if r['results_status'] == 'not_retrieved'],
                                   'unkeyed_archive_lines': problems['unkeyed_lines'], 'unrequested_records': problems['unrequested_records']},
            'parsing_failures': {'n': n(retrieved, lambda r: r['parse']['status'] != 'ok'), 'denominator': 'retrieved',
                                 'definition': 'Retrieved records with at least one section the parser could not read. The record is kept; the section is recorded as not evaluated. n counts trials; rows list each section.',
                                 'rows': [{'nct_id': r['nct_id'], 'section': f['section'], 'reason': f['reason']}
                                          for r in retrieved for f in r['parse']['failures']]},
            'missing_data': {'definition': 'What the records do not contain. Each row is a count of trials, with its denominator.', 'rows': missing},
            'negative_findings': {'definition': 'Searches that found nothing, each with the scope of the search.', 'rows': negative},
        },
        'reviews': {'n_review_rows_applied': sum(len(s['review']) for s in all_signals),
                    'not_matched': problems['reviews_not_matched']},
    }


LIMITATIONS = (
    'Subgroup figures are subgroup reporting detected by the current method: a text rule over 18 structured fields of posted outcome measures. They are not a prevalence.',
    'A signal is wording that looks like subgroup reporting. Only a human review record can accept or reject it; with no review records, no trial is in the detected state.',
    'Posted protocols, statistical analysis plans, study reports and publications are not read. Absence in the structured fields is not absence everywhere.',
    'No result is inferred for a subgroup from a trial\'s overall result and its baseline proportions.',
    'Outcome switching is not assessed: a current record does not show how the registered outcomes changed over time.',
    'Usable-content rules test whether a table can be read, not whether its content is correct, complete or analysed appropriately.',
    'Two or more groups on a primary measure (C1, C2) is the structure of the record; dose cohorts, periods and disease cohorts also appear as groups.',
    'Participant flow counts are for the first period only.',
    'The registry record is read as retrieved on the stated date; registry records change.',
)


def run_identity(run: dict[str, Any], files: dict[str, Any]) -> str:
    """The ten hex digits that end a run id: a digest of everything the family publishes except the
    id itself, the time the build ran (`generated_at`) and the file checksums (which cover the id).

    It is taken from the output, not from a list of inputs, so that two families that differ in any
    published value cannot share an id. A list of inputs has to be kept complete by hand, and was
    not: a trial the archive lacks takes its failure reason from the archive's metadata file, which
    the archive's own sha256 does not cover. `run` is the manifest; `files` maps summary.json and
    records.json to their objects, with or without `run_id` set."""
    body = {k: v for k, v in run.items() if k not in ('run_id', 'generated_at', 'files')}
    content = {name: sha256(dump({k: v for k, v in obj.items() if k != 'run_id'})) for name, obj in sorted(files.items())}
    return sha256(dump([body, content]))[:10]


def build(records: dict[str, Any], problems: dict[str, Any], acquisition: dict[str, Any], producer: dict[str, Any],
          reviews_file: dict[str, Any], parameters: dict[str, Any], scope: dict[str, Any],
          generated_at: str | None = None) -> dict[str, Any]:
    """{file name: object} for the family. Pure apart from the clock when generated_at is not given."""
    retrieved_at = str(acquisition.get('retrieved_at') or '')[:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', retrieved_at):
        raise BuildError('the acquisition record has no retrieval date')
    files: dict[str, Any] = {
        'summary.json': summarize(records, problems, scope),
        'records.json': {'records': records},
    }
    for obj in files.values():
        obj.update(schema_version=SCHEMA_VERSION, method_version=RS.METHOD_VERSION)
    run: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'producer': producer, 'manual_run': True,
        'method': {'version': RS.METHOD_VERSION, 'subgroup_rule': RS.SUBGROUP_RULE,
                   'usable_content_rules': ['B1', 'F1', 'O1', 'A1'], 'comparative_classes': ['C1', 'C2'],
                   'statistical_analysis_rule': 'S1',
                   'source': 'docs/audits/outcomes_results_structure/second_pass/definitions.json'},
        'acquisition': {k: acquisition.get(k) for k in (
            'source', 'endpoint', 'request', 'retrieved_from', 'retrieved_to', 'retrieved_at', 'api_version',
            'data_timestamp', 'n_requested', 'n_records', 'n_requests', 'n_studies_requests', 'n_retries',
            'n_failed_requests', 'min_seconds_between_requests', 'requested_ids_sha256', 'sha256_uncompressed', 'archive')},
        'retrieval_window': {'from': acquisition.get('retrieved_from'), 'to': acquisition.get('retrieved_to')},
        'registry': {'api_version': acquisition.get('api_version'), 'data_timestamp': acquisition.get('data_timestamp')},
        'parameters': parameters,
        # frame, included and excluded, with the reason for the excluded records (build_scope).
        'scope': scope,
        'reviews': reviews_file,
        'n_records': len(records),
        'fields_inspected': list(RS.FIELDS_INSPECTED),
        'fields_not_inspected': list(RS.FIELDS_NOT_INSPECTED),
        'subgroup_states': list(RS.ALL_SUBGROUP_STATES),
        'document_and_publication_review': RS.DOCUMENT_AND_PUBLICATION_REVIEW,
        'limitations': list(LIMITATIONS),
    }
    # The id follows from what is published (run_identity): the archive, the reviews, the scope, the
    # producing commit and the code as it stood on disk are all in the manifest, and the records and
    # counts they gave are in the two files.
    run_id = f'results-structure-{retrieved_at}-{run_identity(run, files)}'
    for obj in files.values():
        obj['run_id'] = run_id
    run.update(run_id=run_id, generated_at=generated_at or _now())
    run['files'] = {p: sha256(dump(o)) for p, o in sorted(files.items())}
    return {'run.json': run, **files}


def _walk_keys(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _walk_keys(v)
    return found


def check(files: dict[str, Any]) -> list[str]:
    """Producer-side check of a family; every problem found (an empty list passes). The build
    runs it before anything is written."""
    p: list[str] = []
    run = files.get('run.json')
    if not isinstance(run, dict):
        return ['run.json is missing']
    if run.get('schema_version') != SCHEMA_VERSION:
        p.append('run.json schema_version is not 1')
    if not isinstance(run.get('run_id'), str) or not RUN_ID.match(run['run_id']):
        p.append('run.json run_id is malformed')
    if (run.get('method') or {}).get('version') != RS.METHOD_VERSION:
        p.append('run.json method version is not the current one')
    listed = run.get('files') or {}
    if set(listed) != {'summary.json', 'records.json'} or set(files) != {'run.json', 'summary.json', 'records.json'}:
        p.append('the family is exactly run.json, summary.json and records.json')
        return p
    for path in sorted(listed):
        if sha256(dump(files[path])) != listed[path]:
            p.append(f'{path}: sha256 does not match run.json')
        if files[path].get('run_id') != run.get('run_id') or files[path].get('schema_version') != SCHEMA_VERSION \
                or files[path].get('method_version') != RS.METHOD_VERSION:
            p.append(f'{path}: from another run, schema or method version')
    if isinstance(run.get('run_id'), str) and run['run_id'][-10:] != run_identity(run, {k: files[k] for k in listed}):
        p.append('run.json run_id does not follow from the content of the family')
    leaked = _walk_keys(files['records.json']) & PRIVATE_KEYS
    if leaked:
        p.append(f'review-file keys present in records.json: {sorted(leaked)}')
    records = files['records.json'].get('records') or {}
    summary = files['summary.json']
    if run.get('n_records') != len(records) or (summary.get('denominators') or {}).get('requested', {}).get('n') != len(records):
        p.append('record count differs between run.json, summary.json and records.json')
    n_detected = 0
    for n, r in records.items():
        if not NCT.match(n) or r.get('nct_id') != n:
            p.append(f'{n}: record key and nct_id disagree')
        if r.get('method_version') != RS.METHOD_VERSION:
            p.append(f'{n}: record has another method version')
        if r.get('results_status') not in RS.RESULTS_STATUSES:
            p.append(f'{n}: unknown results status')
        if r.get('document_and_publication_review') != RS.DOCUMENT_AND_PUBLICATION_REVIEW:
            p.append(f'{n}: document_and_publication_review is not stated')
        sg = r.get('subgroup') or {}
        states = [sg.get('state'), *((sg.get('by_dimension') or {}).get(d) for d in RS.DIMENSIONS)]
        if any(s not in RS.ALL_SUBGROUP_STATES for s in states):
            p.append(f'{n}: a subgroup state is outside the vocabulary')
            continue
        no_results = r['results_status'] == 'no_posted_results'
        if (no_results and any(s != RS.NOT_APPLICABLE for s in states)) or (not no_results and RS.NOT_APPLICABLE in states):
            p.append(f'{n}: not-applicable state and results status disagree')
        accepted = any(v['decision'] == 'accepted' for s in sg.get('signals') or [] for v in s['review'])
        if (RS.DETECTED in states) != accepted:
            p.append(f'{n}: a detected state without an accepted human review, or the reverse')
        n_detected += sg.get('state') == RS.DETECTED
        if r['results_status'] == 'unknown' and r['parse']['status'] != 'failed':
            p.append(f'{n}: unknown results status without a recorded parsing failure')
    # The published summary must be the one these records give: recount it.
    scope = run.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'frame', 'included', 'excluded'}:
        p.append('run.json has no scope (frame, included, excluded)')
        return p
    ex = scope['excluded']
    if scope['included']['n'] != len(records):
        p.append('scope: the included count is not the number of records')
    if ex['n'] != len(ex['nct_ids']) or (ex['n'] > 0 and not ex['reason']):
        p.append('scope: excluded records need their ids and a stated reason')
    if set(ex['nct_ids']) & set(records):
        p.append('scope: an excluded id has a record')
    recount = summarize(records, {'unkeyed_lines': summary['tables']['retrieval_failures'].get('unkeyed_archive_lines', []),
                                  'unrequested_records': summary['tables']['retrieval_failures'].get('unrequested_records', []),
                                  'reviews_not_matched': (summary.get('reviews') or {}).get('not_matched', [])}, scope)
    for key in ('scope', 'denominators', 'metrics', 'subgroup_states', 'signals', 'tables'):
        if recount[key] != summary.get(key):
            p.append(f'summary.json {key} is not what records.json gives')
    den = summary.get('denominators') or {}
    for m in summary.get('metrics') or []:
        d = den.get(m.get('denominator'))
        if d is None or not isinstance(m.get('numerator'), int) or not 0 <= m['numerator'] <= d['n'] or not m.get('definition'):
            p.append(f"metric {m.get('id')}: unknown denominator, numerator out of range, or no definition")
    for d in RS.DIMENSIONS:
        counts = (summary.get('subgroup_states') or {}).get('by_dimension', {}).get(d) or {}
        if set(counts) != set(RS.ALL_SUBGROUP_STATES) or sum(counts.values()) != len(records):
            p.append(f'subgroup states for {d} must count all five values and sum to the requested trials')
    return p


def write(files: dict[str, Any], out: Path) -> None:
    """Write the family into a staging sibling, then swap it in with renames, so a failed or
    interrupted write leaves the previous family intact and never a mix of two runs (the pattern
    of outcomes/artifacts.py write)."""
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.with_name(out.name + '.staging')
    old = out.with_name(out.name + '.previous')
    if old.exists() and not out.exists():
        old.rename(out)               # an interrupted swap: the previous family is the last good one
    for d in (stage, old):
        if d.exists():
            shutil.rmtree(d)
    stage.mkdir(parents=True)
    for path, obj in files.items():
        if path != 'run.json':
            (stage / path).write_bytes(dump(obj))
    (stage / 'run.json').write_text(json.dumps(files['run.json'], indent=1, ensure_ascii=False) + '\n')
    if out.exists():
        out.rename(old)
    try:
        stage.rename(out)
    except BaseException:
        if old.exists() and not out.exists():
            old.rename(out)
        raise
    if old.exists():
        shutil.rmtree(old)


def load_dir(out: Path) -> dict[str, Any]:
    return {name: json.loads((out / name).read_text()) for name in ('run.json', 'summary.json', 'records.json')
            if (out / name).exists()}
