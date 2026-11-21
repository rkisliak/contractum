"""`ExpressionEngine` adapter over cel-expr-python (the cel-1 and cel-2 surfaces, D-136, D-152).

cel-1 binds ``this``, ``contract`` and ``ctx``; cel-2 adds ``previous``, ``changes`` and ``refs``.
Each is its own environment because the engine rejects an undeclared identifier at compile time,
which keeps a cel-1 rule set evaluating exactly as it always did.

Two error channels, mapped differently:

* compile: every failure of the engine's compile step is a ``RuleCompileError("compile_error")``
  regardless of text (syntax, undeclared identifier such as ``previous``, unknown function such
  as ``now()``, parser recursion limit, codepoint limit); a concrete non-bool return type is
  ``not_boolean``. ``map_engine_text`` is never used at compile time.
* evaluation: an engine error arrives either as a returned ERROR value or as a raised
  ``RuntimeError`` (the iteration budget). Both map through ``map_engine_text``.

This module is the only one that imports ``cel_expr_python``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import metadata
from typing import Any

from cel_expr_python import cel
from cel_expr_python.ext import ext_bindings, ext_lists, ext_math, ext_optional, ext_strings

from dc_rules.functions import FUNCTIONS, DcFunction
from dc_rules.ports import ErrorCode, RuleCompileError

_DETAIL_CHARS = 300


def engine_version() -> str:
    """Versions of the library and both engines, for the pins recorded with every run."""
    parts = [
        f"dc-rules {metadata.version('dc-rules')}",
        f"cel-expr-python {metadata.version('cel-expr-python')}",
        f"python-jsonpath {metadata.version('python-jsonpath')}",
    ]
    return "; ".join(parts)


def map_engine_text(text: str) -> ErrorCode:
    """Map the engine's evaluation-time error text to an error code (engine-specific table)."""
    if "Iteration budget exceeded" in text:
        return "budget_exceeded"
    if "NOT_FOUND" in text:
        return "no_such_key"
    if "No matching overloads" in text:
        return "type_error"
    if "INVALID_ARGUMENT" in text:
        return "invalid_argument"
    return "engine_error"


CEL1_VARIABLES = ("this", "contract", "ctx")
CEL2_VARIABLES = (*CEL1_VARIABLES, "previous", "changes", "refs")


def _declare(function: DcFunction) -> Any:
    return cel.FunctionDecl(
        function.name,
        [
            cel.Overload(
                id=function.overload_id,
                return_type=cel.Type.BOOL,
                parameters=[cel.Type.STRING, cel.Type.STRING],
                impl=function.impl,
            )
        ],
    )


def _new_env(variables: tuple[str, ...], functions: Sequence[DcFunction] = ()) -> Any:
    return cel.NewEnv(
        variables={name: cel.Type.DYN for name in variables},
        functions=[_declare(f) for f in functions],
        extensions=[
            ext_strings.ExtStrings(),
            ext_optional.ExtOptional(),
            ext_lists.ExtLists(),
            ext_bindings.ExtBindings(),
            ext_math.ExtMath(),
        ],
    )


class CelEngine:
    name = "cel-expr-python"

    def __init__(self, functions: Sequence[DcFunction] = FUNCTIONS) -> None:
        """``functions`` are the `dc.*` helpers of the cel-2 environment (never of cel-1)."""
        self.version = metadata.version("cel-expr-python")
        self._envs = {
            "cel-1": _new_env(CEL1_VARIABLES),
            "cel-2": _new_env(CEL2_VARIABLES, functions),
        }

    def compile(self, expression: str, dsl_version: str = "cel-1") -> Any:
        env = self._envs.get(dsl_version)
        if env is None:
            raise RuleCompileError("compile_error", f"unknown dsl_version {dsl_version!r}")
        try:
            compiled = env.compile(expression)
        except RuntimeError as exc:
            raise RuleCompileError("compile_error", str(exc)[:_DETAIL_CHARS]) from exc
        return_type = compiled.return_type().name()
        if return_type not in ("BOOL", "DYN"):
            raise RuleCompileError(
                "not_boolean", f"the condition must be boolean, it returns {return_type}"
            )
        return compiled

    def evaluate(self, compiled: Any, bindings: Mapping[str, Any]) -> bool | ErrorCode:
        try:
            value = compiled.eval(data=dict(bindings))
        except MemoryError:
            return "memory_exceeded"
        except RuntimeError as exc:
            return map_engine_text(str(exc))
        if value.type().name() == "ERROR":
            return map_engine_text(str(value.plain_value()))
        result = value.plain_value()
        if isinstance(result, bool):
            return result
        return "not_boolean"

    def compiled_size(self, compiled: Any) -> int:
        return len(compiled.serialize())
