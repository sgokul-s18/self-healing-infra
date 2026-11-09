#!/usr/bin/env bash
# Sends one request through Nginx every 0.5 s and records the result.
# Usage: tests/probe.sh [seconds] [outfile]
cd "$(dirname "$0")/.."
dur="${1:-120}"
out="${2:-logs/probe.csv}"
echo "epoch,http_code,time_total" > "$out"
end=$(( $(date +%s) + dur ))
while [ "$(date +%s)" -lt "$end" ]; do
  curl -s -o /dev/null -m 2 \
    -w "$(date +%s.%N),%{http_code},%{time_total}\n" http://localhost:8080/ >> "$out" || true
  sleep 0.5
done
echo "wrote $out"
