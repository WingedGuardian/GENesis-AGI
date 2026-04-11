"""Shared filesystem interface for Sentinel → Guardian communication.

Writes Sentinel state and run logs to ~/.genesis/shared/sentinel/ so the
Guardian can see what the Sentinel observed, decided, and attempted if
the Sentinel itself fails.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SHARED_DIR = Path.home() / ".genesis" / "shared" / "sentinel"
_LAST_RUN_FILE = "last_run.json"
_LOG_FILE = "sentinel_log.jsonl"
_MAX_LOG_SIZE = 1_000_000  # 1MB — rotate when exceeded


def write_last_run(
    *,
    trigger_source: str,
    tier: int | None,
    diagnosis: str,
    actions_taken: list[str],
    resolved: bool,
    duration_s: float,
    session_id: str = "",
    shared_dir: Path | None = None,
) -> None:
    """Write the most recent Sentinel run result for Guardian visibility."""
    target_dir = shared_dir or _SHARED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "trigger_source": trigger_source,
        "tier": tier,
        "diagnosis": diagnosis,
        "actions_taken": actions_taken,
        "resolved": resolved,
        "duration_s": round(duration_s, 1),
        "session_id": session_id,
    }

    target_path = target_dir / _LAST_RUN_FILE
    _atomic_write(target_path, json.dumps(data, indent=2))


def append_log(entry: dict[str, Any], shared_dir: Path | None = None) -> None:
    """Append a structured log entry to the Sentinel's append-only log.

    Rotates when the file exceeds _MAX_LOG_SIZE.
    """
    target_dir = shared_dir or _SHARED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    log_path = target_dir / _LOG_FILE
    line = json.dumps({
        "timestamp": datetime.now(UTC).isoformat(),
        **entry,
    }) + "\n"

    try:
        # Rotate if too large
        if log_path.exists() and log_path.stat().st_size > _MAX_LOG_SIZE:
            rotated = log_path.with_suffix(".jsonl.1")
            if rotated.exists():
                rotated.unlink()
            log_path.rename(rotated)

        with log_path.open("a") as f:
            f.write(line)
    except OSError:
        logger.warning("Failed to append sentinel log", exc_info=True)


def write_state_for_guardian(state_data: dict, shared_dir: Path | None = None) -> None:
    """Write dispatcher state to shared dir so Guardian can read it."""
    target_dir = shared_dir or _SHARED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "sentinel_state.json"
    _atomic_write(target_path, json.dumps(state_data, indent=2))


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via tmp file + rename."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, content.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, str(path))
    except OSError:
        logger.error("Atomic write failed for %s", path, exc_info=True)
