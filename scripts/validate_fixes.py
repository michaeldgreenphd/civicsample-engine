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
  7. NCT99000103 — Race rows in a "Sex/Gender, Customized" table beside a sex
     table: race only, never also sex unknown (fictional study)
"""
import json
import sys
import os
from typing import Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.race_extractor import extract_race_data
from src.ethnicity_extractor import extract_ethnicity_data
from src.sex_extractor import extract_sex_data


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


# ---------- NCT99000103 (fictional): race rows under a sex/gender title ----------
# 80 participants. The "Sex/Gender, Customized" table posts race, one row per
# race; the "Sex: Female, Male" table posts sex for the same people.
# Key tests:
#   - race reads the sex-titled table (flag race_from_sex_titled_table):
#     White 64, Black 12, Asian 4
#   - sex counts the sex table only: Female 35, Male 45, Unknown 0; the race
#     rows are in sex.quarantined_labels (reason race_label_in_sex_table), not
#     sex unknown 80 as before
MOCK_NCT99000103 = _make_study(
    "NCT99000103",
    "NCT99000103 - Race rows in a Sex/Gender table (fictional mock)",
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


def validate_study(study: dict, nct_id: str) -> Tuple[dict, dict]:
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
    print(f"    flags: {race['flags']}")
    if race.get("raw_categories"):
        print(f"    raw_categories ({len(race['raw_categories'])}):")
        for rc in race["raw_categories"]:
            print(f"      - \"{rc['original']}\" -> {rc['omb_category']} "
                  f"(confidence={rc['confidence']}, count={rc.get('count', 0)}, flags={rc['flags']})")
    print(f"    joint_sex_counts: "
          f"{json.dumps(race['joint_sex_counts']['counts']) if race.get('joint_sex_counts') else None}")

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
    return race, sex


def check_race_rows_in_sex_table(race: dict, sex: dict) -> None:
    """NCT99000103: rows race reads from a sex/gender-titled table are never
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
    print("\n  NCT99000103: race rows counted once, as race (sex unknown 0)")


def main() -> None:
    studies = [
        (MOCK_NCT02766335, "NCT02766335"),
        (MOCK_NCT01221441, "NCT01221441"),
        (MOCK_NCT02498067, "NCT02498067"),
        (MOCK_NCT05109104, "NCT05109104"),
        (MOCK_NCT99000101, "NCT99000101"),
        (MOCK_NCT99000102, "NCT99000102"),
        (MOCK_NCT99000103, "NCT99000103"),
    ]
    for study, nct_id in studies:
        race, sex = validate_study(study, nct_id)
        if nct_id == "NCT99000103":
            check_race_rows_in_sex_table(race, sex)

    print(f"\n{'='*70}")
    print("  Validation complete.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
