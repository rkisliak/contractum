from typing import Literal

IdentityMeGroupOutType = Literal['entra_group', 'local_group']

IDENTITY_ME_GROUP_OUT_TYPE_VALUES: set[IdentityMeGroupOutType] = { 'entra_group', 'local_group',  }

def check_identity_me_group_out_type(value: str) -> IdentityMeGroupOutType:
    if value in IDENTITY_ME_GROUP_OUT_TYPE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {IDENTITY_ME_GROUP_OUT_TYPE_VALUES!r}")
