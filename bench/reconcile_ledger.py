"""Ledger reconciliation — ask the real GL what actually happened (METRICS §7).

    uv run python -m bench.reconcile_ledger --out results/ledger_reconciliation.json

Phase 4's success criterion is that reconciliation passes *or* every exception is
individually explained. This asks ERPNext directly rather than trusting the
agent's own record of what it did: the whole point of a ledger is that it is the
authority, and a benchmark that scored itself from its own logs would be marking
its own homework.

Four checks, straight from METRICS §7:

* no invoice paid twice;
* no payment exceeding the invoice it settles;
* every voucher balanced, debits equal credits;
* payments reconciling against the invoices they claim to settle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from erp.client import IDEMPOTENCY_FIELD, ERPClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = Decimal("0.005")


def _d(value: object) -> Decimal:
    return Decimal(str(value or 0))


def duplicate_payments(erp: ERPClient) -> list[dict[str, Any]]:
    """More than one submitted Payment Entry settling the same invoice."""
    rows = erp.get_list(
        "Payment Entry",
        filters=[["docstatus", "=", 1]],
        fields=["name", "reference_no", "paid_amount"],
    )
    seen: dict[str, list[str]] = {}
    for row in rows:
        ref = str(row.get("reference_no") or "").strip()
        if not ref:
            continue
        seen.setdefault(ref, []).append(str(row["name"]))
    return [
        {"reference_no": ref, "payments": names, "count": len(names)}
        for ref, names in sorted(seen.items())
        if len(names) > 1
    ]


def overpayments(erp: ERPClient) -> list[dict[str, Any]]:
    """A payment larger than the invoice it settles."""
    payments = erp.get_list(
        "Payment Entry",
        filters=[["docstatus", "=", 1]],
        fields=["name", "reference_no", "paid_amount"],
    )
    bad: list[dict[str, Any]] = []
    for row in payments:
        ref = str(row.get("reference_no") or "")
        if not ref.startswith("PAY-"):
            continue
        invoice = ref[len("PAY-") :]
        try:
            pi = erp.get("Purchase Invoice", invoice)
        except Exception:  # noqa: BLE001 - a missing invoice is itself the finding
            bad.append({"payment": row["name"], "invoice": invoice, "issue": "invoice not found"})
            continue
        paid, total = _d(row.get("paid_amount")), _d(pi.get("grand_total"))
        if paid - total > TOLERANCE:
            bad.append(
                {
                    "payment": str(row["name"]),
                    "invoice": invoice,
                    "paid": str(paid),
                    "invoice_total": str(total),
                    "excess": str(paid - total),
                    "issue": "payment exceeds invoice total",
                }
            )
    return bad


def unbalanced_vouchers(erp: ERPClient) -> list[dict[str, Any]]:
    """Double entry either holds for every voucher or the ledger is broken.

    Every GL entry is fetched, with no page limit. A truncated fetch splits
    vouchers across the cut-off and reports the half it can see as one-sided:
    this check capped at 400 rows against a 906-row ledger and produced eight
    "unbalanced vouchers" that were all, on inspection, perfectly balanced.
    A reconciliation that raises false integrity violations is worse than no
    reconciliation, because the next real one gets read as more noise.
    """
    rows = erp.get_list(
        "GL Entry",
        filters=[["is_cancelled", "=", 0]],
        fields=["voucher_no", "debit", "credit"],
        limit=0,
    )
    totals: dict[str, list[Decimal]] = {}
    for row in rows:
        voucher = str(row["voucher_no"])
        entry = totals.setdefault(voucher, [Decimal("0"), Decimal("0")])
        entry[0] += _d(row.get("debit"))
        entry[1] += _d(row.get("credit"))
    return [
        {"voucher_no": v, "debit": str(d), "credit": str(c), "delta": str(d - c)}
        for v, (d, c) in sorted(totals.items())
        if abs(d - c) > TOLERANCE
    ]


def duplicate_idempotency_keys(erp: ERPClient) -> list[dict[str, Any]]:
    """One logical action, one document — Rule 5, checked in the ledger itself."""
    findings: list[dict[str, Any]] = []
    for doctype in (
        "Material Request",
        "Purchase Order",
        "Purchase Receipt",
        "Purchase Invoice",
        "Payment Entry",
    ):
        rows = erp.get_list(
            doctype,
            filters=[["docstatus", "=", 1], [IDEMPOTENCY_FIELD, "!=", ""]],
            fields=["name", IDEMPOTENCY_FIELD],
        )
        seen: dict[str, list[str]] = {}
        for row in rows:
            key = str(row.get(IDEMPOTENCY_FIELD) or "")
            if key:
                seen.setdefault(key, []).append(str(row["name"]))
        findings.extend(
            {"doctype": doctype, "key": k[:16], "documents": names}
            for k, names in sorted(seen.items())
            if len(names) > 1
        )
    return findings


def reconcile(erp: ERPClient) -> dict[str, Any]:
    dupes = duplicate_payments(erp)
    over = overpayments(erp)
    unbalanced = unbalanced_vouchers(erp)
    key_dupes = duplicate_idempotency_keys(erp)
    bad_postings = len(dupes) + len(over)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "erp_url": erp.url,
        "company": erp.company,
        "passes": bad_postings == 0 and not unbalanced and not key_dupes,
        "bad_postings": bad_postings,
        "duplicate_payments": dupes,
        "overpayments": over,
        "unbalanced_vouchers": unbalanced,
        "duplicate_idempotency_keys": key_dupes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="results/ledger_reconciliation.json")
    args = parser.parse_args()

    load_dotenv()
    erp = ERPClient()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1

    report = reconcile(erp)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(f"ledger reconciliation: {'PASS' if report['passes'] else 'EXCEPTIONS'}")
    print(f"  bad postings (duplicate + overpayment): {report['bad_postings']}")
    print(f"  duplicate payments      : {len(report['duplicate_payments'])}")
    print(f"  overpayments            : {len(report['overpayments'])}")
    print(f"  unbalanced vouchers     : {len(report['unbalanced_vouchers'])}")
    print(f"  duplicate idempotency   : {len(report['duplicate_idempotency_keys'])}")
    print(f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0 if report["passes"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
