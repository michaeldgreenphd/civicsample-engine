"""Independent check of the retrieved FDA documents: every stored file's bytes hash to its name,
and which files contain a ClinicalTrials.gov identifier when read with a different text extractor
(pdftotext) from the one the build uses (pypdf). Written without reading the build's code."""
import hashlib, json, os, re, subprocess, sys
store, out = sys.argv[1], sys.argv[2]
NCT = re.compile(r'NCT[\s\-]?0?\d{8}', re.I)
res = {'n_files': 0, 'hash_mismatch': [], 'extract_failed': [], 'no_text': [], 'with_nct': {}}
for name in sorted(os.listdir(store)):
    if not name.endswith('.pdf'): continue
    p = os.path.join(store, name); res['n_files'] += 1
    h = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    if h != name[:-4]: res['hash_mismatch'].append(name)
    try:
        t = subprocess.run(['pdftotext', '-q', p, '-'], capture_output=True, timeout=120).stdout.decode('utf-8', 'replace')
    except Exception as e:
        res['extract_failed'].append([name, repr(e)]); continue
    if not t.strip(): res['no_text'].append(name); continue
    ids = sorted({re.sub(r'[\s\-]', '', m.upper()) for m in NCT.findall(t)})
    if ids: res['with_nct'][name[:-4]] = ids
json.dump(res, open(out, 'w'), indent=1)
print('files', res['n_files'], 'hash mismatches', len(res['hash_mismatch']), 'extract failed', len(res['extract_failed']),
      'no text', len(res['no_text']), 'files with an NCT id', len(res['with_nct']))
