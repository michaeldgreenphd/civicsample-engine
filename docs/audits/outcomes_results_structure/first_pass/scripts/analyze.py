"""Measure what the cached ClinicalTrials.gov results sections contain.

Run offline: python3 -I analyze.py OUTDIR
Reads OUTDIR/cache/raw_responses.jsonl.gz, OUTDIR/sample_ids.txt, OUTDIR/sample_frame.json.
Writes OUTDIR/analysis.json (every count with its denominator) and OUTDIR/subgroup_hits.jsonl
(every outcome measure the demographic-subgroup heuristic flagged, for human review).

Conventions
- "present" means the key exists AND is non-empty (non-empty list / non-blank string).
- Values in the API are strings; counts are parsed with to_int and anything unparsable is None
  (absence is kept as absence, never coerced to 0).
- All heuristics (title matching, subgroup detection, endpoint families) are defined in this file
  next to the code that applies them; the report labels them as heuristics.
"""
import gzip, json, sys, os, re, collections, random, unicodedata, difflib, math, statistics

OUT = sys.argv[1]
SEED = 20261113
C = collections.Counter

# ----------------------------------------------------------------------------- helpers
def load():
    studies = {}
    with gzip.open(os.path.join(OUT, 'cache', 'raw_responses.jsonl.gz'), 'rt', encoding='utf-8') as f:
        for line in f:
            for s in json.loads(json.loads(line)['body'])['studies']:
                studies[s['protocolSection']['identificationModule']['nctId']] = s
    ids = [l.strip() for l in open(os.path.join(OUT, 'sample_ids.txt')) if l.strip()]
    frame = json.load(open(os.path.join(OUT, 'sample_frame.json')))
    assert set(ids) == set(studies) == set(frame), 'cache, sample and frame must cover the same IDs'
    return ids, studies, frame

def nonempty(v):
    if v is None: return False
    if isinstance(v, str): return bool(v.strip())
    if isinstance(v, (list, dict)): return len(v) > 0
    return True

def to_int(v):
    if isinstance(v, int): return v
    if not isinstance(v, str): return None
    t = v.replace(',', '').strip()
    return int(t) if re.fullmatch(r'\d+', t) else None

def norm(s):
    """Normalised text for title comparison: NFKC, lower-case, non-alphanumerics -> space."""
    s = unicodedata.normalize('NFKC', s or '').lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9%<>=]+', ' ', s)).strip()

def wilson(k, n):
    if n == 0: return None
    z = 1.96; p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]

def frac(k, n):
    return {'k': k, 'n': n, 'pct': (round(100 * k / n, 1) if n else None), 'ci95': wilson(k, n)}

def dist(counter, n=None, top=None):
    tot = n if n is not None else sum(counter.values())
    items = counter.most_common(top)
    return {'n': tot, 'distinct': len(counter),
            'rows': [{'value': k, 'k': v, 'pct': round(100 * v / tot, 1) if tot else None} for k, v in items]}

def bucket(x, edges, labels):
    for e, l in zip(edges, labels):
        if x <= e: return l
    return labels[-1]

def quantiles(xs):
    xs = sorted(xs)
    if not xs: return None
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]
    return {'n': len(xs), 'min': xs[0], 'p05': q(.05), 'p25': q(.25), 'median': q(.5), 'p75': q(.75), 'p95': q(.95), 'max': xs[-1],
            'mean': round(statistics.fmean(xs), 3)}

ids, S, F = load()
N = len(ids)
A = {'meta': {'n_sample': N, 'seed': SEED}}
RS = lambda i: S[i].get('resultsSection') or {}
PS = lambda i: S[i].get('protocolSection') or {}
OMS = lambda i: (RS(i).get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []
stype = {i: (PS(i).get('designModule') or {}).get('studyType') for i in ids}
A['meta']['study_type_api'] = dict(C(stype.values()))
A['meta']['study_type_frame_vs_api_disagree'] = sum(1 for i in ids if stype[i] != F[i]['study_type'])
A['meta']['hasResults_true'] = frac(sum(1 for i in ids if S[i].get('hasResults') is True), N)

# ----------------------------------------------------------------------------- 3a module presence
MODS = ['participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule']
A['modules'] = {m: frac(sum(1 for i in ids if nonempty(RS(i).get(m))), N) for m in MODS}
A['modules']['all_five'] = frac(sum(1 for i in ids if all(nonempty(RS(i).get(m)) for m in MODS)), N)
A['modules']['outcomeMeasures_nonempty_list'] = frac(sum(1 for i in ids if OMS(i)), N)
A['modules']['moreInfo_limitationsAndCaveats'] = frac(sum(1 for i in ids if nonempty(((RS(i).get('moreInfoModule') or {}).get('limitationsAndCaveats') or {}).get('description'))), N)
A['modules']['by_study_type'] = {t: {m: frac(sum(1 for i in ids if stype[i] == t and nonempty(RS(i).get(m))), sum(1 for i in ids if stype[i] == t)) for m in MODS} for t in sorted(set(stype.values()))}

# ----------------------------------------------------------------------------- 3b registered vs posted
def has_data(om):
    """A posted measure carries data when any measurement has a non-blank value."""
    for c in om.get('classes') or []:
        for cat in c.get('categories') or []:
            for m in cat.get('measurements') or []:
                if nonempty(m.get('value')): return True
    return False

reg = {i: (PS(i).get('outcomesModule') or {}) for i in ids}
n_reg_p = {i: len(reg[i].get('primaryOutcomes') or []) for i in ids}
n_reg_s = {i: len(reg[i].get('secondaryOutcomes') or []) for i in ids}
n_reg_o = {i: len(reg[i].get('otherOutcomes') or []) for i in ids}
TYPES = ['PRIMARY', 'SECONDARY', 'OTHER_PRE_SPECIFIED', 'POST_HOC']
posted = {i: C(om.get('type') for om in OMS(i)) for i in ids}
cnt_lab = lambda x: bucket(x, [0, 1, 2, 3, 5, 10, 20, 10**9], ['0', '1', '2', '3', '4-5', '6-10', '11-20', '21+'])
R = A['registered_vs_posted'] = {}
R['registered_primary_per_trial'] = dist(C(cnt_lab(v) for v in n_reg_p.values()), N)
R['registered_secondary_per_trial'] = dist(C(cnt_lab(v) for v in n_reg_s.values()), N)
R['registered_counts_summary'] = {'primary': quantiles(list(n_reg_p.values())), 'secondary': quantiles(list(n_reg_s.values())),
                                  'other': quantiles(list(n_reg_o.values())),
                                  'total_registered_primary': sum(n_reg_p.values()), 'total_registered_secondary': sum(n_reg_s.values()),
                                  'total_registered_other': sum(n_reg_o.values())}
R['trials_with_registered'] = {'primary': frac(sum(1 for v in n_reg_p.values() if v), N), 'secondary': frac(sum(1 for v in n_reg_s.values() if v), N),
                               'other': frac(sum(1 for v in n_reg_o.values() if v), N)}
all_types = C(t for i in ids for t in posted[i].elements())
R['posted_measures_by_type'] = dist(all_types)
R['trials_with_posted_type'] = {t: frac(sum(1 for i in ids if posted[i].get(t)), N) for t in TYPES}
R['posted_per_trial'] = {t: {'summary': quantiles([posted[i].get(t, 0) for i in ids]), 'dist': dist(C(cnt_lab(posted[i].get(t, 0)) for i in ids), N)} for t in TYPES}
R['posted_any_type_per_trial'] = quantiles([sum(posted[i].values()) for i in ids])
# count comparison, registered primary vs posted PRIMARY
cmp_p = C('equal' if n_reg_p[i] == posted[i].get('PRIMARY', 0) else ('posted_fewer' if posted[i].get('PRIMARY', 0) < n_reg_p[i] else 'posted_more') for i in ids)
R['count_registered_primary_vs_posted_PRIMARY'] = dist(cmp_p, N)
cmp_s = C('equal' if n_reg_s[i] == posted[i].get('SECONDARY', 0) else ('posted_fewer' if posted[i].get('SECONDARY', 0) < n_reg_s[i] else 'posted_more') for i in ids)
R['count_registered_secondary_vs_posted_SECONDARY'] = dist(cmp_s, N)
R['sum_posted_secondary_over_sum_registered_secondary'] = {'posted': all_types.get('SECONDARY', 0), 'registered': sum(n_reg_s.values())}

# Title matching (HEURISTIC). The registered outcome 'measure' is compared with posted outcome
# 'title' after norm(). exact = identical normalised strings; fuzzy = difflib ratio >= 0.85 with
# the best posted title of that type. The registration compared is the CURRENT one, which sponsors
# usually bring into line with the results at posting; this does not detect outcome switching
# relative to the original registration (that needs the record history, not fetched).
def best(title, cands):
    t = norm(title); b = 0.0
    for c in cands:
        if c == t: return 1.0
        b = max(b, difflib.SequenceMatcher(None, t, c).ratio())
    return b
match_lv = C(); trial_all = trial_any = trial_none = 0; trial_any_data = 0
prim_with_data = {}
for i in ids:
    P = [om for om in OMS(i) if om.get('type') == 'PRIMARY']
    # data present is judged from the values alone: 4 PRIMARY measures carry values but have no reportingStatus key
    prim_with_data[i] = [om for om in P if has_data(om)]
    pt = [norm(om.get('title')) for om in P]
    ot = [norm(om.get('title')) for om in OMS(i) if om.get('type') != 'PRIMARY']
    res = []
    for o in reg[i].get('primaryOutcomes') or []:
        bp = best(o.get('measure'), pt)
        if bp == 1.0: r = 'exact_title_match_to_posted_PRIMARY'
        elif bp >= 0.85: r = 'fuzzy_match_to_posted_PRIMARY(>=0.85)'
        elif best(o.get('measure'), ot) >= 0.85: r = 'matches_only_a_non_PRIMARY_posted_measure'
        else: r = 'no_title_match'
        res.append(r); match_lv[r] += 1
    ok = [r.endswith('PRIMARY') or 'PRIMARY(' in r for r in res]
    if res and all(ok): trial_all += 1
    if any(ok): trial_any += 1
    if res and not any(ok): trial_none += 1
R['registered_primary_title_match'] = dist(match_lv)
R['trial_level_title_match'] = {'all_registered_primaries_matched': frac(trial_all, N), 'at_least_one_matched': frac(trial_any, N), 'none_matched': frac(trial_none, N)}
has_prim = {i: posted[i].get('PRIMARY', 0) > 0 for i in ids}
has_prim_data = {i: len(prim_with_data[i]) > 0 for i in ids}
R['trials_with_posted_PRIMARY'] = frac(sum(has_prim.values()), N)
R['trials_with_posted_PRIMARY_carrying_data'] = frac(sum(has_prim_data.values()), N)
R['trials_with_posted_PRIMARY_by_study_type'] = {t: frac(sum(1 for i in ids if stype[i] == t and has_prim_data[i]), sum(1 for i in ids if stype[i] == t)) for t in sorted(set(stype.values()))}
# every registered primary has a posted PRIMARY slot (count-level) and all posted PRIMARY carry data
R['trials_all_posted_PRIMARY_carry_data'] = frac(sum(1 for i in ids if has_prim[i] and len(prim_with_data[i]) == posted[i]['PRIMARY']), sum(has_prim.values()))
# Field-by-field identity of the registered list and the posted list (same position, raw strings).
# If these are identical the protocol-section outcome list is the SAME list as the posted one, not an
# independent prior registration.
eqc = C()
for i in ids:
    for kind, typ in (('primaryOutcomes', 'PRIMARY'), ('secondaryOutcomes', 'SECONDARY'), ('otherOutcomes', 'OTHER_PRE_SPECIFIED')):
        rp = reg[i].get(kind) or []
        pp = [om for om in OMS(i) if om.get('type') == typ]
        if len(rp) != len(pp): eqc[typ + '_trials_with_count_mismatch'] += 1; continue
        for a_, b_ in zip(rp, pp):
            eqc[typ + '_pairs'] += 1
            eqc[typ + '_title_identical'] += a_.get('measure') == b_.get('title')
            eqc[typ + '_timeFrame_identical'] += a_.get('timeFrame') == b_.get('timeFrame')
            eqc[typ + '_description_identical'] += a_.get('description') == b_.get('description')
R['registered_vs_posted_raw_identity'] = dict(sorted(eqc.items()))
nd = C()
for i in ids:
    for om in OMS(i):
        if om.get('type') != 'PRIMARY' or has_data(om): continue
        vals = [x.get('value') for d in om.get('denoms') or [] for x in d.get('counts') or []]
        nd['NOT_POSTED (no table yet)' if om.get('reportingStatus') == 'NOT_POSTED' else
           'POSTED, every arm has 0 analyzed' if vals and all(v == '0' for v in vals) else 'POSTED, other'] += 1
R['primary_measures_without_data'] = dist(nd)
R['trials_with_a_primary_measure_without_data'] = frac(sum(1 for i in ids if len(prim_with_data[i]) < posted[i].get('PRIMARY', 0)), N)
R['trials_with_no_primary_measure_carrying_data'] = frac(sum(1 for i in ids if not has_prim_data[i]), N)

# ----------------------------------------------------------------------------- 3c PRIMARY measures
PM = [(i, om) for i in ids for om in OMS(i) if om.get('type') == 'PRIMARY']
ALLM = [(i, om) for i in ids for om in OMS(i)]
nPM = len(PM)
P = A['primary_measures'] = {'n_primary_measures': nPM, 'n_trials_with_primary': sum(has_prim.values()), 'n_all_measures': len(ALLM)}
P['paramType'] = dist(C(om.get('paramType') or '(absent)' for _, om in PM))
P['unitOfMeasure_top25'] = dist(C(norm(om.get('unitOfMeasure')) or '(absent)' for _, om in PM), top=25)
def trial_level(fn, top=None):
    c = C(v for i in ids for v in {fn(om) for om in OMS(i) if om.get('type') == 'PRIMARY'})
    return {'n': N, 'distinct': len(c), 'rows': [{'value': k, 'k': v, 'pct': round(100 * v / N, 1)} for k, v in c.most_common(top)]}
P['unitOfMeasure_top25_trials'] = trial_level(lambda om: norm(om.get('unitOfMeasure')) or '(absent)', 25)
P['paramType_trials'] = trial_level(lambda om: om.get('paramType') or '(absent)')
P['dispersionType_trials'] = trial_level(lambda om: om.get('dispersionType') or '(absent)')
P['unitOfMeasure_raw_distinct'] = len({om.get('unitOfMeasure') for _, om in PM})
P['dispersionType'] = dist(C(om.get('dispersionType') or '(absent)' for _, om in PM))
P['n_groups'] = dist(C(bucket(len(om.get('groups') or []), [0, 1, 2, 3, 4, 10**9], ['0', '1', '2', '3', '4', '5+']) for _, om in PM))
P['n_groups_summary'] = quantiles([len(om.get('groups') or []) for _, om in PM])
P['n_classes'] = dist(C(bucket(len(om.get('classes') or []), [0, 1, 2, 5, 10, 10**9], ['0', '1', '2', '3-5', '6-10', '11+']) for _, om in PM))
def max_cats(om): return max([len(c.get('categories') or []) for c in om.get('classes') or []] or [0])
P['max_categories_per_class'] = dist(C(bucket(max_cats(om), [0, 1, 2, 5, 10**9], ['0', '1', '2', '3-5', '6+']) for _, om in PM))
P['any_class_title'] = frac(sum(1 for _, om in PM if any(nonempty(c.get('title')) for c in om.get('classes') or [])), nPM)
P['any_category_title'] = frac(sum(1 for _, om in PM if any(nonempty(cat.get('title')) for c in om.get('classes') or [] for cat in c.get('categories') or [])), nPM)
P['structure'] = dist(C(('single value per group' if len(om.get('classes') or []) == 1 and max_cats(om) == 1 else
                         'multiple classes, one category each' if len(om.get('classes') or []) > 1 and max_cats(om) <= 1 else
                         'one class, multiple categories' if len(om.get('classes') or []) == 1 and max_cats(om) > 1 else
                         'multiple classes and categories' if len(om.get('classes') or []) > 1 else 'no classes (no data table)') for _, om in PM))
P['measure_level_denoms_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('denoms'))), nPM)
P['class_level_denoms_present'] = frac(sum(1 for _, om in PM if any(nonempty(c.get('denoms')) for c in om.get('classes') or [])), nPM)
P['any_denoms_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('denoms')) or any(nonempty(c.get('denoms')) for c in om.get('classes') or [])), nPM)
P['denom_units'] = dist(C(norm(d.get('units')) or '(absent)' for _, om in PM for d in om.get('denoms') or []), top=12)
P['measures_with_non_participant_denom_unit'] = frac(sum(1 for _, om in PM if any(norm(d.get('units')) != 'participants' for d in om.get('denoms') or [])), nPM)
P['timeFrame_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('timeFrame'))), nPM)
P['description_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('description'))), nPM)
P['populationDescription_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('populationDescription'))), nPM)
P['reportingStatus'] = dist(C(om.get('reportingStatus') or '(absent)' for _, om in PM))
P['reportingStatus_all_types'] = dist(C(om.get('reportingStatus') or '(absent)' for _, om in ALLM))
P['carries_data'] = frac(sum(1 for _, om in PM if has_data(om)), nPM)
P['anticipatedPostingDate_present'] = frac(sum(1 for _, om in PM if nonempty(om.get('anticipatedPostingDate'))), nPM)
P['primary_measures_per_trial'] = dist(C(cnt_lab(posted[i].get('PRIMARY', 0)) for i in ids), N)
# Trial-level arm structure of primary measures
maxg = {i: max([len(om.get('groups') or []) for om in OMS(i) if om.get('type') == 'PRIMARY'] or [0]) for i in ids}
P['trials_max_groups_in_primary'] = dist(C(bucket(maxg[i], [0, 1, 2, 3, 4, 10**9], ['0 (no primary or no groups)', '1', '2', '3', '4', '5+']) for i in ids), N)
multi = {i: maxg[i] >= 2 for i in ids}
P['trials_with_primary_having_2plus_groups'] = frac(sum(multi.values()), N)

# Arm-level effect computability (INFERRED definition, stated here):
#  binary: paramType COUNT_OF_PARTICIPANTS, or NUMBER with a unit naming participants/patients/subjects,
#          >=2 groups, denominators present -> a risk difference/ratio is computable from arm counts.
#  continuous: paramType MEAN / LEAST_SQUARES_MEAN / GEOMETRIC_MEAN (+LS variants) with a dispersionType
#          that is an SD, SE or CI, >=2 groups, denominators present -> a mean difference is computable.
def comp_kind(om):
    if len(om.get('groups') or []) < 2 or not has_data(om): return None
    den = nonempty(om.get('denoms')) or any(nonempty(c.get('denoms')) for c in om.get('classes') or [])
    if not den: return None
    pt, u, dt = om.get('paramType') or '', norm(om.get('unitOfMeasure')), (om.get('dispersionType') or '').upper()
    if pt == 'COUNT_OF_PARTICIPANTS' or (pt in ('NUMBER', 'COUNT_OF_UNITS') and re.search(r'participant|patient|subject', u) and not re.search(r'percent|%|proportion', u)):
        return 'binary_counts'
    if 'MEAN' in pt and (('STANDARD' in dt) or ('CONFIDENCE' in dt)):
        return 'continuous_mean_with_dispersion'
    return None
ck = {i: {comp_kind(om) for om in OMS(i) if om.get('type') == 'PRIMARY'} - {None} for i in ids}
P['arm_level_effect_computable'] = {'any': frac(sum(1 for i in ids if ck[i]), N), 'binary_counts': frac(sum(1 for i in ids if 'binary_counts' in ck[i]), N),
                                    'continuous_mean_with_dispersion': frac(sum(1 for i in ids if 'continuous_mean_with_dispersion' in ck[i]), N),
                                    'any_among_trials_with_2plus_group_primary': frac(sum(1 for i in ids if ck[i]), sum(multi.values()))}

# ----------------------------------------------------------------------------- 3d analyses
AN = A['analyses'] = {}
def n_an(om): return len(om.get('analyses') or [])
prim_an = {i: sum(n_an(om) for om in OMS(i) if om.get('type') == 'PRIMARY') for i in ids}
any_an = {i: sum(n_an(om) for om in OMS(i)) for i in ids}
has_pa = {i: prim_an[i] > 0 for i in ids}
AN['trials_with_primary_analysis'] = {'of_all_sampled': frac(sum(has_pa.values()), N),
    'of_trials_with_posted_PRIMARY_data': frac(sum(1 for i in ids if has_pa[i] and has_prim_data[i]), sum(has_prim_data.values())),
    'of_trials_with_2plus_group_primary': frac(sum(1 for i in ids if has_pa[i] and multi[i]), sum(multi.values())),
    'of_trials_with_single_group_primary_only': frac(sum(1 for i in ids if has_pa[i] and maxg[i] == 1), sum(1 for i in ids if maxg[i] == 1))}
AN['trials_with_any_measure_analysis'] = frac(sum(1 for i in ids if any_an[i] > 0), N)
AN['primary_measures_with_analysis'] = {'of_all_primary_measures': frac(sum(1 for _, om in PM if n_an(om)), nPM),
    'of_primary_measures_with_2plus_groups': frac(sum(1 for _, om in PM if n_an(om) and len(om.get('groups') or []) >= 2), sum(1 for _, om in PM if len(om.get('groups') or []) >= 2))}
AN['measures_with_analysis_by_type'] = {t: frac(sum(1 for _, om in ALLM if om.get('type') == t and n_an(om)), sum(1 for _, om in ALLM if om.get('type') == t)) for t in TYPES}
AN['analyses_per_primary_measure_having_any'] = quantiles([n_an(om) for _, om in PM if n_an(om)])
PA = [(i, om, a) for i, om in PM for a in om.get('analyses') or []]
nPA = len(PA)
AN['n_primary_analyses'] = nPA
FIELDS = ['pValue', 'statisticalMethod', 'paramType', 'paramValue', 'ciPctValue', 'ciLowerLimit', 'ciUpperLimit', 'ciNumSides', 'dispersionType',
          'dispersionValue', 'nonInferiorityType', 'testedNonInferiority', 'groupDescription', 'pValueComment', 'statisticalComment', 'estimateComment', 'nonInferiorityComment', 'groupIds']
AN['field_presence_among_primary_analyses'] = {f: frac(sum(1 for *_, a in PA if nonempty(a.get(f))), nPA) for f in FIELDS}
AN['combo_presence_among_primary_analyses'] = {
    'estimate(paramType+paramValue)': frac(sum(1 for *_, a in PA if nonempty(a.get('paramType')) and nonempty(a.get('paramValue'))), nPA),
    'estimate+both_CI_limits': frac(sum(1 for *_, a in PA if nonempty(a.get('paramType')) and nonempty(a.get('paramValue')) and nonempty(a.get('ciLowerLimit')) and nonempty(a.get('ciUpperLimit'))), nPA),
    'pValue_or_estimate': frac(sum(1 for *_, a in PA if nonempty(a.get('pValue')) or nonempty(a.get('paramValue'))), nPA),
    'pValue_only_no_estimate': frac(sum(1 for *_, a in PA if nonempty(a.get('pValue')) and not nonempty(a.get('paramValue'))), nPA),
    'neither_pValue_nor_estimate': frac(sum(1 for *_, a in PA if not nonempty(a.get('pValue')) and not nonempty(a.get('paramValue'))), nPA)}
def pv_kind(v):
    v = (v or '').strip()
    if not v: return '(absent)'
    if re.fullmatch(r'[0-9.]+([eE]-?\d+)?', v): return 'plain number'
    if re.match(r'^[<≤]|^<=', v): return 'upper bound (<, <=)'
    if re.match(r'^[>≥=]|^>=', v): return 'lower bound or = (>, >=, =)'
    return 'other text'
AN['pValue_form'] = dist(C(pv_kind(a.get('pValue')) for *_, a in PA))
AN['nonInferiorityType'] = dist(C(a.get('nonInferiorityType') or '(absent)' for *_, a in PA))
AN['estimate_paramType_top20'] = dist(C((a.get('paramType') or '(absent)').strip() for *_, a in PA), top=21)
AN['estimate_paramType_top20_casefolded'] = dist(C(norm(a.get('paramType')) or '(absent)' for *_, a in PA), top=21)
AN['statisticalMethod_top15'] = dist(C((a.get('statisticalMethod') or '(absent)').strip() for *_, a in PA), top=16)
AN['ciPctValue'] = dist(C((a.get('ciPctValue') or '(absent)').strip() for *_, a in PA), top=8)
AN['ciNumSides'] = dist(C(a.get('ciNumSides') or '(absent)' for *_, a in PA))
AN['n_groups_compared'] = dist(C(bucket(len(a.get('groupIds') or []), [0, 1, 2, 3, 10**9], ['0', '1', '2', '3', '4+']) for *_, a in PA))
def tl(pred): return sum(1 for i in ids if any(pred(a) for om in OMS(i) if om.get('type') == 'PRIMARY' for a in om.get('analyses') or []))
npa_tr = sum(has_pa.values())
AN['trial_level_among_trials_with_primary_analysis'] = {
    'has_pValue': frac(tl(lambda a: nonempty(a.get('pValue'))), npa_tr),
    'has_statisticalMethod': frac(tl(lambda a: nonempty(a.get('statisticalMethod'))), npa_tr),
    'has_estimate': frac(tl(lambda a: nonempty(a.get('paramType')) and nonempty(a.get('paramValue'))), npa_tr),
    'has_estimate_with_both_CI_limits': frac(tl(lambda a: nonempty(a.get('paramValue')) and nonempty(a.get('ciLowerLimit')) and nonempty(a.get('ciUpperLimit'))), npa_tr),
    'has_non_inferiority_or_equivalence_type': frac(tl(lambda a: (a.get('nonInferiorityType') or '').startswith(('NON_INFERIORITY', 'EQUIVALENCE'))), npa_tr),
    'has_type_SUPERIORITY_exactly': frac(tl(lambda a: a.get('nonInferiorityType') == 'SUPERIORITY'), npa_tr),
    'has_any_SUPERIORITY*_type(incl. _OR_OTHER, legacy)': frac(tl(lambda a: (a.get('nonInferiorityType') or '').startswith('SUPERIORITY')), npa_tr)}
AN['trial_level_of_all_sampled'] = {
    'primary_analysis_with_pValue': frac(tl(lambda a: nonempty(a.get('pValue'))), N),
    'primary_analysis_with_estimate_and_both_CI_limits': frac(tl(lambda a: nonempty(a.get('paramValue')) and nonempty(a.get('ciLowerLimit')) and nonempty(a.get('ciUpperLimit'))), N)}
def strat(key, order=None):
    g = collections.defaultdict(list)
    for i in ids: g[key(i)].append(i)
    return {k: {'primary_analysis': frac(sum(1 for i in v if has_pa[i]), len(v)), 'two_plus_group_primary': frac(sum(1 for i in v if multi[i]), len(v))} for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}
AN['by_study_type'] = strat(lambda i: stype[i])
AN['by_phase_frame'] = strat(lambda i: F[i]['phase'] or '(blank)')
AN['by_sponsor_class_frame'] = strat(lambda i: F[i]['sponsor_class'] if F[i]['sponsor_class'] in ('INDUSTRY', 'OTHER', 'NIH') else 'other classes')
def yr(i):
    d = ((PS(i).get('statusModule') or {}).get('resultsFirstPostDateStruct') or {}).get('date') or ''
    y = int(d[:4]) if d[:4].isdigit() else None
    return '(none)' if y is None else ('2008-2012' if y <= 2012 else '2013-2016' if y <= 2016 else '2017-2020' if y <= 2020 else '2021-2026')
AN['by_results_first_post_year'] = strat(yr)

# ----------------------------------------------------------------------------- 3e demographic subgroups
# HEURISTIC, deliberately conservative. "Level titles" = class titles + category titles of ONE measure.
# Level titles (and measures) that mention a DONOR are ignored: donor sex/age describes the donor,
# not a subgroup of participants.
# Tier 1 (levels): the level titles themselves show subgroup levels of a demographic dimension.
#   sex:       one level title names a female term and a DIFFERENT level title names a male term.
#   race:      >=2 distinct race levels among level titles (OMB categories; "non-X" counts as its own level).
#              'white blood/matter/cell/coat/light' and 'black box/stool' style phrases are excluded.
#   ethnicity: >=2 level titles in the measure and at least one names Hispanic/Latino.
#   age:       >=2 distinct level titles that are age levels. An age level is a title with
#              (a) a digit AND an explicit age word ('age', 'aged', 'years old', 'years of age', ...), or
#              (b) a numeric year/month band ('3-4 years', '>=65 years', '12 years or older') when the
#                  measure title contains 'age'/'aged' or the level title also carries a life-stage word;
#              or the level titles name >=2 DIFFERENT life stages (children vs adults, adults vs elderly...).
#              Bare '1 year', '2 years' classes are NOT counted (usually time points), and a single
#              repeated life-stage word ('Child-Pugh', 'Pediatric INTERMED ...', 'child was better') is not.
#   subgroup-titled: the measure title says 'subgroup' and >=1 level title names a demographic level
#              (counted under that level's dimension).
# Tier 2 (title cue only): the measure title says the result is BY the dimension ('by sex', 'by race',
#   'age group', 'subgroup analysis: sex', 'according to sex' ...) but Tier 1 is not satisfied.
# Mention only (NOT counted as subgroup reporting; a loose upper bound): any title/level title
#   contains a sex, race, ethnicity or age-group word (instrument names such as "Children's Depression
#   Rating Scale" and single-sex populations such as "Percentage of Women With ..." land here).
FEM = re.compile(r'\b(females?|women|girls?)\b', re.I)
MAL = re.compile(r'\b(males?|men|boys?)\b', re.I)
RACE_PATS = {
    'white': re.compile(r'\b(white(?!\s+(blood|matter|cells?|coat|count|light|noise|spot|lesion))|caucasians?)\b', re.I),
    'black': re.compile(r'\b(black(?!\s+(box|stools?|light))|african[- ]americans?)\b', re.I),
    'asian': re.compile(r'\basians?\b', re.I),
    'aian': re.compile(r'american indian|alaskan? native|native american', re.I),
    'nhpi': re.compile(r'native hawaiian|pacific islander', re.I),
    'multiracial': re.compile(r'more than one race|multi[- ]?racial|multiple races', re.I)}
NONRACE = re.compile(r'\bnon[- ]?(white|black|asian|caucasian)s?\b', re.I)
ETH = re.compile(r'\b(hispanics?|latinos?|latinas?|latinx|latine)\b', re.I)
AGE_WORD = re.compile(r'\b(age[ds]?|years?[- ]old|yrs?[- ]old|years? of age|months? of age|months?[- ]old|year[- ]olds?|y/o|yo)\b', re.I)
STAGES = collections.OrderedDict([
    ('non-elderly', r'\bnon[- ]?elderly\b'), ('elderly', r'(?<!non-)(?<!non )\b(elderly|seniors?|older adults?)\b'),
    ('infant', r'\b(infants?|toddlers?|neonates?|newborns?)\b'), ('adolescent', r'\b(adolescents?|teen(ager)?s?)\b'),
    ('child', r'\b(child(?![- ]pugh)(ren)?|p(a)?ediatrics?)\b'), ('adult', r'(?<!older )(?<!young )\badults?\b')])
STAGE_RE = {k: re.compile(v, re.I) for k, v in STAGES.items()}
def stages(x): return {k for k, p in STAGE_RE.items() if p.search(x)}
_U = r'(years?|yrs?|y\b|months?|mos?\b)'
YBAND = re.compile(r'(\d+\s*(-|–|to)\s*(<\s*)?\d+|[<>≤≥]=?\s*\d+|\d+\s*\+|\d+\s*(and|or)\s*(older|younger|over|under|above|below))\s*' + _U +
                   r'|\d+\s*' + _U + r'\s*(and|or)\s*(older|younger|over|under|above|below|less|more)|(less|older|younger|more|greater) than\s*\d+\s*' + _U, re.I)
DONOR = re.compile(r'\bdonors?\b', re.I)
_D = r'(biological\s+)?(sex|gender)'
CUE = {
    'sex': re.compile(r'\b(by|per|according to|stratified by|within|across|between)\s+' + _D + r'\b|\b(sex|gender)[- ](sub)?groups?\b|\b(sex|gender)[- ]specific\b|\bsubgroup\w*\b[^.;]{0,30}\b(sex|gender)\b|\beffect of\b[^.;]{0,25}\b(sex|gender)\b|\b(males? (and|vs\.?|versus|or) females?|females? (and|vs\.?|versus|or) males?|men (and|vs\.?|versus) women|women (and|vs\.?|versus) men)\b', re.I),
    'race': re.compile(r'\b(by|per|according to|stratified by|within|across)\s+(race|racial)\b|\b(race|racial)[- ](sub)?groups?\b|\bsubgroup\w*\b[^.;]{0,30}\brac(e|ial)\b', re.I),
    'ethnicity': re.compile(r'\b(by|per|according to|stratified by|within|across)\s+(race/|race and |race or )?ethnicity\b|\bethnic(ity)?[- ](sub)?groups?\b|\bsubgroup\w*\b[^.;]{0,30}\bethnic', re.I),
    'age': re.compile(r'\b(by|per|according to|stratified by|within|across)\s+age\b|\bage[- ]?(sub)?(groups?|cohorts?|strat\w+|categor\w+|bands?|ranges?)\b|\bsubgroup\w*\b[^.;]{0,30}\bage\b', re.I)}
MENTION = {'sex': re.compile(FEM.pattern + '|' + MAL.pattern + r'|\b(sex|gender)\b', re.I),
           'race': re.compile('|'.join(p.pattern for p in RACE_PATS.values()) + r'|\brac(e|ial)\b', re.I),
           'ethnicity': re.compile(ETH.pattern + r'|\bethnic', re.I),
           'age': re.compile(CUE['age'].pattern + '|' + '|'.join(STAGES.values()) + r'|\bage[ds]?\b', re.I)}

def levels(om):
    cls = [(c.get('title') or '').strip() for c in om.get('classes') or [] if nonempty(c.get('title'))]
    cats = [(cat.get('title') or '').strip() for c in om.get('classes') or [] for cat in c.get('categories') or [] if nonempty(cat.get('title'))]
    return cls, cats

def uniq(xs):
    seen = []
    for x in xs:
        if x not in seen: seen.append(x)
    return seen

def classify(om, extra_levels=None):
    """Return (tier1 dims, tier2 dims, mention dims, {dim: matched level titles}).
    With extra_levels (the measure's GROUP titles) only the Tier 1 level rules are applied."""
    cls, cats = levels(om)
    title = om.get('title') or ''
    lv_all = uniq(cls + cats) if extra_levels is None else uniq(extra_levels)
    lv = [x for x in lv_all if not DONOR.search(x)]
    if DONOR.search(title): lv = []
    t1, where = set(), {}
    f = [x for x in lv if FEM.search(x) and not MAL.search(x)]
    m = [x for x in lv if MAL.search(x) and not FEM.search(x)]
    if f and m: t1.add('sex'); where['sex'] = f + m
    rl = {}
    for x in lv:
        non = NONRACE.search(x)
        if non: rl.setdefault('non-' + non.group(1).lower(), x)
        for k, p in RACE_PATS.items():
            if p.search(NONRACE.sub(' ', x)): rl.setdefault(k, x)
    if len(rl) >= 2: t1.add('race'); where['race'] = list(rl.values())
    e = [x for x in lv if ETH.search(x)]
    if e and len(lv) >= 2: t1.add('ethnicity'); where['ethnicity'] = e
    title_age = bool(re.search(r'\bage[ds]?\b', title, re.I))
    a = [x for x in lv if (AGE_WORD.search(x) and re.search(r'\d', x)) or (YBAND.search(x) and (title_age or stages(x)))]
    # life stages must differ BETWEEN level titles ('Children' vs 'Adults'), not co-occur in one title
    st = {frozenset(stages(x)) for x in lv if stages(x)}
    if len(uniq(a)) >= 2: t1.add('age'); where['age'] = uniq(a)
    elif len(st) >= 2: t1.add('age'); where['age'] = [x for x in lv if stages(x)]
    if extra_levels is None and re.search(r'\bsubgroup', title, re.I):
        # subgroup-titled measure: one demographic level title is enough
        one = {'sex': f + m, 'race': list(rl.values()), 'ethnicity': e, 'age': uniq(a) or [x for x in lv if stages(x)]}
        for d, xs in one.items():
            if xs and d not in t1: t1.add(d); where[d] = xs
    t2 = ({d for d, p in CUE.items() if p.search(title)} - t1) if extra_levels is None and not DONOR.search(title) else set()
    men = {d for d, p in MENTION.items() if p.search(title) or any(p.search(x) for x in lv_all)} if extra_levels is None else set()
    return t1, t2, men, where

def class_denoms(om, where_titles):
    """Are per-class denominators given for the demographic levels, and do they differ between levels?"""
    cls = om.get('classes') or []
    in_class = [c for c in cls if (c.get('title') or '').strip() in where_titles]
    in_cat = any((cat.get('title') or '').strip() in where_titles for c in cls for cat in c.get('categories') or [])
    if in_class:
        with_d = [c for c in in_class if nonempty(c.get('denoms'))]
        sig = {json.dumps(c.get('denoms'), sort_keys=True) for c in with_d}
        return {'levels_in': 'class', 'all_levels_have_class_denoms': len(with_d) == len(in_class), 'any_level_has_class_denoms': bool(with_d),
                'denoms_differ_between_levels': len(sig) > 1}
    return {'levels_in': 'category' if in_cat else 'other', 'all_levels_have_class_denoms': False, 'any_level_has_class_denoms': False, 'denoms_differ_between_levels': False}

SG = A['subgroups'] = {}
hits = []
t1_tr = collections.defaultdict(set); t2_tr = collections.defaultdict(set); men_tr = collections.defaultdict(set)
t1_trP = collections.defaultdict(set); t2_trP = collections.defaultdict(set); men_trP = collections.defaultdict(set)
arm_tr = collections.defaultdict(set); arm_trP = collections.defaultdict(set)
t1_meas = C(); t2_meas = C(); den_stats = C(); t1_meas_n = t1_meas_P = t2_only_n = 0; arm_hits = []
for i, om in ALLM:
    t1, t2, men, where = classify(om)
    isP = om.get('type') == 'PRIMARY'
    for d in t1: t1_tr[d].add(i); t1_meas[d] += 1; (t1_trP[d].add(i) if isP else None)
    for d in t2: t2_tr[d].add(i); t2_meas[d] += 1; (t2_trP[d].add(i) if isP else None)
    for d in men: men_tr[d].add(i); (men_trP[d].add(i) if isP else None)
    # supplementary: the same Tier 1 rules applied to the measure's GROUP (arm) titles
    g1, _, _, gw = classify(om, extra_levels=[(g.get('title') or '').strip() for g in om.get('groups') or [] if nonempty(g.get('title'))])
    for d in g1: arm_tr[d].add(i); (arm_trP[d].add(i) if isP else None)
    if g1: arm_hits.append({'nct_id': i, 'type': om.get('type'), 'title': om.get('title'), 'dims': sorted(g1),
                            'group_titles': [(g.get('title') or '') for g in om.get('groups') or []][:8], 'n_groups': len(om.get('groups') or []), 'n_analyses': n_an(om), 'carries_data': has_data(om)})
    if t1:
        t1_meas_n += 1; t1_meas_P += isP
        wt = {x for d in t1 for x in where[d]}
        cd = class_denoms(om, wt)
        den_stats['levels_in_' + cd['levels_in']] += 1
        den_stats['all_levels_have_class_denoms'] += cd['all_levels_have_class_denoms']
        den_stats['any_level_has_class_denoms'] += cd['any_level_has_class_denoms']
        den_stats['class_denoms_differ_between_levels'] += cd['denoms_differ_between_levels']
        den_stats['measure_level_denoms_present'] += nonempty(om.get('denoms'))
    elif t2: t2_only_n += 1
    if t1 or t2:
        cls, cats = levels(om)
        hits.append({'nct_id': i, 'type': om.get('type'), 'title': om.get('title'), 'tier1': sorted(t1), 'tier2_title_cue': sorted(t2),
                     'matched_levels': {d: where[d][:12] for d in t1}, 'class_titles': uniq(cls)[:40], 'category_titles': uniq(cats)[:40],
                     'n_classes': len(om.get('classes') or []), 'n_groups': len(om.get('groups') or []),
                     'class_denoms': class_denoms(om, {x for d in t1 for x in where[d]}) if t1 else None,
                     'paramType': om.get('paramType'), 'unitOfMeasure': om.get('unitOfMeasure'), 'n_analyses': n_an(om), 'carries_data': has_data(om), 'reportingStatus': om.get('reportingStatus')})
with open(os.path.join(OUT, 'subgroup_hits.jsonl'), 'w') as f:
    for h in hits: f.write(json.dumps(h, sort_keys=True) + '\n')
DIMS = ['sex', 'race', 'ethnicity', 'age']
U = lambda dd, dims=DIMS: set().union(*[dd[d] for d in dims]) if dims else set()
def block(t1d, t2d, mend, armd):
    return {'tier1_levels': {**{d: frac(len(t1d[d]), N) for d in DIMS}, 'any_dimension': frac(len(U(t1d)), N), 'sex_race_or_ethnicity': frac(len(U(t1d, ['sex', 'race', 'ethnicity'])), N), 'race_or_ethnicity': frac(len(U(t1d, ['race', 'ethnicity'])), N)},
            'tier2_title_cue_only': {**{d: frac(len(t2d[d] - t1d[d]), N) for d in DIMS}, 'any_dimension': frac(len(U(t2d) - U(t1d)), N)},
            'tier1_or_tier2': {**{d: frac(len(t1d[d] | t2d[d]), N) for d in DIMS}, 'any_dimension': frac(len(U(t1d) | U(t2d)), N), 'sex_race_or_ethnicity': frac(len(U(t1d, ['sex', 'race', 'ethnicity']) | U(t2d, ['sex', 'race', 'ethnicity'])), N)},
            'mention_only_upper_bound': {**{d: frac(len(mend[d]), N) for d in DIMS}, 'any_dimension': frac(len(U(mend)), N)},
            'supplementary_arm_titles_tier1': {**{d: frac(len(armd[d]), N) for d in DIMS}, 'any_dimension': frac(len(U(armd)), N)}}
SG['trials_any_measure'] = block(t1_tr, t2_tr, men_tr, arm_tr)
SG['trials_primary_measure'] = block(t1_trP, t2_trP, men_trP, arm_trP)
SG['measures'] = {'n_all_measures': len(ALLM), 'tier1_measures': t1_meas_n, 'tier1_primary_measures': int(t1_meas_P), 'tier2_only_measures': t2_only_n,
                  'tier1_by_dimension': dict(t1_meas), 'tier2_by_dimension': dict(t2_meas)}
SG['tier1_measure_denominators'] = {'n_tier1_measures': t1_meas_n, **{k: frac(v, t1_meas_n) for k, v in sorted(den_stats.items())}}
t1_any = U(t1_tr)
SG['tier1_trials_with_analysis_on_a_subgroup_measure'] = frac(len({h['nct_id'] for h in hits if h['tier1'] and h['n_analyses']}), len(t1_any))
SG['tier1_trials_by_study_type'] = {t: frac(sum(1 for i in t1_any if stype[i] == t), sum(1 for i in ids if stype[i] == t)) for t in sorted(set(stype.values()))}
# Examples for human review: EVERY flagged trial (Tier 1 or Tier 2), one representative measure each
# (PRIMARY preferred, then most dimensions), ordered by NCT ID -- a census of the hits, not a selection.
byt = collections.defaultdict(list)
for h in hits: byt[h['nct_id']].append(h)
ex = []
for n_ in sorted(byt):
    v = sorted(byt[n_], key=lambda h: (h['type'] != 'PRIMARY', not h['tier1'], -len(h['tier1'])))
    ex.append({**v[0], 'n_flagged_measures_in_trial': len(v), 'flagged_types': dict(C(h['type'] for h in v)),
               'dims_tier1_trial': sorted({d for h in v for d in h['tier1']}), 'dims_tier2_trial': sorted({d for h in v for d in h['tier2_title_cue']})})
SG['flagged_trials_all'] = ex
# 15 unflagged "mention only" measures, seeded, one per trial, to show what the conservative rule leaves out
flagged_keys = {(h['nct_id'], h['title']) for h in hits}
menpool = []
for i, om in ALLM:
    if (i, om.get('title')) in flagged_keys: continue
    _, _, men, _ = classify(om)
    if men:
        cls, cats = levels(om)
        menpool.append({'nct_id': i, 'type': om.get('type'), 'title': om.get('title'), 'mention_dims': sorted(men), 'level_titles': uniq(cls + cats)[:6],
                        'group_titles': [(g.get('title') or '') for g in om.get('groups') or []][:6]})
rng = random.Random(SEED); rng.shuffle(menpool)
seen_t, mex = set(), []
for h in menpool:
    if h['nct_id'] in seen_t: continue
    seen_t.add(h['nct_id']); mex.append(h)
    if len(mex) == 15: break
SG['mention_only_examples_15'] = mex
# trials whose ONLY signal is demographic strata in arm (group) titles
flagged_trials = {h['nct_id'] for h in hits}
ab = collections.defaultdict(list)
for h in arm_hits:
    if h['nct_id'] not in flagged_trials: ab[h['nct_id']].append(h)
SG['arm_level_only_trials'] = [{**sorted(v, key=lambda h: h['type'] != 'PRIMARY')[0], 'n_measures_with_arm_strata': len(v), 'dims_trial': sorted({d for h in v for d in h['dims']})} for _, v in sorted(ab.items())]
SG['union_tier1_tier2_arm'] = {'any_measure': frac(len(flagged_trials | {h['nct_id'] for h in arm_hits}), N),
                               'primary_measure': frac(len({h['nct_id'] for h in hits if h['type'] == 'PRIMARY'} | {h['nct_id'] for h in arm_hits if h['type'] == 'PRIMARY'}), N),
                               'any_measure_sex_race_ethnicity': frac(len({h['nct_id'] for h in hits if set(h['tier1'] + h['tier2_title_cue']) & {'sex', 'race', 'ethnicity'}} | {h['nct_id'] for h in arm_hits if set(h['dims']) & {'sex', 'race', 'ethnicity'}}), N),
                               'any_measure_race_or_ethnicity': frac(len({h['nct_id'] for h in hits if set(h['tier1'] + h['tier2_title_cue']) & {'race', 'ethnicity'}} | {h['nct_id'] for h in arm_hits if set(h['dims']) & {'race', 'ethnicity'}}), N)}
SG['mention_only_measures_unflagged'] = len(menpool)

# ----------------------------------------------------------------------------- 3f adverse events
AE = A['adverse_events'] = {}
aem = {i: RS(i).get('adverseEventsModule') or {} for i in ids}
nAE = sum(1 for i in ids if aem[i])
AE['module_present'] = frac(nAE, N)
ser = {i: nonempty(aem[i].get('seriousEvents')) for i in ids}; oth = {i: nonempty(aem[i].get('otherEvents')) for i in ids}
AE['seriousEvents_nonempty'] = frac(sum(ser.values()), N); AE['otherEvents_nonempty'] = frac(sum(oth.values()), N)
AE['both'] = frac(sum(1 for i in ids if ser[i] and oth[i]), N); AE['either'] = frac(sum(1 for i in ids if ser[i] or oth[i]), N)
AE['module_present_but_no_event_rows'] = frac(sum(1 for i in ids if aem[i] and not ser[i] and not oth[i]), N)
def all_zero(i, k):
    g = aem[i].get('eventGroups') or []
    return bool(g) and all(to_int(str(x.get(k))) == 0 for x in g)
no_ser = [i for i in ids if aem[i] and not ser[i]]
AE['no_serious_rows_and_all_groups_report_0_serious_affected'] = frac(sum(1 for i in no_ser if all_zero(i, 'seriousNumAffected')), len(no_ser))
no_oth = [i for i in ids if aem[i] and not oth[i]]
AE['no_other_rows_and_all_groups_report_0_other_affected'] = frac(sum(1 for i in no_oth if all_zero(i, 'otherNumAffected')), len(no_oth))
def allg(i, k): 
    g = aem[i].get('eventGroups') or []
    return bool(g) and all(nonempty(x.get(k)) for x in g)
for k in ['seriousNumAtRisk', 'otherNumAtRisk', 'seriousNumAffected', 'otherNumAffected', 'deathsNumAtRisk', 'deathsNumAffected']:
    AE['all_groups_have_' + k] = frac(sum(1 for i in ids if allg(i, k)), N)
AE['eventGroups_per_trial'] = dist(C(bucket(len(aem[i].get('eventGroups') or []), [0, 1, 2, 3, 4, 10**9], ['0', '1', '2', '3', '4', '5+']) for i in ids), N)
def rows_stat(kind):
    rows = [st for i in ids for ev in aem[i].get(kind) or [] for st in ev.get('stats') or []]
    return {'n_stat_rows': len(rows), 'numAtRisk': frac(sum(1 for r in rows if nonempty(r.get('numAtRisk'))), len(rows)),
            'numAffected': frac(sum(1 for r in rows if nonempty(r.get('numAffected'))), len(rows)), 'numEvents': frac(sum(1 for r in rows if nonempty(r.get('numEvents'))), len(rows))}
AE['serious_event_rows'] = rows_stat('seriousEvents'); AE['other_event_rows'] = rows_stat('otherEvents')
AE['frequencyThreshold'] = dist(C(str(aem[i].get('frequencyThreshold')) if aem[i] else '(module absent)' for i in ids), N, top=12)
AE['frequencyThreshold_present'] = frac(sum(1 for i in ids if nonempty(aem[i].get('frequencyThreshold'))), N)
AE['timeFrame_present'] = frac(sum(1 for i in ids if nonempty(aem[i].get('timeFrame'))), N)
AE['description_present'] = frac(sum(1 for i in ids if nonempty(aem[i].get('description'))), N)
def vocab(v):
    v = (v or '').strip()
    if not v: return '(absent)'
    return 'MedDRA (any version)' if re.match(r'meddra', v, re.I) else ('CTCAE (any version)' if re.search(r'ctc', v, re.I) else ('SNOMED CT' if re.search(r'snomed', v, re.I) else 'other'))
AE['sourceVocabulary_event_rows'] = dist(C(vocab(ev.get('sourceVocabulary')) for i in ids for k in ('seriousEvents', 'otherEvents') for ev in aem[i].get(k) or []))
AE['assessmentType_event_rows'] = dist(C(ev.get('assessmentType') or '(absent)' for i in ids for k in ('seriousEvents', 'otherEvents') for ev in aem[i].get(k) or []))
AE['event_terms_per_trial'] = {'serious': quantiles([len(aem[i].get('seriousEvents') or []) for i in ids]), 'other': quantiles([len(aem[i].get('otherEvents') or []) for i in ids])}
AE['deaths_table_by_results_year'] = {y: frac(sum(1 for i in ids if yr(i) == y and allg(i, 'deathsNumAtRisk')), sum(1 for i in ids if yr(i) == y)) for y in ('2008-2012', '2013-2016', '2017-2020', '2021-2026')}

# ----------------------------------------------------------------------------- 3g participant flow
PF = A['participant_flow'] = {}
pf = {i: RS(i).get('participantFlowModule') or {} for i in ids}
PF['module_present'] = frac(sum(1 for i in ids if pf[i]), N)
PF['periods_per_trial'] = dist(C(bucket(len(pf[i].get('periods') or []), [0, 1, 2, 3, 10**9], ['0', '1', '2', '3', '4+']) for i in ids), N)
PF['groups_per_trial'] = dist(C(bucket(len(pf[i].get('groups') or []), [0, 1, 2, 3, 4, 10**9], ['0', '1', '2', '3', '4', '5+']) for i in ids), N)
def has_ms(i, t, first_only=False):
    ps = (pf[i].get('periods') or [])
    ps = ps[:1] if first_only else ps
    return any((m.get('type') or '').strip().upper() == t for p in ps for m in p.get('milestones') or [])
PF['first_period_STARTED'] = frac(sum(1 for i in ids if has_ms(i, 'STARTED', True)), N)
PF['first_period_COMPLETED'] = frac(sum(1 for i in ids if has_ms(i, 'COMPLETED', True)), N)
PF['first_period_NOT_COMPLETED'] = frac(sum(1 for i in ids if has_ms(i, 'NOT COMPLETED', True)), N)
PF['every_period_has_STARTED_and_COMPLETED'] = frac(sum(1 for i in ids if (pf[i].get('periods') or []) and all(
    {'STARTED', 'COMPLETED'} <= {(m.get('type') or '').strip().upper() for m in p.get('milestones') or []} for p in pf[i]['periods'])), N)
PF['any_additional_milestone'] = frac(sum(1 for i in ids if any((m.get('type') or '').strip().upper() not in ('STARTED', 'COMPLETED', 'NOT COMPLETED') for p in pf[i].get('periods') or [] for m in p.get('milestones') or [])), N)
PF['any_dropWithdraws'] = frac(sum(1 for i in ids if any(nonempty(p.get('dropWithdraws')) for p in pf[i].get('periods') or [])), N)
PF['dropWithdraw_reason_types_top15'] = dist(C((d.get('type') or '(absent)').strip() for i in ids for p in pf[i].get('periods') or [] for d in p.get('dropWithdraws') or []), top=15)
def any_noncompleter(i):
    for p in pf[i].get('periods') or []:
        for m in p.get('milestones') or []:
            if (m.get('type') or '').strip().upper() == 'NOT COMPLETED' and any((to_int(a.get('numSubjects')) or 0) > 0 for a in m.get('achievements') or []): return True
    return False
nc = [i for i in ids if any_noncompleter(i)]
PF['trials_with_any_NOT_COMPLETED_count_above_0'] = frac(len(nc), N)
PF['dropWithdraws_present_among_trials_with_noncompleters'] = frac(sum(1 for i in nc if any(nonempty(p.get('dropWithdraws')) for p in pf[i].get('periods') or [])), len(nc))
PF['dropWithdraw_reason_types_top15_casefolded'] = dist(C(norm(d.get('type')) or '(absent)' for i in ids for p in pf[i].get('periods') or [] for d in p.get('dropWithdraws') or []), top=15)
PF['dropWithdraw_reason_trials_top10'] = (lambda c: {'n': N, 'rows': [{'value': k, 'k': v, 'pct': round(100 * v / N, 1)} for k, v in c.most_common(10)]})(C(t for i in ids for t in {norm(d.get('type')) for p in pf[i].get('periods') or [] for d in p.get('dropWithdraws') or []}))
PF['recruitmentDetails_present'] = frac(sum(1 for i in ids if nonempty(pf[i].get('recruitmentDetails'))), N)
PF['preAssignmentDetails_present'] = frac(sum(1 for i in ids if nonempty(pf[i].get('preAssignmentDetails'))), N)
PF['typeUnitsAnalyzed_present'] = frac(sum(1 for i in ids if nonempty(pf[i].get('typeUnitsAnalyzed'))), N)
def flow_total(i, t):
    ps = pf[i].get('periods') or []
    if not ps: return None
    ms = [m for m in ps[0].get('milestones') or [] if (m.get('type') or '').strip().upper() == t]
    if not ms: return None
    vals = [to_int(a.get('numSubjects')) for a in ms[0].get('achievements') or []]
    return None if (not vals or any(v is None for v in vals)) else sum(vals)
started = {i: flow_total(i, 'STARTED') for i in ids}
completed = {i: flow_total(i, 'COMPLETED') for i in ids}
PF['first_period_STARTED_total_parsable'] = frac(sum(1 for v in started.values() if v is not None), N)
PF['not_completed_in_first_period'] = frac(sum(1 for i in ids if started[i] is not None and completed[i] is not None and completed[i] < started[i]), sum(1 for i in ids if started[i] is not None and completed[i] is not None))
rat = [completed[i] / started[i] for i in ids if started[i] and completed[i] is not None]
PF['first_period_completed_over_started'] = quantiles([round(x, 3) for x in rat])

# ----------------------------------------------------------------------------- 3h baseline
BL = A['baseline'] = {}
bm = {i: RS(i).get('baselineCharacteristicsModule') or {} for i in ids}
# Total-group selection follows the project's documented rule for AACT baselines: a title match on
# "total" first (word match, so "Total Title" counts), then a sum-check (a last group whose count
# equals the sum of the others is a total under another name, e.g. "Pooled ..."), and only then
# the sum of the groups. Summing without the sum-check double-counts those pooled groups.
method = C(); btot = {}; total_last = C()
for i in ids:
    gs = bm[i].get('groups') or []
    dn = [d for d in bm[i].get('denoms') or [] if norm(d.get('units')) == 'participants'] or (bm[i].get('denoms') or [])
    if not gs or not dn: btot[i] = None; method['no groups or denoms'] += 1; continue
    counts = {c.get('groupId'): to_int(c.get('value')) for c in dn[0].get('counts') or []}
    tot = [g for g in gs if re.search(r'\btotal\b', g.get('title') or '', re.I)]
    v = [counts.get(g.get('id')) for g in gs]
    if tot:
        total_last['Total-titled group is the last (highest-numbered) BG group' if tot[-1].get('id') == gs[-1].get('id') == max(g.get('id') for g in gs) else 'Total-titled group is not last'] += 1
        btot[i] = counts.get(tot[-1].get('id')); method['group titled Total'] += 1
    elif len(gs) == 1:
        btot[i] = v[0]; method['single group'] += 1
    elif len(gs) >= 3 and all(x is not None for x in v) and v[-1] == sum(v[:-1]) and v[-1] > 0:
        btot[i] = v[-1]; method['last group equals the sum of the others (untitled total)'] += 1
    else:
        btot[i] = None if any(x is None for x in v) else sum(v); method['sum of groups (no total group found)'] += 1
BL['baseline_total_method'] = dist(method, N)
BL['total_group_position'] = dist(total_last)
BL['baseline_total_parsable'] = frac(sum(1 for v in btot.values() if v is not None), N)
BL['baseline_denom_units'] = dist(C(norm(d.get('units')) or '(absent)' for i in ids for d in bm[i].get('denoms') or []), top=8)
both = [i for i in ids if btot[i] is not None and started[i] is not None]
BL['baseline_vs_flow_STARTED'] = dist(C('equal' if btot[i] == started[i] else ('baseline_smaller' if btot[i] < started[i] else 'baseline_larger') for i in both), len(both))
BL['baseline_over_started_when_different'] = quantiles([round(btot[i] / started[i], 3) for i in both if started[i] and btot[i] != started[i]])
enr = {i: ((PS(i).get('designModule') or {}).get('enrollmentInfo') or {}) for i in ids}
BL['enrollment_type'] = dist(C(enr[i].get('type') or '(absent)' for i in ids), N)
act = [i for i in ids if enr[i].get('type') == 'ACTUAL' and isinstance(enr[i].get('count'), int) and enr[i]['count'] > 0 and btot[i] is not None]
BL['enrollment_actual_zero_or_missing'] = sum(1 for i in ids if enr[i].get('type') == 'ACTUAL' and not enr[i].get('count'))
ratios = {i: btot[i] / enr[i]['count'] for i in act}
def rb(x):
    return ('<0.50' if x < 0.5 else '0.50-<0.80' if x < 0.8 else '0.80-<0.95' if x < 0.95 else '0.95-<1.00' if x < 1 else '=1.00' if x == 1 else '>1.00-1.05' if x <= 1.05 else '>1.05-1.50' if x <= 1.5 else '>1.50')
order = ['<0.50', '0.50-<0.80', '0.80-<0.95', '0.95-<1.00', '=1.00', '>1.00-1.05', '>1.05-1.50', '>1.50']
cr = C(rb(x) for x in ratios.values())
BL['baseline_total_over_enrollment_actual'] = {'n': len(act), 'rows': [{'value': o, 'k': cr.get(o, 0), 'pct': round(100 * cr.get(o, 0) / len(act), 1)} for o in order], 'summary': quantiles([round(x, 3) for x in ratios.values()])}
BL['ratio_above_1_5'] = [{'nct_id': i, 'baseline_total': btot[i], 'enrollment_actual': enr[i]['count'], 'flow_started': started[i],
                          'flow_unit': pf[i].get('typeUnitsAnalyzed'), 'baseline_groups': [(g.get('title') or '')[:40] for g in bm[i].get('groups') or []][:6]}
                         for i in act if ratios[i] > 1.5]
zc = [i for i in ids if started[i] and completed[i] == 0]
PF['first_period_zero_completers'] = frac(len(zc), sum(1 for i in ids if started[i]))
PF['first_period_zero_completers_by_status'] = dict(C((PS(i).get('statusModule') or {}).get('overallStatus') for i in zc))
BL['enrollment_actual_vs_flow_STARTED'] = dist(C('equal' if enr[i]['count'] == started[i] else ('enrollment_larger' if enr[i]['count'] > started[i] else 'enrollment_smaller') for i in act if started[i] is not None))
# frame enrollment vs API enrollment (consistency of our record with the live registry)
BL['frame_enrollment_equals_api_enrollment'] = frac(sum(1 for i in ids if F[i].get('enrollment') == enr[i].get('count')), N)
pd_ = {i: (bm[i].get('populationDescription') or '').strip() for i in ids}
BL['populationDescription_present'] = frac(sum(1 for v in pd_.values() if v), N)
BL['measure_level_populationDescription_present_any'] = frac(sum(1 for i in ids if any(nonempty(m.get('populationDescription')) for m in bm[i].get('measures') or [])), N)
# Keyword tagging of baseline population descriptions (HEURISTIC; a description can carry several tags)
POP = collections.OrderedDict([
    ('intent-to-treat / ITT / mITT', r'intent(ion)?[- ]to[- ]treat|\bm?itt\b'),
    ('full analysis set / FAS', r'full analysis|\bfas\b'),
    ('safety population / set', r'safety (population|analysis|set)|\bsaf\b'),
    ('all randomized', r'randomi[sz]ed'),
    ('all enrolled', r'\benrolled\b|\benrollment\b'),
    ('treated / received >=1 dose', r'received|treated|at least (one|1) dose|dosed'),
    ('per-protocol / evaluable', r'per[- ]protocol|evaluable|\bpp\b'),
    ('completers / completed', r'complet'),
    ('withdrew / excluded / lost / missing explained', r'withdr[ae]w|excluded|lost to follow|missing|not (collected|available|analy[sz]ed)|dropped|drop[- ]?out|screen fail'),
    ('analyzed / analysis population (generic)', r'analy[sz]ed|analysis population|analysis set')])
tags = C(); untag = 0
for v in pd_.values():
    if not v: continue
    hit = [k for k, p in POP.items() if re.search(p, v, re.I)]
    for k in hit: tags[k] += 1
    untag += not hit
npd = sum(1 for v in pd_.values() if v)
BL['populationDescription_tags'] = {'n': npd, 'rows': [{'value': k, 'k': tags.get(k, 0), 'pct': round(100 * tags.get(k, 0) / npd, 1)} for k in POP] + [{'value': '(no tag matched)', 'k': untag, 'pct': round(100 * untag / npd, 1)}]}
dd = sorted({v for v in pd_.values() if v})
BL['populationDescription_distinct'] = len(dd)
rng2 = random.Random(SEED); pick = rng2.sample(dd, min(30, len(dd)))
inv = collections.defaultdict(list)
for i in ids:
    if pd_[i]: inv[pd_[i]].append(i)
BL['populationDescription_sample30'] = [{'nct_id': inv[v][0], 'text': v if len(v) <= 300 else v[:297] + '...'} for v in pick]
BL['pd_present_by_baseline_vs_started'] = {k: frac(sum(1 for i in both if pd_[i] and ((btot[i] == started[i]) == (k == 'equal'))), sum(1 for i in both if (btot[i] == started[i]) == (k == 'equal'))) for k in ('equal', 'different')}
# baseline demographic measures present in the API record (title-based), and agreement with our record
def has_bm(i, pat): return any(re.search(pat, m.get('title') or '', re.I) for m in bm[i].get('measures') or [])
api_race = {i: has_bm(i, r'\brace\b') for i in ids}; api_eth = {i: has_bm(i, r'ethnic') for i in ids}; api_sex = {i: has_bm(i, r'\bsex\b|\bgender\b') for i in ids}
BL['api_baseline_measure_titles'] = {'race (title contains race)': frac(sum(api_race.values()), N), 'ethnicity (title contains ethnic)': frac(sum(api_eth.values()), N), 'sex/gender': frac(sum(api_sex.values()), N),
                                     'age': frac(sum(1 for i in ids if has_bm(i, r'\bage\b')), N), 'region of enrollment': frac(sum(1 for i in ids if has_bm(i, r'region of enrollment')), N)}
fr_race = {i: bool((F[i].get('race') or {}).get('reported')) for i in ids}
fr_eth = {i: bool((F[i].get('ethnicity') or {}).get('reported')) for i in ids}
BL['frame_race_reported_vs_api_race_title'] = {'both': sum(1 for i in ids if fr_race[i] and api_race[i]), 'frame_only': sum(1 for i in ids if fr_race[i] and not api_race[i]),
                                               'api_only': sum(1 for i in ids if not fr_race[i] and api_race[i]), 'neither': sum(1 for i in ids if not fr_race[i] and not api_race[i]), 'n': N}
BL['api_measure_Race_and_Ethnicity_Not_Collected'] = frac(sum(1 for i in ids if has_bm(i, r'race and ethnicity not collected')), N)
BL['api_only_race_measure_titles'] = dist(C(m.get('title') for i in ids if api_race[i] and not fr_race[i] for m in bm[i].get('measures') or [] if re.search(r'\brace\b', m.get('title') or '', re.I)))

# ----------------------------------------------------------------------------- 3i co-occurrence
def two(a, b, sub=None):
    S_ = [i for i in ids if sub is None or sub(i)]
    t = {'a1_b1': sum(1 for i in S_ if a[i] and b[i]), 'a1_b0': sum(1 for i in S_ if a[i] and not b[i]),
         'a0_b1': sum(1 for i in S_ if not a[i] and b[i]), 'a0_b0': sum(1 for i in S_ if not a[i] and not b[i]), 'n': len(S_)}
    t['pct_b_given_a1'] = frac(t['a1_b1'], t['a1_b1'] + t['a1_b0']); t['pct_b_given_a0'] = frac(t['a0_b1'], t['a0_b1'] + t['a0_b0'])
    t['pct_both_of_all'] = frac(t['a1_b1'], t['n'])
    t['odds_ratio'] = round((t['a1_b1'] * t['a0_b0']) / (t['a1_b0'] * t['a0_b1']), 2) if t['a1_b0'] and t['a0_b1'] else None
    return t
CO = A['cooccurrence'] = {}
CO['frame_counts'] = {'race_reported': frac(sum(fr_race.values()), N), 'ethnicity_reported': frac(sum(fr_eth.values()), N),
                      'sex_reported': frac(sum(1 for i in ids if (F[i].get('sex') or {}).get('reported')), N)}
CO['race_reported_x_primary_analysis'] = two(fr_race, has_pa)
CO['race_reported_x_primary_analysis__interventional_only'] = two(fr_race, has_pa, lambda i: stype[i] == 'INTERVENTIONAL')
CO['race_reported_x_primary_analysis__2plus_group_primary_only'] = two(fr_race, has_pa, lambda i: multi[i])
CO['ethnicity_reported_x_primary_analysis'] = two(fr_eth, has_pa)
both_re = {i: fr_race[i] and fr_eth[i] for i in ids}
CO['race_and_ethnicity_reported_x_primary_analysis'] = two(both_re, has_pa)
pa_est = {i: any(nonempty(a.get('paramValue')) and nonempty(a.get('ciLowerLimit')) and nonempty(a.get('ciUpperLimit')) for om in OMS(i) if om.get('type') == 'PRIMARY' for a in om.get('analyses') or []) for i in ids}
CO['race_reported_x_primary_analysis_with_estimate_and_CI'] = two(fr_race, pa_est)
CO['race_reported_x_posted_primary_with_data'] = two(fr_race, has_prim_data)
CO['race_reported_x_arm_level_effect_computable'] = two(fr_race, {i: bool(ck[i]) for i in ids})

# ----------------------------------------------------------------------------- 5 comparability
CP = A['comparability'] = {}
ptitles = {i: uniq([norm(om.get('title')) for om in OMS(i) if om.get('type') == 'PRIMARY' and nonempty(om.get('title'))]) for i in ids}
wp = [i for i in ids if ptitles[i]]
tc = C(t for i in wp for t in ptitles[i])
CP['n_trials_with_primary_title'] = len(wp)
CP['distinct_normalised_primary_titles'] = len(tc)
CP['titles_used_by_one_trial_only'] = frac(sum(1 for v in tc.values() if v == 1), len(tc))
CP['top15_titles'] = [{'title': t, 'trials': k, 'pct_of_trials_with_primary': round(100 * k / len(wp), 1)} for t, k in tc.most_common(15)]
CP['trials_covered_by_top15_titles'] = frac(sum(1 for i in wp if any(t in dict(tc.most_common(15)) for t in ptitles[i])), len(wp))
CP['trials_sharing_exact_title_with_another_sampled_trial'] = frac(sum(1 for i in wp if any(tc[t] > 1 for t in ptitles[i])), len(wp))
# title + unit pairs
tu = {i: uniq([(norm(om.get('title')), norm(om.get('unitOfMeasure'))) for om in OMS(i) if om.get('type') == 'PRIMARY' and nonempty(om.get('title'))]) for i in ids}
tuc = C(x for i in wp for x in tu[i])
CP['trials_sharing_exact_title_and_unit'] = frac(sum(1 for i in wp if any(tuc[x] > 1 for x in tu[i])), len(wp))
grp = collections.defaultdict(list)
for i in wp: grp[F[i].get('primary_condition') or '(none)'].append(i)
rows = []
for g, mem in sorted(grp.items(), key=lambda kv: -len(kv[1])):
    c = C(t for i in mem for t in ptitles[i])
    uc = C(u for i in mem for u in uniq([norm(om.get('unitOfMeasure')) or '(absent)' for om in OMS(i) if om.get('type') == 'PRIMARY']))
    share = sum(1 for i in mem if any(c[t] > 1 for t in ptitles[i]))
    top_t, top_k = c.most_common(1)[0]
    top_u, top_uk = uc.most_common(1)[0]
    rows.append({'group': g, 'trials': len(mem), 'distinct_titles': len(c), 'trials_sharing_a_title_within_group': share,
                 'pct_sharing': round(100 * share / len(mem), 1), 'top_title': top_t[:80] if top_k > 1 else '(no title shared by 2+ trials)', 'top_title_trials': top_k,
                 'top_unit': top_u, 'top_unit_trials': top_uk, 'pct_top_unit': round(100 * top_uk / len(mem), 1)})
CP['by_primary_condition'] = rows
CP['within_group_sharing_overall'] = frac(sum(r['trials_sharing_a_title_within_group'] for r in rows), len(wp))
# Endpoint families (ROUGH keyword heuristic on primary titles; a trial can fall in several; first-listed has no priority)
FAM = collections.OrderedDict([
    ('adverse events / safety / tolerability / DLT / MTD', r'adverse|safety|tolerab|toxicit|dose[- ]limiting|\bdlts?\b|maximum tolerated|\bmtd\b|\bteaes?\b|\bsaes?\b|side effect'),
    ('pharmacokinetics (Cmax, AUC, concentration, half-life)', r'\bcmax\b|\bauc|pharmacokinetic|plasma concentration|serum concentration|\btmax\b|half[- ]life|clearance|trough|bioavailab|\bpk\b'),
    ('tumour response (ORR, CR, response rate)', r'objective response|overall response|response rate|\borr\b|complete response|complete remission|\bpcr\b|pathologic(al)? complete|clinical benefit rate|disease control'),
    ('progression-free / event-free / disease-free survival', r'progression[- ]free|\bpfs\b|event[- ]free|disease[- ]free|relapse[- ]free|recurrence[- ]free|time to progression'),
    ('overall survival / mortality / death', r'overall survival|\bos\b|mortality|\bdeaths?\b|(?<!free )(?<!free-)\bsurvival'),
    ('immunogenicity (titer, GMT, seroconversion, antibody)', r'geometric mean titer|\bgmts?\b|\bgmc\b|seroconver|seroprotect|seroresponse|antibod|immunogenic|titer|titre'),
    ('HbA1c / glucose', r'hba1c|a1c|glycosylated|glycated|glucose|glycemi|glycaemi'),
    ('pain score', r'\bpain\b'),
    ('blood pressure', r'blood pressure|\bsbp\b|\bdbp\b'),
    ('viral load / virologic response', r'hiv[- ]1? rna|viral load|virologic|\bsvr\d*\b|hcv rna|hbv dna'),
    ('lung function (FEV1, FVC)', r'\bfev1?\b|\bfvc\b|forced expiratory|forced vital|peak expiratory'),
    ('depression / anxiety / psychiatric scale', r'hamilton|ham-?d|madrs|phq|panss|depress|anxiety|\bcaps\b|\bymrs\b|\bbdi\b'),
    ('body weight / BMI', r'body weight|\bbmi\b|body mass|weight loss|weight change|change in weight'),
    ('feasibility / adherence / retention / recruitment', r'feasib|adheren|retention|recruit|complian|acceptab|enroll'),
    ('quality of life / patient-reported function', r'quality of life|\bqol\b|sf-?36|eq-?5d|promis'),
    ('lipids (LDL-C etc.)', r'ldl|cholesterol|triglycerid|lipid'),
    ('visual acuity / intraocular pressure', r'visual acuity|\bbcva\b|intraocular pressure|\biop\b'),
    ('abstinence / substance use', r'abstinen|smoking|cigarette|drinking days|heavy drinking|drug use|cessation|urine (drug|toxicology)')])
famc = C(); nofam = 0
fam_tr = {}
for i in wp:
    raw = ' | '.join((om.get('title') or '') for om in OMS(i) if om.get('type') == 'PRIMARY')
    hit = [k for k, p in FAM.items() if re.search(p, raw, re.I)]
    fam_tr[i] = hit
    for k in hit: famc[k] += 1
    nofam += not hit
CP['endpoint_families'] = {'n': len(wp), 'rows': [{'value': k, 'k': famc.get(k, 0), 'pct': round(100 * famc.get(k, 0) / len(wp), 1)} for k, _ in sorted(FAM.items(), key=lambda kv: -famc.get(kv[0], 0))] +
                           [{'value': '(no family matched)', 'k': nofam, 'pct': round(100 * nofam / len(wp), 1)}]}
onc = [i for i in wp if F[i].get('primary_condition') == 'Oncology']
oc = C(k for i in onc for k in fam_tr[i])
CP['endpoint_families_oncology'] = {'n': len(onc), 'rows': [{'value': k, 'k': v, 'pct': round(100 * v / len(onc), 1)} for k, v in oc.most_common(6)] + [{'value': '(no family matched)', 'k': sum(1 for i in onc if not fam_tr[i]), 'pct': round(100 * sum(1 for i in onc if not fam_tr[i]) / len(onc), 1)}]}
CP['units_distinct_normalised'] = len({norm(om.get('unitOfMeasure')) for _, om in PM})
CP['frame_primary_endpoint_equals_first_posted_primary_title'] = frac(sum(1 for i in wp if norm(F[i].get('primary_endpoint')) in ptitles[i]), len(wp))
reg_first = {i: norm(((reg[i].get('primaryOutcomes') or [{}])[0]).get('measure')) for i in ids}
CP['frame_primary_endpoint_equals_first_registered_primary'] = frac(sum(1 for i in ids if norm(F[i].get('primary_endpoint')) == reg_first[i]), N)

json.dump(A, open(os.path.join(OUT, 'analysis.json'), 'w'), indent=1, sort_keys=True, ensure_ascii=False)
print('wrote analysis.json;', len(hits), 'flagged subgroup measures')
