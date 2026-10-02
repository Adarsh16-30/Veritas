#!/usr/bin/env bash
# run_demo.sh — one-command end-to-end demo (PRD Phase 7).
#
#   bash scripts/run_demo.sh
#
# 1. bring up the real stack (ERPNext + agent Postgres + Redis) and wait for it
# 2. bootstrap the company, the scoped agent key, the idempotency field and the
#    agent schema -- every step idempotent, and the key is only created when
#    .env has none (re-generating it would rotate a working secret)
# 3. the Phase 1 control: a hand-run S1..S6 chain that prints the GL it moved
# 4. the scripted incident replay: five real fault classes, each run through the
#    baseline and the verified configuration, side by side (bench/demo.py).
#    A demonstration, n = 1 per class -- the measured numbers are in
#    docs/results.md.
#
# Needs Docker and Ollama with both models pulled (README). Never `down -v`:
# it wipes the real ledger.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[1/6] bringing up the real stack"
docker compose -f infra/docker-compose/stack.yml up -d
bash scripts/wait_for_erpnext.sh

echo "[2/6] bootstrap (idempotent)"
[ -f .env ] || cp .env.example .env
uv run python scripts/bootstrap_erpnext.py
if grep -qE '^ERPNEXT_API_KEY=.+' .env; then
  echo "    scoped agent key already in .env; keeping it"
else
  uv run python scripts/generate_scoped_api_key.py
fi
uv run python scripts/setup_agent_erp.py
uv run python scripts/apply_schema.py

echo "[3/6] Phase 1 control: hand-run buying chain S1..S6"
uv run python scripts/handrun_buying_chain.py

echo "[4/6] checking the models"
OLLAMA_URL="$(grep -E '^OLLAMA_URL=' .env | cut -d= -f2- || true)"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
if ! curl -fsS -m 5 "$OLLAMA_URL/api/tags" >/dev/null; then
  echo "    Ollama is not reachable at $OLLAMA_URL — start it (see README) and re-run." >&2
  exit 1
fi

echo "[5/6] scripted incident replay: baseline vs verified"
uv run python -m bench.demo

echo "[6/6] done"
echo "    inspect any workflow above:  uv run python scripts/trace_explorer.py"
echo "    live dashboard:              uv run python scripts/metrics_exporter.py &&"
echo "                                 docker compose -f infra/docker-compose/observability.yml up -d"
