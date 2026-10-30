"""The VERITAS production service: an HTTP API and a queue worker.

``service.api`` takes procurement requests and human decisions; ``service.worker``
runs the verified workflow for each request. Both build every client from
``orchestrator.settings`` and share the agent Postgres and Redis with the
benchmark tooling, which is unaffected by either.
"""
