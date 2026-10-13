"""Non-blocking, whole-run advisory locks for the eval runners.

Each runner (bench, gauntlet, skill replay) refuses to start while another run
of its kind holds the lock. The lock files live in ``~/.genesis/locks/``
(``genesis_home()``, so ``GENESIS_HOME`` relocates them). They used to live in
``~/tmp``, which is scratch space: it is age-pruned by ``disk_hygiene.sh`` and,
on installs that give it its own capped volume, can be full, in which case a
pruned lock file could not be re-created and the run could not start (#2612).

TRANSITION(#2701): a runner on the previous code still locks the old
``~/tmp/.<name>.lock``, and the long-running server that schedules gauntlet
runs keeps the old code until it restarts. So a run takes the old lock as well,
and an old and a new runner cannot both proceed across an update. Only a lock
genuinely HELD by someone else (``BlockingIOError``) refuses the run. Any other
failure on the old file (``~/tmp`` unwritable or not a directory) skips that
step with a warning: the old code could not have created a lock there either.
#2701 tracks removing this step, and says when that is safe.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from genesis.env import genesis_home

logger = logging.getLogger(__name__)


@dataclass
class RunLock:
    """Handles held for the run, in acquisition order."""

    handles: list[IO[str]] = field(default_factory=list)


def lock_dir() -> Path:
    return genesis_home() / "locks"


def legacy_dir() -> Path:
    """Where the previous code kept these locks (TRANSITION(#2701))."""
    return Path.home() / "tmp"


def _check_name(name: str) -> None:
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"invalid lock name: {name!r}")


def _lock_nb(path: Path, mode: str) -> IO[str]:
    """Open *path* (creating it) and take an exclusive non-blocking flock.

    Raises ``BlockingIOError`` when another holder has it, ``OSError`` when the
    file cannot be opened. The lock is the inode, not the contents.
    """
    fh = open(path, mode)  # noqa: SIM115 — held for the run, closed in release_run_lock
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        fh.close()
        raise
    return fh


def acquire_run_lock(name: str, *, legacy_name: str | None = None) -> RunLock:
    """Take ``~/.genesis/locks/<name>``, then (if given) ``~/tmp/<legacy_name>``.

    Raises ``BlockingIOError`` if either lock is held by another process; in
    that case, and on any other error, nothing is left held. Callers map
    ``BlockingIOError`` to their own busy error.
    """
    _check_name(name)
    if legacy_name is not None:
        _check_name(legacy_name)
    directory = lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock = RunLock()
    # Append: the new file is never truncated (it may carry nothing, but there
    # is no reason to write it).
    lock.handles.append(_lock_nb(directory / name, "a"))
    if legacy_name is None:
        return lock
    legacy = legacy_dir() / legacy_name
    try:
        # Created the way the previous code did (mkdir, then open "w"), so an
        # old runner starting at the same moment meets the same file. "w" also
        # refreshes the file's mtime on every run, which keeps a lock in use
        # out of the ~/tmp age prune, as before.
        legacy.parent.mkdir(parents=True, exist_ok=True)
        lock.handles.append(_lock_nb(legacy, "w"))
    except BlockingIOError:
        release_run_lock(lock)
        raise
    except OSError as exc:
        _check_existing_legacy(lock, legacy, exc)
    except BaseException:
        release_run_lock(lock)
        raise
    return lock


def _check_existing_legacy(lock: RunLock, legacy: Path, exc: OSError) -> None:
    """The writable open of the old lock failed. Decide whether that proves
    nothing could be holding it.

    If the file does not exist (or ``~/tmp`` is not a directory), no old runner
    can hold a lock there: skip, with a warning. If it DOES exist, an old runner
    may hold it even though we cannot write it (its permissions changed while
    it was held), so check it read-only — flock works on a read-only
    descriptor. A file that exists but cannot be opened at all cannot be
    checked: refuse the run loudly rather than risk two runs at once.
    """
    try:
        exists = legacy.lstat() is not None
    except (FileNotFoundError, NotADirectoryError):
        exists = False
    except OSError:
        exists = True  # cannot even look: treat as present, so it is checked
    if not exists:
        logger.warning("run lock: skipping the old lock %s (%s)", legacy, exc)
        return
    try:
        lock.handles.append(_lock_nb(legacy, "r"))
    except BlockingIOError:
        release_run_lock(lock)
        raise
    except BaseException as exc2:
        release_run_lock(lock)
        raise OSError(
            f"cannot check the old run lock {legacy} ({exc2}); a runner from "
            "before the update may hold it, so this run is refused. Fix the "
            "file's permissions or remove it if no old runner is running."
        ) from exc2


def release_run_lock(lock: RunLock | None) -> None:
    """Release every handle, newest first. Never raises."""
    if lock is None:
        return
    while lock.handles:
        fh = lock.handles.pop()
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            fh.close()


__all__ = ["RunLock", "acquire_run_lock", "legacy_dir", "lock_dir", "release_run_lock"]
