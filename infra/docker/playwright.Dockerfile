# syntax=docker/dockerfile:1.7
# Playwright UI e2e runner (build context = ./ui, dockerfile = this file). Runs on the compose edge
# network via compose.test.yaml (profile "test") and drives Chromium against the gateway's
# browser-visible origin https://localhost:8443 (D-UI-08-32, A-11).
#
# Pinned: the image tag is the exact @playwright/test version in ui/package.json, so the bundled
# browsers match the library (tests/repo/test_compose_topology.py asserts they agree), and pnpm
# 12.17.0 (= packageManager). The base image ships its own Node, which differs from the ui build's
# 26.13.1: pnpm prints an engines warning for it, which is expected. The install runs under
# ui/pnpm-workspace.yaml's supply-chain policy with a frozen lockfile.
FROM mcr.microsoft.com/playwright:v1.64.1-noble
WORKDIR /app
RUN npm install --global pnpm@12.17.0 \
 && test "$(pnpm --version)" = "12.17.0"
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json playwright.config.ts ./
COPY e2e ./e2e
# The router's generated route tree: e2e/axe-routes.spec.ts derives the route list from it.
COPY src/routeTree.gen.ts ./src/routeTree.gen.ts
