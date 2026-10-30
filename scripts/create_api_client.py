"""Issue an API key for the VERITAS service (service/auth.py).

    uv run python scripts/create_api_client.py --id ops-alice --name "Alice (AP)" --roles approver
    uv run python scripts/create_api_client.py --id intake --name "Intake system" --roles submitter

The key is printed once and never stored; only its salted hash is. Lost keys
are replaced, not recovered. Give submitters and approvers different clients:
the API refuses to let a client decide its own request (separation of duties).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.settings import load_settings  # noqa: E402
from service.auth import ROLES  # noqa: E402
from service.store import ServiceStore  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--id", required=True, help="client id: a-z, 0-9 and '-'")
    p.add_argument("--name", required=True)
    p.add_argument("--roles", required=True, help=f"comma-separated, from {', '.join(ROLES)}")
    args = p.parse_args()
    roles = sorted({r.strip() for r in args.roles.split(",") if r.strip()})
    unknown = set(roles) - set(ROLES)
    if not roles or unknown:
        print(f"unknown roles: {sorted(unknown) or '(none given)'}", file=sys.stderr)
        return 2
    load_dotenv()
    settings = load_settings()
    store = ServiceStore(settings.postgres_dsn(), max_size=1)
    store.open()
    try:
        token = store.create_client(args.id, args.name, roles)
    finally:
        store.close()
    print(f"client {args.id} ({', '.join(roles)}). API key, shown once:\n\n  {token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
