"""Contractum: fetch published data contracts and test your data against them."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Mirrors _EXPORTS for type checkers and IDEs; at runtime the names load lazily below.
    from contractum.client import Client as Client
    from contractum.comparison import compare as compare
    from contractum.contracts import ContractVersion as ContractVersion
    from contractum.errors import AccessDeniedError as AccessDeniedError
    from contractum.errors import AmbiguousReferenceError as AmbiguousReferenceError
    from contractum.errors import AuthError as AuthError
    from contractum.errors import ConfigError as ConfigError
    from contractum.errors import ContractumError as ContractumError
    from contractum.errors import DataLoadError as DataLoadError
    from contractum.errors import EngineError as EngineError
    from contractum.errors import ExtraNotInstalledError as ExtraNotInstalledError
    from contractum.errors import FrozenLockError as FrozenLockError
    from contractum.errors import IntegrityError as IntegrityError
    from contractum.errors import NetworkError as NetworkError
    from contractum.errors import NotFoundError as NotFoundError
    from contractum.errors import OfflineError as OfflineError
    from contractum.errors import RateLimitedError as RateLimitedError
    from contractum.errors import ServerError as ServerError
    from contractum.errors import UnsupportedDatasetError as UnsupportedDatasetError
    from contractum.errors import UsageError as UsageError
    from contractum.errors import WriteNotAllowed as WriteNotAllowed
    from contractum.models import ContractSummary as ContractSummary
    from contractum.models import Identity as Identity
    from contractum.models import VersionInfo as VersionInfo
    from contractum.pytest_support import assert_conforms as assert_conforms
    from contractum.report.model import Report as Report
    from contractum.testing import TestOptions as TestOptions
    from contractum.testing import run_test as run_test

__version__ = importlib.metadata.version("contractum")

_ERRORS = (
    "AccessDeniedError",
    "AmbiguousReferenceError",
    "AuthError",
    "ConfigError",
    "ContractumError",
    "DataLoadError",
    "EngineError",
    "ExtraNotInstalledError",
    "FrozenLockError",
    "IntegrityError",
    "NetworkError",
    "NotFoundError",
    "OfflineError",
    "RateLimitedError",
    "ServerError",
    "UnsupportedDatasetError",
    "UsageError",
    "WriteNotAllowed",
)

_EXPORTS: dict[str, tuple[str, str]] = {
    "Client": ("contractum.client", "Client"),
    "ContractVersion": ("contractum.contracts", "ContractVersion"),
    "ContractSummary": ("contractum.models", "ContractSummary"),
    "VersionInfo": ("contractum.models", "VersionInfo"),
    "Identity": ("contractum.models", "Identity"),
    "Report": ("contractum.report.model", "Report"),
    "TestOptions": ("contractum.testing", "TestOptions"),
    "run_test": ("contractum.testing", "run_test"),
    "compare": ("contractum.comparison", "compare"),
    "assert_conforms": ("contractum.pytest_support", "assert_conforms"),
    **{name: ("contractum.errors", name) for name in _ERRORS},
}

__all__ = sorted(["__version__", *_EXPORTS])


def __getattr__(name: str) -> Any:
    """Import an exported name on first access and cache it in the module namespace."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'contractum' has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])
