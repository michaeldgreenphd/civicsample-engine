"""Apply the subgroup rule to the 2,000 sampled trials, draw the negative review samples, write digests.

Run: python3 -I scripts/subgroup_detect.py V1_DIR OUTDIR
Writes OUTDIR/subgroup_detected.jsonl (every signal of every detected trial),
OUTDIR/subgroup_negative_samples.json (the two negative samples with their selection scores) and
OUTDIR/review/{positives,negatives}_digest.txt (what the reviewer read).

NEGATIVE SAMPLES (both drawn from the trials with no signal):
  random    60 trials: random.Random(20261113).sample(sorted(non_detected_ids), 60).
  targeted  40 trials: the highest "likely reporter" scores among the remaining non-detected trials;
            ties broken by a random.Random(20261113) shuffle of the sorted IDs. Score =
              2 if the release phase contains PHASE3
            + 2 if enrolment >= 500 (+1 more if >= 1000)
            + 1 if any measure has >= 6 classes   + 1 if any measure has >= 4 groups
            + 2 if any measure title contains "stratified" or the word "by"
            + 2 if any class, category or group title, or any analysis groupDescription, contains a
                loose demographic mention (MENTION below) that the rule did not turn into a signal
            + 1 if any analysis free text contains "interaction" or "stratif".
"""
import sys, os, json, re, random, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import subgroup_rule as R

V1, OUT = sys.argv[1], sys.argv[2]
S, _ = common.load_sample(V1)
REL = common.load_release(OUT)
ids = [l.strip() for l in open(os.path.join(V1, 'sample_ids.txt')) if l.strip()]
os.makedirs(os.path.join(OUT, 'review'), exist_ok=True)

MENTION = re.compile(r'\b(sex|gender|males?|females?|m[ae]n|wom[ae]n|boys?|girls?|rac(?:e|ial)|ethnic\w*|hispanic|latin[oax]|'
                     r'black|african[- ]american|caucasian|asian|japanese|chinese|elderly|adolescents?|children|pediatric|'
                     r'age group|aged|years of age|years old|younger|older)\b', re.I)
LOOSE = re.compile(r'stratif\w*|interaction|\b(sex|gender|males?|females?|wom[ae]n|rac(?:e|ial)|ethnic\w*|hispanic|black|'
                   r'caucasian|asian|japanese|elderly|age groups?|years of age|aged \d|younger|older|by age)\b', re.I)


def measures(s):
    return ((s.get('resultsSection') or {}).get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []


detected = {}
for n in ids:
    sigs = []
    for i, m in enumerate(measures(S[n])):
        for sg in R.detect_measure(m):
            sg.update(measure_index=i, measure_type=m.get('type'), measure_title=m.get('title'))
            sigs.append(sg)
    if sigs:
        detected[n] = sigs
with open(os.path.join(OUT, 'subgroup_detected.jsonl'), 'w') as f:
    for n in sorted(detected):
        f.write(json.dumps({'nct_id': n, 'signals': detected[n]}) + '\n')

non = sorted(set(ids) - set(detected))
rng = random.Random(20261113)
random60 = sorted(rng.sample(non, 60))


def score(n):
    s, rel = S[n], REL.get(n, {})
    ms = measures(s)
    sc, why = 0, []
    if 'PHASE3' in (rel.get('phase') or ''):
        sc += 2; why.append('phase3')
    en = rel.get('enrollment') or 0
    if en >= 500:
        sc += 2; why.append('n>=500')
    if en >= 1000:
        sc += 1; why.append('n>=1000')
    if any(len(m.get('classes') or []) >= 6 for m in ms):
        sc += 1; why.append('classes>=6')
    if any(len(m.get('groups') or []) >= 4 for m in ms):
        sc += 1; why.append('groups>=4')
    if any(re.search(r'\bstratified\b|\bby\b', m.get('title') or '', re.I) for m in ms):
        sc += 2; why.append('title:by/stratified')
    def titles(m):
        for c in m.get('classes') or []:
            yield c.get('title') or ''
            for cat in c.get('categories') or []:
                yield cat.get('title') or ''
        for g in m.get('groups') or []:
            yield g.get('title') or ''
        for a in m.get('analyses') or []:
            yield a.get('groupDescription') or ''
    if any(MENTION.search(t) for m in ms for t in titles(m)):
        sc += 2; why.append('loose-mention')
    if any(re.search(r'interaction|stratif', a.get(k) or '', re.I) for m in ms for a in m.get('analyses') or [] for k in R.ANALYSIS_TEXT_FIELDS):
        sc += 1; why.append('analysis:interaction/stratif')
    return sc, why


rest = [n for n in non if n not in set(random60)]
rng2 = random.Random(20261113)
order = sorted(rest)
rng2.shuffle(order)
scored = {n: score(n) for n in rest}
targeted = sorted(order, key=lambda n: -scored[n][0])[:40]
cut = scored[targeted[-1]][0]
json.dump({'n_detected': len(detected), 'n_non_detected': len(non), 'random60': random60,
           'targeted40': [{'nct_id': n, 'score': scored[n][0], 'why': scored[n][1]} for n in targeted],
           'targeted_cutoff_score': cut, 'n_rest_at_or_above_cutoff': sum(1 for n in rest if scored[n][0] >= cut),
           'score_distribution_rest': dict(sorted(collections.Counter(scored[n][0] for n in rest).items())),
           'seed': 20261113}, open(os.path.join(OUT, 'subgroup_negative_samples.json'), 'w'), indent=1)


def clip(t, k):
    t = re.sub(r'\s+', ' ', t or '').strip()
    return t if len(t) <= k else t[:k] + '…'


with open(os.path.join(OUT, 'review', 'positives_digest.txt'), 'w') as f:
    for n in sorted(detected):
        rel = REL.get(n, {})
        ms = measures(S[n])
        f.write(f"\n### {n} | {rel.get('phase')} | n={rel.get('enrollment')} | {rel.get('primary_condition')} | measures={len(ms)}\n")
        seen = set()
        k = 0
        for sg in detected[n]:
            key = (sg['field'], sg['dimension'], sg['text'][:60])
            if key in seen:
                continue
            seen.add(key)
            k += 1
            if k > 7:
                f.write(f"   … {len(detected[n])} signals in total\n")
                break
            m = ms[sg['measure_index']]
            cls = [c.get('title') or '' for c in m.get('classes') or []]
            f.write(f"  [{sg['field']}|{sg['dimension']}] {sg['measure_type']} «{clip(sg['measure_title'], 110)}» :: {clip(sg['text'], 230)}\n")
            f.write(f"      groups={[clip(g.get('title'), 28) for g in (m.get('groups') or [])[:6]]} classes({len(cls)})={[clip(c, 32) for c in cls[:6]]} data={'Y' if any(common.is_num(x.get('value')) for c in m.get('classes') or [] for cat in c.get('categories') or [] for x in cat.get('measurements') or []) else 'N'} analyses={len(m.get('analyses') or [])}\n")

with open(os.path.join(OUT, 'review', 'negatives_digest.txt'), 'w') as f:
    for kind, lst in (('random', random60), ('targeted', targeted)):
        for n in lst:
            rel = REL.get(n, {})
            ms = measures(S[n])
            f.write(f"\n### {n} [{kind}] {rel.get('phase')} | n={rel.get('enrollment')} | {rel.get('primary_condition')} | measures={len(ms)}" + (f" | score={scored[n][0]} {scored[n][1]}" if kind == 'targeted' else '') + '\n')
            gt = list(dict.fromkeys(clip(g.get('title'), 40) for m in ms for g in m.get('groups') or []))
            ct = list(dict.fromkeys(clip(c.get('title'), 45) for m in ms for c in m.get('classes') or [] if c.get('title')))
            kt = list(dict.fromkeys(clip(cat.get('title'), 30) for m in ms for c in m.get('classes') or [] for cat in c.get('categories') or [] if cat.get('title')))
            f.write(f"  groups({len(gt)}): {gt[:8]}\n")
            if ct:
                f.write(f"  classes({len(ct)}): {ct[:14]}\n")
            if kt:
                f.write(f"  categories({len(kt)}): {kt[:8]}\n")
            bt = [clip(m.get('title'), 120) for m in ms if re.search(r'\bstratified\b|\bby\b', m.get('title') or '', re.I)]
            if bt:
                f.write(f"  by/stratified titles({len(bt)}): {bt[:4]}\n")
            snips, seen = [], set()
            for i, m in enumerate(ms):
                fields = [('title', m.get('title')), ('desc', m.get('description')), ('pop', m.get('populationDescription'))]
                fields += [('class', c.get('title')) for c in m.get('classes') or []]
                fields += [('cat', cat.get('title')) for c in m.get('classes') or [] for cat in c.get('categories') or []]
                fields += [('gtitle', g.get('title')) for g in m.get('groups') or []]
                fields += [('gdesc', g.get('description')) for g in m.get('groups') or []]
                for a in m.get('analyses') or []:
                    fields += [('an.' + k, a.get(k)) for k in R.ANALYSIS_TEXT_FIELDS if k != 'statisticalMethod']
                for fld, t in fields:
                    if not t:
                        continue
                    mm = LOOSE.search(t)
                    if mm:
                        sn = clip(t[max(0, mm.start() - 70): mm.end() + 90], 170)
                        key = (fld, sn[:50])
                        if key not in seen:
                            seen.add(key)
                            snips.append(f"{fld}: {sn}")
            for sn in snips[:7]:
                f.write(f"    ~ {sn}\n")
            if len(snips) > 7:
                f.write(f"    ~ (+{len(snips) - 7} more loose mentions)\n")
print('detected', len(detected), 'non', len(non), 'cut', cut)
print(collections.Counter(scored[n][0] for n in targeted))
