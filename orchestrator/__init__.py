"""Layer 2 — orchestration (PRD §3.1).

Durable S1..S6 state machine. Workflow state is checkpointed to Postgres
**before** any ERPNext side effect (Rule 6); killing a worker mid-step and
restarting resumes from the checkpoint without re-executing committed steps.
Redis provides the work queue and a per-workflow advisory lock. The agent must
stop: hard retry cap per step, then escalate — never loop (Rule 7).

State machine, queue workers, and the retry/stop policy are built in Phase 2
(state machine + checkpointing) and Phase 3 (retry/stop policy). The
checkpoint-before-side-effect primitive lives in ``orchestrator.checkpoint``.
"""
