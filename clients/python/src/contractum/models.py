"""Typed, frozen views of the server answers the SDK hands to callers.

Each model is built from a generated API model through its ``to_dict()``, so the generated types
never leak into the public surface. Unknown fields are ignored, which keeps a newer server
compatible with an older SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

M = TypeVar("M", bound="_Model")


class _HasToDict(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    @classmethod
    def from_generated(cls: type[M], value: _HasToDict) -> M:
        """Build the model from a generated API object."""
        return cls.model_validate(value.to_dict())


class Group(_Model):
    """A group the caller belongs to."""

    id: str
    display_name: str
    type: str


class Assignment(_Model):
    """A role the caller holds on a folder, directly or through a group."""

    folder_id: UUID
    folder_path: str
    grant_id: UUID
    role: str
    via_group_id: str | None = None


class Identity(_Model):
    """Who the caller is, as the server sees them."""

    principal_id: str
    principal_type: str
    display_name: str
    username: str | None = None
    preferred_username: str | None = None
    idp: str
    via: str
    interactive: bool
    read_only: bool
    global_permissions: list[str] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)


class SignInMethods(_Model):
    """Which sign-in methods the server offers."""

    local: bool
    microsoft: bool


class PasswordRules(_Model):
    """The password rules of local accounts."""

    min_length: int
    require_digit: bool
    require_lower: bool
    require_symbol: bool
    require_upper: bool


class AuthPolicy(_Model):
    """The public sign-in policy of the server."""

    methods: SignInMethods
    password_rules: PasswordRules


class LatestVersionRef(_Model):
    """The Latest published version of a contract."""

    semver: str
    version_id: UUID


class VersionState(_Model):
    """The numbered state of the contract record."""

    number: int
    state: str


class ContractSummary(_Model):
    """A contract as listed or fetched by id (metadata, not the document)."""

    id: UUID
    notation: str
    odcs_id: str
    name: str | None = None
    domain: str | None = None
    status: str | None = None
    api_version: str | None = None
    tags: list[str] = Field(default_factory=list)
    folder_id: UUID
    organization_id: UUID | None = None
    latest_version: LatestVersionRef | None = None
    version: VersionState | None = None
    content_sha256: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    content_redacted: bool = False
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str


class ContractPage(_Model):
    """One page of contracts and the cursor of the next page."""

    items: list[ContractSummary]
    next_cursor: str | None = None


class VersionInfo(_Model):
    """The metadata of one published version of a contract."""

    version_id: UUID
    contract_id: UUID
    semver: str
    status: str
    lifecycle: str
    content_sha256: str
    etag: str
    media_type: str
    size_bytes: int
    odcs_api_version: str
    fetch_url: str
    deprecated: bool
    deprecated_at: datetime | None = None
    sunset_date: date | None = None
    published_at: datetime | None = None
    published_by: str | None = None
    engine_version: str | None = None
    schema_sha: str | None = None
    validation_run_id: UUID | None = None
    pinned_rule_sets: list[dict[str, Any]] | None = None


class StoredValidationReport(_Model):
    """The validation report stored when a version was published."""

    contract_id: UUID
    version_id: UUID
    semver: str
    run_id: UUID
    dsl_version: str
    engine_version: str
    schema_sha: str
    content_sha256: str
    validated_at: datetime
    pins: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class DiffSide(_Model):
    """One side of a diff: the ref as named and what it resolved to."""

    ref: str
    kind: str
    semver: str | None = None
    version_id: UUID
    content_sha256: str


class ContractDiff(_Model):
    """The classified differences between two versions."""

    from_: DiffSide = Field(alias="from")
    to: DiffSide
    changes: list[dict[str, Any]] = Field(default_factory=list)
    changes_total: int
    counts: dict[str, int] = Field(default_factory=dict)
    required_bump: str
    classifier_version: str
    truncated: bool = False
    next_cursor: str | None = None


class Folder(_Model):
    """A folder of the ROOT, Organization, Project, Team hierarchy."""

    id: UUID
    kind: str
    slug: str
    display_name: str
    path: str
    parent_id: UUID | None = None
    archived: bool = False
    block_inheritance: bool = False
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str


@dataclass(frozen=True)
class FetchResult:
    """The byte-faithful answer of a document fetch.

    ``content`` is None for a 304 (the cached copy is current). ``deprecation`` and ``sunset``
    are the parsed ``Deprecation`` and ``Sunset`` headers, None when absent or malformed.
    """

    status: int
    content: bytes | None
    etag: str | None
    media_type: str | None
    deprecation: datetime | None
    sunset: datetime | None

    @property
    def not_modified(self) -> bool:
        """True when the server answered 304."""
        return self.status == 304
