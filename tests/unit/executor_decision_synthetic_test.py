"""Rule 2 — the action comes from a real model call over the real step context.

The static half of this rule is enforced by ``check_rules.sh`` (no action map in
``agent/executor.py``). The dynamic half is here: feed the *same* executor
materially different step contexts and show the decision moves with them. A
handler that returned a constant would fail this.

The contexts are synthetic (filename ends ``_synthetic_test.py``) but the model
call is real — that is the point. Skipped when Ollama is unreachable.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import requests

from agent.executor import Executor, ExecutorError, OllamaLLM
from agent.state import Action, Step, StepContext

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _ollama_up() -> bool:
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).status_code == 200
    except requests.RequestException:
        return False


needs_ollama = pytest.mark.skipif(not _ollama_up(), reason=f"needs Ollama at {OLLAMA_URL}")

CLEAN_MATCH = """STEP: S4 — three-way match the order, the receipt and the supplier invoice
purchase_order: PUR-ORD-2026-00001
po_total: 250.00
receipt_qty: 10
invoice_total: 250.00
tolerance_pct: 2.0
DELTA: qty_match=True, qty_variance=0, amount_variance=0.00, amount_variance_pct=0.000, \
within_tolerance=True, duplicate_bill_no=False, three_way_match_clean=True"""

OVER_TOLERANCE = """STEP: S4 — three-way match the order, the receipt and the supplier invoice
purchase_order: PUR-ORD-2026-00002
po_total: 250.00
receipt_qty: 10
invoice_total: 402.50
tolerance_pct: 2.0
DELTA: qty_match=True, qty_variance=0, amount_variance=152.50, amount_variance_pct=61.000, \
within_tolerance=False, duplicate_bill_no=False, three_way_match_clean=False"""

DUPLICATE_INVOICE = """STEP: S4 — three-way match the order, the receipt and the supplier invoice
purchase_order: PUR-ORD-2026-00003
po_total: 250.00
receipt_qty: 10
invoice_total: 250.00
tolerance_pct: 2.0
DELTA: qty_match=True, qty_variance=0, amount_variance=0.00, amount_variance_pct=0.000, \
within_tolerance=True, duplicate_bill_no=True, three_way_match_clean=False"""


def _ctx(text: str, workflow_id: str) -> StepContext:
    return StepContext(
        workflow_id=workflow_id,
        step=Step.S4,
        step_context=text,
        amount_at_stake=Decimal("250.00"),
    )


@pytest.fixture(scope="module")
def executor() -> Executor:
    return Executor(OllamaLLM())


@needs_ollama
def test_distinct_contexts_produce_distinct_prompts_and_rationales(executor: Executor) -> None:
    """Every decision is a fresh call over this step's own context."""
    decisions = [
        executor.propose(_ctx(text, f"wf-r2-{i}"))
        for i, text in enumerate((CLEAN_MATCH, OVER_TOLERANCE, DUPLICATE_INVOICE))
    ]
    assert len({d.prompt_hash for d in decisions}) == 3, "contexts collapsed to one prompt"
    assert len({d.response_hash for d in decisions}) == 3, "model returned an identical answer"
    assert all(d.rationale for d in decisions), "a decision with no rationale is not auditable"
    assert all(d.model for d in decisions)


@needs_ollama
def test_a_clean_match_is_allowed_to_proceed(executor: Executor) -> None:
    assert executor.propose(_ctx(CLEAN_MATCH, "wf-r2-clean")).action is Action.PROCEED


@needs_ollama
@pytest.mark.parametrize(
    ("name", "context"),
    [("over_tolerance", OVER_TOLERANCE), ("duplicate_bill", DUPLICATE_INVOICE)],
)
def test_a_failing_check_is_not_allowed_to_proceed(
    executor: Executor, name: str, context: str
) -> None:
    """The decision has to move with the facts, or the gate is theatre."""
    decision = executor.propose(_ctx(context, f"wf-r2-{name}"))
    assert decision.action is not Action.PROCEED, (
        f"{name}: model proposed {decision.action.value} on a failing check "
        f"— rationale: {decision.rationale}"
    )


@needs_ollama
def test_an_unusable_answer_is_rejected_not_coerced() -> None:
    """A model that answers off-vocabulary must fail loudly, never default."""

    class OffVocabulary:
        name = "stub-off-vocabulary"

        def complete(self, system: str, user: str) -> str:
            return '{"action": "definitely_pay_it", "args": {}, "rationale": "why not"}'

    with pytest.raises(ExecutorError, match="not one of"):
        Executor(OffVocabulary()).propose(_ctx(CLEAN_MATCH, "wf-r2-bad"))


def test_a_missing_rationale_is_rejected() -> None:
    class NoRationale:
        name = "stub-no-rationale"

        def complete(self, system: str, user: str) -> str:
            return '{"action": "proceed", "args": {}}'

    with pytest.raises(ExecutorError, match="no rationale"):
        Executor(NoRationale()).propose(_ctx(CLEAN_MATCH, "wf-r2-norat"))
