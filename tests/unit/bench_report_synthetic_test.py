"""bench.report citations and partial-run handling (Rules 4 and 10).

Synthetic result payloads only. The numbers are irrelevant here; what is under
test is which file each number is attributed to, and that a run still being
resumed is never rendered as a benchmark result with a delta.
"""

from __future__ import annotations

import re
from typing import Any

from bench.report import ROOT, missing_workflows, render


def _result(wid: str, *, infra: bool = False) -> dict[str, Any]:
    return {
        "workflow_id": wid,
        "config": "baseline",
        "correct": True,
        "documents": {},
        "error": None,
        "expected_terminal_action": "proceed",
        "fault_class": "clean",
        "fault_step": None,
        "infrastructure_failure": infra,
        "llm_calls": 6,
        "reason": None,
        "rule_detectable": False,
        "setup_error": None,
        "status": "completed",
        "steps": [],
        "terminal_action": "proceed",
        "variant": "clean",
        "wall_clock_ms": 30_000,
    }


def _payload(ids: list[str], recorded: list[str], commit: str) -> dict[str, Any]:
    return {
        "benchmark_ids": ids,
        "results": [_result(w) for w in recorded],
        "commit": commit,
        "completed_at": "2026-01-01T00:00:00+00:00",
    }


IDS = ["t-bench-clean-00", "t-bench-clean-01", "t-bench-clean-02"]


def test_citations_name_the_directory_the_numbers_came_from() -> None:
    archive = ROOT / "results" / "archive" / "some-run"
    out = render(
        _payload(IDS, IDS, "a" * 40), _payload(IDS, IDS, "b" * 40), None, results_dir=archive
    )
    cited = set(re.findall(r"\[results: ([^\]]+)\]", out))
    assert "results/archive/some-run/baseline_results.json" in cited
    assert "results/archive/some-run/verified_results.json" in cited
    # Nothing may point at the live files when the report was built from elsewhere.
    assert "results/verified_results.json" not in cited
    assert "results/baseline_results.json" not in cited


def test_every_metric_row_cites_both_files_it_reads() -> None:
    out = render(_payload(IDS, IDS, "a" * 40), _payload(IDS, IDS, "b" * 40), None)
    for line in out.splitlines():
        if "[metric:" in line and "bad_postings" not in line:
            assert "baseline_results.json" in line, line
            assert "verified_results.json" in line, line


def test_complete_runs_report_a_delta() -> None:
    out = render(_payload(IDS, IDS, "a" * 40), _payload(IDS, IDS, "b" * 40), None)
    assert "INCOMPLETE" not in out
    assert re.search(r"\| [+-]\d+\.\d pp \|", out)


def test_partial_run_is_flagged_and_withholds_the_delta() -> None:
    out = render(_payload(IDS, IDS, "a" * 40), _payload(IDS, IDS[:1], ""), None)
    assert "INCOMPLETE RUN" in out
    assert "**verified**: 1 of 3" in out
    assert " pp |" not in out


def test_infrastructure_failures_count_as_not_yet_run() -> None:
    payload = _payload(IDS, IDS[:2], "")
    payload["results"].append(_result(IDS[2], infra=True))
    assert missing_workflows(payload) == [IDS[2]]


def test_default_results_dir_still_cites_results() -> None:
    out = render(_payload(IDS, IDS, "a" * 40), None, None)
    assert "[results: results/baseline_results.json]" in out


def test_runs_under_different_conditions_get_no_delta() -> None:
    baseline = _payload(IDS, IDS, "a" * 40)  # no fields: evidence_version 1
    verified = {**_payload(IDS, IDS, "b" * 40), "evidence_version": 2}
    out = render(baseline, verified, None)
    assert "CONDITIONS DIFFER" in out and "evidence_version: 1 vs 2" in out
    assert " pp |" not in out


def test_same_conditions_still_compare() -> None:
    baseline = {**_payload(IDS, IDS, "a" * 40), "evidence_version": 2}
    verified = {**_payload(IDS, IDS, "b" * 40), "evidence_version": 2}
    out = render(baseline, verified, None)
    assert "CONDITIONS DIFFER" not in out and re.search(r"\| [+-]\d+\.\d pp \|", out)
