"""Observe undeclared reads of secrets.env keys in MCP child processes."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

_original_environ_class: type | None = None
_seen_keys: set[str] = set()
_seen_lock = threading.Lock()


def _caller() -> str:
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "<unknown>")
        if module not in {"os", "_collections_abc", "collections.abc", __name__}:
            return f"{module}:{frame.f_code.co_name}"
        frame = frame.f_back
    return "<unknown>:<unknown>"


def install(
    server: str,
    *,
    declared: frozenset[str],
    declared_prefixes: tuple[str, ...],
    secret_keys: frozenset[str],
    log_path: Path,
) -> None:
    """Record first reads of secret keys outside a server's declarations."""
    global _original_environ_class

    uninstall()
    if os.environ.get("GENESIS_MCP_ENV_RECORDER") == "0":
        return

    environ_class = type(os.environ)
    recording = threading.local()

    def record(key: str) -> None:
        if (
            key not in secret_keys
            or key in declared
            or any(key.startswith(prefix) for prefix in declared_prefixes)
        ):
            return
        with _seen_lock:
            if key in _seen_keys:
                return
            _seen_keys.add(key)
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "server": server,
                "key": key,
                "pid": os.getpid(),
                "caller": _caller(),
            }
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry) + "\n")

    class RecordingEnviron(environ_class):
        def __getitem__(self, key):
            if isinstance(key, str) and not getattr(recording, "active", False):
                recording.active = True
                try:
                    record(key)
                except Exception:
                    pass
                finally:
                    recording.active = False
            return super().__getitem__(key)

    os.environ.__class__ = RecordingEnviron
    _original_environ_class = environ_class


def uninstall() -> None:
    """Restore the environment mapping class captured by ``install``."""
    global _original_environ_class

    if _original_environ_class is not None:
        os.environ.__class__ = _original_environ_class
        _original_environ_class = None
