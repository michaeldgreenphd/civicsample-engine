"""Loaders shared by the analysis scripts. Reads only caches; makes no network call."""
import gzip, json, os, math


def load_sample(v1_dir):
    """The 2,000 sampled studies from the first audit's raw-response cache, keyed by NCT ID.

    Each cache line is one HTTP response (unparsed body); studies are taken verbatim from `studies[]`.
    Returns (studies_by_id, parse_failures) where parse_failures lists cache lines that did not parse."""
    studies, failures = {}, []
    with gzip.open(os.path.join(v1_dir, 'cache', 'raw_responses.jsonl.gz'), 'rt', encoding='utf-8') as f:
        for i, line in enumerate(f):
            try:
                rec = json.loads(line)
                body = json.loads(rec['body'])
            except Exception as e:  # recorded, never skipped silently
                failures.append({'line': i, 'error': repr(e)})
                continue
            for s in body.get('studies', []):
                studies[s['protocolSection']['identificationModule']['nctId']] = s
    return studies, failures


def load_release(v2_dir):
    """The slim copy of the full release (scripts/build_slim.py), keyed by NCT ID."""
    out = {}
    with gzip.open(os.path.join(v2_dir, 'release_slim.jsonl.gz'), 'rt', encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            out[r['nct_id']] = r
    return out


def wilson(k, n, z=1.959964):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def frac(k, n):
    """'k/n (p%; 95% CI lo-hi)' with a Wilson interval; 'k/0' when the denominator is empty."""
    if not n:
        return f'{k:,}/0'
    lo, hi = wilson(k, n)
    return f'{k:,}/{n:,} ({100*k/n:.1f}%; 95% CI {100*lo:.1f}-{100*hi:.1f})'


def is_num(v):
    """True when a registry value string parses as a finite number ('NA', '', None do not)."""
    if v is None:
        return False
    try:
        x = float(str(v).replace(',', '').strip())
    except ValueError:
        return False
    return math.isfinite(x)


def num(v):
    return float(str(v).replace(',', '').strip()) if is_num(v) else None
