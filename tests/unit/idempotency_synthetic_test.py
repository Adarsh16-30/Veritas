"""Rule 5 — the provided idempotency primitive (PRD §9.1).

Synthetic doubles are permitted here: filename ends ``_synthetic_test.py``.
"""

from __future__ import annotations

from erp.idempotent import idempotency_key, submit_once


def test_key_is_deterministic_and_order_insensitive() -> None:
    a = idempotency_key("wf1", "S4", {"qty": 10, "rate": 25})
    b = idempotency_key("wf1", "S4", {"rate": 25, "qty": 10})
    assert a == b
    assert a != idempotency_key("wf1", "S4", {"qty": 11, "rate": 25})
    assert a != idempotency_key("wf1", "S5", {"qty": 10, "rate": 25})
    assert a != idempotency_key("wf2", "S4", {"qty": 10, "rate": 25})


def test_submit_once_commits_exactly_one_document(commit_db, counting_erp) -> None:
    key = idempotency_key("wf1", "S6", {"invoice": "PI-0001"})

    first = submit_once(counting_erp, "Payment Entry", {"x": 1}, key, commit_db)
    second = submit_once(counting_erp, "Payment Entry", {"x": 1}, key, commit_db)

    assert first == second
    assert counting_erp.calls == 1
    assert commit_db.get_committed(key) == first


def test_distinct_keys_each_commit(commit_db, counting_erp) -> None:
    k1 = idempotency_key("wf1", "S3", {"po": 1})
    k2 = idempotency_key("wf1", "S3", {"po": 2})
    submit_once(counting_erp, "Purchase Order", {}, k1, commit_db)
    submit_once(counting_erp, "Purchase Order", {}, k2, commit_db)
    assert counting_erp.calls == 2
