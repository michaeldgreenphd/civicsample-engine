"""Where a site is, in each window's own geography, and how that was decided.

What the registry gives: a postal ZIP code, a city and state, and coordinates. The coordinates
are the city's, not the facility's: every facility in one ZIP carries the same point (110
facilities in 77030 share one point in downtown Houston). So the ZIP is the most precise
locator, and the point is a fallback.

A ZIP code is a set of mail routes; a ZIP Code Tabulation Area (ZCTA) is the Census Bureau's
area built from census blocks to approximate one. They are not interchangeable: a ZIP used only
for PO boxes or one organisation has no ZCTA, and ZCTAs were redrawn in 2020. A site is matched
to a ZCTA only when a ZCTA with that code is published in the window's own file, and the method
is recorded as an identity of codes, not of areas.

County and metro area come from the ZCTA's polygon: the unit holding the largest share of the
ZCTA's area, with that share kept as the match confidence. Each window uses boundary files of
its own vintage, so identifiers that changed (Connecticut's counties became planning regions in
the 2022 files; ZCTAs were redefined in 2020) are resolved rather than joined on stale codes.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

BASE = 'https://www2.census.gov/geo/tiger/'
# Cartographic boundary files (1:500,000). The 2005-2009 window predates the cb_ series, so it
# uses the 2010 generalised files, the closest published; ZCTA polygons for the three windows
# before 2020 are the 2010 definitions (ZCTA5CE10), unchanged through 2019.
BOUNDARIES: dict[int, dict[str, str]] = {
    2009: {'county': 'GENZ2010/gz_2010_us_050_00_500k.zip', 'cbsa': 'GENZ2010/gz_2010_us_310_m1_500k.zip',
           'zcta': 'GENZ2019/shp/cb_2019_us_zcta510_500k.zip'},
    2014: {'county': 'GENZ2014/shp/cb_2014_us_county_500k.zip', 'cbsa': 'GENZ2014/shp/cb_2014_us_cbsa_500k.zip',
           'zcta': 'GENZ2019/shp/cb_2019_us_zcta510_500k.zip'},
    2019: {'county': 'GENZ2019/shp/cb_2019_us_county_500k.zip', 'cbsa': 'GENZ2019/shp/cb_2019_us_cbsa_500k.zip',
           'zcta': 'GENZ2019/shp/cb_2019_us_zcta510_500k.zip'},
    2024: {'county': 'GENZ2024/shp/cb_2024_us_county_500k.zip', 'cbsa': 'GENZ2024/shp/cb_2024_us_cbsa_500k.zip',
           'zcta': 'GENZ2020/shp/cb_2020_us_zcta520_500k.zip'}}
# Below this share of a ZCTA's area, no metro area is assigned: the site is treated as outside
# any. A judgement; the share is recorded so it can be revisited.
CBSA_MIN_SHARE = 0.5
REGION = {**dict.fromkeys(('09', '23', '25', '33', '44', '50', '34', '36', '42'), '1'),
          **dict.fromkeys(('17', '18', '26', '39', '55', '19', '20', '27', '29', '31', '38', '46'), '2'),
          **dict.fromkeys(('10', '11', '12', '13', '24', '37', '45', '51', '54', '01', '21', '28', '47',
                           '05', '22', '40', '48'), '3'),
          **dict.fromkeys(('04', '08', '16', '30', '32', '35', '49', '56', '02', '06', '15', '41', '53'), '4')}
STATE_FIPS = {
    'Alabama': '01', 'Alaska': '02', 'Arizona': '04', 'Arkansas': '05', 'California': '06', 'Colorado': '08',
    'Connecticut': '09', 'Delaware': '10', 'District of Columbia': '11', 'Florida': '12', 'Georgia': '13',
    'Hawaii': '15', 'Idaho': '16', 'Illinois': '17', 'Indiana': '18', 'Iowa': '19', 'Kansas': '20',
    'Kentucky': '21', 'Louisiana': '22', 'Maine': '23', 'Maryland': '24', 'Massachusetts': '25',
    'Michigan': '26', 'Minnesota': '27', 'Mississippi': '28', 'Missouri': '29', 'Montana': '30',
    'Nebraska': '31', 'Nevada': '32', 'New Hampshire': '33', 'New Jersey': '34', 'New Mexico': '35',
    'New York': '36', 'North Carolina': '37', 'North Dakota': '38', 'Ohio': '39', 'Oklahoma': '40',
    'Oregon': '41', 'Pennsylvania': '42', 'Rhode Island': '44', 'South Carolina': '45', 'South Dakota': '46',
    'Tennessee': '47', 'Texas': '48', 'Utah': '49', 'Vermont': '50', 'Virginia': '51', 'Washington': '53',
    'West Virginia': '54', 'Wisconsin': '55', 'Wyoming': '56'}


def download(rel: str, cache: Path) -> dict[str, Any]:
    """Fetch one boundary file once; return its provenance (URL, time, sha256)."""
    path = cache / rel.replace('/', '__')
    meta_path = path.with_suffix(path.suffix + '.meta.json')
    if path.exists() and meta_path.exists():
        return json.loads(meta_path.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    r = requests.get(BASE + rel, timeout=900)
    r.raise_for_status()
    path.write_bytes(r.content)
    zipfile.ZipFile(path).testzip()
    meta = {'url': BASE + rel, 'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'sha256': hashlib.sha256(r.content).hexdigest(), 'bytes': len(r.content)}
    meta_path.write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def _code(record: dict[str, Any], kind: str) -> str:
    """The unit's identifier, across the field names the files have used."""
    if kind == 'county':
        return record.get('GEOID') or (record['STATE'] + record['COUNTY'])
    if kind == 'cbsa':
        return record.get('GEOID') or record.get('CBSAFP') or record['CBSA']
    return record.get('ZCTA5CE20') or record.get('ZCTA5CE10') or record['GEOID10']


def read_shapes(path: Path, kind: str, only: Iterable[str] | None = None) -> dict[str, Any]:
    """{code: shapely geometry}. buffer(0) repairs the few self-touching rings these generalised
    files contain so that areas and overlaps can be computed."""
    import shapefile
    from shapely.geometry import shape
    want = set(only) if only is not None else None
    out: dict[str, Any] = {}
    # Only the identifier is read. The 2010 files store names in Latin-1 ("Doña Ana"); an
    # undecodable name must not stop the build, so decoding errors are replaced.
    with shapefile.Reader(str(path), encodingErrors='replace') as reader:
        for sr in reader.iterShapeRecords():
            code = _code(sr.record.as_dict(), kind)
            if want is None or code in want:
                geom = shape(sr.shape.__geo_interface__)
                out[code] = geom if geom.is_valid else geom.buffer(0)
    return out


class Resolver:
    """Resolves ZIP codes and fallback points against one window's boundaries."""

    def __init__(self, vintage: int, cache: Path, zips: Iterable[str], zcta_published: set[str] | None) -> None:
        from shapely.strtree import STRtree
        files = BOUNDARIES[vintage]
        self.vintage, self.files = vintage, files
        self.zcta_published = zcta_published      # None: this window publishes no ZCTA tables
        self.zcta_shape = read_shapes(cache / files['zcta'].replace('/', '__'), 'zcta', only=set(zips))
        self.units, self.trees = {}, {}
        for kind in ('county', 'cbsa'):
            shapes = read_shapes(cache / files[kind].replace('/', '__'), kind)
            codes = list(shapes)
            self.units[kind] = (codes, [shapes[c] for c in codes])
            self.trees[kind] = STRtree(self.units[kind][1])
        self._memo: dict[str, dict[str, Any]] = {}

    def _largest_overlap(self, geom: Any, kind: str) -> tuple[str | None, float]:
        codes, shapes = self.units[kind]
        best, share = None, 0.0
        for i in self.trees[kind].query(geom):
            part = geom.intersection(shapes[i]).area / geom.area if geom.area else 0.0
            if part > share:
                best, share = codes[i], part
        return best, round(float(share), 3)

    def _point(self, lon: float, lat: float, kind: str) -> str | None:
        from shapely.geometry import Point
        codes, shapes = self.units[kind]
        p = Point(lon, lat)
        return next((codes[i] for i in self.trees[kind].query(p) if shapes[i].covers(p)), None)

    def resolve(self, zip5: str | None, point: tuple[float, float] | None, state_name: str | None) -> dict[str, Any]:
        """ZCTA, county, metro area, state and region for one site, each with its method.

        Nothing is guessed: a unit that cannot be resolved carries a reason instead of a code."""
        key = f'{zip5}|{point}|{state_name}'
        if key in self._memo:
            return self._memo[key]
        out: dict[str, Any] = {'boundary_files': self.files}
        geom = self.zcta_shape.get(zip5) if zip5 else None
        if self.zcta_published is None:
            out['zcta'] = {'geoid': None, 'reason': 'zcta_tables_not_published_for_this_window'}
        elif not zip5:
            out['zcta'] = {'geoid': None, 'reason': 'no_zip_in_registration'}
        elif zip5 in self.zcta_published:
            out['zcta'] = {'geoid': zip5, 'method': 'zip_code_equals_published_zcta_code'}
        else:
            out['zcta'] = {'geoid': None, 'reason': 'zip_has_no_zcta_in_this_window'}
        point_county = self._point(point[1], point[0], 'county') if point else None
        if geom is not None:
            county, share = self._largest_overlap(geom, 'county')
            out['county'] = {'geoid': county, 'method': 'zcta_polygon_largest_area_share', 'area_share': share,
                             'agrees_with_city_point': None if point_county is None else point_county == county}
            cbsa, cshare = self._largest_overlap(geom, 'cbsa')
            out['cbsa'] = ({'geoid': cbsa, 'method': 'zcta_polygon_largest_area_share', 'area_share': cshare}
                           if cbsa and cshare >= CBSA_MIN_SHARE else
                           {'geoid': None, 'reason': 'outside_any_metro_or_micro_area', 'area_share': cshare})
        elif point:
            # The city's point: right for most single-county cities, wrong for some sites in
            # cities that span counties. Kept, and labelled as the weaker method.
            out['county'] = ({'geoid': point_county, 'method': 'city_point_in_polygon', 'confidence': 'low'}
                             if point_county else {'geoid': None, 'reason': 'city_point_outside_every_county'})
            cbsa = self._point(point[1], point[0], 'cbsa')
            out['cbsa'] = ({'geoid': cbsa, 'method': 'city_point_in_polygon', 'confidence': 'low'} if cbsa else
                           {'geoid': None, 'reason': 'outside_any_metro_or_micro_area'})
        else:
            out['county'] = out['cbsa'] = {'geoid': None, 'reason': 'no_zcta_polygon_and_no_coordinates'}
        registered = STATE_FIPS.get(state_name or '')
        county_state = (out['county'].get('geoid') or '')[:2] or None
        if registered and county_state and registered != county_state:
            # The registered state is what the sponsor wrote; a county in another state means the
            # ZIP or the point is wrong. The state stands, the finer units are withdrawn.
            for kind in ('zcta', 'county', 'cbsa'):
                out[kind] = {'geoid': None, 'reason': 'resolved_place_is_outside_the_registered_state'}
        state = registered or county_state
        out['state'] = ({'geoid': state, 'method': 'registered_state_name' if registered else 'state_of_resolved_county'}
                        if state else {'geoid': None, 'reason': 'state_not_recognised'})
        out['region'] = ({'geoid': REGION[state], 'method': 'census_region_of_state'} if state in REGION else
                         {'geoid': None, 'reason': 'state_not_in_a_census_region'})
        out['us'] = {'geoid': '1', 'method': 'national'}
        self._memo[key] = out
        return out
