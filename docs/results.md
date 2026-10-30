# results.md

Every number below is measured. Targets live in `VERITAS_PRD.md` and `docs/METRICS.md` and are never written here (Rule 10).

Corpus: real U.S. federal contract awards from USAspending.gov, seeded into the real ERPNext instance — source, request and checksum in `data/dataset_manifest.json` (Rule 8). Faults are injected as real ERPNext documents and real procurement requests (PRD §6.3); ground truth comes from the injector that created each condition.

**Conformal router: UNCALIBRATED.** No `results/calibration.json` exists, so the gate routed through `verify.conformal.unanimous_region` — all three gates must agree to commit — rather than through a fitted prediction region. The verified numbers below are for that configuration. No coverage or ECE figure is reported, because an uncalibrated region makes no such claim and `verify.conformal.coverage()` refuses to score one.

## Headline

| Metric | Baseline | Verified | Delta |
|---|---|---|---|
| end-to-end success [metric: e2e_success] | 72.7% ± 6.7 (n=44) | 86.4% ± 5.2 (n=44) | +13.6 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| — on faulted workflows [metric: e2e_success_faulted] | 69.4% ± 7.7 (n=36) | 88.9% ± 5.2 (n=36) | +19.4 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| — on clean controls [metric: e2e_success_clean] | 87.5% ± 11.7 (n=8) | 75.0% ± 15.3 (n=8) | -12.5 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| false-commit rate [metric: false_commit_rate] | 59.3% ± 4.7 (n=108) | 38.1% ± 6.1 (n=63) | -21.2 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| escalation precision [metric: esc_precision] | 96.2% ± 3.8 (n=26) | 94.1% ± 4.0 (n=34) | -2.0 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| escalation recall [metric: esc_recall] | 69.4% ± 7.7 (n=36) | 88.9% ± 5.2 (n=36) | +19.4 pp | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

## Compounding-failure curve

Fraction of workflows still correct at every one of their first k steps.

| k | Baseline | Verified | source |
|---|---|---|---|
| 1 | 75.0% ± 6.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| 2 | 75.0% ± 6.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| 3 | 75.0% ± 6.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| 4 | 72.7% ± 6.7 (n=44) | 88.6% ± 4.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| 5 | 72.7% ± 6.7 (n=44) | 88.6% ± 4.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| 6 | 72.7% ± 6.7 (n=44) | 88.6% ± 4.8 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

[results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

## Detection by fault class

The honest weakness map (PRD §5.3). `rules` marks the classes the deterministic rule engine can catch on its own — the classes where it cannot are where a verification gate has to earn its place.

| Class | Expected | Rules? | Baseline | Verified | source |
|---|---|---|---|---|---|
| adversarial_duplicate | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| adversarial_injection | escalate | no | 50.0% ± 25.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| adversarial_injection_indirect | escalate | no | 0.0% ± 0.0 (n=4) | 0.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| ambiguity | escalate | no | 75.0% ± 21.7 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| boundary_at_tolerance | proceed | no | 75.0% ± 21.7 (n=4) | 75.0% ± 21.7 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| boundary_budget_zero | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| clean | proceed | no | 100.0% ± 0.0 (n=4) | 75.0% ± 21.7 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| compounding | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| conflicting | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| missing | escalate | no | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)
| temporal | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

[results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

## Latency and stopping

| Metric | Baseline | Verified |
|---|---|---|
| median end-to-end [metric: latency_p50] | 30.4s | 108.5s | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |
| p95 end-to-end [metric: latency_p95] | 39.2s | 221.6s | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |
| mean model calls per workflow | 4.1 | 8.0 | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |
| forced escalations [metric: forced_escalation_total] | 26 | 34 | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |
| step retries [metric: step_retry_total] | 0 | 32 | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |
| budget violations [metric: budget_violations] | 0 | 0 | [results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00) |

[results: results/baseline_results.json] (commit ee2f4aaffe44, 2026-09-24T16:59:48+00:00); [results: results/verified_results.json] (commit a3a3e8309a76, 2026-10-30T07:55:09+00:00)

## Ledger reconciliation

**PASS — no exceptions.** Checked against the real ERPNext general ledger after the runs, not against the agent's own record of what it did.

bad postings (duplicate + overpayment): 0 [metric: bad_postings] [results: results/ledger_reconciliation.json] (2026-10-30T07:55:21+00:00)

- duplicate payments: 0
- overpayments: 0
- unbalanced vouchers: 0
- documents sharing an idempotency key: 0

[results: results/ledger_reconciliation.json] (2026-10-30T07:55:21+00:00)

## What these numbers do not cover

See `docs/limitations.md`. In particular the conformal router's calibration status, the executor↔verifier correlation, and the latency target are recorded there with what is and is not measured.
