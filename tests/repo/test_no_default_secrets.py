"""T-01-60: no tracked file carries a default password, private key or bootstrap secret.

Every secret is generated at first start by the ``dev-init`` one-shot into a volume (D-04); the
repository must never hold one. The scan covers ``git ls-files`` only, so ignored runtime output
(``.dev/``) is out of scope by construction.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Built from pieces so this file does not match its own scan.
_PEM_PRIVATE_KEY = re.compile("-----BEGIN (?:[A-Z0-9]+ )?PRIVATE" + " KEY-----")
_BARE_POSTGRES_PASSWORD = re.compile(r"POSTGRES_PASSWORD(?!_FILE)")
FORBIDDEN_FILE_NAMES = ("bootstrap_admin_secret", "tls.key")
FORBIDDEN_SUFFIXES = (".pem",)
# Files that legitimately name these patterns: this test itself (patterns) and the common
# password deny-list (the data file is a list of passwords to reject).
SCAN_EXEMPT = frozenset(
    {
        "tests/repo/test_no_default_secrets.py",
        "identity/src/dc_identity/data/common_passwords.txt",
    }
)
CODE_SUFFIXES = (".py", ".yml", ".yaml", ".toml", ".json", ".sh", ".ini", ".cfg", ".env", ".tmpl")

# `password = "x"`, `"client_secret": "x"`, `secret: 'x'`: a key whose name ends in password or
# secret, assigned a non-empty literal. `_file`, `_hash`, `_url` and similar suffixes do not match,
# and neither does SQL such as `LOGIN PASSWORD :'pw'` (a psql variable, not an assignment).
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>[\w.-]*(?:password|secret))
    (?:["']?:\s*|["']?\s*=\s*)
    (?P<value>"[^"\s][^"]*"|'[^'\s][^']*')
    """
)
# A `{{ name }}` value is a template placeholder that dev-init renders at first start.
_TEMPLATE_PLACEHOLDER = re.compile(r"""^["']\{\{[^}]+\}\}["']$""")
# A value that is only a shell variable reference (`"$SMTP_PASSWORD"`, `"${SMTP_PASSWORD}"`) reads
# the secret from the environment at run time; it is not a literal.
_SHELL_VARIABLE = re.compile(r"""^["']\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\})["']$""")
# Reviewed exceptions, keyed by file: (key, reason). Anything else fails the scan.
ALLOWED_LITERALS = {
    "identity/src/dc_identity/passwords.py": (
        "_DUMMY_PASSWORD",
        "hashed to equalise login timing for unknown users; not a credential for any account",
    ),
}


def literal_secret_assignments(text: str) -> list[str]:
    """Lines assigning a non-empty string literal to a password or secret key."""
    hits: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.search(line)
        value = match.group("value") if match else ""
        if match and not (_TEMPLATE_PLACEHOLDER.match(value) or _SHELL_VARIABLE.match(value)):
            hits.append(line.strip())
    return hits


def _tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return sorted(name for name in result.stdout.decode().split("\0") if name)


def _read_text(relative: str) -> str | None:
    path = ROOT / relative
    if not path.is_file():  # tracked but deleted in the working tree, or a symlink target
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return None  # binary


def test_no_tracked_file_is_named_like_a_secret() -> None:
    offenders = [
        name
        for name in _tracked_files()
        if Path(name).name in FORBIDDEN_FILE_NAMES or name.endswith(FORBIDDEN_SUFFIXES)
    ]
    assert not offenders, offenders


def test_no_tracked_file_contains_a_pem_private_key() -> None:
    offenders = []
    for name in _tracked_files():
        if name in SCAN_EXEMPT:
            continue
        text = _read_text(name)
        if text is not None and _PEM_PRIVATE_KEY.search(text):
            offenders.append(name)
    assert not offenders, offenders


def test_postgres_password_is_only_ever_passed_as_a_file() -> None:
    offenders = []
    for name in _tracked_files():
        # Tests talk about the variable; they never set it.
        if name in SCAN_EXEMPT or "tests" in Path(name).parts:
            continue
        text = _read_text(name)
        if text is not None and _BARE_POSTGRES_PASSWORD.search(text):
            offenders.append(name)
    assert not offenders, offenders


def test_no_password_or_secret_key_is_assigned_a_literal_outside_tests() -> None:
    offenders: dict[str, list[str]] = {}
    for name in _tracked_files():
        parts = Path(name).parts
        if name in SCAN_EXEMPT or "tests" in parts or not name.endswith(CODE_SUFFIXES):
            continue
        text = _read_text(name)
        if text is None:
            continue
        allowed = ALLOWED_LITERALS.get(name)
        hits = [
            hit
            for hit in literal_secret_assignments(text)
            if allowed is None or allowed[0] not in hit
        ]
        if hits:
            offenders[name] = hits
    assert not offenders, offenders


def _environment(service: dict[str, Any]) -> dict[str, Any]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        pairs = [str(item).partition("=") for item in environment]
        return {key: value for key, _, value in pairs}
    return dict(environment)


@pytest.mark.parametrize("compose_file", ["compose.yaml", "compose.test.yaml"])
def test_compose_sets_no_environment_value_for_a_password_or_secret_key(compose_file: str) -> None:
    config = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    offenders = [
        f"{service_name}.{key}"
        for service_name, service in config["services"].items()
        for key, value in _environment(service).items()
        if re.search(r"(?i)(password|secret)$", key) and str(value or "") != ""
    ]
    assert not offenders, offenders


def test_the_scanner_flags_literals_and_spares_file_references() -> None:
    """The assignment check can fail, and does not trip on references to secret files."""
    assert literal_secret_assignments('password = "hunter2"')
    assert literal_secret_assignments("  client_secret: 'abc123'")
    assert literal_secret_assignments('{"admin_password": "x"}')
    assert not literal_secret_assignments('password = ""')
    assert not literal_secret_assignments("bootstrap_secret_file: /run/secrets/bootstrap")
    assert not literal_secret_assignments('password_file = "/run/secrets/db"')
    assert not literal_secret_assignments('# password = "commented"')
    assert not literal_secret_assignments("new_password: str")
    assert not literal_secret_assignments('password: "{{ audit_nats_password }}"')
    assert not literal_secret_assignments("CREATE ROLE :\"svc\" LOGIN PASSWORD :'pw';")
    assert not literal_secret_assignments('--from-literal=smtp_password="$SMTP_PASSWORD"')
    assert not literal_secret_assignments('smtp_password="${SMTP_PASSWORD}"')
    assert literal_secret_assignments('smtp_password="$SMTP_PASSWORD-fallback"')
    assert literal_secret_assignments('smtp_password="prefix$SMTP_PASSWORD"')


# --- by-value scan of docs and skills (04.1 security note T-04.1-51) -------------------------
# The literal scan above only reads code suffixes, so a documentation page or a skill could carry
# a real token value unnoticed. These patterns match the value shapes themselves: a `dcp_` PAT is
# `dcp_` + 16 hex public id + `_` + secret; a webhook secret is `whsec_` + base64 of 32 bytes (44
# characters). Placeholders such as `whsec_<secret>` or `dcp_<public_id>_<secret>` do not match.
_DCP_TOKEN_VALUE = re.compile(r"dcp_[0-9a-f]{16}_[A-Za-z0-9_-]+")
_WHSEC_VALUE = re.compile(r"whsec_[A-Za-z0-9+/=]{20,}")
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")


def _is_doc_or_skill(name: str) -> bool:
    parts = Path(name).parts
    return name.endswith(DOC_SUFFIXES) or ".claude" in parts or ".agents" in parts


def token_value_hits(text: str) -> list[str]:
    return [
        m.group(0) for pattern in (_DCP_TOKEN_VALUE, _WHSEC_VALUE) for m in pattern.finditer(text)
    ]


def test_the_by_value_token_scan_flags_real_shapes_and_spares_placeholders() -> None:
    """The scan can fail: planted values of both shapes are caught, placeholders are not."""
    assert token_value_hits("key dcp_0123456789abcdef_" + "Q" * 22)
    assert token_value_hits("secret whsec_" + "A" * 43 + " shown once")
    assert not token_value_hits("use dcp_<public_id>_<secret> in the header")
    assert not token_value_hits("send whsec_<secret> to the consumer")
    assert not token_value_hits("prefix whsec_ alone in prose")


def test_no_tracked_doc_or_skill_carries_a_webhook_secret_or_pat_value() -> None:
    offenders: dict[str, list[str]] = {}
    for name in _tracked_files():
        if name in SCAN_EXEMPT or not _is_doc_or_skill(name):
            continue
        text = _read_text(name)
        if text is None:
            continue
        hits = token_value_hits(text)
        if hits:
            offenders[name] = hits
    assert not offenders, offenders
