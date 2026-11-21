from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from uuid import UUID






T = TypeVar("T", bound="ContractLatestVersionOut")



@_attrs_define
class ContractLatestVersionOut:
    """ The contract's Latest published version (lifecycle metadata, not document content).

        Attributes:
            semver (str):
            version_id (UUID):
     """

    semver: str
    version_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        semver = self.semver

        version_id = str(self.version_id)


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "semver": semver,
            "version_id": version_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        semver = d.pop("semver")

        version_id = UUID(d.pop("version_id"))




        contract_latest_version_out = cls(
            semver=semver,
            version_id=version_id,
        )


        contract_latest_version_out.additional_properties = d
        return contract_latest_version_out

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
