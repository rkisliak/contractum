# syntax=docker/dockerfile:1.7
# The ui container (build context = ./ui, dockerfile = this file): the Vite single-page app built
# in a Node stage and served by an unprivileged nginx that runs read-only (D-UI-08-04, D-UI-08-10).
# Compose builds it with `context: ./ui`, `dockerfile: ../infra/docker/ui.Dockerfile`, `target: runtime`;
# `runtime` stays the last stage, so the default target is unchanged.
#
# Pinned: Node 26.13.1 (= ui/.nvmrc and engines.node), pnpm 12.17.0 (= packageManager, installed
# with npm because Node 26 ships no corepack) and nginxinc/nginx-unprivileged 1.31.7-alpine-slim.
# The install runs under ui/pnpm-workspace.yaml's supply-chain policy (14-day release age, no
# exotic subdependencies, strict dependency builds) with a frozen lockfile. Upgrade a pin here,
# in ui/.nvmrc, in package.json and in ui/CLAUDE.md together.
#
# The runtime stage holds no secret and no Node: the entrypoint script writes /tmp/ui/config.json
# and the security header files from DC_UI_* environment variables (public identifiers only) and
# refuses to start on an invalid value.

FROM node:26.13.1-trixie-slim AS deps
WORKDIR /app
RUN npm install --global pnpm@12.17.0 \
 && test "$(pnpm --version)" = "12.17.0"
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .nvmrc ./
RUN pnpm install --frozen-lockfile

FROM deps AS build
COPY . .
# Build identity shown in the UI's "Versions" dialog (vite.config.ts `__DC_BUILD__`): the platform
# version (the Release workflow passes the released version; "dev" otherwise), the 12-character
# commit and the commit time.
ARG VERSION=dev
ARG GIT_SHA=dev
ARG BUILD_TIME=dev
ENV VERSION=$VERSION GIT_SHA=$GIT_SHA BUILD_TIME=$BUILD_TIME
RUN pnpm build

# The static gate (D-UI-08-31): scripts/ci.sh runs `docker build --target ci` when the host's Node
# does not match .nvmrc, so the result never depends on host tooling. `pnpm ci:static` is the one
# script the native path and this stage both run: generated-client drift, format, lint, types,
# unit tests and the build. Nothing from this stage reaches the runtime image.
FROM deps AS ci
COPY . .
RUN pnpm ci:static

# The full gate (CI_FULL=1 scripts/ci.sh): `pnpm ci:full` is ci:static with the unit tests run under
# V8 coverage and the thresholds of vitest.config.ts enforced.
FROM deps AS ci-full
COPY . .
RUN pnpm ci:full

FROM nginxinc/nginx-unprivileged:1.31.7-alpine-slim AS runtime
# Security (quick 261120-pn7): the pinned nginx tag lags the Alpine security archive (musl, zlib,
# openssl), so upgrade the packages as root, then drop to the image's own unprivileged user below.
USER root
RUN apk upgrade --no-cache
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=0755 nginx/40-ui-config.sh /docker-entrypoint.d/40-ui-config.sh
ARG VERSION=dev
ARG GIT_SHA=dev
ARG BUILD_TIME=dev
LABEL org.opencontainers.image.title="contractum ui" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.source="https://github.com/rkisliak/contractum"
EXPOSE 8080
# The same check as compose's (busybox wget in the image), so a plain `docker run` reports health
# too. Kubernetes ignores it and uses the chart's probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["wget", "-qO-", "http://127.0.0.1:8080/healthz.txt"]
# The image's own user (uid 101) runs the entrypoint scripts and nginx.
USER 101
