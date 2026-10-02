"""Prometheus metrics for VERITAS (PRD §2.7, Phase 6; Rule 7's counters).

    uv run python scripts/metrics_exporter.py      # http://127.0.0.1:9108/metrics

Two sources, kept apart by metric prefix so a live operational number is never
mistaken for a benchmark result:

``veritas_*``        the **live agent state** in Postgres -- workflows, step
                     attempts, retries, escalations, latency. Recomputed from
                     the durable record on every scrape.
``veritas_bench_*``  the **recorded benchmark** in ``results/*.json``, through
                     the same ``bench.metrics.summarise`` that writes
                     ``docs/results.md``, so the dashboard and the report cannot
                     disagree. Each configuration also exports a
                     ``veritas_bench_info`` series carrying the results file and
                     commit (Rule 10), and ``veritas_bench_complete`` says
                     whether the run covers every benchmark workflow.

Why counters are derived from Postgres rather than incremented in-process:
benchmark runs are short CLI processes, and an in-process counter dies with
the process before a scrape can reach it. Rule 7 asks that every retry and
escalation is *counted, never silent*; the durable record already holds each
one, and counting from it covers every worker, every past run and every
restart. Prometheus handles the reset if the state database is ever wiped.

Calibration metrics are exported only when ``results/calibration_curves.json``
exists. Until then ``veritas_calibration_fitted`` is 0 and no ECE is invented.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from bench.metrics import summarise
from bench.report import BASELINE_NAME, CAPS, VERIFIED_NAME, missing_workflows

#: Seconds. A baseline executor call is ~1-5 s; a verified attempt with a model
#: swap can pass 100 s on the 5 GB-RAM dev box (CLAUDE.md 6.6).
LATENCY_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 180.0, 300.0)

_RUN_TAG = re.compile(r"^([a-z]+\d+)-")
_REASON = re.compile(r"^[a-z_]+")


class MetricsSource(Protocol):
    def observability_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...


SourceFactory = Callable[[], AbstractContextManager[MetricsSource]]


def run_tag(workflow_id: str) -> str:
    """The benchmark run a workflow belongs to (``v4-bench-clean-00`` -> ``v4``).

    Anything else -- an ad-hoc ``run_workflow.py`` id -- is ``adhoc``, which keeps
    label cardinality bounded by the number of runs, not workflows.
    """
    m = _RUN_TAG.match(workflow_id)
    return m.group(1) if m else "adhoc"


def reason_class(reason: str | None) -> str:
    """``hard_rule_violation: S5_...`` -> ``hard_rule_violation``; bounded labels."""
    m = _REASON.match(reason or "")
    return m.group(0) if m else "unknown"


def _bucketize(values: list[float]) -> tuple[list[tuple[str, float]], float]:
    buckets = [(str(b), float(sum(1 for v in values if v <= b))) for b in LATENCY_BUCKETS]
    buckets.append(("+Inf", float(len(values))))
    return buckets, float(sum(values))


def live_metrics(
    workflows: list[dict[str, Any]], attempts: list[dict[str, Any]]
) -> Iterator[Metric]:
    """Every ``veritas_*`` series, from rows of the agent state store."""
    gated = {a["workflow_id"] for a in attempts if a.get("gated")}
    config_of = {
        w["workflow_id"]: ("verified" if w["workflow_id"] in gated else "baseline")
        for w in workflows
    }
    tag_of = {w["workflow_id"]: run_tag(w["workflow_id"]) for w in workflows}

    def key(wf: str) -> tuple[str, str]:
        return tag_of.get(wf, run_tag(wf)), config_of.get(wf, "baseline")

    by_status = GaugeMetricFamily(
        "veritas_workflows",
        "Workflows in the agent state store, by run, configuration and status.",
        labels=["run_tag", "config", "status"],
    )
    for (tag, cfg, status), n in sorted(
        Counter((*key(w["workflow_id"]), str(w["status"])) for w in workflows).items()
    ):
        by_status.add_metric([tag, cfg, status], n)
    yield by_status

    # Rule 7. Same definition as bench.metrics.budget: every terminal escalation.
    esc = CounterMetricFamily(
        "veritas_forced_escalation",
        "Workflows that ended in escalation to a human (Rule 7), by reason class.",
        labels=["run_tag", "config", "reason"],
    )
    for (tag, cfg, reason), n in sorted(
        Counter(
            (*key(w["workflow_id"]), reason_class(w.get("escalation_reason")))
            for w in workflows
            if w["status"] == "escalated"
        ).items()
    ):
        esc.add_metric([tag, cfg, reason], n)
    yield esc

    # Rule 7. Same definition as bench.metrics.budget: attempts beyond the first,
    # per step. The highest attempt number counts an attempt whose output was
    # rejected before it was traced, which a row count would miss.
    highest: dict[tuple[str, str], int] = {}
    for a in attempts:
        k = (a["workflow_id"], a["step"])
        highest[k] = max(highest.get(k, 0), int(a["attempt"]))
    retries: Counter[tuple[str, str, str]] = Counter()
    for (wf, step), n in highest.items():
        if n > 1:
            retries[(*key(wf), step)] += n - 1
    retry = CounterMetricFamily(
        "veritas_step_retry",
        "Step attempts beyond the first (Rule 7), by step.",
        labels=["run_tag", "config", "step"],
    )
    for (tag, cfg, step), n in sorted(retries.items()):
        retry.add_metric([tag, cfg, step], n)
    yield retry

    routes = CounterMetricFamily(
        "veritas_step_route",
        "Where each recorded attempt was routed: commit, retry or escalate.",
        labels=["run_tag", "config", "step", "route"],
    )
    for (tag, cfg, step, route), n in sorted(
        Counter((*key(a["workflow_id"]), a["step"], _route(a)) for a in attempts).items()
    ):
        routes.add_metric([tag, cfg, step, route], n)
    yield routes

    latency = HistogramMetricFamily(
        "veritas_step_latency_seconds",
        "Model-call latency per attempt. stage=executor is step_attempts.latency_ms; "
        "stage=verifier is the gate's own call, recorded separately.",
        labels=["config", "step", "stage"],
    )
    series: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for a in attempts:
        cfg = config_of.get(a["workflow_id"], "baseline")
        if a.get("latency_ms") is not None:
            series[(cfg, a["step"], "executor")].append(float(a["latency_ms"]) / 1000)
        if a.get("verifier_latency_ms") is not None:
            series[(cfg, a["step"], "verifier")].append(float(a["verifier_latency_ms"]) / 1000)
    for labels, values in sorted(series.items()):
        buckets, total = _bucketize(values)
        latency.add_metric(list(labels), buckets, total)
    yield latency

    calls = GaugeMetricFamily(
        "veritas_llm_calls_max",
        "Most model calls any single workflow has made, against its configuration's cap.",
        labels=["run_tag", "config"],
    )
    cap = GaugeMetricFamily(
        "veritas_llm_call_cap", "Per-workflow model-call budget (Rule 7).", labels=["config"]
    )
    violations = GaugeMetricFamily(
        "veritas_budget_violations",
        "Workflows over their model-call cap. Any value above 0 is a bug (Rule 7).",
        labels=["run_tag", "config"],
    )
    most: dict[tuple[str, str], int] = {}
    over: Counter[tuple[str, str]] = Counter()
    for w in workflows:
        k = key(w["workflow_id"])
        n = int(w.get("llm_calls") or 0)
        most[k] = max(most.get(k, 0), n)
        over[k] += int(n > CAPS[k[1]])
    for k in sorted(most):
        calls.add_metric(list(k), most[k])
        violations.add_metric(list(k), over[k])
    for cfg, n in sorted(CAPS.items()):
        cap.add_metric([cfg], n)
    yield calls
    yield cap
    yield violations


def _route(attempt: dict[str, Any]) -> str:
    if attempt.get("route"):
        return str(attempt["route"])
    # Baseline: the executor's own choice routes the step (agent/pipeline.py).
    if attempt.get("committed") or attempt.get("action") == "proceed":
        return "commit"
    return "escalate"


def bench_metrics(results_dir: Path) -> Iterator[Metric]:
    """Every ``veritas_bench_*`` series, from the recorded results files."""
    info = GaugeMetricFamily(
        "veritas_bench_info",
        "The results file behind every veritas_bench_* series (Rule 10).",
        labels=["config", "file", "commit", "completed_at"],
    )
    complete = GaugeMetricFamily(
        "veritas_bench_complete",
        "1 when the run covers every benchmark workflow; 0 while it is being resumed.",
        labels=["config"],
    )
    recorded = GaugeMetricFamily(
        "veritas_bench_workflows",
        "Benchmark workflows recorded vs expected.",
        labels=["config", "kind"],
    )
    survive = GaugeMetricFamily(
        "veritas_bench_survival_ratio",
        "Compounding-failure curve: fraction correct at every one of the first k steps.",
        labels=["config", "run", "k"],
    )
    rates = GaugeMetricFamily(
        "veritas_bench_rate_ratio",
        "Headline rates (METRICS.md): e2e success, false-commit rate, escalation precision/recall.",
        labels=["config", "run", "metric"],
    )
    detection = GaugeMetricFamily(
        "veritas_bench_detection_ratio",
        "Correct terminal action by fault class (the honest weakness map).",
        labels=["config", "run", "fault_class"],
    )
    latency = GaugeMetricFamily(
        "veritas_bench_latency_seconds",
        "End-to-end workflow wall clock in the recorded run.",
        labels=["config", "run", "quantile"],
    )
    for cfg, name in (("baseline", BASELINE_NAME), ("verified", VERIFIED_NAME)):
        path = results_dir / name
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = missing_workflows(payload)
        expected = len(payload.get("benchmark_ids", []))
        info.add_metric(
            [
                cfg,
                path.name,
                str(payload.get("commit") or "")[:12],
                str(payload.get("completed_at") or ""),
            ],
            1,
        )
        complete.add_metric([cfg], 0 if missing else 1)
        # Every number from a run still being resumed carries that fact on the
        # series itself, so no panel can show it beside a finished run unlabelled
        # (the same comparison docs/results.md withholds its delta for).
        run = "incomplete" if missing else "complete"
        recorded.add_metric([cfg, "recorded"], expected - len(missing))
        recorded.add_metric([cfg, "expected"], expected)

        s = summarise(payload.get("results", []), CAPS[cfg])
        for k, entry in s["survive"].items():
            _ratio(survive, [cfg, run, k.lstrip("k")], entry)
        for metric in (
            "e2e_success",
            "e2e_success_faulted",
            "e2e_success_clean",
            "false_commit_rate",
            "esc_precision",
            "esc_recall",
        ):
            _ratio(rates, [cfg, run, metric], s.get(metric))
        for fault_class, entry in s["detection_by_class"].items():
            _ratio(detection, [cfg, run, fault_class], entry)
        for q in ("p50", "p95"):
            v = s["latency_s"].get(q)
            if isinstance(v, int | float):
                latency.add_metric([cfg, run, q], float(v))
    yield from (info, complete, recorded, survive, rates, detection, latency)


def _ratio(family: GaugeMetricFamily, labels: list[str], entry: Any) -> None:
    # A rate with no denominator is absent, not zero: a zero would read as a
    # measured failure.
    if isinstance(entry, dict) and entry.get("rate") is not None:
        family.add_metric(labels, float(entry["rate"]))


def calibration_metrics(results_dir: Path) -> Iterator[Metric]:
    fitted = GaugeMetricFamily(
        "veritas_calibration_fitted",
        "1 when a conformal calibration artifact exists; 0 means the gate routes on the "
        "uncalibrated unanimous region and no coverage or ECE claim is made.",
    )
    curves_path = results_dir / "calibration_curves.json"
    has_model = (results_dir / "calibration.json").exists()
    fitted.add_metric([], 1 if has_model else 0)
    yield fitted
    if not (has_model and curves_path.exists()):
        return
    curves = json.loads(curves_path.read_text(encoding="utf-8"))
    cal = GaugeMetricFamily(
        "veritas_calibration",
        "Held-out calibration metrics (METRICS.md §3), pre and post conformal.",
        labels=["metric"],
    )
    for metric in (
        "ece_pre",
        "ece_post",
        "brier_pre",
        "brier_post",
        "coverage",
        "avg_region_size",
        "alpha",
    ):
        v = curves.get(metric)
        if isinstance(v, int | float):
            cal.add_metric([metric], float(v))
    yield cal


class VeritasCollector(Collector):
    """Recomputes everything on each scrape. A source that is down is reported
    through ``veritas_source_up`` rather than failing the whole scrape, so the
    benchmark panels stay up while Postgres restarts."""

    def __init__(self, open_source: SourceFactory, results_dir: Path) -> None:
        self.open_source = open_source
        self.results_dir = results_dir

    def collect(self) -> Iterable[Metric]:
        up = GaugeMetricFamily(
            "veritas_source_up", "Whether each metrics source could be read.", labels=["source"]
        )
        try:
            with self.open_source() as src:
                workflows, attempts = src.observability_rows()
            live = list(live_metrics(workflows, attempts))
            up.add_metric(["postgres"], 1)
        except Exception:  # noqa: BLE001 — any read failure is reported, not raised
            live = []
            up.add_metric(["postgres"], 0)
        try:
            bench = list(bench_metrics(self.results_dir)) + list(
                calibration_metrics(self.results_dir)
            )
            up.add_metric(["results"], 1)
        except (OSError, ValueError, KeyError):
            bench = []
            up.add_metric(["results"], 0)
        yield up
        yield from live
        yield from bench
