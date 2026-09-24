"""Real procurement records, and loading them into ERPNext masters (Rule 8).

Reads the cached USAspending response (never the network — see
``data/usaspending.py``), verifies it against the checksum in
``dataset_manifest.json``, and seeds the real suppliers and line items into
ERPNext so workflows run against real vendor names, real descriptions and real
contract values.

Nothing here invents a field. Where the real data is missing or cryptic — an
award with an empty description, or one whose entire description is an internal
routing code like ``IGF::OT::IGF`` — that record is carried through as-is,
because those are exactly the conditions the fault taxonomy's *missing data* and
*ambiguity* classes describe (PRD §6.3). They occur naturally in federal
procurement data; the harness selects them, it does not manufacture them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from data.usaspending import MANIFEST_PATH, RAW_PATH, canonical_bytes, sha256_of
from erp.client import ERPClient

ROOT = Path(__file__).resolve().parent.parent

#: Descriptions that are entirely internal routing codes carry no information a
#: buyer could act on. `IGF::OT::IGF` and friends are real USAspending values.
_CODE_ONLY = re.compile(r"^[\s:;,./A-Z0-9_-]*$")
_IGF = re.compile(r"IGF::[A-Z,]+::IGF")


class DatasetError(RuntimeError):
    """The cached corpus is missing or does not match its recorded checksum."""


@dataclass(frozen=True)
class AwardRecord:
    """One real federal contract award."""

    internal_id: int
    award_id: str
    supplier: str
    amount: Decimal
    description: str
    start_date: str
    end_date: str

    @property
    def item_code(self) -> str:
        """Stable, unique, and traceable back to the source record."""
        return f"USA-{self.internal_id}"

    @property
    def item_name(self) -> str:
        """The real award description, trimmed to what ERPNext accepts."""
        return self.description.strip()[:140]

    @property
    def bill_no(self) -> str:
        """A supplier bill reference derived from the award's own identifiers."""
        return f"{self.award_id}-{self.internal_id}"

    @property
    def has_description(self) -> bool:
        return bool(self.description.strip())

    @property
    def is_code_only(self) -> bool:
        """True when the description says nothing a human buyer could act on."""
        text = self.description.strip()
        if not text:
            return False
        stripped = _IGF.sub("", text).strip(" :;,./-")
        return not stripped or (len(stripped) < 12 and bool(_CODE_ONLY.match(stripped)))


def load_records(verify: bool = True) -> list[AwardRecord]:
    """Read the cached corpus and check it against its manifest checksum."""
    if not RAW_PATH.exists():
        raise DatasetError(
            f"{RAW_PATH.relative_to(ROOT)} is missing — run "
            "`uv run python -m data.usaspending` to fetch the real corpus (Rule 8)"
        )
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))

    if verify:
        if not MANIFEST_PATH.exists():
            raise DatasetError("dataset_manifest.json is missing; Rule 8 requires it")
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        actual = sha256_of(canonical_bytes(raw))
        if actual != manifest.get("sha256"):
            raise DatasetError(
                "the cached corpus does not match its manifest checksum — the evaluation "
                f"corpus has drifted.\n  manifest: {manifest.get('sha256')}\n  actual:   {actual}"
            )

    return [
        AwardRecord(
            internal_id=int(r["internal_id"]),
            award_id=str(r.get("Award ID") or "").strip(),
            supplier=str(r.get("Recipient Name") or "").strip(),
            amount=Decimal(str(r.get("Award Amount") or 0)).quantize(Decimal("0.01")),
            description=str(r.get("Description") or ""),
            start_date=str(r.get("Start Date") or ""),
            end_date=str(r.get("End Date") or ""),
        )
        for r in raw
        if r.get("Recipient Name")
    ]


# --- ERPNext master seeding ------------------------------------------------------
def ensure_supplier(erp: ERPClient, name: str) -> None:
    if erp.exists("Supplier", [["name", "=", name]]):
        return
    erp.insert(
        "Supplier",
        {
            "supplier_name": name,
            "supplier_group": "All Supplier Groups",
            "supplier_type": "Company",
        },
    )


def ensure_item(
    erp: ERPClient,
    record: AwardRecord,
    item_name: str | None = None,
    item_code: str | None = None,
) -> None:
    """Create the line item and give it the company defaults the buying chain needs.

    ``item_name`` overrides the description — used only by the harness to inject
    a fault into the item master itself, never to improve on the real data.

    ``item_code`` seeds the award under a different code. The harness uses it so
    an injected item belongs to exactly one workflow: writing a payload onto the
    award's own code poisons the item for every other workflow drawing that
    award, permanently, because the ERP keeps it between runs.
    """
    code = item_code or record.item_code
    name = item_name if item_name is not None else record.item_name
    if not erp.exists("Item", [["name", "=", code]]):
        erp.insert(
            "Item",
            {
                "item_code": code,
                # ERPNext requires a non-empty item_name; an award with no
                # description gets its own code, which is exactly as
                # uninformative as the source record is.
                "item_name": name or code,
                "description": record.description or "",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 1,
                "is_purchase_item": 1,
            },
        )
    elif item_name is not None:
        erp.update("Item", code, {"item_name": name or code})

    item = erp.get("Item", code)
    defaults: list[dict[str, Any]] = item.get("item_defaults", [])
    row = next((d for d in defaults if d.get("company") == erp.company), None)
    if row is None:
        row = {"company": erp.company}
        defaults.append(row)
    wanted = {
        "default_warehouse": erp.warehouse,
        "expense_account": _expense_account(erp),
        "buying_cost_center": erp.cost_center,
    }
    if any(row.get(k) != v for k, v in wanted.items()):
        row.update(wanted)
        erp.update("Item", code, {"item_defaults": defaults})


def _expense_account(erp: ERPClient) -> str:
    import os

    return os.environ.get("VERITAS_EXPENSE_ACCOUNT", f"Cost of Goods Sold - {erp.abbr}")


def seed(erp: ERPClient, records: list[AwardRecord]) -> dict[str, int]:
    """Idempotently load these real records into ERPNext masters."""
    suppliers = sorted({r.supplier for r in records})
    for name in suppliers:
        ensure_supplier(erp, name)
    for record in records:
        ensure_item(erp, record)
    return {"suppliers": len(suppliers), "items": len(records)}
