"""Request and response bodies. Validated strictly: unknown fields are refused."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: ERPNext names: letters, digits and the punctuation item codes and supplier
#: names actually use. Rejects control characters and markup outright; the agent
#: additionally quarantines every ERP string it shows a model (agent/context.py).
ERP_NAME = r"^[\w][\w .,&()/'+-]{0,139}$"


class WorkflowRequest(BaseModel):
    """One procurement request: buy ``qty`` of ``item_code`` from ``supplier``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_code: str = Field(pattern=ERP_NAME)
    qty: float = Field(gt=0, le=1_000_000)
    supplier: str = Field(pattern=ERP_NAME)
    rate: float = Field(gt=0, le=100_000_000)
    needed_by: date
    bill_no: str = Field(pattern=r"^[\w][\w./-]{0,63}$")
    tolerance_pct: float = Field(default=2.0, ge=0, le=10)
    approval_threshold: float = Field(default=10_000.0, gt=0)
    #: What arrived and what was billed, when it differs from the order. Absent
    #: means "as ordered". The three-way match at S4 compares these legs.
    received_qty: float | None = Field(default=None, ge=0, le=1_000_000)
    invoice_rate: float | None = Field(default=None, gt=0, le=100_000_000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["reject", "approve_and_resume"]
    #: Recorded verbatim in the append-only audit trail.
    reason: str = Field(min_length=10, max_length=2000)
