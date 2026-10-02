"""Trivial structural smoke test — keeps CI meaningful from Phase 1 (PRD §7)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_layout_present() -> None:
    for rel in [
        "infra/docker-compose/stack.yml",
        "scripts/check_rules.sh",
        "scripts/handrun_buying_chain.py",
        "docs/METRICS.md",
        "docs/results.md",
        "docs/limitations.md",
        "docs/architecture.md",
        "erp/idempotent.py",
        "orchestrator/checkpoint.py",
        # VERITAS_PRD.md and CLAUDE.md are deliberately kept out of the public
        # repo. The PRD §9 blocks they carry are pinned in
        # scripts/provided_code_reference.json instead.
        "scripts/provided_code_reference.json",
    ]:
        assert (ROOT / rel).is_file(), f"missing {rel}"

    for pkg in ["agent", "verify", "orchestrator", "erp", "data", "harness", "trace", "bench"]:
        assert (ROOT / pkg / "__init__.py").is_file(), f"missing package {pkg}"


def test_provided_primitives_import() -> None:
    from erp.idempotent import idempotency_key, submit_once  # noqa: F401
    from orchestrator.checkpoint import advance  # noqa: F401
