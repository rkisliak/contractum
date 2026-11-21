"""The in-process test issuer itself (``dc_auth.testing``): its Entra issuer and its guards."""

from __future__ import annotations

import inspect

import jwt
import pytest
from dc_auth import testing as dc_auth_testing
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.unit


def test_the_entra_issuer_is_the_v2_url_of_the_default_tenant(fake_issuer: FakeIssuer) -> None:
    expected = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"
    assert fake_issuer.entra_issuer == expected
    assert fake_issuer.entra_issuer_config().issuer == expected
    token = fake_issuer.mint_entra("0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e")
    assert jwt.decode(token, options={"verify_signature": False})["iss"] == expected


@pytest.mark.parametrize("alg", ["ES256", "RS512", "PS256"])
def test_minting_with_an_algorithm_the_fake_cannot_sign_is_refused(
    fake_issuer: FakeIssuer, alg: str
) -> None:
    with pytest.raises(ValueError, match=f"unsupported alg for the fake issuer: {alg}"):
        fake_issuer.mint("u", alg=alg)


def test_the_session_fixture_reuses_one_issuer_and_one_key(fake_issuer: FakeIssuer) -> None:
    make = inspect.unwrap(dc_auth_testing.fake_issuer)
    again = make()
    assert again is fake_issuer
    assert again.kid == fake_issuer.kid
