"""The site resolver on synthetic boundary files: two counties, one metro area, three ZCTAs."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('shapely')
shapefile = pytest.importorskip('shapefile')

from census import geography  # noqa: E402


def _write(path: Path, field: str, shapes: dict[str, list[tuple[float, float]]]) -> None:
    with shapefile.Writer(str(path)) as w:
        w.field(field, 'C', 12)
        for code, ring in shapes.items():
            w.poly([ring + ring[:1]])
            w.record(code)


def _box(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]


@pytest.fixture()
def resolver(tmp_path: Path) -> geography.Resolver:
    files = geography.BOUNDARIES[2024]
    # county 25001 covers x 0..10, county 25002 covers x 10..20 (both Massachusetts, FIPS 25)
    _write(tmp_path / files['county'].replace('/', '__'), 'GEOID', {'25001': _box(0, 0, 10, 10), '25002': _box(10, 0, 20, 10)})
    _write(tmp_path / files['cbsa'].replace('/', '__'), 'GEOID', {'14460': _box(0, 0, 10, 10)})
    _write(tmp_path / files['zcta'].replace('/', '__'), 'ZCTA5CE20', {
        '02114': _box(1, 1, 3, 3),            # wholly inside county 25001 and the metro area
        '02199': _box(8, 4, 12, 6),           # straddles the county line evenly
        '01999': _box(15, 1, 17, 3)})         # county 25002, outside any metro area
    return geography.Resolver(2024, tmp_path, {'02114', '02199', '01999', '02201'}, zcta_published={'02114', '02199', '01999'})


def test_a_zip_is_a_zcta_only_when_that_code_is_published(resolver: geography.Resolver) -> None:
    hit = resolver.resolve('02114', (2.0, 2.0), 'Massachusetts')
    assert hit['zcta'] == {'geoid': '02114', 'method': 'zip_code_equals_published_zcta_code'}
    assert hit['county'] == {'geoid': '25001', 'method': 'zcta_polygon_largest_area_share', 'area_share': 1.0,
                             'agrees_with_city_point': True}
    assert hit['cbsa']['geoid'] == '14460' and hit['state']['geoid'] == '25' and hit['region']['geoid'] == '1'
    po_box = resolver.resolve('02201', (2.0, 2.0), 'Massachusetts')
    assert po_box['zcta'] == {'geoid': None, 'reason': 'zip_has_no_zcta_in_this_window'}
    # no polygon for that ZIP: the city point decides the county, labelled as the weaker method
    assert po_box['county'] == {'geoid': '25001', 'method': 'city_point_in_polygon', 'confidence': 'low'}


def test_the_area_share_is_kept_as_match_confidence(resolver: geography.Resolver) -> None:
    split = resolver.resolve('02199', (2.0, 2.0), 'Massachusetts')
    assert split['county']['area_share'] == 0.5 and split['county']['geoid'] in ('25001', '25002')
    rural = resolver.resolve('01999', None, 'Massachusetts')
    assert rural['county']['geoid'] == '25002' and rural['county']['agrees_with_city_point'] is None
    assert rural['cbsa'] == {'geoid': None, 'reason': 'outside_any_metro_or_micro_area', 'area_share': 0.0}


def test_a_place_outside_the_registered_state_withdraws_the_finer_units(resolver: geography.Resolver) -> None:
    out = resolver.resolve('02114', None, 'Texas')
    assert out['state'] == {'geoid': '48', 'method': 'registered_state_name'} and out['region']['geoid'] == '3'
    for kind in ('zcta', 'county', 'cbsa'):
        assert out[kind] == {'geoid': None, 'reason': 'resolved_place_is_outside_the_registered_state'}


def test_nothing_is_guessed_without_a_zip_or_a_point(resolver: geography.Resolver) -> None:
    out = resolver.resolve(None, None, 'Massachusetts')
    assert out['zcta']['reason'] == 'no_zip_in_registration'
    assert out['county'] == {'geoid': None, 'reason': 'no_zcta_polygon_and_no_coordinates'}
    assert out['state']['geoid'] == '25' and out['us']['geoid'] == '1'


def test_a_window_without_zcta_tables_says_so(tmp_path: Path, resolver: geography.Resolver) -> None:
    r = geography.Resolver.__new__(geography.Resolver)
    r.__dict__.update(resolver.__dict__)
    r.zcta_published, r._memo = None, {}
    assert r.resolve('02114', None, 'Massachusetts')['zcta'] == {
        'geoid': None, 'reason': 'zcta_tables_not_published_for_this_window'}
