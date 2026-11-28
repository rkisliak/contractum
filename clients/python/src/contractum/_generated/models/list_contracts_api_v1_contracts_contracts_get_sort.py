from typing import Literal

ListContractsApiV1ContractsContractsGetSort = Literal['id', 'name']

LIST_CONTRACTS_API_V1_CONTRACTS_CONTRACTS_GET_SORT_VALUES: set[ListContractsApiV1ContractsContractsGetSort] = { 'id', 'name',  }

def check_list_contracts_api_v1_contracts_contracts_get_sort(value: str) -> ListContractsApiV1ContractsContractsGetSort:
    if value in LIST_CONTRACTS_API_V1_CONTRACTS_CONTRACTS_GET_SORT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIST_CONTRACTS_API_V1_CONTRACTS_CONTRACTS_GET_SORT_VALUES!r}")
