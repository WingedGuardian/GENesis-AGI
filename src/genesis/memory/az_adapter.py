"""Backward-compat shim — AZ memory adapter has moved to genesis.hosting.agent_zero.memory_compat."""

from genesis.hosting.agent_zero.memory_compat import (
    AREAS,
    AZ_TIMESTAMP_FMT,
    _generate_id,
    doc_to_payload,
    extract_area_filter,
    memory_subdir_to_collection,
    payload_to_doc,
)

__all__ = [
    "AREAS",
    "AZ_TIMESTAMP_FMT",
    "_generate_id",
    "doc_to_payload",
    "extract_area_filter",
    "memory_subdir_to_collection",
    "payload_to_doc",
]
