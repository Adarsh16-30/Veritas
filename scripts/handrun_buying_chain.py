"""Phase 1 reference: drive S1..S6 by hand, with no agent involved.

    uv run python scripts/handrun_buying_chain.py

This is the "you must see a real GL entry move first" check (PRD §6.1) and it
stays useful as the un-agented control: same chain, same ledger, no model in the
loop. The agent-driven equivalent is ``scripts/run_workflow.py``.

S1 Material Request (Purchase)  -> submit
S2 (policy check — trivially "proceed" here; it is the agent's job from Phase 2)
S3 Purchase Order               -> submit
S4 Purchase Receipt + Purchase Invoice (three-way match point) -> submit
S5 (discrepancy resolution — none on the happy path)
S6 Payment Entry               -> submit

Uses the scoped agent API key from .env. Every call hits the real REST API and
moves the real GL (Rule 1). Documents are not idempotency-stamped: this is a
manual chain, so each run deliberately creates a fresh one.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from erp.client import ERPClient, ERPError  # noqa: E402

load_dotenv()

COMPANY = os.environ.get("VERITAS_COMPANY", "Veritas Test Co")
ABBR = os.environ.get("VERITAS_ABBR", "VTC")
SUPPLIER = os.environ.get("VERITAS_SUPPLIER", "Acme Industrial Supply")
ITEM_CODE = os.environ.get("VERITAS_ITEM", "WIDGET-A")
WAREHOUSE = f"Stores - {ABBR}"
COST_CENTER = f"Main - {ABBR}"
CASH_ACCOUNT = f"Cash - {ABBR}"
QTY = 10
RATE = 25.0
TODAY = date.today().isoformat()


def s1_material_request(erp: ERPClient) -> str:
    name = erp.insert_and_submit(
        "Material Request",
        {
            "material_request_type": "Purchase",
            "transaction_date": TODAY,
            "schedule_date": TODAY,
            "company": COMPANY,
            "items": [
                {
                    "item_code": ITEM_CODE,
                    "qty": QTY,
                    "schedule_date": TODAY,
                    "warehouse": WAREHOUSE,
                }
            ],
        },
    )
    print(f"S1  Material Request  {name}  ({QTY} x {ITEM_CODE})")
    return name


def s3_purchase_order(erp: ERPClient, mr: str) -> str:
    po = erp.call(
        "erpnext.stock.doctype.material_request.material_request.make_purchase_order",
        source_name=mr,
    )
    po["supplier"] = SUPPLIER
    po["schedule_date"] = TODAY
    po["transaction_date"] = TODAY
    for it in po["items"]:
        it["rate"] = RATE
        it["warehouse"] = WAREHOUSE
        it["cost_center"] = COST_CENTER
    doc = erp.submit(erp.insert("Purchase Order", po))
    print(f"S3  Purchase Order    {doc['name']}  (rate {RATE}, total {doc.get('grand_total')})")
    return str(doc["name"])


def s4_receipt_and_invoice(erp: ERPClient, po: str) -> tuple[str, str]:
    pr = erp.call(
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
        source_name=po,
    )
    pr["posting_date"] = TODAY
    for it in pr["items"]:
        it["warehouse"] = WAREHOUSE
    pr_name = erp.insert_and_submit("Purchase Receipt", pr)
    print(f"S4  Purchase Receipt  {pr_name}")

    pi = erp.call(
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
        source_name=po,
    )
    pi["bill_no"] = f"ACME-{pr_name}"
    pi["bill_date"] = TODAY
    pi["posting_date"] = TODAY
    pi["update_stock"] = 0
    pi_name = erp.insert_and_submit("Purchase Invoice", pi)
    print(f"S4  Purchase Invoice  {pi_name}  (three-way match: PO={po} PR={pr_name})")
    return pr_name, pi_name


def s6_payment(erp: ERPClient, pi: str) -> str:
    pe = erp.call(
        "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
        dt="Purchase Invoice",
        dn=pi,
    )
    pe["mode_of_payment"] = "Cash"
    pe["paid_from"] = CASH_ACCOUNT
    pe["reference_no"] = f"PAY-{pi}"
    pe["reference_date"] = TODAY
    pe["posting_date"] = TODAY
    doc = erp.submit(erp.insert("Payment Entry", pe))
    print(
        f"S6  Payment Entry     {doc['name']}  (paid {doc.get('paid_amount')} from {CASH_ACCOUNT})"
    )
    return str(doc["name"])


def print_gl(erp: ERPClient, vouchers: list[str]) -> None:
    rows = erp.gl_entries(vouchers)
    if not rows:
        print("\n!! no GL entries found for", vouchers)
        return
    print(f"\nGL entries moved ({len(rows)} lines):")
    print(f"  {'voucher':<24} {'account':<34} {'debit':>12} {'credit':>12}")
    print("  " + "-" * 84)
    tot_d = tot_c = 0.0
    for r in sorted(rows, key=lambda r: (r["voucher_no"], r["account"])):
        d, c = float(r["debit"]), float(r["credit"])
        tot_d += d
        tot_c += c
        print(f"  {r['voucher_no']:<24} {r['account']:<34} {d:>12.2f} {c:>12.2f}")
    print("  " + "-" * 84)
    print(f"  {'TOTAL':<24} {'':<34} {tot_d:>12.2f} {tot_c:>12.2f}")
    balanced = abs(tot_d - tot_c) < 0.005
    print(f"\n  double-entry balances: {balanced}  (debit - credit = {tot_d - tot_c:.4f})")


def main() -> int:
    erp = ERPClient()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url} — start the stack first", file=sys.stderr)
        return 1
    print(f"auth: scoped agent API key   target: {erp.url}")
    try:
        mr = s1_material_request(erp)
        print("S2  policy check       proceed  (agent's job from Phase 2)")
        po = s3_purchase_order(erp, mr)
        pr, pi = s4_receipt_and_invoice(erp, po)
        print("S5  discrepancy        none  (happy path)")
        pe = s6_payment(erp, pi)
    except ERPError as e:
        print(f"\nbuying chain failed:\n{e}", file=sys.stderr)
        return 1
    print_gl(erp, [pi, pe])
    print(f"\nS1..S6 OK   MR={mr} PO={po} PR={pr} PI={pi} PE={pe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
