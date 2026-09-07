"""Phase 1 bootstrap: bring a fresh ERPNext site to a state where the full buying
chain can run.

Idempotent — safe to re-run. Creates (if missing):
  * Company + fiscal year + chart of accounts + default warehouse + cost center
    (via the setup wizard)
  * one Supplier
  * one purchasable, stock-maintained Item with company defaults

Real ERP only (Rule 1). No placeholder data generators (Rule 8) — the single
supplier/item here are fixed bootstrap fixtures for the hand-run chain, not
evaluation data; evaluation data is seeded from cited datasets in Phase 4.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from erp.client import ERPClient, ERPError  # noqa: E402

load_dotenv()

COMPANY = os.environ.get("VERITAS_COMPANY", "Veritas Test Co")
ABBR = os.environ.get("VERITAS_ABBR", "VTC")
COUNTRY = os.environ.get("VERITAS_COUNTRY", "United States")
CURRENCY = os.environ.get("VERITAS_CURRENCY", "USD")
FY_START = os.environ.get("VERITAS_FY_START", "2026-01-01")
FY_END = os.environ.get("VERITAS_FY_END", "2026-12-31")

SUPPLIER = "Acme Industrial Supply"
ITEM_CODE = "WIDGET-A"
WAREHOUSE = f"Stores - {ABBR}"
EXPENSE_ACCOUNT = f"Cost of Goods Sold - {ABBR}"
COST_CENTER = f"Main - {ABBR}"


def ensure_setup_complete(erp: ERPClient) -> None:
    if erp.exists("Company", [["name", "=", COMPANY]]):
        print(f"  setup: Company {COMPANY!r} already exists — skipping wizard")
        return
    print("  setup: running setup wizard ...")
    erp.call(
        "frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
        args={
            "language": "English",
            "country": COUNTRY,
            "currency": CURRENCY,
            "timezone": "America/New_York",
            "company_name": COMPANY,
            "company_abbr": ABBR,
            "chart_of_accounts": "Standard",
            "fy_start_date": FY_START,
            "fy_end_date": FY_END,
            "setup_demo": 0,
        },
    )
    print(f"  setup: Company {COMPANY!r}, FY {FY_START}..{FY_END}, CoA Standard")


def ensure_supplier(erp: ERPClient) -> None:
    if erp.exists("Supplier", [["name", "=", SUPPLIER]]):
        print(f"  supplier: {SUPPLIER!r} exists")
        return
    erp.insert(
        "Supplier",
        {
            "supplier_name": SUPPLIER,
            "supplier_group": "All Supplier Groups",
            "supplier_type": "Company",
        },
    )
    print(f"  supplier: created {SUPPLIER!r}")


def ensure_item(erp: ERPClient) -> None:
    if not erp.exists("Item", [["name", "=", ITEM_CODE]]):
        erp.insert(
            "Item",
            {
                "item_code": ITEM_CODE,
                "item_name": "Widget A",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 1,
                "is_purchase_item": 1,
            },
        )
        print(f"  item: created {ITEM_CODE!r}")
    else:
        print(f"  item: {ITEM_CODE!r} exists")

    # ERPNext auto-creates an item_defaults row (company + default_warehouse) on
    # insert; ensure that row also carries the expense account and buying cost
    # centre the buying chain needs. Idempotent: only writes when a field differs.
    item = erp.get("Item", ITEM_CODE)
    defaults = item.get("item_defaults", [])
    row = next((d for d in defaults if d.get("company") == COMPANY), None)
    if row is None:
        row = {"company": COMPANY}
        defaults.append(row)

    wanted = {
        "default_warehouse": WAREHOUSE,
        "expense_account": EXPENSE_ACCOUNT,
        "buying_cost_center": COST_CENTER,
    }
    if any(row.get(k) != v for k, v in wanted.items()):
        row.update(wanted)
        erp.update("Item", ITEM_CODE, {"item_defaults": defaults})
        print(f"  item: set company defaults (wh={WAREHOUSE}, exp={EXPENSE_ACCOUNT})")
    else:
        print("  item: company defaults already complete")


def main() -> int:
    erp = ERPClient.as_administrator()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url} — start the stack first", file=sys.stderr)
        return 1
    print(f"bootstrapping {erp.url}")
    try:
        ensure_setup_complete(erp)
        ensure_supplier(erp)
        ensure_item(erp)
    except ERPError as e:
        print(f"\nbootstrap failed:\n{e}", file=sys.stderr)
        return 1
    print("\nbootstrap OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
