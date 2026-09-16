"""Run one configuration across the faulted corpus (PRD §5.2, §7 Phase 4).

    uv run python -m bench.run --config baseline --out results/baseline_results.json
    uv run python -m bench.run --config verified --out results/verified_results.json

The two configurations are the same pipeline with one constructor argument
different — ``gate=None`` for the baseline, a ``VerificationGate`` for the
verified path. Nothing else changes between them, so the delta Phase 4 reports
is the gate's effect and not an incidental difference between two code paths.

Rule 4 lives here: this refuses to emit any comparison against a baseline that
does not exist. ``--config verified`` will run and record its own numbers
happily, but the *delta* is computed in ``bench/report.py`` and only against a
recorded ``baseline_results.json``.

Every workflow moves the real ledger. Faulted workflows that a correct agent
escalates never reach a payment; the clean controls do, and post real GL
entries. Results are written after each workflow so a run that dies at
workflow 37 keeps the first 36.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from agent.executor import Executor, OllamaLLM  # noqa: E402
from agent.pipeline import Policy  # noqa: E402
from agent.state import Status  # noqa: E402
from data.corpus import seed  # noqa: E402
from erp.client import ERPClient, ERPError  # noqa: E402
from harness.corpus import (  # noqa: E402
    VARIANTS,
    CorpusPlan,
    WorkflowCase,
    build,
    persist_labels,
)
from harness.faults import ESCALATE, PROCEED  # noqa: E402
from orchestrator.db import Store  # noqa: E402
from orchestrator.machine import WorkflowMachine  # noqa: E402
from verify.gate import build_gate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _rel(path: Path) -> str:
    """Display path, tolerant of an --out that points outside the repo."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


BASELINE_RESULTS = ROOT / "results" / "baseline_results.json"

CONFIGS = ("baseline", "verified")

#: Substrings that mean the *infrastructure* failed, not that the agent decided
#: anything. A model server that is unreachable makes every executor call raise;
#: the pipeline treats that as a retryable fault, burns the retry cap and
#: escalates — and an escalation is indistinguishable, in the results file, from
#: the agent correctly refusing a bad workflow. Scoring those as agent decisions
#: silently credits the benchmark for catches it never made and, on clean
#: controls, penalises it for failures that never happened.
INFRASTRUCTURE_MARKERS: tuple[str, ...] = (
    "ollama unreachable",
    "Max retries exceeded",
    "Connection refused",
    "WinError 10061",
    "Failed to establish a new connection",
    "Read timed out",
)


def is_infrastructure_failure(reason: str | None) -> bool:
    text = reason or ""
    return any(marker.lower() in text.lower() for marker in INFRASTRUCTURE_MARKERS)


def _commit_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def terminal_action(status: Status) -> str:
    """What the agent actually did, in the label's vocabulary.

    A workflow that ran to completion released a payment; anything else stopped
    short of one. Both are correct outcomes for different labels, which is why
    METRICS §1.2 scores a correct refusal as success rather than as failure.
    """
    return PROCEED if status is Status.COMPLETED else ESCALATE


def run_case(
    case: WorkflowCase,
    store: Store,
    erp: ERPClient,
    config: str,
    executor_model: str,
) -> dict[str, Any]:
    """Set up this case's real ERP state, then run the workflow to its end."""
    started = time.monotonic()
    setup_error: str | None = None
    if case.injection.setup is not None:
        try:
            case.injection.setup(erp, store)
        except ERPError as e:
            # PRD §6.3: a fault ERPNext refuses outright is a finding, not a
            # test of the agent — the agent never gets a turn. Recorded as such.
            setup_error = str(e)[:400]

    gate = (
        build_gate(erp, case.spec, executor_model=executor_model) if config == "verified" else None
    )
    policy = Policy.verified() if config == "verified" else Policy()
    machine = WorkflowMachine(
        store, erp, case.spec, Executor(OllamaLLM()), policy=policy, gate=gate
    )

    error: str | None = None
    try:
        outcome = machine.run(case.workflow_id)
        status, reason, docs = outcome.status, outcome.reason, dict(outcome.docs)
        steps = [
            {
                "step": s.step.value,
                "route": s.route.value,
                "action": s.action.value if s.action else None,
                "committed": s.committed,
                "doc_name": s.doc_name,
                "attempts": s.attempts,
                "latency_ms": s.latency_ms,
                "reason": s.reason,
            }
            for s in outcome.steps
        ]
    except Exception as e:  # noqa: BLE001 - a crashed workflow is a recorded result
        status, reason, docs, steps = Status.FAILED, f"{type(e).__name__}: {e}"[:400], {}, []
        error = reason

    label = case.label_row()
    infrastructure = is_infrastructure_failure(reason) or is_infrastructure_failure(error)
    return {
        **label,
        "variant": case.variant,
        "rule_detectable": case.injection.rule_detectable,
        "config": config,
        "status": status.value,
        "terminal_action": terminal_action(status),
        # An infrastructure failure is never "correct" and never "incorrect" —
        # it is not a decision. `summarise` drops these rows entirely rather
        # than letting a dead model server look like a cautious agent.
        "infrastructure_failure": infrastructure,
        "correct": (
            not infrastructure and terminal_action(status) == label["expected_terminal_action"]
        ),
        "reason": reason,
        "steps": steps,
        "documents": docs,
        "llm_calls": store.llm_calls(case.workflow_id),
        "wall_clock_ms": int((time.monotonic() - started) * 1000),
        "setup_error": setup_error,
        "error": error,
    }


def prepare(plan: CorpusPlan, store: Store, erp: ERPClient, which: str) -> list[WorkflowCase]:
    """Seed the real masters these cases need and write their ground truth."""
    cases = plan.benchmark if which == "benchmark" else plan.calibration
    records = {c.record.item_code: c.record for c in cases}
    seed(erp, list(records.values()))
    persist_labels(store, cases)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=CONFIGS, required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--split", choices=("benchmark", "calibration"), default="benchmark")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N (smoke runs)")
    parser.add_argument(
        "--variants",
        default="",
        help="comma-separated variant names to run; default is all of them",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--bench-per-variant", type=int, default=4)
    parser.add_argument("--cal-per-variant", type=int, default=3)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="keep workflows already recorded in --out and run only the rest",
    )
    parser.add_argument(
        "--run-tag",
        default="",
        help="namespaces workflow ids; defaults to the config name so the two "
        "configurations never resume each other's workflows",
    )
    args = parser.parse_args()

    load_dotenv()
    # Resolve against the repo root so a relative --out still reports cleanly.
    out_path = Path(args.out or ROOT / "results" / f"{args.config}_{args.split}_results.json")
    if not out_path.is_absolute():
        out_path = (ROOT / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    erp = ERPClient()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1

    run_tag = args.run_tag or args.config
    plan = build(args.bench_per_variant, args.cal_per_variant, args.seed, run_tag)
    plan.assert_disjoint()
    plan.save(ROOT / "results" / f"corpus_plan_{run_tag}.json")
    # The benchmark ID set, written where scripts/calibrate.py can read it: the
    # conformal fit must be able to prove it never saw these workflows (Rule 9).
    ids_path = ROOT / "results" / "benchmark_ids.json"
    ids_path.write_text(
        json.dumps([c.workflow_id for c in plan.benchmark], indent=2), encoding="utf-8"
    )

    executor_model = OllamaLLM().name
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    with Store() as store:
        cases = prepare(plan, store, erp, args.split)
        if args.variants:
            wanted = {v.strip() for v in args.variants.split(",") if v.strip()}
            unknown = wanted - set(VARIANTS)
            if unknown:
                print(f"unknown variant(s): {sorted(unknown)}", file=sys.stderr)
                return 2
            cases = [c for c in cases if c.variant in wanted]
        print(f"config={args.config} split={args.split} workflows={len(cases)}")
        print(f"  corpus seed={args.seed}  run_tag={run_tag}  executor={executor_model}")
        print(f"  writing {_rel(out_path)}\n", flush=True)

        # Run-level resume. Workflows are already individually resumable (Rule 6),
        # but a benchmark that has to restart from workflow 1 after an
        # interruption is a benchmark that never finishes on a laptop: two hours
        # of real model calls is long enough that being killed part-way through
        # is the normal case, not the exceptional one.
        #
        # Resume is applied *before* --limit, so `--resume --limit 3` means "run
        # the next three outstanding workflows". Limiting first would re-slice
        # the same already-finished prefix on every invocation and the run would
        # never advance — which is exactly what it did before this was fixed.
        results: list[dict[str, Any]] = []
        if args.resume and out_path.exists():
            prior = json.loads(out_path.read_text(encoding="utf-8"))
            results = list(prior.get("results", []))
            # A workflow whose model server was down was never actually run.
            # Keeping it would bake an infrastructure outage into the results.
            results = [r for r in results if not r.get("infrastructure_failure")]
            done = {r["workflow_id"] for r in results}
            before = len(cases)
            cases = [c for c in cases if c.workflow_id not in done]
            print(
                f"  resuming: {len(results)} already recorded, "
                f"{len(cases)} of {before} still outstanding"
            )
        if args.limit:
            cases = cases[: args.limit]

        wall_started = time.monotonic()
        for i, case in enumerate(cases, start=1):
            record = run_case(case, store, erp, args.config, executor_model)
            results.append(record)
            mark = "ok " if record["correct"] else "MISS"
            print(
                f"  [{i:>3}/{len(cases)}] {mark} {case.workflow_id:34} "
                f"{record['terminal_action']:8} (want {record['expected_terminal_action']:8}) "
                f"{record['wall_clock_ms'] / 1000:6.1f}s llm={record['llm_calls']}",
                flush=True,
            )
            _write(out_path, args, plan, executor_model, started_at, results, wall_started)

    correct = sum(1 for r in results if r["correct"])
    print(f"\n{correct}/{len(results)} terminal actions matched the label")
    print(f"wrote {_rel(out_path)}")
    if args.config == "verified" and not BASELINE_RESULTS.exists():
        print(
            "\nnote: results/baseline_results.json does not exist, so no verified-vs-baseline "
            "delta can be reported (Rule 4). Run --config baseline first.",
            file=sys.stderr,
        )
    return 0


def _write(
    out_path: Path,
    args: argparse.Namespace,
    plan: CorpusPlan,
    executor_model: str,
    started_at: str,
    results: list[dict[str, Any]],
    wall_started: float,
) -> None:
    """Rewritten after every workflow: a run that dies keeps what it earned."""
    payload = {
        "config": args.config,
        "split": args.split,
        "corpus_seed": args.seed,
        "executor_model": executor_model,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _commit_sha(),
        "workflow_count": len(results),
        "benchmark_ids": [c.workflow_id for c in plan.benchmark],
        "calibration_ids": [c.workflow_id for c in plan.calibration],
        "elapsed_s": round(time.monotonic() - wall_started, 1),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
