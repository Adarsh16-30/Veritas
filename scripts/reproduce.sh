#!/usr/bin/env bash
# reproduce.sh — PRD Phase 7: "clone, run one command, reproduce the headline numbers".
#
#   bash scripts/reproduce.sh          # seconds, no services: recompute every
#                                      # published number from the committed results
#                                      # files and fail on any drift
#   bash scripts/reproduce.sh --full   # hours, needs the stack + Ollama: rerun the
#                                      # whole benchmark live, then check the new
#                                      # headline numbers against the recorded ones
#
# The quick mode proves the report is a pure function of the results files
# (Rule 10). The full mode proves the results files can be produced again.
# Different questions, both of which a reviewer will ask.
#
# --full compares only runs recorded under the same conditions: rerunning on a
# newer EVIDENCE_VERSION than the recorded files measures something else, and
# compare_results.py refuses it (exit 2) rather than call that reproduction.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "${1:-}" != "--full" ]; then
  step "1/3  regenerate docs/results.md from results/ and compare"
  uv run python -m bench.report --results-dir results --report "$tmp/results.md" >/dev/null
  if ! diff -q docs/results.md "$tmp/results.md" >/dev/null; then
    echo "    docs/results.md does NOT match what results/ produces:"
    diff docs/results.md "$tmp/results.md" | head -20
    exit 1
  fi
  echo "    identical"

  step "2/3  regenerate the compounding-failure chart and compare"
  uv run python -m bench.plots --results-dir results --out "$tmp/curve.svg" >/dev/null
  cmp -s docs/compounding_curve.svg "$tmp/curve.svg" || {
    echo "    docs/compounding_curve.svg does NOT match results/"; exit 1; }
  echo "    identical"

  step "3/3  every cited commit can still be checked out (Rule 10)"
  uv run python scripts/check_provenance.py

  printf '\n\033[32mreproduced: every published number follows from the committed results\033[0m\n'
  echo "For a live re-run of the benchmark itself: bash scripts/reproduce.sh --full"
  exit 0
fi

step "snapshot the recorded results"
mkdir -p "$tmp/before"
cp results/baseline_results.json results/verified_results.json "$tmp/before/"

: "${VERITAS_BASELINE_TAG:?set fresh run tags, e.g. VERITAS_BASELINE_TAG=r1b VERITAS_VERIFIED_TAG=r1v VERITAS_CAL_TAG=r1c}"
: "${VERITAS_VERIFIED_TAG:?}"
: "${VERITAS_CAL_TAG:?}"

step "run the whole benchmark live (scripts/run_benchmark.sh)"
bash scripts/run_benchmark.sh

step "compare the new headline numbers with the recorded ones"
uv run python scripts/compare_results.py --before "$tmp/before" --after results
