"""The service API, migrations and decision rules on a real Postgres and Redis.

Synthetic workflows (no ERPNext, no models), real storage. Each test runs in a
**throwaway Postgres schema** created and dropped here, never in the real
agent tables. A throwaway ``redis-server`` is started on a free port. Skipped
when either is unavailable.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from orchestrator.db import Store, dsn_from_env
from orchestrator.migrate import MigrationError
from orchestrator.queue import WorkQueue
from orchestrator.settings import load_settings
from service.api import create_app
from service.store import ServiceStore, unconsumed_approval


def _postgres_up() -> bool:
    try:
        psycopg.connect(dsn_from_env(), connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.skipif(not _postgres_up(), reason="no agent Postgres reachable"),
    pytest.mark.skipif(shutil.which("redis-server") is None, reason="no redis-server"),
]

ORDER = {
    "item_code": "USA-272060915",
    "qty": 10,
    "supplier": "Acme Supplies",
    "rate": 125.5,
    "needed_by": "2026-12-01",
    "bill_no": "INV-2026-0001",
}


@pytest.fixture
def schema() -> Iterator[Store]:
    name = f"service_test_{uuid.uuid4().hex[:12]}"
    store = Store()
    store.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    store.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
    store.search_path = name  # type: ignore[attr-defined]
    try:
        store.apply_schema()
        yield store
    finally:
        store.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        store.close()


@pytest.fixture
def queue() -> Iterator[WorkQueue]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    q = WorkQueue(f"redis://127.0.0.1:{port}/0")
    for _ in range(50):
        if q.ping():
            break
        time.sleep(0.1)
    try:
        yield q
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@dataclass
class Env:
    client: TestClient
    store: ServiceStore
    db: Store
    queue: WorkQueue
    keys: dict[str, str]

    def as_(self, who: str, **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[who]}", **headers}

    def submit(self, who: str = "intake", key: str = "order-0001", body: Any = None) -> Any:
        return self.client.post(
            "/v1/workflows",
            json=ORDER if body is None else body,
            headers=self.as_(who, **{"Idempotency-Key": key}),
        )

    def escalate(self, workflow_id: str, reason: str, step: str = "S5") -> None:
        self.db.conn.execute(
            "UPDATE workflows SET status = 'escalated', current_step = %s,"
            " escalation_reason = %s WHERE workflow_id = %s",
            (step, reason, workflow_id),
        )


def _env(schema: Store, queue: WorkQueue, **overrides: Any) -> Iterator[Env]:
    settings = load_settings(**overrides)
    store = ServiceStore(dsn_from_env(), max_size=2, search_path=schema.search_path)  # type: ignore[attr-defined]
    store.open()
    keys = {
        "intake": store.create_client("intake", "Intake", ["submitter"]),
        "intake2": store.create_client("intake2", "Other intake", ["submitter"]),
        "alice": store.create_client("alice", "Alice", ["approver"]),
        "viewer": store.create_client("viewer", "Viewer", ["viewer"]),
        "selfie": store.create_client("selfie", "Both hats", ["submitter", "approver"]),
    }
    store.close()
    store = ServiceStore(dsn_from_env(), max_size=2, search_path=schema.search_path)  # type: ignore[attr-defined]
    with TestClient(create_app(store, queue, settings)) as client:
        yield Env(client, store, schema, queue, keys)


@pytest.fixture
def env(schema: Store, queue: WorkQueue) -> Iterator[Env]:
    yield from _env(schema, queue)


# --- auth -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "header",
    ["", "Bearer nonsense", "Basic dXNlcjpwYXNz", "Bearer vk.alice.not-the-secret"],
    ids=["none", "malformed", "wrong-scheme", "wrong-secret"],
)
def test_requests_without_a_valid_key_are_refused(env: Env, header: str) -> None:
    r = env.client.get("/v1/escalations", headers={"Authorization": header} if header else {})
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"


def test_a_disabled_key_is_refused(env: Env) -> None:
    assert env.client.get("/v1/escalations", headers=env.as_("alice")).status_code == 200
    env.store.disable_client("alice")
    assert env.client.get("/v1/escalations", headers=env.as_("alice")).status_code == 401


def test_roles_are_enforced(env: Env) -> None:
    assert env.submit(who="viewer").status_code == 403
    assert env.client.get("/v1/escalations", headers=env.as_("intake")).status_code == 403


def test_keys_are_stored_only_as_salted_hashes(env: Env) -> None:
    row = env.db.conn.execute("SELECT * FROM api_clients WHERE client_id = 'alice'").fetchone()
    secret = env.keys["alice"].split(".", 2)[2]
    assert row is not None and secret not in str(row)


# --- submit: idempotent -------------------------------------------------------------------
def test_a_replayed_submit_returns_the_original_workflow(env: Env) -> None:
    first = env.submit()
    assert first.status_code == 202 and first.json()["created"] is True
    again = env.submit()
    assert again.status_code == 200 and again.json()["workflow_id"] == first.json()["workflow_id"]
    assert env.queue.depth() == 1  # enqueued once
    count = env.db.conn.execute("SELECT count(*) AS n FROM workflow_requests").fetchone()
    assert count is not None and count["n"] == 1


def test_a_reused_key_for_a_different_request_is_refused(env: Env) -> None:
    assert env.submit().status_code == 202
    assert env.submit(body={**ORDER, "qty": 11}).status_code == 409


def test_keys_are_per_client(env: Env) -> None:
    a, b = env.submit(who="intake"), env.submit(who="intake2")
    assert a.json()["workflow_id"] != b.json()["workflow_id"]


@pytest.mark.parametrize(
    "body",
    [
        {**ORDER, "qty": 0},
        {**ORDER, "unexpected": 1},
        {**ORDER, "item_code": "<script>alert(1)</script>"},
        {**ORDER, "needed_by": "next tuesday"},
        {k: v for k, v in ORDER.items() if k != "supplier"},
    ],
    ids=["qty-zero", "extra-field", "markup", "bad-date", "missing-field"],
)
def test_invalid_requests_are_refused(env: Env, body: dict[str, Any]) -> None:
    assert env.submit(body=body).status_code == 422
    assert env.queue.depth() == 0


def test_an_idempotency_key_is_required_and_well_formed(env: Env) -> None:
    r = env.client.post("/v1/workflows", json=ORDER, headers=env.as_("intake"))
    assert r.status_code == 422
    assert env.submit(key="short").status_code == 422


# --- read: a submitter sees only their own ---------------------------------------------------
def test_visibility(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    mine = env.client.get(f"/v1/workflows/{wid}", headers=env.as_("intake"))
    assert mine.status_code == 200 and mine.json()["status"] == "pending"
    assert env.client.get(f"/v1/workflows/{wid}", headers=env.as_("intake2")).status_code == 404
    assert env.client.get(f"/v1/workflows/{wid}", headers=env.as_("viewer")).status_code == 200


def test_documents_link_to_erpnext(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    env.db.conn.execute(
        "INSERT INTO commits (idempotency_key, workflow_id, step, doctype, doc_name)"
        " VALUES ('k1', %s, 'S1', 'Material Request', 'MAT-MR-0001')",
        (wid,),
    )
    docs = env.client.get(f"/v1/workflows/{wid}", headers=env.as_("viewer")).json()["documents"]
    assert docs[0]["url"].endswith("/app/material-request/MAT-MR-0001")


# --- the human queue ----------------------------------------------------------------------------
def _decide(env: Env, who: str, wid: str, decision: str) -> Any:
    return env.client.post(
        f"/v1/escalations/{wid}/decisions",
        json={"decision": decision, "reason": "reviewed the supplier contract"},
        headers=env.as_(who),
    )


def test_an_approval_resumes_the_workflow_once(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    env.queue.r.delete("veritas:workflows")
    env.escalate(wid, "verifier_rejected: amount within approved authority")
    queue = env.client.get("/v1/escalations", headers=env.as_("alice")).json()["escalations"]
    assert queue[0]["workflow_id"] == wid and queue[0]["approvable"] is True

    r = _decide(env, "alice", wid, "approve_and_resume")
    assert r.status_code == 201 and r.json()["status"] == "in_progress"
    assert env.queue.depth() == 1  # handed back to a worker
    decision_id = r.json()["decision_id"]
    assert unconsumed_approval(env.db.conn, wid, "S5") == decision_id
    assert _decide(env, "alice", wid, "approve_and_resume").status_code == 409  # not escalated now

    # Spent by the first committed attempt that cites it.
    env.db.conn.execute(
        "INSERT INTO step_attempts"
        " (workflow_id, step, attempt, action, committed, latency_ms, gate)"
        " VALUES (%s, 'S5', 1, 'proceed', TRUE, 1, %s)",
        (wid, psycopg.types.json.Json({"override_decision": decision_id})),
    )
    assert unconsumed_approval(env.db.conn, wid, "S5") is None


def test_a_hard_rule_violation_can_only_be_rejected(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    env.escalate(wid, "hard_rule_violation: S5_WITHIN_APPROVED_AUTHORITY")
    r = _decide(env, "alice", wid, "approve_and_resume")
    assert r.status_code == 409 and "hard accounting invariant" in r.json()["detail"]
    r = _decide(env, "alice", wid, "reject")
    assert r.status_code == 201 and r.json()["status"] == "rejected"


@pytest.mark.parametrize(
    "reason",
    ["executor_chose_hold", "floor: calibrated region scored commit", "llm_call_budget_exhausted"],
)
def test_the_agent_s_own_refusals_and_spent_budgets_are_not_approvable(
    env: Env, reason: str
) -> None:
    wid = env.submit().json()["workflow_id"]
    env.escalate(wid, reason)
    assert _decide(env, "alice", wid, "approve_and_resume").status_code == 409


def test_an_outage_hand_off_can_be_resumed(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    env.escalate(wid, "infrastructure_unavailable: ollama unreachable", step="S2")
    assert _decide(env, "alice", wid, "approve_and_resume").status_code == 201


def test_separation_of_duties(env: Env) -> None:
    wid = env.submit(who="selfie").json()["workflow_id"]
    env.escalate(wid, "low_confidence: region commit,escalate")
    r = _decide(env, "selfie", wid, "approve_and_resume")
    assert r.status_code == 403 and "separation of duties" in r.json()["detail"]
    assert _decide(env, "intake", wid, "reject").status_code == 403  # no approver role


def test_decisions_are_append_only(env: Env) -> None:
    wid = env.submit().json()["workflow_id"]
    env.escalate(wid, "low_confidence: region commit,escalate")
    assert _decide(env, "alice", wid, "reject").status_code == 201
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        env.db.conn.execute("UPDATE human_decisions SET decision = 'approve_and_resume'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        env.db.conn.execute("DELETE FROM human_decisions")


# --- operations ----------------------------------------------------------------------------------
def test_health_readiness_metrics_and_headers(env: Env) -> None:
    assert env.client.get("/healthz").status_code == 200
    ready = env.client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"] == {"postgres": True, "redis": True}
    r = env.client.get("/v1/escalations", headers=env.as_("alice", **{"X-Request-ID": "req-123"}))
    assert r.headers["X-Request-ID"] == "req-123"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    metrics = env.client.get("/metrics").text
    assert (
        'veritas_api_requests_total{method="GET",route="/v1/escalations",status="200"}' in metrics
    )


def test_oversized_bodies_are_refused(env: Env) -> None:
    big = {**ORDER, "bill_no": "x" * 70_000}
    r = env.client.post(
        "/v1/workflows", json=big, headers=env.as_("intake", **{"Idempotency-Key": "order-big1"})
    )
    assert r.status_code == 413


def test_per_client_rate_limit(schema: Store, queue: WorkQueue) -> None:
    for e in _env(schema, queue, api_rate_limit_per_min=3):
        codes = [
            e.client.get("/v1/escalations", headers=e.as_("alice")).status_code for _ in range(5)
        ]
        assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
        assert e.client.get("/v1/escalations", headers=e.as_("viewer")).status_code == 200


# --- migrations ----------------------------------------------------------------------
def test_migrations_are_recorded_and_idempotent(schema: Store) -> None:
    assert schema.apply_schema() == []  # the fixture already applied everything
    versions = [r["version"] for r in schema.conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["0001", "0002"]


def test_an_edited_applied_migration_is_refused(schema: Store) -> None:
    schema.conn.execute("UPDATE schema_migrations SET checksum = 'x' WHERE version = '0002'")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        schema.apply_schema()


def test_a_newer_schema_does_not_block_an_older_release(schema: Store) -> None:
    """Rollback: the release is older than the database. Warn, do not refuse."""
    schema.conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES ('9999', 'x')")
    assert schema.apply_schema() == []


# --- worker ------------------------------------------------------------------------------
def _runner(db: Store, queue: WorkQueue) -> Any:
    from erp.resilience import CircuitBreaker
    from service.worker import WorkflowRunner

    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner.store = db
    runner.breakers = {n: CircuitBreaker(queue.r, n, 2, 60) for n in ("erp", "model")}
    return runner


def test_the_worker_never_reruns_a_workflow_that_waits_on_a_human(env: Env) -> None:
    runner = _runner(env.db, env.queue)
    wid = env.submit().json()["workflow_id"]
    env.escalate(wid, "verifier_rejected: x")
    assert runner(wid) == "escalated"
    _decide(env, "alice", wid, "reject")
    assert runner(wid) == "rejected"
    assert runner("req-does-not-exist") == "missing"


def test_an_open_breaker_defers_before_any_work(env: Env) -> None:
    from erp.resilience import InfrastructureUnavailable

    runner = _runner(env.db, env.queue)
    runner.breakers["model"].record_failure()
    runner.breakers["model"].record_failure()
    with pytest.raises(InfrastructureUnavailable, match="model circuit open"):
        runner("req-anything")


def test_an_outage_that_outlasts_deferral_goes_to_the_human_queue(env: Env) -> None:
    runner = _runner(env.db, env.queue)
    wid = env.submit().json()["workflow_id"]
    runner.give_up(wid, "infrastructure_unavailable: ollama unreachable at http://x")
    queue = env.client.get("/v1/escalations", headers=env.as_("alice")).json()["escalations"]
    assert [e["workflow_id"] for e in queue] == [wid] and queue[0]["approvable"] is True


def test_a_request_becomes_the_spec_the_agent_runs() -> None:
    from service.worker import spec_from_request

    spec = spec_from_request("req-1", {**ORDER, "received_qty": 9, "tolerance_pct": 1.5})
    assert spec.workflow_id == "req-1" and spec.qty == 10.0 and spec.received_qty == 9
    assert spec.tolerance_pct == 1.5 and spec.invoice_rate is None
    assert str(spec.expected_total) == "1255.00"


def test_the_worker_takes_erp_credentials_from_settings(tmp_path: Any) -> None:
    """A key mounted as a file reaches the ERP client; a buyer key scopes it."""
    from erp.scoped import StepScopedERP
    from service.worker import erp_from_settings

    key = tmp_path / "key"
    key.write_text("fin-key\n", encoding="utf-8")
    import os

    os.environ["ERPNEXT_API_KEY_FILE"] = str(key)
    try:
        plain = erp_from_settings(load_settings(erpnext_api_secret="fin-secret"))
        assert plain.s.headers["Authorization"] == "token fin-key:fin-secret"
        scoped = erp_from_settings(
            load_settings(
                erpnext_api_secret="fin-secret",
                erpnext_buyer_api_key="buy-key",
                erpnext_buyer_api_secret="buy-secret",
            )
        )
        assert isinstance(scoped, StepScopedERP)
    finally:
        del os.environ["ERPNEXT_API_KEY_FILE"]
