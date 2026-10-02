"""Sustained-concurrency load test (PRD §1: ≥ 50 concurrent workflows, 4 workers).

    uv run python -m bench.load --workflows 50 --workers 4 --config baseline \\
        --run-tag l1 --out results/load_results.json

METRICS §6.3 defines ``max_concurrent`` as the largest number of concurrently
in-flight workflows completed without error growth or a latency-p95 breach,
with 4 workers. Here all N workflows are enqueued at once, so N are in flight
from the first second, and 4 stateless workers drain them through the real
Redis queue and per-workflow lock (``orchestrator.queue``). Each worker owns
its own Postgres connection, ERP client and model client; none of them is
thread-safe to share.

Workload: N **clean** workflows built from real award records (Rule 8), so
every one should complete. A load test measures whether the system holds up,
not detection, and a fault corpus would mix the two.

Recorded per workflow: queue wait, service time and end-to-end latency
(enqueue to terminal), its status, and how many times it was run. A workflow
run twice, or a second document under one idempotency key, is a Rule 5 / Rule 6
failure under load and is reported as such, never averaged away.

What this does not do: reconcile the ledger. Run ``bench.reconcile_ledger``
afterwards, as after any benchmark.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

#: A worker's way of running one workflow to its end; returns the final status.
Runner = Callable[[str], str]


@dataclass
class _Entry:
    enqueued: float
    started: float | None = None
    finished: float | None = None
    status: str | None = None
    error: str | None = None
    runs: int = 0


@dataclass
class LoadRecorder:
    """Thread-safe record of every workflow's life through the queue."""

    entries: dict[str, _Entry] = field(default_factory=dict)
    max_executing: int = 0
    _executing: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def enqueued(self, workflow_id: str) -> None:
        with self._lock:
            self.entries[workflow_id] = _Entry(enqueued=time.monotonic())

    def wrap(self, run: Runner) -> Callable[[str], object]:
        def recorded(workflow_id: str) -> object:
            with self._lock:
                entry = self.entries.get(workflow_id)
                if entry is None:  # not part of this load set
                    raise KeyError(f"unexpected workflow on the queue: {workflow_id}")
                entry.runs += 1
                entry.started = entry.started or time.monotonic()
                self._executing += 1
                self.max_executing = max(self.max_executing, self._executing)
            status, error = "failed", None
            try:
                status = run(workflow_id)
            except Exception as e:  # noqa: BLE001 — a crash under load is a result
                error = f"{type(e).__name__}: {e}"[:300]
            finally:
                with self._lock:
                    self._executing -= 1
                    entry.finished = time.monotonic()
                    entry.status, entry.error = status, error
            return status

        return recorded

    def done(self) -> int:
        with self._lock:
            return sum(1 for e in self.entries.values() if e.finished is not None)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarise(rec: LoadRecorder, workers: int, wall_s: float) -> dict[str, Any]:
    entries = rec.entries
    finished = [e for e in entries.values() if e.finished is not None]
    e2e = [e.finished - e.enqueued for e in finished if e.finished is not None]
    service = [e.finished - e.started for e in finished if e.finished is not None and e.started]
    wait = [e.started - e.enqueued for e in finished if e.started is not None]
    by_status: dict[str, int] = {}
    for e in finished:
        by_status[str(e.status)] = by_status.get(str(e.status), 0) + 1
    return {
        "workflows": len(entries),
        "workers": workers,
        "finished": len(finished),
        "unfinished": len(entries) - len(finished),
        "status_counts": by_status,
        "errors": sorted(w for w, e in entries.items() if e.error),
        "error_messages": sorted({e.error for e in entries.values() if e.error})[:10],
        "run_more_than_once": sorted(w for w, e in entries.items() if e.runs > 1),
        "max_in_flight": len(entries),
        "max_executing": rec.max_executing,
        "wall_clock_s": round(wall_s, 1),
        "throughput_per_min": round(len(finished) / wall_s * 60, 2) if wall_s else None,
        "latency_e2e_s": {
            "p50": _pct(e2e, 0.5),
            "p95": _pct(e2e, 0.95),
            "mean": statistics.fmean(e2e) if e2e else None,
        },
        "latency_service_s": {"p50": _pct(service, 0.5), "p95": _pct(service, 0.95)},
        "queue_wait_s": {"p50": _pct(wait, 0.5), "p95": _pct(wait, 0.95)},
    }


def run_load(
    workflow_ids: list[str],
    queue: Any,
    make_runner: Callable[[], Runner],
    workers: int = 4,
    timeout_s: float = 6 * 3600,
    poll_timeout: int = 1,
) -> dict[str, Any]:
    """Enqueue every workflow at once, drain with ``workers`` threads, summarise."""
    from orchestrator.queue import Worker

    if queue.depth():
        raise RuntimeError(
            f"the work queue already holds {queue.depth()} item(s); a load test must "
            "start from an empty queue or it measures someone else's work"
        )
    rec = LoadRecorder()
    for wid in workflow_ids:
        rec.enqueued(wid)
        queue.enqueue(wid)

    stop = threading.Event()
    started = time.monotonic()

    def work() -> None:
        Worker(queue, rec.wrap(make_runner())).run(stop=stop, poll_timeout=poll_timeout)

    threads = [threading.Thread(target=work, name=f"worker-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    while rec.done() < len(workflow_ids) and time.monotonic() - started < timeout_s:
        time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=poll_timeout + 5)
    return summarise(rec, workers, time.monotonic() - started)


def load_cases(n: int, run_tag: str, seed: int) -> list[Any]:
    """N clean workflows on real award records (Rule 8)."""
    from data.corpus import load_records
    from harness.corpus import APPROVAL_THRESHOLD, WorkflowCase
    from harness.faults import INJECTORS, base_spec

    records = [r for r in load_records() if r.has_description and not r.is_code_only]
    random.Random(seed).shuffle(records)
    cases = []
    for i in range(n):
        record = records[i % len(records)]
        wid = f"{run_tag}-load-clean-{i:03d}"
        spec = base_spec(wid, record, APPROVAL_THRESHOLD)
        cases.append(WorkflowCase(wid, "clean", record, INJECTORS["clean"](spec, record)))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflows", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--config", choices=("baseline", "verified"), default="baseline")
    parser.add_argument("--run-tag", required=True, help="a fresh tag, e.g. l1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="results/load_results.json")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    from agent.executor import Executor, OllamaLLM
    from agent.pipeline import Policy
    from bench.conditions import EVIDENCE_VERSION
    from bench.run import _commit_sha, tag_conflict
    from data.corpus import seed
    from erp.scoped import agent_client, describe
    from harness.corpus import persist_labels
    from orchestrator.db import Store
    from orchestrator.machine import WorkflowMachine
    from orchestrator.queue import WorkQueue
    from verify.gate import build_gate

    queue = WorkQueue()
    if not queue.ping():
        print(f"Redis not reachable at {queue.url}", file=sys.stderr)
        return 1
    erp = agent_client()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1

    cases = load_cases(args.workflows, args.run_tag, args.seed)
    by_id = {c.workflow_id: c for c in cases}
    with Store() as store:
        conflict = tag_conflict(store, args.run_tag, resume=False)
        if conflict:
            print(f"refusing to start: {conflict}", file=sys.stderr)
            return 2
        seed(erp, list({c.record.item_code: c.record for c in cases}.values()))
        persist_labels(store, cases)

    verified = args.config == "verified"
    policy = Policy.verified() if verified else Policy()

    def make_runner() -> Runner:
        store = Store()  # one connection per worker: Store is not thread-safe
        worker_erp = agent_client()
        llm = OllamaLLM()

        def run(workflow_id: str) -> str:
            case = by_id[workflow_id]
            gate = build_gate(worker_erp, case.spec, executor_model=llm.name) if verified else None
            machine = WorkflowMachine(
                store, worker_erp, case.spec, Executor(llm), policy=policy, gate=gate
            )
            return machine.run(workflow_id).status.value

        return run

    print(
        f"load: {args.workflows} {args.config} workflows, {args.workers} workers, "
        f"tag {args.run_tag}, erp access: {describe(erp)}",
        flush=True,
    )
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    summary = run_load(list(by_id), queue, make_runner, workers=args.workers)

    with Store() as store:
        dup = store.conn.execute(
            "SELECT doc_name, count(*) AS n FROM commits WHERE workflow_id LIKE %s"
            " GROUP BY doc_name HAVING count(*) > 1",
            (f"{args.run_tag}-load-%",),
        ).fetchall()
        calls = store.conn.execute(
            "SELECT max(llm_calls) AS m FROM workflows WHERE workflow_id LIKE %s",
            (f"{args.run_tag}-load-%",),
        ).fetchone()
    cap = policy.max_llm_calls_per_workflow
    summary.update(
        {
            "config": args.config,
            "run_tag": args.run_tag,
            "evidence_version": EVIDENCE_VERSION,
            "erp_access": describe(erp),
            "commit": _commit_sha(),
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "documents_committed_twice": [dict(r) for r in dup],
            "max_llm_calls": calls["m"] if calls else None,
            "llm_call_cap": cap,
        }
    )
    ok = (
        summary["unfinished"] == 0
        and not summary["errors"]
        and not summary["run_more_than_once"]
        and not dup
        and (summary["max_llm_calls"] or 0) <= cap
    )
    summary["held_up"] = ok

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "finished",
                    "status_counts",
                    "errors",
                    "latency_e2e_s",
                    "max_executing",
                    "held_up",
                )
            },
            indent=2,
            default=str,
        )
    )
    print(f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
