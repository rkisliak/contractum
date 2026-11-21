from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="IdentityPasswordRulesOut")



@_attrs_define
class IdentityPasswordRulesOut:
    """ 
        Attributes:
            min_length (int):
            require_digit (bool):
            require_lower (bool):
            require_symbol (bool):
            require_upper (bool):
     """

    min_length: int
    require_digit: bool
    require_lower: bool
    require_symbol: bool
    require_upper: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        min_length = self.min_length

        require_digit = self.require_digit

        require_lower = self.require_lower

        require_symbol = self.require_symbol

        require_upper = self.require_upper


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "min_length": min_length,
            "require_digit": require_digit,
            "require_lower": require_lower,
            "require_symbol": require_symbol,
            "require_upper": require_upper,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        min_length = d.pop("min_length")

        require_digit = d.pop("require_digit")

        require_lower = d.pop("require_lower")

        require_symbol = d.pop("require_symbol")

        require_upper = d.pop("require_upper")

        identity_password_rules_out = cls(
            min_length=min_length,
            require_digit=require_digit,
            require_lower=require_lower,
            require_symbol=require_symbol,
            require_upper=require_upper,
        )


        identity_password_rules_out.additional_properties = d
        return identity_password_rules_out

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
