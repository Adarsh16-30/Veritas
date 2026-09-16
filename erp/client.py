"""Typed ERPNext REST client — Layer 1, the system of record (PRD §3.1, Rule 1).

Every write here goes to a real running ERPNext over its REST API, submits the
document (``docstatus = 1``) and therefore moves the real general ledger. There is
no mock path and no fixture path; ``tests/`` holds the only stand-ins.

``insert_and_submit`` returns the ERPNext document name as a ``str`` because that
is the contract ``erp.idempotent.submit_once`` (PRD §9.1, provided verbatim)
depends on.

Every submitted document is stamped with the workflow's idempotency key in the
``veritas_idempotency_key`` custom field. That stamp is what makes crash recovery
sound: if a worker dies between ERPNext accepting the submit and Postgres
recording the key, the key is still discoverable *in the ledger itself*, so the
resume path can adopt the existing document instead of posting a second one.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any, cast

import requests

IDEMPOTENCY_FIELD = "veritas_idempotency_key"

#: DocTypes the agent submits, in buying-chain order.
BUYING_CHAIN: tuple[str, ...] = (
    "Material Request",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Payment Entry",
)


class ERPError(RuntimeError):
    """An ERPNext call failed. Carries the server's message for the trace."""


class ERPClient:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: int = 60,
        as_admin: bool = False,
    ) -> None:
        self.url = (url or os.environ.get("ERPNEXT_URL", "http://localhost:8080")).rstrip("/")
        self.timeout = timeout
        self.company = os.environ.get("VERITAS_COMPANY", "Veritas Test Co")
        self.abbr = os.environ.get("VERITAS_ABBR", "VTC")
        self.warehouse = os.environ.get("VERITAS_WAREHOUSE", f"Stores - {self.abbr}")
        self.cost_center = os.environ.get("VERITAS_COST_CENTER", f"Main - {self.abbr}")
        self.cash_account = os.environ.get("VERITAS_CASH_ACCOUNT", f"Cash - {self.abbr}")

        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        key = api_key or os.environ.get("ERPNEXT_API_KEY")
        secret = api_secret or os.environ.get("ERPNEXT_API_SECRET")
        # `as_admin` is for setup scripts only (creating Custom Fields needs
        # System Manager). The agent itself always runs on the scoped key, and
        # never falls back to Administrator when that key is missing.
        if as_admin:
            self._login_admin()
        elif key and secret:
            self.s.headers["Authorization"] = f"token {key}:{secret}"
        else:
            raise ERPError(
                "no scoped ERPNext credentials — set ERPNEXT_API_KEY/ERPNEXT_API_SECRET "
                "(scripts/generate_scoped_api_key.py), or pass as_admin=True for setup"
            )

    @classmethod
    def as_administrator(cls, url: str | None = None, timeout: int = 60) -> ERPClient:
        return cls(url=url, timeout=timeout, as_admin=True)

    def _login_admin(self) -> None:
        pwd = os.environ.get("ERPNEXT_ADMIN_PASSWORD", "admin")
        r = self.s.post(
            f"{self.url}/api/method/login",
            data={"usr": "Administrator", "pwd": pwd},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise ERPError(f"admin login failed: {r.status_code} {r.text[:300]}")

    # --- plumbing --------------------------------------------------------------
    def _check(self, r: requests.Response) -> Any:
        if not r.ok:
            raise ERPError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:700]}")
        try:
            body = r.json()
        except ValueError:
            return r.text
        return body.get("data", body.get("message", body))

    def ping(self) -> bool:
        try:
            return self.s.get(f"{self.url}/api/method/ping", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._check(
                self.s.get(f"{self.url}/api/resource/{doctype}/{name}", timeout=self.timeout)
            ),
        )

    def get_list(
        self,
        doctype: str,
        filters: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = json.dumps(filters)
        if fields:
            params["fields"] = json.dumps(fields)
        return cast(
            "list[dict[str, Any]]",
            self._check(
                self.s.get(
                    f"{self.url}/api/resource/{doctype}", params=params, timeout=self.timeout
                )
            ),
        )

    def exists(self, doctype: str, filters: list[Any]) -> str | None:
        rows = self.get_list(doctype, filters=filters, fields=["name"], limit=1)
        return str(rows[0]["name"]) if rows else None

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._check(
                self.s.put(
                    f"{self.url}/api/resource/{doctype}/{name}",
                    data={"data": json.dumps(doc)},
                    timeout=self.timeout,
                )
            ),
        )

    def insert(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._check(
                self.s.post(
                    f"{self.url}/api/resource/{doctype}",
                    data={"data": json.dumps({**doc, "doctype": doctype})},
                    timeout=self.timeout,
                )
            ),
        )

    def submit(self, doc: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._check(
                self.s.post(
                    f"{self.url}/api/method/frappe.client.submit",
                    data={"doc": json.dumps(doc)},
                    timeout=self.timeout,
                )
            ),
        )

    def call(self, dotted_path: str, **args: Any) -> Any:
        payload = {k: (json.dumps(v) if isinstance(v, list | dict) else v) for k, v in args.items()}
        return self._check(
            self.s.post(f"{self.url}/api/method/{dotted_path}", data=payload, timeout=self.timeout)
        )

    # --- the one write primitive ------------------------------------------------
    def insert_and_submit(self, doctype: str, doc: dict[str, Any]) -> str:
        """Insert then submit; return the ERPNext document name.

        The ``str`` return type is required by ``erp.idempotent.submit_once``.
        """
        created = self.insert(doctype, doc)
        submitted = self.submit(created)
        name = submitted.get("name") or created.get("name")
        if not name:
            raise ERPError(f"{doctype}: submit returned no document name")
        return str(name)

    def find_by_idempotency_key(self, doctype: str, key: str) -> str | None:
        """Find an already-submitted document carrying this idempotency key.

        This is the crash-recovery read (Rule 6): it closes the window between
        ERPNext accepting a submit and Postgres recording the key.
        """
        rows = self.get_list(
            doctype,
            filters=[[IDEMPOTENCY_FIELD, "=", key]],
            fields=["name", "docstatus"],
            limit=2,
        )
        for r in rows:
            if int(r.get("docstatus", 0)) == 1:
                return str(r["name"])
        return str(rows[0]["name"]) if rows else None

    # --- ledger reads -----------------------------------------------------------
    def gl_entries(self, voucher_nos: list[str]) -> list[dict[str, Any]]:
        if not voucher_nos:
            return []
        return self.get_list(
            "GL Entry",
            filters=[["voucher_no", "in", voucher_nos], ["is_cancelled", "=", 0]],
            fields=["posting_date", "voucher_type", "voucher_no", "account", "debit", "credit"],
        )

    def gl_effect(self, voucher_no: str) -> dict[str, Any]:
        """Summarise what a submitted document did to the ledger."""
        rows: list[dict[str, Any]] = self.gl_entries([voucher_no])
        debit = sum((Decimal(str(r["debit"])) for r in rows), Decimal("0"))
        credit = sum((Decimal(str(r["credit"])) for r in rows), Decimal("0"))
        return {
            "voucher_no": voucher_no,
            "lines": len(rows),
            "debit": str(debit),
            "credit": str(credit),
            "balanced": abs(debit - credit) < Decimal("0.005"),
            "accounts": sorted({r["account"] for r in rows}),
        }

    # --- buying chain ------------------------------------------------------------
    def material_request_doc(
        self, item_code: str, qty: float, schedule_date: str, key: str
    ) -> dict[str, Any]:
        return {
            "material_request_type": "Purchase",
            "transaction_date": schedule_date,
            "schedule_date": schedule_date,
            "company": self.company,
            IDEMPOTENCY_FIELD: key,
            "items": [
                {
                    "item_code": item_code,
                    "qty": qty,
                    "schedule_date": schedule_date,
                    "warehouse": self.warehouse,
                }
            ],
        }

    def purchase_order_doc(
        self, material_request: str, supplier: str, rate: float, today: str, key: str
    ) -> dict[str, Any]:
        po: dict[str, Any] = self.call(
            "erpnext.stock.doctype.material_request.material_request.make_purchase_order",
            source_name=material_request,
        )
        po["supplier"] = supplier
        po["transaction_date"] = today
        po["schedule_date"] = today
        po[IDEMPOTENCY_FIELD] = key
        for it in po["items"]:
            it["rate"] = rate
            it["warehouse"] = self.warehouse
            it["cost_center"] = self.cost_center
        return po

    def purchase_receipt_doc(
        self, purchase_order: str, today: str, key: str, received_qty: float | None = None
    ) -> dict[str, Any]:
        """Build the receipt for a purchase order.

        ``received_qty`` posts a delivery that differs from what was ordered — a
        real short or over delivery, which is the condition a three-way match
        exists to catch. ``None`` receives exactly what was ordered.
        """
        pr: dict[str, Any] = self.call(
            "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
            source_name=purchase_order,
        )
        pr["posting_date"] = today
        pr[IDEMPOTENCY_FIELD] = key
        for it in pr["items"]:
            it["warehouse"] = self.warehouse
            if received_qty is not None:
                it["qty"] = received_qty
                it["received_qty"] = received_qty
        return pr

    def purchase_invoice_doc(
        self,
        purchase_order: str,
        bill_no: str,
        today: str,
        key: str,
        invoiced_qty: float | None = None,
        invoiced_rate: float | None = None,
    ) -> dict[str, Any]:
        """Build the supplier invoice for a purchase order.

        ``invoiced_qty`` / ``invoiced_rate`` post an invoice that disagrees with
        the order — a real overbill or short bill. ``None`` bills exactly the
        ordered quantity at the agreed rate.
        """
        pi: dict[str, Any] = self.call(
            "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
            source_name=purchase_order,
        )
        pi["bill_no"] = bill_no
        pi["bill_date"] = today
        pi["posting_date"] = today
        pi["update_stock"] = 0
        pi[IDEMPOTENCY_FIELD] = key
        for it in pi.get("items", []):
            if invoiced_qty is not None:
                it["qty"] = invoiced_qty
            if invoiced_rate is not None:
                it["rate"] = invoiced_rate
                it.pop("price_list_rate", None)
                it.pop("amount", None)
        return pi

    def payment_entry_doc(self, purchase_invoice: str, today: str, key: str) -> dict[str, Any]:
        pe: dict[str, Any] = self.call(
            "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
            dt="Purchase Invoice",
            dn=purchase_invoice,
        )
        pe["mode_of_payment"] = "Cash"
        pe["paid_from"] = self.cash_account
        pe["reference_no"] = f"PAY-{purchase_invoice}"
        pe["reference_date"] = today
        pe["posting_date"] = today
        pe[IDEMPOTENCY_FIELD] = key
        return pe

    # --- reads the context assembler needs ---------------------------------------
    def duplicate_bill_exists(
        self, supplier: str, bill_no: str, exclude: str = "", exclude_key: str = ""
    ) -> bool:
        """Has this supplier bill number already been invoiced by someone else?

        ``exclude_key`` is load-bearing for crash recovery. If a worker died
        after ERPNext accepted our Purchase Invoice but before Postgres recorded
        the key, the resumed step re-enters S4 and finds *its own* orphaned
        invoice sitting there under the same bill number. Counting that as a
        duplicate turns a recoverable crash into a human escalation and stops
        ``Pipeline._adopt_orphan`` from ever adopting the document (Rule 6). A
        duplicate is another party's invoice for the same bill — never our own
        half-committed write, which is identified by its idempotency key.
        """
        rows = self.get_list(
            "Purchase Invoice",
            filters=[
                ["supplier", "=", supplier],
                ["bill_no", "=", bill_no],
                ["docstatus", "=", 1],
                ["name", "!=", exclude or "__none__"],
            ],
            fields=["name", IDEMPOTENCY_FIELD],
            limit=5,
        )
        return any(str(r.get(IDEMPOTENCY_FIELD) or "") != exclude_key for r in rows)
