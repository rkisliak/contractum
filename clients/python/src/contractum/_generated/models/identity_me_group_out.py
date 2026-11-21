from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.identity_me_group_out_type import check_identity_me_group_out_type
from ..models.identity_me_group_out_type import IdentityMeGroupOutType
from typing import cast






T = TypeVar("T", bound="IdentityMeGroupOut")



@_attrs_define
class IdentityMeGroupOut:
    """ 
        Attributes:
            display_name (str):
            id (str):
            type_ (IdentityMeGroupOutType):
     """

    display_name: str
    id: str
    type_: IdentityMeGroupOutType
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        display_name = self.display_name

        id = self.id

        type_: str = self.type_


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "display_name": display_name,
            "id": id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        display_name = d.pop("display_name")

        id = d.pop("id")

        type_ = check_identity_me_group_out_type(d.pop("type"))




        identity_me_group_out = cls(
            display_name=display_name,
            id=id,
            type_=type_,
        )


        identity_me_group_out.additional_properties = d
        return identity_me_group_out

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
