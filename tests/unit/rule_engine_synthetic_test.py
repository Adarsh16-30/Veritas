"""The deterministic gate: every invariant fires, and the expression language is
not a code-execution hole.

Phase 3 success criterion: "rule engine catches 100% of hard-invariant
violations". The test that actually earns that is the parametrised one below —
it walks *every* invariant in the YAML and asserts each one fails when its own
fact is flipped. Adding an invariant without a way to violate it is a test
failure, so the set cannot rot into decoration.

Synthetic facts only; the real ledger half of the gate is exercised in
`tests/integration/test_verified_gate_integration.py` (Rule 1).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.state import Step
from verify.rules import (
    InvariantError,
    InvariantSet,
    RuleReport,
    Violation,
    _coerce,
    _SafeEval,
)

# Facts that satisfy every invariant, per step. The "clean" world.
CLEAN: dict[Step, dict[str, object]] = {
    Step.S1: {
        "estimated_value": "50.00",
        "item_is_purchasable": True,
        "qty_positive": True,
        "needed_by_not_past": True,
    },
    Step.S2: {
        "estimated_total": "50.00",
        "within_approval_threshold": True,
        "headroom": "9950.00",
        "mr_submitted": True,
    },
    Step.S3: {
        "po_total": "50.00",
        "qty_matches_requisition": True,
        "qty_delta_vs_mr": "0",
        "supplier_active": True,
        "rate_positive": True,
    },
    Step.S4: {
        "qty_match": True,
        "qty_variance": "0",
        "amount_variance": "0.00",
        "amount_variance_pct": "0.000",
        "within_tolerance": True,
        "bill_no_not_previously_invoiced": True,
        "three_way_match_clean": True,
    },
    Step.S5: {
        "variance": "0.00",
        "variance_pct": "0.000",
        "within_tolerance": True,
        "no_open_discrepancy": True,
        "invoice_submitted": True,
    },
    Step.S6: {
        "outstanding": "50.00",
        "fully_invoiced": True,
        "outstanding_equals_total": True,
        "not_previously_paid": True,
        "invoice_submitted": True,
    },
}


@pytest.fixture(scope="module")
def invariants() -> InvariantSet:
    return InvariantSet.load()


def _all_invariants() -> list[tuple[Step, str]]:
    iset = InvariantSet.load()
    return [(step, inv.rule_id) for step in Step for inv in iset.for_step(step)]


def test_every_boolean_fact_is_true_in_a_clean_world() -> None:
    """The polarity convention, made enforceable.

    Every boolean DELTA fact is phrased as a check that passed, so a clean
    workflow has *all* of them True. Four facts once read the other way round
    (`already_paid`, `duplicate_bill_no`, `discrepancy_open`,
    `over_approval_threshold`) and the first real benchmark run caught the
    executor inverting one: it read `already_paid=False` on a spotless invoice
    and concluded payment was therefore impossible. The same mixed convention
    also corrupted the conformal `facts_clean` signal, which counts true
    booleans and so scored a duplicated bill as *cleaner* than a good one.

    A fixture with a False boolean in it is the tell, so assert on the fixture.
    """
    for step in Step:
        for name, value in CLEAN[step].items():
            if isinstance(value, bool):
                assert value is True, (
                    f"{step.value}.{name} is False in a clean world — it is phrased as "
                    "'True means something is wrong'. Invert the name so True means "
                    "the check passed."
                )


def test_clean_facts_violate_nothing(invariants: InvariantSet) -> None:
    for step in Step:
        violations, checked = invariants.evaluate(step, CLEAN[step])
        assert violations == [], f"{step.value} flagged a clean world: {violations}"
        assert checked, f"{step.value} has no invariants at all"


@pytest.mark.parametrize(("step", "rule_id"), _all_invariants(), ids=lambda v: str(v))
def test_every_invariant_can_be_violated(
    invariants: InvariantSet, step: Step, rule_id: str
) -> None:
    """Flip the facts this invariant reads; it must fail. No exceptions.

    This is the check that makes "100% of hard-invariant violations" mean
    something: an invariant nothing can violate would otherwise sit in the YAML
    looking like coverage.
    """
    inv = next(i for i in invariants.for_step(step) if i.rule_id == rule_id)
    facts = dict(CLEAN[step])

    # Invert every fact the expression actually reads.
    import ast as _ast

    names = {n.id for n in _ast.walk(inv.tree) if isinstance(n, _ast.Name)}
    assert names, f"{rule_id} reads no facts"
    for name in names:
        value = facts[name]
        if isinstance(value, bool):
            facts[name] = not value
        else:
            facts[name] = "0"  # zero out a numeric fact, e.g. `outstanding > 0`

    assert not inv.holds(facts), f"{rule_id} still holds after inverting {sorted(names)}"
    violations, _ = invariants.evaluate(step, facts)
    assert rule_id in {v.rule_id for v in violations}


def test_duplicate_payment_invariant_is_present_and_terminal(invariants: InvariantSet) -> None:
    """The one that moves money twice gets its own named test."""
    facts = dict(CLEAN[Step.S6]) | {"not_previously_paid": False}
    violations, _ = invariants.evaluate(Step.S6, facts)
    ids = {v.rule_id for v in violations}
    assert "S6_NOT_ALREADY_PAID" in ids
    assert RuleReport(violations=tuple(violations)).ok is False


def test_over_tolerance_invoice_is_caught(invariants: InvariantSet) -> None:
    facts = dict(CLEAN[Step.S4]) | {"within_tolerance": False, "amount_variance_pct": "11.5"}
    violations, _ = invariants.evaluate(Step.S4, facts)
    assert "S4_WITHIN_TOLERANCE" in {v.rule_id for v in violations}


def test_unknown_fact_raises_rather_than_passing(invariants: InvariantSet) -> None:
    """A typo in an invariant must not read as "no violation".

    This is the failure mode that makes a rule engine dangerous: a silent name
    miss turns a hard guard into an unconditional pass.
    """
    with pytest.raises(InvariantError, match="unknown fact"):
        invariants.evaluate(Step.S6, {"not_previously_paid": True})  # missing the rest


def test_missing_invariant_fact_is_not_silently_false(invariants: InvariantSet) -> None:
    inv = next(i for i in invariants.for_step(Step.S4) if i.rule_id == "S4_NO_DUPLICATE_BILL")
    with pytest.raises(InvariantError):
        inv.holds({"qty_match": True})


# --- the expression language is not an execution hole --------------------------
@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__",
        "[x for x in (1, 2)]",
        "qty_positive if qty_positive else False",
        "lambda: 1",
        "facts['qty_positive']",
        "qty_positive + 1",
    ],
)
def test_expression_language_refuses_anything_but_comparisons(expr: str) -> None:
    import ast as _ast

    try:
        tree = _ast.parse(expr, mode="eval")
    except SyntaxError:
        pytest.skip("not parseable as an expression at all, which is also a refusal")
    with pytest.raises(InvariantError):
        _SafeEval({"qty_positive": True}).visit(tree)


def test_numeric_facts_compare_as_decimals_not_floats() -> None:
    """`0.1 + 0.2 > 0.3` is True in binary float. Money comparisons use Decimal."""
    assert _coerce("0.30") == Decimal("0.30")
    assert isinstance(_coerce("12.5"), Decimal)
    assert _coerce(True) is True  # a bool is a bool, not Decimal(1)


def test_non_finite_fact_is_refused() -> None:
    with pytest.raises(InvariantError, match="non-finite"):
        _coerce("NaN")


def test_invariant_ids_are_unique_and_cover_every_step(invariants: InvariantSet) -> None:
    ids = [inv.rule_id for step in Step for inv in invariants.for_step(step)]
    assert len(ids) == len(set(ids))
    for step in Step:
        assert invariants.for_step(step), f"{step.value} has no hard invariants"


def test_violation_renders_for_the_trace() -> None:
    v = Violation("S6_NOT_ALREADY_PAID", "already paid", source="invariant", kind="duplicate_guard")
    assert "S6_NOT_ALREADY_PAID" in str(v)
    assert RuleReport(violations=(v,)).reason.startswith("S6_NOT_ALREADY_PAID")
