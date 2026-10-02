#!/usr/bin/env python
"""Trace explorer (PRD Phase 5) — inspect any recorded workflow end to end.

    uv run python scripts/trace_explorer.py            # http://127.0.0.1:8765
    uv run python scripts/trace_explorer.py --port 9000

Reads the agent Postgres configured in .env (POSTGRES_*), read-only. Links to
ERPNext documents use ERPNEXT_PUBLIC_URL if set, else ERPNEXT_URL.

Binds to 127.0.0.1 by default. The trace store holds full prompts and real
ledger data, so exposing it beyond the local machine is an explicit choice
(``--host 0.0.0.0``), not a default.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    load_dotenv()
    import uvicorn

    from trace.api import create_app

    print(f"trace explorer on http://{args.host}:{args.port}  (read-only)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
