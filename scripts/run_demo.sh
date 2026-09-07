#!/usr/bin/env bash
# run_demo.sh — one-command end-to-end demo.
#
# Phase 7 deliverable. Today it only does the Phase 1 part: bring up the real
# stack and drive a full S1..S6 buying chain, printing the GL entries moved.
# The verified-vs-unverified scripted-incident replay is added in Phase 7.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[1/4] bringing up the real stack"
docker compose -f infra/docker-compose/stack.yml up -d

echo "[2/4] waiting for ERPNext"
bash scripts/wait_for_erpnext.sh

echo "[3/4] bootstrap (idempotent)"
[ -f .env ] || cp .env.example .env
uv run python scripts/bootstrap_erpnext.py
uv run python scripts/generate_scoped_api_key.py

echo "[4/4] hand-run buying chain S1..S6"
uv run python scripts/handrun_buying_chain.py

echo
echo "TODO (Phase 7): replay a scripted incident through baseline vs. verified and show the delta."
