"""The Microsoft Graph directory client (D-60, D-62, D-63, AUTH-03, AUTH-04).

Plain httpx through ``dc_core.http`` (no msal: its HTTP bypasses the shared client factory and
cannot be mocked with respx). Everything that is a URL component is validated before a URL is
built, ``@odata.nextLink`` is followed only on the configured Graph scheme and host, and every
failure (transport, 429, 5xx, 401/403, bad JSON, token endpoint) is ``DirectoryUnavailable`` so the
caller answers 503 and never admits on a guess.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from dc_identity.directory import (
    MEMBERS_LIMIT,
    SEARCH_LIMIT,
    ConnectionResult,
    DirectoryEntry,
    DirectoryGroup,
    DirectoryKind,
    DirectoryMatch,
    DirectoryMember,
    DirectoryMemberKind,
    DirectoryMembers,
    DirectorySearchKind,
    DirectorySearchResult,
    DirectoryUnavailable,
    normalize_search_query,
)
from dc_identity.principal_ids import GUID_PATTERN

if TYPE_CHECKING:
    from collections.abc import Mapping

_log = structlog.get_logger("dc_identity.directory.graph")

_GROUP_TYPE = "#microsoft.graph.group"
_SEGMENTS: dict[str, str] = {"user": "users", "service_principal": "servicePrincipals"}
_TOKEN_EARLY_REFRESH_S = 60.0
_SEARCH_SEGMENTS: dict[str, str] = {
    "user": "users",
    "group": "groups",
    "service_principal": "servicePrincipals",
}
# kind -> (Graph $select, properties $search looks at, property shown as the secondary line)
_SEARCH_SELECT: dict[str, str] = {
    "user": "id,displayName,mail,userPrincipalName",
    "group": "id,displayName,mail,description",
    "service_principal": "id,displayName,appId",
}
_SEARCH_PROPERTIES: dict[str, tuple[str, ...]] = {
    "user": ("displayName", "mail", "userPrincipalName"),
    "group": ("displayName", "mail"),
    "service_principal": ("displayName",),
}
_SECONDARY: dict[str, tuple[str, ...]] = {
    "user": ("userPrincipalName", "mail"),
    "group": ("mail", "description"),
    "service_principal": ("appId",),
}
_MEMBER_SELECT = "id,displayName,mail,userPrincipalName,appId,accountEnabled"
_MEMBER_KINDS: dict[str, DirectoryMemberKind] = {
    "#microsoft.graph.user": "user",
    "#microsoft.graph.group": "group",
    "#microsoft.graph.servicePrincipal": "service_principal",
}
# The Graph client's application id: a GUID in Entra; the compose dev stack uses a plain word. Only
# characters that cannot break out of an OData string or a URL are allowed.
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GRAPH_TOP_MAX = 999
_TEXT_MAX = 256
_EVENTUAL = {"ConsistencyLevel": "eventual"}


def _require_guid(value: str, what: str) -> str:
    if not isinstance(value, str) or GUID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{what} must be a lowercase GUID")
    return value


class GraphTokenError(DirectoryUnavailable):
    """The token step failed; ``code`` is the fixed ``check_connection`` answer for it."""

    def __init__(self, code: ConnectionResult, message: str) -> None:
        super().__init__(message)
        self.code = code


class GraphTokenProvider:
    """Client-credentials access tokens for Graph, one per tenant, kept in memory only."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        token_url_template: str,
        client_id: str,
        secret_file: Path,
        scope: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._template = token_url_template
        self._client_id = client_id
        self._secret_file = secret_file
        self._scope = scope
        self._clock = clock
        self._secret: str | None = None
        self._tokens: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _client_secret(self) -> str:
        if self._secret is None:
            try:
                secret = Path(self._secret_file).read_text(encoding="utf-8").strip()
            except OSError:
                raise GraphTokenError(
                    "not-configured", "the Graph client secret file is unreadable"
                ) from None
            if not secret:
                raise GraphTokenError("not-configured", "the Graph client secret file is empty")
            self._secret = secret
        return self._secret

    @property
    def client_id(self) -> str:
        return self._client_id

    def invalidate(self, tenant_id: str) -> None:
        """Forget a tenant's token (Graph refused it), so the next call asks for a new one."""
        self._tokens.pop(tenant_id, None)

    async def token(self, tenant_id: str) -> str:
        _require_guid(tenant_id, "tenant_id")
        held = self._tokens.get(tenant_id)
        if held is not None and self._clock() < held[0]:
            return held[1]
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            held = self._tokens.get(tenant_id)
            if held is not None and self._clock() < held[0]:
                return held[1]
            access, ttl_s = await self._request(tenant_id)
            self._tokens[tenant_id] = (
                self._clock() + max(ttl_s - _TOKEN_EARLY_REFRESH_S, 0),
                access,
            )
            return access

    async def _request(self, tenant_id: str) -> tuple[str, float]:
        secret = self._client_secret()
        url = self._template.format(tenant_id=tenant_id)
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": secret,
            "scope": self._scope,
        }
        try:
            response = await self._http.post(url, data=form)
        except httpx.HTTPError as exc:
            _log.warning("graph_token_transport_error", tenant=tenant_id, error=type(exc).__name__)
            raise GraphTokenError(
                "graph-unreachable", "the Graph token endpoint is unreachable"
            ) from None
        if response.status_code != 200:
            _log.warning("graph_token_refused", tenant=tenant_id, status=response.status_code)
            raise GraphTokenError("token-refused", "the Graph token endpoint refused the request")
        try:
            body = response.json()
            access = body["access_token"]
            expires_in = float(body.get("expires_in", 0))
        except ValueError, KeyError, TypeError, AttributeError:
            raise DirectoryUnavailable("the Graph token response was unusable") from None
        if not isinstance(access, str) or not access:
            raise DirectoryUnavailable("the Graph token response carried no access token")
        return access, expires_in


class GraphDirectoryClient:
    """``DirectoryClient`` over Microsoft Graph for the configured Entra tenants only."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: GraphTokenProvider,
        *,
        base_url: str,
        max_pages: int,
        tenants: frozenset[str],
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._base = base_url.rstrip("/")
        parts = urllib.parse.urlsplit(self._base)
        self._scheme = parts.scheme.lower()
        self._netloc = parts.netloc.lower()
        self._max_pages = max_pages
        self._tenants = tenants

    async def aclose(self) -> None:
        await self._http.aclose()

    def _check(self, tenant_id: str, object_id: str) -> None:
        if tenant_id not in self._tenants:
            raise ValueError("tenant is not a configured Entra tenant")
        _require_guid(object_id, "object_id")

    async def _get(
        self,
        tenant_id: str,
        kind: str,
        url: str,
        params: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._tokens.token(tenant_id)
        headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            _log.warning(
                "graph_transport_error", tenant=tenant_id, kind=kind, error=type(exc).__name__
            )
            raise DirectoryUnavailable("Microsoft Graph is unreachable") from None
        if response.status_code == 401:
            self._tokens.invalidate(tenant_id)
        if response.status_code not in (200, 404):
            _log.warning(
                "graph_unexpected_status", tenant=tenant_id, kind=kind, status=response.status_code
            )
            raise DirectoryUnavailable(f"Microsoft Graph answered {response.status_code}")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise DirectoryUnavailable("Microsoft Graph answered with invalid JSON") from None
        if not isinstance(body, dict):
            raise DirectoryUnavailable("Microsoft Graph answered with an unexpected document")
        return body

    def _on_graph_host(self, link: str) -> bool:
        parts = urllib.parse.urlsplit(link)
        return parts.scheme.lower() == self._scheme and parts.netloc.lower() == self._netloc

    async def lookup(self, tenant_id: str, kind: DirectoryKind, object_id: str) -> DirectoryEntry:
        self._check(tenant_id, object_id)
        segment = _SEGMENTS[kind]
        root = f"{self._base}/{segment}/{object_id}"
        response = await self._get(
            tenant_id, kind, root, {"$select": "id,accountEnabled,displayName"}
        )
        if response.status_code == 404:
            return DirectoryEntry(found=False)
        body = self._json(response)
        enabled = body.get("accountEnabled")
        if not isinstance(enabled, bool):
            # Without the property Graph cannot say whether the account is live: never guess.
            raise DirectoryUnavailable("Microsoft Graph did not report accountEnabled")
        name = body.get("displayName")
        groups = await self._member_groups(tenant_id, kind, f"{root}/transitiveMemberOf")
        return DirectoryEntry(
            found=True,
            account_enabled=enabled,
            display_name=name if isinstance(name, str) else None,
            group_ids=groups,
        )

    async def _member_groups(self, tenant_id: str, kind: str, first_url: str) -> frozenset[str]:
        groups: set[str] = set()
        url: str | None = first_url
        params: dict[str, str] | None = {"$select": "id", "$top": "999"}
        pages = 0
        while url is not None:
            if pages >= self._max_pages:
                _log.warning("graph_page_cap", tenant=tenant_id, kind=kind, pages=pages)
                raise DirectoryUnavailable("Microsoft Graph returned too many pages")
            response = await self._get(tenant_id, kind, url, params)
            if response.status_code == 404:
                raise DirectoryUnavailable("Microsoft Graph lost the principal mid-lookup")
            pages += 1
            body = self._json(response)
            items = body.get("value")
            if not isinstance(items, list):
                raise DirectoryUnavailable("Microsoft Graph answered without a value array")
            for item in items:
                if not isinstance(item, dict) or item.get("@odata.type") != _GROUP_TYPE:
                    continue
                gid = item.get("id")
                if isinstance(gid, str) and GUID_PATTERN.fullmatch(gid.lower()):
                    groups.add(gid.lower())
            link = body.get("@odata.nextLink")
            if link is None:
                url = None
                continue
            if not isinstance(link, str) or not self._on_graph_host(link):
                _log.warning("graph_foreign_next_link", tenant=tenant_id, kind=kind)
                raise DirectoryUnavailable("Microsoft Graph sent a next link off the Graph host")
            url, params = link, None
        return frozenset(groups)

    async def group(self, tenant_id: str, object_id: str) -> DirectoryGroup | None:
        self._check(tenant_id, object_id)
        response = await self._get(
            tenant_id,
            "group",
            f"{self._base}/groups/{object_id}",
            {"$select": "id,displayName"},
        )
        if response.status_code == 404:
            return None
        body = self._json(response)
        name = body.get("displayName")
        return DirectoryGroup(object_id, name if isinstance(name, str) else object_id)

    # --- Search (quick 261119-ofa) -------------------------------------------------------------

    async def search(
        self, tenant_id: str, kind: DirectorySearchKind, query: str
    ) -> DirectorySearchResult:
        """Objects of ``kind`` matching ``query``: a GUID is an exact lookup, anything else $search.

        The query is normalised again here (the port is the trust boundary, not the route), every
        list call sends ``ConsistencyLevel: eventual`` as Graph requires for ``$search``, at most
        ``SEARCH_LIMIT`` items are kept and ``@odata.nextLink`` is never followed. Only tenant,
        kind and counts are logged, never the query.
        """
        if tenant_id not in self._tenants:
            raise ValueError("tenant is not a configured Entra tenant")
        text = normalize_search_query(query)
        segment = _SEARCH_SEGMENTS[kind]
        select = _SEARCH_SELECT[kind]
        guid = text.lower()
        matches: dict[str, DirectoryMatch] = {}
        truncated = False
        if GUID_PATTERN.fullmatch(guid) is not None:
            response = await self._get(
                tenant_id, kind, f"{self._base}/{segment}/{guid}", {"$select": select}
            )
            if response.status_code == 200:
                self._collect(matches, kind, [self._json(response)])
            if kind == "service_principal":
                listed = await self._get(
                    tenant_id,
                    kind,
                    f"{self._base}/{segment}",
                    {"$filter": f"appId eq '{guid}'", "$top": str(SEARCH_LIMIT), "$select": select},
                    _EVENTUAL,
                )
                self._collect(matches, kind, self._list_values(listed))
        else:
            phrase = " OR ".join(f'"{prop}:{text}"' for prop in _SEARCH_PROPERTIES[kind])
            listed = await self._get(
                tenant_id,
                kind,
                f"{self._base}/{segment}",
                {"$search": phrase, "$top": str(SEARCH_LIMIT), "$select": select},
                _EVENTUAL,
            )
            body = self._json(listed)
            values = self._list_values(listed, body)
            truncated = body.get("@odata.nextLink") is not None or len(values) > SEARCH_LIMIT
            self._collect(matches, kind, values)
        items = tuple(list(matches.values())[:SEARCH_LIMIT])
        _log.info(
            "graph_directory_search",
            tenant=tenant_id,
            kind=kind,
            count=len(items),
            truncated=truncated,
        )
        return DirectorySearchResult(items=items, truncated=truncated)

    def _list_values(
        self, response: httpx.Response, body: dict[str, Any] | None = None
    ) -> list[Any]:
        if response.status_code == 404:
            raise DirectoryUnavailable("Microsoft Graph answered 404 for a list call")
        document = body if body is not None else self._json(response)
        values = document.get("value")
        if not isinstance(values, list):
            raise DirectoryUnavailable("Microsoft Graph answered without a value array")
        return values

    @staticmethod
    def _collect(matches: dict[str, DirectoryMatch], kind: str, values: list[Any]) -> None:
        for item in values:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or GUID_PATTERN.fullmatch(raw_id.lower()) is None:
                continue
            object_id = raw_id.lower()
            if object_id in matches:
                continue
            name = item.get("displayName")
            display = name[:_TEXT_MAX] if isinstance(name, str) and name.strip() else object_id
            secondary: str | None = None
            for prop in _SECONDARY[kind]:
                value = item.get(prop)
                if isinstance(value, str) and value.strip():
                    secondary = value[:_TEXT_MAX]
                    break
            matches[object_id] = DirectoryMatch(
                object_id=object_id,
                display_name=display,
                secondary=secondary,
                kind=kind,  # type: ignore[arg-type]
            )

    # --- Group members (quick 261119-wp6) -------------------------------------------------------

    async def group_members(
        self, tenant_id: str, object_id: str, *, limit: int = MEMBERS_LIMIT
    ) -> DirectoryMembers | None:
        """The transitive members of a group (nested groups included), at most ``limit``.

        ``GET /groups/{id}/transitiveMembers`` with ``$top``, ``$count=true`` and
        ``ConsistencyLevel: eventual`` (Graph needs the header for ``$count``); pages are followed
        through ``@odata.nextLink`` on the Graph host only, up to ``max_pages`` pages or ``limit``
        members. ``None`` is a group Graph does not know. Only tenant, counts and the truncation
        flag are logged, never a member name.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._check(tenant_id, object_id)
        url: str | None = f"{self._base}/groups/{object_id}/transitiveMembers"
        params: dict[str, str] | None = {
            "$select": _MEMBER_SELECT,
            "$top": str(min(limit + 1, _GRAPH_TOP_MAX)),
            "$count": "true",
        }
        members: dict[str, DirectoryMember] = {}
        total: int | None = None
        truncated = False
        pages = 0
        while url is not None:
            if pages >= self._max_pages:
                _log.warning("graph_page_cap", tenant=tenant_id, kind="group_members", pages=pages)
                raise DirectoryUnavailable("Microsoft Graph returned too many pages")
            response = await self._get(tenant_id, "group_members", url, params, _EVENTUAL)
            if response.status_code == 404:
                if pages == 0:
                    return None
                raise DirectoryUnavailable("Microsoft Graph lost the group mid-listing")
            pages += 1
            body = self._json(response)
            values = body.get("value")
            if not isinstance(values, list):
                raise DirectoryUnavailable("Microsoft Graph answered without a value array")
            count = body.get("@odata.count")
            if total is None and isinstance(count, int) and not isinstance(count, bool):
                total = count
            for item in values:
                member = self._member(item)
                if member is None or member.object_id in members:
                    continue
                if len(members) >= limit:
                    truncated = True
                    break
                members[member.object_id] = member
            link = body.get("@odata.nextLink")
            if truncated or link is None:
                url = None
                continue
            if not isinstance(link, str) or not self._on_graph_host(link):
                _log.warning("graph_foreign_next_link", tenant=tenant_id, kind="group_members")
                raise DirectoryUnavailable("Microsoft Graph sent a next link off the Graph host")
            if len(members) >= limit:
                truncated = True  # the cap is reached and Graph has more
                url = None
                continue
            url, params = link, None
        items = tuple(members.values())
        _log.info(
            "graph_group_members",
            tenant=tenant_id,
            count=len(items),
            total=total,
            truncated=truncated,
        )
        return DirectoryMembers(items=items, truncated=truncated, total=total)

    @staticmethod
    def _member(item: Any) -> DirectoryMember | None:
        if not isinstance(item, dict):
            return None
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or GUID_PATTERN.fullmatch(raw_id.lower()) is None:
            return None
        object_id = raw_id.lower()
        odata_type = item.get("@odata.type")
        kind: DirectoryMemberKind = (
            _MEMBER_KINDS.get(odata_type, "other") if isinstance(odata_type, str) else "other"
        )
        name = item.get("displayName")
        display = name[:_TEXT_MAX] if isinstance(name, str) and name.strip() else object_id
        secondary: str | None = None
        props = {
            "user": ("userPrincipalName", "mail"),
            "group": ("mail",),
            "service_principal": ("appId",),
            "other": (),
        }[kind]
        for prop in props:
            value = item.get(prop)
            if isinstance(value, str) and value.strip():
                secondary = value[:_TEXT_MAX]
                break
        enabled = item.get("accountEnabled")
        return DirectoryMember(
            object_id=object_id,
            display_name=display,
            secondary=secondary,
            kind=kind,
            account_enabled=enabled if isinstance(enabled, bool) else None,
        )

    # --- Settings overview and connection test (quick 261120-0yc) --------------------------------

    async def check_connection(self, tenant_id: str) -> ConnectionResult:
        """D-12: a token, then ``GET /servicePrincipals?$filter=appId eq '<id>'&$select=id&$top=1``.

        One fixed code; nothing about the failure (no body, no secret) is logged or returned.
        """
        if tenant_id not in self._tenants:
            return "not-configured"
        client_id = self._tokens.client_id.lower()
        if _APP_ID.fullmatch(client_id) is None:
            return "not-configured"
        try:
            token = await self._tokens.token(tenant_id)
        except GraphTokenError as exc:
            return exc.code
        except DirectoryUnavailable:
            return "token-refused"
        try:
            response = await self._http.get(
                f"{self._base}/servicePrincipals",
                params={"$filter": f"appId eq '{client_id}'", "$select": "id", "$top": "1"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            _log.warning("graph_check_transport_error", tenant=tenant_id, error=type(exc).__name__)
            return "graph-unreachable"
        status = response.status_code
        if status == 200:
            try:
                return "ok" if isinstance(response.json().get("value"), list) else "graph-error"
            except ValueError, AttributeError:
                return "graph-error"
        if status == 429:
            return "graph-throttled"
        if status in (401, 403):
            if status == 401:
                self._tokens.invalidate(tenant_id)
            return "graph-forbidden"
        _log.warning("graph_check_unexpected_status", tenant=tenant_id, status=status)
        return "graph-error"

    async def application_secret_expiries(
        self, tenant_id: str, app_id: str
    ) -> list[datetime] | None:
        """D-11: ``endDateTime`` of the Graph app's client secrets and nothing else.

        ``GET /applications(appId='<id>')?$select=passwordCredentials``; ``hint``, ``keyId``,
        ``displayName`` and every other field are dropped here, before anything is cached or
        returned. Any refusal, not-found or failure is ``None`` (unknown).
        """
        app_id = app_id.lower()
        if tenant_id not in self._tenants or _APP_ID.fullmatch(app_id) is None:
            return None
        try:
            response = await self._get(
                tenant_id,
                "application",
                f"{self._base}/applications(appId='{app_id}')",
                {"$select": "passwordCredentials"},
            )
            if response.status_code != 200:
                return None
            credentials = self._json(response).get("passwordCredentials")
        except DirectoryUnavailable:
            return None
        if not isinstance(credentials, list):
            return None
        ends: list[datetime] = []
        for item in credentials:
            raw = item.get("endDateTime") if isinstance(item, dict) else None
            if not isinstance(raw, str):
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                ends.append(parsed)
        return ends
