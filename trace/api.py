"""Trace explorer API (PRD §7 Phase 5): FastAPI over the append-only trace store.

    uv run python scripts/trace_explorer.py      # http://127.0.0.1:8765

Read-only by construction, not by convention. Each request opens its own
Postgres session and sets it ``READ ONLY`` before the first query, so a bug
here cannot write to agent state even by accident; ``traces`` additionally
refuses UPDATE and DELETE at the database level (``orchestrator/schema.sql``).

The SPA in ``ui/`` is served from ``/`` by the same process, so the explorer is
one command and one port.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from trace.explorer import TraceSource, diff_attempts, reconstruct

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class ExplorerSource(TraceSource, Protocol):
    def list_workflows(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]: ...


SourceFactory = Callable[[], AbstractContextManager[ExplorerSource]]


@contextmanager
def read_only_store(
    dsn: str | None = None, search_path: str | None = None
) -> Iterator[ExplorerSource]:
    """A fresh read-only connection to the agent Postgres, closed after use.

    ``Store`` owns one connection and is not thread-safe, and FastAPI runs sync
    endpoints on a thread pool, so connections are never shared between requests.
    ``search_path`` exists for the database test, which works in a throwaway
    schema rather than the real agent tables.
    """
    from psycopg import sql

    from orchestrator.db import Store

    store = Store(dsn)
    try:
        if search_path:
            store.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(search_path)))
        store.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        yield store
    finally:
        store.close()


def create_app(
    open_source: SourceFactory = read_only_store, erp_base: str | None = None
) -> FastAPI:
    app = FastAPI(
        title="VERITAS trace explorer",
        description="Read-only reconstruction of recorded workflows (PRD Phase 5).",
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/workflows")
    def workflows(
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        status: str | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        with open_source() as src:
            rows = src.list_workflows(limit=limit, offset=offset, status=status, prefix=prefix)
        return [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
            for r in rows
        ]

    @app.get("/api/workflows/{workflow_id}")
    def workflow(workflow_id: str) -> dict[str, Any]:
        with open_source() as src:
            recon = reconstruct(src, workflow_id, base=erp_base)
        if recon is None:
            raise HTTPException(status_code=404, detail=f"no workflow {workflow_id!r}")
        return recon

    @app.get("/api/workflows/{workflow_id}/diff")
    def diff(
        workflow_id: str,
        step: str,
        a: int = Query(..., ge=1),
        b: int = Query(..., ge=1),
    ) -> dict[str, Any]:
        with open_source() as src:
            recon = reconstruct(src, workflow_id, base=erp_base)
        if recon is None:
            raise HTTPException(status_code=404, detail=f"no workflow {workflow_id!r}")
        out = diff_attempts(recon, step, a, b)
        if out is None:
            raise HTTPException(
                status_code=404,
                detail=f"{workflow_id} has no traced attempts {a} and {b} at {step}",
            )
        return out

    if UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="ui")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(UI_DIR / "index.html")

    return app
