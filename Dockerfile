# syntax=docker/dockerfile:1.7
#
# VERITAS service image: the API, the worker and the migrator, one image.
#
#   docker build -t veritas:dev .
#   docker compose -f infra/docker-compose/stack.yml -f infra/docker-compose/service.yml up -d
#
# Two stages: dependencies are resolved from uv.lock (--frozen: the lock is the
# only source) in a build stage, and the runtime stage gets just the virtualenv
# and the packages the service imports. It runs as an unprivileged user, with no
# compilers and no package manager, and works with a read-only root filesystem.
# The base images are pinned by tag; CI pins and scans them by digest
# (.github/workflows/release.yml).

ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm
ARG UV_VERSION=0.11.16

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION
# uv, pinned, from PyPI: the same version CI uses, and one registry fewer.
# Behind a TLS-inspecting proxy, pass its CA as a build secret; it is mounted for
# these two steps only and never lands in a layer:
#   docker build --secret id=ca_bundle,src=/path/to/ca.pem .
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir --disable-pip-version-check "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="veritas" \
      org.opencontainers.image.description="Verification-gated procure-to-pay agent on ERPNext" \
      org.opencontainers.image.source="https://github.com/Adarsh16-30/Veritas"
RUN groupadd --system --gid 10001 veritas \
 && useradd --system --uid 10001 --gid veritas --no-create-home --shell /usr/sbin/nologin veritas
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
# Only what the service imports (agent, erp, orchestrator, service, trace,
# verify), its operator scripts, the ledger reconciliation, and the fitted
# conformal calibration the verified gate loads (results/calibration.json).
COPY --chown=root:root agent ./agent
COPY --chown=root:root erp ./erp
COPY --chown=root:root orchestrator ./orchestrator
COPY --chown=root:root service ./service
COPY --chown=root:root trace ./trace
COPY --chown=root:root verify ./verify
COPY --chown=root:root scripts/migrate.py scripts/create_api_client.py ./scripts/
# The nightly ledger reconciliation (Helm CronJob) asks ERPNext directly.
COPY --chown=root:root bench/__init__.py bench/reconcile_ledger.py ./bench/
COPY --chown=root:root results/calibration.json ./results/calibration.json
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_HOST=0.0.0.0 \
    WORKER_HEALTH_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8090 9109
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"]
CMD ["uvicorn", "--factory", "service.api:create_app_from_settings", \
     "--host", "0.0.0.0", "--port", "8090", "--workers", "1", \
     "--limit-max-requests", "10000", "--no-access-log", "--proxy-headers"]
