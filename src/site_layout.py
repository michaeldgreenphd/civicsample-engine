"""The site's split layout, version 1: which file of a dataset holds which field.

The site decides this, not the engine. Its tests/record_contract.json lists
every study-record field the dashboard reads, in three classes:

  core          read over every record when the page loads;
  studies_tab   read by the Studies table, its tooltips and its breakdown and
                publications views;
  detail        read by the study and geography pop-ups only;

and its `layout` section describes the files a split dataset is cut into and
carries the owner's switch, layout.enabled. scripts/split_data.py cuts a
dataset with this module and scripts/check_site_contract.py checks one with
it, so the two cannot disagree. A dataset is one folder (data/ for the latest
run, snapshots/<date>/ for an archived week). Split, it holds:

  demographics.part{K}.json.gz, K = 1..8
        the core fields of every record, in record order and in the same 8
        positional slices as before the split, with a `layout` block in every
        header: {"version": 1, "studies_tab": {"files": 8},
        "detail": {"shards": N, "key": "nct_number_mod"}};
  studies_tab.part{K}.json.gz
        the studies_tab fields of exactly core part K's records, as an object
        keyed by nct_id;
  detail/{n}.json.gz, n = 0..N-1 (no zero padding)
        the detail fields of every record whose shard is
        n = int(nct_id[3:]) % N, keyed by nct_id. Every record has exactly one
        entry, so "loaded, none" differs from "not loaded";
  run.json
        the run's stamps.

Every file carries the run's stamps, extracted_at and pipeline_commit. N is
256 (owner decision 13a) unless the contract names another count in
layout.detail.shards; each header carries the count it was cut with.

A dataset whose core part 1 carries no `layout` block is inline: every class
is on the records. That is every file published before the split, and what
the engine writes while layout.enabled is false.

The site puts a record back together with mergeStudy (app.js): the four
demographic objects (race, ethnicity, sex, gender) merge one level deep, and
every other top-level key of a studies_tab or detail entry replaces the
core's. So a class file writes whole every merge unit it owns -- a top-level
key, or one key inside a demographic object -- with every contract field in
that unit, those core reads included. Today that is study_sites: its items
carry their country in core, and again, beside the facility, city and the
rest, in the detail shards (the contract's layout.detail.whole_lists).
merge_study below is mergeStudy's twin, and split_data.py checks on every
record that core + studies_tab + detail merge back to the record projected
onto every contract field, so nothing the site reads can be lost on the way.

An optional path (one the site reads when a record has it) rides with the
class the contract's layout.optional_class names, which must share the path's
container; without that map, with the one class whose paths share its
deepest container (official_title, which has none, with core). Paths in no
class and not optional are dropped. Values are copied as they are: null, []
and {} stay (absence is not zero), and a value of the wrong shape is kept
whole, so the publish check reports it rather than the split hiding it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

LAYOUT_VERSION = 1
SHARD_KEY = "nct_number_mod"
DEFAULT_DETAIL_SHARDS = 256
MAX_DETAIL_SHARDS = 1024
CLASSES = ("core", "studies_tab", "detail")
SIDECARS = ("studies_tab", "detail")
# mergeStudy merges these one level deep ({...core.race, ...extra.race}).
DIMENSIONS = ("race", "ethnicity", "sex", "gender")
PATH_RE = re.compile(r"[a-z_]+(\[\])?(\.[a-z_]+(\[\])?)*")
NCT_RE = re.compile(r"NCT\d{8}")

# The file names (the contract's layout.files) and the header keys of each
# file (its layout.headers), as this layout version fixes them.
FILES = {"core": "demographics.part{K}.json.gz",
         "studies_tab": "studies_tab.part{K}.json.gz",
         "detail": "detail/{n}.json.gz"}
HEADERS = {"core": ("extracted_at", "pipeline_commit", "part", "total_parts", "layout", "data"),
           "studies_tab": ("extracted_at", "pipeline_commit", "class", "part", "total_parts", "data"),
           "detail": ("extracted_at", "pipeline_commit", "class", "shard", "shards", "key", "data")}
CORE_PART_RE = re.compile(r"demographics\.part(\d+)\.json\.gz")
STUDIES_TAB_PART_RE = re.compile(r"studies_tab\.part(\d+)\.json\.gz")
DETAIL_DIR = "detail"
RUN_FILE = "run.json"


class LayoutError(ValueError):
    """The contract, or a record, cannot be laid out as the layout says."""


# ── names and shards ────────────────────────────────────────────────────────

def core_part_name(k: int) -> str:
    return FILES["core"].replace("{K}", str(k))


def studies_tab_part_name(k: int) -> str:
    return FILES["studies_tab"].replace("{K}", str(k))


def detail_shard_name(n: int) -> str:
    """The shard's path inside the dataset folder: detail/<n>.json.gz, unpadded."""
    return FILES["detail"].replace("{n}", str(n))


def shard_of(nct_id: Any, shards: int) -> int:
    """n = int(nct_id[3:]) % shards for an id NCT + 8 digits; LayoutError for any other id."""
    if not isinstance(nct_id, str) or not NCT_RE.fullmatch(nct_id):
        raise LayoutError(f"nct_id {nct_id!r} is not NCT followed by 8 digits, so no detail shard can hold it")
    return int(nct_id[3:]) % shards


def header_layout(total_parts: int, shards: int) -> dict[str, Any]:
    """The `layout` block in every core part header (and in run.json) of a split dataset."""
    return {"version": LAYOUT_VERSION, "studies_tab": {"files": total_parts},
            "detail": {"shards": shards, "key": SHARD_KEY}}


# ── the switch ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Switch:
    """What the site's contract says about the split."""
    declared: bool            # the contract has a layout section (version 1)
    enabled: bool             # ... and the owner has turned it on

    @property
    def split(self) -> bool:
        return self.declared and self.enabled


def read_switch(contract: Any) -> Switch:
    """The contract's layout switch.

    Raises LayoutError when the section is not an object, names a layout
    version this engine does not know, or has an `enabled` that is not true
    or false: a layout the site's reader expects but the engine does not
    write would leave its pages empty, so the engine refuses rather than
    guess."""
    if not isinstance(contract, dict):
        raise LayoutError("the contract is not a JSON object")
    if "layout" not in contract:
        return Switch(False, False)
    section = contract["layout"]
    if not isinstance(section, dict):
        raise LayoutError(f"the contract's layout section is a {type(section).__name__}, not an object")
    version = section.get("version")
    if type(version) is not int or version != LAYOUT_VERSION:
        raise LayoutError(f"the contract's layout section is version {version!r}; "
                          f"this engine knows layout version {LAYOUT_VERSION} only")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise LayoutError(f"the contract's layout.enabled is {enabled!r}, not true or false")
    return Switch(True, enabled)


# ── paths as projections ────────────────────────────────────────────────────

# A spec says what to keep of a value: LEAF keeps it whole; a dict keeps those
# keys of an object, each with its own spec; Each keeps a list and, of every
# item that is an object, its item spec's keys.
LEAF = None


@dataclass
class Each:
    item: Any                          # LEAF (whole items) or a dict spec


def segments(path: str) -> list[tuple[str, bool]]:
    """'race.raw_categories[].omb_category' -> [(race, False), (raw_categories, True), (omb_category, False)]."""
    return [(s[:-2], True) if s.endswith("[]") else (s, False) for s in path.split(".")]


def _add_path(spec: dict[str, Any], path: str) -> None:
    node = spec
    segs = segments(path)
    for i, (key, each) in enumerate(segs):
        last = i == len(segs) - 1
        if key in node and node[key] is LEAF:
            return                                       # the whole value is kept already
        current = node.get(key)
        if each:
            if key in node and not isinstance(current, Each):
                raise LayoutError(f"{path} reads {key} as a list; another path reads it as an object")
            if current is None:
                current = node[key] = Each({})
            if last:
                current.item = LEAF                      # 'a[]': every item whole
                return
            if current.item is LEAF:
                return
            node = current.item
        else:
            if last:
                node[key] = LEAF                         # the value, whole
                return
            if isinstance(current, Each):
                raise LayoutError(f"{path} reads {key} as an object; another path reads it as a list")
            if current is None:
                current = node[key] = {}
            node = current


def build_spec(paths: list[str]) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    for p in paths:
        _add_path(spec, p)
    return spec


def project_value(value: Any, spec: Any) -> Any:
    """What spec keeps of value. null, or a value of another shape than the spec reads, is kept as it is."""
    if spec is LEAF:
        return value
    if isinstance(spec, Each):
        if not isinstance(value, list) or spec.item is LEAF:
            return value
        return [project_object(x, spec.item) if isinstance(x, dict) else x for x in value]
    if isinstance(value, dict):
        return project_object(value, spec)
    return value


def project_object(obj: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The keys of obj that spec names, in obj's own order, each projected."""
    return {k: project_value(v, spec[k]) for k, v in obj.items() if k in spec}


def _sub(spec: Any, key: str) -> Any:
    return LEAF if spec is LEAF else spec[key]


# ── merge units ─────────────────────────────────────────────────────────────

def unit_of(path: str) -> tuple[str, ...]:
    """The merge unit a path belongs to: (key,), or (dimension, key) inside a demographic object."""
    segs = segments(path)
    head, each = segs[0]
    if head in DIMENSIONS and not each and len(segs) > 1:
        return (head, segs[1][0])
    return (head,)


def _overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """One unit holds the other: (race,) holds (race, raw_categories)."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def merge_study(core: dict[str, Any], *extras: dict[str, Any] | None) -> dict[str, Any]:
    """The site's mergeStudy: a demographic object merges one level deep (and an
    entry's null or non-object one is skipped); every other key replaces."""
    out = dict(core)
    for extra in extras:
        for k, v in (extra or {}).items():
            if k in DIMENSIONS:
                if isinstance(v, dict):
                    base = out.get(k)
                    out[k] = {**(base if isinstance(base, dict) else {}), **v}
                continue
            out[k] = v
    return out


def _container(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def _keys(path: str) -> list[str]:
    return [k for k, _ in segments(path)]


def place_optional(path: str, classes: dict[str, list[str]]) -> str:
    """The class an optional path rides with when the contract does not say: the
    one class whose paths share its deepest container; core when it has none."""
    parts = path.split(".")
    for depth in range(len(parts) - 1, 0, -1):          # deepest container first
        container = _keys(".".join(parts[:depth]))
        owners = [c for c in CLASSES
                  if any(_keys(q)[:len(container)] == container for q in classes[c])]
        if len(owners) == 1:
            return owners[0]
        if len(owners) > 1:
            raise LayoutError(f"optional path {path}: {' and '.join(owners)} both read {'.'.join(container)}; "
                              "the contract's layout.optional_class has to place it")
    return "core"                                        # a new top-level field: core is always loaded


class Plan:
    """The contract's classes as projections of one record, and the layout's
    parameters, validated. LayoutError when the contract cannot be split
    without losing or guessing at something the site reads.

    paths[c]     the class's contract paths plus the optional paths it carries
    units[c]     the merge units of paths[c]
    whole        the units a studies_tab or detail file writes whole although
                 core reads part of them too (study_sites today)
    required[c]  the paths every record or entry of the class's files must
                 carry: its contract paths, and for studies_tab and detail also
                 every contract path inside the units they write whole
    shards       the detail shard count
    """

    def __init__(self, contract: Any) -> None:
        classes = contract.get("classes") if isinstance(contract, dict) else None
        if not isinstance(classes, dict):
            raise LayoutError("the contract has no classes")
        if sorted(classes) != sorted(CLASSES):
            raise LayoutError(f"the contract's classes are {sorted(classes)}, not {list(CLASSES)}")
        optional = contract.get("optional", [])
        seen: dict[str, str] = {}
        for name, listed in [*((c, classes[c]) for c in CLASSES), ("optional", optional)]:
            if not isinstance(listed, list):
                raise LayoutError(f"the contract's {name} is not a list of paths")
            for p in listed:
                if not isinstance(p, str) or not PATH_RE.fullmatch(p):
                    raise LayoutError(f"malformed path in {name}: {p!r}")
                if p in seen:
                    raise LayoutError(f"{p} is listed twice ({seen[p]} and {name})")
                seen[p] = name
        self.contract_paths: dict[str, list[str]] = {c: list(classes[c]) for c in CLASSES}
        section = contract.get("layout")
        section = section if isinstance(section, dict) else {}
        self.shards = self._read_layout(section)
        self.optional_class = self._place(optional, section.get("optional_class"))
        self.paths: dict[str, list[str]] = {
            c: self.contract_paths[c] + [p for p, oc in self.optional_class.items() if oc == c] for c in CLASSES}
        self.spec: dict[str, dict[str, Any]] = {c: build_spec(self.paths[c]) for c in CLASSES}
        self.union: dict[str, Any] = build_spec([p for c in CLASSES for p in self.paths[c]])
        self.units: dict[str, set[tuple[str, ...]]] = {c: {unit_of(p) for p in self.paths[c]} for c in CLASSES}
        for a in self.units["studies_tab"]:
            for b in self.units["detail"]:
                if _overlap(a, b):
                    raise LayoutError(f"studies_tab reads {'.'.join(a)} and detail reads {'.'.join(b)}; the site "
                                      "replaces such a field whole from whichever file arrives, so one class must hold it")
        self.whole: list[str] = sorted(".".join(u) for c in SIDECARS for u in self.units[c]
                                       if any(_overlap(u, v) for v in self.units["core"]))
        self.whole_lists = self._whole_lists(section)
        all_paths = [p for c in CLASSES for p in self.contract_paths[c]]
        self.required: dict[str, list[str]] = {"core": list(self.contract_paths["core"])}
        for c in SIDECARS:
            inside = [p for p in all_paths if p not in self.contract_paths[c]
                      and any(_overlap(unit_of(p), u) for u in self.units[c])]
            self.required[c] = self.contract_paths[c] + inside
        self.dimension_units: dict[str, dict[str, set[str]]] = {
            c: {d: {u[1] for u in self.units[c] if len(u) == 2 and u[0] == d} for d in DIMENSIONS} for c in CLASSES}

    def _read_layout(self, section: dict[str, Any]) -> int:
        """Check the layout section's fixed parts against this engine's; return the shard count."""
        if "files" in section and section["files"] != FILES:
            raise LayoutError(f"the contract's layout.files is {section['files']!r}; this engine writes {FILES!r}")
        headers = section.get("headers")
        if headers is not None:
            if not isinstance(headers, dict):
                raise LayoutError("the contract's layout.headers is not an object")
            for name, keys in HEADERS.items():
                if name in headers and (not isinstance(headers[name], list) or set(headers[name]) != set(keys)):
                    raise LayoutError(f"the contract's layout.headers.{name} is {headers[name]!r}; "
                                      f"this engine writes {list(keys)}")
        detail = section.get("detail", {})
        if not isinstance(detail, dict):
            raise LayoutError("the contract's layout.detail is not an object")
        if "key" in detail and detail["key"] != SHARD_KEY:
            raise LayoutError(f"the contract's layout.detail.key is {detail['key']!r}; this engine shards by {SHARD_KEY!r}")
        shards = detail.get("shards", DEFAULT_DETAIL_SHARDS)
        if type(shards) is not int or not 1 <= shards <= MAX_DETAIL_SHARDS:
            raise LayoutError(f"the contract's layout.detail.shards is {shards!r}, "
                              f"not a whole number from 1 to {MAX_DETAIL_SHARDS}")
        # The site's test vectors for the shard rule: both sides must agree.
        vectors = detail.get("vectors", [])
        if not isinstance(vectors, list):
            raise LayoutError("the contract's layout.detail.vectors is not a list")
        for v in vectors:
            try:
                got = shard_of(v["nct_id"], v["shards"])
            except (KeyError, TypeError, ZeroDivisionError, LayoutError) as e:
                raise LayoutError(f"the contract's layout.detail.vectors holds {v!r}, which is not a test vector ({e})")
            if got != v["shard"]:
                raise LayoutError(f"the contract's shard test vector {v!r} gives shard {got} here, not {v['shard']}")
        return shards

    def _place(self, optional: list[str], given: Any) -> dict[str, str]:
        """Each optional path's class: the contract's layout.optional_class, checked, or the rule."""
        if given is None:
            return {p: place_optional(p, self.contract_paths) for p in optional}
        if not isinstance(given, dict) or sorted(given) != sorted(optional):
            raise LayoutError("the contract's layout.optional_class does not place exactly the optional paths")
        for p, c in given.items():
            if c not in CLASSES:
                raise LayoutError(f"the contract's layout.optional_class puts {p} in {c!r}, which is not a class")
            container = _container(p)
            if container and not any(q.startswith(container + ".") for q in self.contract_paths[c]):
                raise LayoutError(f"the contract's layout.optional_class puts {p} in {c}, "
                                  f"but no {c} path shares its container {container}")
        return dict(given)

    def _whole_lists(self, section: dict[str, Any]) -> list[str]:
        """layout.detail.whole_lists: lists the detail shards write whole, which detail must read."""
        detail = section.get("detail", {})
        listed = detail.get("whole_lists", []) if isinstance(detail, dict) else []
        if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
            raise LayoutError("the contract's layout.detail.whole_lists is not a list of names")
        for name in listed:
            if (name,) not in self.units["detail"]:
                raise LayoutError(f"the contract's layout.detail.whole_lists names {name}, which the detail class does not read")
        return list(listed)

    # ── one record ──────────────────────────────────────────────────────────

    def core(self, record: dict[str, Any]) -> dict[str, Any]:
        """The record's core fields: its entry in a core part."""
        return project_object(record, self.spec["core"])

    def sidecar(self, record: dict[str, Any], cls: str) -> dict[str, Any]:
        """The record's entry in a studies_tab part or a detail shard: every merge
        unit the class owns, whole (every contract field in it)."""
        units = self.units[cls]
        dims = self.dimension_units[cls]
        out: dict[str, Any] = {}
        for k, v in record.items():
            if (k,) in units:
                out[k] = project_value(v, self.union[k])
            elif k in DIMENSIONS and dims[k]:
                if isinstance(v, dict):
                    sub = {k2: project_value(v2, _sub(self.union[k], k2)) for k2, v2 in v.items() if k2 in dims[k]}
                    if sub:
                        out[k] = sub
                else:
                    out[k] = v                           # null or another shape: kept, for the check to report
        return out

    def full(self, record: dict[str, Any]) -> dict[str, Any]:
        """The record projected onto every class and optional path: what its
        core, studies_tab and detail entries must merge back to."""
        return project_object(record, self.union)

    def stray_keys(self, entry: dict[str, Any], cls: str) -> list[str]:
        """The fields of a class file's record or entry that the class does not
        own: the projection has re-fattened (reported, not fatal)."""
        out: list[str] = []
        units = self.units[cls]
        for k, v in entry.items():
            if (k,) in units:
                continue
            if k in DIMENSIONS and self.dimension_units[cls][k] and isinstance(v, dict):
                out += [f"{k}.{k2}" for k2 in v if k2 not in self.dimension_units[cls][k]]
            elif not (k in DIMENSIONS and self.dimension_units[cls][k]):
                out.append(k)
        return out
