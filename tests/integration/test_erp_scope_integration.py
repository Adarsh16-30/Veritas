"""Least privilege against the live ERPNext (PRD Phase 6).

The unit tests prove the local guard. This proves the other layer: that
ERPNext *itself* refuses the S1..S3 buyer identity any Payment Entry access,
so S1..S3 hold no payment scope even if the local guard had a bug. Each refusal
has a positive control beside it, so a broken key cannot pass as a refusal.

Skipped unless ERPNext is reachable and the buyer key exists
(``scripts/generate_scoped_api_key.py --buyer``). Read-only: it attempts one
Payment Entry insert as the buyer and asserts it is refused.
"""

from __future__ import annotations

import os

import pytest
import requests

from erp.client import ERPClient, ERPError
from erp.scoped import StepScopedERP, agent_client

URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080").rstrip("/")
BUYER = (os.environ.get("ERPNEXT_BUYER_API_KEY"), os.environ.get("ERPNEXT_BUYER_API_SECRET"))


def _erpnext_up() -> bool:
    try:
        return requests.get(f"{URL}/api/method/ping", timeout=3).status_code == 200
    except requests.RequestException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _erpnext_up(), reason=f"no running ERPNext at {URL}"),
    pytest.mark.skipif(not all(BUYER), reason="no buyer key (generate_scoped_api_key.py --buyer)"),
]


def _denied(e: ERPError) -> bool:
    text = str(e)
    return "-> 403" in text or "PermissionError" in text


def _buyer() -> ERPClient:
    return ERPClient(url=URL, api_key=BUYER[0], api_secret=BUYER[1])


def test_buyer_can_do_its_own_job() -> None:
    """Positive control: the key works, and covers what S1..S3 read."""
    erp = _buyer()
    erp.get_list("Material Request", fields=["name"], limit=1)
    erp.get_list("Purchase Order", fields=["name"], limit=1)
    erp.get_list("Supplier", fields=["name"], limit=1)
    erp.get_list("Item", fields=["name"], limit=1)


def test_erpnext_refuses_the_buyer_payment_entry_access() -> None:
    erp = _buyer()
    with pytest.raises(ERPError) as read:
        erp.get_list("Payment Entry", fields=["name"], limit=1)
    assert _denied(read.value), f"expected a permission refusal, got: {read.value}"

    with pytest.raises(ERPError) as write:
        erp.insert("Payment Entry", {"payment_type": "Pay", "company": erp.company})
    assert _denied(write.value), (
        "the buyer's Payment Entry insert failed, but not with a permission error -- "
        f"ERPNext may have rejected it for another reason first: {write.value}"
    )


def test_the_agent_switches_identity_per_step() -> None:
    erp = agent_client(URL)
    assert isinstance(erp, StepScopedERP)
    with erp.acting_for("S6"):
        erp.get_list("Payment Entry", fields=["name"], limit=1)  # finance identity
    with erp.acting_for("S2"), pytest.raises(ERPError) as e:
        erp.get_list("Payment Entry", fields=["name"], limit=1)  # buyer identity
    assert _denied(e.value)
