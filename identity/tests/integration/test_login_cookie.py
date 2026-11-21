"""Opt-in HttpOnly cookie delivery of the refresh token (UI-01, D-UI-08-12, D-UI-08-13)."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/identity/auth/login"
REFRESH = "/api/v1/identity/auth/refresh"
LOGOUT = "/api/v1/identity/auth/logout"
ADMIN_PASSWORD = "an admin password of 30 chars!"  # noqa: S105
COOKIE = "__Secure-dc_refresh"
DELIVERY = {"X-Token-Delivery": "cookie"}


def _credentials() -> dict[str, str]:
    return {"username": "admin", "password": ADMIN_PASSWORD}


def _cookie_header(raw: str) -> dict[str, str]:
    # The test client's base URL is http, so a Secure cookie is sent by hand.
    return {"Cookie": f"{COOKIE}={raw}"}


def _set_cookie(resp: Any) -> str:
    values = resp.headers.get_list("set-cookie")
    assert len(values) == 1, values
    value: str = values[0]
    return value


def _cookie_value(set_cookie: str) -> str:
    first = set_cookie.split(";", 1)[0]
    name, _, value = first.partition("=")
    assert name == COOKIE
    return value


def _attributes(set_cookie: str) -> dict[str, str]:
    parts = [p.strip() for p in set_cookie.split(";")[1:]]
    return {k.lower(): v for k, _, v in (p.partition("=") for p in parts)}


async def _cookie_login(client: Any) -> str:
    resp = await client.post(LOGIN, json=_credentials(), headers=DELIVERY)
    assert resp.status_code == 200, resp.text
    return _cookie_value(_set_cookie(resp))


async def test_login_with_the_header_sets_an_httponly_path_scoped_cookie_and_omits_the_body_token(
    session_env: Any,
) -> None:
    env, client = session_env
    resp = await client.post(LOGIN, json=_credentials(), headers=DELIVERY)
    assert resp.status_code == 200, resp.text
    assert "refresh_token" not in resp.json()
    assert resp.json()["scope"] == "api"
    set_cookie = _set_cookie(resp)
    assert len(_cookie_value(set_cookie)) >= 43
    attrs = _attributes(set_cookie)
    assert "httponly" in attrs
    assert "secure" in attrs
    assert attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/api/v1/identity/auth"
    assert attrs["max-age"] == str(env.settings.refresh_token_ttl_s)


async def test_refresh_with_the_cookie_and_no_body_rotates_it(session_env: Any) -> None:
    _, client = session_env
    first = await _cookie_login(client)
    resp = await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(first)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "refresh_token" not in body
    assert body["scope"] == "api"
    second = _cookie_value(_set_cookie(resp))
    assert second != first
    assert _attributes(_set_cookie(resp))["max-age"].isdigit()
    # The cookie value is the next token of the family: it refreshes once more.
    again = await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(second)})
    assert again.status_code == 200, again.text


async def test_clients_without_the_header_keep_the_body_form_and_get_no_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    login = await client.post(LOGIN, json=_credentials())
    assert login.status_code == 200
    assert login.headers.get_list("set-cookie") == []
    first = login.json()["refresh_token"]
    resp = await client.post(REFRESH, json={"refresh_token": first})
    assert resp.status_code == 200
    assert resp.headers.get_list("set-cookie") == []
    assert resp.json()["refresh_token"] != first


def _assert_cleared(resp: Any) -> None:
    set_cookie = _set_cookie(resp)
    assert _cookie_value(set_cookie) in {"", '""'}  # Starlette quotes an empty value
    attrs = _attributes(set_cookie)
    assert attrs["max-age"] == "0"
    assert attrs["path"] == "/api/v1/identity/auth"
    assert "httponly" in attrs
    assert "secure" in attrs
    assert attrs["samesite"].lower() == "strict"


def _assert_csrf_refused(resp: Any) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["type"] == "urn:dc:problem:csrf-check-failed"
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers.get_list("set-cookie") == []


async def test_a_body_token_and_a_cookie_together_are_a_validation_error(
    session_env: Any,
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    resp = await client.post(
        REFRESH,
        json={"refresh_token": raw},
        headers={**DELIVERY, **_cookie_header(raw)},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "urn:dc:problem:validation-error"
    assert body["errors"][0]["pointer"] == "/refresh_token"
    assert raw not in resp.text
    # Nothing was rotated: the token still works.
    assert (
        await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(raw)})
    ).status_code == 200


async def test_refresh_from_the_cookie_without_the_header_is_a_csrf_refusal(
    session_env: Any,
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    resp = await client.post(REFRESH, headers=_cookie_header(raw))
    _assert_csrf_refused(resp)
    # The refusal does not consume the token.
    assert (
        await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(raw)})
    ).status_code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site", "bogus"])
async def test_refresh_from_the_cookie_refuses_a_cross_site_fetch_metadata_value(
    session_env: Any, site: str
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    resp = await client.post(
        REFRESH,
        headers={**DELIVERY, **_cookie_header(raw), "Sec-Fetch-Site": site},
    )
    _assert_csrf_refused(resp)


@pytest.mark.parametrize("site", ["same-origin", "none", None])
async def test_refresh_from_the_cookie_accepts_same_origin_none_or_absent_fetch_metadata(
    session_env: Any, site: str | None
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    headers = {**DELIVERY, **_cookie_header(raw)}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    resp = await client.post(REFRESH, headers=headers)
    assert resp.status_code == 200, resp.text


async def test_login_with_the_header_refuses_a_cross_site_request(session_env: Any) -> None:
    _, client = session_env
    resp = await client.post(
        LOGIN, json=_credentials(), headers={**DELIVERY, "Sec-Fetch-Site": "cross-site"}
    )
    _assert_csrf_refused(resp)
    ok = await client.post(
        LOGIN, json=_credentials(), headers={**DELIVERY, "Sec-Fetch-Site": "same-origin"}
    )
    assert ok.status_code == 200
    assert "refresh_token" not in ok.json()


async def test_refresh_with_no_token_anywhere_is_401_and_clears_the_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    resp = await client.post(REFRESH, headers=DELIVERY)
    assert resp.status_code == 401
    assert resp.json()["type"] == "urn:dc:problem:invalid-credentials"
    _assert_cleared(resp)


async def test_refresh_with_an_unknown_cookie_is_401_and_clears_the_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    resp = await client.post(
        REFRESH, headers={**DELIVERY, **_cookie_header("not-a-real-token-at-all")}
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "urn:dc:problem:invalid-credentials"
    _assert_cleared(resp)


async def test_logout_with_the_cookie_revokes_the_family_and_clears_the_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    out = await client.post(LOGOUT, headers={**DELIVERY, **_cookie_header(raw)})
    assert out.status_code == 204
    assert out.content == b""
    _assert_cleared(out)
    assert (
        await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(raw)})
    ).status_code == 401


async def test_logout_with_an_unknown_cookie_is_204_and_clears_the_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    out = await client.post(
        LOGOUT, headers={**DELIVERY, **_cookie_header("not-a-real-token-at-all")}
    )
    assert out.status_code == 204
    _assert_cleared(out)
    nothing = await client.post(LOGOUT, headers=DELIVERY)
    assert nothing.status_code == 204
    _assert_cleared(nothing)


async def test_logout_from_the_cookie_without_the_header_is_a_csrf_refusal(
    session_env: Any,
) -> None:
    _, client = session_env
    raw = await _cookie_login(client)
    _assert_csrf_refused(await client.post(LOGOUT, headers=_cookie_header(raw)))
    _assert_csrf_refused(
        await client.post(
            LOGOUT,
            headers={**DELIVERY, **_cookie_header(raw), "Sec-Fetch-Site": "cross-site"},
        )
    )
    # Neither refusal ended the session.
    assert (
        await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(raw)})
    ).status_code == 200


async def test_the_restricted_bootstrap_login_gets_no_cookie_even_with_the_header(
    make_identity: Any, asgi_client: Any, sql: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.post(
            LOGIN, json={"username": "admin", "password": env.secret}, headers=DELIVERY
        )
    assert resp.status_code == 200
    assert resp.json()["scope"] == "password:rotate"
    assert "refresh_token" not in resp.json()
    assert resp.headers.get_list("set-cookie") == []
    assert sql(env.db_url, "SELECT 1 FROM refresh_tokens") == []


async def test_presenting_a_rotated_cookie_revokes_the_whole_family(session_env: Any) -> None:
    _, client = session_env
    first = await _cookie_login(client)
    rotated = await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(first)})
    second = _cookie_value(_set_cookie(rotated))

    reused = await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(first)})
    assert reused.status_code == 401
    assert reused.json()["type"] == "urn:dc:problem:invalid-credentials"
    _assert_cleared(reused)

    newest = await client.post(REFRESH, headers={**DELIVERY, **_cookie_header(second)})
    assert newest.status_code == 401


async def test_body_only_refresh_and_logout_without_the_header_never_set_a_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    login = await client.post(LOGIN, json=_credentials())
    first = login.json()["refresh_token"]
    unknown = await client.post(REFRESH, json={"refresh_token": "nope"})
    assert unknown.status_code == 401
    assert unknown.headers.get_list("set-cookie") == []
    out = await client.post(LOGOUT, json={"refresh_token": first})
    assert out.status_code == 204
    assert out.headers.get_list("set-cookie") == []
    assert (await client.post(REFRESH, json={"refresh_token": first})).status_code == 401


async def test_a_body_token_with_the_header_rotates_and_answers_with_a_cookie(
    session_env: Any,
) -> None:
    _, client = session_env
    first = (await client.post(LOGIN, json=_credentials())).json()["refresh_token"]
    resp = await client.post(REFRESH, json={"refresh_token": first}, headers=DELIVERY)
    assert resp.status_code == 200
    assert "refresh_token" not in resp.json()
    assert _cookie_value(_set_cookie(resp)) != first


async def test_an_empty_cookie_beside_a_body_token_is_no_second_source(session_env: Any) -> None:
    _, client = session_env
    first = (await client.post(LOGIN, json=_credentials())).json()["refresh_token"]
    resp = await client.post(
        REFRESH, json={"refresh_token": first}, headers={**DELIVERY, **_cookie_header("")}
    )
    assert resp.status_code == 200, resp.text
    assert "refresh_token" not in resp.json()
    assert _cookie_value(_set_cookie(resp)) not in {"", first}
    replay = await client.post(REFRESH, json={"refresh_token": first})
    assert replay.status_code == 401  # the body token was the one rotated
