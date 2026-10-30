"""scripts/compare_results.py: reproduction within tolerance, and only like with like."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.compare_results import compare

ROOT = Path(__file__).resolve().parents[2]


def _copy(dst: Path) -> Path:
    dst.mkdir()
    for name in ("baseline_results.json", "verified_results.json"):
        shutil.copy(ROOT / "results" / name, dst / name)
    return dst


def test_a_run_reproduces_itself(tmp_path: Path) -> None:
    before = _copy(tmp_path / "a")
    _, failures = compare(before, before)
    assert failures == []


def test_a_real_change_is_reported(tmp_path: Path) -> None:
    before, after = _copy(tmp_path / "a"), _copy(tmp_path / "b")
    path = after / "verified_results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for r in payload["results"]:  # every faulted workflow now "completes"
        if r["expected_terminal_action"] == "escalate":
            r["terminal_action"], r["correct"], r["status"] = "proceed", False, "completed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _, failures = compare(before, after)
    assert "verified.e2e_success_faulted" in failures


def test_runs_under_different_conditions_are_not_compared(tmp_path: Path) -> None:
    before, after = _copy(tmp_path / "a"), _copy(tmp_path / "b")
    path = after / "baseline_results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="different conditions"):
        compare(before, after)
