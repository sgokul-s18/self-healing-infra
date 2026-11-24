#!/usr/bin/env bash
# Usage: tests/watch.sh [interval_sec] [duration_sec]
# One row per app container per refresh: date, time, name, state, health, CPU, memory.
INTERVAL=${1:-2}
DURATION=${2:-60}
CPU_LIMIT=${CPU_LIMIT:-0.5}   # must match deploy.resources.limits.cpus in docker-compose.yml
END=$((SECONDS + DURATION))

printf '%-11s %-9s %-10s %-9s %-11s %-15s %-9s %-19s %s\n' \
  DATE TIME APP STATE DOCKER_HLTH APP_HEALTH CPU_%LIMIT MEM_USED/LIMIT MEM_%
while [ $SECONDS -lt $END ]; do
  unset CPU MEM MEMP; declare -A CPU MEM MEMP
  while IFS='|' read -r n c m p; do
    CPU[$n]=${c%\%}; MEM[$n]=$m; MEMP[$n]=$p
  done < <(docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}')

  d=$(date +%F); t=$(date +%T)
  for n in $(docker ps -a --filter label=com.docker.compose.service=app --format '{{.Names}}' | sort); do
    read -r st hl ip < <(docker inspect -f \
      '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$n")
    if [ "$st" = "running" ]; then
      code=$(curl -s -o /dev/null --max-time 1 -w '%{http_code}' "http://$ip:5000/health")
      if [ "$code" = "200" ]; then app="HEALTHY"; else app="UNHEALTHY($code)"; fi
      cpu=$(awk -v c="${CPU[$n]:-0}" -v l="$CPU_LIMIT" 'BEGIN{printf "%.1f", c/l}')
      mem=${MEM[$n]:--}; memp=${MEMP[$n]:--}
      hl_show=$hl
    else
      app="DOWN"; cpu="-"; mem="-"; memp="-"; hl_show="-"
    fi
    mark=""; [ "$app" != "HEALTHY" ] && mark="<-- ATTENTION"
    printf '%-11s %-9s %-10s %-9s %-11s %-15s %-9s %-19s %s %s\n' \
      "$d" "$t" "$n" "$st" "$hl_show" "$app" "$cpu" "$mem" "$memp" "$mark"
  done
  echo "-"
  sleep "$INTERVAL"
done
