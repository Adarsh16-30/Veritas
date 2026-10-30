"""Apply pending schema migrations to the agent Postgres (orchestrator/migrate.py).

    uv run python scripts/migrate.py            # apply
    uv run python scripts/migrate.py --status   # list applied / pending, change nothing

Forward-only and safe to re-run; concurrent runs serialise on an advisory lock.
The Helm chart runs this as a pre-install/pre-upgrade Job.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.db import Store  # noqa: E402
from orchestrator.migrate import MigrationError, discover  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--status", action="store_true", help="show state; apply nothing")
    args = p.parse_args()
    load_dotenv()
    with Store() as store:
        if args.status:
            exists = store.conn.execute(
                "SELECT to_regclass('schema_migrations') IS NOT NULL AS e"
            ).fetchone()
            done = (
                {r["version"] for r in store.conn.execute("SELECT version FROM schema_migrations")}
                if exists and exists["e"]
                else set()
            )
            for m in discover():
                print(f"  {'applied' if m.version in done else 'PENDING'}  {m.path.name}")
            return 0
        try:
            applied = store.apply_schema()
        except MigrationError as e:
            print(f"migration refused: {e}", file=sys.stderr)
            return 1
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
