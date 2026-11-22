# Service image for the uv workspace (build context = repo root).
#
#   docker build -f infra/docker/service.Dockerfile --build-arg PACKAGE=dc-identity .
#
# PACKAGE is the workspace member's `[project] name` (dc-identity, dc-contracts, dc-validation,
# dc-audit or dc-mail), never the directory name. It has no default on purpose: a build without
# it fails.
#
# Two-step install (research Pattern 11): third-party dependencies first (cached while the lock
# and member manifests are unchanged), then the workspace packages as non-editable wheels.
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
ARG PACKAGE
RUN test -n "$PACKAGE"
COPY pyproject.toml uv.lock ./
COPY shared/dc-core/pyproject.toml    shared/dc-core/pyproject.toml
COPY shared/dc-auth/pyproject.toml    shared/dc-auth/pyproject.toml
COPY shared/dc-odcs/pyproject.toml    shared/dc-odcs/pyproject.toml
COPY shared/dc-rules/pyproject.toml   shared/dc-rules/pyproject.toml
COPY identity/pyproject.toml          identity/pyproject.toml
COPY contracts/pyproject.toml         contracts/pyproject.toml
COPY validation/pyproject.toml        validation/pyproject.toml
COPY audit/pyproject.toml             audit/pyproject.toml
COPY mail/pyproject.toml              mail/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --package "$PACKAGE" --no-editable --no-dev
COPY shared shared
COPY identity identity
COPY contracts contracts
COPY validation validation
COPY audit audit
COPY mail mail
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package "$PACKAGE" --no-editable --no-dev

FROM python:3.13-slim-trixie
# Security (quick 261120-pn7): the base tag lags the Debian security archive, so upgrade the OS
# packages first, and drop pip. The services never run pip, and the urllib3, msgpack and
# setuptools copies vendored inside it carry HIGH advisories (the app venv has its own, locked,
# dependencies). The pip that is removed is the base image's, called by absolute path.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/python -m pip uninstall -y pip \
 && useradd --system --uid 10001 app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
# Build identity served by GET /api/v1/<module>/version (dc_core.app.service_version). Last, so a
# new commit or build time only rebuilds this metadata layer.
ARG GIT_SHA=dev
ARG BUILD_TIME=dev
ENV DC_GIT_SHA=$GIT_SHA DC_BUILD_TIME=$BUILD_TIME
USER app
# The services answer GET /healthz on port 8000. Compose and the chart's probes set their own
# checks; this one makes a plain `docker run` report health too. A container that reuses the image
# for another server points DC_HEALTHCHECK_URL at it; one-shot jobs disable the check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ.get('DC_HEALTHCHECK_URL', 'http://127.0.0.1:8000/healthz'), timeout=4)"]
