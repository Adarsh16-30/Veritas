"""The VERITAS HTTP API (production hardening, B6).

    uv run uvicorn --factory service.api:create_app_from_settings \\
        --host 127.0.0.1 --port 8090 --workers 1 --limit-max-requests 10000

Endpoints (every one but health and metrics needs ``Authorization: Bearer vk....``):

* ``POST /v1/workflows``: submit a procurement request (role ``submitter``).
  ``Idempotency-Key`` is required. A replay of the same key and body returns
  the original workflow (200); the same key with a different body is refused
  (409). Never a second workflow.
* ``GET /v1/workflows/{id}``: status, current step, the ERPNext documents
  posted (with links), and every human decision. A submitter sees only their
  own requests.
* ``GET /v1/escalations``: the human queue, each with whether it can be
  approved and why (roles ``approver``/``viewer``).
* ``POST /v1/escalations/{id}/decisions``: ``reject`` or
  ``approve_and_resume`` (role ``approver``).
  - Separation of duties: the submitter cannot decide their own request.
  - A hard accounting invariant is never approvable.
  - Decisions are append-only.
* ``GET /healthz`` (liveness), ``GET /readyz`` (Postgres and Redis),
  ``GET /metrics`` (request counts and latency only, no business data).

Small by design for the laptop: one worker process, a four-connection pool, no
in-memory caches. The process recycles itself with ``--limit-max-requests``.
"""

# No `from __future__ import annotations`: FastAPI must evaluate the dependency
# annotations below, which refer to names local to create_app().

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from orchestrator.queue import WorkQueue
from orchestrator.settings import Settings, get_settings
from service.auth import Principal, parse_token, verify
from service.schemas import Decision, WorkflowRequest
from service.store import Conflict, Forbidden, NotFound, ServiceStore
from trace.logging import log_context, setup_logging
from trace.provenance import doc_url

_log = logging.getLogger("veritas.api")

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RATE_PREFIX = "veritas:ratelimit:"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Refuse request bodies over ``max_bytes`` -- by header, and while streaming,
    so a chunked upload with no Content-Length cannot exhaust memory either."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _too_large(scope, receive, send)
            return
        seen = 0

        async def limited() -> Any:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await _too_large(scope, receive, send)


async def _too_large(scope: Any, receive: Any, send: Any) -> None:
    await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)


def create_app(store: ServiceStore, queue: WorkQueue, settings: Settings) -> FastAPI:
    production = settings.veritas_env == "production"
    erp_base = (settings.erpnext_public_url or settings.erpnext_url).rstrip("/")
    registry = CollectorRegistry()
    requests_total = Counter(
        "veritas_api_requests",
        "API requests by route and status.",
        ["method", "route", "status"],
        registry=registry,
    )
    latency = Histogram(
        "veritas_api_request_seconds",
        "API request latency by route.",
        ["route"],
        registry=registry,
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.open()
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="VERITAS",
        version="1",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )

    @app.middleware("http")
    async def envelope(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if REQUEST_ID.match(incoming) else uuid.uuid4().hex
        started = time.perf_counter()
        with log_context(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                _log.exception("unhandled error", extra={"path": request.url.path})
                response = JSONResponse({"detail": "internal error"}, status_code=500)
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = time.perf_counter() - started
            requests_total.labels(request.method, route, str(response.status_code)).inc()
            latency.labels(route).observe(elapsed)
            if route not in ("/healthz", "/readyz", "/metrics"):
                _log.info(
                    "%s %s %s",
                    request.method,
                    route,
                    response.status_code,
                    extra={"status": response.status_code, "ms": round(elapsed * 1000, 1)},
                )
        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # --- auth ----------------------------------------------------------------------
    def principal(authorization: Annotated[str, Header()] = "") -> Principal:
        scheme, _, token = authorization.partition(" ")
        parsed = parse_token(token.strip()) if scheme.lower() == "bearer" else None
        row = store.client(parsed[0]) if parsed else None
        if parsed is None or row is None or row["disabled"]:
            raise HTTPException(401, "invalid or missing API key", {"WWW-Authenticate": "Bearer"})
        if not verify(parsed[1], row["key_salt"], row["key_hash"]):
            raise HTTPException(401, "invalid or missing API key", {"WWW-Authenticate": "Bearer"})
        who = Principal(client_id=row["client_id"], roles=frozenset(row["roles"]))
        _rate_limit(who)
        return who

    def _rate_limit(who: Principal) -> None:
        window = int(time.time() // 60)
        key = f"{RATE_PREFIX}{who.client_id}:{window}"
        try:
            count = int(queue.r.incr(key))
            if count == 1:
                queue.r.expire(key, 120)
        except Exception:  # noqa: BLE001 - abuse protection must not take the API down
            _log.warning("rate limiter unavailable; allowing request")
            return
        if count > settings.api_rate_limit_per_min:
            retry = str(60 - int(time.time()) % 60)
            raise HTTPException(429, "rate limit exceeded", {"Retry-After": retry})

    def require(*roles: str) -> Callable[..., Principal]:
        def check(who: Annotated[Principal, Depends(principal)]) -> Principal:
            if not who.has(*roles):
                raise HTTPException(403, f"requires role: {' or '.join(roles)}")
            return who

        return check

    # --- workflows --------------------------------------------------------------------
    @app.post("/v1/workflows", status_code=202)
    def submit(
        body: WorkflowRequest,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        who: Annotated[Principal, Depends(require("submitter"))],
    ) -> dict[str, Any]:
        if not IDEMPOTENCY_KEY.match(idempotency_key):
            raise HTTPException(422, "Idempotency-Key must be 8-128 chars of [A-Za-z0-9._:-]")
        try:
            workflow_id, created = store.submit(
                who.client_id, idempotency_key, body.model_dump(mode="json")
            )
        except Conflict as e:
            raise HTTPException(409, str(e)) from e
        if created:
            queue.enqueue(workflow_id)
            _log.info("submitted", extra={"workflow_id": workflow_id})
        else:
            response.status_code = 200
        return {
            "workflow_id": workflow_id,
            "created": created,
            "status_url": f"/v1/workflows/{workflow_id}",
        }

    @app.get("/v1/workflows/{workflow_id}")
    def get_workflow(
        workflow_id: str,
        who: Annotated[Principal, Depends(require("submitter", "approver", "viewer"))],
    ) -> dict[str, Any]:
        view = store.view(workflow_id)
        # A submitter sees only their own requests; anything else is "not found",
        # so the API does not confirm that someone else's workflow id exists.
        if view is None or (
            not who.has("approver", "viewer") and view["submitted_by"] != who.client_id
        ):
            raise HTTPException(404, "no such workflow")
        for doc in view["documents"]:
            doc["url"] = doc_url(erp_base, doc["doctype"], doc["doc_name"])
        return view

    # --- the human queue ----------------------------------------------------------------
    @app.get("/v1/escalations")
    def list_escalations(
        who: Annotated[Principal, Depends(require("approver", "viewer"))],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return {"escalations": store.escalations(limit=limit, offset=offset)}

    @app.post("/v1/escalations/{workflow_id}/decisions", status_code=201)
    def decide(
        workflow_id: str,
        body: Decision,
        who: Annotated[Principal, Depends(require("approver"))],
    ) -> dict[str, Any]:
        try:
            result = store.decide(workflow_id, who.client_id, body.decision, body.reason)
        except NotFound as e:
            raise HTTPException(404, "no such workflow") from e
        except Forbidden as e:
            raise HTTPException(403, str(e)) from e
        except Conflict as e:
            raise HTTPException(409, str(e)) from e
        if body.decision == "approve_and_resume":
            queue.enqueue(workflow_id)
        _log.info(
            "decision recorded",
            extra={"workflow_id": workflow_id, "decision": body.decision, "by": who.client_id},
        )
        return result

    # --- operations -----------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks = {"postgres": store.ping(), "redis": queue.ping()}
        ok = all(checks.values())
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(BodyLimit, max_bytes=settings.api_max_body_bytes)
    return app


def create_app_from_settings() -> FastAPI:
    """The uvicorn factory: everything from validated settings, nothing hard-coded."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    store = ServiceStore(settings.postgres_dsn(), max_size=settings.postgres_pool_max)
    queue = WorkQueue(settings.redis_url.get_secret_value())
    return create_app(store, queue, settings)
