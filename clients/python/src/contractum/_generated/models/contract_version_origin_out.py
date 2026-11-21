from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from uuid import UUID






T = TypeVar("T", bound="ContractVersionOriginOut")



@_attrs_define
class ContractVersionOriginOut:
    """ The contract version or draft a contract was copied from (D-44b); the content is not linked.

    ``version_id`` is provenance only: it names a version or draft of the source contract at the
    time of the copy.

        Attributes:
            contract_id (UUID):
            version_id (UUID):
     """

    contract_id: UUID
    version_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        contract_id = str(self.contract_id)

        version_id = str(self.version_id)


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "contract_id": contract_id,
            "version_id": version_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        contract_id = UUID(d.pop("contract_id"))




        version_id = UUID(d.pop("version_id"))




        contract_version_origin_out = cls(
            contract_id=contract_id,
            version_id=version_id,
        )


        contract_version_origin_out.additional_properties = d
        return contract_version_origin_out

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
