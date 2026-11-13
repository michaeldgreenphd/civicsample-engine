"""The comparison itself: age weights, expected shares with margins of error, metrics, pooling.

Every figure is a comparison with a selected reference population (see census/__init__.py).
"""
from __future__ import annotations

from typing import Any

import numpy as np

from . import acs, trials

# The open top band ("85 years and over") is given a nominal 15-year width so that a trial with
# an upper limit inside it takes a proportional part. It only matters for limits above 85.
TOP_BAND_YEARS = 15.0
# The benchmark category for each trial category. 'other' against Some Other Race is the
# loosest pairing: the registry's "Other" is whatever a sponsor wrote, while the survey's is a
# write-in category that many Hispanic respondents choose. 'unknown_not_reported' has no
# counterpart and stays a trial-only row, never spread over the others.
RACE_TABLE = {k: k for k in trials.RACE_KEYS if k != 'unknown_not_reported'}


def band_weights(edges: list[tuple[float, float | None]], lo: float, hi: float | None) -> np.ndarray:
    """Share of each age band inside [lo, hi). Assumes ages are spread evenly within a band,
    the only assumption the published bands allow."""
    out = np.zeros(len(edges))
    for k, (a, b) in enumerate(edges):
        b = a + TOP_BAND_YEARS if b is None else b
        top = b if hi is None else min(b, hi)
        out[k] = max(0.0, top - max(a, lo)) / (b - a)
    return out


def _sum(level: acs.Level, area: int, table: str, sexes: tuple[str, ...], w: np.ndarray) -> tuple[float, float]:
    """Weighted residents and the margin of error of that sum.

    Margins combine as the root of summed squares, the Census Bureau's approximation for a sum
    of estimates; it ignores covariance between cells, so it is approximate (usually too wide)."""
    t = acs.TABLE_ORDER.index(table)
    s = [acs.SEXES.index(x) for x in sexes]
    est = float((level.est[area, t][s] * w).sum())
    moe = float(np.sqrt(((level.moe[area, t][s] * w) ** 2).sum()))
    return est, moe


def _share(num: float, num_moe: float, den: float, den_moe: float) -> tuple[float, float | None]:
    """A proportion and its 90% margin of error by the Census Bureau's formula for a proportion,
    falling back to the ratio formula when the term under the root is negative."""
    p = num / den
    if np.isnan(num_moe) or np.isnan(den_moe):
        return p, None
    under = num_moe ** 2 - (p ** 2) * den_moe ** 2
    if under < 0:
        under = num_moe ** 2 + (p ** 2) * den_moe ** 2
    return p, float(np.sqrt(under) / den)


def expected(level: acs.Level, geoid: str, dimension: str, sexes: tuple[str, ...],
             lo: float, hi: float | None) -> dict[str, Any]:
    """Expected composition among residents of one area who meet the age and sex eligibility.

    Returns {'available': False, 'reason': ...} rather than a number when the area is not in
    this window's file or has no eligible residents."""
    area = level.index.get(geoid)
    if area is None:
        return {'available': False, 'reason': 'area_not_in_this_window_file'}
    w = band_weights(level.edges, lo, hi)
    den, den_moe = _sum(level, area, 'total', sexes, w)
    if not den > 0:
        return {'available': False, 'reason': 'no_eligible_residents_estimated' if den == 0 else 'estimate_not_published'}
    out: dict[str, Any] = {'available': True, 'eligible_residents': round(den), 'pct': {}, 'moe90_pp': {}}

    def put(key: str, num: float, num_moe: float) -> None:
        p, m = _share(num, num_moe, den, den_moe)
        out['pct'][key] = round(100 * p, 3)
        out['moe90_pp'][key] = None if m is None else round(100 * m, 3)

    if dimension == 'race':
        for key, table in RACE_TABLE.items():
            put(key, *_sum(level, area, table, sexes, w))
    elif dimension == 'ethnicity':
        h, h_moe = _sum(level, area, 'hispanic_latino', sexes, w)
        put('hispanic_latino', h, h_moe)
        # Not Hispanic is everyone else; its margin combines the two it is derived from.
        put('not_hispanic_latino', den - h, float(np.sqrt(den_moe ** 2 + h_moe ** 2)))
    elif dimension == 'sex':
        # Only meaningful for a trial open to both sexes; the caller enforces that.
        for key in ('female', 'male'):
            put(key, *_sum(level, area, 'total', (key,), w))
    return out


def compare(observed_pct: dict[str, float], exp: dict[str, Any]) -> dict[str, Any]:
    """Percentage-point difference and representation ratio for every category on both sides.

    The ratio is undefined when the reference share is zero; that is recorded, not infinity."""
    diff, ratio, why = {}, {}, {}
    for key, e in exp['pct'].items():
        o = observed_pct[key]
        diff[key] = round(o - e, 3)
        if e > 0:
            ratio[key] = round(o / e, 4)
        else:
            ratio[key], why[key] = None, 'reference_share_is_zero'
    return {'diff_pp': diff, 'ratio': ratio, **({'ratio_undefined': why} if why else {})}


def pool(rows: list[dict[str, Any]], keys: list[str], *, floor: int, resamples: int,
         rng: np.random.Generator) -> dict[str, Any]:
    """One area's figures from its trials. Each row: {'n': table total, 'obs': {key: pct},
    'exp': {key: pct}, 'moe': {key: pp or None}}.

    Primary: each trial counts once (plain means), so one large study cannot set the result.
    Secondary ('weighted'): participants summed, reported beside it and never as the headline.
    The interval is a bootstrap over trials: it reflects variation between this area's trials,
    not the survey's sampling error, which is carried separately as the mean margin of error
    (trials in one area share the same survey estimates, so their margins do not average down).
    Below the floor nothing is computed: the area is withheld with its count."""
    n_trials = len(rows)
    n = np.array([r['n'] for r in rows], dtype=float)
    out: dict[str, Any] = {'n_trials': n_trials, 'n_participants': int(n.sum())}
    if n_trials < floor:
        return {**out, 'display_rule': 'withheld', 'gate_reason': f'fewer than {floor} qualifying trials'}
    obs = np.array([[r['obs'][k] for k in keys] for r in rows])
    exp = np.array([[r['exp'][k] for k in keys] for r in rows])
    moe = np.array([[np.nan if r['moe'].get(k) is None else r['moe'][k] for k in keys] for r in rows])
    draws = rng.integers(0, n_trials, size=(resamples, n_trials))
    boot = (obs - exp)[draws].mean(axis=1)                       # resamples x keys
    wn = n[draws]
    boot_w = ((obs - exp)[draws] * wn[:, :, None]).sum(axis=1) / wn.sum(axis=1)[:, None]
    lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
    lo_w, hi_w = np.percentile(boot_w, [2.5, 97.5], axis=0)
    cats = []
    for j, k in enumerate(keys):
        o, e = float(obs[:, j].mean()), float(exp[:, j].mean())
        ow, ew = float((obs[:, j] * n).sum() / n.sum()), float((exp[:, j] * n).sum() / n.sum())
        m = moe[:, j]
        cats.append({
            'category': k, 'observed_pct': round(o, 2), 'expected_pct': round(e, 2), 'diff_pp': round(o - e, 2),
            'ratio': round(o / e, 3) if e > 0 else None,
            **({} if e > 0 else {'ratio_undefined': 'reference_share_is_zero'}),
            'lo_pp': round(float(lo[j]), 2), 'hi_pp': round(float(hi[j]), 2), 'interval_level': 0.95,
            'expected_moe90_pp': None if np.isnan(m).all() else round(float(np.nanmean(m)), 2),
            'weighted': {'observed_pct': round(ow, 2), 'expected_pct': round(ew, 2), 'diff_pp': round(ow - ew, 2),
                         'lo_pp': round(float(lo_w[j]), 2), 'hi_pp': round(float(hi_w[j]), 2)}})
    return {**out, 'display_rule': 'show', 'gate_reason': None,
            # qualifies the weighted figure: one trial holding most participants
            'concentration_flag': bool(n.max() / n.sum() > 0.5), 'categories': cats}
