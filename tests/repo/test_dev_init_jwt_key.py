"""dev-init plans the local-IdP signing key: RSA-3072, one-time rotation, JWKS overlap file.

``plan_jwt_keys`` is a pure function (no file system), so the rotation rules are tested directly:
an older, smaller key is replaced once and its public key is kept next to the new one, so tokens
the old key signed keep verifying until they expire (quick 261120-s0p, audit follow-up 7).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "dev-init" / "generate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_init_generate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_init_generate"] = module
    spec.loader.exec_module(module)
    return module


gen = _load()
OVERLAP = gen.PREVIOUS_KEY_OVERLAP_S


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


def _bits(pem: bytes) -> int:
    loaded = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    return loaded.key_size


@pytest.fixture(scope="module")
def key2048() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def key3072() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def test_the_constants() -> None:
    assert gen.JWT_KEY_BITS == 3072
    # far above the 15-minute access-token lifetime
    assert OVERLAP == 86_400


def test_a_new_key_is_3072_bits_and_previous_equals_its_public_key() -> None:
    plan = gen.plan_jwt_keys(None, None, 0.0, now_overlap_s=OVERLAP)
    assert _bits(plan.current_pem) == 3072
    loaded = serialization.load_pem_private_key(plan.current_pem, password=None)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    assert plan.previous_public_pem == _public_pem(loaded)
    assert plan.rotated is False


def test_a_2048_key_is_rotated_and_the_old_public_key_is_kept(key2048: rsa.RSAPrivateKey) -> None:
    plan = gen.plan_jwt_keys(_private_pem(key2048), None, 0.0, now_overlap_s=OVERLAP)
    assert _bits(plan.current_pem) == 3072
    assert plan.previous_public_pem == _public_pem(key2048)
    assert plan.rotated is True


def test_a_2048_key_with_a_stale_previous_file_is_still_rotated(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    plan = gen.plan_jwt_keys(
        _private_pem(key2048), _public_pem(key3072), OVERLAP * 10, now_overlap_s=OVERLAP
    )
    assert plan.previous_public_pem == _public_pem(key2048)
    assert plan.rotated is True


def test_a_3072_key_is_kept_and_an_equal_previous_file_is_not_touched(
    key3072: rsa.RSAPrivateKey,
) -> None:
    current = _private_pem(key3072)
    plan = gen.plan_jwt_keys(current, _public_pem(key3072), OVERLAP * 10, now_overlap_s=OVERLAP)
    assert plan.current_pem == current
    assert plan.previous_public_pem == _public_pem(key3072)
    assert plan.rotated is False


def test_a_missing_previous_file_is_created_from_the_current_key(
    key3072: rsa.RSAPrivateKey,
) -> None:
    plan = gen.plan_jwt_keys(_private_pem(key3072), None, 0.0, now_overlap_s=OVERLAP)
    assert plan.previous_public_pem == _public_pem(key3072)
    assert plan.rotated is False


def test_an_old_previous_file_is_reset_to_the_current_public_key(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    plan = gen.plan_jwt_keys(
        _private_pem(key3072), _public_pem(key2048), OVERLAP + 1, now_overlap_s=OVERLAP
    )
    assert plan.current_pem == _private_pem(key3072)
    assert plan.previous_public_pem == _public_pem(key3072)
    assert plan.rotated is False


def test_a_young_previous_file_is_kept(
    key2048: rsa.RSAPrivateKey, key3072: rsa.RSAPrivateKey
) -> None:
    plan = gen.plan_jwt_keys(
        _private_pem(key3072), _public_pem(key2048), OVERLAP - 1, now_overlap_s=OVERLAP
    )
    assert plan.previous_public_pem == _public_pem(key2048)
    assert plan.rotated is False


def test_a_non_rsa_key_is_refused() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_pem = _private_pem(ec.generate_private_key(ec.SECP256R1()))  # type: ignore[arg-type]
    with pytest.raises(SystemExit):
        gen.plan_jwt_keys(ec_pem, None, 0.0, now_overlap_s=OVERLAP)
