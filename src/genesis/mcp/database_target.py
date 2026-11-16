"""Configured memory DB equality, never an instruction to open a caller path."""

import hashlib
import os
from pathlib import Path

from genesis.mcp.shared_paths import SharedPathError

TARGET_ENDPOINT = "/mcp/v3"
TARGET_META = "genesis/databaseTarget"
TARGET_STATUS = "database_target_contract"
TARGET_VERSION = 1


def database_target(path):
    """Identify this configured logical path without disclosing its spelling."""
    canonical = Path(path).expanduser().resolve(strict=True)
    return hashlib.sha256(b"genesis-memory-sqlite-target-v1\0" + os.fsencode(canonical)).hexdigest()


def require_database_target(metadata, expected):
    """Validate the complete reserved object before entering the tool boundary."""
    value = metadata.get(TARGET_META) if isinstance(metadata, dict) else None
    if (not isinstance(value, dict) or set(value) != {"version", "target"}
            or type(value["version"]) is not int or value["version"] != TARGET_VERSION
            or not isinstance(value["target"], str) or expected is None or value["target"] != expected):
        raise SharedPathError("Shared memory database target unavailable or incompatible; restart this client")
