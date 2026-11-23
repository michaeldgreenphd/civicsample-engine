"""Trial facility geography and US site-mix classification.

Reads ``protocolSection.contactsLocationsModule.locations`` from raw
ClinicalTrials.gov API v2 study records, or ``study_sites`` from weekly
full-record containers (``src/full_records.py``).

Analytical invariants
---------------------
* Every output carries ``unit_basis = "listed_facilities_not_participants"``:
  ClinicalTrials.gov does not report participant counts per facility or per
  country on standard baseline tables, so ``us_facility_share`` is strictly a
  share of listed study facilities with a recorded country, never a share of
  enrolled participants.
* ``site_footprint_class`` partitions all study records into six mutually
  exclusive classes:
    - ``no_listed_facilities``: zero facilities listed on the registration.
    - ``facilities_missing_country``: at least one facility is listed, and
      either every facility lacks a country OR all country-bearing facilities
      lie on one side (all US or all ex-US) alongside at least one facility
      with a missing country (where ``us_only`` or ``ex_us_only`` cannot be
      established without guessing the unlocated site's country).
    - ``us_only``: >= 1 listed facility, zero facilities missing country, and
      every listed facility is in the United States or an inhabited US
      territory under FDA jurisdiction.
    - ``ex_us_only``: >= 1 listed facility, zero facilities missing country,
      and zero US facilities.
    - ``multinational_with_us_sites``: >= 1 US facility and >= 1 non-US
      facility.
    - ``footprint_unknown``: the site list cannot establish any class. Every
      weekly full record (``study_sites``) gets it: ``src/utils.py``
      ``get_study_metadata`` drops each location without a country before
      writing ``study_sites`` and the record keeps no total location count, so
      a dropped countryless site would turn ``us_only``, ``ex_us_only`` or
      ``no_listed_facilities`` into ``facilities_missing_country`` with nothing
      to show it. Raw CT.gov API v2 records keep every location.
* For ``multinational_with_us_sites``, ``us_site_share_bin`` classifies exact
  ``us_facility_share = n_us_facilities / n_facilities_with_country`` using
  exact integer cross-multiplication so rational boundaries (0.25, 0.50, 0.75)
  never suffer IEEE-754 floating-point drift.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from dap import UNIT_BASIS

SITE_FOOTPRINT_CLASSES: tuple[str, ...] = (
    "us_only",
    "multinational_with_us_sites",
    "ex_us_only",
    "facilities_missing_country",
    "no_listed_facilities",
    "footprint_unknown",
)

# Why a weekly record's footprint is ``footprint_unknown``; carried on the row.
WEEKLY_SITES_UNKNOWN_REASON: str = (
    "weekly study_sites omit every location without a country (src/utils.py get_study_metadata) "
    "and the record keeps no total location count, so us_only, ex_us_only, no_listed_facilities "
    "and facilities_missing_country cannot be told apart; build from raw CT.gov API v2 records "
    "for site-mix classes"
)

US_SITE_SHARE_BINS: tuple[str, ...] = (
    "us_share_lt_25",
    "us_share_25_to_lt_50",
    "us_share_50_to_lt_75",
    "us_share_gte_75",
)

_US_COUNTRY_KEYS: frozenset[str] = frozenset(
    {
        "united states",
        "united states of america",
        "u.s.",
        "u.s.a.",
        "usa",
    }
)

# ClinicalTrials.gov lists inhabited US territories in the location `country`
# field as ISO-3166 territory names; under 21 U.S.C. 355(z) / FDORA FDA
# jurisdiction they are US clinical trial sites and are also tallied in
# `us_state_or_territory_counts`.
_US_TERRITORY_BY_COUNTRY: dict[str, str] = {
    "puerto rico": "Puerto Rico",
    "guam": "Guam",
    "virgin islands (u.s.)": "U.S. Virgin Islands",
    "u.s. virgin islands": "U.S. Virgin Islands",
    "united states virgin islands": "U.S. Virgin Islands",
    "american samoa": "American Samoa",
    "northern mariana islands": "Northern Mariana Islands",
}

_US_JURISDICTION_MAP: dict[str, str] = {
    "al": "Alabama",
    "alabama": "Alabama",
    "ak": "Alaska",
    "alaska": "Alaska",
    "az": "Arizona",
    "arizona": "Arizona",
    "ar": "Arkansas",
    "arkansas": "Arkansas",
    "ca": "California",
    "california": "California",
    "co": "Colorado",
    "colorado": "Colorado",
    "ct": "Connecticut",
    "connecticut": "Connecticut",
    "de": "Delaware",
    "delaware": "Delaware",
    "dc": "District of Columbia",
    "d.c.": "District of Columbia",
    "district of columbia": "District of Columbia",
    "washington, d.c.": "District of Columbia",
    "fl": "Florida",
    "florida": "Florida",
    "ga": "Georgia",
    "georgia": "Georgia",
    "hi": "Hawaii",
    "hawaii": "Hawaii",
    "id": "Idaho",
    "idaho": "Idaho",
    "il": "Illinois",
    "illinois": "Illinois",
    "in": "Indiana",
    "indiana": "Indiana",
    "ia": "Iowa",
    "iowa": "Iowa",
    "ks": "Kansas",
    "kansas": "Kansas",
    "ky": "Kentucky",
    "kentucky": "Kentucky",
    "la": "Louisiana",
    "louisiana": "Louisiana",
    "me": "Maine",
    "maine": "Maine",
    "md": "Maryland",
    "maryland": "Maryland",
    "ma": "Massachusetts",
    "massachusetts": "Massachusetts",
    "mi": "Michigan",
    "michigan": "Michigan",
    "mn": "Minnesota",
    "minnesota": "Minnesota",
    "ms": "Mississippi",
    "mississippi": "Mississippi",
    "mo": "Missouri",
    "missouri": "Missouri",
    "mt": "Montana",
    "montana": "Montana",
    "ne": "Nebraska",
    "nebraska": "Nebraska",
    "nv": "Nevada",
    "nevada": "Nevada",
    "nh": "New Hampshire",
    "new hampshire": "New Hampshire",
    "nj": "New Jersey",
    "new jersey": "New Jersey",
    "nm": "New Mexico",
    "new mexico": "New Mexico",
    "ny": "New York",
    "new york": "New York",
    "nc": "North Carolina",
    "north carolina": "North Carolina",
    "nd": "North Dakota",
    "north dakota": "North Dakota",
    "oh": "Ohio",
    "ohio": "Ohio",
    "ok": "Oklahoma",
    "oklahoma": "Oklahoma",
    "or": "Oregon",
    "oregon": "Oregon",
    "pa": "Pennsylvania",
    "pennsylvania": "Pennsylvania",
    "ri": "Rhode Island",
    "rhode island": "Rhode Island",
    "sc": "South Carolina",
    "south carolina": "South Carolina",
    "sd": "South Dakota",
    "south dakota": "South Dakota",
    "tn": "Tennessee",
    "tennessee": "Tennessee",
    "tx": "Texas",
    "texas": "Texas",
    "ut": "Utah",
    "utah": "Utah",
    "vt": "Vermont",
    "vermont": "Vermont",
    "va": "Virginia",
    "virginia": "Virginia",
    "wa": "Washington",
    "washington": "Washington",
    "wv": "West Virginia",
    "west virginia": "West Virginia",
    "wi": "Wisconsin",
    "wisconsin": "Wisconsin",
    "wy": "Wyoming",
    "wyoming": "Wyoming",
    "pr": "Puerto Rico",
    "puerto rico": "Puerto Rico",
    "gu": "Guam",
    "guam": "Guam",
    "vi": "U.S. Virgin Islands",
    "u.s. virgin islands": "U.S. Virgin Islands",
    "virgin islands": "U.S. Virgin Islands",
    "virgin islands (u.s.)": "U.S. Virgin Islands",
    "as": "American Samoa",
    "american samoa": "American Samoa",
    "mp": "Northern Mariana Islands",
    "northern mariana islands": "Northern Mariana Islands",
}


def _clean_text(value: Any) -> str | None:
    """Collapse whitespace on a string field; return None when missing or blank."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if cleaned else None


def is_us_country(country: str | None) -> tuple[bool, str | None]:
    """Return ``(is_us, territory_name_if_territory_country)`` for a country label."""
    cleaned = _clean_text(country)
    if cleaned is None:
        return False, None
    key = cleaned.casefold()
    if key in _US_COUNTRY_KEYS:
        return True, None
    territory = _US_TERRITORY_BY_COUNTRY.get(key)
    if territory is not None:
        return True, territory
    return False, None


def normalize_us_jurisdiction(state: str | None) -> str | None:
    """Normalize a US state or territory name/code; keep unrecognized non-empty text verbatim."""
    cleaned = _clean_text(state)
    if cleaned is None:
        return None
    return _US_JURISDICTION_MAP.get(cleaned.casefold(), cleaned)


def classify_us_site_share_bin(n_us_facilities: int, n_facilities_with_country: int) -> str:
    """Classify ``n_us_facilities / n_facilities_with_country`` for multinational US-site trials.

    Uses integer cross-multiplication against 4 so exact rational cutpoints
    1/4 (0.25), 2/4 (0.50), and 3/4 (0.75) are exact for every denominator.
    """
    if n_facilities_with_country <= 0:
        raise ValueError("n_facilities_with_country must be positive")
    if n_us_facilities <= 0 or n_us_facilities >= n_facilities_with_country:
        raise ValueError(
            "classify_us_site_share_bin requires 0 < n_us_facilities < n_facilities_with_country "
            f"(got {n_us_facilities} / {n_facilities_with_country})"
        )
    scaled = 4 * n_us_facilities
    if scaled < n_facilities_with_country:
        return "us_share_lt_25"
    if scaled < 2 * n_facilities_with_country:
        return "us_share_25_to_lt_50"
    if scaled < 3 * n_facilities_with_country:
        return "us_share_50_to_lt_75"
    return "us_share_gte_75"


def extract_raw_locations(record: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return ``(locations, location_source)`` from either raw CT.gov v2 or weekly records.

    Caveat documented inline: ``src/utils.py::get_study_metadata`` omits
    location entries with an empty ``country`` when building ``study_sites``.
    Whenever ``protocolSection.contactsLocationsModule`` is available, we read
    it directly so facilities lacking ``country`` are retained and counted;
    a ``weekly_study_sites`` list is truncated, and ``extract_geography``
    classifies it ``footprint_unknown``.
    """
    protocol = record.get("protocolSection")
    if isinstance(protocol, dict):
        mod = protocol.get("contactsLocationsModule")
        if isinstance(mod, dict):
            locs = mod.get("locations")
            if isinstance(locs, list):
                return [x for x in locs if isinstance(x, dict)], "contacts_locations_module"
        return [], "contacts_locations_module"

    direct_mod = record.get("contactsLocationsModule")
    if isinstance(direct_mod, dict):
        locs = direct_mod.get("locations")
        if isinstance(locs, list):
            return [x for x in locs if isinstance(x, dict)], "contacts_locations_module"
        return [], "contacts_locations_module"

    if isinstance(record.get("locations"), list):
        return [x for x in record["locations"] if isinstance(x, dict)], "locations"

    if isinstance(record.get("study_sites"), list):
        return [x for x in record["study_sites"] if isinstance(x, dict)], "weekly_study_sites"

    return [], "absent"


def extract_geography(record: dict[str, Any]) -> dict[str, Any]:
    """Compute facility counts, US site share, footprint class, and share bin for one trial."""
    locations, location_source = extract_raw_locations(record)
    n_listed = len(locations)
    country_counter: Counter[str] = Counter()
    us_jurisdiction_counter: Counter[str] = Counter()
    n_with_country = 0
    n_missing_country = 0
    n_us = 0
    n_non_us = 0
    n_us_missing_state = 0

    for loc in locations:
        country = _clean_text(loc.get("country"))
        if country is None:
            n_missing_country += 1
            continue
        n_with_country += 1
        country_counter[country] += 1
        is_us, territory_from_country = is_us_country(country)
        if is_us:
            n_us += 1
            jurisdiction = normalize_us_jurisdiction(loc.get("state")) or territory_from_country
            if jurisdiction is not None:
                us_jurisdiction_counter[jurisdiction] += 1
            else:
                n_us_missing_state += 1
        else:
            n_non_us += 1

    us_share: float | None = (
        (n_us / n_facilities_denom)
        if (n_facilities_denom := n_with_country) > 0
        else None
    )

    # A weekly site list is truncated (WEEKLY_SITES_UNKNOWN_REASON), so no class
    # is asserted from it. Counts over sites WITH a country (n_us, n_non_us,
    # n_with_country, us_facility_share, country counts) stay exact; counts
    # that depend on the dropped sites are recorded as unknown (None) below.
    truncated = location_source == "weekly_study_sites"
    if truncated:
        footprint = "footprint_unknown"
    elif n_listed == 0:
        footprint = "no_listed_facilities"
    elif n_with_country == 0:
        footprint = "facilities_missing_country"
    elif n_us > 0 and n_non_us > 0:
        footprint = "multinational_with_us_sites"
    elif n_missing_country > 0:
        # All country-known sites sit on one side (all US or all non-US),
        # while >= 1 listed facility omits its country: neither us_only nor
        # ex_us_only is established without guessing the missing country.
        footprint = "facilities_missing_country"
    elif n_us > 0:
        footprint = "us_only"
    else:
        footprint = "ex_us_only"

    share_bin: str | None = (
        classify_us_site_share_bin(n_us, n_with_country)
        if footprint == "multinational_with_us_sites"
        else None
    )

    out: dict[str, Any] = {
        "unit_basis": UNIT_BASIS,
        "location_source": location_source,
        "n_listed_facilities": None if truncated else n_listed,
        "n_facilities_with_country": n_with_country,
        "n_facilities_missing_country": None if truncated else n_missing_country,
        "partial_missing_country": None if truncated else bool(n_with_country > 0 and n_missing_country > 0),
        "n_us_facilities": n_us,
        "n_non_us_facilities": n_non_us,
        "n_countries": len(country_counter),
        "country_counts": dict(sorted(country_counter.items())),
        "n_us_states_or_territories": len(us_jurisdiction_counter),
        "us_state_or_territory_counts": dict(sorted(us_jurisdiction_counter.items())),
        "n_us_facilities_missing_state": n_us_missing_state,
        "us_facility_share": us_share,
        "site_footprint_class": footprint,
        "us_site_share_bin": share_bin,
    }
    if truncated:
        out["footprint_unknown_reason"] = WEEKLY_SITES_UNKNOWN_REASON
    return out
