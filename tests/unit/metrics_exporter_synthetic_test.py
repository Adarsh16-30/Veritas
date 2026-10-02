"""The Prometheus exporter (Phase 6): Rule 7 counters and benchmark gauges.

Synthetic: the live-state series are computed from the recorded scenario in
``tests/trace_scenario.py``; the benchmark series from the real committed
results files, so they can be checked against ``docs/results.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from tests.trace_scenario import MemoryStore, write_baseline, write_verified
from trace.metrics import VeritasCollector, reason_class, run_tag

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "results" / "archive" / "phase4-pre-gapfix"


def _scrape(store: MemoryStore | None, results_dir: Path) -> dict[tuple[str, frozenset], float]:
    @contextmanager
    def source() -> Iterator[MemoryStore]:
        if store is None:
            raise ConnectionError("postgres is down")
        yield store

    registry = CollectorRegistry()
    registry.register(VeritasCollector(source, results_dir))  # type: ignore[arg-type]
    text = generate_latest(registry).decode()
    out: dict[tuple[str, frozenset], float] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            out[(s.name, frozenset(s.labels.items()))] = s.value
    return out


def _get(samples: dict[tuple[str, frozenset], float], name: str, **labels: str) -> float:
    return samples[(name, frozenset(labels.items()))]


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    write_verified(s, "v4-bench-compounding-00")
    write_baseline(s, "b4-bench-clean-00")
    return s


def test_rule7_counters_from_the_durable_record(store: MemoryStore) -> None:
    m = _scrape(store, ARCHIVE)
    # S3 reached attempt 2 (attempt 1 left no row) and S4 retried once.
    assert _get(m, "veritas_step_retry_total", run_tag="v4", config="verified", step="S3") == 1
    assert _get(m, "veritas_step_retry_total", run_tag="v4", config="verified", step="S4") == 1
    assert (
        _get(
            m,
            "veritas_forced_escalation_total",
            run_tag="v4",
            config="verified",
            reason="hard_rule_violation",
        )
        == 1
    )
    assert _get(m, "veritas_workflows", run_tag="b4", config="baseline", status="completed") == 1
    assert _get(m, "veritas_budget_violations", run_tag="v4", config="verified") == 0
    assert _get(m, "veritas_llm_call_cap", config="verified") == 36


def test_routes_and_latency_histogram(store: MemoryStore) -> None:
    m = _scrape(store, ARCHIVE)
    assert (
        _get(
            m, "veritas_step_route_total", run_tag="v4", config="verified", step="S4", route="retry"
        )
        == 1
    )
    assert (
        _get(
            m,
            "veritas_step_route_total",
            run_tag="b4",
            config="baseline",
            step="S1",
            route="commit",
        )
        == 1
    )
    # S4 executor: 1.1 s and 1.0 s. Buckets are cumulative; +Inf is the count.
    labels = {"config": "verified", "step": "S4", "stage": "executor"}
    assert _get(m, "veritas_step_latency_seconds_bucket", le="1.0", **labels) == 1
    assert _get(m, "veritas_step_latency_seconds_bucket", le="2.0", **labels) == 2
    assert _get(m, "veritas_step_latency_seconds_count", **labels) == 2
    assert _get(m, "veritas_step_latency_seconds_sum", **labels) == pytest.approx(2.1)
    verifier = {"config": "verified", "step": "S4", "stage": "verifier"}
    assert _get(m, "veritas_step_latency_seconds_count", **verifier) == 2


def test_bench_gauges_match_the_published_report(store: MemoryStore) -> None:
    """docs/results.md was rendered from this archive: 47.5% / 57.5% e2e."""
    m = _scrape(store, ARCHIVE)
    assert (
        _get(m, "veritas_bench_rate_ratio", config="baseline", run="complete", metric="e2e_success")
        == 0.475
    )
    assert (
        _get(m, "veritas_bench_rate_ratio", config="verified", run="complete", metric="e2e_success")
        == 0.575
    )
    assert _get(m, "veritas_bench_survival_ratio", config="verified", run="complete", k="1") == 0.7
    assert (
        _get(
            m,
            "veritas_bench_detection_ratio",
            config="verified",
            run="complete",
            fault_class="temporal",
        )
        == 1
    )
    assert _get(m, "veritas_bench_complete", config="verified") == 1
    assert (
        _get(
            m,
            "veritas_bench_info",
            config="verified",
            file="verified_results.json",
            commit="9284c80bd6fb",
            completed_at="2026-09-16T16:02:12+00:00",
        )
        == 1
    )


def test_a_partial_run_says_so() -> None:
    m = _scrape(MemoryStore(), ROOT / "results")
    assert _get(m, "veritas_bench_complete", config="baseline") == 1
    assert _get(m, "veritas_bench_complete", config="verified") == 0
    assert _get(m, "veritas_bench_workflows", config="verified", kind="expected") == 44
    # Every number from the partial run is labelled as such on the series itself.
    partial = [
        k for k in m if k[0] == "veritas_bench_rate_ratio" and ("config", "verified") in k[1]
    ]
    assert partial and all(("run", "incomplete") in labels for _, labels in partial)
    assert ("run", "complete") in next(
        labels
        for name, labels in m
        if name == "veritas_bench_rate_ratio" and ("config", "baseline") in labels
    )


def test_uncalibrated_exports_no_calibration_numbers(store: MemoryStore) -> None:
    m = _scrape(store, ARCHIVE)
    assert _get(m, "veritas_calibration_fitted") == 0
    assert not any(name == "veritas_calibration" for name, _ in m)


def test_postgres_down_keeps_the_benchmark_series(tmp_path: Path) -> None:
    m = _scrape(None, ARCHIVE)
    assert _get(m, "veritas_source_up", source="postgres") == 0
    assert _get(m, "veritas_source_up", source="results") == 1
    assert _get(m, "veritas_bench_complete", config="verified") == 1


def test_label_normalisation() -> None:
    assert run_tag("v4-bench-clean-00") == "v4"
    assert run_tag("wf-001") == "adhoc"
    assert (
        reason_class("hard_rule_violation: S5_WITHIN_APPROVED_AUTHORITY") == "hard_rule_violation"
    )
    assert reason_class("retry_cap_exhausted: verifier_rejected: ...") == "retry_cap_exhausted"
    assert reason_class(None) == "unknown"
