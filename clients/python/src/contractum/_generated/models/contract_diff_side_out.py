from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.contract_diff_side_out_kind import check_contract_diff_side_out_kind
from ..models.contract_diff_side_out_kind import ContractDiffSideOutKind
from typing import cast
from uuid import UUID






T = TypeVar("T", bound="ContractDiffSideOut")



@_attrs_define
class ContractDiffSideOut:
    """ One side of a diff: the ref as named and the version or draft it resolved to.

        Attributes:
            content_sha256 (str):
            kind (ContractDiffSideOutKind):
            ref (str):
            semver (None | str):
            version_id (UUID):
     """

    content_sha256: str
    kind: ContractDiffSideOutKind
    ref: str
    semver: None | str
    version_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        kind: str = self.kind

        ref = self.ref

        semver: None | str
        semver = self.semver

        version_id = str(self.version_id)


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "content_sha256": content_sha256,
            "kind": kind,
            "ref": ref,
            "semver": semver,
            "version_id": version_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        kind = check_contract_diff_side_out_kind(d.pop("kind"))




        ref = d.pop("ref")

        def _parse_semver(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        semver = _parse_semver(d.pop("semver"))


        version_id = UUID(d.pop("version_id"))




        contract_diff_side_out = cls(
            content_sha256=content_sha256,
            kind=kind,
            ref=ref,
            semver=semver,
            version_id=version_id,
        )


        contract_diff_side_out.additional_properties = d
        return contract_diff_side_out

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
