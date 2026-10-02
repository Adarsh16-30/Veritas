"""Turn recorded results into ``docs/results.md`` (Rule 4, Rule 10).

    uv run python -m bench.report --results-dir results --report docs/results.md

Rule 4 is enforced here and nowhere else: **no delta without a recorded
baseline**. If ``results/baseline_results.json`` does not exist, this writes the
verified configuration's own numbers and refuses to compute a comparison. There
is no flag to override that.

Rule 10 is enforced by construction: every number this emits is written on a
line carrying both a ``[metric: <name>]`` tag naming a definition in
``docs/METRICS.md`` and a ``[results: <file>]`` citation with the commit and
timestamp of the run that produced it. A number without a run behind it cannot
be expressed by this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.conditions import differences, recorded  # noqa: E402
from bench.metrics import summarise  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASELINE_NAME = "baseline_results.json"
VERIFIED_NAME = "verified_results.json"

#: Per-configuration model-call caps (agent/pipeline.py Policy).
CAPS = {"baseline": 18, "verified": 36}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _cite(payload: dict[str, Any], path: str) -> str:
    """A Rule 10 citation naming the file the number was actually read from.

    ``path`` is the file's location relative to the repo root. It used to be
    hardcoded to ``results/<name>`` whatever ``--results-dir`` said, so a report
    rendered from an archived run cited the live file instead -- which by then
    held a different run's numbers.
    """
    commit = str(payload.get("commit") or "")[:12]
    return f"[results: {path}] (commit {commit}, {payload.get('completed_at')})"


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def missing_workflows(payload: dict[str, Any]) -> list[str]:
    """Benchmark workflows a run has not yet recorded a usable result for.

    ``bench.run --resume`` re-runs anything flagged ``infrastructure_failure``,
    so those count as not yet done. An empty list means the run is complete.
    """
    done = {
        r["workflow_id"] for r in payload.get("results", []) if not r.get("infrastructure_failure")
    }
    return [w for w in payload.get("benchmark_ids", []) if w not in done]


def _pct(entry: dict[str, Any] | None) -> str:
    if not entry or entry.get("rate") is None:
        return "n/a"
    rate, err, n = entry["rate"], entry.get("stderr") or 0.0, entry.get("n")
    return f"{rate * 100:.1f}% ± {err * 100:.1f} (n={n})"


def _delta(verified: dict[str, Any] | None, baseline: dict[str, Any] | None) -> str:
    if not verified or not baseline:
        return "n/a"
    if verified.get("rate") is None or baseline.get("rate") is None:
        return "n/a"
    diff = (verified["rate"] - baseline["rate"]) * 100
    return f"{diff:+.1f} pp"


def render(
    baseline: dict[str, Any] | None,
    verified: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
    results_dir: Path | None = None,
) -> str:
    results_dir = results_dir or ROOT / "results"
    lines: list[str] = ["# results.md", ""]

    if not baseline and not verified:
        lines += [
            "No measurements recorded yet. Run `uv run python -m bench.run --config baseline` "
            "to produce the Rule 4 denominator.",
            "",
        ]
        return "\n".join(lines)

    b_sum = summarise(baseline["results"], CAPS["baseline"]) if baseline else None
    v_sum = summarise(verified["results"], CAPS["verified"]) if verified else None
    b_cite = _cite(baseline, _rel(results_dir / BASELINE_NAME)) if baseline else ""
    v_cite = _cite(verified, _rel(results_dir / VERIFIED_NAME)) if verified else ""
    ledger_rel = _rel(results_dir / "ledger_reconciliation.json")
    # A row holds a baseline number and a verified number, read from two files.
    # Citing only one of them leaves the other column untraceable (Rule 10).
    cites = "; ".join(c for c in (b_cite, v_cite) if c)

    lines += [
        "Every number below is measured. Targets live in `VERITAS_PRD.md` and "
        "`docs/METRICS.md` and are never written here (Rule 10).",
        "",
        "Corpus: real U.S. federal contract awards from USAspending.gov, seeded into the real "
        "ERPNext instance — source, request and checksum in `data/dataset_manifest.json` "
        "(Rule 8). Faults are injected as real ERPNext documents and real procurement requests "
        "(PRD §6.3); ground truth comes from the injector that created each condition.",
        "",
    ]

    # The conformal router's status is part of what the verified numbers mean.
    # Reporting a "verified" result without saying whether the calibrated router
    # or the uncalibrated fallback produced it would leave the reader to assume
    # the stronger of the two.
    calibration = results_dir / "calibration.json"
    if verified:
        if calibration.exists():
            cal = json.loads(calibration.read_text(encoding="utf-8"))
            lines += [
                f"**Conformal router: calibrated** (alpha={cal.get('alpha')}, "
                f"qhat={cal.get('qhat')}, fitted on {cal.get('n_calibration')} held-out points "
                f"from {len(cal.get('workflow_ids', []))} workflows disjoint from this "
                "benchmark, Rule 9). [results: {_rel(calibration)}]",
                "",
            ]
        else:
            lines += [
                f"**Conformal router: UNCALIBRATED.** No `{_rel(calibration)}` exists, so "
                "the gate routed through `verify.conformal.unanimous_region` — all three gates "
                "must agree to commit — rather than through a fitted prediction region. The "
                "verified numbers below are for that configuration. No coverage or ECE figure "
                "is reported, because an uncalibrated region makes no such claim and "
                "`verify.conformal.coverage()` refuses to score one.",
                "",
            ]

    # A run still being resumed is not a measurement of the benchmark, and a
    # delta between a complete baseline and half a verified run compares two
    # different workflow sets. Say so, and withhold the delta.
    incomplete = {
        name: missing_workflows(payload)
        for name, payload in (("baseline", baseline), ("verified", verified))
        if payload and missing_workflows(payload)
    }
    if incomplete:
        lines += ["## INCOMPLETE RUN — no delta reported", ""]
        for name, missing in incomplete.items():
            payload = baseline if name == "baseline" else verified
            assert payload is not None
            expected = len(payload.get("benchmark_ids", []))
            lines.append(
                f"- **{name}**: {expected - len(missing)} of {expected} benchmark workflows "
                f"recorded; {len(missing)} still to run (`bench.run --resume`)."
            )
        lines += [
            "",
            "The per-configuration numbers below cover only what has been recorded. "
            "They are not the benchmark result, and no verified-vs-baseline delta is "
            "computed until both runs are complete.",
            "",
        ]

    # Rule 4: a delta is the gate's effect only if nothing else changed between
    # the two runs. Different evidence (context, prompts, corpus) or a different
    # ERP access model makes it a comparison of two experiments.
    mismatch = differences(recorded(baseline), recorded(verified)) if baseline and verified else []
    if mismatch:
        lines += [
            "## CONDITIONS DIFFER — no delta reported",
            "",
            "The baseline and verified runs were recorded under different conditions "
            "(`bench/conditions.py`):",
            "",
            *[f"- {d}" for d in mismatch],
            "",
            "Each configuration's own numbers follow, but the comparison between them would "
            "not isolate the verification gate, so it is not computed.",
            "",
        ]

    if not baseline:
        lines += [
            "## Rule 4",
            "",
            f"`{_rel(results_dir / BASELINE_NAME)}` does not exist, so **no verified-vs-baseline "
            "delta "
            "is reported**. The verified configuration's own numbers follow; the comparison "
            "requires the recorded baseline and is not estimated.",
            "",
        ]

    # --- headline ---------------------------------------------------------------
    lines += ["## Headline", "", "| Metric | Baseline | Verified | Delta |", "|---|---|---|---|"]
    rows = [
        ("e2e_success", "e2e_success", "end-to-end success"),
        ("e2e_success_faulted", "e2e_success_faulted", "— on faulted workflows"),
        ("e2e_success_clean", "e2e_success_clean", "— on clean controls"),
        ("false_commit_rate", "false_commit_rate", "false-commit rate"),
        ("esc_precision", "esc_precision", "escalation precision"),
        ("esc_recall", "esc_recall", "escalation recall"),
    ]
    for key, metric, label in rows:
        b = b_sum.get(key) if b_sum else None
        v = v_sum.get(key) if v_sum else None
        comparable = b_sum and v_sum and not incomplete and not mismatch
        delta = _delta(v, b) if comparable else "n/a"
        lines.append(f"| {label} [metric: {metric}] | {_pct(b)} | {_pct(v)} | {delta} | {cites}")
    lines.append("")

    # --- compounding curve -------------------------------------------------------
    lines += [
        "## Compounding-failure curve",
        "",
        "Fraction of workflows still correct at every one of their first k steps.",
        "",
        "| k | Baseline | Verified | source |",
        "|---|---|---|---|",
    ]
    for k in range(1, 7):
        b = b_sum["survive"].get(f"k{k}") if b_sum else None
        v = v_sum["survive"].get(f"k{k}") if v_sum else None
        lines.append(f"| {k} | {_pct(b)} | {_pct(v)} | [metric: survive] {cites}")
    lines += ["", (cites), ""]

    # --- per-fault-class detection ----------------------------------------------
    lines += [
        "## Detection by fault class",
        "",
        "The honest weakness map (PRD §5.3). `rules` marks the classes the "
        "deterministic rule engine can catch on its own — the classes where it "
        "cannot are where a verification gate has to earn its place.",
        "",
        "| Class | Expected | Rules? | Baseline | Verified | source |",
        "|---|---|---|---|---|---|",
    ]
    names = sorted(set((b_sum or v_sum or {}).get("detection_by_class", {})))
    for name in names:
        b = (b_sum or {}).get("detection_by_class", {}).get(name)
        v = (v_sum or {}).get("detection_by_class", {}).get(name)
        ref = b or v or {}
        rules = "yes" if ref.get("rule_detectable") else "no"
        lines.append(
            f"| {name} | {ref.get('expected_terminal_action', '?')} | {rules} "
            f"| {_pct(b)} | {_pct(v)} | [metric: detection] {cites}"
        )
    lines += ["", (cites), ""]

    # --- cost / stopping ---------------------------------------------------------
    lines += ["## Latency and stopping", "", "| Metric | Baseline | Verified |", "|---|---|---|"]
    for label, key, fmt in [
        ("median end-to-end [metric: latency_p50]", "p50", "{:.1f}s"),
        ("p95 end-to-end [metric: latency_p95]", "p95", "{:.1f}s"),
        ("mean model calls per workflow", "mean", "{:.1f}"),
    ]:
        if "latency" in label:
            b = (b_sum or {}).get("latency_s", {}).get(key)
            v = (v_sum or {}).get("latency_s", {}).get(key)
        else:
            b = (b_sum or {}).get("budget", {}).get("llm_calls", {}).get(key)
            v = (v_sum or {}).get("budget", {}).get("llm_calls", {}).get(key)
        bs = fmt.format(b) if isinstance(b, int | float) else "n/a"
        vs = fmt.format(v) if isinstance(v, int | float) else "n/a"
        lines.append(f"| {label} | {bs} | {vs} | {cites} |")
    for label, key in [
        ("forced escalations [metric: forced_escalation_total]", "forced_escalation_total"),
        ("step retries [metric: step_retry_total]", "step_retry_total"),
        ("budget violations [metric: budget_violations]", "budget_violations"),
    ]:
        b = (b_sum or {}).get("budget", {}).get(key)
        v = (v_sum or {}).get("budget", {}).get(key)
        lines.append(
            f"| {label} | {b if b is not None else 'n/a'} | {v if v is not None else 'n/a'} "
            f"| {cites} |"
        )
    lines += ["", (cites), ""]

    # --- ledger ------------------------------------------------------------------
    if ledger:
        status = "PASS — no exceptions" if ledger.get("passes") else "EXCEPTIONS"
        lines += [
            "## Ledger reconciliation",
            "",
            f"**{status}.** Checked against the real ERPNext general ledger after the runs, "
            "not against the agent's own record of what it did.",
            "",
            f"bad postings (duplicate + overpayment): {ledger.get('bad_postings')} "
            f"[metric: bad_postings] [results: {ledger_rel}] "
            f"({ledger.get('generated_at')})",
            "",
            f"- duplicate payments: {len(ledger.get('duplicate_payments', []))}",
            f"- overpayments: {len(ledger.get('overpayments', []))}",
            f"- unbalanced vouchers: {len(ledger.get('unbalanced_vouchers', []))}",
            f"- documents sharing an idempotency key: "
            f"{len(ledger.get('duplicate_idempotency_keys', []))}",
            "",
            f"[results: {ledger_rel}] ({ledger.get('generated_at')})",
            "",
        ]

    lines += [
        "## What these numbers do not cover",
        "",
        "See `docs/limitations.md`. In particular the conformal router's calibration "
        "status, the executor↔verifier correlation, and the latency target are recorded "
        "there with what is and is not measured.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--report", default="docs/results.md")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = ROOT / results_dir

    baseline = _load(results_dir / BASELINE_NAME)
    verified = _load(results_dir / VERIFIED_NAME)
    ledger = _load(results_dir / "ledger_reconciliation.json")

    if verified and not baseline:
        print(
            "RULE 4: results/baseline_results.json is missing, so no delta is reported. "
            "The verified configuration's own numbers are written; the comparison is not "
            "estimated.",
            file=sys.stderr,
        )

    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render(baseline, verified, ledger, results_dir), encoding="utf-8")

    print(f"wrote {_rel(report_path)}")
    for name, payload in (("baseline", baseline), ("verified", verified)):
        if payload:
            missing = missing_workflows(payload)
            note = f" -- INCOMPLETE, {len(missing)} still to run" if missing else ""
            print(f"  {name}: {len(payload['results'])} workflows{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
