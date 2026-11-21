from typing import Literal

IdentityMeOutIdp = Literal['entra', 'local']

IDENTITY_ME_OUT_IDP_VALUES: set[IdentityMeOutIdp] = { 'entra', 'local',  }

def check_identity_me_out_idp(value: str) -> IdentityMeOutIdp:
    if value in IDENTITY_ME_OUT_IDP_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {IDENTITY_ME_OUT_IDP_VALUES!r}")
