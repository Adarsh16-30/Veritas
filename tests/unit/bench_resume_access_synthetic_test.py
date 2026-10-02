"""bench.run refuses to resume or start a run that would mix conditions (Rule 4)."""

from __future__ import annotations

from bench.conditions import EVIDENCE_VERSION
from bench.run import resume_conflict

SINGLE = "single identity"


def _now(access: str = SINGLE, version: int = EVIDENCE_VERSION) -> dict[str, object]:
    return {"evidence_version": version, "erp_access": access}


def test_results_written_before_the_fields_existed_are_legacy_conditions() -> None:
    """The in-flight v4 file has neither field: version 1, single identity."""
    v4 = {"results": [{"workflow_id": "v4-bench-clean-00"}]}
    assert resume_conflict(v4, _now(version=1)) is None
    conflict = resume_conflict(v4, _now())
    assert conflict is not None and "evidence_version" in conflict


def test_same_conditions_resume_and_any_difference_refuses() -> None:
    prior = {"evidence_version": EVIDENCE_VERSION, "erp_access": "step-scoped (x)"}
    assert resume_conflict(prior, _now("step-scoped (x)")) is None
    assert "erp_access" in (resume_conflict(prior, _now()) or "")


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
