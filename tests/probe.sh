#!/usr/bin/env bash
# Usage: tests/probe.sh [duration_sec] [url]
# Prints one line per request: time, HTTP code, latency, serving container.
DURATION=${1:-30}
URL=${2:-http://localhost:8080/health}
END=$((SECONDS + DURATION))
printf '%-13s %-5s %-9s %-14s %s\n' TIME CODE LATENCY SERVED_BY NOTE
while [ $SECONDS -lt $END ]; do
  ts=$(date +%T.%3N)
  out=$(curl -s --max-time 3 -w '\n%{http_code} %{time_total}' "$URL")
  body=${out%$'\n'*}
  read -r code lat <<< "${out##*$'\n'}"
  who=$(echo "$body" | jq -r '.container' 2>/dev/null)
  [ -z "$who" ] && who="-"
  note=""
  [ "$code" != "200" ] && note="<-- FAIL"
  if [ -z "$note" ] && awk -v l="$lat" 'BEGIN{exit !(l>0.2)}'; then note="<-- SLOW"; fi
  printf '%-13s %-5s %-9s %-14s %s\n' "$ts" "$code" "${lat}s" "$who" "$note"
  sleep 0.5
done
