#!/usr/bin/env bash
# run_benchmark.sh — the whole Phase 4 benchmark, from one command.
#
#   bash scripts/run_benchmark.sh
#
# PRD §7 Phase 4 requires both configurations benchmarked and reproducible from
# one command. The order below is not arbitrary — each step depends on the one
# before it:
#
#   1. fetch the real corpus (Rule 8) if it is not already cached
#   2. baseline over the benchmark split  -> the Rule 4 denominator
#   3. verified over the *calibration* split -> gate signals to calibrate on
#   4. fit the conformal router on that split (Rule 9: disjoint from benchmark)
#   5. verified over the benchmark split, now calibrated
#   6. reconcile the real ledger, then write the report and the plot
#
# Every run is resumable: workflows already committed are skipped (Rule 6), so
# re-running after an interruption continues rather than starting over.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SEED="${VERITAS_SEED:-7}"
BASE_TAG="${VERITAS_BASELINE_TAG:-b2}"
VER_TAG="${VERITAS_VERIFIED_TAG:-v2}"
CAL_TAG="${VERITAS_CAL_TAG:-c2}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "1/6  real procurement corpus (Rule 8)"
if [ -f data/raw/usaspending_awards.json ]; then
  echo "    cached corpus present; skipping fetch (delete data/raw to re-fetch)"
else
  uv run python -m data.usaspending --limit 200
fi

step "2/6  baseline over the benchmark split (the Rule 4 denominator)"
uv run python -m bench.run --config baseline --split benchmark \
  --seed "$SEED" --run-tag "$BASE_TAG" --out results/baseline_results.json

step "3/6  verified over the calibration split (collects gate signals)"
uv run python -m bench.run --config verified --split calibration \
  --seed "$SEED" --run-tag "$CAL_TAG" --out results/calibration_run.json

step "4/6  fit the conformal router (Rule 9: benchmark-disjoint)"
uv run python scripts/calibrate.py \
  --benchmark-ids results/benchmark_ids.json \
  --out results/calibration.json \
  --curves results/calibration_curves.json || {
    echo "    calibration did not produce a model; the verified run will proceed"
    echo "    uncalibrated and will say so (see docs/limitations.md)."
  }

step "5/6  verified over the benchmark split"
uv run python -m bench.run --config verified --split benchmark \
  --seed "$SEED" --run-tag "$VER_TAG" --out results/verified_results.json

step "6/6  reconcile the ledger, then report"
uv run python -m bench.reconcile_ledger --out results/ledger_reconciliation.json || true
uv run python scripts/independence_report.py --out results/independence_report.json || true
uv run python -m bench.plots --results-dir results --out docs/compounding_curve.svg
uv run python -m bench.report --results-dir results --report docs/results.md

printf '\n\033[1mdone.\033[0m results/ holds the raw runs; docs/results.md cites them.\n'
