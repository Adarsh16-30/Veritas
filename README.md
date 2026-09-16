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
| 1 · Real environment foundation | complete |
| **2 · Baseline agent (no verifier)** | happy path complete; baseline corpus deferred to Phase 4 |
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
uv run python scripts/setup_agent_erp.py          # idempotency-key custom field on the buying chain
uv run python scripts/apply_schema.py             # agent state tables in Postgres

uv run python scripts/handrun_buying_chain.py     # un-agented control: the chain driven by hand
uv run python scripts/run_workflow.py --workflow-id wf-001 --show-trace   # the baseline agent
```

The agent needs two local models: an executor, and an **independent verifier**
from a different model family (Rule 3 — two tags of the same weights agreeing
with each other is not verification). Install [Ollama](https://ollama.com),
start it, and pull both:

```bash
ollama pull llama3:8b-instruct-q4_K_M    # executor  — or set OLLAMA_MODEL
ollama pull qwen2.5:7b-instruct-q4_K_M   # verifier  — or set VERIFIER_MODEL
```

Leave `VERIFIER_MODEL` unset and `verify.gate.select_verifier_model` picks an
installed model of another family; `verify.verifier.Verifier` refuses to be
built if the result is not actually independent.

`scripts/handrun_buying_chain.py` is the Phase 1 success check: one command
produces a full S1–S6 document chain and prints the resulting GL entries.
`scripts/run_workflow.py` is the Phase 2 equivalent, driven by the agent — every
action from a real model call, every write idempotent, state checkpointed before
each side effect. Re-running the same `--workflow-id` resumes it and posts
nothing new.

Phase 3 adds the verification gate: deterministic accounting invariants plus a
fresh read of the real ledger, then an independent verifier that never sees the
executor's reasoning, then a conformal prediction region that routes the step.
The gate is one constructor argument — `Pipeline(gate=None)` is the Phase 2
baseline and stays runnable, because Rule 4 needs an honest denominator.

```bash
uv run python scripts/calibrate.py            # fit the conformal router (needs Phase 4 labels)
uv run python scripts/independence_report.py  # Rule 3 evidence from recorded runs
```

Both refuse to emit a number they cannot support. See `docs/limitations.md` for
exactly what Phase 3 does *not* yet measure.

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

