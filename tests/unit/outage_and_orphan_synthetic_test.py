"""Outages are not decisions; a lost lease writes nothing; orphans are adopted correctly.

Synthetic: the store, assembler, executor, tracer and ERP client are in-memory
doubles (Rule 1: stubs only in ``tests/``, only in ``*_synthetic_test.py``).
What is under test is the pipeline's policy around them.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

import pytest
import requests

from agent.executor import Decision, ExecutorError, ModelUnavailable, OllamaLLM
from agent.pipeline import Pipeline
from agent.state import Action, Status, Step, StepContext
from bench.run import is_infrastructure_failure
from erp.client import ERPClient, ERPError, ERPUnavailable
from orchestrator.lease import Lease, LeaseLost, ensure_lease, holding
from orchestrator.queue import defer_delay


class StubDB:
    def __init__(self) -> None:
        self.checkpoints: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.calls = 0
        self.committed: dict[str, str] = {}

    def checkpoint(self, workflow_id: str, step: Any, status: str) -> None:
        self.checkpoints.append((str(getattr(step, "value", step)), status))

    def set_status(self, workflow_id: str, status: str, reason: str | None = None) -> None:
        self.statuses.append(status)

    def llm_calls(self, workflow_id: str) -> int:
        return self.calls

    def bump_llm_calls(self, workflow_id: str) -> None:
        self.calls += 1

    def record_attempt(self, *_: Any, **__: Any) -> None:
        pass

    def get_committed(self, key: str) -> str | None:
        return self.committed.get(key)

    def record_committed(self, key: str, name: str) -> None:
        self.committed[key] = name

    @contextmanager
    def commit_binding(self, *_: Any) -> Iterator[None]:
        yield


class StubAssembler:
    def assemble(self, ctx: StepContext) -> None:
        ctx.idempotency_key = "k" * 64


class ScriptedExecutor:
    def __init__(self, *outcomes: Exception | Action) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def propose(self, ctx: StepContext) -> Decision:
        self.calls += 1
        out = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return Decision(out, {}, "because", "p", "r", "ph", "rh", "m", 1)


class StubTracer:
    def record(self, *_: Any, **__: Any) -> str:
        return "trace-1"


class StubERP:
    def __init__(self, found: tuple[str, int] | None = None) -> None:
        self.found = found
        self.submitted: list[str] = []
        self.writes = 0

    def acting_for(self, step: str) -> Any:
        return nullcontext()

    def find_by_idempotency_key_with_status(self, doctype: str, key: str) -> tuple[str, int] | None:
        return self.found

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        return {"doctype": doctype, "name": name}

    def submit(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(str(doc["name"]))
        return doc


def _pipeline(executor: ScriptedExecutor, erp: StubERP | None = None) -> tuple[Pipeline, StubDB]:
    db = StubDB()
    pipe = Pipeline(db, erp or StubERP(), StubAssembler(), executor, StubTracer())  # type: ignore[arg-type]
    return pipe, db


def _ctx(step: Step = Step.S2) -> StepContext:
    return StepContext(workflow_id="wf-1", step=step)


# --- an outage propagates instead of being spent as a retry ---------------------
def test_a_model_outage_propagates_without_retrying_or_escalating() -> None:
    executor = ScriptedExecutor(ModelUnavailable("ollama unreachable at http://x: refused"))
    pipe, db = _pipeline(executor)
    with pytest.raises(ModelUnavailable):
        pipe.run_step(_ctx())
    assert executor.calls == 1
    assert Status.ESCALATED.value not in db.statuses
    assert db.checkpoints[-1] == ("S2", Status.IN_PROGRESS.value)  # resumable


def test_a_bad_answer_is_still_retried_under_the_cap_then_escalated() -> None:
    """Rule 7 is unchanged for what *is* a model answer."""
    executor = ScriptedExecutor(ExecutorError("model did not return JSON"))
    pipe, db = _pipeline(executor)
    result = pipe.run_step(_ctx())
    assert executor.calls == pipe.policy.max_attempts_per_step
    assert result.reason and result.reason.startswith("retry_cap_exhausted")
    assert Status.ESCALATED.value in db.statuses


def test_an_erp_outage_during_the_write_propagates() -> None:
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED))

    def down(ctx: StepContext) -> list[str]:
        raise ERPUnavailable("ERPNext unreachable", status_code=503)

    pipe._commit = down  # type: ignore[method-assign]
    with pytest.raises(ERPUnavailable):
        pipe.run_step(_ctx(Step.S1))
    assert Status.ESCALATED.value not in db.statuses


def test_an_erp_refusal_is_still_a_retryable_step_fault() -> None:
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED))

    def refused(ctx: StepContext) -> list[str]:
        raise ERPError("ValidationError: qty must be positive", status_code=417)

    pipe._commit = refused  # type: ignore[method-assign]
    result = pipe.run_step(_ctx(Step.S1))
    assert result.reason and "erp_write_failed" in result.reason


# --- B4: a worker that lost its lease writes nothing ----------------------------
class LostRedis:
    """Every renewal finds another worker's token."""

    def eval(self, *_: Any) -> int:
        return 0


def test_a_lost_lease_blocks_the_side_effect() -> None:
    erp = StubERP()
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED), erp)
    lease = Lease(LostRedis(), "veritas:lock:wf-1", "mine", ttl_s=60, renew_s=20)
    assert lease.renew() is False and lease.lost
    with holding(lease), pytest.raises(LeaseLost):
        pipe._commit(_ctx(Step.S1))
    assert db.checkpoints == [] and erp.submitted == []  # not even the §9.2 checkpoint


def test_an_unconfirmed_lease_goes_stale_before_its_ttl() -> None:
    now = [0.0]
    lease = Lease(LostRedis(), "k", "t", ttl_s=60, renew_s=20, clock=lambda: now[0])
    now[0] = 39.0
    lease.ensure()  # confirmed 39 s ago: the key cannot have expired yet
    now[0] = 40.0
    with pytest.raises(LeaseLost):
        lease.ensure()


def test_no_lease_outside_a_worker_is_a_no_op() -> None:
    ensure_lease()  # the benchmark CLI and tests run without a worker


# --- orphan adoption: drafts are finished, cancellations are refused -------------
def test_a_draft_orphan_is_submitted_before_it_is_recorded() -> None:
    erp = StubERP(found=("MAT-MR-0001", 0))
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED), erp)
    pipe._adopt_orphan(_ctx(Step.S1), "Material Request", "k")
    assert erp.submitted == ["MAT-MR-0001"]
    assert db.committed == {"k": "MAT-MR-0001"}


def test_a_submitted_orphan_is_adopted_as_is() -> None:
    erp = StubERP(found=("MAT-MR-0001", 1))
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED), erp)
    pipe._adopt_orphan(_ctx(Step.S1), "Material Request", "k")
    assert erp.submitted == [] and db.committed == {"k": "MAT-MR-0001"}


def test_a_cancelled_orphan_is_neither_adopted_nor_reposted() -> None:
    erp = StubERP(found=("MAT-MR-0001", 2))
    pipe, db = _pipeline(ScriptedExecutor(Action.PROCEED), erp)
    with pytest.raises(ERPError, match="cancelled"):
        pipe._adopt_orphan(_ctx(Step.S1), "Material Request", "k")
    assert erp.submitted == [] and db.committed == {}


class _Listing(ERPClient):
    def __init__(self, rows: list[dict[str, object]]) -> None:  # noqa: D107
        self.rows = rows

    def get_list(self, *a: object, **kw: object) -> list[dict[str, object]]:
        return list(self.rows)


@pytest.mark.parametrize(
    ("rows", "want"),
    [
        ([{"name": "A", "docstatus": 2}, {"name": "B", "docstatus": 0}], ("B", 0)),
        ([{"name": "A", "docstatus": 0}, {"name": "B", "docstatus": 1}], ("B", 1)),
        ([{"name": "A", "docstatus": 2}], ("A", 2)),
        ([], None),
    ],
)
def test_lookup_prefers_submitted_then_draft(
    rows: list[dict[str, object]], want: tuple[str, int] | None
) -> None:
    assert _Listing(rows).find_by_idempotency_key_with_status("Material Request", "k") == want


# --- the model client types its outages ---------------------------------------------
class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.text = "busy"

    def json(self) -> dict[str, Any]:
        return {"response": '{"action": "hold"}'}


def _llm(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> tuple[OllamaLLM, list[int]]:
    calls: list[int] = []

    def post(*_: Any, **__: Any) -> _Resp:
        calls.append(1)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    monkeypatch.setattr("agent.executor.requests.post", post)
    llm = OllamaLLM(model="m", url="http://ollama")
    llm._sleep = lambda _: None  # type: ignore[method-assign]
    return llm, calls


def test_a_refused_connection_is_retried_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, calls = _llm(monkeypatch, [requests.ConnectionError("refused"), _Resp(200)])
    assert llm.complete("s", "u") == '{"action": "hold"}' and len(calls) == 2


def test_a_read_timeout_is_typed_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, calls = _llm(monkeypatch, [requests.ReadTimeout("Read timed out"), _Resp(200)])
    with pytest.raises(ModelUnavailable) as e:
        llm.complete("s", "u")
    assert len(calls) == 1 and isinstance(e.value, ExecutorError)
    assert is_infrastructure_failure(f"{type(e.value).__name__}: {e.value}")


def test_a_busy_server_is_an_outage_with_its_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    script: list[Any] = [_Resp(503, {"Retry-After": "7"})] * 3
    llm, calls = _llm(monkeypatch, script)
    with pytest.raises(ModelUnavailable) as e:
        llm.complete("s", "u")
    assert len(calls) == 3 and e.value.retry_after == 7.0


def test_a_rejected_request_is_not_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, calls = _llm(monkeypatch, [_Resp(400)])
    with pytest.raises(ExecutorError) as e:
        llm.complete("s", "u")
    assert not isinstance(e.value, ModelUnavailable) and len(calls) == 1


# --- deferral delay -----------------------------------------------------------------
def test_defer_delay_grows_is_capped_and_honours_retry_after() -> None:
    rng = random.Random(5)
    first = [defer_delay(1, 30, 900, rng=rng) for _ in range(50)]
    assert all(15 <= d <= 30 for d in first)
    assert all(d <= 900 for d in (defer_delay(n, 30, 900, rng=rng) for n in range(1, 20)))
    assert defer_delay(1, 30, 900, retry_after=120, rng=rng) == 120
    assert defer_delay(1, 30, 900, retry_after=5000, rng=rng) == 900
