"""Shared synthetic test doubles.

These live here (not in any non-test module) per Rule 1 / Rule 8: stubs only in
``tests/``. They stand in for the durable commit store and the ERPNext client so
the idempotency primitive (Rule 5) can be exercised in isolation.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

# Integration tests talk to the real ERPNext, Postgres, Redis and Ollama. Their
# addresses and the scoped agent credentials live in .env (gitignored), exactly as
# they do for the scripts — load it here so a test run is configured identically.
load_dotenv()


class FakeCommitDB:
    """In-memory stand-in for the durable idempotency-key store."""

    def __init__(self) -> None:
        self._committed: dict[str, str] = {}

    def get_committed(self, key: str) -> str | None:
        return self._committed.get(key)

    def record_committed(self, key: str, name: str) -> None:
        if key in self._committed:  # a real store would enforce this too
            raise AssertionError(f"key already committed: {key}")
        self._committed[key] = name


class CountingERP:
    """Counts real writes so a double-dispatch can be detected."""

    def __init__(self) -> None:
        self.calls = 0

    def insert_and_submit(self, doctype: str, doc: dict) -> str:
        self.calls += 1
        return f"{doctype}-{self.calls:04d}"


@pytest.fixture
def commit_db() -> FakeCommitDB:
    return FakeCommitDB()


@pytest.fixture
def counting_erp() -> CountingERP:
    return CountingERP()
