"""The per-workflow model-call budget (Rule 7).

Rule 7 is about stopping. A retry cap bounds how many times one *step* may try;
this bounds how many model calls one *workflow* may ever make, across every step
and every retry, so a workflow cannot burn budget indefinitely by failing
slowly in many places.

The budget is reserved **before** the call, not counted after it. A call that
starts and then fails still consumed capacity, and a counter that only counts
successes is a counter that can be walked past.

Both model call sites go through this object — the executor in
``agent/pipeline.py`` and the independent verifier in ``verify/gate.py``. Adding
the verifier doubled the calls per attempt, which is exactly the kind of change
that silently breaks a cap that each caller enforces for itself.
"""

from __future__ import annotations

from typing import Protocol

from orchestrator.db import Store


class LlmBudget(Protocol):
    """Reserve one model call. False means the budget is spent."""

    def reserve(self, purpose: str) -> bool: ...

    @property
    def spent(self) -> int: ...

    @property
    def cap(self) -> int: ...


class WorkflowBudget:
    """Durable budget: the count lives in Postgres, so it survives a resume.

    A budget held in process memory resets every time a worker restarts, which
    turns "capped" into "capped per process" — no cap at all for a workflow that
    keeps crashing.
    """

    def __init__(self, db: Store, workflow_id: str, cap: int) -> None:
        self.db = db
        self.workflow_id = workflow_id
        self._cap = cap
        self.reservations: list[str] = []

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def spent(self) -> int:
        return self.db.llm_calls(self.workflow_id)

    def reserve(self, purpose: str) -> bool:
        if self.spent >= self._cap:
            return False
        self.db.bump_llm_calls(self.workflow_id)
        self.reservations.append(purpose)
        return True
