# In-network end-to-end test runner (build context = repo root). Runs on the compose networks via
# compose.test.yaml (profile "test"); it carries only the `e2e` dependency group, no service code.
#
# It runs as the unprivileged user 10001. The runner scripts (scripts/e2e.sh, run.sh, run.ps1)
# start it as the invoking host user instead, so it can write the bind-mounted ./.dev on a Linux
# host, and add the group that owns the Docker socket (the outage tests) and the group 10001 that
# may read the e2e secrets (scripts/e2e-runner-env.sh). It never runs as root.
FROM python:3.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 PYTHONUNBUFFERED=1
WORKDIR /app
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
    uv sync --locked --no-install-workspace --only-group e2e
COPY tests/e2e tests/e2e
RUN useradd --system --uid 10001 --user-group --no-create-home e2e
# /app stays read-only for the runner: the pytest and Hypothesis caches go to /tmp.
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache" HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
USER 10001:10001
# The runner is healthy while it can reach the gateway it tests (DC_E2E_GATEWAY_URL).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import os, socket, urllib.parse; u = urllib.parse.urlsplit(os.environ.get('DC_E2E_GATEWAY_URL', 'https://traefik')); socket.create_connection((u.hostname, u.port or 443), timeout=4).close()"]
CMD ["pytest", "-m", "e2e", "-q", "-o", "asyncio_default_test_loop_scope=session", "tests/e2e"]
