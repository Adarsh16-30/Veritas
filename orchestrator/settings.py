"""Validated runtime configuration for the VERITAS service (production hardening).

One typed object for every setting the API and the worker need, read from the
environment under the **same variable names** the scripts and ``.env`` already
use, so nothing about the existing setup changes. What it adds:

* **Fail fast.** ``load_settings()`` validates everything at start-up and, for
  ``VERITAS_ENV=production``, refuses to start on an unsafe combination
  (``production_problems``), listing every problem at once.
* **Secrets from files.** Every secret also accepts ``<NAME>_FILE``, a path to a
  file holding the value -- how Kubernetes Secrets and Docker secrets are
  mounted. Secrets are ``SecretStr``: they never appear in a repr, a log line or
  an error message.
* **Small by default.** The defaults fit the development laptop (see
  docs/operations.md): one worker thread, a small connection pool.

The CLI scripts and the benchmark keep constructing their clients from the
environment directly; the service builds its clients from this object.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Settings that may be supplied as ``<NAME>_FILE``.
SECRET_FIELDS = (
    "erpnext_api_key",
    "erpnext_api_secret",
    "erpnext_buyer_api_key",
    "erpnext_buyer_api_secret",
    "postgres_password",
    "redis_url",
    "executor_api_key",
    "verifier_api_key",
)


class SettingsError(RuntimeError):
    """The configuration cannot be used. Carries every problem found."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # --- deployment -----------------------------------------------------------
    veritas_env: Literal["dev", "production"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- ERPNext (Rule 1: the real system of record) ---------------------------
    erpnext_url: str = "http://localhost:8080"
    erpnext_public_url: str | None = None
    erpnext_api_key: SecretStr | None = None
    erpnext_api_secret: SecretStr | None = None
    erpnext_buyer_api_key: SecretStr | None = None
    erpnext_buyer_api_secret: SecretStr | None = None
    erpnext_timeout_s: int = Field(default=60, ge=1, le=600)

    # --- agent state (Postgres) and queue (Redis) ------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_db: str = "veritas"
    postgres_user: str = "veritas"
    postgres_password: SecretStr = SecretStr("veritas")
    postgres_sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"
    postgres_pool_max: int = Field(default=4, ge=1, le=64)
    redis_url: SecretStr = SecretStr("redis://localhost:6380/0")

    # --- models -----------------------------------------------------------------
    model_backend: Literal["ollama", "openai"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:8b-instruct-q4_K_M"
    verifier_model: str | None = None
    ollama_keep_alive: str = "30m"
    executor_base_url: str | None = None
    executor_api_key: SecretStr | None = None
    executor_openai_model: str | None = None
    executor_family: str | None = None
    verifier_base_url: str | None = None
    verifier_api_key: SecretStr | None = None
    verifier_openai_model: str | None = None
    verifier_family: str | None = None
    model_timeout_s: int = Field(default=180, ge=5, le=900)
    model_max_concurrency: int = Field(default=2, ge=1, le=64)

    # --- worker -----------------------------------------------------------------
    worker_concurrency: int = Field(default=1, ge=1, le=32)
    lock_ttl_s: int = Field(default=60, ge=10, le=3600)
    lock_renew_s: int = Field(default=20, ge=2, le=1800)
    max_deferrals: int = Field(default=5, ge=0, le=100)
    defer_base_s: int = Field(default=30, ge=1, le=3600)
    breaker_failures: int = Field(default=5, ge=1, le=1000)
    breaker_cooldown_s: int = Field(default=60, ge=1, le=3600)

    # --- API --------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_rate_limit_per_min: int = Field(default=60, ge=1, le=100_000)
    api_max_body_bytes: int = Field(default=65_536, ge=1024, le=10_485_760)

    @model_validator(mode="before")
    @classmethod
    def _read_secret_files(cls, data: Any) -> Any:
        """``<NAME>_FILE`` wins over ``<NAME>``: a mounted secret is the source of truth."""
        if not isinstance(data, dict):
            return data
        for name in SECRET_FIELDS:
            path = os.environ.get(f"{name.upper()}_FILE")
            if path:
                try:
                    data[name] = Path(path).read_text(encoding="utf-8").strip()
                except OSError as e:
                    raise ValueError(f"{name.upper()}_FILE={path!r} is unreadable: {e}") from e
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.lock_renew_s * 2 > self.lock_ttl_s:
            raise ValueError(
                "LOCK_RENEW_S must be at most half of LOCK_TTL_S, or one missed renewal "
                "loses the lock"
            )
        return self

    # --- derived ----------------------------------------------------------------
    def postgres_dsn(self) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} dbname={self.postgres_db} "
            f"user={self.postgres_user} password={self.postgres_password.get_secret_value()} "
            f"sslmode={self.postgres_sslmode}"
        )

    def production_problems(self) -> list[str]:
        """Everything that makes this configuration unsafe to run for real money."""
        problems: list[str] = []
        if not (self.erpnext_api_key and self.erpnext_api_secret):
            problems.append("ERPNEXT_API_KEY / ERPNEXT_API_SECRET are required")
        if not (self.erpnext_buyer_api_key and self.erpnext_buyer_api_secret):
            problems.append(
                "ERPNEXT_BUYER_API_KEY / SECRET are required: S1-S3 must hold no payment "
                "scope in production (erp/scoped.py)"
            )
        if self.postgres_password.get_secret_value() == "veritas":
            problems.append("POSTGRES_PASSWORD is the development default")
        if self.postgres_sslmode in ("disable", "allow", "prefer"):
            problems.append("POSTGRES_SSLMODE must be require, verify-ca or verify-full")
        if self.model_backend == "openai":
            for role in ("executor", "verifier"):
                if not getattr(self, f"{role}_base_url"):
                    problems.append(f"{role.upper()}_BASE_URL is required for MODEL_BACKEND=openai")
                if not getattr(self, f"{role}_openai_model"):
                    problems.append(f"{role.upper()}_OPENAI_MODEL is required")
                if not getattr(self, f"{role}_family"):
                    problems.append(f"{role.upper()}_FAMILY is required (Rule 3)")
            if (
                self.executor_family
                and self.verifier_family
                and self.executor_family.strip().lower() == self.verifier_family.strip().lower()
            ):
                problems.append(
                    "EXECUTOR_FAMILY and VERIFIER_FAMILY are the same: the verifier would not "
                    "be independent (Rule 3)"
                )
        return problems


def load_settings(**overrides: Any) -> Settings:
    """Read and validate; in production, refuse an unsafe configuration."""
    try:
        settings = Settings(**overrides)
    except ValueError as e:
        raise SettingsError(f"invalid configuration:\n{e}") from e
    if settings.veritas_env == "production":
        problems = settings.production_problems()
        if problems:
            raise SettingsError(
                "refusing to start in production:\n" + "\n".join(f"  - {p}" for p in problems)
            )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings for long-running services."""
    return load_settings()
