# Operations

How to run the VERITAS service, and how it behaves when its dependencies fail.
`docs/security.md` covers the threat model. Anything not yet exercised against a
live ERPNext, real models and a real deployment is listed under
"Not yet verified live" in `docs/limitations.md`.

## Processes

| Process | Command | Port | Default memory |
|---|---|---|---|
| API | `uvicorn --factory service.api:create_app_from_settings --workers 1 --limit-max-requests 10000` | 8090 | one process, 4-connection Postgres pool |
| Worker | `python -m service.worker` | 9109 (`/healthz`, `/metrics`) | `WORKER_CONCURRENCY=1` thread |
| Migrations | `python scripts/migrate.py` | — | one-shot |
| Metrics exporter (existing) | `python scripts/metrics_exporter.py` | 9108 | opt-in |
| Trace explorer (existing) | `python scripts/trace_explorer.py` | 8765 | local only, no auth: never expose it |

ERPNext keeps 8080, Postgres 5433 and Redis 6380 (`infra/docker-compose/stack.yml`).

## First run

```bash
uv run python scripts/migrate.py                       # forward-only, safe to re-run
uv run python scripts/create_api_client.py --id intake --name "Intake" --roles submitter
uv run python scripts/create_api_client.py --id ap-alice --name "Alice (AP)" --roles approver
uv run uvicorn --factory service.api:create_app_from_settings --port 8090 &
uv run python -m service.worker &
```

Use separate clients for submitting and approving: the API refuses to let a
client decide its own request.

## Memory budget (the development laptop)

The laptop already holds two ~5 GB models with ~5 GB free, so the service must
stay small. Measured in the development container on 2026-10-30, against a real
Postgres 16 and Redis 8. ERPNext and Ollama were unreachable, so the worker
exercised its outage path rather than real workflows:

| Process | Idle RSS | Peak RSS | Under |
|---|---|---|---|
| API | 88 MB | 93 MB | 3 × 400 requests from 8 threads (submit + read), p95 46–55 ms |
| Worker (1 thread) | 64 MB | 64 MB | 800 queued workflows, all deferred through an open circuit breaker |

Together that is about 160 MB, inside the 400 MB budget for new services. The
worker's figure excludes the models, which run in Ollama outside it; a real
workflow adds the verifier's prompt and the ERP reads, which are small next to
the process itself. Prometheus and Grafana (~360 MB together) stay opt-in: do
not run them during benchmarks.

## When a dependency fails

| What fails | What happens | Bound |
|---|---|---|
| ERPNext or a model server (refused, 429, 5xx) | Reads and model calls retry briefly with jittered backoff. Then the workflow is **deferred**, not escalated, and its step stays checkpointed. | `MAX_DEFERRALS` (5) with backoff from `DEFER_BASE_S` (30 s), capped at 15 min: roughly 15 minutes of outage. Then the workflow escalates with `infrastructure_unavailable`, and an approver can resume it. |
| The same, repeatedly | The shared **circuit breaker** opens after `BREAKER_FAILURES` (5) failures in a row. Every worker then defers at once for `BREAKER_COOLDOWN_S` (60 s), without calling the dependency. | Each deferral still counts toward `MAX_DEFERRALS`, so a long outage ends in the human queue rather than waiting forever (Rule 7). |
| An ERPNext write whose response was lost | Never resent blind. On resume, the idempotency key finds the document. A draft is submitted, a submitted one is adopted, and a cancelled one is refused. | — |
| Redis | The worker backs off and keeps polling, and its thread survives. A workflow caught mid-run is re-enqueued by the sweep every `REQUEUE_INTERVAL_S` (300 s) once Redis is back. The API's `/readyz` returns 503. | — |
| Postgres | The workflow defers and the worker reconnects. The API's `/readyz` returns 503. | as the first row |
| A worker dies mid-step | Its lease stops being renewed and expires within `LOCK_TTL_S` (60 s); the next owner resumes from the checkpoint. A worker that loses its lease writes nothing more. | — |
| A bug raises in a workflow | The thread survives, and the workflow goes to the human queue with reason `worker_error: …`. | — |

The worker's `/metrics` exposes these: `veritas_queue{queue=ready|delayed|dead}`,
`veritas_worker_workflows{outcome=processed|deferred|gave_up|leases_lost|redis_errors|failed}`
and `veritas_breaker_open{name=erp|model}`.

## The escalation queue

`GET /v1/escalations` lists every escalated workflow, with `approvable` and the
reason it is or is not.

| Escalation reason | `approve_and_resume` |
|---|---|
| `verifier_rejected`, `verifier_unusable`, `low_confidence` | Allowed. The step re-runs in full, and if the executor again proposes `proceed` and the hard rules pass, the approval lifts the verifier's veto. The trace records `human_override: decision N over …`. The approval is spent by the first commit that uses it. |
| `infrastructure_unavailable` | Allowed. The workflow simply resumes. |
| `hard_rule_violation` | Never. A failed accounting invariant cannot be approved into passing. |
| `floor:`, `executor_chose_*` | Never. The agent itself declined (Rule 2). |
| `llm_call_budget_exhausted`, `retry_cap_exhausted` | Never (Rule 7). |

`reject` is always available. Decisions are append-only, and record who decided
and why.

## Schema changes

Migrations are forward-only and immutable once applied, and they run one at a
time under an advisory lock. Every migration must stay compatible with the
release before it: add, never drop or rename in the same release. A rolled-back
release then runs against the newer schema; the migrator warns and carries on.
