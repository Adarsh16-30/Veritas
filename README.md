# VERITAS

**A verification-gated autonomous Procure-to-Pay agent that operates on a real
ERPNext instance and its live double-entry ledger.**

The contribution is not the P2P automation — it is the reliability layer around
it: an independent verification gate (deterministic accounting rules + a second,
independent reasoning path) checks every step *before* it commits, confidence is
conformal-calibrated, and a real fault-injection benchmark produces a
before/after compounding-failure curve. Full spec: [`VERITAS_PRD.md`](VERITAS_PRD.md).
Build rules: [`CLAUDE.md`](CLAUDE.md).

## Honest boundary

Evaluation scenarios are **constructed**, but as **real records inside a real
ERP** seeded from cited public procurement data. A duplicate invoice posted to
the real ledger is a real duplicate invoice. See [`docs/limitations.md`](docs/limitations.md).

## Status

| Phase | State |
|---|---|
| **1 · Real environment foundation** | in progress |
| 2 · Baseline agent (no verifier) | not started |
| 3 · Verification gate | not started |
| 4 · Fault-injection benchmark | not started |
| 5 · Trace explorer | not started |
| 6 · Hardening & observability | not started |
| 7 · End-to-end demo & writeup | not started |

## Quick start

Requires Docker Desktop (WSL2 VM given ≥ 4 GB) and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --group dev

# real ERPNext + agent Postgres + agent Redis (see infra/README.md)
docker compose -f infra/docker-compose/stack.yml up -d
bash scripts/wait_for_erpnext.sh

cp .env.example .env
uv run python scripts/bootstrap_erpnext.py         # company, fiscal year, CoA, warehouse, cost center, supplier, item
uv run python scripts/generate_scoped_api_key.py   # scoped agent user (NOT System Manager) -> writes key/secret to .env
uv run python scripts/handrun_buying_chain.py      # Material Request -> PO -> Receipt -> Invoice -> Payment, all submitted; prints the GL entries moved
```

`scripts/handrun_buying_chain.py` is the Phase 1 success check: one command
produces a full S1–S6 document chain and prints the resulting GL entries.

## Repo layout

`erp/` ERPNext REST client · `agent/` context + executor · `verify/` rules +
independent verifier + conformal · `orchestrator/` durable state machine ·
`harness/` fault injector + labels · `data/` real dataset loaders · `trace/`
append-only store + query API · `bench/` runner + metrics + ledger reconciliation
· `ui/` trace explorer · `docs/` metrics, architecture, results, limitations.

## Rules

Ten non-negotiable rules (`CLAUDE.md` §1) are enforced by
`scripts/check_rules.sh` and CI. A rule violation is a build failure.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
bash scripts/check_rules.sh
```

