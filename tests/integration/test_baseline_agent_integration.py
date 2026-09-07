"""Phase 2 acceptance: the baseline agent drives S1..S6 and moves the real GL.

This is the Phase 2 success criterion from PRD §7 — "happy-path workflows produce
real correct GL postings" — asserted end to end against the running stack. No
verification gate is involved; that is the point. Skipped when the stack is down.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import requests

from agent.context import WorkflowSpec
from agent.executor import Executor, OllamaLLM
from agent.state import Status, Step
from erp.client import IDEMPOTENCY_FIELD, ERPClient
from orchestrator.db import Store
from orchestrator.machine import WorkflowMachine
from trace.store import TraceLogger

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

QTY = 6
RATE = 15.0


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store()
    yield s
    s.close()


@needs_stack
def test_baseline_agent_completes_s1_to_s6_and_moves_the_ledger(store: Store) -> None:
    wf = f"wf-happy-{uuid.uuid4().hex[:8]}"
    spec = WorkflowSpec(
        workflow_id=wf,
        item_code=os.environ.get("VERITAS_ITEM", "WIDGET-A"),
        qty=QTY,
        supplier=os.environ.get("VERITAS_SUPPLIER", "Acme Industrial Supply"),
        rate=RATE,
        needed_by=(date.today() + timedelta(days=10)).isoformat(),
        bill_no=f"ACME-{wf}",
    )
    erp = ERPClient()
    machine = WorkflowMachine(store, erp, spec, Executor(OllamaLLM()))

    outcome = machine.run(wf)

    # --- the workflow ran all six states -------------------------------------
    assert outcome.completed, f"workflow escalated: {outcome.reason}"
    assert [s.step for s in outcome.steps] == list(Step)
    assert all(s.action is not None for s in outcome.steps), "a step decided nothing"

    # --- every state is durably marked committed (Rule 6) --------------------
    for step in Step:
        assert store.checkpoint_status(wf, step) == Status.COMMITTED.value, (
            f"{step.value} is not checkpointed committed"
        )

    # --- exactly one document per logical action (Rule 5) --------------------
    commits = store.commits_for(wf)
    assert len(commits) == 5, f"expected 5 committed documents, got {len(commits)}"
    assert len({c["idempotency_key"] for c in commits}) == 5, "keys collided"
    assert {c["doctype"] for c in commits} == {
        "Material Request",
        "Purchase Order",
        "Purchase Receipt",
        "Purchase Invoice",
        "Payment Entry",
    }

    # --- each document carries its key, and carries it uniquely --------------
    for c in commits:
        doc = erp.get(c["doctype"], c["doc_name"])
        assert doc[IDEMPOTENCY_FIELD] == c["idempotency_key"], (
            f"{c['doc_name']} is not stamped with the key that committed it"
        )
        assert int(doc["docstatus"]) == 1, f"{c['doc_name']} was never submitted"

    # --- the real ledger moved, and it balances ------------------------------
    effects = machine.gl_effect(outcome.docs)
    assert len(effects) == 2, "expected GL movement from the invoice and the payment"
    expected = (Decimal(str(QTY)) * Decimal(str(RATE))).quantize(Decimal("0.01"))
    for effect in effects:
        assert effect["lines"] > 0, f"{effect['voucher_no']} moved nothing"
        assert effect["balanced"], f"{effect['voucher_no']} does not balance"
        assert Decimal(effect["debit"]) == expected, (
            f"{effect['voucher_no']} posted {effect['debit']}, expected {expected}"
        )

    # --- every decision is auditable (Rule 2) --------------------------------
    traces = TraceLogger(store).reconstruct(wf)
    assert len(traces) == 6, f"expected one trace per state, got {len(traces)}"
    assert len({t["prompt_hash"] for t in traces}) == 6, "states shared a prompt"
    assert all(t["provenance"]["rationale"] for t in traces), "a decision has no rationale"
    assert all(t["model"] for t in traces), "a decision does not name its model"

    # --- Rule 7 budget was respected -----------------------------------------
    assert store.llm_calls(wf) == 6, f"expected 6 model calls, got {store.llm_calls(wf)}"


@needs_stack
def test_the_trace_store_refuses_to_be_rewritten(store: Store) -> None:
    """An audit trail that can be edited is not an audit trail (PRD §3.3)."""
    import psycopg

    row = store.conn.execute("SELECT id FROM traces ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no traces recorded yet")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        store.conn.execute("UPDATE traces SET response = 'tampered' WHERE id = %s", (row["id"],))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        store.conn.execute("DELETE FROM traces WHERE id = %s", (row["id"],))
