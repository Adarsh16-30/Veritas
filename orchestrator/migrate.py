"""Forward-only schema migrations for the agent Postgres (production hardening, B7).

``orchestrator/migrations/NNNN_name.sql`` are applied in order, each in its own
transaction, and recorded in ``schema_migrations`` with the file's SHA-256.

* **Forward-only.** There are no down-migrations; a mistake is fixed by the next
  file. A rollback of the service never rolls back the schema, so every
  migration must stay compatible with the previous release (add columns and
  tables; never drop or rename in the same release that stops using them).
* **Immutable once applied.** A recorded migration whose file has since changed
  is refused: the database and the repository would disagree about what the
  schema is, silently.
* **One migrator at a time.** A session-level advisory lock serialises
  concurrent runs (two pods starting at once, a Helm hook racing a worker).

``0001_baseline.sql`` is the schema every earlier deployment already has. It is
written to be re-runnable, so recording it on an existing database is safe.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

#: Arbitrary, fixed: the advisory-lock id every migrator agrees on.
LOCK_ID = 7_102_026

_log = logging.getLogger("veritas.migrate")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    versions = [p.name.split("_", 1)[0] for p in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {directory}: {versions}")
    return [Migration(p.name.split("_", 1)[0], p) for p in found]


def apply(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration; return the versions applied now.

    ``conn`` is a psycopg connection in autocommit mode (as ``Store`` opens it):
    each migration then runs in an explicit transaction of its own.
    """
    migrations = discover(directory)
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                checksum   TEXT        NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        recorded = {_get(r, "version", 0): _get(r, "checksum", 1) for r in rows}
        for m in migrations:
            if m.version in recorded and recorded[m.version] != m.checksum:
                raise MigrationError(
                    f"migration {m.path.name} was changed after it was applied; write a new "
                    "migration instead of editing an applied one"
                )
        unknown = sorted(set(recorded) - {m.version for m in migrations})
        if unknown:
            # A rolled-back release meets a newer schema. Every migration is
            # backward-compatible by policy, so this is expected, not an error.
            _log.warning("database has newer migrations than this release: %s", unknown)
        applied: list[str] = []
        for m in migrations:
            if m.version in recorded:
                continue
            with conn.transaction():
                conn.execute(m.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (m.version, m.checksum),
                )
            applied.append(m.version)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def _get(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, dict) else row[index]
