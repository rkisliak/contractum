"""Signing keys: the current RS256 key pair and an optional previous public key (T-01-16).

The private key is loaded only from ``jwt_signing_key_file`` and is never logged. ``kid`` is the
RFC 7638 thumbprint of the public key, so a rotated key can never collide with an old ``kid``.
The previous public key stays in the JWKS so live tokens survive a rotation.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

if TYPE_CHECKING:
    from dc_identity.settings import IdentitySettings

_MIN_KEY_BITS = 2048
# New keys are 3072 bits (infra/dev-init). 2048 stays loadable so a deployed key keeps working;
# create_identity_app logs a warning for it.
RECOMMENDED_KEY_BITS = 3072


def thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url without padding) of an RSA public key."""
    canon = json.dumps(
        {name: jwk[name] for name in ("e", "kty", "n")}, separators=(",", ":"), sort_keys=True
    ).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canon).digest()).rstrip(b"=").decode()


def _public_jwk(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update(kid=thumbprint(jwk), use="sig", alg="RS256")
    return jwk


class SigningKeys:
    def __init__(
        self,
        private_key: rsa.RSAPrivateKey,
        previous_public_key: rsa.RSAPublicKey | None = None,
    ) -> None:
        if private_key.key_size < _MIN_KEY_BITS:
            raise ValueError(f"signing key must be at least {_MIN_KEY_BITS} bits")
        self._private_key = private_key
        self._current_jwk = _public_jwk(private_key.public_key())
        self._previous_jwk = (
            _public_jwk(previous_public_key) if previous_public_key is not None else None
        )

    @classmethod
    def load(cls, settings: IdentitySettings) -> SigningKeys:
        loaded = serialization.load_pem_private_key(
            settings.jwt_signing_key_file.read_bytes(), password=None
        )
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("the JWT signing key must be an RSA private key")
        previous: rsa.RSAPublicKey | None = None
        if settings.jwt_previous_public_key_file is not None:
            prev = serialization.load_pem_public_key(
                settings.jwt_previous_public_key_file.read_bytes()
            )
            if not isinstance(prev, rsa.RSAPublicKey):
                raise ValueError("the previous JWT public key must be an RSA public key")
            previous = prev
        return cls(loaded, previous)

    @property
    def key_size(self) -> int:
        return self._private_key.key_size

    @property
    def kid(self) -> str:
        kid: str = self._current_jwk["kid"]
        return kid

    @property
    def private_pem(self) -> bytes:
        return self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def jwks(self) -> dict[str, Any]:
        keys = [copy.deepcopy(self._current_jwk)]
        if self._previous_jwk is not None and self._previous_jwk["kid"] != self.kid:
            keys.append(copy.deepcopy(self._previous_jwk))
        return {"keys": keys}
