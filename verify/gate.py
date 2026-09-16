"""VerificationGate — the three gates composed, in PRD §4.3 order.

    rule_gate(ctx)            deterministic invariants + a fresh ledger read
      -> verify_independently(ctx)   different model, rationale withheld (Rule 3)
      -> calibrate(ctx)              conformal prediction region
      -> route(region)               commit / retry / escalate (PRD §9.3)

Ordering is deliberate and matches the PRD: a hard invariant violation returns
before the verifier is called. It is terminal — retrying cannot make an
over-tolerance invoice within tolerance — and spending a model call to confirm
what arithmetic already settled is waste. The cost is that rule-caught cases
produce no verifier datapoint; that is noted in ``docs/limitations.md`` where the
executor↔verifier correlation is reported.

Only the *verifier* can ask for a retry, and only while attempts remain: its
``violated_expectations`` are concrete enough to feed back into the next prompt.
An ambiguous conformal region never retries — it goes to a human, because
re-rolling a model until the region narrows is how you launder a coin flip into
a decision.

This object is what separates the ``baseline`` and ``verified`` configurations of
PRD §5.2. The pipeline is the same code in both; ``gate=None`` is the Phase 2
denominator, which is why none of this logic may leak into ``agent/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from agent.context import WorkflowSpec
from agent.executor import LLM, OllamaLLM
from agent.state import Action, Region, Route, StepContext, Verdict
from erp.client import ERPClient
from orchestrator.budget import LlmBudget
from verify.conformal import CalibrationModel, route, signals, unanimous_region
from verify.rules import RuleGate, RuleReport
from verify.verifier import Verifier, VerifierCall, VerifierError, model_family

#: Verifier models to prefer, best first. Every one is a different family from
#: the executor's llama3 (Rule 3a) — two tags of the same weights agreeing with
#: each other is not independent verification. Overridable with VERIFIER_MODEL.
VERIFIER_PREFERENCES: tuple[str, ...] = (
    "qwen2.5:7b-instruct-q4_K_M",
    "qwen2.5-coder:7b",
    "mistral:7b-instruct",
    "phi3:medium",
)

#: Where `scripts/calibrate.py` writes the fitted artifact. Absent until a
#: labelled, benchmark-disjoint split exists (Rule 9).
DEFAULT_CALIBRATION_PATH = Path("results/calibration.json")


@dataclass
class GateOutcome:
    """Everything the gate concluded, kept whole for the trace and for Phase 4."""

    route: Route
    reason: str | None = None
    rule_report: RuleReport = field(default_factory=RuleReport)
    verdict: Verdict | None = None
    region: Region | None = None
    signals: dict[str, float] = field(default_factory=dict)
    verifier_call: VerifierCall | None = None

    @property
    def confidence(self) -> float | None:
        return self.verdict.confidence if self.verdict else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.value,
            "reason": self.reason,
            "rules": self.rule_report.as_dict(),
            "verdict": (
                None
                if self.verdict is None
                else {
                    "passed": self.verdict.passed,
                    "violated_expectations": list(self.verdict.violated_expectations),
                    "confidence": self.verdict.confidence,
                }
            ),
            "region": None if self.region is None else self.region.as_dict(),
            "signals": dict(self.signals),
            # `step_attempts.latency_ms` is the *executor* call alone. With two
            # model calls per attempt that number no longer describes the step,
            # so the verifier's own cost is recorded here rather than silently
            # folded into or omitted from the other column.
            "verifier_latency_ms": (
                None if self.verifier_call is None else self.verifier_call.latency_ms
            ),
            "verifier_model": (None if self.verifier_call is None else self.verifier_call.model),
        }


class VerificationGate:
    def __init__(
        self,
        rule_gate: RuleGate,
        verifier: Verifier,
        calibration: CalibrationModel | None = None,
    ) -> None:
        self.rule_gate = rule_gate
        self.verifier = verifier
        self.calibration = calibration

    @property
    def calibrated(self) -> bool:
        return self.calibration is not None

    def evaluate(
        self,
        ctx: StepContext,
        budget: LlmBudget,
        attempts_remaining: int,
    ) -> GateOutcome:
        # --- gate 1: deterministic invariants (terminal on violation) ----------
        report = self.rule_gate.check(ctx)
        if not report.ok:
            return GateOutcome(
                route=Route.ESCALATE,
                reason=f"hard_rule_violation: {report.reason}",
                rule_report=report,
            )

        # --- gate 2: the independent verifier (Rule 3) -------------------------
        if not budget.reserve("verifier"):
            return GateOutcome(
                route=Route.ESCALATE,
                reason="llm_call_budget_exhausted",
                rule_report=report,
            )
        try:
            call = self.verifier.verify(ctx)
        except VerifierError as e:
            # An unusable verdict is never read as a pass. Retry while attempts
            # remain, then hand it to a human.
            if attempts_remaining > 0:
                return GateOutcome(
                    route=Route.RETRY, reason=f"verifier_unusable: {e}", rule_report=report
                )
            return GateOutcome(
                route=Route.ESCALATE, reason=f"verifier_unusable: {e}", rule_report=report
            )

        verdict = call.verdict
        proceed = ctx.proposed_action is Action.PROCEED

        # --- gate 3: conformal region -> route (PRD §9.3) ----------------------
        sig = signals(
            executor_proceed=proceed,
            verifier_passed=verdict.passed,
            verifier_confidence=verdict.confidence or 0.0,
            rule_violations=len(report.violations),
            facts=ctx.facts,
            amount_at_stake=ctx.amount_at_stake,
            attempt=ctx.attempt,
        )
        region = (
            self.calibration.region(sig)
            if self.calibration is not None
            else unanimous_region(
                rule_ok=report.ok, verifier_passed=verdict.passed, executor_proceed=proceed
            )
        )

        decided = route(region)

        # Two floors no downstream probabilistic gate may cross, calibrated or
        # not. The uncalibrated path (`unanimous_region`) enforces both
        # structurally, by construction — but a *fitted* calibration model is
        # just learned weights over signals, with no such guarantee built in. A
        # model trained where these two signals happen to carry little weight
        # can commit despite them; a synthetic fit reproduces this in one shot.
        # Rule 3 says a commit MUST pass the verifier — not "usually pass, per a
        # model's own judgment of how much that matters". And Rule 2's whole
        # premise is that the agent decides whether to proceed; a downstream
        # router that overturns an explicit HOLD/ESCALATE into a COMMIT is not
        # verifying that decision, it is replacing it. `region` is left as the
        # model actually computed it — Phase 4's calibration curves need the
        # honest region, not one edited to match the floor — only the route the
        # pipeline acts on is forced.
        #
        # Forcing `decided` here and nothing else lets both floors fall through
        # to the ordinary retry/escalate logic below rather than duplicating it:
        # a floor triggered by a failed verdict is still just "the verifier
        # rejected this", eligible for the same retry the verifier would have
        # earned on its own; a floor triggered by executor refusal already had
        # no retry path even in the uncalibrated case (there is no concrete
        # objection to feed back when the verifier itself agreed).
        executor_floor = decided is Route.COMMIT and not proceed
        if decided is Route.COMMIT and (not verdict.passed or executor_floor):
            decided = Route.ESCALATE

        outcome = GateOutcome(
            route=decided,
            rule_report=report,
            verdict=verdict,
            region=region,
            signals=sig,
            verifier_call=call,
        )
        if decided is Route.COMMIT:
            return outcome

        # Not a commit. A named verifier objection is actionable feedback, so
        # retry on it while attempts remain; anything else goes to a human.
        if not verdict.passed and attempts_remaining > 0:
            outcome.route = Route.RETRY
            outcome.reason = "verifier_rejected: " + "; ".join(verdict.violated_expectations)
            return outcome

        if executor_floor:
            outcome.reason = (
                f"floor: calibrated region scored commit despite executor "
                f"{ctx.proposed_action.value if ctx.proposed_action else '?'}; "
                "a downstream router may not override the agent's own refusal"
            )
        else:
            outcome.reason = (
                "verifier_rejected: " + "; ".join(verdict.violated_expectations)
                if not verdict.passed
                else "low_confidence: region " + ",".join(sorted(region.labels))
            )
        return outcome


def select_verifier_model(executor_model: str, url: str | None = None) -> str:
    """Pick an installed model whose family differs from the executor's.

    Pinning one tag makes the gate unbuildable on a host that has a different
    but equally independent model installed; taking whatever is installed risks
    silently picking a second llama3. This does neither: it walks a preference
    list, skips anything of the executor's own family, and raises with the
    installed list if nothing qualifies. ``VERIFIER_MODEL`` bypasses the search
    but not ``Verifier``'s independence check.
    """
    override = os.environ.get("VERIFIER_MODEL")
    if override:
        return override

    base = (url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
    try:
        r = requests.get(f"{base}/api/tags", timeout=10)
        r.raise_for_status()
        installed = [str(m["name"]) for m in r.json().get("models", [])]
    except (requests.RequestException, ValueError, KeyError) as e:
        raise VerifierError(f"cannot list models at {base} to choose a verifier: {e}") from e

    family = model_family(executor_model)
    for preferred in VERIFIER_PREFERENCES:
        if preferred in installed and model_family(preferred) != family:
            return preferred
    for name in installed:
        if model_family(name) != family:
            return name
    raise VerifierError(
        "Rule 3: no installed model is from a different family than the executor "
        f"({executor_model!r}). Installed: {installed}. Pull one (e.g. "
        f"`ollama pull {VERIFIER_PREFERENCES[0]}`) or set VERIFIER_MODEL."
    )


def build_gate(
    erp: ERPClient,
    spec: WorkflowSpec,
    executor_model: str,
    verifier_llm: LLM | None = None,
    calibration_path: Path | str | None = None,
) -> VerificationGate:
    """Assemble the three gates.

    The verifier defaults to a different model *family* from the executor
    (``VERIFIER_MODEL``), because two tags of the same weights agreeing with each
    other is not independent verification. ``Verifier`` refuses outright if the
    result is neither a different family nor a distinctly different framing.

    ``calibration_path`` is optional and, when absent, the gate runs uncalibrated
    and says so — see ``verify.conformal.unanimous_region``. It never silently
    substitutes a hand-picked threshold for a fitted one.
    """
    verifier_llm = verifier_llm or OllamaLLM(model=select_verifier_model(executor_model))
    calibration: CalibrationModel | None = None
    path = Path(calibration_path) if calibration_path else DEFAULT_CALIBRATION_PATH
    if path.exists():
        calibration = CalibrationModel.load(path)
    return VerificationGate(
        rule_gate=RuleGate(erp, spec),
        verifier=Verifier(verifier_llm, executor_model=executor_model),
        calibration=calibration,
    )
