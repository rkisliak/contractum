"""The seam between the planner and the thing that reads data.

``Backend`` is what later Spark and SQL backends implement: they compile each ``CheckSpec`` to an
aggregate query (counts plus a ``LIMIT K`` sample) instead of scanning a table in memory, and
return the same ``CheckOutcome`` values, so nothing above the backend changes (D-32).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from contractum.engine.checks import CheckOutcome, CheckSpec


@dataclass(frozen=True)
class ColumnInfo:
    """A dataset column and its type as the backend names it (an Arrow type for ArrowBackend)."""

    name: str
    dtype: str


@dataclass(frozen=True)
class DatasetSchema:
    """The columns of a dataset, in order."""

    columns: tuple[ColumnInfo, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class RunOptions:
    """How a backend evaluates: failure samples kept per check and column-name matching."""

    max_failures: int = 5
    samples: bool = True
    ignore_case: bool = False


@dataclass(frozen=True)
class ParseFailure:
    """Values of a text-coerced column that did not parse as the contract's type.

    ``count`` is the number of unparseable values; ``rows`` (0-based positions) and ``values``
    (the raw text) hold a bounded sample of them, aligned by index.
    """

    count: int
    rows: tuple[int, ...] = ()
    values: tuple[Any, ...] = ()


@runtime_checkable
class Backend(Protocol):
    """Evaluates planned checks against one dataset."""

    name: str

    def schema(self) -> DatasetSchema:
        """The dataset's columns and types."""
        ...

    def evaluate(self, checks: Sequence[CheckSpec], opts: RunOptions) -> list[CheckOutcome]:
        """One outcome per check, in the order given."""
        ...
