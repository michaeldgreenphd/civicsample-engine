"""Census benchmarks for single-site US trials (manual build; see census/README.md).

Compares each trial's reported participant demographics with a *selected reference population*:
residents of the area around its one listed site who meet the trial's age and sex eligibility,
from the American Community Survey. The reference population is a proxy for a potential
recruitment population. It is not the trial's catchment area, the disease population, or an
enrolment target.
"""
SCHEMA_VERSION = 1
METHOD_VERSION = 'census-benchmarks-1'
