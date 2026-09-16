"""The staged step pipeline (PRD §4.1, §4.3).

    Checkpointer -> ContextAssembler -> Executor -> VerificationGate -> Router
                 -> IdempotentCommitter -> TraceLogger

The gate is **optional, and that is the design**. ``gate=None`` is the Phase 2
baseline: the executor's own choice routes the step, with no rules, no verifier
and no calibration. ``gate=<VerificationGate>`` is the verified configuration.
Both are the same code path, so PRD §5.2's ``--config baseline`` and
``--config verified`` differ by one constructor argument and nothing else — the
delta measured in Phase 4 is the gate's effect and not an incidental difference
between two implementations.

That is also why no verification logic lives in this file. The three gates are
in ``verify/``; this module knows only that something may veto a step.

Enforced from day one:

* Rule 5 — every ERPNext write goes through ``submit_once`` under a deterministic key.
* Rule 6 — state is checkpointed before any side effect, via the provided ``advance``.
* Rule 7 — bounded retries and a per-workflow model-call budget covering *both*
  the executor and the verifier; on exhaustion the workflow escalates to a human.
  It never loops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent.context import ContextAssembler
from agent.executor import Decision, Executor, ExecutorError
from agent.state import SIDE_EFFECTING, Action, Route, Status, StepContext, StepResult
from erp.client import ERPClient, ERPError
from erp.idempotent import submit_once
from orchestrator.budget import LlmBudget, WorkflowBudget
from orchestrator.checkpoint import advance
from orchestrator.db import Store
from orchestrator.steps import writes_for
from trace.store import TraceLogger
from verify.gate import GateOutcome, VerificationGate


@dataclass(frozen=True)
class Policy:
    """Rule 7 — the agent must stop."""

    max_attempts_per_step: int = 3  # one try plus two retries
    max_llm_calls_per_workflow: int = 18

    @classmethod
    def verified(cls) -> Policy:
        """The verified configuration makes two model calls per attempt, not one.

        The cap is per configuration and is reported with the results; what Rule 7
        requires is that a cap exists, binds, and escalates on exhaustion — not
        that both configurations happen to share a number. Leaving the baseline's
        18 in place here would starve the verifier and make the verified config
        look worse for a reason that has nothing to do with verification.
        """
        return cls(max_attempts_per_step=3, max_llm_calls_per_workflow=36)


#: The executor's own choice, mapped to a route. This is the **baseline** router,
#: used only when no gate is configured. With a gate, the conformal router in
#: ``verify/conformal.py`` decides and this table is not consulted.
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
        gate: VerificationGate | None = None,
    ) -> None:
        self.db = db
        self.erp = erp
        self.assembler = assembler
        self.executor = executor
        self.tracer = tracer
        self.gate = gate
        self.policy = policy or (Policy.verified() if gate is not None else Policy())

    @property
    def verified(self) -> bool:
        """True when the three-gate path is active. Recorded with every result so
        a number can never be attributed to the wrong configuration (Rule 4)."""
        return self.gate is not None

    # --- one step ---------------------------------------------------------------
    def run_step(self, ctx: StepContext) -> StepResult:
        started = time.monotonic()
        last_error: str | None = None
        budget = WorkflowBudget(self.db, ctx.workflow_id, self.policy.max_llm_calls_per_workflow)

        for attempt in range(1, self.policy.max_attempts_per_step + 1):
            ctx.attempt = attempt
            attempts_remaining = self.policy.max_attempts_per_step - attempt

            # Rule 6: durable marker before anything can touch the ledger.
            self.db.checkpoint(ctx.workflow_id, ctx.step, Status.IN_PROGRESS.value)

            self.assembler.assemble(ctx)

            # Rule 7: the budget is reserved before the call, never counted after.
            if not budget.reserve("executor"):
                return self._escalate(ctx, "llm_call_budget_exhausted", started, attempt)

            try:
                decision = self.executor.propose(ctx)
            except ExecutorError as e:
                # A malformed answer is a retryable fault; the reason is fed back
                # into the next prompt rather than silently dropped.
                last_error = str(e)
                ctx.rejection_reason = last_error
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
                extra_provenance={
                    "stage": "executor",
                    "rationale": decision.rationale,
                    "action": decision.action.value,
                },
            )

            outcome = self._gate(ctx, budget, attempts_remaining)
            route = outcome.route if outcome else _ROUTE_OF[decision.action]
            reason = (
                outcome.reason
                if outcome and outcome.reason
                else f"executor_chose_{decision.action.value}"
            )

            if route is Route.RETRY:
                # Rule 7: a retry is still bounded by the same attempt loop.
                self._record(
                    ctx, attempt, decision.action.value, trace_id, outcome, decision, False
                )
                last_error = reason
                ctx.rejection_reason = reason
                continue

            if route is Route.ESCALATE:
                self._record(
                    ctx, attempt, decision.action.value, trace_id, outcome, decision, False
                )
                return self._escalate(ctx, reason, started, attempt)

            try:
                names = self._commit(ctx)
            except ERPError as e:
                self._record(
                    ctx, attempt, decision.action.value, trace_id, outcome, decision, False
                )
                last_error = f"erp_write_failed: {e}"
                ctx.rejection_reason = last_error
                continue

            # Recorded exactly once per attempt, with the real outcome — never
            # written first as `committed=False` and overwritten moments later.
            # A concurrent reader of `step_attempts` (the trace explorer, a human
            # watching an escalation queue) must never see a transient false row
            # for a step that is, in fact, about to commit.
            self._record(
                ctx, attempt, decision.action.value, trace_id, outcome, decision, bool(names), names
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

    # --- the verification gate (absent in the baseline configuration) ------------
    def _gate(
        self, ctx: StepContext, budget: LlmBudget, attempts_remaining: int
    ) -> GateOutcome | None:
        if self.gate is None:
            return None
        outcome = self.gate.evaluate(ctx, budget, attempts_remaining)
        ctx.verdict = outcome.verdict
        ctx.confidence_region = outcome.region
        if outcome.verifier_call is not None:
            call = outcome.verifier_call
            # The verifier's own call is traced separately. Two independent
            # decisions, two audit records — a single merged row would make it
            # impossible to show afterwards that the verifier was independent.
            self.tracer.record(
                ctx,
                prompt=call.prompt,
                prompt_hash=call.prompt_hash,
                response=call.response,
                response_hash=call.response_hash,
                model=call.model,
                extra_provenance={
                    "stage": "verifier",
                    "verdict": "pass" if call.verdict.passed else "fail",
                    "violated_expectations": call.verdict.violated_expectations,
                    "confidence": call.verdict.confidence,
                    "checklist": list(call.checklist),
                    "independence": call.independence,
                    "rules": outcome.rule_report.as_dict(),
                },
            )
        return outcome

    def _record(
        self,
        ctx: StepContext,
        attempt: int,
        action: str,
        trace_id: int,
        outcome: GateOutcome | None,
        decision: Decision,
        committed: bool,
        names: list[str] | None = None,
    ) -> None:
        verdict = outcome.verdict if outcome else None
        region = outcome.region if outcome else None
        self.db.record_attempt(
            ctx.workflow_id,
            ctx.step,
            attempt,
            action,
            trace_id,
            committed,
            (names[-1] if names else None),
            decision.latency_ms,
            verdict=(None if verdict is None else ("pass" if verdict.passed else "fail")),
            confidence=(None if verdict is None else verdict.confidence),
            region=(None if region is None else ",".join(sorted(region.labels))),
            signals=(outcome.signals if outcome else None),
            gate=(outcome.as_dict() if outcome else None),
        )

    # --- Rule 5 + Rule 6: the only place this module touches the ledger ----------
    def _commit(self, ctx: StepContext) -> list[str]:
        def side_effect() -> list[str]:
            names: list[str] = []
            for plan in writes_for(self.erp, self.assembler, ctx):
                # Resolve "has this logical write already happened?" BEFORE
                # constructing the document. Building is itself an ERPNext call
                # and, after a partial commit, can fail outright — a Purchase
                # Order that is already fully billed has nothing left to map into
                # an invoice. Resolving first is what lets a resumed step adopt
                # the document it would otherwise fail to rebuild (Rule 6).
                existing = self._resolve_existing(ctx, plan.doctype, plan.key)
                if existing is not None:
                    ctx.docs[plan.doc_slot] = existing
                    names.append(existing)
                    continue

                with self.db.commit_binding(ctx.workflow_id, ctx.step, plan.doctype):
                    name = submit_once(self.erp, plan.doctype, plan.build(), plan.key, self.db)
                ctx.docs[plan.doc_slot] = name
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

    def _resolve_existing(self, ctx: StepContext, doctype: str, key: str) -> str | None:
        """The document this key already produced, from Postgres or the ledger."""
        committed = self.db.get_committed(key)
        if committed is not None:
            return committed
        self._adopt_orphan(ctx, doctype, key)
        return self.db.get_committed(key)

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
