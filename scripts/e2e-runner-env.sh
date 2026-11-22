#!/usr/bin/env bash
# Sourced by scripts/e2e.sh, scripts/e2e-ui.sh and run.sh before they start a test runner of
# compose.test.yaml (the `e2e` and `ui-e2e` services). It exports:
#
#   DC_E2E_UID, DC_E2E_GID  the invoking host user, so the non-root runners can write the
#                           bind-mounted ./.dev and ui/test-results on a Linux host. A root caller
#                           still gets the images' unprivileged user 10001: a runner never runs as
#                           root.
#   DC_DOCKER_GID           the group that owns the Docker socket as containers see it (root on
#                           Docker Desktop, `docker` on a Linux host), added to the `e2e` runner
#                           only: its outage tests stop and start containers through the socket.
#
# Each value can be set by the caller instead. The identity image, built by the `docker compose up`
# before this, reads the socket's group; it runs without a network and is removed at once.
uid="$(id -u)"
gid="$(id -g)"
if [ "$uid" = "0" ]; then
  uid=10001
  gid=10001
fi
export DC_E2E_UID="${DC_E2E_UID:-$uid}"
export DC_E2E_GID="${DC_E2E_GID:-$gid}"
if [ "$(id -u)" != "0" ] && [ ! -w .dev ]; then
  echo "e2e-runner-env: .dev is not writable for $(id -un); the runner writes it as you." >&2
  echo "e2e-runner-env: give it back to yourself, for example: sudo chown -R \"\$(id -u):\$(id -g)\" .dev" >&2
  return 1 2>/dev/null || exit 1
fi
if [ -z "${DC_DOCKER_GID:-}" ]; then
  DC_DOCKER_GID="$(docker run --rm --pull never --network none --entrypoint stat \
    -v /var/run/docker.sock:/var/run/docker.sock:ro contractum/identity:dev -c %g /var/run/docker.sock)"
fi
export DC_DOCKER_GID
unset uid gid
