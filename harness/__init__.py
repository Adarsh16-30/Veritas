"""Legitimate-workflow generator + fault injector + ground-truth labels
(PRD §5.1, §6.3). Built in Phase 4.

Posts **real** malformed documents into the **real** ERP across 7 fault classes:
missing data · conflicting data · ambiguity · adversarial (duplicate-with-altered
-ID; instruction text embedded in a remarks/description field) · boundary (amount
exactly at tolerance; budget at zero) · temporal (expired contract; back-dated
invoice, respecting immutable-ledger rules) · compounding (a plausible S2 choice
that only becomes wrong at S5).

Each injected workflow gets a ``labels`` row: ``{fault_class,
expected_terminal_action}``. Faults ERPNext rejects natively are recorded as a
finding; the interesting faults are the ones ERPNext accepts but that are still
wrong.
"""
