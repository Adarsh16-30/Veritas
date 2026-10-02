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


class _Store:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def list_workflows(self, *, prefix: str, limit: int) -> list[dict[str, str]]:
        return [{"workflow_id": i} for i in self.ids if i.startswith(prefix)][:limit]


def test_a_fresh_run_refuses_a_used_tag() -> None:
    from bench.run import tag_conflict

    store = _Store(["v4-bench-clean-00", "v41-bench-clean-00"])
    assert tag_conflict(store, "v4", resume=False) is not None
    assert tag_conflict(store, "v4", resume=True) is None  # the deliberate path
    assert tag_conflict(store, "v5", resume=False) is None
    # "v4-" is the prefix, so tag v4 is not confused with v41 and vice versa.
    assert tag_conflict(_Store(["v41-bench-x-00"]), "v4", resume=False) is None


def test_resume_refuses_a_file_from_another_run() -> None:
    from bench.run import file_tag_conflict

    prior = {"results": [{"workflow_id": "b4-bench-clean-00"}]}
    assert file_tag_conflict(prior, "b4") is None
    assert "b4" in (file_tag_conflict(prior, "b2") or "")
    assert file_tag_conflict({"results": []}, "b5") is None
