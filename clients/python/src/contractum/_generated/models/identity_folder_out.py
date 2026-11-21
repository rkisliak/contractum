from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from uuid import UUID
import datetime






T = TypeVar("T", bound="IdentityFolderOut")



@_attrs_define
class IdentityFolderOut:
    """ 
        Attributes:
            archived (bool):
            archived_at (datetime.datetime | None):
            block_inheritance (bool):
            created_at (datetime.datetime):
            created_by (str):
            display_name (str):
            id (UUID):
            kind (str):
            parent_id (None | UUID):
            path (str):
            slug (str):
            updated_at (datetime.datetime):
            updated_by (str):
     """

    archived: bool
    archived_at: datetime.datetime | None
    block_inheritance: bool
    created_at: datetime.datetime
    created_by: str
    display_name: str
    id: UUID
    kind: str
    parent_id: None | UUID
    path: str
    slug: str
    updated_at: datetime.datetime
    updated_by: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        archived = self.archived

        archived_at: None | str
        if isinstance(self.archived_at, datetime.datetime):
            archived_at = self.archived_at.isoformat()
        else:
            archived_at = self.archived_at

        block_inheritance = self.block_inheritance

        created_at = self.created_at.isoformat()

        created_by = self.created_by

        display_name = self.display_name

        id = str(self.id)

        kind = self.kind

        parent_id: None | str
        if isinstance(self.parent_id, UUID):
            parent_id = str(self.parent_id)
        else:
            parent_id = self.parent_id

        path = self.path

        slug = self.slug

        updated_at = self.updated_at.isoformat()

        updated_by = self.updated_by


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "archived": archived,
            "archived_at": archived_at,
            "block_inheritance": block_inheritance,
            "created_at": created_at,
            "created_by": created_by,
            "display_name": display_name,
            "id": id,
            "kind": kind,
            "parent_id": parent_id,
            "path": path,
            "slug": slug,
            "updated_at": updated_at,
            "updated_by": updated_by,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        archived = d.pop("archived")

        def _parse_archived_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                archived_at_type_0 = datetime.datetime.fromisoformat(data)



                return archived_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        archived_at = _parse_archived_at(d.pop("archived_at"))


        block_inheritance = d.pop("block_inheritance")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))




        created_by = d.pop("created_by")

        display_name = d.pop("display_name")

        id = UUID(d.pop("id"))




        kind = d.pop("kind")

        def _parse_parent_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                parent_id_type_0 = UUID(data)



                return parent_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        parent_id = _parse_parent_id(d.pop("parent_id"))


        path = d.pop("path")

        slug = d.pop("slug")

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        updated_by = d.pop("updated_by")

        identity_folder_out = cls(
            archived=archived,
            archived_at=archived_at,
            block_inheritance=block_inheritance,
            created_at=created_at,
            created_by=created_by,
            display_name=display_name,
            id=id,
            kind=kind,
            parent_id=parent_id,
            path=path,
            slug=slug,
            updated_at=updated_at,
            updated_by=updated_by,
        )


        identity_folder_out.additional_properties = d
        return identity_folder_out

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
