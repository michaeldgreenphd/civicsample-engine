#!/usr/bin/env python3
"""Item 3, second half: what the pilot LLM extractions put in `nct_ids`, and
whether a regex over the same PDFs agrees. Reads existing outputs only; makes
no model or network calls.

usage: python3 -I 06_pilot_llm_vs_regex.py <fda_demographics_extracted.json> <pdf_identifiers.json> <out.json>
"""
from __future__ import annotations

import json
import re
import sys


def find(o, key, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield path + "/" + k, v
            yield from find(v, key, path + "/" + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from find(v, key, f"{path}[{i}]")


def main() -> None:
    llm = json.load(open(sys.argv[1]))
    regex = {r["submission_number"]: r for r in json.load(open(sys.argv[2]))}
    out = []
    for doc in llm:
        sn = doc["submission_number"]
        rx = regex.get(sn)
        models = {}
        for mk, mv in (doc.get("models") or {}).items():
            hits = list(find(mv, "nct_ids"))
            state, value = "field_absent_malformed_output", None
            if hits:
                path, v = hits[0]
                value = v.get("value") if isinstance(v, dict) else v
                # expected location is /data/cited_clinical_studies/nct_ids
                state = ("present_expected_path" if path == "/data/cited_clinical_studies/nct_ids"
                         else "present_misnested:" + path)
            models[mk] = {"model_id": mv.get("model_id"), "call_succeeded": mv.get("call_succeeded"),
                          "nct_ids_state": state, "nct_ids_value": value,
                          "any_nct_pattern_in_raw_output": sorted(set(re.findall(r"NCT\d{8}", json.dumps(mv))))}
        llm_union = sorted({x for m in models.values() for x in (m["nct_ids_value"] or [])})
        out.append({
            "submission_number": sn, "device": doc.get("device_name"), "page_count": doc.get("page_count"),
            "models": models, "llm_union": llm_union,
            "regex_union": rx["nct_pypdf"]["union"] if rx else None,
            "regex_pdf_document_class": rx["document_class"] if rx else None,
            "agree": (rx is not None and llm_union == rx["nct_pypdf"]["union"]),
        })
    json.dump(out, open(sys.argv[3], "w"), indent=1)
    n_models = sum(len(d["models"]) for d in out)
    states = {}
    for d in out:
        for m in d["models"].values():
            k = m["nct_ids_state"].split(":")[0] + (" / empty list" if m["nct_ids_value"] == [] else
                                                    " / non-empty" if m["nct_ids_value"] else "")
            states[k] = states.get(k, 0) + 1
    print(f"documents: {len(out)}; model outputs: {n_models}")
    for k, v in sorted(states.items()):
        print(f"  {v:2d}  {k}")
    print("documents where LLM union == regex union:", sum(d["agree"] for d in out), "of", len(out))
    print("documents with any NCT ID from either method:", sum(1 for d in out if d["llm_union"] or d["regex_union"]))
    for d in out:
        bad = {k: v["nct_ids_state"] for k, v in d["models"].items() if v["nct_ids_state"] != "present_expected_path"}
        if bad:
            print("  malformed:", d["submission_number"], bad)


if __name__ == "__main__":
    main()
