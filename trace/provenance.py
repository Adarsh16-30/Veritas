"""Where every DELTA fact comes from (PRD §7 Phase 5: "click a fact -> the real
ERPNext source document").

The trace store records each fact's *value* and the documents committed so far
(``provenance.erp_documents``), but not which of those documents a fact was
computed from. That mapping lives in the ``ContextAssembler`` builders as code.
It is declared here, once, so the trace explorer can resolve a fact to its
source for **every** recorded run, including runs made before this module
existed, without re-reading the ERP or re-running the assembler.

A declaration can drift from the code it describes. ``FACT_SOURCES`` is
therefore pinned to the builders by ``tests/unit/trace_provenance_synthetic_test.py``,
which runs each builder and fails if a step emits a fact this table does not
cover, or reads a doctype no fact here points at.

Source references
-----------------
``doc:<slot>``    an upstream document this step *read* (``S1``, ``S3``, ``S4_invoice``)
``out:<slot>``    the document this step *committed* from the same values; exists
                  only if the step committed
``item``          the Item master the requisition names (S1)
``supplier``      the Supplier master the order is placed with (S3)
``query:<type>``  a ledger-wide lookup over one doctype (duplicate bill, prior payment)
``request``       the procurement request the workflow was started with. This is
                  **not an ERPNext document**, and the explorer says so rather
                  than pointing it at the nearest record. The ``out:`` document a
                  step commits carries the same values into the ledger.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from agent.context import QUOTE_CLOSE, QUOTE_OPEN
from agent.state import Step

#: ERPNext doctype behind each document slot (``orchestrator/steps.py`` plans).
SLOT_DOCTYPE: dict[str, str] = {
    "S1": "Material Request",
    "S3": "Purchase Order",
    "S4_receipt": "Purchase Receipt",
    "S4_invoice": "Purchase Invoice",
    "S6": "Payment Entry",
}

#: Which documents each step's facts are computed from. See the module docstring.
FACT_SOURCES: dict[Step, dict[str, tuple[str, ...]]] = {
    Step.S1: {
        "estimated_value": ("request", "out:S1"),
        "item_is_purchasable": ("item",),
        "qty_positive": ("request", "out:S1"),
        "needed_by_not_past": ("request", "out:S1"),
        "item_description_informative": ("item",),
        "item_description_free_of_instructions": ("item",),
    },
    Step.S2: {
        "estimated_total": ("request", "doc:S1"),
        "within_approval_threshold": ("request", "doc:S1"),
        "headroom": ("request",),
        "mr_submitted": ("doc:S1",),
    },
    Step.S3: {
        "po_total": ("request", "out:S3"),
        "qty_matches_requisition": ("doc:S1", "request"),
        "qty_delta_vs_mr": ("doc:S1", "request"),
        "supplier_active": ("supplier",),
        "rate_positive": ("request", "out:S3"),
    },
    Step.S4: {
        "qty_match": ("doc:S3", "out:S4_receipt"),
        "qty_variance": ("doc:S3", "out:S4_receipt"),
        "amount_variance": ("doc:S3", "out:S4_invoice"),
        "amount_variance_pct": ("doc:S3", "out:S4_invoice"),
        "within_tolerance": ("doc:S3", "out:S4_invoice", "request"),
        "bill_no_not_previously_invoiced": ("query:Purchase Invoice",),
        "three_way_match_clean": (
            "doc:S3",
            "out:S4_receipt",
            "out:S4_invoice",
            "query:Purchase Invoice",
        ),
    },
    Step.S5: {
        "variance": ("doc:S4_invoice", "doc:S3"),
        "variance_pct": ("doc:S4_invoice", "doc:S3"),
        "within_tolerance": ("doc:S4_invoice", "doc:S3", "request"),
        "no_open_discrepancy": ("doc:S4_invoice", "doc:S3", "request"),
        "invoice_submitted": ("doc:S4_invoice",),
        "approved_authority": ("request",),
        "within_approved_authority": ("doc:S4_invoice", "request"),
    },
    Step.S6: {
        "outstanding": ("doc:S4_invoice",),
        "fully_invoiced": ("doc:S4_invoice",),
        "outstanding_equals_total": ("doc:S4_invoice",),
        "not_previously_paid": ("query:Payment Entry",),
        "invoice_submitted": ("doc:S4_invoice",),
        "approved_authority": ("request",),
        "within_approved_authority": ("doc:S4_invoice", "request"),
    },
}


def erp_base_url() -> str:
    """Where a human opens ERPNext. Defaults to the agent's own API address."""
    return (
        os.environ.get("ERPNEXT_PUBLIC_URL")
        or os.environ.get("ERPNEXT_URL")
        or "http://localhost:8080"
    ).rstrip("/")


def doctype_slug(doctype: str) -> str:
    return doctype.lower().replace(" ", "-")


def doc_url(base: str, doctype: str, name: str) -> str:
    """The ERPNext desk page for one document."""
    return f"{base}/app/{doctype_slug(doctype)}/{quote(name, safe='')}"


def list_url(base: str, doctype: str) -> str:
    return f"{base}/app/{doctype_slug(doctype)}"


@dataclass(frozen=True)
class Source:
    kind: str  # "document" | "query" | "request"
    label: str
    doctype: str | None = None
    name: str | None = None
    url: str | None = None
    #: False when the source should exist but is not on record -- an ``out:``
    #: slot for a step that never committed, a ``doc:`` slot never filled.
    resolved: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "doctype": self.doctype,
            "name": self.name,
            "url": self.url,
            "resolved": self.resolved,
        }


_SUMMARY_LINE = re.compile(r"^(?P<key>[a-z_]+): (?P<value>.*)$")


def summary_field(step_context: str, key: str) -> str | None:
    """One ``key: value`` line from a rendered step context, if present."""
    for line in step_context.splitlines():
        m = _SUMMARY_LINE.match(line)
        if m and m.group("key") == key:
            value = m.group("value").strip()
            # Evidence version 2 quarantines ERP text in guillemets
            # (agent.context.untrusted); earlier traces carry it bare.
            if value.startswith(QUOTE_OPEN) and value.endswith(QUOTE_CLOSE):
                value = value[1:-1].strip()
            return value or None
    return None


def resolve(
    ref: str,
    *,
    docs: dict[str, str],
    step_context: str,
    base: str,
) -> Source:
    """Turn one ``FACT_SOURCES`` reference into a linkable source.

    ``docs`` is every document slot known for the workflow (those recorded in the
    trace when the step ran, plus everything committed since), so an ``out:``
    slot resolves once the step that produces it has committed.
    """
    if ref == "request":
        return Source(kind="request", label="procurement request (not an ERPNext document)")

    if ref.startswith("query:"):
        doctype = ref.split(":", 1)[1]
        return Source(
            kind="query",
            label=f"ledger-wide lookup over {doctype}",
            doctype=doctype,
            url=list_url(base, doctype),
        )

    if ref in ("item", "supplier"):
        doctype = "Item" if ref == "item" else "Supplier"
        # Masters are named by their code (Item) or supplier name (Supplier,
        # data.corpus.ensure_supplier); both are rendered into the step context.
        name = summary_field(step_context, ref)
        if name is None:
            return Source(kind="document", label=doctype, doctype=doctype, resolved=False)
        return Source(
            kind="document",
            label=f"{doctype} {name}",
            doctype=doctype,
            name=name,
            url=doc_url(base, doctype, name),
        )

    role, _, slot = ref.partition(":")
    if role not in ("doc", "out") or slot not in SLOT_DOCTYPE:
        raise ValueError(f"unknown provenance reference {ref!r}")
    doctype = SLOT_DOCTYPE[slot]
    name = docs.get(slot)
    verb = "read" if role == "doc" else "committed by this step"
    if not name:
        return Source(
            kind="document",
            label=f"{doctype} ({verb}; not on record)",
            doctype=doctype,
            resolved=False,
        )
    return Source(
        kind="document",
        label=f"{doctype} {name} ({verb})",
        doctype=doctype,
        name=name,
        url=doc_url(base, doctype, name),
    )


def fact_sources(
    step: str, fact: str, *, docs: dict[str, str], step_context: str, base: str
) -> list[Source] | None:
    """Sources for one fact, or ``None`` when the fact is not declared.

    ``None`` is surfaced in the explorer as an unknown provenance rather than
    silently rendered as a fact with no source.
    """
    try:
        refs = FACT_SOURCES[Step(step)].get(fact)
    except ValueError:
        return None
    if refs is None:
        return None
    return [resolve(r, docs=docs, step_context=step_context, base=base) for r in refs]
