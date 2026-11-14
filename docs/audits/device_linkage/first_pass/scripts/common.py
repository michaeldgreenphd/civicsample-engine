"""Shared helpers for the device-linkage audit. Pure stdlib.

Nothing here reads environment variables or writes inside a git repository.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from datetime import date

# Legal-form and filler tokens dropped when normalising a company name.
# Deliberately conservative: only tokens that carry no identity. Words like
# "medical", "healthcare", "systems" are kept at this level (level 1) because
# "Siemens Medical Solutions" and "Siemens Healthcare" are different legal
# entities even if one corporate family; family clustering is level 2.
LEGAL_TOKENS = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv",
    "nv", "ab", "as", "oy", "aps", "kk", "plc", "pty", "pte", "lp", "llp",
    "kg", "se", "sl", "the", "and", "of", "mbh", "cokg", "pvt", "private",
    "holdings", "holding", "group",
}


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def norm_text(s: str) -> str:
    """Lowercase, ascii-fold, strip punctuation to single spaces."""
    s = ascii_fold(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(s: str) -> str:
    """Level-1 normalisation: case, punctuation, legal-form suffixes.

    Assumptions, stated because they decide what counts as "the same
    manufacturer": (a) dotted legal forms such as "S.A.", "B.V.", "A/S" fall
    apart into single letters under punctuation stripping, so single-letter
    tokens are dropped as legal-form residue; (b) "U.S.A." is rejoined to
    "usa" first so it is not lost that way; (c) "(publ)" is a Swedish legal
    marker; (d) a "d/b/a" tail is cut -- the registered name is kept; (e) a
    parenthesised parent ("(Medtronic)", "(GE Healthcare)") is dropped at this
    level and handled as a family relation instead.
    """
    s = re.sub(r"\bd/b/a\b.*$", "", s or "", flags=re.I)
    s = re.sub(r"\([^)]*\)", " ", s)
    t = norm_text(s)
    t = re.sub(r"\bu s a\b", "usa", t)
    toks = [x for x in t.split() if x not in LEGAL_TOKENS and x != "publ" and len(x) > 1]
    return " ".join(toks)


# Corporate filler dropped to reach a "brand" key (level 2). This is the level
# at which "GE Medical Systems SCS" and "GE Healthcare Japan" coincide. It
# over-merges by design and is used for sizing and for recall-oriented sponsor
# queries, never on its own as evidence of a link.
CORPORATE_FILLER = {
    "medical", "systems", "system", "healthcare", "health", "technologies", "technology", "tech",
    "imaging", "diagnostics", "diagnostic", "solutions", "solution", "usa", "us", "america", "americas",
    "labs", "laboratories", "laboratory", "international", "software", "devices", "device",
    "instruments", "sciences", "science", "research", "ultrasound", "primary", "care", "japan",
    "nederland", "nederlands", "israel", "france", "europe", "china", "korea", "na", "global",
    "innovations", "innovation", "informatics", "intelligence", "electronics", "commercial", "products",
    # City names are deliberately NOT filler: dropping "shanghai" turned
    # "Shanghai United Imaging Healthcare" into the brand key "united", whose
    # sponsor query returned 3,178 studies. Kept, they also keep "GE Hangwei"
    # and "Philips Healthcare (Suzhou)" as their own level-2 names.
    "magnetic", "resonance", "functional", "mr", "finland", "dmc", "scs",
}


def company_brand(s: str) -> str:
    toks = [t for t in norm_company(s).split() if t not in CORPORATE_FILLER]
    return " ".join(toks) or norm_company(s)


def company_family_key(s: str) -> str:
    """Level-2 key: the brand key (level-1 name minus corporate filler).

    A blunt heuristic that over-merges (two unrelated firms sharing a brand
    word) and under-merges (renames, acquisitions). Used only to *size* the
    corporate-family problem, never to assert a link.
    """
    return company_brand(s)


def parse_decision_date(s: str) -> date | None:
    """FDA list uses M/D/YY. All dates in this list are 1995-2025, so a
    two-digit year >= 90 is 19xx and anything else is 20xx."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*", s or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 1900 + y if y >= 90 else 2000 + y
    return date(y, mo, d)


def pathway_of(sn: str) -> str:
    sn = (sn or "").strip().upper()
    if sn.startswith("DEN"):
        return "De Novo"
    if sn.startswith("BK"):
        return "510(k) (CBER)"
    if sn.startswith("P"):
        return "PMA"
    if sn.startswith("K"):
        return "510(k)"
    return "Other"


def read_devices(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Device-name forms
# ---------------------------------------------------------------------------
# Words that describe what a device IS rather than what it is CALLED. A name
# made only of these ("Auto Segmentation", "AI Platform", "Deep Learning Image
# Reconstruction") cannot identify a product in registry free text.
GENERIC_DEVICE_WORDS = {
    "system", "systems", "software", "diagnostic", "diagnostics", "ultrasound", "imaging", "image",
    "images", "ct", "mr", "mri", "pet", "xray", "x", "ray", "digital", "radiography", "scanner",
    "platform", "module", "application", "app", "analysis", "analyzer", "workstation", "viewer",
    "deep", "learning", "reconstruction", "ai", "portable", "series", "station", "processing",
    "mobile", "solution", "suite", "tool", "tools", "kit", "test", "device", "monitor", "monitoring",
    "detection", "assist", "plus", "pro", "auto", "automatic", "automated", "segmentation", "with",
    "and", "for", "the", "of", "option", "only", "plug", "in", "version", "medical", "clinical",
    "cardiac", "remote", "triage", "notification", "enhancement", "view", "one", "go", "base",
    "smart", "model", "bone", "marrow", "spectral", "lung", "bladder", "brain", "breast", "cad",
    "strain", "rapid", "insights", "planner", "classifier", "semen", "low", "dose", "cancer",
    "screening", "body", "fluid", "blood", "smear", "full", "field", "peripheral", "slide", "loader",
    "rhythm", "express", "critical", "care", "algorithm", "endotracheal", "tube",
}
_VERSION_TAIL = re.compile(
    r"(?:\b(?:software\s+)?(?:version|ver|rev|release)\s*[\w.]+|\bv\s*\d[\w.]*|\b\d+(?:\.\w+)+|\b\d+\.x)\s*$",
    re.I)


def _core(segment: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", segment)       # drop parenthesised model numbers / aliases
    s = re.sub(r"\s+", " ", s).strip(" ,;/-")
    prev = None
    while prev != s:                               # peel stacked version tails
        prev = s
        s = _VERSION_TAIL.sub("", s).strip(" ,;/-")
    return s


def device_name_forms(device_clean: str, company_clean: str = "") -> dict:
    """Derive the strings used to query and to match a device name.

    raw       the list's name with control characters removed (queried as-is)
    core      first model segment, parentheses and trailing version removed
    variants  core of every model segment (split on ';', ' / ', ', ')
    distinct  each variant's tokens minus GENERIC_DEVICE_WORDS and minus the
              company's own brand tokens -- what is left is the part of the
              name that can discriminate. Empty => the name is generic or is
              just the manufacturer's name plus a device type.
    """
    segs = [x for x in re.split(r"\s*;\s*|\s+/\s+|,\s+", device_clean) if x.strip()]
    variants = []
    for seg in segs:
        c = _core(seg)
        if c and c not in variants:
            variants.append(c)
    core = variants[0] if variants else device_clean
    brand = set(company_brand(company_clean).split()) if company_clean else set()
    distinct = []
    for v in variants:
        toks = [t for t in norm_text(v).split() if t not in GENERIC_DEVICE_WORDS and t not in brand]
        # a lone 1-2 character or purely numeric leftover is not discriminating
        toks = [t for t in toks if not (len(toks) == 1 and (len(t) <= 2 or t.isdigit()))]
        d = " ".join(toks)
        if d and d not in distinct:
            distinct.append(d)
    return {"raw": device_clean, "core": core, "variants": variants, "distinct": distinct,
            "name_is_generic": not distinct}
