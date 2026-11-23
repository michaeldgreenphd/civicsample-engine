"""Pinned deterministic rule vocabulary and numeric threshold extractors for `eligibility/`.

Every regex pattern, section applicability constraint, polarity effect, negative
guard, and threshold extraction pattern in this module is serialized into a
canonical JSON payload and hashed as `VOCABULARY_SHA256`. Any edit to a rule
pattern, guard, modifier, or threshold regex moves `VOCABULARY_SHA256` and
fails `verify_vocabulary_pin()` until `EXPECTED_VOCABULARY_SHA256` is
deliberately updated alongside its tests.

Covers 12 clinical and demographic representation domains (`DOMAINS`):
  1. `pregnancy_or_lactation`
  2. `hiv_infection`
  3. `hepatitis_b_or_c`
  4. `renal_function`
  5. `hepatic_function`
  6. `cardiac_function`
  7. `cns_or_brain_metastases`
  8. `performance_status`
  9. `psychiatric_or_substance_use`
 10. `prior_malignancy`
 11. `language_or_literacy`
 12. `free_text_age_cutoff`
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

VOCABULARY_VERSION: str = "eligibility_vocabulary/1.0.0"

DOMAINS: tuple[str, ...] = (
    "pregnancy_or_lactation",
    "hiv_infection",
    "hepatitis_b_or_c",
    "renal_function",
    "hepatic_function",
    "cardiac_function",
    "cns_or_brain_metastases",
    "performance_status",
    "psychiatric_or_substance_use",
    "prior_malignancy",
    "language_or_literacy",
    "free_text_age_cutoff",
)

RULE_EFFECTS: tuple[str, ...] = (
    "target_inclusion",
    "strictly_excluded",
    "numeric_cutoff",
    "exception_permitted",
    "contraception_only",
    "domain_mention",
)

VALID_APPLICABLE_SECTIONS: frozenset[str] = frozenset({"inclusion", "exclusion", "unheaded"})


@dataclass(frozen=True)
class DomainRule:
    """One deterministic regex rule in the pinned eligibility vocabulary."""

    rule_id: str
    domain: str
    effect: str
    applicable_sections: tuple[str, ...]
    pattern: str
    guard_not_pattern: str | None
    rationale: str

    def canonical_dict(self) -> dict[str, Any]:
        """Deterministic dictionary representation hashed into `VOCABULARY_SHA256`."""
        return {
            "applicable_sections": list(self.applicable_sections),
            "domain": self.domain,
            "effect": self.effect,
            "guard_not_pattern": self.guard_not_pattern,
            "pattern": self.pattern,
            "rationale": self.rationale,
            "rule_id": self.rule_id,
        }


# Auxiliary polarity cues and threshold extraction patterns — also included in VOCABULARY_SHA256.
MODIFIER_PATTERNS: dict[str, str] = {
    "unheaded_explicit_exclusion": (
        r"\b(?:are\s+excluded|is\s+excluded|will\s+be\s+excluded|are\s+ineligible|"
        r"is\s+ineligible|not\s+eligible|must\s+not\b|may\s+not\b|shall\s+not\b|"
        r"cannot\b|can\s+not\b|contraindicated|prohibited|excluded\s+if)\b"
    ),
    "unheaded_explicit_inclusion": (
        r"\b(?:must\s+(?:be|have|meet|agree|provide)|eligible\s+(?:if|patients?|participants?|subjects?|adults?)|"
        r"required\s+to\b|to\s+be\s+eligible)\b"
    ),
    "local_negation_prefix": (
        r"\b(?:no|not|non[- ]?|without|neither|never|free\s+(?:of|from)|absence\s+of|"
        r"negative\s+for|must\s+not|may\s+not|shall\s+not|cannot|can\s+not)"
        r"(?:\s+(?:have|be|prior|previous|known|active|current|documented|confirmed|chronic|"
        r"history|evidence|sign\w*|hiv(?:[- ]?[12])?|hbv|hcv|hepatitis\s+[bc]|or|and|nor|of|,)){0,10}\s*$"
    ),
    "general_exception_cue": (
        r"\b(?:unless\b|except(?:\s+for|\s+if|\s+when|\s+in|\s*:)?|exception\s*:|"
        r"(?<!not\s)(?<!never\s)(?<!also\s)(?:permitted|allowed|acceptable)\b|"
        r"(?:are|is)\s+(?<!not\s)eligible\b|"
        r"may\s+(?:be\s+)?(?:enrolled|included|participate|enter)|not\s+excluded)\b"
    ),
    "gilberts_syndrome": r"\bgilbert(?:'?s|\u2019s)?\s*(?:syndrome|disease)?\b",
    "skin_or_insitu_exception": (
        r"\b(?:non[- ]?melanoma\s+skin\s+(?:cancer|carcinoma)|"
        r"basal\s+cell(?:\s+(?:or|and)\s+squamous\s+cell)?(?:\s+skin)?(?:\s+carcinoma|\s+cancer)?|"
        r"squamous\s+cell\s+(?:skin|cutaneous)\s+(?:carcinoma|cancer)|"
        r"squamous\s+cell\s+(?:carcinoma|cancer)\s+of\s+the\s+skin|"
        r"carcinoma\s+in\s+situ|"
        r"in\s+situ\s+(?:carcinoma|cervical|breast|bladder|melanoma)|"
        r"cervical\s+intraepithelial\s+neoplasia|superficial\s+bladder\s+(?:cancer|carcinoma)|"
        r"papillary\s+thyroid\s+(?:cancer|carcinoma)|localized\s+prostate\s+cancer)\b"
    ),
    "age_false_positive_context": (
        r"\b(?:gestational\s+age[ds]?|post[- ]?menstrual\s+age[ds]?|post[- ]?conceptional\s+age[ds]?|"
        r"corrected\s+age[ds]?|bone\s+age[ds]?|mental\s+age[ds]?|"
        r"age[ds]?\s+at\s+(?:diagnosis|symptom\s+onset|disease\s+onset|onset|menarche|menopause|screening\s+of\s+donor)|"
        r"(?:onset|symptom\s+onset|disease\s+onset|diagnosed|diagnosis|menarche|menopause)\s+"
        r"(?:at|before|after|prior\s+to|by|under|over)\s+age[ds]?|"
        r"(?:partner|spouse|caregiver|care[- ]?partner|informant|guardian|donor|maternal|paternal|fetal|foetal)\s+"
        r"(?:who\s+is\s+|must\s+be\s+)?age[ds]?|"
        r"(?:mothers?|fathers?|parents?|caregivers?|guardians?)\s+of\s+"
        r"(?:children|infants?|adolescents?|offspring|neonates?|toddlers?)|"
        r"child[- ]?bearing\s+age|"
        r"\d+(?:\.\d+)?\s*(?:(?:to|and|-|–|—)\s*\d+(?:\.\d+)?\s*)?(?:weeks?|wks?|months?|mos?)\s+(?:of\s+)?gestation)\b"
    ),
    "duration_lookback_false_positive": (
        r"\b(?:within\s+(?:the\s+)?(?:past|last|preceding|prior)\s+\d+|"
        r"in\s+the\s+(?:past|last|preceding|prior)\s+\d+|"
        r"over\s+the\s+(?:past|last)\s+\d+|"
        r"for\s+(?:at\s+least|>=|≥|>|more\s+than|less\s+than)\s*\d+\s*(?:years?|months?|weeks?|days?)|"
        r"\d+\s*(?:years?|months?|weeks?|days?)\s+(?:ago|prior|before|after|following|post)|"
        r"pack[- ]?years?|life\s+expectancy|disease[- ]?free\s+for|remission\s+for)\b"
    ),
}

THRESHOLD_PATTERNS: dict[str, str] = {
    "egfr_value": (
        r"\b(?:egfr|estimated\s+(?:gfr|glomerular\s+filtration\s+rate)|glomerular\s+filtration\s+rate)"
        r"(?:\s*\([^)\r\n]{1,40}\))?\s*(?:of\s+|is\s+|:)?\s*"
        r"(?P<op>>=|≤|<=|≥|>|<|=|at\s+least|at\s+most|no\s+less\s+than|less\s+than|below|above|greater\s+than)?"
        r"\s*(?P<v1>\d+(?:\.\d+)?)"
        r"(?:\s*(?:to|-|–|and)\s*(?P<v2>\d+(?:\.\d+)?))?"
        r"(?:\s*ml\s*/\s*min)?"
    ),
    "crcl_value": (
        r"\b(?:crcl|creatinine\s+clearance)(?:\s*\([^)\r\n]{1,40}\))?\s*(?:of\s+|is\s+|:)?\s*"
        r"(?P<op>>=|≤|<=|≥|>|<|=|at\s+least|no\s+less\s+than|less\s+than|below|above|greater\s+than)?"
        r"\s*(?P<v1>\d+(?:\.\d+)?)"
        r"(?:\s*(?:to|-|–|and)\s*(?P<v2>\d+(?:\.\d+)?))?"
        r"(?:\s*ml\s*/\s*min)?"
    ),
    "creatinine_uln": (
        r"\b(?:serum\s+|plasma\s+)?creatinine(?!\s+clearance)\s*"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)?\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*(?:x|×|\*|times)\s*(?:the\s+)?(?:institutional\s+)?uln\b"
    ),
    "creatinine_mg_dl": (
        r"\b(?:serum\s+|plasma\s+)?creatinine(?!\s+clearance)\s*"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)?\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*mg\s*/\s*dl\b"
    ),
    "ast_alt_uln": (
        r"\b(?P<analyte>(?:ast(?:\s*\(\s*sgot\s*\))?|sgot)\s*(?:and\s*/\s*or|and|or|/|,)\s*"
        r"(?:alt(?:\s*\(\s*sgpt\s*\))?|sgpt)|"
        r"ast(?:\s*\(\s*sgot\s*\))?|sgot|alt(?:\s*\(\s*sgpt\s*\))?|sgpt|"
        r"(?:hepatic\s+|serum\s+)?transaminases|aminotransferases)"
        r"[^\r\n.;]{0,35}?"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*(?:x|×|\*|times)\s*(?:the\s+)?(?:institutional\s+)?uln\b"
    ),
    "bilirubin_uln": (
        r"\b(?:total\s+|serum\s+)?bilirubin\b[^\r\n.;()]{0,30}?"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*(?:x|×|\*|times)\s*(?:the\s+)?(?:institutional\s+)?uln\b"
    ),
    "bilirubin_mg_dl": (
        r"\b(?:total\s+|serum\s+)?bilirubin\b[^\r\n.;()]{0,25}?"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*mg\s*/\s*dl\b"
    ),
    "gilberts_uln": (
        r"(?:(?:<=|≤|<|up\s+to|>=|≥|>|above|over|exceeding)\s*(?P<v1>\d+(?:\.\d+)?)\s*(?:x|×|\*|times)\s*(?:the\s+)?uln"
        r"[^\r\n.;]{0,55}?\bgilbert(?:'?s|\u2019s)?|"
        r"\bgilbert(?:'?s|\u2019s)?[^\r\n.;]{0,55}?(?:<=|≤|<|up\s+to|>=|≥|>|above|over|exceeding)\s*(?P<v2>\d+(?:\.\d+)?)\s*"
        r"(?:x|×|\*|times)\s*(?:the\s+)?uln)"
    ),
    "lvef_pct": (
        r"\b(?:lvef|left\s+ventricular\s+ejection\s+fraction)\b[^\r\n.;]{0,30}?"
        r"(?P<op>>=|≥|<=|≤|>|<|at\s+least|no\s+less\s+than|less\s+than|below)\s*"
        r"(?P<val>\d+(?:\.\d+)?)\s*%?"
    ),
    "qtc_ms": (
        r"\b(?P<kind>qtcf|qtcb|qtc)\b[^\r\n.;]{0,35}?"
        r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|exceeding|above|over)\s*"
        r"(?P<val>\d{3}(?:\.\d+)?)\s*(?:ms|msec|milliseconds?)?\b"
    ),
    "nyha_class": (
        r"\bnyha\s+(?:functional\s+)?class(?:ification)?\s+"
        r"(?P<spec>(?:>=|≥|>)\s*(?:ii|iii|iv|[234])|"
        r"(?:i|ii|iii|iv|[1234])(?:\s*(?:or|and|/|-|–|to|,)\s*(?:i|ii|iii|iv|[1234]))*)\b"
    ),
    "ecog_score": (
        r"\b(?:ecog|zubrod|eastern\s+cooperative\s+oncology\s+group|who)\s*"
        r"(?:performance\s+(?:status|score)|ps|score|status)?\s*(?:of\s+|is\s+|:|=)?\s*"
        r"(?P<spec>"
        r"(?:<=|≤|<|>=|≥|>|=)\s*[0-4]"
        r"|[0-4]\s*(?:,\s*[0-4]\s*,?\s*or\s+[0-4]|or\s+[0-4]|to\s+[0-4]|[-–][0-4])\b"
        r"|[0-4]\s+or\s+(?:higher|greater|more|above|worse)\b"
        r"|[0-4]\b"
        r")"
    ),
    "karnofsky_score": (
        r"\b(?:karnofsky(?:\s+performance\s+(?:status|score))?|kps)\s*(?:of\s+|is\s+|:)?\s*"
        r"(?P<op>>=|≥|<=|≤|>|<|at\s+least|less\s+than|below)?\s*(?P<val>\d{2,3})\s*%?"
    ),
    "lansky_score": (
        r"\blansky(?:\s+play[- ]?performance\s+(?:status|scale|score)|\s+score|\s+status)?\s*"
        r"(?:of\s+|is\s+|:)?\s*(?P<op>>=|≥|<=|≤|>|<|at\s+least|less\s+than|below)?\s*(?P<val>\d{2,3})\s*%?"
    ),
    "hiv_cd4": (
        r"\bcd4\+?(?:\s+t[- ]?cell)?(?:\s+count)?\s*(?:>=|≥|>|at\s+least|above|of\s+at\s+least)\s*"
        r"(?P<val>\d{1,3}(?:,\d{3})+|\d+)\b"
    ),
    "malignancy_lookback": (
        r"\b(?:within(?:\s+the\s+(?:past|last|preceding|prior))?|in\s+the\s+(?:past|last|preceding)|"
        r"disease[- ]?free\s+for(?:\s+at\s+least|\s*>=|\s*≥|\s*<)?|"
        r"progression[- ]?free\s+for(?:\s+at\s+least)?|"
        r"at\s+least)\s*(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>years?|yrs?|months?|mos?)\b"
    ),
    "named_languages": (
        r"\b(?P<lang>English|Spanish|French|Mandarin|Cantonese|Chinese|German|Portuguese|"
        r"Arabic|Korean|Vietnamese|Russian|Japanese|Italian|Tagalog|Hindi|Polish|Haitian\s+Creole)\b"
    ),
}


RULES: tuple[DomainRule, ...] = (
    # ------------------------------------------------------------------
    # 1. pregnancy_or_lactation
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="preg_target_inclusion",
        domain="pregnancy_or_lactation",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:confirmed\s+|singleton\s+|viable\s+|intrauterine\s+|uncomplicated\s+|documented\s+){0,5}"
            r"pregnan(?:t|cy)\s+(?:women|mothers?|participants?|subjects?|patients?|individuals?|persons?|females?|"
            r"in\s+(?:the\s+|their\s+)?(?:first|second|third|1st|2nd|3rd)\s+trimester|"
            r"(?:at|between|of|from)\s+\d+\s*(?:to|and|-|–|—)\s*\d+\s*(?:weeks|months)(?:\s+(?:of\s+)?gestation)?)"
            r"|in\s+(?:the\s+|their\s+)?(?:first|second|third|1st|2nd|3rd)\s+trimester\s+of\s+pregnancy"
            r"|singleton\s+(?:intrauterine\s+)?pregnancy"
            r"|gestational\s+age\s+(?:of\s+|between\s+|>=|≥|>\s*)?\d+\s*(?:(?:to|-|–|and)\s*\d+\s*)?weeks"
            r"|(?:currently\s+|exclusively\s+|actively\s+)(?:breast[- ]?feeding|lactating|nursing)\s+"
            r"(?:women|mothers?|participants?|individuals?|subjects?)"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:not\s+(?:be\s+)?(?:pregnant|breast[- ]?feeding|lactating)|non[- ]?pregnant|"
            r"neither\s+pregnant|negative\s+(?:serum\s+|urine\s+|beta[- ]?hcg\s+)?pregnancy\s+test|"
            r"avoid\s+(?:becoming\s+)?pregnan|prevent\s+pregnancy|female\s+partner\w*\s+(?:is\s+|who\s+is\s+)?pregnant|"
            r"are\s+excluded|is\s+excluded|ineligible)\b"
        ),
        rationale="Maternal, obstetric, and lactation trials explicitly enrolling pregnant or breastfeeding participants.",
    ),
    DomainRule(
        rule_id="preg_exception_allowance",
        domain="pregnancy_or_lactation",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"pregnant\s+(?:women|patients?|participants?|subjects?|individuals?)\s+"
            r"(?:in\s+[^\r\n.;]{1,50}\s+)?(?:are|may\s+be)\s+(?:eligible|permitted|allowed)"
            r"|(?:breast[- ]?feeding|lactating|nursing)\s+(?:women\s+|mothers?\s+|participants?\s+)?"
            r"(?:is\s+|are\s+|may\s+be\s+)?(?:permitted|allowed|eligible)\s+(?:if|provided|when|after)"
            r"|if\s+(?:breast[- ]?feeding|lactating|nursing)\s*,?\s*(?:must|agrees?\s+to)\s+"
            r"(?:discontinue|stop|interrupt|pump\s+and\s+discard)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Explicit clinical exception allowing pregnancy in designated cohorts/trimesters or conditional lactation.",
    ),
    DomainRule(
        rule_id="preg_blanket_exclusion_exc",
        domain="pregnancy_or_lactation",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"pregnan(?:t|cy)"
            r"|breast[- ]?feeding"
            r"|lactat(?:ing|ion)"
            r"|nursing\s+(?:women|mothers?|females?)"
            r"|or\s+nursing\b"
            r"|intend(?:ing|s)?\s+to\s+(?:become\s+pregnant|conceive|breast[- ]?feed)"
            r"|planning\s+(?:a\s+|to\s+become\s+)?pregnan(?:t|cy)"
            r"|positive\s+(?:serum\s+|urine\s+|beta[- ]?hcg\s+)?pregnancy\s+test"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:nursing\s+home|skilled\s+nursing\s+facility|nursing\s+staff|nursing\s+care)\b",
        rationale="Exclusion-section pregnancy, lactation, nursing-mother, or positive pregnancy-test bans.",
    ),
    DomainRule(
        rule_id="preg_blanket_exclusion_inc",
        domain="pregnancy_or_lactation",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"(?:must\s+|shall\s+)?not\s+(?:be\s+)?(?:pregnant|breast[- ]?feeding|lactating|nursing\s+a\s+child)"
            r"|no\s+(?:current\s+|known\s+)?pregnan(?:t|cy)(?:\s+(?:or|and)\s+(?:breast[- ]?feeding|lactat(?:ing|ion)|nursing))?"
            r"|non[- ]?pregnant"
            r"|neither\s+pregnant\s+nor\s+(?:breast[- ]?feeding|lactating|nursing)"
            r"|negative\s+(?:(?:serum|urine|blood|beta[- ]?hcg)(?:\s*(?:,|or|and|/)\s*(?:serum|urine|blood|beta[- ]?hcg)){0,4}\s+)?pregnancy\s+test"
            r"|pregnant\s+(?:or\s+(?:breast[- ]?feeding|lactating|nursing)\s+)?"
            r"(?:women|patients?|participants?|subjects?|individuals?)\s+(?:are|will\s+be)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Direct pregnancy/lactation prohibition or mandatory negative pregnancy test written outside Exclusion.",
    ),
    DomainRule(
        rule_id="preg_contraception_only",
        domain="pregnancy_or_lactation",
        effect="contraception_only",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"contracepti(?:on|ve|ves)"
            r"|birth\s+control"
            r"|(?:highly\s+effective\s+|double[- ]?barrier\s+|barrier\s+)method\w*\s+of\s+(?:birth\s+control|contraception)"
            r"|child[- ]?bearing\s+potential[^\r\n.;]{0,120}\b(?:agree|willing|use|practice|abstinen)"
            r"|refrain\s+from\s+(?:heterosexual\s+intercourse|sperm\s+donation|egg\s+donation|ova\s+donation)"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:pregnant\s+or\s+(?:breast[- ]?feeding|lactating|nursing)|currently\s+pregnant|"
            r"must\s+not\s+be\s+pregnant|non[- ]?pregnant|negative\s+(?:serum\s+|urine\s+)?pregnancy\s+test)\b"
        ),
        rationale="Contraception-only requirement for reproductive-potential participants without excluding pregnancy directly.",
    ),
    DomainRule(
        rule_id="preg_unheaded_mention",
        domain="pregnancy_or_lactation",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:pregnan(?:t|cy)|breast[- ]?feeding|lactat(?:ing|ion))\b",
        guard_not_pattern=None,
        rationale="Surface unheaded pregnancy/lactation mentions lacking explicit polarity verbs as ambiguous_or_unheaded.",
    ),

    # ------------------------------------------------------------------
    # 2. hiv_infection
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="hiv_target_inclusion",
        domain="hiv_infection",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:documented|confirmed|chronic|established|diagnosed|laboratory[- ]confirmed)\s+"
            r"(?:hiv(?:[- ]?[12])?\s*(?:infection|positive|seropositive|\+)|human\s+immunodeficiency\s+virus(?:\s+type\s+[12])?\s+infection)"
            r"|hiv(?:[- ]?[12])?[- ]?(?:positive|seropositive|infected)\s+(?:adults?|patients?|participants?|subjects?|individuals?|men|women)"
            r"|people\s+living\s+with\s+hiv|persons?\s+with\s+hiv|\bplwh\b"
            r"|plasma\s+hiv[- ]?1\s+rna\s*(?:>=|≥|>|at\s+least)\s*\d+"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:no\s+(?:known\s+|active\s+|history\s+of\s+)?hiv|not\s+hiv|"
            r"hiv[- ]?(?:1\s*/\s*2\s+)?negative|negative\s+(?:for\s+)?hiv|without\s+hiv|seronegative|"
            r"if\s+hiv[- ]?(?:positive|infected)|"
            r"(?:are|is|will\s+be|may\s+be)\s+(?:eligible|permitted|allowed|acceptable)|"
            r"may\s+(?:be\s+)?enroll(?:ed)?|"
            r"(?:are|is|will\s+be)\s+(?:excluded|ineligible)|not\s+eligible|ineligible)\b"
        ),
        rationale="HIV treatment, cure, or comorbidity trials positively requiring HIV infection for enrollment.",
    ),
    DomainRule(
        rule_id="hiv_conditional_allowance",
        domain="hiv_infection",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:hiv(?:[- ]?[12])?|human\s+immunodeficiency\s+virus)\b[\s\S]{0,240}?"
            r"\b(?:"
            r"(?:stable|effective|suppressive)\s+(?:antiretroviral\s+therapy|anti[- ]?retroviral\s+therapy|art|cart|haart)"
            r"|undetectable\s+(?:hiv\s+|plasma\s+)?viral\s+load"
            r"|hiv(?:[- ]?1)?\s+rna\s*(?:<|<=|≤|below)\s*\d+"
            r"|virologic(?:ally)?\s+suppressed"
            r"|cd4\+?(?:\s+t[- ]?cell)?(?:\s+count)?\s*(?:>=|≥|>|at\s+least|above)\s*\d+"
            r"|well[- ]?controlled\s+hiv"
            r"|(?:are|is|may\s+be)\s+(?:eligible|permitted|allowed|not\s+excluded)"
            r")\b"
            r"|"
            r"\b(?:undetectable\s+viral\s+load|stable\s+(?:art|antiretroviral\s+therapy)|cd4\+?\s*(?:>=|≥|>)\s*\d+)\b"
            r"[\s\S]{0,160}?\b(?:hiv(?:[- ]?[12])?|human\s+immunodeficiency\s+virus)\b"
        ),
        guard_not_pattern=r"\b(?:confirmed\s+hiv[- ]?1\s+infection\s+and\s+on\s+stable\s+art\s+for\s+this\s+hiv\s+switch)\b",
        rationale="NCI/FDA-aligned conditional eligibility for PLWH on stable ART with viral suppression and/or adequate CD4.",
    ),
    DomainRule(
        rule_id="hiv_blanket_exclusion_exc",
        domain="hiv_infection",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"hiv(?:[- ]?[12])?\b"
            r"|human\s+immunodeficiency\s+virus"
            r"|acquired\s+immunodeficiency\s+syndrome"
            r"|history\s+of\s+aids\b"
            r"|aids[- ]defining\s+(?:illness|condition|infection)"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:hearing\s+aids?|visual\s+aids?|mobility\s+aids?|walking\s+aids?|sleep\s+aids?|first\s+aid)\b",
        rationale="Blanket HIV or AIDS exclusion in Exclusion criteria when untempered by ART/viral-load allowance.",
    ),
    DomainRule(
        rule_id="hiv_blanket_exclusion_inc",
        domain="hiv_infection",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"hiv[- ]?(?:1\s*(?:/|and|or)\s*(?:hiv[- ]?)?2\s+|[12]\s+)?negative"
            r"|negative\s+(?:for\s+|serology\s+for\s+|test\s+for\s+)?(?:hiv|human\s+immunodeficiency\s+virus)"
            r"|no\s+(?:known\s+|active\s+|history\s+of\s+)?(?:hiv|human\s+immunodeficiency\s+virus)"
            r"|must\s+not\s+have\s+(?:hiv|human\s+immunodeficiency\s+virus)"
            r"|hiv[- ]?(?:positive|infected)\s+(?:patients?|participants?|subjects?|individuals?)\s+(?:are|will\s+be)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Mandatory HIV-negative serology or direct HIV exclusion written in Inclusion or unheaded text.",
    ),
    DomainRule(
        rule_id="hiv_unheaded_mention",
        domain="hiv_infection",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:hiv(?:[- ]?[12])?|human\s+immunodeficiency\s+virus)\b",
        guard_not_pattern=None,
        rationale="Unheaded HIV mention without explicit polarity verb.",
    ),

    # ------------------------------------------------------------------
    # 3. hepatitis_b_or_c
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="hbv_hcv_target_inclusion",
        domain="hepatitis_b_or_c",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:chronic|confirmed|documented|active|untreated|treatment[- ]naive)\s+"
            r"(?:hepatitis\s+[bc](?:\s+virus)?(?:\s+infection)?|hbv(?:\s+infection)?|hcv(?:\s+infection)?)"
            r"|hcv\s+genotype\s+[1-6]"
            r"|hbsag[- ]?positive(?:\s+for\s+(?:>=|≥|>|at\s+least)\s*\d+\s*months)?"
            r"|detectable\s+(?:serum\s+|plasma\s+)?(?:hcv\s+rna|hbv\s+dna)"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:no\s+(?:known\s+|active\s+|history\s+of\s+)?(?:hepatitis|hbv|hcv)|"
            r"negative\s+(?:for\s+)?(?:hepatitis|hbsag|hcv|hbv)|hbsag[- ]?negative|"
            r"if\s+(?:hbsag|hcv|hbv)|(?:are|is|may\s+be)\s+(?:eligible|permitted|allowed)|"
            r"are\s+excluded|ineligible)\b"
        ),
        rationale="Hepatology/antiviral trials targeting chronic HBV or HCV infection.",
    ),
    DomainRule(
        rule_id="hbv_hcv_conditional_allowance",
        domain="hepatitis_b_or_c",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:hepatitis\s+[bc](?!\s+vaccin)|hbv(?!\s+vaccin)|hcv(?!\s+vaccin)|hbsag|hbcab|anti[- ]?hcv)\b[\s\S]{0,260}?"
            r"\b(?:"
            r"(?:undetectable|negative|cleared|unquantifiable)\s+(?:serum\s+|plasma\s+)?(?:hcv\s+rna|hbv\s+dna|viral\s+load)"
            r"|(?:hcv\s+rna|hbv\s+dna)\s+(?:is\s+|must\s+be\s+)?(?:undetectable|negative|<|<=|≤|below)"
            r"|(?:cured|resolved|cleared|prior\s+curative|sustained\s+virologic\s+response|\bsvr\b)"
            r"|(?:stable|suppressive|effective)\s+(?:antiviral|nucleos\(t\)ide)\s+therapy"
            r"|(?:are|is|may\s+be)\s+(?:eligible|permitted|allowed|not\s+excluded)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Conditional allowance for cured/cleared HCV (RNA negative) or suppressed/resolved HBV on antiviral therapy.",
    ),
    DomainRule(
        rule_id="hbv_hcv_blanket_exclusion_exc",
        domain="hepatitis_b_or_c",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=r"\b(?:hepatitis\s+[bc](?!\s+vaccin)\b|hbv(?!\s+vaccin)\b|hcv(?!\s+vaccin)\b|hbsag\b|hbcab\b|anti[- ]?hcv\b)\b",
        guard_not_pattern=None,
        rationale="Blanket Hepatitis B or C exclusion when no cured/RNA-negative or antiviral suppression allowance is given.",
    ),
    DomainRule(
        rule_id="hbv_hcv_blanket_exclusion_inc",
        domain="hepatitis_b_or_c",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"negative\s+(?:for\s+|serology\s+for\s+)?(?:hepatitis\s+[bc]|hbsag|hcv|hbv)"
            r"|hbsag[- ]?negative(?:\s+and\s+hcv[- ]?(?:antibody[- ]?)?negative)?"
            r"|no\s+(?:known\s+|active\s+|history\s+of\s+)?(?:hepatitis\s+[bc]|hbv|hcv)"
            r"|(?:hepatitis\s+[bc]|hbv|hcv)\s+(?:patients?|infection)\s+(?:are|is)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Mandatory negative HBV/HCV serology or direct exclusion outside Exclusion section.",
    ),
    DomainRule(
        rule_id="hbv_hcv_unheaded_mention",
        domain="hepatitis_b_or_c",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:hepatitis\s+[bc]|hbv|hcv|hbsag)\b",
        guard_not_pattern=None,
        rationale="Unheaded HBV/HCV mention without explicit polarity verb.",
    ),

    # ------------------------------------------------------------------
    # 4. renal_function
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="renal_target_inclusion",
        domain="renal_function",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"end[- ]?stage\s+(?:renal|kidney)\s+disease|\besrd\b"
            r"|(?:on\s+|receiving\s+|requiring\s+)(?:chronic\s+|maintenance\s+)?(?:hemodialysis|peritoneal\s+dialysis|dialysis)"
            r"|chronic\s+kidney\s+disease\s+stage\s*(?:3b|4|5|iii|iv|v)"
            r"|(?:moderate|severe)\s+renal\s+(?:impairment|insufficiency)"
            r"|(?:egfr|estimated\s+gfr|glomerular\s+filtration\s+rate|crcl|creatinine\s+clearance)"
            r"(?:\s*\([^)\r\n]{1,40}\))?\s*(?:of\s+|:)?\s*"
            r"(?:(?:<|<=|≤|less\s+than|below)\s*(?:15|20|25|29|30|45|50|59|60)\b|"
            r"(?:10|15|20|30)\s*(?:to|-|–|and)\s*(?:29|30|44|45|59)\b)"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+(?:end[- ]?stage|severe\s+renal|dialysis)|not\s+on\s+dialysis|excluded|ineligible)\b",
        rationale="Nephrology and renal-impairment PK trials specifically targeting CKD, ESRD, dialysis, or impaired eGFR/CrCl.",
    ),
    DomainRule(
        rule_id="renal_numeric_cutoff",
        domain="renal_function",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion"),
        pattern=(
            r"\b(?:"
            r"(?:egfr|estimated\s+(?:gfr|glomerular\s+filtration\s+rate)|glomerular\s+filtration\s+rate|"
            r"crcl|creatinine\s+clearance)(?:\s*\([^)\r\n]{1,40}\))?\s*(?:of\s+|is\s+|:)?\s*"
            r"(?:>=|≥|<=|≤|>|<|=|at\s+least|no\s+less\s+than|less\s+than|below|above|greater\s+than)\s*\d+(?:\.\d+)?"
            r"|(?:serum\s+|plasma\s+)?creatinine(?!\s+clearance)\s*"
            r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)\s*\d+(?:\.\d+)?\s*"
            r"(?:(?:x|×|\*|times)\s*(?:the\s+)?(?:institutional\s+)?uln|mg\s*/\s*dl)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Quantitative renal eligibility threshold (eGFR, CrCl, or serum creatinine multiple of ULN / mg/dL).",
    ),
    DomainRule(
        rule_id="renal_blanket_exclusion",
        domain="renal_function",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"renal\s+(?:insufficiency|impairment|failure|disease|dysfunction)"
            r"|chronic\s+kidney\s+disease"
            r"|end[- ]?stage\s+(?:renal|kidney)\s+disease|\besrd\b"
            r"|(?:hemodialysis|peritoneal\s+dialysis|renal\s+dialysis|\bdialysis\b)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Qualitative renal disease, renal failure, or dialysis exclusion lacking a numeric GFR/CrCl cutoff.",
    ),
    DomainRule(
        rule_id="renal_direct_exclusion_inc",
        domain="renal_function",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"no\s+(?:history\s+of\s+|active\s+|known\s+)?(?:renal\s+(?:disease|insufficiency|impairment|failure)|chronic\s+kidney\s+disease|dialysis)"
            r"|not\s+(?:on\s+|receiving\s+|requiring\s+)dialysis"
            r"|(?:renal\s+(?:failure|insufficiency)|dialysis)\s+(?:is|are)\s+excluded"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Direct negation of renal disease or dialysis in Inclusion or unheaded text.",
    ),
    DomainRule(
        rule_id="renal_unheaded_mention",
        domain="renal_function",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:egfr|creatinine\s+clearance|crcl|serum\s+creatinine|renal\s+(?:function|impairment|insufficiency)|dialysis)\b",
        guard_not_pattern=None,
        rationale="Unheaded mention of renal lab or disease without explicit polarity.",
    ),

    # ------------------------------------------------------------------
    # 5. hepatic_function
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="hepatic_target_inclusion",
        domain="hepatic_function",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:decompensated\s+|compensated\s+)?(?:hepatic|liver)\s+cirrhosis"
            r"|child[- ]?pugh\s+(?:class\s+|grade\s+|score\s+)?(?:b\s+or\s+c|b\b|c\b|[789]|1[0-5])"
            r"|(?:moderate|severe)\s+hepatic\s+(?:impairment|insufficiency|dysfunction)"
            r"|hepatorenal\s+syndrome|hepatic\s+encephalopathy"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+(?:cirrhosis|hepatic\s+impairment)|absence\s+of\s+cirrhosis|child[- ]?pugh\s+class\s+a\s+only|excluded|ineligible)\b",
        rationale="Hepatic-impairment PK and cirrhosis trials explicitly enrolling patients with impaired liver function.",
    ),
    DomainRule(
        rule_id="hepatic_gilberts_allowance",
        domain="hepatic_function",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\bgilbert(?:'?s|\u2019s)?\s*(?:syndrome|disease)?\b"
        ),
        guard_not_pattern=None,
        rationale="Explicit bilirubin allowance for benign unconjugated hyperbilirubinemia (Gilbert's syndrome).",
    ),
    DomainRule(
        rule_id="hepatic_numeric_cutoff",
        domain="hepatic_function",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion"),
        pattern=(
            r"\b(?:"
            r"(?:ast(?:\s*\(\s*sgot\s*\))?|sgot|alt(?:\s*\(\s*sgpt\s*\))?|sgpt|"
            r"(?:hepatic\s+|serum\s+)?transaminases|aminotransferases|(?:total\s+|serum\s+|direct\s+)?bilirubin)"
            r"[^\r\n.;]{0,55}?"
            r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|less\s+than|up\s+to|above|exceeding)\s*\d+(?:\.\d+)?\s*"
            r"(?:(?:x|×|\*|times)\s*(?:the\s+)?(?:institutional\s+)?uln|mg\s*/\s*dl)"
            r"|child[- ]?pugh\s+(?:class\s+|grade\s+)?(?:a\b|b\s+or\s+c\b|>=?\s*b)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Quantitative hepatic enzyme (AST/ALT), bilirubin (ULN or mg/dL), or Child-Pugh class threshold.",
    ),
    DomainRule(
        rule_id="hepatic_blanket_exclusion",
        domain="hepatic_function",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"(?:hepatic|liver)\s+(?:insufficiency|impairment|failure|cirrhosis|disease|dysfunction)"
            r"|decompensated\s+liver\s+disease"
            r"|chronic\s+liver\s+disease"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Blanket liver disease or hepatic dysfunction exclusion without quantitative threshold or exception.",
    ),
    DomainRule(
        rule_id="hepatic_unheaded_mention",
        domain="hepatic_function",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:bilirubin|ast\b|alt\b|sgot|sgpt|transaminases|child[- ]?pugh|hepatic\s+(?:function|impairment|cirrhosis))\b",
        guard_not_pattern=None,
        rationale="Unheaded mention of hepatic labs or cirrhosis without explicit polarity.",
    ),

    # ------------------------------------------------------------------
    # 6. cardiac_function
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="cardiac_target_inclusion",
        domain="cardiac_function",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:chronic\s+|symptomatic\s+|congestive\s+|acute\s+decompensated\s+){1,5}heart\s+failure"
            r"|\bhfref\b|\bhfpef\b|\bhfmref\b"
            r"|nyha\s+(?:functional\s+)?class\s+(?:ii\s*(?:to|-|–|,|\s+or\s+)\s*(?:iii|iv)|iii\s*(?:or|-|–|to)\s*iv)"
            r"|(?:lvef|left\s+ventricular\s+ejection\s+fraction)\s*(?:<=|≤|<|of\s+less\s+than)\s*(?:30|35|40|45)\s*%?"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+(?:history\s+of\s+)?heart\s+failure|absence\s+of\s+heart\s+failure|excluded|ineligible)\b",
        rationale="Cardiology trials positively requiring heart failure or reduced ejection fraction.",
    ),
    DomainRule(
        rule_id="cardiac_numeric_cutoff",
        domain="cardiac_function",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion"),
        pattern=(
            r"\b(?:"
            r"(?:lvef|left\s+ventricular\s+ejection\s+fraction)\b[^\r\n.;]{0,30}?"
            r"(?:>=|≥|<=|≤|>|<|at\s+least|no\s+less\s+than|less\s+than|below)\s*\d+(?:\.\d+)?\s*%?"
            r"|(?:qtcf|qtcb|qtc)\b[^\r\n.;]{0,35}?"
            r"(?:<=|≤|<|>=|≥|>|no\s+more\s+than|exceeding|above|over)\s*\d{3}(?:\.\d+)?\s*(?:ms|msec|milliseconds?)?"
            r"|nyha\s+(?:functional\s+)?class(?:ification)?\s+"
            r"(?:(?:>=|≥|>)\s*(?:ii|iii|iv|[234])|(?:iii|3)\s*(?:or|and|/|-|–|to)\s*(?:iv|4)|(?:i|1)\s*(?:or|and|/|-|–|to)\s*(?:ii|2))"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Quantitative LVEF %, QTc/QTcF ms, or NYHA Class III/IV functional severity threshold.",
    ),
    DomainRule(
        rule_id="cardiac_blanket_exclusion",
        domain="cardiac_function",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"congestive\s+heart\s+failure|\bchf\b"
            r"|nyha\s+(?:functional\s+)?class\s+i\s*(?:to|-|–)\s*iv"
            r"|clinically\s+significant\s+(?:cardiac|cardiovascular)\s+disease"
            r"|uncontrolled\s+(?:cardiac|cardiovascular)\s+disease"
            r"|myocardial\s+infarction|unstable\s+angina|uncontrolled\s+arrhythmia|long\s+qt\s+syndrome"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Blanket cardiovascular or heart-failure exclusion without quantitative LVEF/QTc/NYHA III-IV cutoff.",
    ),
    DomainRule(
        rule_id="cardiac_unheaded_mention",
        domain="cardiac_function",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:lvef|left\s+ventricular\s+ejection\s+fraction|qtcf|qtc\b|nyha\s+class|congestive\s+heart\s+failure)\b",
        guard_not_pattern=None,
        rationale="Unheaded mention of LVEF, QTc, NYHA, or heart failure without explicit polarity.",
    ),

    # ------------------------------------------------------------------
    # 7. cns_or_brain_metastases
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="cns_target_inclusion",
        domain="cns_or_brain_metastases",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:measurable|radiographically\s+confirmed|histologically\s+confirmed|documented|untreated|progressive|active|symptomatic)?\s*"
            r"(?:brain|central\s+nervous\s+system|cns|leptomeningeal|intracranial)\s+metastas[ei]s"
            r"|metastatic\s+brain\s+(?:tumors?|lesions?)"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:no\s+(?:known\s+|active\s+|untreated\s+|symptomatic\s+|history\s+of\s+)?(?:brain|cns|central\s+nervous|leptomeningeal|intracranial)|"
            r"absence\s+of\s+(?:brain|cns)|free\s+of\s+(?:brain|cns)|without\s+(?:brain|cns)|"
            r"(?:treated|stable|asymptomatic)[^\r\n.;]{0,90}(?:eligible|permitted|allowed|may\s+enroll)|"
            r"if\s+(?:brain|cns)\s+metastas[ei]s|are\s+excluded|ineligible)\b"
        ),
        rationale="Neuro-oncology and CNS metastasis trials specifically requiring brain or leptomeningeal metastases.",
    ),
    DomainRule(
        rule_id="cns_conditional_allowance",
        domain="cns_or_brain_metastases",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:brain|central\s+nervous\s+system|cns|intracranial)\s+metastas[ei]s\b[\s\S]{0,260}?"
            r"\b(?:treated|stable|asymptomatic|radiographically\s+stable|controlled|"
            r"unless\s+treated|except\s+treated|(?:are|is|may\s+be)\s+(?:eligible|permitted|allowed|acceptable))\b"
            r"|"
            r"\b(?:treated|previously\s+treated|stable|asymptomatic)(?:\s*,\s*(?:stable|asymptomatic|treated)){0,4}\s+"
            r"(?:brain|central\s+nervous\s+system|cns|intracranial)\s+metastas[ei]s\b[\s\S]{0,160}?"
            r"\b(?:eligible|permitted|allowed|acceptable|may\s+(?:be\s+)?enroll(?:ed)?|not\s+excluded)\b"
            r"|"
            r"\b(?:untreated|unstable|symptomatic|uncontrolled)\s+(?:or\s+(?:untreated|unstable|symptomatic)\s+)?"
            r"(?:brain|central\s+nervous\s+system|cns)\s+metastas[ei]s\b[\s\S]{0,180}?"
            r"\b(?:unless|except|permitted|allowed|eligible|stable|treated)\b"
        ),
        guard_not_pattern=None,
        rationale="Conditional eligibility for patients with previously treated, stable, or asymptomatic CNS/brain metastases.",
    ),
    DomainRule(
        rule_id="cns_blanket_exclusion_exc",
        domain="cns_or_brain_metastases",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=r"\b(?:brain|central\s+nervous\s+system|cns|leptomeningeal|intracranial)\s+(?:metastas[ei]s|involvement|tumors?\s+metastatic)\b",
        guard_not_pattern=None,
        rationale="Blanket exclusion of brain, CNS, or leptomeningeal metastases without a treated/stable allowance.",
    ),
    DomainRule(
        rule_id="cns_blanket_exclusion_inc",
        domain="cns_or_brain_metastases",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"(?:no|without|absence\s+of|free\s+of)\s+(?:known\s+|active\s+|history\s+of\s+|evidence\s+of\s+)?"
            r"(?:brain|central\s+nervous\s+system|cns|leptomeningeal|intracranial)\s+metastas[ei]s"
            r"|(?:brain|cns|leptomeningeal)\s+metastas[ei]s\s+(?:are|is)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Direct requirement of no brain/CNS metastases written in Inclusion or unheaded text.",
    ),
    DomainRule(
        rule_id="cns_unheaded_mention",
        domain="cns_or_brain_metastases",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:brain|central\s+nervous\s+system|cns|leptomeningeal)\s+metastas[ei]s\b",
        guard_not_pattern=None,
        rationale="Unheaded CNS/brain metastases mention without explicit polarity.",
    ),

    # ------------------------------------------------------------------
    # 8. performance_status
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="ps_target_inclusion",
        domain="performance_status",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:ecog|zubrod|eastern\s+cooperative\s+oncology\s+group)\s*"
            r"(?:performance\s+(?:status|score)|ps|score|status)?\s*(?:of\s+|is\s+|:|=)?\s*"
            r"(?:2\s*(?:or|to|-|–)\s*3|3\s*(?:or|to|-|–)\s*4|(?<!0\s)(?<!1\s)(?<!0-)(?<!1-)2\s+only|=\s*[23]\b|(?:>=|≥)\s*2\b)"
            r"|(?:karnofsky(?:\s+performance\s+status)?|kps)\s*(?:<|<=|≤)\s*(?:50|60)\s*%?"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:0\s*(?:,\s*1\s*,?\s*or|or|to|-|–)\s*2|excluded|ineligible)\b",
        rationale="Frail or poor-performance-status trials dedicated to ECOG >= 2 or KPS <= 60.",
    ),
    DomainRule(
        rule_id="ps_numeric_cutoff",
        domain="performance_status",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion"),
        pattern=(
            r"\b(?:"
            r"(?:ecog|zubrod|eastern\s+cooperative\s+oncology\s+group|who)\s*"
            r"(?:performance\s+(?:status|score)|ps|score|status)?\s*(?:of\s+|is\s+|:|=)?\s*"
            r"(?:(?:<=|≤|<|>=|≥|>|=)\s*[0-4]|[0-4]\s*(?:,\s*[0-4]\s*,?\s*or\s+[0-4]|or\s+[0-4]|to\s+[0-4]|[-–][0-4])|[0-4]\s+or\s+(?:higher|greater|more|above|worse)|0\b)"
            r"|(?:karnofsky(?:\s+performance\s+(?:status|score))?|kps)\s*(?:of\s+|is\s+|:)?\s*"
            r"(?:>=|≥|<=|≤|>|<|at\s+least|less\s+than|below)\s*\d{2,3}\s*%?"
            r"|lansky(?:\s+play[- ]?performance\s+(?:status|scale|score)|\s+score|\s+status)?\s*(?:of\s+|is\s+|:)?\s*"
            r"(?:>=|≥|<=|≤|>|<|at\s+least|less\s+than|below)\s*\d{2,3}\s*%?"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Quantitative performance status cap on ECOG/Zubrod, Karnofsky (KPS), or Lansky scale.",
    ),
    DomainRule(
        rule_id="ps_unheaded_mention",
        domain="performance_status",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:(?:ecog|zubrod)\s*(?:performance\s+status|ps|\d)|karnofsky|kps\s*(?:>=|<=|>|<|\d)|lansky)\b",
        guard_not_pattern=None,
        rationale="Unheaded performance status mention without explicit polarity verb.",
    ),

    # ------------------------------------------------------------------
    # 9. psychiatric_or_substance_use
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="psych_target_inclusion",
        domain="psychiatric_or_substance_use",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"(?:dsm[- ]?(?:iv|5|v)|icd[- ]?1[01]|primary|confirmed|clinical|current|documented)\s+diagnosis\s+of\s+"
            r"(?:(?:mild|moderate|severe|moderate[- ]to[- ]severe|recurrent|treatment[- ]resistant|acute|chronic|active)\s+){0,3}"
            r"(?:major\s+depressive\s+disorder|bipolar\s+(?:i|ii)?\s*disorder|schizophrenia|schizoaffective\s+disorder|"
            r"post[- ]?traumatic\s+stress\s+disorder|generalized\s+anxiety\s+disorder|obsessive[- ]?compulsive\s+disorder|"
            r"(?:opioid|alcohol|substance|stimulant|cocaine|cannabis|methamphetamine)\s+use\s+disorder)"
            r"|(?:moderate[- ]to[- ]severe|severe|moderate|recurrent|treatment[- ]resistant|active|current)\s+"
            r"(?:major\s+depressive\s+disorder|(?:opioid|alcohol|substance|stimulant|cocaine)\s+(?:use\s+disorder|dependence))"
            r"|active\s+suicidal\s+ideation\s+(?:requiring|with\s+c[- ]?ssrs)"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+(?:diagnosis|history|active|current)|without\s+(?:a\s+)?(?:diagnosis|history)|excluded|ineligible)\b",
        rationale="Psychiatry and addiction medicine trials positively targeting psychiatric or substance-use disorders.",
    ),
    DomainRule(
        rule_id="psych_conditional_allowance",
        domain="psychiatric_or_substance_use",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:psychiatric|depression|anxiety|substance\s+(?:use|abuse)|alcohol\s+abuse|drug\s+abuse)\b[\s\S]{0,220}?"
            r"\b(?:"
            r"(?:controlled|stable|well[- ]?controlled)\s+(?:depression|anxiety|psychiatric\s+condition)\s+"
            r"(?:on\s+stable\s+[^\r\n.;]{1,40}\s+)?(?:is\s+|are\s+)?(?:permitted|allowed|eligible)"
            r"|in\s+(?:sustained\s+|full\s+)?remission\s+for\s+(?:>=|≥|>|at\s+least)\s*\d+\s*(?:months?|years?)"
            r"|except\s+(?:nicotine|tobacco|caffeine|controlled\s+depression|stable\s+anxiety)"
            r"|medical\s+cannabis\s+is\s+(?:permitted|allowed)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Explicit clinical allowance for stable/controlled psychiatric conditions or sustained substance-use remission.",
    ),
    DomainRule(
        rule_id="psych_blanket_exclusion_exc",
        domain="psychiatric_or_substance_use",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"psychiatric\s+(?:illness|disorder\w*|disease|condition\w*|impairment)"
            r"|social\s+situations?\s+that\s+would\s+(?:limit|interfere\s+with)\s+compliance"
            r"|suicid(?:al\s+(?:ideation|behavior|intent|risk)|ality|e\s+attempt)"
            r"|(?:substance|drug|alcohol|opioid|illicit\s+drug)\s+(?:abuse|dependence|misuse|addiction|use\s+disorder)"
            r"|major\s+depressive\s+disorder"
            r"|severe\s+(?:depression|psychosis|schizophrenia|bipolar\s+disorder)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Psychiatric compliance, suicidality, or substance/alcohol abuse exclusion.",
    ),
    DomainRule(
        rule_id="psych_blanket_exclusion_inc",
        domain="psychiatric_or_substance_use",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"no\s+(?:history\s+of\s+|active\s+|current\s+|known\s+)?"
            r"(?:psychiatric\s+(?:illness|disorder\w*)|suicidal\s+ideation|(?:substance|alcohol|drug)\s+(?:abuse|dependence))"
            r"|(?:psychiatric\s+illness|substance\s+abuse|active\s+suicidality)\s+(?:is|are)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Direct negation of psychiatric disorder, suicidality, or substance abuse outside Exclusion.",
    ),
    DomainRule(
        rule_id="psych_unheaded_mention",
        domain="psychiatric_or_substance_use",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:psychiatric\s+(?:illness|disorder\w*)|suicid(?:al\s+ideation|ality)|(?:substance|alcohol|drug)\s+(?:abuse|dependence))\b",
        guard_not_pattern=None,
        rationale="Unheaded psychiatric or substance-use mention without explicit polarity verb.",
    ),

    # ------------------------------------------------------------------
    # 10. prior_malignancy
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="malignancy_target_inclusion",
        domain="prior_malignancy",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"therapy[- ]?related\s+(?:myeloid\s+neoplasm|acute\s+myeloid\s+leukemia|aml|mds)\s+"
            r"(?:following|after|secondary\s+to)\s+(?:a\s+)?prior\s+(?:malignancy|cancer)"
            r"|patients?\s+with\s+multiple\s+primary\s+malignancies"
            r"|cancer\s+survivors?\s+with\s+(?:a\s+)?documented\s+prior\s+malignancy"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+prior|excluded|ineligible)\b",
        rationale="Survivorship and therapy-related secondary neoplasm trials specifically targeting prior malignancy.",
    ),
    DomainRule(
        rule_id="malignancy_with_skin_or_insitu_exception",
        domain="prior_malignancy",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"(?:prior|previous|other|another|second|secondary|additional|concurrent|history\s+of(?:\s+another|\s+other)?)\s+"
            r"(?:primary\s+|invasive\s+)?(?:malignanc\w+|cancers?|neoplasms?|carcinomas?)"
            r"|malignanc\w+\s+within\s+(?:the\s+(?:past|last)\s+)?\d+\s*(?:years?|months?)"
            r")\b(?=[\s\S]{0,320}?\b(?:except|unless|other\s+than|excluding|with\s+the\s+exception\s+of|"
            r"exception\s*:|note\s*:|allowance\s*:|(?<!not\s)(?:permitted|allowed|eligible|acceptable)|not\s+excluded)\b)"
            r"[\s\S]{0,320}?"
            r"\b(?:"
            r"non[- ]?melanoma\s+skin\s+(?:cancer|carcinoma)|basal\s+cell|"
            r"squamous\s+cell\s+(?:skin|cutaneous)|squamous\s+cell\s+(?:carcinoma|cancer)\s+of\s+the\s+skin|"
            r"carcinoma\s+in\s+situ|in\s+situ\s+(?:carcinoma|cervical|breast|bladder|melanoma)|"
            r"superficial\s+bladder|papillary\s+thyroid|localized\s+prostate"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Prior/concurrent malignancy exclusion tempered by non-melanoma skin cancer or carcinoma in situ allowances.",
    ),
    DomainRule(
        rule_id="malignancy_numeric_lookback",
        domain="prior_malignancy",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion"),
        pattern=(
            r"\b(?:"
            r"(?:prior|previous|other|another|second|secondary|additional|concurrent|history\s+of(?:\s+another|\s+other)?)\s+"
            r"(?:primary\s+|invasive\s+)?(?:malignanc\w+|cancers?|neoplasms?)\b[^\r\n.;]{0,90}?"
            r"\b(?:within(?:\s+the\s+(?:past|last|preceding))?|in\s+the\s+(?:past|last)|disease[- ]?free\s+for(?:\s+at\s+least|\s*>=|\s*<)?)\s*"
            r"\d+(?:\.\d+)?\s*(?:years?|yrs?|months?|mos?)"
            r"|(?:active\s+)?malignanc\w+\s+within(?:\s+the\s+(?:past|last|preceding))?\s*\d+(?:\.\d+)?\s*(?:years?|yrs?|months?|mos?)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Time-bounded prior malignancy lookback window (e.g. within 2, 3, or 5 years) without skin/in-situ exception.",
    ),
    DomainRule(
        rule_id="malignancy_blanket_exclusion_exc",
        domain="prior_malignancy",
        effect="strictly_excluded",
        applicable_sections=("exclusion",),
        pattern=(
            r"\b(?:"
            r"(?:prior|previous|other|another|second|secondary|additional|concurrent)\s+"
            r"(?:primary\s+|invasive\s+|active\s+|cutaneous\s+)?(?:malignanc\w+|melanomas?|carcinomas?|sarcomas?|lymphomas?|leukemias?|cancers?|neoplasms?)"
            r"|history\s+of\s+(?:any\s+)?(?:other\s+|another\s+|prior\s+|second\s+)?(?:primary\s+|invasive\s+|cutaneous\s+)?"
            r"(?:malignanc\w+|melanomas?|carcinomas?|sarcomas?|lymphomas?|leukemias?|myelomas?|cancers?|neoplasms?)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Blanket exclusion of any prior or concurrent second malignancy with no time window or exception.",
    ),
    DomainRule(
        rule_id="malignancy_blanket_exclusion_inc",
        domain="prior_malignancy",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"no\s+(?:history\s+of\s+|known\s+)?(?:prior|previous|other|another|second|concurrent)\s+"
            r"(?:primary\s+|invasive\s+)?(?:malignanc\w+|cancers?|neoplasms?)"
            r"|(?:prior|other|second)\s+malignanc\w+\s+(?:is|are)\s+(?:excluded|ineligible)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Direct requirement of no prior/other malignancy in Inclusion or unheaded text.",
    ),
    DomainRule(
        rule_id="malignancy_unheaded_mention",
        domain="prior_malignancy",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:prior|previous|other|another|second|concurrent)\s+(?:primary\s+)?malignanc\w+\b",
        guard_not_pattern=None,
        rationale="Unheaded mention of prior/second malignancy without explicit polarity verb.",
    ),

    # ------------------------------------------------------------------
    # 11. language_or_literacy
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="lang_target_inclusion",
        domain="language_or_literacy",
        effect="target_inclusion",
        applicable_sections=("inclusion",),
        pattern=(
            r"\b(?:"
            r"limited\s+english\s+proficiency|\blep\b\s+(?:patients?|participants?|adults?|individuals?)"
            r"|(?:low|limited|inadequate)\s+health\s+literacy"
            r"|monolingual\s+non[- ]?english[- ]?speaking"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:excluded|ineligible)\b",
        rationale="Health-equity and communication trials specifically targeting LEP or limited health literacy populations.",
    ),
    DomainRule(
        rule_id="lang_accommodation_or_multilingual",
        domain="language_or_literacy",
        effect="exception_permitted",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"(?:able|willing|fluent|literate|proficien\w+|read|speak|write|understand|comprehend|communicate)"
            r"[^\r\n.;]{0,70}?"
            r"\b(?:English|Spanish|French|Mandarin|Chinese|German|Portuguese|Arabic|Korean|Vietnamese|Russian)"
            r"(?:\s*,\s*(?:English|Spanish|French|Mandarin|Chinese|German|Portuguese|Arabic|Korean|Vietnamese|Russian)){0,8}\s*"
            r"(?:,\s*)?(?:or|and/or|/)\s*"
            r"(?:Spanish|English|French|Mandarin|Chinese|German|Portuguese|Arabic|Korean|Vietnamese|Russian|another\s+language|local\s+language)"
            r"|(?:with(?:\s+(?:the\s+)?assistance\s+of)?|via|through|using|assistance\s+of)\s+"
            r"(?:(?:a|an|the|qualified|certified|professional|medical|hospital)\s+){0,6}"
            r"(?:interpreter|translator|translated\s+(?:informed\s+)?(?:consent|materials|short[- ]?form)|impartial\s+witness)"
            r"|(?:(?:a|an|the|qualified|certified|professional|medical|hospital)\s+){0,5}"
            r"(?:interpreter|translator)\s+(?:is\s+|services?\s+are\s+|may\s+be\s+|can\s+be\s+)?"
            r"(?:permitted|allowed|available|provided|used|utilized|employed)"
            r"|translated\s+(?:informed\s+)?consent(?:\s+form)?\s+(?:is\s+)?(?:available|permitted|used)"
            r")\b"
        ),
        guard_not_pattern=r"\b(?:no\s+interpreter|without\s+(?:the\s+use\s+of\s+)?an?\s+interpreter|interpreters?\s+(?:are\s+)?not\s+(?:permitted|allowed|used))\b",
        rationale="Multilingual option (e.g. English or Spanish) or explicit interpreter / translated-consent accommodation.",
    ),
    DomainRule(
        rule_id="lang_single_language_or_literacy_exclusion",
        domain="language_or_literacy",
        effect="strictly_excluded",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"(?:must\s+be\s+)?(?:able|fluent|literate|proficient)\s+(?:to\s+)?(?:read|speak|write|understand|comprehend|communicate\s+in)"
            r"(?:\s*(?:,\s*(?:and|or)\b|,|\band\b|\bor\b)\s*(?:read|speak|write|understand|comprehend)){0,6}\s+(?:in\s+)?"
            r"(?:spoken\s+and\s+written\s+)?(?:English|Spanish|French|German|Mandarin|Japanese|Italian)"
            r"|(?:fluent|proficient|literate)\s+in\s+(?:spoken\s+and\s+written\s+)?(?:English|Spanish|French|German)"
            r"|(?:English|Spanish|French)[- ]speaking(?:\s+only|\s+(?:participants?|patients?|subjects?|adults?|individuals?))?"
            r"|(?:unable|inability)\s+to\s+(?:read|speak|write|understand|comprehend|complete\s+questionnaires?\s+in)"
            r"(?:\s*(?:,\s*(?:and|or)\b|,|\band\b|\bor\b)\s*(?:read|speak|write|understand|comprehend)){0,6}\s+"
            r"(?:English|Spanish|French|German)"
            r"|non[- ]?(?:English|Spanish|French)[- ]?speaking"
            r"|illitera(?:te|cy)\b"
            r")\b"
        ),
        guard_not_pattern=(
            r"\b(?:"
            r"(?:English|Spanish|French)\s+(?:or|and/or|/)\s+(?:Spanish|English|French|Mandarin|Chinese|another\s+language)"
            r"|interpreter\s+(?:is\s+)?(?:permitted|allowed|available|provided)"
            r"|with\s+(?:the\s+)?assistance\s+of\s+(?:a\s+|an\s+)?(?:medical\s+|qualified\s+)?interpreter"
            r"|limited\s+english\s+proficiency"
            r")\b"
        ),
        rationale="Monolingual language requirement or literacy restriction excluding non-speakers without accommodation.",
    ),
    DomainRule(
        rule_id="lang_unheaded_mention",
        domain="language_or_literacy",
        effect="domain_mention",
        applicable_sections=("unheaded",),
        pattern=r"\b(?:language\s+proficiency|health\s+literacy|english\s+proficiency)\b",
        guard_not_pattern=None,
        rationale="Unheaded bare language/literacy mention lacking explicit inclusion/exclusion predicate.",
    ),

    # ------------------------------------------------------------------
    # 12. free_text_age_cutoff
    # ------------------------------------------------------------------
    DomainRule(
        rule_id="age_chronological_bound",
        domain="free_text_age_cutoff",
        effect="numeric_cutoff",
        applicable_sections=("inclusion", "exclusion", "unheaded"),
        pattern=(
            r"\b(?:"
            r"age[ds]?\s*(?::|=|is|of|between)?\s*(?:>=|≥|<=|≤|>|<|at\s+least|no\s+more\s+than|up\s+to|under|over|above|below|younger\s+than|older\s+than|less\s+than|greater\s+than)?\s*"
            r"\d+(?:\.\d+)?\s*(?:(?:years?|yrs?|months?|mos?|weeks?|wks?|days?)?\s*(?:of\s+age\s*)?(?:to|through|and|or|-|–|—)\s*"
            r"(?:>=|≥|<=|≤|>|<|over|under|older\s+than|younger\s+than)?\s*\d+(?:\.\d+)?)?\s*"
            r"(?:years?|yrs?|months?|mos?|weeks?|wks?|days?)?(?:\s*(?:of\s+age|old))?"
            r"|(?:>=|≥|<=|≤|>|<|at\s+least|no\s+older\s+than|no\s+more\s+than|up\s+to|under|over|younger\s+than|older\s+than|less\s+than)\s*"
            r"\d+(?:\.\d+)?\s*(?:years?|yrs?|months?|mos?|weeks?|wks?|days?)\s+(?:of\s+age|old)"
            r"|\d+(?:\.\d+)?\s*(?:(?:to|through|-|–|—)\s*\d+(?:\.\d+)?\s*)?(?:years?|yrs?|months?|mos?|weeks?|wks?|days?)\s+"
            r"(?:of\s+age(?:\s+or\s+(?:older|younger))?|old(?:\s+or\s+(?:older|younger))?|and\s+(?:older|above|younger))"
            r"|(?:adults?|men|women|males?|females?|patients?|participants?|subjects?|individuals?|children|adolescents?)\s+"
            r"(?:between\s+)?\d+(?:\.\d+)?\s*(?:to|through|and|-|–|—)\s*\d+(?:\.\d+)?\s*(?:years?|yrs?|months?)"
            r")\b"
        ),
        guard_not_pattern=None,
        rationale="Chronological participant age lower and/or upper cutoffs in Inclusion or Exclusion text.",
    ),
)


def validate_rules(rules: Sequence[DomainRule]) -> None:
    """Validate rule schema invariants: unique IDs, canonical domains/effects, non-empty rationales, valid regexes."""
    seen_ids: set[str] = set()
    domain_set = set(DOMAINS)
    effect_set = set(RULE_EFFECTS)
    for r in rules:
        if not r.rule_id or not r.rule_id.strip():
            raise ValueError("Rule has empty rule_id")
        if r.rule_id in seen_ids:
            raise ValueError(f"Duplicate rule_id in vocabulary: {r.rule_id!r}")
        seen_ids.add(r.rule_id)
        if r.domain not in domain_set:
            raise ValueError(f"Rule {r.rule_id!r} references unknown domain {r.domain!r}")
        if r.effect not in effect_set:
            raise ValueError(f"Rule {r.rule_id!r} references unknown effect {r.effect!r}")
        if not r.applicable_sections or not set(r.applicable_sections).issubset(VALID_APPLICABLE_SECTIONS):
            raise ValueError(f"Rule {r.rule_id!r} has invalid applicable_sections {r.applicable_sections!r}")
        if not r.rationale or not r.rationale.strip():
            raise ValueError(f"Rule {r.rule_id!r} is missing a non-empty rationale")
        re.compile(r.pattern, re.IGNORECASE)
        if r.guard_not_pattern is not None:
            re.compile(r.guard_not_pattern, re.IGNORECASE)
    covered = {r.domain for r in rules}
    missing = domain_set - covered
    if missing:
        raise ValueError(f"Domains missing from vocabulary rules: {sorted(missing)}")


def compute_vocabulary_sha256(
    rules: Sequence[DomainRule] = RULES,
    modifier_patterns: dict[str, str] | None = None,
    threshold_patterns: dict[str, str] | None = None,
) -> str:
    """Compute the deterministic SHA-256 digest of the canonical rule and pattern tables."""
    mods = MODIFIER_PATTERNS if modifier_patterns is None else modifier_patterns
    thresh = THRESHOLD_PATTERNS if threshold_patterns is None else threshold_patterns
    payload = {
        "domains": list(DOMAINS),
        "modifier_patterns": {k: mods[k] for k in sorted(mods)},
        "rules": [r.canonical_dict() for r in rules],
        "threshold_patterns": {k: thresh[k] for k in sorted(thresh)},
        "vocabulary_version": VOCABULARY_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


VOCABULARY_SHA256: str = compute_vocabulary_sha256()

EXPECTED_VOCABULARY_SHA256: str = "250da05fcd443796d35493c3491def64fd84853d598eb6d5c6ce660cc763554d"


def verify_vocabulary_pin() -> None:
    """Raise RuntimeError if `VOCABULARY_SHA256` drifts from `EXPECTED_VOCABULARY_SHA256`."""
    validate_rules(RULES)
    actual = compute_vocabulary_sha256()
    if actual != EXPECTED_VOCABULARY_SHA256:
        raise RuntimeError(
            f"Eligibility vocabulary SHA-256 drift: computed {actual}, "
            f"expected {EXPECTED_VOCABULARY_SHA256}. Update EXPECTED_VOCABULARY_SHA256 "
            f"and regression tests together when deliberately changing rules."
        )


verify_vocabulary_pin()


# Pre-compile rule and helper regexes for fast deterministic evaluation.
@dataclass(frozen=True)
class CompiledRule:
    """Pre-compiled wrapper around a `DomainRule`."""

    spec: DomainRule
    regex: re.Pattern[str]
    guard_not_regex: re.Pattern[str] | None


COMPILED_RULES_BY_DOMAIN: dict[str, tuple[CompiledRule, ...]] = {
    dom: tuple(
        CompiledRule(
            spec=r,
            regex=re.compile(r.pattern, re.IGNORECASE),
            guard_not_regex=(
                re.compile(r.guard_not_pattern, re.IGNORECASE)
                if r.guard_not_pattern is not None
                else None
            ),
        )
        for r in RULES
        if r.domain == dom
    )
    for dom in DOMAINS
}

COMPILED_MODIFIERS: dict[str, re.Pattern[str]] = {
    k: re.compile(v, re.IGNORECASE) for k, v in MODIFIER_PATTERNS.items()
}

COMPILED_THRESHOLDS: dict[str, re.Pattern[str]] = {
    k: re.compile(v, re.IGNORECASE) for k, v in THRESHOLD_PATTERNS.items()
}


# ---------------------------------------------------------------------------
# Deterministic Domain-Specific Numeric & Exception Extractors
# ---------------------------------------------------------------------------

_UNIT_TO_YEARS: dict[str, float] = {
    "year": 1.0,
    "years": 1.0,
    "yr": 1.0,
    "yrs": 1.0,
    "month": 1.0 / 12.0,
    "months": 1.0 / 12.0,
    "mo": 1.0 / 12.0,
    "mos": 1.0 / 12.0,
    "week": 1.0 / 52.0,
    "weeks": 1.0 / 52.0,
    "wk": 1.0 / 52.0,
    "wks": 1.0 / 52.0,
    "day": 1.0 / 365.0,
    "days": 1.0 / 365.0,
    "hour": 1.0 / 8760.0,
    "hours": 1.0 / 8760.0,
}

_ROMAN_TO_INT: dict[str, int] = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _finite_float(val_str: str) -> float | None:
    """Parse a finite float; return None on overflow (`inf`) or `NaN`."""
    try:
        v = float(val_str)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_structured_age_years(age_str: str | None) -> float | None:
    """Convert a structured CT.gov age string (e.g. `'18 Years'`, `'6 Months'`, `'N/A'`) to float years."""
    if age_str is None:
        return None
    if not isinstance(age_str, str):
        raise ValueError(f"Structured age must be str or None, got {type(age_str).__name__}")
    s = age_str.strip()
    if not s or s.upper() in ("N/A", "NA", "NONE"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", s)
    if not m:
        raise ValueError(f"Unreadable structured age string: {age_str!r}")
    unit = m.group(2).lower()
    if unit not in _UNIT_TO_YEARS:
        raise ValueError(f"Unsupported structured age unit in {age_str!r}")
    fv = _finite_float(m.group(1))
    if fv is None:
        raise ValueError(f"Non-finite structured age in {age_str!r}")
    return fv * _UNIT_TO_YEARS[unit]


def _age_to_years(value_str: str, unit_str: str | None) -> float:
    val = _finite_float(value_str)
    if val is None:
        return 999.0
    if not unit_str:
        return val
    u = unit_str.strip().lower()
    factor = _UNIT_TO_YEARS.get(u, 1.0)
    return round(val * factor, 6)


# Age sub-patterns for exact polarity inversion between Inclusion and Exclusion.
_AGE_RANGE_RX = re.compile(
    r"\b(?:"
    r"age[ds]?\s*(?::|=|is|of)?\s*(?:between\s+)?"
    r"(?P<lo1>\d+(?:\.\d+)?)\s*(?P<u1>years?|yrs?|months?|mos?|weeks?|wks?|days?)?\s*"
    r"(?:of\s+age\s*)?(?:to|through|and|-|–|—)\s*"
    r"(?P<hi1>\d+(?:\.\d+)?)\s*(?P<u2>years?|yrs?|months?|mos?|weeks?|wks?|days?)?"
    r"(?:\s*(?:of\s+age|old|inclusive))?"
    r"|(?:between\s+)?(?P<lo2>\d+(?:\.\d+)?)\s*(?P<u3>years?|yrs?|months?|mos?|weeks?|wks?|days?)?\s*"
    r"(?:to|through|and|-|–|—)\s*(?P<hi2>\d+(?:\.\d+)?)\s*"
    r"(?P<u4>years?|yrs?|months?|mos?|weeks?|wks?|days?)\s+(?:of\s+age|old)"
    r"|(?:adults?|men|women|males?|females?|patients?|participants?|subjects?|individuals?|children|adolescents?|infants?)\s+"
    r"(?:who\s+are\s+)?(?:aged\s+|between\s+)?(?P<lo3>\d+(?:\.\d+)?)\s*(?:to|through|and|-|–|—)\s*"
    r"(?P<hi3>\d+(?:\.\d+)?)\s*(?P<u5>years?|yrs?|months?|mos?)(?:\s+(?:of\s+age|old))?"
    r")\b",
    re.IGNORECASE,
)

_AGE_DUAL_INEQ_RX = re.compile(
    r"\b(?:age[ds]?|patients?|participants?|subjects?|individuals?)?\s*"
    r"(?P<op1><=|≤|<|under|younger\s+than|less\s+than|below|>=|≥|>|at\s+least|over|above|older\s+than|greater\s+than|more\s+than)\s*"
    r"(?P<v1>\d+(?:\.\d+)?)\s*(?P<u1>years?|yrs?|months?|mos?|weeks?|wks?|days?)?"
    r"(?:\s*(?:of\s+age|old))?\s*(?P<conj>or|and|,)\s*"
    r"(?:(?:patients?|participants?|subjects?|individuals?)\s+)?(?:age[ds]?\s*(?:is\s+|of\s+|:|=)?\s*)?"
    r"(?P<op2>>=|≥|>|over|above|older\s+than|greater\s+than|more\s+than|<=|≤|<|no\s+more\s+than|up\s+to|under|younger\s+than|less\s+than)\s*"
    r"(?P<v2>\d+(?:\.\d+)?)\s*(?P<u2>years?|yrs?|months?|mos?|weeks?|wks?|days?)?"
    r"(?:\s*(?:of\s+age|old))?\b",
    re.IGNORECASE,
)

_AGE_SINGLE_COMP_RX = re.compile(
    r"\b(?:"
    r"age[ds]?\s*(?::|=|is|of)?\s*"
    r"(?P<op1>>=|≥|<=|≤|>|<|at\s+least|no\s+more\s+than|no\s+older\s+than|not\s+more\s+than|up\s+to|"
    r"under|over|above|below|younger\s+than|older\s+than|less\s+than|greater\s+than)\s*"
    r"(?P<v1>\d+(?:\.\d+)?)\s*(?P<u1>years?|yrs?|months?|mos?|weeks?|wks?|days?)?(?:\s*(?:of\s+age|old))?"
    r"|(?P<op2>>=|≥|<=|≤|>|<|at\s+least|no\s+more\s+than|no\s+older\s+than|up\s+to|"
    r"under|over|above|below|younger\s+than|older\s+than|less\s+than|greater\s+than)\s*"
    r"(?P<v2>\d+(?:\.\d+)?)\s*(?P<u2>years?|yrs?|months?|mos?|weeks?|wks?|days?)\s+(?:of\s+age|old)"
    r"|(?P<v3>\d+(?:\.\d+)?)\s*(?P<u3>years?|yrs?|months?|mos?|weeks?|wks?|days?)\s+"
    r"(?:(?:of\s+age|old)\s+(?P<dir3a>(?:or|and)\s+(?:older|above|over|younger|under|below))|"
    r"(?P<dir3b>(?:or|and)\s+(?:older|younger)))"
    r")\b",
    re.IGNORECASE,
)


def _is_false_age_window(text: str, start: int, end: int) -> bool:
    """Reject non-chronological age phrases ('gestational age', 'within 5 years', 'partner aged', 'onset at age')."""
    ctx_start = max(0, start - 50)
    ctx_end = min(len(text), end + 35)
    window = text[ctx_start:ctx_end]
    if COMPILED_MODIFIERS["age_false_positive_context"].search(window):
        return True
    span_str = text[start:end]
    if not re.search(r"\b(?:age[ds]?|old)\b", span_str, re.IGNORECASE):
        if COMPILED_MODIFIERS["duration_lookback_false_positive"].search(window):
            return True
    suffix = text[end : min(len(text), end + 25)]
    if re.match(r"^\s*(?:%|mg\b|ml\b|g/dl|uln\b|mmhg\b|ms\b|msec\b|pack|(?:of\s+)?gestation\b)", suffix, re.IGNORECASE):
        return True
    return False


def tighten_lower_bound(
    cur_lo: float | None, cur_inc: bool | None, cand_lo: float, cand_inc: bool
) -> tuple[float, bool]:
    """Intersect lower bounds `(cur_lo, cur_inc)` and `(cand_lo, cand_inc)` preserving exact inclusivity."""
    if cur_lo is None or cand_lo > cur_lo:
        return cand_lo, cand_inc
    if cand_lo == cur_lo:
        return cur_lo, bool(cur_inc) and cand_inc
    return cur_lo, bool(cur_inc)


def tighten_upper_bound(
    cur_hi: float | None, cur_inc: bool | None, cand_hi: float, cand_inc: bool
) -> tuple[float, bool]:
    """Intersect upper bounds `(cur_hi, cur_inc)` and `(cand_hi, cand_inc)` preserving exact inclusivity."""
    if cur_hi is None or cand_hi < cur_hi:
        return cand_hi, cand_inc
    if cand_hi == cur_hi:
        return cur_hi, bool(cur_inc) and cand_inc
    return cur_hi, bool(cur_inc)


def _op_is_lower_dir(op: str) -> bool:
    norm = re.sub(r"\s+", " ", op.strip().lower())
    return norm in (">=", "≥", ">", "at least", "over", "above", "older than", "greater than", "more than") or (
        "older" in norm or "above" in norm or "over" in norm
    )


def _op_is_upper_dir(op: str) -> bool:
    norm = re.sub(r"\s+", " ", op.strip().lower())
    return norm in (
        "<=",
        "≤",
        "<",
        "under",
        "below",
        "younger than",
        "less than",
        "no more than",
        "no older than",
        "not more than",
        "up to",
    ) or ("younger" in norm or "under" in norm or "below" in norm)


def _op_is_strict(op: str) -> bool:
    norm = re.sub(r"\s+", " ", op.strip().lower())
    return norm in ("<", ">", "under", "over", "above", "below", "younger than", "older than", "less than", "greater than")


_AGE_LOCAL_NEG_PREFIX_RX = re.compile(
    r"\b(?:(?:must|may|shall|should|can)\s+not\s+be|cannot\s+be|"
    r"not\s+(?:be\s+)?(?:aged?|under|over|younger|older|between)|"
    r"no\s+(?:patients?|participants?|subjects?|individuals?)\s+(?:aged?|under|over|younger|older))\s*$",
    re.IGNORECASE,
)

_AGE_LOCAL_NEG_SUFFIX_RX = re.compile(
    r"^\s*(?:(?:are|is|will\s+be)\s+(?:excluded|ineligible)|not\s+eligible)\b",
    re.IGNORECASE,
)


def _age_match_polarity(clause_text: str, base_kind: str, start: int, end: int) -> str:
    """Resolve inclusion vs exclusion polarity locally around a chronological age match."""
    if base_kind == "exclusion":
        return "exclusion"
    pre_raw = clause_text[max(0, start - 40) : start]
    pre_seg = re.split(r"[(),;.]|\b(?:who|and|or)\b", pre_raw, flags=re.IGNORECASE)[-1]
    if _AGE_LOCAL_NEG_PREFIX_RX.search(pre_seg):
        return "exclusion"
    suf_raw = clause_text[end : min(len(clause_text), end + 35)]
    suf_seg = re.split(r"[(),;.]|\b(?:who|and)\b", suf_raw, flags=re.IGNORECASE)[0]
    if _AGE_LOCAL_NEG_SUFFIX_RX.search(suf_seg):
        return "exclusion"
    return "inclusion"


def extract_clause_age_bounds(clause_text: str, section_kind: str) -> dict[str, Any] | None:
    """Extract chronological participant age bounds from one clause, inverting Exclusion polarity.

    Returns `None` if the clause contains no chronological participant age bound.
    Otherwise returns a dict with:
      - `min_age_years`: `float | None`
      - `max_age_years`: `float | None`
      - `min_inclusive`: `bool | None`
      - `max_inclusive`: `bool | None`
      - `polarity_resolved`: `bool` (`False` when unheaded without explicit inclusion/exclusion verb)
    """
    effective_kind = section_kind
    target_text = clause_text
    if section_kind == "unheaded":
        # Evaluate sentence-level polarity when multiple sentences appear on one unheaded line.
        sentences = re.split(r"(?<=[.;])\s+", clause_text)
        age_sentences: list[str] = []
        for sent in sentences:
            for rx in (_AGE_DUAL_INEQ_RX, _AGE_RANGE_RX, _AGE_SINGLE_COMP_RX):
                if any(not _is_false_age_window(sent, m.start(), m.end()) for m in rx.finditer(sent)):
                    age_sentences.append(sent)
                    break
        if not age_sentences:
            return None
        target_text = " ".join(age_sentences)
        has_exc = bool(COMPILED_MODIFIERS["unheaded_explicit_exclusion"].search(target_text))
        has_inc = bool(COMPILED_MODIFIERS["unheaded_explicit_inclusion"].search(target_text))
        if has_exc and not has_inc:
            effective_kind = "exclusion"
        elif has_inc and not has_exc:
            effective_kind = "inclusion"
        else:
            return {
                "min_age_years": None,
                "max_age_years": None,
                "min_inclusive": None,
                "max_inclusive": None,
                "polarity_resolved": False,
            }

    clause_text = target_text

    min_age: float | None = None
    max_age: float | None = None
    min_inc: bool | None = None
    max_inc: bool | None = None
    matched = False

    # 1. Dual inequality first (e.g. "Exclusion: Age < 18 years or > 70 years" or "Inclusion: Age >= 18 and <= 70 years").
    for m in _AGE_DUAL_INEQ_RX.finditer(clause_text):
        if _is_false_age_window(clause_text, m.start(), m.end()):
            continue
        window_before = clause_text[max(0, m.start() - 20) : m.end()]
        if not re.search(r"\b(?:age[ds]?|old|younger|older)\b", window_before, re.IGNORECASE):
            continue
        polarity = _age_match_polarity(clause_text, effective_kind, m.start(), m.end())
        u2 = m.group("u2") or m.group("u1") or "years"
        u1 = m.group("u1") or u2
        val1 = _age_to_years(m.group("v1"), u1)
        val2 = _age_to_years(m.group("v2"), u2)
        op1 = m.group("op1")
        op2 = m.group("op2")

        for op, val in ((op1, val1), (op2, val2)):
            if val > 130.0:
                continue
            if polarity == "inclusion":
                if _op_is_lower_dir(op):
                    min_age, min_inc = tighten_lower_bound(min_age, min_inc, val, not _op_is_strict(op))
                    matched = True
                elif _op_is_upper_dir(op):
                    max_age, max_inc = tighten_upper_bound(max_age, max_inc, val, not _op_is_strict(op))
                    matched = True
            else:
                if _op_is_upper_dir(op):
                    min_age, min_inc = tighten_lower_bound(min_age, min_inc, val, _op_is_strict(op))
                    matched = True
                elif _op_is_lower_dir(op):
                    max_age, max_inc = tighten_upper_bound(max_age, max_inc, val, _op_is_strict(op))
                    matched = True
        if matched:
            return {
                "min_age_years": min_age,
                "max_age_years": max_age,
                "min_inclusive": min_inc,
                "max_inclusive": max_inc,
                "polarity_resolved": True,
            }

    # 2. Explicit inclusive range (e.g. "Inclusion: Age 18 to 70 years").
    for m in _AGE_RANGE_RX.finditer(clause_text):
        if _is_false_age_window(clause_text, m.start(), m.end()):
            continue
        polarity = _age_match_polarity(clause_text, effective_kind, m.start(), m.end())
        if m.group("lo1") is not None:
            lo_s, hi_s = m.group("lo1"), m.group("hi1")
            u_hi = m.group("u2") or m.group("u1") or "years"
            u_lo = m.group("u1") or u_hi
        elif m.group("lo2") is not None:
            lo_s, hi_s = m.group("lo2"), m.group("hi2")
            u_hi = m.group("u4") or m.group("u3") or "years"
            u_lo = m.group("u3") or u_hi
        else:
            lo_s, hi_s = m.group("lo3"), m.group("hi3")
            u_lo = u_hi = m.group("u5") or "years"

        lo_v = _age_to_years(lo_s, u_lo)
        hi_v = _age_to_years(hi_s, u_hi)
        if hi_v < lo_v or hi_v > 130.0:
            continue

        if polarity == "inclusion":
            return {
                "min_age_years": lo_v,
                "max_age_years": hi_v,
                "min_inclusive": True,
                "max_inclusive": True,
                "polarity_resolved": True,
            }
        if lo_v == 0.0 and hi_v < 25.0:
            next_val = hi_v + 1.0 if u_hi.lower().startswith(("year", "yr")) and hi_v.is_integer() else hi_v
            return {
                "min_age_years": next_val,
                "max_age_years": None,
                "min_inclusive": True,
                "max_inclusive": None,
                "polarity_resolved": True,
            }
        return {
            "min_age_years": None,
            "max_age_years": None,
            "min_inclusive": None,
            "max_inclusive": None,
            "polarity_resolved": False,
        }

    # 3. Single comparator(s) in the clause (e.g. "Inclusion: Age >= 18 years" or "Exclusion: Age > 70 years").
    for m in _AGE_SINGLE_COMP_RX.finditer(clause_text):
        if _is_false_age_window(clause_text, m.start(), m.end()):
            continue
        polarity = _age_match_polarity(clause_text, effective_kind, m.start(), m.end())
        if m.group("v1") is not None:
            op, v_s, u_s = m.group("op1"), m.group("v1"), m.group("u1") or "years"
        elif m.group("v2") is not None:
            op, v_s, u_s = m.group("op2"), m.group("v2"), m.group("u2") or "years"
        else:
            op = m.group("dir3a") or m.group("dir3b") or ""
            v_s, u_s = m.group("v3"), m.group("u3") or "years"

        val = _age_to_years(v_s, u_s)
        if val > 130.0:
            continue

        if polarity == "inclusion":
            if _op_is_lower_dir(op):
                min_age, min_inc = tighten_lower_bound(min_age, min_inc, val, not _op_is_strict(op))
                matched = True
            elif _op_is_upper_dir(op):
                max_age, max_inc = tighten_upper_bound(max_age, max_inc, val, not _op_is_strict(op))
                matched = True
        else:
            if _op_is_upper_dir(op):
                min_age, min_inc = tighten_lower_bound(min_age, min_inc, val, _op_is_strict(op))
                matched = True
            elif _op_is_lower_dir(op):
                max_age, max_inc = tighten_upper_bound(max_age, max_inc, val, _op_is_strict(op))
                matched = True

    if not matched:
        return None
    return {
        "min_age_years": min_age,
        "max_age_years": max_age,
        "min_inclusive": min_inc,
        "max_inclusive": max_inc,
        "polarity_resolved": True,
    }


_GILBERTS_NEGATED_RX = re.compile(
    r"\bgilbert(?:'?s|\u2019s)?(?:\s+(?:syndrome|disease))?[^\r\n.;]{0,55}"
    r"\b(?:(?:are|is)\s+(?:also\s+)?(?:excluded|ineligible)|not\s+(?:permitted|allowed|eligible|acceptable))\b"
    r"|\b(?:no\s+exception\s+for|;\s*excluding|also\s+exclud\w+)\s+[^\r\n.;()]{0,40}\bgilbert(?:'?s|\u2019s)?",
    re.IGNORECASE,
)

_GILBERTS_ALLOWANCE_CUE_RX = re.compile(
    r"\b(?:except|unless|other\s+than|(?<!not\s)(?<!never\s)(?:permitted|allowed|eligible|acceptable)|"
    r"note\s*:|(?<!no\s)exception\s*:|allowance\s*:)\b",
    re.IGNORECASE,
)


def is_gilberts_allowance_in_clause(clause_text: str, section_kind: str) -> bool:
    """Return True when Gilbert's syndrome is mentioned as a clinical allowance rather than an exclusion."""
    if not COMPILED_MODIFIERS["gilberts_syndrome"].search(clause_text):
        return False
    if _GILBERTS_NEGATED_RX.search(clause_text):
        return False
    if section_kind == "inclusion":
        return True
    if COMPILED_THRESHOLDS["gilberts_uln"].search(clause_text):
        return True
    return bool(_GILBERTS_ALLOWANCE_CUE_RX.search(clause_text))


def extract_domain_thresholds(domain: str, clause_text: str, section_kind: str) -> dict[str, Any]:
    """Extract structured numeric thresholds and exception metadata for `domain` from `clause_text`."""
    out: dict[str, Any] = {}

    if domain == "pregnancy_or_lactation":
        if re.search(r"\b(?:contracepti(?:on|ve)|birth\s+control|barrier\s+method|abstinen)\b", clause_text, re.IGNORECASE):
            out["contraception_mentioned"] = True
        return out

    if domain == "hiv_infection":
        m_cd4 = COMPILED_THRESHOLDS["hiv_cd4"].search(clause_text)
        if m_cd4:
            cd4_v = _finite_float(m_cd4.group("val").replace(",", ""))
            if cd4_v is not None and cd4_v <= 100000:
                out["cd4_min_cells_ul"] = int(cd4_v)
        if re.search(r"\bundetectable\s+(?:hiv\s+|plasma\s+)?viral\s+load\b", clause_text, re.IGNORECASE):
            out["requires_undetectable_viral_load"] = True
        return out

    if domain == "hepatitis_b_or_c":
        if re.search(r"\bhcv\s+rna\b[^\r\n.;]{0,30}\b(?:negative|undetectable)\b|\b(?:negative|undetectable)\s+hcv\s+rna\b", clause_text, re.IGNORECASE):
            out["hcv_rna_negative_allowed"] = True
        if re.search(r"\bhbv\s+dna\b[^\r\n.;]{0,30}\b(?:negative|undetectable|suppressed|<|<=|≤)\b|\b(?:undetectable|suppressed)\s+hbv\s+dna\b", clause_text, re.IGNORECASE):
            out["hbv_dna_suppressed_allowed"] = True
        return out

    if domain == "renal_function":
        m_egfr = COMPILED_THRESHOLDS["egfr_value"].search(clause_text)
        if m_egfr:
            v1 = _finite_float(m_egfr.group("v1"))
            v2_s = m_egfr.group("v2")
            v2 = _finite_float(v2_s) if v2_s is not None else None
            op = (m_egfr.group("op") or "").strip().lower()
            if v1 is not None:
                if v2 is not None and section_kind == "inclusion":
                    out["target_egfr_min_ml_min"] = v1
                    out["target_egfr_max_ml_min"] = v2
                elif section_kind == "inclusion" and op in ("<", "<=", "≤", "less than", "below") and v1 <= 60.0:
                    out["target_egfr_max_ml_min"] = v1
                else:
                    out["egfr_min_ml_min"] = v1
        m_crcl = COMPILED_THRESHOLDS["crcl_value"].search(clause_text)
        if m_crcl:
            v1 = _finite_float(m_crcl.group("v1"))
            v2_s = m_crcl.group("v2")
            v2 = _finite_float(v2_s) if v2_s is not None else None
            op = (m_crcl.group("op") or "").strip().lower()
            if v1 is not None:
                if v2 is not None and section_kind == "inclusion":
                    out["target_crcl_min_ml_min"] = v1
                    out["target_crcl_max_ml_min"] = v2
                elif section_kind == "inclusion" and op in ("<", "<=", "≤", "less than", "below") and v1 <= 60.0:
                    out["target_crcl_max_ml_min"] = v1
                else:
                    out["crcl_min_ml_min"] = v1
        m_cr_uln = COMPILED_THRESHOLDS["creatinine_uln"].search(clause_text)
        if m_cr_uln:
            cr_u = _finite_float(m_cr_uln.group("val"))
            if cr_u is not None:
                out["creatinine_max_uln"] = cr_u
        m_cr_mg = COMPILED_THRESHOLDS["creatinine_mg_dl"].search(clause_text)
        if m_cr_mg:
            cr_m = _finite_float(m_cr_mg.group("val"))
            if cr_m is not None:
                out["creatinine_max_mg_dl"] = cr_m
        return out

    if domain == "hepatic_function":
        for m_enz in COMPILED_THRESHOLDS["ast_alt_uln"].finditer(clause_text):
            analyte = m_enz.group("analyte").lower()
            val = _finite_float(m_enz.group("val"))
            if val is None:
                continue
            raw_tail = clause_text[m_enz.end() : min(len(clause_text), m_enz.end() + 55)]
            tail = raw_tail.split("(")[0]
            raw_head = clause_text[max(0, m_enz.start() - 55) : m_enz.start()]
            head = raw_head.rsplit(")", 1)[-1]
            is_liver_mets = bool(
                re.search(r"\b(?:liver|hepatic)\s+(?:metastas[ei]s|involvement)\b", head + " " + tail, re.IGNORECASE)
            )
            has_ast = any(k in analyte for k in ("ast", "sgot", "transaminase", "aminotransferase"))
            has_alt = any(k in analyte for k in ("alt", "sgpt", "transaminase", "aminotransferase"))
            if is_liver_mets:
                if has_ast:
                    out["ast_liver_mets_max_uln"] = val
                if has_alt:
                    out["alt_liver_mets_max_uln"] = val
            else:
                if has_ast and "ast_max_uln" not in out:
                    out["ast_max_uln"] = val
                if has_alt and "alt_max_uln" not in out:
                    out["alt_max_uln"] = val
                m_lm = re.search(
                    r"\(\s*(?:<=|≤|<|up\s+to)?\s*(?P<lm_val>\d+(?:\.\d+)?)\s*(?:x|×|\*|times)\s*(?:the\s+)?uln"
                    r"[^\r\n()]{0,60}?\b(?:liver|hepatic)\s+(?:metastas[ei]s|involvement)",
                    clause_text[m_enz.end() :],
                    re.IGNORECASE,
                )
                if m_lm:
                    lm_v = _finite_float(m_lm.group("lm_val"))
                    if lm_v is not None:
                        if has_ast:
                            out["ast_liver_mets_max_uln"] = lm_v
                        if has_alt:
                            out["alt_liver_mets_max_uln"] = lm_v

        m_bili = COMPILED_THRESHOLDS["bilirubin_uln"].search(clause_text)
        if m_bili:
            bv = _finite_float(m_bili.group("val"))
            if bv is not None:
                out["bilirubin_max_uln"] = bv
        m_bili_mg = COMPILED_THRESHOLDS["bilirubin_mg_dl"].search(clause_text)
        if m_bili_mg:
            bm = _finite_float(m_bili_mg.group("val"))
            if bm is not None:
                out["bilirubin_max_mg_dl"] = bm
        if is_gilberts_allowance_in_clause(clause_text, section_kind):
            out["has_gilberts_exception"] = True
            m_g = COMPILED_THRESHOLDS["gilberts_uln"].search(clause_text)
            if m_g:
                gv = m_g.group("v1") or m_g.group("v2")
                gvf = _finite_float(gv) if gv is not None else None
                if gvf is not None:
                    out["gilberts_bilirubin_max_uln"] = gvf
        return out

    if domain == "cardiac_function":
        m_lvef = COMPILED_THRESHOLDS["lvef_pct"].search(clause_text)
        if m_lvef:
            val = _finite_float(m_lvef.group("val"))
            op = m_lvef.group("op").strip().lower()
            if val is not None:
                if section_kind == "inclusion" and op in ("<=", "≤", "<", "less than", "below") and val <= 45.0:
                    out["target_lvef_max_pct"] = val
                else:
                    out["lvef_min_pct"] = val
        qtc_vals: list[float] = [
            qv
            for m in COMPILED_THRESHOLDS["qtc_ms"].finditer(clause_text)
            if (qv := _finite_float(m.group("val"))) is not None
        ]
        if qtc_vals:
            out["qtc_max_ms"] = max(qtc_vals)
        m_nyha = COMPILED_THRESHOLDS["nyha_class"].search(clause_text)
        if m_nyha and section_kind == "exclusion":
            spec = m_nyha.group("spec").lower()
            nums = [_ROMAN_TO_INT[t] for t in re.findall(r"\b(?:iv|iii|ii|i|[1234])\b", spec) if t in _ROMAN_TO_INT]
            if nums and min(nums) >= 2:
                out["nyha_max_allowed"] = min(nums) - 1
        return out

    if domain == "performance_status":
        m_ecog = COMPILED_THRESHOLDS["ecog_score"].search(clause_text)
        if m_ecog:
            spec = re.sub(r"\s+", " ", m_ecog.group("spec").strip().lower())
            digits = [int(d) for d in re.findall(r"[0-4]", spec)]
            if section_kind == "inclusion":
                if spec.startswith("<") and not spec.startswith("<="):
                    out["ecog_max_allowed"] = max(0, digits[0] - 1)
                elif spec.startswith((">=", "≥")) and digits[0] >= 2:
                    out["target_ecog_min"] = digits[0]
                elif digits:
                    if min(digits) >= 2:
                        out["target_ecog_min"] = min(digits)
                    out["ecog_max_allowed"] = max(digits)
            elif section_kind == "exclusion" and digits:
                if spec.startswith(">") and not spec.startswith(">="):
                    out["ecog_max_allowed"] = digits[0]
                else:
                    min_exc = min(digits)
                    if min_exc >= 1:
                        out["ecog_max_allowed"] = min_exc - 1
        m_kps = COMPILED_THRESHOLDS["karnofsky_score"].search(clause_text)
        if m_kps:
            kv = _finite_float(m_kps.group("val"))
            if kv is not None:
                out["karnofsky_min_pct"] = kv
        m_lan = COMPILED_THRESHOLDS["lansky_score"].search(clause_text)
        if m_lan:
            lv = _finite_float(m_lan.group("val"))
            if lv is not None:
                out["lansky_min_pct"] = lv
        return out

    if domain == "prior_malignancy":
        m_lb = COMPILED_THRESHOLDS["malignancy_lookback"].search(clause_text)
        if m_lb:
            val = _finite_float(m_lb.group("val"))
            if val is not None:
                unit = m_lb.group("unit").lower()
                out["lookback_years"] = round(val / 12.0, 4) if unit.startswith("mo") else val
        if COMPILED_MODIFIERS["skin_or_insitu_exception"].search(clause_text) and COMPILED_MODIFIERS[
            "general_exception_cue"
        ].search(clause_text):
            out["has_skin_or_insitu_exception"] = True
        return out

    if domain == "language_or_literacy":
        langs: list[str] = []
        for m_l in COMPILED_THRESHOLDS["named_languages"].finditer(clause_text):
            canon = re.sub(r"\s+", " ", m_l.group("lang")).title()
            if canon not in langs:
                langs.append(canon)
        if langs:
            out["languages"] = langs
        if re.search(r"\b(?:interpreter|translator|translated\s+(?:informed\s+)?consent|short[- ]?form|impartial\s+witness)\b", clause_text, re.IGNORECASE) and not re.search(r"\b(?:no\s+interpreter|without\s+(?:an?\s+)?interpreter|not\s+(?:permitted|allowed))\b", clause_text, re.IGNORECASE):
            out["accommodation"] = "interpreter_or_translation"
        elif len(langs) >= 2:
            out["accommodation"] = "multilingual"
        return out

    if domain == "free_text_age_cutoff":
        bounds = extract_clause_age_bounds(clause_text, section_kind)
        if bounds is not None:
            out.update(bounds)
        return out

    return out
