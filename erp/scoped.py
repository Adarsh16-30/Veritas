"""Step-scoped ERPNext access — least privilege per workflow step (PRD Phase 6).

PRD Phase 6: "Confirm the scoped service-user role; S1–S3 hold no payment
scope." Two layers, either of which stops a payment from S1–S3 on its own:

1. **ERPNext identity.** S1–S3 run on a *buyer* API key whose user holds only
   purchasing and stock roles — no Accounts role, so ERPNext itself refuses it
   Payment Entry access. S4–S6 run on the existing agent (*finance*) key.
   ``scripts/generate_scoped_api_key.py --buyer`` creates the buyer user.
2. **Local write scope.** Inside a step, only that step's own doctypes may be
   written and only its own mapping methods called; anything else raises
   ``ScopeError`` before a request leaves the process. This layer does not
   depend on ERPNext's role configuration being right.

``StepScopedERP`` is an ``ERPClient``: every caller (assembler, rule gate,
committer, state machine) keeps working unchanged, and the pipeline only wraps
each step in ``erp.acting_for(step)``. Every request goes through one
``requests.Session``, so switching identity is switching that session's
``Authorization`` header for the duration of the step.

Outside any step -- harness setup, ledger reconciliation, the state machine's
GL read-back -- the client acts as the finance identity with no write scope,
exactly as the single-identity client always has.

Opt-in: ``agent_client()`` returns a plain ``ERPClient`` unless
``ERPNEXT_BUYER_API_KEY``/``ERPNEXT_BUYER_API_SECRET`` are set, so nothing
changes for a run started before the buyer identity existed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from erp.client import ERPClient, ERPError

#: Steps that never move money. They run on the buyer identity.
BUYER_STEPS: frozenset[str] = frozenset({"S1", "S2", "S3"})

#: Doctypes each step may insert, update or submit (orchestrator/steps.py plans).
#: S2 and S5 are judgement gates and write nothing.
WRITE_SCOPE: dict[str, frozenset[str]] = {
    "S1": frozenset({"Material Request"}),
    "S2": frozenset(),
    "S3": frozenset({"Purchase Order"}),
    "S4": frozenset({"Purchase Receipt", "Purchase Invoice"}),
    "S5": frozenset(),
    "S6": frozenset({"Payment Entry"}),
}

#: Server methods each step may call (the document mappers in erp/client.py).
CALL_SCOPE: dict[str, frozenset[str]] = {
    "S1": frozenset(),
    "S2": frozenset(),
    "S3": frozenset(
        {"erpnext.stock.doctype.material_request.material_request.make_purchase_order"}
    ),
    "S4": frozenset(
        {
            "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
            "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
        }
    ),
    "S5": frozenset(),
    "S6": frozenset({"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry"}),
}


class ScopeError(ERPError):
    """A step tried to write or call outside its scope.

    An ``ERPError`` on purpose: the pipeline already treats a failed write as a
    bounded retry and then escalates (Rule 7), so a scope violation stops the
    workflow and reaches a human rather than crashing the worker.
    """


class StepScopedERP(ERPClient):
    def __init__(
        self,
        buyer_key: str,
        buyer_secret: str,
        finance_key: str,
        finance_secret: str,
        url: str | None = None,
        timeout: int = 60,
    ) -> None:
        if (buyer_key, buyer_secret) == (finance_key, finance_secret):
            raise ERPError("buyer and finance identities must be different API keys")
        super().__init__(url=url, api_key=finance_key, api_secret=finance_secret, timeout=timeout)
        self._finance_auth = f"token {finance_key}:{finance_secret}"
        self._buyer_auth = f"token {buyer_key}:{buyer_secret}"
        self._step: str | None = None

    @property
    def current_step(self) -> str | None:
        return self._step

    @property
    def identity(self) -> str:
        return "buyer" if self._step in BUYER_STEPS else "finance"

    @contextmanager
    def acting_for(self, step: str) -> Iterator[None]:
        if step not in WRITE_SCOPE:
            raise ScopeError(f"unknown step {step!r}")
        prev_step, prev_auth = self._step, self.s.headers.get("Authorization")
        self._step = step
        self.s.headers["Authorization"] = (
            self._buyer_auth if step in BUYER_STEPS else self._finance_auth
        )
        try:
            yield
        finally:
            self._step = prev_step
            if prev_auth is None:
                self.s.headers.pop("Authorization", None)
            else:
                self.s.headers["Authorization"] = prev_auth

    # --- the local write scope ---------------------------------------------------
    def _guard_write(self, doctype: str) -> None:
        if self._step is not None and doctype not in WRITE_SCOPE[self._step]:
            raise ScopeError(
                f"{self._step} may not write {doctype!r}"
                f" (allowed: {sorted(WRITE_SCOPE[self._step]) or 'nothing'})"
            )

    def insert(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
        self._guard_write(doctype)
        return super().insert(doctype, doc)

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        self._guard_write(doctype)
        return super().update(doctype, name, doc)

    def submit(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._guard_write(str(doc.get("doctype") or ""))
        return super().submit(doc)

    def call(self, dotted_path: str, **args: Any) -> Any:
        if self._step is not None and dotted_path not in CALL_SCOPE[self._step]:
            raise ScopeError(f"{self._step} may not call {dotted_path!r}")
        return super().call(dotted_path, **args)


def agent_client(url: str | None = None) -> ERPClient:
    """The client the agent should use: step-scoped when a buyer key exists."""
    buyer_key = os.environ.get("ERPNEXT_BUYER_API_KEY")
    buyer_secret = os.environ.get("ERPNEXT_BUYER_API_SECRET")
    if not (buyer_key and buyer_secret):
        return ERPClient(url=url)
    finance_key = os.environ.get("ERPNEXT_API_KEY")
    finance_secret = os.environ.get("ERPNEXT_API_SECRET")
    if not (finance_key and finance_secret):
        raise ERPError("ERPNEXT_BUYER_API_KEY is set but ERPNEXT_API_KEY/SECRET are not")
    return StepScopedERP(buyer_key, buyer_secret, finance_key, finance_secret, url=url)


def describe(erp: ERPClient) -> str:
    """One line for run logs: which access model this run is using."""
    if isinstance(erp, StepScopedERP):
        return "step-scoped (S1-S3 buyer identity, S4-S6 finance identity)"
    return "single identity"


__all__ = [
    "BUYER_STEPS",
    "CALL_SCOPE",
    "WRITE_SCOPE",
    "ScopeError",
    "StepScopedERP",
    "agent_client",
    "describe",
]
