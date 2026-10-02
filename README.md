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
| 2 · Baseline agent (no verifier) | complete — `Pipeline(gate=None)` |
| 3 · Verification gate | complete except ECE ≤ 0.05 (no calibration fitted yet) |
| **4 · Fault-injection benchmark** | built; the v4 re-run (evidence version 1) is being completed |
| 5 · Trace explorer | built — `scripts/trace_explorer.py`; records each step's ERP reads from evidence version 2 |
| 6 · Hardening & observability | built, measurement pending — metrics + Grafana, step-scoped ERP access, untrusted-text quarantine, load-test driver; each needs a live run (see `docs/limitations.md`) |
| 7 · End-to-end demo & writeup | demo built (`scripts/run_demo.sh`); writeup waits for measured v4/v5 numbers |

Every number this project reports comes from a results file and is cited in
[`docs/results.md`](docs/results.md); nothing on this page is a measurement.

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
uv run python scripts/calibrate.py            # fit the conformal router
uv run python scripts/independence_report.py  # Rule 3 evidence from recorded runs
```

Both refuse to emit a number they cannot support.

## The benchmark

Phase 4 measures the two configurations against a faulted corpus built from
**real** U.S. federal contract awards (USAspending.gov — source, exact request
and sha256 recorded in `data/dataset_manifest.json`, Rule 8). The seven fault
classes of PRD §6.3 are injected as real ERPNext documents and real procurement
requests; ground truth comes from the injector that created each condition, never
from what the agent did.

```bash
bash scripts/run_benchmark.sh     # corpus -> baseline -> calibrate -> verified -> report
```

That runs both configurations, fits the conformal router on a split sharing zero
workflow IDs with the benchmark (Rule 9), reconciles the real general ledger, and
writes `docs/results.md` with a `[results: ...]` citation on every number
(Rule 10). It refuses to report a verified-vs-baseline delta unless
`results/baseline_results.json` exists (Rule 4) — there is no flag to override
that.

Individual stages, if you want them separately:

```bash
uv run python -m data.usaspending --limit 200            # fetch + checksum the corpus
uv run python -m bench.run --config baseline             # the Rule 4 denominator
uv run python -m bench.run --config verified             # the three-gate path
uv run python -m bench.reconcile_ledger                  # ask the real GL what happened
uv run python -m bench.report                            # write docs/results.md
```

## Trace explorer

```bash
uv run python scripts/trace_explorer.py    # http://127.0.0.1:8765, read-only
```

Pick any recorded workflow and see, step by step, what the agent was shown,
what it proposed and why, what the independent verifier objected to (it never
sees that rationale), which invariant fired, and what committed. Each DELTA fact
links to the ERPNext document it was computed from; a step that retried gets a
diff of exactly what changed between attempts.

Measured results live in `docs/results.md`; what those numbers do *not* cover
lives in `docs/limitations.md`, which is worth reading first.

## Observability

```bash
uv run python scripts/metrics_exporter.py                        # :9108/metrics, read-only
docker compose -f infra/docker-compose/observability.yml up -d    # Prometheus :9090, Grafana :3000
```

The `VERITAS — reliability` dashboard shows the recorded benchmark (compounding
curve, headline rates, detection by fault class, each bar marked complete or
incomplete, with a source table naming the results file and commit) beside the
live agent state (Rule 7's escalation and retry counters, budget violations,
model-call latency, routes per step) and the conformal router's calibration
status.

## Least privilege

```bash
uv run python scripts/generate_scoped_api_key.py --buyer   # S1–S3 identity, no Accounts role
```

With the buyer key in `.env`, S1–S3 run on an identity ERPNext refuses Payment
Entry access, and every step may write only its own documents (`erp/scoped.py`).
Without it the agent runs on the single scoped key, as before.

## Demo

```bash
bash scripts/run_demo.sh
```

Brings up the stack, runs the hand-driven S1–S6 control, then replays five
fault classes (clean, duplicate bill, compounding, back-dated, indirect
injection) through the baseline and the verified configuration side by side,
with trace-explorer links. It is a walkthrough, n = 1 per class, and says so.

## Load test

```bash
uv run python -m bench.load --workflows 50 --workers 4 --config baseline --run-tag l1
```

Enqueues 50 workflows at once and drains them with 4 workers through the real
queue and per-workflow lock (PRD §1). It reports latency, queue wait and
throughput, and fails if any workflow ran twice, any document was committed
twice, or any workflow broke its model-call cap.

## Experimental conditions

A delta is the gate's effect only if nothing else changed. Every results file
records its `evidence_version` (`bench/conditions.py`, bumped whenever what
the models see or how the corpus is built changes) and its ERP access model.
`bench.run` will not resume a run under different conditions, or start one on
a used run tag. The report and the chart will not compare across them.

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

