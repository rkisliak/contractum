"""The closed set of Contractum API operations the SDK can call.

This is the only module that imports ``contractum._generated.api`` (enforced by a test that walks
every import in the package). It exposes the typed consumer operations, all of them GET, and
``raise_for_problem``. Nothing here signs in, creates, changes or deletes anything: no contracts,
folders, grants, rule sets, templates, subscriptions or credentials.

Every function takes the configured ``httpx.Client`` (or a generated client wrapping one) that the
transport layer supplies, so base URL, TLS, timeouts, authentication and retries live in one place.
Requests are built with the generated ``_get_kwargs`` and answers are decoded with the generated
``_parse_response``; the status is checked first, so an error body of any shape is mapped to a
``ContractumError`` built only from the problem's status, type, title and detail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, NoReturn, Protocol, TypeVar
from uuid import UUID

import httpx

from contractum._generated.api.contracts import (
    get_contract_api_v1_contracts_contracts_contract_id_get as _get_contract,
)
from contractum._generated.api.contracts import (
    get_contract_diff_api_v1_contracts_contracts_contract_id_diff_get as _get_diff,
)
from contractum._generated.api.contracts import (
    get_contract_version_api_v1_contracts_contracts_contract_id_versions_ref_get as _get_version,
)
from contractum._generated.api.contracts import (
    get_contract_version_metadata_api_v1_contracts_contracts_contract_id_versions_ref_metadata_get as _get_metadata,  # noqa: E501
)
from contractum._generated.api.contracts import (
    get_contract_version_validation_report_api_v1_contracts_contracts_contract_id_versions_ref_validation_report_get as _get_report,  # noqa: E501
)
from contractum._generated.api.contracts import (
    list_contract_versions_api_v1_contracts_contracts_contract_id_versions_get as _list_versions,
)
from contractum._generated.api.contracts import (
    list_contracts_api_v1_contracts_contracts_get as _list_contracts,
)
from contractum._generated.api.identity import (
    auth_policy_api_v1_identity_auth_policy_get as _auth_policy,
)
from contractum._generated.api.identity import (
    get_me_api_v1_identity_me_get as _get_me,
)
from contractum._generated.api.identity import (
    list_folders_api_v1_identity_folders_get as _list_folders,
)
from contractum._generated.client import AuthenticatedClient, Client
from contractum._generated.models.contract_diff_out import ContractDiffOut
from contractum._generated.models.contract_list_out import ContractListOut
from contractum._generated.models.contract_metadata_out import ContractMetadataOut
from contractum._generated.models.contract_validation_report_out import (
    ContractValidationReportOut,
)
from contractum._generated.models.contract_version_list_out import ContractVersionListOut
from contractum._generated.models.contract_version_metadata_out import ContractVersionMetadataOut
from contractum._generated.models.identity_auth_policy_out import IdentityAuthPolicyOut
from contractum._generated.models.identity_folder_list_out import IdentityFolderListOut
from contractum._generated.models.identity_me_out import IdentityMeOut
from contractum.errors import (
    AccessDeniedError,
    AuthError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_PROBLEM_BYTES = 64 * 1024
MAX_FIELD_CHARS = 300

ALLOWED_OPERATION_MODULES: tuple[str, ...] = tuple(
    sorted(
        module.__name__
        for module in (
            _get_contract,
            _get_diff,
            _get_version,
            _get_metadata,
            _get_report,
            _list_versions,
            _list_contracts,
            _auth_policy,
            _get_me,
            _list_folders,
        )
    )
)
"""Dotted names of the generated operation modules this layer may call."""

_PROBLEM_TYPE = re.compile(r"urn:dc:problem:([a-z0-9][a-z0-9-]{0,62})")
_PROBLEM_MEDIA = "application/problem+json"
_LOGIN_HINT = "run `contractum login --token -` or set CONTRACTUM_TOKEN"

T = TypeVar("T")
Transport = httpx.Client | AuthenticatedClient | Client
"""An ``httpx.Client`` or a generated client that wraps one."""

# Only ``raise_on_unexpected_status`` of this object is read by the generated decoders.
_DECODER_CLIENT = Client(base_url="")


class ResponseLike(Protocol):
    """What ``raise_for_problem`` reads: the status, the body bytes and the headers."""

    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class FetchedVersion:
    """The raw answer of the byte-faithful version fetch."""

    status: int
    content: bytes | None
    etag: str | None
    media_type: str | None
    deprecation: str | None
    sunset: str | None


@dataclass(frozen=True)
class _Buffered:
    status_code: int
    content: bytes
    headers: Mapping[str, str]


def _http(transport: Transport) -> httpx.Client:
    if isinstance(transport, httpx.Client):
        return transport
    return transport.get_httpx_client()


def _clean(value: object) -> str | None:
    """Return a printable, length-capped copy of a problem field, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(text.split())[:MAX_FIELD_CHARS]


def _media_type(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("content-type")
    if raw is None:
        return None
    return raw.split(";", 1)[0].strip().lower() or None


def _problem_fields(response: ResponseLike) -> tuple[str | None, str | None, str | None]:
    """Return ``(problem type, title, detail)`` of a problem+json body, else all None."""
    if _media_type(response.headers) != _PROBLEM_MEDIA:
        return None, None, None
    try:
        body = json.loads(response.content)
    except ValueError:
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None
    raw_type = body.get("type")
    matched = _PROBLEM_TYPE.fullmatch(raw_type) if isinstance(raw_type, str) else None
    return (
        matched.group(0) if matched else None,
        _clean(body.get("title")),
        _clean(body.get("detail")),
    )


def _message(status: int, problem_type: str | None, title: str | None, detail: str | None) -> str:
    text = f"{title or 'request failed'} (HTTP {status}"
    text += f", {problem_type})" if problem_type else ")"
    return f"{text}: {detail}" if detail else text


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if 0 <= seconds < 86_400 else None


def raise_for_problem(response: ResponseLike) -> NoReturn:
    """Raise the ``ContractumError`` that matches an error response; never returns.

    The message uses only the status and the problem's type, title and detail. A body that is not
    ``application/problem+json`` is never echoed.
    """
    status = int(response.status_code)
    problem_type, title, detail = _problem_fields(response)
    slug = problem_type.rsplit(":", 1)[-1] if problem_type else None
    message = _message(status, problem_type, title, detail)
    if status == 401:
        raise AuthError(message, hint=_LOGIN_HINT)
    if status == 403:
        if slug == "password-rotation-required":
            raise AuthError(message, hint="rotate the password in the web interface, then log in")
        if slug == "principal-not-registered":
            raise AuthError(message, hint="ask a platform administrator to register this account")
        raise AccessDeniedError(message)
    if status == 404:
        raise NotFoundError(message)
    if status == 429:
        raise RateLimitedError(message, retry_after=_retry_after(response.headers))
    raise ServerError(status, problem_type, title, detail)


def _decode(operation: ModuleType, response: httpx.Response) -> Any:
    """Check the status, then decode a successful body with the generated parser."""
    if response.status_code >= 400:
        raise_for_problem(response)
    try:
        return operation._parse_response(client=_DECODER_CLIENT, response=response)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ServerError(
            response.status_code, title="the server sent a response that could not be read"
        ) from None


def _invoke(operation: ModuleType, transport: Transport, **params: Any) -> Any:
    kwargs = operation._get_kwargs(**params)
    return _decode(operation, _http(transport).request(**kwargs))


def _expect(kind: type[T], value: object) -> T:
    if isinstance(value, kind):
        return value
    raise ServerError(200, title="the server sent an unexpected response")


def list_contracts(
    transport: Transport,
    *,
    folder_id: UUID | None = None,
    include_subtree: bool = False,
    odcs_id: str | None = None,
    name: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    sort: Literal["id", "name"] = "id",
) -> ContractListOut:
    """GET one page of the contracts the caller can read."""
    return _expect(
        ContractListOut,
        _invoke(
            _list_contracts,
            transport,
            folder_id=folder_id,
            include_subtree=include_subtree,
            odcs_id=odcs_id,
            name=name,
            domain=domain,
            status=status,
            tags=tags,
            q=q,
            limit=limit,
            cursor=cursor,
            sort=sort,
        ),
    )


def get_contract(transport: Transport, contract_id: UUID) -> ContractMetadataOut:
    """GET one contract's metadata."""
    return _expect(ContractMetadataOut, _invoke(_get_contract, transport, contract_id=contract_id))


def list_versions(transport: Transport, contract_id: UUID) -> ContractVersionListOut:
    """GET every visible version of a contract, highest SemVer first."""
    return _expect(
        ContractVersionListOut, _invoke(_list_versions, transport, contract_id=contract_id)
    )


def get_version_metadata(
    transport: Transport, contract_id: UUID, ref: str
) -> ContractVersionMetadataOut:
    """GET the metadata of one version (``ref`` is ``latest`` or a SemVer string)."""
    return _expect(
        ContractVersionMetadataOut,
        _invoke(_get_metadata, transport, contract_id=contract_id, ref=ref),
    )


def get_validation_report(
    transport: Transport, contract_id: UUID, ref: str
) -> ContractValidationReportOut:
    """GET the stored validation report of one version."""
    return _expect(
        ContractValidationReportOut,
        _invoke(_get_report, transport, contract_id=contract_id, ref=ref),
    )


def get_diff(
    transport: Transport,
    contract_id: UUID,
    from_ref: str,
    to_ref: str,
    *,
    limit: int = 500,
    cursor: str | None = None,
) -> ContractDiffOut:
    """GET the classified differences between two versions."""
    return _expect(
        ContractDiffOut,
        _invoke(
            _get_diff,
            transport,
            contract_id=contract_id,
            from_=from_ref,
            to=to_ref,
            limit=limit,
            cursor=cursor,
        ),
    )


def find_folder(transport: Transport, path: str) -> IdentityFolderListOut:
    """GET the folders matching a slug path such as ``acme/payments/ingest``."""
    return _expect(IdentityFolderListOut, _invoke(_list_folders, transport, path=path))


def get_me(transport: Transport) -> IdentityMeOut:
    """GET who the caller is, with groups, role assignments and permissions."""
    return _expect(IdentityMeOut, _invoke(_get_me, transport))


def get_auth_policy(transport: Transport) -> IdentityAuthPolicyOut:
    """GET the sign-in methods and password rules the server offers."""
    return _expect(IdentityAuthPolicyOut, _invoke(_auth_policy, transport))


def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """Read a streamed body, returning None as soon as it exceeds ``limit`` bytes."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_version(
    transport: Transport,
    contract_id: UUID,
    ref: str,
    *,
    if_none_match: str | None = None,
) -> FetchedVersion:
    """GET the byte-faithful document of one version, streamed and capped at 8 MiB.

    ``if_none_match`` is sent on this request only (the shared client is never changed). A 304
    answer returns ``content=None``. Deprecation and Sunset headers come back as raw strings.
    """
    http = _http(transport)
    kwargs = _get_version._get_kwargs(contract_id=contract_id, ref=ref)
    headers = dict(kwargs.pop("headers", {}))
    if if_none_match is not None:
        headers["If-None-Match"] = if_none_match
    request = http.build_request(headers=headers, **kwargs)
    response = http.send(request, stream=True)
    try:
        status = response.status_code
        if status >= 400:
            body = _read_capped(response, MAX_PROBLEM_BYTES) or b""
            raise_for_problem(_Buffered(status, body, response.headers))
        if status == 304:
            content: bytes | None = None
        elif status == 200:
            content = _read_capped(response, MAX_DOCUMENT_BYTES)
            if content is None:
                raise ServerError(
                    status,
                    title=f"the document is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB",
                )
        else:
            raise ServerError(status, title="the server sent an unexpected response")
        return FetchedVersion(
            status=status,
            content=content,
            etag=response.headers.get("etag"),
            media_type=_media_type(response.headers),
            deprecation=response.headers.get("deprecation"),
            sunset=response.headers.get("sunset"),
        )
    finally:
        response.close()
