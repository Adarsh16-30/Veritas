# results.md

Every number below is measured. Targets live in `VERITAS_PRD.md` and `docs/METRICS.md` and are never written here (Rule 10).

Corpus: real U.S. federal contract awards from USAspending.gov, seeded into the real ERPNext instance — source, request and checksum in `data/dataset_manifest.json` (Rule 8). Faults are injected as real ERPNext documents and real procurement requests (PRD §6.3); ground truth comes from the injector that created each condition.

**Conformal router: calibrated** (alpha=0.1, qhat=0.6802799276894204, fitted on 384 held-out points from 201 workflows disjoint from this benchmark, Rule 9). [results: results/calibration.json]

## Headline

| Metric | Baseline | Verified | Delta |
|---|---|---|---|
| end-to-end success [metric: e2e_success] | 63.6% ± 7.3 (n=44) | 90.9% ± 4.3 (n=44) | +27.3 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| — on faulted workflows [metric: e2e_success_faulted] | 55.6% ± 8.3 (n=36) | 97.2% ± 2.7 (n=36) | +41.7 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| — on clean controls [metric: e2e_success_clean] | 100.0% ± 0.0 (n=8) | 62.5% ± 17.1 (n=8) | -37.5 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| false-commit rate [metric: false_commit_rate] | 68.6% ± 3.9 (n=140) | 14.3% ± 5.4 (n=42) | -54.3 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| escalation precision [metric: esc_precision] | 100.0% ± 0.0 (n=20) | 92.1% ± 4.4 (n=38) | -7.9 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| escalation recall [metric: esc_recall] | 55.6% ± 8.3 (n=36) | 97.2% ± 2.7 (n=36) | +41.7 pp | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

## Compounding-failure curve

Fraction of workflows still correct at every one of their first k steps.

| k | Baseline | Verified | source |
|---|---|---|---|
| 1 | 63.6% ± 7.3 (n=44) | 84.1% ± 5.5 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| 2 | 63.6% ± 7.3 (n=44) | 84.1% ± 5.5 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| 3 | 63.6% ± 7.3 (n=44) | 84.1% ± 5.5 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| 4 | 63.6% ± 7.3 (n=44) | 81.8% ± 5.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| 5 | 63.6% ± 7.3 (n=44) | 81.8% ± 5.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| 6 | 63.6% ± 7.3 (n=44) | 81.8% ± 5.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

[results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

## Detection by fault class

The honest weakness map (PRD §5.3). `rules` marks the classes the deterministic rule engine can catch on its own — the classes where it cannot are where a verification gate has to earn its place.

| Class | Expected | Rules? | Baseline | Verified | source |
|---|---|---|---|---|---|
| adversarial_duplicate | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| adversarial_injection | escalate | no | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| adversarial_injection_indirect | escalate | no | 0.0% ± 0.0 (n=4) | 75.0% ± 21.7 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| ambiguity | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| boundary_at_tolerance | proceed | no | 100.0% ± 0.0 (n=4) | 75.0% ± 21.7 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| boundary_budget_zero | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| clean | proceed | no | 100.0% ± 0.0 (n=4) | 50.0% ± 25.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| compounding | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| conflicting | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| missing | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)
| temporal | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

[results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

## Latency and stopping

| Metric | Baseline | Verified |
|---|---|---|
| median end-to-end [metric: latency_p50] | 37.0s | 99.1s | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |
| p95 end-to-end [metric: latency_p95] | 47.5s | 573.8s | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |
| mean model calls per workflow | 4.7 | 5.6 | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |
| forced escalations [metric: forced_escalation_total] | 20 | 38 | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |
| step retries [metric: step_retry_total] | 0 | 16 | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |
| budget violations [metric: budget_violations] | 0 | 0 | [results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00) |

[results: results/baseline_results.json] (commit d049ef00f09b, 2026-10-31T07:10:08+00:00); [results: results/verified_results.json] (commit d049ef00f09b, 2026-10-31T12:35:20+00:00)

## Ledger reconciliation

**PASS — no exceptions.** Checked against the real ERPNext general ledger after the runs, not against the agent's own record of what it did.

bad postings (duplicate + overpayment): 0 [metric: bad_postings] [results: results/ledger_reconciliation.json] (2026-10-31T12:35:33+00:00)

- duplicate payments: 0
- overpayments: 0
- unbalanced vouchers: 0
- documents sharing an idempotency key: 0

[results: results/ledger_reconciliation.json] (2026-10-31T12:35:33+00:00)

## What these numbers do not cover

See `docs/limitations.md`. In particular the conformal router's calibration status, the executor↔verifier correlation, and the latency target are recorded there with what is and is not measured.
