"""What each state *does* — the state machine's own semantics.

This table is deliberately not in ``agent/``. The agent decides **whether** to
proceed (Rule 2: from a real model call); the state machine decides **what**
proceeding means at each state. Keeping the two apart is what makes "no hardcoded
action map" a real property rather than a naming convention.

Some states submit a document and move the GL; some are decision-only gates.

Writes are **planned, not built**. A plan carries the doctype, the slot its
result fills, and its idempotency key — all computable without touching ERPNext —
plus a ``build`` thunk that constructs the actual document only when the write is
really going to happen. That laziness is a Rule 6 requirement, not an
optimisation: see :class:`PlannedWrite`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.context import ContextAssembler, derived_key
from agent.state import Step, StepContext
from erp.client import ERPClient


@dataclass(frozen=True)
class PlannedWrite:
    """One ERPNext submission, described before it is constructed.

    ``build`` is deferred because constructing a document is itself an ERPNext
    call — ``make_purchase_invoice`` and friends map a source document into a
    target — and after a partial commit those mappers can **fail outright**.

    The case that proved it: a worker dies after the Purchase Invoice is
    submitted but before Postgres records the key. On resume the Purchase Order
    is already fully billed, so ``make_purchase_invoice`` has nothing left to
    map and raises ``TypeError: unsupported operand type(s) for -: 'NoneType'
    and 'float'``. Building eagerly meant that 500 happened before the resume
    could notice the document it was about to rebuild already existed — the
    retry cap then burned and a recoverable crash became a human escalation.

    Key and doctype are known without ERPNext, so the committer can check
    "already done?" first and only build what it actually needs to submit.
    """

    doctype: str
    doc_slot: str
    key: str
    build: Callable[[], dict[str, Any]]


def _s1(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> PlannedWrite:
    s = asm.spec
    return PlannedWrite(
        "Material Request",
        "S1",
        ctx.idempotency_key,
        lambda: erp.material_request_doc(s.item_code, s.qty, s.needed_by, ctx.idempotency_key),
    )


def _s3(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> PlannedWrite:
    s = asm.spec
    return PlannedWrite(
        "Purchase Order",
        "S3",
        ctx.idempotency_key,
        lambda: erp.purchase_order_doc(
            ctx.docs["S1"], s.supplier, s.rate, asm.today, ctx.idempotency_key
        ),
    )


def _s6(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> PlannedWrite:
    return PlannedWrite(
        "Payment Entry",
        "S6",
        ctx.idempotency_key,
        lambda: erp.payment_entry_doc(ctx.docs["S4_invoice"], asm.today, ctx.idempotency_key),
    )


def _gate(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> None:
    """S2 and S5 are judgement gates: no document, no ledger movement."""
    return None


#: A builder returns the planned write for this step, or None for a gate.
Builder = Callable[[ERPClient, ContextAssembler, StepContext], "PlannedWrite | None"]


#: S4 is the only state that submits two documents (receipt, then invoice) — the
#: three-way match needs both to exist before it means anything.
def s4_writes(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> list[PlannedWrite]:
    s = asm.spec
    receipt_key = derived_key(ctx, "receipt")
    invoice_key = derived_key(ctx, "invoice")
    return [
        PlannedWrite(
            "Purchase Receipt",
            "S4_receipt",
            receipt_key,
            lambda: erp.purchase_receipt_doc(ctx.docs["S3"], asm.today, receipt_key),
        ),
        PlannedWrite(
            "Purchase Invoice",
            "S4_invoice",
            invoice_key,
            lambda: erp.purchase_invoice_doc(ctx.docs["S3"], s.bill_no, asm.today, invoice_key),
        ),
    ]


def writes_for(erp: ERPClient, asm: ContextAssembler, ctx: StepContext) -> list[PlannedWrite]:
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
