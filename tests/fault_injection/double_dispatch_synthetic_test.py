"""Rules 5–6 — double-dispatch a side effect; checkpoint precedes the effect.

Unit-level stand-ins for the Phase 4 harness that double-dispatches every
side-effecting step 100x against the real ledger and kills workers mid-S4. Here
the "ledger" is a counting fake; Phase 4 swaps in the real ERPNext GL.
"""

from __future__ import annotations

from erp.idempotent import idempotency_key, submit_once
from orchestrator.checkpoint import advance
from tests.conftest import CountingERP, FakeCommitDB


def test_hundred_dispatches_post_once() -> None:
    db, erp = FakeCommitDB(), CountingERP()
    key = idempotency_key("wf-crash", "S4", {"invoice": "PI-9"})
    names = {submit_once(erp, "Purchase Invoice", {"n": 9}, key, db) for _ in range(100)}
    assert erp.calls == 1
    assert names == {"Purchase Invoice-0001"}


def test_crash_resume_replays_committed_step_without_reposting() -> None:
    """Simulates a worker that dies after committing, then restarts and re-enters
    the same step: the second pass must replay, not re-post."""
    db, erp = FakeCommitDB(), CountingERP()
    key = idempotency_key("wf-resume", "S3", {"po": 1})

    submit_once(erp, "Purchase Order", {"po": 1}, key, db)  # pre-crash commit
    # ---- worker killed here, restarts, re-enters S3 ----
    replayed = submit_once(erp, "Purchase Order", {"po": 1}, key, db)

    assert erp.calls == 1
    assert replayed == "Purchase Order-0001"


def test_advance_checkpoints_before_side_effect() -> None:
    events: list[str] = []

    class Ctx:
        workflow_id = "wf1"
        step = "S4"

    class DB:
        def checkpoint(self, wf: str, step: str, status: str) -> None:
            events.append(f"checkpoint:{status}")

    def side_effect() -> str:
        events.append("side_effect")
        return "ok"

    result = advance(Ctx(), DB(), side_effect)

    assert result == "ok"
    assert events == ["checkpoint:in_progress", "side_effect", "checkpoint:committed"]
