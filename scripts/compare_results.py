#!/usr/bin/env python
"""Does a fresh benchmark run reproduce a recorded one, within tolerance? (PRD Phase 7)

    uv run python scripts/compare_results.py --before snapshot/ --after results/

Compares the headline metrics of two results directories (baseline and verified
files in each). Each metric is a proportion with its own standard error, so
"within tolerance" means within two combined standard errors, with a floor of
five points: on 44 workflows a single workflow is 2.3 points, and a looser bar
would hide a real change while a tighter one would call noise a failure.

It refuses to compare runs recorded under different conditions
(bench/conditions.py): a different evidence version measures a different thing,
and agreement across it would mean nothing.

Exit code 0 = reproduced; 1 = a headline metric moved beyond tolerance;
2 = the two runs cannot be compared.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.conditions import differences, recorded  # noqa: E402
from bench.metrics import summarise  # noqa: E402

HEADLINE = ("e2e_success", "e2e_success_faulted", "e2e_success_clean", "false_commit_rate")
FLOOR = 0.05


def _load(directory: Path, config: str) -> dict[str, Any]:
    path = directory / f"{config}_results.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _metric(payload: dict[str, Any], name: str) -> dict[str, Any]:
    cap = int(payload.get("llm_call_cap", 18))
    value: dict[str, Any] = summarise(payload["results"], cap)[name]
    return value


def compare(before: Path, after: Path) -> tuple[list[str], list[str]]:
    """``(lines, failures)``: one report line per metric, and the ones that moved."""
    lines: list[str] = []
    failures: list[str] = []
    for config in ("baseline", "verified"):
        old, new = _load(before, config), _load(after, config)
        diff = differences(recorded(old), recorded(new))
        if diff:
            raise ValueError(f"{config}: recorded under different conditions ({'; '.join(diff)})")
        for name in HEADLINE:
            a, b = _metric(old, name), _metric(new, name)
            if not a["n"] or not b["n"]:
                continue
            tol = max(FLOOR, 2 * math.hypot(a["stderr"], b["stderr"]))
            moved = abs(a["rate"] - b["rate"])
            ok = moved <= tol
            lines.append(
                f"  {'ok  ' if ok else 'MOVED'} {config:<8} {name:<20} "
                f"{a['rate']:6.1%} -> {b['rate']:6.1%}  (|d|={moved:.1%}, tol {tol:.1%})"
            )
            if not ok:
                failures.append(f"{config}.{name}")
    return lines, failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--before", type=Path, required=True)
    p.add_argument("--after", type=Path, required=True)
    args = p.parse_args()
    try:
        lines, failures = compare(args.before, args.after)
    except (OSError, KeyError, ValueError) as e:
        print(f"cannot compare: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if failures:
        print(f"\nNOT reproduced: {', '.join(failures)}")
        return 1
    print("\nreproduced within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
