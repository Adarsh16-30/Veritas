"""Redis work queue and per-workflow advisory lock (PRD §2.5).

The lock is what makes "one owner per workflow" true, which in turn is what lets
``submit_once`` be safe without a distributed transaction: two workers can never
be inside the same workflow's side effect at the same time. (Even if they were,
the ``commits`` primary key still admits exactly one row — the lock is the first
line, not the only one.) The lock is a renewed ``Lease`` (orchestrator/lease.py):
it cannot silently expire under a slow workflow, and the pipeline checks it
immediately before every write.

An outage is not a decision. When a dependency is unavailable the worker
*defers* the workflow -- a delayed retry in a sorted set -- instead of letting it
burn its bounded retries and escalate a healthy purchase to a human. Deferrals
are bounded too: past ``max_deferrals`` the workflow is handed to a human with
reason ``infrastructure_unavailable`` (Rule 7).

No unbounded loop lives here. A worker runs until it is told to stop or until it
has drained its budget of workflows (Rule 7).
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import redis

from erp.resilience import InfrastructureUnavailable
from orchestrator.lease import Lease, LeaseLost, holding

QUEUE_KEY = "veritas:workflows"
DELAYED_KEY = "veritas:workflows:delayed"
DEAD_KEY = "veritas:workflows:dead"
LOCK_PREFIX = "veritas:lock:"
DEFERRALS_PREFIX = "veritas:deferrals:"

#: A deferral count outlives any realistic run of deferrals, then expires.
DEFERRALS_TTL_S = 24 * 3600

_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1])"
    " else return 0 end"
)

#: Move due workflows from the delayed set to the queue, atomically, so two
#: workers promoting at once can never enqueue one workflow twice.
_PROMOTE_SCRIPT = """
local due = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, id in ipairs(due) do
  redis.call('zrem', KEYS[1], id)
  redis.call('lpush', KEYS[2], id)
end
return #due
"""

_log = logging.getLogger("veritas.worker")


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

    # --- delayed retries ----------------------------------------------------------
    def defer(self, workflow_id: str, delay_s: float) -> None:
        """Run ``workflow_id`` again no sooner than ``delay_s`` from now."""
        self.r.zadd(DELAYED_KEY, {workflow_id: time.time() + delay_s})

    def delayed(self) -> int:
        return int(self.r.zcard(DELAYED_KEY))

    def promote_due(self, limit: int = 100) -> int:
        return int(self.r.eval(_PROMOTE_SCRIPT, 2, DELAYED_KEY, QUEUE_KEY, time.time(), limit))

    def count_deferral(self, workflow_id: str) -> int:
        key = f"{DEFERRALS_PREFIX}{workflow_id}"
        n = int(self.r.incr(key))
        self.r.expire(key, DEFERRALS_TTL_S)
        return n

    def clear_deferrals(self, workflow_id: str) -> None:
        self.r.delete(f"{DEFERRALS_PREFIX}{workflow_id}")

    def dead_letter(self, workflow_id: str, reason: str) -> None:
        """Record a workflow handed to a human by the queue itself."""
        self.r.hset(DEAD_KEY, workflow_id, reason[:500])

    # --- ownership ------------------------------------------------------------------
    @contextmanager
    def lock(
        self, workflow_id: str, ttl_seconds: float = 60, renew_seconds: float = 20
    ) -> Iterator[Lease]:
        key = f"{LOCK_PREFIX}{workflow_id}"
        token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        if not self.r.set(key, token, nx=True, px=int(ttl_seconds * 1000)):
            raise LockNotAcquired(f"workflow {workflow_id} is already owned by another worker")
        lease = Lease(self.r, key, token, ttl_seconds, renew_seconds)
        lease.start()
        try:
            with holding(lease):
                yield lease
        finally:
            lease.stop()
            # Release only if we still hold it; a lock that expired mid-run belongs
            # to whoever took it next.
            self.r.eval(_RELEASE_SCRIPT, 1, key, token)


def defer_delay(
    deferrals: int,
    base_s: float,
    cap_s: float,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Exponential, equal-jitter delay before deferral number ``deferrals`` (1-based)."""
    ceiling = min(cap_s, base_s * 2 ** (deferrals - 1))
    delay = ceiling / 2 + (rng or random).uniform(0, ceiling / 2)
    return float(min(cap_s, max(delay, retry_after or 0.0)))


class Worker:
    """Pulls workflow ids and runs them. Stateless: scale by running more of these."""

    def __init__(
        self,
        queue: WorkQueue,
        run_workflow: Callable[[str], object],
        *,
        lock_ttl_s: float = 60,
        lock_renew_s: float = 20,
        max_deferrals: int = 5,
        defer_base_s: float = 30,
        defer_cap_s: float = 900,
        on_give_up: Callable[[str, str], None] | None = None,
    ) -> None:
        self.queue = queue
        self.run_workflow = run_workflow
        self.lock_ttl_s = lock_ttl_s
        self.lock_renew_s = lock_renew_s
        self.max_deferrals = max_deferrals
        self.defer_base_s = defer_base_s
        self.defer_cap_s = defer_cap_s
        # Rule 7: a workflow the queue gives up on goes to a human. The service
        # passes a callback that marks it escalated in Postgres; without one it is
        # recorded in the dead-letter hash, never silently dropped.
        self.on_give_up = on_give_up or queue.dead_letter
        self.processed = 0
        self.deferred = 0
        self.gave_up = 0
        self.leases_lost = 0

    def run(
        self,
        stop: threading.Event | None = None,
        max_workflows: int | None = None,
        poll_timeout: int = 5,
    ) -> int:
        """Drain the queue until stopped or until ``max_workflows`` are done."""
        stop = stop or threading.Event()
        while not stop.is_set() and (max_workflows is None or self.processed < max_workflows):
            self.queue.promote_due()
            workflow_id = self.queue.dequeue(timeout=poll_timeout)
            if workflow_id is None:
                continue
            try:
                with self.queue.lock(
                    workflow_id, ttl_seconds=self.lock_ttl_s, renew_seconds=self.lock_renew_s
                ):
                    self.run_workflow(workflow_id)
                self.queue.clear_deferrals(workflow_id)
                self.processed += 1
            except LockNotAcquired:
                # Someone else owns it; drop it rather than contend.
                continue
            except LeaseLost:
                # Ownership moved on mid-run and nothing was written after it did;
                # the new owner resumes from the checkpoint.
                self.leases_lost += 1
                _log.warning("lease lost", extra={"workflow_id": workflow_id})
            except InfrastructureUnavailable as e:
                self._defer(workflow_id, e)
        return self.processed

    def _defer(self, workflow_id: str, error: InfrastructureUnavailable) -> None:
        n = self.queue.count_deferral(workflow_id)
        if n > self.max_deferrals:
            self.queue.clear_deferrals(workflow_id)
            self.gave_up += 1
            reason = f"infrastructure_unavailable: {error}"
            _log.error("giving up", extra={"workflow_id": workflow_id, "reason": reason})
            self.on_give_up(workflow_id, reason)
            return
        delay = defer_delay(n, self.defer_base_s, self.defer_cap_s, error.retry_after)
        self.queue.defer(workflow_id, delay)
        self.deferred += 1
        _log.warning(
            "deferred",
            extra={"workflow_id": workflow_id, "deferrals": n, "delay_s": round(delay, 1)},
        )
