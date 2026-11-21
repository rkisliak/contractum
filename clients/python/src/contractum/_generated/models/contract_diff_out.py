from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.contract_diff_out_required_bump import check_contract_diff_out_required_bump
from ..models.contract_diff_out_required_bump import ContractDiffOutRequiredBump
from typing import cast

if TYPE_CHECKING:
  from ..models.contract_diff_out_changes_item import ContractDiffOutChangesItem
  from ..models.contract_diff_out_counts import ContractDiffOutCounts
  from ..models.contract_diff_side_out import ContractDiffSideOut





T = TypeVar("T", bound="ContractDiffOut")



@_attrs_define
class ContractDiffOut:
    """ The classified changes between two refs, computed on the fly and never stored (D-156).

    ``changes`` items have the stored-report change shape (D-148): ``code``, ``class``,
    ``element_kind``, ``path``, ``pointer_before``, ``pointer_after``, ``attribute``, ``before``,
    ``after`` (values only for allow-listed attributes).

        Attributes:
            changes (list[ContractDiffOutChangesItem]):
            changes_total (int):
            classifier_version (str):
            counts (ContractDiffOutCounts):
            from_ (ContractDiffSideOut): One side of a diff: the ref as named and the version or draft it resolved to.
            next_cursor (None | str):
            required_bump (ContractDiffOutRequiredBump):
            to (ContractDiffSideOut): One side of a diff: the ref as named and the version or draft it resolved to.
            truncated (bool):
     """

    changes: list[ContractDiffOutChangesItem]
    changes_total: int
    classifier_version: str
    counts: ContractDiffOutCounts
    from_: ContractDiffSideOut
    next_cursor: None | str
    required_bump: ContractDiffOutRequiredBump
    to: ContractDiffSideOut
    truncated: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.contract_diff_out_changes_item import ContractDiffOutChangesItem # noqa: PLC0415
        from ..models.contract_diff_out_counts import ContractDiffOutCounts # noqa: PLC0415
        from ..models.contract_diff_side_out import ContractDiffSideOut # noqa: PLC0415
        changes = []
        for changes_item_data in self.changes:
            changes_item = changes_item_data.to_dict()
            changes.append(changes_item)



        changes_total = self.changes_total

        classifier_version = self.classifier_version

        counts = self.counts.to_dict()

        from_ = self.from_.to_dict()

        next_cursor: None | str
        next_cursor = self.next_cursor

        required_bump: str = self.required_bump

        to = self.to.to_dict()

        truncated = self.truncated


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "changes": changes,
            "changes_total": changes_total,
            "classifier_version": classifier_version,
            "counts": counts,
            "from": from_,
            "next_cursor": next_cursor,
            "required_bump": required_bump,
            "to": to,
            "truncated": truncated,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.contract_diff_out_changes_item import ContractDiffOutChangesItem # noqa: PLC0415
        from ..models.contract_diff_out_counts import ContractDiffOutCounts # noqa: PLC0415
        from ..models.contract_diff_side_out import ContractDiffSideOut # noqa: PLC0415
        d = dict(src_dict)
        changes = []
        _changes = d.pop("changes")
        for changes_item_data in (_changes):
            changes_item = ContractDiffOutChangesItem.from_dict(changes_item_data)



            changes.append(changes_item)


        changes_total = d.pop("changes_total")

        classifier_version = d.pop("classifier_version")

        counts = ContractDiffOutCounts.from_dict(d.pop("counts"))




        from_ = ContractDiffSideOut.from_dict(d.pop("from"))




        def _parse_next_cursor(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))


        required_bump = check_contract_diff_out_required_bump(d.pop("required_bump"))




        to = ContractDiffSideOut.from_dict(d.pop("to"))




        truncated = d.pop("truncated")

        contract_diff_out = cls(
            changes=changes,
            changes_total=changes_total,
            classifier_version=classifier_version,
            counts=counts,
            from_=from_,
            next_cursor=next_cursor,
            required_bump=required_bump,
            to=to,
            truncated=truncated,
        )


        contract_diff_out.additional_properties = d
        return contract_diff_out

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
