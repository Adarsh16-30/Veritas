"""Reconstruct one workflow from the append-only trace store (PRD Phase 5).

The success criterion for Phase 5 is that, for any historical workflow, a
reviewer can see what the agent saw, what it decided, why the verifier passed
or failed it, and with what confidence -- and that every fact traces to a real
ERPNext document. Everything here is a pure function over rows the pipeline has
already written. Nothing is recomputed from the ERP, nothing is re-asked of a
model, and nothing is written: an explorer that could change the record it
displays would not be an audit tool.

Two things the record genuinely does not contain are shown as gaps, never
filled in:

* An attempt whose executor output was rejected before it could be traced
  (``ExecutorError`` in ``agent/pipeline.py``) leaves no row. Its number is
  missing from the sequence, and the next attempt's context carries the
  rejection reason. The explorer lists it as ``untraced``.
* A fact the provenance table does not declare is shown with unknown sources,
  not with a guessed link.
"""

from __future__ import annotations

import difflib
from datetime import datetime
from typing import Any, Protocol

from agent.state import Step
from trace.provenance import SLOT_DOCTYPE, doc_url, erp_base_url, fact_sources, list_url

_STEP_ORDER = [s.value for s in Step]
_DOCTYPE_SLOT = {doctype: slot for slot, doctype in SLOT_DOCTYPE.items()}


class TraceSource(Protocol):
    """The read side of ``orchestrator.db.Store`` the explorer depends on."""

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None: ...
    def label_for(self, workflow_id: str) -> dict[str, Any] | None: ...
    def checkpoints_for(self, workflow_id: str) -> list[dict[str, Any]]: ...
    def commits_for(self, workflow_id: str) -> list[dict[str, Any]]: ...
    def traces_for(self, workflow_id: str) -> list[dict[str, Any]]: ...
    def attempts_for(self, workflow_id: str) -> list[dict[str, Any]]: ...


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def _check(value: str) -> bool | None:
    """DELTA booleans are recorded as strings. Every boolean fact is phrased as a
    check that passed (``agent/context.py``), so True is safe and False is not."""
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _stage(trace: dict[str, Any]) -> str:
    # Phase 2 traces predate the `stage` tag; the executor was the only caller.
    return str((trace.get("provenance") or {}).get("stage") or "executor")


def known_documents(traces: list[dict[str, Any]], commits: list[dict[str, Any]]) -> dict[str, str]:
    """Every document slot the workflow is known to have filled.

    Taken from what each trace saw at the time *and* from the commit store, so
    a step's own output (an ``out:`` source) resolves once it has committed.
    """
    docs: dict[str, str] = {}
    for t in traces:
        for slot, name in ((t.get("provenance") or {}).get("erp_documents") or {}).items():
            docs.setdefault(str(slot), str(name))
    for c in commits:
        slot = _DOCTYPE_SLOT.get(str(c.get("doctype")))
        if slot and c.get("doc_name"):
            docs[slot] = str(c["doc_name"])
    return docs


def _facts(
    step: str, provenance: dict[str, Any], docs: dict[str, str], step_context: str, base: str
) -> list[dict[str, Any]]:
    reads = provenance.get("erp_reads")
    out: list[dict[str, Any]] = []
    for name, value in (provenance.get("facts") or {}).items():
        sources = fact_sources(step, name, docs=docs, step_context=step_context, base=base)
        out.append(
            {
                "name": name,
                "value": value,
                "check": _check(str(value)),
                "sources": None
                if sources is None
                else [{**s.as_dict(), "read": was_read(s.as_dict(), reads)} for s in sources],
            }
        )
    return out


def was_read(source: dict[str, Any], reads: list[dict[str, str]] | None) -> bool | None:
    """Whether the recorded reads of this attempt include a declared source.

    ``None`` when it cannot be said: traces from before reads were recorded, the
    procurement request (not an ERPNext read), and documents this step itself
    committed (written after the facts, never read for them). ``False`` is a
    declared source the step did not read -- a disagreement between the
    provenance table and the record, which the explorer shows rather than hides.
    """
    if reads is None or source["kind"] == "request" or "committed by this step" in source["label"]:
        return None
    if source["kind"] == "query":
        return any(r.get("doctype") == source["doctype"] and "query" in r for r in reads)
    return any(
        r.get("doctype") == source["doctype"] and r.get("name") == source["name"] for r in reads
    )


def _call(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    if trace is None:
        return None
    prov = trace.get("provenance") or {}
    call = {
        "trace_id": trace.get("id"),
        "model": trace.get("model"),
        "prompt": trace.get("prompt"),
        "response": trace.get("response"),
        "prompt_hash": trace.get("prompt_hash"),
        "response_hash": trace.get("response_hash"),
        "created_at": trace.get("created_at"),
    }
    if _stage(trace) == "executor":
        call["action"] = prov.get("action")
        call["rationale"] = prov.get("rationale")
    else:
        call["verdict"] = prov.get("verdict")
        call["violated_expectations"] = prov.get("violated_expectations") or []
        call["confidence"] = prov.get("confidence")
        call["checklist"] = prov.get("checklist") or []
        call["independence"] = prov.get("independence")
    return call


def _route(attempt: dict[str, Any] | None, gate: dict[str, Any]) -> str | None:
    if gate.get("route"):
        return str(gate["route"])
    if attempt is None:
        return None
    if attempt.get("committed"):
        return "commit"
    # Baseline: the executor's own choice routes the step (agent/pipeline.py _ROUTE_OF).
    return "commit" if attempt.get("action") == "proceed" else "escalate"


def reconstruct(
    src: TraceSource, workflow_id: str, base: str | None = None
) -> dict[str, Any] | None:
    """The whole workflow, step by step and attempt by attempt."""
    wf = src.get_workflow(workflow_id)
    if wf is None:
        return None
    base = base or erp_base_url()

    traces = src.traces_for(workflow_id)
    attempts = {(a["step"], int(a["attempt"])): a for a in src.attempts_for(workflow_id)}
    commits = src.commits_for(workflow_id)
    checkpoints = {c["step"]: c for c in src.checkpoints_for(workflow_id)}
    docs = known_documents(traces, commits)

    by_call: dict[tuple[str, int, str], dict[str, Any]] = {}
    for t in traces:
        # Append-only: a key seen twice keeps the later row, which is what the
        # pipeline acted on (a resumed step re-traces its attempt).
        by_call[(t["step"], int(t["attempt"]), _stage(t))] = t

    steps: list[dict[str, Any]] = []
    gated = False
    for step in _STEP_ORDER:
        numbers = sorted(
            {n for (s, n, _) in by_call if s == step} | {n for (s, n) in attempts if s == step}
        )
        rows: list[dict[str, Any]] = []
        for n in range(1, (max(numbers) if numbers else 0) + 1):
            ex = by_call.get((step, n, "executor"))
            ver = by_call.get((step, n, "verifier"))
            att = attempts.get((step, n))
            if ex is None and att is None:
                rows.append({"attempt": n, "untraced": True})
                continue
            gate = dict((att or {}).get("gate") or {})
            gated = gated or bool(gate)
            prov = dict((ex or {}).get("provenance") or {})
            step_context = str((ex or {}).get("step_context") or "")
            commit_name = (att or {}).get("doc_name")
            rows.append(
                {
                    "attempt": n,
                    "untraced": False,
                    "step_context": step_context,
                    "amount_at_stake": prov.get("amount_at_stake"),
                    "idempotency_key": prov.get("idempotency_key"),
                    "facts": _facts(step, prov, docs, step_context, base),
                    # None for traces recorded before reads were (evidence version 1).
                    "erp_reads": None
                    if prov.get("erp_reads") is None
                    else [
                        {
                            **r,
                            "url": doc_url(base, r["doctype"], r["name"])
                            if r.get("name")
                            else list_url(base, r["doctype"]),
                        }
                        for r in prov["erp_reads"]
                    ],
                    "executor": _call(ex),
                    "verifier": _call(ver),
                    "action": (att or {}).get("action") or prov.get("action"),
                    "verdict": (att or {}).get("verdict"),
                    "confidence": (att or {}).get("confidence"),
                    "region": gate.get("region"),
                    "rules": gate.get("rules"),
                    "route": _route(att, gate),
                    "reason": gate.get("reason"),
                    "signals": gate.get("signals") or (att or {}).get("signals") or {},
                    "committed": bool((att or {}).get("committed")),
                    "outcome_recorded": att is not None,
                    "doc_name": commit_name,
                    "latency_ms": (att or {}).get("latency_ms"),
                    "verifier_latency_ms": gate.get("verifier_latency_ms"),
                }
            )
        cp = checkpoints.get(step)
        steps.append(
            {
                "step": step,
                "checkpoint": None if cp is None else cp.get("status"),
                "checkpoint_ts": None if cp is None else cp.get("ts"),
                "attempts": rows,
            }
        )

    documents = [
        {
            "step": c.get("step"),
            "doctype": c.get("doctype"),
            "name": c.get("doc_name"),
            "idempotency_key": c.get("idempotency_key"),
            "url": doc_url(base, str(c.get("doctype")), str(c.get("doc_name"))),
        }
        for c in commits
    ]

    status = wf.get("status")
    # The gate record is NULL in the baseline configuration by design
    # (Store.record_attempt), so its presence is what identifies the config.
    config = "unknown" if not attempts else ("verified" if gated else "baseline")

    result: dict[str, Any] = _json_safe(
        {
            "workflow": {
                "workflow_id": wf.get("workflow_id"),
                "status": status,
                "current_step": wf.get("current_step"),
                "escalation_reason": wf.get("escalation_reason"),
                "llm_calls": wf.get("llm_calls"),
                "created_at": wf.get("created_at"),
                "last_checkpoint_ts": wf.get("last_checkpoint_ts"),
                "terminal_action": {"completed": "proceed", "escalated": "escalate"}.get(
                    str(status)
                ),
            },
            "label": src.label_for(workflow_id),
            "config": config,
            "erp_base_url": base,
            "documents": documents,
            "steps": steps,
        }
    )
    return result


def diff_attempts(recon: dict[str, Any], step: str, a: int, b: int) -> dict[str, Any] | None:
    """What changed between two attempts of one step (the retry diff view).

    A retry is re-assembled from the ERP and carries the previous rejection
    reason, so the context diff shows exactly what the executor was told the
    second time; the fact diff shows whether the evidence itself moved.
    """
    found = next((s for s in recon["steps"] if s["step"] == step), None)
    if found is None:
        return None
    rows = {r["attempt"]: r for r in found["attempts"] if not r.get("untraced")}
    if a not in rows or b not in rows:
        return None
    ra, rb = rows[a], rows[b]
    context = list(
        difflib.unified_diff(
            ra["step_context"].splitlines(),
            rb["step_context"].splitlines(),
            fromfile=f"{step} attempt {a}",
            tofile=f"{step} attempt {b}",
            lineterm="",
        )
    )
    fa = {f["name"]: f["value"] for f in ra["facts"]}
    fb = {f["name"]: f["value"] for f in rb["facts"]}
    facts = [
        {"name": k, "before": fa.get(k), "after": fb.get(k)}
        for k in sorted(set(fa) | set(fb))
        if fa.get(k) != fb.get(k)
    ]
    fields = ["action", "verdict", "confidence", "route", "reason", "committed"]
    decision = [
        {"field": f, "before": ra.get(f), "after": rb.get(f)}
        for f in fields
        if ra.get(f) != rb.get(f)
    ]
    exp_a = set((ra.get("verifier") or {}).get("violated_expectations") or [])
    exp_b = set((rb.get("verifier") or {}).get("violated_expectations") or [])
    return {
        "step": step,
        "a": a,
        "b": b,
        "context_diff": context,
        "fact_changes": facts,
        "decision_changes": decision,
        "expectations_resolved": sorted(exp_a - exp_b),
        "expectations_new": sorted(exp_b - exp_a),
    }
