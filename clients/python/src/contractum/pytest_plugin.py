"""The pytest plugin: ``contractum_contract`` and ``contractum_client`` fixtures.

It is registered through the ``pytest11`` entry point and costs nothing unless a fixture is
requested: this module imports only the standard library and pytest, and registers no hooks that
run per test. ``-p no:contractum`` switches it off.

Options: ``--contractum-frozen`` (or ``CONTRACTUM_FROZEN=1``) reads the versions pinned in the
lock, ``--contractum-lock PATH`` (ini ``contractum_lock``) names the lock file and
``--contractum-offline`` never touches the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest

from contractum.pytest_support import DEFAULT_LOCK_NAME, ContractResolver, assert_conforms

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.contracts import ContractVersion

__all__ = ["assert_conforms", "contractum_client", "contractum_contract", "pytest_addoption"]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("contractum", "Contractum data contracts")
    group.addoption(
        "--contractum-frozen",
        action="store_true",
        default=False,
        help="Read contract versions from the lock file instead of resolving latest or preview.",
    )
    group.addoption(
        "--contractum-lock",
        action="store",
        default=None,
        metavar="PATH",
        help=f"The lock file read by --contractum-frozen (default {DEFAULT_LOCK_NAME}).",
    )
    group.addoption(
        "--contractum-offline",
        action="store_true",
        default=False,
        help="Use only the lock, the pulled files and the cache; never touch the network.",
    )
    parser.addini(
        "contractum_lock",
        help=f"Contractum lock file, relative to the rootdir (default {DEFAULT_LOCK_NAME}).",
        default=DEFAULT_LOCK_NAME,
    )


@pytest.fixture(scope="session")
def _contractum_resolver(pytestconfig: pytest.Config) -> Iterator[ContractResolver]:
    resolver = ContractResolver(pytestconfig)
    yield resolver
    resolver.close()


@pytest.fixture(scope="session")
def contractum_client(_contractum_resolver: ContractResolver) -> Client:
    """The session's Contractum client, built from ``CONTRACTUM_*`` and the profile file."""
    return _contractum_resolver.client()


@pytest.fixture(scope="session")
def contractum_contract(
    _contractum_resolver: ContractResolver,
) -> Callable[[str], ContractVersion]:
    """A function that returns the ``ContractVersion`` for a reference, fetched once per session.

    Example: ``contract = contractum_contract("payments.orders")``.
    """

    def get(ref: str) -> ContractVersion:
        from contractum.errors import ContractumError

        try:
            return _contractum_resolver.get(ref)
        except ContractumError as exc:
            hint = f" ({exc.hint})" if exc.hint else ""
            pytest.fail(f"contractum: cannot get contract {ref!r}: {exc}{hint}", pytrace=False)

    return get
