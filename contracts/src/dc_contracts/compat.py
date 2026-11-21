"""The baseline a publish is compared with and the stable major floor (VAL-02, VAL-03, D-146).

The baseline is the highest-precedence *published* version of the contract (Preview, Latest and
deprecated rows count; drafts and unpublished rows never do), chosen with SemVer precedence, not by
creation time. The floor is the highest major among published versions that are not prereleases:
validation lets a breaking change through only with a major bump above it (a version in a new
major's prerelease line is judged against the floor, so ``2.0.0-rc.2`` may break ``2.0.0-rc.1``).
Both are ``None`` on a first publish.

The pure part (``select_baseline``) is unit tested; ``baseline_and_floor`` reads the rows. No lock
is taken here: ``publish_draft`` reads before its gate call and again under the contract lock.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dc_odcs import MAX_VERSION_CHARS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.lifecycle import SemverParts, compare_semver, parts_of
from dc_contracts.models import ContractVersion


@dataclass(frozen=True)
class BaselineFacts:
    """What a validation run needs to compare a document with the published history."""

    version_id: uuid.UUID
    semver: str
    media_type: str
    raw: bytes
    floor: int | None


def select_baseline(rows: Sequence[SemverParts]) -> tuple[int | None, int | None]:
    """``(index of the baseline in rows, stable major floor)``; ``(None, None)`` for no rows.

    The caller passes only published rows. Ties (same precedence, differing only in build
    metadata, which the database forbids) resolve to the first row.
    """
    best: int | None = None
    floor: int | None = None
    for index, parts in enumerate(rows):
        if best is None or compare_semver(parts, rows[best]) > 0:
            best = index
        if not parts.prerelease and (floor is None or parts.major > floor):
            floor = parts.major
    return best, floor


async def _published_parts(
    session: AsyncSession, contract_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[SemverParts]]:
    """Ids and parsed versions of the published rows, reading only the version columns."""
    rows = (
        await session.execute(
            select(
                ContractVersion.id,
                ContractVersion.semver,
                ContractVersion.semver_major,
                ContractVersion.semver_minor,
                ContractVersion.semver_patch,
                ContractVersion.semver_prerelease,
            ).where(
                ContractVersion.contract_id == contract_id,
                ContractVersion.state == "published",
            )
        )
    ).all()
    return [row.id for row in rows], [parts_of(row) for row in rows]


async def baseline_key(
    session: AsyncSession, contract_id: uuid.UUID
) -> tuple[uuid.UUID | None, int | None]:
    """``(baseline version id, stable major floor)`` without the document bytes.

    What ``publish_draft`` re-resolves under the contract lock (D-147): cheap, because it reads
    only the version columns of the published rows.
    """
    ids, parts = await _published_parts(session, contract_id)
    index, floor = select_baseline(parts)
    return (None if index is None else ids[index]), floor


async def baseline_and_floor(session: AsyncSession, contract_id: uuid.UUID) -> BaselineFacts | None:
    """The baseline of the contract's published versions with the floor, or None for none."""
    ids, parts = await _published_parts(session, contract_id)
    index, floor = select_baseline(parts)
    if index is None:
        return None
    chosen = (
        await session.execute(
            select(ContractVersion.media_type, ContractVersion.raw_document).where(
                ContractVersion.id == ids[index]
            )
        )
    ).one()
    return BaselineFacts(
        version_id=ids[index],
        semver=parts[index].text,
        media_type=chosen.media_type,
        raw=bytes(chosen.raw_document),
        floor=floor,
    )


def baseline_payload(facts: BaselineFacts | None) -> dict[str, Any] | None:
    """The ``baseline`` object of a validation run request (``None`` on a first publish).

    ``semver`` is display only: above validation's ``MAX_VERSION_CHARS`` it is sent as ``None``
    (validation derives it from the baseline document), so a long published version can never
    make every later run a 422 (CR-02).
    """
    if facts is None:
        return None
    return {
        "version_id": str(facts.version_id),
        "semver": facts.semver if len(facts.semver) <= MAX_VERSION_CHARS else None,
        "media_type": facts.media_type,
        "document_b64": base64.b64encode(facts.raw).decode("ascii"),
    }


__all__ = [
    "BaselineFacts",
    "baseline_and_floor",
    "baseline_key",
    "baseline_payload",
    "select_baseline",
]
