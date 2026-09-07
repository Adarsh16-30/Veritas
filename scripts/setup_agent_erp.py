"""Phase 2 ERP setup: add the idempotency-key custom field to the buying chain.

Every document the agent submits carries its idempotency key in
``veritas_idempotency_key``. That stamp lives in the ledger itself, which is what
lets crash recovery tell "ERPNext already accepted this write" from "this write
never happened" — the window ``submit_once`` alone cannot close (Rules 5–6).

Runs as Administrator (creating a Custom Field needs System Manager, which the
scoped agent identity deliberately does not have). Idempotent; safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from erp.client import BUYING_CHAIN, IDEMPOTENCY_FIELD, ERPClient, ERPError  # noqa: E402

load_dotenv()


def ensure_custom_field(erp: ERPClient, doctype: str) -> str:
    name = f"{doctype}-{IDEMPOTENCY_FIELD}"
    existing = erp.get_list("Custom Field", filters=[["name", "=", name]], fields=["name"], limit=1)
    if existing:
        return f"exists   {name}"
    erp.insert(
        "Custom Field",
        {
            "dt": doctype,
            "fieldname": IDEMPOTENCY_FIELD,
            "label": "Veritas Idempotency Key",
            "fieldtype": "Data",
            "length": 64,
            "read_only": 1,
            "no_copy": 1,
            "in_standard_filter": 0,
            "translatable": 0,
            "description": "Deterministic key for the agent write that produced this document.",
        },
    )
    return f"created  {name}"


def main() -> int:
    # Administrator: creating Custom Fields requires System Manager, which the
    # scoped agent identity deliberately lacks.
    erp = ERPClient.as_administrator()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1
    print(f"adding {IDEMPOTENCY_FIELD} to the buying chain on {erp.url}")
    try:
        for doctype in BUYING_CHAIN:
            print("  " + ensure_custom_field(erp, doctype))
    except ERPError as e:
        print(f"\nfailed:\n{e}", file=sys.stderr)
        return 1
    print("\nagent ERP setup OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
