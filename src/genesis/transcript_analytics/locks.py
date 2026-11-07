"""The single writer lock. Readers take no lock: a per-source record is always
either the old directory or the new one — never a mix — because records publish
through an atomic ``current`` symlink swap (see ``store``)."""

import contextlib
import fcntl
from pathlib import Path

from genesis.env import genesis_home

DEFAULT_LOCK = genesis_home() / "locks" / "transcript-analytics.lock"


class Busy(RuntimeError):
    """Another ingest/prune holds the lock."""


@contextlib.contextmanager
def writer(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Busy(str(lock_path)) from e
        yield
