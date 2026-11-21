"""Regular expressions from a contract run only on RE2.

A contract author controls the pattern, so it must never reach a backtracking engine (ReDoS in
someone's CI). ``pyarrow.compute.match_substring_regex`` is backed by RE2, which runs in linear
time and rejects lookaround and back-references. A pattern RE2 rejects is skipped, never
evaluated with the Python ``re`` module.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

UNSUPPORTED_PATTERN = "pattern uses regex features unsupported by the linear-time engine"


def re2_supported(pattern: str) -> bool:
    """Whether RE2 accepts the pattern.

    The pattern is compiled by matching it against one empty string; an empty array would not
    compile it.
    """
    try:
        pc.match_substring_regex(pa.array([""], pa.large_string()), pattern)
    except (pa.ArrowInvalid, ValueError):
        return False
    return True
