"""Rule 3: the verifier is independent, and the executor's rationale never reaches it.

PRD §12's checklist asks for two things: the verifier payload excludes the
rationale, and executor↔verifier correlation stays below a ceiling. The first is
tested here against the real prompt-building path — not against a hand-built
payload, which would test nothing. The second needs labelled known-wrong cases,
so the estimator is tested here and the measurement waits for Phase 4.

The LLM is stubbed (`*_synthetic_test.py`, permitted by Rule 1/Rule 2) because
what is under test is what we *send*, not what a model replies.
"""

from __future__ import annotations

import json

import pytest

from agent.state import Action, Step, StepContext, Verdict
from verify.verifier import (
    CHECKLIST,
    VERIFIER_SYSTEM,
    IndependenceSpec,
    RationaleLeak,
    Verifier,
    VerifierError,
    agreement_phi,
    assert_no_rationale_leak,
    build_verifier_payload,
    model_family,
    strip_rejection_notes,
)

EXECUTOR_MODEL = "llama3:8b-instruct-q4_K_M"
VERIFIER_MODEL = "qwen2.5:7b-instruct-q4_K_M"

RATIONALE = "The DELTA fact three_way_match_clean is true, so the invoice is safe to pay."


class ScriptedLLM:
    """Returns a fixed reply and remembers exactly what it was shown."""

    def __init__(self, reply: object, name: str = VERIFIER_MODEL) -> None:
        self.name = name
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.seen: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        return self.reply


def _ctx(step: Step = Step.S6, **kw: object) -> StepContext:
    ctx = StepContext(workflow_id="wf-test", step=step)
    ctx.step_context = (
        "STEP: S6 — release payment against the matched invoice\n"
        "purchase_invoice: ACC-PINV-2026-00007\n"
        "outstanding_amount: 50.00\n"
        "DELTA: outstanding=50.00, already_paid=False, invoice_submitted=True"
    )
    ctx.proposed_action = Action.PROCEED
    ctx.rationale = RATIONALE
    ctx.facts = {"already_paid": False, "invoice_submitted": True}
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _checks(*satisfied: bool, confidence: float = 0.8) -> dict[str, object]:
    """A well-formed grounded-checklist answer, one entry per expectation."""
    return {
        "checks": [
            {"n": i, "evidence": f"fact_{i}=True", "satisfied": ok}
            for i, ok in enumerate(satisfied, start=1)
        ],
        "confidence": confidence,
    }


def _ok_reply(**kw: object) -> dict[str, object]:
    return _checks(True, True, True) | kw


# --- the payload (PRD §9.4) ----------------------------------------------------
def test_provided_payload_excludes_rationale() -> None:
    payload = build_verifier_payload(_ctx())
    assert set(payload) == {"step_context", "proposed_action"}
    assert "rationale" not in payload


def test_rationale_text_is_not_in_the_payload_anywhere() -> None:
    """The check that matters: not just the key, the words.

    A payload could pass §9.4's assert while carrying the rationale inside
    `step_context`. This walks every string.
    """
    payload = build_verifier_payload(_ctx())
    assert_no_rationale_leak(payload, RATIONALE)


def test_a_rationale_key_anywhere_is_a_leak() -> None:
    leaky = {"step_context": "STEP: S6", "proposed_action": "proceed", "rationale": RATIONALE}
    with pytest.raises(RationaleLeak, match="'rationale' key"):
        assert_no_rationale_leak(leaky, RATIONALE)


def test_a_rationale_that_quotes_the_evidence_is_not_a_leak() -> None:
    """Regression from the first real verified run.

    The executor is told to "cite the specific DELTA fact that decided it", so a
    terse rationale is often a verbatim quote of the evidence. The evidence
    naturally contains that string. Flagging it confuses "the executor quoted its
    input" with "the executor's reasoning leaked" — opposite directions of
    causation — and made every real workflow crash at S1.
    """
    ctx = _ctx()
    ctx.rationale = "invoice_submitted=True"  # a DELTA fact, verbatim
    assert ctx.rationale in ctx.step_context  # the premise of the false positive
    assert_no_rationale_leak(build_verifier_payload(ctx), ctx.rationale)


def test_a_rationale_smuggled_into_a_non_context_field_is_still_a_leak() -> None:
    leaky = {"step_context": "STEP: S6", "proposed_action": f"proceed because {RATIONALE}"}
    with pytest.raises(RationaleLeak, match="rationale text"):
        assert_no_rationale_leak(leaky, RATIONALE)


def test_any_field_beyond_the_prd_allowlist_is_a_leak() -> None:
    """ "Just one more field" is how the executor's reasoning reaches an
    independent verifier. The payload shape is allowlisted, not blocklisted."""
    payload = {"step_context": "STEP: S6", "proposed_action": "proceed", "executor_notes": "hm"}
    with pytest.raises(RationaleLeak, match="unexpected field"):
        assert_no_rationale_leak(payload, RATIONALE)


def test_leak_is_detected_for_a_nested_rationale_key() -> None:
    leaky = {"step_context": {"inner": "x", "rationale": RATIONALE}, "proposed_action": "proceed"}
    with pytest.raises(RationaleLeak, match="rationale field"):
        assert_no_rationale_leak(leaky, RATIONALE)


# --- the prompt actually sent ---------------------------------------------------
def test_verifier_prompt_never_contains_the_executor_rationale() -> None:
    llm = ScriptedLLM(_ok_reply())
    verifier = Verifier(llm, executor_model=EXECUTOR_MODEL)
    verifier.verify(_ctx())

    system, user = llm.seen[0]
    assert RATIONALE not in user
    assert RATIONALE not in system
    for fragment in ("three_way_match_clean is true", "safe to pay"):
        assert fragment not in user


def test_verifier_prompt_carries_the_evidence_and_the_checklist() -> None:
    llm = ScriptedLLM(_ok_reply())
    Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    _, user = llm.seen[0]
    assert "DELTA:" in user
    assert "proceed" in user
    for expectation in CHECKLIST[Step.S6]:
        assert expectation in user


def test_retry_feedback_is_stripped_so_the_verifier_is_not_anchored() -> None:
    """On attempt 2 the executor's context carries the verifier's own last
    objection. Showing that back to the verifier makes it grade its own homework."""
    ctx = _ctx()
    ctx.step_context += "\nPREVIOUS ATTEMPT REJECTED: verifier_rejected: no payment has already..."
    llm = ScriptedLLM(_ok_reply())
    Verifier(llm, executor_model=EXECUTOR_MODEL).verify(ctx)
    _, user = llm.seen[0]
    assert "PREVIOUS ATTEMPT REJECTED" not in user
    assert "DELTA:" in user  # the evidence itself survives


def test_strip_rejection_notes_leaves_everything_else() -> None:
    text = "a\nPREVIOUS ATTEMPT REJECTED: x\nb"
    assert strip_rejection_notes(text) == "a\nb"


def test_framing_is_adversarial_and_differs_from_the_executor() -> None:
    from agent.executor import SYSTEM_PROMPT as EXECUTOR_SYSTEM

    assert VERIFIER_SYSTEM != EXECUTOR_SYSTEM
    # The executor picks an action; the verifier grades a checklist and never
    # sees the action vocabulary.
    assert "proceed" not in VERIFIER_SYSTEM.lower().split()
    assert "auditor" in VERIFIER_SYSTEM.lower()


# --- independence bookkeeping ---------------------------------------------------
def test_same_family_different_tag_is_not_independence() -> None:
    """`llama3:8b` and `llama3:8b-instruct-q4_K_M` are the same model."""
    spec = IndependenceSpec(EXECUTOR_MODEL, "llama3:8b", distinct_framing=False)
    assert spec.distinct_model is False
    assert spec.independent is False


def test_different_family_is_independence() -> None:
    spec = IndependenceSpec(EXECUTOR_MODEL, VERIFIER_MODEL, distinct_framing=False)
    assert spec.distinct_model is True


def test_verifier_refuses_to_be_built_without_any_independence() -> None:
    llm = ScriptedLLM(_ok_reply(), name="llama3:8b")
    with pytest.raises(VerifierError, match="Rule 3"):
        Verifier(llm, executor_model=EXECUTOR_MODEL, distinct_framing=False)


def test_model_family_ignores_the_tag() -> None:
    assert model_family("llama3:8b-instruct-q4_K_M") == model_family("llama3:8b")
    assert model_family("qwen2.5:7b") != model_family("llama3:8b")


def test_independence_is_recorded_on_every_call() -> None:
    llm = ScriptedLLM(_ok_reply())
    call = Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    assert call.independence["distinct_model"] is True
    assert call.independence["executor_model"] == EXECUTOR_MODEL
    assert call.model == VERIFIER_MODEL


# --- parsing: never coerce a bad answer into a pass -----------------------------
def test_fail_verdict_is_derived_from_the_per_expectation_checks() -> None:
    """The verdict is computed from the checks, so "pass" while marking an item
    unsatisfied is not representable."""
    llm = ScriptedLLM(_checks(True, False, True))
    call = Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    assert call.verdict.passed is False
    assert call.verdict.violated_expectations == [CHECKLIST[Step.S6][1]]


def test_an_unsatisfied_item_is_named_by_its_checklist_text_not_its_index() -> None:
    """Models answer a numbered checklist with the number. "2" cannot be fed back
    into a retry prompt or shown to a reviewer."""
    llm = ScriptedLLM(_checks(True, True, False))
    call = Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    assert call.verdict.violated_expectations == [CHECKLIST[Step.S6][2]]


def test_every_checklist_expectation_is_answerable_from_the_delta_facts() -> None:
    """The regression that a real run caught: three expectations asked about
    things the assembler never puts in the evidence. Combined with "anything you
    cannot confirm is unsatisfied", each one silently failed every clean
    workflow at that step."""
    from agent.context import ContextAssembler

    assert set(CHECKLIST) == set(Step)
    for step in Step:
        assert len(CHECKLIST[step]) >= 3, f"{step.value} checklist is too thin"
        assert step in ContextAssembler._BUILDERS


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"confidence": 0.5}),
        json.dumps({"checks": [], "confidence": 0.5}),
        json.dumps({"checks": [{"n": 1, "satisfied": True}], "confidence": 0.5}),
        json.dumps({"checks": [{"n": 1, "satisfied": "yes"}] * 3, "confidence": 0.5}),
        json.dumps({"checks": ["nope"] * 3, "confidence": 0.5}),
        json.dumps(_checks(True, True, True, confidence=1.4)),
        json.dumps({"checks": [{"n": 1, "satisfied": True}] * 3, "confidence": "high"}),
    ],
    ids=[
        "non-json",
        "not-object",
        "no-checks",
        "empty-checks",
        "partial-checklist",
        "non-boolean-satisfied",
        "check-not-an-object",
        "conf>1",
        "nan-conf",
    ],
)
def test_unusable_answers_raise_rather_than_defaulting_to_pass(reply: str) -> None:
    """An unparseable verdict is the dangerous case: read as a pass, it commits."""
    llm = ScriptedLLM(reply)
    with pytest.raises(VerifierError):
        Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())


def test_duplicate_checklist_index_cannot_silently_skip_an_expectation() -> None:
    """The gap a real run's checklist rewrite made possible.

    The only guard used to be `len(checks) == len(checklist)`. A model that
    answers {"n": 1} three times satisfies that count while never addressing
    expectations 2 and 3 — which then default to "no objection" and the step
    passes, even though two of its three checklist items were never checked.
    Position in the array is now authoritative; a declared `n` that disagrees
    with position is rejected rather than silently trusted.
    """
    exploit = {
        "checks": [
            {"n": 1, "evidence": "invoice_submitted=True", "satisfied": True},
            {"n": 1, "evidence": "invoice_submitted=True", "satisfied": True},
            {"n": 1, "evidence": "invoice_submitted=True", "satisfied": True},
        ],
        "confidence": 0.9,
    }
    llm = ScriptedLLM(exploit)
    with pytest.raises(VerifierError, match="out of order"):
        Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())


def test_checklist_answers_out_of_order_are_rejected_even_without_duplicates() -> None:
    shuffled = {
        "checks": [
            {"n": 2, "evidence": "x", "satisfied": True},
            {"n": 1, "evidence": "y", "satisfied": True},
            {"n": 3, "evidence": "z", "satisfied": True},
        ],
        "confidence": 0.9,
    }
    llm = ScriptedLLM(shuffled)
    with pytest.raises(VerifierError, match="out of order"):
        Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())


def test_a_non_integer_n_is_rejected_rather_than_silently_repositioned() -> None:
    bad_n = {
        "checks": [{"n": "one", "evidence": "x", "satisfied": True}] * 3,
        "confidence": 0.9,
    }
    llm = ScriptedLLM(bad_n)
    with pytest.raises(VerifierError, match="non-integer"):
        Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())


def test_answers_in_correct_order_still_pass() -> None:
    ordered = _checks(True, True, True)
    llm = ScriptedLLM(ordered)
    call = Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    assert call.verdict.passed is True


def test_answers_omitting_n_entirely_still_work_by_position() -> None:
    """`n` is redundant, not required — the prompt already asks for answers in
    order, and position alone is sufficient once trusted as authoritative."""
    no_n = {
        "checks": [{"evidence": "x", "satisfied": ok} for ok in (True, False, True)],
        "confidence": 0.9,
    }
    llm = ScriptedLLM(no_n)
    call = Verifier(llm, executor_model=EXECUTOR_MODEL).verify(_ctx())
    assert call.verdict.passed is False
    assert call.verdict.violated_expectations == [CHECKLIST[Step.S6][1]]


# --- METRICS §4.1 ---------------------------------------------------------------
def test_phi_is_one_when_the_verifier_rubber_stamps_the_executor() -> None:
    """A verifier that passes exactly when the executor proposes commit."""
    pairs = [(True, True)] * 10 + [(False, False)] * 10
    assert agreement_phi(pairs) == pytest.approx(1.0)


def test_phi_is_negative_when_the_verifier_systematically_disagrees() -> None:
    pairs = [(True, False)] * 10 + [(False, True)] * 10
    assert agreement_phi(pairs) == pytest.approx(-1.0)


def test_phi_is_zero_when_independent() -> None:
    pairs = [(True, True), (True, False), (False, True), (False, False)]
    assert agreement_phi(pairs) == pytest.approx(0.0)


def test_phi_is_undefined_rather_than_zero_when_a_variable_never_varies() -> None:
    """Reporting 0.0 here would claim measured independence from no evidence."""
    assert agreement_phi([(True, True), (True, False)]) is None
    assert agreement_phi([]) is None


def test_verdict_shape_matches_prd_checklist() -> None:
    v = Verdict(passed=False, violated_expectations=["x"], confidence=0.5)
    assert (v.passed, v.violated_expectations, v.confidence) == (False, ["x"], 0.5)
