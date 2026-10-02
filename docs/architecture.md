# architecture.md

The authoritative spec is `VERITAS_PRD.md` §3–§5. This file is the working
summary; when they disagree, the PRD wins.

## Five layers (PRD §3.1)

| Layer | Package(s) | Responsibility |
|---|---|---|
| 1 · System of record | `erp/` | Typed ERPNext REST client. Every side effect submits a DocType and moves the GL. No mock (Rule 1). |
| 2 · Orchestration | `orchestrator/` | Durable `S1..S6` state machine. Checkpoint to Postgres **before** any side effect (Rule 6). Idempotency keys on every write (Rule 5). Redis queue + per-workflow advisory lock. |
| 3 · Executor | `agent/` | `context.py` assembles step-scoped ERPNext data + precomputed `DELTA:` facts. `executor.py` makes one real LLM call → `{action, args, rationale}` (Rule 2). |
| 4 · Verification gate | `verify/` | `rules.py` deterministic accounting invariants (terminal on violation) → `verifier.py` independent model/framing, executor rationale withheld (Rule 3) → `conformal.py` calibrated region → route. |
| 5 · Observability & trace | `trace/`, `bench/`, `ui/` | Append-only trace store; Prometheus/Grafana; fault-injection benchmark; trace explorer. |

No layer bypasses another. `harness/` (fault injector + labels) and `data/`
(real dataset loaders) feed evaluation only.

## Step pipeline (PRD §4.3)

```
run_step(ctx):
  checkpoint(ctx)                 # Rule 6 — before any side effect
  assemble_context(ctx, erp)      # step-scoped ERPNext data + DELTA facts
  execute(ctx)                    # Rule 2 — one real LLM proposal
  if not rule_gate(ctx): escalate(reason="hard_rule_violation")
  verify_independently(ctx)       # Rule 3 — different model, no rationale
  region = calibrate(ctx)         # conformal confidence
  route(region) -> COMMIT | RETRY (bounded, Rule 7) | ESCALATE
  commit_idempotent(ctx, erp)     # Rule 5
  trace_log(ctx)
```

## Durable state (PRD §3.3)

`workflows` · `step_attempts` · `traces` (append-only) · `labels`. All in the
agent Postgres (`agent-postgres`, port 5433) — never in ERPNext's DB.

## Provided primitives — do not modify (PRD §9)

- `erp/idempotent.py` — `idempotency_key()` + `submit_once()` (Rule 5)
- `orchestrator/checkpoint.py` — `advance()` checkpoint-before-effect (Rule 6)
- `verify/conformal.py` — `route()` (added in Phase 3)
- `verify/verifier.py` — `build_verifier_payload()` rationale-exclusion guard (Phase 3)

## Build order

Phases are sequential (PRD §7). This repo is at **Phase 4 — fault-injection
benchmark**.

Built: `erp/` (typed client + idempotent writes), `orchestrator/` (durable
S1..S6 machine, Postgres state, Redis queue and per-workflow lock, model-call
budget), `agent/` (context assembler, executor, pipeline), `trace/`
(append-only store), `verify/` (rule engine, independent verifier, conformal
router), `data/` (real procurement corpus + ERPNext seeding), `harness/` (fault
taxonomy + labelled corpus), `bench/` (runner, metrics, reconciliation, report,
plot).

Not built: `ui/` — the Phase 5 trace explorer — and Phase 6 hardening.

## The verification gate (Phase 3)

Three gates run between `execute` and `commit`, in PRD §4.3 order:

| Gate | Module | What it does | On failure |
|---|---|---|---|
| RuleGate | `verify/rules.py` | Declarative invariants (`invariants.yaml`) over the DELTA facts, **plus a fresh read of the real ledger** | Terminal — escalate, never retry |
| IndependentVerifier | `verify/verifier.py` | Different model family + adversarial framing, executor rationale withheld (Rule 3) | Retry with the objection re-injected, then escalate |
| ConformalRouter | `verify/conformal.py` | Split-conformal prediction region → `route()` (PRD §9.3) | Ambiguous or empty region → escalate |

Composed in `verify/gate.py`; `agent/pipeline.py` knows only that something may
veto a step.

### The floor a fitted model cannot cross

`route()` (PRD §9.3) decides purely from the conformal region. Before a
calibration model exists, the region comes from `unanimous_region`, which
*structurally* requires the rule gate, the verifier and the executor to all
agree before a region can even contain `COMMIT`. Once `scripts/calibrate.py`
fits a real model (Phase 4), the region instead comes from learned weights over
seven signals — and a fitted model has no such guarantee built in. A synthetic
fit where truth happens to correlate weakly with `verifier_pass` and
`executor_proceed` can produce a confident `COMMIT` region even when the
verifier explicitly failed the step or the executor itself proposed `hold`.

`VerificationGate.evaluate` enforces two floors on top of whatever the region
says: a commit requires `verdict.passed` (Rule 3 — a commit **must** pass the
verifier) and requires the executor to have actually proposed `PROCEED` (Rule
2 — a downstream router does not get to overturn the agent's own refusal). The
region itself is left untouched for audit and for Phase 4's calibration
curves; only the *route the pipeline acts on* is forced. Tests:
`tests/unit/gate_routing_synthetic_test.py::test_a_fitted_model_cannot_commit_over_a_failed_verifier`
and `::test_a_fitted_model_cannot_commit_over_an_executor_that_declined`.

### Why the gate is optional

`Pipeline(gate=None)` is the Phase 2 baseline and `Pipeline(gate=...)` is the
verified configuration — the same code path either way. PRD §5.2's
`--config baseline` and `--config verified` therefore differ by one constructor
argument, so the Phase 4 delta measures the gate's effect rather than an
incidental difference between two implementations. This is also why no
verification logic may live in `agent/`.

### Three guarantees the idempotency layer cannot give

1. **A stale read is not a guard.** `ContextAssembler` computes its facts when
   the step is assembled. Between then and the commit, another worker or a human
   in the ERPNext UI can change the answer. `LedgerGuard` re-asks ERPNext at gate
   time, immediately before the write.
2. **A different workflow derives a different key.** `submit_once` keys on
   (workflow, step, inputs), so a *second* workflow paying an
   already-paid invoice looks like a first-time write and sails through. The
   deterministic duplicate-payment guard is the only thing that stops it. This is
   proven against the real ledger in
   `tests/integration/test_verified_gate_integration.py`.

3. **A resumed step may be unable to rebuild the document it is retrying.**
   Constructing an ERPNext document is itself an ERPNext call — the
   `make_purchase_invoice` family maps a source document into a target — and
   after a partial commit those mappers can fail outright. A Purchase Order that
   an orphaned invoice already billed in full has nothing left to map, and
   returns a 500. So the committer resolves "has this logical write already
   happened?" *before* building: `orchestrator/steps.py` plans a write as
   (doctype, slot, key, `build` thunk), all but the thunk computable without
   touching ERPNext, and `Pipeline._resolve_existing` checks Postgres and then
   the ledger first. Building eagerly meant the 500 landed before the resume
   could notice the document already existed, the retry cap burned, and a
   recoverable crash became a human escalation. Regression:
   `tests/fault_injection/test_crash_resume_integration.py::test_crash_after_the_invoice_resumes_instead_of_escalating`.

### Calibration status

The conformal router needs a labelled split disjoint from the benchmark
(Rule 9). That corpus is Phase 4, so no calibration artifact exists yet and
`verify/gate.py` runs **uncalibrated**: `unanimous_region` requires all three
gates to agree before a commit, and marks the region `calibrated=False`.
Every metric path refuses an uncalibrated region rather than reporting coverage
the method never promised. `scripts/calibrate.py` fits the real artifact and
exits non-zero until the labels exist.

## The benchmark (Phase 4)

    bash scripts/run_benchmark.sh

| Layer | Module | Role |
|---|---|---|
| Corpus | `data/usaspending.py` | Fetches real federal contract awards once, caches them, writes `dataset_manifest.json` with source, request and sha256 (Rule 8) |
| Corpus | `data/corpus.py` | Verifies the cache against its checksum, seeds real suppliers and line items into ERPNext |
| Faults | `harness/faults.py` | PRD §6.3's seven classes, each injected as a real procurement request or real ERPNext state |
| Corpus | `harness/corpus.py` | Labelled workflows in two splits sharing zero IDs (Rule 9); writes ground truth to `labels` |
| Runner | `bench/run.py` | One configuration over one split; `gate=None` vs a real gate is the only difference between them |
| Metrics | `bench/metrics.py` | Every `docs/METRICS.md` definition, and nothing else |
| Ledger | `bench/reconcile_ledger.py` | Asks the real GL what happened rather than trusting the agent's own log |
| Report | `bench/report.py` | Writes `docs/results.md`; refuses a delta without a recorded baseline (Rule 4) |

### A fact the model never sees is a fact the verifier cannot use

The first benchmark run left four classes at 0/4 in both configurations. The
cause was not model capability: `missing`, `ambiguity` and
`adversarial_injection` all corrupt the **item name**, which reached the model
only as free text in the summary, and `compounding` turns on the approved
authority, which S5 and S6 never carried forward. Every DELTA fact those steps
emitted was `True`, honestly.

The verifier's recorded answers show why that is fatal rather than merely
unhelpful. It must ground each checklist answer in a piece of evidence, and it
did — `item_is_purchasable=True`, `qty_positive=True`, `needed_by_not_past=True`,
confidence 1.0. The grounding protocol is what makes the verifier reliable on
the classes it does catch, and it is exactly what leaves it mute when no fact
describes the thing that is wrong.

So the assembler emits two description facts at S1 and the approved authority at
S5/S6, and each one has a matching checklist expectation — a fact the checklist
never asks about is invisible to the verifier. The authority comparison is pure
arithmetic once the figure is present, so it is also a hard invariant; the two
description facts are judgement inputs and deliberately are not, because a
terminal accounting rule should not be deciding whether a description reads as
informative.

This is why `agent/context.py` is the one module shared by both configurations
and why changing it invalidates a recorded baseline: it defines what *either*
configuration is able to notice.

### Ground truth comes from the injector

Each fault declares `fault_step` — the first step at which the evidence is
sufficient to know something is wrong — and `expected_terminal_action`. Per-step
scoring follows from that: every step *before* the fault should commit, the
fault step itself must not, and steps never reached are not scored at all.
Counting an unreached step as either a success or a failure would be inventing
data.

Faults also declare `rule_detectable`. Twelve of the forty benchmark workflows
are catchable by the deterministic rule engine on its own; the other twenty-eight
need judgement. Reporting those together would let the rule engine's work read as
the verifier's, so the weakness map separates them.

### Why the two configurations use different workflow IDs

Workflows are resumable (Rule 6), and the supplier bill number is derived from
the workflow ID. Running both configurations over one set of IDs would have the
second resume the first's committed steps and measure nothing — and would have it
trip the duplicate-bill guard on the first's own invoices, scoring a fault the
harness never injected. `--run-tag` namespaces them.

### What the three-way match needed before it could be benchmarked

Until Phase 4 the assembler derived the received quantity and the invoiced
amount from the same `WorkflowSpec` fields the purchase order was built from, so
`qty_match` was always true and `amount_variance` always exactly zero, for every
workflow, by construction. A match whose two compared legs are definitionally
equal cannot detect a short delivery or an overbill because it cannot represent
one — and three of the seven fault classes were unrepresentable as a result.
`WorkflowSpec.received_qty` and `invoice_rate` make the delivery note and the
supplier invoice independent evidence, and the receipt and invoice are posted to
the real ledger carrying those values.

## Provided primitives — do not modify (PRD §9)

- `erp/idempotent.py` — `idempotency_key()` + `submit_once()` (Rule 5)
- `orchestrator/checkpoint.py` — `advance()` checkpoint-before-effect (Rule 6)
- `verify/conformal.py` — `route()` (Phase 3)
- `verify/verifier.py` — `build_verifier_payload()` rationale-exclusion guard (Phase 3)

Each block sits behind a bare `# fmt: off` directive and is byte-identical to the
PRD. `scripts/check_provided_code.py` diffs all four on every rule check —
added after `ruff format` silently reflowed comment alignment inside them.
