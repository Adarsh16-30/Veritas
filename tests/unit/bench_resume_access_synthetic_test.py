"""bench.run refuses to resume a run under a different ERP access model (Rule 4)."""

from __future__ import annotations

from bench.run import resume_conflict


def test_runs_recorded_before_the_field_existed_are_single_identity() -> None:
    assert resume_conflict({"results": []}, "single identity") is None
    assert resume_conflict({"results": []}, "step-scoped (S1-S3 buyer ...)") is not None


def test_same_conditions_resume() -> None:
    prior = {"erp_access": "step-scoped (x)"}
    assert resume_conflict(prior, "step-scoped (x)") is None
    assert "single identity" in (resume_conflict(prior, "single identity") or "")
