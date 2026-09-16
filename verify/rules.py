"""RuleGate — gate 1 of 3: deterministic accounting invariants (PRD §4.1, §7 Phase 3).

Two independent sources of truth, deliberately kept apart:

1. :class:`InvariantSet` — the declarative invariants in ``invariants.yaml``
   (PRD §2.4), evaluated over the ``DELTA`` facts the ContextAssembler computed
   in Python from the real ERPNext documents.
2. :class:`LedgerGuard` — a **fresh read of the real ledger** taken at gate time,
   immediately before the commit. The assembler's facts were computed when the
   step was assembled; between then and now another worker may have paid the
   invoice or posted the bill. An invariant checked against a stale read is not
   a guard, so the duplicate-payment and duplicate-bill checks are re-asked of
   ERPNext here (Rule 1: the real system of record answers).

A violation from either source is **terminal**. It escalates and is never
retried: no number of retries makes an over-tolerance invoice within tolerance,
and retrying a duplicate-payment guard is how money gets paid twice.

Nothing in this module calls a model. It is deterministic, and that is the point
— it is the floor under the probabilistic gates that follow it.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from agent.context import WorkflowSpec, derived_key
from agent.state import Step, StepContext
from erp.client import ERPClient, ERPError

INVARIANTS_PATH = Path(__file__).with_name("invariants.yaml")


class InvariantError(RuntimeError):
    """The invariant set itself is malformed — a build error, never a verdict.

    Raised loudly rather than skipped. An invariant that silently evaluates to
    "fine" because of a typo in a fact name is worse than no invariant at all.
    """


@dataclass(frozen=True)
class Violation:
    rule_id: str
    message: str
    source: str  # "invariant" | "ledger"
    kind: str = ""

    def __str__(self) -> str:
        return f"{self.rule_id}: {self.message}"


@dataclass(frozen=True)
class RuleReport:
    """What the deterministic gate concluded, and what it actually checked."""

    violations: tuple[Violation, ...] = ()
    checked: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def reason(self) -> str:
        return "; ".join(str(v) for v in self.violations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": list(self.checked),
            "violations": [
                {"rule_id": v.rule_id, "message": v.message, "source": v.source, "kind": v.kind}
                for v in self.violations
            ],
        }


# --- the restricted expression language ---------------------------------------
#: Only these node types may appear in an invariant. No Call, no Attribute, no
#: Subscript, no comprehension: an invariant can read facts and compare them,
#: and can do nothing else.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def _coerce(value: object) -> object:
    """Facts arrive as bools or as strings holding exact Decimals.

    Booleans stay booleans. A numeric string becomes a ``Decimal`` so that
    ``outstanding > 0`` compares exactly rather than through binary float.
    Anything non-finite is refused: a NaN comparison is silently false, which
    would turn a guard into a rubber stamp.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal | int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            d = Decimal(value)
        except (InvalidOperation, ValueError):
            return value
        if not d.is_finite():
            raise InvariantError(f"non-finite numeric fact {value!r}")
        return d
    return value


class _SafeEval(ast.NodeVisitor):
    """Evaluates one invariant expression against one step's facts."""

    def __init__(self, facts: Mapping[str, object]) -> None:
        self.facts = facts

    def visit(self, node: ast.AST) -> Any:
        if not isinstance(node, _ALLOWED_NODES):
            raise InvariantError(f"{type(node).__name__} is not allowed in an invariant expression")
        return super().visit(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise InvariantError(f"{type(node).__name__} is not allowed in an invariant expression")

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return _coerce(node.value)

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self.facts:
            # A missing fact is a build error, not a passing invariant.
            raise InvariantError(f"invariant references unknown fact {node.id!r}")
        return _coerce(self.facts[node.id])

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        if not isinstance(node.op, ast.Not):
            raise InvariantError("only `not` is allowed as a unary operator")
        return not self.visit(node.operand)

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(v) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            if not _compare(op, left, right):
                return False
            left = right
        return True


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    match op:
        case ast.Eq():
            return bool(left == right)
        case ast.NotEq():
            return bool(left != right)
        case ast.Lt():
            return bool(left < right)
        case ast.LtE():
            return bool(left <= right)
        case ast.Gt():
            return bool(left > right)
        case ast.GtE():
            return bool(left >= right)
    raise InvariantError(f"comparison {type(op).__name__} is not allowed")


@dataclass(frozen=True)
class Invariant:
    rule_id: str
    step: Step
    expr: str
    message: str
    kind: str
    tree: ast.Expression

    def holds(self, facts: Mapping[str, object]) -> bool:
        return bool(_SafeEval(facts).visit(self.tree))


class InvariantSet:
    """The declarative invariants, parsed and validated once at load."""

    def __init__(self, invariants: list[Invariant]) -> None:
        self._by_step: dict[Step, list[Invariant]] = {s: [] for s in Step}
        seen: set[str] = set()
        for inv in invariants:
            if inv.rule_id in seen:
                raise InvariantError(f"duplicate invariant id {inv.rule_id!r}")
            seen.add(inv.rule_id)
            self._by_step[inv.step].append(inv)

    @classmethod
    def load(cls, path: Path | None = None) -> InvariantSet:
        raw = yaml.safe_load((path or INVARIANTS_PATH).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "invariants" not in raw:
            raise InvariantError("invariants file has no `invariants:` list")
        out: list[Invariant] = []
        for entry in raw["invariants"]:
            for required in ("id", "step", "expr", "message"):
                if required not in entry:
                    raise InvariantError(f"invariant {entry!r} is missing {required!r}")
            try:
                tree = ast.parse(entry["expr"], mode="eval")
            except SyntaxError as e:
                raise InvariantError(f"{entry['id']}: cannot parse {entry['expr']!r}: {e}") from e
            out.append(
                Invariant(
                    rule_id=str(entry["id"]),
                    step=Step(str(entry["step"])),
                    expr=str(entry["expr"]),
                    message=str(entry["message"]),
                    kind=str(entry.get("kind", "")),
                    tree=tree,
                )
            )
        return cls(out)

    def for_step(self, step: Step) -> list[Invariant]:
        return list(self._by_step[step])

    def evaluate(
        self, step: Step, facts: Mapping[str, object]
    ) -> tuple[list[Violation], list[str]]:
        violations: list[Violation] = []
        checked: list[str] = []
        for inv in self._by_step[step]:
            checked.append(inv.rule_id)
            if not inv.holds(facts):
                violations.append(
                    Violation(inv.rule_id, inv.message, source="invariant", kind=inv.kind)
                )
        return violations, checked


class LedgerGuard:
    """Re-asks the real ledger the questions that must not be answered from a
    stale read (Rule 1).

    Every check here issues its own ERPNext request at gate time. The window
    between assembling a step and committing it is exactly where a concurrent
    worker, a human in the ERPNext UI, or a resumed sibling workflow can change
    the answer.
    """

    def __init__(self, erp: ERPClient, spec: WorkflowSpec) -> None:
        self.erp = erp
        self.spec = spec

    def evaluate(self, ctx: StepContext) -> tuple[list[Violation], list[str]]:
        checker = {
            Step.S3: self._s3,
            Step.S4: self._s4,
            Step.S6: self._s6,
        }.get(ctx.step)
        if checker is None:
            return [], []
        try:
            return checker(ctx)
        except ERPError as e:
            # Unable to confirm the invariant against the ledger is a violation,
            # not a pass. The cautious direction is the only safe default here.
            return [
                Violation(
                    f"{ctx.step.value}_LEDGER_UNAVAILABLE",
                    f"could not re-verify against the ledger: {e}",
                    source="ledger",
                    kind="availability",
                )
            ], ["ledger_reachable"]

    def _s3(self, ctx: StepContext) -> tuple[list[Violation], list[str]]:
        supplier_name = self.spec.supplier
        supplier = self.erp.get("Supplier", supplier_name)
        violations: list[Violation] = []
        if supplier.get("disabled"):
            violations.append(
                Violation(
                    "S3_SUPPLIER_DISABLED_NOW",
                    f"supplier {supplier_name!r} is disabled in ERPNext as of this read",
                    source="ledger",
                    kind="vendor_validity",
                )
            )
        return violations, ["supplier_enabled_now"]

    def _s4(self, ctx: StepContext) -> tuple[list[Violation], list[str]]:
        supplier, bill_no = self.spec.supplier, self.spec.bill_no
        violations: list[Violation] = []
        if self.erp.duplicate_bill_exists(
            supplier, bill_no, exclude_key=derived_key(ctx, "invoice")
        ):
            violations.append(
                Violation(
                    "S4_DUPLICATE_BILL_NOW",
                    f"a submitted invoice for supplier bill {bill_no!r} exists as of this read",
                    source="ledger",
                    kind="duplicate_guard",
                )
            )
        return violations, ["duplicate_bill_now"]

    def _s6(self, ctx: StepContext) -> tuple[list[Violation], list[str]]:
        """The duplicate-payment guard. This is the one that moves money."""
        invoice = ctx.docs.get("S4_invoice")
        if not invoice:
            return [
                Violation(
                    "S6_NO_INVOICE",
                    "no purchase invoice is bound to this workflow; there is nothing to pay",
                    source="ledger",
                    kind="document_state",
                )
            ], ["invoice_bound"]

        violations: list[Violation] = []
        checked = ["invoice_outstanding_now", "no_prior_payment_now", "payment_account_postable"]

        pi = self.erp.get("Purchase Invoice", invoice)
        outstanding = Decimal(str(pi.get("outstanding_amount") or 0))
        status = str(pi.get("status") or "")
        if int(pi.get("docstatus", 0)) != 1:
            violations.append(
                Violation(
                    "S6_INVOICE_NOT_SUBMITTED_NOW",
                    f"invoice {invoice} is not in a submitted state as of this read",
                    source="ledger",
                    kind="document_state",
                )
            )
        if outstanding <= 0 or status == "Paid":
            violations.append(
                Violation(
                    "S6_ALREADY_SETTLED_NOW",
                    f"invoice {invoice} shows outstanding={outstanding} status={status!r} "
                    "as of this read; paying it would pay the supplier twice",
                    source="ledger",
                    kind="duplicate_guard",
                )
            )

        prior = self.erp.get_list(
            "Payment Entry",
            filters=[["reference_no", "=", f"PAY-{invoice}"], ["docstatus", "=", 1]],
            fields=["name"],
            limit=2,
        )
        if prior:
            violations.append(
                Violation(
                    "S6_PRIOR_PAYMENT_NOW",
                    f"payment entry {prior[0]['name']} already references invoice {invoice}",
                    source="ledger",
                    kind="duplicate_guard",
                )
            )

        # GL validity: the account the payment credits must be real and postable.
        account = self.erp.get("Account", self.erp.cash_account)
        if account.get("is_group"):
            violations.append(
                Violation(
                    "S6_ACCOUNT_IS_GROUP",
                    f"payment account {self.erp.cash_account!r} is a group account; "
                    "no entry can be posted to it",
                    source="ledger",
                    kind="gl_validity",
                )
            )
        if account.get("disabled") or str(account.get("freeze_account") or "No") == "Yes":
            violations.append(
                Violation(
                    "S6_ACCOUNT_FROZEN",
                    f"payment account {self.erp.cash_account!r} is disabled or frozen",
                    source="ledger",
                    kind="gl_validity",
                )
            )
        return violations, checked


class RuleGate:
    """Gate 1: declarative invariants over the assembled facts, then a fresh
    ledger re-read. Terminal on any violation."""

    def __init__(
        self, erp: ERPClient, spec: WorkflowSpec, invariants: InvariantSet | None = None
    ) -> None:
        self.invariants = invariants or InvariantSet.load()
        self.ledger = LedgerGuard(erp, spec)

    def check(self, ctx: StepContext) -> RuleReport:
        violations, checked = self.invariants.evaluate(ctx.step, ctx.facts)
        ledger_violations, ledger_checked = self.ledger.evaluate(ctx)
        return RuleReport(
            violations=tuple(violations + ledger_violations),
            checked=tuple(checked + ledger_checked),
        )
