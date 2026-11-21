from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.contract_version_metadata_out_lifecycle import check_contract_version_metadata_out_lifecycle
from ..models.contract_version_metadata_out_lifecycle import ContractVersionMetadataOutLifecycle
from ..models.contract_version_metadata_out_status import check_contract_version_metadata_out_status
from ..models.contract_version_metadata_out_status import ContractVersionMetadataOutStatus
from typing import cast
from uuid import UUID
import datetime

if TYPE_CHECKING:
  from ..models.contract_version_metadata_out_pinned_rule_sets_type_0_item import ContractVersionMetadataOutPinnedRuleSetsType0Item





T = TypeVar("T", bound="ContractVersionMetadataOut")



@_attrs_define
class ContractVersionMetadataOut:
    """ One visible version of a contract, without its document (API-02, D-48).

        Attributes:
            content_sha256 (str):
            contract_id (UUID):
            deprecated (bool):
            deprecated_at (datetime.datetime | None):
            engine_version (None | str):
            etag (str):
            fetch_url (str):
            lifecycle (ContractVersionMetadataOutLifecycle):
            media_type (str):
            odcs_api_version (str):
            pinned_rule_sets (list[ContractVersionMetadataOutPinnedRuleSetsType0Item] | None):
            published_at (datetime.datetime | None):
            published_by (None | str):
            schema_sha (None | str):
            semver (str):
            size_bytes (int):
            status (ContractVersionMetadataOutStatus):
            sunset_date (datetime.date | None):
            validation_run_id (None | UUID):
            version_id (UUID):
     """

    content_sha256: str
    contract_id: UUID
    deprecated: bool
    deprecated_at: datetime.datetime | None
    engine_version: None | str
    etag: str
    fetch_url: str
    lifecycle: ContractVersionMetadataOutLifecycle
    media_type: str
    odcs_api_version: str
    pinned_rule_sets: list[ContractVersionMetadataOutPinnedRuleSetsType0Item] | None
    published_at: datetime.datetime | None
    published_by: None | str
    schema_sha: None | str
    semver: str
    size_bytes: int
    status: ContractVersionMetadataOutStatus
    sunset_date: datetime.date | None
    validation_run_id: None | UUID
    version_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.contract_version_metadata_out_pinned_rule_sets_type_0_item import ContractVersionMetadataOutPinnedRuleSetsType0Item # noqa: PLC0415
        content_sha256 = self.content_sha256

        contract_id = str(self.contract_id)

        deprecated = self.deprecated

        deprecated_at: None | str
        if isinstance(self.deprecated_at, datetime.datetime):
            deprecated_at = self.deprecated_at.isoformat()
        else:
            deprecated_at = self.deprecated_at

        engine_version: None | str
        engine_version = self.engine_version

        etag = self.etag

        fetch_url = self.fetch_url

        lifecycle: str = self.lifecycle

        media_type = self.media_type

        odcs_api_version = self.odcs_api_version

        pinned_rule_sets: list[dict[str, Any]] | None
        if isinstance(self.pinned_rule_sets, list):
            pinned_rule_sets = []
            for pinned_rule_sets_type_0_item_data in self.pinned_rule_sets:
                pinned_rule_sets_type_0_item = pinned_rule_sets_type_0_item_data.to_dict()
                pinned_rule_sets.append(pinned_rule_sets_type_0_item)


        else:
            pinned_rule_sets = self.pinned_rule_sets

        published_at: None | str
        if isinstance(self.published_at, datetime.datetime):
            published_at = self.published_at.isoformat()
        else:
            published_at = self.published_at

        published_by: None | str
        published_by = self.published_by

        schema_sha: None | str
        schema_sha = self.schema_sha

        semver = self.semver

        size_bytes = self.size_bytes

        status: str = self.status

        sunset_date: None | str
        if isinstance(self.sunset_date, datetime.date):
            sunset_date = self.sunset_date.isoformat()
        else:
            sunset_date = self.sunset_date

        validation_run_id: None | str
        if isinstance(self.validation_run_id, UUID):
            validation_run_id = str(self.validation_run_id)
        else:
            validation_run_id = self.validation_run_id

        version_id = str(self.version_id)


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "content_sha256": content_sha256,
            "contract_id": contract_id,
            "deprecated": deprecated,
            "deprecated_at": deprecated_at,
            "engine_version": engine_version,
            "etag": etag,
            "fetch_url": fetch_url,
            "lifecycle": lifecycle,
            "media_type": media_type,
            "odcs_api_version": odcs_api_version,
            "pinned_rule_sets": pinned_rule_sets,
            "published_at": published_at,
            "published_by": published_by,
            "schema_sha": schema_sha,
            "semver": semver,
            "size_bytes": size_bytes,
            "status": status,
            "sunset_date": sunset_date,
            "validation_run_id": validation_run_id,
            "version_id": version_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.contract_version_metadata_out_pinned_rule_sets_type_0_item import ContractVersionMetadataOutPinnedRuleSetsType0Item # noqa: PLC0415
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        contract_id = UUID(d.pop("contract_id"))




        deprecated = d.pop("deprecated")

        def _parse_deprecated_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                deprecated_at_type_0 = datetime.datetime.fromisoformat(data)



                return deprecated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        deprecated_at = _parse_deprecated_at(d.pop("deprecated_at"))


        def _parse_engine_version(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        engine_version = _parse_engine_version(d.pop("engine_version"))


        etag = d.pop("etag")

        fetch_url = d.pop("fetch_url")

        lifecycle = check_contract_version_metadata_out_lifecycle(d.pop("lifecycle"))




        media_type = d.pop("media_type")

        odcs_api_version = d.pop("odcs_api_version")

        def _parse_pinned_rule_sets(data: object) -> list[ContractVersionMetadataOutPinnedRuleSetsType0Item] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                pinned_rule_sets_type_0 = []
                _pinned_rule_sets_type_0 = data
                for pinned_rule_sets_type_0_item_data in (_pinned_rule_sets_type_0):
                    pinned_rule_sets_type_0_item = ContractVersionMetadataOutPinnedRuleSetsType0Item.from_dict(pinned_rule_sets_type_0_item_data)



                    pinned_rule_sets_type_0.append(pinned_rule_sets_type_0_item)

                return pinned_rule_sets_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ContractVersionMetadataOutPinnedRuleSetsType0Item] | None, data)

        pinned_rule_sets = _parse_pinned_rule_sets(d.pop("pinned_rule_sets"))


        def _parse_published_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                published_at_type_0 = datetime.datetime.fromisoformat(data)



                return published_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        published_at = _parse_published_at(d.pop("published_at"))


        def _parse_published_by(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        published_by = _parse_published_by(d.pop("published_by"))


        def _parse_schema_sha(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        schema_sha = _parse_schema_sha(d.pop("schema_sha"))


        semver = d.pop("semver")

        size_bytes = d.pop("size_bytes")

        status = check_contract_version_metadata_out_status(d.pop("status"))




        def _parse_sunset_date(data: object) -> datetime.date | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sunset_date_type_0 = datetime.date.fromisoformat(data)



                return sunset_date_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.date | None, data)

        sunset_date = _parse_sunset_date(d.pop("sunset_date"))


        def _parse_validation_run_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                validation_run_id_type_0 = UUID(data)



                return validation_run_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        validation_run_id = _parse_validation_run_id(d.pop("validation_run_id"))


        version_id = UUID(d.pop("version_id"))




        contract_version_metadata_out = cls(
            content_sha256=content_sha256,
            contract_id=contract_id,
            deprecated=deprecated,
            deprecated_at=deprecated_at,
            engine_version=engine_version,
            etag=etag,
            fetch_url=fetch_url,
            lifecycle=lifecycle,
            media_type=media_type,
            odcs_api_version=odcs_api_version,
            pinned_rule_sets=pinned_rule_sets,
            published_at=published_at,
            published_by=published_by,
            schema_sha=schema_sha,
            semver=semver,
            size_bytes=size_bytes,
            status=status,
            sunset_date=sunset_date,
            validation_run_id=validation_run_id,
            version_id=version_id,
        )


        contract_version_metadata_out.additional_properties = d
        return contract_version_metadata_out

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
