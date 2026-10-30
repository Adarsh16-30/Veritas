"""The experimental conditions a benchmark run was recorded under (Rule 4).

A verified-vs-baseline delta means something only if the two runs differ in the
gate and nothing else. Two things can silently change underneath a run that is
being resumed over days, and both have already cost this project a re-run
(docs/ENGINEERING_RULES.md §5.1):

* **What the models see, or what the corpus is.** Any change to the rendered
  step context, to a model prompt, or to how the fault harness builds a case
  changes the evidence. ``EVIDENCE_VERSION`` is bumped by hand with every such
  change, and the changelog below says why. It is a manual convention because
  no file fingerprint can tell a comment edit from a change in evidence.
* **Which ERP identity ran each step** (``erp.scoped.describe``).
* **Which model backend served the models** (``agent.llm.model_backend``): the
  same weights behind Ollama and behind vLLM are not guaranteed to decode
  identically, and a hosted model is not the local one.

``bench.run`` records both in every results file, refuses to ``--resume`` a run
under different conditions, and ``bench.report`` refuses to compute a delta
between runs recorded under different evidence versions.

Changelog
---------
1. Phase 4 after the evidence-coverage fix (docs/limitations.md, Phase 4): runs b4 / v4.
   Results files written before this field existed are version 1.
2. Phase 6: every ERP-sourced string in a step context is quarantined in
   ``«…»`` and both system prompts say what that marking means (injection
   hardening); ``boundary_at_tolerance`` rounds its invoice down so it never
   lands a fraction of a cent over the tolerance it is labelled as meeting.
3. The S1 description fact no longer overclaims. ``item_description_free_of_instructions``
   becomes ``item_description_passes_injection_screen``: a pattern list can show a
   known injection is present, never that text is free of one. The S1 checklist and
   both system prompts now have the models judge the quoted item name themselves.
   Cause: every v5 false commit (12 of 12) came from the two indirect-injection
   workflows that ran to completion under a "free of instructions = True" fact.
   Runs: b6 / c6 / v6. Watch the clean controls: this can cost false escalations.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_VERSION = 3

#: What a results file written before these fields existed was recorded under.
LEGACY = {"evidence_version": 1, "erp_access": "single identity", "model_backend": "ollama"}


def recorded(payload: dict[str, Any]) -> dict[str, Any]:
    """The conditions a results payload was recorded under."""
    return {key: payload.get(key, default) for key, default in LEGACY.items()}


def differences(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Human-readable differences between two condition sets; empty if equal."""
    return [
        f"{key}: {a.get(key)!r} vs {b.get(key)!r}" for key in LEGACY if a.get(key) != b.get(key)
    ]
