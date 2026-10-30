# Engineering rules

The standing rules for building and changing VERITAS. The full specification is
the PRD (kept outside the repository); this file is the condensed, always-on
ruleset that the code, the tests and `scripts/check_rules.sh` enforce.

## 1. Prime directive

Build for **reliability**, not for a demo. A wrong step in this system moves real
money in a real ledger. Correctness, idempotency and honest measurement beat
speed and feature count every time. If a rule below cannot be satisfied, stop
and raise it — never silently skip it.

## 2. The ten non-negotiable rules

Enforced by `scripts/check_rules.sh` and CI. A violation is a build failure, not
a warning.

1. **No mocked ERP.** Every non-test code path hits the real ERPNext REST API and
   moves the real general ledger. Stubs live only in `tests/`, only in
   `*_synthetic_test.py`.
2. **No fake agent decisions.** Every executor action comes from a real model
   call. No hardcoded action maps.
3. **The verifier is independent.** A different model family or a distinctly
   different adversarial framing, and the executor's rationale is withheld from it.
4. **No improvement claim without a recorded baseline.** A verified-vs-baseline
   delta requires `results/baseline_results.json` to exist and be cited, and both
   runs to share the same recorded conditions (`bench/conditions.py`).
5. **Every side effect is idempotent.** A deterministic idempotency key on every
   ERPNext write; retries and crash-resumes never double-post.
6. **State is durable and crash-safe.** Checkpoint to Postgres *before* any side
   effect; resume without re-executing committed steps.
7. **The agent must stop.** A hard retry cap escalates to a human; every retry and
   escalation is counted; the per-workflow model-call budget is capped.
8. **Real data only in evaluation.** Seeded from cited public procurement data;
   `data/dataset_manifest.json` records source and checksum.
9. **Calibration and benchmark sets are disjoint.** Zero shared workflow IDs.
10. **Every reported number is traceable.** Each metric in `docs/results.md` cites
    a `[results: <file>]` with timestamp and commit.

## 3. Phase discipline

Built in PRD order; a phase starts only when the previous phase's success
criteria pass:

1. Real environment foundation (ERPNext + Postgres + Redis; the buying chain run by hand first)
2. Baseline agent, **no verifier** — the denominator, kept honestly weak
3. Verification gate (rules → independent verifier → conformal calibrator)
4. Fault-injection benchmark (baseline vs. verified)
5. Trace explorer
6. Hardening and observability
7. End-to-end demo and writeup

## 4. Working conventions

- **Small, tested increments.** Every side-effecting path has a test before it is
  done; Rules 5–6 paths have crash and double-dispatch tests.
- **Determinism in benchmarks.** Fixed seeds, at least 30 repetitions per
  configuration, mean ± std — never a single run.
- **Honesty over polish.** If a fault class, the verifier or anything else
  underperforms, record it in `docs/limitations.md`. A stated weakness is a
  credibility signal; a hidden one is the demo trap reviewers look for.
- **Never invent numbers.** Targets live in the PRD; measurements live in results
  files. A target is never presented as a measurement.
- **Mutation-check every guard.** A test that has never failed proves little:
  break the thing it guards once and watch it fail.

## 5. Benchmark operating notes

### 5.1 Run tags and resuming
Workflow ids are `{tag}-{split}-{variant}-{nn}` and every workflow is resumable,
so a new run under a tag already in the state store silently resumes the old
workflows and measures nothing. `bench.run` refuses a fresh run on a used tag, a
`--resume` into a results file holding another tag, and a `--resume` under
different recorded conditions. Burned tags on the development store: b1–b5,
v1–v5, c5 and l1. b4/v4 are evidence version 1; b5/c5/v5 are evidence version 2.
The next runs are b6/c6/v6.

An outage (ERPNext or a model server down) surfaces as a typed
`InfrastructureUnavailable` rather than as an escalation. `bench.run` records it
as `infrastructure_failure`, which is never scored, and `--resume` re-runs it.

### 5.2 Evidence versions
`bench/conditions.py: EVIDENCE_VERSION` is bumped by hand with any change to
what the models see or how the corpus is built. Results files record it with
the ERP access model and the model backend; runs under different conditions are
never compared.

### 5.3 Measurements that look good for the wrong reason
Every serious defect so far has had this shape, and none surfaced as an error.

1. **A heuristic scored against its own payload.** `reads_as_instructions` is a
   pattern list; the direct injection variant largely measures it. The indirect
   variant is phrased outside the list, and a unit test pins that the heuristic
   misses it. Widening the patterns until it passes is overfitting.
2. **Faults that leak between workflows.** Injected items are workflow-private
   (`{code}-INJ-{workflow_id}`); `scripts/repair_poisoned_items.py` resets any
   shared row an older run poisoned.
3. **A fix that passes on a shape production never sends.** Check the live ERP,
   not only the fixture (ERPNext substitutes the item code for an empty name).
4. **A citation that points at nothing.** Rewriting history orphans the commit
   ids that results files record, and nothing breaks syntactically.
   `docs/provenance.md` maps every recorded commit that is not in history, and
   `scripts/check_provenance.py` fails on any that is unmapped. Never rewrite
   published history without updating the map.

### 5.4 The development machine
Two ~5 GB local models with ~5 GB of free RAM: verified workflows take 90–220 s,
mostly model swapping. Executor `llama3:8b-instruct-q4_K_M`, verifier
`qwen2.5-coder:7b` (a different family, Rule 3). Benchmarks run in foreground
chunks with `--resume`; two `bench.run` processes must never share one `--out`.

## 6. Before marking anything done

```bash
bash scripts/check_rules.sh
uv run pytest
uv run ruff format --check . && uv run ruff check . && uv run mypy --strict
uv run python scripts/check_provided_code.py
```

`pytest` runs the store, migration, service and lease tests only when an agent
Postgres is reachable (`POSTGRES_*`) and a `redis-server` binary is on the PATH;
otherwise they skip, and a green run proves less than it appears to. CI provides
both. A change to the image or the chart also needs `docker build .` and the
`release` workflow (helm lint, kubeconform, trivy).
