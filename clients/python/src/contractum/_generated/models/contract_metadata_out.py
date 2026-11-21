from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from uuid import UUID
import datetime

if TYPE_CHECKING:
  from ..models.contract_latest_version_out import ContractLatestVersionOut
  from ..models.contract_template_origin_out import ContractTemplateOriginOut
  from ..models.contract_version_origin_out import ContractVersionOriginOut
  from ..models.contract_version_summary_out import ContractVersionSummaryOut





T = TypeVar("T", bound="ContractMetadataOut")



@_attrs_define
class ContractMetadataOut:
    """ 
        Attributes:
            api_version (None | str):
            content_redacted (bool):
            content_sha256 (None | str):
            created_at (datetime.datetime):
            created_by (str):
            domain (None | str):
            folder_id (UUID):
            id (UUID):
            latest_version (ContractLatestVersionOut | None):
            media_type (None | str):
            name (None | str):
            notation (str):
            odcs_id (str):
            organization_id (None | UUID):
            origin (ContractVersionOriginOut | None):
            size_bytes (int | None):
            status (None | str):
            tags (list[str] | None):
            template (ContractTemplateOriginOut | None):
            updated_at (datetime.datetime):
            updated_by (str):
            version (ContractVersionSummaryOut):
     """

    api_version: None | str
    content_redacted: bool
    content_sha256: None | str
    created_at: datetime.datetime
    created_by: str
    domain: None | str
    folder_id: UUID
    id: UUID
    latest_version: ContractLatestVersionOut | None
    media_type: None | str
    name: None | str
    notation: str
    odcs_id: str
    organization_id: None | UUID
    origin: ContractVersionOriginOut | None
    size_bytes: int | None
    status: None | str
    tags: list[str] | None
    template: ContractTemplateOriginOut | None
    updated_at: datetime.datetime
    updated_by: str
    version: ContractVersionSummaryOut
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.contract_latest_version_out import ContractLatestVersionOut # noqa: PLC0415
        from ..models.contract_template_origin_out import ContractTemplateOriginOut # noqa: PLC0415
        from ..models.contract_version_origin_out import ContractVersionOriginOut # noqa: PLC0415
        from ..models.contract_version_summary_out import ContractVersionSummaryOut # noqa: PLC0415
        api_version: None | str
        api_version = self.api_version

        content_redacted = self.content_redacted

        content_sha256: None | str
        content_sha256 = self.content_sha256

        created_at = self.created_at.isoformat()

        created_by = self.created_by

        domain: None | str
        domain = self.domain

        folder_id = str(self.folder_id)

        id = str(self.id)

        latest_version: dict[str, Any] | None
        if isinstance(self.latest_version, ContractLatestVersionOut):
            latest_version = self.latest_version.to_dict()
        else:
            latest_version = self.latest_version

        media_type: None | str
        media_type = self.media_type

        name: None | str
        name = self.name

        notation = self.notation

        odcs_id = self.odcs_id

        organization_id: None | str
        if isinstance(self.organization_id, UUID):
            organization_id = str(self.organization_id)
        else:
            organization_id = self.organization_id

        origin: dict[str, Any] | None
        if isinstance(self.origin, ContractVersionOriginOut):
            origin = self.origin.to_dict()
        else:
            origin = self.origin

        size_bytes: int | None
        size_bytes = self.size_bytes

        status: None | str
        status = self.status

        tags: list[str] | None
        if isinstance(self.tags, list):
            tags = self.tags


        else:
            tags = self.tags

        template: dict[str, Any] | None
        if isinstance(self.template, ContractTemplateOriginOut):
            template = self.template.to_dict()
        else:
            template = self.template

        updated_at = self.updated_at.isoformat()

        updated_by = self.updated_by

        version = self.version.to_dict()


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "api_version": api_version,
            "content_redacted": content_redacted,
            "content_sha256": content_sha256,
            "created_at": created_at,
            "created_by": created_by,
            "domain": domain,
            "folder_id": folder_id,
            "id": id,
            "latest_version": latest_version,
            "media_type": media_type,
            "name": name,
            "notation": notation,
            "odcs_id": odcs_id,
            "organization_id": organization_id,
            "origin": origin,
            "size_bytes": size_bytes,
            "status": status,
            "tags": tags,
            "template": template,
            "updated_at": updated_at,
            "updated_by": updated_by,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.contract_latest_version_out import ContractLatestVersionOut # noqa: PLC0415
        from ..models.contract_template_origin_out import ContractTemplateOriginOut # noqa: PLC0415
        from ..models.contract_version_origin_out import ContractVersionOriginOut # noqa: PLC0415
        from ..models.contract_version_summary_out import ContractVersionSummaryOut # noqa: PLC0415
        d = dict(src_dict)
        def _parse_api_version(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        api_version = _parse_api_version(d.pop("api_version"))


        content_redacted = d.pop("content_redacted")

        def _parse_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256"))


        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))




        created_by = d.pop("created_by")

        def _parse_domain(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        domain = _parse_domain(d.pop("domain"))


        folder_id = UUID(d.pop("folder_id"))




        id = UUID(d.pop("id"))




        def _parse_latest_version(data: object) -> ContractLatestVersionOut | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                latest_version_type_0 = ContractLatestVersionOut.from_dict(data)



                return latest_version_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ContractLatestVersionOut | None, data)

        latest_version = _parse_latest_version(d.pop("latest_version"))


        def _parse_media_type(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        media_type = _parse_media_type(d.pop("media_type"))


        def _parse_name(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        name = _parse_name(d.pop("name"))


        notation = d.pop("notation")

        odcs_id = d.pop("odcs_id")

        def _parse_organization_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                organization_id_type_0 = UUID(data)



                return organization_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        organization_id = _parse_organization_id(d.pop("organization_id"))


        def _parse_origin(data: object) -> ContractVersionOriginOut | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                origin_type_0 = ContractVersionOriginOut.from_dict(data)



                return origin_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ContractVersionOriginOut | None, data)

        origin = _parse_origin(d.pop("origin"))


        def _parse_size_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        size_bytes = _parse_size_bytes(d.pop("size_bytes"))


        def _parse_status(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        status = _parse_status(d.pop("status"))


        def _parse_tags(data: object) -> list[str] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                tags_type_0 = cast(list[str], data)

                return tags_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None, data)

        tags = _parse_tags(d.pop("tags"))


        def _parse_template(data: object) -> ContractTemplateOriginOut | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                template_type_0 = ContractTemplateOriginOut.from_dict(data)



                return template_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ContractTemplateOriginOut | None, data)

        template = _parse_template(d.pop("template"))


        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        updated_by = d.pop("updated_by")

        version = ContractVersionSummaryOut.from_dict(d.pop("version"))




        contract_metadata_out = cls(
            api_version=api_version,
            content_redacted=content_redacted,
            content_sha256=content_sha256,
            created_at=created_at,
            created_by=created_by,
            domain=domain,
            folder_id=folder_id,
            id=id,
            latest_version=latest_version,
            media_type=media_type,
            name=name,
            notation=notation,
            odcs_id=odcs_id,
            organization_id=organization_id,
            origin=origin,
            size_bytes=size_bytes,
            status=status,
            tags=tags,
            template=template,
            updated_at=updated_at,
            updated_by=updated_by,
            version=version,
        )


        contract_metadata_out.additional_properties = d
        return contract_metadata_out

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
