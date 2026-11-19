"""
Offline validation script for demographic extraction fixes.
Uses mock API data that mirrors the ClinicalTrials.gov v2 response
structure for the target studies.

Tests:
  1. NCT02766335 — "Unknown race" alias mapping + denominator balancing
  2. NCT01221441 — Row-level routing + alias mapping on combined table
  3. NCT02498067 — Quarantine of non-race labels
  4. NCT05109104 — All-zero count rejection
  5. NCT99000101 — Race-by-sex table beside a sex table (fictional study)
  6. NCT99000102 — Race table plus an ethnicity table titled "Race/Ethnicity,
     Customized" (fictional study)
  7. NCT99000103 — A contraception table as the only table whose title
     contains the letters "race" (fictional study)
  8. NCT99000104 — An Unknown-only race table plus a contraception table
     with an "Other" row (fictional study)
  9. NCT99000105 — Race rows under the camelCase title "RaceEthnicityOther"
     (fictional study)
 10. NCT99000106 — Race rows in a "Sex/Gender, Customized" table beside a sex
     table: race only, never also sex unknown (fictional study)
 11. NCT99000107 — Ethnicity table plus a race table titled
     "Race/Ethnicity, Customized" whose "Unknown" row maps to ethnicity
     (fictional study)
 12. NCT99000108 — The same people in a "Sex/Gender, Customized" and a
     "Sex: Female, Male" table (fictional study)
 13. NCT99000109 — A combined race and ethnicity classification, one row per
     participant: Caucasian / African-American / Hispanic / Unknown
     (fictional study, the table shape of NCT00416078)
 14. NCT99000110 — Race crossed with ethnicity in joined labels:
     "Non-Hispanic White", "Hispanic White", ... (fictional study, the table
     shape of NCT03709602)
 15. NCT99000111 — A race and an ethnicity classification of the same
     participants in one "Race/Ethnicity, Customized" table, every row
     prefixed "Race: " or "Ethnicity: " (fictional study, the table shape of
     NCT00707031)
 16. NCT99000112 — A race classification titled "Race/Ethnicity,
     Customized": race rows and the race classification's unknown row, no
     ethnicity row (fictional study, the table shape of NCT02787551)
 17. NCT99000113 — A combined race and ethnicity classification whose
     Hispanic row is posted as "Hispanic American" (fictional study, the
     table shape of NCT00090233)
 18. NCT99000114 — Race crossed with ethnicity as "White, not of Hispanic
     origin" / "White, of Hispanic origin" (fictional study, the table shape
     of NCT00756652)
 19. NCT99000130 — Age-band classes beside a "Total" class that repeats
     their rows, in a "Race/Ethnicity, Customized" and a "Sex: Female, Male"
     measure (fictional study, the labels and counts of NCT04911296)
 20. NCT99000115 — Rows named for the ethnicity dimension ("Ethnicity-Not
     Japanese") beside race rows that hold everyone (fictional study, the
     table shape of NCT04783935)
 21. NCT99000116 — A race classification whose every row is named for the
     ethnicity dimension ("Ethnicity : Caucasian") (fictional study, the
     table shape of NCT03476187)

Studies 7-9 also assert their expected race block and raceBreakdown
(EXPECTED_RACE); study 10 asserts its race and sex blocks
(check_race_rows_in_sex_table), study 11 its ethnicity totals and flags
(EXPECTED_ETHNICITY), and study 12 its sex totals and flags (EXPECTED_SEX).
Studies 1, 2 and 13 assert the ethnicity rows the race block records without
counting them (EXPECTED_SKIPPED_ETHNICITY); study 13 also asserts that its
race rows are counted as not Hispanic (EXPECTED_ETHNICITY). Study 14 asserts that a label
naming a race beside an ethnicity term is counted in that race
(check_joined_labels), and its ethnicity totals and flags, read from the same
joined labels (EXPECTED_ETHNICITY). Studies 15 to 18 assert their ethnicity
totals and flags (EXPECTED_ETHNICITY); study 15 also asserts its race block,
whose rows are prefixed "Race: " (EXPECTED_SKIPPED_ETHNICITY). Study 19
asserts its ethnicity totals and flags (EXPECTED_ETHNICITY) and its sex totals
and flags (EXPECTED_SEX): the Total class is counted once, the age bands not
again. Study 20 asserts that its rows named for the ethnicity dimension are
recorded and not counted as race (EXPECTED_SKIPPED_ETHNICITY,
check_ethnicity_dimension_rows); study 21 asserts its race block and
raceBreakdown, read as race (EXPECTED_RACE).
A mismatch stops the harness.
"""
import json
import sys
import os
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.race_extractor import extract_race_data
from src.ethnicity_extractor import extract_ethnicity_data
from src.sex_extractor import extract_sex_data
from src.utils import extract_demographic_breakdown, get_baseline_measures, get_overall_group_id


def _make_measurement(value, group_id="BG000"):
    return {"groupId": group_id, "value": str(value)}

def _make_category(title, value, group_id="BG000"):
    return {
        "title": title,
        "measurements": [_make_measurement(value, group_id)]
    }

def _make_class(title, categories):
    return {"title": title, "categories": categories}

def _make_measure(title, classes):
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "classes": classes}

def _make_study(nct_id, title, measures, total_participants, groups=None):
    """Build a mock study dict matching ClinicalTrials.gov API v2 structure.

    *total_participants* populates both the baseline ``denoms`` array and the
    protocol enrollment count, so that ``get_total_baseline_participants``
    can find the authoritative denominator.
    """
    if groups is None:
        groups = [{"groupId": "BG000", "title": "All Participants"}]
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": title},
            "statusModule": {"overallStatus": "COMPLETED",
                             "startDateStruct": {"date": "2015-01-01"},
                             "completionDateStruct": {"date": "2020-01-01"},
                             "lastUpdatePostDateStruct": {"date": "2020-06-01"},
                             "resultsFirstPostDateStruct": {"date": "2020-03-01"}},
            "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE3"],
                             "enrollmentInfo": {"count": total_participants, "type": "ACTUAL"}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Test", "class": "OTHER"}},
            "conditionsModule": {"conditions": ["Test Condition"]},
            "eligibilityModule": {},
            "contactsLocationsModule": {"locations": []},
            "outcomesModule": {}
        },
        "resultsSection": {
            "baselineCharacteristicsModule": {
                "groups": groups,
                "denoms": [
                    {
                        "units": "Participants",
                        "counts": [{"groupId": "BG000", "value": str(total_participants)}]
                    }
                ],
                "measures": measures
            }
        }
    }


# ---------- NCT02766335: "Unknown race" alias + denominator balancing ----------
# 74 participants analyzed.  Combined Race/Ethnicity table.
# Race rows: Caucasian/White (40), Black (25), Asian (7), Unknown race (2) = 74
# Ethnicity rows: Hispanic (2), Not Hispanic or Latino (72) = 74
# Key tests:
#   - "Unknown race" (2) → unknown_not_reported (not "other")
#   - Ethnicity: Hispanic (2) + Not Hispanic (72) = 74 (balanced)
MOCK_NCT02766335 = _make_study(
    "NCT02766335",
    "NCT02766335 - Combined Race/Ethnicity Study (mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Caucasian/White", 40),
                _make_category("Black or African American", 25),
                _make_category("Asian", 7),
                _make_category("Unknown race", 2),
                _make_category("Hispanic", 2),
                _make_category("Not Hispanic or Latino", 72),
            ])
        ]),
    ],
    total_participants=74,
)


# ---------- NCT01221441: Combined Race/Ethnicity table ----------
# 102 participants analyzed.
MOCK_NCT01221441 = _make_study(
    "NCT01221441",
    "NCT01221441 - Combined Race/Ethnicity Study (mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Caucasian/White", 82),
                _make_category("Black", 16),
                _make_category("Asian", 2),
                _make_category("Unknown race", 2),
                _make_category("Hispanic", 4),
                _make_category("Not Hispanic or Latino", 98),
            ])
        ]),
    ],
    total_participants=102,
)


# ---------- NCT02498067: Non-race labels in a "Race" table ----------
MOCK_NCT02498067 = _make_study(
    "NCT02498067",
    "NCT02498067 - Contraceptive Methods Study (mock)",
    [
        _make_measure("Race (NIH/OMB)", [
            _make_class("", [
                _make_category("White", 30),
                _make_category("Black or African American", 15),
                _make_category("Asian", 10),
                _make_category("Condom", 8),
                _make_category("IUD", 5),
                _make_category("Withdrawal", 3),
                _make_category("Oral Contraceptive Pills", 4),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 40),
                _make_category("Male", 35),
            ])
        ]),
    ],
    total_participants=75,
)


# ---------- NCT05109104: All-zero counts ----------
MOCK_NCT05109104 = _make_study(
    "NCT05109104",
    "NCT05109104 - All-Zero Study (mock)",
    [
        _make_measure("Race (NIH/OMB)", [
            _make_class("", [
                _make_category("White", 0),
                _make_category("Black or African American", 0),
                _make_category("Asian", 0),
                _make_category("Unknown or Not Reported", 0),
            ])
        ]),
        _make_measure("Ethnicity (NIH/OMB)", [
            _make_class("", [
                _make_category("Hispanic or Latino", 0),
                _make_category("Not Hispanic or Latino", 0),
                _make_category("Unknown or Not Reported", 0),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 0),
                _make_category("Male", 0),
            ])
        ]),
    ],
    total_participants=50,
)


# ---------- NCT99000101 (fictional): race crossed with sex in one table ----------
# 40 participants. The race table posts one row per (race, sex) pair, in the
# three layouts seen in the registry ("X : Female", "X (Male)", "X- Female").
# Key tests:
#   - each row's race part maps as a plain race row would: White 22, Black 12,
#     Asian 6, nothing in "other"
#   - race.joint_sex_counts keeps the posted pairs; the sex table is untouched
MOCK_NCT99000101 = _make_study(
    "NCT99000101",
    "NCT99000101 - Race-by-sex table (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("White : Female", 10),
                _make_category("White (Male)", 12),
                _make_category("Black or African American- Female", 5),
                _make_category("Black or African American- Male", 7),
                _make_category("Asian : Female", 2),
                _make_category("Asian : Male", 4),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 17),
                _make_category("Male", 23),
            ])
        ]),
    ],
    total_participants=40,
)


# ---------- NCT99000102 (fictional): an ethnicity table posted under a race title ----------
# 60 participants. The race table counts everyone; the second "Race/Ethnicity,
# Customized" table is an ethnicity table whose "Unknown" row maps to race.
# Key tests:
#   - the race table alone is counted: White 41, Black 12, Asian 4, Unknown 3
#     = 60, not 60 + 5 ethnicity unknowns
#   - flags race_table_superseded and multiple_race_tables_2
MOCK_NCT99000102 = _make_study(
    "NCT99000102",
    "NCT99000102 - Race plus ethnicity table (fictional mock)",
    [
        _make_measure("Race (NIH/OMB)", [
            _make_class("", [
                _make_category("White", 41),
                _make_category("Black or African American", 12),
                _make_category("Asian", 4),
                _make_category("Unknown or Not Reported", 3),
            ])
        ]),
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Hispanic or Latino", 9),
                _make_category("Not Hispanic or Latino", 46),
                _make_category("Unknown", 5),
            ])
        ]),
    ],
    total_participants=60,
)


# ---------- NCT99000103 (fictional): a contraception table, no race table ----------
# 102 participants. "Contraception" contains the letters "race"; the old
# substring title test made this the trial's race table, "Other method" (7)
# became race "other" and balancing added 95 unknown.
# Key tests:
#   - race not reported, no flags, raceBreakdown None
MOCK_NCT99000103 = _make_study(
    "NCT99000103",
    "NCT99000103 - Contraception table only (fictional mock)",
    [
        _make_measure("Contraception use at time of enrollment", [
            _make_class("", [
                _make_category("None", 33),
                _make_category("Condoms", 62),
                _make_category("Other method", 7),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 102),
                _make_category("Male", 0),
            ])
        ]),
    ],
    total_participants=102,
)


# ---------- NCT99000104 (fictional): Unknown-only race table + contraception table ----------
# 50 participants. Neither table has a race-specific count; under the old
# title test the fuller one by counted sum won, and the contraception
# table's "Other" 30 became race "other".
# Key tests:
#   - race is the race table's Unknown 20 plus 30 balanced: unknown 50, other 0
#   - no multiple_race_tables_2 / race_table_superseded flags
MOCK_NCT99000104 = _make_study(
    "NCT99000104",
    "NCT99000104 - Unknown-only race table plus contraception table (fictional mock)",
    [
        _make_measure("Race (NIH/OMB)", [
            _make_class("", [
                _make_category("Unknown or Not Reported", 20),
            ])
        ]),
        _make_measure("Contraception History", [
            _make_class("", [
                _make_category("Pill", 12),
                _make_category("Other", 30),
                _make_category("None", 8),
            ])
        ]),
    ],
    total_participants=50,
)


# ---------- NCT99000105 (fictional): race rows under a camelCase title ----------
# 40 participants. The only race rows are under "RaceEthnicityOther"; the
# "Race/Ethnicity, Customized" table holds ethnicity rows only. A \b regex
# title rule finds no word break in the camelCase title and would drop it.
# Key tests:
#   - White 30, Black 8, Other 2 from the camelCase table
MOCK_NCT99000105 = _make_study(
    "NCT99000105",
    "NCT99000105 - camelCase race title (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Hispanic or Latino", 4),
                _make_category("Not Hispanic or Latino", 36),
            ])
        ]),
        _make_measure("RaceEthnicityOther", [
            _make_class("", [
                _make_category("White", 30),
                _make_category("Black/African American", 8),
                _make_category("Other", 2),
            ])
        ]),
    ],
    total_participants=40,
)


# ---------- NCT99000106 (fictional): race rows under a sex/gender title ----------
# 80 participants. The "Sex/Gender, Customized" table posts race, one row per
# race; the "Sex: Female, Male" table posts sex for the same people.
# Key tests:
#   - race reads the sex-titled table (flag race_from_sex_titled_table):
#     White 64, Black 12, Asian 4
#   - sex counts the sex table only: Female 35, Male 45, Unknown 0; the race
#     rows are in sex.quarantined_labels (reason race_label_in_sex_table), not
#     sex unknown 80 as before
MOCK_NCT99000106 = _make_study(
    "NCT99000106",
    "NCT99000106 - Race rows in a Sex/Gender table (fictional mock)",
    [
        _make_measure("Sex/Gender, Customized", [
            _make_class("", [
                _make_category("White", 64),
                _make_category("Black/African Heritage", 12),
                _make_category("East Asian/South East Asian", 4),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 35),
                _make_category("Male", 45),
            ])
        ]),
    ],
    total_participants=80,
)


# ---------- NCT99000107 (fictional): a race table posted under an ethnicity title ----------
# 80 participants. The ethnicity table counts everyone; the "Race/Ethnicity,
# Customized" table is a race table (race rows and an "Unknown" row, no
# Hispanic or Not Hispanic row), skipped for ethnicity as race_only_table_skipped:
# counted once, 80, not 86.
MOCK_NCT99000107 = _make_study(
    "NCT99000107",
    "NCT99000107 - Ethnicity plus race table (fictional mock)",
    [
        _make_measure("Ethnicity (NIH/OMB)", [
            _make_class("", [
                _make_category("Hispanic or Latino", 9),
                _make_category("Not Hispanic or Latino", 68),
                _make_category("Unknown or Not Reported", 3),
            ])
        ]),
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("White", 55),
                _make_category("Black or African American", 19),
                _make_category("Unknown", 6),
            ])
        ]),
    ],
    total_participants=80,
)

# ---------- NCT99000108 (fictional): one population in two sex tables ----------
# 60 participants posted in a "Sex/Gender, Customized" table and again in the
# "Sex: Female, Male" table: counted once, 60, not 120.
MOCK_NCT99000108 = _make_study(
    "NCT99000108",
    "NCT99000108 - Two sex tables (fictional mock)",
    [
        _make_measure("Sex/Gender, Customized", [
            _make_class("", [
                _make_category("Female", 34),
                _make_category("Male", 26),
            ])
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class("", [
                _make_category("Female", 34),
                _make_category("Male", 26),
            ])
        ]),
    ],
    total_participants=60,
)

# ---------- NCT99000109 (fictional): combined race and ethnicity classification ----------
# 50 participants, each in exactly one row. The race rows hold 44; the 6 in
# the Hispanic row are not counted as race; they are the whole shortfall, so
# they are filed as unknown_ethnicity_only, not unknown_inferred.
# The race block records that row, so the gap can be told from race the table
# did not report.
MOCK_NCT99000109 = _make_study(
    "NCT99000109",
    "NCT99000109 - Combined race and ethnicity classification (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Caucasian", 30),
                _make_category("African-American", 12),
                _make_category("Hispanic", 6),
                _make_category("Unknown", 2),
            ])
        ]),
    ],
    total_participants=50,
)

# ---------- NCT99000110 (fictional): race named beside ethnicity in one label ----------
# 300 participants, each in exactly one row. "Non-Hispanic White", "Hispanic
# White" and "Non Hispanic Black or African American" name a race and are
# counted in it; "Hispanic Other" is race Other; "Hispanic (regardless of
# race)" names none and stays an ethnicity row, recorded and not counted.
MOCK_NCT99000110 = _make_study(
    "NCT99000110",
    "NCT99000110 - Race named beside ethnicity in one label (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class("", [
                _make_category("Asian", 12),
                _make_category("Non Hispanic Black or African American", 98),
                _make_category("Non-Hispanic White", 145),
                _make_category("Hispanic White", 5),
                _make_category("Hispanic Other", 14),
                _make_category("Hispanic (regardless of race)", 8),
                _make_category("Unknown or Not Reported", 18),
            ])
        ]),
    ],
    total_participants=300,
)

# ---------- NCT99000111 (fictional): two prefixed classifications in one table ----------
# 634 participants, classified twice: by race in the "Race: " rows and by
# ethnicity in the "Ethnicity: " rows, as the registry returns labels on the
# classes. The prefixed ethnicity rows are read; before, they matched nothing,
# were dropped with the race rows, and the block ended in all-zero rejection.
MOCK_NCT99000111 = _make_study(
    "NCT99000111",
    "NCT99000111 - Prefixed race and ethnicity classifications (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("Race: Caucasian/White", 588), ("Race: Black", 18), ("Race: Asian/Oriental", 7),
                             ("Race: Other", 21), ("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)]
        ]),
    ],
    total_participants=634,
)

# ---------- NCT99000112 (fictional): a race table under a race-and-ethnicity title ----------
# 514 participants in race rows plus the race classification's unknown row,
# no ethnicity row. Not an ethnicity report: before, the unknown row was
# counted as ethnicity unknown and 512 more were balanced into it.
MOCK_NCT99000112 = _make_study(
    "NCT99000112",
    "NCT99000112 - Race table titled Race/Ethnicity (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("American Indian or Alaska Native", 0), ("Asian/Oriental", 7), ("Black", 19),
                             ("Native Hawaiian or Other Pacific Islander", 1), ("White", 485),
                             ("Unknown or Not Reported'", 2)]
        ]),
    ],
    total_participants=514,
)

# ---------- NCT99000113 (fictional): "Hispanic American" in a combined classification ----------
# 50 participants, one row each: White 30 / Hispanic American 8 / Black 12.
# "Hispanic American" is now in the ethnicity vocabulary (block comment
# "Plain vocabulary gaps"), so both extractors count 8 Hispanic and the race
# rows are counted as not Hispanic. Before, the ethnicity block ended in
# all-zero rejection, flagged ethnicity_rows_unread.
MOCK_NCT99000113 = _make_study(
    "NCT99000113",
    "NCT99000113 - Hispanic American in a combined classification (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("White", 30), ("Hispanic American", 8), ("Black", 12)]
        ]),
    ],
    total_participants=50,
)

# ---------- NCT99000114 (fictional): "of Hispanic origin" and "not of Hispanic origin" ----------
# 75 participants. Both wordings are in the vocabulary, read through the
# comma split: Not Hispanic 50 + 10, Hispanic 8, and the Asian and Missing
# rows balanced into unknown. Before, nothing was read and all 75 were
# balanced into unknown; with "not of Hispanic origin" alone the block
# would read 60 Not Hispanic and no Hispanic participant.
MOCK_NCT99000114 = _make_study(
    "NCT99000114",
    "NCT99000114 - Of / not of Hispanic origin (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("White, not of Hispanic origin", 50), ("White, of Hispanic origin", 8),
                             ("Black, not of Hispanic origin", 10), ("Asian", 5), ("Missing", 2)]
        ]),
    ],
    total_participants=75,
)

# ---------- NCT99000130 (fictional): subgroup classes beside a Total class ----------
# 150 participants; every measure posts three age bands and a Total class with
# the same rows. Before, every class was summed: Hispanic 168 of 150, female
# 170. The Total class is now read alone
# (table_selection, "A Total class beside subgroup classes").
_NCT99000130_RACE_LABELS = ["Black - Non-Hispanic", "American Indian/Alaskan Native", "Hispanic",
                            "Asian/Pacific Islander", "White - Non-Hispanic"]
MOCK_NCT99000130 = _make_study(
    "NCT99000130",
    "NCT99000130 - Age-band classes beside a Total class (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(band, [_make_category(label, v) for label, v in zip(_NCT99000130_RACE_LABELS, values)])
            for band, values in [("21 - 50 years", [6, 1, 32, 0, 11]), ("51 - 65 years", [5, 1, 28, 0, 16]),
                                 ("66 - 75 years", [7, 0, 24, 2, 17]), ("Total", [18, 2, 84, 2, 44])]
        ]),
        _make_measure("Sex: Female, Male", [
            _make_class(band, [_make_category("Female", f), _make_category("Male", m)])
            for band, (f, m) in [("21 - 50 years", (29, 21)), ("51 - 65 years", (28, 22)),
                                 ("66 - 75 years", (28, 22)), ("Total", (85, 65))]
        ]),
    ],
    total_participants=150,
)

# Expected skipped_ethnicity_rows (label, count), the race block's non-zero
# OMB totals and its inferred unknown. Studies 1 and 2 list ethnicity beside
# race rows that cover everyone (nothing inferred); study 13 is one exclusive
# classification (the Hispanic row's 6 are the inferred unknown). The rows
# are recorded either way and enter no total.
# ---------- NCT99000115 (fictional): ethnicity-dimension rows beside race rows ----------
# 219 participants, classified by race in the "Race-" rows (all 219) and by
# Japanese ethnicity in the "Ethnicity-" rows. Before, the 219 "Ethnicity-"
# participants were counted again as race "other": a block of 438 for 219.
MOCK_NCT99000115 = _make_study(
    "NCT99000115",
    "NCT99000115 - Ethnicity-dimension rows beside race rows (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("Ethnicity-Japanese", 0), ("Ethnicity-Not Japanese", 204),
                             ("Ethnicity-Unknown or Not Reported", 15), ("Race-Asian", 2),
                             ("Race-Black or African American", 1), ("Race-Other", 6),
                             ("Race-Unknown or Not Reported", 26), ("Race-White", 184)]
        ]),
    ],
    total_participants=219,
)

# ---------- NCT99000116 (fictional): a race classification named "Ethnicity" ----------
# 244 participants, one row each; the rows are races under the name
# "Ethnicity". Before, the 228 race-row participants were all race "other".
MOCK_NCT99000116 = _make_study(
    "NCT99000116",
    "NCT99000116 - Race classification named Ethnicity (fictional mock)",
    [
        _make_measure("Race/Ethnicity, Customized", [
            _make_class(label, [_make_category(None, v)])
            for label, v in [("Ethnicity : Caucasian", 154), ("Ethnicity : Black or African American", 62),
                             ("Ethnicity : Other", 5), ("Ethnicity : American Indian or Alaskan Native", 2),
                             ("Ethnicity : Asian", 1), ("Ethnicity : Hispanic or Latino", 16),
                             ("Ethnicity: Multiple Ethnicities", 4)]
        ]),
    ],
    total_participants=244,
)

EXPECTED_SKIPPED_ETHNICITY = {
    "NCT02766335": {"rows": [("Hispanic", 2), ("Not Hispanic or Latino", 72)],
                    "omb": {"white": 40, "black_african_american": 25, "asian": 7, "unknown_not_reported": 2},
                    "unknown_inferred": 0},
    "NCT01221441": {"rows": [("Hispanic", 4), ("Not Hispanic or Latino", 98)],
                    "omb": {"white": 82, "black_african_american": 16, "asian": 2, "unknown_not_reported": 2},
                    "unknown_inferred": 0},
    "NCT99000109": {"rows": [("Hispanic", 6)],
                    "omb": {"white": 30, "black_african_american": 12, "unknown_not_reported": 8},
                    "unknown_ethnicity_only": 6, "unknown_inferred": 0},
    # Rows named for the ethnicity dimension beside race rows that hold all
    # 219 (race_extractor, "Rows named for the ethnicity dimension").
    "NCT99000115": {"rows": [("Ethnicity-Japanese", 0), ("Ethnicity-Not Japanese", 204),
                             ("Ethnicity-Unknown or Not Reported", 15)],
                    "omb": {"white": 184, "asian": 2, "black_african_american": 1, "other": 6,
                            "unknown_not_reported": 26},
                    "unknown_inferred": 0},
    # Prefixed race rows map as their bare labels (race_extractor, "Race
    # dimension named in the row label"): before, Black 18, Asian 7 and
    # Other 21 were all counted as other.
    "NCT99000111": {"rows": [("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)],
                    "omb": {"white": 588, "black_african_american": 18, "asian": 7, "other": 21},
                    "unknown_inferred": 0},
}

# Expected sex results for the legacy table-selection study.
EXPECTED_SEX = {
    "NCT99000108": {"totals": {"female": 34, "male": 26, "unknown": 0},
                    "flags": ["from_combined_table", "sex_table_superseded"]},
    "NCT99000130": {"totals": {"female": 85, "male": 65, "unknown": 0},
                    "flags": ["total_class_only"]},
}

# Expected ethnicity results for the table-selection and combined-table studies.
EXPECTED_ETHNICITY = {
    "NCT99000107": {"omb": {"hispanic_latino": 9, "not_hispanic_latino": 68, "unknown_not_reported": 3},
                    "flags": ["race_only_table_skipped"]},
    # One exact combined classification: its 42 participants in race rows are
    # not Hispanic; its "Unknown" row stays unknown.
    "NCT99000109": {"omb": {"hispanic_latino": 6, "not_hispanic_latino": 42, "unknown_not_reported": 2},
                    "flags": ["not_hispanic_from_combined_table"]},
    # Joined labels read for their ethnicity: Hispanic White 5 + Hispanic
    # Other 14 + Hispanic (regardless of race) 8; Non-Hispanic 98 + 145;
    # Unknown 18 and the 12 Asian balanced into unknown.
    "NCT99000110": {"omb": {"hispanic_latino": 27, "not_hispanic_latino": 243, "unknown_not_reported": 30},
                    "flags": ["denominator_balanced", "dimension_wording_stripped", "race_joined_label"]},
    "NCT99000111": {"omb": {"hispanic_latino": 170, "not_hispanic_latino": 464, "unknown_not_reported": 0},
                    "flags": ["dimension_wording_stripped"]},
    "NCT99000112": {"omb": {"hispanic_latino": 0, "not_hispanic_latino": 0, "unknown_not_reported": 0},
                    "flags": ["race_only_table_skipped"]},
    "NCT99000113": {"omb": {"hispanic_latino": 8, "not_hispanic_latino": 42, "unknown_not_reported": 0},
                    "flags": ["not_hispanic_from_combined_table"]},
    "NCT99000114": {"omb": {"hispanic_latino": 8, "not_hispanic_latino": 60, "unknown_not_reported": 7},
                    "flags": ["denominator_balanced", "split_from_combined"]},
    # The Total class alone: Hispanic 84, Not Hispanic 18 + 44 from the
    # "- Non-Hispanic" rows, the 4 American Indian and Asian participants
    # balanced into unknown.
    "NCT99000130": {"omb": {"hispanic_latino": 84, "not_hispanic_latino": 62, "unknown_not_reported": 4},
                    "flags": ["denominator_balanced", "race_joined_label", "total_class_only"]},
}


# Expected race results for the title-rule studies: reported, the non-zero
# OMB totals, top-level flags, and the raceBreakdown counts (None = no table).
EXPECTED_RACE = {
    "NCT99000103": {"reported": False, "omb": {}, "flags": [], "breakdown": None},
    "NCT99000104": {"reported": True, "omb": {"unknown_not_reported": 50},
                    "flags": ["denominator_balanced"],
                    "breakdown": {"Unknown or Not Reported": 20}},
    "NCT99000105": {"reported": True,
                    "omb": {"white": 30, "black_african_american": 8, "other": 2},
                    "flags": ["combined_race_ethnicity", "multiple_race_tables_2",
                              "race_table_superseded", "slash_normalized"],
                    "breakdown": {"Hispanic or Latino": 4, "Not Hispanic or Latino": 36,
                                  "White": 30, "Black/African American": 8, "Other": 2}},
    # Every row named for the ethnicity dimension: read as race, the
    # Hispanic row still an ethnicity row (its 16 are the shortfall).
    "NCT99000116": {"reported": True,
                    "omb": {"white": 154, "black_african_american": 62, "american_indian_alaska_native": 2,
                            "asian": 1, "other": 9, "unknown_not_reported": 16},
                    "flags": ["combined_race_ethnicity", "customized_table", "denominator_balanced",
                              "ethnicity_dimension_as_race", "ethnicity_rows_skipped",
                              "shortfall_is_ethnicity_rows", "unmapped"],
                    "breakdown": {"Ethnicity : Caucasian": 154, "Ethnicity : Black or African American": 62,
                                  "Ethnicity : Other": 5, "Ethnicity : American Indian or Alaskan Native": 2,
                                  "Ethnicity : Asian": 1, "Ethnicity : Hispanic or Latino": 16,
                                  "Ethnicity: Multiple Ethnicities": 4}},
}


def check_expected(nct_id: str, race: dict, breakdown: Optional[dict]) -> None:
    """Assert a title-rule study's race block and raceBreakdown."""
    expected = EXPECTED_RACE[nct_id]
    nonzero = {k: v for k, v in race["omb_totals"].items() if v}
    counts = None if breakdown is None else {k: v["count"] for k, v in breakdown.items()}
    assert race["reported"] is expected["reported"], f"{nct_id} reported: {race['reported']}"
    assert nonzero == expected["omb"], f"{nct_id} omb_totals: {nonzero}"
    assert race["flags"] == expected["flags"], f"{nct_id} flags: {race['flags']}"
    assert counts == expected["breakdown"], f"{nct_id} raceBreakdown: {counts}"
    print(f"\n  {nct_id}: expected race block and raceBreakdown — OK")


def check_skipped_ethnicity(nct_id: str, race: dict) -> None:
    """Assert the ethnicity rows a race block records, and that they are in
    none of its totals."""
    expected = EXPECTED_SKIPPED_ETHNICITY[nct_id]
    rows = [(r["original"], r["count"]) for r in race["skipped_ethnicity_rows"]]
    nonzero = {k: v for k, v in race["omb_totals"].items() if v}
    assert rows == expected["rows"], f"{nct_id} skipped_ethnicity_rows: {rows}"
    assert "ethnicity_rows_skipped" in race["flags"], f"{nct_id} flags: {race['flags']}"
    assert nonzero == expected["omb"], f"{nct_id} omb_totals: {nonzero}"
    assert race["unknown_inferred"] == expected["unknown_inferred"], \
        f"{nct_id} unknown_inferred: {race['unknown_inferred']}"
    assert race["unknown_ethnicity_only"] == expected.get("unknown_ethnicity_only", 0), \
        f"{nct_id} unknown_ethnicity_only: {race['unknown_ethnicity_only']}"
    assert race["omb_totals"]["unknown_not_reported"] == (
        race["unknown_explicit"] + race["unknown_ethnicity_only"] + race["unknown_inferred"]), \
        f"{nct_id}: the parts of unknown do not add up"
    assert not any(c["original"] in dict(rows) for c in race["raw_categories"]), \
        f"{nct_id}: an ethnicity row is among the counted rows"
    print(f"\n  {nct_id}: ethnicity rows recorded, not counted — OK")


def check_joined_labels(race: dict) -> None:
    """NCT99000110: a label naming a race beside an ethnicity term is counted
    in that race, under the posted label; one that names no race is not."""
    nonzero = {k: v for k, v in race["omb_totals"].items() if v}
    assert nonzero == {"asian": 12, "black_african_american": 98, "white": 150, "other": 14,
                       "unknown_not_reported": 26}, nonzero
    assert (race["unknown_explicit"], race["unknown_ethnicity_only"], race["unknown_inferred"]) == (18, 8, 0), \
        (race["unknown_explicit"], race["unknown_ethnicity_only"], race["unknown_inferred"])
    assert [(r["original"], r["count"]) for r in race["skipped_ethnicity_rows"]] == [
        ("Hispanic (regardless of race)", 8)], race["skipped_ethnicity_rows"]
    joined = {c["original"]: (c["race_part"], c["omb_category"]) for c in race["raw_categories"] if "race_part" in c}
    assert joined == {
        "Non Hispanic Black or African American": ("Black or African American", "black_african_american"),
        "Non-Hispanic White": ("White", "white"),
        "Hispanic White": ("White", "white"),
        "Hispanic Other": ("Other", "other"),
    }, joined
    print("\n  NCT99000110: joined race and ethnicity labels counted in their race — OK")


def check_race_rows_in_sex_table(race: dict, sex: dict) -> None:
    """NCT99000106: rows race reads from a sex/gender-titled table are never
    also sex unknown."""
    assert race["omb_totals"]["white"] == 64 and race["omb_totals"]["black_african_american"] == 12 \
        and race["omb_totals"]["asian"] == 4, race["omb_totals"]
    assert "race_from_sex_titled_table" in race["flags"], race["flags"]
    assert sex["totals"] == {"female": 35, "male": 45, "unknown": 0}, sex["totals"]
    assert {q["original"]: q["reason"] for q in sex["quarantined_labels"]} == {
        "White": "race_label_in_sex_table",
        "Black/African Heritage": "race_label_in_sex_table",
        "East Asian/South East Asian": "race_label_in_sex_table",
    }, sex["quarantined_labels"]
    print("\n  NCT99000106: race rows counted once, as race (sex unknown 0) — OK")


def check_ethnicity_dimension_rows(race: dict) -> None:
    """NCT99000115: every row named for the ethnicity dimension is recorded
    with the ethnicity_dimension_row flag, and the race block adds up to the
    population, not to it twice."""
    assert all("ethnicity_dimension_row" in r["flags"] for r in race["skipped_ethnicity_rows"]), \
        race["skipped_ethnicity_rows"]
    assert sum(race["omb_totals"].values()) == 219, race["omb_totals"]
    print("\n  NCT99000115: ethnicity-dimension rows recorded, not counted as race — OK")


def validate_study(study: dict, nct_id: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {nct_id}")
    print(f"{'='*70}")

    race = extract_race_data(study)
    ethnicity = extract_ethnicity_data(study)
    sex = extract_sex_data(study)

    # Determinism guard: flags must serialize in sorted order so identical
    # extractions produce identical bytes across runs.
    for name, result in (("race", race), ("ethnicity", ethnicity), ("sex", sex)):
        assert result["flags"] == sorted(result["flags"]), \
            f"{nct_id} {name} flags not sorted: {result['flags']}"

    # Race
    print(f"\n  RACE:")
    print(f"    reported: {race['reported']}")
    print(f"    omb_totals: {json.dumps(race['omb_totals'], indent=6)}")
    race_total = sum(race['omb_totals'].values())
    print(f"    RACE TOTAL: {race_total}")
    if race.get("quarantined_labels"):
        print(f"    quarantined_labels:")
        for q in race["quarantined_labels"]:
            print(f"      - \"{q['original']}\" (count={q['count']}, reason={q['reason']})")
    else:
        print(f"    quarantined_labels: []")
    print(f"    skipped_ethnicity_rows: "
          f"{json.dumps([[r['original'], r['count']] for r in race['skipped_ethnicity_rows']])}")
    print(f"    flags: {race['flags']}")
    if race.get("raw_categories"):
        print(f"    raw_categories ({len(race['raw_categories'])}):")
        for rc in race["raw_categories"]:
            print(f"      - \"{rc['original']}\" -> {rc['omb_category']} "
                  f"(confidence={rc['confidence']}, count={rc.get('count', 0)}, flags={rc['flags']})")
    print(f"    joint_sex_counts: "
          f"{json.dumps(race['joint_sex_counts']['counts']) if race.get('joint_sex_counts') else None}")
    breakdown = extract_demographic_breakdown(get_baseline_measures(study), "race",
                                              get_overall_group_id(study))
    print(f"    raceBreakdown: "
          f"{json.dumps({k: v['count'] for k, v in breakdown.items()}) if breakdown else None}")

    # Ethnicity
    print(f"\n  ETHNICITY:")
    print(f"    reported: {ethnicity['reported']}")
    print(f"    omb_totals: {json.dumps(ethnicity['omb_totals'], indent=6)}")
    eth_total = sum(ethnicity['omb_totals'].values())
    print(f"    ETHNICITY TOTAL: {eth_total}")
    print(f"    flags: {ethnicity['flags']}")
    if ethnicity.get("raw_categories"):
        print(f"    raw_categories ({len(ethnicity['raw_categories'])}):")
        for rc in ethnicity["raw_categories"]:
            print(f"      - \"{rc['original']}\" -> {rc['omb_category']} "
                  f"(confidence={rc['confidence']}, count={rc.get('count', 0)}, flags={rc['flags']})")

    # Sex
    print(f"\n  SEX:")
    print(f"    reported: {sex['reported']}")
    print(f"    totals: {json.dumps(sex.get('totals', {}), indent=6)}")
    if sex.get("quarantined_labels"):
        print(f"    quarantined_labels:")
        for q in sex["quarantined_labels"]:
            print(f"      - \"{q['original']}\" (count={q['count']}, reason={q['reason']})")
    print(f"    flags: {sex['flags']}")

    if nct_id in EXPECTED_RACE:
        check_expected(nct_id, race, breakdown)
    if nct_id in EXPECTED_SKIPPED_ETHNICITY:
        check_skipped_ethnicity(nct_id, race)
    if nct_id in EXPECTED_SEX:
        expected = EXPECTED_SEX[nct_id]
        assert sex["totals"] == expected["totals"], f"{nct_id} sex: {sex['totals']}"
        assert sex["flags"] == expected["flags"], f"{nct_id} sex flags: {sex['flags']}"
        print(f"\n  {nct_id}: expected sex totals and flags — OK")
    if nct_id in EXPECTED_ETHNICITY:
        expected = EXPECTED_ETHNICITY[nct_id]
        assert ethnicity["omb_totals"] == expected["omb"], f"{nct_id} ethnicity: {ethnicity['omb_totals']}"
        assert ethnicity["flags"] == expected["flags"], f"{nct_id} ethnicity flags: {ethnicity['flags']}"
        print(f"\n  {nct_id}: expected ethnicity totals and flags — OK")
    if nct_id == "NCT99000106":
        check_race_rows_in_sex_table(race, sex)
    if nct_id == "NCT99000110":
        check_joined_labels(race)
    if nct_id == "NCT99000115":
        check_ethnicity_dimension_rows(race)


def main() -> None:
    studies = [
        (MOCK_NCT02766335, "NCT02766335"),
        (MOCK_NCT01221441, "NCT01221441"),
        (MOCK_NCT02498067, "NCT02498067"),
        (MOCK_NCT05109104, "NCT05109104"),
        (MOCK_NCT99000101, "NCT99000101"),
        (MOCK_NCT99000102, "NCT99000102"),
        (MOCK_NCT99000103, "NCT99000103"),
        (MOCK_NCT99000104, "NCT99000104"),
        (MOCK_NCT99000105, "NCT99000105"),
        (MOCK_NCT99000106, "NCT99000106"),
        (MOCK_NCT99000107, "NCT99000107"),
        (MOCK_NCT99000108, "NCT99000108"),
        (MOCK_NCT99000109, "NCT99000109"),
        (MOCK_NCT99000110, "NCT99000110"),
        (MOCK_NCT99000111, "NCT99000111"),
        (MOCK_NCT99000112, "NCT99000112"),
        (MOCK_NCT99000113, "NCT99000113"),
        (MOCK_NCT99000114, "NCT99000114"),
        (MOCK_NCT99000130, "NCT99000130"),
        (MOCK_NCT99000115, "NCT99000115"),
        (MOCK_NCT99000116, "NCT99000116"),
    ]
    for study, nct_id in studies:
        validate_study(study, nct_id)

    print(f"\n{'='*70}")
    print("  Validation complete.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
