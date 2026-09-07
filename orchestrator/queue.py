"""Redis work queue and per-workflow advisory lock (PRD §2.5).

The lock is what makes "one owner per workflow" true, which in turn is what lets
``submit_once`` be safe without a distributed transaction: two workers can never
be inside the same workflow's side effect at the same time. (Even if they were,
the ``commits`` primary key still admits exactly one row — the lock is the first
line, not the only one.)

No unbounded loop lives here. A worker runs until it is told to stop or until it
has drained its budget of workflows (Rule 7).
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import redis

QUEUE_KEY = "veritas:workflows"
LOCK_PREFIX = "veritas:lock:"


class LockNotAcquired(RuntimeError):
    pass


class WorkQueue:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("REDIS_URL", "redis://localhost:6380/0")
        self.r = redis.Redis.from_url(self.url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except redis.RedisError:
            return False

    def enqueue(self, workflow_id: str) -> None:
        self.r.lpush(QUEUE_KEY, workflow_id)

    def depth(self) -> int:
        return int(self.r.llen(QUEUE_KEY))

    def dequeue(self, timeout: int = 5) -> str | None:
        item = self.r.brpop([QUEUE_KEY], timeout=timeout)
        return str(item[1]) if item else None

    @contextmanager
    def lock(self, workflow_id: str, ttl_seconds: int = 900) -> Iterator[None]:
        key = f"{LOCK_PREFIX}{workflow_id}"
        token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        if not self.r.set(key, token, nx=True, ex=ttl_seconds):
            raise LockNotAcquired(f"workflow {workflow_id} is already owned by another worker")
        try:
            yield
        finally:
            # Release only if we still hold it; a lock that expired mid-run belongs
            # to whoever took it next.
            self.r.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1])"
                " else return 0 end",
                1,
                key,
                token,
            )


class Worker:
    """Pulls workflow ids and runs them. Stateless: scale by running more of these."""

    def __init__(self, queue: WorkQueue, run_workflow: Callable[[str], object]) -> None:
        self.queue = queue
        self.run_workflow = run_workflow
        self.processed = 0

    def run(
        self,
        stop: threading.Event | None = None,
        max_workflows: int | None = None,
        poll_timeout: int = 5,
    ) -> int:
        """Drain the queue until stopped or until ``max_workflows`` are done."""
        stop = stop or threading.Event()
        while not stop.is_set() and (max_workflows is None or self.processed < max_workflows):
            workflow_id = self.queue.dequeue(timeout=poll_timeout)
            if workflow_id is None:
                continue
            try:
                with self.queue.lock(workflow_id):
                    self.run_workflow(workflow_id)
                self.processed += 1
            except LockNotAcquired:
                # Someone else owns it; drop it rather than contend.
                continue
        return self.processed
