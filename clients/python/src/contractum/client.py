"""The ``Client``: typed, read-only access to a Contractum server."""

from __future__ import annotations

import hashlib
import os
import re
import time
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from itertools import islice
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import httpx

from contractum import _api
from contractum._transport import (
    ConnectionSettings,
    build_http_client,
    install_log_redaction,
    parse_deprecation,
    parse_sunset,
)
from contractum.auth import resolve_token_provider
from contractum.auth.tokens import BearerAuth, StaticToken, TokenProvider
from contractum.cache import Cache
from contractum.config import Overrides, Settings, resolve_settings
from contractum.contracts import ContractDeprecationWarning, ContractVersion
from contractum.errors import (
    ConfigError,
    IntegrityError,
    NotFoundError,
    ServerError,
    UsageError,
)
from contractum.models import (
    AuthPolicy,
    ContractDiff,
    ContractPage,
    ContractSummary,
    FetchResult,
    Folder,
    Identity,
    StoredValidationReport,
    VersionInfo,
)
from contractum.refs import (
    ResolvedContract,
    parse_ref,
    parse_version,
    resolve_ref,
    resolve_version_info,
)

if TYPE_CHECKING:
    from contractum.pull import PullResult

MAX_PAGE_SIZE = 100
_REF = re.compile(r"latest|[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
ContractId = UUID | str


def _contract_id(value: ContractId) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except ValueError:
        raise UsageError(
            "a contract id is a UUID", hint="resolve an ODCS id with list_contracts(odcs_id=...)"
        ) from None


def _ref(value: str) -> str:
    if not _REF.fullmatch(value):
        raise UsageError(f"a version reference is 'latest' or an exact SemVer, not {value!r}")
    return value


class Client:
    """A read-only Contractum client bound to one server and one credential.

    Pass exactly one of ``token`` (a PAT, service-account key or JWT) and ``token_provider``.
    ``http_client`` replaces the built-in transport (tests); the caller then owns its auth.
    Every request goes through ``contractum._api`` and the fenced transport: the client can read
    and sign in, and it cannot change anything on the server.
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        token_provider: TokenProvider | None = None,
        ca_bundle: str | os.PathLike[str] | None = None,
        timeout: float = 30.0,
        allow_insecure_http: bool = False,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cache: Cache | Literal[False] | None = None,
    ) -> None:
        if (token is None) == (token_provider is None):
            raise ConfigError("pass exactly one of token and token_provider")
        provider = token_provider if token_provider is not None else StaticToken(token or "")
        install_log_redaction()
        self._settings = ConnectionSettings(
            url=url,
            ca_bundle=Path(ca_bundle) if ca_bundle is not None else None,
            allow_insecure_http=allow_insecure_http,
            timeout=timeout,
        )
        self._sleep = sleep
        self._injected = http_client
        self._http = (
            http_client
            if http_client is not None
            else build_http_client(self._settings, auth=BearerAuth(provider), sleep=sleep)
        )
        self._anonymous: httpx.Client | None = None
        self._cache: Cache | None = Cache() if cache is None else cache or None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        token_provider: TokenProvider | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cache: Cache | Literal[False] | None = None,
    ) -> Client:
        """Build a client from resolved settings; the credential defaults to the env token."""
        provider = (
            token_provider if token_provider is not None else resolve_token_provider(settings)
        )
        return cls(
            settings.url,
            token_provider=provider,
            ca_bundle=settings.ca_bundle,
            timeout=settings.timeout,
            allow_insecure_http=settings.allow_insecure_http,
            http_client=http_client,
            sleep=sleep,
            cache=cache,
        )

    @classmethod
    def from_env(
        cls,
        *,
        profile: str | None = None,
        url: str | None = None,
        ca_bundle: str | os.PathLike[str] | None = None,
        allow_insecure_http: bool | None = None,
        env: Mapping[str, str] | None = None,
        path: Path | None = None,
    ) -> Client:
        """Build a client from arguments, ``CONTRACTUM_*`` variables and the profiles file."""
        settings = resolve_settings(
            profile=profile,
            overrides=Overrides(
                url=url, ca_bundle=ca_bundle, allow_insecure_http=allow_insecure_http
            ),
            env=env,
            path=path,
        )
        return cls.from_settings(settings)

    def __repr__(self) -> str:
        return f"Client(url={self._settings.url!r})"

    @property
    def url(self) -> str:
        """The server URL this client talks to."""
        return self._settings.url

    @property
    def cache(self) -> Cache | None:
        """The revalidation cache, None when caching is off."""
        return self._cache

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection pools this client built (an injected client stays open)."""
        if self._injected is None:
            self._http.close()
        if self._anonymous is not None:
            self._anonymous.close()
            self._anonymous = None

    # --- identity -----------------------------------------------------------------------

    def whoami(self) -> Identity:
        """Return who the token belongs to (``GET /api/v1/identity/me``)."""
        return Identity.from_generated(_api.get_me(self._http))

    def auth_policy(self) -> AuthPolicy:
        """Return the sign-in methods the server offers (a public call, sent without a token)."""
        return AuthPolicy.from_generated(_api.get_auth_policy(self._public_http()))

    def _public_http(self) -> httpx.Client:
        if self._injected is not None:
            return self._injected
        if self._anonymous is None:
            self._anonymous = build_http_client(self._settings, auth=None, sleep=self._sleep)
        return self._anonymous

    def find_folder(self, path: str) -> Folder | None:
        """Return the folder at a slug path such as ``acme/payments/ingest``, or None."""
        wanted = path.strip().strip("/")
        if not wanted:
            raise UsageError("a folder path is not empty")
        try:
            page = _api.find_folder(self._http, wanted)
        except NotFoundError:
            return None
        for item in page.items:
            folder = Folder.from_generated(item)
            if folder.path == wanted:
                return folder
        return None

    # --- contracts ----------------------------------------------------------------------

    def list_contracts(
        self,
        *,
        folder_id: UUID | str | None = None,
        include_subtree: bool = False,
        odcs_id: str | None = None,
        name: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ContractPage:
        """Return one page of the contracts the caller can read; ``limit`` is clamped to 1..100."""
        page = _api.list_contracts(
            self._http,
            folder_id=_contract_id(folder_id) if folder_id is not None else None,
            include_subtree=include_subtree,
            odcs_id=odcs_id,
            name=name,
            domain=domain,
            status=status,
            tags=tags,
            q=q,
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            cursor=cursor,
        )
        return ContractPage.from_generated(page)

    def iter_contracts(
        self,
        *,
        folder_id: UUID | str | None = None,
        include_subtree: bool = False,
        odcs_id: str | None = None,
        name: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        q: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Iterator[ContractSummary]:
        """Yield every matching contract, following ``next_cursor`` until the last page."""
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list_contracts(
                folder_id=folder_id,
                include_subtree=include_subtree,
                odcs_id=odcs_id,
                name=name,
                domain=domain,
                status=status,
                tags=tags,
                q=q,
                limit=page_size,
                cursor=cursor,
            )
            yield from page.items
            cursor = page.next_cursor
            if cursor is None:
                return
            if cursor in seen:
                raise ServerError(200, title="the server repeated a page cursor")
            seen.add(cursor)

    def get_contract(self, contract_id: ContractId) -> ContractSummary:
        """Return one contract's metadata."""
        return ContractSummary.from_generated(
            _api.get_contract(self._http, _contract_id(contract_id))
        )

    def list_versions(self, contract_id: ContractId) -> list[VersionInfo]:
        """Return every visible version in the server's order (highest SemVer first)."""
        listing = _api.list_versions(self._http, _contract_id(contract_id))
        return [VersionInfo.from_generated(item) for item in listing.items]

    def version_metadata(self, contract_id: ContractId, ref: str = "latest") -> VersionInfo:
        """Return the metadata of ``latest`` or an exact SemVer version."""
        return VersionInfo.from_generated(
            _api.get_version_metadata(self._http, _contract_id(contract_id), _ref(ref))
        )

    def fetch_document(
        self, contract_id: ContractId, ref: str = "latest", *, if_none_match: str | None = None
    ) -> FetchResult:
        """Fetch the byte-faithful document of a version; ``content`` is None on a 304."""
        fetched = _api.fetch_version(
            self._http, _contract_id(contract_id), _ref(ref), if_none_match=if_none_match
        )
        return FetchResult(
            status=fetched.status,
            content=fetched.content,
            etag=fetched.etag,
            media_type=fetched.media_type,
            deprecation=parse_deprecation(fetched.deprecation),
            sunset=parse_sunset(fetched.sunset),
        )

    def validation_report(
        self, contract_id: ContractId, ref: str = "latest"
    ) -> StoredValidationReport:
        """Return the validation report stored when the version was published."""
        return StoredValidationReport.from_generated(
            _api.get_validation_report(self._http, _contract_id(contract_id), _ref(ref))
        )

    def diff(
        self,
        contract_id: ContractId,
        from_ref: str,
        to_ref: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> ContractDiff:
        """Return the classified differences between two versions."""
        return ContractDiff.from_generated(
            _api.get_diff(
                self._http,
                _contract_id(contract_id),
                from_ref,
                to_ref,
                limit=max(1, min(limit, 500)),
                cursor=cursor,
            )
        )

    # --- fetching by reference ----------------------------------------------------------

    def locate(
        self, ref: str, *, version: str | None = None
    ) -> tuple[ResolvedContract, VersionInfo]:
        """Resolve a reference to its contract and the metadata of the version it names.

        Nothing is downloaded. ``version`` overrides the version part of the reference.
        """
        parsed = parse_ref(ref)
        chosen = parse_version(version) if version is not None else parsed.version
        resolved = resolve_ref(self, parsed)
        label = resolved.odcs_id or parsed.ident
        return resolved, resolve_version_info(self, resolved.contract_id, chosen, label=label)

    def get(self, ref: str, *, version: str | None = None) -> ContractVersion:
        """Fetch one contract version by reference and prove its bytes with the server's hash.

        ``ref`` is ``[<folder-path>:]<odcs-id-or-uuid>[@latest|@preview|@X.Y.Z]``; ``version``
        overrides the version part. The exact SemVer is read from the metadata first, so a Latest
        that moves meanwhile cannot mix two versions. The answer is never a different version
        than the one asked for. A deprecated version emits ``ContractDeprecationWarning``.
        """
        resolved, info = self.locate(ref, version=version)
        contract = self._document(resolved, info)
        notice = contract.deprecation_notice
        if notice is not None:
            warnings.warn(notice, ContractDeprecationWarning, stacklevel=2)
        return contract

    def _cached(self, resolved: ResolvedContract, info: VersionInfo) -> ContractVersion | None:
        """The cached copy of exactly this version, or None (an unreadable cache is a miss)."""
        if self._cache is None:
            return None
        try:
            found = self._cache.get(self.url, str(resolved.contract_id), info.semver)
        except OSError:
            return None
        return found if found is not None and found.content_sha256 == info.content_sha256 else None

    def _document(self, resolved: ResolvedContract, info: VersionInfo) -> ContractVersion:
        cached = self._cached(resolved, info)
        validator = None
        if cached is not None:
            validator = cached.etag or f'"{cached.content_sha256}"'
        fetched = self.fetch_document(resolved.contract_id, info.semver, if_none_match=validator)
        name = resolved.odcs_id or str(resolved.contract_id)
        content = fetched.content
        source: Literal["server", "cache"] = "server"
        if content is None and cached is not None:
            content, source = cached.content, "cache"
        if content is None:
            raise ServerError(fetched.status, title="the server sent no document")
        digest = hashlib.sha256(content).hexdigest()
        tag = (fetched.etag or "").strip().removeprefix("W/").strip('"')
        if digest != info.content_sha256 or (fetched.etag is not None and tag != digest):
            raise IntegrityError(f"content hash mismatch for {name}@{info.semver}")
        sunset = info.sunset_date or (fetched.sunset.date() if fetched.sunset else None)
        contract = ContractVersion(
            contract_id=str(resolved.contract_id),
            odcs_id=resolved.odcs_id,
            name=resolved.name,
            semver=info.semver,
            status=info.status,
            media_type=fetched.media_type or info.media_type,
            content=content,
            content_sha256=digest,
            etag=fetched.etag,
            deprecated=info.deprecated or fetched.deprecation is not None,
            deprecated_at=info.deprecated_at or fetched.deprecation,
            sunset_date=sunset,
            published_at=info.published_at,
            source=source,
        )
        if source == "server" and self._cache is not None:
            with suppress(OSError):
                self._cache.put(self.url, contract)
        return contract

    def pull(self, refs: Sequence[str] = (), **kwargs: Any) -> PullResult:
        """Pin contracts into files and a lock file; see ``contractum.pull.pull`` for options."""
        from contractum.pull import pull

        return pull(self, refs, **kwargs)

    def search(
        self,
        query: str | None = None,
        *,
        folder: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        all: bool = False,
    ) -> list[ContractSummary]:
        """List the contracts the caller can read, in the server's order.

        ``query`` is a free-text search, ``folder`` a slug path (its subtree is searched). At most
        ``limit`` contracts are returned unless ``all`` is true, which follows every page.
        """
        if limit < 1:
            raise UsageError("limit is at least 1")
        folder_id = None
        if folder is not None:
            found = self.find_folder(folder)
            if found is None:
                raise NotFoundError(f"no readable folder {folder.strip().strip('/')!r}")
            folder_id = found.id
        contracts = self.iter_contracts(
            folder_id=folder_id,
            include_subtree=folder_id is not None,
            domain=domain,
            status=status,
            tags=tags,
            q=query,
            page_size=min(limit, MAX_PAGE_SIZE),
        )
        return list(contracts if all else islice(contracts, limit))

    def versions(self, ref: str) -> list[VersionInfo]:
        """List every readable version of the contract a reference names, highest first."""
        parsed = parse_ref(ref)
        if parsed.version is not None:
            raise UsageError("versions takes a reference without a version suffix")
        return self.list_versions(resolve_ref(self, parsed).contract_id)
