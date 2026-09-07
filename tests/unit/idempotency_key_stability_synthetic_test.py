"""Rule 5 — an idempotency key must be a function of a step's *inputs* only.

Regression test for a double-post bug found in Phase 2: ``_attempt_input`` folded
the whole of ``ctx.docs`` into the key. After a crash-resume ``restore_docs``
returns the step's own committed output as well, so the key changed, the
committed-key lookup missed, and a second document would have been posted.

Synthetic doubles are permitted here: filename ends ``_synthetic_test.py``.
"""

from __future__ import annotations

from agent.context import UPSTREAM_SLOTS, ContextAssembler, WorkflowSpec
from agent.state import Step, StepContext

SPEC = WorkflowSpec(
    workflow_id="wf-key",
    item_code="WIDGET-A",
    qty=10,
    supplier="Acme Industrial Supply",
    rate=25.0,
    needed_by="2026-12-01",
    bill_no="ACME-wf-key",
)

#: What the chain looks like part-way through, and after every step committed.
MID_RUN = {"S1": "MR-1", "S3": "PO-1", "S4_receipt": "PR-1", "S4_invoice": "PI-1"}
AFTER_RESUME = {**MID_RUN, "S6": "PE-1"}


def _assembler() -> ContextAssembler:
    # No ERP call is made: _attempt_input is pure over (spec, ctx).
    return ContextAssembler(erp=None, spec=SPEC)  # type: ignore[arg-type]


def _key_input(step: Step, docs: dict[str, str]) -> dict:
    ctx = StepContext(workflow_id=SPEC.workflow_id, step=step, docs=dict(docs))
    return _assembler()._attempt_input(ctx)


def test_key_input_ignores_documents_the_step_produces() -> None:
    """S6 must derive the same key before and after its own Payment Entry exists."""
    before = _key_input(Step.S6, MID_RUN)
    after = _key_input(Step.S6, AFTER_RESUME)
    assert before == after, "S6 key changed once its own output was in ctx.docs"


def test_every_step_key_is_resume_stable() -> None:
    for step in Step:
        before = _key_input(step, MID_RUN)
        after = _key_input(step, AFTER_RESUME)
        assert before == after, f"{step.value} key is not stable across a resume"


def test_key_input_uses_only_declared_upstream_slots() -> None:
    for step in Step:
        upstream = _key_input(step, AFTER_RESUME)["upstream"]
        assert set(upstream) <= set(UPSTREAM_SLOTS[step]), (
            f"{step.value} folded undeclared documents into its key: {set(upstream)}"
        )
        assert step.value not in upstream, f"{step.value} depends on its own output"


def test_key_input_still_distinguishes_different_chains() -> None:
    """Resume-stability must not collapse two different chains onto one key."""
    a = _key_input(Step.S6, {"S4_invoice": "PI-1"})
    b = _key_input(Step.S6, {"S4_invoice": "PI-2"})
    assert a != b


def test_key_input_excludes_attempt_number() -> None:
    ctx1 = StepContext(workflow_id=SPEC.workflow_id, step=Step.S4, docs=dict(MID_RUN), attempt=1)
    ctx3 = StepContext(workflow_id=SPEC.workflow_id, step=Step.S4, docs=dict(MID_RUN), attempt=3)
    asm = _assembler()
    assert asm._attempt_input(ctx1) == asm._attempt_input(ctx3)
