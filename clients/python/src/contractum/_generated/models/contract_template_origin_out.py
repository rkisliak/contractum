from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from uuid import UUID






T = TypeVar("T", bound="ContractTemplateOriginOut")



@_attrs_define
class ContractTemplateOriginOut:
    """ The template version a contract was copied from (CONT-02); the content is not linked.

        Attributes:
            template_id (UUID):
            version_no (int):
     """

    template_id: UUID
    version_no: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        template_id = str(self.template_id)

        version_no = self.version_no


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "template_id": template_id,
            "version_no": version_no,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        template_id = UUID(d.pop("template_id"))




        version_no = d.pop("version_no")

        contract_template_origin_out = cls(
            template_id=template_id,
            version_no=version_no,
        )


        contract_template_origin_out.additional_properties = d
        return contract_template_origin_out

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
