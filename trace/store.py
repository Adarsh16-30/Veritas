"""TraceLogger — the append-only decision record (PRD §3.3, §4.1).

One row per executor call. Records what the agent was shown, what it proposed,
the prompt/response hashes that make the decision auditable (Rule 2), and
provenance links back to the real ERPNext documents involved so any fact in a
trace can be walked back to the ledger (Phase 5 relies on this).

The table refuses UPDATE and DELETE at the database level. Nothing here can
rewrite history.
"""

from __future__ import annotations

from typing import Any

from agent.state import StepContext
from orchestrator.db import Store


class TraceLogger:
    def __init__(self, store: Store) -> None:
        self.store = store

    def record(
        self,
        ctx: StepContext,
        prompt: str | None,
        prompt_hash: str,
        response: str | None,
        response_hash: str,
        model: str | None,
        extra_provenance: dict[str, Any] | None = None,
    ) -> int:
        provenance: dict[str, Any] = {
            "erp_documents": dict(ctx.docs),
            "idempotency_key": ctx.idempotency_key,
            "facts": {k: str(v) for k, v in ctx.facts.items()},
            "amount_at_stake": str(ctx.amount_at_stake),
        }
        if extra_provenance:
            provenance.update(extra_provenance)
        return self.store.append_trace(
            workflow_id=ctx.workflow_id,
            step=ctx.step,
            attempt=ctx.attempt,
            step_context=ctx.step_context,
            prompt=prompt,
            prompt_hash=prompt_hash,
            response=response,
            response_hash=response_hash,
            model=model,
            provenance=provenance,
        )

    def reconstruct(self, workflow_id: str) -> list[dict[str, Any]]:
        """Everything the agent saw and decided, in order. Phase 5 builds the UI
        for this; the query lives here so the data contract is exercised now."""
        traces = self.store.traces_for(workflow_id)
        attempts = {(a["step"], a["attempt"]): a for a in self.store.attempts_for(workflow_id)}
        out = []
        for t in traces:
            a = attempts.get((t["step"], t["attempt"]), {})
            out.append(
                {
                    "step": t["step"],
                    "attempt": t["attempt"],
                    "step_context": t["step_context"],
                    "prompt_hash": t["prompt_hash"],
                    "response_hash": t["response_hash"],
                    "model": t["model"],
                    "action": a.get("action"),
                    "committed": a.get("committed"),
                    "doc_name": a.get("doc_name"),
                    "latency_ms": a.get("latency_ms"),
                    "provenance": t["provenance"],
                    "created_at": t["created_at"],
                }
            )
        return out
