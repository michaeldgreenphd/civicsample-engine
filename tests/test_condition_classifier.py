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


@pytest.mark.parametrize("condition, wrong", [
    ("Fall", "Hematologic Malignancy"),   # fuzzy "all" scored 85.7 against "fall"
])
def test_short_keyword_is_not_reached_through_the_fuzzy_step(condition, wrong):
    assert cc.classify_condition(condition)[1] != wrong, cc.classify_condition(condition)


def test_fuzzy_step_never_offers_a_short_keyword():
    cc._ensure_loaded()
    assert cc._fuzzy_keywords
    assert all(len(kw) > cc.SHORT_KEYWORD_MAX_LEN for kw in cc._fuzzy_keywords)
    assert set(cc._fuzzy_keywords) | set(cc._short_patterns) == set(cc._all_keywords)


@pytest.mark.parametrize("condition, secondary", [
    ("Schizophernia", "Schizophrenia and Psychotic Disorders"),
    ("Leukemai", "Hematologic Malignancy"),
])
def test_fuzzy_step_still_corrects_typos_in_long_keywords(condition, secondary):
    cc._ensure_loaded()
    norm = cc._normalize(condition)
    assert not any(cc._keyword_matches(kw, norm) for kw in cc._all_keywords)  # reaches Step B
    assert cc.classify_condition(condition)[1] == secondary


# Ontology 1.1.0 keywords. Each condition below is a string from the published
# records that the whole-word rule left in Other (or, for opioid-induced
# constipation, in Opioid Use Disorder through "opioid"); the new keyword
# places it in its category.
@pytest.mark.parametrize("condition, expected", [
    ("Epilepsies, Partial", ("Neurology", "Epilepsy and Seizure Disorders")),
    ("Myoclonus Epilepsies, Progressive", ("Neurology", "Epilepsy and Seizure Disorders")),
    ("Chronic Idiopathic Constipation", ("Gastrointestinal", "Other Gastrointestinal")),
    ("Opioid-Induced Constipation", ("Gastrointestinal", "Other Gastrointestinal")),
    ("Post Traumatic Stress Disorder", ("Mental Health", "PTSD and Trauma")),
    ("mCRPC", ("Oncology", "Prostate Cancer")),
    ("CMML", ("Oncology", "Hematologic Malignancy")),
    ("mCRC", ("Oncology", "Colorectal Cancer")),
])
def test_ontology_keywords_place_conditions_the_whole_word_rule_left_out(condition, expected):
    assert cc.classify_condition(condition) == expected


# Controls for keywords left out of 1.1.0, or worded to avoid a false match.
# "cuti" would match "Cutis" through the plural rule, so it is not a keyword;
# "post traumatic stress" without "disorder" reached "Post Traumatic Stiffness"
# through the fuzzy step (88.9 against the threshold of 85).
@pytest.mark.parametrize("condition, wrong_secondary", [
    ("Xerosis Cutis", "Bacterial Infections"),
    ("Calcinosis Cutis", "Bacterial Infections"),
    ("Post Traumatic Stiffness", "PTSD and Trauma"),
    ("mCRC", "Prostate Cancer"),
])
def test_new_keywords_do_not_reach_unrelated_conditions(condition, wrong_secondary):
    assert cc.classify_condition(condition)[1] != wrong_secondary
