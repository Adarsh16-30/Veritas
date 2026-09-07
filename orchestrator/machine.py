"""The durable S1..S6 state machine (PRD §3.1 Layer 2, §7 Phase 2).

Resume is the whole point of this module. A workflow is a sequence of states, each
of which is checkpointed before it can touch the ledger. Restarting a killed
worker must continue from the checkpoint and must not re-execute a state that
already committed (Rule 6), and any state it *does* re-enter must be safe to
re-enter (Rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.context import ContextAssembler, WorkflowSpec
from agent.executor import Executor
from agent.pipeline import Pipeline, Policy
from agent.state import Route, Status, Step, StepContext, StepResult
from erp.client import ERPClient
from orchestrator.db import Store
from trace.store import TraceLogger

#: Rebuilding ``ctx.docs`` after a crash: which slot a committed document fills.
DOC_SLOT_OF: dict[str, str] = {
    "Material Request": "S1",
    "Purchase Order": "S3",
    "Purchase Receipt": "S4_receipt",
    "Purchase Invoice": "S4_invoice",
    "Payment Entry": "S6",
}


@dataclass
class Outcome:
    workflow_id: str
    status: Status
    steps: list[StepResult] = field(default_factory=list)
    docs: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    resumed_from: Step | None = None
    skipped: list[Step] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.status is Status.COMPLETED

    @property
    def total_latency_ms(self) -> int:
        return sum(s.latency_ms for s in self.steps)


class WorkflowMachine:
    def __init__(
        self,
        db: Store,
        erp: ERPClient,
        spec: WorkflowSpec,
        executor: Executor,
        policy: Policy | None = None,
    ) -> None:
        self.db = db
        self.erp = erp
        self.spec = spec
        self.assembler = ContextAssembler(erp, spec)
        self.pipeline = Pipeline(
            db=db,
            erp=erp,
            assembler=self.assembler,
            executor=executor,
            tracer=TraceLogger(db),
            policy=policy,
        )

    # --- resume support ---------------------------------------------------------
    def restore_docs(self, workflow_id: str) -> dict[str, str]:
        """Rebuild the document map from what is durably committed."""
        docs: dict[str, str] = {}
        for row in self.db.commits_for(workflow_id):
            slot = DOC_SLOT_OF.get(row["doctype"])
            if slot:
                docs[slot] = row["doc_name"]
        return docs

    def already_committed(self, workflow_id: str, step: Step) -> bool:
        return self.db.checkpoint_status(workflow_id, step) == Status.COMMITTED.value

    # --- run / resume -------------------------------------------------------------
    def run(self, workflow_id: str) -> Outcome:
        self.db.create_workflow(workflow_id, Step.S1)
        docs = self.restore_docs(workflow_id)
        outcome = Outcome(workflow_id=workflow_id, status=Status.IN_PROGRESS, docs=docs)

        for step in Step:
            if self.already_committed(workflow_id, step):
                # Rule 6: a committed step is never re-executed on resume.
                outcome.skipped.append(step)
                continue
            if outcome.resumed_from is None and outcome.skipped:
                outcome.resumed_from = step

            ctx = StepContext(workflow_id=workflow_id, step=step, docs=docs)
            result = self.pipeline.run_step(ctx)
            docs = ctx.docs
            outcome.docs = docs
            outcome.steps.append(result)

            if result.route is Route.ESCALATE:
                outcome.status = Status.ESCALATED
                outcome.reason = result.reason
                return outcome

        # Workflow completion is a property of the workflow, not of S6. Stamping
        # COMPLETED onto S6's checkpoint would overwrite its `committed` marker and
        # make a finished workflow re-enter its payment step on the next resume.
        self.db.set_status(workflow_id, Status.COMPLETED.value)
        outcome.status = Status.COMPLETED
        return outcome

    # --- ledger read-back ---------------------------------------------------------
    def gl_effect(self, docs: dict[str, str]) -> list[dict[str, object]]:
        """What this workflow actually did to the real general ledger."""
        vouchers = [docs[k] for k in ("S4_invoice", "S6") if k in docs]
        return [self.erp.gl_effect(v) for v in vouchers]
