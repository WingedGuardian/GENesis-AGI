"""Observe undeclared reads of secrets.env keys in MCP child processes."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

_original_environ_class: type | None = None
_seen_keys: set[tuple[str, str, str]] = set()
_seen_lock = threading.Lock()
_MAX_LOG_BYTES = 1024 * 1024


def _caller() -> str:
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "<unknown>")
        if module not in {"os", "_collections_abc", "collections.abc", __name__}:
            return f"{module}:{frame.f_code.co_name}"
        frame = frame.f_back
    return "<unknown>:<unknown>"


def _load_seen_keys(log_path: Path) -> set[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                server_name, key_name, caller = (
                    entry.get("server"),
                    entry.get("key"),
                    entry.get("caller"),
                )
                if all(isinstance(value, str) for value in (server_name, key_name, caller)):
                    seen.add((server_name, key_name, caller))
    except OSError:
        return set()
    return seen


def install(
    server: str,
    *,
    declared: frozenset[str],
    declared_prefixes: tuple[str, ...],
    secret_keys: frozenset[str],
    log_path: Path,
) -> None:
    """Record secret-read triples; concurrent children may append the same triple once."""
    global _original_environ_class

    uninstall()
    if os.environ.get("GENESIS_MCP_ENV_RECORDER") == "0":
        with _seen_lock:
            _seen_keys.clear()
        return

    with _seen_lock:
        _seen_keys.clear()
        _seen_keys.update(_load_seen_keys(log_path))

    environ_class = type(os.environ)
    recording = threading.local()

    def record(key: str) -> None:
        if (
            key not in secret_keys
            or key in declared
            or any(key.startswith(prefix) for prefix in declared_prefixes)
        ):
            return
        caller = _caller()
        seen_key = (server, key, caller)
        with _seen_lock:
            if seen_key in _seen_keys:
                return
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "server": server,
                "key": key,
                "pid": os.getpid(),
                "caller": caller,
            }
            fd: int | None = None
            try:
                log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(
                    log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                if (os.fstat(fd).st_mode & 0o777) != 0o600:
                    os.chmod(log_path, 0o600)
                if os.fstat(fd).st_size >= _MAX_LOG_BYTES:
                    return
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    fd = None
                    stream.write(json.dumps(entry) + "\n")
            finally:
                if fd is not None:
                    os.close(fd)
            _seen_keys.add(seen_key)

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
