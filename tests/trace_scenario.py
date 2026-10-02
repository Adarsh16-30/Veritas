"""Recorded-workflow scenarios for the trace explorer tests (synthetic).

Written through the same ``Store`` methods, in the same order and with the same
payload shapes, as ``agent/pipeline.py`` writes them, so the explorer is tested
against the record the pipeline actually produces. The unit tests replay this
into an in-memory double; the database test replays it into a real Postgres in
a throwaway schema. Neither touches ERPNext.

Two workflows:

``VERIFIED`` -- S1..S3 commit; S4 attempt 1 is rejected by the verifier and
retried, attempt 2 commits; S5 trips a hard invariant and escalates. Attempt 2
at S3 is deliberately untraced (an ``ExecutorError`` leaves no row), with
attempt 1 never existing for it either -- see ``untraced`` below.

``BASELINE`` -- the same request with no gate: every step commits on the
executor's own say-so.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

DOCS = {
    "S1": "MAT-MR-2026-00001",
    "S3": "PUR-ORD-2026-00001",
    "S4_receipt": "MAT-PRE-2026-00001",
    "S4_invoice": "ACC-PINV-2026-00001",
}
DOCTYPE = {
    "S1": "Material Request",
    "S3": "Purchase Order",
    "S4_receipt": "Purchase Receipt",
    "S4_invoice": "Purchase Invoice",
}

S4_CONTEXT = (
    "STEP: S4 — three-way match the order, the receipt and the supplier invoice\n"
    "purchase_order: PUR-ORD-2026-00001\n"
    "po_total: 400.00\n"
    "invoiced_rate: 103.00\n"
    "DELTA: qty_match=True, amount_variance=12.00, amount_variance_pct=3.000, "
    "within_tolerance=False, bill_no_not_previously_invoiced=True, three_way_match_clean=False"
)
S4_FACTS_1 = {
    "qty_match": "True",
    "qty_variance": "0",
    "amount_variance": "12.00",
    "amount_variance_pct": "3.000",
    "within_tolerance": "False",
    "bill_no_not_previously_invoiced": "True",
    "three_way_match_clean": "False",
}
REJECTION = "verifier_rejected: within_tolerance must be True before an invoice is booked"


class Writer(Protocol):
    def create_workflow(self, workflow_id: str, step: Any, label: str | None = None) -> None: ...
    def checkpoint(self, workflow_id: str, step: Any, status: str) -> None: ...
    def append_trace(self, **kw: Any) -> int: ...
    def record_attempt(self, *a: Any, **kw: Any) -> None: ...
    def commit_binding(self, workflow_id: str, step: Any, doctype: str) -> Any: ...
    def record_committed(self, key: str, name: str) -> None: ...
    def set_status(self, workflow_id: str, status: str, reason: str | None = None) -> None: ...


def _ctx(step: str, *lines: str) -> str:
    return "\n".join([f"STEP: {step} — purpose", *lines])


def _executor(
    w: Writer,
    wf: str,
    step: str,
    attempt: int,
    ctx: str,
    facts: dict[str, str],
    docs: dict[str, str],
    action: str,
    rationale: str,
) -> int:
    return w.append_trace(
        workflow_id=wf,
        step=step,
        attempt=attempt,
        step_context=ctx,
        prompt=f"EXECUTOR PROMPT\n{ctx}",
        prompt_hash=f"p-{wf}-{step}-{attempt}",
        response=f'{{"action": "{action}", "rationale": "{rationale}"}}',
        response_hash=f"r-{wf}-{step}-{attempt}",
        model="llama3:8b-instruct-q4_K_M",
        provenance={
            "erp_documents": docs,
            "idempotency_key": f"key-{wf}-{step}",
            "facts": facts,
            "amount_at_stake": "400.00",
            "stage": "executor",
            "rationale": rationale,
            "action": action,
        },
    )


def _verifier(
    w: Writer,
    wf: str,
    step: str,
    attempt: int,
    ctx: str,
    passed: bool,
    violated: list[str],
) -> None:
    w.append_trace(
        workflow_id=wf,
        step=step,
        attempt=attempt,
        step_context=ctx,
        prompt=f"VERIFIER PROMPT (no rationale)\n{ctx}",
        prompt_hash=f"vp-{wf}-{step}-{attempt}",
        response="[...]",
        response_hash=f"vr-{wf}-{step}-{attempt}",
        model="qwen2.5-coder:7b",
        provenance={
            "erp_documents": {},
            "idempotency_key": f"key-{wf}-{step}",
            "facts": {},
            "amount_at_stake": "400.00",
            "stage": "verifier",
            "verdict": "pass" if passed else "fail",
            "violated_expectations": violated,
            "confidence": 0.9,
            "checklist": ["within_tolerance must be True before an invoice is booked"],
            "independence": {"executor_family": "llama", "verifier_family": "qwen"},
            "rules": {"ok": True, "checked": [], "violations": []},
        },
    )


def _gate(
    route: str,
    reason: str | None,
    passed: bool,
    violated: list[str],
    rule_violations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "route": route,
        "reason": reason,
        "rules": {"ok": not rule_violations, "checked": [], "violations": rule_violations or []},
        "verdict": {"passed": passed, "violated_expectations": violated, "confidence": 0.9},
        "region": {"labels": [route], "p_commit": None, "calibrated": False, "alpha": None},
        "signals": {"rules_ok": 1.0},
        "verifier_latency_ms": 2100,
        "verifier_model": "qwen2.5-coder:7b",
    }


def _commit(w: Writer, wf: str, step: str, slot: str) -> None:
    with w.commit_binding(wf, step, DOCTYPE[slot]):
        w.record_committed(f"key-{wf}-{slot}", DOCS[slot])


def write_verified(w: Writer, wf: str) -> None:
    w.create_workflow(wf, "S1")
    docs: dict[str, str] = {}
    for step, ctx, facts in [
        (
            "S1",
            _ctx("S1", "item: USA-272060915", "DELTA: item_is_purchasable=True"),
            {"item_is_purchasable": "True", "estimated_value": "400.00"},
        ),
        ("S2", _ctx("S2", "DELTA: mr_submitted=True"), {"mr_submitted": "True"}),
        (
            "S3",
            _ctx("S3", "supplier: ACME SUPPLY CO", "DELTA: supplier_active=True"),
            {"supplier_active": "True"},
        ),
    ]:
        # S3's first attempt failed to parse: no trace, no attempt row.
        attempt = 2 if step == "S3" else 1
        w.checkpoint(wf, step, "in_progress")
        tid = _executor(
            w, wf, step, attempt, ctx, facts, dict(docs), "proceed", f"{step} looks fine"
        )
        _verifier(w, wf, step, attempt, ctx, True, [])
        if step in ("S1", "S3"):
            _commit(w, wf, step, step)
            docs[step] = DOCS[step]
        w.record_attempt(
            wf,
            step,
            attempt,
            "proceed",
            tid,
            True,
            DOCS.get(step),
            900,
            verdict="pass",
            confidence=0.9,
            region="commit",
            signals={"rules_ok": 1.0},
            gate=_gate("commit", None, True, []),
        )
        w.checkpoint(wf, step, "committed")

    # S4: rejected, retried with the reason fed back, then committed.
    w.checkpoint(wf, "S4", "in_progress")
    tid = _executor(
        w, wf, "S4", 1, S4_CONTEXT, S4_FACTS_1, dict(docs), "proceed", "variance is small"
    )
    violated = ["within_tolerance must be True before an invoice is booked"]
    _verifier(w, wf, "S4", 1, S4_CONTEXT, False, violated)
    w.record_attempt(
        wf,
        "S4",
        1,
        "proceed",
        tid,
        False,
        None,
        1100,
        verdict="fail",
        confidence=0.9,
        region="retry",
        signals={},
        gate=_gate("retry", REJECTION, False, violated),
    )
    ctx2 = S4_CONTEXT + f"\nPREVIOUS ATTEMPT REJECTED: {REJECTION}"
    tid = _executor(w, wf, "S4", 2, ctx2, S4_FACTS_1, dict(docs), "hold", "over tolerance")
    _verifier(w, wf, "S4", 2, ctx2, True, [])
    _commit(w, wf, "S4", "S4_receipt")
    _commit(w, wf, "S4", "S4_invoice")
    w.record_attempt(
        wf,
        "S4",
        2,
        "hold",
        tid,
        True,
        DOCS["S4_invoice"],
        1000,
        verdict="pass",
        confidence=0.9,
        region="commit",
        signals={},
        gate=_gate("commit", None, True, []),
    )
    w.checkpoint(wf, "S4", "committed")
    docs.update({"S4_receipt": DOCS["S4_receipt"], "S4_invoice": DOCS["S4_invoice"]})

    # S5: a hard invariant fails; terminal.
    w.checkpoint(wf, "S5", "in_progress")
    s5 = {"within_approved_authority": "False", "approved_authority": "300.00"}
    tid = _executor(
        w,
        wf,
        "S5",
        1,
        _ctx("S5", "DELTA: within_approved_authority=False"),
        s5,
        dict(docs),
        "proceed",
        "variance resolved",
    )
    rv = [
        {
            "rule_id": "S5_WITHIN_APPROVED_AUTHORITY",
            "message": "over authority",
            "source": "invariants.yaml",
            "kind": "hard",
        }
    ]
    w.record_attempt(
        wf,
        "S5",
        1,
        "proceed",
        tid,
        False,
        None,
        800,
        gate=_gate("escalate", "hard_rule_violation: S5_WITHIN_APPROVED_AUTHORITY", False, [], rv),
    )
    w.checkpoint(wf, "S5", "escalated")
    w.set_status(wf, "escalated", "hard_rule_violation: S5_WITHIN_APPROVED_AUTHORITY")


def write_baseline(w: Writer, wf: str) -> None:
    w.create_workflow(wf, "S1")
    w.checkpoint(wf, "S1", "in_progress")
    ctx = _ctx("S1", "item: USA-272060915")
    tid = _executor(w, wf, "S1", 1, ctx, {"item_is_purchasable": "True"}, {}, "proceed", "ok")
    _commit(w, wf, "S1", "S1")
    # Baseline: verdict/region/gate stay NULL -- "no gate ran" (Store.record_attempt).
    w.record_attempt(wf, "S1", 1, "proceed", tid, True, DOCS["S1"], 700)
    w.checkpoint(wf, "S1", "committed")
    w.set_status(wf, "completed")


class MemoryStore:
    """In-memory double of the ``Store`` methods the scenario and explorer use."""

    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.checkpoints: dict[tuple[str, str], str] = {}
        self.traces: list[dict[str, Any]] = []
        self.attempts: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.commits: list[dict[str, Any]] = []
        self.labels: dict[str, dict[str, Any]] = {}
        self._binding: tuple[str, str, str] | None = None

    def create_workflow(self, workflow_id: str, step: Any, label: str | None = None) -> None:
        self.workflows.setdefault(
            workflow_id,
            {
                "workflow_id": workflow_id,
                "current_step": step,
                "status": "pending",
                "escalation_reason": None,
                "llm_calls": 0,
                "created_at": None,
                "last_checkpoint_ts": None,
            },
        )

    def checkpoint(self, workflow_id: str, step: Any, status: str) -> None:
        self.checkpoints[(workflow_id, str(step))] = status
        self.workflows[workflow_id].update(current_step=str(step), status=status)

    def append_trace(self, **kw: Any) -> int:
        row = {"id": len(self.traces) + 1, "created_at": None, **kw}
        self.traces.append(row)
        return int(row["id"])

    def record_attempt(
        self,
        workflow_id: str,
        step: Any,
        attempt: int,
        action: str | None,
        rationale_ref: int | None,
        committed: bool,
        doc_name: str | None,
        latency_ms: int,
        verdict: str | None = None,
        confidence: float | None = None,
        region: str | None = None,
        signals: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
    ) -> None:
        self.attempts[(workflow_id, str(step), attempt)] = {
            "workflow_id": workflow_id,
            "step": str(step),
            "attempt": attempt,
            "action": action,
            "rationale_ref": rationale_ref,
            "committed": committed,
            "doc_name": doc_name,
            "latency_ms": latency_ms,
            "verdict": verdict,
            "confidence": confidence,
            "region": region,
            "signals": signals or {},
            "gate": gate or {},
        }

    @contextmanager
    def commit_binding(self, workflow_id: str, step: Any, doctype: str) -> Iterator[None]:
        self._binding = (workflow_id, str(step), doctype)
        yield
        self._binding = None

    def record_committed(self, key: str, name: str) -> None:
        wf, step, doctype = self._binding or ("", "", "")
        self.commits.append(
            {
                "workflow_id": wf,
                "step": step,
                "doctype": doctype,
                "doc_name": name,
                "idempotency_key": key,
            }
        )

    def set_status(self, workflow_id: str, status: str, reason: str | None = None) -> None:
        self.workflows[workflow_id].update(status=status, escalation_reason=reason)

    # --- read side -------------------------------------------------------------
    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.workflows.get(workflow_id)

    def label_for(self, workflow_id: str) -> dict[str, Any] | None:
        return self.labels.get(workflow_id)

    def checkpoints_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return [
            {"step": s, "status": st, "ts": None}
            for (w, s), st in sorted(self.checkpoints.items())
            if w == workflow_id
        ]

    def commits_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return [c for c in self.commits if c["workflow_id"] == workflow_id]

    def traces_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return [t for t in self.traces if t["workflow_id"] == workflow_id]

    def attempts_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return [a for (w, _, _), a in sorted(self.attempts.items()) if w == workflow_id]

    def list_workflows(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            w
            for w in self.workflows.values()
            if (status is None or w["status"] == status)
            and (prefix is None or w["workflow_id"].startswith(prefix))
        ]
        return rows[offset : offset + limit]


def assert_verified_reconstruction(recon: dict[str, Any], wf: str, base: str) -> None:
    """What a reviewer must be able to read off the VERIFIED workflow (PRD Phase 5)."""
    assert recon["workflow"]["workflow_id"] == wf
    assert recon["config"] == "verified"
    assert recon["workflow"]["status"] == "escalated"
    assert recon["workflow"]["terminal_action"] == "escalate"
    steps = {s["step"]: s for s in recon["steps"]}
    assert list(steps) == ["S1", "S2", "S3", "S4", "S5", "S6"]

    # S3: attempt 1 left no record and is shown as such, not dropped.
    s3 = steps["S3"]["attempts"]
    assert s3[0] == {"attempt": 1, "untraced": True}
    assert s3[1]["committed"] is True

    # S4: what it saw, what it proposed and why, what the verifier said.
    a1, a2 = steps["S4"]["attempts"]
    assert a1["action"] == "proceed" and a1["route"] == "retry" and not a1["committed"]
    assert a1["executor"]["rationale"] == "variance is small"
    assert a1["verifier"]["verdict"] == "fail"
    assert a1["verifier"]["violated_expectations"] == [
        "within_tolerance must be True before an invoice is booked"
    ]
    assert "rationale" not in a1["verifier"]  # Rule 3, as displayed
    assert a1["region"]["calibrated"] is False
    assert a2["committed"] and a2["doc_name"] == DOCS["S4_invoice"]
    assert "PREVIOUS ATTEMPT REJECTED" in a2["step_context"]

    # Every fact the step saw links to the real document it came from -- including
    # the S4 receipt, which only exists because the step later committed it.
    facts = {f["name"]: f for f in a1["facts"]}
    assert facts["within_tolerance"]["check"] is False
    urls = [s["url"] for s in facts["qty_match"]["sources"]]
    assert urls == [
        f"{base}/app/purchase-order/{DOCS['S3']}",
        f"{base}/app/purchase-receipt/{DOCS['S4_receipt']}",
    ]

    # S5: the hard rule is what stopped it, and the workflow says so.
    s5 = steps["S5"]["attempts"][0]
    assert s5["route"] == "escalate"
    assert s5["rules"]["violations"][0]["rule_id"] == "S5_WITHIN_APPROVED_AUTHORITY"
    assert steps["S5"]["checkpoint"] == "escalated"
    assert steps["S6"]["attempts"] == [] and steps["S6"]["checkpoint"] is None

    assert {d["doctype"] for d in recon["documents"]} == set(DOCTYPE.values())


def _observability_rows(
    self: MemoryStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mirror of ``Store.observability_rows`` over the in-memory record."""
    workflows = [
        {k: w[k] for k in ("workflow_id", "status", "escalation_reason", "llm_calls")}
        for w in self.workflows.values()
    ]
    attempts = []
    for a in self.attempts.values():
        gate = a.get("gate") or {}
        attempts.append(
            {
                "workflow_id": a["workflow_id"],
                "step": a["step"],
                "attempt": a["attempt"],
                "action": a["action"],
                "committed": a["committed"],
                "latency_ms": a["latency_ms"],
                "route": gate.get("route"),
                "verifier_latency_ms": gate.get("verifier_latency_ms"),
                "gated": bool(gate),
            }
        )
    return workflows, attempts


MemoryStore.observability_rows = _observability_rows  # type: ignore[attr-defined]
