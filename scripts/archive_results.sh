#!/usr/bin/env bash
# archive_results.sh — set the current headline result set aside before a run
# under new conditions.
#
#   bash scripts/archive_results.sh evidence-v2-b5-c5-v5
#
# bench.run refuses to resume into a results file recorded under different
# conditions (bench/conditions.py), so a new evidence version needs empty slots.
# Archiving the calibration matters most: left in place, a verified run whose
# own calibration fit failed would quietly route on a model fitted to the
# previous version's signals.
#
# Files are moved with `git mv`, so their history and the commits they cite stay
# traceable (Rule 10; scripts/check_provenance.py scans results/archive too).
# The report and chart they produced are copied beside them. The load test,
# demo and earlier archives are left where they are.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

name="${1:?usage: archive_results.sh <name>, e.g. evidence-v2-b5-c5-v5}"
case "$name" in */*|.*) echo "archive name must be a plain directory name" >&2; exit 2;; esac
dest="results/archive/$name"
[ -e "$dest" ] && { echo "$dest already exists; choose another name" >&2; exit 2; }

files=(baseline_results.json verified_results.json calibration_run.json calibration.json
       calibration_curves.json independence_report.json ledger_reconciliation.json)
present=()
for f in "${files[@]}"; do [ -f "results/$f" ] && present+=("$f"); done
[ "${#present[@]}" -gt 0 ] || { echo "nothing to archive in results/" >&2; exit 2; }

mkdir -p "$dest"
for f in "${present[@]}"; do
  if git ls-files --error-unmatch "results/$f" >/dev/null 2>&1; then
    git mv "results/$f" "$dest/$f"
  else
    mv "results/$f" "$dest/$f"
  fi
done
cp docs/results.md "$dest/results.md"
cp docs/compounding_curve.svg "$dest/compounding_curve.svg"

uv run python - "$dest" <<'PY'
import json, sys
from pathlib import Path
dest = Path(sys.argv[1])
lines = []
for f in sorted(dest.glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    # Only what the file itself records: a derived report carries no
    # conditions of its own, and guessing them would put a false line here.
    parts = []
    ids = [r.get("workflow_id", "") for r in d.get("results", [])]
    if ids:
        parts.append(f"run tag {ids[0].split('-')[0]}")
    if "evidence_version" in d:
        parts.append(f"evidence version {d['evidence_version']}")
    if d.get("commit"):
        parts.append(f"commit {str(d['commit'])[:12]}")
    if "erp_access" in d:
        parts.append(f"erp access {d['erp_access']}")
    lines.append(f"{f.name}: " + (", ".join(parts) or "derived report; see the run files above"))
lines.append("results.md / compounding_curve.svg here are the report generated from these files.")
(dest / "COMMIT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
PY
git add "$dest"
echo
echo "archived to $dest. Commit it before the next run:"
echo "  git commit -m \"Archive $name results before the next run\" && git push"
