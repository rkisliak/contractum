from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.identity_password_rules_out import IdentityPasswordRulesOut
  from ..models.identity_sign_in_methods_out import IdentitySignInMethodsOut





T = TypeVar("T", bound="IdentityAuthPolicyOut")



@_attrs_define
class IdentityAuthPolicyOut:
    """ What a sign-in screen may know before anyone is signed in.

        Attributes:
            methods (IdentitySignInMethodsOut):
            password_rules (IdentityPasswordRulesOut):
     """

    methods: IdentitySignInMethodsOut
    password_rules: IdentityPasswordRulesOut
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.identity_password_rules_out import IdentityPasswordRulesOut # noqa: PLC0415
        from ..models.identity_sign_in_methods_out import IdentitySignInMethodsOut # noqa: PLC0415
        methods = self.methods.to_dict()

        password_rules = self.password_rules.to_dict()


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "methods": methods,
            "password_rules": password_rules,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.identity_password_rules_out import IdentityPasswordRulesOut # noqa: PLC0415
        from ..models.identity_sign_in_methods_out import IdentitySignInMethodsOut # noqa: PLC0415
        d = dict(src_dict)
        methods = IdentitySignInMethodsOut.from_dict(d.pop("methods"))




        password_rules = IdentityPasswordRulesOut.from_dict(d.pop("password_rules"))




        identity_auth_policy_out = cls(
            methods=methods,
            password_rules=password_rules,
        )


        identity_auth_policy_out.additional_properties = d
        return identity_auth_policy_out

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
