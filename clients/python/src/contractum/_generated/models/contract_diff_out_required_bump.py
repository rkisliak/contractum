from typing import Literal

ContractDiffOutRequiredBump = Literal['major', 'minor', 'none', 'patch']

CONTRACT_DIFF_OUT_REQUIRED_BUMP_VALUES: set[ContractDiffOutRequiredBump] = { 'major', 'minor', 'none', 'patch',  }

def check_contract_diff_out_required_bump(value: str) -> ContractDiffOutRequiredBump:
    if value in CONTRACT_DIFF_OUT_REQUIRED_BUMP_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTRACT_DIFF_OUT_REQUIRED_BUMP_VALUES!r}")
