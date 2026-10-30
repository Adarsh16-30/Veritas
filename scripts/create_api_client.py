"""Issue an API key for the VERITAS service (service/auth.py).

    uv run python scripts/create_api_client.py --id ops-alice --name "Alice (AP)" --roles approver
    uv run python scripts/create_api_client.py --id intake --name "Intake system" --roles submitter

    uv run python scripts/create_api_client.py --disable ops-alice    # revoke

The key is printed once and never stored; only its salted hash is. Lost keys
are replaced, not recovered: issue a new client, then disable the old one.
Give submitters and approvers different clients: the API refuses to let a
client decide its own request (separation of duties).
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
    p.add_argument("--id", help="client id: a-z, 0-9 and '-'")
    p.add_argument("--name")
    p.add_argument("--roles", help=f"comma-separated, from {', '.join(ROLES)}")
    p.add_argument("--disable", metavar="ID", help="revoke this client's key")
    args = p.parse_args()
    load_dotenv()
    if args.disable:
        store = ServiceStore(load_settings().postgres_dsn(), max_size=1)
        store.open()
        try:
            done = store.disable_client(args.disable)
        finally:
            store.close()
        print(f"client {args.disable} {'disabled' if done else 'not found'}")
        return 0 if done else 1
    if not (args.id and args.name and args.roles):
        p.error("--id, --name and --roles are required to create a client")
    roles = sorted({r.strip() for r in args.roles.split(",") if r.strip()})
    unknown = set(roles) - set(ROLES)
    if not roles or unknown:
        print(f"unknown roles: {sorted(unknown) or '(none given)'}", file=sys.stderr)
        return 2
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
