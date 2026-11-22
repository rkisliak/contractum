from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.contract_diff_out import ContractDiffOut
from ...models.problem import Problem
from ...types import UNSET, Unset
from typing import cast
from uuid import UUID



def _get_kwargs(
    contract_id: UUID,
    *,
    from_: str,
    to: str,
    limit: int | Unset = 500,
    cursor: None | str | Unset = UNSET,

) -> dict[str, Any]:
    

    

    params: dict[str, Any] = {}

    params["from"] = from_

    params["to"] = to

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
        "url": "/api/v1/contracts/contracts/{contract_id}/diff".format(contract_id=quote(str(contract_id), safe=""),),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ContractDiffOut | Problem | None:
    if response.status_code == 200:
        response_200 = ContractDiffOut.from_dict(response.json())



        return response_200

    if response.status_code == 304:
        response_304 = cast(Any, None)
        return response_304

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())



        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = Problem.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = Problem.from_dict(response.json())



        return response_404

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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | ContractDiffOut | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,
    from_: str,
    to: str,
    limit: int | Unset = 500,
    cursor: None | str | Unset = UNSET,

) -> Response[Any | ContractDiffOut | Problem]:
    """ Get Contract Diff

     The classified changelog from ``from`` to ``to`` (either order), paged.

    Args:
        contract_id (UUID):
        from_ (str): SemVer, `latest` or a draft id
        to (str): SemVer, `latest` or a draft id
        limit (int | Unset):  Default: 500.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ContractDiffOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,
from_=from_,
to=to,
limit=limit,
cursor=cursor,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,
    from_: str,
    to: str,
    limit: int | Unset = 500,
    cursor: None | str | Unset = UNSET,

) -> Any | ContractDiffOut | Problem | None:
    """ Get Contract Diff

     The classified changelog from ``from`` to ``to`` (either order), paged.

    Args:
        contract_id (UUID):
        from_ (str): SemVer, `latest` or a draft id
        to (str): SemVer, `latest` or a draft id
        limit (int | Unset):  Default: 500.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ContractDiffOut | Problem
     """


    return sync_detailed(
        contract_id=contract_id,
client=client,
from_=from_,
to=to,
limit=limit,
cursor=cursor,

    ).parsed

async def asyncio_detailed(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,
    from_: str,
    to: str,
    limit: int | Unset = 500,
    cursor: None | str | Unset = UNSET,

) -> Response[Any | ContractDiffOut | Problem]:
    """ Get Contract Diff

     The classified changelog from ``from`` to ``to`` (either order), paged.

    Args:
        contract_id (UUID):
        from_ (str): SemVer, `latest` or a draft id
        to (str): SemVer, `latest` or a draft id
        limit (int | Unset):  Default: 500.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ContractDiffOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,
from_=from_,
to=to,
limit=limit,
cursor=cursor,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,
    from_: str,
    to: str,
    limit: int | Unset = 500,
    cursor: None | str | Unset = UNSET,

) -> Any | ContractDiffOut | Problem | None:
    """ Get Contract Diff

     The classified changelog from ``from`` to ``to`` (either order), paged.

    Args:
        contract_id (UUID):
        from_ (str): SemVer, `latest` or a draft id
        to (str): SemVer, `latest` or a draft id
        limit (int | Unset):  Default: 500.
        cursor (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ContractDiffOut | Problem
     """


    return (await asyncio_detailed(
        contract_id=contract_id,
client=client,
from_=from_,
to=to,
limit=limit,
cursor=cursor,

    )).parsed
