"""Layer 1 — system of record (PRD §3.1).

Typed ERPNext REST client wrapping the buying chain: Material Request → Purchase
Order → Purchase Receipt → Purchase Invoice → Payment Entry, with submit/cancel
workflow operations. Every write submits a DocType and moves the real GL — no
mock, ever (Rule 1). Every write goes through the idempotency guard in
``erp.idempotent`` (Rule 5).

Built in Phase 2. Until then the Phase 1 hand-run scripts under ``scripts/`` use a
thin throwaway helper; do not build on that.
"""
