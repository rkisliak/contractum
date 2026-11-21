from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.contract_list_out import ContractListOut
from ...models.problem import Problem
from ...types import UNSET, Unset
from typing import cast
from uuid import UUID



def _get_kwargs(
    *,
    folder_id: None | Unset | UUID = UNSET,
    include_subtree: bool | Unset = False,
    odcs_id: None | str | Unset = UNSET,
    name: None | str | Unset = UNSET,
    domain: None | str | Unset = UNSET,
    status: None | str | Unset = UNSET,
    tags: list[str] | None | Unset = UNSET,
    q: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> dict[str, Any]:
    

    

    params: dict[str, Any] = {}

    json_folder_id: None | str | Unset
    if isinstance(folder_id, Unset):
        json_folder_id = UNSET
    elif isinstance(folder_id, UUID):
        json_folder_id = str(folder_id)
    else:
        json_folder_id = folder_id
    params["folder_id"] = json_folder_id

    params["include_subtree"] = include_subtree

    json_odcs_id: None | str | Unset
    if isinstance(odcs_id, Unset):
        json_odcs_id = UNSET
    else:
        json_odcs_id = odcs_id
    params["odcs_id"] = json_odcs_id

    json_name: None | str | Unset
    if isinstance(name, Unset):
        json_name = UNSET
    else:
        json_name = name
    params["name"] = json_name

    json_domain: None | str | Unset
    if isinstance(domain, Unset):
        json_domain = UNSET
    else:
        json_domain = domain
    params["domain"] = json_domain

    json_status: None | str | Unset
    if isinstance(status, Unset):
        json_status = UNSET
    else:
        json_status = status
    params["status"] = json_status

    json_tags: list[str] | None | Unset
    if isinstance(tags, Unset):
        json_tags = UNSET
    elif isinstance(tags, list):
        json_tags = tags


    else:
        json_tags = tags
    params["tags"] = json_tags

    json_q: None | str | Unset
    if isinstance(q, Unset):
        json_q = UNSET
    else:
        json_q = q
    params["q"] = json_q

    params["limit"] = limit

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/contracts/contracts",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ContractListOut | Problem | None:
    if response.status_code == 200:
        response_200 = ContractListOut.from_dict(response.json())



        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())



        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = Problem.from_dict(response.json())



        return response_403

    if response.status_code == 422:
        response_422 = Problem.from_dict(response.json())



        return response_422

    if response.status_code == 429:
        response_429 = Problem.from_dict(response.json())



        return response_429

    if response.status_code == 503:
        response_503 = Problem.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ContractListOut | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    folder_id: None | Unset | UUID = UNSET,
    include_subtree: bool | Unset = False,
    odcs_id: None | str | Unset = UNSET,
    name: None | str | Unset = UNSET,
    domain: None | str | Unset = UNSET,
    status: None | str | Unset = UNSET,
    tags: list[str] | None | Unset = UNSET,
    q: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> Response[ContractListOut | Problem]:
    """ List Contracts

     List and search contracts (D-12): filters, ``q`` text match, keyset pagination.

    ``require("contract:list")`` admits every authenticated caller; the identity scopes below trim
    the SQL, so a caller without access gets an empty page (D-35). A row appears when the caller
    holds ``contract:read-metadata`` there, or ``contract:read-published`` and the contract has a
    Latest. Every row is the contract's head: the Latest once one exists (for every caller), else
    the newest draft. Its content-derived fields are filled only where the caller holds
    ``contract:read`` (D-36) or ``contract:read-published``, and content filters match only those
    rows, so a published-only caller can never match a draft's values.

    Args:
        folder_id (None | Unset | UUID):
        include_subtree (bool | Unset):  Default: False.
        odcs_id (None | str | Unset):
        name (None | str | Unset):
        domain (None | str | Unset):
        status (None | str | Unset):
        tags (list[str] | None | Unset):
        q (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContractListOut | Problem]
     """


    kwargs = _get_kwargs(
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

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    folder_id: None | Unset | UUID = UNSET,
    include_subtree: bool | Unset = False,
    odcs_id: None | str | Unset = UNSET,
    name: None | str | Unset = UNSET,
    domain: None | str | Unset = UNSET,
    status: None | str | Unset = UNSET,
    tags: list[str] | None | Unset = UNSET,
    q: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> ContractListOut | Problem | None:
    """ List Contracts

     List and search contracts (D-12): filters, ``q`` text match, keyset pagination.

    ``require("contract:list")`` admits every authenticated caller; the identity scopes below trim
    the SQL, so a caller without access gets an empty page (D-35). A row appears when the caller
    holds ``contract:read-metadata`` there, or ``contract:read-published`` and the contract has a
    Latest. Every row is the contract's head: the Latest once one exists (for every caller), else
    the newest draft. Its content-derived fields are filled only where the caller holds
    ``contract:read`` (D-36) or ``contract:read-published``, and content filters match only those
    rows, so a published-only caller can never match a draft's values.

    Args:
        folder_id (None | Unset | UUID):
        include_subtree (bool | Unset):  Default: False.
        odcs_id (None | str | Unset):
        name (None | str | Unset):
        domain (None | str | Unset):
        status (None | str | Unset):
        tags (list[str] | None | Unset):
        q (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContractListOut | Problem
     """


    return sync_detailed(
        client=client,
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

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    folder_id: None | Unset | UUID = UNSET,
    include_subtree: bool | Unset = False,
    odcs_id: None | str | Unset = UNSET,
    name: None | str | Unset = UNSET,
    domain: None | str | Unset = UNSET,
    status: None | str | Unset = UNSET,
    tags: list[str] | None | Unset = UNSET,
    q: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> Response[ContractListOut | Problem]:
    """ List Contracts

     List and search contracts (D-12): filters, ``q`` text match, keyset pagination.

    ``require("contract:list")`` admits every authenticated caller; the identity scopes below trim
    the SQL, so a caller without access gets an empty page (D-35). A row appears when the caller
    holds ``contract:read-metadata`` there, or ``contract:read-published`` and the contract has a
    Latest. Every row is the contract's head: the Latest once one exists (for every caller), else
    the newest draft. Its content-derived fields are filled only where the caller holds
    ``contract:read`` (D-36) or ``contract:read-published``, and content filters match only those
    rows, so a published-only caller can never match a draft's values.

    Args:
        folder_id (None | Unset | UUID):
        include_subtree (bool | Unset):  Default: False.
        odcs_id (None | str | Unset):
        name (None | str | Unset):
        domain (None | str | Unset):
        status (None | str | Unset):
        tags (list[str] | None | Unset):
        q (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContractListOut | Problem]
     """


    kwargs = _get_kwargs(
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

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    folder_id: None | Unset | UUID = UNSET,
    include_subtree: bool | Unset = False,
    odcs_id: None | str | Unset = UNSET,
    name: None | str | Unset = UNSET,
    domain: None | str | Unset = UNSET,
    status: None | str | Unset = UNSET,
    tags: list[str] | None | Unset = UNSET,
    q: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> ContractListOut | Problem | None:
    """ List Contracts

     List and search contracts (D-12): filters, ``q`` text match, keyset pagination.

    ``require("contract:list")`` admits every authenticated caller; the identity scopes below trim
    the SQL, so a caller without access gets an empty page (D-35). A row appears when the caller
    holds ``contract:read-metadata`` there, or ``contract:read-published`` and the contract has a
    Latest. Every row is the contract's head: the Latest once one exists (for every caller), else
    the newest draft. Its content-derived fields are filled only where the caller holds
    ``contract:read`` (D-36) or ``contract:read-published``, and content filters match only those
    rows, so a published-only caller can never match a draft's values.

    Args:
        folder_id (None | Unset | UUID):
        include_subtree (bool | Unset):  Default: False.
        odcs_id (None | str | Unset):
        name (None | str | Unset):
        domain (None | str | Unset):
        status (None | str | Unset):
        tags (list[str] | None | Unset):
        q (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContractListOut | Problem
     """


    return (await asyncio_detailed(
        client=client,
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

    )).parsed
