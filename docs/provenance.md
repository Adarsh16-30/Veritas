# Results provenance map

Every results file records the commit it was produced on (`"commit"` in the JSON,
and `(commit …)` in each `docs/results.md` citation). Rule 10 asks that a reader
can check that commit out. Several of them cannot be: the repository history was
rewritten after the runs (squashing early history, removing references to a
local-only notes file, linearizing a duplicated merge), so the recorded ids no
longer exist in the published history.

This table maps each recorded commit to the published commit holding the same
code. It does not change any measurement. `scripts/check_provenance.py` (run by
`scripts/check_rules.sh`) fails on any recorded commit that is neither reachable
from `HEAD` nor listed here, and on any listed equivalent that is not reachable.

Status:

- **equivalent**: the original commit still exists in a clone, and
  `git diff --stat <recorded> <equivalent>` shows no code change (documentation and
  comments only).
- **inferred**: the original no longer exists in any clone checked so far. The
  equivalent is the published commit whose code the run must have used, from the
  run's date and the commit that recorded it. Confirm by diffing the original
  where it still exists, then mark it equivalent.

| Recorded commit | Results | Code-equivalent commit | Basis | Status |
|---|---|---|---|---|
| `a3a3e8309a76` | `results/archive/evidence-v1-b4-v4/verified_results.json`, `results/archive/evidence-v1-b4-v4/independence_report.json` (v4) | `a2ce7a9` | Pre-rewrite `main`; differs only in 6 doc/comment lines that rename the rules file | equivalent |
| `ee2f4aaffe44` | `results/archive/evidence-v1-b4-v4/baseline_results.json` (b4) | `8693eb0` | b4 baseline was recorded on 2026-09-24 with the evidence-coverage gap fix; that work was squashed into `8693eb0`, which also records the b4 baseline file | inferred |
| `9284c80bd6fb` | `results/archive/phase4-pre-gapfix/*.json` | `72eb60b` | First Phase 4 benchmark; squashed into `72eb60b`, which records these results | inferred |
| `ba2f99f681cf` | `results/baseline_results.json` (b5) | `eede77b` | Evidence version 2 code. Every commit from `eede77b` to `29e6ad3` has the same non-test code except a one-line report-rendering fix (`bench/report.py`) | inferred |
| `3bcff49fe50b` | `results/calibration.json`, `results/calibration_curves.json`, `results/calibration_run.json` (c5) | `eede77b` | As b5 | inferred |
| `684a2e279c6a` | `results/verified_results.json`, `results/independence_report.json` (v5) | `eede77b` | As b5 | inferred |
| `4abe038d2c53` | `results/load_results.json` (l1) | `eede77b` | As b5 | inferred |
| `f655964bb636` | `results/demo/demo_1793362726.json` | `eede77b` | As b5 | inferred |

## Confirming an inferred row

In a clone that still has the original objects (the machine the runs were made
on, before `git gc` prunes them):

```bash
for pair in ee2f4aa:8693eb0 9284c80:72eb60b ba2f99f:eede77b 3bcff49:eede77b \
            684a2e2:eede77b 4abe038:eede77b f655964:eede77b; do
  old=${pair%%:*}; new=${pair##*:}
  if git cat-file -e "$old^{commit}" 2>/dev/null; then
    echo "== $old vs $new"; git diff --stat "$old" "$new" -- . ':!results' ':!docs'
  else
    echo "== $old: not in this clone"
  fi
done
```

No output under a `==` line means the code is identical. Change that row's status
to **equivalent**.
