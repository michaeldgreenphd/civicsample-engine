# Multi-site US catchment bounds and footprint engine (`catchments/`)

Extends [`census/`](../census/README.md) from single-site US registrations to multi-facility
studies without inventing per-site participant enrollment weights.

## Methodological scope and non-claims

ClinicalTrials.gov registrations list study facilities without per-facility participant counts,
and baseline characteristics tables report combined participant counts across all study sites.
Every reference figure in this package is a **site-area population benchmark** derived from the
American Community Survey (ACS) 5-year tables (`B01001`, `B01001A`–`B01001G`, `B01001I`)
restricted to the trial's registered age and sex eligibility:

- It is **not** a record of where participants live, the disease prevalence distribution, or an
  enrollment target.
- No per-site participant counts are imputed or assumed for primary settlement claims. Instead,
  primary directional claims are evaluated over the exact convex hull of all non-negative
  per-site participant allocations alongside unknown-category bounds.

## Module layout

| Module | Responsibility |
|---|---|
| `catchments/footprint.py` | Extracts locations from extracted records (`locations` or `study_sites`) or raw API v2 records (`protocolSection.contactsLocationsModule.locations`), normalizes 5-digit US ZIPs (`12345` or `12345-6789`) and `(lat, lon)` coordinates, deduplicates exact repeat listings of named facilities, classifies studies into 6 footprint tiers, and resolves US sites across `('zcta', 'county', 'cbsa', 'state', 'region', 'us')` with HHI concentration. |
| `catchments/bounds.py` | Computes convex-hull `site_allocation_bounds`, widens unconditional bounds to `[0.0, 100.0]` whenever any US site is unresolved or lacks an ACS estimate, computes covariance-deduplicated `equal_facility_proxy` and pooled `distinct_area_resident_proxy` with 90% MOEs, and evaluates joint unknown-by-site settlement (`compare_with_catchment_bounds`). |
| `catchments/benchmark.py` | Runs `benchmark_multisite_trial` across all 6 geography levels (enforcing `acs.LEVELS_BY_VINTAGE` where 2005–2009 publishes no ZCTA tables) and 3 dimensions (`race`, `ethnicity`, `sex`), and `pool_multisite_cohort` over `multi_site_us_only` trials while isolating `mixed_us_and_international` studies. |

## Six mutually exclusive site-footprint tiers (`footprint_tier`)

Every study (for example, fictional `NCT99000201`) is partitioned into one of six mutually
exclusive, exhaustive tiers based on normalized jurisdictions across the 50 US states + District
of Columbia (`census.geography.STATE_FIPS`), US territories (`Puerto Rico`, `Guam`,
`U.S. Virgin Islands`, `American Samoa`, `Northern Mariana Islands`), and foreign countries:

1. `"no_facilities_listed"` — 0 listed facilities.
2. `"single_us_facility"` — 1 facility, in the 50 US states + DC. Collapses identically to
   `census.benchmark.expected`.
3. `"multi_site_us_only"` — 2 or more facilities, 100% in the 50 US states + DC. Primary cohort
   for US multi-site catchment rollups.
4. `"us_with_territories"` — 1 or more facilities in US states/DC/territories, including at least
   1 US territory and 0 foreign countries. Separated because standard ACS 5-year `us:1`, Census
   region, and ZCTA/CBSA frames do not uniformly cover Puerto Rico and the Island Areas; territory
   sites remain marked unresolved against the 50-state+DC ACS frame (`bounds_unconditional = False`).
5. `"mixed_us_and_international"` — at least 1 US (state/DC/territory) facility and at least 1
   foreign facility. Carries `global_pooled_baseline_table = True`: because ClinicalTrials.gov
   baseline tables pool US and international participants without country-level stratification,
   US facility areas are **not** a standalone catchment for the pooled table and are never mixed
   into `multi_site_us_only` cohort figures.
6. `"non_us_only"` — 1 or more facilities, all outside the US and US territories.

Foreign 5-digit postal codes (such as `75001` in France or `10115` in Germany) are never assigned
a US `zip5`. Exact duplicate listings of the same specific facility name at the same normalized
city, state, ZIP, and country are merged once while preserving unnamed or generic placeholder
entries (`"Investigational Site"`) that multi-site sponsors repeat across distinct clinics in one
city. `classify_footprint` records `location_source` (`"contacts_locations_module"`,
`"locations"`, `"study_sites_projection"`, `"sequence"`, or `"none"`) and sets
`weekly_projection_omits_countryless_sites = True` when reading from `study_sites` alone (because
`src/utils.py` skips facilities without a `country` string and omits `geoPoint` coordinates in
`demographics.json.gz`; passing raw API v2 `contactsLocationsModule.locations` or a registry
location archive preserves both).

## Site-allocation convex-hull bounds and the unresolved-site guard

Consider a trial with $M \ge 1$ US facilities, of which $R \le M$ resolve geographically at level
$L$ to areas with available ACS expected shares $e_1, \dots, e_R \in [0, 100]$:

- **Resolved bounds**: `resolved_min_pct = min(e_i)` and `resolved_max_pct = max(e_i)`, with
  deterministic `min_geoid` and `max_geoid` provenance.
- **Complete resolution (`R == M`, `bounds_unconditional = True`)**: For every non-negative
  participant allocation $(n_1, \dots, n_M)$ with $\sum_{i=1}^M n_i = N > 0$ and weights
  $w_i = n_i / N$, the enrollment-weighted expected share $\sum_{i=1}^M w_i e_i$ lies inside
  `[resolved_min_pct, resolved_max_pct]`. Hence `unconditional_min_pct = resolved_min_pct` and
  `unconditional_max_pct = resolved_max_pct`.
- **Incomplete resolution or missing ACS estimate (`0 < R < M`, `bounds_unconditional = False`)**:
  An unresolved US site (for example, a rural clinic outside every CBSA, a facility missing its
  ZIP code, a US territory outside the 50-state+DC frame, or a medical-campus ZCTA with zero
  estimated eligible residents) could enrol anywhere from $0\%$ to $100\%$ of participants. Thus
  `unconditional_min_pct = 0.0` and `unconditional_max_pct = 100.0`, `n_unresolved_or_unavailable = M - R`
  is recorded alongside `resolved_min_pct` / `resolved_max_pct`, and **neither
  `site_only_settled` / `site_weight_settled` nor `joint_direction_settled` can ever be `True`**.

## Proxy views and ACS 90% MOE covariance deduplication

Two clearly labelled proxy reference distributions are reported beside the bounds and never
replace them:

1. **`equal_facility_proxy`**: Assigns weight $1 / R$ to each resolved US facility with valid ACS
   data. Because multiple clinics inside the same `geoid` $g \in \mathcal{G}$ (for example, four
   clinics in Houston CBSA `26420` or ZCTA `77030`) read the **exact same** ACS survey estimate
   (correlation $= 1$), facility weights are aggregated by unique `geoid` first:
   $$W_g = \frac{c_g}{R}, \qquad \sum_{g \in \mathcal{G}} W_g = 1.$$
   The proxy share and 90% margin of error are then
   $$\hat{p}_{\text{eq}} = \sum_{g \in \mathcal{G}} W_g \hat{p}_g, \qquad \text{MOE}_{90}(\hat{p}_{\text{eq}}) = \sqrt{\sum_{g \in \mathcal{G}} W_g^2 \, \text{MOE}_{90}(\hat{p}_g)^2},$$
   propagating `None` if any contributing area has `moe90_pp is None`. When all $R$ clinics fall
   in a single `geoid` ($W_g = 1$), the MOE equals that single area's exact ACS MOE and is never
   artificially shrunk by $1 / \sqrt{R}$.
2. **`distinct_area_resident_proxy`**: Pools raw ACS age/sex-weighted numerator and denominator
   resident counts across unique non-overlapping `geoid`s $g \in \mathcal{G}$ once per area via
   `census.benchmark.band_weights`, `_sum`, and `_share`, propagating 90% margins of error
   through the Census Bureau's root-sum-of-squares and proportion/ratio formulas. At the `us`
   level ($|\mathcal{G}| = 1$), it collapses to the national estimate.

On any `single_us_facility` trial ($M = R = 1$), `resolved_min_pct`, `resolved_max_pct`,
`equal_facility_proxy['pct']`, and `distinct_area_resident_proxy['pct']` match
`census.benchmark.expected(...)['pct']` identically.

## Joint unknown-by-site settlement and Denominator B guards

For Denominator A (everyone counted in the baseline table), observed share `obs_floor_pct` and
non-classified share (`unknown_pct`, covering both unknown and any unmapped/unharmonised rows)
define `obs_ceiling_pct = min(100.0, obs_floor_pct + unknown_pct)`, evaluated on exact unrounded
proportions before 3-decimal display rounding so sub-0.001 pp rounding never fabricates a
settled direction:

- `site_only_settled` (`site_weight_settled`):
  `bounds_unconditional and (obs_floor_pct < unconditional_min_pct or obs_floor_pct > unconditional_max_pct)`
- `joint_diff_pp_min = round(obs_floor_pct - unconditional_max_pct, 3)`
- `joint_diff_pp_max = round(obs_ceiling_pct - unconditional_min_pct, 3)`
- `joint_direction_settled`:
  `bounds_unconditional and (obs_ceiling_pct < unconditional_min_pct or obs_floor_pct > unconditional_max_pct)`

When `joint_direction_settled` is `True`, the sign of the difference between the trial and its
US site areas holds simultaneously under **every** assignment of unknown/unmapped participants
and **every** non-negative allocation of participants across the trial's US facilities. Any
resolved area with a suppressed ACS sub-table (`NaN` estimate) is recorded as
`estimate_not_published` and sets `bounds_unconditional = False`.

Denominator B (`of_classified`) is reported alongside Denominator A where valid, preserving the
single-ethnicity-row guard from `census.trials.category_structure`: if an ethnicity table reports
only one category alongside unknowns, `of_classified` is `None` with
`classified_share_reason = "table_reports_one_ethnicity_category_only"`.

## Running tests

```bash
python -m pytest tests/catchments -q
```
