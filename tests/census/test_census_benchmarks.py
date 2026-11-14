"""Offline checks of the census benchmark rules on small hand-computed inputs. No network."""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import numpy as np
import pytest

from census import acs, benchmark, registry, trials

EDGES14 = [(0, 5), (5, 10), (10, 15), (15, 18), (18, 20), (20, 25), (25, 30), (30, 35), (35, 45), (45, 55),
           (55, 65), (65, 75), (75, 85), (85, None)]


# --- trial side ------------------------------------------------------------------------------

def test_ages_are_read_in_years_and_the_maximum_year_is_inclusive() -> None:
    assert trials.parse_age('6 Months') == 0.5 and trials.parse_age('N/A') is None
    assert trials.eligible_ages({'min_age': '18 Years', 'max_age': '65 Years'}) == (18.0, 66.0)
    assert trials.eligible_ages({'min_age': 'N/A', 'max_age': 'N/A'}) == (0.0, None)
    with pytest.raises(ValueError):
        trials.parse_age('eighteen')
    with pytest.raises(ValueError):
        trials.eligible_ages({'min_age': '40 Years', 'max_age': '18 Years'})


def test_a_trial_is_matched_to_one_non_overlapping_window_with_its_reason() -> None:
    assert all(a[1] < b[0] for a, b in zip(trials.WINDOWS, trials.WINDOWS[1:]))
    p = trials.enrolment_period({'start_date': '2011-01', 'primary_completion_date': '2013-01'})
    assert p['basis'] == 'midpoint_of_start_to_primary_completion'
    assert trials.assign_window(p['midpoint'])['window'] == '2010-2014'
    early = trials.assign_window(2001.5)
    assert early == {'vintage': 2009, 'window': '2005-2009', 'reason': 'before_first_window', 'gap_years': 3.5}
    assert trials.assign_window(2026.0)['reason'] == 'after_last_window'
    assert trials.assign_window(None)['vintage'] is None
    only_start = trials.enrolment_period({'start_date': '2016-05-02'})
    assert only_start['basis'] == 'start_date_only'
    assert trials.enrolment_period({})['basis'] == 'no_usable_date'


def test_the_window_is_assigned_from_the_unrounded_midpoint() -> None:
    # both dates in the last two days of 2009: the midpoint is 2009.9975, which rounds to 2010.0
    rec = {'start_date': '2009-12-30', 'primary_completion_date': '2009-12-31'}
    basis, exact = trials.enrolment_midpoint(rec)
    assert basis == 'midpoint_of_start_to_primary_completion' and 2009.99 < exact < 2010
    assert trials.enrolment_period(rec)['midpoint'] == 2010.0          # the recorded, rounded value
    assert trials.assign_window(exact)['window'] == '2005-2009'        # the window follows the dates
    assert trials.enrolment_midpoint({}) == ('no_usable_date', None)


@pytest.mark.parametrize('text,tier', [
    ('A single-center randomized study', 'self_described_single_site'),
    ('Monocentric pilot', 'self_described_single_site'),
    ('This multicenter trial enrols at 12 hospitals', 'text_indicates_multiple_sites'),
    ('Randomized trial of aspirin', 'one_listed_facility'),
    ('Single site pilot before a multi-center trial', 'conflicting_text'),
    ('Vaccine injected at 2 sites on the arm, one site per dose', 'one_listed_facility'),
    ('Conducted at three clinical sites', 'text_indicates_multiple_sites'),
])
def test_site_tier_reads_only_the_study_text(text: str, tier: str) -> None:
    assert trials.site_tier(text)['tier'] == tier


def test_several_facilities_are_never_single_site_even_with_one_usable_address() -> None:
    us = {'country': 'United States'}
    kept, funnel = trials.cohort([
        {'nct_id': 'A', 'study_sites': [{**us, 'zip': '02114'}]},
        {'nct_id': 'B', 'study_sites': [{**us, 'zip': '02114'}, {**us, 'zip': ''}]},
        {'nct_id': 'C', 'study_sites': [{**us}, {'country': 'Canada'}]},
        {'nct_id': 'D', 'study_sites': []}])
    assert [r['nct_id'] for r in kept] == ['A']
    assert funnel['us_several_facilities_one_with_zip'] == 1 and funnel['any_non_us_facility'] == 1
    assert funnel['no_facility_listed'] == 1


def test_observed_keeps_unknown_as_a_row_and_absence_as_a_reason() -> None:
    rec = {'race': {'reported': True, 'omb_totals': {'white': 6, 'asian': 2, 'unknown_not_reported': 2},
                    'raw_categories': [{'original': 'White', 'count': 6}]},
           'ethnicity': {'reported': False, 'omb_totals': {'hispanic_latino': 0}},
           'sex': {'reported': True, 'totals': {'female': 3, 'male': 6, 'intersex': 1}}}
    race = trials.observed(rec, 'race')
    assert race['n_in_table'] == 10 and race['counts']['unknown_not_reported'] == 2
    assert race['n_classified'] == 8 and race['structure']['check'] == 'matches_sex_table'
    assert race['original_categories'] == [{'original': 'White', 'count': 6}]
    assert trials.observed(rec, 'ethnicity') == {'reported': False, 'reason': 'ethnicity_not_reported'}
    sex = trials.observed(rec, 'sex')
    assert sex['n_in_table'] == 10 and sex['unharmonised_counts'] == {'intersex': 1}
    assert sex['n_classified'] == 9          # an unmapped category is not "classified" here


# --- survey tables ---------------------------------------------------------------------------

def test_age_bands_are_read_from_labels_in_either_label_style() -> None:
    assert acs.age_span('Estimate!!Total:!!Male:!!18 and 19 years') == (18.0, 20.0)
    assert acs.age_span('Estimate!!Total!!Male!!85 years and over') == (85.0, None)
    assert acs.age_span('Estimate!!Total!!Male!!20 years') == (20.0, 21.0)
    assert acs.age_span('Estimate!!Total!!Male') is None


def _variables(spans: list[tuple[str, str]], colon: bool) -> dict[str, dict[str, str]]:
    c = ':' if colon else ''
    out = {'T_001E': {'label': f'Estimate!!Total{c}'}}
    n = 2
    for sex in ('Male', 'Female'):
        out[f'T_{n:03d}E'] = {'label': f'Estimate!!Total{c}!!{sex}{c}'}
        n += 1
        for _, text in spans:
            out[f'T_{n:03d}E'] = {'label': f'Estimate!!Total{c}!!{sex}{c}!!{text}'}
            out[f'T_{n:03d}M'] = {'label': f'Margin of Error!!Total{c}!!{sex}{c}!!{text}'}
            n += 1
    return out


COARSE = [('a', 'Under 18 years'), ('b', '18 to 64 years'), ('c', '65 years and over')]
FINE = [('a', 'Under 18 years'), ('b1', '18 to 39 years'), ('b2', '40 to 64 years'), ('c', '65 years and over')]


def test_bands_must_tile_the_whole_age_range() -> None:
    assert len(acs.bands(_variables(COARSE, True))) == 6
    with pytest.raises(ValueError):
        acs.bands(_variables([('a', 'Under 18 years'), ('c', '65 years and over')], False))


def _write_level(cache: Path, rows_by_cat: dict[str, list[float]], total_fine: list[float], moe: float) -> None:
    """One area ('01'), level 'state'. Each list is male cells then female cells."""
    for cat, table in acs.TABLES.items():
        spans = FINE if cat == 'total' else COARSE
        variables = _variables(spans, colon=(cat != 'white'))      # mixed label styles on purpose
        cells = [k[:-1] for k in variables if k.endswith('M')]
        values = total_fine if cat == 'total' else rows_by_cat.get(cat, [0.0] * 6)
        header = ['NAME'] + [c + 'E' for c in cells] + [c + 'M' for c in cells] + ['state']
        row = ['Testland'] + [str(v) for v in values] + [str(moe)] * len(cells) + ['01']
        if cat == 'total':
            row[1 + len(cells)] = '-555555555'                     # a controlled cell: margin zero
        with gzip.open(cache / f'acs5_2024_{table}_state.json.gz', 'wt') as fh:
            json.dump({'variables': variables, 'rows': [header, row]}, fh)


@pytest.fixture()
def level(tmp_path: Path) -> acs.Level:
    # males: 100 under 18, 300 aged 18-64 (as 120 + 180), 100 aged 65+; females the same
    _write_level(tmp_path, {'white': [60, 150, 80, 60, 150, 80], 'black_african_american': [20, 90, 10, 20, 90, 10],
                            'hispanic_latino': [30, 60, 5, 30, 60, 5]},
                 total_fine=[100, 120, 180, 100, 100, 120, 180, 100], moe=3.0)
    return acs.load(2024, 'state', tmp_path)


def test_the_all_residents_table_is_summed_onto_the_race_tables_bands(level: acs.Level) -> None:
    t = acs.TABLE_ORDER.index('total')
    assert level.edges == [(0.0, 18.0), (18.0, 65.0), (65.0, None)]
    assert level.est[0, t].tolist() == [[100, 300, 100], [100, 300, 100]]
    # margins add as squares: the male 18-64 band is two cells of 3; the first cell is controlled
    assert level.moe[0, t, 0, 1] == pytest.approx(math.sqrt(18)) and level.moe[0, t, 0, 0] == 0.0


def test_expected_share_restricts_to_eligible_ages_and_sex_and_carries_the_margin(level: acs.Level) -> None:
    adults = benchmark.expected(level, '01', 'race', ('male', 'female'), 18.0, 65.0)
    assert adults['pct']['white'] == pytest.approx(50.0) and adults['pct']['black_african_american'] == pytest.approx(30.0)
    assert adults['eligible_residents'] == 600
    # proportion margin: sqrt(moe_num^2 - p^2 * moe_den^2) / den, with num 300 +/- sqrt(18), den 600 +/- 6
    assert adults['moe90_pp']['white'] == pytest.approx(100 * math.sqrt(18 - 0.25 * 36) / 600, abs=1e-3)
    women = benchmark.expected(level, '01', 'race', ('female',), 0.0, None)
    assert women['eligible_residents'] == 500 and women['pct']['white'] == pytest.approx(58.0)
    eth = benchmark.expected(level, '01', 'ethnicity', ('male', 'female'), 18.0, 65.0)
    assert eth['pct'] == {'hispanic_latino': 20.0, 'not_hispanic_latino': 80.0}
    # the complement carries the same margin as the share it is the complement of
    assert eth['moe90_pp']['not_hispanic_latino'] == eth['moe90_pp']['hispanic_latino'] > 0
    sex = benchmark.expected(level, '01', 'sex', ('male', 'female'), 0.0, None)
    assert sex['pct'] == {'female': 50.0, 'male': 50.0}
    # a single-sex trial has no sex composition to compare: nothing is computed
    assert benchmark.expected(level, '01', 'sex', ('female',), 0.0, None) == {
        'available': False, 'reason': 'single_sex_by_eligibility'}


def test_a_partly_covered_band_counts_in_proportion() -> None:
    w = benchmark.band_weights(EDGES14, 18.0, 66.0)
    assert w[4:11].tolist() == [1.0] * 7 and w[11] == pytest.approx(0.1) and w[:4].sum() == 0
    assert benchmark.band_weights(EDGES14, 0.0, None).sum() == 14
    assert benchmark.band_weights(EDGES14, 90.0, None)[-1] == pytest.approx(10 / 15)


def test_an_area_missing_from_the_window_is_unavailable_not_zero(level: acs.Level) -> None:
    assert benchmark.expected(level, '99', 'race', ('male',), 0.0, None) == {
        'available': False, 'reason': 'area_not_in_this_window_file'}
    assert benchmark.expected(level, '01', 'race', ('male',), 200.0, None)['reason'] == 'no_eligible_residents_estimated'


def test_ratio_is_undefined_not_infinite_when_the_reference_share_is_zero() -> None:
    out = benchmark.compare({'asian': 12.0, 'white': 40.0}, {'pct': {'asian': 0.0, 'white': 50.0}})
    assert out['diff_pp'] == {'asian': 12.0, 'white': -10.0}
    assert out['ratio'] == {'asian': None, 'white': 0.8} and out['ratio_undefined'] == {'asian': 'reference_share_is_zero'}


# --- pooling ---------------------------------------------------------------------------------

def _row(n: int, obs: float, exp: float) -> dict[str, object]:
    return {'n': n, 'obs': {'x': obs}, 'exp': {'x': exp}, 'moe': {'x': 1.0}}


def test_each_trial_counts_once_and_the_weighted_figure_is_secondary() -> None:
    rows = [_row(10000, 80.0, 50.0), _row(20, 20.0, 50.0)]
    out = benchmark.pool(rows, ['x'], floor=2, resamples=200, rng=np.random.default_rng(1))
    cat = out['categories'][0]
    assert cat['observed_pct'] == 50.0 and cat['diff_pp'] == 0.0            # the two trials weigh the same
    assert cat['weighted']['observed_pct'] == pytest.approx(79.88, abs=0.01)
    assert out['concentration_flag'] is True and out['n_participants'] == 10020
    assert cat['expected_moe90_pp'] == 1.0 and cat['interval_level'] == 0.95


def test_an_area_below_the_floor_is_withheld_with_its_count() -> None:
    out = benchmark.pool([_row(30, 10.0, 20.0)] * 3, ['x'], floor=50, resamples=10, rng=np.random.default_rng(1))
    assert out == {'n_trials': 3, 'n_participants': 90, 'display_rule': 'withheld',
                   'gate_reason': 'fewer than 50 qualifying trials'}


def test_the_bootstrap_repeats_exactly_with_the_same_seed() -> None:
    rows = [_row(10 + i, float(i % 7) * 10, 30.0) for i in range(60)]
    a = benchmark.pool(rows, ['x'], floor=50, resamples=500, rng=np.random.default_rng(7))
    b = benchmark.pool(rows, ['x'], floor=50, resamples=500, rng=np.random.default_rng(7))
    assert a == b and a['categories'][0]['lo_pp'] < a['categories'][0]['diff_pp'] < a['categories'][0]['hi_pp']


# --- provenance ------------------------------------------------------------------------------

def test_the_key_never_survives_into_a_recorded_url() -> None:
    assert acs.redact('https://api.census.gov/data/2024/acs/acs5?get=x&key=abc123&for=us:1') == \
        'https://api.census.gov/data/2024/acs/acs5?get=x&key=REDACTED&for=us:1'


def test_registry_facts_take_the_point_only_from_a_single_location() -> None:
    one = {'protocolSection': {'identificationModule': {'officialTitle': 'A single-center study'},
                               'contactsLocationsModule': {'locations': [{'geoPoint': {'lat': 1.0, 'lon': 2.0}}]}}}
    two = {'protocolSection': {'contactsLocationsModule': {'locations': [{'geoPoint': {'lat': 1.0, 'lon': 2.0}}, {}]}}}
    assert registry.site_facts(one)['point'] == (1.0, 2.0) and 'single-center' in registry.site_facts(one)['text']
    assert registry.site_facts(one)['place_now'] == {'city': None, 'state': None, 'zip': None, 'country': None}
    assert registry.site_facts(two)['place_now'] is None
    assert registry.site_facts(two)['point'] is None and registry.site_facts(two)['n_locations_now'] == 2
    assert registry.site_facts(None)['found'] is False


def test_the_known_only_figure_divides_by_participants_with_a_known_category() -> None:
    # two trials, each 40% in category x with half the table unknown: 40% of all, 80% of known
    rows = [{**_row(100, 40.0, 60.0), 'obs_known': {'x': 80.0}, 'n_known': 50},
            {**_row(50, 40.0, 60.0), 'obs_known': {'x': 80.0}, 'n_known': 25},
            {**_row(10, 0.0, 60.0), 'obs_known': None, 'n_known': 0}]       # nobody classified: left out of B
    out = benchmark.pool(rows, ['x'], floor=2, resamples=50, rng=np.random.default_rng(3))
    cat = out['categories'][0]
    assert cat['observed_pct'] == pytest.approx(26.67, abs=0.01) and cat['ratio_denominator'] == 'all_participants_in_tables'
    assert cat['of_classified'] == {'n_trials': 2, 'observed_pct': 80.0, 'diff_pp': 20.0, 'ratio': 1.333}
    assert out['denominators'] == {'participants_in_tables': 160, 'participants_classified': 75,
                                   'unknown_or_unmapped_pct_mean': 0.0}


def test_unknowns_bound_the_difference_and_say_when_its_direction_is_open() -> None:
    def row(obs: float, unknown: float) -> dict[str, object]:
        return {**_row(100, obs, 30.0), 'unknown': unknown}
    # 20% observed against 30% expected with 25% unknown: between 10 points under and 15 over
    open_case = benchmark.pool([row(20.0, 25.0)] * 2, ['x'], floor=2, resamples=20, rng=np.random.default_rng(1))
    assert open_case['categories'][0]['unknown_bounds'] == {
        'diff_pp_if_no_unknown_is_in_category': -10.0, 'diff_pp_if_every_unknown_is_in_category': 15.0,
        'direction_settled': False}
    # 5% unknown cannot close a 10-point gap; a share already above the reference stays above
    under = benchmark.pool([row(20.0, 5.0)] * 2, ['x'], floor=2, resamples=20, rng=np.random.default_rng(1))
    over = benchmark.pool([row(45.0, 25.0)] * 2, ['x'], floor=2, resamples=20, rng=np.random.default_rng(1))
    assert under['categories'][0]['unknown_bounds']['direction_settled'] is True
    assert over['categories'][0]['unknown_bounds']['direction_settled'] is True


def test_a_table_whose_rows_overlap_is_not_used_for_shares() -> None:
    sex = {'reported': True, 'totals': {'female': 20, 'male': 20}}
    doubled = {'race': {'reported': True, 'omb_totals': {'white': 40, 'asian': 30}}, 'sex': sex}
    assert trials.observed(doubled, 'race')['structure'] == {
        'parser_flags': [], 'sex_table_total': 40, 'usable_for_shares': False, 'check': 'table_total_exceeds_sex_table'}
    # a table that carries an ethnicity row is a combined classification: not compared
    mixed = {'race': {'reported': True, 'omb_totals': {'white': 25, 'black_african_american': 10, 'unknown_not_reported': 5},
                      'raw_categories': [{'original': 'White/Non-Hispanic', 'count': 25}, {'original': 'Black', 'count': 10}],
                      'quarantined_labels': [{'original': 'Latin', 'count': 5}]}, 'sex': sex}
    s = trials.observed(mixed, 'race')['structure']
    assert s['usable_for_shares'] is False and s['check'] == 'race_table_has_ethnicity_rows'
    assert s['labels'] == ['White/Non-Hispanic', 'Latin']
    # a title that mentions ethnicity over race-only rows is compared, with the caution kept
    titled = {'race': {'reported': True, 'omb_totals': {'white': 25, 'asian': 15},
                       'raw_categories': [{'original': 'White', 'count': 25, 'flags': ['combined_race_ethnicity']},
                                          {'original': 'Asian', 'count': 15, 'flags': ['combined_race_ethnicity']}]}, 'sex': sex}
    s = trials.observed(titled, 'race')['structure']
    assert s['usable_for_shares'] is True and s['cautions'] == ['title_names_ethnicity_rows_are_race_only']
    # the same words in an ethnicity table are what that table is for
    eth = {'ethnicity': {'reported': True, 'omb_totals': {'hispanic_latino': 10, 'not_hispanic_latino': 30},
                         'raw_categories': [{'original': 'Hispanic or Latino', 'count': 10}]}, 'sex': sex}
    assert trials.observed(eth, 'ethnicity')['structure']['usable_for_shares'] is True
    subset = {'race': {'reported': True, 'omb_totals': {'white': 20, 'asian': 5}}, 'sex': sex}
    assert trials.observed(subset, 'race')['structure']['check'] == 'table_total_below_sex_table'
    alone = {'race': {'reported': True, 'omb_totals': {'white': 20}}}
    assert trials.observed(alone, 'race')['structure']['check'] == 'no_other_table_to_check_against'


def test_one_us_site_among_international_sites_is_counted_and_excluded() -> None:
    kept, funnel = trials.cohort([{'nct_id': 'A', 'study_sites': [{'country': 'United States'}, {'country': 'France'}]},
                                  {'nct_id': 'B', 'study_sites': [{'country': 'France'}, {'country': 'Spain'}]}])
    assert kept == [] and funnel['one_us_facility_plus_non_us_facilities'] == 1 and funnel['any_non_us_facility'] == 2


def test_a_general_population_benchmark_is_labelled_contextual_when_it_does_not_fit() -> None:
    patient = {'healthy_volunteers': False}
    assert trials.reference_caveats(patient, 18.0, None, ('male', 'female'), False) == ['enrols_patients_not_the_general_population']
    child = trials.reference_caveats({'healthy_volunteers': True}, 2.0, 18.0, ('female',), True)
    assert child == ['site_in_a_referral_centre_zip', 'children_only', 'single_sex_eligibility']
    assert trials.reference_caveats({'healthy_volunteers': True}, 18.0, None, ('male', 'female'), False) == []


def test_a_cached_table_is_reused_only_when_its_bytes_are_the_recorded_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Reply:
        status_code = 200

        def __init__(self, url: str, body: object) -> None:
            self.url, self._body, self.content = url, body, json.dumps(body).encode()

        def json(self) -> object:
            return self._body

    class Session:
        def get(self, url: str, params: dict[str, str], timeout: int) -> Reply:
            calls.append(url)
            if url.endswith('.json'):
                return Reply(url, {'variables': {'B01001_001E': {'label': 'Estimate!!Total:'}}})
            return Reply(url + '?key=secret&get=x', [['NAME', 'us'], ['United States', '1']])

    monkeypatch.setenv('CENSUS_API_KEY', 'secret')
    first = acs.fetch(2024, 'B01001', 'us', tmp_path, Session())
    assert 'secret' not in json.dumps(first) and len(calls) == 2
    assert acs.fetch(2024, 'B01001', 'us', tmp_path, Session()) == first and len(calls) == 2     # reused
    payload = tmp_path / 'acs5_2024_B01001_us.json.gz'
    with gzip.open(payload, 'wt') as fh:                                                         # altered on disk
        json.dump({'variables': {}, 'rows': [['NAME', 'us'], ['United States', '999']]}, fh)
    acs.fetch(2024, 'B01001', 'us', tmp_path, Session())
    assert len(calls) == 4, 'altered bytes must be fetched again, not trusted'
    meta = json.loads((tmp_path / 'acs5_2024_B01001_us.meta.json').read_text())
    del meta['sha256_cached_payload']                                                            # a cache from before the hash
    (tmp_path / 'acs5_2024_B01001_us.meta.json').write_text(json.dumps(meta))
    acs.fetch(2024, 'B01001', 'us', tmp_path, Session())
    assert len(calls) == 6
