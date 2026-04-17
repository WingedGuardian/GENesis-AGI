"""Backward-compat shim — canonical location: genesis.hosting.agent_zero.memory_compat."""

from genesis.hosting.agent_zero.memory_compat import (  # noqa: F401
    _az_timestamp_to_iso,
    _generate_id,
    _iso_to_az_timestamp,
    _now_iso,
    doc_to_payload,
    extract_area_filter,
    memory_subdir_to_collection,
    payload_to_doc,
)
