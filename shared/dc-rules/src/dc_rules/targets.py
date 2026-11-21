"""Strict RFC 9535 targeting with python-jsonpath, regex filter functions removed.

The library's ``match()`` and ``search()`` filter functions run on Python ``re`` and backtrack
catastrophically (117 s measured on a 31-character string). They are removed from the
environment, so a path using them fails to compile. Authors use CEL ``matches()`` (RE2) instead.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

from jsonpath import JSONPathEnvironment
from jsonpath.exceptions import JSONPathError

from dc_rules.ports import Match, RuleCompileError, TooManyMatches

_REMOVED_FUNCTIONS = ("match", "search")


class TargetError(Exception):
    """The target could not be evaluated against this document (for example too deeply nested)."""


def _build_environment() -> JSONPathEnvironment:
    env = JSONPathEnvironment(strict=True)
    for name in _REMOVED_FUNCTIONS:
        env.function_extensions.pop(name, None)
    return env


_ENV = _build_environment()


class JsonPathTargeter:
    """`Targeter` over one module-level strict environment."""

    def compile(self, path: str) -> Any:
        try:
            return _ENV.compile(path)
        except JSONPathError as exc:
            raise RuleCompileError("target_invalid", str(exc)[:300]) from exc

    def select(self, compiled: Any, document: Any, cap: int) -> Iterator[Match]:
        """Lazily yield matches; raise `TooManyMatches` when match number cap + 1 arrives."""
        try:
            for index, found in enumerate(itertools.islice(compiled.finditer(document), cap + 1)):
                if index >= cap:
                    raise TooManyMatches
                yield Match(found.obj, str(found.pointer()), found.path)
        except RecursionError as exc:
            raise TargetError("document is too deeply nested for the target") from exc
