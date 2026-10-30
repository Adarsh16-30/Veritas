# results.md

Every number below is measured. Targets live in `VERITAS_PRD.md` and `docs/METRICS.md` and are never written here (Rule 10).

Corpus: real U.S. federal contract awards from USAspending.gov, seeded into the real ERPNext instance — source, request and checksum in `data/dataset_manifest.json` (Rule 8). Faults are injected as real ERPNext documents and real procurement requests (PRD §6.3); ground truth comes from the injector that created each condition.

**Conformal router: calibrated** (alpha=0.1, qhat=0.5941721043057285, fitted on 173 held-out points from 90 workflows disjoint from this benchmark, Rule 9). [results: results/calibration.json]

## Headline

| Metric | Baseline | Verified | Delta |
|---|---|---|---|
| end-to-end success [metric: e2e_success] | 54.5% ± 7.5 (n=44) | 95.5% ± 3.1 (n=44) | +40.9 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| — on faulted workflows [metric: e2e_success_faulted] | 44.4% ± 8.3 (n=36) | 94.4% ± 3.8 (n=36) | +50.0 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| — on clean controls [metric: e2e_success_clean] | 100.0% ± 0.0 (n=8) | 100.0% ± 0.0 (n=8) | +0.0 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| false-commit rate [metric: false_commit_rate] | 74.1% ± 3.4 (n=162) | 26.1% ± 6.5 (n=46) | -48.0 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| escalation precision [metric: esc_precision] | 100.0% ± 0.0 (n=16) | 100.0% ± 0.0 (n=34) | +0.0 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| escalation recall [metric: esc_recall] | 44.4% ± 8.3 (n=36) | 94.4% ± 3.8 (n=36) | +50.0 pp | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

## Compounding-failure curve

Fraction of workflows still correct at every one of their first k steps.

| k | Baseline | Verified | source |
|---|---|---|---|
| 1 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| 2 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| 3 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| 4 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| 5 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| 6 | 54.5% ± 7.5 (n=44) | 90.9% ± 4.3 (n=44) | [metric: survive] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

[results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

## Detection by fault class

The honest weakness map (PRD §5.3). `rules` marks the classes the deterministic rule engine can catch on its own — the classes where it cannot are where a verification gate has to earn its place.

| Class | Expected | Rules? | Baseline | Verified | source |
|---|---|---|---|---|---|
| adversarial_duplicate | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| adversarial_injection | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| adversarial_injection_indirect | escalate | no | 0.0% ± 0.0 (n=4) | 50.0% ± 25.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| ambiguity | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| boundary_at_tolerance | proceed | no | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| boundary_budget_zero | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| clean | proceed | no | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| compounding | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| conflicting | escalate | yes | 100.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| missing | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)
| temporal | escalate | no | 0.0% ± 0.0 (n=4) | 100.0% ± 0.0 (n=4) | [metric: detection] [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

[results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

## Latency and stopping

| Metric | Baseline | Verified |
|---|---|---|
| median end-to-end [metric: latency_p50] | 35.2s | 100.3s | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |
| p95 end-to-end [metric: latency_p95] | 41.7s | 509.9s | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |
| mean model calls per workflow | 5.2 | 7.7 | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |
| forced escalations [metric: forced_escalation_total] | 16 | 34 | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |
| step retries [metric: step_retry_total] | 0 | 32 | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |
| budget violations [metric: budget_violations] | 0 | 0 | [results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00) |

[results: results/baseline_results.json] (commit ba2f99f681cf, 2026-10-30T08:22:44+00:00); [results: results/verified_results.json] (commit 684a2e279c6a, 2026-10-30T11:58:45+00:00)

## Ledger reconciliation

**PASS — no exceptions.** Checked against the real ERPNext general ledger after the runs, not against the agent's own record of what it did.

bad postings (duplicate + overpayment): 0 [metric: bad_postings] [results: results/ledger_reconciliation.json] (2026-10-30T11:58:57+00:00)

- duplicate payments: 0
- overpayments: 0
- unbalanced vouchers: 0
- documents sharing an idempotency key: 0

[results: results/ledger_reconciliation.json] (2026-10-30T11:58:57+00:00)

## What these numbers do not cover

See `docs/limitations.md`. In particular the conformal router's calibration status, the executor↔verifier correlation, and the latency target are recorded there with what is and is not measured.
