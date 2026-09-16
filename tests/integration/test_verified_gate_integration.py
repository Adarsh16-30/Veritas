"""The three-gate pipeline against the real ERPNext ledger and two real models.

Rule 1 all the way down: a real ERPNext instance, real submitted documents, real
GL postings, a real executor call and a real *independent* verifier call from a
different model family.

The test that matters most here is the duplicate-payment one. It pays an invoice
through a complete verified workflow, then points a **second, different**
workflow at that same invoice. Idempotency cannot help: a different workflow
derives a different key, so `submit_once` sees nothing and would happily post a
second Payment Entry. The only thing standing between that invoice and being
paid twice is the rule engine's fresh read of the ledger — which is exactly the
scenario Phase 3 exists to cover.

Skipped when ERPNext or Ollama is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
import requests

from agent.context import WorkflowSpec
from agent.executor import Executor, OllamaLLM
from agent.state import Route, Step, StepContext
from erp.client import ERPClient
from orchestrator.db import Store
from orchestrator.machine import WorkflowMachine
from verify.gate import build_gate, select_verifier_model
from verify.verifier import model_family

pytestmark = pytest.mark.integration

ERP_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EXECUTOR_MODEL = os.environ.get("OLLAMA_MODEL", "llama3:8b-instruct-q4_K_M")


def _up(url: str, path: str) -> bool:
    try:
        return requests.get(f"{url}{path}", timeout=3).status_code == 200
    except requests.RequestException:
        return False


needs_stack = pytest.mark.skipif(
    not (_up(ERP_URL, "/api/method/ping") and _up(OLLAMA_URL, "/api/tags")),
    reason=f"needs a running ERPNext at {ERP_URL} and Ollama at {OLLAMA_URL}",
)


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store()
    yield s
    s.close()


def _spec(workflow_id: str) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        item_code=os.environ.get("VERITAS_ITEM", "WIDGET-A"),
        qty=3,
        supplier=os.environ.get("VERITAS_SUPPLIER", "Acme Industrial Supply"),
        rate=12.5,
        needed_by=(date.today() + timedelta(days=5)).isoformat(),
        bill_no=f"ACME-{workflow_id}",
    )


def _verified_machine(store: Store, erp: ERPClient, spec: WorkflowSpec) -> WorkflowMachine:
    return WorkflowMachine(
        store,
        erp,
        spec,
        Executor(OllamaLLM()),
        gate=build_gate(erp, spec, executor_model=EXECUTOR_MODEL),
    )


def _count(erp: ERPClient, doctype: str) -> int:
    return len(erp.get_list(doctype, filters=[["docstatus", "=", 1]], fields=["name"]))


# --- Rule 3 against the real deployment ----------------------------------------
@needs_stack
def test_the_configured_verifier_is_a_different_model_family() -> None:
    chosen = select_verifier_model(EXECUTOR_MODEL)
    assert model_family(chosen) != model_family(EXECUTOR_MODEL), (
        f"verifier {chosen!r} shares a family with executor {EXECUTOR_MODEL!r}; "
        "that is not independent verification"
    )


# --- the happy path, verified ---------------------------------------------------
@needs_stack
def test_verified_workflow_completes_and_moves_the_real_ledger(store: Store) -> None:
    wf = f"wf-ver-{uuid.uuid4().hex[:8]}"
    erp = ERPClient()
    machine = _verified_machine(store, erp, _spec(wf))
    assert machine.verified is True

    outcome = machine.run(wf)
    assert outcome.completed, f"verified run did not complete: {outcome.reason}"

    # Every step passed all three gates, and the ledger balances.
    for effect in machine.gl_effect(outcome.docs):
        assert effect["balanced"], f"unbalanced GL for {effect['voucher_no']}"
    assert outcome.docs["S6"], "no payment entry was produced"

    # Rule 7: both model calls per attempt came out of one capped budget.
    spent = store.llm_calls(wf)
    assert spent <= machine.pipeline.policy.max_llm_calls_per_workflow
    assert spent >= 12, f"expected an executor and a verifier call per step, got {spent}"


@needs_stack
def test_every_committed_step_recorded_an_independent_verifier_verdict(store: Store) -> None:
    wf = f"wf-ver-{uuid.uuid4().hex[:8]}"
    erp = ERPClient()
    outcome = _verified_machine(store, erp, _spec(wf)).run(wf)
    assert outcome.completed, f"run did not complete: {outcome.reason}"

    traces = store.traces_for(wf)
    executor_rows = [t for t in traces if t["provenance"].get("stage") == "executor"]
    verifier_rows = [t for t in traces if t["provenance"].get("stage") == "verifier"]
    # One verifier call per executor call — including retries, which are a
    # legitimate outcome of a verifier objection, so this is not pinned to six.
    assert len(verifier_rows) == len(executor_rows) >= 6
    assert {t["step"] for t in verifier_rows} == {s.value for s in Step}, (
        "every step must have been independently verified"
    )

    for row in verifier_rows:
        assert model_family(row["model"]) != model_family(EXECUTOR_MODEL)
        assert row["provenance"]["independence"]["distinct_model"] is True
        assert row["provenance"]["verdict"] in {"pass", "fail"}

    # Rule 3, demonstrated on real prompts. A substring search for the rationale
    # is the wrong test: the executor is told to cite the DELTA fact that decided
    # it, so its rationale is often a verbatim quote of the evidence, and the
    # evidence legitimately contains that string. What must hold is structural —
    # the verifier's prompt is built only from the assembled evidence, the
    # proposed action and the step's checklist, and carries nothing the executor
    # added.
    for row in verifier_rows:
        prompt = str(row["prompt"])
        assert "PREVIOUS ATTEMPT REJECTED" not in prompt
        assert "rationale" not in prompt.lower()
        for expectation in row["provenance"]["checklist"]:
            assert expectation in prompt
        # Every non-boilerplate line came from the evidence the assembler built.
        evidence = str(row["step_context"]).splitlines()
        boilerplate = ("EVIDENCE", "CHECKLIST", "PROPOSED ACTION", "")
        for line in prompt.splitlines():
            stripped = line.strip()
            if not stripped or line.startswith(boilerplate) or stripped[0].isdigit():
                continue
            assert line in evidence, f"verifier prompt line not from the evidence: {line!r}"

    # And the gate's verdict is on the attempt row, not only in the trace.
    for attempt in store.attempts_for(wf):
        if attempt["committed"]:
            assert attempt["verdict"] is not None
            assert attempt["region"] is not None
            assert attempt["signals"], "no signal vector recorded for calibration"


# --- the hard invariant: a duplicate payment the idempotency key cannot catch ---
@needs_stack
def test_a_second_workflow_cannot_pay_an_invoice_that_is_already_paid(store: Store) -> None:
    """Phase 3's headline guarantee, against the real ledger.

    `submit_once` keys on (workflow, step, inputs). A *different* workflow paying
    the same invoice derives a different key, so the idempotency layer sees a
    first-time write and lets it through. The deterministic rule gate, reading
    the ledger fresh at commit time, is what stops it.
    """
    erp = ERPClient()

    # 1. Pay an invoice for real, through the full verified path.
    first = f"wf-paid-{uuid.uuid4().hex[:8]}"
    paid = _verified_machine(store, erp, _spec(first)).run(first)
    assert paid.completed, f"setup run did not complete: {paid.reason}"
    invoice = paid.docs["S4_invoice"]

    # 2. A second workflow, pointed at that same already-paid invoice.
    second = f"wf-dup-{uuid.uuid4().hex[:8]}"
    spec = _spec(second)
    machine = _verified_machine(store, erp, spec)
    store.create_workflow(second, Step.S6)

    before = _count(erp, "Payment Entry")
    ctx = StepContext(workflow_id=second, step=Step.S6, docs={"S4_invoice": invoice})
    result = machine.pipeline.run_step(ctx)
    after = _count(erp, "Payment Entry")

    assert result.route is Route.ESCALATE, "the gate let a duplicate payment through"
    assert result.reason is not None and "hard_rule_violation" in result.reason, (
        f"expected the rule engine to stop this, got: {result.reason}"
    )
    assert after == before, f"a second payment was posted to the real ledger: {before} -> {after}"

    # The idempotency layer genuinely could not have caught this.
    assert store.get_committed(ctx.idempotency_key) is None, (
        "precondition failed: this key had already committed, so the rule gate was "
        "not what prevented the double payment"
    )


@needs_stack
def test_the_rule_gate_names_the_invariant_it_caught(store: Store) -> None:
    """An escalation a human cannot act on is only half a guard."""
    erp = ERPClient()
    wf = f"wf-paid-{uuid.uuid4().hex[:8]}"
    paid = _verified_machine(store, erp, _spec(wf)).run(wf)
    assert paid.completed, f"setup run did not complete: {paid.reason}"

    second = f"wf-dup-{uuid.uuid4().hex[:8]}"
    machine = _verified_machine(store, erp, _spec(second))
    store.create_workflow(second, Step.S6)
    ctx = StepContext(
        workflow_id=second, step=Step.S6, docs={"S4_invoice": paid.docs["S4_invoice"]}
    )
    result = machine.pipeline.run_step(ctx)

    assert result.reason is not None
    # Both the declarative invariant and the fresh ledger read should fire.
    assert "S6_NOT_ALREADY_PAID" in result.reason or "S6_ALREADY_SETTLED_NOW" in result.reason, (
        f"escalation reason does not name a rule: {result.reason}"
    )

    workflow = store.get_workflow(second)
    assert workflow is not None
    assert workflow["status"] == "escalated"
    assert workflow["escalation_reason"]
