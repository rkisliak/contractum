from typing import Literal

IdentityMeOutPrincipalType = Literal['entra_user', 'service_account', 'service_principal', 'user']

IDENTITY_ME_OUT_PRINCIPAL_TYPE_VALUES: set[IdentityMeOutPrincipalType] = { 'entra_user', 'service_account', 'service_principal', 'user',  }

def check_identity_me_out_principal_type(value: str) -> IdentityMeOutPrincipalType:
    if value in IDENTITY_ME_OUT_PRINCIPAL_TYPE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {IDENTITY_ME_OUT_PRINCIPAL_TYPE_VALUES!r}")
