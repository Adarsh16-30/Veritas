"""Layer 5 — append-only trace store + query API (PRD §3.1, §3.3, §7 Phase 5).

Every step appends: step context sent to the executor, prompt hash, response
hash, verifier verdict + violated expectations, calibrated confidence/region,
commit/escalate outcome, and provenance links back to the real ERPNext source
documents. Append-only — nothing is mutated after write.

The FastAPI query API over this store, and full-workflow reconstruction, are
built in Phase 5; ``ui/`` is its front end.
"""
