"""Layer 3 — executor (PRD §3.1, §4).

``context.py`` assembles the step-scoped ERPNext context — only the fields the
step needs — and precomputes deterministic ``DELTA:`` facts (never ask the LLM to
do arithmetic). It is also the prompt-injection boundary: a step never sees
fields it does not need. ``executor.py`` makes one real LLM call per step,
returning ``{action, args, rationale}`` — no hardcoded action map (Rule 2).
``state.py`` holds ``StepContext`` / ``Step`` / ``Action`` types. ``pipeline.py``
wires the staged pipeline (PRD §4.3).

Built in Phase 2 (naive single reasoning pass). No verification logic here — that
is Phase 3, in ``verify/``.
"""
