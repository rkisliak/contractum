"""Export the platform's merged OpenAPI document for the UI (UI-06, D-UI-08-30).

The single implementation of the in-process service construction and of the snapshot that the
generated TypeScript client is built from. It builds the identity, contracts, validation, audit
and mail apps with never-connected database URLs and a temporary RSA key, merges the four routed
services' documents with ``gateway/api-docs/merge.py`` (loaded by path, because the gateway imports
nothing from the platform) and renders them deterministically (sorted keys, two-space indent, one
trailing newline). No running stack is needed.

    uv run python scripts/export_openapi.py          write ui/openapi/openapi.json
    uv run python scripts/export_openapi.py --check  exit 1 when the committed snapshot is stale

``tests/repo/conftest.py`` loads this module by path and calls ``build_service_apps``, so the repo
has exactly one app construction (the route-policy tests, the merge test and this export agree).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "gateway" / "api-docs" / "merge.py"
SNAPSHOT = ROOT / "ui" / "openapi" / "openapi.json"
ROUTED = ("identity", "contracts", "validation", "audit")
NEVER = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/never"
STALE_HINT = (
    "ui/openapi/openapi.json is stale: run `uv run python scripts/export_openapi.py` "
    "then `pnpm --dir ui gen:api` "
    "then `uv run python scripts/gen_http_reference.py`"
)


def build_service_apps(tmp_dir: Path) -> dict[str, FastAPI]:
    """Every service app, built in-process. Database URLs are never connected: only the route
    tables and documents are read. ``tmp_dir`` receives the throwaway signing key."""
    from dc_audit.main import create_audit_app
    from dc_audit.settings import AuditSettings
    from dc_contracts.main import create_contracts_app
    from dc_contracts.settings import ContractsSettings
    from dc_identity.main import create_identity_app
    from dc_identity.settings import IdentitySettings
    from dc_mail.main import create_mail_app
    from dc_mail.settings import MailSettings
    from dc_validation.main import create_validation_app
    from dc_validation.settings import ValidationSettings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_dir / "jwt_signing_key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {
        "identity": create_identity_app(
            IdentitySettings(
                database_url=NEVER,
                jwt_signing_key_file=key_file,
                bootstrap_secret_file=tmp_dir / "absent",
            )
        ),
        "contracts": create_contracts_app(ContractsSettings(database_url=NEVER)),
        "validation": create_validation_app(ValidationSettings(database_url=NEVER)),
        "audit": create_audit_app(AuditSettings(database_url=NEVER)),
        "mail": create_mail_app(MailSettings()),
    }


def _load_merge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gateway_api_docs_merge", MERGE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MERGE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def merged_document(apps: dict[str, FastAPI] | None = None) -> dict[str, Any]:
    """The four routed services' documents merged exactly as the gateway serves them."""
    if apps is None:
        with tempfile.TemporaryDirectory(prefix="dc-openapi-") as tmp:
            return merged_document(build_service_apps(Path(tmp)))
    documents = {name: apps[name].openapi() for name in ROUTED}
    merged: dict[str, Any] = _load_merge().merge_specs(documents)
    return merged


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the committed snapshot differs"
    )
    args = parser.parse_args(argv)

    rendered = render(merged_document())
    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else None
        if current != rendered:
            print(STALE_HINT, file=sys.stderr)
            return 1
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(rendered, encoding="utf-8")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
