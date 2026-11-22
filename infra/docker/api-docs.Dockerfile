# syntax=docker/dockerfile:1.7
# The api-docs container (build context = repo root): the merged OpenAPI document of every
# service and a vendored Swagger UI, served by a standard-library server (gateway/api-docs).
# No Python dependency is installed and it runs as an unprivileged user.
#
# Swagger UI (swagger-ui-dist 5.35.0, npm, Apache-2.0) is fetched at build time and verified by
# BuildKit against the pinned sha256 before use. It is unpacked with tar, never `npm install`,
# so no install script (of the package or its dependency) ever runs. Upgrade: bump the version
# in the URL and VERSION line and replace the checksum with the new tarball's sha256.
FROM python:3.13-slim-trixie
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DC_API_DOCS_SWAGGER_DIR=/opt/swagger
# Security (quick 261120-pn7): upgrade the OS packages (the base tag lags the Debian security
# archive) and drop pip, whose vendored urllib3, msgpack and setuptools carry HIGH advisories. The
# server is standard-library only and never runs pip.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/python -m pip uninstall -y pip
ADD --checksum=sha256:1b8e891893404ea4c3558b6a87e2a3e494e7eedba41168c2ece5800a391a6ea6 \
    https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.35.0.tgz /tmp/swagger.tgz
RUN mkdir -p /opt/swagger \
 && tar -xzf /tmp/swagger.tgz -C /opt/swagger --strip-components=1 package/ \
 && rm /tmp/swagger.tgz \
 && echo "swagger-ui-dist 5.35.0" > /opt/swagger/VERSION
COPY gateway/api-docs/ /opt/api-docs/
# Build identity served by GET /api/v1/docs/version (server.py): the platform version (the
# Release workflow passes the released version; "dev" otherwise), the 12-character commit and the
# commit time. Last, so a new version only rebuilds this metadata layer.
ARG VERSION=dev
ARG GIT_SHA=dev
ARG BUILD_TIME=dev
ENV DC_VERSION=$VERSION DC_GIT_SHA=$GIT_SHA DC_BUILD_TIME=$BUILD_TIME
LABEL org.opencontainers.image.title="contractum api-docs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.source="https://github.com/rkisliak/contractum" \
      dc.swagger-ui-dist.version="5.35.0"
USER 65534:65534
EXPOSE 8080
# The same check as compose's, so a plain `docker run` reports health too. Kubernetes ignores it
# and uses the chart's probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
CMD ["python", "/opt/api-docs/server.py"]
