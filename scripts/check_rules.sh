#!/usr/bin/env bash
# check_rules.sh — enforces the 10 non-negotiable rules (CLAUDE.md §1 / PRD §0).
#
# A rule violation is a BUILD FAILURE, not a warning. Run before marking any task
# done, and in CI. Each check is conservative:
#   PASS  — the required primitive/guard is present and correct-looking
#   SKIP  — the files for that rule's phase do not exist yet (pre-phase)
#   FAIL  — a clear violation
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail=0
green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
pass()  { printf '  %s  %s\n' "$(green PASS)" "$1"; }
skip()  { printf '  %s  %s\n' "SKIP" "$1"; }
bad()   { printf '  %s  %s\n' "$(red FAIL)" "$1"; fail=1; }

# All tracked python files outside tests/ . Falls back to find when not a git repo.
non_test_py() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files '*.py'
  else
    find . -name '*.py' -not -path './.venv/*' -not -path './.git/*'
  fi | grep -v -E '^(\./)?tests/' || true
}

grep_non_test() { non_test_py | tr '\n' '\0' | xargs -0 -r grep -nE "$1" 2>/dev/null || true; }

echo "== VERITAS rule check =="

# RULE 1 — no mocked ERP / fake persistence outside tests/.
h=$(grep_non_test 'BaseHTTPRequestHandler|HTTPServer\(|responses\.activate|requests_mock|httpretty|class +(Fake|Mock|Stub)ERP')
if [ -n "$h" ]; then bad "RULE 1 possible mocked ERP outside tests/:"; echo "$h" | sed 's/^/      /'
else pass "RULE 1 no mocked ERP in non-test code"; fi

# RULE 2 — no hardcoded action map in the executor.
if [ -f agent/executor.py ]; then
  h=$(grep -nE 'if +step *== *["'"'"']?S?[0-9]|ACTION_MAP *=|return +["'"'"']match["'"'"'] *$' agent/executor.py agent/pipeline.py 2>/dev/null || true)
  if [ -n "$h" ]; then bad "RULE 2 hardcoded decision logic:"; echo "$h" | sed 's/^/      /'
  else pass "RULE 2 no hardcoded action map"; fi
else skip "RULE 2 agent/executor.py not present yet (pre-Phase 2)"; fi

# RULE 3 — verifier payload must not carry executor rationale.
if [ -f verify/verifier.py ]; then
  if grep -q 'assert *"rationale" not in payload' verify/verifier.py; then
    pass "RULE 3 verifier excludes executor rationale (guard present)"
  else bad "RULE 3 verify/verifier.py missing the rationale-exclusion assert (PRD §9.4)"; fi
else skip "RULE 3 verify/verifier.py not present yet (pre-Phase 3)"; fi

# RULE 4 — no delta emission without a recorded baseline.
if [ -f bench/run.py ]; then
  if grep -q 'baseline_results' bench/run.py; then pass "RULE 4 baseline gate referenced in bench/run.py"
  else bad "RULE 4 bench/run.py does not reference baseline_results.json"; fi
else skip "RULE 4 bench/run.py not present yet (pre-Phase 4)"; fi

# RULE 5 — idempotent side effects.
if [ -f erp/idempotent.py ]; then
  miss=0
  grep -q 'def idempotency_key' erp/idempotent.py || { bad "RULE 5 idempotency_key() missing"; miss=1; }
  grep -q 'get_committed'       erp/idempotent.py || { bad "RULE 5 committed-key replay (get_committed) missing"; miss=1; }
  grep -q 'record_committed'    erp/idempotent.py || { bad "RULE 5 record_committed() missing"; miss=1; }
  [ "$miss" -eq 0 ] && pass "RULE 5 idempotent write primitive intact"
else skip "RULE 5 erp/idempotent.py not present yet (pre-Phase 2)"; fi

# RULE 6 — checkpoint before side effect.
if [ -f orchestrator/checkpoint.py ]; then
  if grep -q 'def advance' orchestrator/checkpoint.py && grep -q 'checkpoint(' orchestrator/checkpoint.py; then
    # the FIRST checkpoint call must come before the FIRST side_effect() call
    if awk '/def advance/{f=1}
            f && /checkpoint\(/ && !c {c=NR}
            f && /side_effect\(\)/ && !s {s=NR}
            END{exit !(c && s && c < s)}' orchestrator/checkpoint.py; then
      pass "RULE 6 checkpoint precedes side effect in advance()"
    else bad "RULE 6 advance() calls side_effect() before checkpoint()"; fi
  else bad "RULE 6 orchestrator/checkpoint.py missing advance()/checkpoint()"; fi
else skip "RULE 6 orchestrator/checkpoint.py not present yet (pre-Phase 2)"; fi

# RULE 7 — bounded retries; no unbounded retry loop in pipeline/orchestrator.
if [ -f agent/pipeline.py ]; then
  h=$(grep -nE 'while +True' agent/pipeline.py orchestrator/*.py 2>/dev/null | grep -iv 'queue\|blpop\|listen' || true)
  if [ -n "$h" ]; then bad "RULE 7 unbounded loop in pipeline/orchestrator:"; echo "$h" | sed 's/^/      /'
  else pass "RULE 7 no unbounded retry loop"; fi
else skip "RULE 7 agent/pipeline.py not present yet (pre-Phase 2)"; fi

# RULE 8 — no placeholder-data generators outside tests/.
h=$(grep_non_test '\bfaker\b|Faker\(|\blorem\b|fake_vendor|placeholder_(vendor|amount|item)|random_supplier')
if [ -n "$h" ]; then bad "RULE 8 placeholder-data generator outside tests/:"; echo "$h" | sed 's/^/      /'
else pass "RULE 8 no placeholder data generators in non-test code"; fi
if [ -f data/dataset_manifest.json ]; then pass "RULE 8 dataset_manifest.json present"
else skip "RULE 8 data/dataset_manifest.json not present yet (pre-Phase 4 seeding)"; fi

# RULE 9 — calibration and benchmark ID sets disjoint.
if git ls-files 2>/dev/null | grep -qiE 'disjoint'; then pass "RULE 9 disjointness test present"
else skip "RULE 9 disjointness test not present yet (pre-Phase 3)"; fi

# RULE 10 — every reported number cites a results file.
if [ -f docs/results.md ]; then
  # a real reported number carries [metric: ...]; ignore prose that names the
  # token inside backticks while describing the convention itself.
  h=$(grep -nE '\[metric:' docs/results.md | grep -v '\[results:' | grep -vF '`[metric' || true)
  if [ -n "$h" ]; then bad "RULE 10 uncited metric lines in docs/results.md:"; echo "$h" | sed 's/^/      /'
  else pass "RULE 10 all reported numbers cite a results file"; fi
else bad "RULE 10 docs/results.md missing"; fi

echo
if [ "$fail" -ne 0 ]; then echo "$(red 'RULE CHECK FAILED')"; exit 1; fi
echo "$(green 'ALL RULES OK')"
