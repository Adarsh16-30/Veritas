"""Least privilege per step (PRD Phase 6): "S1–S3 hold no payment scope".

Synthetic: the client's HTTP session is replaced by a recorder, so the tests
see exactly which identity each request carried and that a refused write never
leaves the process. Whether ERPNext *itself* refuses the buyer identity is a
property of the live role setup, checked by
``tests/integration/test_erp_scope_integration.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from erp.client import ERPClient
from erp.scoped import (
    BUYER_STEPS,
    CALL_SCOPE,
    WRITE_SCOPE,
    ScopeError,
    StepScopedERP,
    agent_client,
    describe,
)

BUYER = "token buyer-key:buyer-secret"
FINANCE = "token fin-key:fin-secret"


class _Response:
    ok = True
    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return {"data": self._data, "message": self._data}


class RecordingSession:
    """Stands in for requests.Session; records (method, url, Authorization)."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.sent: list[tuple[str, str, str | None]] = []

    def _req(self, method: str, url: str) -> _Response:
        self.sent.append((method, url, self.headers.get("Authorization")))
        return _Response({"name": "DOC-0001", "doctype": "Material Request"})

    def get(self, url: str, **_: Any) -> _Response:
        return self._req("GET", url)

    def post(self, url: str, **_: Any) -> _Response:
        return self._req("POST", url)

    def put(self, url: str, **_: Any) -> _Response:
        return self._req("PUT", url)


def _client() -> tuple[StepScopedERP, RecordingSession]:
    erp = StepScopedERP("buyer-key", "buyer-secret", "fin-key", "fin-secret", url="http://erp")
    rec = RecordingSession()
    rec.headers.update(erp.s.headers)
    erp.s = rec  # type: ignore[assignment]
    return erp, rec


@pytest.mark.parametrize("step", ["S1", "S2", "S3", "S4", "S5", "S6"])
def test_each_step_reads_under_its_own_identity(step: str) -> None:
    erp, rec = _client()
    with erp.acting_for(step):
        erp.get("Item", "X")
    want = BUYER if step in BUYER_STEPS else FINANCE
    assert rec.sent[-1][2] == want


def test_outside_any_step_is_the_finance_identity_and_restores_after() -> None:
    erp, rec = _client()
    erp.get("Item", "X")
    with erp.acting_for("S2"):
        erp.get("Item", "X")
    erp.get("Item", "X")
    assert [a for _, _, a in rec.sent] == [FINANCE, BUYER, FINANCE]
    assert erp.current_step is None


def test_no_buyer_step_may_touch_payment_entry() -> None:
    """The PRD criterion, by the local guard alone."""
    for step in sorted(BUYER_STEPS):
        assert "Payment Entry" not in WRITE_SCOPE[step]
        assert not any("payment" in m for m in CALL_SCOPE[step])
        erp, rec = _client()
        with erp.acting_for(step), pytest.raises(ScopeError):
            erp.insert_and_submit("Payment Entry", {"paid_amount": 1})
        with erp.acting_for(step), pytest.raises(ScopeError):
            erp.call("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry")
        assert rec.sent == []  # refused before any request left the process


def test_judgement_gates_write_nothing() -> None:
    for step in ("S2", "S5"):
        erp, rec = _client()
        with erp.acting_for(step):
            for doctype in ("Material Request", "Purchase Order", "Purchase Invoice"):
                with pytest.raises(ScopeError):
                    erp.insert(doctype, {})
        assert rec.sent == []


def test_a_step_may_write_its_own_documents() -> None:
    erp, rec = _client()
    with erp.acting_for("S1"):
        assert erp.insert_and_submit("Material Request", {}) == "DOC-0001"
    assert [a for _, _, a in rec.sent] == [BUYER, BUYER]  # insert + submit
    with erp.acting_for("S3"):
        erp.call("erpnext.stock.doctype.material_request.material_request.make_purchase_order")
    with erp.acting_for("S1"), pytest.raises(ScopeError):
        erp.update("Purchase Order", "PO-1", {})


def test_scope_errors_escalate_through_the_pipeline_path() -> None:
    """ScopeError is an ERPError, which the pipeline turns into a bounded retry
    and then an escalation (Rule 7) rather than a crashed worker."""
    from erp.client import ERPError

    assert issubclass(ScopeError, ERPError)


def test_write_scope_covers_every_planned_write() -> None:
    """orchestrator/steps.py plans exactly the doctypes WRITE_SCOPE allows."""
    from types import SimpleNamespace

    from agent.state import Step, StepContext
    from orchestrator.steps import writes_for

    asm = SimpleNamespace(spec=SimpleNamespace(received_qty=None, invoice_rate=None), today="")
    for step in Step:
        ctx = StepContext(workflow_id="wf", step=step, idempotency_key="k")
        planned = {p.doctype for p in writes_for(None, asm, ctx)}  # type: ignore[arg-type]
        assert planned == WRITE_SCOPE[step.value], step


def test_buyer_and_finance_must_differ() -> None:
    from erp.client import ERPError

    with pytest.raises(ERPError):
        StepScopedERP("k", "s", "k", "s", url="http://erp")


def test_agent_client_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_API_KEY", "fin-key")
    monkeypatch.setenv("ERPNEXT_API_SECRET", "fin-secret")
    monkeypatch.delenv("ERPNEXT_BUYER_API_KEY", raising=False)
    monkeypatch.delenv("ERPNEXT_BUYER_API_SECRET", raising=False)
    plain = agent_client("http://erp")
    assert type(plain) is ERPClient and describe(plain) == "single identity"

    monkeypatch.setenv("ERPNEXT_BUYER_API_KEY", "buyer-key")
    monkeypatch.setenv("ERPNEXT_BUYER_API_SECRET", "buyer-secret")
    scoped = agent_client("http://erp")
    assert isinstance(scoped, StepScopedERP) and describe(scoped).startswith("step-scoped")


def test_single_identity_client_scope_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_API_KEY", "fin-key")
    monkeypatch.setenv("ERPNEXT_API_SECRET", "fin-secret")
    erp = ERPClient(url="http://erp")
    before = dict(erp.s.headers)
    with erp.acting_for("S1"):
        assert dict(erp.s.headers) == before


def test_pipeline_runs_each_step_inside_its_scope() -> None:
    from agent.pipeline import Pipeline
    from agent.state import Step, StepContext

    erp, _ = _client()
    seen: list[str | None] = []
    pipe = Pipeline.__new__(Pipeline)
    pipe.erp = erp
    pipe._run_step = lambda ctx: seen.append(erp.current_step)  # type: ignore[method-assign,assignment,return-value]
    for step in Step:
        pipe.run_step(StepContext(workflow_id="wf", step=step))
    assert seen == [s.value for s in Step]
    assert erp.current_step is None
