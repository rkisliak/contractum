from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.identity_me_out_idp import check_identity_me_out_idp
from ..models.identity_me_out_idp import IdentityMeOutIdp
from ..models.identity_me_out_principal_type import check_identity_me_out_principal_type
from ..models.identity_me_out_principal_type import IdentityMeOutPrincipalType
from ..models.identity_me_out_via import check_identity_me_out_via
from ..models.identity_me_out_via import IdentityMeOutVia
from typing import cast

if TYPE_CHECKING:
  from ..models.identity_me_assignment_out import IdentityMeAssignmentOut
  from ..models.identity_me_group_out import IdentityMeGroupOut





T = TypeVar("T", bound="IdentityMeOut")



@_attrs_define
class IdentityMeOut:
    """ 
        Attributes:
            assignments (list[IdentityMeAssignmentOut]):
            display_name (str):
            global_permissions (list[str]):
            groups (list[IdentityMeGroupOut]):
            idp (IdentityMeOutIdp):
            interactive (bool):
            preferred_username (None | str):
            principal_id (str):
            principal_type (IdentityMeOutPrincipalType):
            read_only (bool):
            username (None | str):
            via (IdentityMeOutVia):
     """

    assignments: list[IdentityMeAssignmentOut]
    display_name: str
    global_permissions: list[str]
    groups: list[IdentityMeGroupOut]
    idp: IdentityMeOutIdp
    interactive: bool
    preferred_username: None | str
    principal_id: str
    principal_type: IdentityMeOutPrincipalType
    read_only: bool
    username: None | str
    via: IdentityMeOutVia
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.identity_me_assignment_out import IdentityMeAssignmentOut # noqa: PLC0415
        from ..models.identity_me_group_out import IdentityMeGroupOut # noqa: PLC0415
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        display_name = self.display_name

        global_permissions = self.global_permissions



        groups = []
        for groups_item_data in self.groups:
            groups_item = groups_item_data.to_dict()
            groups.append(groups_item)



        idp: str = self.idp

        interactive = self.interactive

        preferred_username: None | str
        preferred_username = self.preferred_username

        principal_id = self.principal_id

        principal_type: str = self.principal_type

        read_only = self.read_only

        username: None | str
        username = self.username

        via: str = self.via


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "assignments": assignments,
            "display_name": display_name,
            "global_permissions": global_permissions,
            "groups": groups,
            "idp": idp,
            "interactive": interactive,
            "preferred_username": preferred_username,
            "principal_id": principal_id,
            "principal_type": principal_type,
            "read_only": read_only,
            "username": username,
            "via": via,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.identity_me_assignment_out import IdentityMeAssignmentOut # noqa: PLC0415
        from ..models.identity_me_group_out import IdentityMeGroupOut # noqa: PLC0415
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = IdentityMeAssignmentOut.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        display_name = d.pop("display_name")

        global_permissions = cast(list[str], d.pop("global_permissions"))


        groups = []
        _groups = d.pop("groups")
        for groups_item_data in (_groups):
            groups_item = IdentityMeGroupOut.from_dict(groups_item_data)



            groups.append(groups_item)


        idp = check_identity_me_out_idp(d.pop("idp"))




        interactive = d.pop("interactive")

        def _parse_preferred_username(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        preferred_username = _parse_preferred_username(d.pop("preferred_username"))


        principal_id = d.pop("principal_id")

        principal_type = check_identity_me_out_principal_type(d.pop("principal_type"))




        read_only = d.pop("read_only")

        def _parse_username(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        username = _parse_username(d.pop("username"))


        via = check_identity_me_out_via(d.pop("via"))




        identity_me_out = cls(
            assignments=assignments,
            display_name=display_name,
            global_permissions=global_permissions,
            groups=groups,
            idp=idp,
            interactive=interactive,
            preferred_username=preferred_username,
            principal_id=principal_id,
            principal_type=principal_type,
            read_only=read_only,
            username=username,
            via=via,
        )


        identity_me_out.additional_properties = d
        return identity_me_out

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
