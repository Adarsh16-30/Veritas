"""``trace.provenance.FACT_SOURCES`` must describe what the assembler really does.

The explorer resolves "which document is this fact from" through a declared
table, because recorded traces do not carry that mapping. A table that drifts
from the builders it describes would put a confident, wrong link in front of a
reviewer -- the worst kind of provenance. These tests run every real builder in
``agent.context`` and pin the table to it.

Synthetic: a recording ERP double stands in for ERPNext. It exists only so the
builders can run; nothing here claims anything about the ledger (Rule 1).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.context import ContextAssembler, WorkflowSpec
from agent.state import Step, StepContext
from trace.provenance import FACT_SOURCES, SLOT_DOCTYPE, fact_sources, resolve, summary_field

DOCS = {
    "S1": "MAT-MR-0001",
    "S3": "PUR-ORD-0001",
    "S4_receipt": "MAT-PRE-0001",
    "S4_invoice": "ACC-PINV-0001",
}


class RecordingERP:
    """Answers just enough for each builder to run, and records what it read."""

    cash_account = "Cash - VTC"

    def __init__(self) -> None:
        self.read: set[str] = set()
        self.queried: set[str] = set()

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        self.read.add(doctype)
        return {
            "name": name,
            "item_code": "ITEM-1",
            "item_name": "Aircraft Hydraulic Pump Assembly",
            "is_purchase_item": 1,
            "supplier_name": "ACME SUPPLY CO",
            "disabled": 0,
            "status": "Submitted",
            "docstatus": 1,
            "items": [{"qty": 4}],
            "grand_total": 400.0,
            "outstanding_amount": 400.0,
            "supplier": "ACME SUPPLY CO",
        }

    def get_list(self, doctype: str, **_: Any) -> list[dict[str, Any]]:
        self.queried.add(doctype)
        return []

    def duplicate_bill_exists(self, *_: Any, **__: Any) -> bool:
        self.queried.add("Purchase Invoice")
        return False


SPEC = WorkflowSpec(
    workflow_id="wf-prov",
    item_code="ITEM-1",
    qty=4,
    supplier="ACME SUPPLY CO",
    rate=100.0,
    needed_by="2999-01-01",
    bill_no="BILL-1",
)


def _run(step: Step) -> tuple[StepContext, RecordingERP]:
    erp = RecordingERP()
    asm = ContextAssembler(erp, SPEC)  # type: ignore[arg-type]
    ctx = StepContext(workflow_id=SPEC.workflow_id, step=step, docs=dict(DOCS))
    asm.assemble(ctx)
    return ctx, erp


@pytest.mark.parametrize("step", list(Step))
def test_every_emitted_fact_is_declared_and_nothing_more(step: Step) -> None:
    ctx, _ = _run(step)
    assert set(ctx.facts) == set(FACT_SOURCES[step])


def _doctypes(refs: set[str]) -> set[str]:
    out: set[str] = set()
    for ref in refs:
        if ref == "item":
            out.add("Item")
        elif ref == "supplier":
            out.add("Supplier")
        elif ref.startswith("doc:"):
            out.add(SLOT_DOCTYPE[ref[4:]])
    return out


@pytest.mark.parametrize("step", list(Step))
def test_every_document_read_is_some_facts_source(step: Step) -> None:
    """A builder that reads a doctype no fact points at means a fact's source is
    missing from the table (or the read is dead)."""
    _, erp = _run(step)
    refs = {r for refs in FACT_SOURCES[step].values() for r in refs}
    assert erp.read == _doctypes(refs), step
    assert erp.queried == {r[6:] for r in refs if r.startswith("query:")}, step


def test_master_names_are_recoverable_from_the_rendered_context() -> None:
    """Item/Supplier links are resolved from the step context, so the summary
    keys they are read from must keep being rendered."""
    s1, _ = _run(Step.S1)
    s3, _ = _run(Step.S3)
    assert summary_field(s1.step_context, "item") == "ITEM-1"
    assert summary_field(s3.step_context, "supplier") == "ACME SUPPLY CO"


def test_resolution_links_real_documents_and_admits_the_request() -> None:
    ctx, _ = _run(Step.S4)
    srcs = fact_sources(
        "S4", "qty_match", docs=DOCS, step_context=ctx.step_context, base="http://erp"
    )
    assert srcs is not None
    assert [s.url for s in srcs] == [
        "http://erp/app/purchase-order/PUR-ORD-0001",
        "http://erp/app/purchase-receipt/MAT-PRE-0001",
    ]
    request = resolve("request", docs=DOCS, step_context="", base="http://erp")
    assert request.url is None and "not an ERPNext document" in request.label


def test_an_output_slot_never_committed_is_unresolved_not_invented() -> None:
    src = resolve("out:S4_invoice", docs={"S3": "PUR-ORD-0001"}, step_context="", base="b")
    assert src.resolved is False and src.url is None


def test_unknown_fact_is_reported_as_unknown() -> None:
    assert fact_sources("S4", "no_such_fact", docs={}, step_context="", base="b") is None
    assert fact_sources("S9", "qty_match", docs={}, step_context="", base="b") is None


def test_document_names_are_url_quoted() -> None:
    src = resolve("supplier", docs={}, step_context="supplier: A/B & C", base="http://erp")
    assert src.url == "http://erp/app/supplier/A%2FB%20%26%20C"


def test_summary_fields_read_both_trace_generations() -> None:
    """Traces before evidence version 2 carry ERP text bare; after, quoted."""
    assert summary_field("item: USA-1", "item") == "USA-1"
    assert summary_field("item: \u00abUSA-1\u00bb", "item") == "USA-1"
    assert summary_field("supplier: \u00ab\u00bb", "supplier") is None


@pytest.mark.parametrize("step", list(Step))
def test_the_assembler_records_exactly_the_reads_it_makes(step: Step) -> None:
    """ctx.erp_reads is the record the explorer trusts: it must match what the
    builder actually asked ERPNext for, documents and ledger-wide lookups alike."""
    ctx, erp = _run(step)
    assert {r["doctype"] for r in ctx.erp_reads if "name" in r} == erp.read
    assert {r["doctype"] for r in ctx.erp_reads if "query" in r} == erp.queried
