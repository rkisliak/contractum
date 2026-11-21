from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.identity_folder_list_out import IdentityFolderListOut
from ...models.problem import Problem
from ...types import UNSET, Unset
from typing import cast
from uuid import UUID



def _get_kwargs(
    *,
    parent_id: None | Unset | UUID = UNSET,
    path: None | str | Unset = UNSET,
    include_archived: bool | Unset = False,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> dict[str, Any]:
    

    

    params: dict[str, Any] = {}

    json_parent_id: None | str | Unset
    if isinstance(parent_id, Unset):
        json_parent_id = UNSET
    elif isinstance(parent_id, UUID):
        json_parent_id = str(parent_id)
    else:
        json_parent_id = parent_id
    params["parent_id"] = json_parent_id

    json_path: None | str | Unset
    if isinstance(path, Unset):
        json_path = UNSET
    else:
        json_path = path
    params["path"] = json_path

    params["include_archived"] = include_archived

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
        "url": "/api/v1/identity/folders",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> IdentityFolderListOut | Problem | None:
    if response.status_code == 200:
        response_200 = IdentityFolderListOut.from_dict(response.json())



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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[IdentityFolderListOut | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    parent_id: None | Unset | UUID = UNSET,
    path: None | str | Unset = UNSET,
    include_archived: bool | Unset = False,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> Response[IdentityFolderListOut | Problem]:
    """ List Folders

    Args:
        parent_id (None | Unset | UUID):
        path (None | str | Unset):
        include_archived (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IdentityFolderListOut | Problem]
     """


    kwargs = _get_kwargs(
        parent_id=parent_id,
path=path,
include_archived=include_archived,
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
    parent_id: None | Unset | UUID = UNSET,
    path: None | str | Unset = UNSET,
    include_archived: bool | Unset = False,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> IdentityFolderListOut | Problem | None:
    """ List Folders

    Args:
        parent_id (None | Unset | UUID):
        path (None | str | Unset):
        include_archived (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IdentityFolderListOut | Problem
     """


    return sync_detailed(
        client=client,
parent_id=parent_id,
path=path,
include_archived=include_archived,
limit=limit,
cursor=cursor,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    parent_id: None | Unset | UUID = UNSET,
    path: None | str | Unset = UNSET,
    include_archived: bool | Unset = False,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> Response[IdentityFolderListOut | Problem]:
    """ List Folders

    Args:
        parent_id (None | Unset | UUID):
        path (None | str | Unset):
        include_archived (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IdentityFolderListOut | Problem]
     """


    kwargs = _get_kwargs(
        parent_id=parent_id,
path=path,
include_archived=include_archived,
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
    parent_id: None | Unset | UUID = UNSET,
    path: None | str | Unset = UNSET,
    include_archived: bool | Unset = False,
    limit: int | Unset = 50,
    cursor: None | str | Unset = UNSET,

) -> IdentityFolderListOut | Problem | None:
    """ List Folders

    Args:
        parent_id (None | Unset | UUID):
        path (None | str | Unset):
        include_archived (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IdentityFolderListOut | Problem
     """


    return (await asyncio_detailed(
        client=client,
parent_id=parent_id,
path=path,
include_archived=include_archived,
limit=limit,
cursor=cursor,

    )).parsed
