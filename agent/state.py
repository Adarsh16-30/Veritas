"""Workflow and step types (PRD §4.2).

Phase 2 populates everything except ``verdict`` and ``confidence_region`` — those
are filled by the verification gate in Phase 3. The fields exist here because the
schema is PRD-specified; carrying them as ``None`` is not verification logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Step(StrEnum):
    """The six procure-to-pay states (PRD §1, §3.2)."""

    S1 = "S1"  # requisition          -> Material Request
    S2 = "S2"  # policy check         -> no ERPNext write; a go/no-go decision
    S3 = "S3"  # purchase order       -> Purchase Order
    S4 = "S4"  # three-way match      -> Purchase Receipt + Purchase Invoice
    S5 = "S5"  # discrepancy handling -> no ERPNext write; a go/no-go decision
    S6 = "S6"  # payment release      -> Payment Entry

    @property
    def next(self) -> Step | None:
        order = list(Step)
        i = order.index(self)
        return order[i + 1] if i + 1 < len(order) else None


#: Steps that submit a document and therefore move the real GL. Steps not listed
#: here are decision-only gates: the executor still makes a real call, but a
#: ``PROCEED`` simply advances the state machine.
SIDE_EFFECTING: frozenset[Step] = frozenset({Step.S1, Step.S3, Step.S4, Step.S6})


class Action(StrEnum):
    """What the executor may propose. The choice comes from the model (Rule 2);
    what ``PROCEED`` *does* is determined by the step, not by the model."""

    PROCEED = "proceed"
    HOLD = "hold"
    ESCALATE = "escalate"


class Status(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMMITTED = "committed"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class Route(StrEnum):
    """Where a step goes after the gate. Phase 2 has no gate, so the router is
    trivial; Phase 3 replaces the trivial mapping with the conformal router."""

    COMMIT = "commit"
    RETRY = "retry"
    ESCALATE = "escalate"


@dataclass
class Verdict:
    """Populated by the independent verifier in Phase 3 (Rule 3)."""

    passed: bool
    violated_expectations: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass
class Region:
    """Conformal prediction region (PRD §9.3).

    ``calibrated`` is load-bearing, not decoration. A region produced before a
    calibration model has been fitted carries no coverage guarantee, and
    ``verify.conformal`` refuses to compute coverage or ECE from one. Without
    this flag an uncalibrated region and a calibrated one are indistinguishable
    downstream, which is precisely how an unearned confidence number gets
    reported as a measured one.
    """

    labels: frozenset[str] = frozenset()
    p_commit: float | None = None
    calibrated: bool = False
    alpha: float | None = None

    def is_singleton(self) -> bool:
        return len(self.labels) == 1

    def covers(self, label: str) -> bool:
        return label in self.labels

    def as_dict(self) -> dict[str, object]:
        return {
            "labels": sorted(self.labels),
            "p_commit": self.p_commit,
            "calibrated": self.calibrated,
            "alpha": self.alpha,
        }


@dataclass
class StepContext:
    """PRD §4.2. Carries one step of one workflow through the pipeline."""

    workflow_id: str
    step: Step
    step_context: str = ""
    amount_at_stake: Decimal = Decimal("0")
    proposed_action: Action | None = None
    rationale: str | None = None
    verdict: Verdict | None = None
    confidence_region: Region | None = None
    attempt: int = 1
    idempotency_key: str = ""

    # Phase 2 working state — not part of the PRD schema, but needed to build the
    # ERPNext write and to trace the decision.
    facts: dict[str, object] = field(default_factory=dict)
    payload: dict[str, object] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)
    prompt: str | None = None
    raw_response: str | None = None
    rejection_reason: str | None = None


@dataclass
class StepResult:
    step: Step
    route: Route
    action: Action | None
    committed: bool
    doc_name: str | None = None
    reason: str | None = None
    latency_ms: int = 0
    attempts: int = 1
