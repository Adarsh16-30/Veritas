"""The Phase 7 incident replay: case selection and the side-by-side table."""

from __future__ import annotations

from typing import Any

from bench.demo import INCIDENT, incident_cases, render_table


def test_both_configurations_replay_the_same_real_incidents() -> None:
    b, v = incident_cases("db1", 7), incident_cases("dv1", 7)
    assert [c.variant for c in b] == list(INCIDENT)
    assert [c.record.award_id for c in b] == [c.record.award_id for c in v]
    assert all(c.workflow_id.startswith("db1-demo-") for c in b)
    assert {c.injection.expected_terminal_action for c in b} == {"proceed", "escalate"}


def _rec(wid: str, terminal: str, expected: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "workflow_id": wid,
        "terminal_action": terminal,
        "expected_terminal_action": expected,
        "correct": terminal == expected,
        "reason": reason,
        "steps": [{"step": "S5"}],
        "infrastructure_failure": False,
    }


def test_table_tells_the_story_and_labels_itself() -> None:
    rows = [
        {
            "variant": "compounding",
            "expected": "escalate",
            "baseline": _rec("db1-demo-compounding-00", "proceed", "escalate"),
            "verified": _rec(
                "dv1-demo-compounding-00",
                "escalate",
                "escalate",
                "hard_rule_violation: S5_WITHIN_APPROVED_AUTHORITY",
            ),
        }
    ]
    out = render_table(rows, "http://127.0.0.1:8765")
    line = next(ln for ln in out.splitlines() if ln.startswith("compounding"))
    assert "MISS paid" in line and "ok  stopped at S5 (hard_rule_violation)" in line
    assert "not a measurement" in out
    assert "db1-demo-compounding-00" in out and "dv1-demo-compounding-00" in out
