"""A renewed, checkable hold on one workflow (production hardening, B4).

The per-workflow Redis lock is what makes "one owner per workflow" true. A lock
with a fixed TTL and no renewal is only true for as long as the TTL: a verified
workflow that outlives it (slow model, ERP outage) silently gets a second owner,
and only the ``commits`` primary key then stands between two workers and a
second posting.

``Lease`` keeps the lock alive with a heartbeat thread and, more importantly,
lets the code that is about to write *ask* whether it still owns the workflow.
``ensure_lease()`` is called immediately before every side effect; when the
lease is lost, or merely cannot be confirmed recently enough, it raises
``LeaseLost`` and nothing is written. The step's checkpoint is already durable
(Rule 6), so whoever owns the workflow next resumes it safely.

The current lease travels in a context variable, so the pipeline needs no
reference to the queue: outside a worker (the benchmark CLI, tests) there is no
lease and ``ensure_lease()`` does nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Compare-and-renew: extend the TTL only while the key still holds our token.
RENEW_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
)


class LeaseLost(RuntimeError):
    """This worker no longer owns the workflow. Nothing may be written."""


class Lease:
    """Ownership of one workflow's lock, renewed in the background.

    A renewal that finds another token marks the lease lost at once. A renewal
    that *fails* (Redis unreachable) proves nothing either way, so the lease
    stays usable only while the last confirmed renewal is recent: once
    ``ttl_s - renew_s`` has passed without one, the key may already have expired
    and been taken, and ``ensure`` refuses.
    """

    def __init__(
        self,
        redis: Any,
        key: str,
        token: str,
        ttl_s: float,
        renew_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if renew_s * 2 > ttl_s:
            raise ValueError("renew_s must be at most half of ttl_s")
        self.r = redis
        self.key = key
        self.token = token
        self.ttl_s = ttl_s
        self.renew_s = renew_s
        self._clock = clock
        self._confirmed_at = clock()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return (
            self._lost.is_set() or self._clock() - self._confirmed_at >= self.ttl_s - self.renew_s
        )

    def renew(self) -> bool:
        """One compare-and-renew. Returns whether ownership was confirmed."""
        try:
            held = self.r.eval(RENEW_SCRIPT, 1, self.key, self.token, int(self.ttl_s * 1000))
        except Exception:  # noqa: BLE001 - an unreachable Redis confirms nothing
            return False
        if int(held) == 1:
            self._confirmed_at = self._clock()
            return True
        self._lost.set()
        return False

    def ensure(self) -> None:
        if self.lost:
            raise LeaseLost(f"lost the lock on {self.key}; another worker may own it")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._beat, name=f"lease:{self.key}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.renew_s + 1)

    def _beat(self) -> None:
        while not self._stop.wait(self.renew_s):
            self.renew()
            if self._lost.is_set():
                return


_current: ContextVar[Lease | None] = ContextVar("veritas_lease", default=None)


@contextmanager
def holding(lease: Lease) -> Iterator[Lease]:
    """Make ``lease`` the one ``ensure_lease`` checks, for this block."""
    token = _current.set(lease)
    try:
        yield lease
    finally:
        _current.reset(token)


def ensure_lease() -> None:
    """Raise ``LeaseLost`` if this code runs under a lease it no longer holds."""
    lease = _current.get()
    if lease is not None:
        lease.ensure()
