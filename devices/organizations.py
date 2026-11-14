"""Manufacturer names: the raw string, a normalised form by documented rules, and curated aliases.

Three things are kept apart and none overwrites another:

  raw          the string exactly as the source row carries it (never edited)
  normalised   a key from mechanical rules only: case, accents, punctuation, legal-form tokens.
               Two raw strings with the same key are treated as spellings of one name.
  canonical    the organisation a key belongs to. It equals the key unless
               devices/organization_aliases.csv says otherwise. That file is the ONLY way two
               differently named organisations are merged; no similarity score, shared word,
               prefix or "brand" heuristic merges anything.

Why so strict: the audit that preceded this module found that looser keys merged unrelated firms
("Pearl Inc." with "Pearl Therapeutics", "Exo Inc" with "EXO Biologics"), and that typos in the
source list ("Viz.Al, Inc." for Viz.ai) defeat any mechanical rule. Both are curation problems.

Known over-merge that the rules still make, recorded rather than hidden: removing legal forms makes
"Metamorphosis GmbH" and "Metamorphosis LTD" one key. normalise() therefore returns the legal-form
tokens it removed, and match_tier() reports `normalised_legal_form_differs` when two strings agree
only after different legal forms were dropped.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Any

ALIAS_COLUMNS = ['alias_normalised', 'canonical_normalised', 'canonical_display_name', 'valid_from', 'valid_to',
                 'source', 'note']

# Tokens that state a legal form and carry no identity. Words such as "medical", "healthcare",
# "systems" or a country are NOT here: "Siemens Medical Solutions USA" and "Siemens Healthcare
# GmbH" are different legal entities, and joining them is a curated alias decision.
LEGAL_FORM_TOKENS = frozenset({
    'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp', 'corporation', 'co', 'company', 'gmbh', 'ag', 'sa',
    'sas', 'sarl', 'srl', 'spa', 'bv', 'nv', 'ab', 'as', 'oy', 'aps', 'kk', 'plc', 'pty', 'pte', 'lp', 'llp', 'kg',
    'se', 'sl', 'mbh', 'pvt', 'private', 'publ',
})
# Connectives dropped with the legal forms ("Co., Ltd." leaves none; "X and Company" leaves "x").
_CONNECTIVES = frozenset({'the', 'and', 'of'})


def ascii_fold(s: str) -> str:
    """Accents removed (NFKD), non-ASCII dropped. Assumption: no two manufacturers on the list
    differ only by an accent or by non-Latin script; none does in the 2026-03-05 list."""
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')


def clean_display(s: str) -> str:
    """The raw string with control characters removed and whitespace collapsed, for display and
    for search queries only. The source list carries C0/C1 control characters (cp1252 trademark,
    quote and dash bytes decoded as Latin-1, and a few tabs and newlines inside cells)."""
    s = ''.join(ch for ch in (s or '') if not (ord(ch) < 32 or 127 <= ord(ch) < 160))
    s = s.replace('®', '').replace('™', '')
    return re.sub(r'\s+', ' ', s).strip()


def normalise(raw: str) -> dict[str, Any]:
    """The normalised key of a manufacturer or sponsor string, with what was removed to reach it.

    Rules, in order (each is an assumption about what does not distinguish two organisations):
      1. control characters and trademark signs removed; accents folded; lower case.
      2. a "d/b/a ..." tail is cut: the registered name is kept, the trading name recorded. A
         "c/o ..." tail is cut the same way (the name after it is an agent, not the organisation).
      3. parenthesised text is dropped and recorded (a parent, a city, a former name); whether it
         identifies the same organisation is an alias-file decision.
      4. initials written with dots or a slash are joined ("S.A." -> "sa", "B.V" -> "bv",
         "U.S.A." -> "usa", "A/S" -> "as") so that a legal form written that way is recognised as
         one token. Other single letters are kept: they carry identity ("Q Bio", "X-Nav", "B-Secur").
      5. "&" becomes "and"; every other non-alphanumeric character becomes a space.
      6. legal-form tokens and the connectives the/and/of are dropped, wherever they stand.
    A name made only of dropped tokens keeps all its tokens (it is not reduced to nothing).
    """
    s = clean_display(raw)
    removed: dict[str, Any] = {'legal_form_tokens': [], 'parenthesised': [], 'dba': None, 'care_of': None}
    for key, pattern in (('dba', r'\bd/b/a\b(.*)$'), ('care_of', r'\bc/o\b(.*)$')):
        m = re.search(pattern, s, flags=re.I)
        if m:
            removed[key] = m.group(1).strip() or None
            s = s[:m.start()]
    removed['parenthesised'] = [x.strip() for x in re.findall(r'\(([^)]*)\)', s) if x.strip()]
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = ascii_fold(s).lower()
    s = re.sub(r'\b(?:[a-z]\.)+[a-z]\b\.?|\b[a-z]/[a-z]\b', lambda mm: re.sub(r'[./]', '', mm.group(0)) + ' ', s)
    s = s.replace('&', ' and ')
    tokens = re.sub(r'[^a-z0-9]+', ' ', s).split()
    kept = [t for t in tokens if t not in LEGAL_FORM_TOKENS and t not in _CONNECTIVES]
    removed['legal_form_tokens'] = [t for t in tokens if t in LEGAL_FORM_TOKENS]
    if not kept:
        kept = tokens
    return {'raw': raw, 'display': clean_display(raw), 'normalised': ' '.join(kept), **removed}


def organization_id(normalised: str) -> str:
    """Stable id from the canonical normalised key ('org:' + key with spaces as hyphens)."""
    return 'org:' + (normalised.replace(' ', '-') or 'unnamed')


def read_aliases(path: Path) -> dict[str, dict[str, str]]:
    """{alias_normalised: row} from the curated alias file. Refuses a file whose columns differ,
    an alias without a note (the note records why the two names are one organisation), an alias
    listed twice, and a chain (an alias pointing at another alias): each would resolve silently."""
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ALIAS_COLUMNS:
            raise ValueError(f'{path.name}: columns must be {ALIAS_COLUMNS}')
        rows = list(reader)
    out: dict[str, dict[str, str]] = {}
    for i, r in enumerate(rows, start=2):
        a, c = (r['alias_normalised'] or '').strip(), (r['canonical_normalised'] or '').strip()
        if not a or not c or not (r['note'] or '').strip():
            raise ValueError(f'{path.name} line {i}: alias, canonical and note are all required')
        if a != normalise(a)['normalised'] or c != normalise(c)['normalised']:
            raise ValueError(f'{path.name} line {i}: alias and canonical must already be normalised keys')
        if a == c:
            raise ValueError(f'{path.name} line {i}: an alias cannot point at itself')
        if a in out:
            raise ValueError(f'{path.name} line {i}: alias {a!r} is listed twice')
        out[a] = r
    chained = sorted(a for a, r in out.items() if r['canonical_normalised'].strip() in out)
    if chained:
        raise ValueError(f'{path.name}: alias chains are not resolved: {chained}')
    return out


def canonical(normalised: str, aliases: dict[str, dict[str, str]]) -> str:
    """The canonical key: the alias file's target when the key is listed there, else the key."""
    row = aliases.get(normalised)
    return row['canonical_normalised'].strip() if row else normalised


def match_tier(a_raw: str, b_raw: str, aliases: dict[str, dict[str, str]]) -> str | None:
    """How two organisation strings agree, strongest first; None when they do not.

      normalised_exact               same key, and the legal forms removed were the same (or none)
      normalised_legal_form_differs  same key only after DIFFERENT legal forms were removed
                                     ("X GmbH" / "X Ltd"): possibly two companies; a reviewer decides
      curated_alias                  different keys joined by the alias file
    Nothing weaker exists: a shared word, prefix or brand is not agreement.
    """
    a, b = normalise(a_raw), normalise(b_raw)
    if not a['normalised'] or not b['normalised']:
        return None
    if a['normalised'] == b['normalised']:
        la, lb = sorted(set(a['legal_form_tokens'])), sorted(set(b['legal_form_tokens']))
        return 'normalised_exact' if (la == lb or not la or not lb) else 'normalised_legal_form_differs'
    if canonical(a['normalised'], aliases) == canonical(b['normalised'], aliases):
        return 'curated_alias'
    return None


def build(raw_names: list[str], aliases: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    """{organization_id: record} for every raw manufacturer string, grouped by canonical key.
    Each record keeps every raw spelling and says which merges came from the rules and which
    from the alias file."""
    orgs: dict[str, dict[str, Any]] = {}
    for raw in raw_names:
        n = normalise(raw)
        key = canonical(n['normalised'], aliases)
        o = orgs.setdefault(organization_id(key), {
            'organization_id': organization_id(key), 'canonical_normalised': key,
            'display_name': (aliases.get(n['normalised']) or {}).get('canonical_display_name') or n['display'],
            'raw_names': [], 'normalised_keys': [], 'aliases_applied': []})
        if raw not in o['raw_names']:
            o['raw_names'].append(raw)
        if n['normalised'] not in o['normalised_keys']:
            o['normalised_keys'].append(n['normalised'])
        if n['normalised'] != key:
            a = aliases[n['normalised']]
            entry = {'alias_normalised': n['normalised'], 'source': a['source'], 'note': a['note'],
                     'valid_from': a['valid_from'] or None, 'valid_to': a['valid_to'] or None}
            if entry not in o['aliases_applied']:
                o['aliases_applied'].append(entry)
    for o in orgs.values():
        o['raw_names'].sort()
        o['normalised_keys'].sort()
        o['merge_basis'] = ('curated_alias' if o['aliases_applied'] else
                            'normalisation_rules' if len(o['raw_names']) > 1 else 'single_spelling')
    return dict(sorted(orgs.items()))
