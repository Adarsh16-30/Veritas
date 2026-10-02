"""Create a scoped service user for the agent and print/store its API key+secret.

PRD §2.1 / §6.1: "a dedicated *scoped* service user holds the API key" —
least-privilege, **no System Manager**.

Phase 1 grants the standard buying + accounts role bundle (enough to drive
S1..S6) and deliberately withholds System Manager.

Phase 6 adds a second, narrower identity for S1..S3 (``--buyer``): purchasing
and stock roles only, **no Accounts role**, so ERPNext itself refuses it any
Payment Entry access ("S1–S3 hold no payment scope"). Its key goes to
ERPNEXT_BUYER_API_KEY/SECRET, and ``erp.scoped.agent_client`` then runs S1..S3
on it. ``--buyer`` never touches the existing agent key.

    uv run python scripts/generate_scoped_api_key.py           # agent (S4..S6) key
    uv run python scripts/generate_scoped_api_key.py --buyer   # buyer (S1..S3) key
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from erp.client import ERPClient, ERPError  # noqa: E402
from scripts._env import write_env  # noqa: E402

load_dotenv()

AGENT_USER = "veritas.agent@veritas.local"
AGENT_FIRST_NAME = "Veritas"
AGENT_LAST_NAME = "Agent"

BUYER_USER = "veritas.buyer@veritas.local"

# Standard ERPNext roles that together cover material request -> payment entry.
# NOTE: "System Manager" is intentionally absent.
ROLES = [
    "Purchase Master Manager",
    "Purchase Manager",
    "Purchase User",
    "Stock User",
    "Item Manager",
    "Accounts User",
]

# S1..S3: requisition, policy check, purchase order. No Accounts role of any
# kind -- Payment Entry permissions in ERPNext hang off the Accounts roles.
BUYER_ROLES = [
    "Purchase Master Manager",
    "Purchase Manager",
    "Purchase User",
    "Stock User",
    "Item Manager",
]


def _forbidden_for_buyer(role: str) -> bool:
    return role == "System Manager" or role.startswith("Accounts") or role == "Auditor"


def ensure_user(erp: ERPClient) -> None:
    if erp.exists("User", [["name", "=", AGENT_USER]]):
        print(f"  user: {AGENT_USER!r} exists")
    else:
        erp.insert(
            "User",
            {
                "email": AGENT_USER,
                "first_name": AGENT_FIRST_NAME,
                "last_name": AGENT_LAST_NAME,
                "send_welcome_email": 0,
                "user_type": "System User",
            },
        )
        print(f"  user: created {AGENT_USER!r}")

    user = erp.get("User", AGENT_USER)
    current = {r["role"] for r in user.get("roles", [])}
    if "System Manager" in current:
        raise ERPError("refusing to proceed: agent user has System Manager (must be scoped)")
    want = set(ROLES)
    if not want <= current:
        merged = sorted(want | current)
        erp.update("User", AGENT_USER, {"roles": [{"role": r} for r in merged]})
        print(f"  user: roles set -> {merged}")
    else:
        print("  user: roles already assigned")


def ensure_buyer(erp: ERPClient) -> None:
    """The S1..S3 identity. Its roles are set exactly, never merged: a leftover
    Accounts role from an earlier setup would silently restore payment scope."""
    if not erp.exists("User", [["name", "=", BUYER_USER]]):
        erp.insert(
            "User",
            {
                "email": BUYER_USER,
                "first_name": "Veritas",
                "last_name": "Buyer",
                "send_welcome_email": 0,
                "user_type": "System User",
            },
        )
        print(f"  user: created {BUYER_USER!r}")
    erp.update("User", BUYER_USER, {"roles": [{"role": r} for r in BUYER_ROLES]})
    granted = {r["role"] for r in erp.get("User", BUYER_USER).get("roles", [])}
    bad = sorted(r for r in granted if _forbidden_for_buyer(r))
    if bad:
        raise ERPError(f"refusing to proceed: buyer identity still holds {bad}")
    print(f"  user: {BUYER_USER!r} roles -> {sorted(granted)} (no Accounts role)")


def generate_keys(erp: ERPClient, user: str = AGENT_USER) -> tuple[str, str]:
    res = erp.call("frappe.core.doctype.user.user.generate_keys", user=user)
    secret = res["api_secret"] if isinstance(res, dict) else res
    key = erp.get("User", user)["api_key"]
    return key, secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--buyer", action="store_true", help="create the S1..S3 buyer identity only"
    )
    args = parser.parse_args()

    erp = ERPClient.as_administrator()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1
    if args.buyer:
        try:
            ensure_buyer(erp)
            key, secret = generate_keys(erp, BUYER_USER)
        except ERPError as e:
            print(f"\nfailed:\n{e}", file=sys.stderr)
            return 1
        write_env({"ERPNEXT_BUYER_API_KEY": key, "ERPNEXT_BUYER_API_SECRET": secret})
        print("\nbuyer key generated (S1..S3; no Accounts role) -> written to .env")
        print("  the agent now runs step-scoped: erp.scoped.agent_client()")
        return 0
    try:
        ensure_user(erp)
        key, secret = generate_keys(erp)
    except ERPError as e:
        print(f"\nfailed:\n{e}", file=sys.stderr)
        return 1

    write_env({"ERPNEXT_API_KEY": key, "ERPNEXT_API_SECRET": secret})
    print("\nscoped agent key generated (System Manager withheld):")
    print(f"  ERPNEXT_API_KEY={key}")
    print(f"  ERPNEXT_API_SECRET={secret}")
    print("  -> written to .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
