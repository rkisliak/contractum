"""dev-init: create every development secret and certificate, once, idempotently.

Runs as root inside the identity image (stdlib plus ``cryptography``). Layout:

* ``/opt/infra``  infra templates, read-only bind mount of ``./infra``
* ``/out``        the ``dev-secrets`` named volume
* ``/out-host``   ``./.dev`` on the host (mirrors the engineer needs: CA cert, bootstrap secret)

Canonical values live under ``/out/_canonical`` (root only). Each consumer gets only its own
subset in ``/out/<consumer>/``; compose mounts that subdirectory at ``/run/secrets`` through a
volume subpath, so no container can read another consumer's secrets (T-01-27).

Every file is created only when absent, so a second run changes nothing, with two exceptions for
the local-IdP signing key (quick 261120-s0p): a key smaller than 3072 bits is replaced once by a
3072-bit key and its public key is kept in ``jwt_previous_public_key.pem`` (identity serves both
in the JWKS, so tokens of the old key verify until they expire), and that previous file is reset
to the current public key once it is older than ``PREVIOUS_KEY_OVERLAP_S``. Only file names are
printed, never values.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

OUT = Path(os.environ.get("DEV_INIT_OUT", "/out"))
OUT_HOST = Path(os.environ.get("DEV_INIT_OUT_HOST", "/out-host"))
INFRA = Path(os.environ.get("DEV_INIT_INFRA", "/opt/infra"))

SERVICES = ("identity", "contracts", "validation", "audit")

# Every consumer subpath and its runtime uid:gid (verified with `docker compose exec <svc> id -u`):
#   postgres 999:999        the postgres image's `postgres` user, which runs the init script
#   identity..audit 10001   the service image's `app` user (infra/docker/service.Dockerfile)
#   fake-graph 10001        the identity image's `app` user (it reuses that image)
#   (webhook-sink, mail and mailpit mount no secret and so have no entry)
#   nats, traefik, e2e 0:0  the official nats and traefik images and the e2e image run as root
#   _canonical 0:0          root only
# mock-oidc and problem-responder mount no secret and so have no entry.
OWNERS: dict[str, tuple[int, int]] = {
    "_canonical": (0, 0),
    "postgres": (999, 999),
    "identity": (10001, 10001),
    "contracts": (10001, 10001),
    "validation": (10001, 10001),
    "audit": (10001, 10001),
    "fake-graph": (10001, 10001),
    "nats": (0, 0),
    "traefik": (0, 0),
    "e2e": (0, 0),
}

# Local-IdP signing key: RSA-3072 (NIST SP 800-57 and ASVS 6.2.x guidance for new keys). A smaller
# key is rotated once; its public key stays in the JWKS for the overlap, which is far above the
# 15-minute access-token lifetime (refresh tokens are opaque and unaffected).
JWT_KEY_BITS = 3072
PREVIOUS_KEY_OVERLAP_S = 86_400

CA_VALID_DAYS = 3650
LEAF_VALID_DAYS = 825
LEAF_RENEW_BEFORE_DAYS = 30


def _say(message: str) -> None:
    print(f"dev-init: {message}", flush=True)


def _ensure_dir(consumer: str) -> Path:
    path = OUT / consumer
    uid, gid = OWNERS[consumer]
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    path.chmod(0o500)
    return path


def _write_private(target: Path, data: bytes, *, uid: int, gid: int, mode: int) -> None:
    """Replace ``target`` atomically with ``data``, owned ``uid:gid`` with ``mode`` from the start.

    The bytes go to a new sibling file created with ``O_EXCL | O_NOFOLLOW`` and mode 0600, so the
    content is never readable by anyone else (not even for a moment under a loose umask) and a
    planted symlink is never followed. Owner and final mode are set on the open descriptor, then
    the file is renamed over ``target``.
    """
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.replace(temporary, target)


def _write(consumer: str, name: str, data: bytes, *, overwrite: bool = False) -> bool:
    """Write ``data`` to ``/out/<consumer>/<name>`` (0400) unless it already exists."""
    directory = _ensure_dir(consumer)
    target = directory / name
    if target.exists() and (not overwrite or target.read_bytes() == data):
        return False
    uid, gid = OWNERS[consumer]
    _write_private(target, data, uid=uid, gid=gid, mode=0o400)
    _say(f"wrote {consumer}/{name}")
    return True


def _read(consumer: str, name: str) -> bytes:
    return (OUT / consumer / name).read_bytes()


def _canonical(name: str, factory) -> bytes:  # type: ignore[no-untyped-def]
    """Return the canonical value, creating it first when absent."""
    path = OUT / "_canonical" / name
    if not path.exists():
        _write("_canonical", name, factory())
    return path.read_bytes()


def _token() -> bytes:
    return secrets.token_urlsafe(32).encode("ascii")


def _pem_key(key) -> bytes:  # type: ignore[no-untyped-def]
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _new_ca() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Contractum (dev)"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Contractum dev CA"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return _pem_key(key), cert.public_bytes(serialization.Encoding.PEM)


def _new_leaf(ca_key_pem: bytes, ca_cert_pem: bytes) -> tuple[bytes, bytes]:
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=LEAF_VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("traefik"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # Python 3.13's default TLS context is X509-strict: it needs both key identifiers.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )
    return _pem_key(key), cert.public_bytes(serialization.Encoding.PEM)


def _leaf_needs_renewal(cert_pem: bytes) -> bool:
    cert = x509.load_pem_x509_certificate(cert_pem)
    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(days=LEAF_RENEW_BEFORE_DAYS)
    return cert.not_valid_after_utc < deadline


def _generate_jwt_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=JWT_KEY_BITS)


def _new_jwt_key() -> bytes:
    return _pem_key(_generate_jwt_key())


class JwtKeyPlan(NamedTuple):
    current_pem: bytes
    previous_public_pem: bytes
    rotated: bool


def _public_pem(private_key: rsa.RSAPrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def plan_jwt_keys(
    current_pem: bytes | None,
    previous_public_pem: bytes | None,
    previous_age_s: float,
    *,
    now_overlap_s: float,
) -> JwtKeyPlan:
    """Decide the signing key and the previous public key (pure: no file system access).

    * no current key: a new 3072-bit key, previous = its own public key, not rotated;
    * a current key under 3072 bits: a new 3072-bit key, previous = the old public key, rotated;
    * a 3072-bit key: kept. The previous file is created when absent, kept while younger than the
      overlap and reset to the current public key once it is older (an equal one is untouched).
    """
    if current_pem is None:
        fresh = _generate_jwt_key()
        return JwtKeyPlan(_pem_key(fresh), _public_pem(fresh), False)
    loaded = serialization.load_pem_private_key(current_pem, password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise SystemExit("dev-init: jwt_signing_key.pem is not an RSA private key")
    if loaded.key_size < JWT_KEY_BITS:
        return JwtKeyPlan(_new_jwt_key(), _public_pem(loaded), True)
    own_public = _public_pem(loaded)
    if previous_public_pem is None:
        return JwtKeyPlan(current_pem, own_public, False)
    if previous_public_pem != own_public and previous_age_s > now_overlap_s:
        return JwtKeyPlan(current_pem, own_public, False)
    return JwtKeyPlan(current_pem, previous_public_pem, False)


def _render_nats_conf(nats_passwords: dict[str, str]) -> bytes:
    text = (INFRA / "nats" / "nats.conf.tmpl").read_text(encoding="utf-8")
    for service, password in nats_passwords.items():
        text = text.replace("{{ " + service + "_nats_password }}", password)
    if "{{" in text:
        raise SystemExit("dev-init: unrendered placeholder left in nats.conf.tmpl")
    return text.encode("utf-8")


def _mirror_to_host(name: str, data: bytes) -> None:
    """Refresh a host mirror (0600, owned like the host directory so the engineer can read it)."""
    if not OUT_HOST.is_dir():
        _say(f"host mirror directory {OUT_HOST} missing; skipped {name}")
        return
    target = OUT_HOST / name
    if target.exists() and target.read_bytes() == data:
        return
    host = OUT_HOST.stat()
    _write_private(target, data, uid=host.st_uid, gid=host.st_gid, mode=stat.S_IRUSR | stat.S_IWUSR)
    _say(f"refreshed host mirror {name}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for consumer in OWNERS:
        _ensure_dir(consumer)

    # --- canonical values (created only when absent) -------------------------------------------
    ca_key_path = OUT / "_canonical" / "ca.key"
    ca_crt_path = OUT / "_canonical" / "ca.crt"
    if ca_key_path.exists() != ca_crt_path.exists():
        raise SystemExit(
            "dev-init: CA state inconsistent (one of ca.key, ca.crt missing); wipe the volume"
        )
    if not ca_key_path.exists():
        new_ca_key, new_ca_crt = _new_ca()  # key and certificate must come from the same call
        _write("_canonical", "ca.key", new_ca_key)
        _write("_canonical", "ca.crt", new_ca_crt)
    ca_key = ca_key_path.read_bytes()
    ca_cert = ca_crt_path.read_bytes()
    leaf_key_path = OUT / "_canonical" / "leaf.key"
    leaf_crt_path = OUT / "_canonical" / "leaf.crt"
    renew = leaf_crt_path.exists() and _leaf_needs_renewal(leaf_crt_path.read_bytes())
    if not (leaf_key_path.exists() and leaf_crt_path.exists()) or renew:
        leaf_key, leaf_crt = _new_leaf(ca_key, ca_cert)
        _write("_canonical", "leaf.key", leaf_key, overwrite=True)
        _write("_canonical", "leaf.crt", leaf_crt, overwrite=True)
        if renew:
            _write("traefik", "tls.key", leaf_key, overwrite=True)
            _write("traefik", "tls.crt", leaf_crt, overwrite=True)
    jwt_path = OUT / "_canonical" / "jwt_signing_key.pem"
    jwt_prev_path = OUT / "_canonical" / "jwt_previous_public_key.pem"
    jwt_plan = plan_jwt_keys(
        jwt_path.read_bytes() if jwt_path.exists() else None,
        jwt_prev_path.read_bytes() if jwt_prev_path.exists() else None,
        time.time() - jwt_prev_path.stat().st_mtime if jwt_prev_path.exists() else 0.0,
        now_overlap_s=PREVIOUS_KEY_OVERLAP_S,
    )
    jwt_key = jwt_plan.current_pem
    if jwt_plan.rotated:
        _say(f"rotated jwt_signing_key.pem to {JWT_KEY_BITS} bits")
    _write("_canonical", "jwt_signing_key.pem", jwt_key, overwrite=True)
    _write(
        "_canonical", "jwt_previous_public_key.pem", jwt_plan.previous_public_pem, overwrite=True
    )
    postgres_password = _canonical("postgres_password", _token)
    db_passwords = {s: _canonical(f"{s}_db_password", _token) for s in SERVICES}
    nats_passwords = {s: _canonical(f"{s}_nats_password", _token) for s in SERVICES}
    bootstrap_secret = _canonical("bootstrap_admin_secret", _token)
    # The Microsoft Graph client secret identity presents to the compose-only fake Graph (D-60).
    graph_secret = _canonical("graph_client_secret", _token)
    # The webhook signing-secret encryption key (a Fernet key: urlsafe base64 of 32 random bytes)
    # and the notifier credential pair (Phase 04.1). The notifier's full API key goes only to
    # contracts, which presents it to the mail service; identity gets only the public id and the
    # SHA-256 of the secret, so it can verify the key but never reproduce it (D-105).
    webhook_key = _canonical(
        "webhook_secret_key", lambda: base64.urlsafe_b64encode(secrets.token_bytes(32))
    )
    notifier_public_id = _canonical("notifier_public_id", lambda: secrets.token_hex(8).encode())
    notifier_secret = _canonical("notifier_secret", _token)

    # --- per-consumer copies (created only when absent) ----------------------------------------
    _write("postgres", "postgres_password", postgres_password)
    for service in SERVICES:
        _write("postgres", f"{service}_db_password", db_passwords[service])
        _write(service, "db_password", db_passwords[service])
        _write(service, "nats_password", nats_passwords[service])
    _write("identity", "jwt_signing_key.pem", jwt_key, overwrite=True)
    _write("identity", "jwt_previous_public_key.pem", jwt_plan.previous_public_pem, overwrite=True)
    _write("identity", "bootstrap_admin_secret", bootstrap_secret)
    _write("identity", "graph_client_secret", graph_secret)
    _write("fake-graph", "graph_client_secret", graph_secret)
    _write("contracts", "webhook_secret_key", webhook_key)
    _write("contracts", "mail_api_key", b"dcp_" + notifier_public_id + b"_" + notifier_secret)
    _write(
        "identity",
        "notifier_credential",
        notifier_public_id + b":" + hashlib.sha256(notifier_secret).hexdigest().encode("ascii"),
    )
    # nats.conf is rendered from the tracked template and the persisted passwords, so it is
    # rewritten whenever the template changes; a warm volume never keeps stale permissions.
    _write(
        "nats",
        "nats.conf",
        _render_nats_conf({s: p.decode("ascii") for s, p in nats_passwords.items()}),
        overwrite=True,
    )
    _write("traefik", "tls.crt", _read("_canonical", "leaf.crt"))
    _write("traefik", "tls.key", _read("_canonical", "leaf.key"))
    _write("e2e", "ca.crt", ca_cert)
    _write("e2e", "bootstrap_admin_secret", bootstrap_secret)

    # --- host mirrors so engineers can read them ------------------------------------------------
    _mirror_to_host("ca.crt", ca_cert)
    _mirror_to_host("bootstrap_admin_secret", bootstrap_secret)
    _say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
