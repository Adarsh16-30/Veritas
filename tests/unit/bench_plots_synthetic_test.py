"""The headline chart cites its sources and carries its caveats (Rule 10).

A chart is copied into slides and READMEs without the page around it, so what
docs/results.md says beside its tables must be on the chart itself.
"""

from __future__ import annotations

from pathlib import Path

from bench.plots import chart

ROOT = Path(__file__).resolve().parents[2]
IDS = ["t-bench-clean-00", "t-bench-clean-01"]


def _payload(recorded: list[str], commit: str, **extra: object) -> dict[str, object]:
    return {
        "benchmark_ids": IDS,
        "commit": commit,
        "results": [
            {
                "workflow_id": w,
                "expected_terminal_action": "proceed",
                "terminal_action": "proceed",
                "fault_class": "clean",
                "fault_step": None,
                "rule_detectable": False,
                "steps": [],
                "llm_calls": 6,
                "wall_clock_ms": 1000,
                "correct": True,
            }
            for w in recorded
        ],
        **extra,
    }


def test_the_chart_cites_both_files_and_commits() -> None:
    svg = chart(_payload(IDS, "a" * 40), _payload(IDS, "b" * 40), ROOT / "results")
    assert "results/baseline_results.json @ aaaaaaaaaaaa" in svg
    assert "results/verified_results.json @ bbbbbbbbbbbb" in svg
    assert "INCOMPLETE" not in svg and "CONDITIONS DIFFER" not in svg


def test_an_incomplete_run_says_so_on_the_chart() -> None:
    svg = chart(_payload(IDS, "a" * 40), _payload(IDS[:1], ""), ROOT / "results")
    assert "verified run INCOMPLETE (1/2)" in svg


def test_runs_under_different_conditions_say_so_on_the_chart() -> None:
    svg = chart(
        _payload(IDS, "a" * 40),
        _payload(IDS, "b" * 40, evidence_version=2),
        ROOT / "results",
    )
    assert "CONDITIONS DIFFER" in svg and "evidence_version: 1 vs 2" in svg


def test_the_committed_chart_cites_the_archive_it_was_drawn_from() -> None:
    svg = (ROOT / "docs" / "compounding_curve.svg").read_text(encoding="utf-8")
    assert "results/archive/phase4-pre-gapfix/verified_results.json" in svg
