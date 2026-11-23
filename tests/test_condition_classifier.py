"""Keyword matching in src/condition_classifier.py.

Short ontology keywords (4 characters or fewer, mostly acronyms) match only as
whole words; longer keywords may sit inside a compound term. The cases below
are condition strings from the published records that the earlier
substring-only match placed in an unrelated category.
"""
import pytest

from src import condition_classifier as cc


@pytest.mark.parametrize("condition, wrong", [
    ("Falls", ("Oncology", "Hematologic Malignancy")),                       # "all"
    ("Trauma", ("Autoimmune and Inflammatory", "Rheumatoid Arthritis")),     # "ra"
    ("Fecal Transplant", ("Autoimmune and Inflammatory", "Rheumatoid Arthritis")),
    ("Behavioral Symptoms", ("Neurology", "Multiple Sclerosis")),            # "ms"
    ("Sedation", ("Reproductive and Sexual Health", "Erectile Dysfunction")),  # "ed"
    ("Aging", ("Gastrointestinal", "Other Gastrointestinal")),              # "gi"
    ("Astigmatism", ("Infectious Disease", "Sexually Transmitted Infections")),  # "sti"
    ("Pelvic Organ Prolapse", ("Hematology", "Thrombosis")),               # "pe"
    ("Caregiver Burnout", ("Dermatology", "Wound and Ulcer")),               # "burn"
    ("Tardive Dyskinesia", ("Dermatology", "Other Dermatology")),            # "skin"
])
def test_short_keyword_inside_a_word_no_longer_matches(condition, wrong):
    primary, secondary = cc.classify_condition(condition)
    assert (primary, secondary)[1] != wrong[1], (condition, primary, secondary)


@pytest.mark.parametrize("condition, secondary", [
    ("Rheumatoid Arthritis", "Rheumatoid Arthritis"),
    ("RA", "Rheumatoid Arthritis"),
    ("Multiple Sclerosis", "Multiple Sclerosis"),
    ("HIV Infections", "HIV/AIDS"),
    ("AIDS", "HIV/AIDS"),
    ("COPD", "COPD"),
    ("Gliosarcoma", "Sarcoma"),               # long keyword inside a compound term
    ("Bronchopulmonary Dysplasia", "Other Respiratory"),
    ("Heart Failure", "Heart Failure"),       # longest-first still wins over "heart"
])
def test_whole_word_and_compound_matches_are_kept(condition, secondary):
    assert cc.classify_condition(condition)[1] == secondary, cc.classify_condition(condition)


def test_short_keyword_rule_covers_exactly_the_short_keywords():
    cc._ensure_loaded()
    short = {kw for kw in cc._all_keywords if len(kw) <= cc.SHORT_KEYWORD_MAX_LEN}
    assert set(cc._short_patterns) == short
    assert {"ra", "ms", "all", "skin", "burn"} <= short
    assert "sarcoma" not in short


def test_plural_and_punctuation_boundaries():
    cc._ensure_loaded()
    assert cc._keyword_matches("ms", "ms")
    assert cc._keyword_matches("aids", "hiv/aids")
    assert cc._keyword_matches("ra", "early ra patients")
    assert not cc._keyword_matches("ra", "trauma")
    assert not cc._keyword_matches("all", "falls")
    assert not cc._keyword_matches("skin", "dyskinesia")
