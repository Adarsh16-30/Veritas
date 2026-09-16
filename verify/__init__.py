"""Layer 4 — the verification gate (PRD §3.1, §4.1, §5). Built in Phase 3.

Three gates, run in PRD §4.3 order by :mod:`verify.gate`:

1. :mod:`verify.rules` — deterministic accounting invariants, declared in
   ``invariants.yaml`` (PRD §2.4) and evaluated over the ``DELTA`` facts, plus a
   **fresh read of the real ledger** at gate time. A violation is terminal:
   escalate, never retry.
2. :mod:`verify.verifier` — an independent verifier. A different model *family*
   and a distinctly different adversarial framing, with the executor's rationale
   withheld (Rule 3). Answers a fixed per-step checklist, grounding each item in
   the fact it used. ``build_verifier_payload`` is PRD §9.4, verbatim.
3. :mod:`verify.conformal` — split conformal prediction: signals to a
   ``p(commit)``, nonconformity scored on a held-out partition, prediction region
   at the target coverage, then ``route()`` (PRD §9.3, verbatim). The calibration
   set is disjoint from the benchmark set, enforced in code (Rule 9).

Nothing here may leak into ``agent/``. ``Pipeline(gate=None)`` is the Phase 2
baseline and must stay runnable for as long as Rule 4 needs a denominator.

Until a labelled split exists there is no fitted calibration model, and the gate
says so rather than substituting a hand-picked threshold — see
:func:`verify.conformal.unanimous_region`.
"""
