"""SigningKeys accepts 2048 and 3072 bit RSA keys, refuses 1024, and serves a rotation overlap.

After a rotation the JWKS lists the new key and the previous public key under distinct RFC 7638
thumbprint kids, so a token signed by either verifies until the old ones expire (quick
261120-s0p). With previous equal to current the JWKS lists one key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from dc_identity.keys import RECOMMENDED_KEY_BITS, SigningKeys
from dc_identity.settings import IdentitySettings

pytestmark = pytest.mark.unit


def _key(bits: int) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


@pytest.fixture(scope="module")
def key2048() -> rsa.RSAPrivateKey:
    return _key(2048)


@pytest.fixture(scope="module")
def key3072() -> rsa.RSAPrivateKey:
    return _key(3072)


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _public_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _sign(key: rsa.RSAPrivateKey, kid: str) -> str:
    return jwt.encode({"sub": "u"}, _private_pem(key), algorithm="RS256", headers={"kid": kid})


def _verify(token: str, jwks: dict[str, Any]) -> None:
    kid = jwt.get_unverified_header(token)["kid"]
    jwk = next(k for k in jwks["keys"] if k["kid"] == kid)
    jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["RS256"])


def test_recommended_size_is_3072() -> None:
    assert RECOMMENDED_KEY_BITS == 3072


def test_2048_and_3072_keys_are_accepted(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    assert SigningKeys(key2048).key_size == 2048
    assert SigningKeys(key3072).key_size == 3072


def test_a_1024_bit_key_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2048"):
        SigningKeys(_key(1024))


def test_rotation_overlap_lists_two_distinct_kids(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    keys = SigningKeys(key3072, key2048.public_key())
    listed = keys.jwks()["keys"]
    assert len(listed) == 2
    assert listed[0]["kid"] == keys.kid
    assert listed[0]["kid"] != listed[1]["kid"]


def test_tokens_of_the_old_and_the_new_key_both_verify(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    keys = SigningKeys(key3072, key2048.public_key())
    old_kid = keys.jwks()["keys"][1]["kid"]
    _verify(_sign(key2048, old_kid), keys.jwks())
    _verify(_sign(key3072, keys.kid), keys.jwks())


def test_previous_equal_to_current_lists_one_key(key3072: rsa.RSAPrivateKey) -> None:
    keys = SigningKeys(key3072, key3072.public_key())
    assert len(keys.jwks()["keys"]) == 1


def test_load_reads_the_previous_public_key_file(
    tmp_path: Path, key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    current = tmp_path / "jwt_signing_key.pem"
    previous = tmp_path / "jwt_previous_public_key.pem"
    current.write_bytes(_private_pem(key3072))
    previous.write_bytes(_public_pem(key2048))
    settings = IdentitySettings(jwt_signing_key_file=current, jwt_previous_public_key_file=previous)
    keys = SigningKeys.load(settings)
    assert keys.key_size == 3072
    assert len(keys.jwks()["keys"]) == 2


def _ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def test_load_refuses_a_signing_key_that_is_not_rsa(tmp_path: Path) -> None:
    current = tmp_path / "jwt_signing_key.pem"
    current.write_bytes(
        _ec_key().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="must be an RSA private key"):
        SigningKeys.load(IdentitySettings(jwt_signing_key_file=current))


def test_load_refuses_a_previous_public_key_that_is_not_rsa(
    tmp_path: Path, key2048: rsa.RSAPrivateKey
) -> None:
    current = tmp_path / "jwt_signing_key.pem"
    previous = tmp_path / "jwt_previous_public_key.pem"
    current.write_bytes(_private_pem(key2048))
    previous.write_bytes(
        _ec_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    settings = IdentitySettings(jwt_signing_key_file=current, jwt_previous_public_key_file=previous)
    with pytest.raises(ValueError, match="previous JWT public key must be an RSA public key"):
        SigningKeys.load(settings)
