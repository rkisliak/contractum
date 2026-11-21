from typing import Literal

ContractVersionMetadataOutLifecycle = Literal['published', 'unpublished']

CONTRACT_VERSION_METADATA_OUT_LIFECYCLE_VALUES: set[ContractVersionMetadataOutLifecycle] = { 'published', 'unpublished',  }

def check_contract_version_metadata_out_lifecycle(value: str) -> ContractVersionMetadataOutLifecycle:
    if value in CONTRACT_VERSION_METADATA_OUT_LIFECYCLE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTRACT_VERSION_METADATA_OUT_LIFECYCLE_VALUES!r}")
