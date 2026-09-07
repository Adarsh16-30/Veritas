"""Layer 4 — verification gate (PRD §3.1, §5). Built in Phase 3. Do NOT add any
of this during Phase 2.

Three gates, in order:

1. ``rules.py`` — deterministic accounting invariants (tolerance, duplicate-
   payment guard against the real ledger, budget, vendor/contract/GL validity).
   A violation is terminal → escalate.
2. ``verifier.py`` — an independent verifier: a different model or a distinctly
   different adversarial framing, with the executor's rationale **withheld**
   (Rule 3). Fixed-checklist output ``{verdict, violated_expectations,
   confidence}``. The rationale-exclusion guard is ``build_verifier_payload``
   (PRD §9.4).
3. ``conformal.py`` — conformal calibrator: maps signals to a prediction region
   at a target coverage, then ``route()`` → commit / retry / escalate (PRD §9.3).
   Calibration set is disjoint from the benchmark set (Rule 9).
"""
