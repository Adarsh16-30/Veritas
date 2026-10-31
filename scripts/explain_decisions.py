"""Print what the models saw and said for a set of workflows, one step at a time.

    uv run python scripts/explain_decisions.py --prefix b6-bench-clean --step S4
    uv run python scripts/explain_decisions.py --prefix v6-bench-clean --step S1 --stage verifier

A terminal, read-only view of the trace store (the explorer, scripts/trace_explorer.py,
is the full UI). For each matching trace it prints the DELTA facts the model was
given and its raw response, so a pattern like "the executor holds every clean S4"
can be read from the model's own words instead of guessed at.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.db import Store  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--prefix", required=True, help="workflow id prefix, e.g. b6-bench-clean")
    p.add_argument("--step", help="S1..S6; all steps when omitted")
    p.add_argument("--stage", choices=("executor", "verifier"), default="executor")
    p.add_argument("--limit", type=int, default=8)
    args = p.parse_args()
    load_dotenv()
    query = """
        SELECT workflow_id, step, attempt, step_context, response, model, provenance
          FROM traces
         WHERE workflow_id LIKE %s
           AND (%s::text IS NULL OR step = %s)
           AND provenance ->> 'stage' = %s
         ORDER BY workflow_id, step, attempt
         LIMIT %s
    """
    like = args.prefix.replace("%", r"\%").replace("_", r"\_") + "%"
    with Store() as store:
        rows = store.conn.execute(
            query, (like, args.step, args.step, args.stage, args.limit)
        ).fetchall()
    if not rows:
        print("no traces match; check the prefix and step")
        return 1
    for r in rows:
        delta = next(
            (ln for ln in str(r["step_context"]).splitlines() if ln.startswith("DELTA")), ""
        )
        print(f"=== {r['workflow_id']} {r['step']} attempt {r['attempt']} ({r['model']})")
        print(f"  {delta[:900]}")
        response = str(r["response"] or "")
        with contextlib.suppress(ValueError):  # not JSON: print it as it came
            response = json.dumps(json.loads(response), ensure_ascii=False)
        print(f"  response: {response[:900]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
