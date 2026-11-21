from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from uuid import UUID






T = TypeVar("T", bound="IdentityMeAssignmentOut")



@_attrs_define
class IdentityMeAssignmentOut:
    """ 
        Attributes:
            folder_id (UUID):
            folder_path (str):
            grant_id (UUID):
            role (str):
            via_group_id (None | str):
     """

    folder_id: UUID
    folder_path: str
    grant_id: UUID
    role: str
    via_group_id: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        folder_id = str(self.folder_id)

        folder_path = self.folder_path

        grant_id = str(self.grant_id)

        role = self.role

        via_group_id: None | str
        via_group_id = self.via_group_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "folder_id": folder_id,
            "folder_path": folder_path,
            "grant_id": grant_id,
            "role": role,
            "via_group_id": via_group_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        folder_id = UUID(d.pop("folder_id"))




        folder_path = d.pop("folder_path")

        grant_id = UUID(d.pop("grant_id"))




        role = d.pop("role")

        def _parse_via_group_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        via_group_id = _parse_via_group_id(d.pop("via_group_id"))


        identity_me_assignment_out = cls(
            folder_id=folder_id,
            folder_path=folder_path,
            grant_id=grant_id,
            role=role,
            via_group_id=via_group_id,
        )


        identity_me_assignment_out.additional_properties = d
        return identity_me_assignment_out

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
