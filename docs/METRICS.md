# METRICS.md — every VERITAS metric, defined before any agent code exists

This file is written in **Phase 1**, deliberately before the executor, verifier,
or benchmark exist, so that no metric can be reverse-engineered to flatter a
result. Every number reported anywhere (README, `docs/results.md`, resume) must
map to exactly one definition below and cite a raw results file (Rule 10).

Conventions:

- **Reps.** Every benchmark metric is reported as `mean ± std` over **≥ 30
  repetitions** per configuration, fixed seeds (CLAUDE.md §5). A single run is
  never reported.
- **Configs.** `baseline` = Phase 2 agent, no verification gate. `verified` =
  full three-gate pipeline. Deltas are always `verified − baseline` and require
  `results/baseline_results.json` to exist (Rule 4).
- **Ground truth.** The fault-injection harness writes a `labels` row per
  workflow: `{fault_class, expected_terminal_action}` (PRD §3.3, §5.1).
- **Step index.** `S1` requisition · `S2` policy check · `S3` PO · `S4` three-way
  match · `S5` discrepancy resolution · `S6` payment release.

---

## 1. Success / reliability

### 1.1 Per-step success rate — `step_success[s]`
Fraction of step executions whose committed action equals the label's expected
action for that step, among steps the workflow actually reached.

```
step_success[s] = correct_action_at[s] / reached[s]
```
Source: `step_attempts` (`committed`, `action`) joined to `labels`.
Reported per `s ∈ {S1..S6}`, per config.

### 1.2 End-to-end success rate — `e2e_success`
Fraction of workflows whose **terminal** action equals
`labels.expected_terminal_action` (a correct payment release **or** a correct
refusal/escalation both count as success — the agent is right when it does the
right thing, including stopping).

```
e2e_success = workflows_correct_terminal / workflows_total
```
Target (PRD §1): verified path materially higher than baseline; full delta reported.

### 1.3 Compounding-failure curve — `survive[k]`
Empirical probability that the **first k steps are all correct**:

```
survive[k] = |{ w : action correct at S1..Sk }| / workflows_total,  k = 1..6
```
Plotted for `baseline` and `verified` on one axis — this is the headline plot
(PRD §1, §5.3). The naive expectation `p^k` for per-step reliability `p` is drawn
as a reference line.

### 1.4 False-commit rate — `false_commit_rate`  ← the number that matters most
Fraction of **committed** actions that were wrong, over the adversarial fault set.

```
false_commit_rate = wrong_commits / total_commits          (adversarial subset)
```
A commit is *wrong* if it submitted an ERPNext document the label says should
have been held, refused, or escalated. **Target ≤ 2%** (PRD §1).

### 1.5 Incorrect ledger postings — `bad_postings`
Count, across all fault runs, of GL-moving writes that are duplicates or
wrong-amount relative to the source document.

```
bad_postings = duplicate_postings + wrong_amount_postings
```
**Target = 0** (PRD §1). Measured by `bench/reconcile_ledger.py`, not by the agent.

---

## 2. Escalation quality

### 2.1 Escalation precision — `esc_precision`
```
esc_precision = escalations_that_should_have_escalated / total_escalations
```

### 2.2 Escalation recall — `esc_recall`
```
esc_recall = faults_correctly_escalated / faults_whose_label_requires_escalation
```

### 2.3 Forced-stop counters (Rule 7) — `forced_escalation_total`, `step_retry_total`
Prometheus counters scraped during CI load tests. Reported as totals and as
per-workflow rates. No workflow may exceed the configured **max LLM-calls
budget** (`llm_calls_per_workflow ≤ CAP`); violations are a bug, reported as
`budget_violations` (target 0).

Exported by `scripts/metrics_exporter.py` as `veritas_forced_escalation_total`
(labelled by escalation reason class) and `veritas_step_retry_total` (by step),
plus `veritas_budget_violations` against `veritas_llm_call_cap`. They are
derived from the durable Postgres record on every scrape, with the same
definitions `bench.metrics.budget` uses: a forced escalation is any workflow
that ended escalated, and a retry is any attempt beyond the first at a step.

---

## 3. Calibration (Phase 3, post-conformal)

### 3.1 Expected Calibration Error — `ECE`
Partition the N predictions into M equal-width confidence bins `B_1..B_M`.

```
ECE = Σ_{m=1..M} (|B_m| / N) · | acc(B_m) − conf(B_m) |
```
where `acc(B_m)` is the empirical correctness rate in the bin and `conf(B_m)` the
mean predicted confidence. **Target ≤ 0.05** post-calibration (PRD §1). Reported
pre- and post-conformal with `M = 10`.

### 3.2 Brier score — `brier`
```
brier = (1/N) Σ_{i=1..N} (p_i − y_i)^2 ,   y_i ∈ {0,1}
```
Reported pre- and post-conformal.

### 3.3 Conformal coverage — `coverage`
Fraction of prediction regions that contain the true label.

```
coverage = |{ i : y_i ∈ region_i }| / N
```
**Target ≥ 1 − α**, with `α` the configured miscoverage level. Measured on a
calibration/benchmark **disjoint** split (Rule 9); the disjointness test must
pass before this metric is emitted.

### 3.4 Region efficiency — `avg_region_size`
Mean number of labels per prediction region (smaller = more decisive). Reported
alongside coverage so a trivially-wide region can't masquerade as good calibration.

---

## 4. Verifier independence (Rule 3)

### 4.1 Executor↔verifier agreement on known-wrong cases — `ev_corr`
Restrict to steps whose label says the executor's proposed action is wrong.
Let `x_i = 1` if the executor proposed *commit*, `v_i = 1` if the verifier
returned *pass*. Report the phi coefficient (Pearson on the two binaries):

```
ev_corr = phi(x, v)   over the known-wrong subset
```
Must stay **below a configured ceiling**. A verifier that passes whatever the
executor proposes drives `ev_corr → 1` and fails the build.

### 4.2 Payload leakage check — `verifier_payload_clean`
Boolean. Asserts `build_verifier_payload` output contains no executor-rationale
field (PRD §9.4). Must be `true`.

---

## 5. Detection by fault class (the honest weakness map)

For each class `c` in {missing, conflicting, ambiguity, adversarial, boundary,
temporal, compounding} (PRD §6.3):

```
detection[c] = faults_of_class_c_caught / faults_of_class_c_total
```
"Caught" = the agent held / refused / escalated where the label required it.
Reported as a table for `baseline` and `verified`. Low cells are recorded in
`docs/limitations.md`, not hidden (CLAUDE.md §5).

---

## 6. Performance

### 6.1 End-to-end latency — `latency_p50`, `latency_p95`
Wall-clock from workflow enqueue to terminal action.
```
latency_p95 = percentile(workflow_wall_clock, 95)
```
**Target `latency_p95` < 45 s** (PRD §1).

### 6.2 Per-stage latency
Median wall-clock of each pipeline stage (`Checkpointer`, `ContextAssembler`,
`Executor`, `RuleGate`, `IndependentVerifier`, `ConformalRouter`,
`IdempotentCommitter`, `TraceLogger`) from OpenTelemetry spans.

### 6.3 Sustained throughput — `max_concurrent`
Largest number of concurrently in-flight workflows completed without error growth
or latency-p95 breach, with **4 workers**. **Target ≥ 50** (PRD §1).

---

## 7. Ledger reconciliation (`bench/reconcile_ledger.py`)

Run after every benchmark config:

| Check | Pass condition |
|---|---|
| Duplicate `Payment Entry` for one logical action | count = 0 |
| `Payment Entry` amount over three-way-match tolerance | count = 0 |
| GL debits − credits per voucher | = 0 (double-entry holds) |
| Sum of agent-created `Payment Entry` vs. matched `Purchase Invoice` | equal |

Any exception must be individually explained in `docs/results.md` (PRD §4 success criteria).

---

## 8. Reporting rules (Rule 10)

- Every metric line in `docs/results.md` is written as
  `... [metric: <name>] ... [results: results/<file>.json] (commit <sha>, <ts>)`.
- `check_rules.sh` fails the build if a `[metric:` line lacks a `[results:` citation.
- Targets in this file are **targets**, sourced from the PRD. They are never
  written into a results file or presented as measured values.
