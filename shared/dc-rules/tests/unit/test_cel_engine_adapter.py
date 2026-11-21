"""Adapter-only behaviour of `CelEngine` that the conformance suite cannot provoke.

A ``MemoryError`` out of the C++ runtime (a ``std::bad_alloc``) cannot be triggered reliably in
the pytest process, so the compiled program is replaced by a stand-in whose ``eval`` raises it;
the adapter must turn it into the fixed ``memory_exceeded`` code (D-139), never re-raise it.
"""

from __future__ import annotations

from typing import Any

import pytest
from dc_rules.cel_engine import CelEngine

pytestmark = pytest.mark.unit


class ExhaustedProgram:
    """A compiled program whose evaluation runs out of memory, recording what it was given."""

    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None

    def eval(self, *, data: dict[str, Any]) -> Any:
        self.data = data
        raise MemoryError("std::bad_alloc while evaluating 4242")


def test_a_memory_error_during_evaluation_is_memory_exceeded() -> None:
    program = ExhaustedProgram()
    bindings = {"this": {"n": 1}, "contract": {}, "ctx": {}}
    assert CelEngine().evaluate(program, bindings) == "memory_exceeded"
    assert program.data == bindings  # the bindings reached the runtime as a plain dict
