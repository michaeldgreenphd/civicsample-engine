"""Enumerate the keys actually present in the cached results sections (schema discovery)."""
import gzip, json, sys, collections
cache = sys.argv[1]
keys = collections.defaultdict(collections.Counter)
def walk(path, o):
    if isinstance(o, dict):
        for k, v in o.items():
            keys[path][k] += 1
            walk(path + '.' + k, v)
    elif isinstance(o, list):
        for x in o:
            walk(path + '[]', x)
n = 0
with gzip.open(cache, 'rt', encoding='utf-8') as f:
    for line in f:
        for s in json.loads(json.loads(line)['body'])['studies']:
            n += 1
            walk('study', {k: v for k, v in s.items()})
print(n)
for p in sorted(keys):
    if 'protocolSection.designModule' in p or 'statusModule.' in p: continue
    print(p, dict(keys[p].most_common(30)))
