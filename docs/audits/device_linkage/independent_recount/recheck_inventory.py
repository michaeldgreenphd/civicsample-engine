"""Independent recount of the device inventory figures from FDA's list as downloaded.
Written without reading the build's code. Definitions are stated in the output labels."""
import csv, sys, collections, re
rows = list(csv.DictReader(open(sys.argv[1], newline='', encoding='utf-8-sig')))
num = [r['Submission Number'].strip() for r in rows]
by = collections.defaultdict(list)
for r in rows: by[r['Submission Number'].strip()].append(r)
dup = {k: v for k, v in by.items() if len(v) > 1}
list_cols = ['Date of Final Decision', 'Submission Number', 'Device', 'Company', 'Panel (Lead)', 'Primary Product Code']
ident = sum(1 for v in dup.values() if len({tuple(r[c] for c in list_cols) for r in v}) == 1)
def pathway(n):
    if re.fullmatch(r'K\d{6}', n): return '510k'
    if re.fullmatch(r'DEN\d{6}', n): return 'de_novo'
    if re.fullmatch(r'P\d{6}', n): return 'pma'
    if re.fullmatch(r'P\d{6}/S\d+', n): return 'pma_supplement'
    return 'other:' + n
print('source rows', len(rows))
print('distinct submission numbers', len(by))
print('numbers on more than one row', len(dup), '| extra rows', len(rows) - len(by))
print('  of which all list columns identical', ident, '| differing in some list column', len(dup) - ident)
diffcols = collections.Counter()
for v in dup.values():
    for c in list_cols:
        if len({r[c] for r in v}) > 1: diffcols[c] += 1
print('  columns that differ among duplicated numbers', dict(diffcols))
print('pathway by number format (distinct submissions)', dict(collections.Counter(pathway(n) for n in by)))
print('pathway by number format (rows)', dict(collections.Counter(pathway(n) for n in num)))
print('distinct raw company strings', len({r['Company'] for r in rows}))
