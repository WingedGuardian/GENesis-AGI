"""Settings I/O without destructive pathname updates.

This is a single-user lifecycle boundary, not isolation from a malicious process
of the same uid. Cooperating readers/writers lock the open inode. A foreign
pathname replacement is never followed, overwritten or removed by an update.
Interrupted writes may leave invalid settings; callers must refuse execution and
use the independent native backend stop path. No new recovery journal.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
from pathlib import Path

TEXT = dict(encoding="utf-8", errors="surrogateescape", newline="")


@contextlib.contextmanager
def lifecycle_lock(directory: Path):
    fd = os.open(directory / ".genesis-codebase-config.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "a") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid lifecycle lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield stream


@contextlib.contextmanager
def document(path: Path, *, write: bool = False):
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(f"managed symlink refused: {path}") from error
        raise
    with os.fdopen(fd, "r+" if write else "r", **TEXT) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (write and info.st_nlink != 1):
            raise ValueError(f"managed path is not a single-link regular file: {path}")
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield stream


def read_file(path: Path) -> str:
    with document(path) as stream:
        return stream.read()


def update_settings(path: Path, value: dict) -> None:
    """Only enabled changes; preserve every other setting and every foreign path.

    Do not hold this inode lock across service startup: ready/serve read it too.
    Truncate first so an interrupted write is invalid rather than partly enabled.
    The post-write path check reports interference; it is not a namespace CAS.
    """
    with document(path, write=True) as stream:
        previous = json.load(stream)
        if not isinstance(previous, dict):
            raise ValueError("invalid managed settings")
        old = {k: v for k, v in previous.items() if k != "enabled"}
        new = {k: v for k, v in value.items() if k != "enabled"}
        if old != new or type(value.get("enabled")) is not bool:
            raise ValueError("settings changed; refusing lifecycle update")
        info = os.fstat(stream.fileno())
        stream.seek(0)
        stream.truncate(0)
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        current = os.lstat(path)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("settings pathname changed; replacement preserved")
