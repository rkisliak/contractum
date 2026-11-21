"""Contract references: what a user types to name a contract, and how it is resolved.

The grammar is ``[<folder-path>:]<odcs-id-or-uuid>[@latest|@preview|@X.Y.Z]``. A folder path is
slug segments joined by ``/`` (``acme/payments/ledger``). The ``@`` suffix is a version only when
it is ``latest``, ``preview`` or a strict SemVer; any other suffix stays part of the identifier,
except text that looks like a mistyped version (``@1.4``, ``@v1.0.0``, a bare ``@``), which is a
usage error. The text before the first ``:`` is a folder path when it is a valid slug path; with a
``/`` or empty and not valid it is a usage error; otherwise it stays part of the identifier.
Every pattern is linear: no nested quantifiers.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from contractum.errors import AmbiguousReferenceError, NotFoundError, ServerError, UsageError

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.models import ContractSummary, VersionInfo

LATEST = "latest"
PREVIEW = "preview"
MAX_REF_CHARS = 512
MAX_IDENT_CHARS = 256
MAX_SLUG_CHARS = 63

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_VERSION_LIKE = re.compile(r"v?[0-9][0-9A-Za-z.+-]*")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class ContractRef:
    """A parsed reference: an optional folder path, an identifier and an optional version."""

    folder_path: str | None
    ident: str
    version: str | None = None

    @property
    def uuid(self) -> UUID | None:
        """The identifier as a contract id, or None when it is an ODCS id."""
        return UUID(self.ident) if _UUID.fullmatch(self.ident) else None

    def __str__(self) -> str:
        text = f"{self.folder_path}:{self.ident}" if self.folder_path else self.ident
        return f"{text}@{self.version}" if self.version else text


@dataclass(frozen=True)
class ResolvedContract:
    """The contract a reference points at."""

    contract_id: UUID
    odcs_id: str | None
    folder_id: UUID
    name: str | None = None


def is_version(text: str) -> bool:
    """True for ``latest``, ``preview`` or a strict SemVer string."""
    return text in (LATEST, PREVIEW) or _SEMVER.fullmatch(text) is not None


def parse_version(text: str) -> str:
    """Validate a version selector (``latest``, ``preview`` or ``X.Y.Z[-pre][+build]``)."""
    value = text.strip()
    if value.lower() in (LATEST, PREVIEW):
        value = value.lower()
    if len(value) > MAX_REF_CHARS or not is_version(value):
        raise UsageError(
            f"{text!r} is not a version",
            hint="use latest, preview or an exact SemVer such as 1.4.0",
        )
    return value


def is_folder_path(text: str) -> bool:
    """True for slug segments joined by ``/``, each at most 63 characters."""
    segments = text.split("/")
    return all(
        0 < len(segment) <= MAX_SLUG_CHARS and _SLUG.fullmatch(segment) for segment in segments
    )


def _split_version(text: str) -> tuple[str, str | None]:
    head, at, tail = text.rpartition("@")
    if not at:
        return text, None
    lowered = tail.lower()
    if lowered in (LATEST, PREVIEW) or _SEMVER.fullmatch(tail):
        return head, lowered if lowered in (LATEST, PREVIEW) else tail
    if not tail or _VERSION_LIKE.fullmatch(tail):
        raise UsageError(
            f"{tail!r} is not a version",
            hint="use @latest, @preview or an exact SemVer such as @1.4.0",
        )
    return text, None


def _split_folder(text: str) -> tuple[str | None, str]:
    prefix, colon, rest = text.partition(":")
    if not colon:
        return None, text
    if is_folder_path(prefix):
        return prefix, rest
    if not prefix or "/" in prefix:
        raise UsageError(
            f"{prefix!r} is not a folder path",
            hint="a folder path is lowercase slugs joined by '/', such as acme/payments/ledger",
        )
    return None, text


def parse_ref(text: str) -> ContractRef:
    """Parse a reference; anything that is not one raises UsageError (never another error)."""
    value = text.strip()
    if not value:
        raise UsageError("a contract reference is not empty")
    if len(value) > MAX_REF_CHARS:
        raise UsageError(f"a contract reference is at most {MAX_REF_CHARS} characters")
    head, version = _split_version(value)
    folder, ident = _split_folder(head)
    if not ident or len(ident) > MAX_IDENT_CHARS:
        raise UsageError("a contract reference needs an identifier (an ODCS id or a contract id)")
    if any(ch.isspace() or not ch.isprintable() for ch in ident):
        raise UsageError("a contract identifier has no spaces or control characters")
    if _UUID.fullmatch(ident):
        if folder is not None:
            raise UsageError("a contract id needs no folder path")
        ident = ident.lower()
    return ContractRef(folder, ident, version)


def _candidate(item: ContractSummary) -> str:
    return f"{item.id} ({item.name or '-'}, folder {item.folder_id})"


def _pick_one(ref: ContractRef, hits: list[ContractSummary]) -> ContractSummary:
    if not hits:
        raise NotFoundError(f"no readable contract matches {str(ref)!r}")
    if len(hits) > 1:
        listed = [_candidate(item) for item in hits]
        raise AmbiguousReferenceError(
            f"{ref.ident!r} matches {len(hits)} contracts: " + "; ".join(listed),
            listed,
            hint=f"use the contract id (a UUID) or qualify it as <folder-path>:{ref.ident}",
        )
    return hits[0]


def resolve_ref(client: Client, ref: ContractRef) -> ResolvedContract:
    """Find the one contract a reference names (several matches are an error, never a pick)."""
    identifier = ref.uuid
    if identifier is not None:
        found = client.get_contract(identifier)
        return ResolvedContract(found.id, found.odcs_id, found.folder_id, found.name)
    folder_id = None
    if ref.folder_path is not None:
        folder = client.find_folder(ref.folder_path)
        if folder is None:
            raise NotFoundError(f"no readable folder {ref.folder_path!r}")
        folder_id = folder.id
    hits = [
        item
        for item in client.iter_contracts(folder_id=folder_id, odcs_id=ref.ident)
        if item.odcs_id == ref.ident
    ]
    found = _pick_one(ref, hits)
    return ResolvedContract(found.id, found.odcs_id, found.folder_id, found.name)


def _preview(client: Client, contract_id: UUID, label: str) -> VersionInfo:
    previews = [item for item in client.list_versions(contract_id) if item.status == PREVIEW]
    if not previews:
        raise NotFoundError(f"no Preview exists for {label}", hint="pin an exact version instead")
    if len(previews) > 1:
        names = ", ".join(item.semver for item in previews)
        warnings.warn(
            f"{label} has {len(previews)} previews ({names}); using {previews[0].semver}, "
            "pin @X.Y.Z to choose",
            UserWarning,
            stacklevel=3,
        )
    return previews[0]


def resolve_version_info(
    client: Client, contract_id: UUID, version: str | None, *, label: str | None = None
) -> VersionInfo:
    """Read the metadata of the version a reference asks for; never substitutes another one.

    ``None`` and ``latest`` mean the Latest version, ``preview`` the highest-precedence Preview,
    anything else an exact SemVer.
    """
    wanted = version or LATEST
    name = label or f"contract {contract_id}"
    if wanted == PREVIEW:
        return _preview(client, contract_id, name)
    info = client.version_metadata(contract_id, wanted)
    if wanted != LATEST and info.semver.partition("+")[0] != wanted.partition("+")[0]:
        raise ServerError(200, title=f"the server answered version {info.semver} for {wanted}")
    return info


def resolve_version(
    client: Client, contract_id: UUID, version: str | None, *, label: str | None = None
) -> str:
    """Return the exact SemVer a version reference stands for."""
    return resolve_version_info(client, contract_id, version, label=label).semver
