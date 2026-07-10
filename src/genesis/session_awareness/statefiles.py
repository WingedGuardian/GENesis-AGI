"""Per-session theme statefile: ``~/.genesis/sessions/<id>/session_theme.json``.

Mirrors the proactive hook's working-set idiom: traversal-guarded path,
atomic mkstemp→os.replace writes, corrupt/missing files degrade to an
empty state, saves never raise. The statefile is the single durable
record of the session theme — it survives compaction because it lives
outside the transcript.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
FILENAME = "session_theme.json"

# A session idle longer than this is a new working context: the theme
# EMA restarts rather than blending yesterday's topic into today's.
STALE_AFTER = timedelta(hours=24)


def _sessions_dir() -> Path:
    return Path.home() / ".genesis" / "sessions"


def theme_path(session_id: str, base: Path | None = None) -> Path | None:
    """Statefile path, or None for unusable session IDs.

    Session IDs arrive via hook stdin — refuse anything that could
    escape the sessions dir (mirrors the hook's ``_ws_path``).
    """
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    return (base or _sessions_dir()) / session_id / FILENAME


def empty_state(session_id: str) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "ema": None,  # list[float] | None — the theme vector
        "ema_turns": 0,  # genuine folded turns (outliers excluded)
        "ring": [],  # last-3 EMA snapshots for the stability check
        "entities": {},  # keyword -> decayed weight
        "fired": [],  # [{"ema": [...], "turn": int, "at": iso}]
        "fired_count": 0,
        "worker_pending_since": None,  # iso | None — trigger's claim
        "outlier_skips": 0,  # cross-backend guard counter (total)
        "consecutive_outliers": 0,  # current run; a full run = theme change
        "updated_at": None,
    }


def _valid_shape(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("ring"), list)
        and isinstance(data.get("entities"), dict)
        and isinstance(data.get("fired"), list)
    )


def load_state(
    session_id: str,
    base: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Load the theme state. Empty state if missing, corrupt, or stale."""
    path = theme_path(session_id, base)
    if path is None:
        return empty_state(session_id)
    try:
        if not path.exists():
            return empty_state(session_id)
        data = json.loads(path.read_text())
        if not _valid_shape(data):
            return empty_state(session_id)
        updated = data.get("updated_at")
        if updated:
            now = now or datetime.now(UTC)
            if now - datetime.fromisoformat(updated) > STALE_AFTER:
                return empty_state(session_id)
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("session_id", session_id)
        for key, default in empty_state(session_id).items():
            data.setdefault(key, default)
        return data
    except Exception:
        return empty_state(session_id)


def save_state(session_id: str, state: dict, base: Path | None = None) -> None:
    """Atomic write (mkstemp→replace). Never raises."""
    path = theme_path(session_id, base)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(state).encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        pass  # Ambient state must never block the hook
