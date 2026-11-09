#!/usr/bin/env bash
# Failure injection for the self-healing project.
# Usage: tests/inject_failure.sh <scenario> [container] [arg]
#   stop      graceful docker stop of one app container
#   kill      docker kill (SIGKILL, abrupt)
#   crash     make the Flask app terminate (container exits)
#   cpu       burn CPU for <arg> seconds (default 60)
#   mem       allocate <arg> MB (default 60; 100+ will hit the 128 MB limit and OOM-kill)
#   health    make /health return 503
#   repeated  kill a container <arg> times (default 3), 45 s apart
#   recover   undo health failure and release memory
# Every injection is logged with a timestamp to logs/failures.csv
set -euo pipefail
cd "$(dirname "$0")/.."

LOG=logs/failures.csv
[ -f "$LOG" ] || echo "epoch,scenario,container" > "$LOG"

scenario="${1:?usage: $0 <stop|kill|crash|cpu|mem|health|repeated|recover> [container] [arg]}"
target="${2:-}"
arg="${3:-}"

pick() { docker ps --filter "name=shi-app" --format '{{.Names}}' | sort | head -1; }
[ -n "$target" ] || target="$(pick)"
[ -n "$target" ] || { echo "no running app container found"; exit 1; }

log()  { echo "$(date +%s.%N),$1,$2" >> "$LOG"; echo "injected $1 into $2"; }
call() { docker exec "$1" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5000$2', timeout=${3:-5}).read().decode())"; }

case "$scenario" in
  stop)    log stop "$target";  docker stop -t 1 "$target" >/dev/null ;;
  kill)    log kill "$target";  docker kill "$target" >/dev/null ;;
  crash)   log crash "$target"; call "$target" /crash ;;
  cpu)     log cpu "$target";   call "$target" "/cpu?seconds=${arg:-60}" 120 >/dev/null & ;;
  mem)     log mem "$target";   call "$target" "/mem?mb=${arg:-60}" ;;
  health)  log health "$target"; call "$target" /fail-health ;;
  recover) call "$target" /recover-health; call "$target" /mem/release ;;
  repeated)
    n="${arg:-3}"
    for i in $(seq 1 "$n"); do
      t="${2:-$(pick)}"
      [ -n "$t" ] || { echo "no running container"; break; }
      log repeated "$t"; docker kill "$t" >/dev/null
      [ "$i" -lt "$n" ] && sleep 45
    done ;;
  *) echo "unknown scenario: $scenario"; exit 1 ;;
esac
