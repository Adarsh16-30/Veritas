"""The VERITAS queue worker (production hardening, B6).

    uv run python -m service.worker

Runs the **verified** workflow for every request the API accepted:
- the three-gate pipeline;
- the step-scoped ERP identity when a buyer key is configured (mandatory in
  production, orchestrator/settings.py);
- models from ``MODEL_BACKEND`` (agent/llm.py).

What keeps it safe to run more than one:

* The per-workflow **lease** (orchestrator/lease.py): one owner at a time, and
  no write after ownership is lost.
* **Deferral, not escalation, on outages** (orchestrator/queue.py), plus a
  Redis-shared **circuit breaker** per dependency (erp/resilience.py). Once
  ERPNext or the model server has failed ``BREAKER_FAILURES`` times in a row,
  every worker defers at once for ``BREAKER_COOLDOWN_S``, instead of each
  discovering the outage through its own timeout.
* **Graceful stop.** SIGTERM stops dequeuing; the workflow in hand finishes or
  stops at its next checkpoint, and the lease is released. A hard kill is safe
  too (Rule 6): the next owner resumes from the checkpoint.
* **Nothing stranded.** On start, every request still pending or mid-run is
  re-enqueued. Duplicates are harmless: the lease drops a second copy, and a
  finished workflow is skipped.

Memory: one thread by default (``WORKER_CONCURRENCY=1``). The models serialise
on the laptop anyway, and every extra thread holds its own Postgres connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from agent.context import WorkflowSpec
from agent.executor import Executor, ModelUnavailable
from agent.llm import family_of, make_llm
from agent.pipeline import Policy
from agent.state import Status, Step
from erp.client import ERPUnavailable
from erp.resilience import CircuitBreaker, InfrastructureUnavailable
from erp.scoped import agent_client, describe
from orchestrator.db import Store
from orchestrator.machine import WorkflowMachine
from orchestrator.queue import DEAD_KEY, Worker, WorkQueue
from orchestrator.settings import Settings, load_settings
from service.store import RUNNABLE, unconsumed_approval
from trace.logging import log_context, setup_logging
from verify.gate import build_gate

_log = logging.getLogger("veritas.worker")


def spec_from_request(workflow_id: str, request: dict[str, Any]) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        item_code=request["item_code"],
        qty=float(request["qty"]),
        supplier=request["supplier"],
        rate=float(request["rate"]),
        needed_by=str(request["needed_by"]),
        bill_no=request["bill_no"],
        tolerance_pct=float(request.get("tolerance_pct", 2.0)),
        approval_threshold=float(request.get("approval_threshold", 10_000.0)),
        received_qty=request.get("received_qty"),
        invoice_rate=request.get("invoice_rate"),
    )


class WorkflowRunner:
    """Runs one workflow at a time; one per worker thread (``Store`` is not thread-safe)."""

    def __init__(self, settings: Settings, breakers: dict[str, CircuitBreaker]) -> None:
        self.settings = settings
        self.breakers = breakers
        self.store = Store(settings.postgres_dsn())
        self.erp = agent_client()
        self.executor_llm = make_llm("executor")
        self._verifier_llm: Any = None

    @property
    def verifier_llm(self) -> Any:
        # Built on first use: choosing an Ollama verifier asks the model server
        # which models are installed, and the worker must start while it is down.
        if self._verifier_llm is None:
            self._verifier_llm = make_llm("verifier", executor_model=self.executor_llm.name)
        return self._verifier_llm

    def __call__(self, workflow_id: str) -> str:
        try:
            return self._run(workflow_id)
        except psycopg.OperationalError as e:
            # Postgres restarted or is unreachable: an outage, not a decision.
            # The connection is unusable now, so replace it before the next run.
            self._reconnect()
            raise InfrastructureUnavailable(f"postgres unavailable: {e}") from e

    def _reconnect(self) -> None:
        with contextlib.suppress(Exception):
            self.store.close()
        # Still down: the next run reconnects again.
        with contextlib.suppress(psycopg.OperationalError):
            self.store = Store(self.settings.postgres_dsn())

    def _run(self, workflow_id: str) -> str:
        if self.store.conn.closed:
            self._reconnect()
        for breaker in self.breakers.values():
            if breaker.is_open():
                raise InfrastructureUnavailable(
                    f"{breaker.name} circuit open", retry_after=float(breaker.cooldown_s)
                )
        with log_context(workflow_id=workflow_id):
            wf = self.store.get_workflow(workflow_id)
            row = self.store.conn.execute(
                "SELECT request FROM workflow_requests WHERE workflow_id = %s", (workflow_id,)
            ).fetchone()
            if wf is None or row is None:
                _log.warning("no such request; dropping")
                return "missing"
            if wf["status"] not in RUNNABLE:
                return str(wf["status"])  # finished, rejected, or waiting on a human
            request = (
                row["request"] if isinstance(row["request"], dict) else json.loads(row["request"])
            )
            spec = spec_from_request(workflow_id, request)
            try:
                gate = build_gate(
                    self.erp,
                    spec,
                    executor_model=self.executor_llm.name,
                    verifier_llm=self.verifier_llm,
                    executor_family=family_of(self.executor_llm),
                )
                machine = WorkflowMachine(
                    self.store,
                    self.erp,
                    spec,
                    Executor(self.executor_llm),
                    policy=Policy.verified(),
                    gate=gate,
                    override_for=self.approval,
                )
                outcome = machine.run(workflow_id)
            except ERPUnavailable:
                self.breakers["erp"].record_failure()
                raise
            except ModelUnavailable:
                self.breakers["model"].record_failure()
                raise
            for breaker in self.breakers.values():
                breaker.record_success()
            _log.info("workflow %s", outcome.status.value, extra={"reason": outcome.reason})
            return outcome.status.value

    def approval(self, workflow_id: str, step: Step) -> int | None:
        return unconsumed_approval(self.store.conn, workflow_id, step.value)

    def give_up(self, workflow_id: str, reason: str) -> None:
        """Rule 7: an outage that outlasted every deferral, or a fault in the
        code, goes to a human."""
        self.store.set_status(workflow_id, Status.ESCALATED.value, reason[:500])


class WorkerMetrics(Collector):
    def __init__(
        self, queue: WorkQueue, workers: list[Worker], breakers: dict[str, CircuitBreaker]
    ):
        self.queue, self.workers, self.breakers = queue, workers, breakers

    def collect(self) -> Any:
        g = GaugeMetricFamily("veritas_queue", "Work queue sizes.", labels=["queue"])
        try:
            g.add_metric(["ready"], self.queue.depth())
            g.add_metric(["delayed"], self.queue.delayed())
            g.add_metric(["dead"], int(self.queue.r.hlen(DEAD_KEY)))
        except Exception:  # noqa: BLE001 - a scrape must not fail on Redis
            pass
        yield g
        c = GaugeMetricFamily(
            "veritas_worker_workflows", "Workflows by outcome.", labels=["outcome"]
        )
        for name in ("processed", "deferred", "gave_up", "leases_lost", "redis_errors", "failed"):
            c.add_metric([name], sum(getattr(w, name) for w in self.workers))
        yield c
        b = GaugeMetricFamily("veritas_breaker_open", "1 while a breaker is open.", labels=["name"])
        for name, breaker in self.breakers.items():
            with contextlib.suppress(Exception):  # a scrape must not fail on Redis
                b.add_metric([name], 1.0 if breaker.is_open() else 0.0)
        yield b


def serve_health(
    settings: Settings, threads: list[threading.Thread], registry: CollectorRegistry
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path == "/healthz":
                alive = all(t.is_alive() for t in threads)
                self._send(200 if alive else 503, b'{"alive": %s}' % str(alive).lower().encode())
            elif self.path == "/metrics":
                self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)
            else:
                self._send(404, b"{}")

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(
        (settings.worker_health_host, settings.worker_health_port), Handler
    )
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server


def requeue_runnable(settings: Settings, queue: WorkQueue) -> int:
    """Enqueue every request still pending or mid-run; return how many.

    Covers a submit whose enqueue failed after its row was committed, and a
    workflow popped from the queue just before Redis failed. Duplicates are
    harmless: the lease drops a second copy and a finished workflow is skipped.
    """
    with Store(settings.postgres_dsn()) as store:
        rows = store.conn.execute(
            "SELECT w.workflow_id FROM workflows w JOIN workflow_requests r USING (workflow_id)"
            " WHERE w.status = ANY(%s)",
            (list(RUNNABLE),),
        ).fetchall()
    for row in rows:
        queue.enqueue(row["workflow_id"])
    return len(rows)


def sweep(settings: Settings, queue: WorkQueue, stop: threading.Event) -> None:
    while not stop.wait(settings.requeue_interval_s):
        try:
            requeue_runnable(settings, queue)
        except Exception as e:  # noqa: BLE001 - the sweep retries on its next tick
            _log.warning("requeue sweep failed", extra={"error": str(e)})


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_json)
    access = describe(agent_client())
    if settings.veritas_env == "production" and not access.startswith("step-scoped"):
        _log.error("refusing to start: production requires step-scoped ERP access")
        return 2

    queue = WorkQueue(settings.redis_url.get_secret_value())
    breakers = {
        name: CircuitBreaker(queue.r, name, settings.breaker_failures, settings.breaker_cooldown_s)
        for name in ("erp", "model")
    }
    stop = threading.Event()
    stranded = requeue_runnable(settings, queue)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    workers: list[Worker] = []
    threads: list[threading.Thread] = []
    for i in range(settings.worker_concurrency):
        runner = WorkflowRunner(settings, breakers)
        worker = Worker(
            queue,
            runner,
            lock_ttl_s=settings.lock_ttl_s,
            lock_renew_s=settings.lock_renew_s,
            max_deferrals=settings.max_deferrals,
            defer_base_s=settings.defer_base_s,
            on_give_up=runner.give_up,
        )
        workers.append(worker)
        threads.append(
            threading.Thread(
                target=worker.run,
                kwargs={"stop": stop, "poll_timeout": settings.worker_poll_s},
                name=f"worker-{i}",
            )
        )
    registry = CollectorRegistry()
    registry.register(WorkerMetrics(queue, workers, breakers))
    server = serve_health(settings, threads, registry)
    _log.info(
        "worker started",
        extra={"threads": len(threads), "erp_access": access, "requeued": stranded},
    )
    for t in threads:
        t.start()
    threading.Thread(
        target=sweep, args=(settings, queue, stop), name="requeue-sweep", daemon=True
    ).start()
    for t in threads:
        t.join()
    server.shutdown()
    _log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
