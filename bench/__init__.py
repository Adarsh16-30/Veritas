"""Fault-injection benchmark: runner, metrics, plots, ledger reconciliation
(PRD §5, §7 Phase 4). Built in Phase 4.

``run.py`` executes a config (``baseline`` = no verifier, ``verified`` = full
gate), ≥ 30 reps, fixed seeds, mean ± std. It **refuses to emit a
verified−baseline delta** unless a timestamped ``results/baseline_results.json``
exists and is cited (Rule 4). ``reconcile_ledger.py`` asserts zero duplicate /
over-tolerance ``Payment Entry`` after each run. ``report.py`` writes
``docs/results.md`` with a ``[results: <file>]`` citation on every number
(Rule 10). Metric definitions: ``docs/METRICS.md``.
"""
