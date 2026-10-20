"""Lossless UTF-8 transports for JSON identities and filesystem byte paths.

Only identity columns use transport encoding. Free text is scrubbed before
normalization, so encoding cannot hide credentials from the text scrubber.
"""

import os

IDENTITY_FIELDS = frozenset(
    {
        "session_id",
        "agent_id",
        "uuid",
        "message_id",
        "request_id",
        "tool_use_id",
        "record_session_id",
        "context_session_id",
        "parent_agent_id",
        "call_uuid",
        "result_agent_id",
    }
)
IDENTITY_LIST_FIELDS = frozenset({"result_agent_ids"})


def encode_identity(value: str) -> str:
    if value.startswith("jsonid:") or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        return "jsonid:" + value.encode("utf-8", "surrogatepass").hex()
    return value


def decode_identity(value: str) -> str:
    return (
        bytes.fromhex(value[7:]).decode("utf-8", "surrogatepass")
        if value.startswith("jsonid:")
        else value
    )


def source_identity(relative: str) -> str:
    if relative.startswith("fsbytes:") or any(0xD800 <= ord(c) <= 0xDFFF for c in relative):
        return "fsbytes:" + os.fsencode(relative).hex()
    return relative


def source_path(identity: str) -> str:
    return os.fsdecode(bytes.fromhex(identity[8:])) if identity.startswith("fsbytes:") else identity


def normalize_rows(tables: dict[str, list[dict]]) -> None:
    """Normalize once at the extractor boundary, after every text scrub."""
    for rows in tables.values():
        for row in rows:
            for key, value in row.items():
                if key in IDENTITY_LIST_FIELDS and isinstance(value, list):
                    row[key] = [
                        encode_identity(item) if isinstance(item, str) else item for item in value
                    ]
                    continue
                if not isinstance(value, str):
                    continue
                if key == "source_file":
                    row[key] = source_identity(value)
                elif key in IDENTITY_FIELDS:
                    row[key] = encode_identity(value)
                else:
                    row[key] = value.encode("utf-8", "replace").decode("utf-8")
