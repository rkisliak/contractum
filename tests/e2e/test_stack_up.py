"""PLAT-02: one `docker compose up` leaves a healthy stack with only the gateway published."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.e2e

LONG_RUNNING = (
    "postgres",
    "nats",
    "problem-responder",
    "traefik",
    "identity",
    "contracts",
    "validation",
    "audit",
)
ONE_SHOTS = (
    "dev-init",
    "identity-migrate",
    "contracts-migrate",
    "validation-migrate",
    "audit-migrate",
)


@pytest.mark.parametrize("service", LONG_RUNNING)
def test_long_running_services_report_healthy(compose: Any, service: str) -> None:
    assert compose.health(service) == "healthy"


@pytest.mark.parametrize("service", ONE_SHOTS)
def test_one_shot_jobs_exited_with_code_zero(compose: Any, service: str) -> None:
    container = compose.container(service)
    container.reload()
    assert container.attrs["State"]["Status"] == "exited"
    assert container.attrs["State"]["ExitCode"] == 0


def test_traefik_is_the_only_container_with_host_port_bindings(compose: Any) -> None:
    bound: dict[str, list[str]] = {}
    for container in compose.containers(all=False):
        container.reload()
        ports = container.attrs["NetworkSettings"].get("Ports") or {}
        host_ports = [
            f"{key}->{binding['HostPort']}"
            for key, bindings in ports.items()
            for binding in (bindings or [])
        ]
        if host_ports:
            service = container.labels["com.docker.compose.service"]
            bound[service] = host_ports
    assert list(bound) == ["traefik"]
    assert all(
        entry.endswith("->8443") and entry.startswith("443/tcp") for entry in bound["traefik"]
    )
