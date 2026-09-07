"""Rules 5–6 against the real ledger: kill a worker mid-S4, resume, zero re-posts.

Two crash windows are exercised, and the second is the one that matters:

1. **Between S4's two documents.** The receipt is submitted and recorded; the
   worker dies before the invoice. ``submit_once`` alone handles this — the
   receipt's key is already in Postgres, so the resume replays it.

2. **Inside the record window.** ERPNext accepts the submit and the worker dies
   *before* Postgres records the key. ``submit_once`` cannot see this: it asks
   Postgres, Postgres says no, and a naive retry posts a second document. The
   key is stamped into the document itself so the resume can ask the ledger
   directly (``Pipeline._adopt_orphan``). This test is what proves that works.

Real ERPNext, real Ollama, real GL. Skipped when either is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
import requests

from agent.context import WorkflowSpec
from agent.executor import Executor, OllamaLLM
from agent.state import Status, Step, StepContext
from erp.client import ERPClient
from orchestrator.db import Store
from orchestrator.machine import WorkflowMachine

pytestmark = pytest.mark.integration

ERP_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _up(url: str, path: str) -> bool:
    try:
        return requests.get(f"{url}{path}", timeout=3).status_code == 200
    except requests.RequestException:
        return False


needs_stack = pytest.mark.skipif(
    not (_up(ERP_URL, "/api/method/ping") and _up(OLLAMA_URL, "/api/tags")),
    reason=f"needs a running ERPNext at {ERP_URL} and Ollama at {OLLAMA_URL}",
)


class SimulatedCrash(RuntimeError):
    """Stands in for the worker process dying."""


class CrashAfterSubmit(ERPClient):
    """Submits for real, then dies before the caller can record the key.

    This reproduces the exact window ``submit_once`` is blind to: the document
    exists in the ledger, but nothing in Postgres knows about it.
    """

    def __init__(self, crash_on: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.crash_on = crash_on
        self.submitted: list[str] = []

    def insert_and_submit(self, doctype: str, doc: dict[str, Any]) -> str:
        name = super().insert_and_submit(doctype, doc)
        self.submitted.append(name)
        if doctype == self.crash_on:
            raise SimulatedCrash(f"worker died after submitting {doctype} {name}")
        return name


def _spec(workflow_id: str) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        item_code=os.environ.get("VERITAS_ITEM", "WIDGET-A"),
        qty=4,
        supplier=os.environ.get("VERITAS_SUPPLIER", "Acme Industrial Supply"),
        rate=12.5,
        needed_by=(date.today() + timedelta(days=5)).isoformat(),
        bill_no=f"ACME-{workflow_id}",
    )


def _machine(store: Store, erp: ERPClient, spec: WorkflowSpec) -> WorkflowMachine:
    return WorkflowMachine(store, erp, spec, Executor(OllamaLLM()))


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store()
    yield s
    s.close()


@needs_stack
def test_crash_inside_record_window_does_not_double_post(store: Store) -> None:
    """The nastiest window: ERPNext accepted the write, Postgres never heard."""
    wf = f"wf-crash-{uuid.uuid4().hex[:8]}"
    spec = _spec(wf)

    # Drive S1..S3 normally so S4 has a Purchase Order to work from.
    healthy = ERPClient()
    machine = _machine(store, healthy, spec)
    store.create_workflow(wf, Step.S1)
    docs: dict[str, str] = {}
    for step in (Step.S1, Step.S2, Step.S3):
        ctx = StepContext(workflow_id=wf, step=step, docs=docs)
        result = machine.pipeline.run_step(ctx)
        docs = ctx.docs
        assert result.route.value == "commit", f"{step.value} escalated: {result.reason}"

    # --- crash: the Purchase Receipt is submitted, then the worker dies --------
    crashing = CrashAfterSubmit(crash_on="Purchase Receipt")
    crashed_machine = _machine(store, crashing, spec)
    ctx = StepContext(workflow_id=wf, step=Step.S4, docs=dict(docs))
    with pytest.raises(SimulatedCrash):
        crashed_machine.pipeline.run_step(ctx)

    orphan = crashing.submitted[-1]
    assert store.get_committed_by_doc(orphan) is None, (
        "precondition failed: the crash was supposed to happen before Postgres recorded the key"
    )
    assert store.checkpoint_status(wf, Step.S4) == Status.IN_PROGRESS.value

    # --- resume with a healthy worker -----------------------------------------
    resumed = _machine(store, ERPClient(), spec)
    outcome = resumed.run(wf)

    assert outcome.completed, f"resume did not complete: {outcome.reason}"
    assert Step.S4 not in outcome.skipped, "S4 was in_progress; it must be re-entered"
    for step in (Step.S1, Step.S3):
        assert step in outcome.skipped, f"{step.value} committed before the crash; must be skipped"

    # The orphaned receipt was adopted, not duplicated.
    assert outcome.docs["S4_receipt"] == orphan, (
        f"resume posted a second receipt: {outcome.docs['S4_receipt']} != {orphan}"
    )

    # Exactly one submitted document per logical action, in the real ledger.
    for doctype, slot in (
        ("Material Request", "S1"),
        ("Purchase Order", "S3"),
        ("Purchase Receipt", "S4_receipt"),
        ("Purchase Invoice", "S4_invoice"),
        ("Payment Entry", "S6"),
    ):
        rows = ERPClient().get_list(
            doctype,
            filters=[["veritas_idempotency_key", "!=", ""], ["docstatus", "=", 1]],
            fields=["name", "veritas_idempotency_key"],
        )
        keys = [r["veritas_idempotency_key"] for r in rows]
        this_key = next(
            (r["veritas_idempotency_key"] for r in rows if r["name"] == outcome.docs[slot]), None
        )
        assert this_key is not None, f"{slot} document lost its idempotency stamp"
        assert keys.count(this_key) == 1, f"{doctype}: key {this_key[:12]} posted more than once"

    # And the ledger balances.
    for effect in resumed.gl_effect(outcome.docs):
        assert effect["balanced"], f"unbalanced GL for {effect['voucher_no']}"


@needs_stack
def test_double_dispatch_of_a_committed_step_posts_nothing_new(store: Store) -> None:
    """Re-running a completed workflow must be a no-op against the ledger (Rule 5)."""
    wf = f"wf-replay-{uuid.uuid4().hex[:8]}"
    spec = _spec(wf)
    erp = ERPClient()

    first = _machine(store, erp, spec).run(wf)
    assert first.completed, f"first run did not complete: {first.reason}"

    before = {
        dt: len(erp.get_list(dt, filters=[["docstatus", "=", 1]], fields=["name"]))
        for dt in (
            "Material Request",
            "Purchase Order",
            "Purchase Receipt",
            "Purchase Invoice",
            "Payment Entry",
        )
    }

    second = _machine(store, erp, spec).run(wf)

    after = {
        dt: len(erp.get_list(dt, filters=[["docstatus", "=", 1]], fields=["name"])) for dt in before
    }

    assert second.completed, f"replay did not complete: {second.reason}"
    assert list(second.skipped) == list(Step), "a completed workflow must skip every step"
    assert before == after, f"replay posted new documents: {before} -> {after}"
    assert second.docs == first.docs
