"""A10 spike: TLS is verified against the hostname while the socket connects to a pinned IP.

Pinning closes the DNS-rebinding window between the send-time validation and the connect
(T-04.1-29): the worker connects to ``https://<validated ip>:<port>`` with the ``Host`` header and
the TLS SNI and certificate check both set to the original hostname (httpx request extension
``sni_hostname``, read by httpcore as the TLS ``server_hostname``).

A real HTTPS server (stdlib ``http.server`` + ``ssl``) on 127.0.0.1 serves a leaf certificate for
``hooks.example.test`` signed by a throwaway CA; the client trusts only that CA.
"""

from __future__ import annotations

import datetime as dt
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from dc_contracts import webhooks

pytestmark = pytest.mark.unit

HOSTNAME = "hooks.example.test"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_pki(directory: Path) -> tuple[Path, Path, Path]:
    """A test CA and a leaf for ``HOSTNAME``; returns (ca cert, leaf cert, leaf key) PEM paths."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("webhook test CA"))
        .issuer_name(_name("webhook test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(HOSTNAME))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "leaf.pem"
    key_path = directory / "leaf.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


class TlsServer:
    def __init__(self, cert: Path, key: Path) -> None:
        seen_hosts: list[str] = []
        self.seen_hosts = seen_hosts

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
                seen_hosts.append(self.headers.get("Host", ""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def pki(tmp_path: Path) -> tuple[Path, Path, Path]:
    return _write_pki(tmp_path)


@pytest.fixture
def server(pki: tuple[Path, Path, Path]) -> Iterator[TlsServer]:
    _, cert, key = pki
    tls = TlsServer(cert, key)
    try:
        yield tls
    finally:
        tls.stop()


def _client(ca: Path) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=str(ca))
    return httpx.AsyncClient(verify=context, timeout=5.0)


async def test_connecting_to_the_pinned_ip_verifies_tls_against_the_hostname(
    pki: tuple[Path, Path, Path], server: TlsServer
) -> None:
    async with _client(pki[0]) as client:
        response = await client.get(
            f"https://127.0.0.1:{server.port}/h",
            headers={"Host": HOSTNAME},
            extensions={"sni_hostname": HOSTNAME},
        )
    assert response.status_code == 200
    assert server.seen_hosts == [HOSTNAME]


async def test_a_certificate_for_another_hostname_is_refused(
    pki: tuple[Path, Path, Path], server: TlsServer
) -> None:
    async with _client(pki[0]) as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.get(
                f"https://127.0.0.1:{server.port}/h",
                headers={"Host": "evil.example.test"},
                extensions={"sni_hostname": "evil.example.test"},
            )
    assert webhooks._is_tls_failure(caught.value)
    assert server.seen_hosts == []


async def test_without_sni_hostname_an_ip_connection_fails_verification(
    pki: tuple[Path, Path, Path], server: TlsServer
) -> None:
    async with _client(pki[0]) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://127.0.0.1:{server.port}/h", headers={"Host": HOSTNAME})
    assert server.seen_hosts == []


# --- the pure helpers the worker pins with ----------------------------------------------------


def _target(
    scheme: str, host: str, port: int, path_qs: str = "/h?x=1"
) -> webhooks.ParsedWebhookUrl:
    return webhooks.ParsedWebhookUrl(scheme=scheme, host=host, port=port, path_qs=path_qs)


def test_pinned_parts_for_https_connect_to_the_ip_and_set_host_and_sni() -> None:
    url, headers, extensions = webhooks.pinned_request_parts(
        _target("https", HOSTNAME, 443), "93.184.216.34"
    )
    assert url == "https://93.184.216.34:443/h?x=1"
    assert headers == {"Host": HOSTNAME}
    assert extensions == {"sni_hostname": HOSTNAME}


def test_pinned_parts_name_a_non_default_port_in_the_host_header() -> None:
    url, headers, _ = webhooks.pinned_request_parts(
        _target("https", HOSTNAME, 8443), "93.184.216.34"
    )
    assert url == "https://93.184.216.34:8443/h?x=1"
    assert headers == {"Host": f"{HOSTNAME}:8443"}


def test_pinned_parts_bracket_an_ipv6_address_and_skip_sni_for_plain_http() -> None:
    url, headers, extensions = webhooks.pinned_request_parts(
        _target("http", HOSTNAME, 80, ""), "2606:2800:220:1::1"
    )
    assert url == "http://[2606:2800:220:1::1]:80/"
    assert headers == {"Host": HOSTNAME}
    assert extensions == {}
