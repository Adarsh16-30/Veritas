#!/usr/bin/env bash
# Blocks until the ERPNext stack answers, or times out.
set -uo pipefail
URL="${ERPNEXT_URL:-http://localhost:8080}"
DEADLINE=$(( $(date +%s) + ${1:-600} ))   # default 10 min

echo "waiting for ERPNext at $URL ..."
while : ; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/api/method/ping" || true)
  if [ "$code" = "200" ]; then
    echo "ERPNext is up ($URL/api/method/ping -> 200)"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "timed out waiting for ERPNext (last http_code=$code)" >&2
    echo "check: docker compose -f infra/docker-compose/stack.yml logs erpnext-create-site erpnext-backend" >&2
    exit 1
  fi
  sleep 5
done
