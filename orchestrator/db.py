"""Durable agent state in Postgres (PRD §3.3).

``Store`` deliberately satisfies both provided-verbatim contracts at once:

* ``get_committed`` / ``record_committed`` — required by ``erp.idempotent.submit_once`` (Rule 5)
* ``checkpoint``                          — required by ``orchestrator.checkpoint.advance`` (Rule 6)

so the PRD §9 primitives can be used exactly as written, with no shim around them.

A ``Store`` owns one connection and is **not** thread-safe; give each worker its own.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def dsn_from_env() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5433')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'veritas')} "
        f"user={os.environ.get('POSTGRES_USER', 'veritas')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'veritas')}"
    )


class Store:
    def __init__(self, dsn: str | None = None) -> None:
        self.conn = psycopg.connect(dsn or dsn_from_env(), autocommit=True, row_factory=dict_row)
        # Bound just before a guarded write so record_committed(key, name) — whose
        # signature is fixed by PRD §9.1 — can still record full provenance.
        self._binding: tuple[str, str, str] | None = None

    # --- lifecycle -----------------------------------------------------------
    def apply_schema(self) -> None:
        self.conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Rule 6: checkpoint before side effect --------------------------------
    def checkpoint(self, workflow_id: str, step: Any, status: str) -> None:
        self.conn.execute(
            """
            INSERT INTO checkpoints (workflow_id, step, status, ts)
                 VALUES (%s, %s, %s, now())
            ON CONFLICT (workflow_id, step)
              DO UPDATE SET status = EXCLUDED.status, ts = now()
            """,
            (workflow_id, str(getattr(step, "value", step)), status),
        )
        self.conn.execute(
            """
            UPDATE workflows
               SET current_step = %s, status = %s, last_checkpoint_ts = now()
             WHERE workflow_id = %s
            """,
            (str(getattr(step, "value", step)), status, workflow_id),
        )

    def checkpoint_status(self, workflow_id: str, step: Any) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM checkpoints WHERE workflow_id = %s AND step = %s",
            (workflow_id, str(getattr(step, "value", step))),
        ).fetchone()
        return row["status"] if row else None

    def steps_in_progress(self, workflow_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT step FROM checkpoints WHERE workflow_id = %s AND status = 'in_progress'"
            " ORDER BY step",
            (workflow_id,),
        ).fetchall()
        return [r["step"] for r in rows]

    # --- Rule 5: idempotency-key store ----------------------------------------
    @contextmanager
    def commit_binding(self, workflow_id: str, step: Any, doctype: str) -> Iterator[None]:
        """Provenance for the next ``record_committed`` call."""
        prev = self._binding
        self._binding = (workflow_id, str(getattr(step, "value", step)), doctype)
        try:
            yield
        finally:
            self._binding = prev

    def get_committed(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT doc_name FROM commits WHERE idempotency_key = %s", (key,)
        ).fetchone()
        return row["doc_name"] if row else None

    def record_committed(self, key: str, name: str) -> None:
        workflow_id, step, doctype = self._binding or ("", "", "")
        # ON CONFLICT DO NOTHING: if two dispatches race past get_committed, the
        # primary key still admits exactly one row. The loser sees the winner's
        # document name on its next read.
        self.conn.execute(
            """
            INSERT INTO commits (idempotency_key, workflow_id, step, doctype, doc_name)
                 VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (key, workflow_id, step, doctype, name),
        )

    def get_committed_by_doc(self, doc_name: str) -> str | None:
        """Reverse lookup: which key committed this document, if any."""
        row = self.conn.execute(
            "SELECT idempotency_key FROM commits WHERE doc_name = %s", (doc_name,)
        ).fetchone()
        return row["idempotency_key"] if row else None

    def commits_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT step, doctype, doc_name, idempotency_key FROM commits"
            " WHERE workflow_id = %s ORDER BY created_at",
            (workflow_id,),
        ).fetchall()

    # --- workflows -------------------------------------------------------------
    def create_workflow(self, workflow_id: str, step: Any, label: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO workflows (workflow_id, current_step, status, ground_truth_label)
                 VALUES (%s, %s, 'pending', %s)
            ON CONFLICT (workflow_id) DO NOTHING
            """,
            (workflow_id, str(getattr(step, "value", step)), label),
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()

    def set_status(self, workflow_id: str, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE workflows SET status = %s, escalation_reason = %s WHERE workflow_id = %s",
            (status, reason, workflow_id),
        )

    # --- Rule 7: per-workflow LLM-call budget -----------------------------------
    def bump_llm_calls(self, workflow_id: str) -> int:
        row = self.conn.execute(
            "UPDATE workflows SET llm_calls = llm_calls + 1 WHERE workflow_id = %s"
            " RETURNING llm_calls",
            (workflow_id,),
        ).fetchone()
        return int(row["llm_calls"]) if row else 0

    def llm_calls(self, workflow_id: str) -> int:
        row = self.conn.execute(
            "SELECT llm_calls FROM workflows WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()
        return int(row["llm_calls"]) if row else 0

    # --- traces (append-only) ----------------------------------------------------
    def append_trace(
        self,
        workflow_id: str,
        step: Any,
        attempt: int,
        step_context: str,
        prompt: str | None,
        prompt_hash: str,
        response: str | None,
        response_hash: str,
        model: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO traces (workflow_id, step, attempt, step_context, prompt, prompt_hash,
                                response, response_hash, model, provenance)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
              RETURNING id
            """,
            (
                workflow_id,
                str(getattr(step, "value", step)),
                attempt,
                step_context,
                prompt,
                prompt_hash,
                response,
                response_hash,
                model,
                Json(provenance or {}),
            ),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def traces_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM traces WHERE workflow_id = %s ORDER BY id", (workflow_id,)
        ).fetchall()

    # --- step attempts -------------------------------------------------------------
    def record_attempt(
        self,
        workflow_id: str,
        step: Any,
        attempt: int,
        action: str | None,
        rationale_ref: int | None,
        committed: bool,
        doc_name: str | None,
        latency_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO step_attempts (workflow_id, step, attempt, action, rationale_ref,
                                       committed, doc_name, latency_ms)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workflow_id, step, attempt)
              DO UPDATE SET action = EXCLUDED.action, rationale_ref = EXCLUDED.rationale_ref,
                            committed = EXCLUDED.committed, doc_name = EXCLUDED.doc_name,
                            latency_ms = EXCLUDED.latency_ms
            """,
            (
                workflow_id,
                str(getattr(step, "value", step)),
                attempt,
                action,
                rationale_ref,
                committed,
                doc_name,
                latency_ms,
            ),
        )

    def attempts_for(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM step_attempts WHERE workflow_id = %s ORDER BY step, attempt",
            (workflow_id,),
        ).fetchall()
