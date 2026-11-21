from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.contract_validation_report_out import ContractValidationReportOut
from ...models.problem import Problem
from typing import cast
from uuid import UUID



def _get_kwargs(
    contract_id: UUID,
    ref: str,

) -> dict[str, Any]:
    

    

    

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report".format(contract_id=quote(str(contract_id), safe=""),ref=quote(str(ref), safe=""),),
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ContractValidationReportOut | Problem | None:
    if response.status_code == 200:
        response_200 = ContractValidationReportOut.from_dict(response.json())



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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | ContractValidationReportOut | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    contract_id: UUID,
    ref: str,
    *,
    client: AuthenticatedClient,

) -> Response[Any | ContractValidationReportOut | Problem]:
    """ Get Contract Version Validation Report

     The stored report a visible version was published with, by ``latest`` or SemVer (D-155).

    Args:
        contract_id (UUID):
        ref (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ContractValidationReportOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,
ref=ref,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    contract_id: UUID,
    ref: str,
    *,
    client: AuthenticatedClient,

) -> Any | ContractValidationReportOut | Problem | None:
    """ Get Contract Version Validation Report

     The stored report a visible version was published with, by ``latest`` or SemVer (D-155).

    Args:
        contract_id (UUID):
        ref (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ContractValidationReportOut | Problem
     """


    return sync_detailed(
        contract_id=contract_id,
ref=ref,
client=client,

    ).parsed

async def asyncio_detailed(
    contract_id: UUID,
    ref: str,
    *,
    client: AuthenticatedClient,

) -> Response[Any | ContractValidationReportOut | Problem]:
    """ Get Contract Version Validation Report

     The stored report a visible version was published with, by ``latest`` or SemVer (D-155).

    Args:
        contract_id (UUID):
        ref (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ContractValidationReportOut | Problem]
     """


    kwargs = _get_kwargs(
        contract_id=contract_id,
ref=ref,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    contract_id: UUID,
    ref: str,
    *,
    client: AuthenticatedClient,

) -> Any | ContractValidationReportOut | Problem | None:
    """ Get Contract Version Validation Report

     The stored report a visible version was published with, by ``latest`` or SemVer (D-155).

    Args:
        contract_id (UUID):
        ref (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ContractValidationReportOut | Problem
     """


    return (await asyncio_detailed(
        contract_id=contract_id,
ref=ref,
client=client,

    )).parsed
