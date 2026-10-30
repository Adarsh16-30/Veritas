"""The renewed lease and bounded deferral, on a real Redis (synthetic workflows).

A throwaway ``redis-server`` is started on a free port. The workflows are
synthetic callables, so what is under test is the queue: a lease outlives its
TTL while the worker is alive, a lost lease refuses writes, two workers never
both commit, and an outage defers a workflow a bounded number of times before
handing it to a human. Skipped when no ``redis-server`` binary is installed.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from agent.executor import ModelUnavailable
from orchestrator.lease import LeaseLost, ensure_lease
from orchestrator.queue import (
    DEAD_KEY,
    DEFERRALS_PREFIX,
    LOCK_PREFIX,
    LockNotAcquired,
    Worker,
    WorkQueue,
)

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


def _until(cond: Callable[[], bool], timeout: float = 15) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        time.sleep(0.05)


def test_the_heartbeat_keeps_the_lock_past_its_ttl(queue: WorkQueue) -> None:
    with queue.lock("wf-1", ttl_seconds=1.0, renew_seconds=0.3) as lease:
        time.sleep(2.5)  # 2.5 x the TTL
        assert not lease.lost
        assert queue.r.get(f"{LOCK_PREFIX}wf-1") == lease.token
        with pytest.raises(LockNotAcquired), queue.lock("wf-1"):
            pass
    assert queue.r.get(f"{LOCK_PREFIX}wf-1") is None  # released on exit


def test_a_taken_over_lock_refuses_the_write(queue: WorkQueue) -> None:
    with queue.lock("wf-2", ttl_seconds=1.0, renew_seconds=0.3) as lease:
        queue.r.set(f"{LOCK_PREFIX}wf-2", "another-worker")  # expiry + takeover
        _until(lambda: lease.lost, timeout=3)
        with pytest.raises(LeaseLost):
            ensure_lease()
    # Release is compare-and-delete: the new owner's lock survives.
    assert queue.r.get(f"{LOCK_PREFIX}wf-2") == "another-worker"


def test_two_workers_never_both_commit_a_slow_workflow(queue: WorkQueue) -> None:
    """The workflow outlives the TTL; the second worker arrives after the TTL
    would have expired. Only the renewal keeps it out."""
    commits: list[str] = []

    def slow(workflow_id: str) -> None:
        time.sleep(2.0)
        ensure_lease()
        commits.append(workflow_id)

    def worker(start_after: float) -> Worker:
        w = Worker(queue, slow, lock_ttl_s=1.0, lock_renew_s=0.3)
        time.sleep(start_after)
        w.run(max_workflows=1, poll_timeout=1, stop=stop)
        return w

    stop = threading.Event()
    queue.enqueue("wf-3")
    first = threading.Thread(target=worker, args=(0.0,))
    first.start()
    time.sleep(1.4)  # past the 1 s TTL
    queue.enqueue("wf-3")  # a duplicate dispatch of the same workflow
    second = threading.Thread(target=worker, args=(0.0,))
    second.start()
    first.join(timeout=10)
    time.sleep(1.5)
    stop.set()
    second.join(timeout=5)
    assert commits == ["wf-3"]


def test_an_outage_is_deferred_then_handed_to_a_human(queue: WorkQueue) -> None:
    runs: list[float] = []

    def down(workflow_id: str) -> None:
        runs.append(time.monotonic())
        raise ModelUnavailable("ollama unreachable at http://x: refused")

    w = Worker(queue, down, max_deferrals=2, defer_base_s=0.2, defer_cap_s=0.4)
    stop = threading.Event()
    t = threading.Thread(target=w.run, kwargs={"stop": stop, "poll_timeout": 1})
    queue.enqueue("wf-4")
    t.start()
    _until(lambda: w.gave_up == 1)
    stop.set()
    t.join(timeout=5)
    assert len(runs) == 3 and w.deferred == 2  # the first run plus two deferrals
    assert all(b - a >= 0.1 for a, b in zip(runs, runs[1:], strict=False))  # it waited
    reason = queue.r.hget(DEAD_KEY, "wf-4")
    assert reason and reason.startswith("infrastructure_unavailable")
    assert queue.depth() == 0 and queue.delayed() == 0
    assert not queue.r.exists(f"{DEFERRALS_PREFIX}wf-4")


def test_a_recovered_dependency_clears_the_deferral_count(queue: WorkQueue) -> None:
    attempts: list[int] = []

    def flaky(workflow_id: str) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelUnavailable("ollama unreachable at http://x: refused")

    w = Worker(queue, flaky, defer_base_s=0.2, defer_cap_s=0.4)
    stop = threading.Event()
    t = threading.Thread(target=w.run, kwargs={"stop": stop, "poll_timeout": 1})
    queue.enqueue("wf-5")
    t.start()
    _until(lambda: w.processed == 1)
    stop.set()
    t.join(timeout=5)
    assert len(attempts) == 2 and w.gave_up == 0
    assert not queue.r.exists(f"{DEFERRALS_PREFIX}wf-5")


def test_promotion_moves_only_due_items_once(queue: WorkQueue) -> None:
    queue.defer("due-a", -1)
    queue.defer("due-b", -1)
    queue.defer("later", 60)
    assert queue.promote_due() == 2 and queue.promote_due() == 0
    assert queue.depth() == 2 and queue.delayed() == 1
