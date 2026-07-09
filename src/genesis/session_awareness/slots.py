"""Two-slot flock semaphore for ambient workers.

Same primitive as ``genesis.util.process_lock.ProcessLock`` (flock
LOCK_EX|LOCK_NB, auto-release on process death — no stale-lock problem)
generalized to N slots and returning failure instead of ``sys.exit``:
the worker is fire-and-forget, so "slots busy" is a recorded outcome,
not a process error.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

SLOT_COUNT = 2
ACQUIRE_TIMEOUT_S = 15.0  # a fire's context goes stale fast; don't queue
_POLL_S = 0.5


def default_lock_dir() -> Path:
    return Path.home() / ".genesis" / "session_awareness" / "locks"


class SlotHandle:
    """A held slot. Release explicitly or let process death release it."""

    def __init__(self, index: int, fd: int):
        self.index = index
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def try_acquire_slot(lock_dir: Path | None = None) -> SlotHandle | None:
    """Try each slot once; None if all are held."""
    lock_dir = lock_dir or default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    for i in range(SLOT_COUNT):
        fd = os.open(str(lock_dir / f"slot-{i}.lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return SlotHandle(i, fd)
    return None


async def acquire_slot(
    lock_dir: Path | None = None,
    timeout_s: float | None = None,
) -> SlotHandle | None:
    """Poll for a slot up to *timeout_s* (default ACQUIRE_TIMEOUT_S,
    resolved at call time so tests can shrink it); None on timeout."""
    if timeout_s is None:
        timeout_s = ACQUIRE_TIMEOUT_S
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        handle = try_acquire_slot(lock_dir)
        if handle is not None:
            return handle
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_S)
