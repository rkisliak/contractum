"""``ContractVersion``: one published contract version, byte for byte, with its metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from contractum.engine.checks import CheckOutcome
from contractum.engine.lifecycle import lifecycle_outcome
from contractum.errors import UsageError
from contractum.odcs import load_document
from contractum.report.model import ContractInfo

if TYPE_CHECKING:
    import pyarrow as pa

    from contractum.report.model import Report
    from contractum.testing import TestOptions

Source = Literal["server", "lock", "file", "cache"]
RenderFormat = Literal["original", "yaml", "json"]

_SUFFIX_MEDIA = {
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class ContractDeprecationWarning(UserWarning):
    """The contract version you fetched is deprecated or has a sunset date."""


@dataclass(frozen=True)
class LifecyclePolicy:
    """What a deprecated version means for a run: nothing, a failure, or a failure near sunset."""

    fail_on_deprecated: bool = False
    sunset_within_days: int | None = None


def deprecation_message(label: str, *, deprecated: bool, sunset_date: date | None) -> str | None:
    """The sentence that names a deprecation and its sunset date, or None for a live version."""
    if deprecated:
        when = f"sunset {sunset_date.isoformat()}" if sunset_date else "no sunset date"
        return f"{label} is deprecated, {when}"
    if sunset_date is not None:
        return f"{label} sunsets {sunset_date.isoformat()}"
    return None


@dataclass(frozen=True)
class ContractVersion:
    """A contract version as served: the exact bytes plus what the server said about them."""

    contract_id: str | None
    odcs_id: str | None
    name: str | None
    semver: str | None
    status: str | None
    media_type: str
    content: bytes
    content_sha256: str
    etag: str | None
    deprecated: bool = False
    deprecated_at: datetime | None = None
    sunset_date: date | None = None
    published_at: datetime | None = None
    source: Source = "server"

    @cached_property
    def document(self) -> dict[str, Any]:
        """The parsed document (JSON-native dict); parsed on first use."""
        return load_document(self.content, media_type=self.media_type)

    @property
    def text(self) -> str:
        """The original bytes decoded as UTF-8 (a byte-order mark is dropped)."""
        return self.content.decode("utf-8-sig", errors="replace")

    @property
    def label(self) -> str:
        """``<odcs-id>@<semver>`` (the contract id or ``contract`` when the id is unknown)."""
        return f"{self.odcs_id or self.contract_id or 'contract'}@{self.semver or '-'}"

    @property
    def deprecation_notice(self) -> str | None:
        """The deprecation sentence with the sunset date, None when the version is live."""
        return deprecation_message(
            self.label, deprecated=self.deprecated, sunset_date=self.sunset_date
        )

    @property
    def info(self) -> ContractInfo:
        """The contract block of a test report."""
        return ContractInfo(
            source="server" if self.source == "cache" else self.source,
            contract_id=self.contract_id,
            odcs_id=self.odcs_id,
            name=self.name,
            semver=self.semver,
            status=self.status,
            content_sha256=self.content_sha256,
            deprecated=self.deprecated,
            sunset_date=self.sunset_date.isoformat() if self.sunset_date else None,
        )

    def test(self, data: object, *, options: TestOptions | None = None, **overrides: Any) -> Report:
        """Test ``data`` against this version and return the report the CLI would print.

        ``options`` is the base ``TestOptions``; keyword overrides (``strict=True``, ``limit=100``
        and so on) are applied on top and validated like any other option.
        """
        from contractum.testing import TestOptions, run_test

        base = options or TestOptions()
        merged = TestOptions(**{**base.model_dump(), **overrides}) if overrides else base
        return run_test(self.document, data, options=merged, contract_info=self.info)

    def to_arrow_schema(self, object_name: str | None = None) -> pa.Schema:
        """The Arrow schema of one schema object (the only one, or the one named)."""
        from contractum.engine.arrow_schema import contract_to_arrow_schema

        return contract_to_arrow_schema(self.document, object_name)

    def render(self, fmt: RenderFormat = "original") -> str:
        """The document as text: the original bytes, or a local YAML or JSON conversion."""
        if fmt == "original":
            return self.text
        if fmt == "json":
            return json.dumps(self.document, indent=2, ensure_ascii=False) + "\n"
        if fmt == "yaml":
            dumped: str = yaml.safe_dump(
                self.document, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
            return dumped
        raise UsageError(f"unknown format {fmt!r}: use original, yaml or json")

    @classmethod
    def from_file(cls, path: str | Path) -> ContractVersion:
        """Read a local contract file; the hash is that of its bytes, the media type its suffix."""
        file = Path(path)
        try:
            content = file.read_bytes()
        except OSError as exc:
            raise UsageError(f"cannot read {file.name}: {type(exc).__name__}") from exc
        media_type = _SUFFIX_MEDIA.get(file.suffix.lower(), "application/yaml")
        document = load_document(content, media_type=media_type)
        return cls(
            contract_id=None,
            odcs_id=_text(document.get("id")),
            name=_text(document.get("name")),
            semver=_text(document.get("version")),
            status=_text(document.get("status")),
            media_type=media_type,
            content=content,
            content_sha256=hashlib.sha256(content).hexdigest(),
            etag=None,
            source="file",
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def evaluate_lifecycle(
    version: ContractVersion, policy: LifecyclePolicy, today: date | None = None
) -> CheckOutcome | None:
    """The ``contract.lifecycle`` outcome for a version under a policy (None for a live one)."""
    return lifecycle_outcome(
        deprecated=version.deprecated,
        sunset_date=version.sunset_date,
        today=today or date.today(),
        fail_on_deprecated=policy.fail_on_deprecated,
        sunset_within_days=policy.sunset_within_days,
    )
