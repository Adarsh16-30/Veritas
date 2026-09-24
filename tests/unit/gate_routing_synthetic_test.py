"""How the three gates compose into one route (PRD §4.3, Rules 3 & 7).

The ordering properties tested here are the ones that decide whether money
moves:

* a hard invariant violation is terminal and never reaches a retry;
* an unusable or failing verifier never reads as a pass;
* a commit needs *every* gate to agree, not a majority;
* both model calls — executor and verifier — come out of one workflow budget.

Stubs stand in for the ERP and the models (`*_synthetic_test.py`, permitted by
Rules 1–2). The real gate against the real ledger is
`tests/integration/test_verified_gate_integration.py`.
"""

from __future__ import annotations

import json

import pytest

from agent.state import Action, Route, Step, StepContext
from verify.conformal import (
    COMMIT,
    ESCALATE,
    FEATURES,
    CalibrationModel,
    CalibrationRecord,
    Calibrator,
    LogisticModel,
    signals,
)
from verify.gate import VerificationGate
from verify.rules import RuleReport, Violation
from verify.verifier import CHECKLIST, Verifier

EXECUTOR_MODEL = "llama3:8b-instruct-q4_K_M"


class StubRuleGate:
    """A RuleGate with a predetermined report — the rule engine has its own tests."""

    def __init__(self, violations: tuple[Violation, ...] = ()) -> None:
        self.report = RuleReport(violations=violations, checked=("stub",))
        self.calls = 0

    def check(self, ctx: StepContext) -> RuleReport:
        self.calls += 1
        return self.report


class ScriptedLLM:
    def __init__(self, reply: object, name: str = "qwen2.5:7b-instruct-q4_K_M") -> None:
        self.name = name
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


class StubBudget:
    def __init__(self, cap: int = 10) -> None:
        self._cap = cap
        self.reservations: list[str] = []

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def spent(self) -> int:
        return len(self.reservations)

    def reserve(self, purpose: str) -> bool:
        if self.spent >= self._cap:
            return False
        self.reservations.append(purpose)
        return True


def _ctx(action: Action = Action.PROCEED) -> StepContext:
    ctx = StepContext(workflow_id="wf-gate", step=Step.S6)
    ctx.step_context = "STEP: S6\nDELTA: already_paid=False, invoice_submitted=True"
    ctx.proposed_action = action
    ctx.rationale = "looks fine to me"
    ctx.facts = {"not_previously_paid": True, "invoice_submitted": True}
    return ctx


def _gate(reply: object, violations: tuple[Violation, ...] = (), calibration: object = None):
    rules = StubRuleGate(violations)
    verifier = Verifier(ScriptedLLM(reply), executor_model=EXECUTOR_MODEL)
    return VerificationGate(rules, verifier, calibration), rules, verifier


#: These replies all answer the S6 checklist, so they must be as long as it is.
#: Hard-coding the 3 items it had when these tests were written made every one
#: of them fail on the parser's length guard the moment a fourth was added,
#: before reaching the routing behaviour they exist to pin.
S6_ITEMS = len(CHECKLIST[Step.S6])


def _checks(*satisfied: bool) -> dict[str, object]:
    """A well-formed reply; unspecified expectations default to satisfied."""
    flags = list(satisfied) + [True] * (S6_ITEMS - len(satisfied))
    assert len(flags) == S6_ITEMS, "more answers than the checklist has items"
    return {
        "checks": [
            {"n": i, "evidence": f"fact_{i}", "satisfied": ok}
            for i, ok in enumerate(flags, start=1)
        ],
        "confidence": 0.9,
    }


PASS = _checks()
FAIL = _checks(True, False)
VIOLATION = (Violation("S6_NOT_ALREADY_PAID", "already paid", "invariant", "duplicate_guard"),)


# --- gate 1: deterministic rules are terminal ----------------------------------
def test_rule_violation_escalates_and_never_retries() -> None:
    """No number of retries makes an over-tolerance invoice within tolerance."""
    gate, _, _ = _gate(PASS, violations=VIOLATION)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2)
    assert outcome.route is Route.ESCALATE
    assert outcome.reason is not None and outcome.reason.startswith("hard_rule_violation")


def test_rule_violation_does_not_spend_a_model_call() -> None:
    gate, _, verifier = _gate(PASS, violations=VIOLATION)
    budget = StubBudget()
    gate.evaluate(_ctx(), budget, attempts_remaining=2)
    assert budget.spent == 0
    assert verifier.llm.calls == 0


# --- gate 2: the verifier ------------------------------------------------------
def test_verifier_failure_retries_while_attempts_remain() -> None:
    gate, _, _ = _gate(FAIL)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2)
    assert outcome.route is Route.RETRY
    assert "no payment has already been made" in (outcome.reason or "")


def test_verifier_failure_escalates_on_the_last_attempt() -> None:
    gate, _, _ = _gate(FAIL)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=0)
    assert outcome.route is Route.ESCALATE


@pytest.mark.parametrize("remaining", [0, 2])
def test_unusable_verifier_answer_is_never_read_as_a_pass(remaining: int) -> None:
    """The dangerous default: a verifier that fails to parse must not commit."""
    gate, _, _ = _gate("this is not json")
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=remaining)
    assert outcome.route is not Route.COMMIT
    assert "verifier_unusable" in (outcome.reason or "")


def test_the_verifier_call_is_captured_for_the_trace() -> None:
    gate, _, _ = _gate(PASS)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2)
    call = outcome.verifier_call
    assert call is not None
    assert call.prompt_hash and call.response_hash
    assert call.independence["distinct_model"] is True


# --- gate 3: the router --------------------------------------------------------
def test_unanimous_agreement_commits() -> None:
    gate, _, _ = _gate(PASS)
    outcome = gate.evaluate(_ctx(Action.PROCEED), StubBudget(), attempts_remaining=2)
    assert outcome.route is Route.COMMIT
    assert outcome.region is not None and outcome.region.calibrated is False


@pytest.mark.parametrize("action", [Action.HOLD, Action.ESCALATE])
def test_a_verifier_pass_cannot_override_an_executor_that_declined(action: Action) -> None:
    """The gate only ever subtracts permission. It never manufactures a commit
    the executor did not propose."""
    gate, _, _ = _gate(PASS)
    outcome = gate.evaluate(_ctx(action), StubBudget(), attempts_remaining=2)
    assert outcome.route is Route.ESCALATE


def test_signals_are_recorded_for_later_calibration() -> None:
    gate, _, _ = _gate(PASS)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2)
    assert outcome.signals["executor_proceed"] == 1.0
    assert outcome.signals["verifier_pass"] == 1.0
    assert outcome.signals["rule_violations"] == 0.0


def test_a_calibrated_gate_routes_through_the_conformal_region() -> None:
    """With a fitted model the region — not unanimity — decides."""
    import random

    rng = random.Random(11)
    records = []
    for i in range(200):
        q = rng.random()
        records.append(
            CalibrationRecord(
                workflow_id=f"cal-{i:04d}",
                step="S6",
                signals=signals(
                    executor_proceed=q > 0.3,
                    verifier_passed=q > 0.45,
                    verifier_confidence=0.9,
                    rule_violations=0,
                    facts={"a": q > 0.5, "b": q > 0.4},
                    amount_at_stake=100.0,
                    attempt=1,
                ),
                truth=COMMIT if q > 0.4 else ESCALATE,
            )
        )
    calibration = Calibrator(alpha=0.1, seed=7).fit(records)

    gate, _, _ = _gate(PASS, calibration=calibration)
    outcome = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2)
    assert outcome.region is not None
    assert outcome.region.calibrated is True
    assert outcome.region.p_commit is not None
    assert gate.calibrated is True


def _adversarial_calibration() -> CalibrationModel:
    """A calibration model that commits on bias alone, ignoring every signal.

    This is deliberately not a ``Calibrator.fit()`` output: an SGD fit's exact
    decision surface depends on which corner of feature space a test's context
    happens to land in, and asserting "the fitted model scores this as commit"
    would be asserting an accident of a specific split rather than exercising
    the floor. Constructing the model directly makes the precondition — the
    conformal layer really does say COMMIT — true by construction, for *any*
    input, which is the honest way to prove the floor (not the region) is what
    stops it. A gradient-descent fit reproducing this same gap on real labelled
    data is demonstrated separately and is what motivated the floor.
    """
    n = len(FEATURES)
    model = LogisticModel(weights=[0.0] * n, bias=50.0, mean=[0.0] * n, std=[1.0] * n)
    return CalibrationModel(
        model=model,
        alpha=0.2,
        qhat=0.5,  # score(COMMIT) ~= 0, score(ESCALATE) ~= 1 -> region == {COMMIT}
        workflow_ids=("adv-fixture",),
        n_train=1,
        n_calibration=1,
    )


def test_a_fitted_model_cannot_commit_over_a_failed_verifier() -> None:
    """Rule 3: a commit MUST pass the verifier. A calibrated router is fitted
    weights over signals with no such guarantee built in — this proves the gate
    enforces the floor the model itself does not."""
    calibration = _adversarial_calibration()
    gate, _, _ = _gate(FAIL, calibration=calibration)
    outcome = gate.evaluate(_ctx(Action.PROCEED), StubBudget(), attempts_remaining=0)

    # Precondition: the fitted model really does score this as a commit, so the
    # test is exercising the floor and not accidentally passing for free.
    assert outcome.region is not None and COMMIT in outcome.region.labels

    assert outcome.route is not Route.COMMIT
    assert outcome.route is Route.ESCALATE
    assert "floor" not in (outcome.reason or "")  # this branch keeps the verifier's own wording
    assert "no payment has already been made" in (outcome.reason or "")


def test_a_fitted_model_cannot_commit_over_an_executor_that_declined() -> None:
    """Rule 2: the agent decides whether to proceed. A calibrated router that
    overturns an explicit HOLD into a COMMIT is replacing that decision, not
    verifying it."""
    calibration = _adversarial_calibration()
    gate, _, _ = _gate(PASS, calibration=calibration)
    outcome = gate.evaluate(_ctx(Action.HOLD), StubBudget(), attempts_remaining=2)

    assert outcome.region is not None and COMMIT in outcome.region.labels
    assert outcome.route is Route.ESCALATE
    assert outcome.reason is not None and "floor" in outcome.reason
    assert "hold" in outcome.reason


def test_the_verifier_floor_still_retries_like_an_honest_verifier_rejection() -> None:
    """The floor forcing ESCALATE must not silently swallow the retry budget —
    a failed verifier is a failed verifier whether or not a fitted model would
    have committed anyway."""
    calibration = _adversarial_calibration()
    gate, _, _ = _gate(FAIL, calibration=calibration)
    outcome = gate.evaluate(_ctx(Action.PROCEED), StubBudget(), attempts_remaining=2)
    assert outcome.route is Route.RETRY
    assert "no payment has already been made" in (outcome.reason or "")


# --- Rule 7: one budget across both model calls --------------------------------
def test_verifier_draws_from_the_same_workflow_budget() -> None:
    gate, _, _ = _gate(PASS)
    budget = StubBudget()
    gate.evaluate(_ctx(), budget, attempts_remaining=2)
    assert budget.reservations == ["verifier"]


def test_exhausted_budget_escalates_instead_of_calling_the_model() -> None:
    gate, _, verifier = _gate(PASS)
    budget = StubBudget(cap=0)
    outcome = gate.evaluate(_ctx(), budget, attempts_remaining=2)
    assert outcome.route is Route.ESCALATE
    assert outcome.reason == "llm_call_budget_exhausted"
    assert verifier.llm.calls == 0


def test_outcome_serialises_for_the_attempt_record() -> None:
    gate, _, _ = _gate(PASS)
    d = gate.evaluate(_ctx(), StubBudget(), attempts_remaining=2).as_dict()
    assert d["route"] == "commit"
    assert d["verdict"]["passed"] is True
    assert d["rules"]["ok"] is True
    assert "labels" in d["region"]
