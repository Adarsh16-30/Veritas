"""Rule 1 enforcement — the hand-run buying chain moves the real GL.

Skipped unless a real ERPNext is reachable at ``ERPNEXT_URL``. In CI (no ERP)
this is a skip, not a failure; run it locally after ``docker compose up`` +
``bootstrap_erpnext.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")


def _erpnext_up() -> bool:
    try:
        return requests.get(f"{URL}/api/method/ping", timeout=3).status_code == 200
    except requests.RequestException:
        return False


@pytest.mark.skipif(not _erpnext_up(), reason=f"no running ERPNext at {URL}")
def test_hand_buying_chain_moves_gl_and_balances() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/handrun_buying_chain.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "GL entries moved" in out, out
    assert "double-entry balances: True" in out, out
    assert "S1..S6 OK" in out, out
