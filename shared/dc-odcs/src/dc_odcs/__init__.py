"""ODCS ingest library: strict parsing, apiVersion gate, vendored-schema structural report."""

from dc_odcs.diff import CLASSIFIER_VERSION, BumpVerdict, Change, DiffResult, compare, required_bump
from dc_odcs.element_refs import (
    ReferenceInputs,
    reference_inputs,
    resolve_internal,
)
from dc_odcs.errors import ContractRejected
from dc_odcs.extract import SearchFields, extract_search_fields
from dc_odcs.gate import (
    ALLOWED_MEDIA_TYPES,
    MAX_ODCS_ID_LENGTH,
    SUPPORTED_API_VERSIONS,
    ParsedContract,
    parse_contract,
)
from dc_odcs.loader import (
    MAX_NESTING_DEPTH,
    NestingTooDeepError,
    StrictCLoader,
    load_json,
    load_yaml,
)
from dc_odcs.pointer import string_at
from dc_odcs.refs import MAX_REFS, MAX_VERSION_CHARS, ContractRef, extract_contract_refs
from dc_odcs.validate import (
    StructuralError,
    StructuralReport,
    schema_manifest_sha256,
    unknown_properties_message,
    validate_structure,
)

__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "CLASSIFIER_VERSION",
    "MAX_NESTING_DEPTH",
    "MAX_ODCS_ID_LENGTH",
    "MAX_REFS",
    "MAX_VERSION_CHARS",
    "SUPPORTED_API_VERSIONS",
    "BumpVerdict",
    "Change",
    "ContractRef",
    "ContractRejected",
    "DiffResult",
    "NestingTooDeepError",
    "ParsedContract",
    "ReferenceInputs",
    "SearchFields",
    "StrictCLoader",
    "StructuralError",
    "StructuralReport",
    "compare",
    "extract_contract_refs",
    "extract_search_fields",
    "load_json",
    "load_yaml",
    "parse_contract",
    "reference_inputs",
    "resolve_internal",
    "required_bump",
    "schema_manifest_sha256",
    "string_at",
    "unknown_properties_message",
    "validate_structure",
]
