"""Pure lifecycle rules for contract versions: SemVer parsing, ordering and publish checks.

No I/O. A published version is addressed by the document's own ``version`` string (D-41), which
must be strict SemVer 2.0.0 (D-46). The four typed parts ``(major, minor, patch, prerelease)``
are what the database keeps unique (build metadata is ignored for precedence, so ``1.0.0+a`` and
``1.0.0+b`` are the same version). There is no override parameter anywhere (D-39).

Status is derived, never stored: ``latest`` is the pointer, ``preview`` is anything above it and
``older`` anything below it (VER-04). ``deprecated`` is an orthogonal flag handled by callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import semver
from dc_core.problems import ProblemError

# PostgreSQL ``integer`` upper bound: a bigger core number would be a DataError (HTTP 500).
MAX_SEMVER_PART = 2_147_483_647

VersionStatus = Literal["latest", "preview", "older"]
BumpKind = Literal["major", "minor", "patch", "none"]


@dataclass(frozen=True)
class SemverParts:
    """A parsed SemVer 2.0.0 version; ``prerelease`` and ``build`` are '' when absent."""

    text: str
    major: int
    minor: int
    patch: int
    prerelease: str
    build: str


class VersionRow(Protocol):
    """The stored columns ``parts_of`` reads (a ``ContractVersion`` row or a row of columns)."""

    semver: str | None
    semver_major: int | None
    semver_minor: int | None
    semver_patch: int | None
    semver_prerelease: str | None


def parse_semver(value: object) -> SemverParts | None:
    """Strict SemVer 2.0.0, or None for anything else.

    Rejects non-strings, "1.0", "v1.0.0", "01.0.0", "", surrounding whitespace and core numbers
    above ``MAX_SEMVER_PART``.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = semver.Version.parse(value)
    except ValueError:
        return None
    if max(parsed.major, parsed.minor, parsed.patch) > MAX_SEMVER_PART:
        return None
    return SemverParts(
        text=value,
        major=parsed.major,
        minor=parsed.minor,
        patch=parsed.patch,
        prerelease=parsed.prerelease or "",
        build=parsed.build or "",
    )


def _as_version(parts: SemverParts) -> semver.Version:
    return semver.Version(parts.major, parts.minor, parts.patch, parts.prerelease or None)


def compare_semver(a: SemverParts, b: SemverParts) -> int:
    """SemVer precedence: -1, 0 or 1. Build metadata is ignored."""
    return _as_version(a).compare(_as_version(b))


def parts_of(version: VersionRow) -> SemverParts:
    """The parts of a stored (non-draft) row, from its typed columns."""
    text = version.semver
    if (
        text is None
        or version.semver_major is None
        or version.semver_minor is None
        or version.semver_patch is None
    ):
        raise ValueError("a non-draft contract version row must carry its semver columns")
    return SemverParts(
        text=text,
        major=version.semver_major,
        minor=version.semver_minor,
        patch=version.semver_patch,
        prerelease=version.semver_prerelease or "",
        build=text.partition("+")[2],
    )


def _highest(existing: Sequence[SemverParts]) -> SemverParts:
    best = existing[0]
    for candidate in existing[1:]:
        if compare_semver(candidate, best) > 0:
            best = candidate
    return best


def validate_publish(declared: object, existing: Sequence[SemverParts]) -> SemverParts:
    """The declared version's parts, or the problem that refuses the publish.

    422 ``invalid-version`` unless it is a quoted SemVer 2.0.0 string; 409 ``semver-conflict``
    when a non-draft row (published or unpublished) has equal precedence, or when it is not
    strictly greater than every one of them (D-46). Extensions carry server-stored text only.
    """
    parts = parse_semver(declared)
    if parts is None:
        raise ProblemError(
            422,
            "invalid-version",
            "Unprocessable Content",
            'The document\'s version must be a quoted SemVer 2.0.0 string such as "1.2.0".',
        )
    if not existing:
        return parts
    highest = _highest(existing)
    for row in existing:
        if compare_semver(parts, row) == 0:
            raise ProblemError(
                409,
                "semver-conflict",
                "Conflict",
                "A version with the same SemVer precedence already exists for this contract.",
                existing_semver=row.text,
                highest_semver=highest.text,
            )
    if compare_semver(parts, highest) < 0:
        raise ProblemError(
            409,
            "semver-conflict",
            "Conflict",
            "The version must be strictly greater than every existing version of this contract.",
            highest_semver=highest.text,
        )
    return parts


def bump(declared: str | None, kind: BumpKind) -> str | None:
    """The next version for a derived draft (D-45): the input unchanged for ``none``, for a value
    that is not strict SemVer (the editor fixes it before publish, which validates) and when the
    bumped part would pass ``MAX_SEMVER_PART``; otherwise the bumped core with lower parts reset
    and prerelease and build dropped (``1.2.0-rc.1`` minor -> ``1.3.0``)."""
    if kind == "none":
        return declared
    parts = parse_semver(declared)
    if parts is None:
        return declared
    major, minor, patch = parts.major, parts.minor, parts.patch
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    if max(major, minor, patch) > MAX_SEMVER_PART:
        return declared
    return f"{major}.{minor}.{patch}"


def derive_status(
    version: SemverParts, latest: SemverParts | None, *, is_latest: bool
) -> VersionStatus:
    """``latest`` for the pointer, else ``preview`` above it (or with no Latest), else ``older``."""
    if is_latest:
        return "latest"
    if latest is None or compare_semver(version, latest) > 0:
        return "preview"
    return "older"


__all__ = [
    "MAX_SEMVER_PART",
    "BumpKind",
    "SemverParts",
    "VersionStatus",
    "bump",
    "compare_semver",
    "derive_status",
    "parse_semver",
    "parts_of",
    "validate_publish",
]
