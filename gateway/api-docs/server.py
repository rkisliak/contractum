"""api-docs: the one merged OpenAPI document behind the gateway (PLAT-06, D-176).

A standard-library ``ThreadingHTTPServer`` on port 8080, with no Python dependency and no import
of the platform's packages. At start a background thread fetches each configured service's own
document from ``http://<svc>:8000/api/v1/<svc>/openapi.json`` on the compose network, retrying
with capped backoff until every one answers 200, then merges them with ``merge.merge_specs``.

A partial document is never served: until a merge succeeds, and for good after a ``MergeError``
(logged, naming both owners), ``/healthz`` answers 503 and ``/api/v1/openapi.json`` answers 503
problem+json. The merge runs once per start: Compose restarts this container (``depends_on``
``restart: true``) only when Compose itself restarts or re-creates a service it depends on, so a
service restarted by its own restart policy or updated outside Compose leaves the old document
served; ``docker compose restart api-docs`` re-runs the merge.

Swagger UI (D-178): ``/api/v1/docs`` redirects (308) to ``/api/v1/docs/``, whose relative asset
links need the slash. Under ``/api/v1/docs/`` only an explicit allow-list of the vendored
``swagger-ui-dist`` files is served, read into memory at start, plus this directory's own
``swagger-initializer.js`` in place of the tarball's, and ``/api/v1/docs/version``, the build
identity (``{service, version, commit, built_at}``, the shape of the services' ``/version``);
anything else is 404, so there is no path traversal. Every response carries
``X-Content-Type-Options: nosniff`` and a same-origin Content-Security-Policy that also forbids
framing (``frame-ancestors 'none'``). The query string and request headers are never reflected.
Each connection's socket times out after 30 s.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))  # merge.py sits next to this file

from merge import MergeError, merge_specs  # noqa: E402

PORT = 8080
FETCH_TIMEOUT_S = 5.0
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 10.0
DEFAULT_SERVICES = "identity,contracts,validation,audit"

MERGED_PATH = "/api/v1/openapi.json"
DOCS_PATH = "/api/v1/docs"
DOCS_PREFIX = DOCS_PATH + "/"
VERSION_PATH = DOCS_PREFIX + "version"
# DC_GIT_SHA and DC_BUILD_TIME are baked into the image (infra/docker/api-docs.Dockerfile);
# DC_VERSION is supplied when the container starts. "dev" for any that is unset. The same
# variables and answer shape as the services' GET /api/v1/<module>/version (dc_core.app).
BUILD_ENV = (("version", "DC_VERSION"), ("commit", "DC_GIT_SHA"), ("built_at", "DC_BUILD_TIME"))
HERE = Path(__file__).resolve().parent
DEFAULT_SWAGGER_DIR = "/opt/swagger"

_HTML = "text/html; charset=utf-8"
_CSS = "text/css; charset=utf-8"
_JS = "text/javascript; charset=utf-8"
_PNG = "image/png"
# Name under /api/v1/docs/ -> (file in the vendored Swagger directory, content type). The empty
# name is the directory itself. swagger-initializer.js comes from HERE, never from the tarball.
SWAGGER_FILES: dict[str, tuple[str, str]] = {
    "": ("index.html", _HTML),
    "index.html": ("index.html", _HTML),
    "swagger-ui.css": ("swagger-ui.css", _CSS),
    "index.css": ("index.css", _CSS),
    "swagger-ui-bundle.js": ("swagger-ui-bundle.js", _JS),
    "swagger-ui-standalone-preset.js": ("swagger-ui-standalone-preset.js", _JS),
    "favicon-16x16.png": ("favicon-16x16.png", _PNG),
    "favicon-32x32.png": ("favicon-32x32.png", _PNG),
}
INITIALIZER = "swagger-initializer.js"

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        # frame-ancestors is not covered by default-src: no site may frame Swagger UI and
        # clickjack "Authorize" or "Execute" once a bearer token is pasted (IN-01).
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    ),
)

_PROBLEMS: dict[int, tuple[str, str, str]] = {
    404: ("not-found", "Not found", "The requested resource does not exist."),
    405: (
        "method-not-allowed",
        "Method not allowed",
        "The API documentation is read-only.",
    ),
    503: (
        "service-unavailable",
        "Service unavailable",
        "The merged API document is not available yet.",
    ),
}

log = logging.getLogger("api-docs")


class MergedDocument:
    """The merged JSON bytes, set once after a successful merge; ``None`` until then."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body: bytes | None = None

    def set(self, body: bytes) -> None:
        with self._lock:
            self._body = body

    def get(self) -> bytes | None:
        with self._lock:
            return self._body


STATE = MergedDocument()


def load_docs_assets(swagger_dir: Path, here: Path = HERE) -> dict[str, tuple[bytes, str]]:
    """The allow-listed files, read once; a file missing from the image is simply not served."""
    assets: dict[str, tuple[bytes, str]] = {}
    for name, (filename, content_type) in SWAGGER_FILES.items():
        path = swagger_dir / filename
        if path.is_file():
            assets[name] = (path.read_bytes(), content_type)
        else:
            log.warning("vendored Swagger UI file %s is missing", filename)
    initializer = here / INITIALIZER
    if initializer.is_file():
        assets[INITIALIZER] = (initializer.read_bytes(), _JS)
    return assets


ASSETS: dict[str, tuple[bytes, str]] = {}


def configured_services() -> list[str]:
    raw = os.environ.get("DC_API_DOCS_SERVICES", DEFAULT_SERVICES)
    return [name.strip() for name in raw.split(",") if name.strip()]


def _fetch(service: str) -> dict[str, Any]:
    url = f"http://{service}:8000/api/v1/{service}/openapi.json"
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310 - fixed http URL
        if response.status != 200:
            raise OSError(f"{url} answered {response.status}")
        loaded: dict[str, Any] = json.loads(response.read())
        return loaded


def fetch_all(services: list[str]) -> dict[str, dict[str, Any]]:
    """Every service's document, retrying each with capped backoff until it answers 200."""
    specs: dict[str, dict[str, Any]] = {}
    for service in services:
        delay = BACKOFF_START_S
        while True:
            try:
                specs[service] = _fetch(service)
                break
            except (OSError, ValueError) as exc:
                log.info("waiting for %s: %s (retry in %.0fs)", service, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
    return specs


def build_merged_document(services: list[str]) -> None:
    """Fetch and merge once; on ``MergeError`` log it and leave the container unhealthy."""
    specs = fetch_all(services)
    try:
        merged = merge_specs(specs)
    except MergeError as exc:
        log.error("merge failed, serving no document: %s", exc)
        return
    STATE.set(json.dumps(merged, separators=(",", ":")).encode("utf-8"))
    log.info("merged %d paths from %s", len(merged["paths"]), ", ".join(services))


def build_info() -> bytes:
    """``{service, version, commit, built_at}`` of this build, "dev" where nothing was baked in."""
    info = {"service": "api-docs"}
    info.update({key: os.environ.get(name) or "dev" for key, name in BUILD_ENV})
    return json.dumps(info).encode("utf-8")


def _problem(status: int) -> bytes:
    slug, title, detail = _PROBLEMS[status]
    body = {"type": f"urn:dc:problem:{slug}", "title": title, "status": status, "detail": detail}
    return json.dumps(body).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """GET and HEAD only; every response carries the security headers."""

    server_version = "api-docs"
    sys_version = ""
    # IN-03: a client that connects and sends nothing (or stalls mid-request) releases its
    # ThreadingHTTPServer thread after this many seconds instead of holding it forever.
    timeout = 30

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # Traefik keeps the access log; never echo request lines here.

    def _respond(
        self,
        status: int,
        content_type: str,
        body: bytes,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (*SECURITY_HEADERS, *extra):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond_problem(self, status: int, extra: tuple[tuple[str, str], ...] = ()) -> None:
        no_store = (("Cache-Control", "no-store"),)
        self._respond(status, "application/problem+json", _problem(status), no_store + extra)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        merged = STATE.get()
        if path == "/healthz":
            if merged is None:
                self._respond(503, "application/json", b'{"status":"unavailable"}')
            else:
                self._respond(200, "application/json", b'{"status":"ok"}')
            return
        if path == MERGED_PATH:
            if merged is None:
                self._respond_problem(503)
            else:
                cache = (("Cache-Control", "no-cache"),)
                self._respond(200, "application/json", merged, cache)
            return
        if path == VERSION_PATH:
            # Public build identity for the UI's "Versions" dialog, like the services' /version.
            cache = (("Cache-Control", "public, max-age=60"),)
            self._respond(200, "application/json", build_info(), cache)
            return
        if path == DOCS_PATH:
            self._respond(308, "text/plain; charset=utf-8", b"", (("Location", DOCS_PREFIX),))
            return
        if path.startswith(DOCS_PREFIX):
            asset = ASSETS.get(path[len(DOCS_PREFIX) :])
            if asset is not None:
                body, content_type = asset
                self._respond(200, content_type, body, (("Cache-Control", "no-cache"),))
                return
        self._respond_problem(404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def _refuse(self) -> None:
        self._respond_problem(405, (("Allow", "GET, HEAD"),))

    do_POST = _refuse
    do_PUT = _refuse
    do_PATCH = _refuse
    do_DELETE = _refuse
    do_OPTIONS = _refuse


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )
    services = configured_services()
    swagger_dir = Path(os.environ.get("DC_API_DOCS_SWAGGER_DIR", DEFAULT_SWAGGER_DIR))
    ASSETS.update(load_docs_assets(swagger_dir))
    threading.Thread(target=build_merged_document, args=(services,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104 - container port
    log.info("listening on %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
