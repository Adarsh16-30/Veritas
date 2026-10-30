"""Fault tolerance in the ERP client and the shared circuit breaker.

Synthetic: a scripted HTTP session stands in for ERPNext and a dict stands in
for Redis. What is under test is the policy -- reads retry, writes never do,
an outage is typed as one, and the breaker stops calls after a run of failures.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
import requests

from erp.client import ERPClient, ERPError, ERPUnavailable
from erp.resilience import CircuitBreaker, InfrastructureUnavailable, backoff_delay, retry_call


class _Resp:
    def __init__(self, status: int, data: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self.ok = status < 400
        self._data = data or {"name": "DOC-1"}
        self.headers = headers or {}
        self.text = "body"
        self.request = type("R", (), {"method": "X", "url": "u"})()

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


class ScriptedSession:
    """Answers each request with the next scripted response (or raises it)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def _next(self, verb: str) -> _Resp:
        self.calls.append(verb)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, **_: Any) -> _Resp:
        return self._next("get")

    def post(self, url: str, **_: Any) -> _Resp:
        return self._next("post")

    def put(self, url: str, **_: Any) -> _Resp:
        return self._next("put")


def _client(script: list[Any]) -> tuple[ERPClient, ScriptedSession, list[float]]:
    erp = ERPClient(url="http://erp", api_key="k", api_secret="s")
    session = ScriptedSession(script)
    erp.s = session  # type: ignore[assignment]
    slept: list[float] = []
    erp._sleep = slept.append  # type: ignore[method-assign]
    return erp, session, slept


def test_a_read_retries_a_transient_outage_and_succeeds() -> None:
    erp, s, slept = _client([_Resp(503), requests.ConnectionError("reset"), _Resp(200)])
    assert erp.get("Item", "X") == {"name": "DOC-1"}
    assert s.calls == ["get", "get", "get"] and len(slept) == 2


def test_a_read_gives_up_after_its_attempts_with_a_typed_outage() -> None:
    erp, s, _ = _client([_Resp(502), _Resp(502), _Resp(502)])
    with pytest.raises(ERPUnavailable) as e:
        erp.get_list("Item")
    assert isinstance(e.value, ERPError) and isinstance(e.value, InfrastructureUnavailable)
    assert len(s.calls) == 3


@pytest.mark.parametrize("call", ["insert", "submit", "update"])
def test_a_write_is_sent_exactly_once(call: str) -> None:
    """A write whose response is lost may already have happened; only the
    idempotency key may decide, never a blind resend."""
    erp, s, _ = _client([requests.ReadTimeout("lost"), _Resp(200)])
    with pytest.raises(ERPUnavailable):
        if call == "insert":
            erp.insert("Material Request", {})
        elif call == "submit":
            erp.submit({"doctype": "Material Request"})
        else:
            erp.update("Material Request", "MR-1", {})
    assert len(s.calls) == 1


def test_generic_calls_are_not_retried_but_mappers_are() -> None:
    erp, s, _ = _client([_Resp(503)])
    with pytest.raises(ERPUnavailable):
        erp.call("frappe.core.doctype.user.user.generate_keys", user="u")
    assert len(s.calls) == 1
    erp, s, _ = _client([_Resp(503), _Resp(200, {"items": []})])
    erp.call("erpnext.x.make_purchase_order", idempotent=True, source_name="MR-1")
    assert len(s.calls) == 2


def test_a_client_error_is_not_an_outage_and_is_not_retried() -> None:
    erp, s, _ = _client([_Resp(404)])
    with pytest.raises(ERPError) as e:
        erp.get("Item", "missing")
    assert not isinstance(e.value, ERPUnavailable) and e.value.status_code == 404
    assert len(s.calls) == 1


def test_retry_after_is_honoured_within_the_cap() -> None:
    erp, _, slept = _client([_Resp(429, headers={"Retry-After": "5"}), _Resp(200)])
    erp.get("Item", "X")
    assert slept and slept[0] >= 5


def test_backoff_is_capped_full_jitter() -> None:
    rng = random.Random(1)
    assert all(0 <= backoff_delay(a, 0.5, 8.0, rng) <= min(8.0, 0.5 * 2**a) for a in range(10))
    with pytest.raises(InfrastructureUnavailable):
        retry_call(
            lambda: (_ for _ in ()).throw(InfrastructureUnavailable("x")), sleep=lambda _: None
        )


class DictRedis:
    def __init__(self) -> None:
        self.d: dict[str, Any] = {}

    def exists(self, k: str) -> int:
        return int(k in self.d)

    def delete(self, k: str) -> None:
        self.d.pop(k, None)

    def incr(self, k: str) -> int:
        self.d[k] = int(self.d.get(k, 0)) + 1
        return int(self.d[k])

    def expire(self, k: str, ttl: int) -> None:
        pass

    def set(self, k: str, v: str, ex: int | None = None) -> None:
        self.d[k] = v


def test_breaker_opens_after_consecutive_failures_and_stops_calls() -> None:
    r = DictRedis()
    b = CircuitBreaker(r, "erp", threshold=3, cooldown_s=60)
    calls = 0

    def down() -> None:
        nonlocal calls
        calls += 1
        raise InfrastructureUnavailable("down")

    for _ in range(3):
        with pytest.raises(InfrastructureUnavailable):
            b.guard(down)
    assert b.is_open() and calls == 3
    with pytest.raises(InfrastructureUnavailable, match="circuit open"):
        b.guard(down)
    assert calls == 3  # open: the dependency is not called at all


def test_a_success_resets_the_failure_count() -> None:
    b = CircuitBreaker(DictRedis(), "model", threshold=2, cooldown_s=60)
    with pytest.raises(InfrastructureUnavailable):
        b.guard(lambda: (_ for _ in ()).throw(InfrastructureUnavailable("x")))
    assert b.guard(lambda: "ok") == "ok"
    with pytest.raises(InfrastructureUnavailable):
        b.guard(lambda: (_ for _ in ()).throw(InfrastructureUnavailable("x")))
    assert not b.is_open()  # never two *consecutive* failures
