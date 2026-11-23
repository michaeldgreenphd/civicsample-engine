"""Multi-facility extraction, deduplication, footprint classification, and geo resolution.

ClinicalTrials.gov registrations list study facilities without per-site enrollment
counts, and baseline demographic tables pool participants across every site in the study.
Before any ACS reference population is computed, this module:
  1. Extracts locations from either weekly extracted study records (``locations`` or
     ``study_sites``) or raw ClinicalTrials.gov API v2 records
     (``protocolSection.contactsLocationsModule.locations``), normalizing 5-digit US ZIP
     codes, state/territory names, countries, and ``(lat, lon)`` coordinates.
  2. Deduplicates exact repeat listings of the same named facility while preserving
     distinct clinics and unnamed/redacted site slots in the same ZIP code.
  3. Partitions jurisdictions into the 50 US states + District of Columbia
     (``census.geography.STATE_FIPS``), US territories (Puerto Rico, Guam, U.S. Virgin
     Islands, American Samoa, Northern Mariana Islands), and foreign countries, assigning
     every study to one of six mutually exclusive, exhaustive ``footprint_tier`` values.
  4. Resolves US facilities across ``('zcta', 'county', 'cbsa', 'state', 'region', 'us')``
     through any duck-typed ``resolve(zip5, point, state_name) -> dict`` compatible with
     ``census.geography.Resolver``, recording unresolved reasons, distinct area counts,
     and facility Herfindahl-Hirschman Index (HHI) concentration.
"""
from __future__ import annotations

import collections
import math
import re
from typing import Any, Mapping, Protocol, Sequence

from census import geography

LEVEL_ORDER: tuple[str, ...] = ("zcta", "county", "cbsa", "state", "region", "us")

FOOTPRINT_TIERS: tuple[str, ...] = (
    "no_facilities_listed",
    "single_us_facility",
    "multi_site_us_only",
    "us_with_territories",
    "mixed_us_and_international",
    "non_us_only",
)

US_TERRITORIES: tuple[str, ...] = (
    "Puerto Rico",
    "Guam",
    "U.S. Virgin Islands",
    "American Samoa",
    "Northern Mariana Islands",
)

# Canonical state names in census.geography.STATE_FIPS (50 states + District of Columbia)
_STATE_BY_CASEFOLD: dict[str, str] = {
    name.casefold(): name for name in geography.STATE_FIPS
}
_STATE_POSTAL_TO_NAME: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_STATE_ALIASES: dict[str, str] = {
    "washington, d.c.": "District of Columbia",
    "washington d.c.": "District of Columbia",
    "washington dc": "District of Columbia",
    "d.c.": "District of Columbia",
    "district of columbia": "District of Columbia",
}

_TERRITORY_ALIASES: dict[str, str] = {
    "puerto rico": "Puerto Rico",
    "pr": "Puerto Rico",
    "commonwealth of puerto rico": "Puerto Rico",
    "guam": "Guam",
    "gu": "Guam",
    "u.s. virgin islands": "U.S. Virgin Islands",
    "us virgin islands": "U.S. Virgin Islands",
    "united states virgin islands": "U.S. Virgin Islands",
    "virgin islands (u.s.)": "U.S. Virgin Islands",
    "virgin islands, u.s.": "U.S. Virgin Islands",
    "virgin islands": "U.S. Virgin Islands",
    "usvi": "U.S. Virgin Islands",
    "vi": "U.S. Virgin Islands",
    "american samoa": "American Samoa",
    "as": "American Samoa",
    "northern mariana islands": "Northern Mariana Islands",
    "commonwealth of the northern mariana islands": "Northern Mariana Islands",
    "cnmi": "Northern Mariana Islands",
    "mp": "Northern Mariana Islands",
    "u.s. minor outlying islands": "Northern Mariana Islands",
    "united states minor outlying islands": "Northern Mariana Islands",
}

_MILITARY_STATE_ALIASES: dict[str, str] = {
    "aa": "Armed Forces Americas",
    "ae": "Armed Forces Europe",
    "ap": "Armed Forces Pacific",
    "armed forces americas": "Armed Forces Americas",
    "armed forces americas (aa)": "Armed Forces Americas",
    "armed forcesamericas(aa)": "Armed Forces Americas",
    "armed forces europe": "Armed Forces Europe",
    "armed forces europe (ae)": "Armed Forces Europe",
    "armed forceseurope(ae)": "Armed Forces Europe",
    "armed forces pacific": "Armed Forces Pacific",
    "armed forces pacific (ap)": "Armed Forces Pacific",
    "armed forcespacific(ap)": "Armed Forces Pacific",
    "armed forces": "U.S. Armed Forces Overseas",
    "armed forces africa": "Armed Forces Europe",
    "armed forces canada": "Armed Forces Europe",
    "armed forces middle east": "Armed Forces Europe",
}

_BVI_ZIP_RX = re.compile(r"^VG\d{4}$", re.IGNORECASE)
_BVI_PLACE_TOKENS: tuple[str, ...] = (
    "tortola",
    "road town",
    "virgin gorda",
    "anegada",
    "jost van dyke",
)

_US_COUNTRY_ALIASES: frozenset[str] = frozenset({
    "united states",
    "united states of america",
    "usa",
    "u.s.",
    "u.s.a.",
    "us",
})

# Generic redacted facility placeholders used by multi-site sponsors across distinct
# clinics in the same city/ZIP; two unnamed or generic-placeholder entries in one ZIP are
# not assumed to be the same physical clinic unless explicit address collapsing is asked.
_GENERIC_FACILITY_RX = re.compile(
    r"^(?:(?:investigational|research|clinical|study|participating|local|trial)\s+"
    r"(?:site|center|centre|facility|location)s?)$",
    re.IGNORECASE,
)

_US_ZIP_RX = re.compile(r"^(\d{5})(?:-\d{4})?$")
_PUNCT_DOT_SPACE_RX = re.compile(r"[\s.]+")


class SiteResolver(Protocol):
    """Duck-typed geography resolver matching ``census.geography.Resolver.resolve``."""

    def resolve(
        self,
        zip5: str | None,
        point: tuple[float, float] | None,
        state_name: str | None,
    ) -> dict[str, Any]: ...


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    return cleaned if cleaned else None


def normalize_zip5(raw_zip: Any) -> str | None:
    """Normalize a 5-digit US ZIP from ``12345`` or ``12345-6789``.

    Rejects non-US alphanumeric postal codes (e.g. ``M5G 2C4``, ``SW1A 1AA``), wrong-length
    numeric codes (e.g. 4-digit Swiss/Australian or 6-digit Chinese/Indian codes),
    malformed ZIP+4 suffixes, and the all-zero placeholder ``00000``.
    """
    text = _clean_text(raw_zip)
    if text is None:
        return None
    match = _US_ZIP_RX.fullmatch(text)
    if not match:
        return None
    zip5 = match.group(1)
    if zip5 == "00000":
        return None
    return zip5


def _territory_from_zip5(zip5: str | None) -> str | None:
    """Identify a US territory or overseas military APO/FPO/DPO range from its 5-digit ZIP."""
    if zip5 is None:
        return None
    prefix2 = zip5[:2]
    prefix3 = zip5[:3]
    if prefix2 == "09":
        return "Armed Forces Europe"
    if prefix3 == "340":
        return "Armed Forces Americas"
    if "962" <= prefix3 <= "966":
        return "Armed Forces Pacific"
    if prefix3 in ("006", "007", "009"):
        return "Puerto Rico"
    if prefix3 == "008":
        return "U.S. Virgin Islands"
    if zip5 == "96799":
        return "American Samoa"
    if "96910" <= zip5 <= "96932":
        return "Guam"
    if "96950" <= zip5 <= "96952":
        return "Northern Mariana Islands"
    return None


def _normalize_point(loc: Mapping[str, Any]) -> tuple[float, float] | None:
    """Extract validated ``(lat, lon)`` coordinates from API v2 or flat location dicts."""
    raw_lat: Any = None
    raw_lon: Any = None
    geo_point = loc.get("geoPoint")
    if isinstance(geo_point, Mapping):
        raw_lat = geo_point.get("lat")
        raw_lon = geo_point.get("lon")
    if raw_lat is None or raw_lon is None:
        pt = loc.get("point")
        if isinstance(pt, (tuple, list)) and len(pt) == 2:
            raw_lat, raw_lon = pt[0], pt[1]
    if raw_lat is None:
        raw_lat = loc.get("lat") if loc.get("lat") is not None else loc.get("latitude")
    if raw_lon is None:
        for key in ("lon", "lng", "longitude"):
            if loc.get(key) is not None:
                raw_lon = loc.get(key)
                break
    if raw_lat is None or raw_lon is None or isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
        return None
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


def _has_bvi_indicator(
    raw_country: str | None,
    raw_state: str | None,
    raw_zip: str | None,
    raw_city: str | None,
) -> bool:
    """Detect British Virgin Islands markers so generic ``country='Virgin Islands'`` never
    misclassifies Tortola / Road Town / ``VG1110`` / ``BVI`` clinics as U.S. Virgin Islands.
    """
    for field in (raw_country, raw_state, raw_city):
        if field is None:
            continue
        low = field.casefold()
        if "british" in low and "virgin" in low:
            return True
        comp = _PUNCT_DOT_SPACE_RX.sub("", low)
        if comp in ("bvi", "vg", "vgb"):
            return True
        if any(tok in low for tok in _BVI_PLACE_TOKENS):
            return True
    if raw_zip is not None and _BVI_ZIP_RX.fullmatch(raw_zip):
        return True
    return False


def normalize_state_or_territory(raw_state: Any) -> tuple[str | None, str | None]:
    """Return ``(canonical_state_or_territory, kind)`` where ``kind`` is
    ``'us_state_or_dc'``, ``'us_territory'``, or ``None`` if unrecognized.
    """
    text = _clean_text(raw_state)
    if text is None:
        return None, None
    folded = text.casefold()
    if "british" in folded:
        return text, None
    if folded in _STATE_BY_CASEFOLD:
        return _STATE_BY_CASEFOLD[folded], "us_state_or_dc"
    if folded in _STATE_ALIASES:
        return _STATE_ALIASES[folded], "us_state_or_dc"
    compact = _PUNCT_DOT_SPACE_RX.sub("", folded)
    if compact in ("bvi", "vg", "vgb"):
        return text, None
    upper_compact = compact.upper()
    if upper_compact in _STATE_POSTAL_TO_NAME:
        return _STATE_POSTAL_TO_NAME[upper_compact], "us_state_or_dc"
    if folded in _TERRITORY_ALIASES:
        return _TERRITORY_ALIASES[folded], "us_territory"
    if compact in _TERRITORY_ALIASES:
        return _TERRITORY_ALIASES[compact], "us_territory"
    if folded in _MILITARY_STATE_ALIASES:
        return _MILITARY_STATE_ALIASES[folded], "us_territory"
    if compact in _MILITARY_STATE_ALIASES:
        return _MILITARY_STATE_ALIASES[compact], "us_territory"
    if folded.startswith("armed forces"):
        return "U.S. Armed Forces Overseas", "us_territory"
    return text, None


def _match_territory_country(country_text: str) -> str | None:
    """Match a country string against US territory names and ISO-3166 alpha-2 codes."""
    folded = country_text.casefold()
    if "british" in folded:
        return None
    if folded in _TERRITORY_ALIASES:
        return _TERRITORY_ALIASES[folded]
    compact = _PUNCT_DOT_SPACE_RX.sub("", folded)
    if compact in _TERRITORY_ALIASES:
        return _TERRITORY_ALIASES[compact]
    return None


def classify_jurisdiction(
    raw_country: Any,
    raw_state: Any,
    raw_zip: Any = None,
    *,
    raw_city: Any = None,
) -> tuple[str, str | None, str | None]:
    """Classify a facility's jurisdiction into one of three mutually exclusive buckets:
    ``'us_state_or_dc'``, ``'us_territory'``, or ``'non_us'``.

    Returns ``(jurisdiction, canonical_country_or_territory, canonical_state_or_territory)``.
    """
    country_text = _clean_text(raw_country)
    state_raw_text = _clean_text(raw_state)
    zip_raw_text = _clean_text(raw_zip)
    city_raw_text = _clean_text(raw_city)

    state_norm, state_kind = normalize_state_or_territory(raw_state)
    candidate_zip5 = normalize_zip5(raw_zip)

    if country_text is not None:
        c_fold = country_text.casefold()
        c_compact = _PUNCT_DOT_SPACE_RX.sub("", c_fold)
        if (c_fold in _US_COUNTRY_ALIASES or c_compact in ("usa", "us")) and state_kind == "us_state_or_dc":
            return "us_state_or_dc", "United States", state_norm

    if _has_bvi_indicator(country_text, state_raw_text, zip_raw_text, city_raw_text):
        return "non_us", country_text, state_raw_text

    if country_text is not None:
        c_fold = country_text.casefold()
        c_compact = _PUNCT_DOT_SPACE_RX.sub("", c_fold)
        # ClinicalTrials.gov API v2 lists Puerto Rico, Guam, etc. in the country field,
        # while some registrations use ISO-2 territory codes (PR, GU, VI, AS, MP) or list
        # country="United States" alongside a territory/APO state, abbreviation, or ZIP.
        terr_country = _match_territory_country(country_text)
        if terr_country is not None:
            return "us_territory", terr_country, terr_country
        if c_fold in _US_COUNTRY_ALIASES or c_compact in ("usa", "us"):
            if state_kind == "us_territory":
                return "us_territory", state_norm, state_norm
            if state_kind == "us_state_or_dc":
                return "us_state_or_dc", "United States", state_norm
            terr_from_zip = _territory_from_zip5(candidate_zip5)
            if terr_from_zip is not None:
                return "us_territory", terr_from_zip, terr_from_zip
            if city_raw_text is not None and city_raw_text.upper() in ("APO", "FPO", "DPO"):
                return "us_territory", "U.S. Armed Forces Overseas", "U.S. Armed Forces Overseas"
            return "us_state_or_dc", "United States", None
        return "non_us", country_text, state_raw_text

    # Country omitted in registration: infer US state/territory only when state is an
    # unambiguous full name or multi-word alias (avoiding 2-letter collisions with ISO
    # regions when country is absent, except when paired with a valid 5-digit US ZIP).
    if state_raw_text is not None:
        s_fold = state_raw_text.casefold()
        if s_fold in _STATE_BY_CASEFOLD or s_fold in _STATE_ALIASES:
            return "us_state_or_dc", "United States", state_norm
        if state_kind == "us_territory" and len(_PUNCT_DOT_SPACE_RX.sub("", s_fold)) > 2:
            return "us_territory", state_norm, state_norm
        if candidate_zip5 is not None and state_kind == "us_state_or_dc":
            return "us_state_or_dc", "United States", state_norm
        if candidate_zip5 is not None and state_kind == "us_territory":
            return "us_territory", state_norm, state_norm

    return "non_us", None, state_raw_text


def normalize_facility(loc: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a single location dictionary from extracted or raw API v2 records."""
    raw_facility = loc.get("facility") if loc.get("facility") is not None else loc.get("name")
    facility_name = _clean_text(raw_facility)
    city = _clean_text(loc.get("city"))
    raw_state = _clean_text(loc.get("state"))
    raw_zip = _clean_text(loc.get("zip") if loc.get("zip") is not None else loc.get("postalCode"))
    raw_country = _clean_text(loc.get("country"))
    status = _clean_text(loc.get("status"))
    point = _normalize_point(loc)

    jurisdiction, canonical_country, canonical_state = classify_jurisdiction(
        raw_country, raw_state, raw_zip, raw_city=city
    )
    # Only US and US-territory facilities receive a 5-digit US ZIP; foreign 5-digit
    # postal codes (e.g. Paris 75001 or Berlin 10115) must never resolve against US ZCTAs.
    zip5 = normalize_zip5(raw_zip) if jurisdiction in ("us_state_or_dc", "us_territory") else None

    return {
        "facility": facility_name,
        "city": city,
        "state": canonical_state if jurisdiction in ("us_state_or_dc", "us_territory") else raw_state,
        "raw_state": raw_state,
        "zip_as_registered": raw_zip,
        "zip5": zip5,
        "country": canonical_country,
        "raw_country": raw_country,
        "jurisdiction": jurisdiction,
        "point": point,
        "status": status,
    }


def _raw_locations_with_source(record: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str]:
    """Read raw location list and provenance source from extracted or API v2 study records.

    Prefers explicit ``locations``, then raw API v2 ``protocolSection.contactsLocationsModule.locations``
    (which retains countryless sites and ``geoPoint`` coordinates), then ``study_sites`` (the weekly
    projection in ``demographics.json.gz``, which omits countryless sites and coordinates; see
    ``src/utils.py``). Empty placeholder lists never shadow populated fallback lists.
    """
    named_candidates: list[tuple[str, list[Any]]] = []
    if isinstance(record.get("locations"), list):
        named_candidates.append(("locations", record["locations"]))
    protocol = record.get("protocolSection")
    if isinstance(protocol, Mapping):
        contacts_locs = protocol.get("contactsLocationsModule")
        if isinstance(contacts_locs, Mapping) and isinstance(contacts_locs.get("locations"), list):
            named_candidates.append(("contacts_locations_module", contacts_locs["locations"]))
    if isinstance(record.get("study_sites"), list):
        named_candidates.append(("study_sites_projection", record["study_sites"]))

    for source_name, raw_list in named_candidates:
        valid_items = [x for x in raw_list if isinstance(x, Mapping)]
        if valid_items:
            return valid_items, source_name
    if named_candidates:
        return [], named_candidates[0][0]
    return [], "none"


def _raw_locations_from_record(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read raw location list from extracted (``locations`` / ``study_sites``) or API v2 record."""
    items, _ = _raw_locations_with_source(record)
    return items


def _is_specific_facility_name(name: str | None) -> bool:
    if name is None:
        return False
    return not bool(_GENERIC_FACILITY_RX.fullmatch(name))


def deduplicate_facilities(
    facilities: Sequence[dict[str, Any]],
    *,
    collapse_unnamed: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate exact repeat listings of the same facility.

    Two entries are merged when they carry the same specific (non-generic) facility name
    at the same normalized ``(city, state, zip5_or_raw_zip, country, jurisdiction)``.
    Unnamed entries or generic sponsor placeholders (e.g. ``"Investigational Site"``) are
    kept distinct by default because multi-site sponsors often redact clinic names in a
    shared city/ZIP; set ``collapse_unnamed=True`` to collapse identical unnamed entries.
    When a duplicate is merged, coordinates or status present on the later entry fill in
    missing values on the retained entry.
    """
    deduped: list[dict[str, Any]] = []
    seen_index: dict[tuple[str, ...], int] = {}
    removed = 0

    for fac in facilities:
        if not isinstance(fac, Mapping):
            continue
        name = fac.get("facility")
        can_dedup = _is_specific_facility_name(name) or collapse_unnamed
        if not can_dedup:
            deduped.append(dict(fac))
            continue

        zip_key = (fac.get("zip5") or fac.get("zip_as_registered") or "").casefold()
        key = (
            (name or "").casefold(),
            (fac.get("city") or "").casefold(),
            (fac.get("state") or "").casefold(),
            zip_key,
            (fac.get("country") or "").casefold(),
            str(fac.get("jurisdiction") or ""),
        )
        if key in seen_index:
            removed += 1
            existing = deduped[seen_index[key]]
            if existing.get("point") is None and fac.get("point") is not None:
                existing["point"] = fac["point"]
            if existing.get("status") is None and fac.get("status") is not None:
                existing["status"] = fac["status"]
            if existing.get("zip5") is None and fac.get("zip5") is not None:
                existing["zip5"] = fac["zip5"]
        else:
            seen_index[key] = len(deduped)
            deduped.append(dict(fac))

    return deduped, removed


def extract_facilities(
    record: Mapping[str, Any],
    *,
    deduplicate: bool = True,
    collapse_unnamed: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Extract, normalize, and optionally deduplicate study facilities from a record."""
    raw_locs = _raw_locations_from_record(record)
    normalized = [normalize_facility(loc) for loc in raw_locs]
    if deduplicate:
        facilities, n_removed = deduplicate_facilities(
            normalized, collapse_unnamed=collapse_unnamed
        )
    else:
        facilities, n_removed = normalized, 0
    meta = {
        "n_raw_locations": len(raw_locs),
        "n_facilities": len(facilities),
        "n_duplicate_locations_removed": n_removed,
    }
    return facilities, meta


def classify_footprint(
    record_or_facilities: Mapping[str, Any] | Sequence[dict[str, Any]],
    *,
    deduplicate: bool = True,
    collapse_unnamed: bool = False,
) -> dict[str, Any]:
    """Classify a study's facility listing into one of the 6 mutually exclusive,
    exhaustive ``FOOTPRINT_TIERS``:
      1. ``"no_facilities_listed"`` — 0 facilities
      2. ``"single_us_facility"`` — 1 facility, in 50 US states + DC
      3. ``"multi_site_us_only"`` — >= 2 facilities, 100% in 50 US states + DC
      4. ``"us_with_territories"`` — >= 1 facility in US states/DC/territories, including
         >= 1 US territory and 0 foreign countries
      5. ``"mixed_us_and_international"`` — >= 1 US (state/DC/territory) facility AND
         >= 1 foreign facility (sets ``global_pooled_baseline_table = True``)
      6. ``"non_us_only"`` — >= 1 facility, 0 in US states/DC or US territories
    """
    if isinstance(record_or_facilities, Mapping):
        _, location_source = _raw_locations_with_source(record_or_facilities)
        facilities, dedup_meta = extract_facilities(
            record_or_facilities,
            deduplicate=deduplicate,
            collapse_unnamed=collapse_unnamed,
        )
    else:
        location_source = "sequence"
        raw_mappings = [x for x in record_or_facilities if isinstance(x, Mapping)]
        norm_list = [
            dict(f) if "jurisdiction" in f and "zip5" in f else normalize_facility(f)
            for f in raw_mappings
        ]
        if deduplicate:
            facilities, n_removed = deduplicate_facilities(
                norm_list, collapse_unnamed=collapse_unnamed
            )
        else:
            facilities, n_removed = norm_list, 0
        dedup_meta = {
            "n_raw_locations": len(raw_mappings),
            "n_facilities": len(facilities),
            "n_duplicate_locations_removed": n_removed,
        }

    us_50_dc = [f for f in facilities if f["jurisdiction"] == "us_state_or_dc"]
    us_terr = [f for f in facilities if f["jurisdiction"] == "us_territory"]
    non_us = [f for f in facilities if f["jurisdiction"] == "non_us"]

    n_total = len(facilities)
    n_s = len(us_50_dc)
    n_t = len(us_terr)
    n_f = len(non_us)
    n_us_all = n_s + n_t

    if n_total == 0:
        tier = "no_facilities_listed"
    elif n_us_all == 0:
        tier = "non_us_only"
    elif n_f > 0:
        tier = "mixed_us_and_international"
    elif n_t > 0:
        tier = "us_with_territories"
    elif n_s == 1:
        tier = "single_us_facility"
    else:
        tier = "multi_site_us_only"

    global_pooled = tier == "mixed_us_and_international"
    states = sorted({str(f["state"]) for f in us_50_dc if f.get("state")})
    territories = sorted({str(f["state"]) for f in us_terr if f.get("state")})
    foreign_countries = sorted({str(f["country"]) for f in non_us if f.get("country")})

    return {
        "footprint_tier": tier,
        "location_source": location_source,
        "weekly_projection_omits_countryless_sites": location_source == "study_sites_projection",
        "n_raw_locations": dedup_meta["n_raw_locations"],
        "n_facilities": n_total,
        "n_duplicate_locations_removed": dedup_meta["n_duplicate_locations_removed"],
        "n_us_state_or_dc_facilities": n_s,
        "n_us_territory_facilities": n_t,
        "n_us_facilities": n_us_all,
        "n_non_us_facilities": n_f,
        "us_facility_share": round(n_us_all / n_total, 6) if n_total > 0 else None,
        "global_pooled_baseline_table": global_pooled,
        "eligible_for_us_single_site": tier == "single_us_facility",
        "eligible_for_us_multisite_rollup": tier == "multi_site_us_only",
        "us_states_and_dc": states,
        "us_territories": territories,
        "foreign_countries": foreign_countries,
        "facilities": facilities,
    }


def resolve_us_facilities(
    facilities: Sequence[dict[str, Any]],
    resolver: SiteResolver | None,
    *,
    levels: Sequence[str] = LEVEL_ORDER,
    no_resolver_reason: str = "no_benchmark_window",
    unpublished_levels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve US facilities across geography levels and compute per-level accounting.

    Facilities in the 50 US states + DC are passed to ``resolver.resolve(zip5, point, state)``.
    Facilities in US territories are counted in ``n_us_facilities`` and marked unresolved on
    50-state+DC ACS geography levels with reason
    ``'us_territory_outside_50_states_dc_acs_frame'``, ensuring ``all_us_facilities_resolved``
    is ``False`` whenever a trial includes territory sites outside the standard ACS frame.
    Foreign (``non_us``) facilities are excluded from ``n_us_facilities``.
    """
    unpub_map = dict(unpublished_levels) if unpublished_levels is not None else {}
    us_facilities: list[dict[str, Any]] = []
    for raw_fac in facilities:
        if not isinstance(raw_fac, Mapping):
            continue
        fac = (
            dict(raw_fac)
            if "jurisdiction" in raw_fac and "zip5" in raw_fac
            else normalize_facility(raw_fac)
        )
        if fac["jurisdiction"] in ("us_state_or_dc", "us_territory"):
            us_facilities.append(fac)

    n_us = len(us_facilities)
    per_facility_resolutions: list[dict[str, Any]] = []

    for idx, fac in enumerate(us_facilities):
        if fac["jurisdiction"] == "us_territory":
            fac_levels = {
                lvl: {
                    "geoid": None,
                    "reason": unpub_map.get(
                        lvl, "us_territory_outside_50_states_dc_acs_frame"
                    ),
                }
                for lvl in levels
            }
        elif resolver is None:
            fac_levels = {
                lvl: {"geoid": None, "reason": unpub_map.get(lvl, no_resolver_reason)}
                for lvl in levels
            }
        else:
            raw_res = resolver.resolve(fac.get("zip5"), fac.get("point"), fac.get("state"))
            fac_levels = {}
            for lvl in levels:
                if lvl in unpub_map:
                    fac_levels[lvl] = {"geoid": None, "reason": unpub_map[lvl]}
                    continue
                cell = raw_res.get(lvl) if isinstance(raw_res, Mapping) else None
                if isinstance(cell, Mapping) and cell.get("geoid"):
                    fac_levels[lvl] = dict(cell)
                elif isinstance(cell, Mapping):
                    fac_levels[lvl] = {
                        "geoid": None,
                        "reason": str(cell.get("reason") or "unresolved"),
                        **({
                            "area_share": cell["area_share"]
                        } if "area_share" in cell else {}),
                    }
                else:
                    fac_levels[lvl] = {
                        "geoid": None,
                        "reason": "level_not_returned_by_resolver",
                    }
        per_facility_resolutions.append({
            "us_facility_index": idx,
            "facility": fac.get("facility"),
            "city": fac.get("city"),
            "state": fac.get("state"),
            "zip5": fac.get("zip5"),
            "jurisdiction": fac["jurisdiction"],
            "levels": fac_levels,
        })

    by_level: dict[str, dict[str, Any]] = {}
    for lvl in levels:
        geoid_counts: collections.Counter[str] = collections.Counter()
        reason_counts: collections.Counter[str] = collections.Counter()
        for item in per_facility_resolutions:
            cell = item["levels"][lvl]
            geoid = cell.get("geoid")
            if geoid:
                geoid_counts[str(geoid)] += 1
            else:
                reason_counts[str(cell.get("reason") or "unresolved")] += 1

        n_resolved = sum(geoid_counts.values())
        n_unresolved = n_us - n_resolved
        sorted_counts = {g: geoid_counts[g] for g in sorted(geoid_counts)}
        hhi: float | None = None
        if n_resolved > 0:
            hhi = round(
                sum((count / n_resolved) ** 2 for count in sorted_counts.values()),
                6,
            )

        by_level[lvl] = {
            "level": lvl,
            "n_us_facilities": n_us,
            "n_resolved_facilities": n_resolved,
            "n_unresolved_facilities": n_unresolved,
            "all_us_facilities_resolved": bool(n_us > 0 and n_resolved == n_us),
            "unresolved_reasons": {r: reason_counts[r] for r in sorted(reason_counts)},
            "n_distinct_areas": len(sorted_counts),
            "facility_counts_by_geoid": sorted_counts,
            "facility_hhi": hhi,
        }

    return {
        "n_us_facilities": n_us,
        "facilities": per_facility_resolutions,
        "by_level": by_level,
    }
