"""Candidate endpoint families counted on the FULL release (80,890 records).

Run: python3 -I scripts/families.py OUTDIR
A family is (endpoint regex on `primary_endpoint`) AND (condition gate). `primary_endpoint` is the title
of the FIRST registered primary outcome only (get_study_metadata takes outcomes[0]); a trial whose
family endpoint is its second or later primary, or a secondary, is not counted. Matching is lexical:
it finds titles that name the construct, it does not establish that two matched trials measured it the
same way (that is what the per-family examination is for).

Regexes are Python `re`, case-insensitive unless marked (?-i:...). The condition gate is applied to
`primary_condition` (the engine's category) or to the "; "-joined `conditions` list, as stated per family.
Columns: matched = endpoint regex; gated = matched AND gate; rp3 = gated AND allocation == "RANDOMIZED"
AND phase == "PHASE3" (Phase 2/3 is not counted, mirroring outcomes/evidence.py); race = gated AND
race.reported is True; both = rp3 AND race.reported.
"""
import sys, os, json, re, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


FAMILIES = collections.OrderedDict([
    ('overall_survival', {
        'endpoint': r'\boverall survival\b|(?-i:\bOS\b)',
        'gate_field': 'primary_condition', 'gate': r'^Oncology$'}),
    ('progression_free_survival', {
        'endpoint': r'\bprogression[- ]free survival\b|(?-i:\bPFS\b)',
        'gate_field': 'primary_condition', 'gate': r'^Oncology$'}),
    ('objective_response_rate', {
        'endpoint': r'\b(?:objective|overall|best overall) (?:tumou?r )?response\b|(?-i:\bORR\b)',
        'gate_field': 'primary_condition', 'gate': r'^Oncology$'}),
    ('hba1c_change', {
        'endpoint': r'\bhb\s?a1c\b|\ba1c\b|\bglyc(?:osyl)?ated ha?emoglobin\b',
        'gate_field': 'conditions', 'gate': r'diabet'}),
    ('ldl_cholesterol', {
        'endpoint': r'(?-i:\bLDL\b)|\blow[- ]density lipoprotein\b',
        'gate_field': 'conditions', 'gate': r'cholesterol|lipid|lipoprotein'}),
    ('systolic_blood_pressure', {
        'endpoint': r'\bsystolic\b|(?-i:\bSBP\b)|\bblood pressure\b',
        'gate_field': 'conditions', 'gate': r'hypertens'}),
    ('fev1', {
        'endpoint': r'\bFEV\s?-?1\b|\bforced expiratory volume\b',
        'gate_field': 'conditions', 'gate': r'asthma|copd|chronic obstructive|cystic fibrosis'}),
    ('depression_scale', {
        'endpoint': r'\bHAM-?D\b|\bHDRS\b|\bhamilton\b[^.;]{0,30}\bdepression\b|\bMADRS\b|\bmontgomery[- ][aå]sberg\b|\bPHQ-?9\b|\bpatient health questionnaire\b',
        'gate_field': 'conditions', 'gate': r'depress'}),
    ('pain_intensity', {
        'endpoint': r'(?=.*\bpain\b)(?:.*\b(?:intensity|VAS|visual analog(?:ue)? scale|NRS|NPRS|numeric(?:al)? (?:pain )?rating scale|pain scores?)\b)',
        'gate_field': 'primary_condition', 'gate': r'^(?:Pain|Musculoskeletal)$'}),
    ('acr20', {
        'endpoint': r'\bACR\s?-?20\b|\bamerican college of rheumatology\b[^.;]{0,25}\b20\b',
        'gate_field': 'conditions', 'gate': r'arthritis'}),
    ('pasi75', {
        'endpoint': r'\bPASI\s?-?75\b|\bpsoriasis area (?:and|&) severity index\b[^.;]{0,60}\b75\b|\b75\s?%[^.;]{0,60}\bPASI\b',
        'gate_field': 'conditions', 'gate': r'psoria'}),
    ('svr_or_viral_load', {
        'endpoint': r'\bsustained vir(?:ologic(?:al)?|al) response\b|(?-i:\bSVR\s?\d*\b)|\bviral load\b|\b(?:HIV|HCV)(?:-1)? RNA\b|\bHBV DNA\b|\bvirologic(?:al)? (?:response|suppression|failure|success)\b',
        'gate_field': 'conditions', 'gate': r'\bhiv\b|human immunodeficiency|hepatitis|\bhcv\b|\bhbv\b'}),
    ('svr_hcv', {
        'endpoint': r'\bsustained vir(?:ologic(?:al)?|al) response\b|(?-i:\bSVR\s?\d*\b)',
        'gate_field': 'conditions', 'gate': r'hepatitis c|\bhcv\b'}),
    ('hiv_rna', {
        'endpoint': r'\bHIV(?:-1)? RNA\b|\bviral load\b|\bvirologic(?:al)? (?:response|suppression|failure|success)\b',
        'gate_field': 'conditions', 'gate': r'\bhiv\b|human immunodeficiency'}),
    ('body_weight_change', {
        'endpoint': r'\bbody weight\b|\bweight (?:loss|change|reduction)\b|\bchange in weight\b|\b(?:change|reduction|loss)\b[^.;]{0,25}\bweight\b',
        'gate_field': 'conditions', 'gate': r'obes|overweight'}),
    ('seroconversion_or_titre', {
        'endpoint': r'\bsero(?:conver|protect|respon)\w*|\bantibody tit(?:er|re)s?\b|\bgeometric mean (?:tit(?:er|re)|concentration)s?\b|(?-i:\bGM[TC]s?\b)|\bha?emagglutination[- ]inhibit\w*|\bimmunogenicity\b|\bneutrali[sz]ing antibod\w*',
        'gate_field': None, 'gate': None}),
])


def gate_text(r, field):
    if field == 'primary_condition':
        return r.get('primary_condition') or ''
    if field == 'conditions':
        return '; '.join(r.get('conditions') or [])
    return ''


def member(r, fam, gated=True):
    f = FAMILIES[fam]
    if not re.search(f['endpoint'], r.get('primary_endpoint') or '', re.I):
        return False
    if gated and f['gate']:
        return bool(re.search(f['gate'], gate_text(r, f['gate_field']), re.I))
    return True


def is_rp3(r):
    return r.get('allocation') == 'RANDOMIZED' and r.get('phase') == 'PHASE3'


if __name__ == '__main__':
    OUT = sys.argv[1]
    REL = common.load_release(OUT)
    out = {'n_release': len(REL), 'families': {}}
    members = {}
    for fam, f in FAMILIES.items():
        m = [r for r in REL.values() if member(r, fam, gated=False)]
        g = [r for r in m if member(r, fam)]
        rp3 = [r for r in g if is_rp3(r)]
        race = [r for r in g if r.get('race_reported') is True]
        both = [r for r in rp3 if r.get('race_reported') is True]
        members[fam] = sorted(r['nct_id'] for r in g)
        out['families'][fam] = {
            'endpoint_regex': f['endpoint'], 'gate_field': f['gate_field'], 'gate_regex': f['gate'],
            'matched': len(m), 'gated': len(g), 'rp3': len(rp3), 'race_reported': len(race), 'rp3_and_race_reported': len(both),
            'rp3_industry': sum(1 for r in rp3 if r.get('sponsor_class') == 'INDUSTRY'),
            'rp3_and_race_industry': sum(1 for r in both if r.get('sponsor_class') == 'INDUSTRY'),
            'phase2_3_randomized': sum(1 for r in g if r.get('allocation') == 'RANDOMIZED' and r.get('phase') == 'PHASE2, PHASE3'),
            'distinct_normalised_titles_gated': len({re.sub(r'[^a-z0-9]+', ' ', (r.get('primary_endpoint') or '').lower()).strip() for r in g}),
            'distinct_normalised_titles_rp3': len({re.sub(r'[^a-z0-9]+', ' ', (r.get('primary_endpoint') or '').lower()).strip() for r in rp3}),
            'rp3_results_year_min_max': [min((r['results_date'] or '')[:4] for r in rp3) if rp3 else None, max((r['results_date'] or '')[:4] for r in rp3) if rp3 else None],
            'examples_rp3': [r['primary_endpoint'][:110] for r in sorted(rp3, key=lambda r: r['nct_id'])[:3]],
        }
    any_fam = set().union(*[set(v) for k, v in members.items() if k not in ('svr_hcv', 'hiv_rna')])
    out['any_family_gated'] = len(any_fam)
    out['rp3_total_in_release'] = sum(1 for r in REL.values() if is_rp3(r))
    out['rp3_race_total_in_release'] = sum(1 for r in REL.values() if is_rp3(r) and r.get('race_reported') is True)
    out['rp3_in_any_family'] = sum(1 for n in any_fam if is_rp3(REL[n]))
    # exact-title lexical uniqueness on the full release (kept as a lexical finding)
    norm = collections.Counter(re.sub(r'[^a-z0-9]+', ' ', (r.get('primary_endpoint') or '').lower()).strip() for r in REL.values())
    out['exact_title'] = {'distinct_normalised': len(norm), 'used_once': sum(1 for v in norm.values() if v == 1),
                          'trials_with_unique_title': sum(1 for v in norm.values() if v == 1),
                          'top10': norm.most_common(10)}
    json.dump(out, open(os.path.join(OUT, 'families.json'), 'w'), indent=1)
    json.dump(members, open(os.path.join(OUT, 'family_members.json'), 'w'))
    print(f"{'family':28s} {'matched':>8s} {'gated':>7s} {'rp3':>6s} {'race':>6s} {'both':>6s} {'both_ind':>8s} titles_rp3")
    for fam, v in out['families'].items():
        print(f"{fam:28s} {v['matched']:8d} {v['gated']:7d} {v['rp3']:6d} {v['race_reported']:6d} {v['rp3_and_race_reported']:6d} {v['rp3_and_race_industry']:8d} {v['distinct_normalised_titles_rp3']}")
    print({k: v for k, v in out.items() if k not in ('families',)})
