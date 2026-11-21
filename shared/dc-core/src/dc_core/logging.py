"""Structured JSON logging with secret scrubbing (T-01-06)."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from asgi_correlation_id import correlation_id

_SECRET_KEY = re.compile(r"(?i)(password|secret|token|authorization|cookie|private_key)")
REDACTED = "[redacted]"
# An opaque personal access token (``dcp_<16 hex>_<secret>``): redacted by value, wherever it
# appears, because a token logged under a harmless key ("note", "event") is still a credential.
DCP_TOKEN_RE = re.compile(r"dcp_[0-9a-f]{16}_[A-Za-z0-9_-]+")
# A webhook signing secret (``whsec_`` + base64 of 32 random bytes, 44 characters): redacted by
# value for the same reason as ``dcp_`` tokens. The 20-character floor keeps the bare prefix and
# short look-alikes in prose from being rewritten.
WHSEC_RE = re.compile(r"whsec_[A-Za-z0-9+/=]{20,}")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return WHSEC_RE.sub("whsec_" + REDACTED, DCP_TOKEN_RE.sub("dcp_" + REDACTED, value))
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def scrub_secrets(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the value of any credential-looking key, and any ``dcp_`` token or ``whsec_`` secret
    in a value."""
    return {k: (REDACTED if _SECRET_KEY.search(k) else _scrub(v)) for k, v in event_dict.items()}


def _add_request_id(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    rid = correlation_id.get()
    if rid and "request_id" not in event_dict:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging(service: str, level: str = "INFO", *, stream: TextIO | None = None) -> None:
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Never show locals: frames hold request scopes with Authorization headers.
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            scrub_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(service=service)
