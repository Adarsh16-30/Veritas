"""Idempotency key + guarded ERPNext write (Rule 5).

PROVIDED VERBATIM — PRD §9.1. Do not modify, simplify, or stub. A retry, a
duplicate dispatch, or a crash-resume must never post a second document.
"""

import hashlib
import json


def idempotency_key(workflow_id: str, step: str, attempt_input: dict) -> str:
    payload = json.dumps({"w": workflow_id, "s": step, "i": attempt_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def submit_once(erp, doctype: str, doc: dict, key: str, db) -> str:
    # If this key already committed a document, return it — never post twice.
    existing = db.get_committed(key)
    if existing:
        return existing  # crash-safe / retry-safe replay
    name = erp.insert_and_submit(doctype, doc)  # real ERPNext write + submit
    db.record_committed(key, name)  # durable before returning
    return name
