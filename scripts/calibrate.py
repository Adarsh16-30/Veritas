#!/usr/bin/env python
"""Fit the conformal router from recorded, labelled runs (PRD §7 Phase 3, Rule 9).

    uv run python scripts/calibrate.py --alpha 0.1 --out results/calibration.json

This reads the signal vectors the verified pipeline already recorded on every
step attempt, joins them to the ground-truth `labels` rows, fits a split-conformal
model, and writes the artifact `verify.gate` loads at runtime.

It refuses, loudly, rather than producing a number it cannot stand behind:

* no labelled workflows              -> nothing to calibrate against
* fewer than `--min-records` rows    -> a conformal guarantee from a handful of
                                        points is a number, not a guarantee
* any overlap with the benchmark IDs -> Rule 9; leakage inflates coverage
* no held-out split left over        -> the pre/post curves would be fitted on
                                        their own training data

Until the fault-injection harness lands (Phase 4) there are no labels, so this
script exits non-zero and says so. That is the correct behaviour: the gate then
runs uncalibrated and declares itself uncalibrated, instead of routing on a
threshold somebody picked by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.db import Store  # noqa: E402
from verify.conformal import (  # noqa: E402
    COMMIT,
    ESCALATE,
    CalibrationRecord,
    Calibrator,
    LeakageError,
    NotCalibrated,
    evaluate,
)

#: Terminal actions that mean "this workflow should have gone through".
COMMIT_LABELS = {"proceed", "commit", "pay", "release"}


def load_records(store: Store) -> list[CalibrationRecord]:
    """Join recorded gate signals to ground-truth labels.

    Per-step truth is derived from the workflow label: every step before the
    labelled fault step should have committed, and the fault step and everything
    after it should not have. A label without a `fault_step` is treated as
    clean-throughout only when its terminal action is a commit — guessing where
    an unlocated fault bit would be inventing ground truth.
    """
    rows = store.conn.execute(
        """
        SELECT a.workflow_id, a.step, a.signals, l.fault_class,
               l.expected_terminal_action, l.fault_step
          FROM step_attempts a
          JOIN labels l ON l.workflow_id = a.workflow_id
         WHERE a.signals <> '{}'::jsonb
         ORDER BY a.workflow_id, a.step, a.attempt
        """
    ).fetchall()

    records: list[CalibrationRecord] = []
    for r in rows:
        terminal = str(r["expected_terminal_action"] or "").strip().lower()
        fault_step = r["fault_step"]
        if fault_step:
            truth = ESCALATE if str(r["step"]) >= str(fault_step) else COMMIT
        elif terminal in COMMIT_LABELS:
            truth = COMMIT
        else:
            # An escalation label with no located fault step cannot be turned
            # into per-step truth without guessing. Skip it rather than guess.
            continue
        records.append(
            CalibrationRecord(
                workflow_id=str(r["workflow_id"]),
                step=str(r["step"]),
                signals={k: float(v) for k, v in dict(r["signals"]).items()},
                truth=truth,
            )
        )
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--alpha", type=float, default=0.1, help="miscoverage level (default 0.1)")
    ap.add_argument("--out", default="results/calibration.json")
    ap.add_argument("--curves", default="results/calibration_curves.json")
    ap.add_argument(
        "--benchmark-ids",
        default="",
        help="path to a JSON list of benchmark workflow IDs (Rule 9 disjointness)",
    )
    ap.add_argument("--holdout-fraction", type=float, default=0.3)
    ap.add_argument("--min-records", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    load_dotenv()
    benchmark_ids: list[str] = []
    if args.benchmark_ids:
        benchmark_ids = json.loads(Path(args.benchmark_ids).read_text(encoding="utf-8"))

    with Store() as store:
        records = load_records(store)

    if not records:
        print(
            "no labelled gate signals found.\n"
            "  The conformal calibrator needs a labelled split that is disjoint from\n"
            "  the benchmark (Rule 9). Those labels come from the fault-injection\n"
            "  harness, which is Phase 4. Until then the gate runs uncalibrated and\n"
            "  says so — no threshold is invented here.",
            file=sys.stderr,
        )
        return 1

    # Hold out a slice for the pre/post curves, disjoint from what we fit on.
    ids = sorted({r.workflow_id for r in records})
    import random

    random.Random(args.seed).shuffle(ids)
    cut = int(len(ids) * (1 - args.holdout_fraction))
    fit_ids, holdout_ids = set(ids[:cut]), set(ids[cut:])
    fit_records = [r for r in records if r.workflow_id in fit_ids]
    holdout = [r for r in records if r.workflow_id in holdout_ids]

    try:
        cal = Calibrator(alpha=args.alpha, seed=args.seed, min_records=args.min_records).fit(
            fit_records, benchmark_ids=benchmark_ids
        )
        curves = evaluate(cal, holdout, benchmark_ids=benchmark_ids)
    except LeakageError as e:
        print(f"RULE 9 VIOLATION: {e}", file=sys.stderr)
        return 2
    except NotCalibrated as e:
        print(f"cannot calibrate: {e}", file=sys.stderr)
        return 1

    out = Path(args.out)
    cal.save(out)
    Path(args.curves).parent.mkdir(parents=True, exist_ok=True)
    Path(args.curves).write_text(
        json.dumps(curves.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"calibrated on {cal.n_train} train / {cal.n_calibration} calibration rows")
    print(f"  alpha={cal.alpha}  qhat={cal.qhat:.4f}  workflows={len(cal.workflow_ids)}")
    print(f"  ECE   {curves.ece_pre:.4f} -> {curves.ece_post:.4f}   (n={curves.n})")
    print(f"  Brier {curves.brier_pre:.4f} -> {curves.brier_post:.4f}")
    print(f"  coverage {curves.coverage:.4f} (target >= {1 - cal.alpha})")
    print(f"  mean region size {curves.avg_region_size:.3f}")
    print(f"wrote {out} and {args.curves}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
