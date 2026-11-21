"""The closed error taxonomy of the SDK and CLI.

Every error maps to exit code 2 in the CLI: exit code 1 is reserved for a data mismatch that a
command reports itself. No error stores a token, a header or a response body.
"""

from __future__ import annotations

from collections.abc import Iterable


class ContractumError(Exception):
    """Base class of every error the SDK raises on purpose."""

    exit_code = 2

    def __init__(self, message: str = "", *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class UsageError(ContractumError):
    """The command or call was used wrongly."""


class ConfigError(ContractumError):
    """A profile, environment variable or config file is invalid."""


class AuthError(ContractumError):
    """No credentials, or the server rejected them."""


class AccessDeniedError(ContractumError):
    """The caller is authenticated but may not read the resource."""


class NotFoundError(ContractumError):
    """The contract, version or object does not exist for this caller."""


class AmbiguousReferenceError(ContractumError):
    """A contract reference matched more than one contract."""

    def __init__(
        self, message: str, candidates: Iterable[str] = (), *, hint: str | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.candidates: tuple[str, ...] = tuple(candidates)


class NetworkError(ContractumError):
    """The server could not be reached or the connection failed."""


class ServerError(ContractumError):
    """The server answered with an error; the message is built from its problem fields only."""

    def __init__(
        self,
        status: int,
        problem_type: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        message = f"{title or 'server error'} (HTTP {status}"
        message += f", {problem_type})" if problem_type else ")"
        if detail:
            message += f": {detail}"
        super().__init__(message, hint=hint)
        self.status = status
        self.problem_type = problem_type
        self.title = title
        self.detail = detail


class RateLimitedError(ContractumError):
    """The server kept answering 429 or 503 after the retries."""

    def __init__(
        self, message: str = "", *, retry_after: float | None = None, hint: str | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.retry_after = retry_after


class IntegrityError(ContractumError):
    """A fetched or cached document does not match its recorded hash."""


class FrozenLockError(ContractumError):
    """The lock file does not allow the requested resolution."""


class OfflineError(ContractumError):
    """The operation needs the network but offline mode is on."""


class WriteNotAllowed(ContractumError):
    """The SDK is read-only and refused a request that would change server state."""


class DataLoadError(ContractumError):
    """The dataset could not be read."""


class UnsupportedDatasetError(ContractumError):
    """The dataset type or format is not supported."""


class ExtraNotInstalledError(ContractumError):
    """A feature needs an optional extra that is not installed."""

    def __init__(self, extra: str, feature: str) -> None:
        super().__init__(f"{feature} needs the optional extra: pip install 'contractum[{extra}]'")
        self.extra = extra
        self.feature = feature


class EngineError(ContractumError):
    """The checking engine failed to evaluate a check."""
