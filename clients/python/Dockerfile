# CLI image for CI pipelines (build context = clients/python).
#
#   docker build -t contractum:test clients/python
#   docker run --rm -v "$PWD:/work:ro" contractum:test test data.parquet --contract ORDERS --frozen --offline
#
# The venv is built from the tested uv.lock, so the image carries the dependency versions the SDK
# CI ran. pandas and polars are left out because the CLI reads files itself; dataframe users run
# the library in their own image. No credential is ever baked in: pass CONTRACTUM_URL and
# CONTRACTUM_TOKEN at run time (environment variables or mounts).
FROM python:3.14-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra delta --extra sql

FROM python:3.14-slim-trixie
# Security: the base tag lags the Debian security archive, so upgrade the OS packages first, and
# drop pip (the copies of urllib3, msgpack and setuptools vendored inside it carry advisories).
# The pip that is removed is the base image's, called by absolute path; the app venv has none.
# The OS upgrade must never come from the build cache, or a new Debian/Alpine security fix only
# lands when this file or the base image changes. CI passes a new OS_PACKAGES_REFRESH on every
# run (the run id); local builds leave it empty and may reuse the cached layer.
ARG OS_PACKAGES_REFRESH=
RUN echo "OS packages refresh: ${OS_PACKAGES_REFRESH:-cached}" \
 && apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/python -m pip uninstall -y pip \
 && useradd --system --create-home --uid 10001 contractum
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ARG VERSION=dev
LABEL org.opencontainers.image.title="contractum" \
      org.opencontainers.image.description="Contractum data-engineer CLI: fetch, pin and test data against published data contracts" \
      org.opencontainers.image.source="https://github.com/rkisliak/contractum" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /work
HEALTHCHECK NONE
USER contractum
ENTRYPOINT ["contractum"]
CMD ["--help"]
