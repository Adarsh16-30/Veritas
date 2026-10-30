"""The service's view of the agent Postgres, over a small connection pool.

The API is multi-threaded, and ``orchestrator.db.Store`` owns a single
connection that is not thread-safe. So the API reads and writes through this
pool instead (size ``POSTGRES_POOL_MAX``, 4 by default: small on purpose). The
worker keeps one ``Store`` per thread, as the benchmark does.

Every state change that must be atomic runs in one transaction, with the
workflow row locked ``FOR UPDATE``. Two approvers racing on one escalation
serialise, and the second one sees it already decided.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from agent.state import Status
from service.auth import new_key
from verify.gate import human_overridable

#: Statuses a worker may (re)enter. ``committed`` is the checkpoint status a
#: workflow carries between steps; the rest are terminal or wait on a human.
RUNNABLE = (Status.PENDING.value, Status.IN_PROGRESS.value, Status.COMMITTED.value)


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class Forbidden(PermissionError):
    pass


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def approvable(reason: str | None) -> tuple[bool, str]:
    """Whether a human may approve this escalation, and why not if not."""
    text = reason or ""
    if text.startswith("infrastructure_unavailable"):
        return True, "resumes a workflow parked by an outage"
    if human_overridable(text):
        return True, "a human may overrule this objection"
    if text.startswith("hard_rule_violation"):
        return False, "a hard accounting invariant failed; no approval can make it pass"
    if text.startswith(("floor:", "executor_chose_")):
        return False, "the agent itself declined to proceed (Rule 2); reject and resubmit"
    if text.startswith("llm_call_budget_exhausted"):
        return False, "the model-call budget is spent (Rule 7); reject and resubmit"
    return False, "this escalation is not one a human approval can lift; reject and resubmit"


class ServiceStore:
    def __init__(self, dsn: str, max_size: int = 4, search_path: str | None = None) -> None:
        kwargs: dict[str, Any] = {"row_factory": dict_row, "autocommit": True}
        if search_path:
            kwargs["options"] = f"-c search_path={search_path}"
        self.pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            dsn,
            connection_class=Connection[DictRow],
            min_size=1,
            max_size=max_size,
            kwargs=kwargs,
            open=False,
            name="veritas-api",
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> bool:
        try:
            with self.pool.connection(timeout=5) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - readiness reports, never raises
            return False

    # --- clients -----------------------------------------------------------------
    def create_client(self, client_id: str, name: str, roles: list[str]) -> str:
        token, salt, digest = new_key(client_id)
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO api_clients (client_id, name, key_salt, key_hash, roles)"
                " VALUES (%s, %s, %s, %s, %s)",
                (client_id, name, salt, digest, roles),
            )
        return token

    def client(self, client_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            row: dict[str, Any] | None = conn.execute(
                "SELECT client_id, key_salt, key_hash, roles, disabled FROM api_clients"
                " WHERE client_id = %s",
                (client_id,),
            ).fetchone()
            return row

    # --- requests ------------------------------------------------------------------
    def submit(
        self, client_id: str, idempotency_key: str, body: dict[str, Any]
    ) -> tuple[str, bool]:
        """``(workflow_id, created)``. A replay returns the original workflow."""
        digest = body_hash(body)
        workflow_id = f"req-{uuid.uuid4().hex[:20]}"
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO workflow_requests
                       (workflow_id, client_id, idempotency_key, body_sha256, request)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (client_id, idempotency_key) DO NOTHING
                RETURNING workflow_id
                """,
                (workflow_id, client_id, idempotency_key, digest, json.dumps(body, default=str)),
            ).fetchone()
            if row is None:
                prior = conn.execute(
                    "SELECT workflow_id, body_sha256 FROM workflow_requests"
                    " WHERE client_id = %s AND idempotency_key = %s",
                    (client_id, idempotency_key),
                ).fetchone()
                if prior is None or prior["body_sha256"] != digest:
                    raise Conflict("this Idempotency-Key was already used for a different request")
                return str(prior["workflow_id"]), False
            conn.execute(
                "INSERT INTO workflows (workflow_id, current_step, status)"
                " VALUES (%s, 'S1', %s) ON CONFLICT (workflow_id) DO NOTHING",
                (workflow_id, Status.PENDING.value),
            )
        return workflow_id, True

    def request(self, workflow_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            row: dict[str, Any] | None = conn.execute(
                "SELECT workflow_id, client_id, request, created_at FROM workflow_requests"
                " WHERE workflow_id = %s",
                (workflow_id,),
            ).fetchone()
            return row

    def view(self, workflow_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            wf = conn.execute(
                """
                SELECT r.workflow_id, r.client_id AS submitted_by, r.created_at,
                       w.status, w.current_step, w.escalation_reason, w.last_checkpoint_ts
                  FROM workflow_requests r
                  LEFT JOIN workflows w USING (workflow_id)
                 WHERE r.workflow_id = %s
                """,
                (workflow_id,),
            ).fetchone()
            if wf is None:
                return None
            wf["documents"] = conn.execute(
                "SELECT step, doctype, doc_name FROM commits WHERE workflow_id = %s"
                " ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            wf["decisions"] = conn.execute(
                "SELECT id, step, decision, decided_by, reason, created_at FROM human_decisions"
                " WHERE workflow_id = %s ORDER BY id",
                (workflow_id,),
            ).fetchall()
            return dict(wf)

    def escalations(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT w.workflow_id, w.current_step AS step, w.escalation_reason,
                       w.last_checkpoint_ts AS escalated_at, r.client_id AS submitted_by
                  FROM workflows w
                  JOIN workflow_requests r USING (workflow_id)
                 WHERE w.status = %s
                 ORDER BY w.last_checkpoint_ts
                 LIMIT %s OFFSET %s
                """,
                (Status.ESCALATED.value, limit, offset),
            ).fetchall()
        for r in rows:
            r["approvable"], r["approvable_why"] = approvable(r["escalation_reason"])
        return list(rows)

    # --- human decisions ------------------------------------------------------------
    def decide(
        self, workflow_id: str, client_id: str, decision: str, reason: str
    ) -> dict[str, Any]:
        """Record a decision on an escalated workflow, atomically.

        Raises ``NotFound``, ``Forbidden`` (separation of duties: the submitter
        may not decide their own request) or ``Conflict`` (not escalated, or an
        escalation that cannot be approved).
        """
        with self.pool.connection() as conn, conn.transaction():
            wf = conn.execute(
                """
                SELECT w.status, w.current_step, w.escalation_reason, r.client_id AS submitter
                  FROM workflows w JOIN workflow_requests r USING (workflow_id)
                 WHERE w.workflow_id = %s
                   FOR UPDATE OF w
                """,
                (workflow_id,),
            ).fetchone()
            if wf is None:
                raise NotFound(workflow_id)
            if wf["submitter"] == client_id:
                raise Forbidden("separation of duties: the submitter may not decide this request")
            if wf["status"] != Status.ESCALATED.value:
                raise Conflict(f"workflow is {wf['status']}, not escalated")
            if decision == "approve_and_resume":
                ok, why = approvable(wf["escalation_reason"])
                if not ok:
                    raise Conflict(f"cannot approve: {why}")
            row = conn.execute(
                """
                INSERT INTO human_decisions
                       (workflow_id, step, decision, decided_by, reason, escalation_reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    workflow_id,
                    wf["current_step"],
                    decision,
                    client_id,
                    reason,
                    wf["escalation_reason"] or "",
                ),
            ).fetchone()
            assert row is not None
            new_status = Status.REJECTED.value if decision == "reject" else Status.IN_PROGRESS.value
            conn.execute(
                "UPDATE workflows SET status = %s WHERE workflow_id = %s",
                (new_status, workflow_id),
            )
        return {"decision_id": row["id"], "step": wf["current_step"], "status": new_status}

    def pending(self, limit: int = 1000) -> list[str]:
        """Requests a worker should be running: queued, or interrupted mid-run."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT w.workflow_id FROM workflows w JOIN workflow_requests r USING (workflow_id)"
                " WHERE w.status = ANY(%s) ORDER BY w.created_at LIMIT %s",
                (list(RUNNABLE), limit),
            ).fetchall()
        return [str(r["workflow_id"]) for r in rows]


def unconsumed_approval(conn: Any, workflow_id: str, step: str) -> int | None:
    """The latest decision on this step, if it is an approval no commit has used yet.

    Runs on the worker's own connection (``Store.conn``). An approval is spent by
    the first *committed* attempt that cites it; a write that failed leaves it
    usable for the retry.
    """
    row = conn.execute(
        """
        SELECT d.id, d.decision
          FROM human_decisions d
         WHERE d.workflow_id = %s AND d.step = %s
         ORDER BY d.id DESC
         LIMIT 1
        """,
        (workflow_id, step),
    ).fetchone()
    if row is None or row["decision"] != "approve_and_resume":
        return None
    spent = conn.execute(
        """
        SELECT 1 FROM step_attempts
         WHERE workflow_id = %s AND step = %s AND committed
           AND gate ->> 'override_decision' = %s
         LIMIT 1
        """,
        (workflow_id, step, str(row["id"])),
    ).fetchone()
    return None if spent else int(row["id"])
