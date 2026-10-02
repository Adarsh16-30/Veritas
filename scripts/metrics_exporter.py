#!/usr/bin/env python
"""Prometheus exporter for VERITAS (PRD Phase 6) — live state + recorded benchmark.

    uv run python scripts/metrics_exporter.py               # :9108/metrics
    uv run python scripts/metrics_exporter.py --port 9200 --results-dir results

Prometheus (infra/docker-compose/observability.yml) scrapes it from the host.
Reads the agent Postgres configured in .env through a READ ONLY session, and
``results/*.json``. It writes nothing.

Binds to 0.0.0.0 by default so the Prometheus container can reach it through
``host.docker.internal``; it serves aggregate counts only, never prompts or
document content. Use ``--host 127.0.0.1`` if Prometheus runs on the host.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()

    load_dotenv()
    from prometheus_client import CollectorRegistry, start_http_server

    from trace.api import read_only_store
    from trace.metrics import MetricsSource, VeritasCollector

    @contextmanager
    def source() -> Iterator[MetricsSource]:
        with read_only_store() as store:
            yield store  # type: ignore[misc]  # Store satisfies both protocols

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = Path(__file__).resolve().parent.parent / results_dir

    registry = CollectorRegistry()
    registry.register(VeritasCollector(source, results_dir))
    start_http_server(args.port, addr=args.host, registry=registry)
    print(f"metrics on http://{args.host}:{args.port}/metrics  (read-only)")
    try:
        while True:  # serve until interrupted; the HTTP server runs on its own thread
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
