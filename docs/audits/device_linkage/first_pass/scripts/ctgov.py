"""Minimal polite ClinicalTrials.gov API v2 client with an on-disk cache.

Every response (including errors) is appended to a JSONL cache keyed by the
exact parameter set, so a rerun makes no network calls for work already done
and the raw evidence behind every number is kept. Stdlib only; honours the
session proxy via urllib's default handlers. No credentials are used.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://clinicaltrials.gov/api/v2/studies"
UA = "device-linkage-audit/1.0 (research)"
DELAY_SEC = 0.6


def _key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]


class Cache:
    def __init__(self, path: str):
        self.path = path
        self.mem: dict[str, dict] = {}
        for p in (path, path + ".gz"):
            if os.path.exists(p):
                opener = gzip.open if p.endswith(".gz") else open
                with opener(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        rec = json.loads(line)
                        self.mem[rec["key"]] = rec

    def get(self, params: dict, tag: dict | None = None) -> dict:
        k = _key(params)
        if k in self.mem:
            return self.mem[k]
        url = BASE + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        rec = {"key": k, "tag": tag or {}, "params": params, "url": url,
               "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    rec["status"] = resp.status
                    rec["body"] = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                rec["status"] = e.code
                rec["body"] = None
                rec["error"] = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(5 * (attempt + 1))
                    continue
                break  # 400 etc. is a finding about the query, not a transient
            except Exception as e:  # network-level failure: recorded, retried
                rec["status"] = None
                rec["body"] = None
                rec["error"] = f"{type(e).__name__}: {e}"[:500]
                time.sleep(5 * (attempt + 1))
        # a response that never arrived is not cached, so a rerun retries it
        if rec.get("status") is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.mem[k] = rec
        time.sleep(DELAY_SEC)
        return rec
