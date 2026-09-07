"""Create a scoped service user for the agent and print/store its API key+secret.

PRD §2.1 / §6.1: "a dedicated *scoped* service user holds the API key" —
least-privilege, **no System Manager**.

Phase 1 grants the standard buying + accounts role bundle (enough to drive
S1..S6) and deliberately withholds System Manager. Phase 6 replaces this bundle
with a custom minimal role and removes payment scope from the S1..S3 identity.
"""

from __future__ import annotations

import sys

from _erpclient import ERP, ERPError, write_env

AGENT_USER = "veritas.agent@veritas.local"
AGENT_FIRST_NAME = "Veritas"
AGENT_LAST_NAME = "Agent"

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


def ensure_user(erp: ERP) -> None:
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
    if not current >= ROLES:
        erp.update(
            "User", AGENT_USER, {"roles": [{"role": r} for r in sorted(set(ROLES) | current)]}
        )
        print(f"  user: roles set -> {sorted(set(ROLES) | current)}")
    else:
        print("  user: roles already assigned")


def generate_keys(erp: ERP) -> tuple[str, str]:
    res = erp.method("frappe.core.doctype.user.user.generate_keys", user=AGENT_USER)
    secret = res["api_secret"] if isinstance(res, dict) else res
    key = erp.get("User", AGENT_USER)["api_key"]
    return key, secret


def main() -> int:
    erp = ERP()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1
    erp.login_admin()
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
