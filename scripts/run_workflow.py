"""Run one baseline-agent workflow end to end against the real ERPNext.

    uv run python scripts/run_workflow.py --workflow-id wf-001

Phase 2: no verification gate. Every action comes from a real model call
(Rule 2); every write is idempotent (Rule 5); state is checkpointed before each
side effect (Rule 6). Re-running the same ``--workflow-id`` resumes it and must
not post a second document.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from agent.context import WorkflowSpec  # noqa: E402
from agent.executor import Executor, OllamaLLM  # noqa: E402
from erp.client import ERPClient  # noqa: E402
from orchestrator.db import Store  # noqa: E402
from orchestrator.machine import WorkflowMachine  # noqa: E402
from trace.store import TraceLogger  # noqa: E402

load_dotenv()


def build_spec(args: argparse.Namespace) -> WorkflowSpec:
    needed_by = args.needed_by or (date.today() + timedelta(days=7)).isoformat()
    return WorkflowSpec(
        workflow_id=args.workflow_id,
        item_code=args.item,
        qty=args.qty,
        supplier=args.supplier,
        rate=args.rate,
        needed_by=needed_by,
        bill_no=args.bill_no or f"ACME-{args.workflow_id}",
        tolerance_pct=args.tolerance,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workflow-id", required=True)
    p.add_argument("--item", default="WIDGET-A")
    p.add_argument("--supplier", default="Acme Industrial Supply")
    p.add_argument("--qty", type=float, default=10)
    p.add_argument("--rate", type=float, default=25.0)
    p.add_argument("--needed-by", default=None)
    p.add_argument("--bill-no", default=None)
    p.add_argument("--tolerance", type=float, default=2.0)
    p.add_argument("--show-trace", action="store_true")
    args = p.parse_args()

    erp = ERPClient()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1

    spec = build_spec(args)
    llm = OllamaLLM()
    print(f"executor model: {llm.name}   erp: {erp.url}")
    print(
        f"workflow: {spec.workflow_id}  {spec.qty} x {spec.item_code} @ {spec.rate}"
        f" from {spec.supplier}  (expected total {spec.expected_total})"
    )

    with Store() as store:
        machine = WorkflowMachine(store, erp, spec, Executor(llm))
        outcome = machine.run(spec.workflow_id)

        for r in outcome.steps:
            mark = "commit" if r.committed else ("gate" if r.route.value == "commit" else "STOP")
            doc = f"  -> {r.doc_name}" if r.doc_name else ""
            reason = f"  ({r.reason})" if r.reason else ""
            print(
                f"  {r.step.value}  {r.action.value if r.action else '-':<9}"
                f" {mark:<7} {r.latency_ms:>6} ms  attempts={r.attempts}{doc}{reason}"
            )
        if outcome.skipped:
            print(f"  resumed: skipped already-committed {[s.value for s in outcome.skipped]}")

        print(
            f"\nstatus: {outcome.status.value}   total {outcome.total_latency_ms} ms"
            f"   llm_calls={store.llm_calls(spec.workflow_id)}"
        )
        print(f"documents: {outcome.docs}")

        for effect in machine.gl_effect(outcome.docs):
            print(
                f"  GL {effect['voucher_no']}: {effect['lines']} lines"
                f"  Dr {effect['debit']} / Cr {effect['credit']}"
                f"  balanced={effect['balanced']}  {effect['accounts']}"
            )

        if args.show_trace:
            print("\ntrace:")
            for t in TraceLogger(store).reconstruct(spec.workflow_id):
                print(
                    f"  [{t['step']}#{t['attempt']}] {t['action']} "
                    f"prompt={t['prompt_hash'][:12]} resp={t['response_hash'][:12]}"
                )
                print(f"      {t['provenance'].get('rationale', '')}")

    return 0 if outcome.completed else 2


if __name__ == "__main__":
    raise SystemExit(main())
