from typing import Literal

ContractVersionMetadataOutStatus = Literal['latest', 'older', 'preview']

CONTRACT_VERSION_METADATA_OUT_STATUS_VALUES: set[ContractVersionMetadataOutStatus] = { 'latest', 'older', 'preview',  }

def check_contract_version_metadata_out_status(value: str) -> ContractVersionMetadataOutStatus:
    if value in CONTRACT_VERSION_METADATA_OUT_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTRACT_VERSION_METADATA_OUT_STATUS_VALUES!r}")
