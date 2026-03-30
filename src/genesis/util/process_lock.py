"""Process singleton guard using fcntl file locking."""

import contextlib
import fcntl
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PID_DIR = Path.home() / ".genesis"

# Distinct exit code for "another instance is already running".
# Used by systemd RestartPreventExitStatus to avoid crash-looping
# when a duplicate instance is launched.
EXIT_ALREADY_RUNNING = 200


class ProcessLock:
    """Ensures only one instance of a named process runs at a time.

    Uses fcntl.flock() — lock auto-releases on process death (even SIGKILL).
    No stale lockfile problem.

    Usage:
        with ProcessLock("bridge"):
            asyncio.run(main())
    """

    def __init__(self, name: str, pid_dir: Path | None = None):
        self._name = name
        self._dir = pid_dir or _DEFAULT_PID_DIR
        self._lock_path = self._dir / f"{name}.lock"
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def __enter__(self) -> "ProcessLock":
        self._dir.mkdir(parents=True, exist_ok=True)

        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)

        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            existing_pid = os.read(self._fd, 32).decode().strip()
            os.close(self._fd)
            self._fd = None
            log.error(
                "%s is already running (PID %s, lock: %s)",
                self._name,
                existing_pid or "unknown",
                self._lock_path,
            )
            sys.exit(EXIT_ALREADY_RUNNING)

        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, str(os.getpid()).encode())

        log.info("Process lock acquired: %s (PID %d)", self._name, os.getpid())
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            with contextlib.suppress(OSError):
                self._lock_path.unlink()
            log.info("Process lock released: %s", self._name)
