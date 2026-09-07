"""The staged step pipeline (PRD §4.1, §4.3).

Phase 2 wiring — deliberately missing its middle:

    Checkpointer -> ContextAssembler -> Executor -> [ gate goes here in Phase 3 ]
                 -> Router -> IdempotentCommitter -> TraceLogger

There is no RuleGate, no independent verifier and no conformal calibrator in this
file. That is not an oversight: Phase 2 is the honest denominator every later
claim is measured against, so it must not be quietly strengthened. Phase 3
inserts those three stages between ``Executor`` and ``Router`` and replaces
``_route`` — nothing else here should need to change.

What *is* enforced from day one:

* Rule 5 — every ERPNext write goes through ``submit_once`` under a deterministic key.
* Rule 6 — state is checkpointed before any side effect, via the provided ``advance``.
* Rule 7 — bounded retries and a per-workflow model-call budget; on exhaustion the
  workflow escalates to a human. It never loops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent.context import ContextAssembler
from agent.executor import Executor, ExecutorError
from agent.state import SIDE_EFFECTING, Action, Route, Status, StepContext, StepResult
from erp.client import IDEMPOTENCY_FIELD, ERPClient, ERPError
from erp.idempotent import submit_once
from orchestrator.checkpoint import advance
from orchestrator.db import Store
from orchestrator.steps import writes_for
from trace.store import TraceLogger


@dataclass(frozen=True)
class Policy:
    """Rule 7 — the agent must stop."""

    max_attempts_per_step: int = 3  # one try plus two retries
    max_llm_calls_per_workflow: int = 18


#: The executor's own choice, mapped to a route. Phase 3 replaces this with the
#: conformal router; until then a `hold` or an `escalate` both stop the workflow,
#: which is the cautious direction.
_ROUTE_OF: dict[Action, Route] = {
    Action.PROCEED: Route.COMMIT,
    Action.HOLD: Route.ESCALATE,
    Action.ESCALATE: Route.ESCALATE,
}


class Pipeline:
    def __init__(
        self,
        db: Store,
        erp: ERPClient,
        assembler: ContextAssembler,
        executor: Executor,
        tracer: TraceLogger,
        policy: Policy | None = None,
    ) -> None:
        self.db = db
        self.erp = erp
        self.assembler = assembler
        self.executor = executor
        self.tracer = tracer
        self.policy = policy or Policy()

    # --- one step ---------------------------------------------------------------
    def run_step(self, ctx: StepContext) -> StepResult:
        started = time.monotonic()
        last_error: str | None = None

        for attempt in range(1, self.policy.max_attempts_per_step + 1):
            ctx.attempt = attempt

            # Rule 6: durable marker before anything can touch the ledger.
            self.db.checkpoint(ctx.workflow_id, ctx.step, Status.IN_PROGRESS.value)

            self.assembler.assemble(ctx)

            # Rule 7: the model-call budget is checked before the call, not after.
            if self.db.llm_calls(ctx.workflow_id) >= self.policy.max_llm_calls_per_workflow:
                return self._escalate(ctx, "llm_call_budget_exhausted", started, attempt)

            try:
                decision = self.executor.propose(ctx)
                self.db.bump_llm_calls(ctx.workflow_id)
            except ExecutorError as e:
                # A malformed answer is a retryable fault; the reason is fed back
                # into the next prompt rather than silently dropped.
                last_error = str(e)
                ctx.rejection_reason = last_error
                self.db.bump_llm_calls(ctx.workflow_id)
                continue

            ctx.proposed_action = decision.action
            ctx.rationale = decision.rationale
            ctx.prompt = decision.prompt
            ctx.raw_response = decision.response

            trace_id = self.tracer.record(
                ctx,
                prompt=decision.prompt,
                prompt_hash=decision.prompt_hash,
                response=decision.response,
                response_hash=decision.response_hash,
                model=decision.model,
                extra_provenance={"rationale": decision.rationale, "action": decision.action.value},
            )

            route = _ROUTE_OF[decision.action]
            if route is Route.ESCALATE:
                self.db.record_attempt(
                    ctx.workflow_id,
                    ctx.step,
                    attempt,
                    decision.action.value,
                    trace_id,
                    False,
                    None,
                    decision.latency_ms,
                )
                return self._escalate(
                    ctx, f"executor_chose_{decision.action.value}", started, attempt
                )

            try:
                names = self._commit(ctx)
            except ERPError as e:
                last_error = f"erp_write_failed: {e}"
                ctx.rejection_reason = last_error
                continue

            self.db.record_attempt(
                ctx.workflow_id,
                ctx.step,
                attempt,
                decision.action.value,
                trace_id,
                bool(names),
                names[-1] if names else None,
                decision.latency_ms,
            )
            return StepResult(
                step=ctx.step,
                route=Route.COMMIT,
                action=decision.action,
                committed=bool(names),
                doc_name=names[-1] if names else None,
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempt,
            )

        # Rule 7: retries exhausted -> a human, never another loop.
        return self._escalate(
            ctx,
            f"retry_cap_exhausted: {last_error}",
            started,
            self.policy.max_attempts_per_step,
        )

    # --- Rule 5 + Rule 6: the only place this module touches the ledger ----------
    def _commit(self, ctx: StepContext) -> list[str]:
        def side_effect() -> list[str]:
            names: list[str] = []
            for write in writes_for(self.erp, self.assembler, ctx):
                key = str(write.doc[IDEMPOTENCY_FIELD])
                self._adopt_orphan(ctx, write.doctype, key)
                with self.db.commit_binding(ctx.workflow_id, ctx.step, write.doctype):
                    name = submit_once(self.erp, write.doctype, write.doc, key, self.db)
                ctx.docs[write.doc_slot] = name
                names.append(name)
            return names

        if ctx.step in SIDE_EFFECTING:
            # `advance` is PRD §9.2 provided verbatim and therefore unannotated.
            # It is not modified to satisfy a type checker; the ignore is here,
            # at the call site, where it is visible.
            names: list[str] = advance(ctx, self.db, side_effect)  # type: ignore[no-untyped-call]
            return names
        # A judgement gate writes nothing, but its outcome is still checkpointed.
        self.db.checkpoint(ctx.workflow_id, ctx.step, Status.COMMITTED.value)
        return []

    def _adopt_orphan(self, ctx: StepContext, doctype: str, key: str) -> None:
        """Close the window ``submit_once`` cannot see.

        ``submit_once`` asks Postgres whether this key already committed. If a
        worker died *after* ERPNext accepted the submit but *before* Postgres
        recorded it, Postgres says no and a naive retry would post a second
        document. The key is stamped into the document itself, so the ledger can
        be asked directly. Found here, the existing document is adopted rather
        than duplicated (Rules 5–6).
        """
        if self.db.get_committed(key) is not None:
            return
        orphan = self.erp.find_by_idempotency_key(doctype, key)
        if orphan:
            with self.db.commit_binding(ctx.workflow_id, ctx.step, doctype):
                self.db.record_committed(key, orphan)

    def _escalate(self, ctx: StepContext, reason: str, started: float, attempts: int) -> StepResult:
        self.db.checkpoint(ctx.workflow_id, ctx.step, Status.ESCALATED.value)
        self.db.set_status(ctx.workflow_id, Status.ESCALATED.value, reason)
        return StepResult(
            step=ctx.step,
            route=Route.ESCALATE,
            action=ctx.proposed_action,
            committed=False,
            reason=reason,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
        )
