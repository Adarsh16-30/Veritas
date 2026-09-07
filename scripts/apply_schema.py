"""Apply the agent state schema to the agent Postgres (PRD §3.3).

Idempotent — every statement in ``orchestrator/schema.sql`` is CREATE ... IF NOT
EXISTS or CREATE OR REPLACE. Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.db import Store, dsn_from_env  # noqa: E402

load_dotenv()


def main() -> int:
    dsn = dsn_from_env()
    print(f"applying schema to: {dsn.replace(chr(10), ' ')}")
    with Store() as store:
        store.apply_schema()
        rows = store.conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
    print("tables: " + ", ".join(r["tablename"] for r in rows))
    print("schema OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
