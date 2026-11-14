#!/usr/bin/env bash
# Item 2: one request per host, HTTP status only. No retries, no bypass.
# usage: 02_reachability.sh <out.txt>     (output of the single run made on 2026-11-13 is in out/reachability.txt)
set -u
out="$1"; tmp="$(mktemp)"
for u in "https://clinicaltrials.gov/api/v2/version" \
         "https://www.accessdata.fda.gov/cdrh_docs/pdf25/K253532.pdf" \
         "https://api.fda.gov/device/510k.json?limit=1" \
         "https://www.fda.gov/"; do
  printf '%s\t' "$u"
  curl -sS -o "$tmp" -w '%{http_code}\t%{content_type}\t%{size_download}\n' --max-time 40 \
       -A "device-linkage-audit/1.0 (research)" "$u" 2>&1 | tr '\n' ' '
  echo
done | tee "$out"
rm -f "$tmp"
