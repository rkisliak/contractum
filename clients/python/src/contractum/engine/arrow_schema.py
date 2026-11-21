"""The Arrow schema a contract implies (the inverse of the type checks).

``contract_to_arrow_schema`` gives a data engineer the exact schema to build a table with so that
it passes the contract's presence and type checks. A property's comparable ``physicalType`` wins
over its ``logicalType``; nested objects become structs and arrays lists of their items type, and
a field is nullable unless the contract marks the property ``required``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from contractum.engine.types import parse_physical_type, to_arrow_type, vendor_to_arrow
from contractum.odcs import Property, find_object


def contract_to_arrow_schema(
    document: Mapping[str, Any], object_name: str | None = None
) -> pa.Schema:
    """The Arrow schema of one schema object, fields in contract order.

    Fields are named by ``physicalName`` when a property has one, else by ``name``. A property
    whose types name nothing that maps to Arrow raises a ``UsageError``.
    """
    obj = find_object(document, object_name)
    return pa.schema([_field(prop) for prop in obj.properties])


def _field(prop: Property) -> pa.Field:
    return pa.field(prop.source_name, _type_of(prop), nullable=not prop.required)


def _type_of(prop: Property) -> pa.DataType:
    if _has_physical_mapping(prop):
        return to_arrow_type(prop)
    logical = (prop.logical_type or "").lower()
    if logical == "object" and prop.nested:
        return pa.struct([_field(item) for item in prop.nested])
    if logical == "array" and prop.items is not None:
        return pa.list_(_field_of_items(prop.items))
    return to_arrow_type(prop)


def _field_of_items(items: Property) -> pa.Field:
    return pa.field("item", _type_of(items), nullable=not items.required)


def _has_physical_mapping(prop: Property) -> bool:
    text = prop.physical_type
    return text is not None and (
        parse_physical_type(text) is not None or vendor_to_arrow(text) is not None
    )
