from typing import Literal

ContractDiffSideOutKind = Literal['draft', 'version']

CONTRACT_DIFF_SIDE_OUT_KIND_VALUES: set[ContractDiffSideOutKind] = { 'draft', 'version',  }

def check_contract_diff_side_out_kind(value: str) -> ContractDiffSideOutKind:
    if value in CONTRACT_DIFF_SIDE_OUT_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTRACT_DIFF_SIDE_OUT_KIND_VALUES!r}")
