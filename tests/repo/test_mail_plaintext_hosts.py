"""The plaintext SMTP values the repo ships (Mailpit) pass the MailSettings validator.

``DC_SMTP_TLS=none`` is only accepted for a loopback, single-label or cluster ``.svc`` host (quick
261121-0sh, DEC-1). compose.yaml and the Helm ``edge.yaml`` mailpit branch both use the host
``mailpit``; if either ever names an external relay in plaintext the mail service would refuse to
start, so these tests fail first. The same values with an external host must raise, which proves
the test can detect a regression.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from dc_mail.settings import MailSettings
from pydantic import ValidationError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "deploy" / "helm" / "contractum" / "templates" / "edge.yaml"
EXTERNAL_HOST = "smtp.example.com"


def _settings(env: dict[str, str], **override: Any) -> MailSettings:
    """MailSettings from the DC_SMTP_* values of a deployment, with an explicit host override."""
    params: dict[str, Any] = {
        "smtp_host": env["DC_SMTP_HOST"],
        "smtp_port": int(env["DC_SMTP_PORT"]),
        "smtp_tls": env["DC_SMTP_TLS"],
    }
    if "DC_SMTP_USERNAME" in env:
        params["smtp_username"] = env["DC_SMTP_USERNAME"]
    params.update(override)
    return MailSettings(**params)


def _compose_plaintext_envs() -> dict[str, dict[str, str]]:
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    found: dict[str, dict[str, str]] = {}
    for name, service in config["services"].items():
        env = {k: str(v) for k, v in (service.get("environment") or {}).items()}
        if env.get("DC_SMTP_TLS") == "none":
            found[name] = env
    return found


def _helm_mailpit_env() -> dict[str, str]:
    text = EDGE.read_text(encoding="utf-8")
    branch = text.split("{{- if .Values.mailpit.enabled }}", 1)[1].split("{{- else }}", 1)[0]
    env = dict(re.findall(r"name: (DC_SMTP_\w+), value: \"?([^\"}\s]+)\"?", branch))
    assert env, "the mailpit branch of edge.yaml sets no DC_SMTP_* value"
    return env


def test_compose_mailpit_values_pass_the_mail_validator() -> None:
    envs = _compose_plaintext_envs()
    assert "mail" in envs, "compose.yaml no longer has a plaintext SMTP mail service"
    for env in envs.values():
        assert _settings(env).smtp_tls == "none"
        with pytest.raises(ValidationError, match="DC_SMTP_TLS=none is allowed only"):
            _settings(env, smtp_host=EXTERNAL_HOST)


def test_helm_mailpit_branch_passes_the_mail_validator() -> None:
    env = _helm_mailpit_env()
    assert env["DC_SMTP_TLS"] == "none"
    assert "DC_SMTP_USERNAME" not in env
    assert _settings(env).smtp_host == env["DC_SMTP_HOST"]
    with pytest.raises(ValidationError, match="DC_SMTP_TLS=none is allowed only"):
        _settings(env, smtp_host=EXTERNAL_HOST)


@pytest.mark.parametrize("host", ["2606:4700::1111", "fd00::5", "[fd00::5]", "10.0.0.5"])
def test_non_loopback_ip_literals_are_refused_for_plaintext(host: str) -> None:
    with pytest.raises(ValidationError, match="DC_SMTP_TLS=none is allowed only"):
        MailSettings(smtp_host=host, smtp_tls="none")


@pytest.mark.parametrize(
    "host", ["::1", "127.0.0.1", "localhost", "mailpit", "mailpit.contractum.svc.cluster.local"]
)
def test_loopback_and_cluster_hosts_are_accepted_for_plaintext(host: str) -> None:
    assert MailSettings(smtp_host=host, smtp_tls="none").smtp_host == host
