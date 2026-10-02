# results.md

Every number below is measured. Targets live in `VERITAS_PRD.md` and `docs/METRICS.md` and are never written here (Rule 10).

Corpus: real U.S. federal contract awards from USAspending.gov, seeded into the real ERPNext instance — source, request and checksum in `data/dataset_manifest.json` (Rule 8). Faults are injected as real ERPNext documents and real procurement requests (PRD §6.3); ground truth comes from the injector that created each condition.

**Conformal router: UNCALIBRATED.** No `results/archive/phase4-pre-gapfix/calibration.json` exists, so the gate routed through `verify.conformal.unanimous_region` — all three gates must agree to commit — rather than through a fitted prediction region. The verified numbers below are for that configuration. No coverage or ECE figure is reported, because an uncalibrated region makes no such claim and `verify.conformal.coverage()` refuses to score one.

## Headline

| Metric | Baseline | Verified | Delta |
|---|---|---|---|
| end-to-end success [metric: e2e_success] | 47.5% ± 7.9 (n=40) | 57.5% ± 7.8 (n=40) | +10.0 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| — on faulted workflows [metric: e2e_success_faulted] | 37.5% ± 8.6 (n=32) | 50.0% ± 8.8 (n=32) | +12.5 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| — on clean controls [metric: e2e_success_clean] | 87.5% ± 11.7 (n=8) | 87.5% ± 11.7 (n=8) | +0.0 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| false-commit rate [metric: false_commit_rate] | 70.3% ± 3.8 (n=148) | 64.5% ± 4.3 (n=124) | -5.8 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| escalation precision [metric: esc_precision] | 92.3% ± 7.4 (n=13) | 94.1% ± 5.7 (n=17) | +1.8 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| escalation recall [metric: esc_recall] | 37.5% ± 8.6 (n=32) | 50.0% ± 8.8 (n=32) | +12.5 pp | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

## Compounding-failure curve

Fraction of workflows still correct at every one of their first k steps.

| k | Baseline | Verified | source |
|---|---|---|---|
| 1 | 60.0% ± 7.7 (n=40) | 70.0% ± 7.2 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| 2 | 60.0% ± 7.7 (n=40) | 70.0% ± 7.2 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| 3 | 60.0% ± 7.7 (n=40) | 70.0% ± 7.2 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| 4 | 57.5% ± 7.8 (n=40) | 67.5% ± 7.4 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| 5 | 47.5% ± 7.9 (n=40) | 57.5% ± 7.8 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| 6 | 47.5% ± 7.9 (n=40) | 57.5% ± 7.8 (n=40) | [metric: survive] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

[results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

## Detection by fault class

The honest weakness map (PRD §5.3). `rules` marks the classes the deterministic rule engine can catch on its own — the classes where it cannot are where a verification gate has to earn its place.

| Class | Expected | Rules? | Baseline | Verified | source |
|---|---|---|---|---|---|
| adversarial_duplicate | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| adversarial_injection | escalate | no | 0.0% ± 0.0 (n=4) | 0.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| ambiguity | escalate | no | 0.0% ± 0.0 (n=4) | 0.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| boundary_at_tolerance | proceed | no | 75.0% ± 21.7 (n=4) | 75.0% ± 21.7 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| boundary_budget_zero | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| clean | proceed | no | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| compounding | escalate | no | 0.0% ± 0.0 (n=4) | 0.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| conflicting | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| missing | escalate | no | 0.0% ± 0.0 (n=4) | 0.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)
| temporal | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

[results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

## Latency and stopping

| Metric | Baseline | Verified |
|---|---|---|
| median end-to-end [metric: latency_p50] | 30.9s | 178.6s | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |
| p95 end-to-end [metric: latency_p95] | 33.1s | 197.5s | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |
| mean model calls per workflow | 5.2 | 9.6 | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |
| forced escalations [metric: forced_escalation_total] | 13 | 17 | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |
| step retries [metric: step_retry_total] | 0 | 10 | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |
| budget violations [metric: budget_violations] | 0 | 0 | [results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00) |

[results: results/archive/phase4-pre-gapfix/baseline_results.json] (commit 9284c80bd6fb, 2026-09-16T14:11:24+00:00); [results: results/archive/phase4-pre-gapfix/verified_results.json] (commit 9284c80bd6fb, 2026-09-16T16:02:12+00:00)

## Ledger reconciliation

**PASS — no exceptions.** Checked against the real ERPNext general ledger after the runs, not against the agent's own record of what it did.

bad postings (duplicate + overpayment): 0 [metric: bad_postings] [results: results/archive/phase4-pre-gapfix/ledger_reconciliation.json] (2026-09-16T16:03:24+00:00)

- duplicate payments: 0
- overpayments: 0
- unbalanced vouchers: 0
- documents sharing an idempotency key: 0

[results: results/archive/phase4-pre-gapfix/ledger_reconciliation.json] (2026-09-16T16:03:24+00:00)

## What these numbers do not cover

See `docs/limitations.md`. In particular the conformal router's calibration status, the executor↔verifier correlation, and the latency target are recorded there with what is and is not measured.
