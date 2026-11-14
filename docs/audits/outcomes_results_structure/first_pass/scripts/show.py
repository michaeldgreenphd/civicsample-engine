"""Compact printer for analysis.json sections: python3 -I show.py OUTDIR section [section...]"""
import json, sys
A = json.load(open(sys.argv[1] + '/analysis.json'))
def is_frac(o): return isinstance(o, dict) and set(o) == {'k', 'n', 'pct', 'ci95'}
def show(o, ind=0, key=''):
    pad = ' ' * ind
    if is_frac(o): print(f"{pad}{key}: {o['k']}/{o['n']} = {o['pct']}% {o['ci95']}")
    elif isinstance(o, dict) and 'rows' in o:
        print(f"{pad}{key}: n={o.get('n')} distinct={o.get('distinct')}" + (f" summary={o['summary']}" if 'summary' in o else ''))
        for r in o['rows']: print(f"{pad}   {r['value']!s:60.60} {r['k']:>6} {r['pct']}%")
    elif isinstance(o, dict):
        if all(not isinstance(v, (dict, list)) for v in o.values()): print(f"{pad}{key}: {o}")
        else:
            print(f"{pad}{key}:")
            for k, v in o.items(): show(v, ind + 2, k)
    elif isinstance(o, list) and o and isinstance(o[0], dict):
        print(f"{pad}{key}:")
        for r in o: print(f"{pad}   {json.dumps(r, ensure_ascii=False)[:330]}")
    else: print(f"{pad}{key}: {o}")
for s in sys.argv[2:]: show(A[s], 0, s)
