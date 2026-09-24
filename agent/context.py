"""ContextAssembler — Layer 3 input side (PRD §4.1).

Three jobs, in order of importance:

1. **Scope.** Pull only the ERPNext fields this step needs. A step never sees
   fields it does not need, which is also the prompt-injection boundary: the
   less untrusted text reaches the model, the less there is to inject through.
2. **Arithmetic.** Every comparison the decision depends on is computed here, in
   Python, and handed to the model as a ``DELTA:`` line. The model is never asked
   to do arithmetic — it is asked to judge.

   Every boolean DELTA fact is phrased as a **check that passed**: ``True`` means
   the condition is satisfied and safe, ``False`` means it is not, uniformly.
   Four facts used to be phrased the other way round (``already_paid``,
   ``duplicate_bill_no``, ``discrepancy_open``, ``over_approval_threshold``), and
   the first real benchmark run showed the executor inverting one of them — it
   read ``already_paid=False`` on a clean invoice and concluded payment was
   therefore *not* possible. A mixed convention asks the model to solve a
   polarity puzzle before it can even start reasoning about the evidence, and it
   silently corrupted the conformal ``facts_clean`` signal, which counts true
   booleans and so scored a duplicated bill as *cleaner*. The convention is not
   a hint about what to do: the model still has to decide what a failed check
   means for this step.
3. **Determinism.** The semantic ``attempt_input`` for the step is derived here,
   which is what the idempotency key is built from (Rule 5). It must not vary
   between retries of the same logical action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from agent.state import Step, StepContext
from erp.client import ERPClient
from erp.idempotent import idempotency_key

#: Untrusted free text from the ERP is capped and stripped before it can reach a
#: prompt. Phase 6 hardens and tests this against the embedded-instruction fault
#: class; this is the boundary it will be tested at.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_FREE_TEXT = 200


def sanitize(text: object, limit: int = _MAX_FREE_TEXT) -> str:
    s = _CONTROL.sub(" ", str(text or ""))
    s = " ".join(s.split())
    return s[:limit]


#: An internal routing token that carries no information about what is being
#: bought. USAspending publishes award descriptions that are nothing else
#: (``IGF::OT::IGF``), and the seeded ERP item inherits the name verbatim.
_IGF_TOKEN = re.compile(r"IGF *::? *[A-Z]{2,3} *::? *IGF", re.I)
_CODE_LIKE = re.compile(r"^[A-Z0-9][A-Z0-9 _:./-]*$")


def describes_goods(name: str) -> bool:
    """True when an item name says something a human buyer could act on.

    False for an empty name and for one that is nothing but a short routing
    code. This deliberately mirrors ``data.corpus.AwardRecord.is_code_only`` —
    the same rule the fault harness uses to *select* ambiguous awards — but is
    reimplemented here because ``agent/`` must not import from ``data/``, which
    feeds evaluation only. ``tests/unit/context_facts_synthetic_test.py``
    pins the two to the same answer so they cannot drift apart.
    """
    text = (name or "").strip()
    if not text:
        return False
    stripped = _IGF_TOKEN.sub("", text).strip(" :;,./-")
    if not stripped:
        return False
    return not (len(stripped) < 12 and bool(_CODE_LIKE.match(stripped)))


#: Phrases that address the *reader* of a data field instead of describing the
#: goods. An item name is a noun phrase; a second-person imperative or meta-text
#: about the approval process has no legitimate reason to appear in one.
#:
#: This is a heuristic over a general shape, not a signature for any particular
#: payload, and a determined attacker who phrases the instruction differently
#: will pass it. It is defence in depth behind the standing instruction in both
#: system prompts that ERP text is data and never a command — not a substitute
#: for it. See docs/limitations.md for what the benchmark number does and does
#: not establish.
_INSTRUCTION_LIKE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bignore\s+(all\s+|any\s+)?(previous|prior|earlier|above)",
        r"\bdisregard\b",
        r"\bsystem\s*(note|message|prompt|instruction)",
        r"\boverride\b",
        r"\bapprove\s+(and|this|it|immediately|without)",
        r"\brelease\s+(the\s+)?(payment|funds)\b",
        r"\bwithout\s+(any\s+|further\s+|additional\s+)?(review|verification|approval|check)",
        r"\b(no|skip)\s+(further\s+|additional\s+)?(review|verification|approval|check)",
        r"\bdo\s+not\s+(verify|check|review|escalate|flag|question)",
        r"\b(prior|previous)\s+checks?\s+(are\s+|were\s+|have\s+been\s+)?(complete|completed|passed|done)",
        r"\byou\s+(are|must|should|will|may)\b",
    )
)


def reads_as_instructions(name: str) -> bool:
    """True when a supposedly descriptive field is addressing the model."""
    return any(p.search(name or "") for p in _INSTRUCTION_LIKE)


@dataclass(frozen=True)
class WorkflowSpec:
    """The procurement request a workflow exists to fulfil."""

    workflow_id: str
    item_code: str
    qty: float
    supplier: str
    rate: float
    needed_by: str
    bill_no: str
    tolerance_pct: float = 2.0
    approval_threshold: float = 10_000.0

    #: What the supplier actually delivered and billed, when that differs from
    #: what was ordered. ``None`` means "exactly as ordered" — the clean case.
    #:
    #: These exist because a three-way match needs three independent legs. Until
    #: Phase 4 the assembler derived the received quantity and the invoiced
    #: amount from `qty` and `rate` — the same fields the purchase order was
    #: built from — so `qty_match` was always true and `amount_variance` was
    #: always exactly zero, for every workflow, by construction. A match whose
    #: two compared legs are definitionally equal is not a match; it cannot
    #: detect a short delivery or an overbill because it cannot represent one.
    received_qty: float | None = None
    invoice_rate: float | None = None

    @property
    def expected_total(self) -> Decimal:
        return (Decimal(str(self.qty)) * Decimal(str(self.rate))).quantize(Decimal("0.01"))

    @property
    def delivered_qty(self) -> float:
        """What arrived on the loading dock, which need not be what was ordered."""
        return self.qty if self.received_qty is None else self.received_qty

    @property
    def billed_rate(self) -> float:
        """The rate on the supplier's invoice, which need not be the agreed rate."""
        return self.rate if self.invoice_rate is None else self.invoice_rate


#: Which already-committed documents each state *consumes*. The idempotency key
#: is built from these and only these (see ``_attempt_input``), so a step's key is
#: a function of its inputs and never of its own output.
UPSTREAM_SLOTS: dict[Step, tuple[str, ...]] = {
    Step.S1: (),
    Step.S2: ("S1",),
    Step.S3: ("S1",),
    Step.S4: ("S3",),
    Step.S5: ("S3", "S4_invoice"),
    Step.S6: ("S4_invoice",),
}


def _d(value: object) -> Decimal:
    return Decimal(str(value or 0))


def derived_key(ctx: StepContext, part: str) -> str:
    """A stable sub-key for a step that submits more than one document.

    Derived from the step's own key through the provided primitive, so every key
    in the system is a sha256 of a sorted payload and two sub-keys can never
    collide with each other or with a plain step key (Rule 5).

    It lives here rather than in ``orchestrator/steps.py`` because the
    ContextAssembler needs it too: S4 must be able to tell its own orphaned
    invoice apart from a genuine duplicate bill.
    """
    return idempotency_key(
        ctx.workflow_id, f"{ctx.step.value}:{part}", {"base": ctx.idempotency_key}
    )


class ContextAssembler:
    def __init__(self, erp: ERPClient, spec: WorkflowSpec) -> None:
        self.erp = erp
        self.spec = spec
        self.today = date.today().isoformat()

    # --- public ---------------------------------------------------------------
    def assemble(self, ctx: StepContext) -> None:
        # The key is derived first: it is a function of this step's *inputs*, and
        # S4 needs it during assembly to recognise its own in-flight write.
        ctx.idempotency_key = idempotency_key(
            ctx.workflow_id, ctx.step.value, self._attempt_input(ctx)
        )
        builder = self._BUILDERS[ctx.step]
        facts, summary, amount = builder(self, ctx)
        ctx.facts = facts
        ctx.amount_at_stake = amount
        ctx.step_context = self._render(ctx, summary, facts)

    # --- the semantic identity of this step's write (Rule 5) -------------------
    def _attempt_input(self, ctx: StepContext) -> dict[str, Any]:
        """Stable across retries *and across a crash-resume* of the same logical action.

        Deliberately excludes ``attempt`` and any timestamp, and includes only the
        documents this step **consumes** — never one it produces. Folding the whole
        of ``ctx.docs`` in here would be a double-post bug: after a resume,
        ``restore_docs`` returns the step's own committed output too, the key would
        differ from the one already recorded, and the guard would let a second
        document through.
        """
        s = self.spec
        base: dict[str, Any] = {"item": s.item_code, "qty": s.qty, "supplier": s.supplier}
        if ctx.step in (Step.S3, Step.S4, Step.S6):
            base["rate"] = s.rate
        if ctx.step is Step.S4:
            base["bill_no"] = s.bill_no
        base["upstream"] = {
            slot: ctx.docs[slot] for slot in UPSTREAM_SLOTS[ctx.step] if slot in ctx.docs
        }
        return base

    def _render(self, ctx: StepContext, summary: dict[str, Any], facts: dict[str, Any]) -> str:
        lines = [f"STEP: {ctx.step.value} — {_STEP_PURPOSE[ctx.step]}"]
        lines += [f"{k}: {v}" for k, v in summary.items()]
        delta = ", ".join(f"{k}={v}" for k, v in facts.items())
        lines.append(f"DELTA: {delta}")
        if ctx.rejection_reason:
            lines.append(f"PREVIOUS ATTEMPT REJECTED: {sanitize(ctx.rejection_reason)}")
        return "\n".join(lines)

    # --- per-step assembly ------------------------------------------------------
    def _s1(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        s = self.spec
        item = self.erp.get("Item", s.item_code)
        summary = {
            "item": sanitize(item.get("item_code")),
            "item_name": sanitize(item.get("item_name")),
            "qty_requested": s.qty,
            "needed_by": s.needed_by,
            "is_purchase_item": item.get("is_purchase_item"),
        }
        est = s.expected_total
        name = summary["item_name"]
        # ERPNext falls back to the item code when no name was supplied, so an
        # item with no description at all arrives here looking like
        # "USA-272060915" rather than "". A name that is merely the item's own
        # code describes nothing, and this is the only signal that separates the
        # `missing` class from an ordinary item.
        named_after_its_own_code = name.strip() == summary["item"].strip()
        facts = {
            "estimated_value": str(est),
            "item_is_purchasable": bool(item.get("is_purchase_item")),
            "qty_positive": s.qty > 0,
            "needed_by_not_past": s.needed_by >= self.today,
            # The item name is the one field that says *what is being bought*,
            # and until Phase 4 it reached the model only as free text in the
            # summary. Three fault classes corrupt exactly that field -- an
            # empty description, a pure routing code, and instruction text aimed
            # at the model -- and all three were missed at 0/4 in both
            # configurations, because every DELTA fact still read True: the
            # faults touch neither purchasability, nor quantity, nor the date.
            # The verifier is required to ground each answer in a fact, so with
            # no fact covering the description it had nothing to object with.
            # That is an evidence-coverage gap, not a reasoning failure.
            "item_description_informative": (
                not named_after_its_own_code and describes_goods(name)
            ),
            "item_description_free_of_instructions": not reads_as_instructions(name),
        }
        return facts, summary, est

    def _s2(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        s = self.spec
        mr = self.erp.get("Material Request", ctx.docs["S1"])
        est = s.expected_total
        summary = {
            "material_request": mr["name"],
            "mr_status": sanitize(mr.get("status")),
            "qty": s.qty,
            "estimated_total": str(est),
            "approval_threshold": s.approval_threshold,
        }
        facts = {
            "estimated_total": str(est),
            "within_approval_threshold": est <= Decimal(str(s.approval_threshold)),
            "headroom": str(Decimal(str(s.approval_threshold)) - est),
            "mr_submitted": int(mr.get("docstatus", 0)) == 1,
        }
        return facts, summary, est

    def _s3(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        s = self.spec
        supplier = self.erp.get("Supplier", s.supplier)
        mr = self.erp.get("Material Request", ctx.docs["S1"])
        mr_qty = sum(_d(i.get("qty")) for i in mr.get("items", []))
        total = s.expected_total
        summary = {
            "material_request": mr["name"],
            "supplier": sanitize(supplier.get("supplier_name")),
            "supplier_disabled": supplier.get("disabled"),
            "qty": s.qty,
            "unit_rate": s.rate,
            "po_total": str(total),
        }
        facts = {
            "po_total": str(total),
            "qty_matches_requisition": _d(s.qty) == mr_qty,
            "qty_delta_vs_mr": str(_d(s.qty) - mr_qty),
            "supplier_active": not supplier.get("disabled"),
            "rate_positive": s.rate > 0,
        }
        return facts, summary, total

    def _s4(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        """Three-way match. Every comparison is computed here, not by the model."""
        s = self.spec
        po = self.erp.get("Purchase Order", ctx.docs["S3"])
        po_total = _d(po.get("grand_total"))
        po_qty = sum(_d(i.get("qty")) for i in po.get("items", []))
        # The delivery note and the supplier invoice are independent evidence
        # from the ordered quantity and the agreed rate — that independence is
        # the whole point of a three-way match.
        received_qty = _d(s.delivered_qty)
        invoiced_total = (received_qty * _d(s.billed_rate)).quantize(Decimal("0.01"))

        variance = invoiced_total - po_total
        pct = (abs(variance) / po_total * 100) if po_total else Decimal("0")
        tolerance = Decimal(str(s.tolerance_pct))
        # Our own in-flight invoice is not a duplicate of itself (Rule 6).
        duplicate = self.erp.duplicate_bill_exists(
            s.supplier, s.bill_no, exclude_key=derived_key(ctx, "invoice")
        )

        summary = {
            "purchase_order": po["name"],
            "po_total": str(po_total),
            "po_qty": str(po_qty),
            "receipt_qty": str(received_qty),
            "supplier_bill_no": sanitize(s.bill_no),
            "ordered_rate": str(_d(s.rate)),
            "invoiced_rate": str(_d(s.billed_rate)),
            "invoice_total": str(invoiced_total),
            "tolerance_pct": str(tolerance),
        }
        facts = {
            "qty_match": po_qty == received_qty,
            "qty_variance": str(received_qty - po_qty),
            "amount_variance": str(variance),
            "amount_variance_pct": str(pct.quantize(Decimal("0.001"))),
            "within_tolerance": pct <= tolerance,
            "bill_no_not_previously_invoiced": not duplicate,
            "three_way_match_clean": (
                po_qty == received_qty and pct <= tolerance and not duplicate
            ),
        }
        return facts, summary, invoiced_total

    def _s5(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        s = self.spec
        pi = self.erp.get("Purchase Invoice", ctx.docs["S4_invoice"])
        po = self.erp.get("Purchase Order", ctx.docs["S3"])
        pi_total = _d(pi.get("grand_total"))
        po_total = _d(po.get("grand_total"))
        variance = pi_total - po_total
        pct = (abs(variance) / po_total * 100) if po_total else Decimal("0")
        summary = {
            "purchase_invoice": pi["name"],
            "invoice_total": str(pi_total),
            "po_total": str(po_total),
            "invoice_status": sanitize(pi.get("status")),
            "outstanding": str(_d(pi.get("outstanding_amount"))),
            "approved_authority": str(_d(s.approval_threshold)),
        }
        # What S2 actually approved. The compounding fault class raises the
        # invoiced rate by just under the match tolerance, so S4 and the
        # variance check below both pass honestly while the amount finally paid
        # climbs above the authority S2 checked against. Nothing downstream
        # re-read that figure, so the class scored 0/4 in both configurations --
        # the gap `inject_compounding` predicted in its own docstring. The
        # threshold was on the spec the whole time; only the exposure was
        # missing. Once carried forward the check is pure arithmetic, which is
        # why it is also a hard invariant (S5_WITHIN_APPROVED_AUTHORITY).
        authority = Decimal(str(s.approval_threshold))
        facts = {
            "variance": str(variance),
            "variance_pct": str(pct.quantize(Decimal("0.001"))),
            "within_tolerance": pct <= Decimal(str(s.tolerance_pct)),
            "no_open_discrepancy": pct <= Decimal(str(s.tolerance_pct)),
            "invoice_submitted": int(pi.get("docstatus", 0)) == 1,
            "approved_authority": str(authority),
            "within_approved_authority": pi_total <= authority,
        }
        return facts, summary, pi_total

    def _s6(self, ctx: StepContext) -> tuple[dict[str, Any], dict[str, Any], Decimal]:
        s = self.spec
        pi = self.erp.get("Purchase Invoice", ctx.docs["S4_invoice"])
        outstanding = _d(pi.get("outstanding_amount"))
        grand = _d(pi.get("grand_total"))
        already_paid = self.erp.get_list(
            "Payment Entry",
            filters=[
                ["reference_no", "=", f"PAY-{pi['name']}"],
                ["docstatus", "=", 1],
            ],
            fields=["name"],
            limit=1,
        )
        summary = {
            "purchase_invoice": pi["name"],
            "supplier": sanitize(pi.get("supplier")),
            "grand_total": str(grand),
            "outstanding_amount": str(outstanding),
            "approved_authority": str(_d(s.approval_threshold)),
            "payment_account": self.erp.cash_account,
        }
        authority = Decimal(str(s.approval_threshold))
        facts = {
            "outstanding": str(outstanding),
            "fully_invoiced": grand > 0,
            "outstanding_equals_total": outstanding == grand,
            "not_previously_paid": not already_paid,
            "invoice_submitted": int(pi.get("docstatus", 0)) == 1,
            # S6 is the last point at which the approved figure still means
            # anything: after this the money has moved.
            "approved_authority": str(authority),
            "within_approved_authority": grand <= authority,
        }
        return facts, summary, outstanding

    _BUILDERS = {
        Step.S1: _s1,
        Step.S2: _s2,
        Step.S3: _s3,
        Step.S4: _s4,
        Step.S5: _s5,
        Step.S6: _s6,
    }


_STEP_PURPOSE: dict[Step, str] = {
    Step.S1: "raise a purchase requisition for the requested item",
    Step.S2: "policy check the requisition before it becomes a commitment",
    Step.S3: "place the purchase order with the supplier",
    Step.S4: "three-way match the order, the receipt and the supplier invoice",
    Step.S5: "resolve any discrepancy the match surfaced",
    Step.S6: "release payment against the matched invoice",
}
