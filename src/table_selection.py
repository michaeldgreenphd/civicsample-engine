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
     with specific counts posts its own Number Analyzed below the baseline
     total, those numbers add up to EXACTLY the baseline total, and no table
     counts more participants than its own Number Analyzed. A smaller sum is
     no evidence of different people: the same 20 of 50 participants posted
     twice also add up to less than 50.
(Rule 1, which titles qualify and which take precedence, is each extractor's.)
Every table not counted is the caller's to flag as superseded. The before and
after counts of each extractor's adoption are in its own block comment.
"""
from typing import Dict, FrozenSet, List, Optional, Tuple


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

    n = get_measure_denom(measure, overall_group_id)
    if n is not None:
        return n
    per_class = [get_measure_denom({"denoms": cls.get("denoms") or []}, overall_group_id)
                 for cls in measure.get("classes", []) if isinstance(cls, dict)]
    per_class = [v for v in per_class if v is not None]
    return max(per_class) if per_class else None


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
    if (allow_parts
            and sum(t["analyzed"] for t in informative) == total_participants
            and all(counted_sum(t) <= t["analyzed"] for t in informative)):
        return informative, True
    return [fullest(informative, specific, key)], False



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
