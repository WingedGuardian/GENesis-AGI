"""Translation layer: AZ LangChain Document <-> Genesis Qdrant payload.

This module provides conversion utilities for the eventual AZ memory plugin.
It does NOT import AZ code — it works with dicts matching AZ's Document format.
Designed to be plugged into a replacement memory plugin after rebase.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

# AZ Memory areas — mirrors AZ's Memory.Area enum values
AREAS = ("main", "fragments", "solutions")

# AZ timestamp format (different from ISO 8601)
AZ_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

# Fields that map between AZ metadata and Qdrant payload
_AZ_TO_QDRANT = {
    "id": "memory_id",
    "timestamp": "created_at",
    "area": "area",
    "knowledge_source": "knowledge_source",
    "source_file": "source_file",
    "file_type": "file_type",
    "consolidation_action": "consolidation_action",
    "tags": "tags",
}


def doc_to_payload(page_content: str, metadata: dict) -> dict:
    """Convert AZ Document fields to Qdrant payload format.

    Args:
        page_content: The document's text content.
        metadata: The document's metadata dict.

    Returns:
        Dict suitable for Qdrant point payload.
    """
    # Generate UUID for Qdrant point ID (AZ uses short random strings)
    memory_id = metadata.get("id", _generate_id())

    # Convert AZ timestamp to ISO 8601
    az_ts = metadata.get("timestamp", "")
    created_at = _az_timestamp_to_iso(az_ts) if az_ts else _now_iso()

    payload = {
        "content": page_content,
        "memory_id": memory_id,
        "area": metadata.get("area", "main"),
        "created_at": created_at,
        "source_type": "memory",
        "memory_type": "episodic",
        "tags": metadata.get("tags", []),
        "confidence": 0.5,
        "retrieved_count": 0,
    }

    # Pass through known AZ metadata fields
    for az_key in ("knowledge_source", "source_file", "file_type",
                    "consolidation_action"):
        if az_key in metadata:
            payload[az_key] = metadata[az_key]

    # Pass through any extra metadata fields not in the standard mapping
    for k, v in metadata.items():
        if k not in _AZ_TO_QDRANT and k not in payload:
            payload[k] = v

    return payload


def payload_to_doc(payload: dict, score: float = 0.0) -> dict:
    """Convert Qdrant payload to AZ Document-compatible dict.

    Returns a dict with 'page_content' and 'metadata' keys,
    matching LangChain Document structure.
    """
    created_at = payload.get("created_at", "")
    az_timestamp = _iso_to_az_timestamp(created_at) if created_at else ""

    metadata = {
        "id": payload.get("memory_id", ""),
        "area": payload.get("area", "main"),
        "timestamp": az_timestamp,
        "knowledge_source": payload.get("knowledge_source", False),
        "source_file": payload.get("source_file", ""),
        "file_type": payload.get("file_type", ""),
        "consolidation_action": payload.get("consolidation_action", ""),
        "tags": payload.get("tags", []),
        "_score": score,
    }

    return {
        "page_content": payload.get("content", ""),
        "metadata": metadata,
    }


def extract_area_filter(filter_str: str) -> str | None:
    """Extract area value from AZ's filter syntax.

    AZ uses simple_eval compatible strings like:
        area == 'fragments'
        area == "main"

    Returns the area value or None if not an area filter.
    """
    match = re.match(r"""area\s*==\s*['"](\w+)['"]""", filter_str.strip())
    return match.group(1) if match else None


def memory_subdir_to_collection(memory_subdir: str) -> str:
    """Map AZ memory subdirectory to Qdrant collection.

    All AZ memories go to episodic_memory collection,
    distinguished by a 'memory_subdir' payload field.
    """
    return "episodic_memory"


def _generate_id() -> str:
    """Generate a 10-char random ID matching AZ's format."""
    return uuid.uuid4().hex[:10]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _az_timestamp_to_iso(az_ts: str) -> str:
    """Convert AZ timestamp format to ISO 8601."""
    try:
        dt = datetime.strptime(az_ts, AZ_TIMESTAMP_FMT)
        return dt.replace(tzinfo=UTC).isoformat()
    except ValueError:
        return az_ts  # Already ISO or unknown format — pass through


def _iso_to_az_timestamp(iso_ts: str) -> str:
    """Convert ISO 8601 to AZ timestamp format."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime(AZ_TIMESTAMP_FMT)
    except ValueError:
        return iso_ts  # Unknown format — pass through
