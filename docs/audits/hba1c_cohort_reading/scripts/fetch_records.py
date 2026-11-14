#!/usr/bin/env python3
"""Fetch and store ClinicalTrials.gov v2 records for the 248 HbA1c cohort trials.

Enforces <= 2 requests/second across all calls (0.62s minimum interval = 1.61 req/s max),
stores raw HTTP response bodies gzip-compressed under records/{NCT_ID}.json.gz,
records /api/v2/version once, and writes retrieval_log.csv.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RECORDS_DIR = BASE_DIR / "records"
API_VERSION_FILE = BASE_DIR / "api_version.json"
RETRIEVAL_LOG_JSONL = BASE_DIR / "retrieval_log.jsonl"
RETRIEVAL_LOG_CSV = BASE_DIR / "retrieval_log.csv"

API_BASE = "https://clinicaltrials.gov/api/v2"
MIN_INTERVAL_SECONDS = 0.62  # <= 1.61 req/s, strictly below the 2 req/s limit
USER_AGENT = "civicsample-hba1c-cohort-reading/1.0"

NCT_IDS = [
    "NCT00082381", "NCT00086502", "NCT00086515", "NCT00103857", "NCT00106704",
    "NCT00121641", "NCT00121667", "NCT00131664", "NCT00295633", "NCT00305604",
    "NCT00318461", "NCT00328627", "NCT00395343", "NCT00397631", "NCT00434954",
    "NCT00449930", "NCT00494013", "NCT00510952", "NCT00518882", "NCT00528372",
    "NCT00528879", "NCT00560417", "NCT00577824", "NCT00601250", "NCT00602472",
    "NCT00603239", "NCT00621140", "NCT00637273", "NCT00641043", "NCT00641056",
    "NCT00643851", "NCT00661362", "NCT00666718", "NCT00673231", "NCT00676338",
    "NCT00680745", "NCT00683878", "NCT00698932", "NCT00700817", "NCT00707031",
    "NCT00712673", "NCT00713830", "NCT00715624", "NCT00722371", "NCT00757588",
    "NCT00763451", "NCT00763815", "NCT00789737", "NCT00789750", "NCT00798161",
    "NCT00813995", "NCT00856986", "NCT00859898", "NCT00866658", "NCT00877890",
    "NCT00885352", "NCT00917267", "NCT00918879", "NCT00935532", "NCT00954447",
    "NCT00975286", "NCT00976391", "NCT00984867", "NCT00996658", "NCT01006291",
    "NCT01009580", "NCT01023581", "NCT01029886", "NCT01031680", "NCT01042977",
    "NCT01045447", "NCT01045707", "NCT01046110", "NCT01059799", "NCT01059812",
    "NCT01064414", "NCT01064687", "NCT01068665", "NCT01068678", "NCT01076075",
    "NCT01076088", "NCT01076647", "NCT01081834", "NCT01084005", "NCT01095653",
    "NCT01095666", "NCT01098539", "NCT01106625", "NCT01106651", "NCT01106677",
    "NCT01106690", "NCT01126580", "NCT01128153", "NCT01159600", "NCT01164501",
    "NCT01169779", "NCT01177384", "NCT01177813", "NCT01191268", "NCT01194830",
    "NCT01210001", "NCT01214239", "NCT01215097", "NCT01272193", "NCT01294423",
    "NCT01296412", "NCT01326026", "NCT01336023", "NCT01365507", "NCT01388361",
    "NCT01392573", "NCT01392677", "NCT01406717", "NCT01413204", "NCT01422876",
    "NCT01451398", "NCT01456195", "NCT01468987", "NCT01472185", "NCT01494987",
    "NCT01499082", "NCT01499095", "NCT01513590", "NCT01517412", "NCT01541215",
    "NCT01545388", "NCT01555164", "NCT01558271", "NCT01582451", "NCT01584232",
    "NCT01590771", "NCT01590797", "NCT01617434", "NCT01618162", "NCT01619059",
    "NCT01620489", "NCT01621178", "NCT01624259", "NCT01644500", "NCT01646320",
    "NCT01647542", "NCT01648582", "NCT01664247", "NCT01676116", "NCT01676220",
    "NCT01680341", "NCT01698775", "NCT01703221", "NCT01704261", "NCT01708902",
    "NCT01713530", "NCT01717313", "NCT01719003", "NCT01733758", "NCT01734785",
    "NCT01755156", "NCT01768559", "NCT01769378", "NCT01778049", "NCT01790438",
    "NCT01792518", "NCT01798706", "NCT01809327", "NCT01814137", "NCT01819129",
    "NCT01829477", "NCT01834274", "NCT01841697", "NCT01849289", "NCT01880736",
    "NCT01894568", "NCT01952145", "NCT01958671", "NCT01986855", "NCT02033889",
    "NCT02036515", "NCT02059187", "NCT02099110", "NCT02100475", "NCT02104804",
    "NCT02131272", "NCT02182830", "NCT02226003", "NCT02227875", "NCT02273050",
    "NCT02284893", "NCT02294474", "NCT02302716", "NCT02320721", "NCT02354222",
    "NCT02354235", "NCT02413398", "NCT02420262", "NCT02453555", "NCT02489968",
    "NCT02505334", "NCT02532855", "NCT02551874", "NCT02558296", "NCT02561078",
    "NCT02577003", "NCT02577016", "NCT02597049", "NCT02607865", "NCT02630706",
    "NCT02681094", "NCT02683746", "NCT02715258", "NCT02725593", "NCT02762578",
    "NCT02773368", "NCT02787551", "NCT02827708", "NCT02836873", "NCT02856113",
    "NCT02863328", "NCT02863419", "NCT02897349", "NCT02906917", "NCT02906930",
    "NCT02911948", "NCT02926937", "NCT02926950", "NCT02963766", "NCT02964247",
    "NCT03018028", "NCT03021187", "NCT03066830", "NCT03115112", "NCT03172494",
    "NCT03175120", "NCT03199053", "NCT03211858", "NCT03214380", "NCT03242018",
    "NCT03242252", "NCT03338010", "NCT03351478", "NCT03386344", "NCT03434119",
    "NCT03952143", "NCT04017832", "NCT04029480", "NCT04809220", "NCT05275400",
    "NCT05413369", "NCT05462756", "NCT06340854",
]


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self.last_call: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_call is not None:
            elapsed = now - self.last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self.last_call = time.monotonic()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def http_get(url: str, limiter: RateLimiter, max_attempts: int = 3) -> tuple[int | None, bytes, str, str]:
    """Perform rate-limited GET via macOS /usr/bin/curl (SecureTransport TLS verification)."""
    tmp_body = RECORDS_DIR / ".tmp_response.bin"
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        limiter.wait()
        if tmp_body.exists():
            tmp_body.unlink()
        proc = subprocess.run(
            [
                "/usr/bin/curl",
                "-sS",
                "--max-time",
                "30",
                "-H",
                "Accept: application/json",
                "-A",
                USER_AGENT,
                "-o",
                str(tmp_body),
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
        )
        retrieved_at = utc_now_iso()
        body = tmp_body.read_bytes() if tmp_body.exists() else b""
        if tmp_body.exists():
            tmp_body.unlink()

        code_str = (proc.stdout or "").strip()
        status = int(code_str) if code_str.isdigit() and int(code_str) > 0 else None

        if proc.returncode == 0 and status == 200:
            return 200, body, retrieved_at, ""

        if proc.returncode != 0:
            last_err = f"curl exit {proc.returncode}: {(proc.stderr or '').strip()}"
        else:
            last_err = f"HTTP {status}"

        if (status in (429, 500, 502, 503, 504) or proc.returncode != 0) and attempt < max_attempts:
            time.sleep(2.0 * attempt)
            continue
        return status, body, retrieved_at, last_err
    return None, b"", utc_now_iso(), last_err


def extract_status_dates(data: dict) -> tuple[str, str]:
    status_mod = (
        data.get("protocolSection", {}).get("statusModule")
        or data.get("statusModule")
        or {}
    )
    last_update = (status_mod.get("lastUpdatePostDateStruct") or {}).get("date", "") or ""
    results_first = (status_mod.get("resultsFirstPostDateStruct") or {}).get("date", "") or ""
    return last_update, results_first


def main() -> int:
    assert len(NCT_IDS) == 248, f"Expected 248 NCT IDs, got {len(NCT_IDS)}"
    assert len(set(NCT_IDS)) == 248, "Duplicate NCT IDs found"

    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(MIN_INTERVAL_SECONDS)

    # 1. Fetch /api/v2/version once
    version_meta = None
    if API_VERSION_FILE.exists():
        loaded = json.loads(API_VERSION_FILE.read_text(encoding="utf-8"))
        if loaded.get("http_status") == 200 and loaded.get("apiVersion"):
            version_meta = loaded
            print(f"Loaded existing API version metadata: {version_meta.get('apiVersion')} ({version_meta.get('dataTimestamp')})")
    if version_meta is None:
        v_status, v_body, v_time, v_err = http_get(f"{API_BASE}/version", limiter)
        v_sha = hashlib.sha256(v_body).hexdigest() if v_body else ""
        v_parsed = {}
        if v_body:
            try:
                v_parsed = json.loads(v_body.decode("utf-8"))
            except Exception as exc:
                v_err = f"{v_err}; JSONDecodeError: {exc}".strip("; ")
        version_meta = {
            "url": f"{API_BASE}/version",
            "retrieved_at_utc": v_time,
            "http_status": v_status,
            "sha256": v_sha,
            "apiVersion": v_parsed.get("apiVersion", ""),
            "dataTimestamp": v_parsed.get("dataTimestamp", ""),
            "raw_response": v_parsed,
            "error": v_err,
        }
        API_VERSION_FILE.write_text(json.dumps(version_meta, indent=2) + "\n", encoding="utf-8")
        print(f"Fetched API version: {version_meta['apiVersion']} | dataTimestamp={version_meta['dataTimestamp']} | status={v_status}")
        if v_status != 200:
            print(f"ERROR fetching /api/v2/version: {v_err}", file=sys.stderr)
            return 1

    # Load any already-fetched records for clean resumability
    existing_logs: dict[str, dict] = {}
    if RETRIEVAL_LOG_JSONL.exists():
        for line in RETRIEVAL_LOG_JSONL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                nct = row.get("nct_id")
                gz_path = RECORDS_DIR / f"{nct}.json.gz"
                if nct and row.get("http_status") == 200 and gz_path.exists() and not row.get("error"):
                    existing_logs[nct] = row

    results_by_nct: dict[str, dict] = dict(existing_logs)
    fetched_now = 0
    failures = 0

    with RETRIEVAL_LOG_JSONL.open("a", encoding="utf-8") as log_fp:
        for idx, nct_id in enumerate(NCT_IDS, start=1):
            if nct_id in results_by_nct:
                continue
            url = f"{API_BASE}/studies/{nct_id}"
            status, body, retrieved_at, err = http_get(url, limiter)
            sha256 = hashlib.sha256(body).hexdigest() if body else ""
            last_update = ""
            results_first = ""
            parse_err = ""

            if status == 200 and body:
                gz_bytes = gzip.compress(body, compresslevel=9, mtime=0)
                (RECORDS_DIR / f"{nct_id}.json.gz").write_bytes(gz_bytes)
                try:
                    parsed = json.loads(body.decode("utf-8"))
                    last_update, results_first = extract_status_dates(parsed)
                except Exception as exc:
                    parse_err = f"JSONDecodeError: {exc}"
            else:
                failures += 1
                if body:
                    (RECORDS_DIR / f"{nct_id}.error.gz").write_bytes(gzip.compress(body, compresslevel=9, mtime=0))

            combined_err = "; ".join(x for x in (err, parse_err) if x)
            if parse_err:
                failures += 1

            row = {
                "nct_id": nct_id,
                "retrieved_at_utc": retrieved_at,
                "http_status": status if status is not None else "",
                "sha256": sha256,
                "last_update_post_date": last_update,
                "results_first_post_date": results_first,
                "api_version": version_meta.get("apiVersion", ""),
                "api_data_timestamp": version_meta.get("dataTimestamp", ""),
                "api_version_retrieved_at_utc": version_meta.get("retrieved_at_utc", ""),
                "error": combined_err,
            }
            results_by_nct[nct_id] = row
            log_fp.write(json.dumps(row) + "\n")
            log_fp.flush()
            fetched_now += 1

            if idx % 25 == 0 or idx == len(NCT_IDS) or combined_err:
                print(
                    f"[{idx}/248] {nct_id}: status={status} last_update={last_update} "
                    f"results_first={results_first} err={combined_err or 'none'}",
                    flush=True,
                )

    # Write canonical retrieval_log.csv in the exact 248-trial order
    fieldnames = [
        "nct_id",
        "retrieved_at_utc",
        "http_status",
        "sha256",
        "last_update_post_date",
        "results_first_post_date",
        "api_version",
        "api_data_timestamp",
        "api_version_retrieved_at_utc",
        "error",
    ]
    with RETRIEVAL_LOG_CSV.open("w", encoding="utf-8", newline="") as csv_fp:
        writer = csv.DictWriter(csv_fp, fieldnames=fieldnames)
        writer.writeheader()
        for nct_id in NCT_IDS:
            writer.writerow(results_by_nct[nct_id])

    ok_count = sum(1 for n in NCT_IDS if results_by_nct[n]["http_status"] == 200 and not results_by_nct[n]["error"])
    print(f"Done. Total=248, OK={ok_count}, FetchedThisRun={fetched_now}, Failures={248 - ok_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
