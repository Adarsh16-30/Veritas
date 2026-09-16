"""The benchmark metrics, against inputs whose answers are known by hand.

Every headline number in `docs/results.md` comes out of `bench/metrics.py`, so a
quiet error here becomes a confidently-wrong published result. The cases below
are small enough to verify by inspection, which is the point.

Synthetic result rows (`*_synthetic_test.py`): these test arithmetic over
recorded outcomes, and hand-built rows are the only way to know what the answer
should be.
"""

from __future__ import annotations

import pytest

from bench.metrics import (
    Proportion,
    detection_by_class,
    e2e_success,
    escalation_quality,
    false_commit_rate,
    step_outcomes,
    step_success,
    summarise,
    survive,
)


def _result(
    workflow_id: str,
    *,
    expected: str,
    terminal: str,
    fault_step: str | None,
    committed_steps: list[str],
    reached: list[str] | None = None,
    variant: str = "v",
    llm_calls: int = 6,
    wall_ms: int = 1000,
) -> dict[str, object]:
    reached = reached if reached is not None else committed_steps
    return {
        "workflow_id": workflow_id,
        "variant": variant,
        "fault_class": variant,
        "fault_step": fault_step,
        "expected_terminal_action": expected,
        "terminal_action": terminal,
        "correct": expected == terminal,
        "rule_detectable": False,
        "llm_calls": llm_calls,
        "wall_clock_ms": wall_ms,
        "steps": [
            {
                "step": s,
                "route": "commit" if s in committed_steps else "escalate",
                "action": "proceed",
                "committed": s in committed_steps,
                "attempts": 1,
                "latency_ms": 100,
                "reason": None,
                "doc_name": None,
            }
            for s in reached
        ],
    }


CLEAN_OK = _result(
    "clean-ok",
    expected="proceed",
    terminal="proceed",
    fault_step=None,
    committed_steps=["S1", "S2", "S3", "S4", "S5", "S6"],
    variant="clean",
)
FAULT_CAUGHT = _result(
    "fault-caught",
    expected="escalate",
    terminal="escalate",
    fault_step="S4",
    committed_steps=["S1", "S2", "S3"],
    reached=["S1", "S2", "S3", "S4"],
    variant="conflicting",
)
FAULT_MISSED = _result(
    "fault-missed",
    expected="escalate",
    terminal="proceed",
    fault_step="S4",
    committed_steps=["S1", "S2", "S3", "S4", "S5", "S6"],
    variant="conflicting",
)


# --- Proportion -------------------------------------------------------------------
def test_proportion_reports_denominator_not_just_a_rate() -> None:
    """3/4 and 30/40 are the same rate and very different evidence."""
    small, large = Proportion(3, 4), Proportion(30, 40)
    assert small.rate == large.rate
    assert small.stderr is not None and large.stderr is not None
    assert small.stderr > large.stderr


def test_proportion_of_nothing_is_undefined_not_zero() -> None:
    empty = Proportion(0, 0)
    assert empty.rate is None
    assert empty.stderr is None


# --- per-step correctness ---------------------------------------------------------
def test_a_clean_workflow_is_correct_when_every_step_commits() -> None:
    assert all(ok for _, ok in step_outcomes(CLEAN_OK))


def test_a_faulted_workflow_is_correct_when_it_stops_at_the_fault_step() -> None:
    outcomes = dict(step_outcomes(FAULT_CAUGHT))
    assert outcomes["S3"] is True, "committing before the fault is correct"
    assert outcomes["S4"] is True, "declining at the fault step is correct"


def test_committing_the_faulted_step_is_scored_wrong() -> None:
    outcomes = dict(step_outcomes(FAULT_MISSED))
    assert outcomes["S3"] is True
    assert outcomes["S4"] is False, "committing at the fault step is the failure"
    assert outcomes["S6"] is False


def test_unreached_steps_are_not_scored() -> None:
    """Counting a step the workflow never reached as either success or failure
    would be inventing data."""
    assert len(step_outcomes(FAULT_CAUGHT)) == 4


# --- headline ---------------------------------------------------------------------
def test_a_correct_refusal_counts_as_success() -> None:
    """METRICS §1.2: the agent is right when it does the right thing, including
    stopping."""
    assert e2e_success([CLEAN_OK, FAULT_CAUGHT]).rate == 1.0


def test_a_missed_fault_counts_as_failure() -> None:
    assert e2e_success([FAULT_MISSED]).rate == 0.0


def test_step_success_is_scored_over_steps_actually_reached() -> None:
    scores = step_success([CLEAN_OK, FAULT_CAUGHT])
    assert scores["S1"]["n"] == 2
    assert scores["S6"]["n"] == 1, "only the clean workflow reached S6"


# --- false commits ----------------------------------------------------------------
def test_false_commit_rate_counts_commits_at_or_after_the_fault() -> None:
    fcr = false_commit_rate([FAULT_MISSED])
    assert fcr.numerator == 3, "S4, S5 and S6 all committed after the fault"
    assert fcr.denominator == 6


def test_a_caught_fault_contributes_no_false_commits() -> None:
    assert false_commit_rate([FAULT_CAUGHT]).numerator == 0


def test_clean_workflows_are_excluded_from_the_false_commit_denominator() -> None:
    """The rate is over the adversarial set; a clean workflow's commits are correct
    and would dilute it toward zero."""
    assert false_commit_rate([CLEAN_OK]).denominator == 0


# --- escalation quality -----------------------------------------------------------
def test_escalation_precision_and_recall() -> None:
    q = escalation_quality([CLEAN_OK, FAULT_CAUGHT, FAULT_MISSED])
    assert q["esc_precision"]["rate"] == 1.0, "the one escalation was warranted"
    assert q["esc_recall"]["rate"] == pytest.approx(0.5), "one of two faults was caught"


def test_an_agent_that_escalates_everything_has_poor_precision() -> None:
    over_eager = _result(
        "over",
        expected="proceed",
        terminal="escalate",
        fault_step=None,
        committed_steps=[],
        reached=["S1"],
        variant="clean",
    )
    q = escalation_quality([over_eager, FAULT_CAUGHT])
    assert q["esc_precision"]["rate"] == pytest.approx(0.5)


# --- compounding curve -------------------------------------------------------------
def test_survive_curve_decays_as_faults_accumulate() -> None:
    curve = survive([CLEAN_OK, FAULT_MISSED])
    assert curve["k1"]["rate"] == 1.0
    assert curve["k4"]["rate"] == pytest.approx(0.5), "the missed fault fails from S4 on"


def test_a_correct_refusal_still_counts_as_surviving() -> None:
    """Penalising a workflow for correctly stopping would make the curve measure
    eagerness rather than reliability."""
    curve = survive([FAULT_CAUGHT])
    assert curve["k6"]["rate"] == 1.0


# --- weakness map -------------------------------------------------------------------
def test_detection_is_reported_per_class_with_its_expected_action() -> None:
    table = detection_by_class([CLEAN_OK, FAULT_CAUGHT, FAULT_MISSED])
    assert table["clean"]["rate"] == 1.0
    assert table["conflicting"]["rate"] == pytest.approx(0.5)
    assert table["conflicting"]["expected_terminal_action"] == "escalate"


# --- Rule 7 counters -----------------------------------------------------------------
def test_budget_violations_are_counted_against_the_configured_cap() -> None:
    greedy = _result(
        "greedy",
        expected="proceed",
        terminal="proceed",
        fault_step=None,
        committed_steps=["S1"],
        llm_calls=99,
    )
    summary = summarise([greedy], cap=18)
    assert summary["budget"]["budget_violations"] == 1
    assert "greedy" in summary["budget"]["budget_violation_ids"]


def test_a_run_within_budget_reports_no_violations() -> None:
    assert summarise([CLEAN_OK], cap=18)["budget"]["budget_violations"] == 0


def test_infrastructure_failures_are_excluded_from_scoring() -> None:
    """A dead model server must not read as a cautious agent.

    When Ollama goes down every executor call raises, the pipeline treats that
    as a retryable fault, burns the retry cap and escalates — and in the results
    file that escalation is indistinguishable from the agent correctly refusing
    a bad workflow. Left in, an outage inflates detection on faulted rows and
    depresses it on clean ones. This happened for real during Phase 4 bring-up.
    """
    outage = _result(
        "outage",
        expected="proceed",
        terminal="escalate",
        fault_step=None,
        committed_steps=[],
        reached=["S1"],
        variant="clean",
    )
    outage["infrastructure_failure"] = True
    outage["correct"] = False

    summary = summarise([CLEAN_OK, outage], cap=18)
    assert summary["workflow_count"] == 1, "the outage row must not be scored"
    assert summary["e2e_success"]["rate"] == 1.0
    assert summary["infrastructure_failures"] == 1
    assert "outage" in summary["infrastructure_failure_ids"]


def test_an_outage_is_reported_rather_than_silently_dropped() -> None:
    """Excluding them quietly would make a badly-degraded run look clean."""
    outage = dict(FAULT_CAUGHT, workflow_id="down", infrastructure_failure=True)
    summary = summarise([outage], cap=18)
    assert summary["infrastructure_failures"] == 1
    assert summary["workflow_count"] == 0


def test_summarise_separates_clean_controls_from_faulted_workflows() -> None:
    """Reported together they hide the failure mode that matters: an agent that
    stops everything looks strong on faults and useless on clean work."""
    summary = summarise([CLEAN_OK, FAULT_CAUGHT, FAULT_MISSED], cap=18)
    assert summary["clean_count"] == 1
    assert summary["faulted_count"] == 2
    assert summary["e2e_success_clean"]["rate"] == 1.0
    assert summary["e2e_success_faulted"]["rate"] == pytest.approx(0.5)
