"""Session config cache — persists model/effort to disk for the SessionStart hook.

The SessionStart hook (scripts/genesis_session_context.py) runs as a
lightweight Python script with no DB access. It reads
~/.genesis/session_config.json to display the current model/effort in the
session header. This module keeps that cache file in sync.

Call sites:
  - SessionManager.get_or_create_foreground() — writes defaults on new session
  - session_set_model / session_set_effort MCP tools — writes on explicit change
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_CONFIG = Path.home() / ".genesis" / "session_config.json"


def persist_session_config(*, model: str | None = None, effort: str | None = None) -> None:
    """Write current model/effort to disk for the SessionStart hook to read.

    This is a best-effort cache write. Its only job is to keep the on-disk
    JSON in sync so the SessionStart hook sees the current value. A failure
    here is "recoverable degradation" (WARNING per observability rules) —
    the authoritative source (DB) is already correct.
    """
    import os
    import tempfile

    try:
        data: dict = {}
        if _SESSION_CONFIG.exists():
            data = json.loads(_SESSION_CONFIG.read_text())
        if model is not None:
            data["model"] = model
        if effort is not None:
            data["effort"] = effort
        _SESSION_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_SESSION_CONFIG.parent, suffix=".tmp")
        try:
            os.write(fd, json.dumps(data).encode())
            os.close(fd)
            os.replace(tmp, _SESSION_CONFIG)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
                os.unlink(tmp)
            raise
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning(
            "Failed to persist session config cache at %s",
            _SESSION_CONFIG, exc_info=True,
        )
