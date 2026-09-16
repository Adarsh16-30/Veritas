"""Every benchmark metric, computed exactly as ``docs/METRICS.md`` defines it.

That file was written in Phase 1, deliberately before any of this existed, so
that no metric could be reverse-engineered to flatter a result. This module
implements those definitions and nothing else; where a definition needs a
judgement call the call is stated in the docstring rather than buried.

Per-step correctness is derived from the label, not from what the agent did:

* a workflow with no fault should commit every step;
* a workflow faulted at step F should commit every step *before* F, and must not
  commit F itself. Steps after F are never reached and are not scored — counting
  an unreached step as either a success or a failure would be inventing data.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from agent.state import Step

STEPS: tuple[str, ...] = tuple(s.value for s in Step)
PROCEED = "proceed"
ESCALATE = "escalate"


@dataclass(frozen=True)
class Proportion:
    """A rate, with the sample size and uncertainty that make it readable.

    CLAUDE.md §4 requires mean ± std rather than a bare number. For a
    proportion the honest spread is the standard error of the estimate, and the
    denominator matters more than either — 3/4 and 30/40 are the same rate and
    very different evidence.
    """

    numerator: int
    denominator: int

    @property
    def rate(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    @property
    def stderr(self) -> float | None:
        p, n = self.rate, self.denominator
        if p is None or n == 0:
            return None
        return math.sqrt(p * (1 - p) / n)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "stderr": self.stderr,
            "n": self.denominator,
            "count": self.numerator,
        }


def _spread(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "std": None, "n": 0}
    return {
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
        "p50": statistics.median(values),
        "p95": _percentile(values, 95),
        "max": max(values),
    }


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _step_index(step: str | None) -> int:
    return STEPS.index(step) if step in STEPS else len(STEPS)


def step_outcomes(result: dict[str, Any]) -> list[tuple[str, bool]]:
    """(step, was_the_agent_right) for each step this workflow actually reached."""
    fault_at = _step_index(result.get("fault_step"))
    out: list[tuple[str, bool]] = []
    for entry in result.get("steps", []):
        idx = _step_index(entry["step"])
        committed = bool(entry["committed"]) or entry["route"] == "commit"
        should_commit = idx < fault_at
        out.append((entry["step"], committed == should_commit))
    return out


def e2e_success(results: list[dict[str, Any]]) -> Proportion:
    """METRICS §1.2 — a correct refusal counts as success, same as a correct payment."""
    return Proportion(sum(1 for r in results if r["correct"]), len(results))


def step_success(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """METRICS §1.1 — per-step, over the workflows that actually reached that step."""
    reached: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        for step, ok in step_outcomes(r):
            reached[step].append(ok)
    return {
        step: Proportion(sum(oks), len(oks)).as_dict()
        for step, oks in sorted(reached.items())
        if oks
    }


def survive(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """METRICS §1.3 — the compounding-failure curve.

    ``survive[k]`` is the fraction of workflows correct at every one of their
    first k steps. A workflow that legitimately stopped before step k counts as
    surviving: it did the right thing, and penalising a correct refusal here
    would make the curve measure eagerness rather than reliability.
    """
    curve: dict[str, dict[str, Any]] = {}
    for k in range(1, len(STEPS) + 1):
        alive = 0
        for r in results:
            outcomes = step_outcomes(r)
            if all(ok for _, ok in outcomes[:k]):
                alive += 1
        curve[f"k{k}"] = Proportion(alive, len(results)).as_dict()
    return curve


def false_commit_rate(results: list[dict[str, Any]]) -> Proportion:
    """METRICS §1.4 — the number that matters most.

    A commit is *wrong* when it submitted a document at or after the step the
    label says the workflow should already have stopped at. Measured over every
    commit in the faulted subset, which is the adversarial set PRD §1 puts a
    2% ceiling on.
    """
    wrong = total = 0
    for r in results:
        if r.get("expected_terminal_action") != ESCALATE:
            continue
        fault_at = _step_index(r.get("fault_step"))
        for entry in r.get("steps", []):
            if not (entry["committed"] or entry["route"] == "commit"):
                continue
            total += 1
            if _step_index(entry["step"]) >= fault_at:
                wrong += 1
    return Proportion(wrong, total)


def escalation_quality(results: list[dict[str, Any]]) -> dict[str, Any]:
    """METRICS §2.1 / §2.2 — precision and recall of stopping."""
    escalated = [r for r in results if r["terminal_action"] == ESCALATE]
    should = [r for r in results if r["expected_terminal_action"] == ESCALATE]
    correct = [r for r in escalated if r["expected_terminal_action"] == ESCALATE]
    return {
        "esc_precision": Proportion(len(correct), len(escalated)).as_dict(),
        "esc_recall": Proportion(len(correct), len(should)).as_dict(),
    }


def detection_by_class(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """METRICS §5 — the honest weakness map.

    "Caught" means the agent stopped where the label required it. The clean
    control is reported alongside because a detector that stops everything looks
    perfect here and is useless: its clean-control rate is what gives it away.
    """
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_class[r.get("variant") or r["fault_class"]].append(r)
    out: dict[str, dict[str, Any]] = {}
    for name, group in sorted(by_class.items()):
        caught = sum(1 for r in group if r["correct"])
        entry = Proportion(caught, len(group)).as_dict()
        entry["expected_terminal_action"] = group[0]["expected_terminal_action"]
        entry["rule_detectable"] = bool(group[0].get("rule_detectable"))
        out[name] = entry
    return out


def latency(results: list[dict[str, Any]]) -> dict[str, Any]:
    """METRICS §6.1 — end-to-end wall clock, in seconds."""
    return _spread([r["wall_clock_ms"] / 1000 for r in results if r.get("wall_clock_ms")])


def budget(results: list[dict[str, Any]], cap: int) -> dict[str, Any]:
    """METRICS §2.3 — Rule 7's counters. Any workflow over the cap is a bug."""
    calls = [float(r.get("llm_calls") or 0) for r in results]
    violations = [r["workflow_id"] for r in results if (r.get("llm_calls") or 0) > cap]
    retries = sum(
        max(0, entry.get("attempts", 1) - 1) for r in results for entry in r.get("steps", [])
    )
    return {
        "llm_calls": _spread(calls),
        "cap": cap,
        "budget_violations": len(violations),
        "budget_violation_ids": violations[:10],
        "step_retry_total": retries,
        "forced_escalation_total": sum(1 for r in results if r["terminal_action"] == ESCALATE),
    }


def summarise(results: list[dict[str, Any]], cap: int) -> dict[str, Any]:
    """Every §5.3 metric for one configuration.

    Infrastructure failures are removed before anything is scored. A model
    server that goes down makes every executor call raise; the pipeline treats
    that as a retryable fault, exhausts the retry cap and escalates — which is
    indistinguishable in the results file from the agent correctly refusing a
    bad workflow. Left in, a dead model server reads as a cautious agent: it
    inflates detection on faulted rows and depresses it on clean ones. They are
    counted and reported separately so a run that lost many is visibly suspect
    rather than quietly flattering.
    """
    excluded = [r for r in results if r.get("infrastructure_failure")]
    results = [r for r in results if not r.get("infrastructure_failure")]
    faulted = [r for r in results if r["expected_terminal_action"] == ESCALATE]
    clean = [r for r in results if r["expected_terminal_action"] == PROCEED]
    return {
        "workflow_count": len(results),
        "faulted_count": len(faulted),
        "clean_count": len(clean),
        "e2e_success": e2e_success(results).as_dict(),
        "e2e_success_faulted": e2e_success(faulted).as_dict(),
        "e2e_success_clean": e2e_success(clean).as_dict(),
        "step_success": step_success(results),
        "survive": survive(results),
        "false_commit_rate": false_commit_rate(results).as_dict(),
        **escalation_quality(results),
        "detection_by_class": detection_by_class(results),
        "latency_s": latency(results),
        "budget": budget(results, cap),
        "errors": [r["workflow_id"] for r in results if r.get("error")],
        "setup_errors": [r["workflow_id"] for r in results if r.get("setup_error")],
        "infrastructure_failures": len(excluded),
        "infrastructure_failure_ids": [r["workflow_id"] for r in excluded][:10],
    }
