"""Persist the Telegram getUpdates offset across restarts.

Adapted from openclaw-python update_offset_store.py (MIT license).
Atomic write via temp-file + rename to avoid corruption.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(os.environ.get("GENESIS_DATA_DIR", Path.home() / "genesis" / "data")) / "telegram"


def ensure_telegram_dir() -> Path:
    """Ensure telegram state directory exists."""
    _BASE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return _BASE_DIR


def _offset_path(account_id: str) -> Path:
    return _BASE_DIR / f"update-offset-{account_id}.json"


def read_offset(account_id: str) -> int | None:
    """Return the persisted update offset, or None if not found."""
    path = _offset_path(account_id)
    try:
        if path.exists():
            data = json.loads(path.read_text())
            offset = data.get("offset")
            if isinstance(offset, int):
                return offset
    except Exception as exc:
        logger.error("Failed to read update offset for %s: %s", account_id, exc, exc_info=True)
    return None


def write_offset(account_id: str, offset: int) -> None:
    """Persist the update offset atomically."""
    _BASE_DIR.mkdir(parents=True, exist_ok=True)
    path = _offset_path(account_id)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"offset": offset}))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)
    except Exception as exc:
        logger.error("Failed to write update offset for %s: %s", account_id, exc, exc_info=True)
        with contextlib.suppress(Exception):
            tmp.unlink(missing_ok=True)


def delete_offset(account_id: str) -> None:
    """Remove the persisted offset (e.g. on clean start)."""
    path = _offset_path(account_id)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Failed to delete update offset for %s: %s", account_id, exc)
