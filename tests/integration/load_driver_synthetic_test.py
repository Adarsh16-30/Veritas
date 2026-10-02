"""The load driver over a real Redis queue and lock (synthetic workflows).

A throwaway ``redis-server`` is started on a free port; the workflow runner is a
synthetic sleep, so what is under test is the driver and the queue: every
workflow runs exactly once, no more than ``workers`` run at a time, the bound
is actually reached, and a queue holding someone else's work is refused.
Skipped when no ``redis-server`` binary is installed.
"""

from __future__ import annotations

import random
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator

import pytest

from bench.load import run_load
from orchestrator.queue import WorkQueue

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="no redis-server")


@pytest.fixture
def queue() -> Iterator[WorkQueue]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    q = WorkQueue(f"redis://127.0.0.1:{port}/0")
    for _ in range(50):
        if q.ping():
            break
        time.sleep(0.1)
    try:
        yield q
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_fifty_workflows_four_workers_each_run_once(queue: WorkQueue) -> None:
    ran: list[str] = []
    lock = threading.Lock()
    rng = random.Random(3)

    def make_runner():  # type: ignore[no-untyped-def]
        def run(workflow_id: str) -> str:
            time.sleep(rng.uniform(0.01, 0.05))
            with lock:
                ran.append(workflow_id)
            return "completed"

        return run

    ids = [f"t1-load-clean-{i:03d}" for i in range(50)]
    s = run_load(ids, queue, make_runner, workers=4, timeout_s=60, poll_timeout=1)
    assert sorted(ran) == sorted(ids)
    assert s["finished"] == 50 and s["unfinished"] == 0
    assert s["run_more_than_once"] == [] and s["errors"] == []
    assert s["max_in_flight"] == 50
    assert s["max_executing"] == 4  # bounded by the workers, and the bound is reached
    assert s["status_counts"] == {"completed": 50}
    assert s["latency_e2e_s"]["p95"] >= s["latency_service_s"]["p95"]


def test_a_crashing_workflow_is_recorded_not_lost(queue: WorkQueue) -> None:
    def make_runner():  # type: ignore[no-untyped-def]
        def run(workflow_id: str) -> str:
            if workflow_id.endswith("002"):
                raise RuntimeError("erp went away")
            return "completed"

        return run

    ids = [f"t2-load-clean-{i:03d}" for i in range(5)]
    s = run_load(ids, queue, make_runner, workers=2, timeout_s=30, poll_timeout=1)
    assert s["finished"] == 5
    assert s["errors"] == ["t2-load-clean-002"]
    assert s["status_counts"] == {"completed": 4, "failed": 1}


def test_refuses_a_queue_that_already_holds_work(queue: WorkQueue) -> None:
    queue.enqueue("someone-elses-workflow")
    with pytest.raises(RuntimeError, match="already holds"):
        run_load(["t3-load-clean-000"], queue, lambda: lambda w: "completed", workers=1)
