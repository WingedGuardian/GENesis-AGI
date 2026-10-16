"""Observe undeclared reads of secrets.env keys in MCP child processes."""

from __future__ import annotations

import json
import os
import stat
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


def _open_regular_log(log_path: Path, flags: int) -> int:
    """Refuse a final symlink or special file without waiting for a peer."""
    fd = os.open(log_path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("recorder log must be a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _load_seen_keys(log_path: Path) -> set[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    try:
        with os.fdopen(_open_regular_log(log_path, os.O_RDONLY), "rb") as stream:
            # Only inspect the bounded snapshot the append threshold can cover.
            snapshot = stream.read(_MAX_LOG_BYTES).decode("utf-8", errors="replace")
            # A final unterminated fragment may be a short write, not a record.
            for line in snapshot.split("\n")[:-1]:
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
                fd = _open_regular_log(
                    log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                )
                if (os.fstat(fd).st_mode & 0o777) != 0o600:
                    os.fchmod(fd, 0o600)
                if os.fstat(fd).st_size >= _MAX_LOG_BYTES:
                    return
                # The leading separator isolates any interrupted prior tail.
                # One append syscall keeps complete frames together across children;
                # blank separator lines are ignored by the preload reader.
                frame = ("\n" + json.dumps(entry) + "\n").encode("utf-8")
                if os.write(fd, frame) != len(frame):
                    return
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
