"""What actually varies inside six candidate endpoint families (randomised Phase 3 members examined).

Run: python3 -I scripts/family_detail.py V1_DIR OUTDIR
Examined set per family = family_examined.json: randomised Phase 3 members in the 2,000 sample (results
from the first audit's cache) plus the seeded draw fetched by fetch_family.py.
For each examined trial the FAMILY MEASURE is the first `outcomeMeasures[]` entry with
`type == "PRIMARY"` whose `title` matches the family's endpoint regex (families.py). Everything below is
read from that one measure; a trial with no such posted measure is counted as "no matching posted
primary measure" and excluded from the attribute tables (never silently dropped).

Attributes (all rule-based; the regexes are in this file):
 construct     family-specific wording class of title+description (e.g. PFS by independent review vs
               investigator; HbA1c change vs proportion at target; trough vs peak FEV1)
 instrument    family-specific (response criteria; depression scale and version; n/a for laboratory values)
 unit          `unitOfMeasure`, lower-cased, punctuation collapsed
 param         `paramType` (+ `dispersionType`)
 time_weeks    `timeFrame` parsed to weeks when it names one fixed time point ("Week 24", "24 weeks",
               "6 months", "Day 85"); otherwise "event-driven/variable" (contains "up to", "until",
               "through", "from ... to", several time points, or no number)
 population    tags found in the measure's `populationDescription`: ITT, mITT, FAS, per-protocol,
               safety/treated, all randomized, evaluable/completers, observed-cases/other; "not stated"
 effect        normalised `analyses[].paramType` of analyses naming 2+ groups (between-arm), plus whether
               any such analysis has a numeric estimate with both numeric confidence limits
 control       arm types from `protocolSection.armsInterventionsModule.armGroups[].type`
"""
import sys, os, json, re, gzip, collections, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, families
from common import is_num

V1, OUT = sys.argv[1], sys.argv[2]
S, _ = common.load_sample(V1)
REL = common.load_release(OUT)
EX = json.load(open(os.path.join(OUT, 'family_examined.json')))
full, proto = {}, {}
with gzip.open(os.path.join(OUT, 'cache', 'family_studies.jsonl.gz'), 'rt', encoding='utf-8') as f:
    for line in f:
        rec = json.loads(line)
        for s in json.loads(rec['body']).get('studies', []):
            n = s['protocolSection']['identificationModule']['nctId']
            (full if 'resultsSection' in s or ':full' in rec['purpose'] else proto)[n] = s


def norm_unit(u):
    return re.sub(r'[^a-z0-9%/^]+', ' ', (u or '').lower()).strip() or '(none)'


def time_weeks(tf):
    t = (tf or '').lower()
    if not t.strip():
        return None, 'not stated'
    if re.search(r'\bup to\b|\buntil\b|\bthrough(out)?\b|\bmaximum\b|\bapproximately\b|\bassessed\b|\bmedian\b|\bfrom\b.*\bto\b|\band\b|,', t) and not re.fullmatch(r'\s*(baseline|day 1|randomi[sz]ation)?\s*(,|and|to|through)?\s*(week|month|day)s?\s*\d+\s*', t):
        # several time points or an event-driven window; a "Baseline, Week 24" pair is handled below
        m = re.fullmatch(r'\s*(?:from\s+)?(?:baseline|day 1|day 0|week 0|randomi[sz]ation|pre-?dose)[^0-9]{0,40}(week|month|day|year)s?\s*(\d+(?:\.\d+)?)\s*\.?\s*', t) or \
            re.fullmatch(r'\s*(?:from\s+)?(?:baseline|day 1|day 0|week 0|randomi[sz]ation)[^0-9]{0,40}(\d+(?:\.\d+)?)\s*(week|month|day|year)s?\s*\.?\s*', t)
        if not m:
            return None, 'event-driven/variable'
    nums = re.findall(r'(week|month|day|year)s?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*-?\s*(week|month|day|year)s?', t)
    vals = []
    for a, b, c, d in nums:
        unit, v = (a, float(b)) if a else (d, float(c))
        vals.append(v * {'week': 1, 'month': 52 / 12, 'day': 1 / 7, 'year': 52}[unit])
    vals = [v for v in vals if v > 0.2]
    if not vals:
        return None, 'event-driven/variable'
    return round(max(vals), 1), 'fixed'


POP = [('ITT', r'\bintent(?:ion)?[- ]to[- ]treat\b|\bITT\b'), ('mITT', r'\bmodified intent|\bmITT\b'),
       ('FAS', r'\bfull analysis\b|\bFAS\b'), ('per-protocol', r'\bper[- ]protocol\b|\bPP (?:set|population)\b|\bPPS\b'),
       ('safety/treated', r'\bsafety (?:population|set|analysis)\b|\breceived at least (?:one|1) dose\b|\ball treated\b'),
       ('all randomized', r'\ball randomi[sz]ed\b|\brandomi[sz]ed (?:participants|patients|subjects|set|population)\b'),
       ('evaluable/completers', r'\bevaluable\b|\bcompleter|\bcompleted\b|\bobserved cases?\b|\bavailable data\b|\bnon-?missing\b|\bwith (?:a )?baseline and\b')]


def pop_tags(text):
    if not (text or '').strip():
        return ['not stated']
    tags = [k for k, rx in POP if re.search(rx, text, re.I)]
    if 'mITT' in tags and 'ITT' in tags:
        tags.remove('ITT')
    return tags or ['other wording']


def norm_effect(p):
    p = (p or '').lower()
    p = re.sub(r'\(.*?\)', ' ', p)
    if not p.strip():
        return '(no estimate type)'
    for k, rx in (('hazard ratio', r'hazard'), ('odds ratio', r'odds'), ('risk/rate ratio', r'risk ratio|relative risk|rate ratio'),
                  ('risk difference', r'risk difference|difference in (?:percent|proportion|response|rate)|percent(?:age)? difference|proportion difference|treatment difference in (?:percent|proportion)'),
                  ('LS mean difference', r'ls ?mean|least[- ]squares? mean'),
                  ('mean difference', r'mean difference|difference in mean|difference of mean|mean treatment difference'),
                  ('median difference', r'median'), ('ratio of means', r'ratio')):
        if re.search(rx, p):
            return k
    if re.search(r'difference|diff', p):
        return 'difference (unspecified)'
    return 'other: ' + p.strip()[:30]


def construct_instrument(fam, m):
    t = (m.get('title') or '') + ' ' + (m.get('description') or '')
    tl = t.lower()
    if fam in ('progression_free_survival', 'overall_survival'):
        if fam == 'progression_free_survival':
            assessor = ('independent/central review' if re.search(r'independent|central|blinded|\birc\b|\bbicr\b|\birrc\b|\biro\b|\birf\b', tl)
                        else 'investigator' if re.search(r'investigator', tl) else 'assessor not stated')
        else:
            assessor = 'death from any cause' if re.search(r'any cause|all[- ]cause', tl) else 'death (cause wording absent)'
        crit = 'none stated'
        for k, rx in (('RECIST 1.1', r'recist[^.;]{0,25}1\.1'), ('RECIST 1.0/unspecified', r'recist'), ('IMWG (myeloma)', r'imwg|international myeloma'),
                      ('iwCLL', r'iwcll|\bcll\b'), ('lymphoma (Cheson/Lugano/IWG)', r'cheson|lugano|\biwg\b|international working group'),
                      ('PCWG (prostate)', r'pcwg'), ('RANO', r'\brano\b'), ('GCIG/CA-125', r'gcig|ca-?125')):
            if re.search(rx, tl):
                crit = k
                break
        return assessor, (crit if fam == 'progression_free_survival' else 'n/a')
    if fam == 'hba1c_change':
        c = ('change from baseline' if re.search(r'change', tl) else 'proportion at target' if re.search(r'percent|proportion|number of', tl) else 'level / other')
        return c, 'laboratory HbA1c'
    if fam == 'fev1':
        c = ('trough / pre-dose' if re.search(r'trough|pre-?dose|pre-?bronchodilator|morning', tl) else
             'AUC / serial' if re.search(r'auc|area under|0-\d+|serial|weighted mean', tl) else
             'peak / post-dose' if re.search(r'peak|post-?dose|post-?bronchodilator|maximum', tl) else 'FEV1 (timing not stated)')
        return c, ('% predicted' if re.search(r'percent predicted|% predicted|ppfev', tl) else 'absolute volume')
    if fam == 'acr20':
        c = 'ACR20 responder proportion'
        if re.search(r'acr\s?-?50|acr\s?-?70', (m.get('title') or '').lower()):
            c = 'ACR20 with ACR50/70 in one measure'
        return c, 'ACR20 (ACR 1995 core set)'
    if fam == 'depression_scale':
        inst = []
        for k, rx in (('MADRS', r'madrs|montgomery'), ('HAM-D 17', r'ham-?d[- ]?17|hdrs[- ]?17|17[- ]item'), ('HAM-D 21', r'ham-?d[- ]?21|21[- ]item'),
                      ('HAM-D 24', r'ham-?d[- ]?24|24[- ]item'), ('HAM-D 6/7', r'ham-?d[- ]?[67]\b|\b[67][- ]item'), ('PHQ-9', r'phq'), ):
            if re.search(rx, tl):
                inst.append(k)
        if not inst and re.search(r'ham-?d|hamilton|hdrs', tl):
            inst = ['HAM-D (version not stated)']
        c = ('change from baseline in total score' if re.search(r'change', tl) else
             'response/remission proportion' if re.search(r'respon|remission|remit|percent|proportion|number of', tl) else
             'time to relapse/event' if re.search(r'time to|relapse', tl) else 'score / other')
        return c, ' + '.join(inst) or 'other'
    return '', ''


def control_type(s):
    arms = ((s.get('protocolSection') or {}).get('armsInterventionsModule') or {}).get('armGroups') or []
    types = {a.get('type') for a in arms}
    if 'PLACEBO_COMPARATOR' in types or 'SHAM_COMPARATOR' in types:
        return 'placebo/sham-controlled' + (' + active comparator' if 'ACTIVE_COMPARATOR' in types else '')
    if 'ACTIVE_COMPARATOR' in types:
        return 'active-controlled'
    if 'NO_INTERVENTION' in types:
        return 'no-intervention control'
    return 'experimental arms only / not typed' if arms else 'arms module not retrieved'


COND = {
    'progression_free_survival': 'onc', 'overall_survival': 'onc',
    'hba1c_change': [('type 2 diabetes', r'type 2|type ii|t2dm|non-insulin'), ('type 1 diabetes', r'type 1|type i\b|t1dm')],
    'fev1': [('asthma', r'asthma'), ('COPD', r'copd|chronic obstructive'), ('cystic fibrosis', r'cystic fibrosis')],
    'acr20': [('rheumatoid arthritis', r'rheumatoid'), ('psoriatic arthritis', r'psoria'), ('other arthritis', r'arthritis|spondyl')],
    'depression_scale': [('treatment-resistant depression', r'resistant|refractory'), ('bipolar depression', r'bipolar'),
                         ('major depressive disorder', r'major depress|depressive disorder|\bmdd\b|depression')],
}
ONC = [('breast', r'breast'), ('lung', r'lung|nsclc|sclc'), ('prostate', r'prostat'), ('colorectal', r'colorectal|colon|rectal'),
       ('myeloma', r'myeloma'), ('lymphoma/leukaemia', r'lymphoma|leuk[a]?emia|\bcll\b'), ('melanoma', r'melanoma'), ('renal', r'renal|kidney'),
       ('ovarian', r'ovar|fallopian|peritoneal'), ('gastric/oesophageal', r'gastric|stomach|esophag|oesophag'), ('pancreatic', r'pancrea'),
       ('hepatocellular', r'hepatocellular|liver'), ('head and neck', r'head and neck'), ('brain', r'gliom|glioblastoma|brain'),
       ('urothelial', r'bladder|urothelial')]


def cond_class(fam, conditions):
    t = '; '.join(conditions or []).lower()
    table = ONC if COND[fam] == 'onc' else COND[fam]
    for k, rx in table:
        if re.search(rx, t):
            return k
    return 'other / unspecified'


out = {}
rows_all = []
for fam, ex in EX.items():
    rx = families.FAMILIES[fam]['endpoint']
    examined = [(n, 'sample') for n in ex['sample_members_rp3']] + [(n, 'drawn') for n in ex['drawn_rp3_not_in_sample']]
    rows, no_measure, not_retrieved = [], [], []
    for n, src in examined:
        s = S.get(n) if src == 'sample' else full.get(n)
        if s is None:
            not_retrieved.append(n)
            continue
        p = proto.get(n) if src == 'sample' else s
        oms = ((s.get('resultsSection') or {}).get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []
        prim = [m for m in oms if m.get('type') == 'PRIMARY']
        hit = [m for m in prim if re.search(rx, m.get('title') or '', re.I)]
        if not hit:
            no_measure.append(n)
            continue
        m = hit[0]
        tw, tkind = time_weeks(m.get('timeFrame'))
        between = [a for a in m.get('analyses') or [] if len(a.get('groupIds') or []) >= 2]
        est_ci = any(is_num(a.get('paramValue')) and is_num(a.get('ciLowerLimit')) and is_num(a.get('ciUpperLimit')) for a in between)
        pval = any(re.search(r'\d', str(a.get('pValue') or '')) for a in between)
        has_data = any(is_num(x.get('value')) for c in m.get('classes') or [] for cat in c.get('categories') or [] for x in cat.get('measurements') or [])
        cons, inst = construct_instrument(fam, m)
        rel = REL.get(n, {})
        rows.append({'nct_id': n, 'source': src, 'family': fam, 'title': m.get('title'), 'n_primary': len(prim), 'n_matching_primary': len(hit),
                     'construct': cons, 'instrument': inst, 'unit': norm_unit(m.get('unitOfMeasure')), 'param': m.get('paramType') or '(none)',
                     'dispersion': m.get('dispersionType') or '(none)', 'time_frame': m.get('timeFrame'), 'time_weeks': tw, 'time_kind': tkind,
                     'population': pop_tags(m.get('populationDescription')), 'n_groups': len(m.get('groups') or []), 'n_classes': len(m.get('classes') or []),
                     'has_data': has_data, 'n_between_analyses': len(between), 'effect_types': sorted({norm_effect(a.get('paramType')) for a in between}),
                     'methods': sorted({(a.get('statisticalMethod') or '(none)') for a in between})[:4],
                     'between_est_ci': est_ci, 'between_p': pval, 'control': control_type(p or {}),
                     'conditions': rel.get('conditions'), 'condition_class': cond_class(fam, rel.get('conditions')),
                     'interventions': sorted({(i.get('name') or '').strip().lower() for i in (((p or {}).get('protocolSection') or {}).get('armsInterventionsModule') or {}).get('interventions') or [] if (i.get('type') or '') in ('DRUG', 'BIOLOGICAL')}), 'sponsor_class': rel.get('sponsor_class'), 'race_reported': rel.get('race_reported'),
                     'results_year': (rel.get('results_date') or '')[:4]})
    def dist(key, flat=False):
        c = collections.Counter()
        for r in rows:
            v = r[key]
            for x in (v if isinstance(v, list) else [v]):
                c[str(x)] += 1
        return c.most_common()
    fixed = [r['time_weeks'] for r in rows if r['time_kind'] == 'fixed']
    out[fam] = {
        'examined': len(examined), 'from_sample': len(ex['sample_members_rp3']), 'drawn': len(ex['drawn_rp3_not_in_sample']),
        'not_retrieved': not_retrieved, 'no_matching_posted_primary_measure': no_measure, 'with_family_measure': len(rows),
        'has_data': sum(r['has_data'] for r in rows), 'construct': dist('construct'), 'instrument': dist('instrument'), 'unit': dist('unit'),
        'param': dist('param'), 'dispersion': dist('dispersion'), 'time_kind': dist('time_kind'),
        'time_weeks_fixed': {'n': len(fixed), 'min': min(fixed) if fixed else None, 'median': statistics.median(fixed) if fixed else None,
                             'max': max(fixed) if fixed else None, 'distinct': len(set(fixed)), 'top': collections.Counter(fixed).most_common(6)},
        'population': dist('population'), 'effect_types': dist('effect_types'), 'no_between_analysis': sum(1 for r in rows if not r['n_between_analyses']),
        'between_est_ci': sum(r['between_est_ci'] for r in rows), 'between_p': sum(r['between_p'] for r in rows),
        'control': dist('control'), 'condition_class': dist('condition_class'),
        'distinct_drug_or_biological_names': len({x for r in rows for x in r['interventions']}), 'n_groups': dist('n_groups'), 'race_reported': sum(1 for r in rows if r['race_reported']),
        'race_reported_and_between_est_ci': sum(1 for r in rows if r['race_reported'] and r['between_est_ci']),
        'industry': sum(1 for r in rows if r['sponsor_class'] == 'INDUSTRY'), 'multiple_primaries': sum(1 for r in rows if r['n_primary'] > 1),
        'results_year': sorted(collections.Counter(r['results_year'] for r in rows).items()),
    }
    rows_all += rows
json.dump(out, open(os.path.join(OUT, 'family_detail.json'), 'w'), indent=1)
with open(os.path.join(OUT, 'family_detail_rows.jsonl'), 'w') as f:
    for r in rows_all:
        f.write(json.dumps(r) + '\n')
for fam, v in out.items():
    print('\n==', fam)
    for k, val in v.items():
        print('  ', k, ':', str(val)[:520])
