"""Compose-only Microsoft Graph stand-in for the dev stack and the e2e runner (D-60, AUTH-04).

Answers exactly what identity's real ``GraphDirectoryClient`` asks: the per-tenant
client-credentials token endpoint, ``users`` and ``servicePrincipals`` lookups,
``transitiveMemberOf`` with paging and a ``directoryRole`` item mixed into every page (the client
must keep only groups), ``groups``, ``groups/{id}/transitiveMembers`` (users, nested groups and
service principals, paged, ``$count`` with ``ConsistencyLevel: eventual``), and the list endpoints
the admin directory search uses (``GET /users``, ``/groups`` and ``/servicePrincipals`` with
``$search``, ``$filter=appId eq`` and ``$top``; like real Graph, ``$search`` without
``ConsistencyLevel: eventual`` is a 400).
Two test-only mutators change the directory at run time. It runs on the identity image (FastAPI,
uvicorn, stdlib) and imports nothing from the platform. It sits on the compose backend network
only: no published port and no gateway route, so the mutators are never reachable from outside.

No secret or token is ever logged. The client secret is read from ``/run/secrets`` at each token
request; the access token is random per process.
"""

from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DIRECTORY_FILE = Path(os.environ.get("FAKE_GRAPH_DIRECTORY", "/opt/fake-graph/directory.json"))
CLIENT_ID = os.environ.get("FAKE_GRAPH_CLIENT_ID", "")
SECRET_FILE = Path(os.environ.get("FAKE_GRAPH_SECRET_FILE", "/run/secrets/graph_client_secret"))
# Graph returns at most this many items per page whatever $top asks for, so a 251-group member is
# served over several pages and the client's nextLink handling is exercised.
PAGE_CAP = int(os.environ.get("FAKE_GRAPH_PAGE_CAP", "100"))
MAX_TOP = 999
DEFAULT_TOP = 100

_KINDS = {"users": "users", "servicePrincipals": "servicePrincipals"}
_GROUP_TYPE = "#microsoft.graph.group"
_ROLE_TYPE = "#microsoft.graph.directoryRole"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_ACCESS_TOKEN = secrets.token_urlsafe(32)
_state: dict[str, Any] = {}


def _filler_groups(count: int) -> list[str]:
    return [f"00000000-0000-4000-8000-{i:012x}" for i in range(1, count + 1)]


_LONG_NAME = 200


def _long_text(stem: str, length: int = _LONG_NAME) -> str:
    """``stem`` padded with a fixed lowercase run to exactly ``length`` characters, no spaces."""
    filler = "abcdefghijklmnopqrstuvwxyz"
    padded = stem + (filler * (length // len(filler) + 1))
    return padded[:length]


def _generated_name(prefix: str, index: int) -> str:
    """The display name of generated object ``index`` (volume audit, quick 261120-6i0, D-04).

    Every 10th name is 200 characters without spaces; the 5th and 7th of each ten carry
    right-to-left and CJK text. All keep the prefix, so one substring search finds them.
    """
    if index % 10 == 0:
        return _long_text(f"{prefix.replace(' ', '')}{index:03d}")
    if index % 10 == 5:
        arabic = (
            "\u0645\u062c\u0645\u0648\u0639\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a"
        )
        return f"{prefix} {index:03d} {arabic}"
    if index % 10 == 7:
        return f"{prefix} {index:03d} \u6570\u636e\u56e2\u961f"
    return f"{prefix} {index:03d}"


def _generated_mail(prefix: str, index: int) -> str:
    """A mail address; every 10th has a 64-character local part and a 60-character label."""
    if index % 10 == 0:
        return f"{_long_text(f'{prefix}-{index:03d}-', 64)}@{'v' * 60}.contoso.test"
    return f"{prefix}-{index:03d}@contoso.test"


def _guid(prefix: str, variant: str, index: int) -> str:
    return f"{prefix}-0000-4000-{variant}-{index:012x}"


def _load() -> None:
    seed = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))
    state: dict[str, Any] = {
        "tenants": set(seed["tenants"]),
        "groups": copy.deepcopy(seed.get("groups", {})),
        "role_id": seed["directoryRoleId"],
        "users": {},
        "servicePrincipals": {},
    }
    # Every object id is unique across groups, users and service principals; a collision between
    # the hand-written fixtures and the generated ranges must fail loudly, not shadow a fixture.
    seen: dict[str, str] = {gid: "groups" for gid in state["groups"]}

    def claim(kind: str, oid: str) -> None:
        if oid in seen:
            raise RuntimeError(
                f"fake-graph directory: object id {oid} is both {seen[oid]} and {kind}"
            )
        seen[oid] = kind

    for kind in _KINDS:
        for oid, entry in seed.get(kind, {}).items():
            claim(kind, oid)
            groups = list(entry.get("groups", [])) + _filler_groups(
                entry.get("generated_groups", 0)
            )
            state[kind][oid] = {
                "displayName": entry.get("displayName"),
                "accountEnabled": entry.get("accountEnabled", True),
                "groups": groups,
                "deleted": False,
                "mail": entry.get("mail"),
                "userPrincipalName": entry.get("userPrincipalName"),
                "appId": entry.get("appId"),
            }
    generated = seed.get("generated", {})
    spec = generated.get("groups")
    if spec:
        for i in range(1, int(spec["count"]) + 1):
            gid = _guid(spec["oid_prefix"], "8000", i)
            claim("groups", gid)
            state["groups"][gid] = {
                "displayName": _generated_name(spec["name_prefix"], i),
                "mail": _generated_mail(spec["mail_prefix"], i),
            }
    spec = generated.get("users")
    if spec:
        for i in range(1, int(spec["count"]) + 1):
            oid = _guid(spec["oid_prefix"], "8000", i)
            claim("users", oid)
            mail = _generated_mail(spec["mail_prefix"], i)
            state["users"][oid] = {
                "displayName": _generated_name(spec["name_prefix"], i),
                "accountEnabled": True,
                "groups": [],
                "deleted": False,
                "mail": mail,
                "userPrincipalName": mail,
                "appId": None,
            }
    spec = generated.get("servicePrincipals")
    if spec:
        for i in range(1, int(spec["count"]) + 1):
            oid = _guid(spec["oid_prefix"], "8000", i)
            claim("servicePrincipals", oid)
            state["servicePrincipals"][oid] = {
                "displayName": _generated_name(spec["name_prefix"], i),
                "accountEnabled": True,
                "groups": [],
                "deleted": False,
                "mail": None,
                "userPrincipalName": None,
                "appId": _guid(spec["app_id_prefix"], "8000", i),
            }
            claim("appId", state["servicePrincipals"][oid]["appId"])
    for gid, group_entry in list(state["groups"].items()):
        oid_prefix = group_entry.get("member_oid_prefix", "00000000")
        name_prefix = group_entry.get("member_name_prefix", "Crew Member")
        mail_prefix = group_entry.get("member_mail_prefix", "crew")
        for i in range(1, int(group_entry.get("generated_members", 0)) + 1):
            oid = _guid(oid_prefix, "9000", i)
            claim("users", oid)
            state["users"][oid] = {
                "displayName": f"{name_prefix} {i:03d}",
                "accountEnabled": True,
                "groups": [gid],
                "deleted": False,
                "mail": f"{mail_prefix}{i:03d}@contoso.test",
                "userPrincipalName": f"{mail_prefix}{i:03d}@contoso.test",
                "appId": None,
            }
    _state.clear()
    _state.update(state)


_load()


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "InvalidAuthenticationToken", "message": "unauthorized"}},
        status_code=401,
    )


def _not_found() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "Request_ResourceNotFound", "message": "not found"}}, status_code=404
    )


def _authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token, _ACCESS_TOKEN)


def _principal(kind: str, oid: str) -> dict[str, Any] | None:
    entry = _state[kind].get(oid.lower())
    if entry is None or entry["deleted"]:
        return None
    return entry  # type: ignore[no-any-return]


def _expected_secret() -> str:
    try:
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/{tenant}/oauth2/v2.0/token")
async def token(tenant: str, request: Request) -> JSONResponse:
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))

    def field(name: str) -> str:
        return form.get(name, [""])[0]

    expected = _expected_secret()
    ok = (
        tenant in _state["tenants"]
        and field("grant_type") == "client_credentials"
        and bool(CLIENT_ID)
        and hmac.compare_digest(field("client_id"), CLIENT_ID)
        and bool(expected)
        and hmac.compare_digest(field("client_secret"), expected)
    )
    if not ok:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "client authentication failed"},
            status_code=401,
        )
    return JSONResponse({"access_token": _ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600})


_LIST_KINDS = {"users", "groups", "servicePrincipals"}
_SEARCH_TERM = re.compile(r'"([A-Za-z]+):([^"]*)"')
# A GUID, or the compose dev stack's own client id ("identity-graph-dev"): no quote can appear.
_APP_ID_FILTER = re.compile(r"^appId eq '([A-Za-z0-9][A-Za-z0-9._-]{0,63})'$")


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "Request_BadRequest", "message": message}}, status_code=400
    )


@app.get("/v1.0/users")
def list_users(request: Request) -> JSONResponse:
    return _list("users", request)


@app.get("/v1.0/groups")
def list_groups(request: Request) -> JSONResponse:
    return _list("groups", request)


@app.get("/v1.0/servicePrincipals")
def list_service_principals(request: Request) -> JSONResponse:
    return _list("servicePrincipals", request)


def _list(kind: str, request: Request) -> JSONResponse:
    """$search of "prop:text" terms joined by OR (substring), and $filter=appId eq '<guid>'."""
    if not _authorized(request):
        return _unauthorized()
    query = request.query_params
    search = query.get("$search")
    if search is not None and request.headers.get("consistencylevel", "").lower() != "eventual":
        return _bad_request(
            "Request_UnsupportedQuery: $search needs the ConsistencyLevel: eventual header."
        )
    terms = _SEARCH_TERM.findall(search) if search is not None else None
    if search is not None and not terms:
        return _bad_request("Request_UnsupportedQuery: unsupported $search expression.")
    app_id: str | None = None
    if "$filter" in query:
        found = _APP_ID_FILTER.match(query["$filter"])
        if kind != "servicePrincipals" or found is None:
            return _bad_request("Request_UnsupportedQuery: unsupported $filter expression.")
        app_id = found.group(1).lower()
    source: dict[str, dict[str, Any]] = _state[kind]
    rows: list[dict[str, Any]] = []
    for oid, entry in source.items():
        if entry.get("deleted"):
            continue
        if terms is not None and not any(
            value.lower() in str(entry.get(prop) or "").lower() for prop, value in terms
        ):
            continue
        if app_id is not None and str(entry.get("appId") or "").lower() != app_id:
            continue
        rows.append(
            {
                "id": oid,
                "displayName": entry.get("displayName"),
                "mail": entry.get("mail"),
                "userPrincipalName": entry.get("userPrincipalName"),
                "appId": entry.get("appId"),
                "description": entry.get("description"),
            }
        )
    rows.sort(key=lambda row: str(row["displayName"] or "").lower())
    requested = int(query.get("$top", DEFAULT_TOP))
    offset = max(int(query.get("$skiptoken", 0)), 0)
    size = max(1, min(requested, MAX_TOP, PAGE_CAP))
    page = rows[offset : offset + size]
    selected = (
        {name.strip() for name in query["$select"].split(",")} if "$select" in query else None
    )
    items = [{k: v for k, v in row.items() if selected is None or k in selected} for row in page]
    body: dict[str, Any] = {"value": items}
    if offset + size < len(rows):
        params = {"$top": str(requested), "$skiptoken": str(offset + size)}
        for name in ("$search", "$filter", "$select"):
            if name in query:
                params[name] = query[name]
        base = str(request.base_url).rstrip("/")
        body["@odata.nextLink"] = f"{base}/v1.0/{kind}?{urllib.parse.urlencode(params)}"
    return JSONResponse(body)


@app.get("/v1.0/groups/{oid}")
def group(oid: str, request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    entry = _state["groups"].get(oid.lower())
    if entry is None:
        return _not_found()
    return JSONResponse(
        {
            "id": oid.lower(),
            "displayName": entry["displayName"],
            "mail": entry.get("mail"),
            "description": entry.get("description"),
        }
    )


_MEMBER_TYPES = {
    "users": "#microsoft.graph.user",
    "groups": _GROUP_TYPE,
    "servicePrincipals": "#microsoft.graph.servicePrincipal",
}


def _descendant_groups(root: str) -> set[str]:
    """``root`` and every group nested in it (a group lists its parents in ``memberOf``)."""
    found = {root}
    grew = True
    while grew:
        grew = False
        for gid, entry in _state["groups"].items():
            if gid not in found and found & {m.lower() for m in entry.get("memberOf", [])}:
                found.add(gid)
                grew = True
    return found


@app.get("/v1.0/groups/{oid}/transitiveMembers")
def transitive_members(oid: str, request: Request) -> JSONResponse:
    """Users, service principals and nested groups, transitively, paged like Graph."""
    if not _authorized(request):
        return _unauthorized()
    oid = oid.lower()
    if oid not in _state["groups"]:
        return _not_found()
    query = request.query_params
    counting = query.get("$count", "").lower() == "true"
    if counting and request.headers.get("consistencylevel", "").lower() != "eventual":
        return _bad_request(
            "Request_UnsupportedQuery: $count needs the ConsistencyLevel: eventual header."
        )
    scope = _descendant_groups(oid)
    rows: list[dict[str, Any]] = []
    for gid in scope - {oid}:
        entry = _state["groups"][gid]
        rows.append(
            {
                "@odata.type": _MEMBER_TYPES["groups"],
                "id": gid,
                "displayName": entry.get("displayName"),
                "mail": entry.get("mail"),
            }
        )
    for kind in _KINDS:
        for member_id, entry in _state[kind].items():
            if entry.get("deleted") or not scope & {g.lower() for g in entry["groups"]}:
                continue
            rows.append(
                {
                    "@odata.type": _MEMBER_TYPES[kind],
                    "id": member_id,
                    "displayName": entry.get("displayName"),
                    "mail": entry.get("mail"),
                    "userPrincipalName": entry.get("userPrincipalName"),
                    "appId": entry.get("appId"),
                    "accountEnabled": entry.get("accountEnabled"),
                }
            )
    rows.sort(key=lambda row: (str(row["displayName"] or "").lower(), row["id"]))
    requested = int(query.get("$top", DEFAULT_TOP))
    offset = max(int(query.get("$skiptoken", 0)), 0)
    size = max(1, min(requested, MAX_TOP, PAGE_CAP))
    selected = (
        {name.strip() for name in query["$select"].split(",")} | {"@odata.type"}
        if "$select" in query
        else None
    )
    items = [
        {k: v for k, v in row.items() if v is not None and (selected is None or k in selected)}
        for row in rows[offset : offset + size]
    ]
    body: dict[str, Any] = {"value": items}
    if counting:
        body["@odata.count"] = len(rows)
    if offset + size < len(rows):
        params = {"$top": str(requested), "$skiptoken": str(offset + size)}
        for name in ("$select", "$count"):
            if name in query:
                params[name] = query[name]
        base = str(request.base_url).rstrip("/")
        body["@odata.nextLink"] = (
            f"{base}/v1.0/groups/{oid}/transitiveMembers?{urllib.parse.urlencode(params)}"
        )
    return JSONResponse(body)


@app.get("/v1.0/{kind}/{oid}/transitiveMemberOf")
def transitive_member_of(kind: str, oid: str, request: Request) -> JSONResponse:
    # Graph's `$top`, `$skiptoken` and `$select` are dollar-prefixed query names, read directly.
    if not _authorized(request):
        return _unauthorized()
    if kind not in _KINDS:
        return _not_found()
    entry = _principal(kind, oid)
    if entry is None:
        return _not_found()
    query = request.query_params
    requested = int(query.get("$top", DEFAULT_TOP))
    offset = max(int(query.get("$skiptoken", 0)), 0)
    size = max(1, min(requested, MAX_TOP, PAGE_CAP))
    groups: list[str] = entry["groups"]
    page = groups[offset : offset + size]
    items: list[dict[str, str]] = [{"@odata.type": _GROUP_TYPE, "id": gid} for gid in page]
    # Every answer also carries a directory role: the client must keep only groups.
    items.append({"@odata.type": _ROLE_TYPE, "id": _state["role_id"]})
    body: dict[str, Any] = {"value": items}
    if offset + size < len(groups):
        params = {"$top": str(requested), "$skiptoken": str(offset + size)}
        if "$select" in query:
            params["$select"] = query["$select"]
        base = str(request.base_url).rstrip("/")
        body["@odata.nextLink"] = (
            f"{base}/v1.0/{kind}/{oid}/transitiveMemberOf?{urllib.parse.urlencode(params)}"
        )
    return JSONResponse(body)


@app.get("/v1.0/applications(appId='{app_id}')")
def application(app_id: str, request: Request) -> JSONResponse:
    """The Graph application's own client-secret credentials (quick 261120-0yc, D-11).

    Like real Graph the credentials carry a ``hint`` (the first characters of the secret) that
    identity must drop; the fixed ``endDateTime`` is far in the future so the dev stack shows a
    known expiry.
    """
    if not _authorized(request):
        return _unauthorized()
    if not CLIENT_ID or app_id.lower() != CLIENT_ID.lower():
        return _not_found()
    return JSONResponse(
        {
            "id": "00000000-0000-4000-8000-00000000a99c",
            "passwordCredentials": [
                {
                    "keyId": "00000000-0000-4000-8000-00000000beef",
                    "displayName": "fake-graph dev secret",
                    "hint": "fK8",
                    "startDateTime": "2026-01-01T00:00:00Z",
                    "endDateTime": "2099-01-01T00:00:00Z",
                }
            ],
        }
    )


@app.get("/v1.0/{kind}/{oid}")
def principal(kind: str, oid: str, request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    if kind not in _KINDS:
        return _not_found()
    entry = _principal(kind, oid)
    if entry is None:
        return _not_found()
    return JSONResponse(
        {
            "id": oid.lower(),
            "accountEnabled": entry["accountEnabled"],
            "displayName": entry["displayName"],
            "mail": entry.get("mail"),
            "userPrincipalName": entry.get("userPrincipalName"),
            "appId": entry.get("appId"),
        }
    )


@app.post("/_fake/reset")
def reset() -> dict[str, str]:
    _load()
    return {"status": "reset"}


@app.post("/_fake/groups/{oid}")
async def mutate_group(oid: str, request: Request) -> dict[str, str]:
    body = await request.json()
    _state["groups"][oid.lower()] = {"displayName": str(body.get("displayName", oid.lower()))}
    return {"status": "ok"}


@app.post("/_fake/principals/{oid}")
async def mutate_principal(oid: str, request: Request) -> dict[str, str]:
    """Change a principal: ``accountEnabled``, ``groups`` (replaces the list) and ``deleted``.

    ``kind`` (``users`` or ``servicePrincipals``) creates the principal when it is not seeded.
    """
    body = await request.json()
    oid = oid.lower()
    kind = next((k for k in _KINDS if oid in _state[k]), None)
    if kind is None:
        kind = body.get("kind")
        if kind not in _KINDS:
            raise HTTPException(status_code=404, detail="unknown principal; pass kind to create it")
        _state[kind][oid] = {
            "displayName": body.get("displayName", oid),
            "accountEnabled": True,
            "groups": [],
            "deleted": False,
        }
    entry = _state[kind][oid]
    if "accountEnabled" in body:
        entry["accountEnabled"] = bool(body["accountEnabled"])
    if "groups" in body:
        entry["groups"] = [str(g).lower() for g in body["groups"]]
    if "deleted" in body:
        entry["deleted"] = bool(body["deleted"])
    return {"status": "ok"}
