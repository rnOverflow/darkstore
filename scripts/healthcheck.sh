#!/usr/bin/env bash
# Probe every infra component from the host. Exit non-zero if any is down.
set -u
fail=0
check() { # name, command...
  local name=$1; shift
  local i
  for i in $(seq 1 "${HEALTH_RETRIES:-30}"); do  # tempo needs ~15s ring warm-up
    if "$@" >/dev/null 2>&1; then printf "  OK    %s\n" "$name"; return; fi
    sleep 2
  done
  printf "  FAIL  %s\n" "$name"; fail=1
}
echo "DarkStore infra health:"
check "redpanda (kafka admin)"   curl -sf http://localhost:9644/v1/status/ready
check "schema registry"          curl -sf http://localhost:8081/subjects
check "redpanda console"         curl -sf http://localhost:8080/admin/health
check "postgres"                 docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-darkstore}"
check "redis"                    docker compose exec -T redis redis-cli ping
check "otel collector"           curl -sf http://localhost:13133/
check "tempo"                    curl -sf http://localhost:3200/ready
check "loki"                     curl -sf http://localhost:3100/ready
check "prometheus"               curl -sf http://localhost:9090/-/ready
check "grafana"                  curl -sf http://localhost:3000/api/health
check "order-service"             curl -sf http://localhost:8001/healthz
check "mlflow"                   curl -sf http://localhost:5001/health
exit $fail
