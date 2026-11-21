from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.contract_version_list_out import ContractVersionListOut
from ...models.problem import Problem
from typing import cast
from uuid import UUID



def _get_kwargs(
    contract_id: UUID,

) -> dict[str, Any]:
    

    

    

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/contracts/contracts/{contract_id}/versions".format(contract_id=quote(str(contract_id), safe=""),),
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ContractVersionListOut | Problem | None:
    if response.status_code == 200:
        response_200 = ContractVersionListOut.from_dict(response.json())



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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ContractVersionListOut | Problem]:
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

) -> Response[ContractVersionListOut | Problem]:
    """ List Contract Versions

     Every visible version of the contract, highest SemVer precedence first.

    Args:
        contract_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContractVersionListOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,

) -> ContractVersionListOut | Problem | None:
    """ List Contract Versions

     Every visible version of the contract, highest SemVer precedence first.

    Args:
        contract_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContractVersionListOut | Problem
     """


    return sync_detailed(
        contract_id=contract_id,
client=client,

    ).parsed

async def asyncio_detailed(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,

) -> Response[ContractVersionListOut | Problem]:
    """ List Contract Versions

     Every visible version of the contract, highest SemVer precedence first.

    Args:
        contract_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContractVersionListOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    contract_id: UUID,
    *,
    client: AuthenticatedClient,

) -> ContractVersionListOut | Problem | None:
    """ List Contract Versions

     Every visible version of the contract, highest SemVer precedence first.

    Args:
        contract_id (UUID):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContractVersionListOut | Problem
     """


    return (await asyncio_detailed(
        contract_id=contract_id,
client=client,

    )).parsed
