"""``scripts/independence_report.py::collect`` — the query that turns paired
executor/verifier traces into Rule 3 / METRICS §4.1 evidence.

The defect this guards against: an earlier version filtered the known-wrong
subset down to ``proposed_commit=True`` rows only, which makes ``x`` constant
and ``ev_corr`` permanently ``None`` — even once real fault-injection labels
exist in Phase 4. That would silently defeat the one measurement Rule 3 needs:
"a verifier that passes whatever the executor proposes drives ev_corr -> 1 and
fails the build" can never be observed if ``x`` never takes the value 0.

A stub stands in for ``Store`` (`*_synthetic_test.py`, permitted by Rule 1):
what is under test is the aggregation logic, not a live Postgres connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.independence_report import collect
from verify.verifier import agreement_phi


@dataclass
class _Row:
    """Mimics a psycopg dict_row: subscriptable by column name."""

    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_Row]:
        return self._rows


class _Conn:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def execute(self, _sql: str) -> _Result:
        return _Result(self._rows)


class _StubStore:
    def __init__(self, rows: list[_Row]) -> None:
        self.conn = _Conn(rows)


NOW = datetime.now(UTC)


def _trace_row(
    workflow_id: str,
    step: str,
    attempt: int,
    stage: str,
    model: str,
    action: str | None = None,
    verdict: str | None = None,
    fault_step: str | None = None,
    prompt: str = "STEP: S6\nDELTA: outstanding=50.00",
) -> _Row:
    provenance: dict[str, Any] = {"stage": stage}
    if action is not None:
        provenance["action"] = action
    if verdict is not None:
        provenance["verdict"] = verdict
    return _Row(
        {
            "workflow_id": workflow_id,
            "step": step,
            "attempt": attempt,
            "model": model,
            "prompt": prompt,
            "provenance": provenance,
            "expected_terminal_action": "escalate",
            "fault_step": fault_step,
        }
    )


def _paired(
    workflow_id: str,
    step: str,
    executor_action: str,
    verifier_verdict: str,
    fault_step: str | None,
) -> list[_Row]:
    return [
        _trace_row(
            workflow_id,
            step,
            1,
            "executor",
            "llama3:8b",
            action=executor_action,
            fault_step=fault_step,
        ),
        _trace_row(
            workflow_id,
            step,
            1,
            "verifier",
            "qwen2.5-coder:7b",
            verdict=verifier_verdict,
            fault_step=fault_step,
        ),
    ]


def test_known_wrong_subset_is_not_pre_filtered_to_a_constant() -> None:
    """The regression test.

    Four workflows, all with a fault located at S4. Two executor instances
    correctly refuse (propose ``hold``), two wrongly propose ``proceed`` — `x`
    must vary within the known-wrong subset, and ev_corr must therefore be
    *computable*, not permanently undefined.
    """
    rows: list[_Row] = []
    rows += _paired("wf-1", "S4", "proceed", "pass", fault_step="S4")  # wrong + rubber-stamped
    rows += _paired("wf-2", "S4", "proceed", "fail", fault_step="S4")  # wrong + caught
    rows += _paired("wf-3", "S4", "hold", "pass", fault_step="S4")  # correctly refused
    rows += _paired("wf-4", "S4", "hold", "fail", fault_step="S4")  # correctly refused

    report = collect(_StubStore(rows))

    assert report["paired_steps"] == 4
    assert report["ev_corr_available"] is True, (
        "ev_corr must be computable once known-wrong rows exist with x varying"
    )
    assert report["ev_corr_n"] == 4
    assert report["ev_corr"] is not None


def test_ev_corr_reflects_rubber_stamping_when_present() -> None:
    """A verifier that passes exactly when the executor wrongly commits, and
    fails exactly when the executor correctly refuses, drives ev_corr -> 1 —
    the Rule 3 failure condition METRICS §4.1 exists to catch."""
    rows: list[_Row] = []
    for i in range(10):
        rows += _paired(f"wf-wrong-{i}", "S4", "proceed", "pass", fault_step="S4")
    for i in range(10):
        rows += _paired(f"wf-right-{i}", "S4", "hold", "fail", fault_step="S4")

    report = collect(_StubStore(rows))
    assert report["ev_corr_available"] is True
    assert report["ev_corr"] == pytest.approx(1.0)


def test_clean_only_corpus_reports_ev_corr_unavailable_not_a_substitute() -> None:
    """No fault_step anywhere (a happy-path-only corpus, as Phase 2/3 actually
    have) must report UNAVAILABLE — never the overall agreement rate, which on
    a clean corpus is high for reasons that have nothing to do with independence."""
    rows: list[_Row] = []
    for i in range(5):
        rows += _paired(f"wf-clean-{i}", "S1", "proceed", "pass", fault_step=None)

    report = collect(_StubStore(rows))
    assert report["ev_corr_available"] is False
    assert report["ev_corr"] is None
    assert report["overall_agreement_rate"] == pytest.approx(1.0)


def test_steps_before_the_fault_are_excluded_from_known_wrong() -> None:
    """A step reached before the injected fault has nothing wrong about it yet
    — including it would dilute the known-wrong subset with clean cases."""
    rows: list[_Row] = []
    rows += _paired("wf-1", "S1", "proceed", "pass", fault_step="S4")  # before the fault
    rows += _paired("wf-1", "S4", "proceed", "pass", fault_step="S4")  # at the fault

    report = collect(_StubStore(rows))
    assert report["ev_corr_n"] == 1


def test_no_paired_steps_reports_zero_not_an_error() -> None:
    assert collect(_StubStore([])) == {"paired_steps": 0}


def test_unpaired_traces_are_ignored() -> None:
    """An executor trace with no matching verifier trace (or vice versa) —
    e.g. a rule violation that never reached the verifier — must not crash the
    join or be silently counted as agreement."""
    rows = [_trace_row("wf-1", "S6", 1, "executor", "llama3:8b", action="proceed")]
    report = collect(_StubStore(rows))
    assert report == {"paired_steps": 0}


def test_payload_leak_detection_still_works_alongside_the_fix() -> None:
    leaky = _paired("wf-1", "S6", "proceed", "pass", fault_step=None)
    leaky[1] = _Row({**leaky[1].data, "prompt": "STEP: S6\nrationale: because I said so"})
    report = collect(_StubStore(leaky))
    assert report["verifier_payload_clean"] is False
    assert report["payload_leaks"]


def test_distinct_model_family_still_reported_correctly() -> None:
    rows = _paired("wf-1", "S6", "proceed", "pass", fault_step=None)
    report = collect(_StubStore(rows))
    assert report["distinct_model_family"] is True
    assert report["model_pairs"] == ["llama3 -> qwen2.5-coder"]


def test_agreement_phi_used_directly_confirms_the_computation() -> None:
    """Sanity: the metric this script reports is exactly `agreement_phi`."""
    pairs = [(True, True), (True, False), (False, True), (False, False)] * 3
    assert agreement_phi(pairs) == pytest.approx(0.0)
