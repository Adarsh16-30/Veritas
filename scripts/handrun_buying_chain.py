"""Phase 1 success check: one command -> a full S1..S6 document chain in a real
ERPNext instance, all submitted, then print the GL entries it moved.

    uv run python scripts/handrun_buying_chain.py

S1 Material Request (Purchase)  -> submit
S2 (policy check — trivially "proceed" here; it becomes the agent's job in Phase 2)
S3 Purchase Order               -> submit
S4 Purchase Receipt + Purchase Invoice (three-way match point) -> submit
S5 (discrepancy resolution — none on the happy path)
S6 Payment Entry               -> submit

Uses the scoped agent API key from .env if present, else Administrator. Every
call hits the real REST API and moves the real GL (Rule 1). This script is
replaced by erp/ + agent/ in Phase 2.
"""

from __future__ import annotations

import os
import sys
from datetime import date

from _erpclient import ERP, ERPError

COMPANY = os.environ.get("VERITAS_COMPANY", "Veritas Test Co")
ABBR = os.environ.get("VERITAS_ABBR", "VTC")
SUPPLIER = "Acme Industrial Supply"
ITEM_CODE = "WIDGET-A"
WAREHOUSE = f"Stores - {ABBR}"
COST_CENTER = f"Main - {ABBR}"
CASH_ACCOUNT = f"Cash - {ABBR}"
QTY = 10
RATE = 25.0
TODAY = date.today().isoformat()


def connect() -> ERP:
    erp = ERP()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url} — start the stack first", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("ERPNEXT_API_KEY") and os.environ.get("ERPNEXT_API_SECRET"):
        erp.use_api_key()
        print(f"auth: scoped agent API key   target: {erp.url}")
    else:
        erp.login_admin()
        print(f"auth: Administrator (no scoped key in .env yet)   target: {erp.url}")
    return erp


def s1_material_request(erp: ERP) -> str:
    doc = erp.insert_and_submit(
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
    print(f"S1  Material Request  {doc['name']}  ({QTY} x {ITEM_CODE})")
    return doc["name"]


def s3_purchase_order(erp: ERP, mr: str) -> str:
    po = erp.method(
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
    return doc["name"]


def s4_receipt_and_invoice(erp: ERP, po: str) -> tuple[str, str]:
    pr = erp.method(
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
        source_name=po,
    )
    pr["posting_date"] = TODAY
    for it in pr["items"]:
        it["warehouse"] = WAREHOUSE
    pr_doc = erp.submit(erp.insert("Purchase Receipt", pr))
    print(f"S4  Purchase Receipt  {pr_doc['name']}")

    pi = erp.method(
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
        source_name=po,
    )
    pi["bill_no"] = f"ACME-{pr_doc['name']}"
    pi["bill_date"] = TODAY
    pi["posting_date"] = TODAY
    pi["update_stock"] = 0
    pi_doc = erp.submit(erp.insert("Purchase Invoice", pi))
    print(f"S4  Purchase Invoice  {pi_doc['name']}  (three-way match: PO={po} PR={pr_doc['name']})")
    return pr_doc["name"], pi_doc["name"]


def s6_payment(erp: ERP, pi: str) -> str:
    pe = erp.method(
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
    return doc["name"]


def print_gl(erp: ERP, vouchers: list[str]) -> None:
    rows = erp.list(
        "GL Entry",
        filters=[["voucher_no", "in", vouchers]],
        fields=[
            "posting_date",
            "voucher_type",
            "voucher_no",
            "account",
            "debit",
            "credit",
            "against",
        ],
    )
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
    erp = connect()
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
