"""The trace explorer against a real Postgres (synthetic record, real database).

The scenario from ``tests/trace_scenario.py`` is written through the real
``orchestrator.db.Store`` into a **throwaway schema**, then read back through
the explorer's own read-only connection. The schema is dropped afterwards.

It never writes to the real agent tables: ``scripts/independence_report.py``
reads every row of ``traces``, and synthetic traces there would become Rule 3
evidence. Skipped when no agent Postgres is reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from orchestrator.db import Store, dsn_from_env
from tests.trace_scenario import assert_verified_reconstruction, write_baseline, write_verified
from trace.api import ExplorerSource, create_app, read_only_store

BASE = "http://erp.example"


def _postgres_up() -> bool:
    try:
        psycopg.connect(dsn_from_env(), connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _postgres_up(), reason="no agent Postgres reachable"),
]


@pytest.fixture
def schema() -> Iterator[str]:
    name = f"explorer_test_{uuid.uuid4().hex[:12]}"
    store = Store()
    store.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    store.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
    try:
        store.apply_schema()
        write_verified(store, "v-db")
        write_baseline(store, "b-db")
        store.conn.execute(
            "INSERT INTO labels (workflow_id, fault_class, expected_terminal_action, fault_step)"
            " VALUES ('v-db', 'compounding', 'escalate', 'S5')"
        )
        yield name
    finally:
        store.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        store.close()


def _source(schema: str) -> AbstractContextManager[ExplorerSource]:
    return read_only_store(search_path=schema)


def test_reconstruction_from_the_real_store(schema: str) -> None:
    client = TestClient(create_app(lambda: _source(schema), erp_base=BASE))
    recon = client.get("/api/workflows/v-db").json()
    assert_verified_reconstruction(recon, "v-db", BASE)
    assert recon["label"] == {
        "fault_class": "compounding",
        "expected_terminal_action": "escalate",
        "fault_step": "S5",
    }
    assert recon["workflow"]["created_at"]  # timestamps survive JSON encoding

    listed = client.get("/api/workflows", params={"prefix": "v-"}).json()
    assert [w["workflow_id"] for w in listed] == ["v-db"]
    assert listed[0]["fault_class"] == "compounding"

    baseline = client.get("/api/workflows/b-db").json()
    assert baseline["config"] == "baseline"

    d = client.get("/api/workflows/v-db/diff", params={"step": "S4", "a": 1, "b": 2}).json()
    assert any(line.startswith("+PREVIOUS ATTEMPT REJECTED") for line in d["context_diff"])


def test_prefix_filter_treats_like_wildcards_literally(schema: str) -> None:
    client = TestClient(create_app(lambda: _source(schema), erp_base=BASE))
    assert client.get("/api/workflows", params={"prefix": "%"}).json() == []
    assert client.get("/api/workflows", params={"prefix": "_-db"}).json() == []


def test_the_explorer_session_cannot_write(schema: str) -> None:
    with _source(schema) as src, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        assert isinstance(src, Store)
        src.set_status("v-db", "completed")
