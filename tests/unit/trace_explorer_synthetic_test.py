"""Trace explorer reconstruction, diff and API over an in-memory record.

Synthetic: the recorded workflows come from ``tests/trace_scenario.py``, written
through the same Store method calls the pipeline makes. The database-backed
twin of these tests is ``tests/integration/trace_explorer_db_synthetic_test.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from tests.trace_scenario import (
    DOCS,
    MemoryStore,
    assert_verified_reconstruction,
    write_baseline,
    write_verified,
)
from trace.api import create_app
from trace.explorer import diff_attempts, reconstruct

BASE = "http://erp.example"


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    write_verified(s, "v-1")
    write_baseline(s, "b-1")
    s.labels["v-1"] = {
        "fault_class": "compounding",
        "expected_terminal_action": "escalate",
        "fault_step": "S5",
    }
    return s


def test_verified_workflow_reconstructs(store: MemoryStore) -> None:
    recon = reconstruct(store, "v-1", base=BASE)
    assert recon is not None
    assert_verified_reconstruction(recon, "v-1", BASE)
    assert recon["label"]["fault_class"] == "compounding"


def test_baseline_is_identified_and_has_no_gate_fields(store: MemoryStore) -> None:
    recon = reconstruct(store, "b-1", base=BASE)
    assert recon is not None
    assert recon["config"] == "baseline"
    (a,) = recon["steps"][0]["attempts"]
    assert a["verifier"] is None and a["verdict"] is None and a["region"] is None
    assert a["route"] == "commit"


def test_unknown_workflow_is_none(store: MemoryStore) -> None:
    assert reconstruct(store, "nope", base=BASE) is None


def test_retry_diff_shows_the_fed_back_rejection(store: MemoryStore) -> None:
    recon = reconstruct(store, "v-1", base=BASE)
    assert recon is not None
    d = diff_attempts(recon, "S4", 1, 2)
    assert d is not None
    assert any(line.startswith("+PREVIOUS ATTEMPT REJECTED") for line in d["context_diff"])
    assert d["fact_changes"] == []  # same evidence; only the instruction changed
    changed = {c["field"]: (c["before"], c["after"]) for c in d["decision_changes"]}
    assert changed["action"] == ("proceed", "hold")
    assert changed["route"] == ("retry", "commit")
    assert d["expectations_resolved"] == [
        "within_tolerance must be True before an invoice is booked"
    ]


def test_diff_refuses_an_untraced_attempt(store: MemoryStore) -> None:
    recon = reconstruct(store, "v-1", base=BASE)
    assert recon is not None
    assert diff_attempts(recon, "S3", 1, 2) is None


def _client(store: MemoryStore) -> TestClient:
    @contextmanager
    def source() -> Iterator[MemoryStore]:
        yield store

    return TestClient(create_app(source, erp_base=BASE))  # type: ignore[arg-type]


def test_api_endpoints(store: MemoryStore) -> None:
    c = _client(store)
    assert c.get("/api/health").json() == {"status": "ok"}
    ids = {w["workflow_id"] for w in c.get("/api/workflows").json()}
    assert ids == {"v-1", "b-1"}
    assert [w["workflow_id"] for w in c.get("/api/workflows?status=completed").json()] == ["b-1"]

    recon = c.get("/api/workflows/v-1").json()
    assert_verified_reconstruction(recon, "v-1", BASE)

    d = c.get("/api/workflows/v-1/diff", params={"step": "S4", "a": 1, "b": 2}).json()
    assert d["a"] == 1 and d["b"] == 2

    assert c.get("/api/workflows/missing").status_code == 404
    assert (
        c.get("/api/workflows/v-1/diff", params={"step": "S9", "a": 1, "b": 2}).status_code == 404
    )
    assert (
        c.get("/api/workflows/v-1/diff", params={"step": "S4", "a": 0, "b": 2}).status_code == 422
    )


def test_api_serves_the_ui(store: MemoryStore) -> None:
    c = _client(store)
    page = c.get("/")
    assert page.status_code == 200 and "VERITAS" in page.text
    assert c.get("/static/app.js").status_code == 200


def test_provenance_resolves_committed_outputs(store: MemoryStore) -> None:
    """An `out:` source resolves through the commit store even though the trace
    for that attempt was written before the document existed."""
    recon = reconstruct(store, "v-1", base=BASE)
    assert recon is not None
    s1 = recon["steps"][0]["attempts"][0]
    est = next(f for f in s1["facts"] if f["name"] == "estimated_value")
    names = [s["name"] for s in est["sources"]]
    assert names == [None, DOCS["S1"]]
    item = next(f for f in s1["facts"] if f["name"] == "item_is_purchasable")
    assert item["sources"][0]["url"] == f"{BASE}/app/item/USA-272060915"


def test_recorded_reads_confirm_or_contradict_the_declared_sources(store: MemoryStore) -> None:
    recon = reconstruct(store, "v-1", base=BASE)
    assert recon is not None
    steps = {s["step"]: s for s in recon["steps"]}

    s4 = steps["S4"]["attempts"][0]
    assert [r["url"] for r in s4["erp_reads"]] == [
        f"{BASE}/app/purchase-order/{DOCS['S3']}",
        f"{BASE}/app/purchase-invoice",
    ]
    facts = {f["name"]: f for f in s4["facts"]}
    po, receipt = facts["qty_match"]["sources"]
    assert po["read"] is True  # read at this step
    assert receipt["read"] is None  # committed by this step afterwards, never read
    assert facts["bill_no_not_previously_invoiced"]["sources"][0]["read"] is True

    # Reads recorded but empty: the table claims a source the record does not show.
    s5 = steps["S5"]["attempts"][0]
    auth = next(f for f in s5["facts"] if f["name"] == "within_approved_authority")
    invoice, request = auth["sources"]
    assert invoice["read"] is False and request["read"] is None

    # A trace from before reads were recorded says nothing either way.
    s1 = steps["S1"]["attempts"][0]
    assert s1["erp_reads"] is None
    assert all(src["read"] is None for f in s1["facts"] for src in (f["sources"] or []))
