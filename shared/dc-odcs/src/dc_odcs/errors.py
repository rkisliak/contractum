"""Rejection type raised by the ingest pipeline.

``dc_odcs`` is pure library code with no web dependency, so it cannot raise an HTTP problem
itself. ``ContractRejected`` carries everything the contracts service needs to render an RFC 9457
problem (``urn:dc:problem:<slug>``), see docs/user/api-guide.md.
"""

from __future__ import annotations


class ContractRejected(Exception):  # noqa: N818 - name fixed by the plan's published interface
    """The submitted document must be refused (parse error or hard gate failure)."""

    def __init__(
        self,
        *,
        status: int,
        slug: str,
        title: str,
        detail: str,
        pointer: str | None = None,
    ) -> None:
        super().__init__(f"{slug}: {detail}")
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.pointer = pointer


def malformed(detail: str, pointer: str | None = None) -> ContractRejected:
    return ContractRejected(
        status=422,
        slug="malformed-document",
        title="Malformed document",
        detail=detail,
        pointer=pointer,
    )
