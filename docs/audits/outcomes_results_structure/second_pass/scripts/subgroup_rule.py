r"""The subgroup-detection rule (version 2). Pure functions; no I/O. Imported by subgroup_detect.py.

A "signal" is (matched_field, dimension, matched_text, measure_index). A trial is DETECTED when any of
its posted outcome measures yields at least one signal. Detection is a text rule: it says where the
registrant's wording looks like subgroup reporting, not that a subgroup result exists (that is what the
review step judges).

FIELDS INSPECTED, per `resultsSection.outcomeMeasuresModule.outcomeMeasures[]` entry (all types):
  title; description; populationDescription;
  classes[].title; classes[].categories[].title;
  groups[].title; groups[].description;
  analyses[].groupIds (through the groups they reference); analyses[].groupDescription;
  analyses[].statisticalComment; analyses[].otherAnalysisDescription; analyses[].nonInferiorityComment;
  analyses[].pValueComment; analyses[].estimateComment; analyses[].ciLowerLimitComment;
  analyses[].ciUpperLimitComment; analyses[].statisticalMethod;
  classes[].categories[].measurements[].comment.
NOT inspected: timeFrame, unitOfMeasure, paramType (not places a subgroup is named); baseline, flow and
adverse-event modules; moreInfoModule; protocol-section text; posted documents.

LEVEL RULES (two or more levels of one dimension among sibling titles of ONE measure):
  applied separately to (a) the set of class titles, (b) the set of category titles, (c) the set of
  group titles. A dimension fires when:
   sex        one title matches FEMALE and not MALE, and a different title matches MALE and not FEMALE
   race       two titles carry different RACE tokens (after removing the excluded phrases)
   ethnicity  one title matches HISPANIC and there are 2+ titles in the set
   origin     one title matches ORIGIN (Japanese / Chinese / Korean ...) and another title matches
              NON-ORIGIN or a different ORIGIN token
   age        two titles carry different AGE tokens (explicit age wording with a number, a comparator
              with years/months, or different life-stage words)
  matched_field = class_titles | category_titles | group_titles
  (d) group descriptions: the same sex and age rules applied to groups[].description, used only when
      the group titles did not fire. matched_field = group_descriptions
  (e) analysis group reference: an analysis whose groupIds reference 2+ groups carrying different
      levels under (c)/(d). matched_field = analysis_groupIds (reported as an attribute of (c)/(d))
CUE RULES (single text):
   KEYWORD     \bsub-?\s?(group|population|set|sets|cohort)s?\b  or  \bsubpopulations?\b  or  \bsubsets?\b
   BY_DEMOG    "by|according to|per|stratified by|within|across" + (sex|gender|race|ethnic...|age group...)
               or "(sex|gender|race|ethnic|age)-specific|-stratified|-based subgroup"
  applied to: measure title (measure_title), description (measure_description), populationDescription
  (population_description), each class/category title (class_title_cue / category_title_cue), each
  analysis free-text field (analysis_text:<field>), each measurement comment (measurement_comment).
  "stratified by <non-demographic>", "adjusted for age/sex" and covariate lists are NOT cues: they
  describe the model, not a subgroup result (the negative review checks what this exclusion misses).
"""
import re

I = re.IGNORECASE
FEMALE = re.compile(r'\b(females?|wom[ae]n|girls?)\b', I)
MALE = re.compile(r'\b(males?|m[ae]n|boys?)\b', I)
RACE_EXCLUDE = re.compile(r'white blood|white cell|white matter|white coat|white light|white noise|black box|blackout|'
                          r'asian flu|non-?hodgkin', I)
RACE_TOKENS = [
    ('white', re.compile(r'\b(white|caucasian)s?\b', I)),
    ('black', re.compile(r'\b(black|african[- ]americans?)\b', I)),
    ('asian', re.compile(r'\basians?\b', I)),
    ('aian', re.compile(r'american indian|alaska native', I)),
    ('nhpi', re.compile(r'native hawaiian|pacific islander', I)),
    ('multi', re.compile(r'multiracial|more than one race|multiple races', I)),
    ('nonwhite', re.compile(r'\bnon[- ]?(white|caucasian)\b', I)),
    ('nonblack', re.compile(r'\bnon[- ]?black\b', I)),
    ('nonasian', re.compile(r'\bnon[- ]?asian\b', I)),
    ('other_race', re.compile(r'\bother races?\b', I)),
]
HISPANIC = re.compile(r'\b(hispanic|latin[oax]s?|latine)\b', I)
ORIGIN = re.compile(r'\b(japanese|chinese|korean|taiwanese|indian)\b', I)
NON_ORIGIN = re.compile(r'\bnon[- ]?(japanese|chinese|korean|taiwanese|asian)\b|\brest of (the )?world\b', I)
AGE_BAND = re.compile(
    r'(?:[<>≤≥]\s?=?\s?\d{1,3}(?:\.\d+)?\s?(?:years?|yrs?|y\.?o\.?|months?|mos?)\b)'
    r'|(?:\bage[ds]?\b[^,;()]{0,25}?\d{1,3})'
    r'|(?:\d{1,3}\s?(?:-|–|to|through)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)\b)'
    r'|(?:\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)(?:\s?(?:and|or)\s?(?:older|younger|above|below|over|under))?)'
    r'|(?:\d{1,3}\s?(?:-|–|to)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?)\b)', I)
LIFE_STAGE = [
    ('infant', re.compile(r'\b(infants?|neonates?|newborns?|toddlers?)\b', I)),
    ('child', re.compile(r'\b(child|children|pediatrics?|paediatrics?)\b', I)),
    ('adolescent', re.compile(r'\b(adolescents?|teens?|teenagers?)\b', I)),
    ('adult', re.compile(r'\b(adults?)\b', I)),
    ('older', re.compile(r'\b(elderly|older adults?|seniors?|geriatrics?)\b', I)),
]
KEYWORD = re.compile(r'\bsub-?\s?(?:groups?|populations?|sets?|cohorts?)\b|\bsubpopulations?\b|\bsubsets?\b', I)
DEMOG_WORD = r'(?:sex|gender|race|racial|ethnic(?:ity)?(?: group)?|age(?: group| category| cohort| stratum| strata| range| band)?s?)'
BY_DEMOG = re.compile(r'\b(?:by|according to|per|stratified by|within|across|for each)\s+(?:the\s+)?(?:participant\s+|patient\s+|subject\s+)?'
                      + DEMOG_WORD + r'\b(?!\s*(?:of|at)\b)'
                      r'|\b(?:sex|gender|race|ethnic|age)[- ](?:specific|stratified|based subgroups?)\b', I)
# "per age" in "per age-appropriate", "by age of onset" etc. are removed after matching:
BY_DEMOG_EXCLUDE = re.compile(r'age[- ](?:appropriate|adjusted|matched|related|standardi[sz]ed|corrected|equivalent|at\b|of\b)|'
                              r'(?:adjusted|corrected|standardi[sz]ed|normali[sz]ed|matched) (?:for|by) (?:age|sex|gender)|'
                              r'for age\b|sex[- ]hormone|gender[- ]affirm|sex(?:ual)? (?:partner|behavio|activity|function)', I)


def race_tokens(t):
    t2 = RACE_EXCLUDE.sub(' ', t)
    found = set()
    for name, rx in RACE_TOKENS:
        if rx.search(t2):
            found.add(name)
    # "non-white" also matches "white": keep only the more specific token
    for a, b in (('nonwhite', 'white'), ('nonblack', 'black'), ('nonasian', 'asian')):
        if a in found:
            found.discard(b)
    return found


def age_token(t):
    m = AGE_BAND.search(t)
    if m:
        return 'band:' + re.sub(r'\s+', '', m.group(0).lower())
    stages = [n for n, rx in LIFE_STAGE if rx.search(t)]
    if len(stages) == 1:
        return 'stage:' + stages[0]
    return None


def level_dimensions(titles, dims=('sex', 'race', 'ethnicity', 'origin', 'age')):
    """Dimensions for which the sibling titles show 2+ levels. Returns {dimension: [titles that matched]}."""
    titles = [t for t in dict.fromkeys(t.strip() for t in titles if t and t.strip())]
    out = {}
    if len(titles) < 2:
        return out
    if 'sex' in dims:
        f = [t for t in titles if FEMALE.search(t) and not MALE.search(t)]
        m = [t for t in titles if MALE.search(t) and not FEMALE.search(t)]
        if f and m:
            out['sex'] = f[:2] + m[:2]
    if 'race' in dims:
        tok = {t: race_tokens(t) for t in titles}
        distinct = set().union(*tok.values()) if tok else set()
        carriers = [t for t in titles if tok[t]]
        if len(distinct) >= 2 and len(carriers) >= 2:
            out['race'] = carriers[:4]
    if 'ethnicity' in dims:
        h = [t for t in titles if HISPANIC.search(t)]
        if h:
            out['ethnicity'] = h[:3]
    if 'origin' in dims:
        o = [t for t in titles if ORIGIN.search(t) or NON_ORIGIN.search(t)]
        toks = set()
        for t in o:
            toks.add('non' if NON_ORIGIN.search(t) else ORIGIN.search(t).group(0).lower())
        if len(o) >= 2 and len(toks) >= 2:
            out['origin'] = o[:4]
    if 'age' in dims:
        tok = {t: age_token(t) for t in titles}
        carriers = [t for t in titles if tok[t]]
        if len({tok[t] for t in carriers}) >= 2:
            out['age'] = carriers[:4]
    return out


def cue(text):
    """(kind, snippet) for the first cue in a text, or None."""
    if not text:
        return None
    m = KEYWORD.search(text)
    kind = 'keyword'
    if not m:
        for mm in BY_DEMOG.finditer(text):
            ctx = text[max(0, mm.start() - 25): mm.end() + 25]
            if not BY_DEMOG_EXCLUDE.search(ctx):
                m, kind = mm, 'by_demographic'
                break
    if not m:
        return None
    return kind, text[max(0, m.start() - 90): m.end() + 110].replace('\n', ' ').strip()


ANALYSIS_TEXT_FIELDS = ('groupDescription', 'statisticalComment', 'otherAnalysisDescription', 'nonInferiorityComment',
                        'pValueComment', 'estimateComment', 'ciLowerLimitComment', 'ciUpperLimitComment', 'statisticalMethod')


def detect_measure(m):
    """All signals for one outcome measure: list of dicts(field, dimension, text)."""
    sig = []
    classes = m.get('classes') or []
    class_titles = [c.get('title') or '' for c in classes]
    cat_titles = [cat.get('title') or '' for c in classes for cat in c.get('categories') or []]
    groups = m.get('groups') or []
    gt = [g.get('title') or '' for g in groups]
    for field, titles in (('class_titles', class_titles), ('category_titles', cat_titles), ('group_titles', gt)):
        for dim, hits in level_dimensions(titles).items():
            sig.append({'field': field, 'dimension': dim, 'text': ' | '.join(h[:70] for h in hits)})
    group_level = {s['dimension'] for s in sig if s['field'] == 'group_titles'}
    gd = [g.get('description') or '' for g in groups]
    for dim, hits in level_dimensions(gd, dims=('sex', 'age')).items():
        if dim not in group_level:
            sig.append({'field': 'group_descriptions', 'dimension': dim, 'text': ' | '.join(h[:110] for h in hits[:2])})
    # analysis group reference: an analysis comparing groups that carry different levels
    lvl_dims = {s['dimension'] for s in sig if s['field'] in ('group_titles', 'group_descriptions')}
    if lvl_dims:
        by_id = {g['id']: (g.get('title') or '') + ' ' + (g.get('description') or '') for g in groups}
        for a in m.get('analyses') or []:
            ref = [by_id.get(i, '') for i in a.get('groupIds') or []]
            if len(ref) >= 2 and level_dimensions(ref, dims=tuple(lvl_dims)):
                sig.append({'field': 'analysis_groupIds', 'dimension': '+'.join(sorted(lvl_dims)),
                            'text': 'analysis compares level-defined groups: ' + ' vs '.join(r[:40] for r in ref[:3])})
                break
    for field, text in (('measure_title', m.get('title')), ('measure_description', m.get('description')),
                        ('population_description', m.get('populationDescription'))):
        c = cue(text)
        if c:
            sig.append({'field': field, 'dimension': c[0], 'text': c[1]})
    for field, titles in (('class_title_cue', class_titles), ('category_title_cue', cat_titles)):
        for t in titles:
            c = cue(t)
            if c:
                sig.append({'field': field, 'dimension': c[0], 'text': c[1]})
                break
    for a in m.get('analyses') or []:
        for k in ANALYSIS_TEXT_FIELDS:
            c = cue(a.get(k))
            if c:
                sig.append({'field': 'analysis_text:' + k, 'dimension': c[0], 'text': c[1]})
    done = False
    for cl in classes:
        for cat in cl.get('categories') or []:
            for x in cat.get('measurements') or []:
                c = cue(x.get('comment'))
                if c and not done:
                    sig.append({'field': 'measurement_comment', 'dimension': c[0], 'text': c[1]})
                    done = True
    return sig
