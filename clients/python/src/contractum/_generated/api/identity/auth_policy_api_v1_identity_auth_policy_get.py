from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.identity_auth_policy_out import IdentityAuthPolicyOut
from ...models.problem import Problem
from typing import cast



def _get_kwargs(
    
) -> dict[str, Any]:
    

    

    

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/identity/auth/policy",
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> IdentityAuthPolicyOut | Problem | None:
    if response.status_code == 200:
        response_200 = IdentityAuthPolicyOut.from_dict(response.json())



        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())



        return response_400

    if response.status_code == 429:
        response_429 = Problem.from_dict(response.json())



        return response_429

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[IdentityAuthPolicyOut | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,

) -> Response[IdentityAuthPolicyOut | Problem]:
    """ Sign-in methods and password rules (public)

     The sign-in methods currently on and the rules a new password must meet.

    Public on purpose: the sign-in, rotate-password and change-password screens read it, one of
    them before anyone is signed in. It carries no secret, no tenant and no account detail.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IdentityAuthPolicyOut | Problem]
     """


    kwargs = _get_kwargs(
        
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient | Client,

) -> IdentityAuthPolicyOut | Problem | None:
    """ Sign-in methods and password rules (public)

     The sign-in methods currently on and the rules a new password must meet.

    Public on purpose: the sign-in, rotate-password and change-password screens read it, one of
    them before anyone is signed in. It carries no secret, no tenant and no account detail.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IdentityAuthPolicyOut | Problem
     """


    return sync_detailed(
        client=client,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,

) -> Response[IdentityAuthPolicyOut | Problem]:
    """ Sign-in methods and password rules (public)

     The sign-in methods currently on and the rules a new password must meet.

    Public on purpose: the sign-in, rotate-password and change-password screens read it, one of
    them before anyone is signed in. It carries no secret, no tenant and no account detail.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IdentityAuthPolicyOut | Problem]
     """


    kwargs = _get_kwargs(
        
    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient | Client,

) -> IdentityAuthPolicyOut | Problem | None:
    """ Sign-in methods and password rules (public)

     The sign-in methods currently on and the rules a new password must meet.

    Public on purpose: the sign-in, rotate-password and change-password screens read it, one of
    them before anyone is signed in. It carries no secret, no tenant and no account detail.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IdentityAuthPolicyOut | Problem
     """


    return (await asyncio_detailed(
        client=client,

    )).parsed
