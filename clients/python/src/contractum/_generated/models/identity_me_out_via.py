from typing import Literal

IdentityMeOutVia = Literal['jwt', 'key', 'pat']

IDENTITY_ME_OUT_VIA_VALUES: set[IdentityMeOutVia] = { 'jwt', 'key', 'pat',  }

def check_identity_me_out_via(value: str) -> IdentityMeOutVia:
    if value in IDENTITY_ME_OUT_VIA_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {IDENTITY_ME_OUT_VIA_VALUES!r}")
