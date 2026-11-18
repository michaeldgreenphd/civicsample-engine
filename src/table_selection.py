"""
Choosing which baseline tables to count when a trial posts several for one
demographic dimension.

Shared by the race, ethnicity and legacy sex/gender extractors. A trial often
posts the same people more than once: a "Race (NIH/OMB)" table and a
customized re-post, an "Ethnicity (NIH/OMB)" table beside a race table titled
"Race/Ethnicity, Customized" whose only ethnicity-like row is "Unknown", or a
"Sex: Female, Male" table beside a "Sex/Gender, Customized" table. Adding those
tables together counts people twice. The rule here follows the sex/gender
manuscript parser (sex_gender_parser.parse_trial), which never adds duplicate
tables and counts the ONE fullest measure, with one exception for tables the
registry itself marks as covering different people.

Each extractor reads its tables into dicts with
    "categories"  the counted category records, each with the extractor's
                  category field (``key``), "count" and, where the extractor
                  has them, "subcategory";
    "analyzed"    the table's own Number Analyzed (number_analyzed), or None.
and names its "specific" categories: those that say something about a
participant (race categories, Hispanic / Not Hispanic, female / male). The
residual categories (unknown, other) are left out of "fullest", as the parser
leaves unknown out of female + male.

select_tables applies:
  2. One table is counted: the fullest of those that cover the whole baseline
     population. Fullest means the largest specific sum; on equal sums, the one
     that counts more participants in all; then the first in source order,
     except that of two tables with the same count in every category the one
     with more subcategory detail is kept. A table with no specific count never
     wins over one with any.
  3. Disjoint parts are summed only when the caller allows it, every table
     (all tables in play, residual-only ones included; else every table
     with specific counts) posts its own Number Analyzed below the baseline
     total, those numbers add up to EXACTLY the baseline total, no table
     counts more participants than its own Number Analyzed, and no two
     tables are identical (the same Number Analyzed, the same count in every
     category and the same population description: a re-post of one subset,
     not two matching halves). A smaller sum is
     no evidence of different people: the same 20 of 50 participants posted
     twice also add up to less than 50.
(Rule 1, which titles qualify and which take precedence, is each extractor's.)
Every table not counted is the caller's to flag as superseded. The before and
after counts of each extractor's adoption are in its own block comment.
"""
from typing import Dict, FrozenSet, List, Optional, Tuple

# ── A Total class beside subgroup classes ───────────────────────────────────
#
# Some registrants post one baseline measure as a cross-tabulation: subgroup
# classes (age bands, sites, study parts) that each hold the measure's rows,
# plus a class titled "Total" that holds the same rows again for everyone.
# NCT04911296 (150 participants, one baseline group) posts
# "Race/Ethnicity, Customized" with the classes "21 - 50 years",
# "51 - 65 years", "66 - 75 years" and "Total", each with the rows
# Black - Non-Hispanic / American Indian/Alaskan Native / Hispanic /
# Asian/Pacific Islander / White - Non-Hispanic; Hispanic is 32, 28, 24 and
# Total 84. The extractors read every row of every class, so each participant
# was counted twice: Hispanic 168 of 150. Its "Sex: Female, Male" measure has
# the same shape (Female 29 + 28 + 28 + Total 85).
#
# This is separate from the undocumented fact that the baseline Total is the
# highest-numbered BG *group* (utils.get_overall_group_id), which picks one
# measurement per row. A Total *class* is a set of rows inside one measure,
# and choosing a group does not remove it.
#
# Method (total_class_view). A measure is read from its Total class alone
# when all of these hold:
#   - exactly one class has a whole-population title, "Total", "Overall" or
#     "All participants" (WHOLE_POPULATION_CLASS_TITLES, trimmed,
#     case-insensitive), and there is at least one other class. "Total class"
#     below means that class, whichever of the three titles it has;
#   - the Total class holds at least one labelled row: a category whose title
#     is not a measurement label. A class titled "Total" whose only category
#     is "Count" is a row named Total, not a Total class;
#   - every labelled row of the Total class posts a number in the
#     measurement the extractors read: the Overall group's, else the single
#     or last measurement (utils.measurement_number, the value
#     utils.sum_measurements counts). The registry posts rows with no
#     measurements (template rows, an empty class), null values, and values
#     that are not numbers; in a multi-arm study one arm may post a value
#     while the Overall group's is null. sum_measurements reads each of those
#     as 0, so a Total row without a number would read as 0 while the
#     subgroup rows that hold the count were discarded. Such a measure is
#     read as before: the rule does not repair a measure whose Total class
#     cannot be read, it only declines to drop the rows that hold the count;
#   - every other class holds at least one labelled row, and each of its row
#     labels is one of the Total class's row labels (exact text, trimmed). A
#     subgroup may leave a row out (a band with no Asian participants may
#     post no Asian row); it may not add one.
# The measure is then read as if the Total class were its only class: the
# same row-label rules and the same overall group, and number_analyzed and
# percentage_denominator see the Total class alone, never the subgroup
# classes. percentage_denominator keeps its own rules there: the Total
# class's Number Analyzed when that class holds two or more rows, the
# baseline total for a one-row Total class, as for any one-row table (a
# lone row's class number is as likely its own count as a denominator).
# Otherwise the measure is read exactly as before.
#
# Why the Total class rather than the subgroups. When the subgroup rows add
# up to the Total rows, the two readings give the same counts, so the choice
# matters only when they do not. Then the Total is what the registrant states
# for the whole population, while the subgroups are a breakdown whose classes
# may overlap or leave people out (NCT03284307 below). Reading the subgroups
# only when they add up to the Total needs a fallback when they do not, and
# the only fallback that counts no one twice is the Total class, so that rule
# gives the same counts as this one with an extra sum check. Reading the Total
# class also keeps the denominator of a percentage measure with two or more
# rows: the subgroups' class-level Number Analyzed differ from one another, so
# percentage_denominator would otherwise fall back to the baseline total. (No
# measure the rule applies to in the measured snapshot reports percentages.)
#
# What it does not do. If a subgroup class has a row label the Total class
# lacks, the measure is read as before (every class summed): the classes are
# then not one breakdown of the Total's rows, and the labels alone cannot
# show which reading is right. Other titles are not read as a whole
# population: the one class titled "All" in the measured snapshot is a row
# of its own (NCT05216835: Female 14, Male 27, All 4). The breakdowns
# of utils.extract_demographic_breakdown (raceBreakdown, ethnicityBreakdown,
# sexBreakdown), which already sum every table of a type and which the site
# does not read, are not changed; neither is the vendored sex/gender parser,
# which reads only the first class of such a measure
# (flag_multiclass_timepoint): the Total when it is posted first, a single
# subgroup otherwise (NCT04911296: Female 29, Male 21, the first age band).
#
# The race, ethnicity and legacy sex and gender extractors read every
# measure through total_class_view. Each row read from a Total class carries
# the row flag "total_class_only" (TOTAL_CLASS_FLAG), and each block that
# counts such a row carries it in its own flags.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced
# 80,987 of their published race and legacy sex blocks). Race, ethnicity or
# sex measures with a Total class beside other classes: 8 trials. In 7 the
# subgroups repeat the Total's rows and the rule applies; in the eighth
# (NCT01696435) the class titled "Total" is a row, already quarantined by race
# and dropped by ethnicity. Of the 7, the subgroups add up to the Total in 5,
# overlap in 1 (NCT03284307, a crossover: 56 subgroup places for 27
# participants) and are off by one participant in 1 (NCT06027593). Blocks
# changed: race 6, ethnicity 4, legacy sex 7, legacy gender 0; none changes
# reported. Every changed block now sums to its baseline total. Trials whose
# block summed above the baseline total: race 397 -> 392, ethnicity
# 135 -> 131, legacy sex 279 -> 272.
#
# "Overall" and "All participants" were added to the titles after that, on
# the same inputs. 12 trials have a race, ethnicity or sex measure with such
# a class beside others whose rows it repeats (race 8, ethnicity 8, sex 12).
# In every one, the whole-population class holds exactly the baseline total.
# The other classes are not always a breakdown: they add up to it in 5
# trials ("Adolescents" / "Caregivers", NCT04268004), are subsets of it in 5
# ("Completers", NCT03621280; propensity-matched populations, NCT02785354)
# and overlap in 2 ("Global cohort" / "China cohort", NCT02542293). Summing
# any of them with the whole class counts people twice; the whole class alone
# counts each participant once in all 12. Blocks changed by the two added
# titles: race 8, ethnicity 7 (NCT03026556's ethnicity block is not reported:
# its "Race/Ethnicity, Customized" table is a race-only table), legacy sex 12,
# legacy gender 0; none changes reported, and each now sums to its baseline
# total. Trials whose block summed above the baseline total, from the
# "Total"-only rule to all three titles: race 392 -> 384, ethnicity
# 131 -> 124, legacy sex 272 -> 260.
TOTAL_CLASS_FLAG = "total_class_only"

# Class titles that name the whole baseline population of a measure (block
# comment above), compared trimmed and lower-cased.
WHOLE_POPULATION_CLASS_TITLES = frozenset({"total", "overall", "all participants"})

# Category titles that are measurement labels, not row labels: the row's label
# is then its class title (the same set each extractor keeps as
# _MEASUREMENT_LABELS).
_MEASUREMENT_LABELS = frozenset({"count", "number", "n", "total", "value", "mean", "median"})


def _labelled_rows(cls: dict) -> List[dict]:
    """A class's labelled rows: categories whose trimmed title is not a
    measurement label."""
    rows = []
    for cat in cls.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        title = (cat.get("title") or "").strip()
        if title and title.lower() not in _MEASUREMENT_LABELS:
            rows.append(cat)
    return rows


def _row_labels(cls: dict) -> List[str]:
    """A class's labelled row titles, trimmed."""
    return [(cat.get("title") or "").strip() for cat in _labelled_rows(cls)]


def _posts_a_number(row: dict, overall_group_id: Optional[str]) -> bool:
    """Whether the measurement the extractors read from this row holds a
    number (utils.measurement_number), as opposed to nothing, a null or a
    value that sum_measurements would count as 0 for want of a number."""
    from src.utils import measurement_number

    measurements = row.get("measurements")
    if not isinstance(measurements, list):
        return False
    return measurement_number(measurements, overall_group_id) is not None


def total_class_view(measure: dict, overall_group_id: Optional[str]) -> Tuple[dict, bool]:
    """The measure to read, and whether it was reduced to its Total class
    (block comment above). overall_group_id is the group the extractors read
    (utils.get_overall_group_id). The input is never modified; when the rule
    applies, a shallow copy whose "classes" holds the Total class alone is
    returned."""
    classes = [cls for cls in measure.get("classes") or [] if isinstance(cls, dict)]
    if len(classes) < 2:
        return measure, False
    totals = [cls for cls in classes
              if (cls.get("title") or "").strip().lower() in WHOLE_POPULATION_CLASS_TITLES]
    if len(totals) != 1:
        return measure, False
    total_rows = _labelled_rows(totals[0])
    if not total_rows or not all(_posts_a_number(row, overall_group_id) for row in total_rows):
        return measure, False
    total_labels = set(_row_labels(totals[0]))
    for cls in classes:
        if cls is totals[0]:
            continue
        labels = _row_labels(cls)
        if not labels or not set(labels) <= total_labels:
            return measure, False
    return {**measure, "classes": [totals[0]]}, True


def number_analyzed(measure: dict, overall_group_id: Optional[str]) -> Optional[int]:
    """A table's own Number Analyzed, in participants, or None if it posts none.

    Registry schema fact (API v2): a baseline measure analyzed on fewer people
    than the baseline population posts its own "Number Analyzed", at the
    measure level (measure["denoms"]) or, as the registry returns most of them,
    on its classes (classes[]["denoms"]). A measure with neither was analyzed on
    the whole baseline population. The overall value is read exactly as
    get_measure_denom reads it (Overall group, else the single or last count;
    units "Participants" only). If classes post different numbers, the largest
    is taken, which can only make the table look whole and so never leads to
    summing on unclear evidence.
    """
    from src.utils import get_measure_denom

    # A measure read from its Total class alone is analyzed on that class's
    # people (block comment "A Total class beside subgroup classes").
    measure, _total_only = total_class_view(measure, overall_group_id)
    n = get_measure_denom(measure, overall_group_id)
    if n is not None:
        return n
    per_class = [get_measure_denom({"denoms": cls.get("denoms") or []}, overall_group_id)
                 for cls in measure.get("classes", []) if isinstance(cls, dict)]
    per_class = [v for v in per_class if v is not None]
    return max(per_class) if per_class else None


def population_of(measure: dict) -> Optional[str]:
    """The measure's population description, whitespace- and case-normalized,
    or None. Registrants describe a table analyzed on part of the population
    here ("Data are for mothers only"), which tells two otherwise identical
    part tables apart."""
    text = " ".join(str(measure.get("populationDescription") or "").split()).lower()
    return text or None


def specific_sum(table: Dict, specific: FrozenSet[str], key: str) -> int:
    """Participants a table places in the specific categories."""
    return sum(c["count"] for c in table["categories"] if c[key] in specific)


def counted_sum(table: Dict) -> int:
    """Participants a table counts in any category."""
    return sum(c["count"] for c in table["categories"])


def category_counts(table: Dict, key: str) -> Dict[str, int]:
    """A table's counts by category (categories it counts nobody in left out)."""
    out: Dict[str, int] = {}
    for c in table["categories"]:
        if c["count"]:
            out[c[key]] = out.get(c[key], 0) + c["count"]
    return out


def subcategory_sum(table: Dict) -> int:
    """Participants a table places in a granular subcategory."""
    return sum(c["count"] for c in table["categories"] if c.get("subcategory"))


def fullest(tables: List[Dict], specific: FrozenSet[str], key: str) -> Dict:
    """Rule 2's table: largest specific sum, then largest counted sum, then
    the first in source order. One exception to source order: a tied table
    with the same count in every category (the same people in the same
    categories, e.g. "Asian" 25 re-posted as "Chinese" 25) replaces the leader
    when it places more of them in a subcategory, so choosing between re-posts
    never discards detail. A tie with any category count different ("Unknown"
    8 in one table, "Other" 8 in the other) stays with the first."""
    best = tables[0]
    for table in tables[1:]:
        k = (specific_sum(table, specific, key), counted_sum(table))
        best_k = (specific_sum(best, specific, key), counted_sum(best))
        if k > best_k:
            best = table
        elif (k == best_k and category_counts(table, key) == category_counts(best, key)
              and subcategory_sum(table) > subcategory_sum(best)):
            best = table
    return best


def select_tables(tables: List[Dict], total_participants: Optional[int],
                  specific: FrozenSet[str], key: str,
                  allow_parts: bool) -> Tuple[List[Dict], bool]:
    """The tables to count, and whether they were summed as disjoint parts
    (rules 2 and 3 above)."""
    if len(tables) <= 1:
        return tables, False
    if allow_parts and total_participants is not None and _parts(tables, total_participants, key):
        # Every table is a proven part, residual-only ones included (a part
        # whose only row is "Unknown" is still that part's people).
        return tables, True
    informative = [t for t in tables if specific_sum(t, specific, key) > 0]
    if not informative:
        return [fullest(tables, specific, key)], False
    if len(informative) == 1:
        return informative, False  # the only table with specific counts; the rest add residuals only
    if total_participants is None:
        return [fullest(informative, specific, key)], False
    whole = [t for t in informative if t["analyzed"] is None or t["analyzed"] >= total_participants]
    if whole:
        return [fullest(whole, specific, key)], False
    if allow_parts and _parts(informative, total_participants, key):
        return informative, True
    return [fullest(informative, specific, key)], False


def _parts(tables: List[Dict], total_participants: int, key: str) -> bool:
    """Rule 3's test: every table posts its own Number Analyzed below the
    baseline total, the numbers add up to exactly that total, no table counts
    more participants than its own number, and no two tables are the same
    table posted twice. Two tables with the same Number Analyzed, the same
    count in every category and the same population description (or none)
    (12 female / 8 male of 20, twice, in a 40-person trial) are read as a
    re-post of one subset: the sum of their numbers alone does not show them
    disjoint. Identical counts with different population descriptions are
    different people: NCT03711370 posts the same race counts for "mothers
    only (n = 76)" and "infants only (n = 76)", NCT05314712 "23 adults" and
    "23 children", all Blackfeet."""
    if not (all(t["analyzed"] is not None and t["analyzed"] < total_participants for t in tables)
            and sum(t["analyzed"] for t in tables) == total_participants
            and all(counted_sum(t) <= t["analyzed"] for t in tables)):
        return False
    seen = set()
    for t in tables:
        signature = (t["analyzed"], tuple(sorted(category_counts(t, key).items())), t.get("population"))
        if signature in seen:
            return False
        seen.add(signature)
    return True


def parted_by_description(tables: List[Dict], key: str) -> bool:
    """Whether summed parts include two tables told apart only by their
    population description: the same Number Analyzed and the same count in
    every category. _parts sums those as different people (NCT03711370,
    "mothers only" / "infants only"), but a description is free text, and one
    subset posted twice under reworded descriptions ("participants with
    available data" / "available-data participants") would pass the same
    test and be counted twice. The callers flag such sums so they can be
    audited; the counts are not changed."""
    seen = set()
    for t in tables:
        signature = (t["analyzed"], tuple(sorted(category_counts(t, key).items())))
        if signature in seen:
            return True
        seen.add(signature)
    return False



def percentage_denominator(measure: dict, overall_group_id: Optional[str],
                           fallback: Optional[int]) -> Optional[int]:
    """The participants a percentage measure's rows are percentages of.

    The measure's own Number Analyzed when it posts one at the measure level.
    Otherwise a class-level Number Analyzed only when every class posts one,
    all post the same number, and that number is shown to be table-wide
    (two or more classes, or one class with two or more rows): a table analyzed on part of the population
    repeats that number on each class (NCT00812214: 75 of 79 on every class,
    rows 80% + 20%). When classes post different numbers they are not a
    denominator: registrants put each row's own count there while the
    percentages are of the whole baseline (NCT02649218: Asian class "51",
    22.6%; White class "163", 72.1%; baseline 226, and 22.6% of 226 = 51).
    Then, as with no number at all, ``fallback`` (the baseline total).
    """
    from src.utils import get_measure_denom

    n = get_measure_denom(measure, overall_group_id)
    if n is not None:
        return n
    classes = [cls for cls in measure.get("classes", []) if isinstance(cls, dict)]
    per_class = {get_measure_denom({"denoms": cls.get("denoms") or []}, overall_group_id)
                 for cls in classes}
    # Every class must post it: a number on some classes only is not a
    # denominator the whole table repeats (a None in the set fails this).
    # And its table-wide meaning must be shown: repeated on two or more
    # classes, or posted on one class that holds two or more rows (the
    # NIH/OMB template: one class, a row per category). A lone row's number
    # is as likely its own count as a denominator, so the baseline is used.
    table_wide = len(classes) >= 2 or (len(classes) == 1 and len(classes[0].get("categories") or []) >= 2)
    if len(per_class) == 1 and None not in per_class and table_wide:
        return per_class.pop()
    return fallback
