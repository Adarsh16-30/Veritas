"""Scripted incident replay: baseline vs verified, side by side (PRD Phase 7).

    uv run python -m bench.demo

One workflow from each of five fault classes, on real award records, run
through **both** configurations against the real ERPNext:

* ``clean``                          — a control; both should pay it
* ``adversarial_duplicate``          — the same bill submitted twice
* ``compounding``                    — fine at every step, over budget overall
* ``temporal``                       — required before it can be fulfilled
* ``adversarial_injection_indirect`` — instructions to the agent in the item name

This is a **demonstration, n = 1 per class**, and it says so in its output. It
is not a measurement: the measured numbers, with their citations, are in
``docs/results.md``, and nothing here is ever written there. Each demo run gets
fresh run tags (``db<n>`` / ``dv<n>``) so it never resumes a benchmark workflow,
and it writes only to ``results/demo/`` -- never ``benchmark_ids.json``, which
the calibration's Rule 9 check depends on.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

INCIDENT = (
    "clean",
    "adversarial_duplicate",
    "compounding",
    "temporal",
    "adversarial_injection_indirect",
)


def incident_cases(run_tag: str, seed: int) -> list[Any]:
    """One case per incident variant, on real records (Rule 8)."""
    from data.corpus import load_records
    from harness.corpus import build_split

    cases = build_split(load_records(), f"{run_tag}-demo", 1, random.Random(seed))
    picked = [c for c in cases if c.variant in INCIDENT]
    order = {v: i for i, v in enumerate(INCIDENT)}
    return sorted(picked, key=lambda c: order[c.variant])


def _outcome(record: dict[str, Any]) -> str:
    if record.get("infrastructure_failure"):
        return "INFRA FAILURE (not a decision)"
    if record["terminal_action"] == "proceed":
        return "paid"
    steps = record.get("steps") or []
    where = steps[-1]["step"] if steps else "?"
    reason = (record.get("reason") or "").split(":")[0] or "escalated"
    return f"stopped at {where} ({reason})"


def render_table(rows: list[dict[str, Any]], explorer: str) -> str:
    """The side-by-side story, plain text."""

    def cell(r: dict[str, Any]) -> str:
        return f"{'ok ' if r['correct'] else 'MISS'} {_outcome(r)}"

    # Sized to the content: a truncated reason is a reason the reader never sees.
    cells = [(cell(row["baseline"]), cell(row["verified"])) for row in rows]
    wb = max([len("baseline"), *(len(b) for b, _ in cells)]) + 2
    head = f"{'incident':32} {'should':9} {'baseline':{wb}} verified"
    out = [head, "-" * (len(head) + max([0, *(len(v) for _, v in cells)]))]
    for row, (b, v) in zip(rows, cells, strict=True):
        out.append(f"{row['variant']:32} {row['expected']:9} {b:{wb}} {v}")
    out += [
        "",
        "Demonstration, n = 1 per class -- not a measurement. Measured numbers: docs/results.md",
        f"Inspect any workflow: {explorer}/#/wf/<workflow id>   (scripts/trace_explorer.py)",
    ]
    for row in rows:
        out.append(
            f"  {row['variant']:32} {row['baseline']['workflow_id']}  |  "
            f"{row['verified']['workflow_id']}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--explorer", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    from agent.executor import OllamaLLM
    from bench.conditions import EVIDENCE_VERSION
    from bench.run import _commit_sha, run_case, tag_conflict
    from data.corpus import seed
    from erp.scoped import agent_client, describe
    from harness.corpus import persist_labels
    from orchestrator.db import Store

    erp = agent_client()
    if not erp.ping():
        print(f"ERPNext not reachable at {erp.url}", file=sys.stderr)
        return 1
    executor_model = OllamaLLM().name
    stamp = int(time.time())
    tags = {"baseline": f"db{stamp}", "verified": f"dv{stamp}"}

    by_config: dict[str, list[dict[str, Any]]] = {}
    with Store() as store:
        for config, tag in tags.items():
            conflict = tag_conflict(store, tag, resume=False)
            if conflict:
                print(f"refusing to start: {conflict}", file=sys.stderr)
                return 2
            cases = incident_cases(tag, args.seed)
            seed(erp, list({c.record.item_code: c.record for c in cases}.values()))
            persist_labels(store, cases)
            print(f"\n{config}: {len(cases)} incident workflows (tag {tag})", flush=True)
            records = []
            for case in cases:
                record = run_case(case, store, erp, config, executor_model)
                print(f"  {case.variant:32} {_outcome(record)}", flush=True)
                records.append(record)
            by_config[config] = records

    rows = [
        {
            "variant": b["variant"] if "variant" in b else b["fault_class"],
            "expected": b["expected_terminal_action"],
            "baseline": b,
            "verified": v,
        }
        for b, v in zip(by_config["baseline"], by_config["verified"], strict=True)
    ]
    print("\n" + render_table(rows, args.explorer))

    out_dir = ROOT / "results" / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"demo_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "kind": "demonstration (n=1 per class) -- not a measurement",
                "tags": tags,
                "evidence_version": EVIDENCE_VERSION,
                "erp_access": describe(erp),
                "executor_model": executor_model,
                "commit": _commit_sha(),
                "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "rows": rows,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
