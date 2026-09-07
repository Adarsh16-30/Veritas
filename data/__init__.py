"""Real procurement dataset loaders (PRD §2.6, §6.2). Real data only (Rule 8).

Deterministic loaders pull real suppliers, items, price/contract terms, and
historical POs/invoices from cited public procurement open datasets into ERPNext
masters. ``dataset_manifest.json`` (written here) records source URL + checksum
for every seeded corpus. No placeholder vendor names, amounts, or line items in
any evaluation path — synthetic data is confined to ``tests/*_synthetic_test.py``.

Built in Phase 4 (seeding for the benchmark).
"""
