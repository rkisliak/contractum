# syntax=docker/dockerfile:1.7
# Playwright UI e2e runner (build context = ./ui, dockerfile = this file). Runs on the compose edge
# network via compose.test.yaml (profile "test") and drives Chromium against the gateway's
# browser-visible origin https://localhost:8443 (D-UI-08-32, A-11).
#
# Pinned: the image tag is the exact @playwright/test version in ui/package.json, so the bundled
# browsers match the library (tests/repo/test_compose_topology.py asserts they agree), and pnpm
# 12.22.0 (= packageManager). The base image ships its own Node, which differs from the ui build's
# 26.13.1: pnpm prints an engines warning for it, which is expected. The install runs under
# ui/pnpm-workspace.yaml's supply-chain policy with a frozen lockfile.
#
# It runs as the base image's unprivileged `pwuser`. scripts/e2e-ui.sh starts it as the invoking
# host user instead, so it can write the bind-mounted ./.dev and ui/test-results on a Linux host,
# with the group 10001 that may read the e2e secrets (scripts/e2e-runner-env.sh). It needs no
# Docker socket and never runs as root.
FROM mcr.microsoft.com/playwright:v1.65.1-noble
WORKDIR /app
RUN npm install --global pnpm@12.22.0 \
 && test "$(pnpm --version)" = "12.22.0"
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json playwright.config.ts ./
COPY e2e ./e2e
# The router's generated route tree: e2e/axe-routes.spec.ts derives the route list from it.
COPY src/routeTree.gen.ts ./src/routeTree.gen.ts
# Playwright clears its outputDir (/app/test-results, a bind mount in compose.test.yaml) by removing
# the directory first. rmdir needs write access to the parent, so a non-root runner without it gets
# EACCES and the run stops; with it the kernel answers EBUSY for the mount point and Playwright
# empties the directory instead. The runner always has the group 10001 (compose.test.yaml
# group_add), whatever host uid it runs as, so /app is writable by that group.
RUN chgrp 10001 /app && chmod 0775 /app
USER pwuser
# The runner is healthy while it can reach the gateway it drives (DC_UI_E2E_SETUP_URL).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["node", "-e", "const u = new URL(process.env.DC_UI_E2E_SETUP_URL || 'https://traefik'); require('net').connect(Number(u.port) || 443, u.hostname).on('connect', () => process.exit(0)).on('error', () => process.exit(1))"]
