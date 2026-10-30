"""Fault tolerance for the agent's dependencies (production hardening).

Two ideas, kept separate on purpose:

* **Retries are for reads.** ``retry_call`` retries an operation that is safe to
  repeat -- an ERPNext read, a document *mapping* call, a model completion --
  with capped exponential backoff and full jitter, honouring ``Retry-After``.
  ERPNext *writes* are never retried here: a write whose response was lost may
  already have happened, and the only safe way to find out is the idempotency
  key (``erp.idempotent.submit_once`` and the pipeline's orphan adoption).

* **An outage is not an agent decision.** ``InfrastructureUnavailable`` marks a
  failure of the world, not of the workflow. The pipeline lets it propagate
  instead of spending its bounded retries (Rule 7) on it, and the worker defers
  the workflow and tries again later rather than escalating a healthy purchase
  to a human because a server was restarting. Deferrals are themselves bounded.

``CircuitBreaker`` keeps its state in Redis so every worker shares it: once a
dependency has failed ``threshold`` times in a row, workers stop calling it for
``cooldown_s`` and defer immediately, instead of each discovering the outage on
its own with a 180-second timeout.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

#: HTTP statuses that mean "not now", not "no".
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class InfrastructureUnavailable(RuntimeError):
    """A dependency (ERPNext, a model server, the queue) is unavailable.

    Never a judgement about the workflow. Subclassed by ``ERPUnavailable`` and
    ``ModelUnavailable`` so existing ``except ERPError`` / ``except
    ExecutorError`` clauses still see them.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff: uniform in [0, min(cap, base * 2**attempt)]."""
    ceiling = min(cap, base * (2**attempt))
    return (rng or random).uniform(0, ceiling)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Run ``fn``, retrying only ``InfrastructureUnavailable``, at most ``attempts`` times."""
    for attempt in range(attempts):
        try:
            return fn()
        except InfrastructureUnavailable as e:
            if attempt == attempts - 1:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay, rng)
            if e.retry_after is not None:
                delay = max(delay, min(e.retry_after, max_delay))
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class CircuitBreaker:
    """Consecutive-failure breaker shared through Redis.

    Closed: calls go through; each failure increments a counter, each success
    clears it. At ``threshold`` consecutive failures the breaker opens for
    ``cooldown_s`` (a key with that TTL). When the key expires the next call is a
    trial: success closes it, failure counts again from zero toward the next
    opening. Every key carries a TTL, so nothing here grows without bound.
    """

    PREFIX = "veritas:breaker:"

    def __init__(self, redis: Any, name: str, threshold: int = 5, cooldown_s: int = 60) -> None:
        self.r = redis
        self.name = name
        self.threshold = threshold
        self.cooldown_s = cooldown_s

    @property
    def _open_key(self) -> str:
        return f"{self.PREFIX}{self.name}:open"

    @property
    def _fail_key(self) -> str:
        return f"{self.PREFIX}{self.name}:failures"

    def is_open(self) -> bool:
        return bool(self.r.exists(self._open_key))

    def record_success(self) -> None:
        self.r.delete(self._fail_key)

    def record_failure(self) -> bool:
        """Count a failure; return True if this one opened the breaker."""
        failures = int(self.r.incr(self._fail_key))
        self.r.expire(self._fail_key, max(self.cooldown_s * 10, 600))
        if failures >= self.threshold:
            self.r.set(self._open_key, "1", ex=self.cooldown_s)
            self.r.delete(self._fail_key)
            return True
        return False

    def guard(self, fn: Callable[[], T]) -> T:
        """Call ``fn`` through the breaker."""
        if self.is_open():
            raise InfrastructureUnavailable(
                f"{self.name} circuit open; deferring", retry_after=float(self.cooldown_s)
            )
        try:
            result = fn()
        except InfrastructureUnavailable:
            self.record_failure()
            raise
        self.record_success()
        return result
