"""What each state *does* — the state machine's own semantics.

This table is deliberately not in ``agent/``. The agent decides **whether** to
proceed (Rule 2: from a real model call); the state machine decides **what**
proceeding means at each state. Keeping the two apart is what makes "no hardcoded
action map" a real property rather than a naming convention.

Some states submit a document and move the GL; some are decision-only gates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.context import ContextAssembler
from agent.state import Step, StepContext
from erp.client import ERPClient
from erp.idempotent import idempotency_key


@dataclass(frozen=True)
class Write:
    """One ERPNext submission: the doctype, the document, and where to file the
    resulting document name in ``ctx.docs``."""

    doctype: str
    doc: dict[str, Any]
    doc_slot: str


#: A builder returns the write for this step, or None for a decision-only gate.
Builder = Callable[[ERPClient, ContextAssembler, StepContext], "Write | None"]


def _s1(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> Write:
    s = asm.spec
    return Write(
        "Material Request",
        erp.material_request_doc(s.item_code, s.qty, s.needed_by, ctx.idempotency_key),
        "S1",
    )


def _s3(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> Write:
    s = asm.spec
    return Write(
        "Purchase Order",
        erp.purchase_order_doc(ctx.docs["S1"], s.supplier, s.rate, asm.today, ctx.idempotency_key),
        "S3",
    )


def _s6(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> Write:
    return Write(
        "Payment Entry",
        erp.payment_entry_doc(ctx.docs["S4_invoice"], asm.today, ctx.idempotency_key),
        "S6",
    )


def _gate(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> None:
    """S2 and S5 are judgement gates: no document, no ledger movement."""
    return None


def derived_key(ctx: StepContext, part: str) -> str:
    """A stable sub-key for a state that submits more than one document.

    Derived from the step's own key with the provided primitive, so every key in
    the system is a sha256 of a sorted payload and two sub-keys can never collide
    with each other or with a plain step key (Rule 5).
    """
    return idempotency_key(
        ctx.workflow_id, f"{ctx.step.value}:{part}", {"base": ctx.idempotency_key}
    )


#: S4 is the only state that submits two documents (receipt, then invoice) — the
#: three-way match needs both to exist before it means anything.
def s4_writes(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> list[Write]:
    s = asm.spec
    return [
        Write(
            "Purchase Receipt",
            erp.purchase_receipt_doc(ctx.docs["S3"], asm.today, derived_key(ctx, "receipt")),
            "S4_receipt",
        ),
        Write(
            "Purchase Invoice",
            erp.purchase_invoice_doc(
                ctx.docs["S3"], s.bill_no, asm.today, derived_key(ctx, "invoice")
            ),
            "S4_invoice",
        ),
    ]


def writes_for(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> list[Write]:
    """Every ERPNext submission this state performs when the agent proceeds."""
    if ctx.step is Step.S4:
        return s4_writes(erp, asm, ctx)
    build = _BUILDERS[ctx.step]
    write = build(erp, asm, ctx)
    return [write] if write is not None else []


_BUILDERS: dict[Step, Builder] = {
    Step.S1: _s1,
    Step.S2: _gate,
    Step.S3: _s3,
    Step.S4: _gate,  # handled by s4_writes
    Step.S5: _gate,
    Step.S6: _s6,
}
