"""Validated settings and redacted, correlated logging (production hardening)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from orchestrator.settings import SettingsError, load_settings
from trace.logging import JsonFormatter, log_context, redact

PROD_OK = {
    "veritas_env": "production",
    "erpnext_api_key": "k",
    "erpnext_api_secret": "s",
    "erpnext_buyer_api_key": "bk",
    "erpnext_buyer_api_secret": "bs",
    "postgres_password": "long-random",
    "postgres_sslmode": "require",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VERITAS_ENV", "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", "MODEL_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def test_dev_defaults_match_the_existing_scripts() -> None:
    s = load_settings()
    assert s.veritas_env == "dev" and s.postgres_port == 5433 and s.worker_concurrency == 1
    assert "sslmode=prefer" in s.postgres_dsn()


def test_secrets_never_appear_in_repr() -> None:
    s = load_settings(postgres_password="hunter2", erpnext_api_secret="topsecret")
    assert "hunter2" not in repr(s) and "topsecret" not in repr(s)


def test_secret_file_wins_over_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "pg"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(secret))
    assert load_settings().postgres_password.get_secret_value() == "from-file"


def test_unreadable_secret_file_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", "/nonexistent/secret")
    with pytest.raises(SettingsError, match="unreadable"):
        load_settings()


def test_production_refuses_every_unsafe_setting_at_once() -> None:
    with pytest.raises(SettingsError) as e:
        load_settings(veritas_env="production")
    msg = str(e.value)
    for needle in ("ERPNEXT_API_KEY", "BUYER", "development default", "SSLMODE"):
        assert needle in msg


def test_production_accepts_a_safe_configuration() -> None:
    assert load_settings(**PROD_OK).veritas_env == "production"


def test_rule3_same_declared_family_is_refused() -> None:
    """Two sizes of one model are not an independent verifier."""
    with pytest.raises(SettingsError, match="Rule 3"):
        load_settings(
            **PROD_OK,
            model_backend="openai",
            executor_base_url="http://vllm:8000",
            verifier_base_url="http://vllm:8001",
            executor_openai_model="meta-llama/Llama-3.1-8B-Instruct",
            verifier_openai_model="meta-llama/Llama-3.1-70B-Instruct",
            executor_family="llama",
            verifier_family="Llama",
        )


def test_lock_renewal_must_leave_slack() -> None:
    with pytest.raises(SettingsError, match="LOCK_RENEW_S"):
        load_settings(lock_ttl_s=30, lock_renew_s=20)


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: token abc123:def456", "def456"),
        ("Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("api_key=sk-live-123 next", "sk-live-123"),
        ("password: hunter2", "hunter2"),
        ("redis://user:p4ss@host:6379/0", "p4ss"),
    ],
)
def test_redaction(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_json_lines_carry_context_and_redact_extras() -> None:
    record = logging.LogRecord("veritas.x", logging.INFO, "f", 1, "posting %s", ("doc",), None)
    record.detail = "token key:secretvalue"
    with log_context(workflow_id="wf-1", step="S4"), log_context(attempt=2):
        line = json.loads(JsonFormatter().format(record))
    assert line["workflow_id"] == "wf-1" and line["step"] == "S4" and line["attempt"] == 2
    assert line["msg"] == "posting doc" and "secretvalue" not in line["detail"]
    after = json.loads(JsonFormatter().format(record))
    assert "workflow_id" not in after  # context does not leak past its block
